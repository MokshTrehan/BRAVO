#!/usr/bin/python3
"""Synthetic tests for the append-only rotation robustness harness."""

from __future__ import annotations

import copy
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import yaml


MODULE_PATH = Path(__file__).resolve().parents[1] / "rotation_robustness_trial.py"
SPEC = importlib.util.spec_from_file_location("rotation_robustness_trial", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
TRIAL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TRIAL
SPEC.loader.exec_module(TRIAL)


def _tum_line(timestamp: float, position, quaternion) -> str:
    values = [timestamp, *position, *quaternion]
    return " ".join("{:.9f}".format(float(value)) for value in values) + "\n"


class ArtifactPublicationTests(unittest.TestCase):
    def test_atomic_publication_and_checksum_set_are_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            TRIAL._atomic_write_new_bytes(root / "nested" / "a.txt", b"alpha\n")
            TRIAL.atomic_write_new_json(root / "manifest.json", {"status": "FAILED"})
            entries = TRIAL.write_sha256sums(root)
            self.assertEqual(set(entries), {"manifest.json", "nested/a.txt"})
            lines = (root / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            self.assertEqual([line.split("  ", 1)[1] for line in lines], sorted(entries))
            with self.assertRaises(TRIAL.TrialError):
                TRIAL._atomic_write_new_bytes(root / "nested" / "a.txt", b"replacement")
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.write_sha256sums(root)

    def test_run_directory_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = TRIAL.create_run_directory(root, "C2-rotation-r1-scored")
            self.assertTrue((first / "trajectory").is_dir())
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.create_run_directory(root, "C2-rotation-r1-scored")
            with self.assertRaises(TRIAL.TrialError):
                TRIAL.create_run_directory(root, "../escape")


class RuntimeSeamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = (
            "[SERIAL-KAIST]: exact_header_pairs=100 camera0_without_match=2 "
            "camera1_without_match=3 record_delta_ge_20ms=7 "
            "maximum_record_delta_ns=42000000\n"
            "[SERIAL-KAIST]: queued_pairs=80 processed_pairs=80 "
            "frequency_thinned_pairs=20 cam0_decode_failures=0 "
            "cam1_decode_failures=0 pending_pairs=0\n"
        )

    def test_exact_header_and_enqueue_summaries_close(self) -> None:
        value = TRIAL.parse_runtime_summaries(self.console)
        self.assertEqual(value["exact_header"]["counts"]["exact_header_pairs"], 100)
        self.assertTrue(value["checks"]["pair_accounting_complete"])
        census = {
            "schema": "schurvio.icra27.kaist_pairing_census.v1",
            "census": {
                "s1_exact_pair_count": 100,
                "camera0_unmatched_count": 2,
                "camera1_unmatched_count": 3,
                "s1_first_selected_header_stamp_ns": 10,
                "s1_last_selected_header_stamp_ns": 20,
            },
            "selection_bounds": {"s1_exact_header": {"count": 100}},
        }
        binding = TRIAL.bind_pairing_census(value, census)
        self.assertEqual(binding["status"], "AVAILABLE")
        self.assertTrue(binding["runtime_matches_static_census"])

    def test_recovery_flag_accepts_exact_opencv_yaml_directive_only(self) -> None:
        self.assertTrue(
            TRIAL.candidate_recovery_enabled(
                "%YAML:1.0\nlong_gap_recovery_enabled: true\n"
            )
        )
        self.assertFalse(
            TRIAL.candidate_recovery_enabled(
                "%YAML:1.0\r\nlong_gap_recovery_enabled: false\r\n"
            )
        )
        with self.assertRaisesRegex(TRIAL.TrialError, "YAML is invalid"):
            TRIAL.candidate_recovery_enabled(
                "%YAML:9.9\nlong_gap_recovery_enabled: true\n"
            )
        with self.assertRaisesRegex(TRIAL.TrialError, "must be Boolean"):
            TRIAL.candidate_recovery_enabled(
                "%YAML:1.0\nlong_gap_recovery_enabled: yes\n"
            )

    def test_crash_without_runtime_summary_retains_static_census(self) -> None:
        crash_console = (
            "terminate called after throwing an instance of 'cv::Exception'\n"
            "\x1b[31m============================================================"
            "REQUIRED process [kaist_vio_turnsafe_baseline-2] has died!\n"
            "process has died [pid 1234, exit code -6, cmd /build/ros1_serial_msckf "
            "__name:=kaist_vio_turnsafe_baseline].\n"
            "\x1b[0m"
        )
        outcome = TRIAL.parse_roslaunch_child_deaths(crash_console)
        self.assertTrue(outcome["estimator_required_child_died"])
        self.assertEqual(outcome["process_deaths"][0]["exit_code"], -6)
        wrapper_record = {
            "exit_code": 0,
            "timed_out": False,
            "interrupted": False,
            "process_group_survived_cleanup": False,
            "error": None,
        }
        self.assertEqual(
            TRIAL.classify_trial_status(
                launch_record=wrapper_record,
                estimator_child_died=True,
                state_valid=False,
                output_valid=False,
                seam_valid=False,
                provenance_valid=True,
                evaluation_valid=False,
                capture_valid=True,
            ),
            "ESTIMATOR_FAILED",
        )
        census = {
            "schema": "schurvio.icra27.kaist_pairing_census.v1",
            "census": {
                "s1_first_selected_header_stamp_ns": 10,
                "s1_last_selected_header_stamp_ns": 20,
            },
            "selection_bounds": {"s1_exact_header": {"count": 100}},
        }
        binding = TRIAL.bind_pairing_census({}, census)
        self.assertEqual(binding["status"], "UNAVAILABLE")
        self.assertEqual(binding["reason"], "runtime_summary_unavailable")
        self.assertTrue(binding["static_census_retained"])

    def test_clean_required_node_completion_is_not_a_child_failure(self) -> None:
        console = (
            "\x1b[31m========================================REQUIRED process "
            "[kaist_vio_turnsafe_baseline-2] has died!\n"
            "process has finished cleanly\n\x1b[0m"
        )
        outcome = TRIAL.parse_roslaunch_child_deaths(console)
        self.assertFalse(outcome["estimator_required_child_died"])
        self.assertTrue(outcome["estimator_required_child_completed_cleanly"])
        self.assertFalse(outcome["required_process_death_detected"])
        wrapper_record = {
            "exit_code": 0,
            "timed_out": False,
            "interrupted": False,
            "process_group_survived_cleanup": False,
            "error": None,
        }
        self.assertEqual(
            TRIAL.classify_trial_status(
                launch_record=wrapper_record,
                estimator_child_died=False,
                state_valid=True,
                output_valid=True,
                seam_valid=True,
                provenance_valid=True,
                evaluation_valid=True,
                capture_valid=True,
            ),
            "COMPLETED",
        )

    def test_enabled_recovery_requires_exact_one_success_and_covariance(self) -> None:
        recovery = (
            "[LONG-GAP-RECOVERY]: event=contract_validated enabled=1 "
            "threshold_s=0.500 attempts=10 consensus=3\n"
            "[LONG-GAP-RECOVERY]: event=trigger epoch=0 timestamp=20.0 "
            "last_state_timestamp=6.5 gap_s=13.5 activation=1\n"
            "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=1 "
            "timestamp=20.00 accepted=1 state_unchanged=1\n"
            "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=2 "
            "timestamp=20.04 accepted=1 state_unchanged=1\n"
            "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt=3 "
            "timestamp=20.08 accepted=1 state_unchanged=1\n"
            "[LONG-GAP-RECOVERY]: event=relocalization_commit epoch=1 "
            "timestamp=20.1 p_x=1.0 p_y=2.0 p_z=3.0 velocity_reset=1\n"
            "[LONG-GAP-RECOVERY]: event=first_resumed_covariance epoch=1 "
            "camera_timestamp=20.1 state_output_timestamp=20.07 "
            "frame=estimator_global available=1 "
            "p_cov_00=0.1 p_cov_01=0 p_cov_02=0 "
            "p_cov_10=0 p_cov_11=0.2 p_cov_12=0 "
            "p_cov_20=0 p_cov_21=0 p_cov_22=0.3\n"
            "[LONG-GAP-RECOVERY]: event=warmup_complete epoch=1 "
            "timestamp=20.3 clones=5\n"
            "[LONG-GAP-RECOVERY]: event=summary activations=1 commits=1 "
            "failures=0 final_epoch=1\n"
        )
        parsed = TRIAL.parse_recovery_runtime(
            recovery, True, "rotation/rotation.bag"
        )
        self.assertEqual(parsed["status"], "AVAILABLE")
        self.assertEqual(parsed["sequence_contract"], "EXACT_ONE_TARGET_RECOVERY")
        self.assertEqual(parsed["summary"]["commits"], 1)
        self.assertEqual(len(parsed["attempts"]), 3)
        self.assertTrue(all(attempt["state_unchanged"] for attempt in parsed["attempts"]))
        self.assertEqual(
            parsed["commit_covariance"]["state_output_timestamp"], 20.07
        )
        self.assertEqual(
            TRIAL.parse_recovery_runtime(
                "", False, "rotation/rotation.bag"
            )["status"],
            "NOT_ENABLED",
        )
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.parse_recovery_runtime(
                recovery + recovery.splitlines()[-1] + "\n",
                True,
                "rotation/rotation.bag",
            )
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.parse_recovery_runtime(
                recovery, False, "rotation/rotation.bag"
            )
        with self.assertRaisesRegex(TRIAL.TrialError, "mutated the live state"):
            TRIAL.parse_recovery_runtime(
                recovery.replace(
                    "timestamp=20.04 accepted=1 state_unchanged=1",
                    "timestamp=20.04 accepted=1 state_unchanged=0",
                ),
                True,
                "rotation/rotation.bag",
            )
        first_attempt = recovery.index("[LONG-GAP-RECOVERY]: event=attempt")
        commit_event = recovery.index(
            "[LONG-GAP-RECOVERY]: event=relocalization_commit"
        )
        eleven_attempts = "".join(
            "[LONG-GAP-RECOVERY]: event=attempt epoch=0 attempt={} "
            "timestamp={:.2f} accepted=1 state_unchanged=1\n".format(
                index, 20.0 + 0.04 * (index - 1)
            )
            for index in range(1, 12)
        )
        with self.assertRaisesRegex(TRIAL.TrialError, "three to ten"):
            TRIAL.parse_recovery_runtime(
                recovery[:first_attempt] + eleven_attempts + recovery[commit_event:],
                True,
                "rotation/rotation.bag",
            )

    def test_enabled_regressions_require_exact_zero_and_no_activity(self) -> None:
        regression = (
            "[LONG-GAP-RECOVERY]: event=contract_validated enabled=1 "
            "threshold_s=0.500 attempts=10 consensus=3\n"
            "[LONG-GAP-RECOVERY]: event=summary activations=0 commits=0 "
            "failures=0 final_epoch=0\n"
        )
        regression_sequences = [
            sequence
            for sequence in TRIAL.SEQUENCES
            if sequence != "rotation/rotation.bag"
        ]
        self.assertEqual(len(regression_sequences), 10)
        for sequence in regression_sequences:
            with self.subTest(sequence=sequence):
                parsed = TRIAL.parse_recovery_runtime(regression, True, sequence)
                self.assertEqual(
                    parsed["sequence_contract"], "ZERO_ACTIVATION_REGRESSION"
                )
                self.assertTrue(parsed["contract"]["no_recovery_activity_events"])
        active = regression.replace(
            "[LONG-GAP-RECOVERY]: event=summary",
            "[LONG-GAP-RECOVERY]: event=degraded_frame epoch=0 "
            "timestamp=9.0 state_unchanged=1\n"
            "[LONG-GAP-RECOVERY]: event=summary",
        )
        with self.assertRaisesRegex(TRIAL.TrialError, "recovery activity"):
            TRIAL.parse_recovery_runtime(
                active, True, "rotation/rotation_fast.bag"
            )
        wrong_summary = regression.replace("activations=0", "activations=1")
        with self.assertRaisesRegex(TRIAL.TrialError, "exact-zero"):
            TRIAL.parse_recovery_runtime(
                wrong_summary, True, "rotation/rotation_fast.bag"
            )

    def test_duplicate_or_incomplete_runtime_summary_fails_closed(self) -> None:
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.parse_runtime_summaries(self.console + self.console.splitlines()[0] + "\n")
        broken = self.console.replace("pending_pairs=0", "pending_pairs=1")
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.parse_runtime_summaries(broken)

    def test_resolved_parameters_require_exact_header_and_frozen_seams(self) -> None:
        bag = Path("/tmp/candidate-input.bag")
        config = Path("/tmp/candidate.yaml")
        expected = {
            "path_bag": str(bag),
            "config_path": str(config),
            "kaist_vio_exact_header_stereo": True,
            "cam0_rostopic": "/turnsafe/kaist/infra1/image_raw",
            "cam1_rostopic": "/turnsafe/kaist/infra2/image_raw",
            "imu0_rostopic": "/mavros/imu/data",
            "use_fej": True,
            "use_stereo": True,
            "max_cameras": 2,
            "feat_rep_msckf": "GLOBAL_3D",
            "up_msckf_landmark_elimination": "schur",
            "up_msckf_max_visual_passes": 1,
            "calib_cam_extrinsics": False,
            "calib_cam_intrinsics": False,
            "calib_cam_timeoffset": False,
            "calib_imu_intrinsics": False,
            "calib_imu_g_sensitivity": False,
            "num_opencv_threads": 0,
            "multi_threading_pubs": False,
            "multi_threading_subs": False,
            "save_total_state": True,
            "record_timing_information": True,
            "candidate_only_parameter": 0.5,
        }
        raw = yaml.safe_dump({"/candidate/" + key: value for key, value in expected.items()})
        validated = TRIAL.validate_resolved_parameters(raw, bag, config)
        self.assertTrue(validated["exact_header_seam_preserved"])
        expected["kaist_vio_exact_header_stereo"] = False
        raw = yaml.safe_dump({"/candidate/" + key: value for key, value in expected.items()})
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.validate_resolved_parameters(raw, bag, config)

    def test_ground_truth_firewall_checks_all_runtime_surfaces(self) -> None:
        reference = Path("/private/reference.tum")
        clean = TRIAL.validate_runtime_firewall(
            ["roslaunch", "candidate.launch", "bag:=adapted.bag"],
            "/candidate/path_bag: adapted.bag\n",
            "use_klt: true\n",
            "topics: [/camera0, /camera1, /imu]\n",
            reference,
        )
        self.assertTrue(clean["launch_argv_clean"])
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.validate_runtime_firewall(
                ["roslaunch", "candidate.launch", "path_gt:=/private/reference.tum"],
                "{}",
                "use_klt: true\n",
                "topics: []\n",
                reference,
            )
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.validate_runtime_firewall(
                ["roslaunch", "candidate.launch"],
                "{}",
                "use_klt: true\n",
                "topics: [/pose_transformed]\n",
                reference,
            )


class OutputValidationTests(unittest.TestCase):
    @staticmethod
    def _write_consistency_tables(
        root: Path, state_times, timing_times
    ) -> tuple[Path, Path, Path, Path]:
        paths = tuple(
            root / name
            for name in ("state.txt", "deviation.txt", "timing.csv", "trajectory.tum")
        )
        state, deviation, timing, trajectory = paths
        space_rows = "".join("{:.9f} 0\n".format(value) for value in state_times)
        state.write_text(space_rows, encoding="utf-8")
        deviation.write_text(space_rows, encoding="utf-8")
        trajectory.write_text(space_rows, encoding="utf-8")
        timing.write_text(
            "".join("{:.9f},0\n".format(value) for value in timing_times),
            encoding="utf-8",
        )
        return state, deviation, timing, trajectory

    def test_no_rows_and_nonfinite_are_distinct_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            empty = root / "empty.txt"
            empty.write_text("# header only\n", encoding="utf-8")
            with self.assertRaises(TRIAL.NoRowsError):
                TRIAL.validate_numeric_table(empty, 2)
            invalid = root / "invalid.txt"
            invalid.write_text("1.0 2.0\n2.0 nan\n", encoding="utf-8")
            with self.assertRaisesRegex(TRIAL.TrialError, "nonfinite"):
                TRIAL.validate_numeric_table(invalid, 2)

    def test_short_command_records_argv_log_and_process_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "command.log"
            record = TRIAL.run_command(
                ["/usr/bin/python3", "-c", "print('synthetic')"],
                log,
                os.environ,
                5.0,
            )
            self.assertTrue(TRIAL.command_succeeded(record))
            self.assertEqual(log.read_text(encoding="utf-8"), "synthetic\n")
            self.assertFalse(record["process_group_survived_cleanup"])

    def test_pinned_evo_environment_preserves_isolated_runtime_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site-packages"
            (site / "evo").mkdir(parents=True)
            (site / "evo" / "__init__.py").write_text(
                "__version__ = 'synthetic'\n", encoding="utf-8"
            )
            metadata = site / "evo-1.0.dist-info" / "METADATA"
            metadata.parent.mkdir()
            metadata.write_text("Name: evo\nVersion: 1.0\n", encoding="utf-8")
            runtime_environment = {
                "HOME": "/isolated/trial/home",
                "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
            }
            environment, binding = TRIAL.pinned_post_close_python_environment(
                runtime_environment,
                site,
            )
        self.assertEqual(environment["HOME"], "/isolated/trial/home")
        self.assertEqual(binding["runtime_home_remains_isolated"], environment["HOME"])
        self.assertEqual(environment["PYTHONPATH"].split(os.pathsep)[0], str(site))
        self.assertEqual(
            runtime_environment["PYTHONPATH"],
            "/opt/ros/noetic/lib/python3/dist-packages",
        )
        self.assertNotIn(str(site), runtime_environment["PYTHONPATH"])
        self.assertEqual(
            binding["activation_boundary"], "post_estimator_process_group_close_only"
        )
        self.assertIn("geometry_atlas", binding["consumers"])
        estimator_environment = TRIAL.select_command_environment(
            "estimator_runtime", runtime_environment
        )
        atlas_environment = TRIAL.select_command_environment(
            "geometry_atlas", runtime_environment, environment
        )
        self.assertNotIn(str(site), estimator_environment["PYTHONPATH"])
        self.assertEqual(atlas_environment["PYTHONPATH"].split(os.pathsep)[0], str(site))
        with self.assertRaises(TRIAL.TrialError):
            TRIAL.select_command_environment(
                "geometry_atlas", runtime_environment
            )

    def test_default_off_and_zero_activation_keep_exact_timing_parity(self) -> None:
        times = [1.0, 2.0, 3.0]
        contracts = (
            (
                "rotation/rotation.bag",
                {"status": "NOT_ENABLED", "sequence_contract": "DEFAULT_OFF"},
            ),
            (
                "rotation/rotation_fast.bag",
                {
                    "status": "AVAILABLE",
                    "sequence_contract": "ZERO_ACTIVATION_REGRESSION",
                },
            ),
        )
        for sequence, recovery in contracts:
            with self.subTest(sequence=sequence), tempfile.TemporaryDirectory() as temporary:
                paths = self._write_consistency_tables(Path(temporary), times, times)
                result = TRIAL.validate_output_consistency(
                    *paths, sequence=sequence, recovery_runtime=recovery
                )
                self.assertEqual(result["timing_contract"], "FROZEN_EXACT_ROW_PARITY")
                paths[-2].write_text("1.000000000,0\n2.000000000,0\n", encoding="utf-8")
                with self.assertRaisesRegex(TRIAL.TrialError, "row counts differ"):
                    TRIAL.validate_output_consistency(
                        *paths, sequence=sequence, recovery_runtime=recovery
                    )

    def test_target_recovery_requires_exact_five_row_timing_omission(self) -> None:
        state_times = [1.0, 2.0, 10.00, 10.04, 10.08, 10.12, 10.16, 10.20, 10.24]
        timing_times = [1.0, 2.0, 10.20, 10.24]
        recovery = {
            "status": "AVAILABLE",
            "sequence_contract": "EXACT_ONE_TARGET_RECOVERY",
            "commit_covariance": {
                "camera_timestamp": 10.03,
                "state_output_timestamp": 10.00,
            },
            "warmup_complete": {"timestamp": 10.23},
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = self._write_consistency_tables(
                Path(temporary), state_times, timing_times
            )
            result = TRIAL.validate_output_consistency(
                *paths,
                sequence="rotation/rotation.bag",
                recovery_runtime=recovery,
            )
            self.assertEqual(result["untimed_state_row_count"], 5)
            self.assertEqual(result["untimed_state_indexes"], [2, 3, 4, 5, 6])
            self.assertTrue(result["commit_state_bound_to_covariance_event"])
            self.assertTrue(result["warmup_complete_state_carries_timing"])
            paths[-2].write_text(
                "1.000000000,0\n2.000000000,0\n10.160000000,0\n"
                "10.200000000,0\n10.240000000,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(TRIAL.TrialError, "exactly five"):
                TRIAL.validate_output_consistency(
                    *paths,
                    sequence="rotation/rotation.bag",
                    recovery_runtime=recovery,
                )

    def test_terminal_completion_retains_frozen_point_one_second_bound(self) -> None:
        passing = TRIAL.assess_terminal_completion(
            20_000_000_000, {"last_timestamp": 19.95}
        )
        self.assertTrue(passing["tail_gap_pass"])
        self.assertEqual(passing["tail_gap_max_seconds"], 0.10)
        failing = TRIAL.assess_terminal_completion(
            20_000_000_000, {"last_timestamp": 19.89}
        )
        self.assertFalse(failing["tail_gap_pass"])


class RotationGapEvaluatorTests(unittest.TestCase):
    def test_every_target_numeric_endpoint_is_finite_inclusive_and_independent(self) -> None:
        thresholds = dict(TRIAL.ROTATION_TARGET_THRESHOLDS)
        gap = {
            "cross_gap_relative_transform": {
                "relative_translation_error_m": thresholds[
                    "cross_gap_translation_m"
                ],
                "relative_rotation_error_deg": thresholds[
                    "cross_gap_rotation_deg"
                ],
            },
            "segments": {
                "full_independent_se3_alignment": {
                    "rmse_m": thresholds["full_ate_rmse_m"]
                },
                "pre_gap_independent_se3_alignment": {
                    "rmse_m": thresholds["pre_gap_ate_rmse_m"]
                },
                "post_gap_independent_se3_alignment": {
                    "rmse_m": thresholds["post_gap_ate_rmse_m"]
                },
            },
            "first_resumed_position_nees": {
                "nees": thresholds["first_resumed_position_nees"]
            },
        }
        metrics = {
            "rpe_translation_1m": {
                "stats": {"rmse": thresholds["translation_rpe_1m_rmse_m"]}
            },
            "rpe_rotation_1m_deg": {
                "stats": {"rmse": thresholds["rotation_rpe_1m_rmse_deg"]}
            },
        }
        exact = TRIAL.assess_rotation_target_numeric_acceptance(gap, metrics)
        self.assertTrue(exact["pass"])
        self.assertTrue(all(gate["pass"] for gate in exact["gates"].values()))

        setters = {
            "cross_gap_translation_m": lambda g, m, value: g[
                "cross_gap_relative_transform"
            ].__setitem__("relative_translation_error_m", value),
            "cross_gap_rotation_deg": lambda g, m, value: g[
                "cross_gap_relative_transform"
            ].__setitem__("relative_rotation_error_deg", value),
            "full_ate_rmse_m": lambda g, m, value: g["segments"][
                "full_independent_se3_alignment"
            ].__setitem__("rmse_m", value),
            "pre_gap_ate_rmse_m": lambda g, m, value: g["segments"][
                "pre_gap_independent_se3_alignment"
            ].__setitem__("rmse_m", value),
            "post_gap_ate_rmse_m": lambda g, m, value: g["segments"][
                "post_gap_independent_se3_alignment"
            ].__setitem__("rmse_m", value),
            "translation_rpe_1m_rmse_m": lambda g, m, value: m[
                "rpe_translation_1m"
            ]["stats"].__setitem__("rmse", value),
            "rotation_rpe_1m_rmse_deg": lambda g, m, value: m[
                "rpe_rotation_1m_deg"
            ]["stats"].__setitem__("rmse", value),
            "first_resumed_position_nees": lambda g, m, value: g[
                "first_resumed_position_nees"
            ].__setitem__("nees", value),
        }
        self.assertEqual(set(setters), set(thresholds))
        for name, setter in setters.items():
            with self.subTest(endpoint=name, failure="above_threshold"):
                changed_gap = copy.deepcopy(gap)
                changed_metrics = copy.deepcopy(metrics)
                setter(
                    changed_gap,
                    changed_metrics,
                    thresholds[name] + max(1.0e-6, thresholds[name] * 1.0e-6),
                )
                assessed = TRIAL.assess_rotation_target_numeric_acceptance(
                    changed_gap, changed_metrics
                )
                self.assertFalse(assessed["pass"])
                self.assertFalse(assessed["gates"][name]["pass"])
                self.assertEqual(
                    [key for key, gate in assessed["gates"].items() if not gate["pass"]],
                    [name],
                )
            with self.subTest(endpoint=name, failure="nonfinite"):
                changed_gap = copy.deepcopy(gap)
                changed_metrics = copy.deepcopy(metrics)
                setter(changed_gap, changed_metrics, float("nan"))
                assessed = TRIAL.assess_rotation_target_numeric_acceptance(
                    changed_gap, changed_metrics
                )
                self.assertFalse(assessed["pass"])
                self.assertFalse(assessed["gates"][name]["finite"])
                self.assertIsNone(assessed["gates"][name]["observed"])

    def test_pre_post_and_cross_gap_metrics_use_frozen_boundaries(self) -> None:
        pre = float(TRIAL.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
        post = float(TRIAL.ROTATION_GAP["first_post_gap_estimator_timestamp_s"])
        times = [pre - 2.0, pre - 1.0, pre, post, post + 1.0, post + 2.0]
        ground_positions = [
            np.asarray([0.0, 0.0, 0.0]),
            np.asarray([1.0, 0.0, 0.0]),
            np.asarray([2.0, 1.0, 0.0]),
            np.asarray([2.1, 1.0, 0.0]),
            np.asarray([3.0, 1.5, 0.0]),
            np.asarray([4.0, 2.0, 0.0]),
        ]
        identity_quaternion = np.asarray([0.0, 0.0, 0.0, 1.0])
        angle = math.pi / 2.0
        world_rotation = np.asarray(
            [
                [math.cos(angle), -math.sin(angle), 0.0],
                [math.sin(angle), math.cos(angle), 0.0],
                [0.0, 0.0, 1.0],
            ]
        )
        world_quaternion = np.asarray([0.0, 0.0, math.sin(angle / 2), math.cos(angle / 2)])
        world_translation = np.asarray([10.0, -3.0, 2.0])
        drift_reference_frame = np.asarray([0.5, 0.0, 0.0])
        estimate_positions = []
        for index, ground in enumerate(ground_positions):
            drift = world_rotation @ drift_reference_frame if index >= 3 else 0.0
            estimate_positions.append(world_rotation @ ground + world_translation + drift)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.tum"
            trajectory = root / "estimate.tum"
            reference.write_text(
                "# timestamp tx ty tz qx qy qz qw\n"
                + "".join(
                    _tum_line(timestamp, position, identity_quaternion)
                    for timestamp, position in zip(times, ground_positions)
                ),
                encoding="utf-8",
            )
            trajectory.write_text(
                "# timestamp tx ty tz qx qy qz qw\n"
                + "".join(
                    _tum_line(timestamp, position, world_quaternion)
                    for timestamp, position in zip(times, estimate_positions)
                ),
                encoding="utf-8",
            )
            covariance = {
                "status": "AVAILABLE",
                "reason": "NONE",
                "event": "first_resumed_covariance",
                "semantic_binding": "relocalization_commit_emitted_state",
                "epoch": 1,
                "camera_timestamp": post + 0.03,
                "state_output_timestamp": post,
                "frame": "estimator_global",
                "position_covariance_row_major_m2": [
                    0.25,
                    0.0,
                    0.0,
                    0.0,
                    0.25,
                    0.0,
                    0.0,
                    0.0,
                    0.25,
                ],
            }
            result = TRIAL.evaluate_rotation_gap(
                trajectory, reference, covariance
            )

        segments = result["segments"]
        self.assertLess(segments["pre_gap_independent_se3_alignment"]["rmse_m"], 1e-7)
        self.assertLess(segments["post_gap_independent_se3_alignment"]["rmse_m"], 1e-7)
        self.assertAlmostEqual(
            segments["post_gap_under_pre_gap_alignment"]["rmse_m"], 0.5, places=6
        )
        cross = result["cross_gap_relative_transform"]
        self.assertEqual(cross["status"], "AVAILABLE")
        self.assertAlmostEqual(cross["relative_translation_error_m"], 0.5, places=6)
        self.assertAlmostEqual(cross["relative_rotation_error_deg"], 0.0, places=6)
        self.assertEqual(
            result["frozen_gap"]["selected_stereo_gap_seconds"], 13.441997175
        )
        nees = result["first_resumed_position_nees"]
        self.assertEqual(nees["status"], "AVAILABLE")
        self.assertAlmostEqual(nees["nees"], 1.0, places=6)
        self.assertTrue(nees["pass"])
        self.assertTrue(nees["covariance_finite_symmetric_spd"])
        self.assertTrue(nees["commit_state_is_first_published_post_gap_anchor"])
        self.assertTrue(TRIAL.assess_rotation_gap_evidence(result, True)["pass"])

    def test_commit_covariance_must_be_spd_and_bind_first_post_anchor(self) -> None:
        pre = float(TRIAL.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
        post = float(TRIAL.ROTATION_GAP["first_post_gap_estimator_timestamp_s"])
        times = [pre - 2.0, pre - 1.0, pre, post, post + 1.0, post + 2.0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.tum"
            trajectory = root / "estimate.tum"
            text = "# tum\n" + "".join(
                _tum_line(timestamp, [index, index % 2, 0.0], [0.0, 0.0, 0.0, 1.0])
                for index, timestamp in enumerate(times)
            )
            reference.write_text(text, encoding="utf-8")
            trajectory.write_text(text, encoding="utf-8")
            invalid_covariance = {
                "status": "AVAILABLE",
                "state_output_timestamp": post,
                "position_covariance_row_major_m2": [
                    1.0,
                    0.0,
                    0.0,
                    0.0,
                    -1.0,
                    0.0,
                    0.0,
                    0.0,
                    1.0,
                ],
            }
            result = TRIAL.evaluate_rotation_gap(
                trajectory, reference, invalid_covariance
            )
            wrong_state = dict(invalid_covariance)
            wrong_state["state_output_timestamp"] = post + 1.0
            wrong_state["position_covariance_row_major_m2"] = [
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                1.0,
            ]
            wrong_binding = TRIAL.evaluate_rotation_gap(
                trajectory, reference, wrong_state
            )
        self.assertEqual(result["first_resumed_position_nees"]["status"], "INVALID")
        self.assertIn("not SPD", result["first_resumed_position_nees"]["reason"])
        self.assertEqual(
            wrong_binding["first_resumed_position_nees"]["status"], "INVALID"
        )
        self.assertFalse(
            wrong_binding["first_resumed_position_nees"][
                "commit_state_is_first_published_post_gap_anchor"
            ]
        )
        self.assertFalse(
            TRIAL.assess_rotation_gap_evidence(wrong_binding, True)["pass"]
        )

    def test_first_post_anchor_must_remain_within_frozen_point_one_seconds(self) -> None:
        pre = float(TRIAL.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
        post = float(TRIAL.ROTATION_GAP["first_post_gap_estimator_timestamp_s"])
        identity = [0.0, 0.0, 0.0, 1.0]
        for delay, expected_status in ((0.06, "AVAILABLE"), (0.11, "UNAVAILABLE")):
            times = [pre - 2.0, pre - 1.0, pre, post + delay, post + 1.0]
            with self.subTest(delay=delay), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                reference = root / "reference.tum"
                trajectory = root / "estimate.tum"
                text = "# tum\n" + "".join(
                    _tum_line(timestamp, [index, index % 2, 0.0], identity)
                    for index, timestamp in enumerate(times)
                )
                reference.write_text(text, encoding="utf-8")
                trajectory.write_text(text, encoding="utf-8")
                result = TRIAL.evaluate_rotation_gap(trajectory, reference)
                self.assertEqual(
                    result["cross_gap_relative_transform"]["status"], expected_status
                )
                self.assertEqual(
                    result["frozen_gap"]["post_anchor_search_limit_seconds"], 0.10
                )

    def test_missing_fixed_post_anchor_is_explicitly_unavailable(self) -> None:
        pre = float(TRIAL.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
        times = [pre - 2.0, pre - 1.0, pre]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.tum"
            trajectory = root / "estimate.tum"
            text = "# tum\n" + "".join(
                _tum_line(timestamp, [index, index % 2, 0.0], [0.0, 0.0, 0.0, 1.0])
                for index, timestamp in enumerate(times)
            )
            reference.write_text(text, encoding="utf-8")
            trajectory.write_text(text, encoding="utf-8")
            result = TRIAL.evaluate_rotation_gap(trajectory, reference)
        self.assertEqual(result["cross_gap_relative_transform"]["status"], "UNAVAILABLE")
        self.assertEqual(
            result["segments"]["post_gap_independent_se3_alignment"]["status"],
            "UNAVAILABLE",
        )
        self.assertFalse(TRIAL.assess_rotation_gap_evidence(result, False)["pass"])


if __name__ == "__main__":
    unittest.main()
