#!/usr/bin/env python3
"""Focused deterministic tests for Schema-2 information-budget policies."""

from __future__ import annotations

import dataclasses
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from experiments.anytime_information.capture_reader import (  # noqa: E402
    CandidateRecord,
    GlobalSystem,
    LayoutBlock,
    Observation,
)
from scripts.analysis.anytime_information_study import (  # noqa: E402
    _posterior_from_system,
    reconstruct_update,
)
from scripts.analysis.anytime_policy_evaluation import (  # noqa: E402
    COST_FEATURE_NAMES,
    POLICIES,
    POLICY_BY_NAME,
    BudgetKind,
    BudgetSpec,
    CaptureSpec,
    CostModel,
    DatasetRole,
    Deployability,
    FrozenPolicy,
    PolicyEvaluationError,
    SequencePolicyResult,
    _ACCUMULATED_QUANTILE_FIELDS,
    _ExactSequenceAccumulator,
    _evaluate_policy_update_prepared,
    _predict_candidate_costs,
    _prepare_policy_update_context,
    _policy_posterior_agreement_tolerance,
    _subset_posterior,
    _bearing_parallax,
    _pixel_motion,
    aggregate_policy_results,
    build_candidate_views,
    evaluate_capture,
    evaluate_policy_update,
    evaluate_euroc_replication,
    evaluate_tum_vi_loso,
    fit_cost_model_from_envelopes,
    rank_candidates,
    select_budget,
    summarize_sequence,
    write_evaluation_outputs,
)
from scripts.analysis.test_anytime_information_study import (  # noqa: E402
    _accepted_envelope,
    _matrix,
    _production_posterior,
    _system,
    _zero_update_envelope,
)


def _observations(
    count: int,
    *,
    camera_id: int = 0,
    raw_u: float,
    raw_v: float,
    normalized_step: float,
) -> tuple[Observation, ...]:
    return tuple(
        Observation(
            camera_id,
            1.0 + 0.1 * index,
            raw_u + index,
            raw_v + 2.0 * index,
            True,
            0.01 * raw_u + normalized_step * index,
            0.01 * raw_v,
        )
        for index in range(count)
    )


def _multi_envelope():
    base = _accepted_envelope()
    reduced_rows = (
        np.asarray(((1.0, 0.0),)),
        np.asarray(((0.0, 1.0),)),
        np.asarray(((1.0, 1.0),)),
        np.asarray(((-0.5, 1.0),)),
    )
    residuals = (0.20, -0.10, 0.15, 0.05)
    pixels = ((20.0, 20.0), (30.0, 30.0), (500.0, 30.0), (510.0, 40.0))
    candidates = []
    tracks = []
    for ordinal, (h, residual, pixel) in enumerate(
        zip(reduced_rows, residuals, pixels)
    ):
        count = ordinal + 2
        observations = _observations(
            count,
            raw_u=pixel[0],
            raw_v=pixel[1],
            normalized_step=0.002 * (ordinal + 1),
        )
        candidate = dataclasses.replace(
            base.candidates[0],
            ordinal=ordinal,
            feature_id=10 + ordinal,
            raw_observation_count=count,
            cleaned_observation_count=count,
            first_timestamp=observations[0].timestamp,
            last_timestamp=observations[-1].timestamp,
            track_age=observations[-1].timestamp - observations[0].timestamp,
            observations=observations,
            maximum_normalized_parallax=0.0,
            maximum_pixel_displacement=0.0,
            last_frame_pixel_displacement=math.sqrt(5.0),
            final_normalized_u=observations[-1].normalized_u,
            final_normalized_v=observations[-1].normalized_v,
            prefilter_duration_ns=10 + ordinal,
        )
        raw_h_x = np.vstack((np.zeros((3, 2)), h))
        raw_h_f = np.vstack((np.eye(3), np.zeros((1, 3))))
        track = dataclasses.replace(
            base.tracks[0],
            candidate_ordinal=ordinal,
            feature_id=10 + ordinal,
            h_x=_matrix(raw_h_x),
            h_f=_matrix(raw_h_f),
            residual=_matrix([0.0, 0.0, 0.0, residual]),
            sigma_px=1.0,
            noise_variance=1.0,
            reduced_a=_matrix(h),
            reduced_b=_matrix([residual]),
            global_row_start=ordinal,
            global_row_count=1,
            geometry_duration_ns=20 + 2 * ordinal,
            raw_factor_duration_ns=30 + 3 * ordinal,
            reduction_duration_ns=40 + 4 * ordinal,
            gate_duration_ns=50 + 5 * ordinal,
            accumulation_duration_ns=60 + 6 * ordinal,
            parallax_3d_available=True,
            parallax_3d=0.01 * (4 - ordinal),
        )
        candidates.append(candidate)
        tracks.append(track)
    h = np.vstack(reduced_rows)
    residual = np.asarray(residuals)
    covariance = np.eye(4)
    prior = np.asarray(base.p_minus.values).reshape(2, 2)
    dx, p_plus = _production_posterior(prior, h, residual, covariance)
    envelope = dataclasses.replace(
        base,
        candidates=tuple(candidates),
        tracks=tuple(tracks),
        accepted_feature_ids=tuple(range(10, 14)),
        selected_system=_system(h, residual, covariance, duration_ns=31),
        compressed_system=_system(h, residual, covariance, duration_ns=37),
        production_dx=_matrix(dx),
        p_plus=_matrix(p_plus),
    )
    reconstruct_update(envelope)
    return envelope


def _fit_model(envelope) -> CostModel:
    return fit_cost_model_from_envelopes((('room4', envelope), ('corridor4', envelope)))


def _sequence_summary(sequence_id: str) -> SequencePolicyResult:
    return SequencePolicyResult(
        dataset_role=DatasetRole.TUM_VI_HELD_OUT.value,
        sequence_id=sequence_id,
        policy="original_order",
        deployability=Deployability.PRE_FACTOR.value,
        budget_kind=BudgetKind.TRACK.value,
        budget_fraction=0.5,
        update_count=2,
        median_information_retention=0.8,
        p10_information_retention=0.7,
        worst_information_retention=0.6,
        worst_information_update_id=1,
        median_pose_information_retention=0.8,
        p10_pose_information_retention=0.7,
        median_velocity_information_retention=0.8,
        p10_velocity_information_retention=0.7,
        median_clone_information_retention=0.8,
        p10_clone_information_retention=0.7,
        median_pose_velocity_clone_information_retention=0.8,
        p10_pose_velocity_clone_information_retention=0.7,
        median_logdet_information_retention=0.8,
        p10_logdet_information_retention=0.7,
        worst_logdet_information_retention=0.6,
        median_correction_mahalanobis=0.2,
        p90_correction_mahalanobis=0.3,
        worst_correction_mahalanobis=0.4,
        worst_correction_update_id=1,
        median_measured_cost_fraction=0.5,
        p95_measured_cost_fraction=0.6,
        worst_measured_cost_fraction=0.7,
        worst_measured_cost_update_id=1,
        median_predicted_cost_fraction=0.55,
        p95_predicted_cost_fraction=0.65,
        median_selected_track_fraction=0.5,
        median_selected_row_fraction=0.5,
        median_subset_posterior_trace=3.0,
        median_full_posterior_trace=2.0,
        median_subset_posterior_log_pseudodeterminant=0.5,
        median_full_posterior_log_pseudodeterminant=0.2,
        median_subset_posterior_effective_rank=2.0,
        median_full_posterior_effective_rank=2.0,
        worst_blockwise_correction_l2={"imu": 0.1},
        conservative_update_fraction=1.0,
        cost_model_absolute_error_ns_p50=10.0,
        cost_model_absolute_error_ns_p95=20.0,
        cost_model_absolute_error_ns_p99=30.0,
        cost_model_error_p50=0.1,
        cost_model_error_p95=0.2,
        cost_model_error_p99=0.3,
    )


class AnytimePolicyEvaluationTest(unittest.TestCase):
    def test_fast_subset_posterior_matches_strict_high_dynamic_range(self) -> None:
        envelope = _multi_envelope()
        views = build_candidate_views(envelope)
        scales = np.asarray((1.0e4, 1.0e2, 1.0, 1.0e-2))
        correlation = np.asarray(
            (
                (1.0, 0.2, -0.1, 0.05),
                (0.2, 1.0, 0.15, -0.1),
                (-0.1, 0.15, 1.0, 0.25),
                (0.05, -0.1, 0.25, 1.0),
            )
        )
        prior = (scales[:, None] * correlation) * scales[None, :]
        coefficients = np.asarray(
            (
                (1.0, 0.5, -0.25, 0.125),
                (-0.4, 1.0, 0.75, -0.2),
                (0.3, -0.6, 1.0, 0.9),
            )
        )
        h = coefficients / scales[None, :]
        residual = np.asarray((1.0e4, -1.0e-4, 3.0))
        variances = np.asarray((1.0e-6, 1.0e-3, 1.0e-1))
        selected = tuple(
            dataclasses.replace(
                views[index],
                full_h=h[index : index + 1],
                residual=residual[index : index + 1],
                rows=1,
                track=dataclasses.replace(
                    views[index].track,
                    noise_variance=float(variances[index]),
                    global_row_count=1,
                ),
            )
            for index in range(3)
        )

        fast_dx, fast_p_plus = _subset_posterior(prior, selected)
        state_blocks = (
            dataclasses.replace(envelope.state_blocks[0], dimension=prior.shape[0]),
        )
        system = GlobalSystem(
            available=True,
            h=_matrix(h),
            residual=_matrix(residual.reshape((-1, 1))),
            covariance=_matrix(np.diag(variances)),
            layout=(LayoutBlock(0, 0, prior.shape[0]),),
            duration_ns=0,
        )
        strict = _posterior_from_system(prior, state_blocks, system)
        innovation_condition = float(
            np.linalg.cond(h @ prior @ h.T + np.diag(variances))
        )
        self.assertGreaterEqual(
            float(np.max(np.diag(prior)) / np.min(np.diag(prior))), 1.0e12
        )
        for label, actual, expected in (
            ("dx", fast_dx, strict.dx),
            ("Joseph P_plus", fast_p_plus, strict.independent_p_plus),
        ):
            tolerance = _policy_posterior_agreement_tolerance(
                expected, max(prior.shape[0], h.shape[0]), innovation_condition
            )
            error = float(np.max(np.abs(actual - expected)))
            scale = max(1.0, float(np.max(np.abs(expected))))
            self.assertLessEqual(error, tolerance, f"{label}: {error} > {tolerance}")
            self.assertLess(
                tolerance / scale,
                1.0e-10,
                f"{label}: policy tolerance preserves fewer than ten digits",
            )

    def test_per_update_caches_are_exactly_equivalent_to_uncached_path(self) -> None:
        envelope = _multi_envelope()
        reconstruction = reconstruct_update(envelope)
        views = build_candidate_views(envelope)
        model = _fit_model(envelope)
        predictions = _predict_candidate_costs(views, model)
        context = _prepare_policy_update_context(envelope, reconstruction, views)
        subset_cache = {}
        policy = POLICY_BY_NAME["original_order"]
        ranking = rank_candidates(envelope, views, policy, sequence_id="room4")
        for budget in (
            BudgetSpec(BudgetKind.TRACK, 1.0),
            BudgetSpec(BudgetKind.ROW, 1.0),
            BudgetSpec(BudgetKind.COST, 0.5),
        ):
            keyword = {
                "dataset_role": DatasetRole.TUM_VI_HELD_OUT,
                "sequence_id": "room4",
            }
            expected = _evaluate_policy_update_prepared(
                envelope,
                reconstruction,
                views,
                ranking,
                policy,
                budget,
                model,
                **keyword,
            )
            actual = _evaluate_policy_update_prepared(
                envelope,
                reconstruction,
                views,
                ranking,
                policy,
                budget,
                model,
                cost_predictions=predictions,
                evaluation_context=context,
                subset_cache=subset_cache,
                **keyword,
            )
            self.assertEqual(actual, expected)
        self.assertEqual(len(subset_cache), 2)

    def test_compact_accumulator_exactly_matches_batch_summary(self) -> None:
        envelope = _multi_envelope()
        model = _fit_model(envelope)
        budget = BudgetSpec(BudgetKind.TRACK, 0.5)
        first = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["original_order"],
            budget,
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        update_ids = (8, 6, 4, 2, 0, 9, 1)
        results = []
        for index, update_id in enumerate(update_ids):
            extrema_tie = update_id in (9, 1)
            results.append(
                dataclasses.replace(
                    first,
                    update_id=update_id,
                    full_accepted_tracks=0 if index == 0 else 4,
                    selected_tracks=0 if index == 0 else index % 5,
                    full_rows=0 if index == 0 else 8,
                    selected_rows=0 if index == 0 else index,
                    full_state_information_retention=(
                        -1.0 if extrema_tie else 0.1 * index
                    ),
                    pose_information_retention=0.11 * index,
                    velocity_information_retention=0.12 * index,
                    clone_information_retention=0.13 * index,
                    pose_velocity_clone_information_retention=0.14 * index,
                    full_state_logdet_information_retention=0.15 * index,
                    correction_mahalanobis_distance=(
                        3.0 if extrema_tie else 0.16 * index
                    ),
                    measured_cost_fraction=(
                        2.0 if extrema_tie else 0.17 * index
                    ),
                    predicted_cost_fraction=0.18 * index,
                    subset_posterior_trace=10.0 + 0.19 * index,
                    full_posterior_trace=20.0 + 0.20 * index,
                    subset_posterior_log_pseudodeterminant=0.21 * index,
                    full_posterior_log_pseudodeterminant=0.22 * index,
                    subset_posterior_effective_rank=index,
                    full_posterior_effective_rank=10 + index,
                    cost_model_absolute_error_ns=0.23 * index,
                    cost_model_relative_error=0.24 * index,
                    blockwise_correction_l2=(
                        {"imu": float(index)}
                        if index % 2 == 0
                        else {"clone:1": float(index)}
                    ),
                    covariance_conservative=index % 2 == 0,
                )
            )
        expected = summarize_sequence(tuple(results))
        accumulator = _ExactSequenceAccumulator()
        for result in results:
            accumulator.add(result)
        self.assertEqual(accumulator.summarize(), expected)
        self.assertEqual(expected.worst_information_update_id, 1)
        self.assertEqual(expected.worst_correction_update_id, 1)
        self.assertEqual(expected.worst_measured_cost_update_id, 1)
        self.assertEqual(
            accumulator.numeric_storage_bytes,
            len(_ACCUMULATED_QUANTILE_FIELDS) * len(results) * 8,
        )

    def test_compact_accumulator_rejects_empty_and_mixed_identity(self) -> None:
        accumulator = _ExactSequenceAccumulator()
        with self.assertRaisesRegex(PolicyEvaluationError, "empty"):
            accumulator.summarize()
        envelope = _multi_envelope()
        result = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["original_order"],
            BudgetSpec(BudgetKind.TRACK, 0.5),
            _fit_model(envelope),
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        accumulator.add(result)
        with self.assertRaisesRegex(PolicyEvaluationError, "mixes"):
            accumulator.add(dataclasses.replace(result, sequence_id="corridor4"))

    def test_capture_grid_rejects_empty_or_duplicate_axes_before_io(self) -> None:
        capture = CaptureSpec("room4", "unused.bin")
        model = _fit_model(_multi_envelope())
        policy = POLICY_BY_NAME["original_order"]
        budget = BudgetSpec(BudgetKind.TRACK, 0.5)
        with self.assertRaisesRegex(PolicyEvaluationError, "at least one policy"):
            evaluate_capture(
                capture,
                dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                cost_model=model,
                policies=(),
                budgets=(budget,),
            )
        with self.assertRaisesRegex(PolicyEvaluationError, "at least one budget"):
            evaluate_capture(
                capture,
                dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                cost_model=model,
                policies=(policy,),
                budgets=(),
            )
        with self.assertRaisesRegex(PolicyEvaluationError, "policy names"):
            evaluate_capture(
                capture,
                dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                cost_model=model,
                policies=(policy, policy),
                budgets=(budget,),
            )
        with self.assertRaisesRegex(PolicyEvaluationError, "budgets"):
            evaluate_capture(
                capture,
                dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                cost_model=model,
                policies=(policy,),
                budgets=(budget, budget),
            )

    def test_policy_registry_separates_deployability_and_two_field_limit(self) -> None:
        names = {policy.name for policy in POLICIES}
        self.assertEqual(len(names), len(POLICIES))
        self.assertIn("geometry_parallax", names)
        self.assertIn("greedy_marginal_information_per_cost", names)
        for policy in POLICIES:
            if policy.deployability == Deployability.PRE_FACTOR:
                self.assertLessEqual(len(policy.causal_fields), 2)
        self.assertEqual(
            POLICY_BY_NAME["true_2d_parallax"].deployability,
            Deployability.PRE_FACTOR,
        )
        self.assertEqual(
            POLICY_BY_NAME["geometry_parallax"].deployability,
            Deployability.PARTIAL_COST,
        )

    def test_raw_parallax_and_motion_never_cross_camera_boundaries(self) -> None:
        base = _multi_envelope().candidates[0]
        observations = (
            Observation(0, 1.0, 10.0, 10.0, True, 0.0, 0.0),
            Observation(0, 1.1, 13.0, 14.0, True, 0.1, 0.0),
            Observation(1, 1.0, 1000.0, 1000.0, True, 10.0, 10.0),
        )
        candidate = dataclasses.replace(
            base,
            observations=observations,
            raw_observation_count=3,
            cleaned_observation_count=3,
        )
        expected_angle = math.acos(1.0 / math.sqrt(1.01))
        self.assertAlmostEqual(_bearing_parallax(candidate), expected_angle)
        self.assertEqual(_pixel_motion(candidate), 5.0)

    def test_rankings_are_deterministic_and_use_declared_fields(self) -> None:
        envelope = _multi_envelope()
        views = build_candidate_views(envelope)
        observation_order = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["observation_count"],
            sequence_id="room4",
        )
        self.assertEqual(observation_order, (3, 2, 1, 0))
        spatial = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["spatial_round_robin"],
            sequence_id="room4",
        )
        self.assertEqual(spatial, (0, 2, 1, 3))
        random_a = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["seeded_random"],
            sequence_id="room4",
            seed=1729,
        )
        random_b = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["seeded_random"],
            sequence_id="room4",
            seed=1729,
        )
        self.assertEqual(random_a, random_b)
        self.assertEqual(set(random_a), {0, 1, 2, 3})

    def test_partial_and_post_factor_oracle_rankings_are_deterministic(self) -> None:
        envelope = _multi_envelope()
        views = build_candidate_views(envelope)
        geometry = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["geometry_parallax"],
            sequence_id="room4",
        )
        self.assertEqual(geometry, (0, 1, 2, 3))
        trace = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["trace_information"],
            sequence_id="room4",
        )
        greedy = rank_candidates(
            envelope,
            views,
            POLICY_BY_NAME["greedy_marginal_information_per_cost"],
            sequence_id="room4",
        )
        self.assertEqual(set(trace), {0, 1, 2, 3})
        self.assertEqual(set(greedy), {0, 1, 2, 3})
        self.assertEqual(
            greedy,
            rank_candidates(
                envelope,
                views,
                POLICY_BY_NAME["greedy_marginal_information_per_cost"],
                sequence_id="room4",
            ),
        )

    def test_track_row_and_cost_budgets_are_prefixes(self) -> None:
        envelope = _multi_envelope()
        views = build_candidate_views(envelope)
        model = _fit_model(envelope)
        policy = POLICY_BY_NAME["original_order"]
        ranking = (0, 1, 2, 3)
        tracks = select_budget(
            views, ranking, policy, BudgetSpec(BudgetKind.TRACK, 0.5), model
        )
        self.assertEqual(tracks.processed_ordinals, (0, 1))
        row_views = (
            dataclasses.replace(views[0], rows=2),
            dataclasses.replace(views[1], rows=2),
            dataclasses.replace(views[2], rows=1),
            dataclasses.replace(views[3], rows=1),
        )
        rows = select_budget(
            row_views, ranking, policy, BudgetSpec(BudgetKind.ROW, 0.5), model
        )
        self.assertEqual(rows.processed_ordinals, (0, 1))
        self.assertEqual(rows.selected_ordinals, (0,))
        costs = select_budget(
            views, ranking, policy, BudgetSpec(BudgetKind.COST, 0.5), model
        )
        self.assertEqual(costs.processed_ordinals, tuple(range(len(costs.processed_ordinals))))
        self.assertLess(len(costs.processed_ordinals), 4)

    def test_full_budget_reconstructs_full_metrics_and_measured_cost(self) -> None:
        envelope = _multi_envelope()
        model = _fit_model(envelope)
        result = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["observation_count"],
            BudgetSpec(BudgetKind.TRACK, 1.0),
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        self.assertAlmostEqual(result.full_state_information_retention, 1.0)
        self.assertAlmostEqual(result.full_state_logdet_information_retention, 1.0)
        self.assertLess(result.correction_l2, 1.0e-10)
        self.assertLess(result.correction_mahalanobis_distance, 1.0e-8)
        self.assertTrue(result.covariance_conservative)
        self.assertAlmostEqual(result.measured_cost_fraction, 1.0)
        self.assertEqual(result.selected_feature_ids, (13, 12, 11, 10))

    def test_every_policy_and_full_budget_share_exact_fast_baseline(self) -> None:
        envelope = _multi_envelope()
        model = _fit_model(envelope)
        accepted_ids = frozenset(envelope.accepted_feature_ids)

        for policy in POLICIES:
            for budget_kind in BudgetKind:
                with self.subTest(policy=policy.name, budget=budget_kind.value):
                    result = evaluate_policy_update(
                        envelope,
                        policy,
                        BudgetSpec(budget_kind, 1.0),
                        model,
                        dataset_role=DatasetRole.TUM_VI_HELD_OUT,
                        sequence_id="room4",
                    )
                    self.assertEqual(
                        frozenset(result.selected_feature_ids), accepted_ids
                    )
                    self.assertEqual(result.selected_tracks, len(accepted_ids))
                    self.assertEqual(result.full_state_information_retention, 1.0)
                    self.assertEqual(
                        result.full_state_logdet_information_retention, 1.0
                    )
                    self.assertEqual(
                        result.subset_information_trace,
                        result.full_information_trace,
                    )
                    self.assertEqual(
                        result.subset_information_logdet,
                        result.full_information_logdet,
                    )
                    self.assertEqual(
                        result.subset_posterior_trace,
                        result.full_posterior_trace,
                    )
                    self.assertEqual(
                        result.subset_posterior_log_pseudodeterminant,
                        result.full_posterior_log_pseudodeterminant,
                    )
                    self.assertEqual(
                        result.subset_posterior_effective_rank,
                        result.full_posterior_effective_rank,
                    )
                    self.assertEqual(result.correction_l2, 0.0)
                    self.assertEqual(result.correction_mahalanobis_distance, 0.0)
                    self.assertTrue(result.covariance_conservative)
                    self.assertEqual(
                        result.covariance_difference_min_eigenvalue, 0.0
                    )
                    self.assertTrue(
                        all(
                            difference == 0.0
                            for difference in result.blockwise_correction_l2.values()
                        )
                    )

    def test_subset_is_conservative_and_cost_classes_charge_sunk_stages(self) -> None:
        envelope = _multi_envelope()
        model = _fit_model(envelope)
        budget = BudgetSpec(BudgetKind.TRACK, 0.25)
        pre = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["original_order"],
            budget,
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        partial = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["geometry_parallax"],
            budget,
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        oracle = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["trace_information"],
            budget,
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        self.assertTrue(pre.covariance_conservative)
        self.assertGreaterEqual(pre.full_state_information_retention, 0.0)
        self.assertLess(pre.full_state_information_retention, 1.0)
        self.assertLess(pre.measured_cost_fraction, partial.measured_cost_fraction)
        self.assertLess(partial.measured_cost_fraction, oracle.measured_cost_fraction)
        self.assertFalse(pre.required_partial_stage_cost_charged)
        self.assertTrue(pre.score_computation_cost_measured)
        self.assertTrue(partial.required_partial_stage_cost_charged)
        self.assertFalse(partial.score_computation_cost_measured)
        self.assertTrue(oracle.required_partial_stage_cost_charged)
        self.assertFalse(oracle.score_computation_cost_measured)

    def test_zero_candidate_callback_remains_a_valid_full_retention_point(self) -> None:
        envelope = _zero_update_envelope()
        model = _fit_model(_multi_envelope())
        result = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["original_order"],
            BudgetSpec(BudgetKind.COST, 0.5),
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        self.assertEqual(result.candidate_count, 0)
        self.assertEqual(result.selected_tracks, 0)
        self.assertEqual(result.full_state_information_retention, 1.0)
        self.assertEqual(result.correction_l2, 0.0)
        self.assertTrue(result.covariance_conservative)
        self.assertEqual(result.measured_cost_fraction, 1.0)

    def test_cost_model_fit_is_deterministic_and_tum_vi_only(self) -> None:
        envelope = _multi_envelope()
        first = _fit_model(envelope)
        second = _fit_model(envelope)
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.feature_names, COST_FEATURE_NAMES)
        self.assertEqual(first.training_sequences, ("corridor4", "room4"))
        with self.assertRaisesRegex(PolicyEvaluationError, "TUM-VI"):
            dataclasses.replace(first, training_dataset="euroc")

    def test_sequence_summary_has_declared_quantiles_and_worst_update(self) -> None:
        envelope = _multi_envelope()
        model = _fit_model(envelope)
        full = evaluate_policy_update(
            envelope,
            POLICY_BY_NAME["original_order"],
            BudgetSpec(BudgetKind.TRACK, 1.0),
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        subset = evaluate_policy_update(
            dataclasses.replace(envelope, update_id=1),
            POLICY_BY_NAME["original_order"],
            BudgetSpec(BudgetKind.TRACK, 1.0),
            model,
            dataset_role=DatasetRole.TUM_VI_HELD_OUT,
            sequence_id="room4",
        )
        summary = summarize_sequence((full, subset))
        self.assertEqual(summary.update_count, 2)
        self.assertEqual(summary.cost_model_error_p50, summary.cost_model_error_p95)
        aggregate = aggregate_policy_results((summary,))
        self.assertEqual(aggregate.sequence_count, 1)
        self.assertEqual(aggregate.update_count, 2)

    def test_frozen_policy_round_trip_forbids_euroc_selection(self) -> None:
        model = _fit_model(_multi_envelope())
        frozen = FrozenPolicy(
            1,
            POLICY_BY_NAME["observation_count"],
            BudgetSpec(BudgetKind.COST, 0.5),
            1729,
            model,
            "tum_vi",
            ("corridor4", "room4"),
        )
        self.assertEqual(FrozenPolicy.from_json(frozen.to_json()), frozen)
        with self.assertRaisesRegex(PolicyEvaluationError, "TUM-VI"):
            dataclasses.replace(frozen, selection_dataset="euroc")
        with self.assertRaisesRegex(PolicyEvaluationError, "MH_04"):
            evaluate_euroc_replication((CaptureSpec("MH_04_difficult", "x"),), frozen)

    def test_loso_training_excludes_each_held_out_sequence(self) -> None:
        captures = (CaptureSpec("room4", "a.bin"), CaptureSpec("corridor4", "b.bin"))
        training_calls = []

        def fit(paths):
            names = tuple(name for name, _ in paths)
            training_calls.append(names)
            return CostModel(COST_FEATURE_NAMES, (0.0,) * 5, (0.0,) * 5, names)

        def evaluate(capture, **_kwargs):
            return (_sequence_summary(capture.sequence_id),)

        with mock.patch(
            "scripts.analysis.anytime_policy_evaluation.fit_cost_model_from_captures",
            side_effect=fit,
        ), mock.patch(
            "scripts.analysis.anytime_policy_evaluation.evaluate_capture",
            side_effect=evaluate,
        ):
            sequences, aggregates = evaluate_tum_vi_loso(
                captures,
                policies=(POLICY_BY_NAME["original_order"],),
                budgets=(BudgetSpec(BudgetKind.TRACK, 0.5),),
            )
        self.assertEqual(training_calls, [("room4",), ("corridor4",)])
        self.assertEqual(len(sequences), 2)
        self.assertEqual(len(aggregates), 1)

    def test_output_is_deterministic_create_new_and_json_friendly(self) -> None:
        sequence = _sequence_summary("room4")
        aggregate = aggregate_policy_results((sequence,))
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "result"
            write_evaluation_outputs(
                output,
                mode="test",
                captures=(CaptureSpec("room4", "capture.bin"),),
                sequences=(sequence,),
                aggregates=(aggregate,),
            )
            self.assertTrue((output / "POLICY_RESULTS.csv").is_file())
            self.assertTrue((output / "PER_SEQUENCE_RESULTS.csv").is_file())
            self.assertTrue((output / "BUDGET_CURVES.csv").is_file())
            summary = json.loads((output / "STUDY_SUMMARY.json").read_text())
            self.assertEqual(summary["mode"], "test")
            with self.assertRaises(FileExistsError):
                write_evaluation_outputs(
                    output,
                    mode="test",
                    captures=(CaptureSpec("room4", "capture.bin"),),
                    sequences=(sequence,),
                    aggregates=(aggregate,),
                )


if __name__ == "__main__":
    unittest.main()
