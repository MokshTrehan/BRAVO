#!/usr/bin/python3.8
"""Aggregate a PERTURB-1 artifact tree (docs/icra27/PERTURBATION_PREREG.md).

Read-only consumer of the driver's cell records.  It revalidates every closed
run's checksum closure and every driver record's checksum set, then publishes
per-cell rows, per-sequence-per-system summaries (completion count over
denominator, ATE median + IQR), the prospective KAIST usability threshold with
the prereg disclosure line verbatim, and CR-1/CR-2/CR-3 evaluated mechanically
with the counts that drove each verdict.  Where the prereg is silent
(DECISIONS.md), gates are computed under both readings and both are reported.
Statistics language is counts and distributions only.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_campaign as campaign  # noqa: E402
import perturbation_campaign as driver  # noqa: E402

SCHEMA = "schurvio.icra27.perturbation.aggregate.v1"
CAMPAIGN_ID = "PERTURB-1"
USABILITY_ATE_M = 0.5
DISCLOSURE = (
    "**Disclosure:** threshold set after CDSC-1R4 (n=1) was seen and before this campaign; it is "
    "applied prospectively here and to all subsequent campaigns, and CDSC-1R4 numbers are reported "
    "against it descriptively with this timing disclosed."
)
ROTATION_FAMILY = ("rotation/rotation.bag", "rotation/rotation_fast.bag")
CR3_SEQUENCES = ("square/square_fast.bag", "circle/circle_fast.bag", "square/square_head.bag")
CDSC_PAIR_METRICS = driver.CDSC1R4_ROOT / "final-report" / "pair_metrics.csv"


class AggregateError(RuntimeError):
    pass


def _load_records(root: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cells_root = root / "cells"
    if not cells_root.is_dir():
        return records
    for path in sorted(cells_root.glob("*/*/*/*.driver/cell_record.json")):
        value, identity = campaign.load_json(path, "cell record")
        # Revalidate the driver record's checksum set and, when closed, the run's.
        sums = path.parent / "SHA256SUMS"
        if not sums.is_file():
            raise AggregateError("driver record lacks SHA256SUMS: {}".format(path.parent))
        recorded = driver._read_sums(sums)
        for relative, digest in recorded.items():
            live = campaign.sha256_file(path.parent / relative)
            if live != digest:
                raise AggregateError("driver artifact checksum mismatch: {}/{}".format(path.parent, relative))
        value = dict(value)
        value["cell_record_sha256"] = identity.get("sha256")
        value["driver_directory"] = str(path.parent)
        if value.get("outcome") == "CLOSED":
            run_directory = Path(str(value.get("run_directory")))
            result_path = run_directory / "sequence_result.json"
            campaign._validate_checksum_set(run_directory, result_path)
            live_identity = campaign.file_identity(result_path)
            if live_identity["sha256"] != value.get("sequence_result_sha256"):
                raise AggregateError("sequence result bytes changed after the driver record: {}".format(run_directory))
            value["run_checksums_revalidated"] = True
        records.append(value)
    return records


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _ate(record: Mapping[str, Any]) -> Optional[float]:
    metrics = record.get("metrics") or {}
    return _finite(metrics.get("ate_translation_rmse_m"))


def _is_kaist(record: Mapping[str, Any]) -> bool:
    return record.get("dataset") == "kaist_vio"


def _usable(record: Mapping[str, Any]) -> Optional[bool]:
    """KAIST usable completion := passage complete AND ATE <= 0.5 m (None if not assessable)."""

    if not _is_kaist(record) or record.get("outcome") != "CLOSED":
        return None
    if not record.get("passage_complete"):
        return False
    ate = _ate(record)
    if ate is None:
        return None
    return ate <= USABILITY_ATE_M


def _distribution(values: Sequence[float]) -> Dict[str, Any]:
    clean = sorted(float(v) for v in values)
    if not clean:
        return {"count": 0, "median": None, "q1": None, "q3": None, "iqr": None, "min": None, "max": None, "values": []}
    if len(clean) >= 2:
        q1, _, q3 = statistics.quantiles(clean, n=4, method="inclusive")
    else:
        q1 = q3 = clean[0]
    return {
        "count": len(clean),
        "median": statistics.median(clean),
        "q1": q1,
        "q3": q3,
        "iqr": q3 - q1,
        "quantile_method": "statistics.quantiles(n=4, method=inclusive)",
        "min": clean[0],
        "max": clean[-1],
        "values": clean,
    }


def _matrix_records(records: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [r for r in records if r.get("set") != "integrity"]


def per_sequence_system(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str, str], List[Mapping[str, Any]]] = {}
    for record in _matrix_records(records):
        key = (str(record["dataset"]), str(record["sequence"]), str(record["system"]))
        groups.setdefault(key, []).append(record)
    rows = []
    for (dataset, sequence, system), items in sorted(groups.items(), key=lambda kv: (kv[1][0]["order"], kv[0][2])):
        closed = [r for r in items if r.get("outcome") == "CLOSED"]
        valid = [r for r in closed if r.get("evidence_validity") == "VALID"]
        completions = [r for r in valid if r.get("passage_complete")]
        ates = [a for a in (_ate(r) for r in completions) if a is not None]
        usable = [r for r in completions if _usable(r) is True]
        rows.append(
            {
                "order": items[0]["order"],
                "dataset": dataset,
                "sequence": sequence,
                "system": system,
                "planned_cells": len(items),
                "not_runnable_cells": sum(1 for r in items if r.get("outcome") == "NOT_RUNNABLE"),
                "executed_cells": len(closed),
                "evidence_valid_cells": len(valid),
                "evidence_invalid_or_fatal_cells": len(closed) - len(valid),
                "unclosed_or_no_result_cells": sum(
                    1 for r in items if r.get("outcome") not in ("CLOSED", "NOT_RUNNABLE")
                ),
                "completion_count": len(completions),
                "denominator_planned": len(items),
                "denominator_executed": len(closed),
                "completion_over_planned": "{}/{}".format(len(completions), len(items)),
                "completion_over_executed": "{}/{}".format(len(completions), len(closed)),
                "usable_completion_count": len(usable) if dataset == "kaist_vio" else None,
                "typed_failure_classes": sorted(
                    {str(r.get("typed_failure_class")) for r in valid if r.get("typed_failure_class")}
                ),
                "ate_translation_rmse_m": _distribution(ates),
                "rpe_translation_rmse_1m_m": _distribution(
                    [v for v in (_finite((r.get("metrics") or {}).get("rpe_translation_rmse_1m_m")) for r in completions) if v is not None]
                ),
                "rpe_rotation_rmse_1m_deg": _distribution(
                    [v for v in (_finite((r.get("metrics") or {}).get("rpe_rotation_rmse_1m_deg")) for r in completions) if v is not None]
                ),
            }
        )
    return rows


def _rotation_family_cells(records: Sequence[Mapping[str, Any]], system: str) -> List[Mapping[str, Any]]:
    return [
        r
        for r in _matrix_records(records)
        if r.get("system") == system and r.get("sequence") in ROTATION_FAMILY
    ]


def gate_cr1(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    s1 = _rotation_family_cells(records, "S1")
    n0 = _rotation_family_cells(records, "N0")
    prereg_denominator = 20

    def is_completion(r: Mapping[str, Any]) -> bool:
        return r.get("outcome") == "CLOSED" and r.get("evidence_validity") == "VALID" and bool(r.get("passage_complete"))

    def n0_fails_or_exceeds(r: Mapping[str, Any]) -> Optional[bool]:
        if r.get("outcome") != "CLOSED":
            return None
        if r.get("evidence_validity") != "VALID":
            return None
        if not r.get("passage_complete"):
            return True
        ate = _ate(r)
        if ate is None:
            return None  # completed but no computable ATE: not counted as exceeding
        return ate > USABILITY_ATE_M

    s1_completions = sum(1 for r in s1 if is_completion(r))
    s1_runnable = [r for r in s1 if r.get("runnable")]
    n0_runnable = [r for r in n0 if r.get("runnable")]
    n0_flags = [n0_fails_or_exceeds(r) for r in n0]
    n0_fail_count = sum(1 for f in n0_flags if f is True)
    n0_unassessable = sum(1 for f in n0_flags if f is None and True)
    n0_failures_typed = [
        {
            "run_id": r.get("run_id"),
            "sequence": r.get("sequence"),
            "axis": r.get("axis"),
            "offset_frames": r.get("offset_frames"),
            "seed_label": r.get("seed_label"),
            "status": r.get("status"),
            "typed_failure_class": r.get("typed_failure_class"),
            "ate_translation_rmse_m": _ate(r),
            "exceeds_usability_threshold": (_ate(r) is not None and _ate(r) > USABILITY_ATE_M),
        }
        for r in n0
        if n0_fails_or_exceeds(r) is True
    ]
    s1_noncompletions_typed = [
        {
            "run_id": r.get("run_id"),
            "sequence": r.get("sequence"),
            "axis": r.get("axis"),
            "offset_frames": r.get("offset_frames"),
            "seed_label": r.get("seed_label"),
            "outcome": r.get("outcome"),
            "status": r.get("status"),
            "typed_failure_class": r.get("typed_failure_class"),
            "not_runnable_reason": r.get("not_runnable_reason"),
        }
        for r in s1
        if not is_completion(r)
    ]
    literal = {
        "reading": "(a) literal prereg denominator 20 per system; NOT_RUNNABLE counts as not-completed for S1 and as not-failed for N0",
        "s1_completions": s1_completions,
        "s1_denominator": prereg_denominator,
        "s1_planned_cells_found": len(s1),
        "n0_fail_or_exceed_count": n0_fail_count,
        "n0_denominator": prereg_denominator,
        "n0_planned_cells_found": len(n0),
        "s1_condition_ge_19_of_20": s1_completions >= 19,
        "n0_condition_ge_6_of_20": n0_fail_count >= 6,
    }
    literal["verdict"] = "CLAIMABLE" if literal["s1_condition_ge_19_of_20"] and literal["n0_condition_ge_6_of_20"] else "NOT_CLAIMABLE"
    s1_runnable_completions = sum(1 for r in s1_runnable if is_completion(r))
    n0_runnable_fail = sum(1 for r in n0_runnable if n0_fails_or_exceeds(r) is True)
    proportional = {
        "reading": "(b) runnable-only denominators with the prereg thresholds read proportionally (19/20 -> at most one S1 non-completion; 6/20 -> >= 30% of N0 cells); this proportional reading is an interpretation, not prereg text",
        "s1_completions": s1_runnable_completions,
        "s1_denominator": len(s1_runnable),
        "n0_fail_or_exceed_count": n0_runnable_fail,
        "n0_denominator": len(n0_runnable),
        "s1_condition_at_most_one_noncompletion": (len(s1_runnable) - s1_runnable_completions) <= 1 and len(s1_runnable) > 0,
        "n0_condition_ge_30_percent": len(n0_runnable) > 0 and (n0_runnable_fail / len(n0_runnable)) >= 0.30,
    }
    proportional["verdict"] = (
        "CLAIMABLE_UNDER_PROPORTIONAL_READING"
        if proportional["s1_condition_at_most_one_noncompletion"] and proportional["n0_condition_ge_30_percent"]
        else "NOT_CLAIMABLE_UNDER_PROPORTIONAL_READING"
    )
    return {
        "gate": "CR-1 rotation-family completion robustness",
        "prereg_text": "claimable iff S1 completes >= 19/20 rotation-family samples AND N0 fails or exceeds the usability threshold on >= 6/20, with failures typed",
        "rotation_family": list(ROTATION_FAMILY),
        "literal": literal,
        "proportional": proportional,
        "n0_failures_typed": n0_failures_typed,
        "n0_unassessable_cells": n0_unassessable,
        "s1_noncompletions_typed": s1_noncompletions_typed,
        "verdict_literal": literal["verdict"],
        "verdict_proportional": proportional["verdict"],
    }


def gate_cr2(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    s1 = [r for r in _matrix_records(records) if r.get("system") == "S1" and r.get("outcome") == "CLOSED"]
    valid = [r for r in s1 if r.get("evidence_validity") == "VALID"]
    invalid = [r for r in s1 if r.get("evidence_validity") != "VALID"]

    def is_offset0(r: Mapping[str, Any]) -> bool:
        return r.get("axis") == "offset" and int(r.get("offset_frames") or 0) == 0

    def cls(r: Mapping[str, Any]) -> Optional[str]:
        return r.get("typed_failure_class")

    global_offset0 = sorted({str(cls(r)) for r in valid if is_offset0(r) and cls(r)})
    perturbed = [r for r in valid if not is_offset0(r)]
    occurrences = [
        {
            "run_id": r.get("run_id"),
            "sequence": r.get("sequence"),
            "axis": r.get("axis"),
            "offset_frames": r.get("offset_frames"),
            "seed_label": r.get("seed_label"),
            "typed_failure_class": cls(r),
            "status": r.get("status"),
        }
        for r in perturbed
        if cls(r)
    ]
    global_new = sorted({str(o["typed_failure_class"]) for o in occurrences} - set(global_offset0))
    per_sequence: Dict[str, Any] = {}
    for r in valid:
        per_sequence.setdefault(str(r.get("sequence")), {"offset0_classes": set(), "perturbed_classes": set(), "perturbed_occurrences": []})
    for r in valid:
        entry = per_sequence[str(r.get("sequence"))]
        if is_offset0(r):
            if cls(r):
                entry["offset0_classes"].add(str(cls(r)))
        elif cls(r):
            entry["perturbed_classes"].add(str(cls(r)))
            entry["perturbed_occurrences"].append(r.get("run_id"))
    per_sequence_new = {}
    for sequence, entry in per_sequence.items():
        new = sorted(entry["perturbed_classes"] - entry["offset0_classes"])
        per_sequence_new[sequence] = {
            "offset0_classes": sorted(entry["offset0_classes"]),
            "perturbed_classes": sorted(entry["perturbed_classes"]),
            "new_classes": new,
            "perturbed_failure_run_ids": entry["perturbed_occurrences"],
        }
    any_per_sequence_new = any(v["new_classes"] for v in per_sequence_new.values())
    return {
        "gate": "CR-2 no new S1 fragility",
        "prereg_text": "S1 introduces no failure class absent at offset 0 anywhere in the matrix; any occurrence is reported regardless",
        "global_reading": {
            "reading": "(a) classes observed at offset 0 anywhere in the S1 matrix form the reference set",
            "offset0_failure_classes": global_offset0,
            "perturbed_failure_occurrences": occurrences,
            "new_failure_classes": global_new,
            "verdict": "PASS" if not global_new else "FAIL",
        },
        "per_sequence_reading": {
            "reading": "(b) per sequence: classes at that sequence's offset-0 frozen-seed cell form the reference set",
            "sequences": per_sequence_new,
            "verdict": "PASS" if not any_per_sequence_new else "FAIL",
        },
        "evidence_invalid_s1_cells_excluded_from_class_sets": [
            {"run_id": r.get("run_id"), "status": r.get("status"), "evidence_validity": r.get("evidence_validity")}
            for r in invalid
        ],
        "all_s1_perturbed_failure_occurrences_reported": occurrences,
        "verdict_global": "PASS" if not global_new else "FAIL",
        "verdict_per_sequence": "PASS" if not any_per_sequence_new else "FAIL",
    }


def gate_cr3(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "gate": "CR-3 regression characterization (descriptive)",
        "prereg_text": "square_fast / circle_fast / square_head S1-vs-N0 ATE distributions reported in full - including if S1's regression persists across all perturbations",
        "sequences": {},
    }
    for sequence in CR3_SEQUENCES:
        items = [r for r in _matrix_records(records) if r.get("sequence") == sequence]
        by_system: Dict[str, Any] = {}
        matched = []
        for system in ("S1", "N0"):
            cells = [r for r in items if r.get("system") == system]
            closed = [r for r in cells if r.get("outcome") == "CLOSED" and r.get("evidence_validity") == "VALID"]
            completions = [r for r in closed if r.get("passage_complete")]
            by_system[system] = {
                "planned": len(cells),
                "executed": len(closed),
                "completions": len(completions),
                "typed_failure_classes": sorted({str(r.get("typed_failure_class")) for r in closed if r.get("typed_failure_class")}),
                "ate_translation_rmse_m": _distribution([a for a in (_ate(r) for r in completions) if a is not None]),
                "per_cell": [
                    {
                        "run_id": r.get("run_id"),
                        "axis": r.get("axis"),
                        "offset_frames": r.get("offset_frames"),
                        "seed_label": r.get("seed_label"),
                        "status": r.get("status"),
                        "passage_complete": r.get("passage_complete"),
                        "ate_translation_rmse_m": _ate(r),
                        "rpe_translation_rmse_1m_m": _finite((r.get("metrics") or {}).get("rpe_translation_rmse_1m_m")),
                        "rpe_rotation_rmse_1m_deg": _finite((r.get("metrics") or {}).get("rpe_rotation_rmse_1m_deg")),
                    }
                    for r in closed
                ],
            }
        # matched perturbations (same axis/offset/seed) with both ATEs
        s1_by_key = {(r.get("axis"), r.get("offset_frames"), r.get("seed_label")): r for r in items if r.get("system") == "S1"}
        n0_by_key = {(r.get("axis"), r.get("offset_frames"), r.get("seed_label")): r for r in items if r.get("system") == "N0"}
        for key in sorted(set(s1_by_key) & set(n0_by_key), key=lambda k: (k[0], k[1], k[2])):
            a, b = _ate(s1_by_key[key]), _ate(n0_by_key[key])
            matched.append(
                {
                    "axis": key[0],
                    "offset_frames": key[1],
                    "seed_label": key[2],
                    "s1_ate_m": a,
                    "n0_ate_m": b,
                    "s1_minus_n0_m": (a - b) if (a is not None and b is not None) else None,
                    "s1_worse": (a > b) if (a is not None and b is not None) else None,
                }
            )
        comparable = [m for m in matched if m["s1_worse"] is not None]
        out["sequences"][sequence] = {
            "systems": by_system,
            "matched_perturbations": matched,
            "comparable_matched_count": len(comparable),
            "s1_worse_count": sum(1 for m in comparable if m["s1_worse"]),
            "s1_regression_persists_across_all_comparable_perturbations": (
                bool(comparable) and all(m["s1_worse"] for m in comparable)
            ),
        }
    return out


def usability(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    kaist = [r for r in _matrix_records(records) if _is_kaist(r)]
    table = []
    groups: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for r in kaist:
        groups.setdefault((str(r["sequence"]), str(r["system"])), []).append(r)
    for (sequence, system), items in sorted(groups.items(), key=lambda kv: (kv[1][0]["order"], kv[0][1])):
        closed = [r for r in items if r.get("outcome") == "CLOSED" and r.get("evidence_validity") == "VALID"]
        table.append(
            {
                "sequence": sequence,
                "system": system,
                "executed": len(closed),
                "planned": len(items),
                "complete": sum(1 for r in closed if r.get("passage_complete")),
                "usable": sum(1 for r in closed if _usable(r) is True),
                "complete_but_over_threshold": sum(1 for r in closed if _usable(r) is False and r.get("passage_complete")),
                "complete_unassessable_ate": sum(1 for r in closed if r.get("passage_complete") and _usable(r) is None),
                "not_complete": sum(1 for r in closed if not r.get("passage_complete")),
            }
        )
    cdsc: List[Dict[str, Any]] = []
    if CDSC_PAIR_METRICS.is_file():
        with CDSC_PAIR_METRICS.open(newline="", encoding="utf-8") as stream:
            for row in csv.DictReader(stream):
                if row.get("dataset") != "kaist_vio":
                    continue
                entry = {"sequence": row.get("sequence"), "pair_status": row.get("status")}
                for system in ("U0", "S1"):
                    value = _finite(row.get("ate_translation_rmse_m_" + system.lower()))
                    entry[system + "_common_population_ate_m"] = value
                    entry[system + "_within_threshold"] = None if value is None else value <= USABILITY_ATE_M
                cdsc.append(entry)
    return {
        "definition": "Usable completion on KAIST := passage complete AND ATE <= 0.5 m (~14% of the 3.6 m arena major dimension); ATE per DECISIONS.md D8",
        "threshold_m": USABILITY_ATE_M,
        "disclosure_verbatim": DISCLOSURE,
        "perturb1_table": table,
        "cdsc1r4_descriptive_n1": {
            "note": "CDSC-1R4 KAIST common-population (U0∩S1) ATE per pair_metrics.csv; n=1 per cell; rotation.bag pair was NOT_EVALUATED because U0 did not complete",
            "rows": cdsc,
        },
    }


def n0_configuration_diff(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"launch_diffs": {}, "resolved_parameter_diffs": []}
    for label, s1_launch, n0_launch in (
        ("kaist", REPO_ROOT / "project" / "rotation_robustness_serial.launch", REPO_ROOT / "project" / "icra27_kaist_n0_serial.launch"),
        ("euroc", REPO_ROOT / "project" / "icra27_cross_dataset_s1_serial.launch", REPO_ROOT / "project" / "icra27_cross_dataset_n0_serial.launch"),
    ):
        if s1_launch.is_file() and n0_launch.is_file():
            diff = list(
                difflib.unified_diff(
                    s1_launch.read_text().splitlines(),
                    n0_launch.read_text().splitlines(),
                    fromfile=str(s1_launch),
                    tofile=str(n0_launch),
                    lineterm="",
                )
            )
            result["launch_diffs"][label] = {
                "s1_launch": campaign.file_identity(s1_launch),
                "n0_launch": campaign.file_identity(n0_launch),
                "unified_diff": diff,
                "changed_parameter_lines": [line for line in diff if line.startswith(("+", "-")) and "up_msckf_landmark_elimination" in line],
            }
    # Resolved ROS parameter map diff for every matched S1/N0 pair present.
    closed = [r for r in records if r.get("outcome") == "CLOSED"]
    by_key: Dict[Tuple[Any, ...], Dict[str, Mapping[str, Any]]] = {}
    for r in closed:
        key = (r.get("set"), r.get("sequence"), r.get("axis"), r.get("offset_frames"), r.get("seed_label"))
        by_key.setdefault(key, {})[str(r.get("system"))] = r
    for key, pair in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        if "S1" not in pair or "N0" not in pair:
            continue
        maps = {}
        for system in ("S1", "N0"):
            dump = Path(str(pair[system]["run_directory"])) / "diagnostics" / "resolved_ros_parameters.yaml"
            maps[system] = yaml.safe_load(dump.read_text()) if dump.is_file() else None
        if maps["S1"] is None or maps["N0"] is None:
            continue
        ignore_suffixes = {"filepath_est", "filepath_std", "record_timing_filepath"}
        changed = {}
        for name in sorted(set(maps["S1"]) | set(maps["N0"])):
            if name.rsplit("/", 1)[-1] in ignore_suffixes:
                continue
            if maps["S1"].get(name) != maps["N0"].get(name):
                changed[name] = {"S1": maps["S1"].get(name), "N0": maps["N0"].get(name)}
        result["resolved_parameter_diffs"].append(
            {
                "sequence": key[1],
                "axis": key[2],
                "offset_frames": key[3],
                "seed_label": key[4],
                "s1_run_id": pair["S1"].get("run_id"),
                "n0_run_id": pair["N0"].get("run_id"),
                "changed_parameters_excluding_run_owned_output_paths": changed,
                "single_delta": list(changed) == [k for k in changed if k.endswith("/up_msckf_landmark_elimination")] and len(changed) == 1,
            }
        )
    result["all_pairs_single_delta"] = bool(result["resolved_parameter_diffs"]) and all(
        d["single_delta"] for d in result["resolved_parameter_diffs"]
    )
    return result


def accounting(records: Sequence[Mapping[str, Any]], planned_total: int) -> Dict[str, Any]:
    matrix = _matrix_records(records)
    closed = [r for r in matrix if r.get("outcome") == "CLOSED"]
    valid = [r for r in closed if r.get("evidence_validity") == "VALID"]
    return {
        "prereg_total_runs": 190,
        "planned_cells_in_driver": planned_total,
        "cell_records_found": len(matrix),
        "not_runnable": sum(1 for r in matrix if r.get("outcome") == "NOT_RUNNABLE"),
        "not_runnable_by_reason": {
            "seed_no_runtime_parameter": sum(1 for r in matrix if r.get("outcome") == "NOT_RUNNABLE" and r.get("axis") == "seed"),
            "negative_offset_no_lead_in": sum(1 for r in matrix if r.get("outcome") == "NOT_RUNNABLE" and r.get("axis") == "offset"),
        },
        "executed_closed": len(closed),
        "executed_evidence_valid": len(valid),
        "executed_evidence_invalid_or_fatal": len(closed) - len(valid),
        "unclosed_or_no_result": sum(1 for r in matrix if r.get("outcome") not in ("CLOSED", "NOT_RUNNABLE")),
        "not_yet_recorded": planned_total - len(matrix),
        "completions": sum(1 for r in valid if r.get("passage_complete")),
        "typed_failures": sum(1 for r in valid if not r.get("passage_complete")),
        "by_status": {
            status: sum(1 for r in closed if r.get("status") == status)
            for status in sorted({str(r.get("status")) for r in closed})
        },
        "by_set": {
            name: {
                "planned": sum(1 for r in matrix if r.get("set") == name),
                "not_runnable": sum(1 for r in matrix if r.get("set") == name and r.get("outcome") == "NOT_RUNNABLE"),
                "executed": sum(1 for r in matrix if r.get("set") == name and r.get("outcome") == "CLOSED"),
                "completions": sum(1 for r in valid if r.get("set") == name and r.get("passage_complete")),
            }
            for name in ("kaist-focus", "kaist-full-remaining", "euroc-forks")
        },
    }


def determinism_summary(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = []
    for r in records:
        det = r.get("determinism") or {}
        cd = det.get("against_cdsc1r4")
        rep = det.get("against_offset0_replicate")
        if cd or rep:
            rows.append(
                {
                    "run_id": r.get("run_id"),
                    "set": r.get("set"),
                    "sequence": r.get("sequence"),
                    "system": r.get("system"),
                    "against_cdsc1r4": (cd or {}).get("status"),
                    "against_offset0_replicate": (rep or {}).get("status"),
                }
            )
    return {
        "rows": rows,
        "cdsc1r4_checks": {
            "byte_identical": sum(1 for x in rows if x["against_cdsc1r4"] == "BYTE_IDENTICAL"),
            "mismatch": sum(1 for x in rows if x["against_cdsc1r4"] == "MISMATCH"),
        },
        "replicate_checks": {
            "byte_identical": sum(1 for x in rows if x["against_offset0_replicate"] == "BYTE_IDENTICAL"),
            "mismatch": sum(1 for x in rows if x["against_offset0_replicate"] == "MISMATCH"),
        },
    }


def _cell_rows(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in sorted(records, key=lambda x: (x.get("set"), x.get("order"), x.get("axis"), x.get("offset_frames"), x.get("seed_label"), x.get("system"))):
        metrics = r.get("metrics") or {}
        det = r.get("determinism") or {}
        rows.append(
            {
                "set": r.get("set"),
                "order": r.get("order"),
                "dataset": r.get("dataset"),
                "sequence": r.get("sequence"),
                "system": r.get("system"),
                "axis": r.get("axis"),
                "offset_frames": r.get("offset_frames"),
                "seed_label": r.get("seed_label"),
                "frame_rate_hz": r.get("frame_rate_hz"),
                "estimator_bag_start_seconds": r.get("estimator_bag_start_seconds_repr"),
                "runnable": r.get("runnable"),
                "outcome": r.get("outcome"),
                "status": r.get("status"),
                "evidence_validity": r.get("evidence_validity"),
                "passage_complete": r.get("passage_complete"),
                "typed_failure_class": r.get("typed_failure_class"),
                "strict_process_health": r.get("strict_process_health"),
                "ate_translation_rmse_m": metrics.get("ate_translation_rmse_m"),
                "rpe_translation_rmse_1m_m": metrics.get("rpe_translation_rmse_1m_m"),
                "rpe_rotation_rmse_1m_deg": metrics.get("rpe_rotation_rmse_1m_deg"),
                "metrics_status": r.get("metrics_status"),
                "kaist_usable_completion": _usable(r),
                "determinism_vs_cdsc1r4": ((det.get("against_cdsc1r4") or {}).get("status")),
                "determinism_vs_offset0_replicate": ((det.get("against_offset0_replicate") or {}).get("status")),
                "robustness_mechanism_status": r.get("robustness_mechanism_status"),
                "runner_duration_seconds": r.get("runner_duration_seconds"),
                "started_utc": r.get("started_utc"),
                "finished_utc": r.get("finished_utc"),
                "run_id": r.get("run_id"),
                "run_directory": r.get("run_directory"),
                "sequence_result_sha256": r.get("sequence_result_sha256"),
                "cell_metrics_sha256": r.get("cell_metrics_sha256"),
                "not_runnable_reason": r.get("not_runnable_reason"),
            }
        )
    return rows


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]], header: Sequence[str]) -> None:
    with path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(header), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else json.dumps(row.get(k)) if isinstance(row.get(k), (list, dict)) else row.get(k)) for k in header})


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return "{:.{}f}".format(value, digits)
    return str(value)


def render_report(aggregate: Mapping[str, Any]) -> str:
    acc = aggregate["accounting"]
    lines = [
        "# PERTURB-1 perturbation campaign — aggregate report",
        "",
        "Generated {} from artifact root `{}`. Campaign prereg: `docs/icra27/PERTURBATION_PREREG.md` (SHA-256 `{}`).".format(
            aggregate["generated_utc"], aggregate["artifact_root"], aggregate["prereg"]["sha256"]
        ),
        "",
        "Systems: S1 (frozen paper-intent config) and **N0** (in-repo matched nullspace control: the frozen S1 binary/config/launch with the single ROS parameter `up_msckf_landmark_elimination` set to `nullspace`; see `n0_configuration_diff`). U0 stock rows were not run (optional context only).",
        "",
        "## Integrity gate",
        "",
        "Verdict: **{}** — {}".format(aggregate["integrity_gate"].get("verdict"), "; ".join(
            "{} {} vs CDSC-1R4: {}".format(c.get("sequence"), c.get("system"), c.get("determinism_status"))
            for c in aggregate["integrity_gate"].get("cells", [])
        ) or "no integrity records"),
        "",
        "## Run accounting",
        "",
        "| Quantity | Count |",
        "|---|---:|",
        "| Prereg total runs | {} |".format(acc["prereg_total_runs"]),
        "| Cell records found | {} |".format(acc["cell_records_found"]),
        "| NOT_RUNNABLE (seed axis, no runtime seed parameter) | {} |".format(acc["not_runnable_by_reason"]["seed_no_runtime_parameter"]),
        "| NOT_RUNNABLE (negative offset, no lead-in data) | {} |".format(acc["not_runnable_by_reason"]["negative_offset_no_lead_in"]),
        "| Executed and closed | {} |".format(acc["executed_closed"]),
        "| Executed, evidence VALID | {} |".format(acc["executed_evidence_valid"]),
        "| Executed, evidence invalid / fatal | {} |".format(acc["executed_evidence_invalid_or_fatal"]),
        "| Unclosed / no result | {} |".format(acc["unclosed_or_no_result"]),
        "| Not yet recorded (planned − found) | {} |".format(acc["not_yet_recorded"]),
        "| Completions (valid) | {} |".format(acc["completions"]),
        "| Typed failures (valid, not complete) | {} |".format(acc["typed_failures"]),
        "",
        "By status: " + ", ".join("{}={}".format(k, v) for k, v in acc["by_status"].items()),
        "",
        "By set: " + "; ".join("{}: planned {}, not_runnable {}, executed {}, completions {}".format(k, v["planned"], v["not_runnable"], v["executed"], v["completions"]) for k, v in acc["by_set"].items()),
        "",
        "## Determinism",
        "",
        "Offset-0 frozen-seed S1 cells vs CDSC-1R4: byte-identical {}, mismatch {}. Frozen-seed replicates vs offset-0 cells: byte-identical {}, mismatch {}.".format(
            aggregate["determinism"]["cdsc1r4_checks"]["byte_identical"],
            aggregate["determinism"]["cdsc1r4_checks"]["mismatch"],
            aggregate["determinism"]["replicate_checks"]["byte_identical"],
            aggregate["determinism"]["replicate_checks"]["mismatch"],
        ),
        "",
        "## Per-sequence-per-system summary",
        "",
        "| Order | Sequence | System | Completions/planned | Completions/executed | NOT_RUNNABLE | Usable (KAIST) | ATE median [m] | ATE IQR [m] | ATE n | Failure classes |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in aggregate["per_sequence_system"]:
        d = row["ate_translation_rmse_m"]
        lines.append(
            "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row["order"], row["sequence"], row["system"], row["completion_over_planned"], row["completion_over_executed"],
                row["not_runnable_cells"], _fmt(row["usable_completion_count"]), _fmt(d["median"]), _fmt(d["iqr"]), d["count"],
                ", ".join(row["typed_failure_classes"]) or "—",
            )
        )
    u = aggregate["usability"]
    lines += [
        "",
        "## Usability threshold (prospective)",
        "",
        u["definition"],
        "",
        u["disclosure_verbatim"],
        "",
        "| Sequence | System | Executed/planned | Complete | Usable (ATE ≤ 0.5 m) | Complete but > 0.5 m | Complete, ATE unassessable | Not complete |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in u["perturb1_table"]:
        lines.append("| {} | {} | {}/{} | {} | {} | {} | {} | {} |".format(
            row["sequence"], row["system"], row["executed"], row["planned"], row["complete"], row["usable"],
            row["complete_but_over_threshold"], row["complete_unassessable_ate"], row["not_complete"]))
    lines += ["", "CDSC-1R4 (n=1, common-population ATE, descriptive):", "", "| Sequence | U0 ATE [m] | U0 ≤ 0.5 | S1 ATE [m] | S1 ≤ 0.5 |", "|---|---:|---|---:|---|"]
    for row in u["cdsc1r4_descriptive_n1"]["rows"]:
        lines.append("| {} | {} | {} | {} | {} |".format(row["sequence"], _fmt(row["U0_common_population_ate_m"]), row["U0_within_threshold"], _fmt(row["S1_common_population_ate_m"]), row["S1_within_threshold"]))
    cr1 = aggregate["gates"]["CR-1"]
    cr2 = aggregate["gates"]["CR-2"]
    cr3 = aggregate["gates"]["CR-3"]
    lines += [
        "",
        "## Pre-registered claim gates",
        "",
        "### CR-1 rotation-family completion robustness",
        "",
        "Prereg: " + cr1["prereg_text"],
        "",
        "- Reading (a), literal: S1 completions {}/{}; N0 fails-or-exceeds {}/{} → **{}**.".format(
            cr1["literal"]["s1_completions"], cr1["literal"]["s1_denominator"], cr1["literal"]["n0_fail_or_exceed_count"], cr1["literal"]["n0_denominator"], cr1["literal"]["verdict"]),
        "- Reading (b), runnable-only with proportional thresholds (interpretation, not prereg text): S1 completions {}/{}; N0 fails-or-exceeds {}/{} → **{}**.".format(
            cr1["proportional"]["s1_completions"], cr1["proportional"]["s1_denominator"], cr1["proportional"]["n0_fail_or_exceed_count"], cr1["proportional"]["n0_denominator"], cr1["proportional"]["verdict"]),
        "- N0 failures typed: " + ("; ".join("{} [{}]{}".format(f["run_id"], f["typed_failure_class"] or "COMPLETE", " ATE={:.3f}m>0.5".format(f["ate_translation_rmse_m"]) if f["exceeds_usability_threshold"] else "") for f in cr1["n0_failures_typed"]) or "none"),
        "- S1 non-completions (incl. NOT_RUNNABLE): " + ("; ".join("{} [{}]".format(f["run_id"], f["typed_failure_class"] or f["outcome"]) for f in cr1["s1_noncompletions_typed"]) or "none"),
        "",
        "### CR-2 no new S1 fragility",
        "",
        "Prereg: " + cr2["prereg_text"],
        "",
        "- Global reading: offset-0 classes {}; new perturbed classes {} → **{}**.".format(cr2["global_reading"]["offset0_failure_classes"], cr2["global_reading"]["new_failure_classes"], cr2["global_reading"]["verdict"]),
        "- Per-sequence reading → **{}**; sequences with new classes: {}.".format(
            cr2["per_sequence_reading"]["verdict"],
            {k: v["new_classes"] for k, v in cr2["per_sequence_reading"]["sequences"].items() if v["new_classes"]} or "none"),
        "- All S1 perturbed failure occurrences: " + ("; ".join("{} [{}]".format(o["run_id"], o["typed_failure_class"]) for o in cr2["all_s1_perturbed_failure_occurrences_reported"]) or "none"),
        "",
        "### CR-3 regression characterization (descriptive)",
        "",
        "Prereg: " + cr3["prereg_text"],
        "",
        "| Sequence | System | Executed | Completions | ATE median [m] | ATE IQR [m] | ATE min | ATE max | values |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for sequence, entry in cr3["sequences"].items():
        for system in ("S1", "N0"):
            s = entry["systems"][system]
            d = s["ate_translation_rmse_m"]
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                sequence, system, s["executed"], s["completions"], _fmt(d["median"]), _fmt(d["iqr"]), _fmt(d["min"]), _fmt(d["max"]),
                ", ".join("{:.4f}".format(v) for v in d["values"])))
        lines.append("| {} | matched | comparable {} | S1 worse in {} | persists across all: {} | | | | |".format(
            sequence, entry["comparable_matched_count"], entry["s1_worse_count"], entry["s1_regression_persists_across_all_comparable_perturbations"]))
    lines += [
        "",
        "Statistics language: counts and distributions only. Runnable samples per rotation-family cell are below n=10, so no Fisher's exact test and no significance language.",
        "",
        "## N0 configuration diff",
        "",
        "Launch diffs (S1 → N0): " + "; ".join("{}: {}".format(k, v["changed_parameter_lines"]) for k, v in aggregate["n0_configuration_diff"]["launch_diffs"].items()),
        "",
        "Resolved-ROS-parameter maps of every matched S1/N0 pair differ in exactly `up_msckf_landmark_elimination` (run-owned output paths excluded): **{}** ({} pairs).".format(
            aggregate["n0_configuration_diff"]["all_pairs_single_delta"], len(aggregate["n0_configuration_diff"]["resolved_parameter_diffs"])),
        "",
        "## Evidence inventory",
        "",
        "Machine-readable: `aggregate.json`, `cells.csv`, `sequence_system_summary.csv`, `SHA256SUMS`. Every cell's raw run directory (append-only, checksum-closed) and driver record are listed in `cells.csv`.",
    ]
    return "\n".join(lines) + "\n"


def aggregate_campaign(root: Path, output: Path) -> Dict[str, Any]:
    root = root.resolve(strict=True)
    if output.exists():
        raise AggregateError("refusing to overwrite aggregate output: {}".format(output))
    records = _load_records(root)
    rows = campaign.load_matrix(campaign.RuntimePaths().matrix)
    planned = driver.plan_cells(rows, ["kaist-focus", "kaist-full-remaining", "euroc-forks"])
    integrity_path = root / "integrity" / "INTEGRITY_GATE.json"
    integrity = campaign.load_json(integrity_path, "integrity gate")[0] if integrity_path.is_file() else {"verdict": "NOT_RUN"}
    aggregate: Dict[str, Any] = {
        "schema": SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "generated_utc": campaign.utc_now(),
        "artifact_root": str(root),
        "prereg": campaign.file_identity(driver.PREREG_FILE),
        "tooling_git": campaign.git_identity(REPO_ROOT),
        "aggregator": campaign.file_identity(Path(__file__)),
        "integrity_gate": integrity,
        "accounting": accounting(records, len(planned)),
        "determinism": determinism_summary(records),
        "per_sequence_system": per_sequence_system(records),
        "usability": usability(records),
        "gates": {"CR-1": gate_cr1(records), "CR-2": gate_cr2(records), "CR-3": gate_cr3(records)},
        "n0_configuration_diff": n0_configuration_diff(records),
        "cells": _cell_rows(records),
        "statistics_language": "counts and distributions only; no significance language (runnable n per cell < 10)",
    }
    staging = output.parent / (".staging-" + uuid.uuid4().hex)
    staging.mkdir(parents=True, mode=0o750)
    try:
        (staging / "aggregate.json").write_bytes(driver._json_bytes(aggregate))
        cell_header = list(aggregate["cells"][0].keys()) if aggregate["cells"] else ["set"]
        _write_csv(staging / "cells.csv", aggregate["cells"], cell_header)
        summary_rows = []
        for row in aggregate["per_sequence_system"]:
            flat = {k: v for k, v in row.items() if not isinstance(v, dict)}
            for metric in ("ate_translation_rmse_m", "rpe_translation_rmse_1m_m", "rpe_rotation_rmse_1m_deg"):
                d = row[metric]
                flat[metric + "_n"] = d["count"]
                flat[metric + "_median"] = d["median"]
                flat[metric + "_q1"] = d["q1"]
                flat[metric + "_q3"] = d["q3"]
                flat[metric + "_iqr"] = d["iqr"]
            summary_rows.append(flat)
        _write_csv(staging / "sequence_system_summary.csv", summary_rows, list(summary_rows[0].keys()) if summary_rows else ["order"])
        (staging / "PERTURBATION_REPORT.md").write_text(render_report(aggregate), encoding="utf-8")
        driver._write_sha256sums(staging)
        os.rename(str(staging), str(output))
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return aggregate


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--artifact-root", required=True, type=Path)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        aggregate = aggregate_campaign(args.artifact_root, args.output)
    except (OSError, ValueError, AggregateError, campaign.CampaignError) as exc:
        print("PERTURBATION_AGGREGATE_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("aggregate: {} cells={} -> {}".format(aggregate["accounting"]["cell_records_found"], len(aggregate["cells"]), args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
