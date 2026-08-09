#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independently verify a staged or retained CP1 automated evidence tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import subprocess


SCHEMA_2_REQUIRED_SOURCE_INPUTS = {
    "docs/conventions.md",
    "docs/iterated_update_spec.md",
    "ov_core/src/cam/CamBase.h",
    "ov_core/src/cam/CamRadtan.h",
    "ov_core/src/types/JPLQuat.h",
    "ov_core/src/types/PoseJPL.h",
    "ov_core/src/utils/quat_ops.h",
    "ov_msckf/cmake/ROS1.cmake",
    "ov_msckf/package.xml",
    "ov_msckf/src/state/StateHelper.cpp",
    "ov_msckf/src/update/UpdaterHelper.cpp",
    "ov_msckf/src/update/UpdaterMSCKF.cpp",
    "ov_msckf/test/cp1/cp1_fixture_utils.h",
    "ov_msckf/test/cp1/gtest_main.cpp",
    "ov_msckf/test/cp1/test_projection_jacobian.cpp",
    "ov_msckf/test/cp1/test_rank_rejection.cpp",
    "ov_msckf/test/cp1/test_schur_equivalence.cpp",
    "project/cp1_gate.yaml",
    "scripts/cp1/make_report.py",
    "scripts/cp1/run_cp1.sh",
    "scripts/cp1/verify_report.py",
}
SCHEMA_3_REQUIRED_SOURCE_INPUTS = SCHEMA_2_REQUIRED_SOURCE_INPUTS | {
    "docs/checkpoints.md",
    "docs/schurvio_lite_execution_plan.md",
    "ov_msckf/test/cp1/test_prior_and_compression.cpp",
}


SCHEMA_CONTRACTS = {
    2: {
        "evidence_scope": "automated_math_component_only",
        "overall_checkpoint_status": "in_progress_pending_human_signoff",
        "xml_tests": {
            "test_cp1_projection_jacobian.xml": 2,
            "test_cp1_rank_rejection.xml": 2,
            "test_cp1_schur_equivalence.xml": 1,
        },
        "test_cases": {
            "CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences",
            "CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction",
            "CP1Schur.BoundaryAndInvalidInputsHaveExplicitStatus",
            "CP1Schur.DegenerateLandmarksAreRejectedDeterministically",
            "CP1Schur.FullJointNullspaceAndReducedSystemsAgree",
        },
        "summaries": {"CP1_EQUIVALENCE", "CP1_PROJECTION", "CP1_RANK", "CP1_RETRACTION"},
        "binaries": {
            "test_cp1_projection_jacobian",
            "test_cp1_rank_rejection",
            "test_cp1_schur_equivalence",
        },
        "required_source_inputs": SCHEMA_2_REQUIRED_SOURCE_INPUTS,
    },
    3: {
        "evidence_scope": "automated_math_component_with_post_review_addendum",
        "overall_checkpoint_status": "in_progress_post_review_addendum_pending_fresh_signoff",
        "xml_tests": {
            "test_cp1_prior_and_compression.xml": 2,
            "test_cp1_projection_jacobian.xml": 2,
            "test_cp1_rank_rejection.xml": 2,
            "test_cp1_schur_equivalence.xml": 1,
        },
        "test_cases": {
            "CP1Compression.ProductionTruncationPreservesLambdaEtaButNotGamma",
            "CP1Prior.SemidefiniteCloneAugmentationMatchesInnovationUpdate",
            "CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences",
            "CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction",
            "CP1Schur.BoundaryAndInvalidInputsHaveExplicitStatus",
            "CP1Schur.DegenerateLandmarksAreRejectedDeterministically",
            "CP1Schur.FullJointNullspaceAndReducedSystemsAgree",
        },
        "summaries": {
            "CP1_COMPRESSION",
            "CP1_EQUIVALENCE",
            "CP1_PROJECTION",
            "CP1_PSD_PRIOR",
            "CP1_RANK",
            "CP1_RETRACTION",
        },
        "binaries": {
            "test_cp1_prior_and_compression",
            "test_cp1_projection_jacobian",
            "test_cp1_rank_rejection",
            "test_cp1_schur_equivalence",
        },
        "required_source_inputs": SCHEMA_3_REQUIRED_SOURCE_INPUTS,
    },
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("repo_root", type=Path)
    return parser.parse_args()


def committed_blob(repo_root: Path, commit: str, relative_path: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{commit}:{relative_path}"], cwd=str(repo_root)
    )


def main() -> int:
    args = parse_args()
    artifact_dir = args.artifact_dir.resolve()
    repo_root = args.repo_root.resolve()
    errors = []

    checksum_path = artifact_dir / "SHA256SUMS"
    if not checksum_path.is_file():
        raise SystemExit("missing SHA256SUMS")
    expected = {}
    for line_number, line in enumerate(checksum_path.read_text(encoding="utf-8").splitlines(), 1):
        fields = line.split("  ", 1)
        if len(fields) != 2 or len(fields[0]) != 64:
            errors.append(f"SHA256SUMS:{line_number}: malformed entry")
            continue
        digest, relative = fields
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or relative in expected:
            errors.append(f"SHA256SUMS:{line_number}: unsafe or duplicate path")
            continue
        expected[relative] = digest

    actual_files = {
        str(path.relative_to(artifact_dir))
        for path in artifact_dir.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    }
    if actual_files != set(expected):
        errors.append(
            "artifact file set differs from SHA256SUMS: expected {} got {}".format(
                sorted(expected), sorted(actual_files)
            )
        )
    for relative, digest in expected.items():
        path = artifact_dir / relative
        if not path.is_file() or sha256(path) != digest:
            errors.append(f"checksum mismatch: {relative}")

    report_path = artifact_dir / "cp1_math_report.json"
    if not report_path.is_file():
        errors.append("missing cp1_math_report.json")
        report = {}
    else:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    schema_version = report.get("schema_version")
    contract = SCHEMA_CONTRACTS.get(schema_version)
    if contract is None:
        errors.append("unsupported report schema")
        contract = SCHEMA_CONTRACTS[3]
    if report.get("checkpoint") != "CP1" or report.get("status") != "passed":
        errors.append("report does not record a passed CP1 automated run")
    if report.get("evidence_scope") != contract["evidence_scope"]:
        errors.append("report evidence scope differs from its schema contract")
    if report.get("overall_checkpoint_status") != contract["overall_checkpoint_status"]:
        errors.append("report does not preserve its schema's human-signoff blocker")
    if report.get("production_estimator_math_edits_permitted") is not False:
        errors.append("report incorrectly permits production estimator math edits")
    if report.get("validation_errors") != []:
        errors.append("report contains validation errors")

    expected_artifact_files = {"cp1_math_report.json"}
    for xml_name in contract["xml_tests"]:
        expected_artifact_files.add(xml_name)
        expected_artifact_files.add(xml_name[:-4] + ".log")
    if actual_files != expected_artifact_files:
        errors.append(
            "artifact inventory differs from schema contract: expected {} got {}".format(
                sorted(expected_artifact_files), sorted(actual_files)
            )
        )

    gtest = report.get("gtest", {})
    expected_test_total = sum(contract["xml_tests"].values())
    if (gtest.get("tests"), gtest.get("failures"), gtest.get("errors"), gtest.get("disabled")) != (
        expected_test_total,
        0,
        0,
        0,
    ):
        errors.append("unexpected gtest totals")
    reported_cases = gtest.get("test_cases", [])
    if len(reported_cases) != len(set(reported_cases)):
        errors.append("report contains duplicate test cases")
    if set(reported_cases) != contract["test_cases"]:
        errors.append("report test-case inventory differs from schema contract")
    suites = gtest.get("suites", [])
    suite_files = [suite.get("file") for suite in suites if isinstance(suite, dict)]
    if len(suite_files) != len(set(suite_files)):
        errors.append("report contains duplicate suite files")
    reported_xml_tests = {
        suite.get("file"): suite.get("tests") for suite in suites if isinstance(suite, dict)
    }
    if reported_xml_tests != contract["xml_tests"]:
        errors.append("report suite inventory differs from schema contract")

    summaries = report.get("summaries", {})
    if set(summaries) != contract["summaries"]:
        errors.append("report summary inventory differs from schema contract")
    binary_names = [PurePosixPath(path).name for path in report.get("binary_sha256", {})]
    if len(binary_names) != len(set(binary_names)) or set(binary_names) != contract["binaries"]:
        errors.append("report binary inventory differs from schema contract")

    if schema_version == 3:
        for summary_name, fields in {
            "CP1_COMPRESSION": (
                "max_eta_tolerance_ratio",
                "max_gamma_reconstruction_tolerance_ratio",
                "max_lambda_tolerance_ratio",
            ),
            "CP1_PSD_PRIOR": (
                "max_clone_nullspace_tolerance_ratio",
                "max_known_covariance_tolerance_ratio",
                "max_known_nis_tolerance_ratio",
                "max_known_state_tolerance_ratio",
                "max_spectral_covariance_tolerance_ratio",
                "max_spectral_nis_tolerance_ratio",
                "max_spectral_state_tolerance_ratio",
                "max_zero_eigenvalue_tolerance_ratio",
            ),
        }.items():
            summary = summaries.get(summary_name, {})
            for field in fields:
                value = summary.get(field)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value > 1.0:
                    errors.append(f"{summary_name}.{field} violates its frozen tolerance")
        compression = summaries.get("CP1_COMPRESSION", {})
        if compression.get("fixtures") != 128 or compression.get("seed") != 443998030361:
            errors.append("CP1_COMPRESSION fixture contract differs from schema 3")
        if compression.get("min_discarded_energy", -math.inf) <= 0.1:
            errors.append("CP1_COMPRESSION does not demonstrate positive gamma loss")
        psd_prior = summaries.get("CP1_PSD_PRIOR", {})
        if psd_prior.get("fixtures") != 128 or psd_prior.get("seed") != 21320732:
            errors.append("CP1_PSD_PRIOR fixture contract differs from schema 3")
        if psd_prior.get("min_posterior_normalized_eigenvalue", -math.inf) < -1.0e-10:
            errors.append("CP1_PSD_PRIOR posterior violates the PSD bound")

    source = report.get("source", {})
    if schema_version == 3 and (source.get("dirty") is not False or source.get("status_porcelain_v1") != []):
        errors.append("schema-3 evidence must come from a clean source commit")
    commit = source.get("commit")
    source_input_hashes = source.get("input_sha256", {})
    if not isinstance(source_input_hashes, dict):
        errors.append("source.input_sha256 is not a mapping")
        source_input_hashes = {}
    if set(source_input_hashes) != contract["required_source_inputs"]:
        errors.append("source-input inventory differs from schema contract")
    for relative, digest in source_input_hashes.items():
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            errors.append(f"unsafe source input path: {relative}")
            continue
        try:
            if source.get("dirty"):
                content_digest = sha256(repo_root / relative)
            else:
                content_digest = hashlib.sha256(committed_blob(repo_root, commit, relative)).hexdigest()
            if content_digest != digest:
                errors.append(f"source hash mismatch: {relative}")
        except (OSError, subprocess.CalledProcessError) as exc:
            errors.append(f"cannot verify source input {relative}: {exc}")

    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"CP1 automated evidence verified: {artifact_dir}")
    print(f"SHA256SUMS SHA-256: {sha256(checksum_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
