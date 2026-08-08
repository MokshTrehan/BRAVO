#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic tests for the frozen CP2-D sequence and trajectory mathematics."""

from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import sys
import unittest
from unittest import mock

import numpy as np


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_schema as schema  # noqa: E402
import cp2_sequence_math as sequence_math  # noqa: E402
import cp2_sequence_math_codec as math_codec  # noqa: E402


def rotation_z(radians):
    cosine = math.cos(radians)
    sine = math.sin(radians)
    return np.array(
        [[cosine, -sine, 0.0], [sine, cosine, 0.0], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )


def stored_z_quaternion(radians):
    return np.array([0.0, 0.0, math.sin(radians / 2.0), math.cos(radians / 2.0)], dtype=np.float64)


def noncollinear_positions():
    return np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [1.0, 1.0, 0.0],
            [0.25, 0.75, 0.5],
        ],
        dtype=np.float64,
    )


class TimestampPopulationTests(unittest.TestCase):
    def test_exact_decimal_conversion_intersection_tie_and_reuse(self):
        nullspace = [
            schema.decimal_seconds_to_ns(value)
            for value in ("0.990000000", "1.000000000", "1.000000001", "1.020000000")
        ]
        schur = [
            schema.decimal_seconds_to_ns(value)
            for value in ("0.980000000", "1.000000000", "1.000000001", "1.030000000")
        ]
        shared = sequence_math.shared_timestamp_intersection(nullspace, schur)
        self.assertEqual(shared, (1_000_000_000, 1_000_000_001))
        self.assertEqual(
            sequence_math.validate_shared_timestamp_intersection(nullspace, schur, shared),
            shared,
        )

        ground_truth = [
            schema.decimal_seconds_to_ns(value)
            for value in ("0.990000000", "1.010000000")
        ]
        associations = sequence_math.associate_nearest_ground_truth(shared, ground_truth)
        self.assertEqual(len(associations), 2)
        # The first row is an exact 10 ms tie and must select lower GT index 0.
        self.assertEqual(associations[0].ground_truth_index, 0)
        self.assertEqual(associations[0].absolute_difference_ns, 10_000_000)
        self.assertEqual(associations[1].ground_truth_index, 1)

        reused = sequence_math.associate_nearest_ground_truth(shared, [1_000_000_000])
        self.assertEqual([row.ground_truth_index for row in reused], [0, 0])

        duplicate_ground_truth = sequence_math.associate_nearest_ground_truth(
            [1_000_000_000],
            [1_000_000_000, 1_000_000_000],
        )
        self.assertEqual(duplicate_ground_truth[0].ground_truth_index, 0)

    def test_inclusive_10ms_boundary_and_outside_omission(self):
        estimator = [989_999_999, 990_000_000, 1_010_000_000, 1_010_000_001]
        associations = sequence_math.associate_nearest_ground_truth(estimator, [1_000_000_000])
        self.assertEqual([row.estimator_index for row in associations], [1, 2])
        self.assertEqual([row.absolute_difference_ns for row in associations], [10_000_000, 10_000_000])

    def test_wrong_tie_and_wrong_shared_population_are_rejected(self):
        estimator = [1_000_000_000]
        ground_truth = [990_000_000, 1_010_000_000]
        correct = sequence_math.associate_nearest_ground_truth(estimator, ground_truth)
        wrong = (
            sequence_math.TimestampAssociation(
                estimator_index=0,
                estimator_timestamp_ns=1_000_000_000,
                ground_truth_index=1,
                ground_truth_timestamp_ns=1_010_000_000,
                absolute_difference_ns=10_000_000,
            ),
        )
        self.assertEqual(correct[0].ground_truth_index, 0)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_ground_truth_associations(estimator, ground_truth, wrong)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_shared_timestamp_intersection([1, 2, 3], [2, 3, 4], [2])

    def test_float_decimal_duplicate_and_unordered_timestamps_are_rejected(self):
        for values in ([1.0], ["1.0"], [True], [-1], [sequence_math.U64_MAX + 1], [1, 1], [2, 1]):
            with self.subTest(values=values):
                with self.assertRaises(sequence_math.SequenceMathError):
                    sequence_math.shared_timestamp_intersection(values, [3])


class RankAndAlignmentTests(unittest.TestCase):
    def test_strict_rank_boundary_rejects_equality_and_accepts_next_binary64(self):
        largest = np.float64(1.0)
        threshold = np.float64(np.float64(3.0 * np.finfo(np.float64).eps) * largest)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_source_singular_values([largest, threshold, 0.0], 3)
        accepted = np.nextafter(threshold, np.float64(math.inf))
        values, retained_threshold = sequence_math.validate_source_singular_values(
            [largest, accepted, 0.0], 3
        )
        self.assertEqual(values[1], accepted)
        self.assertEqual(retained_threshold, threshold)

        cross_threshold = np.float64(
            np.float64(3.0 * np.finfo(np.float64).eps) * largest
        )
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_cross_covariance_singular_values(
                [largest, cross_threshold, 0.0], 3, False
            )
        cross_accepted = np.nextafter(cross_threshold, np.float64(math.inf))
        cross_values, retained_cross_threshold = (
            sequence_math.validate_cross_covariance_singular_values(
                [largest, cross_accepted, 0.0], 3, False
            )
        )
        self.assertEqual(cross_values[1], cross_accepted)
        self.assertEqual(retained_cross_threshold, cross_threshold)

        repeated_smallest = np.float64(0.5)
        at_gap_boundary = np.float64(repeated_smallest - cross_threshold)
        self.assertEqual(
            np.float64(repeated_smallest - at_gap_boundary), cross_threshold
        )
        with self.assertRaisesRegex(
            sequence_math.SequenceMathError, "unique smallest singular direction"
        ):
            sequence_math.validate_cross_covariance_singular_values(
                [largest, repeated_smallest, at_gap_boundary], 3, True
            )
        accepted_smallest = np.nextafter(
            at_gap_boundary, np.float64(-math.inf)
        )
        reflected_values, reflected_threshold = (
            sequence_math.validate_cross_covariance_singular_values(
                [largest, repeated_smallest, accepted_smallest], 3, True
            )
        )
        self.assertEqual(reflected_values[2], accepted_smallest)
        self.assertEqual(reflected_threshold, cross_threshold)

    def test_fewer_than_three_collinear_and_coincident_populations_fail(self):
        target = noncollinear_positions()
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.baseline_kabsch_alignment(target[:2], target[:2])
        collinear = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [2.0, 0.0, 0.0], [3.0, 0.0, 0.0]])
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.baseline_kabsch_alignment(collinear, collinear)
        coincident = np.ones((4, 3), dtype=np.float64)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.baseline_kabsch_alignment(coincident, coincident)

    def test_full_rank_source_with_degenerate_target_or_cross_covariance_fails(self):
        source = noncollinear_positions()
        coincident_target = np.tile(
            np.array([4.0, -2.0, 0.5], dtype=np.float64),
            (source.shape[0], 1),
        )
        with self.assertRaisesRegex(
            sequence_math.SequenceMathError, "cross-covariance"
        ):
            sequence_math.baseline_kabsch_alignment(source, coincident_target)

        rank_one_target = np.column_stack(
            (
                np.arange(source.shape[0], dtype=np.float64),
                np.zeros(source.shape[0], dtype=np.float64),
                np.zeros(source.shape[0], dtype=np.float64),
            )
        )
        with self.assertRaisesRegex(
            sequence_math.SequenceMathError, "unique proper rotation"
        ):
            sequence_math.baseline_kabsch_alignment(source, rank_one_target)

    def test_reflection_with_repeated_smallest_singular_values_is_nonunique(self):
        root_three = np.float64(math.sqrt(3.0))
        source = np.array(
            [
                [3.0, 0.0, 0.0],
                [-3.0, 0.0, 0.0],
                [0.0, root_three, 0.0],
                [0.0, -root_three, 0.0],
                [0.0, 0.0, root_three],
                [0.0, 0.0, -root_three],
            ],
            dtype=np.float64,
        )
        target = source.copy()
        target[:, 2] = -target[:, 2]
        identity_residual = target - source
        alternate_rotation = np.diag([1.0, -1.0, -1.0])
        alternate_residual = target - np.array(
            [alternate_rotation @ row for row in source], dtype=np.float64
        )
        self.assertEqual(
            np.sum(identity_residual * identity_residual, dtype=np.float64),
            np.sum(alternate_residual * alternate_residual, dtype=np.float64),
        )
        self.assertFalse(np.array_equal(np.eye(3), alternate_rotation))
        with self.assertRaisesRegex(
            sequence_math.SequenceMathError,
            "unique smallest singular direction",
        ):
            sequence_math.baseline_kabsch_alignment(source, target)

    def test_reflection_with_unique_smallest_singular_direction_is_accepted(self):
        source = np.array(
            [
                [3.0, 0.0, 0.0],
                [-3.0, 0.0, 0.0],
                [0.0, 2.0, 0.0],
                [0.0, -2.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, -1.0],
            ],
            dtype=np.float64,
        )
        target = np.array(
            [
                [-4.0, 2.0, -0.5],
                [2.0, 2.0, -0.5],
                [-1.0, 4.0, -0.5],
                [-1.0, 0.0, -0.5],
                [-1.0, 2.0, 0.5],
                [-1.0, 2.0, -1.5],
            ],
            dtype=np.float64,
        )
        alignment = sequence_math.baseline_kabsch_alignment(source, target)
        self.assertTrue(alignment.reflection_correction_applied)
        self.assertGreater(
            np.float64(
                alignment.cross_covariance_singular_values[1]
                - alignment.cross_covariance_singular_values[2]
            ),
            alignment.cross_covariance_rank_threshold,
        )
        np.testing.assert_allclose(
            alignment.rotation,
            np.diag([-1.0, 1.0, -1.0]),
            rtol=0.0,
            atol=1.0e-15,
        )

    def test_exact_so3_uniqueness_case_partition_accepts_remaining_cases(self):
        source = np.array(
            [
                [1.0, 0.0, 0.0],
                [-1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, -1.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, -1.0],
            ],
            dtype=np.float64,
        )
        positive_repeated = source @ np.diag([2.0, 1.0, 1.0])
        positive_alignment = sequence_math.baseline_kabsch_alignment(
            source, positive_repeated
        )
        self.assertFalse(positive_alignment.reflection_correction_applied)
        np.testing.assert_array_equal(positive_alignment.rotation, np.eye(3))

        negative_largest_repeated = source @ np.diag([2.0, 2.0, -1.0])
        negative_alignment = sequence_math.baseline_kabsch_alignment(
            source, negative_largest_repeated
        )
        self.assertTrue(negative_alignment.reflection_correction_applied)
        self.assertGreater(
            np.float64(
                negative_alignment.cross_covariance_singular_values[1]
                - negative_alignment.cross_covariance_singular_values[2]
            ),
            negative_alignment.cross_covariance_rank_threshold,
        )
        np.testing.assert_array_equal(negative_alignment.rotation, np.eye(3))

    def test_rank_two_null_svd_sign_choice_does_not_change_rotation(self):
        source = np.array(
            [
                [1.0, 0.0, 0.0],
                [-1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, -1.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, -1.0],
            ],
            dtype=np.float64,
        )
        target = source @ np.diag([2.0, 1.0, 0.0])
        ordinary = sequence_math.baseline_kabsch_alignment(source, target)
        original_svd = sequence_math.np.linalg.svd

        def flip_only_cross_null_left_vector(values, *args, **kwargs):
            result = original_svd(values, *args, **kwargs)
            if values.shape == (3, 3) and kwargs.get("full_matrices") is True:
                left, singular_values, right_transpose = result
                left = left.copy()
                left[:, 2] = -left[:, 2]
                return left, singular_values, right_transpose
            return result

        with mock.patch.object(
            sequence_math.np.linalg,
            "svd",
            side_effect=flip_only_cross_null_left_vector,
        ):
            alternate = sequence_math.baseline_kabsch_alignment(source, target)
        self.assertIsNot(
            ordinary.reflection_correction_applied,
            alternate.reflection_correction_applied,
        )
        np.testing.assert_array_equal(ordinary.rotation, alternate.rotation)

    def test_cross_svd_determinant_branch_requires_an_orthogonal_sign(self):
        source = noncollinear_positions()
        target = source + np.array([1.0, -2.0, 0.5], dtype=np.float64)
        original_svd = sequence_math.np.linalg.svd

        def corrupt_only_cross_left_factor(values, *args, **kwargs):
            result = original_svd(values, *args, **kwargs)
            if values.shape == (3, 3) and kwargs.get("full_matrices") is True:
                left, singular_values, right_transpose = result
                left = left.copy()
                left[:, 2] *= np.float64(0.5)
                return left, singular_values, right_transpose
            return result

        with mock.patch.object(
            sequence_math.np.linalg,
            "svd",
            side_effect=corrupt_only_cross_left_factor,
        ), self.assertRaisesRegex(
            sequence_math.SequenceMathError, "finite orthogonal sign"
        ):
            sequence_math.baseline_kabsch_alignment(source, target)

    def test_kabsch_recovers_proper_transform_and_applies_same_bytes_to_both_modes(self):
        source = noncollinear_positions()
        expected_rotation = rotation_z(math.radians(31.0))
        expected_translation = np.array([2.0, -3.0, 0.75], dtype=np.float64)
        target = np.array(
            [expected_rotation @ point + expected_translation for point in source],
            dtype=np.float64,
        )
        alignment = sequence_math.baseline_kabsch_alignment(source, target)
        np.testing.assert_allclose(alignment.rotation, expected_rotation, rtol=0.0, atol=2.0e-15)
        np.testing.assert_allclose(alignment.translation, expected_translation, rtol=0.0, atol=2.0e-15)
        self.assertLessEqual(abs(float(alignment.determinant) - 1.0), 1.0e-10)
        self.assertLessEqual(float(alignment.orthogonality_error_frobenius), 1.0e-10)
        self.assertTrue(alignment.applied_identically_to_both_modes)

        schur_source = source.copy()
        schur_source[:, 0] += 0.001
        identity_quaternions = np.tile(np.array([0.0, 0.0, 0.0, 1.0]), (source.shape[0], 1))
        aligned = sequence_math.apply_common_alignment(
            alignment,
            source,
            identity_quaternions,
            schur_source,
            identity_quaternions,
        )
        self.assertFalse(aligned.nullspace_positions.flags.writeable)
        self.assertFalse(aligned.schur_inverse_rotations.flags.writeable)
        np.testing.assert_allclose(
            aligned.nullspace_inverse_rotations[0],
            expected_rotation,
            rtol=0.0,
            atol=2.0e-15,
        )
        np.testing.assert_allclose(aligned.nullspace_positions, target, rtol=0.0, atol=2.0e-15)
        expected_schur = np.array(
            [expected_rotation @ point + expected_translation for point in schur_source],
            dtype=np.float64,
        )
        np.testing.assert_allclose(aligned.schur_positions, expected_schur, rtol=0.0, atol=2.0e-15)
        np.testing.assert_allclose(
            sequence_math.orientation_differences_deg(
                aligned.nullspace_inverse_rotations,
                aligned.schur_inverse_rotations,
            ),
            np.zeros(source.shape[0]),
            rtol=0.0,
            # The contract evaluates acos(trace(.)) without an identity
            # shortcut.  A proper rotation with ~1e-15 orthogonality error can
            # therefore report about 1.2e-6 degrees against its identical
            # binary copy, which remains far below the 0.05-degree gate.
            atol=2.0e-6,
        )

    def test_independent_alignment_is_rejected(self):
        source = noncollinear_positions()
        target = source + np.array([1.0, 2.0, 3.0])
        alignment = sequence_math.baseline_kabsch_alignment(source, target)
        independently_shifted = replace(
            alignment,
            translation=alignment.translation + np.array([0.001, 0.0, 0.0]),
        )
        quaternions = np.tile(np.array([0.0, 0.0, 0.0, 1.0]), (source.shape[0], 1))
        with self.assertRaisesRegex(sequence_math.SequenceMathError, "independent mode alignment"):
            sequence_math.apply_common_alignment(
                alignment,
                source,
                quaternions,
                source,
                quaternions,
                candidate_alignment=independently_shifted,
            )

    def test_common_alignment_payload_binds_cross_spectrum_and_reflection_branch(self):
        source = np.array(
            [
                [3.0, 0.0, 0.0],
                [-3.0, 0.0, 0.0],
                [0.0, 2.0, 0.0],
                [0.0, -2.0, 0.0],
                [0.0, 0.0, 1.0],
                [0.0, 0.0, -1.0],
            ],
            dtype=np.float64,
        )
        target = source + np.array([1.0, -2.0, 0.5], dtype=np.float64)
        alignment = sequence_math.baseline_kabsch_alignment(source, target)
        self.assertFalse(alignment.reflection_correction_applied)
        quaternions = np.tile(
            np.array([0.0, 0.0, 0.0, 1.0], dtype=np.float64),
            (source.shape[0], 1),
        )
        changed_spectrum = alignment.cross_covariance_singular_values.copy()
        changed_spectrum[2] = np.float64(changed_spectrum[2] * np.float64(0.5))
        changed_spectrum_alignment = replace(
            alignment,
            cross_covariance_singular_values=changed_spectrum,
        )
        changed_branch_alignment = replace(
            alignment, reflection_correction_applied=True
        )
        for candidate in (changed_spectrum_alignment, changed_branch_alignment):
            with self.subTest(candidate=candidate), self.assertRaisesRegex(
                sequence_math.SequenceMathError, "independent mode alignment"
            ):
                sequence_math.apply_common_alignment(
                    alignment,
                    source,
                    quaternions,
                    source,
                    quaternions,
                    candidate_alignment=candidate,
                )

    def test_reflection_and_nonorthogonal_rotation_are_rejected(self):
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_proper_rotation(np.diag([1.0, 1.0, -1.0]))
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_proper_rotation(np.diag([1.0, 1.0, 1.000001]))


class QuaternionConventionTests(unittest.TestCase):
    def test_nonidentity_stored_jpl_bytes_are_hamilton_inverse_rotation(self):
        stored = stored_z_quaternion(math.pi / 2.0)
        inverse_rotation = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(stored)
        expected_inverse = rotation_z(math.pi / 2.0)
        np.testing.assert_allclose(inverse_rotation, expected_inverse, rtol=0.0, atol=2.0e-15)
        # A conjugation error would map +x to -y.  The frozen interpretation
        # copies stored xyzw directly and therefore maps +x to +y in R_ItoG.
        np.testing.assert_allclose(
            inverse_rotation @ np.array([1.0, 0.0, 0.0]),
            np.array([0.0, 1.0, 0.0]),
            rtol=0.0,
            atol=2.0e-15,
        )
        np.testing.assert_allclose(inverse_rotation.T, rotation_z(-math.pi / 2.0), rtol=0.0, atol=2.0e-15)

    def test_quaternion_norm_boundary_is_inclusive_without_repair_beyond_it(self):
        self.assertEqual(
            float(sequence_math.QUATERNION_NORM_TOLERANCE),
            math_codec.QUATERNION_NORM_TOLERANCE,
        )
        lower = np.float64(1.0) - sequence_math.QUATERNION_NORM_TOLERANCE
        upper = np.float64(1.0) + sequence_math.QUATERNION_NORM_TOLERANCE
        sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation([0.0, 0.0, 0.0, lower])
        sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation([0.0, 0.0, 0.0, upper])
        math_codec.validate_stored_quaternion(
            [0.0, 0.0, 0.0, float(lower)], "synthetic stored quaternion"
        )
        math_codec.validate_stored_quaternion(
            [0.0, 0.0, 0.0, float(upper)], "synthetic stored quaternion"
        )
        below = np.nextafter(lower, np.float64(-math.inf))
        above = np.nextafter(upper, np.float64(math.inf))
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation([0.0, 0.0, 0.0, below])
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation([0.0, 0.0, 0.0, above])
        with self.assertRaises(math_codec.SequenceMathCodecError):
            math_codec.validate_stored_quaternion(
                [0.0, 0.0, 0.0, float(below)], "synthetic stored quaternion"
            )
        with self.assertRaises(math_codec.SequenceMathCodecError):
            math_codec.validate_stored_quaternion(
                [0.0, 0.0, 0.0, float(above)], "synthetic stored quaternion"
            )

    def test_bad_quaternions_fail_closed(self):
        bad_values = (
            [0.0, 0.0, 0.0, 0.0],
            [0.0, 0.0, 0.0, 2.0],
            [0.0, 0.0, 0.0, float("nan")],
            [0.0, 0.0, 1.0],
            ["0", "0", "0", "1"],
        )
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(sequence_math.SequenceMathError):
                    sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(value)


class MetricTests(unittest.TestCase):
    def test_position_and_orientation_differences_follow_frozen_formulas(self):
        nullspace_positions = np.zeros((3, 3), dtype=np.float64)
        schur_positions = np.array([[0.003, 0.004, 0.0], [0.0, 0.0, 0.01], [0.0, 0.0, 0.0]])
        np.testing.assert_allclose(
            sequence_math.position_differences_m(nullspace_positions, schur_positions),
            np.array([0.005, 0.01, 0.0]),
            rtol=0.0,
            atol=1.0e-17,
        )

        identity = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation([0.0, 0.0, 0.0, 1.0])
        one_degree = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(
            stored_z_quaternion(math.radians(1.0))
        )
        orientation = sequence_math.orientation_differences_deg(
            np.stack([identity, identity]),
            np.stack([one_degree, one_degree]),
        )
        np.testing.assert_allclose(orientation, np.array([1.0, 1.0]), rtol=0.0, atol=1.0e-12)

    def test_linear_p95_uses_the_contracts_frozen_binary64_evaluation_order(self):
        values = np.array([9.0, 1.0, 7.0, 3.0, 5.0], dtype=np.float64)
        # Independent known answer for sorted [1,3,5,7,9], h=3.8, using the
        # contract's exact x_lo+(h-lo)*(x_hi-x_lo) binary64 evaluation order.
        expected = np.float64(float.fromhex("0x1.1333333333333p+3"))
        self.assertEqual(sequence_math.linear_p95(values).tobytes(), expected.tobytes())
        self.assertEqual(sequence_math.numpy_linear_quantile([10.0], 0.95), 10.0)

        # A cancellation-avoiding algebraic rearrangement differs by three
        # binary64 ULPs here.  The schema's explicit
        # x_lo+(h-lo)*(x_hi-x_lo) order is normative and must be retained.
        boundary_values = np.array([-1.3266232365821902, 1.1116091529085521])
        h = np.float64(0.95)
        frozen = np.float64(
            boundary_values[0]
            + np.float64(h * np.float64(boundary_values[1] - boundary_values[0]))
        )
        observed = sequence_math.linear_p95(boundary_values)
        self.assertEqual(observed.tobytes(), frozen.tobytes())
        rearranged = np.float64(
            boundary_values[1]
            - np.float64(
                np.float64(np.float64(1.0) - h)
                * np.float64(boundary_values[1] - boundary_values[0])
            )
        )
        self.assertNotEqual(observed.tobytes(), rearranged.tobytes())

    def test_already_aligned_translation_rmse_and_relative_ate(self):
        ground_truth = noncollinear_positions()
        baseline = ground_truth + np.array([3.0, 4.0, 0.0])
        candidate = ground_truth + np.array([3.03, 4.04, 0.0])
        baseline_ate = sequence_math.translation_rmse_m(baseline, ground_truth)
        candidate_ate = sequence_math.translation_rmse_m(candidate, ground_truth)
        self.assertEqual(baseline_ate, 5.0)
        self.assertAlmostEqual(candidate_ate, 5.05, places=14)
        self.assertAlmostEqual(
            sequence_math.relative_ate_difference(baseline_ate, candidate_ate),
            0.01,
            places=14,
        )

    def test_zero_baseline_ate_is_invalid(self):
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.relative_ate_difference(0.0, 0.0)

    def test_all_three_metric_boundaries_are_inclusive_and_next_binary64_fails(self):
        limits = (
            sequence_math.POSITION_P95_LIMIT_M,
            sequence_math.ORIENTATION_P95_LIMIT_DEG,
            sequence_math.RELATIVE_ATE_LIMIT,
        )
        sequence_math.validate_metric_limits(*limits)
        for index, limit in enumerate(limits):
            values = list(limits)
            values[index] = np.nextafter(limit, np.float64(math.inf))
            with self.subTest(metric=index):
                with self.assertRaises(sequence_math.SequenceMathError):
                    sequence_math.validate_metric_limits(*values)

    def test_nonfinite_negative_and_empty_metric_populations_fail(self):
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.position_differences_m(np.empty((0, 3)), np.empty((0, 3)))
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.numpy_linear_quantile([], 0.95)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_metric_limits(float("nan"), 0.0, 0.0)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_metric_limits(-1.0, 0.0, 0.0)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.validate_metric_limits("0.0", 0.0, 0.0)
        with self.assertRaises(sequence_math.SequenceMathError):
            sequence_math.relative_ate_difference("1.0", 1.0)


if __name__ == "__main__":
    unittest.main()
