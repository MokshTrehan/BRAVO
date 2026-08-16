#!/usr/bin/python3.8
"""Focused synthetic tests for the CDSC-1R2 fresh-KAIST adapter."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "cross_dataset_kaist_trial.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_kaist_trial", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def identity(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    payload = resolved.read_bytes()
    return {
        "path": str(resolved),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "mtime_ns": resolved.stat().st_mtime_ns,
        "executable": bool(resolved.stat().st_mode & 0o111),
    }


def c2_console() -> str:
    return (
        "[LONG-GAP-RECOVERY]: event=contract_validated enabled=1 "
        "threshold_s=0.500 attempts=10 consensus=3\n"
        "[LONG-GAP-RECOVERY]: event=trigger epoch=0 timestamp=1599131279.8320987 "
        "last_state_timestamp=1599131266.3901017 gap_s=13.441997051 activation=1\n"
        "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=1 "
        "timestamp=1599131279.8320987 imu_prediction=1 accepted=1 reason=accepted "
        "supplied=22 valid=22 inliers=18 inlier_ratio=0.818181818 "
        "max_reprojection_px=1.30 min_depth=2.47 span_x=0.52 span_y=0.26 "
        "support_ratio=0.45 imu_angle_deg=1.00 state_unchanged=1\n"
        "[LONG-GAP-RECOVERY]: event=accepted_pose epoch=0 attempt=1 "
        "timestamp=1599131279.8320987 p_x=-0.020 p_y=0.080 p_z=-0.310 consecutive=1\n"
        "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=2 "
        "timestamp=1599131279.8655050 imu_prediction=1 accepted=1 reason=accepted "
        "supplied=21 valid=21 inliers=18 inlier_ratio=0.857142857 "
        "max_reprojection_px=1.29 min_depth=2.47 span_x=0.52 span_y=0.26 "
        "support_ratio=0.45 imu_angle_deg=1.04 state_unchanged=1\n"
        "[LONG-GAP-RECOVERY]: event=accepted_pose epoch=0 attempt=2 "
        "timestamp=1599131279.8655050 p_x=-0.019 p_y=0.075 p_z=-0.309 consecutive=2\n"
        "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=3 "
        "timestamp=1599131279.8978715 imu_prediction=1 accepted=1 reason=accepted "
        "supplied=21 valid=21 inliers=18 inlier_ratio=0.857142857 "
        "max_reprojection_px=1.15 min_depth=2.48 span_x=0.52 span_y=0.26 "
        "support_ratio=0.45 imu_angle_deg=1.01 state_unchanged=1\n"
        "[LONG-GAP-RECOVERY]: event=accepted_pose epoch=0 attempt=3 "
        "timestamp=1599131279.8978715 p_x=-0.016 p_y=0.072 p_z=-0.309 consecutive=3\n"
        "[LONG-GAP-RECOVERY]: event=consensus_pass epoch=0 "
        "timestamp=1599131279.8978715 radius_m=0.007 samples=3\n"
        "[LONG-GAP-RECOVERY]: event=relocalization_commit epoch=1 "
        "timestamp=1599131279.8978715 p_x=-0.016 p_y=0.072 p_z=-0.309 velocity_reset=1\n"
        "[LONG-GAP-RECOVERY]: event=first_resumed_covariance epoch=1 "
        "camera_timestamp=1599131279.8978715 state_output_timestamp=1599131279.8679130 "
        "frame=estimator_global available=1 p_cov_00=0.01 p_cov_01=0 p_cov_02=0 "
        "p_cov_10=0 p_cov_11=0.01 p_cov_12=0 p_cov_20=0 p_cov_21=0 p_cov_22=0.01\n"
        "[LONG-GAP-RECOVERY]: event=warmup_complete epoch=1 "
        "timestamp=1599131280.1003635 clones=5\n"
        "[LONG-GAP-RECOVERY]: event=summary activations=1 commits=1 "
        "failures=0 final_epoch=1\n"
    )


def input_interval() -> dict:
    return {
        "gaps_over_threshold": [
            {
                "start_timestamp_s": 1599131266.390101559,
                "end_timestamp_s": 1599131279.832098734,
                "duration_s": 13.441997175,
            }
        ]
    }


class FixedContractTests(unittest.TestCase):
    def test_schema_artifact_paths_and_geometry_topics_match_generic_contract(self) -> None:
        for system, namespace in (
            ("U0", "/icra27_kaist_u0"),
            ("S1", "/kaist_vio_turnsafe_baseline"),
        ):
            artifacts = MODULE.initial_artifacts(system)
            self.assertEqual(MODULE.SCHEMA, MODULE.common.SCHEMA)
            self.assertEqual(
                artifacts["state"]["relative_path"], "trajectory/state_estimate.txt"
            )
            self.assertEqual(
                artifacts["deviation"]["relative_path"],
                "trajectory/state_deviation.txt",
            )
            self.assertEqual(
                artifacts["tum"]["relative_path"], "trajectory/estimate_raw.tum"
            )
            self.assertEqual(
                artifacts["timing"]["relative_path"],
                "diagnostics/timing_openvins.csv",
            )
            self.assertEqual(
                artifacts["raw_geometry"]["relative_path"],
                "geometry/feature_stream.bag",
            )
            self.assertEqual(len(artifacts["raw_geometry"]["topics"]), 5)
            self.assertTrue(
                all(topic.startswith(namespace + "/") for topic in artifacts["raw_geometry"]["topics"])
            )

    def test_live_configs_and_launches_match_frozen_hashes(self) -> None:
        for system in MODULE.SYSTEMS:
            policy = MODULE.CANONICAL_INPUTS[system]
            config = Path(policy["config"]).resolve(strict=True)
            launch = Path(policy["launch"]).resolve(strict=True)
            config_contract = MODULE.validate_config_contract(system, config)
            launch_contract = MODULE.validate_launch_contract(system, launch)
            self.assertEqual(config_contract["identity"]["sha256"], policy["config_sha256"])
            self.assertEqual(launch_contract["identity"]["sha256"], policy["launch_sha256"])
            self.assertTrue(launch_contract["ground_truth_bindings_absent"])
        self.assertTrue(
            MODULE.validate_config_contract("U0", MODULE.U0_CONFIG.resolve())["native_unmodified_upstream"]
        )
        self.assertTrue(
            MODULE.validate_config_contract("S1", MODULE.S1_CONFIG.resolve())["s1_frozen_c2"]
        )

    def test_resolved_parameter_contracts_reject_extra_algorithm_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "trajectory").mkdir()
            (run_dir / "diagnostics").mkdir()
            bag = run_dir / "input.bag"
            bag.write_bytes(b"bag")
            for system in MODULE.SYSTEMS:
                config = Path(MODULE.CANONICAL_INPUTS[system]["config"]).resolve()
                expected = MODULE._expected_resolved_parameters(
                    system, config, bag.resolve(), 0.0, run_dir.resolve()
                )
                record = MODULE.validate_resolved_parameters(
                    system,
                    yaml.safe_dump(expected),
                    config,
                    bag.resolve(),
                    0.0,
                    run_dir.resolve(),
                )
                self.assertTrue(record["exact_typed_map_match"])
                poisoned = dict(expected)
                poisoned[next(iter(expected)).rsplit("/", 1)[0] + "/path_gt"] = "/tmp/gt"
                with self.assertRaisesRegex(MODULE.TrialError, "contract drift"):
                    MODULE.validate_resolved_parameters(
                        system,
                        yaml.safe_dump(poisoned),
                        config,
                        bag.resolve(),
                        0.0,
                        run_dir.resolve(),
                    )

    def test_cli_mirrors_generic_driver_and_run_has_no_ground_truth_argument(self) -> None:
        parser = MODULE._parser()
        args = parser.parse_args(
            [
                "run",
                "--protocol-id",
                "CDSC-1R2",
                "--protocol",
                str(MODULE.CANONICAL_PROTOCOL),
                "--matrix",
                str(MODULE.CANONICAL_MATRIX),
                "--run-id",
                "fresh-u0",
                "--dataset",
                "kaist_vio",
                "--sequence",
                "rotation/rotation.bag",
                "--system",
                "U0",
                "--bag",
                "/tmp/input.bag",
                "--config",
                str(MODULE.U0_CONFIG),
                "--launch",
                str(MODULE.U0_LAUNCH),
                "--binary",
                "/tmp/binary",
                "--output-root",
                "/tmp/output",
                "--ros-port",
                "19001",
            ]
        )
        self.assertEqual(args.protocol_file, MODULE.CANONICAL_PROTOCOL)
        self.assertEqual(args.matrix_file, MODULE.CANONICAL_MATRIX)
        self.assertFalse(hasattr(args, "ground_truth"))


class PairingAndOutcomeTests(unittest.TestCase):
    def synthetic_messages(self):
        F = MODULE.pairing.FilteredMessage
        return [
            F("imu", 100_000_000),
            F("camera0", 200_000_000, 1_000_000_000),
            F("camera1", 201_000_000, 1_000_000_000),
            F("imu", 300_000_000),
            F("camera0", 400_000_000, 2_000_000_000),
            F("camera1", 401_000_000, 2_000_000_000),
        ]

    def test_pair_census_selects_each_native_system_stream_and_records_gap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bag = Path(temporary) / "input.bag"
            bag.write_bytes(b"synthetic")
            messages = self.synthetic_messages()
            bag_record = {
                "path": str(bag.resolve()),
                "size_bytes": bag.stat().st_size,
                "sha256": hashlib.sha256(bag.read_bytes()).hexdigest(),
                "first_filtered_record_stamp_ns": 100_000_000,
                "last_filtered_record_stamp_ns": 401_000_000,
                "compression": "none",
                "rosbag_version": 200,
                "filtered_message_count": len(messages),
            }
            topics = {
                "camera0": {"name": MODULE.CAMERA0_TOPIC},
                "camera1": {"name": MODULE.CAMERA1_TOPIC},
                "imu": {"name": MODULE.IMU_TOPIC},
            }
            with mock.patch.object(
                MODULE.pairing, "read_bag", return_value=(messages, bag_record, topics)
            ):
                for system, source in (
                    ("U0", "upstream_native_record_time_first_forward_stereo"),
                    ("S1", "frozen_s1_exact_header_stereo"),
                ):
                    normalized, full, observed = MODULE.kaist_pair_census(
                        system, bag, 0.0, -1.0
                    )
                    self.assertEqual(normalized["input_interval"]["source"], source)
                    self.assertEqual(normalized["input_interval"]["selected_pair_count"], 2)
                    self.assertEqual(len(normalized["input_interval"]["gaps_over_threshold"]), 1)
                    self.assertEqual(full["schema"], MODULE.pairing.SCHEMA)
                    self.assertEqual(observed["sha256"], bag_record["sha256"])

    def test_algorithm_crash_is_not_relabelled_infrastructure_failure(self) -> None:
        facts = {
            "mode": "scored",
            "teardown_ok": True,
            "runtime_contract_valid": False,
            "numeric_integrity_valid": True,
            "state_kind": "missing",
            "launch_abnormal": True,
        }
        self.assertEqual(MODULE.classify_outcome(facts), "INFRASTRUCTURE_FAILED")
        facts["runtime_contract_valid"] = True
        self.assertEqual(MODULE.classify_outcome(facts), "ESTIMATOR_CRASH")
        facts.update(
            {
                "state_kind": "valid",
                "outputs_valid": True,
                "continuity_pass": True,
                "tail_pass": True,
                "exact_u0_teardown": True,
            }
        )
        self.assertEqual(
            MODULE.classify_outcome(facts), "COMPLETED_WITH_TEARDOWN_DEFECT"
        )

    def test_preflight_failure_is_published_as_infrastructure_not_no_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(
                protocol_id="CDSC-1R2",
                protocol_file=MODULE.CANONICAL_PROTOCOL,
                matrix_file=MODULE.CANONICAL_MATRIX,
                run_id="preflight-failure",
                attempt_index=1,
                dataset="kaist_vio",
                sequence="rotation/rotation.bag",
                system="U0",
                mode="scored",
                bag=root / "missing.bag",
                bag_start=0.0,
                bag_duration=-1.0,
                config=MODULE.U0_CONFIG,
                launch=MODULE.U0_LAUNCH,
                binary=root / "missing-binary",
                output_root=root / "runs",
                ros_port=19001,
                timeout_seconds=30.0,
                cpu_list="0",
                scored_result=None,
            )
            with mock.patch.object(MODULE.common, "assert_port_available"):
                result, run_dir = MODULE.run_trial(args)
            self.assertEqual(result["status"], "INFRASTRUCTURE_FAILED")
            self.assertNotEqual(result["status"], "NO_INITIALIZATION")
            self.assertEqual(result["evidence_validity"], "INVALID_INFRA")
            self.assertTrue((run_dir / "sequence_result.json").is_file())
            self.assertTrue((run_dir / "SHA256SUMS").is_file())

    def test_published_preflight_result_binds_live_runner_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "wrong-matrix-bag.bag"
            bag.write_bytes(b"not opened because matrix binding fails first")
            args = argparse.Namespace(
                protocol_id="CDSC-1R2",
                protocol_file=MODULE.CANONICAL_PROTOCOL,
                matrix_file=MODULE.CANONICAL_MATRIX,
                run_id="runner-binding-failure",
                attempt_index=1,
                dataset="kaist_vio",
                sequence="rotation/rotation.bag",
                system="U0",
                mode="scored",
                bag=bag,
                bag_start=0.0,
                bag_duration=-1.0,
                config=MODULE.U0_CONFIG,
                launch=MODULE.U0_LAUNCH,
                binary=Path(
                    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                    "build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
                ),
                output_root=root / "runs",
                ros_port=19002,
                timeout_seconds=30.0,
                cpu_list="0",
                scored_result=None,
            )
            with mock.patch.object(MODULE.common, "assert_port_available"):
                result, _ = MODULE.run_trial(args)
            self.assertEqual(result["status"], "INFRASTRUCTURE_FAILED")
            self.assertEqual(result["inputs"]["runner"], identity(SCRIPT))
            self.assertEqual(
                result["inputs"]["runtime_identity_validator"],
                identity(MODULE.RUNTIME_IDENTITY_TOOL),
            )


class RecoveryContractTests(unittest.TestCase):
    def test_exact_one_c2_contract_includes_attempt_consensus_gap_and_spd_covariance(self) -> None:
        evidence = MODULE._recovery_evidence(
            "S1", "rotation/rotation.bag", c2_console(), input_interval()
        )
        self.assertEqual(evidence["status"], "PASS")
        self.assertTrue(evidence["detail"]["commit_covariance"]["finite_symmetric_spd"])
        self.assertEqual(len(evidence["detail"]["attempts"]), 3)
        self.assertTrue(evidence["gap_binding"]["pass"])
        self.assertEqual(evidence["post_pair_rotation_evaluation"], "PENDING")

    def test_c2_attempt_gate_failure_is_retained(self) -> None:
        bad = c2_console().replace(
            "inlier_ratio=0.818181818", "inlier_ratio=0.600000000", 1
        )
        runtime = MODULE.rotation.parse_recovery_runtime(
            bad, True, "rotation/rotation.bag"
        )
        with self.assertRaisesRegex(MODULE.TrialError, "accepted-attempt gate failed"):
            MODULE.validate_c2_detail(bad, runtime)

    def test_other_ten_require_exact_zero_recovery(self) -> None:
        console = (
            "[LONG-GAP-RECOVERY]: event=contract_validated enabled=1 "
            "threshold_s=0.500 attempts=10 consensus=3\n"
            "[LONG-GAP-RECOVERY]: event=summary activations=0 commits=0 "
            "failures=0 final_epoch=0\n"
        )
        evidence = MODULE._recovery_evidence(
            "S1", "square/square.bag", console, {"gaps_over_threshold": []}
        )
        self.assertTrue(evidence["pass"])
        self.assertEqual(
            evidence["runtime"]["sequence_contract"], "ZERO_ACTIVATION_REGRESSION"
        )

    def test_u0_has_no_recovery_and_is_never_rescued(self) -> None:
        evidence = MODULE._recovery_evidence(
            "U0", "rotation/rotation.bag", "original console\n", input_interval()
        )
        self.assertTrue(evidence["pass"])
        self.assertEqual(evidence["sequence_contract"], "ORIGINAL_U0")
        with self.assertRaisesRegex(MODULE.TrialError, "unexpectedly emitted"):
            MODULE._recovery_evidence(
                "U0", "rotation/rotation.bag", c2_console(), input_interval()
            )

    def test_c2_event_bound_state_gap_is_supported_for_passage(self) -> None:
        gap_start = float(
            MODULE.rotation.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"]
        )
        commit_state = 1599131279.867913
        state_gap = {
            "start_timestamp_s": gap_start,
            "end_timestamp_s": commit_state,
            "duration_s": commit_state - gap_start,
        }
        selected_gap = input_interval()["gaps_over_threshold"][0]
        selected_start = selected_gap["start_timestamp_s"] - 20.0
        selected_end = commit_state + 0.03
        state = {
            "first_timestamp_s": selected_start + 3.0,
            "last_timestamp_s": commit_state,
            "maximum_timestamp_gap_s": state_gap["duration_s"],
            "gaps_over_threshold": [state_gap],
        }
        interval = {
            "first_selected_input_timestamp_s": selected_start,
            "last_selected_input_timestamp_s": selected_end,
            "gaps_over_threshold": [selected_gap],
        }
        mechanism = {
            "pass": True,
            "gap_binding": {"pass": True},
            "runtime": {
                "commit_covariance": {"state_output_timestamp": commit_state}
            },
            "timing": {
                "timing_contract": "TARGET_COMMIT_PLUS_FOUR_PROPAGATE_ONLY_ROWS",
                "consistent": True,
            },
        }
        result = MODULE.assess_kaist_output_coverage(
            "S1", "rotation/rotation.bag", interval, state, mechanism
        )
        self.assertTrue(result["pass"])
        self.assertTrue(result["maximum_state_gap_pass"])
        self.assertEqual(result["recovery_supported_state_gap_count"], 1)
        self.assertEqual(result["unsupported_state_gaps"], [])


class HistoricalAndPostPairTests(unittest.TestCase):
    def test_derived_historical_tum_identity_matches_exact_converter_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state.txt"
            state.write_text(
                "# timestamp(s) q p v bg ba cam_imu_dt num_cam\n"
                "1.0 0 0 0 1 2 3 4 9\n"
                "2.0 0 0 0 1 5 6 7 9\n",
                encoding="ascii",
            )
            expected = (
                "# timestamp tx ty tz qx qy qz qw\n"
                "1.0 2 3 4 0 0 0 1\n"
                "2.0 5 6 7 0 0 0 1\n"
            ).encode("ascii")
            record = MODULE._derived_tum_content_identity_from_state(state)
            self.assertEqual(record["size_bytes"], len(expected))
            self.assertEqual(record["sha256"], hashlib.sha256(expected).hexdigest())

    def test_s1_historical_mismatch_is_report_only(self) -> None:
        artifacts = {
            name: {
                "identity": {
                    "path": "/tmp/" + name,
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                }
            }
            for name in ("state", "deviation", "tum")
        }
        result = MODULE.historical_determinism_check(
            "S1", "rotation/rotation.bag", artifacts
        )
        self.assertEqual(result["status"], "MISMATCH_RETAINED")
        self.assertTrue(result["fresh_outputs_never_replaced"])
        self.assertTrue(result["mismatch_is_not_infrastructure_invalid"])

    def _post_pair_fixture(
        self,
        root: Path,
        u0_closed: bool = True,
        u0_products: bool = True,
        s1_products: bool = True,
        s1_mechanism_pass: bool = True,
    ):
        matrix_identity = identity(MODULE.CANONICAL_MATRIX)
        runs = {}
        for system in MODULE.SYSTEMS:
            run = root / system.lower()
            (run / "trajectory").mkdir(parents=True)
            paths = {
                "state": run / "trajectory" / "state_estimate.txt",
                "deviation": run / "trajectory" / "state_deviation.txt",
                "tum": run / "trajectory" / "estimate_raw.tum",
            }
            products_available = u0_products if system == "U0" else s1_products
            if products_available:
                for path in paths.values():
                    path.write_text("synthetic\n", encoding="ascii")
            manifest = {
                "schema": MODULE.SCHEMA,
                "protocol_id": "CDSC-1R2",
                "dataset": "kaist_vio",
                "sequence": "rotation/rotation.bag",
                "system": system,
                "mode": "scored",
                "run_id": "run-" + system.lower(),
                "status": "COMPLETED" if products_available else "NO_INITIALIZATION",
                "accuracy_eligible": products_available,
                "run_directory": str(run),
                "inputs": {"matrix": matrix_identity},
                "estimator_close_receipt": {
                    "estimator_attempted": True,
                    "estimator_process_group_closed": (
                        u0_closed if system == "U0" else True
                    ),
                    "runtime_services_closed": True,
                    "closed_utc": "2026-08-16T00:00:00Z",
                },
                "artifacts": {
                    name: {
                        "relative_path": "trajectory/" + path.name,
                        "identity": identity(path) if products_available else None,
                    }
                    for name, path in paths.items()
                },
            }
            if system == "S1":
                if s1_mechanism_pass:
                    manifest["robustness_mechanism"] = {
                        "status": "PASS",
                        "pass": True,
                        "runtime": {
                            "commit_covariance": {
                                "status": "AVAILABLE",
                                "position_covariance_row_major_m2": [
                                    0.01,
                                    0,
                                    0,
                                    0,
                                    0.01,
                                    0,
                                    0,
                                    0,
                                    0.01,
                                ],
                            }
                        },
                    }
                else:
                    manifest["robustness_mechanism"] = {
                        "status": "FAIL",
                        "pass": False,
                        "reason": "synthetic retained C2 mechanism failure",
                        "post_pair_rotation_evaluation": "BLOCKED_BY_RUNTIME_CONTRACT",
                    }
            result_path = run / "sequence_result.json"
            result_path.write_text(json.dumps(manifest), encoding="utf-8")
            MODULE.common.write_sha256sums(run)
            runs[system] = result_path
        return runs

    def test_closed_s1_failure_publishes_exit_two_without_opening_gt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = self._post_pair_fixture(
                root,
                u0_products=False,
                s1_products=False,
                s1_mechanism_pass=False,
            )
            output = root / "post-pair.json"
            missing_gt = root / "must-not-open.tum"
            exit_code = MODULE.main(
                [
                    "post-pair-rotation",
                    "--u0-result",
                    str(runs["U0"]),
                    "--s1-result",
                    str(runs["S1"]),
                    "--ground-truth",
                    str(missing_gt),
                    "--matrix",
                    str(MODULE.CANONICAL_MATRIX),
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(exit_code, 2)
            value = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "C2_VALIDATION_FAILURE")
            self.assertFalse(value["pass"])
            self.assertFalse(value["ground_truth_opened"])
            self.assertFalse(value["matrix_binding"]["ground_truth_opened"])
            self.assertIsNone(value["matrix_binding"]["ground_truth"])
            self.assertFalse(
                value["matrix_binding"]["ground_truth_expected_from_matrix"][
                    "live_file_opened"
                ]
            )
            self.assertEqual(
                value["c2_prerequisite"]["reason_code"],
                "S1_MECHANISM_CONTRACT_FAILED",
            )
            self.assertTrue(
                value["c2_validation_failure"]["algorithm_failure_retained"]
            )
            self.assertFalse(
                value["c2_validation_failure"]["infrastructure_failure"]
            )
            self.assertFalse(
                value["source_runs"]["U0"]["closure"]["checksum_closure"][
                    "all_scored_products_present"
                ]
            )

    def test_s1_pass_with_missing_output_is_retained_without_gt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = self._post_pair_fixture(root, s1_products=False)
            with mock.patch.object(MODULE, "_matrix_rotation_gt_binding") as gt_binding:
                result = MODULE.evaluate_rotation_post_pair(
                    runs["U0"],
                    runs["S1"],
                    root / "must-not-open.tum",
                    MODULE.CANONICAL_MATRIX,
                )
            gt_binding.assert_not_called()
            self.assertEqual(result["status"], "C2_VALIDATION_FAILURE")
            self.assertFalse(result["ground_truth_opened"])
            self.assertEqual(
                result["c2_prerequisite"]["reason_code"],
                "S1_REQUIRED_OUTPUT_UNAVAILABLE",
            )

    def test_checksum_closure_rejects_tampered_preflight_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = self._post_pair_fixture(Path(temporary))
            state = runs["U0"].parent / "trajectory" / "state_estimate.txt"
            state.write_text("tampered\n", encoding="ascii")
            with self.assertRaisesRegex(MODULE.TrialError, "checksum mismatch"):
                MODULE._load_result(runs["U0"], "U0")

    def test_checksum_closure_does_not_hide_immutable_input_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = self._post_pair_fixture(Path(temporary))
            result_path = runs["U0"]
            value = json.loads(result_path.read_text(encoding="utf-8"))
            value["inputs"]["matrix"]["sha256"] = "0" * 64
            result_path.write_text(json.dumps(value), encoding="utf-8")
            checksum = result_path.parent / "SHA256SUMS"
            checksum.unlink()
            MODULE.common.write_sha256sums(result_path.parent)
            with self.assertRaisesRegex(MODULE.TrialError, "immutable input identity drift"):
                MODULE._load_result(result_path, "U0")

    def test_post_pair_checks_both_close_receipts_before_gt_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs = self._post_pair_fixture(Path(temporary), u0_closed=False)
            with mock.patch.object(MODULE, "_matrix_rotation_gt_binding") as gt_binding:
                with self.assertRaisesRegex(MODULE.TrialError, "complete close receipt"):
                    MODULE.evaluate_rotation_post_pair(
                        runs["U0"],
                        runs["S1"],
                        Path(temporary) / "gt.tum",
                        MODULE.CANONICAL_MATRIX,
                    )
                gt_binding.assert_not_called()

    def test_post_pair_reports_nees_numeric_gate_without_changing_accuracy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = self._post_pair_fixture(root, u0_products=False)
            gt = root / "gt.tum"
            gt.write_text("synthetic\n", encoding="ascii")
            binding = {
                "matrix": identity(MODULE.CANONICAL_MATRIX),
                "row_order": 19,
                "ground_truth": identity(gt),
            }
            gap = {"synthetic": True}
            metrics = {
                "rpe_translation_1m": {"stats": {"rmse": 0.1}},
                "rpe_rotation_1m_deg": {"stats": {"rmse": 1.0}},
            }
            with mock.patch.object(
                MODULE, "_matrix_rotation_gt_binding", return_value=binding
            ), mock.patch.object(
                MODULE.rotation, "evaluate_rotation_gap", return_value=gap
            ), mock.patch.object(
                MODULE, "_single_system_rotation_metrics", return_value=metrics
            ), mock.patch.object(
                MODULE.rotation,
                "assess_rotation_gap_evidence",
                return_value={"pass": True},
            ), mock.patch.object(
                MODULE.rotation,
                "assess_rotation_target_acceptance",
                return_value={"pass": True, "admission_pass": True},
            ):
                result = MODULE.evaluate_rotation_post_pair(
                    runs["U0"], runs["S1"], gt, MODULE.CANONICAL_MATRIX
                )
            self.assertEqual(result["status"], "PASS")
            self.assertTrue(
                result["accuracy_eligibility_unchanged_by_c2_target_thresholds"]
            )
            self.assertTrue(
                result["both_scored_estimator_groups_closed_before_ground_truth_open"]
            )
            self.assertTrue(
                result["source_runs"]["U0"][
                    "trajectory_not_required_for_c2_evaluation"
                ]
            )
            self.assertFalse(result["source_runs"]["U0"]["accuracy_eligible"])


if __name__ == "__main__":
    unittest.main()
