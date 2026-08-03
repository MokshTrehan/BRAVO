#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed CP2-E timing-runner surface.

The committed CP2-E artifact schema and fixed-clock profile do not yet exist.
Consequently, actual mode deliberately stops after syntax-only CLI validation:
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
from typing import Any, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple


sys.dont_write_bytecode = True

ENTRYPOINT = "scripts/cp2/run_timing_pair.py"
PROFILE_RELPATH = "project/cp2_timing_profile.yaml"
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PAIR_ORDER = (("nullspace", "schur"), ("schur", "nullspace"), ("nullspace", "schur"))
FROZEN_CLOCK_KEYS = ("cpu_ids", "affinity", "driver", "governor", "min_frequency", "max_frequency", "boost")
U64_MAX = (1 << 64) - 1

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

    # This is intentionally checked only after exact CLI syntax validation and
    # before any project import, registry access, bag-provider import, output
    # creation, or host-setting operation.  Even an untracked profile cannot
    # authorize execution while the committed artifact section is still a
    # non-authorizing preregistration.
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    profile_path = os.path.join(repo_root, PROFILE_RELPATH)
    if os.path.lexists(profile_path):
        reason = "the complete authorizing CP2-E artifact schema is absent"
    else:
        reason = "project/cp2_timing_profile.yaml and the complete authorizing CP2-E artifact schema are absent"
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
