# CP2 one-pass production contract

Status: **frozen for implementation; evidence pending**
CP1 authorization commit: `8d80f483752411d34a3bc4c1ff6330b3a5c0fef3`
Pinned upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`

This contract is the hard boundary for CP2. It permits a selectable one-pass
square-root Schur reduction for transient `GLOBAL_3D` MSCKF landmarks. It does
not permit fixed-two-pass behavior, a new covariance-update convention,
regularization, or changes to the visual measurement model.

## Frozen modes and seam

- `nullspace` retains the unchanged OpenVINS Givens reduction and update math
  and remains the default. A shared read-only update preflight, defined below,
  applies identically to both CP2 modes.
- `schur` is the CP2 candidate and must be explicitly selected.
- The configuration key is `up_msckf_landmark_elimination`. It defaults to
  `nullspace`, is loaded with `parse_config(..., required=false)`, and accepts
  only `nullspace` or `schur`; every other value is a startup error. The
  hash-anchored CP0/EuRoC YAML files are not modified merely to add this key.
  CP2 evidence selects the mode through a dedicated ROS launch override.
- Before any feature track is processed, startup validation requires a known
  mode, `GLOBAL_3D` when `schur` is selected, and finite strictly positive
  `sigma_px` whose squared variance is also finite and strictly positive in
  binary64. This representability condition is required by the already-frozen
  `R_reduced=sigma_px^2 I>0` contract; it is not a new noise-model restriction.
  Invalid global configuration fails startup. The production
  reducer still defensively maps an invalid sigma argument to `nonfinite` for
  unit/API callers under the ordered runtime contract.
- The branch is inside `UpdaterMSCKF::update`, immediately after
  `UpdaterHelper::get_feature_jacobian_full` and before the existing
  `UpdaterHelper::nullspace_project_inplace` call.
- Both modes consume the exact same feature order, observations,
  triangulation, runtime residual `r`, mixed-FEJ matrices `H_x,H_f`, state
  ordering, pixel noise, chi-square table, global measurement compressor, and
  `StateHelper::EKFUpdate` implementation.
- The candidate is valid only for a three-column transient `GLOBAL_3D`
  landmark Jacobian. An unsupported configured representation is a startup
  error when `schur` is selected. An invalid per-feature system is rejected
  under the ordered runtime policy below. Neither case may fall back silently
  to the baseline.

## Square-root Schur reduction

For `m` raw rows and scalar pixel standard deviation `sigma_px>0`, define

```text
A = H_x / sigma_px
B = H_f / sigma_px
b = r   / sigma_px.
```

Apply the exact ordered CP1 validity, rank, and conditioning policy. The
executable priority is:

1. reject incompatible `H_x,H_f,r` dimensions or a non-three-column `H_f` as
   `nonfinite`;
2. reject `m<=3` as `insufficient_rows`;
3. reject nonfinite raw fields or nonfinite/nonpositive `sigma_px` as
   `nonfinite`;
4. whiten by direct elementwise division and reject any nonfinite `A,B,b` as
   `nonfinite`; do not reject solely because the unused scalar reciprocal
   `1/sigma_px` would overflow;
5. require a complete finite three-value SVD spectrum;
6. require `s_1>std::numeric_limits<double>::min()`;
7. require `s_3>max(m,3)*epsilon*s_1`;
8. reject `rho=s_3/s_1<1e-6`, accepting exact equality.

Singular values become available only after step 5 and `rho` only after step
6. For an accepted feature, a Householder QR factorization gives

```text
B = Q_1 R_1,              Q = [Q_1 Q_2],
Q^T [A b] = [D e; A_N b_N].
```

The implementation applies the complete `m`-row Householder sequence to
`[A b]` and takes exactly `bottomRows(m-3)`. Thin `Q`, the top three rows, or a
normal-equation factor are not substitutes. Invalid/nonfinite QR factors or
transformed outputs reject the feature as `nonfinite` with a stage-specific
reason; there is no Givens fallback.

The implementation emits the unwhitened equivalent row system

```text
H_reduced = Q_2^T H_x,
r_reduced = Q_2^T r,
R_reduced = sigma_px^2 I,
q = m - 3.
```

If `sigma_px^2` is zero or nonfinite in binary64, the unwhitened output cannot
represent the contracted positive measurement covariance and the reducer
rejects it as `nonfinite` at `reduced_outputs`. Normal startup rejects the same
global configuration before feature processing.

This is a square-root realization of the Schur complement. The orthonormal
basis is not unique, so raw reduced matrices are not a parity target. The
basis-invariant sufficient statistics are

```text
Lambda = H_reduced^T H_reduced / sigma_px^2,
eta    = H_reduced^T r_reduced / sigma_px^2,
gamma  = r_reduced^T r_reduced / sigma_px^2.
```

They must match the signed-off CP1 definition and the baseline Givens
nullspace system within the CP2 tolerances. No normal-block inverse,
pseudoinverse, diagonal damping, jitter, singular-value clamping, or finite
substitute for an unavailable diagnostic is permitted.

## Gate and update invariants

- Per-feature NIS is evaluated before stacking, using the emitted reduced row
  system, the same marginal covariance, and `sigma_px^2 I`. A failed/nonfinite
  innovation factorization or NIS rejects and logs that feature; it cannot
  reach the gate comparison.
- The chi-square degrees of freedom are exactly `q=m-3`; rejection is only for
  strict `chi2 > multiplier*quantile_0.95(q)`.
- Accepted reduced rows are lifted through `Hx_order`, stacked, globally
  compressed, and passed once to the unchanged `StateHelper::EKFUpdate`.
- The existing covariance-form innovation solve is an algebraically identical
  realization of the summed Schur `Lambda,eta` statistics and preserves
  updates to state blocks correlated with directly observed blocks. For
  `P>=0` and `R=sigma_px^2 I>0`, its innovation covariance is strictly positive
  definite. This is the approved-equivalent CP2 realization of Section 7 in
  `docs/iterated_update_spec.md` and avoids introducing a second full-state
  covariance implementation. `gamma` is diagnostic/objective information; it
  does not affect the one-pass posterior.
- Sum and retain accepted per-feature `gamma` before global compression.
  Compression preserves `Lambda,eta`, but the resized residual cannot recover
  discarded residual-only energy.
- The identity covariance-reset convention remains frozen for baseline parity.
- The selected-pass transactional covariance contract in Sections 8--9 of
  `docs/iterated_update_spec.md` remains a blocked fixed-two-pass candidate; it
  is not activated by CP2. The one-pass candidate retains the documented
  baseline commit semantics.
- After global compression and before any live write, both modes execute the
  same read-only preflight using the exact `StateHelper::EKFUpdate` ordering:
  validate shapes and finite `H,res,R,P`; construct full-state `M=P H^T` by
  `H_order`; form the upper-triangle innovation
  `S=H P_small H^T+R`; require a successful LLT; form `S^-1`, `K`, `dx=K res`,
  and the subtractive/mirrored proposal `P_plus=P-K M^T`; require every
  proposal field finite and every diagonal of `P_plus` nonnegative. Any
  failure logs the exact stage and rejects the whole update with zero mean or
  covariance writes. No eigenvalue repair, jitter, alternate solve, or full
  PSD claim is introduced. On success, call the unchanged
  `StateHelper::EKFUpdate` once; tests require its committed `dx,P_plus` to
  match the preview. This shared guard preserves elimination as the only
  baseline/candidate variable on all valid CP2 records.
- Each reduction failure records feature ID, pass index `1`, raw row count,
  exact status, and only the singular values or ratio that are actually
  available under the ordered CP1 policy.

## Hard checkpoints

1. **CP2-A — production reducer:** at least 1,024 deterministic accepted and
   rejected feature systems exercise the actual production helper. Schur
   statistics match an independent production-Givens oracle at
   `1e-10 + 1e-8*||reference||`; NIS, increment, and posterior covariance match
   at `1e-8 + 1e-6*||reference||`. Pixel sigma varies and is not fixed to one.
   Every exact validity/rank/gate boundary has a protecting assertion.
2. **CP2-B — OpenVINS semantics:** an FEJ-on golden fixture freezes the mixed
   current/FEJ `r,H_x,H_f,Hx_order` behavior using literal or hash-anchored
   expected values and deliberately distinct current/FEJ clone values. At
   least 128 fixtures call `StateHelper::clone` directly and compare the actual
   baseline and candidate updater paths, protecting exact clone-copy PSD
   covariance, unchanged FEJ values, identity reset, all covariance block
   pairs, and a directly unobserved correlated block with a nonzero update.
   Invalid proposals leave mean/covariance bitwise unchanged and report zero
   jitter, clamp, regularization, and fallback counts.
3. **CP2-C — recorded updater parity:** shadow comparison on at least 1,000
   deterministic committing visual-update records comes from the serial ROS1
   runner over the predeclared CP2 bags, offsets, and hash-anchored
   configuration—not synthetic fixtures. Source, binary, configuration, and
   input hashes are retained. A committing record has at least one accepted
   feature, produces a valid nonempty globally compressed system, passes the
   shared update preflight, and yields a baseline commit. Attempted,
   all-rejected, empty-after-compression, and preflight-rejected records are
   reported separately and do not count toward 1,000. The comparison reports
   state-increment error for every state block and posterior error for every
   covariance block pair, each bounded by
   `1e-8 + 1e-6*||reference block||`. The baseline is the only
   committing shadow path; the candidate proposal is computed from the same
   pre-mutation state and raw feature systems with zero shadow writes.
   Both `matching feature-gate decisions / all attempted feature-gate
   decisions` and the same agreement weighted by each feature's raw
   measurement-row count must be at least `0.999`; every numerator and
   denominator is retained. Every disagreement is classified. A feature or
   accepted-set disagreement does not exempt the record from the state and
   covariance bounds; an out-of-tolerance proposal fails CP2-C.
4. **CP2-D — sequence parity:** baseline and candidate complete MH_01_easy,
   MH_03_medium, and V1_01_easy from frozen offsets 40 s, 5 s, and 0 s,
   respectively. Bags and all resolved configuration inputs are hash-checked.
   Both modes use FEJ, stereo KLT, 11 clones, `CamRadtan`, no ground-truth
   initialization, and fixed camera extrinsic/intrinsic/time-offset
   calibration. A valid synchronized stereo pair is counted from the hashed
   bag index using the serial runner's required `/cam0/image_raw` and
   `/cam1/image_raw` topics, strict `abs(t_cam1-t_cam0)<0.02 s` forward-search
   rule, and used-message deduplication; processed-pair counts come from the
   serial callback trace. Both modes cover at least 99.5% of the selected
   post-offset input time interval, witnessed by first/last selected and
   processed timestamps and durations; the valid synchronized-camera-pair
   processing fraction is also at least 99.5%. On shared output timestamps, compute one
   baseline-to-ground-truth SE(3) alignment and apply it to both modes—never
   align the modes independently. Position p95 is at most 0.01 m, orientation
   p95 at most 0.05 degrees, and
   `abs(ATE_schur-ATE_nullspace)/ATE_nullspace <= 0.01`. ATE uses `evo_ape`
   TUM input, translation RMSE, SE(3) alignment without scale, and maximum
   timestamp-association difference `0.01 s` on the same shared timestamps.
5. **CP2-E — timing:** only fixed-clock desktop runs count. Candidate median
   visual-update time is at most 10% above baseline and p95 is at most 15%
   above baseline. The timer starts as the first operation on entry to
   `UpdaterMSCKF::update`; the committing endpoint is immediately after
   `StateHelper::EKFUpdate` returns, and every early return records its endpoint
   immediately before returning. The primary sample is a nonempty MSCKF
   invocation that reaches and commits a global update, measured internally
   with `std::chrono::steady_clock` and retained as integer nanoseconds. All
   updater invocations are also reported separately. Exclude the first 60 s
   after each frozen bag offset; use NumPy's linear quantile definition;
   retain sample counts. Use the same executable hash, fixed
   affinity/governor/clocks, and three paired runs in predeclared
   `nullspace/schur`, `schur/nullspace`, `nullspace/schur` order. Within each
   pair, compare only the intersection of post-warm-up camera timestamps that
   committed in both modes. Compute each mode's linear median and p95 on that
   common population, then their candidate/baseline ratios. Every one of the
   three paired ratios must satisfy the limits; also report the median ratio
   across pairs. Exact CPU IDs, governor, and min/max/current frequency are
   recorded before timing evidence begins. Runs under the currently observed
   `ondemand` governor are functional evidence only.

CP2-A/B include protecting assertions that an absent mode key selects
`nullspace`, explicit `schur` selects only the candidate reducer, unknown or
fixed-two-pass values fail startup, an unsupported representation fails
startup in `schur`, and every runtime failure has a zero silent-fallback
counter.

Every trace records update/feature IDs, mode, status, singular diagnostics,
NIS/threshold/decision, accepted-set hash, retained gamma, zero-valued
regularization/fallback counters, and prior/config/input hashes. A CP2 artifact
is eligible only from a clean commit, reruns the complete CP1 test set, is
sealed without overwrite, and has an independent verifier and checksum.
The preregistered entry points are `scripts/cp2/run_unit_gate.sh`,
`scripts/cp2/run_recorded_parity.py`, `scripts/cp2/run_sequence_pair.py`,
`scripts/cp2/run_timing_pair.py`, and `scripts/cp2/verify_report.py`; the
machine report is schema-1 `cp2_report.json`. These programs must exist and
pass self-tests before any CP2 result is eligible for sealing.

CP2 is a correctness/parity bridge. The QR row realization is algebraically
equivalent to the Schur complement but is not, by itself, evidence of a novel
or faster direct-statistics solver. No speed or novelty claim follows from a
CP2 pass.

Failure of CP2-A or CP2-B blocks dataset execution. Failure of CP2-C or CP2-D
keeps the baseline default and blocks CP3. Failure of CP2-E blocks the CP2 pass
decision but does not invalidate already sealed mathematical evidence.
Even a CP2 pass does not authorize fixed two-pass: the separate mixed-FEJ
surrogate and chart-transport tests and review remain mandatory.
