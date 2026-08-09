#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independent capsule-local CP2-D translation-RMSE evaluator.

This is the authorization-approved exact API alternative to evo.  It consumes
the same already-common, already-aligned TUM populations and emits the frozen
population-bearing result archive without importing NumPy, SciPy, evo, or the
direct-math implementation.  Its independent arithmetic is compared with the
direct capsule under the frozen absolute-plus-relative tolerance.
"""

from __future__ import annotations

import errno
import io
import fcntl
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys
from typing import Mapping, Sequence, Tuple
import zipfile

import cp2_fp_control as fp_control


VERSION = "cp2-equivalent-translation-rmse-evaluator-v1"
REQUIRED_FIXED_ENVIRONMENT = {
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
PRIVATE_ENVIRONMENT_SUFFIXES = {
    "HOME": "home",
    "MPLCONFIGDIR": "mpl",
    "TMPDIR": "tmp",
    "XDG_CACHE_HOME": "xdg-cache",
    "XDG_CONFIG_HOME": "xdg-config",
}
TUM_HEADER = b"# timestamp tx ty tz qx qy qz qw\n"
MAX_INPUT_BYTES = 256 << 20
MAX_POSE_COUNT = 1_000_000
REQUIRED_MEMFD_SEALS = (
    fcntl.F_SEAL_WRITE
    | fcntl.F_SEAL_GROW
    | fcntl.F_SEAL_SHRINK
    | fcntl.F_SEAL_SEAL
)
FIXED_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
RESULT_MEMBERS = ("error_array.npy", "info.json", "stats.json")
RESULT_INFO_JSON = (
    b'{"title":"APE w.r.t. translation part (m)",'
    b'"ref_name":"ground-truth common aligned population",'
    b'"est_name":"estimate common aligned population",'
    b'"label":"ape_translation_rmse"}'
)


class EquivalentEvaluatorError(RuntimeError):
    """Raised whenever the independent evaluator fails closed."""


def _fail(message: str) -> None:
    raise EquivalentEvaluatorError(message)


def validate_runtime_environment(environment: Mapping[str, str]) -> Path:
    expected_names = set(REQUIRED_FIXED_ENVIRONMENT) | set(PRIVATE_ENVIRONMENT_SUFFIXES)
    if set(environment) != expected_names:
        _fail("evaluator environment key inventory differs")
    for name, expected in REQUIRED_FIXED_ENVIRONMENT.items():
        if environment.get(name) != expected:
            _fail("evaluator fixed environment differs: " + name)
    private_root = None
    for name, suffix in PRIVATE_ENVIRONMENT_SUFFIXES.items():
        value = environment.get(name)
        if type(value) is not str or not value or "\0" in value:
            _fail("evaluator private path differs: " + name)
        path = Path(value)
        if not path.is_absolute() or os.path.normpath(value) != value or path.name != suffix:
            _fail("evaluator private path differs: " + name)
        if private_root is None:
            private_root = path.parent
        elif path.parent != private_root:
            _fail("evaluator private paths do not share one root")
    if private_root is None:
        _fail("evaluator private root is absent")
    return private_root


def _capsule_root() -> Path:
    module = Path(__file__).resolve(strict=True)
    for ancestor in module.parents:
        if (
            ancestor.name == "python3.11"
            and ancestor.parent.name == "lib"
            and ancestor.parent.parent.name == "python"
        ):
            return ancestor.parent.parent.parent
    _fail("evaluator module is outside capsule layout")


def _validate_import_roots(root: Path) -> None:
    root_text = str(root) + os.path.sep
    for item in sys.path:
        if not item:
            _fail("evaluator import path contains the current directory")
        resolved = str(Path(item).resolve(strict=False))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("evaluator import path escapes the capsule: " + resolved)
        if item.endswith((".zip", ".egg")) and Path(item).exists():
            _fail("evaluator import path contains an opaque archive")
    for name, module in sorted(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        resolved = str(Path(origin).resolve(strict=False))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("evaluator loaded module escapes the capsule: " + name)


def _validate_loader_maps(root: Path) -> None:
    root_text = str(root) + os.path.sep
    try:
        lines = Path("/proc/self/maps").read_text(encoding="ascii", errors="strict").splitlines()
    except (OSError, UnicodeError) as exc:
        raise EquivalentEvaluatorError("evaluator loader maps cannot be read") from exc
    mapped = []
    for line in lines:
        fields = line.split(None, 5)
        if len(fields) < 6 or not fields[5].startswith("/"):
            continue
        if fields[5].endswith(" (deleted)"):
            _fail("evaluator retains a deleted file mapping")
        resolved = str(Path(fields[5]).resolve(strict=True))
        if resolved != str(root) and not resolved.startswith(root_text):
            _fail("evaluator loader mapping escapes the capsule: " + resolved)
        mapped.append(resolved)
    if not mapped:
        _fail("evaluator loader map inventory is empty")


def _read_regular(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or (
                before.st_nlink != 1
                and (
                    before.st_nlink != 0
                    or fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
                    != REQUIRED_MEMFD_SEALS
                )
            )
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
            or before.st_size <= 0
            or before.st_size > MAX_INPUT_BYTES
        ):
            _fail("evaluator input is not one bounded regular file")
        payload = bytearray()
        while len(payload) < before.st_size:
            block = os.read(descriptor, min(1 << 20, before.st_size - len(payload)))
            if not block:
                _fail("evaluator input became short")
            payload.extend(block)
        if os.read(descriptor, 1):
            _fail("evaluator input grew during read")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            _fail("evaluator input changed during read")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _finite_token(token: bytes, label: str) -> float:
    try:
        text = token.decode("ascii", "strict")
        value = float(text)
    except (UnicodeDecodeError, ValueError, OverflowError) as exc:
        raise EquivalentEvaluatorError(label + " is not binary64 text") from exc
    if not math.isfinite(value):
        _fail(label + " is nonfinite")
    # Canonical producer text is lower-case shortest-roundtrip with no plus.
    if format(value, ".17g").lower().encode("ascii") != token:
        _fail(label + " is not canonical .17g binary64 text")
    return value


def _parse_tum(document: bytes, label: str) -> Tuple[Tuple[bytes, Tuple[float, float, float]], ...]:
    if not document.startswith(TUM_HEADER):
        _fail(label + " TUM header differs")
    lines = document[len(TUM_HEADER) :].splitlines(keepends=True)
    if not lines or len(lines) > MAX_POSE_COUNT:
        _fail(label + " TUM population is outside its bound")
    rows = []
    previous_timestamp = None
    for index, line in enumerate(lines):
        if not line.endswith(b"\n") or b"\r" in line:
            _fail(label + " TUM line termination differs")
        tokens = line[:-1].split(b" ")
        if len(tokens) != 8 or any(not token for token in tokens):
            _fail(label + " TUM row shape differs")
        timestamp = tokens[0]
        parts = timestamp.split(b".")
        if (
            len(parts) != 2
            or not parts[0]
            or not parts[0].isdigit()
            or len(parts[1]) != 9
            or not parts[1].isdigit()
            or (len(parts[0]) > 1 and parts[0].startswith(b"0"))
        ):
            _fail(label + " TUM timestamp is not canonical nanoseconds")
        timestamp_ns = int(parts[0]) * 1_000_000_000 + int(parts[1])
        if previous_timestamp is not None and timestamp_ns <= previous_timestamp:
            _fail(label + " TUM timestamps are not strictly increasing")
        previous_timestamp = timestamp_ns
        values = tuple(
            _finite_token(token, label + " TUM component") for token in tokens[1:]
        )
        rows.append((timestamp, (values[0], values[1], values[2])))
    return tuple(rows)


def _ordered_sum(values: Sequence[float]) -> float:
    total = 0.0
    for value in values:
        total = total + value
        if not math.isfinite(total):
            _fail("evaluator ordered sum became nonfinite")
    return total


def _statistics(errors: Sequence[float]) -> Mapping[str, float]:
    count = len(errors)
    if count <= 0 or count > (1 << 53):
        _fail("evaluator error count is outside exact-binary64 range")
    ordered = sorted(errors)
    if count & 1:
        median = ordered[count // 2]
    else:
        median = (ordered[count // 2 - 1] + ordered[count // 2]) / 2.0
    mean = _ordered_sum(errors) / float(count)
    squared = tuple(value * value for value in errors)
    sse = _ordered_sum(squared)
    rmse = math.sqrt(sse / float(count))
    variance = _ordered_sum(tuple((value - mean) * (value - mean) for value in errors)) / float(count)
    result = {
        "max": ordered[-1],
        "mean": mean,
        "median": median,
        "min": ordered[0],
        "rmse": rmse,
        "sse": sse,
        "std": math.sqrt(variance),
    }
    if any(not math.isfinite(value) or value < 0.0 for value in result.values()):
        _fail("evaluator statistic is nonfinite or negative")
    return result


def _npy_f64(values: Sequence[float]) -> bytes:
    shape_text = "({},)".format(len(values))
    mapping = "{'descr': '<f8', 'fortran_order': False, 'shape': " + shape_text + ", }"
    prefix_size = 10
    padding = (-((prefix_size + len(mapping) + 1) % 64)) % 64
    header = (mapping + (" " * padding) + "\n").encode("latin1", "strict")
    if len(header) > 0xFFFF:
        _fail("evaluator NPY header exceeds v1 bound")
    output = bytearray(b"\x93NUMPY\x01\x00")
    output.extend(struct.pack("<H", len(header)))
    output.extend(header)
    for value in values:
        output.extend(struct.pack("<d", value))
    return bytes(output)


def _result_archive(errors: Sequence[float], statistics: Mapping[str, float]) -> bytes:
    payloads = {
        "error_array.npy": _npy_f64(errors),
        "info.json": RESULT_INFO_JSON,
        "stats.json": json.dumps(
            statistics, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode("ascii"),
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in RESULT_MEMBERS:
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.create_version = 20
            info.extract_version = 20
            info.external_attr = (stat.S_IFREG | 0o444) << 16
            info.flag_bits = 0
            archive.writestr(info, payloads[name])
    return output.getvalue()


def evaluate_documents(reference: bytes, estimate: bytes) -> bytes:
    reference_rows = _parse_tum(reference, "reference")
    estimate_rows = _parse_tum(estimate, "estimate")
    if len(reference_rows) != len(estimate_rows):
        _fail("evaluator TUM populations differ")
    errors = []
    for reference_row, estimate_row in zip(reference_rows, estimate_rows):
        if reference_row[0] != estimate_row[0]:
            _fail("evaluator TUM timestamp populations differ")
        squared = 0.0
        for reference_value, estimate_value in zip(reference_row[1], estimate_row[1]):
            difference = estimate_value - reference_value
            squared = squared + difference * difference
        error = math.sqrt(squared)
        if not math.isfinite(error) or error < 0.0:
            _fail("evaluator translation error is nonfinite")
        errors.append(error)
    return _result_archive(tuple(errors), _statistics(tuple(errors)))


def _write_new(path: Path, payload: bytes) -> None:
    parent_status = os.stat(str(path.parent), follow_symlinks=False)
    if not stat.S_ISDIR(parent_status.st_mode) or stat.S_IMODE(parent_status.st_mode) != 0o700:
        _fail("evaluator output parent is not private")
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail("evaluator output write made no progress")
            offset += count
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _write_held(path: Path, payload: bytes) -> None:
    """Fill the sandbox-private tmpfs output later exported by its runner."""

    descriptor = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        try:
            seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            seals = None
        held_kind = (
            (before.st_nlink == 0 and seals == 0)
            or (
                before.st_nlink == 1
                and seals in (None, fcntl.F_SEAL_SEAL)
            )
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or not held_kind
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size != 0
            or (before.st_dev, before.st_ino) != (by_path.st_dev, by_path.st_ino)
        ):
            _fail("evaluator held output identity differs")
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail("evaluator held output write made no progress")
            offset += count
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        by_path = os.stat(str(path), follow_symlinks=False)
        try:
            after_seals = fcntl.fcntl(descriptor, fcntl.F_GET_SEALS)
        except OSError as exc:
            if exc.errno != errno.EINVAL:
                raise
            after_seals = None
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or (by_path.st_dev, by_path.st_ino) != (before.st_dev, before.st_ino)
            or after.st_nlink != before.st_nlink
            or stat.S_IMODE(after.st_mode) != 0o444
            or after.st_size != len(payload)
            or after_seals != seals
        ):
            _fail("evaluator held output differs after write")
    finally:
        os.close(descriptor)


def main(arguments: Sequence[str]) -> int:
    try:
        if not sys.flags.isolated or not sys.dont_write_bytecode:
            _fail("evaluator requires isolated no-bytecode Python")
        validate_runtime_environment(os.environ)
        root = _capsule_root()
        _validate_import_roots(root)
        if fp_control.CAPSULE_NATIVE != 1:
            _fail("evaluator lacks its native FP probe")
        fp_control.establish()
        fp_control.verify()
        _validate_loader_maps(root)
        if tuple(arguments) == ("--version",):
            sys.stdout.write(VERSION + "\n")
            fp_control.verify()
            return 0
        if (
            len(arguments) != 10
            or arguments[0] != "tum"
            or tuple(arguments[3:7]) != ("-r", "trans_part", "--t_max_diff", "0.01")
            or arguments[7] != "--save_results"
            or arguments[9] != "--no_warnings"
        ):
            _fail("evaluator command differs from the frozen API")
        paths = [Path(arguments[index]) for index in (1, 2, 8)]
        if any(
            not path.is_absolute() or os.path.normpath(str(path)) != str(path)
            for path in paths
        ):
            _fail("evaluator path argument is not normalized absolute")
        archive = evaluate_documents(_read_regular(paths[0]), _read_regular(paths[1]))
        if paths[2].exists():
            _write_held(paths[2], archive)
        else:
            _write_new(paths[2], archive)
        _validate_loader_maps(root)
        fp_control.verify()
        # Six decimals are presentation-only and are never parsed as evidence.
        stats_document = zipfile.ZipFile(io.BytesIO(archive)).read("stats.json")
        rmse = json.loads(stats_document.decode("ascii"))["rmse"]
        sys.stdout.write("rmse {:.6f}\n".format(rmse))
        return 0
    except BaseException as exc:
        sys.stderr.write(
            "CP2-D equivalent evaluator failed closed: {}: {}\n".format(
                type(exc).__name__, exc
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))


__all__ = [
    "EquivalentEvaluatorError",
    "evaluate_documents",
    "main",
    "validate_runtime_environment",
]
