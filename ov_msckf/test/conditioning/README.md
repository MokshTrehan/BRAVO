# Deterministic Schur conditioning benchmark

This is a test/analysis harness. It does not change estimator mathematics and
is not a runtime-estimator baseline. It calls the production local
`SchurUpdate::Reduce` and production OpenVINS
`UpdaterHelper::nullspace_project_inplace` routines directly.

## Frozen protocol

- Primary precision: IEEE double, strict target flags (`-fno-fast-math`,
  `-ffp-contract=off`, signed zeros).
- Master seed: `0x434f4e444954494f` (`CONDITIO`).
- Observation rows: `4, 6, 8, 12, 20, 40`.
- State layouts: 39D four-clone fixed calibration, 81D eleven-clone fixed
  calibration, and 110D eleven-clone plus 29 active camera-calibration
  dimensions.
- Landmark spectra: full rank at target condition numbers `1e0, 1e2, 1e4,
  1e6, 1e8, 1e10, 1e12`, plus explicit ranks 3, 2, and 1.
- Algebraic geometry proxies: healthy parallax, very-low parallax, far depth,
  near-collinear rays, repeated views, prewhitened anisotropic image noise, one
  controlled two-pixel outlier, and active/inactive calibration blocks.
- Total: 1,380 fixtures and 4,140 local/upstream-nullspace implementation rows.
  The camera-ray labels are deterministic algebraic Jacobian proxies, not
  replayed OpenVINS camera tracks; this limitation must remain in result
  interpretation.
- Reducer timing: one untimed result call plus nine independently copied timed
  calls; CSV records the median steady-clock nanoseconds. Oracle/posterior work
  is excluded.

Compared rows:

1. `L_SCHUR`: actual local `SchurUpdate::Reduce`.
2. `L_NS`: actual local production in-place Givens nullspace routine.
3. `U_NS`: a second call to the same actual routine, labelled as the exact
   upstream alias. Local and upstream `UpdaterHelper.cpp` are byte-identical:
   SHA-256 `f68fc2e1dd04a6a33e94c576839dc4643114d2100ddb69f1b00da09bd7b12714`,
   Git blob `b36c004f78093b48d483bccef38726eb4f7c7810`.

The required `OV_SOURCE_FAITHFUL_ALGEBRA` reproduction is intentionally not
part of this repository target. The experiment driver creates and hashes that
source only in the external result tree, so selected-source algebra cannot be
mistaken for authoritative estimator code. Its rows are joined to this output
by deterministic fixture identifier.

Independent checks are implemented separately from the compared routines:

- full joint prior-plus-pose-plus-landmark SVD least squares;
- complete-orthogonal-decomposition Moore-Penrose projection;
- full-U SVD nullspace sufficient statistics; and
- covariance-form and information-form posterior solutions.

## Thresholds (declared before results)

Relative error is `||value-reference|| / max(||reference||, 1e-12)` (absolute
value for scalars). Well-conditioned parity requires full numerical rank,
condition at most `1e4`, finite output, information and gradient relative error
at most `1e-8`, increment/covariance/NIS relative error at most `1e-7`, and
scaled PSD validity.

An accepted update is harmful if any prompt-declared condition holds:

- any required output is nonfinite;
- relative state-increment error exceeds `1e-3`;
- relative posterior-covariance error exceeds `1e-3`;
- minimum posterior eigenvalue is below
  `-256*n*epsilon*max(1, max_abs_eigenvalue)`;
- NIS relative error exceeds `1e-3`; or
- an input mutation is unexplained.

The production nullspace function's documented in-place mutation is recorded
as expected, not unexplained. Safe-rejection rate and hard-case acceptance
coverage are both reported so a method is not rewarded for rejecting every
hard case. Hard cases are target condition above `1e6` or exact rank below 3.
Active and inactive calibration-block layouts are separately reported local
diagnostics; the external OV comparison uses only its supported intersection
and records its guard return for the supplemental active-calibration population.

No threshold or fixture population may be changed after inspecting a full
result. A protocol change requires a new named experiment.

## Reproduction

```text
catkin build ov_msckf --no-deps --make-args schur_conditioning_benchmark
schur_conditioning_benchmark NUMERICAL_STRESS_RESULTS.csv NUMERICAL_STRESS_METADATA.txt
/usr/bin/python3 summarize_conditioning.py NUMERICAL_STRESS_RESULTS.csv NUMERICAL_STRESS_REPORT.md
```

Every command must have an outer finite timeout. Generated CSV, metadata, and
reports belong outside Git.
