# SchurVIO-Lite pickup and next-step plan

Last updated: 2026-08-03 (America/Toronto)

## Resume point

- Repository: `/home/moksh/newSlam variant`
- Branch: `schurvio-lite/cp2-one-pass`
- Approval-gated CP2-C3 review-candidate base commit: `71e2c56f1b63a391f417b0243edd5b1355c74012` (tree `0c2e4953f5f0288ab4e8da27e6512ef852503f90`).
- Exact runtime/provenance replacement commit: `0d71fee98499a10df4e92176709c1afe14077f90` (`fix: bind CP2 unit evidence provenance`; tree `9e3dd0c72134b319f17c002c41aa63337de26414`).
- Expanded preauthorization incident-log commit:
  `c2f3ad9c54ed126cc69526c76adb3134c3bf9545` (tree
  `193734a27077ba297747ade987b5c458934b8256`).
- Previous exact-commit unit artifact:
  `results/staging/cp2/unit/cp2_unit_20260803T090606610655669Z-g0d71fee98499-pry4lam2`.
- Previous artifact `SHA256SUMS` external anchor:
  `b2cd771dc014da423a0fb57d2e9bc44a1da65b4bccd40b2fa9a7a01fe585bdd1`.
- Later pre-hardening exact-commit unit artifact:
  `results/staging/cp2/unit/cp2_unit_20260803T114617220547095Z-ga4697813c200-NjBpTlRQ`.
  It binds commit `a4697813c200d1716aa8622f05873fa69af05be6`, tree
  `58531da7102a4cd22dbd49972d4989e045279029`, and external
  `SHA256SUMS` anchor
  `02631c7dc83aafa278830224cb547c589d4a226394ab0a67745a083804c11f90`.
- Focused C3/D/E mathematical and lifetime hardening commit:
  `a3b27a18f865dcfd5e3933ba9efecd302bff2d38` (tree
  `2104f7ab127d4d561e6b8bc2314ff635c0da7805`). This pickup commit follows
  it, so one new exact-HEAD unit artifact is still required before stopping.
- Data-free CP2-D candidate commit:
  `d53139a4bc799e1625a290fec146eef15cfbe1f5` (tree
  `75740cec9261d6fa86e5c8dd29125e62dac85dd0`).
- Data-free CP2-E exact-math candidate commit:
  `66f4eaf56bb42258214c25fe078e78b3cc0eeb15` (tree
  `ccdb530e9f4c4f7da0a484ae3b6c8b4e5acf1998`).
- Approved CP2-C clarification: `b37eff6e5baa035175e1dde3cae52ee496ca9e2d`
- Approved CP1 mathematical contract: `952771e955fe3459f2fd43122a9c6f8ce57d1799`
- Proposed replacement `docs/cp2_c_detached_readiness_binding_clarification_proposed.md` is still pending explicit approval and non-authorizing.
- Proposed `docs/cp2_d_evaluator_precision_clarification_proposed.md` is
  non-authorizing; its required evaluator/direct-math capsule identities and
  known answers must first be frozen in a reviewed implementation commit, then
  explicitly approved.
- Proposed `docs/cp2_e_fixed_clock_and_exact_timing_clarification_proposed.md`
  is non-authorizing. It freezes a review candidate for exact timing math and
  the future profile/artifact boundary, but no real profile has been created.
- Pinned OpenVINS upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
- Final target architecture: NVIDIA Jetson Nano family, CPU-only for the
  claimed path. The currently frozen CP2-E contract still says fixed-clock
  **desktop**; Jetson evidence remains CP6 unless Moksh explicitly approves an
  architecture replacement.
- Current gate state: CP2-A and CP2-B passed; CP2-C1 and CP2-C2 passed
  unit-only. CP2-C3 implementation exists, but formal readiness is `not_run`
  because its replacement and the incident disposition are unapproved. CP2-C
  is unexecuted. CP2-D and CP2-E now have data-free review candidates but are
  still unexecuted and approval/profile blocked.
- Formal-gate/campaign access: the exact unit artifact reports
  `dataset_or_bag_accessed=false`. No bag or recorded-result artifact was
  opened; no provider, readiness campaign, or CP2-C/D/E actual-mode runner was
  invoked. This narrow formal claim does not erase the separately disclosed
  preauthorization development incidents in
  `docs/cp2_predata_incident_log.md`.
- The CP2 deadline has been missed without a waiver. Requirements remain unchanged and the CP3 schedule is compressed.

Before resuming, require no unexplained worktree changes and verify ancestry
from the tested implementation checkpoint above. Read, in order:

1. `docs/conventions.md`
2. `docs/cp2_one_pass_contract.md`
3. `docs/cp2_recorded_evidence_contract.md`
4. `docs/cp2_artifact_schema.md`
5. `docs/cp2_c_composite_and_readiness_clarification.md`
6. `project/cp2_c_clarification_approval.json`
7. `docs/cp2_c_detached_readiness_binding_clarification_proposed.md`
8. `docs/cp2_d_evaluator_precision_clarification_proposed.md`
9. `docs/cp2_e_fixed_clock_and_exact_timing_clarification_proposed.md`
10. `project/cp2_gate.yaml`

Do not inspect dataset registry contents, bags, ground truth, or payload data until the frozen CP2 readiness barrier permits it. Mathematical correctness remains the first priority; a build or run is not evidence of correctness by itself.

## 2026-08-03 satisfactory hardening follow-up

Commit `a3b27a18f865dcfd5e3933ba9efecd302bff2d38` closes three issues found by
fresh contract-to-source audits while keeping every public actual path blocked:

- CP2-C3 authorization now descriptor-binds the private readiness root,
  rehashes and revalidates its exact frozen-unit and attachment inventory
  immediately before the sole registry read, and rejects every tested
  post-barrier root/file/inventory/mapping mutation before that read. Cleanup
  removes descendant-mutated owned trees without following links, preserves a
  symlink victim, and refuses a substituted root without deleting replacement
  or displaced bytes.
- CP2-D rejects the mathematically impossible `RMSE < mean` relation for a
  nonnegative error population at an exact one-binary64-ULP boundary. Its new
  direct-math KAT transport binds all five proposal input arrays, output
  surfaces, exact order, two complete repeats, shared-alignment bytes, and the
  frozen ordered-RMSE and p95 results. Stack-specific SVD/Kabsch answer bits
  remain deliberately unset until a real approved capsule exists.
- CP2-E now enforces the proposed u128 retained-rational and gate-product
  domains, uses the full u256 comparison domain for ratio ordering, and has a
  strict 32-lowercase-hex-digit scalar codec. The exact `Fraction` oracle was
  rerun on 49,600 decisions after the correction with no disagreement.

The combined data-free Python inventory is now 243/243 across 13 files.
Formal inventories remain 39/39 for readiness and 37/37 for E; D is now 78/78
(26 capsule, 11 direct-KAT, 29 evo ZIP/statistics, 12 binary64 codec). The
recorded, sequence, timing, and union-verifier public self-tests also pass at
48/48, 36/36, 43/43, and 83/83. These are protecting results, not CP2-C/D/E
evidence.

## 2026-08-03 CP2-D/E data-free overnight outcome

This section supersedes the older D/E planning language below. The completed
work is a pair of review candidates, not an actual CP2-C, D, or E run.

### Hard checkpoint D — committed, tested, still non-authorizing

Commit `d53139a4bc799e1625a290fec146eef15cfbe1f5` adds:

- a regular-file-only, relocation-neutral `.cp2cap` transport and strict
  evaluator/direct-math profile grammar;
- descriptor-pinned Linux staging, complete rehashing, no-replace publication,
  and failure cleanup that rejects directory/file substitution without
  deleting substituted victim bytes;
- exact distribution, interpreter, launcher, environment, floating-point,
  native-consumer/loader/RPATH/RUNPATH/SONAME/`DT_NEEDED`, provider-search,
  license, version, and known-answer bindings;
- a byte-accounting classic-ZIP/evo `stats.json` parser that retains the
  full-precision RMSE, rejects `RMSE < mean`, and treats six-decimal console
  output as diagnostic;
- a canonical finite-binary64 array codec with checked shape/resource
  arithmetic and no JSON floats; and
- a canonical five-case, twice-run direct-KAT request/response boundary that
  binds exact proposal inputs and reviewed output bits.

The isolated protecting inventory is exactly 78/78: 26 capsule/profile/stager,
11 direct-KAT, 29 ZIP/statistics, and 12 binary64-codec tests. The final cleanup
regressions cover both descendant-directory and regular-file substitution. The
D proposal still does not contain real evo/CPython/NumPy/native capsule bytes,
and both public D paths remain blocked before artifact or recorded-input
access.

### Hard checkpoint E — committed, tested, still non-authorizing

Commit `66f4eaf56bb42258214c25fe078e78b3cc0eeb15` adds an exact, clock-free
timing-math layer and proposed replacement document:

- p50 and p95 use exact NumPy-linear interpolation with `q=1/2` and `19/20`,
  never nearest rank or binary64 conversion;
- quantiles retain reduced rational nanoseconds plus the complete sorted u64
  population and its rederived domain-separated SHA-256;
- candidate/baseline ratios, all `11/10` and `23/20` gates, and the
  median-of-three ordering use exact integer cross products with u128 retained
  values/gate products and u256 ordering products;
- Boolean, negative, overflow, unstable-sequence, resource, forged nested
  evidence, mixed-population, and zero-baseline inputs fail closed; and
- the still-blocked public timing runner and detached verifier now require the
  complete exact timestamp intersection and use independent stdlib-only exact
  rational corruption oracles.

The isolated E math suite is 37/37. An independent exact `Fraction` oracle
agreed on 49,600/49,600 cases. The public timing self-test is 43/43 and the
union verifier self-test is 83/83; they add omitted-bilateral, u64-overflow,
above-`2^53`, and exact ratio-boundary mutations. Across the 13 CP2 Python test
files, the current protecting inventory is 243/243. The formal verifier gate
also requires exact 39-test readiness, 78-test D, and 37-test E result lines.

No `project/cp2_timing_profile.yaml` was guessed or created. No host clock,
governor, frequency, boost/turbo, affinity, thermal, registry, bag, ground
truth, recorded artifact, or user-local evaluator bytes were read or changed
in this D/E work. This claim is limited to this data-free work and does not
erase the earlier incidents in `docs/cp2_predata_incident_log.md`.

### Exact blockers and next authorized actions

1. CP2-C3 still requires Moksh's incident-log disposition and approval of the
   complete detached-readiness replacement, followed by one separate
   source/approval-binding commit and a fresh exact-HEAD unit gate.
2. CP2-D additionally requires Moksh to choose desktop `x86_64` versus Jetson
   `aarch64` for this checkpoint and authorize a named local evaluator/direct-
   math source set or audited build. Only then may real capsules, complete
   native/license inventories, version/preflight bytes, and stack-specific
   known-answer bits be frozen for another exact review and approval.
3. CP2-E requires an explicit decision to retain the frozen desktop checkpoint
   or replace it with Jetson, plus the exact machine/CPU IDs, fixed clocks,
   governor/driver, affinity, boost/turbo, thermal policy, read-only sampling
   commands, sequence/input identity, and resource bounds. The runner validates
   but never changes those controls.
4. Actual execution remains strictly serialized C readiness -> C -> D -> E.
   A failure stops later stages; no data-driven tuning, threshold changes,
   alternate profile, or silent retry is permitted.

The next mechanical action after this pickup is committed is a complete unit
gate at that exact clean `HEAD`. That unit anchor is necessary provenance but
cannot substitute for any missing human approval above.

The first such attempt at commit `e808c10070b380565ac6598fe59e7edd362fc803`
failed closed during the pre-artifact verifier self-test, before any evidence
directory, build, registry, bag, or clock access. Under the gate's required
`umask 077`, a test fixture's `mkdir(mode=0755)` became mode `0700`, so the
fixture no longer represented the unsafe parent it was meant to reject. The
production stager did not fail; the protecting test setup was environment-
dependent. The replacement explicitly `chmod`s that synthetic directory to
`0755`; rerun the complete gate at the resulting clean commit and do not treat
the failed attempt as evidence.

## 2026-08-03 overnight outcome

The authorized result is a clean, exact-commit CP2-C3 review candidate plus a
fresh passing unit anchor. It is the maximum valid progress before Moksh's next
approval; it is **not** a CP2-C3 readiness pass and is **not** completion of
CP2-C/D/E.

### Implementation and review result

- `71e2c56...` added the three recorded/sequence/timing entry points, recorded
  verifier modes, state/artifact assembly, detached replay, exact joins,
  checked counters, readiness plumbing, and fail-closed actual-mode blocks.
- `0d71fee...` is the replacement review commit. It fixes canonical unit-test
  order, binds the standalone recorded assembler's real translation unit and
  `CP2_RECORDED_ASSEMBLE_NO_MAIN=1` macro exactly, forbids path injection, and
  hardens the assembler harness's status/diagnostic, rounding-mode, PID,
  descriptor, watchdog, and cleanup proofs.
- Independent final math and security reviews returned green. The exact
  lambda/eta/gamma, gate, state, covariance, finite-state, counter,
  failure-atomicity, and no-repair/no-fallback semantics remain unchanged.
- In separate precommit, artifact-free, unretained development runs, all five
  public entry-point self-tests passed with `CP2_FORBID_BAG_ACCESS=1` and zero
  bag-provider calls. These are non-formal and must be rerun after approval
  binding. The retained unit verifier independently reports 39/39 readiness
  and binding protecting tests passed.

The standalone assembler unit test directly embeds and calls the retained
assembler entry/core translation unit; it does not independently attest an ELF
loader or production `main`. The later authorized campaign must bind and invoke
a freshly built standalone assembler ELF, so that loader/entry-point boundary
remains a recorded-campaign proof rather than a unit-gate claim.

### Preauthorization incident disclosure

The complete disclosure is `docs/cp2_predata_incident_log.md`; do not summarize
the overnight as “no recorded-input access.” In the non-evidence development
session before the clean anchor, one command opened `MH_01_easy` ground truth
and printed its header, 19 rows, and hash, and a later typo search printed four
`project/datasets.yaml` lines. An earlier 2026-08-02 broad search could open the
registry without printing a registry value. During this handoff, a broad
sanitizer-reference search also omitted the registry/ground-truth exclusions;
because both files were eligible for search, that invocation is conservatively
recorded as opening both even though neither path appeared in retained stdout.

These accesses were read-only, were not made by a campaign, and supplied no
value to implementation, constants, thresholds, fixtures, tests, or execution
choices. No bag or recorded-result artifact was opened; no provider, readiness
campaign, or CP2-C/D/E actual-mode runner was invoked. The formal
`0d71fee...` unit gate used an exact clean source/workspace and reports only
its own zero-access scope; the last broad-search incident occurred after it
finalized. Future recorded evidence must start from a new exact clean commit/
unit anchor and a fresh-process, approval-bound readiness barrier, with nothing
learned from these incidents carried forward.

### Formal exact-commit unit evidence

- Source before and after: commit `0d71fee...`, tree `9e3dd0c...`, branch
  `schurvio-lite/cp2-one-pass`, clean in both snapshots.
- Original execution: 203/203 GoogleTest cases passed across 25/25 binaries;
  failures, errors, and disabled cases were all zero.
- Independent retained-binary re-execution: the same 25/25 exit statuses were
  zero and the same 203/203 cases passed.
- Strict floating-point inventory passed for all 25 targets with
  `-fno-fast-math`, `-ffp-contract=off`, `-fsigned-zeros`, and the frozen Eigen
  ABI macros. Dependency Eigen ABI verification passed. Validation errors were
  empty.
- CP2-A accepted corpus: 1,024 fixtures. Maximum tolerance ratios were
  lambda `0.0186021`, eta `0.0222892`, retained gamma `0.00458785`, covariance
  `0.0000632893`, state increment `0.000227707`, and NIS `0.0000751222`.
  CP2-A rejected corpus: 128/128 classified fixtures.
- CP2-B state-update corpus: 128 fixtures, 256 accepted previews, 256 clone
  calls, and 256 live commits. The five rejection cases made zero state
  mutations; every reported jitter, repair, alternate-solve, clamp,
  regularization, and fallback count was zero.
- Artifact status is exactly `passed_cp2_a_b_cp2_c2_unit_only`; it deliberately
  reports CP2-C3/C/D as `not_run` and CP2-E as
  `not_run_blocked_pending_fixed_clock_profile`. It is trusted-runner,
  internal, non-conveyable staging evidence, not an independent
  source-to-binary attestation and not eligible for a CP2 seal.

The formal artifact contains no sanitizer build or sanitizer execution, so no
sanitizer pass is claimed from it. Separately, the dirty-tree development
diagnostics recorded in `docs/cp2_math_implementation_audit.md` passed 203/203
under UBSan, 189/189 under ASan+LSan, and a 14/14 composite-state supplemental
UBSan run; those diagnostics are useful corroboration but are not the clean
commit unit anchor.

### Retained failed-closed precursor

The first clean candidate run at `71e2c56...` retained
`results/staging/cp2/unit/.cp2_unit_20260803T074825444378033Z-g71e2c56f1b63-gHHhbG9T.partial.Uzr5oXCz`
and its workspace
`build/cp2-unit-workspaces/.cp2-unit-20260803T074825444378033Z-g71e2c56f1b63.workspace.gHHhbG9T`.
It exited 2. All 203 tests passed; the failure was evidence validation, not
estimator math. The two root causes were a runner/verifier ordering mismatch
for the offline/recorded/feature tests and the verifier expecting two instead
of the deliberately compiled three assembler test translation units. The
ordering root cause generated five validation messages; the source-inventory
root cause generated one.
Nothing was overwritten or relabelled.

### Exact evidence limitation after cleanup

The gate's own final independent verification ran after atomic finalization and
before its trap removed the temporary build workspace; it passed and the runner
exited zero. A later default live-verifier invocation correctly fails closed
because the retained dependency compile/output records name that now-removed
temporary workspace. This does not overturn the completed formal gate, and the
external `SHA256SUMS` hash still matches exactly. Later readiness must use the
approved held-source-context/prevalidated-anchor path; do not invoke that path
until the pending replacement is approved and bound.

### Approval-locked resume point

Before binding the CP2-C replacement, Moksh must review the expanded incident
log at exact commit `c2f3ad9c54ed126cc69526c76adb3134c3bf9545`
and decide whether fresh-process CP2-C/D work may continue under the frozen
protocols or whether a separately reviewed mitigation/holdout is required. A
sufficient continue statement is:

> I, Moksh Trehan, acknowledge the CP2 preauthorization incidents recorded in
> docs/cp2_predata_incident_log.md at commit
> c2f3ad9c54ed126cc69526c76adb3134c3bf9545, including the premature
> MH_01_easy ground-truth access and registry disclosures. I authorize
> fresh-process CP2-C/D work to continue under the frozen protocols, subject to
> separate approval of every still-pending replacement; no exposed value may
> be used to change source, configuration, thresholds, fixtures, tests,
> sequence selection, or evaluation protocol; no exceptions.

This acknowledgment is a user evidence-admissibility decision; a new unit
anchor cannot substitute for it. If Moksh does not authorize that disposition,
stop before approval binding and record the selected mitigation in a separately
reviewed contract.

For the CP2-C replacement itself, Moksh must also review exact commit
`0d71fee98499a10df4e92176709c1afe14077f90` and, if accepted, provide a
statement with the exact commit, document, complete replacement, and no
exceptions. A sufficient statement is:

> I, Moksh Trehan, approve the complete CP2-C detached-readiness and
> evidence-binding replacement in
> docs/cp2_c_detached_readiness_binding_clarification_proposed.md as reviewed
> at commit 0d71fee98499a10df4e92176709c1afe14077f90; no exceptions.

Only after both statements, create a **separate** approval-binding commit that
records and source-binds the incident disposition, contains
`project/cp2_c_detached_readiness_binding_approval.json`, and adds the exact
contract/gate bindings. Every tracked-tree change after `0d71fee...`, including
`c2f3ad9...`, the pickup commit, and the binding commit, invalidates the old
anchor for readiness; rerun the complete unit gate at the new exact clean
commit. Only if the nine-step readiness barrier then passes may the registry be
opened once and CP2-C run. CP2-D additionally waits for a reviewed
implementation that freezes the numerical/evaluator capsules and known
answers, followed by explicit approval of that exact implementation/proposal;
CP2-E waits for a separately frozen fixed-clock profile and artifact schema.

## Overnight CP2-C/D/E pickup contract

This section is the handoff for a later explicit overnight work request. It is
a plan, not authorization to open recorded inputs now. The technical order is
strictly serialized:

1. CP2-C3 readiness;
2. CP2-C recorded updater parity;
3. CP2-D sequence/trajectory parity; and
4. CP2-E fixed-clock desktop timing.

Do not start C, D, and E in parallel. CP2-C3 is a hard prerequisite for all
recorded input. CP2-C is a hard prerequisite for crediting D or E. A failure at
one stage stops later stages. A pre-data readiness failure leaves no result
directory; only the separately permitted external diagnostic/status may be
retained. A post-access failure retains its immutable failed staging evidence.
Neither kind of failure triggers threshold changes, baseline changes,
dataset-driven tuning, or a silent retry with altered source/configuration.

Every actual CP2-C, CP2-D, and CP2-E runner invocation must execute the full
nine-step readiness barrier again. Its supplied unit artifact must test the
exact current clean commit and tree. Any tracked source, runner, schema,
profile, configuration, or checkpoint-metadata commit invalidates the prior
unit anchor for later runner invocations and requires a new complete unit gate
and external manifest anchor. Defer post-run checkpoint-metadata commits while
C and D share one runtime/unit identity; otherwise rerun the full unit gate
after each such commit.

### Stage 0 — immutable preflight

- Require branch `schurvio-lite/cp2-one-pass`, a clean worktree including
  untracked files, and ancestry from runtime-tested commit
  `0d71fee98499a10df4e92176709c1afe14077f90`.
- Re-read the nine contract/source-of-truth files listed above. At the current
  HEAD both proposed clarifications are non-authorizing; treat each as such
  unless and until its exact approval and separate binding commit are verified.
  Treat frozen counter units, operation order, CLI, artifact schema, and
  failure taxonomy as locked.
- Confirm the retained unit artifact exists and its `SHA256SUMS` hashes to
  `b2cd771dc014da423a0fb57d2e9bc44a1da65b4bccd40b2fa9a7a01fe585bdd1`.
- Confirm that the CP2-C detached-readiness approval names exact reviewed
  commit `0d71fee...`, the exact proposal path, the complete replacement, and
  no exceptions; then require the separate approval-binding commit before any
  readiness or actual-mode invocation.
- Confirm Moksh has explicitly acknowledged incident-log commit `c2f3ad9...`
  and authorized the chosen evidence disposition. A clean unit anchor does not
  replace that human decision.
- Do not treat the post-anchor pickup commit or later approval-binding commit
  as tested by the `0d71fee...` artifact. Every later tracked-tree change
  requires a new complete exact-commit unit gate.
- Do not semantically parse `project/datasets.yaml`, resolve a bag, import
  `rosbag`, inspect ground truth, or open payload bytes during preflight.
- If the tree is dirty, ancestry is wrong, an anchor mismatches, an approval is
  incomplete, or a contract is internally inconsistent, stop before
  implementation or data access.

### Stage 1 — activate and prove CP2-C3 after approval

The implementation work formerly listed here is present at `0d71fee...` and
has passed its artifact-free self-tests, protecting tests, math review, and
fresh unit gate. It remains deliberately blocked in actual mode.

- In one post-approval binding commit, record and source-bind Moksh's incident
  disposition and exact replacement approval, add every frozen approval/source
  identity to the source context, permitted archives, provenance, manifest,
  and detached verifier, and remove only the two deliberate actual-mode blocks.
  Do not edit the reviewed proposal.
- Run all five exclusive artifact-free `--self-test` modes again under
  `CP2_FORBID_BAG_ACCESS=1`, fresh private roots, process-group timeouts, exact
  case inventories, zero provider calls, and byte-identical protected trees.
- Run a fresh complete unit gate at the exact new clean commit and retain its
  external manifest anchor.
- Execute the full nine-step readiness barrier. It must bind the fresh unit
  artifact, held source context, approval records, lock identity, Git children,
  and command zero before returning a registry capability.
- Only a completely passing readiness barrier at that exact commit/tree and
  unit-artifact identity may authorize semantic registry access. CP2-C3 itself
  produces no dataset-derived artifact and does not pass CP2-C.

The prior version of this section described implementation tasks. Those tasks
are now review-complete; approval binding and formal readiness are the remaining
hard gate.

### Stage 2 — CP2-C recorded updater parity

- After Stage 1 passes, execute the frozen serial campaign on `MH_01_easy`,
  `MH_03_medium`, and `V1_01_easy` to completion using only the committed
  offsets, hashes, configuration, launch file, and exact CLI. Keep the live
  estimator in `nullspace` mode and the orthogonal shadow comparison enabled.
- Require at least 1,000 counted visual updates, at least 99.9% unweighted gate
  agreement, and at least 99.9% raw-row-weighted gate agreement using the
  approved exact-integer cross products.
- Require complete one-to-one raw/proposal/state/feature/block/update/source/
  command/readiness joins, exact baseline commit-oracle agreement, zero
  candidate live writes, complete terminal populations, finite approved
  comparisons, and passing detached offline replay.
- For every shared raw system in every invocation where both reductions are
  mode-valid, including noncounted updates, require lambda/eta/gamma diagnostics
  to be available, finite, and to satisfy `1e-10 + 1e-8*||reference||`.
  Unavailable required diagnostics are failures. Every required
  proposal/state/covariance block comparison must satisfy
  `1e-8 + 1e-6*||reference||`, using the frozen evaluation order and without
  repair or fallback.
- Any baseline-counted update missing the candidate proposal is a hard failure.
  Retain the required unavailable state/covariance block rows and their exact
  expected/seen units rather than dropping them from the population.
- Any unexplained gate/rank/accepted-set difference, incomplete trace,
  nonfinite required quantity, pointer/provenance disconnect, sink failure,
  counter overflow, silent repair, or fallback fails CP2-C.
- Finalize without overwrite, retain an external `SHA256SUMS` anchor, and run
  the detached verifier before marking CP2-C passed. Do not change source or
  configuration after seeing campaign results; a necessary code change creates
  a new commit and restarts readiness and CP2-C from the beginning.

### Stage 3 — CP2-D sequence and trajectory parity

- Before any actual-mode execution, complete a data-free implementation that
  freezes the private evo 1.31.1 evaluator capsule, direct-math Python/NumPy/
  BLAS/LAPACK closure, exact inventories, environments, and binary64 known
  answers required by the pending precision clarification. Commit and review
  those exact bytes, then obtain Moksh's explicit exact-commit approval. Do not
  derive capsule identities from mutable host state during a recorded run.
- Start only after CP2-C passes. Run independent `nullspace` and `schur` modes
  on all three frozen sequences with shadow disabled and identical inputs,
  calibration, initialization, offsets, and frontend configuration.
- Require both `time_coverage` and `processing_fraction` to be at least `0.995`
  independently for each mode on every run, with positive pair counts and
  strictly positive selected/processed durations.
- Require the two resolved parameter maps to be byte-identical after deleting
  exactly `/cp2_vio/up_msckf_landmark_elimination`,
  `/cp2_vio/filepath_est`, `/cp2_vio/filepath_std`,
  `/cp2_vio/record_timing_filepath`, `/cp2_vio/cp2_trace_directory`, and
  `/cp2_vio/cp2_context_path`, and no other key. Missing an allowlisted key or
  finding any other difference fails CP2-D.
- Use the baseline's shared timestamps to compute one SE(3) alignment and apply
  that same transform to both trajectories for direct mode-to-mode parity;
  independently aligned mode-to-mode comparisons are forbidden.
- Require a shared population of at least three poses, a complete finite and
  nondegenerate baseline position spectrum at the frozen rank boundary, a
  proper finite alignment, and a finite strictly positive baseline ATE before
  evaluating the relative ATE ratio.
- Require position difference at most `0.01 m` at p95, orientation difference
  at most `0.05 deg` at p95, and relative ATE difference at most `0.01` on each
  sequence, with every sample/population count retained.
- Finalize and independently verify one immutable artifact per sequence and the
  complete three-sequence set before marking CP2-D passed.

### Stage 4 — CP2-E fixed-clock desktop timing

CP2-E is currently `blocked_pending_fixed_clock_profile`; the observed
host governor is `ondemand`. Do not treat ordinary desktop timing as official
evidence. The existing CP2-E section is a non-authorizing preregistration, not
a complete machine/artifact schema.

- Before actual mode exists, separately commit both
  `project/cp2_timing_profile.yaml` and a replacement of the draft CP2-E
  preregistration with exact schemas for the profile, report, runs, pair
  results, clock snapshots, common payloads, and every artifact file. The
  profile must freeze sequence/offset/input hash, CPU IDs, affinity, governor,
  driver, min/max/current frequencies, boost/turbo state, thermal policy, and
  sampling/preflight commands.
- The runner validates but never changes clock, governor, boost, affinity, or
  thermal controls. Do not silently choose or mutate host-wide settings. If the
  frozen profile cannot be established with available authority, stop with
  CP2-E blocked while retaining completed C/D evidence.
- Because the timing-profile/schema commit changes `HEAD` and the tree, run a
  new complete unit gate at that exact clean commit, retain its external
  manifest anchor, and require the full nine-step readiness barrier to verify
  that new unit artifact before the CP2-E runner may resolve or open a bag.
- Once frozen, use shadow-disabled builds and exactly three ordered pairs/six
  runs in the order `[nullspace_schur, schur_nullspace, nullspace_schur]`, the
  declared 60-second post-offset warm-up, and only the intersection of
  post-warm-up committing camera timestamps.
- Use integer nanoseconds from `std::chrono::steady_clock`, starting at the
  first operation in `UpdaterMSCKF::update` and ending immediately after the
  sole `StateHelper::EKFUpdate` returns. The primary population is only
  nonempty MSCKF invocations that reach and commit the global update; compute
  quantiles with the frozen `numpy_linear` method.
- Require every pair and the median-of-three summary to satisfy candidate
  median-time ratio `<= 1.10` and p95-time ratio `<= 1.15`. Record sample counts,
  clocks, affinity, frequency/governor, temperature, commands, and failures.
- Finalize without overwrite and independently verify the timing artifact
  before marking CP2-E passed. Desktop CP2-E is not Jetson evidence.

### Failure and morning handoff rules

- A readiness-barrier failure stops before data access and leaves no result
  directory. Retain only diagnostics/status explicitly permitted outside the
  result tree. Once actual-mode staging begins after readiness, preserve every
  failed partial/staging directory and its primary validation error. Never
  overwrite or relabel a failed artifact as passed.
- Keep the first fatal reason, exact source commit/tree, command, environment,
  external digest, test/run counts, and the last successfully completed stage.
- Leave the worktree clean at a named checkpoint commit whenever a stage is
  genuinely complete. Do not commit a pass record for a failed or incomplete
  stage.
- The morning report must state separately: CP2-C3 readiness, CP2-C, CP2-D,
  CP2-E, source commit/tree, artifact paths and external anchors, verifier
  results, any retained failure, whether any recorded input was accessed, and
  whether the fixed-clock profile was available.
- Never claim final one-pass parity unless CP2-C, CP2-D, and CP2-E all pass.
  Never claim AArch64/Jetson, accuracy improvement, speedup, energy benefit, or
  deadline guarantees from these desktop gates.
- If CP2-C, D, or E fails, keep `nullspace` as the default, block CP3/two-pass
  work, and record branch rollback as a user decision. Do not silently revert,
  reset, or continue two-pass implementation overnight.

## What exists now

The branch already contains a real estimator modification and an unusually strict verification layer:

- A selectable `nullspace` or `schur` transient-landmark elimination path.
- A production square-root Schur-equivalent reducer that whitens explicitly, rejects rank-deficient or ill-conditioned landmarks, uses Householder QR, and never applies jitter, clamping, regularization, or silent fallback.
- A shared feature-gate implementation driven from one immutable prior.
- A read-only full-update preflight that computes the proposed state increment and posterior covariance before any live EKF write and suppresses invalid commits.
- A shadow path that runs the baseline and candidate from the same raw feature systems without candidate writes.
- Canonical binary64/SHA-256 encoding for raw systems and proposals, plus a value-only trace codec, replay kernel, artifact-bound detached replay, and exact recorded-artifact joins.
- An owning four-phase composite-state snapshot, exact pointer-graph token, canonical state-file codec, detached production-type commit oracle, and prepared nonthrowing/allocation-free phase-3 fill/handoff.
- A production updater transaction that promotes one phase-0 prior before raw assembly, derives all gates and proposals from that prior, prepares detached phase 2 before the final phase-1 proof, performs exactly one baseline EKF commit, then captures the clock endpoint and allocation-free phase 3 in the approved order.
- An authoritative status-returning evidence sink with a sticky fatal latch, exact checked counters, retained gamma provenance, postcommit oracle comparison, and a separately linked full fault-injection runtime.
- Two hundred three CP1/CP2 unit executions across 25 binaries at the current exact-commit anchor, independently re-executed with the same 203/203 result. Coverage includes Schur equivalence, projection Jacobians, FEJ behavior, clone semantics, rank boundaries, transaction ordering, failure atomicity, production-library/source-inventory provenance, exact counters, trace corruption/replay, exhaustive composite mutation, detached update, recorded assembly, runtime context, serial pairing/trace, and allocation-failure behavior.

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
corruptions. Its artifact is
`results/staging/cp2/unit/cp2_unit_20260802T185623152114466Z-g162ed140cb3f-WtrFa1xv`;
its external `SHA256SUMS` anchor is
`58bc351d513bb093a6e355a808fcf62a7d952e01dee5902594ed74946d76f579`.

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

### CP2-C3 — implementation complete; approval/readiness pending

The five entry points, state/artifact assembler, exact joins, detached replay,
opaque readiness engine, and protecting tests are implemented at
`0d71fee...`. Its retained exact-commit unit gate passes 203/203. Separately,
all five entry-point self-tests passed only as unretained development
diagnostics and must be rerun after approval binding. Actual CP2-C entry remains
hard blocked because
`docs/cp2_c_detached_readiness_binding_clarification_proposed.md` is pending.

Immediate hard gate: exact user approval, a separate approval/source-binding
commit, a new complete unit anchor for that changed tree, and one passing
nine-step readiness execution. Readiness must finish without a CP2-C claim or
dataset-derived artifact; only then may CP2-C recorded execution begin.

### CP2-C — recorded updater parity

Run the frozen serial campaign on `MH_01_easy`, `MH_03_medium`, and `V1_01_easy` to completion. Require at least 1,000 counted visual updates, at least 99.9% unweighted and row-weighted gate agreement, complete proposal/state evidence, zero candidate writes, exact baseline commit-oracle agreement, and every predeclared tolerance/counter invariant.

Any unexplained mismatch, nonfinite value, incomplete trace, silent repair, or evidence disconnect fails CP2-C. Do not tune the baseline after seeing candidate results.

### CP2-D and CP2-E — trajectory parity and desktop timing

- CP2-D: the data-free transport/parser/codec review candidate is committed at
  `d53139a...`, but actual mode remains blocked. A target and named real
  evaluator/direct-math inputs must be approved, frozen, reviewed, and bound
  before independent nullspace/Schur sequence execution.
- CP2-E: the exact rational timing-math review candidate is committed at
  `66f4eaf...`, but no real fixed-clock profile or authorizing artifact schema
  exists. The desktop-versus-Jetson disposition and exact host controls remain
  user decisions. If eventually authorized, every paired p50 ratio must pass
  exact `11/10` and every p95 ratio exact `23/20`.

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
git status --porcelain=v1 --untracked-files=all
git rev-parse HEAD
git log -5 --oneline --decorate
git merge-base --is-ancestor 0d71fee98499a10df4e92176709c1afe14077f90 HEAD
git merge-base --is-ancestor 66f4eaf56bb42258214c25fe078e78b3cc0eeb15 HEAD
sha256sum results/staging/cp2/unit/cp2_unit_20260803T090606610655669Z-g0d71fee98499-pry4lam2/SHA256SUMS
```

Require a clean HEAD that descends from the tested CP2-C3 review-candidate
runtime commit
`0d71fee98499a10df4e92176709c1afe14077f90`. A later pickup-metadata commit is
expected; do not confuse it with the runtime-tested source commit. Verify the
previous retained artifact path and external anchor above, then locate and
verify the newer exact-HEAD unit artifact reported by the final handoff. Verify
Moksh's exact incident-log acknowledgment/evidence disposition and exact
replacement approval. Make the source-bound incident disposition, approval record, all
required source/contract identities, and removal of the two deliberate
actual-mode blocks one separately reviewed binding commit; do not invoke
readiness or actual mode until that commit is clean and its fresh unit gate
passes. If ancestry fails or the worktree has
unexplained changes, inspect and reconcile them before continuing. Do not
reset or overwrite unexplained work, and do not inspect recorded inputs during
diagnosis.
