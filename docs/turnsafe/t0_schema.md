# TurnSafe T0 diagnostic schema

Status: frozen for Session 1 passive instrumentation

Schema identifier: `turnsafe.t0.v1`

Authority: v6 plan Sections 5.6, 7, 8.7--8.11, 12.1, 12.3, and 15

This schema describes evidence emitted by capture-only T0 diagnostics. It does
not authorize factor rows, proposal changes, new gates, fallback behavior, or
live-state mutation. A missing diagnostic is represented explicitly; estimator
behavior is never changed to manufacture a value.

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
5. Emit one terminal callback record even when the baseline coasts, rejects,
   resets, or a diagnostic computation is unavailable.
6. Prove disabled and capture-only decision parity against the frozen digest.

These requirements specialize the v6 plan Sections 5.1--5.6, 7, 12.1, and 15
(Session 1).

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
observation_key = (camera_id, timestamp_key, feature_id)
pair_key        = (camera_id, source_timestamp_key, target_timestamp_key)
candidate_key   = (pair_key, feature_id)
callback_key    = callback_index
```

`callback_index` is the zero-based order of baseline visual callbacks. Feature
and observation arrays preserve baseline order where that order is meaningful;
otherwise they use ascending canonical keys. The log must record which rule
applies to each array.

### Run header

| Field | Type | Required meaning |
|---|---|---|
| `schema` | string | Exactly `turnsafe.t0.v1` |
| `record_type` | string | `run_header` |
| `frozen_base_sha` | string | Frozen repository identity |
| `tree_sha` | string | Tree actually executed |
| `build_manifest_sha256` | string/status | Hash of the external baseline build manifest |
| `config_sha256` | string | Hash of the exact effective estimator configuration |
| `calibration_sha256` | string | Hash of the exact calibration input |
| `capture_mode` | string | `disabled` or `capture_only`; T0 has no active mode |
| `supported_configuration` | boolean | Result of the Section 4 production-envelope check, used only for telemetry eligibility |
| `unsupported_reasons` | array[string] | Stable sorted reason codes; empty when supported |
| `threshold_set_id` | string/status | Hash or immutable identifier for all shadow thresholds |
| `digest_contract_version` | string | Baseline digest contract used by the run |

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
| `full_track_attempts` | array | Section 6 records, in detached baseline order |
| `shadow_candidates` | array | Section 7 records, ordered by candidate key |
| `shadow_groups` | array | Section 8 records, ordered by pair key |
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
fmatrix_input_points
fmatrix_survivors
database_observations_written
terminal_msckf_tracks
tracks_with_valid_clone_pairs
full_outcome_counts_by_code
triangulation_outcome_counts_by_code
refinement_outcome_counts_by_code
schur_outcome_counts_by_code
full_nis_outcome_counts_by_code
accepted_full_factors
accepted_full_rows
shadow_pair_candidates
candidates_with_target_stereo
candidates_with_calibrated_range_lcb
candidates_with_calibrated_translation_ucb
candidates_translation_acute
candidates_passing_rho_trans
groups_n2_shadow_only
groups_n3_shadow_only
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
| `frame_timestamp_value/key` | timestamp pair | Exact sensor-frame identity |
| `previous_tracked_points` | uint64 | Points presented to temporal KLT |
| `klt_status_survivors` | uint64 | Points with passing KLT status before bounds/mask checks |
| `klt_status_rejections` | uint64 | KLT status failures |
| `out_of_bounds_rejections` | uint64 | Status survivors rejected by bounds |
| `in_bounds_survivors` | uint64 | Status survivors remaining after bounds |
| `mask_rejections` | uint64 | In-bounds points rejected by the existing mask |
| `mask_survivors` | uint64 | Points remaining after the existing mask |
| `fmatrix_input_points` | uint64 | Points submitted to the existing fundamental-matrix test |
| `fmatrix_inliers` | uint64 | Existing-test inliers |
| `fmatrix_rejections` | uint64 | Existing-test rejections |
| `database_observations_written` | uint64 | All observations written for the frame |
| `database_tracked_observations_written` | uint64/status | Writes attributable to the tracked funnel, if exposed |
| `database_new_observations_written` | uint64/status | Writes attributable to new detections, if exposed |
| `reset_too_few_points` | boolean | Existing low-point reset decision |
| `reset_native_reason` | string/status | Existing native reason, if exposed |
| `klt_error_summary` | object | Distribution defined below |
| `gyro_magnitude_rad_s` | number/status | Read-only frame-interval gyro magnitude |
| `gyro_integrated_rotation_rad` | number/status | Read-only integrated rotation over the exact camera interval |
| `blur_metric` | number/status | Existing or sensor-provided metric only |
| `exposure` | number/status | Recorded metadata with units named in `extensions` |
| `gain` | number/status | Recorded metadata with units named in `extensions` |

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
| `ordered_observation_keys` | array | Raw observations in baseline semantic order |
| `valid_clone_pair_count` | uint64 | Same-camera pairs whose source and target clones are live |
| `triangulation` | typed status + diagnostics | Native outcome, mapped outcome if exact, and existing geometry/conditioning values |
| `refinement` | typed status + diagnostics | Native outcome, mapped outcome if exact, and existing diagnostics |
| `schur` | typed status + diagnostics | Native typed reduction status/stage, row/rank/conditioning/nonfinite evidence |
| `full_nis` | typed status + diagnostics | Attempted/not attempted, statistic, dof, threshold, and result when exposed |
| `full_outcome` | enum/status | Unified terminal outcome below |
| `full_outcome_mapping` | enum | `EXACT`, `UNMAPPED`, or `NOT_APPLICABLE` |
| `target_time_stereo_available` | boolean | Same-feature stereo observation exists at candidate target time |
| `accepted_full_factor` | boolean | Full factor contributed to the accepted baseline proposal |
| `accepted_full_row_count` | uint64 | Rows contributed to that proposal |
| `finalization_result` | native status | Existing terminal lifecycle result after the estimator decision |

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
| `candidate_key` | object | Canonical pair key plus feature ID |
| `source_observation_key` | object | Exact supporting source observation |
| `target_observation_key` | object | Exact supporting target observation |
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
| `candidate_keys` | array | All candidates, sorted by feature ID |
| `eligible_candidate_keys` | array | Per-feature certified candidates |
| `eligible_feature_count` | uint64 | Count before consensus |
| `shadow_only_small_group` | boolean | True for `n=2` or `n=3`; such a group is never production-eligible |
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
the decision log, T0 records the raw fit/residual/spatial metrics and marks the
corresponding threshold-dependent results `NOT_APPLICABLE` with reason
`THRESHOLD_SET_NOT_FROZEN`; it does not claim a passing consensus group. Those
values require the Session 3 contract freeze before kernel/admission use and
before holdout access (Sections 8.10 and 15, Sessions 2--3).

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
