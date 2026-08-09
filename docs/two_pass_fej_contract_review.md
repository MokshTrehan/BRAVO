# Fixed two-pass FEJ contract review

Status: **accepted for narrow implementation**

This review resolves the fixed-two-pass governance blocker without changing
the underlying estimator conventions. It authorizes an ordinary Schur visual
update with a configured maximum of one or two passes only for transient
`GLOBAL_3D` features on the `CamRadtan` path. It does not claim that the
mixed-FEJ matrix is a Taylor/Newton derivative, does not authorize `CamEqui`,
adaptive pass scheduling, or more than two passes, and does not change the
inherited reset convention.

## Approval record

- Reviewer: Moksh Trehan
- Reviewer relationship: project-author self-review
- Review window: 2026-08-08--2026-08-09
- Conditional project-author approval date: 2026-08-09
- Golden condition satisfied by commit:
  `e5441605e1da2072fec578ed7d4b649a83f5307a`
- Golden target: `test_cp1_one_pass_regression`
- Release result: all five existing CP1 binaries passed 14/14 tests, with no
  disabled or skipped tests
- Exceptions: none

The approval became effective when the deterministic golden and the complete
CP1 Release matrix passed. The golden commit is the immutable evidence anchor;
this document does not cite its own later documentation commit as evidence.

## Inherited FEJ, sign, and chart contract

The implementation must preserve `use_fej=true` and the inherited OpenVINS
mixed evaluation exactly:

- distorted-pixel residuals and normalized coordinates are evaluated at the
  current nominal clone, feature, extrinsic, and intrinsic values;
- observing-clone and transient-feature projection geometry used by the
  corresponding Jacobian terms is evaluated at FEJ values;
- camera extrinsic and intrinsic blocks are evaluated at current values;
- after recomputing `p_FinC` with FEJ geometry, the distortion Jacobian keeps
  using the earlier current `uv_norm`, preserving the inherited mixed-FEJ
  behavior; and
- clone FEJ values and every other FEJ-locked reference remain unchanged
  across proposal construction and final state injection.

The stored residual and positive-sign model are

```text
r_i = z_measured - h_runtime(x_i, lambda_i)
r_i ~= H_x,i delta_x + H_f,i delta_lambda + noise.
```

The state correction therefore has the inherited positive sign (`delta=K r`)
and is injected with OpenVINS left-multiplicative JPL orientation updates and
additive Euclidean updates. Both passes use the one frozen prior chart

```text
x(delta) = x^- boxplus delta,             delta_0 = 0
T_theta(a) = (I - 0.5 [a]_x) / (1 + ||a||^2/4),
```

with `T=I` on Euclidean blocks. If `R_f=L_f L_f^T`, pass `i` constructs

```text
A_i = solve(L_f, H_x,i T(delta_i))
b_i = solve(L_f, r_i + H_x,i T(delta_i) delta_i)
```

and defines the affine surrogate

```text
r_i + H_x,i T(delta_i) delta_i
    ~= H_x,i T(delta_i) delta + H_f,i delta_lambda.
```

This plus sign is normative. The construction is an algorithmic affine-FEJ
contract; it is not evidence that `H_x,i` is the derivative of one current
runtime residual, and it carries no Taylor or Newton convergence claim.

## Same-prior and population lock

Before pass 1, snapshot `x^-`, `P^-`, the covariance factor, FEJ values, raw
observations, feature IDs/order, and configuration. Pass 1 performs the
ordinary finite, geometry, whitening, rank, conditioning, solve, and
chi-square/NIS checks against `P^-`; its accepted IDs, raw measurements,
order, unit weights, and gate decisions then become immutable.

Pass 2 starts from `x(delta_1)` but solves against the same captured `P^-`.
It re-triangulates/refines each locked transient feature and rebuilds its
current residual and mixed-FEJ Jacobians. It may neither add a feature nor drop
one independently. For every locked feature, its production NIS is recomputed
against the same `P^-`, with `q=m-3` and rejection only for
`chi2 > threshold(q)`. This is a validity check on the whole pass-2 proposal,
not a new population gate: one nonfinite value or failed geometry, rank,
conditioning, solve, or NIS check invalidates all of pass 2 and falls back to
valid pass 1.

Pass 2 never consumes a pass-1 posterior covariance. No exploratory proposal
may mutate the live state, captured covariance, FEJ references, raw feature
data, or final feature geometry.

## Production validity and proposal objectives

Production validity and iteration benefit remain distinct. A lower objective
cannot rescue a proposal that fails production safeguards, and a lower NIS or
linearized quadratic surrogate alone is not evidence of nonlinear
improvement.

For both proposals, independently re-triangulate/refine the same locked raw
observations and evaluate

```text
C_pix,j  = sum_f ||r_f,j||^2
C_post,j = 0.5 * min_{xi: L_p xi=delta_j} ||xi||^2
           + 0.5 * sum_f ||solve(L_f,r_f,j)||^2,
P^-      = L_p L_p^T,
R_f      = L_f L_f^T.
```

An infeasible prior-support constraint makes `C_post,j=+infinity`. The
initializer's normalized-coordinate refinement cost, NIS, and the affine
surrogate cost are not substitutes for either objective above. Each objective
has its own tolerance:

```text
tau_pix  = 1e-9 * max(1, abs(C_pix,1))
tau_post = 1e-9 * max(1, abs(C_post,1)).
```

Pass 2 is selectable only if it is production-valid, both costs are finite,
and both independent inequalities hold:

```text
C_pix,2  <= C_pix,1  + tau_pix
C_post,2 <= C_post,1 + tau_post.
```

## Decision table

| Case | Pass 1 | Pass 2 | Cost result | Selection and commit rule |
|---|---|---|---|---|
| A | Valid | Valid | Both objectives improve | Select pass 2; form its covariance and commit once |
| B | Valid | Valid | Both objectives are unchanged within their own tolerances | Select pass 2; form its covariance and commit once |
| C | Valid | Valid | Either objective worsens beyond its own tolerance | Fall back to pass 1; form its covariance and commit once |
| D | Valid | Rank, geometry, finite-value, solve, or same-`P^-` NIS failure | Costs cannot override invalidity | Invalidate all of pass 2; keep the locked set and select pass 1 |
| E | Valid | Invalid for any production reason | Pass 1 is the valid fallback | Pass 1 remains commit-safe; exactly one final commit |
| F | Invalid | Irrelevant | No valid fallback exists | Reject the visual update; commit neither mean nor covariance |

Pass 1 itself must remain production-valid and have finite `C_pix,1` and
`C_post,1`; otherwise Case F applies.

## Covariance and reset boundary

After selection, and only for selected pass `*`, construct

```text
J_*       = I + L_p^T Lambda_* L_p
delta_*   = L_p solve(J_*, L_p^T eta_*)
P_chart,* = L_p solve(J_*, L_p^T)
x_live    = x^- boxplus delta_*
P_live    = P_chart,*                         # G = I
```

The covariance is built from the original `P^-` exactly once. There is no
`P_1 -> P_2` covariance sequence, no unselected covariance construction, and
one successful visual update performs exactly one mean commit and one
covariance commit. Invalid pass 1 or an invalid selected covariance performs
neither commit.

`G=I` is the inherited OpenVINS-parity convention and is the entire covariance
claim approved here. The differential transport `T(delta) P T(delta)^T`
remains an out-of-scope, separately named ablation. Neither this contract nor
results produced under it may be described as chart-consistent covariance.

## Deterministic golden evidence

Commit `e5441605e1da2072fec578ed7d4b649a83f5307a` adds a 47-error-state,
FEJ-enabled `GLOBAL_3D`/`CamRadtan` fixture with active current intrinsics,
four distinct current/FEJ clones, nonidentity current/FEJ camera calibration,
four observations, a nonzero fixed-chart displacement, and an independent
float-quantized Radtan residual and mixed-FEJ Jacobian oracle. It exercises the
production `SchurUpdate::Reduce` and production preview only where those are
the objects under test; direct matrix construction, explicit reprojection,
and covariance-form oracles prevent a production-routine-twice comparison.

Target `test_cp1_one_pass_regression` contains the three protecting cases:

- `CP1MixedFejGolden.ProductionRawSchurAndPreviewMatchFixedChartIndependentOracle`
- `CP1MixedFejGolden.ActualTwoPassUsesSamePriorEvaluatesTrueCostsAndCommitsOnce`
- `CP1MixedFejGolden.FixedTwoPassDecisionCasesAThroughF`

The actual two-pass golden recorded:

| Quantity | Observed value |
|---|---:|
| Pass-1 / Pass-2 raw residual oracle error | `0` / `0` |
| Pass-1 / Pass-2 state-Jacobian oracle error | `1.9929960069004156e-13` / `2.1334096249257504e-13` |
| Pass-1 / Pass-2 landmark-Jacobian oracle error | `2.0214597421281585e-14` / `8.8817841970012523e-16` |
| Nonzero affine-correction norm | `0.29811826515652995` |
| Pass-1 / Pass-2 NIS | `0.00070143780076834666` / `0.00070177134142434432` |
| Pass-1 / Pass-2 `C_pix` | `0.01788270496763289` / `0.017882596235722303` |
| Pass-1 / Pass-2 `C_post` | `0.0092893412002579088` / `0.0092894524675044346` |
| Selected pass | `1` |
| Selected increment oracle error | `1.6786794752448733e-19` |
| Selected covariance oracle error | `3.2660158884791178e-19` |
| Preview / covariance / live-commit counts | `1 / 1 / 1` |

Pass 2 lowered `C_pix` but increased `C_post` by more than its independent
`1e-9` tolerance, so the exact chart correctly selected pass 1. Both NIS
values passed the five-degree-of-freedom production threshold. The valid
fixture used zero jitter, repair, alternate solve, clamp, regularization, and
fallback operations. The captured `P^-` retained the same object, address,
and bytes; no pass-1 posterior existed while pass 2 was built; and live FEJ
and feature data remained unchanged until the selected exactly-once commit.

The golden comparisons use scaled bounds `abs_tol + rel_tol*scale`: residual
`1e-12 + 1e-10*scale`, raw Jacobians `1e-11 + 1e-10*scale`, Schur information
terms `1e-10 + 1e-8*scale`, and preview/selected increment, covariance, and
NIS `1e-12 + 1e-10*scale`. The direct two-pass raw-oracle caps are `1e-10`
for residual and `1e-8` for each Jacobian. Posterior raw symmetry and numerical
PSD use the existing relative `1e-10` bounds. The independent proposal
objectives use exactly the two `1e-9` tolerances defined above. No prior
tolerance was weakened; the largest observed raw Jacobian error was
`2.1334096249257504e-13`, over four orders of magnitude below its direct cap,
while explicit `1e-4` mixed-evaluation and `1e-6` affine-sign witnesses catch
convention collapse.

The complete Release matrix was:

- `test_cp1_schur_equivalence`: 1/1 passed
- `test_cp1_rank_rejection`: 2/2 passed
- `test_cp1_projection_jacobian`: 2/2 passed
- `test_cp1_prior_and_compression`: 2/2 passed
- `test_cp1_one_pass_regression`: 7/7 passed

Total: 14/14 passed, with no disabled or skipped tests. This evidence accepts
the governance contract for implementation; it does not assert that the
fixed-two-pass production loop already exists or that its later integration
and EuRoC gates have passed.
