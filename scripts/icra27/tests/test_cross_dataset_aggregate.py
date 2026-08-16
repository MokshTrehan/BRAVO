#!/usr/bin/python3.8

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "cross_dataset_aggregate.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_aggregate", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
aggregate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aggregate)


def passage_cells(completions, invalid=None):
    datasets = ("euroc_mav", "tum_vi", "kaist_vio")
    cells = []
    for order, dataset in enumerate(datasets, 1):
        for system in aggregate.SYSTEMS:
            cells.append(
                {
                    "order": order,
                    "dataset": dataset,
                    "system": system,
                    "evidence_validity": (
                        "INVALID_INFRA" if invalid == (order, system) else "VALID"
                    ),
                    "passage_complete": bool(completions[order - 1][system]),
                }
            )
    return cells


def ratio(value):
    if value is None:
        return {
            "status": "UNASSESSABLE_ZERO_U0_DENOMINATOR",
            "mathematical_ratio_defined": False,
            "value": None,
        }
    return {
        "status": "DEFINED_FINITE",
        "mathematical_ratio_defined": True,
        "value": value,
    }


def accuracy_pairs(values_by_metric):
    count = max(len(values) for values in values_by_metric.values())
    return [
        {
            "status": "COMPLETE",
            "ratios": {
                metric: ratio(values[index])
                for metric, values in values_by_metric.items()
                if index < len(values)
            },
        }
        for index in range(count)
    ]


class PassageTests(unittest.TestCase):
    def test_parity(self):
        cells = passage_cells([{"U0": True, "S1": True}] * 3)
        self.assertEqual(
            aggregate.mechanical_passage_summary(cells, 3)["label"],
            "PASSAGE_PARITY",
        )

    def test_dominant(self):
        cells = passage_cells(
            [
                {"U0": False, "S1": True},
                {"U0": True, "S1": True},
                {"U0": True, "S1": True},
            ]
        )
        self.assertEqual(
            aggregate.mechanical_passage_summary(cells, 3)["label"],
            "PASSAGE_DOMINANT",
        )

    def test_regression_has_priority(self):
        cells = passage_cells(
            [
                {"U0": True, "S1": False},
                {"U0": False, "S1": True},
                {"U0": True, "S1": True},
            ]
        )
        self.assertEqual(
            aggregate.mechanical_passage_summary(cells, 3)["label"],
            "PASSAGE_REGRESSION",
        )

    def test_unassessable_on_invalid_evidence(self):
        cells = passage_cells([{"U0": True, "S1": True}] * 3, invalid=(2, "S1"))
        self.assertEqual(
            aggregate.mechanical_passage_summary(cells, 3)["label"],
            "PASSAGE_UNASSESSABLE",
        )

    def test_unassessable_on_missing_cell(self):
        cells = passage_cells([{"U0": True, "S1": True}] * 3)[:-1]
        self.assertEqual(
            aggregate.mechanical_passage_summary(cells, 3)["label"],
            "PASSAGE_UNASSESSABLE",
        )

    def test_mixed_fallback_in_ordered_decision(self):
        counts = {
            "euroc_mav": {"U0": 1, "S1": 2},
            "tum_vi": {"U0": 2, "S1": 1},
            "kaist_vio": {"U0": 1, "S1": 1},
        }
        self.assertEqual(
            aggregate._passage_label_from_facts(
                True, False, counts, {"U0": 4, "S1": 4}
            ),
            "PASSAGE_MIXED",
        )


class AccuracyTests(unittest.TestCase):
    def test_exact_median_and_individual_boundaries_pass(self):
        values = [0.0, 0.0, 0.0, 0.0, 0.10, 0.10, 0.10, 0.20]
        pairs = accuracy_pairs({metric: values for metric in aggregate.METRICS})
        result = aggregate.accuracy_family_summary(pairs)
        self.assertEqual(result["label"], "ACCURACY_NONINFERIOR")
        self.assertEqual(result["complete_pair_result_count"], 8)

    def test_median_above_boundary_fails(self):
        values = [0.100001] * 8
        result = aggregate.accuracy_family_summary(
            accuracy_pairs({metric: values for metric in aggregate.METRICS})
        )
        self.assertEqual(result["label"], "ACCURACY_GUARD_FAIL")

    def test_one_individual_above_boundary_fails(self):
        values = [0.0] * 7 + [0.200001]
        result = aggregate.accuracy_family_summary(
            accuracy_pairs({metric: values for metric in aggregate.METRICS})
        )
        self.assertEqual(result["label"], "ACCURACY_GUARD_FAIL")

    def test_seven_complete_pairs_are_unassessable(self):
        values = [0.0] * 7
        result = aggregate.accuracy_family_summary(
            accuracy_pairs({metric: values for metric in aggregate.METRICS})
        )
        self.assertEqual(result["label"], "ACCURACY_UNASSESSABLE")

    def test_zero_or_missing_ratio_can_make_family_unassessable(self):
        values = [0.0] * 8
        by_metric = {metric: list(values) for metric in aggregate.METRICS}
        by_metric[aggregate.METRICS[0]][-1] = None
        result = aggregate.accuracy_family_summary(accuracy_pairs(by_metric))
        self.assertEqual(result["label"], "ACCURACY_UNASSESSABLE")
        self.assertEqual(
            result["metrics"][aggregate.METRICS[0]]["defined_ratio_count"], 7
        )

    def test_unassessable_pair_result_is_excluded(self):
        values = [0.0] * 8
        pairs = accuracy_pairs({metric: values for metric in aggregate.METRICS})
        pairs[0] = {"status": "UNASSESSABLE", "ratios": {}}
        result = aggregate.accuracy_family_summary(pairs)
        self.assertEqual(result["label"], "ACCURACY_UNASSESSABLE")
        self.assertEqual(result["complete_pair_result_count"], 7)


class PairQuaternionProjectionTests(unittest.TestCase):
    @staticmethod
    def record():
        member = {
            "policy": "q_over_l2_norm_for_evo_objects_only",
            "maximum_allowed_abs_norm_error": 5.0e-4,
            "source_row_count": 100,
            "rows_projected": 100,
            "minimum_source_norm": 0.9998,
            "maximum_source_norm": 1.0003,
            "maximum_abs_source_norm_error": 0.0003,
            "source_bytes_unchanged": True,
            "source_tokens_unchanged": True,
            "row_ids_from_raw_source_bytes": True,
        }
        return {name: dict(member) for name in ("ground_truth", "U0", "S1")}

    def test_exact_projection_contract_passes(self):
        record = self.record()
        self.assertIs(
            aggregate._validate_quaternion_projection(
                record, {"bounded_quaternion_projection_for_evo_only": True}
            ),
            record,
        )

    def test_missing_or_over_bound_projection_fails_closed(self):
        with self.assertRaises(aggregate.AggregateError):
            aggregate._validate_quaternion_projection(
                None, {"bounded_quaternion_projection_for_evo_only": True}
            )
        record = self.record()
        record["ground_truth"]["maximum_source_norm"] = 1.0006
        record["ground_truth"]["maximum_abs_source_norm_error"] = 0.0006
        with self.assertRaises(aggregate.AggregateError):
            aggregate._validate_quaternion_projection(
                record, {"bounded_quaternion_projection_for_evo_only": True}
            )


class ClosureAndAccountingTests(unittest.TestCase):
    @staticmethod
    def write_closed(root: Path) -> Path:
        manifest = root / "aggregate.json"
        payload = b'{"status":"COMPLETE"}\n'
        manifest.write_bytes(payload)
        other = root / "data.csv"
        other.write_bytes(b"a,b\n1,2\n")
        lines = []
        for path in (manifest, other):
            lines.append(
                "{}  {}\n".format(
                    hashlib.sha256(path.read_bytes()).hexdigest(), path.name
                )
            )
        (root / "SHA256SUMS").write_text("".join(lines), encoding="ascii")
        return manifest

    def test_exact_checksum_closure_and_tamper_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_closed(root)
            self.assertTrue(
                aggregate.verify_checksum_directory(root, manifest)["membership_exact"]
            )
            (root / "data.csv").write_text("tampered\n", encoding="utf-8")
            with self.assertRaises(aggregate.AggregateError):
                aggregate.verify_checksum_directory(root, manifest)

    def test_duplicate_checksum_entry_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_closed(root)
            checksum = root / "SHA256SUMS"
            first = checksum.read_text(encoding="ascii").splitlines()[0]
            checksum.write_text(first + "\n" + first + "\n", encoding="ascii")
            with self.assertRaises(aggregate.AggregateError):
                aggregate.verify_checksum_directory(root, manifest)

    def test_unexpected_unchecksummed_file_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = self.write_closed(root)
            (root / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaises(aggregate.AggregateError):
                aggregate.verify_checksum_directory(root, manifest)

    def test_missing_and_duplicate_canonical_results_rejected(self):
        expected = [Path("/tmp/a"), Path("/tmp/b")]
        aggregate.assert_exact_paths(expected, expected, "fixture")
        with self.assertRaises(aggregate.AggregateError):
            aggregate.assert_exact_paths(expected[:1], expected, "fixture")
        with self.assertRaises(aggregate.AggregateError):
            aggregate.assert_exact_paths(expected + [Path("/tmp/c")], expected, "fixture")
        with self.assertRaises(aggregate.AggregateError):
            aggregate.assert_exact_paths([expected[0], expected[0]], expected, "fixture")

    def test_all_100_cells_accounted(self):
        cells = [
            {"lane": lane, "order": order, "system": system}
            for lane in aggregate.LANES
            for order in range(1, 26)
            for system in aggregate.SYSTEMS
        ]
        result = aggregate.validate_accounting(cells)
        self.assertTrue(result["exact"])
        self.assertEqual(result["all_cells_observed"], 100)
        with self.assertRaises(aggregate.AggregateError):
            aggregate.validate_accounting(cells[:-1])
        with self.assertRaises(aggregate.AggregateError):
            aggregate.validate_accounting(cells[:-1] + [cells[0]])


class ReportTests(unittest.TestCase):
    def test_claim_boundary_late_init_sparse_map_and_review_wording(self):
        report = aggregate.render_report(
            {
                "passage": {
                    "label": "PASSAGE_PARITY",
                    "overall_counts": {"U0": 25, "S1": 25},
                    "dataset_counts": {
                        "euroc_mav": {"U0": 11, "S1": 11},
                        "tum_vi": {"U0": 3, "S1": 3},
                        "kaist_vio": {"U0": 11, "S1": 11},
                    },
                    "kaist_families": {
                        "rotation": {"U0": 2, "S1": 2},
                        "circle": {"U0": 3, "S1": 3},
                        "infinite": {"U0": 3, "S1": 3},
                        "square": {"U0": 3, "S1": 3},
                    },
                },
                "accuracy": {
                    "families": {
                        "euroc_mav": {"label": "ACCURACY_NONINFERIOR"},
                        "kaist_vio": {"label": "ACCURACY_NONINFERIOR"},
                    }
                },
                "qualitative": {
                    "flagged_manifest_count": 2,
                    "label_counts": {
                        "QUALITATIVE_PASS": 0,
                        "QUALITATIVE_FLAGGED": 2,
                        "QUALITATIVE_UNASSESSABLE": 48,
                    }
                },
                "mechanism": {"status": "PASS"},
            }
        )
        self.assertIn("whole-system comparison", report)
        self.assertIn("cannot attribute a difference to Schur", report)
        self.assertIn("Late initialization is descriptive only", report)
        self.assertIn("not a persistent or dense map", report)
        self.assertIn("does not automatically assert a human visual pass", report)
        self.assertIn("corridor4 and outdoors4", report)


class MechanismBoundaryTests(unittest.TestCase):
    def test_early_failure_binds_declaration_without_opening_gt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scored = {}
            for system in aggregate.SYSTEMS:
                run = root / system
                run.mkdir()
                result_path = run / "sequence_result.json"
                result_path.write_text("{}\n", encoding="utf-8")
                checksum = run / "SHA256SUMS"
                checksum.write_text("fixture\n", encoding="ascii")
                scored[system] = aggregate.campaign.ValidatedResult(
                    path=result_path,
                    value={"artifacts": {}},
                    identity=aggregate.file_identity(result_path),
                    classification="retained_algorithm_outcome",
                )
            matrix_path = root / "matrix.yaml"
            matrix_path.write_text("schema: fixture\n", encoding="utf-8")
            matrix_identity = aggregate.file_identity(matrix_path)
            missing_gt = root / "must-not-be-opened.tum"
            row = aggregate.campaign.MatrixRow(
                order=19,
                dataset="kaist_vio",
                sequence="rotation/rotation.bag",
                system_order=("U0", "S1"),
                bag_start_seconds=0.0,
                bag={"path": str(matrix_path), "bytes": matrix_path.stat().st_size, "sha256": matrix_identity["sha256"]},
                ground_truth={
                    "canonical_path": str(missing_gt),
                    "bytes": 123,
                    "sha256": "a" * 64,
                    "capability": "full_trajectory",
                    "format": "tum",
                },
            )
            source_runs = {}
            postflight = {"matrix": matrix_identity}
            for system in aggregate.SYSTEMS:
                checksum_identity = aggregate.file_identity(scored[system].path.parent / "SHA256SUMS")
                source_runs[system] = {
                    "result": scored[system].identity,
                    "closure": {
                        "checksum_closure": {
                            "status": "PASS",
                            "identity": checksum_identity,
                            "all_run_files_closed": True,
                        }
                    },
                }
                postflight[system + "_result"] = scored[system].identity
                postflight[system + "_checksums"] = checksum_identity
            declaration = {
                "canonical_path": str(missing_gt),
                "size_bytes": 123,
                "sha256": "a" * 64,
                "capability": "full_trajectory",
                "format": "tum",
                "source": "FROZEN_MATRIX_DECLARATION_NOT_LIVE_FILE",
                "live_file_opened": False,
            }
            value = {
                "schema": aggregate.MECHANISM_SCHEMA,
                "status": "C2_VALIDATION_FAILURE",
                "pass": False,
                "protocol_id": aggregate.PROTOCOL_ID,
                "dataset": "kaist_vio",
                "sequence": "rotation/rotation.bag",
                "source_runs": source_runs,
                "both_scored_estimator_groups_closed_before_ground_truth_open": True,
                "ground_truth_opened": False,
                "matrix_binding": {
                    "matrix": matrix_identity,
                    "row_order": 19,
                    "ground_truth_expected_from_matrix": declaration,
                    "ground_truth": None,
                    "ground_truth_opened": False,
                },
                "c2_prerequisite": {"pass": False},
                "retained_s1_evidence": {},
                "rotation_gap": None,
                "s1_single_population_metrics": None,
                "rotation_gap_gate": None,
                "numeric_target_acceptance": None,
                "c2_validation_failure": {
                    "algorithm_failure_retained": True,
                    "infrastructure_failure": False,
                },
                "accuracy_eligibility_unchanged_by_c2_target_thresholds": True,
                "c2_validation_is_a_separate_robustness_mechanism_result": True,
                "postflight_identities": postflight,
            }
            mechanism = root / "mechanism.json"
            mechanism.write_text(json.dumps(value), encoding="utf-8")
            result = aggregate._validate_mechanism(
                mechanism, row, scored, matrix_identity
            )
            self.assertEqual(result["status"], "C2_VALIDATION_FAILURE")
            self.assertFalse(missing_gt.exists())


class CaptureLinkageTests(unittest.TestCase):
    def test_closed_truthful_output_mismatch_is_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scored_result_path = root / "scored.json"
            scored_result_path.write_text("{}", encoding="utf-8")
            scored_artifacts = {}
            capture_artifacts = {}
            comparisons = {}
            for name in ("state", "deviation", "tum"):
                scored_path = root / ("scored-" + name)
                capture_path = root / ("capture-" + name)
                scored_path.write_bytes((name + "-same").encode("ascii"))
                capture_path.write_bytes(
                    ((name + "-different") if name == "tum" else (name + "-same")).encode("ascii")
                )
                expected = aggregate.file_identity(scored_path)
                observed = aggregate.file_identity(capture_path)
                scored_artifacts[name] = {"identity": expected}
                capture_artifacts[name] = {"identity": observed}
                exact = expected["size_bytes"] == observed["size_bytes"] and expected["sha256"] == observed["sha256"]
                comparisons[name] = {
                    "expected_sha256": expected["sha256"],
                    "observed_sha256": observed["sha256"],
                    "byte_exact": exact,
                }
            scored = aggregate.campaign.ValidatedResult(
                path=scored_result_path,
                value={"artifacts": scored_artifacts},
                identity=aggregate.file_identity(scored_result_path),
                classification="retained_algorithm_outcome",
            )
            capture = {
                "status": "INVALID_LINKAGE",
                "evidence_validity": "CAPTURE_LINK_INVALID",
                "artifacts": capture_artifacts,
                "outcome_facts": {
                    "capture_closed": True,
                    "linkage_valid": False,
                    "teardown_ok": True,
                    "runtime_contract_valid": True,
                },
                "checks": {"runtime_inputs_unchanged": True},
                "scored_linkage": {
                    "sequence_result": scored.identity,
                    "sequence_result_after": scored.identity,
                    "source_unchanged_during_capture": True,
                    "status": "OUTPUT_MISMATCH",
                    "byte_exact": False,
                    "artifact_comparisons": comparisons,
                },
            }
            retained = aggregate._validate_capture_linkage(capture, scored)
            self.assertEqual(retained["status"], "OUTPUT_MISMATCH")
            capture["status"] = "NO_INITIALIZATION"
            self.assertEqual(
                aggregate._validate_capture_linkage(capture, scored)["status"],
                "OUTPUT_MISMATCH",
            )
            capture["scored_linkage"]["artifact_comparisons"]["tum"]["byte_exact"] = True
            with self.assertRaises(aggregate.AggregateError):
                aggregate._validate_capture_linkage(capture, scored)


if __name__ == "__main__":
    unittest.main()
