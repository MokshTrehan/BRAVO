You are the overnight implementer for ABLATE-REC-1, the recovery-mechanism attribution campaign. Execute docs/icra27/ABLATION_PREREG.md exactly. You are an instrument, not a researcher: the prereg is frozen the moment the first ablated run starts, and the only claims tomorrow morning are the ones its interpretation table licenses.

AUTHORIZATION
Identical to PERTURB-1: the machine owner authorizes reading anywhere on this machine; writes are confined to the project repo, a new dated artifacts directory under /home/moksh/schurvio-icra27-artifacts/, and temporary working directories.

CONTEXT YOU INHERIT (verify each, do not assume)
- Project repo: /home/moksh/schurvio-lite-rotation-robustness. Create branch ablate/rec-20260817 from the HEAD of perturb/preg-20260816.
- PERTURB-1 artifacts: /home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z/ (aggregate/final, per-cell recovery-event summaries, recorded SHA256SUMS).
- CDSC-1R4 artifacts (U0 reference behavior and checksums): /home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z/.
- Frozen binaries: S1/N0 = the repo cp0-ws ros1_serial_msckf; U0 = the schurvio-baseline-triad build. Verify both sha256 values against the PERTURB-1 RUN_LOG records before any run.

HARD RULES (unchanged from PERTURB-1; checked at session end)
Estimator source is read-only — the end-of-session path-scoped git diff over ov_msckf, ov_core, ov_init, ov_eval, and config must be empty, and any fix that seems to require estimator edits means STOP that lane and log it. No tuning, no per-sequence adjustments, no prereg or threshold edits after the first ablated run. Failures are data: never rerun-to-green. Per-cell SHA256SUMS in the established style. Append-only RUN_LOG.md; conservative interpretations recorded in DECISIONS.md before the affected runs. Work through scripts with teed logs; do not stream raw run output through your own context.

STEP 0 — BEFORE ANY RUN
1. Verify docs/icra27/ABLATION_PREREG.md is committed (git log). Absent or uncommitted → write STOP_REPORT.md and end.
2. Extract per-cell recovery-event counts from the PERTURB-1 aggregate. Write and commit recovery_event_distribution.md: which sequences fired recovery events, per system, with counts and timestamps.
3. If recovery fired on sequences beyond {rotation, rotation_fast}: extend the recOFF sequence set to every firing sequence, and record the extension in the prereg's Step-0 table. This is the ONLY permitted prereg edit, and only before runs.
4. Locate the recovery subsystem's runtime disable switch (the "recovery setting" referenced in PERTURB-1 D7). If no such runtime switch exists without touching estimator source → STOP_REPORT.md and end; there is no campaign tonight and that is the correct outcome. Otherwise emit the single-delta resolved-parameter diff template that every recOFF cell must satisfy against its recON counterpart.

STEP 1 — INTEGRITY (prediction P3)
Run recON offset-0 replicas: S1 and N0 on rotation.bag, plus S1 on one EuRoC sequence. Byte-compare state_estimate.txt, state_deviation.txt, estimate_raw.tum against the PERTURB-1 recorded SHA256s. Any mismatch → STOP everything; environment drift voids the night.

STEP 2 — EXECUTION (value-first, serialized)
1. The P1 crux first: S1-recOFF and N0-recOFF on {rotation, rotation_fast} × offsets {0, +5, +10} — 12 cells.
2. U0 context rows: {rotation, rotation_fast} × {0, +5, +10} — 6 cells.
3. Remaining recOFF: {square_fast, circle_fast, square_head, MH_05_difficult, V2_03_difficult} × {0, +5, +10} for both systems — 30 cells, plus any Step-0 extensions.
Every recOFF cell records the machine-verified single-delta diff versus its recON counterpart, full command line, config hash, offset, and resolved recovery parameter value.

STEP 3 — AGGREGATION AND VERDICTS
Per-cell completion, typed failure class, and ATE/RPE via the frozen D8 math core. Evaluate P1, P2, P3 mechanically against the prereg wording: P2 requires byte-comparing recOFF vs recON on exactly the cells whose PERTURB-1 recON counterpart recorded zero recovery events (list them from Step 0). Apply the interpretation table; apply the usability threshold with its disclosure line; report every count with its denominator.

STEP 4 — MORNING_SUMMARY.md, in this order
(1) Step-0 facts: event distribution, the identified switch, any sequence-set extension. (2) Integrity/P3 result. (3) Run accounting: expected vs executed vs NOT_RUNNABLE with reasons. (4) P1 and P2 verdicts with counts, and an explicit failure-class comparison between recOFF outcomes and the CDSC-1R4 U0 failure family. (5) Which interpretation-table row applies, and the licensed claim sentence quoted verbatim from that row — nothing stronger, no editorializing. (6) The empty estimator-diff output. (7) Artifact paths.

Roughly 50 runs, about two hours of compute; you have all night. Slow and checksummed beats fast and plausible, and a truthful STOP is a successful outcome.
