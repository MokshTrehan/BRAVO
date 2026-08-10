#!/usr/bin/env python3
"""Deterministic offline information-budget policies for Schema-2 captures.

This module deliberately separates three deployability classes:

* pre-factor policies use only the causal candidate snapshot;
* partial-cost policies consume and charge geometry before ranking; and
* post-factor policies are offline upper bounds because they inspect reduced
  factors or their measured construction cost.

The evaluator never changes estimator state.  A frozen-policy document records
that its cost model was fitted only on TUM-VI; the EuRoC replication entry point
rejects any document without that provenance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import sys
from array import array
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

import numpy as np


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from experiments.anytime_information.capture_reader import (  # noqa: E402
    CandidateRecord,
    TrackRecord,
    UpdateEnvelope,
    open_capture,
)
from scripts.analysis.anytime_information_study import (  # noqa: E402
    ReconstructionError,
    UpdateReconstruction,
    _as_array,
    _expand_jacobian,
    _prior_support,
    covariance_diagnostics,
    reconstruct_update,
)


TRACK_FRACTIONS = (0.20, 0.30, 0.40, 0.50, 0.60, 0.75, 1.00)
ROW_FRACTIONS = TRACK_FRACTIONS
COST_FRACTIONS = (0.25, 0.50, 0.75, 1.00)
DEFAULT_RANDOM_SEED = 1729
SPATIAL_GRID_SIDE = 4
POLICY_POSTERIOR_ROUNDING_MULTIPLIER = 256.0
POLICY_POSTERIOR_ABSOLUTE_FLOOR = 1.0e-12

STATE_BLOCK_IMU = 1
STATE_BLOCK_CLONE = 10


class PolicyEvaluationError(ValueError):
    """A policy, budget, or dataset-role contract was violated."""


def _policy_posterior_agreement_tolerance(
    reference: np.ndarray,
    system_dimension: int,
    innovation_condition: float,
) -> float:
    """Scaled forward-error allowance for fast-vs-scalar policy validation.

    This is intentionally separate from the strict production reconstruction
    gate.  The factor ``eps * dimension * cond(S) * scale`` is the standard
    first-order floating-point sensitivity of the Cholesky/GEMM computation;
    256 covers the two solves and Joseph-form products while retaining roughly
    twelve or more relative digits for well-conditioned policy systems.
    """

    values = np.asarray(reference, dtype=np.float64)
    if (
        system_dimension <= 0
        or not math.isfinite(innovation_condition)
        or innovation_condition < 1.0
        or not np.all(np.isfinite(values))
    ):
        raise PolicyEvaluationError("policy posterior tolerance inputs are invalid")
    scale = max(1.0, float(np.max(np.abs(values))) if values.size else 0.0)
    return max(
        POLICY_POSTERIOR_ABSOLUTE_FLOOR,
        POLICY_POSTERIOR_ROUNDING_MULTIPLIER
        * np.finfo(np.float64).eps
        * system_dimension
        * innovation_condition
        * scale,
    )


class Deployability(str, Enum):
    PRE_FACTOR = "pre_factor"
    PARTIAL_COST = "partial_cost"
    POST_FACTOR_ORACLE = "post_factor_oracle"


class BudgetKind(str, Enum):
    TRACK = "track"
    ROW = "row"
    COST = "cost"


class DatasetRole(str, Enum):
    TUM_VI_TRAIN = "tum_vi_train"
    TUM_VI_HELD_OUT = "tum_vi_held_out"
    EUROC_REPLICATION = "euroc_replication"


@dataclass(frozen=True)
class PolicySpec:
    name: str
    deployability: Deployability
    ranking: str
    causal_fields: Tuple[str, ...]
    description: str

    def __post_init__(self) -> None:
        if self.deployability == Deployability.PRE_FACTOR and len(self.causal_fields) > 2:
            raise PolicyEvaluationError(
                f"pre-factor policy {self.name!r} uses more than two fields"
            )

    def to_json(self) -> Dict[str, Any]:
        return {
            "causal_fields": list(self.causal_fields),
            "deployability": self.deployability.value,
            "description": self.description,
            "name": self.name,
            "ranking": self.ranking,
        }


POLICIES: Tuple[PolicySpec, ...] = (
    PolicySpec(
        "original_order",
        Deployability.PRE_FACTOR,
        "original",
        ("candidate_order",),
        "Production candidate order control.",
    ),
    PolicySpec(
        "seeded_random",
        Deployability.PRE_FACTOR,
        "random",
        ("feature_id",),
        "SHA-256 keyed seeded-random control.",
    ),
    PolicySpec(
        "observation_count",
        Deployability.PRE_FACTOR,
        "descending",
        ("cleaned_observation_count",),
        "Longest cleaned track first.",
    ),
    PolicySpec(
        "track_age",
        Deployability.PRE_FACTOR,
        "descending",
        ("track_age",),
        "Oldest causal track first.",
    ),
    PolicySpec(
        "true_2d_parallax",
        Deployability.PRE_FACTOR,
        "descending",
        ("same_camera_2d_parallax",),
        "Maximum same-camera angle between raw normalized bearings.",
    ),
    PolicySpec(
        "image_motion",
        Deployability.PRE_FACTOR,
        "descending",
        ("same_camera_pixel_motion",),
        "Maximum same-camera raw-pixel displacement.",
    ),
    PolicySpec(
        "spatial_round_robin",
        Deployability.PRE_FACTOR,
        "spatial_round_robin",
        ("final_image_cell",),
        "Round-robin over camera and fixed image-grid cells.",
    ),
    PolicySpec(
        "observation_count_then_parallax",
        Deployability.PRE_FACTOR,
        "lexicographic_descending",
        ("cleaned_observation_count", "same_camera_2d_parallax"),
        "Two-field lexicographic causal control.",
    ),
    PolicySpec(
        "track_age_then_image_motion",
        Deployability.PRE_FACTOR,
        "lexicographic_descending",
        ("track_age", "same_camera_pixel_motion"),
        "Two-field lexicographic causal control.",
    ),
    PolicySpec(
        "geometry_parallax",
        Deployability.PARTIAL_COST,
        "geometry_descending",
        ("triangulated_3d_parallax",),
        "Triangulated 3-D parallax after charging geometry.",
    ),
    PolicySpec(
        "reduced_row_efficiency",
        Deployability.POST_FACTOR_ORACLE,
        "post_descending",
        ("prior_whitened_trace", "reduced_rows"),
        "Prior-whitened trace information per retained row.",
    ),
    PolicySpec(
        "trace_information",
        Deployability.POST_FACTOR_ORACLE,
        "post_descending",
        ("prior_whitened_trace",),
        "Per-track prior-whitened trace information.",
    ),
    PolicySpec(
        "logdet_information",
        Deployability.POST_FACTOR_ORACLE,
        "post_descending",
        ("prior_whitened_logdet",),
        "Per-track logdet information utility.",
    ),
    PolicySpec(
        "information_per_measured_cost",
        Deployability.POST_FACTOR_ORACLE,
        "post_descending",
        ("prior_whitened_trace", "measured_track_cost"),
        "Trace information divided by measured factor-pipeline cost.",
    ),
    PolicySpec(
        "greedy_marginal_information_per_cost",
        Deployability.POST_FACTOR_ORACLE,
        "greedy_marginal_logdet_per_cost",
        ("reduced_factor", "measured_track_cost"),
        "Greedy marginal logdet information per measured cost.",
    ),
)
POLICY_BY_NAME = {policy.name: policy for policy in POLICIES}


@dataclass(frozen=True)
class BudgetSpec:
    kind: BudgetKind
    fraction: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.fraction) or not (0.0 < self.fraction <= 1.0):
            raise PolicyEvaluationError("budget fraction must be in (0, 1]")

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.fraction:.2f}"

    def to_json(self) -> Dict[str, Any]:
        return {"fraction": self.fraction, "kind": self.kind.value}


DEFAULT_BUDGETS: Tuple[BudgetSpec, ...] = tuple(
    [BudgetSpec(BudgetKind.TRACK, value) for value in TRACK_FRACTIONS]
    + [BudgetSpec(BudgetKind.ROW, value) for value in ROW_FRACTIONS]
    + [BudgetSpec(BudgetKind.COST, value) for value in COST_FRACTIONS]
)


@dataclass(frozen=True)
class CandidateStageCost:
    prefilter_ns: int
    geometry_ns: int
    raw_factor_ns: int
    reduction_ns: int
    gate_ns: int
    accumulation_ns: int

    @property
    def post_prefilter_ns(self) -> int:
        return (
            self.geometry_ns
            + self.raw_factor_ns
            + self.reduction_ns
            + self.gate_ns
            + self.accumulation_ns
        )

    @property
    def post_geometry_ns(self) -> int:
        return (
            self.raw_factor_ns
            + self.reduction_ns
            + self.gate_ns
            + self.accumulation_ns
        )

    @property
    def total_ns(self) -> int:
        return self.prefilter_ns + self.post_prefilter_ns


@dataclass(frozen=True)
class CandidateView:
    ordinal: int
    feature_id: int
    candidate: CandidateRecord
    track: TrackRecord
    accepted: bool
    rows: int
    full_h: np.ndarray
    residual: np.ndarray
    information: np.ndarray
    gradient: np.ndarray
    prior_whitened_trace: float
    prior_whitened_logdet: float
    costs: CandidateStageCost
    parallax_2d: float
    image_motion: float
    final_cell: Tuple[int, int, int]


@dataclass(frozen=True)
class CostModel:
    feature_names: Tuple[str, ...]
    post_prefilter_coefficients: Tuple[float, ...]
    post_geometry_coefficients: Tuple[float, ...]
    training_sequences: Tuple[str, ...]
    training_dataset: str = "tum_vi"

    def __post_init__(self) -> None:
        expected = len(self.feature_names)
        if self.feature_names != COST_FEATURE_NAMES:
            raise PolicyEvaluationError("cost-model feature contract is not canonical")
        if (
            len(self.post_prefilter_coefficients) != expected
            or len(self.post_geometry_coefficients) != expected
        ):
            raise PolicyEvaluationError("cost-model coefficient dimensions differ")
        if not all(
            math.isfinite(value)
            for value in self.post_prefilter_coefficients
            + self.post_geometry_coefficients
        ):
            raise PolicyEvaluationError("cost-model coefficients must be finite")
        if self.training_dataset != "tum_vi":
            raise PolicyEvaluationError("cost model may only be fitted on TUM-VI")

    def _predict_features(
        self, features: np.ndarray, coefficients: Sequence[float]
    ) -> float:
        value = float(np.dot(np.asarray(coefficients), features))
        return max(0.0, value) if math.isfinite(value) else 0.0

    def _predict(self, candidate: CandidateRecord, coefficients: Sequence[float]) -> float:
        return self._predict_features(_cost_features(candidate), coefficients)

    def predict_post_prefilter_ns(self, candidate: CandidateRecord) -> float:
        return self._predict(candidate, self.post_prefilter_coefficients)

    def predict_post_geometry_ns(self, candidate: CandidateRecord) -> float:
        return self._predict(candidate, self.post_geometry_coefficients)

    def to_json(self) -> Dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "post_geometry_coefficients": list(self.post_geometry_coefficients),
            "post_prefilter_coefficients": list(self.post_prefilter_coefficients),
            "training_dataset": self.training_dataset,
            "training_sequences": list(self.training_sequences),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "CostModel":
        return cls(
            feature_names=tuple(str(item) for item in value["feature_names"]),
            post_prefilter_coefficients=tuple(
                float(item) for item in value["post_prefilter_coefficients"]
            ),
            post_geometry_coefficients=tuple(
                float(item) for item in value["post_geometry_coefficients"]
            ),
            training_sequences=tuple(
                str(item) for item in value["training_sequences"]
            ),
            training_dataset=str(value["training_dataset"]),
        )


COST_FEATURE_NAMES = (
    "intercept",
    "cleaned_observation_count",
    "track_age_seconds",
    "same_camera_2d_parallax_rad",
    "same_camera_pixel_motion",
)


def _bearing_parallax(candidate: CandidateRecord) -> float:
    by_camera: Dict[int, List[np.ndarray]] = {}
    for observation in candidate.observations:
        if not observation.normalized_available:
            continue
        bearing = np.asarray(
            (observation.normalized_u, observation.normalized_v, 1.0),
            dtype=np.float64,
        )
        norm = float(np.linalg.norm(bearing))
        if norm > np.finfo(np.float64).tiny and math.isfinite(norm):
            by_camera.setdefault(observation.camera_id, []).append(bearing / norm)
    maximum = 0.0
    for bearings in by_camera.values():
        for left in range(len(bearings)):
            for right in range(left + 1, len(bearings)):
                cosine = float(np.dot(bearings[left], bearings[right]))
                maximum = max(maximum, math.acos(max(-1.0, min(1.0, cosine))))
    return maximum


def _pixel_motion(candidate: CandidateRecord) -> float:
    by_camera: Dict[int, List[np.ndarray]] = {}
    for observation in candidate.observations:
        by_camera.setdefault(observation.camera_id, []).append(
            np.asarray((observation.raw_u, observation.raw_v), dtype=np.float64)
        )
    maximum = 0.0
    for pixels in by_camera.values():
        for left in range(len(pixels)):
            for right in range(left + 1, len(pixels)):
                maximum = max(maximum, float(np.linalg.norm(pixels[left] - pixels[right])))
    return maximum


def _cost_features(candidate: CandidateRecord) -> np.ndarray:
    return np.asarray(
        (
            1.0,
            float(candidate.cleaned_observation_count),
            candidate.track_age if candidate.time_range_available else 0.0,
            _bearing_parallax(candidate),
            _pixel_motion(candidate),
        ),
        dtype=np.float64,
    )


def _candidate_final_cell(
    envelope: UpdateEnvelope, candidate: CandidateRecord
) -> Tuple[int, int, int]:
    if not candidate.observations:
        return (2**31 - 1, SPATIAL_GRID_SIDE, SPATIAL_GRID_SIDE)
    latest_index, latest = max(
        enumerate(candidate.observations), key=lambda item: (item[1].timestamp, item[0])
    )
    del latest_index
    cameras = {camera.camera_id: camera for camera in envelope.cameras}
    camera = cameras.get(latest.camera_id)
    if camera is None:
        return (latest.camera_id, SPATIAL_GRID_SIDE, SPATIAL_GRID_SIDE)
    x = min(
        SPATIAL_GRID_SIDE - 1,
        max(0, int(SPATIAL_GRID_SIDE * latest.raw_u / float(camera.width))),
    )
    y = min(
        SPATIAL_GRID_SIDE - 1,
        max(0, int(SPATIAL_GRID_SIDE * latest.raw_v / float(camera.height))),
    )
    return (latest.camera_id, y, x)


def _logdet_identity_plus(information: np.ndarray) -> float:
    symmetric = 0.5 * (information + information.T)
    eigenvalues = np.linalg.eigvalsh(symmetric)
    if not np.all(np.isfinite(eigenvalues)):
        raise PolicyEvaluationError("information eigenspectrum is non-finite")
    return float(np.sum(np.log1p(np.maximum(0.0, eigenvalues))))


def build_candidate_views(envelope: UpdateEnvelope) -> Tuple[CandidateView, ...]:
    if len(envelope.candidates) != len(envelope.tracks):
        raise PolicyEvaluationError("candidate and track counts differ")
    prior = _as_array(envelope.p_minus)
    factor, diagnostics = _prior_support(prior)
    if factor is None or not diagnostics.scaled_psd:
        raise PolicyEvaluationError(
            f"update {envelope.update_id} has no valid prior support"
        )
    views: List[CandidateView] = []
    for candidate, track in zip(envelope.candidates, envelope.tracks):
        accepted = track.accepted_for_global_system
        if accepted:
            local_h = _as_array(track.reduced_a)
            full_h = _expand_jacobian(local_h, track.layout, prior.shape[0])
            residual = _as_array(track.reduced_b).reshape(-1)
            information = full_h.T @ full_h / track.noise_variance
            information = 0.5 * (information + information.T)
            gradient = full_h.T @ residual / track.noise_variance
            supported_information = factor.T @ information @ factor
            supported_information = 0.5 * (
                supported_information + supported_information.T
            )
            information_trace = float(np.trace(supported_information))
            information_logdet = _logdet_identity_plus(supported_information)
            rows = track.global_row_count
        else:
            full_h = np.zeros((0, prior.shape[0]), dtype=np.float64)
            residual = np.zeros(0, dtype=np.float64)
            information = np.zeros_like(prior)
            gradient = np.zeros(prior.shape[0], dtype=np.float64)
            information_trace = 0.0
            information_logdet = 0.0
            rows = 0
        costs = CandidateStageCost(
            prefilter_ns=candidate.prefilter_duration_ns,
            geometry_ns=track.geometry_duration_ns,
            raw_factor_ns=track.raw_factor_duration_ns,
            reduction_ns=track.reduction_duration_ns,
            gate_ns=track.gate_duration_ns,
            accumulation_ns=track.accumulation_duration_ns,
        )
        views.append(
            CandidateView(
                ordinal=candidate.ordinal,
                feature_id=candidate.feature_id,
                candidate=candidate,
                track=track,
                accepted=accepted,
                rows=rows,
                full_h=full_h,
                residual=residual,
                information=information,
                gradient=gradient,
                prior_whitened_trace=information_trace,
                prior_whitened_logdet=information_logdet,
                costs=costs,
                parallax_2d=_bearing_parallax(candidate),
                image_motion=_pixel_motion(candidate),
                final_cell=_candidate_final_cell(envelope, candidate),
            )
        )
    return tuple(views)


def _random_key(
    seed: int, sequence_id: str, update_id: int, feature_id: int
) -> bytes:
    payload = (
        f"schurvio-policy-v1\0{seed}\0{sequence_id}\0{update_id}\0{feature_id}"
    ).encode("utf-8")
    return hashlib.sha256(payload).digest()


def _spatial_round_robin(views: Sequence[CandidateView]) -> Tuple[int, ...]:
    buckets: Dict[Tuple[int, int, int], List[CandidateView]] = {}
    for view in views:
        buckets.setdefault(view.final_cell, []).append(view)
    for bucket in buckets.values():
        bucket.sort(key=lambda item: item.ordinal)
    keys = sorted(buckets)
    ordered: List[int] = []
    depth = 0
    while True:
        emitted = False
        for key in keys:
            bucket = buckets[key]
            if depth < len(bucket):
                ordered.append(bucket[depth].ordinal)
                emitted = True
        if not emitted:
            break
        depth += 1
    return tuple(ordered)


def _greedy_marginal_order(
    views: Sequence[CandidateView], prior_factor: np.ndarray
) -> Tuple[int, ...]:
    remaining = [view for view in views if view.accepted]
    accumulated = np.zeros(
        (prior_factor.shape[1], prior_factor.shape[1]), dtype=np.float64
    )
    current_utility = 0.0
    ordered: List[int] = []
    while remaining:
        best: CandidateView | None = None
        best_score = -math.inf
        best_information: np.ndarray | None = None
        best_utility = current_utility
        for view in remaining:
            contribution = prior_factor.T @ view.information @ prior_factor
            candidate_information = accumulated + contribution
            utility = _logdet_identity_plus(candidate_information)
            marginal = max(0.0, utility - current_utility)
            score = marginal / max(1.0, float(view.costs.post_prefilter_ns))
            if (
                best is None
                or score > best_score
                or (score == best_score and view.ordinal < best.ordinal)
            ):
                best = view
                best_score = score
                best_information = contribution
                best_utility = utility
        assert best is not None and best_information is not None
        ordered.append(best.ordinal)
        accumulated += best_information
        accumulated = 0.5 * (accumulated + accumulated.T)
        current_utility = best_utility
        remaining.remove(best)
    return tuple(ordered)


def rank_candidates(
    envelope: UpdateEnvelope,
    views: Sequence[CandidateView],
    policy: PolicySpec,
    *,
    sequence_id: str,
    seed: int = DEFAULT_RANDOM_SEED,
) -> Tuple[int, ...]:
    """Return deterministic candidate ordinals for one policy and callback."""

    if tuple(view.ordinal for view in views) != tuple(range(len(views))):
        raise PolicyEvaluationError("candidate views are not in ordinal order")
    if policy.name not in POLICY_BY_NAME or POLICY_BY_NAME[policy.name] != policy:
        raise PolicyEvaluationError(f"unknown or modified policy {policy.name!r}")
    eligible = (
        tuple(view for view in views if view.accepted)
        if policy.deployability == Deployability.POST_FACTOR_ORACLE
        else tuple(views)
    )
    if policy.ranking == "original":
        return tuple(view.ordinal for view in eligible)
    if policy.ranking == "random":
        return tuple(
            view.ordinal
            for view in sorted(
                eligible,
                key=lambda item: (
                    _random_key(seed, sequence_id, envelope.update_id, item.feature_id),
                    item.ordinal,
                ),
            )
        )
    if policy.ranking == "spatial_round_robin":
        return _spatial_round_robin(eligible)
    if policy.ranking == "geometry_descending":
        return tuple(
            view.ordinal
            for view in sorted(
                eligible,
                key=lambda item: (
                    -(
                        item.track.parallax_3d
                        if item.track.parallax_3d_available
                        else -math.inf
                    ),
                    item.ordinal,
                ),
            )
        )
    if policy.ranking == "greedy_marginal_logdet_per_cost":
        prior_factor, _ = _prior_support(_as_array(envelope.p_minus))
        if prior_factor is None:
            raise PolicyEvaluationError("greedy oracle requires valid prior support")
        return _greedy_marginal_order(eligible, prior_factor)

    def causal_value(view: CandidateView, field: str) -> float:
        if field == "cleaned_observation_count":
            return float(view.candidate.cleaned_observation_count)
        if field == "track_age":
            return view.candidate.track_age if view.candidate.time_range_available else 0.0
        if field == "same_camera_2d_parallax":
            return view.parallax_2d
        if field == "same_camera_pixel_motion":
            return view.image_motion
        raise PolicyEvaluationError(f"unsupported causal field {field!r}")

    if policy.ranking in ("descending", "lexicographic_descending"):
        return tuple(
            view.ordinal
            for view in sorted(
                eligible,
                key=lambda item: tuple(
                    -causal_value(item, field) for field in policy.causal_fields
                )
                + (item.ordinal,),
            )
        )
    if policy.ranking == "post_descending":
        def post_score(view: CandidateView) -> float:
            if policy.name == "reduced_row_efficiency":
                return view.prior_whitened_trace / max(1, view.rows)
            if policy.name == "trace_information":
                return view.prior_whitened_trace
            if policy.name == "logdet_information":
                return view.prior_whitened_logdet
            if policy.name == "information_per_measured_cost":
                return view.prior_whitened_trace / max(
                    1.0, float(view.costs.post_prefilter_ns)
                )
            raise PolicyEvaluationError(f"unsupported post-factor policy {policy.name}")

        return tuple(
            view.ordinal
            for view in sorted(eligible, key=lambda item: (-post_score(item), item.ordinal))
        )
    raise PolicyEvaluationError(f"unsupported ranking {policy.ranking!r}")


def fit_cost_model_from_envelopes(
    named_envelopes: Iterable[Tuple[str, UpdateEnvelope]],
) -> CostModel:
    """Fit the frozen linear stage-cost model from TUM-VI envelopes only."""

    features: List[np.ndarray] = []
    post_prefilter: List[float] = []
    post_geometry: List[float] = []
    sequences = set()
    for sequence_id, envelope in named_envelopes:
        sequences.add(sequence_id)
        if len(envelope.candidates) != len(envelope.tracks):
            raise PolicyEvaluationError("cost-model envelope is structurally incomplete")
        for candidate, track in zip(envelope.candidates, envelope.tracks):
            costs = CandidateStageCost(
                candidate.prefilter_duration_ns,
                track.geometry_duration_ns,
                track.raw_factor_duration_ns,
                track.reduction_duration_ns,
                track.gate_duration_ns,
                track.accumulation_duration_ns,
            )
            features.append(_cost_features(candidate))
            post_prefilter.append(float(costs.post_prefilter_ns))
            post_geometry.append(float(costs.post_geometry_ns))
    if not features:
        raise PolicyEvaluationError("cost-model training set has no candidates")
    design = np.vstack(features)
    first = np.asarray(post_prefilter, dtype=np.float64)
    second = np.asarray(post_geometry, dtype=np.float64)
    try:
        coefficients_first = np.linalg.lstsq(design, first, rcond=None)[0]
        coefficients_second = np.linalg.lstsq(design, second, rcond=None)[0]
    except np.linalg.LinAlgError as exc:
        raise PolicyEvaluationError("cost-model least-squares fit failed") from exc
    if not (
        np.all(np.isfinite(coefficients_first))
        and np.all(np.isfinite(coefficients_second))
    ):
        raise PolicyEvaluationError("cost-model fit is non-finite")
    return CostModel(
        feature_names=COST_FEATURE_NAMES,
        post_prefilter_coefficients=tuple(float(value) for value in coefficients_first),
        post_geometry_coefficients=tuple(float(value) for value in coefficients_second),
        training_sequences=tuple(sorted(sequences)),
    )


def fit_cost_model_from_captures(
    captures: Sequence[Tuple[str, os.PathLike[str] | str]],
) -> CostModel:
    if not captures:
        raise PolicyEvaluationError("at least one TUM-VI training capture is required")

    def envelopes() -> Iterator[Tuple[str, UpdateEnvelope]]:
        for sequence_id, path in captures:
            with open_capture(path) as capture:
                if capture.header.sequence_id != sequence_id:
                    raise PolicyEvaluationError(
                        f"capture sequence {capture.header.sequence_id!r} does not match "
                        f"declared {sequence_id!r}"
                    )
                for envelope in capture:
                    yield sequence_id, envelope

    return fit_cost_model_from_envelopes(envelopes())


@dataclass(frozen=True)
class BudgetSelection:
    ranked_ordinals: Tuple[int, ...]
    processed_ordinals: Tuple[int, ...]
    selected_ordinals: Tuple[int, ...]
    selected_feature_ids: Tuple[int, ...]
    selected_rows: int
    decision_cost_ns: float
    decision_cost_source: str


@dataclass(frozen=True)
class CandidateCostPrediction:
    post_prefilter_ns: float
    post_geometry_ns: float


def _predict_candidate_costs(
    views: Sequence[CandidateView], cost_model: CostModel
) -> Dict[int, CandidateCostPrediction]:
    predictions: Dict[int, CandidateCostPrediction] = {}
    for view in views:
        candidate = view.candidate
        features = np.asarray(
            (
                1.0,
                float(candidate.cleaned_observation_count),
                candidate.track_age if candidate.time_range_available else 0.0,
                view.parallax_2d,
                view.image_motion,
            ),
            dtype=np.float64,
        )
        predictions[view.ordinal] = CandidateCostPrediction(
            post_prefilter_ns=max(
                0.0,
                cost_model._predict_features(
                    features, cost_model.post_prefilter_coefficients
                ),
            ),
            post_geometry_ns=max(
                0.0,
                cost_model._predict_features(
                    features, cost_model.post_geometry_coefficients
                ),
            ),
        )
    return predictions


def _candidate_budget_cost(
    view: CandidateView,
    policy: PolicySpec,
    cost_model: CostModel,
    predictions: Mapping[int, CandidateCostPrediction] | None = None,
) -> Tuple[float, str]:
    prediction = predictions.get(view.ordinal) if predictions is not None else None
    if policy.deployability == Deployability.PRE_FACTOR:
        value = (
            prediction.post_prefilter_ns
            if prediction is not None
            else cost_model.predict_post_prefilter_ns(view.candidate)
        )
        return value, "predicted_post_prefilter"
    if policy.deployability == Deployability.PARTIAL_COST:
        value = (
            prediction.post_geometry_ns
            if prediction is not None
            else cost_model.predict_post_geometry_ns(view.candidate)
        )
        return value, "predicted_post_geometry"
    return float(view.costs.post_prefilter_ns), "measured_post_factor_oracle"


def select_budget(
    views: Sequence[CandidateView],
    ranking: Sequence[int],
    policy: PolicySpec,
    budget: BudgetSpec,
    cost_model: CostModel,
    *,
    full_global_cost_ns: float = 0.0,
    cost_predictions: Mapping[int, CandidateCostPrediction] | None = None,
) -> BudgetSelection:
    by_ordinal = {view.ordinal: view for view in views}
    if len(by_ordinal) != len(views) or any(
        ordinal not in by_ordinal for ordinal in ranking
    ):
        raise PolicyEvaluationError("ranking does not reference unique candidate views")
    ranked = tuple(int(ordinal) for ordinal in ranking)
    if len(set(ranked)) != len(ranked):
        raise PolicyEvaluationError("ranking contains duplicate candidates")
    full_rows = sum(view.rows for view in views if view.accepted)
    costs = [
        _candidate_budget_cost(
            by_ordinal[ordinal], policy, cost_model, cost_predictions
        )[0]
        for ordinal in ranked
    ]
    cost_source = (
        _candidate_budget_cost(
            by_ordinal[ranked[0]], policy, cost_model, cost_predictions
        )[1]
        if ranked
        else "none"
    )
    if budget.kind == BudgetKind.TRACK:
        limit = int(math.ceil(budget.fraction * len(ranked)))
        processed = list(ranked[:limit])
        selected = [ordinal for ordinal in processed if by_ordinal[ordinal].accepted]
        decision_cost = sum(costs[:limit])
    elif budget.kind == BudgetKind.ROW:
        target = int(math.ceil(budget.fraction * full_rows))
        processed = []
        selected = []
        used = 0
        for ordinal in ranked:
            view = by_ordinal[ordinal]
            processed.append(ordinal)
            if view.accepted and used + view.rows > target and budget.fraction < 1.0:
                break
            if view.accepted:
                selected.append(ordinal)
                used += view.rows
        decision_cost = sum(
            _candidate_budget_cost(
                by_ordinal[ordinal], policy, cost_model, cost_predictions
            )[0]
            for ordinal in processed
        )
    else:
        prefilter_sunk = float(sum(view.costs.prefilter_ns for view in views))
        if policy.deployability == Deployability.PRE_FACTOR:
            sunk = prefilter_sunk
        elif policy.deployability == Deployability.PARTIAL_COST:
            sunk = prefilter_sunk + float(
                sum(view.costs.geometry_ns for view in views)
            )
        else:
            sunk = prefilter_sunk + float(
                sum(view.costs.post_prefilter_ns for view in views)
            )
            costs = [0.0 for _ in ranked]
            cost_source = "post_factor_sunk_plus_row_attributed_global"
        if policy.deployability != Deployability.POST_FACTOR_ORACLE:
            cost_source += "_plus_row_attributed_global"
        row_costs = [
            (
                full_global_cost_ns * by_ordinal[ordinal].rows / full_rows
                if full_rows
                else 0.0
            )
            for ordinal in ranked
        ]
        total_cost = sunk + sum(costs) + full_global_cost_ns
        target = budget.fraction * total_cost
        processed = []
        selected = []
        decision_cost = sunk
        for index, (ordinal, candidate_cost, row_cost) in enumerate(
            zip(ranked, costs, row_costs)
        ):
            incremental = candidate_cost + row_cost
            if (
                budget.fraction < 1.0
                and decision_cost + incremental > target
            ):
                break
            processed.append(ordinal)
            decision_cost += incremental
            if by_ordinal[ordinal].accepted:
                selected.append(ordinal)
            if index + 1 == len(ranked):
                break
    selected_rows = sum(by_ordinal[ordinal].rows for ordinal in selected)
    return BudgetSelection(
        ranked_ordinals=ranked,
        processed_ordinals=tuple(processed),
        selected_ordinals=tuple(selected),
        selected_feature_ids=tuple(by_ordinal[ordinal].feature_id for ordinal in selected),
        selected_rows=selected_rows,
        decision_cost_ns=float(decision_cost),
        decision_cost_source=cost_source,
    )


@dataclass(frozen=True)
class InformationUtility:
    trace: float
    logdet_identity_plus: float
    effective_rank: int


@dataclass(frozen=True)
class _InformationSubspace:
    indices: np.ndarray
    prior_factor: np.ndarray


@dataclass(frozen=True)
class PosteriorSpectrum:
    trace: float
    log_pseudodeterminant: float
    effective_rank: int
    minimum_eigenvalue: float
    psd_tolerance: float


def _prepare_information_subspace(
    prior: np.ndarray, indices: Sequence[int]
) -> _InformationSubspace:
    if not indices:
        return _InformationSubspace(
            np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float64)
        )
    index = np.asarray(tuple(indices), dtype=np.int64)
    local_prior = prior[np.ix_(index, index)]
    factor, diagnostics = _prior_support(local_prior)
    if factor is None or not diagnostics.scaled_psd:
        raise PolicyEvaluationError("subspace prior has no valid PSD support")
    return _InformationSubspace(index, factor)


def _prepared_information_utility(
    information: np.ndarray, subspace: _InformationSubspace
) -> InformationUtility:
    if subspace.indices.size == 0:
        return InformationUtility(0.0, 0.0, 0)
    local_information = information[np.ix_(subspace.indices, subspace.indices)]
    factor = subspace.prior_factor
    supported = factor.T @ local_information @ factor
    supported = 0.5 * (supported + supported.T)
    eigenvalues = np.linalg.eigvalsh(supported)
    if not np.all(np.isfinite(eigenvalues)):
        raise PolicyEvaluationError("subspace information is non-finite")
    maximum = float(np.max(np.abs(eigenvalues))) if eigenvalues.size else 0.0
    floor = max(1, supported.shape[0]) * np.finfo(np.float64).eps * maximum
    positive = eigenvalues > floor
    return InformationUtility(
        trace=float(np.sum(eigenvalues)),
        logdet_identity_plus=float(np.sum(np.log1p(np.maximum(0.0, eigenvalues)))),
        effective_rank=int(np.count_nonzero(positive)),
    )


def _information_utility(
    information: np.ndarray, prior: np.ndarray, indices: Sequence[int]
) -> InformationUtility:
    return _prepared_information_utility(
        information, _prepare_information_subspace(prior, indices)
    )


def _posterior_spectrum(covariance: np.ndarray) -> PosteriorSpectrum:
    diagnostics = covariance_diagnostics(covariance)
    if not diagnostics.finite or not diagnostics.symmetric or not diagnostics.scaled_psd:
        raise PolicyEvaluationError("posterior covariance is not finite/symmetric/PSD")
    eigenvalues = np.linalg.eigvalsh(0.5 * (covariance + covariance.T))
    positive = eigenvalues > diagnostics.psd_tolerance
    return PosteriorSpectrum(
        trace=float(np.trace(covariance)),
        log_pseudodeterminant=float(np.sum(np.log(eigenvalues[positive]))),
        effective_rank=int(np.count_nonzero(positive)),
        minimum_eigenvalue=diagnostics.minimum_eigenvalue,
        psd_tolerance=diagnostics.psd_tolerance,
    )


def _retention(subset: float, full: float) -> float:
    floor = 1.0e-14 * max(1.0, abs(full))
    if abs(full) <= floor:
        return 1.0
    return float(subset / full)


def _semantic_subspaces(envelope: UpdateEnvelope) -> Dict[str, Tuple[int, ...]]:
    pose: List[int] = []
    velocity: List[int] = []
    clones: List[int] = []
    for block in envelope.state_blocks:
        block_indices = list(range(block.offset, block.offset + block.dimension))
        if block.block_type == STATE_BLOCK_IMU:
            pose.extend(block_indices[: min(6, block.dimension)])
            if block.dimension > 6:
                velocity.extend(block_indices[6 : min(9, block.dimension)])
        elif block.block_type == STATE_BLOCK_CLONE:
            clones.extend(block_indices)
    combined = sorted(set(pose + velocity + clones))
    return {
        "pose": tuple(pose),
        "velocity": tuple(velocity),
        "clone": tuple(clones),
        "pose_velocity_clone": tuple(combined),
        "full_state": tuple(range(envelope.p_minus.rows)),
    }


_BLOCK_TYPE_NAMES = {
    0: "unknown",
    1: "imu",
    2: "imu_gyro_intrinsics",
    3: "imu_accel_intrinsics",
    4: "imu_g_sensitivity",
    5: "gyro_to_imu_rotation",
    6: "accel_to_imu_rotation",
    7: "camera_time_offset",
    8: "camera_extrinsics",
    9: "camera_intrinsics",
    10: "clone",
    11: "slam_landmark",
}


def _blockwise_correction_differences(
    envelope: UpdateEnvelope, difference: np.ndarray
) -> Dict[str, float]:
    result: Dict[str, float] = {}
    for block in envelope.state_blocks:
        values = difference[block.offset : block.offset + block.dimension]
        base = _BLOCK_TYPE_NAMES.get(block.block_type, f"type_{block.block_type}")
        if block.key_type == 1:
            key = f"{base}:{block.key_double:.17g}"
        elif block.key_type in (2, 3):
            key = f"{base}:{block.key_u64}"
        else:
            key = base
        result[key] = float(np.linalg.norm(values))
        if block.block_type == STATE_BLOCK_IMU and block.dimension >= 15:
            names = (
                ("imu_orientation", 0, 3),
                ("imu_position", 3, 6),
                ("imu_velocity", 6, 9),
                ("imu_gyro_bias", 9, 12),
                ("imu_accel_bias", 12, 15),
            )
            for name, begin, end in names:
                result[name] = float(np.linalg.norm(values[begin:end]))
    return dict(sorted(result.items()))


def _subset_posterior(
    prior: np.ndarray,
    selected: Sequence[CandidateView],
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute a policy subset posterior with fast covariance-form BLAS.

    Phase-3 reconstruction deliberately uses scalar-ordered products to match
    the frozen C++ production path bit-for-bit.  Policy evaluation instead
    solves many mathematically equivalent hypothetical subsets, so it uses one
    full ``P H.T`` product and one Cholesky factorization of ``H P H.T + R``.
    ``R`` is diagonal by the captured reduced-factor contract.  The covariance
    remains the independent Joseph form.  Reproducible study invocations pin
    BLAS/LAPACK to one thread before importing NumPy.
    """

    if not selected:
        return np.zeros(prior.shape[0], dtype=np.float64), prior.copy()
    prior = np.asarray(prior, dtype=np.float64, order="C")
    if prior.ndim != 2 or prior.shape[0] != prior.shape[1]:
        raise PolicyEvaluationError("policy subset prior must be square")
    state_dimension = prior.shape[0]
    h = np.ascontiguousarray(
        np.vstack([view.full_h for view in selected]), dtype=np.float64
    )
    residual = np.ascontiguousarray(
        np.concatenate([view.residual for view in selected]), dtype=np.float64
    )
    variances = np.concatenate(
        [np.full(view.rows, view.track.noise_variance) for view in selected]
    )
    if (
        h.ndim != 2
        or h.shape != (residual.size, state_dimension)
        or variances.shape != residual.shape
        or not np.all(np.isfinite(prior))
        or not np.all(np.isfinite(h))
        or not np.all(np.isfinite(residual))
        or not np.all(np.isfinite(variances))
        or np.any(variances <= 0.0)
    ):
        raise PolicyEvaluationError("policy subset system is invalid")

    cross = prior @ h.T
    innovation = h @ cross
    innovation = 0.5 * (innovation + innovation.T)
    innovation.flat[:: innovation.shape[0] + 1] += variances
    try:
        lower = np.linalg.cholesky(innovation)
        intermediate = np.linalg.solve(lower, cross.T)
        solution = np.linalg.solve(lower.T, intermediate)
    except np.linalg.LinAlgError as exc:
        raise PolicyEvaluationError(
            "policy subset innovation Cholesky solve failed"
        ) from exc
    gain = solution.T
    dx = gain @ residual
    identity_minus_kh = np.eye(state_dimension, dtype=np.float64) - gain @ h
    p_plus = (
        identity_minus_kh @ prior @ identity_minus_kh.T
        + (gain * variances[None, :]) @ gain.T
    )
    p_plus = 0.5 * (p_plus + p_plus.T)
    if not np.all(np.isfinite(dx)) or not np.all(np.isfinite(p_plus)):
        raise PolicyEvaluationError("policy subset posterior is non-finite")
    return dx, p_plus


@dataclass(frozen=True)
class _MahalanobisReference:
    retained_vectors: np.ndarray
    retained_eigenvalues: np.ndarray


@dataclass(frozen=True)
class _PolicyUpdateContext:
    prior: np.ndarray
    full_information: np.ndarray
    full_dx: np.ndarray
    full_p_plus: np.ndarray
    subspaces: Mapping[str, Tuple[int, ...]]
    subspace_supports: Mapping[str, _InformationSubspace]
    full_utilities: Mapping[str, InformationUtility]
    full_spectrum: PosteriorSpectrum
    mahalanobis_reference: _MahalanobisReference


@dataclass(frozen=True)
class _SubsetDiagnostics:
    utilities: Mapping[str, InformationUtility]
    full_utility: InformationUtility
    spectrum: PosteriorSpectrum
    correction_mahalanobis: float
    correction_l2: float
    blockwise_correction_l2: Mapping[str, float]
    covariance_conservative: bool
    covariance_difference_min_eigenvalue: float
    covariance_difference_psd_tolerance: float


def _prepare_mahalanobis_reference(covariance: np.ndarray) -> _MahalanobisReference:
    symmetric = 0.5 * (covariance + covariance.T)
    diagnostics = covariance_diagnostics(symmetric)
    if not diagnostics.scaled_psd:
        raise PolicyEvaluationError("Mahalanobis reference covariance is not PSD")
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    retained = eigenvalues > diagnostics.psd_tolerance
    return _MahalanobisReference(eigenvectors[:, retained], eigenvalues[retained])


def _prepared_mahalanobis_distance(
    difference: np.ndarray, reference: _MahalanobisReference
) -> float:
    if reference.retained_eigenvalues.size == 0:
        return 0.0 if not np.any(difference) else math.inf
    coordinates = reference.retained_vectors.T @ difference
    squared = float(
        np.sum(
            coordinates
            * coordinates
            / reference.retained_eigenvalues
        )
    )
    return math.sqrt(max(0.0, squared))


def _prepare_policy_update_context(
    envelope: UpdateEnvelope, reconstruction: UpdateReconstruction
) -> _PolicyUpdateContext:
    prior = _as_array(envelope.p_minus)
    if reconstruction.posterior is None:
        full_dx = np.zeros(prior.shape[0], dtype=np.float64)
        full_p_plus = prior.copy()
    else:
        full_dx = reconstruction.posterior.dx
        full_p_plus = reconstruction.posterior.independent_p_plus
    subspaces = _semantic_subspaces(envelope)
    subspace_supports = {
        name: _prepare_information_subspace(prior, indices)
        for name, indices in subspaces.items()
    }
    full_information = reconstruction.selected.information
    full_utilities = {
        name: _prepared_information_utility(full_information, support)
        for name, support in subspace_supports.items()
    }
    return _PolicyUpdateContext(
        prior=prior,
        full_information=full_information,
        full_dx=full_dx,
        full_p_plus=full_p_plus,
        subspaces=subspaces,
        subspace_supports=subspace_supports,
        full_utilities=full_utilities,
        full_spectrum=_posterior_spectrum(full_p_plus),
        mahalanobis_reference=_prepare_mahalanobis_reference(full_p_plus),
    )


def _prepare_subset_diagnostics(
    envelope: UpdateEnvelope,
    selected: Sequence[CandidateView],
    context: _PolicyUpdateContext,
) -> _SubsetDiagnostics:
    subset_information = sum(
        (view.information for view in selected), np.zeros_like(context.prior)
    )
    subset_information = 0.5 * (subset_information + subset_information.T)
    subset_dx, subset_p_plus = _subset_posterior(context.prior, selected)
    utilities = {
        name: _prepared_information_utility(subset_information, support)
        for name, support in context.subspace_supports.items()
    }
    spectrum = _posterior_spectrum(subset_p_plus)
    correction_difference = subset_dx - context.full_dx
    covariance_difference = 0.5 * (
        subset_p_plus
        - context.full_p_plus
        + (subset_p_plus - context.full_p_plus).T
    )
    difference_diagnostics = covariance_diagnostics(covariance_difference)
    return _SubsetDiagnostics(
        utilities=utilities,
        full_utility=utilities["full_state"],
        spectrum=spectrum,
        correction_mahalanobis=_prepared_mahalanobis_distance(
            correction_difference, context.mahalanobis_reference
        ),
        correction_l2=float(np.linalg.norm(correction_difference)),
        blockwise_correction_l2=_blockwise_correction_differences(
            envelope, correction_difference
        ),
        covariance_conservative=(
            difference_diagnostics.finite
            and difference_diagnostics.symmetric
            and difference_diagnostics.scaled_psd
        ),
        covariance_difference_min_eigenvalue=(
            difference_diagnostics.minimum_eigenvalue
        ),
        covariance_difference_psd_tolerance=difference_diagnostics.psd_tolerance,
    )


@dataclass(frozen=True)
class PolicyUpdateResult:
    dataset_role: str
    sequence_id: str
    update_id: int
    camera_timestamp: float
    policy: str
    deployability: str
    budget_kind: str
    budget_fraction: float
    candidate_count: int
    full_accepted_tracks: int
    processed_candidates: int
    selected_tracks: int
    full_rows: int
    selected_rows: int
    selected_feature_ids: Tuple[int, ...]
    pose_information_retention: float
    velocity_information_retention: float
    clone_information_retention: float
    pose_velocity_clone_information_retention: float
    full_state_information_retention: float
    full_state_logdet_information_retention: float
    subset_information_trace: float
    full_information_trace: float
    subset_information_logdet: float
    full_information_logdet: float
    subset_posterior_trace: float
    full_posterior_trace: float
    subset_posterior_log_pseudodeterminant: float
    full_posterior_log_pseudodeterminant: float
    subset_posterior_effective_rank: int
    full_posterior_effective_rank: int
    correction_mahalanobis_distance: float
    correction_l2: float
    blockwise_correction_l2: Mapping[str, float]
    covariance_conservative: bool
    covariance_difference_min_eigenvalue: float
    covariance_difference_psd_tolerance: float
    attributed_measured_cost_ns: float
    predicted_cost_ns: float
    full_measured_cost_ns: float
    measured_cost_fraction: float
    predicted_cost_fraction: float
    cost_model_absolute_error_ns: float
    cost_model_relative_error: float
    decision_cost_source: str
    required_partial_stage_cost_charged: bool
    score_computation_cost_measured: bool

    def to_row(self) -> Dict[str, Any]:
        row = asdict(self)
        row["blockwise_correction_l2"] = json.dumps(
            self.blockwise_correction_l2,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        row["selected_feature_ids"] = json.dumps(list(self.selected_feature_ids))
        row["covariance_conservative"] = int(self.covariance_conservative)
        row["required_partial_stage_cost_charged"] = int(
            self.required_partial_stage_cost_charged
        )
        row["score_computation_cost_measured"] = int(
            self.score_computation_cost_measured
        )
        return row


def _attributed_costs(
    envelope: UpdateEnvelope,
    views: Sequence[CandidateView],
    selection: BudgetSelection,
    policy: PolicySpec,
    cost_model: CostModel,
    cost_predictions: Mapping[int, CandidateCostPrediction] | None = None,
) -> Tuple[float, float, float]:
    """Return measured attribution, model prediction, and full captured cost.

    Track-stage terms are the capture's serialization-exclusive durations.  The
    selected/compressed-system, preview, and commit durations are callback-level
    measurements, so a subset cannot have a directly observed version of them;
    they are attributed in proportion to selected reduced rows.  Consequently,
    ``measured`` here is a reproducible stage-cost attribution, not a claim that
    the subset was executed and wall-clock timed.  Ranking-computation overhead
    is likewise not present in Schema 2 and is reported separately by the result
    metadata.
    """

    by_ordinal = {view.ordinal: view for view in views}
    prefilter = float(sum(view.costs.prefilter_ns for view in views))
    all_geometry = float(sum(view.costs.geometry_ns for view in views))
    all_post_prefilter = float(sum(view.costs.post_prefilter_ns for view in views))
    if policy.deployability == Deployability.PRE_FACTOR:
        measured = prefilter + sum(
            by_ordinal[ordinal].costs.post_prefilter_ns
            for ordinal in selection.processed_ordinals
        )
        predicted = prefilter + sum(
            (
                cost_predictions[ordinal].post_prefilter_ns
                if cost_predictions is not None
                else cost_model.predict_post_prefilter_ns(
                    by_ordinal[ordinal].candidate
                )
            )
            for ordinal in selection.processed_ordinals
        )
    elif policy.deployability == Deployability.PARTIAL_COST:
        measured = prefilter + all_geometry + sum(
            by_ordinal[ordinal].costs.post_geometry_ns
            for ordinal in selection.processed_ordinals
        )
        predicted = prefilter + all_geometry + sum(
            (
                cost_predictions[ordinal].post_geometry_ns
                if cost_predictions is not None
                else cost_model.predict_post_geometry_ns(
                    by_ordinal[ordinal].candidate
                )
            )
            for ordinal in selection.processed_ordinals
        )
    else:
        measured = prefilter + all_post_prefilter
        predicted = measured
    full_rows = sum(view.rows for view in views if view.accepted)
    row_fraction = selection.selected_rows / full_rows if full_rows else 0.0
    global_cost = float(
        envelope.selected_system.duration_ns
        + envelope.compressed_system.duration_ns
        + envelope.preview_duration_ns
        + envelope.commit_duration_ns
    )
    attributed_global = global_cost * row_fraction
    measured += attributed_global
    predicted += attributed_global
    full = prefilter + all_post_prefilter + global_cost
    return measured, predicted, full


def _evaluate_policy_update_prepared(
    envelope: UpdateEnvelope,
    reconstruction: UpdateReconstruction,
    views: Sequence[CandidateView],
    ranking: Sequence[int],
    policy: PolicySpec,
    budget: BudgetSpec,
    cost_model: CostModel,
    *,
    dataset_role: DatasetRole,
    sequence_id: str,
    seed: int = DEFAULT_RANDOM_SEED,
    cost_predictions: Mapping[int, CandidateCostPrediction] | None = None,
    evaluation_context: _PolicyUpdateContext | None = None,
    subset_cache: Dict[Tuple[int, ...], _SubsetDiagnostics] | None = None,
) -> PolicyUpdateResult:
    """Evaluate one frozen ordering/budget against one validated callback."""

    if dataset_role == DatasetRole.EUROC_REPLICATION and cost_model.training_dataset != "tum_vi":
        raise PolicyEvaluationError("EuRoC replication cannot fit or use a EuRoC cost model")
    full_global_cost = float(
        envelope.selected_system.duration_ns
        + envelope.compressed_system.duration_ns
        + envelope.preview_duration_ns
        + envelope.commit_duration_ns
    )
    selection = select_budget(
        views,
        ranking,
        policy,
        budget,
        cost_model,
        full_global_cost_ns=full_global_cost,
        cost_predictions=cost_predictions,
    )
    by_ordinal = {view.ordinal: view for view in views}
    selected = tuple(by_ordinal[ordinal] for ordinal in selection.selected_ordinals)
    context = evaluation_context or _prepare_policy_update_context(
        envelope, reconstruction
    )
    subset_key = tuple(selection.selected_ordinals)
    if subset_cache is not None and subset_key in subset_cache:
        subset = subset_cache[subset_key]
    else:
        subset = _prepare_subset_diagnostics(envelope, selected, context)
        if subset_cache is not None:
            subset_cache[subset_key] = subset
    full_full_utility = context.full_utilities["full_state"]

    measured_cost, predicted_cost, full_cost = _attributed_costs(
        envelope,
        views,
        selection,
        policy,
        cost_model,
        cost_predictions,
    )
    cost_error = abs(predicted_cost - measured_cost)
    relative_cost_error = cost_error / max(1.0, measured_cost)
    return PolicyUpdateResult(
        dataset_role=dataset_role.value,
        sequence_id=sequence_id,
        update_id=envelope.update_id,
        camera_timestamp=envelope.camera_timestamp,
        policy=policy.name,
        deployability=policy.deployability.value,
        budget_kind=budget.kind.value,
        budget_fraction=budget.fraction,
        candidate_count=len(views),
        full_accepted_tracks=len(envelope.accepted_feature_ids),
        processed_candidates=len(selection.processed_ordinals),
        selected_tracks=len(selection.selected_ordinals),
        full_rows=envelope.selected_system.h.rows if envelope.selected_system.available else 0,
        selected_rows=selection.selected_rows,
        selected_feature_ids=selection.selected_feature_ids,
        pose_information_retention=_retention(
            subset.utilities["pose"].trace,
            context.full_utilities["pose"].trace,
        ),
        velocity_information_retention=_retention(
            subset.utilities["velocity"].trace,
            context.full_utilities["velocity"].trace,
        ),
        clone_information_retention=_retention(
            subset.utilities["clone"].trace,
            context.full_utilities["clone"].trace,
        ),
        pose_velocity_clone_information_retention=_retention(
            subset.utilities["pose_velocity_clone"].trace,
            context.full_utilities["pose_velocity_clone"].trace,
        ),
        full_state_information_retention=_retention(
            subset.full_utility.trace, full_full_utility.trace
        ),
        full_state_logdet_information_retention=_retention(
            subset.full_utility.logdet_identity_plus,
            full_full_utility.logdet_identity_plus,
        ),
        subset_information_trace=subset.full_utility.trace,
        full_information_trace=full_full_utility.trace,
        subset_information_logdet=subset.full_utility.logdet_identity_plus,
        full_information_logdet=full_full_utility.logdet_identity_plus,
        subset_posterior_trace=subset.spectrum.trace,
        full_posterior_trace=context.full_spectrum.trace,
        subset_posterior_log_pseudodeterminant=(
            subset.spectrum.log_pseudodeterminant
        ),
        full_posterior_log_pseudodeterminant=(
            context.full_spectrum.log_pseudodeterminant
        ),
        subset_posterior_effective_rank=subset.spectrum.effective_rank,
        full_posterior_effective_rank=context.full_spectrum.effective_rank,
        correction_mahalanobis_distance=subset.correction_mahalanobis,
        correction_l2=subset.correction_l2,
        blockwise_correction_l2=subset.blockwise_correction_l2,
        covariance_conservative=subset.covariance_conservative,
        covariance_difference_min_eigenvalue=(
            subset.covariance_difference_min_eigenvalue
        ),
        covariance_difference_psd_tolerance=(
            subset.covariance_difference_psd_tolerance
        ),
        attributed_measured_cost_ns=measured_cost,
        predicted_cost_ns=predicted_cost,
        full_measured_cost_ns=full_cost,
        measured_cost_fraction=measured_cost / full_cost if full_cost > 0.0 else 1.0,
        predicted_cost_fraction=predicted_cost / full_cost if full_cost > 0.0 else 1.0,
        cost_model_absolute_error_ns=cost_error,
        cost_model_relative_error=relative_cost_error,
        decision_cost_source=selection.decision_cost_source,
        required_partial_stage_cost_charged=(
            policy.deployability != Deployability.PRE_FACTOR
        ),
        score_computation_cost_measured=policy.name == "original_order",
    )


def evaluate_policy_update(
    envelope: UpdateEnvelope,
    policy: PolicySpec,
    budget: BudgetSpec,
    cost_model: CostModel,
    *,
    dataset_role: DatasetRole,
    sequence_id: str,
    seed: int = DEFAULT_RANDOM_SEED,
) -> PolicyUpdateResult:
    reconstruction = reconstruct_update(envelope, strict=True)
    views = build_candidate_views(envelope)
    cost_predictions = _predict_candidate_costs(views, cost_model)
    ranking = rank_candidates(
        envelope, views, policy, sequence_id=sequence_id, seed=seed
    )
    return _evaluate_policy_update_prepared(
        envelope,
        reconstruction,
        views,
        ranking,
        policy,
        budget,
        cost_model,
        dataset_role=dataset_role,
        sequence_id=sequence_id,
        seed=seed,
        cost_predictions=cost_predictions,
    )


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        return math.nan
    return float(np.quantile(np.asarray(values, dtype=np.float64), probability))


@dataclass(frozen=True)
class SequencePolicyResult:
    dataset_role: str
    sequence_id: str
    policy: str
    deployability: str
    budget_kind: str
    budget_fraction: float
    update_count: int
    median_information_retention: float
    p10_information_retention: float
    worst_information_retention: float
    worst_information_update_id: int
    median_pose_information_retention: float
    p10_pose_information_retention: float
    median_velocity_information_retention: float
    p10_velocity_information_retention: float
    median_clone_information_retention: float
    p10_clone_information_retention: float
    median_pose_velocity_clone_information_retention: float
    p10_pose_velocity_clone_information_retention: float
    median_logdet_information_retention: float
    p10_logdet_information_retention: float
    worst_logdet_information_retention: float
    median_correction_mahalanobis: float
    p90_correction_mahalanobis: float
    worst_correction_mahalanobis: float
    worst_correction_update_id: int
    median_measured_cost_fraction: float
    p95_measured_cost_fraction: float
    worst_measured_cost_fraction: float
    worst_measured_cost_update_id: int
    median_predicted_cost_fraction: float
    p95_predicted_cost_fraction: float
    median_selected_track_fraction: float
    median_selected_row_fraction: float
    median_subset_posterior_trace: float
    median_full_posterior_trace: float
    median_subset_posterior_log_pseudodeterminant: float
    median_full_posterior_log_pseudodeterminant: float
    median_subset_posterior_effective_rank: float
    median_full_posterior_effective_rank: float
    worst_blockwise_correction_l2: Mapping[str, float]
    conservative_update_fraction: float
    cost_model_absolute_error_ns_p50: float
    cost_model_absolute_error_ns_p95: float
    cost_model_absolute_error_ns_p99: float
    cost_model_error_p50: float
    cost_model_error_p95: float
    cost_model_error_p99: float

    def to_row(self) -> Dict[str, Any]:
        row = asdict(self)
        row["worst_blockwise_correction_l2"] = json.dumps(
            self.worst_blockwise_correction_l2,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return row


def summarize_sequence(
    results: Sequence[PolicyUpdateResult],
) -> SequencePolicyResult:
    if not results:
        raise PolicyEvaluationError("cannot summarize an empty policy result")
    identity = {
        (
            result.dataset_role,
            result.sequence_id,
            result.policy,
            result.deployability,
            result.budget_kind,
            result.budget_fraction,
        )
        for result in results
    }
    if len(identity) != 1:
        raise PolicyEvaluationError("sequence summary mixes policy identities")
    (
        dataset_role,
        sequence_id,
        policy,
        deployability,
        budget_kind,
        budget_fraction,
    ) = next(iter(identity))
    information = [result.full_state_information_retention for result in results]
    pose_information = [result.pose_information_retention for result in results]
    velocity_information = [result.velocity_information_retention for result in results]
    clone_information = [result.clone_information_retention for result in results]
    pvc_information = [
        result.pose_velocity_clone_information_retention for result in results
    ]
    logdet = [result.full_state_logdet_information_retention for result in results]
    correction = [result.correction_mahalanobis_distance for result in results]
    cost = [result.measured_cost_fraction for result in results]
    predicted_cost = [result.predicted_cost_fraction for result in results]
    track_fractions = [
        (
            result.selected_tracks / result.full_accepted_tracks
            if result.full_accepted_tracks
            else 1.0
        )
        for result in results
    ]
    row_fractions = [
        result.selected_rows / result.full_rows if result.full_rows else 1.0
        for result in results
    ]
    worst_index = min(
        range(len(results)),
        key=lambda index: (information[index], results[index].update_id),
    )
    worst_correction_index = max(
        range(len(results)),
        key=lambda index: (correction[index], -results[index].update_id),
    )
    worst_cost_index = max(
        range(len(results)), key=lambda index: (cost[index], -results[index].update_id)
    )
    block_names = sorted(
        {
            name
            for result in results
            for name in result.blockwise_correction_l2.keys()
        }
    )
    worst_blocks = {
        name: max(result.blockwise_correction_l2.get(name, 0.0) for result in results)
        for name in block_names
    }
    return SequencePolicyResult(
        dataset_role=dataset_role,
        sequence_id=sequence_id,
        policy=policy,
        deployability=deployability,
        budget_kind=budget_kind,
        budget_fraction=budget_fraction,
        update_count=len(results),
        median_information_retention=_quantile(information, 0.50),
        p10_information_retention=_quantile(information, 0.10),
        worst_information_retention=min(information),
        worst_information_update_id=results[worst_index].update_id,
        median_pose_information_retention=_quantile(pose_information, 0.50),
        p10_pose_information_retention=_quantile(pose_information, 0.10),
        median_velocity_information_retention=_quantile(velocity_information, 0.50),
        p10_velocity_information_retention=_quantile(velocity_information, 0.10),
        median_clone_information_retention=_quantile(clone_information, 0.50),
        p10_clone_information_retention=_quantile(clone_information, 0.10),
        median_pose_velocity_clone_information_retention=_quantile(
            pvc_information, 0.50
        ),
        p10_pose_velocity_clone_information_retention=_quantile(
            pvc_information, 0.10
        ),
        median_logdet_information_retention=_quantile(logdet, 0.50),
        p10_logdet_information_retention=_quantile(logdet, 0.10),
        worst_logdet_information_retention=min(logdet),
        median_correction_mahalanobis=_quantile(correction, 0.50),
        p90_correction_mahalanobis=_quantile(correction, 0.90),
        worst_correction_mahalanobis=max(correction),
        worst_correction_update_id=results[worst_correction_index].update_id,
        median_measured_cost_fraction=_quantile(cost, 0.50),
        p95_measured_cost_fraction=_quantile(cost, 0.95),
        worst_measured_cost_fraction=max(cost),
        worst_measured_cost_update_id=results[worst_cost_index].update_id,
        median_predicted_cost_fraction=_quantile(predicted_cost, 0.50),
        p95_predicted_cost_fraction=_quantile(predicted_cost, 0.95),
        median_selected_track_fraction=_quantile(track_fractions, 0.50),
        median_selected_row_fraction=_quantile(row_fractions, 0.50),
        median_subset_posterior_trace=_quantile(
            [result.subset_posterior_trace for result in results], 0.50
        ),
        median_full_posterior_trace=_quantile(
            [result.full_posterior_trace for result in results], 0.50
        ),
        median_subset_posterior_log_pseudodeterminant=_quantile(
            [result.subset_posterior_log_pseudodeterminant for result in results],
            0.50,
        ),
        median_full_posterior_log_pseudodeterminant=_quantile(
            [result.full_posterior_log_pseudodeterminant for result in results],
            0.50,
        ),
        median_subset_posterior_effective_rank=_quantile(
            [float(result.subset_posterior_effective_rank) for result in results],
            0.50,
        ),
        median_full_posterior_effective_rank=_quantile(
            [float(result.full_posterior_effective_rank) for result in results],
            0.50,
        ),
        worst_blockwise_correction_l2=worst_blocks,
        conservative_update_fraction=sum(
            result.covariance_conservative for result in results
        )
        / len(results),
        cost_model_absolute_error_ns_p50=_quantile(
            [result.cost_model_absolute_error_ns for result in results], 0.50
        ),
        cost_model_absolute_error_ns_p95=_quantile(
            [result.cost_model_absolute_error_ns for result in results], 0.95
        ),
        cost_model_absolute_error_ns_p99=_quantile(
            [result.cost_model_absolute_error_ns for result in results], 0.99
        ),
        cost_model_error_p50=_quantile(
            [result.cost_model_relative_error for result in results], 0.50
        ),
        cost_model_error_p95=_quantile(
            [result.cost_model_relative_error for result in results], 0.95
        ),
        cost_model_error_p99=_quantile(
            [result.cost_model_relative_error for result in results], 0.99
        ),
    )


_ACCUMULATED_QUANTILE_FIELDS = (
    "full_state_information_retention",
    "pose_information_retention",
    "velocity_information_retention",
    "clone_information_retention",
    "pose_velocity_clone_information_retention",
    "full_state_logdet_information_retention",
    "correction_mahalanobis_distance",
    "measured_cost_fraction",
    "predicted_cost_fraction",
    "selected_track_fraction",
    "selected_row_fraction",
    "subset_posterior_trace",
    "full_posterior_trace",
    "subset_posterior_log_pseudodeterminant",
    "full_posterior_log_pseudodeterminant",
    "subset_posterior_effective_rank",
    "full_posterior_effective_rank",
    "cost_model_absolute_error_ns",
    "cost_model_relative_error",
)


class _ExactSequenceAccumulator:
    """Compact exact equivalent of ``summarize_sequence``.

    Exact quantiles require retaining each scalar, but not each large
    ``PolicyUpdateResult`` (which also owns strings, tuples, and a block map).
    Binary64 ``array`` columns make the logical numeric payload deterministic:
    ``8 * len(_ACCUMULATED_QUANTILE_FIELDS)`` bytes per policy-update point,
    plus ordinary container-capacity overhead and one online worst-value map
    per semantic block.
    """

    __slots__ = (
        "_conservative_count",
        "_count",
        "_identity",
        "_values",
        "_worst_blocks",
        "_worst_correction",
        "_worst_cost",
        "_worst_information",
        "_worst_logdet",
    )

    def __init__(self) -> None:
        self._identity: Tuple[str, str, str, str, str, float] | None = None
        self._values = {
            name: array("d") for name in _ACCUMULATED_QUANTILE_FIELDS
        }
        self._count = 0
        self._conservative_count = 0
        self._worst_information: Tuple[float, int] | None = None
        self._worst_logdet: float | None = None
        self._worst_correction: Tuple[float, int] | None = None
        self._worst_cost: Tuple[float, int] | None = None
        self._worst_blocks: Dict[str, float] = {}

    @property
    def numeric_storage_bytes(self) -> int:
        """Return initialized binary64 payload bytes (excluding capacity overhead)."""

        return sum(values.buffer_info()[1] * values.itemsize for values in self._values.values())

    def add(self, result: PolicyUpdateResult) -> None:
        if any(
            not math.isfinite(value) or value < 0.0
            for value in result.blockwise_correction_l2.values()
        ):
            raise PolicyEvaluationError(
                "blockwise correction norms must be finite and nonnegative"
            )
        identity = (
            result.dataset_role,
            result.sequence_id,
            result.policy,
            result.deployability,
            result.budget_kind,
            result.budget_fraction,
        )
        if self._identity is None:
            self._identity = identity
        elif identity != self._identity:
            raise PolicyEvaluationError("sequence accumulator mixes policy identities")

        track_fraction = (
            result.selected_tracks / result.full_accepted_tracks
            if result.full_accepted_tracks
            else 1.0
        )
        row_fraction = (
            result.selected_rows / result.full_rows if result.full_rows else 1.0
        )
        values = {
            "full_state_information_retention": result.full_state_information_retention,
            "pose_information_retention": result.pose_information_retention,
            "velocity_information_retention": result.velocity_information_retention,
            "clone_information_retention": result.clone_information_retention,
            "pose_velocity_clone_information_retention": (
                result.pose_velocity_clone_information_retention
            ),
            "full_state_logdet_information_retention": (
                result.full_state_logdet_information_retention
            ),
            "correction_mahalanobis_distance": (
                result.correction_mahalanobis_distance
            ),
            "measured_cost_fraction": result.measured_cost_fraction,
            "predicted_cost_fraction": result.predicted_cost_fraction,
            "selected_track_fraction": track_fraction,
            "selected_row_fraction": row_fraction,
            "subset_posterior_trace": result.subset_posterior_trace,
            "full_posterior_trace": result.full_posterior_trace,
            "subset_posterior_log_pseudodeterminant": (
                result.subset_posterior_log_pseudodeterminant
            ),
            "full_posterior_log_pseudodeterminant": (
                result.full_posterior_log_pseudodeterminant
            ),
            "subset_posterior_effective_rank": float(
                result.subset_posterior_effective_rank
            ),
            "full_posterior_effective_rank": float(
                result.full_posterior_effective_rank
            ),
            "cost_model_absolute_error_ns": result.cost_model_absolute_error_ns,
            "cost_model_relative_error": result.cost_model_relative_error,
        }
        for name, value in values.items():
            self._values[name].append(float(value))

        information_key = (
            result.full_state_information_retention,
            result.update_id,
        )
        if self._worst_information is None or information_key < self._worst_information:
            self._worst_information = information_key
        logdet = result.full_state_logdet_information_retention
        if self._worst_logdet is None or logdet < self._worst_logdet:
            self._worst_logdet = logdet
        correction_key = (result.correction_mahalanobis_distance, -result.update_id)
        if self._worst_correction is None or correction_key > self._worst_correction:
            self._worst_correction = correction_key
        cost_key = (result.measured_cost_fraction, -result.update_id)
        if self._worst_cost is None or cost_key > self._worst_cost:
            self._worst_cost = cost_key
        for name, value in result.blockwise_correction_l2.items():
            self._worst_blocks[name] = max(self._worst_blocks.get(name, 0.0), value)
        self._conservative_count += int(result.covariance_conservative)
        self._count += 1

    def summarize(self) -> SequencePolicyResult:
        if self._count == 0 or self._identity is None:
            raise PolicyEvaluationError("cannot summarize an empty policy result")
        assert self._worst_information is not None
        assert self._worst_logdet is not None
        assert self._worst_correction is not None
        assert self._worst_cost is not None
        (
            dataset_role,
            sequence_id,
            policy,
            deployability,
            budget_kind,
            budget_fraction,
        ) = self._identity

        def quantile(name: str, probability: float) -> float:
            return _quantile(self._values[name], probability)

        return SequencePolicyResult(
            dataset_role=dataset_role,
            sequence_id=sequence_id,
            policy=policy,
            deployability=deployability,
            budget_kind=budget_kind,
            budget_fraction=budget_fraction,
            update_count=self._count,
            median_information_retention=quantile(
                "full_state_information_retention", 0.50
            ),
            p10_information_retention=quantile(
                "full_state_information_retention", 0.10
            ),
            worst_information_retention=self._worst_information[0],
            worst_information_update_id=self._worst_information[1],
            median_pose_information_retention=quantile(
                "pose_information_retention", 0.50
            ),
            p10_pose_information_retention=quantile(
                "pose_information_retention", 0.10
            ),
            median_velocity_information_retention=quantile(
                "velocity_information_retention", 0.50
            ),
            p10_velocity_information_retention=quantile(
                "velocity_information_retention", 0.10
            ),
            median_clone_information_retention=quantile(
                "clone_information_retention", 0.50
            ),
            p10_clone_information_retention=quantile(
                "clone_information_retention", 0.10
            ),
            median_pose_velocity_clone_information_retention=quantile(
                "pose_velocity_clone_information_retention", 0.50
            ),
            p10_pose_velocity_clone_information_retention=quantile(
                "pose_velocity_clone_information_retention", 0.10
            ),
            median_logdet_information_retention=quantile(
                "full_state_logdet_information_retention", 0.50
            ),
            p10_logdet_information_retention=quantile(
                "full_state_logdet_information_retention", 0.10
            ),
            worst_logdet_information_retention=self._worst_logdet,
            median_correction_mahalanobis=quantile(
                "correction_mahalanobis_distance", 0.50
            ),
            p90_correction_mahalanobis=quantile(
                "correction_mahalanobis_distance", 0.90
            ),
            worst_correction_mahalanobis=self._worst_correction[0],
            worst_correction_update_id=-self._worst_correction[1],
            median_measured_cost_fraction=quantile("measured_cost_fraction", 0.50),
            p95_measured_cost_fraction=quantile("measured_cost_fraction", 0.95),
            worst_measured_cost_fraction=self._worst_cost[0],
            worst_measured_cost_update_id=-self._worst_cost[1],
            median_predicted_cost_fraction=quantile(
                "predicted_cost_fraction", 0.50
            ),
            p95_predicted_cost_fraction=quantile(
                "predicted_cost_fraction", 0.95
            ),
            median_selected_track_fraction=quantile(
                "selected_track_fraction", 0.50
            ),
            median_selected_row_fraction=quantile(
                "selected_row_fraction", 0.50
            ),
            median_subset_posterior_trace=quantile("subset_posterior_trace", 0.50),
            median_full_posterior_trace=quantile("full_posterior_trace", 0.50),
            median_subset_posterior_log_pseudodeterminant=quantile(
                "subset_posterior_log_pseudodeterminant", 0.50
            ),
            median_full_posterior_log_pseudodeterminant=quantile(
                "full_posterior_log_pseudodeterminant", 0.50
            ),
            median_subset_posterior_effective_rank=quantile(
                "subset_posterior_effective_rank", 0.50
            ),
            median_full_posterior_effective_rank=quantile(
                "full_posterior_effective_rank", 0.50
            ),
            worst_blockwise_correction_l2=dict(sorted(self._worst_blocks.items())),
            conservative_update_fraction=self._conservative_count / self._count,
            cost_model_absolute_error_ns_p50=quantile(
                "cost_model_absolute_error_ns", 0.50
            ),
            cost_model_absolute_error_ns_p95=quantile(
                "cost_model_absolute_error_ns", 0.95
            ),
            cost_model_absolute_error_ns_p99=quantile(
                "cost_model_absolute_error_ns", 0.99
            ),
            cost_model_error_p50=quantile("cost_model_relative_error", 0.50),
            cost_model_error_p95=quantile("cost_model_relative_error", 0.95),
            cost_model_error_p99=quantile("cost_model_relative_error", 0.99),
        )


@dataclass(frozen=True)
class PolicyAggregateResult:
    dataset_role: str
    policy: str
    deployability: str
    budget_kind: str
    budget_fraction: float
    sequence_count: int
    update_count: int
    median_sequence_information_retention: float
    worst_sequence_p10_information_retention: float
    worst_update_information_retention: float
    median_sequence_pose_velocity_clone_retention: float
    worst_sequence_p10_pose_velocity_clone_retention: float
    median_sequence_logdet_retention: float
    worst_sequence_p10_logdet_retention: float
    worst_update_logdet_retention: float
    median_sequence_correction_mahalanobis: float
    worst_sequence_correction_mahalanobis: float
    median_sequence_cost_fraction: float
    worst_sequence_p95_cost_fraction: float
    median_sequence_predicted_cost_fraction: float
    worst_sequence_p95_predicted_cost_fraction: float
    median_sequence_subset_posterior_trace: float
    median_sequence_full_posterior_trace: float
    worst_blockwise_correction_l2: Mapping[str, float]
    minimum_conservative_update_fraction: float
    worst_cost_model_absolute_error_ns_p95: float
    worst_cost_model_absolute_error_ns_p99: float
    worst_cost_model_error_p95: float
    worst_cost_model_error_p99: float

    def to_row(self) -> Dict[str, Any]:
        row = asdict(self)
        row["worst_blockwise_correction_l2"] = json.dumps(
            self.worst_blockwise_correction_l2,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return row


def aggregate_policy_results(
    sequences: Sequence[SequencePolicyResult],
) -> PolicyAggregateResult:
    if not sequences:
        raise PolicyEvaluationError("cannot aggregate an empty sequence set")
    identity = {
        (
            result.dataset_role,
            result.policy,
            result.deployability,
            result.budget_kind,
            result.budget_fraction,
        )
        for result in sequences
    }
    if len(identity) != 1:
        raise PolicyEvaluationError("policy aggregate mixes identities")
    dataset_role, policy, deployability, budget_kind, fraction = next(iter(identity))
    block_names = sorted(
        {
            name
            for result in sequences
            for name in result.worst_blockwise_correction_l2.keys()
        }
    )
    worst_blocks = {
        name: max(
            result.worst_blockwise_correction_l2.get(name, 0.0)
            for result in sequences
        )
        for name in block_names
    }
    return PolicyAggregateResult(
        dataset_role=dataset_role,
        policy=policy,
        deployability=deployability,
        budget_kind=budget_kind,
        budget_fraction=fraction,
        sequence_count=len(sequences),
        update_count=sum(result.update_count for result in sequences),
        median_sequence_information_retention=_quantile(
            [result.median_information_retention for result in sequences], 0.50
        ),
        worst_sequence_p10_information_retention=min(
            result.p10_information_retention for result in sequences
        ),
        worst_update_information_retention=min(
            result.worst_information_retention for result in sequences
        ),
        median_sequence_pose_velocity_clone_retention=_quantile(
            [
                result.median_pose_velocity_clone_information_retention
                for result in sequences
            ],
            0.50,
        ),
        worst_sequence_p10_pose_velocity_clone_retention=min(
            result.p10_pose_velocity_clone_information_retention
            for result in sequences
        ),
        median_sequence_logdet_retention=_quantile(
            [result.median_logdet_information_retention for result in sequences], 0.50
        ),
        worst_sequence_p10_logdet_retention=min(
            result.p10_logdet_information_retention for result in sequences
        ),
        worst_update_logdet_retention=min(
            result.worst_logdet_information_retention for result in sequences
        ),
        median_sequence_correction_mahalanobis=_quantile(
            [result.median_correction_mahalanobis for result in sequences], 0.50
        ),
        worst_sequence_correction_mahalanobis=max(
            result.worst_correction_mahalanobis for result in sequences
        ),
        median_sequence_cost_fraction=_quantile(
            [result.median_measured_cost_fraction for result in sequences], 0.50
        ),
        worst_sequence_p95_cost_fraction=max(
            result.p95_measured_cost_fraction for result in sequences
        ),
        median_sequence_predicted_cost_fraction=_quantile(
            [result.median_predicted_cost_fraction for result in sequences], 0.50
        ),
        worst_sequence_p95_predicted_cost_fraction=max(
            result.p95_predicted_cost_fraction for result in sequences
        ),
        median_sequence_subset_posterior_trace=_quantile(
            [result.median_subset_posterior_trace for result in sequences], 0.50
        ),
        median_sequence_full_posterior_trace=_quantile(
            [result.median_full_posterior_trace for result in sequences], 0.50
        ),
        worst_blockwise_correction_l2=worst_blocks,
        minimum_conservative_update_fraction=min(
            result.conservative_update_fraction for result in sequences
        ),
        worst_cost_model_absolute_error_ns_p95=max(
            result.cost_model_absolute_error_ns_p95 for result in sequences
        ),
        worst_cost_model_absolute_error_ns_p99=max(
            result.cost_model_absolute_error_ns_p99 for result in sequences
        ),
        worst_cost_model_error_p95=max(
            result.cost_model_error_p95 for result in sequences
        ),
        worst_cost_model_error_p99=max(
            result.cost_model_error_p99 for result in sequences
        ),
    )


@dataclass(frozen=True)
class FrozenPolicy:
    schema_version: int
    policy: PolicySpec
    budget: BudgetSpec
    seed: int
    cost_model: CostModel
    selection_dataset: str
    selection_sequences: Tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise PolicyEvaluationError("unsupported frozen-policy schema")
        if self.selection_dataset != "tum_vi":
            raise PolicyEvaluationError("policy selection must use TUM-VI only")
        if not self.selection_sequences:
            raise PolicyEvaluationError("frozen policy has no TUM-VI selection sequences")
        if self.policy.name not in POLICY_BY_NAME:
            raise PolicyEvaluationError("frozen policy is unknown")

    def to_json(self) -> Dict[str, Any]:
        return {
            "budget": self.budget.to_json(),
            "cost_model": self.cost_model.to_json(),
            "policy": self.policy.to_json(),
            "schema_version": self.schema_version,
            "seed": self.seed,
            "selection_dataset": self.selection_dataset,
            "selection_sequences": list(self.selection_sequences),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> "FrozenPolicy":
        policy_value = value["policy"]
        name = str(policy_value["name"])
        if name not in POLICY_BY_NAME:
            raise PolicyEvaluationError(f"unknown frozen policy {name!r}")
        canonical = POLICY_BY_NAME[name]
        if canonical.to_json() != dict(policy_value):
            raise PolicyEvaluationError("frozen policy metadata differs from registry")
        budget_value = value["budget"]
        return cls(
            schema_version=int(value["schema_version"]),
            policy=canonical,
            budget=BudgetSpec(
                BudgetKind(str(budget_value["kind"])),
                float(budget_value["fraction"]),
            ),
            seed=int(value["seed"]),
            cost_model=CostModel.from_json(value["cost_model"]),
            selection_dataset=str(value["selection_dataset"]),
            selection_sequences=tuple(
                str(item) for item in value["selection_sequences"]
            ),
        )


@dataclass(frozen=True)
class CaptureSpec:
    sequence_id: str
    path: str

    def to_json(self) -> Dict[str, str]:
        return {"path": str(Path(self.path).resolve()), "sequence_id": self.sequence_id}


def evaluate_capture(
    capture_spec: CaptureSpec,
    *,
    dataset_role: DatasetRole,
    cost_model: CostModel,
    policies: Sequence[PolicySpec] = POLICIES,
    budgets: Sequence[BudgetSpec] = DEFAULT_BUDGETS,
    seed: int = DEFAULT_RANDOM_SEED,
) -> Tuple[SequencePolicyResult, ...]:
    """Stream one capture and return exact per-policy sequence summaries."""

    if not policies:
        raise PolicyEvaluationError("capture evaluation requires at least one policy")
    if not budgets:
        raise PolicyEvaluationError("capture evaluation requires at least one budget")
    policy_names = [policy.name for policy in policies]
    if len(set(policy_names)) != len(policy_names):
        raise PolicyEvaluationError("capture evaluation policy names must be unique")
    budget_keys = [budget.key for budget in budgets]
    if len(set(budget_keys)) != len(budget_keys):
        raise PolicyEvaluationError("capture evaluation budgets must be unique")

    grouped: Dict[Tuple[str, str, float], _ExactSequenceAccumulator] = {
        (policy.name, budget.kind.value, budget.fraction): _ExactSequenceAccumulator()
        for policy in policies
        for budget in budgets
    }
    update_count = 0
    with open_capture(capture_spec.path) as capture:
        if capture.header.sequence_id != capture_spec.sequence_id:
            raise PolicyEvaluationError(
                f"capture sequence {capture.header.sequence_id!r} does not match "
                f"declared {capture_spec.sequence_id!r}"
            )
        for envelope in capture:
            update_count += 1
            reconstruction = reconstruct_update(envelope, strict=True)
            views = build_candidate_views(envelope)
            cost_predictions = _predict_candidate_costs(views, cost_model)
            evaluation_context = _prepare_policy_update_context(
                envelope, reconstruction
            )
            subset_cache: Dict[Tuple[int, ...], _SubsetDiagnostics] = {}
            for policy in policies:
                ranking = rank_candidates(
                    envelope,
                    views,
                    policy,
                    sequence_id=capture_spec.sequence_id,
                    seed=seed,
                )
                for budget in budgets:
                    result = _evaluate_policy_update_prepared(
                        envelope,
                        reconstruction,
                        views,
                        ranking,
                        policy,
                        budget,
                        cost_model,
                        dataset_role=dataset_role,
                        sequence_id=capture_spec.sequence_id,
                        seed=seed,
                        cost_predictions=cost_predictions,
                        evaluation_context=evaluation_context,
                        subset_cache=subset_cache,
                    )
                    grouped[
                        (policy.name, budget.kind.value, budget.fraction)
                    ].add(result)
    if update_count == 0:
        raise PolicyEvaluationError(
            f"capture {capture_spec.path!r} contains no update envelopes"
        )
    return tuple(
        grouped[key].summarize()
        for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2]))
    )


def _aggregate_sequence_grid(
    sequences: Sequence[SequencePolicyResult],
) -> Tuple[PolicyAggregateResult, ...]:
    grouped: Dict[Tuple[str, str, str, float], List[SequencePolicyResult]] = {}
    for result in sequences:
        key = (
            result.dataset_role,
            result.policy,
            result.budget_kind,
            result.budget_fraction,
        )
        grouped.setdefault(key, []).append(result)
    return tuple(
        aggregate_policy_results(grouped[key])
        for key in sorted(grouped, key=lambda item: (item[1], item[2], item[3]))
    )


def evaluate_tum_vi_loso(
    captures: Sequence[CaptureSpec],
    *,
    policies: Sequence[PolicySpec] = POLICIES,
    budgets: Sequence[BudgetSpec] = DEFAULT_BUDGETS,
    seed: int = DEFAULT_RANDOM_SEED,
) -> Tuple[Tuple[SequencePolicyResult, ...], Tuple[PolicyAggregateResult, ...]]:
    """Fit cost only on training sequences and evaluate each held-out TUM-VI sequence."""

    if len(captures) < 2:
        raise PolicyEvaluationError("TUM-VI LOSO requires at least two sequences")
    sequence_ids = [capture.sequence_id for capture in captures]
    if len(set(sequence_ids)) != len(sequence_ids):
        raise PolicyEvaluationError("TUM-VI LOSO sequence IDs must be unique")
    summaries: List[SequencePolicyResult] = []
    for held_out in sorted(captures, key=lambda item: item.sequence_id):
        training = [
            (capture.sequence_id, capture.path)
            for capture in captures
            if capture.sequence_id != held_out.sequence_id
        ]
        model = fit_cost_model_from_captures(training)
        if held_out.sequence_id in model.training_sequences:
            raise PolicyEvaluationError("held-out sequence leaked into the cost model")
        summaries.extend(
            evaluate_capture(
                held_out,
                dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                cost_model=model,
                policies=policies,
                budgets=budgets,
                seed=seed,
            )
        )
    return tuple(summaries), _aggregate_sequence_grid(summaries)


def freeze_tum_vi_policy(
    captures: Sequence[CaptureSpec],
    policy: PolicySpec,
    budget: BudgetSpec,
    *,
    seed: int = DEFAULT_RANDOM_SEED,
) -> FrozenPolicy:
    """Freeze an explicitly chosen policy after TUM-VI-only selection."""

    if not captures:
        raise PolicyEvaluationError("policy freeze requires TUM-VI captures")
    sequences = tuple(sorted(capture.sequence_id for capture in captures))
    if len(set(sequences)) != len(sequences):
        raise PolicyEvaluationError("policy-freeze sequence IDs must be unique")
    model = fit_cost_model_from_captures(
        [(capture.sequence_id, capture.path) for capture in captures]
    )
    return FrozenPolicy(
        schema_version=1,
        policy=policy,
        budget=budget,
        seed=seed,
        cost_model=model,
        selection_dataset="tum_vi",
        selection_sequences=sequences,
    )


def evaluate_euroc_replication(
    captures: Sequence[CaptureSpec], frozen: FrozenPolicy
) -> Tuple[Tuple[SequencePolicyResult, ...], Tuple[PolicyAggregateResult, ...]]:
    """Evaluate one immutable TUM-VI-selected policy without EuRoC fitting."""

    if not captures:
        raise PolicyEvaluationError("EuRoC replication requires captures")
    if any("MH_04" in capture.sequence_id.upper() for capture in captures):
        raise PolicyEvaluationError("EuRoC MH_04 is excluded from replication")
    summaries: List[SequencePolicyResult] = []
    for capture in sorted(captures, key=lambda item: item.sequence_id):
        summaries.extend(
            evaluate_capture(
                capture,
                dataset_role=DatasetRole.EUROC_REPLICATION,
                cost_model=frozen.cost_model,
                policies=(frozen.policy,),
                budgets=(frozen.budget,),
                seed=frozen.seed,
            )
        )
    return tuple(summaries), _aggregate_sequence_grid(summaries)


def _write_csv_create_new(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        raise PolicyEvaluationError(f"cannot write empty CSV {path}")
    fieldnames = tuple(rows[0].keys())
    if any(tuple(row.keys()) != fieldnames for row in rows):
        raise PolicyEvaluationError(f"CSV rows for {path} have inconsistent columns")
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _write_json_create_new(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(
            _json_safe(dict(value)),
            stream,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")


def write_evaluation_outputs(
    output_dir: os.PathLike[str] | str,
    *,
    mode: str,
    captures: Sequence[CaptureSpec],
    sequences: Sequence[SequencePolicyResult],
    aggregates: Sequence[PolicyAggregateResult],
    frozen_policy: FrozenPolicy | None = None,
) -> None:
    root = Path(output_dir)
    root.mkdir(parents=False, exist_ok=False)
    sequence_rows = [result.to_row() for result in sequences]
    aggregate_rows = [result.to_row() for result in aggregates]
    _write_csv_create_new(root / "PER_SEQUENCE_RESULTS.csv", sequence_rows)
    _write_csv_create_new(root / "POLICY_RESULTS.csv", aggregate_rows)
    curve_columns = (
        "dataset_role",
        "policy",
        "deployability",
        "budget_kind",
        "budget_fraction",
        "median_sequence_information_retention",
        "worst_sequence_p10_information_retention",
        "worst_update_information_retention",
        "median_sequence_pose_velocity_clone_retention",
        "worst_sequence_p10_pose_velocity_clone_retention",
        "median_sequence_cost_fraction",
        "worst_sequence_p95_cost_fraction",
        "median_sequence_predicted_cost_fraction",
        "worst_sequence_p95_predicted_cost_fraction",
        "median_sequence_correction_mahalanobis",
        "worst_sequence_correction_mahalanobis",
        "minimum_conservative_update_fraction",
    )
    curve_rows = [
        {column: row[column] for column in curve_columns} for row in aggregate_rows
    ]
    _write_csv_create_new(root / "BUDGET_CURVES.csv", curve_rows)
    _write_json_create_new(
        root / "STUDY_SUMMARY.json",
        {
            "aggregates": aggregate_rows,
            "captures": [capture.to_json() for capture in captures],
            "frozen_policy": frozen_policy.to_json() if frozen_policy else None,
            "mode": mode,
            "sequence_results": sequence_rows,
        },
    )


def _capture_argument(value: str) -> CaptureSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("capture must be SEQUENCE_ID=PATH")
    sequence_id, path = value.split("=", 1)
    if not sequence_id or not path:
        raise argparse.ArgumentTypeError("capture must be SEQUENCE_ID=PATH")
    return CaptureSpec(sequence_id, path)


def _budget_argument(value: str) -> BudgetSpec:
    try:
        kind, fraction = value.split(":", 1)
        budget = BudgetSpec(BudgetKind(kind), float(fraction))
        allowed = {
            BudgetKind.TRACK: TRACK_FRACTIONS,
            BudgetKind.ROW: ROW_FRACTIONS,
            BudgetKind.COST: COST_FRACTIONS,
        }[budget.kind]
        if budget.fraction not in allowed:
            raise PolicyEvaluationError(
                f"{budget.kind.value} fraction must be one of {allowed!r}"
            )
        return budget
    except (ValueError, PolicyEvaluationError) as exc:
        raise argparse.ArgumentTypeError(
            "budget must be track|row|cost:FRACTION"
        ) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate Schema-2 anytime information-budget policies"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    loso = subparsers.add_parser(
        "loso", help="TUM-VI leave-one-sequence-out evaluation"
    )
    loso.add_argument("--tum-vi", action="append", required=True, type=_capture_argument)
    loso.add_argument("--output-dir", required=True)
    loso.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    loso.add_argument("--policy", action="append", choices=sorted(POLICY_BY_NAME))
    loso.add_argument("--budget", action="append", type=_budget_argument)

    freeze = subparsers.add_parser(
        "freeze", help="fit TUM-VI cost model and freeze an explicitly chosen policy"
    )
    freeze.add_argument("--tum-vi", action="append", required=True, type=_capture_argument)
    freeze.add_argument("--policy", required=True, choices=sorted(POLICY_BY_NAME))
    freeze.add_argument("--budget", required=True, type=_budget_argument)
    freeze.add_argument("--seed", type=int, default=DEFAULT_RANDOM_SEED)
    freeze.add_argument("--output", required=True)

    replicate = subparsers.add_parser(
        "replicate", help="evaluate a TUM-VI-frozen policy on EuRoC"
    )
    replicate.add_argument("--euroc", action="append", required=True, type=_capture_argument)
    replicate.add_argument("--frozen-policy", required=True)
    replicate.add_argument("--output-dir", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "loso":
            policies = (
                tuple(POLICY_BY_NAME[name] for name in args.policy)
                if args.policy
                else POLICIES
            )
            budgets = tuple(args.budget) if args.budget else DEFAULT_BUDGETS
            sequences, aggregates = evaluate_tum_vi_loso(
                args.tum_vi, policies=policies, budgets=budgets, seed=args.seed
            )
            write_evaluation_outputs(
                args.output_dir,
                mode="tum_vi_loso",
                captures=args.tum_vi,
                sequences=sequences,
                aggregates=aggregates,
            )
            payload = {
                "aggregate_count": len(aggregates),
                "mode": "tum_vi_loso",
                "output_dir": str(Path(args.output_dir).resolve()),
                "sequence_result_count": len(sequences),
                "status": "valid",
            }
        elif args.command == "freeze":
            frozen = freeze_tum_vi_policy(
                args.tum_vi,
                POLICY_BY_NAME[args.policy],
                args.budget,
                seed=args.seed,
            )
            _write_json_create_new(Path(args.output), frozen.to_json())
            payload = {
                "mode": "freeze",
                "output": str(Path(args.output).resolve()),
                "status": "valid",
            }
        else:
            with Path(args.frozen_policy).open("r", encoding="utf-8") as stream:
                frozen = FrozenPolicy.from_json(json.load(stream))
            sequences, aggregates = evaluate_euroc_replication(args.euroc, frozen)
            write_evaluation_outputs(
                args.output_dir,
                mode="euroc_replication",
                captures=args.euroc,
                sequences=sequences,
                aggregates=aggregates,
                frozen_policy=frozen,
            )
            payload = {
                "aggregate_count": len(aggregates),
                "mode": "euroc_replication",
                "output_dir": str(Path(args.output_dir).resolve()),
                "sequence_result_count": len(sequences),
                "status": "valid",
            }
        print(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, ReconstructionError, PolicyEvaluationError, ValueError) as exc:
        print(
            json.dumps(
                {"error": str(exc), "status": "invalid"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
