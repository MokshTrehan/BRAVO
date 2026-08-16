#!/usr/bin/python3.8
"""Synthetic contract tests for the cross-dataset paired evaluator."""

from __future__ import annotations

import copy
import csv
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "cross_dataset_pair_evaluator.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_pair_evaluator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def file_identity(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    payload = resolved.read_bytes()
    return {
        "path": str(resolved),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def quaternion(yaw: float) -> tuple[float, float, float, float]:
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def trajectory_rows(system: str, start: int, stop: int) -> list[str]:
    rows = []
    for index in range(start, stop):
        timestamp = 1000.0 + 0.1 * index
        x = 0.25 * index
        y = 0.35 * math.sin(0.28 * index)
        z = 0.07 * math.cos(0.17 * index)
        yaw = 0.025 * index
        if system != "GT":
            phase = 0.2 if system == "U0" else 0.5
            amplitude = 0.022 if system == "U0" else 0.013
            angle = 0.31 if system == "U0" else -0.22
            cosine = math.cos(angle)
            sine = math.sin(angle)
            x, y = (
                cosine * x - sine * y + 2.0,
                sine * x + cosine * y - 1.2,
            )
            x += amplitude * math.sin(0.43 * index + phase)
            y += amplitude * math.cos(0.37 * index + phase)
            z += 0.4 + amplitude * math.sin(0.21 * index + phase)
            yaw += angle + amplitude * math.sin(0.19 * index + phase)
            timestamp += 0.004 if system == "U0" else -0.003
        qx, qy, qz, qw = quaternion(yaw)
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


def write_tum(path: Path, rows: list[str]) -> None:
    path.write_text(
        "# timestamp tx ty tz qx qy qz qw\n" + "\n".join(rows) + "\n",
        encoding="ascii",
    )


class SyntheticPair:
    def __init__(
        self,
        root: Path,
        *,
        gt_count: int = 150,
        u0_range: tuple[int, int] = (0, 150),
        s1_range: tuple[int, int] = (20, 150),
    ) -> None:
        self.root = root
        self.gt = root / "ground_truth.tum"
        write_tum(self.gt, trajectory_rows("GT", 0, gt_count))
        self.shared = root / "frozen_input.bin"
        self.shared.write_bytes(b"frozen synthetic input\n")
        self.protocol = root / "protocol.md"
        self.protocol.write_text("synthetic frozen protocol\n", encoding="utf-8")
        self.matrix = root / "matrix.yaml"
        self.matrix_value = {
            "schema": MODULE.MATRIX_SCHEMA,
            "sequences": [
                {
                    "order": 1,
                    "dataset": "synthetic",
                    "sequence": "long_curve",
                    "bag": {
                        "path": str(self.shared.resolve()),
                        "bytes": self.shared.stat().st_size,
                        "sha256": hashlib.sha256(self.shared.read_bytes()).hexdigest(),
                    },
                    "ground_truth": {
                        "capability": "full_trajectory",
                        "canonical_path": str(self.gt.resolve()),
                        "bytes": self.gt.stat().st_size,
                        "sha256": hashlib.sha256(self.gt.read_bytes()).hexdigest(),
                    },
                }
            ],
        }
        self.write_matrix()
        self.runs = {
            "U0": self._make_run("U0", *u0_range),
            "S1": self._make_run("S1", *s1_range),
        }

    def _make_run(self, system: str, start: int, stop: int) -> Path:
        run = self.root / system.lower()
        trajectory = run / "trajectory"
        trajectory.mkdir(parents=True)
        state = trajectory / "state_estimate.txt"
        deviation = trajectory / "state_deviation.txt"
        estimate = trajectory / "estimate_raw.tum"
        state.write_text("1000.0 0 0 0 0 0 0 1\n", encoding="ascii")
        deviation.write_text("1000.0 1 1 1 1 1 1\n", encoding="ascii")
        write_tum(estimate, trajectory_rows(system, start, stop))
        manifest = {
            "schema": MODULE.RUN_SCHEMA,
            "protocol_id": "CDSC-1R4",
            "dataset": "synthetic",
            "sequence": "long_curve",
            "run_id": f"synthetic-{system.lower()}",
            "system": system,
            "mode": "scored",
            "status": "COMPLETED",
            "accuracy_eligible": True,
            "inputs": {
                "protocol": file_identity(self.protocol),
                "matrix": file_identity(self.matrix),
                "bag": file_identity(self.shared),
                "ground_truth": file_identity(self.gt),
            },
            "artifacts": {
                "state": {
                    "relative_path": "trajectory/state_estimate.txt",
                    "identity": file_identity(state),
                },
                "deviation": {
                    "relative_path": "trajectory/state_deviation.txt",
                    "identity": file_identity(deviation),
                },
                "tum": {
                    "relative_path": "trajectory/estimate_raw.tum",
                    "identity": file_identity(estimate),
                },
            },
        }
        (run / "sequence_result.json").write_text(
            json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return run

    def manifest(self, system: str) -> dict:
        return json.loads((self.runs[system] / "sequence_result.json").read_text())

    def write_manifest(self, system: str, manifest: dict) -> None:
        (self.runs[system] / "sequence_result.json").write_text(
            json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_matrix(self) -> None:
        self.matrix.write_text(
            json.dumps(self.matrix_value, allow_nan=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

    def rewrite_matrix_and_refresh_manifests(self) -> None:
        self.write_matrix()
        for system in MODULE.SYSTEMS:
            manifest = self.manifest(system)
            manifest["inputs"]["matrix"] = file_identity(self.matrix)
            self.write_manifest(system, manifest)


class CrossDatasetPairEvaluatorTests(unittest.TestCase):
    def assert_no_staging(self, root: Path, output_name: str) -> None:
        self.assertEqual(list(root.glob(f".{output_name}.staging-*")), [])

    def test_common_population_at_least_100_and_shared_exact_1m_tuples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            output = root / "pair"
            result = MODULE.evaluate_pair(
                pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
            )

            self.assertEqual(result["status"], "COMPLETE")
            self.assertEqual(result["common_population"]["count"], 130)
            self.assertGreaterEqual(
                result["common_population"]["count"], MODULE.MINIMUM_COMMON_POSES
            )
            self.assertEqual(result["common_population"]["reference_pose_count"], 150)
            self.assertEqual(result["matrix_binding"]["identity"], file_identity(pair.matrix))
            self.assertEqual(
                result["matrix_binding"]["ground_truth_capability"],
                "full_trajectory",
            )
            self.assertAlmostEqual(
                result["common_population"]["reference_pose_fraction"]["value"],
                130 / 150,
            )
            pair_record = result["rpe_reference_pairs"]
            self.assertGreaterEqual(pair_record["count"], MODULE.MINIMUM_RPE_PAIRS)
            expected_digest = pair_record["common_index_tuples_sha256"]
            for system in MODULE.SYSTEMS:
                self.assertEqual(
                    pair_record["systems"][system]["common_index_tuples_sha256"],
                    expected_digest,
                )
                self.assertEqual(
                    pair_record["systems"][system]["tuple_count"],
                    pair_record["count"],
                )
            with (output / "associations" / "rpe_pairs_1m.csv").open(
                newline="", encoding="utf-8"
            ) as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), pair_record["count"])
            checksum_lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
            self.assertNotIn("SHA256SUMS", [line.split("  ", 1)[1] for line in checksum_lines])
            for line in checksum_lines:
                digest, relative = line.split("  ", 1)
                self.assertEqual(MODULE.sha256_file(output / relative), digest)
            self.assert_no_staging(root, "pair")

    def test_quantized_quaternions_are_projected_with_raw_identity_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            quantized_rows = []
            for row in trajectory_rows("GT", 0, 150):
                tokens = row.split()
                for index in range(4, 8):
                    tokens[index] = format(float(tokens[index]) * 1.0002, ".12f")
                quantized_rows.append(" ".join(tokens))
            write_tum(pair.gt, quantized_rows)
            gt_identity = file_identity(pair.gt)
            pair.matrix_value["sequences"][0]["ground_truth"].update(
                {
                    "bytes": gt_identity["size_bytes"],
                    "sha256": gt_identity["sha256"],
                }
            )
            pair.write_matrix()
            for system in MODULE.SYSTEMS:
                manifest = pair.manifest(system)
                manifest["inputs"]["ground_truth"] = gt_identity
                manifest["inputs"]["matrix"] = file_identity(pair.matrix)
                pair.write_manifest(system, manifest)

            output = root / "quantized"
            result = MODULE.evaluate_pair(
                pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
            )
            record = result["quaternion_projection"]["ground_truth"]
            self.assertEqual(record["source_row_count"], 150)
            self.assertTrue(record["source_bytes_unchanged"])
            self.assertTrue(record["source_tokens_unchanged"])
            self.assertTrue(record["row_ids_from_raw_source_bytes"])
            self.assertGreater(record["maximum_abs_source_norm_error"], 1.0e-4)
            self.assertLessEqual(
                record["maximum_abs_source_norm_error"],
                MODULE.MAX_EVO_QUATERNION_NORM_ERROR,
            )
            common_path = output / "common/ground_truth_common.tum"
            common = MODULE.CORE.np.loadtxt(common_path)
            self.assertGreater(
                float(
                    MODULE.CORE.np.max(
                        MODULE.CORE.np.abs(
                            MODULE.CORE.np.linalg.norm(common[:, 4:8], axis=1) - 1.0
                        )
                    )
                ),
                1.0e-4,
            )
            first_common_tokens = next(
                line.split()
                for line in common_path.read_text(encoding="ascii").splitlines()
                if line and not line.startswith("#")
            )
            self.assertEqual(first_common_tokens[4:8], quantized_rows[20].split()[4:8])
            self.assertEqual(result["ground_truth"]["sha256"], gt_identity["sha256"])

    def test_quaternion_projection_rejects_over_bound_and_zero_atomically(self) -> None:
        for case in ("over_bound", "zero"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pair = SyntheticPair(root)
                changed_rows = []
                for row in trajectory_rows("GT", 0, 150):
                    tokens = row.split()
                    if case == "over_bound":
                        for index in range(4, 8):
                            tokens[index] = format(float(tokens[index]) * 1.0006, ".12f")
                    else:
                        tokens[4:8] = ["0", "0", "0", "0"]
                    changed_rows.append(" ".join(tokens))
                write_tum(pair.gt, changed_rows)
                gt_identity = file_identity(pair.gt)
                pair.matrix_value["sequences"][0]["ground_truth"].update(
                    {
                        "bytes": gt_identity["size_bytes"],
                        "sha256": gt_identity["sha256"],
                    }
                )
                pair.write_matrix()
                for system in MODULE.SYSTEMS:
                    manifest = pair.manifest(system)
                    manifest["inputs"]["ground_truth"] = gt_identity
                    manifest["inputs"]["matrix"] = file_identity(pair.matrix)
                    pair.write_manifest(system, manifest)
                output = root / "rejected"
                with self.assertRaisesRegex(
                    MODULE.EvaluationError, "evo-projection boundary"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
                self.assertFalse(output.exists())
                self.assert_no_staging(root, "rejected")

    def test_common_population_below_100_publishes_unassessable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(
                root,
                gt_count=120,
                u0_range=(0, 99),
                s1_range=(0, 99),
            )
            output = root / "too_short"
            result = MODULE.evaluate_pair(
                pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
            )
            self.assertEqual(result["status"], "UNASSESSABLE")
            self.assertEqual(
                result["unassessable_reason"],
                {
                    "code": "INSUFFICIENT_COMMON_POSES",
                    "metric_scope": "all_accuracy_metrics",
                    "minimum_required": 100,
                    "observed_count": 99,
                },
            )
            self.assertEqual(result["common_population"]["count"], 99)
            self.assertFalse(result["common_population"]["requirement_passed"])
            self.assertIsNone(result["rpe_reference_pairs"]["count"])
            self.assertEqual(result["metrics"], {})
            self.assertIn(
                "associations/common_population.csv", result["artifacts"]
            )
            self.assertTrue((output / "SHA256SUMS").is_file())
            for system in MODULE.SYSTEMS:
                self.assertEqual(
                    len(result["source_runs"][system]["validated_inputs"]), 4
                )
            self.assert_no_staging(root, "too_short")

    def test_rpe_population_below_100_publishes_unassessable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(
                root,
                gt_count=100,
                u0_range=(0, 100),
                s1_range=(0, 100),
            )
            output = root / "too_few_rpe_pairs"
            result = MODULE.evaluate_pair(
                pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
            )
            self.assertEqual(result["status"], "UNASSESSABLE")
            self.assertEqual(
                result["unassessable_reason"]["code"], "INSUFFICIENT_RPE_PAIRS"
            )
            self.assertEqual(result["common_population"]["count"], 100)
            self.assertTrue(result["common_population"]["requirement_passed"])
            rpe = result["rpe_reference_pairs"]
            self.assertLess(rpe["count"], MODULE.MINIMUM_RPE_PAIRS)
            self.assertFalse(rpe["requirement_passed"])
            self.assertEqual(result["metrics"], {})
            self.assertIn("common/ground_truth_common.tum", result["artifacts"])
            self.assertIn("associations/rpe_pairs_1m.csv", result["artifacts"])
            self.assert_no_staging(root, "too_few_rpe_pairs")

    def test_zero_timestamp_matches_publish_zero_common_population(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            estimate = pair.runs["S1"] / "trajectory" / "estimate_raw.tum"
            shifted_rows = []
            for row in trajectory_rows("S1", 20, 150):
                tokens = row.split()
                tokens[0] = f"{float(tokens[0]) + 100.0:.9f}"
                shifted_rows.append(" ".join(tokens))
            write_tum(estimate, shifted_rows)
            manifest = pair.manifest("S1")
            manifest["artifacts"]["tum"]["identity"] = file_identity(estimate)
            pair.write_manifest("S1", manifest)

            output = root / "zero_matches"
            result = MODULE.evaluate_pair(
                pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
            )
            self.assertEqual(result["status"], "UNASSESSABLE")
            self.assertEqual(
                result["unassessable_reason"]["code"],
                "INSUFFICIENT_COMMON_POSES",
            )
            self.assertEqual(result["association"]["s1_count"], 0)
            self.assertEqual(result["common_population"]["count"], 0)
            self.assertIsNone(result["common_population"]["first_timestamp"])
            self.assertIsNone(result["common_population"]["last_timestamp"])
            self.assertIsNone(result["common_population"]["duration_seconds"])
            self.assertTrue((output / "SHA256SUMS").is_file())
            self.assert_no_staging(root, "zero_matches")

    def test_matrix_binds_exact_row_bag_and_full_ground_truth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            pair.matrix_value["sequences"][0]["ground_truth"][
                "capability"
            ] = "embedded_partial_intervals"
            pair.rewrite_matrix_and_refresh_manifests()
            output = root / "partial_reference"
            with self.assertRaisesRegex(
                MODULE.EvaluationError, "full_trajectory ground truth required"
            ):
                MODULE.evaluate_pair(
                    pair.runs["U0"],
                    pair.runs["S1"],
                    pair.gt,
                    pair.matrix,
                    output,
                )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "partial_reference")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            pair.matrix_value["sequences"][0]["bag"]["sha256"] = "f" * 64
            pair.rewrite_matrix_and_refresh_manifests()
            output = root / "wrong_bag"
            with self.assertRaisesRegex(
                MODULE.EvaluationError, "selected bag identity differs from live file"
            ):
                MODULE.evaluate_pair(
                    pair.runs["U0"],
                    pair.runs["S1"],
                    pair.gt,
                    pair.matrix,
                    output,
                )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "wrong_bag")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            other_gt = root / "other_ground_truth.tum"
            write_tum(other_gt, trajectory_rows("GT", 0, 150))
            output = root / "wrong_gt"
            with self.assertRaisesRegex(
                MODULE.EvaluationError, "requested ground truth differs"
            ):
                MODULE.evaluate_pair(
                    pair.runs["U0"],
                    pair.runs["S1"],
                    other_gt,
                    pair.matrix,
                    output,
                )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "wrong_gt")

    def test_injected_rpe_tuple_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            original = MODULE.CORE.evaluate_method

            def drifting_method(system, reference, estimate, pairs):
                result = original(system, reference, estimate, pairs)
                if system == "S1":
                    result["rpe_pair_tuples"] = copy.deepcopy(
                        result["rpe_pair_tuples"]
                    )
                    result["rpe_pair_tuples"][0][0] += 1
                return result

            output = root / "tuple_drift"
            with mock.patch.object(MODULE.CORE, "evaluate_method", drifting_method):
                with self.assertRaisesRegex(
                    MODULE.EvaluationError, "changed the shared 1 m tuple list"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "tuple_drift")

    def test_recorded_and_live_hash_drift_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            manifest = pair.manifest("U0")
            manifest["artifacts"]["tum"]["identity"]["sha256"] = "0" * 64
            pair.write_manifest("U0", manifest)
            output = root / "recorded_drift"
            with self.assertRaisesRegex(
                MODULE.EvaluationError, "changed after publication"
            ):
                MODULE.evaluate_pair(
                    pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "recorded_drift")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            original_runtime = MODULE.CORE.runtime_identity
            mutated = False

            def mutate_manifest_after_validation():
                nonlocal mutated
                if not mutated:
                    path = pair.runs["S1"] / "sequence_result.json"
                    path.write_bytes(path.read_bytes() + b" \n")
                    mutated = True
                return original_runtime()

            output = root / "live_drift"
            with mock.patch.object(
                MODULE.CORE, "runtime_identity", mutate_manifest_after_validation
            ):
                with self.assertRaisesRegex(
                    MODULE.EvaluationError, "sequence manifest live identity drift"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "live_drift")

    def test_ineligible_failures_are_never_scored(self) -> None:
        for status in ("NO_INIT", "PARTIAL", "CRASH", "NUMERIC_FAILURE"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pair = SyntheticPair(root)
                manifest = pair.manifest("S1")
                manifest["status"] = status
                manifest["accuracy_eligible"] = False
                pair.write_manifest("S1", manifest)
                output = root / "ineligible"
                with self.assertRaisesRegex(
                    MODULE.EvaluationError, "status is not accuracy eligible"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
                self.assertFalse(output.exists())
                self.assert_no_staging(root, "ineligible")

    def test_zero_u0_rmse_is_unassessable_even_when_both_are_zero(self) -> None:
        for candidate in (0.0, 1.0):
            with self.subTest(candidate=candidate):
                record = MODULE._relative_difference_record(0.0, candidate)
                self.assertEqual(record["status"], "UNASSESSABLE_ZERO_U0_DENOMINATOR")
                self.assertFalse(record["mathematical_ratio_defined"])
                self.assertIsNone(record["value"])
        defined = MODULE._relative_difference_record(2.0, 1.0)
        self.assertEqual(defined["status"], "DEFINED_FINITE")
        self.assertEqual(defined["value"], -0.5)

    def test_atomic_no_overwrite_and_failure_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            output = root / "existing"
            output.mkdir()
            sentinel = output / "sentinel"
            sentinel.write_bytes(b"do not replace\n")
            with self.assertRaisesRegex(MODULE.EvaluationError, "refusing to overwrite"):
                MODULE.evaluate_pair(
                    pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                )
            self.assertEqual(sentinel.read_bytes(), b"do not replace\n")
            self.assert_no_staging(root, "existing")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            output = root / "failed"
            with mock.patch.object(
                MODULE.CORE,
                "evaluate_method",
                side_effect=MODULE.CORE.PairEvaluationError("synthetic metric failure"),
            ):
                with self.assertRaisesRegex(
                    MODULE.CORE.PairEvaluationError, "synthetic metric failure"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "failed")

    def test_runtime_pin_is_exact_and_drift_fails_closed(self) -> None:
        self.assertEqual(
            MODULE.identity(MODULE.CORE_PATH)["sha256"],
            MODULE.EXPECTED_MATH_CORE_SHA256,
        )
        runtime = MODULE.CORE.runtime_identity()
        self.assertEqual(runtime["evo"]["version"], MODULE.EXPECTED_EVO_VERSION)
        self.assertEqual(
            MODULE.CORE.canonical_digest(MODULE.CORE.runtime_pin_projection(runtime)),
            MODULE.EXPECTED_RUNTIME_PINS_SHA256,
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair = SyntheticPair(root)
            drifted = copy.deepcopy(runtime)
            drifted["python"]["version_info"] = "3.8.synthetic-drift"
            output = root / "runtime_drift"
            with mock.patch.object(MODULE.CORE, "runtime_identity", return_value=drifted):
                with self.assertRaisesRegex(
                    MODULE.EvaluationError, "evaluator runtime pin drift"
                ):
                    MODULE.evaluate_pair(
                        pair.runs["U0"], pair.runs["S1"], pair.gt, pair.matrix, output
                    )
            self.assertFalse(output.exists())
            self.assert_no_staging(root, "runtime_drift")


if __name__ == "__main__":
    unittest.main()
