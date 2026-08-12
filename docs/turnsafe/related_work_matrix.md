# TurnSafe related-work matrix

Status: Session 0 scope and claim-boundary freeze

Authority: v6 plan Sections 11.2, 13.1--13.2, and 17

This matrix freezes what must be compared, how it may be positioned, and which
external systems may enter evaluation. It is not permission to implement an
external method or a later TurnSafe extension.

The controlling plan names research lineages but does not provide authoritative
bibliographic metadata for every item. To avoid inventing authors, titles,
years, or code availability, handles marked `PLAN_NAMED` must be resolved
against a primary paper and, where relevant, its official repository before
manuscript use. The coverage and claim boundaries below are frozen now; exact
bibliography records are a paper-production task.

## 1. Required literature coverage

| Handle / lineage | Question the primary source must answer | Comparison axes to extract | TurnSafe positioning boundary | Sprint disposition | Citation status |
|---|---|---|---|---|---|
| MSCKF and the repository's OpenVINS lineage | How are feature tracks eliminated, clone errors defined, measurements signed, and visual updates gated? | Structureless elimination, clone support, FEJ/current linearization, NIS, lifecycle, mutation boundary | Repository behavior at the frozen tree is the baseline implementation ground truth. TurnSafe augments its one-pass Schur path; it does not claim to invent MSCKF, FEJ, or structureless elimination. | A0 baseline and implementation context | Repository-verifiable; exact paper record still required |
| First-estimate Jacobian / observability-consistent filtering | Which linearization points preserve the intended unobservable directions, and what consistency claims are supported? | Current residual versus FEJ Jacobian, global-position/global-rotation nullspace, NEES/NIS evidence | TurnSafe follows the repository's affine-FEJ convention. It does not claim exact consistency merely because FEJ is used. | Cite and test; no reimplementation | Primary-source verification required |
| PO-MSCKF | What “pose-only” measurement/state support is actually used, and what depth or translation assumptions remain? | Residual dimension, direct state columns, feature/depth dependence, translation sensitivity, gating, update transaction | Use as prior pose-only/lower-dimensional filtering context. Do not claim TurnSafe is the first pose-only MSCKF or conflate direct orientation support with an orientation-only state update. | Cite-only unless maintained code is verified | `PLAN_NAMED` |
| POPL-KF | What pose-only or pure-rotation formulation, admission conditions, and observability claims are made? | Same axes as PO-MSCKF plus degeneracy handling and uncertainty model | Use to bound novelty honestly. TurnSafe's distinguishable claims must be the causal typed-failure trigger, joint acute certificate, group policy, and exact-once mixed transaction—not generic pose-only estimation. | Cite-only unless maintained code is verified | `PLAN_NAMED` |
| RD-VIO | What failure regime and robust/degeneracy mechanism are addressed, and under which sensors/configuration? | Frontend/updater locus, triggering signal, supported cameras, range/depth reliance, public code/data, evaluation regimes | Treat as an external VIO comparator only after its mechanism and configuration are verified. No claim of superiority from an incompatible or reconstructed implementation. | Run only if public code builds faithfully; otherwise cite | `PLAN_NAMED` |
| S-MSCKF | What structural or selective MSCKF modification is proposed and which failure modes are measured? | Factor construction, feature lifecycle, gating, FEJ/observability, datasets, code/configuration | Compare at the mechanism and evidence level after identity verification. Do not infer equivalence from the acronym or reimplement it during the sprint. | Run only if public code builds faithfully; otherwise cite | `PLAN_NAMED` |
| SchurVINS | How is Schur-complement structure used, and what efficiency/statistical claims depend on it? | Elimination order, prior reuse, numerical equivalence, runtime, factor mixing | Establish that Schur elimination is prior art/context. TurnSafe's contribution is not “using Schur”; it is certified conditional substitution under one captured prior and one transaction. | Cite/context; no sprint reimplementation | `PLAN_NAMED` |
| Gyro-aided KLT / rotation-compensated tracking | How is IMU rotation integrated over the camera interval and used to predict or warp feature locations? | Timing/extrinsics, distortion model, seeding versus gating, determinism, rolling-shutter assumptions | Relevant only if T0 selects T3. It informs gyro-predicted initialization but does not authorize threshold/model changes in T0/T1. A better KLT error alone is not a success claim. | Citation study now; implementation only after T3 authorization | `PLAN_NAMED` |
| Degeneracy-aware VIO filtering and solution remapping | How is degeneracy detected, what solution/state subspace is retained or remapped, and what causal evidence triggers it? | Detection statistic, residual dependence, unobservable directions, covariance treatment, state mutation, recovery | Contrast state/solution remapping with TurnSafe's selective measurement-model substitution. Branch B remains unspecified; related work cannot fill that contract implicitly. | Cite; no implementation by analogy | `PLAN_NAMED` |
| GRIC and geometric model-selection lineage | How are competing geometric models scored without post-selection gate shopping? | Model hypotheses, complexity penalty, causal inputs, degeneracy, tie handling, selection timing | Relevant to a separately reviewed T3 correspondence-model contract only. TurnSafe v1 group selection is deterministic and freezes one winner before NIS; it does not claim to implement GRIC. | Cite; T3-only design input | `PLAN_NAMED` |
| ORB-SLAM homography-versus-fundamental initialization lineage | How are homography and fundamental hypotheses compared and how are planar/low-parallax cases handled? | Score definition, parallax/degeneracy checks, deterministic selection, failure behavior | Provides model-selection precedent for possible T3 work. It does not authorize replacing the existing fundamental-matrix path during T0. | Cite; T3-only design input | `PLAN_NAMED` |
| Stereo range uncertainty / one-sided lower bounds | How are stereo identity, geometry, calibration, and pixel uncertainty propagated into conservative range support? | Range definition, covariance propagation, singularity handling, one-sided coverage, calibration sensitivity | TurnSafe's residual is depth-free, but admission is not depth-independent: target-time stereo and a calibrated positive `d_LCB` are mandatory. | Primary-method context and oracle design; exact sources to resolve | Coverage obligation inferred from Sections 8.8 and 17 |
| Robust estimation and consensus | How do deterministic robust fits treat correlated motion bias and small groups? | Minimum sample/redundancy, trim/refit policy, spatial/rank tests, deterministic ties | NIS is not a substitute for the acute/range certificate. Production needs four features and redundancy-aware consensus; two/three are shadow-only. | Cite context; use only the frozen TurnSafe contract | Coverage obligation inferred from Sections 8.9--8.12 |

## 2. Frozen comparison axes

Every reviewed paper/system receives a row in the manuscript working matrix
with the following fields. `unknown` is preferable to an inference from a name
or abstract.

| Axis | Values/evidence to record |
|---|---|
| Estimator locus | frontend, feature initializer, updater, state remapping, or multiple |
| Trigger | causal upstream outcome, residual/NIS, learned score, heuristic, always active, unknown |
| Feature lifecycle | active prefix, terminal track, persistent landmark, keyframe/multiview, unknown |
| Measurement form | residual dimension and exact measured/predicted quantities |
| Direct state support | state columns with direct Jacobian support; separately note covariance cross-effects |
| Depth/range dependence | residual dependence and admission dependence recorded separately |
| Translation treatment | ignored, estimated, bounded, marginalized, or folded into noise; note correlation model |
| Uncertainty/certificate | confidence level, calibration evidence, failure-closed conditions |
| Linearization | current, FEJ, affine surrogate, or unknown |
| Selection/gating | ordering of static validity, model selection, consensus, NIS, and global validation |
| Ownership | whether an observation can enter competing factors or repeated updates |
| Mutation semantics | prior capture, proposal count, commit count, rollback/finalization behavior |
| Supported configuration | camera model, mono/stereo, calibration state, pass count, platform |
| Evidence | synthetic consistency, public data, paired custom data, nominal regression, runtime |
| Reproducibility | primary paper verified, official code/config available, build reproduced, or cite-only |

For TurnSafe, the corresponding frozen row is:

| Axis | TurnSafe v1 value |
|---|---|
| Estimator locus | terminal-track updater fallback after a typed full-geometry failure |
| Trigger | declared eligible full outcome plus target-stereo/range/translation/acute/rho/group checks |
| Feature lifecycle | detached terminal tracks in T1 |
| Measurement form | 2D target-tangent rotation-bearing innovation per feature |
| Direct state support | source/target clone-orientation columns; cross-covariance can affect other states |
| Depth/range dependence | residual depth-free; admission requires target-time stereo range LCB |
| Translation treatment | shared bounded model error; not independent per-feature pixel noise |
| Uncertainty/certificate | 0.9973 joint target via two 0.99865 component bounds and union bound |
| Linearization | current residual with clone FEJ Jacobians under the repository convention |
| Selection/gating | static certificate and four-feature consensus, one group frozen before NIS, then one mixed proposal |
| Ownership | accepted full and TurnSafe observation keys disjoint |
| Mutation semantics | one captured prior, one accepted proposal, at most one commit, terminal finalization after decision |
| Supported configuration | frozen one-pass Schur/FEJ/`GLOBAL_3D`/`CamRadtan`/fixed-calibration/stereo envelope |
| Evidence | internal A0/A1/A2(/A3) paired offline ladder, consistency/coverage tests, nominal non-inferiority |

The TurnSafe row restates the comparison coordinates defined in Sections 4,
5, 8, 9, 13, and 17; it does not authorize their implementation in Session 0.

## 3. External-system evaluation disposition

| System/lineage | Permitted evidence role | Reproducibility gate |
|---|---|---|
| Frozen SchurVIO-Lite | Required A0 and internal ladder baseline | Exact frozen tree, build/config/calibration, deterministic digest |
| OpenVINS | Context | State the exact relationship/configuration; do not substitute it for A0 |
| RD-VIO | Optional external comparison | Run only when public code builds faithfully with documented configuration |
| S-MSCKF | Optional external comparison | Run only when public code builds faithfully with documented configuration |
| PO-MSCKF / POPL-KF | Related-work context | Cite-only unless maintained code and a faithful supported path are verified |
| SchurVINS | Related-work/Schur context | No baseline reimplementation during the sprint |
| T3 lineages | Mechanism context only | No implementation unless Session 6 selects T3 after T0/T1 evidence |

The headline experiment remains the A0/A1/A2 internal ladder, plus A3 only if
one extension is selected. An unavailable external baseline is reported as
unavailable and never repaired by an in-sprint reimplementation (Sections 13.1
and 13.2).

## 4. Claim/evidence fence

| Claim area | Defensible wording | Required evidence | Wording excluded by the v6 contract |
|---|---|---|---|
| Failure mechanism | T0 causally localizes losses along the observed frontend-to-update funnel | Absolute funnel counts across repeatable events/severities | A turn failed “because of depth” without funnel evidence |
| Residual | Depth-free rotation-bearing residual with direct clone-orientation support | Geometry/Jacobian/noise tests | First pose-only MSCKF; first pure-rotation VIO; orientation-only state update |
| Complete method | Admission uses stereo range and translation uncertainty | Range/translation component and joint coverage | Depth-independent method |
| Validity | Joint-confidence certificate in the strict acute regime | Boundary sweeps and calibrated component/joint coverage | Worst-case bound at `t_UCB >= d_LCB`; NIS-guaranteed validity |
| Consistency | Repository-aligned FEJ surrogate preserves tested invariances under the supported contract | Finite differences, nullspace tests, NIS/NEES | FEJ alone guarantees exact consistency |
| Model selection | One statically certified group is frozen before NIS; no runner-up gate shopping | Deterministic selection and foregone-group tests | Best-NIS group selection or rescue after winner failure |
| Transaction/ownership | Full and fallback rows share one prior/proposal/commit with disjoint accepted observations | Explicit-stack oracle, audit, fault injection, double-counting ablation | Sequential same-prior updates are equivalent; prefix nonreuse equals joint landmark elimination |
| Robustness | The frozen method moves the catastrophic-failure boundary on paired recorded bags if the preregistered gate passes | Paired development/holdout results, severity strata, every failure included | Superiority from cherry-picked successful runs or small average ATE alone |
| Platform | Required contribution is reproducible desktop/offline replay | Runtime/memory plus paired bag evidence | Orin or onboard flight is required proof; optional demo implies estimator validity |

These boundaries follow Section 17's contribution statement and claims to
avoid. Any stronger manuscript claim requires a new decision-log entry and
evidence review before holdout access.

## 5. Bibliography verification checklist

Before a handle is cited in the manuscript, record outside this contract:

1. primary-source title, authors, venue/year, DOI or stable identifier;
2. the exact passage/equation supporting the matrix entry;
3. official code/release URL and immutable revision when executable evidence is
   claimed;
4. supported camera/sensor/configuration and licensing constraints;
5. whether the result is a faithful run, cite-only context, or unavailable;
6. any corrected comparison-axis value, with a claim audit if it changes
   positioning.

Bibliographic correction does not authorize algorithm copying, external code
integration, or T3/Branch-B work. Those remain subject to the stage gates in
Sections 9.5, 11, and 15.
