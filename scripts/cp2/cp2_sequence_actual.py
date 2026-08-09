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
import fcntl
import hashlib
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import cp2_recorded_campaign as campaign
import cp2_capsule as capsule
import cp2_schema as schema
import cp2_evaluator_result as evaluator_result
import cp2_sequence_runner as runner


MAX_GROUND_TRUTH_BYTES = 512 * 1024 * 1024
MAX_PAIR_WITNESS_BYTES = 4 * 1024 * 1024 * 1024
MAX_RUNTIME_TRACE_BYTES = 4 * 1024 * 1024 * 1024
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
SINK_CAPABILITY_FIELDS = (
    "serial_trace",
    "callback_trace",
    "trajectory_trace",
    "updater_trace",
    "state_payload",
    "proposal_payload",
    "raw_system_payload",
    "timing_trace",
    "runtime_parameters",
    "loader_map_before",
    "loader_map_after",
    "legacy_state",
    "legacy_deviation",
    "legacy_timing",
)
D_SINK_NAMES = {
    "serial_trace": "serial.jsonl",
    "callback_trace": "callbacks.jsonl",
    "trajectory_trace": "trajectory.jsonl",
    "updater_trace": None,
    "state_payload": None,
    "proposal_payload": None,
    "raw_system_payload": None,
    "timing_trace": None,
    "runtime_parameters": "runtime_parameters.yaml",
    "loader_map_before": "loader_before.txt",
    "loader_map_after": "loader_after.txt",
    "legacy_state": "state.txt",
    "legacy_deviation": "deviation.txt",
    "legacy_timing": "openvins_timing.csv",
}
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


@dataclass(frozen=True)
class _DirectMathRun:
    request_path: Path
    response_path: Path
    request_bytes: bytes
    response_bytes: bytes
    launcher_path: str
    stdout_path: str
    stdout_bytes: bytes
    stderr_path: str
    stderr_bytes: bytes
    work_root: Path


def _fail(message: str) -> None:
    raise SequenceActualError(message)


def _identity(value: os.stat_result) -> Tuple[int, ...]:
    return tuple(getattr(value, field) for field in BAG_IDENTITY_FIELDS)


def _require_read_only(descriptor: int, label: str) -> None:
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError as exc:
        raise SequenceActualError(label + " access mode cannot be read") from exc
    if flags & os.O_ACCMODE != os.O_RDONLY:
        _fail(label + " capability is writable")


def _sha256_fd(descriptor: int, maximum: int, label: str) -> str:
    return hashlib.sha256(
        campaign._read_fd_all(descriptor, maximum, label)
    ).hexdigest()


class _HeldSourceExecutable:
    """One exact fresh-source inode executed through the parent's /proc FD."""

    def __init__(self, path: Path, expected_bytes: bytes) -> None:
        self.path = Path(path).absolute()
        self.descriptor = -1
        try:
            self.descriptor = os.open(
                str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            _require_read_only(self.descriptor, "pair-index helper")
            self.identity = os.fstat(self.descriptor)
            by_path = self.path.lstat()
            if (
                _identity(self.identity) != _identity(by_path)
                or not stat.S_ISREG(self.identity.st_mode)
                or self.identity.st_nlink != 1
                or self.identity.st_uid != os.geteuid()
            ):
                _fail("fresh pair-index helper inode/path binding differs")
            observed = campaign._read_fd_all(
                self.descriptor, 16 * 1024 * 1024, "held pair-index helper"
            )
            if observed != expected_bytes:
                _fail("fresh pair-index helper differs from held source bytes")
            self.sha256 = hashlib.sha256(observed).hexdigest()
            self.proc_path = "/proc/{}/fd/{}".format(
                os.getpid(), self.descriptor
            )
            if _identity(os.stat(self.proc_path)) != _identity(self.identity):
                _fail("pair-index helper /proc capability differs")
        except BaseException:
            self.close()
            raise

    def revalidate(self) -> None:
        if self.descriptor < 0:
            _fail("pair-index helper capability is closed")
        _require_read_only(self.descriptor, "pair-index helper")
        if (
            _identity(os.fstat(self.descriptor)) != _identity(self.identity)
            or _identity(self.path.lstat()) != _identity(self.identity)
            or _identity(os.stat(self.proc_path)) != _identity(self.identity)
            or _sha256_fd(
                self.descriptor, 16 * 1024 * 1024, "held pair-index helper"
            )
            != self.sha256
        ):
            _fail("held pair-index helper changed across execution")

    def close(self) -> None:
        if getattr(self, "descriptor", -1) >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


class _HeldRuntimeTrace:
    """Parent-created, read-only held capabilities for one runtime trace."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self.directory_fd = os.open(
            str(self.path),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        _require_read_only(self.directory_fd, "runtime trace directory")
        self._directory_identity = os.fstat(self.directory_fd)
        self._files: Dict[
            str, Tuple[int, os.stat_result, os.stat_result, Optional[bytes]]
        ] = {}
        self._revalidate_directory()

    def _revalidate_directory(self) -> None:
        current = os.fstat(self.directory_fd)
        by_path = self.path.lstat()
        expected = self._directory_identity
        for observed in (current, by_path):
            if (
                (observed.st_dev, observed.st_ino)
                != (expected.st_dev, expected.st_ino)
                or not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o700
            ):
                _fail("runtime trace directory capability changed")

    def precreate(self, names: Sequence[str]) -> None:
        """Create every sink once and retain a nonwritable FD to its inode."""

        self._revalidate_directory()
        ordered = tuple(names)
        if len(set(ordered)) != len(ordered) or os.listdir(self.directory_fd):
            _fail("runtime trace precreation inventory differs")
        try:
            for name in ordered:
                if not name or "/" in name or name in (".", ".."):
                    _fail("runtime trace member name is invalid")
                creator = -1
                held = -1
                try:
                    creator = os.open(
                        name,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=self.directory_fd,
                    )
                    initial = os.fstat(creator)
                    if (
                        not stat.S_ISREG(initial.st_mode)
                        or initial.st_nlink != 1
                        or initial.st_uid != os.geteuid()
                        or stat.S_IMODE(initial.st_mode) != 0o600
                        or initial.st_size != 0
                    ):
                        _fail("new runtime sink identity differs: " + name)
                    # Open the still-held creator inode, not its mutable name,
                    # to derive the read-only lifetime capability.
                    held = os.open(
                        "/proc/self/fd/{}".format(creator),
                        os.O_RDONLY | os.O_CLOEXEC,
                    )
                    _require_read_only(held, "runtime trace " + name)
                    held_status = os.fstat(held)
                    by_name = os.stat(
                        name, dir_fd=self.directory_fd, follow_symlinks=False
                    )
                    if (
                        _identity(held_status) != _identity(initial)
                        or _identity(by_name) != _identity(initial)
                    ):
                        _fail("new runtime sink held/name binding differs: " + name)
                    self._files[name] = (held, initial, initial, None)
                    held = -1
                finally:
                    if creator >= 0:
                        os.close(creator)
                    if held >= 0:
                        os.close(held)
            os.fsync(self.directory_fd)
            self.revalidate()
        except BaseException:
            self.close()
            raise

    def proc_path(self, name: str) -> Path:
        descriptor, _initial, _current, _payload = self._member(name)
        capability = Path(
            "/proc/{}/fd/{}".format(os.getpid(), descriptor)
        )
        if _identity(os.stat(str(capability))) != _identity(os.fstat(descriptor)):
            _fail("runtime sink /proc capability differs: " + name)
        return capability

    def capability(self, name: str) -> str:
        descriptor, initial, _current, payload = self._member(name)
        if payload is not None:
            _fail("runtime sink capability was requested after fill: " + name)
        return "v1:{}:{}:{}".format(
            os.getpid(), descriptor, _bag_identity_argument(initial)
        )

    def _member(
        self, name: str
    ) -> Tuple[int, os.stat_result, os.stat_result, Optional[bytes]]:
        if name not in self._files:
            _fail("runtime trace member is not held: " + name)
        return self._files[name]

    def _revalidate_file(self, name: str) -> None:
        descriptor, _initial, expected, payload = self._member(name)
        _require_read_only(descriptor, "runtime trace " + name)
        current = os.fstat(descriptor)
        by_name = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        if _identity(current) != _identity(expected) or _identity(by_name) != _identity(expected):
            _fail("held runtime trace member changed: " + name)
        if payload is not None and campaign._read_fd_all(
            descriptor, MAX_RUNTIME_TRACE_BYTES, "held runtime trace " + name
        ) != payload:
            _fail("held runtime trace bytes changed: " + name)

    def revalidate(self) -> None:
        self._revalidate_directory()
        if set(os.listdir(self.directory_fd)) != set(self._files):
            _fail("runtime trace directory name inventory changed")
        for name in sorted(self._files, key=os.fsencode):
            self._revalidate_file(name)

    def fill_parent(self, name: str, payload: bytes) -> None:
        """Fill one Python-owned sink through the exact held capability."""

        descriptor, initial, _current, prior = self._member(name)
        if prior is not None or not isinstance(payload, bytes) or not payload:
            _fail("parent runtime sink fill is invalid: " + name)
        self._revalidate_file(name)
        writer = -1
        try:
            writer = os.open(str(self.proc_path(name)), os.O_WRONLY | os.O_CLOEXEC)
            if _identity(os.fstat(writer)) != _identity(initial):
                _fail("parent runtime sink writer differs: " + name)
            view = memoryview(payload)
            while view:
                count = os.write(writer, view)
                if count <= 0:
                    _fail("parent runtime sink write made no progress: " + name)
                view = view[count:]
            os.fchmod(writer, 0o444)
            os.fsync(writer)
        finally:
            if writer >= 0:
                os.close(writer)
        os.fsync(self.directory_fd)
        self._accept_completed(name, payload)

    def _accept_completed(self, name: str, expected_payload: Optional[bytes]) -> bytes:
        descriptor, initial, _current, prior = self._member(name)
        if prior is not None:
            _fail("runtime sink was completed twice: " + name)
        current = os.fstat(descriptor)
        by_name = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        if (
            (current.st_dev, current.st_ino) != (initial.st_dev, initial.st_ino)
            or _identity(current) != _identity(by_name)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
            or current.st_size == 0
        ):
            _fail("completed runtime sink identity differs: " + name)
        if stat.S_IMODE(current.st_mode) != 0o444:
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            current = os.fstat(descriptor)
            by_name = os.stat(name, dir_fd=self.directory_fd, follow_symlinks=False)
        payload = campaign._read_fd_all(
            descriptor, MAX_RUNTIME_TRACE_BYTES, "held runtime trace " + name
        )
        if (
            _identity(os.fstat(descriptor)) != _identity(current)
            or _identity(by_name) != _identity(current)
            or stat.S_IMODE(current.st_mode) != 0o444
            or (expected_payload is not None and payload != expected_payload)
        ):
            _fail("completed runtime sink bytes/path differ: " + name)
        self._files[name] = (descriptor, initial, current, payload)
        return payload

    def accept_external(self, names: Sequence[str]) -> None:
        """Accept only exact, nonempty inodes filled by the estimator."""

        self._revalidate_directory()
        if set(os.listdir(self.directory_fd)) != set(self._files):
            _fail("runtime trace directory contains undeclared outputs")
        for name in names:
            self._accept_completed(name, None)
        self.revalidate()

    def payload(self, name: str) -> bytes:
        self._revalidate_file(name)
        payload = self._member(name)[3]
        if payload is None:
            _fail("runtime trace member is not complete: " + name)
        return payload

    def unlink_exact(self, names: Sequence[str]) -> None:
        self.revalidate()
        for name in names:
            self._revalidate_file(name)
            descriptor = self._member(name)[0]
            os.unlink(name, dir_fd=self.directory_fd)
            if os.fstat(descriptor).st_nlink != 0:
                _fail("runtime trace unlink did not remove the held name: " + name)
            os.close(descriptor)
            del self._files[name]
        os.fsync(self.directory_fd)
        self.revalidate()

    def close(self) -> None:
        for descriptor, _initial, _current, _payload in self._files.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._files.clear()
        if getattr(self, "directory_fd", -1) >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1


@dataclass(frozen=True)
class FilteredMessage:
    kind: str
    record_time_ns: int
    header_time_ns: int


@dataclass(frozen=True)
class PairSelectionEvidence:
    """Canonical selector output plus its complete chronological input view."""

    pair_index_bytes: bytes
    witness_bytes: bytes


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


def select_pair_evidence(
    sequence_index: int,
    sequence_id: str,
    filtered_messages: Sequence[FilteredMessage],
) -> PairSelectionEvidence:
    """Apply the selector and retain every filtered message needed to replay it.

    The witness is deliberately the complete chronological view returned for
    the three frozen topics, including IMU rows.  A detached consumer can thus
    replay the exact anchor walk, identify the *first* later opposite-camera
    row, and prove the no-search-past and no-reuse rules without trusting the
    selected-pair producer.
    """

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

    witness_rows = []
    for ordinal, row in enumerate(normalized):
        witness_rows.append(
            {
                "schema_version": 1,
                "record_type": "filtered_message",
                "sequence_index": index,
                "sequence_id": sequence_id,
                "filtered_index": ordinal,
                "kind": row.kind,
                "camera_id": 0 if row.kind == "cam0" else (1 if row.kind == "cam1" else None),
                "record_time_ns": row.record_time_ns,
                "header_time_ns": row.header_time_ns,
            }
        )

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
    if rows[-1]["cam0_record_time_ns"] <= rows[0]["cam0_record_time_ns"]:
        _fail("independent pair index has no positive cam0 record duration")
    try:
        pair_bytes = schema.jsonl_bytes(rows)
        witness_bytes = schema.jsonl_bytes(witness_rows)
    except schema.SchemaError as exc:
        raise SequenceActualError("cannot encode independent pair evidence") from exc
    try:
        runner.validate_pair_selection_witness(
            witness_bytes, pair_bytes, index, sequence_id
        )
    except runner.SequenceRunnerError as exc:
        raise SequenceActualError("generated pair witness does not replay: " + str(exc)) from exc
    return PairSelectionEvidence(pair_bytes, witness_bytes)


def select_pair_index_bytes(
    sequence_index: int,
    sequence_id: str,
    filtered_messages: Sequence[FilteredMessage],
) -> bytes:
    """Compatibility projection of :func:`select_pair_evidence`."""

    return select_pair_evidence(
        sequence_index, sequence_id, filtered_messages
    ).pair_index_bytes


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

    try:
        return campaign._project_serial_pairs_to_pair_index_bytes(
            serial_pair_bytes, sequence_index, sequence_id
        )
    except campaign.CampaignError as exc:
        raise SequenceActualError(str(exc)) from exc


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


def _read_pair_evidence_with_provider(
    bag_open_path: Path,
    sequence_index: int,
    bag_factory: Callable[..., Any],
) -> PairSelectionEvidence:
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
    return select_pair_evidence(
        sequence_index, runner.SEQUENCES[sequence_index], tuple(filtered)
    )


def _read_pair_index_with_provider(
    bag_open_path: Path,
    sequence_index: int,
    bag_factory: Callable[..., Any],
) -> bytes:
    """Compatibility projection used by the existing CP2-C selector seam."""

    return _read_pair_evidence_with_provider(
        bag_open_path, sequence_index, bag_factory
    ).pair_index_bytes


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


def _held_bag_proc_path(bound_bag: campaign.BoundRegularFile) -> Path:
    """Return a fail-closed, parent-held, read-only bag capability path."""

    if not isinstance(bound_bag, campaign.BoundRegularFile):
        _fail("runtime bag capability has the wrong type")
    bound_bag.revalidate()
    _require_read_only(bound_bag.descriptor, "runtime bag")
    capability = Path(
        "/proc/{}/fd/{}".format(os.getpid(), bound_bag.descriptor)
    )
    try:
        observed = os.stat(str(capability))
    except OSError as exc:
        raise SequenceActualError("runtime bag /proc capability is unavailable") from exc
    if _identity(observed) != _identity(bound_bag.identity):
        _fail("runtime bag /proc capability differs from the held inode")
    return capability


def _run_pair_index_command(
    *,
    repo_root: Path,
    partial: Path,
    build: Mapping[str, Any],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    bound_bag: campaign.BoundRegularFile,
    sequence_index: int,
) -> PairSelectionEvidence:
    """Retain exact pair stdout and the complete chronological replay witness."""

    authorization.revalidate()
    bound_bag.revalidate()
    witness_path = partial / runner.PAIR_WITNESS_PATH
    if os.path.lexists(str(witness_path)):
        _fail("pair-selection witness destination already exists")
    helper = Path(build["source_space"]) / "scripts/cp2/cp2_pair_index_extract.py"
    helper_binding = _HeldSourceExecutable(
        helper,
        _held_source_bytes(
            authorization.repository,
            "scripts/cp2/cp2_pair_index_extract.py",
        ),
    )
    parent_fd = -1
    witness_fd = -1
    completed = False
    cleanup_error: Optional[str] = None
    try:
        parent_fd = os.open(
            str(partial),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        parent_before = os.fstat(parent_fd)
        parent_by_path = partial.lstat()
        if (
            tuple(getattr(parent_before, field) for field in BAG_IDENTITY_FIELDS)
            != tuple(getattr(parent_by_path, field) for field in BAG_IDENTITY_FIELDS)
            or not stat.S_ISDIR(parent_before.st_mode)
            or parent_before.st_uid != os.geteuid()
            or stat.S_IMODE(parent_before.st_mode) != 0o700
        ):
            _fail("pair-selection output parent identity differs")
        witness_fd = os.open(
            witness_path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        witness_initial = os.fstat(witness_fd)
        witness_by_name = os.stat(
            witness_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            tuple(getattr(witness_initial, field) for field in BAG_IDENTITY_FIELDS)
            != tuple(getattr(witness_by_name, field) for field in BAG_IDENTITY_FIELDS)
            or not stat.S_ISREG(witness_initial.st_mode)
            or witness_initial.st_nlink != 1
            or witness_initial.st_uid != os.geteuid()
            or stat.S_IMODE(witness_initial.st_mode) != 0o600
            or witness_initial.st_size != 0
        ):
            _fail("caller-created witness identity differs")
        parent_bound = os.fstat(parent_fd)
        if tuple(
            getattr(parent_bound, field) for field in BAG_IDENTITY_FIELDS
        ) != tuple(
            getattr(partial.lstat(), field) for field in BAG_IDENTITY_FIELDS
        ):
            _fail("pair-selection output parent changed during witness creation")

        environment = {
            PAIR_INDEX_POSTAUTH_ENV: PAIR_INDEX_POSTAUTH_VALUE,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "LD_LIBRARY_PATH": "/opt/ros/noetic/lib",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
        }
        argv = [
            "/usr/bin/python3",
            "-I",
            "-B",
            helper_binding.proc_path,
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
            "--parent-process-id",
            str(os.getpid()),
            "--parent-helper-fd",
            str(helper_binding.descriptor),
            "--helper-identity",
            _bag_identity_argument(helper_binding.identity),
            "--helper-sha256",
            helper_binding.sha256,
            "--witness-output",
            str(witness_path),
            "--parent-output-fd",
            str(parent_fd),
            "--output-parent-identity",
            _bag_identity_argument(parent_bound),
            "--parent-witness-fd",
            str(witness_fd),
            "--witness-identity",
            _bag_identity_argument(witness_initial),
        ]
        command = recorder.run(
            "pair_index",
            argv,
            Path(repo_root),
            "pair_index_witness_v4",
            environment,
            sequence_index=sequence_index,
        )
        authorization.revalidate()
        bound_bag.revalidate()
        helper_binding.revalidate()
        if command["exit_code"] != 0 or command["timed_out"]:
            _fail("independent pair-index/witness subprocess failed")
        stdout_path = partial / command["stdout"]
        stderr_path = partial / command["stderr"]
        for label, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            status_value = path.lstat()
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                _fail("pair-index subprocess " + label + " is unsafe")
            if schema.sha256_file(path) != command[label + "_sha256"]:
                _fail("pair-index subprocess " + label + " digest differs")
        if stderr_path.read_bytes() != b"":
            _fail("successful pair-index subprocess wrote diagnostics")
        if tuple(
            getattr(os.fstat(parent_fd), field) for field in BAG_IDENTITY_FIELDS
        ) != tuple(
            getattr(partial.lstat(), field) for field in BAG_IDENTITY_FIELDS
        ):
            _fail("held pair-selection output parent changed across subprocess")
        witness_status = os.fstat(witness_fd)
        witness_by_name = os.stat(
            witness_path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            tuple(getattr(witness_status, field) for field in BAG_IDENTITY_FIELDS)
            != tuple(getattr(witness_by_name, field) for field in BAG_IDENTITY_FIELDS)
            or (witness_status.st_dev, witness_status.st_ino)
            != (witness_initial.st_dev, witness_initial.st_ino)
            or not stat.S_ISREG(witness_status.st_mode)
            or witness_status.st_nlink != 1
            or stat.S_IMODE(witness_status.st_mode) != 0o444
        ):
            _fail("pair-selection witness held/path identity differs")
        witness_bytes = campaign._read_fd_all(
            witness_fd, MAX_PAIR_WITNESS_BYTES, "held pair-selection witness"
        )
        if tuple(
            getattr(os.fstat(witness_fd), field) for field in BAG_IDENTITY_FIELDS
        ) != tuple(
            getattr(
                os.stat(
                    witness_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                ),
                field,
            )
            for field in BAG_IDENTITY_FIELDS
        ):
            _fail("pair-selection witness changed during held read")
        evidence = PairSelectionEvidence(stdout_path.read_bytes(), witness_bytes)
        try:
            runner.validate_pair_selection_witness(
                evidence.witness_bytes,
                evidence.pair_index_bytes,
                sequence_index,
                runner.SEQUENCES[sequence_index],
            )
        except runner.SequenceRunnerError as exc:
            raise SequenceActualError(str(exc)) from exc
        authorization.revalidate()
        bound_bag.revalidate()
        helper_binding.revalidate()
        completed = True
        return evidence
    finally:
        if not completed and witness_fd >= 0 and parent_fd >= 0:
            held = os.fstat(witness_fd)
            try:
                named = os.stat(
                    witness_path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                if held.st_nlink != 0:
                    cleanup_error = "failed witness lost its exact published name"
            else:
                if (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino):
                    os.unlink(witness_path.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    if os.fstat(witness_fd).st_nlink != 0:
                        cleanup_error = "failed witness unlink did not remove its link"
                else:
                    cleanup_error = "failed witness name was replaced before cleanup"
        if witness_fd >= 0:
            os.close(witness_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        helper_binding.close()
        if cleanup_error is not None:
            _fail(cleanup_error)


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
    held_trace: _HeldRuntimeTrace,
    sink_capabilities: Mapping[str, str],
) -> List[str]:
    if set(sink_capabilities) != set(SINK_CAPABILITY_FIELDS):
        _fail("D sink capability field population differs")
    arguments = [
        str(Path(build["source_space"]) / "project/cp2_serial.launch"),
        "bag:=" + str(bag),
        "bag_start:=" + format(runner.OFFSETS_SECONDS[sequence_index], ".1f"),
        "config_path:="
        + str(Path(build["source_space"]) / "config/euroc_mav/estimator_config.yaml"),
        "path_state:=" + str(held_trace.proc_path("state.txt")),
        "path_std:=" + str(held_trace.proc_path("deviation.txt")),
        "path_time:=" + str(held_trace.proc_path("openvins_timing.csv")),
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
    arguments.extend(
        "cp2_{}_sink_capability:={}".format(field, sink_capabilities[field])
        for field in SINK_CAPABILITY_FIELDS
    )
    return arguments


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
    context_bytes: bytes
    loader_before_bytes: bytes
    loader_after_bytes: bytes
    transient_names: Tuple[str, ...]
    held_trace: _HeldRuntimeTrace


def _capture_mode(
    *,
    repo_root: Path,
    partial: Path,
    build: Mapping[str, Any],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    bound_bag: campaign.BoundRegularFile,
    runtime_bag_path: Path,
    sequence_index: int,
    mode: str,
    run_index: int,
    source_commit: str,
    config_sha256: str,
    pair_index_bytes: bytes,
    artifact_run_id: str,
) -> _CapturedMode:
    authorization.revalidate()
    if Path(runtime_bag_path) != _held_bag_proc_path(bound_bag):
        _fail("runtime bag capability changed before mode capture")
    if mode != runner.MODES[run_index]:
        _fail("mode capture order differs from nullspace then schur")
    trace = partial / "runtime" / mode
    trace.mkdir(mode=0o700, parents=True, exist_ok=False)
    held_trace = _HeldRuntimeTrace(trace)
    context_path = trace / "context.json"
    prelaunch_path = trace / "prelaunch_parameters.yaml"
    held_trace.precreate(
        tuple(name for name in D_SINK_NAMES.values() if name is not None)
        + (context_path.name, prelaunch_path.name)
    )
    sink_capabilities = {
        field: (
            "null"
            if D_SINK_NAMES[field] is None
            else held_trace.capability(D_SINK_NAMES[field])
        )
        for field in SINK_CAPABILITY_FIELDS
    }
    launch_args = _launch_arguments(
        build,
        runtime_bag_path,
        sequence_index,
        mode,
        trace,
        context_path,
        held_trace,
        sink_capabilities,
    )
    port = 15381 + 10 * sequence_index + run_index
    environment_id = "sequence_{}_{}".format(sequence_index, mode)
    environment = campaign._runtime_environment(
        build,
        Path(build["workspace"]) / "sequence-environment-{}-{}".format(sequence_index, mode),
        port,
    )
    try:
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
        held_trace.fill_parent(prelaunch_path.name, prelaunch_raw)
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
        held_trace.fill_parent(context_path.name, _canonical_json(context))
        held_trace.revalidate()
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
        if Path(runtime_bag_path) != _held_bag_proc_path(bound_bag):
            _fail("runtime bag capability changed across mode capture")

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
        held_trace.accept_external(
            tuple(name for name in D_SINK_NAMES.values() if name is not None)
        )
        if held_trace.payload(paths["serial"].name) != pair_index_bytes:
            _fail(mode + " runtime pair index differs from the independent bag index")
        runtime_raw = held_trace.payload(paths["runtime_raw"].name)
        try:
            runtime_parameters = schema.strict_json_loads(runtime_raw)
            runtime_canonical = schema.encode_resolved_parameters(runtime_parameters)
        except (schema.SchemaError, TypeError, ValueError) as exc:
            raise SequenceActualError(mode + " runtime parameter capture is invalid") from exc
        if not isinstance(runtime_parameters, Mapping) or runtime_canonical != canonical:
            _fail(mode + " runtime/prelaunch resolved parameters differ")
        loader_before_bytes = held_trace.payload(paths["loader_before"].name)
        loader_after_bytes = held_trace.payload(paths["loader_after"].name)
        dso_before = tuple(
            campaign._loader_dso_records_bytes(
                loader_before_bytes, Path(build["executable"])
            )
        )
        dso_after = tuple(
            campaign._loader_dso_records_bytes(
                loader_after_bytes, Path(build["executable"])
            )
        )
        if dso_before != dso_after:
            _fail(mode + " runtime DSO identities changed")
        executable_sha256 = schema.sha256_file(build["executable"])
        placeholder_stdout = b"rmse 0\n"
        source = runner.ModeRunInput(
            mode=mode,
            pair_index_bytes=pair_index_bytes,
            callback_bytes=held_trace.payload(paths["callbacks"].name),
            trajectory_bytes=held_trace.payload(paths["trajectory"].name),
            prelaunch_raw_bytes=prelaunch_raw,
            runtime_raw_bytes=runtime_raw,
            prelaunch_parameters=dict(parameters),
            runtime_parameters=dict(runtime_parameters),
            runtime_root=trace,
            executable_sha256=executable_sha256,
            loader_map_sha256=hashlib.sha256(loader_before_bytes).hexdigest(),
            legacy_state_bytes=held_trace.payload(paths["state"].name),
            legacy_deviation_bytes=held_trace.payload(paths["deviation"].name),
            legacy_timing_bytes=held_trace.payload(paths["timing"].name),
            evaluator_stdout_path="placeholder/{}.stdout".format(mode),
            evaluator_stdout_bytes=placeholder_stdout,
            evaluator_stderr_path="placeholder/{}.stderr".format(mode),
            evaluator_stderr_bytes=b"",
            evaluator_launcher_path="/placeholder/evaluator-launcher",
            evaluator_result_argument="/placeholder/{}_evaluator_result.zip".format(mode),
            evaluator_result_path="evaluation/{}_evaluator_result.zip".format(mode),
            evaluator_result_bytes=b"placeholder",
            evaluator_ate_m=0.0,
        )
        transient_names = (
            prelaunch_path.name,
            paths["serial"].name,
            paths["callbacks"].name,
            paths["trajectory"].name,
            paths["runtime_raw"].name,
            paths["state"].name,
            paths["deviation"].name,
            paths["timing"].name,
        )
        held_trace.revalidate()
        recorder.add_lifetime_guard(held_trace.revalidate)
        return _CapturedMode(
            source=source,
            run_id=run_id,
            trace=trace,
            context=context_path,
            loader_before=paths["loader_before"],
            loader_after=paths["loader_after"],
            dso_records_before=dso_before,
            dso_records_after=dso_after,
            context_bytes=held_trace.payload(context_path.name),
            loader_before_bytes=loader_before_bytes,
            loader_after_bytes=loader_after_bytes,
            transient_names=transient_names,
            held_trace=held_trace,
        )
    except BaseException:
        held_trace.close()
        raise


def _seal_worker_input(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        status_value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status_value.st_mode)
            or status_value.st_nlink != 1
            or status_value.st_uid != os.geteuid()
        ):
            _fail("direct-math request is not one owned regular file")
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    campaign._fsync_directory(path.parent)


def _run_revalidated_capsule_command(
    runtime_capsule: campaign.RuntimeCapsule,
    recorder: campaign.CommandRecorder,
    *args: Any,
    **kwargs: Any,
) -> Mapping[str, Any]:
    """Require a complete staged-tree check on both sides of execution."""

    runtime_capsule.revalidate()
    try:
        return recorder.run(*args, **kwargs)
    finally:
        runtime_capsule.revalidate()


def _run_direct_math(
    *,
    partial: Path,
    captures: Sequence[_CapturedMode],
    ground_truth: Sequence[runner.GroundTruthPose],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    sequence_index: int,
    runtime_capsule: campaign.RuntimeCapsule,
) -> _DirectMathRun:
    if (
        not isinstance(runtime_capsule, campaign.RuntimeCapsule)
        or runtime_capsule.kind != "direct_math"
    ):
        _fail("direct-math runtime capsule binding differs")
    runtime_capsule.revalidate()
    pairs = runner._validate_pairs(
        captures[0].source.pair_index_bytes,
        sequence_index,
        runner.SEQUENCES[sequence_index],
    )
    validated = tuple(
        runner._validate_mode_trace(
            capture.source, sequence_index, runner.SEQUENCES[sequence_index], pairs
        )
        for capture in captures
    )
    gt_rows = runner._ground_truth(ground_truth)
    request = runner._direct_request_bytes(
        validated,
        gt_rows,
        sequence_index,
        runner.SEQUENCES[sequence_index],
    )
    work_root = runtime_capsule.work_root
    if os.listdir(work_root):
        _fail("direct-math capsule work directory is not initially empty")
    request_path = work_root / "request.json"
    response_path = work_root / "response.json"
    campaign._write_new(request_path, request)
    _seal_worker_input(request_path)
    launcher = runtime_capsule.launcher
    direct_environment = runtime_capsule.sandbox_environment
    evidence_environment = runtime_capsule.evidence_environment
    launcher_status = launcher.lstat()
    if (
        not stat.S_ISREG(launcher_status.st_mode)
        or launcher_status.st_nlink != 1
        or launcher_status.st_uid != os.geteuid()
        or stat.S_IMODE(launcher_status.st_mode) != 0o555
    ):
        _fail("direct-math capsule launcher is not one private executable input")
    logical_argv = [
        str(launcher),
        "--input",
        str(request_path),
        "--output",
        str(response_path),
    ]
    try:
        with capsule.SealedCapsuleSandbox(
            runtime_capsule.staged
        ) as sandbox, sandbox.invocation(
            (
                "/capsule/" + runtime_capsule.profile.launcher_path,
                "--input",
                "/private/work/request.json",
                "--output",
                "/private/work/response.json",
            ),
            read_only_inputs={"/private/work/request.json": request},
            writable_outputs={
                "/private/work/response.json": runner.math_codec.MAX_DOCUMENT_BYTES
            },
        ) as invocation:
            command = _run_revalidated_capsule_command(
                runtime_capsule,
                recorder,
                "evaluation",
                logical_argv,
                Path("/tmp"),
                "direct_math_capsule_v1",
                direct_environment,
                sequence_index=sequence_index,
                timeout=runtime_capsule.profile.execution.timeout_seconds,
                process_argv=invocation.argv,
                process_executable=invocation.executable,
                process_pass_fds=invocation.pass_fds,
                evidence_variables=evidence_environment,
                evidence_cwd=work_root,
            )
            if command["exit_code"] != 0 or command["timed_out"]:
                _fail("direct-math capsule execution failed")
            response = invocation.seal_output(
                "/private/work/response.json",
                runner.math_codec.MAX_DOCUMENT_BYTES,
            )
    except capsule.CapsuleError as exc:
        raise SequenceActualError(
            "direct-math sealed capsule execution failed closed"
        ) from exc
    campaign._write_new(response_path, response, 0o444)
    campaign._fsync_directory(response_path.parent)
    response_status = response_path.lstat()
    if (
        not stat.S_ISREG(response_status.st_mode)
        or response_status.st_nlink != 1
        or response_status.st_uid != os.geteuid()
        or stat.S_IMODE(response_status.st_mode) != 0o444
    ):
        _fail("direct-math response is not one immutable private file")
    if response_path.read_bytes() != response:
        _fail("retained direct-math response differs from sealed output")
    try:
        runner.math_codec.decode_sequence_response(response, request)
    except runner.math_codec.SequenceMathCodecError as exc:
        raise SequenceActualError("direct-math response: " + str(exc)) from exc
    authorization.revalidate()
    return _DirectMathRun(
        request_path=request_path,
        response_path=response_path,
        request_bytes=request,
        response_bytes=response,
        launcher_path=str(launcher),
        stdout_path=command["stdout"],
        stdout_bytes=(partial / command["stdout"]).read_bytes(),
        stderr_path=command["stderr"],
        stderr_bytes=(partial / command["stderr"]).read_bytes(),
        work_root=work_root,
    )


def _run_evaluators(
    *,
    partial: Path,
    captures: Sequence[_CapturedMode],
    ground_truth: Sequence[runner.GroundTruthPose],
    recorder: campaign.CommandRecorder,
    authorization: Any,
    sequence_index: int,
    runtime_capsule: campaign.RuntimeCapsule,
    direct_math: _DirectMathRun,
) -> Tuple[Tuple[runner.ModeRunInput, runner.ModeRunInput], Path]:
    if (
        not isinstance(runtime_capsule, campaign.RuntimeCapsule)
        or runtime_capsule.kind != "evaluator"
    ):
        _fail("evaluator runtime capsule binding differs")
    runtime_capsule.revalidate()
    pairs = runner._validate_pairs(
        captures[0].source.pair_index_bytes,
        sequence_index,
        runner.SEQUENCES[sequence_index],
    )
    validated = tuple(
        runner._validate_mode_trace(
            capture.source, sequence_index, runner.SEQUENCES[sequence_index], pairs
        )
        for capture in captures
    )
    metric_files, _ = runner._metric_artifacts(
        validated,
        runner._ground_truth(ground_truth),
        sequence_index,
        runner.SEQUENCES[sequence_index],
        direct_math.request_bytes,
        direct_math.response_bytes,
        verify_evaluator=False,
    )
    evaluation_root = runtime_capsule.work_root
    if os.listdir(evaluation_root):
        _fail("evaluator capsule work directory is not initially empty")
    for name in (
        "ground_truth_shared.tum",
        "nullspace_shared_aligned.tum",
        "schur_shared_aligned.tum",
    ):
        campaign._write_new(evaluation_root / name, metric_files[name])
    environment = runtime_capsule.sandbox_environment
    evidence_environment = runtime_capsule.evidence_environment
    launcher = runtime_capsule.launcher
    launcher_status = launcher.lstat()
    if (
        not stat.S_ISREG(launcher_status.st_mode)
        or launcher_status.st_nlink != 1
        or launcher_status.st_uid != os.geteuid()
        or stat.S_IMODE(launcher_status.st_mode) != 0o555
    ):
        _fail("evaluator capsule launcher is not one private executable input")
    completed = []
    try:
        with capsule.SealedCapsuleSandbox(
            runtime_capsule.staged
        ) as sandbox:
            for run_index, capture in enumerate(captures):
                mode = runner.MODES[run_index]
                result_path = evaluation_root / (mode + "_evaluator_result.zip")
                logical_argv = [
                    str(launcher),
                    "tum",
                    "ground_truth_shared.tum",
                    mode + "_shared_aligned.tum",
                    "-r",
                    "trans_part",
                    "--t_max_diff",
                    "0.01",
                    "--save_results",
                    str(result_path),
                    "--no_warnings",
                ]
                with sandbox.invocation(
                    (
                        "/capsule/" + runtime_capsule.profile.launcher_path,
                        "tum",
                        "/private/work/ground_truth_shared.tum",
                        "/private/work/" + mode + "_shared_aligned.tum",
                        "-r",
                        "trans_part",
                        "--t_max_diff",
                        "0.01",
                        "--save_results",
                        "/private/work/" + mode + "_evaluator_result.zip",
                        "--no_warnings",
                    ),
                    read_only_inputs={
                        "/private/work/ground_truth_shared.tum": metric_files[
                            "ground_truth_shared.tum"
                        ],
                        "/private/work/" + mode + "_shared_aligned.tum": metric_files[
                            mode + "_shared_aligned.tum"
                        ],
                    },
                    writable_outputs={
                        "/private/work/" + mode + "_evaluator_result.zip":
                            evaluator_result.MAX_ARCHIVE_BYTES
                    },
                ) as invocation:
                    command = _run_revalidated_capsule_command(
                        runtime_capsule,
                        recorder,
                        "evaluation",
                        logical_argv,
                        Path("/tmp"),
                        "evaluator_v1",
                        environment,
                        sequence_index=sequence_index,
                        run_index=run_index,
                        timeout=runtime_capsule.profile.execution.timeout_seconds,
                        process_argv=invocation.argv,
                        process_executable=invocation.executable,
                        process_pass_fds=invocation.pass_fds,
                        evidence_variables=evidence_environment,
                        evidence_cwd=evaluation_root,
                    )
                    if command["exit_code"] != 0 or command["timed_out"]:
                        _fail(mode + " equivalent evaluator failed")
                    result_bytes = invocation.seal_output(
                        "/private/work/" + mode + "_evaluator_result.zip",
                        evaluator_result.MAX_ARCHIVE_BYTES,
                    )
                campaign._write_new(result_path, result_bytes, 0o444)
                campaign._fsync_directory(result_path.parent)
                stdout = (partial / command["stdout"]).read_bytes()
                stderr = (partial / command["stderr"]).read_bytes()
                result_status = result_path.lstat()
                if (
                    not stat.S_ISREG(result_status.st_mode)
                    or result_status.st_nlink != 1
                    or stat.S_IMODE(result_status.st_mode) != 0o444
                    or result_path.read_bytes() != result_bytes
                ):
                    _fail(mode + " evaluator result archive identity differs")
                try:
                    archive_result = evaluator_result.parse_evaluator_result_archive(
                        result_bytes
                    )
                    statistics = archive_result.statistics
                    console_token = runner._parse_evaluator_rmse_token(
                        stdout, mode + " equivalent evaluator"
                    )
                    evaluator_result.require_console_rmse_match(
                        console_token, statistics.rmse
                    )
                except (
                    runner.SequenceRunnerError,
                    evaluator_result.EvaluatorResultError,
                ) as exc:
                    raise SequenceActualError(str(exc)) from exc
                completed.append(
                    replace(
                        capture.source,
                        evaluator_stdout_path=command["stdout"],
                        evaluator_stdout_bytes=stdout,
                        evaluator_stderr_path=command["stderr"],
                        evaluator_stderr_bytes=stderr,
                        evaluator_launcher_path=str(launcher),
                        evaluator_result_argument=str(result_path),
                        evaluator_result_path=(
                            "evaluation/{}_evaluator_result.zip".format(mode)
                        ),
                        evaluator_result_bytes=result_bytes,
                        evaluator_ate_m=statistics.rmse,
                    )
                )
    except capsule.CapsuleError as exc:
        raise SequenceActualError(
            "evaluator sealed capsule execution failed closed"
        ) from exc
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
            "sha256": campaign._held_source_hash(authorization, relative),
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
                "loader_map_before_sha256": hashlib.sha256(
                    capture.loader_before_bytes
                ).hexdigest(),
                "loader_map_after": capture.loader_after.relative_to(partial).as_posix(),
                "loader_map_after_sha256": hashlib.sha256(
                    capture.loader_after_bytes
                ).hexdigest(),
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
        contexts.append(
            {
                "run_id": capture.run_id,
                "path": capture.context.relative_to(partial).as_posix(),
                "size": len(capture.context_bytes),
                "sha256": hashlib.sha256(capture.context_bytes).hexdigest(),
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
    direct_math: _DirectMathRun,
) -> Dict[str, runner.SupportFile]:
    result: Dict[str, runner.SupportFile] = {}

    def add(relative: str, role: str, held_payload: Optional[bytes] = None) -> None:
        schema.validate_relpath(relative, "support path")
        path = partial / relative
        if held_payload is None:
            status_value = path.lstat()
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                _fail("support path is not a single-link regular file: " + relative)
            held_payload = path.read_bytes()
        result[relative] = runner.SupportFile(held_payload, role)

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
        capture.held_trace.revalidate()
        add(
            capture.context.relative_to(partial).as_posix(),
            "configuration",
            capture.context_bytes,
        )
        add(
            capture.loader_before.relative_to(partial).as_posix(),
            "log",
            capture.loader_before_bytes,
        )
        add(
            capture.loader_after.relative_to(partial).as_posix(),
            "log",
            capture.loader_after_bytes,
        )
    evaluation_streams = {
        record[stream]
        for record in recorder.records
        if record["phase"] == "evaluation"
        for stream in ("stdout", "stderr")
    }
    for record in recorder.records:
        for stream in ("stdout", "stderr"):
            relative = record[stream]
            if relative in (direct_math.stdout_path, direct_math.stderr_path):
                role = "direct_math"
            else:
                role = "evaluator" if relative in evaluation_streams else "log"
            add(relative, role)
    return result


def _remove_transient_inputs(
    captures: Sequence[_CapturedMode],
    evaluation_root: Path,
    direct_math_root: Path,
    pair_witness_path: Path,
) -> None:
    for capture in captures:
        capture.held_trace.unlink_exact(capture.transient_names)
        capture.held_trace.revalidate()
        capture.held_trace.close()
    for path in sorted(evaluation_root.iterdir(), key=lambda item: os.fsencode(item.name)):
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("evaluator work directory contains a nonregular input")
        path.unlink()
    evaluation_root.rmdir()
    for path in sorted(direct_math_root.iterdir(), key=lambda item: os.fsencode(item.name)):
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("direct-math work directory contains a nonregular input")
        path.unlink()
    direct_math_root.rmdir()
    witness_status = pair_witness_path.lstat()
    if (
        not stat.S_ISREG(witness_status.st_mode)
        or witness_status.st_nlink != 1
        or stat.S_IMODE(witness_status.st_mode) != 0o444
    ):
        _fail("pair witness changed before fixed-member assembly")
    pair_witness_path.unlink()


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
    workspace_owner: Optional[campaign.OwnedTemporaryWorkspace] = None
    captures_list: List[_CapturedMode] = []
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
        candidate_owner = build.get("workspace_owner")
        if not isinstance(candidate_owner, campaign.OwnedTemporaryWorkspace):
            _fail("runtime build did not return its private-workspace owner")
        workspace_owner = candidate_owner
        executable_before = schema.sha256_file(build["executable"])
        executable_build_id, executable_soname = campaign._elf_identity(
            Path(build["executable"]).absolute(), require_soname=False
        )
        if executable_soname is not None:
            _fail("runtime executable unexpectedly declares a DSO SONAME")
        pair_evidence = _run_pair_index_command(
            repo_root=root,
            partial=partial,
            build=build,
            recorder=recorder,
            authorization=authorization,
            bound_bag=bound_bag,
            sequence_index=sequence_index,
        )
        runtime_bag_path = _held_bag_proc_path(bound_bag)
        recorder.add_lifetime_guard(
            lambda: _held_bag_proc_path(bound_bag)
        )
        for run_index, mode in enumerate(runner.MODES):
            captures_list.append(_capture_mode(
                repo_root=root,
                partial=partial,
                build=build,
                recorder=recorder,
                authorization=authorization,
                bound_bag=bound_bag,
                runtime_bag_path=runtime_bag_path,
                sequence_index=sequence_index,
                mode=mode,
                run_index=run_index,
                source_commit=source_commit,
                config_sha256=config_sha256,
                pair_index_bytes=pair_evidence.pair_index_bytes,
                artifact_run_id=run_id,
            ))
        captures = tuple(captures_list)
        if bound_bag.sha256() != runner.FROZEN_BAG_SHA256[sequence_index]:
            _fail("bound bag SHA-256 changed across the two mode runs")
        bound_ground_truth.revalidate()
        if bound_ground_truth.sha256() != runner.FROZEN_GROUND_TRUTH_SHA256[sequence_index]:
            _fail("bound ground-truth SHA-256 changed across the two mode runs")
        direct_capsule = build.get("direct_math_capsule")
        evaluator_capsule = build.get("evaluator_capsule")
        if (
            not isinstance(direct_capsule, campaign.RuntimeCapsule)
            or direct_capsule.kind != "direct_math"
            or Path(build.get("direct_math_launcher", ""))
            != direct_capsule.launcher
            or build.get("direct_math_environment")
            != direct_capsule.environment
        ):
            _fail("runtime build did not return the exact direct-math capsule")
        if (
            not isinstance(evaluator_capsule, campaign.RuntimeCapsule)
            or evaluator_capsule.kind != "evaluator"
            or Path(build.get("evaluator_launcher", ""))
            != evaluator_capsule.launcher
            or build.get("evaluator_environment")
            != evaluator_capsule.environment
        ):
            _fail("runtime build did not return the exact evaluator capsule")
        direct_capsule.revalidate()
        evaluator_capsule.revalidate()
        direct_math = _run_direct_math(
            partial=partial,
            captures=captures,
            ground_truth=ground_truth,
            recorder=recorder,
            authorization=authorization,
            sequence_index=sequence_index,
            runtime_capsule=direct_capsule,
        )
        modes, evaluation_root = _run_evaluators(
            partial=partial,
            captures=captures,
            ground_truth=ground_truth,
            recorder=recorder,
            authorization=authorization,
            sequence_index=sequence_index,
            runtime_capsule=evaluator_capsule,
            direct_math=direct_math,
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
        support = _support_files(
            partial, authorization, build, captures, recorder, direct_math
        )
        evidence = runner.SequenceAssemblyInput(
            sequence_index=sequence_index,
            sequence_id=sequence_id,
            offset_seconds=runner.OFFSETS_SECONDS[sequence_index],
            run_id=run_id,
            modes=modes,
            pair_witness_bytes=pair_evidence.witness_bytes,
            ground_truth=tuple(ground_truth),
            direct_math_launcher_path=direct_math.launcher_path,
            direct_math_request_argument=str(direct_math.request_path),
            direct_math_response_argument=str(direct_math.response_path),
            direct_math_request_bytes=direct_math.request_bytes,
            direct_math_response_bytes=direct_math.response_bytes,
            direct_math_stdout_path=direct_math.stdout_path,
            direct_math_stdout_bytes=direct_math.stdout_bytes,
            direct_math_stderr_path=direct_math.stderr_path,
            direct_math_stderr_bytes=direct_math.stderr_bytes,
            provenance_without_inventory=provenance,
            commands_bytes=command_bytes,
            support_files=support,
        )
        result = runner.assemble_sequence_artifact(
            partial,
            evidence,
            prepopulated_support=True,
            after_validation=lambda: _remove_transient_inputs(
                captures,
                evaluation_root,
                direct_math.work_root,
                partial / runner.PAIR_WITNESS_PATH,
            ),
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
        workspace_owner.remove()
        workspace_owner = None
        authorization.revalidate()
        runner.publish_sequence_noreplace(partial, final)
        return {"artifact": str(final), "manifest_sha256": result.manifest_sha256}
    except BaseException as exc:
        _retain_failure(partial, parent, exc, source_commit, source_tree)
        raise
    finally:
        for capture in captures_list:
            capture.held_trace.close()
        if bound_ground_truth is not None:
            bound_ground_truth.close()
        if bound_bag is not None:
            bound_bag.close()
        if workspace_owner is not None:
            workspace_owner.remove()


__all__ = [
    "execute_authorized_sequence",
    "FilteredMessage",
    "PairSelectionEvidence",
    "PAIR_INDEX_POSTAUTH_ENV",
    "PAIR_INDEX_POSTAUTH_VALUE",
    "SequenceActualError",
    "parse_ground_truth_tum_bytes",
    "project_serial_pairs_to_pair_index_bytes",
    "read_pair_index_from_bag",
    "select_pair_evidence",
    "select_pair_index_bytes",
]
