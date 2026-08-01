# Reduced and iterated visual update specification

Status: **second adversarial audit correction in progress; prior schema-3 evidence superseded for authorization**
Pinned upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
Frozen rank threshold: `1e-6` relative singular-value ratio
Primary reset policy through CP3: identity, for OpenVINS covariance parity

This document is normative for the SchurVIO-Lite one-pass update and records
the still-blocked fixed-two-pass candidate. Production estimator integration
remains forbidden until the post-review CP1 addendum tests pass and the named
human reviewer attests the corrected commit.

## 1. Symbols and scope

For one transient feature:

- `x^-` is the predicted filter state before the visual update and `P^-` is
  its covariance in the OpenVINS error-state ordering.
- `delta_x in R^n` is a state correction. A feature touches a subset of the
  full state; every feature statistic is lifted into the full ordering through
  `Type::id()` and `Hx_order`, never a guessed absolute column offset.
- `delta_lambda in R^3` is the correction to a transient `GLOBAL_3D`
  landmark. The first implementation is restricted to this representation.
- `N` observations give `m=2N` distorted-pixel residual rows. A useful feature
  must have `m>3` and a full-rank three-column landmark Jacobian.
- `r in R^m`, `H_x in R^(m x n)`, `H_f in R^(m x 3)`, and
  `R in R^(m x m)` denote the residual, positive-sign state model matrix,
  positive-sign landmark model matrix, and measurement covariance before
  landmark elimination.

OpenVINS evaluates the stored residual as

```text
r = z_measured - h_runtime(x_hat, lambda_hat).
```

`h_runtime` calls `CamBase::distort_d`, which casts normalized coordinates to
float and casts the result back to double. For the frozen EuRoC `CamRadtan`
path and tested valid geometry, the analytic camera Jacobians represent the
intended smooth all-double camera model `h_cont`, not the literal derivative
of that quantized wrapper. CP1 makes no derivative claim for `CamEqui`; that
model remains outside the proved projection scope.

With FEJ disabled, the positive-sign model matrices are the continuous-model
derivatives

```text
H_x = + d h_cont(x_hat boxplus delta_x, lambda_hat) / d delta_x at zero
H_f = + d h_cont(x_hat, lambda_hat + delta_lambda) / d delta_lambda at zero
d(z_measured-h_cont)/d(delta_x,delta_lambda) = -[H_x H_f].
```

With FEJ enabled, `H_x` and `H_f` are the pinned mixed-current/FEJ affine
surrogate produced by `get_feature_jacobian_full`; they are not generally the
derivative of either `h_runtime` or a single current `h_cont` residual. In both
cases, the positive Kalman correction consumes the baseline linear surrogate

```text
r ~= H_x delta_x + H_f delta_lambda + noise.
```

Schur/nullspace equivalence requires both paths to consume identical
`r,H_x,H_f`; it does not require the FEJ model matrix to be a current-residual
derivative. These semantics are defined by
`UpdaterHelper.cpp:get_feature_jacobian_full`, `CamBase.h:distort_d`, and
`StateHelper.cpp:EKFUpdate`.

## 2. OpenVINS retraction and fixed prior chart

Quaternion coefficients are JPL scalar-last. For an orientation correction
`a in R^3`, define the exact perturbing quaternion used by OpenVINS as

```text
d(a) = [a/2, 1] / sqrt(1 + ||a||^2/4)
q boxplus a = d(a) (x) q.
```

This implies `R(q boxplus a) = R(d(a)) R(q)` and, to first order,
`R(q boxplus a) = Exp(-a) R(q)`. Vector blocks use ordinary addition.
`JPLQuat::update`, `PoseJPL::update`, and `IMU::update` are the executable
definition.

Both passes use one frozen prior chart:

```text
x(delta) = x^- boxplus delta,       delta_0 = 0.
```

At a nonzero orientation chart coordinate `a`, an infinitesimal change in the
fixed chart maps to the local OpenVINS perturbation through

```text
T_theta(a) = (I - 0.5 [a]_x) / (1 + ||a||^2/4).
```

All Euclidean blocks use `T=I`; the full `T(delta)` is block diagonal. CP1
tests must finite-difference this mapping through the actual JPL retraction.

## 3. Whitening and weights

Factor `R=L L^T` and whiten with triangular solves:

```text
b = L^-1 r
A = L^-1 H_x
B = L^-1 H_f.
```

The frozen EuRoC path has `R=sigma_px^2 I`, so whitening is division by
`sigma_px`. Nullspace rotations and global measurement compression are
orthogonal and preserve this isotropic noise.

Robust loss is disabled for the primary CP1--CP3 path: every row weight is
one. A later ablation may apply a frozen weight `w_j` by multiplying the
corresponding rows of `b`, `A`, and `B` by `sqrt(w_j)`. It may not estimate new
weights in pass 2 or use weighted residuals for the probabilistic chi-square
gate.

## 4. Landmark factorization and rejection

Rank and conditioning are tested on the whitened `B`, never on `B^T B`, a
determinant, or an explicit inverse. Let its singular values satisfy
`s_1 >= s_2 >= s_3 >= 0` and define `rho=s_3/s_1`.

Reject the complete feature before gating when any condition holds:

1. `m<=3`, or a residual/Jacobian field is nonfinite;
2. `s_1` is not strictly greater than
   `std::numeric_limits<double>::min()` (`rank_deficient`);
3. `s_3` is not strictly greater than
   `max(m,3)*std::numeric_limits<double>::epsilon()*s_1`
   (`rank_deficient`);
4. `rho < 1e-6` (`ill_conditioned`).

The conditions are evaluated in the numbered order above. Thus equality at
the numerical-rank floor is rejected, while equality at the `rho=1e-6`
conditioning boundary is accepted. No diagonal damping,
pseudo-measurement, clamping, or silent rank repair is
allowed. Every rejection logs the three singular values, `rho`, feature ID,
pass index, and one of `nonfinite`, `insufficient_rows`, `rank_deficient`, or
`ill_conditioned`. The `1e-6` policy is frozen before real-data Schur results
are inspected and bounds the condition number of the conceptual normal block
to approximately `1e12`.

For accepted `B`, use a direct QR or SVD factorization. With

```text
B = Q_1 R_1,       Q=[Q_1 Q_2] orthogonal,
D = Q_1^T A,       e = Q_1^T b,
```

all solves use `R_1` or the accepted singular values. Code must not call a
matrix `.inverse()`.

## 5. Schur sufficient statistics

For an SPD prior, the whitened joint MAP problem can be written

```text
min 0.5 ||delta_x||_(P^-)^-1^2
  + 0.5 ||b - A delta_x - B delta_lambda||^2.
```

This inverse notation is conceptual and is not valid for the exact
clone-augmented covariance, which can be positive semidefinite. The
rectangular-factor formulation in Section 7 is normative for both SPD and PSD
priors.

Landmark elimination produces three sufficient statistics:

```text
Lambda = A^T A - D^T D
eta    = A^T b - D^T e
gamma  = b^T b - e^T e.
```

The conceptual normal-block form is equivalent:

```text
C = B^T B, E = B^T A, g = B^T b
Lambda = A^T A - E^T solve(C,E)
eta    = A^T b - E^T solve(C,g)
gamma  = b^T b - g^T solve(C,g).
```

It exists only as a derivation; production code factors `B` directly so it
does not square the landmark condition number. `Lambda` is explicitly
symmetrized only after the raw symmetry error has been checked and logged.

After solving the state, landmark back-substitution is

```text
R_1 delta_lambda = e - D delta_x.
```

`gamma` is mandatory. Keeping only `Lambda` and `eta` loses the projected
residual energy and prevents exact NIS/gating parity.

## 6. Exact nullspace and gate equivalence

The corresponding OpenVINS-style left-nullspace system is

```text
A_N = Q_2^T A
b_N = Q_2^T b.
```

For a full-rank three-dimensional feature,

```text
Lambda = A_N^T A_N
eta    = A_N^T b_N
gamma  = b_N^T b_N
q      = m - 3.
```

The per-feature innovation statistic, using the marginal prior covariance
`P_s^-=L_s L_s^T` for the touched state blocks, is

```text
chi2 = b_N^T solve(I + A_N P_s^- A_N^T, b_N)
u    = L_s^T eta
chi2 = gamma - u^T solve(I + L_s^T Lambda L_s, u).
```

Here `L_s` may be rectangular. The reduced matrix is strictly positive
definite because of its identity term, so the second form uses a symmetric
solve and never an inverse of `P_s^-`. For an SPD prior only, this is
algebraically equal to the inverse-information expression.

The gate freezes the exact baseline policy

```text
threshold(q) = chi2_multiplier * quantile(ChiSquared(q), 0.95)
reject if and only if chi2 > threshold(q).
```

Thus equality is accepted. The frozen EuRoC profile has
`up_msckf_chi2_multipler=1`; other profiles use their configured multiplier.
The gate uses exactly `q=m-3` degrees of freedom, matching
`UpdaterMSCKF.cpp`. A naive `m`-row projector is forbidden because it would
silently change the threshold even if its numerical NIS matched.

Global OpenVINS measurement compression first applies an orthogonal transform
and then truncates zero-Jacobian rows. If

```text
Q^T A_N = [R; 0],       Q^T b_N = [c; d],
```

the truncated system preserves `Lambda=R^T R` and `eta=R^T c`, but its stored
residual norm is only `c^T c`; the pre-compression statistic is
`gamma=c^T c+d^T d`. Any code that needs objective or NIS parity after this
compression must retain the pre-compression `gamma` or explicitly carry the
dropped energy `d^T d`. It may not reconstruct `gamma` from the resized
residual alone.

## 7. One-pass state and covariance update

Lift and sum the accepted per-feature state statistics into the full filter
ordering. Clone augmentation copies a pose and its cross-covariances exactly,
so a valid OpenVINS `P^-` can be PSD. Use any full-column covariance factor
`L_p in R^(n x r)`, `r<=n`, satisfying `P^-=L_p L_p^T`; it may be rectangular.
Then solve

```text
J = I_r + L_p^T Lambda L_p
solve J y = L_p^T eta
delta_1 = L_p y
P_chart,1 = L_p solve(J, L_p^T).
```

`J` is strictly positive definite on the prior support. This is algebraically
identical to the covariance-form innovation update for both SPD and PSD
priors. Only when `P^-` is SPD is it also legitimate to write the conceptual
inverse-information form

```text
((P^-)^-1 + Lambda) delta_1 = eta
P_chart,1 = ((P^-)^-1 + Lambda)^-1
```

and it remains identical to the baseline nullspace Kalman update. A production
path may use a preserved augmentation factor or a rank-revealing factor with a
declared numerical-zero tolerance. It may not demand `LLT(P^-)`, add clone
noise, apply diagonal jitter, or silently clamp negative eigenvalues. The CP1
full-joint oracle and nullspace oracle must agree with the Schur state
increment, back-substituted landmark increment, final residual norm, NIS, and
posterior covariance at the declared tolerances.

For CP2 parity, inject the mean once using each OpenVINS type's `update` and
use an identity covariance reset:

```text
x^+ = x^- boxplus delta_1
P_live^+ = P_chart,1.              # G = I
```

OpenVINS performs no explicit covariance reset transport after injection.
This identity policy is a deliberate baseline-parity approximation and must be
used by both compared one-pass paths. Exact chart transport is reserved for a
separately named ablation after CP3.

## 8. Fixed two-pass candidate — blocked

The primary EuRoC configuration uses a mixed FEJ Jacobian: its residual is
evaluated at current values while parts of its projection Jacobian use frozen
FEJ geometry. That matrix is not, in general, the derivative of the current
residual function. Consequently the rules below define an affine FEJ
surrogate candidate; they are not a Taylor/Newton derivation of the current
pixel objective. Fixed-two-pass production work is blocked until an FEJ-on
golden fixture freezes this surrogate and a later review accepts its objective
and exact-chart interpretation.

Before pass 1, snapshot `x^-`, `P^-`, every clone FEJ value, raw observations,
feature IDs/order, configuration, and the predicted prior factor. A pass uses
a working state only; pass 1 must write neither the live mean nor covariance.

At pass `i` with absolute fixed-chart proposal `delta_i`:

1. Construct `x_i=x^- boxplus delta_i` from the snapshot. Never apply an
   absolute proposal sequentially to the previous working state.
2. Triangulate and refine each permitted transient feature using `x_i`.
3. Form the current distorted-pixel residual `r_i` and the baseline mixed-FEJ
   OpenVINS matrices `H_x,i`, `H_f,i`.
4. Map the local state Jacobian to the frozen prior chart:
   `A_i = whiten(H_x,i T(delta_i))`.
5. Correct the right-hand side for the absolute chart:
   `b_i = whiten(r_i + H_x,i T(delta_i) delta_i)`.
6. Factor the whitened landmark Jacobian, form the Schur statistics, and solve
   from the original `P^-`.

The candidate defines the affine surrogate

```text
r_i + H_x,i T(delta_i) delta_i
    ~= H_x,i T(delta_i) delta + H_f,i delta_lambda.
```

If `r_i=z-h_i` and `H_x,i=d h_i/d(local)` for the same smooth current model
`h_i`, this follows from the usual Taylor model of `h_i`. Under the frozen
mixed-FEJ baseline—and also when pairing the float-quantized runtime residual
with the continuous `CamRadtan` derivative—it is instead an explicit
algorithmic definition and no stronger derivative claim is made.
For a genuinely linear current-Jacobian system, pass 2 recreates the pass-1
right-hand side and must return identical mean and covariance.

### Pass-1 selection lock

- Gate only in pass 1 using `P^-`, the unweighted probabilistic model, and the
  explicit `m-3` degrees of freedom.
- Freeze accepted feature IDs, raw measurements, order, gate decisions, and
  unit weights.
- Pass 2 may not admit a feature, drop one independently, or re-gate.
- Clone FEJ values stay frozen. Each transient feature's pass-local FEJ point
  is reset to that pass's triangulation, matching upstream construction.
- Preserve the upstream mixed FEJ behavior: residuals and `uv_norm` are
  current, clone/feature projection geometry used by the later Jacobian terms
  is FEJ, and the distortion Jacobian still uses the earlier current
  `uv_norm`.

If any locked feature fails pass-2 triangulation, depth, finite, rank, or
conditioning checks, reject the entire second pass and select pass 1. Partial
feature-set fallback is forbidden.

### Pass-2 acceptance and fallback

Evaluate both proposals on the same accepted raw pixel measurements. For each
proposal, re-triangulate/refine the locked features and compute:

- the unweighted distorted-pixel cost before nullspace/compression; and
- the frozen-prior posterior objective. For `P^-=L_p L_p^T`, the prior term is
  `0.5*min ||xi||^2` subject to `L_p xi=delta`; inverse-covariance notation is
  allowed only for an SPD prior.

The initializer's normalized-coordinate LM cost is not this acceptance cost.
Select pass 2 only when both costs are finite and neither exceeds the pass-1
value by more than

```text
tau_cost = 1e-9 * max(1, abs(pass1_cost)).
```

Otherwise select pass 1 and log the precise fallback reason. Equal-cost linear
fixtures are valid. CP3 improvement statistics use the raw, untoleranced cost
difference.

### Exactly-once commit

For the selected pass `*`, recompute or retain its statistics and calculate
the posterior from the original prior exactly once:

```text
J_* = I + L_p^T Lambda_* L_p
P_chart,* = L_p solve(J_*, L_p^T)
x_live = x^- boxplus delta_*
P_live = P_chart,*                 # identity reset through CP3
```

No `P_1 -> P_2` sequential covariance update is permitted. Runtime counters
must prove `pass1_live_mean_writes=0`, `pass1_covariance_writes=0`,
`final_mean_commits=1`, and `final_covariance_commits=1`.

The exact selected-chart transport would be
`P_live=T(delta_*) P_chart,* T(delta_*)^T`. It is explicitly not the primary
CP1--CP3 policy because mixing it with an identity-reset baseline would
confound iteration with reset effects. Therefore identity-reset results may be
called baseline-parity results, but never mathematically exact manifold
covariance results.

## 9. Failure and diagnostics contract

Any failed factorization, nonfinite value, invalid depth, inconsistent state
ordering, or non-PSD posterior rejects the affected proposal. No failure may
silently add damping or repair an eigenvalue. Required diagnostics include:

- mode, pass, feature ID, row count, rank, singular values, and `rho`;
- `Lambda` raw symmetry error, `eta`, `gamma`, NIS, gate DoF and decision;
- triangulation/refinement result and accepted-set hash;
- fixed-chart correction, current and corrected residual norms;
- pass costs, fallback reason, and selected pass;
- prior/state/config hashes and mean/covariance write counters;
- posterior symmetry error, minimum eigenvalue, and maximum diagonal.

The posterior acceptance bounds are

```text
||P-P^T||_inf <= 1e-10 * max(1, ||P||_inf)
lambda_min(P) >= -1e-10 * max(1, lambda_max(P)).
```

## 10. CP1 protecting tests

The automated gate must cover at least:

1. 100 deterministic well-conditioned full-joint, nullspace, and Schur
   fixtures for state increment, landmark back-substitution, residual norm,
   `Lambda/eta/gamma`, NIS, and posterior covariance.
2. 200 seeded valid-geometry `GLOBAL_3D` projection cases on the frozen EuRoC
   `CamRadtan` path using the actual JPL retraction and an all-double
   continuous forward-projection oracle with FEJ disabled. Compare `H` to
   `d h_cont/d delta`, or `-H` to the derivative of `z-h_cont`; separately
   verify the nominal stored residual against the float-quantized runtime
   projection. `CamEqui` is outside this CP1 derivative proof.
3. 100 exact-rank-deficient or condition-ratio-at-most-`1e-12` landmark
   fixtures, all rejected deterministically without `.inverse()` or NaN.
4. Raw posterior symmetry and PSD checks before any symmetric view hides an
   error.
5. Fixed-chart `T_theta` finite differences through the exact normalized JPL
   perturbing quaternion.
6. At least 100 algebraic exact clone-copy PSD priors, comparing known and
   rank-revealing rectangular-factor updates with the covariance-form
   innovation oracle without jitter or a prior inverse. CP2 must additionally
   exercise production `StateHelper::clone`.
7. At least 100 calls to the actual production measurement compressor on tall
   systems, proving `Lambda/eta` preservation and accounting for the strictly
   positive discarded contribution to `gamma`.

An FEJ-on golden fixture is required for CP2 parity because the mixed FEJ
Jacobian is intentionally not the finite-difference derivative of a single
current residual function.

## 11. Review lock

Fresh human signoff on the replacement addendum commit must explicitly approve:

- runtime residual, continuous-model derivative, mixed-FEJ surrogate, positive
  update sign, and exact JPL chart;
- whitening, three Schur statistics, NIS, and `m-3` gate DoF;
- the `1e-6` rank threshold and no-regularization rule;
- rectangular-factor handling of exact clone-augmented PSD priors;
- pre-compression `gamma`, the 95% chi-square quantile, configured multiplier,
  `m-3` DoF, and strict-`>` rejection boundary;
- the fact that mixed-FEJ fixed-two-pass work remains blocked and is specified
  only as an affine surrogate candidate;
- identity reset as a baseline-parity policy, not an exact chart transport.

Reviewer, date, reviewed commit, and exceptions are recorded in
`docs/conventions.md` and `project/checkpoints.yaml`.
