# CP2 prerequisite-math implementation audit

Date: 2026-08-01; current addendum: 2026-08-02 (America/Toronto)

## Current decision

CP2-C2 updater transaction integration is **GO for CP2-C3 implementation** at
x86_64 strict-FP unit scope and is **not yet GO for recorded-data execution**.
The current hard stop is CP2-C3 artifact/report plumbing, opaque
source-provenance readiness, all five artifact-free entry-point self-tests,
one-to-one joins, and artifact-bound detached-replay plumbing/self-tests at one
exact clean commit. Actual recorded replay remains part of CP2-C after the
readiness barrier authorizes data access.

## Historical 2026-08-01 decision

The value-only CP2 prerequisite kernel is **GO for live integration** and is
**not yet GO for CP2-C recorded-data execution**.

This decision is bounded to the canonical serializer, shared feature gate,
nullspace/Schur evidence kernel, immutable-prior preview, global assembly, and
their strict floating-point build policy. It does not claim that the live
`UpdaterMSCKF` seam, trace writer, replay executable, five entrypoint
self-tests, or CP2-C runner is complete.

The frozen contract anchor is commit
`5365eb2a6a55d2d939d845ce0bace42126fa0b79` (tree
`1cf11647931c33236e0ac1d06b9c03f62ac73852`). Moksh Trehan's prior CP1
authorization is recorded at commit
`8d80f483752411d34a3bc4c1ff6330b3a5c0fef3`; the approved CP1 replacement
evidence is commit `952771e955fe3459f2fd43122a9c6f8ce57d1799`.

## Re-audited mathematical invariants

- Raw layouts resolve each `(covariance ID,size)` to one exact active
  top-level prior block, cover every raw `H_x` column, and cannot overlap.
- The unchanged production Givens nullspace projection and the production
  Schur reducer receive independent owning copies of the same raw system.
- Nullspace mode validity depends on finite emitted rows and a separately
  evaluated finite whitened-residual gamma. Lambda/eta/gamma evidence retains
  the frozen first-failure prerequisites and cannot introduce a live baseline
  rejection.
- Lambda, eta, and gamma comparisons use the frozen Eigen/std binary64 norms,
  multiplication-then-addition tolerance, one diagnostic ratio division, and
  direct `error <= tolerance` pass rule.
- Both paths use the same gate helper and the exact frozen
  `H*P_marg*H.transpose()`, diagonal-add, default-lower-LLT, solve, dot,
  threshold multiplication, and strict comparison order.
- Accepted IDs and gamma additions follow processing order. Candidate gamma
  overflow suppresses only candidate compression/preview and never terminates
  later per-feature traversal. Baseline gamma remains diagnostic-only.
- Each path assembles its own first-seen global block order, calls the unchanged
  production measurement compressor, and previews from the same owning,
  immutable pre-mutation prior.
- Preview arithmetic mirrors the live block-ordered cross covariance,
  marginal, innovation, LLT inverse, gain, increment, and subtractive/mirrored
  posterior ordering without repair or fallback.
- Canonical integers, binary64 values, UTF-8 strings, matrices, vectors, and
  incremental SHA-256 are value-only and deterministic. Aliased raw-byte
  appends are staged only when their range overlaps the destination buffer.

## Adversarial findings closed

The re-audit stopped implementation progress and corrected all of the
following before this decision:

1. contained prior subranges were replaced by exact top-level raw-layout
   identity matching;
2. invalid raw layouts were prevented from reaching gates or global assembly;
3. nullspace mode validity was separated from Lambda/eta diagnostic overflow;
4. mode gamma was separated from evidence gamma so the live lifecycle and the
   serialized first-failure state machine both remain exact;
5. statistic formation was restored to the required `A,b`, raw-Lambda,
   symmetry, Lambda/eta, gamma precedence;
6. actual Givens/compression and live-EKF translation units were added to the
   strict-FP source set;
7. Eigen 3.3 packet arithmetic was disabled workspace-wide to prevent explicit
   AArch64 NEON FMA, while dynamic/static alignment was pinned to 16 bytes in
   every Eigen-using package to keep one ABI;
8. canonical self-range insertion undefined behavior was removed without
   adding a per-coefficient allocation path; and
9. the unit artifact contract was expanded to prove the strict production
   source inventory, scalar/alignment definitions, exact test inventory, and
   the effective ov_core/ov_init dependency compile commands.

## Verification at the implementation checkpoint

- Isolated ROS1/catkin build: `ov_core`, `ov_init`, and `ov_msckf` all built
  successfully from the shared source tree with a fresh package reconfigure.
- Effective compile commands: all 21 ov_core, 16 ov_init, and 53 ov_msckf
  commands carried `EIGEN_DONT_VECTORIZE=1`, `EIGEN_MAX_ALIGN_BYTES=16`, and
  `EIGEN_MAX_STATIC_ALIGN_BYTES=16` in the audited host workspace.
- Strict arithmetic owners additionally carried effective
  `-fno-fast-math -ffp-contract=off -fsigned-zeros` after the repository's
  global flags.
- Focused C++ gate: 63/63 tests passed (7 CP1 and 56 CP2), including 14 direct
  raw-to-proposal shadow tests.
- Pure-Python schema primitives: 18/18 tests passed.
- The verifier's synthetic-corruption self-test and a fresh exact-commit unit
  artifact are required again after this implementation is committed; ad hoc
  dirty-tree runs are diagnostic and are not evidence.

The host results do not substitute for an AArch64/Jetson build and run. The
source and artifact policy close the known FMA/alignment portability hole;
target execution remains a later required checkpoint.

## Historical hard stop before recorded data (2026-08-01)

The following were blockers at the 2026-08-01 audit. Their current status is
superseded by the 2026-08-02 addenda below; this historical list is retained
to preserve the review trail:

- live nullspace gamma checks still alter lifecycle in `UpdaterMSCKF.cpp`;
- the live updater still owns a duplicate gate instead of the shared helper;
- the raw seam does not yet invoke the shared shadow kernel or compute both
  proposals before the sole baseline commit;
- the recorded trace writer, canonical payload files, context binding,
  independent replay, and five entrypoint self-tests are not implemented; and
- no fresh exact-commit unit artifact yet anchors this implementation.

No dataset registry, EuRoC bag, ground-truth file, or recorded-data payload may
be opened, hashed, imported, or replayed until all five entrypoint self-tests
and the fresh exact-commit unit artifact pass. CP2-C remains `pending`.

## 2026-08-02 CP2-C1 addendum

This addendum supersedes the CP2-C1 readiness items in the 2026-08-01 hard
stop, but not its recorded-data prohibition. Clean source commit
`fe00fc8a979bec8676d33961ef868ab9e64803e0` (tree
`dc6f9e1304debab4f00e769e6fc1514c7fbd3a21`) completes CP2-C1 at source and
strict-FP unit scope only. It adds one owning four-phase composite value, the
nonserialized owning phase-0 pointer-graph token, prior/postcommit canonical
codecs and state-file framing, a value-only phase-0 preview projection, a
detached production-type phase-2 oracle, and preallocated nonthrowing,
allocation-free phase-3 fill and handoff.

The re-audit found no remaining CP2-C1 mathematical blocker:

- Active top-level intervals and semantic subblocks exactly partition the full
  covariance. Clone, landmark, calibration, camera-cache, FEJ, map, and
  pointer identities are retained and checked without serializing addresses.
- Every serialized binary64 coefficient is bit-preserving. Passing snapshots
  are finite, and the validator applies the frozen squared-norm boundary
  `abs(q.squaredNorm()-1) <= 32*epsilon` to every runtime quaternion-bearing
  field; the full-role protecting fixture exercises 14 such fields.
- Phase 2 copies the accepted proposal's complete posterior covariance and
  applies each complete `dx[id:id+size]` exactly once to one detached
  production `IMU`, `PoseJPL`, or inherited-equivalent `Vec` object.
- Phase 3 rereads every live canonical value after proving the original
  pointer graph. A real `StateHelper::EKFUpdate` fixture produced bit-identical
  phase-2 and phase-3 composites with unchanged FEJ and identity sidecars.
- The 14-test focused gate includes a review-locked 19,335-byte known-answer
  payload with SHA-256
  `6d6ab4536c957dd9eed060f7cc32cd03e2d2d270df77f24098e35823bebeed44`,
  every serialized coefficient mutation, 222 metadata/shape/list mutations,
  45 pointer-association mutations, all 14 fixture quaternion boundaries,
  hostile framing/count/offset cases, nonfinite encoding, checked-ID overflow,
  and allocation-failure injection.

The fresh serialized artifact is
`results/staging/cp2/unit/cp2_unit_20260802T152708655097400Z-gfe00fc8a979b-TAamaoSX`.
Its external `SHA256SUMS` anchor is
`85deb80c2fe03e5379addc9609b7f73bdf90c61ea8412d4b7c7fdc169948d945`.
All six build commands, 15/15 captured executables, and 91/91 CP1/CP2 cases
passed; independent re-execution passed with unchanged inputs, strict-FP and
dependency Eigen-ABI checks passed, and the verifier rejected all 49 synthetic
corruptions. The artifact is x86_64 trusted-runner staging evidence, is not an
independent source-to-binary attestation, and is ineligible for a CP2 seal.

The prior missing-fresh-artifact blocker was therefore closed only for the
isolated CP2-C1 primitives. At that CP2-C1 checkpoint, the hard stop was CP2-C2
updater integration:
phase-0 promotion and sole-prior provenance, complete dual-proposal traversal,
unpublished phase 2, value-before-validity phase-1 comparison and pointer
proof, zero-intervening-operation commit ordering, endpoint-first/allocation-
free phase 3, authoritative sink and fatal latch, exact checked counters, and
the full protecting-test matrix. CP2-C3 must then close provenance/report
binding, one-to-one artifact joins, detached replay, and all five artifact-free
readiness entrypoint self-tests. CP2-C/D/E, recorded-data execution, and
AArch64/Jetson evidence remain unpassed and unauthorized.

This addendum is post-run checkpoint metadata. It records, but was not itself
included in, the exact source tree tested by the artifact.

## 2026-08-02 CP2-C2 addendum

This addendum supersedes the CP2-C2 hard stop above, but not the recorded-data
prohibition. Clean source commit
`162ed140cb3fee301cf3f2e212c9afa28829798a` (tree
`46935c58e7a7184f659a6f67fcd33896ebe02d46`) completes updater integration at
strict-FP unit scope only. CP2-C3 and the complete recorded CP2-C/D/E campaigns
remain unexecuted and unpassed.

The mathematical and transaction re-audit found no remaining CP2-C2 blocker:

- Tentative phase 0 is captured after feature cleanup/triangulation and before
  raw-system assembly. Its canonical bytes and semantics must validate before
  the first raw row or raw counter exists; failed promotion is run-fatal with
  zero raw count.
- Every raw layout is captured from production ordering, then validated and
  resolved against phase 0. Every gate covariance and proposal is derived from
  the one owning projected phase-0 prior. Baseline and candidate traverse the
  complete shared population before a commit decision, while every
  population/product uses checked arithmetic.
- Baseline and candidate retain the exact raw/proposal bytes and first-failure
  provenance. Gamma is copied from the production lifecycle whenever it is
  available; evidence arithmetic cannot invent a second acceptance path or
  veto a valid baseline proposal.
- A commit-eligible baseline constructs and validates detached expected phase
  2 and prepares phase-3 storage before final phase-1 verification. Any later
  suppression discards unpublished phase 2. Phase 1 is compared byte-for-byte
  with phase 0 before its independent semantic validation, then proves the
  original owning pointer graph.
- After the final phase-1 value, semantic, and pointer-graph proof, the literal
  production boundary is the sole `StateHelper::EKFUpdate`, followed
  immediately by the steady-clock endpoint and the allocation-free/nonthrowing
  phase-3 fill and handoff. The out-of-line production status mapper runs only
  after fill and before boundary-output publication; it both preserves that
  adjacency and makes the boundary test load the snapshotted production DSO.
- Phase 3 is independently validated and compared exactly with detached phase
  2. A complete encodable nonfinite or unequal phase 3 remains
  `committed_counted` failed evidence; incomplete shape/inventory,
  checked-arithmetic failure, pointer replacement, or sink rejection sets the
  sticky first fatal reason. No rollback is claimed after the live commit.
- Zero-raw terminals emit no state phase; raw noncommit terminals emit exactly
  `[0,1]`; commits emit exactly `[0,1,2,3]`. The authoritative status-returning
  sink publishes before the diagnostic observer, and observer behavior cannot
  determine evidence acceptance.
- Baseline expected-update, verified nominal, nominal mismatch, full ordered
  covariance mismatch, FEJ mismatch, block-row, and campaign commit-mismatch
  counters retain their approved units. Structural failure zeros the numeric
  comparison counters. Candidate live-write count is identically zero.
- `online_math_evidence_passed` remains a necessary online condition only; it
  is not represented as the final CP2-C schema's `math_passed`, which also
  depends on complete recorded joins and detached replay during CP2-C after
  CP2-C3 readiness passes.
- Production and fault-injection runtimes are separate link products with an
  exact 23-source inventory. The private test macro does not enter the
  production DSO, and production/fault updater tests cannot cross-link or
  redirect their declared output libraries.

The protecting matrix contains 139 successful executions across 18 binaries:
7 CP1 regressions and 132 CP2 executions. Focused CP2-C2 coverage includes 14
production updater cases, the same 14 updater cases against the fault runtime
plus 14 additional fault cases (28 total in that binary), 14 composite cases,
10 detached-oracle cases, 4 literal-boundary cases, and 8 trace-codec cases.
All 139 executions independently re-ran under the retained loader environment
with zero failures, errors, or disabled cases. Strict floating-point,
dependency Eigen-ABI, exact source/link/output binding, no-fast-math, and
production/fault isolation checks passed.

The first fresh gate at `fa350bf5caccc9789c70d0f69660881608963c0c`
correctly failed closed: all 139 executions passed, but the commit-boundary
implementation was header-only, so the boundary executable had no `DT_NEEDED`
reference to `libov_msckf_lib.so` and production-library provenance was
unproven. Commit `162ed140...` moved the post-fill status mapping to one
out-of-line production symbol. The corrected boundary executable then loaded
the snapshotted DSO, and the complete fresh gate passed.

The retained staging artifact is
`results/staging/cp2/unit/cp2_unit_20260802T185623152114466Z-g162ed140cb3f-WtrFa1xv`.
Its externally retained `SHA256SUMS` SHA-256 is
`58bc351d513bb093a6e355a808fcf62a7d952e01dee5902594ed74946d76f579`;
the report SHA-256 is
`b1eda70bc3fab8f73dc060deb8654d72d9967774ae664ab5e8f7bd641bb16a33`.
All 6/6 build commands and 18/18 executables passed, the manifest contains 110
entries, the verifier rejected all 85 synthetic corruptions, and a separate
minimal-environment verification matched the external manifest anchor.

This remains x86_64 trusted-runner, internal non-conveyable staging evidence.
It is not an independent source-to-binary attestation, is ineligible for a CP2
seal, and provides no AArch64/Jetson result. No dataset registry content,
EuRoC bag, ground-truth file, or payload was accessed. The next hard stop is
CP2-C3: all five artifact-free entry-point self-tests, the opaque
source-provenance/readiness barrier, one-to-one artifact joins, and
detached-replay plumbing plus its artifact-free self-tests must pass at one
exact clean commit before data access can be authorized. Actual recorded replay
remains part of CP2-C after authorization.

This addendum is post-run checkpoint metadata. It records, but was not itself
included in, the exact source tree tested by the artifact.
