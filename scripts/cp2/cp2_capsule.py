#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Closed, data-free CP2-D capsule transport and Linux private stager.

This module defines a *proposed, non-authorizing* evaluator/direct-math
capsule boundary.  It performs no package discovery, subprocess execution,
repository access, readiness action, or recorded-input access.

``.cp2cap`` is an uncompressed regular-file-only stream::

    b"SchurVIO-CP2-capsule-v1\\0"
    member_count:u64be
    repeated member_count times:
        path_length:u64be, path:portable-ASCII
        role_length:u64be, role:ASCII
        mode:u64be                 # exactly 0444, or 0555 for entry binaries
        payload_length:u64be
        payload_sha256:32 raw bytes
        payload

It cannot encode links, devices, directories, owners, timestamps, sparse
files, compression, or extension records.  Parent directories are derived and
staged as 0700.  The inventory encoding is the same metadata without payloads
under a distinct domain tag, so relocation changes no inventory byte.

Actual staging is deliberately Linux-specific.  It pins directories with
file descriptors, uses ``*at`` operations with ``O_NOFOLLOW|O_EXCL``, streams
the already hash-bound archive, validates the complete private tree through
descriptors, and publishes a hidden partial through ``renameat2(NOREPLACE)``.
If those capabilities are unavailable it fails closed.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import cp2_schema as schema


CAPSULE_MAGIC = b"SchurVIO-CP2-capsule-v1\0"
INVENTORY_MAGIC = b"SchurVIO-CP2-capsule-inventory-v1\0"
# The command record and capsule profile bind the same logical environment.
# One domain prevents a profile/record pair from carrying identical variables
# under two digests that can never compare equal.
ENVIRONMENT_MAGIC = schema.COMMAND_ENVIRONMENT_DOMAIN
NATIVE_EDGE_MAGIC = b"SchurVIO-CP2-native-closure-edges-v1\0"
NATIVE_CONSUMER_MAGIC = b"SchurVIO-CP2-native-consumers-v1\0"

MAX_MEMBER_COUNT = 16_384
MAX_PATH_BYTES = 1_024
MAX_PATH_COMPONENT_BYTES = 128
MAX_ROLE_BYTES = 64
MAX_MEMBER_BYTES = 512 << 20
MAX_ARCHIVE_BYTES = 1 << 30
MAX_IN_MEMORY_ARCHIVE_BYTES = 64 << 20
IO_CHUNK_BYTES = 1 << 20
MAX_COMMAND_OUTPUT_BYTES = 16 << 20
MAX_PREFLIGHT_OUTPUT_BYTES = 64 << 20
MAX_SOURCE_LOCK_BYTES = 16 << 20

EXPECTED_CAPSULE_UNIT_PATHS = {
    "direct_math": (
        "capsules/direct_math.cp2cap",
        "capsules/direct_math.profile.json",
    ),
    "evaluator": (
        "capsules/evaluator.cp2cap",
        "capsules/evaluator.profile.json",
    ),
}
EXPECTED_CAPSULE_IDENTITY_RECORD_TYPE = "cp2_d_expected_capsule_identity"
EXPECTED_CAPSULE_SOURCE_LOCK_PATH = "project/cp2_capsule_source_lock.json"
CONTRACT_BINDING_PATHS = (
    "docs/cp2_d_alignment_uniqueness_clarification_proposed.md",
    "docs/cp2_d_evaluator_precision_clarification_proposed.md",
    "project/cp2_completion_authorization_binding.json",
    "project/cp2_d_completion_authorization_addendum.txt",
)
_SOURCE_LOCK_FORBIDDEN_OUTPUT_KEYS = frozenset(
    (
        "archive_sha256",
        "archive_size",
        "archive_size_bytes",
        "capsule_archive_identity",
        "capsule_output_identity",
        "capsule_profile_identity",
        "expected_capsule_identity",
        "expected_capsule_outputs",
        "profile_sha256",
        "profile_size",
        "profile_size_bytes",
    )
)

CAPSULE_KINDS = frozenset(("evaluator", "direct_math"))
FILE_MODES = frozenset((0o444, 0o555))
FILE_ROLES = frozenset(
    (
        "capsule_launcher",
        "capsule_sandbox",
        "python_interpreter",
        "python_stdlib",
        "python_extension",
        "numpy_extension",
        "numpy_module",
        "numpy_distribution_metadata",
        "evaluator_module",
        "python_dependency",
        "direct_math_module",
        "direct_math_known_answer",
        "native_loader",
        "native_library",
        "preflight_fixture",
        "preflight_known_answer",
        "license_notice",
        "source_lock",
    )
)
EXECUTABLE_ROLES = frozenset(
    ("capsule_launcher", "capsule_sandbox", "python_interpreter", "native_loader")
)
DISTRIBUTION_ROLES = frozenset(
    (
        "numpy_extension",
        "numpy_module",
        "numpy_distribution_metadata",
    )
)
NATIVE_CONSUMER_ROLES = frozenset(
    (
        "capsule_launcher",
        "capsule_sandbox",
        "python_interpreter",
        "python_extension",
        "numpy_extension",
        "native_loader",
        "native_library",
    )
)

PATH_COMPONENT = re.compile(r"^[A-Za-z0-9_+.-]{1,128}$")
PARTIAL_NAME = re.compile(r"^\.cp2-stage-[0-9a-f]{32}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
PYTHON_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
PYTHON_CALLABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
NEEDED_NAME = re.compile(r"^[A-Za-z0-9_+.-]{1,128}$")
GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

PRIVATE_PATH_ENVIRONMENT = {
    "HOME": "${PRIVATE_ROOT}/home",
    "MPLCONFIGDIR": "${PRIVATE_ROOT}/mpl",
    "TMPDIR": "${PRIVATE_ROOT}/tmp",
    "XDG_CACHE_HOME": "${PRIVATE_ROOT}/xdg-cache",
    "XDG_CONFIG_HOME": "${PRIVATE_ROOT}/xdg-config",
}
FIXED_ENVIRONMENT = {
    "BLIS_NUM_THREADS": "1",
    "LANG": "C",
    "LC_ALL": "C",
    "MKL_DYNAMIC": "FALSE",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "NPY_DISABLE_CPU_FEATURES": "X86_V3,X86_V4,AVX512_ICL,AVX512_SPR",
    "OMP_DYNAMIC": "FALSE",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_CORETYPE": "SkylakeX",
    "OPENBLAS_NUM_THREADS": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "TZ": "UTC",
    "VECLIB_MAXIMUM_THREADS": "1",
}
EXACT_ENVIRONMENT = dict(FIXED_ENVIRONMENT, **PRIVATE_PATH_ENVIRONMENT)
INJECTION_DENYLIST = (
    "BASH_ENV",
    "CDPATH",
    "CONDA_PREFIX",
    "ENV",
    "GCONV_PATH",
    "GLIBC_TUNABLES",
    "LD_AUDIT",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PATH",
    "PYTHONBREAKPOINT",
    "PYTHONHASHSEED",
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONWARNINGS",
    "VIRTUAL_ENV",
)


class CapsuleError(ValueError):
    """Raised when capsule bytes, provenance, or staging fail closed."""


class CapsulePublicationIncident(CapsuleError):
    """A failed publication whose exact capsule inode may need caller cleanup.

    ``authoritative_state`` is one of ``partial``, ``destination``, or
    ``indeterminate``.  The exception deliberately retains both names and the
    expected inode identity so a caller can invoke
    :func:`reconcile_capsule_publication` without guessing which path is safe
    to remove.  An indeterminate state is diagnostic only and never grants
    deletion authority.
    """

    def __init__(
        self,
        message: str,
        destination: Path,
        partial_name: str,
        expected_identity: Optional[Tuple[int, int]],
        authoritative_state: str,
    ) -> None:
        super().__init__(message)
        self.destination = destination
        self.partial_name = partial_name
        self.expected_identity = expected_identity
        self.authoritative_state = authoritative_state


def _fail(message: str) -> None:
    raise CapsuleError(message)


def _exact_keys(value: Any, keys: Sequence[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict:
        _fail(label + " must be a plain JSON object")
    try:
        return schema.exact_object_keys(value, keys, label)
    except schema.SchemaError as exc:
        raise CapsuleError(str(exc)) from exc


def _u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > (1 << 64) - 1:
        _fail(label + " must be an unsigned 64-bit integer")
    return value


def _positive_u64(value: Any, label: str, maximum: int) -> int:
    result = _u64(value, label)
    if result == 0 or result > maximum:
        _fail(label + " is outside its frozen positive bound")
    return result


def _sha(value: Any, label: str) -> str:
    try:
        return schema.validate_sha256(value, label)
    except schema.SchemaError as exc:
        raise CapsuleError(str(exc)) from exc


def _safe_id(value: Any, label: str) -> str:
    if type(value) is not str or SAFE_ID.fullmatch(value) is None:
        _fail(label + " is not a safe identifier")
    return value


def _distribution_name(value: Any) -> str:
    name = _safe_id(value, "distribution name")
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if name != normalized:
        _fail("distribution name must use its lowercase normalized form")
    return name


def _safe_text(value: Any, label: str, maximum: int = 512) -> str:
    if type(value) is not str or not value or "\0" in value:
        _fail(label + " must be nonempty NUL-free text")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise CapsuleError(label + " is not strict UTF-8") from exc
    if len(encoded) > maximum or any(byte < 0x20 or byte == 0x7F for byte in encoded):
        _fail(label + " contains control bytes or exceeds its byte bound")
    return value


def _relpath(value: Any, label: str) -> str:
    if type(value) is not str or not value or "\\" in value or "\0" in value:
        _fail(label + " must be a portable relative path")
    try:
        encoded = value.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise CapsuleError(label + " must contain portable ASCII only") from exc
    if len(encoded) > MAX_PATH_BYTES or value.startswith("/") or value.endswith("/"):
        _fail(label + " is outside the portable path bounds")
    parts = value.split("/")
    if any(
        part in ("", ".", "..")
        or len(part.encode("ascii")) > MAX_PATH_COMPONENT_BYTES
        or PATH_COMPONENT.fullmatch(part) is None
        for part in parts
    ):
        _fail(label + " has an unsafe path component")
    return value


def _template_path(value: Any, label: str, roots: Sequence[str]) -> str:
    text = _safe_text(value, label, 2_048)
    for root in roots:
        prefix = "${" + root + "}/"
        if text.startswith(prefix):
            _relpath(text[len(prefix) :], label + " relative suffix")
            return text
    _fail(label + " does not use an approved root token")


def _lp(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _bounded_tuple(values: Iterable[Any], maximum: int, label: str) -> tuple:
    if isinstance(values, (str, bytes, bytearray)):
        _fail(label + " must be an iterable of records")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise CapsuleError(label + " must be iterable") from exc
    result = []
    for value in iterator:
        if len(result) >= maximum:
            _fail(label + " exceeds its count bound")
        result.append(value)
    return tuple(result)


def _checked_add(current: int, increment: int, maximum: int, label: str) -> int:
    if increment < 0 or current > maximum - increment:
        _fail(label + " exceeds its frozen bound")
    return current + increment


@dataclass(frozen=True, order=True)
class CapsuleEntry:
    path: str
    role: str
    mode: int
    size: int
    sha256: str

    def __post_init__(self) -> None:
        path = _relpath(self.path, "capsule member path")
        if type(self.role) is not str or self.role not in FILE_ROLES:
            _fail("capsule member role is not allowlisted")
        mode = _u64(self.mode, "capsule member mode")
        if mode not in FILE_MODES:
            _fail("capsule member mode must be 0444 or 0555")
        if (self.role in EXECUTABLE_ROLES) != (mode == 0o555):
            _fail("only sandbox/launcher/interpreter/native-loader roles may and must be executable")
        size = _u64(self.size, "capsule member size")
        if size > MAX_MEMBER_BYTES:
            _fail("capsule member exceeds its size bound")
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "size", size)
        object.__setattr__(self, "sha256", _sha(self.sha256, "capsule member SHA-256"))


@dataclass(frozen=True)
class CapsuleMember:
    entry: CapsuleEntry
    payload: bytes

    def __post_init__(self) -> None:
        if type(self.entry) is not CapsuleEntry or type(self.payload) is not bytes:
            _fail("capsule member must pair an entry with immutable bytes")
        if len(self.payload) != self.entry.size:
            _fail("capsule member payload size differs from inventory")
        if hashlib.sha256(self.payload).hexdigest() != self.entry.sha256:
            _fail("capsule member payload digest differs from inventory")


@dataclass(frozen=True)
class CapsuleFileSource:
    """One already inventoried regular file for streaming capsule encoding."""

    entry: CapsuleEntry
    source_path: Path

    def __post_init__(self) -> None:
        if type(self.entry) is not CapsuleEntry:
            _fail("capsule file source lacks one exact inventory entry")
        path = Path(self.source_path)
        if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
            _fail("capsule file source path is not normalized absolute")
        object.__setattr__(self, "source_path", path)


@dataclass(frozen=True)
class ParsedCapsule:
    entries: Tuple[CapsuleEntry, ...]
    payloads: Tuple[bytes, ...]
    inventory_bytes: bytes
    inventory_sha256: str


@dataclass(frozen=True)
class TargetIdentity:
    os_name: str
    machine: str
    elf_class: int
    endianness: str
    libc_abi: str
    loader_abi: str
    elf_interpreter: str
    cpu_dispatch_policy: str


@dataclass(frozen=True)
class DistributionIdentity:
    name: str
    version: str
    paths: Tuple[str, ...]
    inventory_sha256: str


@dataclass(frozen=True, order=True)
class NativeEdge:
    consumer: str
    needed: str
    provider: str


@dataclass(frozen=True, order=True)
class NativeConsumer:
    path: str
    elf_type: str
    linkage: str
    interpreter: str
    rpath: Tuple[str, ...]
    runpath: Tuple[str, ...]
    soname: str
    needed: Tuple[str, ...]


@dataclass(frozen=True)
class NativeClosure:
    loader_path: str
    mapped_paths: Tuple[str, ...]
    consumers: Tuple[NativeConsumer, ...]
    needed_edges: Tuple[NativeEdge, ...]
    inventory_sha256: str
    consumers_sha256: str
    edges_sha256: str
    loader_argv_prefix: Tuple[str, ...]
    effective_library_directories: Tuple[str, ...]
    cache_policy: str
    default_search_policy: str
    pathname_policy: str


@dataclass(frozen=True)
class CommandExpectation:
    argv: Tuple[str, ...]
    cwd: str
    timeout_seconds: int
    stdin_policy: str
    exit_code: int
    stdout_size: int
    stdout_sha256: str
    stderr_size: int
    stderr_sha256: str


@dataclass(frozen=True)
class ExecutionIdentity:
    argv_template: Tuple[str, ...]
    cwd: str
    timeout_seconds: int
    stdin_policy: str
    environment_sha256: str
    injection_denylist: Tuple[str, ...]
    thread_policy: str
    hash_seed_policy: str


@dataclass(frozen=True)
class PreflightExpectation:
    command: CommandExpectation
    input_paths: Tuple[str, ...]
    output_codec: str
    output_size: int
    output_sha256: str
    known_answer_bits_sha256: str


@dataclass(frozen=True)
class FloatingPointIdentity:
    format_name: str
    rounding_mode: str
    subnormal_policy: str
    control_register: str
    control_value_hex: str
    secondary_control_register: str
    secondary_control_value_hex: str
    numpy_error_policy: str


@dataclass(frozen=True)
class NumericalRuntimeIdentity:
    runtime_kind: str
    canonical_bytes: bytes
    sha256: str


@dataclass(frozen=True)
class ContractBinding:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CapsuleProfile:
    contract_bindings: Tuple[ContractBinding, ...]
    profile_id: str
    capsule_kind: str
    target: TargetIdentity
    source_lock_path: str
    source_lock_size: int
    source_lock_sha256: str
    archive_size: int
    archive_sha256: str
    entries: Tuple[CapsuleEntry, ...]
    inventory_sha256: str
    launcher_path: str
    interpreter_path: str
    entry_module_path: str
    entry_point_module: str
    entry_point_callable: str
    distributions: Tuple[DistributionIdentity, ...]
    native_closure: NativeClosure
    environment: Tuple[Tuple[str, str], ...]
    environment_sha256: str
    execution: ExecutionIdentity
    floating_point: FloatingPointIdentity
    numerical_runtime: NumericalRuntimeIdentity
    version_probe: CommandExpectation
    synthetic_preflight: PreflightExpectation
    canonical_bytes: bytes
    profile_sha256: str


@dataclass(frozen=True)
class StagedCapsule:
    root: Path
    profile_id: str
    profile_sha256: str
    capsule_kind: str
    target_machine: str
    archive_sha256: str
    inventory_sha256: str
    environment_sha256: str
    entries: Tuple[CapsuleEntry, ...]


def _validated_entries(entries: Iterable[CapsuleEntry]) -> Tuple[CapsuleEntry, ...]:
    result = _bounded_tuple(entries, MAX_MEMBER_COUNT, "capsule inventory")
    if not result or any(type(entry) is not CapsuleEntry for entry in result):
        _fail("capsule inventory must contain at least one CapsuleEntry")
    paths = [entry.path.encode("ascii") for entry in result]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        _fail("capsule inventory paths must be unique and bytewise sorted")
    path_set = {entry.path for entry in result}
    total = 0
    for entry in result:
        total = _checked_add(total, entry.size, MAX_ARCHIVE_BYTES, "capsule payload total")
        for parent in PurePosixPath(entry.path).parents:
            if str(parent) != "." and str(parent) in path_set:
                _fail("capsule inventory has a file/descendant collision")
    return result


def canonical_inventory_bytes(entries: Iterable[CapsuleEntry]) -> bytes:
    validated = _validated_entries(entries)
    output = bytearray(INVENTORY_MAGIC)
    output.extend(len(validated).to_bytes(8, "big"))
    for entry in validated:
        output.extend(_lp(entry.path.encode("ascii")))
        output.extend(_lp(entry.role.encode("ascii")))
        output.extend(entry.mode.to_bytes(8, "big"))
        output.extend(entry.size.to_bytes(8, "big"))
        output.extend(bytes.fromhex(entry.sha256))
    return bytes(output)


def inventory_sha256(entries: Iterable[CapsuleEntry]) -> str:
    return hashlib.sha256(canonical_inventory_bytes(entries)).hexdigest()


def make_capsule_member(path: str, role: str, mode: int, payload: bytes) -> CapsuleMember:
    if type(payload) is not bytes:
        _fail("capsule payload must be immutable bytes")
    entry = CapsuleEntry(path, role, mode, len(payload), hashlib.sha256(payload).hexdigest())
    return CapsuleMember(entry, payload)


def encode_capsule(members: Iterable[CapsuleMember]) -> bytes:
    values = _bounded_tuple(members, MAX_MEMBER_COUNT, "capsule members")
    if not values or any(type(member) is not CapsuleMember for member in values):
        _fail("capsule members must be a nonempty CapsuleMember sequence")
    entries = _validated_entries(member.entry for member in values)
    output = bytearray(CAPSULE_MAGIC)
    output.extend(len(values).to_bytes(8, "big"))
    for member, entry in zip(values, entries):
        frame_size = (
            8 + len(entry.path) + 8 + len(entry.role) + 8 + 8 + 32 + entry.size
        )
        if len(output) > MAX_IN_MEMORY_ARCHIVE_BYTES - frame_size:
            _fail("in-memory capsule encoding exceeds its resource bound")
        output.extend(_lp(entry.path.encode("ascii")))
        output.extend(_lp(entry.role.encode("ascii")))
        output.extend(entry.mode.to_bytes(8, "big"))
        output.extend(entry.size.to_bytes(8, "big"))
        output.extend(bytes.fromhex(entry.sha256))
        output.extend(member.payload)
    return bytes(output)


def encode_capsule_file(sources: Iterable[CapsuleFileSource], destination: Any) -> Tuple[int, str]:
    """Stream a large capsule to one new immutable file without buffering it.

    Every source is hashed once before construction and again from its held
    descriptor while its payload is streamed.  A failure unlinks the incomplete
    destination and fsyncs the held parent directory; no partial capsule is an
    authoritative output.
    """

    values = _bounded_tuple(sources, MAX_MEMBER_COUNT, "capsule file sources")
    if not values or any(type(item) is not CapsuleFileSource for item in values):
        _fail("capsule file sources must be a nonempty exact sequence")
    entries = _validated_entries(item.entry for item in values)
    target = Path(destination)
    if not target.is_absolute() or os.path.normpath(str(target)) != str(target) or target.name in ("", ".", ".."):
        _fail("capsule output path is not normalized absolute")
    parent_fd = _open_absolute_directory(target.parent, "capsule output parent")
    descriptor = -1
    created = False

    def source_digest(item: CapsuleFileSource) -> None:
        source_fd = os.open(
            str(item.source_path),
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            before = os.fstat(source_fd)
            by_path = os.stat(str(item.source_path), follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.geteuid()
                or stat.S_IMODE(before.st_mode) != item.entry.mode
                or before.st_size != item.entry.size
                or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
            ):
                _fail("capsule source identity/mode/size differs: " + item.entry.path)
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = os.read(source_fd, min(IO_CHUNK_BYTES, remaining))
                if not block:
                    _fail("capsule source became short: " + item.entry.path)
                digest.update(block)
                remaining -= len(block)
            if os.read(source_fd, 1):
                _fail("capsule source grew during hashing: " + item.entry.path)
            after = os.fstat(source_fd)
            if _identity(before) != _identity(after) or digest.hexdigest() != item.entry.sha256:
                _fail("capsule source changed or has the wrong digest: " + item.entry.path)
        finally:
            os.close(source_fd)

    try:
        _require_private_directory_fd(parent_fd, "capsule output parent")
        for item in values:
            source_digest(item)
        descriptor = os.open(
            target.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        archive_digest = hashlib.sha256()
        archive_size = 0

        def write(payload: bytes) -> None:
            nonlocal archive_size
            if archive_size > MAX_ARCHIVE_BYTES - len(payload):
                _fail("streamed capsule exceeds its archive-size bound")
            offset = 0
            while offset < len(payload):
                count = os.write(descriptor, payload[offset:])
                if count <= 0:
                    _fail("streamed capsule write made no progress")
                offset += count
            archive_digest.update(payload)
            archive_size += len(payload)

        write(CAPSULE_MAGIC)
        write(len(values).to_bytes(8, "big"))
        for item, entry in zip(values, entries):
            write(_lp(entry.path.encode("ascii")))
            write(_lp(entry.role.encode("ascii")))
            write(entry.mode.to_bytes(8, "big"))
            write(entry.size.to_bytes(8, "big"))
            write(bytes.fromhex(entry.sha256))
            source_fd = os.open(
                str(item.source_path),
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                before = os.fstat(source_fd)
                digest = hashlib.sha256()
                remaining = entry.size
                while remaining:
                    block = os.read(source_fd, min(IO_CHUNK_BYTES, remaining))
                    if not block:
                        _fail("capsule source became short during streaming: " + entry.path)
                    write(block)
                    digest.update(block)
                    remaining -= len(block)
                if os.read(source_fd, 1):
                    _fail("capsule source grew during streaming: " + entry.path)
                after = os.fstat(source_fd)
                if (
                    _identity(before) != _identity(after)
                    or before.st_nlink != 1
                    or before.st_uid != os.geteuid()
                    or stat.S_IMODE(before.st_mode) != entry.mode
                    or digest.hexdigest() != entry.sha256
                ):
                    _fail("capsule source changed during streaming: " + entry.path)
            finally:
                os.close(source_fd)
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        output_status = os.fstat(descriptor)
        by_name = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(output_status.st_mode)
            or output_status.st_nlink != 1
            or stat.S_IMODE(output_status.st_mode) != 0o444
            or output_status.st_size != archive_size
            or _inode_identity(output_status) != _inode_identity(by_name)
        ):
            _fail("streamed capsule output identity differs")
        os.fsync(parent_fd)
        return archive_size, archive_digest.hexdigest()
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if created:
            try:
                os.unlink(target.name, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


class _BytesReader:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.offset = 0

    def read(self, count: int, label: str) -> bytes:
        if count < 0 or count > len(self.payload) - self.offset:
            _fail("capsule is truncated while reading " + label)
        result = self.payload[self.offset : self.offset + count]
        self.offset += count
        return result

    def u64(self, label: str) -> int:
        return int.from_bytes(self.read(8, label), "big")

    def lp(self, label: str, maximum: int) -> bytes:
        count = self.u64(label + " length")
        if count == 0 or count > maximum:
            _fail(label + " length is outside its frozen bound")
        return self.read(count, label)


def parse_capsule_bytes(
    document: Any, *, expected_inventory_sha256: Optional[str] = None
) -> ParsedCapsule:
    if type(document) is not bytes:
        _fail("capsule document must be immutable bytes")
    if len(document) > MAX_IN_MEMORY_ARCHIVE_BYTES:
        _fail("in-memory capsule document exceeds its resource bound")
    reader = _BytesReader(document)
    if reader.read(len(CAPSULE_MAGIC), "magic") != CAPSULE_MAGIC:
        _fail("capsule magic differs")
    count = reader.u64("member count")
    if count == 0 or count > MAX_MEMBER_COUNT:
        _fail("capsule member count is outside its frozen bound")
    entries = []
    payloads = []
    total = 0
    for index in range(count):
        path_bytes = reader.lp("member {} path".format(index), MAX_PATH_BYTES)
        role_bytes = reader.lp("member {} role".format(index), MAX_ROLE_BYTES)
        try:
            path = path_bytes.decode("ascii", "strict")
            role = role_bytes.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise CapsuleError("capsule path/role is not ASCII") from exc
        mode = reader.u64("member {} mode".format(index))
        size = reader.u64("member {} size".format(index))
        digest = reader.read(32, "member {} SHA-256".format(index)).hex()
        if size > MAX_MEMBER_BYTES:
            _fail("capsule member exceeds its size bound")
        total = _checked_add(total, size, MAX_ARCHIVE_BYTES, "capsule payload total")
        payload = reader.read(size, "member {} payload".format(index))
        entry = CapsuleEntry(path, role, mode, size, digest)
        if hashlib.sha256(payload).hexdigest() != digest:
            _fail("capsule member payload SHA-256 differs")
        entries.append(entry)
        payloads.append(payload)
    if reader.offset != len(document):
        _fail("capsule has trailing bytes")
    validated = _validated_entries(entries)
    canonical = canonical_inventory_bytes(validated)
    digest = hashlib.sha256(canonical).hexdigest()
    if expected_inventory_sha256 is not None and digest != _sha(
        expected_inventory_sha256, "expected inventory SHA-256"
    ):
        _fail("capsule inventory SHA-256 differs from the expected value")
    return ParsedCapsule(validated, tuple(payloads), canonical, digest)


def _environment_bytes(environment: Sequence[Tuple[str, str]]) -> bytes:
    if any(type(item) is not tuple or len(item) != 2 for item in environment):
        _fail("capsule environment rows must be name/value pairs")
    names = [item[0] for item in environment]
    if names != sorted(names) or len(names) != len(set(names)):
        _fail("capsule environment names must be unique and ASCII-sorted")
    output = bytearray(ENVIRONMENT_MAGIC)
    output.extend(len(environment).to_bytes(8, "big"))
    for name, value in environment:
        if type(name) is not str or ENVIRONMENT_NAME.fullmatch(name) is None:
            _fail("capsule environment name is invalid")
        text = _safe_text(value, "capsule environment value", 2_048)
        output.extend(_lp(name.encode("ascii")))
        output.extend(_lp(text.encode("ascii")))
    return bytes(output)


def environment_sha256(environment: Iterable[Tuple[str, str]]) -> str:
    values = _bounded_tuple(environment, 128, "capsule environment")
    return hashlib.sha256(_environment_bytes(values)).hexdigest()


def _entry_from_record(value: Any) -> CapsuleEntry:
    record = _exact_keys(value, ("path", "role", "mode", "size", "sha256"), "inventory row")
    return CapsuleEntry(record["path"], record["role"], record["mode"], record["size"], record["sha256"])


def _sorted_paths(value: Any, label: str) -> Tuple[str, ...]:
    if type(value) is not list:
        _fail(label + " must be an array")
    paths = tuple(_relpath(item, label + " path") for item in value)
    if list(paths) != sorted(paths) or len(paths) != len(set(paths)):
        _fail(label + " paths must be unique and sorted")
    return paths


def _ordered_ascii_texts(value: Any, label: str, maximum_count: int = 256) -> Tuple[str, ...]:
    if type(value) is not list or len(value) > maximum_count:
        _fail(label + " must be a bounded array")
    texts = []
    for item in value:
        text = _safe_text(item, label + " value", 1_024)
        try:
            text.encode("ascii", "strict")
        except UnicodeEncodeError as exc:
            raise CapsuleError(label + " values must be ASCII") from exc
        texts.append(text)
    result = tuple(texts)
    if len(result) != len(set(result)):
        _fail(label + " values must be unique")
    return result


def _loader_search_paths(
    value: Any,
    label: str,
    consumer_path: str,
    allowed_directories: set,
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Validate ordered `$ORIGIN` entries that resolve inside capsule native dirs."""

    if type(value) is not list or len(value) > 64:
        _fail(label + " must be a bounded ordered array")
    result = []
    resolved_result = []
    origin = list(PurePosixPath(consumer_path).parent.parts)
    if origin == ["."]:
        origin = []
    for item in value:
        text = _safe_text(item, label + " value", 1_024)
        try:
            text.encode("ascii", "strict")
        except UnicodeEncodeError as exc:
            raise CapsuleError(label + " values must be ASCII") from exc
        if text == "$ORIGIN":
            suffix = []
        elif text.startswith("$ORIGIN/"):
            suffix = text[len("$ORIGIN/") :].split("/")
        else:
            _fail(label + " entries must be `$ORIGIN`-relative")
        resolved = list(origin)
        for component in suffix:
            if component in ("", "."):
                continue
            if component == "..":
                if not resolved:
                    _fail(label + " escapes the capsule root")
                resolved.pop()
            elif PATH_COMPONENT.fullmatch(component) is not None:
                resolved.append(component)
            else:
                _fail(label + " contains an unsafe component")
        resolved_path = "/".join(resolved) if resolved else "."
        if resolved_path not in allowed_directories:
            _fail(label + " does not resolve to an inventoried native directory")
        if text in result:
            _fail(label + " contains a duplicate search entry")
        result.append(text)
        resolved_result.append(resolved_path)
    return tuple(result), tuple(resolved_result)


def _command(value: Any, label: str, launcher_token: str) -> CommandExpectation:
    record = _exact_keys(
        value,
        (
            "argv", "cwd", "timeout_seconds", "stdin_policy", "exit_code",
            "stdout_size", "stdout_sha256", "stderr_size", "stderr_sha256",
        ),
        label,
    )
    if type(record["argv"]) is not list or not record["argv"]:
        _fail(label + " argv must be a nonempty array")
    argv = tuple(_safe_text(item, label + " argv token", 2_048) for item in record["argv"])
    if argv[0] != launcher_token:
        _fail(label + " does not invoke the bound launcher token")
    cwd = _template_path(record["cwd"], label + " cwd", ("PRIVATE_ROOT",))
    timeout = _positive_u64(record["timeout_seconds"], label + " timeout", 600)
    if record["stdin_policy"] != "devnull":
        _fail(label + " stdin policy must be devnull")
    if type(record["exit_code"]) is not int or record["exit_code"] != 0:
        _fail(label + " exit code must be integer zero")
    stdout_size = _u64(record["stdout_size"], label + " stdout size")
    stderr_size = _u64(record["stderr_size"], label + " stderr size")
    if stdout_size > MAX_COMMAND_OUTPUT_BYTES or stderr_size > MAX_COMMAND_OUTPUT_BYTES:
        _fail(label + " output size exceeds the command resource bound")
    return CommandExpectation(
        argv, cwd, timeout, "devnull", 0,
        stdout_size,
        _sha(record["stdout_sha256"], label + " stdout SHA-256"),
        stderr_size,
        _sha(record["stderr_sha256"], label + " stderr SHA-256"),
    )


def _native_edges_bytes(edges: Sequence[NativeEdge]) -> bytes:
    output = bytearray(NATIVE_EDGE_MAGIC)
    output.extend(len(edges).to_bytes(8, "big"))
    for edge in edges:
        for value in (edge.consumer, edge.needed, edge.provider):
            output.extend(_lp(value.encode("ascii")))
    return bytes(output)


def _native_consumers_bytes(consumers: Sequence[NativeConsumer]) -> bytes:
    output = bytearray(NATIVE_CONSUMER_MAGIC)
    output.extend(len(consumers).to_bytes(8, "big"))
    for consumer in consumers:
        for value in (
            consumer.path,
            consumer.elf_type,
            consumer.linkage,
            consumer.interpreter,
            consumer.soname,
        ):
            output.extend(_lp(value.encode("ascii")))
        for values in (consumer.rpath, consumer.runpath, consumer.needed):
            output.extend(len(values).to_bytes(8, "big"))
            for value in values:
                output.extend(_lp(value.encode("ascii")))
    return bytes(output)


def _canonical_profile_document(record: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(record, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("ascii", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleError("capsule profile is not canonical JSON data") from exc


def validate_embedded_source_lock(payload: Any) -> Mapping[str, Any]:
    """Reject output identities that would make an embedded lock circular."""

    if type(payload) is not bytes or not payload or len(payload) > MAX_SOURCE_LOCK_BYTES:
        _fail("embedded capsule source lock is not bounded nonempty bytes")
    try:
        record = schema.strict_json_loads(payload)
    except schema.SchemaError as exc:
        raise CapsuleError("embedded capsule source lock is not strict JSON") from exc
    if not isinstance(record, Mapping):
        _fail("embedded capsule source lock is not an object")
    if (
        record.get("record_type") != "cp2_d_minimal_two_capsule_source_lock"
        or record.get("checkpoint") != "CP2-D"
        or type(record.get("schema_version")) is not int
        or record.get("schema_version") != 2
        or record.get("formal_execution_permitted_by_this_record") is not False
    ):
        _fail("embedded capsule source-lock identity differs")

    def visit(value: Any, path: Tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if type(key) is not str:
                    _fail("embedded capsule source lock has a nontext key")
                if key in _SOURCE_LOCK_FORBIDDEN_OUTPUT_KEYS:
                    _fail(
                        "embedded capsule source lock contains a self-referential "
                        "archive/profile identity field: " + ".".join(path + (key,))
                    )
                if key in ("archive", "profile") and isinstance(child, Mapping):
                    if set(child).intersection(
                        ("sha256", "size", "size_bytes", "profile_sha256")
                    ):
                        _fail(
                            "embedded capsule source lock contains a self-referential "
                            "archive/profile identity object: "
                            + ".".join(path + (key,))
                        )
                visit(child, path + (key,))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, path + (str(index),))

    visit(record, ())
    return record


def validate_expected_capsule_identity(value: Any) -> Mapping[str, Any]:
    """Validate the acyclic, capsule-external archive/profile identity record."""

    record = _exact_keys(
        value,
        (
            "schema_version",
            "record_type",
            "checkpoint",
            "formal_execution_permitted_by_this_record",
            "embedded_source_lock",
            "unit_artifact_members",
        ),
        "expected capsule identity",
    )
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or record["record_type"] != EXPECTED_CAPSULE_IDENTITY_RECORD_TYPE
        or record["checkpoint"] != "CP2-D"
        or record["formal_execution_permitted_by_this_record"] is not False
    ):
        _fail("expected capsule identity header differs")
    lock = _exact_keys(
        record["embedded_source_lock"],
        ("path", "size_bytes", "sha256"),
        "expected capsule source lock",
    )
    if lock["path"] != EXPECTED_CAPSULE_SOURCE_LOCK_PATH:
        _fail("expected capsule source-lock path differs")
    _u64(lock["size_bytes"], "expected capsule source-lock size")
    _sha(lock["sha256"], "expected capsule source-lock SHA-256")
    members = _exact_keys(
        record["unit_artifact_members"],
        tuple(sorted(EXPECTED_CAPSULE_UNIT_PATHS)),
        "expected capsule member kinds",
    )
    for kind in sorted(EXPECTED_CAPSULE_UNIT_PATHS):
        pair = _exact_keys(
            members[kind], ("archive", "profile"), kind + " expected identity"
        )
        for index, label in enumerate(("archive", "profile")):
            item = _exact_keys(
                pair[label],
                ("path", "size_bytes", "sha256"),
                kind + " expected " + label,
            )
            if item["path"] != EXPECTED_CAPSULE_UNIT_PATHS[kind][index]:
                _fail(kind + " expected " + label + " path differs")
            _u64(item["size_bytes"], kind + " expected " + label + " size")
            _sha(item["sha256"], kind + " expected " + label + " SHA-256")
    return record


def make_expected_capsule_identity(
    source_lock_payload: bytes,
    artifacts: Mapping[str, Tuple[bytes, bytes]],
) -> Mapping[str, Any]:
    """Construct an external identity from exact archive/profile byte pairs."""

    validate_embedded_source_lock(source_lock_payload)
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(
        EXPECTED_CAPSULE_UNIT_PATHS
    ):
        _fail("expected capsule artifact population differs")
    output: Dict[str, Any] = {}
    source_lock_sha256 = hashlib.sha256(source_lock_payload).hexdigest()
    for kind in sorted(EXPECTED_CAPSULE_UNIT_PATHS):
        pair = artifacts[kind]
        if (
            type(pair) is not tuple
            or len(pair) != 2
            or any(type(item) is not bytes for item in pair)
        ):
            _fail(kind + " expected capsule inputs are not exact byte pairs")
        archive_payload, profile_payload = pair
        try:
            profile_record = schema.strict_json_loads(profile_payload)
        except schema.SchemaError as exc:
            raise CapsuleError(kind + " expected profile is not strict JSON") from exc
        profile = validate_capsule_profile(profile_record)
        if (
            profile.capsule_kind != kind
            or profile.canonical_bytes != profile_payload
            or profile.archive_size != len(archive_payload)
            or profile.archive_sha256 != hashlib.sha256(archive_payload).hexdigest()
            or profile.source_lock_size != len(source_lock_payload)
            or profile.source_lock_sha256 != source_lock_sha256
        ):
            _fail(kind + " archive/profile/source-lock identity differs")
        output[kind] = {
            "archive": {
                "path": EXPECTED_CAPSULE_UNIT_PATHS[kind][0],
                "size_bytes": len(archive_payload),
                "sha256": profile.archive_sha256,
            },
            "profile": {
                "path": EXPECTED_CAPSULE_UNIT_PATHS[kind][1],
                "size_bytes": len(profile_payload),
                "sha256": profile.profile_sha256,
            },
        }
    record = {
        "schema_version": 1,
        "record_type": EXPECTED_CAPSULE_IDENTITY_RECORD_TYPE,
        "checkpoint": "CP2-D",
        "formal_execution_permitted_by_this_record": False,
        "embedded_source_lock": {
            "path": EXPECTED_CAPSULE_SOURCE_LOCK_PATH,
            "size_bytes": len(source_lock_payload),
            "sha256": source_lock_sha256,
        },
        "unit_artifact_members": output,
    }
    return validate_expected_capsule_identity(record)


def expected_capsule_identity_bytes(record: Any) -> bytes:
    return _canonical_profile_document(validate_expected_capsule_identity(record))


def verify_expected_capsule_identity(
    value: Any,
    source_lock_payload: bytes,
    artifacts: Mapping[str, Tuple[bytes, bytes]],
) -> None:
    retained = validate_expected_capsule_identity(value)
    reconstructed = make_expected_capsule_identity(source_lock_payload, artifacts)
    if retained != reconstructed:
        _fail("external expected capsule identity differs from exact artifacts")


def validate_capsule_profile(value: Any) -> CapsuleProfile:
    """Validate one exact proposed profile without inspecting its capsule path."""

    record = _exact_keys(
        value,
        (
            "schema_version", "record_type", "checkpoint", "contract_bindings",
            "profile_id", "capsule_kind", "target", "source_lock", "archive", "inventory",
            "inventory_sha256", "entry_point", "distributions", "native_closure",
            "environment", "execution", "floating_point", "numerical_runtime", "version_probe",
            "synthetic_preflight",
        ),
        "capsule profile",
    )
    if type(record["schema_version"]) is not int or record["schema_version"] != 2:
        _fail("capsule profile schema_version must be integer 2")
    if record["record_type"] != "cp2_d_capsule_profile" or record["checkpoint"] != "CP2-D":
        _fail("capsule profile record type/checkpoint differs")
    raw_contract_bindings = record["contract_bindings"]
    if (
        type(raw_contract_bindings) is not list
        or len(raw_contract_bindings) != len(CONTRACT_BINDING_PATHS)
    ):
        _fail("capsule contract-binding population differs")
    contract_bindings = []
    for index, expected_path in enumerate(CONTRACT_BINDING_PATHS):
        binding = _exact_keys(
            raw_contract_bindings[index],
            ("path", "size", "sha256"),
            "capsule contract binding",
        )
        if binding["path"] != expected_path:
            _fail("capsule contract-binding path/order differs")
        size = _u64(binding["size"], "capsule contract-binding size")
        if size == 0 or size > MAX_SOURCE_LOCK_BYTES:
            _fail("capsule contract-binding size is outside its bound")
        contract_bindings.append(
            ContractBinding(
                expected_path,
                size,
                _sha(binding["sha256"], "capsule contract-binding SHA-256"),
            )
        )
    profile_id = _safe_id(record["profile_id"], "capsule profile id")
    kind = record["capsule_kind"]
    if type(kind) is not str or kind not in CAPSULE_KINDS:
        _fail("capsule kind must be evaluator or direct_math")

    target_record = _exact_keys(
        record["target"],
        (
            "os", "machine", "elf_class", "endianness", "libc_abi",
            "loader_abi", "elf_interpreter", "cpu_dispatch_policy",
        ),
        "capsule target",
    )
    if target_record["os"] != "linux" or target_record["machine"] not in ("x86_64", "aarch64"):
        _fail("capsule target must select Linux x86_64 or aarch64")
    if type(target_record["elf_class"]) is not int or target_record["elf_class"] != 64:
        _fail("capsule target ELF class must be integer 64")
    if target_record["endianness"] != "little":
        _fail("capsule target endianness must be little")
    elf_interpreter = _safe_text(
        target_record["elf_interpreter"], "target ELF interpreter", 256
    )
    if target_record["machine"] == "x86_64" and elf_interpreter != "/lib64/ld-linux-x86-64.so.2":
        _fail("x86_64 target ELF interpreter differs")
    if target_record["machine"] == "aarch64" and elf_interpreter != "/lib/ld-linux-aarch64.so.1":
        _fail("aarch64 target ELF interpreter differs")
    target = TargetIdentity(
        "linux", target_record["machine"], 64, "little",
        _safe_text(target_record["libc_abi"], "target libc ABI"),
        _safe_text(target_record["loader_abi"], "target loader ABI"),
        elf_interpreter,
        _safe_id(target_record["cpu_dispatch_policy"], "target CPU dispatch policy"),
    )

    archive_record = _exact_keys(record["archive"], ("size", "sha256"), "capsule archive")
    archive_size = _positive_u64(archive_record["size"], "capsule archive size", MAX_ARCHIVE_BYTES)
    archive_digest = _sha(archive_record["sha256"], "capsule archive SHA-256")
    if type(record["inventory"]) is not list:
        _fail("capsule inventory must be an array")
    entries = _validated_entries(_entry_from_record(item) for item in record["inventory"])
    inv_digest = _sha(record["inventory_sha256"], "capsule inventory SHA-256")
    if inventory_sha256(entries) != inv_digest:
        _fail("capsule inventory digest differs from its exact rows")
    by_path = {entry.path: entry for entry in entries}
    role_paths = {
        role: tuple(entry.path for entry in entries if entry.role == role) for role in FILE_ROLES
    }
    if (
        len(role_paths["capsule_sandbox"]) != 1
        or role_paths["capsule_sandbox"][0] != "bin/sandbox"
        or len(role_paths["capsule_launcher"]) != 1
        or len(role_paths["python_interpreter"]) != 1
    ):
        _fail("capsule must have exactly one sandbox, launcher, and interpreter")
    if len(role_paths["native_loader"]) != 1:
        _fail("capsule must have exactly one executable native loader")
    if not role_paths["license_notice"] or not role_paths["preflight_fixture"]:
        _fail("capsule must retain license/notice and preflight-fixture members")
    if len(role_paths["source_lock"]) != 1:
        _fail("capsule must retain exactly one authoritative source lock")
    source_lock_record = _exact_keys(
        record["source_lock"], ("path", "size", "sha256"), "capsule source lock"
    )
    source_lock_path = _relpath(source_lock_record["path"], "source lock path")
    source_lock_size = _positive_u64(
        source_lock_record["size"], "source lock size", MAX_MEMBER_BYTES
    )
    source_lock_sha256 = _sha(
        source_lock_record["sha256"], "source lock SHA-256"
    )
    source_lock_entry = by_path.get(source_lock_path)
    if (
        source_lock_entry is None
        or source_lock_entry.role != "source_lock"
        or source_lock_entry.size != source_lock_size
        or source_lock_entry.sha256 != source_lock_sha256
    ):
        _fail("capsule source lock does not bind its unique inventory member")
    if kind == "evaluator":
        if len(role_paths["evaluator_module"]) != 1:
            _fail("evaluator capsule lacks its unique equivalent evaluator module")
        if role_paths["direct_math_module"] or role_paths["direct_math_known_answer"]:
            _fail("evaluator capsule contains direct-math-only roles")
        if len(role_paths["preflight_known_answer"]) != 1:
            _fail("evaluator capsule must retain exactly one preflight known-answer payload")
    else:
        if (
            not role_paths["direct_math_module"]
            or not role_paths["direct_math_known_answer"]
            or not role_paths["numpy_extension"]
            or not role_paths["numpy_module"]
            or not role_paths["numpy_distribution_metadata"]
        ):
            _fail("direct-math capsule lacks module, known answers, or complete NumPy roles")
        if role_paths["evaluator_module"]:
            _fail("direct-math capsule contains evaluator-only roles")
        if role_paths["preflight_known_answer"]:
            _fail("direct-math capsule must use its direct-math known-answer member")

    entry_record = _exact_keys(
        record["entry_point"],
        ("launcher_path", "interpreter_path", "module_path", "module", "callable"),
        "capsule entry point",
    )
    launcher_path = _relpath(entry_record["launcher_path"], "launcher path")
    interpreter_path = _relpath(entry_record["interpreter_path"], "interpreter path")
    module_path = _relpath(entry_record["module_path"], "entry module path")
    if launcher_path != role_paths["capsule_launcher"][0] or interpreter_path != role_paths["python_interpreter"][0]:
        _fail("entry point does not select the unique launcher/interpreter")
    expected_module_role = "evaluator_module" if kind == "evaluator" else "direct_math_module"
    if module_path not in by_path or by_path[module_path].role != expected_module_role:
        _fail("entry module path does not select the kind-specific module row")
    module = entry_record["module"]
    callable_name = entry_record["callable"]
    if type(module) is not str or PYTHON_MODULE.fullmatch(module) is None:
        _fail("entry-point module name is invalid")
    if type(callable_name) is not str or PYTHON_CALLABLE.fullmatch(callable_name) is None:
        _fail("entry-point callable name is invalid")

    if type(record["distributions"]) is not list:
        _fail("capsule distributions must be an array")
    distributions = []
    claimed_distribution_paths = set()
    for item in record["distributions"]:
        distribution = _exact_keys(
            item, ("name", "version", "paths", "inventory_sha256"), "capsule distribution"
        )
        name = _distribution_name(distribution["name"])
        version = _safe_text(distribution["version"], "distribution version", 128)
        paths = _sorted_paths(distribution["paths"], "distribution")
        if not paths or any(path not in by_path or by_path[path].role not in DISTRIBUTION_ROLES for path in paths):
            _fail("distribution path is absent or has a non-distribution role")
        if claimed_distribution_paths.intersection(paths):
            _fail("distribution inventories overlap")
        claimed_distribution_paths.update(paths)
        digest = _sha(distribution["inventory_sha256"], "distribution inventory SHA-256")
        if inventory_sha256(tuple(by_path[path] for path in paths)) != digest:
            _fail("distribution digest differs from its member subset")
        distributions.append(DistributionIdentity(name, version, paths, digest))
    if distributions != sorted(distributions, key=lambda item: item.name) or len(
        {item.name for item in distributions}
    ) != len(distributions):
        _fail("distribution names must be unique and sorted")
    required_distribution_paths = {
        entry.path for entry in entries if entry.role in DISTRIBUTION_ROLES
    }
    if claimed_distribution_paths != required_distribution_paths:
        _fail("distribution subsets do not exactly cover distribution-role files")
    if kind == "evaluator" and distributions:
        _fail("stdlib-only equivalent evaluator must not claim a third-party distribution")
    if kind == "direct_math" and "numpy" not in {item.name.lower() for item in distributions}:
        _fail("direct-math capsule must bind NumPy")
    distribution_by_name = {item.name: item for item in distributions}
    if "numpy" in distribution_by_name:
        expected_numpy_paths = {
            entry.path
            for entry in entries
            if entry.role
            in ("numpy_module", "numpy_distribution_metadata", "numpy_extension")
        }
        if set(distribution_by_name["numpy"].paths) != expected_numpy_paths:
            _fail("NumPy distribution does not exactly own every NumPy-role member")
    if kind == "direct_math" and set(distribution_by_name) != {"numpy"}:
        _fail("direct-math capsule distribution inventory must be exactly NumPy")

    closure_record = _exact_keys(
        record["native_closure"],
        (
            "loader_path", "loader_argv_prefix", "effective_library_directories",
            "cache_policy", "default_search_policy", "pathname_policy",
            "mapped_paths",
            "consumers",
            "needed_edges",
            "inventory_sha256",
            "consumers_sha256",
            "edges_sha256",
        ),
        "native closure",
    )
    loader_path = _relpath(closure_record["loader_path"], "native loader path")
    mapped_paths = _sorted_paths(closure_record["mapped_paths"], "native mapped")
    native_paths = tuple(
        sorted(role_paths["native_loader"] + role_paths["native_library"])
    )
    if (
        mapped_paths != native_paths
        or loader_path != role_paths["native_loader"][0]
    ):
        _fail("native closure must exactly cover all native-library rows and its loader")
    loader_argv_prefix = _ordered_ascii_texts(
        closure_record["loader_argv_prefix"], "native loader argv prefix", 16
    )
    effective_library_directories = _sorted_paths(
        closure_record["effective_library_directories"],
        "effective native library directory",
    )
    expected_library_directories = (
        ("native", "python/lib", "python/lib/python3.11/numpy.libs")
        if kind == "direct_math"
        else ("native", "python/lib")
    )
    if effective_library_directories != expected_library_directories:
        _fail("effective native library directory order differs from the launcher")
    library_path_token = ":".join(
        {
            "native": "${HELD_NATIVE_DIR}",
            "python/lib": "${HELD_PYTHON_LIB_DIR}",
            "python/lib/python3.11/numpy.libs": "${HELD_NUMPY_LIBS_DIR}",
        }[path]
        for path in effective_library_directories
    )
    expected_loader_prefix = (
        "${HELD_LOADER_FD}",
        "--inhibit-cache",
        "--library-path",
        library_path_token,
        "${HELD_PYTHON_FD}",
        "-I",
        "-S",
        "-B",
        "-m",
        module,
    )
    if loader_argv_prefix != expected_loader_prefix:
        _fail("native loader argv prefix differs from the held-descriptor launcher")
    if closure_record["cache_policy"] != "inhibit-cache":
        _fail("native loader cache policy differs")
    if closure_record["default_search_policy"] != "runtime-maps-reject-outside-capsule":
        _fail("native loader fallback policy differs")
    if (
        closure_record["pathname_policy"]
        != "descriptor-held-loader-python-and-library-directories"
    ):
        _fail("native loader pathname policy differs")
    if type(closure_record["consumers"]) is not list:
        _fail("native consumers must be an array")
    consumers = []
    search_directories_by_consumer = {}
    for item in closure_record["consumers"]:
        consumer_record = _exact_keys(
            item,
            (
                "path", "elf_type", "linkage", "interpreter", "rpath", "runpath",
                "soname", "needed",
            ),
            "native consumer",
        )
        consumer_path = _relpath(consumer_record["path"], "native consumer path")
        if consumer_path not in by_path or by_path[consumer_path].role not in NATIVE_CONSUMER_ROLES:
            _fail("native consumer does not select an audited native-code role")
        elf_type = consumer_record["elf_type"]
        if elf_type not in ("ET_EXEC", "ET_DYN"):
            _fail("native consumer ELF type is invalid")
        linkage = consumer_record["linkage"]
        if linkage not in (
            "dynamic-executable", "static-executable", "shared-object", "dynamic-loader"
        ):
            _fail("native consumer linkage kind is invalid")
        interpreter = consumer_record["interpreter"]
        if interpreter != "none":
            interpreter = _safe_text(
                interpreter, "native consumer ELF PT_INTERP", 256
            )
        if linkage == "dynamic-executable" and interpreter != target.elf_interpreter:
            _fail("dynamic executable lacks its exact on-disk ELF PT_INTERP")
        if linkage == "static-executable" and (
            elf_type != "ET_EXEC" or interpreter != "none"
        ):
            _fail("static executable linkage/interpreter is inconsistent")
        if linkage == "shared-object" and (
            elf_type != "ET_DYN"
            or interpreter not in ("none", target.elf_interpreter)
        ):
            _fail("shared-object linkage/interpreter identity is inconsistent")
        if linkage == "dynamic-loader" and (
            elf_type != "ET_DYN" or interpreter != "none"
        ):
            _fail("dynamic-loader linkage/interpreter identity is inconsistent")
        if by_path[consumer_path].role == "native_loader" and linkage != "dynamic-loader":
            _fail("native-loader role lacks dynamic-loader linkage")
        if by_path[consumer_path].role != "native_loader" and linkage == "dynamic-loader":
            _fail("dynamic-loader linkage is assigned to the wrong role")
        consumer_role = by_path[consumer_path].role
        if consumer_role in (
            "capsule_sandbox", "capsule_launcher", "python_interpreter"
        ) and linkage not in (
            "dynamic-executable", "static-executable"
        ):
            _fail("executable role has non-executable linkage")
        if consumer_role in (
            "python_extension", "numpy_extension", "native_library"
        ) and linkage != "shared-object":
            _fail("extension/library role lacks shared-object linkage")
        native_directories = {
            str(PurePosixPath(path).parent) for path in mapped_paths
        }
        rpath, rpath_directories = _loader_search_paths(
            consumer_record["rpath"], "native consumer RPATH", consumer_path,
            native_directories,
        )
        runpath, runpath_directories = _loader_search_paths(
            consumer_record["runpath"], "native consumer RUNPATH", consumer_path,
            native_directories,
        )
        if rpath and runpath:
            _fail("native consumer cannot retain both RPATH and RUNPATH")
        soname = consumer_record["soname"]
        if soname != "none" and (type(soname) is not str or NEEDED_NAME.fullmatch(soname) is None):
            _fail("native consumer SONAME is invalid")
        needed = _ordered_ascii_texts(consumer_record["needed"], "native consumer DT_NEEDED")
        if any(NEEDED_NAME.fullmatch(name) is None for name in needed):
            _fail("native consumer DT_NEEDED name is invalid")
        if linkage == "static-executable" and (needed or rpath or runpath):
            _fail("static executable must not retain dynamic search/dependency entries")
        search_directories_by_consumer[consumer_path] = set(
            runpath_directories if runpath else rpath_directories
        )
        consumers.append(
            NativeConsumer(
                consumer_path, elf_type, linkage, interpreter, rpath, runpath, soname, needed
            )
        )
    if consumers != sorted(consumers) or len({consumer.path for consumer in consumers}) != len(consumers):
        _fail("native consumers must have unique sorted paths")
    expected_consumer_paths = {
        entry.path for entry in entries if entry.role in NATIVE_CONSUMER_ROLES
    }
    if {consumer.path for consumer in consumers} != expected_consumer_paths:
        _fail("native consumer records do not exactly cover native-code members")
    if type(closure_record["needed_edges"]) is not list:
        _fail("native needed edges must be an array")
    edges = []
    for item in closure_record["needed_edges"]:
        edge_record = _exact_keys(item, ("consumer", "needed", "provider"), "native edge")
        consumer = _relpath(edge_record["consumer"], "native edge consumer")
        provider = _relpath(edge_record["provider"], "native edge provider")
        needed = edge_record["needed"]
        if consumer not in expected_consumer_paths or provider not in mapped_paths:
            _fail("native edge references an absent consumer/provider")
        if type(needed) is not str or NEEDED_NAME.fullmatch(needed) is None:
            _fail("native needed name is invalid")
        edges.append(NativeEdge(consumer, needed, provider))
    if edges != sorted(edges) or len(set(edges)) != len(edges):
        _fail("native edges must be unique and sorted")
    edge_keys = [(edge.consumer, edge.needed) for edge in edges]
    if len(edge_keys) != len(set(edge_keys)):
        _fail("native consumer/needed mapping is ambiguous")
    expected_edge_keys = {
        (consumer.path, needed) for consumer in consumers for needed in consumer.needed
    }
    if set(edge_keys) != expected_edge_keys:
        _fail("native edges do not exactly realize every audited DT_NEEDED entry")
    consumer_by_path = {consumer.path: consumer for consumer in consumers}
    for edge in edges:
        provider_identity = consumer_by_path.get(edge.provider)
        if provider_identity is None or provider_identity.soname != edge.needed:
            _fail("native edge provider SONAME does not match DT_NEEDED")
        provider_directory = str(PurePosixPath(edge.provider).parent)
        if provider_directory not in effective_library_directories:
            _fail("native edge provider is outside the explicit launcher library path")
    referenced_native = {loader_path}.union(edge.provider for edge in edges)
    if referenced_native != set(mapped_paths):
        _fail("native edge graph leaves an unreferenced mapped library")
    closure_inventory_digest = _sha(
        closure_record["inventory_sha256"], "native closure inventory SHA-256"
    )
    if inventory_sha256(tuple(by_path[path] for path in mapped_paths)) != closure_inventory_digest:
        _fail("native closure inventory digest differs")
    consumer_digest = _sha(
        closure_record["consumers_sha256"], "native consumers SHA-256"
    )
    if hashlib.sha256(_native_consumers_bytes(consumers)).hexdigest() != consumer_digest:
        _fail("native consumer digest differs")
    edge_digest = _sha(closure_record["edges_sha256"], "native edge SHA-256")
    if hashlib.sha256(_native_edges_bytes(edges)).hexdigest() != edge_digest:
        _fail("native edge digest differs")
    native_closure = NativeClosure(
        loader_path,
        mapped_paths,
        tuple(consumers),
        tuple(edges),
        closure_inventory_digest,
        consumer_digest,
        edge_digest,
        loader_argv_prefix,
        effective_library_directories,
        "inhibit-cache",
        "runtime-maps-reject-outside-capsule",
        "descriptor-held-loader-python-and-library-directories",
    )

    environment_record = _exact_keys(record["environment"], ("variables", "sha256"), "environment")
    if type(environment_record["variables"]) is not dict:
        _fail("environment variables must be a plain object")
    variables = environment_record["variables"]
    if variables != EXACT_ENVIRONMENT or any(type(key) is not str or type(val) is not str for key, val in variables.items()):
        _fail("environment must equal the exact private single-thread allowlist")
    environment = tuple(sorted(variables.items()))
    env_digest = _sha(environment_record["sha256"], "environment SHA-256")
    if environment_sha256(environment) != env_digest:
        _fail("environment digest differs from its exact variables")

    launcher_token = "${CAPSULE_ROOT}/" + launcher_path
    execution_record = _exact_keys(
        record["execution"],
        (
            "argv_template", "cwd", "timeout_seconds", "stdin_policy", "environment_sha256",
            "injection_denylist", "thread_policy", "hash_seed_policy",
        ),
        "capsule execution",
    )
    if type(execution_record["argv_template"]) is not list:
        _fail("execution argv template must be an array")
    argv_template = tuple(
        _safe_text(item, "execution argv token", 2_048) for item in execution_record["argv_template"]
    )
    evaluator_template = (
        launcher_token, "tum", "{GT_SHARED}", "{MODE_SHARED_ALIGNED}", "-r", "trans_part",
        "--t_max_diff", "0.01", "--save_results", "{ABS_RESULT_ZIP}", "--no_warnings",
    )
    direct_template = (
        launcher_token, "--input", "{ABS_REQUEST}", "--output", "{ABS_RESPONSE}",
    )
    if argv_template != (evaluator_template if kind == "evaluator" else direct_template):
        _fail("execution argv template differs from the kind-specific exact command")
    execution_cwd = _template_path(execution_record["cwd"], "execution cwd", ("PRIVATE_ROOT",))
    execution_timeout = _positive_u64(execution_record["timeout_seconds"], "execution timeout", 600)
    if execution_record["stdin_policy"] != "devnull" or execution_record["environment_sha256"] != env_digest:
        _fail("execution stdin/environment binding differs")
    if execution_record["injection_denylist"] != list(INJECTION_DENYLIST):
        _fail("execution injection denylist differs")
    if execution_record["thread_policy"] != "verified-single-thread":
        _fail("execution thread policy differs")
    if execution_record["hash_seed_policy"] not in ("pyconfig-fixed-zero", "hash-order-independent"):
        _fail("execution hash-seed policy is unresolved")
    execution = ExecutionIdentity(
        argv_template, execution_cwd, execution_timeout, "devnull", env_digest,
        INJECTION_DENYLIST, "verified-single-thread", execution_record["hash_seed_policy"],
    )

    floating_record = _exact_keys(
        record["floating_point"],
        (
            "format",
            "rounding_mode",
            "subnormal_policy",
            "control_register",
            "control_value_hex",
            "secondary_control_register",
            "secondary_control_value_hex",
            "numpy_error_policy",
        ),
        "floating-point profile",
    )
    if target.machine == "x86_64":
        expected_register = "MXCSR"
        expected_control_value = "0000000000001f80"
        expected_secondary_register = "X87_CW"
        # x87 PC=10 selects a 53-bit significand; PC=11 (0x037f) would retain
        # extended precision and permit double-rounding despite the binary64
        # contract.
        expected_secondary_value = "000000000000027f"
    else:
        expected_register = "FPCR"
        expected_control_value = "0000000000000000"
        expected_secondary_register = "FPSR"
        expected_secondary_value = "0000000000000000"
    if (
        floating_record["format"] != "IEEE-754-binary64"
        or floating_record["rounding_mode"] != "FE_TONEAREST"
        or floating_record["subnormal_policy"] != "preserve"
        or floating_record["control_register"] != expected_register
        or floating_record["control_value_hex"] != expected_control_value
        or floating_record["secondary_control_register"] != expected_secondary_register
        or floating_record["secondary_control_value_hex"] != expected_secondary_value
        or floating_record["numpy_error_policy"]
        != (
            "invalid-divide-over-raise_under-ignore"
            if kind == "direct_math"
            else "not-applicable"
        )
    ):
        _fail("floating-point identity is incomplete or target-inconsistent")
    floating_point = FloatingPointIdentity(
        "IEEE-754-binary64", "FE_TONEAREST", "preserve", expected_register,
        expected_control_value, expected_secondary_register, expected_secondary_value,
        (
            "invalid-divide-over-raise_under-ignore"
            if kind == "direct_math"
            else "not-applicable"
        ),
    )

    numerical_record = _exact_keys(
        record["numerical_runtime"], ("identity", "sha256"), "numerical runtime"
    )
    identity = numerical_record["identity"]
    if kind == "evaluator":
        evaluator_identity = _exact_keys(
            identity,
            ("runtime_kind", "implementation"),
            "equivalent evaluator numerical runtime",
        )
        if (
            evaluator_identity["runtime_kind"]
            != "stdlib-binary64-independent-v1"
            or evaluator_identity["implementation"]
            != "CPython-math-no-NumPy-no-SciPy-no-evo"
            or target.cpu_dispatch_policy != "stdlib-binary64-fixed-x86-64"
        ):
            _fail("equivalent evaluator numerical runtime identity differs")
        runtime_kind = evaluator_identity["runtime_kind"]
    else:
        direct_identity = _exact_keys(
            identity,
            (
                "runtime_kind", "numpy_version", "numpy_cpu_baseline",
                "numpy_cpu_dispatch_targets", "numpy_disabled_targets",
                "numpy_effective_target_states", "openblas_library_path",
                "openblas_library_sha256", "openblas_version", "openblas_config",
                "openblas_corename", "openblas_threads", "openblas_parallel", "cpuid",
            ),
            "direct numerical runtime",
        )
        baseline = _ordered_ascii_texts(
            direct_identity["numpy_cpu_baseline"], "NumPy CPU baseline", 16
        )
        dispatch = _ordered_ascii_texts(
            direct_identity["numpy_cpu_dispatch_targets"],
            "NumPy CPU dispatch targets",
            16,
        )
        disabled = _ordered_ascii_texts(
            direct_identity["numpy_disabled_targets"],
            "NumPy disabled targets",
            16,
        )
        expected_dispatch = ("X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR")
        if (
            direct_identity["runtime_kind"] != "numpy-openblas-fixed-dispatch-v1"
            or direct_identity["numpy_version"] != "2.4.6"
            or baseline != ("X86_V2",)
            or dispatch != expected_dispatch
            or disabled != expected_dispatch
            or target.cpu_dispatch_policy != "numpy-x86-v2-openblas-skylakex"
        ):
            _fail("NumPy dispatch identity differs")
        effective = _exact_keys(
            direct_identity["numpy_effective_target_states"],
            ("X86_V2", "X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"),
            "NumPy effective target states",
        )
        if effective != {
            "X86_V2": True,
            "X86_V3": False,
            "X86_V4": False,
            "AVX512_ICL": False,
            "AVX512_SPR": False,
        } or any(type(value) is not bool for value in effective.values()):
            _fail("NumPy effective target state differs")
        openblas_path = _relpath(
            direct_identity["openblas_library_path"], "OpenBLAS library path"
        )
        openblas_entry = by_path.get(openblas_path)
        if (
            openblas_entry is None
            or openblas_entry.role != "native_library"
            or direct_identity["openblas_library_sha256"] != openblas_entry.sha256
            or direct_identity["openblas_version"] != "0.3.31.188.0"
            or direct_identity["openblas_config"]
            != (
                "OpenBLAS 0.3.31.188.0  USE64BITINT DYNAMIC_ARCH "
                "NO_AFFINITY SkylakeX MAX_THREADS=64"
            )
            or direct_identity["openblas_corename"] != "SkylakeX"
            or type(direct_identity["openblas_threads"]) is not int
            or direct_identity["openblas_threads"] != 1
            or type(direct_identity["openblas_parallel"]) is not int
            or direct_identity["openblas_parallel"] != 1
        ):
            _fail("OpenBLAS runtime identity differs")
        cpuid = _exact_keys(
            direct_identity["cpuid"],
            (
                "vendor", "family", "model", "stepping", "leaf1_eax",
                "leaf1_ecx", "leaf1_edx", "leaf7_ebx", "leaf7_ecx",
                "leaf7_edx", "extended_leaf1_ecx", "extended_leaf1_edx", "xcr0",
            ),
            "x86 CPUID identity",
        )
        expected_cpuid = {
            "vendor": "AuthenticAMD",
            "family": 26,
            "model": 68,
            "stepping": 0,
            "leaf1_eax": 11800384,
            "leaf1_ecx": 2128097803,
            "leaf1_edx": 395049983,
            "leaf7_ebx": 4055865259,
            "leaf7_ecx": 423649246,
            "leaf7_edx": 268435728,
            "extended_leaf1_ecx": 1975662591,
            "extended_leaf1_edx": 802421759,
            "xcr0": 743,
        }
        if cpuid != expected_cpuid or any(
            type(value) is not (str if key == "vendor" else int)
            for key, value in cpuid.items()
        ):
            _fail("x86 CPUID identity differs")
        runtime_kind = direct_identity["runtime_kind"]
    numerical_bytes = _canonical_profile_document(identity)
    numerical_sha256 = _sha(
        numerical_record["sha256"], "numerical runtime SHA-256"
    )
    if hashlib.sha256(numerical_bytes).hexdigest() != numerical_sha256:
        _fail("numerical runtime digest differs from its exact identity")
    numerical_runtime = NumericalRuntimeIdentity(
        runtime_kind, numerical_bytes, numerical_sha256
    )

    version_probe = _command(record["version_probe"], "version probe", launcher_token)
    if version_probe.argv != (launcher_token, "--version"):
        _fail("version probe argv differs from the exact command")
    preflight_record = _exact_keys(
        record["synthetic_preflight"],
        ("command", "input_paths", "output_codec", "output_size", "output_sha256", "known_answer_bits_sha256"),
        "synthetic preflight",
    )
    preflight_command = _command(preflight_record["command"], "synthetic preflight command", launcher_token)
    preflight_paths = _sorted_paths(preflight_record["input_paths"], "synthetic preflight input")
    if not preflight_paths or any(
        path not in by_path
        or by_path[path].role
        not in ("preflight_fixture", "preflight_known_answer", "direct_math_known_answer")
        for path in preflight_paths
    ):
        _fail("synthetic preflight inputs do not select fixture/KAT members")
    expected_codec = (
        "cp2_translation_rmse_result_zip_v1"
        if kind == "evaluator"
        else "cp2_f64_known_answer_bundle_v1"
    )
    if preflight_record["output_codec"] != expected_codec:
        _fail("synthetic preflight output codec differs")
    if kind == "evaluator":
        expected_preflight_argv = (
            launcher_token,
            "tum",
            "${CAPSULE_ROOT}/fixtures/ground-truth.tum",
            "${CAPSULE_ROOT}/fixtures/estimate.tum",
            "-r",
            "trans_part",
            "--t_max_diff",
            "0.01",
            "--save_results",
            "${PRIVATE_ROOT}/preflight/results.zip",
            "--no_warnings",
        )
        fixture_tokens = {
            "${CAPSULE_ROOT}/" + path
            for path in preflight_paths
            if by_path[path].role == "preflight_fixture"
        }
        known_answer_paths = tuple(
            path for path in preflight_paths if by_path[path].role == "preflight_known_answer"
        )
        if fixture_tokens != {expected_preflight_argv[2], expected_preflight_argv[3]}:
            _fail("evaluator preflight fixture paths differ from its argv")
        if len(known_answer_paths) != 1:
            _fail("evaluator preflight must bind exactly one known-answer payload")
        known_answer_path = known_answer_paths[0]
    else:
        fixture_tokens = tuple(
            "${CAPSULE_ROOT}/" + path
            for path in preflight_paths
            if by_path[path].role == "preflight_fixture"
        )
        known_answer_tokens = tuple(
            "${CAPSULE_ROOT}/" + path
            for path in preflight_paths
            if by_path[path].role == "direct_math_known_answer"
        )
        if len(fixture_tokens) != 1 or len(known_answer_tokens) != 1:
            _fail("direct-math preflight must bind one fixture and one known-answer member")
        known_answer_path = next(
            path for path in preflight_paths if by_path[path].role == "direct_math_known_answer"
        )
        expected_preflight_argv = (
            launcher_token,
            "--input",
            fixture_tokens[0],
            "--known-answers",
            known_answer_tokens[0],
            "--output",
            "${PRIVATE_ROOT}/preflight/response.json",
        )
    if preflight_command.argv != expected_preflight_argv:
        _fail("synthetic preflight argv differs from the kind-specific exact command")
    known_answer_digest = _sha(
        preflight_record["known_answer_bits_sha256"], "preflight known-answer SHA-256"
    )
    if known_answer_digest != by_path[known_answer_path].sha256:
        _fail("preflight known-answer digest does not bind its retained member bytes")
    preflight_output_size = _u64(
        preflight_record["output_size"], "preflight output size"
    )
    if preflight_output_size > MAX_PREFLIGHT_OUTPUT_BYTES:
        _fail("preflight output exceeds its resource bound")
    preflight = PreflightExpectation(
        preflight_command, preflight_paths, expected_codec,
        preflight_output_size,
        _sha(preflight_record["output_sha256"], "preflight output SHA-256"),
        known_answer_digest,
    )

    canonical = _canonical_profile_document(record)
    profile_digest = hashlib.sha256(canonical).hexdigest()
    return CapsuleProfile(
        tuple(contract_bindings), profile_id, kind, target,
        source_lock_path, source_lock_size, source_lock_sha256,
        archive_size, archive_digest,
        entries, inv_digest, launcher_path, interpreter_path, module_path, module,
        callable_name, tuple(distributions), native_closure, environment, env_digest,
        execution, floating_point, numerical_runtime, version_probe, preflight,
        canonical, profile_digest,
    )


def _require_linux_descriptor_capabilities() -> None:
    required_constants = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_PATH")
    if os.name != "posix" or any(not hasattr(os, name) for name in required_constants):
        _fail("Linux descriptor safety capabilities are unavailable")
    required_dir_fd = (os.open, os.mkdir, os.rename, os.stat, os.unlink, os.rmdir)
    if any(function not in os.supports_dir_fd for function in required_dir_fd):
        _fail("required descriptor-relative filesystem operations are unavailable")
    if os.stat not in os.supports_follow_symlinks:
        _fail("descriptor-relative no-follow stat is unavailable")


def _absolute_normal_path(value: Any, label: str) -> Path:
    if not isinstance(value, (str, os.PathLike)):
        _fail(label + " must be a path")
    raw = os.fspath(value)
    if type(raw) is not str or not raw or "\0" in raw:
        _fail(label + " must be nonempty NUL-free text")
    path = Path(raw)
    if not path.is_absolute() or os.path.normpath(raw) != raw:
        _fail(label + " must be absolute and lexically normalized")
    return path


def _open_absolute_directory(path: Path, label: str) -> int:
    _require_linux_descriptor_capabilities()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        for component in path.parts[1:]:
            next_fd = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise CapsuleError(label + " cannot be opened through nonsymlink directories") from exc


def _identity(status: os.stat_result) -> Tuple[int, ...]:
    return (
        status.st_dev, status.st_ino, status.st_mode, status.st_nlink, status.st_uid,
        status.st_gid, status.st_size, status.st_mtime_ns, status.st_ctime_ns,
    )


def _inode_identity(status: os.stat_result) -> Tuple[int, int]:
    """Stable identity across directory content/ctime/size changes."""

    return status.st_dev, status.st_ino


def _require_private_directory_fd(descriptor: int, label: str) -> os.stat_result:
    status = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(status.st_mode)
        or status.st_uid != os.geteuid()
        or stat.S_IMODE(status.st_mode) != 0o700
    ):
        _fail(label + " must be caller-owned mode-0700 directory")
    return status


class _BoundArchive:
    def __init__(self, path: Any, profile: CapsuleProfile) -> None:
        self.path: Optional[Path] = None
        self.profile = profile
        self.parent_fd = -1
        self.fd = -1
        try:
            if type(path) is int:
                if path < 0:
                    _fail("held capsule archive descriptor is invalid")
                self.fd = os.dup(path)
            else:
                self.path = _absolute_normal_path(path, "capsule archive path")
                self.parent_fd = _open_absolute_directory(
                    self.path.parent, "capsule archive parent"
                )
                before_path = os.stat(
                    self.path.name,
                    dir_fd=self.parent_fd,
                    follow_symlinks=False,
                )
                self.fd = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=self.parent_fd,
                )
            before_fd = os.fstat(self.fd)
            self.identity = _identity(before_fd)
            if self.path is not None and self.identity != _identity(before_path):
                _fail("capsule archive path/descriptor identity differs")
            if (
                not stat.S_ISREG(before_fd.st_mode)
                or before_fd.st_nlink != 1
                or before_fd.st_uid != os.geteuid()
                or stat.S_IMODE(before_fd.st_mode) & 0o022
            ):
                _fail("capsule archive must be caller-owned, single-link, regular, and non-shared-writable")
            if before_fd.st_size != profile.archive_size:
                _fail("capsule archive size differs from profile")
            digest = hashlib.sha256()
            os.lseek(self.fd, 0, os.SEEK_SET)
            remaining = before_fd.st_size
            while remaining:
                chunk = os.read(self.fd, min(IO_CHUNK_BYTES, remaining))
                if not chunk:
                    _fail("capsule archive ended before retained size")
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(self.fd, 1):
                _fail("capsule archive grew during hashing")
            if digest.hexdigest() != profile.archive_sha256:
                _fail("capsule archive SHA-256 differs from profile")
            self.revalidate()
            os.lseek(self.fd, 0, os.SEEK_SET)
        except BaseException as exc:
            self.close()
            if isinstance(exc, CapsuleError):
                raise
            if isinstance(exc, OSError):
                raise CapsuleError("capsule archive cannot be bound safely") from exc
            raise

    def read_exact(self, count: int, label: str) -> bytes:
        result = bytearray()
        while len(result) < count:
            chunk = os.read(self.fd, min(IO_CHUNK_BYTES, count - len(result)))
            if not chunk:
                _fail("capsule archive is truncated while reading " + label)
            result.extend(chunk)
        return bytes(result)

    def u64(self, label: str) -> int:
        return int.from_bytes(self.read_exact(8, label), "big")

    def lp(self, label: str, maximum: int) -> bytes:
        count = self.u64(label + " length")
        if count == 0 or count > maximum:
            _fail(label + " length is outside its frozen bound")
        return self.read_exact(count, label)

    def revalidate(self) -> None:
        fd_status = os.fstat(self.fd)
        if _identity(fd_status) != self.identity:
            _fail("capsule archive identity changed")
        if self.path is not None:
            path_status = os.stat(
                self.path.name, dir_fd=self.parent_fd, follow_symlinks=False
            )
            if _identity(path_status) != self.identity:
                _fail("capsule archive path identity changed")

    def close(self) -> None:
        fd, parent_fd = self.fd, getattr(self, "parent_fd", -1)
        self.fd = -1
        self.parent_fd = -1
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _directory_fd(root_fd: int, components: Sequence[str]) -> int:
    descriptor = os.dup(root_fd)
    try:
        for component in components:
            try:
                os.mkdir(component, 0o700, dir_fd=descriptor)
                os.fsync(descriptor)
            except FileExistsError:
                pass
            next_fd = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_fd
            _require_private_directory_fd(descriptor, "capsule member parent")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stream_member(archive: _BoundArchive, root_fd: int, expected: CapsuleEntry, index: int) -> None:
    path_bytes = archive.lp("member {} path".format(index), MAX_PATH_BYTES)
    role_bytes = archive.lp("member {} role".format(index), MAX_ROLE_BYTES)
    try:
        path = path_bytes.decode("ascii", "strict")
        role = role_bytes.decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise CapsuleError("capsule path/role is not ASCII") from exc
    observed = CapsuleEntry(
        path, role, archive.u64("member mode"), archive.u64("member size"),
        archive.read_exact(32, "member digest").hex(),
    )
    if observed != expected:
        _fail("capsule stream metadata differs from profile inventory")
    parts = PurePosixPath(expected.path).parts
    parent_fd = _directory_fd(root_fd, parts[:-1])
    member_fd = -1
    try:
        member_fd = os.open(
            parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=parent_fd,
        )
        digest = hashlib.sha256()
        remaining = expected.size
        while remaining:
            chunk = archive.read_exact(min(IO_CHUNK_BYTES, remaining), "member payload")
            digest.update(chunk)
            view = memoryview(chunk)
            offset = 0
            while offset < len(view):
                written = os.write(member_fd, view[offset:])
                if written <= 0:
                    _fail("capsule member write made no progress")
                offset += written
            remaining -= len(chunk)
        if digest.hexdigest() != expected.sha256:
            _fail("capsule streamed member digest differs")
        os.fchmod(member_fd, expected.mode)
        os.fsync(member_fd)
        status = os.fstat(member_fd)
        path_status = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(status) != _identity(path_status)
            or not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != expected.mode
            or status.st_size != expected.size
        ):
            _fail("staged capsule member identity/mode/size differs")
        os.fsync(parent_fd)
    finally:
        if member_fd >= 0:
            os.close(member_fd)
        os.close(parent_fd)


def _expected_directories(entries: Sequence[CapsuleEntry]) -> set:
    result = set()
    for entry in entries:
        parts = PurePosixPath(entry.path).parts
        for index in range(1, len(parts)):
            result.add("/".join(parts[:index]))
    return result


def _rehash_regular_at(parent_fd: int, name: str, expected: CapsuleEntry) -> None:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(path_status)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != expected.mode
            or before.st_size != expected.size
        ):
            _fail("staged capsule member metadata differs on revalidation")
        digest = hashlib.sha256()
        remaining = expected.size
        while remaining:
            chunk = os.read(descriptor, min(IO_CHUNK_BYTES, remaining))
            if not chunk:
                _fail("staged capsule member is truncated")
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1) or digest.hexdigest() != expected.sha256:
            _fail("staged capsule member bytes differ")
        after = os.fstat(descriptor)
        final_path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(after)
            or _identity(before) != _identity(final_path_status)
        ):
            _fail("staged capsule member changed during revalidation")
    finally:
        os.close(descriptor)


def _open_child_directory(parent_fd: int, name: str) -> int:
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )


def _revalidate_tree_fd(root_fd: int, entries: Sequence[CapsuleEntry]) -> None:
    root_before = _require_private_directory_fd(root_fd, "staged capsule root")
    by_path = {entry.path: entry for entry in entries}
    expected_directories = _expected_directories(entries)
    observed_files = set()
    observed_directories = set()

    def visit(directory_fd: int, prefix: str) -> None:
        try:
            names = sorted(os.listdir(directory_fd))
        except OSError as exc:
            raise CapsuleError("staged capsule directory cannot be enumerated") from exc
        for name in names:
            if type(name) is not str or PATH_COMPONENT.fullmatch(name) is None:
                _fail("staged capsule contains a nonportable name")
            relative = name if not prefix else prefix + "/" + name
            status = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(status.st_mode):
                if (
                    relative not in expected_directories
                    or status.st_uid != os.geteuid()
                    or stat.S_IMODE(status.st_mode) != 0o700
                ):
                    _fail("staged capsule contains an unexpected/unsafe directory")
                child_fd = _open_child_directory(directory_fd, name)
                try:
                    child_before = os.fstat(child_fd)
                    if _identity(child_before) != _identity(status):
                        _fail("staged capsule directory path/descriptor identity differs")
                    observed_directories.add(relative)
                    visit(child_fd, relative)
                    os.fsync(child_fd)
                    child_after = os.fstat(child_fd)
                    child_path_after = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False
                    )
                    if (
                        _identity(child_before) != _identity(child_after)
                        or _identity(child_before) != _identity(child_path_after)
                    ):
                        _fail("staged capsule directory changed during revalidation")
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(status.st_mode):
                if relative not in by_path:
                    _fail("staged capsule contains an extra regular file")
                observed_files.add(relative)
                _rehash_regular_at(directory_fd, name, by_path[relative])
            else:
                _fail("staged capsule contains a link or non-regular object")

    visit(root_fd, "")
    if observed_files != set(by_path) or observed_directories != expected_directories:
        _fail("staged capsule tree inventory differs")
    os.fsync(root_fd)
    root_after = os.fstat(root_fd)
    if _identity(root_before) != _identity(root_after):
        _fail("staged capsule root changed during revalidation")


def _remove_tree_at(parent_fd: int, name: str, expected_identity: Tuple[int, int]) -> None:
    """Remove only the caller-owned, same-device tree pinned at ``name``.

    Cleanup is a security boundary too: a stale no-follow stat must never be
    treated as authority to delete a subsequently substituted descendant.
    Every child is therefore opened, matched to both the pre-open and current
    path identity, and checked again immediately before its name is removed.
    Any link, foreign device, ownership/mode anomaly, or identity drift leaves
    the remainder in place and fails closed.
    """

    root_fd = os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent_fd,
    )
    try:
        root_status = _require_private_directory_fd(root_fd, "capsule cleanup root")
        root_path_status = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _inode_identity(root_status) != expected_identity
            or _identity(root_status) != _identity(root_path_status)
        ):
            _fail("refusing to clean a replacement capsule tree")
        root_device = root_status.st_dev

        def require_directory(status: os.stat_result) -> None:
            if (
                not stat.S_ISDIR(status.st_mode)
                or status.st_dev != root_device
                or status.st_uid != os.geteuid()
                or stat.S_IMODE(status.st_mode) != 0o700
            ):
                _fail("refusing to clean an unsafe capsule directory")

        def require_regular(status: os.stat_result) -> None:
            if (
                not stat.S_ISREG(status.st_mode)
                or status.st_dev != root_device
                or status.st_uid != os.geteuid()
                or status.st_nlink != 1
                or stat.S_IMODE(status.st_mode) not in FILE_MODES | {0o600}
            ):
                _fail("refusing to clean an unsafe capsule member")

        def clear(directory_fd: int) -> None:
            directory_before = os.fstat(directory_fd)
            require_directory(directory_before)
            for child in sorted(os.listdir(directory_fd)):
                if type(child) is not str or PATH_COMPONENT.fullmatch(child) is None:
                    _fail("refusing to clean a nonportable capsule path")
                path_before = os.stat(
                    child, dir_fd=directory_fd, follow_symlinks=False
                )
                if path_before.st_dev != root_device:
                    _fail("refusing to cross a device during capsule cleanup")
                if stat.S_ISDIR(path_before.st_mode):
                    child_fd = _open_child_directory(directory_fd, child)
                    try:
                        child_before = os.fstat(child_fd)
                        child_path_before = os.stat(
                            child, dir_fd=directory_fd, follow_symlinks=False
                        )
                        require_directory(child_before)
                        if (
                            _identity(path_before) != _identity(child_before)
                            or _identity(path_before) != _identity(child_path_before)
                        ):
                            _fail("capsule cleanup directory identity changed before recursion")
                        clear(child_fd)
                        child_after = os.fstat(child_fd)
                        child_path_after = os.stat(
                            child, dir_fd=directory_fd, follow_symlinks=False
                        )
                        require_directory(child_after)
                        if (
                            _inode_identity(child_before) != _inode_identity(child_after)
                            or _inode_identity(child_before)
                            != _inode_identity(child_path_after)
                        ):
                            _fail("capsule cleanup directory identity changed after recursion")
                        os.rmdir(child, dir_fd=directory_fd)
                        os.fsync(directory_fd)
                        if _inode_identity(os.fstat(child_fd)) != _inode_identity(child_before):
                            _fail("capsule cleanup directory descriptor changed during removal")
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(path_before.st_mode):
                    child_fd = os.open(
                        child,
                        os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC,
                        dir_fd=directory_fd,
                    )
                    try:
                        child_before = os.fstat(child_fd)
                        child_path_before = os.stat(
                            child, dir_fd=directory_fd, follow_symlinks=False
                        )
                        require_regular(child_before)
                        if (
                            _identity(path_before) != _identity(child_before)
                            or _identity(path_before) != _identity(child_path_before)
                        ):
                            _fail("capsule cleanup member identity changed after open")
                        child_after = os.fstat(child_fd)
                        child_path_after = os.stat(
                            child, dir_fd=directory_fd, follow_symlinks=False
                        )
                        if (
                            _identity(child_before) != _identity(child_after)
                            or _identity(child_before) != _identity(child_path_after)
                        ):
                            _fail("capsule cleanup member identity changed before unlink")
                        os.unlink(child, dir_fd=directory_fd)
                        os.fsync(directory_fd)
                        if _inode_identity(os.fstat(child_fd)) != _inode_identity(
                            child_before
                        ):
                            _fail("capsule cleanup member descriptor changed during unlink")
                    finally:
                        os.close(child_fd)
                else:
                    _fail("refusing to clean a link or non-regular capsule object")

            directory_after = os.fstat(directory_fd)
            require_directory(directory_after)
            if _inode_identity(directory_before) != _inode_identity(directory_after):
                _fail("capsule cleanup directory descriptor identity changed")

        clear(root_fd)
        root_after = os.fstat(root_fd)
        root_path_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        require_directory(root_after)
        if (
            _inode_identity(root_status) != _inode_identity(root_after)
            or _inode_identity(root_status) != _inode_identity(root_path_after)
        ):
            _fail("capsule cleanup root identity changed before removal")
        os.rmdir(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if _inode_identity(os.fstat(root_fd)) != _inode_identity(root_status):
            _fail("capsule cleanup root descriptor changed during removal")
    finally:
        os.close(root_fd)


def _rename_noreplace(parent_fd: int, old_name: str, new_name: str) -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        operation = libc.renameat2
    except (OSError, AttributeError) as exc:
        raise CapsuleError("renameat2(NOREPLACE) is unavailable") from exc
    operation.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    operation.restype = ctypes.c_int
    result = operation(
        parent_fd, old_name.encode("ascii"), parent_fd, new_name.encode("ascii"), 1
    )
    if result != 0:
        error = ctypes.get_errno()
        raise CapsuleError("atomic capsule publication failed: " + os.strerror(error))


def _publication_parent_fsync(parent_fd: int) -> None:
    os.fsync(parent_fd)


def _publication_return_revalidate(
    root_fd: int, entries: Sequence[CapsuleEntry]
) -> None:
    """Last fail-closed content boundary before publication returns success."""

    _revalidate_tree_fd(root_fd, entries)


def _capsule_publication_location(
    parent_fd: int,
    partial_name: str,
    destination_name: str,
    expected_identity: Tuple[int, int],
) -> str:
    def observed(name: str) -> Optional[os.stat_result]:
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    partial = observed(partial_name)
    destination = observed(destination_name)
    partial_is_exact = partial is not None and _inode_identity(partial) == expected_identity
    destination_is_exact = (
        destination is not None and _inode_identity(destination) == expected_identity
    )
    if partial_is_exact and not destination_is_exact:
        return "partial"
    if destination_is_exact and partial is None:
        return "destination"
    return "indeterminate"


def reconcile_capsule_publication(incident: CapsulePublicationIncident) -> str:
    """Remove only the exact failed-publication inode named by an incident.

    The current namespace is independently reconstructed.  Cleanup proceeds
    only if exactly one authoritative name still selects the retained inode;
    a replacement, disappearance, or dual/otherwise ambiguous state rejects
    without deleting anything.  The returned value records whether the inode
    was recovered from the hidden partial or the public destination name.
    """

    if type(incident) is not CapsulePublicationIncident:
        _fail("publication reconciliation requires one exact incident")
    destination = _absolute_normal_path(
        incident.destination, "publication-incident destination"
    )
    if PARTIAL_NAME.fullmatch(incident.partial_name) is None:
        _fail("publication-incident partial name is invalid")
    identity = incident.expected_identity
    if (
        type(identity) is not tuple
        or len(identity) != 2
        or any(type(value) is not int or value < 0 for value in identity)
    ):
        _fail("publication incident lacks one exact inode identity")
    parent_fd = _open_absolute_directory(
        destination.parent, "publication reconciliation parent"
    )
    try:
        _require_private_directory_fd(parent_fd, "publication reconciliation parent")
        location = _capsule_publication_location(
            parent_fd, incident.partial_name, destination.name, identity
        )
        if location not in ("partial", "destination"):
            _fail("failed publication remains indeterminate; refusing cleanup")
        cleanup_name = (
            incident.partial_name if location == "partial" else destination.name
        )
        _remove_tree_at(parent_fd, cleanup_name, identity)
        return location
    finally:
        os.close(parent_fd)


def revalidate_staged_capsule(root: Any, entries: Iterable[CapsuleEntry]) -> None:
    path = _absolute_normal_path(root, "staged capsule root")
    validated = _validated_entries(entries)
    parent_fd = _open_absolute_directory(path.parent, "staged capsule parent")
    root_fd = -1
    try:
        root_fd = os.open(
            path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        _revalidate_tree_fd(root_fd, validated)
    except OSError as exc:
        raise CapsuleError("staged capsule cannot be reopened safely") from exc
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        os.close(parent_fd)


def stage_capsule(archive_path: Any, profile_record: Any, destination: Any) -> StagedCapsule:
    """Stream, validate, privately stage, then atomically publish one capsule.

    The strict raw profile mapping is always revalidated; a constructed
    :class:`CapsuleProfile` is intentionally not accepted as a shortcut.
    """

    _require_linux_descriptor_capabilities()
    profile = validate_capsule_profile(profile_record)
    archive_source: Any
    if type(archive_path) is int:
        if archive_path < 0:
            _fail("held capsule archive descriptor is invalid")
        archive_source = archive_path
    else:
        archive_source = _absolute_normal_path(archive_path, "capsule archive path")
    destination_path = _absolute_normal_path(destination, "capsule destination")
    if PATH_COMPONENT.fullmatch(destination_path.name) is None:
        _fail("capsule destination basename is not portable ASCII")
    partial_name = ".cp2-stage-" + secrets.token_hex(16)
    partial_identity: Optional[Tuple[int, int]] = None
    partial_created = False
    partial_opened = False
    published = False
    root_fd = -1
    final_fd = -1
    parent_fd = -1
    archive: Optional[_BoundArchive] = None
    try:
        archive = _BoundArchive(archive_source, profile)
        parent_fd = _open_absolute_directory(
            destination_path.parent, "capsule destination parent"
        )
        _require_private_directory_fd(parent_fd, "capsule destination parent")
        try:
            os.stat(destination_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("capsule destination already exists")
        os.mkdir(partial_name, 0o700, dir_fd=parent_fd)
        partial_created = True
        partial_status = os.stat(partial_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(partial_status.st_mode) or partial_status.st_uid != os.geteuid():
            _fail("capsule partial root creation identity differs")
        partial_identity = _inode_identity(partial_status)
        if stat.S_IMODE(partial_status.st_mode) != 0o700:
            _fail("capsule partial root mode was filtered from 0700")
        root_fd = os.open(
            partial_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        partial_opened = True
        if _inode_identity(
            _require_private_directory_fd(root_fd, "capsule partial root")
        ) != partial_identity:
            _fail("capsule partial root path/descriptor identity differs")
        if archive.read_exact(len(CAPSULE_MAGIC), "magic") != CAPSULE_MAGIC:
            _fail("capsule magic differs")
        if archive.u64("member count") != len(profile.entries):
            _fail("capsule member count differs from profile")
        for index, entry in enumerate(profile.entries):
            _stream_member(archive, root_fd, entry, index)
        if os.read(archive.fd, 1):
            _fail("capsule has trailing bytes")
        archive.revalidate()
        _revalidate_tree_fd(root_fd, profile.entries)

        # Reopen the absolute parent and require it still names the held parent
        # before publishing a path that will be returned to the caller.
        fresh_parent_fd = _open_absolute_directory(destination_path.parent, "capsule destination parent")
        try:
            if _identity(os.fstat(fresh_parent_fd)) != _identity(os.fstat(parent_fd)):
                _fail("capsule destination parent path identity changed")
        finally:
            os.close(fresh_parent_fd)
        _publication_parent_fsync(parent_fd)
        try:
            _rename_noreplace(parent_fd, partial_name, destination_path.name)
        except BaseException:
            location = _capsule_publication_location(
                parent_fd, partial_name, destination_path.name, partial_identity
            )
            if location == "destination":
                # The syscall completed before an injected/asynchronous error.
                # Failure cleanup must now target the exact destination inode.
                published = True
            elif location != "partial":
                _fail("capsule publication interruption left an indeterminate exact inode")
            raise
        published = True
        _publication_parent_fsync(parent_fd)

        final_fd = os.open(
            destination_path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        if _inode_identity(os.fstat(final_fd)) != partial_identity:
            _fail("published capsule identity differs from partial")
        if _inode_identity(os.fstat(root_fd)) != partial_identity:
            _fail("held capsule identity differs after publication")
        _revalidate_tree_fd(final_fd, profile.entries)
        _revalidate_tree_fd(root_fd, profile.entries)
        archive.revalidate()
        result = StagedCapsule(
            destination_path, profile.profile_id, profile.profile_sha256,
            profile.capsule_kind, profile.target.machine, profile.archive_sha256,
            profile.inventory_sha256, profile.environment_sha256, profile.entries,
        )
        # The injected test boundary is intentionally the final substantive
        # operation before success.  A same-root-inode content/mode swap must
        # enter the ordinary rollback path, never become a successful return.
        _publication_return_revalidate(final_fd, profile.entries)
        archive.revalidate()
        os.close(final_fd)
        final_fd = -1
        os.close(root_fd)
        root_fd = -1
        archive.close()
        archive = None
        os.close(parent_fd)
        parent_fd = -1
        return result
    except BaseException as exc:
        if final_fd >= 0:
            os.close(final_fd)
        if root_fd >= 0:
            os.close(root_fd)
        if archive is not None:
            archive.close()
        incident: Optional[CapsulePublicationIncident] = None
        incident_cause: Optional[BaseException] = None
        if partial_created and parent_fd >= 0:
            if partial_identity is not None:
                location = _capsule_publication_location(
                    parent_fd, partial_name, destination_path.name, partial_identity
                )
                if location == "destination":
                    published = True
                elif location == "partial":
                    published = False
                else:
                    incident = CapsulePublicationIncident(
                        "capsule staging failed and exact publication state is indeterminate",
                        destination_path,
                        partial_name,
                        partial_identity,
                        "indeterminate",
                    )
                    incident_cause = exc
            if incident is None:
                cleanup_name = destination_path.name if published else partial_name
                try:
                    if partial_identity is None:
                        cleanup_status = os.stat(
                            cleanup_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if (
                            not stat.S_ISDIR(cleanup_status.st_mode)
                            or cleanup_status.st_uid != os.geteuid()
                        ):
                            _fail("refusing to clean an unbound capsule partial")
                        partial_identity = _inode_identity(cleanup_status)
                    if partial_opened:
                        _remove_tree_at(parent_fd, cleanup_name, partial_identity)
                    else:
                        cleanup_status = os.stat(
                            cleanup_name, dir_fd=parent_fd, follow_symlinks=False
                        )
                        if _inode_identity(cleanup_status) != partial_identity:
                            _fail("refusing to remove a replacement empty capsule partial")
                        os.rmdir(cleanup_name, dir_fd=parent_fd)
                        _publication_parent_fsync(parent_fd)
                except BaseException as cleanup_exc:
                    state = (
                        _capsule_publication_location(
                            parent_fd,
                            partial_name,
                            destination_path.name,
                            partial_identity,
                        )
                        if partial_identity is not None
                        else "indeterminate"
                    )
                    incident = CapsulePublicationIncident(
                        "capsule staging failed and safe cleanup could not complete",
                        destination_path,
                        partial_name,
                        partial_identity,
                        state,
                    )
                    incident_cause = cleanup_exc
        if parent_fd >= 0:
            os.close(parent_fd)
        if incident is not None:
            raise incident from incident_cause
        if isinstance(exc, CapsuleError):
            raise
        if isinstance(exc, OSError):
            raise CapsuleError("capsule staging failed") from exc
        raise


# Linux sealed-memfd/mount-namespace execution boundary.  These values are
# intentionally exact: accepting a subset of seals would reintroduce a
# pre-opened-writer path after the staged tree has been validated.
_SANDBOX_SEALS = (
    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
)
_SANDBOX_PAGE_BYTES = 4096
_SANDBOX_GUARD_INODES = 64
_SANDBOX_MAX_TMPFS_BYTES = 2 << 30
_SANDBOX_PRIVATE_DIRECTORIES = (
    "/private",
    "/private/home",
    "/private/mpl",
    "/private/preflight",
    "/private/tmp",
    "/private/work",
    "/private/xdg-cache",
    "/private/xdg-config",
    "/proc",
)


def _sealed_memfd(payload: bytes, mode: int, label: str) -> int:
    if (
        type(payload) is not bytes
        or mode not in FILE_MODES
        or not hasattr(os, "memfd_create")
        or not hasattr(os, "MFD_ALLOW_SEALING")
    ):
        _fail(label + " cannot be represented by one sealed memfd")
    descriptor = -1
    try:
        descriptor = os.memfd_create(
            "schurvio-cp2-sealed",
            os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail(label + " sealed-memfd write made no progress")
            offset += count
        os.fsync(descriptor)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, _SANDBOX_SEALS)
        status_value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status_value.st_mode)
            or status_value.st_uid != os.geteuid()
            or status_value.st_nlink != 0
            or stat.S_IMODE(status_value.st_mode) != mode
            or status_value.st_size != len(payload)
            or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != _SANDBOX_SEALS
        ):
            _fail(label + " sealed-memfd identity differs")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = len(payload)
        while remaining:
            block = os.read(descriptor, min(IO_CHUNK_BYTES, remaining))
            if not block:
                _fail(label + " sealed-memfd became short")
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or digest.hexdigest() != hashlib.sha256(payload).hexdigest():
            _fail(label + " sealed-memfd bytes differ")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise


def _empty_output_memfd(label: str) -> int:
    if not hasattr(os, "memfd_create") or not hasattr(os, "MFD_ALLOW_SEALING"):
        _fail(label + " cannot be represented by one anonymous output memfd")
    descriptor = os.memfd_create(
        "schurvio-cp2-output",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    try:
        os.fchmod(descriptor, 0o600)
        status_value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status_value.st_mode)
            or status_value.st_uid != os.geteuid()
            or status_value.st_nlink != 0
            or stat.S_IMODE(status_value.st_mode) != 0o600
            or status_value.st_size != 0
            or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != 0
        ):
            _fail(label + " output memfd identity differs")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_bound_entry(root_fd: int, entry: CapsuleEntry) -> bytes:
    components = entry.path.split("/")
    parent_fd = os.dup(root_fd)
    descriptor = -1
    try:
        for component in components[:-1]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
            os.close(parent_fd)
            parent_fd = next_fd
            _require_private_directory_fd(parent_fd, "sealed capsule member parent")
        descriptor = os.open(
            components[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        before = os.fstat(descriptor)
        by_name = os.stat(
            components[-1], dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            _identity(before) != _identity(by_name)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != entry.mode
            or before.st_size != entry.size
        ):
            _fail("staged capsule member differs before sealed copy: " + entry.path)
        digest = hashlib.sha256()
        payload = bytearray()
        remaining = entry.size
        while remaining:
            block = os.read(descriptor, min(IO_CHUNK_BYTES, remaining))
            if not block:
                _fail("staged capsule member became short: " + entry.path)
            payload.extend(block)
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or digest.hexdigest() != entry.sha256:
            _fail("staged capsule member bytes differ: " + entry.path)
        if (
            _identity(os.fstat(descriptor)) != _identity(before)
            or _identity(
                os.stat(components[-1], dir_fd=parent_fd, follow_symlinks=False)
            )
            != _identity(before)
        ):
            _fail("staged capsule member changed during sealed copy: " + entry.path)
        return bytes(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _sandbox_directories(entries: Sequence[CapsuleEntry]) -> Tuple[str, ...]:
    values = {"/capsule", *_SANDBOX_PRIVATE_DIRECTORIES}
    for relative in _expected_directories(entries):
        values.add("/capsule/" + relative)
    return tuple(
        sorted(values, key=lambda value: (value.count("/"), value.encode("ascii")))
    )


class CapsuleSandboxInvocation:
    """One descriptor-complete sandbox argv and its anonymous transports."""

    def __init__(
        self,
        owner: "SealedCapsuleSandbox",
        command: Sequence[str],
        read_only_inputs: Mapping[str, bytes],
        writable_outputs: Mapping[str, int],
    ) -> None:
        self._owner = owner
        self._extra_fds: Dict[str, int] = {}
        self._output_fds: Dict[str, int] = {}
        self._output_limits: Dict[str, int] = {}
        self._closed = False
        try:
            normalized_command = tuple(command)
            if (
                not normalized_command
                or any(type(value) is not str for value in normalized_command)
                or not normalized_command[0].startswith("/capsule/")
            ):
                _fail("capsule sandbox command differs")
            read_only_rows = [
                ("/capsule/" + entry.path, owner._fds[entry.path], entry.mode)
                for entry in owner.entries
            ]
            for destination, payload in sorted(read_only_inputs.items()):
                if (
                    type(destination) is not str
                    or not destination.startswith("/private/")
                    or _relpath(destination[1:], "sandbox input destination")
                    != destination[1:]
                    or type(payload) is not bytes
                    or not payload
                    or len(payload) > MAX_MEMBER_BYTES
                ):
                    _fail("capsule sandbox input differs")
                descriptor = _sealed_memfd(
                    payload, 0o444, "capsule sandbox input " + destination
                )
                self._extra_fds[destination] = descriptor
                read_only_rows.append((destination, descriptor, 0o444))
            if type(writable_outputs) is not dict:
                try:
                    output_values = dict(writable_outputs)
                except (TypeError, ValueError) as exc:
                    raise CapsuleError(
                        "capsule sandbox writable-output mapping differs"
                    ) from exc
            else:
                output_values = dict(writable_outputs)
            if len(output_values) > 1:
                _fail("capsule sandbox writable-output population differs")
            for destination, maximum in output_values.items():
                if (
                    type(destination) is not str
                    or not destination.startswith("/private/")
                    or _relpath(destination[1:], "sandbox output destination")
                    != destination[1:]
                    or destination in self._extra_fds
                    or type(maximum) is not int
                    or maximum <= 0
                    or maximum > MAX_MEMBER_BYTES
                ):
                    _fail("capsule sandbox output differs")
                self._output_fds[destination] = _empty_output_memfd(
                    "capsule sandbox output " + destination
                )
                self._output_limits[destination] = maximum
            all_destinations = [row[0] for row in read_only_rows] + list(
                self._output_fds
            )
            if (
                len(all_destinations) != len(set(all_destinations))
                or normalized_command[0] not in all_destinations
            ):
                _fail("capsule sandbox destination/command join differs")
            parent_directories = set(owner.directories)
            if any(
                str(PurePosixPath(destination).parent) not in parent_directories
                for destination in all_destinations
            ):
                _fail("capsule sandbox transport parent is outside the closed tree")
            read_only_rows.sort(key=lambda row: row[0].encode("ascii"))
            writable_rows = sorted(
                self._output_fds.items(), key=lambda row: row[0].encode("ascii")
            )
            page = _SANDBOX_PAGE_BYTES
            payload_bytes = sum(
                ((os.fstat(descriptor).st_size + page - 1) // page) * page
                for _, descriptor, _ in read_only_rows
            )
            payload_bytes += sum(
                ((self._output_limits[destination] + page - 1) // page) * page
                for destination, _ in writable_rows
            )
            inode_population = (
                len(owner.directories)
                + len(read_only_rows)
                + len(writable_rows)
                + _SANDBOX_GUARD_INODES
            )
            tmpfs_size = payload_bytes + ((inode_population + page - 1) // page) * page
            if tmpfs_size <= 0 or tmpfs_size > _SANDBOX_MAX_TMPFS_BYTES:
                _fail("capsule sandbox checked tmpfs size exceeds its bound")
            arguments = [
                owner.executable,
                "--tmpfs-size-bytes",
                str(tmpfs_size),
                "--directory-count",
                str(len(owner.directories)),
                *owner.directories,
                "--read-only-file-count",
                str(len(read_only_rows)),
            ]
            for destination, descriptor, mode in read_only_rows:
                arguments.extend((str(descriptor), format(mode, "o"), destination))
            arguments.extend(("--writable-file-count", str(len(writable_rows))))
            for destination, descriptor in writable_rows:
                arguments.extend(
                    (
                        str(descriptor),
                        "600",
                        str(self._output_limits[destination]),
                        destination,
                    )
                )
            arguments.extend(("--cwd", "/private/work", "--", *normalized_command))
            self.argv = tuple(arguments)
            self.executable = owner.executable
            self.pass_fds = tuple(
                sorted(
                    set(owner._fds.values())
                    | set(self._extra_fds.values())
                    | set(self._output_fds.values())
                )
            )
        except BaseException:
            self.close()
            raise

    def seal_output(self, destination: str, maximum: int) -> bytes:
        if self._closed or destination not in self._output_fds:
            _fail("capsule sandbox output is not retained")
        if (
            type(maximum) is not int
            or maximum <= 0
            or maximum != self._output_limits[destination]
        ):
            _fail("capsule sandbox output bound differs")
        descriptor = self._output_fds[destination]
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 0
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size <= 0
            or before.st_size > maximum
            or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != 0
        ):
            _fail("capsule sandbox output identity differs before sealing")
        try:
            fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, _SANDBOX_SEALS)
        except OSError as exc:
            raise CapsuleError(
                "capsule sandbox output retains a writable descriptor"
            ) from exc
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != _SANDBOX_SEALS:
            _fail("capsule sandbox output seal set differs")
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(IO_CHUNK_BYTES, remaining))
            if not block:
                _fail("capsule sandbox output became short")
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or _identity(os.fstat(descriptor)) != _identity(before):
            # Adding seals changes ctime but no content-bearing identity.  Check
            # the fields that must remain stable across F_ADD_SEALS explicitly.
            after = os.fstat(descriptor)
            if (
                after.st_dev != before.st_dev
                or after.st_ino != before.st_ino
                or after.st_mode != before.st_mode
                or after.st_nlink != before.st_nlink
                or after.st_uid != before.st_uid
                or after.st_gid != before.st_gid
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
            ):
                _fail("capsule sandbox output changed while sealing")
        return b"".join(chunks)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in tuple(self._extra_fds.values()) + tuple(
            self._output_fds.values()
        ):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._extra_fds.clear()
        self._output_fds.clear()
        self._output_limits.clear()

    def __enter__(self) -> "CapsuleSandboxInvocation":
        if self._closed:
            _fail("capsule sandbox invocation is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


class SealedCapsuleSandbox:
    """Sealed copy of every staged capsule member, including its static runner."""

    def __init__(self, staged: StagedCapsule) -> None:
        if type(staged) is not StagedCapsule:
            _fail("sealed capsule sandbox requires one staged capsule")
        self.entries = _validated_entries(staged.entries)
        self.directories = _sandbox_directories(self.entries)
        self._fds: Dict[str, int] = {}
        self._closed = False
        root_fd = -1
        try:
            revalidate_staged_capsule(staged.root, self.entries)
            root_fd = _open_absolute_directory(staged.root, "staged capsule root")
            _require_private_directory_fd(root_fd, "staged capsule root")
            _revalidate_tree_fd(root_fd, self.entries)
            for entry in self.entries:
                payload = _read_bound_entry(root_fd, entry)
                self._fds[entry.path] = _sealed_memfd(
                    payload, entry.mode, "capsule member " + entry.path
                )
            _revalidate_tree_fd(root_fd, self.entries)
            sandbox_paths = tuple(
                entry.path for entry in self.entries if entry.role == "capsule_sandbox"
            )
            if sandbox_paths != ("bin/sandbox",):
                _fail("capsule must retain exactly bin/sandbox")
            self.executable = "/proc/{}/fd/{}".format(
                os.getpid(), self._fds["bin/sandbox"]
            )
        except BaseException:
            self.close()
            raise
        finally:
            if root_fd >= 0:
                os.close(root_fd)

    def invocation(
        self,
        command: Sequence[str],
        *,
        read_only_inputs: Optional[Mapping[str, bytes]] = None,
        writable_outputs: Optional[Mapping[str, int]] = None,
    ) -> CapsuleSandboxInvocation:
        if self._closed:
            _fail("sealed capsule sandbox is closed")
        return CapsuleSandboxInvocation(
            self,
            command,
            {} if read_only_inputs is None else read_only_inputs,
            {} if writable_outputs is None else writable_outputs,
        )

    def read_member(self, relative: str, maximum: int) -> bytes:
        if (
            self._closed
            or relative not in self._fds
            or type(maximum) is not int
            or maximum <= 0
        ):
            _fail("sealed capsule member read differs")
        entry = next(item for item in self.entries if item.path == relative)
        if entry.size > maximum:
            _fail("sealed capsule member exceeds its read bound")
        descriptor = self._fds[relative]
        payload = bytearray()
        offset = 0
        while offset < entry.size:
            block = os.pread(
                descriptor, min(IO_CHUNK_BYTES, entry.size - offset), offset
            )
            if not block:
                _fail("sealed capsule member became short: " + relative)
            payload.extend(block)
            offset += len(block)
        if (
            hashlib.sha256(payload).hexdigest() != entry.sha256
            or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != _SANDBOX_SEALS
        ):
            _fail("sealed capsule member bytes differ: " + relative)
        return bytes(payload)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in self._fds.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._fds.clear()

    def __enter__(self) -> "SealedCapsuleSandbox":
        if self._closed:
            _fail("sealed capsule sandbox is closed")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
