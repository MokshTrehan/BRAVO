#!/usr/bin/env python3
"""Build a deterministic, fail-closed KAIST qualitative geometry bundle.

The OpenVINS ROS1 visualizer stamps ``points_*`` PointCloud2 messages with
wall time.  Exact-count streams retain the v2 publish-ordinal association.
When a recorder starts after a point publisher, v3 permits only an unmatched
``poseimu`` prefix followed by a proved contiguous point-stream suffix.  The
suffix is proved either directly by unique nearest bag-record times or by a
unique wall-header match to a point stream whose pose association is already
established.  Both proofs have a strict declared bound; neither interpolates
or treats wall time as sensor time.  Internal or trailing gaps fail closed.
``loop_feats`` carries a camera-time header that is offset from the state
header.  It is associated by publish ordinal only when its message count also
has exact parity with ``poseimu``.  A sparse ``loop_feats`` stream remains
raw-only: the tool does not invent a timestamp match, and every pose snapshot
gets an explicit empty loop layer.  The tool never treats MSCKF or loop update
points as a persistent map.

PLYs remain in the estimator's raw frame.  SVGs use GT-derived bounds after
applying one frozen evo-compatible, no-scale SE(3) fit to the estimate and to
every rendered feature point.  The canonical SLAM PLY is the literal final
message, including an explicitly labeled zero-vertex collapse.

The tool deliberately has no dependency on plotting libraries.  PLY and SVG
outputs are stable text encodings, and the manifest contains no generation
time or output-directory path that would make a replay nondeterministic.
"""

from __future__ import annotations

import argparse
import bisect
from dataclasses import dataclass
import hashlib
import html
import json
import math
import os
from pathlib import Path
import statistics
import struct
import sys
import tempfile
from typing import Any, Callable, Iterable, Sequence, Tuple


SCHEMA = "schurvio.icra27.kaist_geometry_bundle.v3"
LOOP_HEADER_OFFSET_STABILITY_TOLERANCE_NS = 2
POINTS_SUFFIX_PROOF_MAX_DELTA_NS = 1_000_000
POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS = 1_000_000
REQUIRED_SUFFIXES = (
    "poseimu",
    "points_slam",
    "points_msckf",
    "points_aruco",
    "loop_feats",
)
EXPECTED_TYPES = {
    "poseimu": "geometry_msgs/PoseWithCovarianceStamped",
    "points_slam": "sensor_msgs/PointCloud2",
    "points_msckf": "sensor_msgs/PointCloud2",
    "points_aruco": "sensor_msgs/PointCloud2",
    "loop_feats": "sensor_msgs/PointCloud",
}
SEMANTIC_IDS = {
    "points_slam": 1,
    "points_msckf": 2,
    "points_aruco": 3,
    "loop_feats": 4,
}
SEMANTIC_COLORS = {
    "points_slam": (35, 102, 184),
    "points_msckf": (230, 126, 34),
    "points_aruco": (171, 71, 188),
    "loop_feats": (40, 155, 91),
}
OUTPUT_PATHS = (
    "geometry/alignment_associations.csv",
    "geometry/slam_landmarks_final.ply",
    "geometry/msckf_update_points.ply",
    "geometry/loop_active_tracks_aggregate.ply",
    "geometry/snapshots/25.ply",
    "geometry/snapshots/50.ply",
    "geometry/snapshots/75.ply",
    "geometry/snapshots/max_angular_rate.ply",
    "figures/top.svg",
    "figures/side.svg",
    "figures/oblique.svg",
    "geometry_manifest.json",
    "SHA256SUMS",
)


class GeometryError(ValueError):
    """Raised when an input cannot support an auditable geometry bundle."""


@dataclass(frozen=True)
class Pose:
    timestamp: float
    position: tuple[float, float, float]
    quaternion_xyzw: tuple[float, float, float, float]


@dataclass(frozen=True)
class RecordedStream:
    namespace: str
    topics: dict[str, str]
    message_types: dict[str, str]
    record_counts: dict[str, int]
    poses: tuple[Pose, ...]
    clouds: dict[str, tuple[tuple[tuple[float, float, float], ...], ...]]
    raw_loop_clouds: tuple[tuple[tuple[float, float, float], ...], ...]
    header_stamps: dict[str, tuple[float, ...]]
    bag_record_stamps: dict[str, tuple[float, ...]]
    header_stamps_ns: dict[str, tuple[int, ...]]
    bag_record_stamps_ns: dict[str, tuple[int, ...]]
    header_sequences: dict[str, tuple[int, ...]]
    association: dict[str, Any]


@dataclass(frozen=True)
class EvoAlignment:
    """One frozen evo-compatible SE(3) transform from estimate to GT."""

    rotation: tuple[
        tuple[float, float, float],
        tuple[float, float, float],
        tuple[float, float, float],
    ]
    translation: tuple[float, float, float]
    scale: float
    associations: tuple[tuple[int, int], ...]
    max_diff_s: float
    evo_version: str
    evo_sync_path: str
    evo_trajectory_path: str
    rotation_determinant: float
    rotation_orthogonality_error_frobenius: float


def _point_cloud_at_pose(
    recorded: RecordedStream, suffix: str, poseimu_ordinal: int
) -> tuple[tuple[float, float, float], ...]:
    """Resolve one proved point suffix without fabricating missing messages."""

    topic = recorded.association["points_topic_associations"][suffix]
    prefix_count = int(topic["unobserved_leading_poseimu_count"])
    raw_ordinal = poseimu_ordinal - prefix_count
    if raw_ordinal < 0:
        raise GeometryError(
            f"{suffix}: poseimu ordinal {poseimu_ordinal} is in the unobserved "
            f"recorder-start prefix of length {prefix_count}"
        )
    clouds = recorded.clouds[suffix]
    if raw_ordinal >= len(clouds):
        raise GeometryError(f"{suffix}: proved suffix lookup exceeds raw messages")
    return clouds[raw_ordinal]


def _finite(values: Iterable[float], context: str) -> tuple[float, ...]:
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise GeometryError(f"{context}: non-finite numeric value")
    return result


def _stamp_to_float(stamp: Any, context: str) -> float:
    try:
        value = float(stamp.to_sec())
    except Exception as exc:  # ROS time objects are an input boundary.
        raise GeometryError(f"{context}: unreadable ROS timestamp") from exc
    if not math.isfinite(value) or value < 0.0:
        raise GeometryError(f"{context}: invalid ROS timestamp {value!r}")
    return value


def _stamp_to_ns(stamp: Any, context: str) -> int:
    """Read the exact integral representation of a ROS1 time value."""

    try:
        seconds = int(stamp.secs)
        nanoseconds = int(stamp.nsecs)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GeometryError(f"{context}: unreadable integral ROS timestamp") from exc
    if seconds < 0 or not 0 <= nanoseconds < 1_000_000_000:
        raise GeometryError(
            f"{context}: invalid integral ROS timestamp {seconds}s {nanoseconds}ns"
        )
    return seconds * 1_000_000_000 + nanoseconds


def _validate_frame(message: Any, expected_frame: str, context: str) -> float:
    try:
        actual_frame = str(message.header.frame_id)
        stamp = _stamp_to_float(message.header.stamp, f"{context} header")
    except AttributeError as exc:
        raise GeometryError(f"{context}: missing ROS header") from exc
    if actual_frame != expected_frame:
        raise GeometryError(
            f"{context}: frame mismatch: expected {expected_frame!r}, got {actual_frame!r}"
        )
    return stamp


def _quaternion_norm(quaternion: Sequence[float]) -> float:
    return math.sqrt(sum(value * value for value in quaternion))


def _parse_pose_message(message: Any, expected_frame: str, context: str) -> Pose:
    timestamp = _validate_frame(message, expected_frame, context)
    try:
        pose = message.pose.pose
        position = _finite(
            (pose.position.x, pose.position.y, pose.position.z),
            f"{context} position",
        )
        quaternion = _finite(
            (
                pose.orientation.x,
                pose.orientation.y,
                pose.orientation.z,
                pose.orientation.w,
            ),
            f"{context} quaternion",
        )
        covariance = _finite(message.pose.covariance, f"{context} covariance")
    except AttributeError as exc:
        raise GeometryError(f"{context}: malformed pose message") from exc
    if len(covariance) != 36:
        raise GeometryError(f"{context}: covariance count is {len(covariance)}, expected 36")
    norm = _quaternion_norm(quaternion)
    if not 0.999 <= norm <= 1.001:
        raise GeometryError(f"{context}: quaternion norm {norm:.12g} is invalid")
    return Pose(timestamp, position, quaternion)


def _pointcloud2_xyz(message: Any, expected_frame: str, context: str) -> tuple[tuple[float, float, float], ...]:
    _validate_frame(message, expected_frame, context)
    try:
        width = int(message.width)
        height = int(message.height)
        point_step = int(message.point_step)
        row_step = int(message.row_step)
        data = bytes(message.data)
        fields = list(message.fields)
        bigendian = bool(message.is_bigendian)
    except (AttributeError, TypeError, ValueError) as exc:
        raise GeometryError(f"{context}: malformed PointCloud2 metadata") from exc

    if width < 0 or height <= 0:
        raise GeometryError(f"{context}: invalid dimensions {width}x{height}")
    point_count = width * height
    if point_step <= 0:
        raise GeometryError(f"{context}: point_step must be positive")
    if row_step < width * point_step:
        raise GeometryError(f"{context}: row_step is smaller than its packed row")
    expected_bytes = row_step * height
    if len(data) != expected_bytes:
        raise GeometryError(
            f"{context}: data byte count {len(data)} does not match row_step*height {expected_bytes}"
        )

    field_by_name: dict[str, Any] = {}
    for field in fields:
        name = str(getattr(field, "name", ""))
        if name in field_by_name:
            raise GeometryError(f"{context}: duplicate PointCloud2 field {name!r}")
        field_by_name[name] = field
    missing = [axis for axis in ("x", "y", "z") if axis not in field_by_name]
    if missing:
        raise GeometryError(f"{context}: missing PointCloud2 fields {missing}")

    # sensor_msgs/PointField: FLOAT32=7 and FLOAT64=8.
    formats = {7: ("f", 4), 8: ("d", 8)}
    unpackers: list[tuple[int, str]] = []
    prefix = ">" if bigendian else "<"
    for axis in ("x", "y", "z"):
        field = field_by_name[axis]
        datatype = int(getattr(field, "datatype", -1))
        count = int(getattr(field, "count", -1))
        offset = int(getattr(field, "offset", -1))
        if datatype not in formats or count != 1:
            raise GeometryError(
                f"{context}: field {axis!r} must be scalar FLOAT32/FLOAT64"
            )
        code, size = formats[datatype]
        if offset < 0 or offset + size > point_step:
            raise GeometryError(f"{context}: field {axis!r} lies outside point_step")
        unpackers.append((offset, prefix + code))

    points: list[tuple[float, float, float]] = []
    for row in range(height):
        row_start = row * row_step
        for column in range(width):
            point_start = row_start + column * point_step
            values = tuple(
                float(struct.unpack_from(fmt, data, point_start + offset)[0])
                for offset, fmt in unpackers
            )
            points.append(_finite(values, f"{context} point {len(points)}"))
    if len(points) != point_count:
        raise GeometryError(f"{context}: decoded point count mismatch")
    return tuple(points)


def _legacy_pointcloud_xyz(message: Any, expected_frame: str, context: str) -> tuple[tuple[float, float, float], ...]:
    _validate_frame(message, expected_frame, context)
    try:
        raw_points = list(message.points)
        channels = list(message.channels)
    except (AttributeError, TypeError) as exc:
        raise GeometryError(f"{context}: malformed PointCloud") from exc
    points = tuple(
        _finite((point.x, point.y, point.z), f"{context} point {index}")
        for index, point in enumerate(raw_points)
    )
    for channel_index, channel in enumerate(channels):
        try:
            _finite(channel.values, f"{context} channel {channel_index}")
        except AttributeError as exc:
            raise GeometryError(f"{context}: malformed channel {channel_index}") from exc
    return points


def _normalize_namespace(namespace: str) -> str:
    stripped = namespace.strip()
    if not stripped or stripped == "/":
        return ""
    if not stripped.startswith("/"):
        stripped = "/" + stripped
    return stripped.rstrip("/")


def _topic(namespace: str, suffix: str) -> str:
    return f"{namespace}/{suffix}" if namespace else f"/{suffix}"


def _infer_namespace(topic_names: Iterable[str]) -> str:
    candidates: list[str] = []
    for topic_name in topic_names:
        if topic_name == "/poseimu":
            candidates.append("")
        elif topic_name.endswith("/poseimu"):
            candidates.append(topic_name[: -len("/poseimu")])
    candidates = sorted(set(candidates))
    if len(candidates) != 1:
        raise GeometryError(
            "cannot infer one private namespace from poseimu; "
            f"found {candidates or 'none'}"
        )
    return candidates[0]


def _paired_stamp_delta_summary_ns(
    left: Sequence[int], right: Sequence[int], context: str
) -> dict[str, int | float | bool]:
    """Summarize paired ROS timestamp deltas without using them to associate."""

    if len(left) != len(right) or not left:
        raise GeometryError(
            f"{context}: paired timestamp diagnostics require equal nonzero counts"
        )
    deltas_ns = tuple(left_stamp - right_stamp for left_stamp, right_stamp in zip(left, right))
    minimum = min(deltas_ns)
    maximum = max(deltas_ns)
    return {
        "count": len(deltas_ns),
        "minimum_ns": minimum,
        "median_ns": float(statistics.median(deltas_ns)),
        "maximum_ns": maximum,
        "range_ns": maximum - minimum,
        "maximum_absolute_ns": max(abs(value) for value in deltas_ns),
        "unique_delta_count": len(set(deltas_ns)),
        "all_deltas_identical": len(set(deltas_ns)) == 1,
    }


def _strictly_increasing(values: Sequence[int]) -> bool:
    return all(current > previous for previous, current in zip(values, values[1:]))


def _header_sequence_proof(values: Sequence[int]) -> dict[str, Any]:
    valid_range = bool(values) and all(0 <= value <= 0xFFFFFFFF for value in values)
    increments_exactly_one = valid_range and all(
        current == previous + 1
        for previous, current in zip(values, values[1:])
    )
    return {
        "accepted": bool(valid_range and increments_exactly_one),
        "count": len(values),
        "all_values_are_uint32": valid_range,
        "increments_exactly_one_without_gap_or_wrap": increments_exactly_one,
        "first": values[0] if values else None,
        "last": values[-1] if values else None,
    }


def _unique_nearest_indices_ns(
    source: Sequence[int], target: Sequence[int]
) -> tuple[tuple[int, ...], int, int | None, int | None]:
    """Return exact nearest indices and the tie count for integral timestamps."""

    if not source or not target or not _strictly_increasing(target):
        return tuple(), len(source), None, None
    indices: list[int] = []
    tie_count = 0
    margins: list[tuple[int, int]] = []
    for source_index, stamp in enumerate(source):
        insertion = bisect.bisect_left(target, stamp)
        candidates = tuple(
            index
            for index in range(insertion - 2, insertion + 2)
            if 0 <= index < len(target)
        )
        if not candidates:
            return tuple(), len(source), None, None
        ranked = sorted(
            ((abs(stamp - target[index]), index) for index in candidates),
            key=lambda value: (value[0], value[1]),
        )
        minimum = ranked[0][0]
        winners = tuple(
            index
            for distance, index in ranked
            if distance == minimum
        )
        if len(winners) != 1:
            tie_count += 1
        indices.append(winners[0])
        if len(ranked) >= 2:
            margins.append((ranked[1][0] - ranked[0][0], source_index))
    if not margins:
        return tuple(indices), tie_count, None, None
    minimum_margin, worst_source_index = min(margins)
    return tuple(indices), tie_count, minimum_margin, worst_source_index


def _direct_points_suffix_proof(
    point_record_stamps_ns: Sequence[int],
    pose_record_stamps_ns: Sequence[int],
    pose_prefix_count: int,
) -> dict[str, Any]:
    """Test a forced point-ordinal to pose-suffix mapping without interpolation."""

    expected = tuple(
        range(pose_prefix_count, pose_prefix_count + len(point_record_stamps_ns))
    )
    shape_valid = (
        bool(point_record_stamps_ns)
        and 0 <= pose_prefix_count < len(pose_record_stamps_ns)
        and len(point_record_stamps_ns) + pose_prefix_count
        == len(pose_record_stamps_ns)
    )
    point_monotonic = _strictly_increasing(point_record_stamps_ns)
    pose_monotonic = _strictly_increasing(pose_record_stamps_ns)
    (
        observed,
        tie_count,
        minimum_margin,
        worst_margin_ordinal,
    ) = _unique_nearest_indices_ns(point_record_stamps_ns, pose_record_stamps_ns)
    nearest_strictly_increasing = (
        len(observed) == len(point_record_stamps_ns)
        and _strictly_increasing(observed)
    )
    mismatch_count = (
        sum(left != right for left, right in zip(observed, expected))
        + abs(len(observed) - len(expected))
    )
    offset_summary: dict[str, Any] | None = None
    if shape_valid:
        offset_summary = _paired_stamp_delta_summary_ns(
            point_record_stamps_ns,
            pose_record_stamps_ns[pose_prefix_count:],
            "points_* minus forced poseimu-suffix bag-record offsets",
        )
        offset_summary["maximum_allowed_absolute_ns"] = (
            POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
        offset_summary["within_declared_bound"] = (
            offset_summary["maximum_absolute_ns"]
            <= POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
    accepted = bool(
        shape_valid
        and point_monotonic
        and pose_monotonic
        and tie_count == 0
        and nearest_strictly_increasing
        and mismatch_count == 0
        and minimum_margin is not None
        and minimum_margin >= POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        and offset_summary is not None
        and offset_summary["within_declared_bound"]
    )
    return {
        "policy": (
            "forced_count_difference_suffix_then_unique_nearest_bag_record_time_gate"
        ),
        "accepted": accepted,
        "pose_prefix_count": pose_prefix_count,
        "shape_is_exact_contiguous_suffix": shape_valid,
        "point_record_stamps_strictly_increasing": point_monotonic,
        "poseimu_record_stamps_strictly_increasing": pose_monotonic,
        "unique_nearest_for_every_message": tie_count == 0 and len(observed) == len(expected),
        "nearest_tie_count": tie_count,
        "nearest_indices_strictly_increasing_and_unique": nearest_strictly_increasing,
        "nearest_indices_exact_forced_suffix": mismatch_count == 0,
        "nearest_index_mismatch_count": mismatch_count,
        "minimum_nearest_competitor_margin_ns": minimum_margin,
        "minimum_required_nearest_competitor_margin_ns": (
            POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        ),
        "worst_nearest_competitor_margin_source_ordinal_zero_based": (
            worst_margin_ordinal
        ),
        "nearest_competitor_margin_within_declared_requirement": (
            minimum_margin is not None
            and minimum_margin >= POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        ),
        "first_nearest_poseimu_ordinal_zero_based": observed[0] if observed else None,
        "last_nearest_poseimu_ordinal_zero_based": observed[-1] if observed else None,
        "bag_record_offset_diagnostics": offset_summary,
        "timestamp_interpolation_used": False,
    }


def _point_header_to_pose_record_suffix_proof(
    point_header_stamps_ns: Sequence[int],
    point_record_stamps_ns: Sequence[int],
    pose_record_stamps_ns: Sequence[int],
    pose_prefix_count: int,
) -> dict[str, Any]:
    """Prove the forced suffix using publisher wall time versus pose receipt time."""

    proof = _direct_points_suffix_proof(
        point_header_stamps_ns,
        pose_record_stamps_ns,
        pose_prefix_count,
    )
    shape_matches_own_records = (
        len(point_header_stamps_ns) == len(point_record_stamps_ns)
    )
    causal = shape_matches_own_records and all(
        header <= record
        for header, record in zip(point_header_stamps_ns, point_record_stamps_ns)
    )
    proof["policy"] = (
        "forced_count_difference_suffix_then_unique_nearest_point_wall_header_"
        "to_poseimu_bag_record_gate"
    )
    proof["accepted"] = bool(proof["accepted"] and causal)
    proof["point_header_count_matches_own_record_count"] = (
        shape_matches_own_records
    )
    proof["every_point_header_not_after_own_bag_record_time"] = causal
    proof["point_wall_header_is_sensor_time"] = False
    proof["point_wall_header_to_poseimu_record_offset_diagnostics"] = proof.pop(
        "bag_record_offset_diagnostics"
    )
    proof["point_header_stamps_strictly_increasing"] = proof.pop(
        "point_record_stamps_strictly_increasing"
    )
    proof["poseimu_bag_record_stamps_strictly_increasing"] = proof.pop(
        "poseimu_record_stamps_strictly_increasing"
    )
    return proof


def _peer_header_suffix_proof(
    point_header_stamps_ns: Sequence[int],
    point_record_stamps_ns: Sequence[int],
    peer_header_stamps_ns: Sequence[int],
    point_pose_prefix_count: int,
    peer_pose_prefix_count: int,
) -> dict[str, Any]:
    """Prove point ordinals via one already-associated point-stream peer."""

    expected = tuple(
        point_pose_prefix_count + index - peer_pose_prefix_count
        for index in range(len(point_header_stamps_ns))
    )
    shape_valid = (
        bool(point_header_stamps_ns)
        and len(point_header_stamps_ns) == len(point_record_stamps_ns)
        and all(0 <= index < len(peer_header_stamps_ns) for index in expected)
    )
    point_monotonic = _strictly_increasing(point_header_stamps_ns)
    peer_monotonic = _strictly_increasing(peer_header_stamps_ns)
    point_headers_causal = shape_valid and all(
        header <= record
        for header, record in zip(point_header_stamps_ns, point_record_stamps_ns)
    )
    (
        observed,
        tie_count,
        minimum_margin,
        worst_margin_ordinal,
    ) = _unique_nearest_indices_ns(point_header_stamps_ns, peer_header_stamps_ns)
    nearest_strictly_increasing = (
        len(observed) == len(point_header_stamps_ns)
        and _strictly_increasing(observed)
    )
    mismatch_count = (
        sum(left != right for left, right in zip(observed, expected))
        + abs(len(observed) - len(expected))
    )
    offset_summary: dict[str, Any] | None = None
    if shape_valid:
        paired_peer = tuple(peer_header_stamps_ns[index] for index in expected)
        offset_summary = _paired_stamp_delta_summary_ns(
            point_header_stamps_ns,
            paired_peer,
            "points_* minus peer points_* wall-header offsets",
        )
        offset_summary["maximum_allowed_absolute_ns"] = (
            POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
        offset_summary["within_declared_bound"] = (
            offset_summary["maximum_absolute_ns"]
            <= POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
    accepted = bool(
        shape_valid
        and point_monotonic
        and peer_monotonic
        and point_headers_causal
        and tie_count == 0
        and nearest_strictly_increasing
        and mismatch_count == 0
        and minimum_margin is not None
        and minimum_margin >= POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        and offset_summary is not None
        and offset_summary["within_declared_bound"]
    )
    return {
        "policy": (
            "forced_pose_suffix_then_unique_nearest_established_peer_wall_header_gate"
        ),
        "accepted": accepted,
        "point_pose_prefix_count": point_pose_prefix_count,
        "peer_pose_prefix_count": peer_pose_prefix_count,
        "shape_has_peer_for_every_forced_pose_ordinal": shape_valid,
        "point_header_stamps_strictly_increasing": point_monotonic,
        "peer_header_stamps_strictly_increasing": peer_monotonic,
        "every_point_header_not_after_own_bag_record_time": point_headers_causal,
        "unique_nearest_for_every_message": tie_count == 0 and len(observed) == len(expected),
        "nearest_tie_count": tie_count,
        "nearest_indices_strictly_increasing_and_unique": nearest_strictly_increasing,
        "nearest_indices_exact_forced_peer_suffix": mismatch_count == 0,
        "nearest_index_mismatch_count": mismatch_count,
        "minimum_nearest_competitor_margin_ns": minimum_margin,
        "minimum_required_nearest_competitor_margin_ns": (
            POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        ),
        "worst_nearest_competitor_margin_source_ordinal_zero_based": (
            worst_margin_ordinal
        ),
        "nearest_competitor_margin_within_declared_requirement": (
            minimum_margin is not None
            and minimum_margin >= POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        ),
        "first_nearest_peer_ordinal_zero_based": observed[0] if observed else None,
        "last_nearest_peer_ordinal_zero_based": observed[-1] if observed else None,
        "wall_header_offset_diagnostics": offset_summary,
        "wall_header_is_sensor_time": False,
        "timestamp_interpolation_used": False,
    }


def _point_header_anchor_proof(
    point_header_stamps_ns: Sequence[int],
    point_record_stamps_ns: Sequence[int],
) -> dict[str, Any]:
    """Validate that one established point stream is safe as a header peer."""

    shape_valid = (
        bool(point_header_stamps_ns)
        and len(point_header_stamps_ns) == len(point_record_stamps_ns)
    )
    header_monotonic = _strictly_increasing(point_header_stamps_ns)
    record_monotonic = _strictly_increasing(point_record_stamps_ns)
    causal = shape_valid and all(
        header <= record
        for header, record in zip(point_header_stamps_ns, point_record_stamps_ns)
    )
    lag_summary: dict[str, Any] | None = None
    if shape_valid:
        lag_summary = _paired_stamp_delta_summary_ns(
            point_record_stamps_ns,
            point_header_stamps_ns,
            "points_* bag-record minus own wall-header offsets",
        )
        lag_summary["maximum_allowed_absolute_ns"] = (
            POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
        lag_summary["within_declared_bound"] = (
            lag_summary["maximum_absolute_ns"]
            <= POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        )
    accepted = bool(
        shape_valid
        and header_monotonic
        and record_monotonic
        and causal
        and lag_summary is not None
        and lag_summary["within_declared_bound"]
    )
    return {
        "policy": "strict_monotonic_causal_wall_header_with_bounded_record_lag",
        "accepted": accepted,
        "count_parity": shape_valid,
        "header_stamps_strictly_increasing": header_monotonic,
        "bag_record_stamps_strictly_increasing": record_monotonic,
        "every_header_not_after_own_bag_record_time": causal,
        "bag_record_minus_header_lag_diagnostics": lag_summary,
        "wall_header_is_sensor_time": False,
    }


def read_feature_bag(
    bag_path: Path,
    namespace: str | None = None,
    expected_frame: str = "global",
) -> RecordedStream:
    """Read and strictly validate all private geometry topics in a ROS1 bag."""

    if not bag_path.is_file():
        raise GeometryError(f"feature bag is not a regular file: {bag_path}")
    try:
        import rosbag  # type: ignore
    except Exception as exc:
        raise GeometryError("the ROS1 rosbag Python module is unavailable") from exc

    grouped_messages: dict[str, list[Any]] = {suffix: [] for suffix in REQUIRED_SUFFIXES}
    header_stamps: dict[str, list[float]] = {suffix: [] for suffix in REQUIRED_SUFFIXES}
    record_stamps: dict[str, list[float]] = {suffix: [] for suffix in REQUIRED_SUFFIXES}
    header_stamps_ns: dict[str, list[int]] = {suffix: [] for suffix in REQUIRED_SUFFIXES}
    record_stamps_ns: dict[str, list[int]] = {suffix: [] for suffix in REQUIRED_SUFFIXES}
    header_sequences: dict[str, list[int]] = {
        suffix: [] for suffix in REQUIRED_SUFFIXES
    }
    message_types: dict[str, str] = {}
    record_counts: dict[str, int] = {}

    try:
        with rosbag.Bag(str(bag_path), "r") as bag:
            info = bag.get_type_and_topic_info()
            topic_info = info.topics
            resolved_namespace = (
                _normalize_namespace(namespace)
                if namespace is not None
                else _infer_namespace(topic_info.keys())
            )
            topics = {
                suffix: _topic(resolved_namespace, suffix)
                for suffix in REQUIRED_SUFFIXES
            }
            unexpected_topics = sorted(set(topic_info) - set(topics.values()))
            if unexpected_topics:
                raise GeometryError(
                    f"feature bag contains unexpected topics: {unexpected_topics}"
                )
            for suffix, topic_name in topics.items():
                if topic_name not in topic_info:
                    raise GeometryError(f"feature bag lacks required topic {topic_name}")
                details = topic_info[topic_name]
                actual_type = str(details.msg_type)
                if actual_type != EXPECTED_TYPES[suffix]:
                    raise GeometryError(
                        f"{topic_name}: type mismatch: expected {EXPECTED_TYPES[suffix]}, got {actual_type}"
                    )
                if int(details.connections) != 1:
                    raise GeometryError(
                        f"{topic_name}: expected one publisher connection, got {details.connections}"
                    )
                message_types[suffix] = actual_type
                record_counts[suffix] = int(details.message_count)

            reverse_topics = {topic_name: suffix for suffix, topic_name in topics.items()}
            for topic_name, message, record_stamp in bag.read_messages(
                topics=list(reverse_topics)
            ):
                suffix = reverse_topics[topic_name]
                grouped_messages[suffix].append(message)
                record_stamps[suffix].append(
                    _stamp_to_float(record_stamp, f"{topic_name} bag record")
                )
                record_stamps_ns[suffix].append(
                    _stamp_to_ns(record_stamp, f"{topic_name} bag record")
                )
                header_stamps[suffix].append(
                    _validate_frame(
                        message,
                        expected_frame,
                        f"{topic_name} message {len(grouped_messages[suffix]) - 1}",
                    )
                )
                header_stamps_ns[suffix].append(
                    _stamp_to_ns(
                        message.header.stamp,
                        f"{topic_name} message {len(grouped_messages[suffix]) - 1} header",
                    )
                )
                try:
                    header_sequences[suffix].append(int(message.header.seq))
                except (AttributeError, TypeError, ValueError) as exc:
                    raise GeometryError(
                        f"{topic_name}: unreadable header sequence"
                    ) from exc
    except GeometryError:
        raise
    except Exception as exc:
        raise GeometryError(f"unreadable feature bag {bag_path}: {exc}") from exc

    for suffix in REQUIRED_SUFFIXES:
        observed = len(grouped_messages[suffix])
        if observed != record_counts[suffix]:
            raise GeometryError(
                f"{topics[suffix]}: bag index count {record_counts[suffix]} != decoded count {observed}"
            )
    pose_count = len(grouped_messages["poseimu"])
    if pose_count == 0:
        raise GeometryError("feature bag contains no poseimu states")
    ordinal_suffixes = ("points_slam", "points_msckf", "points_aruco")
    invalid_point_counts = {
        suffix: len(grouped_messages[suffix])
        for suffix in ordinal_suffixes
        if not 0 < len(grouped_messages[suffix]) <= pose_count
    }
    if invalid_point_counts:
        raise GeometryError(
            "points_* counts must be nonzero and cannot exceed "
            f"poseimu={pose_count}: {invalid_point_counts}"
        )

    poses = tuple(
        _parse_pose_message(message, expected_frame, f"{topics['poseimu']} message {index}")
        for index, message in enumerate(grouped_messages["poseimu"])
    )
    for previous, current in zip(poses, poses[1:]):
        if current.timestamp <= previous.timestamp:
            raise GeometryError("poseimu sensor timestamps are not strictly increasing")

    raw_point_clouds: dict[
        str, tuple[tuple[tuple[float, float, float], ...], ...]
    ] = {}
    for suffix in ordinal_suffixes:
        raw_point_clouds[suffix] = tuple(
            _pointcloud2_xyz(message, expected_frame, f"{topics[suffix]} message {index}")
            for index, message in enumerate(grouped_messages[suffix])
        )

    # A count difference forces exactly one possible suffix offset if and only
    # if the missing messages are a recorder-start pose prefix.  Prove that
    # forced mapping; never search over offsets or interpolate timestamps.
    point_prefix_counts = {
        suffix: pose_count - len(raw_point_clouds[suffix])
        for suffix in ordinal_suffixes
    }
    header_sequence_proofs = {
        suffix: _header_sequence_proof(header_sequences[suffix])
        for suffix in REQUIRED_SUFFIXES
    }
    if any(point_prefix_counts.values()) and not header_sequence_proofs[
        "poseimu"
    ]["accepted"]:
        raise GeometryError(
            "poseimu header sequence is not contiguous during points_* suffix proof"
        )
    direct_point_proofs = {
        suffix: _direct_points_suffix_proof(
            record_stamps_ns[suffix],
            record_stamps_ns["poseimu"],
            point_prefix_counts[suffix],
        )
        for suffix in ordinal_suffixes
    }
    point_header_to_pose_record_proofs = {
        suffix: _point_header_to_pose_record_suffix_proof(
            header_stamps_ns[suffix],
            record_stamps_ns[suffix],
            record_stamps_ns["poseimu"],
            point_prefix_counts[suffix],
        )
        for suffix in ordinal_suffixes
    }
    point_header_anchor_proofs = {
        suffix: _point_header_anchor_proof(
            header_stamps_ns[suffix], record_stamps_ns[suffix]
        )
        for suffix in ordinal_suffixes
    }
    established_topics = {
        suffix
        for suffix in ordinal_suffixes
        if point_prefix_counts[suffix] == 0
        or (
            direct_point_proofs[suffix]["accepted"]
            and header_sequence_proofs[suffix]["accepted"]
        )
    }
    peer_header_anchor_topics = {
        suffix
        for suffix in established_topics
        if point_header_anchor_proofs[suffix]["accepted"]
        and header_sequence_proofs[suffix]["accepted"]
    }
    point_associations: dict[str, Any] = {}
    clouds: dict[str, tuple[tuple[tuple[float, float, float], ...], ...]] = {}
    for suffix in ordinal_suffixes:
        prefix_count = point_prefix_counts[suffix]
        peer_proofs: dict[str, Any] = {}
        accepted_peers: list[str] = []
        if prefix_count == 0:
            mode = "V2_EXACT_COUNT_PUBLISH_ORDINAL"
            proof_used = "EXACT_COUNT_PARITY"
        elif (
            direct_point_proofs[suffix]["accepted"]
            and header_sequence_proofs[suffix]["accepted"]
        ):
            mode = "PROVEN_CONTIGUOUS_POSE_SUFFIX_BAG_RECORD_TIME"
            proof_used = "UNIQUE_NEAREST_BAG_RECORD_TIME"
        else:
            for peer in ordinal_suffixes:
                if peer == suffix or peer not in peer_header_anchor_topics:
                    continue
                proof = _peer_header_suffix_proof(
                    header_stamps_ns[suffix],
                    record_stamps_ns[suffix],
                    header_stamps_ns[peer],
                    prefix_count,
                    point_prefix_counts[peer],
                )
                peer_proofs[peer] = proof
                if proof["accepted"]:
                    accepted_peers.append(peer)
            if (
                not point_header_to_pose_record_proofs[suffix]["accepted"]
                or not header_sequence_proofs[suffix]["accepted"]
                or not accepted_peers
            ):
                raise GeometryError(
                    f"{topics[suffix]}: count deficit cannot be proved as a "
                    f"recorder-start contiguous poseimu suffix; poseimu={pose_count}, "
                    f"points={len(raw_point_clouds[suffix])}, prefix={prefix_count}, "
                    f"direct_record_proof={direct_point_proofs[suffix]}, "
                    "point_header_to_pose_record_proof="
                    f"{point_header_to_pose_record_proofs[suffix]}, "
                    f"header_sequence_proof={header_sequence_proofs[suffix]}, "
                    f"peer_header_proofs={peer_proofs}"
                )
            mode = (
                "PROVEN_CONTIGUOUS_POSE_SUFFIX_HEADER_TO_POSE_RECORD_"
                "AND_PEER_HEADER_TIME"
            )
            proof_used = (
                "UNIQUE_NEAREST_POINT_WALL_HEADER_TO_POSE_RECORD_AND_"
                "ESTABLISHED_PEER_WALL_HEADER"
            )

        clouds[suffix] = raw_point_clouds[suffix]
        point_associations[suffix] = {
            "mode": mode,
            "proof_used": proof_used,
            "raw_message_count": len(raw_point_clouds[suffix]),
            "poseimu_message_count": pose_count,
            "exact_count_parity": prefix_count == 0,
            "unobserved_leading_poseimu_count": prefix_count,
            "snapshot_associated_count": len(raw_point_clouds[suffix]),
            "first_associated_poseimu_ordinal_zero_based": prefix_count,
            "last_associated_poseimu_ordinal_zero_based": pose_count - 1,
            "direct_bag_record_proof": direct_point_proofs[suffix],
            "point_wall_header_to_poseimu_bag_record_proof": (
                point_header_to_pose_record_proofs[suffix]
            ),
            "own_wall_header_anchor_proof": point_header_anchor_proofs[suffix],
            "header_sequence_proof": header_sequence_proofs[suffix],
            "peer_wall_header_proofs": peer_proofs,
            "accepted_peer_topics": accepted_peers,
            "mapping_is_fixed_ordinal_suffix_after_proof": True,
            "internal_or_trailing_gaps_permitted": False,
            "timestamp_interpolation_used": False,
        }

    # loop_feats uses camera time rather than the poseimu state time.  Decode
    # every raw cloud first.  Full-count streams have one publication per
    # update and are associated by the same per-topic bag/publish ordinal used
    # for points_*.  When sparse, the missing publication ordinal is unknowable:
    # bag-record nearest-neighbor matching can collide and would manufacture an
    # association.  Keep every raw cloud for the aggregate evidence PLY, but
    # deliberately populate no pose snapshot loop layer.
    raw_loop_clouds = tuple(
        _legacy_pointcloud_xyz(
            message,
            expected_frame,
            f"{topics['loop_feats']} message {message_index}",
        )
        for message_index, message in enumerate(grouped_messages["loop_feats"])
    )
    loop_count = len(raw_loop_clouds)
    if loop_count > pose_count:
        raise GeometryError(
            f"loop_feats count {loop_count} exceeds poseimu count {pose_count}"
        )

    if loop_count == pose_count:
        loop_by_pose = raw_loop_clouds
        loop_mode = "FULL_COUNT_PUBLISH_ORDINAL"
        snapshot_associated_count = pose_count
        snapshot_explicit_empty_count = 0
        header_offset_diagnostics = _paired_stamp_delta_summary_ns(
            header_stamps_ns["loop_feats"],
            header_stamps_ns["poseimu"],
            "loop_feats minus poseimu ordinal header offsets",
        )
        header_offset_diagnostics.update(
            {
                "diagnostic_only_not_association_gate": True,
                "declared_near_invariant_range_tolerance_ns": (
                    LOOP_HEADER_OFFSET_STABILITY_TOLERANCE_NS
                ),
                "within_declared_near_invariant_range_tolerance": (
                    header_offset_diagnostics["range_ns"]
                    <= LOOP_HEADER_OFFSET_STABILITY_TOLERANCE_NS
                ),
            }
        )
        record_offset_diagnostics: dict[str, Any] | None = (
            _paired_stamp_delta_summary_ns(
                record_stamps_ns["loop_feats"],
                record_stamps_ns["poseimu"],
                "loop_feats minus poseimu ordinal bag-record offsets",
            )
        )
    else:
        loop_by_pose = tuple(tuple() for _ in poses)
        loop_mode = "SPARSE_RAW_ONLY_UNASSOCIATED"
        snapshot_associated_count = 0
        snapshot_explicit_empty_count = pose_count
        header_offset_diagnostics = None
        record_offset_diagnostics = None
    clouds["loop_feats"] = tuple(loop_by_pose)

    association: dict[str, Any] = {
        "points_policy": (
            "per_topic_publish_ordinal_after_exact_count_or_proven_"
            "contiguous_terminal_poseimu_suffix"
        ),
        "points_v2_exact_count_publish_ordinal_behavior_retained": True,
        "points_poseimu_prefix_suffix_extension_enabled": True,
        "points_suffix_proof_maximum_absolute_delta_ns": (
            POINTS_SUFFIX_PROOF_MAX_DELTA_NS
        ),
        "points_suffix_proof_minimum_nearest_competitor_margin_ns": (
            POINTS_SUFFIX_PROOF_MIN_NEAREST_MARGIN_NS
        ),
        "points_prefix_interpretation": (
            "consistent_with_recorder_start_subscription_lag_not_observed_cause"
        ),
        "points_internal_or_trailing_gaps_permitted": False,
        "points_timestamp_interpolation_used": False,
        "points_topics": [
            "points_slam",
            "points_msckf",
            "points_aruco",
        ],
        "points_all_streams_exact_count_parity": all(
            point_prefix_counts[suffix] == 0 for suffix in ordinal_suffixes
        ),
        "points_topic_associations": point_associations,
        "points_reason": (
            "OpenVINS points_* PointCloud2 headers carry wall time; wall headers "
            "can prove same-update peer ordinals but are never sensor time"
        ),
        "points_header_timestamps_are_never_treated_as_sensor_time": True,
        "points_bag_record_and_peer_header_times_are_proof_gates_only": True,
        "loop_feats_mode": loop_mode,
        "loop_feats_policy": (
            "publish_ordinal_only_under_exact_poseimu_count_parity; "
            "otherwise_raw_only_unassociated"
        ),
        "loop_feats_count_parity": loop_count == pose_count,
        "loop_feats_raw_message_count": loop_count,
        "loop_feats_raw_point_count": sum(len(cloud) for cloud in raw_loop_clouds),
        "loop_feats_snapshot_associated_count": snapshot_associated_count,
        "loop_feats_snapshot_explicit_empty_count": snapshot_explicit_empty_count,
        "loop_feats_unassociated_raw_message_count": (
            0 if loop_count == pose_count else loop_count
        ),
        "loop_feats_sparse_stream_timestamp_matching_forbidden": True,
        "loop_feats_header_offset_diagnostics": header_offset_diagnostics,
        "loop_feats_bag_record_offset_diagnostics": record_offset_diagnostics,
        "loop_feats_offset_sign_convention": "loop_feats_minus_poseimu_at_same_publish_ordinal",
        "loop_feats_offsets_are_diagnostic_only": True,
        "header_sequence_diagnostics": header_sequence_proofs,
        "poseimu_first_sensor_timestamp_s": poses[0].timestamp,
        "poseimu_last_sensor_timestamp_s": poses[-1].timestamp,
    }

    return RecordedStream(
        namespace=resolved_namespace,
        topics=topics,
        message_types=message_types,
        record_counts=record_counts,
        poses=poses,
        clouds=clouds,
        raw_loop_clouds=raw_loop_clouds,
        header_stamps={key: tuple(value) for key, value in header_stamps.items()},
        bag_record_stamps={key: tuple(value) for key, value in record_stamps.items()},
        header_stamps_ns={key: tuple(value) for key, value in header_stamps_ns.items()},
        bag_record_stamps_ns={key: tuple(value) for key, value in record_stamps_ns.items()},
        header_sequences={key: tuple(value) for key, value in header_sequences.items()},
        association=association,
    )


def read_trajectory(path: Path, trajectory_format: str = "auto") -> tuple[tuple[Pose, ...], str]:
    """Read TUM or OpenVINS total-state text without interpolation."""

    if not path.is_file():
        raise GeometryError(f"trajectory is not a regular file: {path}")
    if trajectory_format not in {"auto", "tum", "openvins-state"}:
        raise GeometryError(f"unsupported trajectory format {trajectory_format!r}")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise GeometryError(f"unreadable trajectory {path}: {exc}") from exc
    header_lines = [line.strip() for line in raw_lines if line.strip().startswith("#")]
    resolved_format = trajectory_format
    if resolved_format == "auto":
        state_header = any(
            "timestamp(s) q p" in line or "timestamp q p" in line
            for line in header_lines
        )
        resolved_format = "openvins-state" if state_header else "tum"

    poses: list[Pose] = []
    for line_number, raw_line in enumerate(raw_lines, start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 8:
            raise GeometryError(
                f"{path}:{line_number}: expected at least eight columns, got {len(fields)}"
            )
        try:
            numeric_values = tuple(float(field) for field in fields)
        except ValueError as exc:
            raise GeometryError(f"{path}:{line_number}: non-numeric value") from exc
        values = _finite(numeric_values, f"{path}:{line_number}")
        if resolved_format == "tum":
            position = values[1:4]
            quaternion = values[4:8]
        else:
            quaternion = values[1:5]
            position = values[5:8]
        norm = _quaternion_norm(quaternion)
        if not 0.999 <= norm <= 1.001:
            raise GeometryError(
                f"{path}:{line_number}: quaternion norm {norm:.12g} is invalid"
            )
        poses.append(Pose(values[0], tuple(position), tuple(quaternion)))
    if not poses:
        raise GeometryError(f"trajectory contains no poses: {path}")
    for previous, current in zip(poses, poses[1:]):
        if current.timestamp <= previous.timestamp:
            raise GeometryError(f"trajectory timestamps are not strictly increasing: {path}")
    return tuple(poses), resolved_format


def validate_capture_trajectory(
    recorded: Sequence[Pose],
    trajectory: Sequence[Pose],
    timestamp_tolerance_s: float = 5.1e-6,
    pose_tolerance: float = 2.0e-6,
) -> dict[str, Any]:
    if not recorded or not trajectory:
        raise GeometryError("capture trajectory validation requires nonempty inputs")
    max_timestamp_error = 0.0
    max_position_error = 0.0
    max_quaternion_error = 0.0
    trajectory_stamps = [pose.timestamp for pose in trajectory]
    matched_indices: list[int] = []
    previous_trajectory_index = -1
    for index, message_pose in enumerate(recorded):
        insertion = bisect.bisect_left(
            trajectory_stamps,
            message_pose.timestamp,
            lo=previous_trajectory_index + 1,
        )
        candidates = [
            candidate
            for candidate in (insertion - 1, insertion)
            if previous_trajectory_index < candidate < len(trajectory)
        ]
        if not candidates:
            raise GeometryError(
                f"capture trajectory has no unique row for poseimu ordinal {index}"
            )
        trajectory_index = min(
            candidates,
            key=lambda candidate: (
                abs(trajectory[candidate].timestamp - message_pose.timestamp),
                candidate,
            ),
        )
        text_pose = trajectory[trajectory_index]
        timestamp_error = abs(message_pose.timestamp - text_pose.timestamp)
        position_error = max(
            abs(left - right)
            for left, right in zip(message_pose.position, text_pose.position)
        )
        direct_q_error = max(
            abs(left - right)
            for left, right in zip(message_pose.quaternion_xyzw, text_pose.quaternion_xyzw)
        )
        flipped_q_error = max(
            abs(left + right)
            for left, right in zip(message_pose.quaternion_xyzw, text_pose.quaternion_xyzw)
        )
        quaternion_error = min(direct_q_error, flipped_q_error)
        if timestamp_error > timestamp_tolerance_s:
            raise GeometryError(
                f"capture trajectory has no unique row within tolerance for poseimu ordinal "
                f"{index}: nearest error {timestamp_error:.12g}s"
            )
        if position_error > pose_tolerance:
            raise GeometryError(
                f"capture trajectory position mismatch at ordinal {index}: {position_error:.12g}"
            )
        if quaternion_error > pose_tolerance:
            raise GeometryError(
                f"capture trajectory quaternion mismatch at ordinal {index}: {quaternion_error:.12g}"
            )
        max_timestamp_error = max(max_timestamp_error, timestamp_error)
        max_position_error = max(max_position_error, position_error)
        max_quaternion_error = max(max_quaternion_error, quaternion_error)
        matched_indices.append(trajectory_index)
        previous_trajectory_index = trajectory_index
    return {
        "row_count": len(recorded),
        "poseimu_row_count": len(recorded),
        "capture_trajectory_row_count": len(trajectory),
        "association_policy": "each_poseimu_to_unique_monotonic_nearest_capture_trajectory_timestamp",
        "exact_row_count_parity": len(recorded) == len(trajectory),
        "first_matched_capture_trajectory_index_zero_based": matched_indices[0],
        "last_matched_capture_trajectory_index_zero_based": matched_indices[-1],
        "unmatched_capture_rows_before_first_match": matched_indices[0],
        "unmatched_capture_rows_after_last_match": len(trajectory) - matched_indices[-1] - 1,
        "unmatched_capture_rows_between_matches": (
            matched_indices[-1] - matched_indices[0] + 1 - len(matched_indices)
        ),
        "max_timestamp_error_s": max_timestamp_error,
        "max_position_component_error_m": max_position_error,
        "max_quaternion_component_error": max_quaternion_error,
        "timestamp_tolerance_s": timestamp_tolerance_s,
        "pose_component_tolerance": pose_tolerance,
    }


def fit_evo_no_scale_alignment(
    ground_truth: Sequence[Pose],
    estimate: Sequence[Pose],
    max_diff_s: float = 0.01,
) -> EvoAlignment:
    """Fit exactly evo's ``--align`` SE(3) after evo timestamp association.

    The screen evaluates ``evo_ape tum GT EST --t_max_diff 0.01 --align``.
    Reusing evo's public synchronization and ``PoseTrajectory3D.align`` APIs
    here avoids a second, subtly different association or Umeyama fit.
    """

    if not math.isfinite(max_diff_s) or max_diff_s < 0.0:
        raise GeometryError("alignment max timestamp difference is invalid")
    try:
        import evo  # type: ignore
        import numpy as np  # type: ignore
        from evo.core import sync  # type: ignore
        from evo.core.trajectory import PoseTrajectory3D  # type: ignore
    except Exception as exc:
        raise GeometryError(
            "evo and NumPy are required for the frozen no-scale render alignment"
        ) from exc

    def evo_trajectory(poses: Sequence[Pose]) -> Any:
        return PoseTrajectory3D(
            positions_xyz=np.asarray(
                [pose.position for pose in poses], dtype=np.float64
            ),
            orientations_quat_wxyz=np.asarray(
                [
                    (
                        pose.quaternion_xyzw[3],
                        pose.quaternion_xyzw[0],
                        pose.quaternion_xyzw[1],
                        pose.quaternion_xyzw[2],
                    )
                    for pose in poses
                ],
                dtype=np.float64,
            ),
            timestamps=np.asarray(
                [pose.timestamp for pose in poses], dtype=np.float64
            ),
        )

    reference = evo_trajectory(ground_truth)
    estimated = evo_trajectory(estimate)
    try:
        associated_reference, associated_estimate = sync.associate_trajectories(
            reference,
            estimated,
            max_diff=max_diff_s,
            offset_2=0.0,
            first_name="ground truth",
            snd_name="capture estimate",
        )
        rotation, translation, scale = associated_estimate.align(
            associated_reference,
            correct_scale=False,
            correct_only_scale=False,
            n=-1,
        )
    except Exception as exc:
        raise GeometryError(f"evo no-scale SE(3) alignment failed: {exc}") from exc

    # Reproduce associate_trajectories' exact shorter-to-longer index rule so
    # the fit's source rows are independently recorded. Ties inherit NumPy's
    # argmin behavior through evo.core.sync.matching_time_indices.
    gt_stamps = reference.timestamps
    estimate_stamps = estimated.timestamps
    if len(estimate_stamps) > len(gt_stamps):
        gt_indices, estimate_indices = sync.matching_time_indices(
            gt_stamps, estimate_stamps, max_diff=max_diff_s, offset_2=0.0
        )
    else:
        estimate_indices, gt_indices = sync.matching_time_indices(
            estimate_stamps, gt_stamps, max_diff=max_diff_s, offset_2=0.0
        )
    associations = tuple(
        (int(gt_index), int(estimate_index))
        for gt_index, estimate_index in zip(gt_indices, estimate_indices)
    )
    if len(associations) != int(associated_reference.num_poses):
        raise GeometryError("evo association reconstruction count mismatch")
    for association_index, (gt_index, estimate_index) in enumerate(associations):
        if (
            associated_reference.timestamps[association_index]
            != gt_stamps[gt_index]
            or associated_estimate.timestamps[association_index]
            != estimate_stamps[estimate_index]
        ):
            raise GeometryError("evo association reconstruction value mismatch")

    rotation_values = tuple(
        tuple(float(rotation[row, column]) for column in range(3))
        for row in range(3)
    )
    translation_values = tuple(float(value) for value in translation)
    _finite(
        (
            *(value for row in rotation_values for value in row),
            *translation_values,
            float(scale),
        ),
        "evo alignment transform",
    )
    if float(scale) != 1.0:
        raise GeometryError(
            f"evo returned scale {scale!r} despite no-scale alignment"
        )
    determinant = float(np.linalg.det(rotation))
    orthogonality_error = float(
        np.linalg.norm(rotation.T.dot(rotation) - np.eye(3), ord="fro")
    )
    if (
        not math.isfinite(determinant)
        or abs(determinant - 1.0) > 1e-9
        or not math.isfinite(orthogonality_error)
        or orthogonality_error > 1e-9
    ):
        raise GeometryError("evo alignment returned an invalid SE(3) rotation")
    return EvoAlignment(
        rotation=rotation_values,
        translation=translation_values,
        scale=float(scale),
        associations=associations,
        max_diff_s=max_diff_s,
        evo_version=str(getattr(evo, "__version__", "UNKNOWN")),
        evo_sync_path=str(Path(sync.__file__).resolve()),
        evo_trajectory_path=str(
            Path(sys.modules[PoseTrajectory3D.__module__].__file__).resolve()
        ),
        rotation_determinant=determinant,
        rotation_orthogonality_error_frobenius=orthogonality_error,
    )


def apply_alignment_point(
    point: tuple[float, float, float], alignment: EvoAlignment
) -> tuple[float, float, float]:
    """Apply the frozen estimate-to-GT transform, with scale fixed to one."""

    transformed = tuple(
        sum(alignment.rotation[row][column] * point[column] for column in range(3))
        + alignment.translation[row]
        for row in range(3)
    )
    return _finite(transformed, "aligned render point")


def apply_alignment_pose_position(pose: Pose, alignment: EvoAlignment) -> Pose:
    """Transform a pose's rendered position; SVG rendering does not use attitude."""

    return Pose(
        timestamp=pose.timestamp,
        position=apply_alignment_point(pose.position, alignment),
        quaternion_xyzw=pose.quaternion_xyzw,
    )


def _alignment_id(alignment: EvoAlignment) -> str:
    payload = {
        "algorithm": "evo_PoseTrajectory3D.align_Umeyama_SE3_no_scale",
        "max_diff_s": alignment.max_diff_s,
        "rotation": alignment.rotation,
        "scale": alignment.scale,
        "translation": alignment.translation,
    }
    return hashlib.sha256(
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise GeometryError(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _float_text(value: float) -> str:
    if value == 0.0:
        return "0"
    return format(value, ".12g")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())


def _alignment_association_csv(
    alignment: EvoAlignment,
    ground_truth: Sequence[Pose],
    estimate: Sequence[Pose],
) -> str:
    lines = [
        "association_ordinal,ground_truth_index,capture_estimate_index,ground_truth_timestamp_s,capture_estimate_timestamp_s,estimate_minus_ground_truth_s"
    ]
    for ordinal, (gt_index, estimate_index) in enumerate(alignment.associations):
        gt_timestamp = ground_truth[gt_index].timestamp
        estimate_timestamp = estimate[estimate_index].timestamp
        lines.append(
            ",".join(
                (
                    str(ordinal),
                    str(gt_index),
                    str(estimate_index),
                    _float_text(gt_timestamp),
                    _float_text(estimate_timestamp),
                    _float_text(estimate_timestamp - gt_timestamp),
                )
            )
        )
    return "\n".join(lines) + "\n"


def _ply_text(
    groups: Sequence[tuple[str, Sequence[tuple[float, float, float]]]],
    comments: Sequence[str],
) -> tuple[str, int, dict[str, int]]:
    counts = {name: len(points) for name, points in groups}
    total = sum(counts.values())
    lines = [
        "ply",
        "format ascii 1.0",
        f"comment schema {SCHEMA}",
    ]
    lines.extend(f"comment {comment}" for comment in comments)
    lines.extend(
        (
            f"element vertex {total}",
            "property double x",
            "property double y",
            "property double z",
            "property uchar red",
            "property uchar green",
            "property uchar blue",
            "property uchar semantic_id",
            "end_header",
        )
    )
    for name, points in groups:
        color = SEMANTIC_COLORS[name]
        semantic_id = SEMANTIC_IDS[name]
        for point in points:
            lines.append(
                " ".join(
                    (
                        _float_text(point[0]),
                        _float_text(point[1]),
                        _float_text(point[2]),
                        str(color[0]),
                        str(color[1]),
                        str(color[2]),
                        str(semantic_id),
                    )
                )
            )
    return "\n".join(lines) + "\n", total, counts


def _quaternion_angle(left: Sequence[float], right: Sequence[float]) -> float:
    dot = abs(sum(a * b for a, b in zip(left, right)))
    dot = min(1.0, max(-1.0, dot / (_quaternion_norm(left) * _quaternion_norm(right))))
    return 2.0 * math.acos(dot)


def select_snapshots(
    gt: Sequence[Pose],
    sensor_poses: Sequence[Pose],
    timestamp_tolerance_s: float = 5.1e-6,
) -> tuple[dict[str, int], dict[str, Any]]:
    overlap = tuple(
        pose
        for pose in gt
        if sensor_poses[0].timestamp - timestamp_tolerance_s
        <= pose.timestamp
        <= sensor_poses[-1].timestamp + timestamp_tolerance_s
    )
    if len(overlap) < 2:
        raise GeometryError("ground truth has fewer than two poses in capture coverage")
    start = overlap[0].timestamp
    end = overlap[-1].timestamp
    if end <= start:
        raise GeometryError("ground-truth overlap has zero duration")

    targets = {
        str(percent): start + (end - start) * (percent / 100.0)
        for percent in (25, 50, 75)
    }
    max_rate = -math.inf
    max_rate_timestamp = overlap[1].timestamp
    max_rate_pair = (0, 1)
    for index in range(1, len(overlap)):
        dt = overlap[index].timestamp - overlap[index - 1].timestamp
        if dt <= 0.0:
            raise GeometryError("ground-truth timestamps are not strictly increasing")
        rate = _quaternion_angle(
            overlap[index - 1].quaternion_xyzw,
            overlap[index].quaternion_xyzw,
        ) / dt
        if not math.isfinite(rate):
            raise GeometryError("non-finite ground-truth angular rate")
        if rate > max_rate:
            max_rate = rate
            max_rate_timestamp = overlap[index].timestamp
            max_rate_pair = (index - 1, index)
    targets["max_angular_rate"] = max_rate_timestamp

    selected: dict[str, int] = {}
    selection_details: dict[str, Any] = {}
    for label, target in targets.items():
        index = min(
            range(len(sensor_poses)),
            key=lambda candidate: (
                abs(sensor_poses[candidate].timestamp - target),
                candidate,
            ),
        )
        selected[label] = index
        selection_details[label] = {
            "gt_target_timestamp_s": target,
            "poseimu_ordinal_zero_based": index,
            "poseimu_timestamp_s": sensor_poses[index].timestamp,
            "absolute_association_error_s": abs(sensor_poses[index].timestamp - target),
        }
    return selected, {
        "gt_overlap_start_s": start,
        "gt_overlap_end_s": end,
        "gt_overlap_row_count": len(overlap),
        "max_angular_rate_rad_s": max_rate,
        "max_angular_rate_overlap_pair_zero_based": list(max_rate_pair),
        "selections": selection_details,
    }


Projection = Callable[[Tuple[float, float, float]], Tuple[float, float]]


def _top(point: tuple[float, float, float]) -> tuple[float, float]:
    return point[0], point[1]


def _side(point: tuple[float, float, float]) -> tuple[float, float]:
    return point[0], point[2]


def _oblique(point: tuple[float, float, float]) -> tuple[float, float]:
    return (
        (point[0] - point[1]) / math.sqrt(2.0),
        (point[0] + point[1] - 2.0 * point[2]) / math.sqrt(6.0),
    )


VIEW_PROJECTIONS: dict[str, Projection] = {
    "top": _top,
    "side": _side,
    "oblique": _oblique,
}


def _view_bounds(
    gt: Sequence[Pose], projection: Projection
) -> tuple[float, float, float, float]:
    projected = [projection(pose.position) for pose in gt]
    min_x = min(point[0] for point in projected)
    max_x = max(point[0] for point in projected)
    min_y = min(point[1] for point in projected)
    max_y = max(point[1] for point in projected)
    x_margin = 0.05 * max(max_x - min_x, 1.0)
    y_margin = 0.05 * max(max_y - min_y, 1.0)
    return min_x - x_margin, max_x + x_margin, min_y - y_margin, max_y + y_margin


def _inside(point: tuple[float, float], bounds: tuple[float, float, float, float]) -> bool:
    return bounds[0] <= point[0] <= bounds[1] and bounds[2] <= point[1] <= bounds[3]


def _trajectory_segments(
    poses: Sequence[Pose],
    projection: Projection,
    bounds: tuple[float, float, float, float],
    gap_threshold_s: float,
) -> tuple[list[list[tuple[float, float]]], int, int]:
    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    clipped = 0
    gaps = 0
    previous_timestamp: float | None = None
    for pose in poses:
        if previous_timestamp is not None and pose.timestamp - previous_timestamp > gap_threshold_s:
            if current:
                segments.append(current)
            current = []
            gaps += 1
        projected = projection(pose.position)
        if not _inside(projected, bounds):
            if current:
                segments.append(current)
            current = []
            clipped += 1
        else:
            current.append(projected)
        previous_timestamp = pose.timestamp
    if current:
        segments.append(current)
    return segments, clipped, gaps


def _svg_number(value: float) -> str:
    return format(value, ".3f").rstrip("0").rstrip(".") or "0"


def _render_svg(
    view_name: str,
    gt: Sequence[Pose],
    estimate: Sequence[Pose],
    slam_points: Sequence[tuple[float, float, float]],
    msckf_points: Sequence[tuple[float, float, float]],
    gap_threshold_s: float,
    alignment_id: str,
    display_dataset_label: str,
) -> tuple[str, dict[str, Any]]:
    projection = VIEW_PROJECTIONS[view_name]
    bounds = _view_bounds(gt, projection)
    min_x, max_x, min_y, max_y = bounds
    width, height = 960.0, 720.0
    left, right, top_margin, bottom = 70.0, 30.0, 70.0, 55.0
    plot_width = width - left - right
    plot_height = height - top_margin - bottom

    def screen(point: tuple[float, float]) -> tuple[float, float]:
        x = left + (point[0] - min_x) * plot_width / (max_x - min_x)
        y = top_margin + (max_y - point[1]) * plot_height / (max_y - min_y)
        return x, y

    gt_segments, gt_clipped, gt_gaps = _trajectory_segments(
        gt, projection, bounds, gap_threshold_s
    )
    estimate_segments, estimate_clipped, estimate_gaps = _trajectory_segments(
        estimate, projection, bounds, gap_threshold_s
    )
    geometry_layers = {
        "active_slam_state_not_persistent_map": slam_points,
        "transient_msckf_update_points_not_map": msckf_points,
    }
    geometry_colors = {
        "active_slam_state_not_persistent_map": "#2366b8",
        "transient_msckf_update_points_not_map": "#e67e22",
    }
    projected_geometry: dict[str, list[tuple[float, float]]] = {}
    geometry_clipped: dict[str, int] = {}
    for label, points in geometry_layers.items():
        projected = [projection(point) for point in points]
        projected_geometry[label] = [point for point in projected if _inside(point, bounds)]
        geometry_clipped[label] = len(projected) - len(projected_geometry[label])

    title = f"{display_dataset_label} geometry — {view_name} view"
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{int(width)}" height="{int(height)}" viewBox="0 0 {int(width)} {int(height)}" data-alignment-id="{alignment_id}">',
        f"  <title>{html.escape(title)}</title>",
        "  <desc>Fixed GT-derived bounds. Estimate and every rendered feature point use one frozen evo-compatible no-scale SE(3) alignment. Blue points are the literal final active sparse SLAM state, possibly empty and not a persistent map. Orange points are aggregated transient MSCKF update points, not a map. Trajectory gaps are not connected.</desc>",
        "  <rect width=" + f'"{int(width)}" height="{int(height)}" fill="#ffffff"/>',
        f'  <rect x="{_svg_number(left)}" y="{_svg_number(top_margin)}" width="{_svg_number(plot_width)}" height="{_svg_number(plot_height)}" fill="#fafafa" stroke="#777"/>',
        f'  <text x="{_svg_number(left)}" y="34" font-family="sans-serif" font-size="20">{html.escape(title)}</text>',
        f'  <text x="{_svg_number(left)}" y="55" font-family="sans-serif" font-size="12">GT-derived bounds; one frozen evo SE(3), scale=1; gaps preserved; no smoothing/interpolation</text>',
    ]
    if not slam_points:
        lines.append(
            f'  <text x="{_svg_number(left + 8)}" y="{_svg_number(top_margin + 20)}" font-family="sans-serif" font-size="13" font-weight="bold" fill="#b00020">FINAL ACTIVE SLAM EMPTY — FEATURE COLLAPSE (0 points)</text>'
        )
    for label in (
        "transient_msckf_update_points_not_map",
        "active_slam_state_not_persistent_map",
    ):
        commands = []
        for point in projected_geometry[label]:
            x, y = screen(point)
            commands.append(f"M{_svg_number(x)} {_svg_number(y)}h0.01")
        if commands:
            lines.append(
                f'  <path d="{" ".join(commands)}" fill="none" stroke="{geometry_colors[label]}" stroke-width="1.4" stroke-linecap="round" data-semantics="{label}" data-point-count="{len(commands)}"/>'
            )

    def add_segments(
        segments: Sequence[Sequence[tuple[float, float]]],
        css_class: str,
        color: str,
        width_px: str,
    ) -> None:
        for index, segment in enumerate(segments):
            coordinates = " ".join(
                f"{_svg_number(screen(point)[0])},{_svg_number(screen(point)[1])}"
                for point in segment
            )
            lines.append(
                f'  <polyline class="{css_class}" data-segment="{index}" points="{coordinates}" fill="none" stroke="{color}" stroke-width="{width_px}"/>'
            )

    add_segments(gt_segments, "ground-truth", "#111111", "2.2")
    add_segments(estimate_segments, "estimate", "#d62728", "1.7")
    legend_y = height - 30.0
    lines.extend(
        (
            f'  <text x="{_svg_number(left)}" y="{_svg_number(legend_y)}" font-family="sans-serif" font-size="11">GT black · aligned estimate red · final active SLAM blue (may be empty; not persistent) · transient MSCKF orange (not a map)</text>',
            f'  <text x="{_svg_number(width - 330)}" y="{_svg_number(legend_y)}" font-family="monospace" font-size="10">clipped: est={estimate_clipped}, slam={geometry_clipped["active_slam_state_not_persistent_map"]}, msckf={geometry_clipped["transient_msckf_update_points_not_map"]}</text>',
            "</svg>",
        )
    )
    metadata = {
        "projection": view_name,
        "render_alignment_id": alignment_id,
        "render_frame": "ground_truth_frame_after_one_frozen_SE3_no_scale_alignment",
        "aligned_layers": [
            "capture_estimate_trajectory",
            "final_points_slam",
            "aggregate_points_msckf",
        ],
        "gt_derived_bounds": {
            "horizontal_min": min_x,
            "horizontal_max": max_x,
            "vertical_min": min_y,
            "vertical_max": max_y,
            "margin_fraction_with_one_meter_minimum_span": 0.05,
        },
        "gap_threshold_s": gap_threshold_s,
        "trajectory_segments": {
            "ground_truth": len(gt_segments),
            "estimate": len(estimate_segments),
        },
        "detected_time_gaps": {
            "ground_truth": gt_gaps,
            "estimate": estimate_gaps,
        },
        "clipped_counts": {
            "ground_truth_poses": gt_clipped,
            "estimate_poses": estimate_clipped,
            **geometry_clipped,
        },
        "rendered_geometry_counts": {
            label: len(points) for label, points in projected_geometry.items()
        },
        "final_slam_empty": len(slam_points) == 0,
        "qualitative_flags": (
            ["FINAL_ACTIVE_SLAM_EMPTY_FEATURE_COLLAPSE"]
            if not slam_points
            else []
        ),
    }
    return "\n".join(lines) + "\n", metadata


def _median_gt_dt(gt: Sequence[Pose]) -> float:
    differences = [
        current.timestamp - previous.timestamp
        for previous, current in zip(gt, gt[1:])
    ]
    if not differences or any(value <= 0.0 for value in differences):
        raise GeometryError("ground truth needs strictly increasing timestamps")
    value = float(statistics.median(differences))
    if not math.isfinite(value) or value <= 0.0:
        raise GeometryError("ground-truth median sample interval is invalid")
    return value


def _relative_if_within(path: Path, root: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _validated_output_destination(run_dir: Path, relative: str) -> Path:
    """Resolve one fixed output without following a nested output symlink."""

    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise GeometryError(f"unsafe geometry output path: {relative}")
    destination = run_dir / relative_path
    current = run_dir
    for component in relative_path.parts[:-1]:
        current = current / component
        if current.is_symlink():
            raise GeometryError(
                f"refusing output through symlinked directory: {current}"
            )
        if current.exists() and not current.is_dir():
            raise GeometryError(f"geometry output parent is not a directory: {current}")
    try:
        destination.parent.resolve(strict=False).relative_to(run_dir)
    except ValueError as exc:
        raise GeometryError(f"geometry output escapes run directory: {destination}") from exc
    if destination.exists() or destination.is_symlink():
        raise GeometryError(f"refusing to overwrite geometry output: {destination}")
    return destination


def build_bundle(
    feature_bag: Path,
    capture_trajectory_path: Path,
    ground_truth_path: Path,
    run_dir: Path,
    namespace: str | None = None,
    expected_frame: str = "global",
    trajectory_format: str = "auto",
    display_dataset_label: str = "KAIST",
) -> dict[str, Any]:
    """Validate inputs and publish a complete deterministic bundle."""

    feature_bag = feature_bag.resolve()
    capture_trajectory_path = capture_trajectory_path.resolve()
    ground_truth_path = ground_truth_path.resolve()
    run_dir = run_dir.resolve()
    if not display_dataset_label or display_dataset_label.strip() != display_dataset_label:
        raise GeometryError("display dataset label must be a nonempty canonical string")
    if not expected_frame or expected_frame.strip() != expected_frame:
        raise GeometryError("expected frame must be a nonempty canonical string")
    for path in (feature_bag, capture_trajectory_path, ground_truth_path):
        if not path.is_file():
            raise GeometryError(f"input is not a regular file: {path}")
    if len({feature_bag, capture_trajectory_path, ground_truth_path}) != 3:
        raise GeometryError("feature bag, capture trajectory, and ground truth must be distinct files")

    recorded = read_feature_bag(feature_bag, namespace, expected_frame)
    capture_trajectory, resolved_trajectory_format = read_trajectory(
        capture_trajectory_path, trajectory_format
    )
    ground_truth, gt_format = read_trajectory(ground_truth_path, "tum")
    trajectory_validation = validate_capture_trajectory(
        recorded.poses, capture_trajectory
    )
    alignment = fit_evo_no_scale_alignment(ground_truth, capture_trajectory)
    render_alignment_id = _alignment_id(alignment)
    selected_snapshots, snapshot_metadata = select_snapshots(
        ground_truth, recorded.poses
    )
    point_associations = recorded.association["points_topic_associations"]
    maximum_point_prefix = max(
        value["unobserved_leading_poseimu_count"]
        for value in point_associations.values()
    )
    snapshots_before_full_point_coverage = {
        label: index
        for label, index in selected_snapshots.items()
        if index < maximum_point_prefix
    }
    if snapshots_before_full_point_coverage:
        raise GeometryError(
            "required qualitative snapshot precedes complete points_* recorder "
            f"coverage at poseimu ordinal {maximum_point_prefix}: "
            f"{snapshots_before_full_point_coverage}"
        )
    snapshot_metadata["points_stream_coverage_gate"] = {
        "maximum_unobserved_leading_poseimu_count": maximum_point_prefix,
        "every_required_snapshot_at_or_after_complete_points_coverage": True,
    }

    final_slam_index = len(recorded.poses) - 1
    final_slam = _point_cloud_at_pose(
        recorded, "points_slam", final_slam_index
    )
    final_slam_empty = len(final_slam) == 0
    aggregate_msckf = tuple(
        point
        for frame_points in recorded.clouds["points_msckf"]
        for point in frame_points
    )
    aggregate_loop = tuple(
        point
        for raw_message_points in recorded.raw_loop_clouds
        for point in raw_message_points
    )
    aligned_capture_trajectory = tuple(
        apply_alignment_pose_position(pose, alignment)
        for pose in capture_trajectory
    )
    aligned_final_slam = tuple(
        apply_alignment_point(point, alignment) for point in final_slam
    )
    aligned_aggregate_msckf = tuple(
        apply_alignment_point(point, alignment) for point in aggregate_msckf
    )

    if run_dir.exists() and not run_dir.is_dir():
        raise GeometryError(f"run directory path is not a directory: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    for relative in OUTPUT_PATHS:
        _validated_output_destination(run_dir, relative)

    input_hashes = {
        "feature_stream_bag": {
            "path": str(feature_bag),
            "sha256": _sha256(feature_bag),
            "bytes": feature_bag.stat().st_size,
        },
        "capture_trajectory": {
            "path": str(capture_trajectory_path),
            "sha256": _sha256(capture_trajectory_path),
            "bytes": capture_trajectory_path.stat().st_size,
            "format": resolved_trajectory_format,
        },
        "ground_truth": {
            "path": str(ground_truth_path),
            "sha256": _sha256(ground_truth_path),
            "bytes": ground_truth_path.stat().st_size,
            "format": gt_format,
        },
    }
    gap_threshold_s = 5.0 * _median_gt_dt(ground_truth)

    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "COMPLETE",
        "inputs": input_hashes,
        "script": {
            "path": "scripts/icra27/kaist_geometry_bundle.py",
            "sha256": _sha256(Path(__file__).resolve()),
        },
        "frame": {
            "expected": expected_frame,
            "ply_frame": "raw estimator global frame; no alignment applied",
            "svg_frame": "ground-truth frame after one frozen estimate-to-GT SE(3) alignment",
            "gt_bounds_are_method_independent": True,
        },
        "render_alignment": {
            "alignment_id": render_alignment_id,
            "algorithm": "evo PoseTrajectory3D.align using Umeyama SE(3)",
            "equivalent_evaluator_options": {
                "align": True,
                "correct_scale": False,
                "max_timestamp_difference_s": alignment.max_diff_s,
                "timestamp_offset_s": 0.0,
            },
            "fit_direction": "capture_estimate_to_ground_truth",
            "fit_once": True,
            "scale": alignment.scale,
            "rotation_matrix": alignment.rotation,
            "translation_m": alignment.translation,
            "homogeneous_matrix": [
                [*alignment.rotation[0], alignment.translation[0]],
                [*alignment.rotation[1], alignment.translation[1]],
                [*alignment.rotation[2], alignment.translation[2]],
                [0.0, 0.0, 0.0, 1.0],
            ],
            "rotation_determinant": alignment.rotation_determinant,
            "rotation_orthogonality_error_frobenius": (
                alignment.rotation_orthogonality_error_frobenius
            ),
            "evo": {
                "version": alignment.evo_version,
                "sync_module": {
                    "path": alignment.evo_sync_path,
                    "sha256": _sha256(Path(alignment.evo_sync_path)),
                },
                "trajectory_module": {
                    "path": alignment.evo_trajectory_path,
                    "sha256": _sha256(Path(alignment.evo_trajectory_path)),
                },
            },
            "timestamp_association": {
                "algorithm": "evo.core.sync.associate_trajectories",
                "association_count": len(alignment.associations),
                "maximum_allowed_absolute_difference_s": alignment.max_diff_s,
                "table": "geometry/alignment_associations.csv",
                "shorter_trajectory_drives_nearest_neighbor_matching": True,
                "ties_use_numpy_argmin_first_index": True,
            },
            "rendered_layers_using_this_exact_transform": [
                "capture_estimate_trajectory",
                "final_points_slam",
                "aggregate_points_msckf",
            ],
        },
        "recording": {
            "namespace": recorded.namespace,
            "topics": recorded.topics,
            "message_types": recorded.message_types,
            "message_counts": recorded.record_counts,
            "association": recorded.association,
            "raw_feature_bag_is_authoritative": True,
        },
        "capture_trajectory_validation": trajectory_validation,
        "semantics": {
            "slam_landmarks_final": (
                "literal final points_slam message, including a zero-vertex final state; "
                "current active sparse state, not a persistent map"
            ),
            "msckf_update_points": (
                "all recorded transient last-update MSCKF point samples over the "
                "exact/proved topic coverage, concatenated in raw message order; "
                "duplicates may occur; not a persistent map"
            ),
            "loop_active_tracks_aggregate": (
                "all validated loop_feats points concatenated in raw bag message/point order; "
                "transient active tracks, deliberately unassociated, not a persistent map"
            ),
            "snapshots": (
                "one estimator update each after exact/proved per-topic ordinal association; "
                "required snapshots before complete point-stream recorder coverage fail closed; "
                "semantic_id 1=active SLAM, 2=transient MSCKF, 3=active ARUCO, "
                "4=active loop features; none is a dense map"
            ),
        },
        "selection": {
            "final_slam_message": {
                "policy": "literal_final_points_slam_message_never_last_nonempty_substitution",
                "poseimu_ordinal_zero_based": final_slam_index,
                "poseimu_timestamp_s": recorded.poses[final_slam_index].timestamp,
                "point_count": len(final_slam),
                "final_empty": final_slam_empty,
                "qualitative_flag": (
                    "FINAL_ACTIVE_SLAM_EMPTY_FEATURE_COLLAPSE"
                    if final_slam_empty
                    else "NONE"
                ),
            },
            "snapshots": snapshot_metadata,
        },
        "outputs": {},
        "views": {},
        "limitations": [
            "The canonical SLAM PLY is the literal final active filter state, may have zero vertices, and is not a persistent accumulated map.",
            "MSCKF points are recorded transient update visualizations over the exact/proved topic coverage, concatenated over time, and may repeat physical features.",
            "Exact-count points_* streams retain v2 publish-ordinal association; a shorter stream is retained at raw count and mapped only after a forced contiguous-suffix proof, with its leading pose interval labeled unobserved rather than empty.",
            "Exact-count points_* streams retain the v2 parity assumption and do not newly exclude a hypothetical balanced leading-extra plus trailing-missing defect; v3's stronger proof gates apply to count-deficit streams only.",
            "A points_* suffix proof requires unique monotonic nearest bag-record times, or unique bounded wall-header matches to an already-associated peer point stream; wall headers remain non-sensor-time proof evidence and no timestamp is interpolated.",
            "Internal point-stream gaps, trailing gaps, excess point messages, ambiguous nearest matches, and proof deltas above the declared bound fail closed.",
            "loop_feats uses camera-time headers offset from poseimu: it is associated by publish ordinal only under exact count parity; header and bag-record offsets are diagnostics, never association gates.",
            "A sparse loop_feats stream is never timestamp-matched: every pose snapshot loop layer is explicitly empty, while all validated raw points remain in the unassociated aggregate PLY and authoritative raw bag.",
            "SVGs use one evo-compatible no-scale SE(3) alignment fitted once from associated estimate/GT positions; the identical transform is applied to every rendered estimate and feature point.",
            "SVGs use GT-only bounds and clip out-of-bounds aligned geometry for display; raw-frame PLY files retain every validated point.",
            "No trajectory smoothing, interpolation, scale correction, or per-method crop is applied.",
        ],
    }

    with tempfile.TemporaryDirectory(prefix=".kaist-geometry-", dir=run_dir) as temporary:
        staging = Path(temporary)

        association_relative = "geometry/alignment_associations.csv"
        association_path = staging / association_relative
        _write_text(
            association_path,
            _alignment_association_csv(
                alignment, ground_truth, capture_trajectory
            ),
        )
        association_errors = [
            abs(
                capture_trajectory[estimate_index].timestamp
                - ground_truth[gt_index].timestamp
            )
            for gt_index, estimate_index in alignment.associations
        ]
        manifest["outputs"][association_relative] = {
            "sha256": _sha256(association_path),
            "bytes": association_path.stat().st_size,
            "association_count": len(alignment.associations),
            "maximum_absolute_timestamp_difference_s": max(association_errors),
        }

        ply_specs: list[
            tuple[str, Sequence[tuple[str, Sequence[tuple[float, float, float]]]], Sequence[str]]
        ] = [
            (
                "geometry/slam_landmarks_final.ply",
                (("points_slam", final_slam),),
                (
                    "coordinate_frame raw estimator global frame; no render alignment applied",
                    "semantics literal final points_slam message; current active sparse state; not a persistent map",
                    f"final_empty {str(final_slam_empty).lower()}",
                    (
                        "qualitative_flag FINAL_ACTIVE_SLAM_EMPTY_FEATURE_COLLAPSE"
                        if final_slam_empty
                        else "qualitative_flag NONE"
                    ),
                    f"poseimu_ordinal_zero_based {final_slam_index}",
                    f"poseimu_timestamp_s {_float_text(recorded.poses[final_slam_index].timestamp)}",
                ),
            ),
            (
                "geometry/msckf_update_points.ply",
                (("points_msckf", aggregate_msckf),),
                (
                    "coordinate_frame raw estimator global frame; no render alignment applied",
                    "semantics all recorded transient last-update MSCKF points over exact/proved topic coverage; duplicates may occur; not a persistent map",
                    "ordering raw feature_stream.bag points_msckf message ordinal then point ordinal",
                ),
            ),
            (
                "geometry/loop_active_tracks_aggregate.ply",
                (("loop_feats", aggregate_loop),),
                (
                    "coordinate_frame raw estimator global frame; no render alignment applied",
                    "semantics aggregate of transient active loop tracks; deliberately unassociated; not a persistent map",
                    "ordering raw feature_stream.bag loop_feats message ordinal then point ordinal",
                    "authoritative_source geometry/feature_stream.bag",
                    f"association_mode {recorded.association['loop_feats_mode']}",
                ),
            ),
        ]
        for label in ("25", "50", "75", "max_angular_rate"):
            index = selected_snapshots[label]
            groups = tuple(
                (
                    suffix,
                    (
                        recorded.clouds[suffix][index]
                        if suffix == "loop_feats"
                        else _point_cloud_at_pose(recorded, suffix, index)
                    ),
                )
                for suffix in (
                    "points_slam",
                    "points_msckf",
                    "points_aruco",
                    "loop_feats",
                )
            )
            ply_specs.append(
                (
                    f"geometry/snapshots/{label}.ply",
                    groups,
                    (
                        "coordinate_frame raw estimator global frame; no render alignment applied",
                        "semantics one estimator update; sparse active/transient geometry, not a dense or persistent map",
                        f"selection {label}",
                        f"poseimu_ordinal_zero_based {index}",
                        f"poseimu_timestamp_s {_float_text(recorded.poses[index].timestamp)}",
                    ),
                )
            )

        for relative, groups, comments in ply_specs:
            text, point_count, semantic_counts = _ply_text(groups, comments)
            staged_path = staging / relative
            _write_text(staged_path, text)
            manifest["outputs"][relative] = {
                "sha256": _sha256(staged_path),
                "bytes": staged_path.stat().st_size,
                "point_count": point_count,
                "semantic_counts": semantic_counts,
            }

        for view_name in ("top", "side", "oblique"):
            relative = f"figures/{view_name}.svg"
            svg, view_metadata = _render_svg(
                view_name,
                ground_truth,
                aligned_capture_trajectory,
                aligned_final_slam,
                aligned_aggregate_msckf,
                gap_threshold_s,
                render_alignment_id,
                display_dataset_label,
            )
            staged_path = staging / relative
            _write_text(staged_path, svg)
            manifest["views"][view_name] = view_metadata
            manifest["outputs"][relative] = {
                "sha256": _sha256(staged_path),
                "bytes": staged_path.stat().st_size,
            }

        manifest_path = staging / "geometry_manifest.json"
        _write_text(
            manifest_path,
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n",
        )

        checksum_members: dict[str, Path] = {
            relative: staging / relative
            for relative in manifest["outputs"]
        }
        checksum_members["geometry_manifest.json"] = manifest_path
        for input_path in (
            feature_bag,
            capture_trajectory_path,
            ground_truth_path,
        ):
            relative = _relative_if_within(input_path, run_dir)
            if relative is not None and relative not in checksum_members:
                checksum_members[relative] = input_path
        checksum_text = "".join(
            f"{_sha256(path)}  {relative}\n"
            for relative, path in sorted(checksum_members.items())
        )
        _write_text(staging / "SHA256SUMS", checksum_text)

        # The manifest and checksum file are published last, so their presence
        # is the completion marker if a host-level interruption occurs.
        publish_order = [
            relative
            for relative in OUTPUT_PATHS
            if relative not in {"geometry_manifest.json", "SHA256SUMS"}
        ] + ["geometry_manifest.json", "SHA256SUMS"]
        for relative in publish_order:
            source = staging / relative
            destination = _validated_output_destination(run_dir, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = _validated_output_destination(run_dir, relative)
            try:
                os.link(source, destination)
            except FileExistsError as exc:
                raise GeometryError(
                    f"refusing to overwrite geometry output: {destination}"
                ) from exc
            except OSError as exc:
                raise GeometryError(
                    f"cannot publish geometry output {destination}: {exc}"
                ) from exc
            source.unlink()

    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a KAIST OpenVINS feature-stream capture and produce deterministic "
            "sparse geometry PLY/SVG evidence."
        )
    )
    parser.add_argument("--feature-bag", required=True, type=Path)
    parser.add_argument("--capture-trajectory", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument(
        "--namespace",
        help="private estimator namespace (inferred only when exactly one */poseimu exists)",
    )
    parser.add_argument("--expected-frame", default="global")
    parser.add_argument(
        "--trajectory-format",
        choices=("auto", "tum", "openvins-state"),
        default="auto",
    )
    parser.add_argument("--display-dataset-label", default="KAIST")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_bundle(
            feature_bag=args.feature_bag,
            capture_trajectory_path=args.capture_trajectory,
            ground_truth_path=args.ground_truth,
            run_dir=args.run_dir,
            namespace=args.namespace,
            expected_frame=args.expected_frame,
            trajectory_format=args.trajectory_format,
            display_dataset_label=args.display_dataset_label,
        )
    except (GeometryError, OSError) as exc:
        print(f"geometry bundle failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "schema": manifest["schema"],
                "pose_count": manifest["recording"]["message_counts"]["poseimu"],
                "run_dir": str(args.run_dir.resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
