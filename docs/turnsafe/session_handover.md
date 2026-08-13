# TurnSafe v6 Session 0.5 handover

Status: **PASS at the Session 0.5 hard stop**

KAIST compatibility: **`RUNNABLE_WITH_DECLARED_ADAPTER`**

## Identity and authorization

| Field | Value |
|---|---|
| Entry token | `APPROVE_SESSION_0_5_KAIST_DOWNLOAD_AND_ADAPTER` |
| Authoritative cwd | `/home/moksh/newSlam turnsafe-primary` |
| Branch | `schurvio-lite/turnsafe-primary` |
| Start SHA | `fdf34496e4f5212c79ab65d6210eed11e1437dd1` |
| Start tree | `fcedd2a302142f66a367922bcf9ab7c16171ffe1` |
| Implementation commit | `1da493c44f7197b61bbc2cdf66c929bde2af9152` |
| Clean evidence source commit | `6e289fc2d1e8c86f0930605fc6b234df3cea1e84` |
| Clean evidence source tree | `40d59b7c4a6341dcc7ae1565f386cbbeb607db39` |
| Terminal metadata commit/tree | This document's containing commit; exact values and clean-tree proof are in `artifacts/turnsafe/session_00_5/GIT_END.txt` |
| Required next token | `APPROVE_SESSION_1_T0_INSTRUMENTATION_KAIST_READY` |

The entry gate passed on the exact requested branch/SHA with a clean tracked
tree and index, 963 GiB free, all required tools present, and the authorized
read-only Ceres prefix available. `GIT_START.txt` preserves the complete entry
capture. The final documentation commit changes only this handover; all replay
inputs and binaries are hash-bound to the clean evidence source commit above.

The narrow Session 0.5 network authorization was used only for the official
KAIST archive and metadata. No mirror fallback, package installation, sudo, or
push occurred. Dataset bytes remain outside Git below
`/home/moksh/datasets/KAIST_VIO`.

## Outcome

- Official metadata is pinned at
  `ae672591bab119651be1aa9fc3db5af3e599f49a`.
- The primary 51,252,383,249-byte archive completed from the official KAIST
  server. Captured SHA-256:
  `1f3fe695b773342a7a1df227e651a7566802ea2e7a8d5ef1af49f0c112c09e1f`.
  No upstream digest was published, so this is a captured local identity.
- The central-directory audit accepted exactly 21 safe members and eleven
  expected bags; `unzip -t` passed before extraction.
- All eleven source bags are readable and fully audited for topics/types,
  counts, rates, record/header timestamps, finite sensor fields, exact-header
  stereo pairing, and source ground truth.
- Direct bag evidence shows raw `sensor_msgs/Image` infrared streams, contrary
  to README compressed-topic prose. The contract admits an exact `official_raw`
  profile and separately detected documented compressed JPEG/PNG fallback.
- The adapter preserves every retained raw camera/IMU message, timestamp,
  frame ID, pixel/sample payload, count, and order. Ground truth is required and
  audited at source but omitted from estimator input.
- Legacy record-time stereo pairing cannot realize every exact target-time
  pair. The dedicated exact-header serial seam is default false, enabled only
  in the fixed KAIST launch, CP2-exclusive, synchronous, and runtime-GT-free.
  It selects existing messages without retiming and fails unless every queued
  pair is processed and the terminal queue is empty.
- The final source-bound campaign completed all eleven sequences in the frozen
  order. Each manifest is `COMPLETED`, all 13 checks pass, and there are zero
  resets, nonfinite detections, generic unpaired/drop warnings, camera decode
  failures, timeouts, surviving process groups, or pending pairs.
- `rotation_fast` and all three head trajectories satisfy the passing gate.
  Translation APE plus 1 m translation/rotation RPE completed for every
  sequence. Yaw and tilt are explicitly `NOT_AVAILABLE` because no Session 0.5
  deterministic derivation is defined.
- Final MH_01 replay produced 2,767 rows. State, deviation, and trajectory are
  byte-identical to Session 0, and expected/observed stable digest is exactly
  `8525ee08a3e7b00261bcd5089cefd6b51c279747ecb26bed9505edc5e890010a`.

Production estimator mathematics changed: **no**.

No T0 instrumentation, TurnSafe residual, certificate, factor row, typed
outcome, T1/T2/T3 behavior, estimator gate, fallback, frontend policy, feature
lifecycle, calibration optimization, or outcome-driven tuning was introduced.
The exact-header change is a dataset-specific, default-off offline ingestion
seam. MH_01 parity proves the default path remains behavior-neutral.

## Tracked commits

1. `03a4e1044108f20b766acf25ef161a4599b0b242` — contract, fixed KAIST
   configuration/launch, and config tests.
2. `8a29fa3a26c4b5d458227d943e4e3add1335d235` — deterministic adapter and
   tests.
3. `ac9c87bf01e33127e26d4baeb4d9fede79fe62e5` — archive safety auditor and
   tests.
4. `1da493c44f7197b61bbc2cdf66c929bde2af9152` — evidence-driven adapter and
   contract corrections, exact-header serial seam, campaign/report tooling,
   and tests.
5. `6e289fc2d1e8c86f0930605fc6b234df3cea1e84` — clean handover checkpoint
   used as the evidence source identity.
6. The terminal focused documentation commit contains only this corrected
   handover. Its exact SHA/tree is recorded after commit in `GIT_END.txt`.

Generated bags, manifests, logs, metrics, and build state remain untracked
under the authorized dataset root, `artifacts/turnsafe/`, and
`.turnsafe-work/session_00_5/`.

## Final gates

| Gate | Result |
|---|---:|
| Final Release implementation build | PASS |
| Direct CP1 after source freeze | 35/35 PASS |
| Affected registered CP2 after source freeze | 4/4 PASS |
| Full registered CTest after source freeze | 28/28 PASS |
| Adapter/archive/config/campaign/report Python suite | 86/86 PASS |
| Source/adapted bag audit | 11/11 PASS |
| Post-freeze KAIST campaign | 11/11 COMPLETED |
| Runtime exact-header and terminal queue binding | 11/11 exact PASS |
| MH_01 source-bound digest parity | exact PASS |
| Final aggregate source/parity/hash validation | PASS |

The authoritative command ledger is
`artifacts/turnsafe/session_00_5/COMMANDS.log`; detailed results are in
`TEST_RESULTS.md`, and the decisive report is `SESSION_0_5_REPORT.md`.

## Failure and incident ledger

Retained ENGINEERING events include: unsupported curl option; resumable
transfer pauses and one aria2 exit 5; scratch CTest quoting with zero tests
executed; initial compressed-image fixture errors; OpenCV introspection miss;
zero-test archive discovery; launch-node resolution mismatch; initial nested
archive-prefix rejection; README/raw-topic mismatch; legacy record-time pairing
incompatibility; initial ANSI summary parse; and two harmless post-consolidation
CSV convenience-probe `KeyError`s before the successful parser.

The independent final audit also found incomplete evidence binding in the
first implementation: an adapted-bag publication race, no machine-bound
terminal processed/pending proof, self-declared rather than file-bound parity,
and absent common source identity. Each was fixed fail-closed. The earlier
eleven campaign outputs were retained as superseded evidence; all eleven and
MH_01 were rerun with the final binary before classification.

Two CORRECTNESS incidents remain:

- A delegated broad `rg --files` printed pathnames below forbidden
  `scripts/cp2/`. No content there was opened, read, executed, copied, or
  modified, and no edit resulted. Exact output is retained in
  `CORRECTNESS_INCIDENT_SCRIPTS_CP2_PATH_LISTING.txt`.
- The first three focused commits preceded the required MH_01/KAIST replay
  gates. They were not amended or rewritten. All final gates subsequently
  passed before the source and terminal documentation commits.

No SCIENTIFIC failure occurred. No private manifest, raw holdout bag, or
holdout-marked acquisition metadata was observed or used. Neither incident
changed dataset bytes, a test threshold, estimator behavior, or a scientific
decision.

## Key artifact identities

| Artifact | SHA-256 |
|---|---|
| `data/KAIST_DOWNLOAD_MANIFEST.json` | `17502175a17cf6c9fc8c2635dbd41925348632cdfc03d2ef572e5bb25ae72d64` |
| `data/KAIST_BAG_INVENTORY.csv` | `33b91081885d1d0e849bd3b8517cd5bfe2ccf073a8a0d7e2a01a8a89a4869a51` |
| `data/KAIST_COMPATIBILITY_REPORT.md` | `8b14b0b9889ab059724593e20d8f1f595287fcfb4e88b23353e1a64cabae65e7` |
| `data/KAIST_BASELINE_RESULTS.csv` | `99f94546bfe1d807af04c47df82ff5bd0ed63d8c1eb500e5e10bed3fa95bf8b5` |
| `data/KAIST_SEQUENCE_MANIFEST.json` | `6471971482910be26dcb16fb731b3f899b66e6592944b842e61fd7620c37082a` |
| `session_00_5/mh01_parity_source_final/digest.json` | `1d23b1a5edbf294f7ad7ff29383e94834c43822ab0a198647e4d409be46a0f24` |
| `session_00_5/CP1_DIRECT_SOURCE_FINAL.log` | `be069d696387b060659ef0172d28cb9768a1e56fbf63432c14fd0a9c0d44d99a` |
| `session_00_5/AFFECTED_CP2_CTEST_SOURCE_FINAL.log` | `cf7a1851e09d4c0ec40a71fd0f7232004a60c3ec6b2174288b61d180e5399877` |
| `session_00_5/CTEST_ALL_SOURCE_FINAL.log` | `d6b577b64d47afe36b2bcad6b95d3a9ef9c77eb70a43789674c1a3c882835bfc` |
| `session_00_5/PYTHON_ALL_SOURCE_FINAL.log` | `e32ff6647696e9839ae89f349c6444ba7fc91db45d605c546706dc43338e8b6f` |
| `session_00_5/CORRECTNESS_INCIDENT_SCRIPTS_CP2_PATH_LISTING.txt` | `35954c39a001fb30f703655ec5533bc17a92b003b4e7a9ade13c1a93ebc5ce4c` |

The full generated-evidence checksum inventories are
`artifacts/turnsafe/session_00_5/SHA256SUMS` and
`/home/moksh/datasets/KAIST_VIO/manifests/SHA256SUMS`.

## Next gate

Session 0.5 stops here. Session 1 may begin only after the exact token
`APPROVE_SESSION_1_T0_INSTRUMENTATION_KAIST_READY` is supplied. Its scope is
passive T0 instrumentation; this handover does not authorize factor rows,
decision changes, outcome-driven adapter/configuration changes, or runtime
ground truth.

---

# TurnSafe v6 Session 1 handover

Status: **PARTIAL at the Session 1 hard stop**

Session 1 started from the required clean branch identity
`7b965c3b5f43fe1be72858ad5a9149a21b86481b`, tree
`96ca875a32f63733743512cf25e1e0b94446b64a`, under authorization
`APPROVE_SESSION_1_T0_INSTRUMENTATION_KAIST_READY`. The entry, frozen
Session-0.5 evidence, 11/11 adapter classification, and MH_01 digest
`8525ee08a3e7b00261bcd5089cefd6b51c279747ecb26bed9505edc5e890010a`
were independently verified.

The unchanged start reproduced in a fresh Release build. Direct CP1 passed
35/35, full registered CTest passed 28/28, the affected registered CP2 subset
passed 4/4, and the Session-0.5 Python suite passed 86/86. A candidate passive
implementation was then built successfully; direct CP1 again passed 35/35,
the new T0 unit suite passed 8/8, full registered CTest passed 29/29, and the
extended campaign Python suite passed 19/19.

The candidate attempts default-off, explicit-path JSONL capture; passive KLT,
bounds/mask, and fundamental-matrix counters; initializer/refinement, Schur,
and full-NIS native mirrors; detached value-only terminal attempts; one-prior
primitive capture; and typed fail-closed shadow reasons. It adds no factor row,
proposal statistic, state/covariance write, lifecycle decision, or Session-2
branch logic. Consensus-dependent selection remains unavailable because its
threshold set is not frozen.

No candidate source commit was made. The required precommit replay gate could
not start in this execution environment. The first MH_01 launch failed before
estimator construction when ROS network-interface enumeration returned
`EPERM`. A loopback-only `netifaces` shim removed that enumeration, but the
second launch again failed before estimator construction because ROS could not
create its local XML-RPC socket (`Operation not permitted`). This is classified
ENGINEERING, not CORRECTNESS or SCIENTIFIC. It does not constitute capture-off
parity and cannot be papered over, so source freeze, MH_01 capture-on/off,
minimum KAIST parity, all-eleven capture, aggregation, and PASS classification
were not attempted.

After the hard stop, the required independent read-only diff review found
blocking candidate CORRECTNESS defects, each independently verified by the
primary agent: diagnostic allocations can throw through the live KLT path;
valid-clone and target-stereo funnel counts are eligibility-filtered and use
the wrong aggregation granularity; explicit candidate observation/stereo
evidence and required stage/no-full-update fields are absent; small-group and
supported-configuration values are hardcoded; candidates are not globally
canonical-sorted; and source SHA/tree declarations are not independently
bound to Git/build provenance. These defects were not repaired after the hard
stop. Passing local tests therefore do not establish candidate conformance.

The candidate tracked tree remains intentionally uncommitted and dirty because
the prompt forbids a source commit before replay parity. The index is clean.
No dataset byte was modified, no holdout/private path was accessed, and no
content below `scripts/cp2/` was inspected. Session 2 was not begun.

Authoritative Session-1 evidence is under
`artifacts/turnsafe/session_01/`. Continue only in an environment that permits
an isolated loopback ROS master. First repair the recorded correctness defects
under an authorized continuation and rerun focused gates; then rerun the
precommit replay gates and commit only if all gates pass.

Required next disposition: `SESSION_1_REVIEW_REQUIRED`.

---

# TurnSafe v6 Session 1R repair handover

Authorization: `APPROVE_SESSION_1R_REPAIR_AND_REPLAY`

Session 1R starts from commit
`7b965c3b5f43fe1be72858ad5a9149a21b86481b` and tree
`96ca875a32f63733743512cf25e1e0b94446b64a`, preserving the deliberately
uncommitted Session-1 candidate. The repair is committed only if the precommit
MH_01 and minimum-KAIST parity gates pass. Because a commit cannot embed its
own SHA, the exact resulting commit/tree and clean postcommit replay results
are bound in `artifacts/turnsafe/session_01r/GIT_END.txt`,
`SOURCE_PROVENANCE.md`, `PARITY_REPORT.md`, and `SESSION1R_REPORT.md`.

This continuation closes only the eight Session-1 review findings:

- diagnostic allocation, projection, grouping, serialization, publication,
  and sink failures are contained behind fixed first-reason and saturating
  counters and cannot alter native masks, IDs, rows, decisions, or lifecycle;
- clone-pair populations precede typed eligibility, while target stereo is
  retained and aggregated per candidate, attempt, and group;
- candidates retain exact source, target, and target-time stereo observation
  keys, pixels, normalized values, and camera/calibration identities;
- group cardinalities retain the exact `n<2`, `n=2,3`, and `n>=4` meanings;
- support is recomputed from resolved one-pass Schur, FEJ, GLOBAL_3D,
  CamRadtan, fixed-calibration, stereo, and target-range options;
- initializer, refinement, Schur, NIS, accepted-row, numerical, and causal
  no-full-visual-update fields remain distinct and honest about availability;
- candidates and groups use the global canonical key in `t0_schema.md`; and
- configure-time source identity, cache/build identity, runtime binaries,
  dynamic libraries, schema, configuration, calibration, and source state are
  independently bound and checked before and after capture.

No factor row, threshold, estimator gate, fallback, update proposal, state or
covariance write, feature-lifecycle decision, Session-2 branch, or scientific
tuning is introduced. The full eleven-sequence KAIST campaign is explicitly
not run in Session 1R. The authoritative command/test/replay logs and dataset
before/after hashes remain untracked under
`artifacts/turnsafe/session_01r/`; generated build and replay scratch remains
under `.turnsafe-work/session_01r/`.

If and only if the stop packet classifies Session 1R PASS, the next required
authorization is `APPROVE_SESSION_1C_FROZEN_KAIST_CAMPAIGN`. Otherwise the
required disposition remains `SESSION_1_REVIEW_REQUIRED`.

---

# TurnSafe v6 Session 1S recertification handover

Status: **PASS at the Session 1S hard stop**

## Identity and scope

| Field | Value |
|---|---|
| Authorization | `APPROVE_SESSION_1S_SCHEMA_RECERTIFICATION` |
| Authoritative cwd | `/home/moksh/newSlam turnsafe-primary` |
| Branch | `schurvio-lite/turnsafe-primary` |
| Start/end HEAD | `63a02d04fcc5d39ee4511485bfbd0dcc305c2dbd` |
| Start/end tree | `77f8de20c7ae7317f26399ea2afd979322646511` |
| Entry tracked tree/index | clean |
| Final index | clean; nothing staged |
| Final worktree | intentionally dirty at the four approved tracked paths only |
| Erratum | `turnsafe.t0.v1.erratum1.range_lcb_unavailable` |
| Telemetry schema | unchanged `turnsafe.t0.v1` |
| Next requested token after human commit | `APPROVE_SESSION_2_T0_DECISION_ONLY` |

Session 1S was restricted to the terminal-reason taxonomy erratum, validator
strengthening, and offline recertification of immutable Session-1FC evidence.
No Session-2 branch decision, threshold decision, factor work, T1/T2/T3,
Branch-B, or pivot work was performed.

## Outcome

Section 10 of `docs/turnsafe/t0_schema.md` now includes the distinct stable,
ineligible `RANGE_LCB_UNAVAILABLE` reason between
`RANGE_COVARIANCE_INVALID` and `RANGE_LCB_NONPOSITIVE`, with the advisor-frozen
meaning. The Python campaign/corpus validator has one canonical ordered tuple
and derived membership set. Candidate, consensus, eligible non-winner
foregone, and optional summary terminal reasons pass through one
membership/stage function; nested availability causes remain their distinct
taxonomy. Tests bind the canonical taxonomy exactly to the fenced Section-10
list and reject missing, null, non-string, empty, unknown, and wrong-stage
stable values.

The original Session-1FC checksum ledger passed. All 16 preserved telemetry
archives retained their compressed/raw identities and passed `zstd -t` plus
strict bounded streaming validation. The primary corpus remains exactly
47,145 records and 47,134 callbacks, with 135,328 terminal attempts,
11,823,702 shadow candidates, and 2,641,766 shadow groups. The unavailable-LCB
count remains exactly 1,609,352 with vector:

```text
123572, 225878, 119510, 45472, 321464, 35042,
202600, 94592, 108872, 187910, 144440
```

The six derivative aggregates were regenerated from raw preserved streams
into `artifacts/turnsafe/session_01s/`. Every original JSON field and CSV cell
matches Session 1FC after removal of the explicit Session-1S status/erratum
metadata. Source/build/configuration/calibration/input/parity identities,
sequence order, all scientific counts and denominators, and estimator results
are unchanged. Public KAIST remains 48/48 checksum-valid.

`RANGE_LCB_UNAVAILABLE` being valid taxonomy does not mean a range LCB is
available. All 1,609,352 records remain certificate-unavailable and preserve
their nested unavailable cause. Consensus/rank/winner values that were
`NOT_APPLICABLE` remain so. No T1 engineering gate is passed here.

## Changed tracked paths

```text
docs/turnsafe/t0_schema.md
scripts/turnsafe/kaist_vio_campaign.py
scripts/turnsafe/test_kaist_vio_campaign.py
docs/turnsafe/session_handover.md
```

No C++, CMake, launch, calibration, runtime configuration, campaign ingestion,
aggregation formula, estimator, threshold, binary, raw telemetry, or
Session-1FC artifact changed. Nothing was staged or committed, as required.

## Validation

| Gate | Result |
|---|---:|
| Section-10 list versus canonical tuple/set | PASS, 26 unique reasons |
| Focused affected validator tests | 4/4 PASS |
| Complete allowed Python suite | 109/109 PASS under offline socket-probe adapter |
| Strict JSON/JSONL/CSV, duplicate-key, finite-value validation | PASS |
| Immutable compressed archive identity and `zstd -t` | 16/16 PASS |
| Strict archive streaming and corrected enum enforcement | 16/16 PASS |
| Primary records/callbacks | 47,145 / 47,134 exact PASS |
| Cross-file and callback-funnel reconciliation | PASS |
| `RANGE_LCB_UNAVAILABLE` total/vector | exact PASS |
| Original Session-1FC checksum ledger | 19/19 PASS |
| Public KAIST dataset ledger | 48/48 PASS |
| Derivative equality except recertification metadata | PASS |
| Tracked changed-path boundary and clean index | PASS |
| Session-2 hard stop | PASS |

The ordinary Python-suite command encountered a managed-sandbox AF_INET
`EPERM` in one pre-existing port-probe test before its intended assertion.
This is classified ENGINEERING. A scratch-only in-process adapter replaced
only that forbidden socket probe with its unchanged port-range check; the
complete suite then passed 109/109. No tracked test or assertion was weakened.
No CORRECTNESS or SCIENTIFIC failure remains. No ROS/bag replay or C++ rebuild
was run or required.

Exact commands, tests, artifact identities, and final Git checks are preserved
in `artifacts/turnsafe/session_01s/COMMANDS.log`, `TEST_RESULTS.md`,
`CORPUS_RECERTIFICATION.md`, `SCHEMA_VALIDATION.md`, `COMMIT_MANIFEST.json`,
and `SHA256SUMS`.

## Next gate

Run the human-side recertification commit script using the recorded commit
message `fix(turnsafe): align T0 terminal reason schema`. After that commit,
request the exact token `APPROVE_SESSION_2_T0_DECISION_ONLY`. Session 2 has not
begun in this session.
