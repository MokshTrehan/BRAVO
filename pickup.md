# SchurVIO-Lite CP2 pickup

Last updated: 2026-08-04 (America/Toronto)

## Outcome at this handoff

CP2 is **not complete** and no CP2-C, CP2-D, or CP2-E formal/recorded result
has been claimed.  The branch now contains a substantially complete,
data-free CP2-C/D implementation candidate and a fail-closed CP2-E evidence
candidate.  The mathematical and synthetic diagnostics described below are
green.  CP2-E still has source/runtime and host-feasibility blockers, so the
final source freeze, the one authorized formal unit gate, and the serialized
C -> D -> E recorded chain were deliberately not started.

- Repository: `/home/moksh/newSlam variant`
- Branch: `schurvio-lite/cp2-one-pass`
- Starting authorization commit: `1218d3fb73066760f099f87c621fe607f9de8b63`
- Data-free implementation/handoff candidate commit:
  `d960f90567bf113d9d04a4449bc5c9721c250aea`
- Candidate tree: `575f5819696d2f50f3ec447f6025a5b71e75b9cf`
- The later pickup-only commit at the branch tip changes no implementation,
  contract, incident log, profile, threshold, or test source.
- Pinned OpenVINS upstream: `69488123ed9362dd44b6f28e7f4680abbff1442b`
- Desktop x86_64 remains the CP2 target.  Jetson work remains deferred beyond
  CP2.
- No dataset registry, bag, ground-truth file, or recorded-result tree was
  accessed during this continuation.
- No package was installed, no network request was made, and no host control,
  service, module, affinity, cgroup, frequency, IRQ, or thermal state was
  changed.

The governing chained authorization is
`project/cp2_completion_chained_authorization.txt` (original attached bytes:
8,606; SHA-256
`8e07b1e1d5418b65b5d1993e791e9dab5340d7e77f9ea687e6d064e3bc803cca`).
Its D addendum is `project/cp2_d_completion_authorization_addendum.txt`; its E
addendum is `project/cp2_e_completion_authorization_addendum.txt`.  The latest
user message also permits dependency installation if genuinely blocked.  No
dependency blocker was found, and the E addendum's noninteractive-privilege
and exact-evidence requirements remain unsatisfied.

## What is implemented

### CP2-C

- Approval-bound readiness/source-snapshot conformance repair, including
  tracked files beneath separately snapshotted roots.
- Exact serial lifecycle/timing trace, fatal-latching journal, output
  capability, checked counters, and failure-atomic commit boundary.
- Production shared feature gate and retained nullspace/Schur math evidence.
- Detached assembly/replay and descriptor-bound, no-replace, reconciled
  publication paths.

### CP2-D

- Complete chronological camera-candidate witness and detached replay of
  first-forward, no-search-past, strict `< 20 ms`, and no-reuse pairing.
- Shared nullspace-derived Kabsch transform, exact integer timestamp
  association, translation ATE RMSE, p95 position/orientation parity, and
  relative ATE checks.
- Private evaluator/direct-math capsule builder, isolated launcher,
  full-precision result archive, independent direct recomputation, known-answer
  tests, relocation checks, license notices, and source inventory.
- Descriptor-relative, no-replace sequence publication with rollback and
  reconciliation fault injection.

The checked-in capsule identity files are still candidate bindings.  A fresh
final capsule must be regenerated and frozen after the final source is stable;
the older sealed candidate must not be represented as the final CP2-D capsule.

### CP2-E candidate

- Exact integer/rational p50, p95, ratio, median-of-three, overflow, and
  all-pairs-must-pass timing math.
- Schema-v2 profile, raw APERF/MPERF/frequency/temperature/throttle evidence,
  guardian stream, independent artifact verifier, and failure-atomic control
  transaction candidate.
- Estimator versus variable peer-to-helper control-chain partition, complete
  descendant PID/TID witnesses, held descriptor-relative `/proc` identities,
  helper-owned evidence files, durable recovery journal, root launcher,
  profile binder, and least-privilege sudoers candidate.
- Offline replacement for the quarantined web-informed cpuset conclusion.  It
  limits the hard claim to the complete campaign descendant closure and keeps
  foreign affinity eligibility observational and non-gating.

Formal CP2-E actual mode remains intentionally locked in
`scripts/cp2/run_timing_pair.py` and `ov_msckf/src/ros1_serial_msckf.cpp`.

## Math-first verification completed

The estimator derivation was independently rechecked before handoff:

- direct scalar whitening, full Householder nullspace projection, SVD rank and
  conditioning boundaries, and retained `lambda`, `eta`, and `gamma` agree
  with the approved contract;
- the shared feature gate constructs `S = H P H^T + sigma^2 I`, uses the
  frozen strict chi-square comparison, and shares the exact marginal prior;
- state/proposal/covariance joins, commit-oracle semantics, failure atomicity,
  checked counters, and no-repair/no-fallback behavior remain intact;
- Kabsch uses `C = sum((y-ybar)(x-xbar)^T)/N`,
  `R = (U D) V^T`, `t = ybar - R xbar`; the same transform is applied to
  both modes; and ATE RMSE is `sqrt(sum(||p-g||^2)/N)`;
- timing quantiles use exact NumPy-linear ranks `1/2` and `19/20`; gates use
  exact cross-products against `11/10` and `23/20`, inclusively, with every
  pair required to pass.

No unresolved estimator-math discrepancy was found.  The remaining E
blockers concern evidence reachability, lifecycle safety, provenance, and host
feasibility, not a known error in the timing-ratio equations.

## Data-free diagnostics

These are protecting diagnostics only.  They are not the one formal unit gate
and do not constitute CP2 recorded evidence.

- C++ diagnostic build: 25/25 test executables passed (216 captured cases in
  the aggregated diagnostic inventory).
- Focused CP2-C/D Python matrix: 256/256 passed.
- CP2-E Python matrix: 189/189 passed:
  - controls 19/19;
  - artifact/profile evidence 39/39;
  - exact timing math 42/42;
  - orchestration 24/24;
  - privileged backend 13/13;
  - privileged helper 48/48; and
  - root launcher 4/4.
- `packaging/cp2e/schurvio-cp2e.sudoers` parses successfully with `visudo`.
- `git diff --check` was clean before the handoff commit.

## Hard blockers

### 1. Noninteractive privilege is absent

Both `/usr/bin/sudo -n /usr/bin/true` and `/usr/bin/sudo -n -l` fail with
`sudo: a password is required`.  The authorization permits only
noninteractive sudo and forbids requesting, extracting, storing, or repurposing
credentials.  Membership in `docker`/`lxd` is not an acceptable substitute or
privilege-escalation path.

### 2. The required CPU thermal/throttle witness is not proved

Read-only inventory found Linux `5.15.0-67-generic` on a Ryzen 9 9950X.  The
CPU host-bridge device is `1022:14e3`, but the installed, unloaded `k10temp`
module's `modinfo` alias table does not include `14e3`.  There is no CPU hwmon
sensor, no CPU `thermal_throttle` counter, and no installed `amd_hsmp` device
or module.  `msr` is loaded, and `acpi-cpufreq` exposes 3.0/4.3 GHz states,
userspace governor support, and a boost control; clock control may therefore
be trial-feasible, but adequate bound temperature/throttle evidence is not.
The E addendum requires a stop rather than a weakened profile.

### 3. CP2-E production/runtime proof is incomplete

- There is no real `project/cp2_timing_profile.yaml`, privileged reversibility
  receipt, or production six-run actual-mode adapter.
- The root launcher binds three project Python sources, its interpreter, and
  the profile, but the effective Python stdlib/native runtime is not closed.
  A production-like probe loaded 65 module files and 16 mapped runtime files.
  The installed sudo binary/plugin/config/PAM/rule closure is also not fully
  verified.  The current four-file closure claim must not be used.
- Held `/proc` descriptors detect PID/TID exit or reuse, but a numeric
  affinity/cpuset mutation can still race reuse between its precheck and
  syscall.  A lifecycle pin/freeze design and adversarial test are required to
  prove no unrelated task can be mutated.
- The late P1-to-P2 recovery path is fail-closed but lacks the complete
  real-backend adversarial matrix for live/absent predecessor, mid-recovery
  exit, PID/TID reuse, partial old chain, and new-chain collision.

### 4. Formal-transition prerequisites remain

- The two 2026-08-04 CP2-E network-scope incidents at
  `17:45:11-04:00` and `21:31:51-04:00` in
  `docs/cp2_predata_incident_log.md` remain unacknowledged at an exact committed
  identity.  Their web-derived content remains quarantined.
- Final D capsule and E profile identities are not frozen.
- No final clean source-freeze commit/tree exists.
- The exactly-once formal unit gate has not been invoked.
- CP2-C/D/E recorded execution and detached verification remain not run.

## Exact continuation order

1. Review this pushed candidate and acknowledge the two exact incident
   dispositions at its committed identity.
2. Resolve the E platform prerequisite outside the formal chain: configure
   the exact root-owned helper/sudo rule for noninteractive use and provide a
   kernel/telemetry arrangement that exposes an authoritative, profile-bound
   CPU temperature/throttle witness.  A kernel/reboot/new-module path requires
   explicit authorization beyond the current E addendum.
3. Close the root Python/OS/sudo runtime manifest, eliminate the numeric-task
   mutation race, complete recovery tests, and implement the real E profile
   builder and six-run actual adapter.  Re-run all data-free audits.
4. Install the exact reviewed helper/profile and run one data-free privileged
   apply/validate/restore feasibility transaction.  Any failure or
   indeterminate restoration stops; do not retry or tune.
5. Regenerate and freeze the final D capsule, E profile, source/readiness
   digests, and one clean source-freeze commit/tree.
6. Only then invoke the one authorized formal unit gate.  If it passes, run
   exactly C, then the three D sequences in the frozen order, then E.  Any
   failure stops the remainder without retry.

CP3 must not begin until the final CP2 evidence package exists and Moksh Trehan
provides the separate final human sign-off.
