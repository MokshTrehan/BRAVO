# SchurVIO-Lite execution plan

Status: active
Start date: 2026-07-22
Prototype go/no-go: 2026-08-15
Internal submission target: 2026-09-13
ICRA 2027 deadline: 2026-09-15 23:59 Pacific Time

Official constraints verified 2026-07-22 against the
[ICRA 2027 call for technical papers](https://2027.ieee-icra.org/contribute/call-for-icra-2027-papers-now-accepting-submissions/):
eight pages total, double-anonymous review, required disclosure of AI-generated
text/figures/images/code, no AI processing of a manuscript under review, and an
on-site presenter if accepted.

## Mission

Build a reproducible CPU-only VIO research prototype from pinned OpenVINS. The
prototype will provide a baseline-parity one-pass reduced visual update, a
deadline-aware bounded mode, and complete accuracy, latency, resource, and
power evidence. A fixed-two-pass mode remains a conditional research candidate
and is not called mathematically correct unless its mixed-FEJ surrogate and
exact-chart covariance gates pass separately.

This project is VIO, not a flight-ready SLAM product. Loop closure,
relocalization, online calibration, a new initializer, learned features,
ROS/PX4 integration, and GPU dependence are out of scope before submission.
The CP0 profile therefore overrides OpenVINS' EuRoC defaults and keeps the
known camera intrinsics, extrinsics, and camera/IMU time offset fixed in every
mode; those calibration variables are not filter-state blocks.

## Non-negotiable operating rules

1. The baseline is the pinned upstream commit plus the narrowly reviewed
   serial-runner lifecycle patch identified by the sealed run's source-tree
   and binary-diff hashes. Its configuration and raw results are immutable.
2. No estimator-math or updater implementation begins until
   `docs/conventions.md` and
   `docs/iterated_update_spec.md` are complete and the CP1 tests pass.
3. Every estimator change is focused, tested, and followed by an MH_01 smoke
   run. Frame, Jacobian, covariance, FEJ, or relinearization changes require
   human mathematical review.
4. Both successful and failed runs produce manifests. Every regularization,
   clamp, rejected landmark, dropped feature, skipped pass, and numerical
   failure is observable.
5. Baseline and proposed modes use identical calibration, initialization,
   startup interval, feature input, core policy, clock policy, and evaluation
   alignment unless a predeclared ablation changes one of them.
6. No baseline tuning is allowed after proposed-method results are inspected.
7. Missing evidence is a failed checkpoint. A checkpoint may move only through
   a dated written change that records the reason, scope impact, and new
   decision. It may not be silently waived.
8. No new algorithm feature is accepted after 2026-08-15. No algorithm or
   configuration change is accepted after the 2026-09-01 result freeze.

## Workstreams

- Baseline and reproducibility: build, dataset validation, manifests, timing,
  trajectory evaluation, and clean reruns.
- Estimator specification: conventions, updater call graph, Schur equations,
  rank policy, frozen-prior iteration, FEJ policy, covariance, and reset.
- Estimator implementation: synthetic tests, one-pass parity, fixed two-pass,
  numerical diagnostics, and EuRoC stabilization.
- Resource control: stage timing, feature/clone caps, pass admission, deadline
  reserve, CPU/RAM/power collection, and propagated-output continuity.
- Evidence and submission: repeated runs, second dataset, external baselines,
  figures, tables, manuscript, disclosure, anonymization, and reproduction.

## Hard checkpoints

The detailed evidence and failure actions are the source of truth in
`docs/checkpoints.md`.

`project/checkpoint_status.py` is a schedule/status dashboard, not a gate
oracle. A pass requires the gate-specific automated validator plus the linked
evidence and human decisions required by `docs/checkpoints.md`.

| ID | Due | Gate | Required decision |
|---|---:|---|---|
| D0 | Jul 23 | External dependencies named | Embedded path is testable or explicitly blocked |
| CP0 | Jul 24 | Pinned build and MH_01 baseline | Baseline frozen; estimator mapping may proceed |
| CP1 | Jul 28 | Conventions, derivation, synthetic math | Production estimator edits permitted or blocked |
| CP2 | Jul 31 | One-pass baseline parity | One-pass retained; two-pass also needs its separate math prerequisites |
| CP3 | Aug 05 | Fixed two-pass correctness | Iteration retained or reduced to an ablation |
| CP4 | Aug 09 | EuRoC stability | Resource work continues or algorithm path stops |
| CP5 | Aug 12 | Deadline/resource behavior | Bounded mode may be a primary claim or only a heuristic |
| CP6 | Aug 15 | Embedded evidence and research advantage | Pursue ICRA, redirect, or stop the claim |
| CP7 | Aug 25 | External validity | Final figures proceed or submission scope is reconsidered |
| CP8 | Sep 01 | Immutable result freeze | Writing-only phase begins |
| CP9 | Sep 05 | Complete eight-page draft | Internal technical review begins |
| CP10 | Sep 08 | Independent technical review | Fatal correctness/novelty issues are absent or submission stops |
| CP11 | Sep 11 | Clean reproduction and compliance | Submission is authorized or blocked |
| CP12 | Sep 13 | Human final signoff | Submission candidate is locked |
| CP13 | Sep 14 | Upload and portal verification | Sep 15 remains emergency buffer only |

## Branch and evidence policy

The repository retains OpenVINS history and names its original remote
`upstream`. Work uses one branch per focused change, beginning with
`schurvio-lite/bootstrap`. Planned follow-on branches are conventions,
instrumentation, Schur tests, one-pass, two-pass, resource manager, embedded,
and experiment automation.

Immutable run artifacts will live under `results/immutable/` and contain the
trajectory, per-frame diagnostics, resource log, evaluation output, and a
machine-readable manifest. Large datasets and build products are never
committed.

The only pre-CP1 source exception is a harness-level change to
`ros1_serial_msckf.cpp`: make estimator/visualizer ownership local, disable its
unjoinable detached publisher thread, and destroy plugin-owning objects before
process-static class loaders. This changes runner lifecycle, not estimator
state, residual, Jacobian, gating, update, or covariance math. The exact binary
diff is copied into every sealed CP0 artifact.

## Ownership boundary

Codex owns repository work, implementation, tests, diagnostics, automation,
experiment execution on accessible machines, and pre-submission drafting.

A human author owns mathematical signoff, target-hardware access, scientific
interpretation, novelty and claim approval, authorship, PaperPlaza submission,
and any required on-site presentation. Codex must not process the manuscript
after it is under review.
