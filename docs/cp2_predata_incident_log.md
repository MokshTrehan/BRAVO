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
