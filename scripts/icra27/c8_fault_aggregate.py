#!/usr/bin/env python3
"""C8 Item 1 aggregator: H4 fault matrix + natural-incident reconciliation.

Inputs (read-only): a C8 artifact root written by c8_fault_driver.py, and the
campaign artifact roots for the natural-incident scrape.
Outputs (--output dir): fault_matrix.json, fault_matrix.csv, inertness.csv,
class7.csv, natural_incidents.csv, FAULT_MATRIX.md, SHA256SUMS.

Every count carries its denominator. No significance language.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ANSI = re.compile(rb"\x1b\[[0-9;]*m")
CLASS_NAMES = {
    1: "invalid_factor_dimensions", 2: "nonfinite_factor_values", 3: "rank_failure",
    4: "innovation_factorization_failure", 5: "negative_posterior_diagonal",
    6: "prior_changed_between_preview_and_commit", 7: "logging_diagnostics_failure",
    8: "feature_factor_failure_after_proposal",
}
# typed console evidence expected per class (regex on ANSI-stripped console)
TYPED_PATTERNS = {
    1: re.compile(rb"\[MSCKF-SCHUR\]: feature=\d+ pass=1 rows=\d+ status=nonfinite stage=input_dimensions"),
    2: re.compile(rb"\[MSCKF-SCHUR\]: feature=\d+ pass=1 rows=\d+ status=nonfinite stage=raw_inputs"),
    3: re.compile(rb"\[MSCKF-SCHUR\]: feature=\d+ pass=1 rows=\d+ status=rank_deficient stage=numerical_rank"),
    4: re.compile(rb"\[MSCKF-PREFLIGHT\]: status=factorization_failed stage=innovation_factorization"),
    5: re.compile(rb"\[MSCKF-PREFLIGHT\]: status=negative_diagonal stage=posterior_diagonal"),
    6: re.compile(rb"\[MSCKF-PRIOR\]: status=\w+ stage=precommit"),
    8: re.compile(rb"\[MSCKF-COMMIT\]: status=invalid_precomputed_update live_writes=0"),
}
NATURAL_PREFIXES = [
    ("[MSCKF-PREFLIGHT]", "preview rejection (nonfinite/factorization/negative diagonal/invalid input)"),
    ("[MSCKF-SCHUR]", "per-feature Schur reduction rejection (nonfinite/rank/conditioning)"),
    ("[MSCKF-REDUCTION]: feature", "nonfinite reduction statistics"),
    ("[MSCKF-COMMIT]", "rejected precomputed commit"),
    ("[MSCKF-PRIOR]", "prior changed between snapshot and commit"),
    ("[MSCKF-CP2]", "CP2 recorded-path failure"),
    ("[MSCKF-CP2-SHADOW]", "CP2 shadow exception"),
    ("[MSCKF-CP2-OBSERVER]", "CP2 observer callback exception"),
    ("[TURNSAFE-T0]", "T0 diagnostics stream status"),
    ("StateHelper::EKFUpdate() - diagonal at", "legacy EKFUpdate negative-diagonal hard exit"),
    ("[MSCKF-GATE]", "chi-square gate rejection (ordinary, not a numerical typed failure)"),
]
CAMPAIGN_ROOTS = {
    "CDSC-1R4": "/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z",
    "PERTURB-1": "/home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z",
    "ABLATE-REC-1": "/home/moksh/schurvio-icra27-artifacts/recovery-ablation/ablate1-20260817T011231Z",
    "BLACKOUT-1": "/home/moksh/schurvio-icra27-artifacts/blackout-campaign/blackout1-20260817T130445Z",
}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def strip(b: bytes) -> bytes:
    return ANSI.sub(b"", b)


def load_runs(root: Path) -> List[Dict[str, Any]]:
    runs = []
    for rr in sorted(root.glob("cells/*/*/S1/*/run_record.json")):
        rec = json.load(open(rr))
        rec["_dir"] = rr.parent
        rec["_tag"] = rr.parent.parent.parent.parent.name
        runs.append(rec)
    return runs


def shim_records(run_dir: Path) -> List[Dict[str, Any]]:
    p = run_dir / "diagnostics" / "shim.jsonl"
    out = []
    if not p.is_file():
        return out
    with open(p) as f:
        for line in f:
            if line.startswith('{"kind":"invocation"'):
                out.append(json.loads(line))
    return out


def console_typed_counts(run_dir: Path) -> Dict[str, int]:
    p = run_dir / "diagnostics" / "console.log"
    counts: Dict[str, int] = collections.Counter()
    if not p.is_file():
        return counts
    data = strip(p.read_bytes())
    for cls, pat in TYPED_PATTERNS.items():
        counts["class{}".format(cls)] = len(pat.findall(data))
    for prefix, _ in NATURAL_PREFIXES:
        counts[prefix] = data.count(prefix.encode())
    counts["EKFUpdate_hard_exit"] = data.count(b"StateHelper::EKFUpdate() - diagonal at")
    counts["has_died_after_normal_end"] = data.count(b"has died")
    return counts


def cycle_matrix(runs: List[Dict[str, Any]]) -> Dict[str, Any]:
    per_class: Dict[int, Dict[str, Any]] = {c: collections.Counter() for c in (1, 2, 3, 4, 5, 6, 8)}
    per_run_rows = []
    all_invocations = 0
    all_eligible = 0
    all_multi_commit = 0
    all_threw = 0
    all_commit_calls_gt1 = 0
    violations: List[Dict[str, Any]] = []
    for r in runs:
        recs = shim_records(r["_dir"])
        typed = console_typed_counts(r["_dir"])
        n_inv = len(recs)
        n_elig = sum(1 for x in recs if x["features_in"] > 0)
        all_invocations += n_inv
        all_eligible += n_elig
        row = {"run_id": r["run_id"], "dataset": r["dataset"], "sequence": r["sequence"], "invocations": n_inv,
               "eligible_invocations": n_elig, "exit_code": r["estimator"]["exit_code"],
               "console_EKFUpdate_hard_exit": typed.get("EKFUpdate_hard_exit", 0)}
        for x in recs:
            if x["commit_true"] > 1:
                all_multi_commit += 1
                violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "multiple_commits_in_one_callback", "record": x})
            if x["commit_calls"] > 1:
                all_commit_calls_gt1 += 1
            if x["threw"]:
                all_threw += 1
                violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "exception_escaped_update", "record": x})
            c = x["class"]
            if c not in per_class:
                continue
            pc = per_class[c]
            pc["scheduled"] += 1
            reached = False
            realized = bool(x["fault_triggered"])
            if c in (1, 2, 3):
                reached = bool(x["factor_injected"])
            elif c == 4:
                reached = bool(x.get("snapshot_corrupt_injected"))
            elif c == 5:
                reached = x["preview_calls"] > 0
            elif c == 6:
                reached = bool(x["prior_change_injected"])
            elif c == 8:
                reached = bool(x["commit_corrupt_injected"])
            pc["reached_injection_point"] += int(reached)
            pc["fault_realized"] += int(realized)
            if realized:
                # oracle checks
                if c in (4, 5, 6, 8):
                    if x["commit_true"] != 0:
                        pc["viol_commit_after_realized_transaction_fault"] += 1
                        violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "commit_after_realized_transaction_fault", "class": c, "record": x})
                    if not x["state_unchanged_vs_presented"]:
                        pc["viol_state_mutated_without_commit"] += 1
                        violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "state_mutated_without_commit", "class": c, "record": x})
                    if x.get("restored_by_harness") and not x.get("restored_equals_original"):
                        pc["viol_restore_mismatch"] += 1
                        violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "harness_restore_mismatch", "class": c, "record": x})
                    if x.get("terminal_status") == "committed_counted" or x.get("commit_occurred"):
                        pc["viol_event_reports_commit"] += 1
                        violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "event_reports_commit_after_realized_fault", "class": c, "record": x})
                else:
                    # feature-level: the injected factor must not be accepted; transaction may proceed with others
                    if x["commit_true"] > 1:
                        pc["viol_multi_commit"] += 1
                    # if the transaction did not commit, state must be unchanged
                    if x["commit_true"] == 0 and not x["state_unchanged_vs_presented"]:
                        pc["viol_state_mutated_without_commit"] += 1
                        violations.append({"run_id": r["run_id"], "invocation": x["invocation"], "type": "state_mutated_without_commit", "class": c, "record": x})
                    if x["commit_true"] == 1 and x["state_unchanged_vs_presented"]:
                        pc["note_commit_with_unchanged_hash"] += 1
                pc["realized_with_commit_of_other_features"] += int(x["commit_true"] == 1 and c in (1, 2, 3))
                pc["realized_no_commit"] += int(x["commit_true"] == 0)
            if x.get("containment_restore"):
                pc["ineffective_containment_restore"] += 1
            if x["threw"]:
                pc["viol_exception"] += 1
        # per-run typed console tallies per class
        for c in per_class:
            per_class[c]["typed_console_lines"] += typed.get("class{}".format(c), 0)
        row.update({"typed_" + str(c): typed.get("class{}".format(c), 0) for c in per_class})
        per_run_rows.append(row)
    matrix = []
    for c in sorted(per_class):
        pc = per_class[c]
        matrix.append({
            "class": c, "class_name": CLASS_NAMES[c],
            "scheduled": pc["scheduled"], "reached_injection_point": pc["reached_injection_point"],
            "fault_realized": pc["fault_realized"],
            "realized_no_commit": pc["realized_no_commit"],
            "realized_with_commit_of_other_features": pc["realized_with_commit_of_other_features"],
            "typed_console_lines": pc["typed_console_lines"],
            "ineffective_containment_restore": pc["ineffective_containment_restore"],
            "viol_state_mutated_without_commit": pc["viol_state_mutated_without_commit"],
            "viol_commit_after_realized_transaction_fault": pc["viol_commit_after_realized_transaction_fault"],
            "viol_event_reports_commit": pc["viol_event_reports_commit"],
            "viol_restore_mismatch": pc["viol_restore_mismatch"],
            "viol_multi_commit": pc["viol_multi_commit"],
            "viol_exception": pc["viol_exception"],
        })
    return {"matrix": matrix, "per_run": per_run_rows, "violations": violations,
            "totals": {"invocations": all_invocations, "eligible_invocations": all_eligible,
                       "multi_commit_callbacks": all_multi_commit, "commit_calls_gt1_callbacks": all_commit_calls_gt1,
                       "exceptions_escaped": all_threw, "runs": len(runs)}}


def inertness_table(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for r in runs:
        recs_n = 0
        p = r["_dir"] / "diagnostics" / "shim.jsonl"
        if p.is_file():
            with open(p) as f:
                recs_n = sum(1 for line in f if line.startswith('{"kind":"invocation"'))
        rows.append({"run_id": r["run_id"], "dataset": r["dataset"], "sequence": r["sequence"],
                     "status": r["reference_comparison"].get("status"),
                     "state_estimate_identical": r["reference_comparison"].get("trajectory/state_estimate.txt", {}).get("byte_identical"),
                     "state_deviation_identical": r["reference_comparison"].get("trajectory/state_deviation.txt", {}).get("byte_identical"),
                     "invocations_observed": recs_n, "exit_code": r["estimator"]["exit_code"],
                     "reference": r["reference_comparison"].get("reference_run_directory")})
    return rows


def class7_table(runs7a: List[Dict[str, Any]], runs7b: List[Dict[str, Any]], ref_console_lines: Dict[str, int]) -> Dict[str, Any]:
    rows = []
    for r in runs7a:
        rows.append({"variant": "7a_console_writes_fail", "run_id": r["run_id"], "dataset": r["dataset"], "sequence": r["sequence"],
                     "outcome_unaffected": r["reference_comparison"].get("status") == "BYTE_IDENTICAL",
                     "failed_writes_lower_bound": ref_console_lines.get(r["sequence"], 0),
                     "exit_code": r["estimator"]["exit_code"], "t0_stage": None, "t0_kind": None, "t0_disable_reason": None,
                     "t0_bytes": None})
    for r in runs7b:
        env = r["shim"]["env"]
        console = r["_dir"] / "diagnostics" / "console.log"
        reason = None
        if console.is_file():
            data = strip(console.read_bytes())
            m = re.search(rb"\[TURNSAFE-T0\]: status=(\w+)(?: reason=(\w+))?", data)
            if m:
                reason = (m.group(1) + (b" " + m.group(2) if m.group(2) else b"")).decode()
        t0 = r["_dir"] / "diagnostics" / "turnsafe_t0.jsonl"
        t0tmp = r["_dir"] / "diagnostics" / "turnsafe_t0.jsonl.tmp"
        rows.append({"variant": "7b_t0_stream_fault", "run_id": r["run_id"], "dataset": r["dataset"], "sequence": r["sequence"],
                     "outcome_unaffected": r["reference_comparison"].get("status") == "BYTE_IDENTICAL",
                     "failed_writes_lower_bound": 1,
                     "exit_code": r["estimator"]["exit_code"], "t0_stage": int(env.get("C8_T0_STAGE", -1)),
                     "t0_kind": int(env.get("C8_T0_KIND", 0)), "t0_disable_reason": reason,
                     "t0_bytes": (t0.stat().st_size if t0.is_file() else (t0tmp.stat().st_size if t0tmp.is_file() else None))})
    return {"rows": rows,
            "summary": {"7a_runs": len(runs7a), "7a_unaffected": sum(1 for x in rows if x["variant"].startswith("7a") and x["outcome_unaffected"]),
                        "7a_failed_writes_lower_bound_total": sum(x["failed_writes_lower_bound"] for x in rows if x["variant"].startswith("7a")),
                        "7b_runs": len(runs7b), "7b_unaffected": sum(1 for x in rows if x["variant"].startswith("7b") and x["outcome_unaffected"])}}


def natural_incidents() -> Dict[str, Any]:
    out = {"roots": {}, "totals": collections.Counter(), "files": 0}
    for cid, root in CAMPAIGN_ROOTS.items():
        rp = Path(root)
        counts: Dict[str, int] = collections.Counter()
        files = 0
        for log in rp.rglob("console.log"):
            files += 1
            data = strip(log.read_bytes())
            for prefix, _ in NATURAL_PREFIXES:
                counts[prefix] += data.count(prefix.encode())
            # denominators: update callbacks are not logged at INFO; use published state rows as the frame denominator
        state_rows = 0
        for st in rp.rglob("state_estimate.txt"):
            with open(st, "rb") as f:
                state_rows += sum(1 for _ in f) - 1
        out["roots"][cid] = {"console_files": files, "state_rows_total": max(state_rows, 0), "counts": dict(counts)}
        out["totals"].update(counts)
        out["files"] += files
    out["totals"] = dict(out["totals"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--skip-natural", action="store_true")
    args = ap.parse_args()
    root = Path(args.root)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    runs = load_runs(root)
    caps = [r for r in runs if r["_tag"] == "capture"]
    cyc = [r for r in runs if r["_tag"].startswith("cycle")]
    r7a = [r for r in runs if r["_tag"] == "class7a-console"]
    r7b = [r for r in runs if r["_tag"].startswith("class7b-t0")]

    inert = inertness_table(caps)
    cm = cycle_matrix(cyc)
    ref_lines = {}
    for r in caps:
        p = r["_dir"] / "diagnostics" / "console.log"
        if p.is_file():
            with open(p, "rb") as f:
                ref_lines[r["sequence"]] = sum(1 for _ in f)
    c7 = class7_table(r7a, r7b, ref_lines)
    nat = None if args.skip_natural else natural_incidents()

    result = {
        "schema": "schurvio.icra27.c8.fault_matrix.v1",
        "root": str(root),
        "runs_total": len(runs),
        "inertness": {"rows": inert, "byte_identical": sum(1 for x in inert if x["status"] == "BYTE_IDENTICAL"), "n": len(inert)},
        "cycle": cm,
        "class7": c7,
        "natural_incidents": nat,
        "verdict": {
            "H4_all_zero_violation_columns": (len(cm["violations"]) == 0 and c7["summary"]["7a_unaffected"] == c7["summary"]["7a_runs"]
                                              and c7["summary"]["7b_unaffected"] == c7["summary"]["7b_runs"]),
            "violations_total": len(cm["violations"]),
            "class7_affected_runs": (c7["summary"]["7a_runs"] - c7["summary"]["7a_unaffected"]) + (c7["summary"]["7b_runs"] - c7["summary"]["7b_unaffected"]),
            "inertness_gate": "PASS" if inert and all(x["status"] == "BYTE_IDENTICAL" for x in inert) else "FAIL_OR_INCOMPLETE",
        },
    }
    (out / "fault_matrix.json").write_text(json.dumps(result, indent=1, sort_keys=True, default=str))
    with open(out / "fault_matrix.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(cm["matrix"][0].keys()) if cm["matrix"] else ["class"])
        w.writeheader()
        for row in cm["matrix"]:
            w.writerow(row)
    with open(out / "inertness.csv", "w", newline="") as f:
        if inert:
            w = csv.DictWriter(f, fieldnames=list(inert[0].keys()))
            w.writeheader()
            for row in inert:
                w.writerow(row)
    with open(out / "class7.csv", "w", newline="") as f:
        if c7["rows"]:
            w = csv.DictWriter(f, fieldnames=list(c7["rows"][0].keys()))
            w.writeheader()
            for row in c7["rows"]:
                w.writerow(row)
    if nat:
        with open(out / "natural_incidents.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["campaign", "console_files", "state_rows_total"] + [p for p, _ in NATURAL_PREFIXES])
            for cid, v in nat["roots"].items():
                w.writerow([cid, v["console_files"], v["state_rows_total"]] + [v["counts"].get(p, 0) for p, _ in NATURAL_PREFIXES])

    # Markdown
    md = ["# C8 Item 1 — H4 fault-injection matrix (frozen branch)", "",
          "Root: `{}`  Runs: {}".format(root, len(runs)), "",
          "## Inertness gate (shim attached, no injection) — {}/{} BYTE_IDENTICAL to CDSC-1R4 S1".format(result["inertness"]["byte_identical"], result["inertness"]["n"]), "",
          "| sequence | status | invocations |", "|---|---|---:|"]
    for x in inert:
        md.append("| {} | {} | {} |".format(x["sequence"], x["status"], x["invocations_observed"]))
    md += ["", "## Fault matrix (cycle runs: {} runs, {} update invocations, {} eligible)".format(cm["totals"]["runs"], cm["totals"]["invocations"], cm["totals"]["eligible_invocations"]), "",
           "| class | name | scheduled | reached | realized | realized: no commit | realized: commit of other features | typed console lines | ineffective (contained) | state mutated w/o commit | commit after realized fault | event reports commit | restore mismatch | multi-commit | exception |",
           "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for m in cm["matrix"]:
        md.append("| {class} | {class_name} | {scheduled} | {reached_injection_point} | {fault_realized} | {realized_no_commit} | {realized_with_commit_of_other_features} | {typed_console_lines} | {ineffective_containment_restore} | {viol_state_mutated_without_commit} | {viol_commit_after_realized_transaction_fault} | {viol_event_reports_commit} | {viol_restore_mismatch} | {viol_multi_commit} | {viol_exception} |".format(**m))
    md += ["", "Global: callbacks with >1 successful commit = {} / {}; callbacks with >1 commit call = {} / {}; exceptions escaping update() = {} / {}.".format(
        cm["totals"]["multi_commit_callbacks"], cm["totals"]["invocations"], cm["totals"]["commit_calls_gt1_callbacks"], cm["totals"]["invocations"], cm["totals"]["exceptions_escaped"], cm["totals"]["invocations"]), ""]
    md += ["## Class 7 — logging/diagnostics failure", "",
           "7a console writes fail (stdout=/dev/full whole run): {}/{} runs outcome unaffected (trajectory BYTE_IDENTICAL); failed writes lower bound = {} (reference console line count).".format(c7["summary"]["7a_unaffected"], c7["summary"]["7a_runs"], c7["summary"]["7a_failed_writes_lower_bound_total"]),
           "7b T0 diagnostics-stream faults (stage x kind): {}/{} runs outcome unaffected.".format(c7["summary"]["7b_unaffected"], c7["summary"]["7b_runs"]), "",
           "| variant | sequence | stage | kind | T0 status | unaffected |", "|---|---|---:|---:|---|---|"]
    for x in c7["rows"]:
        if x["variant"].startswith("7b"):
            md.append("| 7b | {} | {} | {} | {} | {} |".format(x["sequence"], x["t0_stage"], x["t0_kind"], x["t0_disable_reason"], x["outcome_unaffected"]))
    if nat:
        md += ["", "## Natural-incident reconciliation (all campaign roots, console.log scrape)", "",
               "| campaign | console files | state rows | " + " | ".join(p for p, _ in NATURAL_PREFIXES) + " |",
               "|---|---:|---:|" + "---:|" * len(NATURAL_PREFIXES)]
        for cid, v in nat["roots"].items():
            md.append("| {} | {} | {} | ".format(cid, v["console_files"], v["state_rows_total"]) + " | ".join(str(v["counts"].get(p, 0)) for p, _ in NATURAL_PREFIXES) + " |")
    md += ["", "## Verdict", "", "H4 all-zero violation columns: **{}** (violations={}, class-7 affected runs={}, inertness gate={}).".format(
        result["verdict"]["H4_all_zero_violation_columns"], result["verdict"]["violations_total"], result["verdict"]["class7_affected_runs"], result["verdict"]["inertness_gate"])]
    (out / "FAULT_MATRIX.md").write_text("\n".join(md) + "\n")
    with open(out / "SHA256SUMS", "w") as f:
        for p in sorted(out.iterdir()):
            if p.name != "SHA256SUMS":
                f.write("{}  {}\n".format(sha256_file(p), p.name))
    print(json.dumps(result["verdict"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
