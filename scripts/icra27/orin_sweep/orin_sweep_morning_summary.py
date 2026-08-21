#!/usr/bin/python3.8
"""Compose docs/icra27/orin-sweep/MORNING_SUMMARY.md from the aggregate, the board logs and the session-end checks."""
import argparse, json, subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
GRIDS = sorted({50, 75, 100, 150, 200, 300})
F = 200
SCOPE = ["ov_msckf", "ov_core", "ov_init", "ov_eval", "config"]


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw).stdout.strip()


def fmt(v, d=2):
    return "—" if v is None else ("%.*f" % (d, v))


def mi(t, d=2):
    return "—" if not t or t[0] is None else "%s [%s, %s] (n=%d)" % (fmt(t[0], d), fmt(t[1], d), fmt(t[2], d), t[3])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agg", required=True, type=Path); ap.add_argument("--mirror", required=True, type=Path)
    ap.add_argument("--stamp", required=True); ap.add_argument("--base-commit", default="d4d3134")
    ap.add_argument("--accounting", required=True, type=Path, help="JSON with planned/executed/truncation narrative")
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args()
    A = json.load(open(a.agg / "aggregate.json")); T = (a.agg / "tables.md").read_text()
    acct = json.load(open(a.accounting))
    step0 = (REPO / "docs/icra27/orin-sweep/STEP0_TABLE.md").read_text()
    gate = (a.mirror / "INTEGRITY_GATE.txt").read_text() if (a.mirror / "INTEGRITY_GATE.txt").is_file() else "MISSING"
    # session-end checks
    diff_desktop = sh("cd %s && git diff --stat %s HEAD -- %s" % (REPO, a.base_commit, " ".join(SCOPE)))
    diff_desktop_wt = sh("cd %s && git status --porcelain -- %s" % (REPO, " ".join(SCOPE)))
    head = sh("cd %s && git rev-parse --short HEAD" % REPO)
    board = sh("ssh orin 'cd /data/schurvio-lite-frozen && git rev-parse HEAD && git status --porcelain -- %s | wc -l && git diff --stat -- %s | tail -1; sha256sum build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf; swapon --show | wc -l; sudo nvpmodel -q | head -1'" % (" ".join(SCOPE), " ".join(SCOPE)))
    desktop_bin = sh("sha256sum %s/build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf 2>/dev/null | cut -c1-64" % REPO)
    C = A["counts"]; B = A["bstar"]; c1, c2, c3 = A["CL1"], A["CL2"], A["CL3"]
    gate_cells = len([d for d in (a.mirror / "cells").iterdir() if d.name.startswith("gate-")])
    executed = C["cells_total"] + gate_cells
    L = ["# ORIN-SWEEP-1 — MORNING_SUMMARY (ledger F3) — root `orin-sweep-%s`" % a.stamp, "",
         "Prereg `docs/icra27/SWEEP_PREREG.md` (`%s`), frozen at the first sweep run (2026-08-20T14:22:54Z). Board: Orin Nano, nvpmodel mode 0 (15 W), env manifest `dc72a2f`, binary `b9f4f034…`. Desktop branch `orin/sweep-20260820`, HEAD `%s`." % (a.base_commit, head), "",
         "## (1) Step-0 table and pre-flight", "", step0.split("# ORIN-SWEEP-1 — Step-0 table (committed BEFORE any sweep run)", 1)[-1].strip(), "",
         "## (2) Integrity gate", "", "```", gate.strip(), "```", "",
         "Additionally, the first sweep cell `kaist-rotation_fast-S1-b200-r1` (budget and mode passed through the sweep launch as ROS overrides) reproduced the C5 reference `state_estimate.txt` byte-for-byte (`72236fee…`), proving the override path ≡ the frozen YAML path.", "",
         "## (3) Run accounting", "", acct["narrative"], "",
         "| planned | executed (gate + sweep cells present) | COMPLETED (sweep) | FAILED | INVALID_THERMAL | delta-unverified | truncated (planned − executed) |", "|---|---|---|---|---|---|---|",
         "| %d | %d (%d + %d) | %d | %d | %d | %d | %d |" % (acct["planned"], executed, gate_cells, C["cells_total"], C["completed"], C["failed"], C["invalid_thermal"], C["delta_unverified"], acct["planned"] - executed), "",
         "## (4) B* table (B* := largest grid budget whose median deadline compliance across repeats ≥ 95 %; NONE if no budget qualifies)", ""]
    L.append(T.split("### B* table")[1].strip()); L.append("")
    L += ["KAIST extension rule ({35, 25}) fires: **%s**." % A["kaist_extension_rule_fires"], "",
          "## (5) The four curve tables (medians [IQR] over repeats; n = valid runs)", "", T.split("### B* table")[0].strip(), "",
          "Energy convention (verbatim, C5): %s Fresh idle baseline tonight: %s mW (reported alongside; not used in any number)." % (A["energy_convention"], fmt(A["fresh_idle_mean_mW"], 0)), "",
          "## (6) Clause verdicts", "",
          "**CL-1 (budget)** — B*(S1) exceeds B*(N0) by ≥ 1 grid step on ≥ 1 family: **%s**" % c1["true"], "```", json.dumps(c1["detail"], indent=1), "```",
          "**CL-2 (tail latency)** — median-over-repeats p99(S1) ≤ 0.85 × p99(N0) at F on ≥ 2 sequences: **%s** (%d sequences fire)" % (c2["true"], c2["sequences_firing"]), "```", json.dumps(c2["sequences"], indent=1), "```",
          "**CL-3 (energy)** — energy/update(S1) ≤ 0.80 × energy/update(N0) at F on ≥ 2 sequences: **%s** (%d sequences fire)" % (c3["true"], c3["sequences_firing"]), "```", json.dumps(c3["sequences"], indent=1), "```", "",
          "### F3 status: **%s**%s" % (A["F3"], (" (clause%s %s)" % ("s" if len(A["F3_clauses_true"]) > 1 else "", ", ".join(A["F3_clauses_true"]))) if A["F3_clauses_true"] else " — all three clauses false; the thesis bracket resolves to exact-equivalence-only; the operating-point characterization in (4)–(5) stands on its own"), "",
          "## (7) Licensed sentence", "", acct["licensed_sentence"], "",
          "## (8) Session-end hard-rule checks", "",
          "- Desktop estimator diff `git diff --stat %s HEAD -- %s`: %s" % (a.base_commit, " ".join(SCOPE), ("EMPTY" if not diff_desktop else diff_desktop)),
          "- Desktop working tree in scope: %s" % ("clean" if not diff_desktop_wt else diff_desktop_wt),
          "- Board frozen tree (`git rev-parse HEAD`, dirty-in-scope count, diff, binary sha256, swap entries, power mode):", "```", board, "```",
          "- Desktop x86 binary (contrast only, never a target): `%s`" % (desktop_bin or "n/a"),
          "- Max cpu-thermal over all cells: %.1f °C; max tj: %.1f °C; throttle (INVALID_THERMAL) cells: %d %s" % (A["max_cpu_C_overall"], A["max_tj_C_overall"], len(A["throttle_cells"]), A["throttle_cells"]), "",
          "## (9) Artifact paths", "",
          "- Board: `/data/orin-sweep-%s/` (cells/, power/, RUN_LOG.md, runs.jsonl, QUEUE.tsv, INTEGRITY_GATE.txt, driver.log, state/)" % a.stamp,
          "- Desktop mirror: `%s`" % a.mirror,
          "- Aggregate: `%s` (aggregate.json, cells_universal.csv, tables.md, accuracy_work/)" % a.agg,
          "- Docs: `docs/icra27/orin-sweep/` (STEP0_TABLE.md, DECISIONS.md, RUN_LOG.md, MORNING_SUMMARY.md); tooling `scripts/icra27/orin_sweep/`", ""]
    a.out.write_text("\n".join(L))
    print("wrote", a.out)


if __name__ == "__main__":
    sys.exit(main())
