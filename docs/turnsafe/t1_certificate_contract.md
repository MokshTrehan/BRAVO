# TurnSafe T1 certificate contract

Status: frozen before the first real-data certificate scan

## Constants

```text
translation component confidence p_t = 0.99865
range component confidence       p_d = 0.99865
joint Bonferroni confidence          = 0.9973
translation covariance inflation     = 2.0
pixel noise sigma                     = 1.2 px per independent coordinate
rho_trans maximum, inclusive          = 0.5
CamRadtan Newton maximum iterations    = 20
CamRadtan Newton pixel infinity norm   = 1e-12 px
captured-normalized forward audit      = 5e-4 px infinity norm
checked 2x2 reciprocal condition       >= 1e-12
stereo two-ray reciprocal condition    >= 1e-6
bearing covariance reciprocal condition >= 1e-12
```

Quantiles are evaluated in binary64 with Boost.Math from the declared
probabilities: chi-square with three degrees of freedom for translation and
the standard normal for the one-sided range LCB. No observed survival enters
a quantile.

## CamRadtan and bearings

The fixed parameter order is `[fx,fy,cx,cy,k1,k2,p1,p2]`. For normalized
coordinates `(x,y)`, P1 uses the repository's all-double Brown-Conrady
forward model and its exact 2x2 derivative from normalized coordinates to
raw pixels. The inverse is a checked Newton solve with a maximum of 20
iterations, finite iterates, 2x2 reciprocal condition at least `1e-12`, and
pixel infinity-norm residual at most `1e-12`. It never uses a focal-length
approximation, damping,
pseudoinverse, or coordinate clamp.

Captured native-normalized coordinates are the bearing mean and must pass an
independent forward raw-pixel consistency audit with pixel infinity norm at
most `5e-4`. The local pixel-to-normalized
Jacobian is the checked inverse of the exact forward Jacobian at that point.
For `g=[x,y,1]^T` and `b=g/||g||`,

```text
J_b_z = (I - b b^T)/||g|| * [[1,0],[0,1],[0,0]].
```

Raw pixels must be finite and inside `[0,width) x [0,height)`. Invalid,
singular, nonconvergent, or inconsistent inverses fail closed.

## Target-time stereo range

The main and configured mate observations must have identical feature and
bit-preserving target timestamp identities. For camera `j`, with fixed
`E_j=R_ItoC_j` and `p_j=p_IinC_j`, define its center and unit ray in IMU
coordinates as

```text
q_j = -E_j^T p_j,       u_j = E_j^T b_j.
```

With `A=[u_0,-u_1]` and `w=q_1-q_0`, solve the full-rank two-ray least-squares
system `(A^T A)y=A^T w`, `y=[d_0,d_1]^T`. Both ranges must be strictly
positive. The main-camera radial range is `d_hat=d_0`. Singular or
ill-conditioned stereo geometry is rejected; the 3x2 ray matrix must have
singular-value ratio at least `1e-6`. It is not repaired.

Differentiate the normal equations analytically. With ray-pair residual
`e=w-Ay`,

```text
dy = (A^T A)^-1 [dA^T e - A^T dA y].
```

Chain its first row through both exact pixel-to-bearing Jacobians. With
independent `1.2^2 I_2` pixel covariances,

```text
sigma_d^2 = sigma_px^2 * J_d_pixels J_d_pixels^T,
d_LCB     = d_hat - normal_quantile(p_d) * sigma_d.
```

Require finite positive variance and `d_LCB>0`. The covariance path is
verified against central finite differences and one-sided Monte Carlo
coverage.

## Relative camera-center translation

For clone `i`,

```text
c_i^G = p_i^G - R_i^T E^T p_IinC,
s     = E^T p_IinC,
mu_t  = c_a^G - c_b^G.
```

Clone pose covariance order is `[theta,p]`. Assemble the unmodified captured
12x12 pair covariance as `[[P_ss,P_st],[P_ts,P_tt]]`, including all
orientation-position and cross-clone terms, and use

```text
J_t = [R_a^T[s]x, I, -R_b^T[s]x, -I],
P_t = J_t P_ab J_t^T,
P_t_cert = 2 P_t.
```

The four captured blocks must be finite, dimensionally valid, mutually
symmetric to the declared binary64 audit tolerance, and PSD as one assembled
matrix. No block is dropped and no covariance is clamped, jittered,
regularized, or replaced. Then

```text
t_UCB = ||mu_t|| + sqrt(chi_square_3_quantile(p_t)
                        * lambda_max(P_t_cert)).
```

Nonfinite or invalid covariance fails closed.

## Joint acute certificate

The acute check is the strict comparison `t_UCB < d_LCB`. Equality and every
`t_UCB>d_LCB` case return `TRANSLATION_NOT_ACUTE`; no saturated arcsine is
computed. Only in the acute regime,

```text
theta_trans_UCB = asin(t_UCB/d_LCB).
```

For the 2x2 factor residual covariance `Sigma_R`, require finite symmetry,
strict positive definiteness under checked Cholesky, and
`lambda_min/lambda_max>=1e-12`. Let

```text
sigma_bearing_worst = sqrt(lambda_min(Sigma_R)),
rho_trans = theta_trans_UCB / sigma_bearing_worst.
```

Admission uses inclusive `rho_trans<=0.5`. `lambda_max` is never used in the
denominator. The union bound supplies joint coverage of at least 0.9973 from
the two 0.99865 component bounds without an independence assumption.

## Numerical policy

All required values must be finite. Matrix symmetry audits use
`100*epsilon_double*max(1,max_abs(matrix))`; passing matrices may be read via
their self-adjoint view only after the audit, but values are never repaired.
Rank uses its separately frozen scale-aware tolerance. Cholesky, eigensolver,
and direct 2x2/3x3 solves must report success. There is no damping,
pseudoinverse, clamp, fallback repair, or tolerance selected from real data.
