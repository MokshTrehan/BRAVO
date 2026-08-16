#!/usr/bin/python3.8
"""Validate and publish the final CDSC-1R3 comparison report.

This is a read-only consumer of a completed campaign artifact tree.  It fails
closed on missing, duplicate, moved, or checksum-invalid evidence, but it does
not turn a valid estimator failure into an infrastructure failure.  Passage,
accuracy, process health, historical determinism, and qualitative review stay
as separate result dimensions.
"""

from __future__ import annotations

import argparse
import csv
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_campaign as campaign  # noqa: E402


SCHEMA = "schurvio.icra27.cross_dataset.aggregate.v1"
PAIR_SCHEMA = "schurvio.icra27.cross_dataset.pair_result.v1"
GEOMETRY_SCHEMA = "schurvio.icra27.cross_dataset_geometry_bundle.v1"
MECHANISM_SCHEMA = "schurvio.icra27.cross_dataset.kaist_rotation_post_pair.v1"
PROTOCOL_ID = "CDSC-1R3"
SYSTEMS = ("U0", "S1")
LANES = ("scored", "capture")
DATASETS = ("euroc_mav", "tum_vi", "kaist_vio")
METRICS = (
    "ate_translation_rmse_m",
    "rpe_translation_rmse_1m_m",
    "rpe_rotation_rmse_1m_deg",
)
PASSAGE_COMPLETE_STATUSES = frozenset(("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT"))
PNG_POLICY = "PNG_NOT_PRODUCED_NO_APPROVED_DETERMINISTIC_RASTERIZER"
PAIR_EVALUATOR = SCRIPT_DIR / "cross_dataset_pair_evaluator.py"
PAIR_MATH_CORE = SCRIPT_DIR / "kaist_pair_evaluator.py"
EXPECTED_PAIR_RUNTIME_PINS_SHA256 = (
    "3e09ca65d06eb79e7c6c0c30361bbf189620ce5741771a0d9074ee09a8653f14"
)
U0_SOURCE_COMMIT = "69488123ed9362dd44b6f28e7f4680abbff1442b"
U0_SOURCE_TREE = "12ab1c94ccae78ad50fa7376f47f0e55e2672257"
S1_SOURCE_COMMIT = "2751bcdc0fae25c993b3224dc5fa40aaab6571d7"
S1_SOURCE_TREE = "b3f9191b8bce3852ebff71e1ebf180181bcd6d05"
ACCURACY_MINIMUM_PAIRS = 8
ACCURACY_MEDIAN_LIMIT = 0.10
ACCURACY_INDIVIDUAL_LIMIT = 0.20
PAIR_QUATERNION_NORM_MAX_ERROR = 5.0e-4
SHA256_RE = campaign.SHA256_RE


class AggregateError(RuntimeError):
    """A final evidence tree cannot support a CDSC-1R3 publication."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    if path.is_symlink():
        raise AggregateError("refusing symlink identity: {}".format(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise AggregateError("file does not resolve: {}".format(path)) from exc
    if not resolved.is_file():
        raise AggregateError("not a regular file: {}".format(resolved))
    before = resolved.stat()
    digest = sha256_file(resolved)
    after = resolved.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise AggregateError("file changed while hashing: {}".format(resolved))
    return {"path": str(resolved), "size_bytes": after.st_size, "sha256": digest}


def _strict_json_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AggregateError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_json(path: Path, label: str) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    identity = file_identity(path)
    try:
        payload = Path(identity["path"]).read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                AggregateError("nonfinite JSON token {}".format(token))
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AggregateError("cannot parse {}: {}".format(label, path)) from exc
    if not isinstance(value, dict):
        raise AggregateError("{} is not a JSON object".format(label))
    if hashlib.sha256(payload).hexdigest() != identity["sha256"]:
        raise AggregateError("{} changed while it was read".format(label))
    return value, identity


def _safe_relative(text: str) -> Path:
    if not text or "\n" in text or "\r" in text:
        raise AggregateError("unsafe empty/checksum path")
    value = Path(text)
    if value.is_absolute() or ".." in value.parts or value == Path("."):
        raise AggregateError("unsafe checksum path: {}".format(text))
    return value


def verify_checksum_directory(directory: Path, required_manifest: Path) -> Mapping[str, Any]:
    """Require an exact live-file membership closure under SHA256SUMS."""

    if directory.is_symlink():
        raise AggregateError("checksummed directory is a symlink")
    try:
        root = directory.resolve(strict=True)
    except OSError as exc:
        raise AggregateError("checksummed directory is absent: {}".format(directory)) from exc
    if not root.is_dir():
        raise AggregateError("checksummed path is not a directory: {}".format(root))
    checksum_path = root / "SHA256SUMS"
    checksum_identity = file_identity(checksum_path)
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AggregateError("checksum set is not strict ASCII") from exc
    if not lines:
        raise AggregateError("checksum set is empty")
    entries: Dict[str, str] = {}
    previous: Optional[str] = None
    for line in lines:
        if "  " not in line:
            raise AggregateError("malformed checksum line")
        digest, relative_text = line.split("  ", 1)
        if SHA256_RE.fullmatch(digest) is None:
            raise AggregateError("malformed SHA-256 in checksum set")
        relative = _safe_relative(relative_text)
        name = relative.as_posix()
        if name in entries:
            raise AggregateError("duplicate checksum entry: {}".format(name))
        if previous is not None and name <= previous:
            raise AggregateError("checksum entries are not strictly path-sorted")
        previous = name
        candidate = root / relative
        if candidate.is_symlink():
            raise AggregateError("checksummed artifact is a symlink: {}".format(name))
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise AggregateError("checksummed artifact escapes or is absent: {}".format(name)) from exc
        if not resolved.is_file() or sha256_file(resolved) != digest:
            raise AggregateError("checksummed artifact mismatch: {}".format(name))
        entries[name] = digest
    actual: Set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise AggregateError("checksummed tree contains a symlink: {}".format(path))
        if path.is_file() and path != checksum_path:
            actual.add(path.relative_to(root).as_posix())
    if actual != set(entries):
        raise AggregateError(
            "checksum membership mismatch: missing={}, extra={}".format(
                sorted(set(entries) - actual), sorted(actual - set(entries))
            )
        )
    manifest = required_manifest.resolve(strict=True)
    try:
        relative_manifest = manifest.relative_to(root).as_posix()
    except ValueError as exc:
        raise AggregateError("required manifest is outside checksum directory") from exc
    if relative_manifest not in entries:
        raise AggregateError("required manifest is not checksum-bound")
    return {
        **checksum_identity,
        "entry_count": len(entries),
        "membership_exact": True,
        "required_manifest": relative_manifest,
    }


def assert_exact_paths(found: Iterable[Path], expected: Iterable[Path], label: str) -> None:
    found_strings = [str(path.absolute()) for path in found]
    if len(found_strings) != len(set(found_strings)):
        raise AggregateError("{} contains duplicate path observations".format(label))
    expected_strings = {str(path.absolute()) for path in expected}
    observed = set(found_strings)
    if observed != expected_strings:
        raise AggregateError(
            "{} canonical file set mismatch: missing={}, unexpected={}".format(
                label, sorted(expected_strings - observed), sorted(observed - expected_strings)
            )
        )


def _identity_projection(record: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(record, dict):
        raise AggregateError("{} identity is absent".format(label))
    path_value = record.get("path", record.get("canonical_path"))
    size = record.get("size_bytes", record.get("bytes"))
    digest = record.get("sha256")
    if not isinstance(path_value, str) or not path_value:
        raise AggregateError("{} identity path is malformed".format(label))
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise AggregateError("{} identity size is malformed".format(label))
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise AggregateError("{} identity digest is malformed".format(label))
    try:
        normalized_path = str(Path(path_value).resolve(strict=True))
    except OSError as exc:
        raise AggregateError("{} identity path does not resolve".format(label)) from exc
    return {"path": normalized_path, "size_bytes": size, "sha256": digest}


def _require_same_identity(left: Any, right: Any, label: str) -> Mapping[str, Any]:
    left_value = _identity_projection(left, label + " left")
    right_value = _identity_projection(right, label + " right")
    if left_value != right_value:
        raise AggregateError("{} identity mismatch".format(label))
    return left_value


def _revalidate_live_identity(
    record: Any, label: str, cache: MutableMapping[str, Mapping[str, Any]]
) -> Mapping[str, Any]:
    expected = _identity_projection(record, label)
    path = expected["path"]
    if path not in cache:
        cache[path] = file_identity(Path(path))
    if cache[path] != expected:
        raise AggregateError("{} live bytes changed after the run".format(label))
    return expected


def _finite_number(value: Any, label: str, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AggregateError("{} is not numeric".format(label))
    result = float(value)
    if not math.isfinite(result) or (minimum is not None and result < minimum):
        raise AggregateError("{} is outside its finite domain".format(label))
    return result


def _artifact_identity(result: Mapping[str, Any], name: str) -> Optional[Mapping[str, Any]]:
    artifacts = result.get("artifacts")
    record = artifacts.get(name) if isinstance(artifacts, dict) else None
    if not isinstance(record, dict):
        raise AggregateError("run artifact record {} is absent".format(name))
    identity = record.get("identity")
    if identity is None:
        return None
    return _identity_projection(identity, "run artifact {}".format(name))


def _runner_for_row(row: campaign.MatrixRow) -> Path:
    return (
        SCRIPT_DIR / "cross_dataset_kaist_trial.py"
        if row.dataset == "kaist_vio"
        else SCRIPT_DIR / "cross_dataset_trial.py"
    )


def _validate_run_bindings(
    result: campaign.ValidatedResult,
    row: campaign.MatrixRow,
    lane: str,
    system: str,
    protocol_identity: Mapping[str, Any],
    matrix_identity: Mapping[str, Any],
    runner_identity: Mapping[str, Any],
    live_identity_cache: MutableMapping[str, Mapping[str, Any]],
) -> None:
    value = result.value
    inputs = value.get("inputs")
    if not isinstance(inputs, dict):
        raise AggregateError("sequence result input bindings are absent")
    _require_same_identity(inputs.get("protocol"), protocol_identity, "run protocol")
    _require_same_identity(inputs.get("matrix"), matrix_identity, "run matrix")
    _require_same_identity(inputs.get("runner"), runner_identity, "run runner")
    _require_same_identity(inputs.get("bag"), row.bag, "run selected bag")
    if inputs.get("ground_truth") is not None:
        raise AggregateError("ground truth entered an estimator-facing run manifest")
    after = value.get("input_identities_after")
    if isinstance(after, dict) and isinstance(after.get("bag"), dict):
        _require_same_identity(after["bag"], row.bag, "run postflight bag")
    if value.get("protocol_identity") is not None:
        _require_same_identity(value["protocol_identity"], protocol_identity, "protocol alias")
    if value.get("matrix_identity") is not None:
        _require_same_identity(value["matrix_identity"], matrix_identity, "matrix alias")
    for name in (
        "protocol",
        "matrix",
        "runner",
        "config",
        "launch",
        "binary",
        "converter",
        "pairing_census_tool",
        "runtime_identity_validator",
    ):
        _revalidate_live_identity(
            inputs.get(name), "run input {}".format(name), live_identity_cache
        )
    dependencies = inputs.get("config_dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise AggregateError("run configuration/calibration dependency set is absent")
    for path_text, record in sorted(dependencies.items()):
        if not isinstance(path_text, str) or not path_text:
            raise AggregateError("run configuration dependency key is malformed")
        observed = _revalidate_live_identity(
            record, "run configuration dependency {}".format(path_text), live_identity_cache
        )
        if observed["path"] != str(Path(path_text).resolve(strict=True)):
            raise AggregateError("run configuration dependency key/path mismatch")
    runtime = value.get("runtime_identity")
    if not isinstance(runtime, dict) or runtime.get("system") != system:
        raise AggregateError("run runtime identity is absent or failed")
    _require_same_identity(
        runtime.get("declared_binary"), inputs.get("binary"), "declared/input executable"
    )
    source = runtime.get("source")
    if not isinstance(source, dict) or source.get("dirty") is not False:
        raise AggregateError("run source identity is absent or dirty")
    if system == "U0" and (
        source.get("head_sha") != U0_SOURCE_COMMIT
        or source.get("head_tree") != U0_SOURCE_TREE
    ):
        raise AggregateError("U0 source commit/tree drift")
    pinned = runtime.get("pinned_runtime")
    if (
        not isinstance(pinned, dict)
        or pinned.get("status") != "PASS"
        or pinned.get("system") != system
    ):
        raise AggregateError("pinned runtime identity is absent or failed")
    runtime_executable = _revalidate_live_identity(
        pinned.get("executable"), "runtime executable", live_identity_cache
    )
    _require_same_identity(runtime_executable, inputs.get("binary"), "runtime/input executable")
    libraries = pinned.get("critical_local_dependencies")
    if not isinstance(libraries, dict) or not libraries:
        raise AggregateError("runtime critical library identity set is absent")
    for soname, record in sorted(libraries.items()):
        if not isinstance(soname, str) or not soname:
            raise AggregateError("runtime critical library name is malformed")
        _revalidate_live_identity(
            record, "runtime critical library {}".format(soname), live_identity_cache
        )
    if system == "S1":
        compiled = runtime.get("compiled_source_snapshot")
        if (
            not isinstance(compiled, dict)
            or compiled.get("source_commit") != S1_SOURCE_COMMIT
            or compiled.get("source_tree") != S1_SOURCE_TREE
            or compiled.get("all_compiled_inputs_match_live_bytes") is not True
        ):
            raise AggregateError("S1 compiled source snapshot identity drift")
        _revalidate_live_identity(
            compiled.get("configure_provenance"),
            "S1 configure provenance",
            live_identity_cache,
        )
        provenance = pinned.get("build_provenance")
        if not isinstance(provenance, dict):
            raise AggregateError("S1 runtime build provenance is absent")
        _revalidate_live_identity(
            provenance.get("file"), "S1 build provenance file", live_identity_cache
        )
        runtime_artifacts = provenance.get("runtime_artifacts")
        if not isinstance(runtime_artifacts, list) or not runtime_artifacts:
            raise AggregateError("S1 compiled runtime artifact set is absent")
        for index, record in enumerate(runtime_artifacts):
            _revalidate_live_identity(
                record,
                "S1 compiled runtime artifact {}".format(index),
                live_identity_cache,
            )
    tools = runtime.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise AggregateError("runtime resolution tool identities are absent")
    for name, record in sorted(tools.items()):
        _revalidate_live_identity(
            record, "runtime resolution tool {}".format(name), live_identity_cache
        )
    passage = value.get("passage")
    if not isinstance(passage, dict) or not isinstance(passage.get("complete"), bool):
        raise AggregateError("sequence passage record is malformed")
    if lane == "scored" and passage["complete"] and value.get("status") not in PASSAGE_COMPLETE_STATUSES:
        raise AggregateError("non-complete status claims passage completion")
    if lane == "scored" and bool(value.get("accuracy_eligible")) != bool(
        value.get("status") in PASSAGE_COMPLETE_STATUSES and passage["complete"]
    ):
        raise AggregateError("scored accuracy eligibility disagrees with passage/status")
    if system == "S1" and value.get("status") == "COMPLETED_WITH_TEARDOWN_DEFECT":
        raise AggregateError("the predeclared teardown defect is U0-only")


def _validate_capture_linkage(
    capture_value: Mapping[str, Any], scored: campaign.ValidatedResult
) -> Mapping[str, Any]:
    link = capture_value.get("scored_linkage")
    if not isinstance(link, dict):
        raise AggregateError("capture scored linkage is absent")
    for key in ("sequence_result", "sequence_result_after"):
        _require_same_identity(link.get(key), scored.identity, "capture {}".format(key))
    if link.get("source_unchanged_during_capture") is not True:
        raise AggregateError("capture scored source changed during replay")
    comparisons = link.get("artifact_comparisons")
    if not isinstance(comparisons, dict):
        raise AggregateError("capture artifact comparisons are absent")
    exact_values: List[bool] = []
    for name in ("state", "deviation", "tum"):
        item = comparisons.get(name)
        if not isinstance(item, dict) or not isinstance(item.get("byte_exact"), bool):
            raise AggregateError("capture/scored {} comparison is malformed".format(name))
        expected = _artifact_identity(scored.value, name)
        observed = _artifact_identity(capture_value, name)
        exact = bool(
            (expected is None and observed is None)
            or (
                isinstance(expected, dict)
                and isinstance(observed, dict)
                and expected["size_bytes"] == observed["size_bytes"]
                and expected["sha256"] == observed["sha256"]
            )
        )
        if item.get("byte_exact") is not exact:
            raise AggregateError("capture/scored {} comparison is not truthful".format(name))
        if item.get("expected_sha256") != (expected.get("sha256") if isinstance(expected, dict) else None):
            raise AggregateError("capture/scored {} expected digest drift".format(name))
        if item.get("observed_sha256") != (observed.get("sha256") if isinstance(observed, dict) else None):
            raise AggregateError("capture/scored {} observed digest drift".format(name))
        exact_values.append(exact)
    invalid_linkage = capture_value.get("evidence_validity") == "CAPTURE_LINK_INVALID"
    if invalid_linkage:
        facts = capture_value.get("outcome_facts")
        checks = capture_value.get("checks")
        if (
            capture_value.get("status")
            not in (set(campaign.RETAINED_ALGORITHM_STATUSES) | {"INVALID_LINKAGE"})
            or link.get("status") != "OUTPUT_MISMATCH"
            or link.get("byte_exact") is not False
            or all(exact_values)
            or not isinstance(facts, dict)
            or facts.get("capture_closed") is not True
            or facts.get("linkage_valid") is not False
            or facts.get("teardown_ok") is not True
            or facts.get("runtime_contract_valid") is not True
            or not isinstance(checks, dict)
            or checks.get("runtime_inputs_unchanged") is not True
        ):
            raise AggregateError("capture linkage mismatch is not narrowly proven/retained")
    elif (
        capture_value.get("evidence_validity") != "VALID"
        or link.get("status") != "LINKED_EXACT"
        or link.get("byte_exact") is not True
        or not all(exact_values)
    ):
        raise AggregateError("capture does not retain exact scored linkage")
    return link


def _cell_record(
    row: campaign.MatrixRow,
    lane: str,
    system: str,
    result: campaign.ValidatedResult,
) -> Dict[str, Any]:
    value = result.value
    passage = value.get("passage", {})
    raw_geometry = _artifact_identity(value, "raw_geometry") if lane == "capture" else None
    return {
        "lane": lane,
        "order": row.order,
        "key": campaign.row_key(row),
        "dataset": row.dataset,
        "sequence": row.sequence,
        "system": system,
        "run_id": value.get("run_id"),
        "result": result.identity,
        "checksum_set": None,
        "evidence_validity": value.get("evidence_validity"),
        "status": value.get("status"),
        "passage_complete": passage.get("complete"),
        "passage_eligible": passage.get("eligible"),
        "passage_reason": passage.get("reason"),
        "initialization_timestamp_s": passage.get("initialization_timestamp_s"),
        "initialization_delay_s": passage.get("initialization_delay_s"),
        "tail_gap_s": passage.get("tail_gap_s"),
        "maximum_state_gap_s": passage.get("max_state_gap_s"),
        "supported_input_gap_count": passage.get("supported_input_gap_count"),
        "strict_process_health": value.get("strict_process_health"),
        "accuracy_eligible": value.get("accuracy_eligible"),
        "qualitative_eligible": value.get("qualitative_eligible"),
        "outcome_facts": value.get("outcome_facts"),
        "recovery": value.get("robustness_mechanism", {
            "status": "DEFAULT_OFF" if row.dataset != "kaist_vio" and system == "S1" else "NOT_APPLICABLE",
            "checks": {
                "recovery_default_off": value.get("checks", {}).get("recovery_default_off"),
                "recovery_runtime_event_count_zero": value.get("checks", {}).get(
                    "recovery_runtime_event_count_zero"
                ),
            },
        }),
        "historical_kaist_determinism": value.get("historical_determinism"),
        "capture_linkage": value.get("scored_linkage") if lane == "capture" else None,
        "raw_geometry": raw_geometry,
        "qualitative": None,
    }


def _passage_label_from_facts(
    evidence_complete: bool,
    has_u0_complete_s1_incomplete: bool,
    dataset_counts: Mapping[str, Mapping[str, int]],
    overall: Mapping[str, int],
) -> str:
    if not evidence_complete:
        return "PASSAGE_UNASSESSABLE"
    if has_u0_complete_s1_incomplete:
        return "PASSAGE_REGRESSION"
    if (
        all(dataset_counts[name]["S1"] >= dataset_counts[name]["U0"] for name in DATASETS)
        and overall["S1"] > overall["U0"]
    ):
        return "PASSAGE_DOMINANT"
    if (
        all(dataset_counts[name]["S1"] == dataset_counts[name]["U0"] for name in DATASETS)
        and overall["S1"] == overall["U0"]
    ):
        return "PASSAGE_PARITY"
    return "PASSAGE_MIXED"


def mechanical_passage_summary(
    scored_cells: Sequence[Mapping[str, Any]], expected_pair_count: int = 25
) -> Mapping[str, Any]:
    grouped: Dict[int, Dict[str, Mapping[str, Any]]] = {}
    for cell in scored_cells:
        order = cell.get("order")
        system = cell.get("system")
        if isinstance(order, bool) or not isinstance(order, int) or system not in SYSTEMS:
            return {"label": "PASSAGE_UNASSESSABLE", "reason": "MALFORMED_CELL_SET"}
        if system in grouped.setdefault(order, {}):
            return {"label": "PASSAGE_UNASSESSABLE", "reason": "DUPLICATE_CELL"}
        grouped[order][system] = cell
    evidence_complete = bool(
        len(grouped) == expected_pair_count
        and all(set(pair) == set(SYSTEMS) for pair in grouped.values())
        and all(
            cell.get("evidence_validity") == "VALID"
            and isinstance(cell.get("passage_complete"), bool)
            for pair in grouped.values()
            for cell in pair.values()
        )
    )
    counts = {dataset: {system: 0 for system in SYSTEMS} for dataset in DATASETS}
    overall = {system: 0 for system in SYSTEMS}
    discordances: List[Mapping[str, Any]] = []
    has_regression = False
    for order in sorted(grouped):
        pair = grouped[order]
        if set(pair) != set(SYSTEMS):
            continue
        dataset = pair["U0"].get("dataset")
        if dataset not in counts or pair["S1"].get("dataset") != dataset:
            evidence_complete = False
            continue
        complete = {system: bool(pair[system].get("passage_complete")) for system in SYSTEMS}
        for system in SYSTEMS:
            if complete[system]:
                counts[dataset][system] += 1
                overall[system] += 1
        if complete["U0"] != complete["S1"]:
            direction = "U0_ONLY" if complete["U0"] else "S1_ONLY"
            discordances.append({"order": order, "dataset": dataset, "direction": direction})
            has_regression = has_regression or direction == "U0_ONLY"
    label = _passage_label_from_facts(evidence_complete, has_regression, counts, overall)
    return {
        "label": label,
        "evidence_complete": evidence_complete,
        "scheduled_pair_count": expected_pair_count,
        "dataset_counts": counts,
        "overall_counts": overall,
        "discordances": discordances,
        "u0_complete_s1_incomplete_count": sum(
            item["direction"] == "U0_ONLY" for item in discordances
        ),
        "s1_complete_u0_incomplete_count": sum(
            item["direction"] == "S1_ONLY" for item in discordances
        ),
        "s1_local_passage_25_of_25": bool(
            expected_pair_count == 25 and evidence_complete and overall["S1"] == 25
        ),
    }


def _kaist_family(sequence: str) -> str:
    family = sequence.split("/", 1)[0]
    if family not in ("rotation", "circle", "infinite", "square"):
        raise AggregateError("unknown KAIST passage family")
    return family


def _accuracy_family(ratios: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    values: Dict[str, List[float]] = {metric: [] for metric in METRICS}
    complete_pair_count = 0
    for pair in ratios:
        if pair.get("status") != "COMPLETE":
            continue
        complete_pair_count += 1
        relative = pair.get("ratios")
        if not isinstance(relative, dict):
            continue
        for metric in METRICS:
            record = relative.get(metric)
            if not isinstance(record, dict):
                continue
            if (
                record.get("status") == "DEFINED_FINITE"
                and record.get("mathematical_ratio_defined") is True
                and isinstance(record.get("value"), (int, float))
                and not isinstance(record.get("value"), bool)
                and math.isfinite(float(record["value"]))
            ):
                values[metric].append(float(record["value"]))
    summaries: Dict[str, Any] = {}
    assessable = complete_pair_count >= ACCURACY_MINIMUM_PAIRS and all(
        len(values[metric]) >= ACCURACY_MINIMUM_PAIRS for metric in METRICS
    )
    guard_pass = assessable
    for metric in METRICS:
        metric_values = values[metric]
        median = statistics.median(metric_values) if metric_values else None
        maximum = max(metric_values) if metric_values else None
        metric_pass = bool(
            len(metric_values) >= ACCURACY_MINIMUM_PAIRS
            and median is not None
            and maximum is not None
            and median <= ACCURACY_MEDIAN_LIMIT
            and maximum <= ACCURACY_INDIVIDUAL_LIMIT
        )
        summaries[metric] = {
            "defined_ratio_count": len(metric_values),
            "median_relative_difference": median,
            "maximum_relative_difference": maximum,
            "median_limit": ACCURACY_MEDIAN_LIMIT,
            "individual_limit": ACCURACY_INDIVIDUAL_LIMIT,
            "guard_pass": metric_pass if assessable else None,
        }
        guard_pass = guard_pass and metric_pass
    label = (
        "ACCURACY_UNASSESSABLE"
        if not assessable
        else "ACCURACY_NONINFERIOR"
        if guard_pass
        else "ACCURACY_GUARD_FAIL"
    )
    return {
        "label": label,
        "complete_pair_result_count": complete_pair_count,
        "minimum_complete_defined_pairs": ACCURACY_MINIMUM_PAIRS,
        "metrics": summaries,
    }


def accuracy_family_summary(ratios: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Public pure helper used by focused boundary tests."""

    return _accuracy_family(ratios)


def _validate_ratio_record(
    record: Any, u0: float, s1: float, metric: str
) -> Mapping[str, Any]:
    if not isinstance(record, dict):
        raise AggregateError("pair ratio {} is absent".format(metric))
    if record.get("formula") != "(S1-U0)/U0":
        raise AggregateError("pair ratio formula drift")
    observed_u0 = _finite_number(record.get("baseline_u0"), metric + " ratio U0", 0.0)
    observed_s1 = _finite_number(record.get("candidate_s1"), metric + " ratio S1", 0.0)
    if observed_u0 != u0 or observed_s1 != s1:
        raise AggregateError("pair ratio endpoints differ from metrics")
    if u0 == 0.0:
        if (
            record.get("status") != "UNASSESSABLE_ZERO_U0_DENOMINATOR"
            or record.get("mathematical_ratio_defined") is not False
            or record.get("value") is not None
        ):
            raise AggregateError("zero U0 denominator was assigned a ratio")
    else:
        value = _finite_number(record.get("value"), metric + " ratio")
        expected = (s1 - u0) / u0
        if (
            record.get("status") != "DEFINED_FINITE"
            or record.get("mathematical_ratio_defined") is not True
            or not math.isclose(value, expected, rel_tol=1.0e-12, abs_tol=1.0e-12)
        ):
            raise AggregateError("defined pair ratio does not match metric endpoints")
    return record


def _validate_quaternion_projection(
    value: Any, checks: Mapping[str, Any]
) -> Mapping[str, Any]:
    if checks.get("bounded_quaternion_projection_for_evo_only") is not True:
        raise AggregateError("pair quaternion-projection check is absent")
    if not isinstance(value, dict) or set(value) != {"ground_truth", "U0", "S1"}:
        raise AggregateError("pair quaternion-projection record is malformed")
    for label in ("ground_truth", "U0", "S1"):
        record = value.get(label)
        if not isinstance(record, dict):
            raise AggregateError("pair quaternion-projection member is absent")
        row_count = record.get("source_row_count")
        projected = record.get("rows_projected")
        if (
            record.get("policy") != "q_over_l2_norm_for_evo_objects_only"
            or record.get("maximum_allowed_abs_norm_error")
            != PAIR_QUATERNION_NORM_MAX_ERROR
            or isinstance(row_count, bool)
            or not isinstance(row_count, int)
            or row_count <= 0
            or isinstance(projected, bool)
            or not isinstance(projected, int)
            or projected < 0
            or projected > row_count
            or record.get("source_bytes_unchanged") is not True
            or record.get("source_tokens_unchanged") is not True
            or record.get("row_ids_from_raw_source_bytes") is not True
        ):
            raise AggregateError("pair quaternion-projection policy drift")
        minimum = _finite_number(record.get("minimum_source_norm"), label + " min q norm")
        maximum = _finite_number(record.get("maximum_source_norm"), label + " max q norm")
        maximum_error = _finite_number(
            record.get("maximum_abs_source_norm_error"), label + " max q norm error", 0.0
        )
        observed_error = max(abs(minimum - 1.0), abs(maximum - 1.0))
        if (
            minimum <= 0.0
            or maximum < minimum
            or maximum_error > PAIR_QUATERNION_NORM_MAX_ERROR
            or observed_error > PAIR_QUATERNION_NORM_MAX_ERROR
            or not math.isclose(
                maximum_error, observed_error, rel_tol=1.0e-12, abs_tol=1.0e-15
            )
        ):
            raise AggregateError("pair quaternion-projection bound failed")
    return value


def _validate_pair_result(
    path: Path,
    row: campaign.MatrixRow,
    scored: Mapping[str, campaign.ValidatedResult],
    protocol_identity: Mapping[str, Any],
    matrix_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, identity = load_json(path, "pair result")
    checksums = verify_checksum_directory(path.parent, path)
    status = value.get("status")
    if (
        value.get("schema") != PAIR_SCHEMA
        or status not in ("COMPLETE", "UNASSESSABLE")
        or value.get("protocol_id") != PROTOCOL_ID
        or value.get("dataset") != row.dataset
        or value.get("sequence") != row.sequence
    ):
        raise AggregateError("pair result identity/status contract mismatch")
    sources = value.get("source_runs")
    if not isinstance(sources, dict):
        raise AggregateError("pair source runs are absent")
    for system in SYSTEMS:
        source = sources.get(system)
        if not isinstance(source, dict):
            raise AggregateError("pair source {} is absent".format(system))
        _require_same_identity(source.get("manifest"), scored[system].identity, "pair source manifest")
        for pair_name, run_name in (("state", "state"), ("deviation", "deviation"), ("trajectory", "tum")):
            expected = _artifact_identity(scored[system].value, run_name)
            if expected is None:
                raise AggregateError("accuracy-eligible scored artifact is absent")
            _require_same_identity(source.get(pair_name), expected, "pair source {}".format(pair_name))
    _require_same_identity(value.get("ground_truth"), row.ground_truth, "pair ground truth")
    binding = value.get("matrix_binding")
    if not isinstance(binding, dict):
        raise AggregateError("pair matrix binding is absent")
    _require_same_identity(binding.get("identity"), matrix_identity, "pair matrix")
    _require_same_identity(binding.get("protocol"), protocol_identity, "pair protocol")
    _require_same_identity(binding.get("bag"), row.bag, "pair selected bag")
    _require_same_identity(binding.get("ground_truth"), row.ground_truth, "pair matrix GT")
    if (
        binding.get("row_order") != row.order
        or binding.get("dataset") != row.dataset
        or binding.get("sequence") != row.sequence
        or binding.get("ground_truth_capability") != "full_trajectory"
    ):
        raise AggregateError("pair matrix row binding mismatch")
    checks = value.get("checks")
    if not isinstance(checks, dict):
        raise AggregateError("pair checks are absent")
    _validate_quaternion_projection(value.get("quaternion_projection"), checks)
    common = value.get("common_population")
    rpe = value.get("rpe_reference_pairs")
    if not isinstance(common, dict) or not isinstance(rpe, dict):
        raise AggregateError("pair population records are absent")
    _require_same_identity(value.get("evaluator"), file_identity(PAIR_EVALUATOR), "pair evaluator")
    _require_same_identity(value.get("math_core"), file_identity(PAIR_MATH_CORE), "pair math core")
    if value.get("evaluator_runtime_pins_sha256") != EXPECTED_PAIR_RUNTIME_PINS_SHA256:
        raise AggregateError("pair evaluator runtime pin digest drift")
    if (
        checks.get("run_and_artifact_hashes_revalidated") is not True
        or checks.get("ground_truth_opened_only_by_post_close_evaluator") is not True
        or checks.get("identical_common_gt_population") is not True
        or checks.get("evaluator_runtime_pinned") is not True
    ):
        raise AggregateError("pair evaluator provenance checks are incomplete")
    common_count = common.get("count")
    if isinstance(common_count, bool) or not isinstance(common_count, int) or common_count < 0:
        raise AggregateError("pair common population count is malformed")
    if common.get("minimum_required") != 100:
        raise AggregateError("pair common population minimum drift")
    if common.get("requirement_passed") is not (common_count >= 100):
        raise AggregateError("pair common population pass flag is inconsistent")
    if rpe.get("minimum_required") != 100 or rpe.get("delta_m") != 1.0:
        raise AggregateError("pair RPE population contract drift")
    metrics_out: Dict[str, Any] = {}
    ratios_out: Dict[str, Any] = {}
    if status == "COMPLETE":
        pair_count = rpe.get("count")
        if common_count < 100 or isinstance(pair_count, bool) or not isinstance(pair_count, int) or pair_count < 100:
            raise AggregateError("complete pair lacks required populations")
        systems = rpe.get("systems")
        digest = rpe.get("common_index_tuples_sha256")
        if not isinstance(systems, dict) or not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
            raise AggregateError("complete pair RPE identity is malformed")
        for system in SYSTEMS:
            item = systems.get(system)
            if (
                not isinstance(item, dict)
                or item.get("tuple_count") != pair_count
                or item.get("common_index_tuples_sha256") != digest
            ):
                raise AggregateError("pair systems do not share one RPE tuple list")
        metrics = value.get("metrics")
        relative = value.get("relative_difference_s1_minus_u0_over_u0")
        if not isinstance(metrics, dict) or not isinstance(relative, dict):
            raise AggregateError("complete pair metrics are absent")
        for metric in METRICS:
            u0 = _finite_number(metrics.get("U0", {}).get(metric), "U0 " + metric, 0.0)
            s1 = _finite_number(metrics.get("S1", {}).get(metric), "S1 " + metric, 0.0)
            metrics_out[metric] = {"U0": u0, "S1": s1}
            ratios_out[metric] = _validate_ratio_record(relative.get(metric), u0, s1, metric)
        if checks.get("minimum_population_requirements_passed") is not True:
            raise AggregateError("complete pair denies its population requirements")
        if checks.get("metrics_computed") is not True:
            raise AggregateError("complete pair denies metric computation")
    else:
        reason = value.get("unassessable_reason")
        if (
            not isinstance(reason, dict)
            or reason.get("code") not in ("INSUFFICIENT_COMMON_POSES", "INSUFFICIENT_RPE_PAIRS")
            or reason.get("minimum_required") != 100
            or reason.get("metric_scope") != "all_accuracy_metrics"
            or isinstance(reason.get("observed_count"), bool)
            or not isinstance(reason.get("observed_count"), int)
            or reason.get("observed_count") < 0
        ):
            raise AggregateError("unassessable pair reason is malformed")
        if value.get("metrics") != {} or value.get("relative_difference_s1_minus_u0_over_u0") != {}:
            raise AggregateError("unassessable pair invented accuracy metrics")
        if checks.get("minimum_population_requirements_passed") is not False or checks.get("metrics_computed") is not False:
            raise AggregateError("unassessable pair checks are inconsistent")
        if reason["code"] == "INSUFFICIENT_COMMON_POSES":
            if common_count >= 100 or reason["observed_count"] != common_count:
                raise AggregateError("insufficient-common result count mismatch")
            if (
                rpe.get("status") != "NOT_EVALUATED_INSUFFICIENT_COMMON_POSES"
                or rpe.get("count") is not None
                or rpe.get("requirement_evaluated") is not False
            ):
                raise AggregateError("unassessable common result evaluated RPE")
        else:
            pair_count = rpe.get("count")
            if common_count < 100 or isinstance(pair_count, bool) or not isinstance(pair_count, int):
                raise AggregateError("insufficient-RPE result populations are malformed")
            if pair_count >= 100 or reason["observed_count"] != pair_count:
                raise AggregateError("insufficient-RPE result count mismatch")
            if rpe.get("status") != "INSUFFICIENT_RPE_PAIRS" or rpe.get("requirement_evaluated") is not True:
                raise AggregateError("insufficient-RPE result status mismatch")
    return {
        "order": row.order,
        "key": campaign.row_key(row),
        "dataset": row.dataset,
        "sequence": row.sequence,
        "status": status,
        "result": identity,
        "checksums": checksums,
        "common_pose_count": common_count,
        "rpe_pair_count": rpe.get("count"),
        "unassessable_reason": value.get("unassessable_reason"),
        "metrics": metrics_out,
        "ratios": ratios_out,
    }


def _validate_geometry(
    path: Path,
    row: campaign.MatrixRow,
    system: str,
    capture: campaign.ValidatedResult,
    scored: campaign.ValidatedResult,
    matrix_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, identity = load_json(path, "qualitative manifest")
    checksums = verify_checksum_directory(path.parent, path)
    if (
        value.get("schema") != GEOMETRY_SCHEMA
        or value.get("status") not in ("GEOMETRY_EVIDENCE_COMPLETE", "FAILURE_EVIDENCE_COMPLETE")
        or value.get("protocol_id") != PROTOCOL_ID
        or value.get("dataset") != row.dataset
        or value.get("sequence") != row.sequence
        or value.get("system") != system
        or value.get("matrix_order") != row.order
        or value.get("run_id") != capture.value.get("run_id")
    ):
        raise AggregateError("qualitative manifest identity contract mismatch")
    expected_mode = (
        "REFERENCE_BACKED_VALIDATED_V3"
        if row.ground_truth["capability"] == "full_trajectory"
        else "NATIVE_ESTIMATOR_FRAME_PARTIAL_REFERENCE"
    )
    if value.get("status") == "GEOMETRY_EVIDENCE_COMPLETE" and value.get("bundle_mode") != expected_mode:
        raise AggregateError("qualitative bundle mode differs from reference capability")
    if value.get("status") == "FAILURE_EVIDENCE_COMPLETE" and value.get("bundle_mode") != "FAILURE_TILE_ONLY":
        raise AggregateError("failure qualitative evidence lacks its explicit tile mode")
    if value.get("capture_status") != capture.value.get("status"):
        raise AggregateError("qualitative capture status binding mismatch")
    if value.get("png_policy") != PNG_POLICY or value.get("png_files_produced") != 0:
        raise AggregateError("qualitative PNG policy drift")
    if any(path.parent.rglob("*.png")) or any(path.parent.rglob("*.PNG")):
        raise AggregateError("qualitative bundle contains a forbidden PNG")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict):
        raise AggregateError("qualitative input bindings are absent")
    _require_same_identity(inputs.get("capture_sequence_result"), capture.identity, "geometry capture result")
    _require_same_identity(inputs.get("scored_sequence_result"), scored.identity, "geometry scored result")
    _require_same_identity(inputs.get("campaign_matrix"), matrix_identity, "geometry matrix")
    _require_same_identity(inputs.get("selected_bag"), row.bag, "geometry selected bag")
    _require_same_identity(
        inputs.get("capture_checksum_set"),
        file_identity(capture.path.parent / "SHA256SUMS"),
        "geometry capture checksum set",
    )
    if value.get("ground_truth_capability") != row.ground_truth["capability"]:
        raise AggregateError("qualitative ground-truth capability drift")
    if (
        row.ground_truth["capability"] == "full_trajectory"
        and value.get("bundle_mode") == "REFERENCE_BACKED_VALIDATED_V3"
    ):
        _require_same_identity(inputs.get("ground_truth"), row.ground_truth, "geometry ground truth")
    elif row.ground_truth["capability"] == "full_trajectory" and inputs.get("ground_truth") is not None:
        # A delegated renderer can fail after the frozen reference identity is
        # opened; retain that bound identity even though only a failure tile is
        # publishable.  A failure before reference access truthfully records null.
        _require_same_identity(inputs.get("ground_truth"), row.ground_truth, "failed geometry ground truth")
    elif inputs.get("ground_truth") is not None:
        raise AggregateError("partial-reference geometry opened a ground-truth identity")
    raw = _artifact_identity(capture.value, "raw_geometry")
    if raw is None:
        raise AggregateError("capture lacks its authoritative raw geometry stream")
    _require_same_identity(inputs.get("raw_feature_stream"), raw, "geometry raw stream")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise AggregateError("qualitative artifact index is absent")
    actual_payloads = {
        item.relative_to(path.parent).as_posix()
        for item in path.parent.rglob("*")
        if item.is_file() and item.name not in ("qualitative_manifest.json", "SHA256SUMS")
    }
    if set(artifacts) != actual_payloads:
        raise AggregateError("qualitative manifest artifact membership mismatch")
    for relative, record in artifacts.items():
        observed = file_identity(path.parent / _safe_relative(relative))
        if not isinstance(record, dict) or record.get("size_bytes") != observed["size_bytes"] or record.get("sha256") != observed["sha256"]:
            raise AggregateError("qualitative artifact index mismatch: {}".format(relative))
    review = value.get("qualitative_review_status")
    if review not in (
        "PENDING_HUMAN_REVIEW",
        "QUALITATIVE_PASS",
        "QUALITATIVE_FLAGGED",
        "QUALITATIVE_UNASSESSABLE",
    ):
        raise AggregateError("unknown qualitative review status")
    flags = value.get("qualitative_flags")
    if not isinstance(flags, list) or any(not isinstance(item, str) for item in flags):
        raise AggregateError("qualitative flags are malformed")
    linkage_validation = value.get("validation", {}).get("scored_linkage")
    if capture.value.get("evidence_validity") == "CAPTURE_LINK_INVALID":
        if (
            value.get("status") != "FAILURE_EVIDENCE_COMPLETE"
            or value.get("bundle_mode") != "FAILURE_TILE_ONLY"
            or review != "QUALITATIVE_UNASSESSABLE"
            or "INVALID_LINKAGE" not in flags
            or not isinstance(linkage_validation, dict)
            or linkage_validation.get("valid") is not False
        ):
            raise AggregateError("invalid-linkage capture was not published as unassessable failure evidence")
    elif (
        not isinstance(linkage_validation, dict)
        or linkage_validation.get("valid") is not True
    ):
        raise AggregateError("qualitative bundle does not retain valid scored linkage")
    # A pending machine-generated bundle cannot be promoted to a human pass by
    # aggregation.  Explicit reviewed labels are preserved verbatim.
    aggregate_label = review if review in ("QUALITATIVE_PASS", "QUALITATIVE_FLAGGED") else "QUALITATIVE_UNASSESSABLE"
    return {
        "status": value.get("status"),
        "bundle_mode": value.get("bundle_mode"),
        "recorded_review_status": review,
        "aggregate_qualitative_label": aggregate_label,
        "flags": flags,
        "png_policy": value.get("png_policy"),
        "raw_geometry": raw,
        "manifest": identity,
        "checksums": checksums,
    }


def _validate_historical_determinism(value: Any, system: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AggregateError("fresh KAIST scored cell lacks historical determinism evidence")
    status = value.get("status")
    if status not in ("EXACT_MATCH", "MISMATCH_RETAINED", "UNAVAILABLE_RETAINED"):
        raise AggregateError("unknown historical KAIST determinism status")
    if value.get("fresh_outputs_never_replaced") is not True or value.get(
        "mismatch_is_not_infrastructure_invalid"
    ) is not True:
        raise AggregateError("historical determinism altered fresh-evidence semantics")
    if status == "UNAVAILABLE_RETAINED":
        if not isinstance(value.get("reason"), str) or not value.get("reason"):
            raise AggregateError("unavailable historical determinism lacks a reason")
        return value
    comparisons = value.get("comparisons")
    if not isinstance(comparisons, dict) or set(comparisons) != {"state", "deviation", "tum"}:
        raise AggregateError("historical determinism comparison set is incomplete")
    exact = True
    for name in ("state", "deviation", "tum"):
        item = comparisons[name]
        if not isinstance(item, dict) or not isinstance(item.get("byte_exact"), bool):
            raise AggregateError("historical {} comparison is malformed".format(name))
        exact = exact and item["byte_exact"]
    if value.get("all_three_byte_exact") is not exact:
        raise AggregateError("historical determinism aggregate flag is inconsistent")
    if (status == "EXACT_MATCH") is not exact:
        raise AggregateError("historical determinism status is inconsistent")
    source = value.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str):
        raise AggregateError("historical determinism source is absent")
    if system == "U0" and source.get("kind") != "U0_G0_5_SCORED_MANIFEST":
        raise AggregateError("U0 historical determinism source kind drift")
    if system == "S1" and source.get("kind") != "S1_R2_LEDGER":
        raise AggregateError("S1 historical determinism source kind drift")
    return value


def _validate_mechanism(
    path: Path,
    row: campaign.MatrixRow,
    scored: Mapping[str, campaign.ValidatedResult],
    matrix_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    value, identity = load_json(path, "KAIST rotation mechanism")
    if (
        value.get("schema") != MECHANISM_SCHEMA
        or value.get("status") not in ("PASS", "C2_VALIDATION_FAILURE")
        or value.get("pass") is not (value.get("status") == "PASS")
        or value.get("protocol_id") != PROTOCOL_ID
        or value.get("dataset") != "kaist_vio"
        or value.get("sequence") != "rotation/rotation.bag"
        or value.get("both_scored_estimator_groups_closed_before_ground_truth_open") is not True
        or value.get("accuracy_eligibility_unchanged_by_c2_target_thresholds") is not True
        or value.get("c2_validation_is_a_separate_robustness_mechanism_result") is not True
    ):
        raise AggregateError("KAIST mechanism result contract mismatch")
    sources = value.get("source_runs")
    for system in SYSTEMS:
        source = sources.get(system) if isinstance(sources, dict) else None
        if not isinstance(source, dict):
            raise AggregateError("KAIST mechanism source run is absent")
        _require_same_identity(source.get("result"), scored[system].identity, "mechanism source result")
        closure = source.get("closure")
        checksum_closure = closure.get("checksum_closure") if isinstance(closure, dict) else None
        checksum_identity = checksum_closure.get("identity") if isinstance(checksum_closure, dict) else None
        _require_same_identity(
            checksum_identity,
            file_identity(scored[system].path.parent / "SHA256SUMS"),
            "mechanism source checksum set",
        )
        if (
            not isinstance(checksum_closure, dict)
            or checksum_closure.get("status") != "PASS"
            or checksum_closure.get("all_run_files_closed") is not True
        ):
            raise AggregateError("KAIST mechanism source checksum closure failed")
    binding = value.get("matrix_binding")
    if not isinstance(binding, dict):
        raise AggregateError("KAIST mechanism matrix binding is absent")
    _require_same_identity(binding.get("matrix"), matrix_identity, "mechanism matrix")
    if binding.get("row_order") != row.order:
        raise AggregateError("KAIST mechanism matrix row order drift")
    declaration = binding.get("ground_truth_expected_from_matrix")
    expected_declaration = {
        "canonical_path": row.ground_truth["canonical_path"],
        "size_bytes": row.ground_truth["bytes"],
        "sha256": row.ground_truth["sha256"],
        "capability": row.ground_truth["capability"],
        "format": row.ground_truth.get("format"),
        "source": "FROZEN_MATRIX_DECLARATION_NOT_LIVE_FILE",
        "live_file_opened": False,
    }
    if declaration != expected_declaration:
        raise AggregateError("KAIST mechanism frozen GT declaration drift")
    opened = value.get("ground_truth_opened")
    if not isinstance(opened, bool) or binding.get("ground_truth_opened") is not opened:
        raise AggregateError("KAIST mechanism GT-open flag is malformed")
    prerequisite = value.get("c2_prerequisite")
    if not isinstance(prerequisite, dict) or not isinstance(prerequisite.get("pass"), bool):
        raise AggregateError("KAIST mechanism prerequisite record is malformed")
    if opened:
        _require_same_identity(binding.get("ground_truth"), row.ground_truth, "mechanism ground truth")
        if prerequisite.get("pass") is not True:
            raise AggregateError("KAIST mechanism opened GT before prerequisites passed")
    else:
        # Deliberately do not resolve/stat the matrix-declared GT path here.
        if (
            binding.get("ground_truth") is not None
            or value.get("status") != "C2_VALIDATION_FAILURE"
            or value.get("pass") is not False
            or prerequisite.get("pass") is not False
            or any(
                value.get(name) is not None
                for name in (
                    "rotation_gap",
                    "s1_single_population_metrics",
                    "rotation_gap_gate",
                    "numeric_target_acceptance",
                )
            )
        ):
            raise AggregateError("early KAIST mechanism failure improperly used GT/metrics")
    if prerequisite.get("pass") is not opened:
        raise AggregateError("KAIST mechanism prerequisite/GT-open boundary differs")
    retained = value.get("retained_s1_evidence")
    if not isinstance(retained, dict):
        raise AggregateError("KAIST mechanism did not retain S1 failure evidence")
    if value.get("status") == "C2_VALIDATION_FAILURE":
        failure = value.get("c2_validation_failure")
        if (
            not isinstance(failure, dict)
            or failure.get("algorithm_failure_retained") is not True
            or failure.get("infrastructure_failure") is not False
        ):
            raise AggregateError("KAIST C2 failure semantics are malformed")
    postflight = value.get("postflight_identities")
    if not isinstance(postflight, dict):
        raise AggregateError("KAIST mechanism postflight identity set is absent")
    _require_same_identity(postflight.get("U0_result"), scored["U0"].identity, "mechanism postflight U0 result")
    _require_same_identity(postflight.get("S1_result"), scored["S1"].identity, "mechanism postflight S1 result")
    _require_same_identity(postflight.get("matrix"), matrix_identity, "mechanism postflight matrix")
    for system in SYSTEMS:
        _require_same_identity(
            postflight.get(system + "_checksums"),
            file_identity(scored[system].path.parent / "SHA256SUMS"),
            "mechanism postflight {} checksums".format(system),
        )
    if opened:
        _require_same_identity(postflight.get("ground_truth"), row.ground_truth, "mechanism postflight GT")
        trajectory = _artifact_identity(scored["S1"].value, "tum")
        if trajectory is None:
            raise AggregateError("evaluable C2 mechanism lacks the S1 trajectory")
        _require_same_identity(postflight.get("S1_trajectory"), trajectory, "mechanism postflight S1 trajectory")
    elif "ground_truth" in postflight or "S1_trajectory" in postflight:
        raise AggregateError("early C2 mechanism postflight crossed the GT/trajectory boundary")
    return {"status": value.get("status"), "pass": value.get("pass"), "result": identity, "details": value}


def validate_accounting(cells: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    keys = [(item.get("lane"), item.get("order"), item.get("system")) for item in cells]
    expected = {(lane, order, system) for lane in LANES for order in range(1, 26) for system in SYSTEMS}
    observed = set(keys)
    if len(keys) != 100 or len(observed) != 100 or observed != expected:
        raise AggregateError("campaign cell accounting is not exactly 25x2x2")
    return {
        "scheduled_sequence_pairs": 25,
        "scored_cells_expected": 50,
        "scored_cells_observed": sum(item[0] == "scored" for item in keys),
        "capture_cells_expected": 50,
        "capture_cells_observed": sum(item[0] == "capture" for item in keys),
        "all_cells_expected": 100,
        "all_cells_observed": len(keys),
        "exact": True,
    }


def _json_cell(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def render_report(aggregate: Mapping[str, Any]) -> str:
    passage = aggregate["passage"]
    accuracy = aggregate["accuracy"]
    qualitative = aggregate["qualitative"]
    lines = [
        "# CDSC-1R3 U0--S1 whole-system comparison",
        "",
        "## Claim boundary",
        "",
        "This is a paired whole-system comparison of the exact frozen U0 and S1 systems. It cannot attribute a difference to Schur elimination, does not establish generic robustness or superiority, and is not a Jetson timing or energy result.",
        "",
        "Late initialization is descriptive only: passage is evaluated from the first valid initialized state to the final selected input. No claim is made for an omitted pre-initialization prefix.",
        "",
        "## Passage robustness",
        "",
        "Mechanical label: `{}`.".format(passage["label"]),
        "",
        "With late initialization excluded, frozen S1 completed from initialization to the final selected input on {}/25 local passages versus {}/25 for pinned stock U0.".format(
            passage["overall_counts"]["S1"], passage["overall_counts"]["U0"]
        ),
        "",
        "| Dataset | U0 complete | S1 complete | Scheduled |",
        "|---|---:|---:|---:|",
    ]
    scheduled = {"euroc_mav": 11, "tum_vi": 3, "kaist_vio": 11}
    for dataset in DATASETS:
        counts = passage["dataset_counts"][dataset]
        lines.append("| {} | {} | {} | {} |".format(dataset, counts["U0"], counts["S1"], scheduled[dataset]))
    lines.extend([
        "",
        "KAIST passage-family completion:",
        "",
        "| Family | U0 complete | S1 complete | Scheduled |",
        "|---|---:|---:|---:|",
    ])
    family_scheduled = {"rotation": 2, "circle": 3, "infinite": 3, "square": 3}
    for family in ("rotation", "circle", "infinite", "square"):
        counts = passage["kaist_families"][family]
        lines.append("| {} | {} | {} | {} |".format(family, counts["U0"], counts["S1"], family_scheduled[family]))
    lines.extend([
        "",
        "Strict process health is reported separately from passage completion; U0's predeclared post-coverage teardown defect is never hidden.",
        "",
        "## Accuracy",
        "",
        "EuRoC: `{}`. KAIST: `{}`.".format(
            accuracy["families"]["euroc_mav"]["label"],
            accuracy["families"]["kaist_vio"]["label"],
        ),
        "",
        "TUM-VI room4 is one descriptive paired result only. corridor4 and outdoors4 have no full eligible reference and are explicitly unscored; no TUM-VI family accuracy claim is made.",
        "",
        "An unassessable common-population result and every undefined zero-U0 ratio remain visible and do not enter a family gate. Passage and accuracy labels are independent.",
        "",
        "The separate KAIST rotation C2 mechanism result is `{}`; its target gates do not change passage or common-accuracy eligibility.".format(
            aggregate["mechanism"]["status"]
        ),
        "",
        "## Qualitative geometry",
        "",
        "All 50 capture attempts and their checksum-bound raw sparse geometry streams are accounted for. The geometry is sparse active SLAM state plus transient MSCKF/update/track geometry; it is not a persistent or dense map.",
        "",
        "The aggregator does not automatically assert a human visual pass. Explicit pass: {}; flagged: {}; unassessable or pending review: {}.".format(
            qualitative["label_counts"].get("QUALITATIVE_PASS", 0),
            qualitative["label_counts"].get("QUALITATIVE_FLAGGED", 0),
            qualitative["label_counts"].get("QUALITATIVE_UNASSESSABLE", 0),
        ),
        "",
        "Flag-bearing manifests (including retained failure evidence): {}.".format(
            qualitative["flagged_manifest_count"]
        ),
        "",
        "PNG policy: `{}`. Deterministic PLY/SVG products and the authoritative raw stream are retained; no PNG was produced.".format(PNG_POLICY),
        "",
        "## Evidence inventory",
        "",
        "The machine-readable aggregate and CSV files contain all 100 scored/capture cells, including `NO_INITIALIZATION`, partial, crash, numeric, tracking, teardown, invalid-evidence, recovery, determinism, and capture-linkage fields. Failed outcomes were not repaired or replaced.",
        "",
    ])
    return "\n".join(lines)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _write_text(path: Path, value: str) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())


def _write_sequence_csv(path: Path, cells: Sequence[Mapping[str, Any]]) -> None:
    fields = (
        "lane", "order", "key", "dataset", "sequence", "system", "run_id",
        "evidence_validity", "status", "passage_complete", "passage_eligible",
        "passage_reason", "initialization_timestamp_s", "initialization_delay_s",
        "tail_gap_s", "maximum_state_gap_s", "supported_input_gap_count",
        "strict_process_health", "accuracy_eligible", "qualitative_eligible",
        "result_sha256", "checksum_set_sha256", "recovery_json", "historical_kaist_determinism_json",
        "capture_linkage_json", "raw_geometry_json", "qualitative_json",
        "outcome_facts_json",
    )
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for cell in sorted(cells, key=lambda item: (LANES.index(item["lane"]), item["order"], SYSTEMS.index(item["system"]))):
            writer.writerow({
                **{name: cell.get(name) for name in fields if not name.endswith("_json")},
                "result_sha256": cell["result"]["sha256"],
                "checksum_set_sha256": cell["checksum_set"]["sha256"],
                "recovery_json": _json_cell(cell.get("recovery")),
                "historical_kaist_determinism_json": _json_cell(cell.get("historical_kaist_determinism")),
                "capture_linkage_json": _json_cell(cell.get("capture_linkage")),
                "raw_geometry_json": _json_cell(cell.get("raw_geometry")),
                "qualitative_json": _json_cell(cell.get("qualitative")),
                "outcome_facts_json": _json_cell(cell.get("outcome_facts")),
            })
        stream.flush()
        os.fsync(stream.fileno())


def _write_pair_csv(path: Path, rows: Sequence[campaign.MatrixRow], pairs: Mapping[int, Mapping[str, Any]]) -> None:
    fields = [
        "order", "key", "dataset", "sequence", "ground_truth_capability",
        "pair_result_present", "status", "unassessable_reason_json", "common_pose_count",
        "rpe_pair_count", "pair_result_sha256",
    ]
    for metric in METRICS:
        fields.extend(("{}_u0".format(metric), "{}_s1".format(metric), "{}_ratio_status".format(metric), "{}_relative_difference".format(metric)))
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            pair = pairs.get(row.order)
            record: Dict[str, Any] = {
                "order": row.order,
                "key": campaign.row_key(row),
                "dataset": row.dataset,
                "sequence": row.sequence,
                "ground_truth_capability": row.ground_truth["capability"],
                "pair_result_present": pair is not None,
                "status": pair.get("status") if pair else "NOT_EVALUATED",
                "unassessable_reason_json": _json_cell(pair.get("unassessable_reason") if pair else None),
                "common_pose_count": pair.get("common_pose_count") if pair else None,
                "rpe_pair_count": pair.get("rpe_pair_count") if pair else None,
                "pair_result_sha256": pair.get("result", {}).get("sha256") if pair else None,
            }
            for metric in METRICS:
                metric_values = pair.get("metrics", {}).get(metric, {}) if pair else {}
                ratio = pair.get("ratios", {}).get(metric, {}) if pair else {}
                record["{}_u0".format(metric)] = metric_values.get("U0")
                record["{}_s1".format(metric)] = metric_values.get("S1")
                record["{}_ratio_status".format(metric)] = ratio.get("status", "NOT_AVAILABLE")
                record["{}_relative_difference".format(metric)] = ratio.get("value")
            writer.writerow(record)
        stream.flush()
        os.fsync(stream.fileno())


def _publish_no_replace(staging: Path, final: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AggregateError("atomic no-replace directory publication is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(str(staging)), -100, os.fsencode(str(final)), 1)
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise AggregateError("refusing to overwrite output directory: {}".format(final))
        raise AggregateError("atomic report publication failed: {}".format(os.strerror(error)))
    descriptor = os.open(str(final.parent), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def aggregate_campaign(
    artifact_root: Path,
    output_dir: Path,
    matrix_path: Path = campaign.MATRIX_FILE,
    protocol_path: Path = campaign.PROTOCOL_FILE,
) -> Mapping[str, Any]:
    artifact_root = artifact_root.expanduser().resolve(strict=True)
    if not artifact_root.is_dir():
        raise AggregateError("artifact root is not a directory")
    matrix_resolved = matrix_path.expanduser().resolve(strict=True)
    protocol_resolved = protocol_path.expanduser().resolve(strict=True)
    if matrix_resolved != campaign.MATRIX_FILE.resolve(strict=True) or protocol_resolved != campaign.PROTOCOL_FILE.resolve(strict=True):
        raise AggregateError("CDSC-1R3 aggregate requires the canonical matrix and protocol paths")
    rows = campaign.load_matrix(matrix_resolved, require_canonical=True)
    matrix_identity = file_identity(matrix_resolved)
    protocol_identity = file_identity(protocol_resolved)
    runner_identities = {
        "generic": file_identity(SCRIPT_DIR / "cross_dataset_trial.py"),
        "kaist": file_identity(SCRIPT_DIR / "cross_dataset_kaist_trial.py"),
    }
    live_identity_cache: Dict[str, Mapping[str, Any]] = {}

    expected_run_paths = {
        (lane, row.order, system): campaign.run_location(artifact_root, lane, row, system).result_path
        for lane in LANES for row in rows for system in SYSTEMS
    }
    found_run_paths = list((artifact_root / "scored").rglob("sequence_result.json")) + list((artifact_root / "capture").rglob("sequence_result.json"))
    assert_exact_paths(found_run_paths, expected_run_paths.values(), "sequence results")

    validated: Dict[Tuple[str, int, str], campaign.ValidatedResult] = {}
    cells: List[MutableMapping[str, Any]] = []
    for lane in LANES:
        for row in rows:
            runner = _runner_for_row(row)
            runner_identity = file_identity(runner)
            for system in SYSTEMS:
                path = expected_run_paths[(lane, row.order, system)]
                result = campaign.validate_sequence_result(path, row, system, lane, expected_runner=runner)
                checksum_set = verify_checksum_directory(path.parent, path)
                _validate_run_bindings(
                    result,
                    row,
                    lane,
                    system,
                    protocol_identity,
                    matrix_identity,
                    runner_identity,
                    live_identity_cache,
                )
                validated[(lane, row.order, system)] = result
                cell = _cell_record(row, lane, system, result)
                cell["checksum_set"] = checksum_set
                cells.append(cell)
    accounting = validate_accounting(cells)

    for row in rows:
        for system in SYSTEMS:
            capture = validated[("capture", row.order, system)]
            scored = validated[("scored", row.order, system)]
            _validate_capture_linkage(capture.value, scored)

    expected_pairs = {
        row.order
        for row in rows
        if row.ground_truth["capability"] == "full_trajectory"
        and all(validated[("scored", row.order, system)].value.get("accuracy_eligible") is True for system in SYSTEMS)
    }
    expected_pair_paths = {artifact_root / "pair" / campaign.row_key(row) / "pair_result.json" for row in rows if row.order in expected_pairs}
    found_pair_paths = list((artifact_root / "pair").rglob("pair_result.json")) if (artifact_root / "pair").exists() else []
    assert_exact_paths(found_pair_paths, expected_pair_paths, "pair results")
    pairs: Dict[int, Mapping[str, Any]] = {}
    for row in rows:
        if row.order not in expected_pairs:
            continue
        path = artifact_root / "pair" / campaign.row_key(row) / "pair_result.json"
        pairs[row.order] = _validate_pair_result(
            path,
            row,
            {system: validated[("scored", row.order, system)] for system in SYSTEMS},
            protocol_identity,
            matrix_identity,
        )

    expected_geometry_paths = {
        artifact_root / "geometry" / campaign.row_key(row) / system / "qualitative_manifest.json"
        for row in rows for system in SYSTEMS
    }
    found_geometry_paths = list((artifact_root / "geometry").rglob("qualitative_manifest.json")) if (artifact_root / "geometry").exists() else []
    assert_exact_paths(found_geometry_paths, expected_geometry_paths, "qualitative manifests")
    cell_index = {(cell["lane"], cell["order"], cell["system"]): cell for cell in cells}
    qualitative_records: List[Mapping[str, Any]] = []
    for row in rows:
        for system in SYSTEMS:
            geometry = _validate_geometry(
                artifact_root / "geometry" / campaign.row_key(row) / system / "qualitative_manifest.json",
                row,
                system,
                validated[("capture", row.order, system)],
                validated[("scored", row.order, system)],
                matrix_identity,
            )
            cell_index[("capture", row.order, system)]["qualitative"] = geometry
            qualitative_records.append({"order": row.order, "dataset": row.dataset, "sequence": row.sequence, "system": system, **geometry})

    rotation_row = next(row for row in rows if row.order == 19)
    mechanism_path = artifact_root / "mechanism" / campaign.row_key(rotation_row) / "kaist_rotation_post_pair.json"
    found_mechanisms = list((artifact_root / "mechanism").rglob("kaist_rotation_post_pair.json")) if (artifact_root / "mechanism").exists() else []
    assert_exact_paths(found_mechanisms, (mechanism_path,), "KAIST mechanism results")
    mechanism = _validate_mechanism(
        mechanism_path,
        rotation_row,
        {system: validated[("scored", 19, system)] for system in SYSTEMS},
        matrix_identity,
    )

    scored_cells = [cell for cell in cells if cell["lane"] == "scored"]
    passage = dict(mechanical_passage_summary(scored_cells))
    strict_health = {
        dataset: {
            system: sum(
                cell["dataset"] == dataset and cell["system"] == system and cell["strict_process_health"] is True
                for cell in scored_cells
            )
            for system in SYSTEMS
        }
        for dataset in DATASETS
    }
    passage["strict_process_health_counts"] = strict_health
    passage["late_initializations"] = [
        {key: cell.get(key) for key in ("order", "dataset", "sequence", "system", "initialization_delay_s")}
        for cell in scored_cells if isinstance(cell.get("initialization_delay_s"), (int, float)) and cell["initialization_delay_s"] > 0.0
    ]
    passage["input_supported_gaps"] = [
        {key: cell.get(key) for key in ("order", "dataset", "sequence", "system", "maximum_state_gap_s", "supported_input_gap_count")}
        for cell in scored_cells if (cell.get("supported_input_gap_count") or 0) > 0
    ]
    passage["kaist_families"] = {
        family: {
            system: sum(
                cell["dataset"] == "kaist_vio"
                and _kaist_family(cell["sequence"]) == family
                and cell["system"] == system
                and cell["passage_complete"] is True
                for cell in scored_cells
            )
            for system in SYSTEMS
        }
        for family in ("rotation", "circle", "infinite", "square")
    }

    family_pairs = {
        dataset: [pairs[row.order] for row in rows if row.dataset == dataset and row.order in pairs]
        for dataset in ("euroc_mav", "kaist_vio")
    }
    accuracy = {
        "families": {dataset: _accuracy_family(family_pairs[dataset]) for dataset in family_pairs},
        "tum_vi": {
            "family_label": "NOT_APPLICABLE_SINGLE_DESCRIPTIVE_PAIR",
            "room4": pairs.get(12),
            "corridor4": "UNSCORED_NO_FULL_REFERENCE",
            "outdoors4": "UNSCORED_NO_FULL_REFERENCE",
        },
        "pair_result_count": len(pairs),
        "complete_pair_result_count": sum(pair["status"] == "COMPLETE" for pair in pairs.values()),
        "unassessable_pair_result_count": sum(pair["status"] == "UNASSESSABLE" for pair in pairs.values()),
        "pairs": [pairs[order] for order in sorted(pairs)],
    }

    label_counts = {label: 0 for label in ("QUALITATIVE_PASS", "QUALITATIVE_FLAGGED", "QUALITATIVE_UNASSESSABLE")}
    for item in qualitative_records:
        label_counts[item["aggregate_qualitative_label"]] += 1
    qualitative = {
        "capture_manifest_count": len(qualitative_records),
        "all_50_present": len(qualitative_records) == 50,
        "png_policy": PNG_POLICY,
        "png_files_produced": 0,
        "human_visual_pass_automatically_asserted": False,
        "label_counts": label_counts,
        "flagged_manifest_count": sum(bool(item["flags"]) for item in qualitative_records),
        "records": qualitative_records,
    }

    kaist_historical = []
    for cell in scored_cells:
        if cell["dataset"] != "kaist_vio":
            continue
        evidence = _validate_historical_determinism(
            cell["historical_kaist_determinism"], cell["system"]
        )
        kaist_historical.append(
            {
                "order": cell["order"],
                "sequence": cell["sequence"],
                "system": cell["system"],
                "evidence": evidence,
            }
        )

    aggregate: MutableMapping[str, Any] = {
        "schema": SCHEMA,
        "status": "COMPLETE",
        "protocol_id": PROTOCOL_ID,
        "generated_utc": utc_now(),
        "artifact_root": str(artifact_root),
        "input_bindings": {
            "protocol": protocol_identity,
            "matrix": matrix_identity,
            "runners": runner_identities,
        },
        "accounting": accounting,
        "passage": passage,
        "accuracy": accuracy,
        "mechanism": mechanism,
        "historical_kaist_determinism": kaist_historical,
        "qualitative": qualitative,
        "cells": cells,
        "claim_boundary": {
            "whole_system_comparison": True,
            "schur_attribution_permitted": False,
            "generic_robustness_or_superiority_claim_permitted": False,
            "late_initialization_is_descriptive_only": True,
            "maps_are_sparse_active_and_transient_not_persistent_or_dense": True,
            "human_visual_pass_automatically_asserted": False,
        },
    }

    output_dir = output_dir.expanduser().resolve(strict=False)
    if output_dir.exists() or output_dir.is_symlink():
        raise AggregateError("refusing to overwrite output directory: {}".format(output_dir))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = output_dir.parent / ".{}.staging-{}".format(output_dir.name, uuid.uuid4().hex)
    stage.mkdir(mode=0o755)
    published = False
    try:
        _write_json(stage / "aggregate.json", aggregate)
        _write_sequence_csv(stage / "sequence_results.csv", cells)
        _write_pair_csv(stage / "pair_metrics.csv", rows, pairs)
        _write_text(stage / "CROSS_DATASET_SYSTEM_COMPARISON_REPORT.md", render_report(aggregate))
        outputs = [
            "CROSS_DATASET_SYSTEM_COMPARISON_REPORT.md",
            "aggregate.json",
            "pair_metrics.csv",
            "sequence_results.csv",
        ]
        checksum_text = "".join("{}  {}\n".format(sha256_file(stage / name), name) for name in outputs)
        _write_text(stage / "SHA256SUMS", checksum_text)
        verify_checksum_directory(stage, stage / "aggregate.json")
        _publish_no_replace(stage, output_dir)
        published = True
    finally:
        if not published and stage.exists():
            import shutil
            shutil.rmtree(str(stage))
    return aggregate


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--artifact-root", required=True, type=Path)
    value.add_argument("--output-dir", required=True, type=Path)
    value.add_argument("--matrix", type=Path, default=campaign.MATRIX_FILE)
    value.add_argument("--protocol", type=Path, default=campaign.PROTOCOL_FILE)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        aggregate = aggregate_campaign(args.artifact_root, args.output_dir, args.matrix, args.protocol)
    except (AggregateError, campaign.CampaignError, OSError, ValueError) as exc:
        print("CROSS_DATASET_AGGREGATE_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "status": aggregate["status"],
        "passage_label": aggregate["passage"]["label"],
        "euroc_accuracy": aggregate["accuracy"]["families"]["euroc_mav"]["label"],
        "kaist_accuracy": aggregate["accuracy"]["families"]["kaist_vio"]["label"],
        "output_dir": str(args.output_dir.expanduser().resolve(strict=True)),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
