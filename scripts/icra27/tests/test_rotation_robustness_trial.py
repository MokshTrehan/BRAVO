#!/usr/bin/python3
"""Synthetic tests for the append-only rotation robustness harness."""

from __future__ import annotations

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


class RotationGapEvaluatorTests(unittest.TestCase):
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
            result = TRIAL.evaluate_rotation_gap(trajectory, reference)

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


if __name__ == "__main__":
    unittest.main()
