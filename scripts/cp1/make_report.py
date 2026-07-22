#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Create the machine-readable SchurVIO-Lite CP1 test report."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import xml.etree.ElementTree as ET


SUMMARY_PATTERN = re.compile(r"^(CP1_[A-Z]+)\s+(.*)$")
EXPECTED_TEST_FILES = {
    "test_cp1_projection_jacobian.xml": 2,
    "test_cp1_rank_rejection.xml": 2,
    "test_cp1_schur_equivalence.xml": 1,
}
EXPECTED_TEST_CASES = {
    "CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences",
    "CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction",
    "CP1Schur.BoundaryAndInvalidInputsHaveExplicitStatus",
    "CP1Schur.DegenerateLandmarksAreRejectedDeterministically",
    "CP1Schur.FullJointNullspaceAndReducedSystemsAgree",
}
EXPECTED_SUMMARIES = {
    "CP1_EQUIVALENCE": {"fixtures": 128, "near_column_space_fixtures": 16, "seed": 20260728},
    "CP1_PROJECTION": {"fixtures": 256, "seed": 1900496914},
    "CP1_RANK": {"fixtures": 128, "seed": 1934903571},
    "CP1_RETRACTION": {"fixtures": 256, "seed": 32199698170528780},
}
REQUIRED_SUMMARY_FIELDS = {
    "CP1_EQUIVALENCE": {
        "max_covariance_error",
        "max_landmark_error",
        "max_nis_error",
        "max_state_error",
        "max_symmetry_ratio",
        "min_normalized_eigenvalue",
    },
    "CP1_PROJECTION": {
        "max_landmark_normalized_frobenius",
        "max_residual_definition_error",
        "max_residual_sign_normalized_frobenius",
        "max_state_normalized_frobenius",
    },
    "CP1_RANK": {"ill_conditioned", "rank_deficient"},
    "CP1_RETRACTION": {"max_normalized_frobenius"},
}
EXPECTED_BINARY_NAMES = {
    "test_cp1_projection_jacobian",
    "test_cp1_rank_rejection",
    "test_cp1_schur_equivalence",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--binary", action="append", type=Path, default=[])
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=str(repo), text=True).rstrip("\n")


def parse_value(value: str):
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_summaries(log_path: Path) -> dict:
    summaries = {}
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = SUMMARY_PATTERN.match(line)
        if not match:
            continue
        fields = {}
        for token in match.group(2).split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            fields[key] = parse_value(value)
        summaries[match.group(1)] = fields
    return summaries


def display_path(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root))
    except ValueError:
        return str(path.resolve())


def main() -> int:
    args = parse_args()
    artifact_dir = args.artifact_dir.resolve()
    repo_root = args.repo_root.resolve()
    xml_files = sorted(artifact_dir.glob("*.xml"))
    log_files = sorted(artifact_dir.glob("*.log"))
    if not xml_files or not log_files:
        raise SystemExit("CP1 report requires XML and log files")

    tests = 0
    failures = 0
    errors = 0
    disabled = 0
    suites = []
    test_cases = set()
    validation_errors = []
    actual_xml_names = {path.name for path in xml_files}
    expected_log_names = {name[:-4] + ".log" for name in EXPECTED_TEST_FILES}
    actual_log_names = {path.name for path in log_files}
    if actual_xml_names != set(EXPECTED_TEST_FILES):
        validation_errors.append(
            "XML file set mismatch: expected {} got {}".format(
                sorted(EXPECTED_TEST_FILES), sorted(actual_xml_names)
            )
        )
    if actual_log_names != expected_log_names:
        validation_errors.append(
            "log file set mismatch: expected {} got {}".format(
                sorted(expected_log_names), sorted(actual_log_names)
            )
        )
    for xml_path in xml_files:
        root = ET.parse(str(xml_path)).getroot()
        file_tests = int(root.attrib.get("tests", 0))
        tests += file_tests
        failures += int(root.attrib.get("failures", 0))
        errors += int(root.attrib.get("errors", 0))
        disabled += int(root.attrib.get("disabled", 0))
        if xml_path.name in EXPECTED_TEST_FILES and file_tests != EXPECTED_TEST_FILES[xml_path.name]:
            validation_errors.append(
                f"{xml_path.name}: expected {EXPECTED_TEST_FILES[xml_path.name]} tests, got {file_tests}"
            )
        for test_case in root.findall(".//testcase"):
            test_cases.add(f"{test_case.attrib.get('classname', '')}.{test_case.attrib.get('name', '')}")
        suites.append(
            {
                "file": xml_path.name,
                "name": root.attrib.get("name", xml_path.stem),
                "tests": int(root.attrib.get("tests", 0)),
                "failures": int(root.attrib.get("failures", 0)),
                "errors": int(root.attrib.get("errors", 0)),
                "disabled": int(root.attrib.get("disabled", 0)),
            }
        )

    summaries = {}
    for log_path in log_files:
        summaries.update(parse_summaries(log_path))
    if test_cases != EXPECTED_TEST_CASES:
        validation_errors.append(
            "test case set mismatch: expected {} got {}".format(
                sorted(EXPECTED_TEST_CASES), sorted(test_cases)
            )
        )
    if set(summaries) != set(EXPECTED_SUMMARIES):
        validation_errors.append(
            "summary set mismatch: expected {} got {}".format(
                sorted(EXPECTED_SUMMARIES), sorted(summaries)
            )
        )
    for summary_name, expected_fields in EXPECTED_SUMMARIES.items():
        summary = summaries.get(summary_name, {})
        for field, expected_value in expected_fields.items():
            if summary.get(field) != expected_value:
                validation_errors.append(
                    f"{summary_name}.{field}: expected {expected_value}, got {summary.get(field)!r}"
                )
        for field, value in summary.items():
            if isinstance(value, float) and not math.isfinite(value):
                validation_errors.append(f"{summary_name}.{field} is nonfinite")
        missing_fields = REQUIRED_SUMMARY_FIELDS[summary_name] - set(summary)
        if missing_fields:
            validation_errors.append(
                f"{summary_name}: missing required metrics {sorted(missing_fields)}"
            )
    projection = summaries.get("CP1_PROJECTION", {})
    for field in (
        "max_landmark_normalized_frobenius",
        "max_residual_sign_normalized_frobenius",
        "max_state_normalized_frobenius",
    ):
        if projection.get(field, math.inf) > 1.0e-5:
            validation_errors.append(f"CP1_PROJECTION.{field} exceeds 1e-5")
    if projection.get("max_residual_definition_error", math.inf) > 1.0e-12:
        validation_errors.append("CP1_PROJECTION.max_residual_definition_error exceeds 1e-12")
    equivalence = summaries.get("CP1_EQUIVALENCE", {})
    if equivalence.get("max_symmetry_ratio", math.inf) > 1.0e-10:
        validation_errors.append("CP1_EQUIVALENCE.max_symmetry_ratio exceeds 1e-10")
    if equivalence.get("min_normalized_eigenvalue", -math.inf) < -1.0e-10:
        validation_errors.append("CP1_EQUIVALENCE.min_normalized_eigenvalue is below -1e-10")
    rank = summaries.get("CP1_RANK", {})
    if rank.get("rank_deficient") != 96 or rank.get("ill_conditioned") != 32:
        validation_errors.append("CP1_RANK rejection totals differ from 96/32")
    if summaries.get("CP1_RETRACTION", {}).get("max_normalized_frobenius", math.inf) > 1.0e-7:
        validation_errors.append("CP1_RETRACTION.max_normalized_frobenius exceeds 1e-7")

    tracked_inputs = [
        repo_root / "docs/conventions.md",
        repo_root / "docs/iterated_update_spec.md",
        repo_root / "project/cp1_gate.yaml",
        repo_root / "ov_msckf/cmake/ROS1.cmake",
        repo_root / "ov_msckf/package.xml",
        repo_root / "ov_msckf/test/cp1/gtest_main.cpp",
        repo_root / "ov_msckf/test/cp1/cp1_fixture_utils.h",
        repo_root / "ov_msckf/test/cp1/test_schur_equivalence.cpp",
        repo_root / "ov_msckf/test/cp1/test_rank_rejection.cpp",
        repo_root / "ov_msckf/test/cp1/test_projection_jacobian.cpp",
        repo_root / "scripts/cp1/make_report.py",
        repo_root / "scripts/cp1/run_cp1.sh",
        repo_root / "scripts/cp1/verify_report.py",
        repo_root / "ov_core/src/cam/CamBase.h",
        repo_root / "ov_core/src/cam/CamRadtan.h",
        repo_root / "ov_core/src/types/JPLQuat.h",
        repo_root / "ov_core/src/types/PoseJPL.h",
        repo_root / "ov_core/src/utils/quat_ops.h",
        repo_root / "ov_msckf/src/state/StateHelper.cpp",
        repo_root / "ov_msckf/src/update/UpdaterHelper.cpp",
        repo_root / "ov_msckf/src/update/UpdaterMSCKF.cpp",
    ]
    input_hashes = {
        str(path.relative_to(repo_root)): sha256(path)
        for path in tracked_inputs
    }
    binary_names = {path.name for path in args.binary}
    if binary_names != EXPECTED_BINARY_NAMES or len(args.binary) != len(EXPECTED_BINARY_NAMES):
        validation_errors.append(
            "binary set mismatch: expected {} got {}".format(
                sorted(EXPECTED_BINARY_NAMES), sorted(binary_names)
            )
        )
    binary_hashes = {display_path(path, repo_root): sha256(path) for path in args.binary}
    dirty_lines = git(repo_root, "status", "--porcelain=v1").splitlines()
    passed = failures == 0 and errors == 0 and disabled == 0 and not validation_errors
    report = {
        "schema_version": 2,
        "checkpoint": "CP1",
        "evidence_scope": "automated_math_component_only",
        "overall_checkpoint_status": "in_progress_pending_human_signoff",
        "production_estimator_math_edits_permitted": False,
        "status": "passed" if passed else "failed",
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": {
            "commit": git(repo_root, "rev-parse", "HEAD"),
            "branch": git(repo_root, "branch", "--show-current"),
            "dirty": bool(dirty_lines),
            "status_porcelain_v1": dirty_lines,
            "input_sha256": input_hashes,
        },
        "host": {
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "contract": {
            "master_seed": 20260728,
            "well_conditioned_equivalence_fixtures": 128,
            "projection_finite_difference_fixtures": 256,
            "rank_rejection_fixtures": 128,
            "ill_conditioned_fixture_relative_singular_value": 1e-13,
            "landmark_relative_singular_floor": 1e-6,
        },
        "gtest": {
            "tests": tests,
            "failures": failures,
            "errors": errors,
            "disabled": disabled,
            "suites": suites,
            "test_cases": sorted(test_cases),
        },
        "validation_errors": validation_errors,
        "summaries": summaries,
        "binary_sha256": binary_hashes,
    }
    report_path = artifact_dir / "cp1_math_report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = sorted(path for path in artifact_dir.iterdir() if path.is_file() and path.name != "SHA256SUMS")
    checksum_text = "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_paths)
    (artifact_dir / "SHA256SUMS").write_text(checksum_text, encoding="utf-8")
    print(f"CP1 report {report['status']}: {report_path}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
