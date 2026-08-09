#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Profile-bound Linux backend candidate for the CP2-E root helper.

This module contains no executable entry point.  Importing or constructing it
does not read or change the host.  The actual helper remains disabled until an
installed root launcher, sudoers rule, exact profile, and reversible
feasibility receipt have all been frozen and approved.

The backend deliberately returns a complete schema-v2 state.  In particular,
the durable prior contains every typed CPU/IRQ control, the exact peer thread
population and prior cpuset membership, the complete numeric IRQ population,
and the absence of the helper-owned DMA constraint.  That is enough for a
fresh helper process to restore after predecessor death; process-local hidden
state is never treated as recovery evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cp2_timing_privileged_helper as protocol
import cp2_timing_profile as profile_codec


U64_MAX = (1 << 64) - 1
MAX_TEXT_BYTES = 64 * 1024
MAX_TASKS = profile_codec.GUARDIAN_POPULATION_ITEM_LIMIT
MAX_IRQS = 65_536
MAX_STATE_ROWS = 65_536
CONTROL_STATE_SCHEMA_VERSION = 2
SAFE_CONTROL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,191}$")
PRIOR_STATE_KEYS = (
    "modules_preexisting", "process_affinity", "record_type",
    "schema_version", "services_active", "values", "privileged_v2",
)
TYPED_VALUE_KEYS = (
    "control_id", "desired_text", "parser", "path", "text", "writable",
)
PRIVILEGED_EXTENSION_KEYS = (
    "control_plane_processes", "control_plane_threads", "cpuset",
    "dma_latency_held_by_helper", "helper_affinity_cpu_ids",
    "descendant_process_ids", "descendant_threads", "irq_numbers",
    "irq_population_sha256", "peer", "peer_start_time_ticks", "profile_sha256",
    "record_type", "schema_version",
)
CPUSET_STATE_KEYS = (
    "cpus", "effective_cpus", "effective_mems", "exists", "group_path",
    "members", "mems",
)
DESCENDANT_THREAD_STATE_KEYS = (
    "affinity_cpu_ids", "cpuset_membership", "process_id",
    "start_time_ticks", "tid",
)
CONTROL_PLANE_PROCESS_KEYS = (
    "executable_device", "executable_inode", "parent_pid", "pid",
    "process_gid", "process_uid", "start_time_ticks",
)
FOREIGN_AFFINITY_ELIGIBILITY_KEYS = (
    "effective_affinity_cpu_ids", "process_start_time_ticks", "tid",
)


class PrivilegedBackendError(protocol.PrivilegedHelperError):
    """The exact privileged plan could not be applied or validated."""


class PrivilegedBackendIndeterminate(protocol.RecoveryIndeterminate):
    """Exact restoration or guardian termination could not be proved."""


def _fail(message: str) -> None:
    raise PrivilegedBackendError(message)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _sha_record(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > U64_MAX:
        _fail(label + " is outside u64")
    return value


def _exact_mapping(
    value: Any, expected_keys: Sequence[str], label: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or tuple(sorted(value)) != tuple(sorted(expected_keys)):
        _fail(label + " keys differ")
    return value


def _string_population(
    value: Any, label: str, allowed: Iterable[str],
) -> Tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _fail(label + " is not a string population")
    retained = tuple(value)
    if retained != tuple(sorted(set(retained))) or not set(retained).issubset(set(allowed)):
        _fail(label + " is duplicate, unordered, or outside its frozen population")
    return retained


def _cpu_population(
    value: Any, label: str, allowed: Iterable[int], *, allow_empty: bool = False,
) -> Tuple[int, ...]:
    if type(value) is not list or any(type(item) is not int for item in value):
        _fail(label + " is not an integer CPU population")
    retained = tuple(value)
    if (
        (not retained and not allow_empty)
        or retained != tuple(sorted(set(retained)))
        or not set(retained).issubset(set(allowed))
    ):
        _fail(label + " is empty, duplicate, unordered, or outside the target")
    return retained


def _control_process_rows(
    value: Any, label: str,
) -> Tuple[Mapping[str, Any], ...]:
    if type(value) is not list or not value or len(value) > MAX_TASKS:
        _fail(label + " is not a bounded nonempty process chain")
    rows: List[Mapping[str, Any]] = []
    pids = []
    for raw in value:
        row = _exact_mapping(raw, CONTROL_PLANE_PROCESS_KEYS, label + " row")
        retained = {}
        for key in CONTROL_PLANE_PROCESS_KEYS:
            retained[key] = _u64(row[key], label + " " + key)
        if retained["pid"] == 0 or retained["parent_pid"] == 0:
            _fail(label + " contains a zero process identity")
        rows.append(retained)
        pids.append(retained["pid"])
    if len(pids) != len(set(pids)):
        _fail(label + " contains duplicate process IDs")
    return tuple(rows)


def _scheduler_thread_rows(
    value: Any, process_ids: Sequence[int], allowed_cpus: Sequence[int],
    label: str,
) -> Tuple[Mapping[str, Any], ...]:
    if type(value) is not list or not value or len(value) > MAX_TASKS:
        _fail(label + " is not a bounded nonempty thread population")
    process_set = set(process_ids)
    retained = []
    tids = []
    for raw in value:
        row = _exact_mapping(raw, DESCENDANT_THREAD_STATE_KEYS, label + " row")
        tid = _u64(row["tid"], label + " TID")
        process_id = _u64(row["process_id"], label + " process ID")
        start = _u64(row["start_time_ticks"], label + " start time")
        affinity = _cpu_population(
            row["affinity_cpu_ids"], label + " affinity", allowed_cpus,
        )
        membership = row["cpuset_membership"]
        if (
            tid == 0 or process_id not in process_set
            or type(membership) is not str or not membership.startswith("/")
            or os.path.normpath(membership) != membership
            or len(membership.encode("utf-8"))
            > profile_codec.MAX_CPUSET_MEMBERSHIP_BYTES
        ):
            _fail(label + " identity/membership differs")
        retained.append({
            "affinity_cpu_ids": list(affinity),
            "cpuset_membership": membership,
            "process_id": process_id,
            "start_time_ticks": start,
            "tid": tid,
        })
        tids.append(tid)
    if (
        tids != sorted(set(tids))
        or {item["process_id"] for item in retained} != process_set
        or any(
            sum(
                item["tid"] == process_id
                and item["process_id"] == process_id
                for item in retained
            ) != 1
            for process_id in process_set
        )
    ):
        _fail(label + " is unordered/duplicate or omits a process leader")
    return tuple(retained)


def _normalized_absolute(value: Any, label: str) -> str:
    if (
        type(value) is not str or not value.startswith("/") or value == "/"
        or os.path.normpath(value) != value
    ):
        _fail(label + " is not normalized absolute syntax")
    return value


def _canonical_text(raw: bytes, parser: str, label: str) -> str:
    if type(raw) is not bytes or not raw or len(raw) > MAX_TEXT_BYTES:
        _fail(label + " is empty or oversized")
    try:
        value = raw.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise PrivilegedBackendError(label + " is not ASCII") from exc
    if value.endswith("\n"):
        value = value[:-1]
    if not value or any(character in value for character in "\0\r\n"):
        _fail(label + " is not one canonical text line")
    if parser == "integer":
        if not value.isdigit() or (len(value) > 1 and value.startswith("0")):
            _fail(label + " is not canonical unsigned decimal")
        _u64(int(value), label)
    elif parser == "cpu_list":
        _parse_cpu_list(value, label)
    elif parser == "cpu_mask":
        _parse_cpu_mask(value, label)
    elif parser != "text":
        _fail(label + " parser is unsupported")
    return value


def _parse_cpu_list(value: str, label: str) -> Tuple[int, ...]:
    if type(value) is not str or not value:
        _fail(label + " CPU list is empty")
    result: List[int] = []
    for token in value.split(","):
        pieces = token.split("-")
        if len(pieces) == 1 and pieces[0].isdigit():
            result.append(int(pieces[0]))
        elif len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
            low, high = (int(piece) for piece in pieces)
            if low > high or high > 4095:
                _fail(label + " CPU range is invalid")
            result.extend(range(low, high + 1))
        else:
            _fail(label + " CPU list syntax is invalid")
    retained = tuple(result)
    if (
        not retained or retained != tuple(sorted(set(retained)))
        or retained[-1] > 4095
    ):
        _fail(label + " CPU list is not sorted, unique, and bounded")
    return retained


def _parse_cpu_mask(value: str, label: str) -> Tuple[int, ...]:
    """Decode Linux's big-endian sequence of 32-bit hexadecimal chunks."""

    if type(value) is not str:
        _fail(label + " CPU mask is not text")
    chunks = value.split(",")
    if (
        not chunks or len(chunks) > 128
        or any(re.fullmatch(r"[0-9a-f]{8}", chunk) is None for chunk in chunks)
    ):
        _fail(label + " CPU mask is not canonical 32-bit lowercase chunks")
    result = []
    for word_index, chunk in enumerate(reversed(chunks)):
        word = int(chunk, 16)
        for bit in range(32):
            if word & (1 << bit):
                result.append(word_index * 32 + bit)
    if not result or result[-1] > 4095:
        _fail(label + " CPU mask is empty or exceeds the CPU bound")
    return tuple(result)


def _cpu_list_text(values: Sequence[int]) -> str:
    cpus = tuple(values)
    if not cpus or cpus != tuple(sorted(set(cpus))):
        _fail("CPU population is not sorted and unique")
    return ",".join(str(cpu) for cpu in cpus)


def _cpu_mask_text(
    values: Sequence[int], controlled_cpu_ids: Sequence[int],
) -> str:
    """Encode one CPU set in Linux's comma-separated 32-bit bitmap form."""

    cpus = tuple(values)
    controlled = tuple(controlled_cpu_ids)
    if (
        not cpus or cpus != tuple(sorted(set(cpus)))
        or not controlled or controlled != tuple(range(len(controlled)))
        or not set(cpus).issubset(set(controlled))
    ):
        _fail("CPU mask population is not a bounded canonical subset")
    words = [0] * ((len(controlled) + 31) // 32)
    for cpu in cpus:
        words[cpu // 32] |= 1 << (cpu % 32)
    encoded = ",".join(format(word, "08x") for word in reversed(words))
    if _parse_cpu_mask(encoded, "encoded") != cpus:
        _fail("CPU mask encoding did not round-trip")
    return encoded


@dataclass(frozen=True)
class TypedControl:
    control_id: str
    path: str
    parser: str
    desired_text: Optional[str]
    writable: bool

    def __post_init__(self) -> None:
        if (
            type(self.control_id) is not str
            or SAFE_CONTROL_ID.fullmatch(self.control_id) is None
        ):
            _fail("typed control ID is invalid")
        _normalized_absolute(self.path, "typed control path")
        if self.parser not in ("text", "integer", "cpu_list", "cpu_mask"):
            _fail("typed control parser is invalid")
        if type(self.writable) is not bool:
            _fail("typed control writable flag is not Boolean")
        if self.writable:
            if type(self.desired_text) is not str:
                _fail("writable typed control lacks a desired value")
            _canonical_text(
                (self.desired_text + "\n").encode("ascii"), self.parser,
                "typed desired value",
            )
        elif self.desired_text is not None:
            _fail("read-only typed control unexpectedly has a desired value")

    def observed_record(self, text: str) -> Mapping[str, Any]:
        _canonical_text((text + "\n").encode("ascii"), self.parser, self.control_id)
        return {
            "control_id": self.control_id,
            "desired_text": self.desired_text,
            "parser": self.parser,
            "path": self.path,
            "text": text,
            "writable": self.writable,
        }


def derive_typed_controls(
    profile: profile_codec.FrozenTimingProfile,
) -> Tuple[TypedControl, ...]:
    """Derive the complete state population from typed schema-v2 sections."""

    if type(profile) is not profile_codec.FrozenTimingProfile:
        _fail("typed control derivation requires FrozenTimingProfile")
    value = profile_codec.load_profile_bytes(profile.canonical_bytes).value
    result: List[TypedControl] = []
    boost = value["cpu_controls"]["boost"]
    result.append(TypedControl(
        "boost", boost["path"], boost["parser"], boost["desired_text"], True,
    ))
    for policy in value["cpu_controls"]["policies"]:
        prefix = "cpufreq." + policy["policy_id"] + "."
        result.extend((
            TypedControl(prefix + "driver", policy["driver_path"], "text", None, False),
            TypedControl(
                prefix + "governor", policy["governor_path"], "text",
                policy["desired_governor"], True,
            ),
            TypedControl(
                prefix + "minimum_frequency_khz",
                policy["minimum_frequency_path"], "integer",
                str(policy["desired_minimum_frequency_khz"]), True,
            ),
            TypedControl(
                prefix + "maximum_frequency_khz",
                policy["maximum_frequency_path"], "integer",
                str(policy["desired_maximum_frequency_khz"]), True,
            ),
        ))
    for cpu in value["cpu_controls"]["cpus"]:
        cpu_id = cpu["cpu_id"]
        if cpu["online_path"] is not None:
            result.append(TypedControl(
                "cpu.{}.online".format(cpu_id), cpu["online_path"], "integer",
                "1" if cpu["expected_online"] else "0", True,
            ))
        for idle in cpu["idle_states"]:
            prefix = "cpu.{}.idle.{}.".format(cpu_id, idle["state_index"])
            result.extend((
                TypedControl(prefix + "name", idle["name_path"], "text", None, False),
                TypedControl(
                    prefix + "disabled", idle["disable_path"], "integer",
                    "1" if idle["desired_disabled"] else "0", True,
                ),
            ))
    interrupts = value["interrupts"]
    result.append(TypedControl(
        "irq.default_affinity", interrupts["default_affinity_path"],
        "cpu_mask", _cpu_mask_text(
            interrupts["default_affinity_cpu_ids"],
            value["cpu_plan"]["controlled_cpu_ids"],
        ), True,
    ))
    for irq in interrupts["irq_records"]:
        result.append(TypedControl(
            "irq.{}.affinity".format(irq["irq"]), irq["affinity_path"],
            "cpu_list", _cpu_list_text(irq["desired_cpu_ids"]), True,
        ))
    identifiers = tuple(item.control_id for item in result)
    paths = tuple(item.path for item in result)
    if (
        len(result) > MAX_STATE_ROWS or len(set(identifiers)) != len(result)
        or len(set(paths)) != len(result)
    ):
        _fail("typed control population is duplicate or oversized")
    return tuple(result)


def require_legacy_write_closure(
    profile: profile_codec.FrozenTimingProfile,
    controls: Sequence[TypedControl],
) -> None:
    """Require every typed writable surface in the legacy ordered write list.

    Schema v2 still carries ``controls.writes`` because retained applied-state
    snapshots and command evidence use it.  Formal profiles may not leave that
    legacy list as a partial projection of the authoritative typed plan.
    """

    expected = [
        (item.path, item.parser, item.desired_text)
        for item in controls if item.writable
    ]
    observed = [
        (item["path"], item["parser"], item["desired_text"])
        for item in profile.value["controls"]["writes"]
    ]
    if observed != expected:
        _fail("legacy write list is not the complete ordered typed-v2 projection")


class LinuxRootSystem:
    """Narrow Linux primitives used only after the root launcher unlocks them."""

    def __init__(self) -> None:
        self._dma_descriptor: Optional[int] = None
        self._proc_descriptors: Dict[int, Tuple[int, int, int]] = {}
        self._task_descriptors: Dict[Tuple[int, int], Tuple[int, int, int]] = {}

    def require_root(self) -> None:
        if os.geteuid() != 0:
            _fail("privileged backend process is not effective uid zero")

    @staticmethod
    def monotonic_ns() -> int:
        return time.monotonic_ns()

    def read_raw(self, path: str, maximum: int = MAX_TEXT_BYTES) -> bytes:
        _normalized_absolute(path, "read path")
        if type(maximum) is not int or maximum <= 0 or maximum > MAX_TEXT_BYTES:
            _fail("read byte bound is invalid")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not (stat.S_ISREG(status.st_mode) or stat.S_ISCHR(status.st_mode)):
                _fail("read surface is neither regular nor character-special")
            chunks: List[bytes] = []
            size = 0
            while True:
                chunk = os.read(descriptor, min(4096, maximum + 1 - size))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    _fail("read surface exceeds its byte bound")
        finally:
            os.close(descriptor)

    def read_text(self, path: str, parser: str) -> str:
        return _canonical_text(self.read_raw(path), parser, path)

    def source_identity_sha256(
        self, domain: str, paths: Sequence[Tuple[str, bool]],
        structural: Mapping[str, Any],
    ) -> str:
        """Hash stable inode metadata plus a typed structural observation."""

        if type(domain) is not str or not domain.startswith("SchurVIO-CP2-E-"):
            _fail("source-identity domain is invalid")
        if type(paths) not in (tuple, list) or not paths:
            _fail("source-identity path population is empty")
        rows = []
        retained_paths = []
        for raw in paths:
            if type(raw) not in (tuple, list) or len(raw) != 2:
                _fail("source-identity path descriptor differs")
            path, allow_absent = raw
            _normalized_absolute(path, "source-identity path")
            if type(allow_absent) is not bool:
                _fail("source-identity absence policy is not Boolean")
            if path in retained_paths:
                _fail("source-identity path population is duplicate")
            retained_paths.append(path)
            try:
                status = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                if not allow_absent:
                    _fail("required source-identity path is absent: " + path)
                rows.append({"exists": False, "path": path})
                continue
            if stat.S_ISLNK(status.st_mode):
                _fail("source-identity path is a symlink")
            if not (
                stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)
                or stat.S_ISCHR(status.st_mode)
            ):
                _fail("source-identity inode type is unsupported")
            rows.append({
                "device": status.st_dev, "exists": True, "gid": status.st_gid,
                "inode": status.st_ino, "mode": status.st_mode,
                "nlink": status.st_nlink, "path": path,
                "rdev": status.st_rdev, "size_bytes": status.st_size,
                "uid": status.st_uid,
            })
        record = {
            "domain": domain, "paths": rows, "structural": dict(structural),
        }
        return _sha_record(record)

    def cpuset_mountinfo_line(self, mount_path: str) -> str:
        _normalized_absolute(mount_path, "cpuset mount path")
        payload = self.read_raw("/proc/self/mountinfo", MAX_TEXT_BYTES)
        try:
            lines = payload.decode("ascii", "strict").splitlines()
        except UnicodeDecodeError as exc:
            raise PrivilegedBackendError("mountinfo is not ASCII") from exc
        matches = []
        for line in lines:
            pieces = line.split(" ")
            if len(pieces) < 10 or "-" not in pieces:
                _fail("mountinfo line is malformed")
            separator = pieces.index("-")
            if pieces[4] == mount_path:
                if (
                    separator + 3 >= len(pieces)
                    or pieces[separator + 1] != "cgroup"
                    or "cpuset" not in pieces[separator + 3].split(",")
                ):
                    _fail("frozen cpuset mount is not a cgroup-v1 cpuset")
                matches.append(line)
        if len(matches) != 1:
            _fail("cpuset mountinfo identity is not unique")
        return matches[0]

    def write_text(self, path: str, text: str) -> None:
        _normalized_absolute(path, "write path")
        if any(character in text for character in "\0\r\n") or not text:
            _fail("write value is not one text line")
        flags = os.O_WRONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            payload = (text + "\n").encode("ascii", "strict")
            offset = 0
            while offset < len(payload):
                count = os.write(descriptor, payload[offset:])
                if count <= 0:
                    _fail("control write made no progress")
                offset += count
        finally:
            os.close(descriptor)

    @staticmethod
    def module_loaded(name: str) -> bool:
        if name not in ("k10temp", "msr"):
            _fail("module name is outside the authorized set")
        return os.path.isdir("/sys/module/" + name)

    @staticmethod
    def service_active(name: str) -> bool:
        if name != "irqbalance":
            _fail("service name is outside the authorized set")
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=10, check=False,
        )
        if result.returncode not in (0, 3):
            _fail("service-state query failed")
        return result.returncode == 0

    @staticmethod
    def command(argv: Sequence[str]) -> None:
        command = tuple(argv)
        permitted = (
            len(command) == 3 and command[0] == "/usr/sbin/modprobe"
            and command[1] == "--" and command[2] in ("k10temp", "msr")
        ) or (
            len(command) == 4 and command[:3] == ("/usr/sbin/modprobe", "-r", "--")
            and command[3] in ("k10temp", "msr")
        ) or (
            len(command) == 3 and command[0] == "/usr/bin/systemctl"
            and command[1] in ("start", "stop") and command[2] == "irqbalance"
        )
        if not permitted:
            _fail("root command is outside the exact authorized set")
        result = subprocess.run(
            list(command), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd="/",
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            timeout=30, check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            _fail("root command failed or exceeded its output bound")

    @staticmethod
    def _proc_directory_flags() -> int:
        return (
            getattr(os, "O_PATH", os.O_RDONLY)
            | os.O_DIRECTORY | os.O_CLOEXEC
        )

    @staticmethod
    def _read_at(
        directory_descriptor: int, name: str, maximum: int, label: str,
    ) -> bytes:
        if (
            type(name) is not str or not name or "/" in name
            or type(maximum) is not int or maximum <= 0
        ):
            _fail(label + " descriptor-relative read request differs")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
        try:
            chunks: List[bytes] = []
            size = 0
            while True:
                chunk = os.read(
                    descriptor, min(4096, maximum + 1 - size),
                )
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > maximum:
                    _fail(label + " exceeds its byte bound")
        finally:
            os.close(descriptor)

    @staticmethod
    def _stat_start_time(payload: bytes, label: str) -> int:
        if not payload or len(payload) > MAX_TEXT_BYTES:
            _fail(label + " stat bytes are invalid")
        right = payload.rfind(b")")
        if right < 0:
            _fail(label + " stat comm terminator is absent")
        fields = payload[right + 2:].split()
        if len(fields) < 20 or not fields[19].isdigit():
            _fail(label + " stat start-time field is invalid")
        return _u64(int(fields[19]), label + " start time")

    @staticmethod
    def current_process_id() -> int:
        return os.getpid()

    def hold_process_identity(self, pid: int) -> None:
        if type(pid) is not int or pid <= 0:
            _fail("held process PID is invalid")
        if pid in self._proc_descriptors:
            return
        if len(self._proc_descriptors) >= MAX_TASKS:
            raise PrivilegedBackendIndeterminate(
                "held process-identity descriptor bound is exhausted"
            )
        flags = self._proc_directory_flags()
        descriptor = os.open("/proc/{}".format(pid), flags)
        try:
            status = os.fstat(descriptor)
            # A descriptor-relative stat read proves that the held proc object
            # is live.  Reopening /proc/<pid> here would permit numeric reuse
            # to satisfy the check after the original process exited.
            self._read_at(descriptor, "stat", MAX_TEXT_BYTES, "held process")
        except BaseException:
            os.close(descriptor)
            raise
        self._proc_descriptors[pid] = (
            descriptor, status.st_dev, status.st_ino,
        )

    def process_identity(self, pid: int) -> Mapping[str, int]:
        if type(pid) is not int or pid <= 0:
            _fail("process identity PID is invalid")
        self.hold_process_identity(pid)
        descriptor, held_device, held_inode = self._proc_descriptors[pid]
        held_before = os.fstat(descriptor)
        payload = self._read_at(
            descriptor, "stat", MAX_TEXT_BYTES, "held process stat",
        )
        status_payload = self._read_at(
            descriptor, "status", MAX_TEXT_BYTES, "held process status",
        )
        executable_before = os.stat(
            "exe", dir_fd=descriptor, follow_symlinks=True,
        )
        payload_after = self._read_at(
            descriptor, "stat", MAX_TEXT_BYTES, "held process stat",
        )
        status_payload_after = self._read_at(
            descriptor, "status", MAX_TEXT_BYTES, "held process status",
        )
        executable_after = os.stat(
            "exe", dir_fd=descriptor, follow_symlinks=True,
        )
        held_after = os.fstat(descriptor)
        def parse_process_stat(raw: bytes) -> Tuple[int, int]:
            if not raw or len(raw) > MAX_TEXT_BYTES:
                _fail("process stat bytes are invalid")
            right = raw.rfind(b")")
            if right < 0:
                _fail("process stat comm terminator is absent")
            retained = raw[right + 2:].split()
            if (
                len(retained) < 20 or not retained[1].isdigit()
                or not retained[19].isdigit()
            ):
                _fail("process PPID/start-time fields are invalid")
            return int(retained[1]), int(retained[19])

        parent_pid, start_time_ticks = parse_process_stat(payload)
        following_parent_pid, following_start = parse_process_stat(
            payload_after
        )
        if (
            not payload or len(payload) > MAX_TEXT_BYTES
            or (following_parent_pid, following_start)
            != (parent_pid, start_time_ticks)
            or (
                executable_before.st_dev, executable_before.st_ino,
            ) != (
                executable_after.st_dev, executable_after.st_ino,
            )
            or (held_device, held_inode)
            != (held_before.st_dev, held_before.st_ino)
            or (held_before.st_dev, held_before.st_ino)
            != (held_after.st_dev, held_after.st_ino)
        ):
            _fail("process stat/executable identity changed during capture")
        def parse_credentials(raw: bytes) -> Dict[str, Tuple[int, ...]]:
            result: Dict[str, Tuple[int, ...]] = {}
            for line in raw.decode("ascii", "strict").splitlines():
                pieces = line.split()
                if pieces and pieces[0] in ("Uid:", "Gid:"):
                    if (
                        len(pieces) != 5
                        or any(not item.isdigit() for item in pieces[1:])
                    ):
                        _fail("process credential tuple is malformed")
                    result[pieces[0]] = tuple(
                        int(item) for item in pieces[1:]
                    )
            return result

        credentials = parse_credentials(status_payload)
        following_credentials = parse_credentials(status_payload_after)
        if (
            set(credentials) != {"Uid:", "Gid:"}
            or following_credentials != credentials
            or len(set(credentials["Uid:"])) != 1
            or len(set(credentials["Gid:"])) != 1
        ):
            _fail("process real/effective/saved/fs credentials differ")
        return {
            "executable_device": _u64(
                executable_before.st_dev, "process executable device"
            ),
            "executable_inode": _u64(
                executable_before.st_ino, "process executable inode"
            ),
            "parent_pid": _u64(parent_pid, "process parent PID"),
            "pid": pid,
            "process_gid": _u64(
                credentials["Gid:"][0], "process GID"
            ),
            "process_uid": _u64(
                credentials["Uid:"][0], "process UID"
            ),
            "start_time_ticks": _u64(
                start_time_ticks, "process start time"
            ),
        }

    def hold_task_identity(self, process_id: int, tid: int) -> None:
        if (
            type(process_id) is not int or process_id <= 0
            or type(tid) is not int or tid <= 0
        ):
            _fail("held task identity is invalid")
        key = (process_id, tid)
        if key in self._task_descriptors:
            return
        if len(self._task_descriptors) >= MAX_TASKS:
            raise PrivilegedBackendIndeterminate(
                "held task-identity descriptor bound is exhausted"
            )
        self.hold_process_identity(process_id)
        process_descriptor = self._proc_descriptors[process_id][0]
        descriptor = os.open(
            "task/{}".format(tid), self._proc_directory_flags(),
            dir_fd=process_descriptor,
        )
        try:
            status = os.fstat(descriptor)
            payload = self._read_at(
                descriptor, "stat", MAX_TEXT_BYTES, "held task stat",
            )
            if self._stat_start_time(payload, "held task") < 0:
                _fail("held task start time differs")
        except BaseException:
            os.close(descriptor)
            raise
        self._task_descriptors[key] = (
            descriptor, status.st_dev, status.st_ino,
        )

    def _task_start_identity(self, process_id: int, tid: int) -> int:
        self.hold_task_identity(process_id, tid)
        descriptor, device, inode = self._task_descriptors[(process_id, tid)]
        before = os.fstat(descriptor)
        payload = self._read_at(
            descriptor, "stat", MAX_TEXT_BYTES, "held task stat",
        )
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino) != (device, inode)
            or (after.st_dev, after.st_ino) != (device, inode)
        ):
            _fail("held task directory identity changed")
        return self._stat_start_time(payload, "held task")

    def thread_ids(self, pid: int) -> Tuple[int, ...]:
        self.hold_process_identity(pid)
        process_descriptor = self._proc_descriptors[pid][0]
        before = self.process_identity(pid)
        task_descriptor = os.open(
            "task", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            dir_fd=process_descriptor,
        )
        try:
            names = os.listdir(task_descriptor)
        finally:
            os.close(task_descriptor)
        values = tuple(sorted(int(name) for name in names if name.isdigit()))
        if not values or len(values) > MAX_TASKS:
            _fail("peer thread population is empty or oversized")
        for tid in values:
            self.hold_task_identity(pid, tid)
        if self.process_identity(pid) != before:
            _fail("process identity changed while listing its task population")
        return values

    def affinity(self, tid: int, process_id: Optional[int] = None) -> Tuple[int, ...]:
        owner = tid if process_id is None else process_id
        before = self._task_start_identity(owner, tid)
        value = tuple(sorted(os.sched_getaffinity(tid)))
        if self._task_start_identity(owner, tid) != before:
            _fail("task identity changed around affinity read")
        return value

    def set_affinity(
        self, tid: int, cpus: Sequence[int], process_id: Optional[int] = None,
    ) -> None:
        owner = self.current_process_id() if tid == 0 else (
            tid if process_id is None else process_id
        )
        actual_tid = owner if tid == 0 else tid
        before = self._task_start_identity(owner, actual_tid)
        os.sched_setaffinity(tid, tuple(cpus))
        if self._task_start_identity(owner, actual_tid) != before:
            _fail("task identity changed around affinity write")

    def close_process_identity_descriptors(self) -> None:
        errors = []
        task_values = list(self._task_descriptors.values())
        self._task_descriptors.clear()
        values = list(self._proc_descriptors.values())
        self._proc_descriptors.clear()
        for descriptor, _device, _inode in task_values + values:
            try:
                os.close(descriptor)
            except OSError as exc:
                errors.append(exc)
        if errors:
            raise PrivilegedBackendIndeterminate(
                "held process-identity descriptor closure failed"
            ) from errors[0]

    def start_time_ticks(
        self, tid: int, process_id: Optional[int] = None,
    ) -> int:
        return self._task_start_identity(
            tid if process_id is None else process_id, tid,
        )

    def cpuset_membership(
        self, tid: int, process_id: Optional[int] = None,
    ) -> str:
        owner = tid if process_id is None else process_id
        before = self._task_start_identity(owner, tid)
        descriptor = self._task_descriptors[(owner, tid)][0]
        payload = self._read_at(
            descriptor, "cgroup", MAX_TEXT_BYTES, "held task cgroup",
        )
        if self._task_start_identity(owner, tid) != before:
            _fail("task identity changed around cpuset-membership read")
        if not payload or len(payload) > MAX_TEXT_BYTES:
            _fail("task cgroup bytes are invalid")
        matches = []
        for line in payload.decode("ascii", "strict").splitlines():
            pieces = line.split(":", 2)
            if len(pieces) != 3:
                _fail("task cgroup line is malformed")
            controllers = pieces[1].split(",") if pieces[1] else []
            if "cpuset" in controllers:
                matches.append(pieces[2])
        if len(matches) != 1 or not matches[0].startswith("/"):
            _fail("task cpuset membership is not unique")
        return matches[0]

    @staticmethod
    def path_exists(path: str) -> bool:
        _normalized_absolute(path, "existence path")
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def create_cpuset(path: str) -> None:
        _normalized_absolute(path, "cpuset create path")
        os.mkdir(path, 0o755)

    def cpuset_members(self, tasks_path: str) -> Tuple[int, ...]:
        payload = self.read_raw(tasks_path)
        try:
            lines = payload.decode("ascii", "strict").splitlines()
        except UnicodeDecodeError as exc:
            raise PrivilegedBackendError("cpuset tasks are not ASCII") from exc
        if any(not line.isdigit() for line in lines):
            _fail("cpuset tasks contain a nondecimal member")
        values = tuple(sorted(int(line) for line in lines))
        if len(values) != len(set(values)) or len(values) > MAX_TASKS:
            _fail("cpuset task population is duplicate or oversized")
        return values

    @staticmethod
    def _write_task(tasks_path: str, tid: int) -> None:
        _normalized_absolute(tasks_path, "cpuset tasks path")
        if type(tid) is not int or tid <= 0:
            _fail("cpuset task ID is invalid")
        flags = os.O_WRONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(tasks_path, flags)
        try:
            payload = (str(tid) + "\n").encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                _fail("cpuset membership write was incomplete")
        finally:
            os.close(descriptor)

    def move_to_cpuset(
        self, tasks_path: str, tid: int, process_id: Optional[int] = None,
    ) -> None:
        owner = tid if process_id is None else process_id
        before = self._task_start_identity(owner, tid)
        self._write_task(tasks_path, tid)
        if self._task_start_identity(owner, tid) != before:
            _fail("task identity changed around cpuset membership write")

    def move_to_cpuset_path(
        self, mount_path: str, relative: str, tid: int,
        process_id: Optional[int] = None,
    ) -> None:
        _normalized_absolute(mount_path, "cpuset mount path")
        if (
            type(relative) is not str or not relative.startswith("/")
            or os.path.normpath(relative) != relative
        ):
            _fail("prior cpuset relative path is invalid")
        tasks_path = os.path.normpath(mount_path + relative + "/tasks")
        if not tasks_path.startswith(mount_path.rstrip("/") + "/"):
            _fail("prior cpuset path escapes the hierarchy")
        owner = tid if process_id is None else process_id
        before = self._task_start_identity(owner, tid)
        self._write_task(tasks_path, tid)
        if self._task_start_identity(owner, tid) != before:
            _fail("task identity changed around cpuset restoration write")

    def remove_cpuset(self, group_path: str) -> None:
        _normalized_absolute(group_path, "cpuset removal path")
        if not self.path_exists(group_path):
            return
        tasks_path = group_path + "/tasks"
        if self.cpuset_members(tasks_path):
            _fail("cpuset removal was attempted with retained members")
        os.rmdir(group_path)
        if self.path_exists(group_path):
            _fail("cpuset group remained after removal")

    @staticmethod
    def list_irqs() -> Tuple[int, ...]:
        values = tuple(sorted(
            int(name) for name in os.listdir("/proc/irq") if name.isdigit()
        ))
        if not values or len(values) > MAX_IRQS:
            _fail("numeric IRQ population is empty or oversized")
        return values

    def descendants(self, root_pid: int) -> Tuple[int, ...]:
        if type(root_pid) is not int or root_pid <= 0:
            _fail("descendant root PID is invalid")
        pending = [root_pid]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if len(seen) > MAX_TASKS:
                _fail("descendant population exceeds its bound")
            tids = self.thread_ids(pid)
            for tid in tids:
                self.hold_task_identity(pid, tid)
                task_descriptor = self._task_descriptors[(pid, tid)][0]
                before = self._task_start_identity(pid, tid)
                payload = self._read_at(
                    task_descriptor, "children", MAX_TEXT_BYTES,
                    "held task children",
                )
                if self._task_start_identity(pid, tid) != before:
                    _fail("task identity changed while traversing children")
                if len(payload) > MAX_TEXT_BYTES:
                    _fail("task children surface is oversized")
                tokens = payload.split()
                if any(not token.isdigit() for token in tokens):
                    _fail("task children surface is malformed")
                pending.extend(int(token) for token in tokens)
        return tuple(sorted(seen))

    @staticmethod
    def foreign_affinity_eligibility(
        estimator_cpus: Sequence[int], allowed_pids: Sequence[int],
    ) -> Tuple[Mapping[str, Any], ...]:
        """Observe every stably identified foreign TID eligible on the target.

        Population creation and exit are explicitly non-gating.  A TID that
        vanishes or is reused while its row is sampled is omitted from that
        observation and can appear in a later one.  An inspection-permission
        failure is not silently converted into a complete observation.
        """

        cpus = set(estimator_cpus)
        allowed = set(allowed_pids)
        rows: List[Mapping[str, Any]] = []
        inspected = 0
        for pid_name in os.listdir("/proc"):
            if not pid_name.isdigit() or int(pid_name) in allowed:
                continue
            pid = int(pid_name)
            try:
                tids = tuple(sorted(
                    int(name) for name in os.listdir(
                        "/proc/{}/task".format(pid)
                    ) if name.isdigit()
                ))
                if not tids or len(tids) > MAX_TASKS:
                    continue
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise PrivilegedBackendIndeterminate(
                    "foreign-affinity process inspection was denied"
                ) from exc
            for tid in tids:
                inspected += 1
                if inspected > MAX_TASKS:
                    _fail("foreign-task inspection exceeds its bound")
                try:
                    payload_before = Path(
                        "/proc/{}/stat".format(tid)
                    ).read_bytes()
                    started_before = LinuxRootSystem._stat_start_time(
                        payload_before, "foreign task",
                    )
                    affinity = set(os.sched_getaffinity(tid))
                    payload_after = Path(
                        "/proc/{}/stat".format(tid)
                    ).read_bytes()
                    started_after = LinuxRootSystem._stat_start_time(
                        payload_after, "foreign task",
                    )
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except PermissionError as exc:
                    raise PrivilegedBackendIndeterminate(
                        "foreign-affinity TID inspection was denied"
                    ) from exc
                if started_before != started_after:
                    continue
                if affinity.intersection(cpus):
                    rows.append({
                        "effective_affinity_cpu_ids": sorted(affinity),
                        "process_start_time_ticks": started_before,
                        "tid": tid,
                    })
        rows.sort(key=lambda item: item["tid"])
        if len(rows) != len({item["tid"] for item in rows}):
            _fail("foreign-affinity eligibility observation contains duplicate TIDs")
        return tuple(rows)

    @staticmethod
    def wait_until(deadline_ns: int, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            remaining = deadline_ns - time.monotonic_ns()
            if remaining <= 0:
                return
            stop_event.wait(remaining / 1_000_000_000)

    def open_dma_latency(self, value_us: int) -> None:
        if self._dma_descriptor is not None:
            _fail("DMA latency descriptor is already held")
        descriptor = os.open("/dev/cpu_dma_latency", os.O_RDWR | os.O_CLOEXEC)
        try:
            payload = struct.pack("=i", value_us)
            if os.write(descriptor, payload) != len(payload):
                _fail("DMA latency write was incomplete")
        except BaseException:
            os.close(descriptor)
            raise
        self._dma_descriptor = descriptor

    def dma_latency_held(self) -> bool:
        return self._dma_descriptor is not None

    def close_dma_latency(self) -> None:
        if self._dma_descriptor is not None:
            os.close(self._dma_descriptor)
            self._dma_descriptor = None

    def read_msr(self, path: str, register: int) -> bytes:
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISCHR(status.st_mode):
                _fail("MSR path is not character-special")
            payload = os.pread(descriptor, 8, register)
            if len(payload) != 8:
                _fail("MSR pread did not return eight bytes")
            return payload
        finally:
            os.close(descriptor)


class PrivilegedTimingBackend:
    """Real schema-v2 backend behind the still-disabled root entry point."""

    def __init__(
        self, profile: profile_codec.FrozenTimingProfile,
        expected_peer: protocol.PeerCredentials,
        system: Optional[Any] = None,
    ) -> None:
        if type(profile) is not profile_codec.FrozenTimingProfile:
            _fail("privileged backend requires FrozenTimingProfile")
        if type(expected_peer) is not protocol.PeerCredentials:
            _fail("privileged backend requires exact peer credentials")
        rebound = profile_codec.load_profile_bytes(profile.canonical_bytes)
        if rebound.sha256 != profile.sha256:
            _fail("privileged profile identity changed")
        self.frozen_profile = rebound
        self.profile = rebound.value
        self.expected_peer = expected_peer
        self.system = LinuxRootSystem() if system is None else system
        self.controls = derive_typed_controls(rebound)
        require_legacy_write_closure(rebound, self.controls)
        self.prior: Optional[Mapping[str, Any]] = None
        self._population_active = False
        self._population_session_id: Optional[str] = None
        self._append_observation: Optional[Callable[[Mapping[str, Any]], None]] = None
        self._guard_stop = threading.Event()
        self._guard_thread: Optional[threading.Thread] = None
        self._guard_error: Optional[BaseException] = None
        self._guard_first_ns: Optional[int] = None
        self._guard_last_ns: Optional[int] = None
        self._guard_samples = 0
        self._irq_sha256: Optional[str] = None
        self._control_plane_processes: Tuple[Mapping[str, Any], ...] = ()
        self._peer_start_time_ticks: Optional[int] = None
        self._last_descendants: Tuple[int, ...] = ()
        self._last_descendant_tids: Tuple[int, ...] = ()
        self._last_descendant_threads: Tuple[Mapping[str, Any], ...] = ()
        self._last_control_plane_threads: Tuple[Mapping[str, Any], ...] = ()
        self._last_cpuset_members: Tuple[int, ...] = ()
        self._last_foreign_affinity_eligibility: Tuple[Mapping[str, Any], ...] = ()
        self._last_scheduler_witness_sha256: Optional[str] = None
        self._external_recovery_prior_peer: Optional[
            protocol.PeerCredentials
        ] = None
        self._external_recovery_initially_live: Optional[set] = None
        self._external_recovery_initially_absent: Optional[set] = None
        self._closed = False

    def _expected_read_only_values(self) -> Mapping[str, str]:
        result: Dict[str, str] = {}
        for policy in self.profile["cpu_controls"]["policies"]:
            result[
                "cpufreq." + policy["policy_id"] + ".driver"
            ] = policy["driver"]
        for cpu in self.profile["cpu_controls"]["cpus"]:
            for idle in cpu["idle_states"]:
                result[
                    "cpu.{}.idle.{}.name".format(
                        cpu["cpu_id"], idle["state_index"],
                    )
                ] = idle["name"]
        return result

    def _identity(
        self, suffix: str, paths: Sequence[Tuple[str, bool]],
        structural: Mapping[str, Any],
    ) -> str:
        return self.system.source_identity_sha256(
            "SchurVIO-CP2-E-" + suffix + "-source-identity-v1",
            tuple(paths), structural,
        )

    def observed_control_source_identities(self) -> Mapping[str, str]:
        """Read-only reconstruction of every pre-module profile identity."""

        value = self.profile
        result: Dict[str, str] = {}
        controlled = tuple(value["cpu_plan"]["controlled_cpu_ids"])
        possible_path = "/sys/devices/system/cpu/possible"
        present_path = "/sys/devices/system/cpu/present"
        possible = _parse_cpu_list(
            self.system.read_text(possible_path, "cpu_list"), "possible CPUs",
        )
        present = _parse_cpu_list(
            self.system.read_text(present_path, "cpu_list"), "present CPUs",
        )
        if possible != controlled or present != controlled:
            _fail("possible/present CPU topology differs from the complete profile")
        topology_paths: List[Tuple[str, bool]] = [
            (possible_path, False), (present_path, False),
            ("/sys/devices/system/cpu/online", False),
        ]
        topology_rows = []
        core_by_cpu = {
            cpu: core["physical_core_id"]
            for core in value["cpu_plan"]["smt_cores"]
            for cpu in core["sibling_cpu_ids"]
        }
        siblings_by_cpu = {
            cpu: tuple(core["sibling_cpu_ids"])
            for core in value["cpu_plan"]["smt_cores"]
            for cpu in core["sibling_cpu_ids"]
        }
        for cpu in controlled:
            root = "/sys/devices/system/cpu/cpu{}/topology".format(cpu)
            core_path = root + "/core_id"
            siblings_path = root + "/thread_siblings_list"
            core_id = int(self.system.read_text(core_path, "integer"))
            siblings = _parse_cpu_list(
                self.system.read_text(siblings_path, "cpu_list"),
                "CPU thread siblings",
            )
            if core_id != core_by_cpu[cpu] or siblings != siblings_by_cpu[cpu]:
                _fail("runtime CPU core/SMT topology differs from the profile")
            topology_paths.extend(((core_path, False), (siblings_path, False)))
            topology_rows.append({
                "core_id": core_id, "cpu_id": cpu,
                "thread_sibling_cpu_ids": list(siblings),
            })
        result["cpu_plan.topology_source_sha256"] = self._identity(
            "CPU-topology", topology_paths,
            {"controlled_cpu_ids": list(controlled), "rows": topology_rows},
        )
        cpuset = value["cpuset"]
        mountinfo = self.system.cpuset_mountinfo_line(cpuset["mount_path"])
        result["cpuset.hierarchy_identity_sha256"] = self._identity(
            "cpuset-hierarchy", ((cpuset["mount_path"], False),),
            {
                "cgroup_version": cpuset["cgroup_version"],
                "mount_path": cpuset["mount_path"],
                "mountinfo_line": mountinfo,
                "parent_path": cpuset["parent_path"],
            },
        )
        boost = value["cpu_controls"]["boost"]
        result["cpu_controls.boost.source_identity_sha256"] = self._identity(
            "boost", ((boost["path"], False),),
            {"parser": boost["parser"]},
        )
        for policy in value["cpu_controls"]["policies"]:
            paths = tuple(
                (policy[key], False) for key in (
                    "driver_path", "governor_path", "minimum_frequency_path",
                    "maximum_frequency_path",
                )
            )
            result[
                "cpu_controls.policy.{}.source_identity_sha256".format(
                    policy["policy_id"]
                )
            ] = self._identity(
                "cpufreq-" + policy["policy_id"], paths,
                {
                    "cpu_ids": policy["cpu_ids"], "driver": policy["driver"],
                    "policy_id": policy["policy_id"],
                },
            )
        for cpu in value["cpu_controls"]["cpus"]:
            cpu_id = cpu["cpu_id"]
            online_path = cpu["online_path"]
            identity_path = (
                "/sys/devices/system/cpu/cpu{}/online".format(cpu_id)
                if online_path is None else online_path
            )
            result[
                "cpu_controls.cpu.{}.online_source_identity_sha256".format(cpu_id)
            ] = self._identity(
                "cpu{}-online".format(cpu_id),
                ((identity_path, online_path is None),),
                {"control_kind": cpu["online_control_kind"], "cpu_id": cpu_id},
            )
            idle_paths = []
            idle_rows = []
            for idle in cpu["idle_states"]:
                idle_paths.extend((
                    (idle["name_path"], False), (idle["disable_path"], False),
                ))
                observed_name = self.system.read_text(idle["name_path"], "text")
                if observed_name != idle["name"]:
                    _fail("runtime CPU-idle name differs from the profile")
                idle_rows.append({
                    "name": observed_name, "state_index": idle["state_index"],
                })
            result[
                "cpu_controls.cpu.{}.idle_inventory_sha256".format(cpu_id)
            ] = self._identity(
                "cpu{}-idle-inventory".format(cpu_id), tuple(idle_paths),
                {"cpu_id": cpu_id, "states": idle_rows},
            )
        interrupts = value["interrupts"]
        result["interrupts.default_source_identity_sha256"] = self._identity(
            "IRQ-default-affinity",
            ((interrupts["default_affinity_path"], False),), {},
        )
        for irq in interrupts["irq_records"]:
            result[
                "interrupts.irq.{}.source_identity_sha256".format(irq["irq"])
            ] = self._identity(
                "IRQ{}-affinity".format(irq["irq"]),
                ((irq["affinity_path"], False),), {"irq": irq["irq"]},
            )
        return dict(sorted(result.items()))

    def _expected_control_source_identities(self) -> Mapping[str, str]:
        value = self.profile
        result = {
            "cpu_plan.topology_source_sha256": value["cpu_plan"][
                "topology_source_sha256"
            ],
            "cpuset.hierarchy_identity_sha256": value["cpuset"][
                "hierarchy_identity_sha256"
            ],
            "cpu_controls.boost.source_identity_sha256": value[
                "cpu_controls"
            ]["boost"]["source_identity_sha256"],
            "interrupts.default_source_identity_sha256": value[
                "interrupts"
            ]["default_source_identity_sha256"],
        }
        for policy in value["cpu_controls"]["policies"]:
            result[
                "cpu_controls.policy.{}.source_identity_sha256".format(
                    policy["policy_id"]
                )
            ] = policy["source_identity_sha256"]
        for cpu in value["cpu_controls"]["cpus"]:
            prefix = "cpu_controls.cpu.{}.".format(cpu["cpu_id"])
            result[prefix + "online_source_identity_sha256"] = cpu[
                "online_source_identity_sha256"
            ]
            result[prefix + "idle_inventory_sha256"] = cpu[
                "idle_inventory_sha256"
            ]
        for irq in value["interrupts"]["irq_records"]:
            result[
                "interrupts.irq.{}.source_identity_sha256".format(irq["irq"])
            ] = irq["source_identity_sha256"]
        return dict(sorted(result.items()))

    def _verify_control_source_identities(self) -> None:
        if (
            self.observed_control_source_identities()
            != self._expected_control_source_identities()
        ):
            _fail("runtime control/topology source identities differ from the profile")

    def observed_telemetry_source_identities(self) -> Mapping[str, str]:
        value = self.profile["telemetry_plan"]
        result: Dict[str, str] = {}
        counters = value["aperf_mperf"]
        for source in counters["cpu_sources"]:
            result[
                "telemetry.aperf_mperf.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = self._identity(
                "cpu{}-APERF-MPERF".format(source["cpu_id"]),
                ((source["path"], False),),
                {
                    "aperf_register": source["aperf_register"],
                    "cpu_id": source["cpu_id"],
                    "mperf_register": source["mperf_register"],
                    "source_kind": source["source_kind"],
                },
            )
        reference = counters["reference_frequency_source"]
        result["telemetry.reference.source_identity_sha256"] = self._identity(
            "MPERF-reference-frequency", ((reference["path"], False),),
            {"source_kind": reference["source_kind"]},
        )
        for source in value["frequency_sources"]:
            result[
                "telemetry.frequency.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = self._identity(
                "cpu{}-current-frequency".format(source["cpu_id"]),
                ((source["path"], False),),
                {"cpu_id": source["cpu_id"], "source_kind": source["source_kind"]},
            )
        temperature = value["temperature_source"]
        observed_name = self.system.read_text(temperature["name_path"], "text")
        observed_label = self.system.read_text(temperature["label_path"], "text")
        if (
            observed_name != temperature["name_expected"]
            or observed_label != temperature["label_expected"]
        ):
            _fail("k10temp name/label differs from the frozen source")
        result["telemetry.temperature.source_identity_sha256"] = self._identity(
            "k10temp-temperature",
            tuple((temperature[key], False) for key in (
                "device_path", "name_path", "label_path", "input_path",
            )),
            {"label": observed_label, "name": observed_name},
        )
        for source in value["throttle_sources"]:
            result[
                "telemetry.throttle.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = self._identity(
                "cpu{}-AMD-throttle".format(source["cpu_id"]),
                ((source["path"], False),),
                {
                    "cpu_id": source["cpu_id"], "provider": source["provider"],
                    "register": source["register"],
                    "source_kind": source["source_kind"],
                    "value_extraction": source["value_extraction"],
                },
            )
        return dict(sorted(result.items()))

    def _expected_telemetry_source_identities(self) -> Mapping[str, str]:
        value = self.profile["telemetry_plan"]
        result = {}
        for source in value["aperf_mperf"]["cpu_sources"]:
            result[
                "telemetry.aperf_mperf.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = source["source_identity_sha256"]
        result["telemetry.reference.source_identity_sha256"] = value[
            "aperf_mperf"
        ]["reference_frequency_source"]["source_identity_sha256"]
        for source in value["frequency_sources"]:
            result[
                "telemetry.frequency.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = source["source_identity_sha256"]
        result["telemetry.temperature.source_identity_sha256"] = value[
            "temperature_source"
        ]["source_identity_sha256"]
        for source in value["throttle_sources"]:
            result[
                "telemetry.throttle.cpu{}.source_identity_sha256".format(
                    source["cpu_id"]
                )
            ] = source["source_identity_sha256"]
        return dict(sorted(result.items()))

    def _verify_telemetry_source_identities(self) -> None:
        if (
            self.observed_telemetry_source_identities()
            != self._expected_telemetry_source_identities()
        ):
            _fail("runtime telemetry source identities differ from the profile")

    def _validate_durable_prior(
        self, value: Any, *, external_recovery: bool = False,
    ) -> Mapping[str, Any]:
        """Validate every recovery-driving field before any root mutation.

        A predecessor may die after making an arbitrary prefix of the plan.
        Therefore a replacement helper must reject a malformed or
        profile-mismatched journal state *before* it uses any retained value as
        a restoration target.
        """

        prior = _exact_mapping(value, PRIOR_STATE_KEYS, "durable prior state")
        if (
            prior["record_type"] != "cp2_timing_control_prior_state"
            or prior["schema_version"] != CONTROL_STATE_SCHEMA_VERSION
        ):
            _fail("durable prior state type/version differs")
        module_names = tuple(
            item["name"] for item in self.profile["controls"]["module_actions"]
        )
        service_names = tuple(
            item["name"] for item in self.profile["controls"]["service_actions"]
        )
        _string_population(
            prior["modules_preexisting"], "durable prior module population",
            module_names,
        )
        _string_population(
            prior["services_active"], "durable prior service population",
            service_names,
        )
        allowed_cpus = tuple(self.profile["cpu_plan"]["controlled_cpu_ids"])
        process_affinity = _cpu_population(
            prior["process_affinity"], "durable prior process affinity",
            allowed_cpus,
        )

        rows = prior["values"]
        if type(rows) is not list or len(rows) != len(self.controls):
            _fail("durable prior typed-control population differs")
        read_only_expected = self._expected_read_only_values()
        for control, raw in zip(self.controls, rows):
            row = _exact_mapping(
                raw, TYPED_VALUE_KEYS, "durable prior typed-control row",
            )
            if (
                row["control_id"] != control.control_id
                or row["desired_text"] != control.desired_text
                or row["parser"] != control.parser
                or row["path"] != control.path
                or row["writable"] is not control.writable
                or type(row["text"]) is not str
            ):
                _fail("durable prior typed-control binding differs")
            _canonical_text(
                (row["text"] + "\n").encode("ascii", "strict"),
                control.parser, "durable prior typed-control text",
            )
            if (
                not control.writable
                and row["text"] != read_only_expected[control.control_id]
            ):
                _fail("durable prior read-only typed value differs from the profile")

        extension = _exact_mapping(
            prior["privileged_v2"], PRIVILEGED_EXTENSION_KEYS,
            "durable prior privileged-v2 extension",
        )
        if (
            extension["record_type"] != "cp2e_privileged_prior_extension"
            or extension["schema_version"] != CONTROL_STATE_SCHEMA_VERSION
            or extension["profile_sha256"] != self.frozen_profile.sha256
            or extension["dma_latency_held_by_helper"] is not False
        ):
            _fail("durable prior privileged-v2 type/profile/state differs")
        cpuset = _exact_mapping(
            extension["cpuset"], CPUSET_STATE_KEYS, "durable prior cpuset state",
        )
        if cpuset != {
            "cpus": None, "effective_cpus": None, "effective_mems": None,
            "exists": False,
            "group_path": self.profile["cpuset"]["group_path"],
            "members": [], "mems": None,
        }:
            _fail("durable prior does not prove exact cpuset absence")
        helper_affinity = _cpu_population(
            extension["helper_affinity_cpu_ids"],
            "durable prior helper affinity", allowed_cpus,
        )
        irqs = extension["irq_numbers"]
        expected_irqs = [
            item["irq"] for item in self.profile["interrupts"]["irq_records"]
        ]
        if (
            type(irqs) is not list or any(type(item) is not int for item in irqs)
            or irqs != expected_irqs
            or extension["irq_population_sha256"]
            != self.profile["interrupts"]["inventory_sha256"]
        ):
            _fail("durable prior IRQ identity differs from the profile")
        try:
            prior_peer = protocol.PeerCredentials.from_record(
                extension["peer"]
            )
        except protocol.PrivilegedHelperError as exc:
            raise PrivilegedBackendError(str(exc)) from exc
        if (
            not external_recovery
            and prior_peer != self.expected_peer
        ):
            _fail("durable prior peer credential binding differs")
        peer_start = _u64(
            extension["peer_start_time_ticks"], "durable prior peer start time",
        )
        process_ids = extension["descendant_process_ids"]
        if process_ids != [prior_peer.pid]:
            _fail("durable prior estimator closure is not the lone peer")
        thread_rows = _scheduler_thread_rows(
            extension["descendant_threads"], process_ids, allowed_cpus,
            "durable prior estimator threads",
        )
        control_processes = _control_process_rows(
            extension["control_plane_processes"],
            "durable prior control-plane processes",
        )
        control_pids = tuple(item["pid"] for item in control_processes)
        expected_parent = prior_peer.pid
        for row in control_processes:
            if row["parent_pid"] != expected_parent:
                _fail("durable prior control-plane PPID chain differs")
            expected_parent = row["pid"]
        control_threads = _scheduler_thread_rows(
            extension["control_plane_threads"], control_pids, allowed_cpus,
            "durable prior control-plane threads",
        )
        estimator_tids = {item["tid"] for item in thread_rows}
        control_tids = {item["tid"] for item in control_threads}
        if estimator_tids.intersection(control_tids):
            _fail("durable prior estimator/control TID partitions overlap")
        main_row = next(
            item for item in thread_rows
            if item["tid"] == prior_peer.pid
        )
        helper_main = next(
            (item for item in control_threads if item["tid"] == control_pids[-1]),
            None,
        )
        process_starts = {
            item["pid"]: item["start_time_ticks"] for item in control_processes
        }
        if any(
            next(
                item for item in control_threads if item["tid"] == process_id
            )["start_time_ticks"] != start
            for process_id, start in process_starts.items()
        ):
            _fail("durable prior control process/thread identity join differs")
        if (
            tuple(main_row["affinity_cpu_ids"]) != process_affinity
            or main_row["start_time_ticks"] != peer_start
            or helper_main is None
            or tuple(helper_main["affinity_cpu_ids"]) != helper_affinity
        ):
            _fail("durable prior main-thread/helper identity differs")
        payload = _canonical(prior)
        if len(payload) > protocol.MAX_CONTROL_STATE_BYTES:
            _fail("durable prior state exceeds the control-evidence bound")
        if not external_recovery:
            if self._control_plane_processes:
                if self._control_plane_processes != control_processes:
                    _fail("durable prior control-plane identity changed")
            else:
                self._control_plane_processes = control_processes
            if self._peer_start_time_ticks is None:
                self._peer_start_time_ticks = peer_start
            elif self._peer_start_time_ticks != peer_start:
                _fail("durable prior peer start identity changed")
        return prior

    def prepare_external_recovery(
        self, prior: Mapping[str, Any], journal_peer: protocol.PeerCredentials,
    ) -> Mapping[str, Any]:
        """Authenticate a replacement controller without rebinding old tasks.

        The journal's P1 peer and task identities remain restoration targets;
        the live P2 SO_PEERCRED/PPID chain is independently attested and kept
        disjoint.  No host mutation occurs in this preflight.
        """

        if type(journal_peer) is not protocol.PeerCredentials:
            _fail("external recovery journal peer identity differs")
        self.system.require_root()
        validated = self._validate_durable_prior(
            prior, external_recovery=True,
        )
        prior_extension = validated["privileged_v2"]
        if prior_extension["peer"] != journal_peer.as_record():
            _fail("external recovery journal/prior peer binding differs")
        self._verify_control_source_identities()
        self._irq_identity()
        replacement_chain = self._attest_initial_control_plane()
        replacement_pids = {item["pid"] for item in replacement_chain}
        prior_pids = set(prior_extension["descendant_process_ids"])
        prior_pids.update(
            item["pid"] for item in prior_extension["control_plane_processes"]
        )
        if (
            self.expected_peer == journal_peer
            or self.expected_peer.pid in prior_pids
            or replacement_pids.intersection(prior_pids)
        ):
            raise PrivilegedBackendIndeterminate(
                "replacement controller overlaps predecessor task identities"
            )
        self._control_plane_processes = replacement_chain
        self._peer_start_time_ticks = self.system.start_time_ticks(
            self.expected_peer.pid, self.expected_peer.pid,
        )
        _replacement_processes, replacement_threads, chain, chain_threads = (
            self._stable_descendant_state()
        )
        prior_tids = {
            item["tid"] for item in (
                list(prior_extension["descendant_threads"])
                + list(prior_extension["control_plane_threads"])
            )
        }
        replacement_tids = {
            item["tid"] for item in replacement_threads + chain_threads
        }
        if chain != replacement_chain or replacement_tids.intersection(prior_tids):
            raise PrivilegedBackendIndeterminate(
                "replacement controller TIDs overlap predecessor targets"
            )
        self._external_recovery_prior_peer = journal_peer
        (
            _live_estimator, _live_control, absent_process_ids,
        ) = self._external_recovery_task_rows(
            validated, require_restored_state=False,
        )
        prior_process_ids = set(prior_extension["descendant_process_ids"])
        prior_process_ids.update(
            item["pid"] for item in prior_extension["control_plane_processes"]
        )
        self._external_recovery_initially_absent = set(absent_process_ids)
        self._external_recovery_initially_live = (
            prior_process_ids - set(absent_process_ids)
        )
        return {
            "journal_peer": journal_peer.as_record(),
            "replacement_control_plane_processes": [
                dict(item) for item in replacement_chain
            ],
            "replacement_peer": self.expected_peer.as_record(),
        }

    def _module_command(self, action: Mapping[str, Any], remove: bool) -> Tuple[str, ...]:
        argv = tuple(action["remove_argv"] if remove else action["modprobe_argv"])
        if argv[:2] != ("/usr/bin/sudo", "-n"):
            _fail("profile module command lacks exact sudo prefix")
        return argv[2:]

    def _service_command(self, action: Mapping[str, Any], activate: bool) -> Tuple[str, ...]:
        argv = tuple(action["activate_argv"] if activate else action["deactivate_argv"])
        if argv[:2] != ("/usr/bin/sudo", "-n"):
            _fail("profile service command lacks exact sudo prefix")
        return argv[2:]

    def _control_plane_chain_once(
        self,
    ) -> Optional[Tuple[Mapping[str, Any], ...]]:
        """Walk helper self to SO_PEERCRED through exact kernel identities."""

        try:
            helper_pid = self.system.current_process_id()
            if helper_pid == self.expected_peer.pid:
                return None
            self.system.hold_process_identity(self.expected_peer.pid)
            reverse_rows: List[Mapping[str, Any]] = []
            seen = set()
            current = helper_pid
            while current != self.expected_peer.pid:
                if current <= 0 or current in seen or len(seen) >= MAX_TASKS:
                    return None
                seen.add(current)
                row = dict(self.system.process_identity(current))
                if (
                    set(row) != set(CONTROL_PLANE_PROCESS_KEYS)
                    or row.get("pid") != current
                    or type(row.get("parent_pid")) is not int
                    or row["parent_pid"] <= 0
                ):
                    return None
                for key in (
                    "executable_device", "executable_inode", "parent_pid",
                    "pid", "process_gid", "process_uid", "start_time_ticks",
                ):
                    _u64(row[key], "control-plane " + key)
                reverse_rows.append(row)
                current = row["parent_pid"]
            rows = tuple(reversed(reverse_rows))
            if not rows or rows[0]["parent_pid"] != self.expected_peer.pid:
                return None
            expected_parent = self.expected_peer.pid
            for row in rows:
                if row["parent_pid"] != expected_parent:
                    return None
                expected_parent = row["pid"]
            if rows[-1]["pid"] != helper_pid:
                return None
            raw = tuple(self.system.descendants(self.expected_peer.pid))
            chain_pids = tuple(row["pid"] for row in rows)
            if (
                raw != tuple(sorted((self.expected_peer.pid,) + chain_pids))
                or tuple(self.system.descendants(chain_pids[0]))
                != tuple(sorted(chain_pids))
            ):
                return None
            peer_start = self.system.start_time_ticks(self.expected_peer.pid)
            if (
                tuple(dict(self.system.process_identity(row["pid"])) for row in rows)
                != rows
                or self.system.start_time_ticks(self.expected_peer.pid)
                != peer_start
            ):
                return None
            return rows
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return None

    def _attest_initial_control_plane(
        self,
    ) -> Tuple[Mapping[str, Any], ...]:
        for _attempt in range(8):
            rows = self._control_plane_chain_once()
            if rows is not None:
                return rows
        raise PrivilegedBackendIndeterminate(
            "control-plane PPID/start/executable chain did not stabilize"
        )

    def _thread_rows(
        self, process_ids: Sequence[int],
    ) -> Optional[Tuple[Mapping[str, Any], ...]]:
        rows: List[Mapping[str, Any]] = []
        try:
            for process_id in process_ids:
                tids = tuple(self.system.thread_ids(process_id))
                if process_id not in tids:
                    return None
                for tid in tids:
                    started_before = self.system.start_time_ticks(
                        tid, process_id,
                    )
                    row = {
                        "affinity_cpu_ids": list(
                            self.system.affinity(tid, process_id)
                        ),
                        "cpuset_membership": self.system.cpuset_membership(
                            tid, process_id,
                        ),
                        "process_id": process_id,
                        "start_time_ticks": started_before,
                        "tid": tid,
                    }
                    if self.system.start_time_ticks(
                        tid, process_id,
                    ) != started_before:
                        return None
                    rows.append(row)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            return None
        rows.sort(key=lambda item: item["tid"])
        tids = tuple(item["tid"] for item in rows)
        if (
            not rows or len(rows) > MAX_TASKS
            or tids != tuple(sorted(set(tids)))
            or not set(process_ids).issubset(set(tids))
            or any(item["process_id"] not in process_ids for item in rows)
        ):
            return None
        return tuple(rows)

    def _descendant_state_once(
        self,
    ) -> Optional[Tuple[
        Tuple[int, ...], Tuple[Mapping[str, Any], ...],
        Tuple[Mapping[str, Any], ...], Tuple[Mapping[str, Any], ...],
    ]]:
        """Capture stable estimator and attested control-plane closures."""

        if not self._control_plane_processes:
            return None
        chain_pids = tuple(
            item["pid"] for item in self._control_plane_processes
        )

        def capture() -> Optional[Tuple[
            Tuple[int, ...], Tuple[Mapping[str, Any], ...],
            Tuple[Mapping[str, Any], ...], Tuple[Mapping[str, Any], ...],
        ]]:
            try:
                self.system.hold_process_identity(self.expected_peer.pid)
                current_chain = tuple(
                    dict(self.system.process_identity(pid)) for pid in chain_pids
                )
                raw = tuple(self.system.descendants(self.expected_peer.pid))
                chain_subtree = tuple(self.system.descendants(chain_pids[0]))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                return None
            if (
                current_chain != self._control_plane_processes
                or chain_subtree != tuple(sorted(chain_pids))
                or raw != tuple(sorted(set(raw)))
                or self.expected_peer.pid not in raw
                or not set(chain_pids).issubset(set(raw))
                or len(raw) > MAX_TASKS
            ):
                return None
            estimator = tuple(pid for pid in raw if pid not in set(chain_pids))
            if (
                estimator != tuple(sorted(set(estimator)))
                or not estimator or self.expected_peer.pid not in estimator
            ):
                return None
            estimator_threads = self._thread_rows(estimator)
            control_threads = self._thread_rows(chain_pids)
            if estimator_threads is None or control_threads is None:
                return None
            peer_main = next(
                (
                    item for item in estimator_threads
                    if item["tid"] == self.expected_peer.pid
                    and item["process_id"] == self.expected_peer.pid
                ),
                None,
            )
            if (
                peer_main is None
                or (
                    self._peer_start_time_ticks is not None
                    and peer_main["start_time_ticks"]
                    != self._peer_start_time_ticks
                )
            ):
                return None
            return (
                estimator, estimator_threads, current_chain, control_threads,
            )

        first = capture()
        following = capture()
        if first is None or following is None or first != following:
            return None
        return first

    def _stable_descendant_state(
        self,
    ) -> Tuple[
        Tuple[int, ...], Tuple[Mapping[str, Any], ...],
        Tuple[Mapping[str, Any], ...], Tuple[Mapping[str, Any], ...],
    ]:
        for _attempt in range(8):
            captured = self._descendant_state_once()
            if captured is not None:
                return captured
        raise PrivilegedBackendIndeterminate(
            "descendant PID/TID closure did not stabilize"
        )

    def _typed_values(self) -> Tuple[Mapping[str, Any], ...]:
        return tuple(
            control.observed_record(
                self.system.read_text(control.path, control.parser)
            ) for control in self.controls
        )

    def _irq_identity(self) -> Tuple[Tuple[int, ...], str]:
        values = tuple(self.system.list_irqs())
        expected = tuple(item["irq"] for item in self.profile["interrupts"]["irq_records"])
        if values != expected:
            _fail("runtime numeric IRQ population differs from the frozen complete inventory")
        record = {
            "domain": "SchurVIO-CP2-E-numeric-IRQ-population-v1",
            "irq_numbers": list(values),
        }
        digest = _sha_record(record)
        if digest != self.profile["interrupts"]["inventory_sha256"]:
            _fail("runtime IRQ population digest differs from the profile")
        return values, digest

    def _cpuset_state(self, *, expected_exists: bool) -> Mapping[str, Any]:
        cpuset = self.profile["cpuset"]
        exists = bool(self.system.path_exists(cpuset["group_path"]))
        if exists != expected_exists:
            _fail("cpuset group existence differs")
        if not exists:
            return {
                "cpus": None, "effective_cpus": None, "effective_mems": None,
                "exists": False, "group_path": cpuset["group_path"],
                "members": [], "mems": None,
            }
        members = tuple(self.system.cpuset_members(cpuset["tasks_path"]))
        return {
            "cpus": self.system.read_text(cpuset["cpus_path"], "cpu_list"),
            "effective_cpus": self.system.read_text(
                cpuset["effective_cpus_path"], "cpu_list",
            ),
            "effective_mems": self.system.read_text(
                cpuset["effective_mems_path"], "cpu_list",
            ),
            "exists": True, "group_path": cpuset["group_path"],
            "members": list(members),
            "mems": self.system.read_text(cpuset["mems_path"], "cpu_list"),
        }

    def _prior_record(self) -> Mapping[str, Any]:
        irqs, irq_sha = self._irq_identity()
        modules = sorted(
            item["name"] for item in self.profile["controls"]["module_actions"]
            if self.system.module_loaded(item["name"])
        )
        services = sorted(
            item["name"] for item in self.profile["controls"]["service_actions"]
            if self.system.service_active(item["name"])
        )
        if not self._control_plane_processes:
            self._control_plane_processes = self._attest_initial_control_plane()
        process_ids, threads, control_processes, control_threads = (
            self._stable_descendant_state()
        )
        if process_ids != (self.expected_peer.pid,):
            _fail(
                "durable prior raw peer closure contains a non-control child"
            )
        helper_pid = self.system.current_process_id()
        helper_main = next(
            (item for item in control_threads if item["tid"] == helper_pid),
            None,
        )
        peer_main = next(
            (item for item in threads if item["tid"] == self.expected_peer.pid),
            None,
        )
        if (
            control_processes != self._control_plane_processes
            or helper_main is None or peer_main is None
        ):
            _fail("durable prior control/peer identity population differs")
        if self._peer_start_time_ticks is None:
            self._peer_start_time_ticks = peer_main["start_time_ticks"]
        elif self._peer_start_time_ticks != peer_main["start_time_ticks"]:
            _fail("durable prior peer start identity changed")
        return {
            "modules_preexisting": modules,
            "process_affinity": list(peer_main["affinity_cpu_ids"]),
            "record_type": "cp2_timing_control_prior_state",
            "schema_version": CONTROL_STATE_SCHEMA_VERSION,
            "services_active": services,
            "values": [dict(item) for item in self._typed_values()],
            "privileged_v2": {
                "control_plane_processes": [
                    dict(item) for item in control_processes
                ],
                "control_plane_threads": [dict(item) for item in control_threads],
                "cpuset": self._cpuset_state(expected_exists=False),
                "descendant_process_ids": list(process_ids),
                "descendant_threads": [dict(item) for item in threads],
                "dma_latency_held_by_helper": False,
                "helper_affinity_cpu_ids": list(
                    helper_main["affinity_cpu_ids"]
                ),
                "irq_numbers": list(irqs),
                "irq_population_sha256": irq_sha,
                "peer": self.expected_peer.as_record(),
                "peer_start_time_ticks": peer_main["start_time_ticks"],
                "profile_sha256": self.frozen_profile.sha256,
                "record_type": "cp2e_privileged_prior_extension",
                "schema_version": CONTROL_STATE_SCHEMA_VERSION,
            },
        }

    def capture_prior(self) -> Mapping[str, Any]:
        self.system.require_root()
        if self.prior is not None or self._population_active:
            _fail("privileged prior was captured twice")
        self._verify_control_source_identities()
        value = self._prior_record()
        self._validate_durable_prior(value)
        self.prior = value
        return json.loads(_canonical(value).decode("utf-8"))

    @staticmethod
    def _step(
        boundary: Callable[[str], None], label: str,
        operation: Callable[[], None],
    ) -> None:
        boundary("before_" + label)
        operation()
        boundary("after_" + label)

    def _write_and_verify(self, control: TypedControl, text: str) -> None:
        self.system.write_text(control.path, text)
        if self.system.read_text(control.path, control.parser) != text:
            _fail("typed control did not assume its exact requested text")

    def _set_control_plane_affinity(self) -> None:
        _processes, _threads, control_processes, control_threads = (
            self._stable_descendant_state()
        )
        if control_processes != self._control_plane_processes:
            _fail("control-plane identity changed before affinity placement")
        helper_cpus = self.profile["cpu_plan"]["helper_cpu_ids"]
        for item in control_threads:
            self.system.set_affinity(
                item["tid"], helper_cpus, item["process_id"],
            )
        _pids, _estimator, following_processes, following_threads = (
            self._stable_descendant_state()
        )
        if (
            following_processes != control_processes
            or tuple(
                (item["process_id"], item["tid"], item["start_time_ticks"])
                for item in following_threads
            ) != tuple(
                (item["process_id"], item["tid"], item["start_time_ticks"])
                for item in control_threads
            )
            or any(item["affinity_cpu_ids"] != helper_cpus for item in following_threads)
        ):
            _fail("control-plane TID identity/affinity placement differs")

    def _create_cpuset(self) -> None:
        cpuset = self.profile["cpuset"]
        self.system.create_cpuset(cpuset["group_path"])
        self.system.write_text(
            cpuset["mems_path"], _cpu_list_text(cpuset["desired_memory_nodes"]),
        )
        self.system.write_text(
            cpuset["cpus_path"], _cpu_list_text(cpuset["desired_cpu_ids"]),
        )
        if (
            self.system.read_text(cpuset["effective_cpus_path"], "cpu_list")
            != _cpu_list_text(cpuset["desired_cpu_ids"])
            or self.system.read_text(cpuset["effective_mems_path"], "cpu_list")
            != _cpu_list_text(cpuset["desired_memory_nodes"])
        ):
            _fail("cpuset effective masks differ")
        stable = None
        expected_membership = "/" + os.path.relpath(
            cpuset["group_path"], cpuset["mount_path"],
        )
        for _attempt in range(8):
            process_ids, threads, control_processes, control_threads = (
                self._stable_descendant_state()
            )
            before_identity = (
                process_ids,
                tuple((item["tid"], item["start_time_ticks"]) for item in threads),
            )
            for item in threads:
                self.system.move_to_cpuset(
                    cpuset["tasks_path"], item["tid"], item["process_id"],
                )
                self.system.set_affinity(
                    item["tid"], cpuset["desired_cpu_ids"],
                    item["process_id"],
                )
            (
                following_process_ids, following_threads,
                following_control_processes, following_control_threads,
            ) = self._stable_descendant_state()
            following_identity = (
                following_process_ids,
                tuple(
                    (item["tid"], item["start_time_ticks"])
                    for item in following_threads
                ),
            )
            following_tids = tuple(item["tid"] for item in following_threads)
            if (
                following_identity == before_identity
                and all(
                    item["affinity_cpu_ids"] == cpuset["desired_cpu_ids"]
                    and item["cpuset_membership"] == expected_membership
                    for item in following_threads
                )
                and tuple(self.system.cpuset_members(cpuset["tasks_path"]))
                == following_tids
                and control_processes == following_control_processes
                == self._control_plane_processes
                and tuple(
                    (item["process_id"], item["tid"], item["start_time_ticks"])
                    for item in control_threads
                ) == tuple(
                    (item["process_id"], item["tid"], item["start_time_ticks"])
                    for item in following_control_threads
                )
                and all(
                    item["affinity_cpu_ids"]
                    == self.profile["cpu_plan"]["helper_cpu_ids"]
                    and item["cpuset_membership"] != expected_membership
                    for item in following_control_threads
                )
            ):
                stable = following_tids
                break
        if stable is None:
            _fail(
                "descendant PID/TID closure did not stabilize during cpuset move"
            )

    def apply(self, prior: Mapping[str, Any], boundary: Callable[[str], None]) -> None:
        self._validate_durable_prior(prior)
        if self.prior is None or prior != self.prior:
            _fail("apply prior differs from the exact captured state")
        prior_by_id = {item["control_id"]: item for item in prior["values"]}
        target = self.profile["runtime"]["minimum_frequency_khz"]
        for policy in self.profile["cpu_controls"]["policies"]:
            prefix = "cpufreq." + policy["policy_id"] + "."
            minimum = int(prior_by_id[prefix + "minimum_frequency_khz"]["text"])
            maximum = int(prior_by_id[prefix + "maximum_frequency_khz"]["text"])
            if not minimum <= target <= maximum:
                _fail(
                    "frozen target cannot be reached without an unregistered "
                    "intermediate cpufreq value"
                )
        for action in self.profile["controls"]["module_actions"]:
            if action["name"] not in prior["modules_preexisting"]:
                self._step(
                    boundary, "module_" + action["name"],
                    lambda item=action: self.system.command(
                        self._module_command(item, False)
                    ),
                )
        self._step(
            boundary, "telemetry_source_identity",
            self._verify_telemetry_source_identities,
        )
        for action in self.profile["controls"]["service_actions"]:
            if action["name"] in prior["services_active"]:
                self._step(
                    boundary, "service_" + action["name"],
                    lambda item=action: self.system.command(
                        self._service_command(item, False)
                    ),
                )
        self._step(
            boundary, "helper_affinity",
            self._set_control_plane_affinity,
        )
        # Read-only rows are retained in state but never mutated.  CPU online
        # controls are intentionally delayed until after the peer is inside its
        # exact cpuset, matching the frozen create/move/offline lifecycle.
        online = []
        for control in self.controls:
            if not control.writable:
                continue
            if control.control_id.endswith(".online"):
                online.append(control)
                continue
            self._step(
                boundary, "control_" + control.control_id,
                lambda item=control: self._write_and_verify(
                    item, item.desired_text
                ),
            )
        self._step(boundary, "cpuset", self._create_cpuset)
        for control in online:
            self._step(
                boundary, "control_" + control.control_id,
                lambda item=control: self._write_and_verify(
                    item, item.desired_text
                ),
            )
        self._step(
            boundary, "dma_latency",
            lambda: self.system.open_dma_latency(
                self.profile["dma_latency"]["desired_latency_us"]
            ),
        )

    def _applied_record(self) -> Mapping[str, Any]:
        self._verify_control_source_identities()
        self._verify_telemetry_source_identities()
        irqs, irq_sha = self._irq_identity()
        process_ids, threads, control_processes, control_threads = (
            self._stable_descendant_state()
        )
        applied_values = self._typed_values()
        for control, observed in zip(self.controls, applied_values):
            if control.writable and observed["text"] != control.desired_text:
                _fail("applied typed value differs")
        cpuset_state = self._cpuset_state(expected_exists=True)
        expected_tids = [item["tid"] for item in threads]
        if cpuset_state["members"] != expected_tids:
            _fail(
                "cpuset member population differs from the complete stabilized "
                "descendant TID closure"
            )
        expected_cpus = self.profile["runtime"]["cpu_ids"]
        cpuset = self.profile["cpuset"]
        expected_membership = "/" + os.path.relpath(
            cpuset["group_path"], cpuset["mount_path"],
        )
        if any(
            item["affinity_cpu_ids"] != expected_cpus
            or item["cpuset_membership"] != expected_membership
            for item in threads
        ):
            _fail("descendant thread affinity/cpuset membership differs")
        helper_cpus = self.profile["cpu_plan"]["helper_cpu_ids"]
        if (
            control_processes != self._control_plane_processes
            or any(
                item["affinity_cpu_ids"] != helper_cpus
                or item["cpuset_membership"] == expected_membership
                for item in control_threads
            )
        ):
            _fail("control-plane identity/affinity/cpuset placement differs")
        modules = sorted(
            item["name"] for item in self.profile["controls"]["module_actions"]
            if self.system.module_loaded(item["name"])
        )
        services = sorted(
            item["name"] for item in self.profile["controls"]["service_actions"]
            if self.system.service_active(item["name"])
        )
        expected_modules = sorted(
            item["name"] for item in self.profile["controls"]["module_actions"]
        )
        if modules != expected_modules or services:
            _fail("module/service applied population differs")
        if not self.system.dma_latency_held():
            _fail("DMA latency descriptor is not held")
        helper_pid = self.system.current_process_id()
        helper_main = next(
            (item for item in control_threads if item["tid"] == helper_pid),
            None,
        )
        peer_main = next(
            (item for item in threads if item["tid"] == self.expected_peer.pid),
            None,
        )
        helper_affinity = None if helper_main is None else list(
            helper_main["affinity_cpu_ids"]
        )
        if helper_affinity != helper_cpus or peer_main is None:
            _fail("root helper affinity differs from its frozen CPU role")
        return {
            "dma_latency_us": self.profile["runtime"]["dma_latency_us"],
            "modules_loaded": modules,
            "process_affinity": list(peer_main["affinity_cpu_ids"]),
            "record_type": "cp2_timing_control_applied_state",
            "schema_version": CONTROL_STATE_SCHEMA_VERSION,
            "services_active": services,
            "values": [dict(item) for item in applied_values],
            "privileged_v2": {
                "control_plane_processes": [
                    dict(item) for item in control_processes
                ],
                "control_plane_threads": [dict(item) for item in control_threads],
                "cpuset": cpuset_state,
                "descendant_process_ids": list(process_ids),
                "descendant_threads": [dict(item) for item in threads],
                "dma_latency_held_by_helper": True,
                "helper_affinity_cpu_ids": helper_affinity,
                "irq_numbers": list(irqs),
                "irq_population_sha256": irq_sha,
                "peer": self.expected_peer.as_record(),
                "peer_start_time_ticks": peer_main["start_time_ticks"],
                "profile_sha256": self.frozen_profile.sha256,
                "record_type": "cp2e_privileged_applied_extension",
                "schema_version": CONTROL_STATE_SCHEMA_VERSION,
            },
        }

    def validate_applied(self) -> Mapping[str, Any]:
        value = self._applied_record()
        _canonical(value)
        return value

    def _telemetry_sources(self, phase: str, run_index: int) -> Mapping[str, Any]:
        started = self.system.monotonic_ns()
        rows: List[Mapping[str, Any]] = []

        def append(
            source_id: str, kind: str, source_kind: str, path: str,
            extraction: str, cpu_id: Optional[int], register: Optional[int],
            profile_source: Mapping[str, Any], reader: Callable[[], bytes],
        ) -> None:
            read_start = self.system.monotonic_ns()
            raw = reader()
            read_end = self.system.monotonic_ns()
            if source_kind == "msr_u64_le":
                if len(raw) != 8:
                    _fail("MSR telemetry is not eight bytes")
                parsed = int.from_bytes(raw, "little")
            else:
                signed = kind == "temperature"
                text = raw.decode("ascii", "strict")
                if text.endswith("\n"):
                    text = text[:-1]
                digits = text[1:] if signed and text.startswith("-") else text
                if not digits.isdigit() or (len(digits) > 1 and digits.startswith("0")):
                    _fail("telemetry text is not canonical decimal")
                parsed = int(text)
                if not signed:
                    _u64(parsed, source_id)
            specification = protocol.source_specification_record(
                source_id, kind, source_kind, path, extraction, cpu_id,
                register, profile_source,
            )
            rows.append({
                "cpu_id": cpu_id, "extraction": extraction,
                "measurement_kind": kind, "parsed_value": parsed,
                "raw_hex": raw.hex(), "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "read_ended_monotonic_ns": read_end,
                "read_started_monotonic_ns": read_start,
                "register": register, "source_id": source_id,
                "source_kind": source_kind, "source_path": path,
                "source_spec_sha256": protocol.source_specification_sha256(
                    specification
                ),
            })

        telemetry = self.profile["telemetry_plan"]
        for source in telemetry["throttle_sources"]:
            if source["source_kind"] == "msr_u64_le":
                reader = lambda item=source: self.system.read_msr(
                    item["path"], item["register"]
                )
            else:
                reader = lambda item=source: self.system.read_raw(item["path"], 128)
            append(
                "cpu{}.amd_throttle".format(source["cpu_id"]), "amd_throttle",
                source["source_kind"], source["path"],
                source["value_extraction"], source["cpu_id"], source["register"],
                source, reader,
            )
        for source in telemetry["aperf_mperf"]["cpu_sources"]:
            for name, register in (
                ("aperf", source["aperf_register"]),
                ("mperf", source["mperf_register"]),
            ):
                append(
                    "cpu{}.{}".format(source["cpu_id"], name), name,
                    "msr_u64_le", source["path"],
                    "full_u64_little_endian_pread", source["cpu_id"], register,
                    source,
                    lambda item=source, offset=register: self.system.read_msr(
                        item["path"], offset
                    ),
                )
        for source in telemetry["frequency_sources"]:
            append(
                "cpu{}.scaling_cur_freq".format(source["cpu_id"]),
                "scaling_cur_freq", "text_integer_khz", source["path"],
                "canonical_ascii_decimal_u64_one_line", source["cpu_id"], None,
                source,
                lambda item=source: self.system.read_raw(item["path"], 128),
            )
        counters = telemetry["aperf_mperf"]
        reference_source = counters["reference_frequency_source"]
        append(
            "shared.f_ref", "f_ref", "text_integer_khz",
            reference_source["path"],
            "canonical_ascii_decimal_u64_one_line", None, None,
            {
                "reference_frequency_khz": counters["reference_frequency_khz"],
                "reference_frequency_source": reference_source,
            }, lambda: self.system.read_raw(reference_source["path"], 128),
        )
        temperature = telemetry["temperature_source"]
        append(
            "shared.temperature", "temperature", "k10temp_text_integer",
            temperature["input_path"],
            "canonical_ascii_decimal_i64_one_line", None, None,
            temperature,
            lambda: self.system.read_raw(temperature["input_path"], 128),
        )
        rows.sort(key=lambda item: item["source_id"])
        ended = max(item["read_ended_monotonic_ns"] for item in rows)
        if (
            ended < started
            or ended - started
            > telemetry["aperf_mperf"]["maximum_snapshot_skew_ns"]
        ):
            _fail("raw telemetry snapshot exceeds its frozen skew bound")
        return {
            "phase": phase, "run_index": run_index,
            "schema_version": protocol.SCHEMA_VERSION,
            "snapshot_ended_monotonic_ns": ended,
            "snapshot_skew_ns": ended - started,
            "snapshot_started_monotonic_ns": started,
            "sources": rows,
        }

    def observe(self, phase: str, run_index: int) -> Mapping[str, Any]:
        if not self._population_active:
            _fail("telemetry observation occurred without the population guardian")
        self._raise_guard_error()
        return self._telemetry_sources(phase, run_index)

    def _surface_snapshot(self) -> Mapping[str, Any]:
        applied = self._applied_record()
        return {
            "applied_state_sha256": _sha_record(applied),
            "irq_population_sha256": applied["privileged_v2"]["irq_population_sha256"],
        }

    def _guardian_typed_telemetry(self) -> List[Mapping[str, Any]]:
        telemetry = self.profile["telemetry_plan"]
        specifications: List[Tuple[
            str, str, str, str, str, Optional[int], Optional[int],
            Mapping[str, Any], Callable[[], bytes],
        ]] = []
        for source in telemetry["throttle_sources"]:
            reader = (
                (lambda item=source: self.system.read_msr(
                    item["path"], item["register"]
                ))
                if source["source_kind"] == "msr_u64_le"
                else (lambda item=source: self.system.read_raw(item["path"], 128))
            )
            specifications.append((
                "cpu{}.amd_throttle".format(source["cpu_id"]),
                "amd_throttle", source["source_kind"], source["path"],
                source["value_extraction"], source["cpu_id"], source["register"],
                source, reader,
            ))
        temperature = telemetry["temperature_source"]
        specifications.append((
            "shared.temperature", "temperature", "k10temp_text_integer",
            temperature["input_path"], "canonical_ascii_decimal_i64_one_line",
            None, None, temperature,
            lambda: self.system.read_raw(temperature["input_path"], 128),
        ))
        rows = []
        for (
            source_id, kind, source_kind, path, extraction, cpu_id, register,
            profile_source, reader,
        ) in specifications:
            started = self.system.monotonic_ns()
            raw = reader()
            ended = self.system.monotonic_ns()
            if source_kind == "msr_u64_le":
                if len(raw) != 8:
                    _fail("guardian MSR source is not exactly eight bytes")
                parsed = int.from_bytes(raw, "little")
            else:
                try:
                    text = raw.decode("ascii", "strict")
                except UnicodeDecodeError as exc:
                    raise PrivilegedBackendError(
                        "guardian telemetry is not ASCII"
                    ) from exc
                if text.endswith("\n"):
                    text = text[:-1]
                signed = kind == "temperature"
                digits = text[1:] if signed and text.startswith("-") else text
                if (
                    not digits.isdigit()
                    or (len(digits) > 1 and digits.startswith("0"))
                ):
                    _fail("guardian telemetry is not canonical decimal")
                parsed = int(text)
                if not signed:
                    _u64(parsed, source_id)
            if kind == "temperature":
                temperature = telemetry["temperature_source"]
                if not (
                    temperature["minimum_millicelsius"]
                    <= parsed <= temperature["maximum_millicelsius"]
                ):
                    _fail("guardian temperature is outside the frozen range")
            specification = protocol.source_specification_record(
                source_id, kind, source_kind, path, extraction, cpu_id,
                register, profile_source,
            )
            rows.append({
                "cpu_id": cpu_id, "extraction": extraction,
                "measurement_kind": kind, "parsed_value": parsed,
                "raw_hex": raw.hex(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "read_ended_monotonic_ns": ended,
                "read_started_monotonic_ns": started,
                "register": register, "source_id": source_id,
                "source_kind": source_kind, "source_path": path,
                "source_spec_sha256": protocol.source_specification_sha256(
                    specification
                ),
            })
        rows.sort(key=lambda item: item["source_id"])
        return rows

    def _validate_scheduler_population(
        self,
    ) -> Mapping[str, Any]:
        """Prove the estimator/control partition and observe foreign eligibility."""

        descendants, thread_rows, control_processes, control_threads = (
            self._stable_descendant_state()
        )
        descendant_tids = tuple(item["tid"] for item in thread_rows)
        control_tids = tuple(item["tid"] for item in control_threads)
        expected_cpus = tuple(self.profile["runtime"]["cpu_ids"])
        cpuset = self.profile["cpuset"]
        expected_membership = "/" + os.path.relpath(
            cpuset["group_path"], cpuset["mount_path"],
        )
        if any(
            tuple(item["affinity_cpu_ids"]) != expected_cpus
            or item["cpuset_membership"] != expected_membership
            for item in thread_rows
        ):
            _fail("descendant thread affinity/cpuset confinement escaped")
        if tuple(self.system.cpuset_members(cpuset["tasks_path"])) != descendant_tids:
            _fail(
                "cpuset members differ from the stabilized complete descendant "
                "TID closure"
            )
        if (
            control_processes != self._control_plane_processes
            or set(descendant_tids).intersection(control_tids)
            or any(
                tuple(item["affinity_cpu_ids"])
                != tuple(self.profile["cpu_plan"]["helper_cpu_ids"])
                or item["cpuset_membership"] == expected_membership
                for item in control_threads
            )
        ):
            _fail("control-plane identity/affinity/cpuset partition escaped")

        observed = tuple(
            self.system.foreign_affinity_eligibility(expected_cpus, descendants)
        )
        foreign_rows: List[Mapping[str, Any]] = []
        previous_tid = 0
        controlled_cpus = self.profile["cpu_plan"]["controlled_cpu_ids"]
        for raw in observed:
            row = _exact_mapping(
                raw, FOREIGN_AFFINITY_ELIGIBILITY_KEYS,
                "foreign affinity-eligibility row",
            )
            tid = _u64(row["tid"], "foreign affinity-eligibility TID")
            start = _u64(
                row["process_start_time_ticks"],
                "foreign affinity-eligibility process start time",
            )
            affinity = _cpu_population(
                row["effective_affinity_cpu_ids"],
                "foreign effective affinity", controlled_cpus,
            )
            if (
                tid == 0 or tid <= previous_tid
                or tid in descendant_tids or tid in control_tids
                or not set(affinity).intersection(expected_cpus)
            ):
                _fail("foreign affinity-eligibility row identity/order differs")
            foreign_rows.append({
                "effective_affinity_cpu_ids": list(affinity),
                "process_start_time_ticks": start,
                "tid": tid,
            })
            previous_tid = tid
        return {
            "child_cpuset_members": list(descendant_tids),
            "complete_control_plane_processes": [
                dict(item) for item in control_processes
            ],
            "complete_control_plane_threads": [
                dict(item) for item in control_threads
            ],
            "complete_descendant_population": list(descendants),
            "complete_descendant_threads": [dict(item) for item in thread_rows],
            "complete_descendant_tid_population": list(descendant_tids),
            "foreign_affinity_eligibility": foreign_rows,
        }

    @staticmethod
    def _telemetry_value_map(receipt: Mapping[str, Any]) -> Mapping[str, int]:
        return {
            item["source_id"]: item["parsed_value"]
            for item in receipt["sources"]
        }

    def _validate_data_free_telemetry_pair(
        self, before: Mapping[str, Any], after: Mapping[str, Any],
    ) -> None:
        """Check one non-workload APERF/MPERF/control-effectiveness interval."""

        if [item["source_id"] for item in before["sources"]] != [
            item["source_id"] for item in after["sources"]
        ]:
            _fail("data-free telemetry source population changed")
        pre = self._telemetry_value_map(before)
        post = self._telemetry_value_map(after)
        telemetry = self.profile["telemetry_plan"]
        counters = telemetry["aperf_mperf"]
        reference = counters["reference_frequency_khz"]
        target = counters["target_frequency_khz"]
        minimum = counters["minimum_effective_frequency_khz"]
        maximum = counters["maximum_effective_frequency_khz"]
        temperature = telemetry["temperature_source"]
        for values in (pre, post):
            if values["shared.f_ref"] != reference:
                _fail("data-free reference frequency differs")
            if not (
                temperature["minimum_millicelsius"]
                <= values["shared.temperature"]
                <= temperature["maximum_millicelsius"]
            ):
                _fail("data-free CPU temperature lies outside the frozen range")
        for cpu_id in telemetry["required_cpu_ids"]:
            aperf_id = "cpu{}.aperf".format(cpu_id)
            mperf_id = "cpu{}.mperf".format(cpu_id)
            throttle_id = "cpu{}.amd_throttle".format(cpu_id)
            frequency_id = "cpu{}.scaling_cur_freq".format(cpu_id)
            if (
                pre[frequency_id] != target or post[frequency_id] != target
                or pre[throttle_id] != post[throttle_id]
            ):
                _fail("data-free current-frequency/throttle state differs")
            if post[aperf_id] <= pre[aperf_id] or post[mperf_id] <= pre[mperf_id]:
                _fail("data-free APERF/MPERF counters did not advance")
            aperf_delta = post[aperf_id] - pre[aperf_id]
            mperf_delta = post[mperf_id] - pre[mperf_id]
            products = (
                minimum * mperf_delta,
                reference * aperf_delta,
                maximum * mperf_delta,
            )
            if any(value > (1 << 128) - 1 for value in products):
                _fail("data-free effective-frequency product exceeds u128")
            if not products[0] <= products[1] <= products[2]:
                _fail("data-free effective frequency violates frozen bounds")

    def validate_data_free_feasibility(self) -> Mapping[str, Any]:
        """Prove applied controls over one profile-bound idle interval.

        This is protecting feasibility evidence only.  It executes no
        estimator and returns hashes/counts rather than raw host values.
        """

        if self.prior is None or self._population_active:
            _fail("data-free feasibility backend state differs")
        applied_before = self._applied_record()
        witness_before = self._validate_scheduler_population()
        telemetry_before = self._telemetry_sources("pre", 0)
        interval = self.profile["guardians"]["poll_interval_ns"]
        deadline = self.system.monotonic_ns() + interval
        self.system.wait_until(deadline, threading.Event())
        if self.system.monotonic_ns() < deadline:
            _fail("data-free feasibility interval ended early")
        telemetry_after = self._telemetry_sources("post", 0)
        witness_after = self._validate_scheduler_population()
        applied_after = self._applied_record()
        core_before = dict(witness_before)
        core_after = dict(witness_after)
        foreign_before = core_before.pop("foreign_affinity_eligibility")
        foreign_after = core_after.pop("foreign_affinity_eligibility")
        if (
            applied_after != applied_before
            or core_after != core_before
        ):
            _fail("data-free applied/descendant state drifted across the interval")
        self._validate_data_free_telemetry_pair(
            telemetry_before, telemetry_after,
        )
        return {
            "applied_state_sha256": _sha_record(applied_before),
            "checkpoint": "CP2-E",
            "control_plane_process_count": len(
                witness_before["complete_control_plane_processes"]
            ),
            "control_plane_tid_count": len(
                witness_before["complete_control_plane_threads"]
            ),
            "descendant_count": len(
                witness_before["complete_descendant_population"]
            ),
            "descendant_tid_count": len(
                witness_before["complete_descendant_tid_population"]
            ),
            "foreign_affinity_eligibility_after_sha256": _sha_record({
                "rows": list(foreign_after),
            }),
            "foreign_affinity_eligibility_before_sha256": _sha_record({
                "rows": list(foreign_before),
            }),
            "foreign_affinity_eligibility_count": len(foreign_after),
            "formal_execution_locked": True,
            "interval_ns": interval,
            "profile_sha256": self.frozen_profile.sha256,
            "record_type": "cp2e_data_free_feasibility_probe",
            "schema_version": protocol.SCHEMA_VERSION,
            "telemetry_after_sha256": _sha_record(telemetry_after),
            "telemetry_before_sha256": _sha_record(telemetry_before),
        }

    def _guardian_once(self) -> None:
        if self._append_observation is None:
            _fail("guardian append callback is absent")
        started = self.system.monotonic_ns()
        witness = self._validate_scheduler_population()
        state = self._surface_snapshot()
        typed = self._guardian_typed_telemetry()
        ended = self.system.monotonic_ns()
        surfaces = []
        for surface_id in self.profile["guardians"]["surfaces"]:
            surfaces.append({
                "passed": True,
                "state_sha256": _sha_record({
                    "state": state, "surface_id": surface_id,
                }),
                "surface_id": surface_id,
            })
        row = {
            **witness,
            "drift": False, "ended_monotonic_ns": ended,
            "irq_population_sha256": state["irq_population_sha256"],
            "scheduler_witness_sha256": _sha_record(witness),
            "started_monotonic_ns": started, "surface_states": surfaces,
            "thermal_or_throttle_event": False, "typed_telemetry": typed,
        }
        self._append_observation(row)
        self._last_descendants = tuple(
            witness["complete_descendant_population"]
        )
        self._last_descendant_tids = tuple(
            witness["complete_descendant_tid_population"]
        )
        self._last_descendant_threads = tuple(
            witness["complete_descendant_threads"]
        )
        self._last_control_plane_threads = tuple(
            witness["complete_control_plane_threads"]
        )
        self._last_cpuset_members = tuple(witness["child_cpuset_members"])
        self._last_foreign_affinity_eligibility = tuple(
            witness["foreign_affinity_eligibility"]
        )
        self._last_scheduler_witness_sha256 = row[
            "scheduler_witness_sha256"
        ]
        self._irq_sha256 = state["irq_population_sha256"]
        self._guard_samples += 1
        if self._guard_first_ns is None:
            self._guard_first_ns = started
        self._guard_last_ns = ended

    def _guardian_loop(self) -> None:
        interval = self.profile["guardians"]["poll_interval_ns"]
        deadline = self.system.monotonic_ns() + interval
        try:
            while not self._guard_stop.is_set():
                self.system.wait_until(deadline, self._guard_stop)
                if self._guard_stop.is_set():
                    return
                self._guardian_once()
                deadline += interval
        except BaseException as exc:
            self._guard_error = exc
            self._guard_stop.set()

    def start_population_guard(
        self, peer: protocol.PeerCredentials, session_id: str,
        append_observation: Callable[[Mapping[str, Any]], None],
    ) -> Mapping[str, Any]:
        if peer != self.expected_peer or self._population_active:
            _fail("population guardian peer/state differs")
        protocol._require_hash(session_id, "population guardian session ID")
        self._population_active = True
        self._population_session_id = session_id
        self._append_observation = append_observation
        self._guard_stop.clear()
        # Synchronous first sample closes the apply-to-thread-start gap.
        self._guardian_once()
        self._guard_thread = threading.Thread(
            target=self._guardian_loop, name="cp2e-root-population-guardian",
            daemon=False,
        )
        self._guard_thread.start()
        return {
            "bound_gid": peer.gid, "bound_pid": peer.pid, "bound_uid": peer.uid,
            "cpuset": self.profile["cpuset"]["group_path"],
            "session_id": session_id,
        }

    def _raise_guard_error(self) -> None:
        if self._guard_error is not None:
            raise PrivilegedBackendError("continuous guardian failed") from self._guard_error

    def validate_population(self, phase: str) -> Mapping[str, Any]:
        if not self._population_active:
            _fail("population guardian is inactive")
        self._raise_guard_error()
        # The independent guardian thread is the sole evidence cadence owner.
        # Boundary validation rechecks the complete state but never injects an
        # off-cadence observation into its stream.
        self._surface_snapshot()
        return {
            "complete_control_plane_population": [
                item["pid"] for item in self._control_plane_processes
            ],
            "complete_control_plane_tid_population": [
                item["tid"] for item in self._last_control_plane_threads
            ],
            "complete_descendant_population": list(self._last_descendants),
            "complete_descendant_tid_population": list(self._last_descendant_tids),
            "continuous_guard_active": True, "drift": False,
            "foreign_affinity_eligibility_count": len(
                self._last_foreign_affinity_eligibility
            ),
            "phase": phase,
            "samples": self._guard_samples,
            "scheduler_witness_sha256": self._last_scheduler_witness_sha256,
        }

    def stop_population_guard(self) -> Mapping[str, Any]:
        if not self._population_active or self._guard_thread is None:
            raise PrivilegedBackendIndeterminate("population guardian is not active")
        self._guard_stop.set()
        self._guard_thread.join(timeout=5.0)
        if self._guard_thread.is_alive():
            raise PrivilegedBackendIndeterminate("population guardian did not terminate")
        self._raise_guard_error()
        if (
            self._guard_first_ns is None or self._guard_last_ns is None
            or self._population_session_id is None
            or self._last_scheduler_witness_sha256 is None
        ):
            raise PrivilegedBackendIndeterminate("population guardian lacks coverage")
        coverage_end = self.system.monotonic_ns()
        if (
            coverage_end < self._guard_last_ns
            or coverage_end - self._guard_last_ns
            > self.profile["guardians"]["maximum_observation_gap_ns"]
        ):
            raise PrivilegedBackendIndeterminate(
                "population guardian terminal coverage gap is unproved"
            )
        self._population_active = False
        return {
            "bound_peer": self.expected_peer.as_record(),
            "coverage_ended_monotonic_ns": coverage_end,
            "coverage_started_monotonic_ns": self._guard_first_ns,
            "drift": False,
            "final_child_cpuset_member_count": len(self._last_cpuset_members),
            "final_control_plane_process_count": len(
                self._control_plane_processes
            ),
            "final_control_plane_tid_count": len(
                self._last_control_plane_threads
            ),
            "final_descendant_process_count": len(self._last_descendants),
            "final_descendant_tid_count": len(self._last_descendant_tids),
            "final_foreign_affinity_eligibility_count": len(
                self._last_foreign_affinity_eligibility
            ),
            "final_foreign_affinity_eligibility_sha256": _sha_record({
                "rows": list(self._last_foreign_affinity_eligibility),
            }),
            "final_scheduler_witness_sha256": (
                self._last_scheduler_witness_sha256
            ),
            "irq_population_changed": False,
            "maximum_observation_gap_ns": self.profile["guardians"][
                "maximum_observation_gap_ns"
            ],
            "required_surfaces": list(self.profile["guardians"]["surfaces"]),
            "schema_version": protocol.SCHEMA_VERSION,
            "session_id": self._population_session_id,
            "thermal_or_throttle_event": False,
        }

    def recover_population_guard(self) -> Optional[Mapping[str, Any]]:
        if not self._population_active:
            return None
        self._guard_stop.set()
        if self._guard_thread is not None:
            self._guard_thread.join(timeout=5.0)
            if self._guard_thread.is_alive():
                raise PrivilegedBackendIndeterminate(
                    "recovery could not terminate population guardian"
                )
        self._population_active = False
        return {
            "record_type": "cp2e_population_guard_recovered",
            "samples": self._guard_samples,
        }

    @staticmethod
    def _process_absence_error(exc: BaseException) -> bool:
        return isinstance(exc, (FileNotFoundError, ProcessLookupError)) or (
            isinstance(exc, OSError) and exc.errno in (errno.ENOENT, errno.ESRCH)
        )

    def _external_recovery_task_rows(
        self, prior: Mapping[str, Any], *, require_restored_state: bool,
    ) -> Tuple[
        Tuple[Mapping[str, Any], ...], Tuple[Mapping[str, Any], ...],
        Tuple[int, ...],
    ]:
        """Capture P1 targets independently of the authenticated P2 chain."""

        extension = prior["privileged_v2"]
        prior_peer = protocol.PeerCredentials.from_record(extension["peer"])
        def capture_process_threads(
            process_id: int, expected_rows: Sequence[Mapping[str, Any]],
            label: str,
        ) -> Tuple[Mapping[str, Any], ...]:
            expected = tuple(expected_rows)
            expected_tids = tuple(item["tid"] for item in expected)
            observed_tids = tuple(self.system.thread_ids(process_id))
            if observed_tids != expected_tids:
                raise PrivilegedBackendIndeterminate(
                    label + " thread population changed"
                )
            rows = self._thread_rows((process_id,))
            if rows is None or tuple(
                (item["process_id"], item["tid"], item["start_time_ticks"])
                for item in rows
            ) != tuple(
                (item["process_id"], item["tid"], item["start_time_ticks"])
                for item in expected
            ):
                raise PrivilegedBackendIndeterminate(
                    label + " TID identity was reused"
                )
            if require_restored_state and any(
                item["affinity_cpu_ids"] != expected_item["affinity_cpu_ids"]
                or item["cpuset_membership"]
                != expected_item["cpuset_membership"]
                for item, expected_item in zip(rows, expected)
            ):
                raise PrivilegedBackendIndeterminate(
                    label + " scheduler state was not restored exactly"
                )
            return rows

        expected_estimator_by_process: Dict[
            int, List[Mapping[str, Any]]
        ] = {}
        for row in extension["descendant_threads"]:
            expected_estimator_by_process.setdefault(
                row["process_id"], []
            ).append(row)
        estimator_rows: List[Mapping[str, Any]] = []
        absent_process_ids: List[int] = []
        for process_id in extension["descendant_process_ids"]:
            try:
                observed_process = self.system.process_identity(process_id)
            except BaseException as exc:
                if self._process_absence_error(exc):
                    if (
                        self._external_recovery_initially_live is not None
                        and process_id
                        in self._external_recovery_initially_live
                    ):
                        raise PrivilegedBackendIndeterminate(
                            "held predecessor estimator exited during recovery"
                        ) from exc
                    absent_process_ids.append(process_id)
                    continue
                raise
            expected_leader = next(
                item for item in expected_estimator_by_process[process_id]
                if item["tid"] == process_id
            )
            if (
                self._external_recovery_initially_absent is not None
                and process_id in self._external_recovery_initially_absent
            ):
                raise PrivilegedBackendIndeterminate(
                    "absent predecessor estimator PID was reused"
                )
            if (
                observed_process["start_time_ticks"]
                != expected_leader["start_time_ticks"]
                or (
                    process_id == prior_peer.pid
                    and (
                        observed_process["process_uid"] != prior_peer.uid
                        or observed_process["process_gid"] != prior_peer.gid
                    )
                )
            ):
                raise PrivilegedBackendIndeterminate(
                    "predecessor estimator PID was reused"
                )
            estimator_rows.extend(capture_process_threads(
                process_id, expected_estimator_by_process[process_id],
                "predecessor estimator",
            ))
        expected_by_process: Dict[int, List[Mapping[str, Any]]] = {}
        for row in extension["control_plane_threads"]:
            expected_by_process.setdefault(row["process_id"], []).append(row)
        live_control_rows: List[Mapping[str, Any]] = []
        for expected_process in extension["control_plane_processes"]:
            process_id = expected_process["pid"]
            try:
                observed_process = self.system.process_identity(process_id)
            except BaseException as exc:
                if self._process_absence_error(exc):
                    if (
                        self._external_recovery_initially_live is not None
                        and process_id
                        in self._external_recovery_initially_live
                    ):
                        raise PrivilegedBackendIndeterminate(
                            "held predecessor control process exited during recovery"
                        ) from exc
                    absent_process_ids.append(process_id)
                    continue
                raise
            if (
                self._external_recovery_initially_absent is not None
                and process_id in self._external_recovery_initially_absent
            ):
                raise PrivilegedBackendIndeterminate(
                    "absent predecessor control PID was reused"
                )
            if observed_process != expected_process:
                raise PrivilegedBackendIndeterminate(
                    "predecessor control-plane PID was reused or reparented"
                )
            live_control_rows.extend(capture_process_threads(
                process_id, expected_by_process[process_id],
                "predecessor control plane",
            ))
        return (
            tuple(estimator_rows), tuple(live_control_rows),
            tuple(sorted(absent_process_ids)),
        )

    def _restore_external_scheduler_partition(
        self, prior: Mapping[str, Any],
    ) -> None:
        if self._external_recovery_prior_peer is None:
            _fail("external recovery control plane was not authenticated")
        extension = prior["privileged_v2"]
        _replacement_processes, replacement_threads, chain, chain_threads = (
            self._stable_descendant_state()
        )
        if chain != self._control_plane_processes:
            raise PrivilegedBackendIndeterminate(
                "replacement control plane changed during recovery"
            )
        replacement_tids = {
            item["tid"] for item in replacement_threads + chain_threads
        }
        (
            estimator_rows, live_control_rows, _absent_process_ids,
        ) = self._external_recovery_task_rows(
            prior, require_restored_state=False,
        )
        targets = estimator_rows + live_control_rows
        if replacement_tids.intersection(item["tid"] for item in targets):
            raise PrivilegedBackendIndeterminate(
                "replacement controller was confused with a predecessor target"
            )
        cpuset = self.profile["cpuset"]
        if self.system.path_exists(cpuset["group_path"]):
            members = tuple(self.system.cpuset_members(cpuset["tasks_path"]))
            allowed_members = {
                item["tid"] for item in estimator_rows
            }
            if not set(members).issubset(allowed_members):
                raise PrivilegedBackendIndeterminate(
                    "recovery cpuset contains a non-predecessor-live TID"
                )
        helper_pid = self.system.current_process_id()
        ordered = sorted(
            targets, key=lambda item: (item["tid"] == helper_pid, item["tid"]),
        )
        for item in ordered:
            self.system.move_to_cpuset_path(
                cpuset["mount_path"], item["cpuset_membership"],
                item["tid"], item["process_id"],
            )
            self.system.set_affinity(
                item["tid"], item["affinity_cpu_ids"], item["process_id"],
            )

    def _restore_scheduler_partition(self, prior: Mapping[str, Any]) -> None:
        extension = prior["privileged_v2"]
        (
            current_processes, current_threads, current_control_processes,
            current_control_threads,
        ) = self._stable_descendant_state()
        prior_processes = tuple(extension["descendant_process_ids"])
        prior_threads = tuple(extension["descendant_threads"])
        prior_control_processes = tuple(extension["control_plane_processes"])
        prior_control_threads = tuple(extension["control_plane_threads"])
        identity = lambda rows: tuple(
            (item["process_id"], item["tid"], item["start_time_ticks"])
            for item in rows
        )
        if (
            current_processes != prior_processes
            or current_control_processes != prior_control_processes
            or identity(current_threads) != identity(prior_threads)
            or identity(current_control_threads) != identity(prior_control_threads)
        ):
            raise PrivilegedBackendIndeterminate(
                "estimator/control identity changed; exact prior mapping is unavailable"
            )
        helper_pid = self.system.current_process_id()
        rows = list(prior_threads) + list(prior_control_threads)
        rows.sort(key=lambda item: (item["tid"] == helper_pid, item["tid"]))
        for item in rows:
            if self.system.start_time_ticks(
                item["tid"], item["process_id"],
            ) != item["start_time_ticks"]:
                raise PrivilegedBackendIndeterminate(
                    "estimator/control TID was reused"
                )
            self.system.move_to_cpuset_path(
                self.profile["cpuset"]["mount_path"],
                item["cpuset_membership"], item["tid"], item["process_id"],
            )
            self.system.set_affinity(
                item["tid"], item["affinity_cpu_ids"], item["process_id"],
            )

    def restore(
        self, prior: Mapping[str, Any], boundary: Callable[[str], None],
    ) -> None:
        external_recovery = self._external_recovery_prior_peer is not None
        self._validate_durable_prior(
            prior, external_recovery=external_recovery,
        )
        errors: List[str] = []

        def attempt(label: str, operation: Callable[[], None]) -> None:
            for marker in ("before_" + label,):
                try:
                    boundary(marker)
                except BaseException as exc:
                    errors.append(marker + ":" + str(exc))
            try:
                operation()
            except BaseException as exc:
                errors.append(label + ":" + str(exc))
            try:
                boundary("after_" + label)
            except BaseException as exc:
                errors.append("after_" + label + ":" + str(exc))

        if self._population_active:
            attempt("population_guard", lambda: self.recover_population_guard())
        attempt("dma_latency", self.system.close_dma_latency)
        attempt(
            "scheduler_membership_affinity",
            lambda: (
                self._restore_external_scheduler_partition(prior)
                if external_recovery
                else self._restore_scheduler_partition(prior)
            ),
        )
        attempt(
            "cpuset_remove",
            lambda: self.system.remove_cpuset(self.profile["cpuset"]["group_path"]),
        )
        prior_values = {
            item["control_id"]: item for item in prior["values"]
        }
        controls_by_id = {item.control_id: item for item in self.controls}
        # CPU online is restored first so every remaining sysfs surface is
        # accessible; cpufreq maxima precede minima to admit the prior range.
        ordered_ids = []
        ordered_ids.extend(
            item.control_id for item in self.controls
            if item.writable and item.control_id.endswith(".online")
        )
        ordered_ids.extend(
            item.control_id for item in self.controls
            if item.writable and item.control_id.endswith(".maximum_frequency_khz")
        )
        ordered_ids.extend(
            item.control_id for item in reversed(self.controls)
            if item.writable and item.control_id not in ordered_ids
        )
        for control_id in ordered_ids:
            control = controls_by_id[control_id]
            prior_text = prior_values[control_id]["text"]
            attempt(
                "control_" + control_id,
                lambda item=control, text=prior_text: self._write_and_verify(item, text),
            )
        prior_services = set(prior["services_active"])
        for action in reversed(self.profile["controls"]["service_actions"]):
            attempt(
                "service_" + action["name"],
                lambda item=action: self.system.command(
                    self._service_command(
                        item, item["name"] in prior_services,
                    )
                ) if self.system.service_active(item["name"])
                != (item["name"] in prior_services) else None,
            )
        prior_modules = set(prior["modules_preexisting"])
        for action in reversed(self.profile["controls"]["module_actions"]):
            attempt(
                "module_" + action["name"],
                lambda item=action: self.system.command(
                    self._module_command(
                        item, item["name"] not in prior_modules,
                    )
                ) if self.system.module_loaded(item["name"])
                != (item["name"] in prior_modules) else None,
            )
        if errors:
            raise PrivilegedBackendIndeterminate(";".join(errors))

    def validate_restored(self, prior: Mapping[str, Any]) -> Mapping[str, Any]:
        external_recovery = self._external_recovery_prior_peer is not None
        if external_recovery:
            raise PrivilegedBackendIndeterminate(
                "external recovery requires an observed reconciliation proof"
            )
        self._validate_durable_prior(
            prior, external_recovery=external_recovery,
        )
        self._verify_control_source_identities()
        restored = self._prior_record()
        if restored != prior:
            raise PrivilegedBackendIndeterminate(
                "restored complete schema-v2 state differs from the durable prior"
            )
        return restored

    def validate_external_recovery(
        self, prior: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Return an observed P1/P2 reconciliation, never a copied prior."""

        if self._external_recovery_prior_peer is None:
            _fail("external recovery was not prepared")
        self._validate_durable_prior(prior, external_recovery=True)
        self._verify_control_source_identities()
        irqs, irq_sha256 = self._irq_identity()
        if self.system.dma_latency_held():
            raise PrivilegedBackendIndeterminate(
                "DMA latency descriptor remains held after recovery"
            )
        modules = sorted(
            item["name"]
            for item in self.profile["controls"]["module_actions"]
            if self.system.module_loaded(item["name"])
        )
        services = sorted(
            item["name"]
            for item in self.profile["controls"]["service_actions"]
            if self.system.service_active(item["name"])
        )
        values = self._typed_values()
        if (
            modules != prior["modules_preexisting"]
            or services != prior["services_active"]
            or values != tuple(prior["values"])
            or self.system.path_exists(self.profile["cpuset"]["group_path"])
        ):
            raise PrivilegedBackendIndeterminate(
                "externally restored persistent state differs from the prior"
            )
        estimator_rows, old_control_rows, absent = (
            self._external_recovery_task_rows(
                prior, require_restored_state=True,
            )
        )
        (
            replacement_process_ids, replacement_peer_threads,
            replacement_chain, replacement_chain_threads,
        ) = self._stable_descendant_state()
        if replacement_chain != self._control_plane_processes:
            raise PrivilegedBackendIndeterminate(
                "replacement control plane changed before recovery validation"
            )
        persistent_state = {
            "absent_predecessor_process_ids": list(absent),
            "cpuset": self._cpuset_state(expected_exists=False),
            "dma_latency_held_by_helper": False,
            "irq_numbers": list(irqs),
            "irq_population_sha256": irq_sha256,
            "live_predecessor_control_threads": [
                dict(item) for item in old_control_rows
            ],
            "live_predecessor_estimator_threads": [
                dict(item) for item in estimator_rows
            ],
            "modules_preexisting": modules,
            "replacement_control_plane_processes": [
                dict(item) for item in replacement_chain
            ],
            "replacement_control_plane_threads": [
                dict(item) for item in replacement_chain_threads
            ],
            "replacement_peer": self.expected_peer.as_record(),
            "replacement_peer_process_ids": list(replacement_process_ids),
            "replacement_peer_threads": [
                dict(item) for item in replacement_peer_threads
            ],
            "services_active": services,
            "values": [dict(item) for item in values],
        }
        return {
            "persistent_state": persistent_state,
            "persistent_state_sha256": _sha_record(persistent_state),
            "prior_state_sha256": _sha_record(prior),
            "record_type": "cp2e_external_recovery_validation",
            "schema_version": protocol.SCHEMA_VERSION,
        }

    def close(self) -> None:
        if self._closed:
            return
        errors = []
        if self._population_active:
            try:
                self.recover_population_guard()
            except BaseException as exc:
                errors.append(exc)
        try:
            self.system.close_dma_latency()
        except BaseException as exc:
            errors.append(exc)
        try:
            self.system.close_process_identity_descriptors()
        except BaseException as exc:
            errors.append(exc)
        self._closed = True
        if errors:
            raise PrivilegedBackendIndeterminate(
                "backend descriptor closure failed"
            ) from errors[0]


__all__ = [
    "CONTROL_STATE_SCHEMA_VERSION", "LinuxRootSystem",
    "PrivilegedBackendError", "PrivilegedBackendIndeterminate",
    "PrivilegedTimingBackend", "TypedControl", "derive_typed_controls",
    "require_legacy_write_closure",
]
