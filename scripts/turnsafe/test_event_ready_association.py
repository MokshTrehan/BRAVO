#!/usr/bin/env python3

import csv
import copy
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import event_ready_association as association


def f64_key(value):
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    return "f64:0x{:016x}".format(bits)


class EventReadyAssociationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path

    def _telemetry(self):
        records = [{"schema": "turnsafe.t0.v1",
                    "record_type": "run_header",
                    "extensions": {"event_extension": {
                        "schema": "turnsafe.t0.event_extension.v1"}}}]
        for index, (callback, expected) in enumerate(
                [(1.0, 1.125), (2.0, 2.125), (3.0, None)]):
            def row_key(stream_id):
                return ({"status": "AVAILABLE", "reason": "NONE",
                         "stream_id": stream_id,
                         "timestamp_value": expected,
                         "timestamp_key": f64_key(expected),
                         "row_ordinal": {
                             "status": "NOT_AVAILABLE",
                             "reason": "ROW_ORDINAL_OFFLINE_ONLY"}}
                        if expected is not None else
                        {"status": "NOT_AVAILABLE",
                         "reason": "STATE_TIMESTAMP_UNAVAILABLE"})
            records.append({
                "schema": "turnsafe.t0.v1",
                "record_type": "callback",
                "callback_index": index,
                "callback_timestamp_value": callback,
                "callback_timestamp_key": f64_key(callback),
                "extensions": {"event_extension": {
                    "schema": "turnsafe.t0.event_extension.v1",
                    "record_version": 1,
                    "outcome_association_keys": {
                        "callback_id": index,
                        "callback_camera_timestamp_value": callback,
                        "callback_camera_timestamp_key": f64_key(callback),
                        "estimator_initialized_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "estimator_initialized_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": True},
                        "estimator_valid_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": expected is not None},
                        "estimator_valid_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": expected is not None},
                        "state_timestamp_before": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": callback},
                        "state_timestamp_after": {
                            "status": "AVAILABLE", "reason": "NONE",
                            "value": callback},
                        "expected_state_row_key": row_key("state_estimate"),
                        "expected_deviation_row_key": row_key(
                            "state_deviation"),
                        "expected_pose_row_key": row_key("trajectory_tum"),
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
                            "calibration_sha256": "f" * 64},
                    },
                }},
            })
        return self._write(
            "telemetry.jsonl",
            "".join(json.dumps(record, sort_keys=True) + "\n"
                    for record in records))

    def test_exact_bounded_missing_and_ambiguous_are_explicit(self):
        telemetry = self._telemetry()
        state = self._write("state.txt", "1.125\n2.125004\n")
        deviation = self._write("deviation.txt", "1.125\n2.125004\n")
        trajectory = self._write(
            "trajectory.txt", "1.125 0 0 0 0 0 0 1\n2.125004 0 0 0 0 0 0 1\n")
        reference = self._write(
            "reference.txt", "1.125 0 0 0 0 0 0 1\n2.126 0 0 0 0 0 0 1\n")
        output = self.root / "output"
        coverage = association.associate(
            telemetry, state, deviation, trajectory, reference, output)
        self.assertEqual(coverage["callback_count"], 3)
        self.assertEqual(
            coverage["streams"]["state"]["counts"],
            {"MATCHED": 2, "MISSING": 1, "AMBIGUOUS": 0})
        self.assertEqual(
            coverage["streams"]["reference"]["counts"],
            {"MATCHED": 2, "MISSING": 1, "AMBIGUOUS": 0})
        with (output / "CALLBACK_STATE_ASSOCIATION.csv").open(
                newline="", encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["state_status"], "MATCHED")
        self.assertEqual(rows[1]["state_status"], "MATCHED")
        self.assertEqual(rows[2]["state_status"], "MISSING")
        self.assertEqual(
            rows[2]["state_reason"],
            "STATE_TIMESTAMP_UNAVAILABLE")
        self.assertAlmostEqual(float(rows[1]["state_time_residual_s"]),
                               0.000004, places=12)

        # Duplicate exact rows are not silently tie-broken.
        duplicate = self._write("duplicate.txt", "1.125\n1.125\n2.125\n")
        ambiguous_output = self.root / "ambiguous"
        ambiguous = association.associate(
            telemetry, duplicate, duplicate, duplicate, reference,
            ambiguous_output)
        self.assertEqual(
            ambiguous["streams"]["state"]["counts"]["AMBIGUOUS"], 1)

        # A single source row cannot silently satisfy two callbacks.
        records = [json.loads(line) for line in
                   telemetry.read_text(encoding="utf-8").splitlines()]
        for field in ("expected_state_row_key",
                      "expected_deviation_row_key", "expected_pose_row_key"):
            row = records[2]["extensions"]["event_extension"][
                "outcome_association_keys"][field]
            row["timestamp_value"] = 1.125
            row["timestamp_key"] = f64_key(1.125)
        collision = self._write(
            "collision.jsonl", "".join(
                json.dumps(record, sort_keys=True) + "\n" for record in records))
        single = self._write("single.txt", "1.125\n")
        single_trajectory = self._write(
            "single_trajectory.txt", "1.125 0 0 0 0 0 0 1\n")
        collision_coverage = association.associate(
            collision, single, single, single_trajectory, reference,
            self.root / "collision_output")
        self.assertEqual(
            collision_coverage["streams"]["state"]["counts"]["AMBIGUOUS"],
            2)

    def test_reference_is_offline_and_no_label_or_error_is_emitted(self):
        telemetry = self._telemetry()
        table = self._write("table.txt", "1.125\n2.125\n")
        reference = self._write(
            "reference.txt", "1.125 0 0 0 0 0 0 1\n2.125 0 0 0 0 0 0 1\n")
        output = self.root / "output"
        association.associate(
            telemetry, table, table, table, reference, output)
        combined = "".join(path.read_text(encoding="utf-8")
                           for path in output.iterdir())
        self.assertIn("REFERENCE_ASSOCIATION_OFFLINE_ONLY",
                      telemetry.read_text(encoding="utf-8"))
        for forbidden in ("outcome_label", "degradation_label",
                          "event_id", "final_error", "trajectory_error"):
            self.assertNotIn(f'"{forbidden}":', combined.lower())
        coverage = json.loads(
            (output / "ASSOCIATION_COVERAGE.json").read_text(
                encoding="utf-8"))
        self.assertEqual(
            coverage["association_version"],
            "turnsafe.offline_association.v1")
        self.assertEqual(
            coverage["reference_policy"]["maximum_absolute_residual_s"],
            0.01)

    def test_callback_binding_output_keys_and_runtime_firewall_fail_closed(self):
        telemetry = self._telemetry()
        records = [json.loads(line) for line in
                   telemetry.read_text(encoding="utf-8").splitlines()]
        association_keys = records[1]["extensions"]["event_extension"][
            "outcome_association_keys"]
        mutations = []
        wrong_id = copy.deepcopy(records)
        wrong_id[1]["extensions"]["event_extension"][
            "outcome_association_keys"]["callback_id"] = 9
        mutations.append(wrong_id)
        wrong_outer_key = copy.deepcopy(records)
        wrong_outer_key[1]["callback_timestamp_key"] = f64_key(9.0)
        mutations.append(wrong_outer_key)
        wrong_stream_key = copy.deepcopy(records)
        wrong_stream_key[1]["extensions"]["event_extension"][
            "outcome_association_keys"]["expected_pose_row_key"][
                "timestamp_value"] = 9.0
        mutations.append(wrong_stream_key)
        runtime_reference = copy.deepcopy(records)
        runtime_reference[1]["extensions"]["event_extension"][
            "outcome_association_keys"]["reference_association"] = {
                "status": "AVAILABLE", "value": 1.0}
        mutations.append(runtime_reference)
        runtime_label = copy.deepcopy(records)
        runtime_label[1]["extensions"]["event_extension"][
            "outcome_association_keys"]["outcome_label"] = "BAD"
        mutations.append(runtime_label)
        self.assertIn("expected_deviation_row_key", association_keys)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                path = self._write(
                    "invalid-{}.jsonl".format(index), "".join(
                        json.dumps(record, sort_keys=True) + "\n"
                        for record in mutation))
                with self.assertRaises(ValueError):
                    association._read_callbacks(path)

    def test_output_stream_count_and_timestamp_disagreement_fail_closed(self):
        telemetry = self._telemetry()
        state = self._write("state-disagree.txt", "1.125\n2.125\n")
        deviation = self._write("deviation-disagree.txt", "1.125\n")
        trajectory = self._write(
            "trajectory-disagree.txt",
            "1.125 0 0 0 0 0 0 1\n2.125 0 0 0 0 0 0 1\n")
        reference = self._write(
            "reference-disagree.txt", "1.125 0 0 0 0 0 0 1\n")
        with self.assertRaises(ValueError):
            association.associate(
                telemetry, state, deviation, trajectory, reference,
                self.root / "count-disagreement")
        deviation.write_text("1.125\n2.126\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            association.associate(
                telemetry, state, deviation, trajectory, reference,
                self.root / "timestamp-disagreement")


if __name__ == "__main__":
    unittest.main()
