# Estimator conventions

Status: **blocking draft**
Pinned upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
Human reviewer: **unassigned**
Signoff date: **not signed**

No production estimator modification is permitted while any blocking field
below is unresolved. Each decision must cite the defining source file/function
and the test that protects it.

## Frames and transforms

- World frame and gravity direction: the global frame is `G`; `+z_G` is up.
  The stored gravity vector is `[0,0,+g]` and is subtracted during propagation.
- IMU/body frame: the active pose is `(q_GtoI, p_IinG)`. `R_GtoI`
  maps coordinates from global to IMU; `p_IinG` is the IMU origin expressed in
  global.
- Camera frame(s): calibration is `(R_ItoC, p_IinC)`. `R_ItoC` maps IMU
  coordinates to camera coordinates; `p_IinC` is the IMU origin expressed in
  camera.
- Transform notation and direction: a global feature is projected as
  `p_FinI = R_GtoI*(p_FinG-p_IinG)` and
  `p_FinC = R_ItoC*p_FinI+p_IinC`.
- Composition order: matrix products act right-to-left on column vectors.
  Kalibr `T_imu_cam = (R_CtoI,p_CinI)` is inverted by the configuration loader
  before storage.
- Evidence: `ov_msckf/src/state/State.h`,
  `ov_msckf/src/update/UpdaterHelper.cpp:get_feature_jacobian_full`, and
  `ov_msckf/src/core/VioManagerOptions.h`.
- Protecting test: planned `test_transform_conventions` and
  `test_projection_jacobian` at CP1.

## Quaternion and rotation convention

- Quaternion coefficient order: JPL scalar-last `[qx,qy,qz,qw]`.
- Active/passive interpretation: stored rotation matrices are coordinate-frame
  transforms; for example `R_GtoI` converts global coordinates to IMU
  coordinates. This is not Hamilton quaternion multiplication.
- Rotation matrix direction: the direction is encoded by the symbol
  (`R_AtoB` maps coordinates from `A` to `B`).
- Exp/Log definition: orientation error is left-multiplicative in JPL form,
  equivalent to `R_true ~= Exp(-delta_theta)*R_hat`.
- Evidence: `ov_core/src/types/JPLQuat.h` and
  `ov_core/src/utils/quat_ops.h`.
- Protecting test: planned `test_exp_log_conventions` and
  `test_transform_conventions` at CP1.

## Error-state and perturbation

- Orientation perturbation side: left-multiplicative,
  `q_true ~= [0.5*delta_theta,1] (x) q_hat`.
- Error-state definition: a pose error is `[delta_theta, delta_p]`; the IMU
  error is `[delta_theta, delta_p, delta_v, delta_bg, delta_ba]`.
- State increment/retraction: quaternions use normalized left multiplication;
  position, velocity, and biases use additive increments.
- Error-state block dimensions: pose 6, IMU 15. Nominal storage is pose 7 and
  IMU 16 because the unit quaternion stores four coefficients.
- Evidence: `ov_core/src/types/JPLQuat.h`, `PoseJPL.h`, and `IMU.h`.
- Protecting test: planned `test_error_state_retraction` at CP1.

## Residual and innovation

- Reprojection residual sign: `r = z_measured - z_predicted` in distorted
  pixel coordinates.
- Innovation sign: identical to the residual sign above.
- State increment sign: `delta_x = K*r`, followed directly by each type's
  `update(delta_x_block)` operation.
- Measurement whitening convention: the MSCKF baseline does not explicitly
  whiten. It assumes isotropic `sigma_px^2 I`; orthogonal nullspace and
  measurement-compression rotations preserve that covariance.
- Evidence: `ov_msckf/src/update/UpdaterHelper.cpp`,
  `UpdaterMSCKF.cpp`, and `ov_msckf/src/state/StateHelper.cpp:EKFUpdate`.
- Protecting test: planned `test_residual_increment_sign` and one-pass parity
  fixture at CP1/CP2.

## State and covariance ordering

- Core IMU state ordering: `[theta, p, v, bg, ba]`, 15 error dimensions.
- Calibration state ordering: optional IMU intrinsics, camera-to-IMU time
  offset, and per-camera extrinsics/intrinsics follow the IMU in constructor
  insertion order. Disabled calibration variables are not in covariance. The
  frozen SchurVIO-Lite CP0 profile disables camera extrinsic, intrinsic, and
  time-offset calibration and uses the known EuRoC values in every mode.
- Clone ordering: clones are appended through `StateHelper::clone`; each clone
  is `[theta,p]` and its `Type::id()` defines its live covariance offset.
- Feature/SLAM state ordering: in-state landmarks are appended dynamically and
  indexed through their `Type::id()`. Transient MSCKF landmarks are not filter
  variables.
- Covariance indexing mechanism: never assume a fixed absolute offset beyond a
  type's local block. Use the ordered `Type` vector and each `id()/size()`.
- Evidence: `ov_msckf/src/state/State.cpp`, `State.h`, `StateHelper.cpp`, and
  `ov_core/src/types/Type.h`.
- Protecting test: planned `test_state_block_order` at CP1.

## Units and gravity

- Position/velocity/acceleration units: metres, metres/second, and
  metres/second-squared.
- Angular-rate units: radians/second; orientation perturbations are radians.
- Timestamp units and clock: double-precision seconds. State/clone timestamps
  are camera-clock time; `t_imu = t_cam + dt_CAMtoIMU`.
- Gravity magnitude and signed world vector: EuRoC config uses `g=9.81`; the
  stored vector is `[0,0,+9.81]` and propagation subtracts it.
- Evidence: `ov_msckf/src/state/State.h`, `Propagator.h`, `Propagator.cpp`, and
  `config/euroc_mav/estimator_config.yaml`.
- Protecting test: planned `test_gravity_and_time_conventions` at CP1.

## Feature and projection model

- Pixel/normalized measurement definition: project `p_FinC` to
  `[x/z,y/z]`, apply the configured camera-model distortion, and compare with
  measured image pixels.
- Camera distortion convention: dispatched through the configured
  `CamRadtan` or `CamEqui` model; pixel intrinsics/distortion may be in-state.
- Feature parameterizations in scope: the frozen EuRoC baseline uses
  `GLOBAL_3D` for transient MSCKF features and
  `ANCHORED_MSCKF_INVERSE_DEPTH` for in-state SLAM features. CP1 Schur parity
  targets transient `GLOBAL_3D` first.
- Anchor-frame convention: anchored features are expressed in the selected
  camera clone; anchor transforms follow the `G -> I -> C` convention above.
- Triangulation/refinement convention: `FeatureInitializer` triangulates using
  current clone/calibration estimates and optionally refines inverse depth by
  Gauss-Newton/LM before updater Jacobian construction.
- Evidence: `config/euroc_mav/estimator_config.yaml`,
  `ov_core/src/types/LandmarkRepresentation.h`,
  `ov_core/src/feat/FeatureInitializer.cpp`, and `UpdaterHelper.cpp`.
- Protecting test: planned `test_projection_jacobian` and
  `test_feature_parameterization` at CP1.

## FEJ and linearization points

- FEJ-enabled variables and stored points: `use_fej=true` for EuRoC. Every
  `Type` stores current and FEJ values. Clone creation copies both; the clone's
  FEJ then remains frozen.
- Residual evaluation point: current clone, feature, extrinsic, and intrinsic
  estimates.
- Jacobian evaluation point: observing clone and transient feature position use
  FEJ geometry; camera extrinsics and intrinsics use current estimates.
- Clone/feature FEJ policy: propagation advances both the active IMU current
  and FEJ value; a new clone freezes those values. A transient MSCKF feature's
  FEJ point is initialized to its current triangulation for the update.
- Baseline parity quirk: with FEJ enabled, `p_FinCi` is recomputed at FEJ for
  the normalized-projection derivative, but `uv_norm` is not recomputed before
  the distortion Jacobian. One-pass parity must preserve this mixed evaluation
  until a separately tested ablation changes it.
- Evidence: `ov_core/src/types/Type.h`, `PoseJPL.h`,
  `ov_msckf/src/state/Propagator.cpp`,
  `ov_msckf/src/update/UpdaterHelper.cpp`, and `UpdaterMSCKF.cpp`.
- Protecting test: planned FEJ one-pass regression fixture at CP2.

## Covariance and manifold reset

- Kalman covariance update form: baseline computes `M=P*H^T`,
  `S=H*P_small*H^T+R`, `K=M*S^-1` through LLT, and updates
  `P <- P-K*M^T`. It is not Joseph form.
- Symmetrization/PSD policy: only the upper triangle is updated and copied to
  the lower triangle. The baseline exits on a negative diagonal; it does not
  perform a full eigenvalue PSD check.
- Manifold reset Jacobian and timing: **the baseline applies no explicit
  covariance reset transport after nominal-state injection**. CP2 parity must
  preserve this. The iterated-update policy remains blocking until its exact
  final reset decision is derived and reviewed.
- Regularization policy: the baseline has no declared hidden regularization;
  LLT assumes a valid innovation covariance. Proposed code must reject or log
  conditioning failures rather than silently clamp.
- Evidence: `ov_msckf/src/state/StateHelper.cpp:EKFUpdate` and the type-specific
  `update` methods.
- Protecting test: planned full/Schur covariance parity and PSD tests at CP1;
  iterated final-covariance/reset tests at CP3.

## Iterated-update invariants

- Definition of the frozen predicted prior: **TBD — blocking**
- Pass-1 nominal update without covariance commit: **TBD — blocking**
- Pass-2 re-triangulation/relinearization policy: **TBD — blocking**
- Robust weight, gate, and feature-set policy across passes: **TBD — blocking**
- Final covariance/reset exactly once: **TBD — blocking**
- Evidence: `docs/iterated_update_spec.md` once complete
- Protecting test: **TBD**

## Signoff

Reviewer signoff means the cited code, equations, and tests agree. It does not
mean that the proposed algorithm or paper claim is accepted in advance.

- Reviewer:
- Date:
- Reviewed commit:
- Exceptions:
