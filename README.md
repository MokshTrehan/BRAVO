# BRAVO: Bridgeable Re-Anchoring with Atomic Verification for Camera-Dropout Recovery in Visual–Inertial Odometry

> This repository is an anonymous research artifact. The project description
> intentionally omits author names, affiliations, personal webpages, and other
> identifying information. Please preserve that anonymity during review.

BRAVO is an OpenVINS-based recovery method for complete camera interruptions.
When images return after a gap, ordinary local visual updates may resume while
the estimated pose remains displaced from its established reference frame.
BRAVO instead joins surviving feature-track identities to active 3-D landmarks
already held by the estimator, recovers the returning pose in that frame, and
checks a separately constructed replacement state before changing the live
estimator.

An accepted recovery is applied once as a complete state transition. A rejected
recovery leaves the covered state unchanged and records a typed reason before
fallback. The method does not require descriptor retrieval, place recognition,
or a separate map database.

## Method at a glance

1. Detect a prolonged, post-initialization camera interruption while retaining
   the last trusted filter state and buffering IMU measurements.
2. Match returning 2-D observations to resident 3-D landmarks through surviving
   KLT track identities.
3. Estimate the returning pose with PnP/RANSAC in the estimator's existing
   global frame.
4. Require fixed support, geometry, reprojection, gyro-agreement, and temporal-
   consensus checks.
5. Construct and validate a detached replacement state while the live state
   remains read-only.
6. Commit the complete replacement once, or refuse it without a partial state
   change. Rebuild visual history after acceptance; resume through the fallback
   path after refusal.

The checked recovery configuration uses the following fixed requirements:

| Check | Requirement |
|---|---:|
| Camera-gap trigger | 0.5 s |
| Correspondences / RANSAC inliers | at least 12 / 12 |
| Inlier ratio | at least 0.70 |
| Reprojection threshold | 2 px |
| Inlier image span | at least 25% on both axes |
| PnP–gyro rotation disagreement | at most 5 degrees |
| Temporal consensus | 3 consecutive valid proposals |
| Position-consensus radius | 0.10 m |
| Decision budget | 10 returned frames |

These thresholds were selected empirically during development and then held
fixed for the reported evaluations. They are not claimed to be universally
optimal.

## Results reported in the BRAVO paper

The anonymous paper reports:

- Output after all 26 controlled KAIST camera blackouts, with median 2–8 s
  position error of **0.230 m**, compared with **0.591 m** for hold-and-resume
  and **0.821 m** for recovery-off.
- Completion on **11/11** nominal, unmasked KAIST sequences, compared with
  **10/11** for the pinned upstream OpenVINS baseline. On the ten sequences
  where recovery remains inactive, no recovery-induced degradation is observed.
- For the native 13.415 s KAIST camera interruption, a commit 63 ms after camera
  return with 0.034 m position error and 0.40 degree orientation error. The
  recovery-off comparison first publishes at 7.88 m and 1.31 degrees.
- Across 63,779 checked updates and 8,315 confirmed injected transaction faults,
  zero observed violations of the checked live-state invariants.

These results establish a proof of concept within the tested operating range.
They do not establish universal robustness, full-state covariance consistency,
or performance under physical/asynchronous camera failures.

### Artifact snapshot

The current `main` snapshot contains the validated re-anchoring core and the
original rotation-outage study. The BRAVO paper also reports later integrated
fallback, controlled-mask, cross-dataset, and fault-injection work developed on
subsequent research branches. The numbers above summarize the BRAVO paper; they
should not be read as a claim that every reported experiment is reproducible
from this snapshot alone.

## Repository guide

- `ov_msckf/src/core/LongGapRelocalizer.*` — deterministic retained-landmark
  PnP/RANSAC recovery core.
- `ov_msckf/src/core/VioManager*` — recovery state machine, detached-state
  checks, commit, and visual-history reset for the original re-anchoring core.
- `config/kaist_vio_rotation_robustness/` — fixed recovery configuration used
  by the original validated KAIST study.
- `project/rotation_robustness_serial.launch` — serial ROS 1 replay launch.
- `scripts/icra27/` — experiment, evaluation, integrity, and artifact tooling.
- `docs/icra27/S1_LONG_GAP_RECOVERY_CHANGE_SUMMARY.md` — implementation details.
- `docs/icra27/ROTATION_OUTAGE_ROBUSTNESS_REPORT.md` — frozen initial study,
  evidence boundary, exclusions, and limitations.
- `project/evidence/rotation_robustness/` — committed evidence index for the
  original rotation-outage study.

The repository contains both the recovery work and inherited OpenVINS modules.
Recovery is default-off unless explicitly enabled by configuration.

## Build

BRAVO retains the upstream OpenVINS ROS 1/ROS 2 build layout. For a ROS 1
catkin build, place the repository inside a workspace and run:

```bash
mkdir -p catkin_ws/src
mv BRAVO catkin_ws/src/
cd catkin_ws
catkin build
source devel/setup.bash
```

Dockerfiles for supported ROS distributions are included at the repository
root. For example, from the directory containing `catkin_ws`:

```bash
docker build -t bravo \
  -f catkin_ws/src/BRAVO/Dockerfile_ros1_20_04 catkin_ws
```

General OpenVINS prerequisites and build guidance are available in the
[upstream documentation](https://docs.openvins.com/getting-started.html).

## Replaying the recovery configuration

After building and sourcing the workspace, the serial launch accepts an adapted
KAIST bag and explicit output paths:

```bash
roslaunch src/BRAVO/project/rotation_robustness_serial.launch \
  bag:=/path/to/adapted_kaist.bag \
  candidate_config:="$PWD/src/BRAVO/config/kaist_vio_rotation_robustness/estimator_config.yaml" \
  path_state:=/path/to/output/state_estimate.txt \
  path_std:=/path/to/output/state_deviation.txt \
  path_time:=/path/to/output/timing.txt
```

Datasets and large replay artifacts are not bundled. The experiment tooling
performs additional provenance, input-accounting, and post-run checks; consult
the frozen report before interpreting a replay as paper evidence.

## Scope and limitations

- Recovery depends on persistent track identities and enough retained landmarks
  remaining visible after camera return.
- The reported configuration targets synchronized pinhole/radtan cameras; the
  paper records a typed unsupported-projection refusal for TUM-VI fisheye input.
- PnP supplies pose, while the evaluated reconstruction uses a declared
  zero-velocity restart and fixed diagonal pose/velocity priors.
- Atomicity here is a checked software state-transition property, not a claim of
  statistical consistency.
- Threshold sensitivity, full-state NEES/NIS, physical outages, asynchronous
  camera return, and broader held-out evaluation remain future work.

## Upstream and license

BRAVO is derived from [OpenVINS](https://github.com/rpng/open_vins), pinned in
[`UPSTREAM_REVISION`](UPSTREAM_REVISION). The upstream project and its authors
retain their original attribution.

This repository is distributed under the GNU General Public License v3.0. See
[`LICENSE`](LICENSE) for the complete terms. The anonymous paper citation will
be added after the review process.
