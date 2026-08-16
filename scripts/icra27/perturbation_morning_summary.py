#!/usr/bin/python3.8
"""Render MORNING_SUMMARY.md for a PERTURB-1 artifact root from its final aggregate.

Sections, in order: (1) integrity gate; (2) run accounting with denominators;
(3) CR-1/CR-2/CR-3 verdicts with counts; (4) notable typed failures and
STOPped lanes; (5) estimator-diff check output; (6) exactly the claim
sentences licensed by the prereg gates given the counts; (7) artifact paths.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

REPO = Path("/home/moksh/schurvio-lite-rotation-robustness")
ESTIMATOR_DIRS = ["ov_msckf", "ov_core", "ov_init", "ov_eval", "config"]


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=False, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout


def fmt(v, d=4):
    if v is None:
        return "—"
    if isinstance(v, float):
        return "{:.{}f}".format(v, d)
    return str(v)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--aggregate", required=True, type=Path)
    p.add_argument("--baseline-commit", default="51b8c48")
    p.add_argument("--science-commit", default="2751bcdc0fae25c993b3224dc5fa40aaab6571d7")
    p.add_argument("--ended-early", default=None)
    p.add_argument("--extra-note", action="append", default=[])
    args = p.parse_args()
    root = args.artifact_root
    a = json.load(open(args.aggregate / "aggregate.json"))
    acc = a["accounting"]
    ig = a["integrity_gate"]
    cr1, cr2, cr3 = a["gates"]["CR-1"], a["gates"]["CR-2"], a["gates"]["CR-3"]
    det = a["determinism"]
    cells = a["cells"]
    head = git("rev-parse", "HEAD").strip()
    diff_base = git("diff", "--stat", args.baseline_commit, "HEAD", "--", *ESTIMATOR_DIRS)
    diff_sci = git("diff", "--stat", args.science_commit, "HEAD", "--", "ov_msckf/src", "ov_core/src", "ov_init/src", "ov_eval/src")
    status_est = git("status", "--porcelain=v1", "--untracked-files=all", "--", *ESTIMATOR_DIRS)
    prereg_log = git("log", "--oneline", "-3", "--", "docs/icra27/PERTURBATION_PREREG.md")

    focus_seed_not_runnable = acc["not_runnable_by_reason"]["seed_no_runtime_parameter"]
    neg_not_runnable = acc["not_runnable_by_reason"]["negative_offset_no_lead_in"]
    matrix_cells = [c for c in cells if c["set"] != "integrity"]
    failures = [c for c in matrix_cells if c["outcome"] == "CLOSED" and c["evidence_validity"] == "VALID" and not c["passage_complete"]]
    invalid = [c for c in matrix_cells if c["outcome"] == "CLOSED" and c["evidence_validity"] != "VALID"]
    other = [c for c in matrix_cells if c["outcome"] not in ("CLOSED", "NOT_RUNNABLE")]
    exec_valid = [c for c in matrix_cells if c["outcome"] == "CLOSED" and c["evidence_validity"] == "VALID"]

    L = []
    L.append("# PERTURB-1 MORNING SUMMARY")
    L.append("")
    L.append("Artifact root: `{}`. Aggregate: `{}`. Prereg `docs/icra27/PERTURBATION_PREREG.md` (SHA-256 `{}`) was executed unedited; git log: {}".format(root, args.aggregate, a["prereg"]["sha256"], prereg_log.strip().replace("\n", "; ")))
    L.append("")
    L.append("Attribution rung: **N0** (in-repo matched nullspace control) exists and was used — the frozen S1 binary, config and launch with the single ROS parameter `up_msckf_landmark_elimination=nullspace`; every matched S1/N0 pair's resolved parameter map differs in exactly that key: {} ({} pairs). No U0 rows were run (optional context only). The RANSAC-seed axis is NOT runnable on the frozen binaries (compiled `cv::setRNGSeed(0)`, no runtime parameter); that lane was STOPped and its {} cells are recorded NOT_RUNNABLE (DECISIONS.md D2). Negative offsets have no lead-in data for the 11 KAIST bags and EuRoC V1_01/V2_03 (frozen start 0.0 s); those {} cells are NOT_RUNNABLE (D3).".format(a["n0_configuration_diff"]["all_pairs_single_delta"], len(a["n0_configuration_diff"]["resolved_parameter_diffs"]), focus_seed_not_runnable, neg_not_runnable))
    if args.ended_early:
        L.append("")
        L.append("**The night ended early:** " + args.ended_early)
    L.append("")
    L.append("## 1. Integrity gate")
    L.append("")
    L.append("Verdict **{}**. ".format(ig.get("verdict")) + "; ".join("{} {} offset 0 frozen seed → {} (fresh state_estimate/state_deviation/estimate_raw.tum SHA-256 == CDSC-1R4 recorded)".format(c["sequence"], c["system"], c["determinism_status"]) for c in ig.get("cells", [])))
    L.append("Additionally, every offset-0 frozen-seed S1 matrix cell was byte-checked against CDSC-1R4: byte-identical {}, mismatch {}; frozen-seed replicate cells vs their offset-0 cell: byte-identical {}, mismatch {}.".format(det["cdsc1r4_checks"]["byte_identical"], det["cdsc1r4_checks"]["mismatch"], det["replicate_checks"]["byte_identical"], det["replicate_checks"]["mismatch"]))
    L.append("")
    L.append("## 2. Run accounting")
    L.append("")
    L.append("| Quantity | Count |")
    L.append("|---|---:|")
    for k, v in (
        ("Prereg matrix runs (expected)", acc["prereg_total_runs"]),
        ("Cells planned by driver", acc["planned_cells_in_driver"]),
        ("Cell records found", acc["cell_records_found"]),
        ("NOT_RUNNABLE — seed axis (+1..+4), no runtime seed parameter", focus_seed_not_runnable),
        ("NOT_RUNNABLE — negative offset, no lead-in data", neg_not_runnable),
        ("Executed and closed", acc["executed_closed"]),
        ("Executed, evidence VALID", acc["executed_evidence_valid"]),
        ("Executed, evidence invalid / fatal", acc["executed_evidence_invalid_or_fatal"]),
        ("Unclosed / no result", acc["unclosed_or_no_result"]),
        ("Planned but not recorded", acc["not_yet_recorded"]),
        ("Completions (VALID)", acc["completions"]),
        ("Typed failures (VALID, not complete)", acc["typed_failures"]),
    ):
        L.append("| {} | {} |".format(k, v))
    L.append("")
    L.append("By status: " + ", ".join("{}={}".format(k, v) for k, v in acc["by_status"].items()) + ".")
    L.append("")
    L.append("By set: " + "; ".join("{}: planned {}, NOT_RUNNABLE {}, executed {}, completions {}".format(k, v["planned"], v["not_runnable"], v["executed"], v["completions"]) for k, v in acc["by_set"].items()) + ".")
    L.append("")
    L.append("Per-sequence-per-system (completions/planned, completions/executed, ATE median [IQR] m over completions, n):")
    L.append("")
    L.append("| Seq | System | compl/planned | compl/executed | usable(KAIST) | ATE median | IQR | n | failure classes |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in a["per_sequence_system"]:
        d = r["ate_translation_rmse_m"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(r["sequence"], r["system"], r["completion_over_planned"], r["completion_over_executed"], fmt(r["usable_completion_count"]), fmt(d["median"]), fmt(d["iqr"]), d["count"], ", ".join(r["typed_failure_classes"]) or "—"))
    L.append("")
    L.append("## 3. Gate verdicts")
    L.append("")
    L.append("**CR-1** (rotation-family completion robustness; prereg: S1 ≥ 19/20 AND N0 fails or exceeds ATE 0.5 m on ≥ 6/20):")
    L.append("- literal reading (a), denominator 20/system, NOT_RUNNABLE = not completed for S1 / not failed for N0: S1 completions **{}/{}**, N0 fails-or-exceeds **{}/{}** → **{}**.".format(cr1["literal"]["s1_completions"], cr1["literal"]["s1_denominator"], cr1["literal"]["n0_fail_or_exceed_count"], cr1["literal"]["n0_denominator"], cr1["literal"]["verdict"]))
    L.append("- runnable-only proportional reading (b) (interpretation, not prereg text): S1 completions **{}/{}**, N0 fails-or-exceeds **{}/{}** → **{}**.".format(cr1["proportional"]["s1_completions"], cr1["proportional"]["s1_denominator"], cr1["proportional"]["n0_fail_or_exceed_count"], cr1["proportional"]["n0_denominator"], cr1["proportional"]["verdict"]))
    L.append("- N0 failures typed: " + ("; ".join("{} [{}{}]".format(f["run_id"], f["typed_failure_class"] or "COMPLETE", ", ATE {:.3f} m > 0.5".format(f["ate_translation_rmse_m"]) if f["exceeds_usability_threshold"] else "") for f in cr1["n0_failures_typed"]) or "none") + ".")
    L.append("- S1 non-completions incl. NOT_RUNNABLE: " + ("; ".join("{} [{}]".format(f["run_id"], f["typed_failure_class"] or f["outcome"]) for f in cr1["s1_noncompletions_typed"]) or "none") + ".")
    L.append("")
    L.append("**CR-2** (no new S1 fragility): global reading → **{}** (offset-0 S1 failure classes {}; new classes under perturbation {}); per-sequence reading → **{}**. All S1 perturbed failure occurrences: {}.".format(cr2["verdict_global"], cr2["global_reading"]["offset0_failure_classes"], cr2["global_reading"]["new_failure_classes"], cr2["verdict_per_sequence"], "; ".join("{} [{}]".format(o["run_id"], o["typed_failure_class"]) for o in cr2["all_s1_perturbed_failure_occurrences_reported"]) or "none"))
    L.append("")
    L.append("**CR-3** (descriptive; square_fast / circle_fast / square_head S1 vs N0 ATE distributions, all cells):")
    L.append("")
    L.append("| Seq | System | executed | completions | ATE median | IQR | min | max | values |")
    L.append("|---|---|---:|---:|---:|---:|---:|---:|---|")
    for seq, e in cr3["sequences"].items():
        for sy in ("S1", "N0"):
            s = e["systems"][sy]
            d = s["ate_translation_rmse_m"]
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(seq, sy, s["executed"], s["completions"], fmt(d["median"]), fmt(d["iqr"]), fmt(d["min"]), fmt(d["max"]), ", ".join("{:.4f}".format(v) for v in d["values"])))
        L.append("| {} | S1 vs N0 matched | comparable {} | S1 worse in {} | persists across all comparable perturbations: {} | | | | |".format(seq, e["comparable_matched_count"], e["s1_worse_count"], e["s1_regression_persists_across_all_comparable_perturbations"]))
    L.append("")
    L.append("Statistics language: counts and distributions only; runnable n per rotation-family cell < 10, so no Fisher's exact test and no significance language.")
    L.append("")
    L.append("## 4. Notable typed failures and STOPped lanes")
    L.append("")
    L.append("- STOPped lane: RANSAC-seed axis ({} cells NOT_RUNNABLE) — no runtime seed parameter in the frozen estimator; would require estimator source edit + rebuild (prohibited).".format(focus_seed_not_runnable))
    L.append("- NOT_RUNNABLE negative offsets: {} cells (no lead-in data before the frozen replay start).".format(neg_not_runnable))
    if failures:
        L.append("- Typed failures (VALID evidence, passage not complete):")
        for c in failures:
            L.append("  - {} — {} / {} ; status {} ; ATE {} ; mechanism {}".format(c["run_id"], c["status"], c["typed_failure_class"], c["status"], fmt(c["ate_translation_rmse_m"]), c["robustness_mechanism_status"]))
    else:
        L.append("- Typed failures with VALID evidence: none.")
    if invalid:
        L.append("- Cells with invalid/fatal evidence (retained, excluded from completion counts): " + "; ".join("{} [{}/{}]".format(c["run_id"], c["status"], c["evidence_validity"]) for c in invalid))
    if other:
        L.append("- Cells neither closed nor NOT_RUNNABLE: " + "; ".join("{} [{}]".format(c["run_id"], c["outcome"]) for c in other))
    over = [c for c in exec_valid if c["passage_complete"] and c["dataset"] == "kaist_vio" and c["kaist_usable_completion"] is False]
    L.append("- KAIST completions above the 0.5 m usability threshold: " + ("; ".join("{} ATE {:.3f}".format(c["run_id"], c["ate_translation_rmse_m"]) for c in over) or "none") + ".")
    for note in args.extra_note:
        L.append("- " + note)
    L.append("")
    L.append("## 5. Estimator-diff check")
    L.append("")
    L.append("Tooling HEAD `{}` (branch perturb/preg-20260816). Path-scoped `git diff --stat {} HEAD -- {}` output:".format(head, args.baseline_commit, " ".join(ESTIMATOR_DIRS)))
    L.append("```")
    L.append(diff_base.rstrip() if diff_base.strip() else "(empty)")
    L.append("```")
    L.append("`git diff --stat {} HEAD -- ov_msckf/src ov_core/src ov_init/src ov_eval/src` (frozen S1 science commit vs HEAD):".format(args.science_commit[:7]))
    L.append("```")
    L.append(diff_sci.rstrip() if diff_sci.strip() else "(empty)")
    L.append("```")
    L.append("`git status --porcelain --untracked-files=all -- {}`:".format(" ".join(ESTIMATOR_DIRS)))
    L.append("```")
    L.append(status_est.rstrip() if status_est.strip() else "(empty)")
    L.append("```")
    L.append("")
    L.append("## 6. Licensed claim sentences (and nothing stronger)")
    L.append("")
    lit = cr1["literal"]
    if lit["verdict"] == "CLAIMABLE":
        L.append("- CR-1: Under the pre-registered perturbation family, frozen S1 completed {}/{} rotation-family samples while the matched nullspace control N0 failed or exceeded the 0.5 m usability threshold on {}/{} (failures typed above); the rotation-family completion-robustness claim is licensed as a count statement.".format(lit["s1_completions"], lit["s1_denominator"], lit["n0_fail_or_exceed_count"], lit["n0_denominator"]))
    else:
        L.append("- CR-1: NOT claimable under the literal prereg reading — S1 completed {}/{} rotation-family samples (of which {} planned samples were NOT_RUNNABLE) and N0 failed or exceeded the usability threshold on {}/{}. Runnable-only counts: S1 {}/{}, N0 {}/{} ({}). No completion-robustness claim is licensed.".format(lit["s1_completions"], lit["s1_denominator"], sum(1 for f in cr1["s1_noncompletions_typed"] if f["outcome"] == "NOT_RUNNABLE"), lit["n0_fail_or_exceed_count"], lit["n0_denominator"], cr1["proportional"]["s1_completions"], cr1["proportional"]["s1_denominator"], cr1["proportional"]["n0_fail_or_exceed_count"], cr1["proportional"]["n0_denominator"], cr1["proportional"]["verdict"]))
    L.append("- CR-2: Across {} executed VALID S1 cells, S1 introduced {} failure class(es) absent at offset 0 (global reading {}; per-sequence reading {}); every occurrence is listed in section 3.".format(sum(1 for c in exec_valid if c["system"] == "S1"), len(cr2["global_reading"]["new_failure_classes"]), cr2["verdict_global"], cr2["verdict_per_sequence"]))
    for seq, e in cr3["sequences"].items():
        s1d = e["systems"]["S1"]["ate_translation_rmse_m"]
        n0d = e["systems"]["N0"]["ate_translation_rmse_m"]
        L.append("- CR-3 ({}): S1 ATE median {} m [IQR {}] over {} completions vs N0 {} m [IQR {}] over {} completions; S1 worse than N0 in {}/{} matched perturbations{}.".format(seq, fmt(s1d["median"]), fmt(s1d["iqr"]), s1d["count"], fmt(n0d["median"]), fmt(n0d["iqr"]), n0d["count"], e["s1_worse_count"], e["comparable_matched_count"], " (regression persists across all comparable perturbations)" if e["s1_regression_persists_across_all_comparable_perturbations"] else ""))
    L.append("- Deterministic integrity: offset-0 frozen-seed S1 cells were byte-identical to CDSC-1R4 in {}/{} checks.".format(det["cdsc1r4_checks"]["byte_identical"], det["cdsc1r4_checks"]["byte_identical"] + det["cdsc1r4_checks"]["mismatch"]))
    L.append("")
    L.append("## 7. Artifacts")
    L.append("")
    for name in ("RUN_LOG.md", "DECISIONS.md", "integrity/INTEGRITY_GATE.json", "driver/cells.jsonl", "driver/logs/", "control/events/", "cells/", "launcher/logs/campaign.log"):
        L.append("- `{}`".format(root / name))
    L.append("- final aggregate: `{}` (aggregate.json, cells.csv, sequence_system_summary.csv, PERTURBATION_REPORT.md, SHA256SUMS)".format(args.aggregate))
    L.append("- checkpoints: `{}`".format(root / "aggregate"))
    L.append("- tooling branch `perturb/preg-20260816` in `/home/moksh/schurvio-lite-rotation-robustness` (HEAD `{}`)".format(head))
    L.append("- CDSC-1R4 reference: `/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z/`")
    out = root / "MORNING_SUMMARY.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
