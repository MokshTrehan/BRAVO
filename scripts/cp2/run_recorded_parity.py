#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""CP2-C recorded-parity runner and artifact-free corruption oracle.

The exclusive ``--self-test`` path is deliberately standard-library-only and
cannot resolve the dataset registry or import a bag provider.  The approved
detached-readiness replacement is source-bound by the unit/readiness verifier.
Actual mode may load only that audited readiness machinery; recorded input
remains inaccessible unless all five committed entry points and the exact unit
anchor pass the barrier at the current clean commit.
"""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2-C entrypoint requires isolated Python (-I)")

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import types
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple


sys.dont_write_bytecode = True

ENTRYPOINT = "scripts/cp2/run_recorded_parity.py"
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
U64_MAX = (1 << 64) - 1
READINESS_SHA256 = "c0c09525dab693fd984430bc23b5dfe5888a701236ee8f676d348570612adeab"
READINESS_MAX_BYTES = 4 * 1024 * 1024
POSTAUTHORIZATION_MODULE_MAX_BYTES = 4 * 1024 * 1024
POSTAUTHORIZATION_MODULE_SOURCES = (
    ("cp2_schema", "scripts/cp2/cp2_schema.py"),
    ("cp2_recorded_campaign", "scripts/cp2/cp2_recorded_campaign.py"),
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
    "duplicate_noncontiguous_ids",
    "wrong_terminal_reconciliation",
    "wrong_counted_category",
    "denominator_mismatch",
    "raw_row_weight_mismatch",
    "missing_unclassified_disagreement",
    "bad_statistics_tolerance_edge",
    "missing_state_block",
    "missing_ordered_covariance_pair",
    "candidate_missing_row_omission",
    "nonzero_shadow_write",
    "baseline_commit_count_not_one",
    "live_preview_mismatch",
    "nonzero_repair_fallback",
    "prior_raw_config_hash_drift",
    "raw_prior_layout_disconnect",
    "flipped_gate_decision",
    "permuted_accepted_sequence",
    "candidate_proposal_copied_from_baseline",
    "proposal_disconnected_from_raw_or_phase0",
    "replay_noop",
    "replay_skipped_invocation",
    "phase2_phase3_disconnected",
    "replay_report_mismatch",
    "nonzero_internal_failure_terminal",
    "manifest_corruption",
)


class Rejection(ValueError):
    """An expected fail-closed rejection in a synthetic self-test."""


def _reject(message: str) -> None:
    raise Rejection(message)


class _ReadinessBinding:
    """Held exact bootstrap bytes used without a normal local import."""

    def __init__(self, descriptor: int, path: str, identity: os.stat_result,
                 payload: bytes) -> None:
        self.descriptor = descriptor
        self.path = path
        self.identity = identity
        self.payload = payload

    def revalidate(self) -> None:
        current = os.fstat(self.descriptor)
        by_path = os.stat(self.path, follow_symlinks=False)
        fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid",
                  "st_gid", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or
                any(getattr(current, name) != getattr(self.identity, name)
                    for name in fields) or
                any(getattr(current, name) != getattr(by_path, name)
                    for name in fields)):
            raise RuntimeError("held readiness bootstrap identity changed")
        os.lseek(self.descriptor, 0, os.SEEK_SET)
        chunks: List[bytes] = []
        remaining = READINESS_MAX_BYTES + 1
        while remaining:
            block = os.read(self.descriptor, min(remaining, 1024 * 1024))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        if b"".join(chunks) != self.payload:
            raise RuntimeError("held readiness bootstrap bytes changed")

    def close(self) -> None:
        if self.descriptor >= 0:
            descriptor = self.descriptor
            self.descriptor = -1
            os.close(descriptor)


def _read_held_module(descriptor: int, identity: os.stat_result,
                      label: str) -> bytes:
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_nlink != 1
        or identity.st_size <= 0
        or identity.st_size > POSTAUTHORIZATION_MODULE_MAX_BYTES
    ):
        _reject(label + " source identity is invalid")
    os.lseek(descriptor, 0, os.SEEK_SET)
    content = bytearray()
    while len(content) <= POSTAUTHORIZATION_MODULE_MAX_BYTES:
        block = os.read(
            descriptor,
            min(
                1024 * 1024,
                POSTAUTHORIZATION_MODULE_MAX_BYTES + 1 - len(content),
            ),
        )
        if not block:
            break
        content.extend(block)
    payload = bytes(content)
    if len(payload) != identity.st_size:
        _reject(label + " source size changed")
    return payload


class _HeldPostauthorizationModules:
    """Descriptor-load the two local campaign modules after authorization."""

    def __init__(self, repo_root: Path, authorization: Any) -> None:
        self.authorization = authorization
        self.repo_root = Path(repo_root).absolute()
        self.records: List[Dict[str, Any]] = []
        self.modules: Dict[str, Any] = {}
        local_names = tuple(name for name, _ in POSTAUTHORIZATION_MODULE_SOURCES)
        if any(name in sys.modules for name in local_names):
            _reject("a postauthorization CP2-C local module was preloaded")
        try:
            for name, relative in POSTAUTHORIZATION_MODULE_SOURCES:
                authorization.revalidate()
                descriptor = -1
                try:
                    descriptor = authorization.duplicate_source_fd(relative)
                    identity = os.fstat(descriptor)
                    payload = _read_held_module(descriptor, identity, name)
                    expected_path = str(self.repo_root / relative)
                    module = types.ModuleType(name)
                    module.__file__ = expected_path
                    module.__package__ = ""
                    self.records.append({
                        "name": name,
                        "relative": relative,
                        "descriptor": descriptor,
                        "identity": identity,
                        "payload": payload,
                        "expected_path": expected_path,
                        "module": module,
                    })
                    descriptor = -1
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                self.modules[name] = module
                sys.modules[name] = module
                exec(
                    compile(
                        payload,
                        expected_path,
                        "exec",
                        dont_inherit=True,
                    ),
                    module.__dict__,
                )
                authorization.revalidate()
            self.revalidate()
        except BaseException:
            self.close()
            raise

    @property
    def campaign(self) -> Any:
        module = self.modules.get("cp2_recorded_campaign")
        if module is None:
            _reject("postauthorization CP2-C campaign module is unavailable")
        return module

    def revalidate(self) -> None:
        self.authorization.revalidate()
        if len(self.records) != len(POSTAUTHORIZATION_MODULE_SOURCES):
            _reject("postauthorization CP2-C module population changed")
        for record, expected in zip(self.records, POSTAUTHORIZATION_MODULE_SOURCES):
            if (record["name"], record["relative"]) != expected:
                _reject("postauthorization CP2-C module order changed")
            module = record["module"]
            if (
                sys.modules.get(record["name"]) is not module
                or getattr(module, "__file__", None) != record["expected_path"]
            ):
                _reject("postauthorization CP2-C module binding changed")
            current = os.fstat(record["descriptor"])
            identity = record["identity"]
            if (
                current.st_dev != identity.st_dev
                or current.st_ino != identity.st_ino
                or current.st_mode != identity.st_mode
                or current.st_uid != identity.st_uid
                or current.st_gid != identity.st_gid
                or current.st_nlink != identity.st_nlink
                or current.st_size != identity.st_size
                or _read_held_module(
                    record["descriptor"], current, record["name"]
                )
                != record["payload"]
            ):
                _reject("postauthorization CP2-C module source changed")
        self.authorization.revalidate()

    def close(self) -> None:
        errors: List[BaseException] = []
        for record in reversed(self.records):
            if sys.modules.get(record["name"]) is record["module"]:
                sys.modules.pop(record["name"], None)
            descriptor = record["descriptor"]
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    errors.append(exc)
                record["descriptor"] = -1
        self.records.clear()
        self.modules.clear()
        if errors:
            raise RuntimeError(
                "postauthorization module cleanup failed: "
                + "; ".join(
                    type(error).__name__ + ": " + str(error)
                    for error in errors
                )
            )


def _load_readiness_by_descriptor() -> Tuple[Any, _ReadinessBinding]:
    """Load the audited stdlib bootstrap from one held, hash-bound file.

    This avoids Python's pathname import machinery before the readiness source
    and ignored-path checks.  The embedded digest is synchronized whenever the
    bootstrap bytes change, and the descriptor stays live through the whole
    authorized campaign.
    """

    script_directory = os.path.dirname(os.path.abspath(__file__))
    directory = os.open(script_directory, os.O_RDONLY | os.O_DIRECTORY |
                        os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
    descriptor = -1
    try:
        descriptor = os.open("cp2_readiness.py", os.O_RDONLY | os.O_CLOEXEC |
                             getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
        identity = os.fstat(descriptor)
        if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1 or
                identity.st_size <= 0 or identity.st_size > READINESS_MAX_BYTES):
            raise RuntimeError("readiness bootstrap file identity is invalid")
        payload = bytearray()
        while len(payload) <= READINESS_MAX_BYTES:
            block = os.read(descriptor, min(1024 * 1024,
                                            READINESS_MAX_BYTES + 1 - len(payload)))
            if not block:
                break
            payload.extend(block)
        raw = bytes(payload)
        if (len(raw) != identity.st_size or
                hashlib.sha256(raw).hexdigest() != READINESS_SHA256):
            raise RuntimeError("readiness bootstrap bytes differ from the embedded audit digest")
        path = os.path.join(script_directory, "cp2_readiness.py")
        binding = _ReadinessBinding(descriptor, path, identity, raw)
        module_name = "_cp2_recorded_held_readiness"
        module = types.ModuleType(module_name)
        module.__file__ = path
        module.__package__ = ""
        sys.modules[module_name] = module
        try:
            exec(compile(raw, path, "exec", dont_inherit=True), module.__dict__)
            binding.revalidate()
        except BaseException:
            sys.modules.pop(module_name, None)
            binding.close()
            raise
        descriptor = -1
        return module, binding
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory)


def _u64(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= U64_MAX:
        _reject(label + " is not u64")
    return value


def _checked_sum(values: Iterable[Any], label: str) -> int:
    result = 0
    for value in values:
        item = _u64(value, label)
        if result > U64_MAX - item:
            _reject(label + " overflows u64")
        result += item
    return result


def _checked_product(left: Any, right: Any, label: str) -> int:
    lhs = _u64(left, label + " left")
    rhs = _u64(right, label + " right")
    if lhs and rhs > U64_MAX // lhs:
        _reject(label + " overflows u64")
    return lhs * rhs


def _exact_keys(value: Any, expected: Iterable[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        _reject(label + " keys differ from the frozen schema")
    return value


def _strict_json(document: str) -> Any:
    def object_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _reject("duplicate JSON key")
            result[key] = value
        return result

    def constant(token: str) -> None:
        _reject("non-JSON constant: " + token)

    try:
        return json.loads(document, object_pairs_hook=object_hook, parse_constant=constant)
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


def _tmp_child(value: Any, root: str) -> str:
    path = _absolute(value, "temporary path")
    if os.path.commonpath((path, root)) != root or path == root:
        _reject("write target escapes the temporary root")
    return path


def _relpath(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or "\\" in value
        or os.path.isabs(value)
        or os.path.normpath(value) != value
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        _reject("unsafe relative path")
    return value


def _regular_single(path: str) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        _reject("file is not regular, nonsymlink, and single-link")


def _manifest(expected: Mapping[str, str], observed: Mapping[str, str]) -> None:
    if set(expected) != set(observed):
        _reject("manifest population differs")
    if any(observed[path] != digest for path, digest in expected.items()):
        _reject("manifest digest differs")


def _readiness(records: Sequence[Mapping[str, Any]]) -> None:
    if [row.get("index") for row in records] != list(range(len(records))):
        _reject("readiness order is not contiguous")
    if any(row.get("timed_out") is not False for row in records):
        _reject("readiness subprocess timed out")
    if any(row.get("process_group_complete") is not True for row in records):
        _reject("readiness process group survived")


def _lock(record: Mapping[str, Any]) -> None:
    _exact_keys(record, ("regular_nonsymlink", "owned_by_effective_user", "mode"), "lock")
    if record != {"regular_nonsymlink": True, "owned_by_effective_user": True, "mode": 0o600}:
        _reject("lock identity differs")


def _launch(record: Mapping[str, Any]) -> None:
    keys = (
        "trace_level", "serial", "callback", "updater", "timing", "trajectory",
        "state_payload", "proposal_payload", "raw_payload", "legacy_state",
        "legacy_deviation", "legacy_timing",
    )
    _exact_keys(record, keys, "launch outputs")
    required = ("serial", "updater", "state_payload", "proposal_payload", "raw_payload")
    forbidden = ("callback", "timing", "trajectory", "legacy_state", "legacy_deviation", "legacy_timing")
    if record["trace_level"] != "recorded_full":
        _reject("wrong trace level")
    if any(record[name] is not True for name in required) or any(record[name] is not False for name in forbidden):
        _reject("invalid recorded-full output combination")


def _ids(values: Sequence[Any]) -> None:
    if [_u64(value, "ID") for value in values] != list(range(len(values))):
        _reject("IDs are duplicate or noncontiguous")


def _terminal(attempted: Any, populations: Mapping[str, Any]) -> None:
    exact = (
        "empty_input", "all_rejected", "empty_after_compression",
        "preflight_rejected", "committed_counted", "internal_failure",
    )
    _exact_keys(populations, exact, "terminal populations")
    if _checked_sum((populations[name] for name in exact), "terminal population") != _u64(attempted, "attempted"):
        _reject("terminal reconciliation differs")


def _counted(status: str, counted: Any) -> None:
    if not isinstance(counted, bool) or counted != (status == "committed_counted"):
        _reject("counted category differs from terminal status")


def _agreement(agreements: Any, denominator: Any, retained_denominator: Any, raw_rows: Any = None) -> None:
    numerator = _u64(agreements, "agreement numerator")
    expected = _u64(denominator, "agreement denominator")
    retained = _u64(retained_denominator, "retained denominator")
    if expected == 0 or retained != expected or numerator > expected:
        _reject("agreement denominator differs")
    if raw_rows is not None and _checked_sum(raw_rows, "raw-row weight") != retained:
        _reject("raw-row denominator differs")
    # Exact >= 999/1000 gate. Overflow is a failure, never a float fallback.
    if _checked_product(numerator, 1000, "agreement gate") < _checked_product(expected, 999, "agreement gate"):
        _reject("agreement is below 99.9 percent")


def _disagreements(total: Any, classified: Any) -> None:
    if _u64(total, "disagreement total") != _checked_sum(classified, "classified disagreement"):
        _reject("disagreement is missing or unclassified")


def _tolerance(candidate: float, reference: float, norm: float, absolute: float, relative: float) -> None:
    import math
    values = (candidate, reference, norm, absolute, relative)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values):
        _reject("comparison input is nonfinite")
    if abs(float(candidate) - float(reference)) > float(absolute) + float(relative) * float(norm):
        _reject("statistics tolerance exceeded")


def _complete(expected: Sequence[Any], seen: Sequence[Any], label: str) -> None:
    if list(seen) != list(expected):
        _reject(label + " population is incomplete or reordered")


def _zero_counts(record: Mapping[str, Any]) -> None:
    if any(_u64(value, "write/repair count") != 0 for value in record.values()):
        _reject("candidate write, repair, or fallback count is nonzero")


def _hash_join(values: Sequence[str], label: str) -> None:
    if not values or any(SHA256.fullmatch(value or "") is None for value in values) or len(set(values)) != 1:
        _reject(label + " hash identity drift")


def _distinct_proposals(baseline: str, candidate: str) -> None:
    if SHA256.fullmatch(baseline or "") is None or SHA256.fullmatch(candidate or "") is None or baseline == candidate:
        _reject("candidate proposal is copied from the baseline")


def _proposal_link(proposal_raw: str, raw: str, proposal_prior: str, prior: str) -> None:
    if (proposal_raw, proposal_prior) != (raw, prior):
        _reject("proposal is disconnected from raw system or phase-0 prior")


def _replay(expected_ids: Sequence[int], seen_ids: Sequence[int], derived: bool) -> None:
    if not derived or list(seen_ids) != list(expected_ids):
        _reject("offline replay is skipped, no-op, or incomplete")


def _parse_actual_cli(arguments: Sequence[str]) -> Dict[str, str]:
    if len(arguments) not in (4, 6) or len(arguments) % 2:
        _reject("actual recorded CLI has the wrong option/value population")
    allowed = {"--unit-artifact", "--unit-manifest-sha256", "--run-id"}
    parsed: Dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        option, value = arguments[index], arguments[index + 1]
        if option not in allowed or option in parsed or value.startswith("--"):
            _reject("unknown, duplicate, or valueless actual option")
        parsed[option] = value
    required = {"--unit-artifact", "--unit-manifest-sha256"}
    if set(parsed) not in (required, required | {"--run-id"}):
        _reject("actual recorded CLI is missing a required option")
    _absolute(parsed["--unit-artifact"], "unit artifact")
    if SHA256.fullmatch(parsed["--unit-manifest-sha256"]) is None:
        _reject("unit manifest anchor is invalid")
    if "--run-id" in parsed and SAFE_ID.fullmatch(parsed["--run-id"]) is None:
        _reject("run ID is invalid")
    return parsed


def _valid_launch() -> Dict[str, Any]:
    return {
        "trace_level": "recorded_full", "serial": True, "callback": False,
        "updater": True, "timing": False, "trajectory": False,
        "state_payload": True, "proposal_payload": True, "raw_payload": True,
        "legacy_state": False, "legacy_deviation": False, "legacy_timing": False,
    }


def _valid_fixture() -> None:
    _parse_actual_cli(("--unit-artifact", "/tmp/unit", "--unit-manifest-sha256", "0" * 64))
    _launch(_valid_launch())
    _ids((0, 1, 2))
    _terminal(5, {"empty_input": 1, "all_rejected": 1, "empty_after_compression": 1,
                  "preflight_rejected": 1, "committed_counted": 1, "internal_failure": 0})
    _counted("committed_counted", True)
    _agreement(999, 1000, 1000)
    _agreement(1998, 2000, 2000, (2,) * 1000)
    _disagreements(2, (1, 1))
    _tolerance(1.00000001, 1.0, 1.0, 1e-10, 1e-8)
    _complete(("imu", "clone"), ("imu", "clone"), "state block")
    _complete((("imu", "imu"), ("imu", "clone")), (("imu", "imu"), ("imu", "clone")), "covariance pair")
    _zero_counts({"candidate_mean_writes": 0, "fallback": 0})
    _hash_join(("a" * 64, "a" * 64, "a" * 64), "raw/prior/config")
    _distinct_proposals("b" * 64, "c" * 64)
    _proposal_link("d" * 64, "d" * 64, "e" * 64, "e" * 64)
    _replay((0, 1), (0, 1), True)


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
        value["callback"] = True
        _launch(value)

    def bad_terminal() -> None:
        _terminal(2, {"empty_input": 0, "all_rejected": 0, "empty_after_compression": 0,
                      "preflight_rejected": 0, "committed_counted": 1, "internal_failure": 0})

    def copied() -> None:
        _distinct_proposals("a" * 64, "a" * 64)

    def bad_phase_join() -> None:
        _hash_join(("a" * 64, "b" * 64), "phase-2/phase-3")

    def bad_zero() -> None:
        _zero_counts({"repair": 0, "fallback": 1})

    return {
        "valid_minimal_fixture": _valid_fixture,
        "cli_exclusivity": lambda: _parse_actual_cli(("--self-test", "x")),
        "forbidden_bag_provider": lambda: _reject("bag provider forbidden"),
        "non_tmp_write": lambda: _tmp_child("/var/tmp/forbidden", root),
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
        "readiness_lock_identity": lambda: _lock({"regular_nonsymlink": True, "owned_by_effective_user": False, "mode": 0o600}),
        "readiness_snapshot_mutation": lambda: (_reject("snapshot mutation") if b"before" != b"after" else None),
        "ignored_source_path": lambda: (_reject("ignored source path") if _relpath("cache/x").split("/", 1)[0] not in ("build", "results", "Testing") else None),
        "snapshotted_root_symlink": lambda: (_reject("root symlink") if stat.S_ISLNK(os.lstat(root_link).st_mode) else None),
        "launch_output_combination": bad_launch,
        "unit_anchor_commit_mismatch": lambda: (_reject("unit anchor mismatch") if ("a" * 40, "b" * 40) != ("c" * 40, "b" * 40) else None),
        "duplicate_noncontiguous_ids": lambda: _ids((0, 2, 2)),
        "wrong_terminal_reconciliation": bad_terminal,
        "wrong_counted_category": lambda: _counted("all_rejected", True),
        "denominator_mismatch": lambda: _agreement(999, 1000, 999),
        "raw_row_weight_mismatch": lambda: _agreement(999, 1000, 1000, (1,) * 999),
        "missing_unclassified_disagreement": lambda: _disagreements(2, (1,)),
        "bad_statistics_tolerance_edge": lambda: _tolerance(1.0000000102, 1.0, 1.0, 1e-10, 1e-8),
        "missing_state_block": lambda: _complete(("imu", "clone"), ("imu",), "state block"),
        "missing_ordered_covariance_pair": lambda: _complete((("imu", "imu"), ("imu", "clone")), (("imu", "imu"),), "covariance pair"),
        "candidate_missing_row_omission": lambda: _complete((0, 1, 2), (0, 2), "candidate-missing row"),
        "nonzero_shadow_write": lambda: _zero_counts({"candidate_mean_writes": 1}),
        "baseline_commit_count_not_one": lambda: (_reject("baseline commit count") if _u64(2, "commit count") != 1 else None),
        "live_preview_mismatch": lambda: _hash_join(("a" * 64, "b" * 64), "live preview"),
        "nonzero_repair_fallback": bad_zero,
        "prior_raw_config_hash_drift": lambda: _hash_join(("a" * 64, "a" * 64, "b" * 64), "raw/prior/config"),
        "raw_prior_layout_disconnect": lambda: _hash_join(("a" * 64, "b" * 64), "raw/prior layout"),
        "flipped_gate_decision": lambda: _complete((True,), (False,), "gate decision"),
        "permuted_accepted_sequence": lambda: _complete((3, 7, 9), (7, 3, 9), "accepted sequence"),
        "candidate_proposal_copied_from_baseline": copied,
        "proposal_disconnected_from_raw_or_phase0": lambda: _proposal_link("a" * 64, "b" * 64, "c" * 64, "c" * 64),
        "replay_noop": lambda: _replay((0,), (0,), False),
        "replay_skipped_invocation": lambda: _replay((0, 1), (0,), True),
        "phase2_phase3_disconnected": bad_phase_join,
        "replay_report_mismatch": lambda: _hash_join(("a" * 64, "b" * 64), "replay report"),
        "nonzero_internal_failure_terminal": lambda: (_reject("internal failure terminal") if _u64(1, "internal failure") != 0 else None),
        "manifest_corruption": lambda: _manifest({"a": digest}, {"a": "f" * 64}),
    }


def _run_self_test() -> int:
    root = tempfile.mkdtemp(prefix="schurvio-lite-cp2-recorded-self-test-", dir="/tmp")
    cases: List[Dict[str, Any]] = []
    try:
        functions = _case_functions(root)
        if tuple(functions) != EXPECTED_CASE_NAMES:
            raise RuntimeError("self-test function inventory differs from expected inventory")
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
                          "observed_rejection": observed, "passed": not unexpected and expected == observed})
    finally:
        shutil.rmtree(root)
    passed = not os.path.lexists(root) and len(cases) == len(EXPECTED_CASE_NAMES) and all(row["passed"] for row in cases)
    result = {"schema_version": 1, "record_type": "self_test_result", "entrypoint": ENTRYPOINT,
              "temporary_root": root, "bag_provider_calls": 0, "cases": cases,
              "case_count": len(cases), "passed": passed}
    sys.stdout.write(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return 0 if passed else 1


def _actual_mode(arguments: Sequence[str]) -> int:
    try:
        parsed = _parse_actual_cli(arguments)
    except Rejection as exc:
        sys.stderr.write("CP2-C recorded runner CLI rejected: {}\n".format(exc))
        return 2

    # Descriptor-load only the approved, audited standard-library bootstrap
    # after exclusive CLI dispatch. Normal workspace-local import machinery
    # remains unused until readiness step 8 has passed.
    readiness_binding = None
    try:
        readiness, readiness_binding = _load_readiness_by_descriptor()
    except Exception as exc:
        sys.stderr.write("CP2-C readiness bootstrap load failed: {}\n".format(exc))
        return getattr(os, "EX_CONFIG", 78)

    repo_root = Path(__file__).resolve().parents[2]
    try:
        authorization = readiness.run_readiness_barrier(
            repo_root=repo_root,
            unit_artifact=Path(parsed["--unit-artifact"]),
            unit_manifest_sha256=parsed["--unit-manifest-sha256"],
            expected_case_names=readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT,
            cp1_authorization_commit=readiness.CP1_AUTHORIZATION_COMMIT,
        )
    except Exception as exc:
        # The readiness engine owns and removes all of its temporary state on
        # failure.  In particular, no result directory exists at this point.
        sys.stderr.write("CP2-C pre-data readiness failed: {}\n".format(exc))
        readiness_binding.close()
        return getattr(os, "EX_CONFIG", 78)

    modules = None
    campaign_result = 1
    publication_complete = False
    campaign_error = None
    cleanup_errors: List[BaseException] = []
    publication_state = None
    try:
        readiness_binding.revalidate()
        registry_bytes = authorization.read_registry_once()
        # Load the two workspace-local implementation modules from held,
        # authorization-gated descriptors.  Normal pathname import machinery
        # remains unavailable, and the campaign consumes the one verified
        # registry buffer rather than reopening it.
        modules = _HeldPostauthorizationModules(repo_root, authorization)
        modules.revalidate()

        def prepublication_guard() -> None:
            modules.revalidate()
            readiness_binding.revalidate()

        campaign_result = modules.campaign.execute_recorded_campaign(
            repo_root=repo_root,
            parsed_cli=parsed,
            registry_bytes=registry_bytes,
            authorization=authorization,
            prepublication_guard=prepublication_guard,
        )
        if campaign_result != 0:
            raise RuntimeError("campaign returned a nonzero success result")
        # execute_recorded_campaign returns only after its no-replace rename
        # and durability checks.  From here on, cleanup cannot revoke success.
        publication_complete = True
    except BaseException as exc:
        campaign_error = exc
    finally:
        publication_query_errors: List[BaseException] = []
        for query_index in range(2):
            try:
                publication_state = (
                    authorization.current_output_publication_state()
                )
                break
            except BaseException as exc:
                publication_query_errors.append(exc)
                # A genuine validation Exception is stable and must fail
                # closed.  One non-Exception BaseException may have landed
                # after the read-only query returned but before Python stored
                # its result, so perform one bounded reconciliation query.
                # This never reruns readiness, a campaign, or recorded input.
                if isinstance(exc, Exception) or query_index != 0:
                    break
        cleanup_errors.extend(publication_query_errors)
        if publication_state == "published":
            publication_complete = True
            campaign_result = 0
        elif publication_state is not None and publication_complete:
            publication_complete = False
            campaign_error = RuntimeError(
                "campaign returned success without an authoritative "
                "published output state: " + publication_state
            )
        elif publication_state is None:
            publication_complete = False
        if modules is not None:
            try:
                modules.close()
            except BaseException as exc:
                cleanup_errors.append(exc)
        try:
            authorization.close()
        except BaseException as exc:
            cleanup_errors.append(exc)
        try:
            readiness_binding.close()
        except BaseException as exc:
            cleanup_errors.append(exc)

    if publication_complete:
        diagnostics: List[BaseException] = []
        if campaign_error is not None:
            diagnostics.append(campaign_error)
        diagnostics.extend(cleanup_errors)
        if diagnostics:
            try:
                sys.stderr.write(
                    "CP2-C publication succeeded; post-publication diagnostic "
                    "reported: "
                    + "; ".join(
                        type(error).__name__ + ": " + str(error)
                        for error in diagnostics
                    )
                    + "\n"
                )
            except BaseException:
                pass
        return campaign_result

    summaries = []
    if campaign_error is not None:
        summaries.append(
            type(campaign_error).__name__ + ": " + str(campaign_error)
        )
    summaries.extend(
        "cleanup " + type(error).__name__ + ": " + str(error)
        for error in cleanup_errors
    )
    try:
        sys.stderr.write(
            "CP2-C authorized campaign failed closed: "
            + "; ".join(summaries)
            + "\n"
        )
    except BaseException:
        pass
    return 1


def main(arguments: Sequence[str]) -> int:
    if tuple(arguments) == ("--self-test",):
        return _run_self_test()
    return _actual_mode(arguments)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
