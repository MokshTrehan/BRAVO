#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reversible CP2-E host-control transaction and read-only snapshots.

The transaction fails before mutation unless noninteractive privilege is
already available.  It journals exact prior values before the first change,
restores in reverse order on every Python exit path, verifies restoration, and
retains the journal when exact restoration cannot be proved.  It never asks
for, reads, or stores credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cp2_timing_profile


MAX_CONTROL_TEXT_BYTES = 64 * 1024
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_RECORDS = 4096
ALLOWED_MODULES = frozenset(("k10temp", "msr"))
ALLOWED_SERVICE = "irqbalance"
JOURNAL_NAME = re.compile(r"^schurvio-cp2e-control-[0-9a-f]{32}\.jsonl$")
SAFE_METRIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
U64_MAX = (1 << 64) - 1
MSR_APERF = 0xE8
MSR_MPERF = 0xE7
TELEMETRY_ROLES = frozenset((
    "aperf", "mperf", "thermal_throttle_count",
    "scaling_current_frequency_khz", "temperature_millicelsius",
))
REQUIRED_CONTROL_COVERAGE = (
    "process_affinity", "cpuset", "smt_sibling_placement", "governor",
    "minimum_frequency", "maximum_frequency", "boost", "cpu_online",
    "cpu_idle", "irq_affinity", "irqbalance", "dma_latency", "k10temp",
    "msr", "temperature_telemetry", "frequency_telemetry",
    "aperf_telemetry", "mperf_telemetry", "throttle_telemetry",
)


class TimingControlError(RuntimeError):
    """Host controls were unavailable, drifted, or could not be restored."""


class TimingControlIndeterminate(TimingControlError):
    """Exact prior host state could not be re-established and proved."""


def _fail(message: str) -> None:
    raise TimingControlError(message)


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"


def _plain_u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > U64_MAX:
        _fail(label + " is outside the unsigned 64-bit domain")
    return value


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        count = os.write(descriptor, payload[offset:])
        if count <= 0:
            _fail("descriptor write made no progress")
        offset += count


def _open_absolute_directory_nofollow(path: str) -> int:
    """Open every absolute directory component without following symlinks."""

    if (
        type(path) is not str or not path.startswith("/") or path == "/"
        or os.path.normpath(path) != path
    ):
        _fail("journal parent is not a normalized non-root absolute path")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in path.split("/")[1:]:
            if not component or component in (".", ".."):
                _fail("journal parent contains an unsafe component")
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            following = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = following
        status = os.fstat(descriptor)
        if not stat.S_ISDIR(status.st_mode):
            _fail("journal parent descriptor is not a directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _strict_journal_json(payload: bytes) -> Mapping[str, Any]:
    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, item in items:
            if key in value:
                _fail("control journal contains a duplicate JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(
            payload.decode("utf-8", "strict"),
            object_pairs_hook=pairs,
            parse_float=lambda _: _fail("control journal contains a float"),
            parse_constant=lambda _: _fail("control journal contains a non-JSON constant"),
        )
    except TimingControlError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TimingControlError("control journal record is not strict JSON") from exc
    if type(value) is not dict or _canonical_json(value) != payload + b"\n":
        _fail("control journal record is not canonically encoded")
    return value


def _normalize_control_text(value: bytes, label: str) -> str:
    if type(value) is not bytes or not value or len(value) > MAX_CONTROL_TEXT_BYTES:
        _fail(label + " bytes are empty or oversized")
    try:
        text = value.decode("ascii", "strict").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise TimingControlError(label + " is not ASCII") from exc
    if not text or "\0" in text or "\r" in text or "\n" in text:
        _fail(label + " is not one canonical text line")
    return text


def _parse_cpu_list(text: str) -> Tuple[int, ...]:
    retained: List[int] = []
    for token in text.split(","):
        pieces = token.split("-")
        if len(pieces) == 1 and pieces[0].isdigit():
            retained.append(int(pieces[0]))
        elif len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
            low, high = map(int, pieces)
            if low > high or high > 4095:
                _fail("CPU-list range is invalid")
            retained.extend(range(low, high + 1))
        else:
            _fail("CPU list has invalid syntax")
    if not retained or tuple(retained) != tuple(sorted(set(retained))):
        _fail("CPU list is empty, duplicate, or unordered")
    return tuple(retained)


def _parse_cpu_mask(text: str) -> Tuple[int, ...]:
    chunks = text.split(",")
    if (
        not chunks or len(chunks) > 128
        or any(re.fullmatch(r"[0-9a-f]{8}", chunk) is None for chunk in chunks)
    ):
        _fail("CPU mask is not canonical lowercase 32-bit chunks")
    retained = []
    for word_index, chunk in enumerate(reversed(chunks)):
        word = int(chunk, 16)
        retained.extend(
            word_index * 32 + bit for bit in range(32) if word & (1 << bit)
        )
    if not retained or retained[-1] > 4095:
        _fail("CPU mask is empty or exceeds the CPU bound")
    return tuple(retained)


def parse_control_value(text: str, parser: str) -> Any:
    if parser == "text":
        return text
    if parser == "integer":
        if not text.isdigit():
            _fail("integer control has nondecimal syntax")
        return int(text)
    if parser == "cpu_list":
        return _parse_cpu_list(text)
    if parser == "cpu_mask":
        return _parse_cpu_mask(text)
    _fail("control parser is unsupported")


def _approved_control_write_path(path: Any) -> bool:
    if type(path) is not str or not os.path.isabs(path) or os.path.normpath(path) != path:
        return False
    return any(path.startswith(prefix) for prefix in (
        "/sys/devices/system/cpu/", "/proc/irq/", "/sys/fs/cgroup/",
    ))


class HostBackend:
    """Small production backend; tests inject an in-memory implementation."""

    external_recovery_supported = True

    def __init__(self, sudo_argv: Sequence[str]) -> None:
        self.sudo_argv = tuple(sudo_argv)
        if self.sudo_argv != ("/usr/bin/sudo", "-n"):
            _fail("production backend requires exact noninteractive sudo prefix")
        self._dma_descriptor: Optional[int] = None

    def _run(self, argv: Sequence[str], *, stdin: bytes = b"") -> bytes:
        if not argv or not os.path.isabs(argv[0]):
            _fail("host command executable is not absolute")
        try:
            result = subprocess.run(
                list(argv), input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                cwd="/", timeout=30, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TimingControlError("host command failed to execute") from exc
        if len(result.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(result.stderr) > MAX_COMMAND_OUTPUT_BYTES:
            _fail("host command output exceeds the resource bound")
        if result.returncode != 0:
            diagnostic = result.stderr.decode("utf-8", "replace")[:512].strip()
            _fail("host command exited nonzero: " + diagnostic)
        return result.stdout

    def require_privilege(self) -> None:
        self._run(self.sudo_argv + ("/usr/bin/true",))
        # The current source has no separately frozen SCM_RIGHTS helper for
        # the root-only DMA-latency and MSR descriptors.  Refuse before the
        # first mutation unless this exact process already has the authority
        # to hold/read those descriptors.  A future helper must be profile-
        # and source-bound rather than inferred from a NOPASSWD tee rule.
        if os.geteuid() != 0:
            _fail(
                "root-only DMA/MSR descriptor authority requires an audited "
                "privileged helper; sudo -n command access alone is insufficient"
            )

    def read_text(self, path: str) -> str:
        return _normalize_control_text(self.read_raw(path), "control path")

    def read_raw(self, path: str, maximum_bytes: int = MAX_CONTROL_TEXT_BYTES) -> bytes:
        if (
            type(maximum_bytes) is not int or maximum_bytes <= 0
            or maximum_bytes > MAX_COMMAND_OUTPUT_BYTES
        ):
            _fail("raw-read byte bound is invalid")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                _fail("control path is not a regular sysfs/procfs file")
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(4096, maximum_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum_bytes:
                    _fail("raw control bytes exceed the resource bound")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def write_text(self, path: str, value: str) -> None:
        if not _approved_control_write_path(path):
            _fail("control write path is outside the approved host surfaces")
        if not value or any(character in value for character in "\0\r\n"):
            _fail("control write value is not one text line")
        self._run(
            self.sudo_argv + ("/usr/bin/tee", "--", path),
            stdin=value.encode("ascii", "strict") + b"\n",
        )

    def module_loaded(self, name: str) -> bool:
        if name not in ALLOWED_MODULES:
            _fail("module is outside the approved telemetry set")
        return os.path.isdir("/sys/module/" + name)

    def service_active(self, name: str) -> bool:
        if name != ALLOWED_SERVICE:
            _fail("service is outside the approved timing service")
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", "--quiet", name],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            cwd="/", timeout=10, check=False,
        )
        if result.returncode not in (0, 3):
            _fail("service state query failed")
        return result.returncode == 0

    def command(self, argv: Sequence[str]) -> None:
        retained = tuple(argv)
        if retained[:2] != self.sudo_argv:
            _fail("host mutation command lacks the exact sudo -n prefix")
        command = retained[2:]
        permitted = False
        if len(command) in (3, 4) and command[0] == "/usr/sbin/modprobe":
            if command[1:] == ("--", command[-1]) and command[-1] in ALLOWED_MODULES:
                permitted = True
            if (
                len(command) == 4 and command[1:3] == ("-r", "--")
                and command[-1] in ALLOWED_MODULES
            ):
                permitted = True
        if (
            len(command) == 3 and command[0] == "/usr/bin/systemctl"
            and command[1] in ("start", "stop") and command[2] == ALLOWED_SERVICE
        ):
            permitted = True
        if not permitted:
            _fail("host mutation command is outside the approved exact set")
        self._run(retained)

    def affinity(self) -> Tuple[int, ...]:
        return tuple(sorted(os.sched_getaffinity(0)))

    def set_affinity(self, cpus: Iterable[int]) -> None:
        retained = tuple(cpus)
        if not retained:
            _fail("empty affinity is forbidden")
        os.sched_setaffinity(0, retained)
        if self.affinity() != tuple(sorted(retained)):
            _fail("process affinity did not take effect exactly")

    def hold_dma_latency(self, value_us: int) -> None:
        if self._dma_descriptor is not None:
            _fail("DMA-latency descriptor is already held")
        if os.geteuid() != 0:
            _fail("DMA-latency hold requires a source-bound privileged descriptor helper")
        descriptor = os.open("/dev/cpu_dma_latency", os.O_RDWR | os.O_CLOEXEC)
        try:
            payload = struct.pack("=i", value_us)
            if os.write(descriptor, payload) != len(payload):
                _fail("DMA-latency write was incomplete")
        except BaseException:
            os.close(descriptor)
            raise
        self._dma_descriptor = descriptor

    def dma_latency_active(self) -> bool:
        return self._dma_descriptor is not None

    def release_dma_latency(self) -> None:
        if self._dma_descriptor is not None:
            os.close(self._dma_descriptor)
            self._dma_descriptor = None

    def smt_sibling_groups(self, cpus: Sequence[int]) -> Tuple[Tuple[int, ...], ...]:
        requested = tuple(cpus)
        groups = set()
        for cpu in requested:
            path = "/sys/devices/system/cpu/cpu{}/topology/thread_siblings_list".format(cpu)
            group = _parse_cpu_list(self.read_text(path))
            if cpu not in group:
                _fail("CPU topology surface omits its owning CPU")
            groups.add(group)
        return tuple(sorted(groups))

    def read_telemetry_raw(self, specification: "RawTelemetrySpec") -> bytes:
        if specification.source_kind == "msr_u64_le":
            if os.geteuid() != 0:
                _fail("MSR read requires a source-bound privileged descriptor helper")
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(specification.path, flags)
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISCHR(status.st_mode):
                    _fail("MSR telemetry path is not a character device")
                payload = os.pread(descriptor, 8, specification.register)
                if len(payload) != 8:
                    _fail("MSR telemetry read did not return exactly eight bytes")
                return payload
            finally:
                os.close(descriptor)
        return self.read_raw(specification.path, 128)


@dataclass(frozen=True)
class ControlSnapshot:
    values: Tuple[Tuple[str, str, str], ...]
    modules_preexisting: Tuple[str, ...]
    services_active: Tuple[str, ...]
    process_affinity: Tuple[int, ...]

    def as_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "record_type": "cp2_timing_control_prior_state",
            "values": [
                {"path": path, "parser": parser, "text": text}
                for path, parser, text in self.values
            ],
            "modules_preexisting": list(self.modules_preexisting),
            "services_active": list(self.services_active),
            "process_affinity": list(self.process_affinity),
        }


@dataclass(frozen=True)
class ControlStateReceipt:
    profile_sha256: str
    phase: str
    captured_monotonic_ns: int
    state: Mapping[str, Any]
    _state_bytes: bytes = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.profile_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.profile_sha256) is None:
            _fail("control-state receipt profile SHA-256 is invalid")
        if self.phase not in ("prior", "applied", "restored"):
            _fail("control-state receipt phase is invalid")
        _plain_u64(self.captured_monotonic_ns, "control-state receipt instant")
        if type(self.state) is not dict:
            _fail("control-state receipt state is not one exact mapping")
        canonical = _canonical_json(self.state)
        object.__setattr__(self, "_state_bytes", canonical)

    @property
    def state_sha256(self) -> str:
        return hashlib.sha256(self._state_bytes).hexdigest()

    def as_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "record_type": "cp2_timing_control_state_receipt",
            "profile_sha256": self.profile_sha256,
            "phase": self.phase,
            "captured_monotonic_ns": self.captured_monotonic_ns,
            "state": json.loads(self._state_bytes.decode("utf-8")),
            "state_sha256": self.state_sha256,
            "passed": True,
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.as_record())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def _snapshot_from_record(value: Any) -> ControlSnapshot:
    expected = {
        "schema_version", "record_type", "values", "modules_preexisting",
        "services_active", "process_affinity",
    }
    if type(value) is not dict or set(value) != expected:
        _fail("control snapshot journal keys differ")
    if value["schema_version"] != 1 or value["record_type"] != "cp2_timing_control_prior_state":
        _fail("control snapshot journal identity differs")
    retained_values: List[Tuple[str, str, str]] = []
    if type(value["values"]) is not list:
        _fail("control snapshot values are not a list")
    for item in value["values"]:
        if type(item) is not dict or set(item) != {"path", "parser", "text"}:
            _fail("control snapshot value keys differ")
        if not os.path.isabs(item["path"]) or os.path.normpath(item["path"]) != item["path"]:
            _fail("control snapshot path is not exact absolute syntax")
        parse_control_value(item["text"], item["parser"])
        retained_values.append((item["path"], item["parser"], item["text"]))
    modules = value["modules_preexisting"]
    services = value["services_active"]
    affinity = value["process_affinity"]
    if (
        type(modules) is not list or tuple(modules) != tuple(sorted(set(modules)))
        or any(item not in ALLOWED_MODULES for item in modules)
    ):
        _fail("control snapshot module population is not canonical")
    if (
        type(services) is not list or tuple(services) != tuple(sorted(set(services)))
        or any(item != ALLOWED_SERVICE for item in services)
    ):
        _fail("control snapshot service population is not canonical")
    if type(affinity) is not list:
        _fail("control snapshot affinity is not a list")
    cpus = tuple(_plain_u64(cpu, "snapshot affinity CPU") for cpu in affinity)
    if not cpus or cpus != tuple(sorted(set(cpus))) or cpus[-1] > 4095:
        _fail("control snapshot affinity is not sorted, unique, and bounded")
    return ControlSnapshot(tuple(retained_values), tuple(modules), tuple(services), cpus)


class _JournalHandle:
    """Held-inode, append-only, hash-chained recovery journal."""

    def __init__(
        self, parent_fd: int, file_fd: int, parent: str, name: str,
        profile_sha256: str, transaction_id: str,
    ) -> None:
        self.parent_fd = parent_fd
        self.file_fd = file_fd
        self.parent = parent
        self.name = name
        self.path = os.path.join(parent, name)
        self.profile_sha256 = profile_sha256
        self.transaction_id = transaction_id
        status = os.fstat(file_fd)
        self.device = status.st_dev
        self.inode = status.st_ino
        self.next_sequence = 0
        self.previous_sha256 = "0" * 64
        self.closed = False

    @classmethod
    def create(
        cls, parent: str, profile_sha256: str, snapshot: ControlSnapshot,
    ) -> "_JournalHandle":
        parent_fd = _open_absolute_directory_nofollow(parent)
        file_fd = -1
        name = ""
        try:
            for _attempt in range(128):
                transaction_id = os.urandom(16).hex()
                name = "schurvio-cp2e-control-{}.jsonl".format(transaction_id)
                flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    file_fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
                    break
                except FileExistsError:
                    continue
            if file_fd < 0:
                _fail("could not allocate a unique recovery journal")
            os.fchmod(file_fd, 0o600)
            status = os.fstat(file_fd)
            if (
                not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or stat.S_IMODE(status.st_mode) != 0o600 or status.st_uid != os.geteuid()
            ):
                _fail("recovery journal inode metadata is unsafe")
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle = cls(parent_fd, file_fd, parent, name, profile_sha256, transaction_id)
            handle.append("prior_state_captured", snapshot.as_record())
            os.fsync(parent_fd)
            return handle
        except BaseException:
            if file_fd >= 0:
                os.close(file_fd)
            if name:
                try:
                    os.unlink(name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError:
                    pass
            os.close(parent_fd)
            raise

    def _verify_name(self) -> None:
        if self.closed:
            _fail("recovery journal handle is closed")
        status = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        held = os.fstat(self.file_fd)
        if (
            status.st_dev != self.device or status.st_ino != self.inode
            or held.st_dev != self.device or held.st_ino != self.inode
            or status.st_nlink != 1 or held.st_nlink != 1
            or not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_uid != os.geteuid()
        ):
            _fail("recovery journal name no longer resolves to its held safe inode")

    def append(self, event: str, snapshot: Optional[Mapping[str, Any]] = None) -> None:
        if (
            type(event) is not str or not event or len(event) > 192
            or re.fullmatch(r"[a-z0-9_.:-]+", event) is None
        ):
            _fail("recovery journal event name is invalid")
        self._verify_name()
        record = {
            "event": event,
            "previous_sha256": self.previous_sha256,
            "profile_sha256": self.profile_sha256,
            "record_type": "cp2_timing_control_journal_event",
            "schema_version": 1,
            "sequence": self.next_sequence,
            "snapshot": snapshot,
            "transaction_id": self.transaction_id,
        }
        payload = _canonical_json(record)
        if os.lseek(self.file_fd, 0, os.SEEK_END) + len(payload) > MAX_JOURNAL_BYTES:
            _fail("recovery journal exceeds its byte bound")
        _write_all(self.file_fd, payload)
        os.fsync(self.file_fd)
        self.previous_sha256 = hashlib.sha256(payload).hexdigest()
        self.next_sequence += 1
        if self.next_sequence > MAX_JOURNAL_RECORDS:
            _fail("recovery journal exceeds its record bound")

    def records(self) -> Tuple[Mapping[str, Any], ...]:
        status = os.fstat(self.file_fd)
        if status.st_size <= 0 or status.st_size > MAX_JOURNAL_BYTES:
            _fail("recovery journal byte count is invalid")
        payload = os.pread(self.file_fd, status.st_size, 0)
        if len(payload) != status.st_size or not payload.endswith(b"\n"):
            _fail("recovery journal read is incomplete")
        lines = payload.splitlines(keepends=True)
        if not lines or len(lines) > MAX_JOURNAL_RECORDS:
            _fail("recovery journal record count is invalid")
        previous = "0" * 64
        records: List[Mapping[str, Any]] = []
        exact_keys = {
            "event", "previous_sha256", "profile_sha256", "record_type",
            "schema_version", "sequence", "snapshot", "transaction_id",
        }
        for sequence, line in enumerate(lines):
            if not line.endswith(b"\n"):
                _fail("recovery journal has an unterminated record")
            record = _strict_journal_json(line[:-1])
            if set(record) != exact_keys:
                _fail("recovery journal record keys differ")
            if (
                record["schema_version"] != 1
                or record["record_type"] != "cp2_timing_control_journal_event"
                or record["profile_sha256"] != self.profile_sha256
                or record["transaction_id"] != self.transaction_id
                or record["sequence"] != sequence
                or record["previous_sha256"] != previous
            ):
                _fail("recovery journal identity or hash chain differs")
            if sequence == 0:
                if record["event"] != "prior_state_captured" or record["snapshot"] is None:
                    _fail("recovery journal lacks its unique prior-state record")
                _snapshot_from_record(record["snapshot"])
            elif record["snapshot"] is not None:
                _fail("recovery journal repeats the prior-state snapshot")
            previous = hashlib.sha256(line).hexdigest()
            records.append(record)
        return tuple(records)

    def refresh_chain(self) -> None:
        records = self.records()
        self.next_sequence = len(records)
        status = os.fstat(self.file_fd)
        payload = os.pread(self.file_fd, status.st_size, 0)
        self.previous_sha256 = hashlib.sha256(payload.splitlines(keepends=True)[-1]).hexdigest()

    def remove(self) -> None:
        self._verify_name()
        os.unlink(self.name, dir_fd=self.parent_fd)
        os.fsync(self.parent_fd)
        self.close()

    def close(self) -> None:
        if not self.closed:
            os.close(self.file_fd)
            os.close(self.parent_fd)
            self.closed = True


def _verify_snapshot_exact(
    profile: Mapping[str, Any], snapshot: ControlSnapshot, backend: Any,
    *, process_local: bool,
) -> List[str]:
    errors: List[str] = []
    for path, parser, prior in snapshot.values:
        try:
            observed = backend.read_text(path)
            parse_control_value(observed, parser)
            if observed != prior:
                raise TimingControlError("restored text differs byte-for-byte")
        except BaseException as exc:
            errors.append("verify value {}: {}".format(path, exc))
    expected_modules = set(snapshot.modules_preexisting)
    for action in profile["controls"]["module_actions"]:
        try:
            if bool(backend.module_loaded(action["name"])) != (action["name"] in expected_modules):
                raise TimingControlError("module state differs from prior")
        except BaseException as exc:
            errors.append("verify module {}: {}".format(action["name"], exc))
    expected_services = set(snapshot.services_active)
    for action in profile["controls"]["service_actions"]:
        try:
            if bool(backend.service_active(action["name"])) != (action["name"] in expected_services):
                raise TimingControlError("service state differs from prior")
        except BaseException as exc:
            errors.append("verify service {}: {}".format(action["name"], exc))
    if process_local:
        try:
            if tuple(backend.affinity()) != snapshot.process_affinity:
                raise TimingControlError("process affinity differs from prior")
        except BaseException as exc:
            errors.append("verify affinity: " + str(exc))
    return errors


def _recover_snapshot_exact(
    profile: Mapping[str, Any], snapshot: ControlSnapshot, backend: Any,
    *, process_local: bool,
    boundary: Optional[Callable[[str], None]] = None,
) -> None:
    """Best-effort every restoration action, then prove the complete prior state."""

    errors: List[str] = []

    def operation(label: str, function: Callable[[], None]) -> None:
        if boundary is not None:
            try:
                boundary("before_" + label)
            except BaseException as exc:
                errors.append("before_" + label + ": " + str(exc))
        try:
            function()
        except BaseException as exc:
            errors.append(label + ": " + str(exc))
        if boundary is not None:
            try:
                boundary("after_" + label)
            except BaseException as exc:
                errors.append("after_" + label + ": " + str(exc))

    if process_local:
        operation("dma_latency_release", backend.release_dma_latency)
        operation("process_affinity", lambda: backend.set_affinity(snapshot.process_affinity))
    prior_by_path = {path: (parser, text) for path, parser, text in snapshot.values}
    for index, write in reversed(tuple(enumerate(profile["controls"]["writes"]))):
        parser, prior = prior_by_path[write["path"]]

        def restore_write(path: str = write["path"], value: str = prior, parser_name: str = parser) -> None:
            backend.write_text(path, value)
            observed = backend.read_text(path)
            parse_control_value(observed, parser_name)
            if observed != value:
                _fail("restored host-control text differs exactly")

        operation("write_{}".format(index), restore_write)
    prior_services = set(snapshot.services_active)
    for index, action in reversed(tuple(enumerate(profile["controls"]["service_actions"]))):
        should_be_active = action["name"] in prior_services

        def restore_service(item: Mapping[str, Any] = action, active: bool = should_be_active) -> None:
            observed = backend.service_active(item["name"])
            if observed != active:
                backend.command(item["activate_argv"] if active else item["deactivate_argv"])
            if backend.service_active(item["name"]) != active:
                _fail("service did not return to its exact prior state")

        operation("service_{}".format(index), restore_service)
    prior_modules = set(snapshot.modules_preexisting)
    for index, action in reversed(tuple(enumerate(profile["controls"]["module_actions"]))):
        should_be_loaded = action["name"] in prior_modules

        def restore_module(item: Mapping[str, Any] = action, loaded: bool = should_be_loaded) -> None:
            observed = backend.module_loaded(item["name"])
            if observed != loaded:
                backend.command(item["modprobe_argv"] if loaded else item["remove_argv"])
            if backend.module_loaded(item["name"]) != loaded:
                _fail("module did not return to its exact prior state")

        operation("module_{}".format(index), restore_module)
    errors.extend(_verify_snapshot_exact(profile, snapshot, backend, process_local=process_local))
    if errors:
        raise TimingControlIndeterminate("; ".join(errors))


class _ExternalRecoveryGuardian:
    """Forked guardian that restores global controls if the caller disappears."""

    def __init__(
        self, profile: Mapping[str, Any], snapshot: ControlSnapshot,
        backend: Any, journal: _JournalHandle,
    ) -> None:
        if not hasattr(os, "fork") or threading.active_count() != 1:
            _fail("external recovery guardian requires one Linux main thread")
        flags = getattr(os, "O_CLOEXEC", 0)
        control_read, control_write = os.pipe2(flags)
        status_read, status_write = os.pipe2(flags)
        try:
            child = os.fork()
        except BaseException:
            os.close(control_read)
            os.close(control_write)
            os.close(status_read)
            os.close(status_write)
            raise
        if child == 0:  # pragma: no cover - exercised through parent-visible filesystem tests
            os.close(control_write)
            os.close(status_read)
            status_payload = b"Eguardian did not complete"
            try:
                command = os.read(control_read, 1)
                if command == b"C":
                    status_payload = b"C"
                elif command in (b"R", b""):
                    journal_error: Optional[BaseException] = None
                    try:
                        journal.refresh_chain()
                        journal.append("guardian_recovery_started")
                    except BaseException as exc:
                        journal_error = exc
                    _recover_snapshot_exact(profile, snapshot, backend, process_local=False)
                    try:
                        journal.refresh_chain()
                        journal.append("guardian_recovery_verified")
                    except BaseException as exc:
                        journal_error = exc
                    status_payload = b"O" if journal_error is None else (
                        "Ehost restored but guardian journal failed: " + str(journal_error)
                    )[:4096].encode("utf-8", "replace")
                else:
                    status_payload = b"Eguardian command is invalid"
            except BaseException as exc:
                try:
                    journal.refresh_chain()
                    journal.append("guardian_recovery_failed")
                except BaseException:
                    pass
                status_payload = ("E" + str(exc))[:4096].encode("utf-8", "replace")
            try:
                _write_all(status_write, status_payload)
            except BaseException:
                pass
            os.close(control_read)
            os.close(status_write)
            journal.close()
            os._exit(0 if status_payload[:1] in (b"C", b"O") else 1)
        os.close(control_read)
        os.close(status_write)
        self.pid = child
        self.control_fd = control_write
        self.status_fd = status_read
        self.finished = False

    def assert_alive(self) -> None:
        if self.finished:
            _fail("external recovery guardian is no longer active")
        found, status = os.waitpid(self.pid, os.WNOHANG)
        if found:
            self.finished = True
            _fail("external recovery guardian exited early with status {}".format(status))

    def _finish(self, command: bytes) -> bytes:
        if self.finished:
            _fail("external recovery guardian already finished")
        try:
            _write_all(self.control_fd, command)
        except BrokenPipeError:
            pass
        os.close(self.control_fd)
        _, status = os.waitpid(self.pid, 0)
        payload = b""
        while len(payload) <= 4096:
            chunk = os.read(self.status_fd, 4097 - len(payload))
            if not chunk:
                break
            payload += chunk
        os.close(self.status_fd)
        self.finished = True
        if len(payload) > 4096 or not os.WIFEXITED(status):
            _fail("external recovery guardian completion is malformed")
        return payload

    def cancel(self) -> None:
        if self._finish(b"C") != b"C":
            _fail("external recovery guardian did not acknowledge cancellation")

    def recover(self) -> None:
        payload = self._finish(b"R")
        if payload != b"O":
            diagnostic = payload[1:].decode("utf-8", "replace") if payload[:1] == b"E" else "malformed status"
            raise TimingControlIndeterminate("external recovery guardian failed: " + diagnostic)


class HostControlTransaction:
    """Exact capture/apply/validate/restore state machine for one frozen profile."""

    def __init__(
        self, profile: cp2_timing_profile.FrozenTimingProfile,
        backend: Optional[HostBackend] = None, *,
        fault_injector: Optional[Callable[[str], None]] = None,
        enable_guardian: Optional[bool] = None,
    ) -> None:
        if type(profile) is not cp2_timing_profile.FrozenTimingProfile:
            _fail("host controls require a fully validated FrozenTimingProfile")
        validated = cp2_timing_profile.load_profile_bytes(profile.canonical_bytes)
        if validated.sha256 != profile.sha256:
            _fail("validated timing-profile identity changed")
        self.frozen_profile = validated
        control_profile = self.profile["controls"]
        self.backend = backend if backend is not None else HostBackend(control_profile["noninteractive_sudo_argv"])
        self.snapshot: Optional[ControlSnapshot] = None
        self.prior_receipt: Optional[ControlStateReceipt] = None
        self.applied_receipt: Optional[ControlStateReceipt] = None
        self.restored_receipt: Optional[ControlStateReceipt] = None
        self.journal_path: Optional[str] = None
        self.state = "new"
        self.had_abnormal_recovery = False
        self.boundary_history: List[str] = []
        self._fault_injector = fault_injector
        supported = bool(getattr(self.backend, "external_recovery_supported", False))
        self._guardian_enabled = supported if enable_guardian is None else enable_guardian
        if self._guardian_enabled and not supported:
            _fail("requested backend cannot support process-external recovery")
        self._journal: Optional[_JournalHandle] = None
        self._guardian: Optional[_ExternalRecoveryGuardian] = None
        self._old_signal_handlers: Dict[int, Any] = {}

    @property
    def profile(self) -> Mapping[str, Any]:
        # Never expose the nested mutable mapping used for a control decision.
        # Each access is reconstructed from the immutable, hash-bound bytes.
        return cp2_timing_profile.load_profile_bytes(
            self.frozen_profile.canonical_bytes
        ).value

    def _checkpoint(self, name: str, *, journal: bool = True) -> None:
        if journal and self._journal is not None:
            self._journal.append(name.replace("/", ":"))
        self.boundary_history.append(name)
        if self._guardian is not None and name.startswith("apply:"):
            self._guardian.assert_alive()
        if self._fault_injector is not None:
            self._fault_injector(name)

    def _capture(self) -> ControlSnapshot:
        values = []
        for write in self.profile["controls"]["writes"]:
            text = self.backend.read_text(write["path"])
            parse_control_value(text, write["parser"])
            values.append((write["path"], write["parser"], text))
        modules = tuple(sorted(
            action["name"] for action in self.profile["controls"]["module_actions"]
            if self.backend.module_loaded(action["name"])
        ))
        services = tuple(sorted(
            action["name"] for action in self.profile["controls"]["service_actions"]
            if self.backend.service_active(action["name"])
        ))
        affinity = tuple(self.backend.affinity())
        if not affinity or affinity != tuple(sorted(set(affinity))):
            _fail("captured process affinity is empty or noncanonical")
        return ControlSnapshot(tuple(values), modules, services, affinity)

    def _validate_smt_topology(self) -> None:
        method = getattr(self.backend, "smt_sibling_groups", None)
        if method is None:
            return
        expected = tuple(tuple(group) for group in self.profile["runtime"]["smt_sibling_groups"])
        observed = tuple(method(self.profile["runtime"]["cpu_ids"]))
        if tuple(sorted(observed)) != tuple(sorted(expected)):
            _fail("host SMT sibling topology differs from the frozen placement")

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            _fail("host-control signal restoration requires the main thread")

        def handler(signum: int, _frame: Any) -> None:
            self.restore()
            raise KeyboardInterrupt("received signal {}".format(signum))

        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            self._old_signal_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)

    def _restore_signal_handlers(self) -> None:
        for signum, previous in self._old_signal_handlers.items():
            signal.signal(signum, previous)
        self._old_signal_handlers.clear()

    def _mutation(self, label: str, function: Callable[[], None]) -> None:
        self._checkpoint("apply:before_" + label)
        function()
        self._checkpoint("apply:after_" + label)

    def apply(self) -> ControlSnapshot:
        if self.state != "new":
            _fail("host-control transaction cannot be applied from state " + self.state)
        rebound = cp2_timing_profile.load_profile_bytes(self.frozen_profile.canonical_bytes)
        if rebound.sha256 != self.frozen_profile.sha256 or rebound.value != self.profile:
            _fail("timing profile mutated after validated transaction construction")
        self._checkpoint("apply:before_privilege", journal=False)
        self.backend.require_privilege()
        self._checkpoint("apply:after_privilege", journal=False)
        self._validate_smt_topology()
        self._checkpoint("apply:before_capture", journal=False)
        snapshot = self._capture()
        self.snapshot = snapshot
        self.prior_receipt = ControlStateReceipt(
            self.frozen_profile.sha256, "prior", time.monotonic_ns(),
            dict(snapshot.as_record()),
        )
        self.state = "captured"
        try:
            self._checkpoint("apply:after_capture", journal=False)
        except BaseException:
            self.snapshot = None
            self.prior_receipt = None
            self.state = "new"
            raise
        try:
            self._checkpoint("apply:before_journal", journal=False)
            self._journal = _JournalHandle.create(
                self.profile["controls"]["recovery_journal_parent"],
                self.frozen_profile.sha256, snapshot,
            )
            self.journal_path = self._journal.path
            self.state = "journaled"
            self._checkpoint("apply:after_journal")
            if self._guardian_enabled:
                self._checkpoint("apply:before_guardian")
                self._guardian = _ExternalRecoveryGuardian(
                    self.profile, snapshot, self.backend, self._journal,
                )
                self._checkpoint("apply:after_guardian")
            self._install_signal_handlers()
            self.state = "applying"
            for index, action in enumerate(self.profile["controls"]["module_actions"]):
                if action["name"] not in snapshot.modules_preexisting:
                    self._mutation(
                        "module_{}".format(index),
                        lambda item=action: self.backend.command(item["modprobe_argv"]),
                    )
                    if not self.backend.module_loaded(action["name"]):
                        _fail("telemetry module did not become loaded")
            for index, action in enumerate(self.profile["controls"]["service_actions"]):
                if action["name"] in snapshot.services_active:
                    self._mutation(
                        "service_{}".format(index),
                        lambda item=action: self.backend.command(item["deactivate_argv"]),
                    )
                    if self.backend.service_active(action["name"]):
                        _fail("timing service did not become inactive")
            for index, write in enumerate(self.profile["controls"]["writes"]):
                self._mutation(
                    "write_{}".format(index),
                    lambda item=write: self.backend.write_text(item["path"], item["desired_text"]),
                )
                observed = self.backend.read_text(write["path"])
                parse_control_value(observed, write["parser"])
                if observed != write["desired_text"]:
                    _fail("host control did not assume its exact requested text")
            self._mutation(
                "process_affinity",
                lambda: self.backend.set_affinity(self.profile["runtime"]["cpu_ids"]),
            )
            self._mutation(
                "dma_latency_hold",
                lambda: self.backend.hold_dma_latency(self.profile["runtime"]["dma_latency_us"]),
            )
            self.state = "applied"
            self.validate_applied()
            self.applied_receipt = ControlStateReceipt(
                self.frozen_profile.sha256, "applied", time.monotonic_ns(),
                dict(self.capture_applied_state()),
            )
            self._checkpoint("apply:validated")
            return snapshot
        except BaseException as apply_error:
            try:
                self.restore()
            except BaseException as restore_error:
                raise TimingControlIndeterminate(
                    "host-control apply failed and exact restoration also failed: {} / {}".format(
                        apply_error, restore_error
                    )
                ) from restore_error
            raise

    def capture_applied_state(self) -> Mapping[str, Any]:
        if self.state != "applied":
            _fail("applied control state is unavailable outside the applied state")
        self.validate_applied()
        active_services = sorted(
            action["name"] for action in self.profile["controls"]["service_actions"]
            if self.backend.service_active(action["name"])
        )
        loaded_modules = sorted(
            action["name"] for action in self.profile["controls"]["module_actions"]
            if self.backend.module_loaded(action["name"])
        )
        return {
            "values": [
                {"path": item["path"], "parser": item["parser"], "text": self.backend.read_text(item["path"])}
                for item in self.profile["controls"]["writes"]
            ],
            "modules_loaded": loaded_modules,
            "services_active": active_services,
            "process_affinity": list(self.backend.affinity()),
            "dma_latency_us": self.profile["runtime"]["dma_latency_us"],
        }

    def validate_applied(self) -> None:
        if self.state not in ("applied", "restoring"):
            _fail("host controls are not in a validate-able state")
        self._validate_smt_topology()
        for write in self.profile["controls"]["writes"]:
            observed = self.backend.read_text(write["path"])
            parse_control_value(observed, write["parser"])
            if observed != write["desired_text"]:
                _fail("host control drifted from the exact frozen text")
        if tuple(self.backend.affinity()) != tuple(self.profile["runtime"]["cpu_ids"]):
            _fail("runner affinity drifted from the frozen profile")
        for action in self.profile["controls"]["module_actions"]:
            if not self.backend.module_loaded(action["name"]):
                _fail("required telemetry module is no longer loaded")
        for action in self.profile["controls"]["service_actions"]:
            if self.backend.service_active(action["name"]):
                _fail("timing service unexpectedly became active")
        dma_state = getattr(self.backend, "dma_latency_active", None)
        if dma_state is not None and not dma_state():
            _fail("DMA-latency descriptor is no longer held")

    def restore(self) -> None:
        if self.state == "restored":
            return
        if self.snapshot is None:
            return
        if self.state in ("indeterminate", "recovered_by_guardian") and self._journal is not None:
            try:
                self._journal.refresh_chain()
            except BaseException:
                # Restoration below remains mandatory even when the evidence
                # name/hash chain itself is damaged.
                pass
        self.state = "restoring"
        errors: List[str] = []

        def boundary(name: str) -> None:
            self._checkpoint("restore:" + name)

        try:
            self._checkpoint("restore:begin")
        except BaseException as exc:
            errors.append(str(exc))
        try:
            _recover_snapshot_exact(
                self.profile, self.snapshot, self.backend,
                process_local=True, boundary=boundary,
            )
        except BaseException as exc:
            errors.append(str(exc))
        if not errors:
            try:
                restored_snapshot = self._capture()
                if restored_snapshot != self.snapshot:
                    _fail("restored control snapshot differs from the captured prior state")
                self.restored_receipt = ControlStateReceipt(
                    self.frozen_profile.sha256, "restored", time.monotonic_ns(),
                    dict(restored_snapshot.as_record()),
                )
            except BaseException as exc:
                errors.append(str(exc))
        try:
            self._checkpoint("restore:state_verified")
        except BaseException as exc:
            errors.append(str(exc))
        self._restore_signal_handlers()
        if errors:
            self.had_abnormal_recovery = True
            recovered_by_guardian = False
            if self._guardian is not None and not self._guardian.finished:
                try:
                    self._guardian.recover()
                    recovered_by_guardian = True
                except BaseException as exc:
                    errors.append(str(exc))
            self.state = "recovered_by_guardian" if recovered_by_guardian else "indeterminate"
            # An abnormal recovery is never silently converted into passing
            # campaign evidence.  Its durable journal remains for diagnosis.
            raise TimingControlIndeterminate("; ".join(errors))
        try:
            if self._guardian is not None and not self._guardian.finished:
                self._checkpoint("restore:before_guardian_cancel")
                self._guardian.cancel()
                self._checkpoint("restore:after_guardian_cancel")
            if self._journal is not None:
                self._checkpoint("restore:before_journal_remove")
                self._journal.remove()
                self._journal = None
            self.journal_path = None
            self.state = "restored"
        except BaseException as exc:
            self.had_abnormal_recovery = True
            self.state = "indeterminate"
            raise TimingControlIndeterminate(
                "controls were restored but recovery evidence cleanup was indeterminate"
            ) from exc

    def __enter__(self) -> "HostControlTransaction":
        self.apply()
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> bool:
        self.restore()
        return False

    def control_receipts(self) -> Mapping[str, Any]:
        if (
            self.state != "restored" or self.had_abnormal_recovery
            or self.prior_receipt is None or self.applied_receipt is None
            or self.restored_receipt is None
        ):
            _fail("passing control receipts require one clean complete transaction")
        if self.prior_receipt._state_bytes != self.restored_receipt._state_bytes:
            _fail("prior and restored control states differ")
        return {
            "prior": self.prior_receipt,
            "applied": self.applied_receipt,
            "restored": self.restored_receipt,
        }


def snapshot_digest(snapshot: ControlSnapshot) -> str:
    return hashlib.sha256(_canonical_json(snapshot.as_record())).hexdigest()


def _control_path_categories(path: str) -> Tuple[str, ...]:
    categories: List[str] = []
    basename = os.path.basename(path)
    if basename == "scaling_governor":
        categories.append("governor")
    if basename == "scaling_min_freq":
        categories.append("minimum_frequency")
    if basename == "scaling_max_freq":
        categories.append("maximum_frequency")
    if basename in ("boost", "no_turbo"):
        categories.append("boost")
    if re.search(r"/cpu[0-9]+/online$", path):
        categories.append("cpu_online")
    if re.search(r"/cpu[0-9]+/cpuidle/state[0-9]+/disable$", path):
        categories.append("cpu_idle")
    if (
        path == "/proc/irq/default_smp_affinity"
        or re.fullmatch(r"/proc/irq/[0-9]+/smp_affinity(?:_list)?", path)
    ):
        categories.append("irq_affinity")
    if path.startswith("/sys/fs/cgroup/") and basename in (
        "cpuset.cpus", "cpuset.mems", "tasks", "cgroup.procs",
    ):
        categories.append("cpuset")
    return tuple(categories)


def validate_control_coverage(
    profile: cp2_timing_profile.FrozenTimingProfile,
) -> Mapping[str, Any]:
    """Return a canonical structural coverage audit for the E control profile.

    This deliberately does not claim that category presence proves complete
    per-CPU/per-IRQ population.  The formal profile builder must bind its
    inventory, and raw telemetry receipts must prove the runtime population.
    """

    if type(profile) is not cp2_timing_profile.FrozenTimingProfile:
        _fail("control coverage requires a FrozenTimingProfile")
    rebound = cp2_timing_profile.load_profile_bytes(profile.canonical_bytes)
    categories = {"process_affinity", "smt_sibling_placement", "dma_latency"}
    for write in rebound.value["controls"]["writes"]:
        categories.update(_control_path_categories(write["path"]))
    module_names = {
        action["name"] for action in rebound.value["controls"]["module_actions"]
    }
    categories.update(module_names)
    if any(
        action["name"] == "irqbalance"
        for action in rebound.value["controls"]["service_actions"]
    ):
        categories.add("irqbalance")
    for observation in rebound.value["observations"]:
        descriptor = " ".join(
            [observation["observation_id"]] + list(observation["argv"])
        ).lower()
        if observation["parser"] == "hwmon_temperature_millicelsius" or "temperature" in descriptor:
            categories.add("temperature_telemetry")
        if "scaling_cur_freq" in descriptor or "current_frequency" in descriptor:
            categories.add("frequency_telemetry")
        if "aperf" in descriptor:
            categories.add("aperf_telemetry")
        if "mperf" in descriptor:
            categories.add("mperf_telemetry")
        if "throttle" in descriptor:
            categories.add("throttle_telemetry")
    present = tuple(sorted(categories))
    missing = tuple(item for item in REQUIRED_CONTROL_COVERAGE if item not in categories)
    return {
        "schema_version": 1,
        "record_type": "cp2_timing_control_coverage",
        "profile_sha256": rebound.sha256,
        "required_categories": list(REQUIRED_CONTROL_COVERAGE),
        "present_categories": list(present),
        "missing_categories": list(missing),
        "scope": "structural_categories_only_runtime_population_requires_raw_receipts",
        "passed": not missing,
    }


def require_complete_control_coverage(
    profile: cp2_timing_profile.FrozenTimingProfile,
) -> Mapping[str, Any]:
    result = validate_control_coverage(profile)
    if result["passed"] is not True:
        _fail("timing control profile lacks required structural coverage: " + ",".join(result["missing_categories"]))
    return result


@dataclass(frozen=True)
class RawTelemetrySpec:
    metric_id: str
    role: str
    cpu_id: Optional[int]
    source_kind: str
    path: str
    register: Optional[int] = None

    def __post_init__(self) -> None:
        if type(self.metric_id) is not str or SAFE_METRIC_ID.fullmatch(self.metric_id) is None:
            _fail("raw telemetry metric ID is invalid")
        if self.role not in TELEMETRY_ROLES:
            _fail("raw telemetry role is unsupported")
        if self.cpu_id is not None and (
            type(self.cpu_id) is not int or self.cpu_id < 0 or self.cpu_id > 4095
        ):
            _fail("raw telemetry CPU ID is invalid")
        if type(self.path) is not str or not os.path.isabs(self.path) or os.path.normpath(self.path) != self.path:
            _fail("raw telemetry path is not normalized absolute syntax")
        if self.source_kind == "msr_u64_le":
            if self.cpu_id is None or self.path != "/dev/cpu/{}/msr".format(self.cpu_id):
                _fail("MSR telemetry path is not bound to its CPU")
            expected = MSR_APERF if self.role == "aperf" else MSR_MPERF if self.role == "mperf" else None
            if expected is None or self.register != expected:
                _fail("MSR telemetry register differs from APERF/MPERF")
        elif self.source_kind == "text_integer":
            if self.register is not None:
                _fail("text telemetry unexpectedly names an MSR register")
            prefixes = (
                "/sys/devices/system/cpu/", "/sys/class/hwmon/",
                "/sys/class/thermal/", "/proc/",
            )
            if not any(self.path.startswith(prefix) for prefix in prefixes):
                _fail("text telemetry path is outside approved read-only surfaces")
            if self.cpu_id is not None and self.role != "temperature_millicelsius":
                marker = "/cpu{}/".format(self.cpu_id)
                if marker not in self.path and not self.path.startswith("/proc/"):
                    _fail("per-CPU telemetry path is not bound to its CPU")
        else:
            _fail("raw telemetry source kind is unsupported")

    def as_record(self) -> Mapping[str, Any]:
        return {
            "metric_id": self.metric_id,
            "role": self.role,
            "cpu_id": self.cpu_id,
            "source_kind": self.source_kind,
            "path": self.path,
            "register": self.register,
        }


def _parse_raw_telemetry(specification: RawTelemetrySpec, raw: bytes) -> int:
    if type(raw) is not bytes or not raw or len(raw) > 128:
        _fail("raw telemetry payload is empty or oversized")
    if specification.source_kind == "msr_u64_le":
        if len(raw) != 8:
            _fail("raw MSR telemetry payload is not exactly eight bytes")
        return struct.unpack("<Q", raw)[0]
    try:
        text = raw.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise TimingControlError("raw text telemetry is not ASCII") from exc
    if text.endswith("\n"):
        text = text[:-1]
    if not text or any(character in text for character in "\0\r\n"):
        _fail("raw text telemetry is not one canonical line")
    signed = specification.role == "temperature_millicelsius"
    digits = text[1:] if signed and text.startswith("-") else text
    if not digits.isdigit() or (len(digits) > 1 and digits.startswith("0")):
        _fail("raw text telemetry is not canonical decimal syntax")
    value = int(text)
    if signed:
        if value < -273_150 or value > 1_000_000:
            _fail("temperature telemetry is outside its physical integer domain")
    else:
        _plain_u64(value, "raw telemetry value")
    return value


@dataclass(frozen=True)
class RawTelemetryReceipt:
    specification: RawTelemetrySpec
    phase: str
    started_monotonic_ns: int
    ended_monotonic_ns: int
    raw: bytes
    parsed_value: int

    def __post_init__(self) -> None:
        if self.phase not in ("pre", "post"):
            _fail("raw telemetry phase is not pre or post")
        started = _plain_u64(self.started_monotonic_ns, "telemetry read start")
        ended = _plain_u64(self.ended_monotonic_ns, "telemetry read end")
        if ended < started:
            _fail("telemetry read interval is reversed")
        if _parse_raw_telemetry(self.specification, self.raw) != self.parsed_value:
            _fail("raw telemetry parsed value differs from retained bytes")

    def as_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "record_type": "cp2_timing_raw_telemetry_receipt",
            "specification": self.specification.as_record(),
            "phase": self.phase,
            "started_monotonic_ns": self.started_monotonic_ns,
            "ended_monotonic_ns": self.ended_monotonic_ns,
            "raw_hex": self.raw.hex(),
            "raw_sha256": hashlib.sha256(self.raw).hexdigest(),
            "parsed_value": self.parsed_value,
        }


@dataclass(frozen=True)
class RawTelemetrySnapshot:
    phase: str
    receipts: Tuple[RawTelemetryReceipt, ...]

    def __post_init__(self) -> None:
        if self.phase not in ("pre", "post") or not self.receipts:
            _fail("raw telemetry snapshot phase or population is invalid")
        identifiers = tuple(receipt.specification.metric_id for receipt in self.receipts)
        if identifiers != tuple(sorted(set(identifiers))):
            _fail("raw telemetry receipt IDs are not sorted and unique")
        if any(receipt.phase != self.phase for receipt in self.receipts):
            _fail("raw telemetry receipt phase differs from its snapshot")

    def as_record(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1,
            "record_type": "cp2_timing_raw_telemetry_snapshot",
            "phase": self.phase,
            "receipts": [receipt.as_record() for receipt in self.receipts],
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.as_record())

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()


def capture_raw_telemetry(
    specifications: Sequence[RawTelemetrySpec], phase: str,
    backend: Optional[Any] = None,
) -> RawTelemetrySnapshot:
    retained = tuple(specifications)
    if not retained or any(type(item) is not RawTelemetrySpec for item in retained):
        _fail("raw telemetry specification population is invalid")
    if tuple(item.metric_id for item in retained) != tuple(sorted({item.metric_id for item in retained})):
        _fail("raw telemetry specifications are not sorted and unique")
    if backend is None:
        backend = HostBackend(("/usr/bin/sudo", "-n"))
    receipts: List[RawTelemetryReceipt] = []
    for specification in retained:
        started = time.monotonic_ns()
        raw = backend.read_telemetry_raw(specification)
        ended = time.monotonic_ns()
        parsed = _parse_raw_telemetry(specification, raw)
        receipts.append(RawTelemetryReceipt(specification, phase, started, ended, raw, parsed))
    return RawTelemetrySnapshot(phase, tuple(receipts))


def compare_raw_telemetry(
    pre: RawTelemetrySnapshot, post: RawTelemetrySnapshot,
    expected_cpu_ids: Sequence[int], accepted_frequency_khz: Sequence[int],
    temperature_range_millicelsius: Tuple[int, int],
    aperf_mperf_ratio_bounds: Tuple[Tuple[int, int], Tuple[int, int]],
) -> Mapping[str, Any]:
    if type(pre) is not RawTelemetrySnapshot or type(post) is not RawTelemetrySnapshot:
        _fail("raw telemetry comparison requires exact snapshot objects")
    if pre.phase != "pre" or post.phase != "post":
        _fail("raw telemetry snapshots are not pre/post ordered")
    cpus = tuple(expected_cpu_ids)
    if not cpus or cpus != tuple(sorted(set(cpus))) or any(type(cpu) is not int for cpu in cpus):
        _fail("raw telemetry expected CPU population is invalid")
    frequencies = tuple(accepted_frequency_khz)
    if not frequencies or frequencies != tuple(sorted(set(frequencies))):
        _fail("raw telemetry accepted frequencies are invalid")
    if (
        type(temperature_range_millicelsius) is not tuple
        or len(temperature_range_millicelsius) != 2
        or any(type(item) is not int for item in temperature_range_millicelsius)
        or temperature_range_millicelsius[0] >= temperature_range_millicelsius[1]
    ):
        _fail("raw telemetry temperature range is invalid")
    if (
        type(aperf_mperf_ratio_bounds) is not tuple
        or len(aperf_mperf_ratio_bounds) != 2
        or any(type(bound) is not tuple or len(bound) != 2 for bound in aperf_mperf_ratio_bounds)
    ):
        _fail("APERF/MPERF ratio bounds are not two exact rationals")
    ratio_bounds: List[Tuple[int, int]] = []
    for numerator, denominator in aperf_mperf_ratio_bounds:
        if (
            type(numerator) is not int or type(denominator) is not int
            or numerator <= 0 or denominator <= 0
        ):
            _fail("APERF/MPERF ratio bound is outside its positive rational domain")
        divisor = math.gcd(numerator, denominator)
        if divisor != 1:
            _fail("APERF/MPERF ratio bound is not reduced")
        ratio_bounds.append((numerator, denominator))
    low_ratio, high_ratio = ratio_bounds
    if low_ratio[0] * high_ratio[1] >= high_ratio[0] * low_ratio[1]:
        _fail("APERF/MPERF ratio interval is empty")
    if max(item.ended_monotonic_ns for item in pre.receipts) >= min(
        item.started_monotonic_ns for item in post.receipts
    ):
        _fail("raw telemetry pre/post capture intervals overlap or touch")
    pre_by_id = {item.specification.metric_id: item for item in pre.receipts}
    post_by_id = {item.specification.metric_id: item for item in post.receipts}
    if tuple(pre_by_id) != tuple(post_by_id):
        _fail("raw telemetry pre/post metric population differs")
    by_role_cpu: Dict[Tuple[str, Optional[int]], Tuple[RawTelemetryReceipt, RawTelemetryReceipt]] = {}
    for metric_id in pre_by_id:
        first = pre_by_id[metric_id]
        second = post_by_id[metric_id]
        if first.specification != second.specification:
            _fail("raw telemetry pre/post source identity differs")
        key = (first.specification.role, first.specification.cpu_id)
        if key in by_role_cpu:
            _fail("raw telemetry duplicates one role/CPU")
        by_role_cpu[key] = (first, second)
    for role, cpu in by_role_cpu:
        if role == "temperature_millicelsius":
            if cpu is not None and cpu not in cpus:
                _fail("raw temperature telemetry names an unexpected CPU")
        elif cpu not in cpus:
            _fail("raw telemetry contains an unexpected per-CPU metric")
    per_cpu = []
    for cpu in cpus:
        retained: Dict[str, Any] = {"cpu_id": cpu}
        for role in (
            "thermal_throttle_count", "aperf", "mperf",
            "scaling_current_frequency_khz",
        ):
            pair = by_role_cpu.get((role, cpu))
            if pair is None:
                _fail("raw telemetry omits {} for CPU {}".format(role, cpu))
            before, after = pair[0].parsed_value, pair[1].parsed_value
            if role == "thermal_throttle_count" and after != before:
                _fail("thermal throttle counter changed")
            if role in ("aperf", "mperf") and after <= before:
                _fail(role.upper() + " did not advance without wrap")
            if role == "scaling_current_frequency_khz" and (
                before not in frequencies or after not in frequencies
            ):
                _fail("current frequency differs from the frozen accepted set")
            retained[role + "_pre"] = before
            retained[role + "_post"] = after
            if role in ("aperf", "mperf"):
                retained[role + "_delta"] = after - before
        aperf_delta = retained["aperf_delta"]
        mperf_delta = retained["mperf_delta"]
        if (
            aperf_delta * low_ratio[1] < low_ratio[0] * mperf_delta
            or aperf_delta * high_ratio[1] > high_ratio[0] * mperf_delta
        ):
            _fail("exact APERF/MPERF delta ratio is outside the frozen bounds")
        ratio_divisor = math.gcd(aperf_delta, mperf_delta)
        retained["aperf_mperf_ratio"] = [
            aperf_delta // ratio_divisor, mperf_delta // ratio_divisor,
        ]
        per_cpu.append(retained)
    temperatures = []
    for (role, cpu), pair in by_role_cpu.items():
        if role != "temperature_millicelsius":
            continue
        before, after = pair[0].parsed_value, pair[1].parsed_value
        low, high = temperature_range_millicelsius
        if not (low <= before <= high and low <= after <= high):
            _fail("temperature is outside the frozen inclusive range")
        temperatures.append({"cpu_id": cpu, "pre": before, "post": after})
    if not temperatures:
        _fail("raw telemetry has no bound temperature sensor")
    comparison = {
        "schema_version": 1,
        "record_type": "cp2_timing_raw_telemetry_comparison",
        "pre_snapshot_sha256": pre.sha256,
        "post_snapshot_sha256": post.sha256,
        "expected_cpu_ids": list(cpus),
        "accepted_frequency_khz": list(frequencies),
        "temperature_range_millicelsius": list(temperature_range_millicelsius),
        "aperf_mperf_ratio_bounds": [list(bound) for bound in ratio_bounds],
        "per_cpu": per_cpu,
        "temperatures": temperatures,
        "passed": True,
    }
    canonical = _canonical_json(comparison)
    return {
        "record": comparison,
        "canonical_bytes": canonical,
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _open_existing_journal(
    profile: cp2_timing_profile.FrozenTimingProfile, path: str,
) -> _JournalHandle:
    normalized = os.path.normpath(path)
    parent = profile.value["controls"]["recovery_journal_parent"]
    if type(path) is not str or normalized != path or os.path.dirname(path) != parent:
        _fail("recovery journal path is outside its frozen parent")
    name = os.path.basename(path)
    if JOURNAL_NAME.fullmatch(name) is None:
        _fail("recovery journal filename is invalid")
    parent_fd = _open_absolute_directory_nofollow(parent)
    flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_fd = os.open(name, flags, dir_fd=parent_fd)
    except BaseException:
        os.close(parent_fd)
        raise
    try:
        status = os.fstat(file_fd)
        if (
            not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
            or stat.S_IMODE(status.st_mode) != 0o600 or status.st_uid != os.geteuid()
        ):
            _fail("existing recovery journal inode metadata is unsafe")
        try:
            fcntl.flock(file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise TimingControlError("recovery journal belongs to a live transaction") from exc
        if status.st_size <= 0 or status.st_size > MAX_JOURNAL_BYTES:
            _fail("existing recovery journal byte count is invalid")
        payload = os.pread(file_fd, status.st_size, 0)
        first_line = payload.splitlines(keepends=True)[0]
        if not first_line.endswith(b"\n"):
            _fail("existing recovery journal first record is incomplete")
        first = _strict_journal_json(first_line[:-1])
        transaction_id = first.get("transaction_id")
        if type(transaction_id) is not str or re.fullmatch(r"[0-9a-f]{32}", transaction_id) is None:
            _fail("existing recovery journal transaction ID is invalid")
        handle = _JournalHandle(
            parent_fd, file_fd, parent, name, profile.sha256, transaction_id,
        )
        handle.refresh_chain()
        return handle
    except BaseException:
        os.close(file_fd)
        os.close(parent_fd)
        raise


def recover_control_journal(
    profile: cp2_timing_profile.FrozenTimingProfile, journal_path: str,
    backend: Optional[Any] = None,
) -> Mapping[str, Any]:
    """Recover a dead transaction; a live transaction's inode lock rejects."""

    if type(profile) is not cp2_timing_profile.FrozenTimingProfile:
        _fail("journal recovery requires a FrozenTimingProfile")
    rebound = cp2_timing_profile.load_profile_bytes(profile.canonical_bytes)
    if backend is None:
        backend = HostBackend(rebound.value["controls"]["noninteractive_sudo_argv"])
    handle = _open_existing_journal(rebound, journal_path)
    try:
        records = handle.records()
        snapshot = _snapshot_from_record(records[0]["snapshot"])
        backend.require_privilege()
        journal_errors: List[str] = []
        try:
            handle.append("manual_recovery_started")
        except BaseException as exc:
            journal_errors.append(str(exc))
        _recover_snapshot_exact(rebound.value, snapshot, backend, process_local=False)
        try:
            handle.refresh_chain()
            handle.append("manual_recovery_verified")
        except BaseException as exc:
            journal_errors.append(str(exc))
        if journal_errors:
            raise TimingControlIndeterminate(
                "host controls were recovered but journal proof failed: "
                + "; ".join(journal_errors)
            )
        receipt = {
            "schema_version": 1,
            "record_type": "cp2_timing_control_manual_recovery",
            "profile_sha256": rebound.sha256,
            "journal_path": journal_path,
            "snapshot_sha256": snapshot_digest(snapshot),
            "passed": True,
        }
        canonical = _canonical_json(receipt)
        return {
            "record": receipt,
            "canonical_bytes": canonical,
            "sha256": hashlib.sha256(canonical).hexdigest(),
        }
    except BaseException as exc:
        try:
            handle.refresh_chain()
            handle.append("manual_recovery_failed")
        except BaseException:
            pass
        if isinstance(exc, TimingControlIndeterminate):
            raise
        raise TimingControlIndeterminate("manual control recovery failed") from exc
    finally:
        handle.close()


class ProcessTreeAffinityMonitor:
    """Continuously audit every observable thread in one fresh process tree.

    Children inherit the launcher's exact mask.  This monitor makes any later
    task-level escape a permanent campaign failure.  It is an audit, not a
    cpuset substitute; profiles name that distinction explicitly.
    """

    def __init__(
        self,
        root_pid: int,
        expected_cpus: Sequence[int],
        *,
        poll_interval_ns: int = 1_000_000,
        maximum_tasks: int = 4096,
    ) -> None:
        if type(root_pid) is not int or root_pid <= 0:
            _fail("affinity monitor root PID is invalid")
        cpus = tuple(expected_cpus)
        if not cpus or cpus != tuple(sorted(set(cpus))):
            _fail("affinity monitor CPU set is not sorted and unique")
        if type(poll_interval_ns) is not int or poll_interval_ns <= 0 or poll_interval_ns > 100_000_000:
            _fail("affinity monitor poll interval is invalid")
        if type(maximum_tasks) is not int or maximum_tasks <= 0 or maximum_tasks > 65536:
            _fail("affinity monitor task bound is invalid")
        self.root_pid = root_pid
        self.expected_cpus = cpus
        self.poll_interval_ns = poll_interval_ns
        self.maximum_tasks = maximum_tasks
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._records: List[Tuple[int, int, Tuple[int, ...]]] = []
        self._root_observed = False

    @staticmethod
    def _task_ids(pid: int) -> Tuple[int, ...]:
        try:
            names = os.listdir("/proc/{}/task".format(pid))
        except FileNotFoundError:
            return ()
        tids = tuple(sorted(int(name) for name in names if name.isdigit()))
        return tids

    @staticmethod
    def _children(tid: int) -> Tuple[int, ...]:
        try:
            with open("/proc/{}/task/{}/children".format(tid, tid), "r", encoding="ascii") as stream:
                text = stream.read(64 * 1024).strip()
        except FileNotFoundError:
            return ()
        if not text:
            return ()
        if any(not token.isdigit() for token in text.split()):
            _fail("process-tree children surface is malformed")
        return tuple(int(token) for token in text.split())

    def _scan_once(self) -> bool:
        pending = [self.root_pid]
        seen_processes = set()
        total_tasks = 0
        root_present = False
        timestamp = time.monotonic_ns()
        while pending:
            pid = pending.pop()
            if pid in seen_processes:
                continue
            seen_processes.add(pid)
            tids = self._task_ids(pid)
            if pid == self.root_pid and tids:
                root_present = True
                self._root_observed = True
            for tid in tids:
                total_tasks += 1
                if total_tasks > self.maximum_tasks:
                    _fail("process-tree affinity task count exceeds the bound")
                try:
                    observed = tuple(sorted(os.sched_getaffinity(tid)))
                except ProcessLookupError:
                    continue
                if observed != self.expected_cpus:
                    _fail("process-tree thread escaped the frozen CPU affinity")
                self._records.append((timestamp, tid, observed))
                pending.extend(self._children(tid))
        return root_present

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                present = self._scan_once()
                if self._root_observed and not present:
                    return
                self._stop.wait(self.poll_interval_ns / 1_000_000_000)
        except BaseException as exc:
            self._error = exc
            self._stop.set()

    def start(self) -> None:
        if self._thread is not None:
            _fail("affinity monitor already started")
        # Synchronous first scan closes the launch-to-thread race before the
        # caller resumes ordinary work.
        if not self._scan_once():
            _fail("affinity monitor root process was never observable")
        self._thread = threading.Thread(target=self._run, name="cp2e-affinity-audit", daemon=True)
        self._thread.start()

    def stop_and_validate(self) -> Mapping[str, Any]:
        if self._thread is None:
            _fail("affinity monitor was never started")
        self._stop.set()
        self._thread.join(timeout=5.0)
        if self._thread.is_alive():
            _fail("affinity monitor did not terminate")
        if self._error is not None:
            raise TimingControlError("affinity monitor failed") from self._error
        if not self._root_observed or not self._records:
            _fail("affinity monitor retained no process-tree observation")
        canonical = _canonical_json({
            "schema_version": 1,
            "record_type": "cp2_timing_affinity_audit",
            "root_pid": self.root_pid,
            "expected_cpus": list(self.expected_cpus),
            "poll_interval_ns": self.poll_interval_ns,
            "observation_count": len(self._records),
            "observations": [
                {"monotonic_ns": instant, "tid": tid, "cpu_ids": list(cpus)}
                for instant, tid, cpus in self._records
            ],
            "passed": True,
        })
        return {
            "canonical_bytes": canonical,
            "sha256": hashlib.sha256(canonical).hexdigest(),
            "observation_count": len(self._records),
        }


__all__ = [
    "ControlSnapshot", "HostBackend", "HostControlTransaction", "MSR_APERF",
    "MSR_MPERF", "ProcessTreeAffinityMonitor", "RawTelemetryReceipt",
    "RawTelemetrySnapshot", "RawTelemetrySpec", "TimingControlError",
    "TimingControlIndeterminate", "capture_raw_telemetry",
    "compare_raw_telemetry", "parse_control_value", "recover_control_journal",
    "require_complete_control_coverage", "snapshot_digest",
    "validate_control_coverage",
]
