# ABLATE-REC-1 Step 0 — recovery-event distribution in PERTURB-1 (facts only, no new runs)

Source: `/home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z/driver/cells.jsonl` (192 driver records; 100 CLOSED cells = 98 matrix + 2 integrity; 92 NOT_RUNNABLE) and per-cell `perturbation_recovery_descriptive` summaries (KAIST runner; parsed from `[LONG-GAP-RECOVERY]:` console lines). Written 2026-08-17 before any ABLATE-REC-1 run.

## Summary

- Sequences on which the C2 long-gap recovery subsystem fired (activations > 0) in any executed PERTURB-1 cell: **`rotation/rotation.bag`**.
  - `rotation/rotation.bag` / N0: 4/4 executed cells fired; activations=[1] commits=[1] failures=[0] per cell.
  - `rotation/rotation.bag` / S1: 5/5 executed cells fired; activations=[1] commits=[1] failures=[0] per cell.
- KAIST sequences with recovery enabled (contract_validated=1) but zero activations in every executed cell: `circle/circle.bag`, `circle/circle_fast.bag`, `circle/circle_head.bag`, `infinite/infinite.bag`, `infinite/infinite_fast.bag`, `infinite/infinite_head.bag`, `rotation/rotation_fast.bag`, `square/square.bag`, `square/square_fast.bag`, `square/square_head.bag`.
- EuRoC sequences (`MH_05_difficult`, `V1_01_easy`, `V2_03_difficult`): the frozen S1/N0 EuRoC configuration `config/euroc_mav/estimator_config.yaml` contains no `long_gap_recovery_enabled` key (executable default `false`, `ov_msckf/src/core/VioManagerOptions.h:113`) and `project/icra27_cross_dataset_s1_serial.launch` deliberately omits it; the console logs contain zero `[LONG-GAP-RECOVERY]` lines. Recovery therefore never fired there and could not have.
- **Step-0 rule 2 outcome:** recovery fired only on `rotation/rotation.bag`, which is inside the pre-registered rotation family {rotation, rotation_fast}. **No sequence-set extension is required; the prereg is not edited.**
- **P2 reference set (cells whose PERTURB-1 recON counterpart recorded zero recovery events):** every ABLATE-REC-1 recOFF cell except the six on `rotation/rotation.bag` (S1 offsets {0,+5,+10}, N0 offsets {0,+5,+10}). Concretely: `rotation/rotation_fast.bag`, `square/square_fast.bag`, `circle/circle_fast.bag`, `square/square_head.bag` × {S1,N0} × {0,+5,+10} (24 cells) and `MH_05_difficult`, `V2_03_difficult` × {S1,N0} × {0,+5,+10} (12 cells) = 36 cells. P1 crux set = the 6 `rotation/rotation.bag` recOFF cells + the 6 `rotation/rotation_fast.bag` recOFF cells (12 cells, of which only rotation.bag ever fired).

## Per-cell table (all 100 CLOSED PERTURB-1 cells)

| sequence | system | set | axis | offset | seed | activations | commits | failures | trigger (epoch, ts, last_state_ts, gap_s) | commit (epoch, ts) | status | passage complete | run_id |
|---|---|---|---|---:|---|---:|---:|---:|---|---|---|---|---|
| `MH_05_difficult` | N0 | euroc-forks | offset | -10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-n0-offset-offm10-a1` |
| `MH_05_difficult` | N0 | euroc-forks | offset | -5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-n0-offset-offm5-a1` |
| `MH_05_difficult` | N0 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-n0-offset-off0-a1` |
| `MH_05_difficult` | N0 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-n0-offset-offp5-a1` |
| `MH_05_difficult` | N0 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-n0-offset-offp10-a1` |
| `MH_05_difficult` | S1 | euroc-forks | offset | -10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-s1-offset-offm10-a1` |
| `MH_05_difficult` | S1 | euroc-forks | offset | -5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-s1-offset-offm5-a1` |
| `MH_05_difficult` | S1 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-s1-offset-off0-a1` |
| `MH_05_difficult` | S1 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-s1-offset-offp5-a1` |
| `MH_05_difficult` | S1 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-05-MH_05_difficult-s1-offset-offp10-a1` |
| `MH_05_difficult` | S1 | integrity | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-integrity-05-MH_05_difficult-s1-offset-off0-a1` |
| `V1_01_easy` | N0 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-n0-offset-off0-a1` |
| `V1_01_easy` | N0 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-n0-offset-offp5-a1` |
| `V1_01_easy` | N0 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-n0-offset-offp10-a1` |
| `V1_01_easy` | S1 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-s1-offset-off0-a1` |
| `V1_01_easy` | S1 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-s1-offset-offp5-a1` |
| `V1_01_easy` | S1 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-06-V1_01_easy-s1-offset-offp10-a1` |
| `V2_03_difficult` | N0 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-n0-offset-off0-a1` |
| `V2_03_difficult` | N0 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-n0-offset-offp5-a1` |
| `V2_03_difficult` | N0 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-n0-offset-offp10-a1` |
| `V2_03_difficult` | S1 | euroc-forks | offset | +0 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-s1-offset-off0-a1` |
| `V2_03_difficult` | S1 | euroc-forks | offset | +5 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-s1-offset-offp5-a1` |
| `V2_03_difficult` | S1 | euroc-forks | offset | +10 | frozen | n/a | n/a | n/a | — | — | COMPLETED | True | `perturb1-11-V2_03_difficult-s1-offset-offp10-a1` |
| `circle/circle.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-n0-offset-off0-a1` |
| `circle/circle.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-n0-offset-offp5-a1` |
| `circle/circle.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-n0-offset-offp10-a1` |
| `circle/circle.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-s1-offset-off0-a1` |
| `circle/circle.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-s1-offset-offp5-a1` |
| `circle/circle.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-22-circle_circle.bag-s1-offset-offp10-a1` |
| `circle/circle_fast.bag` | N0 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-n0-offset-off0-a1` |
| `circle/circle_fast.bag` | N0 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-n0-offset-offp5-a1` |
| `circle/circle_fast.bag` | N0 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-n0-offset-offp10-a1` |
| `circle/circle_fast.bag` | N0 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-n0-seed-seedfrozen-a1` |
| `circle/circle_fast.bag` | S1 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-s1-offset-off0-a1` |
| `circle/circle_fast.bag` | S1 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-s1-offset-offp5-a1` |
| `circle/circle_fast.bag` | S1 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-s1-offset-offp10-a1` |
| `circle/circle_fast.bag` | S1 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-24-circle_circle_fast.bag-s1-seed-seedfrozen-a1` |
| `circle/circle_head.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-n0-offset-off0-a1` |
| `circle/circle_head.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-n0-offset-offp5-a1` |
| `circle/circle_head.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-n0-offset-offp10-a1` |
| `circle/circle_head.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-s1-offset-off0-a1` |
| `circle/circle_head.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-s1-offset-offp5-a1` |
| `circle/circle_head.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-18-circle_circle_head.bag-s1-offset-offp10-a1` |
| `infinite/infinite.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-n0-offset-off0-a1` |
| `infinite/infinite.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-n0-offset-offp5-a1` |
| `infinite/infinite.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-n0-offset-offp10-a1` |
| `infinite/infinite.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-s1-offset-off0-a1` |
| `infinite/infinite.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-s1-offset-offp5-a1` |
| `infinite/infinite.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-20-infinite_infinite.bag-s1-offset-offp10-a1` |
| `infinite/infinite_fast.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-n0-offset-off0-a1` |
| `infinite/infinite_fast.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-n0-offset-offp5-a1` |
| `infinite/infinite_fast.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-n0-offset-offp10-a1` |
| `infinite/infinite_fast.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-s1-offset-off0-a1` |
| `infinite/infinite_fast.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-s1-offset-offp5-a1` |
| `infinite/infinite_fast.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-15-infinite_infinite_fast.bag-s1-offset-offp10-a1` |
| `infinite/infinite_head.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-n0-offset-off0-a1` |
| `infinite/infinite_head.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-n0-offset-offp5-a1` |
| `infinite/infinite_head.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-n0-offset-offp10-a1` |
| `infinite/infinite_head.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-s1-offset-off0-a1` |
| `infinite/infinite_head.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-s1-offset-offp5-a1` |
| `infinite/infinite_head.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-25-infinite_infinite_head.bag-s1-offset-offp10-a1` |
| `rotation/rotation.bag` | N0 | kaist-focus | offset | +0 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-n0-offset-off0-a1` |
| `rotation/rotation.bag` | N0 | kaist-focus | offset | +5 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-n0-offset-offp5-a1` |
| `rotation/rotation.bag` | N0 | kaist-focus | offset | +10 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-n0-offset-offp10-a1` |
| `rotation/rotation.bag` | N0 | kaist-focus | seed | +0 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-n0-seed-seedfrozen-a1` |
| `rotation/rotation.bag` | S1 | integrity | offset | +0 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-integrity-19-rotation_rotation.bag-s1-offset-off0-a1` |
| `rotation/rotation.bag` | S1 | kaist-focus | offset | +0 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-s1-offset-off0-a1` |
| `rotation/rotation.bag` | S1 | kaist-focus | offset | +5 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-s1-offset-offp5-a1` |
| `rotation/rotation.bag` | S1 | kaist-focus | offset | +10 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-s1-offset-offp10-a1` |
| `rotation/rotation.bag` | S1 | kaist-focus | seed | +0 | frozen | 1 | 1 | 0 | (0, 1599131279.832099, 1599131266.390102, 13.441997) | (1, 1599131279.897871494293213) | COMPLETED | True | `perturb1-19-rotation_rotation.bag-s1-seed-seedfrozen-a1` |
| `rotation/rotation_fast.bag` | N0 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-n0-offset-off0-a1` |
| `rotation/rotation_fast.bag` | N0 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-n0-offset-offp5-a1` |
| `rotation/rotation_fast.bag` | N0 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-n0-offset-offp10-a1` |
| `rotation/rotation_fast.bag` | N0 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-n0-seed-seedfrozen-a1` |
| `rotation/rotation_fast.bag` | S1 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-s1-offset-off0-a1` |
| `rotation/rotation_fast.bag` | S1 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-s1-offset-offp5-a1` |
| `rotation/rotation_fast.bag` | S1 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-s1-offset-offp10-a1` |
| `rotation/rotation_fast.bag` | S1 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-23-rotation_rotation_fast.bag-s1-seed-seedfrozen-a1` |
| `square/square.bag` | N0 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-n0-offset-off0-a1` |
| `square/square.bag` | N0 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-n0-offset-offp5-a1` |
| `square/square.bag` | N0 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-n0-offset-offp10-a1` |
| `square/square.bag` | S1 | kaist-full-remaining | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-s1-offset-off0-a1` |
| `square/square.bag` | S1 | kaist-full-remaining | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-s1-offset-offp5-a1` |
| `square/square.bag` | S1 | kaist-full-remaining | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-17-square_square.bag-s1-offset-offp10-a1` |
| `square/square_fast.bag` | N0 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-n0-offset-off0-a1` |
| `square/square_fast.bag` | N0 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-n0-offset-offp5-a1` |
| `square/square_fast.bag` | N0 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-n0-offset-offp10-a1` |
| `square/square_fast.bag` | N0 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-n0-seed-seedfrozen-a1` |
| `square/square_fast.bag` | S1 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-s1-offset-off0-a1` |
| `square/square_fast.bag` | S1 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-s1-offset-offp5-a1` |
| `square/square_fast.bag` | S1 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-s1-offset-offp10-a1` |
| `square/square_fast.bag` | S1 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-16-square_square_fast.bag-s1-seed-seedfrozen-a1` |
| `square/square_head.bag` | N0 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-n0-offset-off0-a1` |
| `square/square_head.bag` | N0 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-n0-offset-offp5-a1` |
| `square/square_head.bag` | N0 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-n0-offset-offp10-a1` |
| `square/square_head.bag` | N0 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-n0-seed-seedfrozen-a1` |
| `square/square_head.bag` | S1 | kaist-focus | offset | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-s1-offset-off0-a1` |
| `square/square_head.bag` | S1 | kaist-focus | offset | +5 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-s1-offset-offp5-a1` |
| `square/square_head.bag` | S1 | kaist-focus | offset | +10 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-s1-offset-offp10-a1` |
| `square/square_head.bag` | S1 | kaist-focus | seed | +0 | frozen | 0 | 0 | 0 | — | — | COMPLETED | True | `perturb1-21-square_square_head.bag-s1-seed-seedfrozen-a1` |

EuRoC rows show n/a: the generic runner records no recovery descriptive block; see the EuRoC note above (default-off, no event lines).

## Event-count keys observed on rotation/rotation.bag (identical on all 9 executed S1/N0 cells)

```
{
 "accepted_pose": 3,
 "attempt": 3,
 "consensus_pass": 1,
 "contract_validated": 1,
 "first_resumed_covariance": 1,
 "relocalization_commit": 1,
 "summary": 1,
 "trigger": 1,
 "warmup_complete": 1
}
```
