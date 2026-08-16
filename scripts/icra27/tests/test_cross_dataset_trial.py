#!/usr/bin/python3.8
"""Synthetic contract tests for the append-only cross-dataset runner."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

import yaml


MODULE_PATH = Path(__file__).resolve().parents[1] / "cross_dataset_trial.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_trial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
TRIAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRIAL
SPEC.loader.exec_module(TRIAL)


def _table(path: Path, timestamps) -> None:
    lines = ["# fixture\n"]
    for timestamp in timestamps:
        lines.append("{} 0 0 0 0 0 0 1\n".format(timestamp))
    path.write_text("".join(lines), encoding="utf-8")


def _identity_artifact(path: Path, relative: str) -> dict:
    return {"relative_path": relative, "identity": TRIAL.file_identity(path)}


def _complete_facts(**changes) -> dict:
    value = {
        "mode": "scored",
        "interrupted": False,
        "timed_out": False,
        "teardown_ok": True,
        "runtime_contract_valid": True,
        "numeric_integrity_valid": True,
        "state_kind": "valid",
        "outputs_valid": True,
        "continuity_pass": True,
        "tail_pass": True,
        "launch_abnormal": False,
        "exact_u0_teardown": False,
        "capture_closed": True,
        "linkage_valid": True,
    }
    value.update(changes)
    return value


class ArtifactPublicationTests(unittest.TestCase):
    def test_append_only_publication_and_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = TRIAL.create_run_directory(root, "cdsc1-mh01-u0-scored")
            TRIAL._atomic_write_new_bytes(run / "diagnostics" / "a.txt", b"a\n")
            TRIAL.atomic_write_new_json(run / "sequence_result.json", {"schema": TRIAL.SCHEMA})
            sums = TRIAL.write_sha256sums(run)
            self.assertIn("sequence_result.json", sums)
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.atomic_write_new_json(run / "sequence_result.json", {})
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.create_run_directory(root, "cdsc1-mh01-u0-scored")

    def test_infrastructure_failure_still_publishes_stable_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                protocol_id="CDSC-1",
                protocol_file=TRIAL.CANONICAL_PROTOCOL,
                matrix_file=TRIAL.CANONICAL_MATRIX,
                run_id="fixture-infra-failure",
                attempt_index=1,
                dataset="euroc_mav",
                sequence="MH_01_easy",
                system="U0",
                mode="scored",
                bag=root / "missing.bag",
                bag_start=40.0,
                bag_duration=-1.0,
                config=TRIAL.U0_SOURCE_ROOT / "config/euroc_mav/estimator_config.yaml",
                launch=TRIAL.CANONICAL_LAUNCHES["U0"][0],
                binary=Path(
                    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                    "build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
                ),
                output_root=root,
                ros_port=45991,
                timeout_seconds=30.0,
                cpu_list="8-15",
                scored_result=None,
            )
            result, run = TRIAL.run_trial(args)
            self.assertEqual(result["schema"], TRIAL.SCHEMA)
            self.assertEqual(result["status"], "INFRASTRUCTURE_FAILED")
            self.assertIsNone(result["inputs"]["ground_truth"])
            self.assertEqual(
                result["artifacts"]["state"]["relative_path"],
                "trajectory/state_estimate.txt",
            )
            self.assertEqual(
                result["artifacts"]["deviation"]["relative_path"],
                "trajectory/state_deviation.txt",
            )
            self.assertEqual(
                result["artifacts"]["tum"]["relative_path"],
                "trajectory/estimate_raw.tum",
            )
            self.assertFalse(result["estimator_close_receipt"]["estimator_attempted"])
            self.assertTrue((run / "sequence_result.json").is_file())
            self.assertTrue((run / "SHA256SUMS").is_file())


class LaunchAndConfigContractTests(unittest.TestCase):
    def test_launches_have_exact_system_deltas(self) -> None:
        u0 = TRIAL.validate_launch_contract("U0", TRIAL.CANONICAL_LAUNCHES["U0"][0])
        s1 = TRIAL.validate_launch_contract("S1", TRIAL.CANONICAL_LAUNCHES["S1"][0])
        self.assertTrue(u0["u0_native_only_bindings"])
        self.assertEqual(
            s1["s1_only_algorithm_deltas"],
            [
                "up_msckf_landmark_elimination=schur",
                "up_msckf_max_visual_passes=1",
            ],
        )
        self.assertTrue(s1["recovery_parameter_absent"])

    def test_native_configs_are_default_off_and_topic_exact(self) -> None:
        for dataset in TRIAL.DATASETS:
            for system in TRIAL.SYSTEMS:
                root = TRIAL.U0_SOURCE_ROOT if system == "U0" else TRIAL.REPO_ROOT
                config = root / "config" / dataset / "estimator_config.yaml"
                value = TRIAL.validate_config_contract(system, config)
                self.assertFalse(value["recovery_enabled"])
                self.assertTrue(value["native_stereo"])

    def test_recovery_boolean_is_lexically_strict(self) -> None:
        self.assertFalse(TRIAL._strict_boolean_key("x: 1\n", "long_gap_recovery_enabled", False))
        self.assertFalse(
            TRIAL._strict_boolean_key(
                "long_gap_recovery_enabled: false\n", "long_gap_recovery_enabled", True
            )
        )
        with self.assertRaises(TRIAL.TrialError):
            TRIAL._strict_boolean_key(
                "long_gap_recovery_enabled: no\n", "long_gap_recovery_enabled", False
            )

    def test_canonical_paths_reject_byte_identical_copy(self) -> None:
        dataset = "euroc_mav"
        config = TRIAL.REPO_ROOT / "config" / dataset / "estimator_config.yaml"
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary) / "estimator_config.yaml"
            shutil.copyfile(config, copied)
            with self.assertRaisesRegex(TRIAL.TrialError, "canonical"):
                TRIAL.validate_canonical_paths_and_hashes(
                    dataset,
                    "S1",
                    copied.resolve(),
                    TRIAL.CANONICAL_LAUNCHES["S1"][0].resolve(),
                    TRIAL.CANONICAL_PROTOCOL.resolve(),
                    TRIAL.CANONICAL_MATRIX.resolve(),
                )

    def test_canonical_paths_and_hashes_pass_live_files(self) -> None:
        for dataset in TRIAL.DATASETS:
            for system in TRIAL.SYSTEMS:
                root = TRIAL.U0_SOURCE_ROOT if system == "U0" else TRIAL.REPO_ROOT
                value = TRIAL.validate_canonical_paths_and_hashes(
                    dataset,
                    system,
                    (root / "config" / dataset / "estimator_config.yaml").resolve(),
                    TRIAL.CANONICAL_LAUNCHES[system][0].resolve(),
                    TRIAL.CANONICAL_PROTOCOL.resolve(),
                    TRIAL.CANONICAL_MATRIX.resolve(),
                )
                self.assertTrue(value["all_canonical_paths_and_frozen_hashes_match"])


class CampaignAndParameterTests(unittest.TestCase):
    def test_matrix_binding_selects_exact_start_aware_row(self) -> None:
        bag = Path("/home/moksh/Downloads/machine_hall/MH_01_easy/MH_01_easy.bag").resolve()
        value = TRIAL.validate_campaign_bindings(
            "CDSC-1",
            TRIAL.CANONICAL_PROTOCOL.resolve(),
            TRIAL.CANONICAL_MATRIX.resolve(),
            "euroc_mav",
            "MH_01_easy",
            "U0",
            bag,
            40.0,
        )
        self.assertEqual(value["row_order"], 1)
        self.assertEqual(value["expected_bag"]["path"], str(bag))
        with self.assertRaisesRegex(TRIAL.TrialError, "bag start"):
            TRIAL.validate_campaign_bindings(
                "CDSC-1",
                TRIAL.CANONICAL_PROTOCOL.resolve(),
                TRIAL.CANONICAL_MATRIX.resolve(),
                "euroc_mav",
                "MH_01_easy",
                "U0",
                bag,
                0.0,
            )

    def test_resolved_parameter_contract_is_exact_per_system(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary)
            (run / "trajectory").mkdir()
            (run / "diagnostics").mkdir()
            config = TRIAL.REPO_ROOT / "config/euroc_mav/estimator_config.yaml"
            bag = Path("/tmp/fixture.bag")
            namespace = TRIAL.NODE_NAMESPACE + "/"
            common = {
                namespace + "config_path": str(config.resolve()),
                namespace + "path_bag": str(bag.resolve()),
                namespace + "bag_start": 40.0,
                namespace + "bag_durr": -1.0,
                namespace + "save_total_state": True,
                namespace + "filepath_est": str(run / "trajectory/state_estimate.txt"),
                namespace + "filepath_std": str(run / "trajectory/state_deviation.txt"),
                namespace + "record_timing_information": True,
                namespace + "record_timing_filepath": str(run / "diagnostics/timing_openvins.csv"),
            }
            TRIAL.validate_resolved_parameters(
                "U0", yaml.safe_dump(common), config.resolve(), bag.resolve(), 40.0, -1.0, run
            )
            s1 = dict(common)
            s1[namespace + "up_msckf_landmark_elimination"] = "schur"
            s1[namespace + "up_msckf_max_visual_passes"] = 1
            TRIAL.validate_resolved_parameters(
                "S1", yaml.safe_dump(s1), config.resolve(), bag.resolve(), 40.0, -1.0, run
            )
            s1[namespace + "long_gap_recovery_enabled"] = False
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.validate_resolved_parameters(
                    "S1", yaml.safe_dump(s1), config.resolve(), bag.resolve(), 40.0, -1.0, run
                )


class NativeProjectionAndPassageTests(unittest.TestCase):
    def _camera(self, kind: str, record_s: float, header_s: float):
        return TRIAL.pairing.FilteredMessage(
            kind,
            int(round(record_s * 1.0e9)),
            int(round(header_s * 1.0e9)),
        )

    def test_native_projection_applies_bag_start_before_pairing(self) -> None:
        messages = [
            self._camera(TRIAL.pairing.KIND_CAMERA0, 1.0, 101.0),
            self._camera(TRIAL.pairing.KIND_CAMERA1, 1.005, 101.0),
            self._camera(TRIAL.pairing.KIND_CAMERA0, 41.0, 141.0),
            self._camera(TRIAL.pairing.KIND_CAMERA1, 41.005, 141.0),
        ]
        selected, native, start, end = TRIAL.project_native_bag_view(
            messages, 0.0, 50.0, 40.0, -1.0
        )
        self.assertEqual(start, 40.0)
        self.assertEqual(end, 50.0)
        self.assertEqual(len(selected), 2)
        self.assertEqual(len(native.pairs), 1)
        self.assertEqual(native.pairs[0].camera_timestamp_ns, 141_000_000_000)

    def test_late_initialization_is_descriptive_not_failure(self) -> None:
        interval = {
            "first_selected_input_timestamp_s": 0.0,
            "last_selected_input_timestamp_s": 100.0,
            "gaps_over_threshold": [],
        }
        state = {
            "first_timestamp_s": 50.0,
            "last_timestamp_s": 99.95,
            "maximum_timestamp_gap_s": 0.05,
            "gaps_over_threshold": [],
        }
        completion = TRIAL.assess_output_coverage(interval, state)
        self.assertFalse(completion["initialization_delay_pass"])
        self.assertTrue(completion["initialization_delay_is_descriptive_only"])
        self.assertTrue(completion["pass"])
        passage = TRIAL.passage_record(interval, state, completion)
        self.assertTrue(passage["complete"])
        self.assertEqual(passage["initialization_delay_s"], 50.0)

    def test_constant_offset_input_gap_supports_state_gap(self) -> None:
        interval = {
            "first_selected_input_timestamp_s": 0.0,
            "last_selected_input_timestamp_s": 30.0,
            "gaps_over_threshold": [
                {"start_timestamp_s": 10.0, "end_timestamp_s": 20.0, "duration_s": 10.0}
            ],
        }
        state = {
            "first_timestamp_s": 0.05,
            "last_timestamp_s": 30.0,
            "maximum_timestamp_gap_s": 10.0,
            "gaps_over_threshold": [
                {
                    "start_timestamp_s": 10.05,
                    "end_timestamp_s": 20.05,
                    "duration_s": 10.0,
                }
            ],
        }
        value = TRIAL.assess_output_coverage(interval, state)
        self.assertTrue(value["maximum_state_gap_pass"])
        self.assertEqual(value["supported_input_gap_count"], 1)

    def test_endpoint_nearness_does_not_hide_gap_duration_mismatch(self) -> None:
        interval = {
            "first_selected_input_timestamp_s": 0.0,
            "last_selected_input_timestamp_s": 30.0,
            "gaps_over_threshold": [
                {"start_timestamp_s": 10.0, "end_timestamp_s": 20.0, "duration_s": 10.0}
            ],
        }
        state = {
            "first_timestamp_s": 0.0,
            "last_timestamp_s": 30.0,
            "maximum_timestamp_gap_s": 9.999,
            "gaps_over_threshold": [
                {
                    "start_timestamp_s": 10.05,
                    "end_timestamp_s": 20.049,
                    "duration_s": 9.999,
                }
            ],
        }
        value = TRIAL.assess_output_coverage(interval, state)
        self.assertFalse(value["maximum_state_gap_pass"])
        self.assertEqual(value["supported_input_gap_count"], 0)

    def test_one_input_gap_cannot_support_two_state_gaps(self) -> None:
        interval = {
            "first_selected_input_timestamp_s": 0.0,
            "last_selected_input_timestamp_s": 30.0,
            "gaps_over_threshold": [
                {"start_timestamp_s": 10.0, "end_timestamp_s": 20.0, "duration_s": 10.0}
            ],
        }
        state_gap = {
            "start_timestamp_s": 10.05,
            "end_timestamp_s": 20.05,
            "duration_s": 10.0,
        }
        state = {
            "first_timestamp_s": 0.0,
            "last_timestamp_s": 30.0,
            "maximum_timestamp_gap_s": 10.0,
            "gaps_over_threshold": [dict(state_gap), dict(state_gap)],
        }
        value = TRIAL.assess_output_coverage(interval, state)
        self.assertFalse(value["maximum_state_gap_pass"])
        self.assertEqual(value["supported_input_gap_count"], 1)
        self.assertEqual(len(value["unsupported_state_gaps"]), 1)

    def test_numeric_table_records_large_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.txt"
            _table(path, (1.0, 1.05, 2.0))
            value = TRIAL.validate_numeric_table(path, 8)
            self.assertEqual(value["rows"], 3)
            self.assertEqual(len(value["gaps_over_threshold"]), 1)


class ConsoleAndOutcomeTests(unittest.TestCase):
    def _console(self, text: str) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "console.log"
            path.write_text(text, encoding="utf-8")
            return TRIAL.parse_console(path)

    def test_integrity_patterns_are_machine_consumed_without_benign_false_positive(self) -> None:
        self.assertTrue(self._console("state became nan\n")["nonfinite_pattern"])
        self.assertTrue(self._console("resetting estimator\n")["reset_pattern"])
        self.assertTrue(
            self._console("negative covariance detected\n")["covariance_failure_pattern"]
        )
        benign = self._console("verbosity INFO; infinite sequence; covariance valid\n")
        self.assertFalse(benign["nonfinite_pattern"])
        self.assertFalse(benign["reset_pattern"])
        self.assertFalse(benign["covariance_failure_pattern"])

    def test_exact_u0_unload_signature_is_separate(self) -> None:
        value = self._console(
            "process[icra27_cross_dataset-1]: started with pid [1]\n"
            "class_loader::LibraryUnloadException\n"
            "Attempt to unload library /opt/ros/noetic/lib/"
            "libcompressed_depth_image_transport.so\n"
            "process has died [pid 1, exit code -6, cmd x]\n"
        )
        self.assertTrue(value["post_coverage_teardown_pattern"])
        self.assertEqual(value["child_exit_codes"], [-6])

    def test_outcome_taxonomy(self) -> None:
        self.assertEqual(TRIAL.classify_outcome(_complete_facts()), "COMPLETED")
        self.assertEqual(
            TRIAL.classify_outcome(
                _complete_facts(launch_abnormal=True, exact_u0_teardown=True)
            ),
            "COMPLETED_WITH_TEARDOWN_DEFECT",
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(state_kind="empty")),
            "NO_INITIALIZATION",
        )
        self.assertEqual(
            TRIAL.classify_outcome(
                _complete_facts(state_kind="missing", launch_abnormal=True)
            ),
            "ESTIMATOR_CRASH",
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(continuity_pass=False)),
            "TRACKING_LOSS",
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(numeric_integrity_valid=False)),
            "NUMERIC_FAILURE",
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(tail_pass=False)), "PARTIAL"
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(timed_out=True)), "TIMED_OUT"
        )
        self.assertEqual(
            TRIAL.classify_outcome(_complete_facts(teardown_ok=False)),
            "TEARDOWN_FAILED",
        )
        self.assertEqual(
            TRIAL.classify_outcome(
                _complete_facts(mode="capture", capture_closed=False)
            ),
            "CAPTURE_INCOMPLETE",
        )
        self.assertEqual(
            TRIAL.classify_outcome(
                _complete_facts(mode="capture", linkage_valid=False)
            ),
            "INVALID_LINKAGE",
        )

    def test_evidence_validity_is_independent_of_estimator_outcome(self) -> None:
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "NO_INITIALIZATION", True, True, True
            ),
            "VALID",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "INFRASTRUCTURE_FAILED", False, True, True
            ),
            "INVALID_INFRA",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "INFRASTRUCTURE_FAILED", False, None, True
            ),
            "INVALID_INFRA",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "INFRASTRUCTURE_FAILED", False, False, True
            ),
            "INVALID_PROVENANCE",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "INVALID_LINKAGE", True, True, True
            ),
            "CAPTURE_LINK_INVALID",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "NO_INITIALIZATION",
                True,
                True,
                True,
                mode="capture",
                capture_closed=True,
                linkage_valid=False,
            ),
            "CAPTURE_LINK_INVALID",
        )
        self.assertEqual(
            TRIAL.classify_evidence_validity(
                "NO_INITIALIZATION",
                True,
                True,
                True,
                mode="capture",
                capture_closed=False,
                linkage_valid=True,
            ),
            "INVALID_INFRA",
        )


class CaptureLinkageTests(unittest.TestCase):
    def test_linkage_compares_content_not_path_or_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scored_result = root / "scored_result.json"
            scored_result.write_text("{}\n", encoding="utf-8")
            expected = {}
            artifacts = {}
            for name, relative in (
                ("state", "trajectory/state_estimate.txt"),
                ("deviation", "trajectory/state_deviation.txt"),
                ("tum", "trajectory/estimate_raw.tum"),
            ):
                scored = root / ("scored_" + name)
                capture = root / ("capture_" + name)
                scored.write_bytes((name + "\n").encode("utf-8"))
                capture.write_bytes(scored.read_bytes())
                os.utime(capture, ns=(capture.stat().st_atime_ns, capture.stat().st_mtime_ns + 1_000_000))
                expected[name] = TRIAL.file_identity(scored)
                artifacts[name] = _identity_artifact(capture, relative)
            linkage = {
                "expected_artifacts": expected,
                "sequence_result": TRIAL.file_identity(scored_result),
                "sequence_result_path": str(scored_result),
            }
            self.assertTrue(TRIAL.assess_capture_linkage(linkage, artifacts))
            self.assertTrue(linkage["byte_exact"])
            self.assertNotEqual(
                expected["state"]["path"], artifacts["state"]["identity"]["path"]
            )

    def test_linkage_rejects_scored_source_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "result.json"
            source = root / "source"
            capture = root / "capture"
            result_path.write_text("{}", encoding="utf-8")
            source.write_text("before", encoding="utf-8")
            capture.write_text("before", encoding="utf-8")
            expected = TRIAL.file_identity(source)
            source.write_text("after", encoding="utf-8")
            linkage = {
                "expected_artifacts": {"state": expected, "deviation": None, "tum": None},
                "sequence_result": TRIAL.file_identity(result_path),
                "sequence_result_path": str(result_path),
            }
            artifacts = {
                "state": _identity_artifact(capture, "trajectory/state_estimate.txt"),
                "deviation": {"identity": None},
                "tum": {"identity": None},
            }
            self.assertFalse(TRIAL.assess_capture_linkage(linkage, artifacts))
            self.assertFalse(linkage["source_unchanged_during_capture"])


class RuntimePinTests(unittest.TestCase):
    def test_frozen_executable_and_source_constants(self) -> None:
        self.assertEqual(
            TRIAL.pinned_runtime.PINNED_RUNTIME_POLICY["systems"]["U0"]["executable"]["sha256"],
            "c0e2203d0c01822fd089b32fb25d2b81dc850abc03c273d169b812801788d57b",
        )
        self.assertEqual(
            TRIAL.S1_RUNTIME_POLICY["systems"]["S1"]["executable"]["sha256"],
            "0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e",
        )
        self.assertEqual(TRIAL.U0_SOURCE_COMMIT, "69488123ed9362dd44b6f28e7f4680abbff1442b")
        self.assertEqual(TRIAL.S1_SOURCE_TREE, "b3f9191b8bce3852ebff71e1ebf180181bcd6d05")

    def test_s1_compiled_snapshot_matches_live_compiled_bytes(self) -> None:
        value = TRIAL._validate_s1_compiled_snapshot()
        self.assertTrue(value["all_compiled_inputs_match_live_bytes"])
        self.assertGreater(value["compiled_input_count"], 100)


if __name__ == "__main__":
    unittest.main()
