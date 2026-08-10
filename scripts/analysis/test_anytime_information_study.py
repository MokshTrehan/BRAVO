#!/usr/bin/env python3
"""Deterministic tests for Schema-2 full-update reconstruction."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from experiments.anytime_information.capture_reader import (  # noqa: E402
    CallbackCosts,
    CallbackTimeline,
    CameraDescriptor,
    CandidateRecord,
    CaptureHeader,
    CaptureOutputMetadata,
    GlobalGate,
    GlobalSystem,
    LayoutBlock,
    Matrix,
    Observation,
    StateBlock,
    TerminalStatus,
    TrackRecord,
    UpdateEnvelope,
)
from scripts.analysis.anytime_information_study import (  # noqa: E402
    ReconstructionError,
    covariance_diagnostics,
    reconstruct_update,
    stage_cost_accounting,
    validate_capture_reconstruction,
)


def _matrix(values: np.ndarray | list[list[float]] | list[float]) -> Matrix:
    array = np.asarray(values, dtype=np.float64)
    if array.ndim == 1:
        array = array.reshape((-1, 1))
    if array.ndim != 2:
        raise AssertionError("fixture matrix must be two-dimensional")
    return Matrix(array.shape[0], array.shape[1], tuple(array.ravel()))


def _empty_matrix(rows: int = 0, cols: int = 0) -> Matrix:
    return Matrix(rows, cols, ())


def _system(
    h: np.ndarray | None,
    residual: np.ndarray | None = None,
    covariance: np.ndarray | None = None,
    *,
    duration_ns: int = 0,
) -> GlobalSystem:
    if h is None:
        return GlobalSystem(
            available=False,
            h=_empty_matrix(),
            residual=_empty_matrix(0, 1),
            covariance=_empty_matrix(),
            layout=(),
            duration_ns=0,
        )
    assert residual is not None and covariance is not None
    return GlobalSystem(
        available=True,
        h=_matrix(h),
        residual=_matrix(residual),
        covariance=_matrix(covariance),
        layout=(LayoutBlock(0, 0, 2),),
        duration_ns=duration_ns,
    )


def _production_posterior(
    prior: np.ndarray, h: np.ndarray, residual: np.ndarray, covariance: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    cross = prior @ h.T
    innovation = h @ cross + covariance
    innovation_inverse = np.linalg.inv(innovation)
    gain = cross @ innovation_inverse
    dx = gain @ residual
    delta = gain @ cross.T
    p_plus = prior.copy()
    upper = np.triu_indices(prior.shape[0])
    p_plus[upper] -= delta[upper]
    p_plus = np.triu(p_plus) + np.triu(p_plus, 1).T
    return dx, p_plus


def _candidate() -> CandidateRecord:
    observations = (
        Observation(0, 1.0, 10.0, 12.0, True, 0.10, 0.12),
        Observation(0, 1.1, 11.0, 13.0, True, 0.11, 0.13),
    )
    return CandidateRecord(
        ordinal=0,
        feature_id=17,
        lifecycle_reason=1,
        lifecycle_status=1,
        raw_observation_count=2,
        cleaned_observation_count=2,
        prefilter_recorded=True,
        prefilter_accepted=True,
        prefilter_duration_ns=11,
        time_range_available=True,
        first_timestamp=1.0,
        last_timestamp=1.1,
        track_age=0.1,
        observations=observations,
        parallax_2d_available=True,
        maximum_normalized_parallax=0.01,
        maximum_image_motion_available=True,
        maximum_pixel_displacement=1.5,
        last_frame_displacement_available=True,
        last_frame_pixel_displacement=1.5,
        final_location_available=True,
        final_normalized_u=0.11,
        final_normalized_v=0.13,
        motion_kind=0,
        motion_available=False,
        motion_translation=0.0,
        motion_rotation=0.0,
    )


def _track() -> TrackRecord:
    # The first three raw rows constrain only the temporary landmark.  The
    # fourth row is its one-dimensional orthogonal complement and therefore
    # reduces exactly to the captured A/b row below.
    h_x = np.asarray(
        ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (1.0, -0.5)),
        dtype=np.float64,
    )
    h_f = np.asarray(
        ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (0.0, 0.0, 0.0)),
        dtype=np.float64,
    )
    return TrackRecord(
        update_id=0,
        candidate_ordinal=0,
        feature_id=17,
        geometry_recorded=True,
        triangulation_attempted=True,
        triangulation_succeeded=True,
        refinement_attempted=True,
        refinement_succeeded=True,
        geometry_valid=True,
        p_f_in_g=_matrix([1.0, 2.0, 3.0]),
        depth_available=True,
        depth=3.0,
        parallax_3d_available=True,
        parallax_3d=0.02,
        geometry_duration_ns=13,
        raw_available=True,
        h_x=_matrix(h_x),
        h_f=_matrix(h_f),
        residual=_matrix([0.0, 0.0, 0.0, 0.25]),
        sigma_px=1.5,
        noise_variance=2.25,
        whitening_kind=1,
        layout=(LayoutBlock(0, 0, 2),),
        raw_factor_duration_ns=17,
        reduction_recorded=True,
        reducer=1,
        reducer_status=1,
        reducer_stage=13,
        singular_values_available=True,
        singular_values=_matrix([1.0, 1.0, 1.0]),
        numerical_rank=3,
        singular_ratio_available=True,
        singular_ratio=1.0,
        reduced_a=_matrix([[1.0, -0.5]]),
        reduced_b=_matrix([0.25]),
        reduction_duration_ns=19,
        gate_recorded=True,
        gate_stage=5,
        gate_dof=1,
        nis_available=True,
        nis=0.01,
        gate_threshold_available=True,
        gate_threshold=3.84,
        evidence_decision_available=True,
        evidence_accept=True,
        lifecycle_accept=True,
        gate_duration_ns=23,
        accepted_for_global_system=True,
        global_row_start=0,
        global_row_count=1,
        accumulation_duration_ns=29,
    )


def _accepted_envelope() -> UpdateEnvelope:
    prior = np.asarray(((2.0, 0.1), (0.1, 3.0)), dtype=np.float64)
    h = np.asarray(((1.0, -0.5),), dtype=np.float64)
    residual = np.asarray((0.25,), dtype=np.float64)
    covariance = np.asarray(((2.25,),), dtype=np.float64)
    dx, p_plus = _production_posterior(prior, h, residual, covariance)
    return UpdateEnvelope(
        domain="schurvio.update_envelope.v2",
        update_id=0,
        camera_timestamp=1.1,
        terminal_status=TerminalStatus.COMMITTED,
        terminal_reason=1,
        max_visual_passes=1,
        landmark_elimination=1,
        fej_enabled=True,
        calibrate_camera_pose=False,
        calibrate_camera_intrinsics=False,
        calibrate_camera_timeoffset=False,
        calibrate_imu_intrinsics=False,
        calibrate_imu_g_sensitivity=False,
        feature_representation=0,
        callback_costs=CallbackCosts(0.1, 0.2, 0.3, 0.0, 0.0, 0.4, 1.0),
        callback_timeline=CallbackTimeline(
            True, 0.0, 0.1, 0.3, 0.6, 0.6, 0.6, 1.0
        ),
        capture_output=CaptureOutputMetadata(True, 47, 53, 1000),
        cameras=(
            CameraDescriptor(
                0,
                1,
                640,
                480,
                -1,
                -1,
                _matrix([[1.0]]),
                _matrix([[1.0]]),
                _matrix([[1.0]]),
                _matrix([[1.0]]),
                _matrix([[1.0]]),
            ),
        ),
        state_blocks=(
            StateBlock(
                0,
                1,
                0,
                0,
                0.0,
                0,
                0,
                0,
                2,
                3,
                _matrix([0.0, 0.0]),
                _matrix([0.0, 0.0]),
            ),
        ),
        p_minus=_matrix(prior),
        candidates=(_candidate(),),
        tracks=(_track(),),
        accepted_feature_ids=(17,),
        selected_system=_system(h, residual, covariance, duration_ns=31),
        compressed_system=_system(h, residual, covariance, duration_ns=37),
        posterior_recorded=True,
        preview_status=0,
        preview_stage=13,
        global_gate=GlobalGate(False, False, 0.0, False),
        production_dx=_matrix(dx),
        preview_duration_ns=41,
        p_plus=_matrix(p_plus),
        mean_commit_count=1,
        covariance_commit_count=1,
        feature_finalization_count=1,
        commit_duration_ns=43,
    )


def _zero_update_envelope() -> UpdateEnvelope:
    envelope = _accepted_envelope()
    return dataclasses.replace(
        envelope,
        terminal_status=TerminalStatus.EMPTY_INPUT,
        terminal_reason=2,
        candidates=(),
        tracks=(),
        accepted_feature_ids=(),
        selected_system=_system(None),
        compressed_system=_system(None),
        posterior_recorded=False,
        preview_status=0,
        preview_stage=0,
        production_dx=_empty_matrix(0, 1),
        preview_duration_ns=0,
        p_plus=_empty_matrix(),
        mean_commit_count=0,
        covariance_commit_count=0,
        feature_finalization_count=0,
        commit_duration_ns=0,
    )


class AnytimeInformationStudyTest(unittest.TestCase):
    def test_reconstructs_information_and_three_posteriors(self) -> None:
        result = reconstruct_update(_accepted_envelope())
        expected_h = np.asarray(((1.0, -0.5),), dtype=np.float64)
        expected_information = expected_h.T @ expected_h / 2.25
        expected_gradient = expected_h.reshape(-1) * (0.25 / 2.25)

        self.assertTrue(result.passed)
        np.testing.assert_allclose(result.selected.information, expected_information)
        np.testing.assert_allclose(result.selected.gradient, expected_gradient)
        self.assertIsNotNone(result.posterior)
        self.assertIsNotNone(result.full_joint)
        assert result.posterior is not None and result.full_joint is not None
        self.assertTrue(result.full_joint.supported)
        np.testing.assert_allclose(result.full_joint.information, expected_information)
        np.testing.assert_allclose(result.full_joint.gradient, expected_gradient)
        np.testing.assert_allclose(result.full_joint.dx, result.posterior.dx)
        np.testing.assert_allclose(
            result.full_joint.p_plus, result.posterior.independent_p_plus
        )

    def test_compressed_scaled_row_preserves_information(self) -> None:
        envelope = _accepted_envelope()
        compressed = _system(
            np.asarray(((2.0, -1.0),)),
            np.asarray((0.5,)),
            np.asarray(((9.0,),)),
            duration_ns=37,
        )
        result = reconstruct_update(
            dataclasses.replace(envelope, compressed_system=compressed)
        )
        self.assertTrue(result.passed)

    def test_corrupt_production_dx_fails_strict_reconstruction(self) -> None:
        envelope = dataclasses.replace(
            _accepted_envelope(), production_dx=_matrix([100.0, -100.0])
        )
        with self.assertRaisesRegex(ReconstructionError, "production_dx"):
            reconstruct_update(envelope)
        result = reconstruct_update(envelope, strict=False)
        self.assertFalse(next(c for c in result.checks if c.name == "production_dx").passed)

    def test_selected_track_row_mismatch_is_exactly_rejected(self) -> None:
        envelope = _accepted_envelope()
        bad_track = dataclasses.replace(
            envelope.tracks[0], reduced_b=_matrix([0.25000000000000006])
        )
        with self.assertRaisesRegex(
            ReconstructionError, "selected_track_rows_residual"
        ):
            reconstruct_update(dataclasses.replace(envelope, tracks=(bad_track,)))

    def test_raw_full_joint_mismatch_is_rejected(self) -> None:
        envelope = _accepted_envelope()
        raw_h_x = np.asarray(
            ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0), (2.0, -0.5))
        )
        bad_track = dataclasses.replace(envelope.tracks[0], h_x=_matrix(raw_h_x))
        with self.assertRaisesRegex(ReconstructionError, "full_joint_information"):
            reconstruct_update(dataclasses.replace(envelope, tracks=(bad_track,)))

    def test_full_joint_uses_stable_pseudoinverse_nullspace_rows(self) -> None:
        envelope = _accepted_envelope()
        left_null = np.asarray((0.5, -0.5, 0.5, -0.5))
        h_f = np.asarray(
            (
                (253.0, 1.0, 2.0),
                (3.0, 252.0, 4.0),
                (5.0, 6.0, 3.0),
                (255.0, -245.0, 1.0),
            )
        )
        column_mixture = np.asarray(((2.0, -1.0), (-3.0, 4.0), (1.5, 2.0)))
        reduced_a = np.asarray(((1.0e-13, -5.0e-14),))
        h_x = h_f @ column_mixture + np.outer(left_null, reduced_a.reshape(-1))
        reduced_b = np.asarray((0.2,))
        residual = h_f @ np.asarray((0.1, -0.2, 0.3)) + left_null * reduced_b[0]

        # This is the cancellation that motivated the stable SVD row basis:
        # explicitly materializing I-H_f H_f^+ fabricates visible information.
        explicit_projector = np.eye(4) - h_f @ np.linalg.pinv(h_f)
        explicit_information = h_x.T @ explicit_projector @ h_x
        self.assertGreater(np.linalg.norm(explicit_information), 1.0e-10)

        singular_values = np.linalg.svd(h_f, compute_uv=False)
        track = dataclasses.replace(
            envelope.tracks[0],
            h_x=_matrix(h_x),
            h_f=_matrix(h_f),
            residual=_matrix(residual),
            sigma_px=1.0,
            noise_variance=1.0,
            singular_values=_matrix(singular_values),
            reduced_a=_matrix(reduced_a),
            reduced_b=_matrix(reduced_b),
        )
        selected = _system(reduced_a, reduced_b, np.eye(1), duration_ns=31)
        prior = np.asarray(envelope.p_minus.values).reshape(2, 2)
        dx, p_plus = _production_posterior(
            prior, reduced_a, reduced_b, np.eye(1)
        )
        envelope = dataclasses.replace(
            envelope,
            tracks=(track,),
            selected_system=selected,
            compressed_system=dataclasses.replace(selected, duration_ns=37),
            production_dx=_matrix(dx),
            p_plus=_matrix(p_plus),
        )

        result = reconstruct_update(envelope)
        self.assertTrue(result.passed)
        assert result.full_joint is not None
        self.assertLess(np.linalg.norm(result.full_joint.information), 1.0e-20)

    def test_zero_track_no_update_is_accounted_without_factorization(self) -> None:
        result = reconstruct_update(_zero_update_envelope())
        self.assertTrue(result.passed)
        self.assertFalse(result.accepted_update)
        self.assertEqual(result.candidate_count, 0)
        self.assertIsNone(result.posterior)
        self.assertIsNone(result.full_joint)
        self.assertFalse(np.any(result.selected.information))
        self.assertIn("no_update_commit_accounting", {c.name for c in result.checks})

    def test_stage_cost_accounting_has_exact_owners_and_excludes_output(self) -> None:
        costs = stage_cost_accounting(_accepted_envelope())
        self.assertEqual(costs.prefilter_ns, 11)
        self.assertEqual(costs.geometry_ns, 13)
        self.assertEqual(costs.residual_jacobian_ns, 17)
        self.assertEqual(costs.reduction_ns, 19)
        self.assertEqual(costs.gating_ns, 23)
        self.assertEqual(costs.track_accumulation_ns, 29)
        self.assertEqual(costs.selected_system_ns, 31)
        self.assertEqual(costs.compression_ns, 37)
        self.assertEqual(costs.preview_ns, 41)
        self.assertEqual(costs.commit_ns, 43)
        self.assertEqual(costs.charged_visual_total_ns, 264)
        self.assertEqual(costs.capture_record_construction_ns, 47)
        self.assertEqual(costs.capture_serialization_ns, 53)
        self.assertEqual(costs.capture_output_total_ns, 100)
        self.assertEqual(costs.capture_output_bytes, 1000)

    def test_orphan_stage_cost_is_rejected(self) -> None:
        envelope = _zero_update_envelope()
        envelope = dataclasses.replace(envelope, commit_duration_ns=1)
        with self.assertRaisesRegex(ReconstructionError, "commit_cost_owner"):
            reconstruct_update(envelope)

    def test_scaled_psd_accepts_semidefinite_and_rejects_negative(self) -> None:
        semidefinite = covariance_diagnostics(np.diag((1.0, 0.0)))
        negative = covariance_diagnostics(np.diag((1.0, -1.0e-3)))
        self.assertTrue(semidefinite.scaled_psd)
        self.assertFalse(negative.scaled_psd)

    def test_capture_summary_counts_committed_and_zero_callbacks(self) -> None:
        envelopes = (
            _accepted_envelope(),
            dataclasses.replace(_zero_update_envelope(), update_id=1),
        )

        class _FakeCapture:
            header = CaptureHeader(
                2,
                1,
                1,
                "0123456789abcdef",
                "ab" * 32,
                "fixture",
                "sequence",
                "schurvio.update_envelope.v2",
            )

            def __enter__(self) -> "_FakeCapture":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def __iter__(self):
                return iter(envelopes)

        with mock.patch(
            "scripts.analysis.anytime_information_study.open_capture",
            return_value=_FakeCapture(),
        ):
            summary = validate_capture_reconstruction("fixture.bin")

        self.assertTrue(summary.strict_validation)
        self.assertEqual(summary.update_count, 2)
        self.assertEqual(summary.accepted_update_count, 1)
        self.assertEqual(summary.no_update_count, 1)
        self.assertEqual(summary.zero_candidate_count, 1)
        self.assertEqual(summary.accepted_track_count, 1)
        self.assertEqual(summary.full_joint_supported_count, 1)
        self.assertEqual(summary.capture_record_construction_ns, 94)
        self.assertEqual(summary.capture_serialization_ns, 106)

    def test_empty_capture_is_rejected(self) -> None:
        class _EmptyCapture:
            header = CaptureHeader(
                2,
                1,
                1,
                "0" * 40,
                "ab" * 32,
                "fixture",
                "sequence",
                "schurvio.update_envelope.v2",
            )

            def __enter__(self) -> "_EmptyCapture":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def __iter__(self):
                return iter(())

        with mock.patch(
            "scripts.analysis.anytime_information_study.open_capture",
            return_value=_EmptyCapture(),
        ):
            with self.assertRaisesRegex(ReconstructionError, "no update envelopes"):
                validate_capture_reconstruction("empty.bin")


if __name__ == "__main__":
    unittest.main()
