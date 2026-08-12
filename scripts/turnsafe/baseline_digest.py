#!/usr/bin/python3
"""Validate and hash the stable fields exposed by a frozen baseline replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from typing import Dict, List, Optional, Sequence, Tuple


DIGEST_SCHEMA = "turnsafe.baseline_digest.v1"

REQUIRED_FILES = {
    "state": "state_estimate.txt",
    "deviation": "state_deviation.txt",
    "trajectory": "trajectory_tum.txt",
    "callback_timestamps": "timing_openvins.csv",
}

UNAVAILABLE_BASELINE_FIELDS = [
    "accepted_feature_ids",
    "accepted_observation_ids",
    "callback_accept_reject_decision",
    "feature_lifecycle_transitions",
    "state_commit_count",
    "terminal_finalization_count",
]

NONDETERMINISTIC_EXCLUSIONS = [
    "console_and_ros_log_text",
    "host_user_pid_tid_and_absolute_paths",
    "manifest_creation_and_wall_clock_timestamps",
    "ros_master_port_and_process_names",
    "timing_openvins_duration_columns",
    "wall_clock_runtime_and_build_progress",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a TurnSafe Session-0 baseline replay and write a "
            "deterministic digest manifest."
        )
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    return parser.parse_args()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_rows(
    path: Path, delimiter: Optional[str] = None
) -> Tuple[List[str], int]:
    rows: List[str] = []
    columns: Optional[int] = None
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = (
                [part.strip() for part in stripped.split(delimiter)]
                if delimiter is not None
                else stripped.split()
            )
            if not fields or any(field == "" for field in fields):
                raise ValueError(f"{path}:{line_number}: empty field")
            try:
                values = [float(field) for field in fields]
            except ValueError as exc:
                raise ValueError(
                    f"{path}:{line_number}: non-numeric field"
                ) from exc
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"{path}:{line_number}: non-finite field")
            if columns is None:
                columns = len(fields)
            elif len(fields) != columns:
                raise ValueError(
                    f"{path}:{line_number}: expected {columns} columns, "
                    f"got {len(fields)}"
                )
            rows.append(" ".join(fields))
    if not rows or columns is None:
        raise ValueError(f"{path}: no numeric rows")
    return rows, columns


def timestamps(rows: Sequence[str]) -> List[str]:
    return [row.split()[0] for row in rows]


def validate_strictly_increasing(values: Sequence[str], label: str) -> None:
    previous = -math.inf
    for index, value in enumerate(values):
        current = float(value)
        if current <= previous:
            raise ValueError(
                f"{label}: timestamp {index} is not strictly increasing"
            )
        previous = current


def stable_field_record(rows: Sequence[str], columns: int) -> Dict[str, object]:
    canonical = ("\n".join(rows) + "\n").encode("utf-8")
    return {
        "canonical_sha256": sha256_bytes(canonical),
        "columns": columns,
        "rows": len(rows),
    }


def build_manifest(run_dir: Path, source_sha: str) -> Dict[str, object]:
    if len(source_sha) != 40 or any(
        byte not in "0123456789abcdef" for byte in source_sha
    ):
        raise ValueError("--source-sha must be a lowercase 40-character Git SHA")
    if not run_dir.is_dir():
        raise ValueError(f"run directory does not exist: {run_dir}")

    paths = {name: run_dir / leaf for name, leaf in REQUIRED_FILES.items()}
    for path in paths.values():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"required input is not a regular non-symlink file: {path}")

    state_rows, state_columns = canonical_rows(paths["state"])
    deviation_rows, deviation_columns = canonical_rows(paths["deviation"])
    trajectory_rows, trajectory_columns = canonical_rows(paths["trajectory"])
    timing_rows, timing_columns = canonical_rows(
        paths["callback_timestamps"], delimiter=","
    )

    state_times = timestamps(state_rows)
    deviation_times = timestamps(deviation_rows)
    trajectory_times = timestamps(trajectory_rows)
    callback_times = timestamps(timing_rows)
    for label, values in (
        ("state", state_times),
        ("deviation", deviation_times),
        ("trajectory", trajectory_times),
        ("callback_timestamps", callback_times),
    ):
        validate_strictly_increasing(values, label)

    if state_times != deviation_times or state_times != trajectory_times:
        raise ValueError("state/deviation/trajectory timestamp sequences differ")
    if len(callback_times) != len(state_times):
        raise ValueError("callback and state row counts differ")
    for index, (callback_time, state_time) in enumerate(
        zip(callback_times, state_times)
    ):
        if abs(float(callback_time) - float(state_time)) > 1.0e-5:
            raise ValueError(
                f"callback/state timestamp mismatch at row {index}"
            )

    callback_payload = ("\n".join(callback_times) + "\n").encode("utf-8")
    fields: Dict[str, Dict[str, object]] = {
        "callback_timestamps": {
            "canonical_sha256": sha256_bytes(callback_payload),
            "columns_hashed": 1,
            "rows": len(callback_times),
        },
        "deviation": stable_field_record(deviation_rows, deviation_columns),
        "state": stable_field_record(state_rows, state_columns),
        "trajectory": stable_field_record(trajectory_rows, trajectory_columns),
    }

    combined = hashlib.sha256()
    combined.update((DIGEST_SCHEMA + "\0").encode("ascii"))
    for name in sorted(fields):
        combined.update((name + "\0").encode("ascii"))
        combined.update(
            (str(fields[name]["canonical_sha256"]) + "\0").encode("ascii")
        )

    return {
        "schema": DIGEST_SCHEMA,
        "source_sha": source_sha,
        "combined_stable_sha256": combined.hexdigest(),
        "stable_fields": fields,
        "input_file_sha256": {
            REQUIRED_FILES[name]: sha256_file(path)
            for name, path in sorted(paths.items())
        },
        "validation": {
            "all_numeric_fields_finite": True,
            "callback_state_timestamp_tolerance_seconds": 1.0e-5,
            "row_counts_equal": True,
            "state_deviation_trajectory_timestamps_exact": True,
            "timestamps_strictly_increasing": True,
            "timing_columns_observed": timing_columns,
        },
        "unavailable_baseline_fields": UNAVAILABLE_BASELINE_FIELDS,
        "nondeterministic_exclusions": NONDETERMINISTIC_EXCLUSIONS,
    }


def write_json_atomic(path: Path, value: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main() -> int:
    args = parse_args()
    try:
        manifest = build_manifest(args.run_dir.resolve(), args.source_sha)
        write_json_atomic(args.output, manifest)
    except (OSError, ValueError) as exc:
        print(f"baseline digest failed: {exc}", file=sys.stderr)
        return 1
    print(manifest["combined_stable_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
