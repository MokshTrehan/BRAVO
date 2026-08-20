# SWEEP_PREREG — Orin feature-budget sweep (ORIN-SWEEP-1, ledger F3)
Registered 2026-08-20 after C5 acceptance, BEFORE any sweep run. Frozen at first run. Board: Orin Nano, nvpmodel mode 0 (15 W), env manifest dc72a2f, Orin frozen binary sha256 b9f4f034…, CPU-only, GUI off, swap OFF, exclusive board.

## Motivating facts (C5 bring-up)
At the frozen paper-intent budget, KAIST p50 updater latency is 45 ms against a 33.3 ms native period (~89% deadline misses); EuRoC p50 52.5 ms vs 50 ms (~59%). The board operates BEHIND the real-time wall at the frozen budget, so the informative sweep region is downward: the compliance-achieving budget per update mode, and the accuracy it affords.

## Systems
S1 (schur) and N0 (nullspace): same Orin frozen binary, single ROS-parameter delta (`up_msckf_landmark_elimination`), machine-verified per cell. Budget knob: the tracker's max-features parameter verified to round-trip by the C0 plumbing; its frozen value F is read from the frozen config and recorded in the Step-0 table.

## Grid (pre-registered; no post-hoc additions)
Budgets: {50, 75, 100, 150, F, 300}. **Pre-registered extension rule:** iff NO budget achieves ≥95% compliance for EITHER mode on the KAIST family, one downward extension {35, 25} is permitted, recorded in DECISIONS before those runs.
Sequences: KAIST {rotation_fast, circle, square_head} (severity spread; rotation.bag EXCLUDED from the sweep — its 13.4 s outage and recovery window contaminate per-callback timing statistics; noted as a scope decision, not a cut) + EuRoC {MH_05_difficult}.
Repeats: trajectories are byte-deterministic on-board (C5 gate) — 1 trajectory per cell defines accuracy; timing/power dispersion via repeats: 5 at F and at the two compliance-bracketing budgets once identified, 3 elsewhere. Medians + IQR; no significance language.

## Metrics per cell
Per-callback updater latency p50/p95/p99/max; deadline compliance % at the native period (33.3 ms KAIST, 50 ms EuRoC); energy/update using the C5 bring-up's frozen convention (state the convention verbatim in the report, with the 3.04 W idle baseline reported alongside); peak RSS; ATE/RPE coverage-welded; max SoC temperature; throttle events (any throttle ⇒ cell flagged INVALID_THERMAL and repeated once after cool-down).

## Definitions
Compliant budget B*(mode, dataset family) := the largest grid budget whose median deadline compliance across repeats ≥ 95%. If none qualifies, B* = NONE (reportable).

## Pre-registered clauses — ANY ONE TRUE ⇒ F3 SUPPORTED
- **CL-1 (budget):** B*(S1) exceeds B*(N0) by ≥ 1 grid step on ≥ 1 dataset family; the ATE at each mode's B* is reported side-by-side (the accuracy the extra budget buys).
- **CL-2 (tail latency):** median-over-repeats p99(S1) ≤ 0.85 × p99(N0) at budget F on ≥ 2 sequences.
- **CL-3 (energy):** energy/update(S1) ≤ 0.80 × energy/update(N0) at budget F on ≥ 2 sequences.
ALL false ⇒ **F3 REFUTED**: the thesis bracket resolves to exact-equivalence-only; the full table is still reported (the operating-point characterization stands on its own).

## Honesty clauses
- If B* = NONE for both modes even after the extension: reported plainly as "this workload does not achieve 95% compliance within the tested envelope at any tested budget"; the accuracy-vs-budget and energy-vs-budget curves are still the deliverable.
- Accuracy-vs-budget is reported for BOTH modes across the full grid regardless of clause outcomes, including any budget where accuracy degrades non-monotonically.
- Cross-architecture accuracy comparisons to desktop numbers are descriptive only, labeled, never merged into desktop tables.

## Discipline
Standing hard rules unchanged (estimator source read-only; failures are data; per-cell SHA256SUMS; append-only RUN_LOG; DECISIONS before affected runs; counts with denominators). Integrity gate before sweeping: S1 frozen-budget cells byte-identical to the C5 smoke references. Swapoff verified pre-campaign. Board exclusively owned by this session.
