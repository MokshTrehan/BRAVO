#!/usr/bin/python3.8
"""BLACKOUT-1 aggregator (docs/icra27/BLACKOUT_PREREG.md amended; DECISIONS D7, D8, D11, D13).

Reads <root>/driver/cells.jsonl (append-only driver records) and the gate files,
derives per-cell rows, builds both arms' central tables (success and usable
completion vs k for recON/recOFF under the FROZEN passage rule and the
QUANTIZATION-AWARE reading, with attempt-reason histograms alongside),
computes the GLOBAL false-commit count, and evaluates B1', B2', B3, B4, B5
mechanically against the amended wording.  Counts with denominators; no
significance language.  Writes aggregate.json, cells.csv, REPORT.md, SHA256SUMS.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCHEMA = "schurvio.icra27.blackout.aggregate.v1"
CAMPAIGN_ID = "BLACKOUT-1"
DURATIONS = (2, 5, 10, 15, 20)
KAIST = ("circle/circle.bag", "infinite/infinite.bag", "square/square.bag")
EUROC = ("MH_05_difficult", "V2_02_medium")
SEQUENCES = KAIST + EUROC
USABILITY_THRESHOLD_M = 0.5
DISCLOSURE = (
    "Usability threshold (0.5 m, KAIST cells): set after CDSC-1R4 (n=1) was seen and before PERTURB-1; "
    "applied prospectively here; EuRoC usable completion = completion (the prereg names the threshold for KAIST only)."
)
EXPECTED_PREREG_RUNS = 109  # 50 arm A + 50 arm B + 6 U0 + 3 integrity (amended prereg)


def load_records(root: Path) -> List[Dict[str, Any]]:
    ledger = root / "driver" / "cells.jsonl"
    records: Dict[str, Dict[str, Any]] = {}
    if ledger.is_file():
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                records[r["run_id"]] = r  # last record per run id wins (records are published once)
    return list(records.values())


def f(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def derive(record: Mapping[str, Any]) -> Dict[str, Any]:
    rec = record.get("recovery") or {}
    gt = record.get("gt_metrics") or {}
    bm = record.get("blackout_metrics") or {}
    comp = record.get("completion") or {}
    metrics = record.get("metrics") or {}
    executed = record.get("outcome") == "CLOSED"
    valid = executed and record.get("evidence_validity") == "VALID"
    complete = bool(record.get("passage_complete")) and record.get("status") in ("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT")
    if (comp.get("quantization_aware") is None):
        complete_qa = complete  # un-masked replica: the frozen rule is the only reading
    else:
        complete_qa = bool(record.get("passage_complete_quantization_aware")) and record.get("status") in ("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT", "TRACKING_LOSS", "PARTIAL")
    # under the QA reading a TRACKING_LOSS/PARTIAL status caused only by the rounding gap counts as complete when passage_qa is true
    ate = f(metrics.get("ate_translation_rmse_m"))
    kaist = record.get("dataset") == "kaist_vio"
    usable = complete and (ate is not None and ate < USABILITY_THRESHOLD_M if kaist else True)
    commits = int(rec.get("commits") or 0)
    activations = int(rec.get("activations") or 0)
    failures = int(rec.get("failures") or 0)
    commit_errors = [c.get("commit_position_error_m") for c in (gt.get("commits") or [])]
    speeds = [((c.get("gt_speed_at_commit") or {}).get("instantaneous_mps")) for c in (gt.get("commits") or [])]
    role = record.get("role")
    recovered = role == "recON" and commits >= 1 and complete
    typed_termination = None
    if role == "recON" and executed and not recovered:
        if activations == 0:
            typed_termination = "NO_TRIGGER"
        elif commits == 0 and failures >= 1:
            typed_termination = "RECOVERY_FAILED_TYPED"
        elif commits == 0:
            typed_termination = "REACQUIRING_AT_END"  # attempts still running when input ended
        else:
            typed_termination = "COMMIT_BUT_NOT_COMPLETE"
    return {
        "run_id": record.get("run_id"),
        "set": record.get("set"),
        "dataset": record.get("dataset"),
        "sequence": record.get("sequence"),
        "system": record.get("system"),
        "role": role,
        "arm": record.get("arm"),
        "k": record.get("k_seconds"),
        "outcome": record.get("outcome"),
        "not_runnable_reason": record.get("not_runnable_reason"),
        "status": record.get("status"),
        "evidence_validity": record.get("evidence_validity"),
        "executed": executed,
        "valid": valid,
        "complete_frozen": complete,
        "complete_qa": complete_qa,
        "rounding_only_unsupported_gap": bool(record.get("rounding_only_unsupported_gap")),
        "passage_reason": record.get("passage_reason"),
        "typed_failure_class": record.get("typed_failure_class"),
        "ate_m": ate,
        "rpe_t_1m_m": f(metrics.get("rpe_translation_rmse_1m_m")),
        "rpe_r_1m_deg": f(metrics.get("rpe_rotation_rmse_1m_deg")),
        "usable": usable,
        "activations": activations,
        "commits": commits,
        "failures": failures,
        "attempts": int(rec.get("attempt_count") or 0),
        "histogram": rec.get("attempt_reason_histogram") or {},
        "consensus_rejects": int(rec.get("consensus_reject_count") or 0),
        "degraded_frames": int(rec.get("degraded_frame_count") or 0),
        "state_preserved": rec.get("state_preserved_on_every_attempt"),
        "supplied": rec.get("attempt_supplied_counts") or [],
        "valid_corr": rec.get("attempt_valid_counts") or [],
        "inliers": rec.get("attempt_inlier_counts") or [],
        "recovered": recovered,
        "typed_termination": typed_termination,
        "false_commits": int(gt.get("false_commit_count") or 0),
        "commit_error_unassessable": int(gt.get("commit_error_unassessable_count") or 0),
        "velocity_exposures": int(gt.get("velocity_exposure_count") or 0),
        "commit_errors_m": commit_errors,
        "commit_speeds_mps": speeds,
        "time_to_resume_s": bm.get("time_to_resume_s"),
        "time_to_recover_s": bm.get("time_to_recover_s"),
        "gap_closure": bm.get("gap_closure_success"),
        "gap_closure_qa": bm.get("gap_closure_success_quantization_aware"),
        "resumed_first_frame": bm.get("resumed_at_first_post_mask_frame"),
        "post_mask_err": ((gt.get("post_mask_under_pre_mask_alignment") or {}).get("position_error_m")),
        "post_recovery_rpe": gt.get("post_recovery_rpe_1s"),
        "single_delta": ((record.get("single_delta_vs_recon_counterpart") or record.get("single_delta_vs_frozen_s1_euroc") or {}).get("status")),
        "recovery_param": (record.get("resolved_recovery_parameter") or {}).get("effective_value"),
        "recovery_param_ok": (record.get("resolved_recovery_parameter") or {}).get("matches_expectation"),
        "determinism": {k: (v or {}).get("status") for k, v in (record.get("determinism") or {}).items() if isinstance(v, dict)},
        "wall_s": record.get("wall_seconds"),
        "study_check": bool(record.get("study_check_outside_denominators")),
    }


def hist_str(h: Mapping[str, Any]) -> str:
    return "; ".join("{}={}".format(k, v) for k, v in sorted(h.items())) if h else "—"


def cell_lookup(rows: Sequence[Mapping[str, Any]]) -> Dict[Tuple[str, str, int, str], Mapping[str, Any]]:
    out: Dict[Tuple[str, str, int, str], Mapping[str, Any]] = {}
    for r in rows:
        if r["arm"] in ("A", "B") and not r["study_check"] and r["k"] in DURATIONS:
            out[(r["sequence"], r["arm"], int(r["k"]), r["role"])] = r
    return out


def status_token(r: Optional[Mapping[str, Any]]) -> str:
    if r is None:
        return "—"
    if r["outcome"] == "NOT_RUNNABLE":
        return "NR"
    if not r["executed"]:
        return "UNCLOSED"
    if not r["valid"]:
        return "INVALID"
    return "C" if r["complete_frozen"] else "F:" + str(r["status"])


def arm_table(rows: Sequence[Mapping[str, Any]], arm: str) -> List[Dict[str, Any]]:
    look = cell_lookup(rows)
    out = []
    for seq in SEQUENCES:
        for k in DURATIONS:
            on = look.get((seq, arm, k, "recON"))
            off = look.get((seq, arm, k, "recOFF"))
            u0 = look.get((seq, arm, k, "U0"))
            out.append({
                "sequence": seq,
                "arm": arm,
                "k": k,
                "recON": None if on is None else {
                    "run_id": on["run_id"], "status": status_token(on), "outcome": on["outcome"], "not_runnable_reason": on["not_runnable_reason"],
                    "complete_frozen": on["complete_frozen"], "complete_qa": on["complete_qa"], "usable": on["usable"], "ate_m": on["ate_m"],
                    "recovered": on["recovered"], "activations": on["activations"], "commits": on["commits"], "failures": on["failures"],
                    "attempts": on["attempts"], "histogram": on["histogram"], "consensus_rejects": on["consensus_rejects"],
                    "false_commits": on["false_commits"], "velocity_exposures": on["velocity_exposures"], "commit_errors_m": on["commit_errors_m"],
                    "time_to_recover_s": on["time_to_recover_s"], "time_to_resume_s": on["time_to_resume_s"], "typed_termination": on["typed_termination"],
                    "supplied": on["supplied"], "inliers": on["inliers"], "post_mask_err": on["post_mask_err"], "post_recovery_rpe": on["post_recovery_rpe"],
                    "state_preserved": on["state_preserved"], "degraded_frames": on["degraded_frames"],
                },
                "recOFF": None if off is None else {
                    "run_id": off["run_id"], "status": status_token(off), "outcome": off["outcome"], "not_runnable_reason": off["not_runnable_reason"],
                    "complete_frozen": off["complete_frozen"], "complete_qa": off["complete_qa"], "rounding_only": off["rounding_only_unsupported_gap"],
                    "usable": off["usable"], "ate_m": off["ate_m"], "typed_failure_class": off["typed_failure_class"], "passage_reason": off["passage_reason"],
                    "time_to_resume_s": off["time_to_resume_s"], "resumed_first_frame": off["resumed_first_frame"], "post_mask_err": off["post_mask_err"],
                    "single_delta": off["single_delta"],
                },
                "U0": None if u0 is None else {
                    "run_id": u0["run_id"], "status": status_token(u0), "complete_frozen": u0["complete_frozen"], "complete_qa": u0["complete_qa"],
                    "rounding_only": u0["rounding_only_unsupported_gap"], "usable": u0["usable"], "ate_m": u0["ate_m"], "typed_failure_class": u0["typed_failure_class"],
                    "time_to_resume_s": u0["time_to_resume_s"], "post_mask_err": u0["post_mask_err"],
                },
            })
    return out


def accounting(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    denom = [r for r in rows if not r["study_check"] and r["set"] != "step0"]
    by_reason: Dict[str, int] = {}
    for r in denom:
        if r["outcome"] == "NOT_RUNNABLE":
            key = str(r["not_runnable_reason"]).split(":")[0]
            by_reason[key] = by_reason.get(key, 0) + 1
    per_set: Dict[str, Dict[str, int]] = {}
    for r in rows:
        s = per_set.setdefault(r["set"], {"recorded": 0, "not_runnable": 0, "executed": 0, "valid": 0, "complete_frozen": 0, "complete_qa": 0, "typed_failures": 0, "invalid": 0})
        s["recorded"] += 1
        if r["outcome"] == "NOT_RUNNABLE":
            s["not_runnable"] += 1
        if r["executed"]:
            s["executed"] += 1
            if r["valid"]:
                s["valid"] += 1
                if r["complete_frozen"]:
                    s["complete_frozen"] += 1
                else:
                    s["typed_failures"] += 1
                if r["complete_qa"]:
                    s["complete_qa"] += 1
            else:
                s["invalid"] += 1
    return {
        "expected_prereg_runs": EXPECTED_PREREG_RUNS,
        "expected_breakdown": {"arm_A": 50, "arm_B": 50, "u0_context": 6, "integrity": 3},
        "extra_records_outside_denominators": {"step0_inertness": sum(1 for r in rows if r["set"] == "step0"), "euroc_study_check": sum(1 for r in rows if r["study_check"])},
        "recorded_in_denominators": len(denom),
        "not_runnable": sum(1 for r in denom if r["outcome"] == "NOT_RUNNABLE"),
        "not_runnable_by_reason": by_reason,
        "not_runnable_cells": [{"run_id": r["run_id"], "reason": r["not_runnable_reason"]} for r in denom if r["outcome"] == "NOT_RUNNABLE"],
        "executed": sum(1 for r in denom if r["executed"]),
        "executed_valid": sum(1 for r in denom if r["valid"]),
        "executed_invalid_or_fatal": sum(1 for r in denom if r["executed"] and not r["valid"]),
        "unclosed": sum(1 for r in denom if r["outcome"] not in ("CLOSED", "NOT_RUNNABLE")),
        "planned_but_not_recorded": max(0, EXPECTED_PREREG_RUNS - len(denom)),
        "completions_frozen": sum(1 for r in denom if r["valid"] and r["complete_frozen"]),
        "completions_quantization_aware": sum(1 for r in denom if r["valid"] and r["complete_qa"]),
        "typed_failures_frozen": sum(1 for r in denom if r["valid"] and not r["complete_frozen"]),
        "by_status": _count(denom, "status"),
        "per_set": per_set,
    }


def _count(rows: Sequence[Mapping[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return dict(sorted(out.items()))


def verdicts(rows: Sequence[Mapping[str, Any]], gates: Mapping[str, Any]) -> Dict[str, Any]:
    look = cell_lookup(rows)
    recon = [r for r in rows if r["role"] == "recON" and r["arm"] in ("A", "B") and r["executed"] and not r["study_check"] and r["k"] in DURATIONS]
    recon_valid = [r for r in recon if r["valid"]]
    # ---- B5
    all_commits = sum(r["commits"] for r in recon)
    false_commits = sum(r["false_commits"] for r in recon)
    unassessable = sum(r["commit_error_unassessable"] for r in recon)
    velocity = sum(r["velocity_exposures"] for r in recon)
    non_recovery = [r for r in recon if not r["recovered"]]
    untyped = [r for r in non_recovery if r["typed_termination"] not in ("RECOVERY_FAILED_TYPED", "NO_TRIGGER") or r["state_preserved"] is False]
    b5 = {
        "prediction": "ZERO recovery commits in any unrecoverable situation across the entire matrix; every non-recovery terminates typed with the live state preserved",
        "false_commit_definition": "commit position mapped through the cell's own pre-mask SE(3) alignment > 0.5 m from GT at the commit time (D8)",
        "global_commit_count": all_commits,
        "global_false_commit_count": false_commits,
        "commit_error_unassessable_count": unassessable,
        "velocity_exposure_events": velocity,
        "executed_recon_cells": len(recon),
        "non_recovery_cells": len(non_recovery),
        "non_recovery_typed_with_state_preserved": len(non_recovery) - len(untyped),
        "non_recovery_untyped_or_state_mutated": [{"run_id": r["run_id"], "typed_termination": r["typed_termination"], "state_preserved": r["state_preserved"]} for r in untyped],
        "verdict": "TRUE" if (false_commits == 0 and not untyped and recon) else ("FALSE" if false_commits > 0 or untyped else "NO_DATA"),
    }
    # ---- B1' arm A / arm B
    arm_a = [r for r in recon if r["arm"] == "A"]
    arm_b = [r for r in recon if r["arm"] == "B"]
    b1_a = {
        "prediction": "arm A (mid-motion) recON cells predicted to FAIL recovery",
        "executed": len(arm_a),
        "recovered_commit_and_complete": sum(1 for r in arm_a if r["recovered"]),
        "commit_count_cells": sum(1 for r in arm_a if r["commits"] >= 1),
        "typed_recovery_failed": sum(1 for r in arm_a if r["typed_termination"] == "RECOVERY_FAILED_TYPED"),
        "no_trigger": sum(1 for r in arm_a if r["typed_termination"] == "NO_TRIGGER"),
        "recovered_cells": [r["run_id"] for r in arm_a if r["recovered"]],
        "verdict": ("CONSISTENT (0 recoveries)" if arm_a and not any(r["recovered"] for r in arm_a) else ("COUNTER-EVIDENCE: {} of {} arm-A recON cells recovered".format(sum(1 for r in arm_a if r["recovered"]), len(arm_a)) if arm_a else "NO_DATA")),
    }
    per_seq_b: Dict[str, Any] = {}
    for seq in SEQUENCES:
        ks = {}
        for k in DURATIONS:
            r = look.get((seq, "B", k, "recON"))
            ks[str(k)] = None if r is None else ("NR" if r["outcome"] == "NOT_RUNNABLE" else ("recovered" if r["recovered"] else ("commit_not_complete" if r["commits"] else "not_recovered")))
        tested = [k for k in DURATIONS if ks[str(k)] not in (None, "NR")]
        succ = [k for k in tested if ks[str(k)] == "recovered"]
        # K per sequence: largest k such that every tested k' <= k succeeded (no extrapolation over untested k)
        K = 0
        for k in tested:
            if ks[str(k)] == "recovered":
                K = k
            else:
                break
        per_seq_b[seq] = {"by_k": ks, "tested_k": tested, "successful_k": succ, "K_monotone_prefix_s": K, "K_max_success_s": max(succ) if succ else 0, "ceiling_k": [k for k in tested if ks[str(k)] != "recovered"]}
    per_dataset_K = {}
    for name, seqs in (("kaist_vio", KAIST), ("euroc_mav", EUROC)):
        tested_any = [s for s in seqs if per_seq_b[s]["tested_k"]]
        per_dataset_K[name] = {
            "sequences_with_tested_cells": tested_any,
            "K_s_all_tested_sequences": (min(per_seq_b[s]["K_monotone_prefix_s"] for s in tested_any) if tested_any else None),
            "K_per_sequence": {s: per_seq_b[s]["K_monotone_prefix_s"] for s in seqs},
        }
    b1_b = {
        "prediction": "arm B (low-motion) recON cells recoverable at small k, degrading as k grows; full curve reported",
        "executed": len(arm_b),
        "recovered": sum(1 for r in arm_b if r["recovered"]),
        "per_sequence": per_seq_b,
        "K_per_dataset": per_dataset_K,
    }
    # ---- B2'
    sep = []
    for (seq, arm, k, role), r in look.items():
        if role != "recON":
            continue
        off = look.get((seq, arm, k, "recOFF"))
        if off is None or not r["executed"] or not off["executed"]:
            continue
        if r["complete_frozen"] and not off["complete_frozen"]:
            family = off["status"] == "TRACKING_LOSS" or (off["passage_reason"] in ("UNSUPPORTED_POST_INITIALIZATION_STATE_GAP", "FINAL_STATE_DOES_NOT_REACH_SELECTED_INPUT_TAIL"))
            sep.append({"sequence": seq, "arm": arm, "k": k, "recOFF_status": off["status"], "recOFF_reason": off["passage_reason"], "in_family": bool(family), "rounding_only": off["rounding_only_unsupported_gap"], "recOFF_complete_qa": off["complete_qa"], "survives_quantization_aware": bool(r["complete_qa"] and not off["complete_qa"])})
    b2 = {
        "prediction": "wherever recON completes and recOFF does not, recOFF's failure is in the tracking-loss/unclosed-gap family",
        "separation_cells_frozen": len(sep),
        "in_family": sum(1 for s in sep if s["in_family"]),
        "rounding_only_separations": sum(1 for s in sep if s["rounding_only"]),
        "separations_surviving_quantization_aware": sum(1 for s in sep if s["survives_quantization_aware"]),
        "cells": sep,
        "verdict_frozen": ("TRUE ({}/{})".format(sum(1 for s in sep if s["in_family"]), len(sep)) if sep and all(s["in_family"] for s in sep) else ("FALSE" if sep else "VACUOUS (no separation cell)")),
        "verdict_quantization_aware": ("{} separation cells survive; {} are rounding-only".format(sum(1 for s in sep if s["survives_quantization_aware"]), sum(1 for s in sep if s["rounding_only"])) if sep else "VACUOUS (no separation cell)"),
    }
    # ---- B4 ceilings
    ceilings = []
    for r in sorted(recon, key=lambda r: (r["arm"], r["sequence"], r["k"])):
        if not r["recovered"]:
            ceilings.append({"sequence": r["sequence"], "arm": r["arm"], "k": r["k"], "status": r["status"], "activations": r["activations"], "commits": r["commits"], "failures": r["failures"], "attempts": r["attempts"], "histogram": r["histogram"], "typed_termination": r["typed_termination"], "supplied": r["supplied"], "inliers": r["inliers"], "complete_frozen": r["complete_frozen"]})
    b4 = {"prediction": "every (sequence, k) where recON fails to recover is reported as a measured limitation with counts", "recon_non_recovery_cells": len(ceilings), "of_executed_recon": len(recon), "cells": ceilings}
    # ---- B3
    b3 = {
        "prediction": "all integrity replicas byte-identical to their recorded references (plus the k=0 masking-inertness replica)",
        "inertness_gate": (gates.get("inertness") or {}).get("verdict"),
        "integrity_gate": (gates.get("integrity") or {}).get("verdict"),
        "cells": {name: (g or {}).get("cells") for name, g in gates.items()},
        "verdict": "TRUE" if (gates.get("inertness") or {}).get("verdict") == "PASS" and (gates.get("integrity") or {}).get("verdict") == "PASS" else ("FALSE" if gates else "NO_DATA"),
    }
    return {"B1_prime_arm_A": b1_a, "B1_prime_arm_B": b1_b, "B2_prime": b2, "B3": b3, "B4": b4, "B5": b5}


def licensed_claims(v: Mapping[str, Any], acc: Mapping[str, Any]) -> Dict[str, Any]:
    b1b = v["B1_prime_arm_B"]
    b5 = v["B5"]
    b2 = v["B2_prime"]
    claims: Dict[str, Any] = {}
    kaist_K = (b1b["K_per_dataset"]["kaist_vio"] or {}).get("K_s_all_tested_sequences")
    per_seq = b1b["K_per_dataset"]["kaist_vio"]["K_per_sequence"]
    any_success = any(per_seq_v > 0 for per_seq_v in per_seq.values())
    if any_success:
        claims["arm_B_sentence"] = (
            "Recovery closes low-motion masked outages up to K s on the tested sequences (KAIST): "
            + ", ".join("{} K={} s".format(s.split("/")[-1], per_seq[s]) for s in KAIST if b1b["per_sequence"][s]["tested_k"])
            + " (K = largest tested k with every smaller tested k also recovered; no extrapolation beyond tested k; ceilings listed under B4)."
        )
    else:
        claims["arm_B_sentence"] = "No arm-B positive claim: recovery closed no low-motion masked outage on the tested sequences (see B4)."
    claims["euroc_sentence"] = (
        "EuRoC: no positive claim; the declared study configuration (frozen config + long_gap_recovery_enabled=true) is refused by the "
        "frozen executable's compiled contract (fixed camera calibration required; frozen EuRoC config performs online calibration) — a scope boundary, not a masking outcome."
    )
    n_arm_a = v["B1_prime_arm_A"]["executed"]
    if b5["verdict"] == "TRUE":
        claims["arm_A_sentence"] = (
            "Under mid-motion masked outages the mechanism produced {} inconsistent re-anchors across {} recON cells ({} commits in total, all within 0.5 m of GT under the pre-mask alignment); "
            "every non-recovery terminated typed (attempt-reason histograms per cell) with the live state preserved. Arm-A recoveries: {} of {} cells (reported as measured, see B1')."
        ).format(b5["global_false_commit_count"], n_arm_a, b5["global_commit_count"], v["B1_prime_arm_A"]["recovered_commit_and_complete"], n_arm_a)
    else:
        claims["arm_A_sentence"] = "B5 not TRUE ({}): {} false commit(s) / {} untyped non-recovery cell(s); no gate-integrity claim is licensed.".format(b5["verdict"], b5["global_false_commit_count"], len(b5["non_recovery_untyped_or_state_mutated"]))
    claims["separation_sentence"] = (
        "recON-vs-recOFF separation: {} cells under the frozen passage rule ({} in the tracking-loss/unclosed-gap family), of which {} survive the quantization-aware reading and {} are rounding-only (D13)."
    ).format(b2["separation_cells_frozen"], b2["in_family"], b2["separations_surviving_quantization_aware"], b2["rounding_only_separations"])
    claims["scope_sentence"] = (
        "Scope (stated with any success): recovery under masking succeeds only where the pre-mask -> post-mask frame pair is KLT-bridgeable "
        "(correspondences form by persistent tracker-ID matching against retained SLAM landmarks); it is a state-stall re-anchor under track survival, "
        "not general blackout relocalization; claims are limited to the tested durations, the two deterministic injection rules and the tested sequences."
    )
    return claims


def render_report(agg: Mapping[str, Any]) -> str:
    L: List[str] = ["# BLACKOUT-1 aggregate report", "", "Generated {}. {}".format(agg["generated_utc"], DISCLOSURE), ""]
    v = agg["verdicts"]
    L += ["## Global false-commit count (B5)", "", "| commits (all executed recON cells) | FALSE commits (>0.5 m) | unassessable | velocity-exposure events (>0.3 m/s) | non-recovery cells typed+state preserved | verdict |", "|---:|---:|---:|---:|---|---|",
          "| {} | **{}** | {} | {} | {}/{} | **{}** |".format(v["B5"]["global_commit_count"], v["B5"]["global_false_commit_count"], v["B5"]["commit_error_unassessable_count"], v["B5"]["velocity_exposure_events"], v["B5"]["non_recovery_typed_with_state_preserved"], v["B5"]["non_recovery_cells"], v["B5"]["verdict"]), ""]
    for arm in ("A", "B"):
        L += ["## Arm {} central table".format(arm), "", "Status tokens: C = complete (frozen passage rule), F:<status> = typed non-completion, NR = NOT_RUNNABLE. QA = quantization-aware reading (D13). recOFF 'resume' = seconds from mask end to first state row.", "",
              "| seq | k | recON | recON QA | usable | ATE m | act/commit/fail | attempts | reason histogram | t_recover s | commit err m | |v| commit | recOFF | recOFF QA | rounding-only | recOFF resume s | recOFF post-mask err (first/med/max m) | U0 |", "|---|---:|---|---|---|---:|---|---:|---|---:|---|---|---|---|---|---:|---|---|"]
        for row in agg["arm_tables"][arm]:
            on, off, u0 = row["recON"], row["recOFF"], row["U0"]
            def fmt(x, p=3):
                return "—" if x is None else ("{:.%df}" % p).format(x)
            pm = (off or {}).get("post_mask_err") or {}
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row["sequence"].split("/")[-1], row["k"],
                "—" if on is None else on["status"], "—" if on is None else ("C" if on["complete_qa"] else "F"),
                "—" if on is None else ("Y" if on["usable"] else "N"), "—" if on is None else fmt(on["ate_m"]),
                "—" if on is None else "{}/{}/{}".format(on["activations"], on["commits"], on["failures"]), "—" if on is None else on["attempts"],
                "—" if on is None else hist_str(on["histogram"]), "—" if on is None else fmt(on["time_to_recover_s"]),
                "—" if on is None else ", ".join(fmt(e) for e in on["commit_errors_m"]) or "—",
                "—" if on is None else ", ".join(fmt(s, 2) for s in on.get("commit_speeds_mps", []) if s is not None) or "—",
                "—" if off is None else off["status"], "—" if off is None else ("C" if off["complete_qa"] else "F"), "—" if off is None else ("Y" if off["rounding_only"] else "N"),
                "—" if off is None else fmt(off["time_to_resume_s"]),
                "—" if not pm else "{:.3f}/{:.3f}/{:.3f}".format(pm["first"], pm["median"], pm["max"]),
                "—" if u0 is None else "{} (QA {})".format(u0["status"], "C" if u0["complete_qa"] else "F"),
            ))
        L.append("")
    L += ["## Verdicts", "", "```", json.dumps({k: {kk: vv for kk, vv in val.items() if kk not in ("cells", "per_sequence")} for k, val in v.items()}, indent=1, sort_keys=True), "```", ""]
    L += ["## Licensed claim sentences (constructed mechanically)", ""] + ["- {}".format(s) for s in agg["licensed_claims"].values()] + [""]
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    root = args.artifact_root.resolve(strict=True)
    records = load_records(root)
    rows = [derive(r) for r in records]
    gates = {}
    for name in ("inertness", "integrity"):
        path = root / "integrity" / "{}_GATE.json".format(name.upper())
        if path.is_file():
            gates[name] = json.loads(path.read_text())
    v = verdicts(rows, gates)
    acc = accounting(rows)
    agg = {
        "schema": SCHEMA,
        "campaign_id": CAMPAIGN_ID,
        "generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifact_root": str(root),
        "record_count": len(rows),
        "accounting": acc,
        "gates": gates,
        "arm_tables": {"A": arm_table(rows, "A"), "B": arm_table(rows, "B")},
        "verdicts": v,
        "licensed_claims": licensed_claims(v, acc),
        "usability_disclosure": DISCLOSURE,
        "cells": rows,
    }
    # add commit speeds into arm table recON entries
    for arm in ("A", "B"):
        for row in agg["arm_tables"][arm]:
            if row["recON"]:
                r = next(x for x in rows if x["run_id"] == row["recON"]["run_id"])
                row["recON"]["commit_speeds_mps"] = r["commit_speeds_mps"]
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    (out / "aggregate.json").write_text(json.dumps(agg, allow_nan=False, indent=1, sort_keys=True) + "\n")
    with (out / "cells.csv").open("w", newline="") as stream:
        keys = ["run_id", "set", "dataset", "sequence", "system", "role", "arm", "k", "outcome", "status", "evidence_validity", "complete_frozen", "complete_qa", "rounding_only_unsupported_gap", "usable", "ate_m", "activations", "commits", "failures", "attempts", "false_commits", "velocity_exposures", "time_to_resume_s", "time_to_recover_s", "typed_termination", "typed_failure_class", "single_delta", "not_runnable_reason"]
        w = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    (out / "REPORT.md").write_text(render_report(agg))
    lines = []
    for path in sorted(out.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append("{}  {}".format(hashlib.sha256(path.read_bytes()).hexdigest(), path.relative_to(out).as_posix()))
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n")
    print("aggregate: {} records; B5 {} (false commits {}); output {}".format(len(rows), v["B5"]["verdict"], v["B5"]["global_false_commit_count"], out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
