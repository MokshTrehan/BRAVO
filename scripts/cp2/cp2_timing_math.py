#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Proposed, non-authorizing exact CP2-E timing mathematics.

This module is deliberately data-free and clock-free.  It only validates an
already-collected sequence of unsigned-64-bit integer nanoseconds, computes the
two proposed CP2-E NumPy-style linear quantiles exactly, forms reduced exact
candidate/baseline ratios, orders the frozen median-of-three aggregate, and
compares exact rational results against the two proposed ratio limits.  It does
not authorize an actual timing run, select a timing profile, inspect host
controls, or read or write evidence.

For a sorted population ``x`` of size ``n``, the retained quantile follows
``h = (n - 1) * q`` with zero-based ``lo = floor(h)`` and ``hi = ceil(h)``.
Interpolation is evaluated as an exact rational; no binary floating-point value
is created.  Ratio gates are evaluated by mathematically exact Python-integer
cross products whose retained values are bounded to u128; exact ratio ordering
uses the full u256 product domain.  Exact rational scalars have one canonical
32-lowercase-hex-digit-per-component record encoding.  Rank arithmetic that is
contractually u64 uses explicit checked operations.
Each quantile retains the exact canonical sorted u64 population plus its
domain-separated SHA-256 as a big-endian unsigned-256-bit integer, preventing
two independently computed populations from being combined as ordinary
evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import math
import re
from typing import Any, Dict, Mapping, Tuple


U64_MAX = (1 << 64) - 1
U128_MAX = (1 << 128) - 1
U256_MAX = (1 << 256) - 1
MAX_TIMING_SAMPLE_COUNT = 1_000_000
# Every retained exact rational component is serialized as one fixed-width
# unsigned-128 value by the proposed CP2-E contract.  Keep the historical
# public constant name as the single component bound used by callers/tests.
MAX_EXACT_RATIO_COMPONENT = U128_MAX

P50_QUANTILE = (1, 2)
P95_QUANTILE = (19, 20)
MEDIAN_RATIO_LIMIT = (11, 10)
P95_RATIO_LIMIT = (23, 20)
TIMING_COMMON_DOMAIN = b"SchurVIO-CP2-timing-common-v1\0"

_ALLOWED_QUANTILES = (P50_QUANTILE, P95_QUANTILE)
_ALLOWED_RATIO_LIMITS = (MEDIAN_RATIO_LIMIT, P95_RATIO_LIMIT)
_U128_HEX = re.compile(r"^[0-9a-f]{32}$")
_U256_HEX = re.compile(r"^[0-9a-f]{64}$")


class TimingMathError(ValueError):
    """Raised when proposed CP2-E exact-math preconditions are violated."""


def _plain_integer(value: Any, label: str) -> int:
    if type(value) is not int:
        raise TimingMathError(label + " must be an integer")
    return value


def _u64(value: Any, label: str) -> int:
    converted = _plain_integer(value, label)
    if converted < 0 or converted > U64_MAX:
        raise TimingMathError(label + " is outside u64")
    return converted


def _positive_u64(value: Any, label: str) -> int:
    converted = _u64(value, label)
    if converted == 0:
        raise TimingMathError(label + " must be positive")
    return converted


def u128_to_hex(value: Any, label: str = "u128 value") -> str:
    """Encode one retained unsigned-128 value in its only canonical spelling."""

    converted = _plain_integer(value, label)
    if converted < 0 or converted > U128_MAX:
        raise TimingMathError(label + " is outside u128")
    return format(converted, "032x")


def u128_from_hex(value: Any, label: str = "u128 value") -> int:
    """Decode exactly 32 lowercase hexadecimal digits; reject alternate forms."""

    if type(value) is not str or _U128_HEX.fullmatch(value) is None:
        raise TimingMathError(label + " must be exactly 32 lowercase hexadecimal digits")
    return int(value, 16)


def u256_to_hex(value: Any, label: str = "u256 value") -> str:
    """Encode one unsigned-256 value with leading zeros preserved."""

    converted = _plain_integer(value, label)
    if converted < 0 or converted > U256_MAX:
        raise TimingMathError(label + " is outside u256")
    return format(converted, "064x")


def u256_from_hex(value: Any, label: str = "u256 value") -> int:
    """Decode exactly 64 lowercase hexadecimal digits."""

    if type(value) is not str or _U256_HEX.fullmatch(value) is None:
        raise TimingMathError(label + " must be exactly 64 lowercase hexadecimal digits")
    return int(value, 16)


def _exact_rational_record(value: Any, label: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != {"numerator_hex", "denominator_hex"}:
        raise TimingMathError(label + " record keys differ from the canonical schema")
    return value


def _checked_u64_add(left: int, right: int, label: str) -> int:
    lhs = _u64(left, label + " left operand")
    rhs = _u64(right, label + " right operand")
    if lhs > U64_MAX - rhs:
        raise TimingMathError(label + " overflows u64 addition")
    return lhs + rhs


def _checked_u64_multiply(left: int, right: int, label: str) -> int:
    lhs = _u64(left, label + " left operand")
    rhs = _u64(right, label + " right operand")
    if lhs != 0 and rhs > U64_MAX // lhs:
        raise TimingMathError(label + " overflows u64 multiplication")
    return lhs * rhs


def _canonical_allowed_pair(
    numerator: Any,
    denominator: Any,
    allowed: Tuple[Tuple[int, int], ...],
    label: str,
) -> Tuple[int, int]:
    num = _u64(numerator, label + " numerator")
    den = _positive_u64(denominator, label + " denominator")
    if num == 0 or math.gcd(num, den) != 1:
        raise TimingMathError(label + " must be a positive canonical rational")
    pair = (num, den)
    if pair not in allowed:
        raise TimingMathError(label + " is not one of the proposed frozen values")
    return pair


@dataclass(frozen=True)
class RationalNanoseconds:
    """A reduced, nonnegative exact rational whose value is within u64 ns."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        numerator = _plain_integer(self.numerator, "rational numerator")
        denominator = _positive_u64(self.denominator, "rational denominator")
        if numerator < 0:
            raise TimingMathError("rational numerator must be nonnegative")
        if numerator > U128_MAX:
            raise TimingMathError("rational numerator exceeds the retained u128 domain")
        # The numerator may be wider than u64 because an interpolated u64
        # value is retained over a u64 denominator.  This inequality bounds
        # the represented value itself to u64 without using a float.
        if numerator > U64_MAX * denominator:
            raise TimingMathError("rational nanoseconds value exceeds u64")
        if math.gcd(numerator, denominator) != 1:
            raise TimingMathError("rational nanoseconds must be reduced")

    @classmethod
    def from_u64(cls, value: Any) -> "RationalNanoseconds":
        return cls(_u64(value, "nanoseconds"), 1)


def rational_nanoseconds_to_record(value: Any) -> Dict[str, str]:
    """Return the canonical fixed-width JSON scalar record for exact ns."""

    rational = _validated_rational_nanoseconds(value, "rational record input")
    return {
        "numerator_hex": u128_to_hex(rational.numerator, "rational numerator"),
        "denominator_hex": u128_to_hex(rational.denominator, "rational denominator"),
    }


def rational_nanoseconds_from_record(value: Any) -> RationalNanoseconds:
    """Parse and revalidate one canonical fixed-width exact-ns record."""

    record = _exact_rational_record(value, "rational nanoseconds")
    return RationalNanoseconds(
        u128_from_hex(record["numerator_hex"], "rational numerator"),
        u128_from_hex(record["denominator_hex"], "rational denominator"),
    )


def _validated_rational_nanoseconds(value: Any, label: str) -> RationalNanoseconds:
    if type(value) is not RationalNanoseconds:
        raise TimingMathError(label + " must be canonical RationalNanoseconds")
    try:
        return RationalNanoseconds(value.numerator, value.denominator)
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid rational evidence") from exc


@dataclass(frozen=True)
class ExactRatio:
    """A reduced nonnegative candidate/baseline ratio."""

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        numerator = _plain_integer(self.numerator, "exact-ratio numerator")
        denominator = _plain_integer(self.denominator, "exact-ratio denominator")
        if numerator < 0 or numerator > MAX_EXACT_RATIO_COMPONENT:
            raise TimingMathError("exact-ratio numerator is outside its bounded domain")
        if denominator <= 0 or denominator > MAX_EXACT_RATIO_COMPONENT:
            raise TimingMathError("exact-ratio denominator is outside its bounded domain")
        if math.gcd(numerator, denominator) != 1:
            raise TimingMathError("exact ratio must be reduced")


def exact_ratio_to_record(value: Any) -> Dict[str, str]:
    """Return the canonical fixed-width JSON scalar record for an exact ratio."""

    ratio = _validated_exact_ratio(value, "exact-ratio record input")
    return {
        "numerator_hex": u128_to_hex(ratio.numerator, "exact-ratio numerator"),
        "denominator_hex": u128_to_hex(ratio.denominator, "exact-ratio denominator"),
    }


def exact_ratio_from_record(value: Any) -> ExactRatio:
    """Parse and revalidate one canonical fixed-width exact-ratio record."""

    record = _exact_rational_record(value, "exact ratio")
    return ExactRatio(
        u128_from_hex(record["numerator_hex"], "exact-ratio numerator"),
        u128_from_hex(record["denominator_hex"], "exact-ratio denominator"),
    )


def _validated_exact_ratio(value: Any, label: str) -> ExactRatio:
    if type(value) is not ExactRatio:
        raise TimingMathError(label + " must be a canonical ExactRatio")
    try:
        return ExactRatio(value.numerator, value.denominator)
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid exact-ratio evidence") from exc


@dataclass(frozen=True)
class LinearRank:
    """Canonical exact zero-based rank and interpolation weight."""

    sample_count: int
    quantile_numerator: int
    quantile_denominator: int
    low_index: int
    high_index: int
    weight_numerator: int
    weight_denominator: int

    def __post_init__(self) -> None:
        count = _positive_u64(self.sample_count, "linear-rank sample count")
        q_num, q_den = _canonical_allowed_pair(
            self.quantile_numerator,
            self.quantile_denominator,
            _ALLOWED_QUANTILES,
            "linear-rank quantile",
        )
        low = _u64(self.low_index, "linear low index")
        high = _u64(self.high_index, "linear high index")
        weight_num = _u64(self.weight_numerator, "linear weight numerator")
        weight_den = _positive_u64(self.weight_denominator, "linear weight denominator")

        scaled = _checked_u64_multiply(count - 1, q_num, "linear rank numerator")
        expected_low, remainder = divmod(scaled, q_den)
        expected_high = (
            expected_low
            if remainder == 0
            else _checked_u64_add(expected_low, 1, "linear high rank")
        )
        if remainder == 0:
            expected_weight = (0, 1)
        else:
            divisor = math.gcd(remainder, q_den)
            expected_weight = (remainder // divisor, q_den // divisor)
        if low != expected_low or high != expected_high or (weight_num, weight_den) != expected_weight:
            raise TimingMathError("linear-rank evidence differs from exact recomputation")
        if high >= count:
            raise TimingMathError("linear rank is outside its population")


def _validated_linear_rank(value: Any, label: str) -> LinearRank:
    if type(value) is not LinearRank:
        raise TimingMathError(label + " must be canonical LinearRank evidence")
    try:
        return LinearRank(
            sample_count=value.sample_count,
            quantile_numerator=value.quantile_numerator,
            quantile_denominator=value.quantile_denominator,
            low_index=value.low_index,
            high_index=value.high_index,
            weight_numerator=value.weight_numerator,
            weight_denominator=value.weight_denominator,
        )
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid linear-rank evidence") from exc


@dataclass(frozen=True)
class LinearQuantile:
    """Retained exact evidence for one deterministic sorted quantile."""

    rank: LinearRank
    low_value_ns: int
    high_value_ns: int
    value_ns: RationalNanoseconds
    population_sha256: int
    ordered_samples_ns: Tuple[int, ...]

    def __post_init__(self) -> None:
        rank = _validated_linear_rank(self.rank, "linear-quantile rank")
        if rank.sample_count > MAX_TIMING_SAMPLE_COUNT:
            raise TimingMathError("linear-quantile population exceeds the resource limit")
        value = _validated_rational_nanoseconds(self.value_ns, "linear-quantile value")
        binding = _plain_integer(self.population_sha256, "population SHA-256")
        if binding < 0 or binding >= (1 << 256):
            raise TimingMathError("population SHA-256 is outside 256 bits")
        if type(self.ordered_samples_ns) is not tuple:
            raise TimingMathError("linear-quantile population must be an exact tuple")
        if len(self.ordered_samples_ns) != rank.sample_count:
            raise TimingMathError("linear-quantile population count differs from its rank")
        previous = None
        for sample in self.ordered_samples_ns:
            retained_sample = _u64(sample, "retained timing sample nanoseconds")
            if previous is not None and retained_sample < previous:
                raise TimingMathError("retained timing population is not sorted")
            previous = retained_sample
        if _population_sha256(self.ordered_samples_ns) != binding:
            raise TimingMathError("population SHA-256 differs from retained timing samples")
        low_value = _u64(self.low_value_ns, "linear low value nanoseconds")
        high_value = _u64(self.high_value_ns, "linear high value nanoseconds")
        if low_value > high_value:
            raise TimingMathError("linear-quantile bracketing values are unordered")
        if rank.low_index == rank.high_index and low_value != high_value:
            raise TimingMathError("an exact linear rank has unequal bracketing values")
        if (
            low_value != self.ordered_samples_ns[rank.low_index]
            or high_value != self.ordered_samples_ns[rank.high_index]
        ):
            raise TimingMathError("linear bracketing values differ from the retained population")
        weight_num = rank.weight_numerator
        weight_den = rank.weight_denominator
        numerator = (weight_den - weight_num) * low_value + weight_num * high_value
        expected = _reduced_rational_nanoseconds(numerator, weight_den)
        if value != expected:
            raise TimingMathError("linear-quantile value differs from exact interpolation")


def _validated_linear_quantile(value: Any, label: str) -> LinearQuantile:
    if type(value) is not LinearQuantile:
        raise TimingMathError(label + " must be canonical LinearQuantile evidence")
    try:
        return LinearQuantile(
            rank=value.rank,
            low_value_ns=value.low_value_ns,
            high_value_ns=value.high_value_ns,
            value_ns=value.value_ns,
            population_sha256=value.population_sha256,
            ordered_samples_ns=value.ordered_samples_ns,
        )
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid linear-quantile evidence") from exc


@dataclass(frozen=True)
class TimingQuantiles:
    """The proposed CP2-E p50 and p95 over one population snapshot."""

    p50: LinearQuantile
    p95: LinearQuantile

    def __post_init__(self) -> None:
        p50 = _validated_linear_quantile(self.p50, "p50")
        p95 = _validated_linear_quantile(self.p95, "p95")
        if (
            (p50.rank.quantile_numerator, p50.rank.quantile_denominator) != P50_QUANTILE
            or (p95.rank.quantile_numerator, p95.rank.quantile_denominator) != P95_QUANTILE
            or p50.rank.sample_count != p95.rank.sample_count
        ):
            raise TimingMathError("timing quantiles do not contain one p50/p95 population pair")
        if p50.population_sha256 != p95.population_sha256:
            raise TimingMathError("p50 and p95 do not bind the same sorted population")
        if p50.ordered_samples_ns != p95.ordered_samples_ns:
            raise TimingMathError("p50 and p95 do not retain the same sorted population")
        if p50.value_ns.numerator * p95.value_ns.denominator > p95.value_ns.numerator * p50.value_ns.denominator:
            raise TimingMathError("p50 exceeds p95")


@dataclass(frozen=True)
class MedianOfThreeRatio:
    """Canonical nondecreasing exact ordering and its middle ratio."""

    ordered_ratios: Tuple[ExactRatio, ExactRatio, ExactRatio]
    median_ratio: ExactRatio

    def __post_init__(self) -> None:
        if type(self.ordered_ratios) is not tuple or len(self.ordered_ratios) != 3:
            raise TimingMathError("median evidence must retain exactly three ordered ratios")
        ordered = tuple(
            _validated_exact_ratio(value, "median ordered ratio")
            for value in self.ordered_ratios
        )
        median = _validated_exact_ratio(self.median_ratio, "median ratio")
        if _compare_exact_ratios(ordered[0], ordered[1]) > 0 or _compare_exact_ratios(ordered[1], ordered[2]) > 0:
            raise TimingMathError("median ratios are not in nondecreasing exact order")
        if median != ordered[1]:
            raise TimingMathError("retained median is not the middle ordered ratio")


@dataclass(frozen=True)
class RatioGate:
    """Canonical exact evidence for one rational ratio comparison."""

    baseline_ns: RationalNanoseconds
    candidate_ns: RationalNanoseconds
    candidate_over_baseline: ExactRatio
    limit_numerator: int
    limit_denominator: int
    left_cross_product: int
    right_cross_product: int
    passed: bool

    def __post_init__(self) -> None:
        baseline = _validated_rational_nanoseconds(self.baseline_ns, "ratio-gate baseline")
        candidate = _validated_rational_nanoseconds(self.candidate_ns, "ratio-gate candidate")
        retained_ratio = _validated_exact_ratio(
            self.candidate_over_baseline,
            "ratio-gate candidate/baseline ratio",
        )
        if baseline.numerator == 0:
            raise TimingMathError("ratio baseline must be positive")
        expected_ratio = _candidate_baseline_ratio_from_validated(candidate, baseline)
        if retained_ratio != expected_ratio:
            raise TimingMathError("retained candidate/baseline ratio differs from exact recomputation")
        limit_num, limit_den = _canonical_allowed_pair(
            self.limit_numerator,
            self.limit_denominator,
            _ALLOWED_RATIO_LIMITS,
            "ratio-gate limit",
        )
        left = _plain_integer(self.left_cross_product, "ratio left cross product")
        right = _plain_integer(self.right_cross_product, "ratio right cross product")
        if left < 0 or right < 0:
            raise TimingMathError("ratio cross products must be nonnegative")
        expected_left = candidate.numerator * baseline.denominator * limit_den
        expected_right = baseline.numerator * candidate.denominator * limit_num
        if expected_left > U128_MAX or expected_right > U128_MAX:
            raise TimingMathError(
                "ratio-gate cross product exceeds the retained u128 domain"
            )
        if left != expected_left or right != expected_right:
            raise TimingMathError("ratio-gate cross product differs from exact recomputation")
        if type(self.passed) is not bool or self.passed is not (left <= right):
            raise TimingMathError("ratio-gate decision differs from exact comparison")


@dataclass(frozen=True)
class TimingPairResult:
    """Exact CP2-E result for one interleaved nullspace/Schur pair.

    The cross-mode population is bound independently from both duration
    populations.  This prevents a producer from presenting correct quantiles
    for a different timestamp intersection or silently permuting pair slots.
    """

    timing_pair_index: int
    common_timestamps_ns: Tuple[int, ...]
    common_payload_sha256: int
    baseline_quantiles: TimingQuantiles
    candidate_quantiles: TimingQuantiles
    p50_gate: RatioGate
    p95_gate: RatioGate
    passed: bool

    def __post_init__(self) -> None:
        pair_index = _u64(self.timing_pair_index, "timing-pair index")
        if pair_index > 2:
            raise TimingMathError("timing-pair index is outside the frozen three-pair campaign")
        timestamps = _validated_common_timestamps(self.common_timestamps_ns)
        common_sha = _plain_integer(self.common_payload_sha256, "common payload SHA-256")
        if common_sha < 0 or common_sha > U256_MAX:
            raise TimingMathError("common payload SHA-256 is outside u256")
        if _common_payload_sha256(pair_index, timestamps) != common_sha:
            raise TimingMathError("common payload SHA-256 differs from exact recomputation")
        baseline = _validated_timing_quantiles(self.baseline_quantiles, "baseline quantiles")
        candidate = _validated_timing_quantiles(self.candidate_quantiles, "candidate quantiles")
        if baseline.p50.rank.sample_count != len(timestamps) or candidate.p50.rank.sample_count != len(timestamps):
            raise TimingMathError("timing quantile population count differs from the common timestamps")
        p50 = _validated_ratio_gate(self.p50_gate, "p50 gate")
        p95 = _validated_ratio_gate(self.p95_gate, "p95 gate")
        if (
            p50.baseline_ns != baseline.p50.value_ns
            or p50.candidate_ns != candidate.p50.value_ns
            or (p50.limit_numerator, p50.limit_denominator) != MEDIAN_RATIO_LIMIT
        ):
            raise TimingMathError("p50 gate does not bind the retained pair quantiles")
        if (
            p95.baseline_ns != baseline.p95.value_ns
            or p95.candidate_ns != candidate.p95.value_ns
            or (p95.limit_numerator, p95.limit_denominator) != P95_RATIO_LIMIT
        ):
            raise TimingMathError("p95 gate does not bind the retained pair quantiles")
        expected_pass = p50.passed and p95.passed
        if type(self.passed) is not bool or self.passed is not expected_pass:
            raise TimingMathError("timing-pair decision differs from both exact gates")


@dataclass(frozen=True)
class TimingCampaignResult:
    """Exact three-pair CP2-E aggregate with no aggregate-only escape hatch."""

    pair_results: Tuple[TimingPairResult, TimingPairResult, TimingPairResult]
    median_p50: MedianOfThreeRatio
    median_p95: MedianOfThreeRatio
    every_pair_passed: bool
    passed: bool

    def __post_init__(self) -> None:
        if type(self.pair_results) is not tuple or len(self.pair_results) != 3:
            raise TimingMathError("timing campaign must retain exactly three pair results")
        pairs = tuple(_validated_timing_pair_result(value) for value in self.pair_results)
        if tuple(value.timing_pair_index for value in pairs) != (0, 1, 2):
            raise TimingMathError("timing campaign pair indices are not exactly 0,1,2")
        expected_p50 = median_of_three_ratios(tuple(value.p50_gate.candidate_over_baseline for value in pairs))
        expected_p95 = median_of_three_ratios(tuple(value.p95_gate.candidate_over_baseline for value in pairs))
        median_p50 = _validated_median(self.median_p50, "campaign p50 median")
        median_p95 = _validated_median(self.median_p95, "campaign p95 median")
        if median_p50 != expected_p50 or median_p95 != expected_p95:
            raise TimingMathError("timing campaign median differs from exact pair ratios")
        expected_every = all(value.passed for value in pairs)
        if type(self.every_pair_passed) is not bool or self.every_pair_passed is not expected_every:
            raise TimingMathError("timing campaign every-pair decision differs")
        # The medians are evidence summaries only.  They never rescue a failed
        # pair, so the campaign pass bit is exactly the every-pair result.
        if type(self.passed) is not bool or self.passed is not expected_every:
            raise TimingMathError("timing campaign decision differs from every-pair gates")


def linear_rank(
    sample_count: Any,
    quantile_numerator: Any,
    quantile_denominator: Any,
) -> LinearRank:
    """Return the exact proposed linear-interpolation rank for a u64 count.

    This helper allocates no sample population.  The rank numerator is strict
    checked-u64 arithmetic, so an unrepresentable ``(n - 1) * q_num`` is
    rejected instead of silently widening or wrapping.
    """

    count = _positive_u64(sample_count, "sample count")
    q_num, q_den = _canonical_allowed_pair(
        quantile_numerator,
        quantile_denominator,
        _ALLOWED_QUANTILES,
        "quantile",
    )
    interval_count = count - 1
    scaled_index = _checked_u64_multiply(interval_count, q_num, "linear rank numerator")
    low_index, remainder = divmod(scaled_index, q_den)
    high_index = low_index if remainder == 0 else _checked_u64_add(low_index, 1, "linear high rank")
    if high_index >= count:
        raise TimingMathError("linear rank is outside its population")

    if remainder == 0:
        weight_numerator, weight_denominator = 0, 1
    else:
        divisor = math.gcd(remainder, q_den)
        weight_numerator = remainder // divisor
        weight_denominator = q_den // divisor
    return LinearRank(
        sample_count=count,
        quantile_numerator=q_num,
        quantile_denominator=q_den,
        low_index=low_index,
        high_index=high_index,
        weight_numerator=weight_numerator,
        weight_denominator=weight_denominator,
    )


def _snapshot_sorted_samples(samples: Any) -> Tuple[int, ...]:
    if isinstance(samples, (str, bytes, bytearray)) or not isinstance(samples, Sequence):
        raise TimingMathError("timing samples must be a finite sequence")
    try:
        declared_count = len(samples)
    except Exception as exc:
        raise TimingMathError("timing sample count is invalid") from exc
    if declared_count <= 0:
        raise TimingMathError("timing sample population is empty")
    if declared_count > MAX_TIMING_SAMPLE_COUNT:
        raise TimingMathError("timing sample population exceeds the resource limit")

    retained = []
    try:
        for value in samples:
            if len(retained) >= declared_count:
                raise TimingMathError("timing sequence changed while being snapshotted")
            retained.append(_u64(value, "timing sample nanoseconds"))
    except TimingMathError:
        raise
    except Exception as exc:
        raise TimingMathError("timing samples are not a stable finite sequence") from exc
    if len(retained) != declared_count:
        raise TimingMathError("timing sequence changed while being snapshotted")
    retained.sort()
    return tuple(retained)


def _population_sha256(ordered: Tuple[int, ...]) -> int:
    digest = hashlib.sha256()
    digest.update(b"SchurVIO-Lite CP2-E sorted u64 timing population v1\x00")
    digest.update(len(ordered).to_bytes(8, "big"))
    for value in ordered:
        digest.update(value.to_bytes(8, "big"))
    return int.from_bytes(digest.digest(), "big")


def _reduced_rational_nanoseconds(numerator: int, denominator: int) -> RationalNanoseconds:
    divisor = math.gcd(numerator, denominator)
    return RationalNanoseconds(numerator // divisor, denominator // divisor)


def _reduced_exact_ratio(numerator: int, denominator: int) -> ExactRatio:
    divisor = math.gcd(numerator, denominator)
    return ExactRatio(numerator // divisor, denominator // divisor)


def _candidate_baseline_ratio_from_validated(
    candidate_ns: RationalNanoseconds,
    baseline_ns: RationalNanoseconds,
) -> ExactRatio:
    if baseline_ns.numerator == 0:
        raise TimingMathError("ratio baseline must be positive")
    numerator = candidate_ns.numerator * baseline_ns.denominator
    denominator = candidate_ns.denominator * baseline_ns.numerator
    return _reduced_exact_ratio(numerator, denominator)


def _quantile_from_sorted(
    ordered: Tuple[int, ...],
    quantile_numerator: int,
    quantile_denominator: int,
    population_sha256: int,
) -> LinearQuantile:
    rank = linear_rank(len(ordered), quantile_numerator, quantile_denominator)
    low_value = ordered[rank.low_index]
    high_value = ordered[rank.high_index]
    weight_num = rank.weight_numerator
    weight_den = rank.weight_denominator

    # These products intentionally use exact Python integers.  Every factor is
    # already bounded to u64, and the reduced rational numerator legitimately
    # may be wider than u64 even though its represented value is within u64.
    numerator = (weight_den - weight_num) * low_value + weight_num * high_value
    value = _reduced_rational_nanoseconds(numerator, weight_den)
    return LinearQuantile(
        rank=rank,
        low_value_ns=low_value,
        high_value_ns=high_value,
        value_ns=value,
        population_sha256=population_sha256,
        ordered_samples_ns=ordered,
    )


def linear_quantile_ns(
    samples: Any,
    quantile_numerator: Any,
    quantile_denominator: Any,
) -> LinearQuantile:
    """Compute one proposed frozen linear quantile without floating point."""

    q_num, q_den = _canonical_allowed_pair(
        quantile_numerator,
        quantile_denominator,
        _ALLOWED_QUANTILES,
        "quantile",
    )
    ordered = _snapshot_sorted_samples(samples)
    return _quantile_from_sorted(ordered, q_num, q_den, _population_sha256(ordered))


def timing_quantiles_ns(samples: Any) -> TimingQuantiles:
    """Compute p50 and p95 from one immutable-in-function sample snapshot."""

    ordered = _snapshot_sorted_samples(samples)
    population_sha256 = _population_sha256(ordered)
    return TimingQuantiles(
        p50=_quantile_from_sorted(ordered, *P50_QUANTILE, population_sha256),
        p95=_quantile_from_sorted(ordered, *P95_QUANTILE, population_sha256),
    )


def _validated_timing_quantiles(value: Any, label: str) -> TimingQuantiles:
    if type(value) is not TimingQuantiles:
        raise TimingMathError(label + " must be canonical TimingQuantiles evidence")
    try:
        return TimingQuantiles(p50=value.p50, p95=value.p95)
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid timing-quantile evidence") from exc


def _validated_ratio_gate(value: Any, label: str) -> RatioGate:
    if type(value) is not RatioGate:
        raise TimingMathError(label + " must be canonical RatioGate evidence")
    try:
        return RatioGate(
            baseline_ns=value.baseline_ns,
            candidate_ns=value.candidate_ns,
            candidate_over_baseline=value.candidate_over_baseline,
            limit_numerator=value.limit_numerator,
            limit_denominator=value.limit_denominator,
            left_cross_product=value.left_cross_product,
            right_cross_product=value.right_cross_product,
            passed=value.passed,
        )
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid ratio-gate evidence") from exc


def _validated_median(value: Any, label: str) -> MedianOfThreeRatio:
    if type(value) is not MedianOfThreeRatio:
        raise TimingMathError(label + " must be canonical MedianOfThreeRatio evidence")
    try:
        return MedianOfThreeRatio(value.ordered_ratios, value.median_ratio)
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError(label + " is forged or invalid median evidence") from exc


def _validated_common_timestamps(value: Any) -> Tuple[int, ...]:
    if type(value) is not tuple or not value:
        raise TimingMathError("common timestamps must be one nonempty exact tuple")
    if len(value) > MAX_TIMING_SAMPLE_COUNT:
        raise TimingMathError("common timestamp population exceeds the resource limit")
    previous = None
    for timestamp in value:
        retained = _u64(timestamp, "common camera timestamp")
        if previous is not None and retained <= previous:
            raise TimingMathError("common camera timestamps are not strictly increasing")
        previous = retained
    return value


def canonical_common_population_payload(
    timing_pair_index: Any, common_timestamps_ns: Any
) -> bytes:
    """Encode the frozen domain-separated common-population payload."""

    pair_index = _u64(timing_pair_index, "timing-pair index")
    if pair_index > 2:
        raise TimingMathError("timing-pair index is outside the frozen three-pair campaign")
    timestamps = _validated_common_timestamps(common_timestamps_ns)
    output = bytearray(TIMING_COMMON_DOMAIN)
    output.extend(pair_index.to_bytes(8, "big"))
    output.extend(len(timestamps).to_bytes(8, "big"))
    for timestamp in timestamps:
        output.extend(timestamp.to_bytes(8, "big"))
    return bytes(output)


def _common_payload_sha256(pair_index: int, timestamps: Tuple[int, ...]) -> int:
    return int.from_bytes(
        hashlib.sha256(canonical_common_population_payload(pair_index, timestamps)).digest(),
        "big",
    )


def timing_pair_result(
    timing_pair_index: Any,
    common_timestamps_ns: Any,
    baseline_durations_ns: Any,
    candidate_durations_ns: Any,
) -> TimingPairResult:
    """Build one exact pair result from already joined timestamp-order rows."""

    pair_index = _u64(timing_pair_index, "timing-pair index")
    timestamps = _validated_common_timestamps(common_timestamps_ns)
    if not isinstance(baseline_durations_ns, Sequence) or isinstance(
        baseline_durations_ns, (str, bytes, bytearray)
    ):
        raise TimingMathError("baseline timing durations must be a finite sequence")
    if not isinstance(candidate_durations_ns, Sequence) or isinstance(
        candidate_durations_ns, (str, bytes, bytearray)
    ):
        raise TimingMathError("candidate timing durations must be a finite sequence")
    if len(baseline_durations_ns) != len(timestamps) or len(candidate_durations_ns) != len(timestamps):
        raise TimingMathError("mode timing duration counts differ from the common timestamps")
    baseline = timing_quantiles_ns(baseline_durations_ns)
    candidate = timing_quantiles_ns(candidate_durations_ns)
    p50 = ratio_gate(baseline.p50.value_ns, candidate.p50.value_ns, *MEDIAN_RATIO_LIMIT)
    p95 = ratio_gate(baseline.p95.value_ns, candidate.p95.value_ns, *P95_RATIO_LIMIT)
    return TimingPairResult(
        timing_pair_index=pair_index,
        common_timestamps_ns=timestamps,
        common_payload_sha256=_common_payload_sha256(pair_index, timestamps),
        baseline_quantiles=baseline,
        candidate_quantiles=candidate,
        p50_gate=p50,
        p95_gate=p95,
        passed=p50.passed and p95.passed,
    )


def _validated_timing_pair_result(value: Any) -> TimingPairResult:
    if type(value) is not TimingPairResult:
        raise TimingMathError("campaign pair result must be canonical TimingPairResult evidence")
    try:
        return TimingPairResult(
            timing_pair_index=value.timing_pair_index,
            common_timestamps_ns=value.common_timestamps_ns,
            common_payload_sha256=value.common_payload_sha256,
            baseline_quantiles=value.baseline_quantiles,
            candidate_quantiles=value.candidate_quantiles,
            p50_gate=value.p50_gate,
            p95_gate=value.p95_gate,
            passed=value.passed,
        )
    except (AttributeError, TimingMathError) as exc:
        raise TimingMathError("campaign pair result is forged or invalid") from exc


def timing_campaign_result(pair_results: Any) -> TimingCampaignResult:
    """Build the frozen three-pair aggregate; every individual pair must pass."""

    if type(pair_results) is not tuple or len(pair_results) != 3:
        raise TimingMathError("timing campaign requires an exact three-pair tuple")
    pairs = tuple(_validated_timing_pair_result(value) for value in pair_results)
    if tuple(value.timing_pair_index for value in pairs) != (0, 1, 2):
        raise TimingMathError("timing campaign pair indices are not exactly 0,1,2")
    p50 = median_of_three_ratios(tuple(value.p50_gate.candidate_over_baseline for value in pairs))
    p95 = median_of_three_ratios(tuple(value.p95_gate.candidate_over_baseline for value in pairs))
    every = all(value.passed for value in pairs)
    return TimingCampaignResult(
        pair_results=pairs,  # type: ignore[arg-type]
        median_p50=p50,
        median_p95=p95,
        every_pair_passed=every,
        passed=every,
    )


def exact_candidate_baseline_ratio(candidate_ns: Any, baseline_ns: Any) -> ExactRatio:
    """Return a reduced exact ``candidate / baseline`` timing ratio."""

    candidate = _validated_rational_nanoseconds(candidate_ns, "ratio candidate")
    baseline = _validated_rational_nanoseconds(baseline_ns, "ratio baseline")
    return _candidate_baseline_ratio_from_validated(candidate, baseline)


def _compare_exact_ratios(left: ExactRatio, right: ExactRatio) -> int:
    left_cross = left.numerator * right.denominator
    right_cross = right.numerator * left.denominator
    # Each operand component is retained u128.  Ordering is intentionally a
    # u256 operation rather than imposing the gate's narrower u128-product
    # rule; this is the exact domain promised for median-of-three ordering.
    if left_cross > U256_MAX or right_cross > U256_MAX:
        raise TimingMathError("ratio-ordering cross product exceeds u256")
    return -1 if left_cross < right_cross else (1 if left_cross > right_cross else 0)


def median_of_three_ratios(ratios: Any) -> MedianOfThreeRatio:
    """Return the stable exact nondecreasing order and median of three ratios."""

    if isinstance(ratios, (str, bytes, bytearray)) or not isinstance(ratios, Sequence):
        raise TimingMathError("median ratios must be a finite three-element sequence")
    try:
        if len(ratios) != 3:
            raise TimingMathError("median requires exactly three ratios")
        ordered = [
            _validated_exact_ratio(ratios[index], "median input ratio")
            for index in range(3)
        ]
    except TimingMathError:
        raise
    except Exception as exc:
        raise TimingMathError("median ratios are not a stable three-element sequence") from exc

    # A fixed sorting network is deterministic and only swaps on strict exact
    # inequality, so equal fractions retain their input order without changing
    # the canonical ratio-value result.
    if _compare_exact_ratios(ordered[0], ordered[1]) > 0:
        ordered[0], ordered[1] = ordered[1], ordered[0]
    if _compare_exact_ratios(ordered[1], ordered[2]) > 0:
        ordered[1], ordered[2] = ordered[2], ordered[1]
    if _compare_exact_ratios(ordered[0], ordered[1]) > 0:
        ordered[0], ordered[1] = ordered[1], ordered[0]
    retained = tuple(ordered)
    return MedianOfThreeRatio(retained, retained[1])


def ratio_gate(
    baseline_ns: Any,
    candidate_ns: Any,
    limit_numerator: Any,
    limit_denominator: Any,
) -> RatioGate:
    """Compare ``candidate / baseline <= limit`` by exact cross products."""

    baseline = _validated_rational_nanoseconds(baseline_ns, "ratio baseline")
    candidate = _validated_rational_nanoseconds(candidate_ns, "ratio candidate")
    exact_ratio = _candidate_baseline_ratio_from_validated(candidate, baseline)
    limit_num, limit_den = _canonical_allowed_pair(
        limit_numerator,
        limit_denominator,
        _ALLOWED_RATIO_LIMITS,
        "ratio limit",
    )

    # candidate/base <= limit_num/limit_den.  Python integers are exact, and
    # all factors are bounded by RationalNanoseconds plus a frozen u64 limit.
    left = candidate.numerator * baseline.denominator * limit_den
    right = baseline.numerator * candidate.denominator * limit_num
    if left > U128_MAX or right > U128_MAX:
        raise TimingMathError("ratio-gate cross product exceeds the retained u128 domain")
    return RatioGate(
        baseline_ns=baseline,
        candidate_ns=candidate,
        candidate_over_baseline=exact_ratio,
        limit_numerator=limit_num,
        limit_denominator=limit_den,
        left_cross_product=left,
        right_cross_product=right,
        passed=left <= right,
    )
