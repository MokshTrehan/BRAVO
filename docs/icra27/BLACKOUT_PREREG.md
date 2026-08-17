# BLACKOUT_PREREG — controlled-outage recovery characterization (BLACKOUT-1)

**Registered:** 2026-08-17, after ABLATE-REC-1, before any blackout run. Frozen once the first run starts.
**Motivating facts:** ABLATE-REC-1 attributed rotation.bag completion to the long-gap recovery mechanism (P1/P2/P3 all TRUE), but the natural evidence is a single data-driven event (one sequence, one 13.4 s outage), and the frozen EuRoC configuration omits `long_gap_recovery_enabled` (effectively recOFF). This campaign converts one natural event into a controlled outage family and measures the mechanism's operating envelope — including its ceiling.

## Method
Camera-frame blackout injected at the REPLAY layer (driver masks all camera frames in [t_b, t_b + k]; IMU stream untouched; no estimator edits). Per-cell manifest records the exact masked frame range and its SHA-256.
- **Injection point rule (deterministic, not hand-picked):** t_b = bag_start + 0.40 × bag_duration, rounded to the nearest camera frame. One injection per run.
- **Durations:** k ∈ {2, 5, 10, 15, 20} s.

## Systems and configurations
- S1-recON and S1-recOFF (N0 omitted: PERTURB-1/ABLATE-REC-1 established byte-level equivalence of the update modes; this is recorded justification, not a silent cut).
- EuRoC cells run a **declared new configuration** `S1-recON-euroc` = frozen EuRoC config + `long_gap_recovery_enabled=true` as a single added parameter (single-delta verified per cell, disclosed in the paper as a study configuration distinct from the frozen headline config).
- U0 context: KAIST cells only, k ∈ {5, 15}, offset 0.

## Matrix
Sequences: circle/circle.bag, infinite/infinite.bag, square/square.bag (KAIST; recovery never fires naturally there), MH_05_difficult, V2_02_medium (EuRoC).
5 sequences × 5 durations × {recON, recOFF} = 50 runs; + U0 context 3 KAIST seqs × 2 durations = 6; + integrity replicas (un-injected recON, offset 0, byte-checked vs PERTURB-1/ABLATE-REC-1) 3. **Total 59.**

## Metrics per cell
Completion; usable completion (0.5 m KAIST threshold with standing disclosure); typed failure class; recovery activations/commits/failures; gap-closure success (does an unsupported gap remain over [t_b, t_b+k]?); time-to-recover after blackout end; ATE/RPE on completions; post-blackout RPE over the 10 s following recovery commit.

## Pre-registered predictions
- **B1:** recON closes injected outages for small k, with success and time-to-recover degrading as k grows; the full curve is reported whatever its shape.
- **B2:** on every (sequence, k) where recON completes and recOFF does not, recOFF's failure is in the tracking-loss / unclosed-gap family — the recON-vs-recOFF separation on the curve is the sufficiency evidence.
- **B3:** all integrity replicas byte-identical to their recorded references.
- **B4 (honesty clause):** any (sequence, k) where recON fails to recover is reported as a measured limitation of the mechanism, in the paper, with counts. Non-monotonic or sequence-dependent ceilings are reported, not smoothed.

## Licensed claims
- If B2 separation exists: "The recovery mechanism closes injected visual outages up to K s on the tested sequences, where the ablated system and stock baseline fail" — with K read off the measured curve per dataset, no extrapolation beyond tested k, EuRoC results labeled with the declared study configuration.
- No claim of general outage robustness beyond the tested durations, injection rule, and sequences.

## Discipline
Hard rules of OVERNIGHT_PROMPT.md apply unchanged (estimator source read-only; failures are data; per-cell SHA256SUMS; append-only RUN_LOG; DECISIONS.md; value-first order: KAIST recON/recOFF pairs at k=10 first, then remaining durations, then EuRoC, then U0 context). Counts with denominators everywhere.

---

## AMENDMENTS — 2026-08-17, registered BEFORE any run (design-derived, not outcome-derived)

**Basis:** frozen-source review of LongGapRelocalizer + VioManager integration. Correspondences form by persistent tracker-ID matching against retained in-state SLAM landmarks; therefore recovery under frame masking succeeds only where the pre-mask → post-mask frame pair is KLT-bridgeable (small effective viewpoint change during the mask). Recovery is a state-stall re-anchor under track survival, not general blackout relocalization.

**A1 — Predictions revised (supersede B1/B2 wording):**
- **B1′ (boundary):** success under masking is governed by KLT bridgeability of the masked interval, not primarily by k. Arm A (mid-motion) cells are predicted to FAIL recovery; arm B (low-motion) cells are predicted recoverable at small k, degrading as k grows.
- **B2′ (separation):** wherever recON completes and recOFF does not, recOFF's failure is in the tracking-loss/unclosed-gap family.
- **B5 (gate integrity — new headline for arm A):** ZERO recovery commits in any unrecoverable situation across the entire matrix; every non-recovery terminates typed (attempt-reason histogram reported per cell: INSUFFICIENT_CORRESPONDENCES, gate rejections, consensus rejects, FAILED) with the live state preserved. A single false commit anywhere is a reportable mechanism defect that outranks all other results.
- B3 (integrity replicas) and B4 (honesty clause) unchanged; B4 now explicitly covers arm B ceilings.

**A2 — Arm B added:** for every sequence, a second deterministic injection point t_b2 = center of the minimum-mean-speed 3 s window computed from ground truth (computed and committed in the Step-0 table before any run; ties broken by earliest). Same durations k ∈ {2, 5, 10, 15, 20} s, systems {recON, recOFF}. Arm A (t_b = 40% rule) unchanged. New totals: arm A 50 + arm B 50 + U0 context 6 + integrity 3 ≈ **109 runs**.

**A3 — Metrics added per cell:** attempt-reason histogram; false-commit counter (B5); post-mask surviving-track and per-attempt correspondence counts (KLT-bridge diagnostics, already in solver logs); for any commit, GT speed at commit time — a commit while |v_GT| > 0.3 m/s is logged as a velocity-assumption exposure event (expected zero given the consensus gate; reported regardless).

**Licensed claims (amended):** arm B curve → "recovery closes low-motion masked outages up to K s on the tested sequences" with K per dataset, no extrapolation. Arm A → "under unrecoverable mid-motion outages, the mechanism refused every commit across N cells with typed reasons and preserved state — zero inconsistent re-anchors." The paper states the track-survival boundary as the mechanism's scope in the same paragraph as any success claim.
