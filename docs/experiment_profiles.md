# SchurVIO-Lite experiment profiles

Status: profile names and historical Gate-D provenance frozen on 2026-08-09.

These names separate an evaluated historical configuration from a matched
cross-repository experiment and from the intended deployment scope. They are
not interchangeable.

## FROZEN_GATE_D_PROFILE

This name refers only to the accepted one-pass/two-pass EuRoC runs made from
evaluation commit
`a4463d8f839e6a283632464b8023f788ab72b60a`. The later head
`82504db63fafda40dcf44b8e66cbd29609743a1d` adds tests only and was not the
evaluation executable.

The tracked YAML files and hashes are:

- `config/euroc_mav/estimator_config_gate_d_schur_one_pass.yaml`:
  `39279fd929ae91dc1ec63c44f17ff66c369dcd4c9f094ffbf7b95acae07f6d33`;
- `config/euroc_mav/estimator_config_gate_d_schur_two_pass.yaml`:
  `7d00c36197d21b934f5f555c6a47b3735033963a9acaf2d79199e7b035a35b0d`.

The files differ only in `up_msckf_max_visual_passes: 1` versus `2`.
Their text enables camera extrinsic, intrinsic, and camera/IMU time-offset
calibration and requests four OpenCV threads. Those values were not the
effective runtime values. The immutable Gate-D launch has SHA-256
`baf7afbeb53596c1a609dc570f19db778fd7cba7fef97d7fafe3856245394867`
and supplies ROS overrides setting all three camera-calibration flags to
`false` and `num_opencv_threads` to `0`. The configuration parser checks
ROS first. Every inspected full-run log records those overrides and prints the
three effective calibration flags as zero.

The effective evaluated profile was therefore:

- FEJ enabled;
- camera extrinsic, intrinsic, and time-offset calibration disabled;
- transient landmarks `GLOBAL_3D`;
- 11 clones;
- 50 persistent SLAM landmarks, with 25 per SLAM update;
- at most 40 temporary MSCKF tracks per update;
- stereo KLT, a configured 200-point total budget split uniformly to 100 per
  camera, and 21 Hz tracking;
- zero OpenCV worker threads and serial ROS publication/subscription;
- Schur temporary-landmark elimination;
- one or two visual passes according to the selected hashed YAML;
- bag start `0.0`, full duration, and the ordinary serial estimator.

Thus the accepted Gate-D runs used fixed camera calibration. The apparent
online-calibration setting exists in the hashed YAML text but was superseded
at runtime. The accepted numerical results remain valid; the earlier
documentation/config discrepancy was a provenance-description defect.

The portable evidence inventory is
`docs/frozen_gate_d_manifest.json`. The complete immutable report remains the
authority for run-level outputs and disclosed process-record limitations.

## SUPPORTED_INTERSECTION_PROFILE

This is the matched one-pass profile used to compare exact upstream OpenVINS,
local nullspace, local Schur, and exact ov_SchurVINS. It is a separate
experiment and must not be compared causally with the native Gate-D profile.

Its common contract is:

- `GLOBAL_3D` temporary landmarks;
- FEJ disabled;
- camera extrinsic, intrinsic, and time-offset calibration disabled;
- persistent SLAM landmarks disabled;
- stereo KLT with an identical feature, grid, tracking-frequency, and accepted
  MSCKF-update budget;
- 11 clones;
- identical known EuRoC camera/IMU calibration and noise files;
- identical static initialization;
- zero OpenCV worker threads and serial estimator/pub/sub execution;
- start time `0.0`;
- exactly one visual pass.

Only the implementation selector differs semantically:

- `U-NS`: exact upstream OpenVINS production nullspace;
- `L-NS`: local production nullspace;
- `L-SCHUR`: local production Schur;
- `OV-SCHUR`: exact released ov_SchurVINS Schur path.

Repository-specific ignored YAML keys are not treated as estimator-state
differences. Any source-fixed behavior in ov_SchurVINS, including its internal
Huber/scaling and lack of the upstream NIS gate, is an implementation
difference to report, not a parameter to tune away. Results are attached only
after the matched runs complete.

## DEPLOYMENT_INTENT_PROFILE

This name describes the documented future low-power deployment direction:
local Schur, one visual pass, FEJ enabled, and known camera calibration kept
fixed. It does not identify an evaluated Orin configuration, binary, clock,
core, power, thermal, feature-cap, or persistent-landmark setting. Those fields
remain unfrozen until a later hardware task explicitly opens them.

No Gate-D, supported-intersection, desktop, or future hardware result may be
relabelled as `DEPLOYMENT_INTENT_PROFILE`. This document changes no estimator
behavior and does not authorize embedded work.
