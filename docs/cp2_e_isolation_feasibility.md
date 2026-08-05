# CP2-E estimator-CPU isolation feasibility proof

Status: **quarantined; supplies no audit credit and may not influence CP2**

Date: 2026-08-04

> **Quarantine notice:** the audit workstream used unauthorized public web
> access while deriving this document. The incident and mandatory disposition
> are recorded in `docs/cp2_predata_incident_log.md` at
> `2026-08-04T21:31:51-04:00`. Nothing below may justify source freeze, a
> profile choice, host mutation, or formal execution. A separate offline
> replacement derivation is required.

This audit used only the authorized read-only CPU-affinity, cgroup, scheduler,
and kernel-command-line surfaces.  It opened no dataset registry, bag, ground
truth, trajectory, recorded result, or recorded-workload timing.  It changed
no affinity, cgroup, service, module, interrupt, clock, or host control.

## Contract to preserve

Let `C` be the frozen estimator CPU set and `A` the complete campaign/ROS
descendant TID population.  For every live TID `t`, let `S(t)` be the effective
CPU eligibility returned by Linux `sched_getaffinity`; kernel/cpuset
restrictions are reflected in that returned mask.

The existing guardian requires

```text
for every t not in A: S(t) intersection C is empty.
```

This is an eligibility invariant, not a claim that sampling happened not to
observe contention.  The guardian enumerates the complete descendant
population, requires every descendant thread's exact estimator affinity, then
rejects if any foreign live TID's affinity intersects `C`.  The protecting
backend test now explicitly injects such a foreign TID and requires the same
hard rejection.  No relaxation was made.

## Why a v1 child cpuset is insufficient

The frozen candidate uses the host's v1 cpuset hierarchy.  Linux's documented
v1 exclusivity rule permits overlap with a direct ancestor or descendant; it
only excludes overlapping peers elsewhere in the hierarchy.  A new campaign
child is a direct descendant of the root cpuset, so the root may retain the
same CPUs and its tasks remain eligible on them.  The kernel also documents
that disabling load balancing in one child is ineffective wherever an
overlapping cpuset still enables it; this host's root cpuset does.

Formally, take any ambient `t` in the root cpuset with
`S_before(t) intersection C` nonempty.  The authorized candidate operation
moves only `A` into the child and does not change `t`, the root cpuset, or
`S(t)`.  Because v1 permits the direct-ancestor overlap,

```text
S_after(t) = S_before(t)
S_after(t) intersection C = S_before(t) intersection C, which is nonempty.
```

Therefore child creation, `cpuset.cpu_exclusive=1`, child
`cpuset.sched_load_balance=0`, and exact campaign affinity cannot establish
the guardian invariant without an additional system-wide isolation action.
This is a constructive counterexample, not a probabilistic concern.

Primary kernel references:

- [Linux v1 cpuset documentation](https://docs.kernel.org/admin-guide/cgroup-v1/cpusets.html)
  states that an exclusive cpuset may overlap direct ancestors/descendants and
  explains the overlapping-load-balance behavior.
- [Linux cgroup v2 cpuset partition documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html#cpuset)
  defines partition roots whose exclusive CPUs cannot be used by outside
  cgroups, and states that partition control is parent-owned.

## Read-only host witness

At the audit snapshot:

- the v1 root cpuset allowed and effectively allowed CPUs `0-31`;
- its `cpuset.cpu_exclusive` and `cpuset.sched_load_balance` values were both
  `1`;
- all root cpuset control files were root-owned;
- the hybrid v2 hierarchy exposed an empty `cgroup.controllers` file, so no
  v2 cpuset controller or partition interface was available there;
- `/proc/cmdline` contained no `isolcpus`, `nohz_full`, or `rcu_nocbs`
  selection; and
- a complete read-only `/proc/*/task/*` affinity snapshot inspected 1,130
  live TIDs without a permission skip or race.  Of those, 859 had the complete
  `0-31` mask.  Each individual CPU was eligible for 867 through 869 inspected
  TIDs.

The exact counts are transient environmental evidence and select no CPU or
threshold.  Their only use is to prove materiality: whichever single
estimator CPU were chosen, hundreds of already-live foreign TIDs would satisfy
the guardian's rejection predicate.  No recorded or incident-exposed value
influenced the result.

## Alternatives and authorization boundary

There is no contract-preserving remedy inside the current mutation scope:

- rebinding every ambient process/thread would mutate unrelated tasks, must
  handle concurrent creation and exit, and lacks an exact reversible prior for
  a changing system population;
- changing systemd parent/sibling `AllowedCPUs` or reorganizing the host cgroup
  tree changes unrelated service eligibility;
- moving the cpuset controller from the active v1 hierarchy to v2 and creating
  a parent-owned isolated partition is a host-wide hierarchy operation not in
  the frozen profile or current authorization; and
- boot-time CPU isolation requires a kernel-command-line change and reboot,
  both expressly prohibited by the current addendum.

The technically clean resolution is an externally prepared dedicated boot or
an explicitly authorized and reviewed v2 isolated-partition design whose
parent ownership, task placement, interruption behavior, and exact restoration
are data-free proved before profile freeze.  The alternative is explicit
authorization to mutate and restore unrelated task/cgroup eligibility, but the
dynamic-population restoration problem makes that materially weaker and is
not recommended.

Until one exact mechanism is approved, implemented, and proved effective, the
final profile cannot be frozen, the root installation procedure must not be
run, the data-free mutation trial must not begin, and CP2-E recorded execution
remains locked.
