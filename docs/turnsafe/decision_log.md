# TurnSafe v6 decision log

Status: Session 0 contract freeze

Authority: `docs/turnsafe/guidance/IMPLEMENTATION_PLAN_V6.md`

Frozen repository tree: `82504db63fafda40dcf44b8e66cbd29609743a1d`

This log records the decisions that later TurnSafe stages must consume. Section
references below are to the controlling v6 implementation plan. A later stage
may not reinterpret a decision merely because an older plan differs.

## Frozen decisions

| ID | Decision | Frozen consequence | Plan authority |
|---|---|---|---|
| V6-D001 | Source hierarchy | Repository behavior at the frozen tree is implementation ground truth; the v6 plan controls written conflicts. Conflicts are recorded here and are not resolved by implementing both alternatives. | Sections 2 and 15 |
| V6-D002 | Supported production envelope | TurnSafe v1 fails closed unless one-pass Schur, FEJ, `GLOBAL_3D`, `CamRadtan`, fixed intrinsics/extrinsics/time offset, stereo observations, terminal fallback, and target-time stereo range are active. Fixed-two-pass remains reference/ablation only. | Section 4 |
| V6-D003 | Error and innovation convention | Local error is true-minus-estimate. The production residual is measurement-minus-prediction, with residual/Jacobian sign coupled by test. | Sections 8.3 and 12.2 |
| V6-D004 | Acute-regime admission | The translation model is eligible only when `t_UCB < d_LCB`. At or above equality the result is `TRANSLATION_NOT_ACUTE`; no saturated-arcsine substitute is a certificate. In the eligible case, `theta_trans_UCB = asin(t_UCB / d_LCB)`. | Sections 1 and 8.9 |
| V6-D005 | Worst-axis normalization | The bearing-noise scale is `sqrt(lambda_min(Sigma_R))`; therefore `rho_trans = theta_trans_UCB / sqrt(lambda_min(Sigma_R))`. The primary admission threshold is `rho_trans <= 0.5`, with preregistered sensitivity values 0.25 and 1.0. Invalid or ill-conditioned covariance fails closed. | Sections 1 and 8.9 |
| V6-D006 | Joint confidence allocation | The joint certificate target is 0.9973. Translation and range each use confidence 0.99865; the joint guarantee uses a union bound and does not assume independence. Both component and joint empirical coverage are reported. | Sections 1, 8.7, and 8.8 |
| V6-D007 | Translation covariance hedge | Use `P_t_cert = gamma_P * P_t`. The operative default is `gamma_P = 2`. In the authorized isolated-kernel stage, the only permitted selection set is `{1, 2, 4}` and the selected value is the smallest one that meets the translation-component coverage gate in every preregistered synthetic stress family. Selection uses coverage only, never engagement or trajectory benefit, and is frozen before holdout access. | Sections 1, 8.7, 12.2, and 15 (Session 3) |
| V6-D008 | Range support | Every v1 candidate requires a same-feature target-time stereo observation, audited identity/timestamp, fixed calibration, a finite positive range model, and `d_LCB > 0`. Bearing availability without a calibrated range LCB is logged separately and is not admission. | Sections 4, 8.8, and 12.3 |
| V6-D009 | Redundancy-aware consensus | Production groups require at least four eligible features. Two- and three-feature groups are shadow-only. For `n >= 4`, use deterministic Wahba fit plus one deterministic trim/refit; require at least `max(3, ceil(0.75*n))` inliers, rank three, and frozen spatial coverage. | Sections 1 and 8.10 |
| V6-D010 | One winner before NIS | Enumerate and statically certify groups, score without TurnSafe NIS, then freeze exactly one winner. Only that winner receives factor NIS. If it later loses count/rank or fails NIS, coast; never try a runner-up. Foregone eligible groups and predicted opportunity cost are logged. | Sections 8.11 and 12.3 |
| V6-D011 | Failure eligibility boundary | Full NIS rejects, nonfinite/pathological inputs, unclassified refinement failures, reprojection/dynamic outliers, and unsupported inputs are never rescued. T1 eligibility is limited to the typed geometry failures listed by the plan unless a later written review explicitly changes it. | Sections 9.1 and 9.3 |
| V6-D012 | Estimator transaction | One immutable prior feeds full and TurnSafe calculations; one mixed global proposal is accepted or rejected; at most one state/covariance commit occurs; terminal finalization follows the decision; accepted full and TurnSafe observation keys are disjoint. | Sections 5 and 9.3 |
| V6-D013 | Required evidence platform | Required evidence is deterministic paired desktop/offline replay of public and frozen custom bags. Orin Nano runtime/power profiling is optional only after method freeze. Onboard closed-loop flight is a video-only stretch goal and is never a validity or stage gate. | Sections 4, 13.3, 13.4, and 13.8 |
| V6-D014 | Early custom-data calendar | Pre-register 90 events (`3 maneuver classes x 3 severities x 2 exposure conditions x 5 repeats`). The seeded stratified split assigns two events per cell to development (36) and three per cell to holdout (54). August 12 is hardware/sensor shakedown and split/acquisition-order freeze; collection is August 13--14; August 15 is the weather/hardware and sensor-invalid-reshoot buffer; hashes/quarantine are finalized August 16; the evidence branch decision is August 17. Estimator outcome is never a reshoot criterion. | Sections 13.3, 15 (parallel human lane), and 16 |
| V6-D015 | Development/holdout firewall | Sessions 0--6 may receive only development bags/IDs, the development manifest, and the precommitted public split hash. Private membership and raw holdout content remain outside the agent-visible worktree until the explicit Session 7 unlock after algorithm/configuration/threshold freeze. | Sections 7.6, 13.3, and 15 (Sessions 2 and 7) |
| V6-D016 | Extension budget | T1 is followed by at most one of T2 or T3, selected from T0/T1 evidence; selecting neither is valid. T2 and T3 are never pursued simultaneously in the deadline path. Branch B is unspecified and, if selected, requires a dedicated contract-only session before implementation. | Sections 6 (H4), 9.5, 10, 11, and 15 (Session 6) |
| V6-D017 | Outcome and attribution | The headline outcome is preregistered catastrophic turn failure/severity, not a small mean ATE change. The controlled ladder is A0 baseline, A1 certified-selection coast, A2 A1 plus T1, and A3 only if one extension is selected. Every run is reported and comparisons remain paired. | Sections 13.1, 13.5, 13.7, and 13.8 |
| V6-D018 | Session 0 behavior guard | Session 0 may add contracts and additive baseline-digest tooling only. It adds no factor, fallback, estimator gate, altered threshold, or decision-changing diagnostic. Production estimator behavior changed in Session 0: **no**. | Sections 15 (Session 0) and 18 |

## Frozen parameter registry

| Name | Primary value | Permitted preregistered sensitivity or selection | Freeze rule |
|---|---:|---|---|
| `joint_certificate_confidence` | 0.9973 | none | Fixed by V6-D006 |
| `translation_component_confidence` | 0.99865 | none | Fixed by V6-D006 |
| `range_component_confidence` | 0.99865 | none | Fixed by V6-D006 |
| `translation_covariance_inflation` | 2.0 | coverage-only selection from `{1, 2, 4}` | V6-D007; final before holdout |
| `rho_translation_max` | 0.5 | 0.25 and 1.0 | Primary fixed; sensitivities are ablations |
| `min_pair_features` | 4 | 2 and 3 are telemetry only | Fixed by V6-D009 |
| `min_consensus_inliers` | `max(3, ceil(0.75*n))` | none | Fixed by V6-D009 |
| `min_pair_rank` | 3 | none | Fixed by V6-D009 |
| `require_target_stereo_range` | true | none | Fixed by V6-D008 |
| accepted pair groups per callback | at most 1 | none | Fixed by V6-D010 |
| selected extensions | at most 1 of T2/T3 | zero is valid | Fixed by V6-D016 |

The v6 plan intentionally does not supply numerical absolute-angle,
median/MAD, or spatial-coverage thresholds for the deterministic consensus.
Session 0 does not invent or claim them. Session 1 records the complete raw
fit/residual/spatial metrics and marks threshold-dependent pass/fail fields
unavailable until an advisor-authorized threshold set exists. No later than
Session 2, the scientific authority must enter the exact values, boundary
semantics, and immutable threshold-set identifier here. Session 2 then applies
that set offline to the already captured raw T0 records, regenerates the
consensus-survival/valid-group counts, and only then evaluates the T1 gate in
Sections 7.7--7.8. If the set remains absent, the T1 gate cannot pass and no
Session 3 T1-kernel authorization may be issued. Session 3 implements and
tests the same previously frozen values; it does not select them. Any later
authorized pre-holdout change requires a new decision-log entry and complete
development re-evaluation, consistent with Sections 8.10 and 15 (Sessions
2--3).

## Deliberately unresolved items

- Branch B has no implementation contract. Its selection stops the ordinary T1
  sequence and opens the dedicated contract-only session in Section 9.5.
- T2/T3 production flags and scaffolding are unauthorized before the evidence
  selects an extension (Sections 10, 11, and 15).
- Exact external bibliographic records are not supplied by the controlling
  plan. `related_work_matrix.md` freezes coverage, claim boundaries, and
  evaluation disposition without fabricating citation metadata.

## Change procedure

Each later entry must state the authorization token, prior value, new value,
causal evidence, affected tests/artifacts, whether any development results had
already been viewed, and confirmation that holdout access had not occurred.
Once holdout access begins, scientific thresholds and algorithm behavior are
frozen; only correctness fixes with affected evidence rerun are permitted
(Sections 13.3 and 15, Sessions 7--8).

## Entries

| UTC date | Authorization | Entry | Production behavior changed? |
|---|---|---|---|
| 2026-08-12 | `BEGIN_SESSION_0_V6` | Initial v6 contract freeze, V6-D001 through V6-D018. | no |
