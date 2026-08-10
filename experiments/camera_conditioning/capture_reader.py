#!/usr/bin/python3
"""Read and validate SchurVIO-Lite camera-conditioning captures (schema 1).

The reader intentionally uses only the Python standard library.  It validates
the framing checksums before interpreting each payload and rejects trailing
bytes, oversized allocations, malformed UTF-8, non-canonical metadata, and
camera-system dimension/layout inconsistencies.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from typing import Any, BinaryIO, Dict, List, Sequence, Tuple, Union


MAGIC = b"SCVIOCAPTURE0001"
SCHEMA_VERSION = 1
SCALAR_TYPE_BINARY64 = 1
WHITENING_ISOTROPIC_BINARY64 = 1
TRACK_DOMAIN = "schurvio_camera_track_system_v1"
FROZEN_MIN_SINGULAR_RATIO = 1.0e-6
TRAILER_SENTINEL = (1 << 64) - 1

# These limits are deliberately much larger than a real per-track system but
# keep a corrupted or hostile capture from requesting unbounded allocations.
MAX_HEADER_PAYLOAD_BYTES = 1 << 20
MAX_RECORD_PAYLOAD_BYTES = 64 << 20
MAX_STRING_BYTES = 1 << 20
MAX_MATRIX_DIMENSION = 1 << 20
MAX_MATRIX_ELEMENTS = 4 << 20
MAX_LAYOUT_BLOCKS = 1 << 20
MAX_OBSERVATIONS = 1 << 20

_SHA256_BYTES = hashlib.sha256().digest_size
_LOWER_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|unknown)\Z")
_LOWER_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class CaptureValidationError(ValueError):
    """The capture is truncated, corrupted, or violates schema 1."""


@dataclass(frozen=True)
class Matrix:
    rows: int
    cols: int
    values: Tuple[float, ...]

    def at(self, row: int, col: int) -> float:
        """Return one row-major element."""

        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise IndexError((row, col))
        return self.values[row * self.cols + col]


@dataclass(frozen=True)
class CaptureHeader:
    schema_version: int
    scalar_type: int
    source_commit: str
    config_sha256: str
    frozen_min_singular_ratio: float


@dataclass(frozen=True)
class LayoutBlock:
    local_column: int
    covariance_column: int
    size: int
    kind: int
    key_kind: int
    key_u64: int
    key_double: float


@dataclass(frozen=True)
class Observation:
    camera_id: int
    timestamp: float
    camera_model: int
    depth: float


@dataclass(frozen=True)
class TrackRecord:
    domain: str
    record_index: int
    update_index: int
    feature_ordinal: int
    update_timestamp: float
    feature_id: int
    geometry_valid: bool
    fej_enabled: bool
    calibration_flags: int
    feature_representation: int
    sigma_px: float
    noise_variance: float
    whitening_kind: int
    minimum_singular_ratio: float
    singular_values_available: bool
    singular_values: Matrix
    numerical_rank: int
    singular_ratio_available: bool
    singular_ratio: float
    h_x: Matrix
    h_f: Matrix
    residual: Matrix
    p_active: Matrix
    p_f_in_g: Matrix
    layout: Tuple[LayoutBlock, ...]
    observations: Tuple[Observation, ...]
    minimum_depth: float
    maximum_parallax_rad: float


@dataclass(frozen=True)
class Capture:
    header: CaptureHeader
    records: Tuple[TrackRecord, ...]
    trailer_sha256: str

    def summary(self) -> Dict[str, Any]:
        """Return a stable, JSON-compatible summary of validated contents."""

        ranks = Counter(record.numerical_rank for record in self.records)
        timestamps = [record.update_timestamp for record in self.records]
        return {
            "camera_models": sorted(
                {obs.camera_model for record in self.records for obs in record.observations}
            ),
            "config_sha256": self.header.config_sha256,
            "fej_enabled_records": sum(record.fej_enabled for record in self.records),
            "frozen_min_singular_ratio": self.header.frozen_min_singular_ratio,
            "geometry_valid_records": sum(record.geometry_valid for record in self.records),
            "numerical_rank_counts": [
                {"count": ranks[rank], "rank": rank} for rank in sorted(ranks)
            ],
            "observation_count": sum(len(record.observations) for record in self.records),
            "record_count": len(self.records),
            "scalar_type": self.header.scalar_type,
            "schema_version": self.header.schema_version,
            "source_commit": self.header.source_commit,
            "timestamp_first": min(timestamps) if timestamps else None,
            "timestamp_last": max(timestamps) if timestamps else None,
            "trailer_sha256": self.trailer_sha256,
            "update_count": len({record.update_index for record in self.records}),
        }


class _PayloadReader:
    """Bounds-checked decoder for one already-checksummed payload."""

    def __init__(self, payload: bytes, context: str) -> None:
        self._payload = payload
        self._view = memoryview(payload)
        self._offset = 0
        self._context = context

    @property
    def remaining(self) -> int:
        return len(self._view) - self._offset

    def _take(self, size: int, what: str) -> memoryview:
        if size < 0 or size > self.remaining:
            raise CaptureValidationError(
                f"{self._context}: truncated {what} at byte {self._offset}"
            )
        begin = self._offset
        self._offset += size
        return self._view[begin : begin + size]

    def u64(self, what: str) -> int:
        return struct.unpack(">Q", self._take(8, what))[0]

    def i64(self, what: str) -> int:
        return struct.unpack(">q", self._take(8, what))[0]

    def f64(self, what: str) -> float:
        return struct.unpack(">d", self._take(8, what))[0]

    def utf8(self, what: str) -> str:
        size = self.u64(f"{what} length")
        if size > MAX_STRING_BYTES:
            raise CaptureValidationError(
                f"{self._context}: {what} length {size} exceeds {MAX_STRING_BYTES}"
            )
        raw = bytes(self._take(size, what))
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CaptureValidationError(
                f"{self._context}: {what} is not valid UTF-8"
            ) from exc

    def matrix(self, what: str) -> Matrix:
        rows = self.u64(f"{what} rows")
        cols = self.u64(f"{what} columns")
        if rows > MAX_MATRIX_DIMENSION or cols > MAX_MATRIX_DIMENSION:
            raise CaptureValidationError(
                f"{self._context}: {what} dimensions {rows}x{cols} exceed the limit"
            )
        count = rows * cols
        if count > MAX_MATRIX_ELEMENTS:
            raise CaptureValidationError(
                f"{self._context}: {what} element count {count} exceeds "
                f"{MAX_MATRIX_ELEMENTS}"
            )
        raw = self._take(count * 8, f"{what} values")
        values = struct.unpack(f">{count}d", raw) if count else ()
        if any(not math.isfinite(value) for value in values):
            raise CaptureValidationError(
                f"{self._context}: {what} contains a non-finite value"
            )
        return Matrix(rows=rows, cols=cols, values=tuple(values))

    def finish(self) -> None:
        if self.remaining:
            raise CaptureValidationError(
                f"{self._context}: {self.remaining} trailing payload bytes"
            )


def _read_exact(stream: BinaryIO, size: int, what: str) -> bytes:
    chunks: List[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            received = size - remaining
            raise CaptureValidationError(
                f"truncated {what}: expected {size} bytes, received {received}"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _require_bool(value: int, what: str, context: str) -> bool:
    if value not in (0, 1):
        raise CaptureValidationError(f"{context}: {what} must be 0 or 1, got {value}")
    return bool(value)


def _require_finite(value: float, what: str, context: str) -> None:
    if not math.isfinite(value):
        raise CaptureValidationError(f"{context}: {what} is not finite")


def _parse_header(payload: bytes) -> CaptureHeader:
    reader = _PayloadReader(payload, "header")
    header = CaptureHeader(
        schema_version=reader.u64("schema version"),
        scalar_type=reader.u64("scalar type"),
        source_commit=reader.utf8("source commit"),
        config_sha256=reader.utf8("config SHA-256"),
        frozen_min_singular_ratio=reader.f64("frozen minimum singular ratio"),
    )
    reader.finish()

    if header.schema_version != SCHEMA_VERSION:
        raise CaptureValidationError(
            f"header: schema version must be {SCHEMA_VERSION}, got {header.schema_version}"
        )
    if header.scalar_type != SCALAR_TYPE_BINARY64:
        raise CaptureValidationError(
            "header: scalar type must be 1 (IEEE-754 binary64), "
            f"got {header.scalar_type}"
        )
    if not _LOWER_COMMIT_RE.fullmatch(header.source_commit):
        raise CaptureValidationError(
            "header: source commit must be lowercase 40-hex or 'unknown'"
        )
    if not _LOWER_SHA256_RE.fullmatch(header.config_sha256):
        raise CaptureValidationError(
            "header: config SHA-256 must be lowercase 64-hex"
        )
    if header.frozen_min_singular_ratio != FROZEN_MIN_SINGULAR_RATIO:
        raise CaptureValidationError(
            "header: frozen minimum singular ratio must be exactly 1e-6"
        )
    return header


def _validate_dimensions(record: TrackRecord, context: str) -> None:
    rows = record.h_x.rows
    active_cols = record.h_x.cols
    if active_cols == 0:
        raise CaptureValidationError(f"{context}: H_x must have at least one column")
    if rows == 0:
        raise CaptureValidationError(f"{context}: H_x must have at least one row")
    if (record.h_f.rows, record.h_f.cols) != (rows, 3):
        raise CaptureValidationError(
            f"{context}: H_f must have shape {rows}x3, got "
            f"{record.h_f.rows}x{record.h_f.cols}"
        )
    if (record.residual.rows, record.residual.cols) != (rows, 1):
        raise CaptureValidationError(
            f"{context}: residual must have shape {rows}x1, got "
            f"{record.residual.rows}x{record.residual.cols}"
        )
    if (record.p_active.rows, record.p_active.cols) != (active_cols, active_cols):
        raise CaptureValidationError(
            f"{context}: P_active must be {active_cols}x{active_cols}, got "
            f"{record.p_active.rows}x{record.p_active.cols}"
        )
    if (record.p_f_in_g.rows, record.p_f_in_g.cols) != (3, 1):
        raise CaptureValidationError(
            f"{context}: p_FinG must have shape 3x1, got "
            f"{record.p_f_in_g.rows}x{record.p_f_in_g.cols}"
        )
    if len(record.observations) * 2 != rows:
        raise CaptureValidationError(
            f"{context}: two rows per observation require {len(record.observations) * 2} "
            f"H rows, got {rows}"
        )


def _validate_layout(record: TrackRecord, context: str) -> None:
    local_cursor = 0
    for index, block in enumerate(record.layout):
        if block.size == 0:
            raise CaptureValidationError(f"{context}: layout block {index} has zero size")
        if block.local_column != local_cursor:
            raise CaptureValidationError(
                f"{context}: layout block {index} starts at local column "
                f"{block.local_column}, expected {local_cursor}"
            )
        local_cursor += block.size
    if local_cursor != record.h_x.cols:
        raise CaptureValidationError(
            f"{context}: layout covers {local_cursor} local columns, "
            f"but H_x has {record.h_x.cols}"
        )


def _validate_diagnostics(record: TrackRecord, context: str) -> None:
    singular_values = record.singular_values
    if (singular_values.rows, singular_values.cols) != (3, 1):
        raise CaptureValidationError(
            f"{context}: singular_values must have shape 3x1, got "
            f"{singular_values.rows}x{singular_values.cols}"
        )
    if any(value < 0.0 for value in singular_values.values):
        raise CaptureValidationError(f"{context}: singular values must be nonnegative")
    if not 0 <= record.numerical_rank <= 3:
        raise CaptureValidationError(
            f"{context}: numerical rank {record.numerical_rank} is outside [0, 3]"
        )

    if record.singular_ratio_available:
        if not record.singular_values_available:
            raise CaptureValidationError(
                f"{context}: singular ratio cannot be available without singular values"
            )
        _require_finite(record.singular_ratio, "singular ratio", context)
        if record.singular_ratio < 0.0:
            raise CaptureValidationError(f"{context}: singular ratio must be nonnegative")


def _parse_record(payload: bytes, header: CaptureHeader, expected_index: int) -> TrackRecord:
    context = f"record {expected_index}"
    reader = _PayloadReader(payload, context)

    domain = reader.utf8("domain")
    record_index = reader.u64("record index")
    update_index = reader.u64("update index")
    feature_ordinal = reader.u64("feature ordinal")
    update_timestamp = reader.f64("update timestamp")
    feature_id = reader.u64("feature ID")
    geometry_valid = _require_bool(reader.u64("geometry valid"), "geometry valid", context)
    fej_enabled = _require_bool(reader.u64("FEJ enabled"), "FEJ enabled", context)
    calibration_flags = reader.u64("calibration flags")
    feature_representation = reader.i64("feature representation")
    sigma_px = reader.f64("sigma_px")
    noise_variance = reader.f64("noise variance")
    whitening_kind = reader.u64("whitening kind")
    minimum_singular_ratio = reader.f64("minimum singular ratio")
    singular_values_available = _require_bool(
        reader.u64("singular values available"), "singular values available", context
    )
    singular_values = reader.matrix("singular_values")
    numerical_rank = reader.i64("numerical rank")
    singular_ratio_available = _require_bool(
        reader.u64("singular ratio available"), "singular ratio available", context
    )
    singular_ratio = reader.f64("singular ratio")
    h_x = reader.matrix("H_x")
    h_f = reader.matrix("H_f")
    residual = reader.matrix("residual")
    p_active = reader.matrix("P_active")
    p_f_in_g = reader.matrix("p_FinG")

    layout_count = reader.u64("layout count")
    if layout_count > MAX_LAYOUT_BLOCKS:
        raise CaptureValidationError(
            f"{context}: layout count {layout_count} exceeds {MAX_LAYOUT_BLOCKS}"
        )
    layout = tuple(
        LayoutBlock(
            local_column=reader.u64(f"layout[{index}] local column"),
            covariance_column=reader.u64(f"layout[{index}] covariance column"),
            size=reader.u64(f"layout[{index}] size"),
            kind=reader.u64(f"layout[{index}] kind"),
            key_kind=reader.u64(f"layout[{index}] key kind"),
            key_u64=reader.u64(f"layout[{index}] u64 key"),
            key_double=reader.f64(f"layout[{index}] double key"),
        )
        for index in range(layout_count)
    )

    observation_count = reader.u64("observation count")
    if observation_count > MAX_OBSERVATIONS:
        raise CaptureValidationError(
            f"{context}: observation count {observation_count} exceeds {MAX_OBSERVATIONS}"
        )
    observations = tuple(
        Observation(
            camera_id=reader.u64(f"observation[{index}] camera ID"),
            timestamp=reader.f64(f"observation[{index}] timestamp"),
            camera_model=reader.u64(f"observation[{index}] camera model"),
            depth=reader.f64(f"observation[{index}] depth"),
        )
        for index in range(observation_count)
    )
    minimum_depth = reader.f64("minimum depth")
    maximum_parallax_rad = reader.f64("maximum parallax")
    reader.finish()

    record = TrackRecord(
        domain=domain,
        record_index=record_index,
        update_index=update_index,
        feature_ordinal=feature_ordinal,
        update_timestamp=update_timestamp,
        feature_id=feature_id,
        geometry_valid=geometry_valid,
        fej_enabled=fej_enabled,
        calibration_flags=calibration_flags,
        feature_representation=feature_representation,
        sigma_px=sigma_px,
        noise_variance=noise_variance,
        whitening_kind=whitening_kind,
        minimum_singular_ratio=minimum_singular_ratio,
        singular_values_available=singular_values_available,
        singular_values=singular_values,
        numerical_rank=numerical_rank,
        singular_ratio_available=singular_ratio_available,
        singular_ratio=singular_ratio,
        h_x=h_x,
        h_f=h_f,
        residual=residual,
        p_active=p_active,
        p_f_in_g=p_f_in_g,
        layout=layout,
        observations=observations,
        minimum_depth=minimum_depth,
        maximum_parallax_rad=maximum_parallax_rad,
    )

    if record.domain != TRACK_DOMAIN:
        raise CaptureValidationError(
            f"{context}: domain must be {TRACK_DOMAIN!r}, got {record.domain!r}"
        )
    if record.record_index != expected_index:
        raise CaptureValidationError(
            f"{context}: record index is {record.record_index}, expected {expected_index}"
        )
    _require_finite(record.update_timestamp, "update timestamp", context)
    if record.feature_representation < 0:
        raise CaptureValidationError(
            f"{context}: feature representation must be nonnegative"
        )
    _require_finite(record.sigma_px, "sigma_px", context)
    _require_finite(record.noise_variance, "noise variance", context)
    if record.sigma_px <= 0.0:
        raise CaptureValidationError(f"{context}: sigma_px must be positive")
    if record.noise_variance <= 0.0:
        raise CaptureValidationError(f"{context}: noise variance must be positive")
    if record.whitening_kind != WHITENING_ISOTROPIC_BINARY64:
        raise CaptureValidationError(
            f"{context}: whitening kind must be {WHITENING_ISOTROPIC_BINARY64}"
        )
    if record.minimum_singular_ratio != header.frozen_min_singular_ratio:
        raise CaptureValidationError(
            f"{context}: minimum singular ratio differs from the frozen header value"
        )
    for index, observation in enumerate(record.observations):
        _require_finite(observation.timestamp, f"observation[{index}] timestamp", context)
        _require_finite(observation.depth, f"observation[{index}] depth", context)
    for index, block in enumerate(record.layout):
        _require_finite(block.key_double, f"layout[{index}] double key", context)
    _require_finite(record.minimum_depth, "minimum depth", context)
    _require_finite(record.maximum_parallax_rad, "maximum parallax", context)
    if not 0.0 <= record.maximum_parallax_rad <= math.pi:
        raise CaptureValidationError(
            f"{context}: maximum parallax must be in [0, pi]"
        )

    _validate_dimensions(record, context)
    _validate_layout(record, context)
    _validate_diagnostics(record, context)
    return record


def parse_capture(stream: BinaryIO) -> Capture:
    """Parse and validate one binary capture from an open binary stream."""

    capture_hash = hashlib.sha256()

    magic = _read_exact(stream, len(MAGIC), "file magic")
    if magic != MAGIC:
        raise CaptureValidationError("file magic does not match SCVIOCAPTURE0001")
    capture_hash.update(magic)

    header_length_raw = _read_exact(stream, 8, "header payload length")
    capture_hash.update(header_length_raw)
    header_length = struct.unpack(">Q", header_length_raw)[0]
    if header_length == 0 or header_length > MAX_HEADER_PAYLOAD_BYTES:
        raise CaptureValidationError(
            f"header payload length {header_length} is outside [1, "
            f"{MAX_HEADER_PAYLOAD_BYTES}]"
        )
    header_payload = _read_exact(stream, header_length, "header payload")
    capture_hash.update(header_payload)
    header_checksum = _read_exact(stream, _SHA256_BYTES, "header checksum")
    capture_hash.update(header_checksum)
    expected_header_checksum = hashlib.sha256(header_payload).digest()
    if not hmac.compare_digest(header_checksum, expected_header_checksum):
        raise CaptureValidationError("header payload SHA-256 mismatch")
    header = _parse_header(header_payload)

    records: List[TrackRecord] = []
    while True:
        length_raw = _read_exact(stream, 8, "record length or trailer sentinel")
        payload_length = struct.unpack(">Q", length_raw)[0]
        if payload_length == TRAILER_SENTINEL:
            break
        capture_hash.update(length_raw)
        if payload_length == 0 or payload_length > MAX_RECORD_PAYLOAD_BYTES:
            raise CaptureValidationError(
                f"record {len(records)} payload length {payload_length} is outside "
                f"[1, {MAX_RECORD_PAYLOAD_BYTES}]"
            )
        payload = _read_exact(stream, payload_length, f"record {len(records)} payload")
        capture_hash.update(payload)
        checksum = _read_exact(stream, _SHA256_BYTES, f"record {len(records)} checksum")
        capture_hash.update(checksum)
        expected_checksum = hashlib.sha256(payload).digest()
        if not hmac.compare_digest(checksum, expected_checksum):
            raise CaptureValidationError(f"record {len(records)} payload SHA-256 mismatch")
        records.append(_parse_record(payload, header, len(records)))

        current = records[-1]
        if len(records) == 1:
            if current.update_index != 0 or current.feature_ordinal != 0:
                raise CaptureValidationError(
                    "record 0: capture ordering must begin at update 0, feature 0"
                )
        else:
            previous = records[-2]
            if current.update_index == previous.update_index:
                if current.feature_ordinal != previous.feature_ordinal + 1:
                    raise CaptureValidationError(
                        f"record {current.record_index}: feature ordinal must be contiguous "
                        "within an update"
                    )
                if struct.pack(">d", current.update_timestamp) != struct.pack(
                    ">d", previous.update_timestamp
                ):
                    raise CaptureValidationError(
                        f"record {current.record_index}: timestamp changed within an update"
                    )
            elif current.update_index == previous.update_index + 1:
                if current.feature_ordinal != 0:
                    raise CaptureValidationError(
                        f"record {current.record_index}: a new update must begin at feature 0"
                    )
            else:
                raise CaptureValidationError(
                    f"record {current.record_index}: update index is not contiguous"
                )

    trailer_record_count = struct.unpack(
        ">Q", _read_exact(stream, 8, "trailer record count")
    )[0]
    trailer_checksum = _read_exact(stream, _SHA256_BYTES, "trailer checksum")
    if trailer_record_count != len(records):
        raise CaptureValidationError(
            f"trailer record count is {trailer_record_count}, parsed {len(records)}"
        )
    expected_trailer_checksum = capture_hash.digest()
    if not hmac.compare_digest(trailer_checksum, expected_trailer_checksum):
        raise CaptureValidationError("trailer SHA-256 mismatch")
    if stream.read(1) != b"":
        raise CaptureValidationError("trailing bytes after capture trailer")

    return Capture(
        header=header,
        records=tuple(records),
        trailer_sha256=trailer_checksum.hex(),
    )


def read_capture(path: Union[str, os.PathLike[str]]) -> Capture:
    """Open, parse, and validate one capture file."""

    with open(path, "rb") as stream:
        return parse_capture(stream)


def _json_line(value: Dict[str, Any]) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or summarize a SchurVIO-Lite conditioning capture"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "summary"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("capture", help="schema-1 binary capture path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    try:
        capture = read_capture(args.capture)
        if args.command == "validate":
            output: Dict[str, Any] = {
                "record_count": len(capture.records),
                "schema_version": capture.header.schema_version,
                "status": "valid",
                "trailer_sha256": capture.trailer_sha256,
            }
        else:
            output = capture.summary()
        print(_json_line(output))
        return 0
    except (CaptureValidationError, OSError) as exc:
        print(_json_line({"error": str(exc), "status": "invalid"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
