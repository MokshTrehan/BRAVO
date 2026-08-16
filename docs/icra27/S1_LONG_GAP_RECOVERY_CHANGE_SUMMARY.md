# S1 long-gap recovery: implementation summary

This document summarizes the S1 changes developed for the KAIST `rotation`
camera-outage failure. It describes the production code at science commit
`2751bcdc0fae25c993b3224dc5fa40aaab6571d7`; the result/reporting snapshot is
tagged `icra27-rotation-outage-kaist11-v1` at commit
`6081fa5db11291f25e0c5ec0b396064fda50e38a`.

The pinned upstream OpenVINS checkout was not modified. These changes belong
only to S1, and they do not alter Schur elimination mathematics.

## Problem addressed

KAIST `rotation` contains a 13.441997 s stereo-camera outage while IMU data
continues. Frozen S1 preserved attitude but accumulated about 7.9 m of
translation error. When images resumed, retained landmarks were still visible,
but ordinary linearized EKF updates could not recover the grossly displaced,
overconfident state.

The implemented response is a default-off, fail-closed nonlinear
relocalization boundary. It activates only after an initialized estimator sees
a camera gap longer than 0.5 s. It never globally relaxes feature, NIS, MSCKF,
or SLAM gates.

## Production algorithm changes

### 1. New long-gap relocalization core

[`LongGapRelocalizer.h`](../../ov_msckf/src/core/LongGapRelocalizer.h) and
[`LongGapRelocalizer.cpp`](../../ov_msckf/src/core/LongGapRelocalizer.cpp) add a
VioManager-independent retained-landmark PnP solver.

The solver:

- accepts immutable global 3-D landmark positions and raw left-camera pixels;
- sorts correspondence IDs and fixes OpenCV's RNG for deterministic RANSAC;
- seeds with EPNP RANSAC using 1,000 iterations and 0.999 confidence;
- discards the raw RANSAC membership, reprojects every candidate, and applies
  the fixed 2 px residual bound;
- performs two fixed-set iterative refinements with all-point reclassification;
- returns a pose only after every prospective geometry and consistency gate
  passes.

Hard acceptance gates are:

- at least 12 supplied/valid correspondences;
- at least 12 final inliers and an inlier ratio of at least 0.70;
- finite pose/residuals and positive inlier depth;
- final maximum reprojection error no greater than 2 px;
- non-collinear 3-D support;
- inlier image spans of at least 25% in both axes;
- no more than 5 degrees disagreement from the read-only IMU orientation
  prediction.

A rejected result retains its reason and diagnostics but returns an identity
pose and cannot mutate estimator state.

### 2. Default-off recovery configuration

[`VioManagerOptions.h`](../../ov_msckf/src/core/VioManagerOptions.h) parses and
validates the recovery parameters. Defaults keep recovery disabled. When
enabled, [`VioManager.cpp`](../../ov_msckf/src/core/VioManager.cpp) requires the
exact frozen contract:

| Parameter | Frozen value |
|---|---:|
| Camera-gap trigger | 0.5 s |
| Maximum recovery frames | 10 |
| Minimum correspondences/inliers | 12 / 12 |
| Minimum inlier ratio | 0.70 |
| Maximum reprojection error | 2 px |
| Minimum image span | 0.25 per axis |
| Maximum IMU orientation disagreement | 5 deg |
| Consecutive accepted poses | 3 |
| Position-consensus radius | 0.10 m |
| Reset attitude sigma | 2 deg |
| Reset position sigma | 0.10 m |
| Reset velocity sigma | 0.05 m/s |

The validated KAIST configuration is
[`config/kaist_vio_rotation_robustness/estimator_config.yaml`](../../config/kaist_vio_rotation_robustness/estimator_config.yaml).

### 3. Recovery state machine

[`VioManager.h`](../../ov_msckf/src/core/VioManager.h) and
[`VioManager.cpp`](../../ov_msckf/src/core/VioManager.cpp) add four explicit
phases:

1. `TRACKING`: ordinary frozen S1 behavior.
2. `REACQUIRING`: normal propagation/update is blocked; retained-map PnP is
   evaluated without changing live state.
3. `WARMUP`: a verified fresh epoch is building its first five clones.
4. `FAILED`: ten unsuccessful recovery frames terminate the attempt without a
   hidden state repair.

Each accepted PnP pose is still non-mutating. Three consecutive accepted poses
must lie within 0.10 m of their component-wise median before a commit is
allowed. This consensus gate is what licenses resetting velocity to zero.

### 4. Reanchor commit and fresh feature epoch

[`VioManagerHelper.cpp`](../../ov_msckf/src/core/VioManagerHelper.cpp) performs
the declared recovery commit:

- creates a fresh state at the last consensus-verified global pose;
- resets velocity to zero;
- preserves fixed calibration and the last trusted IMU biases;
- applies conservative pose/velocity covariance floors while retaining the
  bias covariance;
- deletes old clones, landmarks, and stale feature measurements;
- reconstructs and seeds a new KLT frontend from the commit image;
- immediately emits the recovered state and its full 3x3 position covariance;
- resumes full visual updates after five new clones are available.

The immediate commit row keeps the original 0.10 s post-gap evaluation anchor
and first-resumed position NEES measurable. The commit plus four propagation-
only warm-up rows intentionally have no legacy full-update timing rows; the
harness permits exactly those five event-bound omissions and nothing broader.

### 5. Read-only IMU orientation prediction

[`Propagator.h`](../../ov_msckf/src/state/Propagator.h) and
[`Propagator.cpp`](../../ov_msckf/src/state/Propagator.cpp) add a read-only
orientation prediction used only as a PnP consistency check. It does not write
state, covariance, or propagator history.

### 6. Callback/diagnostic lifetime safety

The recovery commit replaces the state and tracker during a camera callback.
[`TurnSafeCallbackInstaller.h`](../../ov_msckf/src/core/TurnSafeCallbackInstaller.h),
[`VioManager.cpp`](../../ov_msckf/src/core/VioManager.cpp), and the TurnSafe
tests were updated so the callback envelope retains the old tracker long enough
for safe polling and resolves the current state at callback end. Frontend epoch
reset rebinds only the tracker observer; it does not reinstall or modify the
already-frozen updater observer.

### 7. Machine-readable recovery evidence

Every run emits a fixed recovery contract plus events for trigger, attempt,
accepted pose, consensus, commit, commit covariance, warm-up completion,
failure, and final summary. Attempt records explicitly say `state_unchanged=1`;
mutation is represented only by the single commit event.

`ros1_serial_msckf` also reports final activation, commit, failure, and epoch
counts. This allows target runs to require exactly `1/1/0/epoch1` and all
non-triggering regressions to require exactly `0/0/0/epoch0`.

## Experiment and artifact tooling

[`rotation_robustness_trial.py`](../../scripts/icra27/rotation_robustness_trial.py)
adds an append-only scored/capture harness with:

- clean-source and compiled-input provenance checks;
- executable/library/config/launch/bag hashing;
- isolated ROS/process-group lifecycle management;
- ground-truth access only after the estimator closes;
- exact input-accounting, recovery-event, state/covariance/timing, tail, ATE,
  RPE, gap-transform, and NEES validation;
- explicit negative statuses for infrastructure, estimator, output,
  evaluation, and provenance failures;
- no overwrite or replacement of failed attempts.

[`kaist_geometry_bundle.py`](../../scripts/icra27/kaist_geometry_bundle.py)
retains the complete subscriber-gated raw geometry stream and derives aligned
PLY/SVG products. These products are deliberately labeled as current active
SLAM state or transient MSCKF/track visualizations—not persistent dense maps.

## Tests added or extended

- 18 deterministic synthetic tests exercise the public PnP path and every
  geometry/rejection boundary.
- TurnSafe tests cover frontend observer rebinding and callback lifetime.
- 23 harness tests cover process cleanup, provenance, recovery contracts,
  timing omissions, covariance/NEES binding, numeric thresholds, and atomic
  artifact publication.
- 19 geometry tests cover trajectory association, suffix proofs, alignment,
  sparse topics, nonfinite data, symlink safety, and deterministic outputs.

## Validated result

At the frozen science commit:

- KAIST `rotation` passed all target endpoints in three byte-identical runs;
- full translation ATE improved from 4.257743 m to 0.087813 m;
- cross-gap translation error improved from 7.878093 m to 0.035133 m;
- first-resumed position NEES was 8.696218 against an 11.345 limit;
- all other ten KAIST sequences had zero recovery activation and exact frozen
  state/deviation/TUM hashes;
- all eleven exact-trajectory capture replays retained raw geometry and a
  deterministic atlas.

See [`ROTATION_OUTAGE_ROBUSTNESS_REPORT.md`](ROTATION_OUTAGE_ROBUSTNESS_REPORT.md)
for the full evidence and claim boundary.

## Deliberately unchanged

- Pinned upstream OpenVINS source, binary, and native behavior.
- Schur reduction and rank/conditioning logic.
- Normal MSCKF and SLAM gates.
- Initializer, calibration, feature budget, clone count, exact-header KAIST
  pairing, and visual-pass count.
- Dataset bytes, timestamps, and ground truth.

## Known limitations before a generic robustness claim

- Stage frontend/reset construction before the state swap to make recovery
  strongly exception-atomic.
- Validate retained bias covariance as symmetric positive definite, not only
  finite.
- Strengthen the C++ runtime contract for calibration/threading assumptions.
- Require raw IMU samples to bracket the read-only orientation endpoint.
- Define bounded behavior for a second outage during warm-up and terminal
  recovery buffers.
- Validate on independent datasets and outage populations; the KAIST target
  was development data.

The next campaign therefore treats EuRoC, locally available TUM-VI, and a fresh
KAIST rerun as a pinned-system comparison between original OpenVINS and S1. It
will not repair or retune original OpenVINS.
