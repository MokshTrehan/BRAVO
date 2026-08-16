#!/usr/bin/python3
"""Extract TUM-VI TransformStamped references after an estimator has closed.

The extractor is a post-run evidence tool.  It validates an explicit close
receipt before it stats, hashes, or opens the ROS bag.  Its output directory is
created once and every artifact is published without overwrite, so a repeated
invocation cannot silently replace a reference.

The close receipt is strict JSON with, at minimum, these fields::

    {
      "schema": "schurvio.icra27.estimator_close_receipt.v1",
      "run_id": "...",
      "estimator_process_group_closed": true,
      "estimator_closed_utc": "2026-08-16T04:12:00Z"
    }

A finalized
``schurvio.icra27.cross_dataset.sequence_result.v1`` may be supplied instead.
In that mode the extractor verifies the explicit close object, terminal status,
teardown facts, estimator process-group survivor flag, source-bag binding, and
every live member of the run's published ``SHA256SUMS``.  The extracted
reference must be placed outside that already checksummed run directory.

The TUM rendering intentionally reproduces the precision path used by the
tracked OpenVINS TUM-VI references: source pose scalars are first represented
at ten decimal places, then rounded half-even to six decimal places.  Header
timestamps are rounded half-even to five decimal places.  This makes room4
byte-compatible with ``ov_data/tum_vi/dataset-room4_512_16.txt``.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA = "schurvio.icra27.tum_vi_reference_extract.v1"
CLOSE_RECEIPT_SCHEMA = "schurvio.icra27.estimator_close_receipt.v1"
DEFAULT_TOPIC = "/vrpn_client/raw_transform"
TRANSFORM_TYPE = "geometry_msgs/TransformStamped"
TUM_HEADER = "# timestamp(s) tx ty tz qx qy qz qw\n"
DEFAULT_GAP_THRESHOLD_NS = 1_000_000_000
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SAFE_SEQUENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")


class ExtractionError(RuntimeError):
    """A fail-closed reference extraction error."""


@dataclass(frozen=True)
class PoseRecord:
    """One unmodified TransformStamped pose sample."""

    timestamp_ns: int
    translation: Tuple[float, float, float]
    quaternion_xyzw: Tuple[float, float, float, float]
    frame_id: str = ""
    child_frame_id: str = ""


@dataclass(frozen=True)
class CloseReceipt:
    path: Path
    payload: bytes
    sha256: str
    value: Mapping[str, Any]
    proof_kind: str
    run_id: str
    estimator_closed_utc: str
    run_directory: Optional[Path]
    bag_binding: Optional[Mapping[str, Any]]
    verified_artifact_count: int


RecordReader = Callable[[Path, str], Iterable[PoseRecord]]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _strict_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ExtractionError("duplicate JSON key in estimator close receipt: {}".format(key))
        value[key] = item
    return value


def _parse_utc(value: Any, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ExtractionError("{} must be a non-empty UTC timestamp".format(label))
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise ExtractionError("{} is not an ISO-8601 timestamp".format(label)) from exc
    if parsed.tzinfo is None:
        raise ExtractionError("{} must include a timezone".format(label))
    return parsed.astimezone(dt.timezone.utc)


def _safe_artifact_relative(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ExtractionError("{} must be a non-empty relative path".format(label))
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or "\n" in value or "\r" in value:
        raise ExtractionError("{} is not a safe relative path".format(label))
    return relative


def _verify_sequence_result_artifacts(
    result_path: Path, value: Mapping[str, Any], result_sha256: str
) -> Tuple[Path, int]:
    publication = value.get("publication")
    if not isinstance(publication, dict):
        raise ExtractionError("finalized sequence result has no publication object")
    if publication.get("append_only_run_directory") is not True:
        raise ExtractionError("sequence result does not assert an append-only run directory")
    result_relative = _safe_artifact_relative(
        publication.get("sequence_result"), "publication.sequence_result"
    )
    checksums_relative = _safe_artifact_relative(
        publication.get("checksums"), "publication.checksums"
    )
    if result_relative != Path("sequence_result.json"):
        raise ExtractionError("publication.sequence_result is not sequence_result.json")
    if checksums_relative != Path("SHA256SUMS"):
        raise ExtractionError("publication.checksums is not SHA256SUMS")
    run_directory_value = value.get("run_directory")
    if not isinstance(run_directory_value, str) or not run_directory_value:
        raise ExtractionError("sequence result run_directory is invalid")
    try:
        run_directory = Path(run_directory_value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExtractionError("sequence result run_directory does not resolve") from exc
    if not run_directory.is_dir() or result_path != run_directory / result_relative:
        raise ExtractionError("sequence result path is not bound to its declared run directory")
    try:
        checksum_path = (run_directory / checksums_relative).resolve(strict=True)
        checksum_path.relative_to(run_directory)
    except (OSError, ValueError) as exc:
        raise ExtractionError("sequence result checksum set escapes its run directory") from exc
    if not checksum_path.is_file():
        raise ExtractionError("sequence result checksum set is missing")
    try:
        checksum_lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ExtractionError("sequence result checksum set is not strict ASCII") from exc
    if not checksum_lines:
        raise ExtractionError("sequence result checksum set is empty")
    entries: Dict[str, str] = {}
    for line in checksum_lines:
        if "  " not in line:
            raise ExtractionError("malformed sequence result checksum line")
        digest, relative_text = line.split("  ", 1)
        _validate_sha256(digest, "artifact SHA-256")
        relative = _safe_artifact_relative(relative_text, "checksummed artifact")
        normalized = relative.as_posix()
        if normalized in entries:
            raise ExtractionError("duplicate path in sequence result checksum set")
        entries[normalized] = digest
        try:
            artifact = (run_directory / relative).resolve(strict=True)
            artifact.relative_to(run_directory)
        except (OSError, ValueError) as exc:
            raise ExtractionError(
                "checksummed artifact is missing or escapes the run directory"
            ) from exc
        try:
            identity_matches = artifact.is_file() and sha256_file(artifact) == digest
        except OSError as exc:
            raise ExtractionError("failed to rehash checksummed run artifact") from exc
        if not identity_matches:
            raise ExtractionError("checksummed run artifact identity mismatch: {}".format(normalized))
    result_key = result_relative.as_posix()
    if entries.get(result_key) != result_sha256:
        raise ExtractionError("sequence_result.json is not bound by the live checksum set")
    return run_directory, len(entries)


def load_close_receipt(path: Path) -> CloseReceipt:
    """Validate closure before any dataset access is allowed.

    Both the narrow standalone receipt and a finalized cross-dataset
    ``sequence_result.json`` are accepted.  The latter is rehashed together
    with every artifact in its published checksum set.
    """

    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExtractionError("estimator close receipt does not resolve: {}".format(path)) from exc
    if not resolved.is_file():
        raise ExtractionError("estimator close receipt is not a regular file: {}".format(resolved))
    payload = resolved.read_bytes()
    try:
        decoded = payload.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError("estimator close receipt is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ExtractionError("estimator close receipt root must be an object")
    schema = value.get("schema")
    run_directory: Optional[Path] = None
    bag_binding: Optional[Mapping[str, Any]] = None
    verified_artifact_count = 0
    if schema == CLOSE_RECEIPT_SCHEMA:
        if value.get("estimator_process_group_closed") is not True:
            raise ExtractionError("estimator process group is not proven closed")
        if value.get("process_group_survived_cleanup") is True:
            raise ExtractionError("estimator process group survived cleanup")
        proof_kind = "standalone_estimator_close_receipt"
        estimator_closed_utc = value.get("estimator_closed_utc")
    elif schema == "schurvio.icra27.cross_dataset.sequence_result.v1":
        if value.get("status") in (None, "ACTIVE") or not isinstance(value.get("status"), str):
            raise ExtractionError("cross-dataset sequence result is not terminal")
        close = value.get("estimator_close_receipt")
        if not isinstance(close, dict):
            raise ExtractionError("cross-dataset sequence result lacks close evidence")
        for field in (
            "estimator_attempted",
            "estimator_process_group_closed",
            "runtime_services_closed",
        ):
            if close.get(field) is not True:
                raise ExtractionError("cross-dataset close evidence {} is not true".format(field))
        commands = value.get("commands")
        estimator_command = commands.get("estimator") if isinstance(commands, dict) else None
        if not isinstance(estimator_command, dict):
            raise ExtractionError("cross-dataset sequence result lacks estimator command evidence")
        if estimator_command.get("process_group_survived_cleanup") is not False:
            raise ExtractionError("estimator process group survivor state is not closed")
        estimator_finished = _parse_utc(
            estimator_command.get("finished_utc"), "commands.estimator.finished_utc"
        )
        if estimator_finished > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1):
            raise ExtractionError("estimator command finished time is in the future")
        outcome = value.get("outcome_facts")
        checks = value.get("checks")
        if not isinstance(outcome, dict) or outcome.get("teardown_ok") is not True:
            raise ExtractionError("cross-dataset runtime teardown is not complete")
        if not isinstance(checks, dict) or checks.get("teardown_complete") is not True:
            raise ExtractionError("cross-dataset teardown check did not pass")
        result_finished = _parse_utc(value.get("finished_utc"), "finished_utc")
        estimator_closed_utc = close.get("closed_utc")
        closed_time = _parse_utc(estimator_closed_utc, "estimator_close_receipt.closed_utc")
        if closed_time < estimator_finished or result_finished < closed_time:
            raise ExtractionError("cross-dataset close timestamps are not causally ordered")
        bag_binding = value.get("inputs", {}).get("bag") if isinstance(value.get("inputs"), dict) else None
        if not isinstance(bag_binding, dict):
            raise ExtractionError("cross-dataset sequence result lacks a bag identity")
        proof_kind = "finalized_cross_dataset_sequence_result"
    else:
        raise ExtractionError("unsupported estimator close proof schema: {}".format(schema))

    run_id = value.get("run_id")
    if not isinstance(run_id, str) or SAFE_SEQUENCE_RE.fullmatch(run_id) is None:
        raise ExtractionError("estimator close receipt run_id is invalid")
    closed = _parse_utc(estimator_closed_utc, "estimator close time")
    if closed > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1):
        raise ExtractionError("estimator close time is in the future")
    payload_sha256 = sha256_bytes(payload)
    if proof_kind == "finalized_cross_dataset_sequence_result":
        run_directory, verified_artifact_count = _verify_sequence_result_artifacts(
            resolved, value, payload_sha256
        )
    return CloseReceipt(
        path=resolved,
        payload=payload,
        sha256=payload_sha256,
        value=value,
        proof_kind=proof_kind,
        run_id=run_id,
        estimator_closed_utc=str(estimator_closed_utc),
        run_directory=run_directory,
        bag_binding=bag_binding,
        verified_artifact_count=verified_artifact_count,
    )


def _resolved_regular_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ExtractionError("{} does not resolve: {}".format(label, path)) from exc
    if not resolved.is_file():
        raise ExtractionError("{} is not a regular file: {}".format(label, resolved))
    return resolved


def _validate_sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ExtractionError("{} must be a lowercase SHA-256 digest".format(label))
    return value


def read_transform_records(bag_path: Path, topic: str) -> Iterable[PoseRecord]:
    """Read only the requested TransformStamped topic from a ROS1 bag."""

    try:
        import rosbag  # type: ignore
    except ImportError as exc:
        raise ExtractionError(
            "ROS1 rosbag is unavailable; run this tool with /usr/bin/python3"
        ) from exc

    try:
        with rosbag.Bag(str(bag_path), "r") as bag:
            for actual_topic, message, _ in bag.read_messages(topics=[topic]):
                if actual_topic != topic:
                    raise ExtractionError("rosbag returned an unexpected topic")
                if getattr(message, "_type", None) != TRANSFORM_TYPE:
                    raise ExtractionError(
                        "{} has type {}, expected {}".format(
                            topic, getattr(message, "_type", None), TRANSFORM_TYPE
                        )
                    )
                transform = message.transform
                yield PoseRecord(
                    timestamp_ns=int(message.header.stamp.to_nsec()),
                    translation=(
                        float(transform.translation.x),
                        float(transform.translation.y),
                        float(transform.translation.z),
                    ),
                    quaternion_xyzw=(
                        float(transform.rotation.x),
                        float(transform.rotation.y),
                        float(transform.rotation.z),
                        float(transform.rotation.w),
                    ),
                    frame_id=str(message.header.frame_id),
                    child_frame_id=str(message.child_frame_id),
                )
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("failed reading {} from {}".format(topic, bag_path)) from exc


def validate_records(records: Sequence[PoseRecord]) -> Mapping[str, Any]:
    if not records:
        raise ExtractionError("reference topic emitted zero TransformStamped messages")
    prior: Optional[int] = None
    quaternion_norms: List[float] = []
    for index, record in enumerate(records):
        if isinstance(record.timestamp_ns, bool) or record.timestamp_ns <= 0:
            raise ExtractionError("record {} has an invalid header timestamp".format(index))
        if prior is not None and record.timestamp_ns <= prior:
            raise ExtractionError("reference header timestamps are not strictly increasing")
        prior = record.timestamp_ns
        values = record.translation + record.quaternion_xyzw
        if not all(math.isfinite(value) for value in values):
            raise ExtractionError("record {} contains a non-finite pose scalar".format(index))
        norm = math.sqrt(sum(value * value for value in record.quaternion_xyzw))
        if not math.isfinite(norm) or norm <= 1.0e-12:
            raise ExtractionError("record {} has an invalid quaternion".format(index))
        quaternion_norms.append(norm)
    return {
        "sample_count": len(records),
        "strictly_increasing_header_timestamps": True,
        "finite_pose_scalars": True,
        "nonzero_quaternions": True,
        "quaternion_norm_min": min(quaternion_norms),
        "quaternion_norm_max": max(quaternion_norms),
        "frame_ids": sorted({record.frame_id for record in records}),
        "child_frame_ids": sorted({record.child_frame_id for record in records}),
    }


def _format_timestamp(timestamp_ns: int) -> str:
    with localcontext() as context:
        context.prec = 40
        seconds = Decimal(timestamp_ns) / Decimal(1_000_000_000)
        rounded = seconds.quantize(Decimal("0.00001"), rounding=ROUND_HALF_EVEN)
    return format(rounded, ".5f")


def _format_pose_scalar(value: float) -> str:
    # The published TUM-VI CSV carries ten pose decimal places.  Reproducing
    # that intermediate representation is necessary for byte-identical room4.
    source = Decimal(format(value, ".10f"))
    rounded = source.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return format(rounded, ".6f")


def render_tum(records: Sequence[PoseRecord]) -> bytes:
    validate_records(records)
    lines = [TUM_HEADER]
    for record in records:
        fields = [_format_timestamp(record.timestamp_ns)]
        fields.extend(_format_pose_scalar(value) for value in record.translation)
        fields.extend(_format_pose_scalar(value) for value in record.quaternion_xyzw)
        lines.append(" ".join(fields) + "\n")
    return "".join(lines).encode("ascii")


def _timestamp_exact(timestamp_ns: int) -> str:
    seconds, nanoseconds = divmod(timestamp_ns, 1_000_000_000)
    return "{}.{:09d}".format(seconds, nanoseconds)


def build_interval_summary(
    records: Sequence[PoseRecord], gap_threshold_ns: int
) -> Mapping[str, Any]:
    validate_records(records)
    if isinstance(gap_threshold_ns, bool) or gap_threshold_ns <= 0:
        raise ExtractionError("interval gap threshold must be a positive integer nanosecond count")

    boundaries = [0]
    discontinuities: List[Mapping[str, Any]] = []
    all_gaps: List[int] = []
    for index in range(1, len(records)):
        gap_ns = records[index].timestamp_ns - records[index - 1].timestamp_ns
        all_gaps.append(gap_ns)
        if gap_ns > gap_threshold_ns:
            boundaries.append(index)
            discontinuities.append(
                {
                    "prior_timestamp_ns": records[index - 1].timestamp_ns,
                    "next_timestamp_ns": records[index].timestamp_ns,
                    "gap_ns": gap_ns,
                    "gap_seconds": gap_ns / 1_000_000_000.0,
                }
            )
    boundaries.append(len(records))

    intervals: List[Mapping[str, Any]] = []
    total_span_ns = 0
    for interval_index, (start_index, stop_index) in enumerate(
        zip(boundaries, boundaries[1:]), start=1
    ):
        subset = records[start_index:stop_index]
        start_ns = subset[0].timestamp_ns
        end_ns = subset[-1].timestamp_ns
        internal_gaps = [
            subset[index].timestamp_ns - subset[index - 1].timestamp_ns
            for index in range(1, len(subset))
        ]
        span_ns = end_ns - start_ns
        total_span_ns += span_ns
        intervals.append(
            {
                "index": interval_index,
                "start_sample_index": start_index,
                "end_sample_index_inclusive": stop_index - 1,
                "sample_count": len(subset),
                "start_timestamp_ns": start_ns,
                "start_timestamp_seconds_exact": _timestamp_exact(start_ns),
                "end_timestamp_ns": end_ns,
                "end_timestamp_seconds_exact": _timestamp_exact(end_ns),
                "span_ns": span_ns,
                "span_seconds": span_ns / 1_000_000_000.0,
                "maximum_internal_gap_ns": max(internal_gaps) if internal_gaps else 0,
            }
        )

    source_span_ns = records[-1].timestamp_ns - records[0].timestamp_ns
    return {
        "sample_count": len(records),
        "first_timestamp_ns": records[0].timestamp_ns,
        "last_timestamp_ns": records[-1].timestamp_ns,
        "source_span_ns": source_span_ns,
        "source_span_seconds": source_span_ns / 1_000_000_000.0,
        "interval_gap_threshold_ns": gap_threshold_ns,
        "interval_gap_threshold_seconds": gap_threshold_ns / 1_000_000_000.0,
        "interval_rule": "new interval iff consecutive header timestamp gap is greater than threshold",
        "interval_count": len(intervals),
        "interval_total_span_ns": total_span_ns,
        "interval_total_span_seconds": total_span_ns / 1_000_000_000.0,
        "largest_consecutive_gap_ns": max(all_gaps) if all_gaps else 0,
        "discontinuity_count": len(discontinuities),
        "discontinuities": discontinuities,
        "intervals": intervals,
    }


def _atomic_write_new_bytes(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ExtractionError("refusing to overwrite {}".format(path))
    temporary_name: Optional[str] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, str(path))
        except FileExistsError as exc:
            raise ExtractionError("refusing to overwrite {}".format(path)) from exc
        os.unlink(temporary_name)
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )


def _stable_stat(before: os.stat_result, after: os.stat_result) -> bool:
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    return all(getattr(before, field) == getattr(after, field) for field in fields)


def extract_reference(
    *,
    bag_path: Path,
    output_dir: Path,
    sequence_id: str,
    close_receipt_path: Path,
    expected_bag_bytes: int,
    expected_bag_sha256: str,
    gt_capability: str,
    gap_threshold_ns: int = DEFAULT_GAP_THRESHOLD_NS,
    expected_reference_sha256: Optional[str] = None,
    topic: str = DEFAULT_TOPIC,
    record_reader: Optional[RecordReader] = None,
) -> Mapping[str, Any]:
    """Extract a reference with all dataset access after close validation."""

    close_receipt = load_close_receipt(close_receipt_path)
    extraction_started_utc = utc_now()

    if SAFE_SEQUENCE_RE.fullmatch(sequence_id) is None:
        raise ExtractionError("sequence_id is invalid")
    if gt_capability not in {"full_trajectory", "embedded_partial_intervals"}:
        raise ExtractionError("unsupported ground-truth capability")
    if isinstance(expected_bag_bytes, bool) or expected_bag_bytes <= 0:
        raise ExtractionError("expected bag byte count must be positive")
    expected_bag_sha256 = _validate_sha256(expected_bag_sha256, "expected bag SHA-256")
    if expected_reference_sha256 is not None:
        expected_reference_sha256 = _validate_sha256(
            expected_reference_sha256, "expected reference SHA-256"
        )
    if not isinstance(topic, str) or not topic.startswith("/"):
        raise ExtractionError("reference topic must be an absolute ROS topic")

    expanded_output = output_dir.expanduser()
    if expanded_output.is_symlink():
        raise ExtractionError("output path is a symlink; refusing publication")
    output = expanded_output.resolve(strict=False)
    if output.exists() or output.is_symlink():
        raise ExtractionError("output already exists; refusing overwrite: {}".format(output))
    if close_receipt.run_directory is not None:
        try:
            output.relative_to(close_receipt.run_directory)
        except ValueError:
            pass
        else:
            raise ExtractionError(
                "reference output must be outside the already checksummed run directory"
            )

    bag = _resolved_regular_file(bag_path, "TUM-VI bag")
    if close_receipt.bag_binding is not None:
        binding = close_receipt.bag_binding
        try:
            bound_path = Path(str(binding["path"])).expanduser().resolve(strict=True)
            bound_bytes = binding["size_bytes"]
            bound_sha256 = str(binding["sha256"])
        except (KeyError, OSError) as exc:
            raise ExtractionError("cross-dataset bag binding is malformed") from exc
        if (
            bound_path != bag
            or bound_bytes != expected_bag_bytes
            or bound_sha256 != expected_bag_sha256
        ):
            raise ExtractionError("cross-dataset result is bound to a different bag identity")
    source_before = bag.stat()
    if source_before.st_size != expected_bag_bytes:
        raise ExtractionError(
            "bag size mismatch: expected {}, observed {}".format(
                expected_bag_bytes, source_before.st_size
            )
        )
    try:
        observed_bag_sha256 = sha256_file(bag)
    except OSError as exc:
        raise ExtractionError("failed to hash the TUM-VI bag") from exc
    if observed_bag_sha256 != expected_bag_sha256:
        raise ExtractionError("bag SHA-256 does not match the frozen matrix")

    reader = read_transform_records if record_reader is None else record_reader
    records = list(reader(bag, topic))
    validation = validate_records(records)
    reference_payload = render_tum(records)
    reference_sha256 = sha256_bytes(reference_payload)
    compatibility_status = "NOT_REQUESTED"
    if expected_reference_sha256 is not None:
        if reference_sha256 != expected_reference_sha256:
            raise ExtractionError("extracted TUM bytes do not match the expected reference")
        compatibility_status = "EXACT_BYTE_MATCH"

    interval_summary = build_interval_summary(records, gap_threshold_ns)
    source_after = bag.stat()
    if not _stable_stat(source_before, source_after):
        raise ExtractionError("source bag identity changed during extraction")

    manifest: Mapping[str, Any] = {
        "schema": SCHEMA,
        "status": "COMPLETE",
        "sequence_id": sequence_id,
        "execution_stage": "post_estimator_process_group_close_only",
        "extraction_started_utc": extraction_started_utc,
        "extraction_completed_utc": utc_now(),
        "source": {
            "bag_path": str(bag),
            "bag_size_bytes": source_before.st_size,
            "bag_sha256": observed_bag_sha256,
            "topic": topic,
            "message_type": TRANSFORM_TYPE,
            "timestamp_source": "message.header.stamp",
            "bag_identity_stable_during_extraction": True,
        },
        "estimator_close_receipt": {
            "source_path": str(close_receipt.path),
            "retained_copy": "estimator_close_receipt.json",
            "sha256": close_receipt.sha256,
            "source_schema": close_receipt.value["schema"],
            "proof_kind": close_receipt.proof_kind,
            "run_id": close_receipt.run_id,
            "estimator_closed_utc": close_receipt.estimator_closed_utc,
            "estimator_process_group_closed": True,
            "live_checksum_artifact_count": close_receipt.verified_artifact_count,
            "source_bag_binding_validated": close_receipt.bag_binding is not None,
        },
        "ground_truth_capability": gt_capability,
        "format": {
            "name": "tum",
            "columns": "timestamp tx ty tz qx qy qz qw",
            "timestamp_decimals": 5,
            "source_pose_decimals": 10,
            "output_pose_decimals": 6,
            "rounding": "round_half_even",
            "quaternion_order": "xyzw",
            "pose_values_normalized_or_interpolated": False,
        },
        "validation": validation,
        "reference": {
            "relative_path": "reference.tum",
            "bytes": len(reference_payload),
            "sha256": reference_sha256,
            "expected_sha256": expected_reference_sha256,
            "compatibility_status": compatibility_status,
        },
        "interval_summary": interval_summary,
        "artifact_policy": {
            "append_only_output_directory": True,
            "individual_files_no_overwrite": True,
            "checksums_written_last": True,
        },
    }
    manifest_payload = _json_bytes(manifest)
    artifact_payloads = {
        "estimator_close_receipt.json": close_receipt.payload,
        "interval_manifest.json": manifest_payload,
        "reference.tum": reference_payload,
    }
    checksum_payload = "".join(
        "{}  {}\n".format(sha256_bytes(artifact_payloads[name]), name)
        for name in sorted(artifact_payloads)
    ).encode("ascii")

    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.mkdir(mode=0o750)
    except FileExistsError as exc:
        raise ExtractionError("output already exists; refusing overwrite: {}".format(output)) from exc
    for name in ("estimator_close_receipt.json", "reference.tum", "interval_manifest.json"):
        _atomic_write_new_bytes(output / name, artifact_payloads[name])
    _atomic_write_new_bytes(output / "SHA256SUMS", checksum_payload)
    directory_descriptor = os.open(str(output), os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)
    return manifest


def parse_gap_threshold_seconds(value: str) -> int:
    try:
        seconds = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("gap threshold must be decimal seconds") from exc
    nanoseconds = seconds * Decimal(1_000_000_000)
    integral = nanoseconds.to_integral_value()
    if seconds <= 0 or nanoseconds != integral:
        raise argparse.ArgumentTypeError(
            "gap threshold must be positive with at most nanosecond precision"
        )
    return int(integral)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--sequence-id", required=True)
    parser.add_argument("--estimator-close-receipt", required=True, type=Path)
    parser.add_argument("--expected-bag-bytes", required=True, type=int)
    parser.add_argument("--expected-bag-sha256", required=True)
    parser.add_argument(
        "--gt-capability",
        required=True,
        choices=("full_trajectory", "embedded_partial_intervals"),
    )
    parser.add_argument("--topic", default=DEFAULT_TOPIC)
    parser.add_argument(
        "--gap-threshold-seconds",
        type=parse_gap_threshold_seconds,
        default=DEFAULT_GAP_THRESHOLD_NS,
        metavar="SECONDS",
    )
    parser.add_argument("--expected-reference-sha256")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest = extract_reference(
            bag_path=args.bag,
            output_dir=args.output_dir,
            sequence_id=args.sequence_id,
            close_receipt_path=args.estimator_close_receipt,
            expected_bag_bytes=args.expected_bag_bytes,
            expected_bag_sha256=args.expected_bag_sha256,
            gt_capability=args.gt_capability,
            gap_threshold_ns=args.gap_threshold_seconds,
            expected_reference_sha256=args.expected_reference_sha256,
            topic=args.topic,
        )
    except ExtractionError as exc:
        print("tum_vi_reference_extract: {}".format(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "sequence_id": manifest["sequence_id"],
                "reference_sha256": manifest["reference"]["sha256"],
                "interval_count": manifest["interval_summary"]["interval_count"],
                "output_dir": str(args.output_dir.expanduser().resolve(strict=True)),
            },
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
