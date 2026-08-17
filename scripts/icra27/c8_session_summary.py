#!/usr/bin/env python3
"""Render SESSION_SUMMARY.md for the C8 desktop-evidence session from the
artifact root's machine-readable outputs (Item 1 aggregate, Item 2 anatomy,
Item 3 paper-numbers). Pure renderer: reads, never recomputes."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/moksh/schurvio-lite-rotation-robustness")
BIN = REPO / "build" / "cp0-ws" / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
LIB = REPO / "build" / "cp0-ws" / "devel" / "lib" / "libov_msckf_lib.so"
FROZEN_BIN = "0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e"


def sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def load(p: Path):
    return json.load(open(p)) if p.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--base-commit", default="f4e1652")
    args = ap.parse_args()
    root = Path(args.root)
    fm = load(root / "aggregate" / "fault_matrix.json")
    pn_dir = root / "item3" / "paper-numbers"
    golden = load(pn_dir / "golden_regression.json")
    manifest = load(pn_dir / "MANIFEST.json")
    verd = load(pn_dir / "verdicts.json")
    head = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    diff = subprocess.run(["git", "-C", str(REPO), "diff", "{}..HEAD".format(args.base_commit), "--", "ov_msckf", "ov_core", "ov_init", "ov_eval", "config"], capture_output=True, text=True).stdout
    md = ["# C8 SESSION_SUMMARY — DESKTOP_EVIDENCE_SESSION v2 (fault injection + stall anatomy + unified paper numbers)", "",
          "Rendered {} from `{}`; branch evidence/c8-20260817 at {} (base {}).".format(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), root, head, args.base_commit), ""]
    # ---- Item 1
    md += ["## 1. Item 1 — H4 fault-injection matrix (ledger F4)", ""]
    if fm:
        v = fm["verdict"]
        md += ["- Inertness gate (frozen binary + shim, no injection): **{}/{} BYTE_IDENTICAL** to CDSC-1R4 S1 (state_estimate + state_deviation) -> {}".format(fm["inertness"]["byte_identical"], fm["inertness"]["n"], v["inertness_gate"]),
               "- Runs observed vs expected: {} vs {} (complete={})".format(fm["observed_runs"], fm["expected_runs"], fm["complete"]),
               "- Cycle pass: {} runs, {} update invocations, {} eligible; callbacks with >1 successful commit {}/{}; exceptions escaping update() {}/{}".format(
                   fm["cycle"]["totals"]["runs"], fm["cycle"]["totals"]["invocations"], fm["cycle"]["totals"]["eligible_invocations"], fm["cycle"]["totals"]["multi_commit_callbacks"], fm["cycle"]["totals"]["invocations"], fm["cycle"]["totals"]["exceptions_escaped"], fm["cycle"]["totals"]["invocations"]), "",
               "| class | scheduled | reached | realized | typed console lines | ineffective (contained) | violations (all columns) |", "|---:|---:|---:|---:|---:|---:|---:|"]
        for m in fm["cycle"]["matrix"]:
            viol = m["viol_state_mutated_without_commit"] + m["viol_commit_after_realized_transaction_fault"] + m["viol_event_reports_commit"] + m["viol_restore_mismatch"] + m["viol_multi_commit"] + m["viol_exception"]
            md.append("| {} {} | {} | {} | {} | {} | {} | {} |".format(m["class"], m["class_name"], m["scheduled"], m["reached_injection_point"], m["fault_realized"], m["typed_console_lines"], m["ineffective_containment_restore"], viol))
        c7 = fm["class7"]["summary"]
        md += ["", "- Class 7a (every console write fails, whole run): {}/{} runs outcome unaffected (trajectory BYTE_IDENTICAL); failed writes lower bound {}".format(c7["7a_unaffected"], c7["7a_runs"], c7["7a_failed_writes_lower_bound_total"]),
               "- Class 7b (TurnSafe T0 diagnostics-stream fault at each stage x kind): {}/{} runs outcome unaffected".format(c7["7b_unaffected"], c7["7b_runs"]),
               "- **H4 all-zero violation columns: {}** (violations={}, class-7 affected runs={}, every class >=100 realized: {})".format(v["H4_all_zero_violation_columns"], v["violations_total"], v["class7_affected_runs"], v.get("per_class_min_realized_ge_100")), ""]
        nat = fm.get("natural_incidents")
        if nat:
            md += ["Natural-incident reconciliation (console.log scrape of all campaign roots; denominators = console files / published state rows):", "",
                   "| campaign | console files | state rows | PREFLIGHT | SCHUR | REDUCTION nonfinite | COMMIT rejected | PRIOR | CP2 | EKFUpdate hard exit | GATE (ordinary chi2) |", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
            for cid, r in nat["roots"].items():
                c = r["counts"]
                md.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(cid, r["console_files"], r["state_rows_total"], c.get("[MSCKF-PREFLIGHT]", 0), c.get("[MSCKF-SCHUR]", 0), c.get("[MSCKF-REDUCTION]: feature", 0), c.get("[MSCKF-COMMIT]", 0), c.get("[MSCKF-PRIOR]", 0), c.get("[MSCKF-CP2]", 0) + c.get("[MSCKF-CP2-SHADOW]", 0) + c.get("[MSCKF-CP2-OBSERVER]", 0), c.get("StateHelper::EKFUpdate() - diagonal at", 0), c.get("[MSCKF-GATE]", 0)))
            md.append("")
        md += ["Full tables: `aggregate/FAULT_MATRIX.md`, `aggregate/fault_matrix.csv`, `aggregate/inertness.csv`, `aggregate/class7.csv`, `aggregate/natural_incidents.csv`.", ""]
    else:
        md += ["Item 1 aggregate not present (campaign still running or aggregator not run).", ""]
    # ---- Item 2
    md += ["## 2. Item 2 — stall anatomy (ledger F14)", ""]
    sa = root / "item2" / "stall_anatomy.md"
    if sa.is_file():
        txt = sa.read_text()
        start = txt.find("**Reading:**")
        md += [txt[start:txt.find("\n", start)] if start >= 0 else "", "",
               "- recOFF: state resumes at the first post-gap exact-header pair (state ts = pair stamp - 0.029959 s); state gap 13.442000 s vs pair-level input gap 13.441997 s (delta 2.86e-6 s -> frozen rule fails, D13 passes: rounding-only).",
               "- U0: aborts (class_loader::LibraryUnloadException / SIGABRT, mid-console-line) at the first post-gap frame in 5/5 frozen U0 rotation cells; trigger not observable at INFO.",
               "- recON: trigger 1599131279.832099, 3/3 accepted state-unchanged attempts, consensus, commit 1599131279.897871, first resumed state 1599131279.86791 (+0.066 s after the pair-level gap end), warmup 1599131280.100363.",
               "- Adjudication: per-frame update rejection REFUTED (no frames in the gap); ZUPT REFUTED (try_zupt=false both builds, 0 lines); transaction/propagation coupling NOT SUPPORTED; shared publish-on-camera-callback behaviour SUPPORTED; U0 abort SUPPORTED as U0's terminal cause (trigger undetermined). Per-frame feature counts are not extractable from INFO logs.",
               "- The mechanism's substantive effect is post-gap accuracy, not resumption: recOFF 4.2577 m ATE @ 85.95 % coverage vs recON 0.0878 m @ 85.9 % (Item 3 accuracy_coverage.csv).", "",
               "Full report: `item2/stall_anatomy.md` (+ `three_regimes.csv`, `cells_summary.csv`, per-cell timelines, `bag_anatomy.json`).", ""]
    # ---- Item 3
    md += ["## 3. Item 3 — `make paper-numbers` (regeneration + ledger)", ""]
    if golden:
        md += ["- Golden-sample regression (frozen aggregators re-run from their tooling commits, namespaced clone): " + ", ".join("{}={} ({} of {} files identical modulo generated_utc, tool commit {})".format(c, r["golden"], r["identical_modulo_generated_utc"], r["compared"], r["tool_commit_resolved"][:8]) for c, r in golden.items())]
    if manifest:
        md += ["- Universal cell table: {} cells; pre-BLACKOUT frozen->quantization-aware flips: {} -> {}".format(manifest["cells_total"], len(manifest["flips_pre_blackout"]), "; ".join("{}:{}".format(f["campaign"], f["run_id"]) for f in manifest["flips_pre_blackout"]))]
        cm = pn_dir / "completion_matrix.csv"
        if cm.is_file():
            md += ["", "| campaign | system | cells | complete (frozen) | complete (QA) | gap-bearing | frozen ATE | supplementary ATE |", "|---|---|---:|---:|---:|---:|---:|---:|"]
            for r in csv.DictReader(open(cm)):
                md.append("| {campaign} | {system} | {cells} | {complete_frozen} | {complete_qa} | {gap_bearing} | {with_frozen_ate} | {with_supplementary_ate} |".format(**r))
        md += ["", "- Verdicts: CR {} ; P {} ; B {} ; H {} ; CG {}".format(verd.get("CR"), verd.get("P"), (verd.get("B") or {}) if not isinstance(verd.get("B"), dict) else {k: (v.get("verdict") if isinstance(v, dict) else v) for k, v in verd["B"].items()}, verd.get("H", {}).get("H4") if not isinstance(verd.get("H", {}).get("H4"), dict) else {k: verd["H"]["H4"][k] for k in ("H4_all_zero_violation_columns", "violations_total", "inertness_gate", "complete") if k in verd["H"]["H4"]}, (verd.get("CG") or {}).get("status")) if verd else "- verdicts.json absent",
               "- Ledger: `item3/paper-numbers/claims/ledger.yaml`; manifest: `item3/paper-numbers/MANIFEST.json`; golden hashes: `item3/paper-numbers/GOLDEN.sha256.json` (mirrored to `project/evidence/c8/paper_numbers_GOLDEN.sha256.json`; `make paper-numbers-check`).", ""]
    # ---- hard rules
    md += ["## 4. Hard-rule checks", "",
           "- Estimator source diff `git diff {}..HEAD -- ov_msckf ov_core ov_init ov_eval config`: {} lines (empty={})".format(args.base_commit, len(diff.splitlines()), len(diff.strip()) == 0),
           "- Frozen binary re-hash: `{}` = {} (matches frozen {})".format(BIN, sha(BIN), sha(BIN) == FROZEN_BIN),
           "- Frozen library re-hash: `{}` = {}".format(LIB, sha(LIB)),
           "- Injectors live only in `tools/c8_faultinj/` (LD_PRELOAD shim) + `scripts/icra27/c8_fault_driver.py`; the frozen trial runner was not patched (it refuses LD_PRELOAD by design; the driver reproduces its recipe).",
           "- Yield rule: every compute batch preceded by `scripts/icra27/c8_yield_check.sh` (RUN_LOG lines `yield-check`); no EXT-VF-1 activity was observed during this session.", ""]
    md += ["## 5. Artifact paths", "", "- Root: `{}`".format(root), "- Item 1: `cells/capture`, `cells/cycle4`, `cells/class7a-console`, `cells/class7b-t0-*`, `aggregate/`", "- Item 2: `item2/`", "- Item 3: `item3/paper-numbers/`", "- Logs: `RUN_LOG.md`, `DECISIONS.md`, `launcher/`", ""]
    (root / "SESSION_SUMMARY.md").write_text("\n".join(md) + "\n")
    print(root / "SESSION_SUMMARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
