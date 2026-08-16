#!/usr/bin/python3.8
"""Publish one fail-closed cross-dataset qualitative geometry bundle.

This tool runs only after a capture ``sequence_result.json`` has been
finalized.  It verifies the capture directory and its live checksum set, the
linked scored run, the frozen campaign matrix, the selected input identities,
the process-close evidence, and the raw feature-stream identity before it
opens geometry.  It never writes into either finalized estimator run.

Reference-backed rows delegate geometry interpretation and rendering to the
validated ``kaist_geometry_bundle`` v3 implementation.  TUM-VI corridor4 and
outdoors4 deliberately stay in the native estimator frame: no reference topic
is opened, no alignment is fitted, and their fixed views are illustration-only.
Terminal failures without usable emitted geometry receive an explicit failure
tile and manifest instead of invented points.  The frozen host has no approved
deterministic SVG rasterizer, so PNG output is explicitly forbidden.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import sys
import tempfile
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import kaist_geometry_bundle as geometry_v3  # noqa: E402


SCHEMA = "schurvio.icra27.cross_dataset_geometry_bundle.v1"
RESULT_SCHEMA = "schurvio.icra27.cross_dataset.sequence_result.v1"
MATRIX_SCHEMA = "schurvio.icra27.cross_dataset_matrix.v1"
V3_SCHEMA = "schurvio.icra27.kaist_geometry_bundle.v3"
PNG_POLICY = "PNG_NOT_PRODUCED_NO_APPROVED_DETERMINISTIC_RASTERIZER"
ELIGIBLE_CAPTURE_STATUSES = ("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT")
EXPECTED_SUFFIXES = (
    "poseimu",
    "points_slam",
    "points_msckf",
    "points_aruco",
    "loop_feats",
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
NATIVE_OUTPUTS = (
    "geometry/trajectory_native.ply",
    "geometry/slam_landmarks_final.ply",
    "geometry/msckf_update_points.ply",
    "geometry/loop_active_tracks_aggregate.ply",
    "geometry/snapshots/25.ply",
    "geometry/snapshots/50.ply",
    "geometry/snapshots/75.ply",
    "geometry/snapshots/max_angular_rate.ply",
    "figures/top.svg",
    "figures/side.svg",
    "figures/oblique.svg",
)
SEMANTIC_STYLE = {
    "trajectory": ((35, 35, 35), 0),
    "points_slam": ((35, 102, 184), 1),
    "points_msckf": ((230, 126, 34), 2),
    "points_aruco": ((171, 71, 188), 3),
    "loop_feats": ((40, 155, 91), 4),
}


class BundleError(RuntimeError):
    """A fail-closed postprocessing or evidence error."""


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.Node, deep: bool = False) -> dict:
    result: dict = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise BundleError("duplicate YAML key in campaign matrix: {}".format(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _strict_json_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        before = path.stat()
    except OSError as exc:
        raise BundleError("cannot stat {} before hashing: {}".format(path, exc)) from exc
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
    except OSError as exc:
        raise BundleError("cannot hash {}: {}".format(path, exc)) from exc
    stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in stable_fields):
        raise BundleError("file identity changed while hashing: {}".format(path))
    return digest.hexdigest()


def _regular_file(path: Path, label: str) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise BundleError("{} is a symlink: {}".format(label, expanded))
    try:
        resolved = expanded.resolve(strict=True)
    except OSError as exc:
        raise BundleError("{} does not resolve: {}".format(label, path)) from exc
    if not resolved.is_file() or resolved.is_symlink():
        raise BundleError("{} is not a regular nonsymlink file: {}".format(label, resolved))
    return resolved


def _file_identity(path: Path) -> Dict[str, Any]:
    resolved = _regular_file(path, "identity input")
    digest = _sha256_file(resolved)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": digest,
    }


def _identity_equal(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left.get(key) == right.get(key) for key in ("path", "size_bytes", "sha256"))


def _validate_recorded_identity(
    record: Any, observed: Mapping[str, Any], label: str
) -> None:
    if not isinstance(record, dict):
        raise BundleError("{} identity is absent".format(label))
    if not _identity_equal(record, observed):
        raise BundleError("{} identity differs from live bytes".format(label))


def _read_strict_json(path: Path, label: str) -> Tuple[Mapping[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=_strict_json_object
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError("{} is not strict UTF-8 JSON".format(label)) from exc
    if not isinstance(value, dict):
        raise BundleError("{} root is not an object".format(label))
    return value, payload


def _parse_utc(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise BundleError("{} must be a nonempty timestamp".format(label))
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise BundleError("{} is not ISO-8601".format(label)) from exc
    if parsed.tzinfo is None:
        raise BundleError("{} has no timezone".format(label))
    return parsed.astimezone(dt.timezone.utc)


def _safe_relative(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise BundleError("{} is not a safe relative path".format(label))
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise BundleError("{} is not a safe relative path".format(label))
    return path


def _verify_checksum_directory(
    run_dir: Path, result_path: Path, result_payload: bytes
) -> Mapping[str, Any]:
    checksum_path = _regular_file(run_dir / "SHA256SUMS", "run checksum set")
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise BundleError("run checksum set is not strict ASCII") from exc
    if not lines:
        raise BundleError("run checksum set is empty")
    entries: Dict[str, str] = {}
    for line in lines:
        if "  " not in line:
            raise BundleError("malformed run checksum line")
        digest, relative_text = line.split("  ", 1)
        if SHA256_RE.fullmatch(digest) is None:
            raise BundleError("malformed SHA-256 in run checksum set")
        relative = _safe_relative(relative_text, "checksummed run artifact")
        name = relative.as_posix()
        if name in entries:
            raise BundleError("duplicate run checksum path: {}".format(name))
        unresolved_artifact = run_dir / relative
        if unresolved_artifact.is_symlink():
            raise BundleError("checksummed artifact is a symlink: {}".format(name))
        try:
            artifact = unresolved_artifact.resolve(strict=True)
            artifact.relative_to(run_dir)
        except (OSError, ValueError) as exc:
            raise BundleError("checksummed artifact escapes or is missing: {}".format(name)) from exc
        if not artifact.is_file():
            raise BundleError("checksummed artifact is not a regular file: {}".format(name))
        if _sha256_file(artifact) != digest:
            raise BundleError("checksummed artifact identity mismatch: {}".format(name))
        entries[name] = digest

    live_names = set()
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            raise BundleError("finalized run contains a symlink: {}".format(path))
        if path.is_file() and path != checksum_path:
            live_names.add(path.relative_to(run_dir).as_posix())
    if live_names != set(entries):
        raise BundleError(
            "live run file membership differs from SHA256SUMS: missing={}, extra={}".format(
                sorted(set(entries) - live_names), sorted(live_names - set(entries))
            )
        )
    result_relative = result_path.relative_to(run_dir).as_posix()
    if entries.get(result_relative) != _sha256_bytes(result_payload):
        raise BundleError("sequence_result.json is not bound by live SHA256SUMS")
    return {
        "path": str(checksum_path),
        "sha256": _sha256_file(checksum_path),
        "size_bytes": checksum_path.stat().st_size,
        "verified_artifact_count": len(entries),
        "membership_exact": True,
    }


def _validate_close_evidence(result: Mapping[str, Any]) -> Mapping[str, Any]:
    close = result.get("estimator_close_receipt")
    if not isinstance(close, dict):
        raise BundleError("capture result lacks estimator_close_receipt")
    attempted = close.get("estimator_attempted")
    group_closed = close.get("estimator_process_group_closed")
    services_closed = close.get("runtime_services_closed")
    if not all(isinstance(value, bool) for value in (attempted, group_closed, services_closed)):
        raise BundleError("capture close booleans are malformed")
    commands = result.get("commands")
    estimator = commands.get("estimator") if isinstance(commands, dict) else None
    if attempted != isinstance(estimator, dict):
        raise BundleError("estimator_attempted disagrees with commands.estimator")
    closed_time = close.get("closed_utc")
    if attempted and group_closed and services_closed:
        if estimator.get("process_group_survived_cleanup") is not False:
            raise BundleError("estimator survivor flag does not prove closure")
        estimator_finished = _parse_utc(
            estimator.get("finished_utc"), "commands.estimator.finished_utc"
        )
        closed = _parse_utc(closed_time, "estimator_close_receipt.closed_utc")
        result_finished = _parse_utc(result.get("finished_utc"), "finished_utc")
        if estimator_finished > closed or closed > result_finished:
            raise BundleError("estimator close timestamps are not causally ordered")
    elif closed_time is not None:
        raise BundleError("unproved estimator closure has a non-null closed_utc")
    if not attempted and group_closed:
        raise BundleError("an unattempted estimator cannot have a closed process group")
    outcome = result.get("outcome_facts")
    checks = result.get("checks")
    teardown_fact = outcome.get("teardown_ok") if isinstance(outcome, dict) else None
    teardown_check = checks.get("teardown_complete") if isinstance(checks, dict) else None
    if not isinstance(teardown_fact, bool) or not isinstance(teardown_check, bool):
        raise BundleError("capture teardown evidence is malformed")
    if teardown_fact != teardown_check:
        raise BundleError("capture teardown fact/check disagree")
    ready = bool(
        attempted
        and group_closed
        and services_closed
        and teardown_fact
        and closed_time is not None
    )
    return {
        "estimator_attempted": attempted,
        "estimator_process_group_closed": group_closed,
        "runtime_services_closed": services_closed,
        "teardown_ok": teardown_fact,
        "closed_utc": closed_time,
        "geometry_access_permitted": ready,
    }


def _validate_sequence_result(path: Path, expected_mode: str) -> Mapping[str, Any]:
    result_path = _regular_file(path, "sequence result")
    value, payload = _read_strict_json(result_path, "sequence result")
    if value.get("schema") != RESULT_SCHEMA:
        raise BundleError("sequence result schema mismatch")
    if value.get("mode") != expected_mode:
        raise BundleError("sequence result mode is not {}".format(expected_mode))
    status = value.get("status")
    if not isinstance(status, str) or status in ("", "ACTIVE"):
        raise BundleError("sequence result is not terminal")
    _parse_utc(value.get("finished_utc"), "finished_utc")
    run_directory = value.get("run_directory")
    if not isinstance(run_directory, str) or not run_directory:
        raise BundleError("sequence result run_directory is invalid")
    try:
        run_dir = Path(run_directory).expanduser().resolve(strict=True)
    except OSError as exc:
        raise BundleError("sequence result run_directory does not resolve") from exc
    if not run_dir.is_dir() or result_path != run_dir / "sequence_result.json":
        raise BundleError("sequence result is not in its declared run directory")
    publication = value.get("publication")
    if not isinstance(publication, dict):
        raise BundleError("sequence result publication is absent")
    if (
        publication.get("sequence_result") != "sequence_result.json"
        or publication.get("checksums") != "SHA256SUMS"
        or publication.get("append_only_run_directory") is not True
    ):
        raise BundleError("sequence result publication contract mismatch")
    checksums = _verify_checksum_directory(run_dir, result_path, payload)
    close = _validate_close_evidence(value)
    identity = _file_identity(result_path)
    if (
        identity["size_bytes"] != len(payload)
        or identity["sha256"] != _sha256_bytes(payload)
    ):
        raise BundleError("sequence result changed while it was being validated")
    return {
        "path": result_path,
        "run_dir": run_dir,
        "value": value,
        "payload": payload,
        "identity": identity,
        "checksums": checksums,
        "close": close,
    }


def _read_matrix(path: Path) -> Mapping[str, Any]:
    matrix_path = _regular_file(path, "campaign matrix")
    payload = matrix_path.read_bytes()
    try:
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BundleError("campaign matrix is not strict UTF-8 YAML") from exc
    if not isinstance(value, dict) or value.get("schema") != MATRIX_SCHEMA:
        raise BundleError("campaign matrix schema mismatch")
    rows = value.get("sequences")
    if not isinstance(rows, list) or len(rows) != value.get("sequence_count"):
        raise BundleError("campaign matrix sequence count does not close")
    if value.get("status") != "INPUT_IDENTITIES_VERIFIED":
        raise BundleError("campaign matrix input identities are not frozen")
    identity = _file_identity(matrix_path)
    if (
        identity["size_bytes"] != len(payload)
        or identity["sha256"] != _sha256_bytes(payload)
    ):
        raise BundleError("campaign matrix changed while it was being validated")
    return {
        "path": matrix_path,
        "payload": payload,
        "identity": identity,
        "value": value,
    }


def _matrix_identity(record: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(record, dict):
        raise BundleError("matrix {} identity is absent".format(label))
    path_value = record.get("canonical_path", record.get("path"))
    if not isinstance(path_value, str) or not path_value:
        raise BundleError("matrix {} path is absent".format(label))
    observed = _file_identity(Path(path_value))
    expected = {
        "path": str(Path(path_value).expanduser().resolve(strict=True)),
        "size_bytes": record.get("bytes"),
        "sha256": record.get("sha256"),
    }
    if not _identity_equal(observed, expected):
        raise BundleError("matrix {} identity differs from live bytes".format(label))
    return observed


def _select_matrix_row(
    matrix: Mapping[str, Any], capture: Mapping[str, Any]
) -> Mapping[str, Any]:
    value = capture["value"]
    rows = matrix["value"]["sequences"]
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("dataset") == value.get("dataset")
        and row.get("sequence") == value.get("sequence")
    ]
    if len(matches) != 1:
        raise BundleError("capture does not select exactly one matrix row")
    row = matches[0]
    if value.get("system") not in row.get("system_order", []):
        raise BundleError("capture system is absent from selected matrix row")
    return row


def _artifact(
    capture: Mapping[str, Any], name: str, expected_relative: str
) -> Optional[Mapping[str, Any]]:
    artifacts = capture["value"].get("artifacts")
    record = artifacts.get(name) if isinstance(artifacts, dict) else None
    if not isinstance(record, dict) or record.get("relative_path") != expected_relative:
        raise BundleError("capture artifact {} path contract mismatch".format(name))
    identity = record.get("identity")
    if identity is None:
        return None
    path = capture["run_dir"] / expected_relative
    observed = _file_identity(path)
    _validate_recorded_identity(identity, observed, "capture artifact {}".format(name))
    if observed["path"] != str(path):
        raise BundleError("capture artifact {} escapes run directory".format(name))
    return observed


def _validate_selected_inputs(
    capture: Mapping[str, Any], matrix: Mapping[str, Any], row: Mapping[str, Any]
) -> Mapping[str, Any]:
    inputs = capture["value"].get("inputs")
    if not isinstance(inputs, dict):
        raise BundleError("capture result lacks inputs")
    matrix_bound = isinstance(inputs.get("matrix"), dict)
    if matrix_bound:
        _validate_recorded_identity(inputs["matrix"], matrix["identity"], "capture matrix")
    bag_bound = isinstance(inputs.get("bag"), dict)
    bag_identity: Optional[Mapping[str, Any]] = None
    if bag_bound:
        bag_identity = _matrix_identity(row.get("bag"), "selected bag")
        _validate_recorded_identity(inputs["bag"], bag_identity, "capture input bag")
        after = capture["value"].get("input_identities_after")
        if not isinstance(after, dict) or not isinstance(after.get("bag"), dict):
            raise BundleError("capture lacks postflight bag identity")
        _validate_recorded_identity(after["bag"], bag_identity, "capture postflight bag")
    return {
        "matrix_bound": matrix_bound,
        "bag_bound": bag_bound,
        "bag_identity": bag_identity,
    }


def _validate_scored_linkage(capture: Mapping[str, Any]) -> Mapping[str, Any]:
    link = capture["value"].get("scored_linkage")
    if not isinstance(link, dict):
        return {"valid": False, "reason": "SCORED_LINKAGE_NOT_ESTABLISHED", "scored": None}
    result_path_value = link.get("sequence_result_path")
    if not isinstance(result_path_value, str) or not result_path_value:
        return {"valid": False, "reason": "SCORED_LINKAGE_RESULT_ABSENT", "scored": None}
    scored = _validate_sequence_result(Path(result_path_value), "scored")
    for key in ("protocol_id", "dataset", "sequence", "system"):
        if scored["value"].get(key) != capture["value"].get(key):
            raise BundleError("linked scored result {} differs from capture".format(key))
    _validate_recorded_identity(link.get("sequence_result"), scored["identity"], "linked scored result")
    _validate_recorded_identity(
        link.get("sequence_result_after"), scored["identity"], "post-capture scored result"
    )
    capture_artifacts = capture["value"].get("artifacts")
    scored_artifacts = scored["value"].get("artifacts")
    content_exact = True
    comparisons = link.get("artifact_comparisons")
    for name in ("state", "deviation", "tum"):
        capture_record = capture_artifacts.get(name) if isinstance(capture_artifacts, dict) else None
        scored_record = scored_artifacts.get(name) if isinstance(scored_artifacts, dict) else None
        capture_identity = capture_record.get("identity") if isinstance(capture_record, dict) else None
        scored_identity = scored_record.get("identity") if isinstance(scored_record, dict) else None
        exact = bool(
            (capture_identity is None and scored_identity is None)
            or (
                isinstance(capture_identity, dict)
                and isinstance(scored_identity, dict)
                and capture_identity.get("size_bytes") == scored_identity.get("size_bytes")
                and capture_identity.get("sha256") == scored_identity.get("sha256")
            )
        )
        content_exact = content_exact and exact
        recorded = comparisons.get(name) if isinstance(comparisons, dict) else None
        if link.get("status") == "LINKED_EXACT" and (
            not isinstance(recorded, dict) or recorded.get("byte_exact") is not True or not exact
        ):
            raise BundleError("claimed exact scored linkage is false for {}".format(name))
    claimed_exact = bool(
        link.get("status") == "LINKED_EXACT"
        and link.get("byte_exact") is True
        and link.get("source_unchanged_during_capture") is True
    )
    if claimed_exact and not content_exact:
        raise BundleError("claimed exact scored linkage differs from live artifacts")
    return {
        "valid": claimed_exact and content_exact,
        "reason": "LINKED_EXACT" if claimed_exact and content_exact else "INVALID_LINKAGE",
        "scored": scored,
    }


def _derive_namespace(raw_record: Mapping[str, Any]) -> str:
    topics = raw_record.get("topics")
    if not isinstance(topics, list) or len(topics) != len(EXPECTED_SUFFIXES):
        raise BundleError("raw geometry topic declaration is malformed")
    candidates = []
    for suffix in EXPECTED_SUFFIXES:
        endings = "/" + suffix
        matches = [topic for topic in topics if isinstance(topic, str) and topic.endswith(endings)]
        if len(matches) != 1:
            raise BundleError("raw geometry topics do not contain exactly one {}".format(endings))
        candidates.append(matches[0][: -len(endings)])
    if len(set(candidates)) != 1:
        raise BundleError("raw geometry topics do not share one namespace")
    namespace = candidates[0]
    if not namespace or not namespace.startswith("/") or namespace.endswith("/"):
        raise BundleError("raw geometry namespace is not canonical")
    expected = [namespace + "/" + suffix for suffix in EXPECTED_SUFFIXES]
    if topics != expected:
        raise BundleError("raw geometry topics are not in the frozen semantic order")
    return namespace


def _output_path(output_dir: Path, capture_run: Path, scored_run: Optional[Path]) -> Path:
    expanded = output_dir.expanduser()
    if expanded.is_symlink():
        raise BundleError("output directory path is a symlink")
    output = expanded.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise BundleError("output directory already exists; refusing overwrite: {}".format(output))
    for run in (capture_run, scored_run):
        if run is None:
            continue
        try:
            output.relative_to(run)
        except ValueError:
            continue
        raise BundleError("qualitative output must be outside finalized estimator runs")
    output.parent.mkdir(parents=True, exist_ok=True)
    return output


def _write_bytes(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise BundleError("refusing to overwrite staged output: {}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_text(path: Path, value: str) -> None:
    _write_bytes(path, value.encode("utf-8"))


def _float_text(value: float) -> str:
    if value == 0.0:
        return "0"
    return format(value, ".12g")


def _ply_text(
    groups: Sequence[Tuple[str, Sequence[Tuple[float, float, float]]]],
    comments: Sequence[str],
) -> Tuple[str, int, Mapping[str, int]]:
    counts = {name: len(points) for name, points in groups}
    total = sum(counts.values())
    lines = ["ply", "format ascii 1.0", "comment schema {}".format(SCHEMA)]
    lines.extend("comment " + comment for comment in comments)
    lines.extend(
        (
            "element vertex {}".format(total),
            "property double x",
            "property double y",
            "property double z",
            "property uchar red",
            "property uchar green",
            "property uchar blue",
            "property uchar semantic_id",
            "end_header",
        )
    )
    for name, points in groups:
        color, semantic_id = SEMANTIC_STYLE[name]
        for point in points:
            if len(point) != 3 or not all(math.isfinite(value) for value in point):
                raise BundleError("nonfinite native geometry point")
            lines.append(
                "{} {} {} {} {} {} {}".format(
                    _float_text(point[0]),
                    _float_text(point[1]),
                    _float_text(point[2]),
                    color[0],
                    color[1],
                    color[2],
                    semantic_id,
                )
            )
    return "\n".join(lines) + "\n", total, counts


def _native_snapshot_indices(recorded: Any) -> Tuple[Mapping[str, int], Mapping[str, Any]]:
    associations = recorded.association["points_topic_associations"]
    first = max(
        int(value["unobserved_leading_poseimu_count"])
        for value in associations.values()
    )
    last = len(recorded.poses) - 1
    if first > last:
        raise geometry_v3.GeometryError("no pose has complete point-stream coverage")
    span = last - first
    selected = {
        label: first + int(math.floor(fraction * span + 0.5))
        for label, fraction in (("25", 0.25), ("50", 0.50), ("75", 0.75))
    }
    maximum_rate = -1.0
    maximum_index = first
    for index in range(max(first + 1, 1), last + 1):
        prior = recorded.poses[index - 1]
        current = recorded.poses[index]
        delta = current.timestamp - prior.timestamp
        if delta <= 0.0:
            raise geometry_v3.GeometryError("native pose timestamps are not increasing")
        rate = geometry_v3._quaternion_angle(
            prior.quaternion_xyzw, current.quaternion_xyzw
        ) / delta
        if rate > maximum_rate:
            maximum_rate = rate
            maximum_index = index
    selected["max_angular_rate"] = maximum_index
    return selected, {
        "policy": "native_poseimu_complete_point_coverage_update_ordinals",
        "first_complete_point_coverage_ordinal_zero_based": first,
        "last_poseimu_ordinal_zero_based": last,
        "max_angular_rate_rad_s": max(maximum_rate, 0.0),
        "selections": dict(selected),
    }


def _native_gap_threshold(poses: Sequence[Any]) -> float:
    differences = [
        current.timestamp - previous.timestamp
        for previous, current in zip(poses, poses[1:])
    ]
    if not differences or any(value <= 0.0 for value in differences):
        raise geometry_v3.GeometryError("native trajectory needs increasing timestamps")
    return 5.0 * float(statistics.median(differences))


def _render_native_svg(
    view_name: str,
    trajectory: Sequence[Any],
    slam: Sequence[Tuple[float, float, float]],
    msckf: Sequence[Tuple[float, float, float]],
    loop: Sequence[Tuple[float, float, float]],
    gap_threshold_s: float,
    title_prefix: str,
) -> Tuple[str, Mapping[str, Any]]:
    projection = geometry_v3.VIEW_PROJECTIONS[view_name]
    bounds = geometry_v3._view_bounds(trajectory, projection)
    min_x, max_x, min_y, max_y = bounds
    width, height = 960.0, 720.0
    left, right, top_margin, bottom = 70.0, 30.0, 75.0, 55.0
    plot_width = width - left - right
    plot_height = height - top_margin - bottom

    def number(value: float) -> str:
        return format(value, ".3f").rstrip("0").rstrip(".") or "0"

    def screen(point: Tuple[float, float]) -> Tuple[float, float]:
        return (
            left + (point[0] - min_x) * plot_width / (max_x - min_x),
            top_margin + (max_y - point[1]) * plot_height / (max_y - min_y),
        )

    segments, trajectory_clipped, gap_count = geometry_v3._trajectory_segments(
        trajectory, projection, bounds, gap_threshold_s
    )
    layers = {
        "active_slam_state_not_persistent_map": (slam, "#2366b8"),
        "transient_msckf_update_points_not_map": (msckf, "#e67e22"),
        "transient_loop_tracks_not_map": (loop, "#289b5b"),
    }
    projected: Dict[str, Sequence[Tuple[float, float]]] = {}
    clipped: Dict[str, int] = {}
    for label, (points, _color) in layers.items():
        raw = [projection(point) for point in points]
        kept = [point for point in raw if geometry_v3._inside(point, bounds)]
        projected[label] = kept
        clipped[label] = len(raw) - len(kept)

    title = "{} — {} native view".format(title_prefix, view_name)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="720" viewBox="0 0 960 720" data-frame="native-estimator">',
        "  <title>{}</title>".format(html.escape(title)),
        "  <desc>Native estimator frame. Bounds derive once from this capture trajectory. No reference or ground truth is opened; no alignment, accuracy, or cross-method visual ranking is claimed. Sparse points retain active/transient estimator semantics and are not persistent maps. Trajectory gaps are not connected.</desc>",
        '  <rect width="960" height="720" fill="#ffffff"/>',
        '  <rect x="{}" y="{}" width="{}" height="{}" fill="#fafafa" stroke="#777"/>'.format(
            number(left), number(top_margin), number(plot_width), number(plot_height)
        ),
        '  <text x="70" y="34" font-family="sans-serif" font-size="20">{}</text>'.format(
            html.escape(title)
        ),
        '  <text x="70" y="55" font-family="sans-serif" font-size="12">native estimator frame · capture-trajectory bounds · illustration-only · no GT/reference/alignment</text>',
    ]
    if not slam:
        lines.append(
            '  <text x="78" y="98" font-family="sans-serif" font-size="13" font-weight="bold" fill="#b00020">FINAL ACTIVE SLAM EMPTY — FEATURE COLLAPSE (0 points)</text>'
        )
    for label, (_points, color) in layers.items():
        commands = []
        for point in projected[label]:
            x, y = screen(point)
            commands.append("M{} {}h0.01".format(number(x), number(y)))
        if commands:
            lines.append(
                '  <path d="{}" fill="none" stroke="{}" stroke-width="1.4" stroke-linecap="round" data-semantics="{}" data-point-count="{}"/>'.format(
                    " ".join(commands), color, label, len(commands)
                )
            )
    for index, segment in enumerate(segments):
        points = " ".join(
            "{},{}".format(number(screen(point)[0]), number(screen(point)[1]))
            for point in segment
        )
        lines.append(
            '  <polyline class="native-estimate" data-segment="{}" points="{}" fill="none" stroke="#111111" stroke-width="2"/>'.format(
                index, points
            )
        )
    lines.extend(
        (
            '  <text x="70" y="690" font-family="sans-serif" font-size="11">trajectory black · final active SLAM blue · transient MSCKF orange · transient loop tracks green · none is a persistent map</text>',
            "</svg>",
        )
    )
    metadata = {
        "projection": view_name,
        "render_frame": "native_estimator_global_frame",
        "reference_or_ground_truth_opened": False,
        "alignment_fitted": False,
        "cross_method_visual_ranking_permitted": False,
        "bounds": {
            "source": "this_capture_native_trajectory_only",
            "horizontal_min": min_x,
            "horizontal_max": max_x,
            "vertical_min": min_y,
            "vertical_max": max_y,
            "margin_fraction_with_one_meter_minimum_span": 0.05,
        },
        "gap_threshold_s": gap_threshold_s,
        "trajectory_segment_count": len(segments),
        "detected_trajectory_gap_count": gap_count,
        "clipped_counts": {"trajectory": trajectory_clipped, **clipped},
        "rendered_counts": {key: len(value) for key, value in projected.items()},
    }
    return "\n".join(lines) + "\n", metadata


def _build_native_bundle(
    stage: Path,
    feature_bag: Path,
    trajectory_path: Path,
    namespace: str,
    capture_value: Mapping[str, Any],
) -> Mapping[str, Any]:
    recorded = geometry_v3.read_feature_bag(
        feature_bag, namespace=namespace, expected_frame="global"
    )
    trajectory, trajectory_format = geometry_v3.read_trajectory(trajectory_path, "tum")
    trajectory_validation = geometry_v3.validate_capture_trajectory(
        recorded.poses, trajectory
    )
    final_index = len(recorded.poses) - 1
    final_slam = geometry_v3._point_cloud_at_pose(recorded, "points_slam", final_index)
    aggregate_msckf = tuple(
        point for cloud in recorded.clouds["points_msckf"] for point in cloud
    )
    aggregate_loop = tuple(
        point for cloud in recorded.raw_loop_clouds for point in cloud
    )
    selected, selection = _native_snapshot_indices(recorded)
    trajectory_points = tuple(pose.position for pose in trajectory)
    specifications = [
        (
            "geometry/trajectory_native.ply",
            (("trajectory", trajectory_points),),
            (
                "coordinate_frame raw estimator global frame; no alignment applied",
                "semantics native capture trajectory vertices in timestamp order",
                "reference_ground_truth_opened false",
            ),
        ),
        (
            "geometry/slam_landmarks_final.ply",
            (("points_slam", final_slam),),
            (
                "coordinate_frame raw estimator global frame; no alignment applied",
                "semantics literal final active points_slam state; not a persistent map",
                "final_empty {}".format(str(not final_slam).lower()),
            ),
        ),
        (
            "geometry/msckf_update_points.ply",
            (("points_msckf", aggregate_msckf),),
            (
                "coordinate_frame raw estimator global frame; no alignment applied",
                "semantics aggregate transient last-update MSCKF points; duplicates may occur; not a map",
            ),
        ),
        (
            "geometry/loop_active_tracks_aggregate.ply",
            (("loop_feats", aggregate_loop),),
            (
                "coordinate_frame raw estimator global frame; no alignment applied",
                "semantics aggregate transient loop tracks in raw message order; not a map",
                "snapshot_association {}".format(recorded.association["loop_feats_mode"]),
            ),
        ),
    ]
    for label in ("25", "50", "75", "max_angular_rate"):
        index = selected[label]
        groups = tuple(
            (
                suffix,
                (
                    recorded.clouds[suffix][index]
                    if suffix == "loop_feats"
                    else geometry_v3._point_cloud_at_pose(recorded, suffix, index)
                ),
            )
            for suffix in ("points_slam", "points_msckf", "points_aruco", "loop_feats")
        )
        specifications.append(
            (
                "geometry/snapshots/{}.ply".format(label),
                groups,
                (
                    "coordinate_frame raw estimator global frame; no alignment applied",
                    "semantics one emitted estimator update; sparse active/transient state; not a persistent map",
                    "selection {}".format(label),
                    "poseimu_ordinal_zero_based {}".format(index),
                ),
            )
        )
    outputs: Dict[str, Any] = {}
    for relative, groups, comments in specifications:
        text, count, semantic_counts = _ply_text(groups, comments)
        path = stage / relative
        _write_text(path, text)
        outputs[relative] = {
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
            "point_count": count,
            "semantic_counts": semantic_counts,
        }
    gap_threshold = _native_gap_threshold(trajectory)
    views: Dict[str, Any] = {}
    title = "{} {} {}".format(
        capture_value.get("dataset"), capture_value.get("sequence"), capture_value.get("system")
    )
    for view_name in ("top", "side", "oblique"):
        svg, metadata = _render_native_svg(
            view_name,
            trajectory,
            final_slam,
            aggregate_msckf,
            aggregate_loop,
            gap_threshold,
            title,
        )
        relative = "figures/{}.svg".format(view_name)
        path = stage / relative
        _write_text(path, svg)
        outputs[relative] = {
            "sha256": _sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        views[view_name] = metadata
    return {
        "trajectory_format": trajectory_format,
        "trajectory_validation": trajectory_validation,
        "recording": {
            "namespace": recorded.namespace,
            "topics": recorded.topics,
            "message_types": recorded.message_types,
            "message_counts": recorded.record_counts,
            "association": recorded.association,
        },
        "selection": selection,
        "final_active_slam_empty": not bool(final_slam),
        "outputs": outputs,
        "views": views,
        "reference_access": {
            "opened": False,
            "alignment_fitted": False,
            "cross_method_visual_ranking_permitted": False,
        },
    }


def _failure_svg(capture: Mapping[str, Any], flags: Sequence[str]) -> str:
    value = capture["value"]
    title = "{} / {} / {}".format(
        value.get("dataset", "UNKNOWN"),
        value.get("sequence", "UNKNOWN"),
        value.get("system", "UNKNOWN"),
    )
    flag_text = " · ".join(flags) if flags else "QUALITATIVE_UNASSESSABLE"
    return "\n".join(
        (
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="540" viewBox="0 0 960 540" data-kind="qualitative-failure-tile">',
            "  <title>{}</title>".format(html.escape(title)),
            "  <desc>Explicit terminal qualitative evidence tile. No geometry is fabricated.</desc>",
            '  <rect width="960" height="540" fill="#fff8f8"/>',
            '  <rect x="30" y="30" width="900" height="480" fill="#ffffff" stroke="#b00020" stroke-width="3"/>',
            '  <text x="65" y="105" font-family="sans-serif" font-size="26" font-weight="bold" fill="#b00020">QUALITATIVE UNASSESSABLE</text>',
            '  <text x="65" y="155" font-family="sans-serif" font-size="18">{}</text>'.format(html.escape(title)),
            '  <text x="65" y="205" font-family="monospace" font-size="16">capture status: {}</text>'.format(html.escape(str(value.get("status")))),
            '  <text x="65" y="255" font-family="monospace" font-size="14">{}</text>'.format(html.escape(flag_text)),
            '  <text x="65" y="325" font-family="sans-serif" font-size="15">No point, pose, map, or alignment was invented.</text>',
            '  <text x="65" y="355" font-family="sans-serif" font-size="15">Consult the checksum-bound capture result and raw artifacts for the retained outcome.</text>',
            "</svg>",
            "",
        )
    )


def _stage_failure(stage: Path, capture: Mapping[str, Any], flags: Sequence[str]) -> Mapping[str, Any]:
    relative = "figures/failure.svg"
    path = stage / relative
    _write_text(path, _failure_svg(capture, flags))
    return {
        "relative_path": relative,
        "sha256": _sha256_file(path),
        "size_bytes": path.stat().st_size,
        "flags": list(flags),
    }


def _scan_stage(stage: Path) -> Mapping[str, Any]:
    artifacts: Dict[str, Any] = {}
    for path in sorted(stage.rglob("*")):
        if path.is_symlink():
            raise BundleError("staged output contains a symlink")
        if path.is_file():
            relative = path.relative_to(stage).as_posix()
            artifacts[relative] = {
                "size_bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
    return artifacts


def _postflight_identities(records: Mapping[str, Optional[Mapping[str, Any]]]) -> Mapping[str, bool]:
    verified: Dict[str, bool] = {}
    for label, record in records.items():
        if record is None:
            continue
        path_value = record.get("path")
        if not isinstance(path_value, str) or not path_value:
            raise BundleError("postflight {} identity has no path".format(label))
        observed = _file_identity(Path(path_value))
        if not _identity_equal(record, observed):
            raise BundleError("{} identity changed during qualitative processing".format(label))
        verified[label] = True
    return verified


def _publish(stage: Path, output: Path) -> None:
    if output.exists() or output.is_symlink():
        raise BundleError("output directory already exists; refusing overwrite")
    paths = [
        path
        for path in sorted(stage.rglob("*"))
        if path.is_file()
        and path not in (
            stage / "qualitative_manifest.json",
            stage / "SHA256SUMS",
        )
    ]
    paths.extend((stage / "qualitative_manifest.json", stage / "SHA256SUMS"))
    output.mkdir(mode=0o750)
    try:
        for source in paths:
            relative = source.relative_to(stage)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                raise BundleError("refusing to overwrite qualitative artifact")
            os.link(str(source), str(destination))
        descriptor = os.open(str(output), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException as exc:
        # ``output`` was created exclusively above and is owned by this call.
        # A failed publication has no completion marker and is removed in full
        # so a campaign resume never encounters an ambiguous partial bundle.
        cleanup_error: Optional[BaseException] = None
        try:
            if output.is_symlink() or not output.is_dir():
                raise BundleError("owned partial output changed type during rollback")
            shutil.rmtree(str(output))
        except BaseException as rollback_exc:
            cleanup_error = rollback_exc
        if cleanup_error is not None:
            raise BundleError(
                "qualitative publication failed and rollback failed: {}; {}".format(
                    exc, cleanup_error
                )
            ) from exc
        if isinstance(exc, BundleError):
            raise
        raise BundleError("qualitative publication failed: {}".format(exc)) from exc


def build_bundle(
    capture_result_path: Path,
    matrix_path: Path,
    output_dir: Path,
) -> Mapping[str, Any]:
    """Validate one finalized capture and publish its qualitative evidence."""

    capture = _validate_sequence_result(capture_result_path, "capture")
    matrix = _read_matrix(matrix_path)
    row = _select_matrix_row(matrix, capture)
    selected_inputs = _validate_selected_inputs(capture, matrix, row)
    linkage = _validate_scored_linkage(capture)
    scored_run = linkage["scored"]["run_dir"] if linkage["scored"] is not None else None
    output = _output_path(output_dir, capture["run_dir"], scored_run)

    raw_geometry = _artifact(
        capture, "raw_geometry", "geometry/feature_stream.bag"
    )
    trajectory = _artifact(capture, "tum", "trajectory/estimate_raw.tum")
    artifacts = capture["value"].get("artifacts", {})
    raw_record = artifacts.get("raw_geometry") if isinstance(artifacts, dict) else None
    if not isinstance(raw_record, dict):
        raise BundleError("capture raw_geometry artifact record is absent")
    active_identity = raw_record.get("active_identity")
    if active_identity is not None:
        active_path = capture["run_dir"] / "geometry/feature_stream.bag.active"
        observed_active = _file_identity(active_path)
        _validate_recorded_identity(active_identity, observed_active, "active geometry bag")
    namespace = _derive_namespace(raw_record)

    capability = row.get("ground_truth", {}).get("capability")
    if capability not in ("full_trajectory", "embedded_partial_intervals"):
        raise BundleError("selected matrix row has unsupported GT capability")
    if capability == "embedded_partial_intervals":
        gt_record = row.get("ground_truth")
        bag_record = row.get("bag")
        if not isinstance(gt_record, dict) or not isinstance(bag_record, dict):
            raise BundleError("partial-reference matrix records are malformed")
        if (
            gt_record.get("canonical_path") != bag_record.get("path")
            or gt_record.get("bytes") != bag_record.get("bytes")
            or gt_record.get("sha256") != bag_record.get("sha256")
            or gt_record.get("source_topic") != "/vrpn_client/raw_transform"
        ):
            raise BundleError("partial-reference matrix declaration is inconsistent")

    flags = []
    status = str(capture["value"].get("status"))
    science_failure = status not in ELIGIBLE_CAPTURE_STATUSES
    if science_failure:
        flags.append(status)
    if not capture["close"]["geometry_access_permitted"]:
        flags.append("ESTIMATOR_CLOSURE_NOT_PROVEN")
    if not selected_inputs["matrix_bound"]:
        flags.append("MATRIX_BINDING_NOT_ESTABLISHED")
    if not selected_inputs["bag_bound"]:
        flags.append("BAG_BINDING_NOT_ESTABLISHED")
    if not linkage["valid"]:
        flags.append(str(linkage["reason"]))
    if raw_geometry is None:
        flags.append("MAP_NOT_EXPOSED")
    if active_identity is not None:
        flags.append("RAW_GEOMETRY_NOT_CLOSED")
    if trajectory is None:
        flags.append("NO_VALID_CAPTURE_TRAJECTORY")

    can_open_geometry = bool(
        capture["close"]["geometry_access_permitted"]
        and selected_inputs["matrix_bound"]
        and selected_inputs["bag_bound"]
        and linkage["valid"]
        and raw_geometry is not None
        and active_identity is None
        and trajectory is not None
    )
    processed_mode = "FAILURE_TILE_ONLY"
    processing: Optional[Mapping[str, Any]] = None
    failure_tile: Optional[Mapping[str, Any]] = None
    ground_truth_identity: Optional[Mapping[str, Any]] = None

    with tempfile.TemporaryDirectory(prefix=".cross-geometry-", dir=str(output.parent)) as temporary:
        stage = Path(temporary)
        _write_bytes(stage / "evidence/capture_sequence_result.json", capture["payload"])
        _write_bytes(stage / "evidence/campaign_matrix.yaml", matrix["payload"])
        if linkage["scored"] is not None:
            _write_bytes(
                stage / "evidence/scored_sequence_result.json",
                linkage["scored"]["payload"],
            )

        if can_open_geometry:
            try:
                if capability == "full_trajectory":
                    ground_truth_identity = _matrix_identity(
                        row.get("ground_truth"), "selected ground truth"
                    )
                    v3_dir = stage / "validated_v3"
                    v3_manifest = geometry_v3.build_bundle(
                        feature_bag=Path(str(raw_geometry["path"])),
                        capture_trajectory_path=Path(str(trajectory["path"])),
                        ground_truth_path=Path(str(ground_truth_identity["path"])),
                        run_dir=v3_dir,
                        namespace=namespace,
                        expected_frame="global",
                        trajectory_format="tum",
                        display_dataset_label={
                            "euroc_mav": "EuRoC MAV",
                            "tum_vi": "TUM-VI",
                            "kaist_vio": "KAIST-VIO",
                        }[str(capture["value"]["dataset"])],
                    )
                    if v3_manifest.get("schema") != V3_SCHEMA or v3_manifest.get("status") != "COMPLETE":
                        raise BundleError("validated v3 geometry bundle returned an invalid manifest")
                    processed_mode = "REFERENCE_BACKED_VALIDATED_V3"
                    processing = {
                        "delegate_schema": V3_SCHEMA,
                        "delegate_manifest": "validated_v3/geometry_manifest.json",
                        "delegate_checksums": "validated_v3/SHA256SUMS",
                        "namespace": namespace,
                        "ground_truth_source": "exact_selected_campaign_matrix_row",
                        "ground_truth_identity": ground_truth_identity,
                    }
                else:
                    native = _build_native_bundle(
                        stage,
                        Path(str(raw_geometry["path"])),
                        Path(str(trajectory["path"])),
                        namespace,
                        capture["value"],
                    )
                    processed_mode = "NATIVE_ESTIMATOR_FRAME_PARTIAL_REFERENCE"
                    processing = native
            except geometry_v3.GeometryError as exc:
                # A closed capture can legitimately expose no usable sparse
                # geometry (notably the untouched stock U0 arm), or expose a
                # stream whose point/topic semantics cannot support the
                # deterministic atlas.  That is qualitative evidence, not a
                # campaign-infrastructure failure.  Preserve the raw bag and
                # publish an explicit unassessable tile so later cells still
                # run; provenance/linkage errors above remain fatal.
                flags.extend(("GEOMETRY_VALIDATION_FAILED", type(exc).__name__))
                processing = {"validation_error": str(exc)}
                processed_mode = "FAILURE_TILE_ONLY"

        if processed_mode == "FAILURE_TILE_ONLY" or science_failure:
            failure_tile = _stage_failure(stage, capture, tuple(dict.fromkeys(flags)))

        postflight = _postflight_identities(
            {
                "capture_sequence_result": capture["identity"],
                "campaign_matrix": matrix["identity"],
                "selected_bag": selected_inputs["bag_identity"],
                "raw_feature_stream": raw_geometry,
                "capture_trajectory": trajectory,
                "ground_truth": ground_truth_identity,
                "scored_sequence_result": (
                    linkage["scored"]["identity"]
                    if linkage["scored"] is not None
                    else None
                ),
            }
        )

        artifacts_before_manifest = _scan_stage(stage)
        manifest: MutableMapping[str, Any] = {
            "schema": SCHEMA,
            "status": (
                "FAILURE_EVIDENCE_COMPLETE"
                if processed_mode == "FAILURE_TILE_ONLY"
                else "GEOMETRY_EVIDENCE_COMPLETE"
            ),
            "bundle_mode": processed_mode,
            "protocol_id": capture["value"].get("protocol_id"),
            "run_id": capture["value"].get("run_id"),
            "dataset": capture["value"].get("dataset"),
            "sequence": capture["value"].get("sequence"),
            "system": capture["value"].get("system"),
            "matrix_order": row.get("order"),
            "capture_status": status,
            "qualitative_review_status": (
                "QUALITATIVE_UNASSESSABLE"
                if processed_mode == "FAILURE_TILE_ONLY"
                else "PENDING_HUMAN_REVIEW"
            ),
            "qualitative_flags": list(dict.fromkeys(flags)),
            "png_policy": PNG_POLICY,
            "png_files_produced": 0,
            "inputs": {
                "capture_sequence_result": capture["identity"],
                "capture_checksum_set": capture["checksums"],
                "campaign_matrix": matrix["identity"],
                "selected_bag": selected_inputs["bag_identity"],
                "raw_feature_stream": raw_geometry,
                "capture_trajectory": trajectory,
                "ground_truth": ground_truth_identity,
                "scored_sequence_result": (
                    linkage["scored"]["identity"] if linkage["scored"] is not None else None
                ),
            },
            "validation": {
                "capture_directory_live_checksum_membership_exact": True,
                "capture_close": capture["close"],
                "matrix_binding": selected_inputs,
                "scored_linkage": {
                    "valid": linkage["valid"], "reason": linkage["reason"]
                },
                "raw_namespace": namespace,
                "raw_semantics_opened_and_validated": processed_mode
                != "FAILURE_TILE_ONLY",
                "live_input_identities_stable_through_postflight": postflight,
                "output_is_outside_finalized_estimator_runs": True,
            },
            "ground_truth_capability": capability,
            "reference_policy": (
                {
                    "opened": True,
                    "source": "exact selected full_trajectory matrix identity",
                    "delegate_alignment": "v3 evo-compatible SE3 no-scale",
                }
                if processed_mode == "REFERENCE_BACKED_VALIDATED_V3"
                else {
                    "opened": False,
                    "alignment_fitted": False,
                    "cross_method_visual_ranking_permitted": False,
                }
            ),
            "processing": processing,
            "failure_tile": failure_tile,
            "artifact_policy": {
                "append_only_new_output_directory": True,
                "no_overwrite": True,
                "capture_and_scored_runs_not_modified": True,
                "manifest_and_checksums_published_last": True,
                "raw_feature_stream_is_authoritative": True,
            },
            "artifacts": artifacts_before_manifest,
            "script": {
                "path": "scripts/icra27/cross_dataset_geometry_bundle.py",
                "sha256": _sha256_file(Path(__file__).resolve()),
            },
        }
        manifest_payload = (
            json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        _write_bytes(stage / "qualitative_manifest.json", manifest_payload)
        checksum_artifacts = _scan_stage(stage)
        checksum_text = "".join(
            "{}  {}\n".format(checksum_artifacts[name]["sha256"], name)
            for name in sorted(checksum_artifacts)
        )
        _write_text(stage / "SHA256SUMS", checksum_text)
        _publish(stage, output)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--capture-result", required=True, type=Path)
    parser.add_argument("--matrix-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = build_bundle(
            capture_result_path=args.capture_result,
            matrix_path=args.matrix_file,
            output_dir=args.output_dir,
        )
    except (BundleError, OSError, ValueError, json.JSONDecodeError) as exc:
        print("CROSS_DATASET_GEOMETRY_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "schema": manifest["schema"],
                "status": manifest["status"],
                "bundle_mode": manifest["bundle_mode"],
                "dataset": manifest["dataset"],
                "sequence": manifest["sequence"],
                "system": manifest["system"],
                "output_dir": str(args.output_dir.expanduser().resolve(strict=True)),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
