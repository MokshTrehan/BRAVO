# EXTERNAL_PREREG — VINS-Fusion under the dataset authors' configuration (EXT-VF-1)

**Registered:** 2026-08-17, before any run. Frozen at the first measured run. Timebox: tonight + tomorrow-morning triage; the standing 2-working-day C4a budget is a hard ceiling.

## Purpose
Produce the same-protocol external comparison the paper currently lacks: VINS-Fusion (both dataset-paper variants) on all eleven KAIST-VIO sequences under the dataset authors' own published configuration, evaluated with the frozen convention, against S1's desktop numbers. Either outcome — completion-and-comparison or documented failure — is a complete deliverable.

## Systems and environment (pre-declared)
- **VF** ("VINS-Fusion" per the dataset paper: stereo, IMU disabled) and **VF-imu** (stereo + IMU): upstream HKUST-Aerial-Robotics/VINS-Fusion at a pinned commit (record hash), built inside a **ROS Melodic container** (record image digest, Ceres version). Rationale: clean build of the 2019-era code without source patches; matches the dataset paper's Ubuntu 18.04/Melodic stack.
- **Configurations:** the dataset authors' published per-algorithm configs (kaistviodataset repo), used **verbatim**, sha256-recorded. Permitted local deltas: topic remapping and file paths ONLY, enumerated in a machine-readable diff. Loop-closure flag recorded **as found** in the authors' configs and reported. Any other delta = tuning = prohibited.
- **Prediction (pre-committed, from the dataset paper's Table IV):** the VF family is expected to complete most or all sequences at roughly 0.05–0.3 m ATE, including the rotation family.

## Protocol
1. **EuRoC sanity gate:** MH_01 with stock EuRoC stereo config must produce a sane trajectory before any KAIST run; failure here is a bring-up problem, not a result.
2. **Exclusive-box rule:** measured runs start only when no other estimator/campaign process is running (checked and logged per run). Host CPU governor recorded.
3. **Playback:** nominal-rate, timestamp-preserving rosbag play. Non-completion triage may use the frozen slowed-rate diagnostic ladder (0.5×, 0.25×), documented per attempt — diagnostics only, never reported as primary results.
4. **Matrix:** 11 KAIST sequences × {VF, VF-imu} × **3 repeats** (live node — nondeterministic; repeats are real again). Odometry captured to TUM format per run.
5. **Evaluation:** frozen convention verbatim — evo 1.31.1, unique nearest association 0.01 s, SE(3) Umeyama without scale, translation ATE RMSE + 1 m RPE. Per-cell values; per-sequence median with min/max.

## Claim gates (pre-registered)
- **CG-1 (comparison licensed):** a variant completing ≥2/3 repeats on a sequence yields a comparable median; the paper's table shows VF-local vs S1 same-protocol, per sequence, with dispersion.
- **CG-2 (failure licensed):** 0/3 on a sequence, with typed observations and the diagnostic ladder documented → "fails under the dataset authors' configuration on a modern containerized stack," with the version-drift caveat. Never phrased as an algorithm-level claim.
- **CG-3 (no protocol mixing):** published Table IV values are cited as context only — different boards, era, and unreported alignment — and never appear unlabeled beside measured values.
- **CG-4:** completion statements always name the variant; "VINS-Fusion" unqualified is prohibited (the paper's naming trap).

## Out of scope tonight (deferred to the Tuesday lane, pre-authorized)
C4b items: freshness reproduction of the frozen ORB-SLAM3/SchurVINS smoke protocol; the SchurVINS input-normalization attempt; the ORB-SLAM3 mono-inertial attempt. Lane-internal cut order unchanged.

## Discipline
Append-only RUN_LOG; DECISIONS before affected runs; per-cell SHA256SUMS including config hashes and container digest; no reruns-to-green (a crashed repeat is a recorded repeat); estimator repos read-only beyond the enumerated deltas; counts with denominators everywhere.
