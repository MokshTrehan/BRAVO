#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Standard-library-only CP2 pre-data readiness primitives.

This module has no import-time repository, ROS, registry, or dataset access.
It deliberately treats all tracked worktree bytes as opaque provenance.  In
particular, ``project/datasets.yaml`` is never decoded or parsed here.

The public ``run_readiness_barrier`` function implements the pre-bag portion
of the frozen CP2 readiness contract.  On success it returns a
``ReadinessAuthorization`` whose lock and repository descriptors remain held
until ``close()`` is called.  A caller must copy the returned attachments into
its hidden artifact partial and keep the authorization alive through atomic
finalization.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import resource
import signal
import stat
import struct
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ENTRYPOINTS = (
    "scripts/cp2/run_unit_gate.sh",
    "scripts/cp2/run_recorded_parity.py",
    "scripts/cp2/run_sequence_pair.py",
    "scripts/cp2/run_timing_pair.py",
    "scripts/cp2/verify_report.py",
)
EXPECTED_BRANCH = "schurvio-lite/cp2-one-pass"
CP1_AUTHORIZATION_COMMIT = "8d80f483752411d34a3bc4c1ff6330b3a5c0fef3"
PREAUTHORIZATION_REGISTRY_PATH = "project/datasets.yaml"
POSTAUTHORIZATION_OUTPUT_PARENT = "results/staging/cp2/recorded"
POSTAUTHORIZATION_SOURCE_PATHS = (
    "config/euroc_mav/estimator_config.yaml",
    "config/euroc_mav/kalibr_imu_chain.yaml",
    "config/euroc_mav/kalibr_imucam_chain.yaml",
    "docs/cp2_artifact_schema.md",
    "docs/cp2_c_composite_and_readiness_clarification.md",
    "docs/cp2_c_detached_readiness_binding_clarification_proposed.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_predata_incident_log.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "project/cp1_gate.yaml",
    "project/cp2_c_clarification_approval.json",
    "project/cp2_c_detached_readiness_binding_approval.json",
    "project/cp2_predata_incident_disposition_approval.json",
    "project/cp2_gate.yaml",
    "project/cp2_serial.launch",
    "scripts/cp2/cp2_recorded_campaign.py",
    "scripts/cp2/cp2_schema.py",
    "scripts/cp2/verify_report.py",
)
PREVALIDATED_SOURCE_RECORD_TYPE = "cp2_prevalidated_unit_source_v1"
DATA_LOCK_PATH = "/tmp/schurvio-lite-cp2-data.lock"
SELF_TEST_TIMEOUT_SECONDS = 300.0
_READINESS_GIT_TIMEOUT_SECONDS = 300.0
SELF_TEST_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "CP2_SELF_TEST": "1",
    "CP2_FORBID_BAG_ACCESS": "1",
}

COMMON_SELF_TEST_CASES = (
    "valid_minimal_fixture",
    "cli_exclusivity",
    "forbidden_bag_provider",
    "non_tmp_write",
    "schema_extra_key",
    "schema_missing_key",
    "duplicate_json_key",
    "unsafe_path",
    "symlink",
    "hardlink",
    "manifest_missing_entry",
    "manifest_extra_entry",
    "manifest_digest_mismatch",
    "readiness_order",
    "readiness_timeout",
    "readiness_process_group",
    "readiness_lock_identity",
    "readiness_snapshot_mutation",
    "ignored_source_path",
    "snapshotted_root_symlink",
    "launch_output_combination",
    "unit_anchor_commit_mismatch",
)

RECORDED_SELF_TEST_SUFFIX = (
    "duplicate_noncontiguous_ids", "wrong_terminal_reconciliation",
    "wrong_counted_category", "denominator_mismatch", "raw_row_weight_mismatch",
    "missing_unclassified_disagreement", "bad_statistics_tolerance_edge",
    "missing_state_block", "missing_ordered_covariance_pair",
    "candidate_missing_row_omission", "nonzero_shadow_write",
    "baseline_commit_count_not_one", "live_preview_mismatch",
    "nonzero_repair_fallback", "prior_raw_config_hash_drift",
    "raw_prior_layout_disconnect", "flipped_gate_decision",
    "permuted_accepted_sequence", "candidate_proposal_copied_from_baseline",
    "proposal_disconnected_from_raw_or_phase0", "replay_noop",
    "replay_skipped_invocation", "phase2_phase3_disconnected",
    "replay_report_mismatch", "nonzero_internal_failure_terminal",
    "manifest_corruption",
)
SEQUENCE_SELF_TEST_SUFFIX = (
    "exact_20_ms_boundary", "nearest_not_first_forward", "reused_image_message",
    "missing_duplicate_pair_index", "callback_source_mismatch", "identity_hash_drift",
    "wrong_mode_order", "coverage_below_0_995", "unequal_shared_populations",
    "independent_alignment", "invalid_nonorthogonal_transform",
    "position_metric_limit", "orientation_metric_limit", "relative_ate_metric_limit",
)
TIMING_SELF_TEST_SUFFIX = (
    "wrong_pair_order", "wrong_pair_index", "runtime_drift", "config_drift",
    "profile_drift", "changed_clock_snapshot", "affinity_mismatch",
    "warm_up_boundary_error", "warm_up_u64_overflow",
    "unilateral_noncommon_samples", "omitted_bilateral_common_sample",
    "duplicate_timestamp", "timestamp_u64_overflow", "negative_duration",
    "duration_u64_overflow", "noninteger_duration", "nonprimary_inclusion",
    "incorrect_linear_quantiles", "binary64_quantile_rounding",
    "median_ratio_limit", "p95_ratio_limit",
)


def _ordered_union(*inventories: Sequence[str]) -> Tuple[str, ...]:
    result: List[str] = []
    for inventory in inventories:
        for name in inventory:
            if name not in result:
                result.append(name)
    return tuple(result)


EXPECTED_CASE_NAMES_BY_ENTRYPOINT = {
    ENTRYPOINTS[0]: COMMON_SELF_TEST_CASES,
    ENTRYPOINTS[1]: COMMON_SELF_TEST_CASES + RECORDED_SELF_TEST_SUFFIX,
    ENTRYPOINTS[2]: COMMON_SELF_TEST_CASES + SEQUENCE_SELF_TEST_SUFFIX,
    ENTRYPOINTS[3]: COMMON_SELF_TEST_CASES + TIMING_SELF_TEST_SUFFIX,
    ENTRYPOINTS[4]: _ordered_union(
        COMMON_SELF_TEST_CASES,
        RECORDED_SELF_TEST_SUFFIX,
        SEQUENCE_SELF_TEST_SUFFIX,
        TIMING_SELF_TEST_SUFFIX,
    ),
}

SNAPSHOT_DOMAIN = b"SchurVIO-CP2-readiness-snapshot-v1\0"
COMMAND_ENVIRONMENT_DOMAIN = b"SchurVIO-CP2-command-environment-v1\0"
SNAPSHOT_ROOT_TAGS = frozenset(("source", "build", "results", "testing", "post_lock"))
ALLOWED_OTHER_ROOTS = frozenset(("build", "results", "Testing"))
U64_MAX = (1 << 64) - 1
MAX_SNAPSHOT_ENTRIES = 10_000_000
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_INDEX_BYTES = 256 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 1024 * 1024 * 1024
MAX_UNIT_ARTIFACT_FILE_BYTES = 512 * 1024 * 1024
MAX_UNIT_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_ZERO_SHA256 = b"\0" * 32
_FILE_TYPES = frozenset(("f", "d", "l"))
_GIT_FILE_MODES = frozenset((0o100644, 0o100755))


class ReadinessError(RuntimeError):
    """A fail-closed readiness-contract violation."""


def _fail(message: str) -> None:
    raise ReadinessError(message)


def _checked_u64(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        _fail(label + " is not u64")
    return value


def _checked_add(left: int, right: int, label: str) -> int:
    left = _checked_u64(left, label + " left")
    right = _checked_u64(right, label + " right")
    if left > U64_MAX - right:
        _fail(label + " overflows u64")
    return left + right


def _u64(value: Any, label: str) -> bytes:
    return _checked_u64(value, label).to_bytes(8, "big")


def _lp_bytes(value: bytes, label: str) -> bytes:
    return _u64(len(value), label + " length") + value


def _utf8(value: Any, label: str, forbid_nul: bool = True) -> bytes:
    if not isinstance(value, str):
        _fail(label + " is not a string")
    if forbid_nul and "\0" in value:
        _fail(label + " contains NUL")
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ReadinessError(label + " is not UTF-8") from exc


def _relpath(value: Any, label: str) -> str:
    _utf8(value, label)
    if not value or "\\" in value:
        _fail(label + " is not a normalized POSIX relpath")
    pure = PurePosixPath(value)
    if pure.is_absolute() or str(pure) != value or any(part in ("", ".", "..") for part in pure.parts):
        _fail(label + " is not a normalized POSIX relpath")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        _fail(label + " is not lowercase SHA-256")
    return value


def _hex40(value: Any, label: str) -> str:
    if not isinstance(value, str) or HEX40.fullmatch(value) is None:
        _fail(label + " is not a 40-character lowercase object ID")
    return value


def _stat_signature(value: os.stat_result) -> Tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_nlink,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _same_binding(before: os.stat_result, after: os.stat_result) -> bool:
    return _stat_signature(before) == _stat_signature(after)


def _process_mount_namespace_identity() -> Tuple[int, int, int]:
    """Bind a descriptor owner to one process and one mount namespace."""

    namespace = os.stat("/proc/self/ns/mnt")
    return (os.getpid(), namespace.st_dev, namespace.st_ino)


def _descriptor_absolute_target(descriptor: int, label: str) -> str:
    """Return the exact extant absolute pathname reported for one held fd."""

    if isinstance(descriptor, bool) or not isinstance(descriptor, int) or descriptor < 0:
        _fail(label + " descriptor is invalid")
    target = os.readlink("/proc/self/fd/{}".format(descriptor))
    if (
        not target
        or "\0" in target
        or not os.path.isabs(target)
        or os.path.normpath(target) != target
    ):
        _fail(label + " descriptor target is not normalized absolute")
    return target


def _descriptor_inventory_record(descriptor: int, label: str) -> Dict[str, Any]:
    """Return an immutable-by-copy description of one owned capability fd."""

    return {
        "fd": descriptor,
        "target": _descriptor_absolute_target(descriptor, label),
        "stat_signature": _stat_signature(os.fstat(descriptor)),
        "access_mode": fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE,
        "close_on_exec": bool(
            fcntl.fcntl(descriptor, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        ),
    }


def _reopen_descriptor_on_current_mount(
    descriptor: int, directory: bool, label: str
) -> Tuple[int, str, Tuple[int, ...]]:
    """Reopen a held object's exact pathname through the current mount tree."""

    before = os.fstat(descriptor)
    if stat.S_ISDIR(before.st_mode) != directory:
        _fail(label + " descriptor type differs")
    target = _descriptor_absolute_target(descriptor, label)
    reopened = os.open(target, _directory_flags() if directory else _file_flags())
    try:
        after = os.fstat(reopened)
        signature = _stat_signature(before)
        if (
            _stat_signature(after) != signature
            or _stat_signature(os.fstat(descriptor)) != signature
            or _descriptor_absolute_target(reopened, label + " rebound") != target
        ):
            _fail(label + " changed while rebinding its mount namespace")
        return reopened, target, signature
    except BaseException:
        os.close(reopened)
        raise


def _directory_object_identity(value: os.stat_result) -> Tuple[int, ...]:
    """Return directory fields that cannot legitimately change while populated."""

    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
        value.st_gid,
    )


def _directory_signature_identity(value: Tuple[int, ...]) -> Tuple[int, ...]:
    """Project a ``_stat_signature`` onto immutable directory identity fields."""

    return (value[0], value[1], stat.S_IFMT(value[3]), value[4], value[5])


def _read_fd(fd: int, maximum: Optional[int] = None) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    result = bytearray()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            break
        if maximum is not None and len(result) > maximum - len(block):
            _fail("descriptor content exceeds its bounded maximum")
        result.extend(block)
    return bytes(result)


def _hash_fd(fd: int) -> Tuple[int, str]:
    os.lseek(fd, 0, os.SEEK_SET)
    size = 0
    digest = hashlib.sha256()
    while True:
        block = os.read(fd, 1024 * 1024)
        if not block:
            return size, digest.hexdigest()
        size = _checked_add(size, len(block), "file byte count")
        digest.update(block)


def _strict_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            _fail("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_constant(token: str) -> None:
    _fail("non-JSON constant: " + token)


def _parse_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        _fail("nonfinite JSON number")
    return value


def strict_json_loads(value: bytes) -> Any:
    try:
        text = value.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise ReadinessError("JSON is not UTF-8") from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
    except ReadinessError:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReadinessError("invalid JSON: " + str(exc)) from exc


def _canonical_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def encode_command_environment(variables: Mapping[str, str]) -> bytes:
    if not isinstance(variables, Mapping):
        _fail("command environment is not a mapping")
    records = []
    for name, value in variables.items():
        name_bytes = _utf8(name, "environment name")
        value_bytes = _utf8(value, "environment value")
        records.append((name_bytes, value_bytes))
    records.sort(key=lambda item: item[0])
    payload = bytearray(COMMAND_ENVIRONMENT_DOMAIN)
    payload.extend(_u64(len(records), "environment count"))
    for name, value in records:
        payload.extend(_lp_bytes(name, "environment name"))
        payload.extend(_lp_bytes(value, "environment value"))
    return bytes(payload)


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    entry_type: str
    mode: int
    size: int
    sha256: str


@dataclass(frozen=True)
class EntrypointIdentity:
    path: str
    git_blob: str
    sha256: str
    mode: int
    regular_nonsymlink: bool = True

    def as_record(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "git_blob": self.git_blob,
            "sha256": self.sha256,
            "mode": self.mode,
            "regular_nonsymlink": self.regular_nonsymlink,
        }


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    tree: str
    index_tree: str
    status: bytes
    entrypoints: Tuple[EntrypointIdentity, ...]


def _validated_entries(entries: Iterable[SnapshotEntry]) -> Tuple[SnapshotEntry, ...]:
    result = tuple(entries)
    if len(result) > MAX_SNAPSHOT_ENTRIES:
        _fail("snapshot entry count exceeds bound")
    previous: Optional[bytes] = None
    seen = set()
    for entry in result:
        if not isinstance(entry, SnapshotEntry):
            _fail("snapshot entry has the wrong type")
        path = _relpath(entry.path, "snapshot path")
        encoded_path = path.encode("utf-8")
        if previous is not None and encoded_path <= previous:
            _fail("snapshot paths are not strictly UTF-8-byte sorted")
        previous = encoded_path
        if path in seen:
            _fail("duplicate snapshot path")
        seen.add(path)
        if entry.entry_type not in _FILE_TYPES:
            _fail("snapshot entry has invalid type")
        _checked_u64(entry.mode, "snapshot mode")
        _checked_u64(entry.size, "snapshot size")
        digest = bytes.fromhex(_sha256(entry.sha256, "snapshot SHA-256"))
        if entry.entry_type == "d" and (entry.size != 0 or digest != _ZERO_SHA256):
            _fail("directory snapshot entry must have zero size/hash")
    return result


def encode_snapshot(root_tag: str, exists: bool, entries: Iterable[SnapshotEntry]) -> bytes:
    if root_tag not in SNAPSHOT_ROOT_TAGS:
        _fail("invalid readiness snapshot root tag")
    if not isinstance(exists, bool):
        _fail("snapshot existence flag is not Boolean")
    records = _validated_entries(entries)
    if not exists and records:
        _fail("absent snapshot root has entries")
    payload = bytearray(SNAPSHOT_DOMAIN)
    payload.extend(_lp_bytes(root_tag.encode("ascii"), "root tag"))
    payload.append(1 if exists else 0)
    payload.extend(_u64(len(records), "snapshot entry count"))
    for entry in records:
        payload.extend(_lp_bytes(entry.path.encode("utf-8"), "snapshot path"))
        payload.extend(entry.entry_type.encode("ascii"))
        payload.extend(_u64(entry.mode, "snapshot mode"))
        payload.extend(_u64(entry.size, "snapshot size"))
        payload.extend(bytes.fromhex(entry.sha256))
    return bytes(payload)


class _Reader:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def take(self, count: int, label: str) -> bytes:
        _checked_u64(count, label + " count")
        end = _checked_add(self.offset, count, label + " end")
        if end > len(self.payload):
            _fail(label + " is truncated")
        value = self.payload[self.offset:end]
        self.offset = end
        return value

    def u64(self, label: str) -> int:
        return int.from_bytes(self.take(8, label), "big")

    def lp(self, label: str) -> bytes:
        return self.take(self.u64(label + " length"), label)


def _parse_snapshot_prefix(payload: bytes) -> Tuple[str, bool, Tuple[SnapshotEntry, ...], int]:
    if not isinstance(payload, bytes):
        _fail("snapshot payload is not bytes")
    reader = _Reader(payload)
    if reader.take(len(SNAPSHOT_DOMAIN), "snapshot domain") != SNAPSHOT_DOMAIN:
        _fail("snapshot domain mismatch")
    try:
        root_tag = reader.lp("root tag").decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ReadinessError("snapshot root tag is not ASCII") from exc
    if root_tag not in SNAPSHOT_ROOT_TAGS:
        _fail("snapshot root tag is invalid")
    existence = reader.take(1, "existence byte")
    if existence not in (b"\0", b"\1"):
        _fail("snapshot existence byte is invalid")
    exists = existence == b"\1"
    count = reader.u64("snapshot entry count")
    if count > MAX_SNAPSHOT_ENTRIES:
        _fail("snapshot entry count exceeds bound")
    entries = []
    for index in range(count):
        try:
            path = reader.lp("snapshot path").decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ReadinessError("snapshot path is not UTF-8") from exc
        try:
            entry_type = reader.take(1, "snapshot type").decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise ReadinessError("snapshot type is not ASCII") from exc
        mode = reader.u64("snapshot mode")
        size = reader.u64("snapshot size")
        digest = reader.take(32, "snapshot digest").hex()
        entries.append(SnapshotEntry(path, entry_type, mode, size, digest))
    validated = _validated_entries(entries)
    if not exists and validated:
        _fail("absent snapshot root has entries")
    return root_tag, exists, validated, reader.offset


def parse_snapshot(payload: bytes) -> Tuple[str, bool, Tuple[SnapshotEntry, ...]]:
    root_tag, exists, entries, offset = _parse_snapshot_prefix(payload)
    if offset != len(payload):
        _fail("snapshot payload has trailing bytes")
    return root_tag, exists, entries


def encode_source_snapshot(
    root_tag: str, entries: Iterable[SnapshotEntry], identity: SourceIdentity
) -> bytes:
    if root_tag not in ("source", "post_lock"):
        _fail("source identity is valid only for source/post_lock snapshots")
    source_entries = tuple(entries)
    for entry in source_entries:
        if entry.entry_type != "f" or entry.mode not in _GIT_FILE_MODES:
            _fail("source snapshots contain only normalized Git regular-file entries")
    payload = bytearray(encode_snapshot(root_tag, True, source_entries))
    payload.extend(bytes.fromhex(_hex40(identity.commit, "source commit")))
    payload.extend(bytes.fromhex(_hex40(identity.tree, "source tree")))
    payload.extend(bytes.fromhex(_hex40(identity.index_tree, "index tree")))
    if not isinstance(identity.status, bytes):
        _fail("source status is not bytes")
    payload.extend(_lp_bytes(identity.status, "source status"))
    if tuple(item.path for item in identity.entrypoints) != ENTRYPOINTS:
        _fail("source entrypoint inventory/order is not exact")
    for item in identity.entrypoints:
        payload.extend(_lp_bytes(_relpath(item.path, "entrypoint path").encode("utf-8"), "entrypoint path"))
        payload.extend(bytes.fromhex(_hex40(item.git_blob, "entrypoint blob")))
        payload.extend(bytes.fromhex(_sha256(item.sha256, "entrypoint SHA-256")))
    return bytes(payload)


def parse_source_snapshot(
    payload: bytes,
) -> Tuple[str, Tuple[SnapshotEntry, ...], SourceIdentity]:
    root_tag, exists, entries, offset = _parse_snapshot_prefix(payload)
    if root_tag not in ("source", "post_lock") or not exists:
        _fail("source snapshot prefix is invalid")
    reader = _Reader(payload)
    reader.offset = offset
    commit = reader.take(20, "source commit").hex()
    tree = reader.take(20, "source tree").hex()
    index_tree = reader.take(20, "index tree").hex()
    status = reader.lp("source status")
    entrypoints = []
    entry_by_path = {entry.path: entry for entry in entries}
    for expected_path in ENTRYPOINTS:
        try:
            path = reader.lp("entrypoint path").decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ReadinessError("entrypoint path is not UTF-8") from exc
        if path != expected_path:
            _fail("source entrypoint inventory/order mismatch")
        blob = reader.take(20, "entrypoint blob").hex()
        digest = reader.take(32, "entrypoint SHA-256").hex()
        source_entry = entry_by_path.get(path)
        if source_entry is None or source_entry.entry_type != "f":
            _fail("entrypoint lacks a matching source entry")
        if source_entry.sha256 != digest or source_entry.mode not in _GIT_FILE_MODES:
            _fail("entrypoint/source identity mismatch")
        entrypoints.append(EntrypointIdentity(path, blob, digest, source_entry.mode, True))
    if reader.offset != len(payload):
        _fail("source snapshot has trailing bytes")
    return root_tag, entries, SourceIdentity(commit, tree, index_tree, status, tuple(entrypoints))


def _snapshot_directory(path: Path, root_tag: str) -> bytes:
    path = path.absolute()
    try:
        root_lstat = os.lstat(str(path))
    except FileNotFoundError:
        return encode_snapshot(root_tag, False, ())
    if not stat.S_ISDIR(root_lstat.st_mode) or stat.S_ISLNK(root_lstat.st_mode):
        _fail(root_tag + " snapshot root is not a real directory")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    root_fd = os.open(str(path), flags)
    entries: List[SnapshotEntry] = []
    try:
        if not _same_binding(root_lstat, os.fstat(root_fd)):
            _fail(root_tag + " snapshot root changed while opening")

        def walk(directory_fd: int, prefix: str) -> None:
            try:
                names = os.listdir(directory_fd)
            except OSError as exc:
                raise ReadinessError("cannot enumerate snapshot directory") from exc
            encoded_names = []
            for name in names:
                name_bytes = _utf8(name, "snapshot path component")
                if name in (".", "..") or "/" in name:
                    _fail("unsafe snapshot path component")
                encoded_names.append((name_bytes, name))
            for _, name in sorted(encoded_names):
                relative = name if not prefix else prefix + "/" + name
                before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                permissions = stat.S_IMODE(before.st_mode)
                if stat.S_ISDIR(before.st_mode):
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                    try:
                        if not _same_binding(before, os.fstat(child_fd)):
                            _fail("snapshot directory binding changed: " + relative)
                        entries.append(SnapshotEntry(relative, "d", permissions, 0, _ZERO_SHA256.hex()))
                        walk(child_fd, relative)
                        if not _same_binding(before, os.fstat(child_fd)):
                            _fail("snapshot directory changed: " + relative)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(before.st_mode):
                    file_flags = os.O_RDONLY | os.O_CLOEXEC
                    if hasattr(os, "O_NOFOLLOW"):
                        file_flags |= os.O_NOFOLLOW
                    file_fd = os.open(name, file_flags, dir_fd=directory_fd)
                    try:
                        if not _same_binding(before, os.fstat(file_fd)):
                            _fail("snapshot file binding changed: " + relative)
                        size, digest = _hash_fd(file_fd)
                        if size != before.st_size or not _same_binding(before, os.fstat(file_fd)):
                            _fail("snapshot file changed: " + relative)
                    finally:
                        os.close(file_fd)
                    entries.append(SnapshotEntry(relative, "f", permissions, size, digest))
                elif stat.S_ISLNK(before.st_mode):
                    target = os.readlink(name, dir_fd=directory_fd)
                    target_bytes = os.fsencode(target)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if not _same_binding(before, after):
                        _fail("snapshot symlink changed: " + relative)
                    entries.append(
                        SnapshotEntry(
                            relative,
                            "l",
                            permissions,
                            len(target_bytes),
                            hashlib.sha256(target_bytes).hexdigest(),
                        )
                    )
                else:
                    _fail("special snapshot entry is forbidden: " + relative)

        walk(root_fd, "")
        if not _same_binding(root_lstat, os.fstat(root_fd)):
            _fail(root_tag + " snapshot root changed")
    finally:
        os.close(root_fd)
    entries.sort(key=lambda item: item.path.encode("utf-8"))
    return encode_snapshot(root_tag, True, entries)


def validate_self_test_result(
    result: Any,
    expected_entrypoint: str,
    expected_case_names: Sequence[str],
    mandatory_case_names: Sequence[str] = COMMON_SELF_TEST_CASES,
) -> Dict[str, Any]:
    if not isinstance(result, dict):
        _fail("self-test result is not an object")
    exact_keys = {
        "schema_version",
        "record_type",
        "entrypoint",
        "temporary_root",
        "bag_provider_calls",
        "cases",
        "case_count",
        "passed",
    }
    if set(result) != exact_keys:
        _fail("self-test result key inventory is not exact")
    if (
        isinstance(result.get("schema_version"), bool)
        or result.get("schema_version") != 1
        or result.get("record_type") != "self_test_result"
    ):
        _fail("self-test result identity is invalid")
    if result.get("entrypoint") != expected_entrypoint:
        _fail("self-test result entrypoint mismatch")
    temporary_root = result.get("temporary_root")
    if not isinstance(temporary_root, str) or "\0" in temporary_root:
        _fail("self-test temporary_root is invalid")
    root_path = Path(temporary_root)
    if not root_path.is_absolute() or root_path.parent != Path("/tmp"):
        _fail("self-test temporary_root is not a direct child of /tmp")
    if os.path.lexists(temporary_root):
        _fail("self-test temporary_root still exists")
    if isinstance(result.get("bag_provider_calls"), bool) or result.get("bag_provider_calls") != 0:
        _fail("self-test reports a bag-provider call")
    expected = tuple(expected_case_names)
    if (
        not expected
        or any(not isinstance(name, str) for name in expected)
        or len(set(expected)) != len(expected)
    ):
        _fail("expected self-test case inventory is empty or duplicated")
    if not set(mandatory_case_names).issubset(set(expected)):
        _fail("expected self-test inventory omits a mandatory case")
    cases = result.get("cases")
    if (
        not isinstance(cases, list)
        or isinstance(result.get("case_count"), bool)
        or not isinstance(result.get("case_count"), int)
        or result.get("case_count") != len(cases)
    ):
        _fail("self-test case count is invalid")
    if result.get("case_count") != len(expected):
        _fail("self-test case count differs from expected inventory")
    names = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or set(case) != {
            "index", "name", "expected_rejection", "observed_rejection", "passed"
        }:
            _fail("self-test case key inventory is not exact")
        if case.get("index") != index or isinstance(case.get("index"), bool):
            _fail("self-test case index is not contiguous")
        name = case.get("name")
        if not isinstance(name, str):
            _fail("self-test case name is not a string")
        names.append(name)
        negative = name != "valid_minimal_fixture"
        if case.get("expected_rejection") is not negative:
            _fail("self-test expected_rejection is wrong")
        if case.get("observed_rejection") is not negative or case.get("passed") is not True:
            _fail("self-test case did not produce its exact outcome")
    if tuple(names) != expected:
        _fail("self-test case names/order differ from expected inventory")
    if names.count("valid_minimal_fixture") != 1 or result.get("passed") is not True:
        _fail("self-test aggregate result is not passing")
    return result


@dataclass
class _TrackedIndexEntry:
    path: str
    path_bytes: bytes
    mode: int
    oid: str
    size_hint: int


def _parse_git_config(content: bytes) -> Dict[Tuple[str, str, str], str]:
    if len(content) > MAX_CONFIG_BYTES or b"\0" in content:
        _fail("Git config exceeds bound or contains NUL")
    try:
        lines = content.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as exc:
        raise ReadinessError("Git config is not UTF-8") from exc
    section = ""
    subsection = ""
    values: Dict[Tuple[str, str, str], str] = {}
    section_pattern = re.compile(r'^\[([A-Za-z0-9.-]+)(?:[ \t]+"([^"\\]*)")?\][ \t]*(?:[#;].*)?$')
    key_pattern = re.compile(r"^([A-Za-z][A-Za-z0-9.-]*)[ \t]*(?:=[ \t]*(.*?))?[ \t]*$")
    for number, raw in enumerate(lines, 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        if raw.rstrip().endswith("\\"):
            _fail("Git config continuation is unsupported")
        if stripped.startswith("["):
            match = section_pattern.fullmatch(stripped)
            if match is None:
                _fail("unsupported Git config section syntax at line {}".format(number))
            section = match.group(1).lower()
            subsection = (match.group(2) or "").lower()
            if section in ("include", "includeif", "alias", "filter", "diff"):
                _fail("dangerous Git config section: " + section)
            continue
        if not section:
            _fail("Git config key appears outside a section")
        match = key_pattern.fullmatch(stripped)
        if match is None:
            _fail("unsupported Git config key syntax at line {}".format(number))
        key = match.group(1).lower()
        value = (match.group(2) if match.group(2) is not None else "true").strip()
        identity = (section, subsection, key)
        if identity in values:
            _fail("duplicate Git config key")
        values[identity] = value

    def boolean(value: str, label: str) -> bool:
        lowered = value.lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
        _fail(label + " is not a supported Git Boolean")
        return False

    dangerous_core = {
        "worktree", "excludesfile", "attributesfile", "hookspath", "fsmonitor"
    }
    for (section_name, subsection_name, key), value in values.items():
        del subsection_name
        if section_name == "core" and key in dangerous_core:
            _fail("dangerous Git core configuration: " + key)
        if section_name == "core" and key == "bare" and boolean(value, "core.bare"):
            _fail("bare Git repository is forbidden")
        if section_name == "core" and key == "filemode" and not boolean(value, "core.filemode"):
            _fail("core.filemode must be true")
        if section_name == "core" and key in ("sparsecheckout", "sparsecheckoutcone", "ignorecase"):
            if boolean(value, "core." + key):
                _fail("dangerous Git core Boolean: " + key)
        if section_name == "extensions" and key in (
            "sparseindex", "worktreeconfig", "partialclone"
        ):
            if key == "partialclone":
                _fail("partial-clone extension is forbidden")
            if boolean(value, "extensions." + key):
                _fail("dangerous Git extension: " + key)
        if section_name == "extensions" and key == "objectformat" and value.lower() != "sha1":
            _fail("only SHA-1 Git object format is supported")
        if section_name == "remote" and key in ("promisor", "partialclonefilter"):
            if key != "promisor" or boolean(value, "remote.promisor"):
                _fail("partial-clone/promisor configuration is forbidden")
        if section_name == "submodule" and key == "recurse" and boolean(value, "submodule.recurse"):
            _fail("submodule recursion is forbidden")
    filemode = values.get(("core", "", "filemode"))
    if filemode is None or not boolean(filemode, "core.filemode"):
        _fail("core.filemode=true is required")
    return values


def _parse_git_index(content: bytes) -> Tuple[_TrackedIndexEntry, ...]:
    if len(content) > MAX_INDEX_BYTES or len(content) < 32:
        _fail("Git index size is invalid")
    if hashlib.sha1(content[:-20]).digest() != content[-20:]:
        _fail("Git index checksum mismatch")
    if content[:4] != b"DIRC":
        _fail("Git index signature mismatch")
    version, count = struct.unpack(">II", content[4:12])
    if version not in (2, 3):
        _fail("only Git index versions 2 and 3 are supported")
    if count > MAX_SNAPSHOT_ENTRIES:
        _fail("Git index entry count exceeds bound")
    offset = 12
    end_entries = len(content) - 20
    entries = []
    prior_path: Optional[bytes] = None
    for _ in range(count):
        start = offset
        if offset + 62 > end_entries:
            _fail("Git index entry is truncated")
        fields = struct.unpack(">10I20sH", content[offset:offset + 62])
        mode = fields[6]
        size_hint = fields[9]
        oid = fields[10].hex()
        flags = fields[11]
        offset += 62
        stage = (flags >> 12) & 3
        if stage != 0:
            _fail("non-stage-0 Git index entry is forbidden")
        if flags & 0x8000:
            _fail("assume-valid Git index entry is forbidden")
        if flags & 0x4000:
            if version < 3 or offset + 2 > end_entries:
                _fail("invalid extended Git index entry")
            extended_flags = struct.unpack(">H", content[offset:offset + 2])[0]
            offset += 2
            if extended_flags != 0:
                _fail("skip-worktree/intent-to-add index entry is forbidden")
        terminator = content.find(b"\0", offset, end_entries)
        if terminator < 0:
            _fail("Git index pathname is unterminated")
        path_bytes = content[offset:terminator]
        declared_length = flags & 0x0FFF
        if declared_length < 0x0FFF and declared_length != len(path_bytes):
            _fail("Git index pathname length mismatch")
        entry_length = terminator + 1 - start
        padding = (-entry_length) % 8
        offset = terminator + 1 + padding
        if offset > end_entries or any(content[terminator + 1:offset]):
            _fail("Git index padding is invalid")
        try:
            path = path_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ReadinessError("tracked Git path is not UTF-8") from exc
        _relpath(path, "tracked Git path")
        if path == ".git" or ".git" in PurePosixPath(path).parts:
            _fail("tracked .git path is forbidden")
        if mode not in _GIT_FILE_MODES:
            _fail("Git index entry is not a regular-file mode")
        if prior_path is not None and path_bytes <= prior_path:
            _fail("Git index paths are not strictly byte sorted")
        if prior_path is not None and path_bytes.startswith(prior_path + b"/"):
            _fail("Git index file/directory prefix conflict")
        prior_path = path_bytes
        entries.append(_TrackedIndexEntry(path, path_bytes, mode, oid, size_hint))
    while offset < end_entries:
        if offset + 8 > end_entries:
            _fail("Git index extension is truncated")
        signature = content[offset:offset + 4]
        size = struct.unpack(">I", content[offset + 4:offset + 8])[0]
        offset += 8
        if offset + size > end_entries:
            _fail("Git index extension payload is truncated")
        if signature != b"TREE":
            _fail("unsupported or dangerous Git index extension: " + repr(signature))
        offset += size
    if offset != end_entries:
        _fail("Git index framing mismatch")
    return tuple(entries)


@dataclass
class _BoundFile:
    relative: str
    fd: int
    signature: Tuple[int, ...]
    digest: Optional[str]


@dataclass
class _BoundDirectory:
    relative: str
    fd: int
    signature: Tuple[int, ...]


@dataclass(frozen=True)
class AuthorizedOutputStaging:
    """The one exact CP2-C output namespace opened after authorization."""

    run_id: str
    parent: Path
    partial: Path
    final: Path


@dataclass
class _OwnedRawProcess:
    """Mutable parent-side state for one raw-fork process-group lifecycle."""

    pid: int
    status: Optional[int] = None
    process_group_complete: bool = False


def _directory_flags() -> int:
    value = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        value |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        value |= os.O_NOFOLLOW
    return value


def _file_flags() -> int:
    value = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        value |= os.O_NOFOLLOW
    return value


@dataclass(frozen=True)
class _PrivateTreeEntry:
    """One exact, private-readiness-tree identity retained after step 8."""

    relative: str
    entry_type: str
    signature: Tuple[int, ...]
    sha256: Optional[str]


class _PrivateTreeSeal:
    """Descriptor-held identity/inventory seal for readiness-owned state.

    The readiness authorization remains live after the barrier returns.  This
    seal makes that lifetime explicit: the private root stays descriptor-bound,
    and every frozen-unit or readiness-attachment leaf is re-opened without
    following links, re-fstat'ed, and re-hashed before recorded access.
    """

    def __init__(
        self,
        root: Path,
        root_fd: int,
        root_signature: Tuple[int, ...],
        entries: Tuple[_PrivateTreeEntry, ...],
        attachment_paths: Tuple[str, ...],
    ) -> None:
        self.root = Path(root).absolute()
        self.root_fd = root_fd
        self.root_signature = root_signature
        self.entries = entries
        self.attachment_paths = attachment_paths
        self._descriptor_origin = _process_mount_namespace_identity()
        self._worker_mount_namespace_rebound = False
        self._closed = False

    @staticmethod
    def _expected_mode(relative: str, entry_type: str) -> int:
        frozen = (
            relative == "unit-artifact-frozen"
            or relative.startswith("unit-artifact-frozen/")
        )
        if entry_type == "d":
            return 0o555 if frozen else 0o700
        if frozen:
            return (
                0o555
                if relative.startswith("unit-artifact-frozen/binaries/")
                else 0o444
            )
        return 0o600

    @classmethod
    def _scan(cls, root_fd: int) -> Tuple[_PrivateTreeEntry, ...]:
        records: List[_PrivateTreeEntry] = []
        owner = os.geteuid()

        def walk(directory_fd: int, prefix: str) -> None:
            directory_before = os.fstat(directory_fd)
            names = sorted(
                os.listdir(directory_fd),
                key=lambda name: _utf8(name, "private readiness path component"),
            )
            for name in names:
                if not name or name in (".", "..") or "/" in name or "\0" in name:
                    _fail("private readiness tree contains an unsafe path component")
                relative = name if not prefix else prefix + "/" + name
                _relpath(relative, "private readiness path")
                before = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if before.st_uid != owner:
                    _fail("private readiness entry has the wrong owner: " + relative)
                if stat.S_ISDIR(before.st_mode):
                    if stat.S_IMODE(before.st_mode) != cls._expected_mode(relative, "d"):
                        _fail("private readiness directory mode differs: " + relative)
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(before, os.fstat(child_fd)):
                            _fail(
                                "private readiness directory changed while opening: "
                                + relative
                            )
                        records.append(
                            _PrivateTreeEntry(
                                relative, "d", _stat_signature(before), None
                            )
                        )
                        walk(child_fd, relative)
                        if not _same_binding(before, os.fstat(child_fd)):
                            _fail(
                                "private readiness directory changed while scanning: "
                                + relative
                            )
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        _fail(
                            "private readiness file is hardlinked: " + relative
                        )
                    if stat.S_IMODE(before.st_mode) != cls._expected_mode(relative, "f"):
                        _fail("private readiness file mode differs: " + relative)
                    descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(before, os.fstat(descriptor)):
                            _fail(
                                "private readiness file changed while opening: "
                                + relative
                            )
                        size, digest = _hash_fd(descriptor)
                        if (
                            size != before.st_size
                            or not _same_binding(before, os.fstat(descriptor))
                        ):
                            _fail(
                                "private readiness file changed while hashing: "
                                + relative
                            )
                    finally:
                        os.close(descriptor)
                    records.append(
                        _PrivateTreeEntry(
                            relative, "f", _stat_signature(before), digest
                        )
                    )
                else:
                    _fail(
                        "private readiness tree contains a link or special entry: "
                        + relative
                    )
                if len(records) > MAX_SNAPSHOT_ENTRIES:
                    _fail("private readiness entry count exceeds bound")
            if not _same_binding(directory_before, os.fstat(directory_fd)):
                _fail("private readiness directory changed during traversal")

        walk(root_fd, "")
        return tuple(records)

    @staticmethod
    def _validate_paths(
        root: Path,
        entries: Sequence[_PrivateTreeEntry],
        attachments: Mapping[str, Path],
        frozen_unit_artifact: Path,
    ) -> Tuple[str, ...]:
        if not isinstance(attachments, Mapping):
            _fail("readiness attachment inventory is not a mapping")
        attachment_paths = tuple(
            sorted(attachments, key=lambda value: _utf8(value, "attachment path"))
        )
        if not attachment_paths:
            _fail("readiness attachment inventory is empty")
        for relative in attachment_paths:
            _relpath(relative, "readiness attachment path")
            if not relative.startswith("readiness/"):
                _fail("readiness attachment leaves its namespace")
            expected = root.joinpath(*PurePosixPath(relative).parts)
            supplied = Path(attachments[relative])
            if (
                not supplied.is_absolute()
                or os.path.normpath(str(supplied)) != str(supplied)
                or supplied != expected
            ):
                _fail("readiness attachment path binding differs: " + relative)

        frozen = Path(frozen_unit_artifact)
        expected_frozen = root / "unit-artifact-frozen"
        if (
            not frozen.is_absolute()
            or os.path.normpath(str(frozen)) != str(frozen)
            or frozen != expected_frozen
        ):
            _fail("frozen unit artifact leaves the held readiness root")

        by_path = {record.relative: record for record in entries}
        if len(by_path) != len(entries):
            _fail("private readiness inventory contains duplicate paths")
        if set(path.split("/", 1)[0] for path in by_path) != {
            "git-home", "git-tmp", "readiness", "unit-artifact-frozen"
        }:
            _fail("private readiness top-level namespace differs")
        for private_git_directory in ("git-home", "git-tmp"):
            record = by_path.get(private_git_directory)
            if record is None or record.entry_type != "d" or any(
                path.startswith(private_git_directory + "/") for path in by_path
            ):
                _fail("private Git directory is missing, nonempty, or invalid")
        if (
            by_path.get("readiness") is None
            or by_path["readiness"].entry_type != "d"
            or by_path.get("unit-artifact-frozen") is None
            or by_path["unit-artifact-frozen"].entry_type != "d"
        ):
            _fail("private readiness/frozen-unit directory inventory differs")
        readiness_files = {
            path
            for path, record in by_path.items()
            if path.startswith("readiness/") and record.entry_type == "f"
        }
        if readiness_files != set(attachment_paths):
            _fail("private readiness attachment file population differs")
        if not any(
            path.startswith("unit-artifact-frozen/") and record.entry_type == "f"
            for path, record in by_path.items()
        ):
            _fail("frozen unit artifact contains no regular files")
        return attachment_paths

    @classmethod
    def capture(
        cls,
        root: Path,
        attachments: Mapping[str, Path],
        frozen_unit_artifact: Path,
    ) -> "_PrivateTreeSeal":
        root = Path(root).absolute()
        before = os.lstat(str(root))
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o700
        ):
            _fail("private readiness root identity/mode differs")
        descriptor = os.open(str(root), _directory_flags())
        try:
            if not _same_binding(before, os.fstat(descriptor)):
                _fail("private readiness root changed while sealing")
            entries = cls._scan(descriptor)
            attachment_paths = cls._validate_paths(
                root, entries, attachments, frozen_unit_artifact
            )
            if not _same_binding(before, os.fstat(descriptor)):
                _fail("private readiness root changed while sealing")
            return cls(
                root,
                descriptor,
                _stat_signature(before),
                entries,
                attachment_paths,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def revalidate(
        self,
        attachments: Mapping[str, Path],
        frozen_unit_artifact: Path,
    ) -> None:
        if self._closed:
            _fail("private readiness tree seal is closed")
        held = os.fstat(self.root_fd)
        current = os.lstat(str(self.root))
        if (
            _stat_signature(held) != self.root_signature
            or _stat_signature(current) != self.root_signature
            or not stat.S_ISDIR(held.st_mode)
            or stat.S_ISLNK(current.st_mode)
        ):
            _fail("held private readiness root identity changed")
        observed = self._scan(self.root_fd)
        if observed != self.entries:
            _fail("private readiness tree identity/inventory changed")
        attachment_paths = self._validate_paths(
            self.root, observed, attachments, frozen_unit_artifact
        )
        if attachment_paths != self.attachment_paths:
            _fail("readiness attachment mapping changed")
        if (
            _stat_signature(os.fstat(self.root_fd)) != self.root_signature
            or _stat_signature(os.lstat(str(self.root))) != self.root_signature
        ):
            _fail("held private readiness root changed during revalidation")

    def descriptor_inventory(self) -> Dict[str, Dict[str, Any]]:
        """Expose the exact private-tree descriptors owned by this seal."""

        if self._closed:
            _fail("private readiness tree seal is closed")
        return {
            "private_tree.root": _descriptor_inventory_record(
                self.root_fd, "private readiness root"
            )
        }

    def rebind_worker_mount_namespace(
        self,
        attachments: Mapping[str, Path],
        frozen_unit_artifact: Path,
    ) -> None:
        """Replace the inherited root fd with one opened on the worker mount."""

        if self._closed:
            _fail("private readiness tree seal is closed")
        if self._worker_mount_namespace_rebound:
            _fail("private readiness descriptors were already rebound")
        current_origin = _process_mount_namespace_identity()
        if (
            current_origin[0] == self._descriptor_origin[0]
            or current_origin[1:] == self._descriptor_origin[1:]
        ):
            _fail("private readiness rebinding requires a child mount namespace")
        self.revalidate(attachments, frozen_unit_artifact)
        original = self.root_fd
        reopened, _, signature = _reopen_descriptor_on_current_mount(
            original, True, "private readiness root"
        )
        try:
            if signature != self.root_signature:
                _fail("private readiness rebound identity differs from its seal")
            self.root_fd = reopened
            self._worker_mount_namespace_rebound = True
            reopened = -1
            os.close(original)
            self.revalidate(attachments, frozen_unit_artifact)
        finally:
            if reopened >= 0:
                os.close(reopened)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptor = self.root_fd
        self.root_fd = -1
        os.close(descriptor)


class OpaqueGitRepository:
    """Descriptor-bound, fail-closed Git/worktree provenance guard."""

    def __init__(self, repo_root: Path, temporary_root: Path):
        self.repo_root = Path(repo_root).absolute()
        self.temporary_root = Path(temporary_root).absolute()
        self.root_fd = -1
        self.git_fd = -1
        self._child_root_fd = -1
        self._child_git_fd = -1
        self._bound_files: List[_BoundFile] = []
        self._bound_directories: List[_BoundDirectory] = []
        self._tracked: Tuple[_TrackedIndexEntry, ...] = ()
        self._tracked_fds: Dict[str, _BoundFile] = {}
        self._root_signature: Optional[Tuple[int, ...]] = None
        self._git_signature: Optional[Tuple[int, ...]] = None
        self._admin_snapshot: Dict[str, Tuple[int, ...]] = {}
        self._object_snapshot: Dict[str, Tuple[int, ...]] = {}
        self._worktree_namespace: Dict[str, Tuple[int, ...]] = {}
        self._source_records: Tuple[SnapshotEntry, ...] = ()
        self._commit = ""
        self._tree = ""
        self._status = b""
        self._entrypoints: Tuple[EntrypointIdentity, ...] = ()
        self._git_private_directories: Dict[str, Tuple[int, ...]] = {}
        self._git_commands: List[Dict[str, Any]] = []
        self._git_outputs: Dict[str, bytes] = {}
        self._registry_read = False
        self._postauthorization_output_relative: Optional[str] = None
        self._postauthorization_output_fd = -1
        self._postauthorization_output_identity: Optional[Tuple[int, ...]] = None
        self._postauthorization_output_created_ancestors: Tuple[str, ...] = ()
        self._postauthorization_output_pre_namespace: Optional[
            Dict[str, Tuple[int, ...]]
        ] = None
        self._descriptor_origin = _process_mount_namespace_identity()
        self._worker_mount_namespace_rebound = False
        self._closed = False
        try:
            self._open_and_validate()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        descriptors: List[int] = []
        for item in self._bound_files:
            descriptors.append(item.fd)
            item.fd = -1
        for item in self._bound_directories:
            descriptors.append(item.fd)
            item.fd = -1
        for item in self._tracked_fds.values():
            descriptors.append(item.fd)
            item.fd = -1
        for attribute in (
            "_postauthorization_output_fd",
            "_child_root_fd",
            "_child_git_fd",
            "git_fd",
            "root_fd",
        ):
            descriptors.append(getattr(self, attribute))
            setattr(self, attribute, -1)

        # Clear every ownership slot before the first close syscall.  A close
        # may have taken effect even when an asynchronous BaseException is
        # delivered immediately afterwards, so retrying that numeric fd could
        # close an unrelated descriptor.  Still attempt every other distinct
        # descriptor and report the first non-OSError interruption afterwards.
        first_error: Optional[BaseException] = None
        seen = set()
        for descriptor in descriptors:
            if descriptor < 0 or descriptor in seen:
                continue
            seen.add(descriptor)
            try:
                os.close(descriptor)
            except OSError:
                pass
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "OpaqueGitRepository":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _owned_descriptor_slots(
        self,
    ) -> List[Tuple[str, int, bool, Any, str]]:
        """Return every descriptor-owning slot in a deterministic inventory."""

        slots: List[Tuple[str, int, bool, Any, str]] = [
            ("repository.root", self.root_fd, True, self, "root_fd"),
            ("repository.git", self.git_fd, True, self, "git_fd"),
            (
                "repository.child_root",
                self._child_root_fd,
                True,
                self,
                "_child_root_fd",
            ),
            (
                "repository.child_git",
                self._child_git_fd,
                True,
                self,
                "_child_git_fd",
            ),
        ]
        for item in self._bound_files:
            slots.append((
                "repository.bound_file/" + item.relative,
                item.fd,
                False,
                item,
                "fd",
            ))
        for item in self._bound_directories:
            slots.append((
                "repository.bound_directory/" + item.relative,
                item.fd,
                True,
                item,
                "fd",
            ))
        for relative in sorted(
            self._tracked_fds, key=lambda value: _utf8(value, "tracked fd path")
        ):
            item = self._tracked_fds[relative]
            slots.append((
                "repository.tracked_file/" + relative,
                item.fd,
                False,
                item,
                "fd",
            ))
        if self._postauthorization_output_fd >= 0:
            slots.append((
                "repository.output_partial",
                self._postauthorization_output_fd,
                True,
                self,
                "_postauthorization_output_fd",
            ))
        labels = [item[0] for item in slots]
        descriptors = [item[1] for item in slots]
        if (
            any(descriptor < 0 for descriptor in descriptors)
            or len(set(labels)) != len(labels)
            or len(set(descriptors)) != len(descriptors)
        ):
            _fail("repository descriptor inventory is invalid or aliased")
        return slots

    def descriptor_inventory(self) -> Dict[str, Dict[str, Any]]:
        """Expose every descriptor capability owned by the repository guard."""

        if self._closed:
            _fail("repository guard is closed")
        return {
            label: _descriptor_inventory_record(descriptor, label)
            for label, descriptor, _, _, _ in self._owned_descriptor_slots()
        }

    def rebind_worker_mount_namespace(self) -> None:
        """Atomically replace inherited fds with current-mount read handles."""

        if self._closed:
            _fail("repository guard is closed")
        if self._worker_mount_namespace_rebound:
            _fail("repository descriptors were already rebound")
        current_origin = _process_mount_namespace_identity()
        if (
            current_origin[0] == self._descriptor_origin[0]
            or current_origin[1:] == self._descriptor_origin[1:]
        ):
            _fail("repository rebinding requires a child mount namespace")
        self._validate_bindings()
        slots = self._owned_descriptor_slots()
        reopened: List[
            Tuple[Tuple[str, int, bool, Any, str], int, str, Tuple[int, ...]]
        ] = []
        committed = False
        try:
            for slot in slots:
                label, descriptor, directory, _, _ = slot
                rebound_fd, target, signature = _reopen_descriptor_on_current_mount(
                    descriptor, directory, label
                )
                reopened.append((slot, rebound_fd, target, signature))
            self._validate_bindings()
            for slot, rebound_fd, target, signature in reopened:
                label, descriptor, _, _, _ = slot
                if (
                    _stat_signature(os.fstat(descriptor)) != signature
                    or _stat_signature(os.fstat(rebound_fd)) != signature
                    or _descriptor_absolute_target(descriptor, label) != target
                    or _descriptor_absolute_target(rebound_fd, label + " rebound")
                    != target
                ):
                    _fail("repository descriptor changed during atomic rebinding")

            for slot, rebound_fd, _, _ in reopened:
                _, _, _, owner, attribute = slot
                setattr(owner, attribute, rebound_fd)
            self._worker_mount_namespace_rebound = True
            committed = True

            close_errors: List[BaseException] = []
            for slot, _, _, _ in reopened:
                try:
                    os.close(slot[1])
                except BaseException as exc:
                    close_errors.append(exc)
            if close_errors:
                _fail("an inherited repository descriptor could not be closed")
            self._validate_bindings()
        finally:
            if not committed:
                for _, descriptor, _, _ in reopened:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _output_staging_identity(
        self, staging: AuthorizedOutputStaging
    ) -> Tuple[int, ...]:
        """Return the exact immutable identity of the registered hidden root."""

        expected_parent = self.repo_root.joinpath(
            *PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        expected_relative = (
            POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.partial.name
            if isinstance(staging, AuthorizedOutputStaging)
            else None
        )
        if (
            not isinstance(staging, AuthorizedOutputStaging)
            or self._postauthorization_output_fd < 0
            or self._postauthorization_output_identity is None
            or self._postauthorization_output_relative != expected_relative
            or staging.parent != expected_parent
            or staging.partial.parent != expected_parent
            or staging.final.parent != expected_parent
            or staging.final.name != staging.run_id
        ):
            _fail("postauthorization output staging token differs")
        self._validate_bindings()
        held = os.fstat(self._postauthorization_output_fd)
        by_path = os.lstat(str(staging.partial))
        expected = self._postauthorization_output_identity
        if (
            not stat.S_ISDIR(held.st_mode)
            or stat.S_ISLNK(by_path.st_mode)
            or _directory_object_identity(held) != expected
            or _directory_object_identity(by_path) != expected
        ):
            _fail("postauthorization output staging identity changed")
        return expected

    def _duplicate_output_partial_fd(
        self, staging: AuthorizedOutputStaging
    ) -> int:
        """Duplicate the exact held partial for trusted-parent failure sealing."""

        self._output_staging_identity(staging)
        held = self._postauthorization_output_fd
        descriptor = fcntl.fcntl(held, fcntl.F_DUPFD_CLOEXEC, 0)
        try:
            if _stat_signature(os.fstat(descriptor)) != _stat_signature(os.fstat(held)):
                _fail("duplicated output-partial identity differs")
            self._output_staging_identity(staging)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _output_publication_location(
        self, staging: AuthorizedOutputStaging
    ) -> Tuple[str, int]:
        """Return the exact held root name and permission mode."""

        expected_parent = self.repo_root.joinpath(
            *PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        partial_relative = (
            POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.partial.name
            if isinstance(staging, AuthorizedOutputStaging)
            else None
        )
        final_relative = (
            POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.final.name
            if isinstance(staging, AuthorizedOutputStaging)
            else None
        )
        if (
            not isinstance(staging, AuthorizedOutputStaging)
            or self._postauthorization_output_fd < 0
            or self._postauthorization_output_identity is None
            or self._postauthorization_output_relative
            not in (partial_relative, final_relative)
            or staging.parent != expected_parent
            or staging.partial.parent != expected_parent
            or staging.final.parent != expected_parent
            or staging.final.name != staging.run_id
        ):
            _fail("postauthorization output publication token differs")
        held = os.fstat(self._postauthorization_output_fd)
        expected_identity = self._postauthorization_output_identity
        if (
            not stat.S_ISDIR(held.st_mode)
            or _directory_object_identity(held) != expected_identity
        ):
            _fail("postauthorization output held identity changed")
        location = (
            "hidden"
            if self._postauthorization_output_relative == partial_relative
            else "published"
        )
        parent_relative = "worktree/" + POSTAUTHORIZATION_OUTPUT_PARENT
        parents = [
            bound for bound in self._bound_directories
            if bound.relative == parent_relative
        ]
        if len(parents) != 1:
            _fail("postauthorization output parent descriptor population differs")
        parent_fd = fcntl.fcntl(
            parents[0].fd, fcntl.F_DUPFD_CLOEXEC, 0
        )
        try:
            parent_status = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_status.st_mode)
                or _directory_signature_identity(
                    _stat_signature(parent_status)
                )
                != _directory_signature_identity(parents[0].signature)
            ):
                _fail("postauthorization output parent identity changed")
            current_name = (
                staging.partial.name
                if location == "hidden"
                else staging.final.name
            )
            other_name = (
                staging.final.name
                if location == "hidden"
                else staging.partial.name
            )
            by_name = os.stat(
                current_name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                stat.S_ISLNK(by_name.st_mode)
                or not stat.S_ISDIR(by_name.st_mode)
                or _directory_object_identity(by_name) != expected_identity
                or stat.S_IMODE(by_name.st_mode) not in (0o555, 0o700)
            ):
                _fail("postauthorization output current-name binding changed")
            try:
                other_status = os.stat(
                    other_name, dir_fd=parent_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                other_status = None
            if (
                other_status is not None
                and _directory_object_identity(other_status)
                == expected_identity
            ):
                _fail("postauthorization output inode is bound at both names")
            return location, stat.S_IMODE(by_name.st_mode)
        finally:
            os.close(parent_fd)

    def _duplicate_output_root_fd(
        self, staging: AuthorizedOutputStaging
    ) -> int:
        """Duplicate the held output root at either authoritative name."""

        location = self._output_publication_location(staging)
        held = self._postauthorization_output_fd
        descriptor = fcntl.fcntl(held, fcntl.F_DUPFD_CLOEXEC, 0)
        try:
            if _stat_signature(os.fstat(descriptor)) != _stat_signature(
                os.fstat(held)
            ):
                _fail("duplicated output-root identity differs")
            if self._output_publication_location(staging) != location:
                _fail("output-root location changed during duplication")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _open_regular_at(
        self, parent_fd: int, name: str, relative: str, owner: int,
        maximum: Optional[int] = None, retain: bool = True,
    ) -> Tuple[int, bytes, os.stat_result]:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _fail(relative + " is not a single-link regular file")
        if before.st_uid != owner or before.st_mode & stat.S_IWOTH:
            _fail(relative + " has unsafe owner/permissions")
        descriptor = os.open(name, _file_flags(), dir_fd=parent_fd)
        try:
            after_open = os.fstat(descriptor)
            if not _same_binding(before, after_open):
                _fail(relative + " changed while opening")
            content = _read_fd(descriptor, maximum)
            if not _same_binding(before, os.fstat(descriptor)):
                _fail(relative + " changed while reading")
            if retain:
                self._bound_files.append(
                    _BoundFile(relative, descriptor, _stat_signature(before), hashlib.sha256(content).hexdigest())
                )
                descriptor = -1
            return descriptor, content, before
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _walk_metadata(
        self,
        root_fd: int,
        skip_first: Optional[str],
        object_rules: bool,
        retain_directories: bool = False,
        namespace: str = "git",
    ) -> Dict[str, Tuple[int, ...]]:
        records: Dict[str, Tuple[int, ...]] = {}

        def walk(directory_fd: int, prefix: str) -> None:
            names = sorted(os.listdir(directory_fd), key=lambda item: _utf8(item, "Git path component"))
            for name in names:
                if not prefix and skip_first is not None and name == skip_first:
                    continue
                relative = name if not prefix else prefix + "/" + name
                status_value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                records[relative] = _stat_signature(status_value)
                if stat.S_ISDIR(status_value.st_mode):
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(status_value, os.fstat(child_fd)):
                            _fail("Git directory changed while opening: " + relative)
                        walk(child_fd, relative)
                    finally:
                        if retain_directories:
                            self._bound_directories.append(
                                _BoundDirectory(
                                    namespace + "/" + relative,
                                    child_fd,
                                    _stat_signature(status_value),
                                )
                            )
                        else:
                            os.close(child_fd)
                elif stat.S_ISREG(status_value.st_mode):
                    if status_value.st_nlink != 1 or status_value.st_uid != self._git_signature[4]:
                        _fail("unsafe Git administrative/object file: " + relative)
                    if status_value.st_mode & stat.S_IWOTH:
                        _fail("world-writable Git file: " + relative)
                    if object_rules:
                        self._validate_object_name(relative)
                else:
                    _fail("Git symlink or special file is forbidden: " + relative)

        walk(root_fd, "")
        return records

    @staticmethod
    def _validate_object_name(relative: str) -> None:
        parts = relative.split("/")
        if len(parts) == 2 and re.fullmatch(r"[0-9a-f]{2}", parts[0]) and re.fullmatch(r"[0-9a-f]{38}", parts[1]):
            return
        if parts[0] == "pack" and len(parts) == 2 and re.fullmatch(
            r"pack-[0-9a-f]{40}\.(?:pack|idx|rev|bitmap|mtimes)", parts[1]
        ):
            return
        if parts == ["info", "packs"]:
            return
        _fail("unsupported Git object-namespace entry: " + relative)

    def _open_relative_directory(self, root_fd: int, parts: Sequence[str]) -> int:
        descriptor = os.dup(root_fd)
        try:
            for part in parts:
                child = os.open(part, _directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _bind_control_file(self, relative: str, maximum: int = MAX_CONFIG_BYTES) -> Optional[bytes]:
        parts = relative.split("/")
        parent = self._open_relative_directory(self.git_fd, parts[:-1])
        try:
            try:
                _, content, _ = self._open_regular_at(
                    parent, parts[-1], relative, self._git_signature[4], maximum, retain=True
                )
                return content
            except FileNotFoundError:
                return None
        finally:
            os.close(parent)

    def _open_and_validate(self) -> None:
        if threading.active_count() != 1:
            _fail("readiness bootstrap must be single-threaded before descriptor-bound Git")
        root_before = os.lstat(str(self.repo_root))
        if not stat.S_ISDIR(root_before.st_mode) or stat.S_ISLNK(root_before.st_mode):
            _fail("repository root is not a real directory")
        self.root_fd = os.open(str(self.repo_root), _directory_flags())
        if not _same_binding(root_before, os.fstat(self.root_fd)):
            _fail("repository root binding changed")
        self._root_signature = _stat_signature(root_before)
        git_before = os.stat(".git", dir_fd=self.root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(git_before.st_mode) or stat.S_ISLNK(git_before.st_mode):
            _fail(".git must be a real directory; gitfiles/symlinks are forbidden")
        self.git_fd = os.open(".git", _directory_flags(), dir_fd=self.root_fd)
        if not _same_binding(git_before, os.fstat(self.git_fd)):
            _fail(".git binding changed")
        self._git_signature = _stat_signature(git_before)
        for forbidden in ("commondir", "config.worktree", "info/grafts", "shallow"):
            try:
                status_value = self._stat_git_relative(forbidden)
            except FileNotFoundError:
                continue
            if status_value is not None:
                _fail("forbidden Git administrative path exists: " + forbidden)

        _, config_bytes, _ = self._open_regular_at(
            self.git_fd, "config", "config", git_before.st_uid, MAX_CONFIG_BYTES, retain=True
        )
        _parse_git_config(config_bytes)
        _, index_bytes, _ = self._open_regular_at(
            self.git_fd, "index", "index", git_before.st_uid, MAX_INDEX_BYTES, retain=True
        )
        self._tracked = _parse_git_index(index_bytes)

        objects_fd = os.open("objects", _directory_flags(), dir_fd=self.git_fd)
        try:
            objects_status = os.fstat(objects_fd)
            self._object_snapshot = self._walk_metadata(
                objects_fd, None, True, retain_directories=True, namespace="git/objects"
            )
            self._bound_directories.append(
                _BoundDirectory("git/objects", objects_fd, _stat_signature(objects_status))
            )
            objects_fd = -1
        finally:
            if objects_fd >= 0:
                os.close(objects_fd)
        for required_object_directory in (("objects", "info"), ("objects", "pack")):
            required_fd = self._open_relative_directory(
                self.git_fd, required_object_directory
            )
            os.close(required_fd)
        self._admin_snapshot = self._walk_metadata(
            self.git_fd, "objects", False, retain_directories=True, namespace="git"
        )
        if "info/sparse-checkout" in self._admin_snapshot:
            _fail("sparse-checkout file is forbidden")
        for forbidden in ("objects/info/alternates", "objects/info/http-alternates"):
            try:
                self._stat_git_relative(forbidden)
            except FileNotFoundError:
                pass
            else:
                _fail("Git object alternates are forbidden")

        head = self._bind_control_file("HEAD", 4096)
        expected_head = ("ref: refs/heads/" + EXPECTED_BRANCH + "\n").encode("ascii")
        if head != expected_head:
            _fail("HEAD is not the exact CP2 branch symbolic ref")
        loose_ref = self._bind_control_file("refs/heads/" + EXPECTED_BRANCH, 4096)
        if loose_ref is None or re.fullmatch(rb"[0-9a-f]{40}\n", loose_ref) is None:
            _fail("exact CP2 branch loose ref is missing or invalid")
        self._bind_control_file("packed-refs", MAX_CONFIG_BYTES)
        self._bind_control_file("info/exclude", MAX_CONFIG_BYTES)
        self._bind_control_file("info/attributes", MAX_CONFIG_BYTES)

        temporary_status = os.lstat(str(self.temporary_root))
        if (
            not stat.S_ISDIR(temporary_status.st_mode)
            or stat.S_ISLNK(temporary_status.st_mode)
            or temporary_status.st_uid != os.geteuid()
            or stat.S_IMODE(temporary_status.st_mode) != 0o700
        ):
            _fail("readiness temporary root identity/mode is invalid")
        for name in ("git-home", "git-tmp"):
            path = self.temporary_root / name
            os.mkdir(str(path), 0o700)
            status_value = os.lstat(str(path))
            if (
                not stat.S_ISDIR(status_value.st_mode)
                or stat.S_ISLNK(status_value.st_mode)
                or status_value.st_uid != os.geteuid()
                or stat.S_IMODE(status_value.st_mode) != 0o700
            ):
                _fail("readiness private Git directory is invalid: " + name)
            descriptor = os.open(str(path), _directory_flags())
            self._bound_directories.append(
                _BoundDirectory("private/" + name, descriptor, _stat_signature(status_value))
            )
            self._git_private_directories[name] = _stat_signature(status_value)

        self._prewalk_worktree()
        self._child_root_fd = fcntl.fcntl(self.root_fd, fcntl.F_DUPFD_CLOEXEC, 10)
        self._child_git_fd = fcntl.fcntl(self.git_fd, fcntl.F_DUPFD_CLOEXEC, 11)
        self._establish_source()

    def _stat_git_relative(self, relative: str) -> os.stat_result:
        parts = relative.split("/")
        parent = self._open_relative_directory(self.git_fd, parts[:-1])
        try:
            return os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(parent)

    def _prewalk_worktree(self) -> None:
        tracked_by_path = {entry.path: entry for entry in self._tracked}
        if any(PurePosixPath(path).parts[0] in ALLOWED_OTHER_ROOTS for path in tracked_by_path):
            _fail("tracked source may not live under separately snapshotted roots")
        prefixes = set()
        for path in tracked_by_path:
            parts = PurePosixPath(path).parts
            for length in range(1, len(parts)):
                prefixes.add("/".join(parts[:length]))
        namespace: Dict[str, Tuple[int, ...]] = {}

        def walk(directory_fd: int, prefix: str, under_allowed: bool) -> None:
            names = sorted(os.listdir(directory_fd), key=lambda item: _utf8(item, "worktree path component"))
            for name in names:
                if not prefix and name == ".git":
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _stat_signature(current) != self._git_signature:
                        _fail("worktree .git binding differs from held Git directory")
                    continue
                relative = name if not prefix else prefix + "/" + name
                top = relative.split("/", 1)[0]
                allowed = under_allowed or top in ALLOWED_OTHER_ROOTS
                is_tracked = relative in tracked_by_path
                is_prefix = relative in prefixes
                if not allowed and not is_tracked and not is_prefix:
                    _fail("untracked source namespace entry is forbidden: " + relative)
                status_value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                namespace[relative] = _stat_signature(status_value)
                if name == ".git" or (allowed and name in (".gitignore", ".gitattributes")):
                    _fail("forbidden untracked control path: " + relative)
                if stat.S_ISDIR(status_value.st_mode):
                    if is_tracked:
                        _fail("tracked Git leaf became a directory: " + relative)
                    child = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(status_value, os.fstat(child)):
                            _fail("worktree directory binding changed: " + relative)
                        walk(child, relative, allowed)
                    finally:
                        self._bound_directories.append(
                            _BoundDirectory(
                                "worktree/" + relative,
                                child,
                                _stat_signature(status_value),
                            )
                        )
                elif stat.S_ISREG(status_value.st_mode):
                    if is_prefix:
                        _fail("tracked path prefix became a file: " + relative)
                    if is_tracked:
                        expected_exec = tracked_by_path[relative].mode == 0o100755
                        actual_exec = bool(status_value.st_mode & stat.S_IXUSR)
                        if expected_exec != actual_exec:
                            _fail("tracked executable bit differs from index: " + relative)
                        descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
                        if not _same_binding(status_value, os.fstat(descriptor)):
                            os.close(descriptor)
                            _fail("tracked leaf binding changed: " + relative)
                        self._tracked_fds[relative] = _BoundFile(
                            relative, descriptor, _stat_signature(status_value), None
                        )
                elif stat.S_ISLNK(status_value.st_mode):
                    if not allowed:
                        _fail("worktree source symlink is forbidden: " + relative)
                else:
                    _fail("worktree special file is forbidden: " + relative)

        walk(self.root_fd, "", False)
        if set(self._tracked_fds) != set(tracked_by_path):
            _fail("worktree does not contain the complete tracked leaf inventory")
        self._worktree_namespace = namespace

    def _scan_worktree_namespace(self) -> Dict[str, Tuple[int, ...]]:
        tracked_paths = {entry.path for entry in self._tracked}
        prefixes = set()
        for path in tracked_paths:
            parts = PurePosixPath(path).parts
            for length in range(1, len(parts)):
                prefixes.add("/".join(parts[:length]))
        result: Dict[str, Tuple[int, ...]] = {}

        def walk(directory_fd: int, prefix: str, under_allowed: bool) -> None:
            names = sorted(
                os.listdir(directory_fd),
                key=lambda item: _utf8(item, "worktree path component"),
            )
            for name in names:
                if not prefix and name == ".git":
                    current_git = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if _stat_signature(current_git) != self._git_signature:
                        _fail("worktree .git binding changed")
                    continue
                relative = name if not prefix else prefix + "/" + name
                top = relative.split("/", 1)[0]
                allowed = under_allowed or top in ALLOWED_OTHER_ROOTS
                tracked = relative in tracked_paths
                prefix_only = relative in prefixes
                if not allowed and not tracked and not prefix_only:
                    _fail("worktree gained an untracked source entry: " + relative)
                status_value = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if relative == self._postauthorization_output_relative:
                    self._validate_postauthorization_output_binding(
                        directory_fd, name, status_value
                    )
                    continue
                result[relative] = _stat_signature(status_value)
                if name == ".git" or (allowed and name in (".gitignore", ".gitattributes")):
                    _fail("worktree gained a forbidden control path: " + relative)
                if stat.S_ISDIR(status_value.st_mode):
                    if tracked:
                        _fail("tracked leaf became a directory: " + relative)
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(status_value, os.fstat(child_fd)):
                            _fail("worktree directory changed while reopening: " + relative)
                        walk(child_fd, relative, allowed)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(status_value.st_mode):
                    if prefix_only:
                        _fail("tracked directory prefix became a file: " + relative)
                    if tracked:
                        expected = next(item for item in self._tracked if item.path == relative)
                        if bool(status_value.st_mode & stat.S_IXUSR) != (expected.mode == 0o100755):
                            _fail("tracked executable bit changed: " + relative)
                elif stat.S_ISLNK(status_value.st_mode):
                    if not allowed:
                        _fail("worktree source symlink is forbidden: " + relative)
                else:
                    _fail("worktree special file is forbidden: " + relative)

        walk(self.root_fd, "", False)
        return result

    def _validate_postauthorization_output_binding(
        self,
        parent_fd: int,
        name: str,
        by_path: os.stat_result,
    ) -> None:
        """Validate the held root of the sole mutable postauthorization tree."""

        if (
            self._postauthorization_output_fd < 0
            or self._postauthorization_output_identity is None
        ):
            _fail("postauthorization output binding is incomplete")
        held = os.fstat(self._postauthorization_output_fd)
        reopened_fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
        try:
            reopened = os.fstat(reopened_fd)
        finally:
            os.close(reopened_fd)
        expected = self._postauthorization_output_identity
        if (
            not stat.S_ISDIR(by_path.st_mode)
            or not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(reopened.st_mode)
            or _directory_object_identity(by_path) != expected
            or _directory_object_identity(held) != expected
            or _directory_object_identity(reopened) != expected
            or stat.S_IMODE(by_path.st_mode) not in (0o555, 0o700)
            or stat.S_IMODE(held.st_mode) != stat.S_IMODE(by_path.st_mode)
            or stat.S_IMODE(reopened.st_mode) != stat.S_IMODE(by_path.st_mode)
            or by_path.st_uid != os.geteuid()
        ):
            _fail("postauthorization output root identity/mode changed")

    @staticmethod
    def _validate_output_parent_component(
        relative: str, value: os.stat_result
    ) -> None:
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.geteuid()
            or value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            _fail("postauthorization output parent is unsafe: " + relative)

    def _create_postauthorization_output_staging(
        self, run_id: str
    ) -> AuthorizedOutputStaging:
        """Create and register the one CP2-C partial after the strict barrier."""

        if (
            not isinstance(run_id, str)
            or SAFE_ID.fullmatch(run_id) is None
            or "\0" in run_id
        ):
            _fail("postauthorization output run ID is unsafe")
        if self._postauthorization_output_relative is not None:
            _fail("postauthorization output capability was already consumed")
        self._validate_bindings()
        if self._root_signature is None:
            _fail("repository root binding is unavailable")
        before_namespace = dict(self._worktree_namespace)
        before_root = self._root_signature
        components = tuple(PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts)
        opened: List[Tuple[str, int, bool]] = []
        transferred_descriptors = set()
        initial_bound_directory_count = len(self._bound_directories)
        output_fd = -1
        output_relative: Optional[str] = None
        partial_name = ""
        staging: Optional[AuthorizedOutputStaging] = None

        def remove_just_created_empty_directory(
            parent_descriptor: int,
            name: str,
            relative: str,
            expected: Optional[os.stat_result],
        ) -> None:
            """Bind, validate, and remove one mkdir result after acquisition fails."""

            cleanup_fd = os.open(
                name, _directory_flags(), dir_fd=parent_descriptor
            )
            try:
                held = os.fstat(cleanup_fd)
                by_path = os.stat(
                    name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISDIR(held.st_mode)
                    or held.st_uid != os.geteuid()
                    or stat.S_IMODE(held.st_mode) != 0o700
                    or _directory_object_identity(held)
                    != _directory_object_identity(by_path)
                    or (
                        expected is not None
                        and (
                            not _same_binding(expected, held)
                            or not _same_binding(expected, by_path)
                        )
                    )
                    or os.listdir(cleanup_fd)
                ):
                    _fail(
                        "new output directory changed before acquisition "
                        "rollback: " + relative
                    )
                os.rmdir(name, dir_fd=parent_descriptor)
                if os.fstat(cleanup_fd).st_nlink != 0:
                    _fail(
                        "held output directory survived acquisition rollback: "
                        + relative
                    )
                os.fsync(parent_descriptor)
            finally:
                os.close(cleanup_fd)

        def rebaseline_after_local_rollback() -> None:
            after_namespace = self._scan_worktree_namespace()
            if set(after_namespace) != set(before_namespace):
                _fail("worktree population differs after output-creation rollback")
            ancestor_set = {relative for relative, _, _ in opened}
            for relative, prior in before_namespace.items():
                current = after_namespace[relative]
                if relative not in ancestor_set:
                    if current != prior:
                        _fail(
                            "unrelated worktree metadata changed during output rollback: "
                            + relative
                        )
                elif (
                    _directory_signature_identity(current)
                    != _directory_signature_identity(prior)
                    or current[3] != prior[3]
                ):
                    _fail("output ancestor identity changed during rollback: " + relative)
            current_root_status = os.fstat(self.root_fd)
            current_root_path = os.lstat(str(self.repo_root))
            if not _same_binding(current_root_status, current_root_path):
                _fail("repository root path changed during output rollback")
            current_root = _stat_signature(current_root_status)
            if (
                _directory_signature_identity(current_root)
                != _directory_signature_identity(before_root)
                or current_root[3] != before_root[3]
            ):
                _fail("repository root identity changed during output rollback")
            rebound = set()
            for bound in self._bound_directories:
                if not bound.relative.startswith("worktree/"):
                    continue
                relative = bound.relative[len("worktree/"):]
                if relative in after_namespace and relative in ancestor_set:
                    bound.signature = after_namespace[relative]
                    rebound.add(relative)
            expected_rebound = {
                relative for relative in ancestor_set if relative in before_namespace
            }
            if rebound != expected_rebound:
                _fail("output rollback ancestor descriptors are incomplete")
            self._root_signature = current_root
            self._worktree_namespace = dict(after_namespace)

        def rollback_local_creation() -> None:
            if partial_name:
                if output_fd < 0:
                    _fail("output rollback lacks the held partial descriptor")
                held_output = os.fstat(output_fd)
                parent_descriptor = opened[-1][1]
                by_path = os.stat(
                    partial_name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _directory_object_identity(held_output)
                    != _directory_object_identity(by_path)
                    or os.listdir(output_fd)
                ):
                    _fail("output partial is nonempty or substituted during rollback")
                os.rmdir(partial_name, dir_fd=parent_descriptor)
                if os.fstat(output_fd).st_nlink != 0:
                    _fail("held output partial survived creation rollback")
                os.fsync(parent_descriptor)
            for index in range(len(opened) - 1, -1, -1):
                relative, descriptor, created = opened[index]
                if not created:
                    continue
                parent_descriptor = self.root_fd if index == 0 else opened[index - 1][1]
                leaf = PurePosixPath(relative).parts[-1]
                held = os.fstat(descriptor)
                by_path = os.stat(
                    leaf, dir_fd=parent_descriptor, follow_symlinks=False
                )
                if (
                    _directory_object_identity(held)
                    != _directory_object_identity(by_path)
                    or os.listdir(descriptor)
                ):
                    _fail("created output ancestor is nonempty or substituted: " + relative)
                os.rmdir(leaf, dir_fd=parent_descriptor)
                if os.fstat(descriptor).st_nlink != 0:
                    _fail("held output ancestor survived rollback: " + relative)
                os.fsync(parent_descriptor)
            rebaseline_after_local_rollback()

        try:
            parent_fd = self.root_fd
            prefix_parts: List[str] = []
            for component in components:
                prefix_parts.append(component)
                relative = "/".join(prefix_parts)
                created = False
                before: Optional[os.stat_result] = None
                try:
                    before = os.stat(
                        component, dir_fd=parent_fd, follow_symlinks=False
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, 0o700, dir_fd=parent_fd)
                    except FileExistsError as exc:
                        raise ReadinessError(
                            "postauthorization output parent raced into existence: "
                            + relative
                        ) from exc
                    except BaseException as mkdir_error:
                        try:
                            os.stat(
                                component,
                                dir_fd=parent_fd,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            raise mkdir_error
                        try:
                            remove_just_created_empty_directory(
                                parent_fd,
                                component,
                                relative,
                                None,
                            )
                        except BaseException as cleanup_error:
                            raise ReadinessError(
                                "output-parent mkdir failed and exact rollback "
                                "failed: " + str(cleanup_error)
                            ) from mkdir_error
                        raise mkdir_error
                    created = True
                try:
                    before = os.stat(
                        component, dir_fd=parent_fd, follow_symlinks=False
                    )
                    self._validate_output_parent_component(relative, before)
                    child_fd = os.open(
                        component, _directory_flags(), dir_fd=parent_fd
                    )
                except BaseException as acquisition_error:
                    if created:
                        try:
                            remove_just_created_empty_directory(
                                parent_fd,
                                component,
                                relative,
                                before,
                            )
                        except BaseException as cleanup_error:
                            raise ReadinessError(
                                "output-parent acquisition failed and exact "
                                "rollback failed: " + str(cleanup_error)
                            ) from acquisition_error
                    raise
                if before is None:
                    _fail("postauthorization output parent status is unavailable")
                opened.append((relative, child_fd, created))
                after = os.fstat(child_fd)
                if not _same_binding(before, after):
                    _fail("postauthorization output parent changed: " + relative)
                parent_fd = child_fd

            try:
                os.stat(run_id, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail("recorded final destination already exists")

            for _ in range(128):
                candidate = ".{}.partial.{}".format(run_id, os.urandom(16).hex())
                try:
                    os.mkdir(candidate, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    continue
                except BaseException as mkdir_error:
                    try:
                        os.stat(
                            candidate,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        raise mkdir_error
                    candidate_relative = (
                        POSTAUTHORIZATION_OUTPUT_PARENT + "/" + candidate
                    )
                    try:
                        remove_just_created_empty_directory(
                            parent_fd,
                            candidate,
                            candidate_relative,
                            None,
                        )
                    except BaseException as cleanup_error:
                        raise ReadinessError(
                            "output-partial mkdir failed and exact rollback "
                            "failed: " + str(cleanup_error)
                        ) from mkdir_error
                    raise mkdir_error
                partial_name = candidate
                break
            if not partial_name:
                _fail("could not allocate a unique recorded partial directory")
            output_relative = POSTAUTHORIZATION_OUTPUT_PARENT + "/" + partial_name
            output_before: Optional[os.stat_result] = None
            try:
                output_before = os.stat(
                    partial_name, dir_fd=parent_fd, follow_symlinks=False
                )
                if (
                    not stat.S_ISDIR(output_before.st_mode)
                    or output_before.st_uid != os.geteuid()
                    or stat.S_IMODE(output_before.st_mode) != 0o700
                ):
                    _fail("new postauthorization output partial is unsafe")
                output_fd = os.open(
                    partial_name, _directory_flags(), dir_fd=parent_fd
                )
            except BaseException as acquisition_error:
                try:
                    remove_just_created_empty_directory(
                        parent_fd,
                        partial_name,
                        output_relative,
                        output_before,
                    )
                    partial_name = ""
                except BaseException as cleanup_error:
                    raise ReadinessError(
                        "output-partial acquisition failed and exact rollback "
                        "failed: " + str(cleanup_error)
                    ) from acquisition_error
                raise
            if output_before is None:
                _fail("postauthorization output partial status is unavailable")
            if not _same_binding(output_before, os.fstat(output_fd)):
                _fail("new postauthorization output partial changed while opening")
            after_namespace = self._scan_worktree_namespace()
            after_root_status = os.fstat(self.root_fd)
            after_root_path = os.lstat(str(self.repo_root))
            if not _same_binding(after_root_status, after_root_path):
                _fail("repository root path changed during output registration")
            after_root = _stat_signature(after_root_status)
            if "results" in before_namespace:
                if after_root != before_root:
                    _fail("repository root changed during nested output creation")
            elif (
                _directory_signature_identity(after_root)
                != _directory_signature_identity(before_root)
                or after_root[3] != before_root[3]
            ):
                _fail("repository root identity changed during output creation")

            ancestor_set = {relative for relative, _, _ in opened}
            allowed_changes = ancestor_set | {output_relative}
            for relative in set(before_namespace) | set(after_namespace):
                before_signature = before_namespace.get(relative)
                after_signature = after_namespace.get(relative)
                if relative not in allowed_changes:
                    if before_signature != after_signature:
                        _fail(
                            "unrelated worktree namespace changed during output creation: "
                            + relative
                        )
                    continue
                if relative == output_relative:
                    if (
                        before_signature is not None
                        or after_signature != _stat_signature(output_before)
                    ):
                        _fail("postauthorization output partial transition differs")
                    continue
                if after_signature is None:
                    _fail("postauthorization output parent disappeared: " + relative)
                if before_signature is not None:
                    same_identity = (
                        _directory_signature_identity(before_signature)
                        == _directory_signature_identity(after_signature)
                    )
                    if (
                        not same_identity
                        or after_signature[3] != before_signature[3]
                    ):
                        _fail(
                            "postauthorization output parent identity changed: "
                            + relative
                        )
                elif stat.S_IMODE(after_signature[3]) != 0o700:
                    _fail("new postauthorization output parent mode differs")

            after_by_relative = {
                relative: after_namespace[relative] for relative in ancestor_set
            }
            existing_ancestors = {
                relative for relative in ancestor_set if relative in before_namespace
            }
            rebound_ancestors = set()
            for bound in self._bound_directories:
                if not bound.relative.startswith("worktree/"):
                    continue
                relative = bound.relative[len("worktree/"):]
                if relative in existing_ancestors:
                    bound.signature = after_by_relative[relative]
                    rebound_ancestors.add(relative)
            if rebound_ancestors != existing_ancestors:
                _fail("existing output-parent descriptors are incomplete")

            # Allocate every fallible transition object before repository
            # capability state or descriptor ownership is committed.
            new_bound_directories = tuple(
                _BoundDirectory(
                    "worktree/" + relative,
                    descriptor,
                    after_by_relative[relative],
                )
                for relative, descriptor, created in opened
                if created
            )
            created_ancestors = tuple(
                relative for relative, _, created in opened if created
            )
            output_identity = _directory_object_identity(output_before)
            registered_namespace = {
                relative: signature
                for relative, signature in after_namespace.items()
                if relative != output_relative
            }
            parent = self.repo_root.joinpath(*components)
            staging = AuthorizedOutputStaging(
                run_id=run_id,
                parent=parent,
                partial=parent / partial_name,
                final=parent / run_id,
            )

            self._bound_directories.extend(new_bound_directories)
            transferred_descriptors.update(
                item.fd for item in new_bound_directories
            )

            self._root_signature = after_root
            self._worktree_namespace = registered_namespace
            self._postauthorization_output_fd = output_fd
            self._postauthorization_output_identity = output_identity
            self._postauthorization_output_created_ancestors = (
                created_ancestors
            )
            self._postauthorization_output_pre_namespace = before_namespace
            # This is the registration commit marker.  All fields it guards
            # are populated before the mutable subtree becomes scan-exempt.
            self._postauthorization_output_relative = output_relative
            output_fd = -1
            return staging
        except BaseException as original_error:
            try:
                if self._postauthorization_output_relative is None:
                    appended = self._bound_directories[
                        initial_bound_directory_count:
                    ]
                    del self._bound_directories[
                        initial_bound_directory_count:
                    ]
                    for item in appended:
                        transferred_descriptors.discard(item.fd)
                    self._postauthorization_output_fd = -1
                    self._postauthorization_output_identity = None
                    self._postauthorization_output_created_ancestors = ()
                    self._postauthorization_output_pre_namespace = None
                    rollback_local_creation()
                else:
                    if staging is None:
                        _fail(
                            "registered output creation lacks its rollback token"
                        )
                    if output_fd == self._postauthorization_output_fd:
                        output_fd = -1
                    self._rollback_empty_postauthorization_output(staging)
            except BaseException as rollback_error:
                raise ReadinessError(
                    "output creation failed and exact rollback failed: "
                    + str(rollback_error)
                ) from original_error
            raise
        finally:
            for _, descriptor, _ in opened:
                if descriptor in transferred_descriptors:
                    continue
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if output_fd >= 0:
                os.close(output_fd)

    def _rollback_empty_postauthorization_output(
        self, staging: AuthorizedOutputStaging
    ) -> None:
        """Remove a just-created, still-empty output registration exactly."""

        expected_relative = self._postauthorization_output_relative
        pre_namespace = self._postauthorization_output_pre_namespace
        created_ancestors = self._postauthorization_output_created_ancestors
        expected_parent = self.repo_root.joinpath(
            *PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        if (
            not isinstance(staging, AuthorizedOutputStaging)
            or expected_relative is None
            or pre_namespace is None
            or self._postauthorization_output_fd < 0
            or staging.parent != expected_parent
            or expected_relative
            != POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.partial.name
        ):
            _fail("empty output rollback token differs from the registration")
        output_fd = self._postauthorization_output_fd
        parent_fd = self._open_relative_directory(
            self.root_fd, PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        try:
            held = os.fstat(output_fd)
            by_path = os.stat(
                staging.partial.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                _directory_object_identity(held)
                != self._postauthorization_output_identity
                or _directory_object_identity(by_path)
                != self._postauthorization_output_identity
                or stat.S_IMODE(held.st_mode) != 0o700
                or stat.S_IMODE(by_path.st_mode) != 0o700
                or os.listdir(output_fd)
            ):
                _fail("registered output is nonempty or substituted during rollback")
            os.rmdir(staging.partial.name, dir_fd=parent_fd)
            if os.fstat(output_fd).st_nlink != 0:
                _fail("held registered output survived rollback")
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

        os.close(output_fd)
        self._postauthorization_output_fd = -1
        self._postauthorization_output_relative = None
        self._postauthorization_output_identity = None

        for relative in reversed(created_ancestors):
            matches = [
                item
                for item in self._bound_directories
                if item.relative == "worktree/" + relative
            ]
            if len(matches) != 1:
                _fail("created output ancestor descriptor population differs")
            bound = matches[0]
            parts = PurePosixPath(relative).parts
            ancestor_parent = (
                os.dup(self.root_fd)
                if len(parts) == 1
                else self._open_relative_directory(self.root_fd, parts[:-1])
            )
            try:
                held = os.fstat(bound.fd)
                by_path = os.stat(
                    parts[-1],
                    dir_fd=ancestor_parent,
                    follow_symlinks=False,
                )
                if (
                    _directory_object_identity(held)
                    != _directory_object_identity(by_path)
                    or os.listdir(bound.fd)
                ):
                    _fail("created output ancestor is nonempty or substituted: " + relative)
                os.rmdir(parts[-1], dir_fd=ancestor_parent)
                if os.fstat(bound.fd).st_nlink != 0:
                    _fail(
                        "held created output ancestor survived rollback: "
                        + relative
                    )
                os.fsync(ancestor_parent)
            finally:
                os.close(ancestor_parent)
            os.close(bound.fd)
            self._bound_directories.remove(bound)

        after_namespace = self._scan_worktree_namespace()
        if set(after_namespace) != set(pre_namespace):
            _fail("worktree population differs after registered-output rollback")
        ancestor_relatives = {
            "/".join(PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts[:length])
            for length in range(
                1, len(PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts) + 1
            )
        }
        for relative, prior in pre_namespace.items():
            current = after_namespace[relative]
            if relative not in ancestor_relatives:
                if current != prior:
                    _fail(
                        "unrelated worktree metadata changed during registered rollback: "
                        + relative
                    )
            elif (
                _directory_signature_identity(current)
                != _directory_signature_identity(prior)
                or current[3] != prior[3]
            ):
                _fail("output ancestor identity changed during registered rollback")
        root_status = os.fstat(self.root_fd)
        root_path = os.lstat(str(self.repo_root))
        if not _same_binding(root_status, root_path):
            _fail("repository root path changed during registered rollback")
        root_signature = _stat_signature(root_status)
        if self._root_signature is None or (
            _directory_signature_identity(root_signature)
            != _directory_signature_identity(self._root_signature)
            or root_signature[3] != self._root_signature[3]
        ):
            _fail("repository root identity changed during registered rollback")
        expected_rebound = {
            relative
            for relative in ancestor_relatives
            if relative in after_namespace
        }
        rebound = set()
        for bound in self._bound_directories:
            if not bound.relative.startswith("worktree/"):
                continue
            relative = bound.relative[len("worktree/"):]
            if relative in expected_rebound:
                bound.signature = after_namespace[relative]
                rebound.add(relative)
        if rebound != expected_rebound:
            _fail("registered rollback ancestor descriptors are incomplete")
        self._root_signature = root_signature
        self._worktree_namespace = dict(after_namespace)
        self._postauthorization_output_created_ancestors = ()
        self._postauthorization_output_pre_namespace = None
        self._validate_bindings()

    @staticmethod
    def _renameat2_noreplace(
        parent_fd: int, source_name: str, destination_name: str
    ) -> None:
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            _fail("renameat2 is unavailable; refusing an overwrite-racy fallback")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_fd,
            os.fsencode(source_name),
            parent_fd,
            os.fsencode(destination_name),
            1,
        )
        if result != 0:
            value = ctypes.get_errno()
            raise OSError(value, os.strerror(value), destination_name)

    def _rebaseline_postauthorization_output_parent(
        self, before_namespace: Mapping[str, Tuple[int, ...]]
    ) -> None:
        """Accept only the parent metadata delta caused by one exact rename."""

        parent_relative = POSTAUTHORIZATION_OUTPUT_PARENT
        after_namespace = self._scan_worktree_namespace()
        for relative in set(before_namespace) | set(after_namespace):
            before_signature = before_namespace.get(relative)
            after_signature = after_namespace.get(relative)
            if relative != parent_relative:
                if before_signature != after_signature:
                    _fail(
                        "unrelated worktree namespace changed during output publication: "
                        + relative
                    )
                continue
            if (
                before_signature is None
                or after_signature is None
                or _directory_signature_identity(before_signature)
                != _directory_signature_identity(after_signature)
                or before_signature[3] != after_signature[3]
            ):
                _fail("recorded output parent identity changed during publication")
        rebound = 0
        for bound in self._bound_directories:
            if bound.relative == "worktree/" + parent_relative:
                bound.signature = after_namespace[parent_relative]
                rebound += 1
        if rebound != 1:
            _fail("recorded output parent descriptor population differs")
        self._worktree_namespace = dict(after_namespace)

    def _publish_postauthorization_output(
        self, staging: AuthorizedOutputStaging
    ) -> None:
        """Atomically bind the held partial inode to its one final CP2-C name."""

        if not isinstance(staging, AuthorizedOutputStaging):
            _fail("postauthorization output publication token is invalid")
        expected_partial_relative = self._postauthorization_output_relative
        expected_parent = self.repo_root.joinpath(
            *PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        if (
            expected_partial_relative is None
            or self._postauthorization_output_fd < 0
            or staging.parent != expected_parent
            or staging.partial.parent != expected_parent
            or staging.final.parent != expected_parent
            or staging.final.name != staging.run_id
            or expected_partial_relative
            != POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.partial.name
        ):
            _fail("postauthorization output publication token differs")
        self._validate_bindings()
        held = os.fstat(self._postauthorization_output_fd)
        if (
            not stat.S_ISDIR(held.st_mode)
            or stat.S_IMODE(held.st_mode) != 0o555
            or _directory_object_identity(held)
            != self._postauthorization_output_identity
        ):
            _fail("only the sealed held output partial may be published")

        parent_fd = self._open_relative_directory(
            self.root_fd, PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        renamed = False
        before_namespace = dict(self._worktree_namespace)
        final_relative = POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.final.name
        try:
            partial_status = os.stat(
                staging.partial.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if _directory_object_identity(partial_status) != (
                self._postauthorization_output_identity
            ):
                _fail("held output partial path binding changed before publication")
            try:
                os.stat(staging.final.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail("recorded final destination already exists")
            self._renameat2_noreplace(
                parent_fd, staging.partial.name, staging.final.name
            )
            renamed = True
            os.fsync(parent_fd)
            try:
                os.stat(staging.partial.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                _fail("hidden output name remained after publication")
            final_status = os.stat(
                staging.final.name, dir_fd=parent_fd, follow_symlinks=False
            )
            if (
                _directory_object_identity(final_status)
                != self._postauthorization_output_identity
                or stat.S_IMODE(final_status.st_mode) != 0o555
            ):
                _fail("published output does not retain the held sealed inode")
            self._postauthorization_output_relative = final_relative
            self._rebaseline_postauthorization_output_parent(before_namespace)
            self._validate_bindings()
        except BaseException as original_error:
            held_identity = self._postauthorization_output_identity
            partial_is_held = False
            final_is_held = False
            try:
                partial_after_error = os.stat(
                    staging.partial.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                partial_after_error = None
            try:
                final_after_error = os.stat(
                    staging.final.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                final_after_error = None
            if partial_after_error is not None:
                partial_is_held = (
                    _directory_object_identity(partial_after_error)
                    == held_identity
                )
            if final_after_error is not None:
                final_is_held = (
                    _directory_object_identity(final_after_error)
                    == held_identity
                )
            if final_is_held and partial_after_error is None:
                renamed = True
                # Reconcile the authoritative name before attempting rollback.
                # The forward rename may have completed immediately before an
                # asynchronous exception, and later trusted-parent failure
                # handling must still be able to bind the exact final inode.
                self._postauthorization_output_relative = final_relative
            elif partial_is_held and not final_is_held:
                renamed = False
                self._postauthorization_output_relative = (
                    expected_partial_relative
                )
            else:
                raise ReadinessError(
                    "output publication failure left an irreconcilable held "
                    "name binding"
                ) from original_error
            if renamed:
                rollback_error: Optional[BaseException] = None
                try:
                    self._renameat2_noreplace(
                        parent_fd, staging.final.name, staging.partial.name
                    )
                    os.fsync(parent_fd)
                    self._postauthorization_output_relative = (
                        expected_partial_relative
                    )
                    self._rebaseline_postauthorization_output_parent(
                        before_namespace
                    )
                    self._validate_bindings()
                except BaseException as exc:
                    rollback_error = exc
                if rollback_error is not None:
                    # The rollback syscall itself may have completed before a
                    # later fsync/rebaseline interruption.  Reconcile both
                    # names again so publication-state and held-root queries
                    # never depend on a stale Python-side location latch.
                    try:
                        partial_after_rollback = os.stat(
                            staging.partial.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        partial_after_rollback = None
                    try:
                        final_after_rollback = os.stat(
                            staging.final.name,
                            dir_fd=parent_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        final_after_rollback = None
                    partial_is_held = (
                        partial_after_rollback is not None
                        and _directory_object_identity(partial_after_rollback)
                        == held_identity
                    )
                    final_is_held = (
                        final_after_rollback is not None
                        and _directory_object_identity(final_after_rollback)
                        == held_identity
                    )
                    if partial_is_held and not final_is_held:
                        self._postauthorization_output_relative = (
                            expected_partial_relative
                        )
                    elif final_is_held and not partial_is_held:
                        self._postauthorization_output_relative = final_relative
                    else:
                        raise ReadinessError(
                            "output publication rollback failure left an "
                            "irreconcilable held name binding"
                        ) from original_error
                    raise ReadinessError(
                        "output publication failed and exact rollback failed: "
                        + type(rollback_error).__name__ + ": "
                        + str(rollback_error)
                    ) from original_error
            raise
        finally:
            os.close(parent_fd)

    def _rollback_published_postauthorization_output(
        self, staging: AuthorizedOutputStaging
    ) -> None:
        """Restore the hidden name if authorization fails after publication."""

        expected_parent = self.repo_root.joinpath(
            *PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        expected_final_relative = (
            POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.final.name
        )
        if (
            not isinstance(staging, AuthorizedOutputStaging)
            or self._postauthorization_output_fd < 0
            or self._postauthorization_output_identity is None
            or self._postauthorization_output_relative
            != expected_final_relative
            or staging.parent != expected_parent
            or staging.partial.parent != expected_parent
            or staging.final.parent != expected_parent
            or staging.final.name != staging.run_id
        ):
            _fail("published-output rollback token differs")

        parent_fd = self._open_relative_directory(
            self.root_fd, PurePosixPath(POSTAUTHORIZATION_OUTPUT_PARENT).parts
        )
        before_namespace = dict(self._worktree_namespace)
        partial_relative = (
            POSTAUTHORIZATION_OUTPUT_PARENT + "/" + staging.partial.name
        )
        try:
            held = os.fstat(self._postauthorization_output_fd)
            final_status = os.stat(
                staging.final.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(held.st_mode)
                or stat.S_IMODE(held.st_mode) != 0o555
                or _directory_object_identity(held)
                != self._postauthorization_output_identity
                or _directory_object_identity(final_status)
                != self._postauthorization_output_identity
            ):
                _fail("published output changed before rollback")
            try:
                os.stat(
                    staging.partial.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                _fail("hidden output destination exists during rollback")
            self._renameat2_noreplace(
                parent_fd, staging.final.name, staging.partial.name
            )
            os.fsync(parent_fd)
            self._postauthorization_output_relative = partial_relative
            self._rebaseline_postauthorization_output_parent(before_namespace)
            self._validate_bindings()
        except BaseException as original_error:
            # The rollback rename may have completed immediately before an
            # fsync, latch-update, rebaseline, or validation interruption.
            # Reconcile both names against the held inode before exposing the
            # root to trusted-parent failure handling.
            held_identity = self._postauthorization_output_identity
            try:
                partial_after_error = os.stat(
                    staging.partial.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                partial_after_error = None
            try:
                final_after_error = os.stat(
                    staging.final.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                final_after_error = None
            partial_is_held = (
                partial_after_error is not None
                and _directory_object_identity(partial_after_error)
                == held_identity
            )
            final_is_held = (
                final_after_error is not None
                and _directory_object_identity(final_after_error)
                == held_identity
            )
            if partial_is_held and not final_is_held:
                self._postauthorization_output_relative = partial_relative
            elif final_is_held and not partial_is_held:
                self._postauthorization_output_relative = (
                    expected_final_relative
                )
            else:
                raise ReadinessError(
                    "published-output rollback failure left an irreconcilable "
                    "held name binding"
                ) from original_error
            raise
        finally:
            os.close(parent_fd)

    def _git_environment(self) -> Dict[str, str]:
        home = self.temporary_root / "git-home"
        tmpdir = self.temporary_root / "git-tmp"
        return {
            "GIT_ALLOW_PROTOCOL": "none",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": str(tmpdir),
        }

    @property
    def readiness_git_environment(self) -> Dict[str, str]:
        """Return the exact retained ``readiness_git_v1`` environment."""

        return dict(self._git_environment())

    @property
    def readiness_git_commands(self) -> List[Dict[str, Any]]:
        """Return an immutable-by-copy summary of every readiness Git child."""

        return [dict(record) for record in self._git_commands]

    @property
    def readiness_git_outputs(self) -> Dict[str, bytes]:
        return dict(self._git_outputs)

    def _record_git_command(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        stdin_sha256: str,
        stdout_sha256: str,
        stderr_sha256: str,
        stdout: Optional[bytes],
        started_utc: str,
        finished_utc: str,
        return_code: int,
    ) -> None:
        command_index = len(self._git_commands)
        stdout_payload = None
        if stdout is not None:
            stdout_payload = "readiness/git/{:02d}.stdout".format(command_index)
            if stdout_payload in self._git_outputs:
                _fail("readiness Git stdout attachment path is duplicated")
            self._git_outputs[stdout_payload] = stdout
        self._git_commands.append({
            "command_index": command_index,
            "argv": list(argv),
            "cwd": "/proc/self/fd/3",
            "environment_sha256": hashlib.sha256(
                encode_command_environment(environment)
            ).hexdigest(),
            "stdin_sha256": stdin_sha256,
            "stdout_sha256": stdout_sha256,
            "stdout_payload": stdout_payload,
            "stderr_sha256": stderr_sha256,
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "exit_code": return_code,
            "timed_out": False,
            "process_group_complete": True,
        })

    @staticmethod
    def _git_prefix() -> List[str]:
        options = (
            "color.ui=false",
            "core.attributesFile=/dev/null",
            "core.commitGraph=false",
            "core.excludesFile=/dev/null",
            "core.fileMode=true",
            "core.fsmonitor=false",
            "core.hooksPath=/dev/null",
            "core.ignoreCase=false",
            "core.sparseCheckout=false",
            "core.sparseCheckoutCone=false",
            "core.untrackedCache=false",
            "diff.external=",
            "pager.status=false",
            "protocol.allow=never",
            "protocol.file.allow=never",
            "status.submoduleSummary=false",
            "submodule.recurse=false",
        )
        result = [
            "/usr/bin/git",
            "--no-pager",
            "--no-optional-locks",
            "--git-dir=/proc/self/fd/4",
            "--work-tree=/proc/self/fd/3",
        ]
        for option in options:
            result.extend(("-c", option))
        return result

    def _validate_bindings(self) -> None:
        held_root = os.fstat(self.root_fd)
        path_root = os.lstat(str(self.repo_root))
        if (
            _stat_signature(held_root) != self._root_signature
            or _stat_signature(path_root) != self._root_signature
            or not stat.S_ISDIR(held_root.st_mode)
            or stat.S_ISLNK(path_root.st_mode)
        ):
            _fail("repository root metadata changed")
        if _stat_signature(os.fstat(self.git_fd)) != self._git_signature:
            _fail("Git directory metadata changed")
        current_git = os.stat(".git", dir_fd=self.root_fd, follow_symlinks=False)
        if _stat_signature(current_git) != self._git_signature:
            _fail("repository .git path binding changed")
        for item in self._bound_files:
            if _stat_signature(os.fstat(item.fd)) != item.signature:
                _fail("held Git control file metadata changed: " + item.relative)
            if item.digest is not None and _hash_fd(item.fd)[1] != item.digest:
                _fail("held Git control file bytes changed: " + item.relative)
        for item in self._bound_directories:
            if _stat_signature(os.fstat(item.fd)) != item.signature:
                _fail("held directory metadata changed: " + item.relative)
        for name, signature in self._git_private_directories.items():
            status_value = os.lstat(str(self.temporary_root / name))
            if _stat_signature(status_value) != signature:
                _fail("private Git directory path binding changed: " + name)
        objects_fd = os.open("objects", _directory_flags(), dir_fd=self.git_fd)
        try:
            current_objects = self._walk_metadata(objects_fd, None, True)
        finally:
            os.close(objects_fd)
        if current_objects != self._object_snapshot:
            _fail("Git object namespace changed")
        if self._walk_metadata(self.git_fd, "objects", False) != self._admin_snapshot:
            _fail("Git administrative namespace changed")
        if self._scan_worktree_namespace() != self._worktree_namespace:
            _fail("worktree namespace changed")
        for item in self._tracked_fds.values():
            if _stat_signature(os.fstat(item.fd)) != item.signature:
                _fail("tracked worktree leaf metadata changed: " + item.relative)
            if item.digest is not None and _hash_fd(item.fd)[1] != item.digest:
                _fail("tracked worktree leaf bytes changed: " + item.relative)

    def _git(self, suffix: Sequence[str], stdin: Optional[bytes] = None) -> bytes:
        allowed_fixed = {
            ("ls-files", "--stage", "-z"),
            ("ls-files", "--others", "-z"),
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
            ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
            ("rev-parse", "--verify", "HEAD"),
            ("rev-parse", "--verify", "HEAD^{tree}"),
        }
        suffix_tuple = tuple(suffix)
        ancestor = (
            len(suffix_tuple) == 4
            and suffix_tuple[:2] == ("merge-base", "--is-ancestor")
            and HEX40.fullmatch(suffix_tuple[2]) is not None
            and suffix_tuple[3] == "HEAD"
        )
        if suffix_tuple not in allowed_fixed and not ancestor:
            _fail("unsupported readiness Git command")
        self._validate_bindings()
        root_fd = self._child_root_fd
        git_fd = self._child_git_fd
        argv = self._git_prefix() + list(suffix_tuple)
        environment = self._git_environment()
        started_utc = _canonical_utc()
        with tempfile.TemporaryFile(dir=str(self.temporary_root)) as input_stream, \
             tempfile.TemporaryFile(dir=str(self.temporary_root)) as output_stream, \
             tempfile.TemporaryFile(dir=str(self.temporary_root)) as error_stream:
            if stdin is not None:
                input_stream.write(stdin)
                input_stream.flush()
                input_stream.seek(0)
            state = _OwnedRawProcess(-1)
            try:
                _fork_owned_raw_process(state)
                child = state.pid
                if child == 0:
                    try:
                        os.setsid()
                        os.dup2(input_stream.fileno(), 0)
                        os.dup2(output_stream.fileno(), 1)
                        os.dup2(error_stream.fileno(), 2)
                        os.dup2(root_fd, 3)
                        os.dup2(git_fd, 4)
                        os.set_inheritable(3, True)
                        os.set_inheritable(4, True)
                        os.fchdir(3)
                        maximum_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                        if maximum_fd == resource.RLIM_INFINITY:
                            maximum_fd = 1 << 20
                        os.closerange(5, int(maximum_fd))
                        os.execve(argv[0], argv, environment)
                    except BaseException as exc:
                        try:
                            os.write(2, ("readiness Git exec failure: " + repr(exc)).encode("utf-8"))
                        finally:
                            os._exit(127)
                state = _OwnedRawProcess(child)
                timed_out, surviving_group = _wait_owned_raw_process_group(
                    state, _READINESS_GIT_TIMEOUT_SECONDS
                )
                if timed_out:
                    _fail("readiness Git command timed out")
                output_stream.seek(0)
                error_stream.seek(0)
                stdout = output_stream.read(MAX_GIT_OUTPUT_BYTES + 1)
                stderr = error_stream.read(16 * 1024 * 1024 + 1)
            except BaseException:
                if state.pid > 0:
                    if not state.process_group_complete:
                        _terminate_owned_raw_process_group(state)
                raise
            status_value = state.status
            if status_value is None:
                _fail("readiness Git process leader has no wait status")
        if os.WIFEXITED(status_value):
            return_code = os.WEXITSTATUS(status_value)
        elif os.WIFSIGNALED(status_value):
            return_code = 128 + os.WTERMSIG(status_value)
        else:
            return_code = 255
        if len(stdout) > MAX_GIT_OUTPUT_BYTES or len(stderr) > 16 * 1024 * 1024:
            _fail("readiness Git output exceeds bound")
        self._validate_bindings()
        if surviving_group:
            _fail("readiness Git command left a surviving process group")
        if return_code != 0:
            _fail("readiness Git command failed: {}: {}".format(
                " ".join(suffix_tuple), stderr.decode("utf-8", "replace")[:1000]
            ))
        if stderr:
            _fail("readiness Git command emitted unexpected stderr")
        self._record_git_command(
            argv,
            environment,
            hashlib.sha256(stdin if stdin is not None else b"").hexdigest(),
            hashlib.sha256(stdout).hexdigest(),
            hashlib.sha256(stderr).hexdigest(),
            stdout,
            started_utc,
            _canonical_utc(),
            return_code,
        )
        return stdout

    def _cat_file_digests(self) -> Dict[str, Tuple[int, str]]:
        """Hash stage-0 blobs from a pipe without persisting or returning content."""

        self._validate_bindings()
        argv = self._git_prefix() + ["cat-file", "--batch"]
        environment = self._git_environment()
        request = b"".join((item.oid + "\n").encode("ascii") for item in self._tracked)
        started_utc = _canonical_utc()
        root_fd = self._child_root_fd
        git_fd = self._child_git_fd
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        results: Dict[str, Tuple[int, str]] = {}
        stdout_digest = hashlib.sha256()
        reader_error: List[BaseException] = []
        reader: Optional[threading.Thread] = None
        reader_fd = -1
        state = _OwnedRawProcess(-1)
        try:
            with tempfile.TemporaryFile(dir=str(self.temporary_root)) as input_stream, \
                 tempfile.TemporaryFile(dir=str(self.temporary_root)) as error_stream:
                input_stream.write(request)
                input_stream.flush()
                input_stream.seek(0)
                _fork_owned_raw_process(state)
                child = state.pid
                if child == 0:
                    try:
                        os.setsid()
                        os.dup2(input_stream.fileno(), 0)
                        os.dup2(write_fd, 1)
                        os.dup2(error_stream.fileno(), 2)
                        os.dup2(root_fd, 3)
                        os.dup2(git_fd, 4)
                        os.set_inheritable(3, True)
                        os.set_inheritable(4, True)
                        os.fchdir(3)
                        maximum_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
                        if maximum_fd == resource.RLIM_INFINITY:
                            maximum_fd = 1 << 20
                        os.closerange(5, int(maximum_fd))
                        os.execve(argv[0], argv, environment)
                    except BaseException as exc:
                        try:
                            os.write(2, ("readiness cat-file exec failure: " + repr(exc)).encode("utf-8"))
                        finally:
                            os._exit(127)
                state = _OwnedRawProcess(child)
                os.close(write_fd)
                write_fd = -1
                reader_fd = read_fd
                read_fd = -1

                def consume(descriptor: int = reader_fd) -> None:
                    try:
                        with os.fdopen(descriptor, "rb", buffering=0) as stream:
                            for item in self._tracked:
                                header = bytearray()
                                while True:
                                    byte = stream.read(1)
                                    if byte == b"":
                                        _fail("cat-file batch header is truncated")
                                    if byte == b"\n":
                                        break
                                    if len(header) >= 255:
                                        _fail("cat-file batch header exceeds bound")
                                    header.extend(byte)
                                match = re.fullmatch(rb"([0-9a-f]{40}) blob ([0-9]+)", bytes(header))
                                if match is None or match.group(1).decode("ascii") != item.oid:
                                    _fail("cat-file returned wrong object identity/type")
                                stdout_digest.update(bytes(header))
                                stdout_digest.update(b"\n")
                                size = int(match.group(2))
                                if size > U64_MAX:
                                    _fail("cat-file blob size exceeds u64")
                                remaining = size
                                digest = hashlib.sha256()
                                while remaining:
                                    block = stream.read(min(remaining, 1024 * 1024))
                                    if not block:
                                        _fail("cat-file blob payload is truncated")
                                    digest.update(block)
                                    stdout_digest.update(block)
                                    remaining -= len(block)
                                terminator = stream.read(1)
                                if terminator != b"\n":
                                    _fail("cat-file blob terminator is invalid")
                                stdout_digest.update(terminator)
                                results[item.oid] = (size, digest.hexdigest())
                            if stream.read(1) != b"":
                                _fail("cat-file batch has trailing bytes")
                    except BaseException as exc:
                        reader_error.append(exc)

                reader = threading.Thread(
                    target=consume, name="cp2-cat-file-reader", daemon=True
                )
                reader.start()
                timed_out, surviving_group = _wait_owned_raw_process_group(
                    state, _READINESS_GIT_TIMEOUT_SECONDS
                )
                _join_cat_file_reader(reader)
                reader_fd = -1
                if timed_out:
                    _fail("readiness cat-file command timed out")
                error_stream.seek(0)
                stderr = error_stream.read(16 * 1024 * 1024 + 1)
        except BaseException:
            try:
                if state.pid > 0:
                    if not state.process_group_complete:
                        _terminate_owned_raw_process_group(state)
            finally:
                if reader is not None and reader.ident is not None:
                    _join_cat_file_reader(reader)
                    reader_fd = -1
                elif reader_fd >= 0:
                    os.close(reader_fd)
                    reader_fd = -1
            raise
        finally:
            for descriptor in (read_fd, write_fd):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
        status_value = state.status
        if status_value is None:
            _fail("readiness cat-file process leader has no wait status")
        if os.WIFEXITED(status_value):
            return_code = os.WEXITSTATUS(status_value)
        elif os.WIFSIGNALED(status_value):
            return_code = 128 + os.WTERMSIG(status_value)
        else:
            return_code = 255
        self._validate_bindings()
        if surviving_group:
            _fail("readiness cat-file command left a surviving process group")
        if len(stderr) > 16 * 1024 * 1024:
            _fail("cat-file stderr exceeds bound")
        if reader_error:
            error = reader_error[0]
            if isinstance(error, ReadinessError):
                raise error
            raise ReadinessError("cat-file stream reader failed: " + repr(error)) from error
        if return_code != 0:
            _fail("readiness cat-file command failed: " + stderr.decode("utf-8", "replace")[:1000])
        if stderr:
            _fail("readiness cat-file command emitted unexpected stderr")
        if set(results) != {item.oid for item in self._tracked}:
            _fail("cat-file result inventory differs from stage-0 blobs")
        self._record_git_command(
            argv,
            environment,
            hashlib.sha256(request).hexdigest(),
            stdout_digest.hexdigest(),
            hashlib.sha256(stderr).hexdigest(),
            None,
            started_utc,
            _canonical_utc(),
            return_code,
        )
        return results

    def _establish_source(self) -> None:
        raw_stage = self._git(("ls-files", "--stage", "-z"))
        stage_records = []
        for raw in raw_stage.split(b"\0")[:-1]:
            match = re.fullmatch(rb"(100644|100755) ([0-9a-f]{40}) 0\t(.+)", raw, re.DOTALL)
            if match is None:
                _fail("sanitized Git stage inventory is malformed")
            stage_records.append((int(match.group(1), 8), match.group(2).decode("ascii"), match.group(3)))
        expected_stage = [(item.mode, item.oid, item.path_bytes) for item in self._tracked]
        if stage_records != expected_stage:
            _fail("binary index and sanitized Git stage inventories differ")

        raw_other = self._git(("ls-files", "--others", "-z"))
        if raw_other and not raw_other.endswith(b"\0"):
            _fail("raw other-path inventory is malformed")
        for raw_path in raw_other.split(b"\0")[:-1]:
            try:
                path = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ReadinessError("untracked Git path is not UTF-8") from exc
            _relpath(path, "untracked Git path")
            if PurePosixPath(path).parts[0] not in ALLOWED_OTHER_ROOTS:
                _fail("untracked path outside snapshotted roots: " + path)

        blob_digests = self._cat_file_digests()
        records = []
        for item in self._tracked:
            size, blob_digest = blob_digests[item.oid]
            bound = self._tracked_fds[item.path]
            worktree_size, worktree_digest = _hash_fd(bound.fd)
            if worktree_size != size or worktree_digest != blob_digest:
                _fail("tracked blob/worktree mismatch: " + item.path)
            bound.digest = worktree_digest
            records.append(SnapshotEntry(item.path, "f", item.mode, size, blob_digest))

        status_bytes = self._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"))
        if status_bytes:
            _fail("source worktree is not clean")
        ignored = self._git(("ls-files", "--others", "--ignored", "--exclude-standard", "-z"))
        if ignored and not ignored.endswith(b"\0"):
            _fail("ignored-path inventory is malformed")
        for raw_path in ignored.split(b"\0")[:-1]:
            try:
                path = raw_path.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ReadinessError("ignored Git path is not UTF-8") from exc
            if not path or PurePosixPath(path).parts[0] not in ALLOWED_OTHER_ROOTS:
                _fail("ignored path outside snapshotted roots: " + path)
        commit = self._git(("rev-parse", "--verify", "HEAD")).rstrip(b"\n").decode("ascii")
        tree = self._git(("rev-parse", "--verify", "HEAD^{tree}")).rstrip(b"\n").decode("ascii")
        _hex40(commit, "HEAD")
        _hex40(tree, "HEAD tree")
        entry_by_path = {item.path: item for item in self._tracked}
        record_by_path = {item.path: item for item in records}
        entrypoints = []
        for path in ENTRYPOINTS:
            tracked = entry_by_path.get(path)
            source = record_by_path.get(path)
            if tracked is None or source is None:
                _fail("missing exact committed entrypoint: " + path)
            entrypoints.append(
                EntrypointIdentity(path, tracked.oid, source.sha256, tracked.mode, True)
            )
        self._source_records = tuple(records)
        self._commit = commit
        self._tree = tree
        self._status = status_bytes
        self._entrypoints = tuple(entrypoints)

    def require_ancestor(self, commit: str) -> None:
        _hex40(commit, "authorization commit")
        self._git(("merge-base", "--is-ancestor", commit, "HEAD"))

    def revalidate(self) -> None:
        """Revalidate every held repository, Git, and worktree binding."""

        if self._closed:
            _fail("repository guard is closed")
        self._validate_bindings()

    def prevalidated_source_context(self) -> Dict[str, Any]:
        """Return the complete opaque stage-0 identity for hidden step 8."""

        self._validate_bindings()
        source_by_path = {item.path: item for item in self._source_records}
        entries = []
        for tracked in self._tracked:
            source = source_by_path.get(tracked.path)
            if source is None:
                _fail("tracked source context is incomplete: " + tracked.path)
            entries.append({
                "git_blob": tracked.oid,
                "mode": tracked.mode,
                "path": tracked.path,
                "sha256": source.sha256,
                "size": source.size,
            })
        context = {
            "branch": EXPECTED_BRANCH,
            "commit": self._commit,
            "entries": entries,
            "entrypoints": [item.as_record() for item in self._entrypoints],
            "index_tree": self._tree,
            "record_type": PREVALIDATED_SOURCE_RECORD_TYPE,
            "schema_version": 1,
            "status_porcelain_v1_hex": self._status.hex(),
            "tree": self._tree,
        }
        self._validate_bindings()
        return context

    def duplicate_tracked_fd(self, relative_path: str) -> int:
        """Duplicate one exact held stage-0 worktree leaf for descriptor exec."""

        if relative_path not in ENTRYPOINTS:
            _fail("descriptor execution is restricted to exact readiness entrypoints")
        self._validate_bindings()
        held = self._tracked_fds.get(relative_path)
        if held is None or held.digest is None:
            _fail("readiness entrypoint is not held: " + relative_path)
        descriptor = os.dup(held.fd)
        os.set_inheritable(descriptor, False)
        return descriptor

    def _duplicate_postauthorization_source_fd(self, relative_path: str) -> int:
        """Duplicate one held nonregistry leaf for an authorization capability."""

        if relative_path == PREAUTHORIZATION_REGISTRY_PATH:
            _fail("the registry is restricted to its single-read capability")
        self._validate_bindings()
        held = self._tracked_fds.get(relative_path)
        if held is None or held.digest is None:
            _fail("postauthorization source is not an exact held leaf: " + relative_path)
        descriptor = os.dup(held.fd)
        try:
            os.set_inheritable(descriptor, False)
            self._validate_bindings()
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def capture_source(self, root_tag: str = "source") -> bytes:
        self._validate_bindings()
        for item in self._tracked_fds.values():
            current_size, current_digest = _hash_fd(item.fd)
            source = next(record for record in self._source_records if record.path == item.relative)
            if current_size != source.size or current_digest != source.sha256:
                _fail("tracked source bytes changed: " + item.relative)
        current_status = self._git(("status", "--porcelain=v1", "-z", "--untracked-files=all"))
        if current_status != self._status:
            _fail("source status changed")
        current_commit = self._git(("rev-parse", "--verify", "HEAD")).rstrip(b"\n").decode("ascii")
        current_tree = self._git(("rev-parse", "--verify", "HEAD^{tree}")).rstrip(b"\n").decode("ascii")
        if current_commit != self._commit or current_tree != self._tree:
            _fail("source Git identity changed")
        return encode_source_snapshot(
            root_tag,
            self._source_records,
            SourceIdentity(self._commit, self._tree, self._tree, current_status, self._entrypoints),
        )

    def capture_repository_root(self, root_name: str, root_tag: str) -> bytes:
        """Snapshot one exact repository child through the held root descriptor."""

        expected = {"build": "build", "results": "results", "Testing": "testing"}
        if expected.get(root_name) != root_tag:
            _fail("repository snapshot root name/tag is not exact")
        self._validate_bindings()
        descriptor_path = Path("/proc/self/fd/{}/{}".format(self.root_fd, root_name))
        payload = _snapshot_directory(descriptor_path, root_tag)
        self._validate_bindings()
        return payload

    def read_preauthorized_registry_once(
        self, relative_path: str = "project/datasets.yaml"
    ) -> bytes:
        """Read the exact held registry once, after caller authorization.

        The returned bytes are the sole buffer a runner may parse.  This
        method performs no decoding and accepts no alternate tracked path.
        """

        if relative_path != "project/datasets.yaml":
            _fail("postauthorization source read is restricted to project/datasets.yaml")
        if self._registry_read:
            _fail("project/datasets.yaml may be read semantically only once")
        self._validate_bindings()
        tracked = next((item for item in self._tracked if item.path == relative_path), None)
        source = next((item for item in self._source_records if item.path == relative_path), None)
        held = self._tracked_fds.get(relative_path)
        if tracked is None or source is None or held is None:
            _fail("project/datasets.yaml is not an exact held stage-0 source entry")
        parts = PurePosixPath(relative_path).parts
        parent_fd = os.dup(self.root_fd)
        registry_fd = -1
        try:
            for component_index, component in enumerate(parts[:-1]):
                before_directory = os.stat(
                    component, dir_fd=parent_fd, follow_symlinks=False
                )
                if not stat.S_ISDIR(before_directory.st_mode) or stat.S_ISLNK(
                    before_directory.st_mode
                ):
                    _fail("registry parent component is not a real directory")
                child_fd = os.open(component, _directory_flags(), dir_fd=parent_fd)
                if not _same_binding(before_directory, os.fstat(child_fd)):
                    os.close(child_fd)
                    _fail("registry parent binding changed")
                expected_relative = "/".join(parts[:component_index + 1])
                expected_signature = self._worktree_namespace.get(expected_relative)
                if expected_signature != _stat_signature(before_directory):
                    os.close(child_fd)
                    _fail("registry parent differs from the preauthorized namespace")
                os.close(parent_fd)
                parent_fd = child_fd
            leaf = parts[-1]
            before_leaf = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(before_leaf.st_mode)
                or stat.S_ISLNK(before_leaf.st_mode)
                or _stat_signature(before_leaf) != held.signature
                or _stat_signature(os.fstat(held.fd)) != held.signature
            ):
                _fail("registry leaf differs from the preauthorized binding")
            registry_fd = os.open(leaf, _file_flags(), dir_fd=parent_fd)
            if not _same_binding(before_leaf, os.fstat(registry_fd)):
                _fail("registry leaf changed while opening")
            content = _read_fd(registry_fd, source.size)
            after_fd = os.fstat(registry_fd)
            after_path = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not _same_binding(before_leaf, after_fd)
                or not _same_binding(before_leaf, after_path)
                or len(content) != source.size
                or hashlib.sha256(content).hexdigest() != source.sha256
                or tracked.mode != source.mode
            ):
                _fail("registry bytes/identity differ from preauthorization")
            self._validate_bindings()
            self._registry_read = True
            return content
        finally:
            if registry_fd >= 0:
                os.close(registry_fd)
            os.close(parent_fd)

    @property
    def commit(self) -> str:
        return self._commit

    @property
    def tree(self) -> str:
        return self._tree

    @property
    def entrypoints(self) -> Tuple[EntrypointIdentity, ...]:
        return self._entrypoints


def acquire_data_lock(path: Path = Path(DATA_LOCK_PATH)) -> int:
    path = Path(path).absolute()
    if path != Path(DATA_LOCK_PATH) and path.parent != Path("/tmp"):
        _fail("test lock override must remain directly under /tmp")
    try:
        before = os.lstat(str(path))
    except FileNotFoundError:
        before = None
    if before is not None and stat.S_ISLNK(before.st_mode):
        _fail("data lock path is a symlink")
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    try:
        status_value = os.fstat(descriptor)
        after = os.lstat(str(path))
        if (
            not stat.S_ISREG(status_value.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or status_value.st_uid != os.geteuid()
            or status_value.st_nlink != 1
            or stat.S_IMODE(status_value.st_mode) != 0o600
            or not _same_binding(status_value, after)
        ):
            _fail("data lock identity/owner/mode is invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReadinessError("CP2 data lock is contended") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_attachment(root: Path, relative: str, content: bytes) -> Path:
    _relpath(relative, "attachment path")
    destination = root.joinpath(*PurePosixPath(relative).parts)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    descriptor = os.open(str(destination), flags, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("attachment write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return destination


def _remove_private_tree(
    path: Path, expected_seal: Optional[_PrivateTreeSeal] = None
) -> None:
    """Descriptor-relatively remove only verified readiness-owned entries.

    A finalized authorization supplies ``expected_seal``.  Cleanup then requires
    the pathname to remain bound to the descriptor-held root, but deliberately
    does not require the descendants to equal the authorization-time inventory:
    a failed revalidation must still clean readiness-owned temporary state.
    Every current descendant is preflighted descriptor-relatively before the
    first mutation; links are unlinked without being followed, and hardlinked or
    special entries are refused.  A substituted root is never traversed or
    removed.
    """

    path = Path(path).absolute()
    allowed = (
        path.parent == Path("/tmp")
        and path.name.startswith("schurvio-cp2-readiness-")
    ) or any(
        parent.parent == Path("/tmp")
        and parent.name.startswith("schurvio-cp2-readiness-")
        for parent in path.parents
    )
    if not allowed or path == Path("/tmp"):
        _fail("refusing to remove a non-readiness private tree")
    try:
        root_status = os.lstat(str(path))
    except FileNotFoundError:
        if expected_seal is not None:
            _fail("sealed readiness private tree disappeared before cleanup")
        return
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_ISLNK(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
    ):
        _fail("readiness private tree root is not an owned real directory")

    if expected_seal is not None:
        if path != expected_seal.root:
            _fail("cleanup path differs from the sealed readiness root")
        if expected_seal._closed:
            _fail("sealed readiness root descriptor is closed before cleanup")
        held_status = os.fstat(expected_seal.root_fd)
        if (
            held_status.st_dev != expected_seal.root_signature[0]
            or held_status.st_ino != expected_seal.root_signature[1]
            or not stat.S_ISDIR(held_status.st_mode)
            or held_status.st_uid != expected_seal.root_signature[4]
        ):
            _fail("held readiness root identity changed before cleanup")
        root_fd = os.dup(expected_seal.root_fd)
    else:
        root_fd = os.open(str(path), _directory_flags())

    parent_fd = -1
    try:
        opened_root = os.fstat(root_fd)
        same_root_identity = (
            root_status.st_dev == opened_root.st_dev
            and root_status.st_ino == opened_root.st_ino
            and root_status.st_uid == opened_root.st_uid
            and stat.S_ISDIR(opened_root.st_mode)
        )
        if (
            (expected_seal is not None and not same_root_identity)
            or (expected_seal is None and not _same_binding(root_status, opened_root))
        ):
            _fail("readiness private tree root changed while opening for cleanup")
        parent_before = os.lstat(str(path.parent))
        if not stat.S_ISDIR(parent_before.st_mode) or stat.S_ISLNK(parent_before.st_mode):
            _fail("readiness private tree parent is not a real directory")
        parent_fd = os.open(str(path.parent), _directory_flags())
        if not _same_binding(parent_before, os.fstat(parent_fd)):
            _fail("readiness private tree parent changed while opening")
        root_from_parent = os.stat(
            path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if not _same_binding(root_status, root_from_parent):
            _fail("readiness private tree path binding changed before cleanup")

        preflight: Dict[str, Tuple[str, Tuple[int, ...]]] = {}

        def inspect(directory_fd: int, prefix: str) -> None:
            names = sorted(
                os.listdir(directory_fd),
                key=lambda name: _utf8(name, "cleanup path component"),
            )
            for name in names:
                if not name or name in (".", "..") or "/" in name or "\0" in name:
                    _fail("readiness cleanup found an unsafe path component")
                relative = name if not prefix else prefix + "/" + name
                _relpath(relative, "readiness cleanup path")
                current = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if current.st_uid != os.geteuid():
                    _fail("readiness cleanup found a foreign-owned entry")
                signature = _stat_signature(current)
                if stat.S_ISDIR(current.st_mode):
                    kind = "d"
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(current, os.fstat(child_fd)):
                            _fail("readiness cleanup directory changed during preflight")
                        preflight[relative] = (kind, signature)
                        inspect(child_fd, relative)
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(current.st_mode):
                    if current.st_nlink != 1:
                        _fail("readiness cleanup refuses a hardlinked file")
                    descriptor = os.open(name, _file_flags(), dir_fd=directory_fd)
                    try:
                        if not _same_binding(current, os.fstat(descriptor)):
                            _fail("readiness cleanup file changed during preflight")
                    finally:
                        os.close(descriptor)
                    preflight[relative] = ("f", signature)
                elif stat.S_ISLNK(current.st_mode):
                    # The link itself may be safely unlinked; its target is never
                    # opened, statted, chmodded, or traversed.
                    preflight[relative] = ("l", signature)
                else:
                    _fail("readiness cleanup refuses a special entry")
                if len(preflight) > MAX_SNAPSHOT_ENTRIES:
                    _fail("readiness cleanup entry count exceeds bound")

        inspect(root_fd, "")

        def same_path_identity(current: os.stat_result, signature: Tuple[int, ...]) -> bool:
            return (
                current.st_dev == signature[0]
                and current.st_ino == signature[1]
                and current.st_uid == signature[4]
            )

        def remove(directory_fd: int, prefix: str) -> None:
            os.fchmod(directory_fd, 0o700)
            names = sorted(
                os.listdir(directory_fd),
                key=lambda name: _utf8(name, "cleanup path component"),
            )
            for name in names:
                relative = name if not prefix else prefix + "/" + name
                expected_kind, expected_signature = preflight[relative]
                current = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if (
                    not same_path_identity(current, expected_signature)
                    or (
                        expected_kind == "d" and not stat.S_ISDIR(current.st_mode)
                    )
                    or (
                        expected_kind == "f" and not stat.S_ISREG(current.st_mode)
                    )
                    or (
                        expected_kind == "l" and not stat.S_ISLNK(current.st_mode)
                    )
                ):
                    _fail("readiness cleanup entry binding changed")
                if expected_kind == "d":
                    child_fd = os.open(name, _directory_flags(), dir_fd=directory_fd)
                    try:
                        if not same_path_identity(
                            os.fstat(child_fd), expected_signature
                        ):
                            _fail("readiness cleanup directory binding changed")
                        remove(child_fd, relative)
                    finally:
                        os.close(child_fd)
                    current = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if not same_path_identity(current, expected_signature):
                        _fail("readiness cleanup directory path was substituted")
                    os.rmdir(name, dir_fd=directory_fd)
                else:
                    current = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if not same_path_identity(current, expected_signature):
                        _fail("readiness cleanup leaf path was substituted")
                    if expected_kind == "f" and current.st_nlink != 1:
                        _fail("readiness cleanup leaf became hardlinked")
                    os.unlink(name, dir_fd=directory_fd)

        remove(root_fd, "")
        current_root = os.stat(
            path.name, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            current_root.st_dev != root_status.st_dev
            or current_root.st_ino != root_status.st_ino
            or not stat.S_ISDIR(current_root.st_mode)
        ):
            _fail("readiness private root path was substituted during cleanup")
        os.rmdir(path.name, dir_fd=parent_fd)
    except ReadinessError:
        raise
    except OSError as exc:
        raise ReadinessError("cannot remove readiness private tree") from exc
    finally:
        if parent_fd >= 0:
            os.close(parent_fd)
        os.close(root_fd)
    if os.path.lexists(str(path)):
        _fail("readiness private tree survived cleanup")


def _parse_self_test_stdout(stdout: bytes) -> Any:
    if not stdout.endswith(b"\n"):
        _fail("self-test stdout lacks a final newline")
    lines = stdout[:-1].split(b"\n")
    if not lines or not lines[-1]:
        _fail("self-test stdout lacks a final result line")
    return strict_json_loads(lines[-1])


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        raise ReadinessError("cannot validate owned self-test process group") from exc
    return True


def _wait_process_group_absent(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.01)
    return not _process_group_exists(process_group)


def _kill_process_group(process_group: int, grace_seconds: float = 0.5) -> None:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    if _wait_process_group_absent(process_group, grace_seconds):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not _wait_process_group_absent(process_group, 1.0):
        _fail("owned readiness process group survived SIGKILL")


def _fork_owned_raw_process(state: _OwnedRawProcess) -> None:
    """Fork with the parent-visible state bound before signals are unblocked."""

    if state.pid != -1:
        _fail("owned readiness raw-fork state was reused")
    blocked = signal.valid_signals() - {signal.SIGKILL, signal.SIGSTOP}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        state.pid = os.fork()
    finally:
        try:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        except BaseException:
            if state.pid == 0:
                os._exit(127)
            raise


def _terminate_owned_raw_process_group(state: _OwnedRawProcess) -> None:
    """SIGKILL, reap, and prove absence for a child created with ``os.fork``."""

    if state.process_group_complete:
        return
    # The direct signal closes the small parent/child race before the child has
    # completed setsid(); the group signal covers the post-setsid leader and all
    # descendants.  Both are attempted before the leader is reaped so its PID
    # cannot be reused between the signal operations.
    cleanup_errors: List[BaseException] = []
    if state.status is None:
        for _ in range(3):
            try:
                os.kill(state.pid, signal.SIGKILL)
                break
            except ProcessLookupError:
                break
            except BaseException as exc:
                cleanup_errors.append(exc)
    for _ in range(3):
        try:
            os.killpg(state.pid, signal.SIGKILL)
            break
        except ProcessLookupError:
            break
        except BaseException as exc:
            cleanup_errors.append(exc)

    leader_reaped = state.status is not None
    wait_failures = 0
    while not leader_reaped and wait_failures < 3:
        try:
            waited, candidate = os.waitpid(state.pid, 0)
            if waited != state.pid:
                cleanup_errors.append(
                    ReadinessError("owned readiness process leader was not reaped")
                )
                wait_failures += 1
                continue
            state.status = candidate
            leader_reaped = True
        except InterruptedError:
            continue
        except ChildProcessError:
            # A BaseException can land after waitpid reaped the leader but
            # before its status was assigned.  The leader is nevertheless
            # gone; exceptional callers do not consume its return status.
            leader_reaped = True
        except BaseException as exc:
            cleanup_errors.append(exc)
            wait_failures += 1

    group_absent = False
    for _ in range(3):
        try:
            group_absent = _wait_process_group_absent(state.pid, 5.0)
            break
        except BaseException as exc:
            cleanup_errors.append(exc)
    if not leader_reaped or not group_absent:
        error = ReadinessError(
            "owned readiness raw-fork leader/group cleanup could not be proven"
        )
        if cleanup_errors:
            raise error from cleanup_errors[0]
        raise error
    state.process_group_complete = True


def _wait_owned_raw_process_group(
    state: _OwnedRawProcess, timeout_seconds: float
) -> Tuple[bool, bool]:
    """Wait for a raw-fork leader and fully clean timeout/survivor groups."""

    deadline = time.monotonic() + timeout_seconds
    while state.status is None:
        try:
            waited, candidate = os.waitpid(state.pid, os.WNOHANG)
        except InterruptedError:
            continue
        if waited == state.pid:
            state.status = candidate
            break
        if waited != 0:
            _fail("readiness raw-fork wait returned an unexpected child")
        if time.monotonic() >= deadline:
            _terminate_owned_raw_process_group(state)
            return True, False
        time.sleep(0.005)
    surviving_group = _process_group_exists(state.pid)
    if surviving_group:
        _terminate_owned_raw_process_group(state)
    else:
        state.process_group_complete = True
    return False, surviving_group


def _join_cat_file_reader(reader: threading.Thread) -> None:
    """Join the pipe consumer, deferring BaseException until it has stopped."""

    pending: Optional[BaseException] = None
    while reader.is_alive():
        try:
            reader.join(timeout=0.05)
        except BaseException as exc:
            if pending is None:
                pending = exc
    if pending is not None:
        raise pending


def _terminate_process_group(process: subprocess.Popen, grace_seconds: float = 0.5) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            _fail("readiness process leader survived SIGKILL")
    if _process_group_exists(process.pid):
        _kill_process_group(process.pid, grace_seconds)


def _communicate_owned_process_group(
    process: subprocess.Popen,
    *,
    input_bytes: Optional[bytes] = None,
    timeout_seconds: float,
) -> Tuple[bytes, bytes, bool, bool]:
    """Communicate, then reap and prove absence of the owned process group."""

    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(
                input=input_bytes, timeout=timeout_seconds
            )
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_group(process)
            stdout, stderr = process.communicate()
    except BaseException:
        try:
            _terminate_process_group(process)
        finally:
            try:
                process.communicate()
            except BaseException:
                pass
        raise
    surviving_group = _process_group_exists(process.pid)
    if surviving_group:
        _kill_process_group(process.pid)
    process_group_complete = not _process_group_exists(process.pid)
    if not process_group_complete:
        _fail("owned readiness process group could not be removed")
    return stdout, stderr, timed_out, surviving_group


def run_self_tests(
    repo_root: Path,
    entrypoints: Sequence[EntrypointIdentity],
    expected_case_names: Mapping[str, Sequence[str]],
    attachment_root: Path,
    timeout_seconds: float = SELF_TEST_TIMEOUT_SECONDS,
    identity_check: Optional[Any] = None,
    descriptor_provider: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Path]]:
    if tuple(item.path for item in entrypoints) != ENTRYPOINTS:
        _fail("self-test entrypoint identity inventory is not exact")
    if set(expected_case_names) != set(ENTRYPOINTS):
        _fail("expected self-test case mapping does not cover exact entrypoints")
    if {
        path: tuple(names) for path, names in expected_case_names.items()
    } != EXPECTED_CASE_NAMES_BY_ENTRYPOINT:
        _fail("self-test case mapping differs from frozen entrypoint inventories")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        _fail("self-test timeout is invalid")
    if descriptor_provider is None:
        _fail("self-tests require descriptor-bound entrypoint execution")
    environment_digest = hashlib.sha256(encode_command_environment(SELF_TEST_ENVIRONMENT)).hexdigest()
    records = []
    attachments: Dict[str, Path] = {}
    for index, identity in enumerate(entrypoints):
        if identity_check is not None:
            identity_check()
        descriptor = descriptor_provider(identity.path)
        descriptor_path = "/proc/self/fd/{}".format(descriptor)
        if identity.path.endswith(".py"):
            argv = ["/usr/bin/python3", "-I", "-B", descriptor_path, "--self-test"]
            executable = None
        else:
            argv = [descriptor_path, "--self-test"]
            executable = descriptor_path
        started = _canonical_utc()
        try:
            process = subprocess.Popen(
                argv,
                executable=executable,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(SELF_TEST_ENVIRONMENT),
                cwd="/tmp",
                start_new_session=True,
                pass_fds=(descriptor,),
            )
        finally:
            os.close(descriptor)
        stdout, stderr, timed_out, surviving_group = _communicate_owned_process_group(
            process, timeout_seconds=float(timeout_seconds)
        )
        finished = _canonical_utc()
        if identity_check is not None:
            identity_check()
        stdout_relative = "readiness/self_tests/{:02d}.stdout".format(index)
        stderr_relative = "readiness/self_tests/{:02d}.stderr".format(index)
        attachments[stdout_relative] = _write_attachment(attachment_root, stdout_relative, stdout)
        attachments[stderr_relative] = _write_attachment(attachment_root, stderr_relative, stderr)
        if timed_out or surviving_group or process.returncode != 0:
            _fail("self-test failed or timed out: " + identity.path)
        parsed = _parse_self_test_stdout(stdout)
        validated = validate_self_test_result(
            parsed, identity.path, expected_case_names[identity.path]
        )
        records.append({
            "index": index,
            "path": identity.path,
            "argv": argv,
            "cwd": "/tmp",
            "entrypoint_sha256": identity.sha256,
            "started_utc": started,
            "finished_utc": finished,
            "environment_sha256": environment_digest,
            "exit_code": process.returncode,
            "timed_out": timed_out,
            "process_group_complete": True,
            "stdout": stdout_relative,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr": stderr_relative,
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "expected_case_names": list(expected_case_names[identity.path]),
            "result": validated,
            "bag_provider_calls": 0,
            "temporary_root_removed": True,
        })
    return records, attachments


def _regular_nonsymlink_file(path: Path, label: str) -> os.stat_result:
    before = os.lstat(str(path))
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        _fail(label + " is not a single-link regular nonsymlink file")
    return before


def _read_regular_file(path: Path, label: str, maximum: int = MAX_GIT_OUTPUT_BYTES) -> bytes:
    before = _regular_nonsymlink_file(path, label)
    descriptor = os.open(str(path), _file_flags())
    try:
        if not _same_binding(before, os.fstat(descriptor)):
            _fail(label + " changed while opening")
        content = _read_fd(descriptor, maximum)
        if not _same_binding(before, os.fstat(descriptor)):
            _fail(label + " changed while reading")
        return content
    finally:
        os.close(descriptor)


def _freeze_unit_artifact(source: Path, temporary_root: Path) -> Path:
    """Copy one finalized artifact through no-follow descriptors into /tmp."""

    source = Path(source).absolute()
    if source.resolve(strict=True) != source:
        _fail("unit artifact path is not canonical")
    before_root = os.lstat(str(source))
    if (
        not stat.S_ISDIR(before_root.st_mode)
        or stat.S_ISLNK(before_root.st_mode)
        or stat.S_IMODE(before_root.st_mode) != 0o555
    ):
        _fail("unit artifact root is not a finalized real directory")
    root_fd = os.open(str(source), _directory_flags())
    destination = temporary_root / "unit-artifact-frozen"
    destination.mkdir(mode=0o700)
    count = 0
    total = 0

    def copy_directory(source_fd: int, destination_path: Path, prefix: str) -> None:
        nonlocal count, total
        directory_before = os.fstat(source_fd)
        names = sorted(os.listdir(source_fd), key=lambda name: _utf8(name, "unit artifact path"))
        for name in names:
            if not name or name in (".", "..") or "/" in name or "\0" in name:
                _fail("unit artifact contains an unsafe path component")
            relative = name if not prefix else prefix + "/" + name
            _relpath(relative, "unit artifact path")
            status_value = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
            count = _checked_add(count, 1, "unit artifact entry count")
            if count > MAX_SNAPSHOT_ENTRIES:
                _fail("unit artifact entry count exceeds bound")
            if stat.S_ISDIR(status_value.st_mode):
                if stat.S_IMODE(status_value.st_mode) != 0o555:
                    _fail("unit artifact directory is not finalized 0555: " + relative)
                child_fd = os.open(name, _directory_flags(), dir_fd=source_fd)
                if not _same_binding(status_value, os.fstat(child_fd)):
                    os.close(child_fd)
                    _fail("unit artifact directory changed while opening: " + relative)
                child_destination = destination_path / name
                child_destination.mkdir(mode=0o700)
                try:
                    copy_directory(child_fd, child_destination, relative)
                    if not _same_binding(status_value, os.fstat(child_fd)):
                        _fail("unit artifact directory changed while copying: " + relative)
                finally:
                    os.close(child_fd)
                os.chmod(str(child_destination), 0o555)
                continue
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                _fail("unit artifact link/special/hardlink is forbidden: " + relative)
            expected_mode = 0o555 if relative.startswith("binaries/") else 0o444
            if stat.S_IMODE(status_value.st_mode) != expected_mode:
                _fail("unit artifact file mode is not finalized: " + relative)
            if status_value.st_size <= 0 or status_value.st_size > MAX_UNIT_ARTIFACT_FILE_BYTES:
                _fail("unit artifact file size is invalid: " + relative)
            total = _checked_add(total, status_value.st_size, "unit artifact total bytes")
            if total > MAX_UNIT_ARTIFACT_TOTAL_BYTES:
                _fail("unit artifact total bytes exceed bound")
            descriptor = os.open(name, _file_flags(), dir_fd=source_fd)
            destination_file = destination_path / name
            output = -1
            try:
                if not _same_binding(status_value, os.fstat(descriptor)):
                    _fail("unit artifact file changed while opening: " + relative)
                output = os.open(
                    str(destination_file),
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                remaining = status_value.st_size
                while remaining:
                    block = os.read(descriptor, min(remaining, 1024 * 1024))
                    if not block:
                        _fail("unit artifact file became truncated: " + relative)
                    view = memoryview(block)
                    while view:
                        written = os.write(output, view)
                        if written <= 0:
                            _fail("unit artifact frozen copy made no progress")
                        view = view[written:]
                    remaining -= len(block)
                if os.read(descriptor, 1):
                    _fail("unit artifact file grew while copying: " + relative)
                if not _same_binding(status_value, os.fstat(descriptor)):
                    _fail("unit artifact file changed while copying: " + relative)
                os.fsync(output)
            finally:
                if output >= 0:
                    os.close(output)
                os.close(descriptor)
            os.chmod(str(destination_file), expected_mode)
        if _stat_signature(os.fstat(source_fd)) != _stat_signature(directory_before):
            _fail("unit artifact directory metadata changed while copying")

    try:
        if not _same_binding(before_root, os.fstat(root_fd)):
            _fail("unit artifact root changed while opening")
        copy_directory(root_fd, destination, "")
        after_path = os.lstat(str(source))
        if (
            not _same_binding(before_root, os.fstat(root_fd))
            or not _same_binding(before_root, after_path)
        ):
            _fail("unit artifact root binding changed while copying")
    except BaseException:
        _remove_private_tree(destination)
        raise
    finally:
        os.close(root_fd)
    os.chmod(str(destination), 0o555)
    return destination


def _invoke_unit_verifier(
    repository: OpaqueGitRepository,
    artifact: Path,
    manifest_digest: str,
    attachment_root: Path,
) -> Tuple[Dict[str, Any], Dict[str, Path], str, str, Path, bytes]:
    original_artifact = Path(artifact).absolute()
    repository.revalidate()
    artifact = _freeze_unit_artifact(original_artifact, attachment_root)
    repository.revalidate()
    manifest = artifact / "SHA256SUMS"
    manifest_bytes = _read_regular_file(manifest, "unit manifest")
    actual_manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_digest != _sha256(manifest_digest, "unit manifest anchor"):
        _fail("unit artifact manifest differs from external anchor")
    source_context = repository.prevalidated_source_context()
    source_context_bytes = json.dumps(
        source_context,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    verifier_fd = repository.duplicate_tracked_fd("scripts/cp2/verify_report.py")
    verifier = "/proc/self/fd/{}".format(verifier_fd)
    argv = [
        "/usr/bin/python3", "-I", "-B", verifier,
        "--verify-unit-anchor-prevalidated", str(artifact),
        "--expected-manifest-sha256", manifest_digest,
    ]
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    started = _canonical_utc()
    repository.revalidate()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            cwd="/tmp",
            start_new_session=True,
            pass_fds=(verifier_fd,),
        )
    finally:
        os.close(verifier_fd)
    stdout, stderr, timed_out, surviving_group = _communicate_owned_process_group(
        process, input_bytes=source_context_bytes, timeout_seconds=1800
    )
    finished = _canonical_utc()
    repository.revalidate()
    attachments = {}
    source_context_relative = "readiness/source_context.json"
    stdout_relative = "readiness/unit_verifier.stdout"
    stderr_relative = "readiness/unit_verifier.stderr"
    attachments[source_context_relative] = _write_attachment(
        attachment_root, source_context_relative, source_context_bytes
    )
    attachments[stdout_relative] = _write_attachment(attachment_root, stdout_relative, stdout)
    attachments[stderr_relative] = _write_attachment(attachment_root, stderr_relative, stderr)
    if timed_out or surviving_group or process.returncode != 0:
        _fail("independent unit artifact verification failed")
    result = _parse_self_test_stdout(stdout)
    if not isinstance(result, dict) or set(result) != {
        "commit", "passed", "record_type", "schema_version", "tree",
    }:
        _fail("prevalidated unit verifier result field inventory is not exact")
    if (
        result.get("schema_version") != 1
        or result.get("record_type") != "cp2_prevalidated_unit_verification_result"
        or result.get("passed") is not True
    ):
        _fail("prevalidated unit verifier did not report an exact pass")
    tested_commit = _hex40(result.get("commit"), "unit tested commit")
    tested_tree = _hex40(result.get("tree"), "unit tested tree")
    if tested_commit != repository.commit or tested_tree != repository.tree:
        _fail("prevalidated unit verifier result differs from held source")
    command = {
        "argv": argv,
        "cwd": "/tmp",
        "environment": environment,
        "started_utc": started,
        "finished_utc": finished,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "process_group_complete": True,
        "stdout": stdout_relative,
        "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        "stderr": stderr_relative,
        "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
        "source_context_sha256": hashlib.sha256(source_context_bytes).hexdigest(),
    }
    return command, attachments, tested_commit, tested_tree, artifact, source_context_bytes


@dataclass
class ReadinessAuthorization:
    record: Dict[str, Any]
    attachments: Dict[str, Path]
    unit_verification_command: Dict[str, Any]
    frozen_unit_artifact: Path
    temporary_root: Path
    lock_fd: int
    lock_path: Path
    repository: OpaqueGitRepository
    private_tree_seal: Optional[_PrivateTreeSeal]
    prebag_authorized: bool = True
    _output_staging: Optional[AuthorizedOutputStaging] = None
    _output_published: bool = False
    _worker_transition_started: bool = False
    _worker_mode: bool = False
    _closed: bool = False

    def revalidate(self) -> None:
        """Fail unless the authorization, data lock, and source remain bound."""

        if self._closed or not self.prebag_authorized:
            _fail("readiness authorization is closed")
        if self._worker_transition_started and not self._worker_mode:
            _fail("readiness authorization worker transition is incomplete")
        if self._worker_mode:
            if self.lock_fd != -1:
                _fail("worker authorization retained the data-lock capability")
        else:
            lock_status = os.fstat(self.lock_fd)
            path_status = os.lstat(str(self.lock_path))
            if (
                not stat.S_ISREG(lock_status.st_mode)
                or stat.S_ISLNK(path_status.st_mode)
                or not _same_binding(lock_status, path_status)
                or lock_status.st_uid != os.geteuid()
                or lock_status.st_nlink != 1
                or stat.S_IMODE(lock_status.st_mode) != 0o600
            ):
                _fail("held data lock identity changed")
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.repository.revalidate()
        if self.private_tree_seal is None:
            _fail("readiness authorization lacks its private-tree seal")
        if Path(self.temporary_root).absolute() != self.private_tree_seal.root:
            _fail("readiness authorization private-root path changed")
        self.private_tree_seal.revalidate(
            self.attachments, self.frozen_unit_artifact
        )

    def read_registry_once(self, relative_path: str = "project/datasets.yaml") -> bytes:
        """Return the one verified postauthorization registry byte buffer."""

        self.revalidate()
        content = self.repository.read_preauthorized_registry_once(relative_path)
        self.revalidate()
        return content

    def duplicate_source_fd(self, relative_path: str) -> int:
        """Return one held approved nonregistry source leaf after step 8."""

        if (
            not isinstance(relative_path, str)
            or relative_path not in POSTAUTHORIZATION_SOURCE_PATHS
        ):
            _fail("postauthorization source path is outside the exact allowlist")
        self.revalidate()
        descriptor = self.repository._duplicate_postauthorization_source_fd(
            relative_path
        )
        try:
            self.revalidate()
            os.lseek(descriptor, 0, os.SEEK_SET)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def output_staging_identity(
        self, staging: AuthorizedOutputStaging
    ) -> Tuple[int, ...]:
        """Return the immutable identity of the sole registered output root."""

        if (
            self._output_staging is None
            or staging != self._output_staging
            or self._output_published
        ):
            _fail("postauthorization output staging is not the live capability")
        self.revalidate()
        identity = self.repository._output_staging_identity(staging)
        self.revalidate()
        return identity

    def duplicate_output_partial_fd(
        self, staging: AuthorizedOutputStaging
    ) -> int:
        """Return a trusted-parent-owned duplicate of the exact output root."""

        if self._worker_mode:
            _fail("campaign worker may not duplicate the publication root")
        self.output_staging_identity(staging)
        descriptor = self.repository._duplicate_output_partial_fd(staging)
        try:
            self.revalidate()
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def output_publication_state(
        self, staging: AuthorizedOutputStaging
    ) -> str:
        """Reconcile the held inode with the hidden/final commit state.

        ``published`` is the sole durable success state.  A final-name inode
        observed before this authorization completed its post-publication
        checks is reported as ``published_uncommitted`` and remains a failure
        root eligible for the trusted marker.
        """

        if self._closed or not self.prebag_authorized:
            _fail("readiness authorization is closed")
        if self._worker_mode:
            _fail("campaign worker may not query publication state")
        if self._output_staging is None or staging != self._output_staging:
            _fail("postauthorization output publication token differs")
        location, mode = self.repository._output_publication_location(staging)
        if self._output_published and (
            location != "published" or mode != 0o555
        ):
            _fail(
                "committed output publication lost its sealed final binding"
            )
        state = (
            "published"
            if self._output_published
            else (
                "published_uncommitted"
                if location == "published"
                else "hidden"
            )
        )
        return state

    def duplicate_output_root_fd(
        self, staging: AuthorizedOutputStaging
    ) -> int:
        """Duplicate the held root even after an interrupted final rename."""

        if self._worker_mode:
            _fail("campaign worker may not duplicate the publication root")
        state = self.output_publication_state(staging)
        descriptor = self.repository._duplicate_output_root_fd(staging)
        try:
            if self.output_publication_state(staging) != state:
                _fail("output publication state changed during duplication")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def current_output_publication_state(self) -> str:
        """Return ``absent`` or the authoritative sole-output state."""

        if self._closed or not self.prebag_authorized or self._worker_mode:
            _fail("readiness authorization cannot report output state")
        if self._output_staging is None:
            return "absent"
        return self.output_publication_state(self._output_staging)

    def descriptor_inventory(self) -> Dict[str, Dict[str, Any]]:
        """Expose every descriptor capability retained by this authorization."""

        self.revalidate()
        inventory = self.repository.descriptor_inventory()
        if self.private_tree_seal is None:
            _fail("readiness authorization lacks its private-tree seal")
        for label, record in self.private_tree_seal.descriptor_inventory().items():
            if label in inventory:
                _fail("authorization descriptor labels are duplicated")
            inventory[label] = record
        if not self._worker_mode:
            inventory["authorization.data_lock"] = _descriptor_inventory_record(
                self.lock_fd, "readiness data lock"
            )
        descriptors = [record["fd"] for record in inventory.values()]
        if len(set(descriptors)) != len(descriptors):
            _fail("authorization descriptor inventory is aliased")
        if self._worker_mode and any(
            record["access_mode"] != os.O_RDONLY
            or record["close_on_exec"] is not True
            for record in inventory.values()
        ):
            _fail("worker authorization retained a writable or inheritable fd")
        return inventory

    def rebind_worker_mount_namespace(
        self, staging: AuthorizedOutputStaging
    ) -> None:
        """Enter one-shot worker mode with only current-mount read handles."""

        if self._worker_mode:
            _fail("readiness authorization is already in worker mode")
        expected_identity = self.output_staging_identity(staging)
        if self.private_tree_seal is None:
            _fail("readiness authorization lacks its private-tree seal")
        current_origin = _process_mount_namespace_identity()
        for label, origin in (
            ("repository", self.repository._descriptor_origin),
            ("private readiness", self.private_tree_seal._descriptor_origin),
        ):
            if current_origin[0] == origin[0] or current_origin[1:] == origin[1:]:
                _fail(label + " worker transition requires a child mount namespace")
        self._worker_transition_started = True
        self.repository.rebind_worker_mount_namespace()
        self.private_tree_seal.rebind_worker_mount_namespace(
            self.attachments, self.frozen_unit_artifact
        )
        if self.repository._output_staging_identity(staging) != expected_identity:
            _fail("output identity changed across worker descriptor rebinding")

        inherited_lock = self.lock_fd
        if inherited_lock < 0:
            _fail("worker transition lacks its inherited data-lock descriptor")
        os.close(inherited_lock)
        try:
            os.fstat(inherited_lock)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
        else:
            _fail("worker transition did not close its data-lock descriptor")
        self.lock_fd = -1
        self._worker_mode = True
        self.descriptor_inventory()

    def create_output_staging(self, run_id: str) -> AuthorizedOutputStaging:
        """Consume the sole capability for an exact CP2-C output partial."""

        if self._worker_mode:
            _fail("campaign worker may not create an output staging root")
        if self._output_staging is not None:
            _fail("postauthorization output capability was already consumed")
        self.revalidate()
        staging = self.repository._create_postauthorization_output_staging(run_id)
        self._output_staging = staging
        try:
            self.revalidate()
        except BaseException as original_error:
            try:
                self.repository._rollback_empty_postauthorization_output(staging)
                self._output_staging = None
            except BaseException as rollback_error:
                raise ReadinessError(
                    "output authorization failed and exact rollback failed: "
                    + str(rollback_error)
                ) from original_error
            raise
        return staging

    def publish_output(self, staging: AuthorizedOutputStaging) -> None:
        """Publish the exact held partial once, without replacement."""

        if self._worker_mode:
            _fail("campaign worker may not publish an output staging root")
        if (
            self._output_staging is None
            or staging != self._output_staging
            or self._output_published
        ):
            _fail("postauthorization output publication is not authorized")
        self.revalidate()
        self.repository._publish_postauthorization_output(staging)
        try:
            self.revalidate()
        except BaseException as original_error:
            try:
                self.repository._rollback_published_postauthorization_output(
                    staging
                )
            except BaseException as rollback_error:
                raise ReadinessError(
                    "post-publication authorization failed and exact rollback "
                    "failed: " + str(rollback_error)
                ) from original_error
            raise
        self._output_published = True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.prebag_authorized = False
        child_transition = self._worker_transition_started or self._worker_mode
        first_error: Optional[BaseException] = None
        if self.lock_fd >= 0:
            lock_descriptor = self.lock_fd
            self.lock_fd = -1
            if not child_transition:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                except BaseException as exc:
                    first_error = exc
            try:
                os.close(lock_descriptor)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        try:
            self.repository.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if not child_transition:
            try:
                _remove_private_tree(self.temporary_root, self.private_tree_seal)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if self.private_tree_seal is not None:
            try:
                self.private_tree_seal.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "ReadinessAuthorization":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()


def run_readiness_barrier(
    repo_root: Path,
    unit_artifact: Path,
    unit_manifest_sha256: str,
    expected_case_names: Mapping[str, Sequence[str]],
    cp1_authorization_commit: str,
    lock_path: Path = Path(DATA_LOCK_PATH),
    self_test_timeout_seconds: float = SELF_TEST_TIMEOUT_SECONDS,
) -> ReadinessAuthorization:
    """Run readiness steps 2--8 without semantically reading the registry.

    CLI-only parsing/dispatch remains the responsibility of the tiny entrypoint
    bootstrap that calls this function.  A failure removes all temporary
    outputs, releases every descriptor/lock, and returns no authorization.
    """

    supplied_cases = {
        path: tuple(names) for path, names in expected_case_names.items()
    } if isinstance(expected_case_names, Mapping) else {}
    if supplied_cases != EXPECTED_CASE_NAMES_BY_ENTRYPOINT:
        _fail("caller self-test inventories differ from frozen entrypoint inventories")
    if cp1_authorization_commit != CP1_AUTHORIZATION_COMMIT:
        _fail("caller CP1 authorization commit differs from frozen authorization")

    repo_root = Path(repo_root).absolute()
    temporary_root = Path(tempfile.mkdtemp(prefix="schurvio-cp2-readiness-", dir="/tmp"))
    os.chmod(str(temporary_root), 0o700)
    repository: Optional[OpaqueGitRepository] = None
    private_tree_seal: Optional[_PrivateTreeSeal] = None
    lock_fd = -1
    attachments: Dict[str, Path] = {}
    try:
        repository = OpaqueGitRepository(repo_root, temporary_root)
        repository.require_ancestor(CP1_AUTHORIZATION_COMMIT)
        source_before = repository.capture_source("source")
        build_before = repository.capture_repository_root("build", "build")
        results_before = repository.capture_repository_root("results", "results")
        testing_before = repository.capture_repository_root("Testing", "testing")
        before_payloads = {
            "source_before": source_before,
            "build_before": build_before,
            "results_before": results_before,
            "testing_before": testing_before,
        }
        for name, payload in before_payloads.items():
            relative = "readiness/{}.bin".format(name)
            attachments[relative] = _write_attachment(temporary_root, relative, payload)

        self_tests, self_attachments = run_self_tests(
            repo_root,
            repository.entrypoints,
            EXPECTED_CASE_NAMES_BY_ENTRYPOINT,
            temporary_root,
            timeout_seconds=self_test_timeout_seconds,
            identity_check=repository.revalidate,
            descriptor_provider=repository.duplicate_tracked_fd,
        )
        attachments.update(self_attachments)

        source_after = repository.capture_source("source")
        build_after = repository.capture_repository_root("build", "build")
        results_after = repository.capture_repository_root("results", "results")
        testing_after = repository.capture_repository_root("Testing", "testing")
        after_payloads = {
            "source_after": source_after,
            "build_after": build_after,
            "results_after": results_after,
            "testing_after": testing_after,
        }
        if source_before != source_after:
            _fail("source readiness snapshot changed during self-tests")
        for label, before, after in (
            ("build", build_before, build_after),
            ("results", results_before, results_after),
            ("testing", testing_before, testing_after),
        ):
            if before != after:
                _fail(label + " readiness snapshot changed during self-tests")
        for name, payload in after_payloads.items():
            relative = "readiness/{}.bin".format(name)
            attachments[relative] = _write_attachment(temporary_root, relative, payload)

        lock_fd = acquire_data_lock(lock_path)
        lock_status = os.fstat(lock_fd)
        data_lock_record = {
            "path": str(Path(lock_path).absolute()),
            "device": lock_status.st_dev,
            "inode": lock_status.st_ino,
            "mode": stat.S_IMODE(lock_status.st_mode),
            "owner_uid": lock_status.st_uid,
            "link_count": lock_status.st_nlink,
            "acquired_exclusive": True,
        }
        post_lock = repository.capture_source("post_lock")
        _, _, source_identity = parse_source_snapshot(source_before)
        _, _, post_identity = parse_source_snapshot(post_lock)
        if (
            source_identity.commit != post_identity.commit
            or source_identity.tree != post_identity.tree
            or source_identity.index_tree != post_identity.index_tree
            or source_identity.status != post_identity.status
            or source_identity.entrypoints != post_identity.entrypoints
        ):
            _fail("post-lock source identity differs from pre-lock identity")
        post_relative = "readiness/post_lock.bin"
        attachments[post_relative] = _write_attachment(temporary_root, post_relative, post_lock)

        (
            unit_command,
            unit_attachments,
            unit_commit,
            unit_tree,
            frozen_unit_artifact,
            source_context_bytes,
        ) = _invoke_unit_verifier(
            repository, Path(unit_artifact), unit_manifest_sha256, temporary_root
        )
        attachments.update(unit_attachments)
        post_unit = repository.capture_source("post_lock")
        if post_unit != post_lock:
            _fail("post-unit source identity differs from the locked source")
        post_unit_relative = "readiness/post_unit.bin"
        attachments[post_unit_relative] = _write_attachment(
            temporary_root, post_unit_relative, post_unit
        )
        if unit_commit != repository.commit or unit_tree != repository.tree:
            _fail("unit artifact commit/tree do not equal current readiness source")

        readiness_git_environment = repository.readiness_git_environment
        readiness_git_environment_record = {
            "environment_id": "readiness_git_v1",
            "variables": [
                {"name": name, "value": readiness_git_environment[name]}
                for name in sorted(
                    readiness_git_environment, key=lambda value: value.encode("utf-8")
                )
            ],
            "canonical_sha256": hashlib.sha256(
                encode_command_environment(readiness_git_environment)
            ).hexdigest(),
        }
        readiness_git_commands = repository.readiness_git_commands
        if len(readiness_git_commands) != 20:
            _fail("readiness Git command population is not the exact 20-command flow")
        readiness_git_outputs = repository.readiness_git_outputs
        if len(readiness_git_outputs) != 19:
            _fail("readiness Git retained-output population is not exactly 19")
        for relative, payload in readiness_git_outputs.items():
            attachments[relative] = _write_attachment(
                temporary_root, relative, payload
            )

        def rel(name: str) -> str:
            return "readiness/{}.bin".format(name)

        record = {
            "schema_version": 1,
            "record_type": "readiness_barrier",
            "entrypoints": [item.as_record() for item in repository.entrypoints],
            "source_before_sha256": hashlib.sha256(source_before).hexdigest(),
            "build_before_sha256": hashlib.sha256(build_before).hexdigest(),
            "source_before_payload": rel("source_before"),
            "build_before_payload": rel("build_before"),
            "results_before_payload": rel("results_before"),
            "results_before_sha256": hashlib.sha256(results_before).hexdigest(),
            "testing_before_payload": rel("testing_before"),
            "testing_before_sha256": hashlib.sha256(testing_before).hexdigest(),
            "self_tests": self_tests,
            "source_after_payload": rel("source_after"),
            "source_after_sha256": hashlib.sha256(source_after).hexdigest(),
            "build_after_payload": rel("build_after"),
            "build_after_sha256": hashlib.sha256(build_after).hexdigest(),
            "results_after_payload": rel("results_after"),
            "results_after_sha256": hashlib.sha256(results_after).hexdigest(),
            "testing_after_payload": rel("testing_after"),
            "testing_after_sha256": hashlib.sha256(testing_after).hexdigest(),
            "post_lock_payload": post_relative,
            "post_lock_recheck_sha256": hashlib.sha256(post_lock).hexdigest(),
            "post_unit_payload": post_unit_relative,
            "post_unit_recheck_sha256": hashlib.sha256(post_unit).hexdigest(),
            "source_context_payload": "readiness/source_context.json",
            "source_context_sha256": hashlib.sha256(source_context_bytes).hexdigest(),
            "data_lock": data_lock_record,
            "readiness_git_environment": readiness_git_environment_record,
            "readiness_git_commands": readiness_git_commands,
            "unit_verification": unit_command,
            "unit_artifact": str(Path(unit_artifact).absolute()),
            "unit_manifest_sha256": _sha256(unit_manifest_sha256, "unit manifest anchor"),
            "unit_tested_commit": unit_commit,
            "unit_tested_tree": unit_tree,
            "bag_provider_calls": 0,
            "passed": True,
        }
        repository.revalidate()
        private_tree_seal = _PrivateTreeSeal.capture(
            temporary_root, attachments, frozen_unit_artifact
        )
        repository.revalidate()
        authorization = ReadinessAuthorization(
            record=record,
            attachments=attachments,
            unit_verification_command=unit_command,
            frozen_unit_artifact=frozen_unit_artifact,
            temporary_root=temporary_root,
            lock_fd=lock_fd,
            lock_path=Path(lock_path).absolute(),
            repository=repository,
            private_tree_seal=private_tree_seal,
        )
        lock_fd = -1
        repository = None
        private_tree_seal = None
        return authorization
    except BaseException as original_error:
        cleanup_error: Optional[BaseException] = None
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except BaseException as exc:
                cleanup_error = exc
            try:
                os.close(lock_fd)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        if repository is not None:
            try:
                repository.close()
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
        try:
            _remove_private_tree(temporary_root, private_tree_seal)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        finally:
            if private_tree_seal is not None:
                try:
                    private_tree_seal.close()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
        if cleanup_error is not None:
            raise cleanup_error from original_error
        raise


__all__ = [
    "CP1_AUTHORIZATION_COMMIT",
    "COMMON_SELF_TEST_CASES",
    "DATA_LOCK_PATH",
    "ENTRYPOINTS",
    "EXPECTED_BRANCH",
    "EXPECTED_CASE_NAMES_BY_ENTRYPOINT",
    "EntrypointIdentity",
    "OpaqueGitRepository",
    "PREAUTHORIZATION_REGISTRY_PATH",
    "PREVALIDATED_SOURCE_RECORD_TYPE",
    "ReadinessAuthorization",
    "ReadinessError",
    "SELF_TEST_ENVIRONMENT",
    "SELF_TEST_TIMEOUT_SECONDS",
    "SNAPSHOT_DOMAIN",
    "SnapshotEntry",
    "SourceIdentity",
    "acquire_data_lock",
    "encode_command_environment",
    "encode_snapshot",
    "encode_source_snapshot",
    "parse_snapshot",
    "parse_source_snapshot",
    "run_readiness_barrier",
    "run_self_tests",
    "strict_json_loads",
    "validate_self_test_result",
]
