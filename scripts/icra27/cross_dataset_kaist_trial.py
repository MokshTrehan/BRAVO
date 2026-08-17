#!/usr/bin/python3.8
"""Append-only fresh-KAIST adapter for the CDSC-1R4 U0/S1 comparison.

This module deliberately does not alter either estimator.  U0 runs the pinned
upstream executable, native KAIST configuration, and native record-time stereo
selector.  S1 runs the pinned C2 executable/configuration and exact-header
selector.  Estimator failures are published as results rather than repaired.

The ``run`` command never accepts or opens ground truth.  The separate
``post-pair-rotation`` command is the only ground-truth-aware surface: it first
proves that both scored estimator process groups are closed, then evaluates the
frozen C2 rotation-gap, covariance/NEES, and numeric target gates.  Those gates
are reported separately from ordinary passage and accuracy eligibility.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

import numpy as np
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import cross_dataset_trial as common  # noqa: E402
import kaist_pairing_census as pairing  # noqa: E402
import kaist_pair_evaluator as metric_core  # noqa: E402
import rotation_robustness_trial as rotation  # noqa: E402


SCHEMA = common.SCHEMA
POST_PAIR_SCHEMA = "schurvio.icra27.cross_dataset.kaist_rotation_post_pair.v1"
KAIST_CENSUS_SCHEMA = "schurvio.icra27.cross_dataset.kaist_pair_census.v2"
DATASET = "kaist_vio"
SYSTEMS = common.SYSTEMS
MODES = common.MODES
ELIGIBLE_STATUSES = common.ELIGIBLE_STATUSES
STATUSES = common.STATUSES
PYTHON = Path("/usr/bin/python3.8")
CONVERTER = common.CONVERTER
PAIRING_TOOL = SCRIPT_DIR / "kaist_pairing_census.py"
RUNTIME_IDENTITY_TOOL = SCRIPT_DIR / "kaist_runtime_identity.py"

CANONICAL_PROTOCOL = REPO_ROOT / "docs" / "icra27" / "CROSS_DATASET_SYSTEM_COMPARISON_PROTOCOL.md"
CANONICAL_MATRIX = REPO_ROOT / "project" / "icra27_cross_dataset_matrix.yaml"
U0_CONFIG = common.U0_SOURCE_ROOT / "config" / "kaist_vio" / "estimator_config.yaml"
S1_CONFIG = REPO_ROOT / "config" / "kaist_vio_rotation_robustness" / "estimator_config.yaml"
U0_LAUNCH = REPO_ROOT / "project" / "icra27_kaist_u0_serial.launch"
S1_LAUNCH = REPO_ROOT / "project" / "rotation_robustness_serial.launch"

CANONICAL_INPUTS: Mapping[str, Mapping[str, Any]] = {
    "U0": {
        "config": U0_CONFIG,
        "config_sha256": "a7212a7b0e7e2df3ddefe402f12b5cbd155b368b43253f96a306d6638ba648d5",
        "launch": U0_LAUNCH,
        "launch_sha256": "b9ea5787fd0ce998028bc63c04b09da796f344800a4b0e9b3f90e4d906412640",
        "node": "icra27_kaist_u0",
        "namespace": "/icra27_kaist_u0",
    },
    "S1": {
        "config": S1_CONFIG,
        "config_sha256": "fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2",
        "launch": S1_LAUNCH,
        "launch_sha256": "57a4bd1fa7fefdc84a73efe7c807a259b391858fbb1157fd8716afdd5770cc18",
        "node": "kaist_vio_turnsafe_baseline",
        "namespace": "/kaist_vio_turnsafe_baseline",
    },
    # PERTURB-1 matched nullspace control: same binary, config, calibration,
    # recovery setting, node name, and namespace as S1; the launch differs only
    # by up_msckf_landmark_elimination=nullspace.
    "N0": {
        "config": S1_CONFIG,
        "config_sha256": "fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2",
        "launch": REPO_ROOT / "project" / "icra27_kaist_n0_serial.launch",
        "launch_sha256": "256fd81971445e9ec927bcb3a075a4c24d24b782ba9f014bb0ba04bfd97eb5cd",
        "node": "kaist_vio_turnsafe_baseline",
        "namespace": "/kaist_vio_turnsafe_baseline",
    },
    # ABLATE-REC-1 (docs/icra27/ABLATION_PREREG.md) single-delta recovery
    # ablations: same binary, config, calibration, node name and namespace as
    # their recON counterpart (S1 / N0); the launch differs only by the one
    # added ROS parameter long_gap_recovery_enabled=false, which the executable
    # reads through the ordinary ROS-over-YAML parser precedence.
    "S1-recOFF": {
        "config": S1_CONFIG,
        "config_sha256": "fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2",
        "launch": REPO_ROOT / "project" / "icra27_kaist_s1_recoff_serial.launch",
        "launch_sha256": "c6d7a91896056c1bf79a6a75052844eb96c9a278a44e8d32ea0ed0e71c5b2036",
        "node": "kaist_vio_turnsafe_baseline",
        "namespace": "/kaist_vio_turnsafe_baseline",
    },
    "N0-recOFF": {
        "config": S1_CONFIG,
        "config_sha256": "fa387a5c2146ef2a0471a4cf7acec392e43632a4e51983bb29f9ca8244d9d4b2",
        "launch": REPO_ROOT / "project" / "icra27_kaist_n0_recoff_serial.launch",
        "launch_sha256": "2592d69a5f3f1327add32ba9086f1e51ea0851d4d12b601a2e20858b8826e1a4",
        "node": "kaist_vio_turnsafe_baseline",
        "namespace": "/kaist_vio_turnsafe_baseline",
    },
}
# Systems that run the frozen S1 executable and exact-header seam.
S1_LIKE_SYSTEMS: Tuple[str, ...] = ("S1", "N0", "S1-recOFF", "N0-recOFF")
# Systems whose frozen configuration keeps the C2 long-gap recovery enabled and
# whose rotation.bag completion is therefore bound to the C2 recovery-gap seam.
RECOVERY_BOUND_SYSTEMS: Tuple[str, ...] = ("S1", "N0")
# ABLATE-REC-1 recOFF systems -> their recON counterpart.
RECOVERY_ABLATED_SYSTEMS = {"S1-recOFF": "S1", "N0-recOFF": "N0"}
RECOVERY_SWITCH_PARAMETER = "long_gap_recovery_enabled"
LANDMARK_ELIMINATION = {"S1": "schur", "N0": "nullspace", "S1-recOFF": "schur", "N0-recOFF": "nullspace"}
PERTURBATION_SYSTEMS: Tuple[str, ...] = ("U0", "S1", "N0", "S1-recOFF", "N0-recOFF")
PERTURBATION_CAMPAIGN_ID = "PERTURB-1"
PERTURBATION_CAMPAIGN_IDS: Tuple[str, ...] = ("PERTURB-1", "ABLATE-REC-1")
PERTURBATION_SEED_LABELS: Tuple[str, ...] = ("frozen",)

KAIST_SEQUENCES: Tuple[str, ...] = (
    "infinite/infinite_fast.bag",
    "square/square_fast.bag",
    "square/square.bag",
    "circle/circle_head.bag",
    "rotation/rotation.bag",
    "infinite/infinite.bag",
    "square/square_head.bag",
    "circle/circle.bag",
    "rotation/rotation_fast.bag",
    "circle/circle_fast.bag",
    "infinite/infinite_head.bag",
)

CAMERA0_TOPIC = pairing.CAMERA0_TOPIC
CAMERA1_TOPIC = pairing.CAMERA1_TOPIC
IMU_TOPIC = pairing.IMU_TOPIC
SAFE_SEQUENCE_RE = re.compile(r"(?:rotation|circle|infinite|square)/[A-Za-z0-9_.-]+\.bag")

U0_LAUNCH_ARGUMENTS = frozenset(
    ("config_path", "bag", "bag_start", "bag_durr", "path_state", "path_std", "path_time", "record_timing")
)
S1_LAUNCH_ARGUMENTS = frozenset(
    ("bag", "candidate_config", "bag_start", "path_state", "path_std", "path_time", "verbosity")
)

ATTEMPT_DETAIL_RE = re.compile(
    r"\[LONG-GAP-RECOVERY\]: event=attempt epoch=(?P<epoch>[0-9]+) "
    r"attempt=(?P<attempt>[0-9]+) timestamp=(?P<timestamp>[-+0-9.eE]+) "
    r"imu_prediction=(?P<imu_prediction>[01]) accepted=(?P<accepted>[01]) "
    r"reason=(?P<reason>[a-z_]+) supplied=(?P<supplied>[0-9]+) "
    r"valid=(?P<valid>[0-9]+) inliers=(?P<inliers>[0-9]+) "
    r"inlier_ratio=(?P<inlier_ratio>[-+0-9.eE]+) "
    r"max_reprojection_px=(?P<max_reprojection_px>[-+0-9.eE]+) "
    r"min_depth=(?P<min_depth>[-+0-9.eE]+) span_x=(?P<span_x>[-+0-9.eE]+) "
    r"span_y=(?P<span_y>[-+0-9.eE]+) support_ratio=(?P<support_ratio>[-+0-9.eE]+) "
    r"imu_angle_deg=(?P<imu_angle_deg>[-+0-9.eE]+) state_unchanged=(?P<state_unchanged>[01])"
)
ACCEPTED_POSE_RE = re.compile(
    r"\[LONG-GAP-RECOVERY\]: event=accepted_pose epoch=(?P<epoch>[0-9]+) "
    r"attempt=(?P<attempt>[0-9]+) timestamp=(?P<timestamp>[-+0-9.eE]+) "
    r"p_x=(?P<p_x>[-+0-9.eE]+) p_y=(?P<p_y>[-+0-9.eE]+) "
    r"p_z=(?P<p_z>[-+0-9.eE]+) consecutive=(?P<consecutive>[0-9]+)"
)
CONSENSUS_RE = re.compile(
    r"\[LONG-GAP-RECOVERY\]: event=consensus_pass epoch=(?P<epoch>[0-9]+) "
    r"timestamp=(?P<timestamp>[-+0-9.eE]+) radius_m=(?P<radius_m>[-+0-9.eE]+) "
    r"samples=(?P<samples>[0-9]+)"
)


TrialError = common.TrialError
NoRowsError = common.NoRowsError
KAIST_VALIDATION_ERRORS = (
    TrialError,
    rotation.TrialError,
    pairing.CensusError,
    metric_core.PairEvaluationError,
)


def geometry_topics(system: str) -> Tuple[str, ...]:
    if system not in PERTURBATION_SYSTEMS:
        raise TrialError("unknown system: {}".format(system))
    namespace = str(CANONICAL_INPUTS[system]["namespace"])
    return tuple(
        namespace + suffix
        for suffix in ("/poseimu", "/points_slam", "/points_msckf", "/points_aruco", "/loop_feats")
    )


def initial_artifacts(system: str) -> Dict[str, Any]:
    value = common._initial_artifacts()
    value["raw_geometry"]["topics"] = list(geometry_topics(system))
    return value


def refresh_artifacts(run_dir: Path, system: str) -> Dict[str, Any]:
    value = common.refresh_artifacts(run_dir)
    value["raw_geometry"]["topics"] = list(geometry_topics(system))
    return value


def _same_content_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("path", "size_bytes", "sha256"))


def _config_dependencies(config: Path) -> Dict[str, Dict[str, Any]]:
    mapping, _ = common._opencv_yaml_mapping(config)
    result: Dict[str, Dict[str, Any]] = {}
    for key in ("relative_config_imu", "relative_config_imucam"):
        relative = mapping.get(key)
        if not isinstance(relative, str) or not relative:
            raise TrialError("KAIST config lacks {}".format(key))
        path = common._regular_file(config.parent / relative, key)
        identity = common.file_identity(path)
        result[identity["path"]] = identity
    if mapping.get("use_mask") is True:
        for key in ("mask0", "mask1"):
            relative = mapping.get(key)
            if not isinstance(relative, str) or not relative:
                raise TrialError("enabled mask lacks {}".format(key))
            path = common._regular_file(config.parent / relative, key)
            identity = common.file_identity(path)
            result[identity["path"]] = identity
    return result


def validate_config_contract(system: str, config: Path) -> Dict[str, Any]:
    policy = CANONICAL_INPUTS[system]
    expected = Path(policy["config"]).resolve(strict=True)
    if config != expected:
        raise TrialError("{} config is not the canonical KAIST path".format(system))
    identity = common.file_identity(config)
    if identity["sha256"] != policy["config_sha256"]:
        raise TrialError("{} KAIST config SHA-256 drift".format(system))
    mapping, text = common._opencv_yaml_mapping(config)
    if mapping.get("use_stereo") is not True or mapping.get("max_cameras") != 2:
        raise TrialError("KAIST config no longer declares two-camera stereo")
    raw_track_frequency = mapping.get("track_frequency")
    if (
        isinstance(raw_track_frequency, bool)
        or not isinstance(raw_track_frequency, (int, float))
    ):
        raise TrialError("KAIST track_frequency must be numeric")
    track_frequency_hz = float(raw_track_frequency)
    if not math.isfinite(track_frequency_hz) or track_frequency_hz <= 0.0:
        raise TrialError("KAIST track_frequency must be finite and positive")
    # The stock U0 file keeps an inline comment on its OpenCV YAML directive;
    # inspect the one Boolean key lexically without normalizing either file.
    enabled = common._strict_boolean_key(text, "long_gap_recovery_enabled", False)
    if system == "U0":
        if "long_gap_recovery_enabled" in mapping or enabled:
            raise TrialError("original U0 config contains an S1 recovery option")
        selectors = [
            key
            for key in ("up_msckf_landmark_elimination", "up_msckf_max_visual_passes")
            if key in mapping
        ]
        if selectors:
            raise TrialError("original U0 config contains S1 updater selectors")
    else:
        if not enabled:
            raise TrialError("frozen S1 KAIST recovery is not enabled")
        if mapping.get("up_msckf_landmark_elimination") != "schur":
            raise TrialError("S1 config no longer selects Schur elimination")
        if mapping.get("up_msckf_max_visual_passes") != 1:
            raise TrialError("S1 config no longer selects one visual pass")
    dependencies = _config_dependencies(config)
    return {
        "system": system,
        "identity": identity,
        "native_unmodified_upstream": system == "U0",
        "recovery_enabled": enabled,
        "s1_frozen_c2": system == "S1",
        "n0_matched_nullspace_control": system == "N0",
        "n0_yaml_selector_shadowed_by_launch_parameter": (
            "up_msckf_landmark_elimination=nullspace" if system == "N0" else None
        ),
        "recovery_ablated_system": system in RECOVERY_ABLATED_SYSTEMS,
        "recovery_yaml_value_shadowed_by_launch_parameter": (
            RECOVERY_SWITCH_PARAMETER + "=false" if system in RECOVERY_ABLATED_SYSTEMS else None
        ),
        "recovery_effective_enabled": bool(enabled and system not in RECOVERY_ABLATED_SYSTEMS),
        "track_frequency_hz": track_frequency_hz,
        "track_frequency_source": "canonical_kaist_config",
        "dependencies": dependencies,
    }


def validate_launch_contract(system: str, launch: Path) -> Dict[str, Any]:
    policy = CANONICAL_INPUTS[system]
    expected = Path(policy["launch"]).resolve(strict=True)
    if launch != expected:
        raise TrialError("{} launch is not the canonical KAIST path".format(system))
    identity = common.file_identity(launch)
    if identity["sha256"] != policy["launch_sha256"]:
        raise TrialError("{} KAIST launch SHA-256 drift".format(system))
    try:
        root = ET.parse(str(launch)).getroot()
    except (ET.ParseError, OSError) as exc:
        raise TrialError("invalid KAIST launch XML") from exc
    arguments = [element.get("name") for element in root.findall("./arg")]
    expected_arguments = U0_LAUNCH_ARGUMENTS if system == "U0" else S1_LAUNCH_ARGUMENTS
    if None in arguments or set(arguments) != expected_arguments or len(arguments) != len(expected_arguments):
        raise TrialError("{} launch argument contract drift".format(system))
    nodes = root.findall("./node")
    if len(nodes) != 1:
        raise TrialError("KAIST launch must contain exactly one estimator node")
    node = nodes[0]
    if (
        node.get("name") != policy["node"]
        or node.get("pkg") != "ov_msckf"
        or node.get("type") != "ros1_serial_msckf"
        or node.get("clear_params") != "true"
        or node.get("required") != "true"
    ):
        raise TrialError("{} estimator node identity drift".format(system))
    parameter_names = [element.get("name") for element in node.findall("./param")]
    forbidden = {
        "path_gt",
        "initialize_with_gt",
        "ground_truth",
        "guardian",
        "restart",
    }
    if any(name in forbidden for name in parameter_names):
        raise TrialError("forbidden estimator binding in KAIST launch")
    if system == "U0" and any(
        name in ("kaist_vio_exact_header_stereo", "up_msckf_landmark_elimination", "long_gap_recovery_enabled")
        for name in parameter_names
    ):
        raise TrialError("U0 launch changes the original algorithm or delivery seam")
    landmark_elimination = None
    recovery_switch = None
    if system in S1_LIKE_SYSTEMS:
        values = [
            element.get("value")
            for element in node.findall("./param")
            if element.get("name") == "up_msckf_landmark_elimination"
        ]
        if values != [LANDMARK_ELIMINATION[system]]:
            raise TrialError(
                "{} launch does not select up_msckf_landmark_elimination={}".format(
                    system, LANDMARK_ELIMINATION[system]
                )
            )
        landmark_elimination = values[0]
        switches = [
            (element.get("type"), element.get("value"))
            for element in node.findall("./param")
            if element.get("name") == RECOVERY_SWITCH_PARAMETER
        ]
        if system in RECOVERY_ABLATED_SYSTEMS:
            if switches != [("bool", "false")]:
                raise TrialError(
                    "{} launch does not set exactly one {}=false".format(
                        system, RECOVERY_SWITCH_PARAMETER
                    )
                )
            recovery_switch = "false"
        elif switches:
            raise TrialError("{} launch must not bind {}".format(system, RECOVERY_SWITCH_PARAMETER))
    return {
        "identity": identity,
        "node_name": policy["node"],
        "namespace": policy["namespace"],
        "argument_names": sorted(arguments),
        "parameter_names": sorted(str(name) for name in parameter_names),
        "u0_native_record_time_pairing": system == "U0",
        "s1_exact_header_pairing": system in S1_LIKE_SYSTEMS,
        "launch_landmark_elimination": landmark_elimination,
        "n0_matched_nullspace_control": system == "N0",
        "recovery_ablated_system": system in RECOVERY_ABLATED_SYSTEMS,
        "launch_recovery_switch": recovery_switch,
        "recon_counterpart_system": RECOVERY_ABLATED_SYSTEMS.get(system),
        "ground_truth_bindings_absent": True,
    }


def validate_canonical_campaign(protocol: Path, matrix: Path) -> Dict[str, Any]:
    if protocol != CANONICAL_PROTOCOL.resolve(strict=True):
        raise TrialError("protocol is not the canonical CDSC-1R4 path")
    if matrix != CANONICAL_MATRIX.resolve(strict=True):
        raise TrialError("matrix is not the canonical CDSC-1R4 path")
    return {"protocol": common.file_identity(protocol), "matrix": common.file_identity(matrix)}


def launch_arguments(
    system: str,
    launch: Path,
    config: Path,
    bag: Path,
    bag_start: float,
    bag_duration: float,
    run_dir: Path,
) -> List[str]:
    if bag_duration != -1.0:
        raise TrialError("CDSC-1R4 KAIST must run from the frozen start to bag end")
    paths = {
        "state": run_dir / "trajectory" / "state_estimate.txt",
        "std": run_dir / "trajectory" / "state_deviation.txt",
        "time": run_dir / "diagnostics" / "timing_openvins.csv",
    }
    if system == "U0":
        return [
            str(launch),
            "config_path:=" + str(config),
            "bag:=" + str(bag),
            "bag_start:=" + format(bag_start, ".17g"),
            "bag_durr:=-1",
            "path_state:=" + str(paths["state"]),
            "path_std:=" + str(paths["std"]),
            "path_time:=" + str(paths["time"]),
            "record_timing:=true",
        ]
    return [
        str(launch),
        "bag:=" + str(bag),
        "candidate_config:=" + str(config),
        "bag_start:=" + format(bag_start, ".17g"),
        "path_state:=" + str(paths["state"]),
        "path_std:=" + str(paths["std"]),
        "path_time:=" + str(paths["time"]),
        "verbosity:=INFO",
    ]


def _expected_resolved_parameters(
    system: str, config: Path, bag: Path, bag_start: float, run_dir: Path
) -> Dict[str, Any]:
    namespace = str(CANONICAL_INPUTS[system]["namespace"]) + "/"
    common_values: Dict[str, Any] = {
        "path_bag": str(bag),
        "bag_start": bag_start,
        "bag_durr": -1.0,
        "config_path": str(config),
        "cam0_rostopic": CAMERA0_TOPIC,
        "cam1_rostopic": CAMERA1_TOPIC,
        "imu0_rostopic": IMU_TOPIC,
        "save_total_state": True,
        "filepath_est": str(run_dir / "trajectory" / "state_estimate.txt"),
        "filepath_std": str(run_dir / "trajectory" / "state_deviation.txt"),
        "record_timing_information": True,
        "record_timing_filepath": str(run_dir / "diagnostics" / "timing_openvins.csv"),
    }
    if system in S1_LIKE_SYSTEMS:
        common_values.update(
            {
                "verbosity": "INFO",
                "use_fej": True,
                "use_stereo": True,
                "max_cameras": 2,
                "cam0_distortion_model": "radtan",
                "cam1_distortion_model": "radtan",
                "feat_rep_msckf": "GLOBAL_3D",
                "up_msckf_landmark_elimination": LANDMARK_ELIMINATION[system],
                "up_msckf_max_visual_passes": 1,
                "calib_cam_extrinsics": False,
                "calib_cam_intrinsics": False,
                "calib_cam_timeoffset": False,
                "calib_imu_intrinsics": False,
                "calib_imu_g_sensitivity": False,
                "num_opencv_threads": 0,
                "multi_threading_pubs": False,
                "multi_threading_subs": False,
                "kaist_vio_exact_header_stereo": True,
            }
        )
        if system in RECOVERY_ABLATED_SYSTEMS:
            common_values[RECOVERY_SWITCH_PARAMETER] = False
    return {namespace + key: value for key, value in common_values.items()}


def validate_resolved_parameters(
    system: str,
    raw: str,
    config: Path,
    bag: Path,
    bag_start: float,
    run_dir: Path,
) -> Dict[str, Any]:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TrialError("resolved ROS parameter dump is invalid YAML") from exc
    if not isinstance(value, dict):
        raise TrialError("resolved ROS parameters are not a mapping")
    path_suffixes = {"config_path", "path_bag", "filepath_est", "filepath_std", "record_timing_filepath"}
    normalized: Dict[str, Any] = {}
    for key, observed in value.items():
        name = str(key)
        suffix = name.rsplit("/", 1)[-1]
        if suffix in path_suffixes:
            if not isinstance(observed, str):
                raise TrialError("resolved path parameter is not a string: {}".format(name))
            observed = str(Path(observed).resolve(strict=False))
        normalized[name] = observed
    expected = _expected_resolved_parameters(system, config, bag, bag_start, run_dir)
    if normalized != expected:
        missing = sorted(set(expected) - set(normalized))
        extra = sorted(set(normalized) - set(expected))
        changed = {
            key: {"observed": normalized[key], "expected": expected[key]}
            for key in sorted(set(normalized).intersection(expected))
            if normalized[key] != expected[key]
        }
        raise TrialError(
            "resolved KAIST parameter contract drift: missing={} extra={} changed={}".format(
                missing, extra, changed
            )
        )
    return {
        "parameter_count": len(normalized),
        "exact_typed_map_match": True,
        "parameters": normalized,
        "ground_truth_parameters_absent": True,
        "u0_original_native_seam": system == "U0",
        "s1_frozen_exact_header_c2_seam": system in S1_LIKE_SYSTEMS,
        "landmark_elimination": (
            LANDMARK_ELIMINATION.get(system) if system in S1_LIKE_SYSTEMS else "upstream_native"
        ),
        "recovery_switch_resolved": (
            normalized.get(str(CANONICAL_INPUTS[system]["namespace"]) + "/" + RECOVERY_SWITCH_PARAMETER)
            if system in RECOVERY_ABLATED_SYSTEMS
            else "ABSENT_YAML_GOVERNS"
        ),
    }


def _selected_pairs(
    system: str, messages: Sequence[pairing.FilteredMessage]
) -> Sequence[pairing.StereoPair]:
    if system == "U0":
        return pairing.select_upstream_native(messages).pairs
    return pairing.select_exact_header(messages).pairs


def _gap_records(timestamps_ns: Sequence[int]) -> List[Dict[str, float]]:
    return [
        {
            "start_timestamp_ns": left,
            "end_timestamp_ns": right,
            "start_timestamp_s": pairing._cpp_ros_time_to_sec(left),
            "end_timestamp_s": pairing._cpp_ros_time_to_sec(right),
            "duration_s": (
                pairing._cpp_ros_time_to_sec(right)
                - pairing._cpp_ros_time_to_sec(left)
            ),
        }
        for left, right in zip(timestamps_ns, timestamps_ns[1:])
        if (
            pairing._cpp_ros_time_to_sec(right)
            - pairing._cpp_ros_time_to_sec(left)
            > common.MAXIMUM_STATE_GAP_SECONDS
        )
    ]


def kaist_pair_census(
    system: str,
    bag: Path,
    bag_start: float,
    bag_duration: float,
    track_frequency_hz: float,
    perturbation: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Return normalized CDSC evidence, the full KAIST census, and bag identity."""

    if bag_duration != -1.0:
        raise TrialError("CDSC-1R4 KAIST census requires the complete adapted bag")
    if bag_start != 0.0 and not perturbation:
        raise TrialError("CDSC-1R4 KAIST census requires the complete adapted bag")
    messages, bag_identity_raw, topic_identity = pairing.read_bag(
        bag, CAMERA0_TOPIC, CAMERA1_TOPIC, IMU_TOPIC
    )
    perturbation_view: Optional[Dict[str, Any]] = None
    if perturbation:
        # PERTURB-1: project the exact integer-nanosecond C++ rosbag::View
        # [begin + bag_start, end] over the all-topic index bounds before any
        # selector runs, mirroring ros1_serial_msckf (view.addQuery(bag,
        # time_init, time_finish) plus the explicit < time_init skip).  Both
        # native and exact-header selectors and the runtime SERIAL-KAIST
        # summaries operate on that sub-view.
        try:
            import rosbag  # type: ignore
        except ImportError as exc:  # pragma: no cover - environment guard
            raise TrialError("ROS1 rosbag Python bindings are unavailable") from exc
        with rosbag.Bag(str(bag), "r") as opened:
            full_bounds = common.rosbag_view_full_bounds(opened)
        full_start_ns = int(full_bounds["first_record_timestamp_ns"])
        full_end_ns = int(full_bounds["last_record_timestamp_ns"])
        view_start_ns = full_start_ns + common._duration_nanoseconds(bag_start, "bag start")
        view_end_ns = full_end_ns
        if view_end_ns <= view_start_ns:
            raise TrialError("perturbed KAIST bag view is empty")
        all_messages = list(messages)
        messages = [
            message
            for message in all_messages
            if view_start_ns <= message.record_time_ns <= view_end_ns
        ]
        if not messages:
            raise TrialError("perturbed KAIST bag view has no sensor messages")
        perturbation_view = {
            "bag_start_seconds": bag_start,
            "bag_start_nanoseconds_ros_rounding": common._duration_nanoseconds(
                bag_start, "bag start"
            ),
            "full_bounds": full_bounds,
            "view_start_record_timestamp_ns": view_start_ns,
            "view_end_record_timestamp_ns": view_end_ns,
            "full_filtered_message_count": len(all_messages),
            "view_filtered_message_count": len(messages),
            "excluded_leading_message_count": len(all_messages) - len(messages),
            "excluded_leading_camera0_count": sum(
                1
                for message in all_messages
                if message.record_time_ns < view_start_ns and message.kind == pairing.KIND_CAMERA0
            ),
            "excluded_leading_camera1_count": sum(
                1
                for message in all_messages
                if message.record_time_ns < view_start_ns and message.kind == pairing.KIND_CAMERA1
            ),
            "excluded_leading_imu_count": sum(
                1
                for message in all_messages
                if message.record_time_ns < view_start_ns and message.kind == pairing.KIND_IMU
            ),
            "semantics": "cpp_rosbag_view_all_topic_begin_plus_bag_start_inclusive_to_end",
        }
        bag_identity_raw = dict(bag_identity_raw)
        bag_identity_raw["first_filtered_record_stamp_ns"] = messages[0].record_time_ns
        bag_identity_raw["last_filtered_record_stamp_ns"] = messages[-1].record_time_ns
        bag_identity_raw["filtered_message_count"] = len(messages)
    full = dict(pairing.build_census(messages, bag_identity_raw, topic_identity))
    if perturbation_view is not None:
        full["perturbation_view"] = perturbation_view
    raw_selected = tuple(_selected_pairs(system, messages))
    if not raw_selected:
        raise TrialError("KAIST selected stereo stream is empty")
    accepted, visualizer_gate = common.apply_visualizer_track_frequency_gate(
        raw_selected, track_frequency_hz
    )
    timestamps_ns = [int(item.camera_timestamp_ns) for item in accepted]
    selector_source = (
        "upstream_native_record_time_first_forward_stereo"
        if system == "U0"
        else "frozen_s1_exact_header_stereo"
    )
    source = selector_source + "_plus_stock_visualizer_frequency_gate"
    interval = {
        "source": source,
        "selector_source": selector_source,
        "first_selected_input_timestamp_s": pairing._cpp_ros_time_to_sec(
            timestamps_ns[0]
        ),
        "last_selected_input_timestamp_s": pairing._cpp_ros_time_to_sec(
            timestamps_ns[-1]
        ),
        "first_selected_input_timestamp_ns": timestamps_ns[0],
        "last_selected_input_timestamp_ns": timestamps_ns[-1],
        "selected_pair_count": len(timestamps_ns),
        "raw_serial_dispatch_pair_count": len(raw_selected),
        "visualizer_frequency_dropped_pair_count": visualizer_gate[
            "frequency_dropped_dispatch_count"
        ],
        "bag_view_start_record_timestamp_s": int(
            bag_identity_raw["first_filtered_record_stamp_ns"]
        )
        / 1.0e9,
        "bag_view_end_record_timestamp_s": int(
            bag_identity_raw["last_filtered_record_stamp_ns"]
        )
        / 1.0e9,
        "gaps_over_threshold": _gap_records(timestamps_ns),
    }
    normalized = {
        "schema": KAIST_CENSUS_SCHEMA,
        "system": system,
        "delivery": source,
        "visualizer_track_frequency_gate": visualizer_gate,
        "input_interval": interval,
        "static_census_schema": full.get("schema"),
        "static_census": full.get("census"),
        "selection_bounds": full.get("selection_bounds"),
        "pair_sets": full.get("pair_sets"),
        "u0_native_diagnostics": full.get("u0_native_diagnostics"),
    }
    if perturbation_view is not None:
        normalized["perturbation_view"] = perturbation_view
    bag_identity = {
        "path": str(Path(str(bag_identity_raw["path"])).resolve(strict=True)),
        "size_bytes": int(bag_identity_raw["size_bytes"]),
        "sha256": str(bag_identity_raw["sha256"]),
        "mtime_ns": bag.stat().st_mtime_ns,
        "executable": False,
    }
    return normalized, full, bag_identity


def validate_s1_visualizer_gate_runtime_binding(
    summaries: Mapping[str, Any], census: Mapping[str, Any]
) -> Dict[str, Any]:
    """Bind S1's runtime queue counters to the projected 31 Hz population."""

    if census.get("schema") != KAIST_CENSUS_SCHEMA or census.get("system") not in S1_LIKE_SYSTEMS:
        raise TrialError("S1 visualizer gate binding received the wrong census")
    interval = census.get("input_interval")
    enqueue_record = summaries.get("camera_enqueue")
    enqueue = (
        enqueue_record.get("counts")
        if isinstance(enqueue_record, Mapping)
        else None
    )
    if not isinstance(interval, Mapping) or not isinstance(enqueue, Mapping):
        raise TrialError("S1 visualizer gate binding lacks interval/runtime counts")
    expected = {
        "queued_pairs": interval.get("selected_pair_count"),
        "processed_pairs": interval.get("selected_pair_count"),
        "frequency_thinned_pairs": interval.get(
            "visualizer_frequency_dropped_pair_count"
        ),
        "raw_exact_header_pairs": interval.get("raw_serial_dispatch_pair_count"),
    }
    runtime_counts = {
        "queued_pairs": enqueue.get("queued_pairs"),
        "processed_pairs": enqueue.get("processed_pairs"),
        "frequency_thinned_pairs": enqueue.get("frequency_thinned_pairs"),
    }
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (*expected.values(), *runtime_counts.values())
    ):
        raise TrialError("S1 visualizer gate binding counts are malformed")
    observed = {
        **runtime_counts,
        "raw_exact_header_pairs": (
            runtime_counts["queued_pairs"]
            + runtime_counts["frequency_thinned_pairs"]
        ),
    }
    mismatches = {
        key: {"runtime": observed[key], "projected": value}
        for key, value in expected.items()
        if observed[key] != value
    }
    if mismatches:
        raise TrialError(
            "S1 runtime/projected visualizer gate mismatch: {}".format(mismatches)
        )
    return {
        "status": "PASS",
        "runtime_matches_projected_visualizer_gate": True,
        "accepted_input": {
            "first_header_stamp_ns": interval.get(
                "first_selected_input_timestamp_ns"
            ),
            "last_header_stamp_ns": interval.get(
                "last_selected_input_timestamp_ns"
            ),
            "callback_count": interval.get("selected_pair_count"),
            "ordered_callback_sequence_sha256": census.get(
                "visualizer_track_frequency_gate", {}
            ).get("accepted_visualizer_callback_sequence_sha256"),
        },
        "projected": expected,
        "runtime": observed,
    }


def _parse_s1_early_selector_summary(console_text: str) -> Dict[str, Any]:
    """Parse the pre-loop exact-header summary independently of terminal output."""

    clean_lines = [rotation.ANSI_RE.sub("", line) for line in console_text.splitlines()]
    lines = [
        line for line in clean_lines if rotation.SERIAL_SUMMARY_PREFIX in line
    ]
    if not lines:
        return {"status": "ABSENT", "line": None, "counts": None}
    if len(lines) != 1:
        raise TrialError(
            "expected at most one early exact-header summary; found {}".format(
                len(lines)
            )
        )
    match = rotation.SERIAL_SUMMARY_RE.fullmatch(lines[0])
    if match is None:
        raise TrialError("malformed early SERIAL-KAIST exact-header summary")
    return {
        "status": "AVAILABLE",
        "line": lines[0],
        "counts": {key: int(value) for key, value in match.groupdict().items()},
    }


def _bind_s1_early_selector_summary(
    early: Mapping[str, Any], full_census: Mapping[str, Any]
) -> Dict[str, Any]:
    if early.get("status") != "AVAILABLE" or not isinstance(
        early.get("counts"), Mapping
    ):
        raise TrialError("early exact-header summary is unavailable")
    if full_census.get("schema") != pairing.SCHEMA or not isinstance(
        full_census.get("census"), Mapping
    ):
        raise TrialError("early exact-header binding received an invalid census")
    observed = early["counts"]
    static = full_census["census"]
    expected = {
        "exact_header_pairs": static.get("s1_exact_pair_count"),
        "camera0_without_match": static.get("camera0_unmatched_count"),
        "camera1_without_match": static.get("camera1_unmatched_count"),
    }
    mismatches = {
        key: {"runtime": observed.get(key), "static": value}
        for key, value in expected.items()
        if observed.get(key) != value
    }
    if mismatches:
        raise TrialError(
            "runtime/static early exact-header mismatch: {}".format(mismatches)
        )
    return {
        "status": "PASS",
        "runtime_matches_static_census": True,
        "static_expected": expected,
        "runtime_observed": dict(observed),
    }


def assess_s1_pairing_runtime(
    console_text: str,
    full_census: Mapping[str, Any],
    normalized_census: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate early selector evidence and, when present, terminal gate evidence."""

    early = _parse_s1_early_selector_summary(console_text)
    terminal_observed = rotation.ENQUEUE_SUMMARY_PREFIX in console_text
    early_binding = (
        _bind_s1_early_selector_summary(early, full_census)
        if early["status"] == "AVAILABLE"
        else None
    )
    if terminal_observed:
        # The terminal summary is meaningful only as one complete, valid pair
        # with the early selector summary.  The shared parser also rejects
        # malformed/duplicate/decode/pending/accounting defects.
        summaries = rotation.parse_runtime_summaries(console_text)
        clean_lines = [
            rotation.ANSI_RE.sub("", line) for line in console_text.splitlines()
        ]
        early_index = next(
            index
            for index, line in enumerate(clean_lines)
            if rotation.SERIAL_SUMMARY_PREFIX in line
        )
        terminal_index = next(
            index
            for index, line in enumerate(clean_lines)
            if rotation.ENQUEUE_SUMMARY_PREFIX in line
        )
        if terminal_index <= early_index:
            raise TrialError("terminal camera-enqueue summary precedes early selector summary")
        raw_binding = rotation.bind_pairing_census(summaries, full_census)
        gate_binding = validate_s1_visualizer_gate_runtime_binding(
            summaries, normalized_census
        )
        if (
            raw_binding.get("status") != "AVAILABLE"
            or gate_binding.get("status") != "PASS"
        ):
            raise TrialError("terminal S1 pairing runtime binding is unavailable")
        return {
            "status": "AVAILABLE",
            "summaries": summaries,
            "early_selector_binding": early_binding,
            "raw_selector_binding": raw_binding,
            "visualizer_gate_binding": gate_binding,
            "early_selector_evidence_observed": True,
            "terminal_enqueue_evidence_observed": True,
        }
    return {
        "status": "TERMINAL_UNAVAILABLE",
        "reason": "terminal_enqueue_summary_absent",
        "static_census_retained": True,
        "early_selector": early,
        "early_selector_binding": early_binding,
        "early_selector_evidence_observed": early["status"] == "AVAILABLE",
        "terminal_enqueue_evidence_observed": False,
    }


def _s1_expected_child_termination_status(child: Mapping[str, Any]) -> str:
    terminations = child.get("required_terminations")
    if not isinstance(terminations, list):
        return "UNPROVEN"
    matches = [
        record
        for record in terminations
        if isinstance(record, Mapping)
        and str(record.get("node", "")).split("-", 1)[0]
        == "kaist_vio_turnsafe_baseline"
    ]
    if len(matches) != 1 or matches[0].get("status") not in ("CLEAN", "FAILED"):
        return "UNPROVEN"
    return str(matches[0]["status"])


def _s1_post_selector_evidence_observed(
    console_text: str,
    state_kind: Optional[str],
    numeric_integrity_valid: bool,
    input_decode_valid: Optional[bool],
) -> bool:
    """Exclude the constructor-time recovery contract from post-selector evidence."""

    clean_lines = [rotation.ANSI_RE.sub("", line) for line in console_text.splitlines()]
    recovery_lines = [
        line for line in clean_lines if rotation.RECOVERY_PREFIX in line
    ]
    if recovery_lines and (
        len(recovery_lines) != 1
        or rotation.RECOVERY_CONTRACT_RE.fullmatch(recovery_lines[0]) is None
    ):
        return True
    return bool(
        any("[SERIAL-KAIST]:" in line for line in clean_lines)
        or state_kind not in (None, "missing", "empty")
        or numeric_integrity_valid is False
        or input_decode_valid is False
    )


def finalize_s1_pairing_runtime_contract(
    current_valid: bool,
    pairing_runtime: Mapping[str, Any],
    child: Mapping[str, Any],
    launch_record: Optional[Mapping[str, Any]],
    estimator_group_closed: bool,
    state_kind: Optional[str],
    post_selector_evidence_observed: bool,
) -> Dict[str, Any]:
    """Apply the exact terminal-summary/native-termination truth table."""

    if not current_valid:
        attempted = launch_record is not None
        timed_out = bool(launch_record and launch_record.get("timed_out"))
        interrupted = bool(launch_record and launch_record.get("interrupted"))
        return {
            "runtime_contract_valid": False,
            "reason": "PAIRING_RUNTIME_EVIDENCE_INVALID",
            "estimator_attempted": attempted,
            "estimator_process_group_closed": estimator_group_closed,
            "expected_child_termination_status": _s1_expected_child_termination_status(
                child
            ),
            "timed_out": timed_out,
            "interrupted": interrupted,
            "post_selector_evidence_observed": post_selector_evidence_observed,
        }
    attempted = launch_record is not None
    timed_out = bool(launch_record and launch_record.get("timed_out"))
    interrupted = bool(launch_record and launch_record.get("interrupted"))
    terminal = pairing_runtime.get("terminal_enqueue_evidence_observed") is True
    early = pairing_runtime.get("early_selector_evidence_observed") is True
    child_status = _s1_expected_child_termination_status(child)
    if timed_out or interrupted:
        valid = bool(attempted and (early or pairing_runtime.get("status") == "TERMINAL_UNAVAILABLE"))
        reason = "TIMEOUT_OR_INTERRUPTION_POLICY" if valid else "UNBOUND_TIMEOUT_OR_INTERRUPTION"
    elif terminal:
        valid = bool(early and child_status in ("CLEAN", "FAILED"))
        reason = (
            "TERMINAL_BOUND_CHILD_{}".format(child_status)
            if valid
            else "TERMINAL_BOUND_CHILD_OUTCOME_UNPROVEN"
        )
    elif early:
        valid = bool(attempted and estimator_group_closed and child_status == "FAILED")
        reason = (
            "VALID_EARLY_ONLY_REQUIRED_CHILD_FAILED"
            if valid
            else "EARLY_ONLY_WITHOUT_EXACT_REQUIRED_CHILD_FAILURE"
        )
    else:
        zero_post_selector_evidence = bool(
            state_kind in (None, "missing", "empty")
            and not post_selector_evidence_observed
        )
        valid = bool(
            attempted
            and estimator_group_closed
            and child_status == "FAILED"
            and zero_post_selector_evidence
        )
        reason = (
            "PRE_SELECTOR_REQUIRED_CHILD_FAILED"
            if valid
            else "ABSENT_SUMMARIES_WITHOUT_PROVEN_PRE_SELECTOR_FAILURE"
        )
    return {
        "runtime_contract_valid": valid,
        "reason": reason,
        "estimator_attempted": attempted,
        "estimator_process_group_closed": estimator_group_closed,
        "expected_child_termination_status": child_status,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "terminal_enqueue_evidence_observed": terminal,
        "early_selector_evidence_observed": early,
        "post_selector_evidence_observed": post_selector_evidence_observed,
    }


def _clean_recovery_lines(text: str) -> Tuple[List[str], List[str]]:
    clean = [rotation.ANSI_RE.sub("", line) for line in text.splitlines()]
    lines = [line for line in clean if rotation.RECOVERY_PREFIX in line]
    names: List[str] = []
    for line in lines:
        match = re.search(r"\bevent=([a-z0-9_]+)(?:\s|$)", line)
        if match is None:
            raise TrialError("recovery event line has no event name")
        names.append(match.group(1))
    return lines, names


def _exact_event_lines(
    lines: Sequence[str], names: Sequence[str], event: str, count: int
) -> List[str]:
    result = [line for line, name in zip(lines, names) if name == event]
    if len(result) != count:
        raise TrialError(
            "C2 requires {} {} event(s), observed {}".format(count, event, len(result))
        )
    return result


def validate_c2_detail(text: str, runtime: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate the frozen accepted-attempt, consensus, and covariance gates."""

    if runtime.get("sequence_contract") != "EXACT_ONE_TARGET_RECOVERY":
        raise TrialError("C2 detail validation requires the exact-one target recovery")
    lines, names = _clean_recovery_lines(text)
    expected_counts = {
        "contract_validated": 1,
        "trigger": 1,
        "attempt": 3,
        "accepted_pose": 3,
        "consensus_pass": 1,
        "relocalization_commit": 1,
        "first_resumed_covariance": 1,
        "warmup_complete": 1,
        "summary": 1,
    }
    observed_counts = {name: names.count(name) for name in sorted(set(names))}
    if observed_counts != expected_counts:
        raise TrialError(
            "C2 event vector differs: observed={} expected={}".format(
                observed_counts, expected_counts
            )
        )

    attempts: List[Dict[str, Any]] = []
    for line in _exact_event_lines(lines, names, "attempt", 3):
        match = ATTEMPT_DETAIL_RE.fullmatch(line)
        if match is None:
            raise TrialError("malformed C2 accepted-attempt diagnostic")
        integers = {
            key: int(match.group(key))
            for key in (
                "epoch",
                "attempt",
                "imu_prediction",
                "accepted",
                "supplied",
                "valid",
                "inliers",
                "state_unchanged",
            )
        }
        floats = {
            key: float(match.group(key))
            for key in (
                "timestamp",
                "inlier_ratio",
                "max_reprojection_px",
                "min_depth",
                "span_x",
                "span_y",
                "support_ratio",
                "imu_angle_deg",
            )
        }
        if not all(math.isfinite(value) for value in floats.values()):
            raise TrialError("C2 accepted-attempt diagnostic is nonfinite")
        gates = {
            "epoch_zero": integers["epoch"] == 0,
            "imu_prediction_available": integers["imu_prediction"] == 1,
            "accepted": integers["accepted"] == 1 and match.group("reason") == "accepted",
            "supplied_correspondences_at_least_12": integers["supplied"] >= 12,
            "valid_correspondences_at_least_12": integers["valid"] >= 12,
            "inliers_at_least_12": integers["inliers"] >= 12,
            "inlier_ratio_at_least_0_70": floats["inlier_ratio"] >= 0.70,
            "reprojection_at_most_2_px": 0.0 <= floats["max_reprojection_px"] <= 2.0,
            "positive_depth": floats["min_depth"] > 0.0,
            "x_span_at_least_0_25": floats["span_x"] >= 0.25,
            "y_span_at_least_0_25": floats["span_y"] >= 0.25,
            "noncollinear_support": floats["support_ratio"] > 1.0e-6,
            "imu_orientation_at_most_5_deg": 0.0 <= floats["imu_angle_deg"] <= 5.0,
            "state_unchanged": integers["state_unchanged"] == 1,
        }
        if not all(gates.values()):
            raise TrialError(
                "C2 accepted-attempt gate failed: {}".format(
                    sorted(name for name, passed in gates.items() if not passed)
                )
            )
        attempts.append(
            {
                **integers,
                **floats,
                "reason": match.group("reason"),
                "gates": gates,
            }
        )
    if [item["attempt"] for item in attempts] != [1, 2, 3]:
        raise TrialError("C2 accepted attempts are not exactly 1,2,3")
    if any(
        right["timestamp"] <= left["timestamp"]
        for left, right in zip(attempts, attempts[1:])
    ):
        raise TrialError("C2 accepted-attempt timestamps do not increase")

    accepted_poses: List[Dict[str, Any]] = []
    for line in _exact_event_lines(lines, names, "accepted_pose", 3):
        match = ACCEPTED_POSE_RE.fullmatch(line)
        if match is None:
            raise TrialError("malformed C2 accepted-pose diagnostic")
        item = {
            "epoch": int(match.group("epoch")),
            "attempt": int(match.group("attempt")),
            "timestamp": float(match.group("timestamp")),
            "position_xyz_m": [
                float(match.group("p_x")),
                float(match.group("p_y")),
                float(match.group("p_z")),
            ],
            "consecutive": int(match.group("consecutive")),
        }
        if not all(
            math.isfinite(value)
            for value in (item["timestamp"], *item["position_xyz_m"])
        ):
            raise TrialError("C2 accepted pose is nonfinite")
        accepted_poses.append(item)
    for index, (attempt, pose) in enumerate(zip(attempts, accepted_poses), 1):
        if (
            pose["epoch"] != 0
            or pose["attempt"] != index
            or pose["consecutive"] != index
            or abs(pose["timestamp"] - attempt["timestamp"]) > 1.0e-6
        ):
            raise TrialError("C2 accepted pose does not bind its accepted attempt")

    consensus_line = _exact_event_lines(lines, names, "consensus_pass", 1)[0]
    consensus_match = CONSENSUS_RE.fullmatch(consensus_line)
    if consensus_match is None:
        raise TrialError("malformed C2 consensus-pass diagnostic")
    consensus = {
        "epoch": int(consensus_match.group("epoch")),
        "timestamp": float(consensus_match.group("timestamp")),
        "radius_m": float(consensus_match.group("radius_m")),
        "samples": int(consensus_match.group("samples")),
    }
    if (
        consensus["epoch"] != 0
        or consensus["samples"] != 3
        or not math.isfinite(consensus["radius_m"])
        or not 0.0 <= consensus["radius_m"] <= 0.10
        or abs(consensus["timestamp"] - attempts[-1]["timestamp"]) > 1.0e-6
    ):
        raise TrialError("C2 three-pose stationary consensus gate failed")
    commit = runtime.get("commit")
    if not isinstance(commit, Mapping):
        raise TrialError("C2 parsed runtime lacks the commit")
    if abs(float(commit["timestamp"]) - attempts[-1]["timestamp"]) > 1.0e-6:
        raise TrialError("C2 commit is not bound to the third accepted attempt")
    if any(
        abs(float(left) - float(right)) > 1.0e-8
        for left, right in zip(commit["position_xyz_m"], accepted_poses[-1]["position_xyz_m"])
    ):
        raise TrialError("C2 commit pose differs from the consensus-selected pose")

    covariance = runtime.get("commit_covariance")
    if not isinstance(covariance, Mapping) or covariance.get("status") != "AVAILABLE":
        raise TrialError("C2 commit covariance is unavailable")
    matrix = np.asarray(covariance.get("position_covariance_row_major_m2"), dtype=float)
    if matrix.size != 9:
        raise TrialError("C2 commit covariance is not 3x3")
    matrix = matrix.reshape(3, 3)
    symmetry_error = float(np.max(np.abs(matrix - matrix.T)))
    eigenvalues = np.linalg.eigvalsh(0.5 * (matrix + matrix.T))
    if (
        not np.all(np.isfinite(matrix))
        or symmetry_error > 1.0e-12 * max(1.0, float(np.max(np.abs(matrix))))
        or not np.all(np.isfinite(eigenvalues))
        or float(np.min(eigenvalues)) <= 0.0
    ):
        raise TrialError("C2 commit covariance is not finite, symmetric, and SPD")
    return {
        "status": "PASS",
        "pass": True,
        "event_counts": observed_counts,
        "attempts": attempts,
        "accepted_poses": accepted_poses,
        "consensus": consensus,
        "commit_covariance": {
            "finite_symmetric_spd": True,
            "symmetry_max_absolute_error": symmetry_error,
            "eigenvalues_m2": [float(value) for value in eigenvalues],
        },
    }


def validate_recovery_gap_binding(
    runtime: Mapping[str, Any], input_interval: Mapping[str, Any]
) -> Dict[str, Any]:
    trigger = runtime.get("trigger")
    if not isinstance(trigger, Mapping):
        raise TrialError("target recovery lacks trigger evidence")
    gaps = input_interval.get("gaps_over_threshold")
    if not isinstance(gaps, list):
        raise TrialError("selected input gap list is malformed")
    frozen_duration = float(rotation.ROTATION_GAP["selected_stereo_gap_seconds"])
    candidates = [
        gap
        for gap in gaps
        if abs(float(gap["duration_s"]) - frozen_duration) <= 1.0e-6
    ]
    if len(candidates) != 1:
        raise TrialError("target recovery does not bind one frozen selected-input gap")
    gap = candidates[0]
    checks = {
        "selected_gap_duration": abs(float(gap["duration_s"]) - frozen_duration) <= 1.0e-6,
        "selected_gap_start": abs(
            float(gap["start_timestamp_s"])
            - int(rotation.ROTATION_GAP["last_pre_gap_selected_header_stamp_ns"]) / 1.0e9
        )
        <= 1.0e-6,
        "selected_gap_end": abs(
            float(gap["end_timestamp_s"])
            - int(rotation.ROTATION_GAP["first_post_gap_selected_header_stamp_ns"]) / 1.0e9
        )
        <= 1.0e-6,
        "trigger_at_first_post_gap_callback": abs(
            float(trigger["timestamp"]) - float(gap["end_timestamp_s"])
        )
        <= 1.0e-5,
        "trigger_gap_matches_selected_gap": abs(
            float(trigger["gap_s"]) - float(gap["duration_s"])
        )
        <= 1.0e-5,
    }
    if not all(checks.values()):
        raise TrialError(
            "C2 recovery/input gap binding failed: {}".format(
                sorted(name for name, passed in checks.items() if not passed)
            )
        )
    return {"status": "PASS", "pass": True, "gap": gap, "checks": checks}


def assess_kaist_output_coverage(
    system: str,
    sequence: str,
    input_interval: Mapping[str, Any],
    state: Mapping[str, Any],
    mechanism: Mapping[str, Any],
) -> Dict[str, Any]:
    """Apply the common passage rule plus C2's declared recovery-gap seam."""

    result = common.assess_output_coverage(input_interval, state)
    result["recovery_supported_state_gap_count"] = 0
    result["recovery_supported_state_gaps"] = []
    if system not in RECOVERY_BOUND_SYSTEMS or sequence != "rotation/rotation.bag":
        return result
    if mechanism.get("pass") is not True:
        return result
    timing = mechanism.get("timing")
    runtime = mechanism.get("runtime")
    covariance = runtime.get("commit_covariance") if isinstance(runtime, Mapping) else None
    if (
        not isinstance(timing, Mapping)
        or timing.get("timing_contract")
        != "TARGET_COMMIT_PLUS_FOUR_PROPAGATE_ONLY_ROWS"
        or not isinstance(covariance, Mapping)
    ):
        return result
    unsupported = result.get("unsupported_state_gaps")
    if not isinstance(unsupported, list) or len(unsupported) != 1:
        return result
    gap = unsupported[0]
    expected_start = float(rotation.ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
    expected_end = float(covariance.get("state_output_timestamp", math.nan))
    checks = {
        "last_pre_gap_state": abs(float(gap["start_timestamp_s"]) - expected_start)
        <= 1.0e-5,
        "first_resumed_commit_state": abs(float(gap["end_timestamp_s"]) - expected_end)
        <= 1.0e-5,
        "duration_closes": abs(
            float(gap["duration_s"])
            - (float(gap["end_timestamp_s"]) - float(gap["start_timestamp_s"]))
        )
        <= 1.0e-6,
        "runtime_gap_bound": isinstance(mechanism.get("gap_binding"), Mapping)
        and mechanism["gap_binding"].get("pass") is True,
        "five_event_bound_timing_omissions": timing.get("consistent") is True,
    }
    if not all(checks.values()):
        return result
    evidence = {"state_gap": gap, "checks": checks}
    result["unsupported_state_gaps"] = []
    result["supported_input_gap_count"] = int(result["supported_input_gap_count"]) + 1
    result["recovery_supported_state_gap_count"] = 1
    result["recovery_supported_state_gaps"] = [evidence]
    result["maximum_state_gap_pass"] = True
    result["pass"] = bool(result["tail_gap_pass"])
    return result


def _start_geometry_recorder(
    run_dir: Path, environment: Mapping[str, str], system: str
) -> common.ManagedProcess:
    recorder_name = "icra27_cdsc1r4_kaist_geometry_recorder"
    recorder = common.start_managed_process(
        "geometry_recorder",
        [
            str(common._which("rosbag")),
            "record",
            "--buffsize=0",
            "--chunksize=768",
            "-O",
            str(run_dir / "geometry" / "feature_stream.bag"),
            *geometry_topics(system),
            "__name:=" + recorder_name,
        ],
        run_dir / "diagnostics" / "geometry_recorder.log",
        environment,
    )
    try:
        deadline = time.monotonic() + 15.0
        rosnode = common._which("rosnode")
        while time.monotonic() < deadline:
            if recorder.process.poll() is not None:
                raise TrialError("geometry recorder exited during startup")
            probe = subprocess.run(
                [str(rosnode), "info", "/" + recorder_name],
                env=dict(environment),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                return recorder
            time.sleep(0.1)
        raise TrialError("geometry recorder did not become ready")
    except BaseException:
        common.finish_managed_process(recorder)
        raise


def _capture_bag_summary(path: Path) -> Dict[str, Any]:
    """Read only rosbag-info YAML already produced by the capture postflight."""

    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TrialError("capture rosbag info is invalid") from exc
    if not isinstance(value, dict):
        raise TrialError("capture rosbag info is not a mapping")
    message_count = value.get("messages")
    if isinstance(message_count, bool) or not isinstance(message_count, int) or message_count < 0:
        raise TrialError("capture rosbag message count is invalid")
    topics = value.get("topics", [])
    if not isinstance(topics, list):
        raise TrialError("capture rosbag topic table is invalid")
    observed = {
        str(item.get("topic")): int(item.get("messages", 0))
        for item in topics
        if isinstance(item, dict) and isinstance(item.get("topic"), str)
    }
    return {
        "message_count": message_count,
        "observed_topic_message_counts": observed,
        "requested_topics": None,
        "map_exposure": "MAP_NOT_EXPOSED" if message_count == 0 else "SPARSE_GEOMETRY_EMITTED",
    }


def classify_outcome(facts: Mapping[str, Any]) -> str:
    """Preserve native algorithm failures before interpreting evidence defects."""

    if facts.get("interrupted"):
        return "INTERRUPTED"
    if facts.get("timed_out"):
        return "TIMED_OUT"
    if not facts.get("teardown_ok", True):
        return "TEARDOWN_FAILED"
    # A preflight, parameter-dump, runtime-identity, port, or launch-contract
    # failure is infrastructure, never evidence that an estimator failed to
    # initialize.  End-of-run summary absence after a proven estimator crash
    # does not clear runtime_contract_valid in run_trial, so real native
    # algorithm failures remain classifiable below.
    if not facts.get("runtime_contract_valid", False):
        return "INFRASTRUCTURE_FAILED"
    if not facts.get("numeric_integrity_valid", True):
        return "NUMERIC_FAILURE"
    state_kind = facts.get("state_kind")
    abnormal = bool(facts.get("launch_abnormal"))
    if state_kind in (None, "missing", "empty"):
        return "ESTIMATOR_CRASH" if abnormal else "NO_INITIALIZATION"
    if state_kind == "invalid":
        return "INVALID_OUTPUT"
    if not facts.get("outputs_valid", False):
        return "PARTIAL" if abnormal else "INVALID_OUTPUT"
    if not facts.get("input_decode_valid", True):
        return "TRACKING_LOSS"
    if not facts.get("continuity_pass", False):
        return "TRACKING_LOSS"
    if not facts.get("tail_pass", False):
        return "PARTIAL"
    exact_u0_teardown = bool(facts.get("exact_u0_teardown"))
    if abnormal and not exact_u0_teardown:
        return "PARTIAL"
    if facts.get("mode") == "capture":
        if not facts.get("capture_closed", False):
            return "CAPTURE_INCOMPLETE"
        if not facts.get("linkage_valid", False):
            return "INVALID_LINKAGE"
    return "COMPLETED_WITH_TEARDOWN_DEFECT" if exact_u0_teardown else "COMPLETED"


def _derived_tum_content_identity_from_state(path: Path) -> Dict[str, Any]:
    """Derive the converter's exact TUM bytes without publishing a file."""

    digest = hashlib.sha256()
    header = b"# timestamp tx ty tz qx qy qz qw\n"
    digest.update(header)
    size = len(header)
    rows = 0
    saw_header = False
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for raw in stream:
            stripped = raw.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                if stripped.startswith("# timestamp(s) q p v bg ba cam_imu_dt num_cam"):
                    saw_header = True
                continue
            fields = stripped.split()
            if len(fields) < 8:
                raise TrialError("historical state row has fewer than eight columns")
            encoded = (
                " ".join(
                    [
                        fields[0],
                        fields[5],
                        fields[6],
                        fields[7],
                        fields[1],
                        fields[2],
                        fields[3],
                        fields[4],
                    ]
                )
                + "\n"
            ).encode("ascii")
            digest.update(encoded)
            size += len(encoded)
            rows += 1
    if not saw_header or rows == 0:
        raise TrialError("historical state cannot supply a converter-compatible TUM identity")
    return {
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "rows": rows,
        "source": "derived from historical state with frozen openvins_to_tum byte contract",
    }


def _artifact_content(artifacts: Mapping[str, Any], name: str) -> Optional[Dict[str, Any]]:
    record = artifacts.get(name)
    identity = record.get("identity") if isinstance(record, Mapping) else None
    if not isinstance(identity, Mapping):
        return None
    return {
        "size_bytes": identity.get("size_bytes"),
        "sha256": identity.get("sha256"),
    }


def historical_determinism_check(
    system: str, sequence: str, artifacts: Mapping[str, Any]
) -> Dict[str, Any]:
    """Compare fresh bytes to compatible historical evidence, never gate the run."""

    expected: Dict[str, Any] = {}
    source: Dict[str, Any]
    if system == "S1":
        ledger = REPO_ROOT / "project" / "evidence" / "rotation_robustness" / "R2_ARTIFACT_INDEX.csv"
        ledger_identity = common.file_identity(common._regular_file(ledger, "S1 R2 artifact ledger"))
        with ledger.open(newline="", encoding="utf-8") as stream:
            matches = [row for row in csv.DictReader(stream) if row.get("sequence") == sequence]
        if len(matches) != 1:
            raise TrialError("S1 R2 ledger does not identify exactly one sequence")
        row = matches[0]
        expected = {
            "state": {"sha256": row["state_sha256"]},
            "deviation": {"sha256": row["deviation_sha256"]},
            "tum": {"sha256": row["tum_sha256"]},
        }
        source = {
            "kind": "S1_R2_LEDGER",
            "identity": ledger_identity,
            "scored_run_id": row.get("scored_run_id"),
            "scored_manifest_sha256": row.get("scored_manifest_sha256"),
        }
    else:
        sequence_id = Path(sequence).stem
        manifest_path = (
            Path("/home/moksh/schurvio-icra27-artifacts/g05/primary")
            / sequence_id
            / "U0"
            / "scored"
            / "sequence_result.json"
        )
        manifest_identity = common.file_identity(
            common._regular_file(manifest_path, "historical U0 G0.5 scored manifest")
        )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise TrialError("historical U0 manifest is invalid JSON") from exc
        if manifest.get("system") != "U0" or manifest.get("sequence") != sequence:
            raise TrialError("historical U0 manifest cell identity differs")
        outputs = manifest.get("outputs")
        if not isinstance(outputs, Mapping):
            raise TrialError("historical U0 manifest lacks outputs")
        for fresh_name, old_name in (("state", "state"), ("deviation", "deviation")):
            record = outputs.get(old_name)
            if not isinstance(record, Mapping):
                raise TrialError("historical U0 manifest lacks {}".format(old_name))
            path = common._regular_file(Path(str(record.get("path", ""))), "historical U0 output")
            live = common.file_identity(path)
            if live.get("sha256") != record.get("sha256") or live.get("size_bytes") != record.get("size_bytes"):
                raise TrialError("historical U0 {} identity drift".format(old_name))
            expected[fresh_name] = {
                "size_bytes": live["size_bytes"],
                "sha256": live["sha256"],
            }
        trajectory = outputs.get("trajectory")
        if isinstance(trajectory, Mapping):
            path = common._regular_file(
                Path(str(trajectory.get("path", ""))), "historical U0 TUM trajectory"
            )
            live = common.file_identity(path)
            if live.get("sha256") != trajectory.get("sha256") or live.get("size_bytes") != trajectory.get("size_bytes"):
                raise TrialError("historical U0 trajectory identity drift")
            expected["tum"] = {
                "size_bytes": live["size_bytes"],
                "sha256": live["sha256"],
                "source": "recorded historical trajectory",
            }
        else:
            state_path = Path(str(outputs["state"]["path"]))
            expected["tum"] = _derived_tum_content_identity_from_state(state_path)
        source = {
            "kind": "U0_G0_5_SCORED_MANIFEST",
            "identity": manifest_identity,
            "historical_status": manifest.get("status"),
            "historical_run_id": manifest.get("run_id"),
        }

    comparisons: Dict[str, Any] = {}
    for name in ("state", "deviation", "tum"):
        observed = _artifact_content(artifacts, name)
        wanted = expected[name]
        digest_match = bool(
            observed is not None and observed.get("sha256") == wanted.get("sha256")
        )
        size_match = bool(
            observed is not None
            and (
                wanted.get("size_bytes") is None
                or observed.get("size_bytes") == wanted.get("size_bytes")
            )
        )
        comparisons[name] = {
            "expected": wanted,
            "observed": observed,
            "sha256_exact_match": digest_match,
            "size_exact_match_when_available": size_match,
            "byte_exact": digest_match and size_match,
        }
    all_exact = all(item["byte_exact"] for item in comparisons.values())
    return {
        "status": "EXACT_MATCH" if all_exact else "MISMATCH_RETAINED",
        "all_three_byte_exact": all_exact,
        "source": source,
        "comparisons": comparisons,
        "fresh_outputs_never_replaced": True,
        "mismatch_is_not_infrastructure_invalid": True,
    }


def perturbation_requested(args: argparse.Namespace) -> bool:
    return getattr(args, "perturbation_campaign_id", None) is not None


def perturbation_record(args: argparse.Namespace) -> Dict[str, Any]:
    """Validate and describe one PERTURB-1 request; raise TrialError if unrunnable.

    The frozen CDSC-1R4 matrix start stays in ``args.bag_start`` (used for the
    matrix binding); the estimator receives ``frozen + offset_frames /
    frame_rate_hz``.  Nothing else changes.  A negative shifted start has no
    lead-in data and is rejected here (the driver records such cells as
    NOT_RUNNABLE without launching).
    """

    campaign_id = getattr(args, "perturbation_campaign_id", None)
    if campaign_id not in PERTURBATION_CAMPAIGN_IDS:
        raise TrialError("unknown perturbation campaign id: {}".format(campaign_id))
    offset_frames = getattr(args, "perturbation_offset_frames", None)
    frame_rate_hz = getattr(args, "perturbation_frame_rate_hz", None)
    seed_label = getattr(args, "perturbation_seed_label", None)
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise TrialError("perturbation offset must be an integer frame count")
    if (
        isinstance(frame_rate_hz, bool)
        or not isinstance(frame_rate_hz, (int, float))
        or not math.isfinite(float(frame_rate_hz))
        or float(frame_rate_hz) <= 0.0
    ):
        raise TrialError("perturbation frame rate must be finite and positive")
    if seed_label not in PERTURBATION_SEED_LABELS:
        raise TrialError(
            "perturbation seed label {!r} is not runnable: the frozen estimator has no "
            "runtime RNG seed parameter (cv::setRNGSeed(0) is compiled in)".format(seed_label)
        )
    if args.system not in PERTURBATION_SYSTEMS:
        raise TrialError("unknown perturbation system: {}".format(args.system))
    frozen_start = float(args.bag_start)
    shift_seconds = float(offset_frames) / float(frame_rate_hz)
    estimator_start = frozen_start + shift_seconds
    if not math.isfinite(estimator_start) or estimator_start < 0.0:
        raise TrialError(
            "NOT_RUNNABLE_NEGATIVE_OFFSET: shifted start {!r} s precedes the bag begin; "
            "no lead-in data".format(estimator_start)
        )
    return {
        "campaign_id": campaign_id,
        "axis": getattr(args, "perturbation_axis", "offset"),
        "offset_frames": offset_frames,
        "frame_rate_hz": float(frame_rate_hz),
        "shift_seconds": shift_seconds,
        "shift_seconds_repr": repr(shift_seconds),
        "seed_label": seed_label,
        "seed_delta": 0,
        "seed_mechanism": "compiled_constant_cv_setRNGSeed_0_not_a_runtime_parameter",
        "frozen_matrix_bag_start_seconds": frozen_start,
        "estimator_bag_start_seconds": estimator_start,
        "estimator_bag_start_seconds_repr": repr(estimator_start),
        "estimator_bag_start_launch_argument": format(estimator_start, ".17g"),
        "imu_and_camera_share_the_shifted_view": True,
        "system": args.system,
        "landmark_elimination": (
            LANDMARK_ELIMINATION.get(args.system) if args.system in S1_LIKE_SYSTEMS else "upstream_native"
        ),
        "recovery_ablated": args.system in RECOVERY_ABLATED_SYSTEMS,
        "recovery_switch": (
            {"parameter": RECOVERY_SWITCH_PARAMETER, "launch_value": False, "recon_counterpart": RECOVERY_ABLATED_SYSTEMS[args.system]}
            if args.system in RECOVERY_ABLATED_SYSTEMS
            else None
        ),
    }


def _validate_request(args: argparse.Namespace) -> None:
    for value, label in ((args.protocol_id, "protocol ID"), (args.run_id, "run ID")):
        if common.SAFE_ID_RE.fullmatch(value) is None:
            raise TrialError("{} must match {}".format(label, common.SAFE_ID_RE.pattern))
    if SAFE_SEQUENCE_RE.fullmatch(args.sequence) is None or args.sequence not in KAIST_SEQUENCES:
        raise TrialError("sequence is not in the frozen KAIST-11 population")
    if args.dataset != DATASET:
        raise TrialError("KAIST adapter requires --dataset kaist_vio")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise TrialError("timeout must be finite and positive")
    if args.bag_start != 0.0 or args.bag_duration != -1.0:
        raise TrialError("KAIST uses the frozen full-bag start/duration")
    if perturbation_requested(args):
        perturbation_record(args)
    elif args.system not in SYSTEMS:
        raise TrialError("system {} requires the PERTURB-1 perturbation mode".format(args.system))
    if args.attempt_index < 1:
        raise TrialError("attempt index must be positive")
    if args.mode == "capture" and args.scored_result is None:
        raise TrialError("capture mode requires --scored-result")
    if args.mode == "scored" and args.scored_result is not None:
        raise TrialError("scored mode forbids --scored-result")


def describe_recovery_events(console_text: str) -> Dict[str, Any]:
    """Purely descriptive, never-gating summary of long-gap recovery lines.

    PERTURB-1 keeps the existing (strict) C2 passage binding for completion.
    This record lets the aggregator report, for a non-completion, whether a
    recovery commit occurred at all and where, so both readings can be shown.
    """

    clean_lines = [rotation.ANSI_RE.sub("", line) for line in console_text.splitlines()]
    recovery_lines = [line for line in clean_lines if rotation.RECOVERY_PREFIX in line]
    names: List[str] = []
    for line in recovery_lines:
        match = re.search(r"\bevent=([a-z0-9_]+)(?:\s|$)", line)
        names.append(match.group(1) if match else "unparsed")
    counts = {name: names.count(name) for name in sorted(set(names))}
    triggers = []
    commits = []
    for line, name in zip(recovery_lines, names):
        if name == "trigger":
            match = rotation.RECOVERY_TRIGGER_RE.fullmatch(line)
            triggers.append(
                {key: float(value) if key != "epoch" and key != "activation" else int(value)
                 for key, value in match.groupdict().items()}
                if match
                else {"unparsed_line": line}
            )
        elif name == "relocalization_commit":
            match = rotation.RECOVERY_COMMIT_RE.fullmatch(line)
            commits.append(
                {key: value for key, value in match.groupdict().items()}
                if match
                else {"unparsed_line": line}
            )
    summary = None
    for line, name in zip(recovery_lines, names):
        if name == "summary":
            match = rotation.RECOVERY_SUMMARY_RE.fullmatch(line)
            summary = (
                {key: int(value) for key, value in match.groupdict().items()}
                if match
                else {"unparsed_line": line}
            )
    return {
        "descriptive_only_never_gates_completion": True,
        "event_line_count": len(recovery_lines),
        "event_counts": counts,
        "triggers": triggers,
        "commits": commits,
        "summary": summary,
    }


def _recovery_evidence(
    system: str,
    sequence: str,
    console_text: str,
    input_interval: Mapping[str, Any],
) -> Dict[str, Any]:
    if system == "U0":
        event_lines = [
            line for line in console_text.splitlines() if rotation.RECOVERY_PREFIX in line
        ]
        if event_lines:
            raise TrialError("original U0 unexpectedly emitted S1 recovery events")
        return {
            "status": "NOT_APPLICABLE",
            "reason": "PINNED_ORIGINAL_UPSTREAM_HAS_NO_C2_RECOVERY",
            "sequence_contract": "ORIGINAL_U0",
            "pass": True,
            "event_line_count": 0,
        }
    if system in RECOVERY_ABLATED_SYSTEMS:
        # The switch is off: the parser asserts that no recovery line was
        # emitted (otherwise the switch did not take effect) and records the
        # DEFAULT_OFF contract used by U0's timing rule.
        runtime = rotation.parse_recovery_runtime(console_text, False, sequence)
        return {
            "status": "NOT_APPLICABLE",
            "reason": "RECOVERY_DISABLED_BY_ABLATION_SWITCH",
            "sequence_contract": "ABLATED_OFF",
            "pass": True,
            "runtime": runtime,
            "event_line_count": 0,
            "post_pair_rotation_evaluation": "NOT_APPLICABLE",
        }
    runtime = rotation.parse_recovery_runtime(console_text, True, sequence)
    result: Dict[str, Any] = {
        "status": "PASS",
        "pass": True,
        "runtime": runtime,
        "detail": None,
        "gap_binding": None,
        "timing": None,
        "post_pair_rotation_evaluation": (
            "PENDING" if sequence == "rotation/rotation.bag" else "NOT_APPLICABLE"
        ),
    }
    if sequence == "rotation/rotation.bag":
        result["detail"] = validate_c2_detail(console_text, runtime)
        result["gap_binding"] = validate_recovery_gap_binding(runtime, input_interval)
    return result


def run_trial(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    _validate_request(args)
    perturbation = perturbation_record(args) if perturbation_requested(args) else None
    estimator_bag_start = (
        float(perturbation["estimator_bag_start_seconds"]) if perturbation else args.bag_start
    )
    run_dir = common.create_run_directory(args.output_root, args.run_id)
    started = time.monotonic()
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "adapter": "fresh_kaist_cdsc1r4" if perturbation is None else "fresh_kaist_cdsc1r4_perturb1",
        "perturbation": perturbation,
        "protocol_id": args.protocol_id,
        "run_id": args.run_id,
        "attempt_index": args.attempt_index,
        "dataset": args.dataset,
        "sequence": args.sequence,
        "system": args.system,
        "mode": args.mode,
        "status": "ACTIVE",
        "accuracy_eligible": False,
        "qualitative_eligible": False,
        "strict_process_health": False,
        "evidence_validity": "INVALID_INFRA",
        "started_utc": common.utc_now(),
        "finished_utc": None,
        "duration_seconds": None,
        "run_directory": str(run_dir),
        "input_interval": {
            "source": None,
            "selector_source": None,
            "first_selected_input_timestamp_s": None,
            "last_selected_input_timestamp_s": None,
            "first_selected_input_timestamp_ns": None,
            "last_selected_input_timestamp_ns": None,
            "selected_pair_count": None,
            "raw_serial_dispatch_pair_count": None,
            "visualizer_frequency_dropped_pair_count": None,
            "bag_view_start_record_timestamp_s": None,
            "bag_view_end_record_timestamp_s": None,
            "gaps_over_threshold": [],
        },
        "inputs": {
            "ground_truth": None,
            "protocol": None,
            "matrix": None,
            "bag": None,
            "config": None,
            "launch": None,
            "binary": None,
            "runner": None,
            "converter": None,
            "pairing_census_tool": None,
            "runtime_identity_validator": None,
            "visualizer_gate_projection_module": None,
            "runtime_summary_parser_module": None,
            "config_dependencies": None,
        },
        "artifacts": initial_artifacts(args.system),
        "runtime_identity": None,
        "resolved_parameters": None,
        "scored_linkage": None,
        "commands": {},
        "checks": {
            "ground_truth_never_opened_by_runner": True,
            "original_u0_never_modified_or_rescued": args.system == "U0",
            "runtime_inputs_unchanged": None,
            "input_decode_failure_count_zero": None,
            "late_initialization_is_descriptive_only": True,
        },
        "completion": None,
        "passage": common.passage_record({}, None, None),
        "robustness_mechanism": {
            "status": "PENDING",
            "pass": None,
            "post_pair_rotation_evaluation": (
                "PENDING"
                if args.system in RECOVERY_BOUND_SYSTEMS and args.sequence == "rotation/rotation.bag"
                else "NOT_APPLICABLE"
            ),
        },
        "estimator_close_receipt": {
            "estimator_attempted": False,
            "estimator_process_group_closed": False,
            "runtime_services_closed": False,
            "closed_utc": None,
        },
        "console_classification": None,
        "failure": None,
        "stage_errors": [],
    }

    environment = common.minimal_environment(run_dir, args.ros_port)
    result["runtime_environment"] = environment
    result["ros_isolation"] = {
        "port": args.ros_port,
        "master_uri": environment["ROS_MASTER_URI"],
        "home": environment["HOME"],
        "ros_home": environment["ROS_HOME"],
        "ros_log_dir": environment["ROS_LOG_DIR"],
    }
    managed: List[common.ManagedProcess] = []
    teardown_ok = True
    runtime_contract_valid = False
    launch_record: Optional[Dict[str, Any]] = None
    state: Optional[Dict[str, Any]] = None
    state_kind = "missing"
    outputs_valid = False
    coverage_pass = False
    continuity_pass = False
    tail_pass = False
    numeric_integrity_valid = True
    input_decode_valid: Optional[bool] = None
    capture_closed = args.mode == "scored"
    linkage_valid = args.mode == "scored"
    exact_u0_teardown = False
    console_text = ""
    input_paths: Dict[str, Path] = {}
    input_identities_before: Dict[str, Dict[str, Any]] = {}
    stage = "preflight"

    try:
        common.assert_port_available(args.ros_port)
        bag = common._regular_file(args.bag, "adapted KAIST bag")
        config = common._regular_file(args.config, "estimator config")
        launch = common._regular_file(args.launch, "KAIST launch")
        binary = common._regular_file(args.binary, "estimator binary")
        protocol = common._regular_file(args.protocol_file, "campaign protocol")
        matrix = common._regular_file(args.matrix_file, "campaign matrix")
        runner = common._regular_file(Path(__file__).resolve(), "fresh-KAIST trial runner")
        converter = common._regular_file(CONVERTER, "state-to-TUM converter")
        pairing_tool = common._regular_file(PAIRING_TOOL, "KAIST pairing census tool")
        runtime_identity_tool = common._regular_file(
            RUNTIME_IDENTITY_TOOL, "runtime identity validator"
        )
        visualizer_gate_module = common._regular_file(
            Path(common.__file__).resolve(), "visualizer gate projection module"
        )
        runtime_summary_module = common._regular_file(
            Path(rotation.__file__).resolve(), "runtime summary parser module"
        )
        input_paths = {
            "bag": bag,
            "config": config,
            "launch": launch,
            "binary": binary,
            "protocol": protocol,
            "matrix": matrix,
            "runner": runner,
            "converter": converter,
            "pairing_census_tool": pairing_tool,
            "runtime_identity_validator": runtime_identity_tool,
            "visualizer_gate_projection_module": visualizer_gate_module,
            "runtime_summary_parser_module": runtime_summary_module,
        }
        for name, path in input_paths.items():
            if name != "bag":
                result["inputs"][name] = common.file_identity(path)

        result["canonical_inputs"] = validate_canonical_campaign(protocol, matrix)
        campaign = common.validate_campaign_bindings(
            args.protocol_id,
            protocol,
            matrix,
            DATASET,
            args.sequence,
            "S1" if args.system in S1_LIKE_SYSTEMS else args.system,
            bag,
            args.bag_start,
        )
        result["protocol_identity"] = campaign["protocol"]
        result["matrix_identity"] = campaign["matrix"]
        result["campaign_binding"] = campaign
        result["launch_contract"] = validate_launch_contract(args.system, launch)
        config_contract = validate_config_contract(args.system, config)
        result["config_contract"] = config_contract
        dependencies = dict(config_contract["dependencies"])
        result["inputs"]["config_dependencies"] = dependencies
        for path_text, identity in dependencies.items():
            input_paths["dependency:" + path_text] = Path(path_text)

        stage = "kaist_pair_census"
        normalized_census, full_census, bag_identity = kaist_pair_census(
            args.system,
            bag,
            estimator_bag_start,
            args.bag_duration,
            float(config_contract["track_frequency_hz"]),
            perturbation=perturbation is not None,
        )
        common.validate_matrix_bag_identity(campaign, bag_identity)
        result["inputs"]["bag"] = bag_identity
        result["input_interval"] = normalized_census["input_interval"]
        census_path = run_dir / "diagnostics" / "native_pair_census.json"
        full_census_path = run_dir / "diagnostics" / "kaist_pairing_census.json"
        common.atomic_write_new_json(census_path, normalized_census)
        common.atomic_write_new_json(full_census_path, full_census)
        result["native_pair_census"] = common.file_identity(census_path)
        result["kaist_pairing_census"] = common.file_identity(full_census_path)

        stage = "resolved_parameters"
        launch_args = launch_arguments(
            args.system,
            launch,
            config,
            bag,
            estimator_bag_start,
            args.bag_duration,
            run_dir,
        )
        result["estimator_launch_arguments"] = launch_args
        dump_path = run_dir / "diagnostics" / "resolved_ros_parameters.yaml"
        dump_record = common.run_command(
            [str(common._which("roslaunch")), "--dump-params", *launch_args],
            dump_path,
            environment,
            min(args.timeout_seconds, 120.0),
        )
        result["commands"]["resolve_parameters"] = dump_record
        if not common.command_succeeded(dump_record):
            raise TrialError("ROS parameter resolution failed")
        resolved = validate_resolved_parameters(
            args.system,
            dump_path.read_text(encoding="utf-8", errors="strict"),
            config,
            bag,
            estimator_bag_start,
            run_dir,
        )
        resolved["artifact"] = common.file_identity(dump_path)
        result["resolved_parameters"] = resolved
        result["ground_truth_firewall"] = {
            "ground_truth_cli_surface_absent": True,
            "ground_truth_resolved_parameters_absent": True,
            "ground_truth_not_opened": True,
        }

        stage = "runtime_identity"
        result["runtime_identity"] = common.resolve_runtime_identity(
            args.system,
            binary,
            run_dir,
            environment,
            args.timeout_seconds,
            result["commands"],
        )

        stage = "cpu_affinity_preflight"
        affinity = common.run_command(
            ["/usr/bin/taskset", "--cpu-list", args.cpu_list, "/usr/bin/true"],
            run_dir / "diagnostics" / "cpu_affinity_preflight.log",
            environment,
            30.0,
        )
        result["commands"]["cpu_affinity_preflight"] = affinity
        if not common.command_succeeded(affinity):
            raise TrialError("requested CPU affinity is unavailable")

        if args.mode == "capture":
            stage = "scored_linkage_preflight"
            result["scored_linkage"] = common.load_scored_linkage(
                args.scored_result,
                args.protocol_id,
                DATASET,
                args.sequence,
                args.system,
            )

        for name, path in input_paths.items():
            if name == "bag":
                input_identities_before[name] = bag_identity
            elif name.startswith("dependency:"):
                input_identities_before[name] = dependencies[str(path)]
            else:
                identity = result["inputs"][name]
                if not isinstance(identity, dict):
                    raise TrialError("missing input identity for {}".format(name))
                input_identities_before[name] = identity

        runtime_contract_valid = True
        stage = "runtime_services"
        roscore = common._start_roscore(run_dir, environment, args.ros_port)
        managed.append(roscore)
        if args.mode == "capture":
            managed.append(_start_geometry_recorder(run_dir, environment, args.system))

        stage = "estimator"
        resource_path = run_dir / "diagnostics" / "resource_usage.txt"
        estimator_argv = [
            "/usr/bin/time",
            "--verbose",
            "--output=" + str(resource_path),
            "--",
            "/usr/bin/taskset",
            "--cpu-list",
            args.cpu_list,
            str(common._which("roslaunch")),
            *launch_args,
        ]
        result["estimator_argv"] = estimator_argv
        launch_record = common.run_command(
            estimator_argv,
            run_dir / "diagnostics" / "console.log",
            environment,
            args.timeout_seconds,
        )
        result["commands"]["estimator"] = launch_record
        if resource_path.is_file():
            result["resource_usage"] = common.file_identity(resource_path)

        stage = "runtime_close"
        for process in reversed(managed):
            close_record = common.finish_managed_process(process)
            result["commands"][process.name] = close_record
            teardown_ok = teardown_ok and not close_record["process_group_survived_cleanup"]
        managed = []
        result["estimator_close_receipt"].update(
            {
                "estimator_attempted": True,
                "estimator_process_group_closed": not bool(
                    launch_record.get("process_group_survived_cleanup")
                ),
                "runtime_services_closed": teardown_ok,
                "closed_utc": common.utc_now() if teardown_ok else None,
            }
        )

        stage = "console_classification"
        console_path = run_dir / "diagnostics" / "console.log"
        console_text = console_path.read_text(encoding="utf-8", errors="replace")
        console = common.parse_console(console_path)
        child = rotation.parse_roslaunch_child_deaths(console_text)
        console["roslaunch_child_outcome"] = child
        result["console_classification"] = console
        node_name = str(CANONICAL_INPUTS[args.system]["node"])
        expected_starts = [
            value for value in console["child_starts"] if value["name"].startswith(node_name + "-")
        ]
        if len(expected_starts) != 1:
            runtime_contract_valid = False
            common._record_error(
                result,
                stage,
                TrialError("roslaunch did not report exactly one expected estimator start"),
            )

        runtime_pairing_valid = args.system == "U0"
        if args.system in S1_LIKE_SYSTEMS:
            try:
                pairing_runtime = assess_s1_pairing_runtime(
                    console_text, full_census, normalized_census
                )
                result["pairing_runtime"] = pairing_runtime
                runtime_pairing_valid = pairing_runtime.get("status") == "AVAILABLE"
            except KAIST_VALIDATION_ERRORS as exc:
                result["pairing_runtime"] = {
                    "status": "UNAVAILABLE",
                    "reason": str(exc),
                    "static_census_retained": True,
                    "early_selector_evidence_observed": (
                        rotation.SERIAL_SUMMARY_PREFIX in console_text
                    ),
                    "terminal_enqueue_evidence_observed": (
                        rotation.ENQUEUE_SUMMARY_PREFIX in console_text
                    ),
                }
                runtime_contract_valid = False
                common._record_error(result, "pairing_runtime", exc)
        else:
            result["pairing_runtime"] = {
                "status": "STATIC_NATIVE_CENSUS",
                "reason": "original upstream exposes no exact-header runtime counters",
                "static_census_retained": True,
            }

        try:
            result["robustness_mechanism"] = _recovery_evidence(
                args.system, args.sequence, console_text, result["input_interval"]
            )
        except KAIST_VALIDATION_ERRORS as exc:
            result["robustness_mechanism"] = {
                "status": "FAIL",
                "pass": False,
                "reason": str(exc),
                "post_pair_rotation_evaluation": (
                    "BLOCKED_BY_RUNTIME_CONTRACT"
                    if args.system in RECOVERY_BOUND_SYSTEMS and args.sequence == "rotation/rotation.bag"
                    else "NOT_APPLICABLE"
                ),
            }
            common._record_error(result, "recovery_runtime", exc)
        if perturbation is not None:
            result["perturbation_recovery_descriptive"] = describe_recovery_events(
                console_text
            )

        numeric_integrity_valid = not any(
            console[name]
            for name in ("nonfinite_pattern", "reset_pattern", "covariance_failure_pattern")
        )
        result["checks"]["numeric_integrity_valid"] = numeric_integrity_valid
        input_decode_valid = not console["image_decode_failure_pattern"]
        result["checks"]["input_decode_failure_count_zero"] = input_decode_valid
        result["checks"]["s1_runtime_pairing_evidence_valid"] = runtime_pairing_valid
        exact_u0_teardown = bool(
            args.system == "U0"
            and console["post_coverage_teardown_pattern"]
            and console["child_exit_codes"] == [-6]
        )

        stage = "output_validation"
        state_path = run_dir / "trajectory" / "state_estimate.txt"
        deviation_path = run_dir / "trajectory" / "state_deviation.txt"
        timing_path = run_dir / "diagnostics" / "timing_openvins.csv"
        tum_path = run_dir / "trajectory" / "estimate_raw.tum"
        if not state_path.is_file():
            state_kind = "missing"
        else:
            try:
                state = common.validate_numeric_table(state_path, 8)
                state_kind = "valid"
            except NoRowsError as exc:
                state_kind = "empty"
                common._record_error(result, stage, exc)
            except TrialError as exc:
                state_kind = "invalid"
                common._record_error(result, stage, exc)

        deviation: Optional[Dict[str, Any]] = None
        timing: Optional[Dict[str, Any]] = None
        tum: Optional[Dict[str, Any]] = None
        if state is not None:
            try:
                deviation = common.validate_numeric_table(deviation_path, 8)
            except TrialError as exc:
                common._record_error(result, "deviation_validation", exc)
            try:
                timing = rotation.validate_numeric_table(timing_path, 2, ",")
            except KAIST_VALIDATION_ERRORS as exc:
                common._record_error(result, "timing_validation", exc)
            conversion = common.run_command(
                [str(PYTHON), str(converter), str(state_path), str(tum_path)],
                run_dir / "diagnostics" / "trajectory_conversion.log",
                environment,
                min(args.timeout_seconds, 300.0),
            )
            result["commands"]["trajectory_conversion"] = conversion
            if common.command_succeeded(conversion):
                try:
                    tum = common.validate_numeric_table(tum_path, 8)
                except KAIST_VALIDATION_ERRORS as exc:
                    common._record_error(result, "tum_validation", exc)
            else:
                common._record_error(
                    result,
                    "trajectory_conversion",
                    TrialError("state-to-TUM conversion failed"),
                )
            outputs_valid = bool(
                deviation is not None
                and tum is not None
                and state["rows"] == deviation["rows"] == tum["rows"]
                and state["timestamp_sequence_sha256"]
                == deviation["timestamp_sequence_sha256"]
                == tum["timestamp_sequence_sha256"]
            )
            timing_contract = None
            if timing is not None and result["robustness_mechanism"].get("pass") is True:
                try:
                    recovery_runtime = (
                        {"status": "NOT_ENABLED", "sequence_contract": "DEFAULT_OFF"}
                        if args.system == "U0" or args.system in RECOVERY_ABLATED_SYSTEMS
                        else result["robustness_mechanism"].get("runtime", {})
                    )
                    timing_contract = rotation.validate_output_consistency(
                        state_path,
                        deviation_path,
                        timing_path,
                        tum_path,
                        sequence=args.sequence,
                        recovery_runtime=recovery_runtime,
                    )
                    result["robustness_mechanism"]["timing"] = timing_contract
                except KAIST_VALIDATION_ERRORS as exc:
                    common._record_error(result, "timing_contract", exc)
                    result["robustness_mechanism"].update(
                        {"status": "FAIL", "pass": False, "reason": str(exc)}
                    )
            result["output_validation"] = {
                "state": state,
                "deviation": deviation,
                "timing": timing,
                "tum": tum,
                "consistent": outputs_valid,
                "timing_contract": timing_contract,
                "timing_contract_is_separate_from_passage_accuracy_eligibility": True,
            }
            if not outputs_valid:
                common._record_error(
                    result,
                    "output_consistency",
                    TrialError("state, deviation, and TUM row/timestamp identity differs"),
                )
            try:
                result["completion"] = assess_kaist_output_coverage(
                    args.system,
                    args.sequence,
                    result["input_interval"],
                    state,
                    result["robustness_mechanism"],
                )
                coverage_pass = bool(result["completion"]["pass"])
                continuity_pass = bool(result["completion"]["maximum_state_gap_pass"])
                tail_pass = bool(result["completion"]["tail_gap_pass"])
                result["passage"] = common.passage_record(
                    result["input_interval"], state, result["completion"]
                )
                if input_decode_valid is False:
                    result["completion"]["pass"] = False
                    result["completion"]["input_decode_valid"] = False
                    result["passage"]["complete"] = False
                    result["passage"]["reason"] = "INPUT_DECODE_FAILURE"
                    coverage_pass = False
                    continuity_pass = False
            except KAIST_VALIDATION_ERRORS as exc:
                common._record_error(result, "completion", exc)

        stage = "capture_validation"
        result["artifacts"] = refresh_artifacts(run_dir, args.system)
        if args.mode == "scored" and (
            perturbation is not None
            and (args.system != "S1" or estimator_bag_start != 0.0)
        ):
            result["historical_determinism"] = {
                "status": "NOT_APPLICABLE_PERTURBED_OR_CONTROL_CELL",
                "reason": (
                    "no compatible historical byte expectation exists for system {} at "
                    "estimator start {!r}".format(args.system, estimator_bag_start)
                ),
                "fresh_outputs_never_replaced": True,
                "mismatch_is_not_infrastructure_invalid": True,
            }
        elif args.mode == "scored":
            try:
                result["historical_determinism"] = historical_determinism_check(
                    args.system, args.sequence, result["artifacts"]
                )
            except TrialError as exc:
                # Historical evidence is context, not a replacement or an
                # infrastructure gate for the fresh attempt.
                result["historical_determinism"] = {
                    "status": "UNAVAILABLE_RETAINED",
                    "reason": str(exc),
                    "fresh_outputs_never_replaced": True,
                    "mismatch_is_not_infrastructure_invalid": True,
                }
                common._record_error(result, "historical_determinism", exc)
        if args.mode == "capture":
            raw = result["artifacts"]["raw_geometry"]
            capture_closed = bool(
                raw["identity"] is not None and raw["identity"]["size_bytes"] > 0
            )
            if capture_closed:
                info_path = run_dir / "diagnostics" / "feature_stream_info.yaml"
                info = common.run_command(
                    [str(common._which("rosbag")), "info", "--yaml", raw["identity"]["path"]],
                    info_path,
                    environment,
                    min(args.timeout_seconds, 300.0),
                )
                result["commands"]["feature_stream_info"] = info
                capture_closed = common.command_succeeded(info)
                if capture_closed:
                    summary = _capture_bag_summary(info_path)
                    summary["requested_topics"] = list(geometry_topics(args.system))
                    result["geometry_capture"] = summary
            linkage_valid = common.assess_capture_linkage(
                result["scored_linkage"], result["artifacts"]
            )

        stage = "input_postflight"
        input_identities_after: Dict[str, Any] = {}
        unchanged = True
        for name, path in input_paths.items():
            observed = common.file_identity(path)
            input_identities_after[name] = observed
            unchanged = unchanged and common._same_file_identity(
                input_identities_before[name], observed
            )
        result["input_identities_after"] = input_identities_after
        result["checks"]["runtime_inputs_unchanged"] = unchanged
        if not unchanged:
            runtime_contract_valid = False
            common._record_error(
                result, stage, TrialError("runtime input identity changed during trial")
            )

    except KeyboardInterrupt as exc:
        runtime_contract_valid = False
        common._record_error(result, stage, exc)
        if launch_record is None:
            launch_record = {"interrupted": True, "timed_out": False, "exit_code": None}
    except BaseException as exc:
        # Unhandled harness/preflight/evidence exceptions are not estimator
        # outcomes.  Expected native algorithm failures are represented by
        # run_command/output facts and do not enter this branch.
        runtime_contract_valid = False
        common._record_error(result, stage, exc)
        result["failure"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        for process in reversed(managed):
            try:
                close_record = common.finish_managed_process(process)
                result["commands"][process.name] = close_record
                teardown_ok = teardown_ok and not close_record["process_group_survived_cleanup"]
            except BaseException as exc:
                teardown_ok = False
                common._record_error(result, process.name + "_cleanup", exc)
        managed = []
        estimator_group_closed = bool(
            launch_record is not None
            and not launch_record.get("process_group_survived_cleanup", False)
        )
        runtime_services_closed = bool(teardown_ok and not managed)
        result["estimator_close_receipt"] = {
            "estimator_attempted": launch_record is not None,
            "estimator_process_group_closed": estimator_group_closed,
            "runtime_services_closed": runtime_services_closed,
            "closed_utc": (
                common.utc_now()
                if launch_record is not None and estimator_group_closed and runtime_services_closed
                else None
            ),
        }
        try:
            result["artifacts"] = refresh_artifacts(run_dir, args.system)
        except BaseException as exc:
            common._record_error(result, "artifact_refresh", exc)

        console = result.get("console_classification") or {}
        child = console.get("roslaunch_child_outcome", {})
        launch_abnormal = bool(
            launch_record is not None
            and (
                launch_record.get("exit_code") not in (0, None)
                or bool(console.get("child_exit_codes"))
                or bool(child.get("estimator_required_child_died"))
            )
        )
        if args.system in S1_LIKE_SYSTEMS and launch_record is not None:
            pairing_runtime = result.get("pairing_runtime")
            if not isinstance(pairing_runtime, Mapping):
                pairing_runtime = {}
            pairing_decision = finalize_s1_pairing_runtime_contract(
                runtime_contract_valid,
                pairing_runtime,
                child if isinstance(child, Mapping) else {},
                launch_record,
                estimator_group_closed,
                state_kind,
                _s1_post_selector_evidence_observed(
                    console_text,
                    state_kind,
                    numeric_integrity_valid,
                    input_decode_valid,
                ),
            )
            if isinstance(result.get("pairing_runtime"), dict):
                result["pairing_runtime"]["termination_binding"] = pairing_decision
            if (
                runtime_contract_valid
                and pairing_decision["runtime_contract_valid"] is False
            ):
                common._record_error(
                    result,
                    "pairing_runtime_termination",
                    TrialError(str(pairing_decision["reason"])),
                )
            runtime_contract_valid = bool(
                pairing_decision["runtime_contract_valid"]
            )
        facts = {
            "mode": args.mode,
            "interrupted": bool(launch_record and launch_record.get("interrupted")),
            "timed_out": bool(launch_record and launch_record.get("timed_out")),
            "teardown_ok": teardown_ok
            and not bool(launch_record and launch_record.get("process_group_survived_cleanup")),
            "runtime_contract_valid": runtime_contract_valid,
            "numeric_integrity_valid": numeric_integrity_valid,
            "input_decode_valid": input_decode_valid,
            "state_kind": state_kind,
            "outputs_valid": outputs_valid,
            "continuity_pass": continuity_pass,
            "tail_pass": tail_pass,
            "launch_abnormal": launch_abnormal,
            "exact_u0_teardown": exact_u0_teardown and coverage_pass,
            "capture_closed": capture_closed,
            "linkage_valid": linkage_valid,
        }
        result["outcome_facts"] = facts
        result["status"] = classify_outcome(facts)
        result["strict_process_health"] = bool(
            runtime_contract_valid
            and numeric_integrity_valid
            and input_decode_valid
            and facts["teardown_ok"]
            and not launch_abnormal
            and not facts["timed_out"]
            and not facts["interrupted"]
        )
        result["accuracy_eligible"] = bool(
            args.mode == "scored"
            and result["status"] in ELIGIBLE_STATUSES
            and result["passage"]["complete"]
        )
        result["qualitative_eligible"] = bool(
            args.mode == "capture"
            and result["status"] in ELIGIBLE_STATUSES
            and capture_closed
            and linkage_valid
        )
        result["evidence_validity"] = common.classify_evidence_validity(
            result["status"],
            runtime_contract_valid,
            result["checks"].get("runtime_inputs_unchanged"),
            bool(facts["teardown_ok"]),
            mode=args.mode,
            capture_closed=capture_closed,
            linkage_valid=linkage_valid,
        )
        result["finished_utc"] = common.utc_now()
        result["duration_seconds"] = time.monotonic() - started
        result["checks"]["status_known"] = result["status"] in STATUSES
        result["checks"]["teardown_complete"] = facts["teardown_ok"]
        result["checks"]["native_failure_retained"] = result["status"] not in ELIGIBLE_STATUSES
        result["checks"]["passage_independent_of_rotation_target_thresholds"] = True
        result["checks"]["ros_latest_symlink_cleanup"] = (
            common.remove_run_owned_ros_latest_symlink(run_dir)
        )
        if result["failure"] is None and result["status"] not in ELIGIBLE_STATUSES:
            result["failure"] = {
                "stage": "classification",
                "type": result["status"],
                "message": "trial retained with status {}".format(result["status"]),
            }
        result["publication"] = {
            "sequence_result": "sequence_result.json",
            "checksums": "SHA256SUMS",
            "append_only_run_directory": True,
            "overwrite_policy": "exclusive unique run directory and atomic new-file publication",
        }
        common.atomic_write_new_json(run_dir / "sequence_result.json", result)
        common.write_sha256sums(run_dir)
    return result, run_dir


def verify_checksum_bundle(run_dir: Path) -> Dict[str, Any]:
    """Verify a run's complete append-only SHA256SUMS closure.

    Algorithm failures are allowed to have no trajectory products.  The
    sequence result itself is mandatory, while every file that *was* emitted
    must still be present in, and byte-exact against, the checksum closure.
    Product requirements for an evaluable S1 recovery are enforced separately
    after both result manifests have been validated.
    """

    if run_dir.is_symlink():
        raise TrialError("run directory is a symlink")
    resolved = run_dir.resolve(strict=True)
    if not resolved.is_dir():
        raise TrialError("run directory is not a regular nonsymlink directory")
    checksum_candidate = resolved / "SHA256SUMS"
    if checksum_candidate.is_symlink():
        raise TrialError("run SHA256SUMS is a symlink")
    checksum_path = common._regular_file(checksum_candidate, "run SHA256SUMS")
    records: Dict[str, str] = {}
    for line_number, line in enumerate(
        checksum_path.read_text(encoding="ascii", errors="strict").splitlines(), 1
    ):
        parts = line.split("  ", 1)
        if (
            len(parts) != 2
            or re.fullmatch(r"[0-9a-f]{64}", parts[0]) is None
            or not parts[1]
        ):
            raise TrialError("malformed SHA256SUMS line {}".format(line_number))
        relative = Path(parts[1])
        if relative.is_absolute() or ".." in relative.parts or parts[1] in records:
            raise TrialError("unsafe or duplicate SHA256SUMS path")
        records[parts[1]] = parts[0]
    actual = {
        path.relative_to(resolved).as_posix()
        for path in resolved.rglob("*")
        if path.is_file() and path != checksum_path
    }
    if set(records) != actual:
        raise TrialError(
            "SHA256SUMS file closure differs: missing={} extra={}".format(
                sorted(actual - set(records)), sorted(set(records) - actual)
            )
        )
    required = {"sequence_result.json"}
    scored_products = {
        "trajectory/state_estimate.txt",
        "trajectory/state_deviation.txt",
        "trajectory/estimate_raw.tum",
    }
    if not required.issubset(records):
        raise TrialError(
            "scored checksum bundle lacks {}".format(sorted(required - set(records)))
        )
    for relative, expected in records.items():
        path = resolved / relative
        if path.is_symlink() or not path.is_file():
            raise TrialError("checksum bundle contains a nonregular file")
        if common.sha256_file(path) != expected:
            raise TrialError("checksum mismatch: {}".format(relative))
    return {
        "status": "PASS",
        "identity": common.file_identity(checksum_path),
        "file_count": len(records),
        "all_run_files_closed": True,
        "sequence_result_present": True,
        "scored_products_present": sorted(scored_products.intersection(records)),
        "scored_products_missing": sorted(scored_products.difference(records)),
        "all_scored_products_present": scored_products.issubset(records),
    }


def _input_identity_records(value: Any, prefix: str = "inputs") -> List[Tuple[str, Mapping[str, Any]]]:
    records: List[Tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, Mapping):
        if (
            isinstance(value.get("path"), str)
            and isinstance(value.get("size_bytes"), int)
            and isinstance(value.get("sha256"), str)
        ):
            records.append((prefix, value))
        for key in sorted(value):
            records.extend(_input_identity_records(value[key], prefix + "." + str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            records.extend(_input_identity_records(item, "{}[{}]".format(prefix, index)))
    return records


def _load_result(
    path: Path,
    system: str,
    identity_cache: Optional[MutableMapping[Path, Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    if path.is_symlink():
        raise TrialError("{} scored sequence result is a symlink".format(system))
    resolved = common._regular_file(path, system + " scored sequence result")
    identity = common.file_identity(resolved)
    try:
        value = json.loads(
            resolved.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError("nonfinite JSON token {}".format(token))
            ),
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise TrialError("{} sequence result is invalid JSON".format(system)) from exc
    if not isinstance(value, dict):
        raise TrialError("{} sequence result is not an object".format(system))
    expected = {
        "schema": SCHEMA,
        "protocol_id": "CDSC-1R4",
        "dataset": DATASET,
        "sequence": "rotation/rotation.bag",
        "system": system,
        "mode": "scored",
    }
    differences = {
        key: {"observed": value.get(key), "expected": wanted}
        for key, wanted in expected.items()
        if value.get(key) != wanted
    }
    if differences:
        raise TrialError("{} post-pair identity differs: {}".format(system, differences))
    run_dir = resolved.parent
    declared_run_dir = Path(str(value.get("run_directory", ""))).resolve(strict=True)
    if declared_run_dir != run_dir:
        raise TrialError("{} result run_directory differs from its parent".format(system))
    checksum_closure = verify_checksum_bundle(run_dir)
    cache = identity_cache if identity_cache is not None else {}
    verified_inputs: List[Dict[str, Any]] = []
    inputs = value.get("inputs")
    if not isinstance(inputs, Mapping):
        raise TrialError("{} result lacks immutable inputs".format(system))
    for label, record in _input_identity_records(inputs):
        source = common._regular_file(Path(str(record["path"])), label)
        resolved_source = source.resolve(strict=True)
        if resolved_source not in cache:
            cache[resolved_source] = common.file_identity(resolved_source)
        observed = cache[resolved_source]
        if not common._same_file_identity(record, observed):
            raise TrialError("{} immutable input identity drift: {}".format(system, label))
        verified_inputs.append({"label": label, "identity": observed})
    if not verified_inputs:
        raise TrialError("{} result has no revalidated immutable input identity".format(system))
    receipt = value.get("estimator_close_receipt")
    if not isinstance(receipt, Mapping) or not all(
        receipt.get(key) is True
        for key in ("estimator_attempted", "estimator_process_group_closed", "runtime_services_closed")
    ) or not isinstance(receipt.get("closed_utc"), str):
        raise TrialError("{} estimator does not have a complete close receipt".format(system))
    return value, identity, {
        "checksum_closure": checksum_closure,
        "immutable_input_count": len(verified_inputs),
        "immutable_inputs": verified_inputs,
    }


def _verify_result_artifact(
    result: Mapping[str, Any], name: str, expected_relative: str
) -> Dict[str, Any]:
    artifacts = result.get("artifacts")
    record = artifacts.get(name) if isinstance(artifacts, Mapping) else None
    if not isinstance(record, Mapping) or record.get("relative_path") != expected_relative:
        raise TrialError("S1 result lacks fixed {} artifact path".format(name))
    identity = record.get("identity")
    if not isinstance(identity, Mapping):
        raise TrialError("S1 result lacks {} artifact identity".format(name))
    recorded_path = Path(str(identity.get("path", "")))
    if recorded_path.is_symlink():
        raise TrialError("S1 {} artifact is a symlink".format(name))
    observed = common.file_identity(common._regular_file(recorded_path, "S1 " + name))
    if not common._same_file_identity(identity, observed):
        raise TrialError("S1 {} artifact identity drift".format(name))
    run_dir = Path(str(result.get("run_directory", ""))).resolve(strict=True)
    expected = (run_dir / expected_relative).resolve(strict=True)
    if Path(observed["path"]) != expected:
        raise TrialError("S1 {} artifact escapes its run directory".format(name))
    return observed


def _s1_rotation_artifact_precondition(
    result: Mapping[str, Any],
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    """Distinguish absent algorithm output from corrupt artifact evidence."""

    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise TrialError("S1 result artifact table is absent")
    expected = {
        "state": "trajectory/state_estimate.txt",
        "deviation": "trajectory/state_deviation.txt",
        "tum": "trajectory/estimate_raw.tum",
    }
    unavailable: List[str] = []
    for name, relative in expected.items():
        record = artifacts.get(name)
        if not isinstance(record, Mapping) or record.get("relative_path") != relative:
            raise TrialError("S1 result fixed {} artifact contract differs".format(name))
        identity = record.get("identity")
        if identity is None:
            unavailable.append(name)
        elif not isinstance(identity, Mapping):
            raise TrialError("S1 result {} artifact identity is malformed".format(name))
    if unavailable:
        return None, {
            "pass": False,
            "reason_code": "S1_REQUIRED_OUTPUT_UNAVAILABLE",
            "message": "S1 did not emit all state/deviation/TUM products",
            "required": expected,
            "unavailable": unavailable,
        }
    verified = {
        name: _verify_result_artifact(result, name, relative)
        for name, relative in expected.items()
    }
    return verified, {
        "pass": True,
        "reason_code": "AVAILABLE",
        "message": "S1 state/deviation/TUM products are checksum-closed and available",
        "required": expected,
        "unavailable": [],
        "identities": verified,
    }


def _matrix_rotation_declared_binding(matrix_path: Path) -> Dict[str, Any]:
    """Bind the rotation row without resolving or opening its ground truth."""

    matrix_path = common._regular_file(matrix_path, "CDSC-1R4 matrix")
    if matrix_path != CANONICAL_MATRIX.resolve(strict=True):
        raise TrialError("post-pair matrix is not the canonical CDSC-1R4 path")
    try:
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8", errors="strict"))
    except yaml.YAMLError as exc:
        raise TrialError("CDSC-1R4 matrix is invalid YAML") from exc
    rows = matrix.get("sequences") if isinstance(matrix, dict) else None
    matches = [
        row
        for row in rows or []
        if isinstance(row, Mapping)
        and row.get("dataset") == DATASET
        and row.get("sequence") == "rotation/rotation.bag"
    ]
    if len(matches) != 1:
        raise TrialError("matrix does not identify one KAIST rotation row")
    gt_record = matches[0].get("ground_truth")
    if not isinstance(gt_record, Mapping) or gt_record.get("capability") != "full_trajectory":
        raise TrialError("matrix KAIST rotation ground truth is not a full trajectory")
    canonical_path = gt_record.get("canonical_path")
    byte_count = gt_record.get("bytes")
    digest = gt_record.get("sha256")
    if (
        not isinstance(canonical_path, str)
        or not Path(canonical_path).is_absolute()
        or isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise TrialError("matrix KAIST rotation ground-truth identity is malformed")
    return {
        "matrix": common.file_identity(matrix_path),
        "row_order": matches[0].get("order"),
        "ground_truth_expected_from_matrix": {
            "canonical_path": canonical_path,
            "size_bytes": byte_count,
            "sha256": digest,
            "capability": gt_record.get("capability"),
            "format": gt_record.get("format"),
            "source": "FROZEN_MATRIX_DECLARATION_NOT_LIVE_FILE",
            "live_file_opened": False,
        },
        "ground_truth": None,
        "ground_truth_opened": False,
    }


def _matrix_rotation_gt_binding(matrix_path: Path, ground_truth: Path) -> Dict[str, Any]:
    declared = _matrix_rotation_declared_binding(matrix_path)
    gt_record = declared["ground_truth_expected_from_matrix"]
    resolved_gt = common._regular_file(ground_truth, "KAIST rotation ground truth")
    if Path(str(gt_record["canonical_path"])).resolve(strict=True) != resolved_gt:
        raise TrialError("ground-truth path differs from the frozen matrix")
    observed = common.file_identity(resolved_gt)
    if (
        observed["size_bytes"] != gt_record.get("size_bytes")
        or observed["sha256"] != gt_record.get("sha256")
    ):
        raise TrialError("ground-truth bytes differ from the frozen matrix")
    return {
        **declared,
        "ground_truth": observed,
        "ground_truth_opened": True,
    }


def _single_system_rotation_metrics(
    trajectory_path: Path, ground_truth_path: Path
) -> Dict[str, Any]:
    gt_rows = metric_core.read_tum_exact(ground_truth_path, "rotation ground truth")
    estimate_rows = metric_core.read_tum_exact(trajectory_path, "S1 rotation estimate")
    associations = metric_core.emulate_evo_association(gt_rows, estimate_rows)
    selected_gt = [gt_rows[item.gt_index] for item in associations]
    selected_estimate = [estimate_rows[item.estimate_index] for item in associations]
    reference = metric_core.rows_to_evo(selected_gt)
    estimate = metric_core.rows_to_evo(selected_estimate)
    reference_pairs = metric_core.metrics.id_pairs_from_delta(
        reference.poses_se3,
        metric_core.RPE_DELTA_METERS,
        metric_core.metrics.Unit.meters,
        metric_core.RPE_RELATIVE_DELTA_TOLERANCE,
        all_pairs=metric_core.RPE_ALL_PAIRS,
    )
    reference_pairs = [(int(left), int(right)) for left, right in reference_pairs]
    if not reference_pairs:
        raise TrialError("rotation target evaluation has no 1 m RPE pairs")
    evaluated = metric_core.evaluate_method("S1", reference, estimate, reference_pairs)
    primary = evaluated["primary"]
    return {
        "association_count": len(associations),
        "rpe_pair_count": len(reference_pairs),
        "ate_translation": {"stats": {"rmse": primary["ate_translation_rmse_m"]}},
        "rpe_translation_1m": {
            "stats": {"rmse": primary["rpe_translation_rmse_1m_m"]}
        },
        "rpe_rotation_1m_deg": {
            "stats": {"rmse": primary["rpe_rotation_rmse_1m_deg"]}
        },
        "alignment": evaluated["alignment"],
        "association_policy": "evo_1.31.1_10ms_single_S1_population",
    }


def _post_pair_source_runs(
    u0_result: Mapping[str, Any],
    u0_identity: Mapping[str, Any],
    u0_closure: Mapping[str, Any],
    s1_result: Mapping[str, Any],
    s1_identity: Mapping[str, Any],
    s1_closure: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "U0": {
            "result": dict(u0_identity),
            "run_id": u0_result.get("run_id"),
            "status": u0_result.get("status"),
            "accuracy_eligible": bool(u0_result.get("accuracy_eligible")),
            "closure": dict(u0_closure),
            "trajectory_not_required_for_c2_evaluation": True,
        },
        "S1": {
            "result": dict(s1_identity),
            "run_id": s1_result.get("run_id"),
            "status": s1_result.get("status"),
            "accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
            "closure": dict(s1_closure),
        },
    }


def _post_pair_result_base(
    source_runs: Mapping[str, Any],
    matrix_binding: Mapping[str, Any],
    s1_result: Mapping[str, Any],
) -> Dict[str, Any]:
    mechanism = s1_result.get("robustness_mechanism")
    return {
        "schema": POST_PAIR_SCHEMA,
        "status": "C2_VALIDATION_FAILURE",
        "pass": False,
        "protocol_id": "CDSC-1R4",
        "dataset": DATASET,
        "sequence": "rotation/rotation.bag",
        "source_runs": dict(source_runs),
        "both_scored_estimator_groups_closed_before_ground_truth_open": True,
        "ground_truth_opened": False,
        "matrix_binding": dict(matrix_binding),
        "c2_prerequisite": None,
        "retained_s1_evidence": {
            "status": s1_result.get("status"),
            "accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
            "robustness_mechanism": mechanism,
            "failure": s1_result.get("failure"),
            "outcome_facts": s1_result.get("outcome_facts"),
        },
        "rotation_gap": None,
        "s1_single_population_metrics": None,
        "rotation_gap_gate": None,
        "numeric_target_acceptance": None,
        "c2_validation_failure": None,
        "accuracy_eligibility_unchanged_by_c2_target_thresholds": True,
        "c2_validation_is_a_separate_robustness_mechanism_result": True,
        "postflight_identities": None,
        "finished_utc": None,
    }


def _post_pair_source_postflight(
    value: MutableMapping[str, Any],
    u0_identity: Mapping[str, Any],
    u0_closure: Mapping[str, Any],
    s1_identity: Mapping[str, Any],
    s1_closure: Mapping[str, Any],
    matrix_identity: Mapping[str, Any],
    trajectory_identity: Optional[Mapping[str, Any]] = None,
    ground_truth_identity: Optional[Mapping[str, Any]] = None,
) -> None:
    u0_checksum_identity = u0_closure["checksum_closure"]["identity"]
    s1_checksum_identity = s1_closure["checksum_closure"]["identity"]
    post = {
        "U0_result": common.file_identity(Path(str(u0_identity["path"]))),
        "U0_checksums": common.file_identity(
            Path(str(u0_checksum_identity["path"]))
        ),
        "S1_result": common.file_identity(Path(str(s1_identity["path"]))),
        "S1_checksums": common.file_identity(
            Path(str(s1_checksum_identity["path"]))
        ),
        "matrix": common.file_identity(Path(str(matrix_identity["path"]))),
    }
    comparisons = (
        ("U0 result", u0_identity, post["U0_result"]),
        ("U0 SHA256SUMS", u0_checksum_identity, post["U0_checksums"]),
        ("S1 result", s1_identity, post["S1_result"]),
        ("S1 SHA256SUMS", s1_checksum_identity, post["S1_checksums"]),
        ("matrix", matrix_identity, post["matrix"]),
    )
    for label, before, after in comparisons:
        if not common._same_file_identity(before, after):
            raise TrialError("{} changed during post-pair evaluation".format(label))
    if trajectory_identity is not None:
        trajectory = common.file_identity(Path(str(trajectory_identity["path"])))
        if not common._same_file_identity(trajectory_identity, trajectory):
            raise TrialError("S1 trajectory changed during post-pair evaluation")
        post["S1_trajectory"] = trajectory
    if ground_truth_identity is not None:
        ground_truth = common.file_identity(Path(str(ground_truth_identity["path"])))
        if not common._same_file_identity(ground_truth_identity, ground_truth):
            raise TrialError("ground truth changed during post-pair evaluation")
        post["ground_truth"] = ground_truth
    value["postflight_identities"] = post
    value["finished_utc"] = common.utc_now()


def evaluate_rotation_post_pair(
    u0_result_path: Path,
    s1_result_path: Path,
    ground_truth_path: Path,
    matrix_path: Path,
) -> Dict[str, Any]:
    """Evaluate C2's GT-dependent gates after both scored arms are closed."""

    # Read and validate both close receipts before the first ground-truth file
    # identity or trajectory parse occurs.
    identity_cache: Dict[Path, Dict[str, Any]] = {}
    u0_result, u0_identity, u0_closure = _load_result(
        u0_result_path, "U0", identity_cache
    )
    s1_result, s1_identity, s1_closure = _load_result(
        s1_result_path, "S1", identity_cache
    )
    declared_binding = _matrix_rotation_declared_binding(matrix_path)
    matrix_identity = declared_binding["matrix"]
    for value, label in ((u0_result, "U0"), (s1_result, "S1")):
        recorded = value.get("inputs", {}).get("matrix")
        if not isinstance(recorded, Mapping) or not common._same_file_identity(
            recorded, matrix_identity
        ):
            raise TrialError("{} result is not bound to the post-pair matrix bytes".format(label))
    if u0_result.get("run_id") == s1_result.get("run_id"):
        raise TrialError("post-pair run IDs are not distinct")
    source_runs = _post_pair_source_runs(
        u0_result,
        u0_identity,
        u0_closure,
        s1_result,
        s1_identity,
        s1_closure,
    )
    result = _post_pair_result_base(source_runs, declared_binding, s1_result)

    mechanism = s1_result.get("robustness_mechanism")
    verified_artifacts, artifact_gate = _s1_rotation_artifact_precondition(s1_result)
    mechanism_pass = isinstance(mechanism, Mapping) and mechanism.get("pass") is True
    if not mechanism_pass:
        mechanism_status = mechanism.get("status") if isinstance(mechanism, Mapping) else None
        result["c2_prerequisite"] = {
            "pass": False,
            "reason_code": (
                "S1_MECHANISM_CONTRACT_FAILED"
                if isinstance(mechanism, Mapping)
                else "S1_MECHANISM_EVIDENCE_UNAVAILABLE"
            ),
            "message": "S1 runtime C2 mechanism contract did not pass",
            "s1_status": s1_result.get("status"),
            "s1_accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
            "mechanism_status": mechanism_status,
            "mechanism_pass": False,
            "required_artifacts": artifact_gate,
        }
        result["c2_validation_failure"] = {
            "reason_code": result["c2_prerequisite"]["reason_code"],
            "message": result["c2_prerequisite"]["message"],
            "algorithm_failure_retained": True,
            "infrastructure_failure": False,
        }
        _post_pair_source_postflight(
            result,
            u0_identity,
            u0_closure,
            s1_identity,
            s1_closure,
            matrix_identity,
        )
        return result
    if verified_artifacts is None:
        result["c2_prerequisite"] = {
            "pass": False,
            "reason_code": artifact_gate["reason_code"],
            "message": artifact_gate["message"],
            "s1_status": s1_result.get("status"),
            "s1_accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
            "mechanism_status": mechanism.get("status"),
            "mechanism_pass": True,
            "required_artifacts": artifact_gate,
        }
        result["c2_validation_failure"] = {
            "reason_code": artifact_gate["reason_code"],
            "message": artifact_gate["message"],
            "algorithm_failure_retained": True,
            "infrastructure_failure": False,
        }
        _post_pair_source_postflight(
            result,
            u0_identity,
            u0_closure,
            s1_identity,
            s1_closure,
            matrix_identity,
        )
        return result

    runtime = mechanism.get("runtime")
    covariance = runtime.get("commit_covariance") if isinstance(runtime, Mapping) else None
    if not isinstance(covariance, Mapping) or covariance.get("status") != "AVAILABLE":
        result["c2_prerequisite"] = {
            "pass": False,
            "reason_code": "S1_COMMIT_COVARIANCE_UNAVAILABLE",
            "message": "S1 result lacks available commit covariance",
            "s1_status": s1_result.get("status"),
            "s1_accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
            "mechanism_status": mechanism.get("status"),
            "mechanism_pass": True,
            "required_artifacts": artifact_gate,
        }
        result["c2_validation_failure"] = {
            "reason_code": "S1_COMMIT_COVARIANCE_UNAVAILABLE",
            "message": result["c2_prerequisite"]["message"],
            "algorithm_failure_retained": True,
            "infrastructure_failure": False,
        }
        _post_pair_source_postflight(
            result,
            u0_identity,
            u0_closure,
            s1_identity,
            s1_closure,
            matrix_identity,
        )
        return result

    result["c2_prerequisite"] = {
        "pass": True,
        "reason_code": "EVALUABLE",
        "message": "S1 C2 runtime, covariance, and output prerequisites passed",
        "s1_status": s1_result.get("status"),
        "s1_accuracy_eligible": bool(s1_result.get("accuracy_eligible")),
        "mechanism_status": mechanism.get("status"),
        "mechanism_pass": True,
        "required_artifacts": artifact_gate,
    }
    trajectory_identity = verified_artifacts["tum"]

    # Ground truth is first opened here, after both receipts and immutable run
    # inputs have closed and been validated.
    binding = _matrix_rotation_gt_binding(matrix_path, ground_truth_path)
    if not common._same_file_identity(matrix_identity, binding["matrix"]):
        raise TrialError("matrix changed before ground-truth binding")
    result["matrix_binding"] = binding
    result["ground_truth_opened"] = True
    ground_truth = Path(binding["ground_truth"]["path"])
    trajectory = Path(trajectory_identity["path"])
    try:
        gap = rotation.evaluate_rotation_gap(trajectory, ground_truth, covariance)
        metrics = _single_system_rotation_metrics(trajectory, ground_truth)
        gap_gate = rotation.assess_rotation_gap_evidence(gap, True)
        numeric = rotation.assess_rotation_target_acceptance(gap, metrics, True)
    except KAIST_VALIDATION_ERRORS as exc:
        result["c2_validation_failure"] = {
            "reason_code": "S1_ROTATION_EVALUATION_FAILED",
            "message": str(exc),
            "exception_type": type(exc).__name__,
            "algorithm_failure_retained": True,
            "infrastructure_failure": False,
        }
        _post_pair_source_postflight(
            result,
            u0_identity,
            u0_closure,
            s1_identity,
            s1_closure,
            binding["matrix"],
            trajectory_identity=trajectory_identity,
            ground_truth_identity=binding["ground_truth"],
        )
        return result
    passed = bool(gap_gate.get("pass") and numeric.get("admission_pass"))
    result.update(
        {
            "status": "PASS" if passed else "C2_VALIDATION_FAILURE",
            "pass": passed,
            "rotation_gap": gap,
            "s1_single_population_metrics": metrics,
            "rotation_gap_gate": gap_gate,
            "numeric_target_acceptance": numeric,
            "c2_validation_failure": (
                None
                if passed
                else {
                    "reason_code": "C2_GAP_OR_NUMERIC_GATE_FAILED",
                    "message": "one or more frozen C2 gap/NEES/numeric gates failed",
                    "algorithm_failure_retained": True,
                    "infrastructure_failure": False,
                }
            ),
        }
    )
    _post_pair_source_postflight(
        result,
        u0_identity,
        u0_closure,
        s1_identity,
        s1_closure,
        binding["matrix"],
        trajectory_identity=trajectory_identity,
        ground_truth_identity=binding["ground_truth"],
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run one append-only KAIST estimator attempt")
    run.add_argument("--protocol-id", required=True)
    run.add_argument("--protocol", "--protocol-file", dest="protocol_file", required=True, type=Path)
    run.add_argument("--matrix", "--matrix-file", dest="matrix_file", required=True, type=Path)
    run.add_argument("--run-id", required=True)
    run.add_argument("--attempt-index", type=int, default=1)
    run.add_argument("--dataset", required=True, choices=(DATASET,))
    run.add_argument("--sequence", required=True)
    run.add_argument("--system", required=True, choices=PERTURBATION_SYSTEMS)
    run.add_argument("--mode", choices=MODES, default="scored")
    run.add_argument("--bag", required=True, type=Path)
    run.add_argument("--bag-start", type=float, default=0.0)
    run.add_argument("--bag-duration", type=float, default=-1.0)
    run.add_argument("--config", required=True, type=Path)
    run.add_argument("--launch", required=True, type=Path)
    run.add_argument("--binary", required=True, type=Path)
    run.add_argument("--output-root", required=True, type=Path)
    run.add_argument("--ros-port", required=True, type=int)
    run.add_argument("--timeout-seconds", type=float, default=21600.0)
    run.add_argument("--cpu-list", default="8-15")
    run.add_argument("--scored-result", type=Path)
    # PERTURB-1 thin extension (docs/icra27/PERTURBATION_PREREG.md).  Absent
    # by default so CDSC-1R4 behaviour is unchanged.
    run.add_argument("--perturbation-campaign-id", default=None)
    run.add_argument("--perturbation-offset-frames", type=int, default=0)
    run.add_argument("--perturbation-frame-rate-hz", type=float, default=30.0)
    run.add_argument("--perturbation-seed-label", default="frozen")
    run.add_argument("--perturbation-axis", choices=("offset", "seed"), default="offset")

    post = commands.add_parser(
        "post-pair-rotation",
        help="evaluate GT-dependent C2 gates after both scored arms close",
    )
    post.add_argument("--u0-result", required=True, type=Path)
    post.add_argument("--s1-result", required=True, type=Path)
    post.add_argument("--ground-truth", required=True, type=Path)
    post.add_argument("--matrix", required=True, type=Path)
    post.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            result, run_dir = run_trial(args)
            print(
                "{} {} {} {}: {}".format(
                    result["status"], result["dataset"], result["sequence"], result["system"], run_dir
                )
            )
            return 0 if result["status"] in ELIGIBLE_STATUSES else 2
        output = args.output.expanduser().resolve(strict=False)
        value = evaluate_rotation_post_pair(
            args.u0_result,
            args.s1_result,
            args.ground_truth,
            args.matrix,
        )
        common.atomic_write_new_json(output, value)
        print("{}: {}".format(value["status"], output))
        return 0 if value["pass"] else 2
    except (OSError, ValueError, json.JSONDecodeError, *KAIST_VALIDATION_ERRORS) as exc:
        print("CROSS_DATASET_KAIST_TRIAL_ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
