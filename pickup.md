# SchurVIO-Lite CP2 pickup

Last updated: 2026-08-04 (America/Toronto)

## Exact resume state

- Repository: `/home/moksh/newSlam variant`
- Branch: `schurvio-lite/cp2-one-pass`
- Approval-bound source-freeze commit:
  `c0dfecfd61e3ac067c4020bb61ab6421df926500`
- Approval-bound source-freeze tree:
  `bd5372514f62cae4e7505e8008ba01b76e9b8520`
- The worktree was clean at source freeze, formal unit-gate invocation, and the
  single authorized CP2-C process invocation. Any later handoff-only commit is
  not the tested source identity and cannot reuse this unit artifact.
- Remote branch tip observed immediately before the failed readiness handoff:
  `f98eb347552ddab8965d899516143344dcb8a0b4`; local source-freeze `HEAD` was
  ahead by two commits and had not yet been pushed.
- Pinned OpenVINS upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
- CP2-A and CP2-B are passed. CP2-C, CP2-D, and CP2-E are not passed.
- The complete CP2-C detached-readiness replacement in
  `docs/cp2_c_detached_readiness_binding_clarification_proposed.md` was
  approved as reviewed at commit
  `0d71fee98499a10df4e92176709c1afe14077f90`, with no exceptions.
- `project/cp2_c_detached_readiness_binding_approval.json` records that human
  approval and is source-bound with all executable bindings at `c0dfecfd...`.
- Moksh exactly acknowledged and accepted the expanded, quarantined incident
  disposition reviewed at commit
  `46b3010ad5e77ea61f9a571ccb7842ea2e0f1979`, with no exceptions and with the
  prohibition on every exposed or incident-derived influence preserved.
- Exact acknowledged incident-log identity at `46b3010...`: Git blob
  `644f35ba9e371ae27efc2827e146f8b69dab70fe`, SHA-256
  `5d1f8aa1728bedd6141a55c756ef9dc3bfd2d52e7480208c27a94324b321af56`.
  `project/cp2_predata_incident_disposition_approval.json` records the exact
  statement with Git blob `c6c1a1b29324b31152cf038d6375fc216ca6f57c` and
  SHA-256 `80d227032c77a060d4bae5b48c869cc207c999ab7e2f2259bc884767ca11412e`.
  The record and its governing bindings are source-bound at `c0dfecfd...`.
- The sanitized exact-HEAD unit gate was invoked exactly once at `c0dfecfd...`
  and exited `0`. It published the immutable unit-only staging artifact
  `/home/moksh/newSlam variant/results/staging/cp2/unit/cp2_unit_20260804T115532208174128Z-gc0dfecfd61e3-1b7s3Qlz`
  with external `SHA256SUMS` anchor
  `8676cb804692230a639b9f212f979955d2ef7da48187467ef4863ee8766950c7`.
- One supplemental post-cleanup invocation used the ordinary positional unit
  verifier and failed its dependency source checks because that mode requires
  the intentionally deleted live unit workspace. Contract and code audits
  confirmed that this was a non-protocol diagnostic, not the required detached
  readiness check. The gate was not rerun, and the hidden prevalidated verifier
  was not invoked manually.
- The single authorized CP2-C process was then invoked once at the same clean
  source identity. It exited `78` during the pre-data readiness barrier with
  `tracked source may not live under separately snapshotted roots`.
- Exact cause: `HEAD` tracks 75 historical CP0/CP1 evidence files under
  `results/immutable/{baseline,cp1}`, while `cp2_readiness.py` unconditionally
  rejects every tracked path whose first component is `build`, `results`, or
  `Testing`. The unit artifact's `source_snapshot.tar` contains all 75 tracked
  files, so this is a readiness source-topology/invariant defect, not missing
  frozen source evidence. The rejection also contradicts the approved
  replacement rule that the canonical context contains every stage-0 tracked
  file; this is an implementation-conformance defect, not a new math contract.
- Readiness failed before its self-tests, data lock, detached unit-anchor check,
  registry open, bag resolution, or campaign. No recorded input was accessed;
  no CP2-C recorded result was created; no retry or tuning was attempted.
- The frozen, data-free CP2-C source has clean pairing/producer
  math and post-fix campaign-publication audits. Nonformal protecting runs pass
  campaign `11/11`, approval-bound actual runner `12/12`, opaque repository
  `12/12`, sequence actual `15/15`, sequence runner `15/15`, and strict C++
  pairing `6/6`. After exact acknowledgment binding, readiness passes `31/31`,
  the approval-bound actual runner passes `12/12`, and the aggregated verifier
  self-test passes all `43` readiness-engine cases. These are protecting
  results, not current formal evidence.
- Unit-artifact publication performs anchored frozen
  verification before its no-replace rename, exact-inode rollback after a
  post-rename failure, and caller reconciliation after interruption. Its
  synthetic fault-injection corpus passes and the independent post-fix audit is
  clean at the frozen source identity.
- Moksh selected desktop `x86_64` for CP2-D and retained the frozen fixed-clock
  desktop checkpoint for CP2-E. Jetson work is deferred beyond CP2; desktop
  results are not Jetson evidence.
- `docs/cp2_d_e_desktop_inventory_candidate.md` is a data-free, read-only,
  nonauthorizing inventory candidate. It is not a D capsule, an E profile, or
  execution evidence.
- CP2-D has no exact committed and approved evaluator/direct-math capsule
  identity. Ambient evo/Python/NumPy/native-loader/license/source material is
  not an admissible capsule and remains blocked pending an exact closure and
  frozen known answers.
- Independently, the current CP2-D artifact contract does not retain a complete
  chronological filtered camera-message candidate view. A detached verifier
  therefore cannot independently prove first-forward/no-search-past pairing;
  correct producer code and runtime/extractor byte equality are insufficient.
  D needs an approved evidence/schema extension and protecting tests.
- CP2-D publication also remains blocked: its sequence publication helpers do
  not yet prove descriptor-relative, failure-atomic reconciliation after every
  post-rename error/interruption. This is separate from the capsule blocker.
- CP2-E has no exact committed and approved timing-profile identity. The host
  is presently unsuitable for official fixed-clock evidence because its
  observed state includes `ondemand`, enabled boost, no frozen affinity, and no
  complete CPU thermal surface.
- CP2-D/E recorded-input access and actual execution remain unauthorized until
  Moksh approves their exact committed capsule/profile identities. No host
  control may be changed under the inventory authorization.

Historical unit artifacts and their manifests are historical evidence only.
They do not test the current live tree, do not activate current readiness, and
must never be described as the current artifact. Any tracked change requires a
new complete exact-commit unit gate before a later actual invocation.

## Non-negotiable boundary

Readiness did not pass. Until a repaired exact source identity is reviewed,
approved, unit-tested, and passes the one-shot readiness barrier:

- do not inspect or semantically parse the dataset registry;
- do not resolve, open, hash, or sample a bag or ground-truth file;
- do not inspect recorded results or invoke a CP2-C/D/E actual-mode path;
- do not run a build or evidence command that can transitively touch those
  surfaces; and
- do not use any incident-derived value in implementation, thresholds,
  profiles, fixtures, tests, sequence choices, or execution choices.

The authorized D/E inventory was local, data-free, read-only, and
nonauthorizing. It did not authorize a network installation, a capsule build,
host-control mutation, recorded-input access, or recorded execution.

## Math-first requirements

Mathematical correctness is the first priority at every checkpoint. A passing
build or test run is necessary but is not, by itself, a proof. Before source
freeze, independently recheck at least:

- the frozen nullspace/Schur-equivalent whitening, rank, conditioning, QR,
  lambda/eta/gamma, chi-square, and no-repair/no-fallback semantics;
- shared-prior, proposal, state, covariance, commit-oracle, and whole-update
  failure-atomicity semantics;
- exact checked counters, finite-state requirements, source/provenance joins,
  and detached replay;
- stereo pairing's strict `< 20 ms` boundary, chronological record-time
  ordering, first-forward-candidate rule, no reuse, exact integer schema, and
  positive selected duration; and
- failure containment, descriptor identity, descendant termination, immutable
  sealing, trusted-parent verification, and no-replace publication.

Any mathematical or evidence-contract discrepancy stops source freeze. Never
tune a threshold, profile, sequence, fixture, or implementation after looking
at recorded data.

## Exact continuation flow after the failed readiness attempt

The order below is mandatory. CP2-C/D/E must not be run in parallel.

1. Preserve the failed attempt exactly. Do not rerun its unit gate, readiness
   barrier, hidden verifier, or campaign, and do not relabel its exit-`78`
   readiness failure as a pass.
2. Prepare a data-free implementation-conformance repair under the already
   approved replacement: remove the blanket tracked-path rejection for all
   three separately snapshotted roots. Do not exclude those tracked files from
   the source context or source archive; retain their simultaneous exact
   coverage in the physical root snapshot.
3. Add positive tracked `0100644` and `0100755` fixtures under the snapshotted
   roots; prove exact source-context/archive and root-snapshot coverage while a
   neighboring untracked leaf remains root-only. Add missing/mode/hash/blob,
   mutation/substitution, special-file/control-file, and detached
   tracked-versus-other classification rejections, all with zero registry/bag-
   provider access. Re-run the full math, source-binding, publication, and
   corruption audits data-free.
4. Commit the complete repair and repin every readiness digest. Obtain Moksh's
   explicit approval of that exact repaired commit/tree. No new contract
   clarification is presently required, but the current approval does not
   authorize a second attempt at a changed identity.
5. Only after that approval, run one new clean exact-HEAD unit gate exactly once.
   If it fails, stop without retry or tuning.
6. Only if the new unit gate passes, invoke the one-shot CP2-C process exactly
   once. Its readiness stage must pass before its sole registry read and frozen
   recorded campaign. Any readiness or campaign failure stops the attempt.
7. After successful no-replace publication, independently verify the externally
   anchored recorded artifact. Publication plus detached verification is the
   CP2-C success boundary.
8. Do not execute CP2-D or CP2-E. First freeze exact committed D capsule and E
   profile candidates, including all evaluator/native/license/source/known-
   answer and host/CPU/affinity/clock/boost/thermal bindings. Then obtain
   Moksh's explicit approval of those exact committed identities. Each later
   source/profile commit requires its own exact-HEAD unit/readiness chain.
9. Run D only after C passes, and E only after C and the required preceding
   gates pass. Any failure stops subsequent work. Desktop results are not
   Jetson evidence.

## D/E candidate blockers

CP2-D remains `data_free_candidate_only`:

- no immutable evaluator/direct-math capsule identity exists;
- the ambient evo/Python/NumPy/native closure is not trusted as a capsule;
- loader and native dependency closure, license notices and corresponding
  source, relocation behavior, and stack-specific known-answer bits are not
  frozen and approved; and
- the detached artifact lacks an independently sufficient complete candidate
  witness for first-forward/no-search-past pairing;
- sequence publication still has unresolved post-rename rollback/
  reconciliation failure windows; and
- recorded inputs and D actual execution are unauthorized.

CP2-E remains `data_free_candidate_only` and
`blocked_pending_fixed_clock_profile`:

- no exact machine/CPU-set/profile artifact is committed or approved;
- the observed governor is `ondemand` and boost is enabled;
- no exact affinity is frozen;
- CPU thermal/throttling observation is incomplete; and
- neither host-control mutation nor recorded E execution is authorized.

## Required handoff

At the next stop, report separately:

- the exact source commit/tree and whether the worktree is clean;
- incident acknowledgment and approval-binding status;
- protecting-test and independent-audit results, without inflating them into
  formal gate evidence;
- exact unit, readiness, CP2-C campaign, and detached-verification status;
- whether any recorded input was accessed;
- any immutable artifact path and external anchor that was actually created;
- D capsule and E profile identity/approval status; and
- whether local and remote branch tips are identical.

Do not mark CP2 complete unless CP2-C, CP2-D, and CP2-E all pass their exact
approved gates. Keep `nullspace` as the default and keep CP3/two-pass work
blocked until then.
