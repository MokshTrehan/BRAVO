#!/usr/bin/python3
"""Focused tests for the one-sequence KAIST-VIO campaign harness."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


sys.path.insert(0, str(Path(__file__).resolve().parent))
import kaist_vio_campaign as campaign  # noqa: E402


class KaistVioCampaignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.data = self.root / "KAIST_VIO"
        self.sequence = "rotation/rotation_fast.bag"
        self.source = self.data / "raw" / self.sequence
        self.adapted = self.data / "adapted" / self.sequence
        self.config = (
            self.repo
            / "config"
            / "kaist_vio_turnsafe_baseline"
            / "estimator_config.yaml"
        )
        self.launch = self.repo / "project" / "kaist_vio_serial.launch"
        self.binary = self.repo / "build" / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
        self.reference = self.repo / "ov_data" / "kaist_vio" / "rotation_fast.txt"
        self.adapter_script = self.repo / "scripts" / "turnsafe" / "kaist_vio_adapter.py"
        self.converter = self.repo / "scripts" / "cp0" / "openvins_to_tum.py"
        self.python = self.repo / "python3"
        for path in (
            self.source,
            self.config,
            self.config.parent / "kalibr_imu_chain.yaml",
            self.config.parent / "kalibr_imucam_chain.yaml",
            self.launch,
            self.binary,
            self.reference,
            self.adapter_script,
            self.converter,
            self.python,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            if path == self.reference:
                path.write_text(
                    "# tum\n1.0 0 0 0 0 0 0 1\n2.0 1 0 0 0 0 0 1\n",
                    encoding="utf-8",
                )
            else:
                path.write_text("fixture\n", encoding="utf-8")
        for path in (self.binary, self.python):
            path.chmod(0o755)

        self.patches = [
            mock.patch.object(campaign, "REPO_ROOT", self.repo),
            mock.patch.object(campaign, "DATA_ROOT", self.data),
            mock.patch.object(campaign, "ARTIFACT_ROOT", self.repo / "artifacts" / "turnsafe"),
            mock.patch.object(campaign, "ADAPTER_PATH", self.adapter_script),
            mock.patch.object(campaign, "CONVERTER_PATH", self.converter),
            mock.patch.object(campaign, "FIXED_LAUNCH", self.launch),
            mock.patch.object(campaign, "FIXED_CONFIG", self.config),
            mock.patch.object(campaign, "PYTHON", self.python),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temporary.cleanup()

    def arguments(self, *, prepare_only: bool = False) -> argparse.Namespace:
        return argparse.Namespace(
            sequence=self.sequence,
            source_bag=self.source,
            adapted_bag=self.adapted,
            config=self.config,
            launch=self.launch,
            binary=self.binary,
            reference_tum=self.reference,
            output_dir=self.repo / "artifacts" / "turnsafe" / "rotation_fast",
            ros_port=None if prepare_only else 15432,
            timeout_seconds=10.0,
            prepare_only=prepare_only,
        )

    @staticmethod
    def audits():
        stereo = {
            "exact_header_pair_count": 2,
            "camera0_unmatched_count": 1,
            "camera1_unmatched_count": 1,
            "record_skew_threshold_ns": 20_000_000,
            "matched_pair_record_skew_at_or_above_threshold_count": 1,
            "matched_pair_record_time_absolute_skew": {"max_ns": 53_519_649},
        }
        source = {
            "streams": {"/pose_transformed": {}, "camera": {}},
            "stereo": dict(stereo),
        }
        adapted = {"streams": {"camera": {}}, "stereo": dict(stereo)}
        return source, adapted

    def fake_adapter(self, operation, source, adapted):
        self.assertEqual(operation, "adapt")
        adapted.parent.mkdir(parents=True, exist_ok=True)
        adapted.write_bytes(b"adapted bag")
        source_audit, adapted_audit = self.audits()
        return (
            {
                "call": "kaist_vio_adapter.adapt_bag",
                "arguments": [str(source), str(adapted)],
                "ground_truth_policy": "source_required_and_audited_output_omitted",
            },
            source_audit,
            adapted_audit,
        )

    @staticmethod
    def command_record(argv, log_path, environment, timeout_seconds):
        return {
            "argv": list(argv),
            "shell": " ".join(argv),
            "cwd": str(campaign.REPO_ROOT),
            "environment": dict(environment),
            "exit_code": 0,
            "timed_out": False,
            "process_group_survived_cleanup": False,
            "error": None,
            "log": str(log_path),
            "timeout_seconds": timeout_seconds,
        }

    @staticmethod
    def write_metric(path: Path) -> None:
        header = repr(
            {
                "descr": "<f8",
                "fortran_order": False,
                "shape": (2,),
            }
        ).encode("latin1")
        padding = 16 - ((10 + len(header) + 1) % 16)
        header += b" " * padding + b"\n"
        npy = b"\x93NUMPY" + bytes((1, 0)) + struct.pack("<H", len(header)) + header
        npy += struct.pack("<dd", 0.1, 0.2)
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("stats.json", json.dumps({"rmse": 0.2, "mean": 0.15}))
            archive.writestr("info.json", json.dumps({"title": "fixture", "label": "x"}))
            archive.writestr("error_array.npy", npy)

    def test_full_command_construction_and_ground_truth_is_post_run_only(self) -> None:
        calls = []

        def fake_run(argv, log_path, environment, timeout_seconds):
            calls.append((list(argv), log_path))
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.name.endswith("rosbag_info.yaml"):
                log_path.write_text("path: fixture.bag\n", encoding="utf-8")
            elif log_path.name == "resolved_ros_parameters.yaml":
                log_path.write_text("/kaist/path_bag: adapted.bag\n", encoding="utf-8")
            elif log_path.name == "resolved_estimator_binary.txt":
                log_path.write_text(str(self.binary) + "\n", encoding="utf-8")
            elif log_path.name == "console.log":
                log_path.write_text(
                    "serial replay complete\n"
                    "[SERIAL-KAIST]: exact_header_pairs=2 "
                    "camera0_without_match=1 camera1_without_match=1 "
                    "record_delta_ge_20ms=1 maximum_record_delta_ns=53519649\n"
                    "[SERIAL-KAIST]: queued_pairs=1 processed_pairs=1 "
                    "frequency_thinned_pairs=1 cam0_decode_failures=0 "
                    "cam1_decode_failures=0 pending_pairs=0\n",
                    encoding="utf-8",
                )
                output = log_path.parent
                output.joinpath("state_estimate.txt").write_text(
                    "# timestamp(s) q p\n1 0 0 0 1 0 0 0\n2 0 0 0 1 1 0 0\n",
                    encoding="utf-8",
                )
                output.joinpath("state_deviation.txt").write_text(
                    "# std\n1 0.1\n2 0.1\n", encoding="utf-8"
                )
                output.joinpath("timing_openvins.csv").write_text(
                    "# timing\n1,0.1\n2,0.1\n", encoding="utf-8"
                )
            elif log_path.name == "trajectory_conversion.log":
                log_path.write_text("converted 2 poses\n", encoding="utf-8")
                log_path.parent.joinpath("trajectory_tum.txt").write_text(
                    "# tum\n1 0 0 0 0 0 0 1\n2 1 0 0 0 0 0 1\n",
                    encoding="utf-8",
                )
            else:
                log_path.write_text("rmse 0.2\n", encoding="utf-8")
                save_index = argv.index("--save_results") + 1
                self.write_metric(Path(argv[save_index]))
            record = self.command_record(argv, log_path, environment, timeout_seconds)
            record["log_sha256"] = hashlib.sha256(log_path.read_bytes()).hexdigest()
            return record

        tools = {
            "rosbag": self.repo / "bin" / "rosbag",
            "catkin_find": self.repo / "bin" / "catkin_find",
            "roslaunch": self.repo / "bin" / "roslaunch",
            "evo_ape": self.repo / "bin" / "evo_ape",
            "evo_rpe": self.repo / "bin" / "evo_rpe",
        }
        for path in tools.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("tool\n", encoding="utf-8")
            path.chmod(0o755)

        with mock.patch.object(campaign, "_command_path", side_effect=lambda name: tools[name]), mock.patch.object(
            campaign, "_adapter_call", side_effect=self.fake_adapter
        ), mock.patch.object(campaign, "_assert_port_available"), mock.patch.object(
            campaign, "run_command", side_effect=fake_run
        ):
            result = campaign.run_campaign(self.arguments())

        self.assertEqual(result["status"], "COMPLETED")
        launch_argv = result["commands"]["roslaunch"]["argv"]
        self.assertEqual(launch_argv[:3], [str(tools["roslaunch"]), "-p", "15432"])
        self.assertIn("bag:=" + str(self.adapted), launch_argv)
        self.assertIn("path_state:=" + str(result["outputs"]["state"]["path"]), launch_argv)
        self.assertNotIn(str(self.reference), launch_argv)
        self.assertNotIn("/pose_transformed", " ".join(launch_argv))
        self.assertIn(str(self.reference), result["commands"]["ape_translation"]["argv"])
        launch_position = next(i for i, call in enumerate(calls) if call[1].name == "console.log")
        evo_position = next(i for i, call in enumerate(calls) if call[1].name == "ape_translation.log")
        self.assertLess(launch_position, evo_position)
        self.assertEqual(result["metrics"]["ape_translation"]["samples"], 2)
        self.assertEqual(result["outputs"]["state"]["rows"], 2)
        self.assertTrue(
            result["output_consistency"]["state_deviation_timestamp_sequences_equal"]
        )
        self.assertTrue(
            result["output_consistency"]["state_trajectory_timestamp_sequences_equal"]
        )
        self.assertEqual(result["diagnostics"]["yaw"]["status"], "NOT_AVAILABLE")
        self.assertEqual(result["diagnostics"]["tilt"]["status"], "NOT_AVAILABLE")
        self.assertTrue(
            result["checks"]["exact_header_runtime_matches_adapted_audit"]
        )
        self.assertEqual(
            result["exact_header_stereo_runtime"]["observed"][
                "maximum_record_delta_ns"
            ],
            53_519_649,
        )
        self.assertTrue(
            result["checks"]["camera_enqueue_accounts_for_exact_header_pairs"]
        )
        self.assertTrue(result["checks"]["camera_decode_failures_zero"])
        self.assertTrue(
            result["checks"]["camera_processed_pairs_match_queued"]
        )
        self.assertTrue(result["checks"]["camera_queue_drained"])
        self.assertEqual(result["camera_enqueue_runtime"]["accounted_pairs"], 2)

    def test_prepare_only_adapts_and_never_constructs_estimator_or_evo_command(self) -> None:
        rosbag = self.repo / "bin" / "rosbag"
        rosbag.parent.mkdir(parents=True, exist_ok=True)
        rosbag.write_text("tool\n", encoding="utf-8")
        rosbag.chmod(0o755)
        calls = []

        def fake_run(argv, log_path, environment, timeout_seconds):
            calls.append(list(argv))
            log_path.write_text("path: fixture.bag\n", encoding="utf-8")
            return self.command_record(argv, log_path, environment, timeout_seconds)

        with mock.patch.object(campaign, "_command_path", return_value=rosbag), mock.patch.object(
            campaign, "_adapter_call", side_effect=self.fake_adapter
        ), mock.patch.object(campaign, "run_command", side_effect=fake_run):
            result = campaign.run_campaign(self.arguments(prepare_only=True))

        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(command[1:3] == ["info", "--yaml"] for command in calls))
        self.assertNotIn("roslaunch", result["commands"])
        self.assertNotIn("metrics", result)

    def test_existing_output_directory_is_refused(self) -> None:
        args = self.arguments(prepare_only=True)
        args.output_dir.mkdir(parents=True)
        with self.assertRaisesRegex(campaign.CampaignError, "refusing to overwrite"):
            campaign.run_campaign(args)

    def test_adapted_bag_must_end_with_exact_sequence_components(self) -> None:
        args = self.arguments(prepare_only=True)
        args.adapted_bag = self.data / "adapted" / "wrong" / "rotation_fast.bag"
        with self.assertRaisesRegex(campaign.CampaignError, "suffix does not match"):
            campaign.run_campaign(args)

    def test_serial_unable_to_find_stereo_pair_warning_is_counted(self) -> None:
        console = self.root / "console.log"
        console.write_text(
            "[WARN] Unable to find stereo pair for timestamp 1.25\n",
            encoding="utf-8",
        )
        diagnostics = campaign._console_diagnostics(console)
        self.assertEqual(diagnostics["counts"]["unpaired_warning_lines"], 1)
        self.assertIn(
            "Unable to find stereo pair",
            diagnostics["samples"]["unpaired_warning_lines"][0],
        )

    def test_exact_header_summary_binds_to_audit_without_warning_counts(self) -> None:
        console = self.root / "console.log"
        line = (
            "[SERIAL-KAIST]: exact_header_pairs=2 "
            "camera0_without_match=1 camera1_without_match=1 "
            "record_delta_ge_20ms=1 maximum_record_delta_ns=53519649"
        )
        console.write_text(line + "\n", encoding="utf-8")
        _, adapted = self.audits()

        binding = campaign._bind_exact_header_runtime_summary(console, adapted)
        diagnostics = campaign._console_diagnostics(console)

        self.assertEqual(binding["line"], line)
        self.assertTrue(binding["matches_adapted_audit"])
        self.assertEqual(binding["observed"], binding["expected_from_adapted_audit"])
        self.assertEqual(diagnostics["counts"]["unpaired_warning_lines"], 0)
        self.assertEqual(diagnostics["counts"]["dropped_message_lines"], 0)

    def test_exact_header_summary_accepts_ros_console_ansi_prefix(self) -> None:
        console = self.root / "console.log"
        console.write_text(
            "\x1b[0m[SERIAL-KAIST]: exact_header_pairs=2 "
            "camera0_without_match=1 camera1_without_match=1 "
            "record_delta_ge_20ms=1 maximum_record_delta_ns=53519649\n",
            encoding="utf-8",
        )
        _, adapted = self.audits()
        binding = campaign._bind_exact_header_runtime_summary(
            console, adapted
        )
        self.assertEqual(binding["observed"]["exact_header_pairs"], 2)

    def test_exact_header_summary_requires_exactly_one_well_formed_line(self) -> None:
        console = self.root / "console.log"
        valid = (
            "[SERIAL-KAIST]: exact_header_pairs=2 "
            "camera0_without_match=1 camera1_without_match=1 "
            "record_delta_ge_20ms=1 maximum_record_delta_ns=53519649"
        )
        _, adapted = self.audits()
        cases = {
            "missing": "serial replay complete\n",
            "duplicate": valid + "\n" + valid + "\n",
            "malformed": valid + " trailing text\n",
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                console.write_text(contents, encoding="utf-8")
                with self.assertRaises(campaign.CampaignError):
                    campaign._bind_exact_header_runtime_summary(console, adapted)

    def test_exact_header_summary_requires_equality_for_every_audit_field(self) -> None:
        console = self.root / "console.log"
        values = {
            "exact_header_pairs": 2,
            "camera0_without_match": 1,
            "camera1_without_match": 1,
            "record_delta_ge_20ms": 1,
            "maximum_record_delta_ns": 53_519_649,
        }
        template = (
            "[SERIAL-KAIST]: exact_header_pairs={exact_header_pairs} "
            "camera0_without_match={camera0_without_match} "
            "camera1_without_match={camera1_without_match} "
            "record_delta_ge_20ms={record_delta_ge_20ms} "
            "maximum_record_delta_ns={maximum_record_delta_ns}\n"
        )
        _, adapted = self.audits()
        for field in values:
            with self.subTest(field=field):
                observed = dict(values)
                observed[field] += 1
                console.write_text(template.format(**observed), encoding="utf-8")
                with self.assertRaisesRegex(campaign.CampaignError, "disagrees"):
                    campaign._bind_exact_header_runtime_summary(console, adapted)

    def test_camera_enqueue_summary_accounts_for_pairs_without_drop_diagnostic(self) -> None:
        console = self.root / "console.log"
        line = (
            "[SERIAL-KAIST]: queued_pairs=1 processed_pairs=1 "
            "frequency_thinned_pairs=1 cam0_decode_failures=0 "
            "cam1_decode_failures=0 pending_pairs=0"
        )
        console.write_text(line + "\n", encoding="utf-8")

        binding = campaign._bind_camera_enqueue_runtime_summary(console, 2)
        diagnostics = campaign._console_diagnostics(console)

        self.assertEqual(binding["line"], line)
        self.assertTrue(binding["pair_accounting_complete"])
        self.assertTrue(binding["decode_failures_zero"])
        self.assertTrue(binding["processed_pairs_match_queued"])
        self.assertTrue(binding["queue_drained"])
        self.assertEqual(
            binding["frequency_thinning_policy"],
            "existing_frozen_baseline_camera_frequency_policy",
        )
        self.assertEqual(diagnostics["counts"]["unpaired_warning_lines"], 0)
        self.assertEqual(diagnostics["counts"]["dropped_message_lines"], 0)

    def test_camera_enqueue_summary_requires_exactly_one_line(self) -> None:
        console = self.root / "console.log"
        valid = (
            "[SERIAL-KAIST]: queued_pairs=1 processed_pairs=1 "
            "frequency_thinned_pairs=1 cam0_decode_failures=0 "
            "cam1_decode_failures=0 pending_pairs=0"
        )
        cases = {
            "missing": "serial replay complete\n",
            "duplicate": valid + "\n" + valid + "\n",
        }
        for name, contents in cases.items():
            with self.subTest(name=name):
                console.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(campaign.CampaignError, "exactly one"):
                    campaign._bind_camera_enqueue_runtime_summary(console, 2)

    def test_camera_enqueue_summary_rejects_pair_accounting_mismatch(self) -> None:
        console = self.root / "console.log"
        console.write_text(
            "[SERIAL-KAIST]: queued_pairs=1 processed_pairs=1 "
            "frequency_thinned_pairs=0 cam0_decode_failures=0 "
            "cam1_decode_failures=0 pending_pairs=0\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "accounting mismatch"):
            campaign._bind_camera_enqueue_runtime_summary(console, 2)

    def test_camera_enqueue_summary_rejects_nonzero_decode_failures(self) -> None:
        console = self.root / "console.log"
        for field in ("cam0_decode_failures", "cam1_decode_failures"):
            with self.subTest(field=field):
                values = {
                    "queued_pairs": 1,
                    "processed_pairs": 1,
                    "frequency_thinned_pairs": 1,
                    "cam0_decode_failures": 0,
                    "cam1_decode_failures": 0,
                    "pending_pairs": 0,
                }
                values[field] = 1
                console.write_text(
                    "[SERIAL-KAIST]: queued_pairs={queued_pairs} "
                    "processed_pairs={processed_pairs} "
                    "frequency_thinned_pairs={frequency_thinned_pairs} "
                    "cam0_decode_failures={cam0_decode_failures} "
                    "cam1_decode_failures={cam1_decode_failures} "
                    "pending_pairs={pending_pairs}\n".format(**values),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(campaign.CampaignError, "decode failure"):
                    campaign._bind_camera_enqueue_runtime_summary(console, 2)

    def test_camera_enqueue_summary_rejects_processing_or_drain_mismatch(self) -> None:
        console = self.root / "console.log"
        cases = {
            "processed": (
                "[SERIAL-KAIST]: queued_pairs=2 processed_pairs=1 "
                "frequency_thinned_pairs=0 cam0_decode_failures=0 "
                "cam1_decode_failures=0 pending_pairs=0"
            ),
            "pending": (
                "[SERIAL-KAIST]: queued_pairs=2 processed_pairs=2 "
                "frequency_thinned_pairs=0 cam0_decode_failures=0 "
                "cam1_decode_failures=0 pending_pairs=1"
            ),
        }
        for name, line in cases.items():
            with self.subTest(name=name):
                console.write_text(line + "\n", encoding="utf-8")
                with self.assertRaises(campaign.CampaignError):
                    campaign._bind_camera_enqueue_runtime_summary(console, 2)

    def test_output_consistency_requires_equal_sequences_rows_and_tolerance(self) -> None:
        state = self.root / "state.txt"
        deviation = self.root / "deviation.txt"
        timing = self.root / "timing.csv"
        trajectory = self.root / "trajectory.txt"
        state.write_text("1.0 0\n2.0 0\n", encoding="utf-8")
        deviation.write_text("1.0 0\n2.0 0\n", encoding="utf-8")
        timing.write_text("1.000001,0\n2.000001,0\n", encoding="utf-8")
        trajectory.write_text("1.0 0\n2.0 0\n", encoding="utf-8")

        report = campaign._validate_output_consistency(
            state, deviation, timing, trajectory
        )
        self.assertTrue(report["callback_timing_row_counts_equal"])
        self.assertLessEqual(
            report["timing_state_maximum_absolute_difference_seconds"], 1e-5
        )

        timing.write_text("1.00002,0\n2.00002,0\n", encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "exceeds 1e-5"):
            campaign._validate_output_consistency(state, deviation, timing, trajectory)

    def test_reference_contents_are_not_parsed_before_estimator_finishes(self) -> None:
        self.reference.write_text("not a trajectory\n", encoding="utf-8")
        args = self.arguments()
        args.output_dir = self.repo / "artifacts" / "turnsafe" / "reference-order"
        tools = {
            name: self.repo / "bin" / name
            for name in ("rosbag", "catkin_find", "roslaunch", "evo_ape", "evo_rpe")
        }
        for path in tools.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("tool\n", encoding="utf-8")
            path.chmod(0o755)
        estimator_was_started = []

        def fake_run(argv, log_path, environment, timeout_seconds):
            if log_path.name.endswith("rosbag_info.yaml"):
                log_path.write_text("path: fixture.bag\n", encoding="utf-8")
            elif log_path.name == "resolved_ros_parameters.yaml":
                log_path.write_text("/kaist/path_bag: adapted.bag\n", encoding="utf-8")
            elif log_path.name == "resolved_estimator_binary.txt":
                log_path.write_text(str(self.binary) + "\n", encoding="utf-8")
            else:
                self.assertEqual(log_path.name, "console.log")
                estimator_was_started.append(True)
                log_path.write_text("estimator failed\n", encoding="utf-8")
            record = self.command_record(argv, log_path, environment, timeout_seconds)
            if log_path.name == "console.log":
                record["exit_code"] = 1
            return record

        with mock.patch.object(campaign, "_command_path", side_effect=lambda name: tools[name]), mock.patch.object(
            campaign, "_adapter_call", side_effect=self.fake_adapter
        ), mock.patch.object(campaign, "_assert_port_available"), mock.patch.object(
            campaign, "run_command", side_effect=fake_run
        ):
            with self.assertRaisesRegex(campaign.CampaignError, "roslaunch did not complete"):
                campaign.run_campaign(args)

        self.assertEqual(estimator_was_started, [True])
        failure = json.loads(
            args.output_dir.joinpath("sequence_result.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("reference_validation", failure)

    def test_timeout_sends_process_group_signal_and_records_cleanup(self) -> None:
        log = self.root / "timeout.log"
        process = mock.Mock()
        process.pid = 43210
        process.wait.side_effect = [
            subprocess.TimeoutExpired(["fixture"], 0.01),
            -2,
        ]
        process.poll.return_value = -2
        group_states = iter([True, False, False])
        with mock.patch.object(subprocess, "Popen", return_value=process), mock.patch.object(
            campaign, "_process_group_exists", side_effect=lambda pid: next(group_states)
        ), mock.patch.object(os, "killpg") as killpg:
            record = campaign.run_command(["fixture"], log, {}, 0.01)

        self.assertTrue(record["timed_out"])
        self.assertEqual(record["exit_code"], -2)
        self.assertEqual(record["signals_sent"], ["SIGINT"])
        self.assertFalse(record["process_group_survived_cleanup"])
        killpg.assert_called_once_with(43210, signal.SIGINT)

    def test_private_or_holdout_component_is_rejected_before_inspection(self) -> None:
        args = self.arguments(prepare_only=True)
        args.source_bag = self.data / "PRIVATE" / self.sequence
        with self.assertRaisesRegex(campaign.CampaignError, "forbidden"):
            campaign.run_campaign(args)


if __name__ == "__main__":
    unittest.main()
