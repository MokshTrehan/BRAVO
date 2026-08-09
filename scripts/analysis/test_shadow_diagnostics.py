#!/usr/bin/env python3
"""Fast synthetic contract tests for shadow_diagnostics.py."""

import csv
import tempfile
import unittest
from pathlib import Path

from scripts.analysis import shadow_diagnostics as subject


def _causal(timestamp: float, tracks: int, rows: int, nis_per_row: float) -> str:
    return (
        "[MSCKF-SHADOW-CAUSAL]: timestamp={timestamp:.3f} shadow_only=1 "
        "finalized_before_pass2=1 diagnostics_available=1 "
        "accepted_tracks_pass1={tracks} pass1_compressed_rows={rows} "
        "pass1_compressed_nis_per_row_available=1 "
        "pass1_compressed_nis_per_row={nis_per_row} "
        "pass1_reduced_whitened_residual_rms_available=1 "
        "pass1_reduced_whitened_residual_rms=0.25 "
        "pass1_prior_whitened_correction_norm_available=1 "
        "pass1_prior_whitened_correction_norm=0.5 "
        "pass1_imu_block_norms_available=1 "
        "pass1_orientation_correction_norm=0.01 "
        "pass1_position_correction_norm=0.02 "
        "pass1_velocity_correction_norm=0.03 "
        "pass1_gyro_bias_correction_norm=0.04 "
        "pass1_accelerometer_bias_correction_norm=0.05 "
        "pass1_clone_aggregate_correction_norm_available=1 "
        "pass1_clone_aggregate_correction_norm=0.06 "
        "pass1_minimum_schur_singular_ratio_available=1 "
        "pass1_minimum_schur_singular_ratio=0.07 "
        "pass1_median_track_observations_available=1 "
        "pass1_median_track_observations=4"
    ).format(timestamp=timestamp, tracks=tracks, rows=rows, nis_per_row=nis_per_row)


def _valid_frame(timestamp: float) -> list:
    return [
        _causal(timestamp, 2, 4, 0.5),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=1 "
            "pass=1 status=accepted accepted_features=2 accepted_set_hash=12345 "
            "rows=4 global_proposal_nis=2 max_feature_gate_nis=2 "
            "threshold_at_max_feature_nis=4 Cpix=10 Cpost=20 dx_norm=0.1 "
            "processing_ms=5"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=2 "
            "pass=2 status=accepted accepted_features=2 raw_rows=7 rows=4 "
            "max_feature_nis=1 threshold_at_max_feature_nis=4 "
            "affine_correction_norm=0.01 Cpix=8 Cpost=19 dx_norm=0.2 "
            "processing_ms=2 repairs=0"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=2 "
            "selected_pass=1 oracle_selected_pass=2 "
            "oracle_reason=dual_cost_accepted accepted_features=2 "
            "cost_difference_available=1 pixel_difference=2 "
            "posterior_difference=1 pixel_tolerance=1e-8 "
            "posterior_tolerance=2e-8 oracle_effect_on_live=0"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} terminal=1 shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=2 "
            "selected_pass=1 oracle_selected_pass=2 "
            "oracle_reason=dual_cost_accepted accepted_features=2 "
            "status=committed mean_commits=1 covariance_commits=1 "
            "feature_finalizations=1"
        ).format(timestamp=timestamp),
    ]


def _invalid_frame(timestamp: float) -> list:
    return [
        _causal(timestamp, 3, 3, 1.0),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=1 "
            "pass=1 status=accepted accepted_features=3 accepted_set_hash=67890 "
            "rows=3 global_proposal_nis=3 max_feature_gate_nis=3 "
            "threshold_at_max_feature_nis=6 Cpix=30 Cpost=40 dx_norm=0.3 "
            "processing_ms=4"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=1 "
            "pass=2 status=invalid reason=gate feature=9 accepted_features=3 "
            "gate_diagnostics_available=1 feature_nis=7 "
            "threshold_at_feature_nis=6 processing_ms=1.5"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=1 "
            "selected_pass=1 oracle_selected_pass=1 oracle_reason=gate "
            "accepted_features=3 cost_difference_available=0 "
            "pixel_difference=0 posterior_difference=0 pixel_tolerance=3e-8 "
            "posterior_tolerance=4e-8 oracle_effect_on_live=0"
        ).format(timestamp=timestamp),
        (
            "[MSCKF-ITER]: timestamp={timestamp:.3f} terminal=1 shadow_only=1 "
            "requested_passes=2 attempted_passes=2 completed_passes=1 "
            "selected_pass=1 oracle_selected_pass=1 oracle_reason=gate "
            "accepted_features=3 status=committed mean_commits=1 "
            "covariance_commits=1 feature_finalizations=1"
        ).format(timestamp=timestamp),
    ]


class ShadowDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="shadow-diag-test-")
        self.run_dir = Path(self.temporary.name)
        (self.run_dir / "exit_status.txt").write_text("0\n", encoding="utf-8")
        (self.run_dir / "timed_out.txt").write_text("0\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_run(self, lines: list, timing_rows: list) -> None:
        startup = ["    - max_visual_passes: 2", "    - pass2_shadow_only: 1"]
        (self.run_dir / "stdout.log").write_text(
            "\n".join(startup + lines) + "\n", encoding="utf-8"
        )
        header = (
            "# timestamp (sec),tracking,propagation,msckf update,slam update,"
            "slam delayed,re-tri & marg,total\n"
        )
        encoded = "".join(
            ",".join(str(value) for value in row) + "\n" for row in timing_rows
        )
        (self.run_dir / "timing.csv").write_text(header + encoded, encoding="utf-8")

    def test_valid_and_invalid_oracle_rows_preserve_live_selection(self) -> None:
        lines = _valid_frame(1.0) + _invalid_frame(2.0)
        self._write_run(
            lines,
            [
                (1.0, 0.001, 0.001, 0.005, 0, 0, 0.001, 0.010),
                (2.0, 0.001, 0.001, 0.004, 0, 0, 0.001, 0.008),
            ],
        )
        frames = subject.extract_run(self.run_dir, "MH_01", "schur_shadow")
        self.assertEqual([row["selected_pass"] for row in frames], [1, 1])
        self.assertEqual([row["oracle_selected_pass"] for row in frames], [2, 1])
        self.assertEqual([row["pass2_valid"] for row in frames], [True, False])
        self.assertAlmostEqual(frames[0]["shadow_skip_total"], 0.008)
        self.assertAlmostEqual(frames[1]["shadow_skip_total"], 0.0065)
        self.assertEqual(frames[0]["reduced_rows_pass1"], 4)
        self.assertAlmostEqual(frames[0]["pass1_max_gate_ratio"], 0.5)
        output = self.run_dir / "diagnostics.csv"
        subject.write_diagnostics(output, frames)
        with output.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            rows = list(reader)
        self.assertNotIn("accepted_set_hash", reader.fieldnames)
        self.assertEqual(rows[0]["oracle_reason"], "dual_cost_accepted")
        self.assertEqual(rows[1]["pass2_invalid_reason"], "gate")

    def test_duplicate_oracle_decision_fails_closed(self) -> None:
        lines = _valid_frame(1.0)
        lines.insert(-1, lines[-2])
        self._write_run(
            lines,
            [(1.0, 0.001, 0.001, 0.005, 0, 0, 0.001, 0.010)],
        )
        with self.assertRaisesRegex(subject.ExtractionError, "oracle decision"):
            subject.extract_run(self.run_dir, "MH_01", "schur_shadow")

    def test_non_bijective_timing_join_fails_closed(self) -> None:
        self._write_run(
            _valid_frame(1.0),
            [(1.01, 0.001, 0.001, 0.005, 0, 0, 0.001, 0.010)],
        )
        with self.assertRaisesRegex(subject.ExtractionError, "timestamp mismatch"):
            subject.extract_run(self.run_dir, "MH_01", "schur_shadow")


if __name__ == "__main__":
    unittest.main()
