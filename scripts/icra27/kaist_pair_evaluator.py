#!/usr/bin/python3
"""Deterministic common-population evaluator for a completed KAIST U0/S1 pair.

This is the primary accuracy evaluator for the G0.5 diagnostic screen.  It
does not mutate either scored run.  It first associates each estimate to the
same ground-truth source file, intersects exact ground-truth row identities,
then writes timestamp-normalized common-population TUM files.  Both systems
are independently aligned with evo's SE(3) Umeyama implementation (scale is
fixed to one), and both RPE metrics are checked against one materialized
reference-derived 1 m pair set.

The output directory is constructed off to the side and installed atomically.
Any provenance, association, population, alignment, or metric ambiguity is a
hard failure.
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import struct
import sys
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import uuid

import numpy as np
import numpy.core.numeric as numpy_core_numeric
import numpy.linalg._umath_linalg as numpy_umath_linalg
import numpy.linalg.linalg as numpy_linalg_implementation
import yaml

import evo
from evo.core import (
    filters,
    geometry,
    lie_algebra,
    metrics,
    result as evo_result,
    sync,
    trajectory,
    transformations,
    units,
)
from evo.tools import file_interface


SCHEMA = "schurvio.icra27.kaist_pair_evaluator.v1"
ASSOCIATION_SCHEMA = "schurvio.icra27.kaist_pair_associations.v1"
CROP_SCHEMA = "schurvio.icra27.kaist_common_population.v1"
RUN_SCHEMA = "schurvio.icra27.kaist_upstream_screen.result.v1"
PROTOCOL_SCHEMA = "schurvio.icra27.kaist_upstream_screen.v1"
EXPECTED_PROTOCOL_ID = "g0.5-kaist11-u0-s1-20260815"
EXPECTED_EVO_VERSION = "1.31.1"
ELIGIBLE_RUN_STATUSES = {"COMPLETE", "COMPLETE_WITH_TEARDOWN_DEFECT"}
SYSTEMS = ("U0", "S1")

ASSOCIATION_MAX_SECONDS = 0.01
ASSOCIATION_OFFSET_SECONDS = 0.0
RPE_DELTA_METERS = 1.0
RPE_RELATIVE_DELTA_TOLERANCE = 0.1
RPE_ALL_PAIRS = True
RPE_PAIRS_FROM_REFERENCE = True
ALIGNMENT_POSE_COUNT = -1  # evo spelling for all poses.
MINIMUM_COMMON_POSES = 10
MINIMUM_RPE_PAIRS = 5
RUNTIME_IDENTITY_SCHEMA = "schurvio.icra27.kaist_runtime_identity.v1"
EVALUATOR_RUNTIME_PINS_SCHEMA = (
    "schurvio.icra27.kaist_pair_evaluator.runtime_pins.v1"
)
RUNTIME_IDENTITY_TOOL = Path(__file__).resolve().with_name(
    "kaist_runtime_identity.py"
)


class PairEvaluationError(RuntimeError):
    """A fail-closed paired-evaluation contract violation."""


@dataclass(frozen=True)
class TumRow:
    """One exact source row plus its binary64 interpretation."""

    data_index: int
    source_line_number: int
    source_bytes: bytes
    tokens: Tuple[str, ...]
    values: Tuple[float, ...]
    row_id: str

    @property
    def timestamp(self) -> float:
        return self.values[0]

    @property
    def timestamp_text(self) -> str:
        return self.tokens[0]


@dataclass(frozen=True)
class Association:
    gt_index: int
    estimate_index: int
    difference_seconds: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strict_file_identity(path: Path) -> Dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise PairEvaluationError(f"not a regular file: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def relative_identity(root: Path, path: Path) -> Dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise PairEvaluationError(f"output escaped staging directory: {resolved}") from exc
    if not resolved.is_file():
        raise PairEvaluationError(f"output is not a regular file: {relative}")
    return {
        "path": relative.as_posix(),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def float_record(value: float) -> Dict[str, Any]:
    if not math.isfinite(value):
        raise PairEvaluationError("attempted to record a nonfinite float")
    return {
        "decimal_17g": format(value, ".17g"),
        "binary64_hex": struct.pack(">d", value).hex(),
    }


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise PairEvaluationError(f"refusing to overwrite output file: {path}")
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    if path.exists():
        raise PairEvaluationError(f"refusing to overwrite output file: {path}")
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def write_tum(path: Path, rows: Sequence[TumRow], timestamp_rows: Sequence[TumRow]) -> None:
    if len(rows) != len(timestamp_rows):
        raise PairEvaluationError("TUM pose/timestamp row count mismatch")
    if path.exists():
        raise PairEvaluationError(f"refusing to overwrite output file: {path}")
    with path.open("x", encoding="ascii", newline="\n") as stream:
        stream.write("# timestamp tx ty tz qx qy qz qw\n")
        for pose_row, timestamp_row in zip(rows, timestamp_rows):
            stream.write(" ".join((timestamp_row.timestamp_text, *pose_row.tokens[1:])))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_tum_exact(path: Path, label: str) -> List[TumRow]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise PairEvaluationError(f"{label} is not a regular file: {resolved}")
    result: List[TumRow] = []
    for line_number, source_line in enumerate(resolved.read_bytes().splitlines(), 1):
        stripped = source_line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        try:
            text = stripped.decode("ascii")
        except UnicodeDecodeError as exc:
            raise PairEvaluationError(
                f"{label} has a non-ASCII data row at line {line_number}"
            ) from exc
        tokens = tuple(text.split())
        if len(tokens) != 8:
            raise PairEvaluationError(
                f"{label} row {line_number} has {len(tokens)} columns, expected 8"
            )
        try:
            values = tuple(float(token) for token in tokens)
        except ValueError as exc:
            raise PairEvaluationError(
                f"{label} has a nonnumeric row at line {line_number}"
            ) from exc
        if not all(math.isfinite(value) for value in values):
            raise PairEvaluationError(
                f"{label} has a nonfinite row at line {line_number}"
            )
        row_index = len(result)
        row_id = hashlib.sha256(
            str(row_index).encode("ascii")
            + b"\0"
            + str(line_number).encode("ascii")
            + b"\0"
            + stripped
        ).hexdigest()
        result.append(
            TumRow(
                data_index=row_index,
                source_line_number=line_number,
                source_bytes=stripped,
                tokens=tokens,
                values=values,
                row_id=row_id,
            )
        )
    if not result:
        raise PairEvaluationError(f"{label} contains no data rows")
    timestamps = np.asarray([row.timestamp for row in result], dtype=np.float64)
    if not np.all(np.isfinite(timestamps)):
        raise PairEvaluationError(f"{label} contains a nonfinite timestamp")
    if not np.all(np.diff(timestamps) > 0.0):
        raise PairEvaluationError(
            f"{label} timestamps are not unique and strictly increasing"
        )
    parsed = rows_to_evo(result)
    valid, details = parsed.check()
    if not valid:
        raise PairEvaluationError(f"{label} is not a valid evo trajectory: {details}")
    return result


def rows_to_evo(rows: Sequence[TumRow]) -> trajectory.PoseTrajectory3D:
    values = np.asarray([row.values for row in rows], dtype=np.float64)
    positions = values[:, 1:4]
    quaternion_xyzw = values[:, 4:8]
    quaternion_wxyz = np.roll(quaternion_xyzw, 1, axis=1)
    return trajectory.PoseTrajectory3D(
        positions_xyz=positions,
        orientations_quat_wxyz=quaternion_wxyz,
        timestamps=values[:, 0],
    )


def emulate_evo_association(
    gt_rows: Sequence[TumRow], estimate_rows: Sequence[TumRow]
) -> List[Association]:
    """Return evo v1.31.1 association indices while retaining GT identities."""

    gt_stamps = np.asarray([row.timestamp for row in gt_rows], dtype=np.float64)
    estimate_stamps = np.asarray(
        [row.timestamp for row in estimate_rows], dtype=np.float64
    )
    estimate_is_longer = len(estimate_stamps) > len(gt_stamps)
    if estimate_is_longer:
        short_ids, long_ids = sync.matching_time_indices(
            gt_stamps,
            estimate_stamps,
            max_diff=ASSOCIATION_MAX_SECONDS,
            offset_2=ASSOCIATION_OFFSET_SECONDS,
        )
        pairs = [
            Association(gt_index=gt_id, estimate_index=estimate_id,
                        difference_seconds=abs(
                            gt_stamps[gt_id] - estimate_stamps[estimate_id]
                        ))
            for gt_id, estimate_id in zip(short_ids, long_ids)
        ]
    else:
        short_ids, long_ids = sync.matching_time_indices(
            estimate_stamps,
            gt_stamps,
            max_diff=ASSOCIATION_MAX_SECONDS,
            offset_2=-ASSOCIATION_OFFSET_SECONDS,
        )
        pairs = [
            Association(gt_index=gt_id, estimate_index=estimate_id,
                        difference_seconds=abs(
                            gt_stamps[gt_id] - estimate_stamps[estimate_id]
                        ))
            for estimate_id, gt_id in zip(short_ids, long_ids)
        ]
    if not pairs:
        raise PairEvaluationError("evo association produced no timestamp matches")
    if any(pair.difference_seconds > ASSOCIATION_MAX_SECONDS for pair in pairs):
        raise PairEvaluationError("association exceeded the frozen 10 ms boundary")
    gt_ids = [pair.gt_index for pair in pairs]
    estimate_ids = [pair.estimate_index for pair in pairs]
    if len(set(gt_ids)) != len(gt_ids):
        raise PairEvaluationError(
            "association maps multiple estimate rows to one GT row; exact GT identity is ambiguous"
        )
    if len(set(estimate_ids)) != len(estimate_ids):
        raise PairEvaluationError(
            "association maps multiple GT rows to one estimate row; exact estimate identity is ambiguous"
        )

    # Cross-check the public evo association path, including its shorter-input
    # branch, rather than trusting only the retained-index reconstruction.
    gt_evo, estimate_evo = sync.associate_trajectories(
        rows_to_evo(gt_rows),
        rows_to_evo(estimate_rows),
        max_diff=ASSOCIATION_MAX_SECONDS,
        offset_2=ASSOCIATION_OFFSET_SECONDS,
        first_name="ground truth",
        snd_name="estimate",
    )
    expected_gt_stamps = np.asarray(
        [gt_rows[pair.gt_index].timestamp for pair in pairs], dtype=np.float64
    )
    expected_estimate_stamps = np.asarray(
        [estimate_rows[pair.estimate_index].timestamp for pair in pairs],
        dtype=np.float64,
    )
    if not (
        np.array_equal(gt_evo.timestamps, expected_gt_stamps)
        and np.array_equal(estimate_evo.timestamps, expected_estimate_stamps)
    ):
        raise PairEvaluationError("retained association indices differ from evo")
    return sorted(pairs, key=lambda pair: pair.gt_index)


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PairEvaluationError(f"could not read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PairEvaluationError(f"{label} is not a JSON object: {path}")
    return value


def manifest_identity_records(value: Any, prefix: str = "inputs") -> List[Tuple[str, Dict[str, Any]]]:
    records: List[Tuple[str, Dict[str, Any]]] = []
    if isinstance(value, dict):
        if (
            {"path", "size_bytes", "sha256"}.issubset(value)
            or {"canonical_path", "size_bytes", "sha256"}.issubset(value)
        ):
            records.append((prefix, dict(value)))
        for key in sorted(value):
            records.extend(manifest_identity_records(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            records.extend(manifest_identity_records(item, f"{prefix}[{index}]"))
    return records


def verify_recorded_identity(
    label: str,
    record: Mapping[str, Any],
    cache: Dict[Path, Dict[str, Any]],
) -> Dict[str, Any]:
    path_value = record.get("canonical_path", record.get("path"))
    if not isinstance(path_value, str):
        raise PairEvaluationError(f"{label} identity path is missing")
    if not isinstance(record.get("sha256"), str) or len(record["sha256"]) != 64:
        raise PairEvaluationError(f"{label} identity SHA-256 is malformed")
    if not isinstance(record.get("size_bytes"), int) or record["size_bytes"] < 0:
        raise PairEvaluationError(f"{label} identity size is malformed")
    path = Path(path_value).resolve(strict=True)
    if "canonical_path" in record and str(path) != record["canonical_path"]:
        raise PairEvaluationError(f"{label} canonical path differs from live resolution")
    if path not in cache:
        cache[path] = strict_file_identity(path)
    observed = cache[path]
    if observed["size_bytes"] != record["size_bytes"]:
        raise PairEvaluationError(f"{label} live size differs from manifest")
    if observed["sha256"] != record["sha256"]:
        raise PairEvaluationError(f"{label} live SHA-256 differs from manifest")
    return observed


def revalidate_runtime_identity(system: str) -> Dict[str, Any]:
    """Re-run the same pinned runtime validator used immediately before a run."""

    spec = importlib.util.spec_from_file_location(
        "_g05_kaist_runtime_identity_for_pair_eval", RUNTIME_IDENTITY_TOOL
    )
    if spec is None or spec.loader is None:
        raise PairEvaluationError("could not load the pinned runtime identity helper")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        value = module.validate_runtime_identity(system)
    except Exception as exc:
        raise PairEvaluationError(
            f"live {system} runtime identity revalidation failed: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise PairEvaluationError(f"live {system} runtime identity is not an object")
    return value


def validate_protocol(protocol_path: Path, protocol_id: str) -> Dict[str, Any]:
    value = yaml.safe_load(protocol_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PairEvaluationError("frozen protocol is not a mapping")
    if value.get("schema") != PROTOCOL_SCHEMA:
        raise PairEvaluationError("frozen protocol schema mismatch")
    if value.get("protocol_id") != protocol_id or protocol_id != EXPECTED_PROTOCOL_ID:
        raise PairEvaluationError("frozen protocol ID mismatch")
    if value.get("status") != "FROZEN_BEFORE_FIRST_U0_LAUNCH":
        raise PairEvaluationError("protocol is not in its frozen state")
    evaluation = value.get("evaluation")
    if not isinstance(evaluation, dict):
        raise PairEvaluationError("protocol lacks evaluation contract")
    expected_top_level = {
        "implementation": "evo",
        "evo_version": EXPECTED_EVO_VERSION,
        "execution_stage": "post_pair_after_both_estimators_close",
        "trajectory_format": "tum",
        "alignment": "se3_without_scale",
        "timestamp_association_max_seconds": ASSOCIATION_MAX_SECONDS,
        "per_run_full_overlap_metrics_are_primary": False,
        "minimum_common_pose_count": MINIMUM_COMMON_POSES,
        "minimum_common_rpe_pair_count": MINIMUM_RPE_PAIRS,
        "rpe_relative_delta_tolerance": RPE_RELATIVE_DELTA_TOLERANCE,
    }
    for key, expected in expected_top_level.items():
        if evaluation.get(key) != expected:
            raise PairEvaluationError(
                f"protocol evaluation field drift: {key}={evaluation.get(key)!r}"
            )
    expected_metrics = {
        "ate_translation_rmse_m": {
            "evo_tool": "evo_ape",
            "pose_relation": "trans_part",
        },
        "rpe_translation_rmse_1m_m": {
            "evo_tool": "evo_rpe",
            "pose_relation": "trans_part",
            "delta": RPE_DELTA_METERS,
            "delta_unit": "m",
            "all_pairs": True,
            "pairs_from_reference": True,
        },
        "rpe_rotation_rmse_1m_deg": {
            "evo_tool": "evo_rpe",
            "pose_relation": "angle_deg",
            "delta": RPE_DELTA_METERS,
            "delta_unit": "m",
            "all_pairs": True,
            "pairs_from_reference": True,
        },
    }
    observed_metrics = evaluation.get("primary_metrics")
    if not isinstance(observed_metrics, list):
        raise PairEvaluationError("protocol primary_metrics is not a list")
    observed_by_id: Dict[str, Dict[str, Any]] = {}
    for metric in observed_metrics:
        if not isinstance(metric, dict) or not isinstance(metric.get("id"), str):
            raise PairEvaluationError("protocol contains malformed primary metric")
        metric_copy = dict(metric)
        metric_id = metric_copy.pop("id")
        if metric_id in observed_by_id:
            raise PairEvaluationError(f"duplicate protocol metric: {metric_id}")
        observed_by_id[metric_id] = metric_copy
    if observed_by_id != expected_metrics:
        raise PairEvaluationError("protocol primary metric contract drift")
    population = str(evaluation.get("ground_truth_population", ""))
    if "intersection" not in population or "identical RPE pair set" not in population:
        raise PairEvaluationError("protocol common-population wording drift")
    attempt_status = value.get("attempt_status")
    if not isinstance(attempt_status, dict):
        raise PairEvaluationError("protocol lacks attempt-status thresholds")
    expected_attempt_thresholds = {
        "tail_gap_max_seconds": 0.10,
        "negative_tail_tolerance_seconds": 0.01,
        "post_initialization_max_state_gap_seconds": 0.20,
        "initialization_delay_max_fraction_of_selected_input": 0.10,
    }
    for key, expected in expected_attempt_thresholds.items():
        if attempt_status.get(key) != expected:
            raise PairEvaluationError(f"protocol attempt threshold drift: {key}")
    return value


def validate_runtime_evidence(
    inputs: Mapping[str, Any], expected_system: str
) -> Dict[str, Any]:
    system_inputs = inputs.get(expected_system)
    if not isinstance(system_inputs, dict):
        raise PairEvaluationError(f"{expected_system} system input record is absent")
    evidence = system_inputs.get("runtime_identity")
    if not isinstance(evidence, dict):
        raise PairEvaluationError(
            f"{expected_system} embedded runtime identity evidence is absent"
        )
    if (
        evidence.get("schema") != RUNTIME_IDENTITY_SCHEMA
        or evidence.get("status") != "PASS"
        or evidence.get("system") != expected_system
    ):
        raise PairEvaluationError(
            f"{expected_system} embedded runtime identity schema/status/system mismatch"
        )
    if not isinstance(evidence.get("policy_id"), str) or not evidence["policy_id"]:
        raise PairEvaluationError(f"{expected_system} runtime policy ID is absent")
    policy_sha = evidence.get("policy_sha256")
    if not isinstance(policy_sha, str) or len(policy_sha) != 64:
        raise PairEvaluationError(f"{expected_system} runtime policy SHA is malformed")
    other_system = "S1" if expected_system == "U0" else "U0"
    other_inputs = inputs.get(other_system)
    if not isinstance(other_inputs, dict):
        raise PairEvaluationError(f"{other_system} base system input record is absent")
    if "runtime_identity" in other_inputs:
        raise PairEvaluationError(
            f"{expected_system} run unexpectedly embeds {other_system} runtime evidence"
        )
    return evidence


def validate_run_eligibility(manifest: Mapping[str, Any], system: str) -> None:
    checks = manifest.get("checks")
    if not isinstance(checks, dict):
        raise PairEvaluationError(f"{system} run lacks eligibility checks")
    required_true = (
        "trajectory_ready_for_paired_evaluation",
        "numeric_estimator_outputs_valid",
        "static_pairing_census_present",
        "pairing_census_present",
    )
    failed = [name for name in required_true if checks.get(name) is not True]
    if failed:
        raise PairEvaluationError(
            f"{system} run lacks required paired-evaluation checks: {failed}"
        )
    completion = manifest.get("completion")
    if not isinstance(completion, dict):
        raise PairEvaluationError(f"{system} run lacks completion evidence")
    for field in (
        "tail_gap_pass",
        "initialization_delay_pass",
        "maximum_state_gap_pass",
    ):
        if completion.get(field) is not True:
            raise PairEvaluationError(f"{system} completion check failed: {field}")
    numeric_fields = (
        "tail_gap_seconds",
        "initialization_delay_seconds",
        "initialization_delay_fraction_of_selected_input",
        "maximum_initialization_delay_fraction",
        "maximum_initialization_delay_seconds",
        "selected_input_span_seconds",
        "maximum_post_initialization_state_gap_seconds",
    )
    if any(
        not isinstance(completion.get(field), (int, float))
        or isinstance(completion.get(field), bool)
        or not math.isfinite(float(completion[field]))
        for field in numeric_fields
    ):
        raise PairEvaluationError(f"{system} completion evidence is nonnumeric")
    if not -0.01 <= float(completion["tail_gap_seconds"]) <= 0.10:
        raise PairEvaluationError(f"{system} tail gap violates frozen bounds")
    selected_span = float(completion["selected_input_span_seconds"])
    initialization_delay = float(completion["initialization_delay_seconds"])
    if selected_span <= 0.0:
        raise PairEvaluationError(f"{system} selected input span is not positive")
    if not -0.01 <= initialization_delay <= 0.10 * selected_span:
        raise PairEvaluationError(
            f"{system} initialization delay violates frozen fractional bound"
        )
    if float(completion["maximum_initialization_delay_fraction"]) != 0.10:
        raise PairEvaluationError(
            f"{system} maximum initialization-delay fraction drift"
        )
    if not math.isclose(
        float(completion["initialization_delay_fraction_of_selected_input"]),
        initialization_delay / selected_span,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise PairEvaluationError(
            f"{system} initialization-delay fraction accounting does not close"
        )
    if not math.isclose(
        float(completion["maximum_initialization_delay_seconds"]),
        0.10 * selected_span,
        rel_tol=1e-12,
        abs_tol=1e-12,
    ):
        raise PairEvaluationError(
            f"{system} initialization-delay maximum accounting does not close"
        )
    if float(completion["maximum_post_initialization_state_gap_seconds"]) > 0.20:
        raise PairEvaluationError(f"{system} maximum state gap violates frozen bound")
    pairing = manifest.get("pairing_census")
    if not isinstance(pairing, dict) or not isinstance(pairing.get("census"), dict):
        raise PairEvaluationError(f"{system} attempt pairing census is absent")
    availability = pairing.get("availability")
    if not isinstance(availability, dict):
        raise PairEvaluationError(f"{system} pairing availability is absent")
    if availability.get("static_input_census") != "COMPLETE":
        raise PairEvaluationError(f"{system} static pairing census is incomplete")
    if availability.get("estimator_output_timestamps") != "COMPLETE":
        raise PairEvaluationError(f"{system} pairing output bounds are incomplete")
    expected_runtime = "COMPLETE" if system == "S1" else "NOT_APPLICABLE"
    if availability.get("s1_runtime_counters") != expected_runtime:
        raise PairEvaluationError(f"{system} pairing runtime accounting is incomplete")


def validate_run(
    run_dir: Path,
    expected_system: str,
    ground_truth: Path,
    identity_cache: Dict[Path, Dict[str, Any]],
) -> Dict[str, Any]:
    resolved_dir = run_dir.resolve(strict=True)
    if not resolved_dir.is_dir():
        raise PairEvaluationError(f"{expected_system} run is not a directory")
    manifest_path = resolved_dir / "sequence_result.json"
    manifest = load_json(manifest_path, f"{expected_system} run manifest")
    if manifest.get("schema") != RUN_SCHEMA:
        raise PairEvaluationError(f"{expected_system} run schema mismatch")
    if manifest.get("protocol_id") != EXPECTED_PROTOCOL_ID:
        raise PairEvaluationError(f"{expected_system} protocol ID mismatch")
    if manifest.get("system") != expected_system:
        raise PairEvaluationError(f"run is not system {expected_system}")
    if manifest.get("attempt_kind") != "SCORED":
        raise PairEvaluationError(f"{expected_system} run is not SCORED")
    if manifest.get("status") not in ELIGIBLE_RUN_STATUSES:
        raise PairEvaluationError(f"{expected_system} run is not complete")
    if not isinstance(manifest.get("sequence"), str) or not manifest["sequence"]:
        raise PairEvaluationError(f"{expected_system} sequence is missing")
    if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
        raise PairEvaluationError(f"{expected_system} run_id is missing")
    validate_run_eligibility(manifest, expected_system)
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise PairEvaluationError(f"{expected_system} manifest lacks inputs")
    required_shared_inputs = (
        "protocol",
        "matrix",
        "data_inventory",
        "selected_bag",
        "ground_truth",
        "converter",
        "pairing_census_tool",
        "geometry_bundle_tool",
        "runtime_identity_tool",
        "pair_evaluator_tool",
        "U0",
        "S1",
    )
    missing_shared = [name for name in required_shared_inputs if name not in inputs]
    if missing_shared:
        raise PairEvaluationError(
            f"{expected_system} manifest lacks shared input records: {missing_shared}"
        )
    required_system_files = {
        "U0": (
            "binary", "config", "imu_config", "camera_imu_config", "launch", "setup"
        ),
        "S1": (
            "binary", "config", "imu_config", "camera_imu_config", "launch", "setup",
            "build_provenance",
        ),
    }
    for arm, names in required_system_files.items():
        arm_value = inputs.get(arm)
        if not isinstance(arm_value, dict):
            raise PairEvaluationError(f"{expected_system} manifest lacks {arm} base inputs")
        missing = [name for name in names if name not in arm_value]
        if missing:
            raise PairEvaluationError(
                f"{expected_system} manifest lacks {arm} base identities: {missing}"
            )
    u0_base = inputs["U0"]
    s1_base = inputs["S1"]
    if (
        not isinstance(u0_base.get("source_commit"), str)
        or not isinstance(u0_base.get("source_tree"), str)
        or u0_base.get("source_clean") is not True
    ):
        raise PairEvaluationError(f"{expected_system} U0 source identity is incomplete")
    if (
        not isinstance(s1_base.get("estimator_source_commit"), str)
        or not isinstance(s1_base.get("estimator_source_tree"), str)
    ):
        raise PairEvaluationError(f"{expected_system} S1 source identity is incomplete")
    runtime_evidence = validate_runtime_evidence(inputs, expected_system)
    records = manifest_identity_records(inputs)
    if not records:
        raise PairEvaluationError(f"{expected_system} inputs contain no identities")
    live_records: Dict[str, Any] = {}
    for label, record in records:
        live_records[label] = verify_recorded_identity(
            f"{expected_system}.{label}", record, identity_cache
        )
    for required in ("protocol", "selected_bag", "ground_truth"):
        record = inputs.get(required)
        if not isinstance(record, dict) or not {
            "path", "size_bytes", "sha256"
        }.issubset(record):
            raise PairEvaluationError(
                f"{expected_system} manifest lacks input identity: {required}"
            )
    recorded_gt = inputs["ground_truth"]
    if Path(recorded_gt["path"]).resolve(strict=True) != ground_truth:
        raise PairEvaluationError(f"{expected_system} ground-truth path mismatch")
    live_gt = identity_cache[ground_truth]
    if (
        recorded_gt["sha256"] != live_gt["sha256"]
        or recorded_gt["size_bytes"] != live_gt["size_bytes"]
    ):
        raise PairEvaluationError(f"{expected_system} ground-truth hash mismatch")

    outputs = manifest.get("outputs")
    trajectory_record = outputs.get("trajectory") if isinstance(outputs, dict) else None
    if not isinstance(trajectory_record, dict):
        raise PairEvaluationError(f"{expected_system} manifest lacks trajectory output")
    expected_trajectory = (resolved_dir / "trajectory" / "estimate_raw.tum").resolve(
        strict=True
    )
    if Path(str(trajectory_record.get("path", ""))).resolve(strict=True) != expected_trajectory:
        raise PairEvaluationError(
            f"{expected_system} manifest trajectory path is outside its run directory"
        )
    live_trajectory = verify_recorded_identity(
        f"{expected_system}.outputs.trajectory", trajectory_record, identity_cache
    )
    fixed_numeric_outputs: Dict[str, Tuple[str, Path]] = {
        "state": ("state_estimate.txt", resolved_dir / "trajectory" / "state_estimate.txt"),
        "deviation": (
            "state_deviation.txt",
            resolved_dir / "trajectory" / "state_deviation.txt",
        ),
    }
    live_numeric_outputs: Dict[str, Any] = {}
    for output_name, (_, expected_path_value) in fixed_numeric_outputs.items():
        record = outputs.get(output_name) if isinstance(outputs, dict) else None
        if not isinstance(record, dict):
            raise PairEvaluationError(
                f"{expected_system} manifest lacks {output_name} output"
            )
        expected_path = expected_path_value.resolve(strict=True)
        recorded_path = Path(str(record.get("path", ""))).resolve(strict=True)
        if recorded_path != expected_path:
            raise PairEvaluationError(
                f"{expected_system} manifest {output_name} path is outside its run directory"
            )
        live_numeric_outputs[output_name] = verify_recorded_identity(
            f"{expected_system}.outputs.{output_name}", record, identity_cache
        )
    return {
        "directory": str(resolved_dir),
        "manifest": manifest,
        "manifest_identity": strict_file_identity(manifest_path),
        "trajectory_path": expected_trajectory,
        "trajectory_identity": live_trajectory,
        "numeric_output_identities": live_numeric_outputs,
        "input_identity_record_count": len(records),
        "live_input_identities": live_records,
        "runtime_identity": runtime_evidence,
    }


def verify_run_pair(u0: Mapping[str, Any], s1: Mapping[str, Any]) -> None:
    u0_manifest = u0["manifest"]
    s1_manifest = s1["manifest"]
    equality_fields = ("protocol_id", "sequence", "attempt_kind", "dry_run")
    for field in equality_fields:
        if u0_manifest.get(field) != s1_manifest.get(field):
            raise PairEvaluationError(f"paired run manifest mismatch: {field}")
    if u0_manifest["run_id"] == s1_manifest["run_id"]:
        raise PairEvaluationError("paired run IDs are not distinct")
    shared_inputs: Dict[str, Dict[str, Any]] = {}
    for system, manifest in (("U0", u0_manifest), ("S1", s1_manifest)):
        value = copy.deepcopy(manifest["inputs"])
        for arm in SYSTEMS:
            arm_value = value.get(arm)
            if isinstance(arm_value, dict):
                arm_value.pop("runtime_identity", None)
        shared_inputs[system] = value
    if shared_inputs["U0"] != shared_inputs["S1"]:
        raise PairEvaluationError(
            "paired runs do not have byte-identical shared input identity trees"
        )


def module_identity(module: Any) -> Dict[str, Any]:
    path = getattr(module, "__file__", None)
    if not isinstance(path, str):
        raise PairEvaluationError(f"module lacks a file identity: {module!r}")
    return strict_file_identity(Path(path))


def runtime_identity() -> Dict[str, Any]:
    observed_evo = str(evo.__version__).lstrip("v")
    if observed_evo != EXPECTED_EVO_VERSION:
        raise PairEvaluationError(
            f"evo version drift: {observed_evo} != {EXPECTED_EVO_VERSION}"
        )
    executables: Dict[str, Any] = {}
    for name in ("evo_ape", "evo_rpe"):
        executable = shutil.which(name)
        if executable is None:
            raise PairEvaluationError(f"missing evo executable identity: {name}")
        executables[name] = strict_file_identity(Path(executable))
    evo_modules = {
        "package": module_identity(evo),
        "filters": module_identity(filters),
        "geometry": module_identity(geometry),
        "lie_algebra": module_identity(lie_algebra),
        "metrics": module_identity(metrics),
        "result": module_identity(evo_result),
        "sync": module_identity(sync),
        "trajectory": module_identity(trajectory),
        "transformations": module_identity(transformations),
        "units": module_identity(units),
        "file_interface": module_identity(file_interface),
    }
    numpy_modules = {
        "package": module_identity(np),
        "core_numeric": module_identity(numpy_core_numeric),
        "linalg_package": module_identity(np.linalg),
        "linalg_implementation": module_identity(numpy_linalg_implementation),
        "multiarray_umath": strict_file_identity(
            Path(np.core._multiarray_umath.__file__)
        ),
        "umath_linalg": module_identity(numpy_umath_linalg),
    }
    return {
        "evaluator": strict_file_identity(Path(__file__)),
        "python": {
            "version": sys.version,
            "version_info": ".".join(str(value) for value in sys.version_info[:3]),
            "implementation": sys.implementation.name,
            "cache_tag": sys.implementation.cache_tag,
            "executable": strict_file_identity(Path(sys.executable)),
        },
        "evo": {
            "version": observed_evo,
            "required_version": EXPECTED_EVO_VERSION,
            "executables": executables,
            "semantic_modules": evo_modules,
        },
        "numpy": {
            "version": np.__version__,
            "semantic_modules": numpy_modules,
        },
    }


def runtime_pin_projection(runtime: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the exact non-circular runtime material pinned by the protocol."""

    return {
        "schema": EVALUATOR_RUNTIME_PINS_SCHEMA,
        "python": copy.deepcopy(runtime["python"]),
        "evo": copy.deepcopy(runtime["evo"]),
        "numpy": copy.deepcopy(runtime["numpy"]),
    }


def validate_protocol_runtime_pins(
    protocol: Mapping[str, Any], runtime: Mapping[str, Any]
) -> Dict[str, Any]:
    evaluation = protocol.get("evaluation")
    pins = evaluation.get("evaluator_runtime_pins") if isinstance(evaluation, dict) else None
    if not isinstance(pins, dict):
        raise PairEvaluationError(
            "protocol lacks evaluation.evaluator_runtime_pins"
        )
    observed = runtime_pin_projection(runtime)
    if pins != observed:
        raise PairEvaluationError(
            "evaluator runtime differs from protocol pins: "
            f"observed={canonical_digest(observed)}, "
            f"expected={canonical_digest(pins)}"
        )
    return {
        "schema": EVALUATOR_RUNTIME_PINS_SCHEMA,
        "exact_match": True,
        "pins_sha256": canonical_digest(pins),
    }


def statistics(metric: metrics.PE) -> Dict[str, float]:
    result = {
        key: float(value) for key, value in metric.get_all_statistics().items()
    }
    if not result or not all(math.isfinite(value) for value in result.values()):
        raise PairEvaluationError("evo returned nonfinite metric statistics")
    return result


def validate_alignment(rotation: np.ndarray, translation: np.ndarray, scale: float) -> None:
    if rotation.shape != (3, 3) or translation.shape != (3,):
        raise PairEvaluationError("evo returned malformed alignment parameters")
    if not (
        np.all(np.isfinite(rotation))
        and np.all(np.isfinite(translation))
        and math.isfinite(float(scale))
    ):
        raise PairEvaluationError("evo returned nonfinite alignment parameters")
    if float(scale) != 1.0:
        raise PairEvaluationError("SE(3) alignment changed scale")
    if not np.allclose(rotation.T @ rotation, np.eye(3), rtol=1e-10, atol=1e-10):
        raise PairEvaluationError("alignment rotation is not orthonormal")
    if not math.isclose(float(np.linalg.det(rotation)), 1.0, rel_tol=1e-10, abs_tol=1e-10):
        raise PairEvaluationError("alignment rotation determinant is not one")


def explicit_rpe_metric(
    reference: trajectory.PoseTrajectory3D,
    estimate: trajectory.PoseTrajectory3D,
    reference_pairs: Sequence[Tuple[int, int]],
    pose_relation: metrics.PoseRelation,
) -> metrics.RPE:
    """Evaluate RPE with one already-selected list of full reference tuples.

    evo's :class:`RPE` stores only each pair's end index after evaluation.  We
    therefore select the full ``(start, end)`` tuples exactly once outside the
    per-method loop and feed those tuples directly to evo's ``rpe_base``.  No
    method-local pair selection or endpoint-only equivalence is accepted.
    """

    if reference.num_poses != estimate.num_poses:
        raise PairEvaluationError("explicit RPE trajectories have unequal lengths")
    normalized: List[Tuple[int, int]] = []
    for ordinal, pair in enumerate(reference_pairs):
        if (
            not isinstance(pair, (tuple, list))
            or len(pair) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, np.integer)) for value in pair)
        ):
            raise PairEvaluationError(f"malformed explicit RPE pair {ordinal}")
        start, end = int(pair[0]), int(pair[1])
        if not (0 <= start < end < reference.num_poses):
            raise PairEvaluationError(f"out-of-range explicit RPE pair {ordinal}")
        normalized.append((start, end))
    if not normalized or len(set(normalized)) != len(normalized):
        raise PairEvaluationError("explicit RPE pair list is empty or contains duplicates")

    metric = metrics.RPE(
        pose_relation=pose_relation,
        delta=RPE_DELTA_METERS,
        delta_unit=metrics.Unit.meters,
        rel_delta_tol=RPE_RELATIVE_DELTA_TOLERANCE,
        all_pairs=RPE_ALL_PAIRS,
        pairs_from_reference=RPE_PAIRS_FROM_REFERENCE,
    )
    metric.E = [
        metrics.RPE.rpe_base(
            reference.poses_se3[start],
            reference.poses_se3[end],
            estimate.poses_se3[start],
            estimate.poses_se3[end],
        )
        for start, end in normalized
    ]
    metric.delta_ids = [end for _, end in normalized]
    if pose_relation == metrics.PoseRelation.translation_part:
        metric.error = np.asarray(
            [np.linalg.norm(error[:3, 3]) for error in metric.E],
            dtype=np.float64,
        )
    elif pose_relation == metrics.PoseRelation.rotation_angle_deg:
        metric.error = np.asarray(
            [
                abs(lie_algebra.so3_log_angle(error[:3, :3], True))
                for error in metric.E
            ],
            dtype=np.float64,
        )
    else:
        raise PairEvaluationError(
            f"unsupported explicit RPE pose relation: {pose_relation}"
        )
    return metric


def evaluate_method(
    system: str,
    reference: trajectory.PoseTrajectory3D,
    estimate: trajectory.PoseTrajectory3D,
    reference_pairs: Sequence[Tuple[int, int]],
) -> Dict[str, Any]:
    if reference.num_poses != estimate.num_poses:
        raise PairEvaluationError(f"{system} common trajectories have unequal lengths")
    aligned = copy.deepcopy(estimate)
    try:
        rotation, translation, scale = aligned.align(
            reference,
            correct_scale=False,
            correct_only_scale=False,
            n=ALIGNMENT_POSE_COUNT,
        )
    except Exception as exc:
        raise PairEvaluationError(f"{system} SE(3) alignment failed: {exc}") from exc
    validate_alignment(rotation, translation, float(scale))

    ape = metrics.APE(metrics.PoseRelation.translation_part)
    try:
        ape.process_data((reference, aligned))
        rpe_translation = explicit_rpe_metric(
            reference,
            aligned,
            reference_pairs,
            metrics.PoseRelation.translation_part,
        )
        rpe_rotation = explicit_rpe_metric(
            reference,
            aligned,
            reference_pairs,
            metrics.PoseRelation.rotation_angle_deg,
        )
    except Exception as exc:
        raise PairEvaluationError(f"{system} evo metric evaluation failed: {exc}") from exc

    exact_pair_tuples = [(int(start), int(end)) for start, end in reference_pairs]
    expected_delta_ids = [end for _, end in exact_pair_tuples]
    if rpe_translation.delta_ids != expected_delta_ids or rpe_rotation.delta_ids != expected_delta_ids:
        raise PairEvaluationError(
            f"{system} evo RPE pair IDs differ from the frozen reference pair list"
        )
    arrays = (ape.error, rpe_translation.error, rpe_rotation.error)
    if any(array.size == 0 or not np.all(np.isfinite(array)) for array in arrays):
        raise PairEvaluationError(f"{system} evo returned empty/nonfinite errors")
    ape_stats = statistics(ape)
    rpe_translation_stats = statistics(rpe_translation)
    rpe_rotation_stats = statistics(rpe_rotation)
    primary = {
        "ate_translation_rmse_m": ape_stats["rmse"],
        "rpe_translation_rmse_1m_m": rpe_translation_stats["rmse"],
        "rpe_rotation_rmse_1m_deg": rpe_rotation_stats["rmse"],
    }
    if any(not math.isfinite(value) or value < 0.0 for value in primary.values()):
        raise PairEvaluationError(
            f"{system} primary RMSE is negative or nonfinite"
        )
    return {
        "system": system,
        "alignment": {
            "implementation": "evo.core.trajectory.PosePath3D.align",
            "method": "Umeyama",
            "group": "SE(3)",
            "correct_scale": False,
            "correct_only_scale": False,
            "n_to_align": ALIGNMENT_POSE_COUNT,
            "fit_population_count": reference.num_poses,
            "rotation_matrix": np.asarray(rotation).tolist(),
            "translation_m": np.asarray(translation).tolist(),
            "scale": float(scale),
        },
        "primary": primary,
        "statistics": {
            "ate_translation_m": ape_stats,
            "rpe_translation_1m_m": rpe_translation_stats,
            "rpe_rotation_1m_deg": rpe_rotation_stats,
        },
        "errors": {
            "ate_translation_m": [float(value) for value in ape.error],
            "rpe_translation_1m_m": [float(value) for value in rpe_translation.error],
            "rpe_rotation_1m_deg": [float(value) for value in rpe_rotation.error],
        },
        "rpe_pair_tuples": [list(pair) for pair in exact_pair_tuples],
        "rpe_pair_tuples_sha256": canonical_digest(exact_pair_tuples),
    }


def association_csv_rows(
    associations: Sequence[Association],
    gt_rows: Sequence[TumRow],
    estimate_rows: Sequence[TumRow],
) -> Iterable[Sequence[Any]]:
    for ordinal, pair in enumerate(associations):
        gt = gt_rows[pair.gt_index]
        estimate = estimate_rows[pair.estimate_index]
        yield (
            ordinal,
            gt.data_index,
            gt.source_line_number,
            gt.row_id,
            gt.timestamp_text,
            format(gt.timestamp, ".17g"),
            struct.pack(">d", gt.timestamp).hex(),
            estimate.data_index,
            estimate.source_line_number,
            estimate.row_id,
            estimate.timestamp_text,
            format(estimate.timestamp, ".17g"),
            struct.pack(">d", estimate.timestamp).hex(),
            format(pair.difference_seconds, ".17g"),
            struct.pack(">d", pair.difference_seconds).hex(),
        )


ASSOCIATION_HEADER = (
    "association_ordinal",
    "gt_data_index",
    "gt_source_line_number",
    "gt_row_id_sha256",
    "gt_timestamp_source_text",
    "gt_timestamp_binary64_decimal_17g",
    "gt_timestamp_binary64_hex",
    "estimate_data_index",
    "estimate_source_line_number",
    "estimate_row_id_sha256",
    "estimate_timestamp_source_text",
    "estimate_timestamp_binary64_decimal_17g",
    "estimate_timestamp_binary64_hex",
    "absolute_difference_seconds_17g",
    "absolute_difference_binary64_hex",
)


def make_artifact_index(root: Path, paths: Sequence[Path]) -> Dict[str, Any]:
    return {
        path.relative_to(root).as_posix(): relative_identity(root, path)
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    }


def relative_difference_record(baseline_u0: float, candidate_s1: float) -> Dict[str, Any]:
    """Represent ``(S1-U0)/U0`` without NaN, infinity, or division by zero."""

    if (
        not math.isfinite(baseline_u0)
        or not math.isfinite(candidate_s1)
        or baseline_u0 < 0.0
        or candidate_s1 < 0.0
    ):
        raise PairEvaluationError("relative-difference inputs must be finite RMSE values")
    common = {
        "baseline_u0": baseline_u0,
        "candidate_s1": candidate_s1,
        "formula": "(S1-U0)/U0",
    }
    if baseline_u0 > 0.0:
        return {
            **common,
            "status": "DEFINED_FINITE",
            "mathematical_ratio_defined": True,
            "value": (candidate_s1 - baseline_u0) / baseline_u0,
        }
    if candidate_s1 == 0.0:
        return {
            **common,
            "status": "BOTH_ZERO_PARITY_CONVENTION",
            "mathematical_ratio_defined": False,
            "value": 0.0,
            "convention": "both exact-zero RMSE values are treated as descriptive parity",
        }
    return {
        **common,
        "status": "UNDEFINED_ZERO_U0_POSITIVE_S1",
        "mathematical_ratio_defined": False,
        "value": None,
        "extended_real_direction": "POSITIVE_INFINITY",
    }


def evaluate_pair(u0_run: Path, s1_run: Path, ground_truth_path: Path, output: Path) -> Dict[str, Any]:
    ground_truth = ground_truth_path.resolve(strict=True)
    if not ground_truth.is_file():
        raise PairEvaluationError("ground truth is not a regular file")
    final_output = output.resolve(strict=False)
    if final_output.exists():
        raise PairEvaluationError(f"refusing to overwrite pair output: {final_output}")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    staging = final_output.parent / f".{final_output.name}.tmp-{uuid.uuid4().hex}"
    if staging.exists():
        raise PairEvaluationError(f"staging path unexpectedly exists: {staging}")
    staging.mkdir(mode=0o755)
    install_attempted = False
    installed = False
    try:
        for relative in ("associations", "common", "metrics"):
            (staging / relative).mkdir()

        identity_cache: Dict[Path, Dict[str, Any]] = {
            ground_truth: strict_file_identity(ground_truth)
        }
        u0 = validate_run(u0_run, "U0", ground_truth, identity_cache)
        s1 = validate_run(s1_run, "S1", ground_truth, identity_cache)
        verify_run_pair(u0, s1)
        protocol_identity = u0["manifest"]["inputs"]["protocol"]
        protocol_path = Path(protocol_identity["path"]).resolve(strict=True)
        protocol = validate_protocol(protocol_path, u0["manifest"]["protocol_id"])
        runtime = runtime_identity()
        evaluator_runtime_pin_check = validate_protocol_runtime_pins(
            protocol, runtime
        )
        live_estimator_runtime = {
            system: revalidate_runtime_identity(system) for system in SYSTEMS
        }
        for system, run in (("U0", u0), ("S1", s1)):
            if live_estimator_runtime[system] != run["runtime_identity"]:
                raise PairEvaluationError(
                    f"live {system} runtime identity differs from scored-run evidence"
                )

        gt_rows = read_tum_exact(ground_truth, "ground truth")
        estimate_rows = {
            "U0": read_tum_exact(u0["trajectory_path"], "U0 trajectory"),
            "S1": read_tum_exact(s1["trajectory_path"], "S1 trajectory"),
        }
        associations = {
            system: emulate_evo_association(gt_rows, estimate_rows[system])
            for system in SYSTEMS
        }
        association_maps = {
            system: {pair.gt_index: pair for pair in associations[system]}
            for system in SYSTEMS
        }
        common_gt_indices = sorted(
            set(association_maps["U0"]).intersection(association_maps["S1"])
        )
        if len(common_gt_indices) < MINIMUM_COMMON_POSES:
            raise PairEvaluationError(
                f"common GT population too small: {len(common_gt_indices)} "
                f"< {MINIMUM_COMMON_POSES}"
            )
        common_gt_rows = [gt_rows[index] for index in common_gt_indices]
        common_estimate_rows = {
            system: [
                estimate_rows[system][association_maps[system][index].estimate_index]
                for index in common_gt_indices
            ]
            for system in SYSTEMS
        }
        common_row_ids = [row.row_id for row in common_gt_rows]
        population_digest = canonical_digest(common_row_ids)
        if len(set(common_row_ids)) != len(common_row_ids):
            raise PairEvaluationError("common GT population contains duplicate row IDs")

        association_paths: Dict[str, Path] = {}
        for system in SYSTEMS:
            path = staging / "associations" / f"{system.lower()}_to_ground_truth.csv"
            write_csv(
                path,
                ASSOCIATION_HEADER,
                association_csv_rows(associations[system], gt_rows, estimate_rows[system]),
            )
            association_paths[system] = path

        common_csv = staging / "associations" / "common_population.csv"
        write_csv(
            common_csv,
            (
                "common_index",
                "gt_data_index",
                "gt_source_line_number",
                "gt_row_id_sha256",
                "gt_timestamp_source_text",
                "gt_timestamp_binary64_decimal_17g",
                "gt_timestamp_binary64_hex",
                "u0_estimate_data_index",
                "u0_estimate_row_id_sha256",
                "u0_estimate_timestamp_source_text",
                "s1_estimate_data_index",
                "s1_estimate_row_id_sha256",
                "s1_estimate_timestamp_source_text",
            ),
            (
                (
                    common_index,
                    gt.data_index,
                    gt.source_line_number,
                    gt.row_id,
                    gt.timestamp_text,
                    format(gt.timestamp, ".17g"),
                    struct.pack(">d", gt.timestamp).hex(),
                    common_estimate_rows["U0"][common_index].data_index,
                    common_estimate_rows["U0"][common_index].row_id,
                    common_estimate_rows["U0"][common_index].timestamp_text,
                    common_estimate_rows["S1"][common_index].data_index,
                    common_estimate_rows["S1"][common_index].row_id,
                    common_estimate_rows["S1"][common_index].timestamp_text,
                )
                for common_index, gt in enumerate(common_gt_rows)
            ),
        )

        common_paths = {
            "reference": staging / "common" / "ground_truth_common.tum",
            "U0": staging / "common" / "u0_common.tum",
            "S1": staging / "common" / "s1_common.tum",
        }
        write_tum(common_paths["reference"], common_gt_rows, common_gt_rows)
        for system in SYSTEMS:
            write_tum(common_paths[system], common_estimate_rows[system], common_gt_rows)

        common_trajectories = {
            name: file_interface.read_tum_trajectory_file(str(path))
            for name, path in common_paths.items()
        }
        reference = common_trajectories["reference"]
        if reference.num_poses != len(common_gt_rows):
            raise PairEvaluationError("evo changed the common reference row count")
        for system in SYSTEMS:
            observed = common_trajectories[system]
            if observed.num_poses != reference.num_poses:
                raise PairEvaluationError(f"{system} common TUM row count drift")
            if not np.array_equal(observed.timestamps, reference.timestamps):
                raise PairEvaluationError(
                    f"{system} common TUM timestamps are not bit-identical to GT"
                )

        try:
            reference_pairs = metrics.id_pairs_from_delta(
                reference.poses_se3,
                RPE_DELTA_METERS,
                metrics.Unit.meters,
                RPE_RELATIVE_DELTA_TOLERANCE,
                all_pairs=RPE_ALL_PAIRS,
            )
        except Exception as exc:
            raise PairEvaluationError(f"reference 1 m RPE pair selection failed: {exc}") from exc
        if len(reference_pairs) < MINIMUM_RPE_PAIRS:
            raise PairEvaluationError(
                f"reference RPE pair population too small: {len(reference_pairs)} "
                f"< {MINIMUM_RPE_PAIRS}"
            )
        pair_identity_rows = [
            {
                "pair_index": index,
                "start_common_index": start,
                "end_common_index": end,
                "start_gt_row_id": common_gt_rows[start].row_id,
                "end_gt_row_id": common_gt_rows[end].row_id,
            }
            for index, (start, end) in enumerate(reference_pairs)
        ]
        rpe_pair_digest = canonical_digest(pair_identity_rows)
        rpe_pairs_path = staging / "associations" / "rpe_pairs_1m.csv"
        distances = reference.distances
        write_csv(
            rpe_pairs_path,
            (
                "pair_index",
                "start_common_index",
                "end_common_index",
                "start_gt_row_id_sha256",
                "end_gt_row_id_sha256",
                "reference_path_delta_m_17g",
            ),
            (
                (
                    index,
                    start,
                    end,
                    common_gt_rows[start].row_id,
                    common_gt_rows[end].row_id,
                    format(float(distances[end] - distances[start]), ".17g"),
                )
                for index, (start, end) in enumerate(reference_pairs)
            ),
        )

        method_results = {
            system: evaluate_method(
                system,
                reference,
                common_trajectories[system],
                reference_pairs,
            )
            for system in SYSTEMS
        }
        exact_pair_tuples = [
            [int(start), int(end)] for start, end in reference_pairs
        ]
        tuple_digest = canonical_digest(reference_pairs)
        for system in SYSTEMS:
            if (
                method_results[system]["rpe_pair_tuples"] != exact_pair_tuples
                or method_results[system]["rpe_pair_tuples_sha256"] != tuple_digest
            ):
                raise PairEvaluationError(
                    f"{system} did not consume the exact full RPE pair tuples"
                )

        metric_paths: Dict[str, Path] = {}
        for system in SYSTEMS:
            lower = system.lower()
            result = method_results[system]
            ate_path = staging / "metrics" / f"{lower}_ate_translation_errors.csv"
            write_csv(
                ate_path,
                ("common_index", "gt_row_id_sha256", "error_m_17g"),
                (
                    (index, common_gt_rows[index].row_id, format(error, ".17g"))
                    for index, error in enumerate(result["errors"]["ate_translation_m"])
                ),
            )
            rpe_path = staging / "metrics" / f"{lower}_rpe_1m_errors.csv"
            write_csv(
                rpe_path,
                (
                    "pair_index",
                    "start_gt_row_id_sha256",
                    "end_gt_row_id_sha256",
                    "translation_error_m_17g",
                    "rotation_error_deg_17g",
                ),
                (
                    (
                        index,
                        common_gt_rows[start].row_id,
                        common_gt_rows[end].row_id,
                        format(result["errors"]["rpe_translation_1m_m"][index], ".17g"),
                        format(result["errors"]["rpe_rotation_1m_deg"][index], ".17g"),
                    )
                    for index, (start, end) in enumerate(reference_pairs)
                ),
            )
            metric_paths[f"{system}_ate"] = ate_path
            metric_paths[f"{system}_rpe"] = rpe_path

        association_artifacts = make_artifact_index(
            staging, [*association_paths.values(), common_csv]
        )
        common_artifacts = make_artifact_index(staging, list(common_paths.values()))
        rpe_pair_artifact = relative_identity(staging, rpe_pairs_path)
        association_manifest = {
            "schema": ASSOCIATION_SCHEMA,
            "status": "COMPLETE",
            "protocol_id": u0["manifest"]["protocol_id"],
            "sequence": u0["manifest"]["sequence"],
            "association": {
                "implementation": "evo.core.sync.matching_time_indices",
                "evo_version": EXPECTED_EVO_VERSION,
                "max_difference_seconds": ASSOCIATION_MAX_SECONDS,
                "offset_seconds": ASSOCIATION_OFFSET_SECONDS,
                "comparison": "absolute_difference_less_than_or_equal",
                "tie_break": "numpy_argmin_first_index",
                "shorter_trajectory_is_iterated": True,
                "duplicate_gt_or_estimate_assignment_policy": "FAIL",
            },
            "source_counts": {
                "ground_truth": len(gt_rows),
                "U0_estimate": len(estimate_rows["U0"]),
                "S1_estimate": len(estimate_rows["S1"]),
            },
            "association_counts": {
                "U0": len(associations["U0"]),
                "S1": len(associations["S1"]),
            },
            "association_gt_row_id_digests": {
                system: canonical_digest(
                    [gt_rows[pair.gt_index].row_id for pair in associations[system]]
                )
                for system in SYSTEMS
            },
            "common_population": {
                "operation": "exact_intersection_of_gt_row_identities",
                "count": len(common_gt_rows),
                "gt_data_index_first": common_gt_rows[0].data_index,
                "gt_data_index_last": common_gt_rows[-1].data_index,
                "gt_timestamp_first": float_record(common_gt_rows[0].timestamp),
                "gt_timestamp_last": float_record(common_gt_rows[-1].timestamp),
                "duration_seconds": float_record(
                    common_gt_rows[-1].timestamp - common_gt_rows[0].timestamp
                ),
                "gt_row_ids_sha256": population_digest,
                "u0_gt_row_ids_sha256": population_digest,
                "s1_gt_row_ids_sha256": population_digest,
                "identical_population_proof": True,
                "minimum_required": MINIMUM_COMMON_POSES,
            },
            "artifacts": association_artifacts,
        }
        association_manifest_path = staging / "association_manifest.json"
        write_json(association_manifest_path, association_manifest)

        crop_manifest = {
            "schema": CROP_SCHEMA,
            "status": "COMPLETE",
            "protocol_id": u0["manifest"]["protocol_id"],
            "sequence": u0["manifest"]["sequence"],
            "timestamp_policy": {
                "reference": "original_ground_truth_timestamp_token",
                "estimate_pose_rows": "selected_original_estimate_pose_tokens",
                "estimate_output_timestamp": "replaced_by_exact_selected_gt_timestamp_token",
                "reason": "force_exact_one_to_one_evo_association_without_changing_poses",
            },
            "population": association_manifest["common_population"],
            "ground_truth_row_ids": common_row_ids,
            "ground_truth_row_ids_sha256": population_digest,
            "common_tum_artifacts": common_artifacts,
            "rpe_reference_pairs": {
                "selection_implementation": "evo.core.metrics.id_pairs_from_delta",
                "delta": RPE_DELTA_METERS,
                "delta_unit": "meters",
                "relative_delta_tolerance": RPE_RELATIVE_DELTA_TOLERANCE,
                "absolute_delta_tolerance_m": (
                    RPE_DELTA_METERS * RPE_RELATIVE_DELTA_TOLERANCE
                ),
                "all_pairs": RPE_ALL_PAIRS,
                "pairs_from_reference": RPE_PAIRS_FROM_REFERENCE,
                "count": len(reference_pairs),
                "minimum_required": MINIMUM_RPE_PAIRS,
                "pair_identities_sha256": rpe_pair_digest,
                "pair_tuples_sha256": tuple_digest,
                "full_tuple_consumption_required": True,
                "artifact": rpe_pair_artifact,
            },
            "identical_population_and_rpe_pair_set_for_both_systems": True,
        }
        crop_manifest_path = staging / "crop_manifest.json"
        write_json(crop_manifest_path, crop_manifest)

        metrics_artifacts = make_artifact_index(staging, list(metric_paths.values()))
        method_public = {
            system: {
                "alignment": method_results[system]["alignment"],
                "primary": method_results[system]["primary"],
                "statistics": method_results[system]["statistics"],
                "error_artifacts": {
                    key: value
                    for key, value in metrics_artifacts.items()
                    if Path(key).name.startswith(system.lower() + "_")
                },
                "rpe_pair_identities_sha256": rpe_pair_digest,
                "rpe_pair_tuples_sha256": tuple_digest,
            }
            for system in SYSTEMS
        }
        metrics_path = staging / "metrics" / "paired_metrics.json"
        write_json(
            metrics_path,
            {
                "schema": SCHEMA + ".metrics",
                "status": "COMPLETE",
                "metric_semantics": {
                    "alignment": "independent SE(3) Umeyama per method, scale fixed to 1",
                    "alignment_population": "all exact common GT rows",
                    "ape_pose_relation": "translation_part",
                    "rpe_delta": RPE_DELTA_METERS,
                    "rpe_delta_unit": "meters",
                    "rpe_relative_delta_tolerance": RPE_RELATIVE_DELTA_TOLERANCE,
                    "rpe_all_pairs": RPE_ALL_PAIRS,
                    "rpe_pairs_from_reference": RPE_PAIRS_FROM_REFERENCE,
                    "rpe_pair_identities_sha256": rpe_pair_digest,
                    "rpe_pair_tuple_consumption": (
                        "one materialized full (start,end) tuple list reused directly "
                        "by evo.core.metrics.RPE.rpe_base for both systems and relations"
                    ),
                    "rpe_pair_tuples_sha256": tuple_digest,
                    "statistic": "RMSE",
                },
                "systems": method_public,
            },
        )

        comparison: Dict[str, Dict[str, Any]] = {}
        for metric_id in (
            "ate_translation_rmse_m",
            "rpe_translation_rmse_1m_m",
            "rpe_rotation_rmse_1m_deg",
        ):
            baseline = method_results["U0"]["primary"][metric_id]
            candidate = method_results["S1"]["primary"][metric_id]
            comparison[metric_id] = relative_difference_record(
                baseline, candidate
            )

        result_paths = [
            association_manifest_path,
            crop_manifest_path,
            metrics_path,
            *association_paths.values(),
            common_csv,
            *common_paths.values(),
            rpe_pairs_path,
            *metric_paths.values(),
        ]
        pair_result = {
            "schema": SCHEMA,
            "status": "COMPLETE",
            "protocol_id": u0["manifest"]["protocol_id"],
            "sequence": u0["manifest"]["sequence"],
            "dry_run": bool(u0["manifest"].get("dry_run")),
            "evidence_class": "PREVIEW_ONE_PASS_DIAGNOSTIC",
            "publication_claim_eligible": False,
            "interpretation": "descriptive paired output only; this file makes no superiority or causal claim",
            "source_runs": {
                "U0": {
                    "directory": u0["directory"],
                    "run_id": u0["manifest"]["run_id"],
                    "status": u0["manifest"]["status"],
                    "manifest": u0["manifest_identity"],
                    "live_state": u0["numeric_output_identities"]["state"],
                    "live_deviation": u0["numeric_output_identities"]["deviation"],
                    "live_trajectory": u0["trajectory_identity"],
                },
                "S1": {
                    "directory": s1["directory"],
                    "run_id": s1["manifest"]["run_id"],
                    "status": s1["manifest"]["status"],
                    "manifest": s1["manifest_identity"],
                    "live_state": s1["numeric_output_identities"]["state"],
                    "live_deviation": s1["numeric_output_identities"]["deviation"],
                    "live_trajectory": s1["trajectory_identity"],
                },
            },
            "inputs": {
                "ground_truth": identity_cache[ground_truth],
                "protocol": dict(protocol_identity),
                "manifest_shared_input_trees_exactly_equal": True,
                "method_runtime_identities_revalidated_live": True,
                "method_runtime_identities": live_estimator_runtime,
                "all_recorded_input_identities_verified_live": True,
                "verified_identity_record_counts": {
                    "U0": u0["input_identity_record_count"],
                    "S1": s1["input_identity_record_count"],
                },
            },
            "runtime": runtime,
            "evaluator_runtime_pin_check": evaluator_runtime_pin_check,
            "common_population": association_manifest["common_population"],
            "rpe_reference_pairs": crop_manifest["rpe_reference_pairs"],
            "metrics": {
                system: method_results[system]["primary"] for system in SYSTEMS
            },
            "relative_difference_s1_minus_u0_over_u0": comparison,
            "checks": {
                "run_manifests_and_live_input_hashes_verified": True,
                "estimator_runtime_identities_revalidated": True,
                "evaluator_runtime_matches_protocol_pins": True,
                "live_trajectory_hashes_verified": True,
                "live_state_and_deviation_hashes_verified": True,
                "exact_gt_row_identity_intersection": True,
                "identical_common_population": True,
                "identical_reference_rpe_pair_set": True,
                "independent_se3_alignment_without_scale": True,
                "minimum_population_requirements_passed": True,
                "primary_metrics_nonnegative_and_finite": True,
                "zero_baseline_relative_difference_semantics_explicit": True,
                "atomic_new_output": True,
            },
            "artifacts": make_artifact_index(staging, result_paths),
        }
        result_path = staging / "pair_result.json"
        write_json(result_path, pair_result)

        checksum_candidates = sorted(
            [path for path in staging.rglob("*") if path.is_file()],
            key=lambda path: path.relative_to(staging).as_posix(),
        )
        checksum_path = staging / "SHA256SUMS"
        with checksum_path.open("x", encoding="ascii", newline="\n") as stream:
            for path in checksum_candidates:
                stream.write(
                    f"{sha256_file(path)}  {path.relative_to(staging).as_posix()}\n"
                )
            stream.flush()
            os.fsync(stream.fileno())

        # Validate strict JSON and every checksum before the atomic install.
        for json_path in staging.rglob("*.json"):
            json.loads(json_path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        for line in checksum_path.read_text(encoding="ascii").splitlines():
            digest, relative = line.split("  ", 1)
            if sha256_file(staging / relative) != digest:
                raise PairEvaluationError(f"staged checksum mismatch: {relative}")
        staging_fd = os.open(str(staging), os.O_RDONLY)
        try:
            os.fsync(staging_fd)
        finally:
            os.close(staging_fd)
        staged_result = load_json(result_path, "staged pair result")
        if staged_result != pair_result:
            raise PairEvaluationError("staged pair result changed after serialization")
        if final_output.exists():
            raise PairEvaluationError(f"pair output appeared during evaluation: {final_output}")
        directory_fd = os.open(str(final_output.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
            install_attempted = True
            os.replace(staging, final_output)
            installed = True
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return staged_result
    except BaseException as original:
        cleanup_errors: List[str] = []
        owns_final = installed or (
            install_attempted and not staging.exists() and final_output.exists()
        )
        if owns_final and final_output.exists():
            try:
                if staging.exists():
                    shutil.rmtree(staging)
                os.replace(final_output, staging)
            except BaseException as exc:
                cleanup_errors.append(f"rollback rename failed: {exc}")
                if final_output.exists():
                    try:
                        shutil.rmtree(final_output)
                    except BaseException as remove_exc:
                        cleanup_errors.append(
                            f"installed output removal failed: {remove_exc}"
                        )
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except BaseException as exc:
                cleanup_errors.append(f"staging cleanup failed: {exc}")
        try:
            cleanup_directory_fd = os.open(str(final_output.parent), os.O_RDONLY)
            try:
                os.fsync(cleanup_directory_fd)
            finally:
                os.close(cleanup_directory_fd)
        except BaseException as exc:
            cleanup_errors.append(f"cleanup directory fsync failed: {exc}")
        if cleanup_errors:
            raise PairEvaluationError(
                "paired-evaluation failure cleanup was incomplete: "
                + "; ".join(cleanup_errors)
            ) from original
        raise


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--u0-run", type=Path, required=True)
    value.add_argument("--s1-run", type=Path, required=True)
    value.add_argument("--ground-truth", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = evaluate_pair(args.u0_run, args.s1_run, args.ground_truth, args.output)
    except (PairEvaluationError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
