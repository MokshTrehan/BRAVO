#!/usr/bin/python3.8
"""Render ABLATE-REC-1 MORNING_SUMMARY.md from the final aggregate, in the order
required by docs/icra27/ABLATION_OVERNIGHT_PROMPT.md Step 4:
(1) Step-0 facts, (2) integrity/P3, (3) run accounting, (4) P1/P2 verdicts +
failure-class comparison, (5) interpretation row + verbatim claim, (6) the
estimator-diff output, (7) artifact paths.  Numbers come only from the
aggregate; the estimator-diff block is passed in as files produced by git.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, List, Mapping, Optional, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import perturbation_aggregate as pagg  # noqa: E402

_fmt = pagg._fmt


def _gaps(gaps: Optional[Sequence[Mapping[str, Any]]]) -> str:
    return "; ".join("{:.3f}->{:.3f} ({:.3f} s)".format(g["start_timestamp_s"], g["end_timestamp_s"], g["duration_s"]) for g in gaps or []) or "none"


def render(a: Mapping[str, Any], root: Path, diff_text: str, distribution_text: str, tooling_head: str, prereg_git: str) -> str:
    L: List[str] = []
    P = a["predictions"]
    p1, p2, p3 = P["P1"], P["P2"], P["P3"]
    acc = a["accounting"]
    it = a["interpretation"]
    L.append("# ABLATE-REC-1 MORNING SUMMARY")
    L.append("")
    L.append("Artifact root: `{}`. Final aggregate: `{}`. Prereg `docs/icra27/ABLATION_PREREG.md` (SHA-256 `{}`, git {}) executed unedited (Step-0 sequence-set extension not required, see 1). Tooling branch `ablate/rec-20260817`, HEAD `{}`.".format(
        root, a["artifact_root_aggregate"], a["prereg"]["sha256"], prereg_git, tooling_head))
    L.append("")
    # (1) Step-0 facts
    L.append("## 1. Step-0 facts")
    L.append("")
    L.append(distribution_text.strip())
    L.append("")
    L.append("- **Identified runtime switch:** estimator option `long_gap_recovery_enabled` (bool; ov_msckf/src/core/VioManagerOptions.h:113 default false, parsed at :196 via YamlParser::parse_config, ROS-parameter-over-YAML precedence). Every recovery code path in the frozen executable is guarded by it. recOFF launch = frozen recON launch + exactly one added line `<param name=\"long_gap_recovery_enabled\" type=\"bool\" value=\"false\" />` (four files under project/, DECISIONS A1). Single-delta template A2: same binary sha256, same config sha256, resolved ROS map identical except the one added key `<ns>/long_gap_recovery_enabled: False`.")
    sd = p2["single_delta"]
    L.append("- **Single-delta machine verification:** {}/{} recOFF cells verified (not single-delta: {}; unverified: {}).".format(sd["machine_verified_single_delta"], sd["recoff_cells_recorded"], sd["not_single_delta"] or "none", sd["unverified"] or "none"))
    L.append("- **Sequence-set extension:** none (recovery fired only on rotation/rotation.bag, inside the pre-registered rotation family). The prereg was not edited.")
    L.append("- Resolved recovery value per cell class: recON KAIST S1/N0 = YAML true (effective true); recON EuRoC = key absent, executable default false; recOFF = ROS parameter false (effective false, console 'overriding node long_gap_recovery_enabled with value from ROS!'); U0 = option does not exist upstream.")
    L.append("")
    # (2) Integrity
    L.append("## 2. Integrity / P3")
    L.append("")
    L.append("Verdict **{}** (integrity gate {}).".format(p3["verdict"], p3["integrity_gate_verdict"]))
    for c in p3.get("cells") or []:
        L.append("- `{}` ({} {}): {} / {}; vs PERTURB-1 recorded SHA256SUMS **{}**; vs CDSC-1R4 {}".format(c.get("run_id"), c.get("system"), c.get("sequence"), c.get("status"), c.get("evidence_validity"), c.get("perturb1_status"), c.get("cdsc1r4_status")))
    L.append("")
    # (3) Accounting
    L.append("## 3. Run accounting")
    L.append("")
    L.append("| Quantity | Count |")
    L.append("|---|---:|")
    t = acc["total"]
    L.append("| Expected (prereg matrix 42 recOFF + 6 U0 context + 3 integrity replicas) | {} |".format(t["planned"]))
    L.append("| Cell records found | {} |".format(t["recorded"]))
    L.append("| NOT_RUNNABLE | {} |".format(t["not_runnable"]))
    L.append("| Executed and closed | {} |".format(t["executed_closed"]))
    L.append("| Executed, evidence VALID | {} |".format(t["executed_valid"]))
    L.append("| Executed, evidence invalid / fatal | {} |".format(t["executed_invalid_or_fatal"]))
    L.append("| Unclosed / no result | {} |".format(t["unclosed_or_no_result"]))
    L.append("| Planned but not recorded | {} |".format(len(t["planned_but_not_recorded"])))
    L.append("| Completions (VALID) | {} |".format(t["completions_valid"]))
    L.append("| Typed failures (VALID, not complete) | {} |".format(t["typed_failures_valid"]))
    L.append("")
    L.append("By set: " + "; ".join("{}: planned {}, recorded {}, NOT_RUNNABLE {}, executed VALID {}, completions {}, typed failures {}".format(k, v.get("planned"), v.get("recorded", 0), v.get("not_runnable", 0), v.get("executed_valid", 0), v.get("completions_valid", 0), v.get("typed_failures_valid", 0)) for k, v in sorted(acc["by_set"].items())) + ".")
    L.append("By status: " + ", ".join("{}={}".format(k, v) for k, v in sorted(acc["by_status"].items())) + ".")
    if t["planned_but_not_recorded"]:
        L.append("Planned but not recorded: " + ", ".join(t["planned_but_not_recorded"]))
    if acc["not_runnable_reasons"]:
        L.append("NOT_RUNNABLE reasons: " + "; ".join(acc["not_runnable_reasons"]))
    L.append("")
    L.append("Per sequence × system (completions/planned, completions/executed, ATE median [IQR] m over completions, n, failure classes, byte identity vs PERTURB-1 recON):")
    L.append("")
    L.append("| Seq | System | compl/planned | compl/executed | ATE median | IQR | n | failure classes | byte identity vs recON |")
    L.append("|---|---|---:|---:|---:|---:|---:|---|---|")
    for r in a["per_sequence_system"]:
        d = r["ate_translation_rmse_m"]
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(r["sequence"], r["system"], r["completion_over_planned"], r["completion_over_executed"], _fmt(d["median"]), _fmt(d["iqr"]), d["count"], ", ".join(r["typed_failure_classes"]) or "—", ", ".join(r["byte_identity_vs_perturb1_recon"]) if r["byte_identity_vs_perturb1_recon"] else "—"))
    L.append("")
    # (4) P1/P2
    L.append("## 4. P1 and P2 verdicts")
    L.append("")
    L.append("**P1** ({}): verdict **{}**. Cells meeting the P1 condition (non-completion or ATE ≥ 0.5 m): {}/{} executed VALID of {} planned; in the U0 failure family (tracking loss / unclosed state gap, DECISIONS A6): {}/{}; completing under threshold: {}/{}.".format(
        p1["prereg_text"], p1["verdict"], p1["cells_meeting_condition"], p1["denominator_executed_valid"], p1["denominator_planned"], p1["cells_in_u0_failure_family"], p1["denominator_executed_valid"], p1["cells_completing_under_threshold"], p1["denominator_executed_valid"]))
    for s, v in p1["per_system"].items():
        L.append("- {}: executed VALID {}/{}, condition met {}/{}, in family {}/{}, completed under threshold {}/{}.".format(s, v["executed_valid"], v["planned"], v["p1_condition_met"], v["planned"], v["in_family"], v["planned"], v["completed_under_threshold"], v["planned"]))
    L.append("")
    L.append("| cell | system | offset | status | validity | passage | ATE m | recovery lines | unsupported gaps | tail gap s | in family | same status as CDSC-1R4 U0 | first gap Δ vs U0 (start, end, dur s) | single-delta |")
    L.append("|---|---|---:|---|---|---|---:|---:|---|---:|---|---|---|---|")
    for x in p1["cells"]:
        sig = x["failure_signature"]
        fam = x["family"] or {}
        d = fam.get("first_unsupported_gap_vs_cdsc1r4_u0")
        L.append("| `{}` | {} | {:+d} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            x["run_id"], x["system"], x["offset_frames"], x["status"], x["evidence_validity"], x["passage_complete"], _fmt(x["ate_translation_rmse_m"]), x["recovery_events_observed"], _gaps(sig["unsupported_state_gaps"]), _fmt(sig["tail_gap_s"], 3),
            fam.get("in_tracking_loss_or_unclosed_gap_family"), fam.get("same_status_as_cdsc1r4_u0"), "—" if not d else "{:+.3f}, {:+.3f}, {:+.3f}".format(d["start_delta_s"], d["end_delta_s"], d["duration_delta_s"]), x["single_delta_verified"]))
    L.append("")
    L.append("**P2** ({}): verdict **{}**. Reference set = {} (planned {} cells; recorded {}). Byte-identical {}/{}; mismatch {}; not comparable {}.".format(
        p2["prereg_text"], p2["verdict"], p2["reference_set_definition"], p2["denominator_planned_reference_set"], p2["denominator_reference_cells_recorded"], p2["byte_identical"], p2["denominator_planned_reference_set"], p2["mismatch"], p2["not_comparable"]))
    if p2["mismatch_cells"]:
        L.append("- Mismatch cells: " + "; ".join("`{}`".format(x["run_id"]) for x in p2["mismatch_cells"]))
    if p2["not_comparable_cells"]:
        L.append("- Not comparable: " + "; ".join("`{}` ({})".format(x["run_id"], x["byte_identity_status"]) for x in p2["not_comparable_cells"]))
    L.append("- rotation.bag recOFF cells (counterpart fired; outside P2, descriptive): " + "; ".join("`{}` -> {} ({} vs recON)".format(x["run_id"], x["status"], x["byte_identity_status"]) for x in p2["fired_counterpart_cells_descriptive"]))
    L.append("")
    fc = a["failure_class_comparison"]
    ref = fc["cdsc1r4_u0_rotation_signature"]
    L.append("**Failure-class comparison (recOFF rotation.bag vs U0):** recOFF status set {}; ABLATE-REC-1 U0 context rotation.bag status set {}; CDSC-1R4 U0 rotation.bag reference: status {}, passage {} ({}), unsupported gap {}, tail gap {} s.".format(
        fc["recoff_status_set"], fc["u0_context_rotation_status_set"], ref.get("status"), ref.get("passage_complete"), ref.get("passage_reason"), _gaps(ref.get("unsupported_state_gaps")), _fmt(ref.get("tail_gap_s"), 3)))
    L.append("")
    u0 = a["u0_context"]
    L.append("U0 context rows (n=3 per sequence): " + "; ".join("{}: executed VALID {}/{}, completions {}, non-completions {} (classes {})".format(seq, v["executed_valid"], v["planned"], v["completions"], v["non_completions"], ", ".join(v["failure_classes"]) or "none") for seq, v in u0["per_sequence"].items()) + ".")
    L.append("")
    L.append("| U0 cell | seq | offset | status | passage | class | ATE m | usable | unsupported gaps | tail gap s | vs CDSC-1R4 |")
    L.append("|---|---|---:|---|---|---|---:|---|---|---:|---|")
    for x in u0["cells"]:
        sig = x["failure_signature"]
        L.append("| `{}` | {} | {:+d} | {} | {} | {} | {} | {} | {} | {} | {} |".format(x["run_id"], x["sequence"], x["offset_frames"], x["status"], x["passage_complete"], x["typed_failure_class"], _fmt(x["ate_translation_rmse_m"]), x["kaist_usable_completion"], _gaps(sig["unsupported_state_gaps"]), _fmt(sig["tail_gap_s"], 3), x["determinism_vs_cdsc1r4"]))
    L.append("")
    us = a["usability"]
    L.append("Usability threshold ({} m, KAIST cells): {}".format(us["threshold_m"], us["disclosure"]))
    L.append("")
    L.append("| seq | system | executed | usable completions | complete but > 0.5 m | complete, ATE unassessable | not complete | ATE values |")
    L.append("|---|---|---:|---:|---:|---:|---:|---|")
    for r in us["rows"]:
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(r["sequence"], r["system"], r["executed"], r["usable_completions"], r["complete_but_over_threshold"], r["complete_unassessable_ate"], r["not_complete"], ", ".join("{:.4f}".format(v) for v in r["ate_values"]) or "—"))
    L.append("")
    L.append("Statistics language: counts and distributions only; n per cell = 3; no significance language.")
    L.append("")
    # (5) Interpretation
    L.append("## 5. Interpretation-table row and licensed claim")
    L.append("")
    L.append("P1 = **{}**, P2 = **{}**, P3 = **{}**. Applicable row: **{}** (basis: {}).".format(p1["verdict"], p2["verdict"], p3["verdict"], it.get("applies"), it.get("basis")))
    if it.get("outcome"):
        L.append("")
        L.append("| Outcome | Meaning | Permitted claim |")
        L.append("|---|---|---|")
        L.append("| {} | {} | {} |".format(it["outcome"], it["meaning"], it["permitted_claim"]))
    L.append("")
    L.append("Licensed claim sentence, quoted verbatim from that row and nothing stronger:")
    L.append("")
    L.append("> {}".format(it.get("permitted_claim")))
    L.append("")
    # (6) diff
    L.append("## 6. Estimator-diff check")
    L.append("")
    L.append(diff_text.rstrip())
    L.append("")
    # (7) artifacts
    L.append("## 7. Artifacts")
    L.append("")
    for rel in ("RUN_LOG.md", "DECISIONS.md", "integrity/INTEGRITY_GATE.json", "driver/cells.jsonl", "driver/logs", "control/events", "cells", "launcher/run_campaign.sh", "launcher/logs/campaign.log"):
        L.append("- `{}`".format(root / rel))
    L.append("- final aggregate: `{}` (aggregate.json, cells.csv, sequence_system_summary.csv, ABLATION_REPORT.md, SHA256SUMS)".format(a["artifact_root_aggregate"]))
    L.append("- checkpoints: `{}`".format(root / "aggregate"))
    L.append("- Step-0 table: `/home/moksh/schurvio-lite-rotation-robustness/docs/icra27/recovery_event_distribution.md`")
    L.append("- tooling branch `ablate/rec-20260817` in `/home/moksh/schurvio-lite-rotation-robustness` (HEAD `{}`)".format(tooling_head))
    L.append("- PERTURB-1 reference: `/home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z/`")
    L.append("- CDSC-1R4 reference: `/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z/`")
    return "\n".join(L) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--aggregate", required=True, type=Path)
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--diff-file", required=True, type=Path)
    p.add_argument("--distribution-summary", required=True, type=Path, help="text block with the Step-0 event distribution bullets")
    p.add_argument("--tooling-head", required=True)
    p.add_argument("--prereg-git", required=True)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    a = dict(json.loads((args.aggregate / "aggregate.json").read_text()))
    a["artifact_root_aggregate"] = str(args.aggregate)
    text = render(a, args.artifact_root, args.diff_file.read_text(), args.distribution_summary.read_text(), args.tooling_head, args.prereg_git)
    if args.output.exists():
        print("refusing to overwrite {}".format(args.output), file=sys.stderr)
        return 2
    args.output.write_text(text, encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
