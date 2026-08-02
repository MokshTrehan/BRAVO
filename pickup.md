# SchurVIO-Lite pickup and next-step plan

Last updated: 2026-08-02 (America/Toronto)

## Resume point

- Repository: `/home/moksh/newSlam variant`
- Branch: `schurvio-lite/cp2-one-pass`
- Last completed runtime implementation checkpoint: `162ed140cb3fee301cf3f2e212c9afa28829798a` (`fix: bind CP2 commit boundary to production library`).
- Tested source tree: `46935c58e7a7184f659a6f67fcd33896ebe02d46`.
- Fresh unit artifact: `results/staging/cp2/unit/cp2_unit_20260802T185623152114466Z-g162ed140cb3f-WtrFa1xv`.
- Artifact `SHA256SUMS` external anchor: `58bc351d513bb093a6e355a808fcf62a7d952e01dee5902594ed74946d76f579`.
- The checkpoint-documentation commit containing this file is necessarily post-run metadata and was not runtime-tested by that artifact.
- Approved CP2-C clarification: `b37eff6e5baa035175e1dde3cae52ee496ca9e2d`
- Approved CP1 mathematical contract: `952771e955fe3459f2fd43122a9c6f8ce57d1799`
- Pinned OpenVINS upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
- Target architecture: NVIDIA Jetson Nano family, CPU-only for the claimed path.
- Current gate state: CP2-A and CP2-B passed; CP2-C1 and CP2-C2 passed unit-only; CP2-C3, CP2-C, and CP2-D remain pending/unexecuted; CP2-E is unexecuted and blocked pending a fixed-clock profile.
- The CP2 deadline has been missed without a waiver. Requirements remain unchanged and the CP3 schedule is compressed.

Before resuming, require no unexplained worktree changes and verify ancestry
from the tested implementation checkpoint above. Read, in order:

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
- Canonical binary64/SHA-256 encoding for raw systems and proposals, plus a value-only trace codec and replay kernel; artifact-bound offline replay plumbing remains CP2-C3 work.
- An owning four-phase composite-state snapshot, exact pointer-graph token, canonical state-file codec, detached production-type commit oracle, and prepared nonthrowing/allocation-free phase-3 fill/handoff.
- A production updater transaction that promotes one phase-0 prior before raw assembly, derives all gates and proposals from that prior, prepares detached phase 2 before the final phase-1 proof, performs exactly one baseline EKF commit, then captures the clock endpoint and allocation-free phase 3 in the approved order.
- An authoritative status-returning evidence sink with a sticky fatal latch, exact checked counters, retained gamma provenance, postcommit oracle comparison, and a separately linked full fault-injection runtime.
- One hundred thirty-nine CP1/CP2 unit executions across 18 binaries at the latest recorded checkpoint (125 unique test names; 14 updater cases intentionally run against both production and fault runtimes), including Schur equivalence, projection Jacobians, FEJ behavior, clone semantics, rank boundaries, transaction ordering, failure atomicity, production-library/source-inventory provenance, exact counters, trace corruption/replay, exhaustive composite mutation, detached update, and allocation-failure coverage.

The default live mode remains `nullspace`. The current `schur` mode is intentionally required to reproduce the same one-pass EKF information as the OpenVINS nullspace update. CP2 is therefore a correctness and parity foundation, not by itself the final research contribution.

## How this differs from just running OpenVINS

| Area | Pinned OpenVINS | SchurVIO-Lite now | Intended research system |
|---|---|---|---|
| Front end and state | FAST/KLT-style OpenVINS tracking, IMU propagation, clone window, MSCKF state | Retained deliberately | Retained deliberately |
| Landmark elimination | Per-feature nullspace projection | Selectable baseline nullspace or guarded square-root Schur-equivalent QR path | Same mathematically verified reduction used as the basis for bounded relinearization |
| Numerical policy | Normal production assertions/early returns | Explicit finite, shape, rank, conditioning, factorization, and posterior checks; no hidden repair/fallback | Same fail-closed policy |
| Update transaction | Direct `StateHelper::EKFUpdate` after compression | Read-only proposal preflight before the sole live commit | One final commit after the selected one- or two-pass computation |
| Baseline/candidate comparison | None | Same raw systems and immutable prior can drive both paths in shadow mode with zero candidate writes | Full experiment and ablation matrix |
| Exact evidence | Normal logs and trajectory output | Canonical raw/proposal/state bytes, composite phases, commit oracle, value-only replay kernel, terminal taxonomy, and strict tests | Artifact-bound replay, recorded campaigns, timing/power evidence |
| Iteration | One linearization/update | Still one pass at CP2 | At most two frozen-prior passes, if CP3 proves the FEJ and chart semantics |
| Resource policy | No project-specific pass-admission deadline manager | Not implemented yet | Bounded manager chooses one or two passes from a declared budget and always caps work at two passes |
| Embedded claim | OpenVINS can be deployed, but this project has no pinned comparative claim from that fact alone | Jetson profile selected but target details/evidence incomplete | CPU-only Jetson accuracy/latency/energy/deadline Pareto evidence, if CP6 passes |

The material distinction has three layers:

1. **Already material as engineering/correctness:** the candidate reduction, transactional preflight, zero-write shadow comparison, canonical trace boundary and value-only replay kernel, and failure-atomic evidence are absent from stock OpenVINS.
2. **Not yet material as a strong paper claim:** one-pass Schur/nullspace parity is supposed to produce almost the same information. Demonstrating that equivalence is necessary, but “we rewrote nullspace elimination as Schur-equivalent QR” is not enough novelty on its own.
3. **Potentially material as research:** a mathematically valid second relinearization and a deadline-aware controller that spends that second pass only when budget permits, followed by Jetson accuracy/latency/energy evidence. This is the proposed paper-level contribution, and it is still unproven.

If the goal were only to obtain working VIO today, running pinned OpenVINS would be simpler. OpenVINS is the control and substrate here. SchurVIO-Lite is justified only if the bounded one/two-pass system produces a predeclared, repeatable accuracy-resource advantage without weakening estimator correctness.

## Next hard checkpoints

### CP2-C1 — composite snapshot and detached oracle — completed unit-only

Completed at source commit `fe00fc8a979bec8676d33961ef868ab9e64803e0`.
The fresh serialized unit gate passed 91/91 cases across 15/15 executables,
including the focused 14/14 composite tests; the verifier independently passed
against the retained external manifest anchor and rejected 49 synthetic
corruptions. This closes only CP2-C1. That CP2-C1 artifact did not integrate
the updater, authorize recorded-data access, pass CP2-C, or establish Jetson
behavior.

The completed layer:

- Capture state timestamp and full covariance.
- Capture the exact ordered semantic error-state partition.
- Capture every active top-level nominal and FEJ value.
- Capture fixed IMU and camera calibration values and camera-model caches.
- Validate active/inactive type membership, IDs, sizes, map identities, clone timestamps, landmark anchors, finiteness, and JPL unit quaternions.
- Retain a nonserialized owning pointer-graph token to detect object replacement.
- Encode/decode phases 0/1 with the frozen prior domain and phases 2/3 with the frozen postcommit domain.
- Implement the detached commit oracle by constructing one production-equivalent object per active top-level type and applying exactly one complete `dx` segment.
- Preallocate the live phase-3 capture so the postcommit fill/handoff is nonthrowing and allocation-free.

Hard gate result: strict-FP known-answer, round-trip, corruption, one-field
mutation, partition, identity, anchor, quaternion-boundary, detached-update,
and allocation-failure tests all passed. No recorded bags were opened.

### CP2-C2 — updater phase ordering and failure atomicity — completed unit-only

Completed at source commit `162ed140cb3fee301cf3f2e212c9afa28829798a`
(tree `46935c58e7a7184f659a6f67fcd33896ebe02d46`). The fresh serialized gate
passed all 139 executions across 18/18 executables (132 CP2 and 7 CP1),
independently re-executed all 139, verified the exact 23-source production and
23-source fault runtime inventories, and rejected all 85 synthetic verifier
corruptions. The external manifest anchor is the one recorded above.

The completed layer and protecting matrix cover tentative phase-0 promotion
before raw counting, sole-prior projection, complete dual-path traversal,
unpublished detached phase 2, value-before-validity phase-1 comparison,
pointer-graph identity, the literal proof-to-commit-to-clock-to-fill boundary,
exact `[0,1]` and `[0,1,2,3]` phase populations, allocation-free postcommit
capture, exact counters, zero candidate writes, and sticky run-fatal sink
behavior. The first fresh run at
`fa350bf5caccc9789c70d0f69660881608963c0c` failed closed even though all 139
executions passed because the commit-boundary implementation was header-only,
so the boundary test did not load the production DSO. Commit `162ed140...`
added an out-of-line production boundary symbol; the corrected fresh gate and
independent verifier both passed. No dataset registry content, bag, ground
truth, or recorded payload was accessed.

This closes only CP2-C2 at unit scope. It does not pass CP2-C, authorize
recorded-data access, or establish AArch64/Jetson behavior. The artifact is
trusted-runner internal non-conveyable staging evidence, is not an independent
source-to-binary attestation, and is ineligible for a CP2 seal.

### CP2-C3 — recorded-evidence runner readiness — immediate next checkpoint

1. Add the three missing committed runners: `run_recorded_parity.py`,
   `run_sequence_pair.py`, and `run_timing_pair.py`; add the recorded modes of
   the existing `verify_report.py` and preserve `run_unit_gate.sh`, yielding the
   exact five preregistered entry points.
2. Integrate the completed state-trace codec and authoritative sink into the
   artifact assembler without changing the approved runtime math or commit
   boundary.
3. Implement the opaque source-provenance/readiness barrier in the exact nine
   frozen steps, including clean Git identity, immutable before/after snapshots,
   process-group timeouts, the data lock, exact unit-artifact binding, and zero
   provider calls before authorization.
4. Give all five entry points exclusive, artifact-free `--self-test` modes and
   run them under `CP2_FORBID_BAG_ACCESS=1` in fresh `/tmp` directories. Require
   each exact nonempty case inventory and every mandatory negative/corruption
   case; reject missing, extra, reordered, skipped, duplicate, or no-op cases.
5. Enforce one-to-one joins for raw, proposal, state, feature, state-block,
   covariance-block, update, configuration, source, command, and readiness
   records, with checked counts; implement artifact-bound detached-replay
   plumbing and its artifact-free synthetic tests without recorded input.
6. Run a new full unit gate at the exact clean CP2-C3 runtime commit, retain its
   external manifest anchor, independently verify it, and only then evaluate
   whether the barrier authorizes semantic registry access in a later CP2-C
   turn.

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

Require a clean HEAD that descends from the tested CP2-C2 implementation commit
`162ed140cb3fee301cf3f2e212c9afa28829798a`. A later checkpoint-metadata commit
is expected; do not confuse it with the runtime-tested source commit. If the
ancestry check fails or the worktree has unexplained changes, inspect and
reconcile them before starting CP2-C3. Do not reset or overwrite unexplained
work.
