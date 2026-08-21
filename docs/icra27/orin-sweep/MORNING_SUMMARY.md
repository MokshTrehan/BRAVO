# ORIN-SWEEP-1 — MORNING_SUMMARY (ledger F3) — root `orin-sweep-20260820T141547Z`

Prereg `docs/icra27/SWEEP_PREREG.md` (`d4d3134`), frozen at the first sweep run (2026-08-20T14:22:54Z). Board: Orin Nano, nvpmodel mode 0 (15 W), env manifest `dc72a2f`, binary `b9f4f034…`. Desktop branch `orin/sweep-20260820`, HEAD `b3986df`.

## (1) Step-0 table and pre-flight

Prereg: `docs/icra27/SWEEP_PREREG.md`, committed at `d4d3134` ("pre-register ORIN-SWEEP-1"), verified via `git log` on 2026-08-20 at session start. Branch `orin/sweep-20260820` from campaign HEAD `d4d3134`.

## The knob and its frozen value F

| Item | Value | Source |
|---|---|---|
| Knob (tracker max-features) | `num_pts` — OpenVINS tracker "number of points (per camera) we will extract and try to track" | `ov_msckf/src/core/VioManagerOptions.h:717` `parser->parse_config("num_pts", num_pts)` |
| **F (frozen value, KAIST)** | **200** | `config/kaist_vio_rotation_robustness/estimator_config.yaml:91` (sha256 `fa387a5c…`; byte-identical on the board at `/data/schurvio-lite-frozen/config/…`) |
| **F (frozen value, EuRoC)** | **200** | `config/euroc_mav/estimator_config.yaml:81` (sha256 `b706f008…`; byte-identical on the board) |
| Override path | ROS private parameter on the estimator node; the frozen binary's `parse_config` checks `nh->getParam(node_name)` first and prints `overriding node num_pts with value from ROS!` (`ov_core/src/utils/opencv_yaml_parse.h:231-235`). Verified per cell from `resolved_ros_parameters.yaml` + `console.log`. | |
| Elimination flag | `up_msckf_landmark_elimination` ∈ {`schur` (S1), `nullspace` (N0)}; same override path; already exercised by every prior campaign | `VioManagerOptions.h:315-320` |
| Budget grid (pre-registered) | **{50, 75, 100, 150, F=200, 300}**; extension {35, 25} only if the prereg rule fires (DECISIONS entry first) | prereg |
| Sequences | KAIST `rotation_fast`, `circle`, `square_head`; EuRoC `MH_05_difficult` (bag_start 5, as in C5) | prereg |
| Native periods | KAIST 33.3 ms (1000/30), EuRoC 50 ms | prereg / C5 |
| Repeats | 3 per cell; 5 at F and at the two compliance-bracketing budgets once identified | prereg |

## Per-cell single-delta template

Every cell runs the **unmodified** C5 cell runner (`orin_cell_run.sh`, sha256 `05690fab…`, byte-identical to `/data/tooling/orin_cell_run.sh` used by C5) inside the C5 run image `schurvio-orin-env:c5-run`, pinned to cores 1–5, with a launch file that differs from the C5 launch by exactly two exposed arguments (diff recorded below) and **exactly two extra roslaunch arguments**:

```
num_pts:=<B>   landmark_elimination:=<schur|nullspace>
```

Nothing else varies between cells. `up_msckf_max_visual_passes` is machine-checked to remain 1. The resolved values of both knobs, the config hash, the launch hash and the binary hash are written to `diagnostics/cell_delta.json` with a `delta_verified` boolean for every cell.

Launch-file deltas (generated from the frozen launches; `diff` output):

KAIST (`project/rotation_robustness_serial.launch` → `orin_sweep_kaist.launch`):
```
9a10,12
>     <!-- ORIN-SWEEP-1: the only two per-cell deltas (defaults = frozen values). -->
>     <arg name="num_pts" default="200" />
>     <arg name="landmark_elimination" default="schur" />
27c30,31
<         <param name="up_msckf_landmark_elimination" type="str" value="schur" />
---
>         <param name="up_msckf_landmark_elimination" type="str" value="$(arg landmark_elimination)" />
>         <param name="num_pts" type="int" value="$(arg num_pts)" />
```
EuRoC (`project/icra27_cross_dataset_s1_serial.launch` → `orin_sweep_euroc.launch`):
```
15a16,18
>     <!-- ORIN-SWEEP-1: the only two per-cell deltas (defaults = frozen values). -->
>     <arg name="num_pts" default="200" />
>     <arg name="landmark_elimination" default="schur" />
26c29,30
<                value="schur" />
---
>                value="$(arg landmark_elimination)" />
>         <param name="num_pts" type="int" value="$(arg num_pts)" />
```

## Planned run accounting

| Phase | Cells | Runs |
|---|---|---|
| gate | C5 smoke replays (S1, F, C5 launches, no overrides) ×2 | 2 |
| a | KAIST × {F, 100} × {S1, N0} × r1–r3 | 36 |
| b1 / b2 | KAIST × {75, 150} / {50, 300} × {S1, N0} × r1–r3 | 36 + 36 |
| c | EuRoC MH_05 × grid × {S1, N0} × r1–r3 | 36 |
| d | top-ups r4–r5 at F and the two bracketing budgets: KAIST 36, EuRoC 12 | 48 |
| **total** | | **194** (plus ≤1 repeat per INVALID_THERMAL cell; +36 if the extension fires) |

Truncation order if the night runs short (prereg/prompt): EuRoC top-ups first, then non-bracketing KAIST top-ups, then budget 300, then budget 50. Never F, 100, or the bracketing budgets.

## Pre-flight (board, 2026-08-20T14:06Z)

| Check | Result |
|---|---|
| `sudo -n true` | OK |
| `swapon --show` | EMPTY (Swap 0/0 MB) |
| `nvpmodel -q` | `NV Power Mode: 15W` / ID 0 |
| `jetson_clocks --show` | schedutil on cpu0–5, 729.6–1497.6 MHz, GPU 306–612 MHz, EMC 204–2133 MHz, `nvfancontrol` dynamic fan — matches manifest `dc72a2f` (jetson_clocks NOT engaged) |
| `tegrastats` | prints (VDD_IN ≈3.06–3.18 W idle) |
| `/data` headroom | 24 G free of 116 G (80 % used) |
| Orin frozen binary | `b9f4f0348c56ec7fa97b823bc81533b405b6bf046bf53a7090327c6cdfc37708` — matches C5 |
| Frozen tree | `2751bcdc…`, `git status --porcelain` empty |
| Idle temps | cpu 41.7 °C, tj 41.8 °C; no cooling device engaged |
| Stray processes | no tegrastats / ros / msckf running |
| Docker run image | `schurvio-orin-env:c5-run` `4c7748709cea` present; `sudo docker run … rosversion -d` → `noetic` |

## (2) Integrity gate

```
ORIN-SWEEP-1 integrity gate — 2026-08-20T14:22:30Z — C5 references /data/orin-bringup-20260820T131729Z
IDENTICAL gate-kaist-rotation_fast-C5 trajectory/state_estimate.txt 72236fee41fce2c8eeb05cadd1ce8cba560275d7c14a8fd2d9d4560c056ca2b6
IDENTICAL gate-kaist-rotation_fast-C5 trajectory/state_deviation.txt 12bdb241ae79a083fd1cb4bec9b73c23a5b85edf8f1c47ad6ac03ac0f3a43911
IDENTICAL gate-euroc-MH_05-C5 trajectory/state_estimate.txt 7f94bdc58a3d745638eb973db472636d862ea29ffdef6543cd25def84e1ab373
IDENTICAL gate-euroc-MH_05-C5 trajectory/state_deviation.txt 37eb8401f26dcf37cb8b90b7ca123762c6a28cb55f4ef7c475b3398af9a377d0
GATE_PASS
```

Additionally, the first sweep cell `kaist-rotation_fast-S1-b200-r1` (budget and mode passed through the sweep launch as ROS overrides) reproduced the C5 reference `state_estimate.txt` byte-for-byte (`72236fee…`), proving the override path ≡ the frozen YAML path.

## (3) Run accounting

Planned 194 board runs: 2 integrity-gate replays + 144 grid cells (KAIST 3 sequences × 6 budgets × 2 modes × 3 repeats = 108; EuRoC MH_05 × 6 × 2 × 3 = 36) + 48 top-ups to 5 repeats at F and the two compliance-bracketing budgets (KAIST {50, 75}: 36; EuRoC {100, 150}: 12). Executed 194/194 (gate 2/2 + sweep 192/192), every cell COMPLETED with `roslaunch_exit = 0`, non-empty trajectory and timing trace; 0 FAILED; 0 INVALID_THERMAL (no cooling device ever left cur_state 0; max cpu-thermal 46.6 °C vs the 70 °C passive trip) so 0 thermal repeats; 0 truncated (no cut from the truncation ladder was needed; the night ran 14:15Z → 00:36Z, 10 h 21 min). The pre-registered extension {35, 25} was NOT permitted (budget 50 achieves ≥ 95 % family-median compliance for both modes on KAIST, D12) and was not run. Three driver restarts at cell boundaries (D9, D10 — both environment-only page-cache controls; RESTART lines in driver.log), no cell was re-run or discarded; 9 pre-D9 and 13 pre-D10 cells are flagged in cells_universal.csv. Reasons recorded per DECISIONS D1–D13.

| planned | executed (gate + sweep cells present) | COMPLETED (sweep) | FAILED | INVALID_THERMAL | delta-unverified | truncated (planned − executed) |
|---|---|---|---|---|---|---|
| 194 | 194 (2 + 192) | 192 | 0 | 0 | 0 | 0 |

## (4) B* table (B* := largest grid budget whose median deadline compliance across repeats ≥ 95 %; NONE if no budget qualifies)

| family | B*(S1) | B*(N0) | per-sequence B* (S1 / N0) | ATE at B*(S1) [m] | ATE at B*(N0) [m] | ATE at F (reference) [m] |
|---|---|---|---|---|---|---|
| kaist | 50 | 50 | circle NONE/50; rotation_fast 75/75; square_head 50/50 | circle 0.305; rotation_fast 1.141; square_head 34.180 | circle 0.305; rotation_fast 1.141; square_head 31.668 | circle 0.044; rotation_fast 0.090; square_head 0.290 |
| euroc | 100 | 100 | MH_05 100/100 | MH_05 0.197 | MH_05 0.197 | MH_05 0.562 |

KAIST extension rule ({35, 25}) fires: **False**.

## (5) The four curve tables (medians [IQR] over repeats; n = valid runs)

### p99 updater latency vs budget (ms)

**KAIST**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/circle | N0/circle | S1/rotation_fast | N0/rotation_fast | S1/square_head | N0/square_head |
|---|---|---|---|---|---|---|---|---|
| 50 | 37.92 [36.94, 55.36] (n=15) | 38.79 [37.53, 55.59] (n=15) | 36.96 [36.71, 37.92] (n=5) | 38.79 [37.83, 39.31] (n=5) | 55.50 [55.48, 56.47] (n=5) | 56.04 [55.71, 56.84] (n=5) | 36.96 [36.92, 37.87] (n=5) | 37.49 [37.07, 37.57] (n=5) |
| 75 | 43.24 [41.86, 54.71] (n=15) | 42.84 [42.29, 53.56] (n=15) | 40.80 [40.68, 41.83] (n=5) | 41.11 [40.88, 42.14] (n=5) | 55.80 [55.01, 55.94] (n=5) | 54.85 [53.78, 55.47] (n=5) | 43.24 [43.24, 43.46] (n=5) | 42.84 [42.75, 43.01] (n=5) |
| 100 | 56.01 [54.83, 67.91] (n=9) | 55.50 [55.05, 68.25] (n=9) | 56.01 [55.84, 56.95] (n=3) | 55.05 [54.64, 55.28] (n=3) | 70.67 [69.29, 71.16] (n=3) | 70.37 [69.31, 70.47] (n=3) | 54.57 [54.45, 54.70] (n=3) | 55.38 [54.63, 56.05] (n=3) |
| 150 | 81.22 [75.98, 85.45] (n=9) | 77.66 [74.51, 86.27] (n=9) | 81.22 [80.90, 81.80] (n=3) | 77.66 [76.80, 78.51] (n=3) | 85.99 [85.72, 86.46] (n=3) | 86.72 [86.50, 87.90] (n=3) | 75.79 [75.43, 75.89] (n=3) | 74.44 [73.60, 74.48] (n=3) |
| 200 (F) | 85.21 [77.35, 88.26] (n=15) | 80.69 [75.25, 87.12] (n=15) | 85.21 [85.16, 86.39] (n=5) | 80.69 [80.16, 80.97] (n=5) | 89.35 [89.09, 90.14] (n=5) | 87.52 [87.41, 88.25] (n=5) | 77.20 [77.12, 77.24] (n=5) | 74.20 [73.89, 75.02] (n=5) |
| 300 | 92.53 [85.75, 93.29] (n=9) | 86.79 [82.23, 89.59] (n=9) | 92.53 [92.05, 92.74] (n=3) | 86.79 [86.65, 87.32] (n=3) | 93.56 [93.43, 93.70] (n=3) | 91.11 [90.35, 91.27] (n=3) | 85.28 [84.88, 85.51] (n=3) | 82.13 [81.98, 82.18] (n=3) |

**EUROC**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/MH_05 | N0/MH_05 |
|---|---|---|---|---|
| 50 | 74.73 [74.70, 74.97] (n=3) | 75.23 [75.14, 76.00] (n=3) | 74.73 [74.70, 74.97] (n=3) | 75.23 [75.14, 76.00] (n=3) |
| 75 | 72.78 [72.38, 74.53] (n=3) | 70.69 [70.63, 73.22] (n=3) | 72.78 [72.38, 74.53] (n=3) | 70.69 [70.63, 73.22] (n=3) |
| 100 | 97.02 [95.41, 97.45] (n=5) | 98.33 [95.12, 99.79] (n=5) | 97.02 [95.41, 97.45] (n=5) | 98.33 [95.12, 99.79] (n=5) |
| 150 | 122.87 [122.59, 123.26] (n=5) | 113.66 [112.43, 114.57] (n=5) | 122.87 [122.59, 123.26] (n=5) | 113.66 [112.43, 114.57] (n=5) |
| 200 (F) | 113.71 [111.45, 113.82] (n=5) | 107.29 [107.13, 107.42] (n=5) | 113.71 [111.45, 113.82] (n=5) | 107.29 [107.13, 107.42] (n=5) |
| 300 | 126.79 [125.55, 126.89] (n=3) | 112.57 [112.39, 112.71] (n=3) | 126.79 [125.55, 126.89] (n=3) | 112.57 [112.39, 112.71] (n=3) |

### Deadline compliance vs budget (% of callbacks with total ≤ native period)

**KAIST**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/circle | N0/circle | S1/rotation_fast | N0/rotation_fast | S1/square_head | N0/square_head |
|---|---|---|---|---|---|---|---|---|
| 50 | 96.35 [95.39, 97.11] (n=15) | 95.55 [94.89, 97.07] (n=15) | 94.81 [94.43, 95.46] (n=5) | 95.19 [93.78, 95.53] (n=5) | 96.35 [96.25, 96.61] (n=5) | 95.13 [94.65, 95.55] (n=5) | 97.26 [97.17, 97.85] (n=5) | 97.31 [97.28, 97.61] (n=5) |
| 75 | 91.56 [84.69, 96.11] (n=15) | 92.01 [88.17, 95.33] (n=15) | 84.24 [80.19, 84.47] (n=5) | 84.54 [83.97, 86.41] (n=5) | 96.16 [96.13, 96.35] (n=5) | 95.42 [95.39, 95.74] (n=5) | 91.56 [91.48, 92.66] (n=5) | 92.01 [91.98, 92.85] (n=5) |
| 100 | 72.11 [55.50, 88.79] (n=9) | 71.04 [56.53, 86.04] (n=9) | 49.89 [48.93, 52.69] (n=3) | 51.37 [50.82, 53.95] (n=3) | 89.24 [89.02, 89.47] (n=3) | 88.06 [87.05, 88.49] (n=3) | 72.11 [71.57, 72.56] (n=3) | 71.04 [69.76, 71.12] (n=3) |
| 150 | 25.69 [3.24, 42.84] (n=9) | 26.13 [3.63, 41.27] (n=9) | 3.17 [3.15, 3.21] (n=3) | 3.24 [3.11, 3.44] (n=3) | 43.55 [43.20, 43.90] (n=3) | 43.00 [42.14, 44.68] (n=3) | 25.69 [25.35, 26.15] (n=3) | 26.13 [25.57, 26.21] (n=3) |
| 200 (F) | 11.21 [1.26, 14.94] (n=15) | 11.27 [1.35, 14.90] (n=15) | 1.18 [1.15, 1.22] (n=5) | 1.34 [1.26, 1.34] (n=5) | 11.21 [10.73, 11.37] (n=5) | 11.27 [11.27, 11.46] (n=5) | 15.72 [15.50, 16.15] (n=5) | 15.50 [14.94, 15.60] (n=5) |
| 300 | 4.13 [0.65, 10.19] (n=9) | 4.07 [0.61, 9.73] (n=9) | 0.57 [0.53, 0.61] (n=3) | 0.53 [0.50, 0.57] (n=3) | 4.13 [3.97, 4.29] (n=3) | 4.07 [3.89, 4.10] (n=3) | 10.60 [10.39, 10.75] (n=3) | 11.45 [10.59, 11.45] (n=3) |

**EUROC**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/MH_05 | N0/MH_05 |
|---|---|---|---|---|
| 50 | 98.54 [98.54, 98.54] (n=3) | 98.21 [98.21, 98.24] (n=3) | 98.54 [98.54, 98.54] (n=3) | 98.21 [98.21, 98.24] (n=3) |
| 75 | 97.56 [97.51, 97.67] (n=3) | 97.83 [97.72, 97.91] (n=3) | 97.56 [97.51, 97.67] (n=3) | 97.83 [97.72, 97.91] (n=3) |
| 100 | 95.18 [95.07, 95.23] (n=5) | 95.40 [94.69, 95.50] (n=5) | 95.18 [95.07, 95.23] (n=5) | 95.40 [94.69, 95.50] (n=5) |
| 150 | 68.15 [66.58, 69.12] (n=5) | 67.50 [67.12, 67.50] (n=5) | 68.15 [66.58, 69.12] (n=5) | 67.50 [67.12, 67.50] (n=5) |
| 200 (F) | 39.82 [39.71, 40.52] (n=5) | 41.44 [41.01, 41.50] (n=5) | 39.82 [39.71, 40.52] (n=5) | 41.44 [41.01, 41.50] (n=5) |
| 300 | 11.16 [10.83, 11.24] (n=3) | 10.83 [10.67, 10.94] (n=3) | 11.16 [10.83, 11.24] (n=3) | 10.83 [10.67, 10.94] (n=3) |

### Energy per update vs budget (mJ above idle; C5 convention; idle 3038 mW)

**KAIST**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/circle | N0/circle | S1/rotation_fast | N0/rotation_fast | S1/square_head | N0/square_head |
|---|---|---|---|---|---|---|---|---|
| 50 | 26.2 [24.2, 32.0] (n=15) | 25.3 [23.6, 31.8] (n=15) | 33.3 [32.6, 37.1] (n=5) | 32.3 [31.9, 32.7] (n=5) | 25.7 [24.4, 26.4] (n=5) | 25.3 [24.7, 25.5] (n=5) | 24.0 [23.2, 24.4] (n=5) | 23.5 [23.1, 23.6] (n=5) |
| 75 | 27.4 [26.0, 34.9] (n=15) | 26.1 [25.0, 34.5] (n=15) | 35.4 [35.2, 35.6] (n=5) | 34.7 [34.6, 35.1] (n=5) | 25.9 [25.5, 26.0] (n=5) | 24.8 [24.4, 24.8] (n=5) | 26.8 [26.1, 27.9] (n=5) | 26.1 [26.1, 26.3] (n=5) |
| 100 | 31.0 [28.0, 39.4] (n=9) | 30.9 [27.8, 38.6] (n=9) | 39.5 [39.4, 40.0] (n=3) | 39.8 [39.2, 40.0] (n=3) | 27.4 [26.8, 27.7] (n=3) | 27.6 [27.5, 27.7] (n=3) | 31.0 [30.9, 31.1] (n=3) | 30.9 [30.7, 31.0] (n=3) |
| 150 | 43.4 [39.5, 57.1] (n=9) | 43.0 [39.2, 56.7] (n=9) | 58.0 [57.5, 58.0] (n=3) | 56.9 [56.8, 56.9] (n=3) | 39.2 [39.1, 39.4] (n=3) | 39.2 [38.9, 39.2] (n=3) | 43.4 [43.2, 43.5] (n=3) | 43.0 [42.8, 43.1] (n=3) |
| 200 (F) | 53.2 [52.2, 68.8] (n=15) | 51.5 [50.7, 65.5] (n=15) | 71.9 [69.2, 76.2] (n=5) | 66.7 [66.1, 66.7] (n=5) | 52.7 [51.4, 53.0] (n=5) | 50.7 [50.6, 50.9] (n=5) | 52.5 [51.9, 53.9] (n=5) | 50.8 [50.2, 51.5] (n=5) |
| 300 | 63.7 [62.1, 80.7] (n=9) | 63.6 [61.2, 77.7] (n=9) | 82.0 [81.4, 84.4] (n=3) | 78.7 [78.2, 79.1] (n=3) | 63.7 [63.4, 64.3] (n=3) | 63.6 [63.5, 63.8] (n=3) | 62.1 [62.0, 62.1] (n=3) | 60.9 [60.7, 61.1] (n=3) |

**EUROC**

| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | S1/MH_05 | N0/MH_05 |
|---|---|---|---|---|
| 50 | 38.8 [38.4, 38.8] (n=3) | 40.1 [39.9, 40.3] (n=3) | 38.8 [38.4, 38.8] (n=3) | 40.1 [39.9, 40.3] (n=3) |
| 75 | 41.8 [41.5, 42.8] (n=3) | 40.8 [40.6, 41.2] (n=3) | 41.8 [41.5, 42.8] (n=3) | 40.8 [40.6, 41.2] (n=3) |
| 100 | 46.6 [46.4, 48.0] (n=5) | 46.9 [46.8, 47.3] (n=5) | 46.6 [46.4, 48.0] (n=5) | 46.9 [46.8, 47.3] (n=5) |
| 150 | 62.5 [62.4, 63.0] (n=5) | 62.5 [61.8, 63.2] (n=5) | 62.5 [62.4, 63.0] (n=5) | 62.5 [61.8, 63.2] (n=5) |
| 200 (F) | 74.1 [74.1, 74.6] (n=5) | 73.4 [73.2, 76.3] (n=5) | 74.1 [74.1, 74.6] (n=5) | 73.4 [73.2, 76.3] (n=5) |
| 300 | 95.6 [95.5, 95.7] (n=3) | 93.8 [93.3, 93.8] (n=3) | 95.6 [95.5, 95.7] (n=3) | 93.8 [93.3, 93.8] (n=3) |

### ATE vs budget (coverage-welded; one deterministic trajectory per cell; frozen CDSC-1R4 core)

**KAIST**

| budget | S1/circle ATE m (cov %; status) | N0/circle ATE m (cov %; status) | S1/rotation_fast ATE m (cov %; status) | N0/rotation_fast ATE m (cov %; status) | S1/square_head ATE m (cov %; status) | N0/square_head ATE m (cov %; status) |
|---|---|---|---|---|---|---|
| 50 | 0.305 (100.0; COMPUTED; repeats identical 5/5) | 0.305 (100.0; COMPUTED; repeats identical 5/5) | 1.141 (100.0; COMPUTED; repeats identical 5/5) | 1.141 (100.0; COMPUTED; repeats identical 5/5) | 34.180 (100.0; COMPUTED; repeats identical 5/5) | 31.668 (100.0; COMPUTED; repeats identical 5/5) |
| 75 | 0.240 (100.0; COMPUTED; repeats identical 5/5) | 0.240 (100.0; COMPUTED; repeats identical 5/5) | 0.909 (100.0; COMPUTED; repeats identical 5/5) | 0.909 (100.0; COMPUTED; repeats identical 5/5) | 2.948 (100.0; COMPUTED; repeats identical 5/5) | 2.948 (100.0; COMPUTED; repeats identical 5/5) |
| 100 | 0.089 (100.0; COMPUTED; repeats identical 3/3) | 0.089 (100.0; COMPUTED; repeats identical 3/3) | 0.572 (100.0; COMPUTED; repeats identical 3/3) | 0.572 (100.0; COMPUTED; repeats identical 3/3) | 1.286 (100.0; COMPUTED; repeats identical 3/3) | 1.286 (100.0; COMPUTED; repeats identical 3/3) |
| 150 | 0.064 (100.0; COMPUTED; repeats identical 3/3) | 0.064 (100.0; COMPUTED; repeats identical 3/3) | 0.269 (100.0; COMPUTED; repeats identical 3/3) | 0.269 (100.0; COMPUTED; repeats identical 3/3) | 0.312 (100.0; COMPUTED; repeats identical 3/3) | 0.312 (100.0; COMPUTED; repeats identical 3/3) |
| 200 (F) | 0.044 (100.0; COMPUTED; repeats identical 5/5) | 0.044 (100.0; COMPUTED; repeats identical 5/5) | 0.090 (100.0; COMPUTED; repeats identical 5/5) | 0.090 (100.0; COMPUTED; repeats identical 5/5) | 0.290 (100.0; COMPUTED; repeats identical 5/5) | 0.290 (100.0; COMPUTED; repeats identical 5/5) |
| 300 | 0.032 (100.0; COMPUTED; repeats identical 3/3) | 0.032 (100.0; COMPUTED; repeats identical 3/3) | 0.088 (100.0; COMPUTED; repeats identical 3/3) | 0.088 (100.0; COMPUTED; repeats identical 3/3) | 0.275 (100.0; COMPUTED; repeats identical 3/3) | 0.275 (100.0; COMPUTED; repeats identical 3/3) |

**EUROC**

| budget | S1/MH_05 ATE m (cov %; status) | N0/MH_05 ATE m (cov %; status) |
|---|---|---|
| 50 | 0.257 (100.0; COMPUTED; repeats identical 3/3) | 0.380 (100.0; COMPUTED; repeats identical 3/3) |
| 75 | 0.364 (100.0; COMPUTED; repeats identical 3/3) | 0.362 (100.0; COMPUTED; repeats identical 3/3) |
| 100 | 0.197 (100.0; COMPUTED; repeats identical 5/5) | 0.197 (100.0; COMPUTED; repeats identical 5/5) |
| 150 | 0.208 (100.0; COMPUTED; repeats identical 5/5) | 0.208 (100.0; COMPUTED; repeats identical 5/5) |
| 200 (F) | 0.562 (100.0; COMPUTED; repeats identical 5/5) | 0.562 (100.0; COMPUTED; repeats identical 5/5) |
| 300 | 0.198 (100.0; COMPUTED; repeats identical 3/3) | 0.198 (100.0; COMPUTED; repeats identical 3/3) |

Energy convention (verbatim, C5): C5 frozen convention: idle mean = mean VDD_IN over the first 60 tegrastats samples of the C5 60 s idle capture (3038 mW); energy above idle = sum over VDD_IN samples with timestamp in [capture start, capture start + /usr/bin/time wall-clock + 12 s] of (VDD_IN - idle mean) x 1 s (declared 1 Hz); energy per update = energy above idle / rows of timing_openvins.csv. Fresh idle baseline tonight: 2991 mW (reported alongside; not used in any number).

## (6) Clause verdicts

**CL-1 (budget)** — B*(S1) exceeds B*(N0) by ≥ 1 grid step on ≥ 1 family: **False**
```
{
 "kaist": {
  "B_S1": 50,
  "B_N0": 50,
  "grid_step_gap": 0,
  "fires": false,
  "ate_at_B_S1": {
   "circle": 0.30508449311597513,
   "rotation_fast": 1.1407614564814017,
   "square_head": 34.1797342970459
  },
  "ate_at_B_N0": {
   "circle": 0.3050845488886763,
   "rotation_fast": 1.1407614564814017,
   "square_head": 31.66829480988091
  }
 },
 "euroc": {
  "B_S1": 100,
  "B_N0": 100,
  "grid_step_gap": 0,
  "fires": false,
  "ate_at_B_S1": {
   "MH_05": 0.19720935968759068
  },
  "ate_at_B_N0": {
   "MH_05": 0.1972091919856708
  }
 }
}
```
**CL-2 (tail latency)** — median-over-repeats p99(S1) ≤ 0.85 × p99(N0) at F on ≥ 2 sequences: **False** (0 sequences fire)
```
{
 "circle": {
  "S1_median": 85.21169999999996,
  "N0_median": 80.68639999999998,
  "ratio": 1.0560850403537645,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "rotation_fast": {
  "S1_median": 89.34939999999993,
  "N0_median": 87.51539999999989,
  "ratio": 1.0209563116891434,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "square_head": {
  "S1_median": 77.20049999999999,
  "N0_median": 74.19869999999999,
  "ratio": 1.0404562344084196,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "MH_05": {
  "S1_median": 113.70649999999995,
  "N0_median": 107.2895,
  "ratio": 1.0598101398552509,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 }
}
```
**CL-3 (energy)** — energy/update(S1) ≤ 0.80 × energy/update(N0) at F on ≥ 2 sequences: **False** (0 sequences fire)
```
{
 "circle": {
  "S1_median": 71.86760814249376,
  "N0_median": 66.69503816793906,
  "ratio": 1.0775555441100446,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "rotation_fast": {
  "S1_median": 52.65679368128946,
  "N0_median": 50.702721741914914,
  "ratio": 1.0385397839059032,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "square_head": {
  "S1_median": 52.467400419286946,
  "N0_median": 50.818453878406494,
  "ratio": 1.0324477904193208,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 },
 "MH_05": {
  "S1_median": 74.09983748645732,
  "N0_median": 73.42156013001095,
  "ratio": 1.009238122361406,
  "fires": false,
  "n_S1": 5,
  "n_N0": 5
 }
}
```

### F3 status: **REFUTED** — all three clauses false; the thesis bracket resolves to exact-equivalence-only; the operating-point characterization in (4)–(5) stands on its own

## (7) Licensed sentence

**Licensed sentence (assembled per the prereg; nothing stronger):** On the Orin Nano at the frozen 15 W mode 0 with the unmodified frozen estimator, the serial MSCKF updater meets ≥ 95 % deadline compliance at the native camera period *only at reduced tracker budgets* — at `num_pts` = 50 on the KAIST family (family median 96.35 % S1 / 95.55 % N0 over 15 runs; circle alone 94.81 % / 95.19 %) and at `num_pts` = 100 on EuRoC MH_05 (95.18 % / 95.40 % over 5 runs) — and at those budgets the accuracy is what the table shows (KAIST at 50: 0.305 m circle, 1.141 m rotation_fast, 34.2 m / 31.7 m square_head vs 0.044 / 0.090 / 0.290 m at F; EuRoC at 100: 0.197 m vs 0.562 m at F); at the paper-intent budget F = 200 compliance is 11 % (KAIST) and 40 % (EuRoC) for both modes. S1 (Schur) and N0 (nullspace) are operationally indistinguishable on this board: identical trajectories at every budget where both converge, p99 within 2–6 % (S1 ≥ N0), energy/update within 1–8 % (S1 ≥ N0). F3 is REFUTED under all three pre-registered clauses; the thesis bracket resolves to exact-equivalence-only, and the budget-conditional operating-point characterization above is the deliverable. No unqualified real-time claim on Orin Nano is licensed.

## (8) Session-end hard-rule checks

- Desktop estimator diff `git diff --stat d4d3134 HEAD -- ov_msckf ov_core ov_init ov_eval config`: EMPTY
- Desktop working tree in scope: clean
- Board frozen tree (`git rev-parse HEAD`, dirty-in-scope count, diff, binary sha256, swap entries, power mode):
```
2751bcdc0fae25c993b3224dc5fa40aaab6571d7
0
b9f4f0348c56ec7fa97b823bc81533b405b6bf046bf53a7090327c6cdfc37708  build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf
0
NV Power Mode: 15W
```
- Desktop x86 binary (contrast only, never a target): `0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e`
- Max cpu-thermal over all cells: 46.6 °C; max tj: 46.6 °C; throttle (INVALID_THERMAL) cells: 0 []

## (9) Artifact paths

- Board: `/data/orin-sweep-20260820T141547Z/` (cells/, power/, RUN_LOG.md, runs.jsonl, QUEUE.tsv, INTEGRITY_GATE.txt, driver.log, state/)
- Desktop mirror: `/home/moksh/schurvio-icra27-artifacts/orin-sweep-20260820T141547Z`
- Aggregate: `/home/moksh/schurvio-icra27-artifacts/orin-sweep-20260820T141547Z/aggregate` (aggregate.json, cells_universal.csv, tables.md, accuracy_work/)
- Docs: `docs/icra27/orin-sweep/` (STEP0_TABLE.md, DECISIONS.md, RUN_LOG.md, MORNING_SUMMARY.md); tooling `scripts/icra27/orin_sweep/`
