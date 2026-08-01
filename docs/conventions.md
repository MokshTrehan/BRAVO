# Estimator conventions

Status: **schema-3 CP1 addendum evidence passed; fresh signoff pending**
Pinned upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
Human reviewer: **Moksh Trehan (project-author self-review)**
Signoff date: **2026-07-27 (date-only attestation)**

No production estimator modification is permitted until the post-review
mathematical addendum passes and its replacement commit receives a fresh
attestation. Each decision cites the defining source file/function and the
test or later parity gate that protects it.

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
- Protecting test: `test_cp1_projection_jacobian` exercises the stored
  `G -> I -> C` transform chain; loader inversion remains in human review.

## Quaternion and rotation convention

- Quaternion coefficient order: JPL scalar-last `[qx,qy,qz,qw]`.
- Active/passive interpretation: stored rotation matrices are coordinate-frame
  transforms; for example `R_GtoI` converts global coordinates to IMU
  coordinates. This is not Hamilton quaternion multiplication.
- Rotation matrix direction: the direction is encoded by the symbol
  (`R_AtoB` maps coordinates from `A` to `B`).
- Exact retraction: for `a in R^3`, OpenVINS constructs
  `d(a)=[a/2,1]/sqrt(1+||a||^2/4)` and applies
  `q boxplus a=d(a) (x) q`. Its first-order rotation interpretation is
  `R(q boxplus a) ~= Exp(-a)*R(q)`.
- Fixed-chart differential: at absolute orientation increment `a`, the map
  from a fixed prior-chart perturbation to the local OpenVINS perturbation is
  `T_theta(a)=(I-0.5*[a]_x)/(1+||a||^2/4)`.
- Evidence: `ov_core/src/types/JPLQuat.h` and
  `ov_core/src/utils/quat_ops.h`.
- Protecting test: `CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction`.

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
- Protecting test: the CP1 retraction and projection-Jacobian tests.

## Residual and innovation

- Reprojection residual sign: `r = z_measured - z_predicted` in distorted
  pixel coordinates. OpenVINS stores `H=+d h/d delta`, so `d r/d delta=-H`
  and the linear measurement equation is `r ~= H*delta + noise`.
- Innovation sign: identical to the residual sign above.
- State increment sign: `delta_x = K*r`, followed directly by each type's
  `update(delta_x_block)` operation.
- Measurement whitening convention: the MSCKF baseline does not explicitly
  whiten. It assumes isotropic `sigma_px^2 I`; orthogonal nullspace and
  measurement-compression rotations preserve that covariance.
- Compression statistic convention: the Givens rotation preserves the full
  residual norm, but `measurement_compress_inplace` then truncates
  zero-Jacobian rows. The resized system preserves `Lambda` and `eta`, not
  `gamma`; retain pre-compression `gamma` (or the dropped squared residual)
  whenever NIS/objective parity needs it.
- Gate convention: `q=m-3`, threshold is the configured chi-square multiplier
  times the 95% `ChiSquared(q)` quantile, and rejection occurs only for a
  strict `chi2 > threshold`; equality is accepted.
- Evidence: `ov_msckf/src/update/UpdaterHelper.cpp`,
  `UpdaterMSCKF.cpp`, and `ov_msckf/src/state/StateHelper.cpp:EKFUpdate`.
- Protecting test: `CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences`
  asserts `r=z-h`, `H=dh/delta`, and `dr/delta=-H`; positive update injection
  is checked by the CP2 one-pass parity fixture.
  `CP1Compression.ProductionTruncationPreservesLambdaEtaButNotGamma` calls the
  actual production compressor and checks the retained/dropped statistics.

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
- Protecting test: human CP1 code review and the CP2 blockwise parity fixture.

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
- Protecting test: CP0 configuration/trajectory evidence and CP2 parity runs.

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
- Protecting test: the CP1 actual-OpenVINS `GLOBAL_3D` projection-Jacobian
  test; other representations remain outside the first Schur implementation.

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
- Mathematical scope: this mixed-FEJ matrix is not generally the derivative
  of the current residual function. One-pass Schur/nullspace equivalence only
  requires both paths to consume the same matrix. A Taylor/Newton claim for a
  second pass is blocked pending an FEJ-on golden fixture and separate review
  of the explicitly defined affine surrogate.
- Evidence: `ov_core/src/types/Type.h`, `PoseJPL.h`,
  `ov_msckf/src/state/Propagator.cpp`,
  `ov_msckf/src/update/UpdaterHelper.cpp`, and `UpdaterMSCKF.cpp`.
- Protecting test: planned FEJ one-pass regression fixture at CP2.

## Covariance and manifold reset

- Prior rank: exact `StateHelper::clone` augmentation copies a pose covariance
  and all cross-covariances, so a valid full prior may be positive
  semidefinite. Schur state updates use a possibly rectangular factor
  `P=L L^T` and the strictly positive-definite reduced matrix
  `I+L^T Lambda L`. They may not require `LLT(P)`, invert `P`, add clone noise,
  inject diagonal jitter, or silently clamp eigenvalues.
- Kalman covariance update form: baseline computes `M=P*H^T`,
  `S=H*P_small*H^T+R`, `K=M*S^-1` through LLT, and updates
  `P <- P-K*M^T`. It is not Joseph form.
- Symmetrization/PSD policy: only the upper triangle is updated and copied to
  the lower triangle. The baseline exits on a negative diagonal; it does not
  perform a full eigenvalue PSD check.
- Manifold reset Jacobian and timing: **the baseline applies no explicit
  covariance reset transport after nominal-state injection**. The primary
  policy is frozen to `G=I` through CP3 so CP2 covariance parity is not
  confounded. This is a baseline-parity approximation, not an exact manifold
  covariance statement. Exact selected-chart transport is
  `T(delta) P T(delta)^T` and remains a separately named ablation.
- Regularization policy: landmark nullspace elimination and
  `StateHelper::EKFUpdate` add no numerical regularization; LLT assumes a valid
  innovation covariance. Upstream feature refinement separately uses declared
  Levenberg--Marquardt damping and geometry rejection. Proposed elimination
  must reject and log conditioning failures rather than silently clamp.
- Evidence: `ov_msckf/src/state/StateHelper.cpp:EKFUpdate` and the type-specific
  `update` methods.
- Protecting test: CP1 full/nullspace/Schur covariance tests and
  `CP1Prior.SemidefiniteCloneAugmentationMatchesInnovationUpdate`; iterated
  final-covariance/reset tests remain blocked behind the fixed-two-pass gate.

## Fixed-two-pass candidate invariants — implementation blocked

These rules freeze a candidate affine surrogate and transaction policy. They
do not establish that the mixed-FEJ matrix is a Taylor derivative, and they do
not permit fixed-two-pass implementation before the FEJ-on and exact-chart
review gates in `docs/iterated_update_spec.md` are satisfied.

- Frozen predicted prior: snapshot `x^-`, `P^-`, FEJ values, observations,
  feature order, and configuration before pass 1. Both proposals are absolute
  corrections in `x(delta)=x^- boxplus delta`.
- Pass-1 behavior: gate and freeze the accepted feature set, then compute a
  working mean proposal without writing the live mean or covariance.
- Pass-2 behavior: reconstruct the working state from the frozen prior and the
  pass-1 absolute proposal; re-triangulate the fixed feature set and use the
  fixed-chart Jacobian/right-hand-side correction defined in
  `docs/iterated_update_spec.md`.
- Cross-pass policy: robust weights remain one; feature IDs, measurements,
  order, gate decisions, weights, and clone FEJ values are frozen. Any
  pass-2 feature failure rejects the complete second pass.
- Final commit: select pass 1 or pass 2 using the same-set pixel cost and
  frozen-prior objective, compute covariance once from `P^-`, and commit mean
  and covariance exactly once.
- Reset: use `G=I` only when measuring OpenVINS parity. Any mathematically
  exact covariance claim requires `T(delta) P T(delta)^T` transport.
- Evidence: `docs/iterated_update_spec.md`.
- Protecting tests: CP1 Schur/retraction tests and CP3 exactly-once/fallback
  integration tests.

## Signoff

Reviewer signoff means the cited code, equations, and tests agree. It does not
mean that the proposed algorithm or paper claim is accepted in advance.

- Reviewer: Moksh Trehan
- Reviewer relationship: project-author self-review
- Date: 2026-07-27 (date-only attestation, recorded 2026-08-01)
- Reviewed commit: `7288b4a2d09420266cfadd9983145e09fd4cf789`
- Remote verification: `origin/schurvio-lite/cp1-math` resolved to the reviewed
  commit on 2026-08-01.
- Exceptions: none stated
- Post-review note: later audit corrections are not covered by this signoff;
  their replacement commit requires a fresh attestation before production
  estimator mathematics is enabled.

## Post-review addendum lock

- Corrected/tested commit: `26588223597a2864149fd228e52caf569e7dfd12`
- Automated evidence: `results/immutable/cp1/automated/cp1_math_20260801T203838596581924Z-g26588223597a`
- `SHA256SUMS` SHA-256: `320c8cda3a98a4036718e34d499c909a8ac70dd0f8d9e71041a6e3fe134d42c3`
- Automated result: 7 tests passed, 0 failures/errors/disabled; clean source
- Fresh reviewer: pending
- Fresh signoff date: pending
- Fresh reviewed commit: pending
- Fresh exceptions: pending
