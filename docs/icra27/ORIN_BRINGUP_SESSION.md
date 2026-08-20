# ORIN_BRINGUP_SESSION — C5 (interactive, today; bounded to bring-up, NOT the campaign)

**Architecture:** Claude Code runs on the desktop (proven environment) and drives the Orin Nano over SSH. Human does the physical steps; the agent does everything scriptable. Session budget: one day. The feature-budget sweep (C6) does not start until every acceptance box below is checked.

## Human pre-steps (before launching the agent)
- [ ] Flash/verify JetPack; boot; enable SSH; note the board's IP and user
- [ ] Passwordless SSH from desktop (`ssh-copy-id`); confirm `ssh orin true` works
- [ ] Datasets: decide transfer path (copy the KAIST 11 + EuRoC stable subset to the board's NVMe/SD — record device and free space)
- [ ] Cooling: fan connected and spinning; board not in an enclosure

## Agent scope (in order)
1. **Environment freeze:** record on the board: L4T/JetPack version, kernel, `nvpmodel -q` (set and record the chosen power mode), `jetson_clocks --show` state, CPU governor per core, GPU explicitly unused (no CUDA in build), storage device, ambient/fan state. Commit as `orin_env_manifest.md` alongside the desktop manifest.
2. **Frozen-source build:** clone the repo on the board at the frozen science commit (2751bcd lineage; verify tree hash), build CPU-only. Record compiler, flags, and the resulting binary sha256 — this is the *Orin frozen binary*; it will not match x86 and must not be "fixed" to.
3. **On-board determinism gate:** run one KAIST sequence (rotation_fast) and one EuRoC sequence twice each, identical config. The two runs of each MUST be byte-identical to each other. Cross-check vs desktop trajectories is *recorded as descriptive* (expect small FP differences across architectures) — never asserted, never patched.
4. **Timing + deadline accounting:** verify the runner captures per-callback updater latency (p50/p95/p99/max) and counts deadline misses at the NATIVE camera period — 33.3 ms KAIST, 50 ms EuRoC. The 20 Hz figure from the old §9.1 must not appear anywhere in the KAIST path.
5. **Power telemetry:** sample the onboard rails (tegrastats or sysfs INA channels) at a fixed declared rate; record idle baseline for 60 s; verify energy-per-update integration produces sane numbers on the smoke runs. If a wall/USB-PD meter is available, log one manual cross-check reading (optional, descriptive).
6. **Smoke cells:** one full cell per dataset (KAIST rotation_fast, EuRoC MH_05) with the complete metric set, SHA256SUMS, and the established cell-record format, landing in a new `orin-bringup-…` artifacts root.

## Hard rules (unchanged)
Estimator source read-only on both machines; the Orin build is from the frozen commit, not a branch with "small ARM fixes" — if the frozen source does not compile on ARM without modification, STOP and report exactly what fails; that is a session outcome, not an obstacle to route around. No thermal/clock tuning between runs. Everything checksummed and committed.

## Acceptance (all required before C6 is permitted)
- [ ] `orin_env_manifest.md` committed; power mode and clocks pinned and recorded
- [ ] Frozen-source build succeeds unmodified; binary sha256 recorded
- [ ] Within-Orin byte-determinism: 2/2 sequences reproduce exactly
- [ ] Deadline accounting verified at native periods on both smoke cells
- [ ] Power sampling produces plausible, integrable traces with recorded idle baseline
- [ ] Two complete smoke cells in the artifact format

## Explicit non-goals today
The feature-budget sweep; any campaign; contention profiles; N0 rows (same binary — one flag, no extra build); performance tuning of any kind. Bring-up produces *trust*, not numbers.
