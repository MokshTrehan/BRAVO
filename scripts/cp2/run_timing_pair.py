#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed CP2-E timing-runner surface.

The candidate evidence layer is intentionally not a formal-run unlock.  The
source-frozen profile still lacks the complete helper-owned schema-v2 control,
topology, telemetry, and descendant-guardian contract.  Consequently, actual
mode deliberately stops after syntax-only CLI validation:
it does not import project modules, inspect a dataset registry, import a bag
provider, create an evidence directory, or alter host controls.

The exclusive ``--self-test`` mode is stdlib-only and exercises synthetic
fixtures beneath one fresh ``/tmp`` root.  It is safe to invoke from the CP2
pre-bag readiness barrier with bag access prohibited.
"""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2-E entrypoint requires isolated Python (-I)")

import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple


sys.dont_write_bytecode = True

ENTRYPOINT = "scripts/cp2/run_timing_pair.py"
PROFILE_RELPATH = "project/cp2_timing_profile.yaml"
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PAIR_ORDER = (("nullspace", "schur"), ("schur", "nullspace"), ("nullspace", "schur"))
FROZEN_CLOCK_KEYS = ("cpu_ids", "affinity", "driver", "governor", "min_frequency", "max_frequency", "boost")
U64_MAX = (1 << 64) - 1
# Protecting-fixture diagnostic only.  These rationals are not formal policy;
# actual mode remains locked until a source-frozen schema-v2 profile binds its
# own exact APERF/MPERF bounds and raw telemetry specification population.
COMMAND_PHASES = ("clock_pre", "runtime_preflight", "ros_run", "clock_post")
SYNTHETIC_APERF_MPERF_RATIO_BOUNDS = ((99, 100), (101, 100))
RUN_CAPTURE_NAMES = (
    "context.json", "serial.jsonl", "callbacks.jsonl", "updater.jsonl",
    "timing.jsonl", "runtime_parameters.json", "loader_before.txt",
    "loader_after.txt", "affinity_audit.json",
)
SYNTHETIC_ONLY_AUTHORIZATION = object()

# This literal tuple is the readiness barrier's deterministic expected-case
# inventory.  The common runner cases come first in contract order, followed
# by atomic CP2-E timing cases in contract order.
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
    "wrong_pair_order",
    "wrong_pair_index",
    "runtime_drift",
    "config_drift",
    "profile_drift",
    "changed_clock_snapshot",
    "affinity_mismatch",
    "warm_up_boundary_error",
    "warm_up_u64_overflow",
    "unilateral_noncommon_samples",
    "omitted_bilateral_common_sample",
    "duplicate_timestamp",
    "timestamp_u64_overflow",
    "negative_duration",
    "duration_u64_overflow",
    "noninteger_duration",
    "nonprimary_inclusion",
    "incorrect_linear_quantiles",
    "binary64_quantile_rounding",
    "median_ratio_limit",
    "p95_ratio_limit",
)


class SelfTestRejection(ValueError):
    """Expected rejection from one synthetic negative fixture."""


def _reject(message: str) -> None:
    raise SelfTestRejection(message)


def _exact_keys(value: Any, expected: Iterable[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != set(expected):
        _reject(label + " keys differ from the frozen schema")
    return value


def _strict_json_loads(document: str) -> Any:
    def object_without_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _reject("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(token: str) -> None:
        _reject("non-JSON numeric constant: " + token)

    try:
        return json.loads(document, object_pairs_hook=object_without_duplicates, parse_constant=reject_constant)
    except SelfTestRejection:
        raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SelfTestRejection("invalid JSON") from exc


def _normalized_absolute_path(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value or not os.path.isabs(value):
        _reject(label + " must be an absolute path")
    if os.path.normpath(value) != value or value == os.path.sep:
        _reject(label + " must be normalized and non-root")
    return value


def _tmp_child(path: Any, temporary_root: str) -> str:
    normalized = _normalized_absolute_path(path, "temporary path")
    root = _normalized_absolute_path(temporary_root, "temporary root")
    try:
        common = os.path.commonpath((normalized, root))
    except ValueError as exc:
        raise SelfTestRejection("temporary path has a different root") from exc
    if common != root or normalized == root:
        _reject("write target is not a child of the self-test root")
    return normalized


def _safe_relpath(value: Any) -> str:
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        _reject("unsafe relative path")
    if os.path.isabs(value) or os.path.normpath(value) != value:
        _reject("unsafe relative path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        _reject("unsafe relative path")
    return value


def _regular_nonsymlink_single_link(path: str) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        _reject("path is not a single-link regular nonsymlink file")


def _manifest_matches(expected: Mapping[str, str], observed: Mapping[str, str]) -> None:
    if set(expected) != set(observed):
        _reject("manifest path population mismatch")
    for path, digest in expected.items():
        if observed[path] != digest:
            _reject("manifest digest mismatch")


def _readiness_records_valid(records: Sequence[Mapping[str, Any]]) -> None:
    if [record.get("index") for record in records] != list(range(len(records))):
        _reject("readiness records are reordered or noncontiguous")
    for record in records:
        if record.get("timed_out") is not False:
            _reject("readiness child timed out")
        if record.get("process_group_complete") is not True:
            _reject("readiness child process group was not completely reaped")


def _readiness_lock_valid(record: Mapping[str, Any]) -> None:
    _exact_keys(record, ("regular_nonsymlink", "owned_by_effective_user", "mode"), "readiness lock")
    if record["regular_nonsymlink"] is not True or record["owned_by_effective_user"] is not True or record["mode"] != 0o600:
        _reject("readiness lock identity mismatch")


def _snapshots_equal(before: bytes, after: bytes) -> None:
    if not isinstance(before, bytes) or not isinstance(after, bytes) or before != after:
        _reject("readiness snapshot changed")


def _ignored_path_allowed(path: str) -> None:
    normalized = _safe_relpath(path)
    if normalized.split("/", 1)[0] not in ("build", "results", "Testing"):
        _reject("ignored path is outside a snapshotted root")


def _real_directory_nonsymlink(path: str) -> None:
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode):
        _reject("snapshotted root is not a real nonsymlink directory")


def _launch_outputs_valid(record: Mapping[str, Any]) -> None:
    expected = (
        "trace_level",
        "serial",
        "callback",
        "updater",
        "timing",
        "trajectory",
        "state_payload",
        "proposal_payload",
        "raw_payload",
        "legacy_state",
        "legacy_deviation",
        "legacy_timing",
    )
    _exact_keys(record, expected, "launch outputs")
    if record["trace_level"] != "timing":
        _reject("timing runner received a different trace level")
    required = ("serial", "callback", "updater", "timing")
    forbidden = ("trajectory", "state_payload", "proposal_payload", "raw_payload", "legacy_state", "legacy_deviation", "legacy_timing")
    if any(record[name] is not True for name in required) or any(record[name] is not False for name in forbidden):
        _reject("timing launch output combination is invalid")


def _unit_anchor_matches(expected_commit: str, expected_tree: str, observed_commit: str, observed_tree: str) -> None:
    if (expected_commit, expected_tree) != (observed_commit, observed_tree):
        _reject("unit anchor does not test the current commit and tree")


def _pair_order_valid(order: Any) -> None:
    try:
        normalized = tuple(tuple(pair) for pair in order)
    except TypeError as exc:
        raise SelfTestRejection("pair order is not nested") from exc
    if normalized != PAIR_ORDER:
        _reject("timing pair order mismatch")


def _pair_indices_valid(indices: Sequence[Any]) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in indices):
        _reject("timing pair index is not an integer")
    if list(indices) != list(range(len(PAIR_ORDER))):
        _reject("timing pair indices are noncontiguous")


def _identity_equal(expected: str, observed: str, label: str) -> None:
    if not isinstance(expected, str) or not isinstance(observed, str) or expected != observed:
        _reject(label + " identity drift")


def _clock_controls_equal(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    _exact_keys(before, FROZEN_CLOCK_KEYS, "clock snapshot before")
    _exact_keys(after, FROZEN_CLOCK_KEYS, "clock snapshot after")
    if any(before[key] != after[key] for key in FROZEN_CLOCK_KEYS):
        _reject("frozen clock control changed")


def _affinity_equal(expected: Sequence[Any], observed: Sequence[Any]) -> None:
    if not expected or list(expected) != list(observed):
        _reject("affinity differs from the frozen profile")
    if any(isinstance(cpu, bool) or not isinstance(cpu, int) or cpu < 0 for cpu in expected):
        _reject("affinity contains an invalid CPU ID")


def _warmup_eligible(record_time_ns: Any, boundary_ns: Any) -> None:
    for value in (record_time_ns, boundary_ns):
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > U64_MAX
        ):
            _reject("warm-up time is not u64")
    if record_time_ns < boundary_ns:
        _reject("sample precedes the inclusive warm-up boundary")


def _common_population_valid(common: Sequence[int], left: Sequence[int], right: Sequence[int]) -> None:
    _unique_timestamps(common)
    _unique_timestamps(left)
    _unique_timestamps(right)
    if list(common) != sorted(common):
        _reject("common timing timestamps are duplicate or unordered")
    expected = sorted(set(left).intersection(right))
    if list(common) != expected:
        _reject("common population is not the complete exact intersection")


def _unique_timestamps(values: Sequence[Any]) -> None:
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 0
        or value > U64_MAX
        for value in values
    ):
        _reject("timestamp is not u64")
    if len(values) != len(set(values)):
        _reject("duplicate timing timestamp")


def _duration_valid(value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        _reject("duration is not an integer")
    if value < 0 or value > U64_MAX:
        _reject("duration is outside u64")


def _primary_inclusion_valid(sample: Mapping[str, Any]) -> None:
    _exact_keys(sample, ("primary", "nonempty", "preflight_accepted", "committed"), "timing inclusion")
    if any(sample[key] is not True for key in ("primary", "nonempty", "preflight_accepted", "committed")):
        _reject("included timing sample is not primary")


def _canonical_rational(value: Any, label: str, positive: bool = False) -> Tuple[int, int]:
    if type(value) is not tuple or len(value) != 2:
        _reject(label + " must be a canonical rational pair")
    numerator, denominator = value
    if (
        type(numerator) is not int
        or type(denominator) is not int
        or numerator < 0
        or denominator <= 0
        or math.gcd(numerator, denominator) != 1
        or (positive and numerator == 0)
    ):
        _reject(label + " is not a reduced nonnegative rational")
    return numerator, denominator


def _reduced_rational(numerator: int, denominator: int) -> Tuple[int, int]:
    divisor = math.gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _linear_quantile(
    values: Sequence[int], quantile: Tuple[int, int]
) -> Tuple[int, int]:
    if not values:
        _reject("quantile population is empty")
    for value in values:
        _duration_valid(value)
    q_num, q_den = _canonical_rational(quantile, "linear quantile", positive=True)
    if (q_num, q_den) not in ((1, 2), (19, 20)):
        _reject("linear quantile is not frozen p50 or p95")
    ordered = sorted(values)
    interval_count = len(ordered) - 1
    if interval_count > U64_MAX // q_num:
        _reject("linear rank overflows u64")
    scaled_rank = interval_count * q_num
    low, remainder = divmod(scaled_rank, q_den)
    high = low if remainder == 0 else low + 1
    if high >= len(ordered):
        _reject("linear rank is outside the population")
    numerator = (q_den - remainder) * ordered[low] + remainder * ordered[high]
    return _reduced_rational(numerator, q_den)


def _quantile_matches(
    values: Sequence[int], quantile: Tuple[int, int], retained: Any
) -> None:
    retained_rational = _canonical_rational(retained, "retained quantile")
    if retained_rational != _linear_quantile(values, quantile):
        _reject("retained linear quantile is incorrect")


def _ratio_passes(
    baseline: Any, candidate: Any, limit: Tuple[int, int]
) -> None:
    baseline_num, baseline_den = _canonical_rational(
        baseline, "baseline timing quantile", positive=True
    )
    candidate_num, candidate_den = _canonical_rational(
        candidate, "candidate timing quantile"
    )
    limit_num, limit_den = _canonical_rational(
        limit, "timing ratio limit", positive=True
    )
    if (limit_num, limit_den) not in ((11, 10), (23, 20)):
        _reject("timing ratio limit is not frozen")
    left = candidate_num * baseline_den * limit_den
    right = limit_num * candidate_den * baseline_num
    if left > right:
        _reject("timing ratio exceeds its frozen limit")


def _valid_clock_snapshot() -> Dict[str, Any]:
    return {
        "cpu_ids": [0, 1, 2, 3],
        "affinity": [0, 1, 2, 3],
        "driver": "synthetic-fixed",
        "governor": "performance",
        "min_frequency": 3000000,
        "max_frequency": 3000000,
        "boost": False,
    }


def _valid_launch_outputs() -> Dict[str, Any]:
    return {
        "trace_level": "timing",
        "serial": True,
        "callback": True,
        "updater": True,
        "timing": True,
        "trajectory": False,
        "state_payload": False,
        "proposal_payload": False,
        "raw_payload": False,
        "legacy_state": False,
        "legacy_deviation": False,
        "legacy_timing": False,
    }


def _validate_minimal_fixture() -> None:
    _parse_actual_cli((
        "--unit-artifact",
        "/tmp/unit-artifact",
        "--unit-manifest-sha256",
        "0" * 64,
        "--run-id",
        "synthetic-timing",
    ))
    _launch_outputs_valid(_valid_launch_outputs())
    _readiness_records_valid((
        {"index": 0, "timed_out": False, "process_group_complete": True},
        {"index": 1, "timed_out": False, "process_group_complete": True},
    ))
    _readiness_lock_valid({"regular_nonsymlink": True, "owned_by_effective_user": True, "mode": 0o600})
    _pair_order_valid(PAIR_ORDER)
    _pair_indices_valid((0, 1, 2))
    _identity_equal("runtime", "runtime", "runtime")
    _identity_equal("config", "config", "configuration")
    _identity_equal("profile", "profile", "profile")
    snapshot = _valid_clock_snapshot()
    _clock_controls_equal(snapshot, dict(snapshot))
    _affinity_equal((0, 1, 2, 3), (0, 1, 2, 3))
    _warmup_eligible(60_000_000_000, 60_000_000_000)
    _common_population_valid((10, 20), (10, 20, 30), (10, 20, 40))
    _unique_timestamps((10, 20))
    _duration_valid(0)
    _primary_inclusion_valid({"primary": True, "nonempty": True, "preflight_accepted": True, "committed": True})
    _quantile_matches((10, 20, 30), (1, 2), (20, 1))
    _quantile_matches((10, 20, 30), (19, 20), (29, 1))
    _ratio_passes((10, 1), (11, 1), (11, 10))
    _ratio_passes((20, 1), (23, 1), (23, 20))


def _parse_actual_cli(arguments: Sequence[str]) -> Dict[str, str]:
    if len(arguments) not in (4, 6) or len(arguments) % 2 != 0:
        _reject("actual timing CLI does not have the exact option/value population")
    allowed = {"--unit-artifact", "--unit-manifest-sha256", "--run-id"}
    parsed: Dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        option = arguments[index]
        value = arguments[index + 1]
        if option not in allowed or option in parsed or not isinstance(value, str) or value.startswith("--"):
            _reject("actual timing CLI contains an unknown, duplicate, or valueless option")
        parsed[option] = value
    if set(parsed) not in (
        {"--unit-artifact", "--unit-manifest-sha256"},
        {"--unit-artifact", "--unit-manifest-sha256", "--run-id"},
    ):
        _reject("actual timing CLI is missing a required option")
    _normalized_absolute_path(parsed["--unit-artifact"], "unit artifact")
    if SHA256_PATTERN.fullmatch(parsed["--unit-manifest-sha256"]) is None:
        _reject("unit manifest SHA-256 is invalid")
    if "--run-id" in parsed and SAFE_ID_PATTERN.fullmatch(parsed["--run-id"]) is None:
        _reject("run ID is invalid")
    return parsed


class TimingOrchestrationError(RuntimeError):
    """The synthetic CP2-E campaign candidate failed closed."""


class TimingOrchestrationIndeterminate(TimingOrchestrationError):
    """Cleanup, restoration, or publication state cannot be proved exact."""


def _orchestration_fail(message: str) -> None:
    raise TimingOrchestrationError(message)


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def _sha256_bytes(payload: bytes) -> str:
    if type(payload) is not bytes:
        _orchestration_fail("digest input is not bytes")
    return hashlib.sha256(payload).hexdigest()


def _exact_orchestration_keys(
    value: Any, expected: Iterable[str], label: str,
) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(expected):
        _orchestration_fail(label + " keys differ from the frozen schema")
    return value


def _bounded_text(value: Any, label: str, maximum: int = 4096) -> str:
    if (
        type(value) is not str
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\0\r\n")
    ):
        _orchestration_fail(label + " is not bounded nonempty text")
    return value


def _lower_sha256(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 64)
    if SHA256_PATTERN.fullmatch(text) is None:
        _orchestration_fail(label + " is not lowercase SHA-256")
    return text


def _lower_git_oid(value: Any, label: str) -> str:
    text = _bounded_text(value, label, 40)
    if re.fullmatch(r"[0-9a-f]{40}", text) is None:
        _orchestration_fail(label + " is not a lowercase Git object ID")
    return text


def _orchestration_absolute(value: Any, label: str) -> str:
    text = _bounded_text(value, label)
    if not os.path.isabs(text) or os.path.normpath(text) != text or text == os.path.sep:
        _orchestration_fail(label + " is not normalized absolute non-root")
    return text


EXECUTION_BINDING_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256",
    "source_commit", "source_tree", "unit_manifest_sha256",
    "sequence_set_manifest_sha256", "executable_path", "executable_sha256",
    "dso_closure_sha256", "configuration_sha256", "boot_id_sha256",
    "machine_identity_sha256", "cwd", "environment", "phase_templates",
)
TEMPLATE_TOKENS = frozenset((
    "{profile_sha256}", "{run_index}", "{timing_pair_index}", "{mode}",
    "{run_id}", "{trace_directory}", "{sequence_index}", "{sequence_id}",
))


@dataclass(frozen=True)
class TimingExecutionBinding:
    """Canonical source/profile-bound executable and argv templates."""

    canonical_bytes: bytes
    profile_sha256: str
    source_commit: str
    source_tree: str
    unit_manifest_sha256: str
    sequence_set_manifest_sha256: str
    executable_path: str
    executable_sha256: str
    dso_closure_sha256: str
    configuration_sha256: str
    boot_id_sha256: str
    machine_identity_sha256: str
    cwd: str
    environment: Tuple[Tuple[str, str], ...]
    phase_templates: Tuple[Tuple[str, Tuple[str, ...]], ...]

    @property
    def launch_sha256(self) -> str:
        return _sha256_bytes(self.canonical_bytes)

    def template(self, phase: str) -> Tuple[str, ...]:
        retained = dict(self.phase_templates)
        if phase not in retained:
            _orchestration_fail("phase is outside the exact four-phase schedule")
        return retained[phase]

    def render(
        self,
        phase: str,
        *,
        run_index: int,
        mode: str,
        run_id: str,
        trace_directory: str,
        sequence_index: int,
        sequence_id: str,
    ) -> Tuple[str, ...]:
        values = {
            "{profile_sha256}": self.profile_sha256,
            "{run_index}": str(run_index),
            "{timing_pair_index}": str(run_index // 2),
            "{mode}": mode,
            "{run_id}": run_id,
            "{trace_directory}": trace_directory,
            "{sequence_index}": str(sequence_index),
            "{sequence_id}": sequence_id,
        }
        return tuple(values.get(item, item) for item in self.template(phase))


def load_execution_binding(value: Any) -> TimingExecutionBinding:
    """Strictly freeze one exact launch binding from a canonical JSON record."""

    row = _exact_orchestration_keys(value, EXECUTION_BINDING_KEYS, "execution binding")
    if (
        row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_execution_binding"
        or row["checkpoint"] != "CP2-E"
    ):
        _orchestration_fail("execution-binding identity differs")
    profile_sha = _lower_sha256(row["profile_sha256"], "binding profile SHA-256")
    source_commit = _lower_git_oid(row["source_commit"], "binding source commit")
    source_tree = _lower_git_oid(row["source_tree"], "binding source tree")
    unit_manifest = _lower_sha256(
        row["unit_manifest_sha256"], "binding unit-manifest SHA-256"
    )
    sequence_manifest = _lower_sha256(
        row["sequence_set_manifest_sha256"],
        "binding sequence-set manifest SHA-256",
    )
    executable = _orchestration_absolute(row["executable_path"], "binding executable")
    if executable in ("/bin/sh", "/bin/bash", "/usr/bin/env"):
        _orchestration_fail("binding executable is a shell or ambient launcher")
    executable_sha = _lower_sha256(
        row["executable_sha256"], "binding executable SHA-256"
    )
    dso_sha = _lower_sha256(
        row["dso_closure_sha256"], "binding DSO-closure SHA-256"
    )
    configuration_sha = _lower_sha256(
        row["configuration_sha256"], "binding configuration SHA-256"
    )
    boot_sha = _lower_sha256(row["boot_id_sha256"], "binding boot-id SHA-256")
    machine_sha = _lower_sha256(
        row["machine_identity_sha256"], "binding machine-identity SHA-256"
    )
    cwd = _orchestration_absolute(row["cwd"], "binding working directory")
    environment_value = row["environment"]
    if type(environment_value) is not dict or not environment_value:
        _orchestration_fail("binding environment is not one nonempty exact mapping")
    environment: List[Tuple[str, str]] = []
    for name in sorted(environment_value):
        if type(name) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name) is None:
            _orchestration_fail("binding environment contains an invalid name")
        environment.append((name, _bounded_text(environment_value[name], "environment value")))
    if list(environment_value) != [name for name, _item in environment]:
        _orchestration_fail("binding environment names are not sorted")

    templates_value = row["phase_templates"]
    _exact_orchestration_keys(templates_value, COMMAND_PHASES, "phase templates")
    templates: List[Tuple[str, Tuple[str, ...]]] = []
    common_required = frozenset(("{profile_sha256}", "{run_index}"))
    for phase in COMMAND_PHASES:
        raw = templates_value[phase]
        if type(raw) is not list or not raw:
            _orchestration_fail("phase template is not a nonempty argv list")
        argv = tuple(_bounded_text(item, "phase-template argument") for item in raw)
        _orchestration_absolute(argv[0], "phase-template executable")
        if argv[0] in ("/bin/sh", "/bin/bash", "/usr/bin/env"):
            _orchestration_fail("phase template invokes a forbidden launcher")
        brace_arguments = frozenset(item for item in argv if "{" in item or "}" in item)
        if not brace_arguments.issubset(TEMPLATE_TOKENS):
            _orchestration_fail("phase template contains an unknown or embedded token")
        required = common_required
        if phase == "ros_run":
            required = required.union((
                "{timing_pair_index}", "{mode}", "{run_id}",
                "{trace_directory}", "{sequence_index}", "{sequence_id}",
            ))
            if argv[0] != executable:
                _orchestration_fail("ROS phase executable differs from the source binding")
        if not required.issubset(brace_arguments):
            _orchestration_fail("phase template omits a required exact token")
        templates.append((phase, argv))
    canonical = _canonical_json_bytes(dict(row))
    return TimingExecutionBinding(
        canonical, profile_sha, source_commit, source_tree, unit_manifest,
        sequence_manifest, executable, executable_sha, dso_sha,
        configuration_sha, boot_sha, machine_sha, cwd, tuple(environment),
        tuple(templates),
    )


@dataclass(frozen=True)
class ProcessIdentityObservation:
    observed_monotonic_ns: int
    pid: int
    start_time_ticks: int
    executable_sha256: str
    loader_before_sha256: str
    loader_after_sha256: str

    def validate(self) -> None:
        if type(self.observed_monotonic_ns) is not int or self.observed_monotonic_ns <= 0:
            _orchestration_fail("process-identity observation time is invalid")
        if type(self.pid) is not int or self.pid <= 0:
            _orchestration_fail("observed process PID is invalid")
        if type(self.start_time_ticks) is not int or self.start_time_ticks <= 0:
            _orchestration_fail("observed process start time is invalid")
        _lower_sha256(self.executable_sha256, "observed executable SHA-256")
        _lower_sha256(self.loader_before_sha256, "observed loader-before SHA-256")
        _lower_sha256(self.loader_after_sha256, "observed loader-after SHA-256")

    def record(
        self, profile_sha256: str, execution_binding_sha256: str,
        run_index: int,
    ) -> Mapping[str, Any]:
        self.validate()
        return {
            "schema_version": 1,
            "record_type": "cp2_timing_process_identity_observation",
            "checkpoint": "CP2-E",
            "profile_sha256": _lower_sha256(
                profile_sha256, "process-identity profile SHA-256",
            ),
            "execution_binding_sha256": _lower_sha256(
                execution_binding_sha256,
                "process-identity execution-binding SHA-256",
            ),
            "run_index": run_index,
            "observed_monotonic_ns": self.observed_monotonic_ns,
            "pid": self.pid,
            "start_time_ticks": self.start_time_ticks,
            "executable_sha256": self.executable_sha256,
            "loader_before_sha256": self.loader_before_sha256,
            "loader_after_sha256": self.loader_after_sha256,
        }


@dataclass(frozen=True)
class TimingPhaseRequest:
    phase: str
    run_index: int
    timing_pair_index: int
    mode: str
    run_id: str
    trace_directory: str
    profile_sha256: str
    control_applied_sha256: str
    argv: Tuple[str, ...]
    environment: Tuple[Tuple[str, str], ...]
    cwd: str


@dataclass(frozen=True)
class TimingPhaseOutcome:
    started_monotonic_ns: int
    ended_monotonic_ns: int
    exit_code: int
    timed_out: bool
    process_group_complete: bool
    stdout: bytes
    stderr: bytes
    payload: Any
    run_support: Mapping[str, bytes]
    process_identity: Any
    raw_telemetry: Any = None


@dataclass(frozen=True)
class SyntheticTimingCampaignResult:
    artifact: Path
    manifest_sha256: str
    passed: bool
    run_identity_sha256: str


def _fresh_process_identity(
    observation: ProcessIdentityObservation,
    profile_sha256: str,
    run_index: int,
) -> str:
    observation.validate()
    payload = _canonical_json_bytes({
        "domain": "SchurVIO-CP2-E-fresh-process-v1",
        "profile_sha256": profile_sha256,
        "run_index": run_index,
        "pid": observation.pid,
        "start_time_ticks": observation.start_time_ticks,
        "executable_sha256": observation.executable_sha256,
        "loader_before_sha256": observation.loader_before_sha256,
        "loader_after_sha256": observation.loader_after_sha256,
    })
    return _sha256_bytes(payload)


def _canonical_record_bytes(value: Any, label: str) -> Tuple[Mapping[str, Any], bytes]:
    if hasattr(value, "as_record") and callable(value.as_record):
        record = value.as_record()
    elif type(value) is dict:
        record = value
    else:
        _orchestration_fail(label + " is not a canonical record")
    if type(record) is not dict:
        _orchestration_fail(label + " record is not one exact mapping")
    payload = _canonical_json_bytes(record)
    if hasattr(value, "canonical_bytes") and value.canonical_bytes != payload:
        _orchestration_fail(label + " canonical bytes differ from its record")
    return record, payload


def _strict_canonical_json_bytes(payload: bytes, label: str) -> Mapping[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > 64 * 1024 * 1024:
        _orchestration_fail(label + " byte count is invalid")

    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        retained: Dict[str, Any] = {}
        for key, value in items:
            if key in retained:
                _orchestration_fail(label + " contains a duplicate JSON key")
            retained[key] = value
        return retained

    try:
        value = json.loads(
            payload.decode("utf-8", "strict"), object_pairs_hook=pairs,
            parse_float=lambda _token: _orchestration_fail(
                label + " contains a floating-point value"
            ),
            parse_constant=lambda token: _orchestration_fail(
                label + " contains " + token
            ),
        )
    except TimingOrchestrationError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TimingOrchestrationError(label + " is not strict JSON") from exc
    if type(value) is not dict or _canonical_json_bytes(value) != payload:
        _orchestration_fail(label + " is not one canonical JSON mapping")
    return value


def _expected_helper_binding(
    profile_sha256: str, profile: Mapping[str, Any],
) -> Mapping[str, str]:
    helper = profile["privileged_helper"]
    return {
        "profile_sha256": profile_sha256,
        "plan_sha256": helper["plan_sha256"],
        "source_sha256": helper["source_sha256"],
        "protocol_core_sha256": helper["protocol_core_sha256"],
        "root_launcher_sha256": helper["root_launcher_sha256"],
        "import_closure_sha256": helper["import_closure_sha256"],
        "sudoers_sha256": helper["sudoers_sha256"],
    }


def _helper_evidence_bytes(
    value: Any, profile_sha256: str, profile: Mapping[str, Any],
    *, require_success: bool,
) -> Tuple[bytes, bytes, bytes, bytes, Mapping[str, Any]]:
    if type(value) is not dict or set(value) != {
        "transcript_bytes", "terminal_receipt_bytes", "guardian_evidence_bytes",
        "control_evidence_bytes",
    }:
        _orchestration_fail("privileged-helper evidence keys differ")
    transcript = value["transcript_bytes"]
    terminal_payload = value["terminal_receipt_bytes"]
    guardian_evidence = value["guardian_evidence_bytes"]
    control_evidence = value["control_evidence_bytes"]
    if (
        type(transcript) is not bytes or not transcript
        or len(transcript) > 64 * 1024 * 1024
        or not transcript.endswith(b"\n")
    ):
        _orchestration_fail("privileged-helper transcript bytes are invalid")
    if (
        type(guardian_evidence) is not bytes or not guardian_evidence
        or len(guardian_evidence) > profile["limits"]["maximum_guardian_evidence_bytes"]
        or not guardian_evidence.endswith(b"\n")
    ):
        _orchestration_fail("privileged-helper guardian evidence bytes are invalid")
    if (
        type(control_evidence) is not bytes or not control_evidence
        or len(control_evidence) > 3 * (2 * 1024 * 1024 + 4096)
        or not control_evidence.endswith(b"\n")
    ):
        _orchestration_fail("privileged-helper control evidence bytes are invalid")
    terminal = _strict_canonical_json_bytes(
        terminal_payload, "privileged-helper terminal receipt",
    )
    expected_binding = _expected_helper_binding(profile_sha256, profile)
    expected_binding_digest = _sha256_bytes(
        _canonical_json_bytes(expected_binding)
    )
    expected_terminal_keys = {
        "abnormal_recovery", "binding", "binding_digest",
        "control_evidence_seal_sha256", "descriptor_closure", "error_type", "event_count",
        "first_record_sha256", "journal_id", "last_record_sha256",
        "population_seal_sha256", "population_stop_count", "record_type",
        "restoration_proved", "schema_version", "session_id",
        "terminal_journal_sha256", "terminal_status", "transcript_sha256",
        "transcript_size_bytes",
    }
    closure = terminal.get("descriptor_closure")
    if (
        set(terminal) != expected_terminal_keys
        or terminal.get("record_type") != "cp2e_privileged_transcript_terminal"
        or type(terminal.get("schema_version")) is not int
        or terminal.get("schema_version") != 2
        or terminal.get("binding") != expected_binding
        or terminal.get("binding_digest") != expected_binding_digest
        or terminal.get("transcript_sha256") != _sha256_bytes(transcript)
        or terminal.get("transcript_size_bytes") != len(transcript)
        or type(terminal.get("transcript_size_bytes")) is not int
        or type(terminal.get("event_count")) is not int
        or terminal.get("event_count") < 0
        or type(terminal.get("population_stop_count")) is not int
        or type(closure) is not dict
        or set(closure) != {"attempted", "error_type", "passed"}
        or closure.get("attempted") is not True
        or closure.get("passed") is not True
        or closure.get("error_type") is not None
    ):
        _orchestration_fail(
            "privileged-helper terminal receipt does not prove normal exact closure"
        )
    if require_success:
        if (
            terminal["terminal_status"] != "success"
            or terminal["abnormal_recovery"] is not False
            or terminal["restoration_proved"] is not True
            or terminal["population_stop_count"] != 1
            or terminal["event_count"] != 16
            or terminal["error_type"] is not None
            or terminal["control_evidence_seal_sha256"] is None
        ):
            _orchestration_fail(
                "privileged-helper terminal receipt does not prove successful exact closure"
            )
    elif (
        terminal["terminal_status"] not in ("success", "fail_closed")
        or terminal["restoration_proved"] is not True
        or terminal["error_type"] is None
    ):
        _orchestration_fail(
            "failed campaign lacks a fail-closed privileged-helper terminal receipt"
        )
    for key in (
        "binding_digest", "transcript_sha256", "first_record_sha256",
        "last_record_sha256", "terminal_journal_sha256",
        "population_seal_sha256",
        "control_evidence_seal_sha256",
    ):
        _lower_sha256(terminal.get(key), "privileged-helper " + key)
    return (
        transcript, terminal_payload, guardian_evidence, control_evidence,
        terminal,
    )


def _control_receipt_bytes(
    value: Any, phase: str, profile_sha256: str,
) -> Tuple[Mapping[str, Any], bytes]:
    record, payload = _canonical_record_bytes(value, phase + " control receipt")
    expected = {
        "schema_version", "record_type", "profile_sha256", "phase",
        "captured_monotonic_ns", "state", "state_sha256", "passed",
    }
    if (
        set(record) != expected
        or record["schema_version"] != 1
        or record["record_type"] != "cp2_timing_control_state_receipt"
        or record["profile_sha256"] != profile_sha256
        or record["phase"] != phase
        or record["passed"] is not True
        or type(record["captured_monotonic_ns"]) is not int
        or record["captured_monotonic_ns"] < 0
        or type(record["state"]) is not dict
        or record["state_sha256"] != _sha256_bytes(
            _canonical_json_bytes(record["state"])
        )
    ):
        _orchestration_fail(phase + " control receipt is malformed or unbound")
    return record, payload


def _runtime_preflight_receipt(
    request: TimingPhaseRequest, outcome: TimingPhaseOutcome,
    execution_binding_sha256: str,
) -> bytes:
    return _canonical_json_bytes({
        "schema_version": 1,
        "record_type": "cp2_timing_runtime_preflight_receipt",
        "checkpoint": "CP2-E",
        "profile_sha256": request.profile_sha256,
        "execution_binding_sha256": execution_binding_sha256,
        "run_index": request.run_index,
        "argv": list(request.argv),
        "environment": dict(request.environment),
        "cwd": request.cwd,
        "started_monotonic_ns": outcome.started_monotonic_ns,
        "ended_monotonic_ns": outcome.ended_monotonic_ns,
        "exit_code": outcome.exit_code,
        "timed_out": outcome.timed_out,
        "process_group_complete": outcome.process_group_complete,
        "stdout_hex": outcome.stdout.hex(),
        "stdout_sha256": _sha256_bytes(outcome.stdout),
        "stderr_hex": outcome.stderr.hex(),
        "stderr_sha256": _sha256_bytes(outcome.stderr),
        "passed": True,
    })


def _validate_phase_outcome(
    outcome: Any,
    request: TimingPhaseRequest,
    maximum_seconds: int,
    maximum_output_bytes: int,
    previous_end_ns: Any,
    controls_module: Any,
) -> TimingPhaseOutcome:
    if type(outcome) is not TimingPhaseOutcome:
        _orchestration_fail("phase executor returned the wrong result type")
    for value, label in (
        (outcome.started_monotonic_ns, "phase start"),
        (outcome.ended_monotonic_ns, "phase end"),
    ):
        if type(value) is not int or value < 0 or value > U64_MAX:
            _orchestration_fail(label + " is outside u64")
    if (
        outcome.ended_monotonic_ns < outcome.started_monotonic_ns
        or outcome.ended_monotonic_ns - outcome.started_monotonic_ns
        > maximum_seconds * 1_000_000_000
    ):
        _orchestration_fail("phase interval is reverse ordered or oversized")
    if previous_end_ns is not None and outcome.started_monotonic_ns < previous_end_ns:
        _orchestration_fail("phase intervals overlap or reverse order")
    if (
        type(outcome.exit_code) is not int
        or outcome.exit_code != 0
        or outcome.timed_out is not False
        or outcome.process_group_complete is not True
    ):
        _orchestration_fail("phase command failed, timed out, or leaked its process group")
    if type(outcome.stdout) is not bytes or type(outcome.stderr) is not bytes:
        _orchestration_fail("phase output is not exact bytes")
    if len(outcome.stdout) > maximum_output_bytes or len(outcome.stderr) > maximum_output_bytes:
        _orchestration_fail("phase output exceeds the frozen resource limit")
    if request.phase in ("clock_pre", "clock_post") and (outcome.stdout or outcome.stderr):
        _orchestration_fail("clock phase retained unexpected aggregate output")
    if type(outcome.run_support) is not dict:
        _orchestration_fail("phase run-support value is not an exact mapping")
    if request.phase == "ros_run":
        if set(outcome.run_support) != set(RUN_CAPTURE_NAMES):
            _orchestration_fail("ROS run support differs from the exact raw inventory")
        if type(outcome.process_identity) is not ProcessIdentityObservation:
            _orchestration_fail("ROS run lacks an observed fresh-process identity")
        if outcome.payload is not None:
            _orchestration_fail("ROS run returned an unexpected aggregate payload")
        if outcome.raw_telemetry is not None:
            _orchestration_fail("ROS run returned unexpected telemetry")
    elif request.phase in ("clock_pre", "clock_post"):
        if type(outcome.payload) is not bytes or not outcome.payload:
            _orchestration_fail("clock phase lacks its raw snapshot bytes")
        if outcome.run_support or outcome.process_identity is not None:
            _orchestration_fail("clock phase returned ROS-only support")
        expected_phase = "pre" if request.phase == "clock_pre" else "post"
        if (
            type(outcome.raw_telemetry) is not controls_module.RawTelemetrySnapshot
            or outcome.raw_telemetry.phase != expected_phase
            or outcome.raw_telemetry.canonical_bytes == b""
        ):
            _orchestration_fail("clock phase lacks its exact raw telemetry snapshot")
    elif request.phase == "runtime_preflight":
        if (
            outcome.payload is not None or outcome.run_support
            or outcome.process_identity is not None or outcome.raw_telemetry is not None
        ):
            _orchestration_fail("runtime preflight returned an unexpected payload")
    return outcome


def _run_synthetic_orchestration(
    *,
    synthetic_authorization: object,
    frozen_profile: Any,
    execution_binding: TimingExecutionBinding,
    campaign_id: str,
    bag_begin_record_time_ns: int,
    working_root: str,
    staging_path: str,
    destination_path: str,
    control_transaction: Any,
    phase_executor: Callable[[TimingPhaseRequest], TimingPhaseOutcome],
    artifact_module: Any = None,
    publication_module: Any = None,
) -> SyntheticTimingCampaignResult:
    """Exercise the complete candidate using only explicitly synthetic inputs.

    The formal CLI never calls this seam while source freeze and privileged
    feasibility remain unsatisfied.  It deliberately accepts no registry or
    bag-provider capability.
    """

    if synthetic_authorization is not SYNTHETIC_ONLY_AUTHORIZATION:
        _orchestration_fail("synthetic-only orchestration authorization is absent")
    if type(execution_binding) is not TimingExecutionBinding:
        _orchestration_fail("execution binding was not strictly loaded")
    if not callable(phase_executor):
        _orchestration_fail("phase executor is not callable")
    if artifact_module is None:
        artifact_module = __import__("cp2_timing_artifact")
    if publication_module is None:
        publication_module = __import__("cp2_timing_publication")
    profile_codec = __import__("cp2_timing_profile")
    controls_module = __import__("cp2_timing_controls")
    try:
        rebound = profile_codec.load_profile_bytes(frozen_profile.canonical_bytes)
    except (AttributeError, profile_codec.TimingProfileError) as exc:
        raise TimingOrchestrationError("timing profile is not fully validated") from exc
    if rebound.sha256 != frozen_profile.sha256 or rebound.sha256 != execution_binding.profile_sha256:
        _orchestration_fail("profile identity differs across the orchestration binding")
    profile = rebound.value
    if execution_binding.machine_identity_sha256 != profile["target"]["machine_identity_sha256"]:
        _orchestration_fail("execution binding machine identity differs from the profile")
    expected_environment = tuple(sorted(profile["runtime"]["environment"].items()))
    if execution_binding.environment != expected_environment:
        _orchestration_fail("execution environment differs from the frozen profile")
    if type(campaign_id) is not str or SAFE_ID_PATTERN.fullmatch(campaign_id) is None:
        _orchestration_fail("campaign ID is unsafe")
    if (
        type(bag_begin_record_time_ns) is not int
        or bag_begin_record_time_ns < 0
        or bag_begin_record_time_ns > U64_MAX
    ):
        _orchestration_fail("bag-begin record time is outside u64")
    work_root = _orchestration_absolute(working_root, "synthetic working root")
    staging = Path(_orchestration_absolute(staging_path, "artifact staging path"))
    destination = Path(_orchestration_absolute(destination_path, "artifact destination path"))
    publication_parent = Path(profile["artifact"]["publication_parent"])
    if staging.parent != publication_parent or destination.parent != publication_parent:
        _orchestration_fail("artifact paths differ from the frozen publication parent")
    if destination.name != campaign_id:
        _orchestration_fail("artifact destination name differs from the campaign ID")
    if not staging.name.startswith("." + campaign_id + ".partial."):
        _orchestration_fail("artifact staging name differs from the hidden campaign partial")
    if os.path.commonpath((work_root, "/tmp")) != "/tmp" or work_root == "/tmp":
        _orchestration_fail("synthetic working root is not a private /tmp child")

    required_transaction_methods = (
        "apply", "validate_applied", "capture_applied_state", "restore",
        "control_receipts", "helper_evidence",
    )
    if any(not callable(getattr(control_transaction, name, None)) for name in required_transaction_methods):
        _orchestration_fail("control transaction lacks the audited API")

    commands: List[Mapping[str, Any]] = []
    support_files: Dict[str, bytes] = {}
    runs: List[Any] = []
    process_keys = set()
    fresh_identities: List[str] = []
    previous_end_ns: Any = None
    prior_digest: Any = None
    applied_digest: Any = None
    restored_digest: Any = None
    prior_receipt_record: Any = None
    applied_receipt_record: Any = None
    restored_receipt_record: Any = None
    prior_receipt_bytes: Any = None
    applied_receipt_bytes: Any = None
    restored_receipt_bytes: Any = None
    helper_transcript_bytes: Any = None
    helper_terminal_receipt_bytes: Any = None
    helper_guardian_evidence_bytes: Any = None
    helper_control_evidence_bytes: Any = None
    helper_terminal_receipt: Any = None
    transaction_applied = False
    execution_error: Any = None
    try:
        prior = control_transaction.apply()
        transaction_applied = True
        applied = control_transaction.capture_applied_state()
        prior_receipt_record, prior_receipt_bytes = _control_receipt_bytes(
            getattr(control_transaction, "prior_receipt", None), "prior",
            rebound.sha256,
        )
        applied_receipt_record, applied_receipt_bytes = _control_receipt_bytes(
            getattr(control_transaction, "applied_receipt", None), "applied",
            rebound.sha256,
        )
        prior_record, _prior_state_bytes = _canonical_record_bytes(
            prior, "returned prior control snapshot",
        )
        applied_record, _applied_state_bytes = _canonical_record_bytes(
            applied, "returned applied control state",
        )
        if prior_receipt_record["state"] != prior_record:
            _orchestration_fail("prior receipt differs from the returned captured state")
        if applied_receipt_record["state"] != applied_record:
            _orchestration_fail("applied receipt differs from the returned applied state")
        prior_digest = _sha256_bytes(prior_receipt_bytes)
        applied_digest = _sha256_bytes(applied_receipt_bytes)
        control_transaction.validate_applied()
        for run_index, slot in enumerate(profile["pairing"]["run_slots"]):
            if (
                slot["run_index"] != run_index
                or slot["timing_pair_index"] != run_index // 2
                or slot["position_in_pair"] != run_index % 2
            ):
                _orchestration_fail("profile run-slot schedule changed after validation")
            mode = PAIR_ORDER[run_index // 2][run_index % 2]
            if slot["mode"] != mode:
                _orchestration_fail("profile run mode differs from the exact interleaving")
            run_id = "{}-r{}".format(campaign_id, run_index)
            trace_directory = os.path.join(work_root, "run_{}".format(run_index))
            run_phase_payloads: Dict[str, bytes] = {}
            run_phase_telemetry: Dict[str, Any] = {}
            runtime_preflight_bytes: Any = None
            ros_outcome: Any = None
            for phase in COMMAND_PHASES:
                control_transaction.validate_applied()
                argv = execution_binding.render(
                    phase,
                    run_index=run_index,
                    mode=mode,
                    run_id=run_id,
                    trace_directory=trace_directory,
                    sequence_index=profile["input"]["sequence_index"],
                    sequence_id=profile["input"]["sequence_id"],
                )
                request = TimingPhaseRequest(
                    phase, run_index, run_index // 2, mode, run_id,
                    trace_directory, rebound.sha256, applied_digest, argv,
                    execution_binding.environment, execution_binding.cwd,
                )
                outcome = _validate_phase_outcome(
                    phase_executor(request), request,
                    profile["limits"]["maximum_process_seconds"],
                    profile["limits"]["maximum_command_output_bytes"],
                    previous_end_ns,
                    controls_module,
                )
                previous_end_ns = outcome.ended_monotonic_ns
                control_transaction.validate_applied()
                if phase in ("clock_pre", "clock_post"):
                    run_phase_payloads[phase] = outcome.payload
                    run_phase_telemetry[phase] = outcome.raw_telemetry
                elif phase == "runtime_preflight":
                    runtime_preflight_bytes = _runtime_preflight_receipt(
                        request, outcome, execution_binding.launch_sha256,
                    )
                elif phase == "ros_run":
                    ros_outcome = outcome
                command_id = len(commands)
                commands.append({
                    "schema_version": 1,
                    "record_type": "timing_command",
                    "command_id": command_id,
                    "run_index": run_index,
                    "timing_pair_index": run_index // 2,
                    "phase": phase,
                    "argv": list(argv),
                    "environment": dict(execution_binding.environment),
                    "cwd": execution_binding.cwd,
                    "started_monotonic_ns": outcome.started_monotonic_ns,
                    "ended_monotonic_ns": outcome.ended_monotonic_ns,
                    "exit_code": outcome.exit_code,
                    "timed_out": outcome.timed_out,
                    "process_group_complete": outcome.process_group_complete,
                    "stdout_sha256": _sha256_bytes(outcome.stdout),
                    "stderr_sha256": _sha256_bytes(outcome.stderr),
                })
            if (
                ros_outcome is None
                or set(run_phase_payloads) != {"clock_pre", "clock_post"}
                or set(run_phase_telemetry) != {"clock_pre", "clock_post"}
                or type(runtime_preflight_bytes) is not bytes
            ):
                _orchestration_fail("run did not complete the exact four-phase schedule")
            observation = ros_outcome.process_identity
            observation.validate()
            if not (
                ros_outcome.started_monotonic_ns
                <= observation.observed_monotonic_ns
                <= ros_outcome.ended_monotonic_ns
            ):
                _orchestration_fail("process identity was observed outside the ROS command")
            if observation.executable_sha256 != execution_binding.executable_sha256:
                _orchestration_fail("observed executable differs from the source binding")
            if observation.loader_before_sha256 != _sha256_bytes(ros_outcome.run_support["loader_before.txt"]):
                _orchestration_fail("observed loader-before identity differs from raw support")
            if observation.loader_after_sha256 != _sha256_bytes(ros_outcome.run_support["loader_after.txt"]):
                _orchestration_fail("observed loader-after identity differs from raw support")
            process_key = (observation.pid, observation.start_time_ticks)
            if process_key in process_keys:
                _orchestration_fail("six run slots do not retain six fresh process observations")
            process_keys.add(process_key)
            affinity = _strict_json_loads(
                ros_outcome.run_support["affinity_audit.json"].decode("utf-8", "strict")
            )
            if _canonical_json_bytes(affinity) != ros_outcome.run_support["affinity_audit.json"]:
                _orchestration_fail("affinity audit is not canonical JSON")
            if affinity.get("root_pid") != observation.pid:
                _orchestration_fail("affinity audit root PID differs from process observation")
            identity = _fresh_process_identity(observation, rebound.sha256, run_index)
            if identity in fresh_identities:
                _orchestration_fail("fresh-process identity digest is duplicate")
            fresh_identities.append(identity)
            prefix = "runs/run_{}/".format(run_index)
            run_support = {
                prefix + name: payload
                for name, payload in ros_outcome.run_support.items()
            }
            run_support[prefix + "clock_pre.json"] = run_phase_payloads["clock_pre"]
            run_support[prefix + "clock_post.json"] = run_phase_payloads["clock_post"]
            pre_raw = run_phase_telemetry["clock_pre"]
            post_raw = run_phase_telemetry["clock_post"]
            thermal_ranges = [
                item["expected"] for item in profile["observations"]
                if item["parser"] == "hwmon_temperature_millicelsius"
            ]
            if len(thermal_ranges) != 1:
                _orchestration_fail("profile does not bind one thermal range")
            comparison = controls_module.compare_raw_telemetry(
                pre_raw, post_raw, profile["runtime"]["cpu_ids"],
                profile["runtime"]["accepted_current_frequency_khz"],
                (
                    thermal_ranges[0]["minimum"],
                    thermal_ranges[0]["maximum"],
                ),
                SYNTHETIC_APERF_MPERF_RATIO_BOUNDS,
            )
            process_identity_bytes = _canonical_json_bytes(observation.record(
                rebound.sha256, execution_binding.launch_sha256, run_index,
            ))
            run_support[prefix + "process_identity.json"] = process_identity_bytes
            run_support[prefix + "runtime_preflight.json"] = runtime_preflight_bytes
            run_support[prefix + "telemetry_pre.json"] = pre_raw.canonical_bytes
            run_support[prefix + "telemetry_post.json"] = post_raw.canonical_bytes
            run_support[prefix + "telemetry_comparison.json"] = comparison["canonical_bytes"]
            run_support[prefix + "stdout.bin"] = ros_outcome.stdout
            run_support[prefix + "stderr.bin"] = ros_outcome.stderr
            support_files.update(run_support)
            runs.append(artifact_module.TimingRun(
                run_index=run_index,
                profile_sha256=rebound.sha256,
                run_id=run_id,
                fresh_process_identity=identity,
                pre_snapshot_sha256=_sha256_bytes(run_phase_payloads["clock_pre"]),
                post_snapshot_sha256=_sha256_bytes(run_phase_payloads["clock_post"]),
                process_identity_sha256=_sha256_bytes(process_identity_bytes),
                runtime_preflight_sha256=_sha256_bytes(runtime_preflight_bytes),
                telemetry_pre_sha256=pre_raw.sha256,
                telemetry_post_sha256=post_raw.sha256,
                telemetry_comparison_sha256=comparison["sha256"],
                trace_bundle_sha256=artifact_module.trace_bundle_sha256(
                    run_support, run_index
                ),
            ))
    except BaseException as exc:
        execution_error = exc
    finally:
        if transaction_applied:
            try:
                control_transaction.restore()
                if getattr(control_transaction, "state", None) != "restored":
                    _orchestration_fail("control transaction did not reach restored state")
                abnormal_recovery = getattr(
                    control_transaction, "had_abnormal_recovery", False
                )
                if type(abnormal_recovery) is not bool or abnormal_recovery:
                    raise TimingOrchestrationIndeterminate(
                        "control transaction required abnormal guardian recovery"
                    )
                receipts = control_transaction.control_receipts()
                if type(receipts) is not dict or set(receipts) != {
                    "prior", "applied", "restored",
                }:
                    _orchestration_fail("control transaction returned an incomplete receipt set")
                final_prior, final_prior_bytes = _control_receipt_bytes(
                    receipts["prior"], "prior", rebound.sha256,
                )
                final_applied, final_applied_bytes = _control_receipt_bytes(
                    receipts["applied"], "applied", rebound.sha256,
                )
                restored_receipt_record, restored_receipt_bytes = (
                    _control_receipt_bytes(
                        receipts["restored"], "restored", rebound.sha256,
                    )
                )
                if (
                    final_prior != prior_receipt_record
                    or final_applied != applied_receipt_record
                    or final_prior_bytes != prior_receipt_bytes
                    or final_applied_bytes != applied_receipt_bytes
                    or restored_receipt_record["state"]
                    != prior_receipt_record["state"]
                ):
                    _orchestration_fail("final control receipts drifted or restoration is inexact")
                if not (
                    prior_receipt_record["captured_monotonic_ns"]
                    < applied_receipt_record["captured_monotonic_ns"]
                    < restored_receipt_record["captured_monotonic_ns"]
                ):
                    _orchestration_fail("control receipt chronology is not strict")
                restored_digest = _sha256_bytes(restored_receipt_bytes)
                (
                    helper_transcript_bytes,
                    helper_terminal_receipt_bytes,
                    helper_guardian_evidence_bytes,
                    helper_control_evidence_bytes,
                    helper_terminal_receipt,
                ) = _helper_evidence_bytes(
                    control_transaction.helper_evidence(execution_error is None),
                    rebound.sha256,
                    profile, require_success=execution_error is None,
                )
            except BaseException as restore_error:
                if isinstance(restore_error, TimingOrchestrationIndeterminate):
                    raise
                raise TimingOrchestrationIndeterminate(
                    "timing execution did not prove exact host-control restoration: {}".format(
                        restore_error
                    )
                ) from restore_error
    if execution_error is not None:
        raise execution_error
    if (
        len(commands) != 24
        or len(runs) != 6
        or len(fresh_identities) != 6
        or prior_digest is None
        or applied_digest is None
        or restored_digest is None
        or prior_receipt_bytes is None
        or applied_receipt_bytes is None
        or restored_receipt_bytes is None
        or helper_transcript_bytes is None
        or helper_terminal_receipt_bytes is None
        or helper_guardian_evidence_bytes is None
        or helper_control_evidence_bytes is None
        or helper_terminal_receipt is None
    ):
        _orchestration_fail("campaign did not retain its exact complete schedule/state")

    run_identity_sha = _sha256_bytes(_canonical_json_bytes({
        "domain": "SchurVIO-CP2-E-six-fresh-processes-v1",
        "profile_sha256": rebound.sha256,
        "fresh_process_identities": fresh_identities,
    }))
    provenance = {
        "schema_version": 1,
        "record_type": "cp2_timing_provenance",
        "checkpoint": "CP2-E",
        "profile_sha256": rebound.sha256,
        "source_commit": execution_binding.source_commit,
        "source_tree": execution_binding.source_tree,
        "unit_manifest_sha256": execution_binding.unit_manifest_sha256,
        "sequence_set_manifest_sha256": execution_binding.sequence_set_manifest_sha256,
        "bag_sha256": profile["input"]["bag_sha256"],
        "executable_sha256": execution_binding.executable_sha256,
        "dso_closure_sha256": execution_binding.dso_closure_sha256,
        "configuration_sha256": execution_binding.configuration_sha256,
        "launch_sha256": execution_binding.launch_sha256,
        "boot_id_sha256": execution_binding.boot_id_sha256,
        "machine_identity_sha256": execution_binding.machine_identity_sha256,
        "run_identity_sha256": run_identity_sha,
        "control_prior_sha256": prior_digest,
        "control_applied_sha256": applied_digest,
        "control_restored_sha256": restored_digest,
        "helper_transcript_sha256": _sha256_bytes(helper_transcript_bytes),
        "helper_terminal_receipt_sha256": _sha256_bytes(
            helper_terminal_receipt_bytes
        ),
        "helper_binding_digest": helper_terminal_receipt["binding_digest"],
        "helper_terminal_journal_sha256": helper_terminal_receipt[
            "terminal_journal_sha256"
        ],
        "helper_population_seal_sha256": helper_terminal_receipt[
            "population_seal_sha256"
        ],
        "helper_guardian_evidence_sha256": _sha256_bytes(
            helper_guardian_evidence_bytes
        ),
        "helper_control_evidence_sha256": _sha256_bytes(
            helper_control_evidence_bytes
        ),
        "helper_control_evidence_seal_sha256": helper_terminal_receipt[
            "control_evidence_seal_sha256"
        ],
    }
    assembly_source = artifact_module.TimingAssemblyInput(
        profile_bytes=rebound.canonical_bytes,
        profile_sha256=rebound.sha256,
        execution_binding_bytes=execution_binding.canonical_bytes,
        control_prior_bytes=prior_receipt_bytes,
        control_applied_bytes=applied_receipt_bytes,
        control_restored_bytes=restored_receipt_bytes,
        helper_transcript_bytes=helper_transcript_bytes,
        helper_terminal_receipt_bytes=helper_terminal_receipt_bytes,
        helper_guardian_evidence_bytes=helper_guardian_evidence_bytes,
        helper_control_evidence_bytes=helper_control_evidence_bytes,
        sequence_index=profile["input"]["sequence_index"],
        sequence_id=profile["input"]["sequence_id"],
        bag_begin_record_time_ns=bag_begin_record_time_ns,
        frozen_offset_ns=profile["input"]["frozen_offset_ns"],
        runs=tuple(runs),
        provenance=provenance,
        commands=tuple(commands),
        support_files=support_files,
    )

    staging_identity: Any = None
    try:
        staging_identity = publication_module.create_staging_directory(staging)
        assembled = artifact_module.assemble_timing_artifact(staging, assembly_source)
        detached = artifact_module.verify_timing_artifact(staging, assembled.manifest_sha256)
        if detached.get("passed") is not assembled.passed:
            _orchestration_fail("staging detached verification result differs from assembly")
        sealed_identity = publication_module.seal_artifact_directory(staging)
        if sealed_identity != staging_identity:
            _orchestration_fail("artifact staging inode changed before publication")

        def verify_published(path: Path) -> None:
            verified = artifact_module.verify_timing_artifact(
                path, assembled.manifest_sha256
            )
            if verified.get("passed") is not assembled.passed:
                _orchestration_fail(
                    "published detached verification result differs from assembly"
                )

        published = publication_module.publish_artifact_noreplace(
            staging,
            destination,
            published_validator=verify_published,
            expected_staging_identity=staging_identity,
        )
        if published.identity != staging_identity or published.path != destination:
            raise TimingOrchestrationIndeterminate(
                "publisher returned a different final artifact identity"
            )
        return SyntheticTimingCampaignResult(
            destination, assembled.manifest_sha256, assembled.passed,
            run_identity_sha,
        )
    except BaseException as artifact_error:
        indeterminate_type = getattr(
            publication_module, "TimingPublicationIndeterminate", ()
        )
        if indeterminate_type and isinstance(artifact_error, indeterminate_type):
            raise TimingOrchestrationIndeterminate(
                "timing artifact publication is indeterminate"
            ) from artifact_error
        if staging_identity is not None:
            try:
                publication_module.cleanup_unpublished_artifact(
                    staging, staging_identity
                )
            except BaseException as cleanup_error:
                raise TimingOrchestrationIndeterminate(
                    "timing artifact failed and exact caller cleanup also failed"
                ) from cleanup_error
        raise


def _case_functions(temporary_root: str) -> Mapping[str, Callable[[], None]]:
    file_a = os.path.join(temporary_root, "file-a")
    file_b = os.path.join(temporary_root, "file-b")
    symlink_path = os.path.join(temporary_root, "file-symlink")
    root_symlink = os.path.join(temporary_root, "root-symlink")
    with open(file_a, "wb") as stream:
        stream.write(b"a")
    os.link(file_a, file_b)
    os.symlink(file_a, symlink_path)
    os.symlink(temporary_root, root_symlink)
    digest_a = hashlib.sha256(b"a").hexdigest()

    def forbidden_provider() -> None:
        # Rejection deliberately precedes a provider-call counter increment.
        _reject("bag-provider call forbidden in self-test")

    def schema_extra() -> None:
        _exact_keys({"required": 1, "extra": 2}, ("required",), "synthetic schema")

    def schema_missing() -> None:
        _exact_keys({}, ("required",), "synthetic schema")

    def bad_timeout() -> None:
        _readiness_records_valid(({"index": 0, "timed_out": True, "process_group_complete": True},))

    def bad_process_group() -> None:
        _readiness_records_valid(({"index": 0, "timed_out": False, "process_group_complete": False},))

    def bad_launch() -> None:
        value = _valid_launch_outputs()
        value["trajectory"] = True
        _launch_outputs_valid(value)

    def changed_clock() -> None:
        before = _valid_clock_snapshot()
        after = dict(before)
        after["boost"] = True
        _clock_controls_equal(before, after)

    precision_scale = (1 << 53) + 1

    def median_ratio_over_limit() -> None:
        _ratio_passes(
            (10 * precision_scale, 1),
            (11 * precision_scale + 1, 1),
            (11, 10),
        )

    def p95_ratio_over_limit() -> None:
        _ratio_passes(
            (20 * precision_scale, 1),
            (23 * precision_scale + 1, 1),
            (23, 20),
        )

    return {
        "valid_minimal_fixture": _validate_minimal_fixture,
        "cli_exclusivity": lambda: _parse_actual_cli(("--self-test", "unexpected")),
        "forbidden_bag_provider": forbidden_provider,
        "non_tmp_write": lambda: _tmp_child("/var/tmp/cp2-forbidden", temporary_root),
        "schema_extra_key": schema_extra,
        "schema_missing_key": schema_missing,
        "duplicate_json_key": lambda: _strict_json_loads('{"key":1,"key":2}'),
        "unsafe_path": lambda: _safe_relpath("../escape"),
        "symlink": lambda: _regular_nonsymlink_single_link(symlink_path),
        "hardlink": lambda: _regular_nonsymlink_single_link(file_a),
        "manifest_missing_entry": lambda: _manifest_matches({"a": digest_a}, {}),
        "manifest_extra_entry": lambda: _manifest_matches({"a": digest_a}, {"a": digest_a, "b": digest_a}),
        "manifest_digest_mismatch": lambda: _manifest_matches({"a": digest_a}, {"a": "0" * 64}),
        "readiness_order": lambda: _readiness_records_valid((
            {"index": 1, "timed_out": False, "process_group_complete": True},
            {"index": 0, "timed_out": False, "process_group_complete": True},
        )),
        "readiness_timeout": bad_timeout,
        "readiness_process_group": bad_process_group,
        "readiness_lock_identity": lambda: _readiness_lock_valid(
            {"regular_nonsymlink": True, "owned_by_effective_user": False, "mode": 0o600}
        ),
        "readiness_snapshot_mutation": lambda: _snapshots_equal(b"before", b"after"),
        "ignored_source_path": lambda: _ignored_path_allowed("ignored-cache/file"),
        "snapshotted_root_symlink": lambda: _real_directory_nonsymlink(root_symlink),
        "launch_output_combination": bad_launch,
        "unit_anchor_commit_mismatch": lambda: _unit_anchor_matches("a" * 40, "b" * 40, "c" * 40, "b" * 40),
        "wrong_pair_order": lambda: _pair_order_valid((("schur", "nullspace"),) + PAIR_ORDER[1:]),
        "wrong_pair_index": lambda: _pair_indices_valid((0, 2, 1)),
        "runtime_drift": lambda: _identity_equal("runtime-a", "runtime-b", "runtime"),
        "config_drift": lambda: _identity_equal("config-a", "config-b", "configuration"),
        "profile_drift": lambda: _identity_equal("profile-a", "profile-b", "profile"),
        "changed_clock_snapshot": changed_clock,
        "affinity_mismatch": lambda: _affinity_equal((0, 1, 2, 3), (0, 1, 2, 4)),
        "warm_up_boundary_error": lambda: _warmup_eligible(59_999_999_999, 60_000_000_000),
        "warm_up_u64_overflow": lambda: _warmup_eligible(U64_MAX + 1, U64_MAX),
        "unilateral_noncommon_samples": lambda: _common_population_valid((10, 20), (10, 20), (10,)),
        "omitted_bilateral_common_sample": lambda: _common_population_valid(
            (10,), (10, 20), (10, 20)
        ),
        "duplicate_timestamp": lambda: _unique_timestamps((10, 10)),
        "timestamp_u64_overflow": lambda: _unique_timestamps((10, U64_MAX + 1)),
        "negative_duration": lambda: _duration_valid(-1),
        "duration_u64_overflow": lambda: _duration_valid(U64_MAX + 1),
        "noninteger_duration": lambda: _duration_valid(1.0),
        "nonprimary_inclusion": lambda: _primary_inclusion_valid(
            {"primary": False, "nonempty": True, "preflight_accepted": True, "committed": True}
        ),
        "incorrect_linear_quantiles": lambda: _quantile_matches(
            (0, 100), (19, 20), (100, 1)
        ),
        "binary64_quantile_rounding": lambda: _quantile_matches(
            (precision_scale, precision_scale + 1),
            (1, 2),
            (precision_scale, 1),
        ),
        "median_ratio_limit": median_ratio_over_limit,
        "p95_ratio_limit": p95_ratio_over_limit,
    }


def _run_self_test() -> int:
    temporary_root = tempfile.mkdtemp(prefix="schurvio-lite-cp2-timing-self-test-", dir="/tmp")
    cases: List[Dict[str, Any]] = []
    cleanup_succeeded = False
    try:
        functions = _case_functions(temporary_root)
        if tuple(functions) != EXPECTED_CASE_NAMES:
            raise RuntimeError("self-test function inventory differs from EXPECTED_CASE_NAMES")
        for index, name in enumerate(EXPECTED_CASE_NAMES):
            expected_rejection = name != "valid_minimal_fixture"
            observed_rejection = False
            unexpected_failure = False
            try:
                functions[name]()
            except SelfTestRejection:
                observed_rejection = True
            except Exception:
                unexpected_failure = True
            passed = not unexpected_failure and observed_rejection == expected_rejection
            cases.append(
                {
                    "index": index,
                    "name": name,
                    "expected_rejection": expected_rejection,
                    "observed_rejection": observed_rejection,
                    "passed": passed,
                }
            )
    finally:
        shutil.rmtree(temporary_root)
        cleanup_succeeded = not os.path.lexists(temporary_root)

    passed = cleanup_succeeded and len(cases) == len(EXPECTED_CASE_NAMES) and all(case["passed"] for case in cases)
    result = {
        "schema_version": 1,
        "record_type": "self_test_result",
        "entrypoint": ENTRYPOINT,
        "temporary_root": temporary_root,
        "bag_provider_calls": 0,
        "cases": cases,
        "case_count": len(cases),
        "passed": passed,
    }
    sys.stdout.write(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    return 0 if passed else 1


def _actual_mode(arguments: Sequence[str]) -> int:
    try:
        _parse_actual_cli(arguments)
    except SelfTestRejection as exc:
        sys.stderr.write("CP2-E timing runner CLI rejected: {}\n".format(exc))
        return 2

    # The lock is a source constant, intentionally checked after syntax-only
    # CLI validation and before even a pathname lookup for the profile,
    # registry, bag, result tree, project module, or host-control surface.
    # An untracked or locally substituted profile can therefore never unlock
    # this candidate by its mere presence.
    reason = (
        "the source-frozen schema-v2 helper/plan/topology/cpuset/IRQ/telemetry/"
        "descendant-guardian binding and successful noninteractive privileged "
        "feasibility proof are absent"
    )
    sys.stderr.write(
        "CP2-E actual mode is blocked before registry or bag access: {}. "
        "No evidence directory was created and no host setting was changed.\n".format(reason)
    )
    return getattr(os, "EX_CONFIG", 78)


def main(arguments: Sequence[str]) -> int:
    if tuple(arguments) == ("--self-test",):
        return _run_self_test()
    return _actual_mode(arguments)


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
