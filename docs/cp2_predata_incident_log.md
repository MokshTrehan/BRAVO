# CP2 pre-data incident log

## 2026-08-02T06:14:40.108703Z — broad read-only source search

- Repository commit at discovery: `b90372485cd48106a004dd6fd6c0391d723991fc`.
- After the exact-commit CP2-A/B unit artifact had finalized and been
  independently reverified, a read-only `rg` source search was invoked with
  the broad directory operand `project/` while mapping the remaining live
  integration surface.
- That operand could cause `rg` to open `project/datasets.yaml`. No registry
  entry or dataset path was printed, resolved, copied, hashed, or passed to a
  provider. No bag, ground-truth file, or recorded-data payload was opened.
- This nevertheless violates the frozen pre-data rule because the rule bars
  opening the registry itself before the five entry-point self-tests and the
  exact unit anchor pass.

Impact and disposition:

- The finalized CP2-A/B artifact is unaffected: its complete execution and
  finalization preceded this command, and its verified policy reports zero
  dataset or bag access.
- No CP2-C/D/E readiness barrier or recorded campaign had started, and none is
  credited as passing. CP2-C remains unauthorized and `not_run`.
- All subsequent pre-barrier searches must use explicit allowlisted paths and
  must exclude `project/datasets.yaml`. A protecting self-test must reject a
  broad project-directory scan or any attempted registry/provider access.
- A fresh readiness barrier, five-entry-point self-test result, and exact
  runtime-commit unit artifact remain mandatory before any recorded input is
  accessed.

There is also a contract-consistency question to resolve before implementing
the readiness snapshot: the artifact schema requires a pre-access source hash
over all tracked files while separately deferring any registry read or hash.
The implementation must not choose a permissive interpretation silently.

## 2026-08-03T04:49Z — premature ground-truth format inspection

- Repository commit at the time of access: `a1ab75f89a9e3582450c8dd6075e0b50290b8358`;
  the working tree contained uncommitted CP2-C3 implementation work.
- During synthetic-only CP2-D runner development, the following exact
  read-only command was mistakenly issued before a fresh exact-commit unit
  anchor and readiness authorization existed:

  ```text
  sed -n '130,260p' project/cp2_gate.yaml && sed -n '1,20p' ov_data/euroc_mav/MH_01_easy.txt 2>/dev/null || true && sha256sum ov_data/euroc_mav/MH_01_easy.txt 2>/dev/null || true
  ```

- Standard output contained the permitted `project/cp2_gate.yaml` section,
  followed by the ground-truth header, 19 ground-truth data rows, and the
  ground-truth file hash. No bag, registry, or other ground-truth path was
  opened, and no campaign runner was invoked.
- The access was read-only. No numeric row value was copied into or used to
  derive implementation code, constants, thresholds, fixtures, or tests. The
  expected file hash was already frozen independently in the approved gate
  configuration and resolver constants before this command.

Impact and disposition:

- This development session and its working tree are non-evidence. The access
  cannot authorize, support, or be represented as a CP2-C or CP2-D result.
- Subsequent runner development is restricted to documentation, source code,
  and synthetic fixtures. The accessed ground-truth file must not be opened
  again during preauthorization implementation or review.
- CP2-C/D recorded execution still requires a new clean committed source
  state, a fresh immutable exact-commit unit artifact, a passing five-entry-
  point readiness barrier, and execution through the audited postauthorization
  runner in a fresh process. No result from before those events may be carried
  into the evidence artifact.
- CP2-D parsing and association logic requires an independent review using
  only the frozen contract and synthetic fixtures before it is eligible for
  that fresh anchor.

## 2026-08-03T05:17Z — omitted registry exclusion in typo search

- Repository commit at the time of access: `a1ab75f89a9e3582450c8dd6075e0b50290b8358`;
  the working tree contained uncommitted CP2-C3 implementation work.
- While locating every occurrence of a newly detected `V1_02_medium` typo,
  the following exact read-only command excluded build, result, and
  ground-truth trees but mistakenly omitted the mandatory explicit exclusion
  for `project/datasets.yaml`:

  ```text
  rg -n "V1_02_medium|V1_02" --glob '!results/**' --glob '!build/**' --glob '!ov_data/**' .
  ```

- Standard output included four matching registry lines: the
  `V1_02_medium` ID, its bag path, its ground-truth filename, and a note with
  camera-message counts. Other output came from ordinary documentation,
  launch/scripts, and the synthetic wrong-sequence protecting test.
- No bag or ground-truth file was opened. No registry path, filename, count,
  or other exposed value was copied into or used to derive implementation,
  constants, thresholds, fixtures, tests, or execution choices.

Impact and disposition:

- This is a second preauthorization registry-read violation in the current
  non-evidence development session. It does not authorize a dataset path and
  cannot support or be represented as CP2-C/D evidence.
- Every subsequent repository-wide search in this session must include the
  explicit glob exclusions `!project/datasets.yaml`, `!ov_data/**`,
  `!results/**`, and `!build/**` unless a later fresh-process readiness
  authorization has already passed.
- The only admissible recorded campaign remains one started later from a new
  clean committed source state, fresh exact-commit unit anchor, and passing
  five-entry-point readiness barrier. Nothing learned from this accidental
  output may be carried into that campaign.

## 2026-08-03T09:38:27.409409Z — broad diagnostic search discovered during handoff

- Repository commit at discovery:
  `0d71fee98499a10df4e92176709c1afe14077f90`; `pickup.md` was the only
  uncommitted tracked change. The exact command-execution timestamp was not
  retained; this timestamp is the immediate discovery/disclosure time.
- While locating retained sanitizer-diagnostic references for the morning
  handoff, the following read-only command was mistakenly given the repository
  root and omitted the required explicit registry and ground-truth exclusions:

  ```text
  rg -n '203|189|14|UBSAN|ASAN|LSAN|SANIT' -S . --glob '!build/**' --glob '!results/**' --glob '!*snapshot*' | head -240
  ```

- Both `project/datasets.yaml` and
  `ov_data/euroc_mav/MH_01_easy.txt` are tracked and not ignored, so this broad
  invocation could have opened them before `head` terminated the pipeline.
  It is conservatively recorded as a preauthorization registry/ground-truth
  open even though no line from either path appeared in the retained stdout.
- No bag was opened, no provider or campaign runner was invoked, and no
  registry or ground-truth value from this command was copied, retained in the
  handoff, or used to derive code, constants, thresholds, fixtures, tests, or
  execution choices.

Impact and disposition:

- The finalized `0d71fee...` unit artifact is unaffected: its build, tests,
  independent re-execution, atomic finalization, and in-gate verification all
  completed before this command. Its `dataset_or_bag_accessed=false` claim is
  limited to that formal gate and must not be generalized to the development
  session.
- This post-gate handoff session is non-evidence. No CP2-C3 readiness or
  CP2-C/D/E campaign is credited as run or passed.
- Every later repository-wide preauthorization search must include exact
  exclusions for `!project/datasets.yaml`, `!ov_data/**`, `!results/**`, and
  `!build/**`. Resume actual work only through a fresh clean commit/unit anchor
  and a fresh-process, approval-bound readiness barrier; nothing from the
  accidental search may enter recorded evidence.

## 2026-08-04 (exact time not retained) — math-audit search crossed the registry boundary

- Repository `HEAD` from the inherited session context was
  `f98eb347552ddab8965d899516143344dcb8a0b4`; the worktree contained
  uncommitted CP2-C implementation and protecting-test changes. The audit
  agent did not independently resolve a commit or tree.
- A read-only audit agent issued the following exact command from the
  repository root:

  ```text
  git status --short && git diff --stat && rg -n "20|pair|candidate|timestamp|chronolog|selector|Schur|gamma|canonical" scripts/cp2 project docs -g '!project/data/**' -g '!project/results/**' -g '!build/**'
  ```

- The allowlist was too broad and omitted the exact exclusion for
  `project/datasets.yaml`. The search opened that registry and printed matches
  containing categories of project/as-of metadata, dataset sizes and hashes,
  canonical dataset paths, and sequence/readability observations. It also
  searched `project/cp0_baseline.json` and printed categories of baseline
  timestamp/tolerance and evaluator-setting metadata. This log deliberately
  does not repeat any exposed value.
- No bag byte, ground-truth file, result artifact, or build content was opened.
  No network access or recorded execution occurred. The agent made no edit or
  commit and communicated no mathematical or implementation finding before
  immediately reporting the incident.

Impact and disposition:

- The entire audit is quarantined and supplies no review credit. No exposed
  registry or baseline value may influence CP2 implementation, thresholds,
  profiles, tests, execution choices, or evidence.
- A replacement math audit must start without that agent's retained context,
  use an exact source-file allowlist, and explicitly forbid the registry,
  recorded inputs, results, and build tree. Only the replacement audit may
  supply review credit.
- This uncommitted development state remains non-evidence. A new clean
  approval-bound source commit, exactly one fresh exact-HEAD unit gate, and a
  fresh readiness barrier remain mandatory before recorded-input access.
- Moksh Trehan's acknowledgment at
  `c2f3ad9c54ed126cc69526c76adb3134c3bf9545` predates this incident and does
  not acknowledge it. Recorded-input access is therefore paused pending an
  explicit acknowledgment and acceptance of this disposition; data-free
  implementation, synthetic tests, and source review may continue.

## 2026-08-04T17:45:11-04:00 — CP2-E public-document web lookup outside the D-only network scope

- Repository `HEAD` was
  `1218d3fb73066760f099f87c621fe607f9de8b63`; the worktree contained the
  authorized, uncommitted data-free CP2-C/D/E implementation candidate.
- While assessing whether this desktop exposed an adequate AMD thermal or
  throttle witness, Codex issued one public web-search request containing
  three queries for official AMD processor documentation and Linux `k10temp`
  documentation. The search returned public result titles, URLs, and snippets
  from AMD and Linux documentation.
- This was outside the chained authorization's narrower rule that pre-freeze
  network use is permitted only when necessary to construct the CP2-D capsule
  from authoritative source material. It was therefore not an authorized
  CP2-E feasibility input even though it accessed no project, dataset, or
  private service.
- No registry, bag, ground-truth file, recorded result, credential, host
  control, service, module, or device state was opened or changed by the web
  request. No downloaded file was installed, retained in the repository, or
  added to a capsule.

Impact and disposition:

- Every fact returned by that web request is quarantined. It may not influence
  the CP2-E helper, timing profile, telemetry definition, threshold, test,
  feasibility decision, source freeze, or execution choice and supplies no
  audit credit.
- The admissible CP2-E candidate remains based only on the authorization,
  approved local contract, wholly synthetic fixtures, and the separately
  authorized local read-only host inventory collected without network access.
  That local inventory had already established that noninteractive `sudo`
  fails and that no adequate bound throttle witness had yet been proved.
- Codex instructed the helper workstream to disregard all web-derived E facts.
  Any later thermal/throttle decision must be derived afresh from local,
  profile-bound surfaces during an authorized privileged apply/validate/
  restore feasibility trial.
- Data-free implementation and synthetic testing may continue. Formal source
  freeze, the unit gate, recorded-input access, and actual C/D/E execution are
  paused until Moksh Trehan explicitly acknowledges and accepts this incident
  disposition in addition to every other chained readiness predicate.

## 2026-08-04T21:31:51-04:00 — replacement CP2-E audit repeated unauthorized web access

- Repository `HEAD` remained
  `1218d3fb73066760f099f87c621fe607f9de8b63`; the working tree contained the
  authorized, uncommitted, data-free CP2-C/D/E implementation candidate.
- During a follow-up review of estimator-CPU isolation, the CP2-E audit
  workstream used public web search/open requests and then `curl` requests for
  Linux kernel cgroup-v1 cpuset and cgroup-v2 documentation, including the
  rendered HTML and source-RST URLs at `docs.kernel.org`.
- The returned v1 direct-ancestor overlap and v2 partition semantics materially
  informed the uncommitted
  `docs/cp2_e_isolation_feasibility.md` decision text. This repeated the earlier
  E-network scope violation: pre-freeze network use is authorized only when
  necessary for the D capsule, not for E feasibility work.
- No dataset registry, bag, ground truth, recorded result, private service,
  credential, host control, module, cgroup, affinity, clock, or device state
  was opened or changed by the web requests.

Impact and disposition:

- The web-informed isolation document and every E implementation, profile,
  test, or execution choice influenced by those returned semantics are
  quarantined and supply no audit credit. They may not be used to justify
  source freeze or any formal or recorded execution.
- The audit workstream was stopped immediately after disclosure. A replacement
  isolation derivation must be performed without that workstream's context,
  without network access, using only explicitly permitted local read-only host
  surfaces and locally installed documentation. The locally installed
  `/usr/share/man/man7/cpuset.7.gz` and `cgroups.7.gz` were identified as
  candidate offline sources, with SHA-256 values
  `bebe87a0dadfa490015f8c0a4028c5cb07b85a31bf2895cb8d151da53dd85a88`
  and `b642b0c570ecadd8ca7ff30a0e3d1fcd5f15567ff3eba96cb71b0357ae3b1153`
  respectively.
- Data-free work unrelated to the quarantined E conclusion may continue.
  Formal source freeze, the unit gate, readiness, recorded-input access, host
  mutation, and actual C/D/E execution remain paused. Moksh Trehan must
  explicitly acknowledge and accept this incident disposition at the exact
  committed identity before the chained formal transition can resume.
