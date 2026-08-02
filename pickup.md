# SchurVIO-Lite pickup and next-step plan

Last updated: 2026-08-02 (America/Toronto)

## Resume point

- Repository: `/home/moksh/newSlam variant`
- Branch: `schurvio-lite/cp2-one-pass`
- Current HEAD: `0ca7cb6a760f2a72b5d334587681dfc760aa1a37`
- HEAD subject: `cp2: record composite clarification approval`
- Known post-HEAD planning change: this `pickup.md` file. It is not an implementation checkpoint until reviewed and committed.
- Approved CP2-C clarification: `b37eff6e5baa035175e1dde3cae52ee496ca9e2d`
- Approved CP1 mathematical contract: `952771e955fe3459f2fd43122a9c6f8ce57d1799`
- Pinned OpenVINS upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
- Target architecture: NVIDIA Jetson Nano family, CPU-only for the claimed path.
- Current gate state: CP2-A and CP2-B passed; CP2-C, CP2-D, and CP2-E remain pending.
- The CP2 deadline has been missed without a waiver. Requirements remain unchanged and the CP3 schedule is compressed.

Before resuming, require no unexplained worktree changes and verify the exact
implementation baseline HEAD above. Read, in order:

1. `docs/conventions.md`
2. `docs/cp2_one_pass_contract.md`
3. `docs/cp2_recorded_evidence_contract.md`
4. `docs/cp2_artifact_schema.md`
5. `docs/cp2_c_composite_and_readiness_clarification.md`
6. `project/cp2_gate.yaml`

Do not inspect dataset registry contents, bags, ground truth, or payload data until the frozen CP2 readiness barrier permits it. Mathematical correctness remains the first priority; a build or run is not evidence of correctness by itself.

## What exists now

The branch already contains a real estimator modification and an unusually strict verification layer:

- A selectable `nullspace` or `schur` transient-landmark elimination path.
- A production square-root Schur-equivalent reducer that whitens explicitly, rejects rank-deficient or ill-conditioned landmarks, uses Householder QR, and never applies jitter, clamping, regularization, or silent fallback.
- A shared feature-gate implementation driven from one immutable prior.
- A read-only full-update preflight that computes the proposed state increment and posterior covariance before any live EKF write and suppresses invalid commits.
- A shadow path that runs the baseline and candidate from the same raw feature systems without candidate writes.
- Canonical binary64/SHA-256 encoding for raw systems and proposals, plus offline replay.
- Seventy-seven CP1/CP2 unit tests at the last recorded unit checkpoint, including Schur equivalence, projection Jacobians, FEJ behavior, clone semantics, rank boundaries, end-to-end updater behavior, trace corruption, and replay.

The default live mode remains `nullspace`. The current `schur` mode is intentionally required to reproduce the same one-pass EKF information as the OpenVINS nullspace update. CP2 is therefore a correctness and parity foundation, not by itself the final research contribution.

## How this differs from just running OpenVINS

| Area | Pinned OpenVINS | SchurVIO-Lite now | Intended research system |
|---|---|---|---|
| Front end and state | FAST/KLT-style OpenVINS tracking, IMU propagation, clone window, MSCKF state | Retained deliberately | Retained deliberately |
| Landmark elimination | Per-feature nullspace projection | Selectable baseline nullspace or guarded square-root Schur-equivalent QR path | Same mathematically verified reduction used as the basis for bounded relinearization |
| Numerical policy | Normal production assertions/early returns | Explicit finite, shape, rank, conditioning, factorization, and posterior checks; no hidden repair/fallback | Same fail-closed policy |
| Update transaction | Direct `StateHelper::EKFUpdate` after compression | Read-only proposal preflight before the sole live commit | One final commit after the selected one- or two-pass computation |
| Baseline/candidate comparison | None | Same raw systems and immutable prior can drive both paths in shadow mode with zero candidate writes | Full experiment and ablation matrix |
| Exact evidence | Normal logs and trajectory output | Canonical raw/proposal bytes, exact-bit identities, replay, terminal taxonomy, and strict tests | Composite state phases, commit oracle, recorded campaigns, timing/power evidence |
| Iteration | One linearization/update | Still one pass at CP2 | At most two frozen-prior passes, if CP3 proves the FEJ and chart semantics |
| Resource policy | No project-specific pass-admission deadline manager | Not implemented yet | Bounded manager chooses one or two passes from a declared budget and always caps work at two passes |
| Embedded claim | OpenVINS can be deployed, but this project has no pinned comparative claim from that fact alone | Jetson profile selected but target details/evidence incomplete | CPU-only Jetson accuracy/latency/energy/deadline Pareto evidence, if CP6 passes |

The material distinction has three layers:

1. **Already material as engineering/correctness:** the candidate reduction, transactional preflight, zero-write shadow comparison, exact trace/replay boundary, and failure-atomic evidence are absent from stock OpenVINS.
2. **Not yet material as a strong paper claim:** one-pass Schur/nullspace parity is supposed to produce almost the same information. Demonstrating that equivalence is necessary, but “we rewrote nullspace elimination as Schur-equivalent QR” is not enough novelty on its own.
3. **Potentially material as research:** a mathematically valid second relinearization and a deadline-aware controller that spends that second pass only when budget permits, followed by Jetson accuracy/latency/energy evidence. This is the proposed paper-level contribution, and it is still unproven.

If the goal were only to obtain working VIO today, running pinned OpenVINS would be simpler. OpenVINS is the control and substrate here. SchurVIO-Lite is justified only if the bounded one/two-pass system produces a predeclared, repeatable accuracy-resource advantage without weakening estimator correctness.

## Next hard checkpoints

### CP2-C1 — composite snapshot and detached oracle

Implement an isolated, owning composite-state layer before changing the updater commit path:

- Capture state timestamp and full covariance.
- Capture the exact ordered semantic error-state partition.
- Capture every active top-level nominal and FEJ value.
- Capture fixed IMU and camera calibration values and camera-model caches.
- Validate active/inactive type membership, IDs, sizes, map identities, clone timestamps, landmark anchors, finiteness, and JPL unit quaternions.
- Retain a nonserialized owning pointer-graph token to detect object replacement.
- Encode/decode phases 0/1 with the frozen prior domain and phases 2/3 with the frozen postcommit domain.
- Implement the detached commit oracle by constructing one production-equivalent object per active top-level type and applying exactly one complete `dx` segment.
- Preallocate the live phase-3 capture so the postcommit fill/handoff is nonthrowing and allocation-free.

Hard gate: strict-FP known-answer, round-trip, corruption, one-field mutation, partition, identity, anchor, quaternion-boundary, detached-update, and allocation-failure tests all pass. Then run the complete CP1/CP2 unit suite and commit this checkpoint separately. No recorded bags are opened here.

### CP2-C2 — updater phase ordering and failure atomicity

Integrate the composite layer into `UpdaterMSCKF` in the approved order:

1. Take a tentative phase-0 capture after cleaning/triangulation and before raw assembly.
2. When the first raw system becomes possible, fully validate and encode phase 0 before assembling or incrementing the raw counter.
3. Project every gate and preview prior from phase 0; never recapture the live prior.
4. Finish traversal and both proposals.
5. Build and validate detached expected phase 2 in unpublished storage.
6. Capture phase 1 and require exact values plus the same pointer graph.
7. With nothing intervening, enter the sole baseline `StateHelper::EKFUpdate` commit.
8. Sample the steady-clock endpoint as the first postcommit operation.
9. Fill phase 3 allocation-free as the next operation and compare it exactly with phase 2.
10. Publish through a status-returning authoritative sink. Keep the existing diagnostic callback separate and nonauthoritative.

Hard gate: protecting tests prove exact operation order, zero raw count on failed phase-0 promotion, no precommit writes on ordinary failure, no candidate writes, no phase 2 for noncommitting records, exact counter units, correct committed-mismatch classification, and run-fatal behavior for incomplete traces or pointer-graph replacement.

### CP2-C3 — recorded-evidence runner readiness

- Complete the state-snapshot binary file and artifact report plumbing.
- Implement the approved source-provenance/readiness barrier and its artifact-free self-tests.
- Bind raw, proposal, state, feature, state-block, covariance-block, update, configuration, source, and command records one-to-one.
- Run a fresh full unit gate at the exact clean runtime commit.
- Independently verify the artifact schema and all corruption cases before any dataset access.

Hard gate: readiness passes at one clean commit and produces no final CP2-C claim or dataset-derived artifact during self-test.

### CP2-C — recorded updater parity

Run the frozen serial campaign on `MH_01_easy`, `MH_03_medium`, and `V1_01_easy` to completion. Require at least 1,000 counted visual updates, at least 99.9% unweighted and row-weighted gate agreement, complete proposal/state evidence, zero candidate writes, exact baseline commit-oracle agreement, and every predeclared tolerance/counter invariant.

Any unexplained mismatch, nonfinite value, incomplete trace, silent repair, or evidence disconnect fails CP2-C. Do not tune the baseline after seeing candidate results.

### CP2-D and CP2-E — trajectory parity and desktop timing

- CP2-D: independent nullspace and Schur sequence runs, common population/alignment checks, trajectory p95 bounds, and ATE-difference bound.
- CP2-E: paired desktop timing under the frozen affinity/clock profile; median overhead at most 10% and p95 overhead at most 15%.

Only after CP2-C/D/E pass is one-pass parity established.

### CP3 onward — where the paper contribution must emerge

- CP3: implement and prove fixed two-pass frozen-prior relinearization, including the separately blocked mixed-FEJ and chart-consistent covariance/reset semantics.
- CP4: complete EuRoC stability matrix.
- CP5: implement and test the bounded pass-admission/deadline manager.
- CP6: execute the CPU-only Jetson campaign with fixed cores/clocks and power/thermal evidence; make the research go/no-go decision.

Do not describe the system as deadline-guaranteed, chart-consistent, more accurate, faster, or more energy-efficient until its corresponding gate passes.

## Publication decision boundary

The publishable thesis is not merely “OpenVINS plus Schur.” The strongest defensible thesis is:

> A mathematically controlled, at-most-two-pass visual-inertial update that uses a deadline-aware admission policy to trade additional relinearization for bounded CPU work, with exact parity/correctness evidence and an embedded accuracy-latency-energy evaluation.

That thesis survives only if the experiments show one of the preregistered CP6 Pareto routes. If two-pass gives no reliable accuracy gain, or the bounded policy gives no resource advantage, retain the work as a strong reproducibility/engineering artifact or redirect the paper rather than inflating the claim.

## Immediate pickup command sequence

Use read-only checks first:

```bash
git status --short --branch
git rev-parse HEAD
git log -5 --oneline --decorate
```

Expected implementation baseline HEAD is `0ca7cb6a760f2a72b5d334587681dfc760aa1a37`.
The only expected post-HEAD change at this handoff is `pickup.md`. If any other
path is changed, or HEAD differs, inspect and reconcile it before starting
CP2-C1. Do not reset or overwrite unexplained work.
