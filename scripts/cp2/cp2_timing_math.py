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
cross products whose factors are bounded by validated u64/rational inputs.
Rank arithmetic that is contractually u64 uses explicit checked operations.
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
from typing import Any, Tuple


U64_MAX = (1 << 64) - 1
MAX_TIMING_SAMPLE_COUNT = 1_000_000
MAX_EXACT_RATIO_COMPONENT = U64_MAX * U64_MAX * U64_MAX

P50_QUANTILE = (1, 2)
P95_QUANTILE = (19, 20)
MEDIAN_RATIO_LIMIT = (11, 10)
P95_RATIO_LIMIT = (23, 20)

_ALLOWED_QUANTILES = (P50_QUANTILE, P95_QUANTILE)
_ALLOWED_RATIO_LIMITS = (MEDIAN_RATIO_LIMIT, P95_RATIO_LIMIT)


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
        if left != expected_left or right != expected_right:
            raise TimingMathError("ratio-gate cross product differs from exact recomputation")
        if type(self.passed) is not bool or self.passed is not (left <= right):
            raise TimingMathError("ratio-gate decision differs from exact comparison")


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


def exact_candidate_baseline_ratio(candidate_ns: Any, baseline_ns: Any) -> ExactRatio:
    """Return a reduced exact ``candidate / baseline`` timing ratio."""

    candidate = _validated_rational_nanoseconds(candidate_ns, "ratio candidate")
    baseline = _validated_rational_nanoseconds(baseline_ns, "ratio baseline")
    return _candidate_baseline_ratio_from_validated(candidate, baseline)


def _compare_exact_ratios(left: ExactRatio, right: ExactRatio) -> int:
    left_cross = left.numerator * right.denominator
    right_cross = right.numerator * left.denominator
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
