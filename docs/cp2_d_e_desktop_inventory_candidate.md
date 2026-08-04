# CP2-D/E desktop candidate inventory

Status: **data-free, read-only, non-authorizing candidate**

Inventory date: 2026-08-04 (America/Toronto)

Execution target selected by Moksh Trehan: desktop `x86_64` for CP2-D and the
frozen fixed-clock desktop checkpoint for CP2-E. Jetson work is deferred beyond
CP2.

This inventory used only local executable/package metadata and read-only Linux
topology, affinity, cpufreq, idle-state, and thermal surfaces. It did not open a
dataset registry, bag, ground-truth file, trajectory, recorded artifact, result
tree, or build tree. It did not use the network, install or modify a package,
change a host control, execute a numerical known-answer test, or run a timing
workload. Nothing in this document is CP2-D/E evidence or authority to execute
those checkpoints.

## CP2-D evaluator and direct-math candidates

- `/usr/bin/python3` resolves to `/usr/bin/python3.8`, reports CPython 3.8.10,
  and is an x86-64 ELF. Its SHA-256 is
  `298a9e830ed52f36c299427565485d717d1ce0179c0597cc16560513eb780b06`.
  The installed package metadata identifies `python3` and `python3-minimal`
  version `3.8.2-0ubuntu2`, architecture `amd64`.
- The only observed evo candidate is user-local evo 1.31.1. Its console
  scripts are under `/home/moksh/.local/bin`, use `#!/usr/bin/python3`, and are
  mode `0775`, uid/gid `1000/1000`. The evo package and distribution-metadata
  directories are also mode `0775`. The `evo_ape` wrapper SHA-256 is
  `6bee25dc5bfdab0ead8988ab4014a72511339e94697ec61699f66f68f5f24d15`,
  and its metadata entry point is `evo.entry_points:ape`.
- Ordinary `/usr/bin/python3` metadata discovery sees user-local NumPy 1.24.4
  and SciPy 1.10.1. Required isolated execution with
  `/usr/bin/python3 -I -B` excludes that user site: evo and SciPy are absent,
  while NumPy resolves instead to system NumPy 1.17.4.
- Evo is GPLv3, NumPy is BSD-3-Clause, and SciPy metadata carries its BSD-family
  and bundled numerical/runtime notices. Moksh Trehan has accepted applicable
  GPLv3 Corresponding Source and license-notice obligations for conveyed
  artifacts, but an eventual capsule still needs an exact retained source,
  license, and notice inventory.

These bytes are not an acceptable capsule. The evo surface is ambient,
group-writable, shebang/host-Python resolved, and absent from the isolated
execution environment. There is no immutable capsule archive, private relative
launcher, complete Python/module/native-library closure, approved single-thread
environment, dataset-free evaluator preflight, or stack-specific direct-math
known-answer record. The isolated NumPy 1.17.4 versus stated NumPy 1.24
semantics is an independent numerical blocker. Evo 1.31.1's six-decimal console
format also remains incompatible with the frozen full-precision direct-RMSE
tolerance unless the proposed result-archive replacement is separately
reviewed and approved.

Consequently, no evaluator/direct-math capsule identity is selected, committed,
or approved. CP2-D recorded access and execution remain unauthorized.

## CP2-D evidence-protocol blockers

The evaluator capsule is not the only D blocker. The current sequence artifact
does not retain the complete chronological filtered camera-message candidate
view (or an equivalently sufficient independent witness). Its detached verifier
can validate the retained pair, but cannot independently recompute and prove
that it was the first forward opposite-camera candidate and that the producer
did not search past an earlier candidate. Correct selector code and equality of
runtime/extractor bytes do not close that detached-proof gap. A separately
reviewed schema/evidence extension must retain enough information for exact
first-forward/no-search-past recomputation and must reject a coordinated later
eligible candidate in protecting tests.

The current D publication path is also not ready for authorization. Sequence
publication helpers retain post-rename failure windows in which a parent-fsync,
rollback, or interruption failure can leave a final name while the caller
reports failure or cleans only the former hidden name. Before D approval, the
path must use descriptor-relative no-replace publication, prove parent-fsync
ordering, reconcile the exact held inode to one authoritative name, and report
an explicit indeterminate state whenever exact rollback/durability cannot be
proved. Protecting tests must cover post-rename fsync failure, rollback failure,
name collision, and interruption recovery.

These protocol blockers are independent of evaluator/license/KAT closure and
must be resolved before any exact D capsule approval can authorize execution.

## CP2-E host/profile candidates

- Host CPU: AMD Ryzen 9 9950X, one socket, 16 physical cores, 32 online logical
  CPUs, SMT2, one NUMA node. Sibling pairs are `0/16`, `1/17`, through `15/31`.
- The current process is eligible on CPUs `0-31`; no affinity isolation is in
  force. The observed kernel command line contains no CPU-isolation,
  `nohz_full`, or RCU-offload selection.
- The host exposes 32 `acpi-cpufreq` policies, one per logical CPU. Each
  reported governor `ondemand`, scaling bounds 3,000,000/4,300,000 kHz, and
  CPU-info bounds 3,000,000/8,839,355 kHz. Global cpufreq boost is enabled.
  Read-only instantaneous values varied; they are environmental observations,
  not checkpoint timings.
- CPU0 exposes enabled `POLL`, `C1`, `C2`, and `C3` idle states. The CPU reports
  `constant_tsc`, `nonstop_tsc`, `aperfmperf`, `cpb`, and `hw_pstate` features.
- No `thermal_zone` surface was present. The readable hwmon temperatures were
  for NVMe/network devices, not a bound CPU package/core thermal sensor.

This host is not currently eligible for the frozen fixed-clock profile:
frequency control is dynamic, boost and idle states are enabled, workload
affinity is not isolated, and required CPU thermal/throttle observability is
absent. No control was changed to test a remedy. No CPU set, governor/frequency
state, boost policy, thermal policy, observation command set, or resource bound
is selected as a profile identity.

Consequently, `project/cp2_timing_profile.yaml` remains absent, no exact
CP2-E profile is committed or approved, and CP2-E recorded access and execution
remain unauthorized.

## Required next approval boundary

Before CP2-D, a separate reviewed commit must resolve both evidence-protocol
blockers above and contain the exact private evaluator and direct-math capsule
bytes/inventories, license/source closure, launcher and native dependency graph,
sanitized environment, and complete preflight/known-answer bits. Capsule
closure alone is insufficient. Before CP2-E, a separate reviewed commit must
contain an exact feasible fixed-clock profile and all machine/CPU, affinity,
clock, boost, idle, thermal, sequence/input, command, and resource identities.
Moksh Trehan must explicitly approve those exact committed identities before
either recorded runner is enabled. No ambient fallback, profile substitution,
host-control mutation, retry, or data-derived tuning is authorized.
