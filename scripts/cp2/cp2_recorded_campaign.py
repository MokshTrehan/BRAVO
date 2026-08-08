#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Authorized CP2-C campaign orchestration.

This module is intentionally imported only after ``run_readiness_barrier`` has
passed and its one verified registry buffer has been obtained.  It never opens
``project/datasets.yaml``.  The functions below keep data identity, subprocess
provenance, runtime contexts, and staging writes fail-closed; mathematical
record translation is delegated to the fresh-build C++ assembler so owning
state partitions are never approximated in Python.
"""

from __future__ import annotations

from dataclasses import dataclass
import ctypes
import datetime as dt
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

import yaml

import cp2_capsule as capsule
import cp2_schema as schema


SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
OFFSETS = (40.0, 5.0, 0.0)
BAG_SHA256 = (
    "57f440ccd68ec8dc8f9461269f5909656b86198bac3adfd677b1fcc7a1428fa9",
    "c51b0064681dfb287b6653f5fd54e6c56af5d9151c866e17574fdbc527db2311",
    "6dc6192fac63dd0a05ba745548b41fe8cae14724168a98865a81d37e681bbc81",
)
STRICT_PAIR_DELTA_NS = 20_000_000
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
PAIR_INDEX_KEYS = (
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
SERIAL_PAIR_KEYS = PAIR_INDEX_KEYS + (
    "camera_timestamp_ns",
    "selected",
    "enqueue_entered",
    "enqueue_returned",
    "enqueue_status",
    "processing_entered",
    "processing_returned",
    "processing_status",
    "updater_invocation_ids",
)
STATIC_PATHS = (
    "config/euroc_mav/estimator_config.yaml",
    "config/euroc_mav/kalibr_imu_chain.yaml",
    "config/euroc_mav/kalibr_imucam_chain.yaml",
    "project/cp2_serial.launch",
)
STATIC_SHA256 = (
    "b706f0082106e49e20c3292147d238b7e225b0df414106b9d4ac009bbb123f3b",
    "408ea8b60b5f9e7c8251e6d302f04c0675bfefdd31229bb1afd139bc8f4a0287",
    "b9e11b7bcda102f7c8c384c97318d67f3916b58942f9073722f83c22bd7073f7",
    "a29c9b74aa6d4f0a5d783d0ba1eadeaca49ad0783f3b8121c5b68c8091023a12",
)
STATIC_BUNDLE_DOMAIN = b"SchurVIO-CP2-static-config-v1\0"
CP1_AUTHORIZATION_COMMIT = "8d80f483752411d34a3bc4c1ff6330b3a5c0fef3"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
MAX_REGISTRY_BYTES = 16 * 1024 * 1024
MAX_COMMAND_SECONDS = 7200.0
MAX_PROCESS_GROUP_CLEANUP_SECONDS = 30.0
PROCESS_GROUP_POLL_SECONDS = 0.01
MAX_SOURCE_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_DEPENDENCY_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_DEPENDENCY_MEMBER_BYTES = 256 * 1024 * 1024
MAX_DEPENDENCY_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_DEPENDENCY_MEMBERS = 100000
MAX_ELF_BYTES = 1024 * 1024 * 1024
MAX_ISOLATED_WORKER_MESSAGE_BYTES = 256 * 1024
MAX_FAILURE_MESSAGE_BYTES = 8192
MAX_CLEANUP_FAILURES = 16
MAX_CAPSULE_PROFILE_BYTES = 16 * 1024 * 1024
MAX_RUNTIME_TRACE_BYTES = 4 * 1024 * 1024 * 1024
UNIT_CAPSULE_PATHS = capsule.EXPECTED_CAPSULE_UNIT_PATHS

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
C_SINK_NAMES = {
    "serial_trace": "serial.jsonl",
    "callback_trace": None,
    "trajectory_trace": None,
    "updater_trace": "updater.journal",
    "state_payload": None,
    "proposal_payload": None,
    "raw_system_payload": None,
    "timing_trace": None,
    "runtime_parameters": "runtime_parameters.yaml",
    "loader_map_before": "loader_before.txt",
    "loader_map_after": "loader_after.txt",
    "legacy_state": None,
    "legacy_deviation": None,
    "legacy_timing": None,
}

# Linux x86_64 namespace/mount constants.  The recorded worker runs as PID 1
# in a fresh PID namespace and sees the host tree recursively read-only except
# for three precreated, descriptor-bound roots.  The trusted parent alone owns
# cleanup and final no-replace publication.
_CLONE_NEWNS = 0x00020000
_CLONE_NEWPID = 0x20000000
_CLONE_NEWUSER = 0x10000000
_MS_NOSUID = 2
_MS_NODEV = 4
_MS_NOEXEC = 8
_MS_BIND = 4096
_MS_REC = 16384
_MS_PRIVATE = 1 << 18
_MNT_DETACH = 2
_AT_FDCWD = -100
_AT_RECURSIVE = 0x8000
_OPEN_TREE_CLONE = 1
_OPEN_TREE_CLOEXEC = 0o2000000
_MOVE_MOUNT_F_EMPTY_PATH = 0x00000004
_SYS_OPEN_TREE_X86_64 = 428
_SYS_MOVE_MOUNT_X86_64 = 429
_SYS_PIDFD_SEND_SIGNAL_X86_64 = 424
_SYS_PIDFD_OPEN_X86_64 = 434
_SYS_MOUNT_SETATTR_X86_64 = 442
_MOUNT_ATTR_RDONLY = 0x00000001
_PR_SET_PDEATHSIG = 1
_PR_CAPBSET_DROP = 24
_PR_SET_NO_NEW_PRIVS = 38
_PR_CAP_AMBIENT = 47
_PR_CAP_AMBIENT_CLEAR_ALL = 4
_LINUX_CAPABILITY_VERSION_3 = 0x20080522
CONTRACT_INPUTS = (
    "docs/cp2_artifact_schema.md",
    "docs/cp2_c_composite_and_readiness_clarification.md",
    "docs/cp2_c_detached_readiness_binding_clarification_proposed.md",
    "docs/cp2_d_alignment_uniqueness_clarification_proposed.md",
    "docs/cp2_d_evaluator_precision_clarification_proposed.md",
    "docs/cp2_e_offline_descendant_confinement_clarification_proposed.md",
    "docs/cp2_e_fixed_clock_and_exact_timing_clarification_proposed.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_predata_incident_log.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "project/cp1_gate.yaml",
    "project/cp2_c_clarification_approval.json",
    "project/cp2_c_detached_readiness_binding_approval.json",
    "project/cp2_completion_authorization_binding.json",
    "project/cp2_completion_chained_authorization.txt",
    "project/cp2_d_completion_authorization_addendum.txt",
    "project/cp2_e_completion_authorization_addendum.txt",
    "project/cp2_full_completion_authorization_20260807.txt",
    "project/cp2_predata_incident_disposition_approval.json",
    "project/cp2_gate.yaml",
)
STRICT_FP_SOURCE_TARGETS = {
    "ov_msckf/src/ros1_serial_msckf.cpp": "ros1_serial_msckf",
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.cpp": "ov_msckf_lib",
    "ov_msckf/src/state/StateHelper.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2Canonical.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CommitBoundary.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CommitOracle.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CompositeState.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2FeatureGate.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2OfflineReplay.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2OutputCapability.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2RecordedAssemble.cpp": "cp2_recorded_assemble",
    "ov_msckf/src/update/CP2RuntimeContext.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2SerialPairing.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2TimingClock.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2ShadowMath.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2StateTraceCodec.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2TraceCodec.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2TraceJournal.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/SchurUpdate.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterHelper.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterMSCKF.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp": "ov_msckf_lib",
}
STRICT_FP_SOURCES = tuple(sorted(STRICT_FP_SOURCE_TARGETS))
STRICT_REQUIRED_MACRO_DEFINITIONS = {
    "EIGEN_DONT_VECTORIZE": (
        "-DEIGEN_DONT_VECTORIZE", "-DEIGEN_DONT_VECTORIZE=1"),
    "EIGEN_MAX_ALIGN_BYTES": ("-DEIGEN_MAX_ALIGN_BYTES=16",),
    "EIGEN_MAX_STATIC_ALIGN_BYTES": (
        "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",),
}
STRICT_FORBIDDEN_FLAGS = {
    "-Ofast", "-fassociative-math", "-fcx-limited-range", "-ffast-math",
    "-ffinite-math-only", "-fno-rounding-math", "-fno-signaling-nans",
    "-fno-trapping-math", "-freciprocal-math",
    "-funsafe-math-optimizations",
}


class CampaignError(RuntimeError):
    """A fail-closed campaign contract violation."""


def _bounded_failure_record(failure: BaseException) -> Dict[str, Any]:
    """Retain exact short diagnostics and a digest/size for every message."""

    raw_message = str(failure).encode("utf-8", errors="replace")
    retained = raw_message[:MAX_FAILURE_MESSAGE_BYTES]
    while retained:
        try:
            decoded = retained.decode("utf-8", errors="strict")
            break
        except UnicodeDecodeError as exc:
            if exc.end != len(retained):
                raise
            retained = retained[:-1]
    else:
        decoded = ""
    return {
        "type": type(failure).__name__,
        "message": decoded,
        "message_utf8_sha256": hashlib.sha256(raw_message).hexdigest(),
        "message_utf8_size": len(raw_message),
        "message_truncated": len(raw_message) > len(retained),
    }


def _validate_failure_record(record: Any, label: str) -> Dict[str, Any]:
    required = {
        "type", "message", "message_utf8_sha256", "message_utf8_size",
        "message_truncated",
    }
    if (
        not isinstance(record, Mapping)
        or set(record) != required
        or not isinstance(record.get("type"), str)
        or not record.get("type")
        or not isinstance(record.get("message"), str)
        or not isinstance(record.get("message_utf8_sha256"), str)
        or re.fullmatch(
            r"[0-9a-f]{64}", record.get("message_utf8_sha256", "")
        ) is None
        or type(record.get("message_utf8_size")) is not int
        or record.get("message_utf8_size") < 0
        or not isinstance(record.get("message_truncated"), bool)
    ):
        _fail(label + " failure record shape differs")
    retained = record["message"].encode("utf-8")
    if (
        len(retained) > MAX_FAILURE_MESSAGE_BYTES
        or record["message_utf8_size"] < len(retained)
        or record["message_truncated"]
        != (record["message_utf8_size"] > len(retained))
        or (
            not record["message_truncated"]
            and hashlib.sha256(retained).hexdigest()
            != record["message_utf8_sha256"]
        )
    ):
        _fail(label + " failure record content differs")
    return dict(record)


class _CampaignFailureBundle(CampaignError):
    """One primary failure plus an ordered, independently retained cleanup set."""

    def __init__(
        self,
        primary_failure: Mapping[str, Any],
        cleanup_failures: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.primary_failure = _validate_failure_record(
            primary_failure, "primary"
        )
        if (
            not isinstance(cleanup_failures, Sequence)
            or isinstance(cleanup_failures, (str, bytes, bytearray))
            or len(cleanup_failures) > MAX_CLEANUP_FAILURES
        ):
            _fail("cleanup failure population differs")
        self.cleanup_failures = tuple(
            _validate_failure_record(record, "cleanup")
            for record in cleanup_failures
        )
        summary = (
            self.primary_failure["type"] + ": "
            + self.primary_failure["message"]
        )
        if self.cleanup_failures:
            summary += "; cleanup: " + "; ".join(
                record["type"] + ": " + record["message"]
                for record in self.cleanup_failures
            )
        super().__init__(summary)


def _failure_components(
    failure: BaseException,
) -> Tuple[Dict[str, Any], Tuple[Dict[str, Any], ...]]:
    if isinstance(failure, _CampaignFailureBundle):
        return (
            dict(failure.primary_failure),
            tuple(dict(record) for record in failure.cleanup_failures),
        )
    return _bounded_failure_record(failure), ()


def _append_cleanup_failures(
    failure: BaseException,
    cleanup_failures: Sequence[BaseException],
) -> _CampaignFailureBundle:
    primary, retained_cleanup = _failure_components(failure)
    additions = tuple(
        _bounded_failure_record(item) for item in cleanup_failures
    )
    if len(retained_cleanup) + len(additions) > MAX_CLEANUP_FAILURES:
        _fail("cleanup failure population exceeds its exact bound")
    return _CampaignFailureBundle(
        primary, retained_cleanup + additions
    )


def _fail(message: str) -> None:
    raise CampaignError(message)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, ensure_ascii=False,
                       separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, content: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        status_value = os.fstat(descriptor)
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _fail("new output is not a regular single-link file: " + str(path))
        view = memoryview(content)
        offset = 0
        while offset < len(view):
            try:
                count = os.write(descriptor, view[offset:])
            except InterruptedError:
                continue
            if count <= 0:
                _fail("short zero write: " + str(path))
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_new(source: Path, destination: Path, mode: int = 0o600) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_fd = -1
    destination_fd = -1
    try:
        source_fd = os.open(str(source), os.O_RDONLY | os.O_CLOEXEC |
                            getattr(os, "O_NOFOLLOW", 0))
        destination_fd = os.open(
            str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL |
            os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0), mode)
        source_status = os.fstat(source_fd)
        destination_status = os.fstat(destination_fd)
        if (not stat.S_ISREG(source_status.st_mode) or source_status.st_nlink != 1 or
                not stat.S_ISREG(destination_status.st_mode) or
                destination_status.st_nlink != 1):
            _fail("copy endpoint is not a regular single-link file")
        while True:
            try:
                block = os.read(source_fd, 1024 * 1024)
            except InterruptedError:
                continue
            if not block:
                break
            offset = 0
            while offset < len(block):
                try:
                    count = os.write(destination_fd, block[offset:])
                except InterruptedError:
                    continue
                if count <= 0:
                    _fail("copy produced a zero write")
                offset += count
        os.fsync(destination_fd)
    finally:
        try:
            if source_fd >= 0:
                os.close(source_fd)
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY |
                         os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_fd_all(descriptor: int, maximum: int, label: str) -> bytes:
    status_value = os.fstat(descriptor)
    if (not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1 or
            status_value.st_size > maximum):
        _fail(label + " is not a bounded regular single-link file")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    total = 0
    while True:
        try:
            block = os.read(descriptor, min(1024 * 1024, maximum - total + 1))
        except InterruptedError:
            continue
        if not block:
            break
        total += len(block)
        if total > maximum:
            _fail(label + " exceeds its byte bound")
        chunks.append(block)
    after = os.fstat(descriptor)
    if not _same_stat(status_value, after):
        _fail(label + " changed while being read")
    return b"".join(chunks)


def _same_stat(left: os.stat_result, right: os.stat_result) -> bool:
    names = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
             "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(left, name) == getattr(right, name) for name in names)


def _stat_tuple(value: os.stat_result) -> Tuple[int, ...]:
    return tuple(
        getattr(value, name)
        for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
            "st_size", "st_mtime_ns", "st_ctime_ns",
        )
    )


def _require_read_only_descriptor(descriptor: int, label: str) -> None:
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except OSError as exc:
        raise CampaignError(label + " access mode cannot be read") from exc
    if flags & os.O_ACCMODE != os.O_RDONLY:
        _fail(label + " capability is writable")


class _HeldRuntimeOutputs:
    """Parent-created, read-only held capabilities for one CP2-C trace.

    The estimator receives only ``/proc/<runner>/fd/<n>``-derived
    capabilities for the exact empty inodes created here.  The trusted parent
    keeps a read-only descriptor to every inode, accepts each producer fill
    exactly once, and revalidates identity and bytes around every later child
    command.  The parent directory is held as well, so a path replacement
    cannot silently redirect either production or later verification.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self.directory_fd = os.open(
            str(self.path),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        _require_read_only_descriptor(
            self.directory_fd, "runtime trace directory"
        )
        self._directory_identity = os.fstat(self.directory_fd)
        self._files: Dict[
            str, Tuple[int, os.stat_result, os.stat_result, Optional[bytes]]
        ] = {}
        self._revalidate_directory()

    def _revalidate_directory(self) -> None:
        if self.directory_fd < 0:
            _fail("runtime trace directory capability is closed")
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
        """Create every declared member once and retain a read-only FD."""

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
                        os.O_RDWR
                        | os.O_CREAT
                        | os.O_EXCL
                        | os.O_NOFOLLOW
                        | os.O_CLOEXEC,
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
                    held = os.open(
                        "/proc/self/fd/{}".format(creator),
                        os.O_RDONLY | os.O_CLOEXEC,
                    )
                    _require_read_only_descriptor(
                        held, "runtime trace " + name
                    )
                    held_status = os.fstat(held)
                    by_name = os.stat(
                        name,
                        dir_fd=self.directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        _stat_tuple(held_status) != _stat_tuple(initial)
                        or _stat_tuple(by_name) != _stat_tuple(initial)
                    ):
                        _fail(
                            "new runtime sink held/name binding differs: "
                            + name
                        )
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

    def _member(
        self, name: str
    ) -> Tuple[int, os.stat_result, os.stat_result, Optional[bytes]]:
        if name not in self._files:
            _fail("runtime trace member is not held: " + name)
        return self._files[name]

    def proc_path(self, name: str) -> Path:
        descriptor, _initial, _current, _payload = self._member(name)
        capability = Path(
            "/proc/{}/fd/{}".format(os.getpid(), descriptor)
        )
        if _stat_tuple(os.stat(str(capability))) != _stat_tuple(
            os.fstat(descriptor)
        ):
            _fail("runtime sink /proc capability differs: " + name)
        return capability

    def capability(self, name: str) -> str:
        descriptor, initial, _current, payload = self._member(name)
        if payload is not None:
            _fail("runtime sink capability was requested after fill: " + name)
        return "v1:{}:{}:{}".format(
            os.getpid(), descriptor, _bag_identity_argument(initial)
        )

    def _revalidate_file(self, name: str) -> None:
        descriptor, _initial, expected, payload = self._member(name)
        _require_read_only_descriptor(descriptor, "runtime trace " + name)
        current = os.fstat(descriptor)
        by_name = os.stat(
            name, dir_fd=self.directory_fd, follow_symlinks=False
        )
        if (
            _stat_tuple(current) != _stat_tuple(expected)
            or _stat_tuple(by_name) != _stat_tuple(expected)
        ):
            _fail("held runtime trace member changed: " + name)
        if payload is not None and _read_fd_all(
            descriptor,
            MAX_RUNTIME_TRACE_BYTES,
            "held runtime trace " + name,
        ) != payload:
            _fail("held runtime trace bytes changed: " + name)

    def revalidate(self) -> None:
        self._revalidate_directory()
        if set(os.listdir(self.directory_fd)) != set(self._files):
            _fail("runtime trace directory name inventory changed")
        for name in sorted(self._files, key=os.fsencode):
            self._revalidate_file(name)

    def fill_parent(self, name: str, payload: bytes) -> None:
        """Fill one trusted-parent member through the exact held inode."""

        _descriptor, initial, _current, prior = self._member(name)
        if prior is not None or not isinstance(payload, bytes) or not payload:
            _fail("parent runtime sink fill is invalid: " + name)
        self._revalidate_file(name)
        writer = -1
        try:
            writer = os.open(
                str(self.proc_path(name)), os.O_WRONLY | os.O_CLOEXEC
            )
            if _stat_tuple(os.fstat(writer)) != _stat_tuple(initial):
                _fail("parent runtime sink writer differs: " + name)
            view = memoryview(payload)
            while view:
                try:
                    count = os.write(writer, view)
                except InterruptedError:
                    continue
                if count <= 0:
                    _fail(
                        "parent runtime sink write made no progress: " + name
                    )
                view = view[count:]
            os.fchmod(writer, 0o444)
            os.fsync(writer)
        finally:
            if writer >= 0:
                os.close(writer)
        os.fsync(self.directory_fd)
        self._accept_completed(name, payload)

    def _accept_completed(
        self, name: str, expected_payload: Optional[bytes]
    ) -> bytes:
        descriptor, initial, _current, prior = self._member(name)
        if prior is not None:
            _fail("runtime sink was completed twice: " + name)
        current = os.fstat(descriptor)
        by_name = os.stat(
            name, dir_fd=self.directory_fd, follow_symlinks=False
        )
        if (
            (current.st_dev, current.st_ino)
            != (initial.st_dev, initial.st_ino)
            or _stat_tuple(current) != _stat_tuple(by_name)
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or current.st_uid != os.geteuid()
            or current.st_size <= 0
            or current.st_size > MAX_RUNTIME_TRACE_BYTES
        ):
            _fail("completed runtime sink identity differs: " + name)
        if stat.S_IMODE(current.st_mode) != 0o444:
            os.fchmod(descriptor, 0o444)
            os.fsync(descriptor)
            current = os.fstat(descriptor)
            by_name = os.stat(
                name, dir_fd=self.directory_fd, follow_symlinks=False
            )
        payload = _read_fd_all(
            descriptor,
            MAX_RUNTIME_TRACE_BYTES,
            "held runtime trace " + name,
        )
        if (
            _stat_tuple(os.fstat(descriptor)) != _stat_tuple(current)
            or _stat_tuple(by_name) != _stat_tuple(current)
            or stat.S_IMODE(current.st_mode) != 0o444
            or (expected_payload is not None and payload != expected_payload)
        ):
            _fail("completed runtime sink bytes/path differ: " + name)
        self._files[name] = (descriptor, initial, current, payload)
        return payload

    def accept_external(self, names: Sequence[str]) -> None:
        """Accept only the exact declared inodes filled by the estimator."""

        self._revalidate_directory()
        if set(os.listdir(self.directory_fd)) != set(self._files):
            _fail("runtime trace directory contains undeclared outputs")
        ordered = tuple(names)
        if len(set(ordered)) != len(ordered):
            _fail("runtime sink completion population contains duplicates")
        for name in ordered:
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
        ordered = tuple(names)
        if len(set(ordered)) != len(ordered):
            _fail("runtime trace unlink population contains duplicates")
        for name in ordered:
            self._revalidate_file(name)
            descriptor = self._member(name)[0]
            os.unlink(name, dir_fd=self.directory_fd)
            if os.fstat(descriptor).st_nlink != 0:
                _fail("runtime trace unlink did not remove held name: " + name)
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
            try:
                os.close(self.directory_fd)
            finally:
                self.directory_fd = -1


def _same_stat_without_ctime(
    left: os.stat_result, right: os.stat_result
) -> bool:
    return all(
        getattr(left, name) == getattr(right, name)
        for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
            "st_size", "st_mtime_ns",
        )
    )


@dataclass
class BoundRegularFile:
    path: Path
    descriptor: int
    identity: os.stat_result
    provenance_path: Optional[Path] = None

    def revalidate(self) -> None:
        current = os.fstat(self.descriptor)
        by_path = os.stat(str(self.path), follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                not _same_stat(current, self.identity) or
                not _same_stat(current, by_path)):
            _fail("bound file identity changed: " + str(self.path))

    def sha256(self) -> str:
        self.revalidate()
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            try:
                block = os.read(self.descriptor, 1024 * 1024)
            except InterruptedError:
                continue
            if not block:
                break
            digest.update(block)
        self.revalidate()
        return digest.hexdigest()

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)

    def __enter__(self) -> "BoundRegularFile":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()


@dataclass(frozen=True)
class _BagMountBinding:
    """One parent-held bag inode and its private read-only mount target."""

    original_path: Path
    target_path: Path
    source_descriptor: int
    source_identity: os.stat_result
    target_identity: os.stat_result


def _directory_object_identity(value: os.stat_result) -> Tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
        value.st_gid,
    )


@dataclass
class OwnedTemporaryWorkspace:
    """Descriptor-bind and safely remove one private dynamic build tree."""

    path: Path
    descriptor: int
    identity: Tuple[int, ...]
    owner_uid: int
    device: int

    @classmethod
    def create(
        cls,
        prefix: str = "schurvio-cp2-recorded-build-",
    ) -> "OwnedTemporaryWorkspace":
        if prefix not in {
            "schurvio-cp2-recorded-build-",
            "schurvio-cp2-final-verifier-",
            "schurvio-cp2-bag-bind-",
        }:
            _fail("private campaign workspace prefix is not approved")
        parent_fd = os.open(
            "/tmp",
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        name: Optional[str] = None
        path: Optional[Path] = None
        created = False
        descriptor = -1
        before: Optional[os.stat_result] = None
        try:
            for _ in range(128):
                candidate = prefix + os.urandom(16).hex()
                name = candidate
                try:
                    os.mkdir(candidate, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    name = None
                    continue
                except BaseException:
                    # The syscall may have succeeded before an asynchronous
                    # exception was delivered.  The preselected unique name
                    # gives the cleanup path an exact candidate to reconcile.
                    created = True
                    raise
                created = True
                break
            if not created or name is None:
                _fail("could not allocate a private campaign workspace")
            path = (Path("/tmp") / name).absolute()
            before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_uid != os.geteuid()
            ):
                _fail("private campaign workspace identity is invalid")
            descriptor = os.open(
                name,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent_fd,
            )
            opened = os.fstat(descriptor)
            if not _same_stat(before, opened):
                _fail("private campaign workspace changed while opening")
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            opened = os.fstat(descriptor)
            by_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                _directory_object_identity(opened)
                != _directory_object_identity(before)
                or not _same_stat(opened, by_path)
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                _fail("private campaign workspace mode transition differs")
            result = cls(
                path=path,
                descriptor=descriptor,
                identity=_directory_object_identity(opened),
                owner_uid=opened.st_uid,
                device=opened.st_dev,
            )
            result.revalidate()
            descriptor = -1
            return result
        except BaseException as original_error:
            cleanup_error: Optional[BaseException] = None
            try:
                if created and name is not None:
                    try:
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        if descriptor >= 0:
                            held = os.fstat(descriptor)
                            if held.st_nlink != 0:
                                _fail(
                                    "failed private campaign workspace escaped "
                                    "its allocated name"
                                )
                        created = False
                if created and name is not None and descriptor < 0:
                    descriptor = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_DIRECTORY
                        | os.O_CLOEXEC
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=parent_fd,
                    )
                if created and name is not None:
                    held = os.fstat(descriptor)
                    current = os.stat(
                        name, dir_fd=parent_fd, follow_symlinks=False
                    )
                    if (
                        not stat.S_ISDIR(held.st_mode)
                        or held.st_uid != os.geteuid()
                        or stat.S_IMODE(held.st_mode) != 0o700
                        or _directory_object_identity(held)
                        != _directory_object_identity(current)
                        or (
                            before is not None
                            and (
                                _directory_object_identity(held)
                                != _directory_object_identity(before)
                            )
                        )
                        or os.listdir(descriptor)
                    ):
                        _fail(
                            "failed private campaign workspace changed before "
                            "cleanup"
                        )
                    os.rmdir(name, dir_fd=parent_fd)
                    if os.fstat(descriptor).st_nlink != 0:
                        _fail(
                            "failed private campaign workspace survived cleanup"
                        )
                    os.fsync(parent_fd)
            except BaseException as exc:
                cleanup_error = exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                    descriptor = -1
            if cleanup_error is not None:
                raise CampaignError(
                    "private campaign workspace creation failed and exact "
                    "cleanup failed: " + str(cleanup_error)
                ) from original_error
            raise
        finally:
            os.close(parent_fd)

    def revalidate(self) -> None:
        if self.descriptor < 0:
            _fail("private campaign workspace is closed")
        held = os.fstat(self.descriptor)
        by_path = os.lstat(str(self.path))
        if (
            not stat.S_ISDIR(held.st_mode)
            or stat.S_ISLNK(by_path.st_mode)
            or _directory_object_identity(held) != self.identity
            or _directory_object_identity(by_path) != self.identity
            or held.st_uid != self.owner_uid
            or stat.S_IMODE(held.st_mode) != 0o700
            or stat.S_IMODE(by_path.st_mode) != 0o700
        ):
            _fail("private campaign workspace root binding changed")

    def rebind_current_mount_namespace(self) -> None:
        """Replace the inherited directory FD with this namespace's mount."""

        self.revalidate()
        old_descriptor = self.descriptor
        replacement = os.open(
            str(self.path),
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(replacement)
            if (
                _directory_object_identity(opened) != self.identity
                or stat.S_IMODE(opened.st_mode) != 0o700
            ):
                _fail("private workspace namespace rebind changed identity")
            self.descriptor = replacement
            replacement = -1
            os.close(old_descriptor)
            self.revalidate()
        finally:
            if replacement >= 0:
                os.close(replacement)

    def _remove_children(self, directory_fd: int, label: str) -> None:
        for name in sorted(os.listdir(directory_fd), key=lambda item: item.encode("utf-8")):
            if not name or name in (".", "..") or "/" in name or "\0" in name:
                _fail("private campaign workspace contains an unsafe name")
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if before.st_uid != self.owner_uid:
                _fail("private campaign workspace entry owner changed: " + label + name)
            if stat.S_ISDIR(before.st_mode):
                if before.st_dev != self.device:
                    _fail("private campaign workspace crossed a device: " + label + name)
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if _directory_object_identity(opened) != _directory_object_identity(before):
                        _fail("private campaign directory changed while opening: " + label + name)
                    self._remove_children(child_fd, label + name + "/")
                    by_path = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if (
                        _directory_object_identity(os.fstat(child_fd))
                        != _directory_object_identity(opened)
                        or _directory_object_identity(by_path)
                        != _directory_object_identity(opened)
                    ):
                        _fail("private campaign directory was substituted: " + label + name)
                    os.rmdir(name, dir_fd=directory_fd)
                    if os.fstat(child_fd).st_nlink != 0:
                        _fail(
                            "held private campaign directory survived cleanup: "
                            + label + name
                        )
                    os.fsync(directory_fd)
                finally:
                    os.close(child_fd)
            else:
                if not hasattr(os, "O_PATH") or before.st_nlink != 1:
                    _fail(
                        "private campaign leaf is not single-link/O_PATH-bindable: "
                        + label + name
                    )
                leaf_fd = os.open(
                    name,
                    os.O_PATH
                    | os.O_CLOEXEC
                    | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(leaf_fd)
                    current = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if (
                        not _same_stat(before, opened)
                        or not _same_stat(before, current)
                    ):
                        _fail(
                            "private campaign leaf was substituted: "
                            + label + name
                        )
                    os.unlink(name, dir_fd=directory_fd)
                    if os.fstat(leaf_fd).st_nlink != 0:
                        _fail(
                            "held private campaign leaf survived cleanup: "
                            + label + name
                        )
                    os.fsync(directory_fd)
                finally:
                    os.close(leaf_fd)

    def remove(self) -> None:
        """Remove only the still-bound owned tree; never follow a substitute."""

        if self.descriptor < 0:
            return
        removed = False
        try:
            self.revalidate()
            self._remove_children(self.descriptor, "")
            self.revalidate()
            if os.listdir(self.descriptor):
                _fail("private campaign workspace was repopulated during cleanup")
            os.fsync(self.descriptor)
            self.revalidate()
            if self.path.parent != Path("/tmp") or "/" in self.path.name:
                _fail("private campaign workspace parent/name changed")
            parent_fd = os.open(
                "/tmp",
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_CLOEXEC
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                by_parent = os.stat(
                    self.path.name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if _directory_object_identity(by_parent) != self.identity:
                    _fail("private campaign workspace changed before removal")
                os.rmdir(self.path.name, dir_fd=parent_fd)
                if os.fstat(self.descriptor).st_nlink != 0:
                    _fail("held private campaign workspace survived removal")
                removed = True
                try:
                    os.stat(
                        self.path.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    _fail("private campaign workspace path survived cleanup")
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except BaseException:
            if removed or os.fstat(self.descriptor).st_nlink == 0:
                descriptor = self.descriptor
                self.descriptor = -1
                os.close(descriptor)
            raise
        else:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)


class _MountAttr(ctypes.Structure):
    _fields_ = (
        ("attr_set", ctypes.c_uint64),
        ("attr_clr", ctypes.c_uint64),
        ("propagation", ctypes.c_uint64),
        ("userns_fd", ctypes.c_uint64),
    )


class _CapabilityHeader(ctypes.Structure):
    _fields_ = (
        ("version", ctypes.c_uint32),
        ("pid", ctypes.c_int),
    )


class _CapabilityData(ctypes.Structure):
    _fields_ = (
        ("effective", ctypes.c_uint32),
        ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    )


def _raise_namespace_errno(label: str) -> None:
    value = ctypes.get_errno()
    raise CampaignError(label + " failed: " + os.strerror(value))


def _write_complete(descriptor: int, payload: bytes, label: str) -> None:
    offset = 0
    while offset < len(payload):
        try:
            written = os.write(descriptor, payload[offset:])
        except InterruptedError:
            continue
        if written <= 0:
            _fail(label + " made no progress")
        offset += written


def _write_pipe_record(descriptor: int, record: Mapping[str, Any]) -> None:
    payload = _json_bytes(record)
    if len(payload) > MAX_ISOLATED_WORKER_MESSAGE_BYTES:
        _fail("isolated-worker status exceeds its exact bound")
    _write_complete(descriptor, payload, "isolated-worker status write")


def _read_pipe_record(descriptor: int) -> Mapping[str, Any]:
    content = bytearray()
    while True:
        try:
            block = os.read(descriptor, 4096)
        except InterruptedError:
            continue
        if not block:
            break
        content.extend(block)
        if len(content) > MAX_ISOLATED_WORKER_MESSAGE_BYTES:
            _fail("isolated-worker status exceeds its exact bound")
    if not content or content[-1:] != b"\n" or bytes(content).count(b"\n") != 1:
        _fail("isolated-worker status framing differs")
    try:
        result = schema.strict_json_loads(bytes(content))
    except schema.SchemaError as exc:
        raise CampaignError("isolated-worker status is not strict JSON") from exc
    if not isinstance(result, Mapping):
        _fail("isolated-worker status is not an object")
    return result


def _write_proc_map(path: str, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _write_complete(descriptor, content, "namespace identity-map write")
    finally:
        os.close(descriptor)


def _mount_set_readonly(path: Path, readonly: bool, recursive: bool) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    attributes = _MountAttr(
        _MOUNT_ATTR_RDONLY if readonly else 0,
        0 if readonly else _MOUNT_ATTR_RDONLY,
        0,
        0,
    )
    result = libc.syscall(
        ctypes.c_long(_SYS_MOUNT_SETATTR_X86_64),
        ctypes.c_int(_AT_FDCWD),
        ctypes.c_char_p(os.fsencode(str(path))),
        ctypes.c_uint(_AT_RECURSIVE if recursive else 0),
        ctypes.byref(attributes),
        ctypes.c_size_t(ctypes.sizeof(attributes)),
    )
    if result != 0:
        _raise_namespace_errno("mount_setattr " + str(path))


def _current_mount_points() -> Tuple[Path, ...]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(
            encoding="utf-8", errors="strict"
        ).splitlines()
    except (OSError, UnicodeError) as exc:
        raise CampaignError("cannot read the current mount inventory") from exc
    result = []
    for line in lines:
        fields = line.split(" ")
        if len(fields) < 7 or "-" not in fields:
            _fail("current mount inventory framing differs")
        encoded = fields[4]
        for escaped, literal in (
            ("\\040", " "), ("\\011", "\t"),
            ("\\012", "\n"), ("\\134", "\\"),
        ):
            encoded = encoded.replace(escaped, literal)
        if not encoded.startswith("/") or os.path.normpath(encoded) != encoded:
            _fail("current mount inventory contains a nonnormalized path")
        result.append(Path(encoded))
    return tuple(result)


def _install_worker_mount_and_pid_namespaces(
    writable_roots: Sequence[Tuple[Path, Tuple[int, ...]]],
    bag_mounts: Sequence[_BagMountBinding],
) -> None:
    """Bind exact capabilities, make all other paths read-only, stage PID NS."""

    platform = os.uname()
    if platform.sysname != "Linux" or platform.machine != "x86_64":
        _fail("CP2-C write sandbox requires native Linux x86_64")
    if len(os.listdir("/proc/self/task")) != 1:
        _fail("CP2-C write sandbox requires a single-threaded supervisor")
    if len(writable_roots) != 2:
        _fail("CP2-C write sandbox root population differs")
    if len(bag_mounts) != len(BAG_SHA256):
        _fail("CP2-C bag mount population differs")
    roots = tuple(Path(item[0]).absolute() for item in writable_roots)
    expected_root_identities = tuple(item[1] for item in writable_roots)
    if len(set(roots)) != len(roots) or any(
        item == Path("/") or str(item) != os.path.normpath(str(item))
        for item in roots
    ):
        _fail("CP2-C write sandbox roots are not exact and distinct")
    identities: List[Tuple[int, ...]] = []
    mount_points = _current_mount_points()
    for item, expected in zip(roots, expected_root_identities):
        value = item.lstat()
        if (
            not stat.S_ISDIR(value.st_mode)
            or stat.S_ISLNK(value.st_mode)
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) != 0o700
            or _directory_object_identity(value) != expected
        ):
            _fail("CP2-C write sandbox root identity/mode differs")
        if os.listdir(str(item)):
            _fail("CP2-C write sandbox root was prepopulated before binding")
        if any(
            mount == item or item in mount.parents for mount in mount_points
        ):
            _fail("CP2-C write sandbox root contains a pre-existing mount")
        identities.append(_directory_object_identity(value))
    bag_targets = set()
    for binding in bag_mounts:
        target = Path(binding.target_path).absolute()
        if (
            target in bag_targets
            or target.parent == Path("/")
            or os.path.normpath(str(target)) != str(target)
            or not str(target).startswith(str(target.parent) + os.path.sep)
        ):
            _fail("CP2-C bag mount target is not exact and distinct")
        if any(
            mount == target
            or mount == target.parent
            or target.parent in mount.parents
            for mount in mount_points
        ):
            _fail("CP2-C bag mount workspace contains a pre-existing mount")
        bag_targets.add(target)
        source_status = os.fstat(binding.source_descriptor)
        target_status = target.lstat()
        if (
            not _same_stat(source_status, binding.source_identity)
            or not stat.S_ISREG(source_status.st_mode)
            or source_status.st_nlink != 1
            or not _same_stat(target_status, binding.target_identity)
            or not stat.S_ISREG(target_status.st_mode)
            or target_status.st_nlink != 1
            or target_status.st_uid != os.geteuid()
            or stat.S_IMODE(target_status.st_mode) != 0o600
        ):
            _fail("CP2-C bag mount source/target identity differs")

    original_uid = os.geteuid()
    original_gid = os.getegid()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.unshare.argtypes = (ctypes.c_int,)
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS | _CLONE_NEWPID) != 0:
        _raise_namespace_errno("unshare user/mount/PID namespaces")
    _write_proc_map("/proc/self/setgroups", b"deny\n")
    _write_proc_map(
        "/proc/self/uid_map",
        (str(original_uid) + " " + str(original_uid) + " 1\n").encode("ascii"),
    )
    _write_proc_map(
        "/proc/self/gid_map",
        (str(original_gid) + " " + str(original_gid) + " 1\n").encode("ascii"),
    )
    if os.geteuid() != original_uid or os.getegid() != original_gid:
        _fail("namespace identity mapping changed the effective identity")

    libc.mount.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    libc.mount.restype = ctypes.c_int
    if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
        _raise_namespace_errno("private mount propagation")
    for item in roots:
        encoded = os.fsencode(str(item))
        if libc.mount(encoded, encoded, None, _MS_BIND | _MS_REC, None) != 0:
            _raise_namespace_errno("bind writable root " + str(item))
    for binding in bag_mounts:
        detached_mount = libc.syscall(
            ctypes.c_long(_SYS_OPEN_TREE_X86_64),
            ctypes.c_int(_AT_FDCWD),
            ctypes.c_char_p(os.fsencode(str(binding.original_path))),
            ctypes.c_uint(_OPEN_TREE_CLONE | _OPEN_TREE_CLOEXEC),
        )
        if detached_mount < 0:
            _raise_namespace_errno(
                "open_tree held recorded input " + str(binding.original_path)
            )
        try:
            if not _same_stat(
                os.fstat(detached_mount), binding.source_identity
            ):
                _fail("CP2-C detached bag mount changed source identity")
            moved = libc.syscall(
                ctypes.c_long(_SYS_MOVE_MOUNT_X86_64),
                ctypes.c_int(detached_mount),
                ctypes.c_char_p(b""),
                ctypes.c_int(_AT_FDCWD),
                ctypes.c_char_p(os.fsencode(str(binding.target_path))),
                ctypes.c_uint(_MOVE_MOUNT_F_EMPTY_PATH),
            )
            if moved != 0:
                _raise_namespace_errno(
                    "move_mount held recorded input "
                    + str(binding.target_path)
                )
        finally:
            os.close(detached_mount)
        mounted = os.lstat(str(binding.target_path))
        if not _same_stat(mounted, binding.source_identity):
            _fail("CP2-C read-only bag bind changed source identity")
    _mount_set_readonly(Path("/"), True, True)
    for item in roots:
        _mount_set_readonly(item, False, True)
    for item, expected in zip(roots, identities):
        current = item.lstat()
        if _directory_object_identity(current) != expected:
            _fail("CP2-C write sandbox root binding changed")
    for binding in bag_mounts:
        mounted = os.lstat(str(binding.target_path))
        if not _same_stat(mounted, binding.source_identity):
            _fail("CP2-C read-only bag binding changed after sealing")
        write_probe = -1
        try:
            write_probe = os.open(
                str(binding.target_path),
                os.O_WRONLY | os.O_CLOEXEC |
                getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
                raise
        else:
            _fail("CP2-C held bag mount is writable")
        finally:
            if write_probe >= 0:
                os.close(write_probe)

    denied = Path("/tmp") / (
        ".schurvio-cp2-denied-write-" + os.urandom(16).hex()
    )
    denied_fd = -1
    try:
        denied_fd = os.open(
            str(denied),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
            getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES, errno.EPERM):
            raise
    else:
        try:
            created = os.fstat(denied_fd)
            by_path = os.lstat(str(denied))
            if not _same_stat(created, by_path):
                _fail("denied write probe was substituted before cleanup")
            denied.unlink()
            if os.fstat(denied_fd).st_nlink != 0:
                _fail("denied write probe survived exact cleanup")
            _fsync_directory(denied.parent)
        finally:
            os.close(denied_fd)
            denied_fd = -1
        _fail("CP2-C write sandbox allowed an unbound /tmp creation")
    finally:
        if denied_fd >= 0:
            os.close(denied_fd)
    if os.path.lexists(str(denied)):
        _fail("CP2-C write-sandbox denial probe left a path")

    allowed_probe = roots[1] / (
        ".namespace-write-probe-" + os.urandom(16).hex()
    )
    allowed_fd = os.open(
        str(allowed_probe),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
        getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        _write_complete(allowed_fd, b"namespace write probe\n", "sandbox probe")
        os.fsync(allowed_fd)
    finally:
        try:
            before_unlink = os.fstat(allowed_fd)
            by_path = os.lstat(str(allowed_probe))
            if not _same_stat(before_unlink, by_path):
                _fail("sandbox write probe was substituted before cleanup")
            allowed_probe.unlink()
            if os.fstat(allowed_fd).st_nlink != 0:
                _fail("sandbox write probe survived exact cleanup")
            _fsync_directory(allowed_probe.parent)
        finally:
            os.close(allowed_fd)


def _install_trusted_verifier_mount_and_pid_namespaces(
    writable_workspace: Path,
    expected_workspace_identity: Tuple[int, ...],
) -> None:
    """Stage a fresh PID namespace with only one exact writable workspace."""

    platform = os.uname()
    if platform.sysname != "Linux" or platform.machine != "x86_64":
        _fail("trusted verifier isolation requires native Linux x86_64")
    if len(os.listdir("/proc/self/task")) != 1:
        _fail("trusted verifier isolation requires a single-threaded supervisor")
    workspace = Path(writable_workspace).absolute()
    if (
        workspace.parent != Path("/tmp")
        or not workspace.name.startswith("schurvio-cp2-final-verifier-")
        or os.path.normpath(str(workspace)) != str(workspace)
    ):
        _fail("trusted verifier writable workspace path differs")
    before = workspace.lstat()
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != os.geteuid()
        or stat.S_IMODE(before.st_mode) != 0o700
        or _directory_object_identity(before) != expected_workspace_identity
    ):
        _fail("trusted verifier writable workspace identity differs")
    if any(
        mount == workspace or workspace in mount.parents
        for mount in _current_mount_points()
    ):
        _fail("trusted verifier writable workspace contains a mount")

    original_uid = os.geteuid()
    original_gid = os.getegid()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = (ctypes.c_int,)
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(_CLONE_NEWUSER | _CLONE_NEWNS | _CLONE_NEWPID) != 0:
        _raise_namespace_errno("unshare trusted verifier namespaces")
    _write_proc_map("/proc/self/setgroups", b"deny\n")
    _write_proc_map(
        "/proc/self/uid_map",
        (str(original_uid) + " " + str(original_uid) + " 1\n").encode("ascii"),
    )
    _write_proc_map(
        "/proc/self/gid_map",
        (str(original_gid) + " " + str(original_gid) + " 1\n").encode("ascii"),
    )
    if os.geteuid() != original_uid or os.getegid() != original_gid:
        _fail("trusted verifier namespace identity mapping changed")

    libc.mount.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    libc.mount.restype = ctypes.c_int
    if libc.mount(None, b"/", None, _MS_REC | _MS_PRIVATE, None) != 0:
        _raise_namespace_errno("trusted verifier private mount propagation")
    encoded = os.fsencode(str(workspace))
    if libc.mount(encoded, encoded, None, _MS_BIND | _MS_REC, None) != 0:
        _raise_namespace_errno("bind trusted verifier writable workspace")
    _mount_set_readonly(Path("/"), True, True)
    _mount_set_readonly(workspace, False, True)
    after = workspace.lstat()
    if (
        _directory_object_identity(after) != expected_workspace_identity
        or stat.S_IMODE(after.st_mode) != 0o700
    ):
        _fail("trusted verifier writable workspace binding changed")


def _enter_worker_pid_namespace_and_drop_capabilities(
    capability_probe: Path,
) -> None:
    """Become PID 1, install the namespace-local procfs, and drop all caps."""

    libc = ctypes.CDLL(None, use_errno=True)
    libc.umount2.argtypes = (ctypes.c_char_p, ctypes.c_int)
    libc.umount2.restype = ctypes.c_int
    libc.mount.argtypes = (
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_void_p,
    )
    libc.mount.restype = ctypes.c_int
    proc_flags = _MS_NOSUID | _MS_NODEV | _MS_NOEXEC
    # A mount namespace created together with a user namespace receives
    # locked copies of inherited mounts; overmounting is permitted whereas
    # detaching that inherited procfs is deliberately rejected by the kernel.
    if libc.mount(b"proc", b"/proc", b"proc", proc_flags, None) != 0:
        _raise_namespace_errno("overmount PID-namespace procfs")
    _mount_set_readonly(Path("/proc"), True, True)
    if os.getpid() != 1 or len(os.listdir("/proc/self/task")) != 1:
        _fail("campaign worker is not the sole PID-namespace init")

    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        _raise_namespace_errno("PR_SET_NO_NEW_PRIVS")
    ambient_result = libc.prctl(
        _PR_CAP_AMBIENT,
        _PR_CAP_AMBIENT_CLEAR_ALL,
        0,
        0,
        0,
    )
    if ambient_result != 0 and ctypes.get_errno() != errno.EINVAL:
        _raise_namespace_errno("clear ambient capabilities")
    try:
        last_capability = int(
            Path("/proc/sys/kernel/cap_last_cap").read_text(
                encoding="ascii"
            ).strip()
        )
    except (OSError, ValueError) as exc:
        raise CampaignError("cannot read the kernel capability bound") from exc
    if last_capability < 0 or last_capability > 255:
        _fail("kernel capability bound is outside the supported range")
    for capability in range(last_capability + 1):
        if libc.prctl(_PR_CAPBSET_DROP, capability, 0, 0, 0) != 0:
            _raise_namespace_errno(
                "drop capability bounding-set bit " + str(capability)
            )
    header = _CapabilityHeader(_LINUX_CAPABILITY_VERSION_3, 0)
    data = (_CapabilityData * 2)()
    capset = getattr(libc, "capset", None)
    if capset is None:
        _fail("capset is unavailable")
    capset.argtypes = (ctypes.POINTER(_CapabilityHeader), ctypes.POINTER(_CapabilityData))
    capset.restype = ctypes.c_int
    if capset(ctypes.byref(header), data) != 0:
        _raise_namespace_errno("drop namespace capabilities")
    status_fields: Dict[str, str] = {}
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            status_fields[key] = value.strip()
    if any(
        status_fields.get(name) != "0000000000000000"
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    ):
        _fail("campaign worker retained a capability")
    if status_fields.get("NoNewPrivs") != "1":
        _fail("campaign worker did not retain no-new-privileges")

    encoded = os.fsencode(str(capability_probe))
    if libc.mount(encoded, encoded, None, _MS_BIND, None) == 0:
        libc.umount2(encoded, _MNT_DETACH)
        _fail("campaign worker retained mount authority")
    if ctypes.get_errno() not in (errno.EPERM, errno.EACCES):
        _raise_namespace_errno("post-drop mount denial")


def _set_parent_death_signal(signal_number: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(_PR_SET_PDEATHSIG, signal_number, 0, 0, 0) != 0:
        _raise_namespace_errno("PR_SET_PDEATHSIG")


def _bind_regular_file(path: Path) -> BoundRegularFile:
    path = Path(path)
    value = str(path)
    if (not path.is_absolute() or os.path.normpath(value) != value or
            value == os.path.sep or "\0" in value):
        _fail("file path is not normalized absolute: " + value)
    parts = PurePosixPath(value).parts
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                     getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    try:
        for component in parts[1:-1]:
            before = os.stat(component, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode):
                _fail("file parent is not a real directory: " + value)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY |
                            os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=parent)
            if not _same_stat(before, os.fstat(child)):
                os.close(child)
                _fail("file parent binding changed: " + value)
            os.close(parent)
            parent = child
        before_leaf = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
        identity = os.fstat(descriptor)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or
                not _same_stat(before_leaf, identity)):
            _fail("input is not a stable regular single-link file: " + value)
        return BoundRegularFile(path, descriptor, identity)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    finally:
        os.close(parent)


@dataclass(frozen=True)
class RuntimeCapsule:
    """One unit-anchored capsule staged for an actual CP2-D process."""

    kind: str
    unit_archive_path: Path
    unit_profile_path: Path
    profile_bytes: bytes
    profile_sha256: str
    staged: capsule.StagedCapsule
    staged_root_identity: Tuple[int, ...]
    staged_entry_identities: Tuple[Tuple[Path, Tuple[int, ...]], ...]
    private_root: Path
    work_root: Path
    environment_pairs: Tuple[Tuple[str, str], ...]
    private_directory_identities: Tuple[Tuple[Path, Tuple[int, ...]], ...]

    @property
    def launcher(self) -> Path:
        return self.staged.root / self.profile.launcher_path

    @property
    def profile(self) -> capsule.CapsuleProfile:
        profile_record = schema.strict_json_loads(self.profile_bytes)
        try:
            return capsule.validate_capsule_profile(profile_record)
        except capsule.CapsuleError as exc:
            raise CampaignError("runtime capsule profile failed validation") from exc

    @property
    def environment(self) -> Dict[str, str]:
        return dict(self.environment_pairs)

    @property
    def evidence_environment(self) -> Dict[str, str]:
        """Return the exact logical profile variables bound by evidence."""

        return dict(self.profile.environment)

    @property
    def sandbox_environment(self) -> Dict[str, str]:
        """Resolve the frozen logical environment inside the private root."""

        result: Dict[str, str] = {}
        prefix = "${PRIVATE_ROOT}/"
        for name, value in self.profile.environment:
            if value.startswith(prefix):
                suffix = value[len(prefix) :]
                if not suffix or "/" in suffix or suffix in (".", ".."):
                    _fail("capsule sandbox environment suffix differs")
                result[name] = "/private/" + suffix
            else:
                result[name] = value
        if self.profile.execution.cwd != "${PRIVATE_ROOT}/work":
            _fail("capsule sandbox working-directory template differs")
        return result

    def revalidate(self) -> None:
        try:
            staged_status = os.stat(self.staged.root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(staged_status.st_mode)
                or stat.S_IMODE(staged_status.st_mode) != 0o700
                or _directory_object_identity(staged_status)
                != self.staged_root_identity
            ):
                _fail("runtime capsule staged-root identity differs")
            for path, expected in self.staged_entry_identities:
                observed = os.stat(path, follow_symlinks=False)
                if _stat_tuple(observed) != expected:
                    _fail("runtime capsule staged-member identity differs")
            profile_record = schema.strict_json_loads(self.profile_bytes)
            profile = capsule.validate_capsule_profile(profile_record)
            if (
                profile.capsule_kind != self.kind
                or profile.profile_sha256 != self.profile_sha256
                or profile.profile_sha256 != self.staged.profile_sha256
                or profile.profile_id != self.staged.profile_id
                or profile.archive_sha256 != self.staged.archive_sha256
                or profile.inventory_sha256 != self.staged.inventory_sha256
                or profile.environment_sha256 != self.staged.environment_sha256
                or hashlib.sha256(self.profile_bytes).hexdigest()
                != self.profile_sha256
            ):
                _fail("runtime capsule/profile identity differs")
            capsule.revalidate_staged_capsule(self.staged.root, self.staged.entries)
        except (capsule.CapsuleError, schema.SchemaError, OSError) as exc:
            raise CampaignError("runtime capsule failed complete revalidation") from exc
        for path, expected in self.private_directory_identities:
            try:
                observed = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise CampaignError("runtime capsule private directory is absent") from exc
            if (
                not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or stat.S_IMODE(observed.st_mode) != 0o700
                or _directory_object_identity(observed) != expected
            ):
                _fail("runtime capsule private directory identity differs")


def _capsule_private_environment(
    profile: capsule.CapsuleProfile,
    private_root: Path,
) -> Tuple[Tuple[Tuple[str, str], ...], Path, Tuple[Tuple[Path, Tuple[int, ...]], ...]]:
    private_root.mkdir(mode=0o700, exist_ok=False)
    environment: Dict[str, str] = {}
    directories = [private_root]
    prefix = "${PRIVATE_ROOT}/"
    for name, value in profile.environment:
        if value.startswith(prefix):
            suffix = value[len(prefix) :]
            if "/" in suffix or not suffix:
                _fail("capsule private environment suffix differs")
            target = private_root / suffix
            target.mkdir(mode=0o700, exist_ok=False)
            directories.append(target)
            environment[name] = str(target)
        else:
            environment[name] = value
    work_root = private_root / "work"
    work_root.mkdir(mode=0o700, exist_ok=False)
    directories.append(work_root)
    if profile.execution.cwd != "${PRIVATE_ROOT}/work":
        _fail("capsule execution working-directory template differs")
    identities = tuple(
        (path, _directory_object_identity(os.stat(path, follow_symlinks=False)))
        for path in directories
    )
    return tuple(sorted(environment.items())), work_root, identities


def _stage_unit_capsule(
    unit_artifact: Path,
    kind: str,
    stage_parent: Path,
    private_parent: Path,
) -> RuntimeCapsule:
    if kind not in UNIT_CAPSULE_PATHS:
        _fail("unit capsule kind differs")
    archive_relative, profile_relative = UNIT_CAPSULE_PATHS[kind]
    archive_path = Path(unit_artifact).absolute() / archive_relative
    profile_path = Path(unit_artifact).absolute() / profile_relative
    try:
        with _bind_regular_file(profile_path) as bound_profile, _bind_regular_file(
            archive_path
        ) as bound_archive:
            for label, bound in (
                ("profile", bound_profile),
                ("archive", bound_archive),
            ):
                if (
                    bound.identity.st_uid != os.geteuid()
                    or stat.S_IMODE(bound.identity.st_mode) != 0o444
                ):
                    _fail("unit capsule {} is not immutable and caller-owned".format(label))
            profile_bytes = _read_fd_all(
                bound_profile.descriptor,
                MAX_CAPSULE_PROFILE_BYTES,
                kind + " capsule profile",
            )
            profile_record = schema.strict_json_loads(profile_bytes)
            profile = capsule.validate_capsule_profile(profile_record)
            if profile.capsule_kind != kind:
                _fail("unit capsule profile kind differs")
            destination = stage_parent / kind
            staged = capsule.stage_capsule(archive_path, profile_record, destination)
            bound_profile.revalidate()
            bound_archive.revalidate()
    except (capsule.CapsuleError, schema.SchemaError) as exc:
        raise CampaignError("unit capsule staging failed closed: " + kind) from exc
    environment_pairs, work_root, directory_identities = _capsule_private_environment(
        profile,
        private_parent / kind,
    )
    runtime = RuntimeCapsule(
        kind=kind,
        unit_archive_path=archive_path,
        unit_profile_path=profile_path,
        profile_bytes=profile_bytes,
        profile_sha256=profile.profile_sha256,
        staged=staged,
        staged_root_identity=_directory_object_identity(
            os.stat(staged.root, follow_symlinks=False)
        ),
        staged_entry_identities=tuple(
            (
                staged.root.joinpath(*entry.path.split("/")),
                _stat_tuple(
                    os.stat(
                        staged.root.joinpath(*entry.path.split("/")),
                        follow_symlinks=False,
                    )
                ),
            )
            for entry in staged.entries
        ),
        private_root=private_parent / kind,
        work_root=work_root,
        environment_pairs=environment_pairs,
        private_directory_identities=directory_identities,
    )
    runtime.revalidate()
    return runtime


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: _UniqueKeyLoader, node: yaml.nodes.MappingNode,
                       deep: bool = False) -> MutableMapping[Any, Any]:
    result: MutableMapping[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            _fail("dataset registry contains a duplicate YAML key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _walk_mappings(value: Any, seen: Optional[set] = None
                   ) -> Iterable[Mapping[str, Any]]:
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        if all(isinstance(key, str) for key in value):
            yield value
        for child in value.values():
            yield from _walk_mappings(child, seen)
    elif isinstance(value, list):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value:
            yield from _walk_mappings(child, seen)


def _walk_scalars(value: Any, seen: Optional[set] = None) -> Iterable[Any]:
    if seen is None:
        seen = set()
    if isinstance(value, Mapping):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value.values():
            yield from _walk_scalars(child, seen)
    elif isinstance(value, list):
        if id(value) in seen:
            _fail("dataset registry aliases or cycles a YAML container")
        seen.add(id(value))
        for child in value:
            yield from _walk_scalars(child, seen)
    else:
        yield value


def _registry_bag_paths(registry_bytes: bytes) -> Tuple[Path, Path, Path]:
    if not isinstance(registry_bytes, bytes) or not registry_bytes:
        _fail("verified registry buffer is empty or not bytes")
    if len(registry_bytes) > MAX_REGISTRY_BYTES:
        _fail("verified registry exceeds the postauthorization bound")
    try:
        document = registry_bytes.decode("utf-8", "strict")
        registry = yaml.load(document, Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CampaignError("verified registry is not strict UTF-8 YAML") from exc
    if not isinstance(registry, Mapping):
        _fail("dataset registry root is not a mapping")

    resolved: List[Path] = []
    try:
        mappings = tuple(_walk_mappings(registry))
    except RecursionError as exc:
        raise CampaignError("dataset registry nesting exceeds the parser bound") from exc
    for sequence, expected_hash in zip(SEQUENCES, BAG_SHA256):
        scoped_candidates: List[Tuple[int, str]] = []
        for mapping in mappings:
            scalars = tuple(_walk_scalars(mapping))
            if sequence not in scalars or expected_hash not in scalars:
                continue
            for scalar in scalars:
                if (isinstance(scalar, str) and scalar.startswith("/") and
                        not scalar.startswith("//") and scalar != os.path.sep and
                        scalar.endswith(".bag") and not any(
                            token in scalar for token in ("\0", "\r", "\n", "\\")) and
                        os.path.normpath(scalar) == scalar):
                    scoped_candidates.append((len(scalars), scalar))
        if not scoped_candidates:
            _fail("registry has no hash-bound absolute bag for " + sequence)
        minimum_scope = min(item[0] for item in scoped_candidates)
        candidates = {path for size, path in scoped_candidates
                      if size == minimum_scope}
        if len(candidates) != 1:
            _fail("registry must resolve exactly one hash-bound absolute bag for " + sequence)
        resolved.append(Path(next(iter(candidates))))
    if len(set(map(str, resolved))) != len(resolved):
        _fail("registry maps distinct CP2 sequences to the same bag path")
    return tuple(resolved)  # type: ignore[return-value]


def _prepare_parent_bag_mounts(
    registry_bytes: bytes,
    mount_owner: OwnedTemporaryWorkspace,
) -> Tuple[Tuple[BoundRegularFile, ...], Tuple[_BagMountBinding, ...]]:
    """Hold and hash each frozen bag, then allocate exact empty mount targets."""

    mount_owner.revalidate()
    original_paths = _registry_bag_paths(registry_bytes)
    held: List[BoundRegularFile] = []
    bindings: List[_BagMountBinding] = []
    try:
        for index, (original, expected_sha256) in enumerate(
            zip(original_paths, BAG_SHA256)
        ):
            bound = _bind_regular_file(original)
            held.append(bound)
            if bound.sha256() != expected_sha256:
                _fail("registry-resolved bag hash differs from frozen CP2 identity")
            target = mount_owner.path / "{:02d}-recorded-input.bag".format(index)
            _write_new(target, b"", 0o600)
            target_status = target.lstat()
            if (
                not stat.S_ISREG(target_status.st_mode)
                or target_status.st_nlink != 1
                or target_status.st_uid != os.geteuid()
                or stat.S_IMODE(target_status.st_mode) != 0o600
            ):
                _fail("recorded-input bind target identity differs")
            bindings.append(_BagMountBinding(
                original_path=original,
                target_path=target,
                source_descriptor=bound.descriptor,
                source_identity=bound.identity,
                target_identity=target_status,
            ))
        os.fsync(mount_owner.descriptor)
        mount_owner.revalidate()
        return tuple(held), tuple(bindings)
    except BaseException:
        for bound in held:
            bound.close()
        raise


def encode_static_bundle(root: Path, authorization: Optional[Any] = None
                         ) -> Tuple[bytes, List[Dict[str, Any]]]:
    payload = bytearray(STATIC_BUNDLE_DOMAIN)
    payload.extend(len(STATIC_PATHS).to_bytes(8, "big"))
    records: List[Dict[str, Any]] = []
    for relative, expected in zip(STATIC_PATHS, STATIC_SHA256):
        path = root / relative
        if authorization is None:
            with _bind_regular_file(path) as bound:
                content = _read_fd_all(bound.descriptor, 64 * 1024 * 1024,
                                       "frozen static input")
                bound.revalidate()
        else:
            authorization.revalidate()
            descriptor = authorization.duplicate_source_fd(relative)
            try:
                content = _read_fd_all(descriptor, 64 * 1024 * 1024,
                                       "held frozen static input")
            finally:
                os.close(descriptor)
            authorization.revalidate()
        digest = hashlib.sha256(content).hexdigest()
        if digest != expected:
            _fail("frozen static input hash mismatch: " + relative)
        encoded = relative.encode("utf-8")
        payload.extend(len(encoded).to_bytes(8, "big"))
        payload.extend(encoded)
        payload.extend(len(content).to_bytes(8, "big"))
        payload.extend(bytes.fromhex(digest))
        records.append({"path": relative, "size": len(content), "sha256": digest})
    return bytes(payload), records


def _environment_record(environment_id: str,
                        variables: Mapping[str, str]) -> Dict[str, Any]:
    digest = schema.command_environment_sha256(variables)
    return {
        "environment_id": schema.validate_safe_id(environment_id),
        "variables": [
            {"name": name, "value": variables[name]}
            for name in sorted(variables, key=lambda item: item.encode("utf-8"))
        ],
        "canonical_sha256": digest,
    }


@dataclass(frozen=True)
class _OwnedProcessResult:
    exit_code: int
    timed_out: bool
    surviving_descendants: bool


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_reap_and_wait_for_group_absence(
        process: subprocess.Popen[bytes], label: str) -> bool:
    """Remove an owned session and reap its leader before returning.

    The return value reports whether the group was still present when cleanup
    began.  Callers use that only after a normally completed communicate to
    distinguish an ordinary exit from a leader that abandoned descendants.
    """

    process_group_id = process.pid
    group_was_present = _process_group_exists(process_group_id)
    if group_was_present:
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            pass

    deadline = time.monotonic() + MAX_PROCESS_GROUP_CLEANUP_SECONDS
    while process.returncode is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _fail(label + " leader could not be reaped")
        try:
            process.wait(timeout=min(PROCESS_GROUP_POLL_SECONDS, remaining))
        except subprocess.TimeoutExpired:
            continue
        except InterruptedError:
            continue

    while _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _fail(label + " process group did not disappear after SIGKILL")
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))
    return group_was_present


def _communicate_owned_process_group(
        process: subprocess.Popen[bytes], timeout: float,
        label: str) -> _OwnedProcessResult:
    """Communicate once and unconditionally close the owned process group."""

    timed_out = False
    completed_normally = False
    group_was_present = False
    try:
        try:
            process.communicate(timeout=timeout)
            completed_normally = True
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        # This finally is deliberately BaseException-wide: RuntimeError from
        # communicate, KeyboardInterrupt, and SystemExit all pass through only
        # after the leader is reaped and its complete process group is absent.
        group_was_present = _terminate_reap_and_wait_for_group_absence(
            process, label)
    if process.returncode is None:
        _fail(label + " has no exit status after process-group cleanup")
    return _OwnedProcessResult(
        exit_code=int(process.returncode),
        timed_out=timed_out,
        surviving_descendants=completed_normally and group_was_present,
    )


class CommandRecorder:
    def __init__(self, root: Path, authorization: Any) -> None:
        self.root = root
        self.authorization = authorization
        self.records: List[Dict[str, Any]] = []
        self.environments: Dict[str, Dict[str, Any]] = {}
        self._lifetime_guards: List[Callable[[], None]] = []

    def add_lifetime_guard(self, guard: Callable[[], None]) -> None:
        """Add one exact identity check around every later child command."""

        if not callable(guard):
            _fail("command lifetime guard is not callable")
        guard()
        self._lifetime_guards.append(guard)

    def revalidate_lifetimes(self) -> None:
        self.authorization.revalidate()
        for guard in self._lifetime_guards:
            guard()
        self.authorization.revalidate()

    def add_environment(self, environment_id: str,
                        variables: Mapping[str, str]) -> str:
        record = _environment_record(environment_id, variables)
        prior = self.environments.get(environment_id)
        if prior is not None and prior != record:
            _fail("environment ID was reused with different bytes")
        self.environments[environment_id] = record
        return record["canonical_sha256"]

    def import_readiness_unit_verification(self) -> None:
        """Import the already-run unit verifier as command zero.

        The readiness subprocess is executed while the source/data lock is
        held.  Its exact streams live in readiness attachments; importing the
        record avoids rerunning it and closes the common-command population.
        """

        if self.records:
            _fail("readiness unit verification must be command zero")
        source = self.authorization.unit_verification_command
        required = {
            "argv", "cwd", "environment", "started_utc", "finished_utc",
            "exit_code", "timed_out", "stdout", "stdout_sha256", "stderr",
            "stderr_sha256", "source_context_sha256", "process_group_complete",
        }
        if not isinstance(source, Mapping) or set(source) != required:
            _fail("readiness unit-verifier command shape differs")
        environment = source["environment"]
        if not isinstance(environment, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in environment.items()):
            _fail("readiness unit-verifier environment is invalid")
        environment_sha = self.add_environment("unit_verifier_v1", environment)
        if source["cwd"] != "/tmp" or source["process_group_complete"] is not True:
            _fail("readiness unit-verifier process-group/cwd evidence differs")
        for stream in ("stdout", "stderr"):
            relative = schema.validate_relpath(source[stream], "unit verifier stream")
            path = self.root / relative
            if (not path.is_file() or path.is_symlink() or
                    schema.sha256_file(path) != source[stream + "_sha256"]):
                _fail("unit-verifier retained stream identity differs")
        self.records.append({
            "schema_version": 1, "record_type": "command", "command_id": 0,
            "phase": "readiness", "sequence_index": None, "pair_index": None,
            "run_index": None, "argv": list(source["argv"]), "cwd": source["cwd"],
            "environment_sha256": environment_sha,
            "started_utc": source["started_utc"],
            "finished_utc": source["finished_utc"],
            "exit_code": source["exit_code"], "timed_out": source["timed_out"],
            "stdout": source["stdout"], "stdout_sha256": source["stdout_sha256"],
            "stderr": source["stderr"], "stderr_sha256": source["stderr_sha256"],
        })

    def run(self, phase: str, argv: Sequence[str], cwd: Path,
            environment_id: str, variables: Mapping[str, str],
            sequence_index: Optional[int] = None,
            pair_index: Optional[int] = None,
            run_index: Optional[int] = None,
            timeout: float = MAX_COMMAND_SECONDS,
            *,
            process_argv: Optional[Sequence[str]] = None,
            process_executable: Optional[str] = None,
            process_pass_fds: Sequence[int] = (),
            evidence_variables: Optional[Mapping[str, str]] = None,
            evidence_cwd: Optional[Path] = None) -> Dict[str, Any]:
        if not argv or any(not isinstance(item, str) or not item or "\0" in item
                           for item in argv):
            _fail("command argv is empty or invalid")
        if phase not in {
            "readiness", "source_archive", "configure", "build",
            "runtime_preflight", "bag_identity", "pair_index", "ros_run",
            "trajectory", "evaluation", "verification",
        }:
            _fail("command phase is outside the frozen enum")
        executed_argv = tuple(argv if process_argv is None else process_argv)
        if (
            not executed_argv
            or any(
                not isinstance(item, str) or not item or "\0" in item
                for item in executed_argv
            )
            or (
                process_executable is not None
                and (
                    not isinstance(process_executable, str)
                    or not process_executable.startswith("/proc/")
                    or "\0" in process_executable
                )
            )
            or any(type(item) is not int or item < 3 for item in process_pass_fds)
            or len(set(process_pass_fds)) != len(tuple(process_pass_fds))
        ):
            _fail("prepared command execution surface differs")
        self.revalidate_lifetimes()
        environment_sha = self.add_environment(
            environment_id,
            variables if evidence_variables is None else evidence_variables,
        )
        command_id = len(self.records)
        stdout_rel = "logs/{:03d}_{}.stdout".format(command_id, phase)
        stderr_rel = "logs/{:03d}_{}.stderr".format(command_id, phase)
        stdout_path = self.root / stdout_rel
        stderr_path = self.root / stderr_rel
        stdout_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        stdout_fd = -1
        stderr_fd = -1
        process: Optional[subprocess.Popen[bytes]] = None
        try:
            stdout_fd = os.open(
                str(stdout_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
                getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            stderr_fd = os.open(
                str(stderr_path),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
                getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            started = _utc_now()
            timed_out = False
            self.revalidate_lifetimes()
            process = subprocess.Popen(
                list(executed_argv),
                executable=process_executable,
                pass_fds=tuple(process_pass_fds),
                cwd=str(cwd), env=dict(variables), stdin=subprocess.DEVNULL,
                stdout=stdout_fd, stderr=stderr_fd, start_new_session=True,
                close_fds=True,
            )
            outcome = _communicate_owned_process_group(
                process, timeout, "command")
            exit_code = outcome.exit_code
            timed_out = outcome.timed_out
            if outcome.surviving_descendants:
                _fail("command left a surviving process group")
            self.revalidate_lifetimes()
            os.fsync(stdout_fd)
            os.fsync(stderr_fd)
        finally:
            try:
                if process is not None:
                    _terminate_reap_and_wait_for_group_absence(
                        process, "command")
            finally:
                try:
                    if stdout_fd >= 0:
                        os.close(stdout_fd)
                finally:
                    if stderr_fd >= 0:
                        os.close(stderr_fd)
        finished = _utc_now()
        record = {
            "schema_version": 1, "record_type": "command",
            "command_id": command_id, "phase": phase,
            "sequence_index": sequence_index, "pair_index": pair_index,
            "run_index": run_index, "argv": list(argv),
            "cwd": str(cwd if evidence_cwd is None else evidence_cwd),
            "environment_sha256": environment_sha,
            "started_utc": started, "finished_utc": finished,
            "exit_code": int(exit_code), "timed_out": timed_out,
            "stdout": stdout_rel, "stdout_sha256": schema.sha256_file(stdout_path),
            "stderr": stderr_rel, "stderr_sha256": schema.sha256_file(stderr_path),
        }
        self.records.append(record)
        return record


def _validate_archive(archive: Path, source_context: Mapping[str, Any],
                      destination: Path) -> None:
    if archive.stat().st_size > MAX_SOURCE_ARCHIVE_BYTES:
        _fail("source archive exceeds bound")
    entries = source_context.get("entries")
    if not isinstance(entries, list):
        _fail("prevalidated source context lacks entries")
    expected = {item["path"]: item for item in entries}
    if len(expected) != len(entries):
        _fail("prevalidated source context contains duplicate paths")
    with tarfile.open(str(archive), "r:") as stream:
        members = stream.getmembers()
        observed = {}
        for member in members:
            name = member.name[:-1] if member.name.endswith("/") else member.name
            if name in ("", ".") or member.isdir():
                continue
            normalized = schema.validate_relpath(name, "archive member")
            if not member.isfile() or member.issym() or member.islnk():
                _fail("source archive contains a nonregular leaf: " + normalized)
            if normalized in observed:
                _fail("source archive contains a duplicate member")
            extracted = stream.extractfile(member)
            if extracted is None:
                _fail("source archive member cannot be read")
            content = extracted.read()
            observed[normalized] = (member, content)
        if set(observed) != set(expected):
            _fail("source archive member inventory differs from held source")
        for name, (member, content) in observed.items():
            expected_item = expected[name]
            expected_mode = int(expected_item["mode"]) & 0o777
            if (len(content) != expected_item["size"] or
                    hashlib.sha256(content).hexdigest() != expected_item["sha256"] or
                    (member.mode & 0o777) != expected_mode):
                _fail("source archive member identity differs: " + name)
            output = destination / name
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_new(output, content, expected_mode)


def _validate_extracted_source_tree(
        destination: Path, source_context: Mapping[str, Any]) -> None:
    entries = source_context.get("entries")
    if not isinstance(entries, list):
        _fail("prevalidated source context lacks entries")
    expected = {item["path"]: item for item in entries}
    if len(expected) != len(entries):
        _fail("prevalidated source context contains duplicate paths")
    observed: Dict[str, Tuple[int, int, str]] = {}
    destination = Path(destination).resolve(strict=True)
    for root, directory_names, file_names in os.walk(
            str(destination), topdown=True, followlinks=False):
        root_path = Path(root)
        for name in directory_names:
            status_value = (root_path / name).lstat()
            if not stat.S_ISDIR(status_value.st_mode):
                _fail("extracted source contains a non-directory path component")
        for name in file_names:
            path = root_path / name
            status_value = path.lstat()
            if (not stat.S_ISREG(status_value.st_mode) or
                    status_value.st_nlink != 1):
                _fail("extracted source contains a nonregular or linked leaf")
            relative = path.relative_to(destination).as_posix()
            if relative in observed:
                _fail("extracted source inventory contains a duplicate path")
            with _bind_regular_file(path) as bound:
                digest = bound.sha256()
                bound.revalidate()
            observed[relative] = (
                stat.S_IMODE(status_value.st_mode), status_value.st_size, digest)
    if set(observed) != set(expected):
        _fail("extracted source inventory changed after archive validation")
    for relative, identity in observed.items():
        item = expected[relative]
        if identity != (
                int(item["mode"]) & 0o777, item["size"], item["sha256"]):
            _fail("extracted source identity changed: " + relative)


def _safe_extract_dependency(archive: Path, destination: Path) -> None:
    destination = Path(destination).resolve(strict=True)
    if not destination.is_dir() or destination.is_symlink():
        _fail("dependency destination is not a real directory")
    with _bind_regular_file(Path(archive).absolute()) as bound:
        if bound.identity.st_size > MAX_DEPENDENCY_ARCHIVE_BYTES:
            _fail("dependency archive exceeds its compressed-size bound")
        duplicate = os.dup(bound.descriptor)
        try:
            file_object = os.fdopen(duplicate, "rb")
        except BaseException:
            os.close(duplicate)
            raise
        with file_object, tarfile.open(fileobj=file_object, mode="r:") as stream:
            members = stream.getmembers()
            if len(members) > MAX_DEPENDENCY_MEMBERS:
                _fail("dependency archive exceeds its member-count bound")
            explicit = set()
            directories = set()
            files = set()
            total = 0
            for member in members:
                name = member.name[:-1] if member.name.endswith("/") else member.name
                if name in ("", "."):
                    continue
                normalized = schema.validate_relpath(
                    name, "dependency archive member")
                if normalized in explicit:
                    _fail("dependency archive contains a duplicate member")
                explicit.add(normalized)
                parents = tuple(
                    parent.as_posix() for parent in
                    PurePosixPath(normalized).parents if parent.as_posix() != ".")
                if any(parent in files for parent in parents):
                    _fail("dependency archive places a child below a file")
                output = destination / normalized
                if member.isdir():
                    if normalized in files:
                        _fail("dependency archive changes a file into a directory")
                    directories.add(normalized)
                    directories.update(parents)
                    output.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if (not member.isfile() or member.issym() or member.islnk() or
                        normalized in directories):
                    _fail("dependency archive contains a link, special file, or file/directory collision")
                if (member.size < 0 or member.size > MAX_DEPENDENCY_MEMBER_BYTES or
                        total > MAX_DEPENDENCY_TOTAL_BYTES - member.size):
                    _fail("dependency archive exceeds its extracted-size bound")
                total += member.size
                content_stream = stream.extractfile(member)
                if content_stream is None:
                    _fail("dependency archive member cannot be read")
                content = content_stream.read(member.size + 1)
                if len(content) != member.size:
                    _fail("dependency archive member size differs from its header")
                files.add(normalized)
                directories.update(parents)
                output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_new(output, content, member.mode & 0o777)
        bound.revalidate()


def _compile_command_path(value: Any, directory: Any, label: str,
                          must_exist: bool) -> Path:
    if (not isinstance(value, str) or not value or "\0" in value or
            not isinstance(directory, str) or not directory or
            "\0" in directory):
        _fail(label + " path is missing or invalid")
    base = Path(directory)
    if not base.is_absolute() or Path(os.path.normpath(directory)) != base:
        _fail("compile-command directory is not normalized absolute")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    normalized = Path(os.path.normpath(str(candidate)))
    if not normalized.is_absolute() or normalized != candidate:
        _fail(label + " path is not normalized absolute")
    try:
        return normalized.resolve(strict=must_exist)
    except OSError as exc:
        raise CampaignError(label + " path cannot be resolved") from exc


def _strict_fp_argv_passes(argv: Sequence[str], workspace: Path) -> bool:
    hidden = any(
        token.startswith("@") or
        any(marker in token for marker in
            (";", "&&", "||", "`", "$(", "\n", "\r")) or
        token in {"|", "<", ">", "2>", "2>&1"}
        for token in argv
    )
    forced_or_opaque = any(
        token in {"-specs", "--specs", "-include", "-imacros", "-Wp",
                  "-Xpreprocessor", "-wrapper"} or
        token.startswith(("-specs=", "--specs=", "-include=", "-imacros=",
                          "-Wp,", "-Xpreprocessor=", "-fplugin=")) or
        (token.startswith("-include") and token != "-include") or
        (token.startswith("-imacros") and token != "-imacros")
        for token in argv
    )
    unsafe_macro = any(
        token.startswith("-D__FAST_MATH__") or
        token.startswith("-D__FINITE_MATH_ONLY__")
        for token in argv
    )
    if (hidden or forced_or_opaque or unsafe_macro or
            any(token in STRICT_FORBIDDEN_FLAGS for token in argv)):
        return False

    effective_options: Dict[str, Optional[str]] = {}
    for name, options in {
            "fast_math": ("-ffast-math", "-fno-fast-math"),
            "signed_zeros": ("-fno-signed-zeros", "-fsigned-zeros"),
            }.items():
        events = [token for token in argv if token in options]
        effective_options[name] = events[-1] if events else None
    contract_events = [token for token in argv
                       if token.startswith("-ffp-contract=")]
    effective_options["fp_contract"] = (
        contract_events[-1] if contract_events else None)
    if effective_options != {
            "fast_math": "-fno-fast-math",
            "signed_zeros": "-fsigned-zeros",
            "fp_contract": "-ffp-contract=off",
            }:
        return False

    for macro, accepted in STRICT_REQUIRED_MACRO_DEFINITIONS.items():
        events: List[Tuple[str, str]] = []
        opaque = False
        for token in argv:
            if token in accepted:
                events.append(("accepted", token))
            elif token == "-U" + macro:
                events.append(("undefined", token))
            elif (token == "-D" + macro or
                  token.startswith("-D" + macro + "=") or
                  token.startswith("-D" + macro + "(")):
                events.append(("rejected", token))
            elif macro in token:
                opaque = True
        if opaque or not events or events[-1][0] != "accepted":
            return False

    # Production evidence must never be compiled with the test-only fault
    # injection interface, regardless of later undefinition.
    if any("OV_MSCKF_CP2_TESTING" in token for token in argv):
        return False

    expected_maps = {
        "-ffile-prefix-map={}=/cp2/reproducible-root".format(workspace),
        "-fdebug-prefix-map={}=/cp2/reproducible-root".format(workspace),
        "-fmacro-prefix-map={}=/cp2/reproducible-root".format(workspace),
    }
    map_prefixes = (
        "-ffile-prefix-map=", "-fdebug-prefix-map=", "-fmacro-prefix-map=")
    actual_maps = [token for token in argv if token.startswith(map_prefixes)]
    return set(actual_maps) == expected_maps and all(
        actual_maps.count(expected) >= 1 for expected in expected_maps)


def _audit_strict_fp_compile_commands(path: Path, source_space: Path,
                                      build_space: Path,
                                      workspace: Path) -> None:
    with _bind_regular_file(Path(path).absolute()) as bound:
        content = _read_fd_all(bound.descriptor, 256 * 1024 * 1024,
                               "compile_commands.json")
        bound.revalidate()
    try:
        document = schema.strict_json_loads(content)
    except (ValueError, TypeError) as exc:
        raise CampaignError("compile_commands.json is not strict JSON") from exc
    if not isinstance(document, list) or not document:
        _fail("compile_commands.json is empty or not an array")
    try:
        source_space = Path(source_space).resolve(strict=True)
        build_space = Path(build_space).resolve(strict=True)
        workspace = Path(workspace).resolve(strict=True)
        compiler = Path("/usr/bin/c++").resolve(strict=True)
    except OSError as exc:
        raise CampaignError("fresh-build audit roots cannot be resolved") from exc
    expected_sources = {
        (source_space / relative).resolve(strict=True): (relative, target)
        for relative, target in STRICT_FP_SOURCE_TARGETS.items()
    }
    relevant_basenames = {Path(relative).name
                          for relative in STRICT_FP_SOURCE_TARGETS}
    observed: Dict[str, List[Tuple[str, Path]]] = {
        relative: [] for relative in STRICT_FP_SOURCE_TARGETS}
    for record in document:
        if not isinstance(record, Mapping):
            _fail("compile command is not an object")
        source = record.get("file")
        if not isinstance(source, str):
            continue
        basename = Path(source).name
        if basename not in relevant_basenames:
            continue
        directory = record.get("directory")
        source_path = _compile_command_path(
            source, directory, "compile-command source", True)
        expected = expected_sources.get(source_path)
        if expected is None:
            _fail("strict-FP compile commands contain a basename spoof: " +
                  basename)
        relative, expected_target = expected
        has_arguments = "arguments" in record
        has_command = "command" in record
        if has_arguments == has_command:
            _fail("compile command must have exactly one argv representation")
        if isinstance(record.get("arguments"), list):
            argv = record["arguments"]
            if (not argv or not all(isinstance(item, str) for item in argv) or
                    any("\0" in item for item in argv)):
                _fail("compile command arguments are not strings")
        elif isinstance(record.get("command"), str):
            if "\0" in record["command"]:
                _fail("compile command contains NUL")
            try:
                argv = shlex.split(record["command"], posix=True)
            except ValueError as exc:
                raise CampaignError("compile command cannot be tokenized") from exc
            if not argv or any("\0" in item for item in argv):
                _fail("compile command token list is empty")
        else:
            _fail("compile command has neither arguments nor command")
        try:
            actual_compiler = Path(argv[0]).resolve(strict=True)
        except OSError as exc:
            raise CampaignError("compile command compiler cannot be resolved") from exc
        if actual_compiler != compiler:
            _fail("strict-FP command does not use the pinned compiler")
        compile_positions = [index for index, token in enumerate(argv)
                             if token == "-c"]
        output_positions = [index for index, token in enumerate(argv)
                            if token == "-o"]
        if (len(compile_positions) != 1 or
                compile_positions[0] + 1 >= len(argv) or
                len(output_positions) != 1 or
                output_positions[0] + 1 >= len(argv)):
            _fail("strict-FP command lacks one exact -c/-o binding")
        command_source = _compile_command_path(
            argv[compile_positions[0] + 1], directory,
            "actual compiler source", True)
        if command_source != source_path:
            _fail("compile-command file differs from its -c source")
        output = _compile_command_path(
            argv[output_positions[0] + 1], directory,
            "compiler output", False)
        expected_output = (build_space / "ov_msckf" / "CMakeFiles" /
                           (expected_target + ".dir") /
                           (relative[len("ov_msckf/"):] + ".o"))
        if output != expected_output:
            _fail("strict-FP object does not bind its exact source and target: " +
                  relative)
        declared_output = record.get("output")
        if declared_output is not None:
            if _compile_command_path(
                    declared_output, directory, "declared compiler output",
                    False) != output:
                _fail("declared compile output differs from -o output")
        if not _strict_fp_argv_passes(argv, workspace):
            _fail("strict-FP command is not effectively strict: " + relative)
        observed[relative].append((expected_target, output))
    invalid = [relative for relative, rows in observed.items()
               if len(rows) != 1 or rows[0][0] !=
               STRICT_FP_SOURCE_TARGETS[relative]]
    if invalid:
        _fail("strict-FP exact source/target population is incomplete: " +
              ",".join(sorted(invalid)))


def _bounded_slice(content: bytes, offset: int, size: int, label: str) -> bytes:
    if (isinstance(offset, bool) or isinstance(size, bool) or offset < 0 or
            size < 0 or offset > len(content) or size > len(content) - offset):
        _fail(label + " lies outside the ELF file")
    return content[offset:offset + size]


def _elf_identity(path: Path, require_soname: bool) -> Tuple[str, Optional[str]]:
    """Return the GNU build ID and optional DT_SONAME without subprocesses."""

    with _bind_regular_file(Path(path).absolute()) as bound:
        content = _read_fd_all(bound.descriptor, MAX_ELF_BYTES, "ELF image")
        bound.revalidate()
    if len(content) < 52 or content[:4] != b"\x7fELF":
        _fail("runtime image is not ELF: " + str(path))
    elf_class = content[4]
    byte_order = content[5]
    if elf_class not in (1, 2) or byte_order not in (1, 2):
        _fail("runtime ELF class/byte order is unsupported")
    prefix = "<" if byte_order == 1 else ">"
    header_format = prefix + ("HHIIIIIHHHHHH" if elf_class == 1 else
                              "HHIQQQIHHHHHH")
    header_size = struct.calcsize(header_format)
    if len(content) < 16 + header_size:
        _fail("runtime ELF header is truncated")
    header = struct.unpack_from(header_format, content, 16)
    section_offset = int(header[5])
    section_entry_size = int(header[10])
    section_count = int(header[11])
    section_format = prefix + ("IIIIIIIIII" if elf_class == 1 else
                               "IIQQQQIIQQ")
    expected_section_size = struct.calcsize(section_format)
    if (section_entry_size < expected_section_size or section_offset <= 0 or
            section_count > 65535):
        _fail("runtime ELF section table is invalid")

    def section(index: int) -> Tuple[int, ...]:
        if index < 0 or index >= section_count:
            _fail("runtime ELF section index is out of range")
        start = section_offset + index * section_entry_size
        raw = _bounded_slice(content, start, expected_section_size,
                             "runtime ELF section header")
        return tuple(int(value) for value in struct.unpack(section_format, raw))

    # ELF extended section counts store the real count in section zero's size.
    if section_count == 0:
        if section_offset <= 0:
            _fail("runtime ELF extended section table is absent")
        raw_zero = _bounded_slice(content, section_offset, expected_section_size,
                                  "runtime ELF section zero")
        zero = tuple(int(value) for value in struct.unpack(section_format, raw_zero))
        section_count = zero[5]
        if section_count <= 0 or section_count > 65535:
            _fail("runtime ELF extended section count is invalid")

    sections = [section(index) for index in range(section_count)]
    build_ids: List[str] = []
    sonames: List[str] = []
    for item in sections:
        section_type = item[1]
        if section_type == 7:  # SHT_NOTE
            section_data = _bounded_slice(content, item[4], item[5],
                                          "runtime ELF note section")
            position = 0
            while position < len(section_data):
                if len(section_data) - position < 12:
                    if any(section_data[position:]):
                        _fail("runtime ELF note tail is nonzero/truncated")
                    break
                name_size, description_size, note_type = struct.unpack_from(
                    prefix + "III", section_data, position)
                position += 12
                name = _bounded_slice(section_data, position, name_size,
                                      "runtime ELF note name")
                padded_name_size = (name_size + 3) & ~3
                _bounded_slice(section_data, position, padded_name_size,
                               "runtime ELF padded note name")
                position += padded_name_size
                description = _bounded_slice(section_data, position, description_size,
                                             "runtime ELF note description")
                padded_description_size = (description_size + 3) & ~3
                _bounded_slice(section_data, position, padded_description_size,
                               "runtime ELF padded note description")
                position += padded_description_size
                if note_type == 3 and name == b"GNU\0" and description:
                    build_ids.append(description.hex())
        elif section_type == 6:  # SHT_DYNAMIC
            section_data = _bounded_slice(content, item[4], item[5],
                                          "runtime ELF dynamic section")
            string_index = item[6]
            if string_index >= len(sections):
                _fail("runtime ELF dynamic string-table link is invalid")
            strings_section = sections[string_index]
            if strings_section[1] != 3:  # SHT_STRTAB
                _fail("runtime ELF dynamic string-table section has wrong type")
            strings = _bounded_slice(content, strings_section[4], strings_section[5],
                                     "runtime ELF dynamic strings")
            dynamic_format = prefix + ("iI" if elf_class == 1 else "qQ")
            dynamic_size = struct.calcsize(dynamic_format)
            entry_size = item[9] or dynamic_size
            if entry_size < dynamic_size or len(section_data) % entry_size != 0:
                _fail("runtime ELF dynamic table is malformed")
            for position in range(0, len(section_data), entry_size):
                tag, value = struct.unpack_from(dynamic_format, section_data, position)
                if tag == 0:
                    break
                if tag == 14:  # DT_SONAME
                    if value >= len(strings):
                        _fail("runtime ELF SONAME offset is out of range")
                    end = strings.find(b"\0", value)
                    if end < 0:
                        _fail("runtime ELF SONAME is not terminated")
                    try:
                        soname = strings[value:end].decode("utf-8", "strict")
                    except UnicodeDecodeError as exc:
                        raise CampaignError("runtime ELF SONAME is not UTF-8") from exc
                    if not soname or "\0" in soname:
                        _fail("runtime ELF SONAME is empty")
                    sonames.append(soname)
    if len(build_ids) != 1:
        _fail("runtime ELF must contain exactly one GNU build ID")
    if len(sonames) > 1 or (require_soname and len(sonames) != 1):
        _fail("runtime DSO must contain exactly one SONAME")
    return build_ids[0], sonames[0] if sonames else None


def _loader_dso_records(path: Path, executable: Path) -> List[Dict[str, Any]]:
    with _bind_regular_file(Path(path).absolute()) as bound:
        raw = _read_fd_all(bound.descriptor, 16 * 1024 * 1024,
                           "runtime loader map")
        bound.revalidate()
    return _loader_dso_records_bytes(raw, executable)


def _loader_dso_records_bytes(
    raw: bytes, executable: Path
) -> List[Dict[str, Any]]:
    """Parse loader-map bytes already consumed from an exact held inode."""

    if not isinstance(raw, bytes) or len(raw) > 16 * 1024 * 1024:
        _fail("runtime loader map bytes are invalid or exceed their bound")
    try:
        text_value = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise CampaignError("runtime loader map is not UTF-8") from exc
    executable = executable.resolve(strict=True)
    candidates = set()
    for line in text_value.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        raw_path = fields[5]
        if raw_path.endswith(" (deleted)"):
            _fail("runtime loader map contains a deleted image")
        if not raw_path.startswith("/") or ".so" not in Path(raw_path).name:
            continue
        candidate = Path(raw_path)
        if os.path.normpath(raw_path) != raw_path:
            _fail("runtime loader map contains a nonnormalized DSO path")
        resolved = candidate.resolve(strict=True)
        if resolved != executable:
            candidates.add(resolved)
    records: List[Dict[str, Any]] = []
    for candidate in sorted(candidates, key=lambda item: str(item).encode("utf-8")):
        with _bind_regular_file(candidate) as bound:
            before = bound.identity
            digest = bound.sha256()
            build_id, soname = _elf_identity(candidate, require_soname=True)
            bound.revalidate()
        records.append({
            "path": str(candidate), "soname": soname, "size": before.st_size,
            "sha256": digest, "build_id": build_id,
        })
    if not records:
        _fail("runtime loader map contains no DSOs")
    return records


def _build_runtime_in_workspace(
    repo_root: Path,
    partial: Path,
    unit_artifact: Path,
    authorization: Any,
    recorder: CommandRecorder,
    source_commit: str,
    workspace: Path,
) -> Dict[str, Any]:
    workspace = Path(workspace).absolute()
    capsule_stage_parent = workspace / "capsules"
    capsule_private_parent = workspace / "capsule-private"
    capsule_stage_parent.mkdir(mode=0o700)
    capsule_private_parent.mkdir(mode=0o700)
    direct_math_capsule = _stage_unit_capsule(
        unit_artifact,
        "direct_math",
        capsule_stage_parent,
        capsule_private_parent,
    )
    evaluator_capsule = _stage_unit_capsule(
        unit_artifact,
        "evaluator",
        capsule_stage_parent,
        capsule_private_parent,
    )
    direct_math_capsule.revalidate()
    evaluator_capsule.revalidate()
    source_space = workspace / "src"
    source_space.mkdir(mode=0o700)
    archive = workspace / "source_snapshot.tar"
    home = workspace / "home"
    temporary = workspace / "tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    build_env = {
        "CC": "/usr/bin/cc", "CMAKE_PREFIX_PATH": "/opt/ros/noetic",
        "CXX": "/usr/bin/c++", "HOME": str(home), "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "LD_LIBRARY_PATH": "/opt/ros/noetic/lib",
        "LOGNAME": "moksh", "OMP_NUM_THREADS": "1",
        "PATH": "/opt/ros/noetic/bin:/usr/bin:/bin",
        "PKG_CONFIG_PATH": "/opt/ros/noetic/lib/pkgconfig",
        "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
        "ROS_DISTRO": "noetic", "ROS_ETC_DIR": "/opt/ros/noetic/etc/ros",
        "ROS_MASTER_URI": "http://127.0.0.1:11311",
        "ROS_PACKAGE_PATH": "/opt/ros/noetic/share", "ROS_PYTHON_VERSION": "3",
        "ROS_ROOT": "/opt/ros/noetic/share/ros", "ROS_VERSION": "1",
        "SOURCE_DATE_EPOCH": "0", "TMPDIR": str(temporary), "USER": "moksh",
    }
    archive_command = recorder.run(
        "source_archive",
        ("/usr/bin/git", "-c", "tar.umask=0002", "-C", str(repo_root),
         "archive", "--format=tar", "--output=" + str(archive), source_commit),
        repo_root, "build_v1", build_env,
    )
    if archive_command["exit_code"] != 0 or archive_command["timed_out"]:
        _fail("fresh source archive command failed")
    source_context = authorization.repository.prevalidated_source_context()
    _validate_archive(archive, source_context, source_space)
    _validate_extracted_source_tree(source_space, source_context)
    _copy_new(archive, partial / "source_snapshot.tar")

    ceres_archive = unit_artifact / "ceres_source_snapshot.tar"
    if not ceres_archive.is_file() or ceres_archive.is_symlink():
        _fail("verified unit artifact lacks its Ceres source archive")
    ceres_source = workspace / "ceres-src"
    ceres_source.mkdir(mode=0o700)
    _safe_extract_dependency(ceres_archive, ceres_source)
    ceres_build = workspace / "ceres-build"
    ceres_prefix = workspace / "ceres-install"
    flags = "-fno-fast-math -ffp-contract=off -fsigned-zeros -ffile-prefix-map={}=/cp2/reproducible-root -fdebug-prefix-map={}=/cp2/reproducible-root -fmacro-prefix-map={}=/cp2/reproducible-root".format(
        workspace, workspace, workspace)
    configure = recorder.run(
        "configure",
        ("/usr/bin/cmake", "-S", str(ceres_source), "-B", str(ceres_build),
         "-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DCMAKE_INSTALL_PREFIX=" + str(ceres_prefix),
         "-DBUILD_TESTING=OFF", "-DBUILD_EXAMPLES=OFF", "-DBUILD_DOCUMENTATION=OFF",
         "-DMINIGLOG=ON", "-DGFLAGS=OFF", "-DLAPACK=OFF", "-DSUITESPARSE=OFF",
         "-DCXSPARSE=OFF", "-DCUSTOM_BLAS=ON", "-DCMAKE_C_FLAGS=" + flags,
         "-DCMAKE_CXX_FLAGS=" + flags, "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--build-id=sha1",
         "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,--build-id=sha1"),
        workspace, "build_v1", build_env,
    )
    if configure["exit_code"] != 0 or configure["timed_out"]:
        _fail("fresh Ceres configure failed")
    for argv in (
        ("/usr/bin/cmake", "--build", str(ceres_build), "--parallel", "1"),
        ("/usr/bin/cmake", "--install", str(ceres_build)),
    ):
        result = recorder.run("build", argv, workspace, "build_v1", build_env)
        if result["exit_code"] != 0 or result["timed_out"]:
            _fail("fresh Ceres build/install failed")

    catkin_build = workspace / "build"
    catkin_devel = workspace / "devel"
    catkin_logs = workspace / "catkin-logs"
    catkin_config = recorder.run(
        "configure",
        ("/usr/bin/catkin", "config", "--workspace", str(workspace),
         "--source-space", str(source_space), "--build-space", str(catkin_build),
         "--devel-space", str(catkin_devel), "--log-space", str(catkin_logs),
         "--extend", "/opt/ros/noetic", "--merge-devel", "--cmake-args",
         "-DCMAKE_BUILD_TYPE=RelWithDebInfo", "-DCMAKE_VERBOSE_MAKEFILE=ON",
         "-DCATKIN_ENABLE_TESTING=OFF", "-DBUILD_TESTING=OFF",
         "-DDISABLE_MATPLOTLIB=ON", "-DPYTHON_EXECUTABLE=/usr/bin/python3",
         "-DPython_EXECUTABLE=/usr/bin/python3", "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
         "-DCMAKE_C_FLAGS=" + flags, "-DCMAKE_CXX_FLAGS=" + flags,
         "-DCMAKE_EXE_LINKER_FLAGS=-Wl,--build-id=sha1",
         "-DCMAKE_SHARED_LINKER_FLAGS=-Wl,--build-id=sha1",
         "-DCeres_DIR=" + str(ceres_prefix / "lib/cmake/Ceres")),
        workspace, "build_v1", build_env,
    )
    if catkin_config["exit_code"] != 0 or catkin_config["timed_out"]:
        _fail("fresh Catkin configure failed")
    catkin = recorder.run(
        "build", ("/usr/bin/catkin", "build", "--workspace", str(workspace),
                  "ov_msckf", "--jobs", "1", "--no-status", "--summarize"),
        workspace, "build_v1", build_env,
    )
    if catkin["exit_code"] != 0 or catkin["timed_out"]:
        _fail("fresh runtime build failed")
    _validate_extracted_source_tree(source_space, source_context)
    executable = (catkin_devel / "lib/ov_msckf/ros1_serial_msckf").resolve(strict=True)
    assembler = (catkin_devel / "lib/ov_msckf/cp2_recorded_assemble").resolve(strict=True)
    compile_commands = catkin_build / "ov_msckf/compile_commands.json"
    cmake_cache = catkin_build / "ov_msckf/CMakeCache.txt"
    for required in (executable, assembler, compile_commands, cmake_cache):
        if (not required.is_file() or required.is_symlink() or
                required.stat().st_nlink != 1):
            _fail("fresh build output is missing: " + str(required))
    if (not executable.stat().st_mode & stat.S_IXUSR or
            not assembler.stat().st_mode & stat.S_IXUSR):
        _fail("fresh runtime/assembler output is not executable")
    _audit_strict_fp_compile_commands(
        compile_commands, source_space, catkin_build, workspace)
    _copy_new(compile_commands, partial / "compile_commands.json")
    _copy_new(cmake_cache, partial / "CMakeCache.txt")
    direct_math_capsule.revalidate()
    evaluator_capsule.revalidate()
    return {
        "workspace": workspace, "source_space": source_space,
        "executable": executable, "assembler": assembler,
        "compile_commands": partial / "compile_commands.json",
        "cmake_cache": partial / "CMakeCache.txt", "build_env": build_env,
        "source_archive": partial / "source_snapshot.tar",
        "direct_math_capsule": direct_math_capsule,
        "evaluator_capsule": evaluator_capsule,
        "direct_math_launcher": direct_math_capsule.launcher,
        "evaluator_launcher": evaluator_capsule.launcher,
        "direct_math_environment": direct_math_capsule.environment,
        "evaluator_environment": evaluator_capsule.environment,
    }


def _build_runtime(
    repo_root: Path,
    partial: Path,
    unit_artifact: Path,
    authorization: Any,
    recorder: CommandRecorder,
    source_commit: str,
    workspace_owner: Optional[OwnedTemporaryWorkspace] = None,
) -> Dict[str, Any]:
    """Build in one descriptor-owned workspace and transfer cleanup ownership."""

    owner = workspace_owner
    created_here = owner is None
    if owner is None:
        owner = OwnedTemporaryWorkspace.create()
    try:
        owner.revalidate()
        recorder.add_lifetime_guard(owner.revalidate)
        result = _build_runtime_in_workspace(
            repo_root,
            partial,
            unit_artifact,
            authorization,
            recorder,
            source_commit,
            owner.path,
        )
        owner.revalidate()
        if (
            not isinstance(result, dict)
            or Path(result.get("workspace", "")).absolute() != owner.path
        ):
            _fail("runtime build returned a different private workspace")
        owner.revalidate()
        result["workspace_owner"] = owner
        return result
    except BaseException as original_error:
        if created_here:
            try:
                owner.remove()
            except BaseException as cleanup_error:
                raise CampaignError(
                    "runtime build failed and private-workspace cleanup failed: "
                    + str(cleanup_error)
                ) from original_error
        raise


def _flatten_dump(raw: bytes) -> Dict[str, Any]:
    try:
        parsed = yaml.load(raw.decode("utf-8", "strict"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CampaignError("roslaunch parameter dump is invalid") from exc
    if not isinstance(parsed, Mapping) or not parsed:
        _fail("roslaunch parameter dump is empty or not a mapping")
    result = dict(parsed)
    if any(not isinstance(key, str) or not key.startswith("/cp2_vio/")
           for key in result):
        _fail("roslaunch parameter dump contains a non-/cp2_vio leaf")
    schema.encode_resolved_parameters(result)
    return result


def _runtime_environment(build: Mapping[str, Any], partial: Path,
                         port: int) -> Dict[str, str]:
    workspace = Path(build["workspace"])
    partial.mkdir(mode=0o700, parents=True, exist_ok=False)
    home = partial / "runtime-home"
    temporary = partial / "runtime-tmp"
    home.mkdir(mode=0o700, exist_ok=False)
    temporary.mkdir(mode=0o700, exist_ok=False)
    return {
        "CMAKE_PREFIX_PATH": str(workspace / "devel") + ":/opt/ros/noetic",
        "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "LD_LIBRARY_PATH": str(workspace / "devel/lib") + ":" +
                           str(workspace / "ceres-install/lib") + ":/opt/ros/noetic/lib",
        "LOGNAME": "moksh", "OMP_NUM_THREADS": "1",
        "PATH": str(workspace / "devel/bin") + ":/opt/ros/noetic/bin:/usr/bin:/bin",
        "PYTHONPATH": str(workspace / "devel/lib/python3/dist-packages") +
                      ":/opt/ros/noetic/lib/python3/dist-packages",
        "ROS_DISTRO": "noetic", "ROS_ETC_DIR": "/opt/ros/noetic/etc/ros",
        "ROS_HOSTNAME": "127.0.0.1", "ROS_IP": "127.0.0.1",
        "ROS_MASTER_URI": "http://127.0.0.1:{}".format(port),
        "ROS_PACKAGE_PATH": str(build["source_space"]) + ":/opt/ros/noetic/share",
        "ROS_PYTHON_VERSION": "3", "ROS_ROOT": "/opt/ros/noetic/share/ros",
        "ROS_VERSION": "1", "TMPDIR": str(temporary), "USER": "moksh",
    }


def _context(run_id: str, sequence_index: int, source_commit: str,
             config_sha256: str, bag_sha256: str,
             resolved_sha256: str, trace: Path) -> Dict[str, Any]:
    path = lambda name: str(trace / name)
    return {
        "schema_version": 1, "record_type": "cp2_runtime_context",
        "checkpoint": "CP2-C", "run_id": run_id,
        "sequence_index": sequence_index, "sequence_id": SEQUENCES[sequence_index],
        "mode": "nullspace", "shadow_enabled": True,
        "trace_level": "recorded_full", "source_commit": source_commit,
        "config_sha256": config_sha256, "bag_sha256": bag_sha256,
        "pair_index_sha256": None,
        "resolved_parameters_sha256": resolved_sha256,
        "trace_directory": str(trace), "serial_trace_path": path("serial.jsonl"),
        "callback_trace_path": None, "trajectory_trace_path": None,
        "updater_trace_path": path("updater.journal"),
        "state_payload_path": path("state.staging.bin"),
        "proposal_payload_path": path("proposal.staging.bin"),
        "raw_system_payload_path": path("raw.staging.bin"),
        "timing_trace_path": None,
        "runtime_parameters_path": path("runtime_parameters.yaml"),
        "loader_map_before_path": path("loader_before.txt"),
        "loader_map_after_path": path("loader_after.txt"),
        "legacy_state_path": None, "legacy_deviation_path": None,
        "legacy_timing_path": None,
    }


def _launch_arguments(
    build: Mapping[str, Any],
    bag: Path,
    offset: float,
    trace: Path,
    context_path: Path,
    sequence_index: int,
    sink_capabilities: Mapping[str, str],
) -> List[str]:
    if set(sink_capabilities) != set(SINK_CAPABILITY_FIELDS):
        _fail("CP2-C sink capability field population differs")
    arguments = [
        str(Path(build["source_space"]) / "project/cp2_serial.launch"),
        "bag:=" + str(bag), "bag_start:=" + format(offset, ".1f"),
        "config_path:=" + str(Path(build["source_space"]) /
                              "config/euroc_mav/estimator_config.yaml"),
        "path_state:=" + str(trace / "forbidden_legacy_state.txt"),
        "path_std:=" + str(trace / "forbidden_legacy_deviation.txt"),
        "path_time:=" + str(trace / "forbidden_legacy_timing.csv"),
        "save_total_state:=false", "record_openvins_timing:=false",
        "cp2_trace_directory:=" + str(trace),
        "cp2_context_path:=" + str(context_path),
        "cp2_sequence_id:=" + SEQUENCES[sequence_index],
        "cp2_sequence_index:=" + str(sequence_index),
        "landmark_elimination:=nullspace", "cp2_shadow_enabled:=true",
        "cp2_trace_level:=recorded_full", "verbosity:=INFO",
    ]
    arguments.extend(
        "cp2_{}_sink_capability:={}".format(
            field, sink_capabilities[field]
        )
        for field in SINK_CAPABILITY_FIELDS
    )
    return arguments


def _copy_readiness(partial: Path, authorization: Any) -> None:
    for relative, source in authorization.attachments.items():
        normalized = schema.validate_relpath(relative, "readiness attachment")
        _write_new(partial / normalized, Path(source).read_bytes())
    _write_new(partial / "readiness/barrier.json", _json_bytes(authorization.record))


def _pair_u64(value: Any, label: str) -> int:
    try:
        return schema.validate_u64(value, label)
    except schema.SchemaError as exc:
        raise CampaignError(str(exc)) from exc


def _validate_pair_index_bytes(
    payload: bytes, sequence_index: int, sequence_id: str
) -> Tuple[Mapping[str, Any], ...]:
    """Validate the independent selector output without importing CP2-D."""

    if not isinstance(payload, bytes):
        _fail("pair-index payload is not bytes")
    try:
        parsed = schema.strict_jsonl_loads(payload)
        if schema.jsonl_bytes(parsed) != payload:
            _fail("pair index is not canonical JSONL")
    except schema.SchemaError as exc:
        raise CampaignError("pair index is not strict JSONL") from exc
    rows = tuple(parsed)
    if len(rows) < 2:
        _fail("pair index must contain at least two selected rows")
    if (
        isinstance(sequence_index, bool)
        or not isinstance(sequence_index, int)
        or not 0 <= sequence_index < len(SEQUENCES)
        or SEQUENCES[sequence_index] != sequence_id
    ):
        _fail("pair-index requested sequence identity differs")
    used_camera_indices = set()
    previous_anchor = -1
    selected_times: List[int] = []
    for expected_index, row in enumerate(rows):
        if not isinstance(row, Mapping) or set(row) != set(PAIR_INDEX_KEYS):
            _fail("pair-index row has the wrong exact key set")
        if (
            type(row.get("schema_version")) is not int
            or row.get("schema_version") != 1
            or row.get("record_type") != "pair_index"
            or _pair_u64(row.get("sequence_index"), "pair-index sequence")
            != sequence_index
            or row.get("sequence_id") != sequence_id
        ):
            _fail("pair-index row identity differs from the requested sequence")
        if _pair_u64(row["pair_index"], "pair index") != expected_index:
            _fail("pair indices are not contiguous from zero")
        anchor = _pair_u64(
            row["anchor_filtered_index"], "anchor filtered index"
        )
        camera_id = _pair_u64(row["anchor_camera_id"], "anchor camera ID")
        cam0 = _pair_u64(row["cam0_filtered_index"], "cam0 filtered index")
        cam1 = _pair_u64(row["cam1_filtered_index"], "cam1 filtered index")
        if camera_id not in (0, 1) or anchor != (
            cam0 if camera_id == 0 else cam1
        ):
            _fail("pair anchor camera/index relation is invalid")
        if anchor <= previous_anchor or cam0 == cam1:
            _fail("pair selection order or camera identity is invalid")
        previous_anchor = anchor
        if cam0 in used_camera_indices or cam1 in used_camera_indices:
            _fail("a camera message is reused by the pair population")
        used_camera_indices.update((cam0, cam1))
        cam0_record = _pair_u64(row["cam0_record_time_ns"], "cam0 record time")
        cam1_record = _pair_u64(row["cam1_record_time_ns"], "cam1 record time")
        _pair_u64(row["cam0_header_time_ns"], "cam0 header time")
        _pair_u64(row["cam1_header_time_ns"], "cam1 header time")
        delta = _pair_u64(
            row["absolute_record_delta_ns"], "absolute record delta"
        )
        if (
            delta != abs(cam0_record - cam1_record)
            or delta >= STRICT_PAIR_DELTA_NS
        ):
            _fail("pair record-time delta is not exact or outside the strict bound")
        candidate_index = cam1 if camera_id == 0 else cam0
        anchor_time = cam0_record if camera_id == 0 else cam1_record
        candidate_time = cam1_record if camera_id == 0 else cam0_record
        if candidate_index <= anchor or candidate_time < anchor_time:
            _fail("pair candidate is not forward of its anchor")
        selected_times.append(cam0_record)
    if any(
        selected_times[index] > selected_times[index + 1]
        for index in range(len(selected_times) - 1)
    ):
        _fail("selected cam0 record times reverse")
    if selected_times[-1] <= selected_times[0]:
        _fail("selected pair population has no positive duration")
    return rows


def _project_serial_pairs_to_pair_index_bytes(
    serial_pair_bytes: bytes, sequence_index: int, sequence_id: str
) -> bytes:
    """Project CP2-C serial rows onto the exact source-key population."""

    index = _pair_u64(sequence_index, "serial projection sequence index")
    if index >= len(SEQUENCES) or SEQUENCES[index] != sequence_id:
        _fail("serial projection sequence identity differs from the frozen inventory")
    if not isinstance(serial_pair_bytes, bytes):
        _fail("serial projection input is not bytes")
    try:
        decoded = schema.strict_jsonl_loads(serial_pair_bytes)
        if schema.jsonl_bytes(decoded) != serial_pair_bytes:
            _fail("serial projection input is not canonical JSONL")
    except schema.SchemaError as exc:
        raise CampaignError("serial projection input is not strict JSONL") from exc
    projected: List[Mapping[str, Any]] = []
    previous: Optional[Tuple[int, int]] = None
    for row in decoded:
        if not isinstance(row, Mapping) or set(row) != set(SERIAL_PAIR_KEYS):
            _fail("serial projection row has the wrong exact key set")
        if (
            type(row["schema_version"]) is not int
            or row["schema_version"] != 1
            or row["record_type"] != "serial_pair"
        ):
            _fail("serial projection row has the wrong schema identity")
        row_sequence = _pair_u64(
            row["sequence_index"], "serial projection row sequence"
        )
        pair_index = _pair_u64(row["pair_index"], "serial projection row pair")
        if (
            row_sequence >= len(SEQUENCES)
            or row["sequence_id"] != SEQUENCES[row_sequence]
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
            _pair_u64(row[field], "serial projection " + field)
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
            _pair_u64(value, "serial projection invocation ID")
            for value in invocation_ids
        ]
        if len(normalized_invocations) != len(set(normalized_invocations)):
            _fail("serial projection invocation IDs are invalid")
        if row_sequence == index:
            source = {key: row[key] for key in PAIR_INDEX_KEYS}
            source["record_type"] = "pair_index"
            projected.append(source)
    try:
        payload = schema.jsonl_bytes(projected)
    except schema.SchemaError as exc:
        raise CampaignError("cannot encode projected pair index") from exc
    _validate_pair_index_bytes(payload, index, sequence_id)
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
    recorder: CommandRecorder,
    authorization: Any,
    bound_bag: BoundRegularFile,
    sequence_index: int,
) -> bytes:
    """Retain the independent bag selector output from fresh source."""

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
        SEQUENCES[sequence_index],
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
        if schema.jsonl_bytes(schema.strict_jsonl_loads(stdout)) != stdout:
            _fail("pair-index subprocess stdout is not canonical JSONL")
    except schema.SchemaError as exc:
        raise CampaignError("pair-index subprocess stdout is invalid") from exc
    _validate_pair_index_bytes(stdout, sequence_index, SEQUENCES[sequence_index])
    authorization.revalidate()
    bound_bag.revalidate()
    return stdout


def _run_sequences(repo_root: Path, partial: Path, build: Mapping[str, Any],
                   recorder: CommandRecorder, authorization: Any,
                   bags: Sequence[BoundRegularFile], source_commit: str,
                   config_sha: str) -> Tuple[List[Dict[str, Any]], str]:
    serial_parts: List[bytes] = []
    run_records: List[Dict[str, Any]] = []
    for sequence_index, (bound, expected_hash, offset) in enumerate(
            zip(bags, BAG_SHA256, OFFSETS)):
        authorization.revalidate()
        if bound.sha256() != expected_hash:
            _fail("bag SHA-256 differs before run: " + SEQUENCES[sequence_index])
        independent_pair_index = _run_pair_index_command(
            repo_root=repo_root, partial=partial, build=build,
            recorder=recorder, authorization=authorization,
            bound_bag=bound, sequence_index=sequence_index)
        trace = partial / "runtime/{:02d}_{}".format(sequence_index, SEQUENCES[sequence_index])
        trace.mkdir(mode=0o700, parents=True, exist_ok=False)
        held_outputs: Optional[_HeldRuntimeOutputs] = None
        try:
            held_outputs = _HeldRuntimeOutputs(trace)
            context_path = trace / "context.json"
            prelaunch_path = trace / "prelaunch_parameters.yaml"
            canonical_path = trace / "parameters_canonical.bin"
            required = tuple(
                name for name in C_SINK_NAMES.values() if name is not None
            )
            held_outputs.precreate(
                required
                + (
                    context_path.name,
                    prelaunch_path.name,
                    canonical_path.name,
                )
            )
            sink_capabilities = {
                field: (
                    "null"
                    if C_SINK_NAMES[field] is None
                    else held_outputs.capability(C_SINK_NAMES[field])
                )
                for field in SINK_CAPABILITY_FIELDS
            }
            launch_args = _launch_arguments(
                build,
                bound.path,
                offset,
                trace,
                context_path,
                sequence_index,
                sink_capabilities,
            )
            port = 14381 + sequence_index
            environment = _runtime_environment(
                build,
                Path(build["workspace"])
                / "runtime-environment-{:02d}".format(sequence_index),
                port,
            )
            dump_argv = [
                "/opt/ros/noetic/bin/roslaunch",
                "--dump-params",
            ] + launch_args
            dump = recorder.run(
                "runtime_preflight",
                dump_argv,
                repo_root,
                "ros_run_{:02d}".format(sequence_index),
                environment,
                sequence_index=sequence_index,
                run_index=sequence_index,
            )
            if dump["exit_code"] != 0 or dump["timed_out"]:
                _fail("roslaunch preflight failed")
            raw_prelaunch = (partial / dump["stdout"]).read_bytes()
            parameters = _flatten_dump(raw_prelaunch)
            canonical = schema.encode_resolved_parameters(parameters)
            resolved_sha = hashlib.sha256(canonical).hexdigest()
            held_outputs.fill_parent(prelaunch_path.name, raw_prelaunch)
            held_outputs.fill_parent(canonical_path.name, canonical)
            run_id = "cp2c-g{}-s{}-{}".format(
                source_commit[:12],
                sequence_index,
                hashlib.sha256(str(partial).encode("utf-8")).hexdigest()[:12],
            )
            context = _context(
                run_id,
                sequence_index,
                source_commit,
                config_sha,
                expected_hash,
                resolved_sha,
                trace,
            )
            held_outputs.fill_parent(context_path.name, _json_bytes(context))
            held_outputs.revalidate()
            launch_argv = [
                "/opt/ros/noetic/bin/roslaunch",
                "-p",
                str(port),
            ] + launch_args
            run = recorder.run(
                "ros_run",
                launch_argv,
                repo_root,
                "ros_run_{:02d}".format(sequence_index),
                environment,
                sequence_index=sequence_index,
                run_index=sequence_index,
            )
            if run["exit_code"] != 0 or run["timed_out"]:
                _fail("recorded ROS run failed: " + SEQUENCES[sequence_index])
            _fsync_directory(trace)
            held_outputs.accept_external(required)
            recorder.add_lifetime_guard(held_outputs.revalidate)
            bound.revalidate()
            if bound.sha256() != expected_hash:
                _fail(
                    "bag SHA-256 differs after run: "
                    + SEQUENCES[sequence_index]
                )
            runtime_raw = held_outputs.payload("runtime_parameters.yaml")
            runtime_map = schema.strict_json_loads(runtime_raw)
            if not isinstance(runtime_map, Mapping):
                _fail("runtime parameter capture is not a map")
            if schema.encode_resolved_parameters(runtime_map) != canonical:
                _fail(
                    "runtime parameter bytes differ from prelaunch canonical map"
                )
            loader_before_bytes = held_outputs.payload("loader_before.txt")
            loader_after_bytes = held_outputs.payload("loader_after.txt")
            dso_before = _loader_dso_records_bytes(
                loader_before_bytes, Path(build["executable"])
            )
            dso_after = _loader_dso_records_bytes(
                loader_after_bytes, Path(build["executable"])
            )
            if dso_before != dso_after:
                _fail("runtime DSO identity set changed during sequence run")
            serial_bytes = held_outputs.payload("serial.jsonl")
            projected_pair_index = _project_serial_pairs_to_pair_index_bytes(
                serial_bytes, sequence_index, SEQUENCES[sequence_index]
            )
            if projected_pair_index != independent_pair_index:
                _fail(
                    "runtime serial selection differs from independent pair-index replay: "
                    + SEQUENCES[sequence_index]
                )
            serial_parts.append(serial_bytes)
            run_records.append({
                "run_id": run_id,
                "sequence_index": sequence_index,
                "sequence_id": SEQUENCES[sequence_index],
                "mode": "nullspace",
                "trace": trace,
                "context": context_path,
                "journal": trace / "updater.journal",
                "resolved_sha256": resolved_sha,
                "prelaunch_raw": prelaunch_path,
                "runtime_raw": trace / "runtime_parameters.yaml",
                "canonical": canonical_path,
                "loader_before": trace / "loader_before.txt",
                "loader_after": trace / "loader_after.txt",
                "dso_records_before": dso_before,
                "dso_records_after": dso_after,
                "bag_size": bound.identity.st_size,
                "held_outputs": held_outputs,
            })
            held_outputs = None
        finally:
            if held_outputs is not None:
                held_outputs.close()
    serial = b"".join(serial_parts)
    _write_new(partial / "serial_pairs.jsonl", serial)
    return run_records, hashlib.sha256(serial).hexdigest()


def _invoke_assembler(partial: Path, build: Mapping[str, Any],
                      runs: Sequence[Mapping[str, Any]], serial_sha: str,
                      recorder: CommandRecorder, authorization: Any,
                      config_sha: str) -> Dict[str, Any]:
    assembler = Path(build["assembler"])
    if not assembler.is_file() or assembler.is_symlink():
        _fail("fresh build lacks the required cp2_recorded_assemble tool")
    output = partial / "assembled"
    output.mkdir(mode=0o700, exist_ok=False)
    spec = {
        "schema_version": 1, "record_type": "cp2_recorded_assembly_spec",
        "config_sha256": config_sha,
        "serial_pairs": str(partial / "serial_pairs.jsonl"),
        "serial_pairs_sha256": serial_sha,
        "runs": [
            {"sequence_index": row["sequence_index"],
             "sequence_id": row["sequence_id"],
             "bag_sha256": BAG_SHA256[row["sequence_index"]],
             "resolved_parameters_sha256": row["resolved_sha256"],
             "journal": str(row["journal"])}
            for row in runs
        ],
    }
    spec_path = partial / "assembly_spec.json"
    _write_new(spec_path, _json_bytes(spec))
    environment = {
        "HOME": str(Path(build["workspace"]) / "assembler-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8", "LD_LIBRARY_PATH":
            str(Path(build["workspace"]) / "devel/lib") + ":" +
            str(Path(build["workspace"]) / "ceres-install/lib") +
            ":/opt/ros/noetic/lib",
        "LOGNAME": "moksh", "OMP_NUM_THREADS": "1",
        "PATH": "/usr/bin:/bin", "USER": "moksh",
    }
    Path(environment["HOME"]).mkdir(mode=0o700, exist_ok=False)
    result = recorder.run(
        "trajectory", (str(assembler), "--spec", str(spec_path),
                       "--output-dir", str(output)), partial,
        "assembler_v1", environment,
    )
    if result["exit_code"] != 0 or result["timed_out"]:
        _fail("recorded mathematical artifact assembler failed")
    retained = (
        "updates.jsonl", "features.jsonl", "state_blocks.jsonl",
        "covariance_blocks.jsonl", "state_snapshot_payloads.bin",
        "proposal_payloads.bin", "raw_system_payloads.bin",
    )
    for name in retained + ("summary.json",):
        source = output / name
        if not source.is_file() or source.is_symlink() or source.stat().st_nlink != 1:
            _fail("assembler omitted required output: " + name)
    extras = {path.name for path in output.iterdir()} - set(retained) - {"summary.json"}
    if extras:
        _fail("assembler emitted undeclared output files")
    summary = schema.strict_json_loads((output / "summary.json").read_bytes())
    if not isinstance(summary, dict):
        _fail("assembler summary is not a JSON object")
    for name in retained:
        source = output / name
        destination = partial / name
        os.rename(str(source), str(destination))
    (output / "summary.json").unlink()
    output.rmdir()
    spec_path.unlink()
    authorization.revalidate()
    return summary


def _invoke_offline_replay(
        partial: Path, build: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
        recorder: CommandRecorder, authorization: Any, source_commit: str,
        source_tree: str, executable_sha256: str, executable_build_id: str,
        ) -> Dict[str, Any]:
    transient = partial / "replay_input.json"
    replay_output = partial / "replay_report.json"
    resolved = []
    for row in runs:
        canonical = Path(row["canonical"])
        relative = canonical.relative_to(partial).as_posix()
        resolved.append({
            "sequence_index": row["sequence_index"],
            "canonical_path": schema.validate_relpath(relative,
                                                       "replay canonical path"),
            "canonical_sha256": row["resolved_sha256"],
        })
    replay_input = {
        "schema_version": 1, "record_type": "cp2_offline_replay_input",
        "checkpoint": "CP2-C", "source_commit": source_commit,
        "source_tree": source_tree, "executable_sha256": executable_sha256,
        "executable_build_id": executable_build_id,
        "strict_fp_verified": True, "resolved_parameters": resolved,
    }
    _write_new(transient, _json_bytes(replay_input))
    _write_new(replay_output, b"")
    output_before = replay_output.lstat()
    environment = {
        "HOME": str(Path(build["workspace"]) / "offline-replay-home"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "LD_LIBRARY_PATH": str(Path(build["workspace"]) / "devel/lib") + ":" +
                           str(Path(build["workspace"]) / "ceres-install/lib") +
                           ":/opt/ros/noetic/lib",
        "LOGNAME": "moksh", "OMP_NUM_THREADS": "1",
        "PATH": "/usr/bin:/bin", "USER": "moksh",
    }
    Path(environment["HOME"]).mkdir(mode=0o700, exist_ok=False)
    executable = Path(build["executable"])
    result = recorder.run(
        "verification",
        (str(executable), "--cp2-offline-replay", str(partial),
         "--output", str(replay_output)),
        Path("/tmp"), "offline_replay_v1", environment,
    )
    if result["exit_code"] != 0 or result["timed_out"]:
        _fail("recorded offline replay failed")
    output_after = replay_output.lstat()
    if (not stat.S_ISREG(output_after.st_mode) or output_after.st_nlink != 1 or
            (output_before.st_dev, output_before.st_ino) !=
            (output_after.st_dev, output_after.st_ino)):
        _fail("offline replay replaced its precreated output")
    replay = schema.strict_json_loads(replay_output.read_bytes())
    if not isinstance(replay, dict) or replay.get("passed") is not True:
        _fail("offline replay did not emit a passing report")
    transient.unlink()
    authorization.revalidate()
    return replay


def _file_record(root: Path, path: Path, role: str) -> Dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    schema.validate_relpath(relative, "inventory path")
    status_value = path.lstat()
    if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
        _fail("inventory leaf is not a regular single-link file: " + relative)
    return {
        "path": relative, "role": role, "size": status_value.st_size,
        "mode": stat.S_IMODE(status_value.st_mode),
        "sha256": schema.sha256_file(path),
    }


def _inventory_role(relative: str) -> str:
    if relative == "commands.jsonl":
        return "command"
    if relative == "source_snapshot.tar":
        return "source"
    if relative in ("compile_commands.json", "CMakeCache.txt"):
        return "build"
    if relative.startswith("logs/") or relative.endswith((".stdout", ".stderr")):
        return "log"
    if relative.startswith("readiness/"):
        return "readiness"
    if relative == "configuration/static_bundle.bin":
        return "configuration"
    if relative == "replay_report.json":
        return "evaluator"
    if relative.endswith(".bin"):
        return "payload"
    if relative.endswith(".jsonl") or relative.startswith("runtime/"):
        return "trace"
    _fail("artifact file has no closed inventory role: " + relative)
    raise AssertionError("unreachable")


def _freeze_existing_files(root: Path) -> None:
    identities = set()
    for current, directories, filenames in os.walk(str(root), topdown=True,
                                                    followlinks=False):
        directories.sort(key=lambda item: os.fsencode(item))
        filenames.sort(key=lambda item: os.fsencode(item))
        current_path = Path(current)
        current_status = current_path.lstat()
        if not stat.S_ISDIR(current_status.st_mode) or stat.S_ISLNK(current_status.st_mode):
            _fail("artifact contains a replaced/symlink directory")
        for name in directories:
            child = current_path / name
            child_status = child.lstat()
            if not stat.S_ISDIR(child_status.st_mode) or stat.S_ISLNK(child_status.st_mode):
                _fail("artifact contains a non-directory branch")
        for name in filenames:
            path = current_path / name
            status_value = path.lstat()
            identity = (status_value.st_dev, status_value.st_ino)
            if (not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1 or
                    identity in identities):
                _fail("artifact contains a link, special file, or inode alias")
            identities.add(identity)
            descriptor = os.open(str(path), os.O_RDONLY | os.O_CLOEXEC |
                                 getattr(os, "O_NOFOLLOW", 0))
            try:
                if not _same_stat(status_value, os.fstat(descriptor)):
                    _fail("artifact file changed while freezing")
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o444)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _inventory(root: Path) -> List[Dict[str, Any]]:
    records = []
    excluded = {"cp2_report.json", "provenance.json", "SHA256SUMS"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        records.append(_file_record(root, path, _inventory_role(relative)))
    records.sort(key=lambda item: item["path"].encode("utf-8"))
    return records


def _write_manifest(root: Path) -> str:
    rows = []
    paths = []
    identities = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        status_value = path.lstat()
        if stat.S_ISDIR(status_value.st_mode):
            continue
        if (not stat.S_ISREG(status_value.st_mode) or
                status_value.st_nlink != 1):
            _fail("manifest input contains a link or special file")
        identity = (status_value.st_dev, status_value.st_ino)
        if identity in identities:
            _fail("manifest input contains an inode alias")
        identities.add(identity)
        if relative != "SHA256SUMS":
            paths.append(path)
    paths.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    for path in paths:
        relative = path.relative_to(root).as_posix()
        schema.validate_relpath(relative, "manifest path")
        rows.append(schema.sha256_file(path) + "  " + relative + "\n")
    content = "".join(rows).encode("utf-8")
    _write_new(root / "SHA256SUMS", content, 0o444)
    return hashlib.sha256(content).hexdigest()


def _seal_directories(root: Path) -> None:
    directories = []
    for current, children, filenames in os.walk(str(root), topdown=False,
                                                followlinks=False):
        del children, filenames
        directories.append(Path(current))
    for path in directories:
        _fsync_directory(path)
        os.chmod(str(path), 0o555)
        _fsync_directory(path)


def _rename_noreplace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        _fail("final rename is not same-directory/same-filesystem")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _fail("renameat2 is unavailable; refusing an overwrite-racy fallback")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    _fsync_directory(source.parent)
    result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    if result != 0:
        value = ctypes.get_errno()
        raise OSError(value, os.strerror(value), str(destination))
    try:
        _fsync_directory(destination.parent)
    except BaseException as durability_error:
        # Restore the hidden source name if the directory-entry durability
        # proof fails.  This preserves the invariant that a returned failure
        # does not knowingly leave a newly published final name.
        rollback = renameat2(-100, os.fsencode(destination), -100,
                             os.fsencode(source), 1)
        if rollback == 0:
            try:
                _fsync_directory(source.parent)
            except BaseException:
                pass
            raise durability_error
        value = ctypes.get_errno()
        raise CampaignError(
            "final rename durability failed and rollback failed with errno {}"
            .format(value)) from durability_error


def _read_version_macro(path: Path, names: Sequence[str]) -> Optional[str]:
    try:
        text_value = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError):
        return None
    values = {}
    for name in names:
        match = re.search(r"^\s*#\s*define\s+" + re.escape(name) +
                          r"\s+\"?([A-Za-z0-9._-]+)\"?\s*$", text_value,
                          flags=re.MULTILINE)
        if match is None:
            return None
        values[name] = match.group(1)
    return ".".join(values[name] for name in names)


def _host_record(build: Mapping[str, Any]) -> Dict[str, Any]:
    uname = os.uname()
    os_release = None
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                os_release = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    cpu_model = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.lower().startswith(("model name", "hardware")) and ":" in line:
                cpu_model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    cache_text = Path(build["cmake_cache"]).read_text(encoding="utf-8", errors="strict")

    def cache(name: str) -> Optional[str]:
        match = re.search(r"^" + re.escape(name) + r":[^=]*=(.*)$", cache_text,
                          flags=re.MULTILINE)
        return match.group(1).strip() if match and match.group(1).strip() else None

    cmake_parts = [cache("CMAKE_CACHE_" + name + "_VERSION")
                   for name in ("MAJOR", "MINOR", "PATCH")]
    cmake_version = ".".join(cmake_parts) if all(cmake_parts) else None
    workspace = Path(build["workspace"])
    return {
        "hostname": socket.gethostname() or None, "os_release": os_release,
        "kernel_release": uname.release or None, "architecture": uname.machine or None,
        "cpu_model": cpu_model, "logical_cpu_count": os.cpu_count(),
        "ros_distribution": "noetic",
        "compiler_version": cache("CMAKE_CXX_COMPILER_VERSION"),
        "cmake_version": cmake_version,
        "catkin_version": _read_version_macro(
            Path("/opt/ros/noetic/include/catkin/catkin_generated/version.h"),
            ("CATKIN_VERSION",)),
        "eigen_version": _read_version_macro(
            Path("/usr/include/eigen3/Eigen/src/Core/util/Macros.h"),
            ("EIGEN_WORLD_VERSION", "EIGEN_MAJOR_VERSION", "EIGEN_MINOR_VERSION")),
        "opencv_version": _read_version_macro(
            Path("/usr/include/opencv4/opencv2/core/version.hpp"),
            ("CV_VERSION_MAJOR", "CV_VERSION_MINOR", "CV_VERSION_REVISION")),
        "boost_version": _read_version_macro(
            Path("/usr/include/boost/version.hpp"), ("BOOST_LIB_VERSION",)),
        "ceres_version": _read_version_macro(
            workspace / "ceres-src/include/ceres/version.h",
            ("CERES_VERSION_MAJOR", "CERES_VERSION_MINOR", "CERES_VERSION_REVISION")),
        "python_version": ".".join(str(value) for value in os.sys.version_info[:3]),
        "evo_version": None,
    }


ASSEMBLER_SUMMARY_KEYS = (
    "sequence_summaries", "attempted_updates", "empty_input", "all_rejected",
    "empty_after_compression", "preflight_rejected", "internal_failure",
    "committing_updates", "minimum_committing_updates", "raw_systems",
    "nullspace_gate_attempts", "schur_gate_attempts", "gate_union_denominator",
    "gate_intersection", "gate_match_numerator", "gate_ratio", "row_denominator",
    "row_match_numerator", "row_ratio", "disagreement_counts",
    "per_feature_statistics_passed", "state_blocks_expected", "state_blocks_seen",
    "covariance_blocks_expected", "covariance_blocks_seen", "maximum_state_ratio",
    "maximum_covariance_ratio", "candidate_missing_proposals",
    "baseline_commit_mismatches", "shadow_write_totals", "repair_fallback_totals",
    "gate_passed", "math_passed", "passed_pre_replay",
)


def _commands_bytes(recorder: CommandRecorder) -> bytes:
    if not recorder.records:
        _fail("command population is empty")
    if [record.get("command_id") for record in recorder.records] != list(
            range(len(recorder.records))):
        _fail("command IDs are not exact and contiguous")
    return b"".join(_json_bytes(record) for record in recorder.records)


def _held_source_hash(authorization: Any, relative: str) -> str:
    authorization.revalidate()
    descriptor = authorization.duplicate_source_fd(relative)
    try:
        content = _read_fd_all(descriptor, 64 * 1024 * 1024,
                               "held provenance source")
    finally:
        os.close(descriptor)
    authorization.revalidate()
    return hashlib.sha256(content).hexdigest()


def _build_provenance(
        partial: Path, build: Mapping[str, Any], runs: Sequence[Mapping[str, Any]],
        bags: Sequence[BoundRegularFile], static_records: Sequence[Mapping[str, Any]],
        recorder: CommandRecorder, authorization: Any, original_unit_artifact: Path,
        unit_manifest_sha256: str, executable_sha256: str, executable_build_id: str,
        inventory: Sequence[Mapping[str, Any]], created_utc: str,
        ) -> Dict[str, Any]:
    contracts = [
        {"path": relative,
         "sha256": _held_source_hash(authorization, relative)}
        for relative in CONTRACT_INPUTS
    ]
    readiness_entrypoints = authorization.record.get("entrypoints")
    if not isinstance(readiness_entrypoints, list) or len(readiness_entrypoints) != 5:
        _fail("readiness entrypoint inventory is unavailable")
    entrypoints = []
    for record in readiness_entrypoints:
        if not isinstance(record, Mapping) or not all(
                isinstance(record.get(key), str)
                for key in ("path", "sha256", "git_blob")):
            _fail("readiness entrypoint identity is invalid")
        entrypoints.append({key: record[key] for key in ("path", "sha256", "git_blob")})
    unit_report = authorization.frozen_unit_artifact / "cp2_report.json"
    if not unit_report.is_file() or unit_report.is_symlink():
        _fail("frozen unit artifact lacks cp2_report.json")
    runtime_runs = []
    resolved_records = []
    context_records = []
    inputs = []
    for index, (row, bag) in enumerate(zip(runs, bags)):
        loader_before = Path(row["loader_before"])
        loader_after = Path(row["loader_after"])
        context = Path(row["context"])
        runtime_runs.append({
            "run_id": row["run_id"], "sequence_index": index,
            "mode": "nullspace",
            "loader_map_before": loader_before.relative_to(partial).as_posix(),
            "loader_map_before_sha256": schema.sha256_file(loader_before),
            "loader_map_after": loader_after.relative_to(partial).as_posix(),
            "loader_map_after_sha256": schema.sha256_file(loader_after),
            "dso_records_before": row["dso_records_before"],
            "dso_records_after": row["dso_records_after"],
        })
        resolved_records.append({
            "run_id": row["run_id"],
            "prelaunch_raw_path": Path(row["prelaunch_raw"]).relative_to(partial).as_posix(),
            "prelaunch_raw_sha256": schema.sha256_file(row["prelaunch_raw"]),
            "runtime_raw_path": Path(row["runtime_raw"]).relative_to(partial).as_posix(),
            "runtime_raw_sha256": schema.sha256_file(row["runtime_raw"]),
            "canonical_path": Path(row["canonical"]).relative_to(partial).as_posix(),
            "canonical_sha256": row["resolved_sha256"],
            "normalized_path": None, "normalized_sha256": None,
        })
        context_status = context.lstat()
        context_records.append({
            "run_id": row["run_id"],
            "path": context.relative_to(partial).as_posix(),
            "size": context_status.st_size, "sha256": schema.sha256_file(context),
        })
        inputs.append({
            "sequence_index": index, "sequence_id": SEQUENCES[index],
            "offset_seconds": OFFSETS[index],
            "bag_path": str(bag.provenance_path or bag.path),
            "bag_size": row["bag_size"],
            "bag_sha256_before": BAG_SHA256[index],
            "bag_sha256_after": BAG_SHA256[index],
            "ground_truth_path": None, "ground_truth_sha256": None,
        })
    environments = sorted(recorder.environments.values(),
                          key=lambda item: item["environment_id"].encode("utf-8"))
    commands_path = partial / "commands.jsonl"
    source_archive = partial / "source_snapshot.tar"
    readiness_path = partial / "readiness/barrier.json"
    executable = Path(build["executable"]).resolve(strict=True)
    executable_status = executable.lstat()
    return {
        "schema_version": 1, "record_type": "provenance", "checkpoint": "CP2-C",
        "evidence_class": "trusted_runner_local_staging_evidence",
        "distribution_status": "internal_non_conveyable_staging",
        "eligible_for_cp2_seal": False, "created_utc": created_utc,
        "branch": "schurvio-lite/cp2-one-pass",
        "source_commit": authorization.repository.commit,
        "source_tree": authorization.repository.tree,
        "source_archive": "source_snapshot.tar",
        "source_archive_sha256": schema.sha256_file(source_archive), "clean": True,
        "cp1_authorization_commit": CP1_AUTHORIZATION_COMMIT,
        "contracts": contracts, "entrypoints": entrypoints,
        "readiness_barrier": "readiness/barrier.json",
        "unit_anchor": {
            "artifact": str(original_unit_artifact),
            "manifest_sha256": unit_manifest_sha256,
            "tested_commit": authorization.record["unit_tested_commit"],
            "tested_tree": authorization.record["unit_tested_tree"],
            "report_sha256": schema.sha256_file(unit_report), "verified": True,
        },
        "build": {
            "fresh_git_archive": True, "workspace": str(build["workspace"]),
            "commands_sha256": schema.sha256_file(commands_path),
            "compile_commands": "compile_commands.json",
            "compile_commands_sha256": schema.sha256_file(build["compile_commands"]),
            "cmake_cache": "CMakeCache.txt",
            "cmake_cache_sha256": schema.sha256_file(build["cmake_cache"]),
            "strict_fp_verified": True,
        },
        "readiness_barrier_sha256": schema.sha256_file(readiness_path),
        "runtime": {
            "executable": str(executable), "executable_size": executable_status.st_size,
            "executable_sha256_before": executable_sha256,
            "executable_sha256_after": executable_sha256,
            "build_id_before": executable_build_id,
            "build_id_after": executable_build_id, "runs": runtime_runs,
        },
        "configuration": {
            "static_files": list(static_records[:3]),
            "static_bundle_payload": "configuration/static_bundle.bin",
            "static_bundle_sha256": schema.sha256_file(
                partial / "configuration/static_bundle.bin"),
            "launch": dict(static_records[3]),
            "resolved_parameters": resolved_records,
            "runtime_contexts": context_records,
        },
        "inputs": inputs, "environment": {"classes": environments},
        "host": _host_record(build), "file_inventory": list(inventory),
    }


def _build_report(partial: Path, summary: Mapping[str, Any], replay: Mapping[str, Any],
                  created_utc: str) -> Dict[str, Any]:
    if set(summary) != set(ASSEMBLER_SUMMARY_KEYS):
        _fail("assembler summary key inventory differs from the closed schema")
    if (summary.get("minimum_committing_updates") != 1000 or
            summary.get("passed_pre_replay") is not True or
            summary.get("gate_passed") is not True or
            summary.get("math_passed") is not True or replay.get("passed") is not True):
        _fail("CP2-C mathematical conjunction did not pass")
    report = {
        "schema_version": 1, "record_type": "recorded_parity_campaign",
        "checkpoint": "CP2-C", "status": "passed",
        "evidence_class": "trusted_runner_local_staging_evidence",
        "distribution_status": "internal_non_conveyable_staging",
        "eligible_for_cp2_seal": False, "created_utc": created_utc,
        "provenance_sha256": schema.sha256_file(partial / "provenance.json"),
        "commands_sha256": schema.sha256_file(partial / "commands.jsonl"),
        "serial_pairs_sha256": schema.sha256_file(partial / "serial_pairs.jsonl"),
        "updates_sha256": schema.sha256_file(partial / "updates.jsonl"),
        "features_sha256": schema.sha256_file(partial / "features.jsonl"),
        "state_blocks_sha256": schema.sha256_file(partial / "state_blocks.jsonl"),
        "covariance_blocks_sha256": schema.sha256_file(
            partial / "covariance_blocks.jsonl"),
        "state_snapshot_payloads_sha256": schema.sha256_file(
            partial / "state_snapshot_payloads.bin"),
        "proposal_payloads_sha256": schema.sha256_file(
            partial / "proposal_payloads.bin"),
        "raw_system_payloads_sha256": schema.sha256_file(
            partial / "raw_system_payloads.bin"),
        "replay_report_sha256": schema.sha256_file(partial / "replay_report.json"),
        "proposal_derivation_passed": True,
    }
    for key in ASSEMBLER_SUMMARY_KEYS:
        if key != "passed_pre_replay":
            report[key] = summary[key]
    report["passed"] = True
    return report


def _detached_verify_recorded(
    partial: Path,
    build: Mapping[str, Any],
    manifest_sha256: str,
    authorization: Any,
    temporary_owner: Optional[OwnedTemporaryWorkspace] = None,
) -> None:
    verifier = Path(build["source_space"]) / "scripts/cp2/verify_report.py"
    if _held_source_hash(authorization,
                         "scripts/cp2/verify_report.py") != schema.sha256_file(verifier):
        _fail("detached verifier bytes differ from held source")
    created_here = temporary_owner is None
    if temporary_owner is None:
        temporary_owner = OwnedTemporaryWorkspace.create(
            "schurvio-cp2-final-verifier-"
        )
    temporary_owner.revalidate()
    temporary = temporary_owner.path
    stdout_path = temporary / "stdout"
    stderr_path = temporary / "stderr"
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    argv = ["/usr/bin/python3", "-I", "-B", str(verifier), "--verify-recorded",
            str(partial), "--manifest-sha256", manifest_sha256]
    process: Optional[subprocess.Popen[bytes]] = None
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            authorization.revalidate()
            try:
                process = subprocess.Popen(
                    argv, cwd="/tmp", env=environment,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    start_new_session=True, close_fds=True)
                outcome = _communicate_owned_process_group(
                    process, 1800, "detached CP2-C verifier")
                exit_code = outcome.exit_code
                if outcome.timed_out:
                    _fail("detached CP2-C verifier timed out")
                if outcome.surviving_descendants:
                    _fail("detached CP2-C verifier left a process group")
            finally:
                if process is not None:
                    _terminate_reap_and_wait_for_group_absence(
                        process, "detached CP2-C verifier")
            authorization.revalidate()
        if exit_code != 0:
            _fail("detached CP2-C verifier rejected the sealed artifact: " +
                  stderr_path.read_text(encoding="utf-8", errors="replace")[-4000:])
        expected_line = "CP2-C recorded artifact independently verified: " + str(partial)
        if expected_line not in stdout_path.read_text(encoding="utf-8", errors="strict").splitlines():
            _fail("detached CP2-C verifier stdout lacks its exact success line")
        temporary_owner.revalidate()
    except BaseException as original_error:
        if created_here:
            try:
                temporary_owner.remove()
            except BaseException as cleanup_error:
                raise CampaignError(
                    "detached verifier failed and private-workspace cleanup "
                    "failed: " + str(cleanup_error)
                ) from original_error
        raise
    else:
        if created_here:
            temporary_owner.remove()


def _trusted_parent_detached_verify_recorded(
    partial: Path,
    manifest_sha256: str,
    authorization: Any,
    temporary_owner: OwnedTemporaryWorkspace,
) -> None:
    """Verify as PID 1; retain and read only exact parent-opened log inodes."""

    partial = Path(partial).absolute()
    if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256) is None:
        _fail("trusted-parent verifier manifest identity is invalid")
    temporary_owner.revalidate()
    stdout_path = temporary_owner.path / "trusted-parent.stdout"
    stderr_path = temporary_owner.path / "trusted-parent.stderr"
    verifier_fd = -1
    stdout_fd = -1
    stderr_fd = -1
    try:
        verifier_fd = authorization.duplicate_source_fd(
            "scripts/cp2/verify_report.py"
        )
        verifier_status = os.fstat(verifier_fd)
        if not stat.S_ISREG(verifier_status.st_mode):
            _fail("trusted-parent detached verifier is not a held regular file")
        stdout_fd = os.open(
            stdout_path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
            getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=temporary_owner.descriptor,
        )
        stdout_identity = _trusted_verifier_stream_identity(os.fstat(stdout_fd))
        _validate_trusted_verifier_stream(
            stdout_fd, stdout_path, stdout_identity,
            temporary_owner, "stdout",
        )
        stderr_fd = os.open(
            stderr_path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
            getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=temporary_owner.descriptor,
        )
        stderr_identity = _trusted_verifier_stream_identity(os.fstat(stderr_fd))
        _validate_trusted_verifier_stream(
            stderr_fd, stderr_path, stderr_identity,
            temporary_owner, "stderr",
        )
        os.fsync(temporary_owner.descriptor)
        authorization.revalidate()
        verifier_path = "/proc/self/fd/{}".format(verifier_fd)
        result = _run_trusted_verifier_in_namespaces(
            [
                "/usr/bin/python3", "-I", "-B", verifier_path,
                "--verify-recorded", str(partial),
                "--manifest-sha256", manifest_sha256,
            ],
            {
                "PATH": "/usr/bin:/bin",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            },
            verifier_fd,
            stdout_fd,
            stderr_fd,
            stdout_path,
            stderr_path,
            stdout_identity,
            stderr_identity,
            temporary_owner,
        )
        required = {
            "schema_version", "record_type", "worker_started",
            "descendants_absent", "exit_code", "timed_out",
            "error_type", "error",
        }
        if (
            set(result) != required
            or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1
            or result.get("record_type")
            != "cp2_trusted_verifier_supervisor_result"
            or not isinstance(result.get("worker_started"), bool)
            or result.get("descendants_absent") is not True
            or not isinstance(result.get("timed_out"), bool)
            or (
                result.get("error_type") is None
                and (
                    result.get("error") is not None
                    or type(result.get("exit_code")) is not int
                )
            )
            or (
                result.get("error_type") is not None
                and (
                    not isinstance(result.get("error_type"), str)
                    or not isinstance(result.get("error"), str)
                    or result.get("exit_code") is not None
                )
            )
        ):
            _fail("trusted verifier supervisor result shape differs")
        authorization.revalidate()
        os.fsync(stdout_fd)
        os.fsync(stderr_fd)
        _validate_trusted_verifier_stream(
            stdout_fd, stdout_path, stdout_identity,
            temporary_owner, "stdout",
        )
        _validate_trusted_verifier_stream(
            stderr_fd, stderr_path, stderr_identity,
            temporary_owner, "stderr",
        )
        stdout_bytes = _read_fd_all(
            stdout_fd, 16 * 1024 * 1024, "trusted verifier stdout"
        )
        stderr_bytes = _read_fd_all(
            stderr_fd, 16 * 1024 * 1024, "trusted verifier stderr"
        )
        _validate_trusted_verifier_stream(
            stdout_fd, stdout_path, stdout_identity,
            temporary_owner, "stdout",
        )
        _validate_trusted_verifier_stream(
            stderr_fd, stderr_path, stderr_identity,
            temporary_owner, "stderr",
        )
        if result["error_type"] is not None:
            _fail(
                "trusted-parent detached verifier isolation failed: "
                + result["error_type"] + ": " + result["error"]
            )
        if result["timed_out"]:
            _fail("trusted-parent detached CP2-C verifier timed out")
        if result["exit_code"] != 0:
            _fail(
                "trusted-parent detached CP2-C verifier rejected the sealed "
                "artifact: "
                + stderr_bytes.decode("utf-8", errors="replace")[-4000:]
            )
        expected_line = (
            "CP2-C recorded artifact independently verified: " + str(partial)
        )
        try:
            stdout_lines = stdout_bytes.decode("utf-8", errors="strict").splitlines()
        except UnicodeError as exc:
            raise CampaignError(
                "trusted-parent detached verifier stdout is not UTF-8"
            ) from exc
        if expected_line not in stdout_lines:
            _fail("trusted-parent detached verifier lacks its exact success line")
        temporary_owner.revalidate()
    finally:
        if stderr_fd >= 0:
            os.close(stderr_fd)
        if stdout_fd >= 0:
            os.close(stdout_fd)
        if verifier_fd >= 0:
            os.close(verifier_fd)


def _validate_output_staging(
    repo_root: Path,
    run_id: str,
    staging: Any,
) -> Tuple[Path, Path, Path]:
    parent = Path(staging.parent).absolute()
    partial = Path(staging.partial).absolute()
    final = Path(staging.final).absolute()
    expected_parent = repo_root / "results/staging/cp2/recorded"
    if (
        staging.run_id != run_id
        or parent != expected_parent
        or partial.parent != parent
        or final != parent / run_id
        or re.fullmatch(
            r"\." + re.escape(run_id) + r"\.partial\.[0-9a-f]{32}",
            partial.name,
        ) is None
    ):
        _fail("readiness output-staging capability returned an invalid binding")
    if os.path.lexists(str(final)):
        _fail("recorded final destination already exists")
    partial_status = partial.lstat()
    if (
        not stat.S_ISDIR(partial_status.st_mode)
        or stat.S_ISLNK(partial_status.st_mode)
        or partial_status.st_uid != os.geteuid()
        or stat.S_IMODE(partial_status.st_mode) not in (0o700, 0o555)
    ):
        _fail("readiness output-staging partial identity/mode is invalid")
    return parent, partial, final


def _retain_campaign_failure(
    artifact_root: Path,
    root_descriptor: int,
    source_commit: str,
    source_tree: str,
    failure: BaseException,
    *,
    quarantine_untrusted_collision: bool = False,
) -> None:
    """Commit the sole failure marker descriptor-relatively without replace."""

    artifact_root = Path(artifact_root).absolute()
    root_status = artifact_root.lstat()
    held_status = os.fstat(root_descriptor)
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or stat.S_ISLNK(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or _directory_object_identity(root_status)
        != _directory_object_identity(held_status)
    ):
        _fail("failed artifact root is no longer an owned real directory")
    os.fchmod(root_descriptor, 0o700)
    if stat.S_IMODE(os.fstat(root_descriptor).st_mode) != 0o700:
        _fail("failed artifact exact mode transition differs")
    primary_failure, cleanup_failures = _failure_components(failure)
    quarantined_collision: Optional[str] = None
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _fail("renameat2 is unavailable for failure-marker commit")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
        ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if quarantine_untrusted_collision:
        try:
            collision_before = os.stat(
                "failure.json",
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            collision_before = None
        if collision_before is not None:
            quarantined_collision = (
                ".untrusted-failure.json." + os.urandom(16).hex()
            )
            if renameat2(
                root_descriptor,
                b"failure.json",
                root_descriptor,
                os.fsencode(quarantined_collision),
                1,
            ) != 0:
                _raise_namespace_errno(
                    "failure-marker collision quarantine renameat2 noreplace"
                )
            collision_after = os.stat(
                quarantined_collision,
                dir_fd=root_descriptor,
                follow_symlinks=False,
            )
            if (
                _directory_object_identity(collision_before)
                != _directory_object_identity(collision_after)
            ):
                _fail("failure-marker collision changed during quarantine")
            try:
                os.stat(
                    "failure.json",
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                _fail("failure-marker collision retained its trusted name")
            os.fsync(root_descriptor)
    payload = _json_bytes({
        "schema_version": 1,
        "record_type": "cp2_failure",
        "checkpoint": "CP2-C",
        "created_utc": _utc_now(),
        "source_commit": source_commit,
        "source_tree": source_tree,
        "error_type": primary_failure["type"],
        "error": primary_failure["message"],
        "primary_failure": primary_failure,
        "cleanup_failures": list(cleanup_failures),
        "quarantined_untrusted_failure_name": quarantined_collision,
        "recorded_input_authorized": True,
    })
    temporary_name = ".failure.json.partial." + os.urandom(16).hex()
    temporary_fd = -1
    created_status: Optional[os.stat_result] = None
    renamed = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC |
            getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        created_status = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(created_status.st_mode)
            or created_status.st_nlink != 1
        ):
            _fail("failure-marker temporary is not a regular single-link file")
        _write_complete(temporary_fd, payload, "failure-marker write")
        os.fsync(temporary_fd)
        created_status = os.fstat(temporary_fd)
        if renameat2(
            root_descriptor,
            os.fsencode(temporary_name),
            root_descriptor,
            b"failure.json",
            1,
        ) != 0:
            _raise_namespace_errno("failure-marker renameat2 noreplace")
        renamed = True
        marker_status = os.stat(
            "failure.json", dir_fd=root_descriptor, follow_symlinks=False
        )
        if not _same_stat_without_ctime(created_status, marker_status):
            _fail("failure marker changed during exact commit")
        marker_fd = os.open(
            "failure.json",
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            if (
                not _same_stat(marker_status, os.fstat(marker_fd))
                or _read_fd_all(
                    marker_fd,
                    MAX_ISOLATED_WORKER_MESSAGE_BYTES,
                    "retained failure marker",
                )
                != payload
            ):
                _fail("failure marker bytes/binding differ after commit")
        finally:
            os.close(marker_fd)
        os.fsync(root_descriptor)
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if not renamed:
            try:
                temporary_status = os.stat(
                    temporary_name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                if created_status is None or not _same_stat_without_ctime(
                    temporary_status, created_status
                ):
                    _fail("failure-marker temporary was substituted")
                os.unlink(temporary_name, dir_fd=root_descriptor)
                os.fsync(root_descriptor)


def _execute_recorded_campaign_worker(
    repo_root: Path,
    parsed_cli: Mapping[str, str],
    registry_bytes: bytes,
    authorization: Any,
    staging: Any,
    workspace_owner: OwnedTemporaryWorkspace,
    bag_mounts: Sequence[_BagMountBinding],
) -> str:
    """Populate and seal one hidden artifact; never publish."""

    source_commit = authorization.repository.commit
    source_tree = authorization.repository.tree
    run_id = staging.run_id
    parent, partial, _ = _validate_output_staging(repo_root, run_id, staging)
    original_unit_artifact = Path(parsed_cli["--unit-artifact"]).absolute()
    unit_artifact = Path(authorization.frozen_unit_artifact).absolute()
    if unit_artifact.parent != authorization.temporary_root:
        _fail("frozen unit artifact is outside the held readiness root")

    # Parse only the one verified postauthorization buffer.  This worker never
    # reopens the registry and never retries a recorded command.
    bag_paths = _registry_bag_paths(registry_bytes)
    if (
        len(bag_mounts) != len(bag_paths)
        or tuple(binding.original_path for binding in bag_mounts) != bag_paths
    ):
        _fail("recorded-input mount/original-path binding differs")
    failure: Optional[BaseException] = None
    completed_manifest: Optional[str] = None
    bound_bags: List[BoundRegularFile] = []
    runs: List[Dict[str, Any]] = []
    try:
        workspace_owner.revalidate()
        authorization.revalidate()
        _copy_readiness(partial, authorization)
        recorder = CommandRecorder(partial, authorization)
        recorder.import_readiness_unit_verification()
        self_test_environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "CP2_SELF_TEST": "1", "CP2_FORBID_BAG_ACCESS": "1",
        }
        self_test_digest = recorder.add_environment(
            "readiness_self_test_v1", self_test_environment)
        if any(record.get("environment_sha256") != self_test_digest
               for record in authorization.record.get("self_tests", [])):
            _fail("readiness self-test environment does not join provenance")
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
        bundle, static_records = encode_static_bundle(repo_root, authorization)
        config_sha = hashlib.sha256(bundle).hexdigest()
        _write_new(partial / "configuration/static_bundle.bin", bundle)
        for binding, expected in zip(bag_mounts, BAG_SHA256):
            bound = _bind_regular_file(binding.target_path)
            bound.provenance_path = binding.original_path
            bound_bags.append(bound)
            if (
                not _same_stat(bound.identity, binding.source_identity)
                or bound.sha256() != expected
            ):
                _fail("registry-resolved bag hash differs from frozen CP2 identity")
        build = _build_runtime(
            repo_root,
            partial,
            unit_artifact,
            authorization,
            recorder,
            source_commit,
            workspace_owner=workspace_owner,
        )
        candidate_owner = build.get("workspace_owner")
        if (
            candidate_owner is not workspace_owner
            or Path(build["workspace"]).absolute() != candidate_owner.path
        ):
            _fail("runtime build did not return its exact workspace owner")
        workspace_owner.revalidate()
        executable_before = schema.sha256_file(build["executable"])
        build_id_before, executable_soname = _elf_identity(
            Path(build["executable"]).absolute(), require_soname=False)
        if executable_soname is not None:
            _fail("runtime executable unexpectedly declares a DSO SONAME")
        runs, serial_sha = _run_sequences(
            repo_root, partial, build, recorder, authorization, bound_bags,
            source_commit, config_sha)
        workspace_owner.revalidate()
        summary = _invoke_assembler(partial, build, runs, serial_sha, recorder,
                                    authorization, config_sha)
        workspace_owner.revalidate()
        replay = _invoke_offline_replay(
            partial, build, runs, recorder, authorization, source_commit,
            source_tree, executable_before, build_id_before)
        workspace_owner.revalidate()
        executable_after = schema.sha256_file(build["executable"])
        build_id_after, _ = _elf_identity(
            Path(build["executable"]).absolute(), require_soname=False)
        if (executable_before != executable_after or
                build_id_before != build_id_after):
            _fail("runtime executable bytes/build ID changed during campaign")

        # Journals and per-run serial files are authoritative transient inputs
        # to the assembler.  Their complete information is now represented by
        # the fixed final rows/payloads, so retaining them would create orphan
        # evidence outside the closed inventory.
        for row in runs:
            held_outputs = row.get("held_outputs")
            if not isinstance(held_outputs, _HeldRuntimeOutputs):
                _fail("runtime output capability owner is unavailable")
            held_outputs.unlink_exact(("updater.journal", "serial.jsonl"))
            for unexpected in ("state.staging.bin", "proposal.staging.bin",
                               "raw.staging.bin"):
                if os.path.lexists(str(Path(row["trace"]) / unexpected)):
                    _fail("runtime emitted an undeclared staging payload")

        command_bytes = _commands_bytes(recorder)
        _write_new(partial / "commands.jsonl", command_bytes)
        _freeze_existing_files(partial)
        inventory = _inventory(partial)
        created_utc = _utc_now()
        provenance = _build_provenance(
            partial, build, runs, bound_bags, static_records, recorder,
            authorization, original_unit_artifact,
            parsed_cli["--unit-manifest-sha256"], executable_before,
            build_id_before, inventory, created_utc)
        _write_new(partial / "provenance.json", _json_bytes(provenance), 0o444)
        report = _build_report(partial, summary, replay, created_utc)
        _write_new(partial / "cp2_report.json", _json_bytes(report), 0o444)
        manifest_sha256 = _write_manifest(partial)
        _seal_directories(partial)
        workspace_owner.revalidate()
        authorization.revalidate()
        completed_manifest = manifest_sha256
    except BaseException as exc:
        failure = exc
    finally:
        cleanup_errors: List[BaseException] = []
        for row in runs:
            held_outputs = row.get("held_outputs")
            if isinstance(held_outputs, _HeldRuntimeOutputs):
                try:
                    held_outputs.close()
                except BaseException as exc:
                    cleanup_errors.append(exc)
        for bound in bound_bags:
            try:
                bound.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if failure is None:
                failure = _CampaignFailureBundle(
                    _bounded_failure_record(CampaignError(
                        "recorded-input descriptor cleanup failed"
                    )),
                    tuple(
                        _bounded_failure_record(item)
                        for item in cleanup_errors
                    ),
                )
            else:
                failure = _append_cleanup_failures(
                    failure, cleanup_errors
                )
    if failure is not None:
        raise failure
    if completed_manifest is None:
        _fail("recorded campaign ended without a finalized manifest")
    return completed_manifest


def _waitpid_exit_code(process_id: int) -> int:
    while True:
        try:
            observed, status_value = os.waitpid(process_id, 0)
            break
        except InterruptedError:
            continue
    if observed != process_id:
        _fail("isolated-worker wait returned a different process")
    if os.WIFEXITED(status_value):
        return os.WEXITSTATUS(status_value)
    if os.WIFSIGNALED(status_value):
        return 128 + os.WTERMSIG(status_value)
    _fail("isolated-worker wait returned a nonterminal status")


def _kill_and_reap_bounded(process_id: int, label: str) -> None:
    """SIGKILL one exact child and bound the terminal wait."""

    try:
        os.kill(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + MAX_PROCESS_GROUP_CLEANUP_SECONDS
    while True:
        try:
            observed, status_value = os.waitpid(process_id, os.WNOHANG)
        except ChildProcessError:
            return
        except InterruptedError:
            continue
        if observed == process_id:
            if not (os.WIFEXITED(status_value) or os.WIFSIGNALED(status_value)):
                _fail(label + " cleanup returned a nonterminal status")
            return
        if observed != 0:
            _fail(label + " cleanup reaped a different process")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _fail(label + " could not be reaped after SIGKILL")
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def _waitpid_exit_code_bounded(
    process_id: int,
    timeout: float,
    label: str,
) -> Tuple[int, bool]:
    """Wait for one exact child, SIGKILLing and reaping it at the deadline."""

    if not math.isfinite(timeout) or timeout <= 0.0:
        _fail(label + " wait bound is invalid")
    deadline = time.monotonic() + timeout
    while True:
        try:
            observed, status_value = os.waitpid(process_id, os.WNOHANG)
        except InterruptedError:
            continue
        if observed == process_id:
            if os.WIFEXITED(status_value):
                return os.WEXITSTATUS(status_value), False
            if os.WIFSIGNALED(status_value):
                return 128 + os.WTERMSIG(status_value), False
            _fail(label + " wait returned a nonterminal status")
        if observed != 0:
            _fail(label + " wait reaped a different process")
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _kill_and_reap_bounded(process_id, label)
            return 128 + signal.SIGKILL, True
        time.sleep(min(PROCESS_GROUP_POLL_SECONDS, remaining))


def _pidfd_open(process_id: int) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    descriptor = libc.syscall(
        ctypes.c_long(_SYS_PIDFD_OPEN_X86_64),
        ctypes.c_int(process_id),
        ctypes.c_uint(0),
    )
    if descriptor < 0:
        _raise_namespace_errno("pidfd_open trusted verifier worker")
    os.set_inheritable(descriptor, False)
    return int(descriptor)


def _send_trusted_verifier_pidfd(
    channel: socket.socket, process_id: int
) -> None:
    descriptor = _pidfd_open(process_id)
    try:
        sent = channel.sendmsg(
            [b"V"],
            [(
                socket.SOL_SOCKET,
                socket.SCM_RIGHTS,
                struct.pack("=i", descriptor),
            )],
        )
        if sent != 1:
            _fail("trusted verifier pidfd announcement was incomplete")
    finally:
        os.close(descriptor)


def _receive_trusted_verifier_pidfd(channel: socket.socket) -> int:
    """Receive either one exact worker pidfd or a pre-worker failure token."""

    channel.settimeout(MAX_PROCESS_GROUP_CLEANUP_SECONDS)
    flags = getattr(socket, "MSG_CMSG_CLOEXEC", 0)
    payload, ancillary, message_flags, _ = channel.recvmsg(
        1,
        socket.CMSG_SPACE(struct.calcsize("=i")),
        flags,
    )
    received: List[int] = []
    try:
        if message_flags & getattr(socket, "MSG_CTRUNC", 0):
            _fail("trusted verifier pidfd announcement was truncated")
        for level, kind, content in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                _fail("trusted verifier pidfd announcement type differs")
            width = struct.calcsize("=i")
            if len(content) != width:
                _fail("trusted verifier pidfd announcement count differs")
            received.append(struct.unpack("=i", content)[0])
        if payload == b"E" and not received:
            return -1
        if payload != b"V" or len(received) != 1:
            _fail("trusted verifier pidfd announcement framing differs")
        descriptor = received.pop()
        os.set_inheritable(descriptor, False)
        os.fstat(descriptor)
        return descriptor
    finally:
        for descriptor in received:
            os.close(descriptor)


def _pidfd_ready(descriptor: int, timeout: float) -> bool:
    if timeout < 0.0 or not math.isfinite(timeout):
        _fail("trusted verifier pidfd wait bound is invalid")
    deadline = time.monotonic() + timeout
    poller = select.poll()
    poller.register(
        descriptor,
        select.POLLIN | select.POLLHUP | select.POLLERR,
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return bool(poller.poll(0))
        try:
            events = poller.poll(max(1, int(math.ceil(remaining * 1000.0))))
        except InterruptedError:
            continue
        return bool(events)


def _kill_and_wait_pidfd_bounded(descriptor: int, label: str) -> None:
    """Make one exact pidfd non-live and observe its bounded terminal event."""

    if not _pidfd_ready(descriptor, 0.0):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long
        result = libc.syscall(
            ctypes.c_long(_SYS_PIDFD_SEND_SIGNAL_X86_64),
            ctypes.c_int(descriptor),
            ctypes.c_int(signal.SIGKILL),
            ctypes.c_void_p(),
            ctypes.c_uint(0),
        )
        if result != 0 and ctypes.get_errno() != errno.ESRCH:
            _raise_namespace_errno(label + " pidfd SIGKILL")
    if not _pidfd_ready(descriptor, MAX_PROCESS_GROUP_CLEANUP_SECONDS):
        _fail(label + " pidfd did not reach a terminal state")


def _redirect_worker_standard_descriptors() -> None:
    descriptor = os.open("/dev/null", os.O_RDWR | os.O_CLOEXEC)
    try:
        for target in (0, 1, 2):
            if descriptor != target:
                os.dup2(descriptor, target, inheritable=True)
    finally:
        if descriptor > 2:
            os.close(descriptor)


def _close_unapproved_worker_descriptors(approved: Iterable[int]) -> None:
    """Close every inherited FD except exact current-namespace capabilities."""

    keep = {0, 1, 2}
    for descriptor in approved:
        if isinstance(descriptor, bool) or not isinstance(descriptor, int):
            _fail("worker descriptor inventory contains a noninteger")
        if descriptor < 0:
            _fail("worker descriptor inventory contains a closed descriptor")
        os.fstat(descriptor)
        keep.add(descriptor)
    directory_fd = os.open(
        "/proc/self/fd",
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
        getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        population = tuple(os.listdir(directory_fd))
    finally:
        os.close(directory_fd)
    for raw in population:
        try:
            descriptor = int(raw)
        except ValueError:
            _fail("worker descriptor namespace contains a nonnumeric name")
        if descriptor in keep or descriptor == directory_fd:
            continue
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    for descriptor in keep:
        os.fstat(descriptor)


def _receive_exact_socket_message(
    channel: socket.socket, expected: bytes, label: str
) -> None:
    """Receive one fixed handshake without assuming stream packet boundaries."""

    observed = bytearray()
    while len(observed) < len(expected):
        block = channel.recv(len(expected) - len(observed))
        if not block:
            _fail(label + " ended before its complete fixed message")
        observed.extend(block)
    if bytes(observed) != expected:
        _fail(label + " differs")


def _worker_parent_liveness_handshake(channel: socket.socket) -> None:
    """Close the pre-prctl parent-death race with a two-way handshake."""

    _set_parent_death_signal(signal.SIGKILL)
    channel.settimeout(MAX_PROCESS_GROUP_CLEANUP_SECONDS)
    channel.sendall(b"CP2-WORKER-READY\n")
    _receive_exact_socket_message(
        channel,
        b"CP2-SUPERVISOR-ACK\n",
        "campaign worker supervisor-liveness acknowledgement",
    )


def _supervisor_worker_liveness_handshake(channel: socket.socket) -> None:
    channel.settimeout(MAX_PROCESS_GROUP_CLEANUP_SECONDS)
    _receive_exact_socket_message(
        channel,
        b"CP2-WORKER-READY\n",
        "campaign supervisor worker-readiness handshake",
    )
    channel.sendall(b"CP2-SUPERVISOR-ACK\n")


def _trusted_verifier_stream_identity(value: os.stat_result) -> Tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_uid,
        value.st_gid,
    )


def _validate_trusted_verifier_stream(
    descriptor: int,
    path: Path,
    expected_identity: Tuple[int, ...],
    temporary_owner: OwnedTemporaryWorkspace,
    label: str,
) -> None:
    temporary_owner.revalidate()
    held = os.fstat(descriptor)
    by_path = os.lstat(str(path))
    if (
        not stat.S_ISREG(held.st_mode)
        or stat.S_ISLNK(by_path.st_mode)
        or held.st_nlink != 1
        or held.st_uid != os.geteuid()
        or stat.S_IMODE(held.st_mode) != 0o600
        or _trusted_verifier_stream_identity(held) != expected_identity
        or not _same_stat(held, by_path)
    ):
        _fail("trusted verifier " + label + " leaf/path identity changed")


def _redirect_trusted_verifier_standard_descriptors(
    stdout_descriptor: int,
    stderr_descriptor: int,
) -> None:
    null_descriptor = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.dup2(null_descriptor, 0, inheritable=True)
        os.dup2(stdout_descriptor, 1, inheritable=True)
        os.dup2(stderr_descriptor, 2, inheritable=True)
    finally:
        if null_descriptor > 2:
            os.close(null_descriptor)


def _trusted_verifier_supervisor_record(
    *,
    worker_started: bool,
    descendants_absent: bool,
    exit_code: Optional[int],
    timed_out: bool,
    error_type: Optional[str],
    error: Optional[str],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "cp2_trusted_verifier_supervisor_result",
        "worker_started": worker_started,
        "descendants_absent": descendants_absent,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "error_type": error_type,
        "error": error,
    }


def _run_trusted_verifier_in_namespaces(
    argv: Sequence[str],
    environment: Mapping[str, str],
    verifier_descriptor: int,
    stdout_descriptor: int,
    stderr_descriptor: int,
    stdout_path: Path,
    stderr_path: Path,
    stdout_identity: Tuple[int, ...],
    stderr_identity: Tuple[int, ...],
    temporary_owner: OwnedTemporaryWorkspace,
) -> Mapping[str, Any]:
    """Run verifier as PID 1 and return only after its namespace is dead."""

    expected_outer_parent_pid = os.getpid()
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    parent_pid_channel, supervisor_pid_channel = socket.socketpair(
        socket.AF_UNIX,
        socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
    )
    supervisor_pid = -1
    worker_pidfd = -1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        supervisor_pid = os.fork()
    except BaseException:
        os.close(status_read)
        os.close(status_write)
        parent_pid_channel.close()
        supervisor_pid_channel.close()
        raise
    if supervisor_pid == 0:
        os.close(status_read)
        parent_pid_channel.close()
        worker_pid = -1
        worker_started = False
        pidfd_announced = False
        rebound_stdout = -1
        rebound_stderr = -1
        try:
            _set_parent_death_signal(signal.SIGKILL)
            if os.getppid() != expected_outer_parent_pid:
                os._exit(125)
            _install_trusted_verifier_mount_and_pid_namespaces(
                temporary_owner.path,
                temporary_owner.identity,
            )
            temporary_owner.rebind_current_mount_namespace()
            rebound_stdout = os.open(
                stdout_path.name,
                os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=temporary_owner.descriptor,
            )
            rebound_stderr = os.open(
                stderr_path.name,
                os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=temporary_owner.descriptor,
            )
            _validate_trusted_verifier_stream(
                rebound_stdout, stdout_path, stdout_identity,
                temporary_owner, "stdout",
            )
            _validate_trusted_verifier_stream(
                rebound_stderr, stderr_path, stderr_identity,
                temporary_owner, "stderr",
            )
            supervisor_channel, worker_channel = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
            )
            worker_pid = os.fork()
            worker_started = True
            if worker_pid == 0:
                supervisor_channel.close()
                supervisor_pid_channel.close()
                os.close(status_write)
                try:
                    _worker_parent_liveness_handshake(worker_channel)
                    worker_channel.close()
                    _redirect_trusted_verifier_standard_descriptors(
                        rebound_stdout, rebound_stderr
                    )
                    os.set_inheritable(verifier_descriptor, True)
                    _close_unapproved_worker_descriptors({verifier_descriptor})
                    os.chdir("/tmp")
                    _enter_worker_pid_namespace_and_drop_capabilities(
                        temporary_owner.path
                    )
                    os.execve(argv[0], list(argv), dict(environment))
                except BaseException as exc:
                    message = (
                        "trusted verifier worker setup failed: "
                        + type(exc).__name__ + ": " + str(exc) + "\n"
                    ).encode("utf-8", errors="replace")[-4096:]
                    try:
                        _write_complete(2, message, "trusted verifier error")
                    except BaseException:
                        pass
                    os._exit(126)

            worker_channel.close()
            _send_trusted_verifier_pidfd(supervisor_pid_channel, worker_pid)
            pidfd_announced = True
            supervisor_pid_channel.close()
            os.close(verifier_descriptor)
            os.close(stdout_descriptor)
            os.close(stderr_descriptor)
            os.close(rebound_stdout)
            rebound_stdout = -1
            os.close(rebound_stderr)
            rebound_stderr = -1
            _supervisor_worker_liveness_handshake(supervisor_channel)
            supervisor_channel.close()
            worker_exit, timed_out = _waitpid_exit_code_bounded(
                worker_pid,
                1800.0,
                "trusted verifier PID namespace",
            )
            worker_pid = -1
            record = _trusted_verifier_supervisor_record(
                worker_started=True,
                descendants_absent=True,
                exit_code=worker_exit,
                timed_out=timed_out,
                error_type=None,
                error=None,
            )
            _write_pipe_record(status_write, record)
            os.close(status_write)
            os._exit(0)
        except BaseException as exc:
            if worker_pid > 0:
                try:
                    _kill_and_reap_bounded(
                        worker_pid, "trusted verifier PID namespace"
                    )
                    worker_pid = -1
                except BaseException as cleanup_exc:
                    exc = CampaignError(
                        type(exc).__name__ + ": " + str(exc)
                        + "; verifier PID-namespace cleanup failed: "
                        + type(cleanup_exc).__name__ + ": " + str(cleanup_exc)
                    )
            if not pidfd_announced:
                try:
                    supervisor_pid_channel.sendall(b"E")
                except BaseException:
                    pass
            try:
                _write_pipe_record(status_write, _trusted_verifier_supervisor_record(
                    worker_started=worker_started,
                    descendants_absent=worker_pid <= 0,
                    exit_code=None,
                    timed_out=False,
                    error_type=type(exc).__name__,
                    error=str(exc)[:8192],
                ))
                os.close(status_write)
                os._exit(0)
            except BaseException:
                os._exit(127)

    os.close(status_write)
    supervisor_pid_channel.close()
    try:
        worker_pidfd = _receive_trusted_verifier_pidfd(parent_pid_channel)
        parent_pid_channel.close()
        supervisor_exit, supervisor_timed_out = _waitpid_exit_code_bounded(
            supervisor_pid,
            1800.0 + 3.0 * MAX_PROCESS_GROUP_CLEANUP_SECONDS,
            "trusted verifier supervisor",
        )
        supervisor_pid = -1
        if worker_pidfd >= 0:
            if not _pidfd_ready(worker_pidfd, 0.0):
                _kill_and_wait_pidfd_bounded(
                    worker_pidfd, "trusted verifier worker"
                )
                _fail("trusted verifier supervisor returned before its PID namespace")
        if supervisor_timed_out:
            _fail("trusted verifier supervisor timed out")
        try:
            result = _read_pipe_record(status_read)
        finally:
            os.close(status_read)
            status_read = -1
        if supervisor_exit != 0:
            _fail("trusted verifier supervisor failed: exit " + str(supervisor_exit))
        return result
    except BaseException:
        if supervisor_pid > 0:
            _kill_and_reap_bounded(
                supervisor_pid, "trusted verifier supervisor"
            )
            supervisor_pid = -1
        if worker_pidfd >= 0:
            _kill_and_wait_pidfd_bounded(
                worker_pidfd, "trusted verifier worker"
            )
        raise
    finally:
        parent_pid_channel.close()
        if status_read >= 0:
            os.close(status_read)
        if worker_pidfd >= 0:
            os.close(worker_pidfd)


def _isolated_worker_result(
    *,
    passed: bool,
    worker_started: bool,
    descendants_absent: bool,
    manifest_sha256: Optional[str],
    error_type: Optional[str],
    error: Optional[str],
    primary_failure: Optional[Mapping[str, Any]],
    cleanup_failures: Sequence[Mapping[str, Any]],
    failure_marker_retained: bool,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "cp2_isolated_worker_result",
        "passed": passed,
        "worker_started": worker_started,
        "descendants_absent": descendants_absent,
        "manifest_sha256": manifest_sha256,
        "error_type": error_type,
        "error": error,
        "primary_failure": (
            None if primary_failure is None else dict(primary_failure)
        ),
        "cleanup_failures": [dict(item) for item in cleanup_failures],
        "failure_marker_retained": failure_marker_retained,
    }


def _run_campaign_in_namespaces(
    repo_root: Path,
    parsed_cli: Mapping[str, str],
    registry_bytes: bytes,
    authorization: Any,
    staging: Any,
    workspace_owner: OwnedTemporaryWorkspace,
    bag_mounts: Sequence[_BagMountBinding],
) -> Mapping[str, Any]:
    """Return only after PID-namespace init and every descendant are absent."""

    _, partial, _ = _validate_output_staging(repo_root, staging.run_id, staging)
    partial_identity = authorization.output_staging_identity(staging)
    workspace_owner.revalidate()
    writable_roots = (
        (partial, partial_identity),
        (workspace_owner.path, workspace_owner.identity),
    )
    expected_outer_parent_pid = os.getpid()
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        supervisor_pid = os.fork()
    except BaseException:
        os.close(status_read)
        os.close(status_write)
        raise
    if supervisor_pid == 0:
        os.close(status_read)
        campaign_pid = -1
        try:
            _set_parent_death_signal(signal.SIGKILL)
            if os.getppid() != expected_outer_parent_pid:
                os._exit(125)
            _install_worker_mount_and_pid_namespaces(
                writable_roots, bag_mounts
            )
            authorization.rebind_worker_mount_namespace(staging)
            workspace_owner.rebind_current_mount_namespace()
            result_read, result_write = os.pipe2(os.O_CLOEXEC)
            supervisor_channel, worker_channel = socket.socketpair(
                socket.AF_UNIX,
                socket.SOCK_STREAM | getattr(socket, "SOCK_CLOEXEC", 0),
            )
            campaign_pid = os.fork()
            if campaign_pid == 0:
                supervisor_channel.close()
                os.close(result_read)
                os.close(status_write)
                _worker_parent_liveness_handshake(worker_channel)
                _redirect_worker_standard_descriptors()
                authorization_inventory = authorization.descriptor_inventory()
                approved_descriptors = {
                    record["fd"] for record in authorization_inventory.values()
                }
                approved_descriptors.add(workspace_owner.descriptor)
                approved_descriptors.add(result_write)
                approved_descriptors.add(worker_channel.fileno())
                _close_unapproved_worker_descriptors(approved_descriptors)
                worker_channel.close()

                def cancel_worker(signum: int, frame: Any) -> None:
                    del frame
                    raise CampaignError(
                        "campaign worker received signal " + str(signum)
                    )

                signal.signal(signal.SIGTERM, cancel_worker)
                signal.signal(signal.SIGINT, cancel_worker)
                worker_started = False
                try:
                    _enter_worker_pid_namespace_and_drop_capabilities(
                        workspace_owner.path
                    )
                    worker_started = True
                    manifest = _execute_recorded_campaign_worker(
                        repo_root,
                        parsed_cli,
                        registry_bytes,
                        authorization,
                        staging,
                        workspace_owner,
                        bag_mounts,
                    )
                    record = _isolated_worker_result(
                        passed=True,
                        worker_started=True,
                        descendants_absent=False,
                        manifest_sha256=manifest,
                        error_type=None,
                        error=None,
                        primary_failure=None,
                        cleanup_failures=(),
                        failure_marker_retained=False,
                    )
                    exit_code = 0
                except BaseException as exc:
                    primary_failure, cleanup_failures = _failure_components(exc)
                    record = _isolated_worker_result(
                        passed=False,
                        worker_started=worker_started,
                        descendants_absent=False,
                        manifest_sha256=None,
                        error_type=primary_failure["type"],
                        error=primary_failure["message"],
                        primary_failure=primary_failure,
                        cleanup_failures=cleanup_failures,
                        failure_marker_retained=False,
                    )
                    exit_code = 1
                try:
                    _write_pipe_record(result_write, record)
                except BaseException:
                    exit_code = 126
                try:
                    os.close(result_write)
                finally:
                    os._exit(exit_code)

            worker_channel.close()
            _supervisor_worker_liveness_handshake(supervisor_channel)
            supervisor_channel.close()
            os.close(result_write)
            try:
                child_record = _read_pipe_record(result_read)
            finally:
                os.close(result_read)
            campaign_exit = _waitpid_exit_code(campaign_pid)
            campaign_pid = -1
            required = {
                "schema_version", "record_type", "passed", "worker_started",
                "descendants_absent", "manifest_sha256", "error_type",
                "error", "primary_failure", "cleanup_failures",
                "failure_marker_retained",
            }
            if (
                set(child_record) != required
                or type(child_record.get("schema_version")) is not int
                or child_record.get("schema_version") != 1
                or child_record.get("record_type")
                != "cp2_isolated_worker_result"
                or not isinstance(child_record.get("passed"), bool)
                or not isinstance(child_record.get("worker_started"), bool)
                or child_record.get("descendants_absent") is not False
                or child_record.get("failure_marker_retained") is not False
                or (
                    child_record.get("passed") is True
                    and (
                        not isinstance(child_record.get("manifest_sha256"), str)
                        or re.fullmatch(
                            r"[0-9a-f]{64}", child_record["manifest_sha256"]
                        ) is None
                        or child_record.get("error_type") is not None
                        or child_record.get("error") is not None
                        or child_record.get("primary_failure") is not None
                        or child_record.get("cleanup_failures") != []
                    )
                )
                or (
                    child_record.get("passed") is False
                    and (
                        child_record.get("manifest_sha256") is not None
                        or not isinstance(child_record.get("error_type"), str)
                        or not isinstance(child_record.get("error"), str)
                        or child_record.get("primary_failure") is None
                        or not isinstance(
                            child_record.get("cleanup_failures"), list
                        )
                    )
                )
            ):
                _fail("campaign PID-namespace result shape differs")
            if child_record["passed"] is False:
                primary_failure = _validate_failure_record(
                    child_record["primary_failure"], "worker primary"
                )
                cleanup_failures = tuple(
                    _validate_failure_record(item, "worker cleanup")
                    for item in child_record["cleanup_failures"]
                )
                if (
                    len(cleanup_failures) > MAX_CLEANUP_FAILURES
                    or child_record["error_type"] != primary_failure["type"]
                    or child_record["error"] != primary_failure["message"]
                ):
                    _fail("campaign PID-namespace failure projection differs")
            child_passed = child_record["passed"]
            if (child_passed and campaign_exit != 0) or (
                not child_passed and campaign_exit == 0
            ):
                _fail("campaign PID-namespace exit/result differs")
            forwarded = dict(child_record)
            forwarded["descendants_absent"] = True
            if campaign_exit not in (0, 1):
                abnormal = _bounded_failure_record(CampaignError(
                    "campaign worker exit code " + str(campaign_exit)
                ))
                forwarded.update({
                    "passed": False,
                    "manifest_sha256": None,
                    "error_type": abnormal["type"],
                    "error": abnormal["message"],
                    "primary_failure": abnormal,
                    "cleanup_failures": [],
                })
            _write_pipe_record(status_write, forwarded)
            os.close(status_write)
            os._exit(0)
        except BaseException as exc:
            if campaign_pid > 0:
                try:
                    _kill_and_reap_bounded(
                        campaign_pid, "isolated campaign PID namespace"
                    )
                    campaign_pid = -1
                except BaseException as cleanup_exc:
                    exc = _append_cleanup_failures(exc, (cleanup_exc,))
            primary_failure, cleanup_failures = _failure_components(exc)
            try:
                _write_pipe_record(status_write, _isolated_worker_result(
                    passed=False,
                    worker_started=False,
                    descendants_absent=campaign_pid <= 0,
                    manifest_sha256=None,
                    error_type=primary_failure["type"],
                    error=primary_failure["message"],
                    primary_failure=primary_failure,
                    cleanup_failures=cleanup_failures,
                    failure_marker_retained=False,
                ))
            except BaseException:
                pass
            try:
                os.close(status_write)
            finally:
                os._exit(127)

    os.close(status_write)
    try:
        try:
            result = _read_pipe_record(status_read)
        finally:
            os.close(status_read)
        supervisor_exit = _waitpid_exit_code(supervisor_pid)
    except BaseException as original_error:
        try:
            _kill_and_reap_bounded(
                supervisor_pid, "isolated campaign supervisor"
            )
        except BaseException as cleanup_error:
            raise _append_cleanup_failures(
                original_error, (cleanup_error,)
            ) from original_error
        raise
    if supervisor_exit != 0:
        primary_failure = _validate_failure_record(
            result.get("primary_failure"), "campaign supervisor primary"
        )
        cleanup_failures = tuple(
            _validate_failure_record(item, "campaign supervisor cleanup")
            for item in result.get("cleanup_failures", [])
        )
        failure = _CampaignFailureBundle(
            primary_failure, cleanup_failures
        )
        raise _append_cleanup_failures(
            failure,
            (CampaignError(
                "isolated campaign supervisor exit " + str(supervisor_exit)
            ),),
        )
    return result


def _sealed_artifact_snapshot(partial: Path) -> Tuple[Any, ...]:
    """Hash and bind every sealed artifact entry without following links."""

    partial = Path(partial).absolute()
    root_before = partial.lstat()
    if (
        not stat.S_ISDIR(root_before.st_mode)
        or stat.S_ISLNK(root_before.st_mode)
        or root_before.st_uid != os.geteuid()
        or stat.S_IMODE(root_before.st_mode) != 0o555
    ):
        _fail("sealed artifact root identity/mode differs")
    root_fd = os.open(
        str(partial),
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
        getattr(os, "O_NOFOLLOW", 0),
    )
    records: List[Tuple[Any, ...]] = []
    try:
        if not _same_stat(root_before, os.fstat(root_fd)):
            _fail("sealed artifact root changed while opening")

        def walk(directory_fd: int, prefix: str) -> None:
            directory_before = os.fstat(directory_fd)
            names = sorted(
                os.listdir(directory_fd), key=lambda item: item.encode("utf-8")
            )
            for name in names:
                if not name or name in (".", "..") or "/" in name or "\0" in name:
                    _fail("sealed artifact contains an unsafe path component")
                relative = name if not prefix else prefix + "/" + name
                schema.validate_relpath(relative, "sealed artifact path")
                before = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if before.st_uid != os.geteuid():
                    _fail("sealed artifact entry owner differs: " + relative)
                if stat.S_ISDIR(before.st_mode):
                    if stat.S_IMODE(before.st_mode) != 0o555:
                        _fail("sealed artifact directory mode differs: " + relative)
                    child_fd = os.open(
                        name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC |
                        getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        if not _same_stat(before, os.fstat(child_fd)):
                            _fail("sealed artifact directory binding changed")
                        records.append((relative, "d", _stat_tuple(before), None))
                        walk(child_fd, relative)
                        if not _same_stat(before, os.fstat(child_fd)):
                            _fail("sealed artifact directory changed while scanning")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(before.st_mode):
                    if (
                        before.st_nlink != 1
                        or stat.S_IMODE(before.st_mode) != 0o444
                    ):
                        _fail("sealed artifact file mode/link count differs: " + relative)
                    descriptor = os.open(
                        name,
                        os.O_RDONLY | os.O_CLOEXEC |
                        getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=directory_fd,
                    )
                    try:
                        if not _same_stat(before, os.fstat(descriptor)):
                            _fail("sealed artifact file binding changed")
                        digest = hashlib.sha256()
                        while True:
                            block = os.read(descriptor, 1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                        if not _same_stat(before, os.fstat(descriptor)):
                            _fail("sealed artifact file changed while hashing")
                    finally:
                        os.close(descriptor)
                    records.append(
                        (relative, "f", _stat_tuple(before), digest.hexdigest())
                    )
                else:
                    _fail("sealed artifact contains a link or special entry")
                if len(records) > MAX_DEPENDENCY_MEMBERS:
                    _fail("sealed artifact entry count exceeds its exact bound")
            if not _same_stat(directory_before, os.fstat(directory_fd)):
                _fail("sealed artifact directory changed during traversal")

        walk(root_fd, "")
        root_after = os.fstat(root_fd)
        by_path = partial.lstat()
        if not _same_stat(root_before, root_after) or not _same_stat(
            root_before, by_path
        ):
            _fail("sealed artifact root changed during snapshot")
        return (_stat_tuple(root_before), tuple(records))
    finally:
        os.close(root_fd)


def execute_recorded_campaign(
    repo_root: Path,
    parsed_cli: Mapping[str, str],
    registry_bytes: bytes,
    authorization: Any,
    prepublication_guard: Optional[Callable[[], None]] = None,
) -> int:
    """Run one confined CP2-C campaign, then publish only in the trusted parent."""

    repo_root = Path(repo_root).absolute()
    source_commit = authorization.repository.commit
    source_tree = authorization.repository.tree
    if HEX40.fullmatch(source_commit) is None or HEX40.fullmatch(source_tree) is None:
        _fail("readiness source identity is invalid")
    if prepublication_guard is not None and not callable(prepublication_guard):
        _fail("CP2-C prepublication guard is not callable")
    run_id = parsed_cli.get("--run-id")
    if run_id is None:
        run_id = "cp2_recorded_{}-g{}".format(
            dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"),
            source_commit[:12],
        )
    schema.validate_safe_id(run_id, "recorded run ID")
    staging = authorization.create_output_staging(run_id)
    _, partial, final = _validate_output_staging(repo_root, run_id, staging)
    workspace_owner: Optional[OwnedTemporaryWorkspace] = None
    verifier_owner: Optional[OwnedTemporaryWorkspace] = None
    bag_mount_owner: Optional[OwnedTemporaryWorkspace] = None
    parent_bound_bags: Tuple[BoundRegularFile, ...] = ()
    bag_mounts: Tuple[_BagMountBinding, ...] = ()
    result: Optional[Mapping[str, Any]] = None
    sealed_snapshot: Optional[Tuple[Any, ...]] = None
    failure: Optional[BaseException] = None
    try:
        workspace_owner = OwnedTemporaryWorkspace.create()
        verifier_owner = OwnedTemporaryWorkspace.create(
            "schurvio-cp2-final-verifier-"
        )
        bag_mount_owner = OwnedTemporaryWorkspace.create(
            "schurvio-cp2-bag-bind-"
        )
        parent_bound_bags, bag_mounts = _prepare_parent_bag_mounts(
            registry_bytes, bag_mount_owner
        )
        authorization.revalidate()
        result = _run_campaign_in_namespaces(
            repo_root,
            parsed_cli,
            registry_bytes,
            authorization,
            staging,
            workspace_owner,
            bag_mounts,
        )
        if result.get("passed") is False:
            primary_failure = _validate_failure_record(
                result.get("primary_failure"), "isolated worker primary"
            )
            cleanup_failures = tuple(
                _validate_failure_record(item, "isolated worker cleanup")
                for item in result.get("cleanup_failures", [])
            )
            if (
                len(cleanup_failures) > MAX_CLEANUP_FAILURES
                or result.get("error_type") != primary_failure["type"]
                or result.get("error") != primary_failure["message"]
            ):
                _fail("isolated campaign failure projection differs")
            raise _CampaignFailureBundle(
                primary_failure, cleanup_failures
            )
        if (
            result.get("passed") is not True
            or result.get("worker_started") is not True
            or result.get("descendants_absent") is not True
            or not isinstance(result.get("manifest_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", result["manifest_sha256"]) is None
            or result.get("error_type") is not None
            or result.get("error") is not None
            or result.get("primary_failure") is not None
            or result.get("cleanup_failures") != []
            or result.get("failure_marker_retained") is not False
        ):
            raise CampaignError(
                "isolated campaign worker failed closed: {}: {}".format(
                    result.get("error_type"), result.get("error")
                )
            )
        manifest_sha256 = result["manifest_sha256"]
        authorization.revalidate()
        for bound, expected in zip(parent_bound_bags, BAG_SHA256):
            if bound.sha256() != expected:
                _fail("parent-held recorded input changed across isolated run")
        partial_status = partial.lstat()
        if (
            not stat.S_ISDIR(partial_status.st_mode)
            or stat.S_ISLNK(partial_status.st_mode)
            or stat.S_IMODE(partial_status.st_mode) != 0o555
            or schema.sha256_file(partial / "SHA256SUMS") != manifest_sha256
        ):
            _fail("postworker sealed artifact identity differs")
        _trusted_parent_detached_verify_recorded(
            partial, manifest_sha256, authorization, verifier_owner
        )
        for bound, expected in zip(parent_bound_bags, BAG_SHA256):
            if bound.sha256() != expected:
                _fail("parent-held recorded input changed during detached verification")
        authorization.revalidate()
        sealed_snapshot = _sealed_artifact_snapshot(partial)
    except BaseException as exc:
        failure = exc
    finally:
        cleanup_errors: List[BaseException] = []
        for bound in parent_bound_bags:
            try:
                bound.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        for owner in (verifier_owner, bag_mount_owner, workspace_owner):
            if owner is None:
                continue
            try:
                owner.remove()
            except BaseException as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            if failure is None:
                failure = _CampaignFailureBundle(
                    _bounded_failure_record(CampaignError(
                        "isolated campaign capability cleanup failed"
                    )),
                    tuple(
                        _bounded_failure_record(item)
                        for item in cleanup_errors
                    ),
                )
            else:
                failure = _append_cleanup_failures(
                    failure, cleanup_errors
                )

    if failure is not None:
        try:
            failure_state = authorization.output_publication_state(staging)
            if failure_state != "hidden":
                _fail(
                    "prepublication campaign failure lost its hidden root"
                )
            partial_descriptor = authorization.duplicate_output_root_fd(staging)
            try:
                _retain_campaign_failure(
                    partial,
                    partial_descriptor,
                    source_commit,
                    source_tree,
                    failure,
                    quarantine_untrusted_collision=True,
                )
            finally:
                os.close(partial_descriptor)
        except BaseException as marker_error:
            raise CampaignError(
                "campaign failed and its exact retained failure marker failed: "
                + str(marker_error)
            ) from failure
        raise failure
    if result is None or sealed_snapshot is None:
        _fail("isolated campaign returned no verified sealed result")
    manifest_sha256 = result["manifest_sha256"]
    publication_diagnostic: Optional[BaseException] = None
    try:
        authorization.revalidate()
        if _sealed_artifact_snapshot(partial) != sealed_snapshot:
            _fail("sealed artifact changed after detached verification")
        if prepublication_guard is not None:
            prepublication_guard()
        authorization.revalidate()
        if _sealed_artifact_snapshot(partial) != sealed_snapshot:
            _fail("sealed artifact changed before trusted publication")
        authorization.publish_output(staging)
    except BaseException as exc:
        publication_state: Optional[str] = None
        try:
            publication_state = authorization.output_publication_state(staging)
            if publication_state == "published":
                publication_diagnostic = exc
            elif publication_state in ("hidden", "published_uncommitted"):
                failure_root = (
                    partial if publication_state == "hidden" else final
                )
                root_descriptor = authorization.duplicate_output_root_fd(staging)
                try:
                    _retain_campaign_failure(
                        failure_root,
                        root_descriptor,
                        source_commit,
                        source_tree,
                        exc,
                        quarantine_untrusted_collision=True,
                    )
                finally:
                    os.close(root_descriptor)
            else:
                _fail("publication reconciliation returned an invalid state")
        except BaseException as marker_error:
            raise CampaignError(
                "publication failed and its exact state/marker reconciliation "
                "failed: " + str(marker_error)
            ) from exc
        if publication_state != "published":
            raise

    # Publication is the authoritative success boundary.  An exception after
    # the authorization committed the exact sealed final inode is diagnostic;
    # the artifact is never reopened or mutated in that state.
    if publication_diagnostic is not None:
        try:
            sys.stderr.write(
                "CP2-C publication committed; post-commit diagnostic: "
                + type(publication_diagnostic).__name__ + ": "
                + str(publication_diagnostic) + "\n"
            )
        except BaseException:
            pass

    # Closed reporting
    # streams cannot turn a durable final artifact into a campaign failure.
    try:
        print("CP2-C recorded campaign passed: " + str(final))
        print("SHA256SUMS SHA-256: " + manifest_sha256)
    except BaseException:
        pass
    return 0


__all__ = [
    "BAG_SHA256", "CampaignError", "OFFSETS", "SEQUENCES",
    "encode_static_bundle", "execute_recorded_campaign",
]
