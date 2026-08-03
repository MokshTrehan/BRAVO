#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Authorized CP2-D execution plumbing.

Importing this module is inert with respect to the dataset registry and every
resolved input.  The public executor is called only after the common readiness
authorization has returned the sole registry byte buffer.  Pure pair-selection
and ground-truth parsing functions provide synthetic-test seams without a bag
provider or filesystem input.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import datetime as dt
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import cp2_recorded_campaign as campaign
import cp2_schema as schema
import cp2_sequence_runner as runner


MAX_GROUND_TRUTH_BYTES = 512 * 1024 * 1024
FROZEN_TOPICS = ("/imu0", "/cam0/image_raw", "/cam1/image_raw")
GROUND_TRUTH_TIMESTAMP = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,9})?")
GROUND_TRUTH_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?(?:0|[1-9][0-9]*))?"
)
PAIR_INDEX_POSTAUTH_ENV = "CP2_POSTAUTH_PAIR_INDEX"
PAIR_INDEX_POSTAUTH_VALUE = "held-readiness-bound-fd-v1"
BAG_IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
SERIAL_PAIR_KEYS = (
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
    "camera_timestamp_ns",
    "absolute_record_delta_ns",
    "selected",
    "enqueue_entered",
    "enqueue_returned",
    "enqueue_status",
    "processing_entered",
    "processing_returned",
    "processing_status",
    "updater_invocation_ids",
)


class SequenceActualError(RuntimeError):
    """Raised when an authorized CP2-D operation cannot remain exact."""


def _fail(message: str) -> None:
    raise SequenceActualError(message)


@dataclass(frozen=True)
class FilteredMessage:
    kind: str
    record_time_ns: int
    header_time_ns: int


def _u64(value: Any, label: str) -> int:
    try:
        return schema.validate_u64(value, label)
    except schema.SchemaError as exc:
        raise SequenceActualError(str(exc)) from exc


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    try:
        return schema.json_line_bytes(value)
    except schema.SchemaError as exc:
        raise SequenceActualError(str(exc)) from exc


def select_pair_index_bytes(
    sequence_index: int,
    sequence_id: str,
    filtered_messages: Sequence[FilteredMessage],
) -> bytes:
    """Apply the frozen first-forward/deduplicating selector to metadata."""

    index = _u64(sequence_index, "pair selector sequence index")
    if index >= len(runner.SEQUENCES) or runner.SEQUENCES[index] != sequence_id:
        _fail("pair selector sequence identity differs from the frozen inventory")
    if not isinstance(filtered_messages, Sequence) or isinstance(
        filtered_messages, (str, bytes, bytearray)
    ):
        _fail("filtered bag metadata is not a sequence")
    normalized: List[FilteredMessage] = []
    for ordinal, row in enumerate(filtered_messages):
        if not isinstance(row, FilteredMessage) or row.kind not in ("imu", "cam0", "cam1"):
            _fail("filtered bag metadata has an invalid message kind")
        record_time = _u64(row.record_time_ns, "filtered record time")
        header_time = _u64(row.header_time_ns, "filtered header time")
        if row.kind == "imu" and header_time != 0:
            _fail("filtered IMU metadata must not carry a camera header timestamp")
        if ordinal > 0 and record_time < normalized[-1].record_time_ns:
            _fail("filtered rosbag record times reverse")
        normalized.append(FilteredMessage(row.kind, record_time, header_time))

    used = set()
    rows = []
    for anchor_index, anchor in enumerate(normalized):
        if anchor.kind not in ("cam0", "cam1") or anchor_index in used:
            continue
        other = "cam1" if anchor.kind == "cam0" else "cam0"
        candidate_index = next(
            (
                ordinal
                for ordinal in range(anchor_index + 1, len(normalized))
                if normalized[ordinal].kind == other
            ),
            None,
        )
        if candidate_index is None or candidate_index in used:
            continue
        candidate = normalized[candidate_index]
        delta = abs(anchor.record_time_ns - candidate.record_time_ns)
        if delta >= runner.STRICT_PAIR_DELTA_NS:
            continue
        cam0_index, cam1_index = (
            (anchor_index, candidate_index)
            if anchor.kind == "cam0"
            else (candidate_index, anchor_index)
        )
        cam0 = normalized[cam0_index]
        cam1 = normalized[cam1_index]
        rows.append(
            {
                "schema_version": 1,
                "record_type": "pair_index",
                "sequence_index": index,
                "sequence_id": sequence_id,
                "pair_index": len(rows),
                "anchor_filtered_index": anchor_index,
                "anchor_camera_id": 0 if anchor.kind == "cam0" else 1,
                "cam0_filtered_index": cam0_index,
                "cam1_filtered_index": cam1_index,
                "cam0_record_time_ns": cam0.record_time_ns,
                "cam1_record_time_ns": cam1.record_time_ns,
                "cam0_header_time_ns": cam0.header_time_ns,
                "cam1_header_time_ns": cam1.header_time_ns,
                "absolute_record_delta_ns": delta,
            }
        )
        used.update((anchor_index, candidate_index))
    if len(rows) < 2:
        _fail("independent pair index has fewer than two selected rows")
    try:
        return schema.jsonl_bytes(rows)
    except schema.SchemaError as exc:
        raise SequenceActualError("cannot encode independent pair index") from exc


def project_serial_pairs_to_pair_index_bytes(
    serial_pair_bytes: bytes,
    sequence_index: int,
    sequence_id: str,
) -> bytes:
    """Project CP2-C serial rows to the exact CP2-D source-key schema.

    A CP2-C campaign can apply this pure function to its retained
    ``serial_pairs.jsonl`` bytes and compare the result byte-for-byte with the
    independent extractor stdout for the same sequence.
    """

    index = _u64(sequence_index, "serial projection sequence index")
    if index >= len(runner.SEQUENCES) or runner.SEQUENCES[index] != sequence_id:
        _fail("serial projection sequence identity differs from the frozen inventory")
    if not isinstance(serial_pair_bytes, bytes):
        _fail("serial projection input is not bytes")
    try:
        decoded = schema.strict_jsonl_loads(serial_pair_bytes)
    except schema.SchemaError as exc:
        raise SequenceActualError("serial projection input is not strict JSONL") from exc
    projected: List[Mapping[str, Any]] = []
    previous: Optional[Tuple[int, int]] = None
    for row in decoded:
        if not isinstance(row, Mapping) or set(row) != set(SERIAL_PAIR_KEYS):
            _fail("serial projection row has the wrong exact key set")
        if row["schema_version"] != 1 or row["record_type"] != "serial_pair":
            _fail("serial projection row has the wrong schema identity")
        row_sequence = _u64(row["sequence_index"], "serial projection row sequence")
        pair_index = _u64(row["pair_index"], "serial projection row pair")
        if (
            row_sequence >= len(runner.SEQUENCES)
            or row["sequence_id"] != runner.SEQUENCES[row_sequence]
        ):
            _fail("serial projection row has an invalid frozen sequence identity")
        order = (row_sequence, pair_index)
        if previous is not None and order <= previous:
            _fail("serial projection rows are duplicate or globally out of order")
        previous = order
        for field in (
            "anchor_filtered_index",
            "anchor_camera_id",
            "cam0_filtered_index",
            "cam1_filtered_index",
            "cam0_record_time_ns",
            "cam1_record_time_ns",
            "cam0_header_time_ns",
            "cam1_header_time_ns",
            "camera_timestamp_ns",
            "absolute_record_delta_ns",
        ):
            _u64(row[field], "serial projection " + field)
        if row["selected"] is not True:
            _fail("serial projection row is not selected")
        for field in (
            "enqueue_entered",
            "enqueue_returned",
            "processing_entered",
            "processing_returned",
        ):
            if not isinstance(row[field], bool):
                _fail("serial projection event flag is not Boolean")
        if not isinstance(row["enqueue_status"], str) or not isinstance(
            row["processing_status"], str
        ):
            _fail("serial projection status is not a string")
        invocation_ids = row["updater_invocation_ids"]
        if not isinstance(invocation_ids, list):
            _fail("serial projection invocation IDs are invalid")
        normalized_invocations = [
            _u64(invocation_id, "serial projection invocation ID")
            for invocation_id in invocation_ids
        ]
        if len(normalized_invocations) != len(set(normalized_invocations)):
            _fail("serial projection invocation IDs are invalid")
        if row_sequence == index:
            source = {key: row[key] for key in runner.PAIR_KEYS}
            source["record_type"] = "pair_index"
            projected.append(source)
    try:
        payload = schema.jsonl_bytes(projected)
        runner._validate_pairs(payload, index, sequence_id)
    except (schema.SchemaError, runner.SequenceRunnerError) as exc:
        raise SequenceActualError("projected serial-pair population is invalid") from exc
    return payload


def parse_ground_truth_tum_bytes(payload: bytes) -> Tuple[runner.GroundTruthPose, ...]:
    """Parse a bounded source-compatible space-delimited trajectory.

    This follows the repository's pre-existing ``DatasetReader`` trajectory
    semantics without binding evidence to any observed dataset header:
    printable-ASCII comment lines beginning with ``#`` are ignored, repeated
    ASCII spaces are ignored, and data rows contain exactly eight fields.
    """

    if not isinstance(payload, bytes) or not payload:
        _fail("ground-truth payload is empty or not bytes")
    if len(payload) > MAX_GROUND_TRUTH_BYTES:
        _fail("ground-truth payload exceeds its byte bound")
    if not payload.endswith(b"\n") or any(
        byte != 0x0A and not 0x20 <= byte <= 0x7E for byte in payload
    ):
        _fail("ground-truth payload is not printable ASCII with LF newlines")
    rows: List[runner.GroundTruthPose] = []
    for row_index, raw in enumerate(payload.splitlines(), 1):
        if not raw or raw.startswith(b"#"):
            continue
        fields = [field.decode("ascii") for field in raw.split(b" ") if field]
        if len(fields) != 8:
            _fail("ground-truth row {} does not contain eight fields".format(row_index))
        if GROUND_TRUTH_TIMESTAMP.fullmatch(fields[0]) is None:
            _fail("ground-truth row {} has a noncanonical timestamp".format(row_index))
        if any(GROUND_TRUTH_NUMBER.fullmatch(field) is None for field in fields[1:]):
            _fail("ground-truth row {} has a noncanonical pose field".format(row_index))
        try:
            timestamp = schema.decimal_seconds_to_ns(
                fields[0], "ground-truth row {} timestamp".format(row_index)
            )
            values = tuple(float(field) for field in fields[1:])
        except (schema.SchemaError, ValueError, OverflowError) as exc:
            raise SequenceActualError("ground-truth row is not exact finite TUM") from exc
        if not all(math.isfinite(value) for value in values):
            _fail("ground-truth row contains a nonfinite pose")
        rows.append(
            runner.GroundTruthPose(
                timestamp_ns=timestamp,
                position=(values[0], values[1], values[2]),
                quaternion_xyzw=(values[3], values[4], values[5], values[6]),
            )
        )
    try:
        return runner._ground_truth(rows)
    except runner.SequenceRunnerError as exc:
        raise SequenceActualError(str(exc)) from exc


def _ros_time_ns(value: Any, label: str) -> int:
    seconds = getattr(value, "secs", None)
    nanoseconds = getattr(value, "nsecs", None)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or isinstance(nanoseconds, bool)
        or not isinstance(nanoseconds, int)
        or seconds < 0
        or not 0 <= nanoseconds < 1_000_000_000
    ):
        _fail(label + " is not an exact nonnegative ROS timestamp")
    try:
        return schema.checked_u64_add(
            schema.checked_u64_multiply(seconds, 1_000_000_000, label),
            nanoseconds,
            label,
        )
    except schema.SchemaError as exc:
        raise SequenceActualError(str(exc)) from exc


def _read_pair_index_with_provider(
    bag_open_path: Path,
    sequence_index: int,
    bag_factory: Callable[..., Any],
) -> bytes:
    if (
        isinstance(sequence_index, bool)
        or not isinstance(sequence_index, int)
        or not 0 <= sequence_index < len(runner.SEQUENCES)
    ):
        _fail("independent bag index sequence is outside the frozen inventory")
    bag = bag_factory(str(Path(bag_open_path)), "r")
    try:
        first = next(iter(bag.read_messages(raw=True)), None)
        if first is None or not isinstance(first, tuple) or len(first) != 3:
            _fail("rosbag is empty or has an invalid message iterator")
        first_time = first[2]
        first_ns = _ros_time_ns(first_time, "rosbag begin time")
        offset_ns = int(runner.OFFSETS_SECONDS[sequence_index]) * 1_000_000_000
        start_ns = schema.checked_u64_add(first_ns, offset_ns, "post-offset bag start")
        start_time = type(first_time)(start_ns // 1_000_000_000, start_ns % 1_000_000_000)
        filtered: List[FilteredMessage] = []
        for topic, message, record_time in bag.read_messages(
            topics=list(FROZEN_TOPICS), start_time=start_time
        ):
            record_ns = _ros_time_ns(record_time, "rosbag record time")
            if topic == FROZEN_TOPICS[0]:
                filtered.append(FilteredMessage("imu", record_ns, 0))
                continue
            if topic not in FROZEN_TOPICS[1:]:
                _fail("topic-filtered rosbag iterator emitted an unknown topic")
            if getattr(message, "_type", None) != "sensor_msgs/Image":
                _fail("camera topic does not contain sensor_msgs/Image")
            header = getattr(message, "header", None)
            stamp = getattr(header, "stamp", None)
            header_ns = _ros_time_ns(stamp, "camera header time")
            filtered.append(
                FilteredMessage("cam0" if topic == FROZEN_TOPICS[1] else "cam1", record_ns, header_ns)
            )
    finally:
        bag.close()
    return select_pair_index_bytes(
        sequence_index, runner.SEQUENCES[sequence_index], tuple(filtered)
    )


def read_pair_index_from_bag(
    bag_path: Path,
    sequence_index: int,
    authorization: Any,
    *,
    bag_factory: Optional[Callable[..., Any]] = None,
) -> bytes:
    """Read only bag metadata/messages needed by the independent selector."""

    if getattr(authorization, "prebag_authorized", False) is not True:
        _fail("independent bag index received no held authorization")
    authorization.revalidate()
    if bag_factory is None:
        try:
            bag_factory = __import__("rosbag").Bag
        except (ImportError, AttributeError) as exc:
            raise SequenceActualError("ROS1 rosbag provider is unavailable") from exc
    payload = _read_pair_index_with_provider(
        Path(bag_path), sequence_index, bag_factory
    )
    authorization.revalidate()
    return payload


def _bag_identity_argument(identity: os.stat_result) -> str:
    values = []
    for field in BAG_IDENTITY_FIELDS:
        value = getattr(identity, field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _fail("bound bag identity is not canonical nonnegative integers")
        values.append(str(value))
    return ":".join(values)


def _run_pair_index_command(
    *,
    repo_root: Path,
    partial: Path,
    build: Mapping[str, Any],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    bound_bag: campaign.BoundRegularFile,
    sequence_index: int,
) -> bytes:
    """Retain the independent index as exact stdout from fresh source."""

    authorization.revalidate()
    bound_bag.revalidate()
    helper = Path(build["source_space"]) / "scripts/cp2/cp2_pair_index_extract.py"
    helper_status = helper.lstat()
    if not stat.S_ISREG(helper_status.st_mode) or helper_status.st_nlink != 1:
        _fail("fresh source lacks the pair-index subprocess helper")
    environment = {
        PAIR_INDEX_POSTAUTH_ENV: PAIR_INDEX_POSTAUTH_VALUE,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
    }
    argv = [
        "/usr/bin/python3",
        "-I",
        "-B",
        str(helper),
        "--sequence-index",
        str(sequence_index),
        "--sequence-id",
        runner.SEQUENCES[sequence_index],
        "--bag-path",
        str(bound_bag.path),
        "--parent-bag-fd",
        str(bound_bag.descriptor),
        "--bag-identity",
        _bag_identity_argument(bound_bag.identity),
    ]
    command = recorder.run(
        "pair_index",
        argv,
        Path(repo_root),
        "pair_index_v1",
        environment,
        sequence_index=sequence_index,
    )
    authorization.revalidate()
    bound_bag.revalidate()
    if command["exit_code"] != 0 or command["timed_out"]:
        _fail("independent pair-index subprocess failed")
    stdout_path = partial / command["stdout"]
    stderr_path = partial / command["stderr"]
    for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("pair-index subprocess " + label + " is unsafe")
        if schema.sha256_file(path) != command[label + "_sha256"]:
            _fail("pair-index subprocess " + label + " digest differs")
    stdout = stdout_path.read_bytes()
    if stderr_path.read_bytes() != b"":
        _fail("successful pair-index subprocess wrote diagnostics")
    try:
        rows = schema.strict_jsonl_loads(stdout)
        if schema.jsonl_bytes(rows) != stdout:
            _fail("pair-index subprocess stdout is not canonical JSONL")
        runner._validate_pairs(
            stdout, sequence_index, runner.SEQUENCES[sequence_index]
        )
    except (schema.SchemaError, runner.SequenceRunnerError) as exc:
        raise SequenceActualError("pair-index subprocess stdout is invalid") from exc
    authorization.revalidate()
    bound_bag.revalidate()
    return stdout


def _held_source_bytes(repository: Any, relative: str) -> bytes:
    repository.revalidate()
    descriptor = repository.duplicate_tracked_fd(relative)
    try:
        content = campaign._read_fd_all(
            descriptor, 64 * 1024 * 1024, "held CP2-D source input"
        )
    finally:
        os.close(descriptor)
    repository.revalidate()
    return content


def _launch_arguments(
    build: Mapping[str, Any],
    bag: Path,
    sequence_index: int,
    mode: str,
    trace: Path,
    context_path: Path,
) -> List[str]:
    return [
        str(Path(build["source_space"]) / "project/cp2_serial.launch"),
        "bag:=" + str(bag),
        "bag_start:=" + format(runner.OFFSETS_SECONDS[sequence_index], ".1f"),
        "config_path:="
        + str(Path(build["source_space"]) / "config/euroc_mav/estimator_config.yaml"),
        "path_state:=" + str(trace / "state.txt"),
        "path_std:=" + str(trace / "deviation.txt"),
        "path_time:=" + str(trace / "openvins_timing.csv"),
        "save_total_state:=true",
        "record_openvins_timing:=true",
        "cp2_trace_directory:=" + str(trace),
        "cp2_context_path:=" + str(context_path),
        "cp2_sequence_id:=" + runner.SEQUENCES[sequence_index],
        "cp2_sequence_index:=" + str(sequence_index),
        "landmark_elimination:=" + mode,
        "cp2_shadow_enabled:=false",
        "cp2_trace_level:=sequence",
        "verbosity:=INFO",
    ]


def _runtime_context(
    *,
    run_id: str,
    sequence_index: int,
    mode: str,
    source_commit: str,
    config_sha256: str,
    bag_sha256: str,
    resolved_sha256: str,
    trace: Path,
) -> Dict[str, Any]:
    path = lambda name: str(trace / name)
    return {
        "schema_version": 1,
        "record_type": "cp2_runtime_context",
        "checkpoint": "CP2-D",
        "run_id": run_id,
        "sequence_index": sequence_index,
        "sequence_id": runner.SEQUENCES[sequence_index],
        "mode": mode,
        "shadow_enabled": False,
        "trace_level": "sequence",
        "source_commit": source_commit,
        "config_sha256": config_sha256,
        "bag_sha256": bag_sha256,
        "pair_index_sha256": None,
        "resolved_parameters_sha256": resolved_sha256,
        "trace_directory": str(trace),
        "serial_trace_path": path("serial.jsonl"),
        "callback_trace_path": path("callbacks.jsonl"),
        "trajectory_trace_path": path("trajectory.jsonl"),
        "updater_trace_path": None,
        "state_payload_path": None,
        "proposal_payload_path": None,
        "raw_system_payload_path": None,
        "timing_trace_path": None,
        "runtime_parameters_path": path("runtime_parameters.yaml"),
        "loader_map_before_path": path("loader_before.txt"),
        "loader_map_after_path": path("loader_after.txt"),
        "legacy_state_path": path("state.txt"),
        "legacy_deviation_path": path("deviation.txt"),
        "legacy_timing_path": path("openvins_timing.csv"),
    }


@dataclass(frozen=True)
class _CapturedMode:
    source: runner.ModeRunInput
    run_id: str
    trace: Path
    context: Path
    loader_before: Path
    loader_after: Path
    dso_records_before: Tuple[Mapping[str, Any], ...]
    dso_records_after: Tuple[Mapping[str, Any], ...]
    transient_paths: Tuple[Path, ...]


def _capture_mode(
    *,
    repo_root: Path,
    partial: Path,
    build: Mapping[str, Any],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    bound_bag: campaign.BoundRegularFile,
    sequence_index: int,
    mode: str,
    run_index: int,
    source_commit: str,
    config_sha256: str,
    pair_index_bytes: bytes,
    artifact_run_id: str,
) -> _CapturedMode:
    authorization.revalidate()
    if mode != runner.MODES[run_index]:
        _fail("mode capture order differs from nullspace then schur")
    trace = partial / "runtime" / mode
    trace.mkdir(mode=0o700, parents=True, exist_ok=False)
    context_path = trace / "context.json"
    launch_args = _launch_arguments(
        build, bound_bag.path, sequence_index, mode, trace, context_path
    )
    port = 15381 + 10 * sequence_index + run_index
    environment_id = "sequence_{}_{}".format(sequence_index, mode)
    environment = campaign._runtime_environment(
        build,
        Path(build["workspace"]) / "sequence-environment-{}-{}".format(sequence_index, mode),
        port,
    )
    dump = recorder.run(
        "runtime_preflight",
        ["/opt/ros/noetic/bin/roslaunch", "--dump-params"] + launch_args,
        repo_root,
        environment_id,
        environment,
        sequence_index=sequence_index,
        run_index=run_index,
    )
    if dump["exit_code"] != 0 or dump["timed_out"]:
        _fail(mode + " roslaunch preflight failed")
    prelaunch_raw = (partial / dump["stdout"]).read_bytes()
    try:
        parameters = campaign._flatten_dump(prelaunch_raw)
        canonical = schema.encode_resolved_parameters(parameters)
    except (campaign.CampaignError, schema.SchemaError) as exc:
        raise SequenceActualError(mode + " prelaunch parameters are invalid") from exc
    prelaunch_path = trace / "prelaunch_parameters.yaml"
    campaign._write_new(prelaunch_path, prelaunch_raw)
    resolved_sha256 = hashlib.sha256(canonical).hexdigest()
    run_id = "{}-{}".format(artifact_run_id, mode)
    schema.validate_safe_id(run_id, "sequence runtime run ID")
    context = _runtime_context(
        run_id=run_id,
        sequence_index=sequence_index,
        mode=mode,
        source_commit=source_commit,
        config_sha256=config_sha256,
        bag_sha256=runner.FROZEN_BAG_SHA256[sequence_index],
        resolved_sha256=resolved_sha256,
        trace=trace,
    )
    campaign._write_new(context_path, _canonical_json(context))
    launch = recorder.run(
        "ros_run",
        ["/opt/ros/noetic/bin/roslaunch", "-p", str(port)] + launch_args,
        repo_root,
        environment_id,
        environment,
        sequence_index=sequence_index,
        run_index=run_index,
    )
    if launch["exit_code"] != 0 or launch["timed_out"]:
        _fail(mode + " ROS sequence run failed")
    authorization.revalidate()
    bound_bag.revalidate()

    paths = {
        "serial": trace / "serial.jsonl",
        "callbacks": trace / "callbacks.jsonl",
        "trajectory": trace / "trajectory.jsonl",
        "runtime_raw": trace / "runtime_parameters.yaml",
        "loader_before": trace / "loader_before.txt",
        "loader_after": trace / "loader_after.txt",
        "state": trace / "state.txt",
        "deviation": trace / "deviation.txt",
        "timing": trace / "openvins_timing.csv",
    }
    for label, path in paths.items():
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail(mode + " runtime sink is missing or unsafe: " + label)
    observed_names = {path.name for path in trace.iterdir()}
    expected_names = {path.name for path in paths.values()} | {
        context_path.name,
        prelaunch_path.name,
    }
    if observed_names != expected_names:
        _fail(mode + " runtime trace directory contains undeclared outputs")
    if paths["serial"].read_bytes() != pair_index_bytes:
        _fail(mode + " runtime pair index differs from the independent bag index")
    runtime_raw = paths["runtime_raw"].read_bytes()
    try:
        runtime_parameters = schema.strict_json_loads(runtime_raw)
        runtime_canonical = schema.encode_resolved_parameters(runtime_parameters)
    except (schema.SchemaError, TypeError, ValueError) as exc:
        raise SequenceActualError(mode + " runtime parameter capture is invalid") from exc
    if not isinstance(runtime_parameters, Mapping) or runtime_canonical != canonical:
        _fail(mode + " runtime/prelaunch resolved parameters differ")
    dso_before = tuple(
        campaign._loader_dso_records(paths["loader_before"], Path(build["executable"]))
    )
    dso_after = tuple(
        campaign._loader_dso_records(paths["loader_after"], Path(build["executable"]))
    )
    if dso_before != dso_after:
        _fail(mode + " runtime DSO identities changed")
    executable_sha256 = schema.sha256_file(build["executable"])
    placeholder_stdout = b"rmse 0\n"
    source = runner.ModeRunInput(
        mode=mode,
        pair_index_bytes=pair_index_bytes,
        callback_bytes=paths["callbacks"].read_bytes(),
        trajectory_bytes=paths["trajectory"].read_bytes(),
        prelaunch_raw_bytes=prelaunch_raw,
        runtime_raw_bytes=runtime_raw,
        prelaunch_parameters=dict(parameters),
        runtime_parameters=dict(runtime_parameters),
        runtime_root=trace,
        executable_sha256=executable_sha256,
        loader_map_sha256=schema.sha256_file(paths["loader_before"]),
        legacy_state_bytes=paths["state"].read_bytes(),
        legacy_deviation_bytes=paths["deviation"].read_bytes(),
        legacy_timing_bytes=paths["timing"].read_bytes(),
        evaluator_stdout_path="placeholder/{}.stdout".format(mode),
        evaluator_stdout_bytes=placeholder_stdout,
        evaluator_stderr_path="placeholder/{}.stderr".format(mode),
        evaluator_stderr_bytes=b"",
        evaluator_ate_m=0.0,
    )
    transient = (
        prelaunch_path,
        paths["serial"],
        paths["callbacks"],
        paths["trajectory"],
        paths["runtime_raw"],
        paths["state"],
        paths["deviation"],
        paths["timing"],
    )
    return _CapturedMode(
        source=source,
        run_id=run_id,
        trace=trace,
        context=context_path,
        loader_before=paths["loader_before"],
        loader_after=paths["loader_after"],
        dso_records_before=dso_before,
        dso_records_after=dso_after,
        transient_paths=transient,
    )


def _run_evaluators(
    *,
    partial: Path,
    captures: Sequence[_CapturedMode],
    ground_truth: Sequence[runner.GroundTruthPose],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    sequence_index: int,
    workspace: Path,
) -> Tuple[Tuple[runner.ModeRunInput, runner.ModeRunInput], Path]:
    pairs = runner._validate_pairs(
        captures[0].source.pair_index_bytes,
        sequence_index,
        runner.SEQUENCES[sequence_index],
    )
    validated = tuple(
        runner._validate_mode(
            capture.source, sequence_index, runner.SEQUENCES[sequence_index], pairs
        )
        for capture in captures
    )
    metric_files, _ = runner._metric_artifacts(
        validated, ground_truth, verify_evaluator=False
    )
    evaluation_root = partial / "evaluation_work"
    evaluation_root.mkdir(mode=0o700, exist_ok=False)
    for name in (
        "ground_truth_shared.tum",
        "nullspace_shared_aligned.tum",
        "schur_shared_aligned.tum",
    ):
        campaign._write_new(evaluation_root / name, metric_files[name])
    environment = {
        "HOME": str(Path(workspace) / "evo-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    Path(environment["HOME"]).mkdir(mode=0o700, exist_ok=False)
    completed = []
    for run_index, capture in enumerate(captures):
        mode = runner.MODES[run_index]
        command = recorder.run(
            "evaluation",
            [
                "evo_ape",
                "tum",
                "ground_truth_shared.tum",
                mode + "_shared_aligned.tum",
                "-r",
                "trans_part",
                "--t_max_diff",
                "0.01",
            ],
            evaluation_root,
            "evaluator_v1",
            environment,
            sequence_index=sequence_index,
            run_index=run_index,
        )
        if command["exit_code"] != 0 or command["timed_out"]:
            _fail(mode + " evo_ape evaluation failed")
        stdout = (partial / command["stdout"]).read_bytes()
        stderr = (partial / command["stderr"]).read_bytes()
        try:
            ate = runner._parse_evaluator_rmse(stdout, mode + " evo_ape")
        except runner.SequenceRunnerError as exc:
            raise SequenceActualError(str(exc)) from exc
        completed.append(
            replace(
                capture.source,
                evaluator_stdout_path=command["stdout"],
                evaluator_stdout_bytes=stdout,
                evaluator_stderr_path=command["stderr"],
                evaluator_stderr_bytes=stderr,
                evaluator_ate_m=ate,
            )
        )
    authorization.revalidate()
    return tuple(completed), evaluation_root  # type: ignore[return-value]


def _copy_static_inputs(
    partial: Path, authorization: Any
) -> Tuple[bytes, Tuple[Mapping[str, Any], ...]]:
    bundle, static_records = campaign.encode_static_bundle(
        authorization.repository.repo_root, authorization.repository
    )
    campaign._write_new(partial / "configuration/static_bundle.bin", bundle)
    for record in static_records:
        relative = record["path"]
        content = _held_source_bytes(authorization.repository, relative)
        if len(content) != record["size"] or hashlib.sha256(content).hexdigest() != record["sha256"]:
            _fail("held static source differs from its frozen bundle record")
        campaign._write_new(partial / relative, content)
    return bundle, tuple(static_records)


def _provenance_base(
    *,
    partial: Path,
    build: Mapping[str, Any],
    captures: Sequence[_CapturedMode],
    modes: Sequence[runner.ModeRunInput],
    bound_bag: campaign.BoundRegularFile,
    bound_ground_truth: campaign.BoundRegularFile,
    sequence_index: int,
    static_records: Sequence[Mapping[str, Any]],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    parsed_cli: Mapping[str, str],
    executable_sha256: str,
    executable_build_id: str,
) -> Dict[str, Any]:
    contracts = [
        {
            "path": relative,
            "sha256": campaign._held_source_hash(authorization.repository, relative),
        }
        for relative in campaign.CONTRACT_INPUTS
    ]
    readiness_entrypoints = authorization.record.get("entrypoints")
    if not isinstance(readiness_entrypoints, list) or len(readiness_entrypoints) != 5:
        _fail("readiness entrypoint provenance is unavailable")
    entrypoints = []
    for record in readiness_entrypoints:
        if not isinstance(record, Mapping) or not all(
            isinstance(record.get(key), str) for key in ("path", "sha256", "git_blob")
        ):
            _fail("readiness entrypoint record differs")
        entrypoints.append(
            {key: record[key] for key in ("path", "sha256", "git_blob")}
        )
    unit_report = authorization.frozen_unit_artifact / "cp2_report.json"
    if not unit_report.is_file() or unit_report.is_symlink():
        _fail("frozen unit artifact lacks cp2_report.json")
    runtime_runs = []
    resolved = []
    contexts = []
    for run_index, (capture, source) in enumerate(zip(captures, modes)):
        mode = runner.MODES[run_index]
        canonical, normalized, _ = runner._validate_parameters(source)
        runtime_runs.append(
            {
                "run_id": capture.run_id,
                "sequence_index": sequence_index,
                "mode": mode,
                "loader_map_before": capture.loader_before.relative_to(partial).as_posix(),
                "loader_map_before_sha256": schema.sha256_file(capture.loader_before),
                "loader_map_after": capture.loader_after.relative_to(partial).as_posix(),
                "loader_map_after_sha256": schema.sha256_file(capture.loader_after),
                "dso_records_before": list(capture.dso_records_before),
                "dso_records_after": list(capture.dso_records_after),
            }
        )
        resolved.append(
            {
                "run_id": capture.run_id,
                "prelaunch_raw_path": "parameters/{}_prelaunch_raw.yaml".format(mode),
                "prelaunch_raw_sha256": hashlib.sha256(source.prelaunch_raw_bytes).hexdigest(),
                "runtime_raw_path": "parameters/{}_runtime_raw.yaml".format(mode),
                "runtime_raw_sha256": hashlib.sha256(source.runtime_raw_bytes).hexdigest(),
                "canonical_path": "parameters/{}_canonical.bin".format(mode),
                "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
                "normalized_path": "parameters/{}_normalized.bin".format(mode),
                "normalized_sha256": hashlib.sha256(normalized).hexdigest(),
            }
        )
        context_status = capture.context.lstat()
        contexts.append(
            {
                "run_id": capture.run_id,
                "path": capture.context.relative_to(partial).as_posix(),
                "size": context_status.st_size,
                "sha256": schema.sha256_file(capture.context),
            }
        )
    executable = Path(build["executable"]).resolve(strict=True)
    executable_status = executable.lstat()
    environments = sorted(
        recorder.environments.values(),
        key=lambda item: item["environment_id"].encode("utf-8"),
    )
    return {
        "schema_version": 1,
        "record_type": "provenance",
        "checkpoint": "CP2-D",
        "evidence_class": "trusted_runner_local_staging_evidence",
        "distribution_status": "internal_non_conveyable_staging",
        "eligible_for_cp2_seal": False,
        "created_utc": campaign._utc_now(),
        "branch": "schurvio-lite/cp2-one-pass",
        "source_commit": authorization.repository.commit,
        "source_tree": authorization.repository.tree,
        "source_archive": "source_snapshot.tar",
        "source_archive_sha256": schema.sha256_file(partial / "source_snapshot.tar"),
        "clean": True,
        "cp1_authorization_commit": runner.CP1_AUTHORIZATION_COMMIT,
        "contracts": contracts,
        "entrypoints": entrypoints,
        "readiness_barrier": "readiness/barrier.json",
        "unit_anchor": {
            "artifact": str(Path(parsed_cli["--unit-artifact"]).absolute()),
            "manifest_sha256": parsed_cli["--unit-manifest-sha256"],
            "tested_commit": authorization.record["unit_tested_commit"],
            "tested_tree": authorization.record["unit_tested_tree"],
            "report_sha256": schema.sha256_file(unit_report),
            "verified": True,
        },
        "build": {
            "fresh_git_archive": True,
            "workspace": str(build["workspace"]),
            "commands_sha256": hashlib.sha256(campaign._commands_bytes(recorder)).hexdigest(),
            "compile_commands": "compile_commands.json",
            "compile_commands_sha256": schema.sha256_file(build["compile_commands"]),
            "cmake_cache": "CMakeCache.txt",
            "cmake_cache_sha256": schema.sha256_file(build["cmake_cache"]),
            "strict_fp_verified": True,
        },
        "readiness_barrier_sha256": schema.sha256_file(partial / "readiness/barrier.json"),
        "runtime": {
            "executable": str(executable),
            "executable_size": executable_status.st_size,
            "executable_sha256_before": executable_sha256,
            "executable_sha256_after": executable_sha256,
            "build_id_before": executable_build_id,
            "build_id_after": executable_build_id,
            "runs": runtime_runs,
        },
        "configuration": {
            "static_files": [dict(record) for record in static_records[:3]],
            "static_bundle_payload": "configuration/static_bundle.bin",
            "static_bundle_sha256": schema.sha256_file(
                partial / "configuration/static_bundle.bin"
            ),
            "launch": dict(static_records[3]),
            "resolved_parameters": resolved,
            "runtime_contexts": contexts,
        },
        "inputs": [
            {
                "sequence_index": sequence_index,
                "sequence_id": runner.SEQUENCES[sequence_index],
                "offset_seconds": runner.OFFSETS_SECONDS[sequence_index],
                "bag_path": str(bound_bag.path),
                "bag_size": bound_bag.identity.st_size,
                "bag_sha256_before": runner.FROZEN_BAG_SHA256[sequence_index],
                "bag_sha256_after": runner.FROZEN_BAG_SHA256[sequence_index],
                "ground_truth_path": str(bound_ground_truth.path),
                "ground_truth_sha256": runner.FROZEN_GROUND_TRUTH_SHA256[sequence_index],
            }
        ],
        "environment": {"classes": environments},
        "host": campaign._host_record(build),
    }


def _support_files(
    partial: Path,
    authorization: Any,
    build: Mapping[str, Any],
    captures: Sequence[_CapturedMode],
    recorder: campaign.CommandRecorder,
) -> Dict[str, runner.SupportFile]:
    result: Dict[str, runner.SupportFile] = {}

    def add(relative: str, role: str) -> None:
        schema.validate_relpath(relative, "support path")
        path = partial / relative
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("support path is not a single-link regular file: " + relative)
        result[relative] = runner.SupportFile(path.read_bytes(), role)

    for relative in authorization.attachments:
        add(relative, "readiness")
    add("readiness/barrier.json", "readiness")
    add("source_snapshot.tar", "source")
    add("compile_commands.json", "build")
    add("CMakeCache.txt", "build")
    add("configuration/static_bundle.bin", "configuration")
    for relative in campaign.STATIC_PATHS:
        add(relative, "configuration")
    for capture in captures:
        add(capture.context.relative_to(partial).as_posix(), "configuration")
        add(capture.loader_before.relative_to(partial).as_posix(), "log")
        add(capture.loader_after.relative_to(partial).as_posix(), "log")
    evaluation_streams = {
        record[stream]
        for record in recorder.records
        if record["phase"] == "evaluation"
        for stream in ("stdout", "stderr")
    }
    for record in recorder.records:
        for stream in ("stdout", "stderr"):
            relative = record[stream]
            add(relative, "evaluator" if relative in evaluation_streams else "log")
    return result


def _remove_transient_inputs(
    captures: Sequence[_CapturedMode], evaluation_root: Path
) -> None:
    for capture in captures:
        for path in capture.transient_paths:
            status_value = path.lstat()
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                _fail("transient runtime input changed before assembly")
            path.unlink()
    for path in sorted(evaluation_root.iterdir(), key=lambda item: os.fsencode(item.name)):
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("evaluator work directory contains a nonregular input")
        path.unlink()
    evaluation_root.rmdir()


def _retain_failure(
    partial: Path,
    parent: Path,
    error: BaseException,
    source_commit: str,
    source_tree: str,
) -> None:
    try:
        if not os.path.lexists(str(partial)):
            return
        status_value = partial.lstat()
        if not stat.S_ISDIR(status_value.st_mode) or stat.S_ISLNK(status_value.st_mode):
            return
        os.chmod(str(partial), 0o700)
        failure = partial / "failure.json"
        if not os.path.lexists(str(failure)):
            campaign._write_new(
                failure,
                _canonical_json(
                    {
                        "schema_version": 1,
                        "record_type": "cp2_failure",
                        "checkpoint": "CP2-D",
                        "created_utc": campaign._utc_now(),
                        "source_commit": source_commit,
                        "source_tree": source_tree,
                        "error_type": type(error).__name__,
                        "error": str(error),
                        "recorded_input_authorized": True,
                    }
                ),
            )
        campaign._freeze_existing_files(partial)
        campaign._seal_directories(partial)
        campaign._fsync_directory(parent)
    except BaseException:
        pass


def execute_authorized_sequence(
    *,
    repo_root: Path,
    parsed_cli: Mapping[str, str],
    authorization: Any,
    resolved_input: Any,
) -> Mapping[str, str]:
    """Run one fully authorized nullspace/Schur CP2-D sequence transaction."""

    if getattr(authorization, "prebag_authorized", False) is not True:
        _fail("CP2-D executor received no held readiness authorization")
    root = Path(repo_root).absolute()
    sequence_id = parsed_cli.get("--sequence")
    if sequence_id not in runner.SEQUENCES:
        _fail("CP2-D executor sequence is outside the frozen inventory")
    sequence_index = runner.SEQUENCES.index(sequence_id)
    expected_fields = (
        resolved_input.sequence_index,
        resolved_input.sequence_id,
        resolved_input.offset_seconds,
        resolved_input.bag_sha256,
        resolved_input.ground_truth_sha256,
    )
    if expected_fields != (
        sequence_index,
        sequence_id,
        runner.OFFSETS_SECONDS[sequence_index],
        runner.FROZEN_BAG_SHA256[sequence_index],
        runner.FROZEN_GROUND_TRUTH_SHA256[sequence_index],
    ):
        _fail("resolved CP2-D input differs from the frozen sequence identity")
    source_commit = authorization.repository.commit
    source_tree = authorization.repository.tree
    if campaign.HEX40.fullmatch(source_commit or "") is None or campaign.HEX40.fullmatch(source_tree or "") is None:
        _fail("readiness source identity is invalid")
    unit_artifact = Path(authorization.frozen_unit_artifact).absolute()
    if unit_artifact.parent != authorization.temporary_root:
        _fail("frozen unit artifact is outside the readiness capability root")
    run_id = parsed_cli.get("--run-id")
    if run_id is None:
        run_id = "cp2d_{}_{}-g{}".format(
            sequence_id,
            dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
            source_commit[:12],
        )
    schema.validate_safe_id(run_id, "sequence artifact run ID")
    parent = root / "results/staging/cp2/sequence"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    final = parent / run_id
    if os.path.lexists(str(final)):
        _fail("sequence final destination already exists")
    partial = Path(tempfile.mkdtemp(prefix="." + run_id + ".partial.", dir=str(parent)))
    os.chmod(str(partial), 0o700)
    bound_bag: Optional[campaign.BoundRegularFile] = None
    bound_ground_truth: Optional[campaign.BoundRegularFile] = None
    try:
        authorization.revalidate()
        bound_bag = campaign._bind_regular_file(Path(resolved_input.bag_path))
        bound_ground_truth = campaign._bind_regular_file(Path(resolved_input.ground_truth_path))
        if bound_bag.sha256() != runner.FROZEN_BAG_SHA256[sequence_index]:
            _fail("bound bag SHA-256 differs from the frozen sequence")
        ground_truth_bytes = campaign._read_fd_all(
            bound_ground_truth.descriptor,
            MAX_GROUND_TRUTH_BYTES,
            "bound CP2-D ground truth",
        )
        bound_ground_truth.revalidate()
        if hashlib.sha256(ground_truth_bytes).hexdigest() != runner.FROZEN_GROUND_TRUTH_SHA256[sequence_index]:
            _fail("bound ground-truth SHA-256 differs from the frozen sequence")
        ground_truth = parse_ground_truth_tum_bytes(ground_truth_bytes)
        authorization.revalidate()

        campaign._copy_readiness(partial, authorization)
        recorder = campaign.CommandRecorder(partial, authorization)
        recorder.import_readiness_unit_verification()
        readiness_environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CP2_SELF_TEST": "1",
            "CP2_FORBID_BAG_ACCESS": "1",
        }
        readiness_digest = recorder.add_environment(
            "readiness_self_test_v1", readiness_environment
        )
        if any(
            record.get("environment_sha256") != readiness_digest
            for record in authorization.record.get("self_tests", [])
        ):
            _fail("readiness self-test environment does not join D provenance")
        git_environment_record = authorization.record.get("readiness_git_environment")
        if not isinstance(git_environment_record, Mapping):
            _fail("readiness Git environment record is unavailable")
        git_variables = {
            item["name"]: item["value"]
            for item in git_environment_record.get("variables", [])
            if isinstance(item, Mapping) and set(item) == {"name", "value"}
        }
        git_digest = recorder.add_environment("readiness_git_v1", git_variables)
        if (
            git_environment_record.get("environment_id") != "readiness_git_v1"
            or git_environment_record.get("canonical_sha256") != git_digest
            or any(
                record.get("environment_sha256") != git_digest
                for record in authorization.record.get("readiness_git_commands", [])
            )
        ):
            _fail("readiness Git environment/command binding differs")
        bundle, static_records = _copy_static_inputs(partial, authorization)
        config_sha256 = hashlib.sha256(bundle).hexdigest()
        build = campaign._build_runtime(
            root,
            partial,
            unit_artifact,
            authorization,
            recorder,
            source_commit,
        )
        executable_before = schema.sha256_file(build["executable"])
        executable_build_id, executable_soname = campaign._elf_identity(
            Path(build["executable"]).absolute(), require_soname=False
        )
        if executable_soname is not None:
            _fail("runtime executable unexpectedly declares a DSO SONAME")
        pair_index_bytes = _run_pair_index_command(
            repo_root=root,
            partial=partial,
            build=build,
            recorder=recorder,
            authorization=authorization,
            bound_bag=bound_bag,
            sequence_index=sequence_index,
        )
        captures = tuple(
            _capture_mode(
                repo_root=root,
                partial=partial,
                build=build,
                recorder=recorder,
                authorization=authorization,
                bound_bag=bound_bag,
                sequence_index=sequence_index,
                mode=mode,
                run_index=run_index,
                source_commit=source_commit,
                config_sha256=config_sha256,
                pair_index_bytes=pair_index_bytes,
                artifact_run_id=run_id,
            )
            for run_index, mode in enumerate(runner.MODES)
        )
        if bound_bag.sha256() != runner.FROZEN_BAG_SHA256[sequence_index]:
            _fail("bound bag SHA-256 changed across the two mode runs")
        bound_ground_truth.revalidate()
        if bound_ground_truth.sha256() != runner.FROZEN_GROUND_TRUTH_SHA256[sequence_index]:
            _fail("bound ground-truth SHA-256 changed across the two mode runs")
        modes, evaluation_root = _run_evaluators(
            partial=partial,
            captures=captures,
            ground_truth=ground_truth,
            recorder=recorder,
            authorization=authorization,
            sequence_index=sequence_index,
            workspace=Path(build["workspace"]),
        )
        executable_after = schema.sha256_file(build["executable"])
        build_id_after, _ = campaign._elf_identity(
            Path(build["executable"]).absolute(), require_soname=False
        )
        if executable_after != executable_before or build_id_after != executable_build_id:
            _fail("runtime executable identity changed across CP2-D")
        command_bytes = campaign._commands_bytes(recorder)
        provenance = _provenance_base(
            partial=partial,
            build=build,
            captures=captures,
            modes=modes,
            bound_bag=bound_bag,
            bound_ground_truth=bound_ground_truth,
            sequence_index=sequence_index,
            static_records=static_records,
            recorder=recorder,
            authorization=authorization,
            parsed_cli=parsed_cli,
            executable_sha256=executable_before,
            executable_build_id=executable_build_id,
        )
        support = _support_files(partial, authorization, build, captures, recorder)
        evidence = runner.SequenceAssemblyInput(
            sequence_index=sequence_index,
            sequence_id=sequence_id,
            offset_seconds=runner.OFFSETS_SECONDS[sequence_index],
            run_id=run_id,
            modes=modes,
            ground_truth=tuple(ground_truth),
            provenance_without_inventory=provenance,
            commands_bytes=command_bytes,
            support_files=support,
        )
        result = runner.assemble_sequence_artifact(
            partial,
            evidence,
            prepopulated_support=True,
            after_validation=lambda: _remove_transient_inputs(captures, evaluation_root),
        )
        runner.make_tree_read_only(partial)
        before = runner._snapshot_tree(partial)
        authorization.revalidate()
        verification = runner.detached_verifier(Path(build["source_space"]))(
            partial, result.manifest_sha256
        )
        authorization.revalidate()
        if not isinstance(verification, Mapping) or verification.get("passed") is not True:
            _fail("detached CP2-D verifier did not pass")
        if runner._snapshot_tree(partial) != before:
            _fail("artifact changed during detached verification")
        campaign._rename_noreplace(partial, final)
        return {"artifact": str(final), "manifest_sha256": result.manifest_sha256}
    except BaseException as exc:
        _retain_failure(partial, parent, exc, source_commit, source_tree)
        raise
    finally:
        if bound_ground_truth is not None:
            bound_ground_truth.close()
        if bound_bag is not None:
            bound_bag.close()


__all__ = [
    "execute_authorized_sequence",
    "FilteredMessage",
    "PAIR_INDEX_POSTAUTH_ENV",
    "PAIR_INDEX_POSTAUTH_VALUE",
    "SequenceActualError",
    "parse_ground_truth_tum_bytes",
    "project_serial_pairs_to_pair_index_bytes",
    "read_pair_index_from_bag",
    "select_pair_index_bytes",
]
