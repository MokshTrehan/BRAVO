#!/usr/bin/env python3

import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts.turnsafe import event_ready_corpus as corpus


def identity(path: Path):
    data = path.read_bytes()
    return {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class EventReadyCorpusTests(unittest.TestCase):
    def setUp(self):
        corpus.ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="session1e-corpus-test.", dir=str(corpus.ARTIFACT_ROOT))
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def _telemetry(self):
        typed_missing = {"status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK"}
        frame = {
            "camera_id": 0,
            "extensions": {"event_extension": {
                "schema": corpus.EXTENSION_SCHEMA, "gyro_interval_id": 0}},
        }
        interval = {
            "gyro_interval_id": 0, "callback_id": 0,
            "status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK",
            "units": {"time": "s", "angular_rate": "rad/s", "rotation": "rad"},
            "frame_convention": {
                "raw": "native_gyroscope_measurement_frame",
                "bias_corrected": (
                    "native_gyroscope_measurement_frame_wm_minus_bias_g"),
                "delta": (
                    "native_gyroscope_frame_passive_left_composed_"
                    "Exp_minus_omega_dt_diagnostic"),
            },
            "camera_frame_ids": [0],
            "current_callback_camera_timestamp": {
                "status": "AVAILABLE", "reason": "NONE", "value": 1.0},
            "previous_processed_callback_camera_timestamp": typed_missing,
            "interval_start_camera_s": typed_missing,
            "interval_end_camera_s": {
                "status": "AVAILABLE", "reason": "NONE", "value": 1.0},
            "duration_s": typed_missing,
            "dt_CAMtoIMU_s": {
                "status": "AVAILABLE", "reason": "NONE", "value": 0.0},
            "interval_start_imu_s": typed_missing,
            "interval_end_imu_s": typed_missing,
            "clock_equation": "t_imu=t_camera+dt_CAMtoIMU",
            "integration_method": "piecewise_linear_trapezoid.v1",
            "gyro_bias_snapshot_xyz_rad_s": {
                "status": "AVAILABLE", "reason": "NONE",
                "value": [0.0, 0.0, 0.0]},
            "support": {
                "source_sample_count": typed_missing,
                "first_timestamp_s": typed_missing,
                "last_timestamp_s": typed_missing,
                "maximum_internal_sample_gap_s": typed_missing,
                "coverage_fraction": typed_missing,
                "endpoint_policy": (
                    "exact_or_linear_bracket_no_extrapolation.v1"),
                "start_endpoint": {
                    "status": "UNSUPPORTED", "reason": "FIRST_CALLBACK",
                    "lower_timestamp": typed_missing,
                    "upper_timestamp": typed_missing},
                "end_endpoint": {
                    "status": "UNSUPPORTED", "reason": "FIRST_CALLBACK",
                    "lower_timestamp": typed_missing,
                    "upper_timestamp": typed_missing},
            },
            "knots": {"status": "NOT_AVAILABLE", "reason": "FIRST_CALLBACK"},
            "raw_summary": typed_missing,
            "bias_corrected_summary": typed_missing,
        }
        records = [
            {"schema": "turnsafe.t0.v1", "record_type": "run_header",
             "frozen_base_sha": "c" * 40,
             "source_sha": "a" * 40, "tree_sha": "b" * 40,
             "source_snapshot_sha256": "2" * 64, "source_dirty": True,
             "build_provenance_id": "3" * 64,
             "configure_manifest_sha256": "9" * 64,
             "build_manifest_sha256": "a" * 64,
             "binary_sha256": "4" * 64, "config_sha256": "5" * 64,
             "calibration_sha256": "8" * 64,
             "diagnostic_schema_sha256": "b" * 64,
             "extensions": {"event_extension": {
                 "schema": corpus.EXTENSION_SCHEMA, "record_version": 1,
                 "capture_flags": {
                     "causal_imu_intervals": True,
                     "outcome_association_keys": True,
                     "group_bearing_provenance": True},
                 "integration_method": "piecewise_linear_trapezoid.v1",
                 "rotation_convention": (
                     "native_gyroscope_frame_passive_left_exp_minus_"
                     "omega_diagnostic.v1"),
                 "gyro_bias_convention": (
                     "raw_wm_minus_current_callback_bias_g.v1"),
                 "clock_mapping": (
                     "t_imu_equals_t_camera_plus_dt_CAMtoIMU.v1"),
                 "association_version": "turnsafe.offline_association.v1",
                 "canonical_ordering_version": (
                     "turnsafe.event_extension.order.v1"),
                 "image_cell_representation": (
                     "continuous_pixel_center_fraction.v1"),
                 "group_hash_algorithm": "fnv1a64_binary64_be.v1"}}},
            {"schema": "turnsafe.t0.v1", "record_type": "callback",
             "callback_index": 0, "callback_timestamp_value": 1.0,
             "callback_timestamp_key": "f64:0x3ff0000000000000",
             "frontend_cameras": [frame],
             "extensions": {"event_extension": {
                 "schema": corpus.EXTENSION_SCHEMA, "record_version": 1,
                 "causal_imu_interval": interval,
                 "group_bearing_provenance": {
                     "status": "AVAILABLE", "reason": "NONE",
                     "group_count": 0, "groups": []}}}},
        ]
        raw = "".join(json.dumps(record, sort_keys=True) + "\n"
                      for record in records).encode("utf-8")
        raw_identity = {
            "path": str(self.root / "deleted.jsonl"),
            "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
        archive = self.root / "telemetry.jsonl.gz"
        with archive.open("wb") as stream:
            with gzip.GzipFile(fileobj=stream, mode="wb", mtime=0) as compressor:
                compressor.write(raw)
        gzip_path = Path(shutil.which("gzip"))
        archive_binding = {
            "schema_version": "turnsafe.lossless_telemetry_archive.v1",
            "algorithm": "gzip", "compressor": identity(gzip_path),
            "archive_test": {"exit_code": 0, "timed_out": False,
                             "process_group_survived_cleanup": False},
            "roundtrip": {"sha256": raw_identity["sha256"],
                          "size_bytes": raw_identity["size_bytes"]},
            "raw": raw_identity, "archive": identity(archive),
            "raw_removed_after_verified_roundtrip": True,
        }
        return raw_identity, archive_binding

    def _association(self, telemetry_sha, outputs, reference_sha):
        state_path = self.root / "CALLBACK_STATE_ASSOCIATION.source.csv"
        state_fields = [
            "association_version", "policy_id", "callback_id",
            "state_status", "state_reason", "state_time_residual_s",
            "deviation_status", "deviation_reason",
            "deviation_time_residual_s", "trajectory_status",
            "trajectory_reason", "trajectory_time_residual_s"]
        reference_path = self.root / "CALLBACK_REFERENCE_ASSOCIATION.source.csv"
        reference_fields = [
            "association_version", "policy_id", "callback_id",
            "reference_status", "reference_reason",
            "reference_time_residual_s"]
        for path, fields, streams in (
                (state_path, state_fields,
                 ("state", "deviation", "trajectory")),
                (reference_path, reference_fields, ("reference",))):
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields,
                                        lineterminator="\n")
                writer.writeheader()
                row = {"association_version": "turnsafe.offline_association.v1",
                       "policy_id": (
                           "unique_nearest_expected_output_timestamp_max_5p1e-6s.v1"
                           if streams[0] == "state" else
                           "unique_nearest_expected_output_timestamp_max_0p01s.v1"),
                       "callback_id": 0}
                for name in streams:
                    row[name + "_status"] = "MISSING"
                    row[name + "_reason"] = "FIRST_CALLBACK"
                    row[name + "_time_residual_s"] = ""
                writer.writerow(row)
        coverage = {
            "schema_version": "turnsafe.association_coverage.v1",
            "association_version": "turnsafe.offline_association.v1",
            "callback_count": 1,
            "inputs": {
                "telemetry_sha256": telemetry_sha,
                "state_sha256": outputs["state"]["sha256"],
                "deviation_sha256": outputs["deviation"]["sha256"],
                "trajectory_sha256": outputs["trajectory_tum"]["sha256"],
                "reference_sha256": reference_sha},
            "streams": {
                name: {"counts": {"MATCHED": 0, "MISSING": 1,
                                  "AMBIGUOUS": 0}}
                for name in ("state", "deviation", "trajectory", "reference")},
        }
        coverage_path = self._write(
            "ASSOCIATION_COVERAGE.source.json",
            json.dumps(coverage, sort_keys=True) + "\n")
        return {
            "coverage": coverage,
            "state_association_row_count": 1,
            "reference_association_row_count": 1,
            "outputs": {
                "callback_state_association": identity(state_path),
                "callback_reference_association": identity(reference_path),
                "association_coverage": identity(coverage_path),
            },
        }

    def test_full_role_graph_streams_repeats_and_publishes_atomically(self):
        raw_identity, archive_binding = self._telemetry()
        state = self._write("state.txt", "1 0\n")
        deviation = self._write("deviation.txt", "1 0\n")
        trajectory = self._write("trajectory.txt", "1 0\n")
        estimator_outputs = {
            "state": identity(state), "deviation": identity(deviation),
            "trajectory_tum": identity(trajectory)}
        digest = "1" * 64
        common_source = {
            "source_snapshot_sha256": "2" * 64,
            "build_provenance_id": "3" * 64}
        common_inputs = {
            "estimator_binary": {"sha256": "4" * 64},
            "config": {"sha256": "5" * 64},
            "reference_tum": {"sha256": "6" * 64},
            "event_telemetry_compressor": archive_binding["compressor"]}
        association = self._association(
            raw_identity["sha256"], estimator_outputs, "6" * 64)

        specs = []

        def add(run_id, scope, sequence, role, order=None,
                estimator_compare=None, telemetry_compare=None,
                include=False):
            manifest = {
                "schema": (corpus.KAIST_RESULT_SCHEMA
                           if scope == "KAIST_DEVELOPMENT"
                           else corpus.MH01_RESULT_SCHEMA),
                "sequence": sequence,
                "campaign_index": (order - 1 if scope == "KAIST_DEVELOPMENT"
                                   else 0),
                "status": "COMPLETED",
                "completion": {
                    "estimator_completed": True,
                    "output_validation_passed": True,
                    "evaluation_completed": True,
                    "process_group_survived_cleanup": False,
                    "roslaunch_exit_code": 0},
                "checks": {"camera_queue_drained": True,
                           "fixed_inputs_unchanged": True},
                "outputs": estimator_outputs,
                "stable_estimator_digest": {
                    "value": {"combined_stable_sha256": digest}},
                "source_provenance_before": common_source,
                "inputs_before": dict(common_inputs),
                "adapted_bag": {"sha256": "7" * 64},
                "turnsafe_t0_provenance_binding": {
                    "calibration_sha256": "8" * 64},
            }
            if role != "EXTENSION_OFF":
                manifest["turnsafe_t0"] = {
                    "identity": raw_identity,
                    "record_count": 2,
                    "callback_count": 1,
                    "event_extension": {
                        "interval_count": 1,
                        "interval_available_count": 0,
                        "group_provenance_count": 0,
                        "group_member_count": 0}}
                manifest["event_extension"] = {
                    "schema": corpus.EXTENSION_SCHEMA,
                    "capture_flags": {
                        "causal_imu_intervals": True,
                        "outcome_association_keys": True,
                        "group_bearing_provenance": True},
                    "telemetry_archive": archive_binding,
                    "associations": association}
                manifest["turnsafe_t0_provenance_binding"] = {
                    "frozen_base_sha": "c" * 40,
                    "source_sha": "a" * 40, "tree_sha": "b" * 40,
                    "source_snapshot_sha256": "2" * 64,
                    "source_dirty": True,
                    "build_provenance_id": "3" * 64,
                    "configure_manifest_sha256": "9" * 64,
                    "build_manifest_sha256": "a" * 64,
                    "binary_sha256": "4" * 64,
                    "config_sha256": "5" * 64,
                    "calibration_sha256": "8" * 64,
                    "diagnostic_schema_sha256": "b" * 64,
                    "extensions": {
                        "event_extension": {
                            "schema": corpus.EXTENSION_SCHEMA,
                            "record_version": 1,
                            "capture_flags": {
                                "causal_imu_intervals": True,
                                "outcome_association_keys": True,
                                "group_bearing_provenance": True},
                            "integration_method": "piecewise_linear_trapezoid.v1",
                            "rotation_convention": (
                                "native_gyroscope_frame_passive_left_exp_minus_"
                                "omega_diagnostic.v1"),
                            "gyro_bias_convention": (
                                "raw_wm_minus_current_callback_bias_g.v1"),
                            "clock_mapping": (
                                "t_imu_equals_t_camera_plus_dt_CAMtoIMU.v1"),
                            "association_version": (
                                "turnsafe.offline_association.v1"),
                            "canonical_ordering_version": (
                                "turnsafe.event_extension.order.v1"),
                            "image_cell_representation": (
                                "continuous_pixel_center_fraction.v1"),
                            "group_hash_algorithm": (
                                "fnv1a64_binary64_be.v1")}}}
            result_path = self._write(
                "runs/{}/sequence_result.json".format(run_id),
                json.dumps(manifest, sort_keys=True) + "\n")
            specs.append({
                "run_id": run_id, "dataset_scope": scope,
                "sequence_order": order, "sequence": sequence,
                "run_role": role, "include_in_primary_totals": include,
                "result_manifest_path": str(result_path),
                "result_manifest_schema": manifest["schema"],
                "estimator_compare_run_id": estimator_compare,
                "telemetry_compare_run_id": telemetry_compare,
                "expected_stable_digest_sha256": digest,
            })

        add("mh_off", "MH01_PARITY", "MH_01", "EXTENSION_OFF")
        add("mh_on1", "MH01_PARITY", "MH_01", "CAPTURE_ON_PRIMARY",
            estimator_compare="mh_off")
        add("mh_on2", "MH01_PARITY", "MH_01", "CAPTURE_ON_REPEAT",
            estimator_compare="mh_on1", telemetry_compare="mh_on1")
        add("rotation_fast_off", "KAIST_DEVELOPMENT",
            corpus.SEQUENCE_ORDER[0], "EXTENSION_OFF", order=1)
        add("circle_head_off", "KAIST_DEVELOPMENT",
            corpus.SEQUENCE_ORDER[2], "EXTENSION_OFF", order=3)
        primary_ids = {}
        for order, sequence in enumerate(corpus.SEQUENCE_ORDER, start=1):
            run_id = "primary_{:02d}".format(order)
            primary_ids[sequence] = run_id
            compare = ("rotation_fast_off" if order == 1 else
                       "circle_head_off" if order == 3 else None)
            add(run_id, "KAIST_DEVELOPMENT", sequence,
                "CAPTURE_ON_PRIMARY", order=order,
                estimator_compare=compare, include=True)
        for index, sequence in enumerate(corpus.SEQUENCE_ORDER[:3], start=1):
            add("repeat_{:02d}".format(index), "KAIST_DEVELOPMENT", sequence,
                "CAPTURE_ON_REPEAT", order=index,
                estimator_compare=primary_ids[sequence],
                telemetry_compare=primary_ids[sequence])

        role_input = {
            "schema_version": corpus.INPUT_SCHEMA,
            "kaist_dataset_ledger_before": {"sha256": "9" * 64},
            "kaist_dataset_ledger_after": {"sha256": "9" * 64},
            "runs": specs,
        }
        input_path = self._write(
            "aggregate_input.json", json.dumps(role_input, sort_keys=True) + "\n")
        result = corpus.aggregate(input_path, self.root, 30.0)
        self.assertEqual(result["primary_totals"]["callbacks"], 11)
        self.assertEqual(len(result["repeat_run_ids"]), 4)
        self.assertTrue((self.root / "EVENT_READY_CORPUS_MANIFEST.json").is_file())
        self.assertFalse(any(self.root.glob(".event-ready-corpus.*")))
        with (self.root / "SEQUENCE_RESULTS.csv").open(
                encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 19)
        self.assertEqual(rows[0]["telemetry_reason"],
                         "EVENT_EXTENSION_CAPTURE_DISABLED")
        self.assertEqual({row["process_cleanup_passed"] for row in rows},
                         {"True"})

    def test_typed_missingness_rejects_hidden_zero(self):
        with self.assertRaises(corpus.CorpusError):
            corpus._typed_uint({
                "status": "NOT_AVAILABLE", "reason": "MISSING", "value": 0})


if __name__ == "__main__":
    unittest.main()
