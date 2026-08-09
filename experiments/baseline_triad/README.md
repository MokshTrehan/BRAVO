# Supported-intersection desktop baselines

This directory freezes the configuration and ordinary serial-runner contract
for the four `SUPPORTED_INTERSECTION_PROFILE` methods:

- `U-NS`: exact upstream OpenVINS production nullspace at
  `69488123ed9362dd44b6f28e7f4680abbff1442b`;
- `L-NS`: local production nullspace descended without estimator-source
  changes from `82504db63fafda40dcf44b8e66cbd29609743a1d`;
- `L-SCHUR`: local production Schur, one pass, from the same algorithm freeze;
- `OV-SCHUR`: exact ov_SchurVINS `feat/schurVINS` source at
  `d988139c0ba3cc51abacb8b5b5acb482a19325ba`.

It does not contain external source, results, bags, trajectories, or build
products. All generated output must go under the run root outside Git.

## Parser and source audit

At the frozen external commits, upstream OpenVINS and ov_SchurVINS have
byte-identical `VioManagerOptions.h`, `StateOptions.h`,
`FeatureInitializerOptions.h`, and `InertialInitializerOptions.h`. Their KLT
and feature-initializer implementation hashes are also identical to local:

- `TrackKLT.cpp`:
  `05af28f9b547ae07f619eb01f37b7682216d6ec3b8a5db491834b34790aeeaea`;
- `FeatureInitializer.cpp`:
  `a016e3eeedbe371c56ce43b16f279a58b043ceeb3cc17078ed2b6c4d3ad1058e`.

Only local parses `up_msckf_landmark_elimination` and
`up_msckf_max_visual_passes`. Exact upstream invokes its production
nullspace updater directly. Exact ov constructs and invokes
`UpdaterSchurVINS` directly. Consequently, `u_ns.yaml` and `ov_schur.yaml`
omit the inapplicable local keys; the local configs require both keys. The
normalizer removes only those parser-inapplicable keys before enforcing exact
equality of every other top-level estimator parameter.

All four configs freeze `GLOBAL_3D`, FEJ off, all camera calibration off,
`max_slam: 0`, 11 clones, an accepted MSCKF-track cap of 40, identical stereo
KLT and initialization, zero OpenCV workers, serial pub/sub behavior, and the
same EuRoC calibration/noise values. Start time `0.0`, one pass, CPU affinity,
no GPU, and a finite wall timeout are runtime invariants.
The wrapper also fixes common BLAS/OpenMP/vecLib thread environments to one,
the Python hash seed to zero, timezone to UTC, and locale to `C.UTF-8`.

## Irreducible ov implementation semantics

Configuration equality is not algebraic identity. Exact ov source hardcodes
the following inside `UpdaterSchurVINS.cpp`:

- residual and Jacobian scaling by `0.25`;
- Huber threshold `1.5`;
- no upstream-style per-feature NIS gate;
- direct inversion of the landmark information block without a rank check.

These are implementation differences under test, not profile parameters.
They must be disclosed in analysis and must not be tuned away or described as
matched NIS/noise behavior. The quantitative ov binary must remain clean; an
instrumentation-only reachability build is not valid for timing.

## Verify and normalize

The verifier uses only `/usr/bin/python3` and writes normalized profiles,
semantic diffs, and actual configuration hashes to a new directory:

```bash
/usr/bin/python3 experiments/baseline_triad/tools/verify_profiles.py \
  --output /home/moksh/schurvio-baseline-triad/<RUN>/manifests/supported_intersection
```

It fails if any parsed estimator setting other than elimination mode differs.
Comments and the two repository-inapplicable local selector keys do not enter
the semantic comparison. `parameter_matrix.csv`, `method_manifest.json`, and
`config_sha256.csv` are the checked-in machine-readable contract.

## Run one method

`run_serial.py` requires a separately built workspace, its exact source tree,
an already declared dataset hash, a CPU list, an isolated ROS master port, and
a finite timeout. It recomputes the bag hash under its own finite hash timeout
before opening ROS:

```bash
/usr/bin/python3 experiments/baseline_triad/tools/run_serial.py \
  --method U-NS \
  --workspace-setup /path/to/upstream_ws/devel/setup.bash \
  --source-repo /path/to/open_vins \
  --bag /path/to/MH_01_easy.bag \
  --bag-sha256 <64-lowercase-hex-digits> \
  --output-dir /home/moksh/schurvio-baseline-triad/<RUN>/results/MH_01/U-NS-smoke \
  --cpu-list 0-3 \
  --ros-master-port 11401 \
  --timeout-seconds 600 \
  --bag-duration-seconds 60
```

Use `--bag-duration-seconds -1` for a full run. Repeats need distinct output
directories and must keep CPU affinity and environment controls identical.
Run order alternation is an orchestration responsibility outside this wrapper.

The wrapper launches only `ros1_serial_msckf` through
`launch/serial_baseline.launch`. The estimator writes the same native
total-state rows used by the shared converter. No `pose_to_file` node is
started. Per-run output is:

- `command.json` — exact command, environment, source, binary, config, and bag
  provenance;
- `run.log` — combined roslaunch/estimator console;
- `resource_usage.txt` — `/usr/bin/time -v` output;
- `state_estimate.txt` and `state_deviation.txt` — native OpenVINS total state;
- `timing.csv` — native estimator stage timings;
- `exit_status.txt` and `timed_out.txt` — uniform summarizer inputs;
- `result.json` — roslaunch exit, parsed estimator-child exit, completion,
  timeout, elapsed time, and output hashes;
- isolated `ros_home/` and `ros_logs/` directories.

The wrapper starts a new process session, handles `INT`, `TERM`, and `HUP`, and
terminates the complete process group with bounded `TERM` then `KILL`. It also
recognizes roslaunch's misleading zero exit when its required estimator child
reports a nonzero `process has died` status. Process completion and child exit
are deliberately separate from dataset-interval coverage; coverage is assessed
from native state timestamps by the shared summarizer.
