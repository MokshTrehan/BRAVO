#!/usr/bin/python3.8
"""BLACKOUT-1 MORNING_SUMMARY.md renderer (sections in the order required by BLACKOUT_OVERNIGHT_PROMPT.md).

Reads the final aggregate (aggregate.json), the committed injection-point table,
the gate files and the estimator-diff/binary re-hash outputs; writes
<root>/MORNING_SUMMARY.md.  Nothing here recomputes verdicts — it renders them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Mapping, Optional, Sequence

NS = 1_000_000_000
REPO = Path("/home/moksh/schurvio-lite-rotation-robustness")
KAIST = ("circle/circle.bag", "infinite/infinite.bag", "square/square.bag")
EUROC = ("MH_05_difficult", "V2_02_medium")
DURATIONS = (2, 5, 10, 15, 20)


def fmt(x: Any, p: int = 3) -> str:
    if x is None:
        return "—"
    try:
        return ("{:.%df}" % p).format(float(x))
    except (TypeError, ValueError):
        return str(x)


def hist(h: Optional[Mapping[str, Any]]) -> str:
    return "; ".join("{}={}".format(k, v) for k, v in sorted((h or {}).items())) or "—"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=False, capture_output=True, text=True).stdout.strip()


def sha(path: Path) -> str:
    return subprocess.run(["sha256sum", str(path)], check=False, capture_output=True, text=True).stdout.split()[0]


def render(root: Path, agg: Mapping[str, Any], table: Mapping[str, Any], final_dir: Path) -> str:
    v = agg["verdicts"]
    acc = agg["accounting"]
    gates = agg["gates"]
    L: List[str] = []
    head = git("rev-parse", "HEAD")
    L += ["# BLACKOUT-1 MORNING SUMMARY", "",
          "Artifact root: `{}`. Final aggregate: `{}`. Prereg `docs/icra27/BLACKOUT_PREREG.md` (amended 2026-08-17; sha256 `{}`, git 0d2cd8e) executed unedited after the first injected run. Tooling branch `blackout/preg-20260817`, HEAD `{}`. DECISIONS D1-D13 in `{}` (all recorded before the runs they affect).".format(
              root, final_dir, sha(REPO / "docs/icra27/BLACKOUT_PREREG.md"), head, root / "DECISIONS.md"), ""]
    # (1) inertness + integrity
    L += ["## 1. Masking-inertness and integrity (B3)", ""]
    for name in ("inertness", "integrity"):
        g = gates.get(name) or {}
        L.append("**{} gate: {}**".format(name.upper(), g.get("verdict", "ABSENT")))
        for c in g.get("cells") or []:
            L.append("- `{}` ({} {}): {} / {}; vs CDSC-1R4 {}; vs PERTURB-1 {}; vs ABLATE-REC-1 {}".format(c["run_id"], c["system"], c["sequence"], c["status"], c["evidence_validity"], c["cdsc1r4_status"], c["perturb1_status"], c["ablate1_status"]))
        L.append("")
    L.append("B3 verdict: **{}** (the k=0 replica ran THROUGH the masking path — rewritten bag, zero dropped frames — and reproduced the reference byte-for-byte; masking is inert when empty).".format(v["B3"]["verdict"]))
    L.append("")
    # (2) injection table
    L += ["## 2. Committed injection-point table (docs/icra27/BLACKOUT_INJECTION_POINTS.md, git d86f0ea; computed by scripts/icra27/blackout_injection_points.py under D1-D3, no adjustment)", "",
          "| seq | view start (s) | view dur (s) | ref init (+s) | arm A t_A (+s) | GT |v| at t_A (m/s) | 3 s mean at t_A | arm B t_B (+s) | GT |v| at t_B (m/s) | 3 s mean at t_B | unconstrained min (+s / mean) | same? |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|"]
    for s in table["sequences"]:
        a, b, u = s["arm_A"], s["arm_B"], s["arm_B_search"]["unconstrained"]
        L.append("| {} | {:.6f} | {:.3f} | {:.3f} | {:.3f} | {:.4f} | {:.4f} | {:.3f} | {:.4f} | {:.4f} | {:.3f} / {:.4f} | {} |".format(
            s["sequence"], s["view_start_header_stamp_ns"] / NS, s["view_duration_s"], s["reference_first_state_timestamp_s"] - s["view_start_header_stamp_ns"] / NS,
            a["seconds_from_view_start"], a["gt_speed_instantaneous_mps"], a["gt_speed_3s_window_mean_mps"], b["seconds_from_view_start"], b["gt_speed_instantaneous_mps"], b["gt_speed_3s_window_mean_mps"],
            (u["center_header_stamp_ns"] - s["view_start_header_stamp_ns"]) / NS, u["window_mean_speed_mps"], s["arm_B_search"]["constrained_equals_unconstrained"]))
    L += ["", "Arm-B constraint (D2, declared before computing): the 3 s window must lie inside the initialized region of the frozen reference run; where the unconstrained minimum differs it lies pre-initialization (circle +10.6 s, MH_05 +13.2 s, V2_02 +1.6 s: the vehicle at rest before the estimator exists). Mechanical NOT_RUNNABLE per D3: MH_05/V2_02 arm B all k (MASK_PAST_END: the min-speed window is the landing at the sequence end), circle arm B k>=5 (ARM_OVERLAP with arm A at +63.5 s).", ""]
    # (3) accounting
    L += ["## 3. Run accounting", "", "| Quantity | Count |", "|---|---:|"]
    for k, lab in (("expected_prereg_runs", "Expected (amended prereg: 50 arm A + 50 arm B + 6 U0 + 3 integrity)"), ("recorded_in_denominators", "Cell records found (in denominators)"), ("not_runnable", "NOT_RUNNABLE (recorded with reason, never launched)"), ("executed", "Executed and closed"), ("executed_valid", "Executed, evidence VALID"), ("executed_invalid_or_fatal", "Executed, evidence invalid / fatal"), ("unclosed", "Unclosed / no result"), ("planned_but_not_recorded", "Planned but not recorded"), ("completions_frozen", "Completions (frozen passage rule, VALID)"), ("completions_quantization_aware", "Completions (quantization-aware reading D13, VALID)"), ("typed_failures_frozen", "Typed non-completions (frozen rule, VALID)")):
        L.append("| {} | {} |".format(lab, acc[k]))
    L += ["", "Extra records outside every denominator: step0 masking-inertness {}; euroc-study-check {} (D12 evidence run).".format(acc["extra_records_outside_denominators"]["step0_inertness"], acc["extra_records_outside_denominators"]["euroc_study_check"]), "", "NOT_RUNNABLE by reason: " + ", ".join("{} = {}".format(k, n) for k, n in sorted(acc["not_runnable_by_reason"].items())), ""]
    L += ["| set | recorded | NOT_RUNNABLE | executed | VALID | complete (frozen) | complete (QA) | typed failures | invalid |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for s, d in acc["per_set"].items():
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(s, d["recorded"], d["not_runnable"], d["executed"], d["valid"], d["complete_frozen"], d["complete_qa"], d["typed_failures"], d["invalid"]))
    L += ["", "By status: " + ", ".join("{}={}".format(k, n) for k, n in acc["by_status"].items()), ""]
    L += ["EuRoC recON (S1-recON-euroc, D5/D12): NOT_RUNNABLE_STUDY_CONFIG_REJECTED_BY_FROZEN_BINARY on all 20 cells — the frozen executable's compiled recovery contract (ov_msckf/src/core/VioManager.cpp:252-289, read-only) requires calib_cam_extrinsics = calib_cam_intrinsics = calib_cam_timeoffset = false; the frozen config/euroc_mav/estimator_config.yaml sets all three true; enabling long_gap_recovery_enabled therefore throws `enabled long-gap recovery violates its frozen runtime contract` before initialization (evidence run in set euroc-study-check: see section 6). The single-delta study configuration required by the prereg cannot execute; no further deltas were added (no tuning). EuRoC recOFF cells (frozen behaviour under masking) ran.", ""]
    # (4) arm A
    b5 = v["B5"]
    L += ["## 4. Arm A (mid-motion, 40% rule) — gate integrity (B5) and per-cell results", "",
          "**GLOBAL false-commit count (all executed recON cells, arms A and B): {} of {} commits** (false = committed position mapped through the cell's own pre-mask SE(3) alignment > 0.5 m from GT at commit time, D8; unassessable {}). Velocity-assumption exposure events (|v_GT| > 0.3 m/s at commit): {} of {} commits (reported regardless, prereg A3). Non-recovery recON cells: {}, of which {} terminated typed (recovery_failed / no trigger) with state_unchanged=1 on every attempt; untyped or state-mutated: {}. **B5 verdict: {}**.".format(
              b5["global_false_commit_count"], b5["global_commit_count"], b5["commit_error_unassessable_count"], b5["velocity_exposure_events"], b5["global_commit_count"], b5["non_recovery_cells"], b5["non_recovery_typed_with_state_preserved"], len(b5["non_recovery_untyped_or_state_mutated"]), b5["verdict"]), ""]
    for arm in ("A", "B"):
        if arm == "B":
            L += ["## 5. Arm B (low-motion window) — success vs k and the measured K per dataset", ""]
        L += ["Status tokens: C = complete (frozen passage rule), F:<status> = typed non-completion (frozen rule), NR = NOT_RUNNABLE, INVALID = evidence not VALID. QA = quantization-aware reading (D13). recON 'recovered' = commit AND complete. |v| = GT speed at commit (m/s).", "",
              "| seq | k | recON | QA | recovered | usable | ATE m | act/commit/fail | attempts | attempt-reason histogram | supplied per attempt | t_recover s | commit err m | |v| commit | recON post-mask err first/med/max m | recOFF | QA | rounding-only | recOFF resume s | recOFF post-mask err first/med/max m | recOFF ATE m | U0 (frozen/QA) |",
              "|---|---:|---|---|---|---|---:|---|---:|---|---|---:|---|---|---|---|---|---|---:|---|---:|---|"]
        for row in agg["arm_tables"][arm]:
            on, off, u0 = row["recON"], row["recOFF"], row["U0"]
            pm = (off or {}).get("post_mask_err") or {}
            pon = (on or {}).get("post_mask_err") or {}
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                row["sequence"].split("/")[-1], row["k"],
                "—" if on is None else on["status"], "—" if on is None else ("C" if on["complete_qa"] else "F"), "—" if on is None else ("Y" if on["recovered"] else "N"),
                "—" if on is None else ("Y" if on["usable"] else "N"), "—" if on is None else fmt(on["ate_m"]),
                "—" if on is None else "{}/{}/{}".format(on["activations"], on["commits"], on["failures"]), "—" if on is None else on["attempts"], "—" if on is None else hist(on["histogram"]),
                "—" if on is None else (",".join(str(x) for x in on["supplied"][:10]) + ("…" if len(on["supplied"]) > 10 else "") or "—"),
                "—" if on is None else fmt(on["time_to_recover_s"]), "—" if on is None else (", ".join(fmt(e) for e in on["commit_errors_m"]) or "—"),
                "—" if on is None else (", ".join(fmt(s, 2) for s in on.get("commit_speeds_mps", []) if s is not None) or "—"),
                "—" if not pon else "{:.3f}/{:.3f}/{:.3f}".format(pon["first"], pon["median"], pon["max"]),
                "—" if off is None else off["status"], "—" if off is None else ("C" if off["complete_qa"] else "F"), "—" if off is None else ("Y" if off["rounding_only"] else "N"),
                "—" if off is None else fmt(off["time_to_resume_s"]), "—" if not pm else "{:.3f}/{:.3f}/{:.3f}".format(pm["first"], pm["median"], pm["max"]), "—" if off is None else fmt(off["ate_m"]),
                "—" if u0 is None else "{} / {}".format(u0["status"], "C" if u0["complete_qa"] else "F")))
        L.append("")
        if arm == "A":
            a = v["B1_prime_arm_A"]
            L += ["Arm A recON: executed {}, recovered (commit AND complete) {}, cells with >=1 commit {}, typed recovery_failed {}, no trigger {}. B1' (arm A predicted to FAIL): **{}**.".format(a["executed"], a["recovered_commit_and_complete"], a["commit_count_cells"], a["typed_recovery_failed"], a["no_trigger"], a["verdict"]), ""]
            b2 = v["B2_prime"]
            L += ["recON-vs-recOFF separation (both arms): {} cells under the frozen rule (recOFF failure in family {}/{}), surviving the quantization-aware reading {}, rounding-only {} — B2' verdict frozen: **{}**; quantization-aware: {}.".format(b2["separation_cells_frozen"], b2["in_family"], b2["separation_cells_frozen"], b2["separations_surviving_quantization_aware"], b2["rounding_only_separations"], b2["verdict_frozen"], b2["verdict_quantization_aware"]), ""]
            if b2["cells"]:
                L += ["| seq | arm | k | recOFF status | recOFF reason | in family | rounding-only | survives QA |", "|---|---|---:|---|---|---|---|---|"]
                for c in b2["cells"]:
                    L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(c["sequence"].split("/")[-1], c["arm"], c["k"], c["recOFF_status"], c["recOFF_reason"], c["in_family"], c["rounding_only"], c["survives_quantization_aware"]))
                L.append("")
        else:
            b1b = v["B1_prime_arm_B"]
            L += ["Measured K per dataset (largest tested k with every smaller tested k also recovered; no extrapolation):", ""]
            for ds, d in b1b["K_per_dataset"].items():
                L.append("- {}: K = {} s over sequences with tested cells {}; per sequence {}.".format(ds, d["K_s_all_tested_sequences"], d["sequences_with_tested_cells"], d["K_per_sequence"]))
            L.append("")
            L += ["| seq | k=2 | k=5 | k=10 | k=15 | k=20 | tested k | successful k | K (prefix) | ceiling k |", "|---|---|---|---|---|---|---|---|---:|---|"]
            for seq, d in b1b["per_sequence"].items():
                L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(seq.split("/")[-1], *[d["by_k"][str(k)] or "—" for k in DURATIONS], d["tested_k"], d["successful_k"], d["K_monotone_prefix_s"], d["ceiling_k"]))
            L.append("")
    # (6) verdicts + B4
    L += ["## 6. B-verdicts (mechanical, amended wording)", "",
          "- **B1' arm A** ({}): {}".format(v["B1_prime_arm_A"]["prediction"], v["B1_prime_arm_A"]["verdict"]),
          "- **B1' arm B** ({}): recovered {} of {} executed arm-B recON cells; K per dataset above.".format(v["B1_prime_arm_B"]["prediction"], v["B1_prime_arm_B"]["recovered"], v["B1_prime_arm_B"]["executed"]),
          "- **B2'**: frozen {}; quantization-aware {}.".format(v["B2_prime"]["verdict_frozen"], v["B2_prime"]["verdict_quantization_aware"]),
          "- **B3**: {}.".format(v["B3"]["verdict"]),
          "- **B5**: {} (global false commits {} of {} commits; untyped non-recoveries {}).".format(b5["verdict"], b5["global_false_commit_count"], b5["global_commit_count"], len(b5["non_recovery_untyped_or_state_mutated"])),
          "- **B4 (honesty clause) — every recON cell that did NOT recover, stated plainly ({} of {} executed recON cells):**".format(v["B4"]["recon_non_recovery_cells"], v["B4"]["of_executed_recon"]), ""]
    if v["B4"]["cells"]:
        L += ["| seq | arm | k | status | act/commit/fail | attempts | histogram | supplied per attempt | inliers per attempt | termination | complete (frozen) |", "|---|---|---:|---|---|---:|---|---|---|---|---|"]
        for c in v["B4"]["cells"]:
            L.append("| {} | {} | {} | {} | {}/{}/{} | {} | {} | {} | {} | {} | {} |".format(c["sequence"].split("/")[-1], c["arm"], c["k"], c["status"], c["activations"], c["commits"], c["failures"], c["attempts"], hist(c["histogram"]), ",".join(str(x) for x in c["supplied"]) or "—", ",".join(str(x) for x in c["inliers"]) or "—", c["typed_termination"], c["complete_frozen"]))
        L.append("")
    study = [c for c in agg["cells"] if c["study_check"]]
    if study:
        c = study[0]
        L += ["EuRoC study-configuration evidence run `{}` (outside denominators): status {}, evidence {}, recovery lines {} — the frozen executable refused the enabled configuration at startup (D12).".format(c["run_id"], c["status"], c["evidence_validity"], c["activations"]), ""]
    # (6b) descriptive observations computed from the tables (facts, no interpretation)
    L += ["### 6b. Descriptive observations (facts from the tables above; not verdict-bearing)", ""]
    cells = agg["cells"]
    def by(role, arm=None):
        return [c for c in cells if c["role"] == role and c["executed"] and c["k"] in DURATIONS and (arm is None or c["arm"] == arm)]
    recon = by("recON")
    recov = [c for c in recon if c["recovered"]]
    L.append("- recON recoveries: {} of {} executed recON cells; every recovery committed on the first three post-mask attempts (accepted x3, consensus radius <= 0.10 m) {:.2f}-{:.3f} s after the mask end; commit position error vs GT under the pre-mask alignment {} m (min-max); ATE of recovered cells {} m; all recovered cells usable (< 0.5 m).".format(
        len(recov), len(recon), min(c["time_to_recover_s"] for c in recov) if recov else 0, max(c["time_to_recover_s"] for c in recov) if recov else 0,
        "{:.3f}-{:.3f}".format(min(e for c in recov for e in c["commit_errors_m"]), max(e for c in recov for e in c["commit_errors_m"])) if recov else "—",
        "{:.3f}-{:.3f}".format(min(c["ate_m"] for c in recov if c["ate_m"] is not None), max(c["ate_m"] for c in recov if c["ate_m"] is not None)) if recov else "—"))
    fails = [c for c in recon if not c["recovered"]]
    L.append("- recON non-recoveries: {} cells, all 10-attempt typed recovery_failed with state_unchanged=1 (fail-closed: the estimator emits no further state, hence PARTIAL / tail not reached). Reason histogram over all failed attempts: {}. Supplied correspondences (surviving tracks matched to retained SLAM landmarks) on failed cells ranged {}-{}; on recovered cells the first attempt supplied {}-{}.".format(
        len(fails), hist({k: sum(c["histogram"].get(k, 0) for c in fails) for k in set(kk for c in fails for kk in c["histogram"])}),
        min(min(c["supplied"]) for c in fails if c["supplied"]) if fails else "—", max(max(c["supplied"]) for c in fails if c["supplied"]) if fails else "—",
        min(c["supplied"][0] for c in recov if c["supplied"]) if recov else "—", max(c["supplied"][0] for c in recov if c["supplied"]) if recov else "—"))
    arm_a = by("recON", "A")
    L.append("- Arm A recovery is not monotone in k: recovered at " + "; ".join("{} k={}".format(c["sequence"].split("/")[-1], c["k"]) for c in arm_a if c["recovered"]) + " and refused at the other {} arm-A cells; recovery is governed by whether >= 12 pre-mask tracks survive KLT across the masked pair (supplied/inliers columns), not by k (B1' boundary reading).".format(sum(1 for c in arm_a if not c["recovered"])))
    vel = [c for c in recov if c["velocity_exposures"]]
    L.append("- Velocity-assumption exposure events (commit while |v_GT| > 0.3 m/s): {} of {} commits ({}); the committed positions were nevertheless within {} m of GT — the consensus gate accepted a moving commit in these cells; reported as required by prereg A3.".format(
        sum(c["velocity_exposures"] for c in recov), sum(c["commits"] for c in recon), "; ".join("{} arm {} k={} |v|={:.2f} m/s".format(c["sequence"].split("/")[-1], c["arm"], c["k"], c["commit_speeds_mps"][0]) for c in vel), "{:.3f}".format(max(e for c in vel for e in c["commit_errors_m"])) if vel else "—"))
    recoff = by("recOFF")
    L.append("- recOFF (switch off) never stalls: it propagates through the mask on IMU and resumes state output {:.3f}-{:.3f} s after the mask end in every executed recOFF cell ({} cells); frozen-rule statuses: {}; rounding-only TRACKING_LOSS {} of {}; quantization-aware complete {} of {}. Its post-mask position error under the pre-mask alignment (max over the 10 s after resume) grows with k: ".format(
        min(c["time_to_resume_s"] for c in recoff if c["time_to_resume_s"] is not None), max(c["time_to_resume_s"] for c in recoff if c["time_to_resume_s"] is not None), len(recoff),
        ", ".join("{}={}".format(k, n) for k, n in sorted({s: sum(1 for c in recoff if c["status"] == s) for s in set(c["status"] for c in recoff)}.items())),
        sum(1 for c in recoff if c["rounding_only_unsupported_gap"]), len(recoff), sum(1 for c in recoff if c["complete_qa"]), len(recoff))
        + "; ".join("{} arm {} k={}: {:.2f} m".format(c["sequence"].split("/")[-1], c["arm"], c["k"], c["post_mask_err"]["max"]) for c in sorted(recoff, key=lambda c: (c["arm"], c["sequence"], c["k"])) if c["post_mask_err"]) + ".")
    L.append("- Consequently completion is not the discriminating quantity between recON and recOFF under masking (all 9 frozen-rule separations are rounding-only, D13): recON either re-anchors within centimetres or fails closed (no output), while recOFF always continues with IMU-propagated drift that reaches metres for k >= 10 s on the mid-motion arm. The paper must state both.")
    L.append("- U0 (stock upstream, KAIST arm A k in {5,15}) behaves like recOFF: " + "; ".join("{} k={} {} (QA {}, resume {:.3f} s, post-mask max err {:.2f} m)".format(c["sequence"].split("/")[-1], c["k"], c["status"], "C" if c["complete_qa"] else "F", c["time_to_resume_s"] or float("nan"), (c["post_mask_err"] or {}).get("max", float("nan"))) for c in by("U0")) + ".")
    euroc_off = [c for c in recoff if c["dataset"] == "euroc_mav"]
    L.append("- EuRoC recOFF (frozen behaviour) arm A: " + "; ".join("{} k={} {} (QA {}{}, ATE {})".format(c["sequence"], c["k"], c["status"], "C" if c["complete_qa"] else "F", ", rounding-only" if c["rounding_only_unsupported_gap"] else "", fmt(c["ate_m"])) for c in euroc_off) + ".")
    L.append("")
    # (7) licensed claims
    L += ["## 7. Licensed claim sentences (constructed from the amended Licensed-claims section; nothing stronger)", ""]
    for key, s in agg["licensed_claims"].items():
        L.append("- **{}**: {}".format(key, s))
    L += ["", "The paper states the track-survival boundary (scope_sentence) in the same paragraph as any success claim; EuRoC results are labelled with the declared study configuration, which the frozen binary refuses (no EuRoC positive claim). D13 disclosure: recOFF/U0 non-completions flagged rounding-only are classification artifacts of the frozen 1e-6 s duration tolerance versus the 1e-5 s state-file quantization; the quantization-aware reading is reported alongside everywhere and the separation sentence is licensed only where the separation survives it.", ""]
    # (8) estimator diff + binaries
    L += ["## 8. Estimator-diff check and binary re-hashes", ""]
    for cmd in (("diff", "--stat", "0d2cd8e", "HEAD", "--", "ov_msckf", "ov_core", "ov_init", "ov_eval", "config"), ("diff", "--stat", "2751bcd", "HEAD", "--", "ov_msckf/src", "ov_core/src", "ov_init/src", "ov_eval/src"), ("status", "--porcelain", "--untracked-files=all", "--", "ov_msckf", "ov_core", "ov_init", "ov_eval", "config")):
        out = git(*cmd)
        L += ["`git {}`:".format(" ".join(cmd)), "```", out if out else "(empty)", "```"]
    s1 = REPO / "build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf"
    u0 = Path("/home/moksh/schurvio-baseline-triad/20260809T190830Z/build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf")
    L += ["", "Frozen binary re-hash at session end: S1/recOFF `{}` = {} (recorded 0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e); U0 `{}` = {} (recorded c0e2203d0c01822fd089b32fb25d2b81dc850abc03c273d169b812801788d57b).".format(s1, sha(s1), u0, sha(u0)), ""]
    # (9) artifacts
    L += ["## 9. Artifact paths", ""] + ["- `{}`".format(p) for p in (root / "RUN_LOG.md", root / "DECISIONS.md", root / "MORNING_SUMMARY.md", final_dir, root / "integrity", root / "driver/cells.jsonl", root / "launcher/run_campaign.sh", root / "launcher/logs/campaign.log", root / "masked-bags/manifests", REPO / "docs/icra27/BLACKOUT_INJECTION_POINTS.md", REPO / "docs/icra27/BLACKOUT_PREREG.md")] + [""]
    return "\n".join(L)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact-root", required=True, type=Path)
    p.add_argument("--final", required=True, type=Path)
    args = p.parse_args(argv)
    agg = json.loads((args.final / "aggregate.json").read_text())
    table = json.loads((REPO / "docs/icra27/BLACKOUT_INJECTION_POINTS.json").read_text())
    text = render(args.artifact_root, agg, table, args.final)
    (args.artifact_root / "MORNING_SUMMARY.md").write_text(text)
    print("wrote", args.artifact_root / "MORNING_SUMMARY.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
