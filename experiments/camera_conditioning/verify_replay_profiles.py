#!/usr/bin/python3
"""Fail closed unless the declared conditioning replay profiles stay frozen."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, Tuple


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROFILE_SPECS = {
    "euroc-nullspace": (
        REPOSITORY_ROOT / "config/euroc_mav/estimator_config_conditioning_nullspace.yaml",
        "nullspace",
        "radtan",
    ),
    "euroc-schur": (
        REPOSITORY_ROOT / "config/euroc_mav/estimator_config_conditioning_schur.yaml",
        "schur",
        "radtan",
    ),
    "tumvi-nullspace": (
        REPOSITORY_ROOT / "config/tum_vi/estimator_config_conditioning_nullspace.yaml",
        "nullspace",
        "equidistant",
    ),
    "tumvi-schur": (
        REPOSITORY_ROOT / "config/tum_vi/estimator_config_conditioning_schur.yaml",
        "schur",
        "equidistant",
    ),
    "euroc-schema2-schur": (
        REPOSITORY_ROOT
        / "config/euroc_mav/estimator_config_anytime_schema2_schur.yaml",
        "schur",
        "radtan",
    ),
    "euroc-schema2-nullspace": (
        REPOSITORY_ROOT
        / "config/euroc_mav/estimator_config_anytime_schema2_nullspace.yaml",
        "nullspace",
        "radtan",
    ),
    "tumvi-schema2-schur": (
        REPOSITORY_ROOT
        / "config/tum_vi/estimator_config_anytime_schema2_schur.yaml",
        "schur",
        "equidistant",
    ),
    "tumvi-schema2-nullspace": (
        REPOSITORY_ROOT
        / "config/tum_vi/estimator_config_anytime_schema2_nullspace.yaml",
        "nullspace",
        "equidistant",
    ),
}
SCHEMA2_PROFILE_IDS = frozenset(
    (
        "euroc-schema2-nullspace",
        "euroc-schema2-schur",
        "tumvi-schema2-nullspace",
        "tumvi-schema2-schur",
    )
)
PAIR_IDS = (
    ("euroc-nullspace", "euroc-schur"),
    ("tumvi-nullspace", "tumvi-schur"),
)
BASE_CONFIGS = {
    "euroc": REPOSITORY_ROOT / "config/euroc_mav/estimator_config.yaml",
    "tumvi": REPOSITORY_ROOT / "config/tum_vi/estimator_config.yaml",
}
FIXED_EXISTING_KEYS = (
    "integration",
    "use_stereo",
    "max_cameras",
    "max_clones",
    "max_msckf_in_update",
    "use_klt",
    "num_pts",
    "fast_threshold",
    "grid_x",
    "grid_y",
    "min_px_dist",
    "knn_ratio",
    "track_frequency",
    "downsample_cameras",
    "num_opencv_threads",
    "histogram_method",
)
REQUIRED_VALUES = {
    "use_fej": "true",
    "calib_cam_extrinsics": "false",
    "calib_cam_intrinsics": "false",
    "calib_cam_timeoffset": "false",
    "max_slam": "0",
    "feat_rep_msckf": "GLOBAL_3D",
    "up_msckf_max_visual_passes": "1",
    "up_msckf_capture_conditioning_systems": "false",
}
SCHEMA2_REQUIRED_VALUES = {
    "up_msckf_capture_update_envelopes_v2": "false",
}
SCHEMA2_BASE_PAIRS = (
    ("euroc-nullspace", "euroc-schema2-nullspace"),
    ("euroc-schur", "euroc-schema2-schur"),
    ("tumvi-nullspace", "tumvi-schema2-nullspace"),
    ("tumvi-schur", "tumvi-schema2-schur"),
)
SCALAR_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*):\s*(.*?)\s*$")
METHOD_LINE = re.compile(
    rb"(?m)^up_msckf_landmark_elimination:\s*(?:nullspace|schur)\s*$"
)


class ProfileError(RuntimeError):
    """A profile violates the predeclared conditioning contract."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_scalar(value: str) -> str:
    value = value.split("#", 1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def read_scalars(path: Path) -> Dict[str, str]:
    if not path.is_file():
        raise ProfileError(f"missing profile/configuration: {path}")
    result: Dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = SCALAR_LINE.match(raw_line)
        if match is None:
            continue
        key = match.group(1)
        if key in result:
            raise ProfileError(f"duplicate scalar {key!r} in {path}")
        result[key] = clean_scalar(match.group(2))
    return result


def require_values(path: Path, values: Dict[str, str], expected_method: str) -> None:
    for key, expected in REQUIRED_VALUES.items():
        actual = values.get(key)
        if actual != expected:
            raise ProfileError(f"{path}: {key}={actual!r}, expected {expected!r}")
    actual_method = values.get("up_msckf_landmark_elimination")
    if actual_method != expected_method:
        raise ProfileError(
            f"{path}: reducer={actual_method!r}, expected {expected_method!r}"
        )
    if "up_msckf_conditioning_capture_path" in values:
        raise ProfileError(f"{path}: capture output belongs in a private ROS parameter")


def require_schema2_values(path: Path, values: Dict[str, str]) -> None:
    for key, expected in SCHEMA2_REQUIRED_VALUES.items():
        actual = values.get(key)
        if actual != expected:
            raise ProfileError(f"{path}: {key}={actual!r}, expected {expected!r}")
    for key in (
        "up_msckf_update_envelope_capture_path",
        "up_msckf_update_envelope_run_id",
        "up_msckf_update_envelope_sequence_id",
    ):
        if key in values:
            raise ProfileError(f"{path}: {key} belongs in a private ROS parameter")


def require_existing_settings(path: Path, values: Dict[str, str], profile_id: str) -> None:
    dataset_id = "euroc" if profile_id.startswith("euroc-") else "tumvi"
    base_values = read_scalars(BASE_CONFIGS[dataset_id])
    for key in FIXED_EXISTING_KEYS:
        actual = values.get(key)
        expected = base_values.get(key)
        if actual != expected:
            raise ProfileError(
                f"{path}: frozen existing {key}={actual!r}, expected {expected!r}"
            )


def require_native_calibration(path: Path, values: Dict[str, str], model: str) -> Path:
    relative = values.get("relative_config_imucam")
    if relative != "kalibr_imucam_chain.yaml":
        raise ProfileError(f"{path}: unexpected camera calibration reference {relative!r}")
    calibration = path.parent / relative
    if not calibration.is_file():
        raise ProfileError(f"{path}: missing camera calibration {calibration}")
    calibration_values = re.findall(
        r"(?m)^\s*distortion_model:\s*([A-Za-z0-9_]+)\s*$",
        calibration.read_text(encoding="utf-8"),
    )
    if calibration_values != [model, model]:
        raise ProfileError(
            f"{calibration}: camera models {calibration_values!r}, expected {[model, model]!r}"
        )
    return calibration


def require_sole_method_difference(left: Path, right: Path) -> None:
    left_bytes = left.read_bytes()
    right_bytes = right.read_bytes()
    normalized_left, left_count = METHOD_LINE.subn(
        b"up_msckf_landmark_elimination: METHOD", left_bytes
    )
    normalized_right, right_count = METHOD_LINE.subn(
        b"up_msckf_landmark_elimination: METHOD", right_bytes
    )
    if left_count != 1 or right_count != 1:
        raise ProfileError(f"method key is not unique in pair {left}, {right}")
    if normalized_left != normalized_right:
        raise ProfileError(f"profile pair differs beyond the reducer: {left}, {right}")


def require_sole_schema2_extension(base_id: str, schema2_id: str) -> None:
    base_path = PROFILE_SPECS[base_id][0]
    schema2_path = PROFILE_SPECS[schema2_id][0]
    base_values = read_scalars(base_path)
    schema2_values = read_scalars(schema2_path)
    for key in SCHEMA2_REQUIRED_VALUES:
        schema2_values.pop(key, None)
    if schema2_values != base_values:
        raise ProfileError(
            f"Schema-2 profile differs from its frozen reducer base beyond the "
            f"default-off capture selector: {base_path}, {schema2_path}"
        )


def validate_profiles(selected: Path | None = None) -> Dict[str, Dict[str, str]]:
    selected_resolved = selected.resolve() if selected is not None else None
    allowed_paths = {spec[0].resolve() for spec in PROFILE_SPECS.values()}
    if selected_resolved is not None and selected_resolved not in allowed_paths:
        raise ProfileError(f"config is not a declared conditioning profile: {selected}")

    for left_id, right_id in PAIR_IDS:
        require_sole_method_difference(PROFILE_SPECS[left_id][0], PROFILE_SPECS[right_id][0])
    for base_id, schema2_id in SCHEMA2_BASE_PAIRS:
        require_sole_schema2_extension(base_id, schema2_id)

    report: Dict[str, Dict[str, str]] = {}
    for profile_id, (path, method, model) in PROFILE_SPECS.items():
        values = read_scalars(path)
        require_values(path, values, method)
        if profile_id in SCHEMA2_PROFILE_IDS:
            require_schema2_values(path, values)
        require_existing_settings(path, values, profile_id)
        calibration = require_native_calibration(path, values, model)
        report[profile_id] = {
            "path": str(path.resolve()),
            "sha256": sha256(path),
            "method": method,
            "camera_model": model,
            "calibration_path": str(calibration.resolve()),
            "calibration_sha256": sha256(calibration),
        }

    if selected_resolved is not None:
        report = {
            profile_id: record
            for profile_id, record in report.items()
            if Path(record["path"]) == selected_resolved
        }
    return report


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="also require this declared profile")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = validate_profiles(args.config)
    except (OSError, UnicodeError, ProfileError) as exc:
        print(f"profile validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
