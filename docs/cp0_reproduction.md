# CP0 reproducible baseline

The CP0 run contract is defined by `project/cp0_baseline.json`; the pinned
upstream revision is defined by `UPSTREAM_REVISION`. Together they freeze the
reviewed serial-runner lifecycle patch, EuRoC calibration and ground-truth
files, MH_01 bag identity, startup offset, deterministic ROS overrides, evo
protocol, completion witness, and ATE acceptance threshold. The observed
result and checksum anchor are tracked separately in `project/baselines.yaml`.

The scripts support repository and dataset paths containing spaces. Do not
remove their quoting when copying commands.

## Build

The supported host path is ROS Noetic on Ubuntu 20.04. System ROS/OpenCV/Eigen
packages must already be installed. Ceres is not installed system-wide and no
`sudo` command is used:

```bash
cd "/home/moksh/newSlam variant"
CP0_BUILD_JOBS=8 scripts/cp0/build_ros1.sh
```

The build script checks out Ceres 1.14.0 at
`facb199f3eda902360f9e1d5271372b7e54febe1`, builds it under
`build/vendor/`, configures catkin with `/usr/bin/python3`, links OpenVINS to
that local Ceres, and writes `build/cp0-ws/CP0_BUILD_PROVENANCE.json`. It
refuses a dirty Ceres checkout. The provenance marker content-addresses all
OpenVINS build inputs, the canonical serial executable, its project shared
libraries, and Ceres; a stale or swapped build cannot pass run preflight.

## Run and seal

Use an explicit four-core affinity for the canonical desktop run:

```bash
scripts/cp0/run_mh01.sh \
  --bag "/home/moksh/Downloads/machine_hall/MH_01_easy/MH_01_easy.bag" \
  --affinity 0-3
```

Omit `--affinity` only for a diagnostic run; the effective inherited affinity
is still recorded. Use `--no-seal` to test the full gate while retaining a
passing result under staging.

The harness performs these operations in order:

1. verifies the exact bag size and SHA-256, pinned upstream ancestry, reviewed
   lifecycle-patch hash, frozen config/ground truth, and build provenance;
2. snapshots every run-control input under the staging artifact;
3. resolves the launch parameters and proves OpenCV threads, ROS threading,
   and all camera calibration states are fixed;
4. enables OpenVINS's synchronous in-process `save_total_state` writer for
   both the complete state and its standard deviations, then runs the bag;
5. converts total-state `[t,qx,qy,qz,qw,px,py,pz,...]` into TUM
   `[t,px,py,pz,qx,qy,qz,qw]` without changing numeric text;
6. runs SE(3)-aligned/no-scale evo APE translation and 1 m all-pairs-from-
   reference RPE translation and rotation with a fixed 0.01 s association
   tolerance;
7. checks every numeric field in the state and std logs, exact 2,767-row
   state/std/trajectory/timing timestamp parity, the frozen final-camera
   completion witness, and ATE RMSE at most 0.25 m;
8. writes `manifest.json`, `validation.json`, and `SHA256SUMS`, removes ROS
   bookkeeping, rejects every remaining symlink, and moves the entire passing
   directory with Linux `renameat2(RENAME_NOREPLACE)`.

All executions begin in `results/staging/baseline/`. A failed run stays there
with a failure manifest and is never promoted. A passing run is atomically
moved to `results/immutable/baseline/<run-id>`; an existing destination is
never replaced.

ROS uses alarming but normal wording when the required serial node returns
zero. The validator accepts `REQUIRED process [cp0_vio-N] has died!` only when
the immediately following status is `process has finished cleanly`. It still
rejects nonzero process exits, restarts, fatal logs, NaN/Inf, and covariance
failure markers.

`save_total_state` computes and writes the full covariance on every update, so
the CP0 timing file is instrumentation/completion evidence only. It is not a
performance baseline and must not support latency claims.

## Independent verification

Verify the sealed tree without replaying the 2.67 GB dataset:

```bash
scripts/cp0/verify_run.py \
  "results/immutable/baseline/<run-id>"
```

The verifier checks complete checksum coverage, absence of symlinks/scratch
ROS state, the source/config snapshots, exact evo commands and result archives,
full-run completion, synchronous state/std parity, and every validation gate.

After sealing, copy the printed `SHA256SUMS SHA-256` value into the CP0
checkpoint evidence committed to Git. That external digest anchors the
otherwise self-contained artifact tree. It can be supplied during later audit:

```bash
scripts/cp0/verify_run.py \
  "results/immutable/baseline/<run-id>" \
  --sha256sums-sha256 "<digest recorded in Git>"
```
