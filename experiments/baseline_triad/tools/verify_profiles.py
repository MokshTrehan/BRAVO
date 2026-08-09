#!/usr/bin/python3
"""Validate and normalize the supported-intersection baseline configs.

Only the Python standard library is used. The OpenCV YAML files used here have
flat estimator keys, so parsing their top-level scalar map does not require a
third-party YAML package. Nested calibration files are compared by SHA-256.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PACKAGE_ROOT / "method_manifest.json"
MATRIX_PATH = PACKAGE_ROOT / "parameter_matrix.csv"
CONFIG_HASH_PATH = PACKAGE_ROOT / "config_sha256.csv"
LOCAL_ONLY_KEYS = {
    "up_msckf_landmark_elimination",
    "up_msckf_max_visual_passes",
}
METHOD_ORDER = ("U-NS", "L-NS", "L-SCHUR", "OV-SCHUR")

REQUIRED_COMMON = {
    "use_fej": False,
    "integration": "rk4",
    "use_stereo": True,
    "max_cameras": 2,
    "calib_cam_extrinsics": False,
    "calib_cam_intrinsics": False,
    "calib_cam_timeoffset": False,
    "calib_imu_intrinsics": False,
    "calib_imu_g_sensitivity": False,
    "max_clones": 11,
    "max_slam": 0,
    "max_msckf_in_update": 40,
    "feat_rep_msckf": "GLOBAL_3D",
    "try_zupt": False,
    "use_klt": True,
    "num_pts": 200,
    "fast_threshold": 20,
    "grid_x": 5,
    "grid_y": 5,
    "min_px_dist": 10,
    "track_frequency": 21.0,
    "downsample_cameras": False,
    "num_opencv_threads": 0,
    "multi_threading_pubs": False,
    "multi_threading_subs": False,
    "histogram_method": "HISTOGRAM",
    "fi_triangulate_1d": False,
    "fi_refine_features": True,
    "use_aruco": False,
    "up_msckf_sigma_px": 1,
    "up_msckf_chi2_multipler": 1,
    "relative_config_imu": "kalibr_imu_chain.yaml",
    "relative_config_imucam": "kalibr_imucam_chain.yaml",
}


class ProfileError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strip_yaml_comment(value: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in ("'", '"'):
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
            continue
        if char == "#" and quote is None:
            return value[:index].rstrip()
    return value.strip()


def parse_scalar(text: str) -> Any:
    value = strip_yaml_comment(text).strip()
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~"):
        return None
    if value.startswith(("'", '"', "[")):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ProfileError(f"cannot parse scalar {value!r}: {exc}") from exc
    try:
        return int(value, 10)
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        return value
    if not math.isfinite(number):
        raise ProfileError(f"nonfinite scalar is forbidden: {value!r}")
    return number


def parse_flat_config(path: Path) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line or line[0].isspace() or line.startswith(("#", "%", "---")):
            continue
        if ":" not in line:
            raise ProfileError(f"{path}:{line_number}: expected top-level key/value")
        key, raw_value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise ProfileError(f"{path}:{line_number}: empty key")
        if key in values:
            raise ProfileError(f"{path}:{line_number}: duplicate key {key}")
        values[key] = parse_scalar(raw_value)
    return values


def load_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    methods = manifest.get("methods", [])
    ids = tuple(method.get("id") for method in methods)
    if ids != METHOD_ORDER:
        raise ProfileError(f"method order mismatch: {ids!r}")
    return manifest


def validate_matrix(configs: dict[str, dict[str, Any]]) -> None:
    with MATRIX_PATH.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    expected_columns = {"parameter", "parser_key", *METHOD_ORDER, "classification"}
    if not rows or set(rows[0]) != expected_columns:
        raise ProfileError("parameter_matrix.csv has the wrong schema")
    for row in rows:
        parser_key = row["parser_key"]
        classification = row["classification"]
        if not parser_key or not classification.startswith("common"):
            continue
        expected_values = [parse_scalar(row[method]) for method in METHOD_ORDER]
        if expected_values.count(expected_values[0]) != len(expected_values):
            raise ProfileError(
                f"matrix common row {row['parameter']} differs across methods"
            )
        for method, expected in zip(METHOD_ORDER, expected_values):
            actual = configs[method].get(parser_key)
            if actual != expected:
                raise ProfileError(
                    f"{method}: {parser_key}={actual!r}, matrix expects {expected!r}"
                )


def validate_expected_hashes(actual_hashes: dict[str, str]) -> None:
    with CONFIG_HASH_PATH.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or set(rows[0]) != {"method", "path", "sha256"}:
        raise ProfileError("config_sha256.csv has the wrong schema")
    expected = {row["path"]: row["sha256"] for row in rows}
    if len(expected) != len(rows):
        raise ProfileError("config_sha256.csv contains duplicate paths")
    if expected != actual_hashes:
        differing = sorted(
            path
            for path in set(expected) | set(actual_hashes)
            if expected.get(path) != actual_hashes.get(path)
        )
        raise ProfileError(f"checked-in config SHA-256 mismatch: {differing}")


def validate() -> tuple[dict[str, Any], dict[str, str], list[dict[str, Any]]]:
    manifest = load_manifest()
    methods = {entry["id"]: entry for entry in manifest["methods"]}
    configs: dict[str, dict[str, Any]] = {}
    hashes: dict[str, str] = {}

    for method in METHOD_ORDER:
        path = PACKAGE_ROOT / methods[method]["config"]
        if not path.is_file():
            raise ProfileError(f"missing config for {method}: {path}")
        configs[method] = parse_flat_config(path)
        hashes[str(path.relative_to(PACKAGE_ROOT))] = sha256(path)

    # Strip comments through parsing and ignore only keys that the exact
    # upstream/ov parsers do not implement. Every other parsed parameter must
    # match exactly across all four configurations.
    normalized_common: dict[str, dict[str, Any]] = {
        method: {
            key: value
            for key, value in config.items()
            if key not in LOCAL_ONLY_KEYS
        }
        for method, config in configs.items()
    }
    reference = normalized_common["U-NS"]
    for method in METHOD_ORDER[1:]:
        if normalized_common[method] != reference:
            all_keys = sorted(set(reference) | set(normalized_common[method]))
            differences = [
                key
                for key in all_keys
                if reference.get(key) != normalized_common[method].get(key)
            ]
            raise ProfileError(
                f"non-method config divergence U-NS vs {method}: {differences}"
            )

    for key, required in REQUIRED_COMMON.items():
        actual = reference.get(key)
        if actual != required:
            raise ProfileError(f"required {key}={required!r}, got {actual!r}")

    for method in ("U-NS", "OV-SCHUR"):
        forbidden = sorted(LOCAL_ONLY_KEYS.intersection(configs[method]))
        if forbidden:
            raise ProfileError(f"{method} contains parser-inapplicable keys: {forbidden}")

    expected_local = {"L-NS": "nullspace", "L-SCHUR": "schur"}
    for method, expected_mode in expected_local.items():
        config = configs[method]
        if config.get("up_msckf_landmark_elimination") != expected_mode:
            raise ProfileError(f"{method} has wrong local elimination selector")
        if config.get("up_msckf_max_visual_passes") != 1:
            raise ProfileError(f"{method} is not explicitly one pass")

    for method in METHOD_ORDER:
        if methods[method].get("visual_passes") != 1:
            raise ProfileError(f"{method} manifest pass count is not one")

    validate_matrix(configs)

    shared = manifest["shared_external_configs"]
    for key in ("imu", "camera_imu"):
        path = PACKAGE_ROOT / shared[key]
        if not path.is_file():
            raise ProfileError(f"missing shared calibration/noise config: {path}")
        hashes[str(path.relative_to(PACKAGE_ROOT))] = sha256(path)

    validate_expected_hashes(hashes)

    normalized: dict[str, Any] = {}
    for method in METHOD_ORDER:
        normalized[method] = {
            "common_config": reference,
            "elimination_mode": methods[method]["elimination_mode"],
            "visual_passes": 1,
            "bag_start_seconds": 0.0,
        }

    diffs: list[dict[str, Any]] = []
    for left_index, left in enumerate(METHOD_ORDER):
        for right in METHOD_ORDER[left_index + 1 :]:
            left_semantic = {
                **reference,
                "elimination_mode": methods[left]["elimination_mode"],
                "visual_passes": 1,
                "bag_start_seconds": 0.0,
            }
            right_semantic = {
                **reference,
                "elimination_mode": methods[right]["elimination_mode"],
                "visual_passes": 1,
                "bag_start_seconds": 0.0,
            }
            changed = {
                key: {left: left_semantic[key], right: right_semantic[key]}
                for key in sorted(left_semantic)
                if left_semantic[key] != right_semantic[key]
            }
            forbidden = sorted(set(changed) - {"elimination_mode"})
            if forbidden:
                raise ProfileError(
                    f"semantic divergence {left} vs {right} outside method: {forbidden}"
                )
            diffs.append({"left": left, "right": right, "differences": changed})

    return normalized, dict(sorted(hashes.items())), diffs


def write_outputs(
    output: Path,
    normalized: dict[str, Any],
    hashes: dict[str, str],
    diffs: list[dict[str, Any]],
) -> None:
    output.mkdir(parents=True, exist_ok=False)
    (output / "NORMALIZED_PROFILES.json").write_text(
        json.dumps(normalized, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "CONFIG_SHA256.json").write_text(
        json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "NORMALIZED_DIFF.json").write_text(
        json.dumps(diffs, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="new output directory for normalized configs, diffs, and hashes",
    )
    args = parser.parse_args()
    try:
        normalized, hashes, diffs = validate()
        if args.output is not None:
            write_outputs(args.output.resolve(), normalized, hashes, diffs)
    except (OSError, ValueError, ProfileError) as exc:
        print(f"SUPPORTED_INTERSECTION_PROFILE: FAIL: {exc}", file=sys.stderr)
        return 1
    print("SUPPORTED_INTERSECTION_PROFILE: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
