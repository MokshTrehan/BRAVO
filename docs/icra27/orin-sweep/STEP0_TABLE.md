# ORIN-SWEEP-1 — Step-0 table (committed BEFORE any sweep run)

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
