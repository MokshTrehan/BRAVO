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
| Container policy | CPU-only (no `--gpus`, no CUDA libs); bind mounts: frozen source, `/data` datasets, artifacts root. Timing runs execute natively on host cores via the container (no CPU virtualization layer) |

## Frozen source identity (on-board)

| Field | Value |
|---|---|
| Clone | `/data/schurvio-lite-frozen` (from git bundle of the desktop repo) |
| Commit | `2751bcdc0fae25c993b3224dc5fa40aaab6571d7` (frozen science commit, tag `frozen-orin-2751bcd`) |
| Tree | `b3f9191b8bce3852ebff71e1ebf180181bcd6d05` — **verified equal to desktop** `git rev-parse 2751bcd^{tree}` |
| Working tree | clean (`git status --porcelain` empty) at checkout |
| Build scripts | repo-canonical `scripts/cp0/bootstrap_ceres_1_14.sh` (Ceres 1.14.0 @ `facb199`, MINIGLOG, CUSTOM_BLAS) + `scripts/cp0/build_ros1.sh` (catkin, RelWithDebInfo), run unmodified inside the container |

## Dataset identity (on-board, `/data`)

| Set | Result |
|---|---|
| KAIST-VIO `turnsafe_adapted` (11 bags, 32 G) | 11/11 SHA-256 byte-identical to desktop originals (`sha256sum -c` vs desktop manifest, all OK) |
| EuRoC (11 bags, ~13 G) | 8/11 verified against tracked SHA-256 in `project/datasets.yaml` (see `orin_euroc_verify.txt`); remaining 3 bags (not hash-tracked in datasets.yaml) byte-size-identical to desktop copies |
| Free space after transfer | ≈31–40 G on `mmcblk0p1` |

## Deviations / notes

- The stock readiness collector's 6 s `tegrastats` sample outlives its
  `timeout --signal=INT` wrapper (sudo child survives SIGINT to sudo); the
  sample in `orin_readiness_c5.txt` is therefore ~47 s long and was terminated
  manually. Cosmetic; readings unaffected.
- `nvidia-jetpack` metapackage absent (component packages installed directly
  by the flashing tool); L4T core version is authoritative.
- Board local time zone is America/New_York while the desktop is
  America/Toronto; both NTP-synced, all recorded timestamps UTC.
