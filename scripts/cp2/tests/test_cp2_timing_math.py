#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic mutation and boundary tests for proposed exact CP2-E math."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import fields, is_dataclass, replace
import itertools
from pathlib import Path
import sys
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_timing_math as timing_math  # noqa: E402


class OversizedSequence(Sequence):
    def __len__(self):
        return timing_math.MAX_TIMING_SAMPLE_COUNT + 1

    def __getitem__(self, index):
        raise AssertionError("oversized population must be rejected before item access")


class LyingSequence(Sequence):
    def __len__(self):
        return 1

    def __getitem__(self, index):
        if index < 2:
            return index
        raise IndexError


def assert_integer_tree(test_case, value):
    if is_dataclass(value):
        for field in fields(value):
            assert_integer_tree(test_case, getattr(value, field.name))
        return
    if type(value) is tuple:
        for item in value:
            assert_integer_tree(test_case, item)
        return
    test_case.assertIs(type(value) in (int, bool), True)


class ExactLinearQuantileTests(unittest.TestCase):
    def test_two_point_results_distinguish_linear_from_nearest_rank(self):
        quantiles = timing_math.timing_quantiles_ns([0, 100])
        self.assertEqual(quantiles.p50.value_ns, timing_math.RationalNanoseconds(50, 1))
        self.assertEqual(quantiles.p95.value_ns, timing_math.RationalNanoseconds(95, 1))
        self.assertNotEqual(quantiles.p50.value_ns, timing_math.RationalNanoseconds(100, 1))
        self.assertNotEqual(quantiles.p95.value_ns, timing_math.RationalNanoseconds(100, 1))

    def test_three_point_p95_is_linear_nineteen_not_nearest_rank_twenty(self):
        result = timing_math.linear_quantile_ns([0, 10, 20], 19, 20)
        self.assertEqual(result.value_ns, timing_math.RationalNanoseconds(19, 1))
        self.assertEqual((result.rank.low_index, result.rank.high_index), (1, 2))
        self.assertEqual((result.rank.weight_numerator, result.rank.weight_denominator), (9, 10))

    def test_fractional_nanosecond_results_are_reduced(self):
        p50 = timing_math.linear_quantile_ns([0, 1], 1, 2)
        p95 = timing_math.linear_quantile_ns([0, 1], 19, 20)
        self.assertEqual(p50.value_ns, timing_math.RationalNanoseconds(1, 2))
        self.assertEqual(p95.value_ns, timing_math.RationalNanoseconds(19, 20))
        self.assertEqual(
            format(p50.population_sha256, "064x"),
            "f1df145da2a9da5a903a74ae318d003b2c6904eef9b2305d9716a718e87fdb9e",
        )
        self.assertEqual(p50.ordered_samples_ns, (0, 1))

    def test_above_binary64_exact_integer_range_preserves_one_nanosecond(self):
        base = (1 << 53) + 1
        quantiles = timing_math.timing_quantiles_ns([base, base + 1])
        self.assertEqual(quantiles.p50.value_ns, timing_math.RationalNanoseconds(2 * base + 1, 2))
        self.assertEqual(quantiles.p95.value_ns, timing_math.RationalNanoseconds(20 * base + 19, 20))
        self.assertNotEqual(quantiles.p50.value_ns.numerator, 2 * base)

    def test_u64_maximum_samples_remain_exact(self):
        quantiles = timing_math.timing_quantiles_ns([timing_math.U64_MAX, timing_math.U64_MAX])
        expected = timing_math.RationalNanoseconds(timing_math.U64_MAX, 1)
        self.assertEqual(quantiles.p50.value_ns, expected)
        self.assertEqual(quantiles.p95.value_ns, expected)

    def test_sorting_makes_both_results_order_invariant(self):
        population = [91, 7, 31, 31, 52, 13, 2]
        forward = timing_math.timing_quantiles_ns(population)
        reverse = timing_math.timing_quantiles_ns(list(reversed(population)))
        rotated = timing_math.timing_quantiles_ns(population[3:] + population[:3])
        self.assertEqual(forward, reverse)
        self.assertEqual(forward, rotated)

    def test_single_sample_has_zero_weight_for_both_quantiles(self):
        quantiles = timing_math.timing_quantiles_ns([123])
        for result in (quantiles.p50, quantiles.p95):
            self.assertEqual((result.rank.low_index, result.rank.high_index), (0, 0))
            self.assertEqual((result.rank.weight_numerator, result.rank.weight_denominator), (0, 1))
            self.assertEqual(result.value_ns, timing_math.RationalNanoseconds(123, 1))

    def test_retained_evidence_contains_no_float(self):
        quantiles = timing_math.timing_quantiles_ns([(1 << 53) + 1, (1 << 53) + 2, (1 << 53) + 9])
        assert_integer_tree(self, quantiles)

    def test_composite_binds_one_population_and_rejects_p50_above_p95(self):
        low_population = timing_math.timing_quantiles_ns([0, 0])
        high_population = timing_math.timing_quantiles_ns([100, 100])
        with self.assertRaisesRegex(timing_math.TimingMathError, "same sorted population"):
            timing_math.TimingQuantiles(low_population.p50, high_population.p95)

        with self.assertRaisesRegex(timing_math.TimingMathError, "SHA-256 differs"):
            replace(
                high_population.p95,
                population_sha256=low_population.p50.population_sha256,
            )

        forged_p95 = object.__new__(timing_math.LinearQuantile)
        for field in fields(low_population.p95):
            object.__setattr__(forged_p95, field.name, getattr(low_population.p95, field.name))
        object.__setattr__(forged_p95, "low_value_ns", 200)
        object.__setattr__(forged_p95, "high_value_ns", 200)
        object.__setattr__(forged_p95, "value_ns", timing_math.RationalNanoseconds.from_u64(200))
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.TimingQuantiles(low_population.p50, forged_p95)

    def test_forged_hash_cannot_combine_plausibly_ordered_populations(self):
        low_population = timing_math.timing_quantiles_ns([0, 0])
        high_population = timing_math.timing_quantiles_ns([100, 100])
        forged = object.__new__(timing_math.LinearQuantile)
        for field in fields(high_population.p95):
            object.__setattr__(forged, field.name, getattr(high_population.p95, field.name))
        object.__setattr__(forged, "population_sha256", low_population.p50.population_sha256)
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.TimingQuantiles(low_population.p50, forged)

    def test_mutated_nested_quantile_evidence_is_revalidated(self):
        quantiles = timing_math.timing_quantiles_ns([10, 20, 30])
        object.__setattr__(quantiles.p50.rank, "low_index", 2)
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.TimingQuantiles(quantiles.p50, quantiles.p95)


class RankAndInputBoundaryTests(unittest.TestCase):
    def test_p50_and_p95_rank_evidence_is_exact(self):
        p50 = timing_math.linear_rank(10, 1, 2)
        p95 = timing_math.linear_rank(10, 19, 20)
        self.assertEqual((p50.low_index, p50.high_index), (4, 5))
        self.assertEqual((p50.weight_numerator, p50.weight_denominator), (1, 2))
        self.assertEqual((p95.low_index, p95.high_index), (8, 9))
        self.assertEqual((p95.weight_numerator, p95.weight_denominator), (11, 20))

    def test_rank_multiplication_overflow_is_rejected(self):
        overflowing_count = timing_math.U64_MAX // 19 + 2
        with self.assertRaisesRegex(timing_math.TimingMathError, "overflows u64 multiplication"):
            timing_math.linear_rank(overflowing_count, 19, 20)

    def test_checked_u64_addition_and_multiplication_overflow_are_rejected(self):
        with self.assertRaisesRegex(timing_math.TimingMathError, "addition"):
            timing_math._checked_u64_add(timing_math.U64_MAX, 1, "synthetic add")
        with self.assertRaisesRegex(timing_math.TimingMathError, "multiplication"):
            timing_math._checked_u64_multiply(timing_math.U64_MAX, 2, "synthetic multiply")

    def test_invalid_counts_are_rejected(self):
        for value in (True, False, 0, -1, 1.0, timing_math.U64_MAX + 1):
            with self.subTest(value=value):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.linear_rank(value, 1, 2)

    def test_noncanonical_unapproved_and_invalid_quantiles_are_rejected(self):
        invalid = ((2, 4), (1, 3), (0, 1), (1, 0), (-1, 2), (True, 2), (1, False), (timing_math.U64_MAX + 1, 2))
        for numerator, denominator in invalid:
            with self.subTest(pair=(numerator, denominator)):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.linear_quantile_ns([1, 2], numerator, denominator)

    def test_empty_nonsequence_and_invalid_sample_types_are_rejected(self):
        invalid_populations = ([], (), "12", b"12", (value for value in (1, 2)), [True], [1.0], [-1], [timing_math.U64_MAX + 1])
        for population in invalid_populations:
            with self.subTest(population=type(population).__name__):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.timing_quantiles_ns(population)

    def test_oversized_population_is_rejected_before_item_access(self):
        with self.assertRaisesRegex(timing_math.TimingMathError, "resource limit"):
            timing_math.timing_quantiles_ns(OversizedSequence())

    def test_sequence_length_mutation_is_rejected(self):
        with self.assertRaisesRegex(timing_math.TimingMathError, "changed"):
            timing_math.timing_quantiles_ns(LyingSequence())


class ExactRatioGateTests(unittest.TestCase):
    def test_candidate_baseline_ratio_is_exact_and_reduced(self):
        integer_ratio = timing_math.exact_candidate_baseline_ratio(
            timing_math.RationalNanoseconds.from_u64(22),
            timing_math.RationalNanoseconds.from_u64(20),
        )
        fractional_ratio = timing_math.exact_candidate_baseline_ratio(
            timing_math.RationalNanoseconds(11, 20),
            timing_math.RationalNanoseconds(1, 2),
        )
        self.assertEqual(integer_ratio, timing_math.ExactRatio(11, 10))
        self.assertEqual(fractional_ratio, timing_math.ExactRatio(11, 10))

    def test_candidate_baseline_ratio_preserves_above_binary64_one_nanosecond(self):
        baseline_value = (1 << 53) + 1
        candidate_value = baseline_value + 1
        result = timing_math.exact_candidate_baseline_ratio(
            timing_math.RationalNanoseconds.from_u64(candidate_value),
            timing_math.RationalNanoseconds.from_u64(baseline_value),
        )
        self.assertEqual(result, timing_math.ExactRatio(candidate_value, baseline_value))

    def test_median_of_three_order_is_permutation_invariant(self):
        ratios = (
            timing_math.ExactRatio(23, 20),
            timing_math.ExactRatio(1, 1),
            timing_math.ExactRatio(11, 10),
        )
        expected_order = (
            timing_math.ExactRatio(1, 1),
            timing_math.ExactRatio(11, 10),
            timing_math.ExactRatio(23, 20),
        )
        for permutation in itertools.permutations(ratios):
            with self.subTest(permutation=permutation):
                result = timing_math.median_of_three_ratios(permutation)
                self.assertEqual(result.ordered_ratios, expected_order)
                self.assertEqual(result.median_ratio, timing_math.ExactRatio(11, 10))

    def test_median_ties_from_equal_reduced_fractions_are_exact(self):
        equal_a = timing_math.exact_candidate_baseline_ratio(
            timing_math.RationalNanoseconds.from_u64(11),
            timing_math.RationalNanoseconds.from_u64(10),
        )
        equal_b = timing_math.exact_candidate_baseline_ratio(
            timing_math.RationalNanoseconds.from_u64(22),
            timing_math.RationalNanoseconds.from_u64(20),
        )
        higher = timing_math.ExactRatio(6, 5)
        for permutation in itertools.permutations((equal_a, equal_b, higher)):
            with self.subTest(permutation=permutation):
                result = timing_math.median_of_three_ratios(permutation)
                self.assertEqual(result.ordered_ratios, (equal_a, equal_a, higher))
                self.assertEqual(result.median_ratio, equal_a)

    def test_median_above_binary64_values_uses_exact_cross_multiplication(self):
        base = (1 << 53) + 1
        ratios = tuple(
            timing_math.exact_candidate_baseline_ratio(
                timing_math.RationalNanoseconds.from_u64(base + offset),
                timing_math.RationalNanoseconds.from_u64(base),
            )
            for offset in (3, 1, 2)
        )
        result = timing_math.median_of_three_ratios(ratios)
        self.assertEqual(result.median_ratio, timing_math.ExactRatio(base + 2, base))

    def test_median_and_exact_ratio_reject_forged_or_zero_baseline_inputs(self):
        zero = timing_math.RationalNanoseconds.from_u64(0)
        one = timing_math.RationalNanoseconds.from_u64(1)
        with self.assertRaisesRegex(timing_math.TimingMathError, "baseline must be positive"):
            timing_math.exact_candidate_baseline_ratio(one, zero)

        forged_rational = object.__new__(timing_math.RationalNanoseconds)
        object.__setattr__(forged_rational, "numerator", 1)
        object.__setattr__(forged_rational, "denominator", 0)
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.exact_candidate_baseline_ratio(one, forged_rational)

        mutated_rational = timing_math.RationalNanoseconds.from_u64(1)
        object.__setattr__(mutated_rational, "denominator", 0)
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.ratio_gate(mutated_rational, one, 11, 10)

        forged_ratio = object.__new__(timing_math.ExactRatio)
        object.__setattr__(forged_ratio, "numerator", 2)
        object.__setattr__(forged_ratio, "denominator", 2)
        with self.assertRaisesRegex(timing_math.TimingMathError, "forged"):
            timing_math.median_of_three_ratios((timing_math.ExactRatio(1, 1), forged_ratio, timing_math.ExactRatio(2, 1)))

    def test_median_requires_exactly_three_canonical_ratios(self):
        one = timing_math.ExactRatio(1, 1)
        invalid = ((), (one,), (one, one), (one, one, one, one), [one, one, 1], "1,1,1")
        for ratios in invalid:
            with self.subTest(ratios=ratios):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.median_of_three_ratios(ratios)

    def test_median_limit_equality_passes_and_one_nanosecond_above_fails(self):
        baseline = timing_math.RationalNanoseconds.from_u64(10)
        below = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(10), 11, 10)
        equality = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(11), 11, 10)
        above = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(12), 11, 10)
        self.assertTrue(below.passed)
        self.assertTrue(equality.passed)
        self.assertEqual(equality.left_cross_product, equality.right_cross_product)
        self.assertFalse(above.passed)

    def test_p95_limit_equality_passes_and_one_nanosecond_above_fails(self):
        baseline = timing_math.RationalNanoseconds.from_u64(20)
        below = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(22), 23, 20)
        equality = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(23), 23, 20)
        above = timing_math.ratio_gate(baseline, timing_math.RationalNanoseconds.from_u64(24), 23, 20)
        self.assertTrue(below.passed)
        self.assertTrue(equality.passed)
        self.assertEqual(equality.left_cross_product, equality.right_cross_product)
        self.assertFalse(above.passed)

    def test_above_binary64_boundary_one_nanosecond_failure_is_not_rounded_away(self):
        scale = (1 << 53) + 1
        baseline = timing_math.RationalNanoseconds.from_u64(10 * scale)
        equality = timing_math.RationalNanoseconds.from_u64(11 * scale)
        one_ns_above = timing_math.RationalNanoseconds.from_u64(11 * scale + 1)
        self.assertTrue(timing_math.ratio_gate(baseline, equality, 11, 10).passed)
        self.assertFalse(timing_math.ratio_gate(baseline, one_ns_above, 11, 10).passed)

    def test_fractional_quantiles_are_compared_without_rounding(self):
        baseline = timing_math.linear_quantile_ns([0, 1], 1, 2).value_ns
        candidate_equal = timing_math.RationalNanoseconds(11, 20)
        candidate_above = timing_math.RationalNanoseconds(111, 200)
        self.assertTrue(timing_math.ratio_gate(baseline, candidate_equal, 11, 10).passed)
        self.assertFalse(timing_math.ratio_gate(baseline, candidate_above, 11, 10).passed)

    def test_cross_products_wider_than_u64_remain_exact_in_retained_evidence(self):
        baseline = timing_math.RationalNanoseconds.from_u64(timing_math.U64_MAX - 1)
        candidate = timing_math.RationalNanoseconds.from_u64(timing_math.U64_MAX)
        result = timing_math.ratio_gate(baseline, candidate, 23, 20)
        self.assertGreater(result.left_cross_product, timing_math.U64_MAX)
        self.assertGreater(result.right_cross_product, timing_math.U64_MAX)
        self.assertEqual(result.left_cross_product, timing_math.U64_MAX * 20)
        self.assertEqual(result.right_cross_product, (timing_math.U64_MAX - 1) * 23)
        self.assertTrue(result.passed)
        assert_integer_tree(self, result)

    def test_zero_candidate_passes_but_zero_baseline_is_rejected(self):
        zero = timing_math.RationalNanoseconds.from_u64(0)
        one = timing_math.RationalNanoseconds.from_u64(1)
        self.assertTrue(timing_math.ratio_gate(one, zero, 11, 10).passed)
        with self.assertRaisesRegex(timing_math.TimingMathError, "baseline must be positive"):
            timing_math.ratio_gate(zero, one, 11, 10)

    def test_noncanonical_unapproved_and_invalid_ratio_limits_are_rejected(self):
        baseline = timing_math.RationalNanoseconds.from_u64(10)
        candidate = timing_math.RationalNanoseconds.from_u64(11)
        invalid = ((22, 20), (12, 10), (1, 1), (0, 1), (11, 0), (-11, 10), (True, 10), (11, False))
        for numerator, denominator in invalid:
            with self.subTest(pair=(numerator, denominator)):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.ratio_gate(baseline, candidate, numerator, denominator)

    def test_ratio_operands_must_be_canonical_rational_nanoseconds(self):
        one = timing_math.RationalNanoseconds.from_u64(1)
        for baseline, candidate in ((1, one), (one, 1), (True, one), (one, False)):
            with self.subTest(types=(type(baseline).__name__, type(candidate).__name__)):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.ratio_gate(baseline, candidate, 11, 10)

    def test_invalid_rational_evidence_is_rejected(self):
        invalid = (
            (2, 2),
            (0, 2),
            (-1, 1),
            (1, 0),
            (True, 1),
            (1, True),
            (timing_math.U64_MAX + 1, 1),
            (1, timing_math.U64_MAX + 1),
        )
        for numerator, denominator in invalid:
            with self.subTest(value=(numerator, denominator)):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.RationalNanoseconds(numerator, denominator)

    def test_invalid_exact_ratio_evidence_is_rejected(self):
        invalid = (
            (2, 2),
            (0, 2),
            (-1, 1),
            (1, 0),
            (True, 1),
            (1, True),
            (timing_math.MAX_EXACT_RATIO_COMPONENT + 1, 1),
            (1, timing_math.MAX_EXACT_RATIO_COMPONENT + 1),
        )
        for numerator, denominator in invalid:
            with self.subTest(value=(numerator, denominator)):
                with self.assertRaises(timing_math.TimingMathError):
                    timing_math.ExactRatio(numerator, denominator)

    def test_forged_ratio_evidence_is_rejected(self):
        baseline = timing_math.RationalNanoseconds.from_u64(10)
        candidate = timing_math.RationalNanoseconds.from_u64(11)
        correct = timing_math.ratio_gate(baseline, candidate, 11, 10)
        with self.assertRaisesRegex(timing_math.TimingMathError, "cross product"):
            timing_math.RatioGate(
                baseline,
                candidate,
                correct.candidate_over_baseline,
                11,
                10,
                correct.left_cross_product + 1,
                correct.right_cross_product,
                True,
            )
        with self.assertRaisesRegex(timing_math.TimingMathError, "decision"):
            timing_math.RatioGate(
                baseline,
                candidate,
                correct.candidate_over_baseline,
                11,
                10,
                correct.left_cross_product,
                correct.right_cross_product,
                False,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
