#!/usr/bin/python3.8
"""ABLATE-REC-1 aggregator: per-cell table, P1/P2/P3 evaluated mechanically
against docs/icra27/ABLATION_PREREG.md, the interpretation-table row, the
usability threshold with its disclosure line, and the recOFF-vs-U0
failure-class comparison.  Reads only the driver records (revalidating their
checksums and the run checksum closures); never touches a run directory.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_campaign as campaign  # noqa: E402
import ablation_campaign as driver  # noqa: E402
import perturbation_aggregate as pagg  # noqa: E402

SCHEMA = "schurvio.icra27.ablation.aggregate.v1"
CAMPAIGN_ID = "ABLATE-REC-1"
USABILITY_ATE_M = pagg.USABILITY_ATE_M
DISCLOSURE = pagg.DISCLOSURE
ROTATION_BAG = "rotation/rotation.bag"
ROTATION_FAST = "rotation/rotation_fast.bag"
CDSC1R4_U0_ROTATION = (
    driver.CDSC1R4_ROOT / "scored" / "19-rotation_rotation.bag" / "U0" / "cdsc1r4-19-rotation_rotation.bag-u0-scored-a1" / "sequence_result.json"
)
INTERPRETATION_TABLE = [
    {
        "row": 1,
        "outcome": "P1 true, P2 true, P3 true",
        "meaning": "Recovery subsystem is the attributed completion mechanism for rotation-induced tracking loss",
        "permitted_claim": "Disabling the recovery subsystem alone reproduces the stock-baseline failure mode on rotation sequences; the recovery mechanism is necessary for completion in this stack.",
    },
    {
        "row": 2,
        "outcome": "P1 false (recOFF completes)",
        "meaning": "Recovery is NOT the mechanism; residual repo-vs-upstream delta responsible; bisection required",
        "permitted_claim": "No mechanism claim. Whole-stack claims only.",
    },
    {
        "row": 3,
        "outcome": "P2 false",
        "meaning": "The switch has side effects beyond the recovery path; single-delta assumption broken",
        "permitted_claim": "STOP; find the true single-delta switch before claiming anything.",
    },
    {
        "row": 4,
        "outcome": "P3 false",
        "meaning": "Environment drift",
        "permitted_claim": "Campaign void.",
    },
]


class AggregateError(RuntimeError):
    pass


def _load_records(root: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    cells_root = root / "cells"
    if not cells_root.is_dir():
        return records
    for path in sorted(cells_root.glob("*/*/*/*.driver/cell_record.json")):
        value, identity = campaign.load_json(path, "cell record")
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


_ate = pagg._ate
_finite = pagg._finite
_usable = pagg._usable
_distribution = pagg._distribution
_fmt = pagg._fmt
_write_csv = pagg._write_csv


def _executed_valid(r: Mapping[str, Any]) -> bool:
    return r.get("outcome") == "CLOSED" and r.get("evidence_validity") == "VALID"


def _recoff(records: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    return [r for r in records if r.get("recovery_ablated")]


def _failure_signature(r: Mapping[str, Any]) -> Dict[str, Any]:
    completion = r.get("completion") or {}
    gaps = completion.get("unsupported_state_gaps") or []
    return {
        "status": r.get("status"),
        "typed_failure_class": r.get("typed_failure_class"),
        "passage_complete": r.get("passage_complete"),
        "passage_reason": r.get("passage_reason") or ((r.get("passage") or {}).get("reason")),
        "tail_gap_pass": completion.get("tail_gap_pass"),
        "tail_gap_s": completion.get("tail_gap_s"),
        "maximum_state_gap_pass": completion.get("maximum_state_gap_pass"),
        "maximum_state_gap_s": completion.get("maximum_state_gap_s"),
        "unsupported_state_gap_count": len(gaps),
        "unsupported_state_gaps": [
            {k: g.get(k) for k in ("start_timestamp_s", "end_timestamp_s", "duration_s")} for g in gaps
        ],
        "recovery_supported_state_gap_count": completion.get("recovery_supported_state_gap_count"),
        "ate_translation_rmse_m": _ate(r),
    }


def cdsc1r4_u0_reference() -> Dict[str, Any]:
    if not CDSC1R4_U0_ROTATION.is_file():
        return {"status": "REFERENCE_ABSENT"}
    value, identity = campaign.load_json(CDSC1R4_U0_ROTATION, "CDSC-1R4 U0 rotation result")
    completion = value.get("completion") or {}
    gaps = completion.get("unsupported_state_gaps") or []
    return {
        "status": "PRESENT",
        "path": str(CDSC1R4_U0_ROTATION),
        "sha256": identity.get("sha256"),
        "signature": {
            "status": value.get("status"),
            "typed_failure_class": driver.typed_failure_class(value),
            "passage_complete": bool((value.get("passage") or {}).get("complete")),
            "passage_reason": (value.get("passage") or {}).get("reason"),
            "tail_gap_pass": completion.get("tail_gap_pass"),
            "tail_gap_s": completion.get("tail_gap_s"),
            "maximum_state_gap_pass": completion.get("maximum_state_gap_pass"),
            "maximum_state_gap_s": completion.get("maximum_state_gap_s"),
            "unsupported_state_gap_count": len(gaps),
            "unsupported_state_gaps": [
                {k: g.get(k) for k in ("start_timestamp_s", "end_timestamp_s", "duration_s")} for g in gaps
            ],
            "evidence_validity": value.get("evidence_validity"),
        },
    }


def _same_family(sig: Mapping[str, Any], reference: Mapping[str, Any]) -> Dict[str, Any]:
    """Failure-class family = tracking loss / unclosed state gap (prereg P1 wording).

    A cell is in the family iff it is not passage-complete AND (its status is
    TRACKING_LOSS, or it has >= 1 unsupported (unclosed) state gap, or its
    tail-gap check fails); the reference U0 signature is reported alongside.
    """

    in_family = bool(
        not sig.get("passage_complete")
        and (
            sig.get("status") == "TRACKING_LOSS"
            or int(sig.get("unsupported_state_gap_count") or 0) >= 1
            or sig.get("tail_gap_pass") is False
        )
    )
    same_status = sig.get("status") == reference.get("status")
    gap_overlap = None
    if sig.get("unsupported_state_gaps") and reference.get("unsupported_state_gaps"):
        a = sig["unsupported_state_gaps"][0]
        b = reference["unsupported_state_gaps"][0]
        try:
            gap_overlap = {
                "start_delta_s": float(a["start_timestamp_s"]) - float(b["start_timestamp_s"]),
                "end_delta_s": float(a["end_timestamp_s"]) - float(b["end_timestamp_s"]),
                "duration_delta_s": float(a["duration_s"]) - float(b["duration_s"]),
            }
        except (TypeError, ValueError, KeyError):
            gap_overlap = None
    return {
        "in_tracking_loss_or_unclosed_gap_family": in_family,
        "same_status_as_cdsc1r4_u0": same_status,
        "first_unsupported_gap_vs_cdsc1r4_u0": gap_overlap,
    }


def prediction_p1(records: Sequence[Mapping[str, Any]], reference: Mapping[str, Any]) -> Dict[str, Any]:
    cells = [r for r in _recoff(records) if r.get("sequence") == ROTATION_BAG]
    planned = 6  # 2 recOFF systems x offsets {0,+5,+10}
    rows = []
    for r in sorted(cells, key=lambda x: (x.get("system"), x.get("offset_frames"))):
        sig = _failure_signature(r)
        ate = sig["ate_translation_rmse_m"]
        non_completion = not bool(r.get("passage_complete"))
        over_threshold = ate is not None and ate >= USABILITY_ATE_M
        family = _same_family(sig, reference.get("signature") or {}) if _executed_valid(r) else None
        rows.append(
            {
                "run_id": r.get("run_id"),
                "system": r.get("system"),
                "offset_frames": r.get("offset_frames"),
                "executed_valid": _executed_valid(r),
                "status": r.get("status"),
                "evidence_validity": r.get("evidence_validity"),
                "passage_complete": r.get("passage_complete"),
                "non_completion": non_completion,
                "ate_translation_rmse_m": ate,
                "ate_at_or_over_threshold": over_threshold,
                "p1_condition_met": bool(_executed_valid(r) and (non_completion or over_threshold)),
                "failure_signature": sig,
                "family": family,
                "recovery_events_observed": ((r.get("perturbation_recovery_descriptive") or {}).get("event_line_count")),
                "single_delta_verified": ((r.get("single_delta_vs_recon_counterpart") or {}).get("machine_verified_single_delta")),
            }
        )
    executed = [x for x in rows if x["executed_valid"]]
    met = [x for x in executed if x["p1_condition_met"]]
    in_family = [x for x in met if (x["family"] or {}).get("in_tracking_loss_or_unclosed_gap_family")]
    completes = [x for x in executed if not x["p1_condition_met"]]
    if len(executed) == 0:
        verdict = "NOT_EVALUABLE"
    elif completes:
        verdict = "FALSE"
    elif len(met) == len(executed) and len(in_family) == len(met) and len(executed) == planned:
        verdict = "TRUE"
    elif len(met) == len(executed) and len(in_family) == len(met):
        verdict = "TRUE_ON_EXECUTED_CELLS_ONLY"
    else:
        verdict = "TRUE_NONCOMPLETION_BUT_FAMILY_MISMATCH"
    per_system = {}
    for system in ("S1-recOFF", "N0-recOFF"):
        sub = [x for x in rows if x["system"] == system]
        per_system[system] = {
            "planned": 3,
            "executed_valid": sum(1 for x in sub if x["executed_valid"]),
            "p1_condition_met": sum(1 for x in sub if x["p1_condition_met"]),
            "in_family": sum(1 for x in sub if (x["family"] or {}).get("in_tracking_loss_or_unclosed_gap_family")),
            "completed_under_threshold": sum(1 for x in sub if x["executed_valid"] and not x["p1_condition_met"]),
        }
    return {
        "prereg_text": "S1-recOFF and N0-recOFF exhibit non-completion or >= usability-threshold error (0.5 m) on rotation.bag, in the same failure class family as U0 in CDSC-1R4 (tracking loss / unclosed state gap).",
        "denominator_planned": planned,
        "denominator_executed_valid": len(executed),
        "cells_meeting_condition": len(met),
        "cells_in_u0_failure_family": len(in_family),
        "cells_completing_under_threshold": len(completes),
        "per_system": per_system,
        "verdict": verdict,
        "verdict_rule": "TRUE iff all 6 planned cells executed VALID, every one is non-complete or ATE >= 0.5 m, and every one is in the tracking-loss/unclosed-gap family; FALSE iff any executed recOFF rotation.bag cell completes with ATE < 0.5 m; otherwise the qualified verdict is spelled out",
        "cdsc1r4_u0_reference": reference,
        "cells": rows,
    }


def prediction_p2(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = []
    for r in sorted(_recoff(records), key=lambda x: (x.get("order"), x.get("system"), x.get("offset_frames"))):
        cmp = (r.get("determinism") or {}).get("against_perturb1_recon_counterpart") or {}
        rows.append(
            {
                "run_id": r.get("run_id"),
                "set": r.get("set"),
                "sequence": r.get("sequence"),
                "system": r.get("system"),
                "offset_frames": r.get("offset_frames"),
                "executed_valid": _executed_valid(r),
                "status": r.get("status"),
                "counterpart_run_id": cmp.get("perturb1_run_id"),
                "counterpart_zero_recovery_events": cmp.get("counterpart_zero_recovery_events"),
                "counterpart_recovery_activations": cmp.get("counterpart_recovery_activations"),
                "byte_identity_status": cmp.get("status"),
                "single_delta_status": (r.get("single_delta_vs_recon_counterpart") or {}).get("status"),
                "machine_verified_single_delta": (r.get("single_delta_vs_recon_counterpart") or {}).get("machine_verified_single_delta"),
            }
        )
    reference_set = [x for x in rows if x["counterpart_zero_recovery_events"] is True]
    # The P2 reference set is fixed by Step 0: every recOFF cell except the six on rotation.bag.
    planned_reference = 36
    identical = [x for x in reference_set if x["byte_identity_status"] == "BYTE_IDENTICAL"]
    mismatch = [x for x in reference_set if x["byte_identity_status"] == "MISMATCH"]
    other = [x for x in reference_set if x["byte_identity_status"] not in ("BYTE_IDENTICAL", "MISMATCH")]
    if mismatch:
        verdict = "FALSE"
    elif len(identical) == planned_reference:
        verdict = "TRUE"
    elif identical and not other and len(reference_set) < planned_reference:
        verdict = "TRUE_ON_EXECUTED_CELLS_ONLY"
    elif not reference_set:
        verdict = "NOT_EVALUABLE"
    else:
        verdict = "INDETERMINATE"
    fired_set = [x for x in rows if x["counterpart_zero_recovery_events"] is False]
    return {
        "prereg_text": "On every cell whose PERTURB-1 recON counterpart recorded zero recovery events, recOFF is byte-identical to recON (the switch is inert when the mechanism never fires).",
        "reference_set_definition": "recOFF cells whose PERTURB-1 recON counterpart (same sequence, recON system, offset) recorded zero recovery activations; from Step 0 that is every recOFF cell except the six on rotation/rotation.bag (36 cells)",
        "denominator_planned_reference_set": planned_reference,
        "denominator_reference_cells_recorded": len(reference_set),
        "byte_identical": len(identical),
        "mismatch": len(mismatch),
        "not_comparable": len(other),
        "verdict": verdict,
        "verdict_rule": "TRUE iff all 36 reference cells are BYTE_IDENTICAL; FALSE iff any reference cell is MISMATCH; otherwise qualified",
        "mismatch_cells": mismatch,
        "not_comparable_cells": other,
        "fired_counterpart_cells_descriptive": [
            {k: x[k] for k in ("run_id", "system", "offset_frames", "status", "byte_identity_status", "counterpart_recovery_activations")}
            for x in fired_set
        ],
        "single_delta": {
            "recoff_cells_recorded": len(rows),
            "machine_verified_single_delta": sum(1 for x in rows if x["machine_verified_single_delta"] is True),
            "not_single_delta": [x["run_id"] for x in rows if x["machine_verified_single_delta"] is False],
            "unverified": [x["run_id"] for x in rows if x["machine_verified_single_delta"] is None],
        },
        "cells": rows,
    }


def prediction_p3(integrity: Mapping[str, Any]) -> Dict[str, Any]:
    verdict = integrity.get("verdict")
    return {
        "prereg_text": "All recON integrity replicas byte-identical to PERTURB-1.",
        "integrity_gate_verdict": verdict,
        "verdict": "TRUE" if verdict == "PASS" else ("FALSE" if verdict == "FAIL" else "NOT_EVALUABLE"),
        "cells": integrity.get("cells"),
    }


def interpretation(p1: Mapping[str, Any], p2: Mapping[str, Any], p3: Mapping[str, Any]) -> Dict[str, Any]:
    if p3["verdict"] == "FALSE":
        row = INTERPRETATION_TABLE[3]
        basis = "P3 is FALSE (an integrity replica is not byte-identical to PERTURB-1)"
    elif p3["verdict"] != "TRUE":
        return {
            "applies": None,
            "basis": "P3 not evaluable (integrity gate {}); no claim is licensed".format(p3.get("integrity_gate_verdict")),
            "permitted_claim": "No claim licensed (integrity gate not evaluated).",
        }
    elif p2["verdict"] == "FALSE":
        row = INTERPRETATION_TABLE[2]
        basis = "P2 is FALSE"
    elif p1["verdict"] == "FALSE":
        row = INTERPRETATION_TABLE[1]
        basis = "P1 is FALSE (a recOFF rotation.bag cell completed under threshold)"
    elif p1["verdict"] == "TRUE" and p2["verdict"] == "TRUE":
        row = INTERPRETATION_TABLE[0]
        basis = "P1, P2 and P3 all TRUE"
    else:
        return {
            "applies": None,
            "basis": "no table row applies mechanically: P1={} P2={} P3={} (qualified verdicts); no claim is licensed".format(p1["verdict"], p2["verdict"], p3["verdict"]),
            "permitted_claim": "No claim licensed (no interpretation-table row applies mechanically).",
        }
    return {"applies": row["row"], "basis": basis, **row}


def u0_context(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = []
    for r in sorted([x for x in records if x.get("system") == "U0"], key=lambda x: (x.get("order"), x.get("offset_frames"))):
        sig = _failure_signature(r)
        det = (r.get("determinism") or {}).get("against_cdsc1r4") or {}
        rows.append(
            {
                "run_id": r.get("run_id"),
                "sequence": r.get("sequence"),
                "offset_frames": r.get("offset_frames"),
                "executed_valid": _executed_valid(r),
                "status": r.get("status"),
                "evidence_validity": r.get("evidence_validity"),
                "passage_complete": r.get("passage_complete"),
                "typed_failure_class": r.get("typed_failure_class"),
                "ate_translation_rmse_m": sig["ate_translation_rmse_m"],
                "kaist_usable_completion": _usable(r),
                "failure_signature": sig,
                "determinism_vs_cdsc1r4": det.get("status"),
            }
        )
    by_seq = {}
    for seq in (ROTATION_BAG, ROTATION_FAST):
        sub = [x for x in rows if x["sequence"] == seq]
        by_seq[seq] = {
            "planned": 3,
            "executed_valid": sum(1 for x in sub if x["executed_valid"]),
            "completions": sum(1 for x in sub if x["passage_complete"]),
            "non_completions": sum(1 for x in sub if x["executed_valid"] and not x["passage_complete"]),
            "failure_classes": sorted({str(x["typed_failure_class"]) for x in sub if x["executed_valid"] and not x["passage_complete"]}),
        }
    return {"per_sequence": by_seq, "cells": rows}


def failure_class_comparison(p1: Mapping[str, Any], u0: Mapping[str, Any]) -> Dict[str, Any]:
    ref = (p1.get("cdsc1r4_u0_reference") or {}).get("signature") or {}
    table = []
    for x in p1["cells"]:
        table.append(
            {
                "cell": x["run_id"],
                "system": x["system"],
                "offset_frames": x["offset_frames"],
                "status": x["status"],
                "typed_failure_class": (x["failure_signature"] or {}).get("typed_failure_class"),
                "unsupported_gaps": (x["failure_signature"] or {}).get("unsupported_state_gaps"),
                "tail_gap_s": (x["failure_signature"] or {}).get("tail_gap_s"),
                "ate_m": x["ate_translation_rmse_m"],
                "family": x["family"],
            }
        )
    for x in u0["cells"]:
        if x["sequence"] == ROTATION_BAG:
            table.append(
                {
                    "cell": x["run_id"],
                    "system": "U0 (ABLATE-REC-1 context)",
                    "offset_frames": x["offset_frames"],
                    "status": x["status"],
                    "typed_failure_class": x["typed_failure_class"],
                    "unsupported_gaps": (x["failure_signature"] or {}).get("unsupported_state_gaps"),
                    "tail_gap_s": (x["failure_signature"] or {}).get("tail_gap_s"),
                    "ate_m": x["ate_translation_rmse_m"],
                    "family": _same_family(x["failure_signature"], ref) if x["executed_valid"] else None,
                }
            )
    return {
        "cdsc1r4_u0_rotation_signature": ref,
        "rows": table,
        "recoff_status_set": sorted({str(x["status"]) for x in p1["cells"] if x["executed_valid"]}),
        "u0_context_rotation_status_set": sorted({str(x["status"]) for x in u0["cells"] if x["sequence"] == ROTATION_BAG and x["executed_valid"]}),
    }


def accounting(records: Sequence[Mapping[str, Any]], planned: Sequence[Any]) -> Dict[str, Any]:
    by_set: Dict[str, Dict[str, int]] = {}
    for cell in planned:
        by_set.setdefault(cell.set_name, {"planned": 0})["planned"] += 1
    found = {r.get("run_id") for r in records}
    for r in records:
        entry = by_set.setdefault(str(r.get("set")), {"planned": 0})
        entry["recorded"] = entry.get("recorded", 0) + 1
        for key, cond in (
            ("not_runnable", r.get("outcome") == "NOT_RUNNABLE"),
            ("executed_closed", r.get("outcome") == "CLOSED"),
            ("executed_valid", _executed_valid(r)),
            ("executed_invalid_or_fatal", r.get("outcome") == "CLOSED" and r.get("evidence_validity") != "VALID"),
            ("unclosed_or_no_result", r.get("outcome") in ("UNCLOSED_RUN_DIRECTORY_RETAINED", "RUNNER_PUBLISHED_NO_RESULT")),
            ("completions_valid", _executed_valid(r) and bool(r.get("passage_complete"))),
            ("typed_failures_valid", _executed_valid(r) and not r.get("passage_complete")),
        ):
            entry[key] = entry.get(key, 0) + (1 if cond else 0)
    total = {"planned": len(planned), "recorded": len(records), "planned_but_not_recorded": [c.run_id for c in planned if c.run_id not in found]}
    for key in ("not_runnable", "executed_closed", "executed_valid", "executed_invalid_or_fatal", "unclosed_or_no_result", "completions_valid", "typed_failures_valid"):
        total[key] = sum(v.get(key, 0) for v in by_set.values())
    statuses: Dict[str, int] = {}
    for r in records:
        statuses[str(r.get("status"))] = statuses.get(str(r.get("status")), 0) + 1
    return {"total": total, "by_set": by_set, "by_status": statuses, "not_runnable_reasons": sorted({str(r.get("not_runnable_reason")) for r in records if r.get("outcome") == "NOT_RUNNABLE"})}


def usability(records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    rows = []
    kaist = [r for r in records if r.get("dataset") == "kaist_vio" and r.get("outcome") == "CLOSED"]
    keys = sorted({(r.get("sequence"), r.get("system")) for r in kaist})
    for seq, system in keys:
        sub = [r for r in kaist if r.get("sequence") == seq and r.get("system") == system]
        rows.append(
            {
                "sequence": seq,
                "system": system,
                "executed": len(sub),
                "usable_completions": sum(1 for r in sub if _usable(r) is True),
                "complete_but_over_threshold": sum(1 for r in sub if _usable(r) is False and r.get("passage_complete")),
                "complete_unassessable_ate": sum(1 for r in sub if _usable(r) is None and r.get("passage_complete")),
                "not_complete": sum(1 for r in sub if not r.get("passage_complete")),
                "ate_values": sorted(_ate(r) for r in sub if _ate(r) is not None),
            }
        )
    return {"threshold_m": USABILITY_ATE_M, "disclosure": DISCLOSURE, "rows": rows}


def per_sequence_system(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    keys = sorted({(r.get("order"), r.get("sequence"), r.get("system")) for r in records if r.get("set") != "integrity"}, key=lambda k: (k[0], str(k[2])))
    for order, seq, system in keys:
        sub = [r for r in records if r.get("set") != "integrity" and r.get("sequence") == seq and r.get("system") == system]
        closed = [r for r in sub if r.get("outcome") == "CLOSED"]
        completions = [r for r in closed if _executed_valid(r) and r.get("passage_complete")]
        out.append(
            {
                "order": order,
                "sequence": seq,
                "system": system,
                "planned_cells": len(sub),
                "executed_cells": len(closed),
                "evidence_valid_cells": sum(1 for r in closed if _executed_valid(r)),
                "completion_count": len(completions),
                "completion_over_planned": "{}/{}".format(len(completions), len(sub)),
                "completion_over_executed": "{}/{}".format(len(completions), len(closed)),
                "typed_failure_classes": sorted({str(r.get("typed_failure_class")) for r in closed if not r.get("passage_complete")}),
                "ate_translation_rmse_m": _distribution([_ate(r) for r in completions if _ate(r) is not None]),
                "byte_identity_vs_perturb1_recon": sorted(
                    str(((r.get("determinism") or {}).get("against_perturb1_recon_counterpart") or {}).get("status")) for r in closed
                ) if system in driver.RECOFF_SYSTEMS else None,
            }
        )
    return out


def _cell_rows(records: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in sorted(records, key=lambda x: (str(x.get("set")), x.get("order") or 0, str(x.get("system")), x.get("offset_frames") or 0)):
        metrics = r.get("metrics") or {}
        det = r.get("determinism") or {}
        rrp = r.get("resolved_recovery_parameter") or {}
        sd = r.get("single_delta_vs_recon_counterpart") or {}
        rows.append(
            {
                "set": r.get("set"),
                "order": r.get("order"),
                "dataset": r.get("dataset"),
                "sequence": r.get("sequence"),
                "system": r.get("system"),
                "recovery_ablated": r.get("recovery_ablated"),
                "offset_frames": r.get("offset_frames"),
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
                "recovery_ros_parameter_value": rrp.get("ros_parameter_value"),
                "recovery_yaml_value": rrp.get("yaml_value"),
                "recovery_effective_value": rrp.get("effective_value"),
                "recovery_event_line_count": ((r.get("perturbation_recovery_descriptive") or {}).get("event_line_count")),
                "determinism_vs_cdsc1r4": ((det.get("against_cdsc1r4") or {}).get("status")),
                "determinism_vs_perturb1_same_system": ((det.get("against_perturb1_same_system") or {}).get("status")),
                "determinism_vs_perturb1_recon_counterpart": ((det.get("against_perturb1_recon_counterpart") or {}).get("status")),
                "single_delta_status": sd.get("status"),
                "machine_verified_single_delta": sd.get("machine_verified_single_delta"),
                "resolved_parameters_sha256": r.get("resolved_parameters_sha256"),
                "config_sha256": (r.get("config_identity") or {}).get("sha256"),
                "launch_sha256": (r.get("launch_identity") or {}).get("sha256"),
                "binary_sha256": (r.get("binary_identity") or {}).get("sha256"),
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


def render_report(a: Mapping[str, Any]) -> str:
    L: List[str] = []
    L.append("# ABLATE-REC-1 aggregate report (generated {})".format(a["generated_utc"]))
    L.append("")
    L.append("Artifact root `{}`; prereg `{}` sha256 `{}`; tooling HEAD `{}`.".format(a["artifact_root"], a["prereg"]["path"], a["prereg"]["sha256"], a["tooling_git"]["head_sha"]))
    L.append("")
    acc = a["accounting"]
    L.append("## Run accounting")
    L.append("")
    L.append("| quantity | count |")
    L.append("|---|---:|")
    for k in ("planned", "recorded", "not_runnable", "executed_closed", "executed_valid", "executed_invalid_or_fatal", "unclosed_or_no_result", "completions_valid", "typed_failures_valid"):
        L.append("| {} | {} |".format(k, acc["total"][k]))
    L.append("| planned but not recorded | {} |".format(len(acc["total"]["planned_but_not_recorded"])))
    L.append("")
    L.append("By set: " + "; ".join("{}: planned {}, recorded {}, executed_valid {}, completions {}".format(k, v.get("planned"), v.get("recorded", 0), v.get("executed_valid", 0), v.get("completions_valid", 0)) for k, v in sorted(acc["by_set"].items())))
    L.append("By status: " + ", ".join("{}={}".format(k, v) for k, v in sorted(acc["by_status"].items())))
    L.append("")
    p3 = a["predictions"]["P3"]
    L.append("## P3 — integrity replicas")
    L.append("")
    L.append("Verdict **{}** (integrity gate {}).".format(p3["verdict"], p3["integrity_gate_verdict"]))
    for c in p3.get("cells") or []:
        L.append("- {} ({} {}): status {} / {}; vs PERTURB-1 {}; vs CDSC-1R4 {}".format(c.get("run_id"), c.get("system"), c.get("sequence"), c.get("status"), c.get("evidence_validity"), c.get("perturb1_status"), c.get("cdsc1r4_status")))
    L.append("")
    p1 = a["predictions"]["P1"]
    L.append("## P1 — recOFF on rotation.bag")
    L.append("")
    L.append("Verdict **{}**: cells meeting the P1 condition {}/{} executed VALID (planned {}); in the U0 failure family {}/{}; completing under threshold {}/{}.".format(
        p1["verdict"], p1["cells_meeting_condition"], p1["denominator_executed_valid"], p1["denominator_planned"], p1["cells_in_u0_failure_family"], p1["denominator_executed_valid"], p1["cells_completing_under_threshold"], p1["denominator_executed_valid"]))
    for s, v in p1["per_system"].items():
        L.append("- {}: executed VALID {}/{}, condition met {}/{}, in family {}/{}, completed under threshold {}/{}".format(s, v["executed_valid"], v["planned"], v["p1_condition_met"], v["planned"], v["in_family"], v["planned"], v["completed_under_threshold"], v["planned"]))
    L.append("")
    L.append("| cell | system | offset | status | validity | passage | ATE m | recovery lines | unsupported gaps | tail gap s | family | single-delta |")
    L.append("|---|---|---:|---|---|---|---:|---:|---|---:|---|---|")
    for x in p1["cells"]:
        sig = x["failure_signature"]
        fam = x["family"] or {}
        L.append("| {} | {} | {:+d} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            x["run_id"], x["system"], x["offset_frames"], x["status"], x["evidence_validity"], x["passage_complete"], _fmt(x["ate_translation_rmse_m"]), x["recovery_events_observed"],
            "; ".join("{:.3f}->{:.3f} ({:.3f}s)".format(g["start_timestamp_s"], g["end_timestamp_s"], g["duration_s"]) for g in sig["unsupported_state_gaps"]) or "none",
            _fmt(sig["tail_gap_s"], 3), fam.get("in_tracking_loss_or_unclosed_gap_family"), x["single_delta_verified"]))
    L.append("")
    ref = (p1.get("cdsc1r4_u0_reference") or {}).get("signature") or {}
    L.append("CDSC-1R4 U0 rotation.bag reference: status {}, passage {} ({}), unsupported gaps {}, tail gap {} s.".format(
        ref.get("status"), ref.get("passage_complete"), ref.get("passage_reason"),
        "; ".join("{:.3f}->{:.3f} ({:.3f}s)".format(g["start_timestamp_s"], g["end_timestamp_s"], g["duration_s"]) for g in ref.get("unsupported_state_gaps") or []) or "none", _fmt(ref.get("tail_gap_s"), 3)))
    L.append("")
    p2 = a["predictions"]["P2"]
    L.append("## P2 — switch inert where recovery never fired")
    L.append("")
    L.append("Verdict **{}**: byte-identical {}/{} reference cells recorded (planned reference set {}); mismatch {}; not comparable {}.".format(p2["verdict"], p2["byte_identical"], p2["denominator_reference_cells_recorded"], p2["denominator_planned_reference_set"], p2["mismatch"], p2["not_comparable"]))
    L.append("Single-delta verification over all recOFF cells: {}/{} machine-verified; not single-delta: {}; unverified: {}.".format(p2["single_delta"]["machine_verified_single_delta"], p2["single_delta"]["recoff_cells_recorded"], p2["single_delta"]["not_single_delta"] or "none", p2["single_delta"]["unverified"] or "none"))
    if p2["mismatch_cells"]:
        L.append("Mismatch cells: " + "; ".join(x["run_id"] for x in p2["mismatch_cells"]))
    L.append("Fired-counterpart cells (rotation.bag; descriptive, not in P2): " + "; ".join("{} -> {} ({})".format(x["run_id"], x["status"], x["byte_identity_status"]) for x in p2["fired_counterpart_cells_descriptive"]))
    L.append("")
    L.append("| cell | seq | system | offset | status | counterpart | counterpart activations | byte identity | single-delta |")
    L.append("|---|---|---|---:|---|---|---:|---|---|")
    for x in p2["cells"]:
        L.append("| {} | {} | {} | {:+d} | {} | {} | {} | {} | {} |".format(x["run_id"], x["sequence"], x["system"], x["offset_frames"], x["status"], x["counterpart_run_id"], x["counterpart_recovery_activations"], x["byte_identity_status"], x["single_delta_status"]))
    L.append("")
    u0 = a["u0_context"]
    L.append("## U0 context rows (rotation family)")
    L.append("")
    for seq, v in u0["per_sequence"].items():
        L.append("- {}: executed VALID {}/{}, completions {}, non-completions {} (classes {})".format(seq, v["executed_valid"], v["planned"], v["completions"], v["non_completions"], v["failure_classes"] or "none"))
    L.append("")
    L.append("| cell | seq | offset | status | validity | passage | class | ATE m | usable | unsupported gaps | tail gap s | vs CDSC-1R4 |")
    L.append("|---|---|---:|---|---|---|---|---:|---|---|---:|---|")
    for x in u0["cells"]:
        sig = x["failure_signature"]
        L.append("| {} | {} | {:+d} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(x["run_id"], x["sequence"], x["offset_frames"], x["status"], x["evidence_validity"], x["passage_complete"], x["typed_failure_class"], _fmt(x["ate_translation_rmse_m"]), x["kaist_usable_completion"],
            "; ".join("{:.3f}->{:.3f} ({:.3f}s)".format(g["start_timestamp_s"], g["end_timestamp_s"], g["duration_s"]) for g in sig["unsupported_state_gaps"]) or "none", _fmt(sig["tail_gap_s"], 3), x["determinism_vs_cdsc1r4"]))
    L.append("")
    fc = a["failure_class_comparison"]
    L.append("## Failure-class comparison: recOFF rotation.bag vs U0")
    L.append("")
    L.append("recOFF rotation.bag status set: {}; U0 context rotation.bag status set: {}; CDSC-1R4 U0 rotation.bag status: {}.".format(fc["recoff_status_set"], fc["u0_context_rotation_status_set"], fc["cdsc1r4_u0_rotation_signature"].get("status")))
    L.append("")
    L.append("| cell | system | offset | status | class | unsupported gaps | tail gap s | ATE m | in family | same status as CDSC-1R4 U0 | first gap delta vs U0 (start,end,dur s) |")
    L.append("|---|---|---:|---|---|---|---:|---:|---|---|---|")
    for x in fc["rows"]:
        fam = x["family"] or {}
        d = fam.get("first_unsupported_gap_vs_cdsc1r4_u0")
        L.append("| {} | {} | {:+d} | {} | {} | {} | {} | {} | {} | {} | {} |".format(x["cell"], x["system"], x["offset_frames"], x["status"], x["typed_failure_class"],
            "; ".join("{:.3f}->{:.3f} ({:.3f}s)".format(g["start_timestamp_s"], g["end_timestamp_s"], g["duration_s"]) for g in x["unsupported_gaps"] or []) or "none", _fmt(x["tail_gap_s"], 3), _fmt(x["ate_m"]), fam.get("in_tracking_loss_or_unclosed_gap_family"), fam.get("same_status_as_cdsc1r4_u0"),
            "—" if not d else "{:+.3f}, {:+.3f}, {:+.3f}".format(d["start_delta_s"], d["end_delta_s"], d["duration_delta_s"])))
    L.append("")
    it = a["interpretation"]
    L.append("## Interpretation table")
    L.append("")
    L.append("Applicable row: **{}** (basis: {}).".format(it.get("applies"), it.get("basis")))
    L.append("")
    L.append("Licensed claim (verbatim): \"{}\"".format(it.get("permitted_claim")))
    L.append("")
    us = a["usability"]
    L.append("## Usability threshold (prospective, KAIST cells)")
    L.append("")
    L.append(us["disclosure"])
    L.append("")
    L.append("| seq | system | executed | usable completions | complete but > {} m | complete, ATE unassessable | not complete | ATE values |".format(us["threshold_m"]))
    L.append("|---|---|---:|---:|---:|---:|---:|---|")
    for r in us["rows"]:
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(r["sequence"], r["system"], r["executed"], r["usable_completions"], r["complete_but_over_threshold"], r["complete_unassessable_ate"], r["not_complete"], ", ".join("{:.4f}".format(v) for v in r["ate_values"]) or "—"))
    L.append("")
    L.append("## Per sequence × system")
    L.append("")
    L.append("| order | seq | system | planned | executed | valid | completions/planned | completions/executed | classes | ATE median | IQR | n | byte identity vs PERTURB-1 recON |")
    L.append("|---:|---|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---|")
    for r in a["per_sequence_system"]:
        d = r["ate_translation_rmse_m"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(r["order"], r["sequence"], r["system"], r["planned_cells"], r["executed_cells"], r["evidence_valid_cells"], r["completion_over_planned"], r["completion_over_executed"], r["typed_failure_classes"] or "—", _fmt(d["median"]), _fmt(d["iqr"]), d["count"], r["byte_identity_vs_perturb1_recon"] or "—"))
    L.append("")
    L.append("Statistics language: counts and distributions only; no significance language (n per cell = 3).")
    return "\n".join(L) + "\n"


def aggregate_campaign(root: Path, output: Path) -> Dict[str, Any]:
    root = root.resolve(strict=True)
    if output.exists():
        raise AggregateError("refusing to overwrite aggregate output: {}".format(output))
    records = _load_records(root)
    rows = campaign.load_matrix(campaign.RuntimePaths().matrix)
    planned = driver.plan_cells(rows, list(driver.SETS))
    integrity_path = root / "integrity" / "INTEGRITY_GATE.json"
    integrity = campaign.load_json(integrity_path, "integrity gate")[0] if integrity_path.is_file() else {"verdict": "NOT_RUN"}
    reference = cdsc1r4_u0_reference()
    p1 = prediction_p1(records, reference)
    p2 = prediction_p2(records)
    p3 = prediction_p3(integrity)
    u0 = u0_context(records)
    aggregate: Dict[str, Any] = {
        "schema": SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "generated_utc": campaign.utc_now(),
        "artifact_root": str(root),
        "prereg": campaign.file_identity(driver.PREREG_FILE),
        "recovery_event_distribution": campaign.file_identity(driver.DISTRIBUTION_FILE),
        "tooling_git": campaign.git_identity(REPO_ROOT),
        "aggregator": campaign.file_identity(Path(__file__)),
        "integrity_gate": integrity,
        "accounting": accounting(records, planned),
        "predictions": {"P1": p1, "P2": p2, "P3": p3},
        "interpretation": interpretation(p1, p2, p3),
        "interpretation_table": INTERPRETATION_TABLE,
        "u0_context": u0,
        "failure_class_comparison": failure_class_comparison(p1, u0),
        "usability": usability(records),
        "per_sequence_system": per_sequence_system(records),
        "cells": _cell_rows(records),
        "statistics_language": "counts and distributions only; no significance language (n per cell = 3)",
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
            d = row["ate_translation_rmse_m"]
            for k in ("count", "median", "q1", "q3", "iqr"):
                flat["ate_translation_rmse_m_" + k] = d[k]
            summary_rows.append(flat)
        _write_csv(staging / "sequence_system_summary.csv", summary_rows, list(summary_rows[0].keys()) if summary_rows else ["order"])
        (staging / "ABLATION_REPORT.md").write_text(render_report(aggregate), encoding="utf-8")
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
        print("ABLATION_AGGREGATE_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("aggregate: {} records -> {} ; P1={} P2={} P3={} row={}".format(
        aggregate["accounting"]["total"]["recorded"], args.output,
        aggregate["predictions"]["P1"]["verdict"], aggregate["predictions"]["P2"]["verdict"], aggregate["predictions"]["P3"]["verdict"], aggregate["interpretation"].get("applies")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
