You are the implementer for ORIN-SWEEP-1, the feature-budget sweep — the final experiment of this project's evidence phase (ledger F3). Execute docs/icra27/SWEEP_PREREG.md exactly. The prereg freezes at the first sweep run; its honesty clauses mean a null result is a complete deliverable.

CONTEXT
Board: ssh orin, passwordless sudo, nvpmodel mode 0 (15 W, frozen), env manifest dc72a2f, Orin frozen binary sha256 b9f4f034… (verify before any run), datasets hash-verified at /data, C5 artifacts at /data/orin-bringup-20260820T131729Z (smoke-cell byte references, timing/power tooling, the frozen energy convention). Desktop repo: /home/moksh/schurvio-lite-rotation-robustness — branch orin/sweep-20260820 from the current campaign HEAD. This session exclusively owns the board; desktop-side work is orchestration and aggregation only.

HARD RULES (unchanged; checked at session end)
Estimator source read-only on both machines (path-scoped diff empty). No tuning beyond the single budget knob and the single elimination flag, both machine-verified per cell. Failures are data. Per-cell SHA256SUMS. Append-only RUN_LOG.md; DECISIONS.md before affected runs. Any thermal throttle event ⇒ cell INVALID_THERMAL, cool-down, one repeat, both recorded.

STEP 0 — BEFORE ANY SWEEP RUN
1. Verify docs/icra27/SWEEP_PREREG.md committed (git log); absent/uncommitted -> STOP_REPORT.md.
2. Read the frozen budget F from the frozen config; commit the Step-0 table: F's value, the knob's parameter name, the full grid, the per-cell single-delta template (budget + elimination flag only).
3. Pre-flight on the board: `sudo -n true`; swapon --show EMPTY; nvpmodel confirms mode 0; jetson_clocks state matches the manifest; tegrastats prints; disk headroom on /data.
4. Integrity gate: rerun the two C5 smoke cells (S1, frozen budget) and byte-compare trajectories to the C5 references. Mismatch -> STOP everything.

STEP 1 — EXECUTION (value-first, serialized on the board)
Order: (a) budgets {F, 100} for both modes on all three KAIST sequences — this brackets the compliance wall fastest; (b) fill {75, 150} then {50, 300} KAIST; (c) EuRoC MH_05 across the grid; (d) identify the compliance-bracketing budgets and top up repeats to 5 there and at F (3 elsewhere). If the prereg's extension rule fires ({35, 25}), record the DECISIONS entry before those runs.
Per cell: full command, resolved budget + elimination values, config hash, per-callback latency trace, deadline ledger at the native period, tegrastats window (timestamp-sliced per the C5 workaround), peak RSS, SoC temps, trajectory + SHA256SUMS.
Graceful truncation if the night runs short (record the cut): EuRoC repeats 5->3 first, then non-bracketing KAIST repeats 5->3, then budget 300, then budget 50. NEVER cut F, 100, or the bracketing budgets.

STEP 2 — AGGREGATION (desktop)
Extend the frozen aggregators on the campaign branch. Emit, per dataset family and mode: p99-vs-budget, compliance-vs-budget, ATE-vs-budget (coverage-welded), energy/update-vs-budget (C5 convention stated verbatim, idle baseline alongside) — the paper's Figure F6 data — plus the B* table and the full universal-table rows. Evaluate CL-1/CL-2/CL-3 mechanically against the prereg wording; medians + IQR; counts with denominators.

STEP 3 — MORNING_SUMMARY.md, in order
(1) Step-0 table (F revealed, grid, knob) and pre-flight results; (2) integrity gate; (3) run accounting: planned vs executed vs INVALID_THERMAL vs truncated, with reasons; (4) the B* table — B*(S1) and B*(N0) per family, with ATE at each B* side-by-side; (5) the four curve tables; (6) CL-1/CL-2/CL-3 verdicts with the driving numbers, and the F3 status line: SUPPORTED (naming the clause) or REFUTED; (7) the licensed sentence assembled per the prereg — nothing stronger, budget-conditional real-time phrasing only, no unqualified "real-time on Orin Nano" anywhere; (8) empty estimator diffs, binary re-hashes both machines, max temperature, throttle count; (9) artifact paths (board + desktop mirrors).

Estimated 150–200 board runs, 6–9 h at 15 W. The board is slow tonight on purpose — that is the experiment. Whatever the clauses say in the morning, say it plainly: this session ends the question, not the argument.
