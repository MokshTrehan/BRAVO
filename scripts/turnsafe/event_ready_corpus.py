#!/usr/bin/env python3
"""Aggregate a threshold-free Session-1E event-ready development corpus.

The tool consumes completed, immutable per-run manifests.  It validates and
streams their lossless telemetry archives, concatenates post-close association
tables, and proves estimator/telemetry parity.  It never reads estimator input
bags or reference trajectories and never assigns an event or outcome label.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "turnsafe"
SESSION_SCRATCH_ROOT = REPO_ROOT / ".turnsafe-work" / "session_01e"

INPUT_SCHEMA = "turnsafe.session1e.aggregate_input.v1"
MANIFEST_SCHEMA = "turnsafe.session1e.event_ready_corpus_manifest.v1"
EXTENSION_SCHEMA = "turnsafe.t0.event_extension.v1"
KAIST_RESULT_SCHEMA = "turnsafe.kaist_vio_campaign.sequence.v1"
MH01_RESULT_SCHEMA = "turnsafe.session1e.mh01_run.v1"
SEQUENCE_ORDER: Tuple[str, ...] = (
    "rotation/rotation_fast.bag",
    "rotation/rotation.bag",
    "circle/circle_head.bag",
    "infinite/infinite_head.bag",
    "square/square_head.bag",
    "circle/circle_fast.bag",
    "infinite/infinite_fast.bag",
    "square/square_fast.bag",
    "circle/circle.bag",
    "infinite/infinite.bag",
    "square/square.bag",
)
REPEAT_SEQUENCES = frozenset(SEQUENCE_ORDER[:3])
RUN_ROLES = frozenset(
    ("EXTENSION_OFF", "CAPTURE_ON_PRIMARY", "CAPTURE_ON_REPEAT")
)
DATASET_SCOPES = frozenset(("MH01_PARITY", "KAIST_DEVELOPMENT"))
FORBIDDEN_COMPONENTS = ("holdout", "private")
FORBIDDEN_OUTPUT_KEYS = frozenset(
    (
        "event_id", "severity", "outcome_label", "degraded_label",
        "failure_label", "consensus_acceptance", "spatial_acceptance",
        "rank_acceptance", "conditioning_acceptance", "winner",
        "certificate", "t1", "t2", "t3", "branch",
    )
)


class CorpusError(RuntimeError):
    """Fail-closed corpus aggregation error."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.relative_to(REPO_ROOT)),
        "size_bytes": stat.st_size,
        "sha256": _sha256(path),
    }


def _reject_path(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    lowered = [component.lower() for component in resolved.parts]
    if any(any(word in component for word in FORBIDDEN_COMPONENTS)
           for component in lowered):
        raise CorpusError("{} contains a forbidden path component".format(label))
    for index in range(len(lowered) - 1):
        if lowered[index:index + 2] == ["scripts", "cp2"]:
            raise CorpusError("{} enters protected scripts/cp2".format(label))
    allowed_roots = [ARTIFACT_ROOT.resolve(strict=True)]
    if SESSION_SCRATCH_ROOT.exists():
        allowed_roots.append(SESSION_SCRATCH_ROOT.resolve(strict=True))
    if not any(root == resolved or root in resolved.parents
               for root in allowed_roots):
        raise CorpusError("{} is outside the Session-1E evidence roots".format(label))
    if not resolved.is_file() or resolved.is_symlink():
        raise CorpusError("{} is not a regular non-symlink file".format(label))
    return resolved


def _strict_json(path: Path, label: str) -> Mapping[str, Any]:
    def reject_constant(value: str) -> None:
        raise CorpusError("{} contains nonfinite token {}".format(label, value))

    def reject_duplicate(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CorpusError("{} contains duplicate key {}".format(label, key))
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding="utf-8"),
                       parse_constant=reject_constant,
                       object_pairs_hook=reject_duplicate)
    _require_recursive_finite(value, label)
    if not isinstance(value, dict):
        raise CorpusError("{} is not a JSON object".format(label))
    return value


def _require_recursive_finite(value: Any, label: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise CorpusError("{} contains a nonfinite number".format(label))
    if isinstance(value, dict):
        for item in value.values():
            _require_recursive_finite(item, label)
    elif isinstance(value, list):
        for item in value:
            _require_recursive_finite(item, label)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
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


def _atomic_text(path: Path, value: str) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _format_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    number = float(value)
    if not math.isfinite(number):
        raise CorpusError("nonfinite numeric corpus value")
    return format(number, ".17g")


def _f64_key(value: float) -> str:
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    return "f64:0x{:016x}".format(bits)


def _typed_scalar(value: Any) -> Tuple[str, str, str]:
    if not isinstance(value, dict):
        raise CorpusError("typed scalar is not an object")
    status = value.get("status")
    reason = value.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str):
        raise CorpusError("typed scalar status/reason is invalid")
    if status == "AVAILABLE":
        if reason != "NONE":
            raise CorpusError("available typed scalar reason is not NONE")
        rendered = _format_number(value.get("value"))
        if not rendered:
            raise CorpusError("available typed scalar lacks a finite value")
        return status, reason, rendered
    if status not in ("NOT_AVAILABLE", "NONFINITE", "NOT_EXPOSED") or \
            reason == "NONE":
        raise CorpusError("unavailable typed scalar status/reason is invalid")
    if "value" in value:
        raise CorpusError("unavailable typed scalar contains a value")
    return status, reason, ""


def _typed_vector(value: Any, length: int) -> Tuple[str, str, str]:
    if not isinstance(value, dict):
        raise CorpusError("typed vector is not an object")
    status = value.get("status")
    reason = value.get("reason")
    if not isinstance(status, str) or not isinstance(reason, str):
        raise CorpusError("typed vector status/reason is invalid")
    if status != "AVAILABLE":
        if status not in ("NOT_AVAILABLE", "NONFINITE", "NOT_EXPOSED") or \
                reason == "NONE":
            raise CorpusError("unavailable typed vector status/reason is invalid")
        if "value" in value:
            raise CorpusError("unavailable typed vector contains a value")
        return status, reason, ""
    if reason != "NONE":
        raise CorpusError("available typed vector reason is not NONE")
    vector = value.get("value")
    if not isinstance(vector, list) or len(vector) != length:
        raise CorpusError("typed vector length is invalid")
    rendered = [_format_number(item) for item in vector]
    if any(not item for item in rendered):
        raise CorpusError("typed vector contains an invalid number")
    return status, reason, ";".join(rendered)


def _typed_uint(value: Any) -> Tuple[str, str, str]:
    status, reason, rendered = _typed_scalar(value)
    if status == "AVAILABLE":
        raw = value.get("value")
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise CorpusError("available typed integer is invalid")
        rendered = str(raw)
    return status, reason, rendered


def _summary_fields(summary: Any) -> Dict[str, str]:
    if not isinstance(summary, dict):
        raise CorpusError("gyro summary is not an object")
    status = summary.get("status")
    reason = summary.get("reason")
    if status not in ("AVAILABLE", "NOT_AVAILABLE", "NONFINITE") or \
            not isinstance(reason, str) or \
            ((status == "AVAILABLE") != (reason == "NONE")):
        raise CorpusError("gyro summary status/reason is invalid")
    result = {"status": str(status), "reason": str(reason)}
    scalar_names = (
        "max_norm_rad_s", "rms_norm_rad_s", "integral_norm_rad",
        "delta_rotation_angle_rad",
    )
    vector_names = (
        ("mean_omega_xyz_rad_s", 3),
        ("integral_omega_xyz_rad", 3),
        ("delta_rotation_jpl_xyzw", 4),
    )
    if status == "AVAILABLE":
        for name in scalar_names:
            rendered = _format_number(summary.get(name))
            if not rendered:
                raise CorpusError("available gyro summary lacks {}".format(name))
            result[name] = rendered
        for name, length in vector_names:
            vector = summary.get(name)
            if not isinstance(vector, list) or len(vector) != length:
                raise CorpusError("available gyro summary vector is invalid")
            rendered = [_format_number(item) for item in vector]
            if any(not item for item in rendered):
                raise CorpusError("available gyro summary vector is nonnumeric")
            result[name] = ";".join(rendered)
        matrix = summary.get("delta_rotation_matrix_row_major")
        if not isinstance(matrix, list) or len(matrix) != 9 or any(
                not _format_number(item) for item in matrix):
            raise CorpusError("available gyro rotation matrix is invalid")
        quaternion = summary.get("delta_rotation_jpl_xyzw")
        quaternion_norm = math.sqrt(sum(float(item) * float(item)
                                        for item in quaternion))
        if (abs(quaternion_norm - 1.0) > 1.0e-9 or
                float(quaternion[3]) < -1.0e-15):
            raise CorpusError("available gyro quaternion is not canonical unit")
        angle = float(summary.get("delta_rotation_angle_rad"))
        if angle < 0.0 or angle > math.pi + 1.0e-12:
            raise CorpusError("available gyro rotation angle is invalid")
        axis_status, axis_reason, axis_value = _typed_vector(
            summary.get("delta_rotation_axis"), 3
        )
        result.update({"axis_status": axis_status, "axis_reason": axis_reason,
                       "axis_xyz": axis_value})
    else:
        for name in scalar_names:
            result[name] = ""
        for name, _ in vector_names:
            result[name] = ""
        result.update({"axis_status": "NOT_AVAILABLE",
                       "axis_reason": str(reason), "axis_xyz": ""})
    return result


INTERVAL_FIELDS = (
    "sequence_order", "run_id", "sequence", "callback_id",
    "callback_camera_timestamp_value", "callback_camera_timestamp_key",
    "gyro_interval_id", "interval_status", "interval_reason",
    "camera_frame_count", "camera_frame_ids",
    "previous_callback_status", "previous_callback_reason",
    "previous_callback_value_s", "interval_start_camera_status",
    "interval_start_camera_reason", "interval_start_camera_value_s",
    "interval_end_camera_status", "interval_end_camera_reason",
    "interval_end_camera_value_s", "duration_status", "duration_reason",
    "duration_value_s", "dt_CAMtoIMU_status", "dt_CAMtoIMU_reason",
    "dt_CAMtoIMU_value_s", "interval_start_imu_status",
    "interval_start_imu_reason", "interval_start_imu_value_s",
    "interval_end_imu_status", "interval_end_imu_reason",
    "interval_end_imu_value_s",
)

AVAILABILITY_FIELDS = (
    "sequence_order", "run_id", "sequence", "callback_id",
    "gyro_interval_id", "interval_status", "interval_reason",
    "source_sample_count_status", "source_sample_count_reason",
    "source_sample_count_value", "first_support_status", "first_support_reason",
    "first_support_value_s", "last_support_status", "last_support_reason",
    "last_support_value_s", "start_endpoint_status", "start_endpoint_reason",
    "start_lower_status", "start_lower_reason", "start_lower_value_s",
    "start_upper_status", "start_upper_reason", "start_upper_value_s",
    "end_endpoint_status", "end_endpoint_reason", "end_lower_status",
    "end_lower_reason", "end_lower_value_s", "end_upper_status",
    "end_upper_reason", "end_upper_value_s", "maximum_gap_status",
    "maximum_gap_reason", "maximum_gap_value_s", "coverage_status",
    "coverage_reason", "coverage_value", "bias_status", "bias_reason",
    "bias_xyz_rad_s", "knot_status", "knot_reason", "knot_count",
    "integration_method",
    "raw_status", "raw_reason", "raw_max_norm_rad_s", "raw_rms_norm_rad_s",
    "raw_mean_omega_xyz_rad_s", "raw_integral_norm_rad",
    "raw_integral_omega_xyz_rad", "raw_delta_rotation_jpl_xyzw",
    "raw_delta_rotation_angle_rad", "raw_axis_status", "raw_axis_reason",
    "raw_axis_xyz", "bias_corrected_status", "bias_corrected_reason",
    "bias_corrected_max_norm_rad_s", "bias_corrected_rms_norm_rad_s",
    "bias_corrected_mean_omega_xyz_rad_s",
    "bias_corrected_integral_norm_rad",
    "bias_corrected_integral_omega_xyz_rad",
    "bias_corrected_delta_rotation_jpl_xyzw",
    "bias_corrected_delta_rotation_angle_rad",
    "bias_corrected_axis_status", "bias_corrected_axis_reason",
    "bias_corrected_axis_xyz",
)


def _interval_rows(
    prefix: Mapping[str, Any], record: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    callback_id = record.get("callback_index")
    event = record.get("extensions", {}).get("event_extension", {})
    if (record.get("schema") != "turnsafe.t0.v1" or
            record.get("record_type") != "callback" or
            event.get("schema") != EXTENSION_SCHEMA or
            event.get("record_version") != 1):
        raise CorpusError("event-ready callback schema is invalid")
    interval = event.get("causal_imu_interval")
    if not isinstance(interval, dict):
        raise CorpusError("primary callback lacks causal IMU interval")
    if interval.get("status") not in ("AVAILABLE", "NOT_AVAILABLE") or \
            ((interval.get("status") == "AVAILABLE") !=
             (interval.get("reason") == "NONE")):
        raise CorpusError("interval status/reason is invalid")
    if (isinstance(callback_id, bool) or not isinstance(callback_id, int) or
            callback_id < 0 or interval.get("callback_id") != callback_id or
            interval.get("gyro_interval_id") != callback_id):
        raise CorpusError("callback/interval identity is invalid")
    if interval.get("units") != {
            "time": "s", "angular_rate": "rad/s", "rotation": "rad"} or \
            interval.get("frame_convention") != {
                "raw": "native_gyroscope_measurement_frame",
                "bias_corrected": (
                    "native_gyroscope_measurement_frame_wm_minus_bias_g"),
                "delta": (
                    "native_gyroscope_frame_passive_left_composed_"
                    "Exp_minus_omega_dt_diagnostic"),
            }:
        raise CorpusError("interval unit/frame convention is invalid")
    frame_ids = interval.get("camera_frame_ids")
    if (not isinstance(frame_ids, list) or
            any(isinstance(value, bool) or not isinstance(value, int) or
                value < 0 for value in frame_ids) or
            frame_ids != sorted(set(frame_ids))):
        raise CorpusError("interval camera-frame IDs are invalid")
    frontend = record.get("frontend_cameras")
    if (not isinstance(frontend, list) or
            [frame.get("camera_id") for frame in frontend] != frame_ids):
        raise CorpusError("callback frontend/interval frame IDs differ")
    for frame in frontend:
        if not isinstance(frame, dict):
            raise CorpusError("frontend camera record is invalid")
        reference = frame.get("extensions", {}).get("event_extension", {})
        if (reference.get("schema") != EXTENSION_SCHEMA or
                reference.get("gyro_interval_id") !=
                interval.get("gyro_interval_id")):
            raise CorpusError("camera frame interval reference differs")
    callback_timestamp_rendered = _format_number(
        record.get("callback_timestamp_value"))
    if (not callback_timestamp_rendered or
            record.get("callback_timestamp_key") !=
            _f64_key(float(record["callback_timestamp_value"]))):
        raise CorpusError("callback timestamp identity is invalid")
    current_callback = _typed_scalar(
        interval.get("current_callback_camera_timestamp"))
    if (current_callback[0] != "AVAILABLE" or
            _f64_key(float(current_callback[2])) !=
            record.get("callback_timestamp_key")):
        raise CorpusError("interval current callback timestamp differs")
    if (interval.get("clock_equation") !=
            "t_imu=t_camera+dt_CAMtoIMU" or
            interval.get("integration_method") !=
            "piecewise_linear_trapezoid.v1"):
        raise CorpusError("interval clock/integration convention is invalid")
    previous = _typed_scalar(
        interval.get("previous_processed_callback_camera_timestamp")
    )
    start_camera = _typed_scalar(interval.get("interval_start_camera_s"))
    end_camera = _typed_scalar(interval.get("interval_end_camera_s"))
    duration = _typed_scalar(interval.get("duration_s"))
    offset = _typed_scalar(interval.get("dt_CAMtoIMU_s"))
    start_imu = _typed_scalar(interval.get("interval_start_imu_s"))
    end_imu = _typed_scalar(interval.get("interval_end_imu_s"))
    interval_row: Dict[str, Any] = dict(prefix)
    interval_row.update({
        "callback_id": callback_id,
        "callback_camera_timestamp_value": callback_timestamp_rendered,
        "callback_camera_timestamp_key": record.get("callback_timestamp_key"),
        "gyro_interval_id": interval.get("gyro_interval_id"),
        "interval_status": interval.get("status"),
        "interval_reason": interval.get("reason"),
        "camera_frame_count": len(frame_ids),
        "camera_frame_ids": ";".join(str(value) for value in frame_ids),
        "previous_callback_status": previous[0],
        "previous_callback_reason": previous[1],
        "previous_callback_value_s": previous[2],
        "interval_start_camera_status": start_camera[0],
        "interval_start_camera_reason": start_camera[1],
        "interval_start_camera_value_s": start_camera[2],
        "interval_end_camera_status": end_camera[0],
        "interval_end_camera_reason": end_camera[1],
        "interval_end_camera_value_s": end_camera[2],
        "duration_status": duration[0], "duration_reason": duration[1],
        "duration_value_s": duration[2],
        "dt_CAMtoIMU_status": offset[0], "dt_CAMtoIMU_reason": offset[1],
        "dt_CAMtoIMU_value_s": offset[2],
        "interval_start_imu_status": start_imu[0],
        "interval_start_imu_reason": start_imu[1],
        "interval_start_imu_value_s": start_imu[2],
        "interval_end_imu_status": end_imu[0],
        "interval_end_imu_reason": end_imu[1],
        "interval_end_imu_value_s": end_imu[2],
    })

    support = interval.get("support")
    knots = interval.get("knots")
    if not isinstance(support, dict) or not isinstance(knots, dict):
        raise CorpusError("interval support/knots are invalid")
    if support.get("endpoint_policy") != \
            "exact_or_linear_bracket_no_extrapolation.v1":
        raise CorpusError("interval endpoint policy is invalid")
    first = _typed_scalar(support.get("first_timestamp_s"))
    sample_count = _typed_uint(support.get("source_sample_count"))
    last = _typed_scalar(support.get("last_timestamp_s"))
    maximum_gap = _typed_scalar(support.get("maximum_internal_sample_gap_s"))
    coverage = _typed_scalar(support.get("coverage_fraction"))
    bias = _typed_vector(interval.get("gyro_bias_snapshot_xyz_rad_s"), 3)
    start_endpoint = support.get("start_endpoint")
    end_endpoint = support.get("end_endpoint")
    if not isinstance(start_endpoint, dict) or not isinstance(end_endpoint, dict):
        raise CorpusError("interval endpoint evidence is invalid")
    for endpoint in (start_endpoint, end_endpoint):
        status = endpoint.get("status")
        reason = endpoint.get("reason")
        if (status not in ("EXACT_SAMPLE", "LINEAR_INTERPOLATED",
                           "UNSUPPORTED") or
                not isinstance(reason, str) or
                ((status != "UNSUPPORTED") != (reason == "NONE"))):
            raise CorpusError("interval endpoint status/reason is invalid")
    start_lower = _typed_scalar(start_endpoint.get("lower_timestamp"))
    start_upper = _typed_scalar(start_endpoint.get("upper_timestamp"))
    end_lower = _typed_scalar(end_endpoint.get("lower_timestamp"))
    end_upper = _typed_scalar(end_endpoint.get("upper_timestamp"))
    raw = _summary_fields(interval.get("raw_summary"))
    corrected = _summary_fields(interval.get("bias_corrected_summary"))
    knot_status = knots.get("status")
    knot_reason = knots.get("reason")
    if knot_status == "AVAILABLE":
        if knot_reason != "NONE":
            raise CorpusError("available knot reason is invalid")
        timestamps = knots.get("timestamp_value_s")
        timestamp_keys = knots.get("timestamp_key")
        raw_knots = knots.get("raw_omega_xyz_rad_s")
        corrected_knots = knots.get("bias_corrected_omega_xyz_rad_s")
        if (not isinstance(timestamps, list) or len(timestamps) < 2 or
                not isinstance(timestamp_keys, list) or
                not isinstance(raw_knots, list) or
                not isinstance(corrected_knots, list) or
                not (len(timestamps) == len(timestamp_keys) ==
                     len(raw_knots) == len(corrected_knots))):
            raise CorpusError("available knot arrays are invalid")
        numeric_times = [float(_format_number(value)) for value in timestamps]
        if any(numeric_times[index] <= numeric_times[index - 1]
               for index in range(1, len(numeric_times))) or any(
                   key != _f64_key(value)
                   for key, value in zip(timestamp_keys, numeric_times)):
            raise CorpusError("knot timestamp order/keys are invalid")
        for raw_value, corrected_value in zip(raw_knots, corrected_knots):
            if (not isinstance(raw_value, list) or len(raw_value) != 3 or
                    not isinstance(corrected_value, list) or
                    len(corrected_value) != 3 or any(
                        not _format_number(item)
                        for item in raw_value + corrected_value)):
                raise CorpusError("knot angular-rate vectors are invalid")
        if (start_imu[0] != "AVAILABLE" or end_imu[0] != "AVAILABLE" or
                _f64_key(numeric_times[0]) !=
                _f64_key(float(start_imu[2])) or
                _f64_key(numeric_times[-1]) !=
                _f64_key(float(end_imu[2]))):
            raise CorpusError("knot endpoints differ from interval")
    elif (knot_status not in ("NOT_AVAILABLE", "NONFINITE") or
          not isinstance(knot_reason, str) or knot_reason == "NONE" or
          any(name in knots for name in (
              "timestamp_value_s", "timestamp_key", "raw_omega_xyz_rad_s",
              "bias_corrected_omega_xyz_rad_s"))):
        raise CorpusError("unavailable knot object is invalid")
    if interval.get("status") == "AVAILABLE":
        if (knot_status != "AVAILABLE" or raw["status"] != "AVAILABLE" or
                corrected["status"] != "AVAILABLE"):
            raise CorpusError("available interval lacks integrated primitives")
        for typed in (previous, start_camera, end_camera, duration, offset,
                      start_imu, end_imu, sample_count, first, last,
                      maximum_gap, coverage, bias):
            if typed[0] != "AVAILABLE":
                raise CorpusError("available interval has unavailable support")
    availability: Dict[str, Any] = dict(prefix)
    availability.update({
        "callback_id": callback_id,
        "gyro_interval_id": interval.get("gyro_interval_id"),
        "interval_status": interval.get("status"),
        "interval_reason": interval.get("reason"),
        "source_sample_count_status": sample_count[0],
        "source_sample_count_reason": sample_count[1],
        "source_sample_count_value": sample_count[2],
        "first_support_status": first[0], "first_support_reason": first[1],
        "first_support_value_s": first[2], "last_support_status": last[0],
        "last_support_reason": last[1], "last_support_value_s": last[2],
        "start_endpoint_status": start_endpoint.get("status"),
        "start_endpoint_reason": start_endpoint.get("reason"),
        "start_lower_status": start_lower[0], "start_lower_reason": start_lower[1],
        "start_lower_value_s": start_lower[2], "start_upper_status": start_upper[0],
        "start_upper_reason": start_upper[1], "start_upper_value_s": start_upper[2],
        "end_endpoint_status": end_endpoint.get("status"),
        "end_endpoint_reason": end_endpoint.get("reason"),
        "end_lower_status": end_lower[0], "end_lower_reason": end_lower[1],
        "end_lower_value_s": end_lower[2], "end_upper_status": end_upper[0],
        "end_upper_reason": end_upper[1], "end_upper_value_s": end_upper[2],
        "maximum_gap_status": maximum_gap[0], "maximum_gap_reason": maximum_gap[1],
        "maximum_gap_value_s": maximum_gap[2], "coverage_status": coverage[0],
        "coverage_reason": coverage[1], "coverage_value": coverage[2],
        "bias_status": bias[0], "bias_reason": bias[1],
        "bias_xyz_rad_s": bias[2], "knot_status": knot_status,
        "knot_reason": knot_reason,
        "knot_count": len(knots.get("timestamp_value_s", []))
        if knots.get("status") == "AVAILABLE" else "",
        "integration_method": interval.get("integration_method"),
    })
    for output_prefix, summary in (("raw", raw), ("bias_corrected", corrected)):
        availability.update({
            output_prefix + "_status": summary["status"],
            output_prefix + "_reason": summary["reason"],
            output_prefix + "_max_norm_rad_s": summary["max_norm_rad_s"],
            output_prefix + "_rms_norm_rad_s": summary["rms_norm_rad_s"],
            output_prefix + "_mean_omega_xyz_rad_s": summary["mean_omega_xyz_rad_s"],
            output_prefix + "_integral_norm_rad": summary["integral_norm_rad"],
            output_prefix + "_integral_omega_xyz_rad": summary["integral_omega_xyz_rad"],
            output_prefix + "_delta_rotation_jpl_xyzw": summary["delta_rotation_jpl_xyzw"],
            output_prefix + "_delta_rotation_angle_rad": summary["delta_rotation_angle_rad"],
            output_prefix + "_axis_status": summary["axis_status"],
            output_prefix + "_axis_reason": summary["axis_reason"],
            output_prefix + "_axis_xyz": summary["axis_xyz"],
        })
    provenance = event.get("group_bearing_provenance")
    if not isinstance(provenance, dict):
        raise CorpusError("primary callback lacks group provenance")
    return interval_row, availability, provenance


def _kill_group(process: subprocess.Popen) -> None:
    for stop_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, stop_signal)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3.0)
            return
        except subprocess.TimeoutExpired:
            continue


def _stream_archive(
    archive: Path,
    algorithm: str,
    compressor_binding: Mapping[str, Any],
    expected_raw: Mapping[str, Any],
    timeout_seconds: float,
    callback: Any,
) -> Dict[str, Any]:
    if algorithm not in ("zstd", "gzip"):
        raise CorpusError("archive compression algorithm is invalid")
    executable = Path(str(compressor_binding.get("path", "")))
    executable = executable.resolve(strict=True)
    if (not executable.is_file() or
            _sha256(executable) != compressor_binding.get("sha256") or
            executable.stat().st_size != compressor_binding.get("size_bytes")):
        raise CorpusError("manifest-bound decompressor identity differs")
    expected_name = "zstd" if algorithm == "zstd" else "gzip"
    if not executable.name.startswith(expected_name):
        raise CorpusError("manifest-bound decompressor name differs")
    argv = ([executable, "-q", "-d", "-c", str(archive)]
            if algorithm == "zstd" else
            [executable, "-d", "-c", str(archive)])
    process = subprocess.Popen(
        argv, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdout is None:
        raise CorpusError("archive decompressor pipes are unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    started = time.monotonic()
    digest = hashlib.sha256()
    byte_count = 0
    buffer = b""
    line_number = 0
    try:
        eof = False
        while not eof:
            remaining = timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                _kill_group(process)
                raise CorpusError("archive streaming timed out")
            events = selector.select(min(1.0, remaining))
            if not events:
                if process.poll() is not None:
                    chunk = os.read(process.stdout.fileno(), 4 * 1024 * 1024)
                    if not chunk:
                        eof = True
                    else:
                        digest.update(chunk)
                        byte_count += len(chunk)
                        buffer += chunk
                continue
            chunk = os.read(process.stdout.fileno(), 4 * 1024 * 1024)
            if not chunk:
                eof = True
            else:
                digest.update(chunk)
                byte_count += len(chunk)
                buffer += chunk
                if len(buffer) > 256 * 1024 * 1024:
                    _kill_group(process)
                    raise CorpusError("telemetry JSONL record exceeds 256 MiB")
            while b"\n" in buffer:
                raw, buffer = buffer.split(b"\n", 1)
                line_number += 1
                try:
                    text = raw.decode("utf-8")
                    record = json.loads(
                        text,
                        parse_constant=lambda value: (_ for _ in ()).throw(
                            ValueError("nonfinite JSON token " + value)),
                        object_pairs_hook=_unique_pairs,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError,
                        ValueError) as exc:
                    _kill_group(process)
                    raise CorpusError(
                        "archive JSONL line {} is invalid".format(line_number)
                    ) from exc
                if not isinstance(record, dict):
                    raise CorpusError("archive JSONL record is not an object")
                _require_recursive_finite(record, "archive JSONL record")
                callback(line_number, record, len(raw) + 1)
        if buffer:
            raise CorpusError("archive JSONL is not newline terminated")
        exit_code = process.wait(timeout=5.0)
        if exit_code != 0:
            raise CorpusError("archive decompression failed")
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            _kill_group(process)
    if digest.hexdigest() != expected_raw.get("sha256"):
        raise CorpusError("streamed telemetry SHA-256 differs from manifest")
    if byte_count != expected_raw.get("size_bytes"):
        raise CorpusError("streamed telemetry byte count differs from manifest")
    return {
        "decoded_sha256": digest.hexdigest(),
        "decoded_size_bytes": byte_count,
        "record_count": line_number,
        "bounded_record_streaming": True,
    }


def _unique_pairs(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: " + key)
        value[key] = item
    return value


def _csv_atomic(path: Path, fieldnames: Sequence[str]):
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames,
                            lineterminator="\n", extrasaction="raise")
    writer.writeheader()
    return stream, writer, temporary


def _finish_csv(stream: Any, temporary: str, path: Path) -> None:
    stream.flush()
    os.fsync(stream.fileno())
    stream.close()
    os.replace(temporary, path)


def _nearest_rank(values: Sequence[float], probability: float) -> float:
    if not values:
        raise CorpusError("nearest rank requires a nonempty population")
    rank = max(1, math.ceil(probability * len(values)))
    return sorted(values)[rank - 1]


def _distribution(values: Sequence[float], unit: str) -> Mapping[str, Any]:
    if not values:
        return {"status": "NOT_AVAILABLE", "reason": "NO_AVAILABLE_VALUES",
                "unit": unit}
    ordered = sorted(values)
    return {
        "status": "AVAILABLE", "reason": "NONE", "unit": unit,
        "count": len(ordered), "min": ordered[0], "max": ordered[-1],
        "p50": _nearest_rank(ordered, 0.50),
        "p90": _nearest_rank(ordered, 0.90),
        "p99": _nearest_rank(ordered, 0.99),
    }


_ACTIVE_STAGING: Optional[Path] = None


def _cleanup_staging_on_failure(function):
    def wrapped(*args, **kwargs):
        global _ACTIVE_STAGING
        try:
            return function(*args, **kwargs)
        except BaseException:
            staging = _ACTIVE_STAGING
            if (staging is not None and staging.name.startswith(
                    ".event-ready-corpus.") and staging.exists()):
                shutil.rmtree(str(staging))
            raise
        finally:
            _ACTIVE_STAGING = None
    return wrapped


@_cleanup_staging_on_failure
def aggregate(input_path: Path, output_dir: Path,
              timeout_seconds: float) -> Mapping[str, Any]:
    global _ACTIVE_STAGING
    role_input = _strict_json(input_path, "aggregate input")
    if role_input.get("schema_version") != INPUT_SCHEMA:
        raise CorpusError("aggregate input schema mismatch")
    run_specs = role_input.get("runs")
    if not isinstance(run_specs, list):
        raise CorpusError("aggregate run list is invalid")
    output_dir = output_dir.resolve(strict=True)
    if output_dir != input_path.resolve(strict=True).parent:
        raise CorpusError("aggregate input and outputs must share one evidence root")

    run_ids: set = set()
    specs_by_id: Dict[str, Mapping[str, Any]] = {}
    manifests: Dict[str, Mapping[str, Any]] = {}
    for spec in run_specs:
        if not isinstance(spec, dict):
            raise CorpusError("aggregate run specification is invalid")
        run_id = spec.get("run_id")
        if not isinstance(run_id, str) or not run_id or run_id in run_ids:
            raise CorpusError("aggregate run ID is missing or duplicated")
        run_ids.add(run_id)
        if spec.get("dataset_scope") not in DATASET_SCOPES:
            raise CorpusError("aggregate dataset scope is invalid")
        if spec.get("run_role") not in RUN_ROLES:
            raise CorpusError("aggregate run role is invalid")
        result_path = Path(str(spec.get("result_manifest_path", "")))
        if not result_path.is_absolute():
            result_path = REPO_ROOT / result_path
        result_path = _reject_path(result_path, "run result manifest")
        manifest = _strict_json(result_path, "run result manifest")
        if manifest.get("status") != "COMPLETED":
            raise CorpusError("run {} is not completed".format(run_id))
        expected_manifest_schema = spec.get("result_manifest_schema")
        if (not isinstance(expected_manifest_schema, str) or
                manifest.get("schema") != expected_manifest_schema):
            raise CorpusError("run {} manifest schema differs".format(run_id))
        required_schema = (KAIST_RESULT_SCHEMA
                           if spec.get("dataset_scope") == "KAIST_DEVELOPMENT"
                           else MH01_RESULT_SCHEMA)
        if expected_manifest_schema != required_schema:
            raise CorpusError("run {} manifest schema is invalid".format(run_id))
        if (manifest.get("sequence") != spec.get("sequence") or
                (spec.get("dataset_scope") == "KAIST_DEVELOPMENT" and
                 manifest.get("campaign_index") !=
                 int(spec.get("sequence_order")) - 1)):
            raise CorpusError("run {} sequence binding differs".format(run_id))
        completion = manifest.get("completion")
        checks = manifest.get("checks")
        if (not isinstance(completion, dict) or
                completion.get("estimator_completed") is not True or
                completion.get("output_validation_passed") is not True or
                completion.get("evaluation_completed") is not True or
                completion.get("process_group_survived_cleanup") is not False or
                completion.get("roslaunch_exit_code") != 0 or
                not isinstance(checks, dict) or not checks or
                any(value is not True for value in checks.values())):
            raise CorpusError("run {} completion/check proof failed".format(run_id))
        specs_by_id[run_id] = spec
        manifests[run_id] = manifest

    primaries = sorted(
        (spec for spec in run_specs
         if spec.get("dataset_scope") == "KAIST_DEVELOPMENT" and
         spec.get("run_role") == "CAPTURE_ON_PRIMARY"),
        key=lambda value: value.get("sequence_order", -1),
    )
    if [spec.get("sequence") for spec in primaries] != list(SEQUENCE_ORDER):
        raise CorpusError("eleven KAIST primaries are not in frozen order")
    if [spec.get("sequence_order") for spec in primaries] != list(range(1, 12)):
        raise CorpusError("KAIST primary order indices are invalid")
    repeats = [spec for spec in run_specs
               if spec.get("dataset_scope") == "KAIST_DEVELOPMENT" and
               spec.get("run_role") == "CAPTURE_ON_REPEAT"]
    if (len(repeats) != 3 or
            {spec.get("sequence") for spec in repeats} != REPEAT_SEQUENCES):
        raise CorpusError("required KAIST repeat population is incomplete")
    kaist_off = [spec for spec in run_specs
                 if spec.get("dataset_scope") == "KAIST_DEVELOPMENT" and
                 spec.get("run_role") == "EXTENSION_OFF"]
    if (len(kaist_off) != 2 or
            {spec.get("sequence") for spec in kaist_off} !=
            {SEQUENCE_ORDER[0], SEQUENCE_ORDER[2]}):
        raise CorpusError("minimum KAIST capture-off population is incomplete")
    mh_specs = [spec for spec in run_specs
                if spec.get("dataset_scope") == "MH01_PARITY"]
    if (len(mh_specs) != 3 or
            [spec.get("run_role") for spec in mh_specs].count("EXTENSION_OFF") != 1 or
            [spec.get("run_role") for spec in mh_specs].count(
                "CAPTURE_ON_PRIMARY") != 1 or
            [spec.get("run_role") for spec in mh_specs].count(
                "CAPTURE_ON_REPEAT") != 1 or
            any(spec.get("sequence") != "MH_01" for spec in mh_specs)):
        raise CorpusError("MH_01 off/on1/on2 population is incomplete")
    if any(spec.get("include_in_primary_totals") is not True for spec in primaries):
        raise CorpusError("KAIST primary inclusion flags are invalid")
    if any(spec.get("include_in_primary_totals") is not False
           for spec in run_specs if spec not in primaries):
        raise CorpusError("non-primary run entered primary totals")
    for spec in run_specs:
        expected_digest = spec.get("expected_stable_digest_sha256")
        if (not isinstance(expected_digest, str) or
                re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None):
            raise CorpusError("run frozen stable digest is missing")
        for compare_field in ("estimator_compare_run_id", "telemetry_compare_run_id"):
            compare = spec.get(compare_field)
            if compare is not None and compare not in specs_by_id:
                raise CorpusError("run comparison target is missing")
            if compare == spec.get("run_id"):
                raise CorpusError("run comparison cannot target itself")
        if spec.get("run_role") == "CAPTURE_ON_REPEAT":
            if (spec.get("estimator_compare_run_id") is None or
                    spec.get("telemetry_compare_run_id") is None):
                raise CorpusError("repeat comparison edges are missing")
        elif spec.get("telemetry_compare_run_id") is not None:
            raise CorpusError("non-repeat has a telemetry comparison edge")

    def unique_spec(scope: str, sequence: str, role: str) -> Mapping[str, Any]:
        matches = [spec for spec in run_specs
                   if spec.get("dataset_scope") == scope and
                   spec.get("sequence") == sequence and
                   spec.get("run_role") == role]
        if len(matches) != 1:
            raise CorpusError("required run role is missing or duplicated")
        return matches[0]

    mh_off_spec = unique_spec("MH01_PARITY", "MH_01", "EXTENSION_OFF")
    mh_on1_spec = unique_spec(
        "MH01_PARITY", "MH_01", "CAPTURE_ON_PRIMARY")
    mh_on2_spec = unique_spec(
        "MH01_PARITY", "MH_01", "CAPTURE_ON_REPEAT")
    if (mh_off_spec.get("estimator_compare_run_id") is not None or
            mh_off_spec.get("telemetry_compare_run_id") is not None or
            mh_on1_spec.get("estimator_compare_run_id") !=
            mh_off_spec.get("run_id") or
            mh_on1_spec.get("telemetry_compare_run_id") is not None or
            mh_on2_spec.get("estimator_compare_run_id") !=
            mh_on1_spec.get("run_id") or
            mh_on2_spec.get("telemetry_compare_run_id") !=
            mh_on1_spec.get("run_id")):
        raise CorpusError("MH_01 comparison graph is invalid")

    kaist_off_by_sequence = {
        sequence: unique_spec("KAIST_DEVELOPMENT", sequence, "EXTENSION_OFF")
        for sequence in (SEQUENCE_ORDER[0], SEQUENCE_ORDER[2])
    }
    primary_by_sequence = {str(spec["sequence"]): spec for spec in primaries}
    for sequence, primary in primary_by_sequence.items():
        required_off = kaist_off_by_sequence.get(sequence)
        required_compare = (None if required_off is None
                            else required_off.get("run_id"))
        if (primary.get("estimator_compare_run_id") != required_compare or
                primary.get("telemetry_compare_run_id") is not None):
            raise CorpusError("KAIST primary comparison graph is invalid")
    for off_spec in kaist_off_by_sequence.values():
        if (off_spec.get("estimator_compare_run_id") is not None or
                off_spec.get("telemetry_compare_run_id") is not None):
            raise CorpusError("KAIST off run has a comparison edge")
    for repeat in repeats:
        primary = primary_by_sequence[str(repeat["sequence"])]
        if (repeat.get("estimator_compare_run_id") != primary.get("run_id") or
                repeat.get("telemetry_compare_run_id") != primary.get("run_id")):
            raise CorpusError("KAIST repeat comparison graph is invalid")

    for sequence in SEQUENCE_ORDER:
        sequence_digests = {
            spec.get("expected_stable_digest_sha256") for spec in run_specs
            if spec.get("dataset_scope") == "KAIST_DEVELOPMENT" and
            spec.get("sequence") == sequence
        }
        if len(sequence_digests) != 1:
            raise CorpusError("KAIST frozen digest binding differs by role")
    if len({spec.get("expected_stable_digest_sha256") for spec in mh_specs}) != 1:
        raise CorpusError("MH_01 frozen digest binding differs by role")

    ledger_before = role_input.get("kaist_dataset_ledger_before")
    ledger_after = role_input.get("kaist_dataset_ledger_after")
    if (not isinstance(ledger_before, dict) or ledger_before != ledger_after or
            re.fullmatch(r"[0-9a-f]{64}",
                         str(ledger_before.get("sha256", ""))) is None):
        raise CorpusError("KAIST before/after ledger identity differs")

    output_names = (
        "CALLBACK_INTERVALS.csv", "IMU_INTERVAL_AVAILABILITY.csv",
        "GROUP_PROVENANCE_COUNTS.csv", "SEQUENCE_RESULTS.csv",
        "CALLBACK_STATE_ASSOCIATION.csv",
        "CALLBACK_REFERENCE_ASSOCIATION.csv", "ASSOCIATION_COVERAGE.json",
        "GROUP_BEARING_PROVENANCE_MANIFEST.json",
        "EVENT_READY_CORPUS_MANIFEST.json", "EVENT_READY_CORPUS_REPORT.md",
    )
    final_output_dir = output_dir
    if any((final_output_dir / name).exists() for name in output_names):
        raise CorpusError("refusing to overwrite an aggregate output")
    output_dir = Path(tempfile.mkdtemp(
        prefix=".event-ready-corpus.", dir=str(final_output_dir)))
    _ACTIVE_STAGING = output_dir

    def output_identity(path: Path) -> Dict[str, Any]:
        identity = _identity(path)
        identity["path"] = str(
            (final_output_dir / path.name).relative_to(REPO_ROOT))
        return identity
    interval_path = output_dir / "CALLBACK_INTERVALS.csv"
    availability_path = output_dir / "IMU_INTERVAL_AVAILABILITY.csv"
    group_counts_path = output_dir / "GROUP_PROVENANCE_COUNTS.csv"
    sequence_path = output_dir / "SEQUENCE_RESULTS.csv"
    state_assoc_path = output_dir / "CALLBACK_STATE_ASSOCIATION.csv"
    reference_assoc_path = output_dir / "CALLBACK_REFERENCE_ASSOCIATION.csv"

    interval_stream, interval_writer, interval_tmp = _csv_atomic(
        interval_path, INTERVAL_FIELDS
    )
    availability_stream, availability_writer, availability_tmp = _csv_atomic(
        availability_path, AVAILABILITY_FIELDS
    )

    combined_state_fields: Optional[List[str]] = None
    combined_reference_fields: Optional[List[str]] = None
    state_stream = state_writer = state_tmp = None
    reference_stream = reference_writer = reference_tmp = None
    pooled_residuals: Dict[str, List[float]] = {
        name: [] for name in ("state", "deviation", "trajectory", "reference")
    }
    pooled_counts: Dict[str, Dict[str, int]] = {
        name: {status: 0 for status in ("MATCHED", "MISSING", "AMBIGUOUS")}
        for name in pooled_residuals
    }
    interval_reason_counts: Dict[str, int] = {}
    group_status_counts: Dict[str, int] = {}
    group_reason_counts: Dict[str, int] = {}
    member_status_counts: Dict[str, int] = {}
    member_reason_counts: Dict[str, int] = {}
    member_buckets = {"N2": 0, "N3": 0, "N_GE4": 0}
    distributions: Dict[str, List[float]] = {
        "source_sample_count": [], "maximum_gap_s": [], "coverage_fraction": [],
        "raw_max_norm_rad_s": [], "raw_integral_norm_rad": [],
        "bias_corrected_max_norm_rad_s": [],
        "bias_corrected_integral_norm_rad": [],
    }
    normalized_runs: Dict[str, Dict[str, Any]] = {}
    primary_totals = {
        "records": 0, "callbacks": 0, "camera_frames": 0,
        "intervals": 0, "intervals_available": 0,
        "intervals_unavailable": 0, "groups": 0, "members": 0,
        "raw_telemetry_bytes": 0, "archive_bytes": 0,
    }

    capture_specs = [spec for spec in run_specs
                     if spec.get("run_role") != "EXTENSION_OFF"]
    per_run_group_counts: Dict[str, Dict[str, Dict[str, int]]] = {}
    try:
        for spec in capture_specs:
            run_id = str(spec["run_id"])
            sequence = str(spec["sequence"])
            emit_primary = spec in primaries
            manifest = manifests[run_id]
            event_manifest = manifest.get("event_extension")
            telemetry = manifest.get("turnsafe_t0")
            if not isinstance(event_manifest, dict) or not isinstance(telemetry, dict):
                raise CorpusError("capture run lacks event telemetry binding")
            if event_manifest.get("schema") != EXTENSION_SCHEMA:
                raise CorpusError("capture event-extension schema mismatch")
            if event_manifest.get("capture_flags") != {
                "causal_imu_intervals": True,
                "outcome_association_keys": True,
                "group_bearing_provenance": True,
            }:
                raise CorpusError("capture flags are incomplete")
            archive_binding = event_manifest.get("telemetry_archive")
            if not isinstance(archive_binding, dict):
                raise CorpusError("capture telemetry archive binding is missing")
            raw_identity = archive_binding.get("raw")
            archive_identity = archive_binding.get("archive")
            if not isinstance(raw_identity, dict) or not isinstance(archive_identity, dict):
                raise CorpusError("capture telemetry identities are invalid")
            archive_test = archive_binding.get("archive_test")
            manifest_compressor = manifest.get("inputs_before", {}).get(
                "event_telemetry_compressor")
            if (not isinstance(archive_test, dict) or
                    archive_binding.get("compressor") != manifest_compressor or
                    archive_binding.get("schema_version") !=
                    "turnsafe.lossless_telemetry_archive.v1" or
                    archive_binding.get("raw_removed_after_verified_roundtrip") is not True or
                    archive_test.get("exit_code") != 0 or
                    archive_test.get("timed_out") is not False or
                    archive_test.get("process_group_survived_cleanup") is not False or
                    archive_binding.get("roundtrip", {}).get("sha256") !=
                    raw_identity.get("sha256") or
                    archive_binding.get("roundtrip", {}).get("size_bytes") !=
                    raw_identity.get("size_bytes")):
                raise CorpusError("lossless telemetry archive proof is incomplete")
            archive = Path(str(archive_identity.get("path", "")))
            archive = _reject_path(archive, "telemetry archive")
            if _sha256(archive) != archive_identity.get("sha256") or \
                    archive.stat().st_size != archive_identity.get("size_bytes"):
                raise CorpusError("telemetry archive identity mismatch")
            prefix = {
                "sequence_order": spec["sequence_order"],
                "run_id": run_id,
                "sequence": sequence,
            }
            streamed_counts = {
                "callbacks": 0, "camera_frames": 0, "intervals": 0,
                "available": 0, "groups": 0, "members": 0,
                "group_serialized_bytes": 0, "record_bytes_total": 0,
                "maximum_record_bytes": 0,
            }
            run_group_status: Dict[str, int] = {}
            run_group_reason: Dict[str, int] = {}
            run_member_status: Dict[str, int] = {}
            run_member_reason: Dict[str, int] = {}
            run_member_buckets = {"N2": 0, "N3": 0, "N_GE4": 0}

            def consume(line_number: int, record: Mapping[str, Any],
                        record_bytes: int) -> None:
                if line_number == 1:
                    if (record.get("schema") != "turnsafe.t0.v1" or
                            record.get("record_type") != "run_header"):
                        raise CorpusError("telemetry header is invalid")
                    header_event = record.get("extensions", {}).get("event_extension")
                    expected_header = manifest.get(
                        "turnsafe_t0_provenance_binding")
                    if not isinstance(expected_header, dict):
                        raise CorpusError("telemetry header binding is missing")
                    for field in (
                            "frozen_base_sha", "source_sha", "tree_sha",
                            "source_snapshot_sha256", "source_dirty",
                            "build_provenance_id", "configure_manifest_sha256",
                            "build_manifest_sha256", "binary_sha256",
                            "config_sha256", "calibration_sha256",
                            "diagnostic_schema_sha256"):
                        if field not in expected_header or record.get(field) != \
                                expected_header.get(field):
                            raise CorpusError(
                                "telemetry header provenance differs")
                    expected_event_header = expected_header.get(
                        "extensions", {}).get("event_extension")
                    if (not isinstance(header_event, dict) or
                            header_event != expected_event_header or
                            header_event.get("schema") != EXTENSION_SCHEMA or
                            header_event.get("record_version") != 1 or
                            header_event.get("capture_flags") != {
                                "causal_imu_intervals": True,
                                "outcome_association_keys": True,
                                "group_bearing_provenance": True}):
                        raise CorpusError("telemetry extension header is invalid")
                    return
                streamed_counts["record_bytes_total"] += record_bytes
                streamed_counts["maximum_record_bytes"] = max(
                    streamed_counts["maximum_record_bytes"], record_bytes)
                callback_id = record.get("callback_index")
                if callback_id != streamed_counts["callbacks"]:
                    raise CorpusError("telemetry callback order is not contiguous")
                interval_row, availability_row, provenance = _interval_rows(
                    prefix, record
                )
                if interval_row["gyro_interval_id"] != callback_id:
                    raise CorpusError("callback/interval identity differs")
                if emit_primary:
                    interval_writer.writerow(interval_row)
                    availability_writer.writerow(availability_row)
                streamed_counts["callbacks"] += 1
                streamed_counts["camera_frames"] += int(
                    interval_row["camera_frame_count"]
                )
                streamed_counts["intervals"] += 1
                reason = str(interval_row["interval_reason"])
                if emit_primary:
                    interval_reason_counts[reason] = interval_reason_counts.get(reason, 0) + 1
                if interval_row["interval_status"] == "AVAILABLE":
                    streamed_counts["available"] += 1
                if (emit_primary and
                        availability_row["source_sample_count_value"] != ""):
                    distributions["source_sample_count"].append(float(
                        availability_row["source_sample_count_value"]))
                for source_name, column_name in (
                    ("maximum_gap_s", "maximum_gap_value_s"),
                    ("coverage_fraction", "coverage_value"),
                    ("raw_max_norm_rad_s", "raw_max_norm_rad_s"),
                    ("raw_integral_norm_rad", "raw_integral_norm_rad"),
                    ("bias_corrected_max_norm_rad_s",
                     "bias_corrected_max_norm_rad_s"),
                    ("bias_corrected_integral_norm_rad",
                     "bias_corrected_integral_norm_rad"),
                ):
                    value = availability_row[column_name]
                    if emit_primary and value != "":
                        distributions[source_name].append(float(value))
                status = str(provenance.get("status"))
                reason_value = str(provenance.get("reason"))
                if status not in ("AVAILABLE", "NOT_AVAILABLE") or \
                        ((status == "AVAILABLE") != (reason_value == "NONE")):
                    raise CorpusError("group provenance status/reason is invalid")
                run_group_status[status] = run_group_status.get(status, 0) + 1
                run_group_reason[reason_value] = run_group_reason.get(reason_value, 0) + 1
                if emit_primary:
                    group_status_counts[status] = group_status_counts.get(status, 0) + 1
                    group_reason_counts[reason_value] = group_reason_counts.get(reason_value, 0) + 1
                groups = provenance.get("groups")
                if not isinstance(groups, list) or provenance.get("group_count") != len(groups):
                    raise CorpusError("group provenance count is invalid")
                if status != "AVAILABLE" and groups:
                    raise CorpusError("unavailable group provenance exposes groups")
                streamed_counts["group_serialized_bytes"] += len(
                    json.dumps(provenance, sort_keys=True,
                               separators=(",", ":")).encode("utf-8")
                )
                for group in groups:
                    count = group.get("member_count")
                    members = group.get("members")
                    if (not isinstance(count, int) or count < 2 or
                            not isinstance(members, list) or len(members) != count):
                        raise CorpusError("group member count is invalid")
                    streamed_counts["groups"] += 1
                    streamed_counts["members"] += count
                    for member in members:
                        if not isinstance(member, dict):
                            raise CorpusError("group member is not an object")
                        incomplete_reasons: List[str] = []
                        for field in (
                                "source_raw_pixel", "target_raw_pixel",
                                "source_normalized_coordinates",
                                "target_normalized_coordinates",
                                "source_unit_bearing", "target_unit_bearing",
                                "target_stereo_raw_pixel", "source_image_cell",
                                "target_image_cell", "target_stereo_image_cell",
                                "finite_validation"):
                            typed = member.get(field)
                            if (not isinstance(typed, dict) or
                                    typed.get("status") != "AVAILABLE"):
                                incomplete_reasons.append(
                                    str(typed.get("reason"))
                                    if isinstance(typed, dict) else
                                    "FIELD_OBJECT_MISSING")
                        member_status = ("COMPLETE" if not incomplete_reasons
                                         else "INCOMPLETE")
                        run_member_status[member_status] = (
                            run_member_status.get(member_status, 0) + 1)
                        if incomplete_reasons:
                            for missing_reason in sorted(set(incomplete_reasons)):
                                run_member_reason[missing_reason] = (
                                    run_member_reason.get(missing_reason, 0) + 1)
                        else:
                            run_member_reason["NONE"] = (
                                run_member_reason.get("NONE", 0) + 1)
                        if emit_primary:
                            member_status_counts[member_status] = (
                                member_status_counts.get(member_status, 0) + 1)
                            for missing_reason in (
                                    sorted(set(incomplete_reasons))
                                    if incomplete_reasons else ["NONE"]):
                                member_reason_counts[missing_reason] = (
                                    member_reason_counts.get(missing_reason, 0) + 1)
                    bucket = "N2" if count == 2 else \
                        "N3" if count == 3 else "N_GE4"
                    run_member_buckets[bucket] += 1
                    if emit_primary:
                        member_buckets[bucket] += 1

            stream_result = _stream_archive(
                archive, str(archive_binding.get("algorithm")),
                archive_binding.get("compressor", {}), raw_identity,
                timeout_seconds, consume,
            )
            if stream_result["record_count"] != streamed_counts["callbacks"] + 1:
                raise CorpusError("telemetry record/callback counts do not reconcile")
            extension_counts = telemetry.get("event_extension")
            if not isinstance(extension_counts, dict):
                raise CorpusError("validated extension counts are missing")
            expected_counts = {
                "callback_count": streamed_counts["callbacks"],
                "interval_count": streamed_counts["intervals"],
                "interval_available_count": streamed_counts["available"],
                "group_provenance_count": streamed_counts["groups"],
                "group_member_count": streamed_counts["members"],
            }
            if telemetry.get("callback_count") != expected_counts["callback_count"]:
                raise CorpusError("telemetry callback count differs from stream")
            if (telemetry.get("identity") != raw_identity or
                    telemetry.get("record_count") != stream_result["record_count"]):
                raise CorpusError("telemetry raw identity/count differs")
            for field in (
                "interval_count", "interval_available_count",
                "group_provenance_count", "group_member_count",
            ):
                if extension_counts.get(field) != expected_counts[field]:
                    raise CorpusError("telemetry {} differs from stream".format(field))

            associations = event_manifest.get("associations")
            if not isinstance(associations, dict):
                raise CorpusError("capture association binding is missing")
            association_outputs = associations.get("outputs")
            if not isinstance(association_outputs, dict):
                raise CorpusError("capture association outputs are missing")
            coverage_identity = association_outputs.get("association_coverage")
            if not isinstance(coverage_identity, dict):
                raise CorpusError("association coverage identity is missing")
            coverage_path = _reject_path(
                Path(str(coverage_identity.get("path", ""))),
                "association coverage")
            if (_sha256(coverage_path) != coverage_identity.get("sha256") or
                    coverage_path.stat().st_size !=
                    coverage_identity.get("size_bytes")):
                raise CorpusError("association coverage identity differs")
            coverage_value = _strict_json(
                coverage_path, "per-run association coverage")
            if (coverage_value != associations.get("coverage") or
                    coverage_value.get("callback_count") !=
                    streamed_counts["callbacks"] or
                    coverage_value.get("association_version") !=
                    "turnsafe.offline_association.v1" or
                    coverage_value.get("inputs", {}).get("telemetry_sha256") !=
                    raw_identity.get("sha256")):
                raise CorpusError("per-run association coverage differs")
            coverage_inputs = coverage_value.get("inputs")
            estimator_outputs = manifest.get("outputs")
            if not isinstance(coverage_inputs, dict) or not isinstance(
                    estimator_outputs, dict):
                raise CorpusError("association input binding is missing")
            required_association_inputs = {
                "state_sha256": estimator_outputs.get("state", {}).get("sha256"),
                "deviation_sha256": estimator_outputs.get(
                    "deviation", {}).get("sha256"),
                "trajectory_sha256": estimator_outputs.get(
                    "trajectory_tum", {}).get("sha256"),
                "reference_sha256": manifest.get("inputs_before", {}).get(
                    "reference_tum", {}).get("sha256"),
            }
            if any(coverage_inputs.get(name) != value
                   for name, value in required_association_inputs.items()):
                raise CorpusError("association source hash binding differs")
            for output_name, combined_kind in (
                ("callback_state_association", "state"),
                ("callback_reference_association", "reference"),
            ):
                identity = association_outputs.get(output_name)
                if not isinstance(identity, dict):
                    raise CorpusError("association output identity is invalid")
                path = _reject_path(Path(str(identity.get("path", ""))),
                                    "association CSV")
                if (_sha256(path) != identity.get("sha256") or
                        path.stat().st_size != identity.get("size_bytes")):
                    raise CorpusError("association CSV identity mismatch")
                with path.open("r", encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    if reader.fieldnames is None:
                        raise CorpusError("association CSV has no header")
                    if any(field in FORBIDDEN_OUTPUT_KEYS
                           for field in reader.fieldnames):
                        raise CorpusError("association CSV contains scientific fields")
                    if any(field in ("sequence_order", "run_id", "sequence")
                           for field in reader.fieldnames):
                        raise CorpusError("association CSV collides with run prefix")
                    prefixed = ["sequence_order", "run_id", "sequence"] + reader.fieldnames
                    if combined_kind == "state":
                        if emit_primary and combined_state_fields is None:
                            combined_state_fields = prefixed
                            state_stream, state_writer, state_tmp = _csv_atomic(
                                state_assoc_path, combined_state_fields
                            )
                        elif emit_primary and prefixed != combined_state_fields:
                            raise CorpusError("state association headers differ")
                        writer = state_writer if emit_primary else None
                    else:
                        if emit_primary and combined_reference_fields is None:
                            combined_reference_fields = prefixed
                            reference_stream, reference_writer, reference_tmp = _csv_atomic(
                                reference_assoc_path, combined_reference_fields
                            )
                        elif emit_primary and prefixed != combined_reference_fields:
                            raise CorpusError("reference association headers differ")
                        writer = reference_writer if emit_primary else None
                    row_count = 0
                    for row in reader:
                        if int(row["callback_id"]) != row_count:
                            raise CorpusError("association callback order is invalid")
                        expected_policy = (
                            "unique_nearest_expected_output_timestamp_max_5p1e-6s.v1"
                            if combined_kind == "state" else
                            "unique_nearest_expected_output_timestamp_max_0p01s.v1")
                        if (row.get("association_version") !=
                                "turnsafe.offline_association.v1" or
                                row.get("policy_id") != expected_policy):
                            raise CorpusError("association row policy differs")
                        if emit_primary:
                            combined_row = dict(prefix)
                            combined_row.update(row)
                            writer.writerow(combined_row)
                        row_count += 1
                        stream_names = (("state", "deviation", "trajectory")
                                        if combined_kind == "state" else
                                        ("reference",))
                        for stream_name in stream_names:
                            status = row[stream_name + "_status"]
                            reason = row[stream_name + "_reason"]
                            if status not in ("MATCHED", "MISSING", "AMBIGUOUS"):
                                raise CorpusError("association status is invalid")
                            if ((status == "MATCHED") != (reason == "NONE")):
                                raise CorpusError("association status/reason differs")
                            if emit_primary:
                                pooled_counts[stream_name][status] += 1
                            residual = row[stream_name + "_time_residual_s"]
                            if status == "MATCHED":
                                if residual == "":
                                    raise CorpusError("matched association lacks residual")
                                try:
                                    residual_value = float(residual)
                                except ValueError as exc:
                                    raise CorpusError(
                                        "association residual is invalid") from exc
                                if not math.isfinite(residual_value):
                                    raise CorpusError(
                                        "association residual is nonfinite")
                                if emit_primary:
                                    pooled_residuals[stream_name].append(
                                        abs(residual_value))
                            elif residual != "":
                                raise CorpusError("unmatched association exposes residual")
                    if row_count != streamed_counts["callbacks"]:
                        raise CorpusError("association callback count differs from telemetry")

            if emit_primary:
                primary_totals["records"] += stream_result["record_count"]
                primary_totals["callbacks"] += streamed_counts["callbacks"]
                primary_totals["camera_frames"] += streamed_counts["camera_frames"]
                primary_totals["intervals"] += streamed_counts["intervals"]
                primary_totals["intervals_available"] += streamed_counts["available"]
                primary_totals["intervals_unavailable"] += (
                    streamed_counts["intervals"] - streamed_counts["available"]
                )
                primary_totals["groups"] += streamed_counts["groups"]
                primary_totals["members"] += streamed_counts["members"]
                primary_totals["raw_telemetry_bytes"] += raw_identity["size_bytes"]
                primary_totals["archive_bytes"] += archive_identity["size_bytes"]
            per_run_group_counts[run_id] = {
                "status": run_group_status, "reason": run_group_reason,
                "member_count_bucket": run_member_buckets,
                "member_status": run_member_status,
                "member_reason": run_member_reason,
            }
            normalized_runs[run_id] = {
                "stream": stream_result,
                "counts": streamed_counts,
                "archive": {
                    "raw": raw_identity, "archive": archive_identity,
                    "algorithm": archive_binding.get("algorithm"),
                },
                "association_coverage": coverage_identity,
            }
    except Exception:
        for stream in (interval_stream, availability_stream,
                       state_stream, reference_stream):
            if stream is not None and not stream.closed:
                stream.close()
        for temporary in (interval_tmp, availability_tmp, state_tmp, reference_tmp):
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        raise

    _finish_csv(interval_stream, interval_tmp, interval_path)
    _finish_csv(availability_stream, availability_tmp, availability_path)
    if state_stream is None or reference_stream is None:
        raise CorpusError("primary association outputs were not produced")
    _finish_csv(state_stream, state_tmp, state_assoc_path)
    _finish_csv(reference_stream, reference_tmp, reference_assoc_path)

    comparisons: List[Dict[str, Any]] = []
    for spec in run_specs:
        run_id = str(spec["run_id"])
        manifest = manifests[run_id]
        stable = manifest.get("stable_estimator_digest")
        if not isinstance(stable, dict) or not isinstance(stable.get("value"), dict):
            raise CorpusError("run {} lacks stable estimator digest".format(run_id))
        outputs = manifest.get("outputs")
        if not isinstance(outputs, dict):
            raise CorpusError("run {} lacks estimator outputs".format(run_id))
        if (stable["value"].get("combined_stable_sha256") !=
                spec["expected_stable_digest_sha256"]):
            raise CorpusError("run {} differs from frozen stable digest".format(
                run_id))
        output_hashes: Dict[str, Any] = {}
        for name in ("state", "deviation", "trajectory_tum"):
            identity = outputs.get(name)
            if not isinstance(identity, dict):
                raise CorpusError("run {} output identity is missing".format(run_id))
            output_path = _reject_path(
                Path(str(identity.get("path", ""))), "estimator output")
            if (_sha256(output_path) != identity.get("sha256") or
                    output_path.stat().st_size != identity.get("size_bytes")):
                raise CorpusError("run {} output identity differs".format(run_id))
            output_hashes[name] = identity["sha256"]
        base_t0 = manifest.get("turnsafe_t0")
        base_extension_summary = (base_t0.get("event_extension")
                                  if isinstance(base_t0, dict) else None)
        base_extension_active = (
            isinstance(base_extension_summary, dict) and
            base_extension_summary.get("schema") == EXTENSION_SCHEMA)
        if (spec["run_role"] == "EXTENSION_OFF" and
                ("event_extension" in manifest or
                 base_extension_active)):
            raise CorpusError("capture-off run contains extension telemetry")
        normalized_runs.setdefault(run_id, {})
        normalized_runs[run_id].update({
            "run_role": spec["run_role"],
            "dataset_scope": spec["dataset_scope"],
            "sequence": spec.get("sequence"),
            "sequence_order": spec.get("sequence_order"),
            "result_manifest": _identity(_reject_path(
                Path(str(spec["result_manifest_path"]))
                if Path(str(spec["result_manifest_path"])).is_absolute()
                else REPO_ROOT / str(spec["result_manifest_path"]),
                "run result manifest",
            )),
            "stable_estimator_digest": stable,
            "estimator_output_sha256": output_hashes,
            "source_snapshot_sha256": manifest.get(
                "source_provenance_before", {}
            ).get("source_snapshot_sha256"),
            "build_provenance_id": manifest.get(
                "source_provenance_before", {}
            ).get("build_provenance_id"),
            "binary_sha256": manifest.get("inputs_before", {}).get(
                "estimator_binary", {}
            ).get("sha256"),
            "config_sha256": manifest.get("inputs_before", {}).get(
                "config", {}
            ).get("sha256"),
            "calibration_sha256": manifest.get(
                "turnsafe_t0_provenance_binding", {}
            ).get("calibration_sha256"),
            "adapted_input_sha256": manifest.get("adapted_bag", {}).get("sha256"),
            "reference_sha256": manifest.get("inputs_before", {}).get(
                "reference_tum", {}
            ).get("sha256"),
        })
        compare_id = spec.get("estimator_compare_run_id")
        if compare_id is None:
            comparisons.append({
                "run_id": run_id, "compare_run_id": None,
                "status": "PASS", "reason": "FROZEN_STABLE_DIGEST_MATCH",
                "state_deviation_trajectory_bytes_equal": None,
                "stable_digest_equal": True,
                "telemetry_compare_run_id": None,
                "telemetry_raw_bytes_equal": None,
            })
            continue
        compare = manifests[str(compare_id)]
        compare_stable = compare.get("stable_estimator_digest", {}).get("value", {})
        compare_outputs = compare.get("outputs", {})
        fields_equal = all(
            outputs[name]["sha256"] == compare_outputs[name]["sha256"]
            for name in ("state", "deviation", "trajectory_tum")
        )
        digest_equal = (
            stable["value"]["combined_stable_sha256"] ==
            compare_stable.get("combined_stable_sha256")
        )
        if not fields_equal or not digest_equal:
            raise CorpusError("estimator parity failed for {}".format(run_id))
        telemetry_compare = spec.get("telemetry_compare_run_id")
        telemetry_equal: Optional[bool] = None
        if telemetry_compare is not None:
            left_raw = normalized_runs[run_id].get("archive", {}).get("raw")
            right_raw = normalized_runs[str(telemetry_compare)].get(
                "archive", {}
            ).get("raw")
            telemetry_equal = (
                isinstance(left_raw, dict) and isinstance(right_raw, dict) and
                left_raw.get("sha256") == right_raw.get("sha256") and
                left_raw.get("size_bytes") == right_raw.get("size_bytes") and
                normalized_runs[run_id].get("stream", {}).get("record_count") ==
                normalized_runs[str(telemetry_compare)].get("stream", {}).get(
                    "record_count"))
            if not telemetry_equal:
                raise CorpusError("telemetry determinism failed for {}".format(run_id))
        comparisons.append({
            "run_id": run_id, "compare_run_id": compare_id,
            "status": "PASS", "reason": "NONE",
            "state_deviation_trajectory_bytes_equal": fields_equal,
            "stable_digest_equal": digest_equal,
            "telemetry_compare_run_id": telemetry_compare,
            "telemetry_raw_bytes_equal": telemetry_equal,
        })

    for field, scope in (
            ("source_snapshot_sha256", run_specs),
            ("build_provenance_id", run_specs),
            ("binary_sha256", run_specs),
            ("config_sha256", [spec for spec in run_specs
                               if spec["dataset_scope"] == "KAIST_DEVELOPMENT"]),
            ("calibration_sha256", [spec for spec in run_specs
                                    if spec["dataset_scope"] ==
                                    "KAIST_DEVELOPMENT"])):
        values = {normalized_runs[spec["run_id"]].get(field)
                  for spec in scope}
        if None in values or "" in values or len(values) != 1:
            raise CorpusError("campaign {} identity is not frozen".format(field))

    group_rows: List[Dict[str, Any]] = []
    for spec in primaries:
        run_id = str(spec["run_id"])
        partitions = per_run_group_counts[run_id]
        for entity, partition, values in (
                ("callback", "status", partitions["status"]),
                ("callback", "reason", partitions["reason"]),
                ("group", "member_count_bucket",
                 partitions["member_count_bucket"]),
                ("member", "status", partitions["member_status"]),
                ("member", "reason", partitions["member_reason"])):
            for value, count in sorted(values.items()):
                group_rows.append({
                    "sequence_order": spec["sequence_order"],
                    "run_id": run_id, "sequence": spec["sequence"],
                    "entity": entity, "partition": partition,
                    "value": value, "count": count,
                })
    for entity, partition, values in (
        ("callback", "status", group_status_counts),
        ("callback", "reason", group_reason_counts),
        ("group", "member_count_bucket", member_buckets),
        ("member", "status", member_status_counts),
        ("member", "reason", member_reason_counts),
    ):
        for value, count in sorted(values.items()):
            group_rows.append({
                "sequence_order": "ALL_PRIMARY", "run_id": "ALL_PRIMARY",
                "sequence": "ALL_PRIMARY", "entity": entity,
                "partition": partition, "value": value, "count": count,
            })
    group_fields = (
        "sequence_order", "run_id", "sequence", "entity", "partition",
        "value", "count",
    )
    group_stream, group_writer, group_tmp = _csv_atomic(
        group_counts_path, group_fields
    )
    for row in group_rows:
        group_writer.writerow(row)
    _finish_csv(group_stream, group_tmp, group_counts_path)

    sequence_fields = (
        "campaign_order", "run_id", "dataset_scope", "sequence_order",
        "sequence", "run_role", "include_in_primary_totals",
        "estimator_compare_run_id", "telemetry_compare_run_id", "status",
        "completed", "terminal_queue_proof", "process_cleanup_passed",
        "telemetry_status", "telemetry_reason", "comparison_status",
        "comparison_reason", "source_snapshot_sha256", "build_provenance_id",
        "binary_sha256",
        "config_sha256", "calibration_sha256", "adapted_input_sha256",
        "reference_sha256", "event_extension_schema",
        "raw_telemetry_sha256", "archive_sha256", "raw_bytes",
        "archive_bytes", "record_count", "callback_count",
        "camera_frame_count", "interval_count", "interval_available_count",
        "interval_unavailable_count", "group_count", "member_count",
        "state_sha256", "deviation_sha256", "trajectory_tum_sha256",
        "estimator_parity_passed", "telemetry_determinism_passed",
        "association_status", "association_reason", "association_state_rows",
        "association_reference_rows", "dataset_unchanged",
    )
    sequence_stream, sequence_writer, sequence_tmp = _csv_atomic(
        sequence_path, sequence_fields
    )
    comparison_by_run = {value["run_id"]: value for value in comparisons}
    for campaign_order, spec in enumerate(run_specs, start=1):
        run_id = str(spec["run_id"])
        manifest = manifests[run_id]
        normalized = normalized_runs[run_id]
        capture = spec["run_role"] != "EXTENSION_OFF"
        comparison = comparison_by_run[run_id]
        association_binding = manifest.get("event_extension", {}).get(
            "associations"
        ) if capture else None
        sequence_writer.writerow({
            "campaign_order": campaign_order, "run_id": run_id,
            "dataset_scope": spec["dataset_scope"],
            "sequence_order": spec.get("sequence_order", ""),
            "sequence": spec.get("sequence", ""), "run_role": spec["run_role"],
            "include_in_primary_totals": spec["include_in_primary_totals"],
            "estimator_compare_run_id": spec.get("estimator_compare_run_id") or "",
            "telemetry_compare_run_id": spec.get("telemetry_compare_run_id") or "",
            "status": manifest.get("status"), "completed": True,
            "terminal_queue_proof": bool(
                manifest.get("checks", {}).get("camera_queue_drained", False)
            ),
            "process_cleanup_passed": manifest.get("completion", {}).get(
                "process_group_survived_cleanup") is False,
            "telemetry_status": "AVAILABLE" if capture else "NOT_APPLICABLE",
            "telemetry_reason": "NONE" if capture else
            "EVENT_EXTENSION_CAPTURE_DISABLED",
            "comparison_status": comparison["status"],
            "comparison_reason": comparison["reason"],
            "source_snapshot_sha256": normalized.get("source_snapshot_sha256", ""),
            "build_provenance_id": normalized.get("build_provenance_id", ""),
            "binary_sha256": normalized.get("binary_sha256", ""),
            "config_sha256": normalized.get("config_sha256", ""),
            "calibration_sha256": normalized.get("calibration_sha256", ""),
            "adapted_input_sha256": normalized.get("adapted_input_sha256", ""),
            "reference_sha256": normalized.get("reference_sha256", ""),
            "event_extension_schema": EXTENSION_SCHEMA if capture else "",
            "raw_telemetry_sha256": normalized.get("archive", {}).get("raw", {}).get("sha256", ""),
            "archive_sha256": normalized.get("archive", {}).get("archive", {}).get("sha256", ""),
            "raw_bytes": normalized.get("archive", {}).get("raw", {}).get("size_bytes", ""),
            "archive_bytes": normalized.get("archive", {}).get("archive", {}).get("size_bytes", ""),
            "record_count": normalized.get("stream", {}).get("record_count", ""),
            "callback_count": normalized.get("counts", {}).get("callbacks", ""),
            "camera_frame_count": normalized.get("counts", {}).get("camera_frames", ""),
            "interval_count": normalized.get("counts", {}).get("intervals", ""),
            "interval_available_count": normalized.get("counts", {}).get("available", ""),
            "interval_unavailable_count": (
                normalized.get("counts", {}).get("intervals", 0) -
                normalized.get("counts", {}).get("available", 0)
            ) if capture and "counts" in normalized else "",
            "group_count": normalized.get("counts", {}).get("groups", ""),
            "member_count": normalized.get("counts", {}).get("members", ""),
            "state_sha256": normalized["estimator_output_sha256"]["state"],
            "deviation_sha256": normalized["estimator_output_sha256"]["deviation"],
            "trajectory_tum_sha256": normalized["estimator_output_sha256"]["trajectory_tum"],
            "estimator_parity_passed": comparison["status"] == "PASS",
            "telemetry_determinism_passed": comparison.get(
                "telemetry_raw_bytes_equal", ""
            ) if comparison.get("telemetry_compare_run_id") else "",
            "association_status": "AVAILABLE" if capture else "NOT_APPLICABLE",
            "association_reason": "NONE" if capture else
            "EVENT_EXTENSION_CAPTURE_DISABLED",
            "association_state_rows": association_binding.get(
                "state_association_row_count", ""
            ) if association_binding else "",
            "association_reference_rows": association_binding.get(
                "reference_association_row_count", ""
            ) if association_binding else "",
            "dataset_unchanged": ledger_before == ledger_after and
            manifest.get("checks", {}).get("fixed_inputs_unchanged", False),
        })
    _finish_csv(sequence_stream, sequence_tmp, sequence_path)

    pooled_streams: Dict[str, Any] = {}
    for stream_name in pooled_counts:
        residuals = pooled_residuals[stream_name]
        pooled_streams[stream_name] = {
            "counts": pooled_counts[stream_name],
            "residuals": _distribution(residuals, "s"),
        }
    association_coverage = {
        "schema_version": "turnsafe.session1e.association_coverage.v1",
        "association_version": "turnsafe.offline_association.v1",
        "aggregation_scope": "eleven_KAIST_capture_on_primary_runs_only",
        "callback_count": primary_totals["callbacks"],
        "streams": pooled_streams,
        "prohibited_outputs_absent": {
            "event_ids": True, "outcome_labels": True,
            "degradation_labels": True, "error_metrics": True,
        },
        "outputs": {
            "callback_state_association": output_identity(state_assoc_path),
            "callback_reference_association": output_identity(reference_assoc_path),
        },
    }
    association_path = output_dir / "ASSOCIATION_COVERAGE.json"
    _atomic_json(association_path, association_coverage)

    group_manifest = {
        "schema_version": "turnsafe.session1e.group_bearing_provenance_manifest.v1",
        "storage": "embedded_in_event_ready_telemetry_archive",
        "selector": "callbacks[].extensions.event_extension.group_bearing_provenance",
        "extension_schema": EXTENSION_SCHEMA,
        "canonical_ordering_version": "turnsafe.event_extension.order.v1",
        "primary_totals": {
            "callbacks": primary_totals["callbacks"],
            "groups": primary_totals["groups"],
            "members": primary_totals["members"],
            "member_count_buckets": member_buckets,
            "callback_status_counts": dict(sorted(group_status_counts.items())),
            "callback_reason_counts": dict(sorted(group_reason_counts.items())),
        },
        "runs": {
            spec["run_id"]: {
                "sequence_order": spec["sequence_order"],
                "sequence": spec["sequence"],
                "raw": normalized_runs[spec["run_id"]]["archive"]["raw"],
                "archive": normalized_runs[spec["run_id"]]["archive"]["archive"],
                "counts": normalized_runs[spec["run_id"]]["counts"],
            }
            for spec in primaries
        },
    }
    group_manifest_path = output_dir / "GROUP_BEARING_PROVENANCE_MANIFEST.json"
    _atomic_json(group_manifest_path, group_manifest)

    summary_distributions = {
        "gyro_source_sample_count": _distribution(
            distributions["source_sample_count"], "samples"
        ),
        "maximum_internal_sample_gap": _distribution(
            distributions["maximum_gap_s"], "s"
        ),
        "coverage_fraction": _distribution(
            distributions["coverage_fraction"], "fraction"
        ),
        "raw_peak_angular_rate": _distribution(
            distributions["raw_max_norm_rad_s"], "rad/s"
        ),
        "raw_integrated_angular_magnitude": _distribution(
            distributions["raw_integral_norm_rad"], "rad"
        ),
        "bias_corrected_peak_angular_rate": _distribution(
            distributions["bias_corrected_max_norm_rad_s"], "rad/s"
        ),
        "bias_corrected_integrated_angular_magnitude": _distribution(
            distributions["bias_corrected_integral_norm_rad"], "rad"
        ),
    }
    manifest_path = output_dir / "EVENT_READY_CORPUS_MANIFEST.json"
    report_path = output_dir / "EVENT_READY_CORPUS_REPORT.md"
    corpus_manifest: Dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA,
        "extension_schema": EXTENSION_SCHEMA,
        "aggregation_scope": {
            "primary_totals": "eleven_KAIST_capture_on_primary_runs_only",
            "off_repeats_and_MH01_excluded_from_primary_totals": True,
        },
        "primary_run_ids": [spec["run_id"] for spec in primaries],
        "repeat_run_ids": [spec["run_id"] for spec in run_specs
                           if spec["run_role"] == "CAPTURE_ON_REPEAT"],
        "aggregation_inputs": {
            "role_input": _identity(input_path),
            "aggregator_tool": _identity(Path(__file__).resolve()),
            "kaist_dataset_ledger_before": ledger_before,
            "kaist_dataset_ledger_after": ledger_after,
        },
        "runs": normalized_runs,
        "comparisons": comparisons,
        "primary_totals": primary_totals,
        "interval_unavailable_reasons": dict(sorted(
            (reason, count) for reason, count in interval_reason_counts.items()
            if reason != "NONE"
        )),
        "threshold_free_distributions": summary_distributions,
        "hard_stop": {
            "event_segmentation_performed": False,
            "outcome_labels_assigned": False,
            "scientific_thresholds_selected": False,
            "t1_or_t3_selected": False,
            "branch_recommendation_made": False,
        },
        "outputs": {},
    }
    report = """# Session 1E Event-Ready Corpus Report

This corpus contains passive causal primitives only. It performs no event
segmentation, outcome labeling, scientific threshold selection, T1/T3
classification, certificate evaluation, or branch recommendation.

## Primary development corpus

- KAIST primary sequences: 11
- callbacks: {callbacks}
- camera frames: {frames}
- causal intervals: {intervals}
- intervals available: {available}
- intervals unavailable: {unavailable}
- captured group-provenance records: {groups}
- group members: {members}
- members with complete required provenance: {complete_members}
- members with typed incomplete provenance: {incomplete_members}
- raw telemetry bytes: {raw_bytes}
- compressed archive bytes: {archive_bytes}

Unavailable interval reasons and all threshold-free distributions are recorded
in `EVENT_READY_CORPUS_MANIFEST.json`. Exact callback/output/reference joins
are in the two association CSVs and `ASSOCIATION_COVERAGE.json`. Group members
remain compactly embedded in the lossless telemetry archives; the group
manifest and counts CSV bind their location and totals without duplicating the
member corpus.

All estimator comparisons use both exact state/deviation/trajectory file bytes
and `turnsafe.baseline_digest.v1`. Required repeat telemetry comparisons use the
decoded raw JSONL SHA-256 and byte size.
""".format(
        callbacks=primary_totals["callbacks"], frames=primary_totals["camera_frames"],
        intervals=primary_totals["intervals"],
        available=primary_totals["intervals_available"],
        unavailable=primary_totals["intervals_unavailable"],
        groups=primary_totals["groups"], members=primary_totals["members"],
        complete_members=member_status_counts.get("COMPLETE", 0),
        incomplete_members=member_status_counts.get("INCOMPLETE", 0),
        raw_bytes=primary_totals["raw_telemetry_bytes"],
        archive_bytes=primary_totals["archive_bytes"],
    )
    _atomic_text(report_path, report)
    for path in (
        interval_path, availability_path, state_assoc_path,
        reference_assoc_path, association_path, group_manifest_path,
        group_counts_path, sequence_path, report_path,
    ):
        corpus_manifest["outputs"][path.name] = output_identity(path)
    _atomic_json(manifest_path, corpus_manifest)
    publish_order = [name for name in output_names
                     if name != "EVENT_READY_CORPUS_MANIFEST.json"] + [
                         "EVENT_READY_CORPUS_MANIFEST.json"]
    published: List[Path] = []
    try:
        for name in publish_order:
            target = final_output_dir / name
            os.replace(str(output_dir / name), str(target))
            published.append(target)
    except BaseException:
        # These targets were all proven absent immediately before staging and
        # are therefore tool-owned products of this invocation.
        for target in reversed(published):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        raise
    output_dir.rmdir()
    _ACTIVE_STAGING = None
    return corpus_manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-run-timeout-seconds", type=float, default=3600.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if (not math.isfinite(args.per_run_timeout_seconds) or
            args.per_run_timeout_seconds <= 0):
        print("EVENT_READY_CORPUS_ERROR: invalid timeout", file=sys.stderr)
        return 2
    try:
        aggregate(args.input.resolve(strict=True),
                  args.output_dir.resolve(strict=True),
                  args.per_run_timeout_seconds)
    except (CorpusError, OSError, ValueError, KeyError, TypeError,
            AttributeError, subprocess.TimeoutExpired) as exc:
        print("EVENT_READY_CORPUS_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
