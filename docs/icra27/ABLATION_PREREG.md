# ABLATION_PREREG — recovery-mechanism attribution (ABLATE-REC-1)

**Registered:** 2026-08-16, after PERTURB-1, before any ablation run. Frozen once the first run starts.
**Motivating fact:** PERTURB-1 showed S1 ≡ N0 to ≤4.1e-7 m on all matched pairs, including rotation.bag completion with identical C2 recovery outcomes. The CDSC-1R4 whole-stack differential vs stock U0 is therefore NOT attributable to the Schur update. The leading candidate mechanism is the C2 recovery subsystem (present in S1 and N0, absent in stock OpenVINS). This campaign tests that attribution by single-delta ablation.

## Step 0 — before predictions lock (facts only, no new runs)
1. Extract per-cell recovery-event counts from PERTURB-1 aggregate (already recorded per cell as descriptive summaries). Commit the table `recovery_event_distribution.md` alongside this file.
2. If recovery events fired on sequences beyond the rotation family, EXTEND the sequence set below to include every firing sequence, and record the extension in the Step-0 table. Sequence-set extension driven by Step-0 facts is the only permitted edit; nothing may change after ablation runs start.
3. Locate the existing runtime switch that disables the recovery subsystem (D7 references "same recovery setting", implying one exists). If no runtime switch exists: **STOP — do not edit estimator source.** Report and end.

## Systems (single-delta, machine-verified per cell)
- S1-recOFF := frozen S1 launch with ONLY the recovery switch disabled
- N0-recOFF := frozen N0 launch with ONLY the recovery switch disabled
- Context rows (6 cells, required): U0 stock on the rotation family (rotation, rotation_fast) at offsets {0, +5, +10} — establishes perturbation stability of the stock-side failure, whose prior evidence is n=1 at offset 0. [Amended 2026-08-17 before any ablation run; permitted because the campaign has not started.]

## Matrix
Sequences: rotation, rotation_fast, square_fast, circle_fast, square_head, MH_05_difficult, V2_03_difficult (+ Step-0 additions). Offsets {0, +5, +10}. 2 systems × 7 sequences × 3 offsets = 42 runs (+ context cells). Deterministic; integrity replicas: recON offset-0 cells must reproduce PERTURB-1 byte-identically before ablated runs execute.

## Pre-registered predictions
- **P1:** S1-recOFF and N0-recOFF exhibit non-completion or ≥ usability-threshold error (0.5 m) on rotation.bag, in the same failure class family as U0 in CDSC-1R4 (tracking loss / unclosed state gap).
- **P2:** On every cell whose PERTURB-1 recON counterpart recorded zero recovery events, recOFF is byte-identical to recON (the switch is inert when the mechanism never fires).
- **P3:** All recON integrity replicas byte-identical to PERTURB-1.

## Interpretation (committed now)
| Outcome | Meaning | Permitted claim |
|---|---|---|
| P1 true, P2 true, P3 true | Recovery subsystem is the attributed completion mechanism for rotation-induced tracking loss | "Disabling the recovery subsystem alone reproduces the stock-baseline failure mode on rotation sequences; the recovery mechanism is necessary for completion in this stack." |
| P1 false (recOFF completes) | Recovery is NOT the mechanism; residual repo-vs-upstream delta responsible; bisection required | No mechanism claim. Whole-stack claims only. |
| P2 false | The switch has side effects beyond the recovery path; single-delta assumption broken | STOP; find the true single-delta switch before claiming anything. |
| P3 false | Environment drift | Campaign void. |

## Discipline
Hard rules of OVERNIGHT_PROMPT.md apply unchanged (estimator source read-only; no tuning; failures are data; SHA256SUMS per cell; append-only RUN_LOG; DECISIONS.md for conservative interpretations). Report counts with denominators. The paper may state the attributed-mechanism claim only under the first row of the table, quoted at or below the strength written there.
