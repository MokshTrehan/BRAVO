#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic, no-registry/no-bag tests for the CP2-D authorized runner slice."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[1]
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_schema as schema  # noqa: E402
import cp2_sequence_math as sequence_math  # noqa: E402
import cp2_sequence_runner as runner  # noqa: E402
import run_sequence_pair as entrypoint  # noqa: E402


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class SyntheticEvidence:
    def __init__(self, temporary: str) -> None:
        self.root = Path(temporary)
        self.runtime_roots = {}
        common_positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.2),
        )
        ground_truth_positions = (
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.201),
        )
        timestamps = tuple((10 + index) * 1_000_000_000 for index in range(4))
        self.ground_truth = tuple(
            runner.GroundTruthPose(timestamp, position, (0.0, 0.0, 0.0, 1.0))
            for timestamp, position in zip(timestamps, ground_truth_positions)
        )
        alignment = sequence_math.baseline_kabsch_alignment(
            np.asarray(common_positions, dtype=np.float64),
            np.asarray(ground_truth_positions, dtype=np.float64),
        )
        aligned = sequence_math.apply_common_alignment(
            alignment,
            np.asarray(common_positions, dtype=np.float64),
            np.asarray([(0.0, 0.0, 0.0, 1.0)] * 4, dtype=np.float64),
            np.asarray(common_positions, dtype=np.float64),
            np.asarray([(0.0, 0.0, 0.0, 1.0)] * 4, dtype=np.float64),
        )
        self.ate = float(
            sequence_math.translation_rmse_m(
                aligned.nullspace_positions,
                np.asarray(ground_truth_positions, dtype=np.float64),
            )
        )

        pair_rows = []
        callback_rows = {mode: [] for mode in runner.MODES}
        trajectory_rows = {mode: [] for mode in runner.MODES}
        for index, (timestamp, position) in enumerate(zip(timestamps, common_positions)):
            pair = {
                "schema_version": 1,
                "record_type": "pair_index",
                "sequence_index": 0,
                "sequence_id": "MH_01_easy",
                "pair_index": index,
                "anchor_filtered_index": 2 * index,
                "anchor_camera_id": 0,
                "cam0_filtered_index": 2 * index,
                "cam1_filtered_index": 2 * index + 1,
                "cam0_record_time_ns": (index + 1) * 1_000_000_000,
                "cam1_record_time_ns": (index + 1) * 1_000_000_000 + 1_000_000,
                "cam0_header_time_ns": timestamp,
                "cam1_header_time_ns": timestamp + 1000,
                "absolute_record_delta_ns": 1_000_000,
            }
            pair_rows.append(pair)
            for mode in runner.MODES:
                callback_rows[mode].append(
                    {
                        "schema_version": 1,
                        "record_type": "serial_callback",
                        "sequence_index": 0,
                        "sequence_id": "MH_01_easy",
                        "mode": mode,
                        "callback_index": index,
                        "pair_index": index,
                        "anchor_filtered_index": pair["anchor_filtered_index"],
                        "cam0_filtered_index": pair["cam0_filtered_index"],
                        "cam1_filtered_index": pair["cam1_filtered_index"],
                        "cam0_record_time_ns": pair["cam0_record_time_ns"],
                        "cam1_record_time_ns": pair["cam1_record_time_ns"],
                        "cam0_header_time_ns": pair["cam0_header_time_ns"],
                        "cam1_header_time_ns": pair["cam1_header_time_ns"],
                        "camera_timestamp_ns": timestamp,
                        "enqueue_entered": True,
                        "enqueue_returned": True,
                        "enqueue_status": "queued",
                        "processing_entered": True,
                        "processing_returned": True,
                        "processing_status": "processed",
                        "state_row_emitted": True,
                        "trajectory_index": index,
                    }
                )
                trajectory_rows[mode].append(
                    {
                        "schema_version": 1,
                        "record_type": "trajectory_pose",
                        "sequence_index": 0,
                        "sequence_id": "MH_01_easy",
                        "mode": mode,
                        "trajectory_index": index,
                        "callback_index": index,
                        "pair_index": index,
                        "camera_timestamp_ns": timestamp,
                        "position_G": list(position),
                        "quaternion_ItoG_xyzw": [0.0, 0.0, 0.0, 1.0],
                    }
                )
        self.pair_bytes = schema.jsonl_bytes(pair_rows)

        evaluator_logs = {}
        command_rows = []
        command_environment_sha = schema.command_environment_sha256({"LANG": "C"})
        for index, mode in enumerate(runner.MODES):
            stdout_path = "evaluation/{}_evo.stdout".format(mode)
            stderr_path = "evaluation/{}_evo.stderr".format(mode)
            stdout = ("rmse {:.17g}\n".format(self.ate)).encode("ascii")
            stderr = b""
            evaluator_logs[stdout_path] = runner.SupportFile(stdout, "evaluator")
            evaluator_logs[stderr_path] = runner.SupportFile(stderr, "evaluator")
            command_rows.append(
                {
                    "schema_version": 1,
                    "record_type": "command",
                    "command_id": index,
                    "phase": "evaluation",
                    "sequence_index": 0,
                    "pair_index": None,
                    "run_index": index,
                    "argv": [
                        "evo_ape",
                        "tum",
                        "ground_truth_shared.tum",
                        mode + "_shared_aligned.tum",
                        "-r",
                        "trans_part",
                        "--t_max_diff",
                        "0.01",
                    ],
                    "cwd": "/tmp/synthetic-cp2-d",
                    "environment_sha256": command_environment_sha,
                    "started_utc": "2026-08-02T00:00:00.000000Z",
                    "finished_utc": "2026-08-02T00:00:01.000000Z",
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout": stdout_path,
                    "stdout_sha256": digest(stdout),
                    "stderr": stderr_path,
                    "stderr_sha256": digest(stderr),
                }
            )
        self.support = dict(evaluator_logs)
        self.support.update(
            {
                "source/source.tar": runner.SupportFile(b"synthetic source archive", "source"),
                "readiness/barrier.json": runner.SupportFile(b"{}\n", "readiness"),
            }
        )
        self.commands = schema.jsonl_bytes(command_rows)

        modes = []
        for index, mode in enumerate(runner.MODES):
            runtime_root = self.root / "runs" / mode
            runtime_root.mkdir(parents=True)
            trace_directory = runtime_root / "trace"
            trace_directory.mkdir()
            paths = {
                "/cp2_vio/filepath_est": runtime_root / "state.txt",
                "/cp2_vio/filepath_std": runtime_root / "deviation.txt",
                "/cp2_vio/record_timing_filepath": runtime_root / "timing.csv",
                "/cp2_vio/cp2_trace_directory": trace_directory,
                "/cp2_vio/cp2_context_path": runtime_root / "context.json",
            }
            for key, path in paths.items():
                if key != "/cp2_vio/cp2_trace_directory":
                    path.write_bytes(b"synthetic sink")
            parameters = {
                "/cp2_vio/common_integer": 7,
                "/cp2_vio/up_msckf_landmark_elimination": mode,
                **{key: str(path) for key, path in paths.items()},
            }
            stdout_path = "evaluation/{}_evo.stdout".format(mode)
            stderr_path = "evaluation/{}_evo.stderr".format(mode)
            modes.append(
                runner.ModeRunInput(
                    mode=mode,
                    pair_index_bytes=self.pair_bytes,
                    callback_bytes=schema.jsonl_bytes(callback_rows[mode]),
                    trajectory_bytes=schema.jsonl_bytes(trajectory_rows[mode]),
                    prelaunch_raw_bytes=b"synthetic: prelaunch\n",
                    runtime_raw_bytes=b"synthetic: runtime\n",
                    prelaunch_parameters=dict(parameters),
                    runtime_parameters=dict(parameters),
                    runtime_root=runtime_root,
                    executable_sha256="a" * 64,
                    loader_map_sha256="b" * 64,
                    legacy_state_bytes=b"state\n",
                    legacy_deviation_bytes=b"deviation\n",
                    legacy_timing_bytes=b"timing\n",
                    evaluator_stdout_path=stdout_path,
                    evaluator_stdout_bytes=evaluator_logs[stdout_path].payload,
                    evaluator_stderr_path=stderr_path,
                    evaluator_stderr_bytes=evaluator_logs[stderr_path].payload,
                    evaluator_ate_m=self.ate,
                )
            )
        self.modes = tuple(modes)
        additional_support = {
            "build/compile_commands.json": b"[]\n",
            "build/CMakeCache.txt": b"SYNTHETIC=1\n",
            "config/euroc_mav/estimator_config.yaml": (
                REPOSITORY_ROOT / "config/euroc_mav/estimator_config.yaml"
            ).read_bytes(),
            "config/euroc_mav/kalibr_imu_chain.yaml": (
                REPOSITORY_ROOT / "config/euroc_mav/kalibr_imu_chain.yaml"
            ).read_bytes(),
            "config/euroc_mav/kalibr_imucam_chain.yaml": (
                REPOSITORY_ROOT / "config/euroc_mav/kalibr_imucam_chain.yaml"
            ).read_bytes(),
            "configuration/static-bundle.bin": b"synthetic static bundle",
            "project/cp2_serial.launch": (
                REPOSITORY_ROOT / "project/cp2_serial.launch"
            ).read_bytes(),
        }
        for index, mode in enumerate(runner.MODES):
            additional_support.update(
                {
                    "runtime/{}_loader_before.txt".format(mode): b"loader map " + mode.encode("ascii"),
                    "runtime/{}_loader_after.txt".format(mode): b"loader map " + mode.encode("ascii") + b" after",
                    "configuration/{}_context.json".format(mode): b"{}\n",
                }
            )
        for path, payload in additional_support.items():
            role = "build" if path.startswith("build/") else (
                "log" if path.startswith("runtime/") else "configuration"
            )
            self.support[path] = runner.SupportFile(payload, role)

    def provenance(self) -> dict:
        static_records = []
        for path in (
            "config/euroc_mav/estimator_config.yaml",
            "config/euroc_mav/kalibr_imu_chain.yaml",
            "config/euroc_mav/kalibr_imucam_chain.yaml",
        ):
            payload = self.support[path].payload
            static_records.append({"path": path, "size": len(payload), "sha256": digest(payload)})
        launch_payload = self.support["project/cp2_serial.launch"].payload
        launch_record = {
            "path": "project/cp2_serial.launch",
            "size": len(launch_payload),
            "sha256": digest(launch_payload),
        }
        bundle_payload = runner._static_bundle_bytes(static_records + [launch_record])
        self.support["configuration/static-bundle.bin"] = runner.SupportFile(
            bundle_payload, "configuration"
        )
        runtime_runs = []
        resolved = []
        contexts = []
        for index, mode in enumerate(runner.MODES):
            before = "runtime/{}_loader_before.txt".format(mode)
            after = "runtime/{}_loader_after.txt".format(mode)
            runtime_runs.append(
                {
                    "run_id": "synthetic-" + mode,
                    "sequence_index": 0,
                    "mode": mode,
                    "loader_map_before": before,
                    "loader_map_before_sha256": digest(self.support[before].payload),
                    "loader_map_after": after,
                    "loader_map_after_sha256": digest(self.support[after].payload),
                    "dso_records_before": [],
                    "dso_records_after": [],
                }
            )
            canonical = schema.encode_resolved_parameters(self.modes[index].runtime_parameters)
            normalized_map = dict(self.modes[index].runtime_parameters)
            for key in runner.NORMALIZED_KEYS:
                normalized_map.pop(key)
            normalized = schema.encode_resolved_parameters(normalized_map)
            resolved.append(
                {
                    "run_id": "synthetic-" + mode,
                    "prelaunch_raw_path": "parameters/{}_prelaunch_raw.yaml".format(mode),
                    "prelaunch_raw_sha256": digest(self.modes[index].prelaunch_raw_bytes),
                    "runtime_raw_path": "parameters/{}_runtime_raw.yaml".format(mode),
                    "runtime_raw_sha256": digest(self.modes[index].runtime_raw_bytes),
                    "canonical_path": "parameters/{}_canonical.bin".format(mode),
                    "canonical_sha256": digest(canonical),
                    "normalized_path": "parameters/{}_normalized.bin".format(mode),
                    "normalized_sha256": digest(normalized),
                }
            )
            context_path = "configuration/{}_context.json".format(mode)
            contexts.append(
                {
                    "run_id": "synthetic-" + mode,
                    "path": context_path,
                    "size": len(self.support[context_path].payload),
                    "sha256": digest(self.support[context_path].payload),
                }
            )
        environment_variables = {"LANG": "C"}
        environment_sha = schema.command_environment_sha256(environment_variables)
        return {
            "schema_version": 1,
            "record_type": "provenance",
            "checkpoint": "CP2-D",
            "evidence_class": "trusted_runner_local_staging_evidence",
            "distribution_status": "internal_non_conveyable_staging",
            "eligible_for_cp2_seal": False,
            "created_utc": "2026-08-02T00:00:00.000000Z",
            "branch": "schurvio-lite/cp2-one-pass",
            "source_commit": "c" * 40,
            "source_tree": "d" * 40,
            "source_archive": "source/source.tar",
            "source_archive_sha256": digest(b"synthetic source archive"),
            "clean": True,
            "cp1_authorization_commit": runner.CP1_AUTHORIZATION_COMMIT,
            "contracts": [{"path": "docs/cp2_artifact_schema.md", "sha256": "2" * 64}],
            "entrypoints": [
                {
                    "path": path,
                    "sha256": str(index + 3) * 64,
                    "git_blob": str(index + 3) * 40,
                }
                for index, path in enumerate(
                    (
                        "scripts/cp2/run_unit_gate.sh",
                        "scripts/cp2/run_recorded_parity.py",
                        "scripts/cp2/run_sequence_pair.py",
                        "scripts/cp2/run_timing_pair.py",
                        "scripts/cp2/verify_report.py",
                    )
                )
            ],
            "readiness_barrier": "readiness/barrier.json",
            "unit_anchor": {
                "artifact": "/tmp/synthetic-unit",
                "manifest_sha256": "8" * 64,
                "tested_commit": "c" * 40,
                "tested_tree": "d" * 40,
                "report_sha256": "b" * 64,
                "verified": True,
            },
            "build": {
                "fresh_git_archive": True,
                "workspace": "/tmp/synthetic-workspace",
                "commands_sha256": digest(self.commands),
                "compile_commands": "build/compile_commands.json",
                "compile_commands_sha256": digest(self.support["build/compile_commands.json"].payload),
                "cmake_cache": "build/CMakeCache.txt",
                "cmake_cache_sha256": digest(self.support["build/CMakeCache.txt"].payload),
                "strict_fp_verified": True,
            },
            "readiness_barrier_sha256": digest(b"{}\n"),
            "runtime": {
                "executable": "/tmp/synthetic-workspace/devel/lib/ov_msckf/ros1_serial_msckf",
                "executable_size": 1,
                "executable_sha256_before": "a" * 64,
                "executable_sha256_after": "a" * 64,
                "build_id_before": "synthetic-build-id",
                "build_id_after": "synthetic-build-id",
                "runs": runtime_runs,
            },
            "configuration": {
                "static_files": static_records,
                "static_bundle_payload": "configuration/static-bundle.bin",
                "static_bundle_sha256": digest(self.support["configuration/static-bundle.bin"].payload),
                "launch": launch_record,
                "resolved_parameters": resolved,
                "runtime_contexts": contexts,
            },
            "inputs": [
                {
                    "sequence_index": 0,
                    "sequence_id": "MH_01_easy",
                    "offset_seconds": 40.0,
                    "bag_path": "/tmp/synthetic.bag",
                    "bag_size": 1,
                    "bag_sha256_before": runner.FROZEN_BAG_SHA256[0],
                    "bag_sha256_after": runner.FROZEN_BAG_SHA256[0],
                    "ground_truth_path": "/tmp/synthetic-ground-truth.csv",
                    "ground_truth_sha256": runner.FROZEN_GROUND_TRUTH_SHA256[0],
                }
            ],
            "environment": {
                "classes": [
                    {
                        "environment_id": "synthetic",
                        "variables": [{"name": "LANG", "value": "C"}],
                        "canonical_sha256": environment_sha,
                    }
                ]
            },
            "host": {
                "hostname": None,
                "os_release": None,
                "kernel_release": None,
                "architecture": None,
                "cpu_model": None,
                "logical_cpu_count": None,
                "ros_distribution": None,
                "compiler_version": None,
                "cmake_version": None,
                "catkin_version": None,
                "eigen_version": None,
                "opencv_version": None,
                "boost_version": None,
                "ceres_version": None,
                "python_version": None,
                "evo_version": None,
            },
        }

    def evidence(self) -> runner.SequenceAssemblyInput:
        return runner.SequenceAssemblyInput(
            sequence_index=0,
            sequence_id="MH_01_easy",
            offset_seconds=40.0,
            run_id="synthetic-run",
            modes=self.modes,
            ground_truth=self.ground_truth,
            provenance_without_inventory=self.provenance(),
            commands_bytes=self.commands,
            support_files=self.support,
        )


class SequenceAssemblyTests(unittest.TestCase):
    def test_valid_fixture_assembles_exact_math_and_manifest(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-assembly-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            partial = Path(temporary) / "partial"
            partial.mkdir()
            result = runner.assemble_sequence_artifact(partial, fixture.evidence())
            self.assertTrue(result.report["passed"])
            self.assertEqual(result.report["sequence_index"], 0)
            self.assertEqual([item["mode"] for item in result.report["runs"]], list(runner.MODES))
            self.assertEqual(len(result.report["normalized_parameter_diff"]), 6)
            self.assertEqual(result.report["shared_timestamp_count"], 4)
            self.assertGreater(result.report["ate_nullspace_m"], 0.0)
            self.assertEqual(result.report["relative_ate_difference"], 0.0)
            # Coverage is frozen in rosbag record time (1..4 seconds), while
            # estimator/trajectory timestamps are cam0 headers (10..13).
            for run in result.report["runs"]:
                self.assertEqual(run["first_selected_timestamp_ns"], 1_000_000_000)
                self.assertEqual(run["last_selected_timestamp_ns"], 4_000_000_000)
                self.assertEqual(run["first_processed_timestamp_ns"], 1_000_000_000)
                self.assertEqual(run["last_processed_timestamp_ns"], 4_000_000_000)
                self.assertEqual(run["selected_duration_ns"], 3_000_000_000)
                self.assertEqual(run["processed_duration_ns"], 3_000_000_000)
            manifest = (partial / "SHA256SUMS").read_bytes()
            self.assertEqual(digest(manifest), result.manifest_sha256)
            entries = schema.parse_manifest_bytes(manifest)
            self.assertIn("cp2_report.json", entries)
            self.assertIn("provenance.json", entries)
            self.assertIn("shared_population.bin", entries)
            self.assertNotIn("SHA256SUMS", entries)
            self.assertEqual(stat.S_IMODE((partial / "pair_index.jsonl").stat().st_mode), 0o444)

    def test_prepopulated_support_is_exactly_bound_before_assembly(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-prepopulated-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            evidence = fixture.evidence()
            partial = Path(temporary) / "partial"
            partial.mkdir()
            for relative, support in evidence.support_files.items():
                destination = partial / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(support.payload)
            result = runner.assemble_sequence_artifact(
                partial, evidence, prepopulated_support=True
            )
            self.assertTrue(result.report["passed"])
            self.assertEqual(stat.S_IMODE((partial / "source/source.tar").stat().st_mode), 0o444)

            fixture = SyntheticEvidence(str(Path(temporary) / "second-fixture"))
            evidence = fixture.evidence()
            invalid = Path(temporary) / "invalid-partial"
            invalid.mkdir()
            for relative, support in evidence.support_files.items():
                destination = invalid / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(support.payload)
            (invalid / "orphan").write_bytes(b"orphan")
            with self.assertRaisesRegex(runner.SequenceRunnerError, "file set differs"):
                runner.assemble_sequence_artifact(
                    invalid,
                    evidence,
                    prepopulated_support=True,
                    after_validation=lambda: None,
                )

    def test_pair_identity_drift_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-pair-drift-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            changed = replace(fixture.modes[1], pair_index_bytes=fixture.pair_bytes + b"\n")
            evidence = replace(fixture.evidence(), modes=(fixture.modes[0], changed))
            partial = Path(temporary) / "partial"
            partial.mkdir()
            with self.assertRaises(runner.SequenceRunnerError):
                runner.assemble_sequence_artifact(partial, evidence)

    def test_pair_candidate_must_be_forward_and_callbacks_must_be_complete(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-pair-forward-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            pair_rows = list(schema.strict_jsonl_loads(fixture.pair_bytes))
            pair_rows[0]["anchor_camera_id"] = 1
            pair_rows[0]["anchor_filtered_index"] = pair_rows[0]["cam1_filtered_index"]
            invalid_pairs = schema.jsonl_bytes(pair_rows)
            changed_modes = tuple(
                replace(mode, pair_index_bytes=invalid_pairs) for mode in fixture.modes
            )
            partial = Path(temporary) / "invalid-forward"
            partial.mkdir()
            with self.assertRaisesRegex(runner.SequenceRunnerError, "not forward"):
                runner.assemble_sequence_artifact(
                    partial, replace(fixture.evidence(), modes=changed_modes)
                )

            callbacks = list(schema.strict_jsonl_loads(fixture.modes[0].callback_bytes))
            incomplete = replace(
                fixture.modes[0], callback_bytes=schema.jsonl_bytes(callbacks[:-1])
            )
            partial = Path(temporary) / "incomplete-callbacks"
            partial.mkdir()
            with self.assertRaisesRegex(runner.SequenceRunnerError, "one-to-one"):
                runner.assemble_sequence_artifact(
                    partial,
                    replace(fixture.evidence(), modes=(incomplete, fixture.modes[1])),
                )

            callbacks = list(
                schema.strict_jsonl_loads(fixture.modes[0].callback_bytes)
            )
            callbacks[0], callbacks[1] = callbacks[1], callbacks[0]
            for callback_index, callback in enumerate(callbacks):
                callback["callback_index"] = callback_index
            permuted = replace(
                fixture.modes[0],
                callback_bytes=schema.jsonl_bytes(callbacks),
            )
            partial = Path(temporary) / "permuted-callbacks"
            partial.mkdir()
            with self.assertRaisesRegex(
                runner.SequenceRunnerError, "callback/pair order differs"
            ):
                runner.assemble_sequence_artifact(
                    partial,
                    replace(
                        fixture.evidence(),
                        modes=(permuted, fixture.modes[1]),
                    ),
                )

    def test_nonallowlisted_parameter_drift_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-param-drift-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            parameters = dict(fixture.modes[1].runtime_parameters)
            parameters["/cp2_vio/common_integer"] = 8
            changed = replace(
                fixture.modes[1],
                prelaunch_parameters=parameters,
                runtime_parameters=parameters,
            )
            evidence = replace(fixture.evidence(), modes=(fixture.modes[0], changed))
            partial = Path(temporary) / "partial"
            partial.mkdir()
            with self.assertRaises(runner.SequenceRunnerError):
                runner.assemble_sequence_artifact(partial, evidence)

    def test_evaluator_value_cannot_be_detached_from_retained_stdout(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-evaluator-drift-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            changed = replace(fixture.modes[1], evaluator_ate_m=fixture.ate + 1.0)
            evidence = replace(fixture.evidence(), modes=(fixture.modes[0], changed))
            partial = Path(temporary) / "partial"
            partial.mkdir()
            with self.assertRaises(runner.SequenceRunnerError):
                runner.assemble_sequence_artifact(partial, evidence)

    def test_failed_command_and_nonfrozen_input_hash_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-command-input-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            commands = list(schema.strict_jsonl_loads(fixture.commands))
            commands[0]["exit_code"] = 1
            partial = Path(temporary) / "failed-command"
            partial.mkdir()
            with self.assertRaisesRegex(runner.SequenceRunnerError, "failed command"):
                runner.assemble_sequence_artifact(
                    partial,
                    replace(fixture.evidence(), commands_bytes=schema.jsonl_bytes(commands)),
                )

            provenance = fixture.provenance()
            provenance["inputs"][0]["bag_sha256_before"] = "0" * 64
            provenance["inputs"][0]["bag_sha256_after"] = "0" * 64
            partial = Path(temporary) / "wrong-input-hash"
            partial.mkdir()
            with self.assertRaisesRegex(runner.SequenceRunnerError, "frozen sequence"):
                runner.assemble_sequence_artifact(
                    partial,
                    replace(fixture.evidence(), provenance_without_inventory=provenance),
                )

    def test_read_only_detached_verify_and_noreplace_seal(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-seal-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            staging = Path(temporary) / "staging"
            observed = []

            def builder(partial):
                return runner.assemble_sequence_artifact(partial, fixture.evidence())

            def verifier(partial, manifest_sha256):
                observed.append((partial, manifest_sha256))
                self.assertEqual(stat.S_IMODE(partial.stat().st_mode), 0o555)
                self.assertEqual(stat.S_IMODE((partial / "cp2_report.json").stat().st_mode), 0o444)
                return {"passed": True}

            final, result = runner.build_verify_seal(staging, "synthetic-run", builder, verifier)
            self.assertEqual(final, staging / "synthetic-run")
            self.assertTrue(final.is_dir())
            self.assertEqual(observed[0][1], result.manifest_sha256)
            with self.assertRaises(runner.SequenceRunnerError):
                runner.build_verify_seal(staging, "synthetic-run", builder, verifier)

    def test_verifier_mutation_cannot_be_sealed_and_partial_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="cp2-d-verify-mutation-", dir="/tmp") as temporary:
            fixture = SyntheticEvidence(temporary)
            staging = Path(temporary) / "staging"

            def builder(partial):
                return runner.assemble_sequence_artifact(partial, fixture.evidence())

            def mutating_verifier(partial, manifest_sha256):
                del manifest_sha256
                report = partial / "cp2_report.json"
                report.chmod(0o600)
                report.write_bytes(report.read_bytes() + b" ")
                return {"passed": True}

            with self.assertRaises(runner.SequenceRunnerError):
                runner.build_verify_seal(staging, "synthetic-run", builder, mutating_verifier)
            self.assertFalse((staging / "synthetic-run").exists())
            self.assertEqual(list(staging.glob(".synthetic-run.partial.*")), [])

    def test_postrename_directory_fsync_failure_rolls_back_publication(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-d-seal-fsync-", dir="/tmp"
        ) as temporary:
            fixture = SyntheticEvidence(temporary)
            staging = Path(temporary) / "staging"

            def builder(partial):
                return runner.assemble_sequence_artifact(
                    partial, fixture.evidence()
                )

            def verifier(partial, manifest_sha256):
                del partial, manifest_sha256
                return {"passed": True}

            real_fsync_directory = runner._fsync_directory
            call_count = 0

            def fail_postrename(path):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise OSError("injected post-rename directory fsync failure")
                return real_fsync_directory(path)

            with mock.patch.object(
                runner,
                "_fsync_directory",
                side_effect=fail_postrename,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected post-rename directory fsync failure"
                ):
                    runner.build_verify_seal(
                        staging, "synthetic-run", builder, verifier
                    )
            self.assertGreaterEqual(call_count, 3)
            self.assertFalse((staging / "synthetic-run").exists())
            self.assertEqual(
                list(staging.glob(".synthetic-run.partial.*")), []
            )


class BootstrapOrderingTests(unittest.TestCase):
    class Authorization:
        def __init__(self, events):
            self.events = events
            self.prebag_authorized = True
            self.read_count = 0

        def read_registry_once(self):
            self.events.append("registry")
            self.read_count += 1
            if self.read_count != 1:
                raise RuntimeError("second registry read")
            return b"synthetic registry buffer"

        def revalidate(self):
            self.events.append("authorization-revalidate")

        def close(self):
            self.events.append("authorization-close")

    class Handle:
        def __init__(self, events, fail=False):
            self.events = events
            self.authorization = BootstrapOrderingTests.Authorization(events)
            authorization = self.authorization

            class Module:
                Path = Path
                EXPECTED_CASE_NAMES_BY_ENTRYPOINT = {"synthetic": ("case",)}
                CP1_AUTHORIZATION_COMMIT = "f" * 40

                @staticmethod
                def run_readiness_barrier(*args):
                    del args
                    events.append("barrier")
                    if fail:
                        raise RuntimeError("synthetic barrier failure")
                    return authorization

            self.module = Module

        def revalidate(self):
            self.events.append("handle-revalidate")

        def close(self):
            self.events.append("handle-close")

    def arguments(self):
        return (
            "--unit-artifact",
            "/tmp/synthetic-unit",
            "--unit-manifest-sha256",
            "0" * 64,
            "--sequence",
            "MH_01_easy",
        )

    def test_barrier_precedes_single_registry_buffer_and_executor(self):
        events = []
        handle = self.Handle(events)

        def loader(repo_root):
            self.assertTrue(Path(repo_root).is_absolute())
            events.append("loader")
            return handle

        def executor(repo_root, parsed, authorization, registry_bytes):
            del repo_root, parsed
            events.append("executor")
            self.assertIs(authorization, handle.authorization)
            self.assertEqual(registry_bytes, b"synthetic registry buffer")
            return {"artifact": "/tmp/synthetic-artifact", "manifest_sha256": "1" * 64}

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status_value = entrypoint._actual(
                self.arguments(), readiness_loader=loader,
                postauthorization_executor=executor,
                _allow_unapproved_synthetic_test=True,
            )
        self.assertEqual(status_value, 0)
        self.assertIn('"passed":true', stdout.getvalue())
        self.assertEqual(handle.authorization.read_count, 1)
        self.assertLess(events.index("barrier"), events.index("registry"))
        self.assertLess(events.index("registry"), events.index("executor"))
        self.assertLess(events.index("executor"), events.index("authorization-close"))
        self.assertLess(events.index("authorization-close"), events.index("handle-close"))

    def test_failed_barrier_never_reads_registry(self):
        events = []
        handle = self.Handle(events, fail=True)
        with contextlib.redirect_stderr(io.StringIO()):
            status_value = entrypoint._actual(
                self.arguments(),
                readiness_loader=lambda repo_root: handle,
                postauthorization_executor=lambda *args: self.fail("executor reached"),
                _allow_unapproved_synthetic_test=True,
            )
        self.assertEqual(status_value, 1)
        self.assertNotIn("registry", events)
        self.assertNotIn("authorization-close", events)
        self.assertIn("handle-close", events)

    def test_authorization_cleanup_failure_cannot_report_pass(self):
        events = []
        handle = self.Handle(events)

        def failed_close():
            events.append("authorization-close-failed")
            raise RuntimeError("synthetic close failure")

        handle.authorization.close = failed_close
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status_value = entrypoint._actual(
                self.arguments(),
                readiness_loader=lambda repo_root: handle,
                postauthorization_executor=lambda *args: {
                    "artifact": "/tmp/synthetic-artifact",
                    "manifest_sha256": "1" * 64,
                },
                _allow_unapproved_synthetic_test=True,
            )
        self.assertEqual(status_value, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("synthetic close failure", stderr.getvalue())
        self.assertIn("handle-close", events)

    def test_unapproved_actual_mode_stops_before_readiness_loader(self):
        reached = []
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status_value = entrypoint._actual(
                self.arguments(),
                readiness_loader=lambda repo_root: reached.append(repo_root),
            )
        self.assertEqual(status_value, 1)
        self.assertEqual(reached, [])
        self.assertIn("blocked before readiness/data access", stderr.getvalue())

    def test_descriptor_loader_matches_embedded_readiness_anchor(self):
        root = Path(__file__).resolve().parents[3]
        expected = hashlib.sha256((root / entrypoint.READINESS_RELATIVE_PATH).read_bytes()).hexdigest()
        if expected != entrypoint.READINESS_SHA256:
            with self.assertRaises(entrypoint.Rejection):
                entrypoint._VerifiedReadinessModule(str(root))
            return
        handle = entrypoint._VerifiedReadinessModule(str(root))
        try:
            handle.revalidate()
            self.assertTrue(callable(handle.module.run_readiness_barrier))
        finally:
            handle.close()


if __name__ == "__main__":
    unittest.main()
