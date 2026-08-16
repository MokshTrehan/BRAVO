#!/usr/bin/python3
"""Focused synthetic tests for the deterministic KAIST pair evaluator."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "kaist_pair_evaluator.py"
SPEC = importlib.util.spec_from_file_location("kaist_pair_evaluator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _identity(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
    }


def _canonical_identity(path: Path) -> dict:
    value = _identity(path)
    return {
        "canonical_path": value["path"],
        "size_bytes": value["size_bytes"],
        "sha256": value["sha256"],
    }


def _quaternion(yaw: float) -> tuple[float, float, float, float]:
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def _trajectory_rows(
    *,
    system: str,
    start: int = 0,
    stop: int = 30,
    perfect: bool = False,
) -> list[str]:
    rows: list[str] = []
    for index in range(start, stop):
        timestamp = 1000.0 + index * 0.1
        x = index * 0.25
        y = 0.35 * math.sin(index * 0.28)
        z = 0.07 * math.cos(index * 0.17)
        yaw = 0.025 * index
        if system != "GT" and not perfect:
            phase = 0.2 if system == "U0" else 0.5
            amplitude = 0.022 if system == "U0" else 0.013
            angle = 0.31 if system == "U0" else -0.22
            cosine = math.cos(angle)
            sine = math.sin(angle)
            rotated_x = cosine * x - sine * y
            rotated_y = sine * x + cosine * y
            x = rotated_x + 2.0 + amplitude * math.sin(index * 0.43 + phase)
            y = rotated_y - 1.2 + amplitude * math.cos(index * 0.37 + phase)
            z = z + 0.4 + amplitude * math.sin(index * 0.21 + phase)
            yaw = yaw + angle + amplitude * math.sin(index * 0.19 + phase)
        if system != "GT":
            timestamp += 0.004 if system == "U0" else -0.003
        qx, qy, qz, qw = _quaternion(yaw)
        rows.append(
            " ".join(
                (
                    f"{timestamp:.9f}",
                    f"{x:.12f}",
                    f"{y:.12f}",
                    f"{z:.12f}",
                    f"{qx:.12f}",
                    f"{qy:.12f}",
                    f"{qz:.12f}",
                    f"{qw:.12f}",
                )
            )
        )
    return rows


def _write_tum(path: Path, rows: list[str]) -> None:
    path.write_text(
        "# timestamp tx ty tz qx qy qz qw\n" + "\n".join(rows) + "\n",
        encoding="ascii",
    )


def _protocol() -> dict:
    return {
        "schema": MODULE.PROTOCOL_SCHEMA,
        "protocol_id": MODULE.EXPECTED_PROTOCOL_ID,
        "status": "FROZEN_BEFORE_FIRST_U0_LAUNCH",
        "evaluation": {
            "implementation": "evo",
            "evo_version": MODULE.EXPECTED_EVO_VERSION,
            "execution_stage": "post_pair_after_both_estimators_close",
            "trajectory_format": "tum",
            "alignment": "se3_without_scale",
            "timestamp_association_max_seconds": 0.01,
            "ground_truth_population": (
                "Exact intersection of ground-truth row identities; both use "
                "an identical RPE pair set"
            ),
            "per_run_full_overlap_metrics_are_primary": False,
            "minimum_common_pose_count": MODULE.MINIMUM_COMMON_POSES,
            "minimum_common_rpe_pair_count": MODULE.MINIMUM_RPE_PAIRS,
            "rpe_relative_delta_tolerance": MODULE.RPE_RELATIVE_DELTA_TOLERANCE,
            "evaluator_runtime_pins": MODULE.runtime_pin_projection(
                MODULE.runtime_identity()
            ),
            "primary_metrics": [
                {
                    "id": "ate_translation_rmse_m",
                    "evo_tool": "evo_ape",
                    "pose_relation": "trans_part",
                },
                {
                    "id": "rpe_translation_rmse_1m_m",
                    "evo_tool": "evo_rpe",
                    "pose_relation": "trans_part",
                    "delta": 1.0,
                    "delta_unit": "m",
                    "all_pairs": True,
                    "pairs_from_reference": True,
                },
                {
                    "id": "rpe_rotation_rmse_1m_deg",
                    "evo_tool": "evo_rpe",
                    "pose_relation": "angle_deg",
                    "delta": 1.0,
                    "delta_unit": "m",
                    "all_pairs": True,
                    "pairs_from_reference": True,
                },
            ],
        },
        "attempt_status": {
            "tail_gap_max_seconds": 0.10,
            "negative_tail_tolerance_seconds": 0.01,
            "post_initialization_max_state_gap_seconds": 0.20,
            "initialization_delay_max_fraction_of_selected_input": 0.10,
        },
    }


def _runtime(system: str, artifact: Path) -> dict:
    return {
        "schema": MODULE.RUNTIME_IDENTITY_SCHEMA,
        "status": "PASS",
        "system": system,
        "policy_id": "synthetic-policy-v1",
        "policy_sha256": ("a" if system == "U0" else "b") * 64,
        "executable": _canonical_identity(artifact),
    }


def _completion() -> dict:
    return {
        "tail_gap_pass": True,
        "initialization_delay_pass": True,
        "maximum_state_gap_pass": True,
        "tail_gap_seconds": 0.02,
        "initialization_delay_seconds": 0.2,
        "initialization_delay_fraction_of_selected_input": 0.02,
        "maximum_initialization_delay_fraction": 0.10,
        "maximum_initialization_delay_seconds": 1.0,
        "selected_input_span_seconds": 10.0,
        "state_span_fraction_of_selected_input": 0.91,
        "maximum_post_initialization_state_gap_seconds": 0.1,
    }


class SyntheticPair:
    def __init__(
        self,
        root: Path,
        u0_range: tuple[int, int] = (0, 30),
        s1_range: tuple[int, int] = (0, 30),
        *,
        perfect: bool = False,
    ):
        self.root = root
        self.gt = root / "ground_truth.tum"
        _write_tum(self.gt, _trajectory_rows(system="GT"))
        self.shared_artifact = root / "shared-input.bin"
        self.shared_artifact.write_bytes(b"frozen synthetic input\n")
        self.selected_bag = root / "adapted.bag"
        self.selected_bag.write_bytes(b"synthetic bag bytes\n")
        self.protocol = root / "protocol.yaml"
        self.protocol.write_text(yaml.safe_dump(_protocol(), sort_keys=False), encoding="utf-8")
        shared = _identity(self.shared_artifact)
        u0_base = {
            "binary": shared,
            "config": shared,
            "imu_config": shared,
            "camera_imu_config": shared,
            "launch": shared,
            "setup": shared,
            "source_commit": "1" * 40,
            "source_tree": "2" * 40,
            "source_clean": True,
        }
        s1_base = {
            "binary": shared,
            "config": shared,
            "imu_config": shared,
            "camera_imu_config": shared,
            "launch": shared,
            "setup": shared,
            "build_provenance": shared,
            "estimator_source_commit": "3" * 40,
            "estimator_source_tree": "4" * 40,
        }
        self.base_inputs = {
            "protocol": _identity(self.protocol),
            "matrix": shared,
            "data_inventory": shared,
            "selected_bag": _identity(self.selected_bag),
            "ground_truth": _identity(self.gt),
            "converter": shared,
            "pairing_census_tool": shared,
            "geometry_bundle_tool": shared,
            "runtime_identity_tool": shared,
            "pair_evaluator_tool": shared,
            "U0": u0_base,
            "S1": s1_base,
        }
        self.runtimes = {
            system: _runtime(system, self.shared_artifact) for system in ("U0", "S1")
        }
        self.perfect = perfect
        self.run_dirs = {
            "U0": self._run("U0", *u0_range),
            "S1": self._run("S1", *s1_range),
        }

    def _run(self, system: str, start: int, stop: int) -> Path:
        run = self.root / system.lower()
        trajectory_dir = run / "trajectory"
        trajectory_dir.mkdir(parents=True)
        estimate = trajectory_dir / "estimate_raw.tum"
        _write_tum(
            estimate,
            _trajectory_rows(
                system=system, start=start, stop=stop, perfect=self.perfect
            ),
        )
        state = trajectory_dir / "state_estimate.txt"
        deviation = trajectory_dir / "state_deviation.txt"
        state.write_text("1 2 3 4 5 6 7 8\n", encoding="ascii")
        deviation.write_text("1 2 3 4 5 6 7 8\n", encoding="ascii")
        inputs = copy.deepcopy(self.base_inputs)
        inputs[system]["runtime_identity"] = copy.deepcopy(self.runtimes[system])
        manifest = {
            "schema": MODULE.RUN_SCHEMA,
            "protocol_id": MODULE.EXPECTED_PROTOCOL_ID,
            "run_id": f"synthetic-{system.lower()}",
            "sequence": "rotation/rotation_fast.bag",
            "system": system,
            "attempt_kind": "SCORED",
            "dry_run": True,
            "status": "COMPLETE",
            "inputs": inputs,
            "outputs": {
                "state": _identity(state),
                "deviation": _identity(deviation),
                "trajectory": _identity(estimate),
            },
            "checks": {
                "trajectory_ready_for_paired_evaluation": True,
                "numeric_estimator_outputs_valid": True,
                "static_pairing_census_present": True,
                "pairing_census_present": True,
            },
            "completion": _completion(),
            "pairing_census": {
                "census": {"estimator_output_first_timestamp_ns": 1},
                "availability": {
                    "static_input_census": "COMPLETE",
                    "estimator_output_timestamps": "COMPLETE",
                    "s1_runtime_counters": (
                        "COMPLETE" if system == "S1" else "NOT_APPLICABLE"
                    ),
                },
            },
        }
        (run / "sequence_result.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return run


class PairEvaluatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_runtime_validator = MODULE.revalidate_runtime_identity

    def tearDown(self) -> None:
        MODULE.revalidate_runtime_identity = self.original_runtime_validator

    def _install_runtime_stub(self, pair: SyntheticPair) -> None:
        MODULE.revalidate_runtime_identity = lambda system: copy.deepcopy(
            pair.runtimes[system]
        )

    def test_complete_pair_is_deterministic_and_uses_one_exact_population(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            self._install_runtime_stub(pair)
            before = {
                system: hashlib.sha256(
                    (run / "trajectory" / "estimate_raw.tum").read_bytes()
                ).hexdigest()
                for system, run in pair.run_dirs.items()
            }
            result_a = MODULE.evaluate_pair(
                pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, root / "pair_a"
            )
            result_b = MODULE.evaluate_pair(
                pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, root / "pair_b"
            )
            self.assertEqual(result_a, result_b)
            self.assertEqual(result_a["status"], "COMPLETE")
            self.assertEqual(result_a["common_population"]["count"], 30)
            population = result_a["common_population"]
            self.assertEqual(population["gt_row_ids_sha256"], population["u0_gt_row_ids_sha256"])
            self.assertEqual(population["gt_row_ids_sha256"], population["s1_gt_row_ids_sha256"])
            self.assertTrue(population["identical_population_proof"])
            self.assertGreaterEqual(result_a["rpe_reference_pairs"]["count"], 5)
            for system in ("U0", "S1"):
                self.assertEqual(
                    result_a["rpe_reference_pairs"]["pair_identities_sha256"],
                    json.loads(
                        (root / "pair_a" / "metrics" / "paired_metrics.json").read_text()
                    )["systems"][system]["rpe_pair_identities_sha256"],
                )
                self.assertTrue(
                    all(value >= 0.0 for value in result_a["metrics"][system].values())
                )
            rpe_pair_count = result_a["rpe_reference_pairs"]["count"]
            for system in ("u0", "s1"):
                rows = (
                    root / "pair_a" / "metrics" / f"{system}_rpe_1m_errors.csv"
                ).read_text().splitlines()
                self.assertEqual(len(rows), rpe_pair_count + 1)
            for system, run in pair.run_dirs.items():
                observed = hashlib.sha256(
                    (run / "trajectory" / "estimate_raw.tum").read_bytes()
                ).hexdigest()
                self.assertEqual(observed, before[system])
            self.assertEqual(
                (root / "pair_a" / "SHA256SUMS").read_bytes(),
                (root / "pair_b" / "SHA256SUMS").read_bytes(),
            )

    def test_full_rpe_tuple_reuse_distinguishes_same_end_different_start(self) -> None:
        quaternions = np.asarray([[1.0, 0.0, 0.0, 0.0]] * 3)
        timestamps = np.asarray([0.0, 1.0, 2.0])
        reference = MODULE.trajectory.PoseTrajectory3D(
            positions_xyz=np.asarray([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]),
            orientations_quat_wxyz=quaternions,
            timestamps=timestamps,
        )
        estimate = MODULE.trajectory.PoseTrajectory3D(
            positions_xyz=np.asarray([[0.5, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0]]),
            orientations_quat_wxyz=quaternions,
            timestamps=timestamps,
        )
        from_zero = MODULE.explicit_rpe_metric(
            reference,
            estimate,
            [(0, 2)],
            MODULE.metrics.PoseRelation.translation_part,
        )
        from_one = MODULE.explicit_rpe_metric(
            reference,
            estimate,
            [(1, 2)],
            MODULE.metrics.PoseRelation.translation_part,
        )
        self.assertEqual(from_zero.delta_ids, from_one.delta_ids)
        self.assertAlmostEqual(float(from_zero.error[0]), 0.5)
        self.assertEqual(float(from_one.error[0]), 0.0)

    def test_perfect_trajectories_and_zero_baseline_semantics_are_valid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root, perfect=True)
            self._install_runtime_stub(pair)
            result = MODULE.evaluate_pair(
                pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, root / "pair"
            )
            self.assertEqual(result["status"], "COMPLETE")
            for system in ("U0", "S1"):
                self.assertTrue(
                    all(value >= 0.0 for value in result["metrics"][system].values())
                )
        both_zero = MODULE.relative_difference_record(0.0, 0.0)
        self.assertEqual(both_zero["status"], "BOTH_ZERO_PARITY_CONVENTION")
        self.assertEqual(both_zero["value"], 0.0)
        self.assertFalse(both_zero["mathematical_ratio_defined"])
        positive_over_zero = MODULE.relative_difference_record(0.0, 0.5)
        self.assertEqual(positive_over_zero["status"], "UNDEFINED_ZERO_U0_POSITIVE_S1")
        self.assertIsNone(positive_over_zero["value"])
        self.assertEqual(positive_over_zero["extended_real_direction"], "POSITIVE_INFINITY")

    def test_runtime_semantic_identities_are_complete_and_protocol_pinned(self) -> None:
        runtime = MODULE.runtime_identity()
        self.assertEqual(runtime["python"]["version_info"], "3.8.10")
        self.assertEqual(runtime["evo"]["version"], "1.31.1")
        self.assertEqual(runtime["numpy"]["version"], "1.24.4")
        self.assertEqual(
            set(runtime["evo"]["semantic_modules"]),
            {
                "package", "filters", "geometry", "lie_algebra", "metrics",
                "result", "sync", "trajectory", "transformations", "units",
                "file_interface",
            },
        )
        self.assertEqual(
            set(runtime["numpy"]["semantic_modules"]),
            {
                "package", "core_numeric", "linalg_package",
                "linalg_implementation", "multiarray_umath", "umath_linalg",
            },
        )
        protocol = _protocol()
        check = MODULE.validate_protocol_runtime_pins(protocol, runtime)
        self.assertTrue(check["exact_match"])
        protocol["evaluation"]["evaluator_runtime_pins"]["numpy"]["version"] = "drift"
        with self.assertRaisesRegex(MODULE.PairEvaluationError, "differs from protocol pins"):
            MODULE.validate_protocol_runtime_pins(protocol, runtime)

    def test_intersection_uses_exact_gt_row_indices(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root, u0_range=(1, 30), s1_range=(0, 29))
            self._install_runtime_stub(pair)
            result = MODULE.evaluate_pair(
                pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, root / "pair"
            )
            common = result["common_population"]
            self.assertEqual(common["count"], 28)
            self.assertEqual(common["gt_data_index_first"], 1)
            self.assertEqual(common["gt_data_index_last"], 28)
            crop = json.loads((root / "pair" / "crop_manifest.json").read_text())
            self.assertEqual(len(crop["ground_truth_row_ids"]), 28)

    def test_live_trajectory_hash_drift_fails_without_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            self._install_runtime_stub(pair)
            trajectory = pair.run_dirs["U0"] / "trajectory" / "estimate_raw.tum"
            trajectory.write_text(trajectory.read_text() + "# drift\n", encoding="ascii")
            output = root / "pair"
            with self.assertRaisesRegex(MODULE.PairEvaluationError, "live (?:size|SHA-256)"):
                MODULE.evaluate_pair(
                    pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, output
                )
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".pair.tmp-*")), [])

    def test_ineligible_completion_check_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            self._install_runtime_stub(pair)
            manifest_path = pair.run_dirs["S1"] / "sequence_result.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["checks"]["trajectory_ready_for_paired_evaluation"] = False
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            output = root / "pair"
            with self.assertRaisesRegex(
                MODULE.PairEvaluationError, "paired-evaluation checks"
            ):
                MODULE.evaluate_pair(
                    pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, output
                )
            self.assertFalse(output.exists())

    def test_post_rename_fsync_failure_rolls_back_installed_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            self._install_runtime_stub(pair)
            output = root / "pair"
            original_fsync = MODULE.os.fsync

            def fail_after_install(file_descriptor: int) -> None:
                target = Path(MODULE.os.readlink(f"/proc/self/fd/{file_descriptor}"))
                if output.exists() and target.resolve() == root.resolve():
                    raise OSError("synthetic post-rename directory fsync failure")
                original_fsync(file_descriptor)

            MODULE.os.fsync = fail_after_install
            try:
                with self.assertRaisesRegex(OSError, "post-rename"):
                    MODULE.evaluate_pair(
                        pair.run_dirs["U0"], pair.run_dirs["S1"], pair.gt, output
                    )
            finally:
                MODULE.os.fsync = original_fsync
            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".pair.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
