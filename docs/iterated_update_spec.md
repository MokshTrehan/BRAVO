# Reduced and iterated visual update specification

Status: **blocking draft**
Pinned upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`

Production estimator code must not depend on this document until every section
is complete, equation symbols are dimensioned, source conventions are cited,
and CP1 tests encode the decisions.

## Required contents

1. Predicted state, covariance, and manifold error definition.
2. Visual residual, state Jacobian, landmark Jacobian, and whitening.
3. Exact relationship between the OpenVINS nullspace/compression baseline and
   the proposed Schur-reduced system.
4. Landmark block factorization, rank/conditioning test, and rejection policy.
5. Reduced information vector/matrix and landmark back-substitution.
6. One-pass state increment and posterior covariance/reset.
7. Frozen-prior iterated-error correction on the manifold.
8. Pass-2 re-triangulation, residual/Jacobian, robust weights, gate, and
   accepted-feature policy.
9. Exactly-once final covariance and reset at the accepted final iterate.
10. Failure, reversion, regularization, and diagnostic behavior.

## Decisions still blocking implementation

- Mathematical symbols and dimensions: **TBD**
- Whitening and measurement covariance: **TBD**
- Schur factorization and conditioning threshold: **TBD**
- Equivalence target versus nullspace projection: **TBD**
- Frozen-prior manifold correction: **TBD**
- FEJ/linearization policy per quantity: **TBD**
- Cross-pass feature/gating policy: **TBD**
- Final covariance and reset equations: **TBD**
- Pass-2 acceptance/reversion rule: **TBD**

## Required tests

- Full/Schur state and landmark increment equivalence.
- Full/Schur posterior covariance equivalence.
- Singular and ill-conditioned landmark rejection.
- Projection and state Jacobians against central finite differences.
- Linear one-pass/two-pass equality.
- Nonlinear true-cost reduction without double-counting measurements.
- Frozen-prior and exactly-once covariance assertions.
- Symmetry, finite-value, PSD, reset, NIS, and observability checks.
