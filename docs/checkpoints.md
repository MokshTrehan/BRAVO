# Hard checkpoints and kill criteria

All dates are 2026 and use America/Toronto local time unless the checkpoint
states otherwise. A gate passes only when every required artifact is linked in
the checkpoint record and the automated checks exit successfully.

Unless a gate explicitly overrides them, the following definitions apply:

- A complete dataset run covers at least 99.5% of the selected input time
  interval and has no ground-truth initialization, crash, NaN/Inf, covariance
  failure, silent restart, or manual intervention.
- ATE uses fixed SE(3) alignment with no scale correction. Alignment, startup
  offsets, calibration, frontend settings, random seeds, and estimator commit
  are frozen before comparisons.
- Canonical trajectory evaluation uses the manifest-recorded `evo` version,
  a maximum timestamp-association difference of 0.01 s, translation APE, and
  translation/rotation RPE at 1 m using all pairs selected from the reference.
  Each mode's accuracy metric may align to ground truth independently, but a
  direct mode-to-mode trajectory comparison must compute one SE(3) alignment
  from the baseline's shared timestamps and apply that same transform to both
  trajectories; independently aligning both modes is forbidden for parity.
- Timing and power measurements exclude a declared 60-second warm-up and use
  fixed cores and clocks. Every percentile reports its sample count.
- Missing a checkpoint does not automatically move a later checkpoint. Only a
  rescue window explicitly written below is allowed.

## D0 — external dependency gate — July 23, 18:00

Required evidence:

- Target board model, OS/toolchain, access method, available CPU cores, and
  power-reading interface are recorded.
- A human reviewer is named for frame, Jacobian, covariance, and iterated-update
  signoff.
- The missing EuRoC V2 sequences have an acquisition plan, and the second
  dataset is selected by CP1.
- GPLv3 use is accepted by the author: private modification does not itself
  require publication, while any conveyed covered binary/artifact must satisfy
  the applicable Corresponding Source and license-notice duties. Obtain legal
  or venue review if the planned artifact release is unclear.

Failure action: desktop work continues, but embedded/power claims are marked
blocked. CP6 cannot pass an embedded claim without actual target evidence.

## CP0 — reproducible baseline — July 24, 23:59

CP0 override: its timing log proves instrumentation and row integrity only; it
does not support a performance claim. Affinity and governor must be recorded,
but the fixed-clock requirement begins with comparative timing at CP2.

Required evidence:

- The pinned OpenVINS commit in `UPSTREAM_REVISION`, plus the declared
  serial-runner lifecycle patch, builds from the documented command on the
  host or in a pinned container. The manifest records the full source-tree
  hash and carries the exact binary diff; no estimator-math file is changed.
- MH_01_easy runs end-to-end from the frozen startup offset without a fatal
  error, NaN, or silent estimator restart, and SE(3)-aligned ATE RMSE is at most
  0.25 m.
- A nonempty trajectory, timing log, console log, and ATE/RPE evaluation are
  stored under `results/immutable/baseline/`.
- The run manifest records commit, dirty status, exact command, configuration
  SHA-256, dataset identity, host/OS/compiler, affinity, governor, and exit
  status.
- `docs/conventions.md` and an estimator call map are ready for human review.

Pass decision: freeze the baseline and allow specification/test work. No
production estimator edit is yet permitted.

Failure action: spend at most 24 additional hours on dependencies or the
supported Docker path. If MH_01 is still not reproducible by July 25 at 23:59,
remove the August 15 embedded commitment and re-evaluate the submission path.

## CP1 — conventions and mathematical correctness — July 28, 23:59

Required evidence:

- `docs/conventions.md` has no unresolved blocking field and has dated human
  signoff.
- `docs/iterated_update_spec.md` defines whitening, residual and increment
  signs, landmark elimination, rank rejection, robust weighting/gating,
  frozen-prior correction, re-triangulation, FEJ points, final covariance, and
  manifold reset.
- On at least 100 deterministic well-conditioned fixtures, full and Schur
  state increments, landmark back-substitution, and posterior covariance obey
  `||candidate-reference|| <= 1e-9 + 1e-7*||reference||`; final residual norms
  obey the same bound with an absolute term of `1e-10`.
- Central finite-difference pose and landmark model matrices pass at least 200
  seeded valid-geometry cases on the frozen EuRoC `CamRadtan` path against the
  intended all-double continuous projection with FEJ disabled, with maximum
  normalized Frobenius error at most `1e-5`. The nominal stored residual is
  separately checked against the float-quantized runtime projection;
  mixed-FEJ matrices are treated as affine surrogates and require a CP2 golden
  fixture. `CamEqui` is outside this CP1 derivative proof.
- At least 100 singular or ill-conditioned landmark fixtures are rejected
  deterministically without an explicit matrix inverse or NaN.
- At least 100 algebraic exact clone-copy PSD fixtures match the
  covariance-form innovation update through rectangular prior factors; no
  full-prior LLT, inverse, clone noise, jitter, or eigenvalue clamping is
  permitted. A production `StateHelper::clone` integration fixture remains a
  CP2 requirement.
- At least 100 tall-system calls to the actual OpenVINS measurement compressor
  prove `Lambda/eta` preservation and explicitly account for residual-only
  energy discarded from `gamma`.
- Posterior covariance symmetry error is at most
  `1e-10 * max(1, ||P||_inf)` and its minimum eigenvalue is at least
  `-1e-10 * max(1, lambda_max(P))` on seeded fixtures.

Pass decision: production one-pass implementation is permitted. Fixed
two-pass remains blocked until the mixed-FEJ affine-surrogate semantics and
exact-chart covariance claim receive their own protecting test and review.

Failure action: estimator integration remains blocked. Fix the derivation or
tests; do not change the baseline to make the tests pass.

## CP2 — one-pass parity — July 31, 23:59

Required evidence:

- A deterministic updater fixture matches baseline state increment and
  posterior covariance to `1e-8 + 1e-6*||reference||` by state block on at
  least 1,000 recorded visual updates, and accept/reject decisions agree on at
  least 99.9% of measurements.
- On MH_01_easy, MH_03_medium, and V1_01_easy, both modes complete with identical calibration,
  initialization, startup offset, inputs, and gating configuration.
- The aligned trajectory difference between modes is at most 1 cm at p95 in
  position and 0.05 degrees at p95 in orientation; ATE differs by at most 1%.
- Any difference in accepted measurements, ranks, or gates is explained in the
  diagnostics. There is no silent regularization.
- Median visual-update time is no more than 10% above baseline and p95 is no
  more than 15% above baseline on the frozen desktop profile.

Pass decision: one-pass parity is established. Fixed-two-pass implementation
is permitted only if its separately recorded FEJ-surrogate and chart-transport
prerequisites have also passed; CP2 alone does not satisfy them.

Failure action: keep the original updater as default, revert the integration
branch, and block two-pass work until parity is restored.

## CP3 — fixed two-pass correctness — August 5, 23:59

Required evidence:

- A linear fixture produces identical one- and two-pass mean/covariance with
  relative error at most `1e-10`.
- Across 100 seeded nonlinear fixtures, pass 2 lowers true reprojection cost in
  at least 90% of trials, with at least 10% median reduction and no aggregate
  cost increase from an altered accepted measurement set.
- Runtime assertions prove both passes use the same predicted prior, pass 1
  does not commit covariance, and posterior covariance/reset occur exactly
  once at the final linearization.
- MH_01_easy, V1_01_easy, and MH_04_difficult complete in one-pass and fixed
  two-pass modes without NaN, covariance-gate violation, or unexplained reset.
- Fixed two-pass ATE is no more than 10% worse than one-pass on any of those
  three sequences; all pass-specific triangulation, cost, gates, and timing are
  logged.

Pass decision: retain fixed two-pass as a candidate mode.

Failure action: fixed two-pass becomes an experimental ablation. Continue only
with bounded one-pass resource control unless the gate is repaired by Aug 7.

## CP4 — EuRoC stability — August 9, 23:59

Required evidence:

- Baseline, one-pass, and retained two-pass modes run the complete 11-sequence
  EuRoC matrix.
- Proposed modes complete at least 10 of 11 sequences and introduce no failure
  on a sequence completed by the baseline without a documented root cause.
- No run contains NaN/Inf state, unexplained covariance repair, unbounded RAM,
  or a trajectory gap longer than one camera second.
- ATE, RPE, initialization, accepted/rejected features, pass counts, timing,
  CPU, and RAM are summarized without excluding failed runs.

Pass decision: proceed to deadline/resource experiments.

Failure action: allow stabilization only—no features. Fewer than 9 successful
sequences stops the ICRA algorithm path and triggers a redirect review.

## CP5 — bounded resource behavior — August 12, 23:59

Required evidence:

- The resource manager has configurable camera deadline, reserve, feature cap,
  clone cap, and maximum passes; every skip has a reason code.
- A deterministic 10,000-frame admission test has zero pass-2 launches when
  predicted pass cost plus reserve exceeds remaining budget.
- A test proves high-rate propagated output continues when a visual update or
  pass 2 is skipped.
- On a frozen four-core desktop stress profile with three repeated runs,
  bounded mode either has at most 1% camera-deadline misses or reduces miss rate
  by at least 50% relative to fixed two-pass.
- Bounded-mode ATE stays within 5% of the better stable one/fixed-two-pass mode,
  and p95 update latency is not above fixed two-pass.
- Enabled instrumentation adds no more than 5% to median or p95 update latency
  in a paired on/off measurement.

Pass decision: bounded mode may enter the primary experiment matrix.

Failure action: describe it as a best-effort heuristic only; it cannot support
a deadline guarantee or primary paper claim.

## CP6 — prototype freeze and research go/no-go — August 15, 23:59

Required evidence:

- On the named CPU-only target with fixed cores/clocks and GPU disabled, three
  repeated runs produce synchronized trajectory, p50/p95/p99/max latency,
  deadline misses, CPU, RAM, power, temperature/throttling, and energy/frame.
- The target sustains 20 Hz input with p95 camera processing at most 50 ms and
  at most 1% deadline misses on the predeclared primary sequence set.
- Across the complete reported sequence set, at least one predeclared
  Pareto route must pass. Route A: bounded ATE is at least 8% better than
  one-pass on the median sequence, wins on at least 7 of 11 sequences, has no
  per-sequence regression above 20%, has at most 1% deadline misses, and uses
  no more than 15% additional energy/frame. Route B: bounded ATE is within 3%
  of fixed two-pass while p95 latency or energy/frame is at least 20% lower,
  and fixed two-pass is itself at least 5% more accurate than one-pass. Failed
  runs remain in the matrix and no replacement metric may be selected after
  seeing results.
- A novelty review supports a contribution stronger than “Schur plus IEKF” and
  maps every proposed claim to existing evidence.
- Source, tests, configs, and prototype artifacts are tagged; algorithm scope
  is frozen.

Pass decision: pursue the ICRA submission on the supported claim.

Failure action: do not force the claim. Redirect to an engineering artifact,
technical report, later venue, or a smaller scientifically honest result.

## CP7 — external validity — August 25, 23:59

Required evidence:

- The predeclared second-dataset matrix, external baselines, embedded
  power/core sweeps, and ablations complete with three repetitions where
  timing or power is claimed.
- The second dataset contains at least five sequences or 30 minutes of data,
  at least 90% of declared runs complete, and the primary effect has the same
  direction on at least four of five sequences with at least half its EuRoC
  magnitude.
- Repeated target timing has coefficient of variation at most 10%; repeated
  power/energy has coefficient of variation at most 5%.
- Primary endpoints, alignment, startup rules, and statistical summaries are
  unchanged from their pre-result definitions.
- Every table and plot is generated from immutable run CSVs by a committed
  script.

Failure action: reconsider submission scope. EuRoC-only evidence may not be
presented as broad external validity.

## CP8 — immutable result freeze — September 1, 23:59

Required evidence:

- All raw logs, manifests, configs, evaluation outputs, tables, and figure
  sources are immutable and indexed by commit/config hash.
- The central matrix has no missing cells: 11 EuRoC sequences, at least five
  second-dataset sequences, all four modes, and three repetitions per claimed
  timing/power condition. A paired sequence-bootstrap 95% confidence interval
  excludes zero for the primary effect.
- Central results reproduce from documented commands.
- No algorithm, baseline, or configuration change remains open.

Failure action: block new experiments except a documented reproduction of an
already frozen configuration. A scientific result change requires a formal
submission go/no-go review.

## CP9 — complete manuscript — September 5, 23:59

Required evidence:

- A complete anonymous eight-page paper, including references and
  acknowledgments, exists in the official format.
- Every numerical claim traces to a frozen artifact; limitations, failures,
  and the exact best-effort/deadline semantics are stated.
- AI disclosure identifies the system, affected sections/content, and manner
  of use.

Failure action: no video polish or optional experiment may displace a missing
methods, results, limitations, or reproducibility section.

## CP10 — independent technical review — September 8, 23:59

Required evidence:

- At least one VIO/EKF reviewer and one systems/experimental reviewer submit a
  written review.
- The issue log has zero unresolved fatal correctness, novelty, fairness, or
  unsupported-claim findings and at most three major issues, each with an
  owner and closure date no later than September 10.
- The manuscript is anonymized and coauthor metadata is ready.
- The optional video is uploaded in the first official window or deliberately
  assigned to the second window; its status cannot block the paper.

Failure action: narrow removable claims. Any unresolved fatal estimator,
novelty, or fairness issue stops submission; no new exploratory experiment is
permitted.

## CP11 — clean reproduction and compliance — September 11, 23:59

Required evidence:

- From a clean pinned environment, one command builds/tests and reruns all four
  modes on two EuRoC and one second-dataset sequence. It reproduces central
  trajectory metrics within 2%, p95 timing within 10%, and regenerates the
  paper tables exactly from frozen logs.
- PDF format, eight-page limit, fonts, anonymization, citations, figures,
  disclosure, artifact hashes, and author checklist all pass.

Failure action: block submission until the discrepancy is explained or the
affected claim is removed.

## CP12 — submission authorization — September 13, 18:00

Required evidence:

- All human authors approve the exact PDF, disclosure, claims, and submission
  metadata.
- The final PDF and optional video hashes are recorded.
- A human author owns upload and the on-site presentation obligation if
  accepted.

Failure action: do not submit an unapproved or non-reproducible paper.

## CP13 — upload and lock — September 14, 17:00

Required evidence:

- The PDF is uploaded, downloaded back from PaperPlaza, and matches the recorded
  SHA-256; portal status and timestamp are recorded.
- September 15 is reserved for verification or a documented portal emergency,
  not algorithm, result, or substantive manuscript changes.

Failure action: preserve timestamped evidence of any portal failure and contact
the conference chairs. Do not rush an unverified replacement submission.
