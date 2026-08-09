#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free construction and audit of the two minimal CP2-D capsules.

This tool never discovers project data or invokes a formal CP2-D entry point.
It consumes one already constructed private capsule root, validates every byte
and ELF dependency, runs only the retained synthetic preflight, and writes one
new ``.cp2cap`` plus its strict profile and construction receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cp2_capsule as capsule
import cp2_evaluator_result as evaluator_result


READELF = Path("/usr/bin/x86_64-linux-gnu-readelf")
READELF_SHA256 = "41a813335a74480f145fba9400aa7fb5c0ec7ffcaa7af7f33e832dc75eb90ffb"
SOURCE_LOCK_RELATIVE = "notices/cp2-capsule-source-lock.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_LOCK_PROJECT = PROJECT_ROOT / "project/cp2_capsule_source_lock.json"
OPENBLAS_RELATIVE = (
    "python/lib/python3.11/numpy.libs/"
    "libscipy_openblas64_-32a4b2a6.so"
)
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


class CapsuleBuildError(RuntimeError):
    """Raised whenever construction cannot prove the frozen capsule identity."""


def _fail(message: str) -> None:
    raise CapsuleBuildError(message)


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleBuildError("construction record is not canonical JSON") from exc


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def expected_identity_from_construction_directory(
    output_directory: Path,
) -> Mapping[str, Any]:
    """Reconstruct the external identity from both completed candidate pairs."""

    output_directory = Path(output_directory).absolute()
    source_lock = _read_regular(SOURCE_LOCK_PROJECT.resolve())
    artifacts = {}
    for kind in sorted(capsule.EXPECTED_CAPSULE_UNIT_PATHS):
        artifacts[kind] = (
            _read_regular(
                output_directory / (kind + ".cp2cap"), expected_mode=0o444
            ),
            _read_regular(
                output_directory / (kind + ".profile.json"), expected_mode=0o444
            ),
        )
    try:
        return capsule.make_expected_capsule_identity(source_lock, artifacts)
    except capsule.CapsuleError as exc:
        raise CapsuleBuildError(
            "constructed capsule pairs cannot form the external identity"
        ) from exc


def _read_regular(
    path: Path,
    *,
    expected_mode: Optional[int] = None,
    expected_uid: Optional[int] = None,
) -> bytes:
    required_uid = os.geteuid() if expected_uid is None else expected_uid
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != required_uid
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
            or (expected_mode is not None and stat.S_IMODE(before.st_mode) != expected_mode)
        ):
            _fail("construction input identity differs: " + str(path))
        chunks = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                _fail("construction input became short: " + str(path))
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            _fail("construction input grew: " + str(path))
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _fail("construction input changed: " + str(path))
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _contract_bindings() -> List[Mapping[str, Any]]:
    result = []
    for relative in capsule.CONTRACT_BINDING_PATHS:
        payload = _read_regular(PROJECT_ROOT.joinpath(*relative.split("/")))
        if not payload or len(payload) > capsule.MAX_SOURCE_LOCK_BYTES:
            _fail("capsule contract input is outside its byte bound: " + relative)
        result.append(
            {"path": relative, "size": len(payload), "sha256": _sha(payload)}
        )
    return result


def _write_new(path: Path, payload: bytes) -> None:
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        _fail("construction output must be normalized absolute")
    parent_status = os.stat(path.parent, follow_symlinks=False)
    if (
        not stat.S_ISDIR(parent_status.st_mode)
        or parent_status.st_uid != os.geteuid()
        or stat.S_IMODE(parent_status.st_mode) != 0o700
    ):
        _fail("construction output parent is not private")
    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=parent_fd,
        )
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail("construction output write made no progress")
            offset += count
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        os.fsync(parent_fd)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        try:
            os.unlink(path.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        except OSError:
            pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _output_identity(status_value: os.stat_result) -> Tuple[int, ...]:
    return (
        status_value.st_dev,
        status_value.st_ino,
        status_value.st_mode,
        status_value.st_nlink,
        status_value.st_uid,
        status_value.st_gid,
        status_value.st_size,
        status_value.st_mtime_ns,
        status_value.st_ctime_ns,
    )


def _bind_created_output(path: Path, size: int, digest: str) -> Tuple[int, ...]:
    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
        before = os.fstat(descriptor)
        by_path = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        identity = _output_identity(before)
        if (
            identity != _output_identity(by_path)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size != size
        ):
            _fail("constructed output identity differs: " + str(path))
        observed = hashlib.sha256()
        remaining = size
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                _fail("constructed output became short: " + str(path))
            observed.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or observed.hexdigest() != digest:
            _fail("constructed output bytes differ: " + str(path))
        if _output_identity(os.fstat(descriptor)) != identity:
            _fail("constructed output changed while binding: " + str(path))
        return identity
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _remove_bound_output(
    path: Path, identity: Tuple[int, ...], digest: str
) -> None:
    """Rollback one output only while its complete retained identity matches."""

    parent_fd = os.open(
        path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    descriptor = -1
    try:
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd
        )
        before = os.fstat(descriptor)
        by_path = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _output_identity(before) != identity
            or _output_identity(by_path) != identity
        ):
            _fail("refusing to remove a replacement construction output")
        observed = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                _fail("construction rollback output became short")
            observed.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or observed.hexdigest() != digest:
            _fail("refusing to remove changed construction output")
        if (
            _output_identity(os.fstat(descriptor)) != identity
            or _output_identity(
                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            )
            != identity
        ):
            _fail("construction output changed before rollback")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _role(kind: str, relative: str, payload: bytes) -> str:
    if relative == "bin/sandbox":
        return "capsule_sandbox"
    if relative == "bin/launcher":
        return "capsule_launcher"
    if relative == "python/bin/python3.11":
        return "python_interpreter"
    if relative == "native/ld-linux-x86-64.so.2":
        return "native_loader"
    if relative.startswith("native/"):
        return "native_library"
    if relative == SOURCE_LOCK_RELATIVE:
        return "source_lock"
    if relative.startswith("notices/"):
        return "license_notice"
    if relative.startswith("fixtures/"):
        if kind == "direct_math":
            if relative.endswith("direct-kat-known-answers.json"):
                return "direct_math_known_answer"
            if relative.endswith("direct-kat-request.json"):
                return "preflight_fixture"
        else:
            if relative.endswith("expected-result-bits.json"):
                return "preflight_known_answer"
            if relative.endswith(("ground-truth.tum", "estimate.tum")):
                return "preflight_fixture"
        _fail("unexpected capsule fixture: " + relative)
    prefix = "python/lib/python3.11/"
    if not relative.startswith(prefix):
        _fail("unclassified capsule member: " + relative)
    suffix = relative[len(prefix) :]
    if suffix.startswith("numpy.libs/"):
        return "native_library"
    if suffix.startswith("numpy-2.4.6.dist-info/"):
        return "numpy_distribution_metadata"
    if suffix.startswith("numpy/"):
        return "numpy_extension" if payload.startswith(b"\x7fELF") else "numpy_module"
    if suffix == "cp2_equivalent_evaluator.py":
        if kind != "evaluator":
            _fail("direct capsule contains the equivalent evaluator")
        return "evaluator_module"
    if suffix in {
        "cp2_sequence_math_worker.py",
        "cp2_sequence_math.py",
        "cp2_sequence_math_codec.py",
        "cp2_f64_codec.py",
        "cp2_direct_kat.py",
    }:
        if kind != "direct_math":
            _fail("evaluator capsule contains a direct-math module")
        return "direct_math_module"
    if payload.startswith(b"\x7fELF"):
        return "python_extension"
    return "python_stdlib"


def inventory_root(kind: str, root: Path) -> Tuple[capsule.CapsuleEntry, ...]:
    if kind not in capsule.CAPSULE_KINDS:
        _fail("capsule kind differs")
    if not root.is_absolute() or os.path.normpath(str(root)) != str(root):
        _fail("capsule root is not normalized absolute")
    root_status = os.stat(root, follow_symlinks=False)
    if (
        not stat.S_ISDIR(root_status.st_mode)
        or root_status.st_uid != os.geteuid()
        or stat.S_IMODE(root_status.st_mode) != 0o700
    ):
        _fail("capsule root is not one private directory")
    entries = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        status_value = os.stat(path, follow_symlinks=False)
        if stat.S_ISDIR(status_value.st_mode):
            if status_value.st_uid != os.geteuid() or stat.S_IMODE(status_value.st_mode) != 0o700:
                _fail("capsule directory is not private: " + str(path))
            continue
        if not stat.S_ISREG(status_value.st_mode):
            _fail("capsule contains a link or nonregular member: " + str(path))
        relative = path.relative_to(root).as_posix()
        payload = _read_regular(path)
        role = _role(kind, relative, payload)
        mode = 0o555 if role in capsule.EXECUTABLE_ROLES else 0o444
        if stat.S_IMODE(status_value.st_mode) != mode:
            _fail("capsule member mode differs: " + relative)
        entries.append(capsule.CapsuleEntry(relative, role, mode, len(payload), _sha(payload)))
    return capsule._validated_entries(entries)


def _readelf(path: Path) -> str:
    if _sha(_read_regular(READELF, expected_mode=0o755, expected_uid=0)) != READELF_SHA256:
        _fail("readelf executable identity differs")
    completed = subprocess.run(
        [str(READELF), "--wide", "--file-header", "--program-headers", "--dynamic", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
    )
    if completed.returncode != 0 or completed.stderr:
        _fail("readelf failed closed for " + str(path))
    return completed.stdout.decode("ascii", "strict")


def _elf_identity(relative: str, role: str, root: Path) -> capsule.NativeConsumer:
    document = _readelf(root / relative)
    type_match = re.search(r"^\s*Type:\s+(EXEC|DYN)\b", document, re.MULTILINE)
    if type_match is None:
        _fail("ELF type is absent: " + relative)
    elf_type = "ET_" + type_match.group(1)
    interpreter_match = re.search(
        r"Requesting program interpreter:\s*([^\]]+)\]", document
    )
    interpreter = interpreter_match.group(1) if interpreter_match else "none"
    needed = tuple(re.findall(r"\(NEEDED\).*Shared library: \[([^\]]+)\]", document))
    soname_values = re.findall(r"\(SONAME\).*Library soname: \[([^\]]+)\]", document)
    if len(soname_values) > 1:
        _fail("ELF has multiple SONAME values: " + relative)
    soname = soname_values[0] if soname_values else "none"
    rpath_values = re.findall(r"\(RPATH\).*Library rpath: \[([^\]]*)\]", document)
    runpath_values = re.findall(r"\(RUNPATH\).*Library runpath: \[([^\]]*)\]", document)
    if len(rpath_values) > 1 or len(runpath_values) > 1:
        _fail("ELF has multiple search-path tags: " + relative)
    rpath = tuple(rpath_values[0].split(":")) if rpath_values and rpath_values[0] else ()
    runpath = tuple(runpath_values[0].split(":")) if runpath_values and runpath_values[0] else ()
    if role in ("capsule_sandbox", "capsule_launcher"):
        linkage = "static-executable"
    elif role == "python_interpreter":
        linkage = "dynamic-executable"
    elif role == "native_loader":
        linkage = "dynamic-loader"
    else:
        linkage = "shared-object"
    return capsule.NativeConsumer(
        relative, elf_type, linkage, interpreter, rpath, runpath, soname, needed
    )


def native_closure(
    kind: str, root: Path, entries: Sequence[capsule.CapsuleEntry], module: str
) -> Mapping[str, Any]:
    by_path = {entry.path: entry for entry in entries}
    consumers = tuple(
        sorted(
            _elf_identity(entry.path, entry.role, root)
            for entry in entries
            if entry.role in capsule.NATIVE_CONSUMER_ROLES
        )
    )
    mapped_paths = tuple(
        sorted(
            entry.path
            for entry in entries
            if entry.role in ("native_loader", "native_library")
        )
    )
    providers: Dict[str, str] = {}
    for consumer in consumers:
        if consumer.path not in mapped_paths or consumer.soname == "none":
            continue
        if consumer.soname in providers:
            _fail("native provider SONAME is ambiguous: " + consumer.soname)
        providers[consumer.soname] = consumer.path
    edges = []
    for consumer in consumers:
        for needed in consumer.needed:
            provider = providers.get(needed)
            if provider is None:
                _fail("native dependency is unresolved: {} -> {}".format(consumer.path, needed))
            edges.append(capsule.NativeEdge(consumer.path, needed, provider))
    edges.sort()
    directories = (
        ("native", "python/lib", "python/lib/python3.11/numpy.libs")
        if kind == "direct_math"
        else ("native", "python/lib")
    )
    token = ":".join(
        {
            "native": "${HELD_NATIVE_DIR}",
            "python/lib": "${HELD_PYTHON_LIB_DIR}",
            "python/lib/python3.11/numpy.libs": "${HELD_NUMPY_LIBS_DIR}",
        }[item]
        for item in directories
    )
    consumer_rows = [
        {
            "path": item.path,
            "elf_type": item.elf_type,
            "linkage": item.linkage,
            "interpreter": item.interpreter,
            "rpath": list(item.rpath),
            "runpath": list(item.runpath),
            "soname": item.soname,
            "needed": list(item.needed),
        }
        for item in consumers
    ]
    edge_rows = [
        {"consumer": item.consumer, "needed": item.needed, "provider": item.provider}
        for item in edges
    ]
    mapped_entries = tuple(by_path[path] for path in mapped_paths)
    return {
        "loader_path": "native/ld-linux-x86-64.so.2",
        "loader_argv_prefix": [
            "${HELD_LOADER_FD}",
            "--inhibit-cache",
            "--library-path",
            token,
            "${HELD_PYTHON_FD}",
            "-I",
            "-S",
            "-B",
            "-m",
            module,
        ],
        "effective_library_directories": list(directories),
        "cache_policy": "inhibit-cache",
        "default_search_policy": "runtime-maps-reject-outside-capsule",
        "pathname_policy": "descriptor-held-loader-python-and-library-directories",
        "mapped_paths": list(mapped_paths),
        "consumers": consumer_rows,
        "needed_edges": edge_rows,
        "inventory_sha256": capsule.inventory_sha256(mapped_entries),
        "consumers_sha256": hashlib.sha256(
            capsule._native_consumers_bytes(consumers)
        ).hexdigest(),
        "edges_sha256": hashlib.sha256(capsule._native_edges_bytes(edges)).hexdigest(),
    }


def _sandbox_environment() -> Mapping[str, str]:
    result = {}
    for name, value in capsule.EXACT_ENVIRONMENT.items():
        result[name] = (
            "/private/" + value.rsplit("/", 1)[-1]
            if value.startswith("${PRIVATE_ROOT}/")
            else value
        )
    return result


def _command_record(argv: Sequence[str], stdout: bytes, stderr: bytes) -> Mapping[str, Any]:
    return {
        "argv": list(argv),
        "cwd": "${PRIVATE_ROOT}/work",
        "timeout_seconds": 120,
        "stdin_policy": "devnull",
        "exit_code": 0,
        "stdout_size": len(stdout),
        "stdout_sha256": _sha(stdout),
        "stderr_size": len(stderr),
        "stderr_sha256": _sha(stderr),
    }


def _run_sealed_preflight(
    sandbox: capsule.SealedCapsuleSandbox,
    command: Sequence[str],
    environment: Mapping[str, str],
    *,
    output_destination: Optional[str] = None,
    output_maximum: Optional[int] = None,
) -> Tuple[subprocess.CompletedProcess[bytes], Optional[bytes]]:
    writable = (
        {}
        if output_destination is None
        else {output_destination: output_maximum}
    )
    with sandbox.invocation(command, writable_outputs=writable) as invocation:
        completed = subprocess.run(
            invocation.argv,
            executable=invocation.executable,
            pass_fds=invocation.pass_fds,
            cwd="/tmp",
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
            start_new_session=True,
            check=False,
        )
        if completed.returncode != 0:
            _fail("sealed capsule diagnostic command failed closed")
        output = (
            None
            if output_destination is None
            else invocation.seal_output(output_destination, output_maximum)
        )
        return completed, output


def synthetic_preflight(
    kind: str,
    root: Path,
    entries: Sequence[capsule.CapsuleEntry],
) -> Mapping[str, Any]:
    inventory = tuple(entries)
    staged = capsule.StagedCapsule(
        Path(root),
        "construction-preflight",
        "0" * 64,
        kind,
        "x86_64",
        "0" * 64,
        capsule.inventory_sha256(inventory),
        capsule.environment_sha256(tuple(sorted(capsule.EXACT_ENVIRONMENT.items()))),
        inventory,
    )
    environment = _sandbox_environment()
    capsule.revalidate_staged_capsule(root, inventory)
    with capsule.SealedCapsuleSandbox(staged) as sandbox:
        version, no_output = _run_sealed_preflight(
            sandbox,
            ("/capsule/bin/launcher", "--version"),
            environment,
        )
        if no_output is not None:
            _fail("capsule version probe unexpectedly retained an output")
        launcher = "${CAPSULE_ROOT}/bin/launcher"
        if kind == "direct_math":
            actual_argv = [
                "/capsule/bin/launcher",
                "--input",
                "/capsule/fixtures/direct-kat-request.json",
                "--known-answers",
                "/capsule/fixtures/direct-kat-known-answers.json",
                "--output",
                "/private/preflight/response.json",
            ]
            template_argv = [
                launcher,
                "--input",
                "${CAPSULE_ROOT}/fixtures/direct-kat-request.json",
                "--known-answers",
                "${CAPSULE_ROOT}/fixtures/direct-kat-known-answers.json",
                "--output",
                "${PRIVATE_ROOT}/preflight/response.json",
            ]
            inputs = [
                "fixtures/direct-kat-known-answers.json",
                "fixtures/direct-kat-request.json",
            ]
            known = root / "fixtures/direct-kat-known-answers.json"
            codec = "cp2_f64_known_answer_bundle_v1"
            output_destination = "/private/preflight/response.json"
            output_maximum = 256 << 20
        else:
            actual_argv = [
                "/capsule/bin/launcher",
                "tum",
                "/capsule/fixtures/ground-truth.tum",
                "/capsule/fixtures/estimate.tum",
                "-r",
                "trans_part",
                "--t_max_diff",
                "0.01",
                "--save_results",
                "/private/preflight/results.zip",
                "--no_warnings",
            ]
            template_argv = [
                launcher,
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
            ]
            inputs = [
                "fixtures/estimate.tum",
                "fixtures/expected-result-bits.json",
                "fixtures/ground-truth.tum",
            ]
            known = root / "fixtures/expected-result-bits.json"
            codec = "cp2_translation_rmse_result_zip_v1"
            output_destination = "/private/preflight/results.zip"
            output_maximum = evaluator_result.MAX_ARCHIVE_BYTES
        completed, output_bytes = _run_sealed_preflight(
            sandbox,
            actual_argv,
            environment,
            output_destination=output_destination,
            output_maximum=output_maximum,
        )
        if type(output_bytes) is not bytes:
            _fail("capsule synthetic preflight retained no output")
        known_relative = known.relative_to(root).as_posix()
        known_bytes = sandbox.read_member(known_relative, 16 << 20)
        if kind == "evaluator":
            result = evaluator_result.parse_evaluator_result_archive(output_bytes)
            expected = json.loads(known_bytes.decode("ascii"))
            if expected != {
                "archive_sha256": _sha(output_bytes),
                "error_bits": list(result.error_bits),
                "rmse_bits": struct.pack(">d", result.statistics.rmse).hex(),
            }:
                _fail("evaluator known-answer bits differ")
        retained = {
            "version": _command_record([launcher, "--version"], version.stdout, version.stderr),
            "preflight": {
                "command": _command_record(template_argv, completed.stdout, completed.stderr),
                "input_paths": inputs,
                "output_codec": codec,
                "output_size": len(output_bytes),
                "output_sha256": _sha(output_bytes),
                "known_answer_bits_sha256": _sha(known_bytes),
            },
        }
    capsule.revalidate_staged_capsule(root, inventory)
    return retained


def _entry_rows(entries: Sequence[capsule.CapsuleEntry]) -> List[Mapping[str, Any]]:
    return [
        {
            "path": item.path,
            "role": item.role,
            "mode": item.mode,
            "size": item.size,
            "sha256": item.sha256,
        }
        for item in entries
    ]


def _numerical_runtime(kind: str, by_path: Mapping[str, capsule.CapsuleEntry]) -> Mapping[str, Any]:
    if kind == "evaluator":
        identity = {
            "runtime_kind": "stdlib-binary64-independent-v1",
            "implementation": "CPython-math-no-NumPy-no-SciPy-no-evo",
        }
    else:
        identity = {
            "runtime_kind": "numpy-openblas-fixed-dispatch-v1",
            "numpy_version": "2.4.6",
            "numpy_cpu_baseline": ["X86_V2"],
            "numpy_cpu_dispatch_targets": ["X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"],
            "numpy_disabled_targets": ["X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"],
            "numpy_effective_target_states": {
                "X86_V2": True,
                "X86_V3": False,
                "X86_V4": False,
                "AVX512_ICL": False,
                "AVX512_SPR": False,
            },
            "openblas_library_path": OPENBLAS_RELATIVE,
            "openblas_library_sha256": by_path[OPENBLAS_RELATIVE].sha256,
            "openblas_version": "0.3.31.188.0",
            "openblas_config": (
                "OpenBLAS 0.3.31.188.0  USE64BITINT DYNAMIC_ARCH "
                "NO_AFFINITY SkylakeX MAX_THREADS=64"
            ),
            "openblas_corename": "SkylakeX",
            "openblas_threads": 1,
            "openblas_parallel": 1,
            "cpuid": {
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
            },
        }
    return {"identity": identity, "sha256": _sha(_canonical(identity))}


def build_profile(
    kind: str,
    root: Path,
    entries: Sequence[capsule.CapsuleEntry],
    archive_size: int,
    archive_sha256: str,
    contract_bindings: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    by_path = {entry.path: entry for entry in entries}
    preflight = synthetic_preflight(kind, root, entries)
    environment = dict(capsule.EXACT_ENVIRONMENT)
    env_sha = capsule.environment_sha256(tuple(sorted(environment.items())))
    if kind == "evaluator":
        module_path = "python/lib/python3.11/cp2_equivalent_evaluator.py"
        module = "cp2_equivalent_evaluator"
        distributions = []
        execution_argv = [
            "${CAPSULE_ROOT}/bin/launcher", "tum", "{GT_SHARED}",
            "{MODE_SHARED_ALIGNED}", "-r", "trans_part", "--t_max_diff", "0.01",
            "--save_results", "{ABS_RESULT_ZIP}", "--no_warnings",
        ]
        dispatch = "stdlib-binary64-fixed-x86-64"
    else:
        module_path = "python/lib/python3.11/cp2_sequence_math_worker.py"
        module = "cp2_sequence_math_worker"
        numpy_paths = [
            entry.path
            for entry in entries
            if entry.role
            in ("numpy_module", "numpy_extension", "numpy_distribution_metadata")
        ]
        distributions = [
            {
                "name": "numpy",
                "version": "2.4.6",
                "paths": numpy_paths,
                "inventory_sha256": capsule.inventory_sha256(
                    tuple(by_path[path] for path in numpy_paths)
                ),
            }
        ]
        execution_argv = [
            "${CAPSULE_ROOT}/bin/launcher", "--input", "{ABS_REQUEST}",
            "--output", "{ABS_RESPONSE}",
        ]
        dispatch = "numpy-x86-v2-openblas-skylakex"
    source_lock = by_path[SOURCE_LOCK_RELATIVE]
    profile = {
        "schema_version": 2,
        "record_type": "cp2_d_capsule_profile",
        "checkpoint": "CP2-D",
        "contract_bindings": list(contract_bindings),
        "profile_id": "cp2-d-{}-{}".format(kind, capsule.inventory_sha256(entries)[:16]),
        "capsule_kind": kind,
        "target": {
            "os": "linux",
            "machine": "x86_64",
            "elf_class": 64,
            "endianness": "little",
            "libc_abi": "glibc-2.31-0ubuntu9.18",
            "loader_abi": "ld-linux-x86-64-glibc-2.31",
            "elf_interpreter": "/lib64/ld-linux-x86-64.so.2",
            "cpu_dispatch_policy": dispatch,
        },
        "source_lock": {
            "path": source_lock.path,
            "size": source_lock.size,
            "sha256": source_lock.sha256,
        },
        "archive": {"size": archive_size, "sha256": archive_sha256},
        "inventory": _entry_rows(entries),
        "inventory_sha256": capsule.inventory_sha256(entries),
        "entry_point": {
            "launcher_path": "bin/launcher",
            "interpreter_path": "python/bin/python3.11",
            "module_path": module_path,
            "module": module,
            "callable": "main",
        },
        "distributions": distributions,
        "native_closure": native_closure(kind, root, entries, module),
        "environment": {"variables": environment, "sha256": env_sha},
        "execution": {
            "argv_template": execution_argv,
            "cwd": "${PRIVATE_ROOT}/work",
            "timeout_seconds": 120,
            "stdin_policy": "devnull",
            "environment_sha256": env_sha,
            "injection_denylist": list(capsule.INJECTION_DENYLIST),
            "thread_policy": "verified-single-thread",
            "hash_seed_policy": "hash-order-independent",
        },
        "floating_point": {
            "format": "IEEE-754-binary64",
            "rounding_mode": "FE_TONEAREST",
            "subnormal_policy": "preserve",
            "control_register": "MXCSR",
            "control_value_hex": "0000000000001f80",
            "secondary_control_register": "X87_CW",
            "secondary_control_value_hex": "000000000000027f",
            "numpy_error_policy": (
                "invalid-divide-over-raise_under-ignore"
                if kind == "direct_math"
                else "not-applicable"
            ),
        },
        "numerical_runtime": _numerical_runtime(kind, by_path),
        "version_probe": preflight["version"],
        "synthetic_preflight": preflight["preflight"],
    }
    capsule.validate_capsule_profile(profile)
    return profile


def relocation_rehearsal(
    kind: str,
    archive_path: Any,
    profile_record: Mapping[str, Any],
    expected_preflight: Mapping[str, Any],
) -> Sequence[Mapping[str, Any]]:
    """Independently stage and execute the retained synthetic preflight twice.

    Both roots are reconstructed only from the just-written transport archive.
    This proves that neither the original construction path nor one lucky
    extraction location is needed by the launcher or numerical runtime.
    """

    records = []
    with tempfile.TemporaryDirectory(prefix="cp2-capsule-relocation-", dir="/tmp") as name:
        parent = Path(name)
        os.chmod(parent, 0o700)
        for stage_index in (1, 2):
            destination = parent / "stage-{}".format(stage_index)
            staged = capsule.stage_capsule(archive_path, profile_record, destination)
            capsule.revalidate_staged_capsule(destination, staged.entries)
            observed = synthetic_preflight(kind, destination, staged.entries)
            if observed != expected_preflight:
                _fail("relocated capsule preflight differs from construction root")
            records.append(
                {
                    "stage_index": stage_index,
                    "capsule_kind": staged.capsule_kind,
                    "profile_id": staged.profile_id,
                    "profile_sha256": staged.profile_sha256,
                    "archive_sha256": staged.archive_sha256,
                    "inventory_sha256": staged.inventory_sha256,
                    "environment_sha256": staged.environment_sha256,
                    "version_probe": observed["version"],
                    "synthetic_preflight": observed["preflight"],
                }
            )
    left = dict(records[0])
    right = dict(records[1])
    left.pop("stage_index")
    right.pop("stage_index")
    if left != right:
        _fail("two independent relocation receipts differ")
    return records


def construct(
    kind: str,
    root: Path,
    output_directory: Path,
) -> Mapping[str, Any]:
    entries = inventory_root(kind, root)
    project_lock = _read_regular(SOURCE_LOCK_PROJECT.resolve())
    retained_lock = _read_regular(root / SOURCE_LOCK_RELATIVE, expected_mode=0o444)
    try:
        project_lock_record = capsule.validate_embedded_source_lock(project_lock)
        retained_lock_record = capsule.validate_embedded_source_lock(retained_lock)
    except capsule.CapsuleError as exc:
        raise CapsuleBuildError("capsule source lock is invalid") from exc
    if retained_lock != project_lock:
        _fail("capsule source lock differs from the active project lock")
    contract_bindings = _contract_bindings()
    if (
        project_lock_record.get("profile_contract_bindings") != contract_bindings
        or retained_lock_record.get("profile_contract_bindings") != contract_bindings
    ):
        _fail("capsule source-lock contract bindings differ from exact source bytes")
    sources = tuple(
        capsule.CapsuleFileSource(entry, root / entry.path) for entry in entries
    )
    archive_path = output_directory / (kind + ".cp2cap")
    profile_path = output_directory / (kind + ".profile.json")
    receipt_path = output_directory / (kind + ".construction-receipt.json")
    output_paths = (archive_path, profile_path, receipt_path)
    for path in output_paths:
        try:
            os.stat(path, follow_symlinks=False)
        except FileNotFoundError:
            continue
        _fail("construction output already exists: " + str(path))

    created = []
    try:
        archive_size, archive_sha = capsule.encode_capsule_file(sources, archive_path)
        created.append(
            (
                archive_path,
                _bind_created_output(archive_path, archive_size, archive_sha),
                archive_sha,
            )
        )
        profile_record = build_profile(
            kind, root, entries, archive_size, archive_sha, contract_bindings
        )
        profile = capsule.validate_capsule_profile(profile_record)
        relocations = relocation_rehearsal(
            kind,
            archive_path,
            profile_record,
            {
                "version": profile_record["version_probe"],
                "preflight": profile_record["synthetic_preflight"],
            },
        )
        _write_new(profile_path, profile.canonical_bytes)
        created.append(
            (
                profile_path,
                _bind_created_output(
                    profile_path,
                    len(profile.canonical_bytes),
                    profile.profile_sha256,
                ),
                profile.profile_sha256,
            )
        )
        receipt = {
            "schema_version": 1,
            "record_type": "cp2_d_capsule_construction_receipt",
            "formal_evidence": False,
            "capsule_kind": kind,
            "archive": {"size": archive_size, "sha256": archive_sha},
            "profile": {
                "size": len(profile.canonical_bytes),
                "sha256": profile.profile_sha256,
            },
            "source_lock": {"size": len(project_lock), "sha256": _sha(project_lock)},
            "contract_bindings": profile_record["contract_bindings"],
            "inventory_sha256": profile.inventory_sha256,
            "environment_sha256": profile.environment_sha256,
            "native_consumers_sha256": profile.native_closure.consumers_sha256,
            "native_edges_sha256": profile.native_closure.edges_sha256,
            "numerical_runtime_sha256": profile.numerical_runtime.sha256,
            "readelf": {"path": str(READELF), "sha256": READELF_SHA256},
            "version_probe": profile_record["version_probe"],
            "synthetic_preflight": profile_record["synthetic_preflight"],
            "relocation_rehearsals": relocations,
        }
        receipt_bytes = _canonical(receipt)
        receipt_sha = _sha(receipt_bytes)
        _write_new(receipt_path, receipt_bytes)
        created.append(
            (
                receipt_path,
                _bind_created_output(receipt_path, len(receipt_bytes), receipt_sha),
                receipt_sha,
            )
        )
        return receipt
    except BaseException as exc:
        cleanup_error = None
        for path, identity, digest in reversed(created):
            try:
                _remove_bound_output(path, identity, digest)
            except BaseException as cleanup_exc:
                if cleanup_error is None:
                    cleanup_error = cleanup_exc
        if cleanup_error is not None:
            raise CapsuleBuildError(
                "capsule construction failed and exact output rollback could not complete"
            ) from cleanup_error
        raise


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=sorted(capsule.CAPSULE_KINDS))
    parser.add_argument("--root", required=True)
    parser.add_argument("--output-directory", required=True)
    options = parser.parse_args(arguments)
    root = Path(options.root)
    output = Path(options.output_directory)
    if not root.is_absolute() or not output.is_absolute():
        _fail("construction root/output must be absolute")
    construct(options.kind, root, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(os.sys.argv[1:])))
