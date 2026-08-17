# DESKTOP_EVIDENCE_SESSION v2 — C8: fault injection + stall anatomy + unified paper numbers
(supersedes v1, 2026-08-17; updated after BLACKOUT-1 and for co-residence with EXT-VF-1)

**Branch:** `evidence/c8-20260817` from the latest campaign HEAD (record it). Hard rules unchanged: estimator source read-only with the end-of-session path-scoped diff; injectors live ONLY at the envelope-replay boundary; failures are data; per-cell SHA256SUMS; append-only RUN_LOG; DECISIONS.md before affected work.

**YIELD RULE (one-way, this session only):** EXT-VF-1 runs live nominal-rate playback tonight and is contention-fragile; this session's replay is deterministic and contention-immune. Before starting any compute batch, check for active VINS-Fusion estimator/container processes; if present, wait and poll every 5 minutes. Never make VF wait for you.

## Item 1 — Fault-injection matrix on the frozen branch (H4 → ledger F4; run FIRST, it's tonight's heavy compute)
Deterministic injectors at the envelope-replay boundary for the eight declared fault classes: invalid factor dimensions; nonfinite factor values; rank failure; innovation-factorization failure; negative posterior diagonal; prior changed between preview and commit; logging/diagnostics failure; feature/factor failure after proposal construction. Corpora: EuRoC + KAIST captured envelopes from the frozen campaigns; ≥100 injections/class or exhaustive.
Per injection, verify mechanically: typed failure emitted; pre/post state+covariance hash equality on every failed transaction (zero partial mutation); zero hidden repairs; ≤1 commit per callback; no duplicate finalization; logging-failure class leaves the update outcome unaffected. **Natural-incident reconciliation:** scrape all campaign roots for naturally occurring typed failures; emit the ledger table with denominators (expected near-zero).
Acceptance: H4 matrix with all-zero violation columns — any violation STOPs the session and outranks everything else tonight.

## Item 2 — Stall anatomy: why frame-present processing stalls where frame-absent bridging does not (ledger F14)
The keystone question, now with a control condition. BLACKOUT-1 proved the same binaries bridge 10–20 s frame-ABSENT gaps on IMU in 4–50 ms (recOFF and U0 alike), yet the natural rotation passage with frames PRESENT stalls the state 13.442 s in recOFF and U0. Item 2 must explain that difference with log-level evidence.
Extract per-frame timelines from: CDSC-1R4 U0 rotation.bag; ABLATE-REC-1 recOFF rotation.bag (all offsets); BLACKOUT-1 recOFF/U0 masked cells (the control). Signals: tracked-feature counts, update attempts and typed outcomes, state-timestamp advance per frame, ZUPT decisions, propagation/clone calls, transaction previews/commits/rollbacks.
Deliverables: `stall_anatomy.md` — the causal chain from yaw onset → track collapse → first stalled frame → gap → (U0: terminal output stop | recOFF: resume at gap end +0.030 s | recON: trigger→consensus→commit), with timestamps and log-line citations; an explicit evidence-based adjudication among candidate mechanisms (per-frame update rejection preventing state advance; ZUPT engagement; transaction semantics coupling propagation to update outcome; an upstream stock behavior shared by both builds — U0 stalls too, so the shared cause must exist upstream or in the shared frame-processing path); and the three-regime contrast table (natural-stall vs masked-gap vs recovered). Report what the logs support; do not conclude past them. CSV for the anatomy figure.

## Item 3 — Unified `make paper-numbers` (ledger regeneration; run after or between yields)
One command regenerating every table/figure-datum from raw logs across all campaign roots present at execution time: CDSC-1R4, PERTURB-1 (abandoned root `perturb1-...204025Z` ingested as RETAINED_EXCLUDED, outside all denominators), ABLATE-REC-1, BLACKOUT-1, and EXT-VF-1 once its final aggregate exists; schema-ready for the Orin root (Wednesday).
New universal conventions, applied everywhere:
- **Dual completion readings:** the frozen passage rule stays primary; the D13 quantization-aware reading (2e-5 s tolerance) is computed side-by-side for every gap-bearing cell in every campaign, with `rounding_only` flags. Report explicitly whether any pre-BLACKOUT verdict flips (expected: none — the ablation gap exceeds tolerance by six orders of magnitude — but compute it, don't assume it).
- **Coverage-welded accuracy:** every ATE for a cell with any state gap is emitted as (ATE, coverage %) and rendered together; ingest the ABLATE-REC-1 rot-n addendum (0.0878 m) as a first-class cell metric with its coverage.
- Severity axis from GT |ω| statistics; gate verdicts (CR/P/B/H/CG) recomputed mechanically; golden-sample regression test; manifest mapping every paper table/figure ID → script → source artifacts → SHA256.
Regenerate `claims/ledger.yaml` to the current findings set:
```
F1  S1 completes all primary desktop runs            SUPPORTED (98/98 PERTURB-1; 25/25 CDSC-1R4)
F2  S1 ≡ N0 exact equivalence                        SUPPORTED (≤4.1e-7 m, 49 matched pairs)
F3  Embedded advantage at fixed Orin budget          PENDING (Wed sweep)
F4  Fault atomicity on the frozen branch (H4)        PENDING (Item 1 tonight)
F5  External comparison/context (VF + pinned ORB3/SchurVINS evidence)  PENDING (EXT-VF-1 tonight)
F6  Recovery necessity for the natural rotation stall SUPPORTED (ablation P1; perturbation-stable)
F7  One-pass vs two-pass on the frozen protocol      PENDING (Tue-night S2 rows)
F8  Severity/margin analysis (GT |ω| axis)           PENDING (emitted by Item 3)
F9  Cross-campaign byte-determinism chain            SUPPORTED (4 campaigns)
F10 Stock-context rows incl. perturbed rotation      SUPPORTED
F11 Blackout envelope: K=2 s + non-monotone bridgeability  SUPPORTED
F12 Gate integrity: 0/11 false commits; 3 exposures bounded ≤0.16 m  SUPPORTED
F13 Integrity/availability trade quantified (dual readings)          SUPPORTED
F14 Stall anatomy (frame-present vs frame-absent)    PENDING (Item 2)
F15 Recovery contract fixed-calibration scope        SUPPORTED (D12, source-cited)
```
Acceptance: clean-clone run regenerates everything; ledger statuses match evidence pointers; this is the command the skeleton cites per figure.

## Non-goals
Orin work; VINS-Fusion (its own session, running tonight — yield to it); blackout/ablation prereg edits; estimator changes; new experiments beyond Item 1.
