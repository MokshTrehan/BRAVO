# Proposed CP2-D alignment-uniqueness clarification

Status: **proposed, non-authorizing, and required before source freeze**

Recorded-input access used for this finding: **none**

Date: 2026-08-04

## Mathematical defect

The current CP2-D alignment rule checks only that the centered nullspace/source
position matrix has numerical rank at least two. That is necessary but not
sufficient to make the source-to-ground-truth Kabsch rotation unique. A
full-rank source paired with coincident ground-truth positions has
cross-covariance `C=0`; a rank-one target can similarly make `rank(C)=1`.
`det(UV^T)` remains normally `+1` or `-1`, so the existing determinant-sign
test does not reject either case. The returned rotation is then an arbitrary
choice of SVD bases rather than a uniquely data-determined alignment.

Rank two alone is also not a complete uniqueness test for every full-rank
cross-covariance. If `det(UV^T)<0`, the proper-rotation solution must reverse
one singular direction. When `c_2=c_3>0`, every direction in the repeated
smallest-singular-value subspace is an equally optimal correction. For
example, the orientation-reversing synthetic covariance
`diag(3,1,-1)` has singular spectrum `(3,1,1)` and multiple distinct optimal
proper rotations. A rank-at-least-two-only replacement would still accept
that nonunique case.

A wholly synthetic adversarial construction reproduced this defect with no
registry, bag, project ground truth, result, or recorded trajectory access.
The prior claim that source rank alone prevents an arbitrary alignment is
therefore false. CP2-D source freeze and recorded execution remain stopped.

## Replacement mathematical contract

The existing source-spectrum gate remains unchanged. In addition, the exact
3-by-3 cross-covariance

`C=(1/N) sum_k (y_k-y_bar)(x_k-x_bar)^T`

must retain a complete finite descending nonnegative singular spectrum
`c_1>=c_2>=c_3>=0`. With binary64 arithmetic, define

`cross_rank_threshold = max(N,3) * epsilon * c_1`.

This is the frozen reproducible numerical-admissibility margin shared with the
existing source-rank policy. It is not claimed to be a certified forward-error
or singular-value interval bound under arbitrary centering and cancellation.

Let `d=det(UV^T)` and retain
`reflection_correction_applied = (d<0)`. Require

1. `d` is finite and `abs(abs(d)-1)<=1e-10`, so the branch is an observed
   orthogonal sign;
2. `c_1 > std::numeric_limits<double>::min()`;
3. the strict numerical-rank boundary `c_2 > cross_rank_threshold`; and
4. if `reflection_correction_applied`, the left-to-right binary64 subtraction
   `smallest_gap=c_2-c_3` must satisfy the strict boundary
   `smallest_gap > cross_rank_threshold`.

Equality fails at either strict boundary. In exact SO(3) Procrustes
mathematics, the rotation is unique exactly when `c_2>0` and either `d>0` or
`c_2>c_3`; the two numerical tests above are the frozen finite-precision
counterparts. A nonfinite or incomplete spectrum, rank-zero or rank-one
cross-covariance, coincident or collinear target, an orientation-reversing
repeated-smallest-singular-value spectrum, or failed SVD rejects the sequence
before transform application or metric evaluation. No fallback,
regularization, alternate alignment, repair, or threshold adaptation is
permitted.

The same SVD factors and spectrum feed the already frozen
`R=U diag(1,1,sign(det(UV^T))) V^T` expression. Association, common-alignment
reuse, translation, quaternion convention, ATE population, RMSE, p95,
thresholds, sequences, offsets, and pass/fail limits otherwise remain
unchanged.

## Evidence and protecting requirements

The direct-math response and `baseline_alignment` report record must add exact
`cross_covariance_singular_values` and
`cross_covariance_rank_threshold` fields plus the exact Boolean
`reflection_correction_applied`. Assembly and detached verification
must independently recompute all of them from the exact shared population and require
binary64 equality with the retained direct-capsule response. The direct-math
known-answer bundle must retain both spectra, both thresholds, and the exact
`+1`/`-1` determinant-correction sign for its two Kabsch cases.

Protecting tests must include at least:

- a full-rank source with a coincident target;
- a full-rank source with rank-one cross-covariance;
- equality at the strict second-singular-value threshold;
- the next representable binary64 value above that boundary;
- an orientation-reversing full-rank covariance with `c_2=c_3`;
- equality at the strict reflection-gap threshold and the next representable
  accepted gap;
- an accepted orientation-reversing case with a unique smallest singular
  direction and the correction marker set;
- a finite determinant-correction value that is not within `1e-10` of `+1` or
  `-1`;
- retained spectrum and threshold substitution; and
- a complete worker request that must fail before emitting a response.

## Approval boundary

The chained authorization permits additional corrections only when they do not
change a frozen acceptance boundary. This repair necessarily adds a rejection
boundary for previously accepted but mathematically non-unique alignments.
Therefore this document and its data-free implementation candidate do not
authorize source freeze, the formal unit gate, readiness, or recorded access.
Moksh Trehan must explicitly approve the exact committed clarification and
candidate before the chain may resume. No exceptions.
