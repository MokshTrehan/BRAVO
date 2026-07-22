#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Independently verify a staged or retained CP1 automated evidence tree."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess


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
    if report.get("schema_version") != 2:
        errors.append("unsupported report schema")
    if report.get("checkpoint") != "CP1" or report.get("status") != "passed":
        errors.append("report does not record a passed CP1 automated run")
    if report.get("validation_errors") != []:
        errors.append("report contains validation errors")
    gtest = report.get("gtest", {})
    if (gtest.get("tests"), gtest.get("failures"), gtest.get("errors"), gtest.get("disabled")) != (5, 0, 0, 0):
        errors.append("unexpected gtest totals")

    source = report.get("source", {})
    commit = source.get("commit")
    for relative, digest in source.get("input_sha256", {}).items():
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
