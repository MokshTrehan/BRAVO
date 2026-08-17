You are the implementer for BLACKOUT-1 (amended), the controlled-outage characterization of the long-gap recovery mechanism. Execute docs/icra27/BLACKOUT_PREREG.md exactly, INCLUDING its 2026-08-17 pre-run amendments (arm B, revised predictions B1'/B2'/B5, added metrics). The prereg freezes at the first injected run. Prediction B4 means a mechanism ceiling is a reportable result; prediction B5 means a single false commit outranks every other result tonight.

AUTHORIZATION AND CONTEXT
As in prior campaigns: owner authorizes full-machine read; writes confined to the repo, a new dated artifacts root under /home/moksh/schurvio-icra27-artifacts/blackout-campaign/, and temp dirs. Repo: /home/moksh/schurvio-lite-rotation-robustness — branch blackout/preg-20260817 from the current campaign HEAD. Reference artifacts: PERTURB-1 and ABLATE-REC-1 roots; verify frozen binary sha256s against recorded values before any run.

HARD RULES (unchanged; checked at session end)
Estimator source read-only (empty path-scoped git diff over ov_msckf, ov_core, ov_init, ov_eval, config at session end). Masking lives ONLY in the replay/driver layer. No tuning; no prereg edits after the first injected run; failures are data; per-cell SHA256SUMS; append-only RUN_LOG.md; DECISIONS.md before affected runs; scripts with teed logs.

STEP 0 — BEFORE ANY RUN
1. Verify the amended prereg is committed (git log shows the amendment). Absent/uncommitted -> STOP_REPORT.md.
2. Compute and COMMIT the injection-point table for both arms, per sequence: arm A t_b = bag_start + 0.40 x duration (nearest camera frame); arm B t_b2 = center of the minimum-mean-GT-speed 3 s window (earliest on ties). Table includes the GT speed at each point. No hand adjustment ever.
3. Implement replay-layer masking (drop all camera messages in [t_b, t_b+k]; IMU untouched); per-cell manifest of masked frame range + sha256.
4. EuRoC study config S1-recON-euroc = frozen EuRoC config + exactly long_gap_recovery_enabled=true (single-delta diff per EuRoC recON cell). KAIST uses frozen configs; recOFF = the ABLATE-REC-1 single-line disable.
5. Masking-inertness check: one k=0 run THROUGH the masking path, byte-identical to its recorded reference. Mismatch -> STOP everything.

STEP 1 — INTEGRITY (B3)
Un-injected recON replicas at offset 0: circle.bag (KAIST), MH_05 (frozen config, no study key), plus one ABLATE-REC-1 replica. Byte-compare to recorded references. Mismatch -> STOP.

STEP 2 — EXECUTION (value-first, serialized)
1. Arm B, k in {2, 5}, recON, all five sequences (the positive-envelope crux — 10 cells)
2. Arm A, k=10, recON/recOFF pairs, KAIST sequences (the gate-integrity crux — 6 cells)
3. Remaining arm B durations and recOFF counterparts
4. Remaining arm A cells; then EuRoC; then U0 context (KAIST, arm A, k in {5,15})
Per cell: masked-range manifest, resolved recovery parameter, full recovery event log (trigger/attempt/reason/consensus/commit lines), attempt-reason histogram, post-mask surviving-track and correspondence counts, GT speed at any commit, command line, config/launch/binary sha256s.

STEP 3 — AGGREGATION
Per-cell completion, usable completion (0.5 m KAIST threshold + standing disclosure), typed failure class, recovery activations/commits/failures, gap-closure success, time-to-recover, ATE/RPE on completions, post-recovery 10 s RPE. Build BOTH arms' central tables: per sequence, success and usable-completion vs k for recON/recOFF, with attempt-reason histograms alongside. Evaluate B1', B2', B3, B4, B5 mechanically against the amended wording. FALSE-COMMIT COUNT is computed globally and must appear in the first table. Counts with denominators; no significance language.

STEP 4 — MORNING_SUMMARY.md, in order
(1) masking-inertness + integrity results; (2) the committed injection-point table with GT speeds; (3) run accounting (expected ~109 vs recorded vs NOT_RUNNABLE with reasons — e.g., k extending past bag end, or a min-speed window overlapping the 40% point); (4) arm A results with the global false-commit count and reason histograms; (5) arm B success-vs-k tables and the measured K per dataset; (6) B-verdicts including every recON ceiling under B4 stated plainly; (7) the licensed claim sentences constructed from the amended Licensed-claims section — nothing stronger, EuRoC labeled as study configuration, the track-survival boundary stated alongside any success; (8) empty estimator-diff output + binary re-hashes; (9) artifact paths.

Roughly 109 runs plus masking work; comfortably inside a day. If arm B closes nothing and arm A refuses everything, that is a coherent, publishable characterization of exactly what this mechanism is — write it down without apology and without spin.
