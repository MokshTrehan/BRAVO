# Cross-dataset U0--S1 system comparison protocol

- Protocol ID: `CDSC-1R1`
- Status: **PROSPECTIVE_NOT_RUN**
- Frozen on: 2026-08-16, before the first CDSC-1R1 estimator attempt
- Restart lineage: supersedes the stopped `CDSC-1` tooling pilot described below
- Systems: pinned stock OpenVINS `U0` and frozen SchurVIO-Lite `S1`
- Datasets: EuRoC MAV, every runnable local TUM-VI bag, and fresh KAIST-11
- Primary question: post-initialization passage robustness
- Secondary question: accuracy on an identical ground-truth population
- Qualitative requirement: retain and review every emitted sparse geometry map

## 0. Restart provenance

The original `CDSC-1` tooling freeze at commit
`3199bb4fa3f2f77b8a7d181663e66b79992ea090` used protocol SHA-256
`85d2d9a81a4a36e513653a87aa0c788b15a34fe02056791fd33e65b0a6c142fb`.
Its append-only artifact root is
`/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1-20260816T142909Z`.
Only the two order-1 `MH_01_easy` estimator cells closed: U0 manifest SHA-256
`c541d35eb8e95a8ea9a0a6c8b9595ecf68717b3ec2a83295bdee9f0fa89c1812`
and S1 manifest SHA-256
`db523eb4ca129ecdfb774b99092c6787890ad2b2b41a5068badcbb772fc5d12c`.
The campaign then stopped before publishing any pair metric because the driver
passed each `sequence_result.json` where the evaluator required its enclosing
run directory. A post-close diagnostic also established that the exact
six-decimal EuRoC reference quaternions require the bounded evo-only projection
frozen in section 7. No capture cell or later scored cell was attempted.

Those two estimator outputs and the failure receipt remain immutable diagnostic
evidence but are excluded from every `CDSC-1R1` denominator and metric. This
restart changes only protocol/evaluator/orchestration tooling. U0, S1 estimator
source and binaries, configs, inputs, the 25-row matrix, and all scientific
acceptance rules are unchanged. Every `CDSC-1R1` estimator cell starts in a new
artifact root after this amended protocol and tooling commit are sealed.

## 1. Purpose and claim boundary

This is a paired, whole-system comparison. It asks whether the frozen S1
system continues from whatever time it initializes to the end of each locally
available passage, and how its accuracy compares with the original pinned U0
system on the common evaluable portion. Late initialization is recorded but is
not a passage-completion failure, as requested by the user. No output is
claimed for an omitted pre-initialization prefix.

This protocol cannot attribute an observed difference to Schur elimination.
The systems differ in source, and KAIST additionally preserves their different
native stereo-delivery seams. A positive result is therefore evidence about
the exact frozen systems, configurations, runners, and inputs below. It is not
evidence of generic VIO robustness, statistical superiority, an embedded or
Jetson result, a timing or energy result, or performance on unseen data.

Ground truth is never supplied to an estimator. It is opened only after both
scored estimator processes for a sequence have closed.

## 2. Frozen systems

### 2.1 U0: original pinned OpenVINS

U0 is the clean detached checkout at:

`/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/open_vins`

Its frozen identities are:

| Item | Identity |
|---|---|
| Upstream commit | `69488123ed9362dd44b6f28e7f4680abbff1442b` |
| Git tree | `12ab1c94ccae78ad50fa7376f47f0e55e2672257` |
| Existing serial executable | `/home/moksh/schurvio-baseline-triad/20260809T190830Z/build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf` |
| Executable SHA-256 | `c0e2203d0c01822fd089b32fb25d2b81dc850abc03c273d169b812801788d57b` |
| Native EuRoC estimator config SHA-256 | `b706f0082106e49e20c3292147d238b7e225b0df414106b9d4ac009bbb123f3b` |
| Native TUM-VI estimator config SHA-256 | `be7df3758fb38fbee102efdb6f2c98a7250416fe54e6045ff13b64a58972a060` |
| Native KAIST estimator config SHA-256 | `a7212a7b0e7e2df3ddefe402f12b5cbd155b368b43253f96a306d6638ba648d5` |

U0 must remain stock. It may not be patched, rebuilt, retuned, recalibrated,
reinitialized by a guardian, moved to S1's exact-header KAIST seam, or given a
different frontend to rescue an outcome. The only runtime bindings allowed
are the bag, full-run start and duration, native topics (or the three necessary
adapted-KAIST topic names), node namespace, passive state/deviation/timing
outputs, and isolated ROS paths. The source worktree, executable, resolved
libraries, native estimator configuration, and calibration files are hashed
again before the first run and bound into every U0 manifest.

`NO_INITIALIZATION`, partial output, tracking loss, a numerical failure,
`ESTIMATOR_CRASH`, and the known native teardown defect are valuable
original-system outcomes. None
is repaired or replaced.

### 2.2 S1: frozen SchurVIO-Lite

The evidentiary S1 implementation is the clean science snapshot used for the
validated KAIST recovery study:

| Item | Identity |
|---|---|
| Science commit | `2751bcdc0fae25c993b3224dc5fa40aaab6571d7` |
| Science tree | `b3f9191b8bce3852ebff71e1ebf180181bcd6d05` |
| Existing serial executable SHA-256 | `0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e` |
| KAIST recovery config SHA-256 | `fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2` |

The reporting commit and tag may identify the completed prior study, but each
run must bind the science source snapshot and compiled-input provenance above.
No S1 source, threshold, or estimator behavior may change during CDSC-1R1.

S1 uses two deliberately different dataset configuration rules:

1. **EuRoC and TUM-VI:** start from the byte-exact native dataset estimator
   configuration and add only:

   ```yaml
   up_msckf_landmark_elimination: schur
   up_msckf_max_visual_passes: 1
   ```

   Long-gap recovery remains at its production default, disabled. The key must
   be absent from the native dataset config and resolved ROS parameters, the
   compiled default must be verified as `false`, and the console must contain
   zero `[LONG-GAP-RECOVERY]` records. The existing EuRoC profile
   `config/euroc_mav/estimator_config_gate_d_schur_one_pass.yaml`, SHA-256
   `39279fd929ae91dc1ec63c44f17ff66c369dcd4c9f094ffbf7b95acae07f6d33`,
   already demonstrates exactly this two-key delta. For CDSC-1R1, both datasets
   use their byte-exact native config files and the two selectors are applied
   as typed parameters by the hash-frozen S1 launch file. The resolved
   parameter map and launch contract must prove that these are the only
   algorithm deltas. Native calibration behavior, `max_slam: 50`, camera
   model, masks, initialization, frontend, window, and noise parameters remain
   unchanged.

2. **KAIST:** use the already-validated
   `config/kaist_vio_rotation_robustness/estimator_config.yaml` and its exact
   calibration files. This is the only CDSC-1R1 dataset on which long-gap
   recovery is enabled.

The recovery boundary is intentional. The frozen recovery implementation and
runtime contract are KAIST pinhole/radtan, fixed-calibration evidence. Native
EuRoC/TUM-VI configurations do not satisfy that contract; TUM-VI is
equidistant and the native profiles enable online camera calibration. Enabling
recovery there would be a new algorithm/configuration study and is forbidden
in CDSC-1R1. In particular, the prior TUM-VI camera-conditioning profile is not
eligible: it fixed calibration and set `max_slam: 0`, so it is neither native
S1 nor capable of supplying C2's retained SLAM landmarks.

## 3. Dataset inventory and fixed population

All local candidates must be checked before any download. Exact bag size and
the recorded checksum are verified first; a missing or irreparably corrupt
candidate may be acquired only before the CDSC-1R1 input manifest is frozen.
No bag is overwritten. The tracked `project/datasets.yaml` TUM-VI entries are
stale: they refer to old pending paths, while the three runnable bags are
already present under `calibrated/512_16`.

### 3.1 EuRoC MAV: 11 scored sequences

All eleven bags and all eleven TUM-format references are local. The bag paths
and hashes already recorded in `project/datasets.yaml` are revalidated. The
reference is `ov_data/euroc_mav/<sequence>.txt`; `V1_01_easy.txt` is the
corrected reference and `V1_01_easy_original.txt` is not used.

The fixed EuRoC order is:

1. `MH_01_easy`
2. `MH_02_easy`
3. `MH_03_medium`
4. `MH_04_difficult`
5. `MH_05_difficult`
6. `V1_01_easy`
7. `V1_02_medium`
8. `V1_03_difficult`
9. `V2_01_easy`
10. `V2_02_medium`
11. `V2_03_difficult`

### 3.2 TUM-VI: three runnable local sequences, one scored

The complete local bag population is exactly:

| Sequence | Absolute bag path | Bytes | Recorded MD5 | Accuracy role |
|---|---|---:|---|---|
| `room4` | `/home/moksh/Downloads/tum_vi/calibrated/512_16/dataset-room4_512_16.bag` | 2,346,890,121 | `1d9914b87a5374123988ea35ea5ccfa0` | scored |
| `corridor4` | `/home/moksh/Downloads/tum_vi/calibrated/512_16/dataset-corridor4_512_16.bag` | 2,029,099,748 | `a8b937f85258c745fa5baf217be2701d` | completion and qualitative only |
| `outdoors4` | `/home/moksh/Downloads/tum_vi/calibrated/512_16/dataset-outdoors4_512_16.bag` | 14,736,044,692 | `92a0a8d6887348e408c76ebc22997544` | completion and qualitative only |

Only `room4` has an eligible local full reference:

`ov_data/tum_vi/dataset-room4_512_16.txt`

Its SHA-256 is
`2e8819fd0371c10125f33879bb2209146c126a2bff101e3c8ef20852e8597b67`.
No accuracy, drift, or direct aligned-map ranking is reported for `corridor4`
or `outdoors4`. Their absence of a full eligible reference is not repaired by
inventing an endpoint or partial-ground-truth score.

### 3.3 KAIST-VIO: 11 scored sequences

KAIST uses the unchanged adapted ROS1 bags under:

`/home/moksh/datasets/KAIST_VIO/raw/turnsafe_adapted/`

and the references `ov_data/kaist_vio/<sequence>.txt`. The source/adapted bag
identities, adapter audit, and exact-header census remain checksum-bound. The
fresh order reuses the prospectively committed G0.5 order:

1. `infinite/infinite_fast.bag`
2. `square/square_fast.bag`
3. `square/square.bag`
4. `circle/circle_head.bag`
5. `rotation/rotation.bag`
6. `infinite/infinite.bag`
7. `square/square_head.bag`
8. `circle/circle.bag`
9. `rotation/rotation_fast.bag`
10. `circle/circle_fast.bag`
11. `infinite/infinite_head.bag`

### 3.4 Global order and method alternation

There are 25 sequence pairs: 11 EuRoC, 3 TUM-VI, and 11 KAIST. Twenty-three
pairs have eligible ground truth. Scored runs are serial and adjacent within a
pair. Odd global ordinals run U0 then S1; even ordinals run S1 then U0.

| Global order | Dataset | Sequence | Scored method order |
|---:|---|---|---|
| 1 | EuRoC | `MH_01_easy` | U0, S1 |
| 2 | EuRoC | `MH_02_easy` | S1, U0 |
| 3 | EuRoC | `MH_03_medium` | U0, S1 |
| 4 | EuRoC | `MH_04_difficult` | S1, U0 |
| 5 | EuRoC | `MH_05_difficult` | U0, S1 |
| 6 | EuRoC | `V1_01_easy` | S1, U0 |
| 7 | EuRoC | `V1_02_medium` | U0, S1 |
| 8 | EuRoC | `V1_03_difficult` | S1, U0 |
| 9 | EuRoC | `V2_01_easy` | U0, S1 |
| 10 | EuRoC | `V2_02_medium` | S1, U0 |
| 11 | EuRoC | `V2_03_difficult` | U0, S1 |
| 12 | TUM-VI | `room4` | S1, U0 |
| 13 | TUM-VI | `corridor4` | U0, S1 |
| 14 | TUM-VI | `outdoors4` | S1, U0 |
| 15 | KAIST | `infinite_fast` | U0, S1 |
| 16 | KAIST | `square_fast` | S1, U0 |
| 17 | KAIST | `square` | U0, S1 |
| 18 | KAIST | `circle_head` | S1, U0 |
| 19 | KAIST | `rotation` | U0, S1 |
| 20 | KAIST | `infinite` | S1, U0 |
| 21 | KAIST | `square_head` | U0, S1 |
| 22 | KAIST | `circle` | S1, U0 |
| 23 | KAIST | `rotation_fast` | U0, S1 |
| 24 | KAIST | `circle_fast` | S1, U0 |
| 25 | KAIST | `infinite_head` | U0, S1 |

The scored lane completes before the capture lane begins. Capture replays use
the same global order and method alternation. Each process receives a fresh
ROS master port, `ROS_HOME`, log directory, and process group. Both systems use
the same fixed CPU affinity and single-process environment; OpenCV and math
library thread settings are bound. No estimator runs in parallel. Each pair
uses the frozen native start offset in the matrix (the upstream-recommended
EuRoC offsets and zero for TUM-VI/KAIST), then runs to the end of the bag;
timing/resource values are diagnostic only.

## 4. Native input delivery and recovery expectations

The comparison preserves native delivery rather than forcing callback
identity:

- On EuRoC and TUM-VI, both serial runners use the inherited record-time
  stereo rule because the KAIST exact-header option is false.
- On KAIST, U0 retains the upstream record-time `<20 ms` stereo rule and S1
  retains its frozen exact-header seam.

Every cell therefore retains a deterministic pairing census: input topic
counts, exact-header matches and unmatched images, native selected/skipped
pairs and record skew, selected-input first/last timestamps, pair-set
intersection/differences where applicable, and first/last estimator output.
A native U0 skip is a U0 system outcome, not infrastructure invalidity.

The expected S1 recovery evidence is fixed:

| Population | Recovery enabled | Required fresh evidence |
|---|---:|---|
| EuRoC 11 | no | key absent; compiled default `false`; zero `[LONG-GAP-RECOVERY]` records |
| TUM-VI 3 | no | key absent; compiled default `false`; zero `[LONG-GAP-RECOVERY]` records |
| KAIST `rotation` | yes | exactly 1 activation, 1 commit, 0 failures, epoch 1 |
| Other KAIST 10 | yes | exactly 0 activations, 0 commits, 0 failures, epoch 0 |

For the one expected KAIST commit, all frozen C2 invariants must be present:
three state-unchanged accepted attempts, the frozen correspondence/inlier/
ratio/reprojection/span/orientation/consensus gates, a complete commit
covariance, and the declared five-row timing omission comprising the commit
plus four propagate-only warm-up rows. Ground truth must be absent from the
runtime process. Any different algorithm outcome is retained and is a fresh
recovery/determinism failure; it is not tuned or retried.

## 5. Evidence validity and outcome statuses

Evidence validity and estimator outcome are separate fields.

### 5.1 Evidence validity

- `VALID`: all source, executable, library, config, calibration, input,
  command, environment, and output bindings close and all checksums verify.
- `INVALID_INFRA`: an external failure such as a port collision, harness
  launch failure, storage I/O failure, or process-supervision failure occurred
  before an estimator outcome can be interpreted.
- `INVALID_PROVENANCE`: source/config/input bytes changed during a run or a
  required identity cannot be reconciled.
- `CAPTURE_LINK_INVALID`: a capture ran but its estimator outputs do not match
  the linked scored outputs byte-for-byte.

### 5.2 Estimator outcome

- `COMPLETED`: valid passage completion and clean process termination.
- `COMPLETED_WITH_TEARDOWN_DEFECT`: U0 alone reached complete valid coverage
  before its already-known native teardown abort. It is accuracy-eligible, but
  `strict_process_health` remains false and the defect is never hidden.
- `NO_INITIALIZATION`: no valid initialized state was emitted.
- `PARTIAL`: initialized finite output exists but does not satisfy the terminal
  passage rule.
- `TRACKING_LOSS`: an unsupported post-initialization output gap or terminal
  loss is observed.
- `NUMERIC_FAILURE`: nonfinite state, covariance failure, undeclared reset, or
  another numerical integrity failure is observed.
- `ESTIMATOR_CRASH`: the estimator terminates abnormally before eligible passage
  completion, including an empty-output crash.

Partial and failed files remain evidence. Empty expected products are recorded
as explicit absences rather than silently omitted.

## 6. Primary passage-completion rule

Initialization must occur, but there is no initialization deadline. The first
valid initialized state begins the evaluated passage. Initialization time in
seconds, its fraction of the selected-input span, and the excluded prefix are
always reported.

A system passes a sequence's post-initialization passage only when all of the
following hold:

1. State and deviation outputs are nonempty, finite, strictly timestamp
   increasing, mutually row/timestamp consistent, and free of non-recovery
   resets and covariance-failure evidence.
2. If `t_input_last` is the last callback in that system's frozen selected
   stereo stream and `t_state_last` is its final state, then
   `-0.01 s <= t_input_last - t_state_last <= 0.10 s`.
3. Every post-initialization state gap is at most 0.20 s, or the gap is fully
   input-supported: its endpoints correspond to consecutive callbacks in that
   system's selected stereo stream, no selected callback exists inside it, and
   selected-input and adjacent timing/output gap durations agree within
   1 microsecond. A C2 recovery interval must additionally bind to its recovery
   events and exact five-row timing exception.
4. No ground-truth access, user intervention, hidden restart, trajectory
   stitch, smoothing, or imputation occurred.
5. Cleanup leaves no descendant process. A nonzero process exit fails strict
   process health. Only U0's predeclared post-coverage teardown classification
   may preserve passage and accuracy eligibility after all required outputs
   were already complete.

The primary comparison reports, for each dataset and overall:

- S1 and U0 passage counts over the complete scheduled population;
- S1-only failures and U0-only failures;
- strict-process-health counts separately;
- every late initialization and input-supported gap; and
- KAIST family passage results: rotation 2/2, circle 3/3, infinite 3/3, and
  square 3/3, without assuming their outcome in advance.

The ordered passage labels are:

- `PASSAGE_DOMINANT`: S1 has no S1-only passage failure, is not worse than U0
  within EuRoC, TUM-VI, or KAIST, and completes more passages overall.
- `PASSAGE_PARITY`: neither system has a completion advantage within any
  dataset and their overall counts are equal.
- `PASSAGE_MIXED`: neither system dominates because each has at least one
  discordant completion or family-level directions disagree.
- `PASSAGE_REGRESSION`: at least one U0-complete/S1-incomplete passage exists.
- `PASSAGE_UNASSESSABLE`: provenance or unresolved infrastructure prevents a
  complete scheduled denominator.

If S1 passes all 25 local passages, the report may additionally state
`S1_LOCAL_PASSAGE_25_OF_25`. This means only continuity from initialization to
the final selected input; it is not full-prefix or generic robustness.

## 7. Secondary common-population accuracy

Accuracy is computed only when both systems have valid passage-eligible
outputs and an eligible full reference. Failed or missing trajectories remain
in the completion denominator and receive no invented error.

The one common evaluator and reference contract is:

1. Retain and hash every original TUM row, token, and row identity unchanged.
   For evo objects only, project each finite nonzero quaternion as
   `q / ||q||` when `abs(||q|| - 1) <= 0.0005`; reject the pair if any source
   quaternion falls outside that frozen bound. Derived common-population TUM
   files retain the source pose tokens. Record the source norm range, row
   count, and maximum absolute norm error separately for the reference and
   both estimates.
2. Associate each estimate independently to unique nearest reference rows
   within 0.01 s.
3. Intersect the exact reference-row identities associated to both systems.
   The two estimates are then evaluated on that identical common population.
4. Require at least 100 common poses. Report the common start/end timestamps,
   duration, spatial coverage, and fraction of each reference retained so late
   starts remain visible.
5. Align each estimate independently to the identical reference population by
   SE(3) Umeyama alignment without scale.
6. Report translation ATE RMSE.
7. Construct one 1 m all-pairs-from-reference endpoint list from the common
   reference population and apply the identical list to both systems. Require
   at least 100 pairs, then report translation RPE RMSE and rotation RPE RMSE
   in degrees.

For metric `m`, define `d[s,m] = (m[S1] - m[U0]) / m[U0]`; negative values
favor S1. If a U0 denominator is zero/nonfinite, the corresponding relative
endpoint is unassessable rather than assigned an ad hoc value.

Accuracy is deliberately secondary to passage robustness and receives its own
label:

- A family with eleven scored sequences (EuRoC or KAIST) is assessable only
  with at least eight common passage-eligible pairs.
- A family passes `ACCURACY_NONINFERIOR` only when every metric's median `d`
  is at most 0.10 and no individual `d` exceeds 0.20.
- Otherwise it is `ACCURACY_GUARD_FAIL` or `ACCURACY_UNASSESSABLE`.
- TUM-VI `room4` is a single descriptive paired result and cannot establish a
  TUM-VI family median or noninferiority result.

All per-sequence metrics and ratios are reported even when a family gate is
unassessable. No accuracy win is required for a passage-robustness result, and
an accuracy-guard failure is not relabeled as a completion failure. The final
report preserves the complete outcome vector rather than collapsing it into a
single favorable label.

## 8. Exact-byte and determinism checks

Fresh scored runs are the primary scientific evidence. Historical artifacts
are used only as compatible determinism checks.

- Compatible U0 state/deviation/TUM expectations exist for all eleven KAIST
  cells in the frozen G0.5 scored manifests under
  `/home/moksh/schurvio-icra27-artifacts/g05/primary/`.
- Compatible C2 S1 expectations exist for all eleven KAIST cells in
  `project/evidence/rotation_robustness/R2_ARTIFACT_INDEX.csv`.
- No eligible full-run native U0/current-S1 exact ledger exists for EuRoC or
  TUM-VI. CP0 `MH_01_easy` begins at 40 s and uses a different binary package;
  the baseline-triad profile is not the native configuration; prior
  camera-conditioning and preemption runs bind different source/configuration
  profiles. Their hashes are context, not goldens.

Every fresh KAIST scored state, deviation, and TUM file must be compared to its
compatible historical identity. A mismatch is retained and reported as a
determinism/provenance finding; a clean valid fresh run is never overwritten
by the historical file. For every dataset, the linked capture replay must be
byte-identical to its scored state, deviation, and TUM files. Timing values may
differ, but ordered timing/state timestamp accounting must satisfy the system's
declared normal or C2 exception.

## 9. Qualitative geometry and map retention

Every scored cell, including `NO_INITIALIZATION`, partial,
`ESTIMATOR_CRASH`, and recovery failure, receives a linked capture attempt
after the scored lane. Capture is passive and
may not modify U0 or estimator decisions. Each cell retains, when emitted:

- the closed raw sparse feature/geometry stream;
- raw state, deviation, timing, and TUM trajectory;
- the literal final active SLAM landmark cloud;
- transient last-update MSCKF points and active loop/track points;
- deterministic PLY snapshots and top, side, and oblique SVG views;
- `PNG_NOT_PRODUCED_NO_APPROVED_DETERMINISTIC_RASTERIZER` in the qualitative
  manifest (the frozen host has no approved deterministic SVG rasterizer);
- clipped/missing point counts, epoch boundaries, and trajectory gaps;
- a machine-readable qualitative review, manifest, and `SHA256SUMS`.

If stock U0 exposes no requested geometry, record `MAP_NOT_EXPOSED`; do not add
instrumentation to U0 or fabricate a map. An empty or partial map from an
algorithm failure remains the result. The raw stream is authoritative.
`points_slam` is the current sparse filter state, `points_msckf` is transient
last-update geometry, and loop/track points are transient. None is described
as a persistent or dense reconstruction.

For reference-backed sequences, both systems use the same full-GT-derived
bounds, margin, deterministic snapshot-selection rule, marker styles, and fixed
views. Snapshot timestamps are derived from each capture's valid GT overlap
and are recorded explicitly; when late initialization makes those timestamps
differ, the snapshots are not treated as a direct paired ranking. There is no
smoothing, interpolation, point pruning, or method-specific crop. For TUM-VI
`corridor4` and `outdoors4`, native-frame figures are saved for failure
inspection but are illustration-only and cannot support a directly aligned
visual ranking.

The review records `missing`, `gap`, `jump`, `gross_divergence`,
`apparent_drift`, `feature_collapse`, `feature_explosion`, and `uncertain`, with
severity 0--3. It explicitly states whether each KAIST rotation, square,
infinite, and circle passage family is visually continuous. A catastrophic
qualitative flag is reconciled against raw trajectory, input, recovery, and
metric evidence before any positive system wording. It never licenses a rerun
or estimator repair.

Qualitative outcomes are `QUALITATIVE_PASS`, `QUALITATIVE_FLAGGED`, or
`QUALITATIVE_UNASSESSABLE`, reported separately from passage and accuracy.

## 10. Append-only artifacts

The campaign root is a new timestamped directory under:

`/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/`

Every attempt has a unique run ID and retains source commit/tree/diff status,
compiled-input and library identities, config/calibration/launch hashes,
bag/reference identities, resolved parameters, exact command and environment,
pairing census, console and resource logs, process status, partial/complete
outputs, recovery records, metric commands/results, geometry or explicit
absence, manifest, and checksums. Finalized attempt directories are never
overwritten. Git tracks this protocol, small indices, reports, and small
figures; large raw artifacts remain checksum-bound outside Git.

## 11. Retry, continuation, and stop rules

One valid scored attempt is scheduled for each system/sequence cell. An
estimator outcome is never retried: `NO_INITIALIZATION`, late initialization,
partial coverage, rejected recovery, tracking loss, numeric failure,
`ESTIMATOR_CRASH`, and U0
teardown are final observations. The campaign continues through later
scheduled cells so the entire denominator is retained.

Only a proven external `INVALID_INFRA` may be retried. The invalid attempt is
kept, the correction may touch only infrastructure outside both estimators and
their inputs, and **both U0 and S1 are rerun for that sequence** with new IDs
and the original method order. A one-sided retry cannot enter the paired
result. A capture may be repeated only for independently proven capture
infrastructure failure; an estimator-output mismatch is a determinism result,
not retry authority.

Stop the campaign without estimator repair when any of these occurs:

- U0 or S1 source, binary, linked library, config, calibration, bag, or
  reference identity drifts after freeze;
- the native-U0 or dataset-specific S1 configuration rule is violated;
- an infrastructure/algorithm distinction cannot be proved;
- disk safety, checksum closure, or process-group cleanup cannot be ensured;
- a harness or protocol change would be required after estimator execution
  began; or
- ground truth entered an estimator process.

A harness defect discovered after execution starts requires a new protocol/
campaign identity and preserves all existing attempts; it does not authorize
retroactive mutation. Algorithm failures alone do not stop remaining matrix
execution and never authorize tuning.

## 12. Final reporting contract

The final report includes all 50 scored cells and all 50 capture attempts, not
only common successes. It publishes:

1. provenance and exact input/config identities;
2. per-cell evidence validity, estimator outcome, initialization time,
   passage status, tail and maximum gap, process health, and recovery counts;
3. completion counts and discordances by EuRoC, TUM-VI, KAIST, and overall;
4. every common-population metric, population size, ratio, and family accuracy
   label;
5. room4 as the sole TUM-VI accuracy cell and corridor4/outdoors4 as explicitly
   unscored;
6. scored/capture and historical KAIST determinism checks;
7. the map/geometry inventory and qualitative flags, including whole KAIST
   passage-family judgments; and
8. every failed, invalid, partial, or unassessable attempt.

Permitted wording follows the observed outcome exactly, for example: “With
late initialization excluded, frozen S1 completed from initialization to the
final selected input on X/25 local passages versus Y/25 for pinned stock U0.”
It must name the datasets, state that corridor4/outdoors4 lack accuracy scores,
and preserve strict process-health defects. “S1 is more accurate,” “Schur
caused the improvement,” “S1 tracked every dataset from the beginning,” and
general robustness/superiority claims require separate evidence and are not
authorized by CDSC-1R1.
