#!/usr/bin/env python3
"""Deterministic run-level provenance manifest for Schema-2 captures.

The capture reader's JSON manifest proves the integrity of the capture stream
itself.  This companion tool binds that stream to the ordinary replay
harness's command/result records, source and configuration identity, input
bag, estimator outputs, timing/resource records, ground truth, and evaluator
outputs.  It never runs the estimator and never modifies a replay directory.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

if __package__:
    from .capture_reader import CaptureValidationError, validate_capture
else:
    from capture_reader import CaptureValidationError, validate_capture


SPEC_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
TIMING_CLASSIFICATIONS = frozenset(("ACCEPTED", "CONTAMINATED"))
DATASET_FAMILIES = frozenset(("euroc_mav", "tum_vi"))

MANIFEST_COLUMNS = (
    "manifest_schema_version",
    "dataset_family",
    "sequence_id",
    "run_id",
    "timing_classification",
    "replay_head_commit",
    "replay_source_branch",
    "replay_source_status_json",
    "estimator_source_commit",
    "estimator_binary_path",
    "estimator_binary_bytes",
    "estimator_binary_sha256",
    "config_path",
    "config_bytes",
    "config_sha256",
    "dataset_path",
    "dataset_bytes",
    "dataset_sha256",
    "ground_truth_path",
    "ground_truth_bytes",
    "ground_truth_sha256",
    "capture_path",
    "capture_bytes",
    "capture_sha256",
    "capture_trailer_sha256",
    "capture_update_count",
    "capture_candidate_count",
    "capture_accepted_count",
    "capture_zero_candidate_count",
    "capture_no_update_count",
    "capture_first_timestamp",
    "capture_last_timestamp",
    "result_directory",
    "command_record_path",
    "command_record_sha256",
    "command_argv_json",
    "result_record_path",
    "result_record_sha256",
    "completed",
    "effective_exit_code",
    "roslaunch_exit_code",
    "estimator_child_exit",
    "timed_out",
    "elapsed_wall_seconds",
    "state_output_path",
    "state_output_sha256",
    "deviation_output_path",
    "deviation_output_sha256",
    "trajectory_output_path",
    "trajectory_output_sha256",
    "timing_output_path",
    "timing_output_sha256",
    "resource_output_path",
    "resource_output_sha256",
    "evaluation_paths_json",
    "evaluation_sha256_json",
    "recorded_output_sha256_json",
    "current_output_sha256_json",
)


class ManifestError(ValueError):
    """A replay record is incomplete, inconsistent, or has changed."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ManifestError(f"{label} is not a regular non-symlink file: {path}")
    return path


def _load_json(path: Path, label: str) -> Dict[str, Any]:
    _require_regular_file(path, label)
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise ManifestError(f"{label} exceeds {MAX_JSON_BYTES} bytes: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"invalid {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must contain a JSON object: {path}")
    return value


def _require_exact_keys(
    value: Mapping[str, Any], required: Iterable[str], optional: Iterable[str], label: str
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing or extra:
        raise ManifestError(f"{label} keys mismatch: missing={missing}, extra={extra}")


def _resolve_supplied_path(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a nonempty path string")
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _require_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ManifestError(f"{label} must be a JSON boolean")
    return value


def _require_int_or_none(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ManifestError(f"{label} must be an integer or null")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise ManifestError(f"{label} must be 64 lowercase hexadecimal digits")
    return value


def _require_commit(value: Any, label: str) -> str:
    if not isinstance(value, str) or not COMMIT_PATTERN.fullmatch(value):
        raise ManifestError(f"{label} must be 40 lowercase hexadecimal digits")
    return value


def _extract_launch_assignment(command: Any, key: str) -> str:
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        raise ManifestError("command.json command must be a string array")
    prefix = f"{key}:="
    matches = [item[len(prefix) :] for item in command if item.startswith(prefix)]
    if len(matches) != 1 or not matches[0]:
        raise ManifestError(f"command must contain exactly one nonempty {key}:= value")
    return matches[0]


def _require_launch_assignment(command: Any, key: str, expected: str) -> None:
    actual = _extract_launch_assignment(command, key)
    if actual != expected:
        raise ManifestError(
            f"command {key}:= value does not match command.json: "
            f"{actual!r} != {expected!r}"
        )


def _relative_output_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a nonempty relative path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ManifestError(f"{label} escapes result directory: {value!r}")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError(f"{label} escapes result directory: {value!r}") from exc
    return resolved


def _validate_recorded_output_hashes(
    result_directory: Path, value: Any
) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ManifestError("result.json output_sha256 must be an object")
    validated: Dict[str, str] = {}
    for relative in sorted(value):
        expected = _require_sha256(value[relative], f"output_sha256[{relative!r}]")
        path = _relative_output_path(
            result_directory, relative, f"output_sha256[{relative!r}]"
        )
        _require_regular_file(path, f"recorded output {relative!r}")
        actual = _sha256(path)
        if actual != expected:
            raise ManifestError(
                f"recorded output hash mismatch for {path}: {actual} != {expected}"
            )
        validated[relative] = actual
    if not validated:
        raise ManifestError("result.json output_sha256 is empty")
    return validated


def _is_allowed_ros_latest_symlink(path: Path, result_directory: Path) -> bool:
    try:
        relative = path.relative_to(result_directory)
    except ValueError:
        return False
    if relative.as_posix() != "ros_logs/latest":
        return False
    try:
        target = path.resolve(strict=True)
        target.relative_to(result_directory)
    except (FileNotFoundError, RuntimeError, ValueError):
        return False
    return target != result_directory and target.is_dir()


def _current_output_hashes(
    result_directory: Path, known_hashes: Mapping[str, str]
) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for path in sorted(result_directory.rglob("*")):
        if path.is_symlink():
            if _is_allowed_ros_latest_symlink(path, result_directory):
                continue
            raise ManifestError(f"result directory contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(result_directory).as_posix()
            result[relative] = known_hashes.get(relative) or _sha256(path)
    return result


def _recorded_file_hash(
    path: Path,
    result_directory: Path,
    recorded_hashes: Mapping[str, str],
    label: str,
    *,
    allow_external: bool = False,
) -> str:
    try:
        relative = path.relative_to(result_directory).as_posix()
    except ValueError:
        if allow_external:
            return _sha256(path)
        raise ManifestError(f"{label} is outside the result directory: {path}")
    if relative not in recorded_hashes:
        raise ManifestError(f"{label} is absent from result.json output_sha256: {relative}")
    return recorded_hashes[relative]


def _optional_provenance_file(
    supplied: Any, base: Path, label: str
) -> tuple[str, str, str]:
    if supplied is None:
        return "", "", ""
    path = _resolve_supplied_path(supplied, base, label)
    _require_regular_file(path, label)
    return str(path), str(path.stat().st_size), _sha256(path)


def _read_spec(spec_path: Path) -> tuple[Path, list[Dict[str, Any]]]:
    spec = _load_json(spec_path, "manifest specification")
    _require_exact_keys(spec, ("schema_version", "runs"), (), "manifest specification")
    if type(spec["schema_version"]) is not int or spec["schema_version"] != SPEC_SCHEMA_VERSION:
        raise ManifestError(
            f"manifest specification schema_version must equal {SPEC_SCHEMA_VERSION}"
        )
    runs = spec["runs"]
    if not isinstance(runs, list) or not runs:
        raise ManifestError("manifest specification runs must be a nonempty array")
    base = spec_path.parent.resolve()
    normalized: list[Dict[str, Any]] = []
    seen_directories: set[Path] = set()
    for index, run in enumerate(runs):
        label = f"runs[{index}]"
        if not isinstance(run, dict):
            raise ManifestError(f"{label} must be an object")
        _require_exact_keys(
            run,
            ("dataset_family", "result_directory", "timing_classification"),
            ("evaluation_paths", "ground_truth_path"),
            label,
        )
        family = run["dataset_family"]
        if family not in DATASET_FAMILIES:
            raise ManifestError(f"{label}.dataset_family must be one of {sorted(DATASET_FAMILIES)}")
        timing = run["timing_classification"]
        if timing not in TIMING_CLASSIFICATIONS:
            raise ManifestError(
                f"{label}.timing_classification must be one of "
                f"{sorted(TIMING_CLASSIFICATIONS)}"
            )
        result_directory = _resolve_supplied_path(
            run["result_directory"], base, f"{label}.result_directory"
        )
        if result_directory.is_symlink() or not result_directory.is_dir():
            raise ManifestError(f"{label}.result_directory is not a directory")
        if result_directory in seen_directories:
            raise ManifestError(f"duplicate result directory: {result_directory}")
        seen_directories.add(result_directory)
        evaluation_values = run.get("evaluation_paths", [])
        if not isinstance(evaluation_values, list):
            raise ManifestError(f"{label}.evaluation_paths must be an array")
        evaluations = sorted(
            (
                _resolve_supplied_path(value, base, f"{label}.evaluation_paths")
                for value in evaluation_values
            ),
            key=str,
        )
        if len(set(evaluations)) != len(evaluations):
            raise ManifestError(f"{label}.evaluation_paths contains a duplicate")
        normalized.append(
            {
                "dataset_family": family,
                "evaluation_paths": evaluations,
                "ground_truth_path": run.get("ground_truth_path"),
                "result_directory": result_directory,
                "timing_classification": timing,
                "spec_base": base,
            }
        )
    return base, normalized


def _manifest_row(run: Mapping[str, Any]) -> Dict[str, Any]:
    result_directory = run["result_directory"]
    command_path = _require_regular_file(
        result_directory / "command.json", "command record"
    )
    result_path = _require_regular_file(result_directory / "result.json", "result record")
    command_record = _load_json(command_path, "command record")
    result_record = _load_json(result_path, "result record")

    command = command_record.get("command")
    config_path = Path(_extract_launch_assignment(command, "config_path")).resolve()
    _require_regular_file(config_path, "configuration")
    config_hash = _sha256(config_path)
    if config_hash != _require_sha256(command_record.get("config_sha256"), "config_sha256"):
        raise ManifestError("configuration hash does not match command.json")

    dataset_path = _resolve_supplied_path(
        command_record.get("bag_path"), result_directory, "bag_path"
    )
    _require_regular_file(dataset_path, "dataset bag")
    dataset_hash = _sha256(dataset_path)
    if dataset_hash != _require_sha256(command_record.get("bag_sha256"), "bag_sha256"):
        raise ManifestError("dataset hash does not match command.json")
    bag_size = command_record.get("bag_size_bytes")
    if type(bag_size) is not int or bag_size != dataset_path.stat().st_size:
        raise ManifestError("dataset size does not match command.json")
    _require_launch_assignment(command, "bag", str(dataset_path))

    source = command_record.get("source")
    if not isinstance(source, dict):
        raise ManifestError("command.json source must be an object")
    replay_head_commit = _require_commit(source.get("head"), "source.head")
    source_branch = source.get("branch")
    source_status = source.get("status")
    if not isinstance(source_branch, str) or not isinstance(source_status, list) or not all(
        isinstance(item, str) for item in source_status
    ):
        raise ManifestError("source branch/status have invalid types")

    estimator_binary = _resolve_supplied_path(
        command_record.get("estimator_binary"), result_directory, "estimator_binary"
    )
    _require_regular_file(estimator_binary, "estimator binary")
    estimator_binary_hash = _sha256(estimator_binary)
    if estimator_binary_hash != _require_sha256(
        command_record.get("estimator_binary_sha256"), "estimator_binary_sha256"
    ):
        raise ManifestError("estimator binary hash does not match command.json")

    if _require_bool(
        command_record.get("update_envelope_capture_enabled"),
        "update_envelope_capture_enabled",
    ) is not True:
        raise ManifestError("replay did not enable Schema-2 capture")
    capture_path = _resolve_supplied_path(
        command_record.get("update_envelope_capture_path"),
        result_directory,
        "update_envelope_capture_path",
    )
    _require_regular_file(capture_path, "Schema-2 capture")
    capture_summary = validate_capture(capture_path)
    run_id = command_record.get("update_envelope_run_id")
    sequence_id = command_record.get("update_envelope_sequence_id")
    if (
        not isinstance(run_id, str)
        or not run_id
        or not isinstance(sequence_id, str)
        or not sequence_id
    ):
        raise ManifestError("Schema-2 run/sequence identifiers are absent")
    _require_launch_assignment(command, "capture_update_envelopes_v2", "true")
    _require_launch_assignment(
        command, "update_envelope_capture_path", str(capture_path)
    )
    _require_launch_assignment(command, "update_envelope_run_id", run_id)
    _require_launch_assignment(command, "update_envelope_sequence_id", sequence_id)
    header = capture_summary.header
    for actual, expected, label in (
        (header.config_sha256, config_hash, "configuration hash"),
        (header.run_id, run_id, "run ID"),
        (header.sequence_id, sequence_id, "sequence ID"),
    ):
        if actual != expected:
            raise ManifestError(f"capture {label} does not match command.json")

    capture_valid = _require_bool(
        result_record.get("update_envelope_capture_output_valid"),
        "update_envelope_capture_output_valid",
    )
    if not capture_valid:
        raise ManifestError("result.json reports an invalid Schema-2 capture")
    completed = _require_bool(result_record.get("completed"), "completed")
    timed_out = _require_bool(result_record.get("timed_out"), "timed_out")
    effective_exit = _require_int_or_none(
        result_record.get("effective_exit_code"), "effective_exit_code"
    )
    if effective_exit is None or completed != (effective_exit == 0):
        raise ManifestError("completed and effective_exit_code are inconsistent")
    exit_status_path = _require_regular_file(
        result_directory / "exit_status.txt", "exit status"
    )
    try:
        recorded_exit = int(exit_status_path.read_text(encoding="utf-8").strip())
    except ValueError as exc:
        raise ManifestError("exit_status.txt is not an integer") from exc
    if recorded_exit != effective_exit:
        raise ManifestError("exit_status.txt does not match result.json")
    elapsed = result_record.get("elapsed_wall_seconds")
    if type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        raise ManifestError("elapsed_wall_seconds must be finite and nonnegative")

    recorded_outputs = _validate_recorded_output_hashes(
        result_directory, result_record.get("output_sha256")
    )
    capture_hash = _recorded_file_hash(
        capture_path,
        result_directory,
        recorded_outputs,
        "Schema-2 capture",
        allow_external=True,
    )
    standard: Dict[str, tuple[Path, str]] = {}
    for key, filename in (
        ("state", "state_estimate.txt"),
        ("deviation", "state_deviation.txt"),
        ("trajectory", "trajectory_tum.txt"),
        ("timing", "timing.csv"),
        ("resource", "resource_usage.txt"),
    ):
        path = _require_regular_file(result_directory / filename, f"{key} output")
        standard[key] = (
            path,
            _recorded_file_hash(
                path, result_directory, recorded_outputs, f"{key} output"
            ),
        )

    evaluation_hashes: Dict[str, str] = {}
    for path in run["evaluation_paths"]:
        _require_regular_file(path, "evaluation output")
        evaluation_hashes[str(path)] = _sha256(path)
    ground_truth_path, ground_truth_bytes, ground_truth_hash = _optional_provenance_file(
        run["ground_truth_path"], run["spec_base"], "ground truth"
    )

    current_outputs = _current_output_hashes(result_directory, recorded_outputs)
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "dataset_family": run["dataset_family"],
        "sequence_id": sequence_id,
        "run_id": run_id,
        "timing_classification": run["timing_classification"],
        "replay_head_commit": replay_head_commit,
        "replay_source_branch": source_branch,
        "replay_source_status_json": _canonical_json(source_status),
        "estimator_source_commit": _require_commit(
            header.source_commit, "capture source commit"
        ),
        "estimator_binary_path": str(estimator_binary),
        "estimator_binary_bytes": estimator_binary.stat().st_size,
        "estimator_binary_sha256": estimator_binary_hash,
        "config_path": str(config_path),
        "config_bytes": config_path.stat().st_size,
        "config_sha256": config_hash,
        "dataset_path": str(dataset_path),
        "dataset_bytes": dataset_path.stat().st_size,
        "dataset_sha256": dataset_hash,
        "ground_truth_path": ground_truth_path,
        "ground_truth_bytes": ground_truth_bytes,
        "ground_truth_sha256": ground_truth_hash,
        "capture_path": str(capture_path),
        "capture_bytes": capture_path.stat().st_size,
        "capture_sha256": capture_hash,
        "capture_trailer_sha256": capture_summary.trailer_sha256,
        "capture_update_count": capture_summary.update_count,
        "capture_candidate_count": capture_summary.candidate_count,
        "capture_accepted_count": capture_summary.accepted_count,
        "capture_zero_candidate_count": capture_summary.zero_candidate_count,
        "capture_no_update_count": capture_summary.no_update_count,
        "capture_first_timestamp": capture_summary.first_timestamp,
        "capture_last_timestamp": capture_summary.last_timestamp,
        "result_directory": str(result_directory),
        "command_record_path": str(command_path),
        "command_record_sha256": _recorded_file_hash(
            command_path, result_directory, recorded_outputs, "command record"
        ),
        "command_argv_json": _canonical_json(command),
        "result_record_path": str(result_path),
        "result_record_sha256": _sha256(result_path),
        "completed": completed,
        "effective_exit_code": effective_exit,
        "roslaunch_exit_code": _require_int_or_none(
            result_record.get("roslaunch_exit_code"), "roslaunch_exit_code"
        ),
        "estimator_child_exit": _require_int_or_none(
            result_record.get("estimator_child_exit"), "estimator_child_exit"
        ),
        "timed_out": timed_out,
        "elapsed_wall_seconds": elapsed,
        "state_output_path": str(standard["state"][0]),
        "state_output_sha256": standard["state"][1],
        "deviation_output_path": str(standard["deviation"][0]),
        "deviation_output_sha256": standard["deviation"][1],
        "trajectory_output_path": str(standard["trajectory"][0]),
        "trajectory_output_sha256": standard["trajectory"][1],
        "timing_output_path": str(standard["timing"][0]),
        "timing_output_sha256": standard["timing"][1],
        "resource_output_path": str(standard["resource"][0]),
        "resource_output_sha256": standard["resource"][1],
        "evaluation_paths_json": _canonical_json(sorted(evaluation_hashes)),
        "evaluation_sha256_json": _canonical_json(evaluation_hashes),
        "recorded_output_sha256_json": _canonical_json(recorded_outputs),
        "current_output_sha256_json": _canonical_json(current_outputs),
    }


def build_manifest_rows(spec_path: os.PathLike[str] | str) -> list[Dict[str, Any]]:
    """Validate all declared replay directories and return deterministic rows."""

    resolved_spec = Path(spec_path).resolve()
    _, runs = _read_spec(resolved_spec)
    rows = [_manifest_row(run) for run in runs]
    rows.sort(
        key=lambda row: (
            row["dataset_family"],
            row["sequence_id"],
            row["run_id"],
            row["result_directory"],
        )
    )
    identities = [
        (row["dataset_family"], row["sequence_id"], row["run_id"]) for row in rows
    ]
    if len(set(identities)) != len(identities):
        raise ManifestError("duplicate dataset-family/sequence/run identity")
    return rows


def render_manifest_csv(rows: Sequence[Mapping[str, Any]]) -> str:
    """Render rows with a frozen column order and LF line endings."""

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=MANIFEST_COLUMNS,
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        normalized = {
            key: (
                "true"
                if value is True
                else "false"
                if value is False
                else ""
                if value is None
                else value
            )
            for key, value in row.items()
        }
        writer.writerow(normalized)
    return stream.getvalue()


def create_manifest_csv(
    output_path: os.PathLike[str] | str, spec_path: os.PathLike[str] | str
) -> list[Dict[str, Any]]:
    """Validate provenance and create a CSV without overwriting an existing file."""

    output = Path(output_path)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to overwrite manifest output: {output}")
    rows = build_manifest_rows(spec_path)
    serialized = render_manifest_csv(rows)
    with output.open("x", encoding="utf-8", newline="") as stream:
        stream.write(serialized)
        stream.flush()
        os.fsync(stream.fileno())
    return rows


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create deterministic SCHEMA2_CAPTURE_MANIFEST.csv provenance"
    )
    parser.add_argument("output", help="create-new CSV output path")
    parser.add_argument("spec", help="JSON run specification")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        rows = create_manifest_csv(args.output, args.spec)
    except (CaptureValidationError, ManifestError, OSError) as exc:
        print(_canonical_json({"error": str(exc), "status": "invalid"}), file=sys.stderr)
        return 1
    print(
        _canonical_json(
            {
                "manifest_sha256": _sha256(Path(args.output)),
                "output": str(Path(args.output).resolve()),
                "rows": len(rows),
                "status": "valid",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
