# OpenVINS estimator update map

Pinned source: `69488123ed9362dd44b6f28e7f4680abbff1442b`
Purpose: CP0 read-only estimator map; only the separately declared serial-runner
lifecycle/reproducibility patch is present, and estimator math is unchanged

## End-to-end path

For deterministic dataset and parity work, prefer the serial ROS1 executable
`ov_msckf/src/ros1_serial_msckf.cpp`. It disables subscriber multithreading and
dispatches bag messages in recorded order.

The live path is:

1. `ROS1Visualizer::callback_inertial`, `callback_monocular`, or
   `callback_stereo` convert ROS messages and order camera input behind IMU
   input in camera-clock coordinates.
2. `VioManager::feed_measurement_imu` buffers IMU data in the propagator and
   initializer.
3. `VioManager::feed_measurement_camera` calls `track_image_and_update`, which
   runs KLT/descriptor tracking, attempts initialization, and enters
   `do_feature_propagate_update`.
4. `Propagator::propagate_and_clone` integrates the IMU, propagates covariance,
   advances the camera timestamp, and augments a pose clone.
5. `UpdaterMSCKF::update` cleans feature tracks, builds clone poses,
   triangulates/refines landmarks, constructs each feature system, eliminates
   the landmark, gates it, stacks accepted rows, compresses measurements, and
   calls `StateHelper::EKFUpdate`.
6. `ROS1Visualizer::publish_state` publishes update-rate pose/path;
   `visualize_odometry` publishes fast propagated odometry. `ov_eval`'s
   `pose_to_file` records the trajectory.

## Source map

| Stage | Defining source and function |
|---|---|
| ROS camera/IMU ingestion | `ov_msckf/src/ros/ROS1Visualizer.cpp`: subscriber setup and callbacks |
| Deterministic bag runner | `ov_msckf/src/ros1_serial_msckf.cpp` |
| Front-end boundary | `ov_msckf/src/core/VioManager.cpp:track_image_and_update` |
| KLT/descriptor tracking | `ov_core/src/track/TrackKLT.cpp`, `TrackDescriptor.cpp` |
| IMU buffering/propagation | `VioManager::feed_measurement_imu`; `ov_msckf/src/state/Propagator.cpp` |
| Covariance propagation | `ov_msckf/src/state/StateHelper.cpp:EKFPropagation` |
| Initialization dispatch | `ov_init/src/init/InertialInitializer.cpp` |
| Static/dynamic initialization | `ov_init/src/static/StaticInitializer.cpp`; `ov_init/src/dynamic/DynamicInitializer.cpp` |
| Clone augmentation | `StateHelper::clone`, `StateHelper::augment_clone` |
| Triangulation/refinement | `ov_core/src/feat/FeatureInitializer.cpp` |
| Full feature residual/Jacobian | `ov_msckf/src/update/UpdaterHelper.cpp:get_feature_jacobian_full` |
| Representation chain rule | `UpdaterHelper::get_feature_jacobian_representation` |
| Landmark nullspace projection | `UpdaterHelper::nullspace_project_inplace` |
| Chi-square gating and stacking | `ov_msckf/src/update/UpdaterMSCKF.cpp:update` |
| Global measurement compression | `UpdaterHelper::measurement_compress_inplace` |
| Mean/covariance update | `ov_msckf/src/state/StateHelper.cpp:EKFUpdate` |
| Update-rate output | `ROS1Visualizer::publish_state`; `ov_eval/src/utils/Recorder.h` |
| IMU-rate propagated output | `ROS1Visualizer::visualize_odometry`; `Propagator::fast_state_propagate` |

## Baseline visual-update ordering

`UpdaterMSCKF::update` performs these observable stages:

1. Remove measurements without live clones and reject too-short tracks.
2. Build current camera clone poses.
3. Triangulate/refine each transient feature.
4. Build `(H_f,H_x,r)` for one feature.
5. Apply fixed-column Givens nullspace projection to remove `H_f`.
6. Gate the projected feature using marginalized state covariance and isotropic
   pixel noise.
7. Append accepted feature rows while retaining `Hx_order`.
8. Givens-compress the global state measurement.
9. Form `R=sigma_px^2 I` and call the EKF update once.

The nullspace helper removes exactly `H_f.cols()` leading rows after Givens
rotations; it does not estimate numerical rank. For the frozen transient
`GLOBAL_3D` mode, this means three eliminated feature columns.

## Schur insertion seam and parity constraints

The narrowest safe one-pass seam is in
`ov_msckf/src/update/UpdaterMSCKF.cpp`, immediately after
`UpdaterHelper::get_feature_jacobian_full` and before
`UpdaterHelper::nullspace_project_inplace`.

The first implementation should leave triangulation, residual/Jacobian
construction, feature order, `Hx_order`, chi-square gating, global measurement
compression, pixel noise, and `StateHelper::EKFUpdate` unchanged. It must match
the baseline's fixed three-column elimination and FEJ behavior. This isolates
the elimination method as the only variable for CP2.

The seam is not authorization to code yet. The exact reduced covariance and
information equations, conditioning policy, and equivalence target remain
blocking in `docs/iterated_update_spec.md` until CP1.

## Known baseline properties that tests must capture

- EuRoC uses FEJ, stereo KLT, 11 clones, transient `GLOBAL_3D` MSCKF features,
  and in-state anchored inverse-depth SLAM features.
- Residual is measured minus predicted distorted pixel.
- Nullspace projection occurs per feature; gating happens before global
  measurement compression.
- Pixel noise is scalar/isotropic and is instantiated only after compression.
- Covariance uses the subtractive `P-K(PH^T)^T` update and symmetrization, not
  Joseph form, with no explicit post-injection reset Jacobian.
- The FEJ distortion-Jacobian evaluation contains the mixed current/FEJ point
  documented in `docs/conventions.md`; parity code must not accidentally fix it.
- The serial bag runner, not the live asynchronous node, is the reference path
  for deterministic parity evidence.
