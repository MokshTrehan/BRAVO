# ORIN_BRINGUP_REPORT — C5 (2026-08-20)

Session executed per `ORIN_BRINGUP_SESSION.md` by the desktop Claude Code
session driving the board over SSH. Environment identity:
`orin_env_manifest.md` (+ `orin_readiness_c5.txt`,
`orin_build_provenance_c5.json`, `Dockerfile.orin-noetic`).

Artifact root: `/home/moksh/schurvio-icra27-artifacts/orin-bringup-20260820T131729Z`
(desktop copy; original on-board at `/data/orin-bringup-20260820T131729Z`).
Per-cell `SHA256SUMS` inside each cell directory.

## Acceptance (all six required boxes)

- [x] `orin_env_manifest.md` committed; power mode (ID 0 — 15W) and clock
      policy pinned and recorded (`dc72a2f`)
- [x] Frozen-source build succeeds **unmodified** on ARM; Orin frozen binary
      `ros1_serial_msckf` SHA-256
      `b9f4f0348c56ec7fa97b823bc81533b405b6bf046bf53a7090327c6cdfc37708`
      recorded with full provenance (`09ec41f`); aggregate source hash
      byte-identical to desktop CP0 provenance
- [x] Within-Orin byte-determinism: **2/2 sequences reproduce exactly**
      (`DETERMINISM_GATE.txt`: `state_estimate.txt` and `state_deviation.txt`
      byte-identical across independent replays for KAIST `rotation_fast` and
      EuRoC `MH_05_difficult`)
- [x] Deadline accounting verified at native camera periods on both smoke
      cells (33.3 ms KAIST, 50 ms EuRoC; no 20 Hz figure anywhere in the
      KAIST path)
- [x] Power sampling produces plausible, integrable traces; 60 s idle
      baseline recorded
- [x] Two complete smoke cells in the established cell layout (trajectory/,
      diagnostics/ incl. per-callback `timing_openvins.csv` +
      `resource_usage.txt` + resolved ROS parameters, ros-logs/, console.log,
      `SHA256SUMS`)

**All acceptance boxes pass. C6 (feature-budget sweep) is unblocked.**

## Smoke-cell descriptive numbers (15W mode 0, cores 1–5, container c5-run)

Per-callback total-latency (from `timing_openvins.csv`, `total` column):

| Cell | n | p50 | p95 | p99 | max | native period | misses |
|---|---:|---:|---:|---:|---:|---:|---:|
| kaist-rotation_fast-a1 | 3123 | 45.12 ms | 72.74 ms | 88.80 ms | 113.08 ms | 33.3 ms | 2779 (88.98 %) |
| kaist-rotation_fast-a2 | 3123 | 45.30 ms | 72.63 ms | 89.90 ms | 109.39 ms | 33.3 ms | 2808 (89.91 %) |
| euroc-MH_05-a1 | 1846 | 52.58 ms | 94.20 ms | 114.11 ms | 173.82 ms | 50.0 ms | 1100 (59.59 %) |
| euroc-MH_05-a2 | 1846 | 52.49 ms | 93.12 ms | 112.87 ms | 177.79 ms | 50.0 ms | 1083 (58.67 %) |

These are bring-up trust numbers at the frozen 15W budget, not campaign
results; the miss rates are descriptive facts about the unmodified serial
estimator at this budget.

Power (host tegrastats VDD_IN @ declared 1 Hz, windows sliced by timestamp —
see deviations):

| Trace | Window | Mean | Peak | Energy above idle | Per update |
|---|---:|---:|---:|---:|---:|
| Idle baseline | 60 s | 3038 mW (sd 41) | 3215 mW | — | — |
| kaist-rotation_fast-a1 | 192 s | 3881 mW | 4687 mW | 161.8 J | 51.8 mJ |
| kaist-rotation_fast-a2 | 166 s | 3939 mW | 4887 mW | 149.6 J | 47.9 mJ |
| euroc-MH_05-a1 | 129 s | 4195 mW | 4687 mW | 149.3 J | 80.9 mJ |
| euroc-MH_05-a2 | 124 s | 4072 mW | 4687 mW | 128.2 J | 69.5 mJ |

Thermals stayed ≤ ~48 °C throughout; well inside the 15W envelope; no
throttling observed.

## Cross-architecture check (descriptive only, never asserted)

- KAIST `rotation_fast`: Orin vs desktop CDSC-1R4 S1 scored trajectory —
  all 3123 state timestamps identical; |Δposition| median 2.1 cm,
  p95 5.7 cm, max 8.3 cm (FP accumulation across architectures, expected).
  Apparent |Δquat| = 2.0 is the q ≡ −q sign convention, not divergence.
- EuRoC `MH_05`: Orin emitted 1846 state rows vs desktop 1845, with almost no
  common timestamps — initialization latched one frame differently across
  architectures, so row-wise deltas are not meaningful. Recorded as a
  descriptive cross-architecture initialization difference; not patched,
  per hard rules.

## Deviations

1. Build: two environment-side fixes (git `safe.directory` ownership guard in
   the frozen provenance step → build container runs as uid 1000). No source
   modification. Detailed in `orin_env_manifest.md`.
2. Power capture: `tegrastats` ignores SIGINT delivered through its sudo
   wrapper, so the five capture processes outlived their cells and produced
   overlapping logs. All samples are wall-clock-timestamped, so per-window
   statistics above are sliced to [capture start, start + cell wall-clock
   + 12 s]; raw (overlapping) logs preserved unmodified in `power/`. Future
   drivers should `pkill -TERM/-KILL` tegrastats.
3. Cooling pre-step: fan verified active via `nvfancontrol` (pwm 43 idle) and
   thermal traces; not visually confirmed by a human during the session.

## Non-goals honored

No sweep, no campaign, no contention profiles, no N0 rows, no performance
tuning, no thermal/clock changes between runs. Estimator source read-only on
both machines throughout (tracked tree clean after build, tree hash
re-verified).
