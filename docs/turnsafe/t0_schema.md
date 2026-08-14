# TurnSafe T0 diagnostic schema

Status: Session 1E passive event-extension contract; replay validation pending

Schema identifiers: `turnsafe.t0.v1` and additive
`turnsafe.t0.event_extension.v1`

Authority: v6 plan Sections 5.6, 7, 8.7--8.11, 12.1, 12.3, and 15

This schema describes evidence emitted by capture-only T0 diagnostics. It does
not authorize factor rows, proposal changes, new gates, fallback behavior, or
live-state mutation. A missing diagnostic is represented explicitly; estimator
behavior is never changed to manufacture a value.

Session 1E retains every `turnsafe.t0.v1` field and adds an optional event-ready
extension under the compatibility surface already reserved by `extensions`.
Until the Session-1E precommit replay gates finish, this document makes no new
parity or campaign claim. The extension records causal primitives and stable
offline association inputs only; it does not segment events, assign outcomes,
choose thresholds or branches, or construct a factor.

The repository-convention tests needed for stereo range covariance, relative camera-center
translation covariance, and CamRadtan inverse bearing covariance were not
established in this run. Their emitted typed results are respectively:

```text
SHADOW_NOT_COMPUTED_STEREO_RANGE_COVARIANCE_NOT_ESTABLISHED
SHADOW_NOT_COMPUTED_RELATIVE_CAMERA_CENTER_JACOBIAN_NOT_CERTIFIED
SHADOW_NOT_COMPUTED_PIXEL_TO_BEARING_JACOBIAN_NOT_CERTIFIED
```

The absolute-angle, median/MAD, and spatial-coverage thresholds remain
unfrozen. Consensus, eligible-group, winner, runner-up, and derived survival
fields therefore remain `NOT_APPLICABLE`, retain reason
`THRESHOLD_SET_NOT_FROZEN`, and also carry typed result
`SHADOW_NOT_COMPUTED_THRESHOLD_SET_NOT_FROZEN`. Session 1 does not choose or
infer those thresholds.

## 1. Non-interference contract

Capture mode obeys all of the following:

1. Read from the same immutable callback prior used by the baseline path.
2. Preserve baseline feature order, thresholds, branches, proposal contents,
   state/covariance commit count, and finalization behavior.
3. Keep shadow values out of estimator decisions. In particular, shadow NIS,
   winner score, certificate status, and predicted information are write-only
   telemetry.
4. Store value-only identifiers and diagnostics; no record owns a live feature,
   database, state, or mutation callback.
5. While capture remains active, emit one terminal callback record even when
   the baseline coasts, rejects, or resets. A contained diagnostic failure
   atomically disables capture and records only its fixed-size reason/counter.
6. Prove disabled and capture-only decision parity against the frozen digest.

These requirements specialize the v6 plan Sections 5.1--5.6, 7, 12.1, and 15
(Session 1).

Native masks, accepted IDs, database writes, tracker state, estimator decision,
and finalization complete independently of diagnostic containers. Every
diagnostic allocation/copy, summary, grouping, serialization, observer call,
sink open/write/flush, and final publication operation is inside an explicit
containment boundary with ordered `bad_alloc`, standard-exception, and unknown
handling. The first failure stores only a fixed enum and saturating counter;
no diagnostic retry or native decision change is permitted. Capture-off does
not allocate diagnostic containers on the live KLT path.

## 2. Serialization and identity

The required interchange form is UTF-8 JSON Lines. A run header is followed by
one `callback` object per baseline visual callback. Diagnostic implementations
may use typed in-memory records, but the emitted representation has these
rules:

- JSON keys use the names in this document; unknown additive keys are allowed
  only under an `extensions` object.
- Arrays whose order is not explicitly semantic are sorted by the canonical
  keys below before serialization.
- Finite numeric values are serialized with round-trip precision. `NaN`,
  infinities, and sentinel magnitudes are never JSON values; use the
  availability/reason representation in Section 3.
- A repository timestamp is preserved without arithmetic conversion in
  `timestamp_value` and paired with `timestamp_key`, a bit-preserving canonical
  representation used for equality and sorting.
- Sensor timestamps are deterministic inputs and may enter the digest.
  Wall-clock time, process/thread IDs, host paths, log prefixes, and measured
  durations are nondeterministic metadata and never enter the deterministic
  digest.

Canonical keys are:

```text
observation_key = (camera_id, timestamp_key, feature_id,
                   detached_index, observation_ordinal)
pair_key        = (camera_id, source_timestamp_key, target_timestamp_key)
candidate_key   = (camera_id, source_timestamp_key, target_timestamp_key,
                   feature_id, detached_index,
                   source_observation_ordinal,
                   target_observation_ordinal)
callback_key    = callback_index
```

Timestamp-key comparison is bytewise over the fixed-width bit-preserving key.
The complete callback candidate collection is globally sorted once by
`candidate_key` after collection and before grouping or serialization. Group
membership, group order, and any foregone-group order derive from the same
typed keys. Duplicate detached attempt identities or duplicate complete
candidate keys disable capture as an ownership error; input order is never a
tie-breaker. `callback_index` is the zero-based order of baseline visual
callbacks. Attempt records sort by unique `detached_index`; observations within
an attempt sort cameras by numeric `camera_id` and preserve native vector order
within each camera. Those positions define the observation ordinals.

### Run header

| Field | Type | Required meaning |
|---|---|---|
| `schema` | string | Exactly `turnsafe.t0.v1` |
| `record_type` | string | `run_header` |
| `frozen_base_sha` | string | Frozen repository identity |
| `source_sha` | string | Git HEAD independently resolved by the source-snapshot tool at configure time |
| `tree_sha` | string | Git `HEAD^{tree}` independently resolved at configure time |
| `source_snapshot_sha256` | SHA-256 | Aggregate identity of HEAD/tree, porcelain state, tracked diff, compiled inputs, untracked compiled inputs, and curated replay/provenance tooling inputs |
| `source_dirty` | boolean | True only for an explicit precommit dirty snapshot identity |
| `build_provenance_id` | SHA-256 | Configure descriptor ID compiled into the TurnSafe-owning shared library |
| `configure_manifest_sha256` | SHA-256 | Hash of the configure manifest used to generate the compiled identity |
| `build_manifest_sha256` | SHA-256 | Campaign-computed hash of the detached binary/library/cache build manifest |
| `binary_sha256` | string | Hash of the estimator executable actually launched |
| `config_sha256` | string | Hash of the exact effective estimator configuration |
| `calibration_sha256` | string | Hash of the exact calibration input |
| `capture_mode` | string | `disabled` or `capture_only`; T0 has no active mode |
| `supported_configuration` | boolean | Result of the Section 4 production-envelope check, used only for telemetry eligibility |
| `unsupported_reasons` | array[string] | Stable sorted reason codes; empty when supported |
| `resolved_configuration` | object | Actual one-pass Schur, FEJ, GLOBAL_3D, CamRadtan, fixed-calibration, stereo, camera-count, and target-range predicates |
| `threshold_set_id` | string/status | Hash or immutable identifier for all shadow thresholds |
| `digest_contract_version` | string | Baseline digest contract used by the run |

Caller-provided source SHA/tree/build values are expectations only; a conflict
disables capture. The source-snapshot tool accepts only repository and output
paths. A detached build manifest binds the configure descriptor, CMake cache,
schema, estimator executable, and both TurnSafe-owning shared libraries. The
configure descriptor and cache must identify the same source/build directories,
compiler, cached flags, generator, Ceres prefix, ROS setting, and testing
setting. The descriptor separately records the effective `CMAKE_CXX_FLAGS`;
the finalizer and campaign bind the generated `ov_msckf_lib` target-flags file
and require its effective-flag prefix to match. The campaign also verifies that
the dynamic loader resolves `ov_msckf` and `ov_core` to
the exact manifest-bound library paths, then regenerates and compares the source
snapshot before and after every provenance-bound replay. Capture-on implies
this binding; capture-off parity runs request the same binding explicitly while
leaving telemetry disabled.

Dataset names, maneuver labels, development/holdout membership, ground truth,
future frames, and final-error feedback are not estimator inputs and are not
part of this header. Offline evaluation may join permitted development metadata
after replay. This boundary follows Sections 5, 12.3, and 13.3.

## 3. Availability and failure representation

Every conditionally available scalar/object uses:

```json
{"status":"AVAILABLE","value":0.0,"reason":"NONE"}
```

When unavailable, `value` is omitted and `status` is one of:

```text
NOT_APPLICABLE
NOT_EXPOSED
NOT_AVAILABLE
NOT_AVAILABLE_FROM_SENSOR
UNSUPPORTED_CONFIGURATION
INVALID_INPUT
NONFINITE
NUMERICAL_FAILURE
```

`reason` is a stable machine-readable code. An optional `native_status` retains
the baseline/internal value verbatim when a precise mapping is not justified.
Free text is supplementary and excluded from deterministic hashes. Unknown
native failures are never coerced into a fallback-eligible class (Sections 7.4
and 9.1).

## 4. Callback envelope and funnel

Each callback record contains:

| Field | Type | Required meaning |
|---|---|---|
| `schema` | string | `turnsafe.t0.v1` |
| `record_type` | string | `callback` |
| `callback_index` | uint64 | Stable callback order |
| `callback_timestamp_value` | repository timestamp type | Original sensor timestamp |
| `callback_timestamp_key` | string | Canonical timestamp identity |
| `prior_fingerprint` | string/status | Read-only fingerprint of the one captured prior, when exposed |
| `frontend_cameras` | array | Section 5 records, ordered by camera ID |
| `full_track_attempts` | array | Section 6 records, sorted by unique detached index |
| `shadow_candidates` | array | Section 7 records, ordered by candidate key |
| `shadow_groups` | array | Section 8 records, ordered by pair key |
| `prior_primitives` | object/status | Immutable clone, calibration, and covariance primitives captured before mutation, or a typed absence reason |
| `funnel` | object | Counts below, derived without changing the baseline |
| `baseline_decision` | object | Section 9 baseline result |
| `shadow_summary` | object | Section 9 counterfactual summary |
| `completeness` | object | Required fields present/missing and reasons |

The `funnel` object provides absolute callback counts for every v6 T0 stage:

```text
camera_frames
previous_tracked_points
klt_status_survivors
in_bounds_survivors
mask_survivors
native_combined_track_survivors
fmatrix_input_points
fmatrix_survivors
database_observations_written
terminal_attempt_count
tracks_with_valid_clone_pairs
attempt_valid_clone_pair_count_total
full_outcome_counts_by_code
triangulation_outcome_counts_by_code
refinement_outcome_counts_by_code
schur_outcome_counts_by_code
full_nis_outcome_counts_by_code
accepted_full_factors
accepted_full_rows
shadow_pair_candidates
candidate_pairs_with_target_stereo
attempts_with_at_least_one_target_stereo_pair
groups_with_at_least_one_target_stereo_pair
candidates_with_calibrated_range_lcb
candidates_with_calibrated_translation_ucb
candidates_translation_acute
candidates_passing_rho_trans
groups_n_lt_2
groups_n_2
groups_n_3
groups_n_ge_4
groups_passing_rank
groups_passing_consensus
eligible_groups_before_selection
winner_count
foregone_eligible_groups
foregone_eligible_features
winner_shadow_factors_passing_nis
winner_shadow_rows_passing_nis
```

No funnel stage is inferred from a later count. For example, target stereo
availability, calibrated range-LCB availability, acute survival, and rho
survival remain distinct even when they happen to have equal counts. Required
event-level aggregations and plots are derived offline from these callback
records, as required by Sections 7.1 and 7.7.

## 5. Frontend camera record

One record is emitted per camera frame processed in the callback. Rejection
counts are exclusive at the named sequential stage.

| Field | Type | Required meaning |
|---|---|---|
| `camera_id` | integer | Stable repository camera ID |
| `source_camera_timestamp` | timestamp/status | Previous same-camera frame timestamp when the native tracker exposes one |
| `frame_timestamp_value/key` | timestamp pair | Exact sensor-frame identity |
| `reseed_count` | uint64 | Native reseed population for this frame |
| `klt_attempted_count` | uint64 | Points for which native temporal KLT was attempted |
| `klt_counts_available` | boolean | Whether the native path exposed exact KLT stage counts |
| `previous_tracked_points` | uint64 | Points presented to temporal KLT |
| `klt_status_survivors` | uint64 | Points with passing KLT status before bounds/mask checks |
| `klt_status_rejections` | uint64 | KLT status failures |
| `out_of_bounds_rejections` | uint64 | Status survivors rejected by bounds |
| `in_bounds_survivors` | uint64 | Status survivors remaining after bounds |
| `mask_rejections` | uint64 | In-bounds points rejected by the existing mask |
| `mask_survivors` | uint64 | Points remaining after the existing mask |
| `mask_stage_present` | boolean | Whether the native path executed a distinct mask stage |
| `fmatrix_input_points` | uint64 | Points submitted to the existing fundamental-matrix test |
| `fmatrix_inliers` | uint64 | Existing-test inliers |
| `fmatrix_rejections` | uint64 | Existing-test rejections |
| `native_combined_track_survivors` | uint64 | Points passing the unchanged KLT/RANSAC combination and native bounds/mask rules |
| `database_observations_written` | uint64 | All observations written for the frame |
| `database_tracked_observations_written` | uint64/status | Writes attributable to the tracked funnel, if exposed |
| `database_new_observations_written` | uint64/status | Writes attributable to new detections, if exposed |
| `reset_too_few_points` | boolean | Existing low-point reset decision |
| `reset_native_reason` | string/status | Existing native reason, if exposed |
| `forward_backward_check` | object/status | Native forward/backward-check evidence, or explicit native-path absence |
| `klt_error_summary` | object | Distribution defined below |
| `gyro_magnitude_rad_s` | number/status | Read-only frame-interval gyro magnitude |
| `gyro_integrated_rotation_rad` | number/status | Read-only integrated rotation over the exact camera interval |
| `blur_metric` | number/status | Existing or sensor-provided metric only |
| `exposure` | number/status | Recorded metadata with units named in `extensions` |
| `gain` | number/status | Recorded metadata with units named in `extensions` |
| `native_accepted_feature_ids` | array | Exact native KLT/RANSAC accepted IDs after all unchanged frontend gates |

`klt_error_summary` covers finite KLT errors for status survivors and contains
`count`, `nonfinite_count`, `min`, `max`, `mean`, `p50`, `p90`, `p95`, and
`p99`. Quantiles use deterministic nearest-rank selection on ascending finite
values. T0 does not alter masks, thresholds, initialization, or the geometric
model to populate these fields (Sections 7.2 and 15, Session 1).

The following accounting equalities are checked and logged, not used as gates:

```text
previous_tracked_points = klt_status_survivors + klt_status_rejections
klt_status_survivors = in_bounds_survivors + out_of_bounds_rejections
in_bounds_survivors = mask_survivors + mask_rejections
fmatrix_input_points = fmatrix_inliers + fmatrix_rejections
```

If existing control flow prevents an equality from being observed precisely,
the affected field is `NOT_EXPOSED`; it is not reconstructed by changing the
frontend.

## 6. Full-track attempt record

An immutable record is emitted for every detached terminal track, even when an
early full-path stage fails.

| Field | Type | Required meaning |
|---|---|---|
| `detached_index` | uint64 | Index in the immutable detached batch |
| `feature_id` | repository ID type | Baseline feature identity |
| `ordered_observation_keys` | array | Numeric camera order, preserving native vector order within each camera |
| `attempt_has_any_live_same_camera_clone_pair` | boolean | Any same-camera pair has two live clones, independent of typed eligibility |
| `attempt_valid_clone_pair_count` | uint64 | Same-camera live-clone pairs, computed before typed eligibility |
| `triangulation` | typed status + diagnostics | Native outcome, mapped outcome if exact, and existing geometry/conditioning values |
| `refinement` | typed status + diagnostics | Native outcome, mapped outcome if exact, and existing diagnostics |
| `schur` | typed status + diagnostics | Native typed reduction status/stage, row/rank/conditioning/nonfinite evidence |
| `full_nis` | typed status + diagnostics | Attempted/not attempted, statistic, dof, threshold, and result when exposed |
| `full_outcome` | enum/status | Unified terminal outcome below |
| `full_outcome_mapping` | enum | `EXACT`, `UNMAPPED`, or `NOT_APPLICABLE` |
| `native_terminal_status` | string/status | Unmodified native terminal stage/status for auditability |
| `target_time_stereo_available` | boolean | Legacy attempt summary only; never substitutes for candidate-scoped evidence |
| `accepted_at_native_feature_gate` | boolean | Native full-row gate result before global proposal selection |
| `native_feature_row_count` | uint64 or typed unavailable | Native rows presented to and evaluated by that feature gate, including a rejected factor; unavailable if the native gate was not reached |
| `accepted_full_factor` | boolean | Full factor contributed to the accepted baseline proposal |
| `accepted_full_row_count` | uint64 | Rows contributed to that proposal |
| `accepted_full_row_status` | enum | Explicit contributed/not-contributed status after the estimator decision |
| `finalization_result` | native status | Existing terminal lifecycle result after the estimator decision |

Triangulation, refinement, Schur reduction, full-factor NIS, and accepted-row
status are distinct fields and distinct funnel maps; the unified outcome never
replaces them. Refinement retains native lambda, actual last-step norm, native
control epsilon, run count, and termination reason. Schur retains raw/reduced
row counts, descending singular values, numerical rank, reciprocal condition,
condition number, and zero/nonzero repair counters. Full NIS retains dof,
statistic, threshold, native stage, and decision. A native quantity that does
not exist is `NOT_EXPOSED` with reason `NOT_EXPOSED_BY_NATIVE_PATH`; no finite
placeholder is invented. In particular, `schur.raw_rows` and
`schur.rows_before_reduction` are typed `NOT_EXPOSED` when native Schur was not
attempted, and `native_feature_row_count` is typed `NOT_EXPOSED` when the
native full-row gate was not reached, rather than using zero as an availability
sentinel. Unreached initializer, Schur, and NIS native function/status/stage
strings use the explicit `NOT_EXPOSED_BY_NATIVE_PATH` code; empty strings are
not availability sentinels.

The exact unified outcomes are:

```text
FULL_ACCEPTED
INIT_ILL_CONDITIONED
INIT_TOO_NEAR_OR_BEHIND
INIT_TOO_FAR
INIT_NONFINITE
INIT_BASELINE_RATIO
REFINEMENT_FAILED
SCHUR_INSUFFICIENT_ROWS
SCHUR_RANK_DEFICIENT
SCHUR_ILL_CONDITIONED
SCHUR_NONFINITE
FULL_NIS_REJECTED
UNSUPPORTED_OR_INVALID_INPUT
```

When no exact mapping exists, `full_outcome` is unavailable,
`full_outcome_mapping` is `UNMAPPED`, and the native stage/status is retained.
Unmapped outcomes are ineligible in shadow admission. This is the typed-outcome
boundary in Sections 7.3--7.4 and 9.1.

## 7. Shadow candidate record

A candidate is one feature and one same-camera clone pair. It is read-only and
may be emitted even when ineligible.

| Field | Type | Required meaning |
|---|---|---|
| `candidate_key` | object | Complete globally unique canonical key from Section 2 |
| `source_observation_key` | object | Exact supporting source observation |
| `target_observation_key` | object | Exact supporting target observation |
| `supporting_target_time_stereo_observation_key` | object/status | Exact configured-mate support at the target timestamp |
| `source_observation` | object | Exact key plus raw and native-normalized pixel primitives |
| `target_observation` | object | Exact key plus raw and native-normalized pixel primitives |
| `supporting_target_time_stereo_observation` | object/status | Supporting stereo pixels or exact rejection reason |
| `camera_calibration` | object | Source/target/stereo camera, model, intrinsic ID, and extrinsic ID |
| `target_time_stereo_available` | boolean | Candidate-scoped configured-mate observation availability |
| `target_stereo_rejection_reason` | reason code | Exact missing configuration/observation/pixel primitive reason |
| `stereo_geometry_primitives` | object/status | Pair-scoped target/stereo raw and normalized pixel geometry availability |
| `full_outcome` | enum/status | Source typed failure; unmapped is ineligible |
| `target_bearing` | 3-vector/status | Measured target unit bearing for spatial diagnostics |
| `range_certificate` | object | Range fields below |
| `translation_certificate` | object | Translation fields below |
| `bearing_covariance` | object | Covariance fields below |
| `acute_regime` | object | Acute decision and angle below |
| `rho_trans` | number/status | Defined only for valid acute/covariance results |
| `rho_threshold` | number | Primary or explicitly identified sensitivity threshold |
| `static_quality_status` | enum | Existing non-NIS candidate checks |
| `eligible_before_group` | boolean | All per-feature pre-group checks pass |
| `terminal_reason` | reason code | First failing check in Section 10 ordering |

Raw and normalized pixel primitives distinguish native absence from an exposed
nonfinite value. Missing pixels use `NOT_EXPOSED_BY_NATIVE_PATH`; exposed
NaN/Inf pixels use `NONFINITE` with a source-, target-, or target-stereo-scoped
reason, and `stereo_geometry_primitives.status` is never `AVAILABLE` for such a
candidate.

`range_certificate` contains supporting stereo observation keys, identity and
timestamp audit results, nominal radial range, propagated standard deviation,
component confidence (0.99865), one-sided quantile, `d_LCB`, and a reason. It
does not exist without target-time stereo support.

`translation_certificate` contains the camera-center translation mean (3),
unmodified `P_t` (3x3), `gamma_P`, `P_t_cert` (3x3), maximum eigenvalue,
component confidence (0.99865), chi-square radius, `t_UCB`, covariance validity,
and a reason. All values come from the captured callback prior.

`acute_regime` contains `is_acute = (t_UCB < d_LCB)` and
`theta_trans_UCB`. The angle is unavailable when the strict inequality fails;
equality receives `TRANSLATION_NOT_ACUTE`. No saturated value is logged as a
bound.

`bearing_covariance` contains the 2x2 `Sigma_R`, minimum and maximum
eigenvalues, condition number, positive-definite/conditioning status, and
`sigma_bearing_worst = sqrt(lambda_min)`. Consequently
`rho_trans = theta_trans_UCB / sigma_bearing_worst`. The record also identifies
the exact pixel-noise/covariance configuration. These definitions freeze the
acute and worst-axis rules in Sections 8.6--8.9.

## 8. Shadow pair-group record

One group is identified by `(camera_id, source clone timestamp, target clone
timestamp)`. Group processing is deterministic and remains counterfactual in
T0.

| Field | Type | Required meaning |
|---|---|---|
| `pair_key` | object | Canonical group identity |
| `candidate_keys` | array | All candidates, sorted by complete canonical key |
| `eligible_candidate_keys` | array | Per-feature certified candidates |
| `raw_candidate_count` | uint64 | Candidate-pair population before group eligibility |
| `pair_group_feature_count` | uint64 | Distinct `(feature_id, detached_index)` population before the production minimum |
| `eligible_feature_count` | uint64 | Count before consensus |
| `shadow_only_small_group` | boolean | True exactly for `n=2` or `n=3`; false for `n<2` and `n>=4` |
| `group_size_status` | enum | `INSUFFICIENT_FOR_PAIR_GROUP` for `n<2`, `SHADOW_ONLY_SMALL_GROUP` for `n=2,3`, otherwise `PRODUCTION_MINIMUM_MET` |
| `contains_target_stereo_candidate` | boolean | At least one candidate in this group has exact target-time stereo evidence |
| `target_bearing_spatial_metrics` | object | Frozen coverage metric, threshold, and result |
| `rotation_stack_singular_values` | array[3] | Descending local-stack singular values |
| `rotation_stack_rank` | uint64 | Rank under the recorded threshold |
| `consensus` | object | Deterministic fit/trim/refit record below |
| `eligible_before_selection` | boolean | Certificate, count, rank, consensus, and spatial checks pass |
| `predicted_rotation_angle` | number/status | First score component |
| `predicted_information` | object/status | Orientation-information summary from the captured prior |
| `score` | object/status | Exact lexicographic components below; never contains NIS |
| `selection_role` | enum | `WINNER`, `FOREGONE_ELIGIBLE`, or `INELIGIBLE` |
| `foregone_reason` | reason code | `LOWER_PRE_NIS_SCORE` for eligible non-winners |
| `winner_shadow_nis` | object/status | Computed only for the frozen winner |
| `post_nis_count_rank_status` | object/status | Whether the winner would retain count/rank; failure means coast, not runner-up |

`consensus` records the prior-predicted source bearings, deterministic Wahba
fit, fitted rotation magnitude, per-feature post-fit angular residuals, exact
absolute and median/MAD thresholds, one trim/refit only, inlier keys/count/ratio,
post-refit residual summary, rank, spatial result, and terminal reason. It
requires `n >= 4`, at least `max(3, ceil(0.75*n))` inliers, and rank three. The
fitted rotation is telemetry only and is never an estimator update.

The v6 plan does not supply numerical absolute-angle, median/MAD, or spatial
coverage thresholds. Until an advisor-authorized threshold set is entered in
the decision log, T0 records the complete raw fit/residual/spatial metrics and
marks the corresponding threshold-dependent results `NOT_APPLICABLE` with
reason `THRESHOLD_SET_NOT_FROZEN`; it does not claim a passing consensus group.
No later than Session 2, the scientific authority must freeze the exact
values, boundary semantics, and threshold-set identifier. Offline T0 tooling
then deterministically re-evaluates the captured raw records under that set and
regenerates every consensus-survival, valid-group, winner, and foregone-group
field before the Sections 7.7--7.8 branch gate is evaluated. If no set is
frozen, the T1 engineering gate is not evaluable and cannot pass. Session 3
may implement only the same Session-2-frozen values; it does not choose them
(Sections 7.7--7.8, 8.10, and 15, Sessions 2--3).

The pre-NIS score is recorded in this exact order:

1. larger predicted relative-rotation angle;
2. larger minimum singular value of the whitened local rotation stack;
3. smaller maximum candidate `rho_trans`;
4. larger eligible-feature count;
5. ascending camera ID and timestamps as stable ties.

Exactly one eligible group is marked `WINNER` before any TurnSafe NIS. Every
other eligible group logs its feature count, complete score, and predicted
information. A winner failure never changes a runner-up's role. This implements
the diagnostic contract in Sections 7.5 and 8.10--8.12 without adding rows.

## 9. Callback decision and shadow summary

`baseline_decision` captures only behavior that the frozen estimator actually
executed:

```text
native decision/result
accepted full feature IDs
accepted full observation keys, when exposed
accepted full factor and row counts
state/covariance update attempted, accepted, committed
CommitPrecomputedUpdate call count
terminal finalization count/result
feature lifecycle/accepted-ID fingerprint
nominal-state fingerprint
covariance/deviation fingerprint
reset/recovery status observable in this callback
no-full-visual-update duration measured in sensor time
translation covariance scale from the captured prior
```

The duration state exists only in an active capture sink. It starts
`NO_PRIOR_ACCEPTED_FULL_UPDATE`, resets to zero only on the ordinary accepted
full-visual decision, advances from causal callback camera timestamps on
coasts, and never reads wall time. Nonfinite timestamps, callback/updater
timestamp mismatch, and regression against the last valid callback-time
frontier produce typed unavailable reasons and do not move either trusted
timestamp backward.

`shadow_summary` contains:

```text
eligible group count
frozen winner key or reason none exists
winner and runner-up pre-NIS scores
foregone eligible feature and information totals
winner shadow factor/row NIS counts
predicted winner orientation information
predicted mixed-proposal information
shadow terminal reason
```

The predicted mixed proposal is a read-only calculation. No shadow correction,
state preview, information term, or recovery label is allowed to influence the
baseline. Multi-callback recovery intervals and ground-truth errors are derived
offline, so future information never feeds a callback (Sections 5, 7.5, and
12.3).

## 10. Stable reason codes and ordering

Every candidate/group records the first terminal reason in this order, matching
Section 8.12:

```text
FULL_OUTCOME_INELIGIBLE
FULL_OUTCOME_UNMAPPED
OBSERVATION_OWNERSHIP_INVALID
CLONE_UNAVAILABLE
TARGET_STEREO_UNAVAILABLE
TARGET_STEREO_IDENTITY_MISMATCH
TARGET_STEREO_TIMESTAMP_MISMATCH
RANGE_GEOMETRY_INVALID
RANGE_COVARIANCE_INVALID
RANGE_LCB_UNAVAILABLE
RANGE_LCB_NONPOSITIVE
TRANSLATION_COVARIANCE_INVALID
TRANSLATION_NOT_ACUTE
BEARING_COVARIANCE_INVALID
RHO_TRANSLATION_EXCEEDED
STATIC_QUALITY_FAILED
THRESHOLD_SET_NOT_FROZEN
INSUFFICIENT_FEATURES
SPATIAL_COVERAGE_FAILED
CONSENSUS_FAILED
ROTATION_STACK_RANK_DEFICIENT
LOWER_PRE_NIS_SCORE
WINNER_NIS_REJECTED
WINNER_POST_NIS_COUNT_OR_RANK_FAILED
GLOBAL_SHADOW_VALIDATION_FAILED
NONE
```

`RANGE_LCB_UNAVAILABLE` means that target-time stereo support and the required
pair evidence exist, but an audited finite range LCB is unavailable. The
nested range-certificate record retains the exact unavailable or
`SHADOW_NOT_COMPUTED_*` cause. This reason is ineligible and is distinct from
an attempted-but-invalid covariance (`RANGE_COVARIANCE_INVALID`) and from a
computed nonpositive LCB (`RANGE_LCB_NONPOSITIVE`).

Additional native detail may be nested under the stage record, but must not
replace the stable reason. Full-factor NIS/outlier/nonfinite/pathological
failures remain visible as source outcomes and are never relabeled into an
eligible reason.

## 11. Deterministic digest membership

The parity digest includes every baseline field actually exposed at the frozen
tree in these classes:

- callback decision and commit/finalization counts;
- accepted full feature/observation IDs and row counts;
- terminal lifecycle ordering and result;
- stable nominal trajectory/state values;
- covariance/deviation values;
- deterministic frontend/updater outcome counts.

Capture-only parity compares those fields byte-for-byte after canonical
normalization. New shadow fields are separately digestible but cannot substitute
for baseline parity. The following are explicitly excluded:

- wall-clock timestamps and elapsed timings;
- host, user, PID/TID, absolute path, and logger prefix;
- build progress and unordered diagnostic text;
- optional runtime/power measurements;
- fields marked unavailable at the frozen baseline.

Each exclusion and unavailable baseline field must be listed in the baseline
manifest; no instrumentation change is made in Session 0 to fill a gap. This is
the disabled/capture parity contract in Sections 5.6 and 12.1.

## 12. Offline T0 aggregation

Development-only offline tooling may join callback telemetry to preregistered
event boundaries and permissible ground truth/reference data. It computes
absolute event counts, dominant funnel loss, target-range survival, acute and
rho survival, four-feature/rank/consensus survival, failure events containing a
valid group, foregone opportunity cost, and recovery outcomes. Denominators and
missing counts are always reported.

Telemetry or aggregates containing holdout identifiers are invalid before the
Session 7 unlock. The branch report must follow Sections 7.7--7.8 and may not
turn an unavailable field into evidence for later-stage implementation.

## 13. Passive event-ready extension

### 13.1 Versioning and activation

The base stream remains one `turnsafe.t0.v1` `run_header` followed by one
`turnsafe.t0.v1` `callback` per processed visual callback. When any Session-1E
capture flag is true, both records add:

```text
extensions.event_extension.schema = turnsafe.t0.event_extension.v1
extensions.event_extension.record_version = 1
```

Old readers may ignore `extensions`; no v1 field becomes newly required. The
three independent flags default false:

```text
capture_causal_imu_intervals
capture_outcome_association_keys
capture_group_bearing_provenance
```

A true extension flag requires passive T0 capture. The header binds their
resolved Boolean vector plus the integration, rotation, gyro-bias, clock,
association, ordering, image-cell, and calibration-hash convention IDs. The
configure/build descriptor binds both schema identifiers and the bytes of this
document. Capture off calls no extension provider and performs no extension
allocation.

### 13.2 One causal IMU interval per callback

When interval capture is enabled, each callback has exactly one
`causal_imu_interval`; `gyro_interval_id` equals the zero-based `callback_id`
within the opaque run namespace. `camera_frame_ids` is globally sorted and
unique. Every emitted frontend frame has only an additive interval reference,
and all frames from a stereo callback reference that same interval.

The camera-clock interval is the previous processed callback timestamp through
the current callback timestamp. The recorded mapping is:

```text
t_imu = t_camera + dt_CAMtoIMU
```

Both camera and IMU endpoints, duration, offset, bit-preserving timestamp keys,
units, and frame/convention strings are explicit. The first callback and the
first callback after an observed uninitialized-to-initialized boundary are
unavailable with typed reasons. Timestamp regression/nonfiniteness and an
unexposed reset likewise cannot fabricate an interval.

Only already-buffered causal `ImuData::wm` samples may be copied. The read-only
copy holds the native IMU-buffer lock only while validating native order and
copying the endpoint brackets/interior support; it never consumes, erases,
sorts, extrapolates, or retains a buffer pointer. Endpoint status is exactly
`EXACT_SAMPLE`, `LINEAR_INTERPOLATED`, or `UNSUPPORTED`. Linear interpolation
requires two finite, strictly ordered native samples. Missing either bracket
makes the interval unavailable.

Support fields record the first/last copied IMU timestamps, source sample
count, both endpoint/bracket records, maximum consecutive copied-sample gap,
coverage fraction, and endpoint-policy version. Large gaps remain measured
numbers, not a gate. The canonical knot arrays contain the exact interpolated
endpoints and native interior samples in strictly increasing time order.

Raw angular velocity is native `ImuData::wm` in rad/s. The read-only bias
snapshot is callback-entry `state->_imu->bias_g()` in the same bias convention;
the separately named bias-only series is `wm-bias_g`, not the fully calibrated
propagation-equivalent rate. Both series record maximum norm, time-weighted RMS
norm, time-weighted mean vector, trapezoidal integral of norm, trapezoidal
integral vector, SO(3) delta matrix, canonical scalar-last JPL quaternion,
principal rotation angle, and a typed axis. The method identifiers are:

```text
piecewise_linear_trapezoid.v1
native_gyroscope_frame_passive_left_exp_minus_omega_diagnostic.v1
raw_wm_minus_current_callback_bias_g.v1
```

For each chronological segment the diagnostic composes
`Exp(-0.5*(omega_k+omega_{k+1})*dt)` on the left. This freezes a passive
native-gyro-frame diagnostic direction only; it is not the fully calibrated
propagation-equivalent `R_GtoI` delta because gyro intrinsics, g-sensitivity,
and `R_GYROtoIMU` are intentionally not applied. All interpolation,
integration, SO(3), and
numeric serialization execute in a strict-floating-point diagnostic unit.

### 13.3 Causal output-association keys and offline joins

`outcome_association_keys` contains only callback/estimator metadata available
without reference data: callback ID/time, initialized and finite/valid status,
state timestamp, deterministic expected state/deviation/pose timestamp keys,
ordinary accepted-full factor count, ordinary full-update acceptance, camera
time since that acceptance, incomplete/nonfinite observations, and opaque
run/source/tree/snapshot/build/config/calibration identities. Actual output row
ordinals, pose-write status, reset status when unexposed, run completion, and
reference association remain typed offline-only or not-exposed values.

After ROS and the estimator writers close, the byte-bound tool
`turnsafe.offline_association.v1` performs unique-nearest deterministic joins.
State, deviation, and trajectory use the expected IMU-clock output timestamp
and maximum absolute residual 5.1e-6 s, covering the native five-decimal output
format. The permissible development reference is first opened offline and uses
that same expected IMU-clock timestamp with maximum absolute residual 0.01 s,
matching the frozen KAIST evaluation association. Duplicate/equidistant rows
are `AMBIGUOUS`; absence is `MISSING`. Rows include matched source ordinals,
line numbers, timestamps/keys, signed residuals, reference hash/time base, and
association version. No time-offset search, cropping, error, event ID, outcome,
or degradation label is produced.

### 13.4 Compact group-bearing provenance

Full provenance is emitted only for a same-camera source/target group whose
members have live clones, exact target-time stereo support, an exact typed
T1-source outcome, and at least two distinct canonical member identities.
`n=1` is omitted; `n=2`, `n=3`, and `n>=4` remain as structural populations
without acceptance meaning.

Groups sort globally by `(camera_id,source_timestamp_key,target_timestamp_key)`;
members sort by the complete v1 candidate key. Duplicates disable diagnostics
as ownership failures. Each group writes shared clone/calibration values once:
member keys/count, camera model/dimensions, canonical binary64 calibration and
extrinsic hashes, source/target clone timestamps, current and FEJ `R_GtoI`,
fixed `R_ItoC`, and supported-configuration status/reasons. The compact member
array then contains feature/detached identity, source/target/stereo observation
keys, raw pixels, native-normalized coordinates, camera-frame unit bearings,
target-stereo pixels, and threshold-free continuous pixel-center fractions.
Each conditional member value is a typed status/reason/value object; an
unavailable value omits `value`.

The extension intentionally contains no consensus, spatial acceptance, rank or
conditioning acceptance, winner, certificate, event, severity, outcome, T1,
T2, T3, or branch field. Its primitives are sufficient for a later offline
oracle to reconstruct target spatial inputs, relative-rotation residual inputs,
and the local rotation stack without selecting an oracle threshold here.

### 13.5 Failure isolation, validation, and storage

Expected unavailable support is record missingness, not a diagnostic exception.
Unexpected support-copy, allocation, interpolation, integration, SO(3),
metadata, provenance, serialization, sink, or publication failures cross the
existing no-throw boundary, latch only a fixed reason/saturating counter, and
disable diagnostics without retry or native-state effect.

The replay validator accepts old v1 streams unchanged and validates extension
streams in bounded memory. It reconciles one interval per callback, stereo
references, typed availability, knots/summaries, runtime identity/firewall,
group/member ordering and geometry, and post-close association coverage. After
validation and derivative generation, raw telemetry is compressed with
single-threaded zstd (`-T1 -9`) or deterministic gzip (`-9 -n`), archive-tested,
and decompressed through a bounded SHA-256/byte-count round trip. Raw bytes are
removed only after exact equality. Extension archives and derivatives are
separately hash-bound and remain outside the frozen estimator-parity digest.
