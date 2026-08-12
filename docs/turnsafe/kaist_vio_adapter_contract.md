# KAIST VIO deterministic adapter contract

Status: **Session 0.5 contract freeze**

Authorization: `APPROVE_SESSION_0_5_KAIST_DOWNLOAD_AND_ADAPTER`

Scope: public KAIST VIO ingestion and frozen-baseline replay only. This
contract does not authorize estimator mathematics, T0 instrumentation,
TurnSafe factors, fallback behavior, frontend policy, estimator gating, or
feature-lifecycle changes.

## 1. Normative sources and precedence

The dataset source is the official `url-kaist/kaistviodataset` repository,
resolved and pinned for this session at
`ae672591bab119651be1aa9fc3db5af3e599f49a`. The normative metadata files at
that commit are:

```text
README.md
config/cam-imu.yaml
config/imu-params.yaml
config/trans-mat.yaml
config/vins_fusion_config/realsense_stereo_imu_config.yaml
config/vins_fusion_config/left.yaml
config/vins_fusion_config/right.yaml
```

Core metadata byte identities are:

| File | SHA-256 |
|---|---|
| `README.md` | `0d73b4efeb93571fab2761985246c67a8b055b9c04d0eb1bd68b8f82400fddab` |
| `License` | `3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986` |
| `config/cam-imu.yaml` | `81f162c0c7983c463c58246aea64cd864d4a44c17ac8525f884ad82509873d02` |
| `config/imu-params.yaml` | `0bf07d9e23c624ec3af51caec26b673069dbfcc05c76740e45ef48b813f56a29` |
| `config/trans-mat.yaml` | `ae3d00d6022ca520044bb84d190a74a61aa2cd5e86cc6005615b33c686404108` |

The public archive and the direct bag evidence admitted in Session 0.5 are
byte-identified separately:

| Evidence | SHA-256 |
|---|---|
| official `kaist_vio_dataset.zip` archive | `1f3fe695b773342a7a1df227e651a7566802ea2e7a8d5ef1af49f0c112c09e1f` |
| central-directory-only `ARCHIVE_SAFETY_AUDIT.json` | `d7647058217ff809cfd1be4d6d2e1d7644c2f80fc37d0285e12ab59946a21a30` |
| extracted `rotation/rotation_fast.bag` | `d77f19421a271e8cb0475971ee3475d8954e19b1b081b758966ba81f3e57ebbb` |

The archive safety audit records 21 regular-file/directory members, the exact
eleven-bag layout, unique normalized and case-folded paths, and no absolute,
drive-qualified, or ambiguous paths. It is an archive-structure check; it is
not a substitute for per-bag semantic audit.

Repository frame, state, timestamp, and camera conventions are fixed by
`docs/conventions.md`. Repository loader behavior at the Session 0.5 entry
commit `fdf34496e4f5212c79ab65d6210eed11e1437dd1` is implementation ground
truth, specifically:

- `ov_core/src/utils/opencv_yaml_parse.h` for inverse-key transform loading;
- `ov_msckf/src/core/VioManagerOptions.h` for stored camera calibration and
  the single camera-to-IMU time offset;
- `ov_core/src/cam/CamRadtan.h` for radtan coefficient order; and
- `ov_msckf/src/ros1_serial_msckf.cpp` for accepted ROS message types and
  offline stereo dispatch.

For calibration, frames, units, and noise, the pinned metadata and repository
conventions remain normative. For the archive's recorded topic names, ROS
types, message counts, and timestamps, the byte-identified bag and its
deterministic audit take precedence over README prose. Thus the directly
observed raw infrared profile below is authoritative for the official archive;
the README's compressed topics remain a separately detected fallback profile.
If a bag matches neither exact profile or disagrees with another fixed mapping,
the adapter fails closed and records the discrepancy. It must not infer a new
profile, frame, unit, clock correction, calibration, or noise model from
trajectory quality.

## 2. Input and output message contract

The official README declares compressed infrared streams at nominal 30 Hz, one
IMU stream at nominal 100 Hz, and ground truth at nominal 50 Hz. Direct
inspection of the byte-identified `rotation_fast.bag`, however, found raw
infrared `sensor_msgs/Image` connections. The adapter therefore supports
exactly two mutually exclusive source profiles:

| Profile | Role | Exact source topic and type | Exact target topic and type | Action |
|---|---|---|---|---|
| `official_raw` (primary) | left infrared | `/camera/infra1/image_rect_raw`, `sensor_msgs/Image` | `/turnsafe/kaist/infra1/image_raw`, `sensor_msgs/Image` | validate and retopic unchanged |
| `official_raw` (primary) | right infrared | `/camera/infra2/image_rect_raw`, `sensor_msgs/Image` | `/turnsafe/kaist/infra2/image_raw`, `sensor_msgs/Image` | validate and retopic unchanged |
| `documented_compressed` (fallback) | left infrared | `/camera/infra1/image_rect_raw/compressed`, `sensor_msgs/CompressedImage` | `/turnsafe/kaist/infra1/image_raw`, `sensor_msgs/Image` | validate and decode only |
| `documented_compressed` (fallback) | right infrared | `/camera/infra2/image_rect_raw/compressed`, `sensor_msgs/CompressedImage` | `/turnsafe/kaist/infra2/image_raw`, `sensor_msgs/Image` | validate and decode only |

Both profiles require these source-only/common streams:

| Role | Exact source topic and type | Exact target | Action |
|---|---|---|---|
| IMU | `/mavros/imu/data`, `sensor_msgs/Imu` | `/mavros/imu/data`, `sensor_msgs/Imu` | exact serialized pass-through |
| ground truth | `/pose_transformed`, `geometry_msgs/PoseStamped` | none | require and audit source; omit output |

Exactly one complete camera profile must be present. Any raw/compressed camera
mixture, either profile with a missing side, a wrong ROS type, or no recognized
profile fails before an output is advertised. The selected profile name is
recorded as `source_profile` in the machine-readable adaptation and source
audit reports.

Every color-camera connection, including the raw stream observed in the
archive and the compressed form named by the README, is outside the stereo
estimator input and is inventoried but omitted. All other nonrequired
connections are likewise inventoried and omitted. An output bag containing
ground truth, color, or another unreviewed sensor topic is not an accepted
estimator input.

All camera messages from the uniquely selected profile and all IMU messages
are retained one-for-one. In particular, a camera message without an exact
stamp match on the other side remains in the adapted bag. The adapter never
drops, duplicates, interpolates, resamples, rate-controls, reorders, or retimes
a retained sensor message. Ground truth is the sole required source stream
intentionally omitted from output. Missing required streams, an unreadable
message, or a zero-message required stream is a hard failure. Nominal rates are
validation metadata, not permission to synthesize samples: measured counts,
duration, and rates are recorded, and a departure is reported rather than
silently repaired.

### 2.1 Authoritative raw-image pass-through

For each `official_raw` infrared `sensor_msgs/Image`, the adapter requires
`width=640`, `height=480`, `encoding="mono8"`, `is_bigendian=0`, `step=640`,
and exactly `307200` row-major payload bytes. Its header timestamp must be
nonzero. The original message object is written under the TurnSafe-owned target
topic without reconstruction or field mutation. Its serialized ROS message
bytes, `seq`, `stamp`, `frame_id`, pixels, bag-record timestamp, and position in
the retained stream must therefore be semantically identical to the source.
Any dimension, encoding, endian, step, or payload-length mismatch rejects the
bag.

### 2.2 Documented compressed-image fallback

For each `documented_compressed` infrared `sensor_msgs/CompressedImage`:

1. validate a nonzero ROS header timestamp and a nonempty payload;
2. require `CompressedImage.format` to identify exactly one JPEG or PNG codec
   and require the payload signature to agree;
3. decode with the pinned OpenCV `imdecode(..., IMREAD_UNCHANGED)` path, without
   resizing, debayering, rectifying, undistorting, contrast adjustment, color
   conversion, or lossy re-encoding;
4. require a `640 x 480`, two-dimensional, unsigned 8-bit decoded image;
5. emit `sensor_msgs/Image` with `encoding="mono8"`, `is_bigendian=0`,
   `step=640`, and the decoded row-major pixel bytes; and
6. copy `seq`, `stamp`, and `frame_id` from the source header exactly.

An empty or ambiguous format, a codec other than JPEG/PNG, a signature
mismatch, decode failure, or dimension/depth/channel mismatch raises a visible
adapter error and leaves no accepted partial output. A future codec or pixel
format requires a reviewed contract change.

For either profile, the source topic name contains `image_rect_raw`; that name
does not authorize another geometric operation. The official nonzero radtan
calibration remains active, so the adapter performs no rectification or
distortion change.

### 2.3 Ordering, timestamps, and semantic identity

Input traversal follows the deterministic order yielded by the pinned ROS1 bag
reader over the required topics. The adapter does not impose a new tie-breaker
or reorder equal-record-time messages. A decoded image replaces its compressed
source at the same position in that traversal. Each output message uses exactly
the source bag-record timestamp; every retained header timestamp is unchanged.
The adapter must not substitute wall time or header time for bag-record time.
Bag-record and header timestamps must each be strictly increasing within every
required topic, and every required header stamp must be nonzero.

Determinism is judged by canonical semantic-stream digests over ordered logical
roles, bag-record nanoseconds, header fields, and either exact serialized
pass-through bytes or normalized decoded `mono8` pixels. Logical roles, rather
than source topic names, permit the deliberate retopicking and compressed-to-raw
conversion to be compared. Two adaptations of the same input with the same
pinned decoder must produce the same digests. Raw-source cameras and IMU must
also preserve their recorded-message semantic digests exactly. Container-byte
equality may be reported but is not a substitute for semantic equality.

### 2.4 Exact-header pairing audit and serial replay seam

Stereo audit pairs only the exact intersection of the two cameras' integer
header-stamp sets. It never uses ordinal position, nearest-neighbor tolerance,
or bag-record-time proximity to choose a pair. From the original absolute
bag-record-time differences of the exact pairs, the report records
minimum/maximum/sum/mean statistics and the count at or above `20000000 ns`; it
also records the exact pair count and the count and stamp list unmatched on
each side. The `20000000 ns` threshold is an audit boundary, not an adapter
rejection threshold. No header or bag-record timestamp is rewritten. A bag
with no exact common header stamp fails closed.

The directly audited `rotation_fast.bag` has `3761` raw infrared messages on
each side, `3760` exact-header pairs, and one unmatched header stamp per side.
Among the exact pairs, `22` have an original bag-record difference at or above
`20000000 ns`; the maximum is `53519649 ns` and the observed p99 is
approximately `15.09 ms`. These are evidence for the exact-header seam and are
not permission to filter, retime, or impose a record-time acceptance window.

The legacy serial reader's record-time first-forward window cannot represent
those target-time pairs. KAIST replay therefore uses the dedicated
`kaist_vio_exact_header_stereo` input-plumbing mode. The executable default is
`false`; only the dedicated fixed KAIST launch sets it to `true`. When enabled,
the reader first builds one immutable, topic-filtered view for the authorized
bag interval, validates the camera headers, and joins exact equal stamps. Each
pair is dispatched once at the earlier of its two filtered record ordinals.
The full view makes both already-recorded pair members addressable at that
anchor without copying, rewriting, or synthesizing a measurement.

Earlier anchoring leaves the subsequent IMU records available to process the
queued camera callback. For a full-duration replay, the loop drains the full
filtered view instead of stopping at the last camera record, so a later real
IMU message can drain the final camera callback; no synthetic IMU or camera
sample is permitted. Unmatched camera messages stay in the adapted bag and are
counted, but do not enter a stereo callback. Their exclusion is explicit
exact-header incompatibility accounting, not a generic dropped/unpaired
warning. The runtime must emit exactly one selection summary whose exact-pair
count, unmatched counts, count at or above `20 ms`, and maximum record
difference equal the adapted-bag audit. A separate terminal summary accounts
for pairs queued to the existing callback, pairs removed by the already-frozen
baseline frequency policy, pairs proven processed, camera decode failures, and
the terminal pending-pair count. Exact-header replay requires synchronous
subscriber processing and fails unless the terminal queue is empty and no
camera-processing callback is active. Only after that drain proof may the
runtime report `processed_pairs = queued_pairs`.

This default-off path is a KAIST offline serial-dispatch seam only. It supplies
the existing stereo callback with two existing images at one exact target time
and does not authorize any estimator mathematics, update, gating, calibration,
frontend, feature-lifecycle, timestamp, image, or IMU behavior change. It
requires two distinct raw camera input topics, fails closed on malformed or
nonunique pair identity, and is not accepted when a runtime `path_gt` parameter
exists.

## 3. Camera model and calibration

Both cameras are `pinhole` plus `radtan`, at `640 x 480`. OpenVINS stores the
camera vector as

```text
[fx, fy, cx, cy, k1, k2, p1, p2]
```

and `CamRadtan` consumes the final four entries in Kalibr/OpenCV order
`[k1, k2, p1, p2]`. No coefficient permutation or sign change is permitted.

| Camera | `[fx, fy, cx, cy]` | `[k1, k2, p1, p2]` |
|---|---|---|
| cam0 / infra1 | `[380.9229090195708, 380.29264802262736, 324.68121181846755, 224.6741321466431]` | `[0.006896928127777268, -0.009144207062654397, 0.000254113977103925, 0.0021434982252719545]` |
| cam1 / infra2 | `[380.95187095303424, 380.3065956074995, 324.0678433553536, 225.9586983198407]` | `[0.007044055287844759, -0.010251485722185347, 0.0006674304399871926, 0.001678899816379666]` |

Intrinsics and extrinsics are fixed during every TurnSafe-compatible KAIST
baseline run. The adapter does not estimate or alter them.

The separate fixed calibration file must name
`/turnsafe/kaist/infra1/image_raw` for cam0 and
`/turnsafe/kaist/infra2/image_raw` for cam1. The official compressed source
names are never configured as estimator inputs.

### 3.1 Official `T_cam_imu` direction

The official Kalibr matrices are homogeneous transforms from IMU coordinates
to camera coordinates:

```text
p_Ci = R_ItoCi p_I + p_IinCi.
```

Thus each official `T_cam_imu` is
`[R_ItoCi, p_IinCi; 0, 1]`. The exact pinned values are:

```text
cam0 T_cam_imu =
[-0.04030123999740945  -0.9989998755524683    0.01936643232049068   0.02103955032447366 ]
[ 0.026311325355146964 -0.020436499663524704 -0.9994448777394171   -0.038224929976612206]
[ 0.9988410905708309   -0.0397693113802049    0.027108627033059024 -0.1363488241088845  ]
[ 0.0                    0.0                    0.0                   1.0                 ]

cam1 T_cam_imu =
[-0.03905752472566068  -0.9990498568899562    0.019336318430946575 -0.02909273113160158 ]
[ 0.025035478432625047 -0.020323396666370924 -0.9994799569614147   -0.03811090793611019 ]
[ 0.99892328763622     -0.03855311914877835   0.02580547271309183  -0.13656684822705098 ]
[ 0.0                    0.0                    0.0                   1.0                 ]
```

The repository loader requests `T_imu_cam`, which denotes the inverse
camera-to-IMU transform. When only `T_cam_imu` is present, the specialized
parser calls `Inv_se3` before returning it. `VioManagerOptions` then converts
that camera-to-IMU matrix into the stored `(R_ItoC, p_IinC)` representation.
Consequently the final stored rotation and translation must numerically equal
the rotation and translation in the official `T_cam_imu` above. The adapter
must not invert, rotate, or translate any measurement to compensate for this
loader behavior.

### 3.2 Stereo transform and baseline

The official cam1 `T_cn_cnm1` maps cam0 coordinates into cam1 coordinates:

```text
T_C0toC1 =
[ 0.9999992248836708     6.384241340452582e-05  0.0012434452955667624  -0.049960282472300055]
[-6.225102643531651e-05  0.9999991790958949     -0.0012798173093508036 -5.920119010064575e-05]
[-0.001243525981443161   0.0012797389115975439   0.9999984079544582    -0.00014316003395349448]
[ 0.0                    0.0                      0.0                     1.0                  ]
```

It must agree with

```text
T_C0toC1 = T_ItoC1 * inverse(T_ItoC0)
```

to a maximum absolute element error of `1e-9`. Its translation norm is the
frozen stereo baseline `0.049960522658277336 m`. The determinant and
orthonormality of every rotation must also pass a `1e-9` maximum-error check.
Failure rejects the calibration; no projection, polar repair, or baseline
replacement is permitted.

## 4. Camera/IMU clock contract

The repository convention is

```text
t_imu = t_cam + dt_CAMtoIMU.
```

The pinned official calibration provides:

| Camera | `timeshift_cam_imu` (s) |
|---|---:|
| cam0 | `-0.029958533056650416` |
| cam1 | `-0.030340187355085417` |

They are not equal. Their signed difference is

```text
dt_cam1 - dt_cam0 = -0.000381654298435001 s
```

with magnitude `381.654298435001 microseconds`.

OpenVINS has one global `calib_camimu_dt` and its loader explicitly uses the
first camera's value. Session 0.5 therefore freezes

```text
dt_CAMtoIMU = -0.029958533056650416 s
```

from official cam0. With that global value, cam1 is represented with a
`+0.000381654298435001 s` offset relative to its own official calibration.
This is a declared single-offset approximation justified only by the pinned
official dual-camera calibration and the repository's first-camera policy; it
is not an empirical correction or a claim that the clocks are identical.

No timestamp is shifted in the adapter. Camera time remains the state/clone
timestamp, and the fixed calibration supplies the camera-to-IMU relation. No
per-camera correction, online time-offset calibration, time-offset sweep, or
trajectory-based sign selection is permitted. If later bag evidence
contradicts the official sign convention or requires a larger/different
correction, processing fails closed until this contract is independently
revised.

## 5. IMU units, noise, and gravity

`/mavros/imu/data` is passed through without axis remapping, sign changes,
gravity removal, filtering, interpolation, or covariance synthesis. OpenVINS
consumes angular velocity in radians/second and linear acceleration in
metres/second-squared. Those are the ROS `sensor_msgs/Imu` SI units and the
repository conventions. The published IMU measurement frame is the estimator
frame `I`; a bag whose frame semantics contradict the official camera/IMU
calibration is unsupported.

The exact continuous-time values in pinned `config/imu-params.yaml` map
directly, without rate multiplication, division, squaring, or unit conversion:

| OpenVINS field | Frozen value | Unit |
|---|---:|---|
| `accelerometer_noise_density` | `0.00333388` | `m s^-2 / sqrt(Hz)` |
| `accelerometer_random_walk` | `0.00047402` | `m s^-3 / sqrt(Hz)` |
| `gyroscope_noise_density` | `0.00005770` | `rad s^-1 / sqrt(Hz)` |
| `gyroscope_random_walk` | `0.00001565` | `rad s^-2 / sqrt(Hz)` |

The official nominal IMU rate is `100.0 Hz`; it validates the recorded stream
but does not rescale these continuous-time parameters. No published
scale/misalignment or gravity-sensitivity matrices are supplied by this
metadata, so the baseline uses identity IMU intrinsic maps, zero gyro gravity
sensitivity, and disables their online calibration. Any nonidentity correction
requires a new evidence-backed contract.

Pinned official VINS-Fusion metadata gives `g_norm: 9.805`. The KAIST baseline
therefore freezes `gravity_mag: 9.805 m/s^2`. Per repository convention the
stored global gravity vector is `[0, 0, +9.805]` and propagation subtracts it.
The adapter neither adds nor removes gravity from accelerometer samples.

## 6. Frozen estimator-facing configuration and initialization

The adapted bag is accepted only with a separately named KAIST configuration
that satisfies the v6 production envelope:

```text
use_fej = true
use_stereo = true
max_cameras = 2
up_msckf_landmark_elimination = schur
up_msckf_max_visual_passes = 1
feat_rep_msckf = GLOBAL_3D
camera model = CamRadtan
calib_cam_extrinsics = false
calib_cam_intrinsics = false
calib_cam_timeoffset = false
```

The stock `config/kaist_vio/estimator_config.yaml` uses transient anchored
inverse depth, leaves intrinsic calibration enabled, and defaults to nullspace
elimination. It must not be silently presented as the frozen SchurVIO-Lite
A0 configuration, and it remains unmodified for provenance.

The adapter performs no estimator initialization. Runs start at the beginning
of each complete bag (`bag_start=0`, full duration) and use the repository's
ordinary visual-inertial initializer. The existing KAIST initialization policy
is frozen for the separate baseline configuration:

```text
try_zupt = false
init_window_time = 2.0
init_imu_thresh = 0.60
init_max_disparity = 5.0
init_max_features = 50
init_dyn_use = false
```

No start-time search, manual pose, truth-derived attitude/position/bias,
sequence-specific parameter, or retry with altered initialization is allowed.
Failure to initialize is a reported run failure, not an adapter-repair trigger.

## 7. Ground truth and evaluation boundary

Ground truth is never an estimator input. In particular:

- the estimator-input bag contains no `/pose_transformed` connection;
- the estimator node has no `path_gt` parameter, including an empty one;
- `initialize_with_gt` is never reachable in a KAIST evidence run;
- live ground-truth display is disabled; and
- ground truth cannot select bag start, configuration, adapter behavior,
  initialization, a run retry, or an output segment.

The official README states that the body/VI-sensor offset has already been
applied to `/pose_transformed`; `config/trans-mat.yaml` is reference-only and
must not be applied again. Ground truth may be extracted in a separate process
after estimator completion, or the tracked TUM-format KAIST VIO references may
be used after their identity is recorded. Extraction uses the
`PoseStamped.header.stamp`, position in metres, and scalar-last quaternion
`[qx,qy,qz,qw]`; nonfinite values or a zero quaternion reject the reference.

Evaluation is post-run with fixed `SE(3)` alignment and no scale correction.
Timestamp association uses `--t_max_diff 0.01`; there is no temporal-offset
search or outcome-dependent cropping. The frozen command shapes are:

```bash
evo_ape tum GT_TUM EST_TUM --t_max_diff 0.01 \
  --pose_relation trans_part --align

evo_rpe tum GT_TUM EST_TUM --t_max_diff 0.01 \
  --pose_relation trans_part --delta 1.0 --delta_unit m \
  --all_pairs --pairs_from_reference --align

evo_rpe tum GT_TUM EST_TUM --t_max_diff 0.01 \
  --pose_relation angle_deg --delta 1.0 --delta_unit m \
  --all_pairs --pairs_from_reference --align
```

Every sequence is reported, including initialization failure, crash,
nonfinite output, missing association, or evaluator failure. No poor interval
is removed.

## 8. Fail-closed and unsupported cases

The following are outside this adapter contract:

- a ROS2 bag, non-ROS1 serialization, encrypted archive, or damaged bag;
- a camera layout other than exactly one complete `official_raw` or
  `documented_compressed` profile, including a mixture of their topic markers;
- raw images other than the exact `640 x 480 mono8`, `is_bigendian=0`,
  tightly-packed layout, or compressed images that do not decode to
  `640 x 480 mono8` through the fixed JPEG/PNG path;
- approximate stereo timestamp pairing or any timestamp rewrite;
- filtering an unmatched camera message or rejecting an exact pair solely
  because its original bag-record difference reaches `20 ms`;
- image rectification, undistortion, resizing, photometric enhancement, or
  camera-axis remapping;
- IMU unit conversion, axis remapping, saturation repair, filtering, or
  interpolation;
- online camera intrinsic/extrinsic/time-offset calibration;
- a different gravity or IMU noise set selected from trajectory performance;
- rolling-shutter/readout compensation (the pinned metadata does not specify a
  readout model in this contract);
- use of color or ground truth by the estimator, or any runtime `path_gt` in
  the dedicated exact-header KAIST launch; and
- sequence-name, future-frame, final-error, or evaluator feedback at runtime.

Zero/invalid timestamps, nonfinite IMU angular-velocity or linear-acceleration
components, nonmonotonic required-topic record or header time, nonfinite ground
truth position/quaternion, a zero ground-truth quaternion, inconsistent
calibration transforms, unsupported compressed formats, mixed or incomplete
source profiles, and a bag with no exact common camera header stamp are hard
failures. Nonfinite optional IMU orientation/covariance fields are counted in
the audit because the runtime does not consume them; they are neither mutated
nor silently hidden. The implementation must emit a visible reason and leave
no partial output advertised as compatible.

## 9. Required conformance evidence

Before any full-sequence baseline run, tests must prove:

1. the metadata commit is exactly
   `ae672591bab119651be1aa9fc3db5af3e599f49a`;
2. both source profiles are uniquely detected, their source and output
   topic/type allowlists are exact, and mixed/incomplete profiles fail closed;
3. a raw source image is retopicked with identical serialized message bytes,
   header, pixels, bag-record time, and retained order;
4. a compressed source image produces one pixel-identical decoded target with
   preserved header, bag-record time, and retained order;
5. corrupt, unsupported, wrong-size, wrong-depth, wrong-channel, wrong-encoding,
   wrong-step, and wrong-payload-length images fail closed;
6. IMU serialized semantics and timestamps are unchanged, while ground truth
   is source-validated and intentionally absent from output;
7. two independent adaptations have identical canonical semantic digests;
8. unmatched header stamps, including equal-count streams with different stamp
   sets, are preserved and reported; exact pairs at and above `20 ms` record
   difference are retained, and exact-pair/unmatched/skew audits are unchanged
   across adaptation;
9. the exact official transforms load to the expected stored
   `(R_ItoC, p_IinC)` values, and the stereo composition/baseline checks pass;
10. radtan coefficients reach `CamRadtan` in `[k1,k2,p1,p2]` order;
11. the cam0 global offset, its sign equation, and the quantified cam1 disparity
   are asserted;
12. the four official IMU noise scalars and `9.805` gravity load exactly;
13. the estimator-input bag and resolved estimator parameters contain no
    ground-truth input; and
14. a full-duration smoke replay enables the otherwise default-off exact-header
    seam, emits exactly one runtime selection summary whose accounting fields
    equal the adapted audit, drains the full filtered view and camera queue,
    reports `processed_pairs = queued_pairs` and `pending_pairs = 0`, and completes
    without any estimator- or evaluator-driven change to the frozen adapter,
    serial plumbing, calibration, or configuration.

After the smoke checkpoint, hash the adapter, decoder/runtime dependencies,
configuration, bags, and references before the ordered eleven-sequence run.
Generated bags and evidence remain untracked under the authorized dataset and
`artifacts/turnsafe/` roots. Only this contract, the minimal adapter/harness,
its deterministic tests, and the separate fixed configuration may be tracked.

The Session 0.5 hard stop remains in force after these checks: no estimator
mathematics, T0 instrumentation, factor/fallback behavior, frontend-policy
change, estimator gate, or feature-lifecycle change is authorized.
