# MORNING_SUMMARY — EXT-VF-1 (fresh run, executed 2026-08-20 evening)

## 1. Environment identity
Container: osrf/ros@sha256:39b2900892a32886033f168e55fd8ce4e01d804ed1ffdde156639b3cf789ee71
(melodic-desktop-full) -> extvf-melodic-vf:1 id 5a575db0c26a. VINS-Fusion upstream
be55a93 compiled UNPATCHED; Ceres 1.14.0 from source. Configs: kaistviodataset@ae67259
verbatim; permitted deltas = output_path + imu variant selector only
(docs/icra27/ext-vf/PERMITTED_DELTAS.txt, committed eaa8dcf pre-run). Loop closure as
found: load_previous_pose_graph 0; loop_fusion not launched, either variant.
Host: desktop (20.04, 16C), docker 26.1.3, exclusive-box checks logged per run.

## 2. Sanity gate
PASS — MH_01 stock configs in-container: VF-imu ATE 0.2645 m, VF 0.5501 m (1830 poses
each; finite, distinct, sane).

## 3. Run accounting
Planned 66 nominal-rate repeats; executed 66/66, crashed 0, INVALID 0. One additional
infra-invalid cell (driver permission bug, quarantined, logged, not an estimator
outcome). Slowed-rate diagnostic ladder never armed (trigger was 0/2 crashes; none
occurred). Exclusive-box: all green.

## 4-5. Completion + three-way accuracy (ATE translation RMSE, m; frozen convention:
evo 1.31.1, 0.01 s association, SE(3) Umeyama no scale; GT = in-bag /pose_transformed)
Completion rule (declared): finite ATE AND odometry covers >=90 % of GT span.
'part.@cov' rows: covered-span descriptive ATE (labeled *), truncation mechanism in §7.

| sequence | VF median [min–max] (n/3) | VF-imu median [min–max] (n/3) | S1 frozen desktop |
|---|---|---|---:|
| rotation_fast | (0/3) part.@cov 0.849: 0.253* | (0/3) part.@cov 0.849: 0.118* | 0.09536 |
| rotation | 0.155 [0.155–0.182] (3/3) | 91.465 [11.870–93.611] (3/3) | unassessable |
| circle | (0/3) part.@cov 0.825: 0.046* | (0/3) part.@cov 0.825: 0.068* | 0.049943 |
| circle_fast | (0/3) part.@cov 0.816: 0.090* | (0/3) part.@cov 0.816: 0.069* | 0.092991 |
| circle_head | (0/3) part.@cov 0.856: 0.051* | (0/3) part.@cov 0.856: 0.099* | 0.059745 |
| infinite | (0/3) part.@cov 0.788: 0.039* | (0/3) part.@cov 0.788: 0.077* | 0.027789 |
| infinite_fast | 0.057 [0.042–0.070] (3/3) | 0.055 [0.055–0.055] (3/3) | 0.040156 |
| infinite_head | (0/3) part.@cov 0.865: 0.053* | (0/3) part.@cov 0.866: 0.110* | 0.140064 |
| square | (0/3) part.@cov 0.856: 0.087* | (0/3) part.@cov 0.856: 0.117* | 0.050238 |
| square_fast | 0.068 [0.068–0.071] (3/3) | 0.071 [0.071–0.071] (3/3) | 0.092856 |
| square_head | 0.116 [0.116–0.116] (3/3) | 0.129 [0.122–0.129] (3/3) | 0.272364 |

S1 column: frozen CDSC-1R4 aggregates, same convention (desktop replay protocol —
deterministic serial, not live playback; protocol difference noted). Dataset-paper
Table IV context (cross-protocol, different boards/era/alignment, per CG-3 cited only):
stereo-VIO family incl. VINS-Fusion-imu reported completing both rotation sequences at
~0.07–0.28 m on NX/AGX-class boards.

## 6. Claim-gate verdicts (per prereg wording)
- CG-1 (comparison licensed, >=2/3 complete): rotation, infinite_fast, square_fast,
  square_head — both variants. Licensed rows as tabled. Notables: VF-imu DIVERGES on
  rotation (median 91.46 m; min 11.87 m) while VF (IMU off) tracks at 0.155 m —
  contrary to the Table IV context row; VF beats S1 on square_head (0.116 vs 0.272);
  S1 beats or ties the VF family elsewhere it is assessable.
- CG-2 (0/3 by the completion rule): rotation_fast, circle, circle_fast, circle_head,
  infinite, infinite_head — for BOTH variants, with one shared, measured mechanism
  (§7): the estimator sustains only ~80–90 % of nominal rate on this stack, and the
  session-end drain (~13 s) truncates the backlog tail. Licensed sentence: on these
  sequences the pinned variants "do not sustain nominal-rate processing under the
  dataset authors' configuration on a modern containerized desktop stack" — with the
  version-drift caveat, never an algorithm-level claim. Covered-span accuracy on those
  same sequences is 0.04–0.12 m (*descriptive), consistent with the pre-committed
  Table-IV-based prediction of 0.05–0.3 m.
- CG-3: honored above. CG-4: variant names explicit throughout.

## 7. Diagnostic finding (post-campaign analysis, evaluation-time)
Truncation anatomy: init fast (~0.7 s); odometry cadence steady ~15 Hz with no stalls
to the cut; in-bag IMU and GT end together, so truncation is NOT bag structure. VINS
processes ~10–20 % slower than wall clock on 6/11 sequences; the driver's ~13 s
post-playback drain (vs the A0 harness's 180 s) clips the backlog tail uniformly
(~20–39 s). Limitation recorded: a drain-window extension would convert tail-lag into
full-span trajectories WITHOUT changing what nominal-rate processing means; left as a
labeled follow-up, not rerun tonight (prereg frozen at first measured run).

## 8. Artifacts
/home/moksh/schurvio-icra27-artifacts/external-vf/extvf1-20260820T143502Z (66 cells + quarantined infra cell, per-cell SHA256SUMS, ground-truth/,
EVALUATION.json, RUN_LOG.md, DECISIONS.md, docker/, configs/, sanity/).
