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
