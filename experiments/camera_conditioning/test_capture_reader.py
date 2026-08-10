#!/usr/bin/python3
"""Unit tests for the deterministic camera-conditioning capture reader."""

from __future__ import annotations

import hashlib
import io
import math
import struct
import unittest
from typing import Iterable, Sequence

if __package__:
    from .capture_reader import (
        FROZEN_MIN_SINGULAR_RATIO,
        MAGIC,
        TRACK_DOMAIN,
        TRAILER_SENTINEL,
        CaptureValidationError,
        parse_capture,
    )
else:
    from capture_reader import (
        FROZEN_MIN_SINGULAR_RATIO,
        MAGIC,
        TRACK_DOMAIN,
        TRAILER_SENTINEL,
        CaptureValidationError,
        parse_capture,
    )


def _u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def _i64(value: int) -> bytes:
    return struct.pack(">q", value)


def _f64(value: float) -> bytes:
    return struct.pack(">d", value)


def _utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _matrix(rows: int, cols: int, values: Iterable[float]) -> bytes:
    values_tuple = tuple(values)
    if len(values_tuple) != rows * cols:
        raise AssertionError("fixture matrix dimensions do not match its values")
    return _u64(rows) + _u64(cols) + b"".join(_f64(value) for value in values_tuple)


def _record_payload(
    *,
    layout_local_columns: Sequence[int] = (0, 1),
    h_x_first: float = 1.0,
) -> bytes:
    parts = [
        _utf8(TRACK_DOMAIN),
        _u64(0),  # record_index
        _u64(0),  # update_index
        _u64(0),  # feature_ordinal
        _f64(123.5),
        _u64(99),  # feature_id
        _u64(1),  # geometry_valid
        _u64(1),  # fej_enabled
        _u64(0),  # calibration_flags
        _i64(0),  # feature_representation
        _f64(1.5),
        _f64(2.25),
        _u64(1),  # whitening_kind
        _f64(FROZEN_MIN_SINGULAR_RATIO),
        _u64(1),  # singular_values_available
        _matrix(3, 1, (4.0, 2.0, 0.4)),
        _i64(3),
        _u64(1),  # singular_ratio_available
        _f64(0.1),
        _matrix(4, 2, (h_x_first, 0.0, 0.5, 1.0, -1.0, 0.25, 0.0, 2.0)),
        _matrix(4, 3, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0)),
        _matrix(4, 1, (0.1, -0.2, 0.3, -0.4)),
        _matrix(2, 2, (2.0, 0.25, 0.25, 3.0)),
        _matrix(3, 1, (1.0, 2.0, 3.0)),
        _u64(2),  # layout_count
    ]
    for local_column, covariance_column, key in zip(
        layout_local_columns, (10, 20), (100, 101)
    ):
        parts.extend(
            (
                _u64(local_column),
                _u64(covariance_column),
                _u64(1),
                _u64(1),
                _u64(1),
                _u64(key),
                _f64(0.0),
            )
        )
    parts.extend(
        (
            _u64(2),  # observation_count
            _u64(0),
            _f64(123.4),
            _u64(1),
            _f64(4.0),
            _u64(0),
            _f64(123.5),
            _u64(1),
            _f64(3.5),
            _f64(3.5),
            _f64(0.2),
        )
    )
    return b"".join(parts)


def _capture_bytes(
    *,
    layout_local_columns: Sequence[int] = (0, 1),
    h_x_first: float = 1.0,
) -> bytes:
    header_payload = b"".join(
        (
            _u64(1),
            _u64(1),
            _utf8("unknown"),
            _utf8("a" * 64),
            _f64(FROZEN_MIN_SINGULAR_RATIO),
        )
    )
    record_payload = _record_payload(
        layout_local_columns=layout_local_columns, h_x_first=h_x_first
    )
    prefix = b"".join(
        (
            MAGIC,
            _u64(len(header_payload)),
            header_payload,
            hashlib.sha256(header_payload).digest(),
            _u64(len(record_payload)),
            record_payload,
            hashlib.sha256(record_payload).digest(),
        )
    )
    return b"".join(
        (
            prefix,
            _u64(TRAILER_SENTINEL),
            _u64(1),
            hashlib.sha256(prefix).digest(),
        )
    )


class CaptureReaderTests(unittest.TestCase):
    def test_roundtrip_like_fixture_parses_and_summarizes(self) -> None:
        capture = parse_capture(io.BytesIO(_capture_bytes()))

        self.assertEqual(capture.header.schema_version, 1)
        self.assertEqual(capture.header.source_commit, "unknown")
        self.assertEqual(len(capture.records), 1)
        record = capture.records[0]
        self.assertEqual(record.record_index, 0)
        self.assertEqual(record.h_x.rows, 4)
        self.assertEqual(record.h_x.cols, 2)
        self.assertEqual(record.h_x.at(0, 0), 1.0)
        self.assertEqual([block.local_column for block in record.layout], [0, 1])
        self.assertEqual(len(record.observations), 2)

        summary = capture.summary()
        self.assertEqual(summary["record_count"], 1)
        self.assertEqual(summary["update_count"], 1)
        self.assertEqual(summary["camera_models"], [1])
        self.assertEqual(summary["numerical_rank_counts"], [{"count": 1, "rank": 3}])

    def test_record_checksum_failure_is_rejected(self) -> None:
        corrupted = bytearray(_capture_bytes())
        domain_offset = corrupted.index(TRACK_DOMAIN.encode("utf-8"))
        corrupted[domain_offset] ^= 0x01

        with self.assertRaisesRegex(CaptureValidationError, "record 0 payload SHA-256"):
            parse_capture(io.BytesIO(corrupted))

    def test_noncontiguous_layout_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "starts at local column"):
            parse_capture(
                io.BytesIO(_capture_bytes(layout_local_columns=(0, 2)))
            )

    def test_nonfinite_matrix_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "H_x contains a non-finite"):
            parse_capture(io.BytesIO(_capture_bytes(h_x_first=math.nan)))


if __name__ == "__main__":
    unittest.main()
