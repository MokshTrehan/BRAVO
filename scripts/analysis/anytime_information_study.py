#!/usr/bin/env python3
"""Offline full-update reconstruction for Schema-2 update envelopes.

The production reconstruction uses the captured compressed system and the
frozen OpenVINS upper-triangle covariance convention.  Two independent checks
are deliberately kept separate:

* a conventional symmetric covariance-form update from the same compressed
  inputs; and
* an augmented full-joint least-squares oracle built from every accepted raw
  ``H_x, H_f, r`` track and a PSD factor of the complete predicted prior.

No reconstructed value feeds the live estimator.  This module depends only on
NumPy and the standard-library Schema-2 reader.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Sequence, Tuple

import numpy as np


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from experiments.anytime_information.capture_reader import (  # noqa: E402
    CaptureValidationError,
    GlobalSystem,
    LayoutBlock,
    Matrix,
    StateBlock,
    TerminalStatus,
    TrackRecord,
    UpdateEnvelope,
    open_capture,
)


PRODUCTION_ABSOLUTE_TOLERANCE = 1.0e-12
PRODUCTION_RELATIVE_TOLERANCE = 1.0e-10
INFORMATION_ABSOLUTE_TOLERANCE = 1.0e-10
INFORMATION_RELATIVE_TOLERANCE = 1.0e-8
FULL_JOINT_ABSOLUTE_TOLERANCE = 1.0e-10
FULL_JOINT_RELATIVE_TOLERANCE = 1.0e-7
PSD_TOLERANCE_MULTIPLIER = 256.0
NUMERICAL_CERTIFICATION_MULTIPLIER = 256.0
PREVIEW_STATUS_ACCEPTED = 0
PREVIEW_STAGE_ACCEPTED = 13


class ReconstructionError(ValueError):
    """An envelope is valid Schema 2 but fails full-update reconstruction."""


@dataclass(frozen=True)
class Agreement:
    name: str
    passed: bool
    error: float
    tolerance: float
    ratio: float
    detail: str = ""
    applicable: bool = True
    unsupported_reason: str = ""


@dataclass(frozen=True)
class CovarianceDiagnostics:
    finite: bool
    symmetry_error_inf: float
    symmetry_tolerance: float
    symmetric: bool
    minimum_eigenvalue: float
    maximum_absolute_eigenvalue: float
    psd_tolerance: float
    scaled_psd: bool


@dataclass(frozen=True)
class SystemInformation:
    full_h: np.ndarray
    residual: np.ndarray
    covariance: np.ndarray
    information: np.ndarray
    gradient: np.ndarray
    gamma: float


@dataclass(frozen=True)
class PosteriorReconstruction:
    dx: np.ndarray
    production_p_plus: np.ndarray
    independent_p_plus: np.ndarray
    nis: float
    innovation: np.ndarray
    independent_supported: bool
    independent_reason: str


@dataclass(frozen=True)
class FullJointOracle:
    supported: bool
    reason: str
    landmark_ranks: Tuple[int, ...]
    joint_rank: int
    information: np.ndarray
    gradient: np.ndarray
    dx: np.ndarray | None
    p_plus: np.ndarray | None
    nis: float | None


@dataclass(frozen=True)
class SelectedAssembly:
    """Selected global system rebuilt only from accepted per-track records."""

    full_h: np.ndarray
    residual: np.ndarray
    covariance: np.ndarray


@dataclass(frozen=True)
class StageCostAccounting:
    prefilter_ns: int
    geometry_ns: int
    residual_jacobian_ns: int
    reduction_ns: int
    gating_ns: int
    track_accumulation_ns: int
    selected_system_ns: int
    compression_ns: int
    preview_ns: int
    commit_ns: int
    charged_visual_total_ns: int
    capture_record_construction_ns: int
    capture_serialization_ns: int
    capture_output_total_ns: int
    capture_output_bytes: int
    callback_tracking_seconds: float
    callback_propagation_seconds: float
    callback_msckf_seconds: float
    callback_slam_update_seconds: float
    callback_slam_delay_seconds: float
    callback_finalization_seconds: float
    callback_total_seconds: float


@dataclass(frozen=True)
class UpdateReconstruction:
    update_id: int
    camera_timestamp: float
    terminal_status: TerminalStatus
    accepted_feature_ids: Tuple[int, ...]
    candidate_count: int
    accepted_update: bool
    selected: SystemInformation
    compressed: SystemInformation
    posterior: PosteriorReconstruction | None
    full_joint: FullJointOracle | None
    prior_covariance: CovarianceDiagnostics
    captured_posterior_covariance: CovarianceDiagnostics | None
    independent_posterior_covariance: CovarianceDiagnostics | None
    costs: StageCostAccounting
    checks: Tuple[Agreement, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks if check.applicable)


@dataclass(frozen=True)
class CaptureReconstructionSummary:
    path: str
    source_commit: str
    config_sha256: str
    run_id: str
    sequence_id: str
    update_count: int
    candidate_count: int
    accepted_track_count: int
    accepted_update_count: int
    zero_candidate_count: int
    no_update_count: int
    independent_covariance_supported_count: int
    independent_covariance_unsupported_count: int
    full_joint_supported_count: int
    full_joint_unsupported_count: int
    unsupported_check_count: int
    unsupported_update_count: int
    unsupported_reasons: Tuple[Tuple[str, int], ...]
    failed_check_count: int
    maximum_check_ratio: float
    charged_visual_total_ns: int
    capture_record_construction_ns: int
    capture_serialization_ns: int
    capture_output_total_ns: int
    strict_validation: bool

    def to_json(self) -> Dict[str, Any]:
        return {
            "accepted_track_count": self.accepted_track_count,
            "accepted_update_count": self.accepted_update_count,
            "candidate_count": self.candidate_count,
            "capture_output_total_ns": self.capture_output_total_ns,
            "capture_record_construction_ns": self.capture_record_construction_ns,
            "capture_serialization_ns": self.capture_serialization_ns,
            "charged_visual_total_ns": self.charged_visual_total_ns,
            "config_sha256": self.config_sha256,
            "failed_check_count": self.failed_check_count,
            "full_joint_unsupported_count": self.full_joint_unsupported_count,
            "full_joint_supported_count": self.full_joint_supported_count,
            "independent_covariance_supported_count": (
                self.independent_covariance_supported_count
            ),
            "independent_covariance_unsupported_count": (
                self.independent_covariance_unsupported_count
            ),
            "maximum_check_ratio": (
                self.maximum_check_ratio
                if math.isfinite(self.maximum_check_ratio)
                else None
            ),
            "no_update_count": self.no_update_count,
            "path": self.path,
            "run_id": self.run_id,
            "sequence_id": self.sequence_id,
            "source_commit": self.source_commit,
            "strict_validation": self.strict_validation,
            "update_count": self.update_count,
            "unsupported_check_count": self.unsupported_check_count,
            "unsupported_reasons": dict(self.unsupported_reasons),
            "unsupported_update_count": self.unsupported_update_count,
            "zero_candidate_count": self.zero_candidate_count,
        }


def _as_array(matrix: Matrix) -> np.ndarray:
    values = np.asarray(matrix.values, dtype=np.float64)
    return values.reshape((matrix.rows, matrix.cols))


def _frobenius_norm(value: np.ndarray) -> float:
    return float(np.linalg.norm(value.ravel(), ord=2))


def _matrix_inf_norm(value: np.ndarray) -> float:
    if value.size == 0:
        return 0.0
    return float(np.max(np.sum(np.abs(value), axis=1)))


def _agreement(
    name: str,
    value: np.ndarray,
    reference: np.ndarray,
    absolute: float,
    relative: float,
    detail: str = "",
) -> Agreement:
    if value.shape != reference.shape or not (
        np.all(np.isfinite(value)) and np.all(np.isfinite(reference))
    ):
        return Agreement(name, False, math.inf, 0.0, math.inf, detail)
    error = _frobenius_norm(value - reference)
    tolerance = absolute + relative * _frobenius_norm(reference)
    ratio = error / tolerance if tolerance > 0.0 else (0.0 if error == 0.0 else math.inf)
    return Agreement(name, error <= tolerance, error, tolerance, ratio, detail)


def _exact_agreement(
    name: str,
    value: np.ndarray,
    reference: np.ndarray,
    detail: str = "",
) -> Agreement:
    """Require equality of every decoded binary64 value and every dimension."""

    passed = value.shape == reference.shape and bool(np.array_equal(value, reference))
    if passed:
        return Agreement(name, True, 0.0, 0.0, 0.0, detail)
    if value.shape != reference.shape:
        detail = (
            f"shape {value.shape!r} != {reference.shape!r}"
            + (f"; {detail}" if detail else "")
        )
        return Agreement(name, False, math.inf, 0.0, math.inf, detail)
    error = _frobenius_norm(value - reference)
    return Agreement(name, False, error, 0.0, math.inf, detail)


def _boolean_check(name: str, passed: bool, detail: str = "") -> Agreement:
    return Agreement(
        name=name,
        passed=passed,
        error=0.0 if passed else math.inf,
        tolerance=0.0,
        ratio=0.0 if passed else math.inf,
        detail=detail,
    )


def _unsupported_check(name: str, reason: str, detail: str = "") -> Agreement:
    """Record an intentionally non-applicable oracle comparison."""

    return Agreement(
        name=name,
        passed=False,
        error=math.inf,
        tolerance=0.0,
        ratio=math.inf,
        detail=detail or f"UNSUPPORTED: {reason}",
        applicable=False,
        unsupported_reason=reason,
    )


def _upper_self_adjoint(value: np.ndarray) -> np.ndarray:
    upper = np.triu(value)
    return upper + np.triu(value, 1).T


def _ordered_matmul(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Multiply binary64 matrices in deterministic scalar inner-dimension order."""

    if left.ndim != 2 or right.ndim != 2 or left.shape[1] != right.shape[0]:
        raise ReconstructionError("ordered matrix product dimensions are invalid")
    result = np.zeros((left.shape[0], right.shape[1]), dtype=np.float64)
    for inner in range(left.shape[1]):
        result += left[:, inner, None] * right[None, inner, :]
    return result


def _ordered_dot(left: np.ndarray, right: np.ndarray) -> float:
    """Match Eigen's non-vectorized sequential binary64 dot product."""

    if left.ndim != 1 or right.ndim != 1 or left.shape != right.shape:
        raise ReconstructionError("ordered dot-product dimensions are invalid")
    result = 0.0
    for index in range(left.shape[0]):
        result += float(left[index]) * float(right[index])
    return result


def _expand_jacobian(
    local_h: np.ndarray, layout: Sequence[LayoutBlock], state_dimension: int
) -> np.ndarray:
    full_h = np.zeros((local_h.shape[0], state_dimension), dtype=np.float64)
    for block in layout:
        local_slice = slice(block.local_column, block.local_column + block.size)
        state_slice = slice(
            block.covariance_column, block.covariance_column + block.size
        )
        full_h[:, state_slice] = local_h[:, local_slice]
    return full_h


def _solve_spd(matrix: np.ndarray, right_hand_side: np.ndarray) -> np.ndarray:
    symmetric = _upper_self_adjoint(matrix)
    try:
        lower = np.linalg.cholesky(symmetric)
        intermediate = np.linalg.solve(lower, right_hand_side)
        result = np.linalg.solve(lower.T, intermediate)
    except np.linalg.LinAlgError as exc:
        raise ReconstructionError("positive-definite solve failed") from exc
    if not np.all(np.isfinite(result)):
        raise ReconstructionError("positive-definite solve produced non-finite values")
    return result


def _system_information(
    system: GlobalSystem, state_dimension: int
) -> SystemInformation:
    if not system.available:
        return SystemInformation(
            full_h=np.zeros((0, state_dimension), dtype=np.float64),
            residual=np.zeros(0, dtype=np.float64),
            covariance=np.zeros((0, 0), dtype=np.float64),
            information=np.zeros((state_dimension, state_dimension), dtype=np.float64),
            gradient=np.zeros(state_dimension, dtype=np.float64),
            gamma=0.0,
        )
    local_h = _as_array(system.h)
    residual = _as_array(system.residual).reshape(-1)
    covariance = _upper_self_adjoint(_as_array(system.covariance))
    full_h = _expand_jacobian(local_h, system.layout, state_dimension)
    solved_h = _solve_spd(covariance, full_h)
    solved_residual = _solve_spd(covariance, residual)
    information = full_h.T @ solved_h
    information = 0.5 * (information + information.T)
    gradient = full_h.T @ solved_residual
    gamma = float(residual @ solved_residual)
    if not (
        np.all(np.isfinite(information))
        and np.all(np.isfinite(gradient))
        and math.isfinite(gamma)
    ):
        raise ReconstructionError("global information system is non-finite")
    return SystemInformation(
        full_h, residual, covariance, information, gradient, gamma
    )


def _posterior_from_system(
    prior: np.ndarray,
    state_blocks: Sequence[StateBlock],
    system: GlobalSystem,
) -> PosteriorReconstruction:
    if not system.available:
        raise ReconstructionError("production posterior requested without a system")
    local_h = _as_array(system.h)
    residual = _as_array(system.residual).reshape(-1)
    measurement_covariance = _as_array(system.covariance)
    state_dimension = prior.shape[0]
    full_h = _expand_jacobian(local_h, system.layout, state_dimension)

    # Reproduce UpdaterMSCKFPreview::ComputeFromSnapshot field-for-field.  The
    # frozen C++ path deliberately accumulates M one semantic state block and
    # one measurement-layout block at a time to match StateHelper::EKFUpdate.
    # A mathematically equivalent full P @ H.T GEMM has a different binary64
    # reduction order and can diverge after long, high-dynamic-range runs.
    expected_offset = 0
    cross = np.zeros((state_dimension, local_h.shape[0]), dtype=np.float64)
    for ordinal, state_block in enumerate(state_blocks):
        if (
            state_block.ordinal != ordinal
            or state_block.offset != expected_offset
            or state_block.covariance_id != state_block.offset
            or state_block.dimension <= 0
            or state_block.offset > state_dimension - state_block.dimension
        ):
            raise ReconstructionError("production state-block layout is invalid")
        cross_block = np.zeros(
            (state_block.dimension, local_h.shape[0]), dtype=np.float64
        )
        for measurement_block in system.layout:
            prior_block = prior[
                state_block.offset : state_block.offset + state_block.dimension,
                measurement_block.covariance_column :
                measurement_block.covariance_column + measurement_block.size,
            ]
            jacobian_block = local_h[
                :,
                measurement_block.local_column :
                measurement_block.local_column + measurement_block.size,
            ]
            cross_block += _ordered_matmul(prior_block, jacobian_block.T)
        cross[
            state_block.offset : state_block.offset + state_block.dimension, :
        ] = cross_block
        expected_offset += state_block.dimension
    if expected_offset != state_dimension:
        raise ReconstructionError("production state blocks do not tile covariance")

    local_dimension = local_h.shape[1]
    marginal = np.zeros((local_dimension, local_dimension), dtype=np.float64)
    expected_local_column = 0
    for row_block in system.layout:
        if (
            row_block.local_column != expected_local_column
            or row_block.size <= 0
            or row_block.local_column > local_dimension - row_block.size
            or row_block.covariance_column
            > state_dimension - row_block.size
        ):
            raise ReconstructionError("production measurement layout is invalid")
        for column_block in system.layout:
            marginal[
                row_block.local_column : row_block.local_column + row_block.size,
                column_block.local_column :
                column_block.local_column + column_block.size,
            ] = prior[
                row_block.covariance_column :
                row_block.covariance_column + row_block.size,
                column_block.covariance_column :
                column_block.covariance_column + column_block.size,
            ]
        expected_local_column += row_block.size
    if expected_local_column != local_dimension:
        raise ReconstructionError("production measurement blocks do not tile system")

    # Preserve the scalar inner-product order of Eigen's non-vectorized
    # production build.  BLAS and even unoptimized einsum use different
    # reductions for some small state blocks, which becomes measurable on
    # long, high-dynamic-range sequences.
    h_times_marginal = _ordered_matmul(local_h, marginal)
    innovation = _upper_self_adjoint(
        _ordered_matmul(h_times_marginal, local_h.T) + measurement_covariance
    )
    innovation_inverse = _upper_self_adjoint(
        _solve_spd(
            innovation,
            np.eye(innovation.shape[0], dtype=np.float64),
        )
    )
    gain = _ordered_matmul(cross, innovation_inverse)
    dx = _ordered_matmul(gain, residual[:, None]).reshape(-1)
    covariance_delta = _ordered_matmul(gain, cross.T)

    production_p_plus = prior.copy()
    upper_indices = np.triu_indices(prior.shape[0])
    production_p_plus[upper_indices] -= covariance_delta[upper_indices]
    production_p_plus = _upper_self_adjoint(production_p_plus)

    identity_minus_kh = np.eye(state_dimension, dtype=np.float64) - gain @ full_h
    independent_p_plus = (
        identity_minus_kh @ prior @ identity_minus_kh.T
        + gain @ _upper_self_adjoint(measurement_covariance) @ gain.T
    )
    independent_p_plus = 0.5 * (independent_p_plus + independent_p_plus.T)
    solved_residual = _ordered_matmul(
        innovation_inverse, residual[:, None]
    ).reshape(-1)
    nis = _ordered_dot(residual, solved_residual)
    prior_factor, prior_diagnostics = _prior_support(prior)
    independent_supported, independent_reason = _prior_posterior_certification(
        prior, prior_factor, prior_diagnostics
    )
    if independent_supported:
        independent_supported, independent_reason = _spd_solve_certification(
            innovation, "innovation_cholesky_stability"
        )
    if not (
        np.all(np.isfinite(dx))
        and np.all(np.isfinite(production_p_plus))
        and np.all(np.isfinite(independent_p_plus))
        and math.isfinite(nis)
    ):
        raise ReconstructionError("production posterior reconstruction is non-finite")
    return PosteriorReconstruction(
        dx=dx,
        production_p_plus=production_p_plus,
        independent_p_plus=independent_p_plus,
        nis=nis,
        innovation=innovation,
        independent_supported=independent_supported,
        independent_reason=independent_reason,
    )


def covariance_diagnostics(covariance: np.ndarray) -> CovarianceDiagnostics:
    finite = covariance.ndim == 2 and covariance.shape[0] == covariance.shape[1]
    finite = finite and bool(np.all(np.isfinite(covariance)))
    if not finite:
        return CovarianceDiagnostics(
            finite=False,
            symmetry_error_inf=math.inf,
            symmetry_tolerance=0.0,
            symmetric=False,
            minimum_eigenvalue=-math.inf,
            maximum_absolute_eigenvalue=math.inf,
            psd_tolerance=0.0,
            scaled_psd=False,
        )
    symmetric_covariance = 0.5 * (covariance + covariance.T)
    try:
        eigenvalues = np.linalg.eigvalsh(symmetric_covariance)
    except np.linalg.LinAlgError:
        return CovarianceDiagnostics(
            finite=True,
            symmetry_error_inf=math.inf,
            symmetry_tolerance=0.0,
            symmetric=False,
            minimum_eigenvalue=-math.inf,
            maximum_absolute_eigenvalue=math.inf,
            psd_tolerance=0.0,
            scaled_psd=False,
        )
    maximum_absolute = float(np.max(np.abs(eigenvalues))) if eigenvalues.size else 0.0
    tolerance = (
        PSD_TOLERANCE_MULTIPLIER
        * max(1, covariance.shape[0])
        * np.finfo(np.float64).eps
        * max(1.0, maximum_absolute)
    )
    symmetry_error = _matrix_inf_norm(covariance - covariance.T)
    minimum = float(np.min(eigenvalues)) if eigenvalues.size else 0.0
    return CovarianceDiagnostics(
        finite=True,
        symmetry_error_inf=symmetry_error,
        symmetry_tolerance=tolerance,
        symmetric=symmetry_error <= tolerance,
        minimum_eigenvalue=minimum,
        maximum_absolute_eigenvalue=maximum_absolute,
        psd_tolerance=tolerance,
        scaled_psd=minimum >= -tolerance,
    )


def _prior_support(prior: np.ndarray) -> Tuple[np.ndarray | None, CovarianceDiagnostics]:
    diagnostics = covariance_diagnostics(prior)
    if not diagnostics.finite or not diagnostics.symmetric or not diagnostics.scaled_psd:
        return None, diagnostics
    symmetric = 0.5 * (prior + prior.T)
    try:
        eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    except np.linalg.LinAlgError:
        return None, diagnostics
    # The scaled-PSD tolerance is deliberately conservative: it decides
    # whether a slightly negative eigenvalue is compatible with binary64
    # roundoff.  Reusing its 256x envelope as a rank cutoff discards resolvable
    # positive covariance modes and can change the full-joint correction even
    # when the support-space normal system is exceptionally well conditioned.
    # Use the ordinary binary64 spectral-resolution floor for support rank;
    # negative and numerically unresolved modes remain excluded.
    rank_tolerance = (
        max(1, prior.shape[0])
        * np.finfo(np.float64).eps
        * max(1.0, diagnostics.maximum_absolute_eigenvalue)
    )
    positive = eigenvalues > rank_tolerance
    factor = eigenvectors[:, positive] * np.sqrt(eigenvalues[positive])
    if not np.all(np.isfinite(factor)):
        return None, diagnostics
    return factor, diagnostics


def _prior_posterior_certification(
    prior: np.ndarray,
    factor: np.ndarray | None,
    diagnostics: CovarianceDiagnostics,
) -> Tuple[bool, str]:
    """Certify that retained prior modes are separated for posterior oracles."""

    if factor is None:
        return False, "prior_not_scaled_psd"
    if factor.shape[1] == 0:
        return False, "prior_support_empty"
    rank_floor = (
        max(1, prior.shape[0])
        * np.finfo(np.float64).eps
        * max(1.0, diagnostics.maximum_absolute_eigenvalue)
    )
    certification_floor = NUMERICAL_CERTIFICATION_MULTIPLIER * rank_floor
    try:
        eigenvalues = np.linalg.eigvalsh(0.5 * (prior + prior.T))
    except np.linalg.LinAlgError:
        return False, "prior_spectral_separation"
    retained_eigenvalues = eigenvalues[eigenvalues > rank_floor]
    if retained_eigenvalues.size != factor.shape[1] or (
        not np.all(np.isfinite(retained_eigenvalues))
    ) or float(retained_eigenvalues[0]) <= certification_floor:
        return False, "prior_spectral_separation"
    return True, "supported"


def _svd_rank_certification(
    singular_values: np.ndarray, rank: int, rank_floor: float
) -> Tuple[bool, str]:
    """Require a fixed 256x gap on both sides of an SVD rank decision."""

    if not np.all(np.isfinite(singular_values)) or not math.isfinite(rank_floor):
        return False, "landmark_svd_nonfinite"
    if rank == 0 and not np.any(singular_values) and rank_floor == 0.0:
        return True, "supported"
    if rank > 0 and float(singular_values[rank - 1]) <= (
        NUMERICAL_CERTIFICATION_MULTIPLIER * rank_floor
    ):
        return False, "landmark_svd_rank_gap"
    if rank < singular_values.size and float(singular_values[rank]) >= (
        rank_floor / NUMERICAL_CERTIFICATION_MULTIPLIER
    ):
        return False, "landmark_svd_rank_gap"
    return True, "supported"


def _spd_solve_certification(
    matrix: np.ndarray, reason: str
) -> Tuple[bool, str]:
    """Certify an SPD solve with the same fixed scaled 256x guard."""

    symmetric = _upper_self_adjoint(matrix)
    try:
        eigenvalues = np.linalg.eigvalsh(symmetric)
    except np.linalg.LinAlgError:
        return False, reason
    if eigenvalues.size == 0 or not np.all(np.isfinite(eigenvalues)):
        return False, reason
    scale = max(1.0, float(np.max(np.abs(eigenvalues))))
    floor = (
        NUMERICAL_CERTIFICATION_MULTIPLIER
        * max(1, symmetric.shape[0])
        * np.finfo(np.float64).eps
        * scale
    )
    if float(eigenvalues[0]) <= floor:
        return False, reason
    return True, "supported"


def _raw_track_system(
    track: TrackRecord, state_dimension: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    if not track.raw_available:
        raise ReconstructionError(
            f"accepted feature {track.feature_id} has no raw factor"
        )
    h_x = _as_array(track.h_x)
    h_f = _as_array(track.h_f)
    residual = _as_array(track.residual).reshape(-1)
    full_h_x = _expand_jacobian(h_x, track.layout, state_dimension)
    if h_f.shape != (h_x.shape[0], 3) or residual.shape != (h_x.shape[0],):
        raise ReconstructionError(
            f"accepted feature {track.feature_id} has inconsistent raw shapes"
        )
    if not (
        np.all(np.isfinite(full_h_x))
        and np.all(np.isfinite(h_f))
        and np.all(np.isfinite(residual))
        and math.isfinite(track.noise_variance)
        and track.noise_variance > 0.0
    ):
        raise ReconstructionError(
            f"accepted feature {track.feature_id} has invalid raw values"
        )
    return full_h_x, h_f, residual, track.noise_variance


def _assemble_selected_tracks(
    accepted_tracks: Sequence[TrackRecord], state_dimension: int
) -> SelectedAssembly:
    """Rebuild the exact uncompressed selected system in accepted-ID order."""

    total_rows = sum(track.global_row_count for track in accepted_tracks)
    full_h = np.zeros((total_rows, state_dimension), dtype=np.float64)
    residual = np.zeros(total_rows, dtype=np.float64)
    covariance = np.zeros((total_rows, total_rows), dtype=np.float64)
    cursor = 0
    for track in accepted_tracks:
        if track.global_row_start != cursor:
            raise ReconstructionError(
                f"accepted feature {track.feature_id} starts at global row "
                f"{track.global_row_start}, expected {cursor}"
            )
        reduced_a = _as_array(track.reduced_a)
        reduced_b = _as_array(track.reduced_b).reshape(-1)
        rows = track.global_row_count
        if (
            not track.reduction_recorded
            or reduced_a.shape[0] != rows
            or reduced_b.shape != (rows,)
        ):
            raise ReconstructionError(
                f"accepted feature {track.feature_id} has an incomplete reduced factor"
            )
        row_slice = slice(cursor, cursor + rows)
        full_h[row_slice, :] = _expand_jacobian(
            reduced_a, track.layout, state_dimension
        )
        residual[row_slice] = reduced_b
        covariance[row_slice, row_slice] = (
            track.noise_variance * np.eye(rows, dtype=np.float64)
        )
        cursor += rows
    return SelectedAssembly(full_h, residual, covariance)


def _pseudoinverse_nullspace_rows(
    matrix: np.ndarray,
) -> Tuple[np.ndarray, int, float, np.ndarray]:
    """Return a stable row basis for ``I - matrix @ pinv(matrix)``.

    Forming that projector explicitly catastrophically cancels when a raw
    state Jacobian is large but almost entirely contained in the landmark
    column space.  The discarded columns of the full left singular-vector
    matrix are the same Moore-Penrose projector represented as orthonormal
    rows, without that subtraction.
    """

    u, singular_values, _ = np.linalg.svd(matrix, full_matrices=True)
    largest = float(singular_values[0]) if singular_values.size else 0.0
    floor = max(1, *matrix.shape) * np.finfo(np.float64).eps * largest
    retained = singular_values > floor
    rank = int(np.count_nonzero(retained))
    return u[:, rank:].T, rank, floor, singular_values


def _full_joint_oracle(
    prior: np.ndarray,
    accepted_tracks: Sequence[TrackRecord],
) -> FullJointOracle | None:
    if not accepted_tracks:
        return None
    state_dimension = prior.shape[0]
    factor, prior_diagnostics = _prior_support(prior)
    prior_supported, support_reason = _prior_posterior_certification(
        prior, factor, prior_diagnostics
    )

    raw_systems = [_raw_track_system(track, state_dimension) for track in accepted_tracks]
    information = np.zeros((state_dimension, state_dimension), dtype=np.float64)
    gradient = np.zeros(state_dimension, dtype=np.float64)
    projected_gamma = 0.0
    landmark_ranks: List[int] = []
    for h_x, h_f, residual, variance in raw_systems:
        try:
            (
                left_nullspace,
                rank,
                rank_floor,
                singular_values,
            ) = _pseudoinverse_nullspace_rows(h_f)
        except np.linalg.LinAlgError:
            return FullJointOracle(
                supported=False,
                reason="landmark_pseudoinverse_failed",
                landmark_ranks=tuple(landmark_ranks),
                joint_rank=0,
                information=information,
                gradient=gradient,
                dx=None,
                p_plus=None,
                nis=None,
            )
        landmark_ranks.append(rank)
        rank_supported, rank_reason = _svd_rank_certification(
            singular_values, rank, rank_floor
        )
        if support_reason == "supported" and not rank_supported:
            support_reason = rank_reason
        sigma = math.sqrt(variance)
        projected_h = left_nullspace @ h_x / sigma
        projected_residual = left_nullspace @ residual / sigma
        information += projected_h.T @ projected_h
        gradient += projected_h.T @ projected_residual
        projected_gamma += float(projected_residual @ projected_residual)

    information = 0.5 * (information + information.T)
    support_rank = factor.shape[1] if factor is not None else 0
    joint_rank = support_rank + sum(landmark_ranks)
    if not (
        np.all(np.isfinite(information)) and np.all(np.isfinite(gradient))
    ):
        return FullJointOracle(
            supported=False,
            reason="pseudoinverse_information_nonfinite",
            landmark_ranks=tuple(landmark_ranks),
            joint_rank=joint_rank,
            information=information,
            gradient=gradient,
            dx=None,
            p_plus=None,
            nis=None,
        )

    if not prior_supported or support_reason != "supported":
        return FullJointOracle(
            supported=False,
            reason=support_reason,
            landmark_ranks=tuple(landmark_ranks),
            joint_rank=joint_rank,
            information=information,
            gradient=gradient,
            dx=None,
            p_plus=None,
            nis=None,
        )
    assert factor is not None

    # This is the state Schur complement of the augmented full-joint problem
    #   min ||z||^2 + sum_i ||(H_x P^(1/2) z + H_f df_i - r_i)/sigma_i||^2.
    # Each landmark is eliminated only here with an SVD pseudoinverse; no
    # captured reduced row is used by this oracle.
    normal = np.eye(support_rank, dtype=np.float64)
    normal += factor.T @ information @ factor
    normal = 0.5 * (normal + normal.T)
    rhs = factor.T @ gradient
    normal_supported, normal_reason = _spd_solve_certification(
        normal, "full_joint_normal_cholesky_stability"
    )
    if not normal_supported:
        return FullJointOracle(
            supported=False,
            reason=normal_reason,
            landmark_ranks=tuple(landmark_ranks),
            joint_rank=joint_rank,
            information=information,
            gradient=gradient,
            dx=None,
            p_plus=None,
            nis=None,
        )
    try:
        lower = np.linalg.cholesky(normal)
        solved_rhs = np.linalg.solve(lower.T, np.linalg.solve(lower, rhs))
        inverse = np.linalg.solve(
            lower.T, np.linalg.solve(lower, np.eye(support_rank, dtype=np.float64))
        )
    except np.linalg.LinAlgError:
        return FullJointOracle(
            supported=False,
            reason="joint_state_schur_factorization_failed",
            landmark_ranks=tuple(landmark_ranks),
            joint_rank=joint_rank,
            information=information,
            gradient=gradient,
            dx=None,
            p_plus=None,
            nis=None,
        )
    dx = factor @ solved_rhs
    p_plus = factor @ inverse @ factor.T
    p_plus = 0.5 * (p_plus + p_plus.T)
    nis = float(projected_gamma - rhs @ solved_rhs)
    supported = (
        np.all(np.isfinite(dx))
        and np.all(np.isfinite(p_plus))
        and math.isfinite(nis)
        and prior_diagnostics.scaled_psd
    )
    return FullJointOracle(
        supported=supported,
        reason="supported" if supported else "joint_state_schur_nonfinite",
        landmark_ranks=tuple(landmark_ranks),
        joint_rank=joint_rank,
        information=information,
        gradient=gradient,
        dx=dx if supported else None,
        p_plus=p_plus if supported else None,
        nis=nis if supported else None,
    )


def stage_cost_accounting(envelope: UpdateEnvelope) -> StageCostAccounting:
    prefilter = sum(candidate.prefilter_duration_ns for candidate in envelope.candidates)
    geometry = sum(track.geometry_duration_ns for track in envelope.tracks)
    residual_jacobian = sum(track.raw_factor_duration_ns for track in envelope.tracks)
    reduction = sum(track.reduction_duration_ns for track in envelope.tracks)
    gating = sum(track.gate_duration_ns for track in envelope.tracks)
    track_accumulation = sum(
        track.accumulation_duration_ns for track in envelope.tracks
    )
    selected_system = envelope.selected_system.duration_ns
    compression = envelope.compressed_system.duration_ns
    preview = envelope.preview_duration_ns
    commit = envelope.commit_duration_ns
    charged = sum(
        (
            prefilter,
            geometry,
            residual_jacobian,
            reduction,
            gating,
            track_accumulation,
            selected_system,
            compression,
            preview,
            commit,
        )
    )
    callback = envelope.callback_costs
    capture_record_construction = (
        envelope.capture_output.record_construction_duration_ns
    )
    capture_serialization = envelope.capture_output.serialization_duration_ns
    return StageCostAccounting(
        prefilter_ns=prefilter,
        geometry_ns=geometry,
        residual_jacobian_ns=residual_jacobian,
        reduction_ns=reduction,
        gating_ns=gating,
        track_accumulation_ns=track_accumulation,
        selected_system_ns=selected_system,
        compression_ns=compression,
        preview_ns=preview,
        commit_ns=commit,
        charged_visual_total_ns=charged,
        capture_record_construction_ns=capture_record_construction,
        capture_serialization_ns=capture_serialization,
        capture_output_total_ns=capture_record_construction + capture_serialization,
        capture_output_bytes=envelope.capture_output.encoded_payload_bytes,
        callback_tracking_seconds=callback.tracking_seconds,
        callback_propagation_seconds=callback.propagation_seconds,
        callback_msckf_seconds=callback.msckf_seconds,
        callback_slam_update_seconds=callback.slam_update_seconds,
        callback_slam_delay_seconds=callback.slam_delay_seconds,
        callback_finalization_seconds=callback.finalization_seconds,
        callback_total_seconds=callback.total_seconds,
    )


def _costs_valid(costs: StageCostAccounting) -> bool:
    integer_values = (
        costs.prefilter_ns,
        costs.geometry_ns,
        costs.residual_jacobian_ns,
        costs.reduction_ns,
        costs.gating_ns,
        costs.track_accumulation_ns,
        costs.selected_system_ns,
        costs.compression_ns,
        costs.preview_ns,
        costs.commit_ns,
        costs.charged_visual_total_ns,
        costs.capture_record_construction_ns,
        costs.capture_serialization_ns,
        costs.capture_output_total_ns,
        costs.capture_output_bytes,
    )
    float_values = (
        costs.callback_tracking_seconds,
        costs.callback_propagation_seconds,
        costs.callback_msckf_seconds,
        costs.callback_slam_update_seconds,
        costs.callback_slam_delay_seconds,
        costs.callback_finalization_seconds,
        costs.callback_total_seconds,
    )
    return all(value >= 0 for value in integer_values) and all(
        math.isfinite(value) and value >= 0.0 for value in float_values
    )


def _stage_ownership_checks(envelope: UpdateEnvelope) -> Tuple[Agreement, ...]:
    """Check that every charged duration belongs to a stage that actually ran."""

    checks: List[Agreement] = []
    for candidate in envelope.candidates:
        checks.append(
            _boolean_check(
                f"feature_{candidate.feature_id}_prefilter_cost_owner",
                candidate.prefilter_recorded or candidate.prefilter_duration_ns == 0,
            )
        )
    for track in envelope.tracks:
        ownership = (
            ("geometry", track.geometry_recorded, track.geometry_duration_ns),
            ("raw_factor", track.raw_available, track.raw_factor_duration_ns),
            ("reduction", track.reduction_recorded, track.reduction_duration_ns),
            ("gate", track.gate_recorded, track.gate_duration_ns),
            (
                "accumulation",
                track.accepted_for_global_system,
                track.accumulation_duration_ns,
            ),
        )
        for stage, recorded, duration in ownership:
            checks.append(
                _boolean_check(
                    f"feature_{track.feature_id}_{stage}_cost_owner",
                    recorded or duration == 0,
                )
            )
    checks.extend(
        (
            _boolean_check(
                "preview_cost_owner",
                envelope.posterior_recorded or envelope.preview_duration_ns == 0,
            ),
            _boolean_check(
                "commit_cost_owner",
                envelope.terminal_status == TerminalStatus.COMMITTED
                or envelope.commit_duration_ns == 0,
            ),
            _boolean_check(
                "capture_output_cost_owner",
                envelope.capture_output.available
                and envelope.capture_output.serialization_duration_ns > 0
                and envelope.capture_output.encoded_payload_bytes > 0,
            ),
        )
    )
    timeline = envelope.callback_timeline
    endpoints = (
        timeline.callback_begin,
        timeline.tracking_end,
        timeline.propagation_end,
        timeline.msckf_end,
        timeline.slam_update_end,
        timeline.slam_delay_end,
        timeline.finalization_end,
    )
    callback_durations = (
        envelope.callback_costs.tracking_seconds,
        envelope.callback_costs.propagation_seconds,
        envelope.callback_costs.msckf_seconds,
        envelope.callback_costs.slam_update_seconds,
        envelope.callback_costs.slam_delay_seconds,
        envelope.callback_costs.finalization_seconds,
    )
    timeline_valid = timeline.available and all(
        math.isfinite(value) for value in endpoints
    )
    timeline_valid = timeline_valid and all(
        left <= right for left, right in zip(endpoints, endpoints[1:])
    )
    timeline_valid = timeline_valid and abs(endpoints[0]) == 0.0
    for left, right, duration in zip(endpoints, endpoints[1:], callback_durations):
        scale = max(1.0, abs(right - left), abs(duration))
        timeline_valid = timeline_valid and (
            abs((right - left) - duration) <= 1.0e-12 * scale
        )
    total_scale = max(
        1.0,
        abs(endpoints[-1] - endpoints[0]),
        abs(envelope.callback_costs.total_seconds),
    )
    timeline_valid = timeline_valid and (
        abs(
            endpoints[-1]
            - endpoints[0]
            - envelope.callback_costs.total_seconds
        )
        <= 1.0e-12 * total_scale
    )
    checks.append(_boolean_check("callback_timeline_cost_ownership", timeline_valid))
    return tuple(checks)


def reconstruct_update(
    envelope: UpdateEnvelope, *, strict: bool = True
) -> UpdateReconstruction:
    prior = _as_array(envelope.p_minus)
    state_dimension = prior.shape[0]
    selected = _system_information(envelope.selected_system, state_dimension)
    compressed = _system_information(envelope.compressed_system, state_dimension)
    checks: List[Agreement] = []

    accepted_tracks = tuple(
        track for track in envelope.tracks if track.accepted_for_global_system
    )
    accepted_order = tuple(track.feature_id for track in accepted_tracks)
    checks.append(
        _boolean_check(
            "accepted_feature_order",
            accepted_order == envelope.accepted_feature_ids,
            f"tracks={accepted_order!r} envelope={envelope.accepted_feature_ids!r}",
        )
    )
    rebuilt_selected = _assemble_selected_tracks(accepted_tracks, state_dimension)
    checks.extend(
        (
            _exact_agreement(
                "selected_track_rows_H",
                rebuilt_selected.full_h,
                selected.full_h,
            ),
            _exact_agreement(
                "selected_track_rows_residual",
                rebuilt_selected.residual,
                selected.residual,
            ),
            _exact_agreement(
                "selected_track_rows_R",
                rebuilt_selected.covariance,
                selected.covariance,
            ),
        )
    )
    checks.append(
        _agreement(
            "selected_vs_compressed_information",
            compressed.information,
            selected.information,
            INFORMATION_ABSOLUTE_TOLERANCE,
            INFORMATION_RELATIVE_TOLERANCE,
        )
    )
    checks.append(
        _agreement(
            "selected_vs_compressed_gradient",
            compressed.gradient,
            selected.gradient,
            INFORMATION_ABSOLUTE_TOLERANCE,
            INFORMATION_RELATIVE_TOLERANCE,
        )
    )

    prior_diagnostics = covariance_diagnostics(prior)
    checks.extend(
        (
            _boolean_check("P_minus_finite", prior_diagnostics.finite),
            _boolean_check("P_minus_symmetric", prior_diagnostics.symmetric),
            _boolean_check("P_minus_scaled_psd", prior_diagnostics.scaled_psd),
        )
    )

    costs = stage_cost_accounting(envelope)
    checks.append(_boolean_check("stage_costs_valid", _costs_valid(costs)))
    checks.extend(_stage_ownership_checks(envelope))

    accepted_update = envelope.terminal_status == TerminalStatus.COMMITTED
    posterior: PosteriorReconstruction | None = None
    captured_diagnostics: CovarianceDiagnostics | None = None
    independent_diagnostics: CovarianceDiagnostics | None = None
    if envelope.compressed_system.available:
        posterior = _posterior_from_system(
            prior, envelope.state_blocks, envelope.compressed_system
        )
        if envelope.global_gate.nis_available:
            checks.append(
                _agreement(
                    "production_global_NIS",
                    np.asarray((posterior.nis,), dtype=np.float64),
                    np.asarray((envelope.global_gate.nis,), dtype=np.float64),
                    PRODUCTION_ABSOLUTE_TOLERANCE,
                    PRODUCTION_RELATIVE_TOLERANCE,
                )
            )

    if accepted_update:
        checks.append(
            _boolean_check(
                "accepted_update_has_complete_system",
                envelope.selected_system.available
                and envelope.compressed_system.available
                and envelope.posterior_recorded
                and envelope.preview_status == PREVIEW_STATUS_ACCEPTED
                and envelope.preview_stage == PREVIEW_STAGE_ACCEPTED
                and envelope.mean_commit_count == 1
                and envelope.covariance_commit_count == 1
                and envelope.feature_finalization_count == 1
                and posterior is not None,
            )
        )
        if posterior is not None:
            captured_dx = _as_array(envelope.production_dx).reshape(-1)
            captured_p_plus = _as_array(envelope.p_plus)
            checks.extend(
                (
                    _agreement(
                        "production_dx",
                        posterior.dx,
                        captured_dx,
                        PRODUCTION_ABSOLUTE_TOLERANCE,
                        PRODUCTION_RELATIVE_TOLERANCE,
                    ),
                    _agreement(
                        "production_P_plus",
                        posterior.production_p_plus,
                        captured_p_plus,
                        PRODUCTION_ABSOLUTE_TOLERANCE,
                        PRODUCTION_RELATIVE_TOLERANCE,
                    ),
                )
            )
            if posterior.independent_supported:
                checks.append(
                    _agreement(
                        "independent_covariance_P_plus",
                        posterior.independent_p_plus,
                        captured_p_plus,
                        PRODUCTION_ABSOLUTE_TOLERANCE,
                        PRODUCTION_RELATIVE_TOLERANCE,
                    )
                )
            else:
                checks.append(
                    _unsupported_check(
                        "independent_covariance_P_plus",
                        posterior.independent_reason,
                    )
                )
            captured_diagnostics = covariance_diagnostics(captured_p_plus)
            independent_diagnostics = covariance_diagnostics(
                posterior.independent_p_plus
            )
            checks.extend(
                (
                    _boolean_check(
                        "captured_P_plus_finite", captured_diagnostics.finite
                    ),
                    _boolean_check(
                        "captured_P_plus_symmetric", captured_diagnostics.symmetric
                    ),
                    _boolean_check(
                        "captured_P_plus_scaled_psd", captured_diagnostics.scaled_psd
                    ),
                    _boolean_check(
                        "independent_P_plus_finite", independent_diagnostics.finite
                    ),
                    _boolean_check(
                        "independent_P_plus_symmetric",
                        independent_diagnostics.symmetric,
                    ),
                    _boolean_check(
                        "independent_P_plus_scaled_psd",
                        independent_diagnostics.scaled_psd,
                    ),
                )
            )
    else:
        checks.append(
            _boolean_check(
                "no_update_commit_accounting",
                envelope.mean_commit_count == 0
                and envelope.covariance_commit_count == 0,
            )
        )
        if not envelope.accepted_feature_ids:
            checks.append(
                _boolean_check(
                    "empty_selected_information",
                    not envelope.selected_system.available
                    and not envelope.compressed_system.available
                    and not np.any(selected.information)
                    and not np.any(selected.gradient),
                )
            )

    full_joint = _full_joint_oracle(prior, accepted_tracks)
    if full_joint is not None:
        checks.extend(
            (
                _agreement(
                    "full_joint_information",
                    full_joint.information,
                    selected.information,
                    INFORMATION_ABSOLUTE_TOLERANCE,
                    INFORMATION_RELATIVE_TOLERANCE,
                ),
                _agreement(
                    "full_joint_gradient",
                    full_joint.gradient,
                    selected.gradient,
                    INFORMATION_ABSOLUTE_TOLERANCE,
                    INFORMATION_RELATIVE_TOLERANCE,
                ),
            )
        )
        if accepted_update and posterior is not None:
            posterior_oracles_supported = (
                full_joint.supported and posterior.independent_supported
            )
            if posterior_oracles_supported:
                assert full_joint.dx is not None
                assert full_joint.p_plus is not None
                checks.extend(
                    (
                        _agreement(
                            "full_joint_dx",
                            full_joint.dx,
                            posterior.dx,
                            FULL_JOINT_ABSOLUTE_TOLERANCE,
                            FULL_JOINT_RELATIVE_TOLERANCE,
                        ),
                        _agreement(
                            "full_joint_P_plus",
                            full_joint.p_plus,
                            posterior.independent_p_plus,
                            FULL_JOINT_ABSOLUTE_TOLERANCE,
                            FULL_JOINT_RELATIVE_TOLERANCE,
                        ),
                    )
                )
            else:
                unsupported_reason = (
                    full_joint.reason
                    if not full_joint.supported
                    else posterior.independent_reason
                )
                checks.extend(
                    (
                        _unsupported_check(
                            "full_joint_dx", unsupported_reason
                        ),
                        _unsupported_check(
                            "full_joint_P_plus", unsupported_reason
                        ),
                    )
                )

    result = UpdateReconstruction(
        update_id=envelope.update_id,
        camera_timestamp=envelope.camera_timestamp,
        terminal_status=envelope.terminal_status,
        accepted_feature_ids=envelope.accepted_feature_ids,
        candidate_count=len(envelope.candidates),
        accepted_update=accepted_update,
        selected=selected,
        compressed=compressed,
        posterior=posterior,
        full_joint=full_joint,
        prior_covariance=prior_diagnostics,
        captured_posterior_covariance=captured_diagnostics,
        independent_posterior_covariance=independent_diagnostics,
        costs=costs,
        checks=tuple(checks),
    )
    if strict and not result.passed:
        failures = "; ".join(
            f"{check.name}: error={check.error:.17g} "
            f"tolerance={check.tolerance:.17g} {check.detail}".strip()
            for check in result.checks
            if check.applicable and not check.passed
        )
        raise ReconstructionError(f"update {envelope.update_id}: {failures}")
    return result


def iter_reconstructions(
    path: os.PathLike[str] | str, *, strict: bool = True
) -> Iterator[UpdateReconstruction]:
    """Stream strict reconstructions while preserving reader EOF validation."""

    with open_capture(path) as capture:
        yield from (
            reconstruct_update(envelope, strict=strict) for envelope in capture
        )


def validate_capture_reconstruction(
    path: os.PathLike[str] | str, *, strict: bool = True
) -> CaptureReconstructionSummary:
    update_count = 0
    candidate_count = 0
    accepted_track_count = 0
    accepted_update_count = 0
    zero_candidate_count = 0
    no_update_count = 0
    independent_covariance_supported_count = 0
    independent_covariance_unsupported_count = 0
    full_joint_supported_count = 0
    full_joint_unsupported_count = 0
    unsupported_check_count = 0
    unsupported_update_count = 0
    unsupported_reasons: Counter[str] = Counter()
    failed_check_count = 0
    maximum_check_ratio = 0.0
    charged_visual_total_ns = 0
    capture_record_construction_ns = 0
    capture_serialization_ns = 0
    capture_output_total_ns = 0
    with open_capture(path) as capture:
        header = capture.header
        for envelope in capture:
            result = reconstruct_update(envelope, strict=strict)
            update_count += 1
            candidate_count += result.candidate_count
            accepted_track_count += len(result.accepted_feature_ids)
            accepted_update_count += result.accepted_update
            zero_candidate_count += result.candidate_count == 0
            no_update_count += not result.accepted_update
            independent_checks = tuple(
                check
                for check in result.checks
                if check.name == "independent_covariance_P_plus"
            )
            full_joint_checks = tuple(
                check for check in result.checks if check.name == "full_joint_dx"
            )
            independent_covariance_supported_count += sum(
                check.applicable for check in independent_checks
            )
            independent_covariance_unsupported_count += sum(
                not check.applicable for check in independent_checks
            )
            full_joint_supported_count += sum(
                check.applicable for check in full_joint_checks
            )
            full_joint_unsupported_count += sum(
                not check.applicable for check in full_joint_checks
            )
            unsupported_checks = tuple(
                check for check in result.checks if not check.applicable
            )
            unsupported_check_count += len(unsupported_checks)
            unsupported_update_count += bool(unsupported_checks)
            unsupported_reasons.update(
                {
                    check.unsupported_reason
                    for check in unsupported_checks
                    if check.unsupported_reason
                }
            )
            failed_check_count += sum(
                check.applicable and not check.passed for check in result.checks
            )
            finite_ratios = [
                check.ratio
                for check in result.checks
                if check.applicable and math.isfinite(check.ratio)
            ]
            if finite_ratios:
                maximum_check_ratio = max(maximum_check_ratio, max(finite_ratios))
            elif any(
                check.applicable and not check.passed for check in result.checks
            ):
                maximum_check_ratio = math.inf
            charged_visual_total_ns += result.costs.charged_visual_total_ns
            capture_record_construction_ns += (
                result.costs.capture_record_construction_ns
            )
            capture_serialization_ns += result.costs.capture_serialization_ns
            capture_output_total_ns += result.costs.capture_output_total_ns

    if update_count == 0:
        raise ReconstructionError("capture contains no update envelopes")

    return CaptureReconstructionSummary(
        path=str(Path(path).resolve()),
        source_commit=header.source_commit,
        config_sha256=header.config_sha256,
        run_id=header.run_id,
        sequence_id=header.sequence_id,
        update_count=update_count,
        candidate_count=candidate_count,
        accepted_track_count=accepted_track_count,
        accepted_update_count=accepted_update_count,
        zero_candidate_count=zero_candidate_count,
        no_update_count=no_update_count,
        independent_covariance_supported_count=(
            independent_covariance_supported_count
        ),
        independent_covariance_unsupported_count=(
            independent_covariance_unsupported_count
        ),
        full_joint_supported_count=full_joint_supported_count,
        full_joint_unsupported_count=full_joint_unsupported_count,
        unsupported_check_count=unsupported_check_count,
        unsupported_update_count=unsupported_update_count,
        unsupported_reasons=tuple(sorted(unsupported_reasons.items())),
        failed_check_count=failed_check_count,
        maximum_check_ratio=maximum_check_ratio,
        charged_visual_total_ns=charged_visual_total_ns,
        capture_record_construction_ns=capture_record_construction_ns,
        capture_serialization_ns=capture_serialization_ns,
        capture_output_total_ns=capture_output_total_ns,
        strict_validation=failed_check_count == 0,
    )


def _json_line(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strictly reconstruct full updates from Schema-2 captures"
    )
    parser.add_argument("captures", nargs="+")
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="report every check instead of stopping at the first failed update",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        summaries = [
            validate_capture_reconstruction(
                capture, strict=not args.allow_failures
            )
            for capture in args.captures
        ]
        valid = all(summary.strict_validation for summary in summaries)
        print(
            _json_line(
                {
                    "captures": [summary.to_json() for summary in summaries],
                    "status": "valid" if valid else "invalid",
                }
            )
        )
        return 0 if valid else 1
    except (CaptureValidationError, ReconstructionError, OSError) as exc:
        print(_json_line({"error": str(exc), "status": "invalid"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
