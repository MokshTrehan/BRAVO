#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed CP2-D sequence-association and trajectory math.

All timestamps entering this module are integer nanoseconds.  In particular,
ground-truth decimal seconds must first be converted without binary64 rounding
by :func:`cp2_schema.decimal_seconds_to_ns`.  This module intentionally rejects
decimal strings and floating-point timestamps.

The implementation follows the frozen CP2-D evaluation order.  Ordered sums
are explicit binary64 scalar additions; SVD and matrix products use the
imported NumPy/native numerical stack.  CP2-D actual evidence remains blocked
until that complete stack is version- and byte-bound by an approved evaluator/
direct-math profile.  No function reads project files, registries, bags, or
trajectories.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
import math
from typing import Any, Iterable, Optional, Sequence, Tuple

import numpy as np


U64_MAX = (1 << 64) - 1
EXACT_BINARY64_INTEGER_MAX = (1 << 53) - 1
GROUND_TRUTH_MAX_DIFFERENCE_NS = 10_000_000
# The frozen OpenVINS EuRoC ground-truth transport contains linearly
# interpolated, decimal-serialized quaternions.  They are rotations only after
# normalization, so this is an input-admissibility guard rather than a metric
# tolerance.  The exact inclusive boundary is shared with the stdlib codec and
# detached verifier.
QUATERNION_NORM_TOLERANCE = np.float64(1.0e-3)
ROTATION_VALIDATION_TOLERANCE = np.float64(1.0e-10)
POSITION_P95_LIMIT_M = np.float64(0.01)
ORIENTATION_P95_LIMIT_DEG = np.float64(0.05)
RELATIVE_ATE_LIMIT = np.float64(0.01)


class SequenceMathError(ValueError):
    """Raised when a CP2-D mathematical precondition or gate fails."""


@dataclass(frozen=True)
class TimestampAssociation:
    estimator_index: int
    estimator_timestamp_ns: int
    ground_truth_index: int
    ground_truth_timestamp_ns: int
    absolute_difference_ns: int


@dataclass(frozen=True)
class SE3Alignment:
    """One baseline-derived transform retained and reused for both modes."""

    rotation: np.ndarray
    translation: np.ndarray
    source_count: int
    source_singular_values: np.ndarray
    source_rank_threshold: np.float64
    cross_covariance_singular_values: np.ndarray
    cross_covariance_rank_threshold: np.float64
    reflection_correction_applied: bool
    determinant: np.float64
    orthogonality_error_frobenius: np.float64
    applied_identically_to_both_modes: bool = True

    def __post_init__(self) -> None:
        rotation = _as_f64_matrix(self.rotation, 3, "alignment rotation", exact_rows=3)
        translation = _as_f64_vector(self.translation, 3, "alignment translation")
        determinant, orthogonality_error = validate_proper_rotation(rotation)
        singular_values, rank_threshold = validate_source_singular_values(
            self.source_singular_values, self.source_count
        )
        cross_singular_values, cross_rank_threshold = (
            validate_cross_covariance_singular_values(
                self.cross_covariance_singular_values,
                self.source_count,
                self.reflection_correction_applied,
            )
        )
        if not _same_f64(self.source_rank_threshold, rank_threshold):
            raise SequenceMathError("alignment source-rank threshold does not match its frozen spectrum")
        if not _same_f64(self.cross_covariance_rank_threshold, cross_rank_threshold):
            raise SequenceMathError(
                "alignment cross-covariance-rank threshold does not match its frozen spectrum"
            )
        if type(self.reflection_correction_applied) is not bool:
            raise SequenceMathError(
                "alignment reflection-correction flag is not Boolean"
            )
        if not _same_f64(self.determinant, determinant):
            raise SequenceMathError("alignment determinant does not match its rotation")
        if not _same_f64(self.orthogonality_error_frobenius, orthogonality_error):
            raise SequenceMathError("alignment orthogonality error does not match its rotation")
        if self.applied_identically_to_both_modes is not True:
            raise SequenceMathError("alignment is not marked as applied identically to both modes")

        rotation.setflags(write=False)
        translation.setflags(write=False)
        singular_values.setflags(write=False)
        cross_singular_values.setflags(write=False)
        object.__setattr__(self, "rotation", rotation)
        object.__setattr__(self, "translation", translation)
        object.__setattr__(self, "source_count", _positive_integer(self.source_count, "alignment source count"))
        object.__setattr__(self, "source_singular_values", singular_values)
        object.__setattr__(self, "source_rank_threshold", rank_threshold)
        object.__setattr__(
            self, "cross_covariance_singular_values", cross_singular_values
        )
        object.__setattr__(
            self, "cross_covariance_rank_threshold", cross_rank_threshold
        )
        object.__setattr__(self, "determinant", determinant)
        object.__setattr__(self, "orthogonality_error_frobenius", orthogonality_error)


@dataclass(frozen=True)
class AlignedModePair:
    nullspace_positions: np.ndarray
    schur_positions: np.ndarray
    nullspace_inverse_rotations: np.ndarray
    schur_inverse_rotations: np.ndarray

    def __post_init__(self) -> None:
        arrays = (
            self.nullspace_positions,
            self.schur_positions,
            self.nullspace_inverse_rotations,
            self.schur_inverse_rotations,
        )
        copies = tuple(np.ascontiguousarray(value, dtype=np.float64).copy() for value in arrays)
        if (
            copies[0].ndim != 2
            or copies[0].shape[1:] != (3,)
            or copies[1].shape != copies[0].shape
            or copies[2].shape != (copies[0].shape[0], 3, 3)
            or copies[3].shape != copies[2].shape
            or not all(np.all(np.isfinite(value)) for value in copies)
        ):
            raise SequenceMathError("aligned mode-pair arrays are incomplete or nonfinite")
        for population in (copies[2], copies[3]):
            for index in range(population.shape[0]):
                validate_proper_rotation(population[index])
        for value in copies:
            value.setflags(write=False)
        object.__setattr__(self, "nullspace_positions", copies[0])
        object.__setattr__(self, "schur_positions", copies[1])
        object.__setattr__(self, "nullspace_inverse_rotations", copies[2])
        object.__setattr__(self, "schur_inverse_rotations", copies[3])


def _same_f64(left: Any, right: Any) -> bool:
    try:
        lhs = np.float64(left)
        rhs = np.float64(right)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SequenceMathError("value is not binary64") from exc
    return bool(np.isfinite(lhs) and np.isfinite(rhs) and lhs.tobytes() == rhs.tobytes())


def _f64_scalar(value: Any, label: str) -> np.float64:
    if isinstance(value, (bool, np.bool_)) or not isinstance(
        value, (int, float, np.integer, np.floating)
    ):
        raise SequenceMathError(label + " must be a finite binary64 number")
    try:
        converted = np.float64(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SequenceMathError(label + " must be a finite binary64 number") from exc
    if not np.isfinite(converted):
        raise SequenceMathError(label + " must be a finite binary64 number")
    return converted


def _positive_integer(value: Any, label: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise SequenceMathError(label + " must be a positive integer")
    converted = int(value)
    if converted <= 0:
        raise SequenceMathError(label + " must be a positive integer")
    return converted


def _u64(value: Any, label: str) -> int:
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, (int, np.integer)):
        raise SequenceMathError(label + " must be integer nanoseconds")
    converted = int(value)
    if converted < 0 or converted > U64_MAX:
        raise SequenceMathError(label + " is outside u64")
    return converted


def _timestamps(values: Iterable[Any], label: str) -> Tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise SequenceMathError(label + " must be integer-nanosecond rows")
    try:
        result = tuple(_u64(value, label + " timestamp") for value in values)
    except TypeError as exc:
        raise SequenceMathError(label + " must be iterable") from exc
    for index in range(1, len(result)):
        if result[index] <= result[index - 1]:
            raise SequenceMathError(label + " timestamps must be strictly increasing and unique")
    return result


def _ground_truth_timestamps(values: Iterable[Any]) -> Tuple[int, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise SequenceMathError("ground truth must be integer-nanosecond rows")
    try:
        result = tuple(_u64(value, "ground truth timestamp") for value in values)
    except TypeError as exc:
        raise SequenceMathError("ground truth must be iterable") from exc
    for index in range(1, len(result)):
        if result[index] < result[index - 1]:
            raise SequenceMathError("ground-truth timestamps must be nondecreasing")
    return result


def _numeric_array(value: Any, label: str) -> np.ndarray:
    try:
        unconverted = np.asarray(value)
    except (TypeError, ValueError) as exc:
        raise SequenceMathError(label + " is not an array") from exc
    if unconverted.dtype.kind not in ("i", "u", "f"):
        raise SequenceMathError(label + " must contain only real numeric values")
    try:
        converted = unconverted.astype(np.float64, copy=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SequenceMathError(label + " is not representable as binary64") from exc
    if not np.all(np.isfinite(converted)):
        raise SequenceMathError(label + " contains a nonfinite value")
    return converted


def _as_f64_matrix(value: Any, columns: int, label: str, exact_rows: Optional[int] = None) -> np.ndarray:
    converted = _numeric_array(value, label)
    if converted.ndim != 2 or converted.shape[1] != columns:
        raise SequenceMathError("{} must have shape (N,{})".format(label, columns))
    if exact_rows is not None and converted.shape[0] != exact_rows:
        raise SequenceMathError("{} must have exactly {} rows".format(label, exact_rows))
    return np.ascontiguousarray(converted, dtype=np.float64)


def _as_f64_vector(value: Any, length: int, label: str) -> np.ndarray:
    converted = _numeric_array(value, label)
    if converted.shape != (length,):
        raise SequenceMathError("{} must have shape ({},)".format(label, length))
    return np.ascontiguousarray(converted, dtype=np.float64)


def _ordered_centroid(points: np.ndarray) -> np.ndarray:
    count = points.shape[0]
    if count <= 0 or count > EXACT_BINARY64_INTEGER_MAX:
        raise SequenceMathError("centroid population count is outside the exact binary64 integer range")
    sums = np.zeros(3, dtype=np.float64)
    for row in range(count):
        for column in range(3):
            sums[column] = np.float64(sums[column] + points[row, column])
    divisor = np.float64(count)
    centroid = np.empty(3, dtype=np.float64)
    for column in range(3):
        centroid[column] = np.float64(sums[column] / divisor)
    if not np.all(np.isfinite(centroid)):
        raise SequenceMathError("ordered centroid is nonfinite")
    return centroid


def shared_timestamp_intersection(
    nullspace_timestamps_ns: Iterable[Any],
    schur_timestamps_ns: Iterable[Any],
) -> Tuple[int, ...]:
    """Return the increasing exact intersection of the two unique mode traces."""

    nullspace = _timestamps(nullspace_timestamps_ns, "nullspace trajectory")
    schur = _timestamps(schur_timestamps_ns, "schur trajectory")
    shared = []
    left = 0
    right = 0
    while left < len(nullspace) and right < len(schur):
        if nullspace[left] == schur[right]:
            shared.append(nullspace[left])
            left += 1
            right += 1
        elif nullspace[left] < schur[right]:
            left += 1
        else:
            right += 1
    return tuple(shared)


def validate_shared_timestamp_intersection(
    nullspace_timestamps_ns: Iterable[Any],
    schur_timestamps_ns: Iterable[Any],
    retained_shared_timestamps_ns: Iterable[Any],
) -> Tuple[int, ...]:
    expected = shared_timestamp_intersection(nullspace_timestamps_ns, schur_timestamps_ns)
    retained = _timestamps(retained_shared_timestamps_ns, "retained shared trajectory")
    if retained != expected:
        raise SequenceMathError("retained shared timestamp population is not the exact mode intersection")
    return retained


def associate_nearest_ground_truth(
    estimator_timestamps_ns: Iterable[Any],
    ground_truth_timestamps_ns: Iterable[Any],
) -> Tuple[TimestampAssociation, ...]:
    """Associate by integer-nanosecond distance, retaining the lower row tie.

    Ground-truth rows may be reused.  Estimator rows with no ground-truth row
    at or below the inclusive maximum difference are omitted.
    """

    estimator = _timestamps(estimator_timestamps_ns, "estimator shared")
    ground_truth = _ground_truth_timestamps(ground_truth_timestamps_ns)
    maximum = GROUND_TRUTH_MAX_DIFFERENCE_NS
    if not ground_truth and estimator:
        raise SequenceMathError("ground-truth timestamp population is empty")

    associations = []
    for estimator_index, timestamp in enumerate(estimator):
        insertion = bisect_left(ground_truth, timestamp)
        candidates = []
        if insertion > 0:
            # If duplicate lower timestamps exist, select their first row so
            # the lower-index tie rule remains exact.
            candidates.append(bisect_left(ground_truth, ground_truth[insertion - 1]))
        if insertion < len(ground_truth):
            candidates.append(insertion)
        best_index = -1
        best_difference = U64_MAX
        for ground_truth_index in candidates:
            difference = abs(timestamp - ground_truth[ground_truth_index])
            # Strict improvement deliberately retains the lower row index on a
            # tie because candidates are visited in increasing index order.
            if difference < best_difference:
                best_difference = difference
                best_index = ground_truth_index
        if best_index >= 0 and best_difference <= maximum:
            associations.append(
                TimestampAssociation(
                    estimator_index=estimator_index,
                    estimator_timestamp_ns=timestamp,
                    ground_truth_index=best_index,
                    ground_truth_timestamp_ns=ground_truth[best_index],
                    absolute_difference_ns=best_difference,
                )
            )
    return tuple(associations)


def validate_ground_truth_associations(
    estimator_timestamps_ns: Iterable[Any],
    ground_truth_timestamps_ns: Iterable[Any],
    retained: Sequence[TimestampAssociation],
) -> Tuple[TimestampAssociation, ...]:
    expected = associate_nearest_ground_truth(estimator_timestamps_ns, ground_truth_timestamps_ns)
    if not isinstance(retained, Sequence) or tuple(retained) != expected:
        raise SequenceMathError("retained ground-truth association differs from exact nearest-row selection")
    return expected


def validate_source_singular_values(singular_values: Any, population_count: Any) -> Tuple[np.ndarray, np.float64]:
    count = _positive_integer(population_count, "source population count")
    if count < 3:
        raise SequenceMathError("common alignment requires at least three poses")
    if count > EXACT_BINARY64_INTEGER_MAX:
        raise SequenceMathError("source population count exceeds the exact binary64 integer range")
    values = _as_f64_vector(singular_values, 3, "source singular values")
    if np.any(values < np.float64(0.0)) or values[0] < values[1] or values[1] < values[2]:
        raise SequenceMathError("source singular values are not a complete descending nonnegative spectrum")
    largest = values[0]
    if not largest > np.finfo(np.float64).tiny:
        raise SequenceMathError("source position spectrum is coincident or numerically zero")
    multiplier = np.float64(max(count, 3))
    threshold = np.float64(np.float64(multiplier * np.finfo(np.float64).eps) * largest)
    if not values[1] > threshold:
        raise SequenceMathError("source position spectrum is collinear at the strict rank boundary")
    return values, threshold


def validate_cross_covariance_singular_values(
    singular_values: Any,
    population_count: Any,
    reflection_correction_applied: Any,
) -> Tuple[np.ndarray, np.float64]:
    """Require enough cross-covariance rank for a unique proper 3-D rotation.

    Source non-collinearity alone is insufficient: a degenerate target can
    make the Kabsch cross-covariance rank zero or one and leave an arbitrary
    SVD-basis rotation.  Rank at least two is necessary and is sufficient when
    no reflection correction is required.  When a correction is required,
    the two smallest singular values must also be numerically distinct: if
    they coincide, the corrected axis is arbitrary within their repeated
    singular subspace.  The strict numerical threshold uses the common
    population count because every covariance element is an ordered sum of
    that many products; equality is rejected.
    """

    count = _positive_integer(population_count, "cross-covariance population count")
    if type(reflection_correction_applied) is not bool:
        raise SequenceMathError(
            "cross-covariance reflection-correction flag is not Boolean"
        )
    if count < 3:
        raise SequenceMathError("common alignment requires at least three poses")
    if count > EXACT_BINARY64_INTEGER_MAX:
        raise SequenceMathError(
            "cross-covariance population count exceeds the exact binary64 integer range"
        )
    values = _as_f64_vector(
        singular_values, 3, "cross-covariance singular values"
    )
    if (
        np.any(values < np.float64(0.0))
        or values[0] < values[1]
        or values[1] < values[2]
    ):
        raise SequenceMathError(
            "cross-covariance singular values are not a complete descending nonnegative spectrum"
        )
    largest = values[0]
    if not largest > np.finfo(np.float64).tiny:
        raise SequenceMathError("alignment cross-covariance is numerically zero")
    multiplier = np.float64(max(count, 3))
    threshold = np.float64(
        np.float64(multiplier * np.finfo(np.float64).eps) * largest
    )
    if not values[1] > threshold:
        raise SequenceMathError(
            "alignment cross-covariance does not define a unique proper rotation"
        )
    if reflection_correction_applied:
        smallest_gap = np.float64(values[1] - values[2])
        if not smallest_gap > threshold:
            raise SequenceMathError(
                "alignment reflection correction has no unique smallest singular direction"
            )
    return values, threshold


def _source_rank(points: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.float64]:
    count = points.shape[0]
    if count < 3:
        raise SequenceMathError("common alignment requires at least three poses")
    centroid = _ordered_centroid(points)
    centered = np.empty((3, count), dtype=np.float64)
    for row in range(count):
        for column in range(3):
            centered[column, row] = np.float64(points[row, column] - centroid[column])
    try:
        singular_values = np.linalg.svd(centered, full_matrices=False, compute_uv=False)
    except np.linalg.LinAlgError as exc:
        raise SequenceMathError("source position SVD failed") from exc
    values, threshold = validate_source_singular_values(singular_values, count)
    return centroid, centered, values, threshold


def _ordered_cross_covariance(
    source: np.ndarray,
    target: np.ndarray,
    source_centroid: np.ndarray,
    target_centroid: np.ndarray,
) -> np.ndarray:
    count = source.shape[0]
    sums = np.zeros((3, 3), dtype=np.float64)
    for row in range(count):
        for target_axis in range(3):
            target_delta = np.float64(target[row, target_axis] - target_centroid[target_axis])
            for source_axis in range(3):
                source_delta = np.float64(source[row, source_axis] - source_centroid[source_axis])
                product = np.float64(target_delta * source_delta)
                sums[target_axis, source_axis] = np.float64(sums[target_axis, source_axis] + product)
    divisor = np.float64(count)
    covariance = np.empty((3, 3), dtype=np.float64)
    for row in range(3):
        for column in range(3):
            covariance[row, column] = np.float64(sums[row, column] / divisor)
    if not np.all(np.isfinite(covariance)):
        raise SequenceMathError("alignment cross-covariance is nonfinite")
    return covariance


def validate_proper_rotation(rotation: Any) -> Tuple[np.float64, np.float64]:
    matrix = _as_f64_matrix(rotation, 3, "rotation", exact_rows=3)
    determinant = np.float64(np.linalg.det(matrix))
    gram = np.matmul(matrix.T, matrix, dtype=np.float64)
    squared_error = np.float64(0.0)
    for row in range(3):
        for column in range(3):
            identity = np.float64(1.0 if row == column else 0.0)
            delta = np.float64(gram[row, column] - identity)
            squared_error = np.float64(squared_error + np.float64(delta * delta))
    orthogonality_error = np.float64(math.sqrt(float(squared_error)))
    if not np.isfinite(determinant) or not np.isfinite(orthogonality_error):
        raise SequenceMathError("rotation validation produced a nonfinite result")
    if abs(np.float64(determinant - np.float64(1.0))) > ROTATION_VALIDATION_TOLERANCE:
        raise SequenceMathError("rotation determinant is not proper within 1e-10")
    if orthogonality_error > ROTATION_VALIDATION_TOLERANCE:
        raise SequenceMathError("rotation orthogonality error exceeds 1e-10")
    return determinant, orthogonality_error


def baseline_kabsch_alignment(nullspace_positions: Any, ground_truth_positions: Any) -> SE3Alignment:
    """Compute the sole nullspace-to-ground-truth proper SE(3) alignment."""

    source = _as_f64_matrix(nullspace_positions, 3, "nullspace positions")
    target = _as_f64_matrix(ground_truth_positions, 3, "ground-truth positions")
    if source.shape != target.shape:
        raise SequenceMathError("alignment source and target populations differ")
    source_centroid, _, source_singular_values, source_rank_threshold = _source_rank(source)
    target_centroid = _ordered_centroid(target)
    covariance = _ordered_cross_covariance(source, target, source_centroid, target_centroid)
    try:
        left, cross_singular_values_raw, right_transpose = np.linalg.svd(
            covariance, full_matrices=True
        )
    except np.linalg.LinAlgError as exc:
        raise SequenceMathError("alignment cross-covariance SVD failed") from exc
    if (
        left.shape != (3, 3)
        or right_transpose.shape != (3, 3)
        or not np.all(np.isfinite(left))
        or not np.all(np.isfinite(right_transpose))
    ):
        raise SequenceMathError("alignment SVD did not return complete finite factors")
    uv_transpose = np.matmul(left, right_transpose, dtype=np.float64)
    uv_determinant = np.float64(np.linalg.det(uv_transpose))
    if (
        not np.isfinite(uv_determinant)
        or abs(np.float64(abs(uv_determinant) - np.float64(1.0)))
        > ROTATION_VALIDATION_TOLERANCE
    ):
        raise SequenceMathError(
            "alignment determinant correction is not a finite orthogonal sign"
        )
    reflection_correction_applied = bool(uv_determinant < np.float64(0.0))
    cross_singular_values, cross_rank_threshold = (
        validate_cross_covariance_singular_values(
            cross_singular_values_raw,
            source.shape[0],
            reflection_correction_applied,
        )
    )
    correction = np.eye(3, dtype=np.float64)
    correction[2, 2] = np.float64(
        -1.0 if reflection_correction_applied else 1.0
    )
    # The multiplication is intentionally left-associated: R = (U D) V^T.
    rotation = np.matmul(np.matmul(left, correction, dtype=np.float64), right_transpose, dtype=np.float64)
    determinant, orthogonality_error = validate_proper_rotation(rotation)
    translated_centroid = np.matmul(rotation, source_centroid, dtype=np.float64)
    translation = np.empty(3, dtype=np.float64)
    for axis in range(3):
        translation[axis] = np.float64(target_centroid[axis] - translated_centroid[axis])
    if not np.all(np.isfinite(translation)):
        raise SequenceMathError("alignment translation is nonfinite")

    return SE3Alignment(
        rotation=rotation,
        translation=translation,
        source_count=source.shape[0],
        source_singular_values=source_singular_values,
        source_rank_threshold=source_rank_threshold,
        cross_covariance_singular_values=cross_singular_values,
        cross_covariance_rank_threshold=cross_rank_threshold,
        reflection_correction_applied=reflection_correction_applied,
        determinant=determinant,
        orthogonality_error_frobenius=orthogonality_error,
        applied_identically_to_both_modes=True,
    )


def jpl_stored_xyzw_to_hamilton_inverse_rotation(stored_xyzw: Any) -> np.ndarray:
    """Interpret stored JPL coefficients byte-order-preservingly as Hamilton R_ItoG.

    The four input components remain in their stored ``x,y,z,w`` positions;
    there is no conjugation or component reorder.  Normalization occurs only
    after the frozen norm gate, as required by the CP2-D contract.
    """

    quaternion = _as_f64_vector(stored_xyzw, 4, "stored JPL quaternion")
    squared_norm = np.float64(0.0)
    for component in quaternion:
        squared_norm = np.float64(squared_norm + np.float64(component * component))
    norm = np.float64(math.sqrt(float(squared_norm)))
    lower = np.float64(np.float64(1.0) - QUATERNION_NORM_TOLERANCE)
    upper = np.float64(np.float64(1.0) + QUATERNION_NORM_TOLERANCE)
    if not np.isfinite(norm) or norm < lower or norm > upper:
        raise SequenceMathError("stored quaternion norm is outside [1-1e-3,1+1e-3]")
    normalized = np.empty(4, dtype=np.float64)
    for index in range(4):
        normalized[index] = np.float64(quaternion[index] / norm)
    x, y, z, w = normalized

    xx = np.float64(x * x)
    yy = np.float64(y * y)
    zz = np.float64(z * z)
    xy = np.float64(x * y)
    xz = np.float64(x * z)
    yz = np.float64(y * z)
    wx = np.float64(w * x)
    wy = np.float64(w * y)
    wz = np.float64(w * z)
    two = np.float64(2.0)
    one = np.float64(1.0)
    rotation = np.array(
        (
            (
                np.float64(one - two * np.float64(yy + zz)),
                np.float64(two * np.float64(xy - wz)),
                np.float64(two * np.float64(xz + wy)),
            ),
            (
                np.float64(two * np.float64(xy + wz)),
                np.float64(one - two * np.float64(xx + zz)),
                np.float64(two * np.float64(yz - wx)),
            ),
            (
                np.float64(two * np.float64(xz - wy)),
                np.float64(two * np.float64(yz + wx)),
                np.float64(one - two * np.float64(xx + yy)),
            ),
        ),
        dtype=np.float64,
    )
    validate_proper_rotation(rotation)
    return rotation


def _quaternion_rows(value: Any, expected_count: int, label: str) -> np.ndarray:
    rows = _as_f64_matrix(value, 4, label)
    if rows.shape[0] != expected_count:
        raise SequenceMathError(label + " population differs from its positions")
    return rows


def _apply_alignment_to_mode(
    alignment: SE3Alignment,
    positions: Any,
    stored_quaternions_xyzw: Any,
    label: str,
) -> Tuple[np.ndarray, np.ndarray]:
    if not isinstance(alignment, SE3Alignment):
        raise SequenceMathError("common alignment has the wrong type")
    points = _as_f64_matrix(positions, 3, label + " positions")
    if points.shape[0] != alignment.source_count:
        raise SequenceMathError(label + " population differs from the baseline alignment population")
    quaternions = _quaternion_rows(stored_quaternions_xyzw, points.shape[0], label + " stored quaternions")
    aligned_positions = np.empty_like(points)
    aligned_inverse_rotations = np.empty((points.shape[0], 3, 3), dtype=np.float64)
    for index in range(points.shape[0]):
        rotated = np.matmul(alignment.rotation, points[index], dtype=np.float64)
        for axis in range(3):
            aligned_positions[index, axis] = np.float64(rotated[axis] + alignment.translation[axis])
        inverse_rotation = jpl_stored_xyzw_to_hamilton_inverse_rotation(quaternions[index])
        # R_ItoG' = R_alignment R_ItoG, equivalent to the frozen
        # R_G'toI = R_GtoI R_alignment^T convention.
        aligned_inverse_rotations[index] = np.matmul(alignment.rotation, inverse_rotation, dtype=np.float64)
        validate_proper_rotation(aligned_inverse_rotations[index])
    return aligned_positions, aligned_inverse_rotations


def _alignment_payload_equal(left: SE3Alignment, right: SE3Alignment) -> bool:
    if not isinstance(left, SE3Alignment) or not isinstance(right, SE3Alignment):
        return False
    return (
        left.rotation.tobytes(order="C") == right.rotation.tobytes(order="C")
        and left.translation.tobytes(order="C") == right.translation.tobytes(order="C")
        and left.source_count == right.source_count
        and left.source_singular_values.tobytes(order="C") == right.source_singular_values.tobytes(order="C")
        and _same_f64(left.source_rank_threshold, right.source_rank_threshold)
        and left.cross_covariance_singular_values.tobytes(order="C")
        == right.cross_covariance_singular_values.tobytes(order="C")
        and _same_f64(
            left.cross_covariance_rank_threshold,
            right.cross_covariance_rank_threshold,
        )
        and left.reflection_correction_applied
        is right.reflection_correction_applied
        and _same_f64(left.determinant, right.determinant)
        and _same_f64(left.orthogonality_error_frobenius, right.orthogonality_error_frobenius)
        and left.applied_identically_to_both_modes is True
        and right.applied_identically_to_both_modes is True
    )


def apply_common_alignment(
    alignment: SE3Alignment,
    nullspace_positions: Any,
    nullspace_stored_quaternions_xyzw: Any,
    schur_positions: Any,
    schur_stored_quaternions_xyzw: Any,
    *,
    candidate_alignment: Optional[SE3Alignment] = None,
) -> AlignedModePair:
    """Apply one baseline alignment to both modes and reject a second transform."""

    if candidate_alignment is not None and not _alignment_payload_equal(alignment, candidate_alignment):
        raise SequenceMathError("independent mode alignment is forbidden")
    nullspace_aligned, nullspace_rotations = _apply_alignment_to_mode(
        alignment, nullspace_positions, nullspace_stored_quaternions_xyzw, "nullspace"
    )
    schur_aligned, schur_rotations = _apply_alignment_to_mode(
        alignment, schur_positions, schur_stored_quaternions_xyzw, "schur"
    )
    if nullspace_aligned.shape != schur_aligned.shape:
        raise SequenceMathError("aligned mode populations differ")
    return AlignedModePair(
        nullspace_positions=nullspace_aligned,
        schur_positions=schur_aligned,
        nullspace_inverse_rotations=nullspace_rotations,
        schur_inverse_rotations=schur_rotations,
    )


def _euclidean_norm3(vector: np.ndarray) -> np.float64:
    squared = np.float64(0.0)
    for axis in range(3):
        squared = np.float64(squared + np.float64(vector[axis] * vector[axis]))
    result = np.float64(math.sqrt(float(squared)))
    if not np.isfinite(result):
        raise SequenceMathError("Euclidean norm is nonfinite")
    return result


def position_differences_m(nullspace_aligned_positions: Any, schur_aligned_positions: Any) -> np.ndarray:
    nullspace = _as_f64_matrix(nullspace_aligned_positions, 3, "aligned nullspace positions")
    schur = _as_f64_matrix(schur_aligned_positions, 3, "aligned schur positions")
    if nullspace.shape != schur.shape or nullspace.shape[0] == 0:
        raise SequenceMathError("position-difference populations must be equal and nonempty")
    result = np.empty(nullspace.shape[0], dtype=np.float64)
    for index in range(nullspace.shape[0]):
        difference = np.empty(3, dtype=np.float64)
        for axis in range(3):
            difference[axis] = np.float64(schur[index, axis] - nullspace[index, axis])
        result[index] = _euclidean_norm3(difference)
    return result


def orientation_differences_deg(nullspace_inverse_rotations: Any, schur_inverse_rotations: Any) -> np.ndarray:
    nullspace = _numeric_array(nullspace_inverse_rotations, "aligned nullspace inverse rotations")
    schur = _numeric_array(schur_inverse_rotations, "aligned schur inverse rotations")
    if (
        nullspace.ndim != 3
        or nullspace.shape[1:] != (3, 3)
        or nullspace.shape != schur.shape
        or nullspace.shape[0] == 0
    ):
        raise SequenceMathError("orientation-difference rotation populations must have equal nonempty shape (N,3,3)")
    result = np.empty(nullspace.shape[0], dtype=np.float64)
    for index in range(nullspace.shape[0]):
        validate_proper_rotation(nullspace[index])
        validate_proper_rotation(schur[index])
        nullspace_g_to_i = nullspace[index].T
        schur_g_to_i = schur[index].T
        relative = np.matmul(nullspace_g_to_i.T, schur_g_to_i, dtype=np.float64)
        trace = np.float64(np.float64(relative[0, 0] + relative[1, 1]) + relative[2, 2])
        cosine = np.float64(np.float64(trace - np.float64(1.0)) / np.float64(2.0))
        clamped = np.float64(min(1.0, max(-1.0, float(cosine))))
        radians = np.float64(math.acos(float(clamped)))
        result[index] = np.float64(radians * np.float64(180.0 / math.pi))
        if not np.isfinite(result[index]):
            raise SequenceMathError("orientation difference is nonfinite")
    return result


def numpy_linear_quantile(values: Any, quantile: Any) -> np.float64:
    """Frozen NumPy-linear interpolation over finite binary64 values."""

    samples = _numeric_array(values, "quantile population")
    if samples.ndim != 1 or samples.size == 0:
        raise SequenceMathError("quantile population must be a nonempty vector")
    if isinstance(quantile, (bool, np.bool_)) or not isinstance(quantile, (int, float, np.integer, np.floating)):
        raise SequenceMathError("quantile must be a finite binary64 number")
    q = np.float64(quantile)
    if not np.isfinite(q) or q < np.float64(0.0) or q > np.float64(1.0):
        raise SequenceMathError("quantile must be in [0,1]")
    ordered = np.sort(samples, kind="quicksort")
    h = np.float64(np.float64(ordered.size - 1) * q)
    low = int(math.floor(float(h)))
    high = int(math.ceil(float(h)))
    fraction = np.float64(h - np.float64(low))
    result = np.float64(ordered[low] + np.float64(fraction * np.float64(ordered[high] - ordered[low])))
    if not np.isfinite(result):
        raise SequenceMathError("linear quantile is nonfinite")
    return result


def linear_p95(values: Any) -> np.float64:
    return numpy_linear_quantile(values, np.float64(0.95))


def translation_rmse_m(aligned_positions: Any, associated_ground_truth_positions: Any) -> np.float64:
    aligned = _as_f64_matrix(aligned_positions, 3, "aligned trajectory positions")
    ground_truth = _as_f64_matrix(associated_ground_truth_positions, 3, "associated ground-truth positions")
    if aligned.shape != ground_truth.shape or aligned.shape[0] == 0:
        raise SequenceMathError("ATE populations must be equal and nonempty")
    if aligned.shape[0] > EXACT_BINARY64_INTEGER_MAX:
        raise SequenceMathError("ATE population count exceeds exact binary64 range")
    squared_sum = np.float64(0.0)
    for index in range(aligned.shape[0]):
        for axis in range(3):
            difference = np.float64(aligned[index, axis] - ground_truth[index, axis])
            squared_sum = np.float64(squared_sum + np.float64(difference * difference))
    mean_squared = np.float64(squared_sum / np.float64(aligned.shape[0]))
    result = np.float64(math.sqrt(float(mean_squared)))
    if not np.isfinite(result):
        raise SequenceMathError("translation RMSE is nonfinite")
    return result


def relative_ate_difference(ate_nullspace_m: Any, ate_schur_m: Any) -> np.float64:
    baseline = _f64_scalar(ate_nullspace_m, "baseline ATE")
    candidate = _f64_scalar(ate_schur_m, "candidate ATE")
    if baseline <= np.float64(0.0) or candidate < np.float64(0.0):
        raise SequenceMathError("baseline ATE must be strictly positive and both ATE values finite and nonnegative")
    result = np.float64(abs(np.float64(candidate - baseline)) / baseline)
    if not np.isfinite(result):
        raise SequenceMathError("relative ATE difference is nonfinite")
    return result


def validate_metric_limits(
    position_p95_m: Any,
    orientation_p95_deg: Any,
    relative_ate: Any,
) -> None:
    values = (position_p95_m, orientation_p95_deg, relative_ate)
    limits = (POSITION_P95_LIMIT_M, ORIENTATION_P95_LIMIT_DEG, RELATIVE_ATE_LIMIT)
    converted_values = tuple(_f64_scalar(value, "trajectory metric") for value in values)
    for converted in converted_values:
        if converted < np.float64(0.0):
            raise SequenceMathError("trajectory metric must be finite and nonnegative")
    labels = ("position p95", "orientation p95", "relative ATE")
    for label, value, limit in zip(labels, converted_values, limits):
        if value > limit:
            raise SequenceMathError(label + " exceeds its inclusive CP2-D limit")


__all__ = [
    "AlignedModePair",
    "EXACT_BINARY64_INTEGER_MAX",
    "GROUND_TRUTH_MAX_DIFFERENCE_NS",
    "ORIENTATION_P95_LIMIT_DEG",
    "POSITION_P95_LIMIT_M",
    "QUATERNION_NORM_TOLERANCE",
    "RELATIVE_ATE_LIMIT",
    "ROTATION_VALIDATION_TOLERANCE",
    "SE3Alignment",
    "SequenceMathError",
    "TimestampAssociation",
    "U64_MAX",
    "apply_common_alignment",
    "associate_nearest_ground_truth",
    "baseline_kabsch_alignment",
    "jpl_stored_xyzw_to_hamilton_inverse_rotation",
    "linear_p95",
    "numpy_linear_quantile",
    "orientation_differences_deg",
    "position_differences_m",
    "relative_ate_difference",
    "shared_timestamp_intersection",
    "translation_rmse_m",
    "validate_ground_truth_associations",
    "validate_metric_limits",
    "validate_proper_rotation",
    "validate_shared_timestamp_intersection",
    "validate_source_singular_values",
]
