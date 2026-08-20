# MORNING_SUMMARY — lanes/c4b-s2 (executed 2026-08-20, one day after registration)

## 1. Lane 1 freshness verdict
REPRODUCED 12/12 (no STOP): EuRoC sanities COMPLETED (orb\_retry1 & schur\_valid variants);
all KAIST nominal + slowed-rate diagnostics reproduce recorded classes exactly
(ORB exit 139; SchurVINS premature exit 0). Env fixes documented in RUN\_LOG
(ORB Thirdparty LD\_LIBRARY\_PATH — the A0 lineage's own first attempt shows the
same class; harness dir/cwd contracts).

## 2. Normalization + mono-inertial outcomes (four-target subset)
(ii) SchurVINS header/sync normalization: NO CHANGE on any target — failure
classes and timings identical between timestamp-preserving and normalized
conditions (infinite+square: frame\_handler CHECK(0) @~11s; infinite\_head +
square\_fast: schur\_vins.cpp:169 CHECK(\_dt) @11.7s/31.3s). Adapter proof:
stereo stamps already exactly equal (max skew 0.0; 0 drops; 4821 pairs).
**NO ESCALATE** — completion narrative unchanged, strengthened (failures are
algorithmic, not input-header).
Invalid runner attempts (runs 1–3, startup-env CHECKs) preserved in
lane1-attempts-invalid-run*/ and logged.
(iii) ORB-SLAM3 mono-inertial: NOT\_POSSIBLE\_UNDER\_PINNED\_BUILD — no
mono-inertial target exists in the pinned ROS-compat build configuration;
creating one requires modifying the frozen build (prohibited). Evidence:
lane1-mono-iii-record.txt.

## 3. S2 single-delta proof + integrity gate
S2 = launch-level single delta up\_msckf\_max\_visual\_passes 1→2 (ROS-param
precedence verified in ov\_core parser; two-pass legal only with schur, per
source). Machine-verified per cell via resolved\_ros\_parameters.yaml.
Integrity gate: fresh S1 rotation\_fast byte-identical to CDSC-1R4 scored
reference — PASS.

## 4. Paired-delta table (S2 vs frozen S1 aggregates; coverage = matched/S1-common)
| sequence | dataset | S1 ATE (m) | S2 ATE (m) | Δ | coverage |
|---|---|---:|---:|---:|---:|
| 01-MH_01_easy | euroc_mav | 0.0908 | 0.0781 | -14.02% | 1.0 |
| 02-MH_02_easy | euroc_mav | 0.1244 | 0.1274 | +2.42% | 1.0 |
| 03-MH_03_medium | euroc_mav | 0.1376 | 0.1138 | -17.33% | 1.0 |
| 04-MH_04_difficult | euroc_mav | 0.1661 | 0.1858 | +11.82% | 1.0 |
| 05-MH_05_difficult | euroc_mav | 0.1939 | 0.3029 | +56.24% | 1.0 |
| 06-V1_01_easy | euroc_mav | 0.0435 | 0.0386 | -11.11% | 1.0 |
| 07-V1_02_medium | euroc_mav | 0.0590 | 0.0537 | -8.95% | 1.0 |
| 08-V1_03_difficult | euroc_mav | 0.0567 | 0.0681 | +20.10% | 1.0 |
| 09-V2_01_easy | euroc_mav | 0.0582 | 0.0571 | -1.84% | 1.0 |
| 10-V2_02_medium | euroc_mav | 0.0459 | 0.0518 | +12.78% | 1.0 |
| 11-V2_03_difficult | euroc_mav | 0.1418 | 0.1188 | -16.21% | 1.0 |
| 15-infinite_infinite_fast.bag | kaist_vio | 0.0402 | 0.0402 | +0.07% | 1.0 |
| 16-square_square_fast.bag | kaist_vio | 0.0929 | 0.0832 | -10.39% | 1.0 |
| 17-square_square.bag | kaist_vio | 0.0502 | 0.0502 | -0.06% | 1.0 |
| 18-circle_circle_head.bag | kaist_vio | 0.0597 | 0.0598 | +0.05% | 1.0 |
| 20-infinite_infinite.bag | kaist_vio | 0.0278 | 0.0278 | +0.03% | 1.0 |
| 21-square_square_head.bag | kaist_vio | 0.2724 | 0.2722 | -0.06% | 1.0 |
| 22-circle_circle.bag | kaist_vio | 0.0499 | 0.0453 | -9.35% | 1.0 |
| 23-rotation_rotation_fast.bag | kaist_vio | 0.0954 | 0.0949 | -0.46% | 1.0 |
| 24-circle_circle_fast.bag | kaist_vio | 0.0930 | 0.0830 | -10.72% | 1.0 |
| 25-infinite_infinite_head.bag | kaist_vio | 0.1401 | 0.1405 | +0.29% | 1.0 |

n=21 assessable (rotation.bag unassessable for S1 in the frozen aggregate, matching
its denominators; S2 produced output on it as well). Median |Δ| = 9.3518%.

## 5. Latency ratios S2/S1 (DESKTOP PREVIEW; median over 3 repeats of per-callback median)
| sequence | median | min–max |
|---|---:|---|
| rotation_fast | 1.147 | 1.118–1.149 |
| circle | 3.185 | 3.165–3.212 |
| MH_05 | 1.703 | 1.688–1.729 |
| V2_03 | 2.776 | 2.747–2.831 |

## 6. F7 verdict (mechanical)
Completion parity: HOLDS (all S1-completed sequences complete under S2; association
coverage 1.0 everywhere). |paired ATE delta| median = 9.3518% > 2%.
**F7 = CHARACTERIZED-NOT-SUPPORTED.** The pre-licensed sentence ("negligible
accuracy delta at ~half the update cost") is NOT licensed. What the table
supports: the second pass changes accuracy substantially and inconsistently by
sequence (−17.3% to +56.2% on EuRoC; near-zero on 6/10 KAIST; ~−10% on 3
KAIST), at 1.15×–3.19× update-latency cost. Historical ±0.4% expectation not
reproduced on this protocol.

## 7. Integrity
Estimator source read-only both machines (no source edits this session; S2 via
launch param only). Desktop frozen binary re-verified pre-session
(0e46fa3e…, ceres a85e4691…). Per-cell SHA256SUMS throughout. RUN\_LOG
append-only; DECISIONS D1–D3 before affected work.

## 8. Artifacts
/home/moksh/schurvio-icra27-artifacts/lanes-c4b-s2-20260820T141454Z (cells, invalid-run preserves, F7\_EVALUATION.json, this summary).
Branch lanes/c4b-s2-20260819 @ d4d3134 base.
