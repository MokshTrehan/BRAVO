# SchurVIO-Lite ICRA 2027 execution plan

## Document control

| Field | Value |
|---|---|
| Plan version | `0.1.0` |
| Plan status | `ACTIVE -- Phase A in progress` |
| Approved scope | Local Phase A readiness work; 2026-08-15 |
| Target venue | IEEE ICRA 2027 main track, conditional on G3 |
| Submission deadline | 2026-09-15 23:59 Pacific; internal upload target 2026-09-14 |
| Timezone for this plan | `America/Toronto` unless explicitly marked Pacific |
| Source repository | `https://github.com/MokshTrehan/schur-vio-lite.git` |
| Frozen A0 commit | `4d2f2d275437ead9496b848730d5a9eb0e402864` |
| Frozen A0 tree | `7c390aeb126f1ef69aad75733f40559639889ed3` |
| Local frozen tag | `icra27-a0-base` |
| Working branch | `schurvio-lite/icra-robust-core` |
| Authoritative worktree | `/home/moksh/schurvio-lite-icra27-a0` |
| Prohibited worktree | `/home/moksh/newSlam turnsafe-primary` (dirty T1 work; do not clean, reuse, or modify) |
| Decision owners | Moksh Trehan; primary execution agent records evidence and recommendations |

This file is the authoritative plan and progress ledger. It supersedes the
review pack wherever the pack conflicts with this document. In particular,
KAIST uses its native 30 Hz / 33.3 ms period, the GO/NO-GO rule below is
exhaustive, N0g is required for Schur-specific attribution, and required
review evidence must fit in the eight-page paper rather than a supplement.

## Authority and change control

The current authorization permits local, reversible Phase A work: source
edits on the working branch, builds, tests, local worktrees, dataset and
external-system inventory, and downloads of confirmed missing datasets or
test systems. Before any download, the executor must check existing local
copies, verify identity where a checksum is available, record URL/version/
license/size, and avoid duplicate acquisition. Official or upstream sources
are preferred; mirrors require a recorded reason.

The current authorization does not include pushing, opening a pull request,
changing remote branch protection, publishing artifacts, deleting existing
data, cleaning another worktree, or changing an external service. These need
separate approval.

Any estimator-mathematics change outside the narrow N0g comparator requires a
written decision entry before implementation. Any post-freeze correctness
change creates a new source identity and invalidates every affected run.
Thresholds, sequence selection, and claim wording may not be relaxed after
results are observed. Because these datasets and earlier results have already
been inspected, this plan uses "prospectively frozen," not "preregistered."

## Decision summary

The strategic pivot, matched controls, immutable evidence, bounded claims,
and early kill gates are retained. The earlier `MINOR REVISION -> PROCEED`
ruling is replaced by `MAJOR REVISION -> CONDITIONAL`.

Only Phase A is currently authorized. The full campaign is not authorized
until G0 passes and the bounded causal falsification in Phase B finds a useful
S1-versus-N0g signal. Prior evidence adverse to a Schur-specific efficiency or
robustness claim must remain visible in the evidence registry and paper
decision.

## Status vocabulary

- `execution_status`: `NOT_STARTED | READY | RUNNING | BLOCKED | DONE | CUT`
- `evidence_status`: `PENDING | PREVIEW | SUPPORTED | REFUTED | INVALIDATED`
- `gate_status`: `PENDING | PASS | FAIL | OVERRIDDEN`

An override never becomes a gate pass. Every override records its author,
timestamp, rationale, affected claims, and required reruns.

## Scientific hypotheses

### H0 -- observational neutrality

Configuration plumbing, telemetry, map capture, and report generation do not
change estimator outputs. Trajectory/state identity is required where byte
identity is meaningful; otherwise an exact, prospectively frozen numerical
tolerance and rationale must be recorded before use.

### HC -- correctness prerequisite

S1 completes the primary protocol without a new catastrophic failure and is
accuracy-noninferior to the matched controls. Invalid precommit proposals do
not mutate declared live estimator or feature state. Mid-commit exceptions
are either proved no-throw/rollback-safe or explicitly excluded from the
atomicity claim.

### HA -- method-specific attribution

On identical backend inputs, S1 provides a material efficiency, numerical, or
completion advantage over N0g. Only N0g-versus-S1 isolates the update
formulation; N0-versus-S1 measures the bundled update-path effect.

### HS -- embedded operating-point value

Any HA advantage survives end-to-end execution on the frozen Jetson Orin Nano
profile at the dataset's native input period, without worse accuracy or
completion and without hidden queue drops or backlog.

### HR -- bounded fault containment

For the prospectively enumerated fault model, a failed update causes zero
mutation across all declared mutable estimator and feature state, finalizes at
most once, and is followed by a successful next valid update. A natural
differential incident is required for wording about preventing corruption in
practice.

### HE -- external validity

The observed effect generalizes across prospectively frozen sequences or
datasets and at least one credible independent system completes a shared,
admissible protocol. A documented configuration failure is a reproducibility
result, not positive evidence of estimator superiority.

## Canonical systems and attribution

| ID | Definition | Permitted inference |
|---|---|---|
| N0 | Local one-pass nullspace path with the frozen A0 frontend and the already-common preview/commit transaction | Reference bundled path |
| N0g | N0 plus S1's typed rank/condition guard; it retains the same already-common transaction | Guard effect from N0->N0g; formulation effect from N0g->S1 |
| S1 | Guarded, transaction-wrapped, one-pass Schur update | Primary candidate |
| S2 | Frozen two-pass Schur ablation | Existing supporting ablation; no new broad campaign unless a gate requires it |
| N0p | Upstream OpenVINS algorithm defaults with only unavoidable correct dataset calibration, topics, and adapter bindings | Context, not a matched causal control |
| VF | VINS-Fusion variant(s) prospectively named from the dataset authors' configuration | Mandatory two-day engagement; success or documented failure ends the timebox |
| EXT | At least one completing independent public implementation on a shared supported dataset | External context |

Machine-readable resolved-parameter diffs must prove:

- N0 -> N0g changes only the authorized guard axis; the transaction is already
  common and is not an experimental delta.
- N0g -> S1 changes only the elimination/update-formulation axis.
- Every other runtime parameter is identical within a matched cell.

If a valid N0g cannot be implemented without changing shared production
mathematics, work stops for a decision. The default fallback removes every
Schur-specific robustness/safety claim; it does not silently proceed with a
confounded comparison.

### N0g implementation boundary

N0g is a third exact one-pass mode, `guarded_nullspace`. It evaluates a
read-only implementation of S1's exact dimension/finite/whitening/SVD rank and
condition guard. A rejected factor follows the same feature disposition as
S1; an accepted factor calls the unchanged production Givens nullspace
projection. N0g then uses the preview, prior-match, transaction, and sole
commit path already shared by N0 and S1.

The existing bodies of the S1 reducer, Givens reducer, preview, commit, and
`StateHelper` update are quarantined from N0g edits. Calling the full Schur
reducer, discarding its output, and then running Givens is forbidden in scored
N0g because that double work invalidates latency and energy comparisons.

N0g acceptance requires: exact status/stage/binary64 diagnostic agreement with
the S1 guard on synthetic boundaries and the captured camera-system corpus;
bitwise N0-equivalent reduced rows and downstream state whenever the guard
accepts; zero mutation plus next-valid-update recovery on guard rejection;
byte-identical frozen N0/S1 smoke outputs; and a trace proving one guard SVD
plus one Givens projection, with no Schur QR. CP2 authoritative/shadow modes
remain scoped to their existing controls rather than being expanded for N0g.

## Canonical measurement contract

The complete machine-readable contract will live in `docs/icra27/PROTOCOL.yaml`
and must pass schema validation before science runs. It will freeze:

- exact sequence IDs, bag and ground-truth hashes, evaluation intervals, and
  the deterministic rule for any reduced subset;
- initialization, admissible trajectory, full completion, coverage, maximum
  pose gap, timeout, reset, crash, divergence, backlog, and dropped-frame
  predicates;
- SE(3) alignment without scale, timestamp association, trimming, ATE
  translation RMSE, and 1 m translation/rotation RPE conventions;
- native periods: KAIST `33.333... ms` and EuRoC `50 ms`;
- replay pacing, warm-up/cool-down, randomization, seeds, CPU affinity, clocks,
  thermals, telemetry population, power rails, and idle-subtraction policy;
- actual achieved backend workload: tracks offered/accepted, observations,
  factor rows, state dimension, rejection count, and update count;
- immutable run IDs, output schema, evaluator versions, and failure taxonomy.

The sequence is the scientific sampling unit. Repeats characterize
repeatability and timing variation; they are not treated as independent
sequences. Every attempted run appears exactly once as a success or frozen
failure class. Missing and nonfinite results remain in denominators.

### Accuracy and completion thresholds

- H1: no primary S1 completion regression or new catastrophic error against
  either N0g (primary causal comparator) or N0 (secondary bundled comparator).
- H2: all declared accuracy metrics satisfy both a sequence-level one-sided
  noninferiority analysis against N0g and the frozen practical margins: no
  more than 10% median regression and no more than 20% regression on any
  declared metric. N0 is reported as secondary context under the same margins.
  Near-zero denominators use a prospectively frozen absolute margin.
- A completion advantage must be S1 `3/3` versus N0g `0/3`, or reproduce on a
  second sequence or prospectively frozen condition. A searched single
  `2/3` event is insufficient.

### Resource thresholds

- H3-L: the end-to-end headline gates on camera-callback p99. It requires at
  least 15% lower paired repeat-level p99 versus N0g: the median paired ratio
  in a condition is at most 0.85 and the direction is consistent in at least
  three of four prospectively frozen representative conditions. Updater p99
  is a separately labelled component result and cannot substitute after data
  are seen.
- H3-E: the primary gate is at least 20% lower total platform energy per input
  frame at the same completed workload, paired by repeat: the median of five
  paired S1/N0g ratios in the prospectively selected headline condition is at
  most 0.80 and every paired block is at most 1.00. Total energy per completed
  sequence and energy per accepted update are mandatory secondary reports;
  neither may replace the primary gate. Rejection rate may not game the
  denominator.
- H3-B: a sustainable budget has p99 below the native period, no more than 1%
  deadline misses in every repeat, no backlog or undeclared drops, successful
  completion, and H2-compliant accuracy. S1 must sustain at least one frozen
  budget step and at least 1.5x the median realized accepted updater-landmark
  count of N0g; accepted raw visual rows are a mandatory corroborating report.
  At that operating point, at least one primary accuracy
  metric must improve by at least 5% and by more than twice the prospectively
  frozen pooled repeat-dispersion estimate. Requested feature count alone is
  not workload evidence.
- Headline Orin results require five randomized/interleaved repeat blocks,
  fixed profile/affinity, no thermal throttling, and complete telemetry.

Desktop timing is diagnostic until the frozen platform protocol passes.

## Experiment matrix

Exact selections and expected counts will be generated into
`docs/icra27/RUN_MATRIX.csv` before execution. The matrix below defines scope.

| ID | Purpose | Systems | Data/platform | Repeats | Gate |
|---|---|---|---|---:|---|
| E0 | One-cell harness and instrumentation neutrality | N0, N0g, S1 | One frozen KAIST sequence, desktop | 1 plus capture replay | G0 |
| E0L | Cheap causal/load falsification on identical captured backend envelopes | N0, N0g, S1 | Four prospectively frozen realistic conditions, desktop | 5 interleaved process repeats over one fixed eligible-envelope population per condition | G1 |
| E1 | Primary matched desktop evidence | N0, N0g, S1 | All 11 KAIST sequences | 3 | G2 |
| E2 | External context | N0p; both VF dataset-paper variants; completing EXT candidate | Frozen four-sequence EuRoC sanity subset, then KAIST with dataset-author bindings where supported; VF capped at 2 working days and fallback EXT at 1 day | 3 if admissible | G2/G3 |
| E3 | Breadth and explicit common-mode failure reporting | N0, N0g, S1 | All 11 EuRoC sequences; MH_04 never silently omitted | 3 where authorized by G1 | G2/G3 |
| E4 | Secondary-dataset context only | N0g, S1 | TUM-VI room4, corridor4, outdoors4 valid-GT intervals | 3 | G3 |
| E5 | Embedded confirmation | N0g, S1 | Frozen KAIST and EuRoC subsets, Orin | 3; 5 headline | G3 |
| E6 | Backend feature-budget operating point | N0g, S1 | 2-3 KAIST plus 1 EuRoC, Orin | 5 | G3 |
| E7 | Bounded fault containment | N0, N0g, S1 in isolated copies where applicable | KAIST/EuRoC captured envelopes | >=100/class or exhaustive | G3 |

E6 must sweep actual updater load. Each budget cell couples tracker supply to
`max_msckf_in_update` or directly controls accepted backend landmarks and logs
the realized population. Within a cell, N0g and S1 differ only by formulation.
A `num_pts`-only sweep is forbidden because the current KAIST updater caps its
input at 50 features.

E2's two mandatory VF variants are defined by behavior, not the review pack's
contradictory labels: (a) stereo visual-only with IMU disabled and (b)
stereo-inertial with IMU enabled; loop closure is disabled for both. Their
exact executable/config labels must be reconciled against the dataset paper,
dataset-author configuration, and pinned source before G0. The primary
completing candidate is official VINS-Fusion at the Phase A pinned commit. It
must pass the frozen four-sequence EuRoC sanity subset before KAIST. If it does
not complete within its two-day engagement, the one-day fallback candidate is
official SchurVINS on the same supported EuRoC subset. A EuRoC-only completion
does not unlock a KAIST accuracy comparison; it only satisfies shared-dataset
external context while the failed KAIST attempt remains fully disclosed.

## Qualitative geometry and map protocol (M0)

This is a mandatory QA/illustration lane, not a rescue endpoint. OpenVINS
produces trajectories, current sparse SLAM landmarks, and transient MSCKF
update points; the latter must not be described as a persistent dense map.

Every attempted run, including partial failures, receives a geometry bundle:

```text
runs/<campaign>/<dataset>/<sequence>/<system>/<budget>/rNN/
  manifest.json
  trajectory/estimate_raw.tum
  trajectory/estimate_aligned.tum
  trajectory/ground_truth_shared.tum
  geometry/feature_stream.bag
  geometry/slam_landmarks_final.ply
  geometry/msckf_update_points.ply
  geometry/snapshots/{25,50,75,max_angular_rate}.ply
  figures/{top,side,oblique}.svg
  qualitative_review.json
  SHA256SUMS
```

If an external system exposes no map, the manifest records
`MAP_NOT_EXPOSED`; no map is fabricated. Full-rate camera/track imagery is not
recorded by default because it can consume gigabytes per run.

Timing-critical attempts run without point/image subscribers. Every such run
gets a linked deterministic capture replay. The replay must use identical
input/config/seed and reproduce the scored trajectory; otherwise both are
retained and the qualitative pairing is invalidated.

Render rules are prospectively frozen: shared timestamps, GT-derived bounds
and margin, fixed top and orthographic-oblique views, fixed markers, preserved
gaps, no smoothing/interpolation/pruning, no per-method crop, and a clipped
point count. A shared-frame overlay uses one method-independent transform from
the frozen calibration/initial reference and applies it to every matched
method. If that transform cannot be defined, separately aligned views are
explicitly labelled and cannot support direct visual ranking. All primary
cells render; the paper subset is selected by a GT-only rule before method
labels are revealed.

Review tags are `missing`, `gap`, `jump`, `gross_divergence`,
`apparent_drift`, `feature_collapse`, `feature_explosion`, and `uncertain`,
each with severity 0-3. Comparative qualitative prose requires blinded
method labels, two independent raters, at least 80% binary agreement, weighted
kappa at least 0.6, and agreement with the corresponding quantitative result.
Otherwise figures are illustration-only. Any catastrophic visual artifact in
a nominally complete run halts evidence freeze until reconciled.

## Artifact, storage, and download policy

Large run data remains outside Git under the Phase A primary root
`/home/moksh/schurvio-icra27-artifacts`; each finalized run directory is
write-locked after its manifest/checksum seal. Git tracks protocols, schemas, manifests, inventory,
checksums, reports, and small figures. Before evidence freeze, the campaign
root receives a checksum-verified second copy and a recovery spot-check.
Reserve at least 25 GB for geometry; refresh the forecast after the E0 capture
replay. Raw logs are append-only; derived outputs are regenerable and stored
separately.

Every per-run manifest binds source commit/tree, binary and container/native
environment identity, resolved parameters, config/calibration/bag/GT hashes,
command, seed, timestamps, hardware profile, status/failure class, row counts,
artifact hashes, and byte sizes. Paper cells must trace to raw attempts through
one deterministic report entry point.

Before downloading a dataset or external test system:

1. Search the declared paths and reasonable local roots.
2. Compare size and published/captured checksums; inspect local Git remotes and
   commits for source systems.
3. Record `PRESENT_VERIFIED`, `PRESENT_UNVERIFIED`, `PARTIAL`, or `MISSING`.
4. Download only `MISSING` or irreparable `PARTIAL` items from the pinned
   source, with resume and temporary-file semantics where practical.
5. Verify size/checksum/archive safety before publication into the canonical
   path; never overwrite an existing candidate.

The anonymous export must strip user paths, machine names, Git remotes/history,
and identifying metadata. No claim may require reviewers to inspect an
artifact or URL.

## Fault-containment scope

E7 covers at least these classes: invalid dimensions, nonfinite factors, rank
failure, innovation-factorization failure, negative posterior diagonal, prior
change between preview and commit, diagnostics failure, and feature/factor
failure after proposal construction. Each row records injected count, typed
failure, mutation oracle, finalization count, next-valid-update recovery, and
all affected mutable state hashes.

Any partial mutation, hidden repair, duplicate commit/finalization, or failed
next-valid-update recovery is an immediate HR failure and pauses the campaign.
The claim remains bounded to tested fault locations unless mid-commit
no-throw/rollback behavior is separately proved.

## Phases, dates, deliverables, and cuts

| Phase | Target dates | execution_status | Deliverable |
|---|---|---|---|
| A -- readiness | Aug 15-18 | RUNNING | Clean identity, local-first inventory, environment/Orin probe, canonical protocol, matched harness MVP, deterministic metrics/map fixture, paper skeleton |
| B -- causal falsification | Aug 19-20 | NOT_STARTED | E0L N0/N0g/S1 backend-envelope load sweep and G1 report |
| C -- desktop attribution | Aug 21-24 | NOT_STARTED | E1 plus bounded E2/E3, paired statistics, G2 report |
| D -- embedded/fault evidence | Aug 23-27 | NOT_STARTED | E5/E6, mandatory VF timebox, completing EXT result, E7, natural-incident ledger |
| G3 -- venue decision | Aug 28 | NOT_STARTED | Exhaustive ICRA GO/NO-GO note |
| E -- evidence freeze | Aug 29-Sep 3 | NOT_STARTED | Independent metric reproduction, claim ledger, final tables/figures/maps |
| F -- paper/video | Aug 15-Sep 9 | RUNNING | Method/protocol writing from day one; full draft Sep 5; internal video target Sep 8; first video window Sep 9 |
| G -- correction/upload | Sep 10-14 | NOT_STARTED | Red-team, anonymity/compliance, final upload; Sep 15 emergency buffer only |

Pre-authorized cut order when a gate slips by at least two days:

1. contention and drone work;
2. new broad S2 reruns;
3. severity-correlation figures and expanded external failure reproduction;
4. fresh TUM-VI claims beyond the bounded context set;
5. nonessential visualization/video polish.

Never cut N0/N0g/S1 attribution, actual-load falsification, Orin confirmation
for an embedded headline, one completing external baseline, bounded fault
validation, the map inventory, independent metrics, claim ledger, or the
two-day documented VF engagement. VF stops after two working days unless a
new decision explicitly authorizes continuation.

## Gates and exhaustive decision rules

### G0 -- readiness hard stop

`gate_status: PENDING`

Pass only when all are true:

- clean, exact source identity and fresh build/test suite pass;
- datasets/evidence are registered by hash and runner dependencies work from
  the new worktree;
- Orin login, fixed-profile control, timing and power telemetry are proved, or
  the embedded thesis is explicitly removed before science runs;
- protocol predicates, sequence rule, thresholds, run matrix, and claim ledger
  are frozen and schema-valid;
- N0/N0g/S1 resolved-parameter parity and one-sequence completion pass;
- instrumentation and map-capture neutrality pass;
- the same fixture regenerates byte-identical metrics, reports, and gate
  verdict twice; invalid/missing/duplicate/nonfinite rows fail closed;
- storage capacity, second-copy destination, and recovery check are recorded.
- the scored-run plus capture-replay count, wall-clock forecast, peak I/O, and
  storage forecast fit the remaining schedule and measured E0 artifact size;
  the initial 25 GB reserve is a placeholder, not an acceptance value.

Any missing item keeps G0 from passing; acceptance criteria may not be deferred
into science runs.

### G1 -- causal/load falsification

`gate_status: PENDING`

Pass if S1 shows a prospectively defined useful advantage over N0g within the
realized production workload range, with HC still satisfied. For a latency
headline, use H3-L. A reproducible formulation-specific numerical or
completion difference may pass only under an equally explicit frozen rule.

A desktop E0L miss does not by itself falsify an ARM embedded effect. If E0L
is negative and Orin readiness is green, G1 permits exactly one
prospectively selected Orin sentinel cell: N0g/S1, one realistic high-load
condition, five interleaved repeats, plus capture replay. G1 fails only when
both the desktop test and sentinel lack the frozen useful signal, or when the
Orin is unavailable and the embedded hypothesis is explicitly cut. On G1
failure, stop the full ICRA method/system campaign and cut Schur-specific
efficiency, safety, and robustness claims. Record an artifact/RA-L/workshop/
stop decision; do not add a mechanism to escape the gate.

### G2 -- matched desktop attribution

`gate_status: PENDING`

Pass only if H1/H2 pass, there is no catastrophic S1 regression, any claimed
completion difference meets the stringent reproduction rule, and the
efficiency direction agrees with G1 when G1 passed on efficiency. All attempts
and maps must be accounted for. A config or source change invalidates affected
cells.

### G3 -- ICRA main-track GO/NO-GO

`gate_status: PENDING`

Prerequisites P are: source/data/environment integrity; frozen protocol; H0,
HC, H1, H2, repeatability, and bounded HR; mandatory documented VF engagement;
at least one completing independent shared-protocol baseline; map/attempt
inventory completeness; and format/anonymity/compliance.

Differentiators D are:

- D1: reproducible formulation-specific completion or numerical advantage
  versus N0g;
- D2: embedded operating-point or resource advantage versus N0g;
- D3: a naturally occurring, same-input differential incident with objective
  benign S1 and harmful N0g outcomes.

Decision table:

| P | Any D | Decision |
|---|---|---|
| fail | either | `NO-GO` |
| pass | yes | `GO`, using only the differentiator-supported thesis |
| pass | no | `NO-GO`; choose RA-L, workshop/artifact, or stop within 48 h |

There is no automatic "borderline ICRA" branch. External configuration
failures never count as D. A human venue override is recorded as `OVERRIDDEN`,
not `PASS`.

### G4 -- evidence freeze

`gate_status: PENDING`

Pass when every mandatory non-CUT cell is complete or classified, independent
metrics agree, all paper numbers regenerate, all required geometry is stored,
and every sentence-level claim maps to an in-PDF table/figure or bounded
artifact statement. Any later correctness change requires rerun or retraction
of affected evidence.

### G5 -- paper, video, and anonymity gate

`gate_status: PENDING`

Owner: Moksh Trehan; execution agent supplies reproducible evidence and the
compliance checklist. A full eight-page draft is due 2026-09-05 18:00
Toronto. The internal video freeze is 2026-09-08 18:00 Toronto. Pass requires
all required claims linked to frozen evidence, paper figures regenerated,
limitations present, two independent anonymity/red-team reads complete, no
identifying PDF metadata, and the video package validated if used.

### G6 -- upload gate

`gate_status: PENDING`

Owner: Moksh Trehan. Target: 2026-09-14 18:00 Toronto. Pass requires the final
PDF format/font/page/anonymity check, bibliography and disclosure review,
checksum of the uploaded PDF against the approved local file, successful
submission receipt, and a retained compliance note. September 15 is an
emergency buffer, not planned work time.

## Claims discipline

- N0 vs N0g supports guard-layer claims.
- N0g vs S1 supports formulation claims.
- N0 vs S1 supports bundled-path claims only.
- Injected faults support "atomic under the enumerated injected faults."
- "Prevents corruption in practice" requires D3.
- External failures always name exact pinned configurations.
- No "first," universal superiority, or general VIO failure claim.
- No external KAIST accuracy ranking without admissible external trajectories
  produced under the same frozen input and evaluation protocol. Non-equivalent
  resource, modality, or configuration results remain explicitly scoped
  context even when trajectories exist.
- No resource/energy/real-time claim without the frozen Orin evidence.
- A visually attractive map cannot rescue a failed quantitative gate.

Prewrite three conditional thesis sentences before observing Phase B scored results: resource result,
differential completion/numerical result without resource benefit, and
whole-stack/artifact fallback. Only a sentence unlocked by the decision table
may enter the abstract.

## Paper and compliance workstream

The paper is eight total pages including references and double-anonymous. No
appendix or supplementary attachment is assumed; video is the only supplement.
Essential per-sequence evidence and limitations must fit the PDF. The paper
skeleton starts in Phase A with section owners, a page budget, table/figure
slots, and cut order. An optional anonymous artifact is supporting material,
not required reviewer work.

Maintain an AI-use log. If AI-generated article content falls under the venue's
disclosure rule, include the required anonymous-safe acknowledgment at the
appropriate submission stage. Final checks cover anonymity, PDF metadata,
fonts, references, claims, video, and upload integrity.

## Phase A checklist

### A0 -- identity and authority

- [x] Verify frozen commit and tree.
- [x] Create separate clean worktree.
- [x] Create local working branch.
- [x] Create local annotated `icra27-a0-base` tag.
- [x] Record local-only authority and protect the dirty T1 worktree by policy.
- [ ] Generate and retain a clean source snapshot.
- [ ] Verify remote ref/protection only after separate approval; no push now.

### A1 -- local-first readiness inventory

- [x] Inventory all EuRoC, KAIST, and selected TUM-VI bytes; compare hashes or
  size plus existing audited manifests before downloading anything.
- [x] Inventory OpenVINS, SchurVINS/ov_SchurVINS, VINS-Fusion, ORB-SLAM3, and
  any dataset-author baseline source/builds before cloning.
- [x] Import or reference prior evidence by hash; eliminate the runner's
  dependency on ignored, worktree-local evidence.
- [x] Record host environment, tools, free space, and authoritative native or
  immutable-container policy.
- [ ] Prove Orin access/profile/telemetry/power or record the 48-hour H3 cut.
- [ ] Freeze primary artifact root, 25 GB geometry reserve, backup target, and
  recovery test.

### A2 -- clean build and test

- [x] Build from the new worktree using pinned dependencies.
- [x] Generate binary/build provenance; environment registry freeze remains
  part of A1/G0.
- [x] Run the full registered CTest suite and relevant Python suites.
- [x] Record failures without changing source; diagnose before fixing.
- [ ] Re-run from clean outputs after any approved fix.

### A3 -- protocol and controls

- [ ] Add schema-validated `PROTOCOL.yaml`, `RUN_MATRIX.csv`, and
  `claims/ledger.yaml`.
- [ ] Freeze exact sequence/subset rules and every metric/failure predicate.
- [ ] Implement/test N0g or stop for a scope decision.
- [ ] Add N0/N0g/S1 configs and parameterized KAIST launch.
- [ ] Require resolved-parameter diffs at every matched run.

### A4 -- campaign, reports, and maps MVP

- [ ] Generalize runner to system/repeat/budget IDs and immutable manifests.
- [ ] Add failure taxonomy, actual backend-load fields, passive timing, and
  map/trajectory capture contracts.
- [ ] Split hosted small-fixture CI from local/self-hosted full-bag smoke.
- [ ] Add deterministic metrics/gates, claim ledger integration, and a golden
  fixture that regenerates identically twice.
- [ ] Run E0 on one sequence for N0/N0g/S1.
- [ ] Prove scored-run/capture-replay trajectory identity and render the first
  fixed-view qualitative atlas row.
- [ ] Create the eight-page paper skeleton and AI-use log.

## Phase A acceptance record

| Item | execution_status | Evidence / blocker | Next action |
|---|---|---|---|
| Exact source identity | DONE | A0 commit/tree above | Generate source snapshot |
| Clean worktree/branch | DONE | `/home/moksh/schurvio-lite-icra27-a0` | Keep unrelated worktrees untouched |
| Local tag | DONE | `icra27-a0-base` -> A0 commit | Remote action not authorized |
| Dataset inventory | DONE | EuRoC 11/11, KAIST source/adapted 22/22, and selected TUM-VI 3/3 freshly hash-verified | Repair portable tracked bindings |
| External systems | DONE | Existing systems inventoried; only missing mandatory VINS-Fusion acquired at two pinned commits | Bring-up remains Phase D timebox |
| Clean build/tests | DONE | Five-package build; 580 C++ tests, 121 TurnSafe Python tests, and 494 isolated CP2 Python tests pass | Preserve provenance and source snapshot |
| Orin readiness | BLOCKED | No endpoint/profile/power declaration is locally discoverable | Request endpoint and control/telemetry details |
| Protocol/control files | NOT_STARTED | This plan defines required content | Implement after inventory/build facts |
| Harness/metrics/maps MVP | NOT_STARTED | Existing fixed-S1 runner is insufficient | Begin only after clean build diagnosis |
| G0 | PENDING | All A1-A4 requirements are conjunctive | Do not start science runs |

## Decision and progress log

| Timestamp (Toronto) | Decision / event | Result and rationale |
|---|---|---|
| 2026-08-15 | Audit existing review pack and source/evidence | Conditional major revision; full campaign not authorized |
| 2026-08-15 | Add N0g control | Required for formulation-specific attribution |
| 2026-08-15 | Add qualitative geometry retention | Mandatory for every attempt, with timing-isolated capture replay |
| 2026-08-15 | Authorize Phase A and missing-data/system downloads | Local-first verification required; remote publication remains out of scope |
| 2026-08-15 | Create worktree, branch, and local tag | Exact A0 identity isolated; dirty T1 worktree untouched |
| 2026-08-15 | Complete local-first inventory | All selected dataset bytes already present and verified; only missing mandatory VINS-Fusion was downloaded at pinned commits |
| 2026-08-15 | Complete clean build and baseline tests | Five-package build passes; 580 C++ + 123 TurnSafe Python + 494 isolated CP2 Python tests pass |
| 2026-08-15 | Repair ignored KAIST ledger dependency | Exact-byte historical ledger imported into tracked registry; clean-worktree prepare-only preflight passes; TurnSafe suite now 123 tests |
| 2026-08-15 | Repair historical CP0 package | Restored the exact checksum-listed build-provenance member and bound verification to the artifact-snapshotted verifier; sealed artifact passes |

Update this file whenever a work item changes state, a gate is evaluated, a
download occurs, scope changes, or evidence is invalidated. A status change is
not complete until its evidence path or blocker is recorded.
