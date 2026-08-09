# Proposed CP2-E offline descendant-confinement clarification

Status: **proposed, non-authorizing, and pending exact human approval**

Recorded-input, registry, result, trajectory, or workload access used: **none**

Network or web-document access used: **none**

Host-control mutation or formal execution used: **none**

Date: 2026-08-04

## Scope and local source provenance

This clarification is derived only from installed, offline Linux manuals and
read-only host observations.  It does not approve a timing profile,
installation, privileged feasibility trial, formal six-run campaign, or
publication.  Those transitions remain locked until a human explicitly
approves the exact reviewed bytes or commit containing this clarification and
its implementation.

The installed source closure used for the semantic audit was:

- `/usr/share/man/man7/cpuset.7.gz`, from `manpages 5.05-1`, compressed
  SHA-256 `bebe87a0dadfa490015f8c0a4028c5cb07b85a31bf2895cb8d151da53dd85a88`
  and uncompressed roff SHA-256
  `63f2f87045e9d9fb4e4e320142f63c99cd4afc43afaca82ad181f50d0a21142d`;
- `/usr/share/man/man7/cgroups.7.gz`, from `manpages 5.05-1`, compressed
  SHA-256 `b642b0c570ecadd8ca7ff30a0e3d1fcd5f15567ff3eba96cb71b0357ae3b1153`
  and uncompressed roff SHA-256
  `ef487299393dcfda147433296673c170c2e1ffc932ae01d453ff3797ec5336d5`;
  and
- `/usr/share/man/man2/sched_setaffinity.2.gz`, from `manpages-dev 5.05-1`,
  compressed SHA-256
  `ba6b086c1bdab79f1ea9326ca9201217cb32037a8cf9e628c7229d54053c0b86`
  and uncompressed roff SHA-256
  `f2d2161e91193f35196611a64c8e1a4df9577ca370ee295d28c07653b6d07b41`.

The relevant locally formatted `cpuset(7)` material is at lines 64--83 for
hierarchy and task membership, 93--113 for the intersection with scheduler
affinity, 209--235 and 399--405 for `cpu_exclusive`, 324--337 and 692--765 for
scheduler load balancing, and 924--945 for hierarchy rules.  These line
locations and hashes bind the local source consulted; they are not web
citations.

## Impossibility of the prior universal foreign-disjointness predicate

Let `E` be the nonempty set of estimator CPUs.  The prior predicate required
every non-campaign TID `t` to satisfy

```text
effective_affinity(t) intersection E = empty.
```

That is not a property a new cgroup-v1 child cpuset can establish.  A task is
attached to exactly one cpuset, and its scheduler-eligible CPUs are restricted
by both its affinity mask and that cpuset.  Child `cpu_exclusive=1` prevents
disallowed overlap with sibling or cousin cpusets; it does not evict tasks
attached to an ancestor.  The root cpuset remains an ancestor containing the
machine CPUs and may retain tasks eligible on the child's CPUs.  Disabling
`sched_load_balance` changes load-balancing behavior, not the eligible CPU
mask.  Finally, an effective affinity mask is an eligibility set, not proof
that the TID occupied a CPU during a measured interval.

A read-only snapshot at `2026-08-05T01:37:23Z` of the target Linux
`5.15.0-67-generic` host made the counterexample concrete.  The active v1
cpuset root was `/`, its configured
and effective CPUs were `0-31`, `cpu_exclusive` was `1`, and
`sched_load_balance` was `1`; the v2 hierarchy exposed no cpuset controller.
The v1 root contained approximately 1,151 task IDs at the snapshot.  For each
CPU `N` in `0-31`, `/proc` exposed a foreign `cpuhp/N`, `idle_inject/N`,
`ksoftirqd/N`, and `migration/N` TID attached to `/` with singleton effective
affinity `{N}`.  The 128 sorted canonical witness rows had SHA-256
`edb76653d0d91578a187b7e007dfc1942f79aa23ea3557794ca10b6353bb1a63`.
Therefore every nonempty choice of `E` intersects at least these observed
foreign eligibility rows.  This counterexample used neither recorded data nor
host mutation.

The universal predicate is consequently removed.  It must not be renamed as
"occupancy", approximated with a process-only snapshot, or reintroduced as an
implicit feasibility, timing, CPU-selection, report, or publication gate.

## Proposed hard descendant-confinement contract

Subject to exact human approval, the hard scheduler claim is limited to the
complete campaign descendant closure.  At every applied-state and guardian
observation:

1. start from the bound campaign peer PID and enumerate its full descendant
   process closure through every thread's `/proc/.../children` surface;
2. enumerate every TID of every descendant process, retaining TID, process
   start-time ticks, effective scheduler affinity, and cpuset membership;
3. repeat the before/after PID and TID closure capture until it is identical;
   a vanished or reused identity causes a retry, and a closure that cannot be
   stabilized within the frozen bound is indeterminate;
4. require every stabilized descendant TID to have exactly the profile's
   estimator affinity and exactly the profile's child-cpuset membership; and
5. require the child cpuset `tasks` population to equal, in full, the sorted
   stabilized descendant TID population.

This includes a peer that becomes multithreaded and every process/thread
created by a fork.  Process-leader-only membership, a single initial PID,
subset membership, an affinity superset, a task outside the child, a foreign
TID inside the child, unreadable identity state, or an unresolved fork/exit
race rejects.  The retained control evidence uses separate sorted
`descendant_process_ids` and canonical `descendant_threads` populations so the
claim can be independently rederived.  No claim of whole-machine CPU
exclusivity follows from descendant confinement.

## Proposed non-gating foreign-affinity observation

Each guardian observation also retains the sorted set of stably identified
foreign TIDs whose effective affinity intersects an estimator CPU.  Every row
has exactly:

```text
effective_affinity_cpu_ids
process_start_time_ticks
tid
```

`tid` is a positive u64, `process_start_time_ticks` is a u64 identity guard,
and `effective_affinity_cpu_ids` is a nonempty, increasing, duplicate-free
population drawn from the controlled CPUs and intersecting the estimator set.
Rows are in strictly increasing TID order with no descendant TID.  The
privileged observer brackets affinity collection with start-time reads;
vanished or reused TIDs are not fabricated, and an inspection-permission
failure is indeterminate.  The detached helper and artifact verifier require
the exact schema, order, identities, affinity domain, guardian chain, final
seal, and source binding.  A malformed, omitted-from-a-resealed-claim,
duplicated, reordered, or substituted row invalidates evidence integrity.

The surface is named `foreign_affinity_eligibility`, not scheduler occupancy.
Its empty, nonempty, or changing canonical population is observational only.
It cannot change profile feasibility, select CPUs, reject a run, enter timing
sample populations, alter exact quantiles or ratios, change any pass/fail
Boolean, or affect aggregate mathematics.  Before/after hashes and a final
count may be retained for audit.  Failure to produce structurally complete,
source-bound evidence remains an integrity failure; the observed population's
size or membership is not.

## Unchanged gates and authorization boundary

All other CP2-E requirements remain unchanged: exact runtime and DSO identity,
clock/frequency configuration and drift, CPU online/SMT state, IRQ population
and affinity, idle and latency controls, boost state, APERF/MPERF bounds,
thermal and throttle telemetry, guardian cadence and coverage, process
freshness, exact common populations, integer/rational timing mathematics,
pair order, manifest closure, restoration, and independent verification.

The data-free implementation candidate changes only the impossible foreign
disjointness predicate and corrects descendant membership to the stabilized
full PID/TID closure.  Positive kernel-like foreign rows and changes between
observations are protecting-test fixtures and cannot change outcomes;
malformation and substitution reject.  It was developed and tested without
network access, recorded input, host mutation, installation, privilege use, or
formal execution.

This document is not self-approving.  Before a profile is finalized or any
root installation, data-free privileged trial, or formal campaign is
eligible, a human must approve the exact clarification and reviewed
implementation.  The existing noninteractive-authority, installation,
reversibility, source-freeze, chained-authorization, and formal-execution
prerequisites continue to apply independently.
