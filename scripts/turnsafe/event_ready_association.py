#!/usr/bin/env python3
"""Deterministic, post-close Session-1E callback/output/reference joins.

This tool never assigns an event or outcome label.  It is deliberately a
standalone offline process: the estimator has closed before any reference file
is opened.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


SCHEMA = "turnsafe.offline_association.v1"
STATE_POLICY = "unique_nearest_expected_output_timestamp_max_5p1e-6s.v1"
REFERENCE_POLICY = "unique_nearest_expected_output_timestamp_max_0p01s.v1"
STATE_TOLERANCE_S = 5.1e-6
REFERENCE_TOLERANCE_S = 0.01


def _f64_key(value: float) -> str:
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    return f"f64:0x{bits:016x}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class TimestampRow:
    ordinal: int
    line_number: int
    timestamp: float


@dataclass(frozen=True)
class Match:
    status: str
    reason: str
    row: Optional[TimestampRow]
    residual_s: Optional[float]


def _unique_object(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    value: Dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: " + key)
        value[key] = item
    return value


def _read_timestamp_rows(path: Path) -> List[TimestampRow]:
    rows: List[TimestampRow] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            token = stripped.replace(",", " ").split()[0]
            try:
                timestamp = float(token)
            except ValueError as error:
                raise ValueError(
                    f"{path}: line {line_number}: invalid timestamp"
                ) from error
            if not math.isfinite(timestamp):
                raise ValueError(
                    f"{path}: line {line_number}: nonfinite timestamp"
                )
            rows.append(TimestampRow(len(rows), line_number, timestamp))
    if any(rows[index].timestamp < rows[index - 1].timestamp
           for index in range(1, len(rows))):
        raise ValueError(f"{path}: timestamps are not nondecreasing")
    return rows


def _unique_nearest(rows: Sequence[TimestampRow], query: Optional[float],
                    tolerance_s: float, unavailable_reason: str,
                    timestamps: Optional[Sequence[float]] = None) -> Match:
    if query is None or not math.isfinite(query):
        return Match("MISSING", unavailable_reason, None, None)
    if timestamps is None:
        timestamps = [row.timestamp for row in rows]
    position = bisect.bisect_left(timestamps, query)
    candidates: List[TimestampRow] = []
    if position < len(rows):
        candidates.append(rows[position])
    if position > 0:
        candidates.append(rows[position - 1])
    if not candidates:
        return Match("MISSING", "SOURCE_STREAM_EMPTY", None, None)
    minimum = min(abs(row.timestamp - query) for row in candidates)
    if minimum > tolerance_s:
        return Match("MISSING", "NO_ROW_WITHIN_ASSOCIATION_BOUND", None, None)
    nearest: List[TimestampRow] = []
    target_values = {candidate.timestamp for candidate in candidates
                     if abs(abs(candidate.timestamp - query) - minimum)
                     <= 1.0e-15}
    for target in target_values:
        begin = bisect.bisect_left(timestamps, target)
        end = bisect.bisect_right(timestamps, target)
        nearest.extend(rows[begin:end])
    if len(nearest) != 1:
        return Match("AMBIGUOUS", "MULTIPLE_EQUALLY_NEAREST_ROWS", None, None)
    row = nearest[0]
    return Match("MATCHED", "NONE", row, row.timestamp - query)


def _typed_number(value: object) -> Optional[float]:
    if not isinstance(value, dict) or value.get("status") != "AVAILABLE":
        return None
    number = value.get("value")
    if isinstance(number, (int, float)) and math.isfinite(float(number)):
        return float(number)
    return None


def _require_finite_number(value: object, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(float(value))):
        raise ValueError(label + " must be a finite number")
    return float(value)


def _require_timestamp_key(value: float, key: object, label: str) -> None:
    if key != _f64_key(value):
        raise ValueError(label + " timestamp key differs from its value")


def _expected_row_key(keys: Dict[str, object], field: str,
                      stream_id: str, line_label: str
                      ) -> Tuple[str, str, Optional[float]]:
    expected = keys.get(field)
    if not isinstance(expected, dict):
        raise ValueError(line_label + ": " + field + " is missing")
    status = expected.get("status")
    reason = expected.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str):
        raise ValueError(line_label + ": " + field + " status is invalid")
    if status == "AVAILABLE":
        if reason != "NONE" or expected.get("stream_id") != stream_id:
            raise ValueError(line_label + ": available " + field +
                             " metadata is invalid")
        timestamp = _require_finite_number(
            expected.get("timestamp_value"), line_label + ": " + field)
        _require_timestamp_key(timestamp, expected.get("timestamp_key"),
                               line_label + ": " + field)
        ordinal = expected.get("row_ordinal")
        if ordinal != {"status": "NOT_AVAILABLE",
                       "reason": "ROW_ORDINAL_OFFLINE_ONLY"}:
            raise ValueError(line_label + ": runtime row ordinal exposed")
        return status, reason, timestamp
    if status not in ("NOT_AVAILABLE", "NONFINITE"):
        raise ValueError(line_label + ": unexpected " + field + " status")
    if reason == "NONE" or any(name in expected for name in (
            "stream_id", "timestamp_value", "timestamp_key", "row_ordinal")):
        raise ValueError(line_label + ": unavailable " + field +
                         " is malformed")
    return status, reason, None


def _require_typed_status(value: object, label: str,
                          available_kind: str = "any") -> None:
    if not isinstance(value, dict):
        raise ValueError(label + " must be a typed object")
    status = value.get("status")
    reason = value.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str):
        raise ValueError(label + " status/reason is invalid")
    if status == "AVAILABLE":
        if reason != "NONE" or "value" not in value:
            raise ValueError(label + " available object is malformed")
        item = value["value"]
        if available_kind == "bool" and not isinstance(item, bool):
            raise ValueError(label + " Boolean value is invalid")
        if (available_kind == "uint" and
                (isinstance(item, bool) or not isinstance(item, int) or
                 item < 0)):
            raise ValueError(label + " integer value is invalid")
        if (available_kind == "number" and
                (isinstance(item, bool) or
                 not isinstance(item, (int, float)) or
                 not math.isfinite(float(item)))):
            raise ValueError(label + " numeric value is invalid")
    elif status in ("NOT_AVAILABLE", "NOT_EXPOSED", "NONFINITE"):
        if reason == "NONE" or "value" in value:
            raise ValueError(label + " unavailable object is malformed")
    else:
        raise ValueError(label + " status is invalid")


def _require_outcome_shape(keys: Dict[str, object], line_label: str) -> None:
    required = {
        "callback_id", "callback_camera_timestamp_value",
        "callback_camera_timestamp_key", "estimator_initialized_before",
        "estimator_initialized_after", "estimator_valid_before",
        "estimator_valid_after", "state_timestamp_before",
        "state_timestamp_after", "expected_state_row_key",
        "expected_deviation_row_key", "expected_pose_row_key",
        "pose_stream_write_status", "reset_status", "nonfinite_observed",
        "callback_incomplete", "run_completeness",
        "ordinary_accepted_full_factor_count",
        "ordinary_full_visual_update_accepted",
        "time_since_last_accepted_ordinary_full_update_camera_s",
        "reference_association", "identity",
    }
    if set(keys) != required:
        raise ValueError(line_label + ": association-key schema differs")
    for name in ("estimator_initialized_before", "estimator_initialized_after",
                 "estimator_valid_before", "estimator_valid_after",
                 "nonfinite_observed", "callback_incomplete",
                 "ordinary_full_visual_update_accepted"):
        _require_typed_status(keys[name], line_label + ": " + name, "bool")
    for name in ("state_timestamp_before", "state_timestamp_after",
                 "time_since_last_accepted_ordinary_full_update_camera_s"):
        _require_typed_status(keys[name], line_label + ": " + name, "number")
    _require_typed_status(
        keys["ordinary_accepted_full_factor_count"],
        line_label + ": ordinary accepted-full count", "uint")
    for name in ("pose_stream_write_status", "reset_status",
                 "run_completeness"):
        _require_typed_status(keys[name], line_label + ": " + name)
        if keys[name].get("status") == "AVAILABLE":
            raise ValueError(line_label + ": runtime output fact exposed")
    callback_incomplete = keys["callback_incomplete"]
    if not isinstance(callback_incomplete.get("completion_reason"), str):
        raise ValueError(line_label + ": callback completion reason is missing")
    identity = keys["identity"]
    required_identity = {
        "run_identity", "source_sha", "source_tree",
        "source_snapshot_sha256", "build_provenance_id", "config_sha256",
        "calibration_sha256",
    }
    if not isinstance(identity, dict) or set(identity) != required_identity:
        raise ValueError(line_label + ": association identity schema differs")
    if any(not isinstance(identity[name], (str, dict))
           for name in required_identity):
        raise ValueError(line_label + ": association identity value is invalid")


def _reject_runtime_scientific_fields(keys: Dict[str, object],
                                      line_label: str) -> None:
    forbidden = {
        "ground_truth", "path_gt", "reference", "reference_payload",
        "reference_row", "final_error", "trajectory_error", "outcome_label",
        "degradation_label", "event_id", "severity", "severity_label",
    }

    def inspect(value: object) -> None:
        if isinstance(value, dict):
            for name, item in value.items():
                lowered = str(name).lower()
                normalized = "".join(character for character in lowered
                                     if character.isalnum())
                forbidden_normalized = {
                    "groundtruth", "pathgt", "runtimegroundtruth", "gtpose",
                    "referencepayload", "referencerow", "finalerror",
                    "trajectoryerror", "outcomelabel", "degradationlabel",
                    "eventid", "eventids", "severity", "severitylabel",
                }
                if lowered in forbidden or normalized in forbidden_normalized:
                    raise ValueError(line_label +
                                     ": runtime scientific field is forbidden: " +
                                     str(name))
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)

    inspect(keys)


def _read_callbacks(path: Path) -> List[Dict[str, object]]:
    callbacks: List[Dict[str, object]] = []
    header_seen = False
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                record = json.loads(
                    line,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError("nonfinite JSON constant: " + value)),
                    object_pairs_hook=_unique_object,
                )
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}: line {line_number}: invalid JSON"
                ) from error
            if not isinstance(record, dict):
                raise ValueError(f"{path}: line {line_number}: object required")
            if line_number == 1:
                if (record.get("schema") != "turnsafe.t0.v1" or
                        record.get("record_type") != "run_header"):
                    raise ValueError(f"{path}: first record is not a v1 header")
                header_seen = True
                continue
            if (record.get("schema") != "turnsafe.t0.v1" or
                    record.get("record_type") != "callback"):
                raise ValueError(f"{path}: line {line_number}: callback required")
            callback_id = record.get("callback_index")
            if callback_id != len(callbacks):
                raise ValueError(
                    f"{path}: line {line_number}: noncontiguous callback index"
                )
            extension = (record.get("extensions", {})
                         .get("event_extension", {}))
            if (extension.get("schema") != "turnsafe.t0.event_extension.v1" or
                    extension.get("record_version") != 1):
                raise ValueError(
                    f"{path}: line {line_number}: event extension missing"
                )
            keys = extension.get("outcome_association_keys")
            if not isinstance(keys, dict):
                raise ValueError(
                    f"{path}: line {line_number}: association keys missing"
                )
            line_label = f"{path}: line {line_number}"
            _reject_runtime_scientific_fields(record, line_label)
            _require_outcome_shape(keys, line_label)
            callback_timestamp = _require_finite_number(
                record.get("callback_timestamp_value"),
                line_label + ": callback timestamp")
            _require_timestamp_key(callback_timestamp,
                                   record.get("callback_timestamp_key"),
                                   line_label + ": callback")
            if keys.get("callback_id") != callback_id:
                raise ValueError(line_label + ": inner callback ID differs")
            inner_timestamp = _require_finite_number(
                keys.get("callback_camera_timestamp_value"),
                line_label + ": inner callback timestamp")
            _require_timestamp_key(
                inner_timestamp, keys.get("callback_camera_timestamp_key"),
                line_label + ": inner callback")
            if (_f64_key(inner_timestamp) != _f64_key(callback_timestamp)):
                raise ValueError(line_label +
                                 ": inner callback timestamp differs")
            expected_status, expected_reason, expected_timestamp = (
                _expected_row_key(keys, "expected_state_row_key",
                                  "state_estimate", line_label))
            for field, stream_id in (
                    ("expected_deviation_row_key", "state_deviation"),
                    ("expected_pose_row_key", "trajectory_tum")):
                status, reason, timestamp = _expected_row_key(
                    keys, field, stream_id, line_label)
                if ((status, reason) != (expected_status, expected_reason) or
                        ((timestamp is None) !=
                         (expected_timestamp is None)) or
                        (timestamp is not None and
                         _f64_key(timestamp) !=
                         _f64_key(expected_timestamp))):
                    raise ValueError(line_label +
                                     ": expected output row keys disagree")
            if keys.get("reference_association") != {
                    "status": "NOT_AVAILABLE",
                    "reason": "REFERENCE_ASSOCIATION_OFFLINE_ONLY"}:
                raise ValueError(line_label +
                                 ": runtime reference firewall violated")
            callbacks.append({
                "callback_id": callback_id,
                "callback_timestamp": float(callback_timestamp),
                "expected_output_timestamp": expected_timestamp,
                "expected_output_timestamp_status": expected_status,
                "expected_output_timestamp_reason": expected_reason,
            })
    if not header_seen:
        raise ValueError(f"{path}: empty telemetry")
    return callbacks


def _enforce_one_to_one(matches: Sequence[Match]) -> List[Match]:
    ordinals: Dict[int, int] = {}
    for match in matches:
        if match.status == "MATCHED" and match.row is not None:
            ordinals[match.row.ordinal] = ordinals.get(match.row.ordinal, 0) + 1
    return [
        (Match("AMBIGUOUS", "SOURCE_ROW_MATCHED_MULTIPLE_CALLBACKS", None, None)
         if match.status == "MATCHED" and match.row is not None and
         ordinals.get(match.row.ordinal, 0) != 1 else match)
        for match in matches
    ]


def _row_columns(prefix: str, match: Match) -> Dict[str, object]:
    result: Dict[str, object] = {
        f"{prefix}_status": match.status,
        f"{prefix}_reason": match.reason,
        f"{prefix}_row_ordinal": "",
        f"{prefix}_source_line_number": "",
        f"{prefix}_timestamp_value": "",
        f"{prefix}_timestamp_key": "",
        f"{prefix}_time_residual_s": "",
    }
    if match.row is not None:
        result.update({
            f"{prefix}_row_ordinal": match.row.ordinal,
            f"{prefix}_source_line_number": match.row.line_number,
            f"{prefix}_timestamp_value": format(match.row.timestamp, ".17g"),
            f"{prefix}_timestamp_key": _f64_key(match.row.timestamp),
            f"{prefix}_time_residual_s": format(match.residual_s, ".17g"),
        })
    return result


def _atomic_csv(path: Path, fieldnames: Sequence[str],
                rows: Iterable[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames,
                                    lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _atomic_json(path: Path, value: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _residual_summary(matches: Sequence[Match]) -> Dict[str, object]:
    residuals = sorted(abs(match.residual_s) for match in matches
                       if match.status == "MATCHED" and
                       match.residual_s is not None)
    if not residuals:
        return {"status": "NOT_AVAILABLE", "reason": "NO_MATCHED_ROWS"}

    def nearest_rank(probability: float) -> float:
        rank = max(1, math.ceil(probability * len(residuals)))
        return residuals[rank - 1]

    return {
        "status": "AVAILABLE",
        "count": len(residuals),
        "absolute_residual_s": {
            "min": residuals[0],
            "max": residuals[-1],
            "p50": nearest_rank(0.50),
            "p90": nearest_rank(0.90),
            "p99": nearest_rank(0.99),
        },
    }


def associate(telemetry: Path, state: Path, deviation: Path,
              trajectory: Path, reference: Path,
              output_dir: Path) -> Dict[str, object]:
    callbacks = _read_callbacks(telemetry)
    state_rows = _read_timestamp_rows(state)
    deviation_rows = _read_timestamp_rows(deviation)
    trajectory_rows = _read_timestamp_rows(trajectory)
    # This is the first reference-content read in this process.  Callers must
    # invoke the process only after estimator/ROS closure.
    reference_rows = _read_timestamp_rows(reference)
    if not (len(state_rows) == len(deviation_rows) == len(trajectory_rows)):
        raise ValueError("state/deviation/trajectory row counts differ")
    for ordinal, (state_row, deviation_row, trajectory_row) in enumerate(
            zip(state_rows, deviation_rows, trajectory_rows)):
        if not (state_row.timestamp == deviation_row.timestamp ==
                trajectory_row.timestamp):
            raise ValueError(
                "state/deviation/trajectory timestamp mismatch at row {}".format(
                    ordinal))
    state_timestamps = [row.timestamp for row in state_rows]
    deviation_timestamps = [row.timestamp for row in deviation_rows]
    trajectory_timestamps = [row.timestamp for row in trajectory_rows]
    reference_timestamps = [row.timestamp for row in reference_rows]
    reference_sha256 = _sha256(reference)

    state_matches: List[Match] = []
    deviation_matches: List[Match] = []
    trajectory_matches: List[Match] = []
    reference_matches: List[Match] = []
    state_output_rows: List[Dict[str, object]] = []
    reference_output_rows: List[Dict[str, object]] = []
    for callback in callbacks:
        expected = callback["expected_output_timestamp"]
        state_match = _unique_nearest(
            state_rows, expected, STATE_TOLERANCE_S,
            str(callback["expected_output_timestamp_reason"]), state_timestamps)
        deviation_match = _unique_nearest(
            deviation_rows, expected, STATE_TOLERANCE_S,
            str(callback["expected_output_timestamp_reason"]),
            deviation_timestamps)
        trajectory_match = _unique_nearest(
            trajectory_rows, expected, STATE_TOLERANCE_S,
            str(callback["expected_output_timestamp_reason"]),
            trajectory_timestamps)
        reference_match = _unique_nearest(
            reference_rows, expected,
            REFERENCE_TOLERANCE_S,
            str(callback["expected_output_timestamp_reason"]),
            reference_timestamps)
        state_matches.append(state_match)
        deviation_matches.append(deviation_match)
        trajectory_matches.append(trajectory_match)
        reference_matches.append(reference_match)
    state_matches = _enforce_one_to_one(state_matches)
    deviation_matches = _enforce_one_to_one(deviation_matches)
    trajectory_matches = _enforce_one_to_one(trajectory_matches)
    reference_matches = _enforce_one_to_one(reference_matches)

    for callback, state_match, deviation_match, trajectory_match, reference_match in zip(
            callbacks, state_matches, deviation_matches, trajectory_matches,
            reference_matches):
        expected = callback["expected_output_timestamp"]
        common = {
            "association_version": SCHEMA,
            "policy_id": STATE_POLICY,
            "callback_id": callback["callback_id"],
            "callback_camera_timestamp_value": format(
                callback["callback_timestamp"], ".17g"),
            "callback_camera_timestamp_key": _f64_key(
                callback["callback_timestamp"]),
            "expected_output_timestamp_value": (
                "" if expected is None else format(expected, ".17g")),
            "expected_output_timestamp_key": (
                "" if expected is None else _f64_key(expected)),
            "expected_output_timestamp_status": callback[
                "expected_output_timestamp_status"],
            "expected_output_timestamp_reason": callback[
                "expected_output_timestamp_reason"],
        }
        common.update(_row_columns("state", state_match))
        common.update(_row_columns("deviation", deviation_match))
        common.update(_row_columns("trajectory", trajectory_match))
        state_output_rows.append(common)

        reference_common = {
            "association_version": SCHEMA,
            "policy_id": REFERENCE_POLICY,
            "callback_id": callback["callback_id"],
            "callback_camera_timestamp_value": format(
                callback["callback_timestamp"], ".17g"),
            "callback_camera_timestamp_key": _f64_key(
                callback["callback_timestamp"]),
            "reference_query_timestamp_value": (
                "" if expected is None else format(expected, ".17g")),
            "reference_query_timestamp_key": (
                "" if expected is None else _f64_key(expected)),
            "reference_query_timestamp_status": callback[
                "expected_output_timestamp_status"],
            "reference_query_timestamp_reason": callback[
                "expected_output_timestamp_reason"],
            "reference_source_sha256": reference_sha256,
            "reference_time_base": (
                "estimator_output_imu_clock_to_reference_header"),
        }
        reference_common.update(_row_columns("reference", reference_match))
        reference_output_rows.append(reference_common)

    state_fields = list(state_output_rows[0].keys()) if state_output_rows else [
        "association_version", "policy_id", "callback_id",
        "callback_camera_timestamp_value", "callback_camera_timestamp_key",
        "expected_output_timestamp_value", "expected_output_timestamp_key",
        "expected_output_timestamp_status", "expected_output_timestamp_reason",
    ]
    reference_fields = (list(reference_output_rows[0].keys())
                        if reference_output_rows else [
                            "association_version", "policy_id", "callback_id",
                            "callback_camera_timestamp_value",
                            "callback_camera_timestamp_key",
                            "reference_source_sha256", "reference_time_base",
                        ])
    _atomic_csv(output_dir / "CALLBACK_STATE_ASSOCIATION.csv",
                state_fields, state_output_rows)
    _atomic_csv(output_dir / "CALLBACK_REFERENCE_ASSOCIATION.csv",
                reference_fields, reference_output_rows)

    def counts(matches: Sequence[Match]) -> Dict[str, int]:
        return {status: sum(match.status == status for match in matches)
                for status in ("MATCHED", "MISSING", "AMBIGUOUS")}

    coverage: Dict[str, object] = {
        "schema_version": "turnsafe.association_coverage.v1",
        "association_version": SCHEMA,
        "callback_count": len(callbacks),
        "state_policy": {
            "id": STATE_POLICY,
            "maximum_absolute_residual_s": STATE_TOLERANCE_S,
            "time_base": "expected_estimator_output_timestamp",
        },
        "reference_policy": {
            "id": REFERENCE_POLICY,
            "maximum_absolute_residual_s": REFERENCE_TOLERANCE_S,
            "time_base": "expected_estimator_output_imu_clock_to_reference_header",
            "query_time_base": (
                "expected_estimator_output_imu_clock_from_causal_time_offset"),
            "reference_sha256": reference_sha256,
        },
        "streams": {
            "state": {"counts": counts(state_matches),
                      "residuals": _residual_summary(state_matches)},
            "deviation": {"counts": counts(deviation_matches),
                          "residuals": _residual_summary(deviation_matches)},
            "trajectory": {"counts": counts(trajectory_matches),
                           "residuals": _residual_summary(trajectory_matches)},
            "reference": {"counts": counts(reference_matches),
                          "residuals": _residual_summary(reference_matches)},
        },
        "prohibited_outputs_absent": {
            "event_ids": True,
            "outcome_labels": True,
            "degradation_labels": True,
            "error_metrics": True,
        },
        "inputs": {
            "telemetry_sha256": _sha256(telemetry),
            "state_sha256": _sha256(state),
            "deviation_sha256": _sha256(deviation),
            "trajectory_sha256": _sha256(trajectory),
            "reference_sha256": reference_sha256,
        },
    }
    _atomic_json(output_dir / "ASSOCIATION_COVERAGE.json", coverage)
    return coverage


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--deviation", type=Path, required=True)
    parser.add_argument("--trajectory", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    associate(arguments.telemetry.resolve(), arguments.state.resolve(),
              arguments.deviation.resolve(), arguments.trajectory.resolve(),
              arguments.reference.resolve(), arguments.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
