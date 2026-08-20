# Orin environment manifest — C5 bring-up (2026-08-20)

Status: `ACTIVE — environment frozen for C5 bring-up and all subsequent Orin campaigns`

Recorded: 2026-08-20 (UTC capture `2026-08-20T12:27:58Z`), by the desktop
Claude Code session driving the board over SSH (`ssh orin`), per
`ORIN_BRINGUP_SESSION.md`. Raw read-only collector output
(`schurvio.icra27.orin_readiness.v1`) is committed alongside as
`orin_readiness_c5.txt`; this manifest summarizes and pins it.

## Board identity

| Field | Value |
|---|---|
| Device | NVIDIA Jetson Orin Nano Engineering Reference Developer Kit **Super** |
| L4T / JetPack | L4T R36.4.4 (`nvidia-l4t-core 36.4.4-20250616085344`); `nvidia-jetpack` metapackage not installed |
| Kernel | `5.15.148-tegra #1 SMP PREEMPT Mon Jun 16 08:24:48 PDT 2025 aarch64` |
| Host OS | Ubuntu 22.04.5 LTS (aarch64) |
| Hostname / address | `jetsonvio-desktop`, 192.168.68.53, SSH alias `orin`, passwordless key auth + NOPASSWD sudo |
| RAM | 7.4 GiB |
| Storage | microSD `mmcblk0` 119.2 GB; rootfs `mmcblk0p1` (ext4); datasets at `/data` (same device — no NVMe present) |
| Clock sync | NTP synchronized (`NTPSynchronized=yes`), TZ America/New_York |

## Power / clocks (FROZEN)

| Field | Value |
|---|---|
| nvpmodel mode | **ID 0 — 15W** (chosen 2026-08-20; frozen for all measured Orin work) |
| Mode menu at freeze | 0=15W, 1=25W (shipped default), 2=MAXN_SUPER, 3=7W |
| CPU | 6× Cortex-A78AE online, governor `schedutil` on all cores, caps 729.6 MHz – 1497.6 MHz (15W table) |
| GPU | Ampere iGPU present, capped 306–612 MHz under mode 0 — **explicitly unused**: CPU-only build, no CUDA in the container, `CUDA_VISIBLE_DEVICES=""` convention at run time |
| jetson_clocks | NOT engaged (no static max-clock pinning); DVFS runs under schedutil within the 15W caps. No thermal/clock tuning between runs, per hard rules |
| Fan / thermal | `nvfancontrol` active (default profile), fan responding (pwm≈43 idle); board open-air; idle temps ≈41–42 °C across zones; idle VDD_IN ≈3.0 W |
| Boot target | `multi-user.target` (headless; no GUI at measurement time) |

## Update / interference freeze

- All 55 `nvidia-l4t-*` packages: `apt-mark hold`
- `unattended-upgrades`: disabled; `packagekit`: stopped + disabled
- zram (`nvzramconfig`): disabled (0 B swap at boot, verified post-reboot)
- `/swapfile` 8 GiB exists, **OFF by default** — `swapon` permitted only for
  builds; MUST be `swapoff` before any measured run (verified 0 B before runs)

## Build environment (containerized ROS1)

The board's Ubuntu 22.04 has no ROS1; the desktop reference machine is
Ubuntu 20.04 + ROS Noetic. The frozen Orin build therefore runs in an arm64
Docker container reproducing the desktop's userland. Docker 28.2.2 (host),
storage driver overlay2.

| Field | Value |
|---|---|
| Base image | `ros:noetic` (arm64) digest `sha256:72b8bc59035dc0a5b8e07aae28c16caa84192971d72d207c72ed734fb1d5e97d` |
| Build image | `schurvio-orin-env:c5` — Dockerfile committed as `docs/icra27/Dockerfile.orin-noetic`; image ID recorded below after build |
| Image ID | `sha256:e1c338f7b1332dfb967ac442e70939de88c20b739b40647997c24fceb4454947` (2.92 GB) |
| Run image | `schurvio-orin-env:c5-run` = `sha256:4c7748709cea93d6677dc1fd97312a245445b976102e3cf2ef621f7afda77520` — identical plus the `time` package (`/usr/bin/time` for per-cell resource accounting); estimator replays execute in this image |
| Container policy | CPU-only (no `--gpus`, no CUDA libs); bind mounts: frozen source, `/data` datasets, artifacts root. Timing runs execute natively on host cores via the container (no CPU virtualization layer) |

## Frozen source identity (on-board)

| Field | Value |
|---|---|
| Clone | `/data/schurvio-lite-frozen` (from git bundle of the desktop repo) |
| Commit | `2751bcdc0fae25c993b3224dc5fa40aaab6571d7` (frozen science commit, tag `frozen-orin-2751bcd`) |
| Tree | `b3f9191b8bce3852ebff71e1ebf180181bcd6d05` — **verified equal to desktop** `git rev-parse 2751bcd^{tree}` |
| Working tree | clean (`git status --porcelain` empty) at checkout |
| Build scripts | repo-canonical `scripts/cp0/bootstrap_ceres_1_14.sh` (Ceres 1.14.0 @ `facb199`, MINIGLOG, CUSTOM_BLAS) + `scripts/cp0/build_ros1.sh` (catkin, RelWithDebInfo), run unmodified inside the container |

## Orin frozen build (C5 result)

Built 2026-08-20 inside `schurvio-orin-env:c5` as uid 1000, via unmodified
`scripts/cp0/build_ros1.sh` (`CP0_BUILD_JOBS=4`, swap ON for build only). All
5 packages succeeded; tracked source clean afterward. Full provenance
committed as `docs/icra27/orin_build_provenance_c5.json`.

| Field | Value |
|---|---|
| Compiler / CMake | gcc 9.4.0 (Ubuntu 9.4.0-1ubuntu1~20.04.2), cmake 3.16.3, catkin_tools, `RelWithDebInfo` (flags per `build_ros1.sh`, unchanged) |
| Aggregate source SHA-256 | `ae0ebfd0b4e2935b07d43c1dca324b0263541ea6f8cb171c1929132395e84213` — **identical to desktop CP0 provenance** (same source file set byte-for-byte) |
| **Orin frozen binary** | `build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf` (aarch64 ELF, 37,218,192 B) SHA-256 `b9f4f0348c56ec7fa97b823bc81533b405b6bf046bf53a7090327c6cdfc37708` |
| Vendored Ceres (arm64) | `libceres.so.1.14.0` SHA-256 `782b07f7597f29dc41487d233629b0bc80fcdc796313640067b34c5641443b2a` |
| x86 desktop binary (contrast, not a target) | SHA-256 `0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e` — differs by architecture as expected; per protocol it must not be "fixed" to match |

## Dataset identity (on-board, `/data`)

| Set | Result |
|---|---|
| KAIST-VIO `turnsafe_adapted` (11 bags, 32 G) | 11/11 SHA-256 byte-identical to desktop originals (`sha256sum -c` vs desktop manifest, all OK) |
| EuRoC (11 bags, ~13 G) | 8/11 verified against tracked SHA-256 in `project/datasets.yaml` (see `orin_euroc_verify.txt`); remaining 3 bags (not hash-tracked in datasets.yaml) byte-size-identical to desktop copies |
| Free space after transfer | ≈31–40 G on `mmcblk0p1` |

## Deviations / notes

- First `ov_msckf` configure inside the container failed at the frozen
  source's own provenance step (`git rev-parse HEAD` in `cmake/ROS1.cmake`):
  git's "dubious ownership" guard fires because the container user (root)
  differed from the bind-mount owner (uid 1000). The snapshot layer invokes
  git with a scrubbed environment (no `HOME`, `GIT_CONFIG_NOSYSTEM=1`), so no
  git-config `safe.directory` exception can apply — fail-closed by design.
  Resolved environment-side by running the build container as `-u 1000:1000`
  (matching the repo owner), which satisfies git's ownership check natively.
  **No source modification.**

- The stock readiness collector's 6 s `tegrastats` sample outlives its
  `timeout --signal=INT` wrapper (sudo child survives SIGINT to sudo); the
  sample in `orin_readiness_c5.txt` is therefore ~47 s long and was terminated
  manually. Cosmetic; readings unaffected.
- `nvidia-jetpack` metapackage absent (component packages installed directly
  by the flashing tool); L4T core version is authoritative.
- Board local time zone is America/New_York while the desktop is
  America/Toronto; both NTP-synced, all recorded timestamps UTC.
