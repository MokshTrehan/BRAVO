# PERTURBATION_PREREG — CDSC perturbation campaign (pre-registered 2026-08-16)

**Purpose:** convert the deterministic n=1 completion differential (CDSC-1R4: U0 TRACKING_LOSS on rotation.bag; room-scale drift on rotation_fast) into a defensible completion-robustness claim, and characterize fork stability on the sequences where accuracy diverged. Registered BEFORE any perturbed run executes. No element below may be edited after the first run starts.

## Systems
- S1 (frozen paper-intent config)
- N0 (in-repo matched nullspace control — the attribution rung; U0 stock rows optional context only)

## Perturbation family (declared, exhaustive)
- **Primary axis — replay start offset:** {−10, −5, 0, +5, +10} frames (±0.33 s at 30 Hz KAIST; ±0.5 s at 20 Hz EuRoC). Everything else frozen.
- **Secondary axis (rotation family + regression sequences only) — RANSAC seed:** {frozen, +1, +2, +3, +4} at offset 0.
- No other perturbation may be added, removed, or reweighted after results are seen.

## Run matrix
| Set | Sequences | Systems | Samples/cell | Runs |
|---|---|---|---|---:|
| KAIST full | all 11 | N0, S1 | 5 (offsets) | 110 |
| KAIST focus | rotation, rotation_fast, square_fast, circle_fast, square_head | N0, S1 | +5 (seeds) | 50 |
| EuRoC forks | MH_05, V1_01, V2_03 | N0, S1 | 5 (offsets) | 30 |
| **Total** | | | | **190** |

## Metrics per cell
Completion (post-initialization passage, existing definition); typed failure class on non-completion; ATE/RPE on completions; per-cell completion count, ATE median + IQR. Deterministic integrity: offset-0 frozen-seed cells must be byte-identical to CDSC-1R4.

## Usability threshold (prospective)
"Usable completion" on KAIST := passage complete AND ATE ≤ 0.5 m (≈14% of the 3.6 m arena major dimension). **Disclosure:** threshold set after CDSC-1R4 (n=1) was seen and before this campaign; it is applied prospectively here and to all subsequent campaigns, and CDSC-1R4 numbers are reported against it descriptively with this timing disclosed.

## Pre-registered claim gates
- **CR-1 (rotation-family completion robustness):** claimable iff S1 completes ≥ 19/20 rotation-family samples AND N0 fails or exceeds the usability threshold on ≥ 6/20, with failures typed.
- **CR-2 (no new S1 fragility):** S1 introduces no failure class absent at offset 0 anywhere in the matrix; any occurrence is reported regardless.
- **CR-3 (regression characterization, descriptive):** square_fast / circle_fast / square_head S1-vs-N0 ATE distributions reported in full — including if S1's regression persists across all perturbations. This set exists so the campaign cannot only confirm.
- Statistics language: counts and distributions; Fisher's exact test permitted on completion counts at n=10/cell; no significance language below that.

## Prohibited
Editing this file after first run; per-sequence tuning; dropping cells; adding perturbations post hoc; reporting rates without their denominators; excluding CR-3 outcomes from the paper.

## Relationship to other lanes
External systems (SchurVINS, ORB-SLAM3, VINS-Fusion) are NOT in this matrix — no differential exists to perturb for a system that fails at ingestion. Their evidence lane is C4a/C4b. Promotion rule: any external system that completes KAIST under its documented protocol enters a follow-up perturbation matrix under these same rules.
