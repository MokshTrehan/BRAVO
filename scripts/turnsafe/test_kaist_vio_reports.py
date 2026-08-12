#!/usr/bin/python3
"""Fixture-only tests for the deterministic KAIST VIO evidence consolidator."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().with_name("kaist_vio_reports.py")
SPEC = importlib.util.spec_from_file_location("kaist_vio_reports", str(MODULE_PATH))
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("unable to load kaist_vio_reports.py")
reports = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reports)


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _identity(path: Path, executable: bool = False) -> dict:
    return {
        "path": str(path.resolve()),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "executable": executable,
    }


def _stream(message_type: str, kind: str, count: int = 2) -> dict:
    timing = {
        "start_ns": 1_000_000_000,
        "end_ns": 1_100_000_000,
        "duration_ns": 100_000_000,
        "strictly_increasing": True,
        "rate_hz": 10.0,
    }
    result = {
        "kind": kind,
        "type": message_type,
        "count": count,
        "record_time": dict(timing),
        "header_time": dict(timing),
        "recorded_message_semantic_sha256": "5" * 64,
    }
    if message_type in ("sensor_msgs/CompressedImage", "sensor_msgs/Image"):
        result["image"] = {"decoded_image_semantic_sha256": "6" * 64}
    if message_type == "sensor_msgs/Imu":
        result["finite_runtime_components"] = True
    if message_type == "geometry_msgs/PoseStamped":
        result["finite_pose_components"] = True
        result["nonzero_quaternion"] = True
    return result


def _audit(role: str) -> dict:
    topics = reports.SOURCE_TOPICS if role == "source" else reports.ADAPTED_TOPICS
    streams = {
        topic: _stream(
            message_type,
            reports.EXPECTED_STREAM_KINDS[topic],
            3 if reports.EXPECTED_STREAM_KINDS[topic] == "camera0" else 2,
        )
        for topic, message_type in topics.items()
    }
    cameras = [topic for topic in topics if "infra" in topic]
    return {
        "schema": "turnsafe.kaist_vio_adapter.audit.v1",
        "role": role,
        "source_profile": "official_raw" if role == "source" else None,
        "streams": streams,
        "stereo": {
            "pair_count": 2,
            "exact_header_pair_count": 2,
            "camera0_count": 3,
            "camera1_count": 2,
            "camera0_unmatched_count": 1,
            "camera1_unmatched_count": 0,
            "camera0_unmatched_header_stamps_ns": [1_200_000_000],
            "camera1_unmatched_header_stamps_ns": [],
            "record_skew_threshold_ns": 20_000_000,
            "matched_pair_record_skew_at_or_above_threshold_count": 0,
            "camera0_topic": cameras[0],
            "camera1_topic": cameras[1],
            "policy": "exact_header_stamp_no_filter_no_retime",
            "matched_pair_record_time_absolute_skew": {
                "min_ns": 0, "max_ns": 0, "sum_ns": 0, "mean_ns": 0.0,
            },
            "pair_semantic_sha256": "1" * 64,
        },
        "logical_order_semantic_sha256": ("2" if role == "source" else "3") * 64,
        "estimator_input_order_semantic_sha256": "4" * 64,
    }


def _adapter_report() -> dict:
    return {
        "schema": reports.ADAPTATION_SCHEMA,
        "source_profile": "official_raw",
        "source_audit": _audit("source"),
        "adapted_audit": _audit("adapted"),
        "preservation": {
            "all_passed": True,
            "checks": {
                "camera0_decoded_image_semantics": True,
                "camera1_decoded_image_semantics": True,
                "imu_recorded_message_semantics": True,
                "source_ground_truth_validated": True,
                "ground_truth_intentionally_omitted": True,
                "stereo_pair_semantics": True,
                "estimator_input_order_semantics": True,
                "retained_message_count": True,
            },
        },
        "ground_truth_policy": "source_required_and_audited_output_omitted",
    }


class Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.artifacts = root / "artifact-output"
        self.dataset_manifests = root / "dataset-manifests"
        self.artifacts.mkdir()
        self.dataset_manifests.mkdir()
        self.static = self._static_inputs()
        self.source_identity_path, self.source_identity = self._source_identity()
        (
            self.parity_evidence_path,
            self.parity_evidence,
            self.expected_digest_path,
            self.expected_digest,
            self.observed_digest_path,
            self.observed_digest,
        ) = self._parity_evidence()
        self.download_path, self.download = self._download()
        self.index_path, self.index = self._index()

    def _static_inputs(self) -> dict:
        names = (
            "config", "kalibr_imu_chain", "kalibr_imucam_chain", "launch",
            "estimator_binary", "reference_tum", "adapter",
            "trajectory_converter", "python", "rosbag", "roslaunch",
            "evo_ape", "evo_rpe", "catkin_find",
        )
        result = {}
        executable_names = {
            "estimator_binary", "python", "rosbag", "roslaunch",
            "evo_ape", "evo_rpe", "catkin_find",
        }
        for name in names:
            path = _write(self.root / "fixed" / name, (name + "\n").encode("ascii"))
            if name in executable_names:
                path.chmod(0o755)
            result[name] = _identity(path, executable=name in executable_names)
        return result

    def _source_identity(self):
        value = {
            "schema": reports.BASELINE_SOURCE_SCHEMA,
            "source_commit": "a" * 40,
            "source_tree": "b" * 40,
            "tracked_tree_clean": True,
            "source_files": {
                name: self.static[name]
                for name in reports.REQUIRED_BASELINE_SOURCE_FILES
            },
            "estimator_binary": self.static["estimator_binary"],
        }
        path = self.root / "baseline-source.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def _digest_document(self, input_identities: dict) -> dict:
        return {
            "schema": reports.BASELINE_DIGEST_SCHEMA,
            "source_sha": self.source_identity["source_commit"],
            "combined_stable_sha256": "f" * 64,
            "input_file_sha256": {
                filename: input_identities[name]["sha256"]
                for name, filename in reports.PARITY_DIGEST_INPUTS.items()
            },
            "stable_fields": {},
            "validation": {
                "all_numeric_fields_finite": True,
                "row_counts_equal": True,
                "state_deviation_trajectory_timestamps_exact": True,
                "timestamps_strictly_increasing": True,
            },
        }

    def _parity_evidence(self):
        expected_inputs = {}
        observed_inputs = {}
        for name, filename in reports.PARITY_DIGEST_INPUTS.items():
            stable = (name + " stable\n").encode("ascii")
            expected_inputs[name] = _identity(
                _write(self.root / "parity" / "expected" / filename, stable)
            )
            observed_bytes = stable
            if name == "timing":
                observed_bytes = b"timing nondeterministic durations differ\n"
            observed_inputs[name] = _identity(
                _write(
                    self.root / "parity" / "observed" / filename,
                    observed_bytes,
                )
            )
        expected_digest = self._digest_document(expected_inputs)
        observed_digest = self._digest_document(observed_inputs)
        expected_path = self.root / "parity" / "expected-digest.json"
        observed_path = self.root / "parity" / "observed-digest.json"
        expected_path.write_text(json.dumps(expected_digest), encoding="utf-8")
        observed_path.write_text(json.dumps(observed_digest), encoding="utf-8")
        comparisons = {}
        for name in ("state", "deviation", "trajectory"):
            comparisons[name] = {
                "passed": True,
                "expected": expected_inputs[name],
                "observed": observed_inputs[name],
            }
        comparisons["timing"] = {
            "bytes_equal": False,
            "expected": expected_inputs["timing"],
            "observed": observed_inputs["timing"],
        }
        parity = {
            "schema": reports.PARITY_EVIDENCE_SCHEMA,
            "expected_digest": _identity(expected_path),
            "observed_digest": _identity(observed_path),
            "byte_comparisons": comparisons,
        }
        parity_path = self.root / "parity" / "parity-evidence.json"
        parity_path.write_text(json.dumps(parity), encoding="utf-8")
        return (
            parity_path,
            parity,
            expected_path,
            expected_digest,
            observed_path,
            observed_digest,
        )

    def _download(self):
        archive = _write(self.root / "downloads" / "kaist.zip", b"fixture archive")
        head_capture = _write(
            self.root / "logs" / "archive-head.txt", b"HTTP/1.1 200 OK\n"
        )
        download_capture = _write(
            self.root / "logs" / "archive-download-head.txt",
            b"HTTP/1.1 200 OK\n",
        )
        effective_capture = _write(
            self.root / "logs" / "archive-effective-url.txt",
            (reports.PRIMARY_ARCHIVE_URL + "\n").encode("ascii"),
        )
        metadata = {}
        for name in reports.REQUIRED_METADATA_FILES:
            path = _write(self.root / "metadata" / name, (name + "\n").encode("utf-8"))
            metadata[name] = _identity(path)
        value = {
            "schema": reports.DOWNLOAD_INPUT_SCHEMA,
            "retrieval_utc": "2026-08-12T15:00:00Z",
            "extraction_utc": "2026-08-12T16:00:00Z",
            "source_urls": [{
                "url": reports.PRIMARY_ARCHIVE_URL,
                "route": "primary_archive",
                "attempted": True,
                "succeeded": True,
            }],
            "fallback_status": "not_used",
            "http": {
                "status": 200,
                "content_length": archive.stat().st_size,
                "header_capture_path": str(head_capture.resolve()),
                "header_capture_size_bytes": head_capture.stat().st_size,
                "header_capture_sha256": hashlib.sha256(
                    head_capture.read_bytes()
                ).hexdigest(),
                "download_header_capture": _identity(download_capture),
                "effective_url_capture": _identity(effective_capture),
            },
            "archive": {**_identity(archive), "unzip_test_passed": True},
            "official_metadata": {
                "repository_url": "https://github.com/url-kaist/kaistviodataset.git",
                "commit_sha": reports.OFFICIAL_METADATA_COMMIT,
                "retrieval_utc": "2026-08-12T14:00:00Z",
                "license": {
                    "readme_declaration": "CC BY-NC-SA 3.0",
                    "repository_file": "GPL-3.0 text",
                },
                "files": metadata,
            },
            "failures_and_retries": [],
        }
        path = self.root / "download-evidence.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def _command(self, run_dir: Path, name: str) -> dict:
        log = _write(run_dir / (name + ".log"), (name + " log\n").encode("ascii"))
        identity = _identity(log)
        return {
            "argv": ["/fixture/" + name, "--fixed"],
            "shell": "/fixture/{} --fixed".format(name),
            "cwd": str(self.root),
            "environment": {"TZ": "UTC"},
            "started_utc": "2026-08-12T17:00:00Z",
            "finished_utc": "2026-08-12T17:00:01Z",
            "duration_seconds": 1.0,
            "timeout_seconds": 10.0,
            "exit_code": 0,
            "timed_out": False,
            "interrupted": False,
            "signals_sent": [],
            "process_group_survived_cleanup": False,
            "error": None,
            "log": identity["path"],
            "log_sha256": identity["sha256"],
            "log_size_bytes": identity["size_bytes"],
        }

    def _numeric_output(self, run_dir: Path, name: str) -> dict:
        path = _write(run_dir / (name + ".txt"), b"1.0 0.0\n2.0 0.0\n")
        return {
            **_identity(path),
            "rows": 2,
            "columns": 2,
            "timestamps_strictly_increasing": True,
            "all_values_finite": True,
            "first_timestamp": 1.0,
            "last_timestamp": 2.0,
        }

    def _metric(self, run_dir: Path, name: str) -> dict:
        path = _write(run_dir / (name + ".zip"), (name + " metric\n").encode("ascii"))
        identity = _identity(path)
        return {
            "path": identity["path"],
            "sha256": identity["sha256"],
            "samples": 2,
            "stats": {"rmse": 0.1, "mean": 0.09},
            "title": name,
            "label": "fixture",
        }

    def _campaign(self, sequence: str, source: dict, adapted: dict, adapter: dict, run_dir: Path) -> dict:
        before = dict(self.static)
        before["source_bag"] = source
        command_names = (
            "source_rosbag_info", "adapted_rosbag_info", "resolve_parameters",
            "resolve_estimator_binary", "roslaunch", "trajectory_conversion",
            "ape_translation", "rpe_translation_1m", "rpe_rotation_1m_deg",
        )
        commands = {name: self._command(run_dir, name) for name in command_names}
        parameters = _write(run_dir / "resolve_parameters.log", b"/fixed: true\n")
        parameter_identity = _identity(parameters)
        commands["resolve_parameters"].update({
            "log": parameter_identity["path"],
            "log_sha256": parameter_identity["sha256"],
            "log_size_bytes": parameter_identity["size_bytes"],
        })
        after = dict(before)
        after["adapted_bag"] = adapted
        checks = {
            "adapted_bag_has_no_ground_truth": True,
            "runtime_ground_truth_excluded": True,
            "exact_header_runtime_matches_adapted_audit": True,
            "no_reset_lines": True,
            "no_nonfinite_lines": True,
            "no_unpaired_warning_lines": True,
            "no_dropped_message_lines": True,
            "fixed_inputs_unchanged": True,
            "camera_enqueue_accounts_for_exact_header_pairs": True,
            "camera_decode_failures_zero": True,
            "camera_processed_pairs_match_queued": True,
            "camera_queue_drained": True,
        }
        return {
            "schema": "turnsafe.kaist_vio_campaign.sequence.v1",
            "sequence": sequence,
            "campaign_index": reports.ORDERED_SEQUENCES.index(sequence),
            "prepare_only": False,
            "started_utc": "2026-08-12T17:00:00Z",
            "finished_utc": "2026-08-12T17:01:00Z",
            "status": "COMPLETED",
            "commands": commands,
            "checks": checks,
            "completion": {
                "estimator_started": True,
                "estimator_completed": True,
                "output_validation_passed": True,
                "evaluation_completed": True,
                "timed_out": False,
                "roslaunch_exit_code": 0,
                "roslaunch_runtime_seconds": 10.0,
                "process_group_survived_cleanup": False,
                "reset_detected": False,
                "nonfinite_detected": False,
                "unpaired_warning_count": 0,
                "dropped_message_count": 0,
            },
            "runtime": {
                "ros_port": 20000,
                "timeout_seconds": 100.0,
                "environment": {"TZ": "UTC"},
                "ground_truth_runtime_policy": "reference_used_only_after_roslaunch_finished",
                "total_duration_seconds": 60.0,
            },
            "inputs_before": before,
            "inputs_after": after,
            "adapted_bag": adapted,
            "source_audit": adapter["source_audit"],
            "adapted_audit": adapter["adapted_audit"],
            "exact_header_stereo_runtime": {
                "adapted_audit_record_skew_threshold_ns": 20_000_000,
                "expected_from_adapted_audit": {
                    "camera0_without_match": 1,
                    "camera1_without_match": 0,
                    "exact_header_pairs": 2,
                    "maximum_record_delta_ns": 0,
                    "record_delta_ge_20ms": 0,
                },
                "line": "[SERIAL-KAIST]: fixture exact-header summary",
                "matches_adapted_audit": True,
                "observed": {
                    "camera0_without_match": 1,
                    "camera1_without_match": 0,
                    "exact_header_pairs": 2,
                    "maximum_record_delta_ns": 0,
                    "record_delta_ge_20ms": 0,
                },
            },
            "camera_enqueue_runtime": {
                "observed": {
                    "queued_pairs": 1,
                    "processed_pairs": 1,
                    "frequency_thinned_pairs": 1,
                    "cam0_decode_failures": 0,
                    "cam1_decode_failures": 0,
                    "pending_pairs": 0,
                },
                "line": "[SERIAL-KAIST]: fixture camera enqueue summary",
                "exact_header_pairs": 2,
                "accounted_pairs": 2,
                "frequency_thinning_policy": (
                    "existing_frozen_baseline_camera_frequency_policy"
                ),
                "pair_accounting_complete": True,
                "processed_pairs_match_queued": True,
                "queue_drained": True,
                "decode_failures_zero": True,
            },
            "reference_validation": {
                **before["reference_tum"],
                "rows": 2,
                "columns": 8,
                "timestamps_strictly_increasing": True,
                "all_values_finite": True,
                "first_timestamp": 1.0,
                "last_timestamp": 2.0,
            },
            "outputs": {
                name: self._numeric_output(run_dir, name)
                for name in ("state", "deviation", "timing", "trajectory_tum")
            },
            "metrics": {
                name: self._metric(run_dir, name)
                for name in ("ape_translation", "rpe_translation_1m", "rpe_rotation_1m_deg")
            },
            "diagnostics": {
                "yaw": {"status": "NOT_AVAILABLE", "reason": "not emitted"},
                "tilt": {"status": "NOT_AVAILABLE", "reason": "not emitted"},
            },
            "output_consistency": {
                "state_deviation_timestamp_sequences_equal": True,
                "state_trajectory_timestamp_sequences_equal": True,
                "callback_state_rows": 2,
                "timing_rows": 2,
                "callback_timing_row_counts_equal": True,
                "timing_state_timestamp_tolerance_seconds": 1e-5,
                "timing_state_maximum_absolute_difference_seconds": 0.0,
                "timing_state_timestamps_within_tolerance": True,
            },
        }

    def _index(self):
        items = []
        for order, sequence in enumerate(reports.ORDERED_SEQUENCES):
            run_dir = self.root / "runs" / str(order)
            source = _identity(_write(
                self.root / "source" / sequence,
                (sequence + " source").encode("utf-8"),
            ))
            adapted = _identity(_write(
                self.root / "adapted" / sequence,
                (sequence + " adapted").encode("utf-8"),
            ))
            run_dir.mkdir(parents=True, exist_ok=True)
            adapter = _adapter_report()
            adapter_path = run_dir / "adapter.json"
            adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
            info = {
                "path": source["path"],
                "duration": 0.1,
                "start": 1.0,
                "end": 1.1,
                "messages": sum(
                    stream["count"]
                    for stream in adapter["source_audit"]["streams"].values()
                ),
                "topics": [
                    {
                        "topic": topic,
                        "type": message_type,
                        "messages": adapter["source_audit"]["streams"][topic]["count"],
                        "frequency": 10.0,
                    }
                    for topic, message_type in reports.SOURCE_TOPICS.items()
                ],
            }
            info_path = run_dir / "source_info.json"
            info_path.write_text(json.dumps(info), encoding="utf-8")
            campaign = self._campaign(sequence, source, adapted, adapter, run_dir)
            info_identity = _identity(info_path)
            campaign["commands"]["source_rosbag_info"].update({
                "log": info_identity["path"],
                "log_sha256": info_identity["sha256"],
                "log_size_bytes": info_identity["size_bytes"],
            })
            campaign_path = run_dir / "sequence_result.json"
            campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
            items.append({
                "sequence": sequence,
                "source_bag": source,
                "adapted_bag": adapted,
                "adapter_report": str(adapter_path.resolve()),
                "rosbag_info": str(info_path.resolve()),
                "campaign_manifest": str(campaign_path.resolve()),
            })
        value = {
            "schema": reports.INDEX_SCHEMA,
            "adapter_required": True,
            "baseline_source_identity": _identity(self.source_identity_path),
            "mh01_parity": {
                "passed": True,
                "expected_digest": "f" * 64,
                "observed_digest": "f" * 64,
                "parity_evidence": _identity(self.parity_evidence_path),
            },
            "unsupported_reasons": [],
            "engineering_blockers": [],
            "sequences": items,
        }
        path = self.root / "evidence-index.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def rewrite_index(self) -> None:
        self.index_path.write_text(json.dumps(self.index), encoding="utf-8")

    def rewrite_source_identity(self) -> None:
        self.source_identity_path.write_text(
            json.dumps(self.source_identity), encoding="utf-8"
        )
        self.index["baseline_source_identity"] = _identity(
            self.source_identity_path
        )
        self.rewrite_index()

    def rewrite_expected_digest(self) -> None:
        self.expected_digest_path.write_text(
            json.dumps(self.expected_digest), encoding="utf-8"
        )
        self.parity_evidence["expected_digest"] = _identity(
            self.expected_digest_path
        )
        self.rewrite_parity_evidence()

    def rewrite_observed_digest(self) -> None:
        self.observed_digest_path.write_text(
            json.dumps(self.observed_digest), encoding="utf-8"
        )
        self.parity_evidence["observed_digest"] = _identity(
            self.observed_digest_path
        )
        self.rewrite_parity_evidence()

    def rewrite_parity_evidence(self) -> None:
        self.parity_evidence_path.write_text(
            json.dumps(self.parity_evidence), encoding="utf-8"
        )
        self.index["mh01_parity"]["parity_evidence"] = _identity(
            self.parity_evidence_path
        )
        self.rewrite_index()


class ClassificationTests(unittest.TestCase):
    def _passes(self, value=True):
        return dict((name, value) for name in reports.ORDERED_SEQUENCES)

    def test_download_source_allowlist_accepts_official_git_clone_url(self):
        entries = reports._validate_source_urls([
            {
                "url": reports.PRIMARY_ARCHIVE_URL,
                "route": "primary_archive",
                "attempted": True,
                "succeeded": True,
            },
            {
                "url": "https://github.com/url-kaist/kaistviodataset.git",
                "route": "official_metadata",
                "attempted": True,
                "succeeded": True,
            },
        ])
        self.assertEqual(entries[1]["route"], "official_metadata")

    def test_all_four_classifications(self):
        passed = self._passes()
        self.assertEqual(
            reports.classify_compatibility(False, passed, True), "RUNNABLE"
        )
        self.assertEqual(
            reports.classify_compatibility(True, passed, True),
            "RUNNABLE_WITH_DECLARED_ADAPTER",
        )
        self.assertEqual(
            reports.classify_compatibility(True, passed, True, engineering_blockers=["tool"]),
            "BLOCKED_ENGINEERING",
        )
        self.assertEqual(
            reports.classify_compatibility(True, passed, True, unsupported_reasons=["codec"]),
            "UNSUPPORTED",
        )

    def test_required_passing_gate_and_full_campaign(self):
        passed = self._passes()
        passed["rotation/rotation_fast.bag"] = False
        self.assertEqual(
            reports.classify_compatibility(True, passed, True), "BLOCKED_ENGINEERING"
        )
        passed = self._passes()
        for name in list(passed):
            if "_head.bag" in name:
                passed[name] = False
        self.assertEqual(
            reports.classify_compatibility(True, passed, True), "BLOCKED_ENGINEERING"
        )


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _consolidate(self):
        return reports.consolidate(
            self.fixture.download_path,
            self.fixture.index_path,
            [self.root],
            self.fixture.artifacts,
            self.fixture.dataset_manifests,
        )

    def _first_campaign(self):
        path = Path(
            self.fixture.index["sequences"][0]["campaign_manifest"]
        )
        return path, json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_campaign(path: Path, campaign: dict) -> None:
        path.write_text(json.dumps(campaign), encoding="utf-8")

    def test_complete_fixture_outputs_and_mirrors(self):
        hashes = self._consolidate()
        self.assertEqual(len(hashes), 7)
        compatibility = (
            self.fixture.artifacts / "KAIST_COMPATIBILITY_REPORT.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in compatibility.splitlines() if line.strip()][-1],
            "RUNNABLE_WITH_DECLARED_ADAPTER",
        )
        self.assertEqual(
            (self.fixture.artifacts / "KAIST_DOWNLOAD_MANIFEST.json").read_bytes(),
            (self.fixture.dataset_manifests / "DOWNLOAD_MANIFEST.json").read_bytes(),
        )
        self.assertEqual(
            (self.fixture.artifacts / "KAIST_BAG_INVENTORY.csv").read_bytes(),
            (self.fixture.dataset_manifests / "BAG_INVENTORY.csv").read_bytes(),
        )
        download_manifest = json.loads(
            (
                self.fixture.artifacts / "KAIST_DOWNLOAD_MANIFEST.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            download_manifest["http"]["header_capture_sha256"],
            self.fixture.download["http"]["header_capture_sha256"],
        )
        self.assertEqual(
            download_manifest["http"]["download_header_capture"],
            {
                key: self.fixture.download["http"]["download_header_capture"][key]
                for key in ("path", "size_bytes", "sha256")
            },
        )
        self.assertEqual(
            download_manifest["http"]["effective_url_capture"],
            {
                key: self.fixture.download["http"]["effective_url_capture"][key]
                for key in ("path", "size_bytes", "sha256")
            },
        )
        inventory = list(csv.DictReader(io.StringIO(
            (self.fixture.artifacts / "KAIST_BAG_INVENTORY.csv").read_text(encoding="utf-8")
        )))
        baseline = list(csv.DictReader(io.StringIO(
            (self.fixture.artifacts / "KAIST_BASELINE_RESULTS.csv").read_text(encoding="utf-8")
        )))
        self.assertEqual([row["sequence"] for row in inventory], list(reports.ORDERED_SEQUENCES))
        self.assertEqual([row["sequence"] for row in baseline], list(reports.ORDERED_SEQUENCES))
        self.assertTrue(all(row["passed"] == "True" for row in baseline))
        self.assertTrue(
            all(
                row["baseline_source_commit"] == "a" * 40
                and row["baseline_source_tree"] == "b" * 40
                and json.loads(row["baseline_source_evidence"])["sha256"]
                == self.fixture.index["baseline_source_identity"]["sha256"]
                and row["exact_header_pair_count"] == "2"
                and row["queued_pair_count"] == "1"
                and row["processed_pair_count"] == "1"
                and row["frequency_thinned_pair_count"] == "1"
                and row["camera0_decode_failure_count"] == "0"
                and row["camera1_decode_failure_count"] == "0"
                and row["pending_pair_count"] == "0"
                and row["camera_enqueue_accounting_passed"] == "True"
                and row["processed_pairs_match_queued"] == "True"
                and row["camera_queue_drained"] == "True"
                and row["camera_decode_failures_zero"] == "True"
                for row in baseline
            )
        )
        sequence_manifest = json.loads(
            (
                self.fixture.artifacts / "KAIST_SEQUENCE_MANIFEST.json"
            ).read_text(encoding="utf-8")
        )
        self.assertTrue(sequence_manifest["adapter_required"])
        self.assertFalse(
            sequence_manifest["mh01_parity"]["parity_evidence"]
            ["byte_comparisons"]["timing"]["bytes_equal"]
        )
        for item in sequence_manifest["sequences"]:
            self.assertEqual(item["baseline_source_commit"], "a" * 40)
            self.assertEqual(item["baseline_source_tree"], "b" * 40)
            self.assertEqual(
                item["baseline_source_evidence"],
                sequence_manifest["baseline_source"]["evidence_identity"],
            )
            self.assertTrue(
                item["campaign"]["camera_enqueue_runtime"]
                ["pair_accounting_complete"]
            )

    def test_payload_consolidation_is_byte_deterministic(self):
        first = reports.build_outputs(
            self.fixture.download,
            self.fixture.index,
            [self.root.resolve()],
        )
        second = reports.build_outputs(
            self.fixture.download,
            self.fixture.index,
            [self.root.resolve()],
        )
        self.assertEqual(first, second)

    def test_refuses_any_overwrite(self):
        self._consolidate()
        with self.assertRaisesRegex(reports.ReportError, "refusing to overwrite"):
            self._consolidate()

    def test_wrong_sequence_order_fails_before_outputs(self):
        sequences = self.fixture.index["sequences"]
        sequences[0], sequences[1] = sequences[1], sequences[0]
        self.fixture.rewrite_index()
        with self.assertRaisesRegex(reports.ReportError, "exact frozen order"):
            self._consolidate()
        self.assertEqual(list(self.fixture.artifacts.iterdir()), [])
        self.assertEqual(list(self.fixture.dataset_manifests.iterdir()), [])

    def test_failed_campaign_is_classified_blocked(self):
        first = self.fixture.index["sequences"][0]
        campaign_path = Path(first["campaign_manifest"])
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign["status"] = "FAILED"
        campaign["completion"]["estimator_completed"] = False
        campaign["checks"]["no_reset_lines"] = False
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        self._consolidate()
        compatibility = (
            self.fixture.artifacts / "KAIST_COMPATIBILITY_REPORT.md"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            [line for line in compatibility.splitlines() if line.strip()][-1],
            "BLOCKED_ENGINEERING",
        )

    def test_source_adapted_unmatched_stereo_mismatch_fails_closed(self):
        first = self.fixture.index["sequences"][0]
        adapter_path = Path(first["adapter_report"])
        adapter = json.loads(adapter_path.read_text(encoding="utf-8"))
        adapter["adapted_audit"]["stereo"]["camera0_unmatched_header_stamps_ns"] = [
            1_300_000_000
        ]
        adapter_path.write_text(json.dumps(adapter), encoding="utf-8")
        with self.assertRaisesRegex(
            reports.ReportError, "source/adapted exact-header stereo evidence differs"
        ):
            self._consolidate()

    def test_runtime_exact_header_mismatch_fails_closed(self):
        first = self.fixture.index["sequences"][0]
        campaign_path = Path(first["campaign_manifest"])
        campaign = json.loads(campaign_path.read_text(encoding="utf-8"))
        campaign["exact_header_stereo_runtime"]["observed"][
            "camera0_without_match"
        ] = 0
        campaign_path.write_text(json.dumps(campaign), encoding="utf-8")
        with self.assertRaisesRegex(
            reports.ReportError, "observed values mismatch adapted audit"
        ):
            self._consolidate()

    def test_adapter_required_must_be_true(self):
        self.fixture.index["adapter_required"] = False
        self.fixture.rewrite_index()
        with self.assertRaisesRegex(
            reports.ReportError, "adapter_required must be true"
        ):
            self._consolidate()

    def test_all_download_header_capture_identities_are_verified(self):
        records = (
            self.fixture.download["http"]["header_capture_path"],
            self.fixture.download["http"]["download_header_capture"]["path"],
            self.fixture.download["http"]["effective_url_capture"]["path"],
        )
        for raw_path in records:
            with self.subTest(path=raw_path):
                path = Path(raw_path)
                original = path.read_bytes()
                path.write_bytes(original + b"tampered\n")
                with self.assertRaisesRegex(
                    reports.ReportError, "size mismatch|SHA-256 mismatch"
                ):
                    self._consolidate()
                path.write_bytes(original)

    def test_baseline_source_outer_identity_is_verified(self):
        self.fixture.source_identity_path.write_text(
            json.dumps({"tampered": True}), encoding="utf-8"
        )
        with self.assertRaisesRegex(reports.ReportError, "size mismatch|SHA-256 mismatch"):
            self._consolidate()

    def test_baseline_source_requires_clean_exact_git_identity(self):
        self.fixture.source_identity["tracked_tree_clean"] = False
        self.fixture.rewrite_source_identity()
        with self.assertRaisesRegex(reports.ReportError, "tracked tree was not clean"):
            self._consolidate()

    def test_baseline_source_rejects_malformed_commit_and_tree(self):
        for field in ("source_commit", "source_tree"):
            with self.subTest(field=field):
                original = self.fixture.source_identity[field]
                self.fixture.source_identity[field] = "A" * 40
                self.fixture.rewrite_source_identity()
                with self.assertRaisesRegex(
                    reports.ReportError, "lowercase 40-character Git SHA"
                ):
                    self._consolidate()
                self.fixture.source_identity[field] = original
                self.fixture.rewrite_source_identity()

    def test_baseline_source_requires_relevant_source_file_set(self):
        del self.fixture.source_identity["source_files"]["launch"]
        self.fixture.rewrite_source_identity()
        with self.assertRaisesRegex(reports.ReportError, "source files are missing"):
            self._consolidate()

    def test_baseline_source_files_bind_campaign_inputs(self):
        replacement = _identity(
            _write(self.root / "fixed" / "other-config", b"other config\n")
        )
        self.fixture.source_identity["source_files"]["config"] = replacement
        self.fixture.rewrite_source_identity()
        with self.assertRaisesRegex(
            reports.ReportError, "input config differs from the common baseline"
        ):
            self._consolidate()

    def test_every_campaign_binary_must_match_common_binary(self):
        path, campaign = self._first_campaign()
        replacement_path = _write(
            self.root / "fixed" / "other-binary", b"other binary\n"
        )
        replacement_path.chmod(0o755)
        replacement = _identity(replacement_path, executable=True)
        campaign["inputs_before"]["estimator_binary"] = replacement
        campaign["inputs_after"]["estimator_binary"] = replacement
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(
            reports.ReportError, "estimator binary differs from the common baseline"
        ):
            self._consolidate()

    def test_parity_evidence_outer_identity_is_verified(self):
        self.fixture.parity_evidence_path.write_text(
            json.dumps({"tampered": True}), encoding="utf-8"
        )
        with self.assertRaisesRegex(reports.ReportError, "size mismatch|SHA-256 mismatch"):
            self._consolidate()

    def test_parity_evidence_schema_is_required(self):
        self.fixture.parity_evidence["schema"] = "wrong.schema"
        self.fixture.rewrite_parity_evidence()
        with self.assertRaisesRegex(reports.ReportError, "parity evidence schema"):
            self._consolidate()

    def test_parity_digest_schema_and_combined_digest_are_bound(self):
        self.fixture.expected_digest["schema"] = "wrong.schema"
        self.fixture.rewrite_expected_digest()
        with self.assertRaisesRegex(reports.ReportError, "schema must be"):
            self._consolidate()

        self.fixture.expected_digest["schema"] = reports.BASELINE_DIGEST_SCHEMA
        self.fixture.expected_digest["combined_stable_sha256"] = "e" * 64
        self.fixture.rewrite_expected_digest()
        with self.assertRaisesRegex(
            reports.ReportError, "expected digest document disagrees"
        ):
            self._consolidate()

    def test_observed_digest_source_must_match_baseline_commit(self):
        self.fixture.observed_digest["source_sha"] = "c" * 40
        self.fixture.rewrite_observed_digest()
        with self.assertRaisesRegex(
            reports.ReportError, "source SHA disagrees with baseline source commit"
        ):
            self._consolidate()

    def test_stable_byte_comparisons_must_pass(self):
        self.fixture.parity_evidence["byte_comparisons"]["state"][
            "passed"
        ] = False
        self.fixture.rewrite_parity_evidence()
        with self.assertRaisesRegex(
            reports.ReportError, "state byte comparison did not pass"
        ):
            self._consolidate()

    def test_comparison_identity_must_match_digest_input_hash(self):
        replacement = _identity(
            _write(self.root / "parity" / "wrong-state.txt", b"wrong state\n")
        )
        self.fixture.parity_evidence["byte_comparisons"]["state"][
            "expected"
        ] = replacement
        self.fixture.rewrite_parity_evidence()
        with self.assertRaisesRegex(
            reports.ReportError, "state expected identity disagrees"
        ):
            self._consolidate()

    def test_timing_byte_equality_flag_must_match_identities(self):
        self.fixture.parity_evidence["byte_comparisons"]["timing"][
            "bytes_equal"
        ] = True
        self.fixture.rewrite_parity_evidence()
        with self.assertRaisesRegex(
            reports.ReportError, "timing byte-comparison flag disagrees"
        ):
            self._consolidate()

    def test_camera_enqueue_accounting_fails_closed(self):
        path, campaign = self._first_campaign()
        campaign["camera_enqueue_runtime"]["accounted_pairs"] = 1
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(
            reports.ReportError, "enqueue accounting is incomplete"
        ):
            self._consolidate()

    def test_camera_decode_failure_fails_closed(self):
        path, campaign = self._first_campaign()
        campaign["camera_enqueue_runtime"]["observed"][
            "cam0_decode_failures"
        ] = 1
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(reports.ReportError, "camera decode failures"):
            self._consolidate()

    def test_camera_processed_count_and_queue_drain_fail_closed(self):
        path, campaign = self._first_campaign()
        campaign["camera_enqueue_runtime"]["observed"]["processed_pairs"] = 0
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(
            reports.ReportError, "processed/queued pair counts disagree"
        ):
            self._consolidate()

        campaign["camera_enqueue_runtime"]["observed"]["processed_pairs"] = 1
        campaign["camera_enqueue_runtime"]["observed"]["pending_pairs"] = 1
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(reports.ReportError, "retains pending stereo pairs"):
            self._consolidate()

    def test_camera_enqueue_campaign_checks_are_required(self):
        path, campaign = self._first_campaign()
        campaign["checks"]["camera_queue_drained"] = False
        self._write_campaign(path, campaign)
        with self.assertRaisesRegex(
            reports.ReportError, "queue-drained campaign check did not pass"
        ):
            self._consolidate()

    def test_forbidden_paths_are_rejected_before_resolution(self):
        with self.assertRaisesRegex(reports.ReportError, "forbidden"):
            reports._input_path(
                "/tmp/HOLDOUT/sample.bag", "fixture", [self.root.resolve()]
            )
        with self.assertRaisesRegex(reports.ReportError, "forbidden"):
            reports._input_path(
                "/tmp/scripts/cp2/result.json", "fixture", [self.root.resolve()]
            )


if __name__ == "__main__":
    unittest.main()
