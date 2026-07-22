#!/usr/bin/python3
"""Independently verify a staged or immutable CP0 artifact tree."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Dict, List

from run_mh01 import (
    ANSI_ESCAPE,
    REPO_ROOT,
    metric_stats,
    projected_rows_match,
    reproducibility_input_identities,
    sha256_file,
    timestamps_match,
    timing_rows,
    total_state_rows,
    data_rows,
    pinned_commit,
)


REQUIRED_CHECKS = {
    "declared_serial_runner_source_delta_only",
    "roslaunch_exit_zero_without_timeout",
    "clean_console_and_single_estimator_lifetime",
    "deterministic_parameters_and_no_ground_truth_initialization",
    "roslaunch_parameter_resolution_exit_zero",
    "build_provenance_unchanged_after_run",
    "required_nonempty_artifacts",
    "trajectory_is_finite_monotonic_and_long_enough",
    "minimum_trajectory_length",
    "timing_is_finite_monotonic_and_long_enough",
    "minimum_timing_length",
    "full_state_estimate_is_finite_monotonic_and_complete",
    "full_state_deviation_is_finite_monotonic_and_complete",
    "synchronous_state_and_std_cover_every_timed_update",
    "full_selected_camera_interval_processed",
    "tum_trajectory_reorders_total_state_pose_columns",
    "trajectory_conversion_exit_zero_without_timeout",
    "ape_translation_exit_zero_without_timeout",
    "rpe_translation_1m_exit_zero_without_timeout",
    "rpe_rotation_1m_exit_zero_without_timeout",
    "valid_ape_translation_archive",
    "valid_rpe_translation_1m_archive",
    "valid_rpe_rotation_1m_deg_archive",
    "ape_translation_rmse_gate",
    "dataset_unchanged_during_run",
    "ros_bookkeeping_removed_before_sealing",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument(
        "--sha256sums-sha256",
        help="optional externally recorded SHA-256 of the run's SHA256SUMS file",
    )
    return parser.parse_args()


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(str(path) + " is not a JSON object")
    return value


def parse_checksums(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.rstrip("\n")
            if not line:
                continue
            if len(line) < 67 or line[64:66] != "  ":
                raise ValueError("SHA256SUMS:{}: malformed line".format(line_number))
            digest, relative = line[:64], line[66:]
            if any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("SHA256SUMS:{}: invalid digest".format(line_number))
            candidate = Path(relative)
            if candidate.is_absolute() or ".." in candidate.parts or relative in result:
                raise ValueError("SHA256SUMS:{}: unsafe or duplicate path".format(line_number))
            result[relative] = digest
    return result


def require_flag_value(argv: List[str], flag: str, expected: str) -> bool:
    try:
        index = argv.index(flag)
    except ValueError:
        return False
    return index + 1 < len(argv) and argv[index + 1] == expected


def verify_protocol(manifest: Dict[str, Any], errors: List[str]) -> None:
    spec = manifest["configuration"]["baseline_spec"]
    commands = manifest["runtime"]["commands"]
    tolerance = str(spec["evaluation"]["timestamp_association_max_difference_seconds"])
    delta = str(spec["evaluation"]["rpe_delta"])
    delta_unit = str(spec["evaluation"]["rpe_delta_unit"])
    definitions = {
        "ape_translation": ("trans_part", False),
        "rpe_translation_1m": ("trans_part", True),
        "rpe_rotation_1m": ("angle_deg", True),
    }
    for name, (relation, is_rpe) in definitions.items():
        argv = commands.get(name, {}).get("argv", [])
        if commands.get(name, {}).get("exit_code") != 0 or commands.get(name, {}).get("timed_out"):
            errors.append(name + " did not exit zero without timeout")
        if not require_flag_value(argv, "--t_max_diff", tolerance):
            errors.append(name + " does not freeze --t_max_diff " + tolerance)
        if not require_flag_value(argv, "--pose_relation", relation):
            errors.append(name + " has the wrong pose relation")
        if "--align" not in argv or "--correct_scale" in argv or "-s" in argv:
            errors.append(name + " is not fixed SE(3) alignment without scale")
        if is_rpe:
            if not require_flag_value(argv, "--delta", delta):
                errors.append(name + " has the wrong RPE delta")
            if not require_flag_value(argv, "--delta_unit", delta_unit):
                errors.append(name + " has the wrong RPE delta unit")
            if "--all_pairs" not in argv or "--pairs_from_reference" not in argv:
                errors.append(name + " is not all-pairs-from-reference RPE")


def verify_console_and_parameters(run_dir: Path, errors: List[str]) -> None:
    console = ANSI_ESCAPE.sub(
        "", (run_dir / "console.log").read_text(encoding="utf-8", errors="replace")
    )
    clean = re.findall(
        r"REQUIRED process \[cp0_vio-[0-9]+\] has died!\s*\nprocess has finished cleanly",
        console,
    )
    starts = re.findall(r"process\[cp0_vio-[0-9]+\]: started with pid", console)
    if len(clean) != 1 or len(starts) != 1:
        errors.append("console lacks one clean estimator lifecycle witness")
    fatal = re.compile(
        r"(?i)segmentation fault|terminate called|uncaught exception|\[FATAL\]|"
        r"process \[[^\]]+\] has died[^\n]*exit code [1-9]|"
        r"(?<![A-Za-z])(?:nan|[-+]?inf(?:inity)?)(?![A-Za-z])|"
        r"covariance[^\n]*(?:invalid|failed|failure|not positive)"
    )
    if fatal.search(console):
        errors.append("console contains a fatal/non-finite marker")

    parameters = (run_dir / "resolved_ros_parameters.yaml").read_text(encoding="utf-8")
    expected = {
        "num_opencv_threads": r"^/cp0_vio/num_opencv_threads:\s*0\s*$",
        "multi_threading_pubs": r"^/cp0_vio/multi_threading_pubs:\s*false\s*$",
        "multi_threading_subs": r"^/cp0_vio/multi_threading_subs:\s*false\s*$",
        "calib_cam_extrinsics": r"^/cp0_vio/calib_cam_extrinsics:\s*false\s*$",
        "calib_cam_intrinsics": r"^/cp0_vio/calib_cam_intrinsics:\s*false\s*$",
        "calib_cam_timeoffset": r"^/cp0_vio/calib_cam_timeoffset:\s*false\s*$",
        "save_total_state": r"^/cp0_vio/save_total_state:\s*true\s*$",
        "filepath_est": r"^/cp0_vio/filepath_est:\s*.+state_estimate\.txt\s*$",
        "filepath_std": r"^/cp0_vio/filepath_std:\s*.+state_deviation\.txt\s*$",
    }
    for name, pattern in expected.items():
        if re.search(pattern, parameters, re.MULTILINE | re.IGNORECASE) is None:
            errors.append("resolved parameter is not frozen: " + name)
    if re.search(r"^/cp0_vio/path_gt:", parameters, re.MULTILINE):
        errors.append("ground truth was exposed to the estimator")


def main() -> int:
    args = parse_args()
    run_dir = args.run_directory.resolve()
    errors: List[str] = []
    try:
        if not run_dir.is_dir():
            raise ValueError("run directory does not exist: " + str(run_dir))
        manifest = read_json(run_dir / "manifest.json")
        validation = read_json(run_dir / "validation.json")
        trusted_spec = read_json(REPO_ROOT / "project" / "cp0_baseline.json")
        checksums = parse_checksums(run_dir / "SHA256SUMS")

        actual_files: Dict[str, Path] = {}
        for path in run_dir.rglob("*"):
            relative = path.relative_to(run_dir).as_posix()
            if path.is_symlink():
                errors.append("artifact tree contains symlink: " + relative)
            elif path.is_file() and relative != "SHA256SUMS":
                actual_files[relative] = path
        if set(actual_files) != set(checksums):
            errors.append(
                "checksum coverage differs: missing={} extra={}".format(
                    sorted(set(actual_files) - set(checksums)),
                    sorted(set(checksums) - set(actual_files)),
                )
            )
        for relative, path in actual_files.items():
            expected = checksums.get(relative)
            if expected is not None and sha256_file(path) != expected:
                errors.append("checksum mismatch: " + relative)

        if manifest.get("schema_version") != 1 or manifest.get("checkpoint") != "CP0":
            errors.append("unsupported manifest schema/checkpoint")
        if manifest.get("status") != "passed" or validation.get("passed") is not True:
            errors.append("run is not marked passed")
        if manifest.get("validation") != validation:
            errors.append("manifest validation copy differs from validation.json")
        if manifest.get("run_id") != run_dir.name:
            errors.append("manifest run_id does not match directory name")
        check_names = [check.get("name") for check in validation.get("checks", [])]
        if set(check_names) != REQUIRED_CHECKS or len(check_names) != len(REQUIRED_CHECKS):
            errors.append("validation does not contain the exact canonical CP0 gate set")
        failed_checks = [
            check.get("name") for check in validation.get("checks", []) if check.get("passed") is not True
        ]
        if failed_checks:
            errors.append("validation contains failed checks: " + ", ".join(str(item) for item in failed_checks))

        spec = manifest["configuration"]["baseline_spec"]
        if spec != trusted_spec:
            errors.append("artifact baseline spec differs from trusted repository spec")
        snapshotted_spec = read_json(
            run_dir / manifest["reproducibility_snapshot"]["project/cp0_baseline.json"]
        )
        if snapshotted_spec != trusted_spec:
            errors.append("snapshotted baseline spec differs from trusted repository spec")
        dataset = manifest["dataset"]["identity"]
        frozen_bag = spec["dataset"]["bag_identity"]
        for key in ("size_bytes", "sha256"):
            if dataset.get(key) != frozen_bag.get(key):
                errors.append("dataset identity mismatch for " + key)

        source = manifest["source"]
        if source.get("pinned_upstream_base") != pinned_commit():
            errors.append("manifest upstream base differs from trusted UPSTREAM_REVISION")
        diff_path = run_dir / source["tracked_delta_from_pinned_base_artifact"]
        if sha256_file(diff_path) != source["tracked_delta_from_pinned_base_sha256"]:
            errors.append("persisted source delta hash mismatch")
        trusted_runner_path = trusted_spec["frozen_source"]["serial_runner_patch_path"]
        if source.get("build_source_changes_from_pinned_base") != [trusted_runner_path]:
            errors.append("undeclared build-source changes are present")
        if source.get("allowed_build_source_changes") != [trusted_runner_path]:
            errors.append("artifact changed the allowed build-source policy")
        trusted_patch_hash = trusted_spec["frozen_source"]["serial_runner_patch_sha256"]
        patch_path = run_dir / source["serial_runner_patch_artifact"]
        if sha256_file(patch_path) != trusted_patch_hash:
            errors.append("persisted serial-runner patch differs from trusted lifecycle patch")
        if source.get("serial_runner_patch_sha256") != trusted_patch_hash:
            errors.append("serial-runner patch is not the frozen lifecycle patch")

        snapshots = manifest.get("reproducibility_snapshot", {})
        trusted_inputs = reproducibility_input_identities()
        if manifest.get("reproducibility_inputs") != trusted_inputs:
            errors.append("artifact reproducibility-input identities differ from trusted repository inputs")
        if set(snapshots) != set(trusted_inputs):
            errors.append("artifact reproducibility snapshot set is incomplete or unexpected")
        for relative, identity in trusted_inputs.items():
            snapshot_relative = snapshots.get(relative)
            if not snapshot_relative:
                errors.append("missing reproducibility snapshot mapping: " + relative)
                continue
            candidate = Path(snapshot_relative)
            if candidate.is_absolute() or ".." in candidate.parts:
                errors.append("unsafe reproducibility snapshot path: " + str(snapshot_relative))
                continue
            path = (run_dir / candidate).resolve()
            try:
                path.relative_to(run_dir)
            except ValueError:
                errors.append("reproducibility snapshot escapes run directory: " + str(snapshot_relative))
                continue
            if not path.is_file() or path.stat().st_size != identity["size_bytes"]:
                errors.append("reproducibility snapshot missing or wrong size: " + relative)
            elif sha256_file(path) != identity["sha256"]:
                errors.append("reproducibility snapshot hash mismatch: " + relative)
        for relative, trusted_hash in trusted_spec["frozen_source"]["input_sha256"].items():
            snapshot_relative = snapshots.get(relative)
            if not snapshot_relative or sha256_file(run_dir / snapshot_relative) != trusted_hash:
                errors.append("frozen config/ground-truth snapshot mismatch: " + relative)

        state_estimate = total_state_rows(run_dir / "state_estimate.txt", 19)
        state_deviation = total_state_rows(run_dir / "state_deviation.txt", 18)
        trajectory = data_rows(run_dir / "trajectory_tum.txt", 8)
        timing = timing_rows(run_dir / "timing_openvins.csv")
        if not (
            state_estimate[0] == state_deviation[0] == trajectory[0] == timing[0]
            and timestamps_match(run_dir / "state_estimate.txt", run_dir / "state_deviation.txt")
            and timestamps_match(run_dir / "state_estimate.txt", run_dir / "trajectory_tum.txt")
            and timestamps_match(run_dir / "state_estimate.txt", run_dir / "timing_openvins.csv")
        ):
            errors.append("synchronous state/std logs do not cover every timed update")
        completion = spec["dataset"]["completion_witness"]
        if timing[0] != completion["expected_timing_rows"]:
            errors.append("timing row count does not prove full bag completion")
        if abs(timing[2] - completion["final_selected_camera_timestamp_seconds"]) > completion[
            "timestamp_tolerance_seconds"
        ]:
            errors.append("run did not reach the frozen final selected-camera timestamp")
        if not projected_rows_match(
            run_dir / "state_estimate.txt", run_dir / "trajectory_tum.txt"
        ):
            errors.append("TUM trajectory does not correctly reorder total-state pose columns")

        metrics = {
            "ape_translation": metric_stats(run_dir / "evo_ape_translation.zip"),
            "rpe_translation_1m": metric_stats(run_dir / "evo_rpe_translation_1m.zip"),
            "rpe_rotation_1m_deg": metric_stats(run_dir / "evo_rpe_rotation_1m_deg.zip"),
        }
        for name, metric in metrics.items():
            if metric["samples"] <= 0 or any(
                not math.isfinite(float(value)) for value in metric["stats"].values()
            ):
                errors.append(name + " has invalid statistics")
        ate = metrics["ape_translation"]["stats"]["rmse"]
        if ate > spec["evaluation"]["ape_translation_rmse_max_m"]:
            errors.append("APE translation RMSE exceeds the frozen gate")
        verify_protocol(manifest, errors)
        verify_console_and_parameters(run_dir, errors)
        for command_name in ("roslaunch", "trajectory_conversion"):
            record = manifest["runtime"]["commands"].get(command_name, {})
            if record.get("exit_code") != 0 or record.get("timed_out"):
                errors.append(command_name + " did not exit zero without timeout")
        if manifest["runtime"]["commands"].get("verify_build_after_run", {}).get(
            "exit_code"
        ) != 0:
            errors.append("post-run build provenance verification failed")

        if args.sha256sums_sha256 and sha256_file(run_dir / "SHA256SUMS") != args.sha256sums_sha256:
            errors.append("SHA256SUMS does not match the external digest anchor")

        if (run_dir / "ros-home").exists() or (run_dir / "ros-logs" / "latest").exists():
            errors.append("unsealed ROS bookkeeping remains in the artifact tree")
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        errors.append(str(exc))

    if errors:
        print("CP0 verification FAILED", file=sys.stderr)
        for error in errors:
            print("  - " + error, file=sys.stderr)
        return 1
    print("CP0 verification PASSED: " + str(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
