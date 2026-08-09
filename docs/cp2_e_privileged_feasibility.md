# CP2-E privileged-control feasibility record

Status: **failed pre-data predicate; actual CP2-E remains locked**

Recorded at: `2026-08-04T16:41:48-04:00`

Target: desktop `x86_64`, Linux `5.15.0-67-generic`, effective uid `1000`

## Authority and scope

This data-free check was performed under Moksh Trehan's CP2-E addendum recorded
in `project/cp2_e_completion_authorization_addendum.txt` (SHA-256
`96b758e8ed8346720ab9fa1006c6c8c3cb68b31448fe54b676a71ee2e14149eb`,
Git blob `3f9ea1f33f25a66f8cf715bfcd88cdc823a4878e`). The addendum permits
noninteractive use of already-configured privileged authority for exact,
temporary, reversible timing controls. It expressly requires CP2-E to stop if
that authority or exact restoration cannot be proved.

No dataset registry, bag, ground truth, trajectory, recorded result, or
recorded-workload timing was opened or collected. No host control, service,
module, affinity, cgroup, clock, or device state was changed.

## Observations

The exact privilege probe `/usr/bin/sudo -n /usr/bin/true` exited `1` with
`sudo: a password is required`. A second read-only policy probe,
`/usr/bin/sudo -n -l`, failed with the same diagnostic, so no narrower
preconfigured `NOPASSWD` command inventory could be established. Codex did not
request, extract, store, or use a password or other credential.

Read-only observations confirmed that the controls which a viable profile
would have to change are not already in the required state:

- `irqbalance` was active;
- `k10temp` was installed but not loaded, while `msr` was loaded;
- global cpufreq boost was `1`;
- CPU 0 used governor `ondemand`, with scaling bounds
  `3000000`/`4300000` kHz;
- CPU 0 idle state `C1` was enabled; and
- the inspected cpufreq, cpuidle, and IRQ affinity surfaces were owned by uid
  `0` and not writable by the recording uid; and
- `/dev/cpu_dma_latency` was a root-owned mode-`0600` character device, so the
  proposed latency constraint also had no unprivileged apply path.

## Disposition

The mandatory noninteractive-authority predicate fails before any trial
mutation. Therefore no final timing profile can truthfully bind proved control
effectiveness and exact restoration on this host, and CP2-E source freeze,
formal gate, recorded timing, and publication are ineligible. The evidence
standard is not weakened and no retry is made under the unchanged host state.

Data-free implementation, synthetic testing, and mathematical/evidence audit
may continue. To resume the chained transition, the user must configure
noninteractive privilege outside Codex; a later fresh feasibility transaction
must then capture, apply, validate, restore, and verify the exact candidate
profile without recorded input before source freeze.

An exact fail-closed launcher, held-profile binder, data-free client, and
sudoers candidate
have now been prepared without installation or privilege use:

- `scripts/cp2/cp2_timing_root_launcher.c`;
- `scripts/cp2/cp2_timing_production_identity.py`;
- `scripts/cp2/cp2_timing_reversibility_client.py`;
- `packaging/cp2e/schurvio-cp2e.sudoers`; and
- `docs/cp2_e_user_installation_candidate.md`.

They remain candidates only.  A fully bound candidate receipt is labelled
`candidate_profile_binding_valid_privileged_feasibility_unproved`.  The Python
entry point permits only the data-free reversibility transaction and keeps
formal execution locked.  No profile identity may be frozen from these files
until the isolation prerequisite, user-side installation, and subsequent
single data-free reversibility trial all pass.

## Continued data-free source and control audit

The subsequent audit found and corrected a semantic encoding error before any
host mutation or recorded-input access.  Linux exposes
`/proc/irq/default_smp_affinity` as a hexadecimal bitmap, whereas each
`/proc/irq/<n>/smp_affinity_list` surface is a decimal CPU-list.  Treating both
as CPU-lists is not conservative: for example, text `3` denotes CPU 3 in a
CPU-list but bitmap bits 0 and 1 in the default surface.  The backend and the
detached verifier now independently encode the default set in big-endian,
comma-separated, lowercase 32-bit words, decode every set bit, and require an
exact round trip.  For the synthetic four-CPU protecting topology, the
housekeeping singleton `{3}` is therefore `00000008`; multiword bit 32 is
`00000001,00000000`.  Empty, uppercase, malformed, over-wide, and wrong-parser
forms reject.  Per-IRQ `_list` controls remain decimal CPU-lists.

The same audit replaced syntactic-only source-provenance fields with backend
reconstruction.  Before capture, the backend recomputes the complete CPU/SMT,
cpuset-mount, cpufreq, CPU-online/idle, and IRQ-control source identities from
typed structure and inode metadata.  After the authorized modules are loaded,
it independently recomputes every APERF/MPERF, reference-frequency,
current-frequency, k10temp, and AMD-throttle source identity.  Applied-state
and guardian validation repeat those comparisons; restored validation repeats
the pre-module control comparison.  Any mismatch rejects rather than trusting
a profile-carried digest.

The launcher now retains a fourth root-owned canonical profile FD and its own
executing inode, compiles the complete nonrecursive plan digest, and passes
only fixed descriptors into isolated Python.  This closes substitution of the
three project Python sources, profile, launcher, and interpreter, but **does
not close the effective root runtime**.  A production-like
`/usr/bin/python3.8 -I -B -S` probe loaded 65 module files and mapped 16
runtime files, including Python stdlib modules, extension DSOs, the dynamic
loader, libc, libcrypto, libexpat, libm, libpthread, libutil, and libz.  The
installed sudo binary/plugin/config/PAM/rule closure is likewise not fully
verified.  The former four-file installed-closure claim is therefore
withdrawn; a private runtime or separately frozen OS/sudo TCB manifest is a
source-freeze prerequisite.

The only enabled production adapter remains a data-free, durable-prior,
apply/validate/restore trial.  Formal six-run execution is unreachable.  The
deterministic binder and installation draft are in
`scripts/cp2/cp2_timing_production_identity.py` and
`docs/cp2_e_user_installation_candidate.md`.  They were tested only with
unprivileged synthetic/temp fixtures; no root installation or host trial was
performed.

The later strictly offline audit in
`docs/cp2_e_offline_descendant_confinement_clarification_proposed.md` found
that universal foreign-affinity disjointness is not a property a v1 child
cpuset can establish and that affinity eligibility is not observed scheduler
occupancy.  Its non-authorizing replacement limits the hard claim to the
stabilized complete campaign descendant PID/TID closure and retains complete
canonical foreign-affinity eligibility rows as non-gating observations.  An
empty, nonempty, or changing foreign population cannot change feasibility,
timing math, or pass/fail; malformed or substituted evidence still rejects.

The implementation candidate and synthetic data-free tests follow that
proposal, but this does not unlock the failed privilege disposition above.
The proposal and exact implementation still require explicit human approval,
and no final profile, root installation, reversibility trial, or formal run
has occurred.  The unavailable-sudo disposition remains an independent hard
stop.

## Final read-only host follow-up

A later local-only inventory, performed without network access or host
mutation, tightened the thermal blocker:

- the processor is a Ryzen 9 9950X on Linux `5.15.0-67-generic`;
- the CPU host-bridge device is `1022:14e3`;
- the installed, unloaded `k10temp` module's `modinfo` alias table does not
  include `14e3`;
- no CPU hwmon temperature input, CPU `thermal_throttle` counter, `/dev/hsmp`,
  or installed `amd_hsmp` module is present; and
- `msr` is loaded, while `acpi-cpufreq` exposes 3.0 and 4.3 GHz states,
  userspace governor support, and global boost control.

This does not prove that a privileged `k10temp` load would fail, because no
module-load trial was authorized/reachable.  It does prove that the final
profile's required bound CPU temperature/throttle source is presently absent
and may not be guessed.  The clock surfaces appear structurally plausible,
but both control effectiveness and exact restoration still require the one
privileged data-free transaction.

The final synthetic matrix passed 189/189 checks, including 13/13 privileged
backend, 48/48 helper, and 39/39 independent artifact/profile checks.  This
does not discharge the runtime-closure, numeric PID/TID mutation-race,
P1-to-P2 adversarial-recovery, privilege, telemetry, actual-mode, or final
profile blockers.
