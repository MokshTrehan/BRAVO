#!/usr/bin/python3
"""Focused tests for the one-sequence KAIST-VIO campaign harness."""

import argparse
import csv
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
import event_ready_association as association  # noqa: E402


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
        self.adapted.parent.mkdir(parents=True, exist_ok=True)
        self.adapted.write_bytes(b"adapted bag")
        self.frozen_baseline = (
            self.repo / "artifacts" / "turnsafe" / "data" /
            "KAIST_BASELINE_RESULTS.csv"
        )
        self.frozen_baseline.parent.mkdir(parents=True, exist_ok=True)
        frozen_fields = [
            "sequence",
            "source_bag_sha256",
            "adapted_bag_sha256",
            "config_sha256",
            "kalibr_imu_chain_sha256",
            "kalibr_imucam_chain_sha256",
            "reference_sha256",
        ]
        with self.frozen_baseline.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=frozen_fields)
            writer.writeheader()
            writer.writerow(
                {
                    "sequence": self.sequence,
                    "source_bag_sha256": campaign.sha256_file(self.source),
                    "adapted_bag_sha256": campaign.sha256_file(self.adapted),
                    "config_sha256": campaign.sha256_file(self.config),
                    "kalibr_imu_chain_sha256": campaign.sha256_file(
                        self.config.parent / "kalibr_imu_chain.yaml"
                    ),
                    "kalibr_imucam_chain_sha256": campaign.sha256_file(
                        self.config.parent / "kalibr_imucam_chain.yaml"
                    ),
                    "reference_sha256": campaign.sha256_file(self.reference),
                }
            )
        self.frozen_baseline_sha256 = campaign.sha256_file(
            self.frozen_baseline
        )

        self.patches = [
            mock.patch.object(campaign, "REPO_ROOT", self.repo),
            mock.patch.object(campaign, "DATA_ROOT", self.data),
            mock.patch.object(campaign, "ARTIFACT_ROOT", self.repo / "artifacts" / "turnsafe"),
            mock.patch.object(campaign, "ADAPTER_PATH", self.adapter_script),
            mock.patch.object(campaign, "CONVERTER_PATH", self.converter),
            mock.patch.object(campaign, "FIXED_LAUNCH", self.launch),
            mock.patch.object(campaign, "FIXED_CONFIG", self.config),
            mock.patch.object(
                campaign, "FROZEN_BASELINE_RESULTS", self.frozen_baseline
            ),
            mock.patch.object(
                campaign,
                "FROZEN_BASELINE_RESULTS_SHA256",
                self.frozen_baseline_sha256,
            ),
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
            turnsafe_t0_capture=False,
            turnsafe_t0_capture_causal_imu_intervals=False,
            turnsafe_t0_capture_outcome_association_keys=False,
            turnsafe_t0_capture_group_bearing_provenance=False,
            turnsafe_t0_provenance=False,
            turnsafe_t0_frozen_base_sha="",
            turnsafe_t0_source_sha="",
            turnsafe_t0_source_tree="",
            turnsafe_t0_build_provenance_id="",
            turnsafe_t0_expected_source_state=None,
            turnsafe_t0_repository=None,
            turnsafe_t0_build_manifest=None,
            turnsafe_t0_schema_file=None,
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
        self.assertIn(operation, ("adapt", "audit"))
        adapted.parent.mkdir(parents=True, exist_ok=True)
        if operation == "adapt":
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
        # This test isolates the runtime ordering contract. Rebind the trusted
        # fixture ledger to the deliberately invalid reference bytes so the
        # independent frozen-input check passes without parsing the reference.
        rows = []
        with self.frozen_baseline.open(
            "r", encoding="utf-8", newline=""
        ) as stream:
            reader = csv.DictReader(stream)
            fieldnames = reader.fieldnames
            rows = list(reader)
        self.assertIsNotNone(fieldnames)
        rows[0]["reference_sha256"] = campaign.sha256_file(self.reference)
        with self.frozen_baseline.open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        rebound_ledger_sha256 = campaign.sha256_file(self.frozen_baseline)
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

        with mock.patch.object(
            campaign,
            "FROZEN_BASELINE_RESULTS_SHA256",
            rebound_ledger_sha256,
        ), mock.patch.object(campaign, "_command_path", side_effect=lambda name: tools[name]), mock.patch.object(
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

    def test_frozen_inputs_reject_pre_run_bag_config_and_calibration_drift(self) -> None:
        cases = (
            self.source,
            self.adapted,
            self.config,
            self.config.parent / "kalibr_imu_chain.yaml",
            self.config.parent / "kalibr_imucam_chain.yaml",
        )
        for index, path in enumerate(cases):
            with self.subTest(path=path):
                original = path.read_bytes()
                path.write_bytes(original + b"drift")
                args = self.arguments(prepare_only=True)
                args.output_dir = (
                    self.repo / "artifacts" / "turnsafe" /
                    "frozen-drift-{}".format(index)
                )
                try:
                    with self.assertRaisesRegex(
                        campaign.CampaignError,
                        "differs from frozen Session-0.5 input",
                    ):
                        campaign.run_campaign(args)
                finally:
                    path.write_bytes(original)

    def test_frozen_baseline_ledger_hash_is_independently_pinned(self) -> None:
        self.frozen_baseline.write_bytes(
            self.frozen_baseline.read_bytes() + b"drift"
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "frozen Session-0.5 baseline result hash mismatch",
        ):
            campaign.run_campaign(self.arguments(prepare_only=True))

    def test_capture_off_provenance_requires_expected_source_state(self) -> None:
        args = self.arguments()
        args.turnsafe_t0_provenance = True
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "T0 provenance requires --turnsafe-t0-expected-source-state",
        ):
            campaign.run_campaign(args)

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

    def test_stable_terminal_reason_set_matches_normative_schema(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[2] / "docs" / "turnsafe" /
            "t0_schema.md"
        )
        schema = schema_path.read_text(encoding="utf-8")
        section = schema.split(
            "## 10. Stable reason codes and ordering", 1
        )[1].split("## 11. Deterministic digest membership", 1)[0]
        fenced = section.split("```text", 1)[1].split("```", 1)[0]
        normative_order = tuple(
            line.strip() for line in fenced.splitlines() if line.strip()
        )
        self.assertEqual(len(normative_order), len(set(normative_order)))
        self.assertEqual(
            frozenset(normative_order), campaign.STABLE_TERMINAL_REASONS
        )
        self.assertEqual(
            normative_order, campaign.STABLE_TERMINAL_REASON_ORDER
        )
        range_index = normative_order.index("RANGE_GEOMETRY_INVALID")
        self.assertEqual(
            normative_order[range_index:range_index + 4],
            (
                "RANGE_GEOMETRY_INVALID",
                "RANGE_COVARIANCE_INVALID",
                "RANGE_LCB_UNAVAILABLE",
                "RANGE_LCB_NONPOSITIVE",
            ),
        )

    def test_stable_terminal_reason_membership_and_stage(self) -> None:
        for reason in campaign.STABLE_TERMINAL_REASONS:
            self.assertEqual(
                campaign.validate_stable_terminal_reason(reason, "fixture"),
                reason,
            )
        self.assertEqual(
            campaign.validate_stable_terminal_reason(
                "RANGE_LCB_UNAVAILABLE", "fixture", stage="candidate"
            ),
            "RANGE_LCB_UNAVAILABLE",
        )
        for invalid in (None, 7, "", "FUTURE_UNKNOWN_REASON"):
            with self.subTest(invalid=invalid), self.assertRaises(
                campaign.CampaignError
            ):
                campaign.validate_stable_terminal_reason(invalid, "fixture")
        with self.assertRaisesRegex(campaign.CampaignError, "wrong stage"):
            campaign.validate_stable_terminal_reason(
                "CONSENSUS_FAILED", "fixture", stage="candidate"
            )
        self.assertEqual(
            campaign.validate_stable_terminal_reason(
                "CONSENSUS_FAILED", "fixture", stage="group_consensus"
            ),
            "CONSENSUS_FAILED",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "wrong stage"):
            campaign.validate_stable_terminal_reason(
                "RANGE_LCB_UNAVAILABLE", "fixture", stage="group_consensus"
            )
        self.assertEqual(
            campaign.validate_stable_terminal_reason(
                "LOWER_PRE_NIS_SCORE", "fixture", stage="group_foregone"
            ),
            "LOWER_PRE_NIS_SCORE",
        )

    def test_strict_json_rejects_duplicate_keys_and_nonfinite_values(self) -> None:
        with self.assertRaisesRegex(campaign.CampaignError, "duplicate JSON key"):
            campaign._strict_json('{"value":1,"value":2}', "fixture")
        with self.assertRaisesRegex(campaign.CampaignError, "nonfinite JSON token"):
            campaign._strict_json('{"value":NaN}', "fixture")
        with self.assertRaisesRegex(campaign.CampaignError, "nonfinite number"):
            campaign._strict_json('{"value":1e400}', "fixture")

    def test_event_extension_flags_require_active_passive_capture(self) -> None:
        flag_names = (
            "turnsafe_t0_capture_causal_imu_intervals",
            "turnsafe_t0_capture_outcome_association_keys",
            "turnsafe_t0_capture_group_bearing_provenance",
        )
        for flag_name in flag_names:
            arguments = self.arguments(prepare_only=True)
            setattr(arguments, flag_name, True)
            with self.subTest(flag=flag_name), self.assertRaisesRegex(
                campaign.CampaignError, "requires --turnsafe-t0-capture"
            ):
                campaign.run_campaign(arguments)
            self.assertFalse(arguments.output_dir.exists())

    def test_post_close_association_outputs_bind_every_input_and_callback(self) -> None:
        telemetry = self.root / "t0_events.jsonl"
        telemetry.write_text(
            json.dumps({
                "schema": "turnsafe.t0.v1", "record_type": "run_header"
            }) + "\n" +
            json.dumps({
                "schema": "turnsafe.t0.v1", "record_type": "callback",
                "callback_index": 0, "callback_timestamp_value": 1.0,
                "callback_timestamp_key": "f64:0x3ff0000000000000",
                "extensions": {"event_extension": {
                    "schema": campaign.T0_EVENT_EXTENSION_SCHEMA,
                    "record_version": 1,
                    "outcome_association_keys": {
                        "callback_id": 0,
                        "callback_camera_timestamp_value": 1.0,
                        "callback_camera_timestamp_key": (
                            "f64:0x3ff0000000000000"),
                        "estimator_initialized_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "estimator_initialized_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "estimator_valid_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "estimator_valid_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "state_timestamp_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": 1.0},
                        "state_timestamp_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": 1.0},
                        "expected_state_row_key": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "stream_id": "state_estimate",
                            "timestamp_value": 1.0,
                            "timestamp_key": "f64:0x3ff0000000000000",
                            "row_ordinal": {
                                "status": "NOT_AVAILABLE",
                                "reason": "ROW_ORDINAL_OFFLINE_ONLY"}},
                        "expected_deviation_row_key": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "stream_id": "state_deviation",
                            "timestamp_value": 1.0,
                            "timestamp_key": "f64:0x3ff0000000000000",
                            "row_ordinal": {
                                "status": "NOT_AVAILABLE",
                                "reason": "ROW_ORDINAL_OFFLINE_ONLY"}},
                        "expected_pose_row_key": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "stream_id": "trajectory_tum",
                            "timestamp_value": 1.0,
                            "timestamp_key": "f64:0x3ff0000000000000",
                            "row_ordinal": {
                                "status": "NOT_AVAILABLE",
                                "reason": "ROW_ORDINAL_OFFLINE_ONLY"}},
                        "pose_stream_write_status": {
                            "status": "NOT_EXPOSED",
                            "reason": "POSE_WRITE_STATUS_OFFLINE_ONLY"},
                        "reset_status": {
                            "status": "NOT_EXPOSED",
                            "reason": (
                                "RESET_STATUS_NOT_EXPOSED_BY_NATIVE_PATH")},
                        "nonfinite_observed": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": False},
                        "callback_incomplete": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": False,
                            "completion_reason": "NORMAL_SCOPE_EXIT"},
                        "run_completeness": {
                            "status": "NOT_AVAILABLE",
                            "reason": "RUN_COMPLETENESS_POST_CLOSE_ONLY"},
                        "ordinary_accepted_full_factor_count": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": 0},
                        "ordinary_full_visual_update_accepted": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": False},
                        "time_since_last_accepted_ordinary_full_update_camera_s": {
                            "status": "NOT_AVAILABLE",
                            "reason": "NO_PRIOR_ACCEPTED_FULL_UPDATE"},
                        "reference_association": {
                            "status": "NOT_AVAILABLE",
                            "reason": "REFERENCE_ASSOCIATION_OFFLINE_ONLY"},
                        "identity": {
                            "run_identity": "opaque-fixture",
                            "source_sha": "a" * 40,
                            "source_tree": "b" * 40,
                            "source_snapshot_sha256": "c" * 64,
                            "build_provenance_id": "d" * 64,
                            "config_sha256": "e" * 64,
                            "calibration_sha256": "f" * 64}
                    },
                }},
            }) + "\n",
            encoding="utf-8",
        )
        state = self.root / "state.txt"
        deviation = self.root / "deviation.txt"
        trajectory = self.root / "trajectory.txt"
        reference = self.root / "reference.txt"
        for path in (state, deviation, trajectory, reference):
            path.write_text("1 0\n", encoding="utf-8")
        output = self.root / "association"
        association.associate(
            telemetry, state, deviation, trajectory, reference, output
        )
        binding = campaign._validate_association_outputs(
            output, 1, telemetry, state, deviation, trajectory, reference
        )
        self.assertEqual(binding["callback_count"], 1)
        self.assertEqual(binding["coverage"]["streams"]["reference"]["counts"], {
            "MATCHED": 1, "MISSING": 0, "AMBIGUOUS": 0,
        })
        coverage_path = output / "ASSOCIATION_COVERAGE.json"
        coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
        coverage["callback_count"] = 2
        coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "does not reconcile"):
            campaign._validate_association_outputs(
                output, 1, telemetry, state, deviation, trajectory, reference
            )

    def test_lossless_compression_roundtrip_and_failure_preserves_raw(self) -> None:
        gzip = Path("/usr/bin/gzip")
        if not gzip.is_file():
            self.skipTest("/usr/bin/gzip is unavailable")
        environment = dict(os.environ)
        first_dir = self.root / "compress-success"
        first_dir.mkdir()
        raw = first_dir / "t0_events.jsonl"
        payload = (b'{"record_type":"callback"}\n' * 1000)
        raw.write_bytes(payload)
        result = campaign._compress_verified_jsonl(
            raw, gzip, environment, 30.0
        )
        self.assertFalse(raw.exists())
        self.assertTrue(Path(result["archive"]["path"]).is_file())
        self.assertEqual(result["roundtrip"]["sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["roundtrip"]["size_bytes"], len(payload))

        second_dir = self.root / "compress-failure"
        second_dir.mkdir()
        retained = second_dir / "t0_events.jsonl"
        retained.write_bytes(payload)
        with mock.patch.object(
            campaign,
            "_stream_decompressed_identity",
            return_value={"sha256": "0" * 64, "size_bytes": len(payload)},
        ), self.assertRaisesRegex(campaign.CampaignError, "differs; raw retained"):
            campaign._compress_verified_jsonl(
                retained, gzip, environment, 30.0
            )
        self.assertEqual(retained.read_bytes(), payload)

    def test_t0_jsonl_validator_binds_header_order_and_runtime_firewall(self) -> None:
        path = self.root / "t0.jsonl"
        expected = {
            "frozen_base_sha": "a" * 40,
            "source_sha": "b" * 40,
            "tree_sha": "c" * 40,
            "build_manifest_sha256": "d" * 64,
            "binary_sha256": "e" * 64,
            "config_sha256": "f" * 64,
            "calibration_sha256": "1" * 64,
            "diagnostic_schema_sha256": "2" * 64,
        }
        header = dict(expected)
        header.update({
            "schema": campaign.T0_SCHEMA,
            "record_type": "run_header",
            "supported_configuration": True,
            "unsupported_reasons": [],
            "resolved_configuration": {
                "one_pass_schur": True,
                "fej_enabled": True,
                "global_3d_transient": True,
                "all_cameras_radtan": True,
                "camera_extrinsic_calibration_off": True,
                "camera_intrinsic_calibration_off": True,
                "camera_time_offset_calibration_off": True,
                "stereo_enabled": True,
                "stereo_available": True,
                "require_target_stereo_range": True,
                "camera_count": 2,
            },
        })
        frontend = {
            "camera_id": 0,
            "source_camera_timestamp": {
                "status": "NOT_APPLICABLE",
                "reason": "INITIAL_FRAME",
            },
            "frame_timestamp_value": 1.0,
            "frame_timestamp_key": "f64:0x3ff0000000000000",
            "reseed_count": 0,
            "klt_attempted_count": 0,
            "klt_counts_available": True,
            "previous_tracked_points": 0,
            "klt_status_survivors": 0,
            "klt_status_rejections": 0,
            "out_of_bounds_rejections": 0,
            "in_bounds_survivors": 0,
            "mask_rejections": 0,
            "mask_survivors": 0,
            "mask_stage_present": True,
            "fmatrix_input_points": 0,
            "fmatrix_inliers": 0,
            "fmatrix_rejections": 0,
            "native_combined_track_survivors": 0,
            "database_observations_written": 0,
            "database_tracked_observations_written": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "database_new_observations_written": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "reset_too_few_points": False,
            "reset_native_reason": "NONE",
            "forward_backward_check": {
                "status": "NOT_APPLICABLE",
                "reason": "NATIVE_KLT_HAS_NO_FORWARD_BACKWARD_CHECK",
            },
            "klt_error_summary": {"count": 0, "nonfinite_count": 0},
            "gyro_magnitude_rad_s": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "gyro_integrated_rotation_rad": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "blur_metric": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "exposure": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "gain": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "native_accepted_feature_ids": [],
        }
        funnel_fields = (
            "camera_frames",
            "previous_tracked_points",
            "klt_status_survivors",
            "in_bounds_survivors",
            "mask_survivors",
            "native_combined_track_survivors",
            "fmatrix_input_points",
            "fmatrix_survivors",
            "database_observations_written",
            "terminal_attempt_count",
            "tracks_with_valid_clone_pairs",
            "attempt_valid_clone_pair_count_total",
            "accepted_full_factors",
            "accepted_full_rows",
            "shadow_pair_candidates",
            "candidate_pairs_with_target_stereo",
            "attempts_with_at_least_one_target_stereo_pair",
            "groups_with_at_least_one_target_stereo_pair",
            "candidates_with_calibrated_range_lcb",
            "candidates_with_calibrated_translation_ucb",
            "candidates_translation_acute",
            "candidates_passing_rho_trans",
            "groups_n_lt_2",
            "groups_n_2",
            "groups_n_3",
            "groups_n_ge_4",
            "groups_passing_rank",
            "groups_passing_consensus",
            "eligible_groups_before_selection",
            "winner_count",
            "foregone_eligible_groups",
            "foregone_eligible_features",
            "winner_shadow_factors_passing_nis",
            "winner_shadow_rows_passing_nis",
        )
        funnel = dict((field, 0) for field in funnel_fields)
        funnel["camera_frames"] = 2
        for field in (
            "full_outcome_counts_by_code",
            "triangulation_outcome_counts_by_code",
            "refinement_outcome_counts_by_code",
            "schur_outcome_counts_by_code",
            "full_nis_outcome_counts_by_code",
        ):
            funnel[field] = {}
        callback = {
            "schema": campaign.T0_SCHEMA,
            "record_type": "callback",
            "callback_index": 0,
            "callback_timestamp_value": 1.0,
            "callback_timestamp_key": "f64:0x3ff0000000000000",
            "prior_fingerprint": {
                "status": "NOT_EXPOSED",
                "reason": "UPDATER_PRIOR_NOT_CAPTURED",
            },
            "frontend_cameras": [dict(frontend), dict(frontend, camera_id=1)],
            "full_track_attempts": [],
            "baseline_decision": {
                "native_status": "all_rejected",
                "native_subreason": "NONE",
                "accepted_full_feature_ids": [],
                "proposal_sufficient_statistics": {
                    "gamma": {
                        "status": "NOT_EXPOSED",
                        "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
                    },
                    "precompression_rows": {
                        "status": "NOT_EXPOSED",
                        "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
                    },
                    "compressed_rows": {
                        "status": "NOT_EXPOSED",
                        "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
                    },
                },
                "proposal_attempted": False,
                "proposal_accepted": False,
                "baseline_commit_occurred": False,
                "no_full_visual_update_duration": {
                    "status": "NOT_AVAILABLE",
                    "reason": "NO_PRIOR_ACCEPTED_FULL_UPDATE",
                },
                "mean_commit_count": 0,
                "covariance_commit_count": 0,
                "terminal_finalization_count": 1,
                "nominal_state_fingerprint": {
                    "status": "NOT_EXPOSED",
                    "reason": "FILE_BOUND_PARITY_USED",
                },
                "covariance_fingerprint": {
                    "status": "NOT_EXPOSED",
                    "reason": "FILE_BOUND_PARITY_USED",
                },
            },
            "shadow_candidates": [],
            "shadow_groups": [],
            "prior_primitives": {
                "status": "NOT_EXPOSED",
                "reason": "UPDATER_PRIOR_NOT_CAPTURED",
            },
            "funnel": funnel,
            "shadow_summary": {
                "eligible_group_count": 0,
                "frozen_winner": {},
                "runner_up": {},
                "foregone_eligible_groups": 0,
                "foregone_eligible_features": 0,
                "no_runner_up_gate_shopping": True,
                "translation_covariance_scale": 2.0,
            },
            "completeness": {
                "status": "PARTIAL_SHADOW_BY_CONTRACT",
                "typed_reasons": [],
            },
        }
        path.write_text(
            json.dumps(header, sort_keys=True) + "\n" +
            json.dumps(callback, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        result = campaign._validate_t0_jsonl(path, expected)
        self.assertEqual(result["callback_count"], 1)
        self.assertTrue(result["runtime_sequence_identity_absent"])

        event_header = {
            "schema": campaign.T0_EVENT_EXTENSION_SCHEMA,
            "record_version": 1,
            "capture_flags": {
                "causal_imu_intervals": True,
                "outcome_association_keys": True,
                "group_bearing_provenance": True,
            },
            "integration_method": "piecewise_linear_trapezoid.v1",
            "rotation_convention": (
                "native_gyroscope_frame_passive_left_exp_minus_"
                "omega_diagnostic.v1"),
            "gyro_bias_convention": "raw_wm_minus_current_callback_bias_g.v1",
            "clock_mapping": "t_imu_equals_t_camera_plus_dt_CAMtoIMU.v1",
            "association_version": "turnsafe.offline_association.v1",
            "canonical_ordering_version": "turnsafe.event_extension.order.v1",
            "image_cell_representation": "continuous_pixel_center_fraction.v1",
            "group_hash_algorithm": "fnv1a64_binary64_be.v1",
        }
        expected["extensions"] = {"event_extension": event_header}
        header["extensions"] = {"event_extension": event_header}
        for frame in callback["frontend_cameras"]:
            frame["extensions"] = {
                "event_extension": {
                    "schema": campaign.T0_EVENT_EXTENSION_SCHEMA,
                    "gyro_interval_id": 0,
                }
            }
        member_keys = [
            {
                "camera_id": 0,
                "source_timestamp_key": "f64:0x3fe0000000000000",
                "target_timestamp_key": "f64:0x3ff0000000000000",
                "feature_id": feature_id,
                "detached_index": feature_id - 1,
                "source_observation_ordinal": 0,
                "target_observation_ordinal": 1,
            }
            for feature_id in (1, 2)
        ]
        status_value = {
            "status": "AVAILABLE", "reason": "NONE", "value": [0.0, 0.0]
        }
        image_cell = {
            "status": "AVAILABLE", "reason": "NONE",
            "representation": "continuous_pixel_center_fraction.v1",
            "value": [0.5 / 640.0, 0.5 / 480.0],
        }
        provenance_members = []
        for member_index, member_key in enumerate(member_keys):
            def observation_key(camera_id, timestamp, ordinal):
                return {
                    "camera_id": camera_id,
                    "timestamp_value": timestamp,
                    "timestamp_key": (
                        "f64:0x3fe0000000000000" if timestamp == 0.5 else
                        "f64:0x3ff0000000000000"
                    ),
                    "feature_id": member_key["feature_id"],
                    "detached_index": member_key["detached_index"],
                    "observation_ordinal": ordinal,
                }
            provenance_members.append({
                "member_key": member_key,
                "feature_id": member_key["feature_id"],
                "detached_index": member_key["detached_index"],
                "source_observation_key": observation_key(0, 0.5, 0),
                "target_observation_key": observation_key(0, 1.0, 1),
                "target_stereo_observation_key": observation_key(1, 1.0, 2),
                "source_raw_pixel": status_value,
                "target_raw_pixel": status_value,
                "source_normalized_coordinates": status_value,
                "target_normalized_coordinates": status_value,
                "source_unit_bearing": {
                    "status": "AVAILABLE", "reason": "NONE",
                    "frame": "camera",
                    "value": [0.0, 0.0, 1.0],
                },
                "target_unit_bearing": {
                    "status": "AVAILABLE", "reason": "NONE",
                    "frame": "camera",
                    "value": [0.0, 0.0, 1.0],
                },
                "target_stereo_raw_pixel": status_value,
                "source_image_cell": image_cell,
                "target_image_cell": image_cell,
                "target_stereo_image_cell": image_cell,
                "native_source_status": {
                    "status": "AVAILABLE", "reason": "TYPED_T1_SOURCE_OUTCOME",
                    "full_outcome": "INIT_TOO_FAR",
                },
                "finite_validation": {"status": "AVAILABLE", "reason": "NONE"},
            })
        event_callback = {
            "schema": campaign.T0_EVENT_EXTENSION_SCHEMA,
            "record_version": 1,
            "causal_imu_interval": {
                "gyro_interval_id": 0,
                "callback_id": 0,
                "status": "NOT_AVAILABLE",
                "reason": "FIRST_CALLBACK",
                "units": {
                    "time": "s", "angular_rate": "rad/s", "rotation": "rad"
                },
                "frame_convention": {
                    "raw": "native_gyroscope_measurement_frame",
                    "bias_corrected": (
                        "native_gyroscope_measurement_frame_wm_minus_bias_g"),
                    "delta": (
                        "native_gyroscope_frame_passive_left_composed_"
                        "Exp_minus_omega_dt_diagnostic"),
                },
                "camera_frame_ids": [0, 1],
                "current_callback_camera_timestamp": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 1.0,
                },
                "previous_processed_callback_camera_timestamp": {
                    "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
                },
                "interval_start_camera_s": {
                    "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
                },
                "interval_end_camera_s": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 1.0,
                },
                "duration_s": {
                    "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
                },
                "dt_CAMtoIMU_s": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 0.0,
                },
                "clock_equation": "t_imu=t_camera+dt_CAMtoIMU",
                "interval_start_imu_s": {
                    "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
                },
                "interval_end_imu_s": {
                    "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
                },
                "support": {
                    "first_timestamp_s": {
                        "status": "NOT_AVAILABLE",
                        "reason": "NO_CAUSAL_BUFFERED_IMU",
                    },
                    "last_timestamp_s": {
                        "status": "NOT_AVAILABLE",
                        "reason": "NO_CAUSAL_BUFFERED_IMU",
                    },
                    "source_sample_count": {
                        "status": "NOT_AVAILABLE",
                        "reason": "FIRST_CALLBACK"
                    },
                    "start_endpoint": {
                        "status": "UNSUPPORTED", "reason": "FIRST_CALLBACK",
                        "lower_timestamp": {"status": "NOT_AVAILABLE", "reason": "LOWER_BRACKET_UNAVAILABLE"},
                        "upper_timestamp": {"status": "NOT_AVAILABLE", "reason": "UPPER_BRACKET_UNAVAILABLE"},
                    },
                    "end_endpoint": {
                        "status": "UNSUPPORTED", "reason": "FIRST_CALLBACK",
                        "lower_timestamp": {"status": "NOT_AVAILABLE", "reason": "LOWER_BRACKET_UNAVAILABLE"},
                        "upper_timestamp": {"status": "NOT_AVAILABLE", "reason": "UPPER_BRACKET_UNAVAILABLE"},
                    },
                    "maximum_internal_sample_gap_s": {"status": "NOT_AVAILABLE", "reason": "INSUFFICIENT_SUPPORT_FOR_GAP"},
                    "coverage_fraction": {"status": "NOT_AVAILABLE", "reason": "INTERVAL_ENDPOINTS_UNAVAILABLE"},
                    "endpoint_policy": "exact_or_linear_bracket_no_extrapolation.v1",
                },
                "gyro_bias_snapshot_xyz_rad_s": {
                    "status": "AVAILABLE", "reason": "NONE",
                    "value": [0.0, 0.0, 0.0],
                },
                "knots": {"status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK"},
                "integration_method": "piecewise_linear_trapezoid.v1",
                "raw_summary": {"status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK"},
                "bias_corrected_summary": {"status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK"},
            },
            "outcome_association_keys": {
                "callback_id": 0,
                "callback_camera_timestamp_value": 1.0,
                "callback_camera_timestamp_key": "f64:0x3ff0000000000000",
                "estimator_initialized_before": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "estimator_initialized_after": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "estimator_valid_before": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "estimator_valid_after": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "state_timestamp_before": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 1.0,
                },
                "state_timestamp_after": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 1.0,
                },
                "expected_state_row_key": {"status": "NOT_AVAILABLE", "reason": "OUTPUT_NOT_READY"},
                "expected_deviation_row_key": {"status": "NOT_AVAILABLE", "reason": "OUTPUT_NOT_READY"},
                "expected_pose_row_key": {"status": "NOT_AVAILABLE", "reason": "OUTPUT_NOT_READY"},
                "pose_stream_write_status": {"status": "NOT_EXPOSED", "reason": "POSE_WRITE_STATUS_OFFLINE_ONLY"},
                "reset_status": {"status": "NOT_EXPOSED", "reason": "RESET_STATUS_NOT_EXPOSED_BY_NATIVE_PATH"},
                "nonfinite_observed": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "callback_incomplete": {
                    "status": "AVAILABLE", "reason": "NONE",
                    "completion_reason": "NORMAL_SCOPE_EXIT", "value": False,
                },
                "time_since_last_accepted_ordinary_full_update_camera_s": {
                    "status": "NOT_AVAILABLE",
                    "reason": "SEE_BASELINE_DECISION_TYPED_REASON",
                },
                "run_completeness": {
                    "status": "NOT_AVAILABLE",
                    "reason": "RUN_COMPLETENESS_POST_CLOSE_ONLY",
                },
                "ordinary_accepted_full_factor_count": {
                    "status": "AVAILABLE", "reason": "NONE", "value": 0,
                },
                "ordinary_full_visual_update_accepted": {
                    "status": "AVAILABLE", "reason": "NONE", "value": False,
                },
                "reference_association": {"status": "NOT_AVAILABLE", "reason": "REFERENCE_ASSOCIATION_OFFLINE_ONLY"},
                "identity": {
                    "run_identity": "sha256:" + "9" * 64,
                    "source_sha": "b" * 40,
                    "source_tree": "c" * 40,
                    "source_snapshot_sha256": "3" * 64,
                    "build_provenance_id": "4" * 64,
                    "config_sha256": "f" * 64,
                    "calibration_sha256": "1" * 64,
                },
            },
            "group_bearing_provenance": {
                "status": "AVAILABLE", "reason": "NONE",
                "schema_version": campaign.T0_EVENT_EXTENSION_SCHEMA,
                "canonical_ordering_version": "turnsafe.event_extension.order.v1",
                "shared_field_encoding": "one_group_record_plus_member_array.v1",
                "group_count": 1,
                "groups": [{
                    "group_key": {
                        "camera_id": 0,
                        "source_timestamp_key": "f64:0x3fe0000000000000",
                        "target_timestamp_key": "f64:0x3ff0000000000000",
                    },
                    "member_count": 2,
                    "member_key_list": member_keys,
                    "bearing_provenance": (
                        "direct_native_normalized_unit_ray.v1"),
                    "source_clone_timestamp_value": 0.5,
                    "target_clone_timestamp_value": 1.0,
                    "camera": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "camera_id": 0, "model": "RADTAN",
                        "image_width": 640, "image_height": 480,
                        "intrinsics_distortion_hash": "fnv1a64:0000000000000001",
                        "camera_extrinsic_hash": "fnv1a64:0000000000000002",
                        "hash_algorithm": "fnv1a64_binary64_be.v1",
                    },
                    "target_stereo_camera": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "camera_id": 1, "model": "RADTAN",
                        "image_width": 640, "image_height": 480,
                        "intrinsics_distortion_hash": (
                            "fnv1a64:0000000000000003"),
                        "camera_extrinsic_hash": (
                            "fnv1a64:0000000000000004"),
                        "hash_algorithm": "fnv1a64_binary64_be.v1",
                    },
                    "source_current_R_GtoI": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "rows": 3, "cols": 3,
                        "values_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    },
                    "source_fej_R_GtoI": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "rows": 3, "cols": 3,
                        "values_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    },
                    "target_current_R_GtoI": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "rows": 3, "cols": 3,
                        "values_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    },
                    "target_fej_R_GtoI": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "rows": 3, "cols": 3,
                        "values_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    },
                    "fixed_R_ItoC": {
                        "status": "AVAILABLE", "reason": "NONE",
                        "rows": 3, "cols": 3,
                        "values_row_major": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                    },
                    "supported_configuration": {"value": True, "reasons": []},
                    "members": provenance_members,
                }],
            },
        }
        callback["extensions"] = {"event_extension": event_callback}
        path.write_text(
            json.dumps(header, sort_keys=True) + "\n" +
            json.dumps(callback, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        extension_result = campaign._validate_t0_jsonl(
            path, expected, "sha256:" + "9" * 64
        )
        self.assertEqual(extension_result["event_extension"]["interval_count"], 1)
        self.assertEqual(extension_result["event_extension"]["group_provenance_count"], 1)
        self.assertEqual(extension_result["event_extension"]["group_member_count"], 2)

        event_callback["causal_imu_interval"]["gyro_interval_id"] = 1
        path.write_text(json.dumps(header) + "\n" + json.dumps(callback) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "identity mismatch"):
            campaign._validate_t0_jsonl(path, expected, "sha256:" + "9" * 64)
        event_callback["causal_imu_interval"]["gyro_interval_id"] = 0
        event_callback["group_bearing_provenance"]["groups"][0]["member_key_list"].reverse()
        path.write_text(json.dumps(header) + "\n" + json.dumps(callback) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "not canonical"):
            campaign._validate_t0_jsonl(path, expected, "sha256:" + "9" * 64)
        event_callback["group_bearing_provenance"]["groups"][0]["member_key_list"].reverse()

        event_callback["schema"] = "turnsafe.t0.event_extension.future"
        path.write_text(json.dumps(header) + "\n" + json.dumps(callback) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "version mismatch"):
            campaign._validate_t0_jsonl(path, expected, "sha256:" + "9" * 64)
        event_callback["schema"] = campaign.T0_EVENT_EXTENSION_SCHEMA
        event_callback["causal_imu_interval"]["duration_s"]["value"] = 0.0
        path.write_text(json.dumps(header) + "\n" + json.dumps(callback) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(campaign.CampaignError, "hides unavailable"):
            campaign._validate_t0_jsonl(path, expected, "sha256:" + "9" * 64)
        event_callback["causal_imu_interval"]["duration_s"].pop("value")

        callback.pop("extensions")
        for frame in callback["frontend_cameras"]:
            frame.pop("extensions")
        header.pop("extensions")
        expected.pop("extensions")

        callback["sequence_id"] = "forbidden"
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "forbidden key"):
            campaign._validate_t0_jsonl(path, expected)

        callback.pop("sequence_id")
        duplicate_key = {
            "camera_id": 0,
            "source_timestamp_key": "f64:0x1",
            "target_timestamp_key": "f64:0x2",
            "feature_id": 1,
            "detached_index": 0,
            "source_observation_ordinal": 0,
            "target_observation_ordinal": 1,
        }
        observation_key = {
            "camera_id": 0,
            "timestamp_value": 1.0,
            "timestamp_key": "f64:0x3ff0000000000000",
            "feature_id": 1,
            "detached_index": 0,
            "observation_ordinal": 0,
        }
        unavailable = {
            "status": "NOT_EXPOSED",
            "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
        }
        source_camera = {
            "status": "AVAILABLE",
            "camera_id": 0,
            "model": "CamRadtan",
            "intrinsic_id": 10,
            "extrinsic_id": 20,
        }
        candidate = {
            "candidate_key": dict(duplicate_key),
            "source_observation_key": dict(observation_key),
            "source_observation": {
                "observation_key": dict(observation_key),
                "raw_pixel": [1.0, 2.0],
                "normalized_pixel": [0.1, 0.2],
            },
            "target_observation_key": dict(observation_key),
            "target_observation": {
                "observation_key": dict(observation_key),
                "raw_pixel": [1.0, 2.0],
                "normalized_pixel": [0.1, 0.2],
            },
            "supporting_target_time_stereo_observation_key": {
                "status": "NOT_AVAILABLE_FROM_SENSOR",
                "reason": "TARGET_STEREO_UNAVAILABLE",
            },
            "supporting_target_time_stereo_observation": {
                "status": "NOT_AVAILABLE_FROM_SENSOR",
                "reason": "TARGET_STEREO_UNAVAILABLE",
            },
            "camera_calibration": {
                "source": dict(source_camera),
                "target": dict(source_camera),
                "stereo": {
                    "status": "NOT_APPLICABLE",
                    "reason": "TARGET_STEREO_UNAVAILABLE",
                },
            },
            "full_outcome": "INIT_TOO_FAR",
            "target_time_stereo_available": False,
            "target_stereo_rejection_reason": "TARGET_STEREO_UNAVAILABLE",
            "stereo_geometry_primitives": {
                "status": "NOT_AVAILABLE_FROM_SENSOR",
                "reason": "TARGET_STEREO_UNAVAILABLE",
            },
            "target_bearing": dict(unavailable),
            "range_certificate": dict(unavailable),
            "translation_certificate": dict(unavailable),
            "bearing_covariance": dict(unavailable),
            "acute_regime": dict(unavailable),
            "rho_trans": dict(unavailable),
            "rho_threshold": dict(unavailable),
            "static_quality_status": "NOT_APPLICABLE",
            "eligible_before_group": False,
            "terminal_reason": "TARGET_STEREO_UNAVAILABLE",
        }
        for label, value in (
            ("null", None),
            ("non-string", 7),
            ("unknown", "FUTURE_UNKNOWN_REASON"),
            ("wrong-stage", "CONSENSUS_FAILED"),
        ):
            malformed_reason = dict(candidate)
            malformed_reason["terminal_reason"] = value
            callback["shadow_candidates"] = [malformed_reason]
            path.write_text(
                json.dumps(header) + "\n" + json.dumps(callback) + "\n",
                encoding="utf-8",
            )
            with self.subTest(label=label), self.assertRaises(
                campaign.CampaignError
            ):
                campaign._validate_t0_jsonl(path, expected)
        missing_reason = dict(candidate)
        missing_reason.pop("terminal_reason")
        callback["shadow_candidates"] = [missing_reason]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "missing required field terminal_reason"
        ):
            campaign._validate_t0_jsonl(path, expected)

        accepted_erratum_reason = dict(candidate)
        accepted_erratum_reason["terminal_reason"] = "RANGE_LCB_UNAVAILABLE"
        accepted_erratum_reason["target_time_stereo_available"] = True
        group = {
            "pair_key": {
                "camera_id": duplicate_key["camera_id"],
                "source_timestamp_key": duplicate_key["source_timestamp_key"],
                "target_timestamp_key": duplicate_key["target_timestamp_key"],
            },
            "candidate_keys": [dict(duplicate_key)],
            "eligible_candidate_keys": [],
            "raw_candidate_count": 1,
            "pair_group_feature_count": 1,
            "eligible_feature_count": 0,
            "shadow_only_small_group": False,
            "group_size_status": "INSUFFICIENT_FOR_PAIR_GROUP",
            "contains_target_stereo_candidate": True,
            "target_bearing_spatial_metrics": dict(unavailable),
            "rotation_stack_singular_values": dict(unavailable),
            "rotation_stack_rank": dict(unavailable),
            "consensus": {
                "status": "NOT_APPLICABLE",
                "reason": "THRESHOLD_SET_NOT_FROZEN",
            },
            "eligible_before_selection": False,
            "predicted_rotation_angle": dict(unavailable),
            "predicted_information": dict(unavailable),
            "score": dict(unavailable),
            "selection_role": "INELIGIBLE",
            "foregone_reason": "NOT_APPLICABLE",
            "winner_shadow_nis": dict(unavailable),
            "post_nis_count_rank_status": dict(unavailable),
        }
        callback["shadow_candidates"] = [accepted_erratum_reason]
        callback["shadow_groups"] = [group]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        accepted_result = campaign._validate_t0_jsonl(path, expected)
        self.assertEqual(accepted_result["callback_count"], 1)

        for label, value in (
            ("null", None),
            ("non-string", 7),
            ("unknown", "FUTURE_UNKNOWN_REASON"),
            ("wrong-stage", "RANGE_LCB_UNAVAILABLE"),
        ):
            malformed_group = json.loads(json.dumps(group))
            malformed_group["consensus"]["reason"] = value
            callback["shadow_groups"] = [malformed_group]
            path.write_text(
                json.dumps(header) + "\n" + json.dumps(callback) + "\n",
                encoding="utf-8",
            )
            with self.subTest(group_consensus=label), self.assertRaises(
                campaign.CampaignError
            ):
                campaign._validate_t0_jsonl(path, expected)
        missing_group_reason = json.loads(json.dumps(group))
        missing_group_reason["consensus"].pop("reason")
        callback["shadow_groups"] = [missing_group_reason]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "consensus.*missing required field reason"
        ):
            campaign._validate_t0_jsonl(path, expected)

        foregone_group = json.loads(json.dumps(group))
        foregone_group["selection_role"] = "FOREGONE_ELIGIBLE"
        foregone_group["foregone_reason"] = "LOWER_PRE_NIS_SCORE"
        callback["shadow_groups"] = [foregone_group]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        campaign._validate_t0_jsonl(path, expected)
        for label, value in (
            ("unknown", "FUTURE_UNKNOWN_REASON"),
            ("wrong-stage", "RANGE_LCB_UNAVAILABLE"),
        ):
            malformed_foregone = json.loads(json.dumps(foregone_group))
            malformed_foregone["foregone_reason"] = value
            callback["shadow_groups"] = [malformed_foregone]
            path.write_text(
                json.dumps(header) + "\n" + json.dumps(callback) + "\n",
                encoding="utf-8",
            )
            with self.subTest(group_foregone=label), self.assertRaises(
                campaign.CampaignError
            ):
                campaign._validate_t0_jsonl(path, expected)

        callback["shadow_groups"] = []
        callback["shadow_candidates"] = [dict(candidate), dict(candidate)]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "not canonical"):
            campaign._validate_t0_jsonl(path, expected)

        callback["shadow_candidates"] = []
        initializer = {
            "attempted": False,
            "native_success": False,
            "native_function": "NOT_EXPOSED_BY_NATIVE_PATH",
            "native_outcome": "NOT_ATTEMPTED",
            "condition_number": dict(unavailable),
            "depth": dict(unavailable),
            "baseline_ratio": dict(unavailable),
            "predicates": {
                "ill_conditioned": False,
                "too_near_or_behind": False,
                "too_far": False,
                "baseline_ratio": False,
                "native_nan": False,
            },
            "refinement_runs": {
                "status": "NOT_APPLICABLE",
                "reason": "NOT_A_REFINEMENT_STAGE",
            },
            "refinement_lambda": dict(unavailable),
            "refinement_last_step_norm": dict(unavailable),
            "refinement_control_epsilon": dict(unavailable),
            "termination_reason": "NOT_APPLICABLE",
        }
        attempt = {
            "detached_index": 0,
            "feature_id": 1,
            "ordered_observation_keys": [],
            "attempt_has_any_live_same_camera_clone_pair": False,
            "attempt_valid_clone_pair_count": 0,
            "triangulation": dict(initializer),
            "refinement": dict(initializer),
            "schur": {
                "attempted": False,
                "native_accepted": False,
                "native_status": "NOT_EXPOSED_BY_NATIVE_PATH",
                "native_stage": "NOT_EXPOSED_BY_NATIVE_PATH",
                "raw_rows": dict(unavailable),
                "rows_before_reduction": dict(unavailable),
                "degrees_of_freedom": dict(unavailable),
                "rows_after_reduction": dict(unavailable),
                "singular_values": dict(unavailable),
                "numerical_rank": dict(unavailable),
                "reciprocal_condition": dict(unavailable),
                "condition_number": dict(unavailable),
                "numerical_repairs": {
                    "jitter": 0,
                    "clamp": 0,
                    "regularization": 0,
                    "fallback": 0,
                },
            },
            "full_outcome": "UNAVAILABLE",
            "full_outcome_mapping": "NOT_APPLICABLE",
            "native_terminal_status": "NOT_REACHED",
            "target_time_stereo_available": False,
            "accepted_at_native_feature_gate": False,
            "native_feature_row_count": {
                "status": "NOT_EXPOSED",
                "reason": "NOT_EXPOSED_BY_NATIVE_PATH",
            },
            "accepted_full_factor": False,
            "accepted_full_row_count": 0,
            "accepted_full_row_status": "NOT_CONTRIBUTED",
            "finalization_result": "NOT_REACHED",
        }
        callback["full_track_attempts"] = [attempt]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "missing required field full_nis"
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["full_track_attempts"] = []

        missing_stereo_key = dict(candidate)
        missing_stereo_key.pop("supporting_target_time_stereo_observation_key")
        callback["shadow_candidates"] = [missing_stereo_key]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "supporting_target_time_stereo_observation_key",
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["shadow_candidates"] = []

        malformed_calibration = json.loads(json.dumps(candidate))
        malformed_calibration["camera_calibration"]["source"] = {}
        callback["shadow_candidates"] = [malformed_calibration]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "camera_calibration.source.*missing required field status",
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["shadow_candidates"] = []

        malformed_geometry = json.loads(json.dumps(candidate))
        malformed_geometry["stereo_geometry_primitives"] = {}
        callback["shadow_candidates"] = [malformed_geometry]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "stereo_geometry_primitives.*missing required field status",
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["shadow_candidates"] = []

        attempt["full_nis"] = {
            "attempted": False,
            "native_stage": "NOT_EXPOSED_BY_NATIVE_PATH",
            "lifecycle_accept": False,
            "degrees_of_freedom": dict(unavailable),
            "statistic": dict(unavailable),
            "threshold": dict(unavailable),
            "decision": "NOT_ATTEMPTED",
        }
        attempt["schur"]["raw_rows"] = {}
        callback["full_track_attempts"] = [attempt]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "schur.raw_rows.*missing required field status",
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["full_track_attempts"] = []

        callback["completeness"]["typed_reasons"] = "not-an-array"
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "completeness.typed_reasons is not an array"
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["completeness"]["typed_reasons"] = []

        callback["baseline_decision"]["no_full_visual_update_duration"] = {
            "status": "AVAILABLE",
            "reason": "NONE",
        }
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "AVAILABLE status is missing numeric value"
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["baseline_decision"]["no_full_visual_update_duration"] = {
            "status": "NOT_AVAILABLE",
            "reason": "NO_PRIOR_ACCEPTED_FULL_UPDATE",
        }

        malformed_available_stereo = json.loads(json.dumps(candidate))
        malformed_available_stereo[
            "supporting_target_time_stereo_observation_key"
        ] = {"status": "AVAILABLE", "reason": "NONE"}
        callback["shadow_candidates"] = [malformed_available_stereo]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError, "AVAILABLE status is missing observation key"
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["shadow_candidates"] = []

        callback["funnel"]["full_outcome_counts_by_code"] = {
            "FULL_ACCEPTED": "one"
        }
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            campaign.CampaignError,
            "full_outcome_counts_by_code.FULL_ACCEPTED.*nonnegative integer",
        ):
            campaign._validate_t0_jsonl(path, expected)
        callback["funnel"]["full_outcome_counts_by_code"] = {}

        header["supported_configuration"] = False
        header["unsupported_reasons"] = ["CALLER_SELF_DECLARED"]
        path.write_text(
            json.dumps(header) + "\n" + json.dumps(callback) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(campaign.CampaignError, "not a supported"):
            campaign._validate_t0_jsonl(path, expected)


if __name__ == "__main__":
    unittest.main()
