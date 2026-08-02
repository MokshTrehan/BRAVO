# CP2 prerequisite-math implementation audit

Date: 2026-08-01 (America/Toronto)

## Decision

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

## Hard stop before recorded data

The following remain blockers, not waived follow-up items:

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
