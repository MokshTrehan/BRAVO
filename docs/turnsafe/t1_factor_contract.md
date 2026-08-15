# TurnSafe T1 rotation-bearing factor and selector contract

Status: frozen before the first real-data certificate scan

## Residual, sign, and FEJ split

Use `R_i=R_GtoI_i`, fixed `E=R_ItoC`, measured unit bearings `b_a,b_b`, and

```text
R_ab = E R_b R_a^T E^T.
```

The deterministic target tangent basis selects the Cartesian axis with the
smallest absolute dot product with `b_b`, resolving exact ties x, then y,
then z. Project and normalize that axis as `v1`, set `v2=b_b x v1`, and use
`B=[v1,v2]`. The measured target bearing freezes B.

Production-sign shadow innovation is measurement minus prediction:

```text
r = B^T (b_b - R_ab b_a).
```

The local error is true minus estimate under the repository's left JPL
retraction. Residual uses current clone rotations. Jacobians use FEJ clone
rotations, while bearings and fixed calibration are unchanged. With FEJ
values in

```text
u = R_b R_a^T E^T b_a,       w = E^T b_a,
J_a = -E R_b R_a^T [w]x,     J_b = E[u]x,
H_a = B^T J_a,                H_b = B^T J_b,
```

the direct factor columns are `[H_a,0_2x3,H_b,0_2x3]`. `H` is the positive
measurement-model Jacobian in `r≈H delta+n`; holding the measurement fixed
while perturbing the estimate gives residual derivative `-H`. Tests bind
both statements. The mixed current/FEJ object is an affine surrogate, not
the derivative of one current residual function.

## Residual covariance and whitening

At the measured source and target normalized coordinates, use the audited
pixel-to-bearing Jacobians from the certificate contract. With current
`R_ab`,

```text
J_za = -B^T R_ab J_ba J_ua,
J_zb =  B^T      J_bb J_ub,
Sigma_R = 1.2^2 * (J_za J_za^T + J_zb J_zb^T).
```

Source and target pixel noises are independent; shared translation bias is
not placed in `Sigma_R`. Require finite audited symmetry, strict positive
definiteness, and checked LLT. Whitening is the direct lower-triangular solve
`L^-1 r` and `L^-1 H`; no explicit matrix inverse, damping, or repair is
allowed.

## pilot_consensus_v1

Canonical member order is the complete candidate key. Groups with n=2 or
n=3 are reported shadow-only; n<4 is never production-eligible. For n>=4:

1. Form current prior-predicted source bearings `p_i=R_ab b_ai`.
2. Fit the proper rotation `Q` minimizing the unweighted sum
   `||b_bi-Q p_i||^2` by deterministic 3x3 SVD. Reject a failed/nonfinite SVD.
3. For each member, compute its two-dimensional tangent residual at `Q` and
   its checked whitened norm `q_i` using that member's frozen `Sigma_R`.
4. The inclusive absolute threshold is
   `sqrt(chi_square_2_quantile(0.99))+0.25`, numerically
   `3.2848542587702925` whitened units.
5. Compute the lower middle order statistic for even n as the deterministic
   median, `MAD=median(|q_i-median(q)|)`, and the inclusive robust threshold
   `median(q)+4.0*1.482602218505602*MAD`.
6. Retain a member exactly when `q_i` is no greater than both thresholds.
7. Require at least `max(3,ceil(0.75*n))` retained members. Fit one proper
   rotation to the retained set. Never reintroduce, retrim, or refit again.
8. Recompute retained residuals at the refit and require each to remain no
   greater than both already-frozen thresholds. Otherwise reject the group.

The fitted rotation and its magnitude are telemetry only. Consensus does not
change the factor linearization.

## Spatial, rank, and conditioning gates

Apply these gates to retained inlier target raw pixels. Pixel-center fractions
are `x=(u+0.5)/width`, `y=(v+0.5)/height` and must lie in `[0,1]`. Boundaries
are inclusive:

```text
convex hull area                         >= 0.1
minimum population-covariance eigenvalue >= 0.03
occupied fixed 4x4 cells                 >= 4
x span and y span                        >= 0.6 each
```

Cells are `floor(4*x),floor(4*y)` with exact coordinate 1 assigned to cell 3.
A retained n=4 group must therefore occupy four distinct cells.

Stack one `-[b_b]x` 3x3 block per retained target bearing. With descending
singular values `sigma_1>=sigma_2>=sigma_3`, rank counts singular values
strictly greater than
`100*epsilon_double*max(3*n_inliers,3)*sigma_1`; require rank exactly 3.
Require inclusive practical conditioning `sigma_3/sigma_1>=0.15`.

## Pre-NIS selector

For each fully eligible group, compute the whitened local relative-rotation
stack from the retained factors. Score lexicographically:

1. larger current prior-predicted relative camera rotation principal angle;
2. larger minimum singular value of the whitened local rotation stack;
3. smaller maximum retained-member `rho_trans`;
4. larger retained eligible-feature count;
5. smaller camera ID;
6. bytewise smaller source timestamp key;
7. bytewise smaller target timestamp key.

All finite binary64 score components compare exactly; no epsilon tie is used.
Freeze exactly one winner before NIS. Every other fully eligible group is
foregone. Winner failure never promotes a runner-up.

## Shadow NIS and predicted information

Stack current residuals and direct two-clone Jacobians for retained winner
members. Assemble the captured 12x12 pose covariance with every cross-block.
For block-diagonal measurement covariance `R`, compute

```text
S = H P_ab H^T + R,
NIS = r^T S^-1 r
```

by checked LLT. The frozen threshold source is the production KAIST MSCKF
rule: Boost.Math chi-square quantile at probability 0.95, degrees of freedom
equal to the stacked residual row count, multiplied by the frozen config
multiplier 1.0. Equality passes; only `NIS>threshold` rejects.

For each factor define its whitened relative-orientation block from the two
clone orientation blocks under the common relative-rotation coordinate. The
reported predicted orientation information is

```text
I_rel = H_rel_whitened^T H_rel_whitened,
info_trace = trace(I_rel),
info_eigenvalues = eigenvalues(I_rel).
```

It is finite and nonzero only when `info_trace>1e-12 rad^-2`. At or below
that preregistered numerical/practical floor it is `INFORMATION_NEGLIGIBLE`.
The information metric cannot select thresholds or use trajectory outcomes.

