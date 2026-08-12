# TurnSafe-MSCKF v6 Evaluation Protocol

**Status:** Session 0 protocol freeze

**Applies to:** development evaluation in Sessions 1--6 and the single frozen
holdout evaluation authorized in Session 7

**Controlling source:**
[`IMPLEMENTATION_PLAN_V6.md`](guidance/IMPLEMENTATION_PLAN_V6.md), especially
Sections 1, 4, 6, 8.7--8.12, 12, 13, 15--16, and 18.

This document freezes the evaluation design. It does not authorize estimator
changes, holdout access, or execution of a later session.

## 1. Frozen scientific question and evidence boundary

The primary question is whether the final frozen TurnSafe configuration moves
the catastrophic-turn failure boundary without materially degrading nominal
performance. The required evidence is deterministic, paired desktop/offline
replay of public data and recorded custom bags. Every compared configuration
uses the same bag, codebase, frontend, calibration, initialization, and
non-TurnSafe parameters (plan Sections 13.1 and 13.3).

Jetson Orin Nano runtime/power profiling is optional context and begins only
after the required desktop, public-data, and custom-holdout runs are complete.
Onboard closed-loop flight is a video-only stretch goal. Neither is a
contribution gate, a success gate, or a substitute for paired offline evidence
(plan Sections 4, 13.4, and 13.8).

The production method contains T1 plus **at most one** evidence-selected
extension, T2 or T3. Selecting neither is valid. Simultaneous T2+T3 results are
outside the confirmatory ladder (plan Sections 6/H4, 13.1, and 15/Session 6).

## 2. Configuration ladder and attribution

The preregistered ladder is:

| Label | Frozen meaning |
|---|---|
| A0 | Frozen SchurVIO-Lite baseline. |
| A1 | The A2 selection/certificate policy, but coast whenever A2 would add TurnSafe rows. |
| A2 | A1 plus the T1 TurnSafe factor. |
| A3 | A2 plus the single selected extension, if Session 6 selects one. |

If no extension is selected, A3 is absent and A2 is the final method. The
headline system comparison is final method versus A0. A1 versus A2 is the
required causal factor-attribution comparison. If A3 exists, A2 versus A3 is
the extension-attribution comparison. Every configuration present in the
frozen ladder must preserve a paired event key and expose failures rather than
silently dropping runs.

The preregistered ablations are shadow-only mode, no certificate, no consensus,
translation-ratio thresholds 0.25/0.5/1.0, translation-covariance inflation
1/2/4, and deliberate double counting in an offline test executable only.
Ablations are explanatory and cannot replace the frozen primary comparison or
be used to select a holdout winner (plan Sections 8.9, 12.6, and 13.1).

## 3. Frozen admission contract reported by evaluation

Every A1/A2/A3 report must preserve and audit these definitions from plan
Sections 1 and 8.7--8.12:

- The total translation/range certificate confidence is 0.9973. Translation
  and range use component confidences 0.99865 each; joint coverage is claimed
  by the union bound, without an independence assumption.
- The certified translation covariance is
  `P_t_cert = gamma_P * P_t`. The default `gamma_P` is 2. The only selection
  set is `{1, 2, 4}`; Session 3 may select the smallest value that meets the
  translation-component coverage gate in **every** preregistered synthetic
  stress family. Selection is based on coverage only, never engagement,
  trajectory error, or failure-rate benefit, and is final before holdout
  access.
- A candidate is eligible only when `t_UCB < d_LCB`. When that strict
  inequality fails, it is ineligible; no saturated arcsine is evaluated or
  reported as a bound.
- In the acute regime,
  `theta_trans_UCB = asin(t_UCB / d_LCB)` and the conservative bearing scale is
  `sqrt(lambda_min(Sigma_R))`. Thus
  `rho_trans = theta_trans_UCB / sqrt(lambda_min(Sigma_R))`. The primary gate
  is `rho_trans <= 0.5`; 0.25 and 1.0 are sensitivity values only.
- A production pair group requires at least four eligible features and the
  frozen deterministic trim/refit, spatial-coverage, inlier, and rank-three
  consensus checks. Two- and three-feature groups are shadow-only. For
  `n >= 4`, consensus requires at least `max(3, ceil(0.75*n))` inliers.
- Exactly one group is frozen before TurnSafe NIS. A failed winner never causes
  runner-up gate shopping. Foregone eligible groups and predicted information
  are reported.

Reports must include translation-component, range-component, and joint
coverage separately; acute and worst-axis boundary sweeps; factor NIS and
synthetic state NEES; admission/rejection counts and reasons; and consensus
survival by group size. Engagement alone is not evidence of calibration.

## 4. Dataset roles and pairing

### 4.1 Public data

- EuRoC is the frozen nominal-regression dataset.
- KAIST enters quantitative evaluation only if the Session 0 compatibility
  report proves the supported camera/calibration/reader/ground-truth path with
  an exact successful baseline command. Otherwise KAIST is reported as a
  classified blocker and is not silently replaced or repaired as a legacy
  lane.
- OpenVINS is contextual. RD-VIO and S-MSCKF are quantitative external
  baselines only when their public code builds faithfully. PO-MSCKF/POPL-KF are
  cite-only unless maintained code is available. No sprint-time baseline
  reimplementation is evidence (plan Section 13.2).

### 4.2 Custom airborne data

The custom design is 90 events:

```text
3 maneuver classes x 3 severity bands x 2 exposure conditions x 5 repeats
```

The stratified split assigns two repeats per cell to development (36 events)
and three per cell to holdout (54 events). Acquisition and quarantine follow
[`flight_collection_protocol.md`](flight_collection_protocol.md). Sessions
1--6 may use only the development manifest and the precommitted public split
hash. This document intentionally contains no seed, private membership, event
identifier, or private path.

Each configuration is replayed against the exact same immutable bag and
reference data. The paired key is the custodian-issued event identity after
unlock; before unlock, development reports use only development identities.
Calibration, estimator configuration, executable, source tree, evaluator, and
input hashes accompany every result.

## 5. Holdout firewall and one-shot evaluation

Until the literal authorization token `APPROVE_SESSION_7_HOLDOUT_UNLOCK` is
issued, the private manifest, private acquisition records, holdout identities,
holdout raw data, and any derived holdout output are unavailable to algorithm
work. They are not mounted, listed, searched, inspected, copied, rehashed,
summarized, plotted, or evaluated. Only the already committed public split
commitment and development manifest may be consumed (plan Sections 7.6,
7.8, 13.3, and 15/Session 7).

Before unlock, freeze and hash:

1. source commit and clean tracked tree;
2. executable/build manifest and dependency versions;
3. all A0--final-method configurations and random seeds;
4. certificate, consensus, NIS, failure, and non-inferiority thresholds;
5. evaluator implementation and output schema;
6. dataset/reference/calibration commitments;
7. ordered run matrix and statistical analysis script.

After unlock, run the complete frozen matrix once. Infrastructure failures may
be rerun only from the same immutable inputs/configuration, with the original
failed attempt retained and a reason recorded. A scientific failure is a
result, not an infrastructure rerun. Holdout outcomes cannot motivate a new
threshold, extension, configuration, exclusion, or winner. A correctness fix
requires refreezing and rerunning every affected result, with both versions
preserved (plan Sections 15/Session 7--8 and 18).

## 6. Outcomes and preregistration record

### 6.1 Primary outcome

The primary event-level binary outcome is catastrophic turn failure. It is the
logical OR of preregistered, mechanically evaluated conditions:

1. estimator reset, termination, divergence flag, or nonfinite pose/covariance;
2. orientation error above the frozen angle threshold for the frozen duration;
3. usable pose-stream interruption beyond the frozen duration; or
4. failure to resume ordinary full Schur updates within the frozen interval
   after the maneuver.

The v6 plan does not supply the numeric angle, duration, and interval values
named above. They therefore have no implicit default: the scientific authority
must enter them, their time reference, and exact boundary semantics (`>` versus
`>=`) in the hashed preregistration record before the first confirmatory replay
using reference truth. If any value is absent, the confirmatory primary outcome
is not defined and no primary claim may be made. The definition is then
immutable through holdout evaluation. A small mean ATE change is never
substituted as the headline outcome (plan Section 13.5).

### 6.2 Secondary outcomes

Report success probability by severity; yaw and tilt error through the turn;
maximum transient orientation error; time to resume full Schur updates; ATE and
RPE on successful runs; full/TurnSafe factors and rows; target-range,
certificate, consensus, and NIS rejection reasons; factor NIS; synthetic state
NEES; desktop runtime percentiles and memory; nominal non-inferiority; and,
when separately available, optional Orin Nano runtime/power (plan Section
13.6).

The evaluator freezes coordinate frames, alignment, trajectory interpolation,
time windows, warm-up removal, error aggregation, percentile definitions, and
missing-sample handling before confirmatory runs. No trajectory segment is
discarded because it looks poor.

### 6.3 Run disposition

Every scheduled run appears in the result ledger. A crash, reset, nonfinite
output, missing required pose interval, or evaluator-detectable divergence is a
primary failure. Continuous outcomes unavailable because of that failure are
marked unavailable with the failure reason and retained in denominators; they
are not imputed as successful values. Exclusion is allowed only for a
preregistered, outcome-blind sensor-quality failure from the flight protocol.

## 7. Frozen statistical analysis

The event is the analysis unit and every comparison is within-event paired.
Report raw 2-by-2 paired counts and all denominators.

- Use the exact two-sided McNemar test for paired catastrophic success/failure.
  The confirmatory comparison is final method versus A0. Report A2 versus A1,
  and A3 versus A2 when applicable, as attribution comparisons with their raw
  discordant counts.
- Use paired bootstrap confidence intervals for continuous error and recovery
  differences. Resample paired events within the 18 preregistered
  maneuver/severity/exposure cells; preserve the same sampled event on both
  sides. The bootstrap seed, replicate count, interval type, and summary
  statistic are frozen in the preregistration record.
- Report Wilson intervals, absolute numerator/denominator counts, and severity
  strata for failure rates. Do not report percentages without counts.
- Report every failed run. No post-hoc exclusion, subgroup, severity merge,
  direction change, or outcome redefinition is permitted outside the
  preregistered sensor-quality rules.

The protocol makes one confirmatory system comparison. Attribution,
sensitivities, ablations, and subgroup analyses are labeled secondary or
exploratory; their unadjusted intervals or p-values are not promoted to the
confirmatory claim. Any additional multiplicity procedure must be named and
frozen before unlock, not selected from holdout results (plan Section 13.7).

## 8. Decision gates

The final evidence package records pass/fail for every gate in plan Section
13.8:

- disabled and capture parity are exact;
- median ATE degradation on frozen nominal sequences is no more than 5%;
- holdout failure reduction is at least 20 percentage points versus A0, or the
  frozen analysis demonstrates a clear increase in failure-severity threshold;
- translation, range, and joint certificate coverage gates pass;
- certified synthetic motion shows no NIS/NEES inconsistency;
- the benefit is causally attributable to T1 or the one selected extension;
- frozen desktop/offline runtime overhead is measured and acceptable under the
  preregistered budget.

The 5% nominal median-ATE limit and 20-percentage-point holdout improvement
criterion above are already numerically frozen by plan Section 13.8. They are
not among the still-unsupplied catastrophic-event thresholds identified in
Section 6.1 of this protocol.

The runtime budget and any operational definition of a "clear" severity
threshold increase must be explicit in the preregistration record before
confirmatory evaluation; neither has an inferred default. Optional Orin Nano
or onboard evidence cannot change a gate outcome.

## 9. Required reproducibility record

For every evaluation batch, retain exact commands, UTC times, host/toolchain
versions, source and tree identity, build/config/evaluator hashes, immutable
input/reference/calibration hashes, environment variables affecting execution,
run order, exit status, complete stdout/stderr, per-run result rows, exclusions
and reasons, and aggregate outputs. Stable result tables and plots must be
regenerable solely from this record. Wall-clock text and other declared
nondeterministic fields are never mixed into deterministic parity digests.
