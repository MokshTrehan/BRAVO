# Conditioning replay profiles

These profiles freeze the real-camera experiment before looking at estimator
results. Both datasets use FEJ, one visual pass, transient `GLOBAL_3D`
landmarks, fixed camera extrinsics/intrinsics/time offset, and no persistent
SLAM landmarks. The dataset's existing KLT front end, 11-clone window,
40-track update cap, 200-point detection budget, and four OpenCV threads are
unchanged.

| Dataset | Native camera model | Nullspace profile | Schur profile |
|---|---|---|---|
| EuRoC | stereo radtan | `config/euroc_mav/estimator_config_conditioning_nullspace.yaml` | `config/euroc_mav/estimator_config_conditioning_schur.yaml` |
| TUM-VI 512 | stereo equidistant | `config/tum_vi/estimator_config_conditioning_nullspace.yaml` | `config/tum_vi/estimator_config_conditioning_schur.yaml` |

Within each dataset pair, the only byte-level setting difference is
`up_msckf_landmark_elimination`. Capture is explicitly false in YAML, and no
capture path is stored there. The harness supplies capture enablement and its
absolute create-new output path as private ROS parameters at replay time.

Validate all four profiles without opening a bag:

```bash
/usr/bin/python3 experiments/camera_conditioning/verify_replay_profiles.py
```

The replay harness launches only the ordinary `ros1_serial_msckf` node through
`roslaunch`. It rejects undeclared configs, output inside Git, an existing
output/capture target, an incorrectly resolved ROS package, a busy ROS port,
and invalid or unbounded time settings. It starts a new process group, sends
the complete group `SIGTERM` at timeout/interruption, escalates to `SIGKILL`
after the finite grace interval, and records command/source/config/bag/binary
provenance plus hashes in `command.json` and `result.json`.
After a successful replay it also derives `trajectory_tum.txt` from the native
total-state output by token-preserving reordering, and records its pose count
and SHA-256. This derived file is suitable for byte-parity checks and external
trajectory evaluation; it does not feed back into the estimator.

Use a workspace built from this worktree. These are the exact capture-off and
capture-on forms; set the four absolute path/hash variables to the frozen run
inputs before executing them:

```bash
REPOSITORY='/home/moksh/newSlam camera-conditioning'
WORKSPACE_SETUP='/absolute/current_ros1_ws/devel/setup.bash'
BAG='/home/moksh/Downloads/tum_vi/calibrated/512_16/dataset-room4_512_16.bag'
BAG_SHA256='<64-lowercase-hex-dataset-sha256>'
RUN_ROOT='/home/moksh/schurvio-camera-conditioning/20260810T003905Z'

/usr/bin/python3 "$REPOSITORY/experiments/camera_conditioning/run_ros1_replay.py" \
  --workspace-setup "$WORKSPACE_SETUP" \
  --bag "$BAG" \
  --expected-bag-sha256 "$BAG_SHA256" \
  --config "$REPOSITORY/config/tum_vi/estimator_config_conditioning_nullspace.yaml" \
  --output-dir "$RUN_ROOT/replays/room4/nullspace-capture-off" \
  --bag-start-seconds 0.0 \
  --bag-duration-seconds -1 \
  --timeout-seconds 7200 \
  --termination-grace-seconds 15 \
  --bag-hash-timeout-seconds 1800 \
  --ros-master-port 11341

/usr/bin/python3 "$REPOSITORY/experiments/camera_conditioning/run_ros1_replay.py" \
  --workspace-setup "$WORKSPACE_SETUP" \
  --bag "$BAG" \
  --expected-bag-sha256 "$BAG_SHA256" \
  --config "$REPOSITORY/config/tum_vi/estimator_config_conditioning_nullspace.yaml" \
  --output-dir "$RUN_ROOT/replays/room4/nullspace-capture-on" \
  --capture \
  --capture-path "$RUN_ROOT/replays/room4/nullspace-capture-on/camera_systems.scvio" \
  --bag-start-seconds 0.0 \
  --bag-duration-seconds -1 \
  --timeout-seconds 7200 \
  --termination-grace-seconds 15 \
  --bag-hash-timeout-seconds 1800 \
  --ros-master-port 11342
```

For the same-sequence reducer replication, change only the profile path from
`conditioning_nullspace` to `conditioning_schur`, choose a new output/capture
path and free ROS port, and leave every estimator and bag argument fixed.
EuRoC instrumentation smoke uses the corresponding `config/euroc_mav` pair;
TUM-VI held-out runs use the `config/tum_vi` pair. Full held-out runs retain
the declared `0.0` bag start and `-1` duration. The harness never selects or
tunes a sequence-specific estimator parameter.
