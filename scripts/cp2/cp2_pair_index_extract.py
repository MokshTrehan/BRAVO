#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Post-authorization CP2 pair-index subprocess.

Success writes only canonical ``pair_index`` JSONL to stdout.  The caller
retains that stream as a command log.  The helper opens the bag through the
parent runner's already-held descriptor and independently joins the original
path and descriptor identities before and after rosbag access.
"""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2 pair-index extractor requires isolated Python (-I)")

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


POSTAUTH_ENV = "CP2_POSTAUTH_PAIR_INDEX"
POSTAUTH_VALUE = "held-readiness-bound-fd-v1"
IDENTITY_FIELDS = (
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
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
OFFSETS_SECONDS = (40, 40, 35)
TOPICS = ("/imu0", "/cam0/image_raw", "/cam1/image_raw")
STRICT_PAIR_DELTA_NS = 20_000_000
WITNESS_BASENAME = "pair_selection_witness.jsonl"
SAFE_LEAF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PairIndexCLIError(RuntimeError):
    """Raised when the post-authorization CLI contract is not exact."""


def _fail(message: str) -> None:
    raise PairIndexCLIError(message)


def _canonical_uint(text: Any, label: str) -> int:
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdecimal():
        _fail(label + " is not an unsigned decimal integer")
    value = int(text, 10)
    if str(value) != text or value > (1 << 64) - 1:
        _fail(label + " is not a canonical u64")
    return value


def _parse_cli(arguments: Sequence[str]) -> Mapping[str, str]:
    base_flags = (
        "--sequence-index",
        "--sequence-id",
        "--bag-path",
        "--parent-bag-fd",
        "--bag-identity",
    )
    source_flags = (
        "--parent-process-id",
        "--parent-helper-fd",
        "--helper-identity",
        "--helper-sha256",
    )
    witness_flags = (
        "--witness-output",
        "--parent-output-fd",
        "--output-parent-identity",
        "--parent-witness-fd",
        "--witness-identity",
    )
    if len(arguments) == 10:
        expected_flags = base_flags
    elif len(arguments) == 28:
        expected_flags = base_flags + source_flags + witness_flags
    else:
        expected_flags = ()
    if tuple(arguments[0::2]) != expected_flags:
        _fail("arguments differ from the exact post-authorization CLI")
    values = dict(zip(arguments[0::2], arguments[1::2]))
    if any(not isinstance(value, str) or not value or "\0" in value for value in values.values()):
        _fail("an argument value is empty or invalid")
    return values


def _write_witness(
    path_text: str,
    payload: bytes,
    parent_pid: int,
    parent_output_fd: int,
    output_parent_identity: Tuple[int, ...],
    parent_witness_fd: int,
    witness_identity: Tuple[int, ...],
) -> None:
    """Fill the caller-created witness while both exact inodes stay held."""

    if (
        not isinstance(path_text, str)
        or not os.path.isabs(path_text)
        or os.path.normpath(path_text) != path_text
        or "\0" in path_text
    ):
        _fail("witness output path is not normalized absolute")
    path = Path(path_text)
    if path.name != WITNESS_BASENAME or SAFE_LEAF.fullmatch(path.name) is None:
        _fail("witness output basename differs from the frozen name")
    if (
        isinstance(parent_pid, bool)
        or not isinstance(parent_pid, int)
        or parent_pid <= 0
        or isinstance(parent_output_fd, bool)
        or not isinstance(parent_output_fd, int)
        or not 0 <= parent_output_fd <= (1 << 31) - 1
        or isinstance(parent_witness_fd, bool)
        or not isinstance(parent_witness_fd, int)
        or not 0 <= parent_witness_fd <= (1 << 31) - 1
    ):
        _fail("witness parent capability is invalid")
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd = os.open(
            "/proc/{}/fd/{}".format(parent_pid, parent_output_fd),
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
        )
        descriptor = os.open(
            "/proc/{}/fd/{}".format(parent_pid, parent_witness_fd),
            os.O_RDWR | os.O_CLOEXEC,
        )
        parent_status = os.fstat(parent_fd)
        parent_by_path = os.stat(str(path.parent), follow_symlinks=False)
        status_value = os.fstat(descriptor)
        by_name_before = os.stat(
            path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            _observed_identity(parent_status) != output_parent_identity
            or _observed_identity(parent_by_path) != output_parent_identity
            or not stat.S_ISDIR(parent_status.st_mode)
            or parent_status.st_uid != os.geteuid()
            or stat.S_IMODE(parent_status.st_mode) != 0o700
            or _observed_identity(status_value) != witness_identity
            or _observed_identity(by_name_before) != witness_identity
            or not stat.S_ISREG(status_value.st_mode)
            or status_value.st_nlink != 1
            or status_value.st_uid != os.geteuid()
            or stat.S_IMODE(status_value.st_mode) != 0o600
            or status_value.st_size != 0
        ):
            _fail("held witness parent/file identity differs before write")
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(payload)
        offset = 0
        while offset < len(view):
            written = os.write(descriptor, view[offset:])
            if written <= 0:
                _fail("witness output write made no progress")
            offset += written
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        final_status = os.fstat(descriptor)
        by_path = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _observed_identity(final_status) != _observed_identity(by_path)
            or (final_status.st_dev, final_status.st_ino)
            != (status_value.st_dev, status_value.st_ino)
            or final_status.st_nlink != 1
            or final_status.st_uid != os.geteuid()
            or stat.S_IMODE(final_status.st_mode) != 0o444
            or final_status.st_size != len(payload)
        ):
            _fail("witness output path/descriptor identity differs")
        os.fsync(parent_fd)
        if (
            _observed_identity(os.fstat(parent_fd)) != output_parent_identity
            or _observed_identity(
                os.stat(str(path.parent), follow_symlinks=False)
            )
            != output_parent_identity
        ):
            _fail("held witness parent identity changed during write")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)


def _parse_identity(text: str, label: str = "bag identity") -> Tuple[int, ...]:
    fields = text.split(":")
    if len(fields) != len(IDENTITY_FIELDS):
        _fail(label + " has the wrong field population")
    return tuple(
        _canonical_uint(value, label + " " + name)
        for name, value in zip(IDENTITY_FIELDS, fields)
    )


def _observed_identity(value: os.stat_result) -> Tuple[int, ...]:
    return tuple(getattr(value, field) for field in IDENTITY_FIELDS)


def _sha256_descriptor(descriptor: int) -> str:
    status_before = os.fstat(descriptor)
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        try:
            block = os.read(descriptor, 1024 * 1024)
        except InterruptedError:
            continue
        if not block:
            break
        digest.update(block)
    if _observed_identity(os.fstat(descriptor)) != _observed_identity(status_before):
        _fail("held helper changed while being hashed")
    return digest.hexdigest()


def _open_held_helper(
    parsed: Mapping[str, str],
    parent_pid: int,
    executed_helper_path: Optional[str],
) -> int:
    """Open and authenticate the exact source inode used as this script."""

    if "--parent-process-id" not in parsed:
        return -1
    recorded_pid = _canonical_uint(
        parsed["--parent-process-id"], "parent process ID"
    )
    helper_fd = _canonical_uint(
        parsed["--parent-helper-fd"], "parent helper descriptor"
    )
    if (
        recorded_pid != parent_pid
        or recorded_pid > (1 << 31) - 1
        or helper_fd > (1 << 31) - 1
    ):
        _fail("held helper parent capability differs")
    expected = _parse_identity(parsed["--helper-identity"], "helper identity")
    expected_sha256 = parsed["--helper-sha256"]
    if HEX64.fullmatch(expected_sha256) is None:
        _fail("helper SHA-256 is not canonical")
    capability_path = "/proc/{}/fd/{}".format(parent_pid, helper_fd)
    if executed_helper_path is not None and executed_helper_path != capability_path:
        _fail("executed helper path differs from its held capability")
    try:
        descriptor = os.open(capability_path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        raise PairIndexCLIError("held helper capability is unavailable") from exc
    try:
        status_value = os.fstat(descriptor)
        if (
            _observed_identity(status_value) != expected
            or not stat.S_ISREG(status_value.st_mode)
            or status_value.st_nlink != 1
            or _sha256_descriptor(descriptor) != expected_sha256
        ):
            _fail("held helper inode or SHA-256 differs")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _checked_time_ns(value: Any, label: str) -> int:
    seconds = getattr(value, "secs", None)
    nanoseconds = getattr(value, "nsecs", None)
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or isinstance(nanoseconds, bool)
        or not isinstance(nanoseconds, int)
        or seconds < 0
        or not 0 <= nanoseconds < 1_000_000_000
        or seconds > ((1 << 64) - 1 - nanoseconds) // 1_000_000_000
    ):
        _fail(label + " is not an exact nonnegative ROS timestamp")
    return seconds * 1_000_000_000 + nanoseconds


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    try:
        return b"".join(
            (
                json.dumps(
                    row,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            for row in rows
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PairIndexCLIError("cannot encode canonical pair evidence") from exc


def _select_pair_evidence(
    sequence_index: int,
    sequence_id: str,
    messages: Sequence[Tuple[str, int, int]],
) -> Tuple[bytes, bytes]:
    normalized = []
    witness_rows = []
    for ordinal, item in enumerate(messages):
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or item[0] not in ("imu", "cam0", "cam1")
        ):
            _fail("filtered bag metadata has an invalid message kind")
        kind, record_time, header_time = item
        if (
            isinstance(record_time, bool)
            or not isinstance(record_time, int)
            or not 0 <= record_time <= (1 << 64) - 1
            or isinstance(header_time, bool)
            or not isinstance(header_time, int)
            or not 0 <= header_time <= (1 << 64) - 1
            or (kind == "imu" and header_time != 0)
            or (normalized and record_time < normalized[-1][1])
        ):
            _fail("filtered bag metadata is not canonical chronological u64 data")
        normalized.append((kind, record_time, header_time))
        witness_rows.append(
            {
                "schema_version": 1,
                "record_type": "filtered_message",
                "sequence_index": sequence_index,
                "sequence_id": sequence_id,
                "filtered_index": ordinal,
                "kind": kind,
                "camera_id": 0 if kind == "cam0" else (1 if kind == "cam1" else None),
                "record_time_ns": record_time,
                "header_time_ns": header_time,
            }
        )

    used = set()
    pair_rows = []
    for anchor_index, anchor in enumerate(normalized):
        if anchor[0] not in ("cam0", "cam1") or anchor_index in used:
            continue
        other = "cam1" if anchor[0] == "cam0" else "cam0"
        candidate_index = next(
            (
                ordinal
                for ordinal in range(anchor_index + 1, len(normalized))
                if normalized[ordinal][0] == other
            ),
            None,
        )
        if candidate_index is None or candidate_index in used:
            continue
        candidate = normalized[candidate_index]
        delta = abs(anchor[1] - candidate[1])
        if delta >= STRICT_PAIR_DELTA_NS:
            continue
        cam0_index, cam1_index = (
            (anchor_index, candidate_index)
            if anchor[0] == "cam0"
            else (candidate_index, anchor_index)
        )
        cam0 = normalized[cam0_index]
        cam1 = normalized[cam1_index]
        pair_rows.append(
            {
                "schema_version": 1,
                "record_type": "pair_index",
                "sequence_index": sequence_index,
                "sequence_id": sequence_id,
                "pair_index": len(pair_rows),
                "anchor_filtered_index": anchor_index,
                "anchor_camera_id": 0 if anchor[0] == "cam0" else 1,
                "cam0_filtered_index": cam0_index,
                "cam1_filtered_index": cam1_index,
                "cam0_record_time_ns": cam0[1],
                "cam1_record_time_ns": cam1[1],
                "cam0_header_time_ns": cam0[2],
                "cam1_header_time_ns": cam1[2],
                "absolute_record_delta_ns": delta,
            }
        )
        used.update((anchor_index, candidate_index))
    if len(pair_rows) < 2:
        _fail("independent pair index has fewer than two selected rows")
    if pair_rows[-1]["cam0_record_time_ns"] <= pair_rows[0]["cam0_record_time_ns"]:
        _fail("independent pair index has no positive cam0 record duration")
    return _jsonl(pair_rows), _jsonl(witness_rows)


def _read_pair_evidence(
    descriptor: int,
    sequence_index: int,
    bag_factory: Callable[..., Any],
) -> Tuple[bytes, bytes]:
    bag = bag_factory("/proc/self/fd/{}".format(descriptor), "r")
    try:
        first = next(iter(bag.read_messages(raw=True)), None)
        if first is None or not isinstance(first, tuple) or len(first) != 3:
            _fail("rosbag is empty or has an invalid message iterator")
        first_time = first[2]
        first_ns = _checked_time_ns(first_time, "rosbag begin time")
        offset_ns = OFFSETS_SECONDS[sequence_index] * 1_000_000_000
        if first_ns > (1 << 64) - 1 - offset_ns:
            _fail("post-offset bag start overflows u64")
        start_ns = first_ns + offset_ns
        start_time = type(first_time)(
            start_ns // 1_000_000_000, start_ns % 1_000_000_000
        )
        filtered = []
        for topic, message, record_time in bag.read_messages(
            topics=list(TOPICS), start_time=start_time
        ):
            record_ns = _checked_time_ns(record_time, "rosbag record time")
            if topic == TOPICS[0]:
                filtered.append(("imu", record_ns, 0))
                continue
            if topic not in TOPICS[1:]:
                _fail("topic-filtered rosbag iterator emitted an unknown topic")
            if getattr(message, "_type", None) != "sensor_msgs/Image":
                _fail("camera topic does not contain sensor_msgs/Image")
            header = getattr(message, "header", None)
            header_ns = _checked_time_ns(
                getattr(header, "stamp", None), "camera header time"
            )
            filtered.append(
                ("cam0" if topic == TOPICS[1] else "cam1", record_ns, header_ns)
            )
    finally:
        bag.close()
    return _select_pair_evidence(
        sequence_index, SEQUENCES[sequence_index], tuple(filtered)
    )


def _revalidate(
    original_path: Path,
    descriptor: int,
    expected: Tuple[int, ...],
) -> None:
    by_descriptor = os.fstat(descriptor)
    by_path = os.stat(str(original_path), follow_symlinks=False)
    if (
        _observed_identity(by_descriptor) != expected
        or _observed_identity(by_path) != expected
        or not stat.S_ISREG(by_descriptor.st_mode)
        or by_descriptor.st_nlink != 1
    ):
        _fail("held bag descriptor or original path identity changed")


def _extract(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    output: Any,
    *,
    bag_factory: Optional[Callable[..., Any]] = None,
    parent_pid: Optional[int] = None,
    executed_helper_path: Optional[str] = None,
) -> None:
    parsed = _parse_cli(arguments)
    if environment.get(POSTAUTH_ENV) != POSTAUTH_VALUE:
        _fail("explicit held-readiness authorization capability is absent")

    sequence_index = _canonical_uint(parsed["--sequence-index"], "sequence index")
    if sequence_index >= len(SEQUENCES) or parsed["--sequence-id"] != SEQUENCES[sequence_index]:
        _fail("sequence identity differs from the frozen inventory")
    bag_path = Path(parsed["--bag-path"])
    bag_text = str(bag_path)
    if (
        not bag_path.is_absolute()
        or os.path.normpath(bag_text) != bag_text
        or bag_text == os.path.sep
    ):
        _fail("bag path is not normalized absolute")
    parent_fd = _canonical_uint(parsed["--parent-bag-fd"], "parent bag descriptor")
    if parent_fd > (1 << 31) - 1:
        _fail("parent bag descriptor is outside the supported range")
    expected = _parse_identity(parsed["--bag-identity"])
    if parent_pid is None:
        parent_pid = os.getppid()
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        _fail("parent process identity is invalid")

    helper_descriptor = _open_held_helper(
        parsed, parent_pid, executed_helper_path
    )

    parent_descriptor_path = "/proc/{}/fd/{}".format(parent_pid, parent_fd)
    try:
        descriptor = os.open(parent_descriptor_path, os.O_RDONLY | os.O_CLOEXEC)
    except OSError as exc:
        if helper_descriptor >= 0:
            os.close(helper_descriptor)
        raise PairIndexCLIError("held bag capability is unavailable") from exc
    try:
        _revalidate(bag_path, descriptor, expected)

        if bag_factory is None:
            ros_python_directory = "/opt/ros/noetic/lib/python3/dist-packages"
            sys.path.insert(0, ros_python_directory)
            try:
                bag_factory = __import__("rosbag").Bag
            except (ImportError, AttributeError) as exc:
                raise PairIndexCLIError("ROS1 rosbag provider is unavailable") from exc
        pair_index_bytes, witness_bytes = _read_pair_evidence(
            descriptor, sequence_index, bag_factory
        )
        _revalidate(bag_path, descriptor, expected)
        if helper_descriptor >= 0 and (
            _observed_identity(os.fstat(helper_descriptor))
            != _parse_identity(parsed["--helper-identity"], "helper identity")
            or _sha256_descriptor(helper_descriptor) != parsed["--helper-sha256"]
        ):
            _fail("held helper changed across extraction")
    finally:
        os.close(descriptor)
        if helper_descriptor >= 0:
            os.close(helper_descriptor)
    if "--witness-output" in parsed:
        _write_witness(
            parsed["--witness-output"],
            witness_bytes,
            parent_pid,
            _canonical_uint(parsed["--parent-output-fd"], "parent output descriptor"),
            _parse_identity(
                parsed["--output-parent-identity"], "output parent identity"
            ),
            _canonical_uint(parsed["--parent-witness-fd"], "parent witness descriptor"),
            _parse_identity(parsed["--witness-identity"], "witness identity"),
        )
    written = output.write(pair_index_bytes)
    if written is not None and written != len(pair_index_bytes):
        _fail("stdout did not accept the complete canonical JSONL payload")
    output.flush()


def main(arguments: Sequence[str]) -> int:
    try:
        _extract(
            arguments,
            os.environ,
            sys.stdout.buffer,
            executed_helper_path=sys.argv[0],
        )
    except BaseException as exc:
        # stdout is deliberately untouched until extraction and every identity
        # recheck have succeeded.  Failure diagnostics are never mixed with
        # the retained canonical JSONL stream.
        sys.stderr.write(
            "cp2_pair_index_extract failed closed: {}: {}\n".format(
                type(exc).__name__, exc
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
