#!/usr/bin/env python3
"""Streaming reader and manifest generator for update-envelope capture schema 2.

The wire format is deliberately decoded without NumPy or ROS dependencies so
that captures can be validated before they are admitted to an offline study.
Framing checksums are verified before an envelope payload is interpreted, and
the iterator must reach the authenticated trailer and exact EOF to become
strictly valid.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import struct
import sys
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterator, List, Sequence, Tuple, Union


MAGIC = b"SCVIOUPDATES0002"
SCHEMA_VERSION = 2
SCALAR_TYPE_BINARY64 = 1
BYTE_ORDER_BIG_ENDIAN = 1
ENVELOPE_DOMAIN = "schurvio_update_envelope_v2"
TRAILER_SENTINEL = (1 << 64) - 1

# Allocation guards are intentionally above expected update sizes.  They bound
# allocations driven by corrupt input without imposing an estimator setting.
MAX_HEADER_PAYLOAD_BYTES = 1 << 20
MAX_ENVELOPE_PAYLOAD_BYTES = 512 << 20
MAX_STRING_BYTES = 1 << 20
MAX_COLLECTION_ITEMS = 1 << 20
MAX_MATRIX_DIMENSION = 1 << 20
MAX_MATRIX_ELEMENTS = 64 << 20

_SHA256_BYTES = hashlib.sha256().digest_size
_LOWER_COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# Callback endpoints are accumulated binary64 seconds.  Adjacent subtraction
# may lose a few ulps, so compare against the independently encoded durations
# with a small scale-aware tolerance rather than requiring identical bits.
_CALLBACK_TIMELINE_TOLERANCE = 16.0 * sys.float_info.epsilon


class CaptureValidationError(ValueError):
    """A capture is truncated, corrupted, or violates schema 2."""


class Stage(IntEnum):
    CALLBACK = 0
    PREFILTER = 1
    GEOMETRY = 2
    RAW_FACTOR = 3
    REDUCTION = 4
    GATE = 5
    ACCUMULATION = 6
    COMPRESSION = 7
    PREVIEW = 8
    COMMIT = 9
    CALLBACK_FINALIZATION = 10
    CAPTURE_OUTPUT = 11


class TerminalStatus(IntEnum):
    ACTIVE = 0
    INSUFFICIENT_CLONES = 1
    EMPTY_INPUT = 2
    ALL_REJECTED = 3
    EMPTY_AFTER_COMPRESSION = 4
    PREFLIGHT_REJECTED = 5
    COMMITTED = 6
    INTERNAL_FAILURE = 7
    PROPAGATION_FAILURE = 8


class TrackLifecycle(IntEnum):
    CANDIDATE = 0
    PREFILTER_REJECTED = 1
    TRIANGULATION_REJECTED = 2
    REFINEMENT_REJECTED = 3
    RAW_AVAILABLE = 4
    REDUCTION_REJECTED = 5
    GATE_REJECTED = 6
    GATE_ACCEPTED = 7
    ACCUMULATED = 8


@dataclass(frozen=True)
class Matrix:
    rows: int
    cols: int
    values: Tuple[float, ...]

    def at(self, row: int, col: int) -> float:
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError((row, col))
        return self.values[row * self.cols + col]


@dataclass(frozen=True)
class CaptureHeader:
    schema_version: int
    scalar_type: int
    byte_order: int
    source_commit: str
    config_sha256: str
    run_id: str
    sequence_id: str
    record_domain: str


@dataclass(frozen=True)
class CallbackCosts:
    tracking_seconds: float
    propagation_seconds: float
    msckf_seconds: float
    slam_update_seconds: float
    slam_delay_seconds: float
    finalization_seconds: float
    total_seconds: float


@dataclass(frozen=True)
class CallbackTimeline:
    available: bool
    callback_begin: float
    tracking_end: float
    propagation_end: float
    msckf_end: float
    slam_update_end: float
    slam_delay_end: float
    finalization_end: float


@dataclass(frozen=True)
class CaptureOutputMetadata:
    available: bool
    record_construction_duration_ns: int
    serialization_duration_ns: int
    encoded_payload_bytes: int


@dataclass(frozen=True)
class CameraDescriptor:
    camera_id: int
    model: int
    width: int
    height: int
    extrinsic_id: int
    intrinsic_id: int
    extrinsic_value: Matrix
    extrinsic_fej: Matrix
    intrinsic_value: Matrix
    intrinsic_fej: Matrix
    cache_value: Matrix


@dataclass(frozen=True)
class StateBlock:
    ordinal: int
    block_type: int
    key_type: int
    key_u64: int
    key_double: float
    variable_id: int
    covariance_id: int
    offset: int
    dimension: int
    role: int
    current_value: Matrix
    fej_value: Matrix


@dataclass(frozen=True)
class Observation:
    camera_id: int
    timestamp: float
    raw_u: float
    raw_v: float
    normalized_available: bool
    normalized_u: float
    normalized_v: float


@dataclass(frozen=True)
class CandidateRecord:
    ordinal: int
    feature_id: int
    lifecycle_reason: int
    lifecycle_status: int
    raw_observation_count: int
    cleaned_observation_count: int
    prefilter_recorded: bool
    prefilter_accepted: bool
    prefilter_duration_ns: int
    time_range_available: bool
    first_timestamp: float
    last_timestamp: float
    track_age: float
    observations: Tuple[Observation, ...]
    parallax_2d_available: bool
    maximum_normalized_parallax: float
    maximum_image_motion_available: bool
    maximum_pixel_displacement: float
    last_frame_displacement_available: bool
    last_frame_pixel_displacement: float
    final_location_available: bool
    final_normalized_u: float
    final_normalized_v: float
    motion_kind: int
    motion_available: bool
    motion_translation: float
    motion_rotation: float


@dataclass(frozen=True)
class LayoutBlock:
    local_column: int
    covariance_column: int
    size: int


@dataclass(frozen=True)
class TrackRecord:
    update_id: int
    candidate_ordinal: int
    feature_id: int
    geometry_recorded: bool
    triangulation_attempted: bool
    triangulation_succeeded: bool
    refinement_attempted: bool
    refinement_succeeded: bool
    geometry_valid: bool
    p_f_in_g: Matrix
    depth_available: bool
    depth: float
    parallax_3d_available: bool
    parallax_3d: float
    geometry_duration_ns: int
    raw_available: bool
    h_x: Matrix
    h_f: Matrix
    residual: Matrix
    sigma_px: float
    noise_variance: float
    whitening_kind: int
    layout: Tuple[LayoutBlock, ...]
    raw_factor_duration_ns: int
    reduction_recorded: bool
    reducer: int
    reducer_status: int
    reducer_stage: int
    singular_values_available: bool
    singular_values: Matrix
    numerical_rank: int
    singular_ratio_available: bool
    singular_ratio: float
    reduced_a: Matrix
    reduced_b: Matrix
    reduction_duration_ns: int
    gate_recorded: bool
    gate_stage: int
    gate_dof: int
    nis_available: bool
    nis: float
    gate_threshold_available: bool
    gate_threshold: float
    evidence_decision_available: bool
    evidence_accept: bool
    lifecycle_accept: bool
    gate_duration_ns: int
    accepted_for_global_system: bool
    global_row_start: int
    global_row_count: int
    accumulation_duration_ns: int


@dataclass(frozen=True)
class GlobalSystem:
    available: bool
    h: Matrix
    residual: Matrix
    covariance: Matrix
    layout: Tuple[LayoutBlock, ...]
    duration_ns: int


@dataclass(frozen=True)
class GlobalGate:
    applied: bool
    nis_available: bool
    nis: float
    accepted: bool


@dataclass(frozen=True)
class UpdateEnvelope:
    domain: str
    update_id: int
    camera_timestamp: float
    terminal_status: TerminalStatus
    terminal_reason: int
    max_visual_passes: int
    landmark_elimination: int
    fej_enabled: bool
    calibrate_camera_pose: bool
    calibrate_camera_intrinsics: bool
    calibrate_camera_timeoffset: bool
    calibrate_imu_intrinsics: bool
    calibrate_imu_g_sensitivity: bool
    feature_representation: int
    callback_costs: CallbackCosts
    callback_timeline: CallbackTimeline
    capture_output: CaptureOutputMetadata
    cameras: Tuple[CameraDescriptor, ...]
    state_blocks: Tuple[StateBlock, ...]
    p_minus: Matrix
    candidates: Tuple[CandidateRecord, ...]
    tracks: Tuple[TrackRecord, ...]
    accepted_feature_ids: Tuple[int, ...]
    selected_system: GlobalSystem
    compressed_system: GlobalSystem
    posterior_recorded: bool
    preview_status: int
    preview_stage: int
    global_gate: GlobalGate
    production_dx: Matrix
    preview_duration_ns: int
    p_plus: Matrix
    mean_commit_count: int
    covariance_commit_count: int
    feature_finalization_count: int
    commit_duration_ns: int


@dataclass(frozen=True)
class CaptureValidationSummary:
    header: CaptureHeader
    trailer_sha256: str
    update_count: int
    candidate_count: int
    accepted_count: int
    zero_candidate_count: int
    no_update_count: int
    first_timestamp: float | None
    last_timestamp: float | None


@dataclass(frozen=True)
class EnvelopeCapture:
    header: CaptureHeader
    envelopes: Tuple[UpdateEnvelope, ...]
    trailer_sha256: str


class _PayloadReader:
    """Bounds-checked decoder for one checksummed payload."""

    def __init__(self, payload: bytes, context: str) -> None:
        self._view = memoryview(payload)
        self._offset = 0
        self._context = context

    @property
    def remaining(self) -> int:
        return len(self._view) - self._offset

    def _take(self, size: int, what: str) -> memoryview:
        if size < 0 or size > self.remaining:
            raise CaptureValidationError(
                f"{self._context}: truncated {what} at byte {self._offset}"
            )
        begin = self._offset
        self._offset += size
        return self._view[begin : begin + size]

    def u64(self, what: str) -> int:
        return struct.unpack(">Q", self._take(8, what))[0]

    def i64(self, what: str) -> int:
        return struct.unpack(">q", self._take(8, what))[0]

    def f64(self, what: str) -> float:
        value = struct.unpack(">d", self._take(8, what))[0]
        if not math.isfinite(value):
            raise CaptureValidationError(f"{self._context}: {what} is not finite")
        return value

    def boolean(self, what: str) -> bool:
        value = self.u64(what)
        if value not in (0, 1):
            raise CaptureValidationError(
                f"{self._context}: {what} must be canonical 0 or 1, got {value}"
            )
        return bool(value)

    def utf8(self, what: str) -> str:
        size = self.u64(f"{what} length")
        if size > MAX_STRING_BYTES:
            raise CaptureValidationError(
                f"{self._context}: {what} length {size} exceeds {MAX_STRING_BYTES}"
            )
        raw = bytes(self._take(size, what))
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CaptureValidationError(
                f"{self._context}: {what} is not valid UTF-8"
            ) from exc

    def count(self, what: str, maximum: int = MAX_COLLECTION_ITEMS) -> int:
        value = self.u64(what)
        if value > maximum:
            raise CaptureValidationError(
                f"{self._context}: {what} {value} exceeds {maximum}"
            )
        return value

    def matrix(self, what: str) -> Matrix:
        rows = self.u64(f"{what} rows")
        cols = self.u64(f"{what} columns")
        if rows > MAX_MATRIX_DIMENSION or cols > MAX_MATRIX_DIMENSION:
            raise CaptureValidationError(
                f"{self._context}: {what} dimensions {rows}x{cols} exceed the limit"
            )
        count = rows * cols
        if count > MAX_MATRIX_ELEMENTS:
            raise CaptureValidationError(
                f"{self._context}: {what} has {count} elements; limit is "
                f"{MAX_MATRIX_ELEMENTS}"
            )
        raw = self._take(count * 8, f"{what} values")
        values = struct.unpack(f">{count}d", raw) if count else ()
        if any(not math.isfinite(value) for value in values):
            raise CaptureValidationError(
                f"{self._context}: {what} contains a non-finite value"
            )
        return Matrix(rows=rows, cols=cols, values=tuple(values))

    def stage(self, expected: Stage, what: str) -> None:
        actual = self.u64(f"{what} stage")
        if actual != int(expected):
            raise CaptureValidationError(
                f"{self._context}: {what} stage is {actual}, expected "
                f"{int(expected)} ({expected.name.lower()})"
            )

    def optional_f64(self, what: str) -> Tuple[bool, float]:
        available = self.boolean(f"{what} available")
        value = self.f64(what)
        if not available and struct.pack(">d", value) != b"\0" * 8:
            raise CaptureValidationError(
                f"{self._context}: unavailable {what} must use canonical +0.0"
            )
        return available, value

    def finish(self) -> None:
        if self.remaining:
            raise CaptureValidationError(
                f"{self._context}: {self.remaining} trailing payload bytes"
            )


def _read_exact(stream: BinaryIO, size: int, what: str) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            received = size - remaining
            raise CaptureValidationError(
                f"truncated {what}: expected {size} bytes, received {received}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _parse_header(payload: bytes) -> CaptureHeader:
    reader = _PayloadReader(payload, "header")
    header = CaptureHeader(
        schema_version=reader.u64("schema version"),
        scalar_type=reader.u64("scalar type"),
        byte_order=reader.u64("byte order"),
        source_commit=reader.utf8("source commit"),
        config_sha256=reader.utf8("config SHA-256"),
        run_id=reader.utf8("run ID"),
        sequence_id=reader.utf8("sequence ID"),
        record_domain=reader.utf8("record domain"),
    )
    reader.finish()
    if header.schema_version != SCHEMA_VERSION:
        raise CaptureValidationError(
            f"header: schema version must be {SCHEMA_VERSION}, got "
            f"{header.schema_version}"
        )
    if header.scalar_type != SCALAR_TYPE_BINARY64:
        raise CaptureValidationError(
            "header: scalar type must be 1 (IEEE-754 binary64)"
        )
    if header.byte_order != BYTE_ORDER_BIG_ENDIAN:
        raise CaptureValidationError("header: byte order must be 1 (big endian)")
    if not _LOWER_COMMIT_RE.fullmatch(header.source_commit):
        raise CaptureValidationError(
            "header: source commit must be exactly lowercase 40-hex"
        )
    if not _LOWER_SHA256_RE.fullmatch(header.config_sha256):
        raise CaptureValidationError(
            "header: config SHA-256 must be lowercase 64-hex"
        )
    if not header.run_id or not header.sequence_id:
        raise CaptureValidationError("header: run and sequence IDs must be nonempty")
    if header.record_domain != ENVELOPE_DOMAIN:
        raise CaptureValidationError(
            f"header: record domain must be {ENVELOPE_DOMAIN!r}"
        )
    return header


def _parse_layout(reader: _PayloadReader, what: str) -> Tuple[LayoutBlock, ...]:
    count = reader.count(f"{what} count")
    return tuple(
        LayoutBlock(
            local_column=reader.u64(f"{what}[{index}] local column"),
            covariance_column=reader.u64(f"{what}[{index}] covariance column"),
            size=reader.u64(f"{what}[{index}] size"),
        )
        for index in range(count)
    )


def _parse_system(reader: _PayloadReader, what: str) -> GlobalSystem:
    return GlobalSystem(
        available=reader.boolean(f"{what} available"),
        duration_ns=reader.u64(f"{what} duration ns"),
        layout=_parse_layout(reader, f"{what} layout"),
        h=reader.matrix(f"{what} H"),
        residual=reader.matrix(f"{what} residual"),
        covariance=reader.matrix(f"{what} R"),
    )


def _parse_track(
    reader: _PayloadReader, index: int, update_id: int
) -> Tuple[CandidateRecord, TrackRecord]:
    prefix = f"track[{index}]"
    reader.stage(Stage.PREFILTER, f"{prefix} prefilter")
    candidate_ordinal = reader.u64(f"{prefix} ordinal")
    feature_id = reader.u64(f"{prefix} feature ID")
    lifecycle_reason = reader.u64(f"{prefix} candidate reason")
    lifecycle_status = reader.u64(f"{prefix} lifecycle status")
    raw_count = reader.u64(f"{prefix} raw observation count")
    cleaned_count = reader.u64(f"{prefix} cleaned observation count")
    prefilter_recorded = reader.boolean(f"{prefix} prefilter recorded")
    prefilter_accepted = reader.boolean(f"{prefix} prefilter accepted")
    prefilter_duration_ns = reader.u64(f"{prefix} prefilter duration ns")
    time_range_available = reader.boolean(f"{prefix} time range available")
    first_timestamp = reader.f64(f"{prefix} first timestamp")
    last_timestamp = reader.f64(f"{prefix} last timestamp")
    track_age = reader.f64(f"{prefix} track age seconds")
    observation_count = reader.count(f"{prefix} observation count")
    observations = tuple(
        Observation(
            camera_id=reader.u64(f"{prefix} observation[{obs}] camera ID"),
            timestamp=reader.f64(f"{prefix} observation[{obs}] timestamp"),
            raw_u=reader.f64(f"{prefix} observation[{obs}] pixel x"),
            raw_v=reader.f64(f"{prefix} observation[{obs}] pixel y"),
            normalized_available=True,
            normalized_u=reader.f64(f"{prefix} observation[{obs}] normalized x"),
            normalized_v=reader.f64(f"{prefix} observation[{obs}] normalized y"),
        )
        for obs in range(observation_count)
    )
    parallax_2d_available, maximum_normalized_parallax = reader.optional_f64(
        f"{prefix} 2-D parallax"
    )
    maximum_image_motion_available, maximum_pixel_displacement = (
        reader.optional_f64(f"{prefix} maximum image motion")
    )
    last_displacement_available, last_frame_pixel_displacement = (
        reader.optional_f64(f"{prefix} last-frame displacement")
    )
    final_location_available = reader.boolean(f"{prefix} final location available")
    final_normalized_u = reader.f64(f"{prefix} final normalized x")
    final_normalized_v = reader.f64(f"{prefix} final normalized y")
    motion_available = reader.boolean(f"{prefix} causal motion available")
    motion_kind = reader.u64(f"{prefix} causal motion kind")
    motion_translation = reader.f64(f"{prefix} causal translation")
    motion_rotation = reader.f64(f"{prefix} causal rotation")
    candidate = CandidateRecord(
        ordinal=candidate_ordinal,
        feature_id=feature_id,
        lifecycle_reason=lifecycle_reason,
        lifecycle_status=lifecycle_status,
        raw_observation_count=raw_count,
        cleaned_observation_count=cleaned_count,
        prefilter_recorded=prefilter_recorded,
        prefilter_accepted=prefilter_accepted,
        prefilter_duration_ns=prefilter_duration_ns,
        time_range_available=time_range_available,
        first_timestamp=first_timestamp,
        last_timestamp=last_timestamp,
        track_age=track_age,
        observations=observations,
        parallax_2d_available=parallax_2d_available,
        maximum_normalized_parallax=maximum_normalized_parallax,
        maximum_image_motion_available=maximum_image_motion_available,
        maximum_pixel_displacement=maximum_pixel_displacement,
        last_frame_displacement_available=last_displacement_available,
        last_frame_pixel_displacement=last_frame_pixel_displacement,
        final_location_available=final_location_available,
        final_normalized_u=final_normalized_u,
        final_normalized_v=final_normalized_v,
        motion_kind=motion_kind,
        motion_available=motion_available,
        motion_translation=motion_translation,
        motion_rotation=motion_rotation,
    )

    reader.stage(Stage.GEOMETRY, f"{prefix} geometry")
    geometry_recorded = reader.boolean(f"{prefix} geometry recorded")
    triangulation_attempted = reader.boolean(f"{prefix} triangulation attempted")
    triangulation_succeeded = reader.boolean(f"{prefix} triangulation succeeded")
    refinement_attempted = reader.boolean(f"{prefix} refinement attempted")
    refinement_succeeded = reader.boolean(f"{prefix} refinement succeeded")
    geometry_valid = reader.boolean(f"{prefix} geometry valid")
    geometry_duration_ns = reader.u64(f"{prefix} geometry duration ns")
    p_f_in_g = reader.matrix(f"{prefix} p_FinG")
    depth_available, depth = reader.optional_f64(f"{prefix} depth")
    parallax_available, parallax = reader.optional_f64(f"{prefix} 3-D parallax")

    reader.stage(Stage.RAW_FACTOR, f"{prefix} raw factor")
    raw_available = reader.boolean(f"{prefix} raw factor available")
    raw_factor_duration_ns = reader.u64(f"{prefix} raw-factor duration ns")
    sigma_px = reader.f64(f"{prefix} sigma_px")
    noise_variance = reader.f64(f"{prefix} noise variance")
    whitening_kind = reader.u64(f"{prefix} whitening kind")
    layout = _parse_layout(reader, f"{prefix} layout")
    h_x = reader.matrix(f"{prefix} H_x")
    h_f = reader.matrix(f"{prefix} H_f")
    residual = reader.matrix(f"{prefix} residual")

    reader.stage(Stage.REDUCTION, f"{prefix} reduction")
    reduction_recorded = reader.boolean(f"{prefix} reduction recorded")
    reducer = reader.u64(f"{prefix} reducer")
    reducer_status = reader.u64(f"{prefix} reducer status")
    reducer_stage = reader.u64(f"{prefix} reducer stage")
    singular_values_available = reader.boolean(
        f"{prefix} singular values available"
    )
    singular_values = reader.matrix(f"{prefix} singular values")
    numerical_rank = reader.i64(f"{prefix} numerical rank")
    ratio_available, singular_ratio = reader.optional_f64(f"{prefix} singular ratio")
    reduced_a = reader.matrix(f"{prefix} reduced A")
    reduced_b = reader.matrix(f"{prefix} reduced b")
    reduction_duration_ns = reader.u64(f"{prefix} reduction duration ns")

    reader.stage(Stage.GATE, f"{prefix} gate")
    gate_recorded = reader.boolean(f"{prefix} gate recorded")
    gate_stage = reader.u64(f"{prefix} gate stage")
    gate_dof = reader.i64(f"{prefix} gate degrees of freedom")
    nis_available, nis = reader.optional_f64(f"{prefix} NIS")
    threshold_available, threshold = reader.optional_f64(f"{prefix} gate threshold")
    evidence_available = reader.boolean(f"{prefix} evidence decision available")
    evidence_accept = reader.boolean(f"{prefix} evidence accept")
    lifecycle_accept = reader.boolean(f"{prefix} lifecycle accept")
    gate_duration_ns = reader.u64(f"{prefix} gate duration ns")

    reader.stage(Stage.ACCUMULATION, f"{prefix} accumulation")
    accepted_for_global_system = reader.boolean(
        f"{prefix} accepted for global system"
    )
    global_row_start = reader.u64(f"{prefix} global row start")
    global_row_count = reader.u64(f"{prefix} global row count")
    accumulation_duration_ns = reader.u64(f"{prefix} accumulation duration ns")
    track = TrackRecord(
        update_id=update_id,
        candidate_ordinal=candidate_ordinal,
        feature_id=feature_id,
        geometry_recorded=geometry_recorded,
        triangulation_attempted=triangulation_attempted,
        triangulation_succeeded=triangulation_succeeded,
        refinement_attempted=refinement_attempted,
        refinement_succeeded=refinement_succeeded,
        geometry_valid=geometry_valid,
        p_f_in_g=p_f_in_g,
        depth_available=depth_available,
        depth=depth,
        parallax_3d_available=parallax_available,
        parallax_3d=parallax,
        geometry_duration_ns=geometry_duration_ns,
        raw_available=raw_available,
        h_x=h_x,
        h_f=h_f,
        residual=residual,
        sigma_px=sigma_px,
        noise_variance=noise_variance,
        whitening_kind=whitening_kind,
        layout=layout,
        raw_factor_duration_ns=raw_factor_duration_ns,
        reduction_recorded=reduction_recorded,
        reducer=reducer,
        reducer_status=reducer_status,
        reducer_stage=reducer_stage,
        singular_values_available=singular_values_available,
        singular_values=singular_values,
        numerical_rank=numerical_rank,
        singular_ratio_available=ratio_available,
        singular_ratio=singular_ratio,
        reduced_a=reduced_a,
        reduced_b=reduced_b,
        reduction_duration_ns=reduction_duration_ns,
        gate_recorded=gate_recorded,
        gate_stage=gate_stage,
        gate_dof=gate_dof,
        nis_available=nis_available,
        nis=nis,
        gate_threshold_available=threshold_available,
        gate_threshold=threshold,
        evidence_decision_available=evidence_available,
        evidence_accept=evidence_accept,
        lifecycle_accept=lifecycle_accept,
        gate_duration_ns=gate_duration_ns,
        accepted_for_global_system=accepted_for_global_system,
        global_row_start=global_row_start,
        global_row_count=global_row_count,
        accumulation_duration_ns=accumulation_duration_ns,
    )
    return candidate, track


def _parse_envelope(payload: bytes, expected_update_id: int) -> UpdateEnvelope:
    context = f"envelope {expected_update_id}"
    reader = _PayloadReader(payload, context)

    domain = reader.utf8("domain")
    reader.stage(Stage.CALLBACK, "identity")
    update_id = reader.u64("update ID")
    camera_timestamp = reader.f64("camera timestamp")
    raw_status = reader.u64("terminal status")
    try:
        terminal_status = TerminalStatus(raw_status)
    except ValueError as exc:
        raise CaptureValidationError(
            f"{context}: unknown terminal status {raw_status}"
        ) from exc
    terminal_reason = reader.u64("terminal reason")
    max_visual_passes = reader.u64("maximum visual passes")
    landmark_elimination = reader.u64("landmark elimination")
    fej_enabled = reader.boolean("FEJ enabled")
    calibrate_camera_pose = reader.boolean("calibrate camera pose")
    calibrate_camera_intrinsics = reader.boolean("calibrate camera intrinsics")
    calibrate_camera_timeoffset = reader.boolean("calibrate camera time offset")
    calibrate_imu_intrinsics = reader.boolean("calibrate IMU intrinsics")
    calibrate_imu_g_sensitivity = reader.boolean("calibrate IMU g-sensitivity")
    feature_representation = reader.i64("feature representation")

    reader.stage(Stage.CALLBACK_FINALIZATION, "callback costs")
    callback_costs = CallbackCosts(
        tracking_seconds=reader.f64("tracking seconds"),
        propagation_seconds=reader.f64("propagation seconds"),
        msckf_seconds=reader.f64("MSCKF seconds"),
        slam_update_seconds=reader.f64("SLAM update seconds"),
        slam_delay_seconds=reader.f64("SLAM delay seconds"),
        finalization_seconds=reader.f64("finalization seconds"),
        total_seconds=reader.f64("total seconds"),
    )
    timeline_available = reader.boolean("callback timeline available")
    callback_timeline = CallbackTimeline(
        available=timeline_available,
        callback_begin=reader.f64("callback begin offset seconds"),
        tracking_end=reader.f64("tracking end offset seconds"),
        propagation_end=reader.f64("propagation end offset seconds"),
        msckf_end=reader.f64("MSCKF end offset seconds"),
        slam_update_end=reader.f64("SLAM update end offset seconds"),
        slam_delay_end=reader.f64("SLAM delay end offset seconds"),
        finalization_end=reader.f64("finalization end offset seconds"),
    )
    reader.stage(Stage.CAPTURE_OUTPUT, "capture output")
    capture_output_available = reader.boolean("capture output available")
    record_construction_duration = reader.u64(
        "capture record-construction duration ns"
    )
    serialization_duration = reader.u64("capture serialization duration ns")
    capture_output_bytes = reader.u64("encoded envelope payload bytes")
    if not capture_output_available and (
        record_construction_duration != 0
        or serialization_duration != 0
        or capture_output_bytes != 0
    ):
        raise CaptureValidationError(
            f"{context}: unavailable capture-output metadata must use zeros"
        )
    capture_output = CaptureOutputMetadata(
        available=capture_output_available,
        record_construction_duration_ns=record_construction_duration,
        serialization_duration_ns=serialization_duration,
        encoded_payload_bytes=capture_output_bytes,
    )

    camera_count = reader.count("camera descriptor count")
    cameras = tuple(_parse_camera(reader, index) for index in range(camera_count))

    state_block_count = reader.count("state block count")
    state_blocks = tuple(
        _parse_state_block(reader, index) for index in range(state_block_count)
    )
    p_minus = reader.matrix("P_minus")

    track_count = reader.count("track count")
    candidate_track_pairs = tuple(
        _parse_track(reader, index, update_id) for index in range(track_count)
    )
    candidates = tuple(pair[0] for pair in candidate_track_pairs)
    tracks = tuple(pair[1] for pair in candidate_track_pairs)

    accepted_count = reader.count("accepted feature ID count")
    accepted_feature_ids = tuple(
        reader.u64(f"accepted feature ID[{index}]")
        for index in range(accepted_count)
    )
    reader.stage(Stage.ACCUMULATION, "selected system")
    selected_system = _parse_system(reader, "selected system")
    reader.stage(Stage.COMPRESSION, "compressed system")
    compressed_system = _parse_system(reader, "compressed system")

    reader.stage(Stage.PREVIEW, "production preview")
    posterior_recorded = reader.boolean("posterior recorded")
    preview_status = reader.u64("preview status")
    preview_stage = reader.u64("preview stage")
    global_gate_applied = reader.boolean("global gate applied")
    global_nis_available, global_nis = reader.optional_f64("global NIS")
    global_gate_accepted = reader.boolean("global gate accepted")
    global_gate = GlobalGate(
        applied=global_gate_applied,
        nis_available=global_nis_available,
        nis=global_nis,
        accepted=global_gate_accepted,
    )
    production_dx = reader.matrix("production dx")
    p_plus = reader.matrix("P_plus")
    preview_duration_ns = reader.u64("preview duration ns")

    reader.stage(Stage.COMMIT, "commit")
    mean_commit_count = reader.u64("mean commit count")
    covariance_commit_count = reader.u64("covariance commit count")
    feature_finalization_count = reader.u64("feature finalization count")
    commit_duration_ns = reader.u64("commit duration ns")
    reader.finish()

    envelope = UpdateEnvelope(
        domain=domain,
        update_id=update_id,
        camera_timestamp=camera_timestamp,
        terminal_status=terminal_status,
        terminal_reason=terminal_reason,
        max_visual_passes=max_visual_passes,
        landmark_elimination=landmark_elimination,
        fej_enabled=fej_enabled,
        calibrate_camera_pose=calibrate_camera_pose,
        calibrate_camera_intrinsics=calibrate_camera_intrinsics,
        calibrate_camera_timeoffset=calibrate_camera_timeoffset,
        calibrate_imu_intrinsics=calibrate_imu_intrinsics,
        calibrate_imu_g_sensitivity=calibrate_imu_g_sensitivity,
        feature_representation=feature_representation,
        callback_costs=callback_costs,
        callback_timeline=callback_timeline,
        capture_output=capture_output,
        cameras=cameras,
        state_blocks=state_blocks,
        p_minus=p_minus,
        candidates=candidates,
        tracks=tracks,
        accepted_feature_ids=accepted_feature_ids,
        selected_system=selected_system,
        compressed_system=compressed_system,
        posterior_recorded=posterior_recorded,
        preview_status=preview_status,
        preview_stage=preview_stage,
        global_gate=global_gate,
        production_dx=production_dx,
        preview_duration_ns=preview_duration_ns,
        p_plus=p_plus,
        mean_commit_count=mean_commit_count,
        covariance_commit_count=covariance_commit_count,
        feature_finalization_count=feature_finalization_count,
        commit_duration_ns=commit_duration_ns,
    )
    _validate_envelope(envelope, expected_update_id, len(payload), context)
    return envelope


def _parse_camera(reader: _PayloadReader, index: int) -> CameraDescriptor:
    reader.stage(Stage.CALLBACK, f"camera[{index}]")
    return CameraDescriptor(
        camera_id=reader.u64(f"camera[{index}] ID"),
        model=reader.u64(f"camera[{index}] model"),
        width=reader.i64(f"camera[{index}] width"),
        height=reader.i64(f"camera[{index}] height"),
        extrinsic_id=reader.i64(f"camera[{index}] extrinsic ID"),
        intrinsic_id=reader.i64(f"camera[{index}] intrinsic ID"),
        extrinsic_value=reader.matrix(f"camera[{index}] extrinsic value"),
        extrinsic_fej=reader.matrix(f"camera[{index}] extrinsic FEJ"),
        intrinsic_value=reader.matrix(f"camera[{index}] intrinsic value"),
        intrinsic_fej=reader.matrix(f"camera[{index}] intrinsic FEJ"),
        cache_value=reader.matrix(f"camera[{index}] projection cache"),
    )


def _parse_state_block(reader: _PayloadReader, index: int) -> StateBlock:
    reader.stage(Stage.CALLBACK, f"state block[{index}]")
    return StateBlock(
        ordinal=reader.u64(f"state block[{index}] ordinal"),
        block_type=reader.u64(f"state block[{index}] type"),
        key_type=reader.u64(f"state block[{index}] key type"),
        key_u64=reader.u64(f"state block[{index}] u64 key"),
        key_double=reader.f64(f"state block[{index}] double key"),
        variable_id=reader.i64(f"state block[{index}] variable ID"),
        covariance_id=reader.i64(f"state block[{index}] covariance ID"),
        offset=reader.i64(f"state block[{index}] offset"),
        dimension=reader.i64(f"state block[{index}] dimension"),
        role=reader.u64(f"state block[{index}] role"),
        current_value=reader.matrix(f"state block[{index}] current value"),
        fej_value=reader.matrix(f"state block[{index}] FEJ value"),
    )


def _validate_nonnegative(values: Sequence[Tuple[str, float]], context: str) -> None:
    for name, value in values:
        if value < 0.0:
            raise CaptureValidationError(f"{context}: {name} must be nonnegative")


def _timeline_values_match(left: float, right: float) -> bool:
    scale = max(1.0, abs(left), abs(right))
    return abs(left - right) <= _CALLBACK_TIMELINE_TOLERANCE * scale


def _validate_layout(
    layout: Sequence[LayoutBlock],
    local_columns: int,
    state_dimension: int,
    what: str,
    context: str,
) -> None:
    cursor = 0
    covariance_ranges: List[Tuple[int, int]] = []
    for index, block in enumerate(layout):
        if block.size == 0:
            raise CaptureValidationError(
                f"{context}: {what} block {index} has zero size"
            )
        if block.local_column != cursor:
            raise CaptureValidationError(
                f"{context}: {what} block {index} local column is "
                f"{block.local_column}, expected {cursor}"
            )
        if block.covariance_column + block.size > state_dimension:
            raise CaptureValidationError(
                f"{context}: {what} block {index} exceeds P_minus columns"
            )
        covariance_ranges.append(
            (block.covariance_column, block.covariance_column + block.size)
        )
        cursor += block.size
    if cursor != local_columns:
        raise CaptureValidationError(
            f"{context}: {what} covers {cursor} columns, matrix has {local_columns}"
        )
    ordered_ranges = sorted(covariance_ranges)
    if any(
        left[1] > right[0]
        for left, right in zip(ordered_ranges, ordered_ranges[1:])
    ):
        raise CaptureValidationError(f"{context}: {what} covariance ranges overlap")


def _validate_system(
    system: GlobalSystem, state_dimension: int, what: str, context: str
) -> None:
    if not system.available:
        if (
            system.duration_ns != 0
            or system.layout
            or system.h.values
            or system.residual.values
            or system.covariance.values
        ):
            raise CaptureValidationError(
                f"{context}: unavailable {what} must use canonical empty values"
            )
        return
    rows = system.h.rows
    if (system.residual.rows, system.residual.cols) != (rows, 1):
        raise CaptureValidationError(
            f"{context}: {what} residual must have shape {rows}x1"
        )
    if (system.covariance.rows, system.covariance.cols) != (rows, rows):
        raise CaptureValidationError(
            f"{context}: {what} R must have shape {rows}x{rows}"
        )
    _validate_layout(
        system.layout, system.h.cols, state_dimension, f"{what} layout", context
    )


def _validate_track(
    track: TrackRecord,
    candidate: CandidateRecord,
    state_dimension: int,
    context: str,
) -> None:
    prefix = f"{context}: track candidate {track.candidate_ordinal}"
    if track.feature_id != candidate.feature_id:
        raise CaptureValidationError(f"{prefix}: feature ID does not match candidate")
    if candidate.lifecycle_status not in range(len(TrackLifecycle)):
        raise CaptureValidationError(f"{prefix}: unknown lifecycle status")
    if not candidate.prefilter_recorded:
        raise CaptureValidationError(f"{prefix}: prefilter was not recorded")
    if track.geometry_recorded and not candidate.prefilter_accepted:
        raise CaptureValidationError(
            f"{prefix}: geometry exists for a prefilter-rejected track"
        )
    if not track.geometry_recorded and (
        track.triangulation_attempted
        or track.triangulation_succeeded
        or track.refinement_attempted
        or track.refinement_succeeded
        or track.geometry_valid
        or track.geometry_duration_ns != 0
    ):
        raise CaptureValidationError(
            f"{prefix}: unrecorded geometry has stage results"
        )
    if track.geometry_recorded and not track.triangulation_attempted:
        raise CaptureValidationError(
            f"{prefix}: recorded geometry did not attempt triangulation"
        )
    if track.triangulation_succeeded and not track.triangulation_attempted:
        raise CaptureValidationError(
            f"{prefix}: triangulation succeeded without an attempt"
        )
    if track.raw_available and not (
        track.geometry_recorded
        and track.triangulation_succeeded
        and track.refinement_succeeded
    ):
        raise CaptureValidationError(
            f"{prefix}: raw factor exists before successful geometry"
        )
    if track.reduction_recorded and not track.raw_available:
        raise CaptureValidationError(
            f"{prefix}: reduction exists without a raw factor"
        )
    if track.gate_recorded and not track.reduction_recorded:
        raise CaptureValidationError(
            f"{prefix}: gate exists without a recorded reduction"
        )
    if track.geometry_recorded:
        if (track.p_f_in_g.rows, track.p_f_in_g.cols) != (3, 1):
            raise CaptureValidationError(f"{prefix}: p_FinG must have shape 3x1")
    elif any(value != 0.0 for value in track.p_f_in_g.values):
        raise CaptureValidationError(
            f"{prefix}: unrecorded geometry must have a canonical zero p_FinG"
        )
    if track.geometry_valid and not track.geometry_recorded:
        raise CaptureValidationError(f"{prefix}: valid geometry was not recorded")

    rows = track.h_x.rows
    if track.raw_available:
        if (track.h_f.rows, track.residual.rows) != (rows, rows):
            raise CaptureValidationError(f"{prefix}: raw factor row counts disagree")
        if rows == 0 or track.h_f.cols != 3 or track.residual.cols != 1:
            raise CaptureValidationError(
                f"{prefix}: available H_f needs 3 columns and residual one column"
            )
        _validate_layout(
            track.layout, track.h_x.cols, state_dimension, "layout", prefix
        )
    elif (
        track.layout
        or any(value != 0.0 for value in track.h_x.values)
        or any(value != 0.0 for value in track.h_f.values)
        or any(value != 0.0 for value in track.residual.values)
    ):
        raise CaptureValidationError(
            f"{prefix}: unavailable raw factor must use canonical empty values"
        )
    if track.sigma_px <= 0.0 or track.noise_variance <= 0.0:
        raise CaptureValidationError(f"{prefix}: pixel noise values must be positive")
    if struct.pack(">d", track.noise_variance) != struct.pack(
        ">d", track.sigma_px * track.sigma_px
    ):
        raise CaptureValidationError(f"{prefix}: noise variance is not sigma_px squared")
    if track.whitening_kind != 1:
        raise CaptureValidationError(f"{prefix}: unknown whitening representation")
    if track.reducer not in (1, 2):
        raise CaptureValidationError(f"{prefix}: unknown reducer")
    if track.reducer_status not in range(5):
        raise CaptureValidationError(f"{prefix}: unknown reduction status")
    if track.reducer_stage not in range(14):
        raise CaptureValidationError(f"{prefix}: unknown reduction stage")
    if not track.singular_values_available and any(
        value != 0.0 for value in track.singular_values.values
    ):
        raise CaptureValidationError(
            f"{prefix}: unavailable singular values must be empty"
        )
    if track.singular_values_available and (
        track.singular_values.cols != 1
        or any(value < 0.0 for value in track.singular_values.values)
    ):
        raise CaptureValidationError(
            f"{prefix}: singular values must be a nonnegative column vector"
        )
    if track.reduction_recorded:
        if track.numerical_rank < -1 or track.numerical_rank > min(
            track.h_f.rows, track.h_f.cols
        ):
            raise CaptureValidationError(
                f"{prefix}: numerical rank exceeds H_f shape"
            )
        if track.singular_values_available and track.numerical_rank < 0:
            raise CaptureValidationError(
                f"{prefix}: available singular values need a numerical rank"
            )
        if not track.singular_values_available and track.numerical_rank != -1:
            raise CaptureValidationError(
                f"{prefix}: unavailable singular values require rank -1"
            )
        if track.reduced_a.values or track.reduced_b.values:
            if (
                track.reduced_a.rows != track.reduced_b.rows
                or track.reduced_b.cols != 1
            ):
                raise CaptureValidationError(
                    f"{prefix}: reduced A/b row counts disagree"
                )
            if track.reduced_a.cols != track.h_x.cols:
                raise CaptureValidationError(
                    f"{prefix}: reduced A columns do not match the active layout"
                )
    else:
        if track.numerical_rank != -1:
            raise CaptureValidationError(
                f"{prefix}: unrecorded reduction must use rank -1"
            )
        if any(value != 0.0 for value in track.reduced_a.values) or any(
            value != 0.0 for value in track.reduced_b.values
        ):
            raise CaptureValidationError(
                f"{prefix}: unrecorded reduction must have empty factor values"
            )
    if track.singular_ratio_available and track.singular_ratio < 0.0:
        raise CaptureValidationError(f"{prefix}: singular ratio must be nonnegative")
    if track.singular_ratio_available and not track.singular_values_available:
        raise CaptureValidationError(
            f"{prefix}: singular ratio exists without singular values"
        )
    if track.gate_stage not in range(6):
        raise CaptureValidationError(f"{prefix}: unknown gate stage")
    if track.gate_recorded and track.gate_dof < 0:
        raise CaptureValidationError(f"{prefix}: recorded gate has negative dof")
    if track.nis_available and track.nis < 0.0:
        raise CaptureValidationError(f"{prefix}: NIS must be nonnegative")
    if track.gate_threshold_available and track.gate_threshold < 0.0:
        raise CaptureValidationError(f"{prefix}: gate threshold must be nonnegative")
    if track.evidence_decision_available and not track.nis_available:
        raise CaptureValidationError(f"{prefix}: gate evidence has no NIS")
    if not track.evidence_decision_available and track.evidence_accept:
        raise CaptureValidationError(
            f"{prefix}: unavailable evidence decision must be false"
        )
    if track.accepted_for_global_system and not track.lifecycle_accept:
        raise CaptureValidationError(
            f"{prefix}: globally accepted track did not pass its gate"
        )
    if track.gate_recorded and track.reduced_a.rows == 0:
        raise CaptureValidationError(
            f"{prefix}: gate exists without an accepted reduced factor"
        )
    if track.accepted_for_global_system and not track.gate_recorded:
        raise CaptureValidationError(
            f"{prefix}: globally accepted track has no gate record"
        )
    if not track.accepted_for_global_system and (
        track.global_row_start != 0 or track.global_row_count != 0
    ):
        raise CaptureValidationError(
            f"{prefix}: unaccepted track must use a canonical zero row range"
        )
    if track.accepted_for_global_system and (
        track.global_row_count == 0
        or track.global_row_count != track.reduced_a.rows
    ):
        raise CaptureValidationError(
            f"{prefix}: accepted row range does not match reduced A"
        )

    if track.accepted_for_global_system:
        expected_lifecycle = TrackLifecycle.ACCUMULATED
    elif track.gate_recorded:
        expected_lifecycle = (
            TrackLifecycle.GATE_ACCEPTED
            if track.lifecycle_accept
            else TrackLifecycle.GATE_REJECTED
        )
    elif track.reduction_recorded:
        expected_lifecycle = (
            TrackLifecycle.RAW_AVAILABLE
            if track.reduced_a.rows > 0
            else TrackLifecycle.REDUCTION_REJECTED
        )
    elif track.raw_available:
        expected_lifecycle = TrackLifecycle.RAW_AVAILABLE
    elif track.geometry_recorded:
        if not track.triangulation_succeeded:
            expected_lifecycle = TrackLifecycle.TRIANGULATION_REJECTED
        elif not track.refinement_succeeded:
            expected_lifecycle = TrackLifecycle.REFINEMENT_REJECTED
        else:
            expected_lifecycle = TrackLifecycle.CANDIDATE
    elif candidate.prefilter_accepted:
        expected_lifecycle = TrackLifecycle.CANDIDATE
    else:
        expected_lifecycle = TrackLifecycle.PREFILTER_REJECTED
    if candidate.lifecycle_status != int(expected_lifecycle):
        raise CaptureValidationError(
            f"{prefix}: lifecycle status {candidate.lifecycle_status} is incoherent; "
            f"expected {int(expected_lifecycle)} ({expected_lifecycle.name.lower()})"
        )


def _validate_envelope(
    envelope: UpdateEnvelope,
    expected_update_id: int,
    payload_length: int,
    context: str,
) -> None:
    if envelope.domain != ENVELOPE_DOMAIN:
        raise CaptureValidationError(
            f"{context}: domain must be {ENVELOPE_DOMAIN!r}"
        )
    if envelope.update_id != expected_update_id:
        raise CaptureValidationError(
            f"{context}: update ID is {envelope.update_id}, expected "
            f"{expected_update_id}"
        )
    if envelope.max_visual_passes != 1:
        raise CaptureValidationError(
            f"{context}: schema 2 is one-pass-only, got pass count "
            f"{envelope.max_visual_passes}"
        )
    if envelope.landmark_elimination not in (1, 2):
        raise CaptureValidationError(f"{context}: unknown landmark elimination")
    if envelope.feature_representation < 0:
        raise CaptureValidationError(f"{context}: invalid feature representation")
    _validate_nonnegative(
        (
            ("tracking cost", envelope.callback_costs.tracking_seconds),
            ("propagation cost", envelope.callback_costs.propagation_seconds),
            ("MSCKF cost", envelope.callback_costs.msckf_seconds),
            ("SLAM update cost", envelope.callback_costs.slam_update_seconds),
            ("SLAM delay cost", envelope.callback_costs.slam_delay_seconds),
            ("finalization cost", envelope.callback_costs.finalization_seconds),
            ("total cost", envelope.callback_costs.total_seconds),
        ),
        context,
    )
    timeline = envelope.callback_timeline
    if not timeline.available:
        raise CaptureValidationError(f"{context}: callback timeline is unavailable")
    endpoints = (
        timeline.callback_begin,
        timeline.tracking_end,
        timeline.propagation_end,
        timeline.msckf_end,
        timeline.slam_update_end,
        timeline.slam_delay_end,
        timeline.finalization_end,
    )
    if struct.pack(">d", endpoints[0]) != b"\0" * 8:
        raise CaptureValidationError(
            f"{context}: callback timeline must begin at canonical +0.0"
        )
    if any(left > right for left, right in zip(endpoints, endpoints[1:])):
        raise CaptureValidationError(
            f"{context}: callback timeline endpoints are not monotonic"
        )
    durations = (
        envelope.callback_costs.tracking_seconds,
        envelope.callback_costs.propagation_seconds,
        envelope.callback_costs.msckf_seconds,
        envelope.callback_costs.slam_update_seconds,
        envelope.callback_costs.slam_delay_seconds,
        envelope.callback_costs.finalization_seconds,
    )
    duration_names = (
        "tracking",
        "propagation",
        "MSCKF",
        "SLAM update",
        "SLAM delay",
        "finalization",
    )
    for name, left, right, duration in zip(
        duration_names, endpoints, endpoints[1:], durations
    ):
        if not _timeline_values_match(right - left, duration):
            raise CaptureValidationError(
                f"{context}: callback {name} duration disagrees with timeline"
            )
    if not _timeline_values_match(
        endpoints[-1] - endpoints[0], envelope.callback_costs.total_seconds
    ):
        raise CaptureValidationError(
            f"{context}: callback total duration disagrees with timeline"
        )
    output = envelope.capture_output
    if not output.available:
        raise CaptureValidationError(f"{context}: capture-output metadata is unavailable")
    if output.serialization_duration_ns == 0:
        raise CaptureValidationError(
            f"{context}: capture serialization duration must be positive"
        )
    if output.encoded_payload_bytes != payload_length:
        raise CaptureValidationError(
            f"{context}: encoded payload byte count is "
            f"{output.encoded_payload_bytes}, expected {payload_length}"
        )
    camera_ids = [camera.camera_id for camera in envelope.cameras]
    if camera_ids != sorted(set(camera_ids)):
        raise CaptureValidationError(
            f"{context}: camera IDs must be unique and increasing"
        )
    for index, camera in enumerate(envelope.cameras):
        if (
            camera.model not in (1, 2)
            or camera.width <= 0
            or camera.height <= 0
            or camera.extrinsic_id < -1
            or camera.intrinsic_id < -1
        ):
            raise CaptureValidationError(
                f"{context}: camera[{index}] descriptor is incomplete"
            )
        if (
            camera.extrinsic_value.rows,
            camera.extrinsic_value.cols,
        ) != (
            camera.extrinsic_fej.rows,
            camera.extrinsic_fej.cols,
        ):
            raise CaptureValidationError(
                f"{context}: camera[{index}] extrinsic current/FEJ shapes differ"
            )
        if (
            camera.intrinsic_value.rows,
            camera.intrinsic_value.cols,
        ) != (
            camera.intrinsic_fej.rows,
            camera.intrinsic_fej.cols,
        ):
            raise CaptureValidationError(
                f"{context}: camera[{index}] intrinsic current/FEJ shapes differ"
            )
        for what, matrix in (
            ("extrinsic", camera.extrinsic_value),
            ("intrinsic", camera.intrinsic_value),
            ("projection cache", camera.cache_value),
        ):
            if matrix.rows == 0 or matrix.cols == 0:
                raise CaptureValidationError(
                    f"{context}: camera[{index}] {what} value is empty"
                )

    cursor = 0
    variable_ids = set()
    for index, block in enumerate(envelope.state_blocks):
        if block.ordinal != index:
            raise CaptureValidationError(
                f"{context}: state block ordinal {block.ordinal}, expected {index}"
            )
        if block.dimension <= 0 or block.offset != cursor:
            raise CaptureValidationError(
                f"{context}: state blocks do not contiguously partition P_minus"
            )
        if (
            block.variable_id != block.offset
            or block.covariance_id != block.offset
        ):
            raise CaptureValidationError(
                f"{context}: state block {index} variable ID differs from offset"
            )
        if block.block_type not in range(1, 12):
            raise CaptureValidationError(
                f"{context}: state block {index} has unknown block type"
            )
        if block.key_type not in range(4):
            raise CaptureValidationError(
                f"{context}: state block {index} has unknown key type"
            )
        zero_double = struct.pack(">d", block.key_double) == b"\0" * 8
        if block.key_type == 0 and (block.key_u64 != 0 or not zero_double):
            raise CaptureValidationError(
                f"{context}: unkeyed state block {index} must use zero keys"
            )
        if block.key_type == 1 and block.key_u64 != 0:
            raise CaptureValidationError(
                f"{context}: timestamp-keyed state block {index} has a u64 key"
            )
        if block.key_type in (2, 3) and not zero_double:
            raise CaptureValidationError(
                f"{context}: integer-keyed state block {index} has a double key"
            )
        if block.role != 3:
            raise CaptureValidationError(
                f"{context}: state block {index} must contain current and FEJ values"
            )
        if block.variable_id in variable_ids:
            raise CaptureValidationError(f"{context}: duplicate state variable ID")
        variable_ids.add(block.variable_id)
        if block.current_value.rows == 0 or block.current_value.cols != 1:
            raise CaptureValidationError(
                f"{context}: state block {index} current value must be a nonempty column"
            )
        if (
            block.fej_value.rows,
            block.fej_value.cols,
        ) != (
            block.current_value.rows,
            block.current_value.cols,
        ):
            raise CaptureValidationError(
                f"{context}: state block {index} current/FEJ shapes differ"
            )
        cursor += block.dimension
    if cursor == 0:
        raise CaptureValidationError(f"{context}: semantic state layout is empty")
    if (envelope.p_minus.rows, envelope.p_minus.cols) != (cursor, cursor):
        raise CaptureValidationError(
            f"{context}: P_minus must have shape {cursor}x{cursor}"
        )

    candidate_ids = set()
    for index, candidate in enumerate(envelope.candidates):
        if candidate.ordinal != index:
            raise CaptureValidationError(
                f"{context}: candidate ordinal {candidate.ordinal}, expected {index}"
            )
        if candidate.feature_id in candidate_ids:
            raise CaptureValidationError(f"{context}: duplicate candidate feature ID")
        candidate_ids.add(candidate.feature_id)
        if candidate.cleaned_observation_count > candidate.raw_observation_count:
            raise CaptureValidationError(
                f"{context}: candidate {index} cleaned count exceeds raw count"
            )
        if len(candidate.observations) != candidate.cleaned_observation_count:
            raise CaptureValidationError(
                f"{context}: candidate {index} cleaned count differs from samples"
            )
        if any(
            observation.camera_id not in set(camera_ids)
            for observation in candidate.observations
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} references an unknown camera"
            )
        if candidate.lifecycle_reason not in range(4):
            raise CaptureValidationError(
                f"{context}: candidate {index} has unknown lifecycle reason"
            )
        if not candidate.prefilter_recorded and (
            candidate.prefilter_accepted or candidate.prefilter_duration_ns != 0
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} has unrecorded prefilter values"
            )
        if candidate.time_range_available and (
            candidate.first_timestamp > candidate.last_timestamp
            or candidate.track_age < 0.0
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} timestamp interval is reversed"
            )
        if not candidate.time_range_available and any(
            struct.pack(">d", value) != b"\0" * 8
            for value in (
                candidate.first_timestamp,
                candidate.last_timestamp,
                candidate.track_age,
            )
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} unavailable time range is nonzero"
            )
        _validate_nonnegative(
            (
                ("2-D parallax", candidate.maximum_normalized_parallax),
                ("pixel displacement", candidate.maximum_pixel_displacement),
                ("last-frame displacement", candidate.last_frame_pixel_displacement),
            ),
            f"{context}: candidate {index}",
        )
        if not candidate.final_location_available and (
            struct.pack(">d", candidate.final_normalized_u) != b"\0" * 8
            or struct.pack(">d", candidate.final_normalized_v) != b"\0" * 8
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} unavailable location is nonzero"
            )
        if not candidate.motion_available and (
            candidate.motion_kind != 0
            or struct.pack(">d", candidate.motion_translation) != b"\0" * 8
            or struct.pack(">d", candidate.motion_rotation) != b"\0" * 8
        ):
            raise CaptureValidationError(
                f"{context}: candidate {index} unavailable motion is nonzero"
            )

    seen_track_ordinals = set()
    accepted_from_tracks: List[int] = []
    selected_row_cursor = 0
    for track in envelope.tracks:
        if track.update_id != envelope.update_id:
            raise CaptureValidationError(f"{context}: track references another update")
        if track.candidate_ordinal >= len(envelope.candidates):
            raise CaptureValidationError(f"{context}: track references missing candidate")
        if track.candidate_ordinal in seen_track_ordinals:
            raise CaptureValidationError(f"{context}: duplicate track candidate ordinal")
        seen_track_ordinals.add(track.candidate_ordinal)
        candidate = envelope.candidates[track.candidate_ordinal]
        _validate_track(track, candidate, cursor, context)
        if track.reducer != envelope.landmark_elimination:
            raise CaptureValidationError(
                f"{context}: track reducer differs from update reducer"
            )
        if track.accepted_for_global_system:
            accepted_from_tracks.append(track.feature_id)
            if track.global_row_start != selected_row_cursor:
                raise CaptureValidationError(
                    f"{context}: accepted global row ranges do not tile from zero"
                )
            selected_row_cursor += track.global_row_count
    if tuple(accepted_from_tracks) != envelope.accepted_feature_ids:
        raise CaptureValidationError(
            f"{context}: accepted track order differs from global accepted-ID order"
        )
    if len(set(envelope.accepted_feature_ids)) != len(envelope.accepted_feature_ids):
        raise CaptureValidationError(f"{context}: duplicate accepted feature ID")

    _validate_system(envelope.selected_system, cursor, "selected system", context)
    _validate_system(envelope.compressed_system, cursor, "compressed system", context)
    if envelope.selected_system.available != bool(envelope.accepted_feature_ids):
        raise CaptureValidationError(
            f"{context}: selected-system availability differs from accepted set"
        )
    if selected_row_cursor != envelope.selected_system.h.rows:
        raise CaptureValidationError(
            f"{context}: accepted row ranges do not tile selected H"
        )
    if envelope.compressed_system.available and not envelope.selected_system.available:
        raise CaptureValidationError(
            f"{context}: compressed system exists without a selected system"
        )
    if (
        envelope.compressed_system.available
        and envelope.compressed_system.h.rows > envelope.selected_system.h.rows
    ):
        raise CaptureValidationError(
            f"{context}: compression increased the selected row count"
        )
    if envelope.global_gate.nis_available and envelope.global_gate.nis < 0.0:
        raise CaptureValidationError(f"{context}: global NIS is negative")
    if envelope.preview_status not in range(5):
        raise CaptureValidationError(f"{context}: unknown preview status")
    if envelope.preview_stage not in range(14):
        raise CaptureValidationError(f"{context}: unknown preview stage")
    if not envelope.global_gate.applied and (
        envelope.global_gate.nis_available
        or envelope.global_gate.accepted
    ):
        raise CaptureValidationError(
            f"{context}: unapplied global gate must have unavailable evidence/result"
        )

    if envelope.production_dx.values and (
        envelope.production_dx.rows,
        envelope.production_dx.cols,
    ) != (cursor, 1):
        raise CaptureValidationError(
            f"{context}: production dx must have shape {cursor}x1"
        )
    if envelope.p_plus.values and (
        envelope.p_plus.rows,
        envelope.p_plus.cols,
    ) != (cursor, cursor):
        raise CaptureValidationError(
            f"{context}: P_plus must have shape {cursor}x{cursor}"
        )
    if envelope.posterior_recorded:
        if bool(envelope.production_dx.values) != bool(envelope.p_plus.values):
            raise CaptureValidationError(
                f"{context}: preview correction/posterior availability differs"
            )
    elif any(value != 0.0 for value in envelope.production_dx.values) or any(
        value != 0.0 for value in envelope.p_plus.values
    ):
        raise CaptureValidationError(
            f"{context}: unrecorded posterior must use canonical empty values"
        )
    for name, count in (
        ("mean commit", envelope.mean_commit_count),
        ("covariance commit", envelope.covariance_commit_count),
        ("feature finalization", envelope.feature_finalization_count),
    ):
        if count > 1:
            raise CaptureValidationError(f"{context}: {name} count exceeds one")
    committed = envelope.terminal_status == TerminalStatus.COMMITTED
    if committed:
        if not envelope.accepted_feature_ids:
            raise CaptureValidationError(f"{context}: committed update has no tracks")
        if (envelope.mean_commit_count, envelope.covariance_commit_count) != (1, 1):
            raise CaptureValidationError(
                f"{context}: committed update must commit mean and covariance once"
            )
        if not envelope.posterior_recorded:
            raise CaptureValidationError(
                f"{context}: committed update has no recorded posterior"
            )
        if not envelope.production_dx.values or not envelope.p_plus.values:
            raise CaptureValidationError(
                f"{context}: committed update has incomplete posterior values"
            )
    elif envelope.mean_commit_count or envelope.covariance_commit_count:
        raise CaptureValidationError(
            f"{context}: no-update callback committed mean or covariance"
        )
    if not envelope.candidates and (
        envelope.tracks
        or envelope.accepted_feature_ids
        or envelope.selected_system.available
        or envelope.compressed_system.available
    ):
        raise CaptureValidationError(
            f"{context}: zero-candidate envelope has visual factor data"
        )


class EnvelopeStream(Iterator[UpdateEnvelope]):
    """Single-pass, bounded-memory iterator over a schema-2 capture.

    Strict validation is complete only after iteration reaches ``StopIteration``
    (or :meth:`finish` is called), because the count, trailer checksum, and EOF
    are stored after the last envelope.
    """

    def __init__(self, stream: BinaryIO, *, close_stream: bool = False) -> None:
        self._stream = stream
        self._close_stream = close_stream
        self._capture_hash = hashlib.sha256()
        self._next_index = 0
        self._finished = False
        self._closed = False
        self._trailer_sha256: str | None = None
        self.header = self._read_header()

    def _read_and_hash(self, size: int, what: str) -> bytes:
        value = _read_exact(self._stream, size, what)
        self._capture_hash.update(value)
        return value

    def _read_header(self) -> CaptureHeader:
        magic = self._read_and_hash(len(MAGIC), "file magic")
        if magic != MAGIC:
            raise CaptureValidationError("file magic does not match SCVIOUPDATES0002")
        length_raw = self._read_and_hash(8, "header payload length")
        payload_length = struct.unpack(">Q", length_raw)[0]
        if payload_length == 0 or payload_length > MAX_HEADER_PAYLOAD_BYTES:
            raise CaptureValidationError(
                f"header payload length {payload_length} is outside [1, "
                f"{MAX_HEADER_PAYLOAD_BYTES}]"
            )
        payload = self._read_and_hash(payload_length, "header payload")
        checksum = self._read_and_hash(_SHA256_BYTES, "header checksum")
        if not hmac.compare_digest(checksum, hashlib.sha256(payload).digest()):
            raise CaptureValidationError("header payload SHA-256 mismatch")
        return _parse_header(payload)

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def envelope_count(self) -> int:
        return self._next_index

    @property
    def trailer_sha256(self) -> str:
        if self._trailer_sha256 is None:
            raise RuntimeError("capture trailer has not been validated")
        return self._trailer_sha256

    def __iter__(self) -> "EnvelopeStream":
        return self

    def __next__(self) -> UpdateEnvelope:
        if self._finished:
            raise StopIteration
        if self._closed:
            raise ValueError("capture stream is closed")
        length_raw = _read_exact(
            self._stream, 8, "envelope length or trailer sentinel"
        )
        payload_length = struct.unpack(">Q", length_raw)[0]
        if payload_length == TRAILER_SENTINEL:
            self._read_trailer()
            raise StopIteration
        self._capture_hash.update(length_raw)
        if payload_length == 0 or payload_length > MAX_ENVELOPE_PAYLOAD_BYTES:
            raise CaptureValidationError(
                f"envelope {self._next_index} payload length {payload_length} is "
                f"outside [1, {MAX_ENVELOPE_PAYLOAD_BYTES}]"
            )
        payload = self._read_and_hash(
            payload_length, f"envelope {self._next_index} payload"
        )
        checksum = self._read_and_hash(
            _SHA256_BYTES, f"envelope {self._next_index} checksum"
        )
        if not hmac.compare_digest(checksum, hashlib.sha256(payload).digest()):
            raise CaptureValidationError(
                f"envelope {self._next_index} payload SHA-256 mismatch"
            )
        envelope = _parse_envelope(payload, self._next_index)
        self._next_index += 1
        return envelope

    def _read_trailer(self) -> None:
        trailer_count = struct.unpack(
            ">Q", _read_exact(self._stream, 8, "trailer envelope count")
        )[0]
        checksum = _read_exact(self._stream, _SHA256_BYTES, "trailer checksum")
        if trailer_count != self._next_index:
            raise CaptureValidationError(
                f"trailer envelope count is {trailer_count}, parsed "
                f"{self._next_index}"
            )
        if not hmac.compare_digest(checksum, self._capture_hash.digest()):
            raise CaptureValidationError("trailer SHA-256 mismatch")
        if self._stream.read(1) != b"":
            raise CaptureValidationError("trailing bytes after capture trailer")
        self._trailer_sha256 = checksum.hex()
        self._finished = True

    def finish(self) -> None:
        """Consume remaining envelopes and authenticate the trailer/EOF."""

        for _ in self:
            pass

    def close(self) -> None:
        if not self._closed:
            if self._close_stream:
                self._stream.close()
            self._closed = True

    def __enter__(self) -> "EnvelopeStream":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def open_capture(path: Union[str, os.PathLike[str]]) -> EnvelopeStream:
    """Open ``path`` and return a streaming envelope iterator."""

    stream = open(path, "rb")
    try:
        return EnvelopeStream(stream, close_stream=True)
    except BaseException:
        stream.close()
        raise


def parse_capture(stream: BinaryIO) -> EnvelopeCapture:
    """Read and strictly validate a capture from an already-open stream."""

    reader = EnvelopeStream(stream)
    envelopes = tuple(reader)
    return EnvelopeCapture(
        header=reader.header,
        envelopes=envelopes,
        trailer_sha256=reader.trailer_sha256,
    )


def read_capture(path: Union[str, os.PathLike[str]]) -> EnvelopeCapture:
    """Read a complete capture into memory; prefer ``iter_envelopes`` for data."""

    with open_capture(path) as reader:
        envelopes = tuple(reader)
        return EnvelopeCapture(
            header=reader.header,
            envelopes=envelopes,
            trailer_sha256=reader.trailer_sha256,
        )


def iter_envelopes(path: Union[str, os.PathLike[str]]) -> Iterator[UpdateEnvelope]:
    """Yield envelopes with bounded memory and always validate the trailer."""

    with open_capture(path) as reader:
        yield from reader


def validate_capture(
    path: Union[str, os.PathLike[str]],
) -> CaptureValidationSummary:
    """Strictly validate ``path`` and return streaming aggregate metadata."""

    candidate_count = 0
    accepted_count = 0
    zero_candidate_count = 0
    no_update_count = 0
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    with open_capture(path) as reader:
        header = reader.header
        for envelope in reader:
            candidate_count += len(envelope.candidates)
            accepted_count += len(envelope.accepted_feature_ids)
            zero_candidate_count += not envelope.candidates
            no_update_count += envelope.terminal_status != TerminalStatus.COMMITTED
            if first_timestamp is None:
                first_timestamp = envelope.camera_timestamp
            last_timestamp = envelope.camera_timestamp
        return CaptureValidationSummary(
            header=header,
            trailer_sha256=reader.trailer_sha256,
            update_count=reader.envelope_count,
            candidate_count=candidate_count,
            accepted_count=accepted_count,
            zero_candidate_count=zero_candidate_count,
            no_update_count=no_update_count,
            first_timestamp=first_timestamp,
            last_timestamp=last_timestamp,
        )


def _file_sha256(path: Union[str, os.PathLike[str]]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    capture_paths: Sequence[Union[str, os.PathLike[str]]],
    *,
    relative_to: Union[str, os.PathLike[str]],
) -> Dict[str, Any]:
    """Strictly validate captures and return a deterministic manifest object."""

    base = Path(relative_to).resolve()
    entries: List[Dict[str, Any]] = []
    for supplied_path in capture_paths:
        path = Path(supplied_path).resolve()
        summary = validate_capture(path)
        entries.append(
            {
                "accepted_count": summary.accepted_count,
                "byte_size": path.stat().st_size,
                "candidate_count": summary.candidate_count,
                "config_sha256": summary.header.config_sha256,
                "first_timestamp": summary.first_timestamp,
                "last_timestamp": summary.last_timestamp,
                "no_update_count": summary.no_update_count,
                "relative_path": os.path.relpath(path, base),
                "run_id": summary.header.run_id,
                "schema_version": summary.header.schema_version,
                "sequence_id": summary.header.sequence_id,
                "sha256": _file_sha256(path),
                "source_commit": summary.header.source_commit,
                "strict_validation": "valid",
                "trailer_sha256": summary.trailer_sha256,
                "update_count": summary.update_count,
                "zero_candidate_count": summary.zero_candidate_count,
            }
        )
    entries.sort(key=lambda entry: entry["relative_path"])
    return {
        "captures": entries,
        "manifest_domain": "schurvio_update_envelope_v2_manifest",
        "schema_version": SCHEMA_VERSION,
    }


def create_manifest(
    output_path: Union[str, os.PathLike[str]],
    capture_paths: Sequence[Union[str, os.PathLike[str]]],
) -> Dict[str, Any]:
    """Validate captures and create ``output_path`` without overwriting it."""

    output = Path(output_path)
    manifest = build_manifest(capture_paths, relative_to=output.parent)
    serialized = (
        json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    # Mode ``x`` maps to O_CREAT|O_EXCL and therefore cannot overwrite even if
    # validation and output naming race another process.
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def _json_line(value: Dict[str, Any]) -> str:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _summary_json(summary: CaptureValidationSummary) -> Dict[str, Any]:
    return {
        "accepted_count": summary.accepted_count,
        "candidate_count": summary.candidate_count,
        "config_sha256": summary.header.config_sha256,
        "first_timestamp": summary.first_timestamp,
        "last_timestamp": summary.last_timestamp,
        "no_update_count": summary.no_update_count,
        "run_id": summary.header.run_id,
        "schema_version": summary.header.schema_version,
        "sequence_id": summary.header.sequence_id,
        "source_commit": summary.header.source_commit,
        "status": "valid",
        "trailer_sha256": summary.trailer_sha256,
        "update_count": summary.update_count,
        "zero_candidate_count": summary.zero_candidate_count,
    }


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate schema-2 captures or create a deterministic manifest"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("capture")
    manifest_parser = subparsers.add_parser("manifest")
    manifest_parser.add_argument("output")
    manifest_parser.add_argument("captures", nargs="+")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        if args.command == "validate":
            result = _summary_json(validate_capture(args.capture))
        else:
            result = create_manifest(args.output, args.captures)
        print(_json_line(result))
        return 0
    except (CaptureValidationError, OSError) as exc:
        print(_json_line({"error": str(exc), "status": "invalid"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
