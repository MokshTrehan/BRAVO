#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Postauthorization CP2-D trace assembly and immutable artifact lifecycle.

This module is deliberately inert at import time.  It does not read a project
file, registry, bag, ground-truth file, environment variable, or repository
state, and it does not import a bag provider.  ``run_sequence_pair.py`` may
import it only after the common readiness barrier has returned a held
authorization and that authorization has performed its sole registry read.

The pure assembly API is also the synthetic-test seam: real runtime plumbing
must provide completed, byte-retained mode traces and provenance inputs, while
this module independently validates their referential joins, computes the
frozen CP2-D shared-population mathematics, writes the exact fixed outputs,
and supports fsync/read-only/detached-verifier/no-overwrite finalization.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

import cp2_schema as schema
import cp2_sequence_math as sequence_math


MODES = ("nullspace", "schur")
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
ENTRYPOINTS = (
    "scripts/cp2/run_unit_gate.sh",
    "scripts/cp2/run_recorded_parity.py",
    "scripts/cp2/run_sequence_pair.py",
    "scripts/cp2/run_timing_pair.py",
    "scripts/cp2/verify_report.py",
)
OFFSETS_SECONDS = (40.0, 5.0, 0.0)
FROZEN_BAG_SHA256 = (
    "57f440ccd68ec8dc8f9461269f5909656b86198bac3adfd677b1fcc7a1428fa9",
    "c51b0064681dfb287b6653f5fd54e6c56af5d9151c866e17574fdbc527db2311",
    "6dc6192fac63dd0a05ba745548b41fe8cae14724168a98865a81d37e681bbc81",
)
FROZEN_GROUND_TRUTH_SHA256 = (
    "ab1579de35a047d241e2d0d1a4f4306b4fa51d99c6f11bcdebf336ab2b784df9",
    "8c7c9873f5cb102eda2b68d665134f144eed9d5778f0dbdc82bbf8e8fdbd558e",
    "6d2f961334ff3069105be0aacf118d3c7e82bf3ab0e238acd5f40f1d897573d1",
)
STRICT_PAIR_DELTA_NS = 20_000_000
MINIMUM_COVERAGE_NUMERATOR = 995
MINIMUM_COVERAGE_DENOMINATOR = 1000
TUM_HEADER = b"# timestamp tx ty tz qx qy qz qw\n"
SHARED_POPULATION_DOMAIN = b"SchurVIO-CP2-shared-population-v1\0"
SHARED_TIMESTAMPS_DOMAIN = b"SchurVIO-CP2-shared-timestamps-v1\0"
CP1_AUTHORIZATION_COMMIT = "8d80f483752411d34a3bc4c1ff6330b3a5c0fef3"
FROZEN_STATIC_SHA256 = (
    "b706f0082106e49e20c3292147d238b7e225b0df414106b9d4ac009bbb123f3b",
    "408ea8b60b5f9e7c8251e6d302f04c0675bfefdd31229bb1afd139bc8f4a0287",
    "b9e11b7bcda102f7c8c384c97318d67f3916b58942f9073722f83c22bd7073f7",
)
FROZEN_LAUNCH_SHA256 = "bede519721575d769a1fcef67c5527661cb39ba05ab77faa6c20771b0756c49a"
UTC_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$")
NORMALIZED_KEYS = (
    "/cp2_vio/up_msckf_landmark_elimination",
    "/cp2_vio/filepath_est",
    "/cp2_vio/filepath_std",
    "/cp2_vio/record_timing_filepath",
    "/cp2_vio/cp2_trace_directory",
    "/cp2_vio/cp2_context_path",
)
PATH_PARAMETER_KEYS = NORMALIZED_KEYS[1:]
ALLOWED_ROLES = frozenset(
    {
        "report",
        "provenance",
        "command",
        "trace",
        "payload",
        "source",
        "build",
        "configuration",
        "readiness",
        "log",
        "trajectory",
        "evaluator",
    }
)
PAIR_KEYS = (
    "schema_version",
    "record_type",
    "sequence_index",
    "sequence_id",
    "pair_index",
    "anchor_filtered_index",
    "anchor_camera_id",
    "cam0_filtered_index",
    "cam1_filtered_index",
    "cam0_record_time_ns",
    "cam1_record_time_ns",
    "cam0_header_time_ns",
    "cam1_header_time_ns",
    "absolute_record_delta_ns",
)
CALLBACK_KEYS = (
    "schema_version",
    "record_type",
    "sequence_index",
    "sequence_id",
    "mode",
    "callback_index",
    "pair_index",
    "anchor_filtered_index",
    "cam0_filtered_index",
    "cam1_filtered_index",
    "cam0_record_time_ns",
    "cam1_record_time_ns",
    "cam0_header_time_ns",
    "cam1_header_time_ns",
    "camera_timestamp_ns",
    "enqueue_entered",
    "enqueue_returned",
    "enqueue_status",
    "processing_entered",
    "processing_returned",
    "processing_status",
    "state_row_emitted",
    "trajectory_index",
)
TRAJECTORY_KEYS = (
    "schema_version",
    "record_type",
    "sequence_index",
    "sequence_id",
    "mode",
    "trajectory_index",
    "callback_index",
    "pair_index",
    "camera_timestamp_ns",
    "position_G",
    "quaternion_ItoG_xyzw",
)
RUN_KEYS = (
    "run_index",
    "mode",
    "executable_sha256",
    "loader_map_sha256",
    "resolved_parameters_sha256",
    "callback_trace_sha256",
    "trajectory_sha256",
    "processed_unique_pairs",
    "processing_fraction",
    "first_selected_timestamp_ns",
    "last_selected_timestamp_ns",
    "first_processed_timestamp_ns",
    "last_processed_timestamp_ns",
    "selected_duration_ns",
    "processed_duration_ns",
    "time_coverage",
    "completed",
    "exit_code",
)
REPORT_KEYS = (
    "schema_version",
    "record_type",
    "checkpoint",
    "status",
    "sequence_index",
    "sequence_id",
    "offset_seconds",
    "provenance_sha256",
    "pair_index_sha256",
    "valid_pair_count",
    "runs",
    "normalized_parameter_diff",
    "shared_timestamp_count",
    "shared_timestamp_sha256",
    "shared_population_sha256",
    "baseline_alignment",
    "position_p95_m",
    "orientation_p95_deg",
    "ate_nullspace_m",
    "ate_schur_m",
    "relative_ate_difference",
    "coverage_passed",
    "trajectory_passed",
    "passed",
)
PROVENANCE_KEYS = (
    "schema_version",
    "record_type",
    "checkpoint",
    "evidence_class",
    "distribution_status",
    "eligible_for_cp2_seal",
    "created_utc",
    "branch",
    "source_commit",
    "source_tree",
    "source_archive",
    "source_archive_sha256",
    "clean",
    "cp1_authorization_commit",
    "contracts",
    "entrypoints",
    "readiness_barrier",
    "unit_anchor",
    "build",
    "readiness_barrier_sha256",
    "runtime",
    "configuration",
    "inputs",
    "environment",
    "host",
    "file_inventory",
)
COMMAND_KEYS = (
    "schema_version",
    "record_type",
    "command_id",
    "phase",
    "sequence_index",
    "pair_index",
    "run_index",
    "argv",
    "cwd",
    "environment_sha256",
    "started_utc",
    "finished_utc",
    "exit_code",
    "timed_out",
    "stdout",
    "stdout_sha256",
    "stderr",
    "stderr_sha256",
)
COMMAND_PHASES = frozenset(
    {
        "readiness",
        "source_archive",
        "configure",
        "build",
        "runtime_preflight",
        "bag_identity",
        "pair_index",
        "ros_run",
        "trajectory",
        "evaluation",
        "verification",
    }
)


class SequenceRunnerError(ValueError):
    """Raised when an authorized CP2-D run cannot be proven exact."""


def _fail(message: str) -> None:
    raise SequenceRunnerError(message)


@dataclass(frozen=True)
class GroundTruthPose:
    timestamp_ns: int
    position: Tuple[float, float, float]
    quaternion_xyzw: Tuple[float, float, float, float]


@dataclass(frozen=True)
class SupportFile:
    payload: bytes
    role: str


@dataclass(frozen=True)
class ModeRunInput:
    mode: str
    pair_index_bytes: bytes
    callback_bytes: bytes
    trajectory_bytes: bytes
    prelaunch_raw_bytes: bytes
    runtime_raw_bytes: bytes
    prelaunch_parameters: Mapping[str, Any]
    runtime_parameters: Mapping[str, Any]
    runtime_root: Path
    executable_sha256: str
    loader_map_sha256: str
    legacy_state_bytes: bytes
    legacy_deviation_bytes: bytes
    legacy_timing_bytes: bytes
    evaluator_stdout_path: str
    evaluator_stdout_bytes: bytes
    evaluator_stderr_path: str
    evaluator_stderr_bytes: bytes
    evaluator_ate_m: float
    completed: bool = True
    exit_code: int = 0


@dataclass(frozen=True)
class SequenceAssemblyInput:
    sequence_index: int
    sequence_id: str
    offset_seconds: float
    run_id: str
    modes: Tuple[ModeRunInput, ModeRunInput]
    ground_truth: Tuple[GroundTruthPose, ...]
    provenance_without_inventory: Mapping[str, Any]
    commands_bytes: bytes
    support_files: Mapping[str, SupportFile]


@dataclass(frozen=True)
class AssemblyResult:
    report: Mapping[str, Any]
    provenance: Mapping[str, Any]
    manifest_sha256: str


@dataclass(frozen=True)
class _Coverage:
    processed_unique_pairs: int
    processing_fraction: float
    first_selected_timestamp_ns: int
    last_selected_timestamp_ns: int
    first_processed_timestamp_ns: int
    last_processed_timestamp_ns: int
    selected_duration_ns: int
    processed_duration_ns: int
    time_coverage: float


@dataclass(frozen=True)
class _ValidatedMode:
    source: ModeRunInput
    callbacks: Tuple[Mapping[str, Any], ...]
    trajectory: Tuple[Mapping[str, Any], ...]
    coverage: _Coverage
    canonical_parameters: bytes
    normalized_parameters: bytes
    normalized_diff: Tuple[Mapping[str, Any], ...]


def _bytes(value: Any, label: str) -> bytes:
    if not isinstance(value, bytes):
        _fail(label + " must be bytes")
    return value


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _u64(value: Any, label: str) -> int:
    try:
        return schema.validate_u64(value, label)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc


def _f64(value: Any, label: str) -> float:
    try:
        return schema.validate_f64(value, label)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc


def _sha_value(value: Any, label: str) -> str:
    try:
        return schema.validate_sha256(value, label)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc


def _absolute_normalized(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or "\0" in value
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or ".." in Path(value).parts
    ):
        _fail(label + " must be an absolute normalized path")
    return value


def _utc(value: Any, label: str) -> str:
    if not isinstance(value, str) or UTC_PATTERN.fullmatch(value) is None:
        _fail(label + " must be canonical UTC")
    try:
        dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise SequenceRunnerError(label + " is not a valid UTC instant") from exc
    return value


def _json_document(value: Mapping[str, Any]) -> bytes:
    try:
        return schema.json_line_bytes(value)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc


def _parse_jsonl(payload: bytes, label: str) -> Tuple[Mapping[str, Any], ...]:
    try:
        rows = tuple(schema.strict_jsonl_loads(_bytes(payload, label)))
        if schema.jsonl_bytes(rows) != payload:
            _fail(label + " is not the canonical compact JSONL projection")
        return rows
    except schema.SchemaError as exc:
        raise SequenceRunnerError(label + ": " + str(exc)) from exc


def _exact(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    try:
        return schema.exact_object_keys(value, keys, label)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc


def _identity(row: Mapping[str, Any], record_type: str, sequence_index: int, sequence_id: str) -> None:
    if (
        row.get("schema_version") != 1
        or row.get("record_type") != record_type
        or _u64(row.get("sequence_index"), record_type + " sequence index") != sequence_index
        or row.get("sequence_id") != sequence_id
    ):
        _fail(record_type + " identity differs from the requested sequence")


def _validate_sequence_identity(value: SequenceAssemblyInput) -> None:
    index = _u64(value.sequence_index, "sequence index")
    if index >= len(SEQUENCES) or SEQUENCES[index] != value.sequence_id:
        _fail("sequence index/ID differs from the frozen inventory")
    if not isinstance(value.offset_seconds, float) or not math.isfinite(value.offset_seconds):
        _fail("offset seconds must be a finite JSON binary64 value")
    if struct.pack(">d", value.offset_seconds) != struct.pack(">d", OFFSETS_SECONDS[index]):
        _fail("offset seconds differ from the frozen sequence offset")
    try:
        schema.validate_safe_id(value.run_id, "run ID")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    if tuple(mode.mode for mode in value.modes) != MODES:
        _fail("mode execution order must be exactly nullspace then schur")


def _validate_pairs(payload: bytes, sequence_index: int, sequence_id: str) -> Tuple[Mapping[str, Any], ...]:
    rows = _parse_jsonl(payload, "pair index")
    if len(rows) < 2:
        _fail("pair index must contain at least two selected rows")
    used_camera_indices = set()
    previous_anchor = -1
    for expected_index, row in enumerate(rows):
        _exact(row, PAIR_KEYS, "pair-index row")
        _identity(row, "pair_index", sequence_index, sequence_id)
        if _u64(row["pair_index"], "pair index") != expected_index:
            _fail("pair indices are not contiguous from zero")
        anchor = _u64(row["anchor_filtered_index"], "anchor filtered index")
        camera_id = _u64(row["anchor_camera_id"], "anchor camera ID")
        cam0 = _u64(row["cam0_filtered_index"], "cam0 filtered index")
        cam1 = _u64(row["cam1_filtered_index"], "cam1 filtered index")
        if camera_id not in (0, 1) or anchor != (cam0 if camera_id == 0 else cam1):
            _fail("pair anchor camera/index relation is invalid")
        if anchor <= previous_anchor or cam0 == cam1:
            _fail("pair selection order or camera identity is invalid")
        previous_anchor = anchor
        if cam0 in used_camera_indices or cam1 in used_camera_indices:
            _fail("a camera message is reused by the pair population")
        used_camera_indices.update((cam0, cam1))
        cam0_record = _u64(row["cam0_record_time_ns"], "cam0 record time")
        cam1_record = _u64(row["cam1_record_time_ns"], "cam1 record time")
        _u64(row["cam0_header_time_ns"], "cam0 header time")
        _u64(row["cam1_header_time_ns"], "cam1 header time")
        delta = _u64(row["absolute_record_delta_ns"], "absolute record delta")
        if delta != abs(cam0_record - cam1_record) or delta >= STRICT_PAIR_DELTA_NS:
            _fail("pair record-time delta is not exact or is outside the strict bound")
        candidate_index = cam1 if camera_id == 0 else cam0
        anchor_time = cam0_record if camera_id == 0 else cam1_record
        candidate_time = cam1_record if camera_id == 0 else cam0_record
        if candidate_index <= anchor or candidate_time < anchor_time:
            _fail("pair candidate is not forward of its anchor")
    selected_times = [_u64(row["cam0_record_time_ns"], "selected record time") for row in rows]
    if any(selected_times[index] > selected_times[index + 1] for index in range(len(selected_times) - 1)):
        _fail("selected cam0 record times reverse")
    if selected_times[-1] <= selected_times[0]:
        _fail("selected pair population has no positive duration")
    return rows


def _valid_event_transition(row: Mapping[str, Any]) -> bool:
    flags = (
        row["enqueue_entered"],
        row["enqueue_returned"],
        row["processing_entered"],
        row["processing_returned"],
        row["state_row_emitted"],
    )
    if any(not isinstance(value, bool) for value in flags):
        _fail("callback event fields must be Boolean")
    enqueue = row["enqueue_status"]
    processing = row["processing_status"]
    if (enqueue, processing) == ("queued", "processed"):
        return all(flags[:4])
    if (enqueue, processing) == ("frequency_dropped", "not_queued"):
        return flags[0] and flags[1] and not flags[2] and not flags[3] and not flags[4]
    return False


def _validate_callbacks(
    payload: bytes,
    mode: str,
    sequence_index: int,
    sequence_id: str,
    pairs: Sequence[Mapping[str, Any]],
) -> Tuple[Tuple[Mapping[str, Any], ...], _Coverage]:
    rows = _parse_jsonl(payload, mode + " callbacks")
    pair_by_index = {row["pair_index"]: row for row in pairs}
    referenced = set()
    processed: List[Mapping[str, Any]] = []
    source_keys = (
        "pair_index",
        "anchor_filtered_index",
        "cam0_filtered_index",
        "cam1_filtered_index",
        "cam0_record_time_ns",
        "cam1_record_time_ns",
        "cam0_header_time_ns",
        "cam1_header_time_ns",
    )
    for expected_index, row in enumerate(rows):
        _exact(row, CALLBACK_KEYS, mode + " callback row")
        _identity(row, "serial_callback", sequence_index, sequence_id)
        if row["mode"] != mode or _u64(row["callback_index"], "callback index") != expected_index:
            _fail(mode + " callback identity/order differs")
        pair_index = _u64(row["pair_index"], "callback pair index")
        if pair_index not in pair_by_index or pair_index in referenced:
            _fail(mode + " callback references a missing or duplicate pair")
        referenced.add(pair_index)
        pair = pair_by_index[pair_index]
        if any(row[key] != pair[key] for key in source_keys):
            _fail(mode + " callback source fields differ from pair index")
        if row["camera_timestamp_ns"] != pair["cam0_header_time_ns"]:
            _fail(mode + " callback camera timestamp is not cam0 header time")
        if not _valid_event_transition(row):
            _fail(mode + " callback has a nonpassing status/event transition")
        trajectory_index = row["trajectory_index"]
        if row["state_row_emitted"]:
            _u64(trajectory_index, "callback trajectory index")
        elif trajectory_index is not None:
            _fail("callback trajectory index is nonnull without a state row")
        if row["processing_status"] == "processed":
            processed.append(row)
        elif row["state_row_emitted"]:
            _fail("a nonprocessed callback emitted a trajectory state")

    if referenced != set(pair_by_index):
        _fail(mode + " callback population is not one-to-one with the pair index")
    emitted_indices = [row["trajectory_index"] for row in rows if row["state_row_emitted"]]
    if len(emitted_indices) != len(set(emitted_indices)):
        _fail(mode + " callbacks contain duplicate trajectory indices")

    pair_count = len(pairs)
    processed_count = len(processed)
    if processed_count <= 0 or processed_count > pair_count:
        _fail(mode + " processed pair population is empty or impossible")
    selected_first = _u64(pairs[0]["cam0_record_time_ns"], "first selected timestamp")
    selected_last = _u64(pairs[-1]["cam0_record_time_ns"], "last selected timestamp")
    processed_first = _u64(processed[0]["cam0_record_time_ns"], "first processed timestamp")
    processed_last = _u64(processed[-1]["cam0_record_time_ns"], "last processed timestamp")
    selected_duration = selected_last - selected_first
    if processed_last <= processed_first:
        _fail(mode + " processed population has no positive duration")
    processed_duration = processed_last - processed_first
    if (
        MINIMUM_COVERAGE_DENOMINATOR * processed_count
        < MINIMUM_COVERAGE_NUMERATOR * pair_count
        or MINIMUM_COVERAGE_DENOMINATOR * processed_duration
        < MINIMUM_COVERAGE_NUMERATOR * selected_duration
    ):
        _fail(mode + " processing fraction or time coverage is below 0.995")
    coverage = _Coverage(
        processed_unique_pairs=processed_count,
        processing_fraction=float(processed_count) / float(pair_count),
        first_selected_timestamp_ns=selected_first,
        last_selected_timestamp_ns=selected_last,
        first_processed_timestamp_ns=processed_first,
        last_processed_timestamp_ns=processed_last,
        selected_duration_ns=selected_duration,
        processed_duration_ns=processed_duration,
        time_coverage=float(processed_duration) / float(selected_duration),
    )
    return rows, coverage


def _pose_vector(value: Any, length: int, label: str) -> Tuple[float, ...]:
    if not isinstance(value, list) or len(value) != length:
        _fail("{} must contain exactly {} f64 values".format(label, length))
    return tuple(_f64(item, label) for item in value)


def _validate_trajectory(
    payload: bytes,
    mode: str,
    sequence_index: int,
    sequence_id: str,
    callbacks: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    rows = _parse_jsonl(payload, mode + " trajectory")
    callback_by_index = {row["callback_index"]: row for row in callbacks}
    emitted = {row["trajectory_index"]: row for row in callbacks if row["state_row_emitted"]}
    previous_timestamp = -1
    joined_callbacks = set()
    for expected_index, row in enumerate(rows):
        _exact(row, TRAJECTORY_KEYS, mode + " trajectory row")
        _identity(row, "trajectory_pose", sequence_index, sequence_id)
        if row["mode"] != mode or _u64(row["trajectory_index"], "trajectory index") != expected_index:
            _fail(mode + " trajectory identity/order differs")
        callback_index = _u64(row["callback_index"], "trajectory callback index")
        if callback_index not in callback_by_index or callback_index in joined_callbacks:
            _fail(mode + " trajectory references a missing or duplicate callback")
        callback = callback_by_index[callback_index]
        if (
            callback["processing_status"] != "processed"
            or callback["state_row_emitted"] is not True
            or callback["trajectory_index"] != expected_index
            or row["pair_index"] != callback["pair_index"]
            or row["camera_timestamp_ns"] != callback["camera_timestamp_ns"]
        ):
            _fail(mode + " trajectory/callback back-reference differs")
        if expected_index not in emitted:
            _fail(mode + " trajectory has no emitting callback")
        joined_callbacks.add(callback_index)
        timestamp = _u64(row["camera_timestamp_ns"], "trajectory timestamp")
        if timestamp <= previous_timestamp:
            _fail(mode + " trajectory timestamps are not strictly increasing and unique")
        previous_timestamp = timestamp
        _pose_vector(row["position_G"], 3, mode + " position")
        quaternion = _pose_vector(row["quaternion_ItoG_xyzw"], 4, mode + " quaternion")
        try:
            sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(quaternion)
        except sequence_math.SequenceMathError as exc:
            raise SequenceRunnerError(mode + " trajectory quaternion: " + str(exc)) from exc
    if len(rows) != len(emitted) or len(rows) == 0:
        _fail(mode + " trajectory does not join every and only state-emitting callback")
    return rows


def _inside_runtime_root(path_value: Any, root: Path, label: str) -> str:
    if not isinstance(path_value, str) or not path_value or "\0" in path_value or not os.path.isabs(path_value):
        _fail(label + " must be an absolute normalized path")
    if os.path.normpath(path_value) != path_value:
        _fail(label + " path is not normalized")
    absolute_root = os.path.abspath(str(root))
    try:
        root_status = os.lstat(absolute_root)
    except OSError as exc:
        raise SequenceRunnerError(label + " run partial is missing") from exc
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        _fail(label + " run partial is not a real directory")
    if os.path.commonpath((absolute_root, path_value)) != absolute_root or path_value == absolute_root:
        _fail(label + " path is not a child of its run partial directory")
    relative_parts = Path(path_value).relative_to(Path(absolute_root)).parts
    current = Path(absolute_root)
    for part in relative_parts:
        current = current / part
        try:
            status = os.lstat(str(current))
        except OSError as exc:
            raise SequenceRunnerError(label + " path is missing") from exc
        if stat.S_ISLNK(status.st_mode):
            _fail(label + " path or one of its parents is a symlink")
    return path_value


def _validate_parameters(source: ModeRunInput) -> Tuple[bytes, bytes, Tuple[Mapping[str, Any], ...]]:
    if not isinstance(source.prelaunch_parameters, Mapping) or not isinstance(source.runtime_parameters, Mapping):
        _fail(source.mode + " resolved parameters must be maps")
    try:
        prelaunch = schema.encode_resolved_parameters(source.prelaunch_parameters)
        runtime = schema.encode_resolved_parameters(source.runtime_parameters)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(source.mode + " parameters: " + str(exc)) from exc
    if prelaunch != runtime:
        _fail(source.mode + " prelaunch/runtime canonical parameters differ")
    if any(key not in source.runtime_parameters for key in NORMALIZED_KEYS):
        _fail(source.mode + " parameter map is missing an allowlisted normalized key")
    if source.runtime_parameters[NORMALIZED_KEYS[0]] != source.mode:
        _fail(source.mode + " mode parameter differs from the run mode")
    paths = tuple(
        _inside_runtime_root(source.runtime_parameters[key], source.runtime_root, source.mode + " " + key)
        for key in PATH_PARAMETER_KEYS
    )
    if len(set(paths)) != len(paths):
        _fail(source.mode + " runtime sink paths are not distinct")

    normalized = dict(source.runtime_parameters)
    differences = []
    for key in NORMALIZED_KEYS:
        value = normalized.pop(key)
        differences.append({"key": key, source.mode + "_typed_value": schema.typed_parameter_value(value)})
    try:
        normalized_bytes = schema.encode_resolved_parameters(normalized)
    except schema.SchemaError as exc:
        raise SequenceRunnerError(source.mode + " normalized parameters: " + str(exc)) from exc
    return runtime, normalized_bytes, tuple(differences)


def _parse_evaluator_rmse(payload: bytes, label: str) -> float:
    try:
        evaluator_text = _bytes(payload, label + " stdout").decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise SequenceRunnerError(label + " stdout is not UTF-8") from exc
    matches = []
    number = r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?"
    expression = re.compile(r"^[ \t]*rmse[ \t]+(" + number + r")[ \t]*$")
    for line in evaluator_text.splitlines():
        match = expression.fullmatch(line)
        if match is not None:
            matches.append(float(match.group(1)))
    if len(matches) != 1 or not math.isfinite(matches[0]) or matches[0] < 0.0:
        _fail(label + " stdout lacks one finite nonnegative rmse row")
    return matches[0]


def _validate_mode(
    source: ModeRunInput,
    sequence_index: int,
    sequence_id: str,
    pairs: Sequence[Mapping[str, Any]],
) -> _ValidatedMode:
    if source.mode not in MODES or source.completed is not True or source.exit_code != 0:
        _fail("mode run is incomplete, failed, or has an invalid mode")
    _sha_value(source.executable_sha256, source.mode + " executable SHA-256")
    _sha_value(source.loader_map_sha256, source.mode + " loader-map SHA-256")
    callbacks, coverage = _validate_callbacks(
        source.callback_bytes, source.mode, sequence_index, sequence_id, pairs
    )
    trajectory = _validate_trajectory(
        source.trajectory_bytes, source.mode, sequence_index, sequence_id, callbacks
    )
    canonical, normalized, difference = _validate_parameters(source)
    _bytes(source.prelaunch_raw_bytes, source.mode + " prelaunch raw parameters")
    _bytes(source.runtime_raw_bytes, source.mode + " runtime raw parameters")
    _bytes(source.legacy_state_bytes, source.mode + " legacy state")
    _bytes(source.legacy_deviation_bytes, source.mode + " legacy deviation")
    _bytes(source.legacy_timing_bytes, source.mode + " legacy timing")
    retained_ate = _f64(source.evaluator_ate_m, source.mode + " retained evaluator ATE")
    parsed_ate = _parse_evaluator_rmse(
        source.evaluator_stdout_bytes, source.mode + " evaluator"
    )
    if struct.pack(">d", parsed_ate) != struct.pack(">d", retained_ate):
        _fail(source.mode + " retained evaluator ATE differs from its stdout rmse")
    return _ValidatedMode(source, callbacks, trajectory, coverage, canonical, normalized, difference)


def _ground_truth(value: Sequence[GroundTruthPose]) -> Tuple[GroundTruthPose, ...]:
    rows = tuple(value)
    if not rows:
        _fail("ground-truth population is empty")
    previous = -1
    validated = []
    for row in rows:
        if not isinstance(row, GroundTruthPose):
            _fail("ground-truth row has the wrong type")
        timestamp = _u64(row.timestamp_ns, "ground-truth timestamp")
        if timestamp < previous:
            _fail("ground-truth timestamps are not nondecreasing")
        previous = timestamp
        if not isinstance(row.position, (tuple, list)) or len(row.position) != 3:
            _fail("ground-truth position has the wrong shape")
        if not isinstance(row.quaternion_xyzw, (tuple, list)) or len(row.quaternion_xyzw) != 4:
            _fail("ground-truth quaternion has the wrong shape")
        position = tuple(_f64(item, "ground-truth position") for item in row.position)
        quaternion = tuple(_f64(item, "ground-truth quaternion") for item in row.quaternion_xyzw)
        try:
            sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(quaternion)
        except sequence_math.SequenceMathError as exc:
            raise SequenceRunnerError("ground-truth quaternion: " + str(exc)) from exc
        validated.append(GroundTruthPose(timestamp, position, quaternion))
    return tuple(validated)


def _timestamp_text(timestamp_ns: int) -> str:
    return "{}.{:09d}".format(timestamp_ns // 1_000_000_000, timestamp_ns % 1_000_000_000)


def _number_text(value: Any) -> str:
    finite = _f64(value, "TUM pose component")
    return format(finite, ".17g").lower()


def _tum(rows: Iterable[Tuple[int, Sequence[float], Sequence[float]]]) -> bytes:
    output = bytearray(TUM_HEADER)
    for timestamp, position, quaternion in rows:
        values = tuple(position) + tuple(quaternion)
        if len(values) != 7:
            _fail("TUM pose row does not contain seven pose components")
        output.extend(
            (
                _timestamp_text(_u64(timestamp, "TUM timestamp"))
                + " "
                + " ".join(_number_text(value) for value in values)
                + "\n"
            ).encode("ascii")
        )
    return bytes(output)


def _rotation_quaternion_xyzw(rotation: Any) -> Tuple[float, float, float, float]:
    matrix = np.asarray(rotation, dtype=np.float64)
    try:
        sequence_math.validate_proper_rotation(matrix)
    except sequence_math.SequenceMathError as exc:
        raise SequenceRunnerError("aligned quaternion rotation: " + str(exc)) from exc
    trace = float(matrix[0, 0] + matrix[1, 1] + matrix[2, 2])
    if trace > 0.0:
        scale = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * scale
        x = (float(matrix[2, 1]) - float(matrix[1, 2])) / scale
        y = (float(matrix[0, 2]) - float(matrix[2, 0])) / scale
        z = (float(matrix[1, 0]) - float(matrix[0, 1])) / scale
    else:
        diagonal = [float(matrix[index, index]) for index in range(3)]
        axis = max(range(3), key=lambda index: diagonal[index])
        if axis == 0:
            scale = math.sqrt(1.0 + diagonal[0] - diagonal[1] - diagonal[2]) * 2.0
            x = 0.25 * scale
            y = (float(matrix[0, 1]) + float(matrix[1, 0])) / scale
            z = (float(matrix[0, 2]) + float(matrix[2, 0])) / scale
            w = (float(matrix[2, 1]) - float(matrix[1, 2])) / scale
        elif axis == 1:
            scale = math.sqrt(1.0 + diagonal[1] - diagonal[0] - diagonal[2]) * 2.0
            x = (float(matrix[0, 1]) + float(matrix[1, 0])) / scale
            y = 0.25 * scale
            z = (float(matrix[1, 2]) + float(matrix[2, 1])) / scale
            w = (float(matrix[0, 2]) - float(matrix[2, 0])) / scale
        else:
            scale = math.sqrt(1.0 + diagonal[2] - diagonal[0] - diagonal[1]) * 2.0
            x = (float(matrix[0, 2]) + float(matrix[2, 0])) / scale
            y = (float(matrix[1, 2]) + float(matrix[2, 1])) / scale
            z = 0.25 * scale
            w = (float(matrix[1, 0]) - float(matrix[0, 1])) / scale
    quaternion = np.asarray((x, y, z, w), dtype=np.float64)
    norm = float(np.linalg.norm(quaternion))
    if not math.isfinite(norm) or norm == 0.0:
        _fail("rotation-to-quaternion conversion is singular")
    quaternion /= norm
    # q and -q encode the same rotation.  This sign rule makes the projection
    # byte-deterministic without changing the represented orientation.
    for component in (quaternion[3], quaternion[0], quaternion[1], quaternion[2]):
        if component != 0.0:
            if component < 0.0:
                quaternion *= -1.0
            break
    return tuple(float(value) for value in quaternion)


def _shared_payload(
    timestamps: Sequence[int],
    associations: Sequence[sequence_math.TimestampAssociation],
    nullspace_rows: Sequence[Mapping[str, Any]],
    schur_rows: Sequence[Mapping[str, Any]],
    ground_truth: Sequence[GroundTruthPose],
) -> Tuple[bytes, bytes]:
    population = bytearray(SHARED_POPULATION_DOMAIN)
    timestamp_payload = bytearray(SHARED_TIMESTAMPS_DOMAIN)
    population.extend(struct.pack(">Q", len(timestamps)))
    timestamp_payload.extend(struct.pack(">Q", len(timestamps)))
    for timestamp, association, nullspace, schur in zip(timestamps, associations, nullspace_rows, schur_rows):
        gt = ground_truth[association.ground_truth_index]
        population.extend(struct.pack(">QQ", timestamp, gt.timestamp_ns))
        for row in (nullspace, schur):
            for value in tuple(row["position_G"]) + tuple(row["quaternion_ItoG_xyzw"]):
                population.extend(struct.pack(">d", _f64(value, "shared pose component")))
        for value in tuple(gt.position) + tuple(gt.quaternion_xyzw):
            population.extend(struct.pack(">d", _f64(value, "shared ground-truth component")))
        timestamp_payload.extend(struct.pack(">Q", timestamp))
    return bytes(population), bytes(timestamp_payload)


def _metric_artifacts(
    modes: Sequence[_ValidatedMode],
    ground_truth: Sequence[GroundTruthPose],
    *,
    verify_evaluator: bool = True,
) -> Tuple[Dict[str, bytes], Dict[str, Any]]:
    by_mode = {
        item.source.mode: {row["camera_timestamp_ns"]: row for row in item.trajectory}
        for item in modes
    }
    timestamps = sequence_math.shared_timestamp_intersection(
        tuple(by_mode["nullspace"]), tuple(by_mode["schur"])
    )
    if len(timestamps) < 3:
        _fail("shared trajectory population contains fewer than three poses")
    associations = sequence_math.associate_nearest_ground_truth(
        timestamps, (row.timestamp_ns for row in ground_truth)
    )
    if len(associations) != len(timestamps):
        _fail("a shared estimator timestamp has no ground-truth row within 10 ms")
    nullspace_rows = tuple(by_mode["nullspace"][timestamp] for timestamp in timestamps)
    schur_rows = tuple(by_mode["schur"][timestamp] for timestamp in timestamps)
    gt_rows = tuple(ground_truth[item.ground_truth_index] for item in associations)
    nullspace_positions = np.asarray([row["position_G"] for row in nullspace_rows], dtype=np.float64)
    schur_positions = np.asarray([row["position_G"] for row in schur_rows], dtype=np.float64)
    nullspace_quaternions = np.asarray(
        [row["quaternion_ItoG_xyzw"] for row in nullspace_rows], dtype=np.float64
    )
    schur_quaternions = np.asarray(
        [row["quaternion_ItoG_xyzw"] for row in schur_rows], dtype=np.float64
    )
    gt_positions = np.asarray([row.position for row in gt_rows], dtype=np.float64)
    alignment = sequence_math.baseline_kabsch_alignment(nullspace_positions, gt_positions)
    aligned = sequence_math.apply_common_alignment(
        alignment,
        nullspace_positions,
        nullspace_quaternions,
        schur_positions,
        schur_quaternions,
    )
    position_p95 = sequence_math.linear_p95(
        sequence_math.position_differences_m(aligned.nullspace_positions, aligned.schur_positions)
    )
    orientation_p95 = sequence_math.linear_p95(
        sequence_math.orientation_differences_deg(
            aligned.nullspace_inverse_rotations, aligned.schur_inverse_rotations
        )
    )
    ate_nullspace = sequence_math.translation_rmse_m(aligned.nullspace_positions, gt_positions)
    ate_schur = sequence_math.translation_rmse_m(aligned.schur_positions, gt_positions)
    relative_ate = sequence_math.relative_ate_difference(ate_nullspace, ate_schur)
    sequence_math.validate_metric_limits(position_p95, orientation_p95, relative_ate)

    if verify_evaluator:
        for mode_index, direct in enumerate((ate_nullspace, ate_schur)):
            retained = modes[mode_index].source.evaluator_ate_m
            if abs(float(retained) - float(direct)) > 1.0e-12 + 1.0e-10 * abs(float(direct)):
                _fail(MODES[mode_index] + " evo RMSE differs from direct shared-population RMSE")

    shared_population, shared_timestamps = _shared_payload(
        timestamps, associations, nullspace_rows, schur_rows, ground_truth
    )
    shared_population_sha = _sha(shared_population)
    files: Dict[str, bytes] = {
        "shared_population.bin": shared_population,
        "shared_timestamps.bin": shared_timestamps,
        "nullspace_raw.tum": _tum(
            (row["camera_timestamp_ns"], row["position_G"], row["quaternion_ItoG_xyzw"])
            for row in modes[0].trajectory
        ),
        "schur_raw.tum": _tum(
            (row["camera_timestamp_ns"], row["position_G"], row["quaternion_ItoG_xyzw"])
            for row in modes[1].trajectory
        ),
        "ground_truth_shared.tum": _tum(
            (row.timestamp_ns, row.position, row.quaternion_xyzw) for row in gt_rows
        ),
        "nullspace_shared_aligned.tum": _tum(
            (
                timestamps[index],
                aligned.nullspace_positions[index],
                _rotation_quaternion_xyzw(aligned.nullspace_inverse_rotations[index]),
            )
            for index in range(len(timestamps))
        ),
        "schur_shared_aligned.tum": _tum(
            (
                timestamps[index],
                aligned.schur_positions[index],
                _rotation_quaternion_xyzw(aligned.schur_inverse_rotations[index]),
            )
            for index in range(len(timestamps))
        ),
    }
    alignment_quaternion = _rotation_quaternion_xyzw(alignment.rotation)
    metrics = {
        "shared_timestamp_count": len(timestamps),
        "shared_timestamp_sha256": _sha(shared_timestamps),
        "shared_population_sha256": shared_population_sha,
        "baseline_alignment": {
            "source": "nullspace_to_ground_truth",
            "shared_population_sha256": shared_population_sha,
            "rotation_row_major": [float(value) for value in alignment.rotation.reshape(9)],
            "translation": [float(value) for value in alignment.translation],
            "quaternion_xyzw": list(alignment_quaternion),
            "source_singular_values": [float(value) for value in alignment.source_singular_values],
            "source_rank_threshold": float(alignment.source_rank_threshold),
            "determinant": float(alignment.determinant),
            "orthogonality_error_frobenius": float(alignment.orthogonality_error_frobenius),
            "applied_identically_to_both_modes": True,
        },
        "position_p95_m": float(position_p95),
        "orientation_p95_deg": float(orientation_p95),
        "ate_nullspace_m": float(ate_nullspace),
        "ate_schur_m": float(ate_schur),
        "relative_ate_difference": float(relative_ate),
    }
    return files, metrics


def _safe_path(root: Path, relative: str) -> Path:
    try:
        normalized = schema.validate_relpath(relative, "artifact path")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    candidate = root.joinpath(*normalized.split("/"))
    if os.path.commonpath((str(root), str(candidate))) != str(root):
        _fail("artifact path escapes its partial")
    return candidate


def _write_new(root: Path, relative: str, payload: bytes) -> None:
    target = _safe_path(root, relative)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(str(target), flags, 0o600)
    try:
        view = memoryview(_bytes(payload, relative + " payload"))
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("short artifact write for " + relative)
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _seal_existing(root: Path, relative: str, payload: bytes) -> None:
    target = _safe_path(root, relative)
    try:
        status_value = os.lstat(str(target))
    except OSError as exc:
        raise SequenceRunnerError("prepopulated support file is missing: " + relative) from exc
    if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
        _fail("prepopulated support path is not a single-link regular file: " + relative)
    descriptor = os.open(
        str(target),
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != status_value.st_dev
            or opened.st_ino != status_value.st_ino
            or opened.st_size != len(payload)
        ):
            _fail("prepopulated support binding/size differs: " + relative)
        observed = bytearray()
        while len(observed) < opened.st_size:
            block = os.read(descriptor, min(1024 * 1024, opened.st_size - len(observed)))
            if not block:
                _fail("prepopulated support file became short: " + relative)
            observed.extend(block)
        if bytes(observed) != payload or os.read(descriptor, 1) != b"":
            _fail("prepopulated support bytes differ: " + relative)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing_artifact_files(root: Path) -> set:
    files = set()
    for current, directories, filenames in os.walk(str(root), topdown=True, followlinks=False):
        directories.sort(key=os.fsencode)
        filenames.sort(key=os.fsencode)
        current_path = Path(current)
        for name in directories:
            candidate = current_path / name
            status_value = os.lstat(str(candidate))
            if not stat.S_ISDIR(status_value.st_mode) or stat.S_ISLNK(status_value.st_mode):
                _fail("prepopulated artifact contains a non-directory branch")
        for name in filenames:
            candidate = current_path / name
            relative = candidate.relative_to(root).as_posix()
            status_value = os.lstat(str(candidate))
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                _fail("prepopulated artifact contains a link or nonregular file: " + relative)
            files.add(relative)
    return files


def _role_for_fixed(path: str) -> str:
    if path == "commands.jsonl":
        return "command"
    if path.startswith("parameters/"):
        return "configuration"
    if path.endswith("_callbacks.jsonl") or path == "pair_index.jsonl":
        return "trace"
    if path.endswith("_trajectory.jsonl") or path.endswith(".tum") or path.endswith("_state.txt") or path.endswith("_deviation.txt"):
        return "trajectory"
    if path.endswith("_openvins_timing.csv"):
        return "log"
    if path in ("shared_population.bin", "shared_timestamps.bin"):
        return "payload"
    _fail("fixed artifact file has no role: " + path)


def _validate_commands(
    payload: bytes, support_files: Mapping[str, SupportFile]
) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    rows = _parse_jsonl(payload, "commands")
    if not rows:
        _fail("commands.jsonl must retain at least one top-level subprocess")
    log_owners = set()
    evaluator_rows = []
    for expected_id, row in enumerate(rows):
        _exact(row, COMMAND_KEYS, "command row")
        if (
            row["schema_version"] != 1
            or row["record_type"] != "command"
            or _u64(row["command_id"], "command ID") != expected_id
            or row["phase"] not in COMMAND_PHASES
        ):
            _fail("command identity/order/phase differs")
        for nullable in ("sequence_index", "pair_index", "run_index"):
            if row[nullable] is not None:
                _u64(row[nullable], "command " + nullable)
        if (
            not isinstance(row["argv"], list)
            or not row["argv"]
            or any(not isinstance(item, str) or "\0" in item for item in row["argv"])
        ):
            _fail("command argv must be a nonempty string array")
        _absolute_normalized(row["cwd"], "command cwd")
        _utc(row["started_utc"], "command start time")
        _utc(row["finished_utc"], "command finish time")
        if row["finished_utc"] < row["started_utc"]:
            _fail("command UTC interval reverses")
        _sha_value(row["environment_sha256"], "command environment SHA-256")
        if isinstance(row["exit_code"], bool) or row["exit_code"] != 0:
            _fail("a passing artifact contains a failed command")
        if not isinstance(row["timed_out"], bool):
            _fail("command timeout flag must be Boolean")
        if row["timed_out"]:
            _fail("a passing artifact contains a timed-out command")
        for field in ("stdout", "stderr"):
            path = row[field]
            try:
                schema.validate_relpath(path, "command " + field)
            except schema.SchemaError as exc:
                raise SequenceRunnerError(str(exc)) from exc
            if path in log_owners or path not in support_files:
                _fail("command log is missing, aliased, or not retained: " + path)
            log_owners.add(path)
            if _sha(support_files[path].payload) != row[field + "_sha256"]:
                _fail("command log digest differs: " + path)
        if row["phase"] == "evaluation":
            evaluator_rows.append(row)
    if len(evaluator_rows) != 2:
        _fail("commands.jsonl must contain exactly two evaluator commands")
    for run_index, row in enumerate(evaluator_rows):
        expected = [
            "evo_ape",
            "tum",
            "ground_truth_shared.tum",
            MODES[run_index] + "_shared_aligned.tum",
            "-r",
            "trans_part",
            "--t_max_diff",
            "0.01",
        ]
        if row["argv"] != expected or row["run_index"] != run_index:
            _fail("retained evaluator command differs from the frozen CP2-D argv/order")
    return tuple(evaluator_rows)  # type: ignore[return-value]


def _file_records(root: Path, roles: Mapping[str, str]) -> List[Mapping[str, Any]]:
    records = []
    for relative in sorted(roles, key=lambda value: value.encode("utf-8")):
        role = roles[relative]
        if role not in ALLOWED_ROLES:
            _fail("artifact file has an invalid role: " + role)
        path = _safe_path(root, relative)
        status = os.lstat(str(path))
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("artifact inventory path is not a single-link regular file: " + relative)
        records.append(
            {
                "path": relative,
                "role": role,
                "size": status.st_size,
                "mode": stat.S_IMODE(status.st_mode),
                "sha256": schema.sha256_file(path),
            }
        )
    return records


def _static_bundle_bytes(records: Sequence[Mapping[str, Any]]) -> bytes:
    if len(records) != 4:
        _fail("static bundle requires exactly four records")
    payload = bytearray(b"SchurVIO-CP2-static-config-v1\0")
    payload.extend(struct.pack(">Q", len(records)))
    for record in records:
        encoded_path = record["path"].encode("utf-8", "strict")
        payload.extend(struct.pack(">Q", len(encoded_path)))
        payload.extend(encoded_path)
        payload.extend(struct.pack(">Q", _u64(record["size"], "static bundle file size")))
        payload.extend(bytes.fromhex(_sha_value(record["sha256"], "static bundle file SHA-256")))
    return bytes(payload)


def _validate_provenance_base(value: Mapping[str, Any]) -> Dict[str, Any]:
    expected = tuple(key for key in PROVENANCE_KEYS if key != "file_inventory")
    _exact(value, expected, "provenance base")
    result = copy.deepcopy(dict(value))
    if (
        result["schema_version"] != 1
        or result["record_type"] != "provenance"
        or result["checkpoint"] != "CP2-D"
        or result["evidence_class"] != "trusted_runner_local_staging_evidence"
        or result["distribution_status"] != "internal_non_conveyable_staging"
        or result["eligible_for_cp2_seal"] is not False
        or result["clean"] is not True
    ):
        _fail("provenance policy identity differs from CP2-D staging evidence")
    if result["branch"] != "schurvio-lite/cp2-one-pass":
        _fail("provenance branch differs from the frozen CP2 branch")
    _utc(result["created_utc"], "provenance creation time")
    git_identity = re.compile(r"^[0-9a-f]{40}$")
    if (
        not isinstance(result["source_commit"], str)
        or git_identity.fullmatch(result["source_commit"]) is None
        or not isinstance(result["source_tree"], str)
        or git_identity.fullmatch(result["source_tree"]) is None
        or not isinstance(result["cp1_authorization_commit"], str)
        or git_identity.fullmatch(result["cp1_authorization_commit"]) is None
    ):
        _fail("provenance Git identity is invalid")
    if result["cp1_authorization_commit"] != CP1_AUTHORIZATION_COMMIT:
        _fail("provenance CP1 authorization commit differs from the frozen anchor")
    try:
        schema.validate_relpath(result["source_archive"], "source archive path")
        schema.validate_relpath(result["readiness_barrier"], "readiness barrier path")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    _sha_value(result["source_archive_sha256"], "source archive SHA-256")
    _sha_value(result["readiness_barrier_sha256"], "readiness barrier SHA-256")

    if not isinstance(result["contracts"], list) or not result["contracts"]:
        _fail("provenance contracts must be a nonempty sorted array")
    previous_contract = None
    for record in result["contracts"]:
        _exact(record, ("path", "sha256"), "contract record")
        try:
            encoded = schema.validate_relpath(record["path"], "contract path").encode("utf-8")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        if previous_contract is not None and encoded <= previous_contract:
            _fail("contract records are not strictly bytewise sorted")
        previous_contract = encoded
        _sha_value(record["sha256"], "contract SHA-256")

    if not isinstance(result["entrypoints"], list) or len(result["entrypoints"]) != 5:
        _fail("provenance entry-point inventory must contain exactly five records")
    for expected_path, record in zip(ENTRYPOINTS, result["entrypoints"]):
        _exact(record, ("path", "sha256", "git_blob"), "entry-point provenance record")
        try:
            schema.validate_relpath(record["path"], "entry-point path")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        _sha_value(record["sha256"], "entry-point SHA-256")
        if not isinstance(record["git_blob"], str) or git_identity.fullmatch(record["git_blob"]) is None:
            _fail("entry-point Git blob is invalid")
        if record["path"] != expected_path:
            _fail("entry-point provenance order/path differs")

    unit = _exact(
        result["unit_anchor"],
        ("artifact", "manifest_sha256", "tested_commit", "tested_tree", "report_sha256", "verified"),
        "unit anchor",
    )
    if (
        unit["verified"] is not True
        or git_identity.fullmatch(unit["tested_commit"] or "") is None
        or git_identity.fullmatch(unit["tested_tree"] or "") is None
    ):
        _fail("unit anchor identity or verification flag is invalid")
    _absolute_normalized(unit["artifact"], "unit anchor artifact")
    _sha_value(unit["manifest_sha256"], "unit manifest SHA-256")
    _sha_value(unit["report_sha256"], "unit report SHA-256")

    build = _exact(
        result["build"],
        (
            "fresh_git_archive",
            "workspace",
            "commands_sha256",
            "compile_commands",
            "compile_commands_sha256",
            "cmake_cache",
            "cmake_cache_sha256",
            "strict_fp_verified",
        ),
        "build provenance",
    )
    if build["fresh_git_archive"] is not True or build["strict_fp_verified"] is not True:
        _fail("build is not marked as a fresh strict-FP archive build")
    _absolute_normalized(build["workspace"], "build workspace")
    for field in ("commands_sha256", "compile_commands_sha256", "cmake_cache_sha256"):
        _sha_value(build[field], "build " + field)
    for field in ("compile_commands", "cmake_cache"):
        try:
            schema.validate_relpath(build[field], "build " + field)
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc

    runtime = _exact(
        result["runtime"],
        (
            "executable",
            "executable_size",
            "executable_sha256_before",
            "executable_sha256_after",
            "build_id_before",
            "build_id_after",
            "runs",
        ),
        "runtime provenance",
    )
    _absolute_normalized(runtime["executable"], "runtime executable")
    if _u64(runtime["executable_size"], "runtime executable size") == 0:
        _fail("runtime executable size must be positive")
    for field in ("executable_sha256_before", "executable_sha256_after"):
        _sha_value(runtime[field], "runtime " + field)
    if runtime["executable_sha256_before"] != runtime["executable_sha256_after"]:
        _fail("runtime executable bytes changed across the run")
    if runtime["build_id_before"] != runtime["build_id_after"]:
        _fail("runtime executable build ID changed across the run")
    if not isinstance(runtime["runs"], list) or len(runtime["runs"]) != 2:
        _fail("runtime provenance must contain exactly two run records")
    for run_index, record in enumerate(runtime["runs"]):
        _exact(
            record,
            (
                "run_id",
                "sequence_index",
                "mode",
                "loader_map_before",
                "loader_map_before_sha256",
                "loader_map_after",
                "loader_map_after_sha256",
                "dso_records_before",
                "dso_records_after",
            ),
            "runtime run record",
        )
        try:
            schema.validate_safe_id(record["run_id"], "runtime run ID")
            schema.validate_relpath(record["loader_map_before"], "loader map before")
            schema.validate_relpath(record["loader_map_after"], "loader map after")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        _u64(record["sequence_index"], "runtime sequence index")
        if record["mode"] != MODES[run_index]:
            _fail("runtime mode order differs")
        _sha_value(record["loader_map_before_sha256"], "loader map before SHA-256")
        _sha_value(record["loader_map_after_sha256"], "loader map after SHA-256")
        if record["dso_records_before"] != record["dso_records_after"]:
            _fail("runtime DSO identity set changed across a run")
        if not isinstance(record["dso_records_before"], list):
            _fail("runtime DSO records must be arrays")
        previous_path = None
        for dso in record["dso_records_before"]:
            _exact(dso, ("path", "soname", "size", "sha256", "build_id"), "DSO record")
            _absolute_normalized(dso["path"], "DSO path")
            encoded = dso["path"].encode("utf-8")
            if previous_path is not None and encoded <= previous_path:
                _fail("DSO records are not strictly bytewise-path sorted")
            previous_path = encoded
            if not isinstance(dso["soname"], str):
                _fail("DSO SONAME must be a string")
            _u64(dso["size"], "DSO size")
            _sha_value(dso["sha256"], "DSO SHA-256")
            if dso["build_id"] is not None and not isinstance(dso["build_id"], str):
                _fail("DSO build ID must be a string or null")

    configuration = _exact(
        result["configuration"],
        (
            "static_files",
            "static_bundle_payload",
            "static_bundle_sha256",
            "launch",
            "resolved_parameters",
            "runtime_contexts",
        ),
        "configuration provenance",
    )
    if not isinstance(configuration["static_files"], list) or len(configuration["static_files"]) != 3:
        _fail("configuration must contain exactly three static YAML records")
    expected_static_paths = (
        "config/euroc_mav/estimator_config.yaml",
        "config/euroc_mav/kalibr_imu_chain.yaml",
        "config/euroc_mav/kalibr_imucam_chain.yaml",
    )
    for expected_path, expected_sha, record in zip(
        expected_static_paths, FROZEN_STATIC_SHA256, configuration["static_files"]
    ):
        _exact(record, ("path", "size", "sha256"), "static configuration record")
        try:
            schema.validate_relpath(record["path"], "static configuration path")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        if _u64(record["size"], "static configuration size") == 0:
            _fail("static configuration size must be positive")
        _sha_value(record["sha256"], "static configuration SHA-256")
        if record["path"] != expected_path or record["sha256"] != expected_sha:
            _fail("static configuration literal path/order/hash differs")
    try:
        schema.validate_relpath(configuration["static_bundle_payload"], "static bundle payload")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    _sha_value(configuration["static_bundle_sha256"], "static bundle SHA-256")
    launch = _exact(configuration["launch"], ("path", "size", "sha256"), "launch record")
    try:
        schema.validate_relpath(launch["path"], "launch path")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    if _u64(launch["size"], "launch size") == 0:
        _fail("launch size must be positive")
    _sha_value(launch["sha256"], "launch SHA-256")
    if launch["path"] != "project/cp2_serial.launch" or launch["sha256"] != FROZEN_LAUNCH_SHA256:
        _fail("configuration launch path/hash differs from the frozen launch")
    if not isinstance(configuration["resolved_parameters"], list) or len(configuration["resolved_parameters"]) != 2:
        _fail("configuration must contain two resolved-parameter records")
    for record in configuration["resolved_parameters"]:
        _exact(
            record,
            (
                "run_id",
                "prelaunch_raw_path",
                "prelaunch_raw_sha256",
                "runtime_raw_path",
                "runtime_raw_sha256",
                "canonical_path",
                "canonical_sha256",
                "normalized_path",
                "normalized_sha256",
            ),
            "resolved-parameter record",
        )
        try:
            schema.validate_safe_id(record["run_id"], "resolved-parameter run ID")
            for field in ("prelaunch_raw_path", "runtime_raw_path", "canonical_path", "normalized_path"):
                schema.validate_relpath(record[field], "resolved-parameter " + field)
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        for field in (
            "prelaunch_raw_sha256",
            "runtime_raw_sha256",
            "canonical_sha256",
            "normalized_sha256",
        ):
            _sha_value(record[field], "resolved-parameter " + field)
    if not isinstance(configuration["runtime_contexts"], list) or len(configuration["runtime_contexts"]) != 2:
        _fail("configuration must contain two runtime-context records")
    for record in configuration["runtime_contexts"]:
        _exact(record, ("run_id", "path", "size", "sha256"), "runtime-context record")
        try:
            schema.validate_safe_id(record["run_id"], "runtime-context run ID")
            schema.validate_relpath(record["path"], "runtime-context path")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        _u64(record["size"], "runtime-context size")
        _sha_value(record["sha256"], "runtime-context SHA-256")

    if not isinstance(result["inputs"], list) or len(result["inputs"]) != 1:
        _fail("sequence-pair provenance must contain exactly one input record")
    for record in result["inputs"]:
        _exact(
            record,
            (
                "sequence_index",
                "sequence_id",
                "offset_seconds",
                "bag_path",
                "bag_size",
                "bag_sha256_before",
                "bag_sha256_after",
                "ground_truth_path",
                "ground_truth_sha256",
            ),
            "input record",
        )
        _u64(record["sequence_index"], "input sequence index")
        if not isinstance(record["offset_seconds"], float):
            _fail("input offset seconds must be encoded as JSON f64")
        _f64(record["offset_seconds"], "input offset")
        if not isinstance(record["sequence_id"], str):
            _fail("input sequence ID is invalid")
        _absolute_normalized(record["bag_path"], "input bag path")
        _absolute_normalized(record["ground_truth_path"], "input ground-truth path")
        if _u64(record["bag_size"], "input bag size") == 0:
            _fail("input bag size must be positive")
        for field in ("bag_sha256_before", "bag_sha256_after", "ground_truth_sha256"):
            _sha_value(record[field], "input " + field)
        if record["bag_sha256_before"] != record["bag_sha256_after"]:
            _fail("input bag bytes changed across the run")

    environment = _exact(result["environment"], ("classes",), "environment provenance")
    if not isinstance(environment["classes"], list) or not environment["classes"]:
        _fail("environment classes must be a nonempty array")
    previous_environment = None
    seen_environments = set()
    seen_environment_digests = set()
    for record in environment["classes"]:
        _exact(record, ("environment_id", "variables", "canonical_sha256"), "environment class")
        try:
            identifier = schema.validate_safe_id(record["environment_id"], "environment ID")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        encoded_identifier = identifier.encode("utf-8")
        if identifier in seen_environments or (
            previous_environment is not None and encoded_identifier <= previous_environment
        ):
            _fail("environment IDs are duplicate or not bytewise sorted")
        seen_environments.add(identifier)
        previous_environment = encoded_identifier
        if not isinstance(record["variables"], list) or not record["variables"]:
            _fail("environment variable population must be nonempty")
        variables = {}
        previous_name = None
        for variable in record["variables"]:
            _exact(variable, ("name", "value"), "environment variable")
            if not isinstance(variable["name"], str) or not isinstance(variable["value"], str):
                _fail("environment name/value must be strings")
            encoded_name = variable["name"].encode("utf-8")
            if previous_name is not None and encoded_name <= previous_name:
                _fail("environment variables are not strictly bytewise-name sorted")
            previous_name = encoded_name
            variables[variable["name"]] = variable["value"]
        try:
            canonical = schema.command_environment_sha256(variables)
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
        if canonical != record["canonical_sha256"]:
            _fail("environment canonical SHA-256 differs")
        if canonical in seen_environment_digests:
            _fail("environment classes contain a duplicate canonical payload")
        seen_environment_digests.add(canonical)

    host = _exact(
        result["host"],
        (
            "hostname",
            "os_release",
            "kernel_release",
            "architecture",
            "cpu_model",
            "logical_cpu_count",
            "ros_distribution",
            "compiler_version",
            "cmake_version",
            "catkin_version",
            "eigen_version",
            "opencv_version",
            "boost_version",
            "ceres_version",
            "python_version",
            "evo_version",
        ),
        "host provenance",
    )
    for key, item in host.items():
        if key == "logical_cpu_count":
            if item is not None:
                if _u64(item, "host logical CPU count") == 0:
                    _fail("host logical CPU count must be positive")
        elif item is not None and not isinstance(item, str):
            _fail("host provenance values must be strings or null")
    return result


def _retained_identity(root: Path, relative: str, expected_sha256: str, expected_size: Optional[int] = None) -> None:
    path = _safe_path(root, relative)
    try:
        status = os.lstat(str(path))
    except OSError as exc:
        raise SequenceRunnerError("provenance-referenced file is missing: " + relative) from exc
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        _fail("provenance-referenced path is not a single-link regular file: " + relative)
    if expected_size is not None and status.st_size != expected_size:
        _fail("provenance-referenced file size differs: " + relative)
    if schema.sha256_file(path) != expected_sha256:
        _fail("provenance-referenced file digest differs: " + relative)


def _validate_provenance_joins(
    root: Path,
    provenance: Mapping[str, Any],
    evidence: SequenceAssemblyInput,
    modes: Sequence[_ValidatedMode],
) -> None:
    _retained_identity(
        root, provenance["source_archive"], provenance["source_archive_sha256"]
    )
    _retained_identity(
        root, provenance["readiness_barrier"], provenance["readiness_barrier_sha256"]
    )
    unit = provenance["unit_anchor"]
    if (
        unit["tested_commit"] != provenance["source_commit"]
        or unit["tested_tree"] != provenance["source_tree"]
    ):
        _fail("unit-tested commit/tree differs from runtime provenance")

    build = provenance["build"]
    if build["commands_sha256"] != _sha(evidence.commands_bytes):
        _fail("build commands SHA-256 differs from commands.jsonl")
    _retained_identity(root, build["compile_commands"], build["compile_commands_sha256"])
    _retained_identity(root, build["cmake_cache"], build["cmake_cache_sha256"])

    runtime = provenance["runtime"]
    if any(
        mode.source.executable_sha256 != runtime["executable_sha256_before"] for mode in modes
    ):
        _fail("run report executable identity differs from runtime provenance")
    runtime_run_ids = []
    for run_index, (record, mode) in enumerate(zip(runtime["runs"], modes)):
        if record["sequence_index"] != evidence.sequence_index or record["mode"] != mode.source.mode:
            _fail("runtime run does not join the sequence/mode execution order")
        runtime_run_ids.append(record["run_id"])
        _retained_identity(root, record["loader_map_before"], record["loader_map_before_sha256"])
        _retained_identity(root, record["loader_map_after"], record["loader_map_after_sha256"])
    if len(set(runtime_run_ids)) != 2:
        _fail("runtime run IDs are not distinct")

    configuration = provenance["configuration"]
    for record in configuration["static_files"]:
        _retained_identity(root, record["path"], record["sha256"], record["size"])
    launch = configuration["launch"]
    _retained_identity(root, launch["path"], launch["sha256"], launch["size"])
    expected_bundle = _static_bundle_bytes(tuple(configuration["static_files"]) + (launch,))
    if _sha(expected_bundle) != configuration["static_bundle_sha256"]:
        _fail("static configuration bundle digest is not reconstructed from four records")
    _retained_identity(
        root,
        configuration["static_bundle_payload"],
        configuration["static_bundle_sha256"],
        len(expected_bundle),
    )
    if _safe_path(root, configuration["static_bundle_payload"]).read_bytes() != expected_bundle:
        _fail("retained static configuration bundle bytes are not canonical")

    resolved_run_ids = []
    for run_index, (record, mode) in enumerate(zip(configuration["resolved_parameters"], modes)):
        name = mode.source.mode
        expected = {
            "prelaunch_raw_path": "parameters/{}_prelaunch_raw.yaml".format(name),
            "prelaunch_raw_sha256": _sha(mode.source.prelaunch_raw_bytes),
            "runtime_raw_path": "parameters/{}_runtime_raw.yaml".format(name),
            "runtime_raw_sha256": _sha(mode.source.runtime_raw_bytes),
            "canonical_path": "parameters/{}_canonical.bin".format(name),
            "canonical_sha256": _sha(mode.canonical_parameters),
            "normalized_path": "parameters/{}_normalized.bin".format(name),
            "normalized_sha256": _sha(mode.normalized_parameters),
        }
        if any(record[field] != value for field, value in expected.items()):
            _fail("resolved-parameter provenance differs from assembled mode bytes")
        resolved_run_ids.append(record["run_id"])
    context_run_ids = []
    for record in configuration["runtime_contexts"]:
        context_run_ids.append(record["run_id"])
        _retained_identity(root, record["path"], record["sha256"], record["size"])
    if resolved_run_ids != runtime_run_ids or context_run_ids != runtime_run_ids:
        _fail("runtime/configuration/context run IDs do not join one-to-one")

    input_record = provenance["inputs"][0]
    if (
        input_record["sequence_index"] != evidence.sequence_index
        or input_record["sequence_id"] != evidence.sequence_id
        or struct.pack(">d", input_record["offset_seconds"])
        != struct.pack(">d", evidence.offset_seconds)
        or input_record["bag_sha256_before"] != FROZEN_BAG_SHA256[evidence.sequence_index]
        or input_record["bag_sha256_after"] != FROZEN_BAG_SHA256[evidence.sequence_index]
        or input_record["ground_truth_sha256"]
        != FROZEN_GROUND_TRUTH_SHA256[evidence.sequence_index]
    ):
        _fail("provenance input identity/hash differs from the frozen sequence")

    command_environment_hashes = {
        row["environment_sha256"] for row in _parse_jsonl(evidence.commands_bytes, "commands")
    }
    retained_environment_hashes = set()
    for record in provenance["environment"]["classes"]:
        digest = record["canonical_sha256"]
        variables = {item["name"]: item["value"] for item in record["variables"]}
        retained_environment_hashes.add(digest)
        readiness_only = "CP2_SELF_TEST" in variables or "CP2_FORBID_BAG_ACCESS" in variables
        if readiness_only:
            if variables != {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "CP2_SELF_TEST": "1",
                "CP2_FORBID_BAG_ACCESS": "1",
            }:
                _fail("readiness self-test environment class is not exact")
        elif record["environment_id"] == "readiness_git_v1":
            pass
        elif digest not in command_environment_hashes:
            _fail("a non-readiness environment class is unreferenced")
    if not command_environment_hashes.issubset(retained_environment_hashes):
        _fail("a command environment is absent from provenance")

    references = [
        provenance["source_archive"],
        provenance["readiness_barrier"],
        build["compile_commands"],
        build["cmake_cache"],
        configuration["static_bundle_payload"],
        launch["path"],
    ]
    references.extend(record["path"] for record in configuration["static_files"])
    references.extend(record["path"] for record in configuration["runtime_contexts"])
    for record in runtime["runs"]:
        references.extend((record["loader_map_before"], record["loader_map_after"]))
    command_rows = _parse_jsonl(evidence.commands_bytes, "commands")
    for row in command_rows:
        references.extend((row["stdout"], row["stderr"]))

    barrier_bytes = _safe_path(root, provenance["readiness_barrier"]).read_bytes()
    try:
        barrier = schema.strict_json_loads(barrier_bytes)
    except schema.SchemaError as exc:
        raise SequenceRunnerError("retained readiness barrier is not strict JSON") from exc
    if isinstance(barrier, Mapping):
        for field in (
            "source_before_payload",
            "build_before_payload",
            "results_before_payload",
            "testing_before_payload",
            "source_after_payload",
            "build_after_payload",
            "results_after_payload",
            "testing_after_payload",
            "post_lock_payload",
        ):
            if field in barrier:
                references.append(barrier[field])
        self_tests = barrier.get("self_tests", [])
        if not isinstance(self_tests, list):
            _fail("readiness barrier self-tests are not an array")
        for record in self_tests:
            if not isinstance(record, Mapping):
                _fail("readiness barrier self-test is not an object")
            for field in ("stdout", "stderr"):
                if field not in record:
                    _fail("readiness barrier self-test lacks a retained log")
                references.append(record[field])
    for relative in references:
        try:
            schema.validate_relpath(relative, "artifact support reference")
        except schema.SchemaError as exc:
            raise SequenceRunnerError(str(exc)) from exc
    if len(references) != len(set(references)):
        _fail("a support file is referenced by more than one owning field")
    if set(references) != set(evidence.support_files):
        missing = sorted(set(references) - set(evidence.support_files))
        orphaned = sorted(set(evidence.support_files) - set(references))
        _fail(
            "support-file ownership differs (missing={!r}, orphaned={!r})".format(
                missing, orphaned
            )
        )


def assemble_sequence_artifact(
    partial: Path,
    evidence: SequenceAssemblyInput,
    *,
    prepopulated_support: bool = False,
    after_validation: Optional[Callable[[], None]] = None,
) -> AssemblyResult:
    """Validate completed mode traces and populate one empty hidden partial."""

    partial = Path(partial).absolute()
    status = os.lstat(str(partial))
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        _fail("artifact partial must be an existing real directory")
    if not prepopulated_support and any(partial.iterdir()):
        _fail("artifact partial must be empty unless support was prepopulated")
    _validate_sequence_identity(evidence)
    nullspace_pairs = _validate_pairs(
        evidence.modes[0].pair_index_bytes, evidence.sequence_index, evidence.sequence_id
    )
    if evidence.modes[1].pair_index_bytes != evidence.modes[0].pair_index_bytes:
        _fail("nullspace and Schur pair-index bytes differ")
    modes = tuple(
        _validate_mode(item, evidence.sequence_index, evidence.sequence_id, nullspace_pairs)
        for item in evidence.modes
    )
    if modes[0].source.executable_sha256 != modes[1].source.executable_sha256:
        _fail("mode executable identities differ")
    if modes[0].normalized_parameters != modes[1].normalized_parameters:
        _fail("normalized parameter payloads differ outside the six-key allowlist")

    normalized_diff = []
    for index, key in enumerate(NORMALIZED_KEYS):
        left = modes[0].normalized_diff[index]
        right = modes[1].normalized_diff[index]
        if left["key"] != key or right["key"] != key:
            _fail("normalized parameter difference order differs")
        normalized_diff.append(
            {
                "key": key,
                "nullspace_typed_value": left["nullspace_typed_value"],
                "schur_typed_value": right["schur_typed_value"],
            }
        )

    gt_rows = _ground_truth(evidence.ground_truth)
    metric_files, metrics = _metric_artifacts(modes, gt_rows)
    evaluator_commands = _validate_commands(evidence.commands_bytes, evidence.support_files)
    for run_index, (mode, command) in enumerate(zip(modes, evaluator_commands)):
        source = mode.source
        if (
            command["sequence_index"] != evidence.sequence_index
            or command["pair_index"] is not None
            or command["run_index"] != run_index
            or command["stdout"] != source.evaluator_stdout_path
            or command["stderr"] != source.evaluator_stderr_path
            or command["stdout_sha256"] != _sha(source.evaluator_stdout_bytes)
            or command["stderr_sha256"] != _sha(source.evaluator_stderr_bytes)
        ):
            _fail(source.mode + " evaluator evidence does not join its command row")

    if after_validation is not None:
        after_validation()
    existing_files = _existing_artifact_files(partial)
    expected_existing = set(evidence.support_files) if prepopulated_support else set()
    if existing_files != expected_existing:
        _fail(
            "prepopulated support file set differs (missing={!r}, extra={!r})".format(
                sorted(expected_existing - existing_files),
                sorted(existing_files - expected_existing),
            )
        )

    fixed: Dict[str, bytes] = {
        "commands.jsonl": evidence.commands_bytes,
        "pair_index.jsonl": evidence.modes[0].pair_index_bytes,
    }
    for mode in modes:
        name = mode.source.mode
        fixed.update(
            {
                name + "_callbacks.jsonl": mode.source.callback_bytes,
                name + "_trajectory.jsonl": mode.source.trajectory_bytes,
                "parameters/" + name + "_prelaunch_raw.yaml": mode.source.prelaunch_raw_bytes,
                "parameters/" + name + "_runtime_raw.yaml": mode.source.runtime_raw_bytes,
                "parameters/" + name + "_canonical.bin": mode.canonical_parameters,
                "parameters/" + name + "_normalized.bin": mode.normalized_parameters,
                name + "_state.txt": mode.source.legacy_state_bytes,
                name + "_deviation.txt": mode.source.legacy_deviation_bytes,
                name + "_openvins_timing.csv": mode.source.legacy_timing_bytes,
            }
        )
    fixed.update(metric_files)
    roles: Dict[str, str] = {}
    for relative, payload in fixed.items():
        _write_new(partial, relative, payload)
        roles[relative] = _role_for_fixed(relative)

    for relative, support in evidence.support_files.items():
        if relative in roles or relative in ("cp2_report.json", "provenance.json", "SHA256SUMS"):
            _fail("support file collides with a fixed artifact path: " + relative)
        if not isinstance(support, SupportFile) or support.role not in ALLOWED_ROLES:
            _fail("support file has the wrong type or role")
        if prepopulated_support:
            _seal_existing(partial, relative, support.payload)
        else:
            _write_new(partial, relative, support.payload)
        roles[relative] = support.role
    for mode in modes:
        for relative, payload in (
            (mode.source.evaluator_stdout_path, mode.source.evaluator_stdout_bytes),
            (mode.source.evaluator_stderr_path, mode.source.evaluator_stderr_bytes),
        ):
            if relative in roles:
                # Evaluator logs normally also own command rows and therefore
                # arrive through support_files.  Byte equality prevents an
                # alternate unrecorded projection.
                if _safe_path(partial, relative).read_bytes() != payload:
                    _fail("evaluator log bytes differ from retained command log")
            else:
                _write_new(partial, relative, payload)
                roles[relative] = "evaluator"

    inventory = _file_records(partial, roles)
    provenance = _validate_provenance_base(evidence.provenance_without_inventory)
    _validate_provenance_joins(partial, provenance, evidence, modes)
    provenance["file_inventory"] = inventory
    provenance_bytes = _json_document(provenance)
    _write_new(partial, "provenance.json", provenance_bytes)
    provenance_sha = _sha(provenance_bytes)

    runs = []
    for run_index, mode in enumerate(modes):
        coverage = mode.coverage
        run = {
            "run_index": run_index,
            "mode": mode.source.mode,
            "executable_sha256": mode.source.executable_sha256,
            "loader_map_sha256": mode.source.loader_map_sha256,
            "resolved_parameters_sha256": _sha(mode.canonical_parameters),
            "callback_trace_sha256": _sha(mode.source.callback_bytes),
            "trajectory_sha256": _sha(mode.source.trajectory_bytes),
            "processed_unique_pairs": coverage.processed_unique_pairs,
            "processing_fraction": coverage.processing_fraction,
            "first_selected_timestamp_ns": coverage.first_selected_timestamp_ns,
            "last_selected_timestamp_ns": coverage.last_selected_timestamp_ns,
            "first_processed_timestamp_ns": coverage.first_processed_timestamp_ns,
            "last_processed_timestamp_ns": coverage.last_processed_timestamp_ns,
            "selected_duration_ns": coverage.selected_duration_ns,
            "processed_duration_ns": coverage.processed_duration_ns,
            "time_coverage": coverage.time_coverage,
            "completed": True,
            "exit_code": 0,
        }
        _exact(run, RUN_KEYS, "run report")
        runs.append(run)
    report = {
        "schema_version": 1,
        "record_type": "sequence_pair",
        "checkpoint": "CP2-D",
        "status": "passed",
        "sequence_index": evidence.sequence_index,
        "sequence_id": evidence.sequence_id,
        "offset_seconds": evidence.offset_seconds,
        "provenance_sha256": provenance_sha,
        "pair_index_sha256": _sha(evidence.modes[0].pair_index_bytes),
        "valid_pair_count": len(nullspace_pairs),
        "runs": runs,
        "normalized_parameter_diff": normalized_diff,
        "shared_timestamp_count": metrics["shared_timestamp_count"],
        "shared_timestamp_sha256": metrics["shared_timestamp_sha256"],
        "shared_population_sha256": metrics["shared_population_sha256"],
        "baseline_alignment": metrics["baseline_alignment"],
        "position_p95_m": metrics["position_p95_m"],
        "orientation_p95_deg": metrics["orientation_p95_deg"],
        "ate_nullspace_m": metrics["ate_nullspace_m"],
        "ate_schur_m": metrics["ate_schur_m"],
        "relative_ate_difference": metrics["relative_ate_difference"],
        "coverage_passed": True,
        "trajectory_passed": True,
        "passed": True,
    }
    _exact(report, REPORT_KEYS, "CP2-D report")
    _write_new(partial, "cp2_report.json", _json_document(report))

    manifest_entries = {}
    for path in partial.rglob("*"):
        if path.is_dir():
            continue
        relative = path.relative_to(partial).as_posix()
        if relative == "SHA256SUMS":
            continue
        status = os.lstat(str(path))
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("manifest path is not a single-link regular file")
        manifest_entries[relative] = schema.sha256_file(path)
    manifest = schema.manifest_bytes(manifest_entries)
    _write_new(partial, "SHA256SUMS", manifest)
    return AssemblyResult(report, provenance, _sha(manifest))


def _snapshot_tree(root: Path) -> Tuple[Tuple[str, str, int, int], ...]:
    root_status = os.lstat(str(root))
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        _fail("artifact root is not a real directory")
    records = [("", "directory", stat.S_IMODE(root_status.st_mode), 0)]
    for path in sorted(root.rglob("*"), key=lambda value: value.relative_to(root).as_posix().encode("utf-8")):
        relative = path.relative_to(root).as_posix()
        status = os.lstat(str(path))
        if stat.S_ISDIR(status.st_mode) and not stat.S_ISLNK(status.st_mode):
            records.append((relative, "directory", stat.S_IMODE(status.st_mode), 0))
        elif stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
            records.append((relative, schema.sha256_file(path), stat.S_IMODE(status.st_mode), status.st_size))
        else:
            _fail("artifact contains a symlink, hardlink, or nonregular entry")
    return tuple(records)


def make_tree_read_only(root: Path) -> None:
    root = Path(root).absolute()
    for path in root.rglob("*"):
        status = os.lstat(str(path))
        if stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
            descriptor = os.open(
                str(path),
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        elif not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
            _fail("artifact contains a forbidden filesystem entry")
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
        descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fchmod(descriptor, 0o555)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fchmod(descriptor, 0o555)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _fail("renameat2(RENAME_NOREPLACE) is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(str(source)),
        -100,
        os.fsencode(str(destination)),
        1,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            _fail("final artifact already exists; overwrite is forbidden")
        raise OSError(error, os.strerror(error), str(destination))


def _cleanup_partial(path: Path) -> None:
    if not os.path.lexists(str(path)):
        return
    for child in path.rglob("*"):
        if child.is_dir() and not child.is_symlink():
            os.chmod(str(child), 0o700)
        elif not child.is_symlink():
            os.chmod(str(child), 0o600)
    os.chmod(str(path), 0o700)
    shutil.rmtree(str(path))


def build_verify_seal(
    staging_parent: Path,
    run_id: str,
    builder: Callable[[Path], AssemblyResult],
    verifier: Callable[[Path, str], Mapping[str, Any]],
) -> Tuple[Path, AssemblyResult]:
    """Build one hidden partial, verify immutable bytes, and rename no-overwrite."""

    try:
        schema.validate_safe_id(run_id, "run ID")
    except schema.SchemaError as exc:
        raise SequenceRunnerError(str(exc)) from exc
    parent = Path(staging_parent).absolute()
    parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    parent_status = os.lstat(str(parent))
    if not stat.S_ISDIR(parent_status.st_mode) or stat.S_ISLNK(parent_status.st_mode):
        _fail("sequence staging parent is not a real directory")
    final = parent / run_id
    if os.path.lexists(str(final)):
        _fail("final artifact already exists; overwrite is forbidden")
    partial = Path(tempfile.mkdtemp(prefix="." + run_id + ".partial.", dir=str(parent)))
    os.chmod(str(partial), 0o700)
    try:
        result = builder(partial)
        if not isinstance(result, AssemblyResult):
            _fail("artifact builder returned the wrong result type")
        make_tree_read_only(partial)
        before = _snapshot_tree(partial)
        verification = verifier(partial, result.manifest_sha256)
        if not isinstance(verification, Mapping) or verification.get("passed") is not True:
            _fail("detached exact-byte verifier did not pass")
        after = _snapshot_tree(partial)
        if after != before:
            _fail("artifact bytes or modes changed during detached verification")
        _rename_noreplace(partial, final)
        parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        return final, result
    except BaseException:
        _cleanup_partial(partial)
        raise


def detached_verifier(repo_root: Path) -> Callable[[Path, str], Mapping[str, Any]]:
    """Return the production verifier callback with external temporary logs."""

    root = Path(repo_root).absolute()
    verifier_path = root / "scripts/cp2/verify_report.py"

    def invoke(artifact: Path, manifest_sha256: str) -> Mapping[str, Any]:
        temporary = Path(tempfile.mkdtemp(prefix="schurvio-cp2-sequence-verify-", dir="/tmp"))
        stdout_path = temporary / "stdout"
        stderr_path = temporary / "stderr"
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        argv = (
            "/usr/bin/python3",
            "-I",
            "-B",
            str(verifier_path),
            "--verify-sequence",
            str(Path(artifact).absolute()),
            "--manifest-sha256",
            _sha_value(manifest_sha256, "manifest SHA-256"),
        )
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            completed = subprocess.run(
                argv,
                cwd=str(root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
            )
            stdout.flush()
            stderr.flush()
            os.fsync(stdout.fileno())
            os.fsync(stderr.fileno())
        if completed.returncode != 0:
            _fail("detached sequence verifier failed; logs retained at " + str(temporary))
        lines = stdout_path.read_bytes().splitlines()
        if not lines:
            _fail("detached sequence verifier emitted no result; logs retained at " + str(temporary))
        try:
            result = schema.strict_json_loads(lines[-1])
        except schema.SchemaError as exc:
            raise SequenceRunnerError(
                "detached sequence verifier result is invalid; logs retained at " + str(temporary)
            ) from exc
        if not isinstance(result, Mapping) or result.get("passed") is not True:
            _fail("detached sequence verifier rejected the artifact; logs retained at " + str(temporary))
        shutil.rmtree(str(temporary))
        return result

    return invoke


def run_authorized_sequence(
    *,
    repo_root: str,
    parsed_cli: Mapping[str, str],
    authorization: Any,
    registry_bytes: bytes,
) -> Mapping[str, str]:
    """Production postauthorization hook.

    The one verified registry buffer is resolved in memory first.  Only then is
    the concrete actual executor imported and given the single requested,
    hash-bound input record while the readiness authorization remains held.
    """

    if getattr(authorization, "prebag_authorized", False) is not True:
        _fail("postauthorization executor received no held readiness authorization")
    if not isinstance(repo_root, str) or not os.path.isabs(repo_root):
        _fail("postauthorization repository root is not absolute")
    if not isinstance(parsed_cli, Mapping) or parsed_cli.get("--sequence") not in SEQUENCES:
        _fail("postauthorization sequence selection differs from the frozen inventory")
    authorization.revalidate()
    resolver = __import__("cp2_postauth_registry")
    resolver_path = os.path.abspath(getattr(resolver, "__file__", ""))
    expected_resolver_path = os.path.join(repo_root, "scripts", "cp2", "cp2_postauth_registry.py")
    try:
        resolver_status = os.lstat(resolver_path)
    except OSError as exc:
        raise SequenceRunnerError("postauthorization resolver identity is unavailable") from exc
    if (
        resolver_path != expected_resolver_path
        or not stat.S_ISREG(resolver_status.st_mode)
        or resolver_status.st_nlink != 1
    ):
        _fail("postauthorization resolver identity differs")
    resolved = resolver.resolve_postauthorized_sequence_inputs(registry_bytes)
    selected = tuple(
        item for item in resolved if item.sequence_id == parsed_cli["--sequence"]
    )
    if len(selected) != 1:
        _fail("postauthorization resolver did not return the requested sequence exactly once")
    authorization.revalidate()
    actual = __import__("cp2_sequence_actual")
    actual_path = os.path.abspath(getattr(actual, "__file__", ""))
    expected_actual_path = os.path.join(repo_root, "scripts", "cp2", "cp2_sequence_actual.py")
    try:
        actual_status = os.lstat(actual_path)
    except OSError as exc:
        raise SequenceRunnerError("postauthorization actual executor identity is unavailable") from exc
    if (
        actual_path != expected_actual_path
        or not stat.S_ISREG(actual_status.st_mode)
        or actual_status.st_nlink != 1
    ):
        _fail("postauthorization actual executor identity differs")
    authorization.revalidate()
    result = actual.execute_authorized_sequence(
        repo_root=Path(repo_root),
        parsed_cli=dict(parsed_cli),
        authorization=authorization,
        resolved_input=selected[0],
    )
    authorization.revalidate()
    return result


__all__ = [
    "AssemblyResult",
    "GroundTruthPose",
    "ModeRunInput",
    "SequenceAssemblyInput",
    "SequenceRunnerError",
    "SupportFile",
    "assemble_sequence_artifact",
    "build_verify_seal",
    "detached_verifier",
    "make_tree_read_only",
    "run_authorized_sequence",
]
