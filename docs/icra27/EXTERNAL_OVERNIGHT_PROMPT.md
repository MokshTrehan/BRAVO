You are the implementer for EXT-VF-1, the external-systems campaign. Execute docs/icra27/EXTERNAL_PREREG.md exactly. This session differs from every prior campaign in one way you must internalize: VINS-Fusion is a live ROS node under wall-clock playback — nondeterministic. There are no byte-identity gates; the discipline is repeats, dispersion, environment capture, and an exclusive box.

AUTHORIZATION AND CONTEXT
As before: owner authorizes full-machine read; writes confined to the repo, a new dated artifacts root under /home/moksh/schurvio-icra27-artifacts/external-vf/, container images/volumes, and temp dirs. Repo: /home/moksh/schurvio-lite-rotation-robustness — branch external/vf-20260817 from the current campaign HEAD. Network access is expected for this session (container pull, upstream clones) — record every fetched artifact's identity (image digest, commit hashes).

HARD RULES
No tuning: the dataset authors' configs are used verbatim; permitted deltas are topic remapping and file paths ONLY, enumerated in a machine-readable diff committed before the first measured run. The loop-closure flag is recorded as found and reported, not chosen. No source patches to VINS-Fusion — if it will not build in the Melodic container without modification, STOP that variant and report exactly what fails. No reruns-to-green: crashed repeats are recorded repeats. Append-only RUN_LOG.md; DECISIONS.md before affected runs; per-cell SHA256SUMS. Your own stack's source remains read-only as always.

STEP 0 — BEFORE ANY MEASURED RUN
1. Verify docs/icra27/EXTERNAL_PREREG.md committed; absent/uncommitted -> STOP_REPORT.md.
2. Pull a ROS Melodic desktop container (record digest). Clone upstream VINS-Fusion at latest master (record commit). Install Ceres (record version). Build inside the container; a clean unpatched build is a precondition.
3. Fetch the dataset authors' per-algorithm KAIST-VIO configs from the kaistviodataset repo (record commit + per-file sha256). Identify the "VINS-Fusion" (IMU off) and "VINS-Fusion-imu" variants per the paper's naming. Emit the permitted-delta diff (topics/paths only). Record the loop-closure flag as found.
4. EuRoC SANITY GATE: MH_01 with stock EuRoC stereo config inside the container; a sane trajectory (qualitative + finite ATE vs GT) is required before any KAIST run. Failure -> bring-up triage, not results.
5. EXCLUSIVE-BOX CHECK: before each measured run, verify no other estimator/campaign processes (C8 or otherwise) are running; log the check. If the box is busy, wait — do not share.

STEP 1 — CAMPAIGN (nominal rate, serialized)
11 KAIST sequences x {VF, VF-imu} x 3 repeats, value-first order: rotation and rotation_fast pairs first (both variants, all repeats), then the head variants, then the remainder. Per run: timestamp-preserving nominal-rate playback; odometry captured to TUM; stdout/stderr teed; wall-clock, host-load snapshot, and container stats recorded. A non-starting or crashing run is captured with its console evidence and counted. For any sequence at 0/2 after two repeats, you may insert the slowed-rate diagnostic ladder (0.5x, 0.25x) as DIAGNOSTIC runs — labeled, outside the 3-repeat denominators — before the third nominal repeat.

STEP 2 — EVALUATION
Frozen convention verbatim (evo 1.31.1, unique nearest association 0.01 s, SE(3) Umeyama without scale; ATE translation RMSE + 1 m RPE). Per-cell metrics; per-sequence per-variant median with min/max across repeats; completion counts with denominators.

STEP 3 — THE THREE-WAY TABLE
Per sequence: (a) VF and VF-imu measured medians (this campaign, desktop, authors' configs), (b) S1's frozen desktop numbers from the existing campaign aggregates, (c) the dataset paper's published Table IV values as a context column, clearly labeled cross-protocol (different boards, era, unreported alignment). CG-4: variant names always explicit.

STEP 4 — MORNING_SUMMARY.md, in order
(1) environment identity: container digest, VF commit, Ceres, config hashes, permitted-delta diff; (2) sanity-gate result; (3) run accounting: 66 planned repeats vs executed vs crashed, diagnostics listed separately, exclusive-box checks all green; (4) completion table per variant with per-sequence n/3; (5) the three-way accuracy table with dispersion; (6) CG-1/CG-2 verdicts per sequence and the licensed sentences verbatim from the prereg — nothing stronger; (7) any diagnostic-ladder findings for failed cells; (8) artifact paths. No editorializing; a documented failure under the authors' configuration is a complete deliverable, and so is VINS-Fusion beating S1 somewhere — if it does, that number goes in the table like every other.

Budget: build + sanity ~1-2 h, campaign ~3-4 h at nominal rate. If the timebox threatens, cut repeats from 3 to 2 uniformly (record the cut) rather than cutting sequences — breadth beats depth for this table.
