#!/usr/bin/python3
"""Run one fixed KAIST-VIO preparation or baseline-evaluation job.

This is an orchestration and evidence tool, not an estimator configuration
surface.  It accepts exactly one of the eleven public KAIST-VIO sequences,
uses :mod:`kaist_vio_adapter` to audit/adapt it, gives only the adapted bag to
the fixed serial launch file, and exposes the public reference trajectory only
after roslaunch has finished.
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DATA_ROOT = Path("/home/moksh/datasets/KAIST_VIO")
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "turnsafe"
ADAPTER_PATH = SCRIPT_DIR / "kaist_vio_adapter.py"
SOURCE_SNAPSHOT_PATH = SCRIPT_DIR / "source_snapshot.py"
CONVERTER_PATH = REPO_ROOT / "scripts" / "cp0" / "openvins_to_tum.py"
FIXED_LAUNCH = REPO_ROOT / "project" / "kaist_vio_serial.launch"
FIXED_CONFIG = (
    REPO_ROOT / "config" / "kaist_vio_turnsafe_baseline" / "estimator_config.yaml"
)
FROZEN_BASELINE_RESULTS = (
    REPO_ROOT / "artifacts" / "turnsafe" / "data" /
    "KAIST_BASELINE_RESULTS.csv"
)
FROZEN_BASELINE_RESULTS_SHA256 = (
    "99f94546bfe1d807af04c47df82ff5bd0ed63d8c1eb500e5e10bed3fa95bf8b5"
)
PYTHON = Path("/usr/bin/python3")

# This order is the frozen Session-0.5 smoke/campaign order, not a tuning order.
SEQUENCE_ORDER: Tuple[str, ...] = (
    "rotation/rotation_fast.bag",
    "rotation/rotation.bag",
    "circle/circle_head.bag",
    "infinite/infinite_head.bag",
    "square/square_head.bag",
    "circle/circle_fast.bag",
    "infinite/infinite_fast.bag",
    "square/square_fast.bag",
    "circle/circle.bag",
    "infinite/infinite.bag",
    "square/square.bag",
)

SCHEMA = "turnsafe.kaist_vio_campaign.sequence.v1"
T0_SCHEMA = "turnsafe.t0.v1"
STABLE_TERMINAL_REASON_ORDER: Tuple[str, ...] = (
    "FULL_OUTCOME_INELIGIBLE",
    "FULL_OUTCOME_UNMAPPED",
    "OBSERVATION_OWNERSHIP_INVALID",
    "CLONE_UNAVAILABLE",
    "TARGET_STEREO_UNAVAILABLE",
    "TARGET_STEREO_IDENTITY_MISMATCH",
    "TARGET_STEREO_TIMESTAMP_MISMATCH",
    "RANGE_GEOMETRY_INVALID",
    "RANGE_COVARIANCE_INVALID",
    "RANGE_LCB_UNAVAILABLE",
    "RANGE_LCB_NONPOSITIVE",
    "TRANSLATION_COVARIANCE_INVALID",
    "TRANSLATION_NOT_ACUTE",
    "BEARING_COVARIANCE_INVALID",
    "RHO_TRANSLATION_EXCEEDED",
    "STATIC_QUALITY_FAILED",
    "THRESHOLD_SET_NOT_FROZEN",
    "INSUFFICIENT_FEATURES",
    "SPATIAL_COVERAGE_FAILED",
    "CONSENSUS_FAILED",
    "ROTATION_STACK_RANK_DEFICIENT",
    "LOWER_PRE_NIS_SCORE",
    "WINNER_NIS_REJECTED",
    "WINNER_POST_NIS_COUNT_OR_RANK_FAILED",
    "GLOBAL_SHADOW_VALIDATION_FAILED",
    "NONE",
)
STABLE_TERMINAL_REASONS = frozenset(STABLE_TERMINAL_REASON_ORDER)
_TERMINAL_REASON_STAGE_BOUNDS = {
    "candidate": ("FULL_OUTCOME_INELIGIBLE", "STATIC_QUALITY_FAILED", True),
    "group_consensus": (
        "THRESHOLD_SET_NOT_FROZEN",
        "ROTATION_STACK_RANK_DEFICIENT",
        True,
    ),
    "group_foregone": ("LOWER_PRE_NIS_SCORE", "LOWER_PRE_NIS_SCORE", False),
}
SOURCE_SNAPSHOT_SCHEMA = "turnsafe.source_snapshot.v1"
CONFIGURE_PROVENANCE_SCHEMA = "turnsafe.configure_provenance.v1"
BUILD_MANIFEST_SCHEMA = "turnsafe.build_manifest.v1"
SERIAL_KAIST_SUMMARY_PREFIX = "[SERIAL-KAIST]: exact_header_pairs="
SERIAL_KAIST_SUMMARY_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]: exact_header_pairs=(?P<exact_header_pairs>[0-9]+) "
    r"camera0_without_match=(?P<camera0_without_match>[0-9]+) "
    r"camera1_without_match=(?P<camera1_without_match>[0-9]+) "
    r"record_delta_ge_20ms=(?P<record_delta_ge_20ms>[0-9]+) "
    r"maximum_record_delta_ns=(?P<maximum_record_delta_ns>[0-9]+)"
)
CAMERA_ENQUEUE_SUMMARY_PREFIX = "[SERIAL-KAIST]: queued_pairs="
CAMERA_ENQUEUE_SUMMARY_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]: queued_pairs=(?P<queued_pairs>[0-9]+) "
    r"processed_pairs=(?P<processed_pairs>[0-9]+) "
    r"frequency_thinned_pairs=(?P<frequency_thinned_pairs>[0-9]+) "
    r"cam0_decode_failures=(?P<cam0_decode_failures>[0-9]+) "
    r"cam1_decode_failures=(?P<cam1_decode_failures>[0-9]+) "
    r"pending_pairs=(?P<pending_pairs>[0-9]+)"
)
ANSI_CONTROL_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FORBIDDEN_COMPONENTS = ("holdout", "private")
FORBIDDEN_RUNTIME_TEXT = (
    "/pose_transformed",
    "path_gt",
    "ground_truth",
    "initialize_with_gt",
)
FIXED_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONHASHSEED": "0",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "MPLBACKEND": "Agg",
}


class CampaignError(RuntimeError):
    """A visible, fail-closed campaign error."""


def validate_stable_terminal_reason(
    value: Any, label: str, stage: Optional[str] = None
) -> str:
    """Require exact normative membership and field-stage legality."""

    if not isinstance(value, str) or not value:
        raise CampaignError("{} is not a nonempty string".format(label))
    if value not in STABLE_TERMINAL_REASONS:
        raise CampaignError("{} is not a stable terminal reason".format(label))
    if stage is None:
        return value
    bounds = _TERMINAL_REASON_STAGE_BOUNDS.get(stage)
    if bounds is None:
        raise CampaignError("{} has unknown terminal-reason stage".format(label))
    first, last, allow_none = bounds
    first_index = STABLE_TERMINAL_REASON_ORDER.index(first)
    last_index = STABLE_TERMINAL_REASON_ORDER.index(last)
    allowed = STABLE_TERMINAL_REASON_ORDER[first_index:last_index + 1]
    if value not in allowed and not (allow_none and value == "NONE"):
        raise CampaignError(
            "{} has a terminal reason from the wrong stage".format(label)
        )
    return value


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(path),
        "executable": bool(stat.st_mode & 0o111),
    }


def _frozen_sequence_inputs(sequence: str) -> Dict[str, str]:
    """Load byte identities from the checksum-bound Session-0.5 result."""
    path = _regular_file(
        FROZEN_BASELINE_RESULTS, "frozen Session-0.5 baseline results"
    )
    if sha256_file(path) != FROZEN_BASELINE_RESULTS_SHA256:
        raise CampaignError("frozen Session-0.5 baseline result hash mismatch")
    required = (
        "source_bag_sha256",
        "adapted_bag_sha256",
        "config_sha256",
        "kalibr_imu_chain_sha256",
        "kalibr_imucam_chain_sha256",
        "reference_sha256",
    )
    with path.open("r", encoding="utf-8", errors="strict", newline="") as stream:
        reader = csv.DictReader(stream)
        rows = [row for row in reader if row.get("sequence") == sequence]
    if len(rows) != 1:
        raise CampaignError(
            "frozen Session-0.5 baseline has ambiguous sequence identity"
        )
    result: Dict[str, str] = {}
    for key in required:
        value = rows[0].get(key, "")
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise CampaignError(
                "frozen Session-0.5 baseline {} is invalid".format(key)
            )
        result[key] = value
    return result


def _require_frozen_identity(
    actual: Mapping[str, Any], expected_sha256: str, label: str
) -> None:
    if actual.get("sha256") != expected_sha256:
        raise CampaignError("{} differs from frozen Session-0.5 input".format(label))


def _sha256_canonical_json(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _validate_recorded_file_identity(
    record: Any, label: str
) -> Tuple[Path, Dict[str, Any]]:
    if not isinstance(record, dict):
        raise CampaignError("{} identity is not an object".format(label))
    try:
        raw_path = Path(str(record["path"]))
        _reject_forbidden_path(raw_path, label)
        path = raw_path.resolve(strict=True)
    except (KeyError, OSError) as exc:
        raise CampaignError("{} identity path is invalid".format(label)) from exc
    actual = file_identity(path)
    for key in ("size_bytes", "sha256", "executable"):
        if record.get(key) != actual[key]:
            raise CampaignError("{} {} identity mismatch".format(label, key))
    return path, actual


def _validate_source_snapshot(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != SOURCE_SNAPSHOT_SCHEMA:
        raise CampaignError("{} source snapshot schema mismatch".format(label))
    claimed = value.get("aggregate_source_snapshot_sha256")
    body = dict(value)
    body.pop("aggregate_source_snapshot_sha256", None)
    if claimed != _sha256_canonical_json(body):
        raise CampaignError("{} source snapshot aggregate mismatch".format(label))
    return dict(value)


def _parse_cmake_cache(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw or raw.startswith("#") or raw.startswith("//") or "=" not in raw:
            continue
        typed_key, value = raw.split("=", 1)
        key = typed_key.split(":", 1)[0]
        if key in values:
            raise CampaignError("duplicate CMake cache key {}".format(key))
        values[key] = value
    return values


def _normal_cmake_bool(value: str) -> str:
    upper = value.upper()
    if upper in ("1", "ON", "TRUE", "YES", "Y"):
        return "ON"
    if upper in ("0", "OFF", "FALSE", "NO", "N", ""):
        return "OFF"
    raise CampaignError("invalid CMake Boolean value {}".format(value))


def _validate_cache_descriptor_binding(
    cache_path: Path, configure_path: Path, descriptor: Mapping[str, Any]
) -> Tuple[Path, Path, Path]:
    configuration = descriptor.get("configuration")
    if not isinstance(configuration, dict):
        raise CampaignError("build configuration descriptor is missing")
    raw_binary = Path(str(configuration.get("cmake_binary_directory", "")))
    raw_source = Path(str(configuration.get("cmake_source_directory", "")))
    _reject_forbidden_path(raw_binary, "configured binary directory")
    _reject_forbidden_path(raw_source, "configured source directory")
    binary_directory = raw_binary.resolve(strict=True)
    source_directory = raw_source.resolve(strict=True)
    if cache_path != binary_directory / "CMakeCache.txt":
        raise CampaignError("CMake cache is outside the configured build")
    if configure_path != (
        binary_directory / "turnsafe-generated" / "configure_provenance.json"
    ):
        raise CampaignError("configure manifest is outside the configured build")
    cache = _parse_cmake_cache(cache_path)
    exact = {
        "CMAKE_BUILD_TYPE": str(configuration.get("build_type", "")),
        "CMAKE_CXX_FLAGS": str(configuration.get("cmake_cxx_flags_cache", "")),
        "CMAKE_GENERATOR": str(configuration.get("cmake_generator", "")),
    }
    for key, expected in exact.items():
        if cache.get(key) != expected:
            raise CampaignError("CMake cache {} binding mismatch".format(key))
    paths = {
        "CMAKE_CXX_COMPILER": str(configuration.get("cxx_compiler", "")),
        "Ceres_DIR": str(configuration.get("ceres_dir", "")),
        "CMAKE_HOME_DIRECTORY": str(source_directory),
        "ov_msckf_BINARY_DIR": str(binary_directory),
    }
    for key, expected in paths.items():
        actual = cache.get(key)
        if actual is None or Path(actual).resolve(strict=False) != Path(
            expected
        ).resolve(strict=False):
            raise CampaignError("CMake cache {} binding mismatch".format(key))
    booleans = {
        "ENABLE_ROS": str(configuration.get("enable_ros", "")),
        "CATKIN_ENABLE_TESTING": str(
            configuration.get("catkin_enable_testing", "")
        ),
    }
    for key, expected in booleans.items():
        actual = cache.get(key)
        if actual is None or _normal_cmake_bool(actual) != _normal_cmake_bool(
            expected
        ):
            raise CampaignError("CMake cache {} binding mismatch".format(key))
    if not isinstance(configuration.get("cmake_cxx_flags_cache"), str) or not isinstance(
        configuration.get("cmake_cxx_flags_effective"), str
    ):
        raise CampaignError("build configuration has incomplete C++ flags")
    flags_path = binary_directory / "CMakeFiles/ov_msckf_lib.dir/flags.make"
    _reject_forbidden_path(flags_path, "target compile flags")
    lines = [
        line.split("=", 1)[1].strip()
        for line in flags_path.read_text(encoding="utf-8", errors="strict").splitlines()
        if line.startswith("CXX_FLAGS =")
    ]
    if len(lines) != 1:
        raise CampaignError("target compile flags are missing or ambiguous")
    try:
        expected_flags = shlex.split(
            str(configuration["cmake_cxx_flags_effective"])
        )
        actual_flags = shlex.split(lines[0])
    except ValueError as exc:
        raise CampaignError("invalid target compile flags: {}".format(exc))
    if actual_flags[: len(expected_flags)] != expected_flags:
        raise CampaignError("target compile flags binding mismatch")
    return binary_directory, binary_directory.parent.parent, flags_path.resolve(strict=True)


def _validate_build_manifest(
    path: Path, estimator_binary: Path, schema_path: Path, repository: Path
) -> Dict[str, Any]:
    _reject_forbidden_path(path, "TurnSafe build manifest")
    _reject_forbidden_path(estimator_binary, "estimator binary")
    _reject_forbidden_path(schema_path, "TurnSafe schema")
    _reject_forbidden_path(repository, "TurnSafe repository")
    value = _strict_json(path.read_text(encoding="ascii"), "build manifest")
    if value.get("schema_version") != BUILD_MANIFEST_SCHEMA:
        raise CampaignError("TurnSafe build-manifest schema mismatch")
    claimed_payload = value.get("build_manifest_payload_sha256")
    body = dict(value)
    body.pop("build_manifest_payload_sha256", None)
    if claimed_payload != _sha256_canonical_json(body):
        raise CampaignError("TurnSafe build-manifest payload mismatch")
    descriptor = value.get("descriptor")
    if not isinstance(descriptor, dict):
        raise CampaignError("TurnSafe build descriptor is missing")
    build_id = _sha256_canonical_json(descriptor)
    if value.get("build_provenance_id") != build_id:
        raise CampaignError("TurnSafe build-provenance ID mismatch")
    source_snapshot = _validate_source_snapshot(
        value.get("source_snapshot"), "build manifest"
    )
    recorded_repository = Path(str(source_snapshot.get("repository", "")))
    _reject_forbidden_path(recorded_repository, "build source repository")
    if recorded_repository.resolve(strict=True) != repository:
        raise CampaignError("build manifest belongs to a different repository")
    source_descriptor = descriptor.get("source")
    if not isinstance(source_descriptor, dict):
        raise CampaignError("build source descriptor is missing")
    expected_source = {
        "head_sha": source_snapshot.get("head_sha"),
        "head_tree": source_snapshot.get("head_tree"),
        "source_dirty": source_snapshot.get("source_dirty"),
        "source_snapshot_sha256": source_snapshot.get(
            "aggregate_source_snapshot_sha256"
        ),
        "tracked_diff_sha256": source_snapshot.get("tracked_diff_sha256"),
        "status_porcelain_sha256": source_snapshot.get(
            "status_porcelain_sha256"
        ),
    }
    if source_descriptor != expected_source:
        raise CampaignError("build source descriptor does not match snapshot")

    configure_path, configure_identity = _validate_recorded_file_identity(
        value.get("configure_manifest"), "configure manifest"
    )
    configure = _strict_json(
        configure_path.read_text(encoding="ascii"), "configure manifest"
    )
    if configure.get("schema_version") != CONFIGURE_PROVENANCE_SCHEMA:
        raise CampaignError("configure-provenance schema mismatch")
    if configure.get("descriptor") != descriptor or configure.get(
        "build_provenance_id"
    ) != build_id:
        raise CampaignError("configure provenance differs from build manifest")
    if configure.get("source_snapshot") != source_snapshot:
        raise CampaignError("configure source snapshot differs from build manifest")
    cache_path, _ = _validate_recorded_file_identity(
        value.get("cmake_cache"), "CMake cache"
    )
    _, workspace_root, flags_path = _validate_cache_descriptor_binding(
        cache_path, configure_path, descriptor
    )
    recorded_flags_path, _ = _validate_recorded_file_identity(
        value.get("target_compile_flags"), "target compile flags"
    )
    if recorded_flags_path != flags_path:
        raise CampaignError("target compile flags are outside the configured build")
    recorded_schema_path, recorded_schema = _validate_recorded_file_identity(
        value.get("diagnostic_schema"), "diagnostic schema"
    )
    if recorded_schema_path != schema_path or recorded_schema["sha256"] != descriptor.get(
        "diagnostic_schema_sha256"
    ):
        raise CampaignError("diagnostic schema is not build-bound")

    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CampaignError("build artifacts are missing")
    for required in (
        "estimator_binary",
        "ov_msckf_library",
        "ov_core_library",
    ):
        if required not in artifacts:
            raise CampaignError("build artifact {} is missing".format(required))
    artifact_paths: Dict[str, Path] = {}
    for name, identity in sorted(artifacts.items()):
        artifact_paths[name], _ = _validate_recorded_file_identity(
            identity, "build artifact {}".format(name)
        )
    expected_artifacts = {
        "estimator_binary": workspace_root
        / "devel/lib/ov_msckf/ros1_serial_msckf",
        "ov_msckf_library": workspace_root / "devel/lib/libov_msckf_lib.so",
        "ov_core_library": workspace_root / "devel/lib/libov_core_lib.so",
    }
    for name, expected in expected_artifacts.items():
        if artifact_paths[name] != expected:
            raise CampaignError(
                "build artifact {} is outside the configured workspace".format(name)
            )
    if artifact_paths["estimator_binary"] != estimator_binary:
        raise CampaignError("build manifest binds a different estimator binary")
    return {
        "value": dict(value),
        "build_provenance_id": build_id,
        "source_snapshot": source_snapshot,
        "configure_manifest_path": configure_path,
        "configure_manifest_sha256": configure_identity["sha256"],
        "artifact_paths": artifact_paths,
        "cmake_cache_path": cache_path,
    }


def _validate_t0_caller_expectations(
    build_source: Mapping[str, Any],
    build_provenance_id: str,
    source_sha: str,
    source_tree: str,
    expected_build_id: str,
    expected_source_state: str,
) -> None:
    if source_sha and source_sha != build_source.get("head_sha"):
        raise CampaignError("caller-provided source SHA conflicts with build")
    if source_tree and source_tree != build_source.get("head_tree"):
        raise CampaignError("caller-provided source tree conflicts with build")
    if expected_build_id and expected_build_id != build_provenance_id:
        raise CampaignError("caller-provided build ID conflicts with build")
    if bool(build_source.get("source_dirty")) != (
        expected_source_state == "dirty"
    ):
        raise CampaignError("build source state conflicts with requested state")


def _strict_json(line: str, label: str) -> Mapping[str, Any]:
    def reject_constant(value: str) -> None:
        raise CampaignError("{} contains nonfinite JSON token {}".format(label, value))

    def reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
        value: Dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise CampaignError("{} contains duplicate JSON key {}".format(label, key))
            value[key] = child
        return value

    value = json.loads(
        line,
        object_pairs_hook=reject_duplicate_keys,
        parse_constant=reject_constant,
    )

    def reject_nonfinite(value: Any, value_label: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                reject_nonfinite(child, value_label + "." + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                reject_nonfinite(child, "{}[{}]".format(value_label, index))
        elif isinstance(value, float) and not math.isfinite(value):
            raise CampaignError("{} contains a nonfinite number".format(value_label))

    reject_nonfinite(value, label)
    if not isinstance(value, dict):
        raise CampaignError("{} is not a JSON object".format(label))
    return value


def _validate_t0_jsonl(
    path: Path, expected_header: Mapping[str, Any]
) -> Dict[str, Any]:
    """Open and validate the actual atomically published estimator log."""

    if not path.is_file() or path.stat().st_size == 0:
        raise CampaignError("TurnSafe T0 JSONL was not atomically published")
    records: List[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, raw in enumerate(stream, 1):
            if not raw.endswith("\n"):
                raise CampaignError("T0 JSONL final line is not newline terminated")
            records.append(_strict_json(raw, "T0 JSONL line {}".format(line_number)))
    if not records or records[0].get("record_type") != "run_header":
        raise CampaignError("T0 JSONL is missing its run header")
    header = records[0]
    if header.get("schema") != T0_SCHEMA:
        raise CampaignError("T0 run-header schema mismatch")
    for key, expected in sorted(expected_header.items()):
        if header.get(key) != expected:
            raise CampaignError("T0 run-header {} identity mismatch".format(key))
    resolved_configuration = header.get("resolved_configuration")
    required_configuration = {
        "one_pass_schur": True,
        "fej_enabled": True,
        "global_3d_transient": True,
        "all_cameras_radtan": True,
        "camera_extrinsic_calibration_off": True,
        "camera_intrinsic_calibration_off": True,
        "camera_time_offset_calibration_off": True,
        "stereo_enabled": True,
        "stereo_available": True,
        "require_target_stereo_range": True,
        "camera_count": 2,
    }
    if resolved_configuration != required_configuration:
        raise CampaignError("T0 resolved supported configuration mismatch")
    unsupported_reasons = header.get("unsupported_reasons")
    if not isinstance(unsupported_reasons, list) or not all(
        isinstance(reason, str) for reason in unsupported_reasons
    ) or unsupported_reasons != sorted(set(unsupported_reasons)):
        raise CampaignError("T0 unsupported reasons are not canonical")
    supported = header.get("supported_configuration")
    if not isinstance(supported, bool) or supported != (not unsupported_reasons):
        raise CampaignError("T0 supported-configuration claim is inconsistent")
    if not supported:
        raise CampaignError("fixed KAIST replay is not a supported configuration")

    forbidden_keys = {
        "sequence",
        "sequence_id",
        "sequence_name",
        "bag",
        "bag_path",
        "bag_sha256",
        "ground_truth",
        "path_gt",
        "final_error",
    }

    def inspect(value: Any, label: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.lower() in forbidden_keys:
                    raise CampaignError("runtime T0 record contains forbidden key {}".format(key))
                inspect(child, label + "." + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                inspect(child, "{}[{}]".format(label, index))
        elif isinstance(value, float) and not math.isfinite(value):
            raise CampaignError("{} contains a nonfinite number".format(label))

    def require_object(
        value: Any, label: str, required: Sequence[str]
    ) -> Mapping[str, Any]:
        if not isinstance(value, dict):
            raise CampaignError("{} is not an object".format(label))
        missing = [key for key in required if key not in value]
        if missing:
            raise CampaignError(
                "{} is missing required field {}".format(label, missing[0])
            )
        return value

    def require_array(value: Any, label: str) -> List[Any]:
        if not isinstance(value, list):
            raise CampaignError("{} is not an array".format(label))
        return value

    def require_nonnegative_integer(value: Any, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise CampaignError("{} is not a nonnegative integer".format(label))

    def require_bool(value: Any, label: str) -> None:
        if not isinstance(value, bool):
            raise CampaignError("{} is not a Boolean".format(label))

    def require_string(value: Any, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise CampaignError("{} is not a nonempty string".format(label))

    def require_finite_number(value: Any, label: str) -> None:
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value)):
            raise CampaignError("{} is not a finite number".format(label))

    availability_statuses = {
        "AVAILABLE",
        "NOT_APPLICABLE",
        "NOT_EXPOSED",
        "NOT_AVAILABLE",
        "NOT_AVAILABLE_FROM_SENSOR",
        "UNSUPPORTED_CONFIGURATION",
        "INVALID_INPUT",
        "NONFINITE",
        "NUMERICAL_FAILURE",
    }

    def require_number_or_status(value: Any, label: str) -> None:
        if isinstance(value, bool):
            raise CampaignError("{} has invalid numeric availability".format(label))
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise CampaignError("{} is nonfinite".format(label))
            return
        status = require_object(value, label, ("status", "reason"))
        if status.get("status") not in availability_statuses:
            raise CampaignError("{} has an invalid availability status".format(label))
        require_string(status.get("reason"), label + ".reason")
        if status.get("status") == "AVAILABLE":
            if "value" not in status:
                raise CampaignError(
                    "{} AVAILABLE status is missing numeric value".format(label)
                )
            require_finite_number(status["value"], label + ".value")

    def require_nonnegative_integer_or_status(value: Any, label: str) -> None:
        if isinstance(value, dict):
            require_number_or_status(value, label)
            return
        require_nonnegative_integer(value, label)

    def require_status(value: Any, label: str) -> Mapping[str, Any]:
        status = require_object(value, label, ("status", "reason"))
        if status.get("status") not in availability_statuses:
            raise CampaignError("{} has an invalid availability status".format(label))
        require_string(status.get("reason"), label + ".reason")
        return status

    def require_pixel(value: Any, label: str) -> None:
        if isinstance(value, list):
            if len(value) != 2:
                raise CampaignError("{} is not a two-coordinate pixel".format(label))
            for index, coordinate in enumerate(value):
                require_finite_number(coordinate, "{}[{}]".format(label, index))
            return
        status = require_status(value, label)
        if status.get("status") == "AVAILABLE":
            raise CampaignError(
                "{} AVAILABLE pixel is missing two coordinates".format(label)
            )

    observation_key_fields = (
        "camera_id",
        "timestamp_value",
        "timestamp_key",
        "feature_id",
        "detached_index",
        "observation_ordinal",
    )

    def require_observation_key(value: Any, label: str) -> Mapping[str, Any]:
        key = require_object(value, label, observation_key_fields)
        for field in ("camera_id", "feature_id", "detached_index",
                      "observation_ordinal"):
            require_nonnegative_integer(key[field], label + "." + field)
        require_finite_number(key["timestamp_value"], label + ".timestamp_value")
        require_string(key["timestamp_key"], label + ".timestamp_key")
        return key

    def require_observation_evidence(value: Any, label: str) -> None:
        evidence = require_object(
            value, label, ("observation_key", "raw_pixel", "normalized_pixel")
        )
        require_observation_key(evidence["observation_key"], label + ".observation_key")
        require_pixel(evidence["raw_pixel"], label + ".raw_pixel")
        require_pixel(evidence["normalized_pixel"], label + ".normalized_pixel")

    def require_camera_identity(value: Any, label: str) -> None:
        identity = require_object(value, label, ("status",))
        require_string(identity["status"], label + ".status")
        if identity["status"] == "AVAILABLE":
            require_object(
                identity, label,
                ("camera_id", "model", "intrinsic_id", "extrinsic_id"),
            )
            require_nonnegative_integer(identity["camera_id"], label + ".camera_id")
            require_string(identity["model"], label + ".model")
            require_nonnegative_integer(identity["intrinsic_id"], label + ".intrinsic_id")
            require_nonnegative_integer(identity["extrinsic_id"], label + ".extrinsic_id")
        else:
            require_string(identity.get("reason"), label + ".reason")

    initializer_fields = (
        "attempted",
        "native_success",
        "native_function",
        "native_outcome",
        "condition_number",
        "depth",
        "baseline_ratio",
        "predicates",
        "refinement_runs",
        "refinement_lambda",
        "refinement_last_step_norm",
        "refinement_control_epsilon",
        "termination_reason",
    )
    schur_fields = (
        "attempted",
        "native_accepted",
        "native_status",
        "native_stage",
        "raw_rows",
        "rows_before_reduction",
        "degrees_of_freedom",
        "rows_after_reduction",
        "singular_values",
        "numerical_rank",
        "reciprocal_condition",
        "condition_number",
        "numerical_repairs",
    )
    nis_fields = (
        "attempted",
        "native_stage",
        "lifecycle_accept",
        "degrees_of_freedom",
        "statistic",
        "threshold",
        "decision",
    )
    attempt_fields = (
        "detached_index",
        "feature_id",
        "ordered_observation_keys",
        "attempt_has_any_live_same_camera_clone_pair",
        "attempt_valid_clone_pair_count",
        "triangulation",
        "refinement",
        "schur",
        "full_nis",
        "full_outcome",
        "full_outcome_mapping",
        "native_terminal_status",
        "target_time_stereo_available",
        "accepted_at_native_feature_gate",
        "native_feature_row_count",
        "accepted_full_factor",
        "accepted_full_row_count",
        "accepted_full_row_status",
        "finalization_result",
    )
    candidate_fields = (
        "candidate_key",
        "source_observation_key",
        "source_observation",
        "target_observation_key",
        "target_observation",
        "supporting_target_time_stereo_observation_key",
        "supporting_target_time_stereo_observation",
        "camera_calibration",
        "full_outcome",
        "target_time_stereo_available",
        "target_stereo_rejection_reason",
        "stereo_geometry_primitives",
        "target_bearing",
        "range_certificate",
        "translation_certificate",
        "bearing_covariance",
        "acute_regime",
        "rho_trans",
        "rho_threshold",
        "static_quality_status",
        "eligible_before_group",
        "terminal_reason",
    )
    funnel_fields = (
        "camera_frames",
        "previous_tracked_points",
        "klt_status_survivors",
        "in_bounds_survivors",
        "mask_survivors",
        "native_combined_track_survivors",
        "fmatrix_input_points",
        "fmatrix_survivors",
        "database_observations_written",
        "terminal_attempt_count",
        "tracks_with_valid_clone_pairs",
        "attempt_valid_clone_pair_count_total",
        "full_outcome_counts_by_code",
        "triangulation_outcome_counts_by_code",
        "refinement_outcome_counts_by_code",
        "schur_outcome_counts_by_code",
        "full_nis_outcome_counts_by_code",
        "accepted_full_factors",
        "accepted_full_rows",
        "shadow_pair_candidates",
        "candidate_pairs_with_target_stereo",
        "attempts_with_at_least_one_target_stereo_pair",
        "groups_with_at_least_one_target_stereo_pair",
        "candidates_with_calibrated_range_lcb",
        "candidates_with_calibrated_translation_ucb",
        "candidates_translation_acute",
        "candidates_passing_rho_trans",
        "groups_n_lt_2",
        "groups_n_2",
        "groups_n_3",
        "groups_n_ge_4",
        "groups_passing_rank",
        "groups_passing_consensus",
        "eligible_groups_before_selection",
        "winner_count",
        "foregone_eligible_groups",
        "foregone_eligible_features",
        "winner_shadow_factors_passing_nis",
        "winner_shadow_rows_passing_nis",
    )

    callback_count = 0
    updater_callback_count = 0
    for record_index, record in enumerate(records):
        inspect(record, "record[{}]".format(record_index))
        if record_index == 0:
            continue
        if record.get("schema") != T0_SCHEMA or record.get("record_type") != "callback":
            raise CampaignError("T0 JSONL contains an unknown record type")
        if record.get("callback_index") != callback_count:
            raise CampaignError("T0 callback ordering is not contiguous")
        callback_count += 1
        timestamp = record.get("callback_timestamp_value")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise CampaignError("T0 callback timestamp is unavailable")
        require_object(
            record,
            "T0 callback",
            (
                "callback_timestamp_key",
                "prior_fingerprint",
                "frontend_cameras",
                "full_track_attempts",
                "shadow_candidates",
                "shadow_groups",
                "prior_primitives",
                "baseline_decision",
                "funnel",
                "shadow_summary",
                "completeness",
            ),
        )
        cameras = require_array(record.get("frontend_cameras"), "T0 frontend_cameras")
        camera_fields = (
            "camera_id",
            "source_camera_timestamp",
            "frame_timestamp_value",
            "frame_timestamp_key",
            "reseed_count",
            "klt_attempted_count",
            "klt_counts_available",
            "previous_tracked_points",
            "klt_status_survivors",
            "klt_status_rejections",
            "out_of_bounds_rejections",
            "in_bounds_survivors",
            "mask_rejections",
            "mask_survivors",
            "mask_stage_present",
            "fmatrix_input_points",
            "fmatrix_inliers",
            "fmatrix_rejections",
            "native_combined_track_survivors",
            "database_observations_written",
            "database_tracked_observations_written",
            "database_new_observations_written",
            "reset_too_few_points",
            "reset_native_reason",
            "forward_backward_check",
            "klt_error_summary",
            "gyro_magnitude_rad_s",
            "gyro_integrated_rotation_rad",
            "blur_metric",
            "exposure",
            "gain",
            "native_accepted_feature_ids",
        )
        for camera_index, camera in enumerate(cameras):
            camera = require_object(
                camera,
                "T0 frontend camera {}".format(camera_index),
                camera_fields,
            )
            camera_label = "T0 frontend camera {}".format(camera_index)
            for count_field in (
                "camera_id", "reseed_count", "klt_attempted_count",
                "previous_tracked_points", "klt_status_survivors",
                "klt_status_rejections", "out_of_bounds_rejections",
                "in_bounds_survivors", "mask_rejections", "mask_survivors",
                "fmatrix_input_points", "fmatrix_inliers",
                "fmatrix_rejections", "native_combined_track_survivors",
                "database_observations_written",
            ):
                require_nonnegative_integer(
                    camera[count_field], camera_label + "." + count_field
                )
            for boolean_field in (
                "klt_counts_available", "mask_stage_present",
                "reset_too_few_points",
            ):
                require_bool(
                    camera[boolean_field], camera_label + "." + boolean_field
                )
            require_finite_number(
                camera["frame_timestamp_value"],
                camera_label + ".frame_timestamp_value",
            )
            require_string(
                camera["frame_timestamp_key"], camera_label + ".frame_timestamp_key"
            )
            require_string(
                camera["reset_native_reason"], camera_label + ".reset_native_reason"
            )
            require_status(
                camera["forward_backward_check"],
                camera_label + ".forward_backward_check",
            )
            for optional_numeric in (
                "database_tracked_observations_written",
                "database_new_observations_written", "gyro_magnitude_rad_s",
                "gyro_integrated_rotation_rad", "blur_metric", "exposure", "gain",
            ):
                require_number_or_status(
                    camera[optional_numeric], camera_label + "." + optional_numeric
                )
            accepted_ids = require_array(
                camera["native_accepted_feature_ids"],
                camera_label + ".native_accepted_feature_ids",
            )
            for accepted_index, accepted_id in enumerate(accepted_ids):
                require_nonnegative_integer(
                    accepted_id,
                    "{}.native_accepted_feature_ids[{}]".format(
                        camera_label, accepted_index
                    ),
                )
        camera_ids = [item.get("camera_id") for item in cameras]
        if camera_ids != sorted(camera_ids) or len(camera_ids) != len(set(camera_ids)):
            raise CampaignError("T0 camera ordering is not canonical")
        attempts = require_array(record.get("full_track_attempts"), "T0 full_track_attempts")
        for attempt_index, attempt in enumerate(attempts):
            label = "T0 full-track attempt {}".format(attempt_index)
            attempt = require_object(attempt, label, attempt_fields)
            require_nonnegative_integer(attempt["detached_index"], label + ".detached_index")
            require_nonnegative_integer(attempt["feature_id"], label + ".feature_id")
            require_bool(
                attempt["attempt_has_any_live_same_camera_clone_pair"],
                label + ".attempt_has_any_live_same_camera_clone_pair",
            )
            require_nonnegative_integer(
                attempt["attempt_valid_clone_pair_count"],
                label + ".attempt_valid_clone_pair_count",
            )
            observations = require_array(
                attempt["ordered_observation_keys"],
                label + ".ordered_observation_keys",
            )
            for observation_index, observation in enumerate(observations):
                observation = require_object(
                    observation,
                    "{}.ordered_observation_keys[{}]".format(
                        label, observation_index
                    ),
                    (
                        "camera_id",
                        "timestamp_value",
                        "timestamp_key",
                        "feature_id",
                        "detached_index",
                        "baseline_observation_index",
                        "clone_available",
                        "uv",
                        "uv_normalized",
                    ),
                )
                for field in ("camera_id", "feature_id", "detached_index",
                              "baseline_observation_index"):
                    require_nonnegative_integer(
                        observation[field],
                        "{}.ordered_observation_keys[{}].{}".format(
                            label, observation_index, field
                        ),
                    )
                require_finite_number(
                    observation["timestamp_value"],
                    "{}.ordered_observation_keys[{}].timestamp_value".format(
                        label, observation_index
                    ),
                )
                require_string(
                    observation["timestamp_key"],
                    "{}.ordered_observation_keys[{}].timestamp_key".format(
                        label, observation_index
                    ),
                )
                require_bool(
                    observation["clone_available"],
                    "{}.ordered_observation_keys[{}].clone_available".format(
                        label, observation_index
                    ),
                )
                require_pixel(
                    observation["uv"],
                    "{}.ordered_observation_keys[{}].uv".format(
                        label, observation_index
                    ),
                )
                require_pixel(
                    observation["uv_normalized"],
                    "{}.ordered_observation_keys[{}].uv_normalized".format(
                        label, observation_index
                    ),
                )
            for stage_name in ("triangulation", "refinement"):
                stage = require_object(
                    attempt[stage_name], label + "." + stage_name,
                    initializer_fields,
                )
                require_object(
                    stage["predicates"],
                    label + "." + stage_name + ".predicates",
                    (
                        "ill_conditioned",
                        "too_near_or_behind",
                        "too_far",
                        "baseline_ratio",
                        "native_nan",
                    ),
                )
                require_bool(stage["attempted"], label + "." + stage_name + ".attempted")
                require_bool(stage["native_success"], label + "." + stage_name + ".native_success")
                require_string(stage["native_function"], label + "." + stage_name + ".native_function")
                require_string(stage["native_outcome"], label + "." + stage_name + ".native_outcome")
                for numeric_field in (
                    "condition_number", "depth", "baseline_ratio",
                    "refinement_lambda", "refinement_last_step_norm",
                    "refinement_control_epsilon",
                ):
                    require_number_or_status(
                        stage[numeric_field],
                        label + "." + stage_name + "." + numeric_field,
                    )
                require_nonnegative_integer_or_status(
                    stage["refinement_runs"],
                    label + "." + stage_name + ".refinement_runs",
                )
                for predicate_name, predicate in stage["predicates"].items():
                    require_bool(
                        predicate,
                        label + "." + stage_name + ".predicates." + predicate_name,
                    )
                require_string(
                    stage["termination_reason"],
                    label + "." + stage_name + ".termination_reason",
                )
            schur = require_object(
                attempt["schur"], label + ".schur", schur_fields
            )
            require_bool(schur["attempted"], label + ".schur.attempted")
            require_bool(schur["native_accepted"], label + ".schur.native_accepted")
            require_string(schur["native_status"], label + ".schur.native_status")
            require_string(schur["native_stage"], label + ".schur.native_stage")
            for integer_field in (
                "raw_rows", "rows_before_reduction", "degrees_of_freedom",
                "rows_after_reduction", "numerical_rank",
            ):
                require_nonnegative_integer_or_status(
                    schur[integer_field], label + ".schur." + integer_field
                )
            singular_values = schur["singular_values"]
            if isinstance(singular_values, list):
                if len(singular_values) != 3:
                    raise CampaignError(
                        "{}.schur.singular_values is not a three-value array".format(label)
                    )
                for value_index, value in enumerate(singular_values):
                    require_finite_number(
                        value,
                        "{}.schur.singular_values[{}]".format(label, value_index),
                    )
            else:
                require_status(singular_values, label + ".schur.singular_values")
            for numeric_field in ("reciprocal_condition", "condition_number"):
                require_number_or_status(
                    schur[numeric_field], label + ".schur." + numeric_field
                )
            repairs = require_object(
                schur["numerical_repairs"], label + ".schur.numerical_repairs",
                ("jitter", "clamp", "regularization", "fallback"),
            )
            for repair_name, repair_count in repairs.items():
                require_nonnegative_integer(
                    repair_count,
                    label + ".schur.numerical_repairs." + repair_name,
                )
            full_nis = require_object(
                attempt["full_nis"], label + ".full_nis", nis_fields
            )
            require_bool(full_nis["attempted"], label + ".full_nis.attempted")
            require_bool(
                full_nis["lifecycle_accept"], label + ".full_nis.lifecycle_accept"
            )
            require_string(full_nis["native_stage"], label + ".full_nis.native_stage")
            require_string(full_nis["decision"], label + ".full_nis.decision")
            require_nonnegative_integer_or_status(
                full_nis["degrees_of_freedom"],
                label + ".full_nis.degrees_of_freedom",
            )
            for numeric_field in ("statistic", "threshold"):
                require_number_or_status(
                    full_nis[numeric_field], label + ".full_nis." + numeric_field
                )
            require_number_or_status(
                attempt["native_feature_row_count"],
                label + ".native_feature_row_count",
            )
            require_nonnegative_integer(
                attempt["accepted_full_row_count"],
                label + ".accepted_full_row_count",
            )
            for boolean_field in (
                "target_time_stereo_available", "accepted_at_native_feature_gate",
                "accepted_full_factor",
            ):
                require_bool(attempt[boolean_field], label + "." + boolean_field)
            for string_field in (
                "full_outcome", "full_outcome_mapping", "native_terminal_status",
                "accepted_full_row_status", "finalization_result",
            ):
                require_string(attempt[string_field], label + "." + string_field)
        detached = [item.get("detached_index") for item in attempts]
        if detached != sorted(detached) or len(detached) != len(set(detached)):
            raise CampaignError("T0 detached-attempt ordering is not canonical")
        baseline = record.get("baseline_decision")
        baseline = require_object(
            baseline,
            "T0 baseline decision",
            (
                "native_status",
                "native_subreason",
                "accepted_full_feature_ids",
                "proposal_sufficient_statistics",
                "proposal_attempted",
                "proposal_accepted",
                "baseline_commit_occurred",
                "no_full_visual_update_duration",
                "mean_commit_count",
                "covariance_commit_count",
                "terminal_finalization_count",
                "nominal_state_fingerprint",
                "covariance_fingerprint",
            ),
        )
        if baseline.get("native_status") != "UPDATER_NOT_REACHED":
            updater_callback_count += 1
        require_string(baseline["native_status"], "T0 baseline decision.native_status")
        require_string(
            baseline["native_subreason"], "T0 baseline decision.native_subreason"
        )
        accepted_full_ids = require_array(
            baseline["accepted_full_feature_ids"],
            "T0 baseline decision.accepted_full_feature_ids",
        )
        for accepted_index, accepted_id in enumerate(accepted_full_ids):
            require_nonnegative_integer(
                accepted_id,
                "T0 baseline decision.accepted_full_feature_ids[{}]".format(
                    accepted_index
                ),
            )
        proposal = require_object(
            baseline["proposal_sufficient_statistics"],
            "T0 baseline decision.proposal_sufficient_statistics",
            ("gamma", "precompression_rows", "compressed_rows"),
        )
        require_number_or_status(
            proposal["gamma"],
            "T0 baseline decision.proposal_sufficient_statistics.gamma",
        )
        for row_field in ("precompression_rows", "compressed_rows"):
            require_nonnegative_integer_or_status(
                proposal[row_field],
                "T0 baseline decision.proposal_sufficient_statistics." + row_field,
            )
        for boolean_field in (
            "proposal_attempted", "proposal_accepted", "baseline_commit_occurred",
        ):
            require_bool(
                baseline[boolean_field],
                "T0 baseline decision." + boolean_field,
            )
        require_number_or_status(
            baseline["no_full_visual_update_duration"],
            "T0 baseline decision.no_full_visual_update_duration",
        )
        for counter in (
            "mean_commit_count",
            "covariance_commit_count",
            "terminal_finalization_count",
        ):
            value = baseline.get(counter)
            if not isinstance(value, int) or value < 0 or value > 1:
                raise CampaignError("T0 {} violates the one-boundary invariant".format(counter))
        if baseline.get("mean_commit_count") != baseline.get("covariance_commit_count"):
            raise CampaignError("T0 mean/covariance commit counts disagree")
        candidates = require_array(record.get("shadow_candidates"), "T0 shadow_candidates")
        candidate_keys = []
        for candidate_index, candidate in enumerate(candidates):
            label = "T0 shadow candidate {}".format(candidate_index)
            candidate = require_object(candidate, label, candidate_fields)
            for observation_name in (
                "source_observation_key",
                "target_observation_key",
            ):
                require_observation_key(
                    candidate[observation_name],
                    label + "." + observation_name,
                )
            for evidence_name in ("source_observation", "target_observation"):
                require_observation_evidence(
                    candidate[evidence_name],
                    label + "." + evidence_name,
                )
            support_key_name = "supporting_target_time_stereo_observation_key"
            support_key = require_object(
                candidate[support_key_name], label + "." + support_key_name, ()
            )
            if "status" in support_key:
                support_status = require_status(
                    support_key, label + "." + support_key_name
                )
                if support_status.get("status") == "AVAILABLE":
                    raise CampaignError(
                        "{}.{} AVAILABLE status is missing observation key".format(
                            label, support_key_name
                        )
                    )
            else:
                require_observation_key(
                    support_key, label + "." + support_key_name
                )
            support_evidence_name = "supporting_target_time_stereo_observation"
            support_evidence = require_object(
                candidate[support_evidence_name],
                label + "." + support_evidence_name,
                (),
            )
            if "status" in support_evidence:
                support_status = require_status(
                    support_evidence, label + "." + support_evidence_name
                )
                if support_status.get("status") == "AVAILABLE":
                    raise CampaignError(
                        "{}.{} AVAILABLE status is missing pixel evidence".format(
                            label, support_evidence_name
                        )
                    )
            else:
                require_observation_evidence(
                    support_evidence, label + "." + support_evidence_name
                )
            calibration = require_object(
                candidate["camera_calibration"],
                label + ".camera_calibration",
                ("source", "target", "stereo"),
            )
            for calibration_name in ("source", "target", "stereo"):
                require_camera_identity(
                    calibration[calibration_name],
                    label + ".camera_calibration." + calibration_name,
                )
            for diagnostic_name in (
                "stereo_geometry_primitives", "target_bearing",
                "range_certificate", "translation_certificate",
                "bearing_covariance", "acute_regime", "rho_trans",
                "rho_threshold",
            ):
                require_status(
                    candidate[diagnostic_name], label + "." + diagnostic_name
                )
            require_bool(
                candidate["target_time_stereo_available"],
                label + ".target_time_stereo_available",
            )
            require_bool(
                candidate["eligible_before_group"],
                label + ".eligible_before_group",
            )
            for string_field in (
                "full_outcome", "target_stereo_rejection_reason",
                "static_quality_status",
            ):
                require_string(candidate[string_field], label + "." + string_field)
            validate_stable_terminal_reason(
                candidate["terminal_reason"],
                label + ".terminal_reason",
                stage="candidate",
            )
            key = require_object(
                candidate["candidate_key"],
                label + ".candidate_key",
                (
                    "camera_id",
                    "source_timestamp_key",
                    "target_timestamp_key",
                    "feature_id",
                    "detached_index",
                    "source_observation_ordinal",
                    "target_observation_ordinal",
                ),
            )
            candidate_keys.append(
                (
                    key.get("camera_id"),
                    key.get("source_timestamp_key"),
                    key.get("target_timestamp_key"),
                    key.get("feature_id"),
                    key.get("detached_index"),
                    key.get("source_observation_ordinal"),
                    key.get("target_observation_ordinal"),
                )
            )
        if (candidate_keys != sorted(candidate_keys) or
                len(candidate_keys) != len(set(candidate_keys))):
            raise CampaignError("T0 shadow candidate ordering is not canonical")
        groups = require_array(record.get("shadow_groups"), "T0 shadow_groups")
        pair_keys = []
        grouped_candidate_keys = []
        group_fields = (
            "pair_key",
            "candidate_keys",
            "eligible_candidate_keys",
            "raw_candidate_count",
            "pair_group_feature_count",
            "eligible_feature_count",
            "shadow_only_small_group",
            "group_size_status",
            "contains_target_stereo_candidate",
            "target_bearing_spatial_metrics",
            "rotation_stack_singular_values",
            "rotation_stack_rank",
            "consensus",
            "eligible_before_selection",
            "predicted_rotation_angle",
            "predicted_information",
            "score",
            "selection_role",
            "foregone_reason",
            "winner_shadow_nis",
            "post_nis_count_rank_status",
        )
        for group_index, group in enumerate(groups):
            group = require_object(
                group, "T0 shadow group {}".format(group_index), group_fields
            )
            pair_key = require_object(
                group["pair_key"],
                "T0 shadow group {}.pair_key".format(group_index),
                ("camera_id", "source_timestamp_key", "target_timestamp_key"),
            )
            require_nonnegative_integer(
                pair_key["camera_id"],
                "T0 shadow group {}.pair_key.camera_id".format(group_index),
            )
            require_string(
                pair_key["source_timestamp_key"],
                "T0 shadow group {}.pair_key.source_timestamp_key".format(group_index),
            )
            require_string(
                pair_key["target_timestamp_key"],
                "T0 shadow group {}.pair_key.target_timestamp_key".format(group_index),
            )
            pair_keys.append(
                (
                    pair_key.get("camera_id"),
                    pair_key.get("source_timestamp_key"),
                    pair_key.get("target_timestamp_key"),
                )
            )
            members = group.get("candidate_keys")
            if not isinstance(members, list):
                raise CampaignError("T0 group candidate_keys is not an array")
            member_keys = [
                (
                    key.get("camera_id"),
                    key.get("source_timestamp_key"),
                    key.get("target_timestamp_key"),
                    key.get("feature_id"),
                    key.get("detached_index"),
                    key.get("source_observation_ordinal"),
                    key.get("target_observation_ordinal"),
                )
                for key in members
            ]
            if (member_keys != sorted(member_keys) or
                    len(member_keys) != len(set(member_keys))):
                raise CampaignError("T0 group membership is not canonical")
            grouped_candidate_keys.extend(member_keys)
            eligible_members = require_array(
                group["eligible_candidate_keys"],
                "T0 shadow group {}.eligible_candidate_keys".format(group_index),
            )
            for eligible_index, eligible_key in enumerate(eligible_members):
                require_object(
                    eligible_key,
                    "T0 shadow group {}.eligible_candidate_keys[{}]".format(
                        group_index, eligible_index
                    ),
                    (
                        "camera_id", "source_timestamp_key", "target_timestamp_key",
                        "feature_id", "detached_index",
                        "source_observation_ordinal", "target_observation_ordinal",
                    ),
                )
            for count_field in (
                "raw_candidate_count", "pair_group_feature_count",
                "eligible_feature_count",
            ):
                require_nonnegative_integer(
                    group[count_field],
                    "T0 shadow group {}.{}".format(group_index, count_field),
                )
            for boolean_field in (
                "shadow_only_small_group", "contains_target_stereo_candidate",
                "eligible_before_selection",
            ):
                require_bool(
                    group[boolean_field],
                    "T0 shadow group {}.{}".format(group_index, boolean_field),
                )
            for string_field in ("group_size_status", "selection_role"):
                require_string(
                    group[string_field],
                    "T0 shadow group {}.{}".format(group_index, string_field),
                )
            if group["selection_role"] not in (
                "WINNER", "FOREGONE_ELIGIBLE", "INELIGIBLE",
            ):
                raise CampaignError(
                    "T0 shadow group {} has an invalid selection role".format(
                        group_index
                    )
                )
            foregone_label = "T0 shadow group {}.foregone_reason".format(
                group_index
            )
            require_string(group["foregone_reason"], foregone_label)
            if group["selection_role"] == "FOREGONE_ELIGIBLE":
                validate_stable_terminal_reason(
                    group["foregone_reason"],
                    foregone_label,
                    stage="group_foregone",
                )
            elif group["foregone_reason"] != "NOT_APPLICABLE":
                raise CampaignError(
                    "{} is not applicable to this selection role".format(
                        foregone_label
                    )
                )
            for status_field in (
                "target_bearing_spatial_metrics", "rotation_stack_singular_values",
                "rotation_stack_rank", "predicted_rotation_angle",
                "predicted_information", "score", "winner_shadow_nis",
                "post_nis_count_rank_status",
            ):
                require_status(
                    group[status_field],
                    "T0 shadow group {}.{}".format(group_index, status_field),
                )
            consensus_label = "T0 shadow group {}.consensus".format(group_index)
            consensus = require_status(group["consensus"], consensus_label)
            validate_stable_terminal_reason(
                consensus["reason"],
                consensus_label + ".reason",
                stage="group_consensus",
            )
            if "terminal_reason" in consensus:
                validate_stable_terminal_reason(
                    consensus["terminal_reason"],
                    consensus_label + ".terminal_reason",
                    stage="group_consensus",
                )
        if pair_keys != sorted(pair_keys) or len(pair_keys) != len(set(pair_keys)):
            raise CampaignError("T0 shadow group ordering is not canonical")
        if sorted(grouped_candidate_keys) != candidate_keys:
            raise CampaignError("T0 group membership differs from candidates")
        prior = require_object(
            record["prior_primitives"], "T0 prior_primitives", ("status", "reason")
        )
        if prior.get("status") == "AVAILABLE":
            require_object(
                prior,
                "T0 prior_primitives",
                ("clones", "pair_covariances", "cameras", "covariance_dimension"),
            )
        funnel = require_object(record["funnel"], "T0 funnel", funnel_fields)
        for key in funnel_fields:
            if key.endswith("_by_code"):
                if not isinstance(funnel[key], dict):
                    raise CampaignError("T0 funnel.{} is not an object".format(key))
                for outcome, count in funnel[key].items():
                    require_string(outcome, "T0 funnel.{} outcome".format(key))
                    require_nonnegative_integer(
                        count, "T0 funnel.{}.{}".format(key, outcome)
                    )
            else:
                require_nonnegative_integer(funnel[key], "T0 funnel." + key)
        summary = require_object(
            record["shadow_summary"],
            "T0 shadow_summary",
            (
                "eligible_group_count",
                "frozen_winner",
                "runner_up",
                "foregone_eligible_groups",
                "foregone_eligible_features",
                "no_runner_up_gate_shopping",
                "translation_covariance_scale",
            ),
        )
        for count_field in (
            "eligible_group_count", "foregone_eligible_groups",
            "foregone_eligible_features",
        ):
            require_nonnegative_integer(
                summary[count_field], "T0 shadow_summary." + count_field
            )
        require_bool(
            summary["no_runner_up_gate_shopping"],
            "T0 shadow_summary.no_runner_up_gate_shopping",
        )
        require_finite_number(
            summary["translation_covariance_scale"],
            "T0 shadow_summary.translation_covariance_scale",
        )
        if "terminal_reason" in summary:
            validate_stable_terminal_reason(
                summary["terminal_reason"],
                "T0 shadow_summary.terminal_reason",
            )
        completeness = require_object(
            record["completeness"],
            "T0 completeness",
            ("status", "typed_reasons"),
        )
        require_string(completeness["status"], "T0 completeness.status")
        typed_reasons = require_array(
            completeness["typed_reasons"], "T0 completeness.typed_reasons"
        )
        for reason_index, reason in enumerate(typed_reasons):
            require_string(
                reason,
                "T0 completeness.typed_reasons[{}]".format(reason_index),
            )
    if callback_count == 0:
        raise CampaignError("T0 JSONL contains no camera callbacks")
    return {
        "schema": T0_SCHEMA,
        "record_count": len(records),
        "callback_count": callback_count,
        "updater_callback_count": updater_callback_count,
        "identity": file_identity(path),
        "runtime_sequence_identity_absent": True,
        "finite_json": True,
        "deterministic_ordering_validated": True,
        "required_schema_fields_validated": True,
        "single_commit_boundary_validated": True,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_forbidden_path(path: Path, label: str) -> None:
    def reject_components(candidate: Path) -> None:
        lowered = [component.lower() for component in candidate.parts]
        for component in lowered:
            if any(word in component for word in FORBIDDEN_COMPONENTS):
                raise CampaignError(
                    "{} contains a forbidden PRIVATE/HOLDOUT component: {}".format(
                        label, path
                    )
                )
        for index in range(len(lowered) - 1):
            if lowered[index : index + 2] == ["scripts", "cp2"]:
                raise CampaignError("{} enters protected scripts/cp2: {}".format(label, path))

    # Reject lexical components before resolving so a hostile embedded path is
    # never stat'ed, traversed, or hashed merely to discover that it is banned.
    reject_components(path)
    reject_components(path.resolve(strict=False))


def _regular_file(path: Path, label: str, executable: bool = False) -> Path:
    _reject_forbidden_path(path, label)
    resolved = path.resolve(strict=False)
    if not resolved.is_file():
        raise CampaignError("{} is not a regular file: {}".format(label, resolved))
    if executable and not os.access(str(resolved), os.X_OK):
        raise CampaignError("{} is not executable: {}".format(label, resolved))
    return resolved


def _command_path(name: str) -> Path:
    resolved = shutil.which(name)
    if resolved is None:
        raise CampaignError("required command is unavailable: {}".format(name))
    return _regular_file(Path(resolved), name, executable=True)


def _reference_for_sequence(sequence: str) -> Path:
    return REPO_ROOT / "ov_data" / "kaist_vio" / (Path(sequence).stem + ".txt")


def _validate_paths(args: argparse.Namespace) -> Dict[str, Path]:
    source = _regular_file(args.source_bag, "source bag")
    adapted = args.adapted_bag.resolve(strict=False)
    _reject_forbidden_path(adapted, "adapted bag")
    config = _regular_file(args.config, "configuration")
    launch = _regular_file(args.launch, "launch file")
    binary = _regular_file(args.binary, "estimator binary", executable=True)
    reference = _regular_file(args.reference_tum, "reference trajectory")
    output = args.output_dir.resolve(strict=False)
    _reject_forbidden_path(output, "output directory")

    data_root = DATA_ROOT.resolve(strict=False)
    if not _is_within(source, data_root) or not _is_within(adapted, data_root):
        raise CampaignError("source and adapted bags must remain below {}".format(data_root))
    if source == adapted:
        raise CampaignError("source and adapted bag paths must differ")
    if tuple(source.parts[-2:]) != tuple(Path(args.sequence).parts):
        raise CampaignError(
            "source bag suffix does not match sequence {}: {}".format(
                args.sequence, source
            )
        )
    if tuple(adapted.parts[-2:]) != tuple(Path(args.sequence).parts):
        raise CampaignError(
            "adapted bag suffix does not match sequence {}: {}".format(
                args.sequence, adapted
            )
        )

    if launch != FIXED_LAUNCH.resolve():
        raise CampaignError("launch path is not the fixed KAIST launch: {}".format(launch))
    if config != FIXED_CONFIG.resolve():
        raise CampaignError("config path is not the fixed KAIST configuration: {}".format(config))
    expected_reference = _reference_for_sequence(args.sequence).resolve()
    if reference != expected_reference:
        raise CampaignError(
            "reference path does not match sequence; expected {}".format(
                expected_reference
            )
        )
    if not _is_within(binary, REPO_ROOT.resolve()):
        raise CampaignError("estimator binary must be a build input below the worktree")

    allowed_outputs = (data_root, ARTIFACT_ROOT.resolve(strict=False))
    if not any(_is_within(output, root) for root in allowed_outputs):
        raise CampaignError(
            "output directory must be below {} or {}".format(*allowed_outputs)
        )
    if output.exists():
        raise CampaignError("refusing to overwrite output directory: {}".format(output))
    if adapted.exists() and not adapted.is_file():
        raise CampaignError("adapted path exists but is not a regular file: {}".format(adapted))

    return {
        "source_bag": source,
        "adapted_bag": adapted,
        "config": config,
        "launch": launch,
        "binary": binary,
        "reference_tum": reference,
        "output_dir": output,
    }


def _minimal_environment(output_dir: Path, ros_port: Optional[int]) -> Dict[str, str]:
    allowed = {
        "CMAKE_PREFIX_PATH",
        "HOME",
        "LD_LIBRARY_PATH",
        "LOGNAME",
        "PATH",
        "PKG_CONFIG_PATH",
        "PYTHONPATH",
        "ROS_DISTRO",
        "ROS_ETC_DIR",
        "ROS_PACKAGE_PATH",
        "ROS_PYTHON_VERSION",
        "ROS_ROOT",
        "ROS_VERSION",
        "SHELL",
        "TMPDIR",
        "USER",
    }
    environment = {
        key: os.environ[key] for key in sorted(allowed) if key in os.environ
    }
    environment.update(FIXED_ENVIRONMENT)
    environment["ROS_HOME"] = str(output_dir / "ros-home")
    environment["ROS_LOG_DIR"] = str(output_dir / "ros-logs")
    environment["ROS_HOSTNAME"] = "127.0.0.1"
    environment["ROS_IP"] = "127.0.0.1"
    if ros_port is not None:
        environment["ROS_MASTER_URI"] = "http://127.0.0.1:{}".format(ros_port)
    return dict(sorted(environment.items()))


def _assert_port_available(port: int) -> None:
    if port < 1024 or port > 65535:
        raise CampaignError("ROS port must be in [1024, 65535]")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise CampaignError("ROS port {} is unavailable: {}".format(port, exc)) from exc
    finally:
        probe.close()


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _clean_process_group(process: subprocess.Popen) -> Tuple[List[str], bool]:
    signals_sent: List[str] = []
    for stop_signal, grace in (
        (signal.SIGINT, 5.0),
        (signal.SIGTERM, 3.0),
        (signal.SIGKILL, 2.0),
    ):
        if not _process_group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, stop_signal)
            signals_sent.append(signal.Signals(stop_signal).name)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    return signals_sent, _process_group_exists(process.pid)


def run_command(
    argv: Sequence[str],
    log_path: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> Dict[str, Any]:
    """Run one argv in a new process group and retain a complete combined log."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CampaignError("subprocess timeout must be finite and positive")
    started_utc = utc_now()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    error: Optional[str] = None
    exit_code: Optional[int] = None
    signals_sent: List[str] = []
    surviving_group = False
    with log_path.open("x", encoding="utf-8", errors="replace") as stream:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(REPO_ROOT),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                signals_sent, surviving_group = _clean_process_group(process)
                exit_code = process.poll()
            except KeyboardInterrupt:
                interrupted = True
                signals_sent, surviving_group = _clean_process_group(process)
                exit_code = process.poll()
                raise
            else:
                if _process_group_exists(process.pid):
                    extra_signals, surviving_group = _clean_process_group(process)
                    signals_sent.extend(extra_signals)
        except OSError as exc:
            error = str(exc)
            stream.write("failed to execute command: {}\n".format(error))
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "argv": list(argv),
        "shell": shlex.join(list(argv)),
        "cwd": str(REPO_ROOT),
        "environment": dict(environment),
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "timeout_seconds": timeout_seconds,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "signals_sent": signals_sent,
        "process_group_survived_cleanup": surviving_group,
        "error": error,
        "log": str(log_path),
        "log_sha256": sha256_file(log_path),
        "log_size_bytes": log_path.stat().st_size,
    }


def _command_succeeded(record: Mapping[str, Any]) -> bool:
    return (
        record.get("exit_code") == 0
        and not record.get("timed_out", False)
        and not record.get("process_group_survived_cleanup", False)
        and not record.get("signals_sent", [])
        and record.get("error") is None
    )


def _validate_numeric_table(
    path: Path, separator: Optional[str], minimum_columns: int
) -> Dict[str, Any]:
    if not path.is_file():
        raise CampaignError("required numeric output is missing: {}".format(path))
    previous = -math.inf
    rows = 0
    columns: Optional[int] = None
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split(separator) if separator is not None else stripped.split()
        fields = [field.strip() for field in fields]
        if len(fields) < minimum_columns:
            raise CampaignError(
                "{}:{} has {} columns, expected at least {}".format(
                    path, line_number, len(fields), minimum_columns
                )
            )
        try:
            values = [float(field) for field in fields]
        except ValueError as exc:
            raise CampaignError("{}:{} contains a non-numeric value".format(path, line_number)) from exc
        if not all(math.isfinite(value) for value in values):
            raise CampaignError("{}:{} contains a non-finite value".format(path, line_number))
        if values[0] <= previous:
            raise CampaignError("{}:{} timestamps are not strictly increasing".format(path, line_number))
        if columns is None:
            columns = len(fields)
        elif len(fields) != columns:
            raise CampaignError("{}:{} has an inconsistent column count".format(path, line_number))
        previous = values[0]
        rows += 1
    if rows == 0:
        raise CampaignError("numeric output contains no rows: {}".format(path))
    return {
        "path": str(path),
        "rows": rows,
        "columns": columns,
        "timestamps_strictly_increasing": True,
        "all_values_finite": True,
        "first_timestamp": _first_timestamp(path, separator),
        "last_timestamp": previous,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _first_timestamp(path: Path, separator: Optional[str]) -> float:
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split(separator) if separator is not None else stripped.split()
            return float(fields[0].strip())
    raise CampaignError("numeric output contains no rows: {}".format(path))


def _timestamps(path: Path, separator: Optional[str]) -> List[float]:
    result: List[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split(separator) if separator is not None else stripped.split()
            result.append(float(fields[0].strip()))
    return result


def _validate_output_consistency(
    state: Path, deviation: Path, timing: Path, trajectory: Path
) -> Dict[str, Any]:
    state_times = _timestamps(state, None)
    deviation_times = _timestamps(deviation, None)
    timing_times = _timestamps(timing, ",")
    trajectory_times = _timestamps(trajectory, None)
    if state_times != deviation_times:
        raise CampaignError("state and deviation timestamp sequences differ")
    if state_times != trajectory_times:
        raise CampaignError("state and TUM trajectory timestamp sequences differ")
    if len(timing_times) != len(state_times):
        raise CampaignError(
            "timing row count {} differs from callback/state row count {}".format(
                len(timing_times), len(state_times)
            )
        )
    differences = [
        abs(timing_stamp - state_stamp)
        for timing_stamp, state_stamp in zip(timing_times, state_times)
    ]
    maximum_difference = max(differences)
    if maximum_difference > 1e-5:
        raise CampaignError(
            "timing/state timestamp difference {:.12g} exceeds 1e-5 seconds".format(
                maximum_difference
            )
        )
    return {
        "state_deviation_timestamp_sequences_equal": True,
        "state_trajectory_timestamp_sequences_equal": True,
        "callback_state_rows": len(state_times),
        "timing_rows": len(timing_times),
        "callback_timing_row_counts_equal": True,
        "timing_state_timestamp_tolerance_seconds": 1e-5,
        "timing_state_maximum_absolute_difference_seconds": maximum_difference,
        "timing_state_timestamps_within_tolerance": True,
    }


def _npy_element_count(data: bytes) -> int:
    stream = memoryview(data)
    if bytes(stream[:6]) != b"\x93NUMPY":
        raise CampaignError("metric archive contains invalid NumPy data")
    major, minor = struct.unpack("BB", stream[6:8])
    if (major, minor) == (1, 0):
        header_length = struct.unpack("<H", stream[8:10])[0]
        header_start = 10
    elif major in (2, 3):
        header_length = struct.unpack("<I", stream[8:12])[0]
        header_start = 12
    else:
        raise CampaignError("metric archive uses unsupported NumPy version")
    header = ast.literal_eval(
        bytes(stream[header_start : header_start + header_length])
        .decode("latin1")
        .strip()
    )
    shape = header.get("shape")
    if not isinstance(shape, tuple):
        raise CampaignError("metric error array has no shape")
    count = 1
    for dimension in shape:
        count *= int(dimension)
    return count


def _metric_archive(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise CampaignError("metric archive is missing: {}".format(path))
    try:
        with zipfile.ZipFile(str(path), "r") as archive:
            stats = json.loads(archive.read("stats.json").decode("utf-8"))
            info = json.loads(archive.read("info.json").decode("utf-8"))
            samples = _npy_element_count(archive.read("error_array.npy"))
    except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise CampaignError("invalid evo result archive {}: {}".format(path, exc)) from exc
    for key, value in stats.items():
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise CampaignError("metric {} is non-finite in {}".format(key, path))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "samples": samples,
        "stats": stats,
        "title": info.get("title"),
        "label": info.get("label"),
    }


def _console_diagnostics(path: Path) -> Dict[str, Any]:
    patterns = {
        "reset_lines": re.compile(r"\breset(?:s|ting)?\b", re.IGNORECASE),
        "nonfinite_lines": re.compile(
            r"non[-_ ]?finite|(?<![A-Za-z])nan(?![A-Za-z])|(?<![A-Za-z])inf(?:inity)?(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "unpaired_warning_lines": re.compile(
            r"unpaired|not[ -]paired|unable to find stereo pair|stereo.*(?:skew|sync|drop)",
            re.IGNORECASE,
        ),
        "dropped_message_lines": re.compile(r"\bdrop(?:ped|ping)?\b", re.IGNORECASE),
    }
    counts = dict((name, 0) for name in patterns)
    samples: Dict[str, List[str]] = dict((name, []) for name in patterns)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for name, pattern in patterns.items():
            if pattern.search(raw):
                counts[name] += 1
                if len(samples[name]) < 10:
                    samples[name].append(raw[:1000])
    return {"counts": counts, "samples": samples}


def _nonnegative_audit_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignError(
            "adapted audit {} is not a nonnegative integer: {!r}".format(
                label, value
            )
        )
    return value


def _bind_exact_header_runtime_summary(
    path: Path, adapted_audit: Mapping[str, Any]
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = [line for line in lines if SERIAL_KAIST_SUMMARY_PREFIX in line]
    if len(candidates) != 1:
        raise CampaignError(
            "expected exactly one SERIAL-KAIST exact-header summary line; found {}".format(
                len(candidates)
            )
        )
    line = ANSI_CONTROL_PATTERN.sub("", candidates[0])
    match = SERIAL_KAIST_SUMMARY_PATTERN.fullmatch(line)
    if match is None:
        raise CampaignError("malformed SERIAL-KAIST exact-header summary: {}".format(line))
    observed = dict((name, int(value)) for name, value in match.groupdict().items())

    stereo = adapted_audit.get("stereo")
    if not isinstance(stereo, Mapping):
        raise CampaignError("adapted audit has no stereo mapping")
    skew = stereo.get("matched_pair_record_time_absolute_skew")
    if not isinstance(skew, Mapping):
        raise CampaignError("adapted audit has no matched-pair record-skew mapping")
    threshold_ns = _nonnegative_audit_integer(
        stereo.get("record_skew_threshold_ns"), "stereo.record_skew_threshold_ns"
    )
    if threshold_ns != 20_000_000:
        raise CampaignError(
            "adapted audit record-skew threshold is {}, not 20 ms".format(
                threshold_ns
            )
        )
    expected = {
        "exact_header_pairs": _nonnegative_audit_integer(
            stereo.get("exact_header_pair_count"),
            "stereo.exact_header_pair_count",
        ),
        "camera0_without_match": _nonnegative_audit_integer(
            stereo.get("camera0_unmatched_count"),
            "stereo.camera0_unmatched_count",
        ),
        "camera1_without_match": _nonnegative_audit_integer(
            stereo.get("camera1_unmatched_count"),
            "stereo.camera1_unmatched_count",
        ),
        "record_delta_ge_20ms": _nonnegative_audit_integer(
            stereo.get("matched_pair_record_skew_at_or_above_threshold_count"),
            "stereo.matched_pair_record_skew_at_or_above_threshold_count",
        ),
        "maximum_record_delta_ns": _nonnegative_audit_integer(
            skew.get("max_ns"),
            "stereo.matched_pair_record_time_absolute_skew.max_ns",
        ),
    }
    mismatches = [
        "{}: runtime={} audit={}".format(name, observed[name], expected[name])
        for name in expected
        if observed[name] != expected[name]
    ]
    if mismatches:
        raise CampaignError(
            "SERIAL-KAIST exact-header summary disagrees with adapted audit: {}".format(
                "; ".join(mismatches)
            )
        )
    return {
        "line": line,
        "observed": observed,
        "expected_from_adapted_audit": expected,
        "adapted_audit_record_skew_threshold_ns": threshold_ns,
        "matches_adapted_audit": True,
    }


def _bind_camera_enqueue_runtime_summary(
    path: Path, exact_header_pairs: int
) -> Dict[str, Any]:
    if isinstance(exact_header_pairs, bool) or not isinstance(
        exact_header_pairs, int
    ) or exact_header_pairs < 0:
        raise CampaignError(
            "exact-header runtime pair count is not a nonnegative integer: {!r}".format(
                exact_header_pairs
            )
        )
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = [line for line in lines if CAMERA_ENQUEUE_SUMMARY_PREFIX in line]
    if len(candidates) != 1:
        raise CampaignError(
            "expected exactly one SERIAL-KAIST camera-enqueue summary line; found {}".format(
                len(candidates)
            )
        )
    line = ANSI_CONTROL_PATTERN.sub("", candidates[0])
    match = CAMERA_ENQUEUE_SUMMARY_PATTERN.fullmatch(line)
    if match is None:
        raise CampaignError("malformed SERIAL-KAIST camera-enqueue summary: {}".format(line))
    observed = dict((name, int(value)) for name, value in match.groupdict().items())
    accounted_pairs = (
        observed["queued_pairs"] + observed["frequency_thinned_pairs"]
    )
    if accounted_pairs != exact_header_pairs:
        raise CampaignError(
            "SERIAL-KAIST camera enqueue accounting mismatch: queued {} + frequency-thinned {} != exact-header pairs {}".format(
                observed["queued_pairs"],
                observed["frequency_thinned_pairs"],
                exact_header_pairs,
            )
        )
    if observed["cam0_decode_failures"] != 0 or observed[
        "cam1_decode_failures"
    ] != 0:
        raise CampaignError(
            "SERIAL-KAIST camera decode failure: cam0={} cam1={}".format(
                observed["cam0_decode_failures"],
                observed["cam1_decode_failures"],
            )
        )
    if observed["processed_pairs"] != observed["queued_pairs"]:
        raise CampaignError(
            "SERIAL-KAIST camera processing mismatch: processed {} != queued {}".format(
                observed["processed_pairs"], observed["queued_pairs"]
            )
        )
    if observed["pending_pairs"] != 0:
        raise CampaignError(
            "SERIAL-KAIST camera queue did not drain: pending {}".format(
                observed["pending_pairs"]
            )
        )
    return {
        "line": line,
        "observed": observed,
        "exact_header_pairs": exact_header_pairs,
        "accounted_pairs": accounted_pairs,
        "pair_accounting_complete": True,
        "decode_failures_zero": True,
        "processed_pairs_match_queued": True,
        "queue_drained": True,
        "frequency_thinning_policy": "existing_frozen_baseline_camera_frequency_policy",
    }


def _atomic_write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise CampaignError("refusing to overwrite JSON result: {}".format(path))
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _adapter_call(
    operation: str, source: Path, adapted: Path
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    # Import only after all paths pass the holdout/protected-path checks.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import kaist_vio_adapter as adapter
    finally:
        del sys.path[0]

    started = time.monotonic()
    started_utc = utc_now()
    if operation == "adapt":
        report = adapter.adapt_bag(source, adapted)
        source_audit = report["source_audit"]
        adapted_audit = report["adapted_audit"]
    else:
        source_audit = adapter.audit_bag(source, "source")
        adapted_audit = adapter.audit_bag(adapted, "adapted")
        preservation = adapter._assert_adaptation_preservation(
            source_audit, adapted_audit
        )
        report = {
            "source_audit": source_audit,
            "adapted_audit": adapted_audit,
            "preservation": preservation,
            "ground_truth_policy": "source_required_and_audited_output_omitted",
        }
    record = {
        "call": "kaist_vio_adapter.{}_bag".format(operation),
        "arguments": [str(source), str(adapted)],
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "ground_truth_policy": report.get("ground_truth_policy"),
        "preservation": report.get("preservation"),
    }
    return record, source_audit, adapted_audit


def _write_failure_result(
    result_path: Path, manifest: Dict[str, Any], exc: BaseException
) -> None:
    manifest["finished_utc"] = utc_now()
    manifest["status"] = "FAILED"
    manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
    try:
        _atomic_write_new_json(result_path, manifest)
    except Exception:
        pass


def run_campaign(args: argparse.Namespace) -> Dict[str, Any]:
    campaign_started = time.monotonic()
    paths = _validate_paths(args)
    frozen_sequence_inputs = _frozen_sequence_inputs(args.sequence)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise CampaignError("--timeout-seconds must be finite and positive")
    if not args.prepare_only:
        if args.ros_port is None:
            raise CampaignError("--ros-port is required unless --prepare-only is used")
        _assert_port_available(args.ros_port)

    t0_capture = bool(getattr(args, "turnsafe_t0_capture", False))
    t0_provenance = t0_capture or bool(
        getattr(args, "turnsafe_t0_provenance", False)
    )
    t0_source_sha = str(getattr(args, "turnsafe_t0_source_sha", ""))
    t0_source_tree = str(getattr(args, "turnsafe_t0_source_tree", ""))
    t0_frozen_base_sha = str(getattr(args, "turnsafe_t0_frozen_base_sha", ""))
    t0_expected_build_id = str(
        getattr(args, "turnsafe_t0_build_provenance_id", "")
    )
    t0_expected_source_state = str(
        getattr(args, "turnsafe_t0_expected_source_state", "")
    )
    t0_build_manifest_arg = getattr(args, "turnsafe_t0_build_manifest", None)
    t0_schema_file_arg = getattr(args, "turnsafe_t0_schema_file", None)
    t0_repository_arg = getattr(args, "turnsafe_t0_repository", None)
    t0_build_manifest: Optional[Path] = None
    t0_schema_file: Optional[Path] = None
    t0_repository: Optional[Path] = None
    t0_build_binding: Optional[Dict[str, Any]] = None
    if t0_provenance:
        if t0_source_sha and not re.fullmatch(r"[0-9a-f]{40}", t0_source_sha):
            raise CampaignError("--turnsafe-t0-source-sha must be a lowercase SHA-1")
        if t0_source_tree and not re.fullmatch(r"[0-9a-f]{40}", t0_source_tree):
            raise CampaignError("--turnsafe-t0-source-tree must be a lowercase tree SHA-1")
        if t0_capture and not re.fullmatch(r"[0-9a-f]{40}", t0_frozen_base_sha):
            raise CampaignError("--turnsafe-t0-frozen-base-sha must be a lowercase SHA-1")
        if t0_expected_build_id and not re.fullmatch(
            r"[0-9a-f]{64}", t0_expected_build_id
        ):
            raise CampaignError(
                "--turnsafe-t0-build-provenance-id must be lowercase SHA-256"
            )
        if t0_expected_source_state not in ("dirty", "clean"):
            raise CampaignError(
                "T0 provenance requires --turnsafe-t0-expected-source-state"
            )
        if (
            t0_build_manifest_arg is None
            or t0_schema_file_arg is None
            or t0_repository_arg is None
        ):
            raise CampaignError(
                "T0 provenance requires build-manifest, schema, and repository"
            )
        t0_build_manifest = _regular_file(
            Path(t0_build_manifest_arg), "TurnSafe T0 build manifest"
        )
        t0_schema_file = _regular_file(
            Path(t0_schema_file_arg), "TurnSafe T0 schema"
        )
        t0_repository = Path(t0_repository_arg).resolve(strict=True)
        if t0_repository != REPO_ROOT:
            raise CampaignError("T0 repository is not the authoritative worktree")
        t0_build_binding = _validate_build_manifest(
            t0_build_manifest, paths["binary"], t0_schema_file, t0_repository
        )
        build_source = t0_build_binding["source_snapshot"]
        _validate_t0_caller_expectations(
            build_source,
            t0_build_binding["build_provenance_id"],
            t0_source_sha,
            t0_source_tree,
            t0_expected_build_id,
            t0_expected_source_state,
        )

    output = paths["output_dir"]
    output.mkdir(parents=True, exist_ok=False)
    (output / "ros-home").mkdir()
    (output / "ros-logs").mkdir()
    result_path = output / "sequence_result.json"
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "sequence": args.sequence,
        "campaign_index": SEQUENCE_ORDER.index(args.sequence),
        "prepare_only": bool(args.prepare_only),
        "started_utc": utc_now(),
        "status": "RUNNING",
        "commands": {},
        "checks": {},
        "completion": {
            "estimator_started": False,
            "estimator_completed": False,
            "output_validation_passed": False,
            "evaluation_completed": False,
            "timed_out": False,
            "reset_detected": False,
            "nonfinite_detected": False,
            "unpaired_warning_count": 0,
            "dropped_message_count": 0,
        },
    }

    try:
        rosbag = _command_path("rosbag")
        tool_paths = {"rosbag": rosbag}
        if not args.prepare_only:
            tool_paths.update(
                {
                    "catkin_find": _command_path("catkin_find"),
                    "roslaunch": _command_path("roslaunch"),
                    "evo_ape": _command_path("evo_ape"),
                    "evo_rpe": _command_path("evo_rpe"),
                }
            )
            if t0_provenance:
                tool_paths["ldd"] = _command_path("ldd")
        environment = _minimal_environment(output, args.ros_port)
        manifest["runtime"] = {
            "ros_port": args.ros_port,
            "timeout_seconds": args.timeout_seconds,
            "environment": environment,
            "ground_truth_runtime_policy": "reference_used_only_after_roslaunch_finished",
        }
        manifest["diagnostics"] = {
            "yaw": {
                "status": "NOT_AVAILABLE",
                "reason": "no frozen deterministic yaw-diagnostic derivation is defined for Session 0.5",
            },
            "tilt": {
                "status": "NOT_AVAILABLE",
                "reason": "no frozen deterministic tilt-diagnostic derivation is defined for Session 0.5",
            },
        }

        fixed_inputs = {
            "source_bag": paths["source_bag"],
            "config": paths["config"],
            "kalibr_imu_chain": paths["config"].parent / "kalibr_imu_chain.yaml",
            "kalibr_imucam_chain": paths["config"].parent / "kalibr_imucam_chain.yaml",
            "launch": paths["launch"],
            "estimator_binary": paths["binary"],
            "reference_tum": paths["reference_tum"],
            "adapter": ADAPTER_PATH,
            "trajectory_converter": CONVERTER_PATH,
            "frozen_baseline_results": FROZEN_BASELINE_RESULTS,
            "python": PYTHON,
        }
        if t0_provenance:
            fixed_inputs["turnsafe_t0_build_manifest"] = t0_build_manifest
            fixed_inputs["turnsafe_t0_schema"] = t0_schema_file
            fixed_inputs["turnsafe_source_snapshot_tool"] = SOURCE_SNAPSHOT_PATH
            if t0_build_binding is None:
                raise CampaignError("T0 build binding was not initialized")
            fixed_inputs["turnsafe_configure_manifest"] = t0_build_binding[
                "configure_manifest_path"
            ]
            fixed_inputs["turnsafe_cmake_cache"] = t0_build_binding[
                "cmake_cache_path"
            ]
            for name, path in sorted(
                t0_build_binding["artifact_paths"].items()
            ):
                fixed_inputs["turnsafe_artifact_" + name] = path
        for name, path in list(fixed_inputs.items()):
            fixed_inputs[name] = _regular_file(path, name, name in ("estimator_binary", "python"))
        fixed_inputs.update(tool_paths)
        manifest["inputs_before"] = {
            name: file_identity(path) for name, path in sorted(fixed_inputs.items())
        }
        frozen_pre_adapter = {
            "source_bag": "source_bag_sha256",
            "config": "config_sha256",
            "kalibr_imu_chain": "kalibr_imu_chain_sha256",
            "kalibr_imucam_chain": "kalibr_imucam_chain_sha256",
            "reference_tum": "reference_sha256",
        }
        for input_name, frozen_name in frozen_pre_adapter.items():
            _require_frozen_identity(
                manifest["inputs_before"][input_name],
                frozen_sequence_inputs[frozen_name],
                input_name,
            )
        manifest["frozen_input_binding"] = {
            "baseline_results_sha256": FROZEN_BASELINE_RESULTS_SHA256,
            "expected": dict(sorted(frozen_sequence_inputs.items())),
            "pre_adapter_inputs_match": True,
            "adapted_bag_matches": False,
        }
        manifest["checks"]["frozen_pre_adapter_inputs_match"] = True
        if paths["adapted_bag"].exists():
            adapted_before = file_identity(
                _regular_file(paths["adapted_bag"], "adapted bag")
            )
            _require_frozen_identity(
                adapted_before,
                frozen_sequence_inputs["adapted_bag_sha256"],
                "adapted_bag",
            )
            manifest["frozen_input_binding"]["adapted_bag_matches"] = True
            manifest["checks"]["frozen_adapted_bag_matches"] = True
        source_snapshot_before: Optional[Dict[str, Any]] = None
        if t0_provenance:
            if t0_repository is None or t0_build_binding is None:
                raise CampaignError("T0 provenance binding is unavailable")
            source_before_path = output / "source_snapshot_before.json"
            source_before_record = run_command(
                [
                    str(PYTHON),
                    str(SOURCE_SNAPSHOT_PATH),
                    str(t0_repository),
                    str(source_before_path),
                ],
                output / "source_snapshot_before.log",
                environment,
                min(args.timeout_seconds, 120.0),
            )
            manifest["commands"]["source_snapshot_before"] = source_before_record
            if not _command_succeeded(source_before_record):
                raise CampaignError("pre-run source snapshot failed")
            source_snapshot_before = _validate_source_snapshot(
                _strict_json(
                    source_before_path.read_text(encoding="ascii"),
                    "pre-run source snapshot",
                ),
                "pre-run",
            )
            if source_snapshot_before[
                "aggregate_source_snapshot_sha256"
            ] != t0_build_binding["source_snapshot"][
                "aggregate_source_snapshot_sha256"
            ]:
                raise CampaignError("current source snapshot differs from build")
            if bool(source_snapshot_before["source_dirty"]) != (
                t0_expected_source_state == "dirty"
            ):
                raise CampaignError("current source state differs from requested state")
            manifest["source_provenance_before"] = {
                "head_sha": source_snapshot_before["head_sha"],
                "head_tree": source_snapshot_before["head_tree"],
                "source_dirty": source_snapshot_before["source_dirty"],
                "source_snapshot_sha256": source_snapshot_before[
                    "aggregate_source_snapshot_sha256"
                ],
                "build_provenance_id": t0_build_binding[
                    "build_provenance_id"
                ],
            }

        def verify_source_after() -> None:
            if not t0_provenance:
                return
            if t0_repository is None or source_snapshot_before is None:
                raise CampaignError("pre-run source snapshot is unavailable")
            source_after_path = output / "source_snapshot_after.json"
            source_after_record = run_command(
                [
                    str(PYTHON),
                    str(SOURCE_SNAPSHOT_PATH),
                    str(t0_repository),
                    str(source_after_path),
                ],
                output / "source_snapshot_after.log",
                environment,
                min(args.timeout_seconds, 120.0),
            )
            manifest["commands"]["source_snapshot_after"] = source_after_record
            if not _command_succeeded(source_after_record):
                raise CampaignError("post-run source snapshot failed")
            source_after = _validate_source_snapshot(
                _strict_json(
                    source_after_path.read_text(encoding="ascii"),
                    "post-run source snapshot",
                ),
                "post-run",
            )
            if source_after[
                "aggregate_source_snapshot_sha256"
            ] != source_snapshot_before[
                "aggregate_source_snapshot_sha256"
            ]:
                raise CampaignError("source changed during campaign")
            manifest["source_provenance_after"] = {
                "head_sha": source_after["head_sha"],
                "head_tree": source_after["head_tree"],
                "source_dirty": source_after["source_dirty"],
                "source_snapshot_sha256": source_after[
                    "aggregate_source_snapshot_sha256"
                ],
            }
            manifest["checks"]["source_snapshot_unchanged"] = True

        paths["adapted_bag"].parent.mkdir(parents=True, exist_ok=True)
        operation = "audit" if paths["adapted_bag"].exists() else "adapt"
        adapter_record, source_audit, adapted_audit = _adapter_call(
            operation, paths["source_bag"], paths["adapted_bag"]
        )
        manifest["adapter"] = adapter_record
        manifest["source_audit"] = source_audit
        manifest["adapted_audit"] = adapted_audit
        manifest["checks"]["adapted_bag_has_no_ground_truth"] = (
            "/pose_transformed" not in adapted_audit.get("streams", {})
        )
        if not manifest["checks"]["adapted_bag_has_no_ground_truth"]:
            raise CampaignError("adapted estimator-input bag contains ground truth")

        paths["adapted_bag"] = _regular_file(paths["adapted_bag"], "adapted bag")
        manifest["adapted_bag"] = file_identity(paths["adapted_bag"])
        _require_frozen_identity(
            manifest["adapted_bag"],
            frozen_sequence_inputs["adapted_bag_sha256"],
            "adapted_bag",
        )
        manifest["frozen_input_binding"]["adapted_bag_matches"] = True
        manifest["checks"]["frozen_adapted_bag_matches"] = True
        info_timeout = min(args.timeout_seconds, 300.0)
        for role in ("source", "adapted"):
            bag_path = paths[role + "_bag"]
            record = run_command(
                [str(rosbag), "info", "--yaml", str(bag_path)],
                output / (role + "_rosbag_info.yaml"),
                environment,
                info_timeout,
            )
            manifest["commands"][role + "_rosbag_info"] = record
            if not _command_succeeded(record):
                raise CampaignError("rosbag info failed for {} bag".format(role))

        if args.prepare_only:
            verify_source_after()
            manifest["inputs_after"] = {
                name: file_identity(path) for name, path in sorted(fixed_inputs.items())
            }
            manifest["inputs_after"]["adapted_bag"] = file_identity(paths["adapted_bag"])
            manifest["checks"]["runtime_ground_truth_excluded"] = True
            manifest["status"] = "PREPARED"
            manifest["finished_utc"] = utc_now()
            manifest["runtime"]["total_duration_seconds"] = (
                time.monotonic() - campaign_started
            )
            _atomic_write_new_json(result_path, manifest)
            return manifest

        state = output / "state_estimate.txt"
        deviation = output / "state_deviation.txt"
        timing = output / "timing_openvins.csv"
        trajectory = output / "trajectory_tum.txt"
        launch_arguments = [
            str(paths["launch"]),
            "bag:=" + str(paths["adapted_bag"]),
            "bag_start:=0.0",
            "path_state:=" + str(state),
            "path_std:=" + str(deviation),
            "path_time:=" + str(timing),
            "verbosity:=INFO",
        ]
        t0_output = output / "t0_events.jsonl"
        t0_expected_header: Dict[str, Any] = {}
        if t0_provenance:
            if t0_build_binding is None:
                raise CampaignError("T0 build binding is unavailable")
            calibration_bundle = {
                "kalibr_imu_chain": manifest["inputs_before"]["kalibr_imu_chain"]["sha256"],
                "kalibr_imucam_chain": manifest["inputs_before"]["kalibr_imucam_chain"]["sha256"],
            }
            build_source = t0_build_binding["source_snapshot"]
            t0_expected_header = {
                "frozen_base_sha": t0_frozen_base_sha,
                "source_sha": build_source["head_sha"],
                "tree_sha": build_source["head_tree"],
                "source_snapshot_sha256": build_source[
                    "aggregate_source_snapshot_sha256"
                ],
                "source_dirty": bool(build_source["source_dirty"]),
                "build_provenance_id": t0_build_binding[
                    "build_provenance_id"
                ],
                "configure_manifest_sha256": t0_build_binding[
                    "configure_manifest_sha256"
                ],
                "build_manifest_sha256": manifest["inputs_before"]["turnsafe_t0_build_manifest"]["sha256"],
                "binary_sha256": manifest["inputs_before"]["estimator_binary"]["sha256"],
                "config_sha256": manifest["inputs_before"]["config"]["sha256"],
                "calibration_sha256": _sha256_canonical_json(calibration_bundle),
                "diagnostic_schema_sha256": manifest["inputs_before"]["turnsafe_t0_schema"]["sha256"],
            }
            if t0_capture:
                launch_arguments.extend(
                    [
                        "turnsafe_t0_capture:=true",
                        "turnsafe_t0_output_path:=" + str(t0_output),
                        "turnsafe_t0_schema:=" + T0_SCHEMA,
                        "turnsafe_t0_frozen_base_sha:="
                        + t0_expected_header["frozen_base_sha"],
                        "turnsafe_t0_source_sha:="
                        + t0_expected_header["source_sha"],
                        "turnsafe_t0_source_tree:="
                        + t0_expected_header["tree_sha"],
                        "turnsafe_t0_source_snapshot_sha256:="
                        + t0_expected_header["source_snapshot_sha256"],
                        "turnsafe_t0_build_provenance_id:="
                        + t0_expected_header["build_provenance_id"],
                        "turnsafe_t0_build_manifest_sha256:="
                        + t0_expected_header["build_manifest_sha256"],
                        "turnsafe_t0_binary_sha256:="
                        + t0_expected_header["binary_sha256"],
                        "turnsafe_t0_config_sha256:="
                        + t0_expected_header["config_sha256"],
                        "turnsafe_t0_calibration_sha256:="
                        + t0_expected_header["calibration_sha256"],
                        "turnsafe_t0_diagnostic_schema_sha256:="
                        + t0_expected_header["diagnostic_schema_sha256"],
                    ]
                )
        joined_runtime = "\n".join(launch_arguments).lower()
        if str(paths["reference_tum"]) in joined_runtime or any(
            token in joined_runtime for token in FORBIDDEN_RUNTIME_TEXT
        ):
            raise CampaignError("ground truth leaked into estimator launch arguments")

        preflight_timeout = min(args.timeout_seconds, 120.0)
        dump_record = run_command(
            [str(tool_paths["roslaunch"]), "--dump-params"] + launch_arguments,
            output / "resolved_ros_parameters.yaml",
            environment,
            preflight_timeout,
        )
        manifest["commands"]["resolve_parameters"] = dump_record
        if not _command_succeeded(dump_record):
            raise CampaignError("roslaunch parameter resolution failed")
        resolved_parameters = Path(dump_record["log"]).read_text(
            encoding="utf-8", errors="replace"
        ).lower()
        leaked_tokens = [token for token in FORBIDDEN_RUNTIME_TEXT if token in resolved_parameters]
        if leaked_tokens:
            raise CampaignError(
                "ground-truth runtime parameter detected: {}".format(
                    ", ".join(leaked_tokens)
                )
            )

        find_record = run_command(
            [
                str(tool_paths["catkin_find"]),
                "--libexec",
                "ov_msckf",
                "ros1_serial_msckf",
            ],
            output / "resolved_estimator_binary.txt",
            environment,
            preflight_timeout,
        )
        manifest["commands"]["resolve_estimator_binary"] = find_record
        if not _command_succeeded(find_record):
            raise CampaignError("catkin_find estimator-binary resolution failed")
        resolved_lines = [
            line.strip()
            for line in Path(find_record["log"])
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
            if line.strip()
        ]
        if not resolved_lines or Path(resolved_lines[-1]).resolve(strict=False) != paths["binary"]:
            raise CampaignError(
                "catkin_find resolved a different estimator binary than --binary"
            )

        if t0_provenance:
            if t0_build_binding is None:
                raise CampaignError("T0 build binding is unavailable")
            loader_record = run_command(
                [str(tool_paths["ldd"]), str(paths["binary"])],
                output / "resolved_dynamic_libraries.txt",
                environment,
                preflight_timeout,
            )
            manifest["commands"]["resolve_dynamic_libraries"] = loader_record
            if not _command_succeeded(loader_record):
                raise CampaignError("dynamic-loader resolution failed")
            loader_lines = Path(loader_record["log"]).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            resolved_libraries: Dict[str, Path] = {}
            for line in loader_lines:
                if "=>" not in line:
                    continue
                name, remainder = line.split("=>", 1)
                name = name.strip()
                target = remainder.strip().split(" (", 1)[0].strip()
                if target == "not found":
                    raise CampaignError("dynamic library {} was not found".format(name))
                if target.startswith("/"):
                    resolved_libraries[name] = Path(target).resolve(strict=True)
            for artifact_name, soname in (
                ("ov_msckf_library", "libov_msckf_lib.so"),
                ("ov_core_library", "libov_core_lib.so"),
            ):
                expected = t0_build_binding["artifact_paths"][artifact_name]
                if resolved_libraries.get(soname) != expected:
                    raise CampaignError(
                        "dynamic loader resolved {} outside build manifest".format(
                            soname
                        )
                    )
            manifest["checks"]["dynamic_libraries_match_build_manifest"] = True

        launch_argv = [
            str(tool_paths["roslaunch"]),
            "-p",
            str(args.ros_port),
        ] + launch_arguments
        launch_record = run_command(
            launch_argv,
            output / "console.log",
            environment,
            args.timeout_seconds,
        )
        manifest["commands"]["roslaunch"] = launch_record
        manifest["completion"].update(
            {
                "estimator_started": True,
                "estimator_completed": _command_succeeded(launch_record),
                "timed_out": bool(launch_record.get("timed_out", False)),
                "roslaunch_exit_code": launch_record.get("exit_code"),
                "roslaunch_runtime_seconds": launch_record.get("duration_seconds"),
                "process_group_survived_cleanup": bool(
                    launch_record.get("process_group_survived_cleanup", False)
                ),
            }
        )
        manifest["checks"]["runtime_ground_truth_excluded"] = all(
            token not in "\n".join(launch_argv).lower()
            for token in FORBIDDEN_RUNTIME_TEXT
        ) and str(paths["reference_tum"]) not in launch_argv
        manifest["console_diagnostics"] = _console_diagnostics(output / "console.log")
        diagnostic_counts = manifest["console_diagnostics"]["counts"]
        manifest["completion"].update(
            {
                "reset_detected": diagnostic_counts["reset_lines"] != 0,
                "nonfinite_detected": diagnostic_counts["nonfinite_lines"] != 0,
                "unpaired_warning_count": diagnostic_counts[
                    "unpaired_warning_lines"
                ],
                "dropped_message_count": diagnostic_counts[
                    "dropped_message_lines"
                ],
            }
        )
        if not _command_succeeded(launch_record):
            if launch_record.get("timed_out"):
                manifest["status"] = "TIMED_OUT"
            raise CampaignError("fixed KAIST roslaunch did not complete cleanly")

        manifest["exact_header_stereo_runtime"] = (
            _bind_exact_header_runtime_summary(
                output / "console.log", adapted_audit
            )
        )
        manifest["checks"]["exact_header_runtime_matches_adapted_audit"] = True
        manifest["camera_enqueue_runtime"] = _bind_camera_enqueue_runtime_summary(
            output / "console.log",
            manifest["exact_header_stereo_runtime"]["observed"][
                "exact_header_pairs"
            ],
        )
        manifest["checks"]["camera_enqueue_accounts_for_exact_header_pairs"] = True
        manifest["checks"]["camera_decode_failures_zero"] = True
        manifest["checks"]["camera_processed_pairs_match_queued"] = True
        manifest["checks"]["camera_queue_drained"] = True

        if t0_capture:
            manifest["turnsafe_t0"] = _validate_t0_jsonl(
                t0_output, t0_expected_header
            )
            manifest["turnsafe_t0"].update(
                {
                    "capture_requested": True,
                    "run_header_expected": t0_expected_header,
                    "calibration_bundle": calibration_bundle,
                }
            )
            manifest["checks"]["turnsafe_t0_schema_valid"] = True
            manifest["checks"]["turnsafe_t0_runtime_identity_firewall"] = True

        # Until roslaunch terminates, only the reference's byte identity is
        # bound. Its numeric ground-truth contents are first parsed here.
        manifest["reference_validation"] = _validate_numeric_table(
            paths["reference_tum"], None, 8
        )

        manifest["outputs"] = {
            "state": _validate_numeric_table(state, None, 8),
            "deviation": _validate_numeric_table(deviation, None, 2),
            "timing": _validate_numeric_table(timing, ",", 2),
        }

        converter_argv = [str(PYTHON), str(CONVERTER_PATH), str(state), str(trajectory)]
        conversion_record = run_command(
            converter_argv,
            output / "trajectory_conversion.log",
            environment,
            min(args.timeout_seconds, 300.0),
        )
        manifest["commands"]["trajectory_conversion"] = conversion_record
        if not _command_succeeded(conversion_record):
            raise CampaignError("OpenVINS-to-TUM conversion failed")
        manifest["outputs"]["trajectory_tum"] = _validate_numeric_table(
            trajectory, None, 8
        )
        manifest["output_consistency"] = _validate_output_consistency(
            state, deviation, timing, trajectory
        )
        manifest["checks"]["output_timestamp_and_row_consistency"] = True
        manifest["completion"]["output_validation_passed"] = True

        metric_specs = {
            "ape_translation": (
                tool_paths["evo_ape"],
                "trans_part",
                output / "evo_ape_translation.zip",
            ),
            "rpe_translation_1m": (
                tool_paths["evo_rpe"],
                "trans_part",
                output / "evo_rpe_translation_1m.zip",
            ),
            "rpe_rotation_1m_deg": (
                tool_paths["evo_rpe"],
                "angle_deg",
                output / "evo_rpe_rotation_1m_deg.zip",
            ),
        }
        manifest["metrics"] = {}
        for name, (tool, relation, archive) in metric_specs.items():
            metric_argv = [
                str(tool),
                "tum",
                str(paths["reference_tum"]),
                str(trajectory),
                "--t_max_diff",
                "0.01",
                "--pose_relation",
                relation,
            ]
            if name.startswith("rpe_"):
                metric_argv.extend(
                    [
                        "--delta",
                        "1.0",
                        "--delta_unit",
                        "m",
                        "--all_pairs",
                        "--pairs_from_reference",
                    ]
                )
            metric_argv.extend(
                ["--align", "--save_results", str(archive), "--no_warnings"]
            )
            record = run_command(
                metric_argv,
                output / (name + ".log"),
                environment,
                min(args.timeout_seconds, 300.0),
            )
            manifest["commands"][name] = record
            if not _command_succeeded(record):
                raise CampaignError("{} evaluation failed".format(name))
            manifest["metrics"][name] = _metric_archive(archive)

        manifest["completion"]["evaluation_completed"] = True
        manifest["checks"].update(
            {
                "no_reset_lines": diagnostic_counts["reset_lines"] == 0,
                "no_nonfinite_lines": diagnostic_counts["nonfinite_lines"] == 0,
                "no_unpaired_warning_lines": diagnostic_counts["unpaired_warning_lines"] == 0,
                "no_dropped_message_lines": diagnostic_counts["dropped_message_lines"] == 0,
            }
        )
        if not all(manifest["checks"].values()):
            raise CampaignError("runtime diagnostics or ground-truth boundary check failed")

        verify_source_after()
        manifest["inputs_after"] = {
            name: file_identity(path) for name, path in sorted(fixed_inputs.items())
        }
        manifest["inputs_after"]["adapted_bag"] = file_identity(paths["adapted_bag"])
        changed = [
            name
            for name in manifest["inputs_before"]
            if manifest["inputs_before"][name] != manifest["inputs_after"][name]
        ]
        manifest["checks"]["fixed_inputs_unchanged"] = not changed
        if changed:
            raise CampaignError("fixed inputs changed during run: {}".format(", ".join(changed)))

        manifest["status"] = "COMPLETED"
        manifest["finished_utc"] = utc_now()
        manifest["runtime"]["total_duration_seconds"] = (
            time.monotonic() - campaign_started
        )
        _atomic_write_new_json(result_path, manifest)
        return manifest
    except BaseException as exc:
        if manifest.get("status") != "TIMED_OUT":
            manifest["status"] = "FAILED"
        manifest["finished_utc"] = utc_now()
        if "runtime" in manifest:
            manifest["runtime"]["total_duration_seconds"] = (
                time.monotonic() - campaign_started
            )
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _atomic_write_new_json(result_path, manifest)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, run, and post-run evaluate one fixed KAIST-VIO sequence."
    )
    parser.add_argument("--sequence", required=True, choices=SEQUENCE_ORDER)
    parser.add_argument("--source-bag", required=True, type=Path)
    parser.add_argument("--adapted-bag", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--reference-tum", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ros-port", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="audit/adapt and record rosbag metadata without starting the estimator",
    )
    parser.add_argument("--turnsafe-t0-capture", action="store_true")
    parser.add_argument(
        "--turnsafe-t0-provenance",
        action="store_true",
        help="bind a capture-off replay to the same verified source/build identity",
    )
    parser.add_argument("--turnsafe-t0-frozen-base-sha", default="")
    parser.add_argument("--turnsafe-t0-source-sha", default="")
    parser.add_argument("--turnsafe-t0-source-tree", default="")
    parser.add_argument("--turnsafe-t0-build-provenance-id", default="")
    parser.add_argument(
        "--turnsafe-t0-expected-source-state", choices=("dirty", "clean")
    )
    parser.add_argument("--turnsafe-t0-repository", type=Path)
    parser.add_argument("--turnsafe-t0-build-manifest", type=Path)
    parser.add_argument("--turnsafe-t0-schema-file", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_campaign(args)
    except (RuntimeError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print("KAIST_VIO_CAMPAIGN_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(
        "{} {}: {}".format(result["status"], result["sequence"], args.output_dir)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
