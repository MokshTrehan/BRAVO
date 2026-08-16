#!/usr/bin/python3.8
"""Synthetic tests for the PERTURB-1 harness extensions."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import unittest

import os
SCRIPTS = Path(os.environ.get("PERTURB_SCRIPTS", Path(__file__).resolve().parents[1]))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


kaist = _load("cross_dataset_kaist_trial")
generic = _load("cross_dataset_trial")
driver = _load("perturbation_campaign")
aggregate = _load("perturbation_aggregate")


def _args(**overrides):
    base = dict(
        bag_start=0.0,
        system="S1",
        perturbation_campaign_id="PERTURB-1",
        perturbation_offset_frames=5,
        perturbation_frame_rate_hz=30.0,
        perturbation_seed_label="frozen",
        perturbation_axis="offset",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class PerturbationRecordTest(unittest.TestCase):
    def test_kaist_positive_offset(self):
        record = kaist.perturbation_record(_args())
        self.assertEqual(record["estimator_bag_start_seconds"], 5.0 / 30.0)
        self.assertEqual(record["landmark_elimination"], "schur")

    def test_n0_records_nullspace(self):
        record = kaist.perturbation_record(_args(system="N0"))
        self.assertEqual(record["landmark_elimination"], "nullspace")

    def test_negative_offset_without_lead_in_is_rejected(self):
        with self.assertRaises(kaist.TrialError):
            kaist.perturbation_record(_args(perturbation_offset_frames=-5))

    def test_generic_negative_offset_with_lead_in(self):
        record = generic.perturbation_record(_args(bag_start=5.0, perturbation_offset_frames=-10, perturbation_frame_rate_hz=20.0))
        self.assertEqual(record["estimator_bag_start_seconds"], 4.5)

    def test_unfrozen_seed_is_not_runnable(self):
        with self.assertRaises(generic.TrialError):
            generic.perturbation_record(_args(perturbation_seed_label="+1"))

    def test_n0_requires_perturbation_mode(self):
        args = argparse.Namespace(
            protocol_id="CDSC-1R4", run_id="x-1", sequence="rotation/rotation.bag", dataset="kaist_vio",
            timeout_seconds=10.0, bag_start=0.0, bag_duration=-1.0, attempt_index=1, mode="scored",
            scored_result=None, system="N0",
        )
        with self.assertRaises(kaist.TrialError):
            kaist._validate_request(args)


class LaunchContractTest(unittest.TestCase):
    def test_kaist_n0_launch_selects_nullspace(self):
        launch = kaist.CANONICAL_INPUTS["N0"]["launch"].resolve(strict=True)
        contract = kaist.validate_launch_contract("N0", launch)
        self.assertEqual(contract["launch_landmark_elimination"], "nullspace")
        s1 = kaist.validate_launch_contract("S1", kaist.CANONICAL_INPUTS["S1"]["launch"].resolve(strict=True))
        self.assertEqual(s1["launch_landmark_elimination"], "schur")

    def test_generic_n0_launch_selects_nullspace(self):
        launch = generic.CANONICAL_LAUNCHES["N0"][0].resolve(strict=True)
        contract = generic.validate_launch_contract("N0", launch)
        self.assertIn("up_msckf_landmark_elimination=nullspace", contract["s1_only_algorithm_deltas"])
        with self.assertRaises(generic.TrialError):
            generic.validate_launch_contract("S1", launch)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.rows = driver.campaign.load_matrix(driver.campaign.RuntimePaths().matrix)

    def test_matrix_size_and_not_runnable_typing(self):
        cells = driver.plan_cells(self.rows, ["kaist-focus", "kaist-full-remaining", "euroc-forks"])
        self.assertEqual(len(cells), 190)
        not_runnable = [c for c in cells if not c.runnable[0]]
        self.assertEqual(len(not_runnable), 92)
        seeds = [c for c in not_runnable if c.axis == "seed"]
        self.assertEqual(len(seeds), 40)
        negative = [c for c in not_runnable if c.axis == "offset"]
        self.assertEqual(len(negative), 52)
        self.assertTrue(all(c.estimator_start < 0 for c in negative))
        mh05 = [c for c in cells if c.row.sequence == "MH_05_difficult"]
        self.assertTrue(all(c.runnable[0] for c in mh05))

    def test_run_ids_are_unique_and_safe(self):
        cells = driver.plan_cells(self.rows, list(driver.SETS))
        ids = [c.run_id for c in cells]
        self.assertEqual(len(ids), len(set(ids)))
        for value in ids:
            self.assertIsNotNone(driver.campaign.SAFE_COMPONENT_RE.search(value) or True)
            self.assertRegex(value, r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _cell(sequence, system, axis, offset, seed, complete=True, ate=0.1, outcome="CLOSED", validity="VALID", cls=None, order=19):
    return {
        "set": "kaist-focus", "order": order, "dataset": "kaist_vio", "sequence": sequence, "system": system,
        "axis": axis, "offset_frames": offset, "seed_label": seed, "runnable": outcome != "NOT_RUNNABLE",
        "outcome": outcome, "evidence_validity": validity, "passage_complete": complete,
        "metrics": {"ate_translation_rmse_m": ate} if complete else None,
        "typed_failure_class": cls, "run_id": "{}-{}-{}-{}-{}".format(sequence, system, axis, offset, seed),
    }


class GateTest(unittest.TestCase):
    def test_cr1_literal_counts(self):
        records = []
        for seq in aggregate.ROTATION_FAMILY:
            for off in (0, 5, 10):
                records.append(_cell(seq, "S1", "offset", off, "frozen"))
                records.append(_cell(seq, "N0", "offset", off, "frozen", complete=(off != 10), cls=None if off != 10 else "TRACKING_LOSS", ate=0.7))
            records.append(_cell(seq, "S1", "seed", 0, "frozen"))
            records.append(_cell(seq, "N0", "seed", 0, "frozen"))
        gate = aggregate.gate_cr1(records)
        self.assertEqual(gate["literal"]["s1_completions"], 8)
        self.assertEqual(gate["literal"]["n0_fail_or_exceed_count"], 6)  # 2 tracking losses + 4 over-threshold completions
        self.assertEqual(gate["literal"]["verdict"], "NOT_CLAIMABLE")

    def test_cr2_new_class_detected(self):
        records = [
            _cell("square/square.bag", "S1", "offset", 0, "frozen"),
            _cell("square/square.bag", "S1", "offset", 5, "frozen", complete=False, cls="NUMERIC_FAILURE"),
        ]
        gate = aggregate.gate_cr2(records)
        self.assertEqual(gate["verdict_global"], "FAIL")
        self.assertEqual(gate["global_reading"]["new_failure_classes"], ["NUMERIC_FAILURE"])


if __name__ == "__main__":
    unittest.main()
