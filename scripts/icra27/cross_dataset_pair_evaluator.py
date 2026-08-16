#!/usr/bin/python3
"""Evaluate one completed U0/S1 pair on one exact common GT population.

The estimator runs are immutable inputs.  This post-close evaluator re-hashes
their manifests and trajectory artifacts, associates each trajectory to the
same ground-truth source rows, intersects those exact row identities, and
uses one reference-derived list of 1 m RPE tuples for both systems.  Each
trajectory receives an independent SE(3) alignment; scale is never changed.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, Mapping, Optional, Sequence
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
CORE_PATH = SCRIPT_DIR / "kaist_pair_evaluator.py"
CORE_SPEC = importlib.util.spec_from_file_location(
    "_cross_dataset_pair_math", CORE_PATH
)
if CORE_SPEC is None or CORE_SPEC.loader is None:
    raise RuntimeError("cannot load the frozen pair-evaluation math")
CORE = importlib.util.module_from_spec(CORE_SPEC)
sys.modules[CORE_SPEC.name] = CORE
CORE_SPEC.loader.exec_module(CORE)


SCHEMA = "schurvio.icra27.cross_dataset.pair_result.v1"
RUN_SCHEMA = "schurvio.icra27.cross_dataset.sequence_result.v1"
MATRIX_SCHEMA = "schurvio.icra27.cross_dataset_matrix.v1"
EXPECTED_PROTOCOL_ID = "CDSC-1R4"
SYSTEMS = ("U0", "S1")
ELIGIBLE_STATUSES = {"COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT"}
MINIMUM_COMMON_POSES = 100
MINIMUM_RPE_PAIRS = 100
EXPECTED_EVO_VERSION = "1.31.1"
EXPECTED_RUNTIME_PINS_SHA256 = (
    "3e09ca65d06eb79e7c6c0c30361bbf189620ce5741771a0d9074ee09a8653f14"
)
EXPECTED_MATH_CORE_SHA256 = (
    "24c30b19c65f95c644065eda35534e803d4ffc72825f9e6aa0ffb9a2bbffaa7c"
)
MAX_EVO_QUATERNION_NORM_ERROR = 5.0e-4


class EvaluationError(RuntimeError):
    """A fail-closed paired-evaluation contract violation."""


def _read_tum_with_bounded_quaternion_projection(
    path: Path, label: str
) -> tuple[Sequence[Any], Dict[str, Any]]:
    """Parse exact TUM rows and project only their in-memory evo quaternion."""

    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise EvaluationError(f"{label} is not a regular file: {resolved}")
    rows = []
    norms = []
    errors = []
    for line_number, source_line in enumerate(resolved.read_bytes().splitlines(), 1):
        stripped = source_line.strip()
        if not stripped or stripped.startswith(b"#"):
            continue
        try:
            text = stripped.decode("ascii")
        except UnicodeDecodeError as exc:
            raise EvaluationError(f"{label} has a non-ASCII row") from exc
        tokens = tuple(text.split())
        if len(tokens) != 8:
            raise EvaluationError(
                f"{label} row {line_number} has {len(tokens)} columns, expected 8"
            )
        try:
            raw_values = tuple(float(token) for token in tokens)
        except ValueError as exc:
            raise EvaluationError(f"{label} has a nonnumeric row") from exc
        if not all(math.isfinite(value) for value in raw_values):
            raise EvaluationError(f"{label} has a nonfinite row")
        quaternion = raw_values[4:8]
        norm = math.sqrt(sum(value * value for value in quaternion))
        error = abs(norm - 1.0)
        if (
            not math.isfinite(norm)
            or norm <= 0.0
            or error > MAX_EVO_QUATERNION_NORM_ERROR
        ):
            raise EvaluationError(
                f"{label} row {line_number} quaternion norm is outside the "
                "frozen evo-projection boundary"
            )
        projected_values = (
            *raw_values[:4],
            *(value / norm for value in quaternion),
        )
        row_index = len(rows)
        row_id = hashlib.sha256(
            str(row_index).encode("ascii")
            + b"\0"
            + str(line_number).encode("ascii")
            + b"\0"
            + stripped
        ).hexdigest()
        rows.append(
            CORE.TumRow(
                data_index=row_index,
                source_line_number=line_number,
                source_bytes=stripped,
                tokens=tokens,
                values=tuple(projected_values),
                row_id=row_id,
            )
        )
        norms.append(norm)
        errors.append(error)
    if not rows:
        raise EvaluationError(f"{label} contains no data rows")
    timestamps = CORE.np.asarray([row.timestamp for row in rows], dtype=CORE.np.float64)
    if not CORE.np.all(CORE.np.diff(timestamps) > 0.0):
        raise EvaluationError(f"{label} timestamps are not unique and increasing")
    projected = CORE.rows_to_evo(rows)
    valid, details = projected.check()
    if not valid:
        raise EvaluationError(f"{label} projected evo trajectory is invalid: {details}")
    return rows, {
        "policy": "q_over_l2_norm_for_evo_objects_only",
        "maximum_allowed_abs_norm_error": MAX_EVO_QUATERNION_NORM_ERROR,
        "source_row_count": len(rows),
        "rows_projected": sum(error != 0.0 for error in errors),
        "minimum_source_norm": min(norms),
        "maximum_source_norm": max(norms),
        "maximum_abs_source_norm_error": max(errors),
        "source_bytes_unchanged": True,
        "source_tokens_unchanged": True,
        "row_ids_from_raw_source_bytes": True,
    }


def _project_loaded_evo_trajectory(value: Any, label: str) -> Any:
    quaternions = CORE.np.asarray(value.orientations_quat_wxyz, dtype=CORE.np.float64)
    norms = CORE.np.linalg.norm(quaternions, axis=1)
    errors = CORE.np.abs(norms - 1.0)
    if (
        not CORE.np.all(CORE.np.isfinite(norms))
        or CORE.np.any(norms <= 0.0)
        or CORE.np.any(errors > MAX_EVO_QUATERNION_NORM_ERROR)
    ):
        raise EvaluationError(f"{label} quaternion projection boundary failed")
    projected = CORE.trajectory.PoseTrajectory3D(
        positions_xyz=CORE.np.asarray(value.positions_xyz, dtype=CORE.np.float64),
        orientations_quat_wxyz=quaternions / norms[:, None],
        timestamps=CORE.np.asarray(value.timestamps, dtype=CORE.np.float64),
    )
    valid, details = projected.check()
    if not valid:
        raise EvaluationError(f"{label} projected evo trajectory is invalid: {details}")
    return projected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(path: Path) -> Dict[str, Any]:
    if path.is_symlink():
        raise EvaluationError(f"refusing symlink identity: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise EvaluationError(f"not a regular, nonsymlink file: {resolved}")
    before = resolved.stat()
    digest = sha256_file(resolved)
    after = resolved.stat()
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise EvaluationError(f"file changed while hashing: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": after.st_size,
        "sha256": digest,
    }


def _cached_identity(
    path: Path, cache: Dict[Path, Dict[str, Any]]
) -> Dict[str, Any]:
    if path.is_symlink():
        raise EvaluationError(f"refusing symlink identity: {path}")
    resolved = path.resolve(strict=True)
    if resolved not in cache:
        cache[resolved] = identity(path)
    return cache[resolved]


def load_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON token {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError(f"cannot read {label}: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{label} is not a JSON object")
    return value


def _load_json_with_identity(path: Path, label: str) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Parse and identify exactly the same manifest bytes."""

    if path.is_symlink():
        raise EvaluationError(f"refusing symlink {label}: {path}")
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise EvaluationError(f"{label} is not a regular file: {resolved}")
    try:
        payload = resolved.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=lambda token: (_ for _ in ()).throw(
                ValueError(f"nonfinite JSON token {token}")
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvaluationError(f"cannot read {label}: {resolved}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvaluationError(f"{label} is not a JSON object")
    observed = {
        "path": str(resolved),
        "size_bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    if identity(resolved) != observed:
        raise EvaluationError(f"{label} changed while it was read")
    return value, observed


def _recorded_identity(record: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(record, dict):
        raise EvaluationError(f"{label} identity is absent")
    path = record.get("path", record.get("canonical_path"))
    if not isinstance(path, str) or not path:
        raise EvaluationError(f"{label} identity path is malformed")
    size = record.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise EvaluationError(f"{label} identity size is malformed")
    digest = record.get("sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise EvaluationError(f"{label} identity digest is malformed")
    return record


def _revalidate_identity(
    record: Mapping[str, Any],
    label: str,
    cache: Optional[Dict[Path, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    recorded = _recorded_identity(record, label)
    path = Path(str(recorded.get("path", recorded.get("canonical_path"))))
    observed = identity(path) if cache is None else _cached_identity(path, cache)
    if (
        observed["path"] != str(path.resolve(strict=True))
        or observed["size_bytes"] != recorded["size_bytes"]
        or observed["sha256"] != recorded["sha256"]
    ):
        raise EvaluationError(f"{label} live identity drift")
    return observed


def _manifest_identity_records(
    value: Any, prefix: str = "inputs"
) -> list[tuple[str, Mapping[str, Any]]]:
    """Find file identities nested in a sequence-result input tree."""

    records: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(value, dict):
        path_present = isinstance(value.get("path", value.get("canonical_path")), str)
        if path_present and {"size_bytes", "sha256"}.issubset(value):
            records.append((prefix, value))
        for key in sorted(value):
            records.extend(_manifest_identity_records(value[key], f"{prefix}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            records.extend(_manifest_identity_records(item, f"{prefix}[{index}]"))
    return records


def _verify_manifest_inputs(
    manifest: Mapping[str, Any], cache: Dict[Path, Dict[str, Any]]
) -> list[Dict[str, Any]]:
    inputs = manifest.get("inputs")
    if inputs is None:
        return []
    if not isinstance(inputs, dict):
        raise EvaluationError("run manifest inputs are malformed")
    verified = []
    for label, record in _manifest_identity_records(inputs):
        observed = _revalidate_identity(record, label, cache)
        verified.append({"label": label, "identity": observed})
    return verified


def _identity_projection(record: Any, label: str) -> Dict[str, Any]:
    value = _recorded_identity(record, label)
    path = Path(str(value.get("path", value.get("canonical_path")))).resolve(
        strict=True
    )
    return {
        "path": str(path),
        "size_bytes": value["size_bytes"],
        "sha256": value["sha256"],
    }


def _require_shared_run_input(
    runs: Mapping[str, Mapping[str, Any]], name: str
) -> Dict[str, Any]:
    records = {}
    for system in SYSTEMS:
        inputs = runs[system]["manifest"].get("inputs")
        record = inputs.get(name) if isinstance(inputs, dict) else None
        records[system] = _identity_projection(record, f"{system} inputs.{name}")
    if records["U0"] != records["S1"]:
        raise EvaluationError(f"paired run input {name} identity mismatch")
    return records["U0"]


def _matrix_file_identity(
    record: Any, label: str, cache: Dict[Path, Dict[str, Any]]
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise EvaluationError(f"matrix {label} record is absent")
    path = record.get("canonical_path", record.get("path"))
    standard = {
        "path": path,
        "size_bytes": record.get("bytes"),
        "sha256": record.get("sha256"),
    }
    _recorded_identity(standard, f"matrix {label}")
    observed = _cached_identity(Path(str(path)), cache)
    if observed != standard:
        raise EvaluationError(f"matrix {label} identity differs from live file")
    return observed


def _validate_matrix_binding(
    matrix_path: Path,
    runs: Mapping[str, Mapping[str, Any]],
    ground_truth: Mapping[str, Any],
    cache: Dict[Path, Dict[str, Any]],
) -> Dict[str, Any]:
    matrix_identity = _cached_identity(matrix_path, cache)
    recorded_matrix = _require_shared_run_input(runs, "matrix")
    if matrix_identity != recorded_matrix:
        raise EvaluationError("requested matrix differs from paired run matrix identity")
    protocol_identity = _require_shared_run_input(runs, "protocol")
    recorded_bag = _require_shared_run_input(runs, "bag")
    try:
        matrix = CORE.yaml.safe_load(matrix_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, CORE.yaml.YAMLError) as exc:
        raise EvaluationError(f"cannot read frozen matrix: {matrix_path}: {exc}") from exc
    if not isinstance(matrix, dict) or matrix.get("schema") != MATRIX_SCHEMA:
        raise EvaluationError("cross-dataset matrix schema drift")
    sequences = matrix.get("sequences")
    if not isinstance(sequences, list):
        raise EvaluationError("cross-dataset matrix sequence list is malformed")
    dataset = runs["U0"]["manifest"]["dataset"]
    sequence = runs["U0"]["manifest"]["sequence"]
    matches = [
        row
        for row in sequences
        if isinstance(row, dict)
        and row.get("dataset") == dataset
        and row.get("sequence") == sequence
    ]
    if len(matches) != 1:
        raise EvaluationError(
            f"matrix must contain exactly one row for {dataset}/{sequence}"
        )
    row = matches[0]
    matrix_bag = _matrix_file_identity(row.get("bag"), "selected bag", cache)
    if matrix_bag != recorded_bag:
        raise EvaluationError("matrix selected bag differs from paired run bag")
    gt_record = row.get("ground_truth")
    if not isinstance(gt_record, dict):
        raise EvaluationError("matrix ground-truth record is absent")
    if gt_record.get("capability") != "full_trajectory":
        raise EvaluationError(
            "matrix row is not accuracy eligible: full_trajectory ground truth required"
        )
    matrix_ground_truth = _matrix_file_identity(gt_record, "ground truth", cache)
    if matrix_ground_truth != ground_truth:
        raise EvaluationError("requested ground truth differs from frozen matrix row")
    return {
        "identity": matrix_identity,
        "protocol": protocol_identity,
        "schema": MATRIX_SCHEMA,
        "row_order": row.get("order"),
        "dataset": dataset,
        "sequence": sequence,
        "bag": matrix_bag,
        "ground_truth": matrix_ground_truth,
        "ground_truth_capability": "full_trajectory",
    }


def _verify_artifact(
    run_dir: Path,
    manifest: Mapping[str, Any],
    name: str,
    expected_relative: str,
    cache: Dict[Path, Dict[str, Any]],
) -> Dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not isinstance(artifacts.get(name), dict):
        raise EvaluationError(f"run artifact {name} is absent")
    entry = artifacts[name]
    if entry.get("relative_path") != expected_relative:
        raise EvaluationError(f"run artifact {name} path contract drift")
    expected_candidate = run_dir / expected_relative
    if expected_candidate.is_symlink():
        raise EvaluationError(f"run artifact {name} cannot be a symlink")
    expected_path = expected_candidate.resolve(strict=True)
    try:
        expected_path.relative_to(run_dir)
    except ValueError as exc:
        raise EvaluationError(f"run artifact {name} escaped run directory") from exc
    recorded = _recorded_identity(entry.get("identity"), f"artifact {name}")
    recorded_path = Path(str(recorded.get("path", recorded.get("canonical_path")))).resolve(
        strict=True
    )
    if recorded_path != expected_path:
        raise EvaluationError(f"run artifact {name} identity points elsewhere")
    observed = _cached_identity(expected_path, cache)
    if (
        observed["size_bytes"] != recorded["size_bytes"]
        or observed["sha256"] != recorded["sha256"]
    ):
        raise EvaluationError(f"run artifact {name} changed after publication")
    return observed


def validate_run(
    run: Path, expected_system: str, cache: Dict[Path, Dict[str, Any]]
) -> Dict[str, Any]:
    if run.is_symlink():
        raise EvaluationError(f"{expected_system} run cannot be a symlink")
    run_dir = run.resolve(strict=True)
    if not run_dir.is_dir():
        raise EvaluationError(f"{expected_system} run is not a regular directory")
    manifest_path = run_dir / "sequence_result.json"
    manifest, manifest_identity = _load_json_with_identity(
        manifest_path, f"{expected_system} sequence result"
    )
    if manifest.get("schema") != RUN_SCHEMA:
        raise EvaluationError(f"{expected_system} sequence-result schema drift")
    if manifest.get("protocol_id") != EXPECTED_PROTOCOL_ID:
        raise EvaluationError(f"{expected_system} protocol ID drift")
    if manifest.get("system") != expected_system or manifest.get("mode") != "scored":
        raise EvaluationError(f"{expected_system} run identity/mode mismatch")
    if manifest.get("status") not in ELIGIBLE_STATUSES:
        raise EvaluationError(f"{expected_system} run status is not accuracy eligible")
    if manifest.get("accuracy_eligible") is not True:
        raise EvaluationError(f"{expected_system} run denied accuracy eligibility")
    if (
        expected_system != "U0"
        and manifest.get("status") == "COMPLETED_WITH_TEARDOWN_DEFECT"
    ):
        raise EvaluationError("the teardown-defect completion is U0-only")
    for field in ("protocol_id", "dataset", "sequence", "run_id"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise EvaluationError(f"{expected_system} run field {field} is malformed")
    input_identities = _verify_manifest_inputs(manifest, cache)
    state = _verify_artifact(
        run_dir, manifest, "state", "trajectory/state_estimate.txt", cache
    )
    deviation = _verify_artifact(
        run_dir, manifest, "deviation", "trajectory/state_deviation.txt", cache
    )
    tum = _verify_artifact(
        run_dir, manifest, "tum", "trajectory/estimate_raw.tum", cache
    )
    return {
        "directory": str(run_dir),
        "manifest": manifest,
        "manifest_identity": manifest_identity,
        "input_identities": input_identities,
        "state_identity": state,
        "deviation_identity": deviation,
        "trajectory_identity": tum,
        "trajectory_path": Path(tum["path"]),
    }


def _write_csv(path: Path, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, allow_nan=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _relative_identity(root: Path, path: Path) -> Dict[str, Any]:
    observed = identity(path)
    relative = path.resolve(strict=True).relative_to(root.resolve(strict=True))
    return {
        "relative_path": relative.as_posix(),
        "size_bytes": observed["size_bytes"],
        "sha256": observed["sha256"],
    }


def _float_record(value: float) -> Dict[str, Any]:
    if not math.isfinite(value):
        raise EvaluationError("attempted to record nonfinite float")
    return {"decimal_17g": format(value, ".17g"), "value": value}


def _relative_difference_record(
    baseline_u0: float, candidate_s1: float
) -> Dict[str, Any]:
    """Represent the frozen ratio while leaving every zero denominator undefined."""

    if (
        not math.isfinite(baseline_u0)
        or not math.isfinite(candidate_s1)
        or baseline_u0 < 0.0
        or candidate_s1 < 0.0
    ):
        raise EvaluationError("relative-difference inputs must be finite RMSE values")
    common = {
        "baseline_u0": baseline_u0,
        "candidate_s1": candidate_s1,
        "formula": "(S1-U0)/U0",
    }
    if baseline_u0 == 0.0:
        return {
            **common,
            "status": "UNASSESSABLE_ZERO_U0_DENOMINATOR",
            "mathematical_ratio_defined": False,
            "value": None,
        }
    return {
        **common,
        "status": "DEFINED_FINITE",
        "mathematical_ratio_defined": True,
        "value": (candidate_s1 - baseline_u0) / baseline_u0,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_no_replace(staging: Path, destination: Path) -> None:
    """Atomically install a directory without ever replacing an existing path."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise EvaluationError("atomic no-replace directory publication is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,  # AT_FDCWD
        os.fsencode(str(staging)),
        -100,
        os.fsencode(str(destination)),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise EvaluationError(f"refusing to overwrite pair output: {destination}")
    raise OSError(error_number, os.strerror(error_number), str(destination))


def _common_population_record(
    common_gt: Sequence[Any], reference_pose_count: int
) -> Dict[str, Any]:
    """Describe an exact common population, including the empty case."""

    if reference_pose_count <= 0:
        raise EvaluationError("ground-truth population is empty")
    positions = CORE.np.asarray(
        [row.values[1:4] for row in common_gt], dtype=CORE.np.float64
    )
    if len(common_gt) < 2:
        path_length = 0.0
    else:
        path_length = float(
            CORE.np.linalg.norm(CORE.np.diff(positions, axis=0), axis=1).sum()
        )
    if not math.isfinite(path_length):
        raise EvaluationError("common reference spatial coverage is nonfinite")
    return {
        "operation": "exact intersection of ground-truth row identities",
        "count": len(common_gt),
        "minimum_required": MINIMUM_COMMON_POSES,
        "requirement_passed": len(common_gt) >= MINIMUM_COMMON_POSES,
        "first_timestamp": (
            _float_record(common_gt[0].timestamp) if common_gt else None
        ),
        "last_timestamp": (
            _float_record(common_gt[-1].timestamp) if common_gt else None
        ),
        "duration_seconds": (
            _float_record(common_gt[-1].timestamp - common_gt[0].timestamp)
            if common_gt
            else None
        ),
        "reference_pose_count": reference_pose_count,
        "reference_pose_fraction": _float_record(
            len(common_gt) / reference_pose_count
        ),
        "reference_path_length_m": _float_record(path_length),
        "row_ids_sha256": CORE.canonical_digest(
            [row.row_id for row in common_gt]
        ),
    }


def _write_common_population_csv(
    staging: Path,
    common_gt: Sequence[Any],
    common_estimate: Mapping[str, Sequence[Any]],
) -> None:
    _write_csv(
        staging / "associations" / "common_population.csv",
        (
            "common_index",
            "gt_data_index",
            "gt_row_id_sha256",
            "gt_timestamp",
            "u0_estimate_data_index",
            "u0_estimate_row_id_sha256",
            "s1_estimate_data_index",
            "s1_estimate_row_id_sha256",
        ),
        [
            (
                ordinal,
                gt.data_index,
                gt.row_id,
                gt.timestamp_text,
                common_estimate["U0"][ordinal].data_index,
                common_estimate["U0"][ordinal].row_id,
                common_estimate["S1"][ordinal].data_index,
                common_estimate["S1"][ordinal].row_id,
            )
            for ordinal, gt in enumerate(common_gt)
        ],
    )


def _runtime_provenance() -> tuple[Dict[str, Any], str]:
    runtime = CORE.runtime_identity()
    if runtime.get("evo", {}).get("version") != EXPECTED_EVO_VERSION:
        raise EvaluationError("evo version drift")
    runtime_projection = CORE.runtime_pin_projection(runtime)
    runtime_digest = CORE.canonical_digest(runtime_projection)
    if runtime_digest != EXPECTED_RUNTIME_PINS_SHA256:
        raise EvaluationError(f"evaluator runtime pin drift: {runtime_digest}")
    return runtime, runtime_digest


def _associate_or_empty(
    ground_truth_rows: Sequence[Any], estimate_rows: Sequence[Any]
) -> Sequence[Any]:
    """Keep a valid zero-match association as an accuracy population outcome."""

    try:
        return CORE.emulate_evo_association(ground_truth_rows, estimate_rows)
    except CORE.PairEvaluationError as exc:
        if str(exc) == "evo association produced no timestamp matches":
            return []
        raise


def _publish_pair_result(
    staging: Path,
    final_output: Path,
    result: Dict[str, Any],
    runs: Mapping[str, Mapping[str, Any]],
    ground_truth_identity: Mapping[str, Any],
    matrix_binding: Mapping[str, Any],
    evaluator_identity: Mapping[str, Any],
    math_core_identity: Mapping[str, Any],
) -> Dict[str, Any]:
    """Checksum, revalidate, and atomically publish a terminal pair result."""

    result_path = staging / "pair_result.json"
    _write_json(result_path, result)
    files = sorted(
        (path for path in staging.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(staging).as_posix(),
    )
    result["artifacts"] = {
        path.relative_to(staging).as_posix(): _relative_identity(staging, path)
        for path in files
        if path != result_path
    }
    result_path.unlink()
    _write_json(result_path, result)
    checksum_path = staging / "SHA256SUMS"
    with checksum_path.open("x", encoding="ascii", newline="\n") as stream:
        for path in sorted(
            (
                item
                for item in staging.rglob("*")
                if item.is_file() and item != checksum_path
            ),
            key=lambda item: item.relative_to(staging).as_posix(),
        ):
            stream.write(
                f"{sha256_file(path)}  {path.relative_to(staging).as_posix()}\n"
            )
        stream.flush()
        os.fsync(stream.fileno())
    for line in checksum_path.read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        if sha256_file(staging / relative) != digest:
            raise EvaluationError(f"staged checksum mismatch: {relative}")

    final_identity_cache: Dict[Path, Dict[str, Any]] = {}
    for system in SYSTEMS:
        _revalidate_identity(
            runs[system]["manifest_identity"],
            f"{system} sequence manifest",
            final_identity_cache,
        )
        for name in ("state_identity", "deviation_identity", "trajectory_identity"):
            _revalidate_identity(
                runs[system][name], f"{system} {name}", final_identity_cache
            )
        for item in runs[system]["input_identities"]:
            _revalidate_identity(
                item["identity"],
                f"{system} {item['label']}",
                final_identity_cache,
            )
    _revalidate_identity(ground_truth_identity, "ground truth", final_identity_cache)
    _revalidate_identity(
        matrix_binding["identity"], "cross-dataset matrix", final_identity_cache
    )
    _revalidate_identity(evaluator_identity, "pair evaluator", final_identity_cache)
    _revalidate_identity(
        math_core_identity, "pair-evaluation math core", final_identity_cache
    )
    _fsync_directory(staging)
    _fsync_directory(final_output.parent)
    _publish_directory_no_replace(staging, final_output)
    _fsync_directory(final_output.parent)
    return load_json(final_output / "pair_result.json", "published pair result")


def evaluate_pair(
    u0_run: Path,
    s1_run: Path,
    ground_truth_path: Path,
    matrix_path: Path,
    output: Path,
) -> Dict[str, Any]:
    final_output = output.resolve()
    if final_output.exists():
        raise EvaluationError(f"refusing to overwrite pair output: {final_output}")
    final_output.parent.mkdir(parents=True, exist_ok=True)
    staging = final_output.parent / f".{final_output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o755)
    committed = False
    try:
        initial_identity_cache: Dict[Path, Dict[str, Any]] = {}
        math_core_identity = _cached_identity(CORE_PATH, initial_identity_cache)
        if math_core_identity["sha256"] != EXPECTED_MATH_CORE_SHA256:
            raise EvaluationError("frozen pair-evaluation math core drift")
        runs = {
            "U0": validate_run(u0_run, "U0", initial_identity_cache),
            "S1": validate_run(s1_run, "S1", initial_identity_cache),
        }
        u0_manifest = runs["U0"]["manifest"]
        s1_manifest = runs["S1"]["manifest"]
        for key in ("protocol_id", "dataset", "sequence"):
            if u0_manifest.get(key) != s1_manifest.get(key):
                raise EvaluationError(f"paired run {key} mismatch")
        if u0_manifest["run_id"] == s1_manifest["run_id"]:
            raise EvaluationError("paired run IDs are not distinct")

        gt_identity = _cached_identity(ground_truth_path, initial_identity_cache)
        gt_path = Path(gt_identity["path"])
        matrix_binding = _validate_matrix_binding(
            matrix_path, runs, gt_identity, initial_identity_cache
        )
        gt_rows, gt_projection = _read_tum_with_bounded_quaternion_projection(
            gt_path, "ground truth"
        )
        estimate_rows = {}
        estimate_projection = {}
        for system in SYSTEMS:
            estimate_rows[system], estimate_projection[system] = (
                _read_tum_with_bounded_quaternion_projection(
                    runs[system]["trajectory_path"], f"{system} estimate"
                )
            )
        quaternion_projection = {
            "ground_truth": gt_projection,
            **estimate_projection,
        }
        associations = {
            system: _associate_or_empty(gt_rows, estimate_rows[system])
            for system in SYSTEMS
        }
        by_gt = {
            system: {item.gt_index: item.estimate_index for item in associations[system]}
            for system in SYSTEMS
        }
        common_indices = sorted(set(by_gt["U0"]).intersection(by_gt["S1"]))
        common_gt = [gt_rows[index] for index in common_indices]
        common_estimate = {
            system: [estimate_rows[system][by_gt[system][index]] for index in common_indices]
            for system in SYSTEMS
        }
        _write_common_population_csv(staging, common_gt, common_estimate)
        common_population = _common_population_record(common_gt, len(gt_rows))
        runtime, runtime_digest = _runtime_provenance()
        evaluator_identity = identity(Path(__file__))
        base_result: Dict[str, Any] = {
            "schema": SCHEMA,
            "protocol_id": u0_manifest["protocol_id"],
            "dataset": u0_manifest["dataset"],
            "sequence": u0_manifest["sequence"],
            "interpretation": (
                "whole-system paired descriptive evidence; not Schur attribution "
                "or a standalone robustness/superiority claim"
            ),
            "source_runs": {
                system: {
                    "directory": runs[system]["directory"],
                    "run_id": runs[system]["manifest"]["run_id"],
                    "status": runs[system]["manifest"]["status"],
                    "manifest": runs[system]["manifest_identity"],
                    "state": runs[system]["state_identity"],
                    "deviation": runs[system]["deviation_identity"],
                    "trajectory": runs[system]["trajectory_identity"],
                    "validated_inputs": runs[system]["input_identities"],
                }
                for system in SYSTEMS
            },
            "ground_truth": gt_identity,
            "quaternion_projection": quaternion_projection,
            "matrix_binding": matrix_binding,
            "association": {
                "implementation": "evo 1.31.1 matching_time_indices",
                "maximum_difference_s": CORE.ASSOCIATION_MAX_SECONDS,
                "u0_count": len(associations["U0"]),
                "s1_count": len(associations["S1"]),
            },
            "common_population": common_population,
            "evaluator_runtime": runtime,
            "evaluator_runtime_pins_sha256": runtime_digest,
            "evaluator": evaluator_identity,
            "math_core": math_core_identity,
        }
        base_checks: Dict[str, Any] = {
            "run_and_artifact_hashes_revalidated": True,
            "live_manifest_input_identity_count": {
                system: len(runs[system]["input_identities"])
                for system in SYSTEMS
            },
            "ground_truth_opened_only_by_post_close_evaluator": True,
            "identical_common_gt_population": True,
            "evaluator_runtime_pinned": True,
            "bounded_quaternion_projection_for_evo_only": True,
        }
        if len(common_gt) < MINIMUM_COMMON_POSES:
            result = {
                **base_result,
                "status": "UNASSESSABLE",
                "unassessable_reason": {
                    "code": "INSUFFICIENT_COMMON_POSES",
                    "observed_count": len(common_gt),
                    "minimum_required": MINIMUM_COMMON_POSES,
                    "metric_scope": "all_accuracy_metrics",
                },
                "rpe_reference_pairs": {
                    "status": "NOT_EVALUATED_INSUFFICIENT_COMMON_POSES",
                    "delta_m": 1.0,
                    "relative_tolerance": CORE.RPE_RELATIVE_DELTA_TOLERANCE,
                    "count": None,
                    "minimum_required": MINIMUM_RPE_PAIRS,
                    "requirement_evaluated": False,
                    "requirement_passed": None,
                },
                "metrics": {},
                "relative_difference_s1_minus_u0_over_u0": {},
                "checks": {
                    **base_checks,
                    "identical_full_reference_rpe_tuple_set": None,
                    "independent_se3_alignment_without_scale": None,
                    "minimum_population_requirements_passed": False,
                    "metrics_computed": False,
                },
            }
            published = _publish_pair_result(
                staging,
                final_output,
                result,
                runs,
                gt_identity,
                matrix_binding,
                evaluator_identity,
                math_core_identity,
            )
            committed = True
            return published

        common_dir = staging / "common"
        common_dir.mkdir()
        paths = {
            "reference": common_dir / "ground_truth_common.tum",
            "U0": common_dir / "u0_common.tum",
            "S1": common_dir / "s1_common.tum",
        }
        CORE.write_tum(paths["reference"], common_gt, common_gt)
        for system in SYSTEMS:
            CORE.write_tum(paths[system], common_estimate[system], common_gt)
        trajectories = {
            name: _project_loaded_evo_trajectory(
                CORE.file_interface.read_tum_trajectory_file(str(path)), name
            )
            for name, path in paths.items()
        }
        reference = trajectories["reference"]
        for system in SYSTEMS:
            if not CORE.np.array_equal(trajectories[system].timestamps, reference.timestamps):
                raise EvaluationError(f"{system} normalized timestamps differ from GT")

        reference_pairs = CORE.metrics.id_pairs_from_delta(
            reference.poses_se3,
            CORE.RPE_DELTA_METERS,
            CORE.metrics.Unit.meters,
            CORE.RPE_RELATIVE_DELTA_TOLERANCE,
            all_pairs=CORE.RPE_ALL_PAIRS,
        )
        reference_pairs = [(int(start), int(end)) for start, end in reference_pairs]
        if len(reference_pairs) < MINIMUM_RPE_PAIRS:
            pair_identity = [
                {
                    "index": index,
                    "start_gt_row_id": common_gt[start].row_id,
                    "end_gt_row_id": common_gt[end].row_id,
                }
                for index, (start, end) in enumerate(reference_pairs)
            ]
            _write_csv(
                staging / "associations" / "rpe_pairs_1m.csv",
                (
                    "pair_index",
                    "start_common_index",
                    "end_common_index",
                    "start_gt_row_id_sha256",
                    "end_gt_row_id_sha256",
                ),
                [
                    (
                        index,
                        start,
                        end,
                        common_gt[start].row_id,
                        common_gt[end].row_id,
                    )
                    for index, (start, end) in enumerate(reference_pairs)
                ],
            )
            result = {
                **base_result,
                "status": "UNASSESSABLE",
                "unassessable_reason": {
                    "code": "INSUFFICIENT_RPE_PAIRS",
                    "observed_count": len(reference_pairs),
                    "minimum_required": MINIMUM_RPE_PAIRS,
                    "metric_scope": "all_accuracy_metrics",
                },
                "rpe_reference_pairs": {
                    "status": "INSUFFICIENT_RPE_PAIRS",
                    "delta_m": 1.0,
                    "relative_tolerance": CORE.RPE_RELATIVE_DELTA_TOLERANCE,
                    "count": len(reference_pairs),
                    "minimum_required": MINIMUM_RPE_PAIRS,
                    "requirement_evaluated": True,
                    "requirement_passed": False,
                    "common_index_tuples_sha256": CORE.canonical_digest(
                        reference_pairs
                    ),
                    "full_pair_identities_sha256": CORE.canonical_digest(
                        pair_identity
                    ),
                },
                "metrics": {},
                "relative_difference_s1_minus_u0_over_u0": {},
                "checks": {
                    **base_checks,
                    "identical_full_reference_rpe_tuple_set": None,
                    "independent_se3_alignment_without_scale": None,
                    "minimum_population_requirements_passed": False,
                    "metrics_computed": False,
                },
            }
            published = _publish_pair_result(
                staging,
                final_output,
                result,
                runs,
                gt_identity,
                matrix_binding,
                evaluator_identity,
                math_core_identity,
            )
            committed = True
            return published
        method = {
            system: CORE.evaluate_method(
                system, reference, trajectories[system], reference_pairs
            )
            for system in SYSTEMS
        }
        expected_pair_tuples = [list(pair) for pair in reference_pairs]
        expected_pair_tuple_digest = CORE.canonical_digest(reference_pairs)
        for system in SYSTEMS:
            if method[system].get("rpe_pair_tuples") != expected_pair_tuples:
                raise EvaluationError(
                    f"{system} metric evaluation changed the shared 1 m tuple list"
                )
            if method[system].get("rpe_pair_tuples_sha256") != expected_pair_tuple_digest:
                raise EvaluationError(
                    f"{system} metric tuple digest differs from the shared list"
                )
            errors = method[system].get("errors", {})
            for metric_id in (
                "rpe_translation_1m_m",
                "rpe_rotation_1m_deg",
            ):
                if len(errors.get(metric_id, ())) != len(reference_pairs):
                    raise EvaluationError(
                        f"{system} {metric_id} does not cover every shared tuple"
                    )

        pair_csv = staging / "associations" / "rpe_pairs_1m.csv"
        _write_csv(
            pair_csv,
            (
                "pair_index",
                "start_common_index",
                "end_common_index",
                "start_gt_row_id_sha256",
                "end_gt_row_id_sha256",
            ),
            [
                (
                    index,
                    start,
                    end,
                    common_gt[start].row_id,
                    common_gt[end].row_id,
                )
                for index, (start, end) in enumerate(reference_pairs)
            ],
        )

        for system in SYSTEMS:
            path = staging / "metrics" / f"{system.lower()}_errors.csv"
            errors = method[system]["errors"]
            _write_csv(
                path,
                (
                    "kind",
                    "index",
                    "value",
                ),
                [
                    ("ate_translation_m", index, format(value, ".17g"))
                    for index, value in enumerate(errors["ate_translation_m"])
                ]
                + [
                    ("rpe_translation_1m_m", index, format(value, ".17g"))
                    for index, value in enumerate(errors["rpe_translation_1m_m"])
                ]
                + [
                    ("rpe_rotation_1m_deg", index, format(value, ".17g"))
                    for index, value in enumerate(errors["rpe_rotation_1m_deg"])
                ],
            )
        comparison = {}
        for metric_id in (
            "ate_translation_rmse_m",
            "rpe_translation_rmse_1m_m",
            "rpe_rotation_rmse_1m_deg",
        ):
            comparison[metric_id] = _relative_difference_record(
                method["U0"]["primary"][metric_id],
                method["S1"]["primary"][metric_id],
            )

        pair_identity = [
            {
                "index": index,
                "start_gt_row_id": common_gt[start].row_id,
                "end_gt_row_id": common_gt[end].row_id,
            }
            for index, (start, end) in enumerate(reference_pairs)
        ]
        result: Dict[str, Any] = {
            **base_result,
            "status": "COMPLETE",
            "rpe_reference_pairs": {
                "status": "COMPLETE",
                "delta_m": 1.0,
                "relative_tolerance": CORE.RPE_RELATIVE_DELTA_TOLERANCE,
                "count": len(reference_pairs),
                "minimum_required": MINIMUM_RPE_PAIRS,
                "requirement_evaluated": True,
                "requirement_passed": True,
                "common_index_tuples_sha256": expected_pair_tuple_digest,
                "full_pair_identities_sha256": CORE.canonical_digest(pair_identity),
                "systems": {
                    system: {
                        "tuple_count": len(method[system]["rpe_pair_tuples"]),
                        "common_index_tuples_sha256": method[system][
                            "rpe_pair_tuples_sha256"
                        ],
                    }
                    for system in SYSTEMS
                },
            },
            "metrics": {system: method[system]["primary"] for system in SYSTEMS},
            "relative_difference_s1_minus_u0_over_u0": comparison,
            "checks": {
                **base_checks,
                "identical_full_reference_rpe_tuple_set": True,
                "independent_se3_alignment_without_scale": True,
                "minimum_population_requirements_passed": True,
                "metrics_computed": True,
            },
        }
        published = _publish_pair_result(
            staging,
            final_output,
            result,
            runs,
            gt_identity,
            matrix_binding,
            evaluator_identity,
            math_core_identity,
        )
        committed = True
        return published
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--u0-run", type=Path, required=True)
    value.add_argument("--s1-run", type=Path, required=True)
    value.add_argument("--ground-truth", type=Path, required=True)
    value.add_argument("--matrix", type=Path, required=True)
    value.add_argument("--output", type=Path, required=True)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = evaluate_pair(
            args.u0_run,
            args.s1_run,
            args.ground_truth,
            args.matrix,
            args.output,
        )
    except (EvaluationError, CORE.PairEvaluationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
