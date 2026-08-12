# TurnSafe v6 Session 0.5 handover

Status: **PASS at the Session 0.5 hard stop**

KAIST compatibility classification:
**`RUNNABLE_WITH_DECLARED_ADAPTER`**

Authority: `docs/turnsafe/guidance/IMPLEMENTATION_PLAN_V6.md`, the completed
Session 0 handover, and the Session 0.5 authorization below. Session 0.5 was a
data-readiness and deterministic-ingestion extension only.

## Identity and authorization

| Field | Value |
|---|---|
| Entry token | `APPROVE_SESSION_0_5_KAIST_DOWNLOAD_AND_ADAPTER` |
| Authoritative cwd | `/home/moksh/newSlam turnsafe-primary` |
| Branch | `schurvio-lite/turnsafe-primary` |
| Start SHA | `fdf34496e4f5212c79ab65d6210eed11e1437dd1` |
| Start tree | `fcedd2a302142f66a367922bcf9ab7c16171ffe1` |
| Last completed tracked checkpoint SHA | `ac9c87bf01e33127e26d4baeb4d9fede79fe62e5` |
| Last completed tracked checkpoint tree | `0385ed9398713bc5319f501dad936be4268fd93b` |
| End commit/tree | Pending the final focused Session 0.5 commit containing the reviewed remaining changes and this handover; no final SHA or tree is claimed in advance |
| Required next token | `APPROVE_SESSION_1_T0_INSTRUMENTATION_KAIST_READY` |

The entry gate passed on the exact branch/SHA with a clean tracked worktree and
index, 963 GiB available, the required download/archive/ROS tools present, and
the authorized read-only Ceres prefix usable. The complete capture is
`artifacts/turnsafe/session_00_5/GIT_START.txt`.

The three completed focused checkpoints are:

- `03a4e1044108f20b766acf25ef161a4599b0b242` —
  `docs(turnsafe): freeze KAIST VIO ingestion contract`;
- `8a29fa3a26c4b5d458227d943e4e3add1335d235` —
  `tools(turnsafe): add deterministic KAIST VIO adapter`; and
- `ac9c87bf01e33127e26d4baeb4d9fede79fe62e5` —
  `tools(turnsafe): audit KAIST archive safety`.

Those commits are not described as the final Session 0.5 tree. The remaining
exact-header serial plumbing, evidence harness/report tooling, evidence-driven
adapter/contract corrections, tests, and this handover are intentionally
pending one final reviewed commit. Its actual SHA/tree must be recorded after
creation; prior commits are not amended or rewritten.

## Session outcome

- Official metadata was resolved at
  `ae672591bab119651be1aa9fc3db5af3e599f49a`. The primary official archive
  route completed; individual-bag and Hugging Face fallbacks were not used.
- The captured official archive is 51,252,383,249 bytes with local SHA-256
  `1f3fe695b773342a7a1df227e651a7566802ea2e7a8d5ef1af49f0c112c09e1f`.
  The publisher supplied no upstream checksum, so this is a reproducible local
  identity, not an upstream-authenticated digest.
- A central-directory-only fail-closed audit accepted exactly 21 safe members
  under the consistent `kaistviodataset/data` prefix and exactly the frozen 11
  bags. Full `unzip -t` passed before extraction. All 11 extracted bags were
  readable and received local SHA-256 identities.
- Direct bag evidence overrides the README only for recorded topic/type facts:
  all 11 archive bags contain raw `sensor_msgs/Image` infrared streams on
  `/camera/infra1/image_rect_raw` and `/camera/infra2/image_rect_raw`. The
  adapter freezes this as the primary `official_raw` profile while retaining a
  separately detected, fail-closed `documented_compressed` JPEG/PNG fallback.
- The adapter writes only `/turnsafe/kaist/infra1/image_raw`,
  `/turnsafe/kaist/infra2/image_raw`, and unchanged `/mavros/imu/data`. Raw
  images are retopicked without reconstruction; compressed images are decoded
  deterministically to exact `640 x 480 mono8`. All retained headers, pixels,
  message counts, bag-record times, and retained-stream order are preserved.
- `/pose_transformed` is required and validated only at the source boundary.
  It is absent from every adapted estimator-input bag and from runtime
  parameters. Reference parsing occurs only after estimator completion.
- Exact stereo pairs are the intersection of integer camera header stamps.
  Unmatched camera messages remain in the bag and are audited; record-time
  skew is reported but never used to filter or retime. Every official bag has
  exact pairs and also has pairs whose record-time skew exceeds the legacy
  20 ms first-forward window.
- The dataset-specific `kaist_vio_exact_header_stereo` serial seam is default
  false and enabled only by the dedicated KAIST launch. It selects existing
  exact-header pairs from one immutable filtered view, anchors each callback at
  the earlier record ordinal, and drains the full view so later real IMU
  messages can process the terminal queued camera callback. It does not alter
  pixels, timestamps, estimator mathematics, frontend policy, or the unchanged
  default-false legacy path.
- The frozen ordered campaign completed **11/11** sequences. Each sequence
  manifest is `COMPLETED`, every recorded check is true, estimator and
  evaluation completion are true, and no reset, nonfinite output, timeout,
  surviving process group, generic dropped-message warning, unpaired warning,
  or camera decode failure was recorded. Translation APE plus 1 m translation
  and rotation RPE completed for every sequence.
- The aggregate manifest has no unsupported reasons or engineering blockers
  and derives the exact classification
  **`RUNNABLE_WITH_DECLARED_ADAPTER`**. The adapter remains a declared
  prerequisite; this is not a claim that the source bags are directly runnable
  through the legacy serial path.
- The final post-change MH_01 replay produced 2,767 finite, strictly increasing
  rows. State, deviation, and trajectory outputs compared byte-for-byte with
  the frozen Session 0 run, and the stable digest is exactly
  `8525ee08a3e7b00261bcd5089cefd6b51c279747ecb26bed9505edc5e890010a`.

Production estimator mathematics changed: **no**.

The exact-header change is offline input plumbing, guarded default-off. No
TurnSafe factor, factor row, T0 instrumentation, estimator gate, fallback,
update rule, feature-lifecycle policy, calibration optimization, or
decision-changing diagnostic was introduced.

## Tracked files and commits

Commit `03a4e1044108f20b766acf25ef161a4599b0b242` adds the initial fixed
contract/configuration surface:

- `docs/turnsafe/kaist_vio_adapter_contract.md`;
- `config/kaist_vio_turnsafe_baseline/estimator_config.yaml`;
- `config/kaist_vio_turnsafe_baseline/kalibr_imu_chain.yaml`;
- `config/kaist_vio_turnsafe_baseline/kalibr_imucam_chain.yaml`;
- `project/kaist_vio_serial.launch`; and
- `scripts/turnsafe/test_kaist_vio_config.py`.

Commit `8a29fa3a26c4b5d458227d943e4e3add1335d235` adds:

- `scripts/turnsafe/kaist_vio_adapter.py`; and
- `scripts/turnsafe/test_kaist_vio_adapter.py`.

Commit `ac9c87bf01e33127e26d4baeb4d9fede79fe62e5` adds:

- `scripts/turnsafe/kaist_vio_archive_audit.py`; and
- `scripts/turnsafe/test_kaist_vio_archive_audit.py`.

The pending final focused commit contains only Session 0.5 work: the
evidence-driven dual-profile adapter/contract corrections; the default-off
KAIST exact-header selector and serial integration; the dedicated launch/build
registration and tests; deterministic campaign/report generation; and this
handover. Review its actual staged file list and record its SHA/tree after the
commit. Generated bags, manifests, logs, metrics, and scratch build state remain
untracked under `/home/moksh/datasets/KAIST_VIO/`, `artifacts/turnsafe/`, and
`.turnsafe-work/session_00_5/`.

## Commands and tests

The command/outcome ledger is
`artifacts/turnsafe/session_00_5/COMMANDS.log`. The public consolidated evidence
is under `artifacts/turnsafe/data/`; per-sequence manifests and raw logs remain
under `artifacts/turnsafe/kaist_baseline/` and the authorized dataset root.

Green checkpoints:

| Check | Result |
|---|---|
| Fresh ROS1 Noetic Release configure/build, `/usr/bin/python3`, authorized Ceres, two jobs | PASS |
| Direct CP1 tests | 35/35 cases PASS |
| Affected registered CP2 tests after scratch-wrapper repair | 4/4 executables PASS |
| Final full registered CTest, including KAIST selector | 28/28 executables PASS |
| Final adapter/archive/config/campaign/report Python suite | 65/65 tests PASS |
| Adapter `py_compile`, CLI help, and scoped whitespace checks | PASS |
| Archive central-directory audit and complete decompression test | PASS |
| Source bag readability and topic/type inventory | 11/11 PASS |
| Adaptation semantic preservation and exact-header audit | 11/11 PASS |
| Fixed KAIST estimator/evaluator campaign | 11/11 COMPLETED |
| Runtime exact-pair/unmatched/skew accounting versus adapted audit | 11/11 exact PASS |
| MH_01 frozen digest parity | exact PASS |
| Scoped/final `git diff --check` | PASS at recorded checkpoints; rerun immediately before final commit |

The final Python command covered
`test_kaist_vio_adapter`, `test_kaist_vio_archive_audit`,
`test_kaist_vio_config`, `test_kaist_vio_campaign`, and
`test_kaist_vio_reports`. Expected malformed-PNG decoder diagnostics occurred
only in the negative synthetic test; that test passed.

## Failure ledger

| Classification | Event | Disposition |
|---|---|---|
| ENGINEERING | Installed curl 7.68.0 rejected `--retry-all-errors` before issuing the first archive HEAD request. | Retried with supported bounded options; official URL returned HTTP 200 and the primary download completed. |
| ENGINEERING | Generated scratch CTest quoting repairs initially produced malformed wrappers; the affected run executed 0/4 binaries. | Preserved `AFFECTED_CP2_CTEST.log`; repaired only generated scratch commands; affected CTest passed 4/4 and final CTest passed 28/28. |
| ENGINEERING | Initial synthetic compressed-image tests exposed Noetic CvBridge requesting a BGR-to-mono conversion for an already one-channel PNG: adapter result 2/6, with one error and three expected-reason mismatches. | Replaced that decode path with signature/format-validated OpenCV `IMREAD_UNCHANGED`, required exact 2-D `uint8`, and performed no color conversion; final combined Python suite passed 65/65. |
| ENGINEERING | One OpenCV dependency introspection spelling (`import cv2.cv2`) failed. | Resolved and hashed the installed package-local native module without changing code or data. |
| ENGINEERING | Initial module-style archive-auditor invocation could not import its sibling and executed zero tests. | Added the explicit sibling import root used by the adapter tests; archive suite passed. |
| ENGINEERING | `roslaunch --find-node` exited zero but returned the containing launch path, not the executable. | Replaced the harness lookup with `catkin_find --libexec` before any dataset replay. |
| ENGINEERING | The first archive safety policy rejected the official consistent nested prefix `kaistviodataset/data/`. | Failed before extraction, then generalized only the consistent-prefix rule while retaining all path/type/collision checks; 17/17 auditor tests and the real audit passed. |
| ENGINEERING | Direct bag metadata contradicted README compressed-topic prose; the first `rotation_fast` adaptation failed closed because the compressed source topic was absent. | No output was created. Added uniquely detected `official_raw` primary and documented compressed fallback profiles; all source messages remain preserved. |
| ENGINEERING | Direct all-bag audit proved the legacy record-time `<20 ms` stereo rule cannot realize exact target-time pairs, and the legacy last-camera cutoff can leave a terminal pair undrained. | Added the default-off, KAIST-only exact-header selector with earlier anchoring and full-view drain; dedicated tests, all 11 runtime accounting checks, and MH_01 parity passed. |
| ENGINEERING | The first `rotation_fast` campaign completed replay but the harness rejected a ROS ANSI-prefixed exact-header summary as malformed. | Normalized only ANSI control prefixes, added regression coverage, and reran `rotation_fast` successfully; the frozen 11-sequence campaign then completed. |
| CORRECTNESS | A delegated broad `rg --files` command listed pathnames beneath forbidden `scripts/cp2/`. No file content there was opened, executed, copied, or modified. | Interrupted immediately; no edits resulted and no pathname was used for implementation or science. Exact command/output is retained in `CORRECTNESS_INCIDENT_SCRIPTS_CP2_PATH_LISTING.txt`. No private/holdout surface was involved. |
| CORRECTNESS | Three early focused commits were created after fresh/unit tests but before the required MH_01 parity and physically unavailable KAIST replay gates. | Commits were not amended. The ordering deviation remains recorded. Post-change MH_01 parity and all 11 KAIST replays passed before any later tracked commit. |

The archive's extracted `trans-mat.yaml` differs from the pinned repository
copy only in a comment prefix. The pinned repository file remains authoritative
and the reference-only transform is not applied at runtime. This provenance
discrepancy is declared, not silently normalized.

No SCIENTIFIC failure occurred. No raw holdout bag, private manifest, or
holdout-marked acquisition metadata was observed or used. The pathname-listing
incident above is a procedural correctness violation and is not relabeled as a
scientific or holdout-data event.

## Key artifact checksums

| Artifact | SHA-256 |
|---|---|
| official captured `kaist_vio_dataset.zip` | `1f3fe695b773342a7a1df227e651a7566802ea2e7a8d5ef1af49f0c112c09e1f` |
| public `ARCHIVE_SAFETY_AUDIT.json` | `d7647058217ff809cfd1be4d6d2e1d7644c2f80fc37d0285e12ab59946a21a30` |
| `artifacts/turnsafe/data/KAIST_DOWNLOAD_MANIFEST.json` | `21e035cc0cff890efb38b7d0dfb5035d680a6cbd8b1e1be4d411ff9fd5376ccf` |
| `artifacts/turnsafe/data/KAIST_BAG_INVENTORY.csv` | `33b91081885d1d0e849bd3b8517cd5bfe2ccf073a8a0d7e2a01a8a89a4869a51` |
| `artifacts/turnsafe/data/KAIST_COMPATIBILITY_REPORT.md` | `b2a3233556bfe02e2a48b30955c9af7638a2337c196eb198eb9edd936af74be2` |
| `artifacts/turnsafe/data/KAIST_BASELINE_RESULTS.csv` | `ef37abf1681dda6c7d685235f0e51d16fe9d0a1d17eaa251124143bad9058a17` |
| `artifacts/turnsafe/data/KAIST_SEQUENCE_MANIFEST.json` | `5714bbea191063ad707f401da6d3ce5d97b61f2784e7bbb9e0e5c694e4a42a93` |
| `artifacts/turnsafe/session_00_5/mh01_parity_final/digest.json` | `fbf64c988b7d08faaaeb00c7c3da46ebf845f7aee2536ea57684668e6d588f6d` |
| `artifacts/turnsafe/session_00_5/CTEST_ALL_FINAL.log` | `cfc17fd257cba73af51d77f58b53e27264f3b956fbb2b6076967b74dc321a729` |
| `artifacts/turnsafe/session_00_5/AFFECTED_CP2_CTEST_FINAL.log` | `f059b917b41439741e73f077f975434e78cf41ce15bbfcdc6b87baf239298939` |
| `artifacts/turnsafe/session_00_5/CORRECTNESS_INCIDENT_SCRIPTS_CP2_PATH_LISTING.txt` | `35954c39a001fb30f703655ec5533bc17a92b003b4e7a9ade13c1a93ebc5ce4c` |

The authoritative aggregate compatibility report ends with exactly
`RUNNABLE_WITH_DECLARED_ADAPTER`. Its manifest binds all 11 source/adapted bag
identities, adapter audits, runtime results, evaluation artifacts, and exact
MH_01 expected/observed digest equality.

## Gate state for the next session

Session 0.5 is complete and stops here. The next session may begin only after
the exact token `APPROVE_SESSION_1_T0_INSTRUMENTATION_KAIST_READY` is supplied.

Session 1 scope remains passive T0 instrumentation under `turnsafe.t0.v1`.
KAIST is now data-ready only through the declared adapter, fixed configuration,
and dedicated default-off exact-header launch seam frozen here. The next token
does not authorize changes to those inputs based on trajectory quality, any
estimator factor rows, fallback behavior, decision-changing gate, frontend
policy, or feature lifecycle. Runtime ground truth remains forbidden. The
Session 0 unavailable accepted-ID/lifecycle/callback-decision fields remain
declared gaps until passively exposed, and the consensus threshold set remains
`THRESHOLD_SET_NOT_FROZEN` pending the controlling Session 2 decision.
