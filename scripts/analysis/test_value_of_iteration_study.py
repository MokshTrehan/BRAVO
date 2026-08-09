#!/usr/bin/env python3
"""Deterministic, synthetic tests for value_of_iteration_study.py."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import value_of_iteration_study as study  # noqa: E402


def synthetic_grouped_frame(group_count=4, rows_per_group=24):
    rows = []
    for group_index in range(group_count):
        sequence = "S%02d" % group_index
        for row_index in range(rows_per_group):
            phase = (row_index + group_index) % 6
            strict = phase in (0, 1)
            harmful = phase in (4, 5)
            p1_pix = 10.0 + 0.1 * group_index
            p1_post = 20.0 + 0.2 * group_index
            if strict:
                p2_pix, p2_post = 0.90 * p1_pix, 0.92 * p1_post
            elif harmful:
                p2_pix, p2_post = 1.05 * p1_pix, 1.04 * p1_post
            else:
                p2_pix, p2_post = p1_pix, p1_post
            tracks = 8 + phase + group_index
            global_nis = 2.0 + 3.0 * strict + 0.1 * row_index
            rows.append({
                "dataset_row_id": "%s:%03d" % (sequence, row_index),
                "sequence": sequence,
                "timestamp": 1000.0 * group_index + 0.05 * row_index,
                "accepted_tracks_pass1": tracks,
                "reduced_rows_pass1": 2 * tracks + phase,
                "pass1_global_nis": global_nis,
                "pass1_nis_per_row": global_nis / (2 * tracks + phase),
                "pass1_max_gate_ratio": 0.5 + 0.25 * strict + 0.01 * phase,
                "pass1_cpix": p1_pix,
                "pass2_cpix": p2_pix,
                "pass1_cpost": p1_post,
                "pass2_cpost": p2_post,
                "pass1_dx_norm": 0.02 + 0.03 * strict + 0.001 * phase,
                "pass1_processing_ms": 0.5 + 0.08 * tracks,
                "pass2_valid": True,
                "selected_pass": 2 if strict else 1,
                "one_total": 0.010 + 0.0001 * phase,
                "two_total": 0.018 + 0.0002 * phase,
            })
    return study.compute_labels(pd.DataFrame(rows))


def synthetic_shadow_frame(rows_per_group=12):
    frame = synthetic_grouped_frame(
        group_count=len(study.STABLE_TEN), rows_per_group=rows_per_group
    )
    mapping = {
        "S%02d" % index: sequence
        for index, sequence in enumerate(study.STABLE_TEN)
    }
    frame["sequence"] = frame["sequence"].map(mapping)
    frame["dataset_row_id"] = [
        "shadow:%s:%03d" % (sequence, index)
        for sequence in study.STABLE_TEN
        for index in range(rows_per_group)
    ]
    base = np.arange(len(frame), dtype=float)
    for index, feature in enumerate(study.SHADOW_ONLY_CAUSAL_CANDIDATES):
        frame[feature] = 0.01 * (index + 1) + 1.0e-4 * base
    return frame


def shadow_diagnostic_row(sequence, timestamp, eligible):
    row = {
        "sequence": sequence,
        "timestamp": timestamp,
        "shadow_only": 1,
        "requested_passes": 2,
        "attempted_passes": 2 if eligible else 1,
        "completed_passes": 2 if eligible else 0,
        "pass1_valid": bool(eligible),
        "pass2_valid": bool(eligible),
        "selected_pass": 1 if eligible else 0,
        "oracle_selected_pass": 2 if eligible else 0,
        "oracle_reason": "dual_cost_accepted" if eligible else "not_attempted",
        "pass1_cpix": 10.0 if eligible else np.nan,
        "pass2_cpix": 9.0 if eligible else np.nan,
        "pass1_cpost": 20.0 if eligible else np.nan,
        "pass2_cpost": 18.0 if eligible else np.nan,
        "accepted_tracks_pass1": 10.0 if eligible else np.nan,
        "reduced_rows_pass1": 20.0 if eligible else np.nan,
        "pass1_global_nis": 4.0 if eligible else np.nan,
        "pass1_nis_per_row": 0.2 if eligible else np.nan,
        "pass1_max_gate_ratio": 0.8 if eligible else np.nan,
        "pass1_dx_norm": 0.05 if eligible else np.nan,
        "one_total": 0.010,
        "two_total": 0.018,
        "one_total_source": "aligned_max_one_timing",
        "two_total_source": "measured_shadow_timing",
        "source_commit": "a" * 40,
        "config_sha256": "b" * 64,
    }
    for index, feature in enumerate(study.SHADOW_ONLY_CAUSAL_CANDIDATES):
        row[feature] = 0.1 * (index + 1) if eligible else np.nan
    return row


def write_shadow_root(root, sequences=study.STABLE_TEN, embedded_provenance=True):
    for sequence_index, sequence in enumerate(sequences):
        directory = Path(root) / sequence
        directory.mkdir(parents=True)
        frame = pd.DataFrame([
            shadow_diagnostic_row(sequence, 1000.0 + sequence_index, False),
            shadow_diagnostic_row(sequence, 1000.05 + sequence_index, True),
        ])
        if not embedded_provenance:
            frame["shadow_skip_total"] = frame["one_total"]
            frame["shadow_trigger_total"] = frame["two_total"]
            frame = frame.drop(columns=list(study.SHADOW_REQUIRED_PROVENANCE_FIELDS))
            (directory / "source_commit.txt").write_text("%s\n" % ("a" * 40))
            (directory / "config_sha256.txt").write_text(
                "%s  estimator_config.yaml\n" % ("b" * 64)
            )
        frame.to_csv(directory / "diagnostics.csv", index=False)


class LabelTests(unittest.TestCase):
    def test_frozen_tolerance_and_secondary_labels(self):
        frame = pd.DataFrame({
            "pass2_valid": [True] * 5 + [False],
            "selected_pass": [2, 1, 1, 1, 1, 1],
            "pass1_cpix": [10.0] * 6,
            "pass1_cpost": [20.0] * 6,
            "pass2_cpix": [9.0, 10.0, 9.0, 10.0, 11.0, 9.0],
            "pass2_cpost": [18.0, 20.0, 20.0, 18.0, 20.0, 18.0],
        })
        labelled = study.compute_labels(frame)
        self.assertAlmostEqual(labelled.loc[0, "tau_pix"], 1.0e-8)
        self.assertAlmostEqual(labelled.loc[0, "tau_post"], 2.0e-8)
        self.assertEqual(labelled["STRICT_BENEFIT"].tolist(), [1, 0, 0, 0, 0, 0])
        self.assertEqual(labelled["EQUAL_WITHIN_TOLERANCE"].tolist(), [0, 1, 0, 0, 0, 0])
        self.assertEqual(labelled["PIXEL_ONLY_HELP"].tolist(), [0, 0, 1, 0, 0, 0])
        self.assertEqual(labelled["POSTERIOR_ONLY_HELP"].tolist(), [0, 0, 0, 1, 0, 0])
        self.assertEqual(labelled["HARMFUL"].tolist(), [0, 0, 0, 0, 1, 0])
        self.assertEqual(labelled["INVALID"].tolist(), [0, 0, 0, 0, 0, 1])
        self.assertAlmostEqual(labelled.loc[0, "relative_pixel_gain"], 0.1)
        self.assertAlmostEqual(labelled.loc[0, "relative_posterior_gain"], 0.1)
        self.assertAlmostEqual(labelled.loc[0, "rel_gain_pix"], 0.1)
        self.assertAlmostEqual(labelled.loc[0, "rel_gain_post"], 0.1)
        self.assertAlmostEqual(labelled.loc[0, "joint_gain"], 0.1)
        self.assertTrue(np.isnan(labelled.loc[5, "joint_gain"]))
        self.assertTrue(np.isnan(labelled.loc[5, "positive_joint_gain"]))

    def test_shadow_oracle_is_separate_from_live_selection(self):
        frame = pd.DataFrame({
            "pass2_valid": [True],
            "selected_pass": [1],
            "oracle_selected_pass": [2],
            "pass1_cpix": [10.0],
            "pass2_cpix": [9.0],
            "pass1_cpost": [20.0],
            "pass2_cpost": [18.0],
        })
        labelled = study.compute_labels(frame)
        self.assertEqual(labelled.loc[0, "selected_pass"], 1)
        self.assertEqual(labelled.loc[0, "ORACLE_SELECTED"], 1)


class LeakageTests(unittest.TestCase):
    def test_feature_whitelist_fails_closed(self):
        study.validate_causal_features(study.CAUSAL_FEATURES)
        self.assertLessEqual(len(study.CAUSAL_FEATURES), 8)
        with self.assertRaises(ValueError):
            study.validate_causal_features(list(study.CAUSAL_FEATURES) + ["pass2_cpix"])
        with self.assertRaises(ValueError):
            study.validate_causal_features(["sequence"])

    def test_random_baseline_is_prefix_stable(self):
        prefix = synthetic_grouped_frame(group_count=3, rows_per_group=10)
        suffix = synthetic_grouped_frame(group_count=2, rows_per_group=10).copy()
        suffix["sequence"] = "TAIL_" + suffix["sequence"]
        suffix["timestamp"] += 100000.0
        complete = pd.concat([prefix, suffix], ignore_index=True)
        prefix_trigger, prefix_score = study._rate_matched_random(prefix, 0.37, 77)
        full_trigger, full_score = study._rate_matched_random(complete, 0.37, 77)
        np.testing.assert_array_equal(prefix_trigger, full_trigger[:len(prefix)])
        np.testing.assert_array_equal(prefix_score, full_score[:len(prefix)])


class ShadowIngestionTests(unittest.TestCase):
    def test_strict_stable_ten_and_live_selection_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            write_shadow_root(directory)
            loaded = study.load_shadow_dataset(Path(directory))
            self.assertEqual(set(loaded["sequence"]), set(study.STABLE_TEN))
            self.assertTrue((loaded["selected_pass"] == 1).all())
            self.assertTrue((loaded["ORACLE_SELECTED"] == 1).all())
            self.assertEqual(len(loaded), len(study.STABLE_TEN))
            self.assertEqual(len(loaded.attrs["input_manifest"]), len(study.STABLE_TEN))
        with tempfile.TemporaryDirectory() as directory:
            write_shadow_root(directory, embedded_provenance=False)
            loaded = study.load_shadow_dataset(Path(directory))
            self.assertEqual(
                set(loaded["one_total_source"]),
                {"same_callback_shadow_total_minus_pass2_processing"},
            )

    def test_rejects_live_pass_two_and_incomplete_sequence_set(self):
        with tempfile.TemporaryDirectory() as directory:
            write_shadow_root(directory)
            path = Path(directory) / study.STABLE_TEN[0] / "diagnostics.csv"
            frame = pd.read_csv(path)
            frame.loc[frame["pass1_valid"].astype(bool), "selected_pass"] = 2
            frame.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "selected Pass 2"):
                study.load_shadow_dataset(Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            write_shadow_root(directory, study.STABLE_TEN[:-1])
            with self.assertRaisesRegex(ValueError, "stable ten"):
                study.load_shadow_dataset(Path(directory))


class LosoTests(unittest.TestCase):
    def test_group_exclusion_and_determinism(self):
        frame = synthetic_grouped_frame()
        first = study.run_loso(frame, seed=123, search_grid=study.TEST_SEARCH_GRID)
        second = study.run_loso(frame, seed=123, search_grid=study.TEST_SEARCH_GRID)
        pd.testing.assert_frame_equal(first["policy_results"], second["policy_results"])
        pd.testing.assert_frame_equal(first["loso_results"], second["loso_results"])
        pd.testing.assert_frame_equal(first["predictions"], second["predictions"])

        loso = first["loso_results"]
        for row in loso.itertuples(index=False):
            self.assertNotIn(row.outer_sequence, row.train_sequences.split("|"))
            self.assertEqual(row.outer_sequence, row.test_sequences)
            self.assertNotIn("pass2", row.spec_json.lower())
            self.assertNotIn("sequence", row.spec_json.lower())
        predictions = first["predictions"]
        counts = predictions.groupby(["policy", "dataset_row_id"]).size()
        self.assertTrue((counts == 1).all())
        self.assertEqual(set(predictions["outer_sequence"]), set(frame["sequence"]))

    def test_shadow_candidates_are_selected_inside_fit_and_capped(self):
        frame = synthetic_shadow_frame()
        selected = study._select_model_features(
            frame[frame["sequence"] != study.STABLE_TEN[-1]],
            study.SHADOW_CAUSAL_CANDIDATES,
        )
        self.assertLessEqual(len(selected), 8)
        self.assertTrue(set(selected).issubset(study.ALL_CAUSAL_FEATURES))
        results = study.run_loso(
            frame, seed=321, search_grid=study.TEST_SEARCH_GRID,
            causal_candidates=study.SHADOW_CAUSAL_CANDIDATES,
        )
        self.assertEqual(set(results["loso_results"]["outer_sequence"]), set(study.STABLE_TEN))
        self.assertTrue(set(results["policy_results"]["gate_result"]).issubset({
            "STRONG", "BORDERLINE", "NO_GO", "NOT_APPLICABLE",
        }))
        comparison = study.compare_existing_and_shadow(
            frame, frame, results["policy_results"], results["policy_results"]
        )
        self.assertIn("STRICT_BENEFIT_prevalence", set(comparison["metric"]))
        self.assertIn("trigger_rate", set(comparison["metric"]))

    def test_zero_trigger_cannot_outrank_borderline(self):
        zero = {
            "triggered_events": 0, "trigger_rate": 0.0, "recall": 0.0,
            "macro_recall": 0.0, "captured_positive_joint_gain_fraction": 0.0,
            "harmful_trigger_rate": np.nan,
            "p95_overhead_fraction_of_always_two": 0.0,
            "sequences_ge20_below_50pct_recall": 0, "f1": np.nan,
            "precision": np.nan,
        }
        useful = {
            "triggered_events": 10, "trigger_rate": 0.40, "recall": 0.65,
            "macro_recall": 0.61, "captured_positive_joint_gain_fraction": 0.66,
            "harmful_trigger_rate": 0.20,
            "p95_overhead_fraction_of_always_two": 0.60,
            "sequences_ge20_below_50pct_recall": 3, "f1": 0.5,
            "precision": 0.4,
        }
        zero["gate_result"] = study._gate_classification(zero)
        useful["gate_result"] = study._gate_classification(useful)
        self.assertEqual(zero["gate_result"], "NO_GO")
        self.assertEqual(useful["gate_result"], "BORDERLINE")
        self.assertGreater(
            study._selection_key(useful, 1), study._selection_key(zero, 0)
        )

    def test_no_go_tie_break_uses_balanced_unchanged_gate_attainment(self):
        always = {
            "triggered_events": 100, "trigger_rate": 1.0, "recall": 1.0,
            "macro_recall": 1.0, "captured_positive_joint_gain_fraction": 1.0,
            "harmful_trigger_rate": 0.55,
            "p95_overhead_fraction_of_always_two": 1.0,
            "sequences_ge20_below_50pct_recall": 0, "f1": 0.44,
            "precision": 0.28, "gate_result": "NO_GO",
        }
        balanced = {
            "triggered_events": 40, "trigger_rate": 0.40, "recall": 0.52,
            "macro_recall": 0.50, "captured_positive_joint_gain_fraction": 0.70,
            "harmful_trigger_rate": 0.60,
            "p95_overhead_fraction_of_always_two": 0.98,
            "sequences_ge20_below_50pct_recall": 4, "f1": 0.45,
            "precision": 0.40, "gate_result": "NO_GO",
        }
        self.assertGreater(
            study._selection_key(balanced, 1), study._selection_key(always, 0)
        )

    def test_recommendation_uses_frontier_before_complexity(self):
        rows = []
        for policy, recall, trigger in (
            ("P3_SINGLE_THRESHOLD", 0.76, 0.34),
            ("P4_TWO_RULE", 0.82, 0.30),
            ("P5_L1_LOGISTIC", 0.78, 0.32),
        ):
            rows.append({
                "policy": policy, "gate_result": "STRONG", "triggered_events": 10,
                "trigger_rate": trigger, "recall": recall, "macro_recall": recall,
                "captured_positive_joint_gain_fraction": recall,
                "harmful_trigger_rate": 0.05,
                "p95_overhead_fraction_of_always_two": 0.40,
                "sequences_ge20_below_50pct_recall": 0,
            })
        recommendation = study._select_recommendation(pd.DataFrame(rows))
        self.assertEqual(recommendation["selected_policy"], "P4_TWO_RULE")
        self.assertEqual(recommendation["pareto_frontier"], ["P4_TWO_RULE"])
        self.assertTrue(recommendation["passed_stable_ten_gate"])

    def test_no_go_freezes_least_complex_pareto_candidate_for_stress(self):
        rows = []
        for policy, recall, trigger, gain in (
            ("P3_SINGLE_THRESHOLD", 0.30, 0.20, 0.25),
            ("P4_TWO_RULE", 0.50, 0.40, 0.50),
            ("P5_L1_LOGISTIC", 0.55, 0.50, 0.58),
        ):
            rows.append({
                "policy": policy, "gate_result": "NO_GO", "triggered_events": 10,
                "trigger_rate": trigger, "recall": recall, "macro_recall": recall,
                "captured_positive_joint_gain_fraction": gain,
                "harmful_trigger_rate": 0.50,
                "p95_overhead_fraction_of_always_two": 0.90,
                "sequences_ge20_below_50pct_recall": 5,
            })
        recommendation = study._select_recommendation(pd.DataFrame(rows))
        self.assertEqual(recommendation["classification"], "NO_GO")
        self.assertFalse(recommendation["passed_stable_ten_gate"])
        self.assertEqual(recommendation["selected_policy"], "P3_SINGLE_THRESHOLD")
        self.assertIn("P3_SINGLE_THRESHOLD", recommendation["pareto_frontier"])

    def test_materialized_policy_round_trip_and_complete_schema(self):
        frame = synthetic_grouped_frame()
        recommendation = {
            "classification": "BORDERLINE",
            "selected_policy": "P5_L1_LOGISTIC",
            "pareto_frontier": ["P5_L1_LOGISTIC"],
        }
        spec = study.materialize_policy(
            frame, recommendation, 456, search_grid=study.TEST_SEARCH_GRID
        )
        self.assertLessEqual(len(spec["features"]), 8)
        self.assertEqual(len(spec["features"]), len(spec["coefficients"]))
        trigger, _ = study.predict_materialized_policy(spec, frame)
        self.assertEqual(len(trigger), len(frame))
        tree_recommendation = dict(recommendation)
        tree_recommendation["selected_policy"] = "P6_DEPTH3_TREE"
        tree_recommendation["pareto_frontier"] = ["P6_DEPTH3_TREE"]
        tree_spec = study.materialize_policy(
            frame, tree_recommendation, 457, search_grid=study.TEST_SEARCH_GRID
        )
        self.assertLessEqual(len(tree_spec["features"]), 8)
        self.assertEqual(len(tree_spec["children_left"]), len(tree_spec["node_class_weights"]))
        tree_trigger, _ = study.predict_materialized_policy(tree_spec, frame)
        self.assertEqual(len(tree_trigger), len(frame))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "DATASET_SCHEMA.md"
            study.write_dataset_schema(
                path, frame,
                {"POLICY_RESULTS.csv": pd.DataFrame({"trigger_rate": [0.2]})},
            )
            text = path.read_text()
            self.assertIn("## `POLICY_RESULTS.csv`", text)
            self.assertIn("`trigger_rate`", text)


if __name__ == "__main__":
    unittest.main()
