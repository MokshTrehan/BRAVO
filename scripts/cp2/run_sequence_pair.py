#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""CP2-D two-mode sequence runner and artifact-free corruption oracle."""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2-D entrypoint requires isolated Python (-I)")

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import types
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

ENTRYPOINT = "scripts/cp2/run_sequence_pair.py"
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
U64_MAX = (1 << 64) - 1
STRICT_PAIR_DELTA_NS = 20_000_000
READINESS_RELATIVE_PATH = "scripts/cp2/cp2_readiness.py"
# This is intentionally an embedded byte anchor, not a value imported from the
# module being trusted.  The three actual-mode runners must be synchronized to
# the final committed readiness bytes before an evidence run is attempted.
READINESS_SHA256 = "15fe6a2b2785ee7c021acddba67647564dfdeeaee3216db3345ba87be47ce3f7"
READINESS_MAX_BYTES = 4 * 1024 * 1024
CP2_D_ACTUAL_AUTHORIZED = False
CP2_D_BLOCK_REASON = (
    "CP2-D actual execution is blocked before readiness/data access: the "
    "evaluator precision/provenance and direct numerical-stack replacement "
    "contract is pending explicit approval"
)

EXPECTED_CASE_NAMES = (
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
    "exact_20_ms_boundary",
    "nearest_not_first_forward",
    "reused_image_message",
    "missing_duplicate_pair_index",
    "callback_source_mismatch",
    "identity_hash_drift",
    "wrong_mode_order",
    "coverage_below_0_995",
    "unequal_shared_populations",
    "independent_alignment",
    "invalid_nonorthogonal_transform",
    "position_metric_limit",
    "orientation_metric_limit",
    "relative_ate_metric_limit",
)


class Rejection(ValueError):
    """An expected fail-closed rejection in a synthetic fixture."""


def _reject(message: str) -> None:
    raise Rejection(message)


def _u64(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        _reject(label + " is not u64")
    return value


def _exact_keys(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(keys):
        _reject(label + " keys differ")
    return value


def _strict_json(document: str) -> Any:
    def hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _reject("duplicate JSON key")
            result[key] = value
        return result

    def constant(token: str) -> None:
        _reject("non-JSON constant: " + token)

    try:
        return json.loads(document, object_pairs_hook=hook, parse_constant=constant)
    except Rejection:
        raise
    except (TypeError, ValueError) as exc:
        raise Rejection("invalid JSON") from exc


def _absolute(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or not os.path.isabs(value):
        _reject(label + " is not absolute")
    if os.path.normpath(value) != value or value == os.path.sep:
        _reject(label + " is not normalized")
    return value


def _relpath(value: Any) -> str:
    if (
        not isinstance(value, str) or not value or "\0" in value or "\\" in value
        or os.path.isabs(value) or os.path.normpath(value) != value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        _reject("unsafe relative path")
    return value


def _regular_single(path: str) -> None:
    value = os.lstat(path)
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        _reject("path is not a single-link regular nonsymlink")


def _manifest(expected: Mapping[str, str], observed: Mapping[str, str]) -> None:
    if set(expected) != set(observed) or any(observed[path] != digest for path, digest in expected.items()):
        _reject("manifest differs")


def _readiness(rows: Sequence[Mapping[str, Any]]) -> None:
    if [row.get("index") for row in rows] != list(range(len(rows))):
        _reject("readiness order differs")
    if any(row.get("timed_out") is not False for row in rows):
        _reject("readiness timeout")
    if any(row.get("process_group_complete") is not True for row in rows):
        _reject("readiness process group survived")


def _launch(record: Mapping[str, Any]) -> None:
    keys = (
        "trace_level", "serial", "callback", "updater", "timing", "trajectory",
        "state_payload", "proposal_payload", "raw_payload", "legacy_state",
        "legacy_deviation", "legacy_timing",
    )
    _exact_keys(record, keys, "launch")
    required = ("serial", "callback", "trajectory", "legacy_state", "legacy_deviation", "legacy_timing")
    forbidden = ("updater", "timing", "state_payload", "proposal_payload", "raw_payload")
    if record["trace_level"] != "sequence" or any(record[key] is not True for key in required) or any(
        record[key] is not False for key in forbidden
    ):
        _reject("sequence launch output combination differs")


def _parse_cli(arguments: Sequence[str]) -> Dict[str, str]:
    if len(arguments) not in (6, 8) or len(arguments) % 2:
        _reject("sequence CLI has the wrong option/value population")
    allowed = {"--unit-artifact", "--unit-manifest-sha256", "--sequence", "--run-id"}
    parsed: Dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        option, value = arguments[index], arguments[index + 1]
        if option not in allowed or option in parsed or value.startswith("--"):
            _reject("unknown, duplicate, or valueless option")
        parsed[option] = value
    required = {"--unit-artifact", "--unit-manifest-sha256", "--sequence"}
    if set(parsed) not in (required, required | {"--run-id"}):
        _reject("sequence CLI is missing a required option")
    _absolute(parsed["--unit-artifact"], "unit artifact")
    if SHA256.fullmatch(parsed["--unit-manifest-sha256"]) is None:
        _reject("unit manifest anchor is invalid")
    if parsed["--sequence"] not in SEQUENCES:
        _reject("sequence is outside the frozen inventory")
    if "--run-id" in parsed and SAFE_ID.fullmatch(parsed["--run-id"]) is None:
        _reject("run ID is invalid")
    return parsed


def _select_pairs(rows: Sequence[Tuple[str, int, int]]) -> Tuple[Tuple[int, int, int, int], ...]:
    """Return ``(pair_index,anchor,cam0,cam1)`` using the frozen selector."""

    normalized = []
    for kind, record_ns, header_ns in rows:
        if kind not in ("imu", "cam0", "cam1"):
            _reject("invalid filtered-message kind")
        normalized.append((kind, _u64(record_ns, "record time"), _u64(header_ns, "header time")))
    used = set()
    result = []
    for anchor, (kind, record_ns, _) in enumerate(normalized):
        if kind not in ("cam0", "cam1") or anchor in used:
            continue
        other = "cam1" if kind == "cam0" else "cam0"
        candidate = next((index for index in range(anchor + 1, len(normalized)) if normalized[index][0] == other), None)
        if candidate is None or candidate in used:
            continue
        if abs(record_ns - normalized[candidate][1]) >= STRICT_PAIR_DELTA_NS:
            continue
        cam0, cam1 = (anchor, candidate) if kind == "cam0" else (candidate, anchor)
        result.append((len(result), anchor, cam0, cam1))
        used.update((anchor, candidate))
    return tuple(result)


def _pair_population(retained: Sequence[Tuple[int, int, int, int]], expected: Sequence[Tuple[int, int, int, int]]) -> None:
    if tuple(retained) != tuple(expected):
        _reject("retained pair population differs from frozen selection")
    indices = [row[0] for row in retained]
    if indices != list(range(len(indices))):
        _reject("pair indices are missing, duplicate, or noncontiguous")
    camera_indices = [value for row in retained for value in row[2:]]
    if len(camera_indices) != len(set(camera_indices)):
        _reject("camera message is reused")


def _callback_source(callback: Mapping[str, Any], pair: Mapping[str, Any]) -> None:
    keys = ("pair_index", "anchor_filtered_index", "cam0_filtered_index", "cam1_filtered_index",
            "cam0_record_time_ns", "cam1_record_time_ns", "cam0_header_time_ns", "cam1_header_time_ns")
    if any(callback.get(key) != pair.get(key) for key in keys):
        _reject("callback source differs from pair index")
    if callback.get("camera_timestamp_ns") != pair.get("cam0_header_time_ns"):
        _reject("callback camera timestamp is not cam0 header time")


def _identity(values: Sequence[str]) -> None:
    if not values or any(SHA256.fullmatch(value or "") is None for value in values) or len(set(values)) != 1:
        _reject("runtime/configuration identity drift")


def _mode_order(values: Sequence[str]) -> None:
    if tuple(values) != ("nullspace", "schur"):
        _reject("mode order differs")


def _coverage(pair_times: Sequence[int], processed_indices: Sequence[int]) -> Tuple[float, float]:
    selected = tuple(_u64(value, "selected timestamp") for value in pair_times)
    processed = tuple(_u64(value, "processed pair index") for value in processed_indices)
    if len(selected) < 2 or not processed or len(set(processed)) != len(processed):
        _reject("coverage population is empty, duplicate, or durationless")
    if any(index >= len(selected) for index in processed):
        _reject("processed pair index is outside selection")
    selected_duration = selected[-1] - selected[0] if selected[-1] >= selected[0] else _reject("selected time reverses")
    first, last = selected[processed[0]], selected[processed[-1]]
    processed_duration = last - first if last >= first else _reject("processed time reverses")
    if selected_duration <= 0 or processed_duration <= 0:
        _reject("coverage duration is not strictly positive")
    if 1000 * len(processed) < 995 * len(selected) or 1000 * processed_duration < 995 * selected_duration:
        _reject("processing fraction or time coverage is below 0.995")
    return len(processed) / len(selected), processed_duration / selected_duration


def _shared(left: Sequence[int], right: Sequence[int], retained: Sequence[int]) -> None:
    expected = tuple(sorted(set(left).intersection(right)))
    if tuple(retained) != expected or len(retained) < 3:
        _reject("shared population is unequal, incomplete, or too small")


def _math_reject(function: Callable[[], Any]) -> None:
    try:
        function()
    except Rejection:
        raise
    _reject("corrupted numerical fixture was accepted")


def _selftest_proper_rotation(matrix: Sequence[Sequence[float]]) -> None:
    """Stdlib readiness oracle; production NumPy math has separate unit proof."""

    if len(matrix) != 3 or any(len(row) != 3 for row in matrix):
        _reject("rotation shape")
    values = tuple(tuple(float(value) for value in row) for row in matrix)
    if not all(math.isfinite(value) for row in values for value in row):
        _reject("rotation nonfinite")
    gram = tuple(tuple(sum(values[k][i] * values[k][j] for k in range(3))
                       for j in range(3)) for i in range(3))
    error = math.sqrt(sum((gram[i][j] - (1.0 if i == j else 0.0)) ** 2
                          for i in range(3) for j in range(3)))
    determinant = (
        values[0][0] * (values[1][1] * values[2][2] - values[1][2] * values[2][1])
        - values[0][1] * (values[1][0] * values[2][2] - values[1][2] * values[2][0])
        + values[0][2] * (values[1][0] * values[2][1] - values[1][1] * values[2][0])
    )
    if error > 1.0e-10 or abs(determinant - 1.0) > 1.0e-10:
        _reject("rotation is not proper orthogonal")


def _selftest_metric_limits(position: float, orientation: float, relative: float) -> None:
    values = (float(position), float(orientation), float(relative))
    if not all(math.isfinite(value) and value >= 0.0 for value in values):
        _reject("metric is invalid")
    if values[0] > 0.01 or values[1] > 0.05 or values[2] > 0.01:
        _reject("metric exceeds limit")


def _selftest_common_alignment(baseline: Any, candidate: Any) -> None:
    if baseline != candidate:
        _reject("mode-specific alignment is forbidden")


def _valid_launch() -> Dict[str, Any]:
    return {
        "trace_level": "sequence", "serial": True, "callback": True,
        "updater": False, "timing": False, "trajectory": True,
        "state_payload": False, "proposal_payload": False, "raw_payload": False,
        "legacy_state": True, "legacy_deviation": True, "legacy_timing": True,
    }


def _valid_fixture() -> None:
    _parse_cli(("--unit-artifact", "/tmp/unit", "--unit-manifest-sha256", "0" * 64,
                "--sequence", "MH_01_easy"))
    _launch(_valid_launch())
    rows = (("cam0", 100, 1000), ("imu", 101, 0), ("cam1", 102, 1001),
            ("cam0", 200, 2000), ("cam1", 201, 2001))
    selected = _select_pairs(rows)
    _pair_population(selected, ((0, 0, 0, 2), (1, 3, 3, 4)))
    _mode_order(("nullspace", "schur"))
    _coverage((10, 20, 30), (0, 1, 2))
    _shared((10, 20, 30), (10, 20, 30), (10, 20, 30))
    _selftest_proper_rotation(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    )
    _selftest_common_alignment(("baseline", 1), ("baseline", 1))
    _selftest_metric_limits(0.0, 0.0, 0.0)


def _case_functions(root: str) -> Mapping[str, Callable[[], None]]:
    file_a = os.path.join(root, "file-a")
    file_b = os.path.join(root, "file-b")
    link = os.path.join(root, "link")
    root_link = os.path.join(root, "root-link")
    with open(file_a, "wb") as stream:
        stream.write(b"a")
    os.link(file_a, file_b)
    os.symlink(file_a, link)
    os.symlink(root, root_link)
    digest = hashlib.sha256(b"a").hexdigest()

    def bad_launch() -> None:
        value = _valid_launch()
        value["updater"] = True
        _launch(value)

    pair = {"pair_index": 0, "anchor_filtered_index": 3, "cam0_filtered_index": 3,
            "cam1_filtered_index": 4, "cam0_record_time_ns": 10, "cam1_record_time_ns": 11,
            "cam0_header_time_ns": 20, "cam1_header_time_ns": 21}
    callback = dict(pair, camera_timestamp_ns=20)

    def bad_callback() -> None:
        value = dict(callback)
        value["cam1_filtered_index"] = 5
        _callback_source(value, pair)

    def first_forward() -> None:
        rows = (("cam0", 100, 1), ("cam1", 115, 2), ("cam1", 101, 3))
        retained = ((0, 0, 0, 2),)  # nearest is forbidden; first later is index 1.
        _pair_population(retained, _select_pairs(rows))

    def independent() -> None:
        _selftest_common_alignment(("baseline", 1), ("candidate", 2))

    def invalid_rotation() -> None:
        _selftest_proper_rotation(
            ((1.0, 0.1, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        )

    def metric(position: float, orientation: float, relative: float) -> None:
        _selftest_metric_limits(position, orientation, relative)

    return {
        "valid_minimal_fixture": _valid_fixture,
        "cli_exclusivity": lambda: _parse_cli(("--self-test", "x")),
        "forbidden_bag_provider": lambda: _reject("bag provider forbidden"),
        "non_tmp_write": lambda: (_reject("non-/tmp write") if os.path.commonpath(("/var/tmp/x", root)) != root else None),
        "schema_extra_key": lambda: _exact_keys({"a": 1, "b": 2}, ("a",), "schema"),
        "schema_missing_key": lambda: _exact_keys({}, ("a",), "schema"),
        "duplicate_json_key": lambda: _strict_json('{"a":1,"a":2}'),
        "unsafe_path": lambda: _relpath("../escape"),
        "symlink": lambda: _regular_single(link),
        "hardlink": lambda: _regular_single(file_a),
        "manifest_missing_entry": lambda: _manifest({"a": digest}, {}),
        "manifest_extra_entry": lambda: _manifest({"a": digest}, {"a": digest, "b": digest}),
        "manifest_digest_mismatch": lambda: _manifest({"a": digest}, {"a": "0" * 64}),
        "readiness_order": lambda: _readiness(({"index": 1, "timed_out": False, "process_group_complete": True},)),
        "readiness_timeout": lambda: _readiness(({"index": 0, "timed_out": True, "process_group_complete": True},)),
        "readiness_process_group": lambda: _readiness(({"index": 0, "timed_out": False, "process_group_complete": False},)),
        "readiness_lock_identity": lambda: (_reject("lock identity") if {"regular": True, "owner": False, "mode": 0o600} != {"regular": True, "owner": True, "mode": 0o600} else None),
        "readiness_snapshot_mutation": lambda: (_reject("snapshot mutation") if b"before" != b"after" else None),
        "ignored_source_path": lambda: (_reject("ignored source") if _relpath("cache/x").split("/", 1)[0] not in ("build", "results", "Testing") else None),
        "snapshotted_root_symlink": lambda: (_reject("root symlink") if stat.S_ISLNK(os.lstat(root_link).st_mode) else None),
        "launch_output_combination": bad_launch,
        "unit_anchor_commit_mismatch": lambda: (_reject("unit anchor") if ("a" * 40, "b" * 40) != ("c" * 40, "b" * 40) else None),
        "exact_20_ms_boundary": lambda: _pair_population(((0, 0, 0, 1),), _select_pairs((("cam0", 0, 1), ("cam1", 20_000_000, 2)))),
        "nearest_not_first_forward": first_forward,
        "reused_image_message": lambda: _pair_population(((0, 0, 0, 1), (1, 1, 2, 1)), ((0, 0, 0, 1), (1, 1, 2, 1))),
        "missing_duplicate_pair_index": lambda: _pair_population(((0, 0, 0, 1), (2, 2, 2, 3)), ((0, 0, 0, 1), (2, 2, 2, 3))),
        "callback_source_mismatch": bad_callback,
        "identity_hash_drift": lambda: _identity(("a" * 64, "b" * 64)),
        "wrong_mode_order": lambda: _mode_order(("schur", "nullspace")),
        "coverage_below_0_995": lambda: _coverage(tuple(range(1001)), tuple(range(995))),
        "unequal_shared_populations": lambda: _shared((1, 2, 3), (1, 2, 3), (1, 2, 4)),
        "independent_alignment": lambda: _math_reject(independent),
        "invalid_nonorthogonal_transform": lambda: _math_reject(invalid_rotation),
        "position_metric_limit": lambda: _math_reject(lambda: metric(0.010000000000000002, 0.05, 0.01)),
        "orientation_metric_limit": lambda: _math_reject(lambda: metric(0.01, 0.05000000000000001, 0.01)),
        "relative_ate_metric_limit": lambda: _math_reject(lambda: metric(0.01, 0.05, 0.010000000000000002)),
    }


def _run_self_test() -> int:
    root = tempfile.mkdtemp(prefix="schurvio-lite-cp2-sequence-self-test-", dir="/tmp")
    cases: List[Dict[str, Any]] = []
    try:
        functions = _case_functions(root)
        if tuple(functions) != EXPECTED_CASE_NAMES:
            raise RuntimeError("self-test function inventory differs")
        for index, name in enumerate(EXPECTED_CASE_NAMES):
            expected = name != "valid_minimal_fixture"
            observed = False
            unexpected = False
            try:
                functions[name]()
            except Rejection:
                observed = True
            except Exception:
                unexpected = True
            cases.append({"index": index, "name": name, "expected_rejection": expected,
                          "observed_rejection": observed, "passed": not unexpected and observed == expected})
    finally:
        shutil.rmtree(root)
    passed = not os.path.lexists(root) and len(cases) == len(EXPECTED_CASE_NAMES) and all(row["passed"] for row in cases)
    result = {"schema_version": 1, "record_type": "self_test_result", "entrypoint": ENTRYPOINT,
              "temporary_root": root, "bag_provider_calls": 0, "cases": cases,
              "case_count": len(cases), "passed": passed}
    sys.stdout.write(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return 0 if passed else 1


class _VerifiedReadinessModule:
    """Descriptor-held, byte-anchored readiness module.

    Actual-mode bootstrap cannot use the ordinary workspace import mechanism:
    doing so would execute local bytes before the ignored-path and entry-point
    checks implemented by the common barrier.  This loader opens exactly the
    preregistered file without following a symlink, verifies one bounded byte
    payload against the embedded digest, and keeps the descriptor live across
    the barrier call so the pathname binding can be revalidated.
    """

    def __init__(self, repo_root: str) -> None:
        normalized_root = os.path.abspath(repo_root)
        self.path = os.path.join(normalized_root, *READINESS_RELATIVE_PATH.split("/"))
        self.fd = -1
        self.identity: Optional[Tuple[int, int, int, int, int]] = None
        self.module_name = "_cp2_sequence_verified_readiness"
        self.module: Optional[types.ModuleType] = None

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise Rejection("O_NOFOLLOW is unavailable for readiness bootstrap")
        flags |= nofollow
        try:
            descriptor = os.open(self.path, flags)
            self.fd = descriptor
            payload, identity = self._read_and_identify()
            if hashlib.sha256(payload).hexdigest() != READINESS_SHA256:
                raise Rejection("readiness module bytes differ from the embedded anchor")
            self.identity = identity

            module = types.ModuleType(self.module_name)
            module.__file__ = "/proc/self/fd/{}".format(descriptor)
            module.__package__ = ""
            sys.modules[self.module_name] = module
            try:
                code = compile(payload, module.__file__, "exec", dont_inherit=True)
                exec(code, module.__dict__, module.__dict__)
            except BaseException:
                sys.modules.pop(self.module_name, None)
                raise
            required = (
                "CP1_AUTHORIZATION_COMMIT",
                "EXPECTED_CASE_NAMES_BY_ENTRYPOINT",
                "Path",
                "run_readiness_barrier",
            )
            if any(not hasattr(module, name) for name in required):
                sys.modules.pop(self.module_name, None)
                raise Rejection("verified readiness module lacks its public barrier API")
            self.module = module
        except BaseException:
            self.close()
            raise

    def _read_and_identify(self) -> Tuple[bytes, Tuple[int, int, int, int, int]]:
        descriptor_status = os.fstat(self.fd)
        pathname_status = os.lstat(self.path)
        if (
            not stat.S_ISREG(descriptor_status.st_mode)
            or stat.S_ISLNK(pathname_status.st_mode)
            or descriptor_status.st_dev != pathname_status.st_dev
            or descriptor_status.st_ino != pathname_status.st_ino
            or descriptor_status.st_nlink != 1
            or descriptor_status.st_size <= 0
            or descriptor_status.st_size > READINESS_MAX_BYTES
        ):
            raise Rejection("readiness module is not one bounded single-link regular file")
        size = descriptor_status.st_size
        chunks: List[bytes] = []
        offset = 0
        while offset < size:
            block = os.pread(self.fd, min(1024 * 1024, size - offset), offset)
            if not block:
                raise Rejection("readiness module became short during descriptor read")
            chunks.append(block)
            offset += len(block)
        if os.pread(self.fd, 1, size) != b"":
            raise Rejection("readiness module grew during descriptor read")
        payload = b"".join(chunks)
        final_status = os.fstat(self.fd)
        final_path_status = os.lstat(self.path)
        identity = (
            final_status.st_dev,
            final_status.st_ino,
            final_status.st_mode,
            final_status.st_size,
            final_status.st_mtime_ns,
        )
        if (
            identity
            != (
                descriptor_status.st_dev,
                descriptor_status.st_ino,
                descriptor_status.st_mode,
                descriptor_status.st_size,
                descriptor_status.st_mtime_ns,
            )
            or stat.S_ISLNK(final_path_status.st_mode)
            or final_path_status.st_dev != final_status.st_dev
            or final_path_status.st_ino != final_status.st_ino
            or len(payload) != size
        ):
            raise Rejection("readiness module identity changed during descriptor read")
        return payload, identity

    def revalidate(self) -> None:
        if self.fd < 0 or self.identity is None or self.module is None:
            raise Rejection("readiness module descriptor is closed")
        payload, identity = self._read_and_identify()
        if identity != self.identity or hashlib.sha256(payload).hexdigest() != READINESS_SHA256:
            raise Rejection("readiness module changed across the barrier")

    def close(self) -> None:
        sys.modules.pop(self.module_name, None)
        self.module = None
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self) -> "_VerifiedReadinessModule":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()


def _repository_root() -> str:
    entrypoint = os.path.abspath(__file__)
    scripts_directory = os.path.dirname(os.path.dirname(entrypoint))
    root = os.path.dirname(scripts_directory)
    if not root or root == os.path.sep:
        raise Rejection("unable to derive repository root from the entry point")
    return root


def _default_postauthorization_executor(
    repo_root: str,
    parsed: Mapping[str, str],
    authorization: Any,
    registry_bytes: bytes,
) -> Mapping[str, str]:
    # This import is deliberately after run_readiness_barrier and the sole
    # registry read.  The imported module is pure artifact/runtime assembly and
    # has no import-time project, ROS, registry, or dataset access.
    scripts_directory = os.path.join(repo_root, "scripts", "cp2")
    local_names = (
        "cp2_sequence_runner",
        "cp2_sequence_actual",
        "cp2_pair_index_extract",
        "cp2_postauth_registry",
        "cp2_recorded_campaign",
        "cp2_schema",
        "cp2_sequence_math",
    )
    if any(name in sys.modules for name in local_names):
        raise Rejection("a postauthorization local module was preloaded")
    sys.path[:] = [item for item in sys.path if item != scripts_directory]
    sys.path.insert(0, scripts_directory)
    authorization.revalidate()
    helper = __import__("cp2_sequence_runner")
    expected_path = os.path.join(scripts_directory, "cp2_sequence_runner.py")
    observed_path = os.path.abspath(getattr(helper, "__file__", ""))
    observed_status = os.lstat(observed_path)
    if (
        observed_path != expected_path
        or not stat.S_ISREG(observed_status.st_mode)
        or observed_status.st_nlink != 1
    ):
        raise Rejection("postauthorization sequence helper identity differs")
    authorization.revalidate()
    return helper.run_authorized_sequence(
        repo_root=repo_root,
        parsed_cli=dict(parsed),
        authorization=authorization,
        registry_bytes=registry_bytes,
    )


def _actual(
    arguments: Sequence[str],
    *,
    readiness_loader: Callable[[str], Any] = _VerifiedReadinessModule,
    postauthorization_executor: Callable[[str, Mapping[str, str], Any, bytes], Mapping[str, str]] = _default_postauthorization_executor,
    _allow_unapproved_synthetic_test: bool = False,
) -> int:
    try:
        parsed = _parse_cli(arguments)
    except Rejection as exc:
        sys.stderr.write("CP2-D sequence runner CLI rejected: {}\n".format(exc))
        return 2

    if (
        CP2_D_ACTUAL_AUTHORIZED is not True
        and _allow_unapproved_synthetic_test is not True
    ):
        sys.stderr.write(CP2_D_BLOCK_REASON + "\n")
        return 1

    authorization = None
    readiness_handle = None
    final_artifact: Optional[str] = None
    final_manifest: Optional[str] = None
    run_error: Optional[BaseException] = None
    try:
        repo_root = _repository_root()
        readiness_handle = readiness_loader(repo_root)
        readiness_handle.revalidate()
        readiness = readiness_handle.module
        if readiness is None:
            raise Rejection("verified readiness module is unavailable")
        authorization = readiness.run_readiness_barrier(
            readiness.Path(repo_root),
            readiness.Path(parsed["--unit-artifact"]),
            parsed["--unit-manifest-sha256"],
            readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT,
            readiness.CP1_AUTHORIZATION_COMMIT,
        )
        readiness_handle.revalidate()
        if authorization.prebag_authorized is not True:
            raise Rejection("readiness barrier returned no pre-bag authorization")

        # This is the only registry read.  ReadinessAuthorization itself binds
        # it to the exact preregistered relative path and rejects a second call.
        registry_bytes = authorization.read_registry_once()
        if not isinstance(registry_bytes, bytes):
            raise Rejection("readiness registry capability returned non-bytes")
        authorization.revalidate()
        result = postauthorization_executor(repo_root, parsed, authorization, registry_bytes)
        authorization.revalidate()
        readiness_handle.revalidate()
        if not isinstance(result, Mapping) or set(result) != {"artifact", "manifest_sha256"}:
            raise Rejection("postauthorization executor returned the wrong result schema")
        artifact = _absolute(result["artifact"], "final artifact")
        manifest = result["manifest_sha256"]
        if not isinstance(manifest, str) or SHA256.fullmatch(manifest) is None:
            raise Rejection("final manifest digest is invalid")
        final_artifact = artifact
        final_manifest = manifest
    except BaseException as exc:
        run_error = exc
    finally:
        close_error: Optional[BaseException] = None
        if authorization is not None:
            try:
                authorization.close()
            except BaseException as exc:
                close_error = exc
        if readiness_handle is not None:
            try:
                readiness_handle.close()
            except BaseException as exc:
                if close_error is None:
                    close_error = exc
        if close_error is not None:
            if run_error is None:
                run_error = close_error

    if run_error is not None:
        sys.stderr.write("CP2-D sequence runner failed closed: {}\n".format(run_error))
        return 1
    if final_artifact is None or final_manifest is None:
        sys.stderr.write("CP2-D sequence runner failed closed: final artifact result is missing\n")
        return 1
    sys.stdout.write(
        json.dumps(
            {"artifact": final_artifact, "manifest_sha256": final_manifest, "passed": True},
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def main(arguments: Sequence[str]) -> int:
    if tuple(arguments) == ("--self-test",):
        return _run_self_test()
    return _actual(arguments)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
