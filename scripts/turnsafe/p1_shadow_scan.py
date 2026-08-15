#!/usr/bin/python3
"""Run and aggregate the frozen TurnSafe T1 P1 offline shadow scan."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import csv
from dataclasses import dataclass, field
import hashlib
import io
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, TextIO, Tuple
import xml.etree.ElementTree as ET


AGGREGATION_SCHEMA = "turnsafe.t1.p1.shadow_aggregation.v1"
ADMISSION_FREEZE_SCHEMA = "turnsafe.t1.p1.admission_freeze.v1"
COVERAGE_SCHEMA = "turnsafe.t1.p1.certificate_coverage.v1"
EXPECTED_CALLBACK_SCHEMA = "turnsafe.t1.p1.shadow_callback.v1"

FIXED_RUNS: Tuple[str, ...] = (
    "07_infinite_fast_on1_post_mh",
    "10_infinite_on1_post_mh",
    "04_infinite_head_on1_post_mh",
    "05_square_head_on1_post_mh",
    "08_square_fast_on1_post_mh",
    "11_square_on1_post_mh",
)
REPEAT_RUNS: Tuple[str, ...] = (
    "07_infinite_fast_on1_post_mh",
    "05_square_head_on1_post_mh",
)
RUN_SEQUENCE: Mapping[str, str] = {
    "07_infinite_fast_on1_post_mh": "infinite/infinite_fast.bag",
    "10_infinite_on1_post_mh": "infinite/infinite.bag",
    "04_infinite_head_on1_post_mh": "infinite/infinite_head.bag",
    "05_square_head_on1_post_mh": "square/square_head.bag",
    "08_square_fast_on1_post_mh": "square/square_fast.bag",
    "11_square_on1_post_mh": "square/square.bag",
}
RUN_SEQUENCE_ORDER: Mapping[str, int] = {
    "04_infinite_head_on1_post_mh": 4,
    "05_square_head_on1_post_mh": 5,
    "07_infinite_fast_on1_post_mh": 7,
    "08_square_fast_on1_post_mh": 8,
    "10_infinite_on1_post_mh": 10,
    "11_square_on1_post_mh": 11,
}

EVENT_WINDOWS: Tuple[Mapping[str, object], ...] = (
    {
        "event_id": "E07",
        "run": "07_infinite_fast_on1_post_mh",
        "analysis": (1467, 1474),
        "primary": (1470, 1471),
        "pre_onset": None,
    },
    {
        "event_id": "E10",
        "run": "10_infinite_on1_post_mh",
        "analysis": (1245, 1252),
        "primary": (1248, 1249),
        "pre_onset": (1245, 1247),
    },
)

PREREG_LEDGER_ENTRIES: Tuple[str, ...] = (
    "./P1_DECISION_PREREGISTRATION.json",
    "./P1_METRIC_CONTRACT.md",
    "./SOURCE_MAP.md",
    "../../../docs/turnsafe/t1_pilot_contract.md",
    "../../../docs/turnsafe/t1_certificate_contract.md",
    "../../../docs/turnsafe/t1_factor_contract.md",
)

COUNTER_COLUMNS: Tuple[str, ...] = (
    "raw_groups",
    "typed_groups",
    "raw_members",
    "stereo_valid_members",
    "range_lcb_valid_members",
    "translation_ucb_valid_members",
    "acute_members",
    "rho_passed_members",
    "stereo_valid_groups",
    "range_lcb_valid_groups",
    "translation_ucb_valid_groups",
    "acute_groups",
    "rho_groups",
    "groups_n_ge_4",
    "consensus_groups",
    "spatial_groups",
    "rank_groups",
    "conditioning_groups",
    "winner_count",
    "winner_nis_evaluated_count",
    "winner_nis_pass_count",
    "winner_information_non_negligible_count",
    "foregone_eligible_groups",
    "foregone_eligible_features",
)

WORKER_COLUMNS: Tuple[str, ...] = (
    "schema_version",
    "sequence",
    "run",
    "callback_index",
    "callback_timestamp_value",
    "callback_timestamp_key",
    "provenance_status",
    "provenance_reason",
    "pilot_status",
    "selection_status",
) + COUNTER_COLUMNS + (
    "eligible_group_count",
    "certified_winner_available",
    "winner_camera_id",
    "winner_source_timestamp_key",
    "winner_target_timestamp_key",
    "winner_group_status",
    "winner_terminal_reason",
    "winner_selection_role",
    "winner_raw_member_count",
    "winner_retained_member_count",
    "winner_minimum_acute_margin",
    "winner_minimum_rho_margin",
    "winner_score_predicted_rotation_angle_rad",
    "winner_score_minimum_whitened_local_singular_value",
    "winner_score_maximum_retained_rho_trans",
    "winner_score_retained_feature_count",
    "winner_consensus_first_fit_rotation_angle_rad",
    "winner_consensus_median",
    "winner_consensus_mad",
    "winner_consensus_robust_threshold",
    "winner_consensus_required_retained_count",
    "winner_consensus_refit_rotation_angle_rad",
    "winner_spatial_convex_hull_area",
    "winner_spatial_minimum_population_covariance_eigenvalue",
    "winner_spatial_occupied_fixed_4x4_cells",
    "winner_spatial_x_span",
    "winner_spatial_y_span",
    "winner_rotation_stack_sigma_1",
    "winner_rotation_stack_sigma_2",
    "winner_rotation_stack_sigma_3",
    "winner_rotation_stack_rank_tolerance",
    "winner_rotation_stack_rank",
    "winner_rotation_stack_condition_ratio",
    "winner_whitened_local_stack_sigma_1",
    "winner_whitened_local_stack_sigma_2",
    "winner_whitened_local_stack_sigma_3",
    "winner_factor_status",
    "winner_predicted_information_trace",
    "winner_predicted_information_determinant",
    "winner_predicted_information_eigenvalue_min",
    "winner_predicted_information_eigenvalue_middle",
    "winner_predicted_information_eigenvalue_max",
    "winner_predicted_information_non_negligible",
    "winner_nis",
    "winner_nis_degrees_of_freedom",
    "winner_nis_threshold",
    "winner_nis_passed",
    "winner_information_trace",
    "winner_information_determinant",
    "winner_information_eigenvalue_min",
    "winner_information_eigenvalue_middle",
    "winner_information_eigenvalue_max",
    "winner_relative_stack_sigma_1",
    "winner_relative_stack_sigma_2",
    "winner_relative_stack_sigma_3",
    "winner_information_non_negligible",
    "post_nis_frozen_winner_available",
    "post_nis_accepted_winner_available",
    "post_nis_runner_up_promoted",
    "rejection_reasons_json",
    "group_diagnostics_json",
    "candidate_diagnostics_json",
    "winner_candidate_diagnostics_json",
)

SCORE_COLUMNS: Tuple[str, ...] = (
    "winner_score_predicted_rotation_angle_rad",
    "winner_score_minimum_whitened_local_singular_value",
    "winner_score_maximum_retained_rho_trans",
    "winner_score_retained_feature_count",
    "winner_camera_id",
    "winner_source_timestamp_key",
    "winner_target_timestamp_key",
)

WINNER_FLOAT_COLUMNS: Tuple[str, ...] = (
    "winner_minimum_acute_margin",
    "winner_minimum_rho_margin",
    "winner_score_predicted_rotation_angle_rad",
    "winner_score_minimum_whitened_local_singular_value",
    "winner_score_maximum_retained_rho_trans",
    "winner_consensus_first_fit_rotation_angle_rad",
    "winner_consensus_median",
    "winner_consensus_mad",
    "winner_consensus_robust_threshold",
    "winner_consensus_refit_rotation_angle_rad",
    "winner_spatial_convex_hull_area",
    "winner_spatial_minimum_population_covariance_eigenvalue",
    "winner_spatial_x_span",
    "winner_spatial_y_span",
    "winner_rotation_stack_sigma_1",
    "winner_rotation_stack_sigma_2",
    "winner_rotation_stack_sigma_3",
    "winner_rotation_stack_rank_tolerance",
    "winner_rotation_stack_condition_ratio",
    "winner_whitened_local_stack_sigma_1",
    "winner_whitened_local_stack_sigma_2",
    "winner_whitened_local_stack_sigma_3",
    "winner_predicted_information_trace",
    "winner_predicted_information_determinant",
    "winner_predicted_information_eigenvalue_min",
    "winner_predicted_information_eigenvalue_middle",
    "winner_predicted_information_eigenvalue_max",
)

WINNER_UINT_COLUMNS: Tuple[str, ...] = (
    "winner_camera_id",
    "winner_raw_member_count",
    "winner_retained_member_count",
    "winner_score_retained_feature_count",
    "winner_consensus_required_retained_count",
    "winner_spatial_occupied_fixed_4x4_cells",
    "winner_rotation_stack_rank",
)

CHECKED_INFORMATION_FLOAT_COLUMNS: Tuple[str, ...] = (
    "winner_information_trace",
    "winner_information_determinant",
    "winner_information_eigenvalue_min",
    "winner_information_eigenvalue_middle",
    "winner_information_eigenvalue_max",
    "winner_relative_stack_sigma_1",
    "winner_relative_stack_sigma_2",
    "winner_relative_stack_sigma_3",
)

PREDICTED_INFORMATION_TRACE_FLOOR = 1.0e-12

SUMMARY_COLUMNS: Tuple[str, ...] = (
    "sequence_order",
    "run",
    "sequence",
    "family",
    "callbacks",
) + COUNTER_COLUMNS + (
    "certified_winner_callbacks",
    "winner_nis_evaluated_callbacks",
    "winner_nis_passing_callbacks",
    "predicted_information_non_negligible_callbacks",
    "post_nis_accepted_winner_callbacks",
    "predicted_information_trace_min",
    "predicted_information_trace_max",
    "predicted_information_trace_mean",
    "rejection_reason_counts_json",
)

FORBIDDEN_INPUT_TOKENS: Tuple[str, ...] = ("private", "holdout")
MAX_TOTAL_SECONDS = 9 * 60 * 60
MAX_WORKER_SECONDS = 90 * 60
PROCESS_TERM_GRACE_SECONDS = 10.0


class ScanFailure(RuntimeError):
    """A fail-closed P1 orchestration or aggregation error."""


@dataclass(frozen=True)
class FileIdentity:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class RunIdentity:
    run: str
    sequence: str
    sequence_order: int
    callback_count: int
    archive: Path
    archive_size: int
    archive_sha256: str
    raw_size: int
    raw_sha256: str
    record_count: int


@dataclass(frozen=True)
class WorkerScan:
    ordinal: int
    label: str
    run: str
    repeat: bool
    callback_csv: Path
    metadata_json: Path
    callback_identity: FileIdentity
    metadata_identity: FileIdentity
    stdout_identity: FileIdentity
    stderr_identity: FileIdentity


@dataclass
class SequenceAccumulator:
    identity: RunIdentity
    counters: Counter = field(default_factory=Counter)
    callbacks: int = 0
    certified: int = 0
    nis_evaluated: int = 0
    nis_passing: int = 0
    predicted_information_non_negligible: int = 0
    post_nis_accepted: int = 0
    predicted_information_traces: List[float] = field(default_factory=list)
    rejection_reasons: Counter = field(default_factory=Counter)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the fixed six-sequence P1 worker scan, deterministic repeats, "
            "admission aggregation, and post-freeze external-context join."
        )
    )
    parser.add_argument(
        "--worker",
        type=Path,
        required=True,
        help="Path to the turnsafe_p1_shadow_worker executable.",
    )
    parser.add_argument(
        "--certificate-test-xml",
        type=Path,
        default=None,
        help=(
            "Focused certificate GTest XML; defaults to the fixed P1 logs "
            "location."
        ),
    )
    parser.add_argument(
        "--worker-timeout-seconds",
        type=int,
        default=MAX_WORKER_SECONDS,
    )
    parser.add_argument(
        "--total-timeout-seconds",
        type=int,
        default=MAX_TOTAL_SECONDS,
    )
    return parser.parse_args()


def _strict_pairs(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    output: Dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ScanFailure("JSON contains duplicate key {!r}".format(key))
        output[key] = value
    return output


def _reject_json_constant(value: str) -> object:
    raise ScanFailure("JSON contains forbidden nonfinite constant {}".format(value))


def _strict_json_bytes(payload: bytes, label: str) -> object:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScanFailure("{} is not UTF-8".format(label)) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_strict_pairs,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ScanFailure) as exc:
        if isinstance(exc, ScanFailure):
            raise
        raise ScanFailure("{} is not strict JSON: {}".format(label, exc)) from exc


def _strict_json_file(path: Path, label: str) -> object:
    _require_regular_file(path, label)
    return _strict_json_bytes(path.read_bytes(), label)


def _object(value: object, label: str) -> MutableMapping[str, object]:
    if not isinstance(value, dict):
        raise ScanFailure("{} must be an object".format(label))
    return value


def _list(value: object, label: str) -> List[object]:
    if not isinstance(value, list):
        raise ScanFailure("{} must be an array".format(label))
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ScanFailure("{} must be a nonempty string".format(label))
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ScanFailure("{} must be a nonnegative integer".format(label))
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ScanFailure("{} must be Boolean".format(label))
    return value


def _sha_text(value: object, label: str) -> str:
    text = _string(value, label)
    if len(text) != 64 or any(byte not in "0123456789abcdef" for byte in text):
        raise ScanFailure("{} must be lowercase SHA-256".format(label))
    return text


def _require_regular_file(path: Path, label: str, executable: bool = False) -> os.stat_result:
    try:
        status = path.lstat()
    except OSError as exc:
        raise ScanFailure("{} is unavailable: {}".format(label, exc)) from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise ScanFailure("{} must be a regular non-symlink file".format(label))
    if executable and not os.access(str(path), os.X_OK):
        raise ScanFailure("{} is not executable".format(label))
    if not os.access(str(path), os.R_OK):
        raise ScanFailure("{} is not readable".format(label))
    return status


def _sha256_file(path: Path, deadline: Optional[float] = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            if deadline is not None and time.monotonic() >= deadline:
                raise ScanFailure("global scan deadline expired while hashing inputs")
    return digest.hexdigest()


def _file_identity(path: Path, deadline: Optional[float] = None) -> FileIdentity:
    status = _require_regular_file(path, str(path))
    return FileIdentity(str(path), status.st_size, _sha256_file(path, deadline))


def _canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_atomic_new(path: Path, payload: bytes, mode: int = 0o444) -> FileIdentity:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ScanFailure("refusing to overwrite {}".format(path))
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=".{}.".format(path.name), suffix=".tmp"
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ScanFailure("refusing to overwrite {}".format(path)) from exc
        os.unlink(temporary)
        temporary = ""
        directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return _file_identity(path)


@contextmanager
def _atomic_text_stream_new(path: Path, mode: int = 0o444) -> Iterator[TextIO]:
    """Yield a bounded-memory text stream and publish it without overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ScanFailure("refusing to overwrite {}".format(path))
    temporary = ""
    stream: Optional[TextIO] = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=str(path.parent), prefix=".{}.".format(path.name), suffix=".tmp"
        )
        stream = os.fdopen(descriptor, "w", encoding="utf-8", newline="")
        try:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            stream.close()
            stream = None
        os.chmod(temporary, mode)
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise ScanFailure("refusing to overwrite {}".format(path)) from exc
        os.unlink(temporary)
        temporary = ""
        directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if stream is not None:
            stream.close()
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _csv_bytes(fieldnames: Sequence[str], rows: Iterable[Mapping[str, object]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(fieldnames),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return stream.getvalue().encode("utf-8")


def _resolve_inside(path: Path, root: Path, label: str) -> Path:
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ScanFailure("{} escapes its authorized root".format(label)) from exc
    return resolved


def _parse_checksum_ledger(path: Path) -> Dict[str, str]:
    _require_regular_file(path, str(path))
    entries: Dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if len(raw) < 67 or raw[64:66] != "  ":
            raise ScanFailure("{}:{} malformed checksum entry".format(path, line_number))
        digest = raw[:64]
        relative = raw[66:]
        _sha_text(digest, "{}:{} digest".format(path, line_number))
        if not relative or "\x00" in relative or relative in entries:
            raise ScanFailure("{}:{} invalid or duplicate path".format(path, line_number))
        entries[relative] = digest
    if not entries:
        raise ScanFailure("{} is empty".format(path))
    return entries


def _validate_checksum_ledger(
    path: Path,
    expected_entries: Optional[Sequence[str]] = None,
    deadline: Optional[float] = None,
) -> Dict[str, FileIdentity]:
    entries = _parse_checksum_ledger(path)
    if expected_entries is not None and tuple(entries) != tuple(expected_entries):
        raise ScanFailure("{} entry order/content differs from frozen ledger".format(path))
    root = path.parent.resolve(strict=True)
    output: Dict[str, FileIdentity] = {}
    for relative, expected in entries.items():
        candidate = Path(relative)
        if candidate.is_absolute():
            raise ScanFailure("{} contains an absolute path".format(path))
        resolved = (root / candidate).resolve(strict=True)
        _require_regular_file(resolved, relative)
        actual = _sha256_file(resolved, deadline)
        if actual != expected:
            raise ScanFailure("checksum mismatch for {}".format(relative))
        output[relative] = FileIdentity(str(resolved), resolved.stat().st_size, actual)
    return output


def _run_git(repo: Path, arguments: Sequence[str]) -> str:
    try:
        completed = subprocess.run(
            ["git"] + list(arguments),
            cwd=str(repo),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScanFailure("git identity query failed: {}".format(exc)) from exc
    return completed.stdout.strip()


def _validate_entry(repo: Path, prereg: Mapping[str, object]) -> Mapping[str, str]:
    repository = _object(prereg.get("repository"), "prereg.repository")
    expected_cwd = _string(repository.get("cwd"), "prereg.repository.cwd")
    if repo != Path(expected_cwd).resolve(strict=True):
        raise ScanFailure("script repository root differs from preregistration")
    observed = {
        "cwd": _run_git(repo, ["rev-parse", "--show-toplevel"]),
        "branch": _run_git(repo, ["symbolic-ref", "--short", "HEAD"]),
        "head": _run_git(repo, ["rev-parse", "HEAD"]),
        "tree": _run_git(repo, ["rev-parse", "HEAD^{tree}"]),
    }
    for name in ("cwd", "branch", "head", "tree"):
        expected = _string(repository.get(name), "prereg.repository.{}".format(name))
        observed_value = observed[name]
        if name == "cwd":
            observed_value = str(Path(observed_value).resolve(strict=True))
            expected = str(Path(expected).resolve(strict=True))
        if observed_value != expected:
            raise ScanFailure(
                "repository {} differs: expected {!r}, observed {!r}".format(
                    name, expected, observed_value
                )
            )
    return observed


def _forbid_input_terms(value: str, label: str) -> None:
    lowered = value.lower()
    if any(token in lowered for token in FORBIDDEN_INPUT_TOKENS):
        raise ScanFailure("{} contains a forbidden input term".format(label))


def _validate_manifest(
    repo: Path,
    manifest_path: Path,
    prereg: Mapping[str, object],
    deadline: float,
) -> Tuple[Mapping[str, object], Dict[str, RunIdentity], FileIdentity]:
    manifest_identity = _file_identity(manifest_path, deadline)
    entry = _object(prereg.get("entry"), "prereg.entry")
    expected_manifest_hash = _sha_text(
        entry.get("event_ready_manifest_sha256"),
        "prereg.entry.event_ready_manifest_sha256",
    )
    if manifest_identity.sha256 != expected_manifest_hash:
        raise ScanFailure("event-ready manifest hash differs from preregistration")
    manifest = _object(
        _strict_json_file(manifest_path, "event-ready corpus manifest"),
        "event-ready corpus manifest",
    )
    runs = _object(manifest.get("runs"), "manifest.runs")
    primary = tuple(
        _string(value, "manifest.primary_run_ids[]")
        for value in _list(manifest.get("primary_run_ids"), "manifest.primary_run_ids")
    )
    if not all(run in primary for run in FIXED_RUNS):
        raise ScanFailure("a frozen P1 run is absent from primary_run_ids")

    public_archive_root = (
        repo / "artifacts/turnsafe/session_01e/replay/post_mh_campaign"
    ).resolve(strict=True)
    identities: Dict[str, RunIdentity] = {}
    for run in FIXED_RUNS:
        _forbid_input_terms(run, "run identity")
        record = _object(runs.get(run), "manifest.runs[{}]".format(run))
        sequence = _string(record.get("sequence"), "{}.sequence".format(run))
        _forbid_input_terms(sequence, "{}.sequence".format(run))
        if sequence != RUN_SEQUENCE[run]:
            raise ScanFailure("{} sequence identity differs".format(run))
        if _string(record.get("run_role"), "{}.run_role".format(run)) != "CAPTURE_ON_PRIMARY":
            raise ScanFailure("{} is not a capture-on primary run".format(run))
        if _string(record.get("dataset_scope"), "{}.dataset_scope".format(run)) != "KAIST_DEVELOPMENT":
            raise ScanFailure("{} is not in the public development scope".format(run))
        sequence_order = _integer(record.get("sequence_order"), "{}.sequence_order".format(run))
        if sequence_order != RUN_SEQUENCE_ORDER[run]:
            raise ScanFailure("{} sequence order differs".format(run))

        archive_block = _object(record.get("archive"), "{}.archive".format(run))
        if _string(archive_block.get("algorithm"), "{}.archive.algorithm".format(run)) != "zstd":
            raise ScanFailure("{} archive algorithm differs".format(run))
        archive = _object(archive_block.get("archive"), "{}.archive.archive".format(run))
        raw = _object(archive_block.get("raw"), "{}.archive.raw".format(run))
        if _boolean(archive.get("executable"), "{}.archive.executable".format(run)):
            raise ScanFailure("{} archive is unexpectedly executable".format(run))
        archive_path_text = _string(archive.get("path"), "{}.archive.path".format(run))
        _forbid_input_terms(archive_path_text, "{}.archive.path".format(run))
        archive_path = Path(archive_path_text)
        expected_path = public_archive_root / run / "t0_events.jsonl.zst"
        resolved_path = _resolve_inside(archive_path, public_archive_root, "archive")
        if resolved_path != expected_path.resolve(strict=True):
            raise ScanFailure("{} archive path differs from fixed public path".format(run))
        status = _require_regular_file(resolved_path, "{} archive".format(run))
        if status.st_mode & 0o111:
            raise ScanFailure("{} archive has executable permission bits".format(run))
        archive_size = _integer(archive.get("size_bytes"), "{}.archive.size_bytes".format(run))
        if status.st_size != archive_size:
            raise ScanFailure("{} archive size differs from manifest".format(run))
        archive_sha = _sha_text(archive.get("sha256"), "{}.archive.sha256".format(run))
        observed_sha = _sha256_file(resolved_path, deadline)
        if observed_sha != archive_sha:
            raise ScanFailure("{} archive SHA-256 differs from manifest".format(run))

        stream = _object(record.get("stream"), "{}.stream".format(run))
        raw_size = _integer(raw.get("size_bytes"), "{}.raw.size_bytes".format(run))
        raw_sha = _sha_text(raw.get("sha256"), "{}.raw.sha256".format(run))
        if _integer(stream.get("decoded_size_bytes"), "{}.stream.decoded_size_bytes".format(run)) != raw_size:
            raise ScanFailure("{} raw/stream sizes differ".format(run))
        if _sha_text(stream.get("decoded_sha256"), "{}.stream.decoded_sha256".format(run)) != raw_sha:
            raise ScanFailure("{} raw/stream hashes differ".format(run))
        if not _boolean(stream.get("bounded_record_streaming"), "{}.stream.bounded_record_streaming".format(run)):
            raise ScanFailure("{} stream is not bounded".format(run))
        record_count = _integer(stream.get("record_count"), "{}.stream.record_count".format(run))
        counts = _object(record.get("counts"), "{}.counts".format(run))
        callback_count = _integer(counts.get("callbacks"), "{}.counts.callbacks".format(run))
        if record_count != callback_count + 1:
            raise ScanFailure("{} record count is not header plus callbacks".format(run))
        identities[run] = RunIdentity(
            run=run,
            sequence=sequence,
            sequence_order=sequence_order,
            callback_count=callback_count,
            archive=resolved_path,
            archive_size=archive_size,
            archive_sha256=archive_sha,
            raw_size=raw_size,
            raw_sha256=raw_sha,
            record_count=record_count,
        )
    return manifest, identities, manifest_identity


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_process_group_gone(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _terminate_process_group(process: subprocess.Popen, first_signal: int) -> None:
    if process.poll() is None or _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, first_signal)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=PROCESS_TERM_GRACE_SECONDS)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if not _wait_process_group_gone(process.pid, PROCESS_TERM_GRACE_SECONDS):
        raise ScanFailure("worker process group survived SIGKILL cleanup")


def _worker_command(
    worker: Path,
    identity: RunIdentity,
    callback_csv: Path,
    metadata_json: Path,
) -> List[str]:
    return [
        str(worker),
        "--sequence",
        identity.sequence,
        "--run",
        identity.run,
        "--archive",
        str(identity.archive),
        "--expected-archive-sha256",
        identity.archive_sha256,
        "--expected-archive-size",
        str(identity.archive_size),
        "--expected-raw-sha256",
        identity.raw_sha256,
        "--expected-raw-size",
        str(identity.raw_size),
        "--expected-record-count",
        str(identity.record_count),
        "--output-csv",
        str(callback_csv),
        "--output-metadata",
        str(metadata_json),
    ]


def _run_one_worker(
    worker: Path,
    identity: RunIdentity,
    ordinal: int,
    repeat: bool,
    output_root: Path,
    worker_timeout: int,
    deadline: float,
) -> WorkerScan:
    label = "{:02d}_{}{}".format(ordinal, identity.run, "_repeat" if repeat else "")
    run_directory = output_root / "runs" / label
    run_directory.mkdir(parents=True, exist_ok=False)
    callback_csv = run_directory / "callbacks.csv"
    metadata_json = run_directory / "metadata.json"
    stdout_path = run_directory / "worker.stdout.log"
    stderr_path = run_directory / "worker.stderr.log"
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
        raise ScanFailure("global scan deadline expired before {}".format(label))
    timeout = min(float(worker_timeout), remaining)
    command = _worker_command(worker, identity, callback_csv, metadata_json)
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        try:
            process = subprocess.Popen(
                command,
                cwd=str(output_root.parent.parent.parent),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            raise ScanFailure("failed to launch {}: {}".format(label, exc)) from exc
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process, signal.SIGTERM)
            raise ScanFailure("{} exceeded {:.3f} seconds".format(label, timeout)) from exc
        except BaseException:
            _terminate_process_group(process, signal.SIGTERM)
            raise
        if _process_group_exists(process.pid):
            _terminate_process_group(process, signal.SIGTERM)
            raise ScanFailure("{} left a live process-group member".format(label))
        if return_code != 0:
            raise ScanFailure("{} worker exited {}".format(label, return_code))

    for path in (callback_csv, metadata_json, stdout_path, stderr_path):
        os.chmod(path, 0o444)
    callback_identity = _file_identity(callback_csv, deadline)
    metadata_identity = _file_identity(metadata_json, deadline)
    stdout_identity = _file_identity(stdout_path, deadline)
    stderr_identity = _file_identity(stderr_path, deadline)
    return WorkerScan(
        ordinal=ordinal,
        label=label,
        run=identity.run,
        repeat=repeat,
        callback_csv=callback_csv,
        metadata_json=metadata_json,
        callback_identity=callback_identity,
        metadata_identity=metadata_identity,
        stdout_identity=stdout_identity,
        stderr_identity=stderr_identity,
    )


def _canonical_uint(text: str, label: str) -> int:
    if not text or not text.isascii() or not text.isdecimal():
        raise ScanFailure("{} is not an unsigned decimal integer".format(label))
    value = int(text)
    if str(value) != text:
        raise ScanFailure("{} is not canonical decimal".format(label))
    return value


def _finite_csv_float(text: str, label: str) -> float:
    if not text:
        raise ScanFailure("{} is unavailable".format(label))
    try:
        value = float(text)
    except ValueError as exc:
        raise ScanFailure("{} is not numeric".format(label)) from exc
    if not math.isfinite(value):
        raise ScanFailure("{} is nonfinite".format(label))
    return value


def _csv_bool(text: str, label: str, allow_empty: bool = False) -> Optional[bool]:
    if allow_empty and text == "":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    raise ScanFailure("{} is not canonical Boolean".format(label))


def _parse_embedded_json(text: str, expected: type, label: str) -> object:
    value = _strict_json_bytes(text.encode("utf-8"), label)
    if not isinstance(value, expected):
        raise ScanFailure("{} has the wrong JSON type".format(label))
    return value


def _validate_stage_counters(row: Mapping[str, str], label: str) -> Dict[str, int]:
    counters = {name: _canonical_uint(row[name], "{}.{}".format(label, name)) for name in COUNTER_COLUMNS}
    member_chain = (
        "raw_members",
        "stereo_valid_members",
        "range_lcb_valid_members",
        "translation_ucb_valid_members",
        "acute_members",
        "rho_passed_members",
    )
    group_chain = (
        "raw_groups",
        "typed_groups",
        "stereo_valid_groups",
        "range_lcb_valid_groups",
        "translation_ucb_valid_groups",
        "acute_groups",
        "rho_groups",
        "groups_n_ge_4",
        "consensus_groups",
        "spatial_groups",
        "rank_groups",
        "conditioning_groups",
        "winner_count",
    )
    for chain in (member_chain, group_chain):
        for left, right in zip(chain, chain[1:]):
            if counters[right] > counters[left]:
                raise ScanFailure("{} stage count {} exceeds {}".format(label, right, left))
    if counters["winner_count"] > 1:
        raise ScanFailure("{} has more than one pre-NIS winner".format(label))
    for name in (
        "winner_nis_evaluated_count",
        "winner_nis_pass_count",
        "winner_information_non_negligible_count",
    ):
        if counters[name] > counters["winner_count"]:
            raise ScanFailure("{} lifecycle count {} exceeds winner count".format(label, name))
    if counters["winner_nis_pass_count"] > counters["winner_nis_evaluated_count"]:
        raise ScanFailure("{} NIS-pass count exceeds NIS-evaluated count".format(label))
    return counters


def _validate_worker_metadata(
    scan: WorkerScan,
    identity: RunIdentity,
    header: Sequence[str],
    row_count: int,
    observed_totals: Mapping[str, int],
    observed_rejection_reasons: Mapping[str, int],
) -> Mapping[str, object]:
    metadata = _object(
        _strict_json_file(scan.metadata_json, "{} metadata".format(scan.label)),
        "{} metadata".format(scan.label),
    )
    schema = _string(metadata.get("schema_version"), "metadata.schema_version")
    if schema != "turnsafe.t1.p1.shadow_scan_metadata.v1":
        raise ScanFailure("{} metadata schema differs".format(scan.label))
    if _string(metadata.get("status"), "metadata.status") != "COMPLETE":
        raise ScanFailure("{} metadata is not complete".format(scan.label))
    if _string(metadata.get("run"), "metadata.run") != identity.run:
        raise ScanFailure("{} metadata run differs".format(scan.label))
    if _string(metadata.get("sequence"), "metadata.sequence") != identity.sequence:
        raise ScanFailure("{} metadata sequence differs".format(scan.label))

    archive = _object(metadata.get("archive_identity"), "metadata.archive_identity")
    if Path(_string(archive.get("path"), "metadata.archive_identity.path")).resolve(strict=True) != identity.archive:
        raise ScanFailure("{} metadata archive path differs".format(scan.label))
    for prefix, expected in (
        ("sha256", identity.archive_sha256),
        ("size_bytes", identity.archive_size),
    ):
        parser = _sha_text if prefix == "sha256" else _integer
        if parser(archive.get("expected_" + prefix), "metadata.archive_identity.expected_" + prefix) != expected:
            raise ScanFailure("{} metadata expected archive identity differs".format(scan.label))
        if parser(archive.get("actual_" + prefix), "metadata.archive_identity.actual_" + prefix) != expected:
            raise ScanFailure("{} metadata actual archive identity differs".format(scan.label))
    if not _boolean(archive.get("verified"), "metadata.archive_identity.verified"):
        raise ScanFailure("{} metadata did not verify archive".format(scan.label))

    raw = _object(metadata.get("raw_identity"), "metadata.raw_identity")
    for prefix, expected in (
        ("sha256", identity.raw_sha256),
        ("size_bytes", identity.raw_size),
    ):
        parser = _sha_text if prefix == "sha256" else _integer
        if parser(raw.get("expected_" + prefix), "metadata.raw_identity.expected_" + prefix) != expected:
            raise ScanFailure("{} metadata expected raw identity differs".format(scan.label))
        if parser(raw.get("actual_" + prefix), "metadata.raw_identity.actual_" + prefix) != expected:
            raise ScanFailure("{} metadata actual raw identity differs".format(scan.label))
    if not _boolean(raw.get("verified"), "metadata.raw_identity.verified"):
        raise ScanFailure("{} metadata did not verify raw stream".format(scan.label))

    records = _object(metadata.get("records"), "metadata.records")
    if _integer(records.get("expected_record_count"), "metadata.records.expected_record_count") != identity.record_count:
        raise ScanFailure("{} metadata expected record count differs".format(scan.label))
    if _integer(records.get("actual_record_count"), "metadata.records.actual_record_count") != identity.record_count:
        raise ScanFailure("{} metadata actual record count differs".format(scan.label))
    if _integer(records.get("callback_count"), "metadata.records.callback_count") != row_count:
        raise ScanFailure("{} metadata callback count differs".format(scan.label))
    if _integer(records.get("run_header_count"), "metadata.records.run_header_count") != 1:
        raise ScanFailure("{} metadata run-header count differs".format(scan.label))

    csv_identity = _object(metadata.get("callback_csv"), "metadata.callback_csv")
    if Path(_string(csv_identity.get("path"), "metadata.callback_csv.path")).resolve(strict=True) != scan.callback_csv:
        raise ScanFailure("{} metadata CSV path differs".format(scan.label))
    if _integer(csv_identity.get("size_bytes"), "metadata.callback_csv.size_bytes") != scan.callback_identity.size_bytes:
        raise ScanFailure("{} metadata CSV size differs".format(scan.label))
    if _sha_text(csv_identity.get("sha256"), "metadata.callback_csv.sha256") != scan.callback_identity.sha256:
        raise ScanFailure("{} metadata CSV hash differs".format(scan.label))
    if _integer(csv_identity.get("row_count"), "metadata.callback_csv.row_count") != row_count:
        raise ScanFailure("{} metadata CSV row count differs".format(scan.label))
    if _integer(csv_identity.get("column_count"), "metadata.callback_csv.column_count") != len(header):
        raise ScanFailure("{} metadata CSV column count differs".format(scan.label))
    if _string(csv_identity.get("row_schema_version"), "metadata.callback_csv.row_schema_version") != EXPECTED_CALLBACK_SCHEMA:
        raise ScanFailure("{} metadata row schema differs".format(scan.label))
    run_header = _object(
        metadata.get("run_header_identity"), "metadata.run_header_identity"
    )
    for name in (
        "build_provenance_id",
        "calibration_sha256",
        "config_sha256",
        "frozen_base_sha",
        "source_sha",
        "source_snapshot_sha256",
        "tree_sha",
    ):
        _string(run_header.get(name), "metadata.run_header_identity." + name)

    validation = _object(metadata.get("validation"), "metadata.validation")
    for name in (
        "archive_streamed_with_libzstd",
        "archive_sha256_openssl",
        "raw_sha256_openssl",
        "duplicate_json_keys_rejected",
        "recursive_nonfinite_rejected",
        "schema_and_identity_audited",
        "jpl_xyzw_repository_rotation_audited",
        "calibration_hashes_audited",
        "clone_cross_covariance_blocks_audited",
        "newline_terminated_jsonl",
        "one_row_per_callback",
    ):
        if not _boolean(validation.get(name), "metadata.validation." + name):
            raise ScanFailure(
                "{} metadata validation {} is false".format(scan.label, name)
            )
    if _integer(
        validation.get("maximum_jsonl_record_bytes"),
        "metadata.validation.maximum_jsonl_record_bytes",
    ) != 256 * 1024 * 1024:
        raise ScanFailure("{} metadata record bound differs".format(scan.label))

    totals = _object(metadata.get("totals"), "metadata.totals")
    for name in (
        "accepted_winner_callbacks",
        "certified_winner_callbacks",
        "nis_passing_callbacks",
        "winner_nis_evaluated_count",
        "winner_nis_pass_count",
        "winner_information_non_negligible_count",
        "provenance_available_callbacks",
        "provenance_group_count",
        "provenance_member_count",
    ):
        actual = _integer(totals.get(name), "metadata.totals." + name)
        if actual != observed_totals.get(name, 0):
            raise ScanFailure(
                "{} metadata total {} differs from callback rows".format(
                    scan.label, name
                )
            )
    metadata_reasons = _object(
        totals.get("rejection_reasons"), "metadata.totals.rejection_reasons"
    )
    parsed_metadata_reasons: Dict[str, int] = {}
    for reason, count in metadata_reasons.items():
        parsed_metadata_reasons[_string(reason, "metadata rejection reason")] = (
            _integer(count, "metadata rejection count")
        )
    if parsed_metadata_reasons != dict(observed_rejection_reasons):
        raise ScanFailure(
            "{} metadata rejection totals differ from callback rows".format(
                scan.label
            )
        )

    hard_stop = _object(metadata.get("hard_stop"), "metadata.hard_stop")
    for name in (
        "live_estimator_types_absent",
        "live_proposal_calls_absent",
        "state_mutation_absent",
    ):
        if not _boolean(hard_stop.get(name), "metadata.hard_stop." + name):
            raise ScanFailure(
                "{} violates the P1 hard stop at {}".format(scan.label, name)
            )
    if _integer(
        hard_stop.get("t1_rows_emitted_to_estimator"),
        "metadata.hard_stop.t1_rows_emitted_to_estimator",
    ) != 0:
        raise ScanFailure("{} emitted a live T1 estimator row".format(scan.label))
    return metadata


def _consume_worker_csv(
    scan: WorkerScan,
    identity: RunIdentity,
    expected_header: Optional[Sequence[str]],
    consumer: Optional[Callable[[Mapping[str, str]], None]] = None,
    deadline: Optional[float] = None,
) -> Tuple[List[str], int, Mapping[str, object]]:
    current_identity = _file_identity(scan.callback_csv, deadline)
    if current_identity != scan.callback_identity:
        raise ScanFailure("{} callback CSV identity changed".format(scan.label))
    with scan.callback_csv.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ScanFailure("{} has a missing or duplicate CSV header".format(scan.label))
        header = list(reader.fieldnames)
        if header != list(WORKER_COLUMNS):
            raise ScanFailure("{} worker CSV header differs from frozen schema".format(scan.label))
        if expected_header is not None and header != list(expected_header):
            raise ScanFailure("{} worker CSV header differs across scans".format(scan.label))
        row_count = 0
        previous_index: Optional[int] = None
        observed_totals: Counter = Counter()
        observed_rejection_reasons: Counter = Counter()
        for row_number, raw in enumerate(reader, 2):
            if deadline is not None and (row_number & 127) == 0 and time.monotonic() >= deadline:
                raise ScanFailure("global scan deadline expired during CSV aggregation")
            if None in raw or any(value is None for value in raw.values()):
                raise ScanFailure("{}:{} has malformed CSV width".format(scan.callback_csv, row_number))
            row = {key: str(value) for key, value in raw.items()}
            label = "{}:{}".format(scan.label, row_number)
            if row["schema_version"] != EXPECTED_CALLBACK_SCHEMA:
                raise ScanFailure("{} callback schema differs".format(label))
            if row["run"] != identity.run or row["sequence"] != identity.sequence:
                raise ScanFailure("{} run/sequence identity differs".format(label))
            callback_index = _canonical_uint(row["callback_index"], label + ".callback_index")
            if callback_index != row_count:
                raise ScanFailure("{} callback indices are not contiguous from zero".format(scan.label))
            if previous_index is not None and callback_index != previous_index + 1:
                raise ScanFailure("{} callback indices are not contiguous".format(scan.label))
            previous_index = callback_index
            _finite_csv_float(row["callback_timestamp_value"], label + ".callback_timestamp_value")
            if not row["callback_timestamp_key"]:
                raise ScanFailure("{}.callback_timestamp_key is empty".format(label))
            counters = _validate_stage_counters(row, label)
            eligible_groups = _canonical_uint(
                row["eligible_group_count"], label + ".eligible_group_count"
            )
            if eligible_groups > counters["conditioning_groups"]:
                raise ScanFailure("{} eligible groups exceed conditioning survivors".format(label))
            if counters["foregone_eligible_groups"] + counters["winner_count"] != eligible_groups:
                raise ScanFailure("{} winner/foregone eligible accounting differs".format(label))
            certified = _csv_bool(row["certified_winner_available"], label + ".certified_winner_available")
            frozen = _csv_bool(row["post_nis_frozen_winner_available"], label + ".post_nis_frozen_winner_available")
            accepted = _csv_bool(row["post_nis_accepted_winner_available"], label + ".post_nis_accepted_winner_available")
            promoted = _csv_bool(row["post_nis_runner_up_promoted"], label + ".post_nis_runner_up_promoted")
            if promoted:
                raise ScanFailure("{} promoted a post-NIS runner-up".format(label))
            if certified != (counters["winner_count"] == 1) or frozen != certified:
                raise ScanFailure("{} winner lifecycle is inconsistent".format(label))
            if accepted and not certified:
                raise ScanFailure("{} accepted a winner without a certificate".format(label))
            reasons = _parse_embedded_json(row["rejection_reasons_json"], dict, label + ".rejection_reasons_json")
            for reason, count in reasons.items():
                _string(reason, label + ".rejection reason")
                observed_rejection_reasons[reason] += _integer(
                    count, label + ".rejection count"
                )
            _parse_embedded_json(row["group_diagnostics_json"], list, label + ".group_diagnostics_json")
            _parse_embedded_json(row["candidate_diagnostics_json"], list, label + ".candidate_diagnostics_json")
            _parse_embedded_json(row["winner_candidate_diagnostics_json"], list, label + ".winner_candidate_diagnostics_json")
            if certified:
                for name in WINNER_FLOAT_COLUMNS:
                    _finite_csv_float(row[name], label + "." + name)
                for name in WINNER_UINT_COLUMNS:
                    _canonical_uint(row[name], label + "." + name)
                for name in ("winner_source_timestamp_key", "winner_target_timestamp_key"):
                    if not row[name]:
                        raise ScanFailure("{}.{} is empty".format(label, name))
                for name in (
                    "winner_group_status",
                    "winner_terminal_reason",
                    "winner_selection_role",
                    "winner_factor_status",
                ):
                    if not row[name]:
                        raise ScanFailure("{}.{} is empty".format(label, name))
                if (
                    row["winner_group_status"] != "ELIGIBLE"
                    or row["winner_terminal_reason"] != "NONE"
                    or row["winner_selection_role"] != "WINNER"
                ):
                    raise ScanFailure("{} frozen winner role/status differs".format(label))
                if _finite_csv_float(
                    row["winner_minimum_acute_margin"],
                    label + ".winner_minimum_acute_margin",
                ) <= 0.0:
                    raise ScanFailure("{} winner violates strict acute margin".format(label))
                if _finite_csv_float(
                    row["winner_minimum_rho_margin"],
                    label + ".winner_minimum_rho_margin",
                ) < 0.0:
                    raise ScanFailure("{} winner violates inclusive rho bound".format(label))
                retained = _canonical_uint(
                    row["winner_retained_member_count"],
                    label + ".winner_retained_member_count",
                )
                raw_members = _canonical_uint(
                    row["winner_raw_member_count"],
                    label + ".winner_raw_member_count",
                )
                required_retained = max(3, (3 * raw_members + 3) // 4)
                if (
                    raw_members < 4
                    or retained < required_retained
                    or required_retained
                    != _canonical_uint(
                        row["winner_consensus_required_retained_count"],
                        label + ".winner_consensus_required_retained_count",
                    )
                    or retained != _canonical_uint(
                        row["winner_score_retained_feature_count"],
                        label + ".winner_score_retained_feature_count",
                    )
                ):
                    raise ScanFailure("{} winner retained-member accounting differs".format(label))
                if not (
                    _finite_csv_float(
                        row["winner_spatial_convex_hull_area"],
                        label + ".winner_spatial_convex_hull_area",
                    )
                    >= 0.1
                    and _finite_csv_float(
                        row[
                            "winner_spatial_minimum_population_covariance_eigenvalue"
                        ],
                        label
                        + ".winner_spatial_minimum_population_covariance_eigenvalue",
                    )
                    >= 0.03
                    and _canonical_uint(
                        row["winner_spatial_occupied_fixed_4x4_cells"],
                        label + ".winner_spatial_occupied_fixed_4x4_cells",
                    )
                    >= 4
                    and _finite_csv_float(
                        row["winner_spatial_x_span"],
                        label + ".winner_spatial_x_span",
                    )
                    >= 0.6
                    and _finite_csv_float(
                        row["winner_spatial_y_span"],
                        label + ".winner_spatial_y_span",
                    )
                    >= 0.6
                    and _canonical_uint(
                        row["winner_rotation_stack_rank"],
                        label + ".winner_rotation_stack_rank",
                    )
                    == 3
                    and _finite_csv_float(
                        row["winner_rotation_stack_condition_ratio"],
                        label + ".winner_rotation_stack_condition_ratio",
                    )
                    >= 0.15
                ):
                    raise ScanFailure("{} winner violates a frozen group gate".format(label))
                maximum_rho = _finite_csv_float(
                    row["winner_score_maximum_retained_rho_trans"],
                    label + ".winner_score_maximum_retained_rho_trans",
                )
                if maximum_rho < 0.0 or maximum_rho > 0.5:
                    raise ScanFailure("{} winner score violates the rho gate".format(label))
                predicted_trace = _finite_csv_float(
                    row["winner_predicted_information_trace"],
                    label + ".winner_predicted_information_trace",
                )
                predicted_non_negligible = _csv_bool(
                    row["winner_predicted_information_non_negligible"],
                    label + ".winner_predicted_information_non_negligible",
                )
                if predicted_non_negligible != (
                    predicted_trace > PREDICTED_INFORMATION_TRACE_FLOOR
                ):
                    raise ScanFailure(
                        "{} predicted-information flag differs from frozen floor".format(
                            label
                        )
                    )
                nis_fields = (
                    "winner_nis",
                    "winner_nis_degrees_of_freedom",
                    "winner_nis_threshold",
                    "winner_nis_passed",
                )
                nis_field_presence = tuple(bool(row[name]) for name in nis_fields)
                if any(nis_field_presence) != all(nis_field_presence):
                    raise ScanFailure("{} has a partial serialized NIS".format(label))
                nis_evaluated = counters["winner_nis_evaluated_count"] == 1
                if all(nis_field_presence) != nis_evaluated:
                    raise ScanFailure(
                        "{} NIS field availability differs from evaluated count".format(
                            label
                        )
                    )
                nis_passed: Optional[bool] = None
                if nis_evaluated:
                    nis = _finite_csv_float(
                        row["winner_nis"], label + ".winner_nis"
                    )
                    if nis < 0.0:
                        raise ScanFailure("{} NIS is negative".format(label))
                    nis_degrees_of_freedom = _canonical_uint(
                        row["winner_nis_degrees_of_freedom"],
                        label + ".winner_nis_degrees_of_freedom",
                    )
                    if nis_degrees_of_freedom != 2 * retained:
                        raise ScanFailure(
                            "{} NIS degrees of freedom differs from twice retained members".format(
                                label
                            )
                        )
                    nis_threshold = _finite_csv_float(
                        row["winner_nis_threshold"],
                        label + ".winner_nis_threshold",
                    )
                    if nis_threshold <= 0.0:
                        raise ScanFailure("{} NIS threshold is not positive".format(label))
                    nis_passed = _csv_bool(
                        row["winner_nis_passed"], label + ".winner_nis_passed"
                    )
                    if counters["winner_nis_pass_count"] != int(nis_passed):
                        raise ScanFailure("{} serialized NIS differs from lifecycle counters".format(label))
                checked_fields = CHECKED_INFORMATION_FLOAT_COLUMNS + (
                    "winner_information_non_negligible",
                )
                checked_presence = tuple(bool(row[name]) for name in checked_fields)
                if any(checked_presence) != all(checked_presence):
                    raise ScanFailure("{} has partial checked information".format(label))
                checked_present = all(checked_presence)
                factor_accepted = row["winner_factor_status"] == "accepted"
                if factor_accepted and not nis_evaluated:
                    raise ScanFailure(
                        "{} accepted factor lacks an evaluated NIS".format(label)
                    )
                if checked_present != factor_accepted:
                    raise ScanFailure(
                        "{} checked information availability differs from factor status".format(
                            label
                        )
                    )
                if checked_present:
                    for name in CHECKED_INFORMATION_FLOAT_COLUMNS:
                        _finite_csv_float(row[name], label + "." + name)
                    checked_non_negligible = _csv_bool(
                        row["winner_information_non_negligible"],
                        label + ".winner_information_non_negligible",
                    )
                    if counters["winner_information_non_negligible_count"] != int(
                        checked_non_negligible
                    ):
                        raise ScanFailure("{} checked information differs from lifecycle counter".format(label))
                elif counters["winner_information_non_negligible_count"] != 0:
                    raise ScanFailure(
                        "{} information counter is set without checked information".format(
                            label
                        )
                    )
                if accepted != (factor_accepted and nis_passed is True):
                    raise ScanFailure(
                        "{} post-NIS acceptance differs from factor/NIS lifecycle".format(
                            label
                        )
                    )
            elif any(row[name] for name in WORKER_COLUMNS[WORKER_COLUMNS.index("winner_camera_id"):WORKER_COLUMNS.index("post_nis_frozen_winner_available")]):
                raise ScanFailure("{} has winner diagnostics without a certified winner".format(label))

            observed_totals["certified_winner_callbacks"] += int(certified)
            observed_totals["accepted_winner_callbacks"] += int(accepted)
            observed_totals["nis_passing_callbacks"] += int(
                row["winner_nis_passed"] == "true"
            )
            observed_totals["winner_nis_evaluated_count"] += counters[
                "winner_nis_evaluated_count"
            ]
            observed_totals["winner_nis_pass_count"] += counters[
                "winner_nis_pass_count"
            ]
            observed_totals["winner_information_non_negligible_count"] += counters[
                "winner_information_non_negligible_count"
            ]
            observed_totals["provenance_available_callbacks"] += int(
                row["provenance_status"] == "AVAILABLE"
            )
            observed_totals["provenance_group_count"] += counters["typed_groups"]
            observed_totals["provenance_member_count"] += counters["raw_members"]
            if consumer is not None:
                consumer(row)
            row_count += 1
    if row_count != identity.callback_count:
        raise ScanFailure(
            "{} produced {} callbacks; manifest requires {}".format(
                scan.label, row_count, identity.callback_count
            )
        )
    if _file_identity(scan.callback_csv, deadline) != scan.callback_identity:
        raise ScanFailure("{} callback CSV changed while streaming".format(scan.label))
    if _file_identity(scan.metadata_json, deadline) != scan.metadata_identity:
        raise ScanFailure("{} metadata identity changed".format(scan.label))
    metadata = _validate_worker_metadata(
        scan,
        identity,
        header,
        row_count,
        observed_totals,
        observed_rejection_reasons,
    )
    return header, row_count, metadata


def _worker_csv_header(
    scan: WorkerScan, expected_header: Optional[Sequence[str]]
) -> List[str]:
    with scan.callback_csv.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ScanFailure("{} has a missing or duplicate CSV header".format(scan.label))
        header = list(reader.fieldnames)
    if header != list(WORKER_COLUMNS):
        raise ScanFailure("{} worker CSV header differs from frozen schema".format(scan.label))
    if expected_header is not None and header != list(expected_header):
        raise ScanFailure("{} worker CSV header differs across scans".format(scan.label))
    return header


def _exact_file_equal(
    left: Path, right: Path, deadline: Optional[float] = None
) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as first, right.open("rb") as second:
        while True:
            left_chunk = first.read(1024 * 1024)
            right_chunk = second.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                raise ScanFailure("global scan deadline expired during repeat comparison")


def _coverage_property_map(xml_path: Path) -> Tuple[Mapping[Tuple[str, str], Mapping[str, str]], FileIdentity]:
    identity = _file_identity(xml_path)
    if identity.size_bytes > 16 * 1024 * 1024:
        raise ScanFailure("certificate test XML is unexpectedly large")
    payload = xml_path.read_bytes()
    if b"<!DOCTYPE" in payload or b"<!ENTITY" in payload:
        raise ScanFailure("certificate test XML contains a forbidden declaration")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ScanFailure("certificate test XML is malformed") from exc
    if root.tag != "testsuites":
        raise ScanFailure("certificate test XML root is not testsuites")
    for attribute in ("failures", "disabled", "errors"):
        if _canonical_uint(root.attrib.get(attribute, ""), "testsuites." + attribute) != 0:
            raise ScanFailure("certificate test XML contains failed/disabled/error tests")
    output: Dict[Tuple[str, str], Mapping[str, str]] = {}
    for suite in root.findall("testsuite"):
        suite_name = suite.attrib.get("name", "")
        for case in suite.findall("testcase"):
            if case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None:
                raise ScanFailure("certificate test XML contains an unsuccessful testcase")
            classname = case.attrib.get("classname", suite_name)
            name = case.attrib.get("name", "")
            key = (classname, name)
            if not classname or not name or key in output:
                raise ScanFailure("certificate test XML has a duplicate/unnamed testcase")
            properties: Dict[str, str] = {}
            container = case.find("properties")
            if container is not None:
                for prop in container.findall("property"):
                    prop_name = prop.attrib.get("name", "")
                    prop_value = prop.attrib.get("value", "")
                    if not prop_name or prop_name in properties:
                        raise ScanFailure("certificate testcase has duplicate property")
                    properties[prop_name] = prop_value
            output[key] = properties
    return output, identity


def _test_properties(
    properties: Mapping[Tuple[str, str], Mapping[str, str]],
    suite: str,
    test: str,
    required: Sequence[str],
) -> Mapping[str, str]:
    key = (suite, test)
    if key not in properties:
        raise ScanFailure("certificate XML is missing {}.{}".format(suite, test))
    output = properties[key]
    missing = [name for name in required if name not in output]
    if missing:
        raise ScanFailure("{}.{} is missing properties {}".format(suite, test, missing))
    return output


def _clopper_pearson(successes: int, samples: int) -> Tuple[float, float, str]:
    if samples <= 0 or successes < 0 or successes > samples:
        raise ScanFailure("invalid binomial coverage count")
    try:
        import scipy
        from scipy.stats import beta
    except ImportError as exc:
        raise ScanFailure("scipy is required for exact Clopper-Pearson intervals") from exc
    if scipy.__version__ != "1.10.1":
        raise ScanFailure(
            "exact Clopper-Pearson provider differs from scipy 1.10.1"
        )
    alpha = 0.05
    lower = 0.0 if successes == 0 else float(beta.ppf(alpha / 2.0, successes, samples - successes + 1))
    upper = 1.0 if successes == samples else float(beta.ppf(1.0 - alpha / 2.0, successes + 1, samples - successes))
    if not math.isfinite(lower) or not math.isfinite(upper):
        raise ScanFailure("Clopper-Pearson beta quantile is nonfinite")
    return lower, upper, scipy.__version__


def _coverage_record(
    successes: int,
    samples: int,
    declared: float,
    scipy_versions: set,
) -> Mapping[str, object]:
    lower, upper, scipy_version = _clopper_pearson(successes, samples)
    scipy_versions.add(scipy_version)
    coverage = successes / samples
    return {
        "covered_samples": successes,
        "denominator_samples": samples,
        "empirical_coverage": coverage,
        "declared_minimum": declared,
        "point_estimate_meets_declared_minimum": coverage >= declared,
        "clopper_pearson_two_sided_95": {
            "confidence": 0.95,
            "lower": lower,
            "upper": upper,
        },
    }


def _certificate_coverage(xml_path: Path, prereg: Mapping[str, object]) -> Mapping[str, object]:
    properties, xml_identity = _coverage_property_map(xml_path)
    constants = _object(prereg.get("constants"), "prereg.constants")
    range_p = float(constants.get("range_component_confidence"))
    translation_p = float(constants.get("translation_component_confidence"))
    joint_p = float(constants.get("joint_confidence_union_bound"))
    if (range_p, translation_p, joint_p) != (0.99865, 0.99865, 0.9973):
        raise ScanFailure("coverage probabilities differ from frozen constants")

    grid = _test_properties(
        properties,
        "TurnSafeCertificateCoverage",
        "FrozenComponentQuantilesAndUnionBoundMeetDeclaredCoverage",
        (
            "deterministic_grid_samples",
            "range_component_covered",
            "translation_component_covered",
            "joint_union_bound_covered",
        ),
    )
    grid_n = _canonical_uint(grid["deterministic_grid_samples"], "grid samples")
    range_k = _canonical_uint(grid["range_component_covered"], "range grid covered")
    translation_k = _canonical_uint(grid["translation_component_covered"], "translation grid covered")
    joint_k = _canonical_uint(grid["joint_union_bound_covered"], "joint grid covered")
    if (grid_n, range_k, translation_k, joint_k) != (100000, 99865, 99865, 99730):
        raise ScanFailure("deterministic component/joint coverage counts differ")

    stereo = _test_properties(
        properties,
        "TurnSafeCertificateStereoRange",
        "FrozenOneSidedLcbHasDeterministicMonteCarloCoverage",
        ("monte_carlo_samples", "covered_samples", "empirical_coverage"),
    )
    stereo_n = _canonical_uint(stereo["monte_carlo_samples"], "stereo MC samples")
    stereo_k = _canonical_uint(stereo["covered_samples"], "stereo MC covered")
    if stereo_n != 16000:
        raise ScanFailure("stereo Monte Carlo denominator differs from focused test")
    if not math.isclose(float(stereo["empirical_coverage"]), stereo_k / stereo_n, rel_tol=0.0, abs_tol=5e-7):
        raise ScanFailure("stereo empirical coverage property differs from count")
    if stereo_k / stereo_n < range_p:
        raise ScanFailure("stereo Monte Carlo coverage misses frozen range p_d")

    nees = _test_properties(
        properties,
        "TurnSafeCertificateTranslation",
        "DeterministicTranslationNeesStressBindsInflatedCoverage",
        (
            "monte_carlo_samples",
            "certified_covered_samples",
            "certified_empirical_coverage",
            "uninflated_nees_mean",
            "certified_nees_mean",
        ),
    )
    nees_n = _canonical_uint(nees["monte_carlo_samples"], "translation NEES samples")
    nees_k = _canonical_uint(nees["certified_covered_samples"], "translation NEES covered")
    if nees_n != 24000:
        raise ScanFailure("translation NEES denominator differs from focused test")
    if not math.isclose(float(nees["certified_empirical_coverage"]), nees_k / nees_n, rel_tol=0.0, abs_tol=5e-7):
        raise ScanFailure("translation NEES coverage property differs from count")
    if nees_k / nees_n < translation_p:
        raise ScanFailure("translation certified NEES coverage misses p_t")

    bearing = _test_properties(
        properties,
        "TurnSafeCertificateBearing",
        "LinearizedBearingCovarianceMatchesDeterministicMonteCarlo",
        ("monte_carlo_samples", "relative_covariance_error"),
    )
    residual = _test_properties(
        properties,
        "TurnSafeCertificateResidualCovariance",
        "ExactBearingNoisePropagationMatchesDeterministicMonteCarlo",
        ("monte_carlo_samples", "relative_covariance_error"),
    )
    bearing_n = _canonical_uint(bearing["monte_carlo_samples"], "bearing MC samples")
    residual_n = _canonical_uint(residual["monte_carlo_samples"], "residual MC samples")
    bearing_error = _finite_csv_float(bearing["relative_covariance_error"], "bearing covariance error")
    residual_error = _finite_csv_float(residual["relative_covariance_error"], "residual covariance error")
    if bearing_n != 12000 or residual_n != 16000:
        raise ScanFailure("covariance Monte Carlo denominator differs from focused tests")
    if bearing_error < 0.0 or bearing_error > 0.04 or residual_error < 0.0 or residual_error > 0.04:
        raise ScanFailure("covariance Monte Carlo error exceeds focused-test bound")
    uninflated_nees_mean = _finite_csv_float(nees["uninflated_nees_mean"], "uninflated NEES mean")
    certified_nees_mean = _finite_csv_float(nees["certified_nees_mean"], "certified NEES mean")
    if abs(uninflated_nees_mean - 3.0) > 0.06 or abs(certified_nees_mean - 1.5) > 0.03:
        raise ScanFailure("translation NEES mean differs from focused-test bound")
    scipy_versions: set = set()
    output = {
        "schema_version": COVERAGE_SCHEMA,
        "status": "PASS_FROZEN_CERTIFICATE_TEST_EVIDENCE",
        "certificate_test_xml": xml_identity.__dict__,
        "component_grid": {
            "range": _coverage_record(range_k, grid_n, range_p, scipy_versions),
            "translation": _coverage_record(translation_k, grid_n, translation_p, scipy_versions),
        },
        "joint_union_bound_grid": _coverage_record(joint_k, grid_n, joint_p, scipy_versions),
        "stereo_range_one_sided_monte_carlo": _coverage_record(stereo_k, stereo_n, range_p, scipy_versions),
        "translation_certified_nees_monte_carlo": {
            **_coverage_record(nees_k, nees_n, translation_p, scipy_versions),
            "uninflated_nees_mean": uninflated_nees_mean,
            "certified_nees_mean": certified_nees_mean,
        },
        "covariance_monte_carlo": {
            "bearing": {
                "denominator_samples": bearing_n,
                "relative_covariance_error": bearing_error,
            },
            "factor_residual": {
                "denominator_samples": residual_n,
                "relative_covariance_error": residual_error,
            },
        },
        "interval_method": {
            "name": "Clopper-Pearson exact two-sided",
            "confidence": 0.95,
            "beta_quantile_provider": "scipy.stats.beta.ppf",
            "scipy_version": None,
        },
    }
    if len(scipy_versions) != 1:
        raise ScanFailure("coverage intervals used inconsistent scipy versions")
    output["interval_method"]["scipy_version"] = next(iter(scipy_versions))
    return output


def _score_key(row: Mapping[str, str]) -> Tuple[object, ...]:
    return (
        -_finite_csv_float(row["winner_score_predicted_rotation_angle_rad"], "winner score angle"),
        -_finite_csv_float(row["winner_score_minimum_whitened_local_singular_value"], "winner score singular value"),
        _finite_csv_float(row["winner_score_maximum_retained_rho_trans"], "winner score rho"),
        -_canonical_uint(row["winner_score_retained_feature_count"], "winner score feature count"),
        _canonical_uint(row["winner_camera_id"], "winner camera id"),
        row["winner_source_timestamp_key"].encode("utf-8"),
        row["winner_target_timestamp_key"].encode("utf-8"),
    )


def _sequence_family(sequence: str) -> str:
    if sequence.startswith("infinite/"):
        return "infinity"
    if sequence.startswith("square/"):
        return "square"
    raise ScanFailure("sequence is outside infinity/square families")


def _accumulate_row(accumulator: SequenceAccumulator, row: Mapping[str, str]) -> None:
    accumulator.callbacks += 1
    for name in COUNTER_COLUMNS:
        accumulator.counters[name] += _canonical_uint(row[name], name)
    certified = _csv_bool(row["certified_winner_available"], "certified winner")
    if certified:
        accumulator.certified += 1
        accumulator.predicted_information_traces.append(
            _finite_csv_float(
                row["winner_predicted_information_trace"],
                "winner predicted information trace",
            )
        )
        if _csv_bool(
            row["winner_predicted_information_non_negligible"],
            "predicted information non-negligible",
        ):
            accumulator.predicted_information_non_negligible += 1
    accumulator.nis_evaluated += _canonical_uint(
        row["winner_nis_evaluated_count"], "winner NIS evaluated count"
    )
    accumulator.nis_passing += _canonical_uint(
        row["winner_nis_pass_count"], "winner NIS pass count"
    )
    if _csv_bool(row["post_nis_accepted_winner_available"], "accepted winner"):
        accumulator.post_nis_accepted += 1
    reasons = _parse_embedded_json(row["rejection_reasons_json"], dict, "rejection reasons")
    for reason, count in reasons.items():
        accumulator.rejection_reasons[reason] += _integer(count, "rejection count")


def _summary_row(accumulator: SequenceAccumulator) -> Mapping[str, object]:
    traces = accumulator.predicted_information_traces
    row: Dict[str, object] = {
        "sequence_order": accumulator.identity.sequence_order,
        "run": accumulator.identity.run,
        "sequence": accumulator.identity.sequence,
        "family": _sequence_family(accumulator.identity.sequence),
        "callbacks": accumulator.callbacks,
        **{name: accumulator.counters[name] for name in COUNTER_COLUMNS},
        "certified_winner_callbacks": accumulator.certified,
        "winner_nis_evaluated_callbacks": accumulator.nis_evaluated,
        "winner_nis_passing_callbacks": accumulator.nis_passing,
        "predicted_information_non_negligible_callbacks": accumulator.predicted_information_non_negligible,
        "post_nis_accepted_winner_callbacks": accumulator.post_nis_accepted,
        "predicted_information_trace_min": "" if not traces else format(min(traces), ".17g"),
        "predicted_information_trace_max": "" if not traces else format(max(traces), ".17g"),
        "predicted_information_trace_mean": "" if not traces else format(sum(traces) / len(traces), ".17g"),
        "rejection_reason_counts_json": json.dumps(
            dict(sorted(accumulator.rejection_reasons.items())),
            separators=(",", ":"),
            sort_keys=True,
        ),
    }
    return row


def _aggregate_admission(
    output_root: Path,
    identities: Mapping[str, RunIdentity],
    primary_scans: Sequence[WorkerScan],
    coverage: Mapping[str, object],
    deadline: Optional[float] = None,
) -> Tuple[
    Mapping[str, FileIdentity],
    List[Mapping[str, str]],
    Mapping[str, Mapping[str, object]],
    Sequence[str],
]:
    header: Optional[List[str]] = None
    # Only certified winner rows and the sixteen fixed event rows are retained.
    # Every callback row, including its potentially large diagnostics JSON, is
    # otherwise validated and copied directly to the aggregate stream.
    certified_context_rows: List[Mapping[str, str]] = []
    square_certified_rows: List[Mapping[str, str]] = []
    event_keys = {
        (str(event["run"]), callback_index)
        for event in EVENT_WINDOWS
        for callback_index in range(event["analysis"][0], event["analysis"][1] + 1)
    }
    event_rows_by_key: Dict[Tuple[str, int], Mapping[str, str]] = {}
    summaries: Dict[str, SequenceAccumulator] = {
        run: SequenceAccumulator(identities[run]) for run in FIXED_RUNS
    }
    metadata: Dict[str, Mapping[str, object]] = {}
    outputs: Dict[str, FileIdentity] = {}
    shadow_path = output_root / "SHADOW_CALLBACK_RESULTS.csv"
    with _atomic_text_stream_new(shadow_path) as shadow_stream:
        shadow_writer: Optional[csv.DictWriter] = None
        for scan in primary_scans:
            observed_header = _worker_csv_header(scan, header)
            if header is None:
                header = observed_header
                shadow_writer = csv.DictWriter(
                    shadow_stream,
                    fieldnames=header,
                    extrasaction="raise",
                    lineterminator="\n",
                )
                shadow_writer.writeheader()

            def consume(row: Mapping[str, str], current_scan: WorkerScan = scan) -> None:
                if shadow_writer is None:
                    raise ScanFailure("aggregate CSV writer was not initialized")
                shadow_writer.writerow(row)
                callback_index = _canonical_uint(row["callback_index"], "callback index")
                key = (current_scan.run, callback_index)
                if key in event_keys:
                    if key in event_rows_by_key:
                        raise ScanFailure("duplicate event callback aggregation key")
                    event_rows_by_key[key] = dict(row)
                _accumulate_row(summaries[current_scan.run], row)
                if _csv_bool(row["certified_winner_available"], "certified winner"):
                    certified_context_rows.append(
                        {
                            "run": row["run"],
                            "sequence": row["sequence"],
                            "callback_index": row["callback_index"],
                        }
                    )
                    if row["sequence"].startswith("square/"):
                        # The square report requires every worker field. Only
                        # certified square rows are retained; all other rows
                        # are released after their streaming callback.
                        square_certified_rows.append(dict(row))

            _, _, worker_metadata = _consume_worker_csv(
                scan, identities[scan.run], header, consume, deadline
            )
            metadata[scan.label] = worker_metadata
    if header is None:
        raise ScanFailure("worker scans contain no CSV header")
    outputs["SHADOW_CALLBACK_RESULTS.csv"] = _file_identity(shadow_path)

    event_fields = (
        "event_id",
        "event_analysis_member",
        "event_primary_member",
        "event_pre_onset_member",
    ) + tuple(header)
    event_rows: List[Mapping[str, object]] = []
    for event in EVENT_WINDOWS:
        run = str(event["run"])
        analysis = event["analysis"]
        primary = event["primary"]
        pre_onset = event["pre_onset"]
        if not isinstance(analysis, tuple) or not isinstance(primary, tuple):
            raise ScanFailure("frozen event window has an invalid type")
        for callback_index in range(analysis[0], analysis[1] + 1):
            key = (run, callback_index)
            if key not in event_rows_by_key:
                raise ScanFailure("fixed event callback {}:{} is absent".format(run, callback_index))
            base = event_rows_by_key[key]
            joined: Dict[str, object] = {
                "event_id": event["event_id"],
                "event_analysis_member": "true",
                "event_primary_member": "true" if primary[0] <= callback_index <= primary[1] else "false",
                "event_pre_onset_member": "true" if pre_onset is not None and pre_onset[0] <= callback_index <= pre_onset[1] else "false",
                **base,
            }
            event_rows.append(joined)
    outputs["INFINITY_EVENT_RESULTS.csv"] = _write_atomic_new(
        output_root / "INFINITY_EVENT_RESULTS.csv",
        _csv_bytes(event_fields, event_rows),
    )

    ranked = sorted(square_certified_rows, key=_score_key)
    score_keys = [_score_key(row) for row in ranked]
    if len(score_keys) != len(set(score_keys)):
        raise ScanFailure("frozen pre-NIS score does not uniquely order square callbacks")
    square_fields = ("square_pre_nis_rank", "square_top20") + tuple(header)
    square_rows: List[Mapping[str, object]] = []
    for rank, row in enumerate(ranked, 1):
        square_rows.append(
            {
                "square_pre_nis_rank": rank,
                "square_top20": "true" if rank <= 20 else "false",
                **row,
            }
        )
    outputs["SQUARE_SEQUENCE_RESULTS.csv"] = _write_atomic_new(
        output_root / "SQUARE_SEQUENCE_RESULTS.csv",
        _csv_bytes(square_fields, square_rows),
    )

    summary_rows = [_summary_row(summaries[run]) for run in FIXED_RUNS]
    outputs["SEQUENCE_SUMMARY.csv"] = _write_atomic_new(
        output_root / "SEQUENCE_SUMMARY.csv",
        _csv_bytes(SUMMARY_COLUMNS, summary_rows),
    )
    outputs["CERTIFICATE_COVERAGE.json"] = _write_atomic_new(
        output_root / "CERTIFICATE_COVERAGE.json",
        _canonical_json_bytes(coverage),
    )
    return outputs, certified_context_rows, metadata, header


def _baseline_context(
    p0b_dir: Path,
    p0c_dir: Path,
    certified_rows: Sequence[Mapping[str, str]],
    admission_freeze_sha256: str,
    deadline: Optional[float] = None,
) -> Tuple[str, Mapping[str, FileIdentity]]:
    sources: Dict[str, FileIdentity] = {}
    for ledger in (p0b_dir / "SHA256SUMS", p0c_dir / "SHA256SUMS"):
        _validate_checksum_ledger(ledger, deadline=deadline)
        sources[str(ledger)] = _file_identity(ledger, deadline)
    p0b_decision = p0b_dir / "P0B_DECISION.md"
    p0c_decision = p0c_dir / "P0C_DECISION.md"
    if "P0B_CONTINUE_T1_PILOT_STRONG" not in p0b_decision.read_text(encoding="utf-8"):
        raise ScanFailure("P0B decision is not the required strong continuation")
    p0c_text = p0c_decision.read_text(encoding="utf-8")
    if not any(token in p0c_text for token in (
        "P0C_INPUT_ROBUSTNESS_CONFIRMED",
        "P0C_NOMINAL_RATE_ROBUSTNESS_CONFIRMED",
    )):
        raise ScanFailure("P0C decision is not an authorized robustness outcome")

    baseline_path = p0b_dir / "BASELINE_RESULTS.csv"
    with baseline_path.open("r", encoding="utf-8", newline="") as stream:
        baseline_rows = list(csv.DictReader(stream, strict=True))
    a0: Dict[str, Mapping[str, str]] = {}
    nominal: Dict[Tuple[str, str], str] = {}
    for row in baseline_rows:
        sequence = row.get("sequence", "")
        baseline = row.get("baseline", "")
        if baseline == "A0" and sequence in RUN_SEQUENCE.values():
            if sequence in a0:
                raise ScanFailure("duplicate A0 baseline sequence")
            if row.get("completion") != "TRUE" or row.get("classification") != "COMPLETE_VALID_NUMERIC":
                raise ScanFailure("A0 context is not complete valid numeric")
            for field_name in ("ate_rmse_m", "rotation_rpe_rmse_deg_1m"):
                _finite_csv_float(row.get(field_name, ""), "A0 " + field_name)
            a0[sequence] = row
        elif baseline in ("ORB_SLAM3", "SchurVINS") and sequence in RUN_SEQUENCE.values():
            classification = row.get("classification", "")
            if classification:
                key = (baseline, sequence)
                previous = nominal.get(key)
                if previous is not None and previous != classification:
                    raise ScanFailure("external nominal classifications disagree")
                nominal[key] = classification
    if set(a0) != set(RUN_SEQUENCE.values()):
        raise ScanFailure("frozen A0 context does not cover all six sequences")

    slow: Dict[Tuple[str, str], str] = {}
    for baseline, filename in (
        ("ORB_SLAM3", "ORB_RATE_LADDER.csv"),
        ("SchurVINS", "SCHUR_RATE_INVARIANCE.csv"),
    ):
        path = p0c_dir / filename
        with path.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream, strict=True))
        by_sequence: Dict[str, set] = {}
        for row in rows:
            sequence_leaf = row.get("sequence", "")
            sequence = ""
            for value in RUN_SEQUENCE.values():
                candidate_leaf = value.split("/")[-1]
                if candidate_leaf.endswith(".bag"):
                    candidate_leaf = candidate_leaf[:-4]
                if candidate_leaf == sequence_leaf:
                    sequence = value
                    break
            if not sequence:
                continue
            if row.get("record_status") != "EXCLUDED" or row.get("metric_scope") != "EXCLUDED_FROM_NOMINAL_RATE_COMPARISON":
                raise ScanFailure("slow-rate context entered nominal scope")
            by_sequence.setdefault(sequence, set()).add(row.get("classification", ""))
        for sequence, values in by_sequence.items():
            if "" in values:
                raise ScanFailure("empty slow-rate classification")
            slow[(baseline, sequence)] = ";".join(sorted(values)) + " (EXCLUDED_FROM_NOMINAL_RATE_COMPARISON)"

    for path in (
        baseline_path,
        p0b_dir / "COMPETITIVE_GAP_ANALYSIS.md",
        p0c_dir / "PAPER_CLAIM.md",
        p0c_dir / "ROBUSTNESS_CLAIM_MATRIX.csv",
        p0c_dir / "ORB_RATE_LADDER.csv",
        p0c_dir / "SCHUR_RATE_INVARIANCE.csv",
    ):
        sources[str(path)] = _file_identity(path, deadline)

    lines = [
        "# P1 external robustness context",
        "",
        "Status: descriptive join performed only after immutable admission freeze `{}`.".format(admission_freeze_sha256),
        "",
        "External results did not enter certificate survival, winner selection, NIS, information, event-window, or square-ranking logic. Slow-rate observations remain excluded from nominal-rate comparison, and no external KAIST accuracy comparison is made.",
        "",
        "| Run | Callback | A0 metric to improve | ORB-SLAM3 nominal | ORB-SLAM3 slow diagnostic | SchurVINS nominal | SchurVINS slow diagnostic | Required future A2 endpoint |",
        "|---|---:|---|---|---|---|---|---|",
    ]
    for row in certified_rows:
        sequence = row["sequence"]
        baseline = a0[sequence]
        event_metric = baseline.get("orientation_event_metric", "NOT_APPLICABLE")
        if event_metric == "NOT_APPLICABLE":
            metric = "rotation RPE 1 m {} deg; ATE {} m; preserve completion".format(
                baseline["rotation_rpe_rmse_deg_1m"], baseline["ate_rmse_m"]
            )
        else:
            metric = "{}; rotation RPE 1 m {} deg; preserve completion".format(
                event_metric, baseline["rotation_rpe_rmse_deg_1m"]
            )
        if sequence == "infinite/infinite.bag":
            endpoint = "fixed E10 orientation-consistency peak and time-integral over primary/analysis windows; A2 vs A0 and A1"
        elif sequence.startswith("infinite/"):
            endpoint = "A2 improves A0 and beats A1 on the declared TurnSafe endpoint; preserve completion; best/tied on >=1 infinity target"
        else:
            endpoint = "A2 improves A0 and beats A1 on the declared TurnSafe endpoint; preserve completion; best/tied on >=1 square target"
        external_values = []
        for external in ("ORB_SLAM3", "SchurVINS"):
            external_values.extend(
                [
                    nominal.get((external, sequence), "NOT_RUN_INVALID_SMOKE_GATE"),
                    slow.get((external, sequence), "NOT_RUN_INVALID_SMOKE_GATE"),
                ]
            )
        cells = [
            row["run"],
            row["callback_index"],
            metric,
            external_values[0],
            external_values[1],
            external_values[2],
            external_values[3],
            endpoint,
        ]
        escaped = [value.replace("|", "\\|").replace("\n", " ") for value in cells]
        lines.append("| " + " | ".join(escaped) + " |")
    lines.extend(
        [
            "",
            "The join includes every certified infinity/square callback and no uncertified callback. It is context only and emits no P1 decision.",
            "",
        ]
    )
    return "\n".join(lines), sources


def _identity_json(identity: FileIdentity) -> Mapping[str, object]:
    return {
        "path": identity.path,
        "size_bytes": identity.size_bytes,
        "sha256": identity.sha256,
    }


def main() -> int:
    args = _parse_args()
    try:
        if args.worker_timeout_seconds <= 0 or args.worker_timeout_seconds > MAX_WORKER_SECONDS:
            raise ScanFailure("worker timeout must be in [1, {}]".format(MAX_WORKER_SECONDS))
        if args.total_timeout_seconds <= 0 or args.total_timeout_seconds > MAX_TOTAL_SECONDS:
            raise ScanFailure("total timeout must be in [1, {}]".format(MAX_TOTAL_SECONDS))
        deadline = time.monotonic() + args.total_timeout_seconds

        script = Path(__file__).resolve(strict=True)
        if script.name != "p1_shadow_scan.py" or script.parent.name != "turnsafe" or script.parent.parent.name != "scripts":
            raise ScanFailure("orchestrator is not running from its repository script identity")
        repo = script.parents[2].resolve(strict=True)
        prereg_dir = repo / "artifacts/turnsafe/t1_pilot_p1"
        prereg_ledger = prereg_dir / "P1_PREREGISTRATION_SHA256SUMS"
        prereg_files = _validate_checksum_ledger(
            prereg_ledger, PREREG_LEDGER_ENTRIES, deadline
        )
        prereg = _object(
            _strict_json_file(
                prereg_dir / "P1_DECISION_PREREGISTRATION.json",
                "P1 decision preregistration",
            ),
            "P1 decision preregistration",
        )
        if _string(prereg.get("status"), "prereg.status") != "FROZEN_BEFORE_FIRST_REAL_DATA_CERTIFICATE_SCAN":
            raise ScanFailure("P1 preregistration is not frozen")
        if tuple(_list(prereg.get("runs_in_fixed_order"), "prereg.runs_in_fixed_order")) != FIXED_RUNS:
            raise ScanFailure("preregistered scan order differs")
        if tuple(_list(prereg.get("repeat_scans"), "prereg.repeat_scans")) != REPEAT_RUNS:
            raise ScanFailure("preregistered repeat order differs")
        entry = _object(prereg.get("entry"), "prereg.entry")
        if _boolean(entry.get("holdout_accessed"), "prereg.entry.holdout_accessed"):
            raise ScanFailure("preregistration records disallowed corpus access")
        repository_identity = _validate_entry(repo, prereg)

        p0b_ledger = repo / "artifacts/turnsafe/t1_pilot_p0b/SHA256SUMS"
        p0c_ledger = repo / "artifacts/turnsafe/t1_pilot_p0c/SHA256SUMS"
        _require_regular_file(p0b_ledger, "P0B checksum ledger")
        _require_regular_file(p0c_ledger, "P0C checksum ledger")
        if _sha256_file(p0b_ledger, deadline) != _sha_text(entry.get("p0b_checksum_ledger_sha256"), "p0b ledger hash"):
            raise ScanFailure("P0B ledger identity differs from preregistration")
        if _sha256_file(p0c_ledger, deadline) != _sha_text(entry.get("p0c_checksum_ledger_sha256"), "p0c ledger hash"):
            raise ScanFailure("P0C ledger identity differs from preregistration")
        _validate_checksum_ledger(p0b_ledger, deadline=deadline)
        _validate_checksum_ledger(p0c_ledger, deadline=deadline)
        if _string(entry.get("p0b_decision"), "prereg.entry.p0b_decision") != "P0B_CONTINUE_T1_PILOT_STRONG":
            raise ScanFailure("preregistered P0B decision is not the required outcome")
        if _string(entry.get("p0c_decision"), "prereg.entry.p0c_decision") not in (
            "P0C_INPUT_ROBUSTNESS_CONFIRMED",
            "P0C_NOMINAL_RATE_ROBUSTNESS_CONFIRMED",
        ):
            raise ScanFailure("preregistered P0C decision is not authorized")
        p0b_decision_text = (repo / "artifacts/turnsafe/t1_pilot_p0b/P0B_DECISION.md").read_text(encoding="utf-8")
        p0c_decision_text = (repo / "artifacts/turnsafe/t1_pilot_p0c/P0C_DECISION.md").read_text(encoding="utf-8")
        if _string(entry.get("p0b_decision"), "prereg.entry.p0b_decision") not in p0b_decision_text:
            raise ScanFailure("P0B decision file differs from preregistration")
        if _string(entry.get("p0c_decision"), "prereg.entry.p0c_decision") not in p0c_decision_text:
            raise ScanFailure("P0C decision file differs from preregistration")

        session_2c = repo / "artifacts/turnsafe/session_02c/SESSION2C_DECISION.md"
        session_2c_identity = _file_identity(session_2c, deadline)
        if session_2c_identity.sha256 != _sha_text(entry.get("session_2c_decision_sha256"), "session-2C decision hash"):
            raise ScanFailure("Session-2C decision identity differs from preregistration")
        if _string(entry.get("session_2c_decision"), "prereg.entry.session_2c_decision") != "NO_EXTENSION" or "NO_EXTENSION" not in session_2c.read_text(encoding="utf-8"):
            raise ScanFailure("Session-2C decision is not the frozen NO_EXTENSION")

        manifest_path = repo / "artifacts/turnsafe/session_01e/EVENT_READY_CORPUS_MANIFEST.json"
        _, run_identities, manifest_identity = _validate_manifest(
            repo, manifest_path, prereg, deadline
        )
        _forbid_input_terms(str(args.worker), "worker path")
        worker = args.worker.resolve(strict=True)
        _require_regular_file(worker, "P1 worker", executable=True)
        if worker.name != "turnsafe_p1_shadow_worker":
            raise ScanFailure("worker executable basename differs from frozen target")
        worker_identity = _file_identity(worker, deadline)

        if args.certificate_test_xml is not None:
            _forbid_input_terms(
                str(args.certificate_test_xml), "certificate test XML path"
            )
            certificate_xml = args.certificate_test_xml.resolve(strict=True)
        else:
            certificate_xml = (
                repo / ".turnsafe-work/t1_pilot_p1/logs/certificate_test.xml"
            )
        coverage = _certificate_coverage(certificate_xml, prereg)

        output_root = repo / ".turnsafe-work/t1_pilot_p1/scan"
        output_root.mkdir(parents=True, exist_ok=False)
        primary_scans: List[WorkerScan] = []
        repeat_scans: List[WorkerScan] = []
        ordinal = 1
        for run in FIXED_RUNS:
            primary_scans.append(
                _run_one_worker(
                    worker,
                    run_identities[run],
                    ordinal,
                    False,
                    output_root,
                    args.worker_timeout_seconds,
                    deadline,
                )
            )
            ordinal += 1
        for run in REPEAT_RUNS:
            repeat_scans.append(
                _run_one_worker(
                    worker,
                    run_identities[run],
                    ordinal,
                    True,
                    output_root,
                    args.worker_timeout_seconds,
                    deadline,
                )
            )
            ordinal += 1

        primary_by_run = {scan.run: scan for scan in primary_scans}
        repeat_evidence: Dict[str, Mapping[str, object]] = {}
        for repeat in repeat_scans:
            primary = primary_by_run[repeat.run]
            bytes_equal = _exact_file_equal(
                primary.callback_csv, repeat.callback_csv, deadline
            )
            if not bytes_equal or primary.callback_identity.sha256 != repeat.callback_identity.sha256:
                raise ScanFailure("{} repeat callback bytes differ".format(repeat.run))
            repeat_evidence[repeat.run] = {
                "primary_sha256": primary.callback_identity.sha256,
                "repeat_sha256": repeat.callback_identity.sha256,
                "size_bytes": primary.callback_identity.size_bytes,
                "byte_equal": True,
            }

        admission_outputs, certified_rows, worker_metadata, worker_header = _aggregate_admission(
            output_root, run_identities, primary_scans, coverage, deadline
        )
        worker_metadata = dict(worker_metadata)
        for repeat in repeat_scans:
            _, _, repeat_metadata = _consume_worker_csv(
                repeat, run_identities[repeat.run], worker_header,
                deadline=deadline,
            )
            worker_metadata[repeat.label] = repeat_metadata
        admission_payload: Dict[str, object] = {
            "schema_version": ADMISSION_FREEZE_SCHEMA,
            "status": "IMMUTABLE_BEFORE_EXTERNAL_CONTEXT_JOIN",
            "repository": repository_identity,
            "preregistration_ledger": _identity_json(_file_identity(prereg_ledger)),
            "event_ready_manifest": _identity_json(manifest_identity),
            "session_2c_decision": _identity_json(session_2c_identity),
            "worker": _identity_json(worker_identity),
            "run_callback_csv": {
                scan.label: _identity_json(scan.callback_identity)
                for scan in primary_scans + repeat_scans
            },
            "repeat_byte_equality": repeat_evidence,
            "admission_outputs": {
                name: _identity_json(identity)
                for name, identity in sorted(admission_outputs.items())
            },
            "certified_callback_count": len(certified_rows),
            "external_context_opened": False,
            "decision_emitted": False,
        }
        admission_identity = _write_atomic_new(
            output_root / "ADMISSION_FREEZE.json",
            _canonical_json_bytes(admission_payload),
        )

        external_text, external_sources = _baseline_context(
            repo / "artifacts/turnsafe/t1_pilot_p0b",
            repo / "artifacts/turnsafe/t1_pilot_p0c",
            certified_rows,
            admission_identity.sha256,
            deadline,
        )
        external_identity = _write_atomic_new(
            output_root / "EXTERNAL_ROBUSTNESS_CONTEXT.md",
            external_text.encode("utf-8"),
        )

        aggregation = {
            "schema_version": AGGREGATION_SCHEMA,
            "status": "COMPLETE_ADMISSION_FROZEN_EXTERNAL_CONTEXT_JOINED",
            "repository": repository_identity,
            "preregistration": {
                "ledger": _identity_json(_file_identity(prereg_ledger)),
                "validated_files": {
                    name: _identity_json(identity)
                    for name, identity in prereg_files.items()
                },
            },
            "event_ready_manifest": _identity_json(manifest_identity),
            "session_2c_decision": _identity_json(session_2c_identity),
            "worker": _identity_json(worker_identity),
            "worker_csv_columns": worker_header,
            "worker_scans": [
                {
                    "ordinal": scan.ordinal,
                    "label": scan.label,
                    "run": scan.run,
                    "repeat": scan.repeat,
                    "callback_csv": _identity_json(scan.callback_identity),
                    "metadata_json": _identity_json(scan.metadata_identity),
                    "stdout": _identity_json(scan.stdout_identity),
                    "stderr": _identity_json(scan.stderr_identity),
                }
                for scan in primary_scans + repeat_scans
            ],
            "worker_metadata": worker_metadata,
            "repeat_byte_equality": repeat_evidence,
            "admission_freeze": _identity_json(admission_identity),
            "admission_outputs": {
                name: _identity_json(identity)
                for name, identity in sorted(admission_outputs.items())
            },
            "external_context": _identity_json(external_identity),
            "external_context_sources": {
                name: _identity_json(identity)
                for name, identity in sorted(external_sources.items())
            },
            "integrity": {
                "fixed_run_order": True,
                "fixed_repeat_order": True,
                "repeat_callbacks_byte_equal": True,
                "one_winner_before_nis_enforced": True,
                "runner_up_promotion_absent": True,
                "event_windows_fixed": True,
                "external_join_after_admission_freeze": True,
                "trajectory_outcomes_excluded_from_selection": True,
            },
            "decision_emitted": False,
            "stopped_before_p2": True,
        }
        aggregation_identity = _write_atomic_new(
            output_root / "P1_SHADOW_AGGREGATION.json",
            _canonical_json_bytes(aggregation),
        )
        print(aggregation_identity.sha256)
        return 0
    except (OSError, ScanFailure, ValueError, csv.Error) as exc:
        print("P1 shadow scan failed: {}".format(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
