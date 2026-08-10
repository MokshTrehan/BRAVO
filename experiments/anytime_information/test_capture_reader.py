#!/usr/bin/env python3
"""Focused tests and explicit C++-wire fixtures for capture schema 2."""

from __future__ import annotations

import hashlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from typing import Iterable, Sequence

if __package__:
    from .capture_reader import (
        ENVELOPE_DOMAIN,
        MAGIC,
        TRAILER_SENTINEL,
        CaptureValidationError,
        EnvelopeStream,
        TerminalStatus,
        TrackLifecycle,
        create_manifest,
        parse_capture,
        validate_capture,
    )
else:
    from capture_reader import (
        ENVELOPE_DOMAIN,
        MAGIC,
        TRAILER_SENTINEL,
        CaptureValidationError,
        EnvelopeStream,
        TerminalStatus,
        TrackLifecycle,
        create_manifest,
        parse_capture,
        validate_capture,
    )


# These helpers intentionally live only in the test.  Their order mirrors
# UpdateEnvelopeCaptureWriter::Impl::encode field for field; production code is
# a reader and cannot accidentally become a competing capture writer.
def _u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def _i64(value: int) -> bytes:
    return struct.pack(">q", value)


def _f64(value: float) -> bytes:
    return struct.pack(">d", value)


def _boolean(value: bool | int) -> bytes:
    return _u64(int(value))


def _utf8(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _u64(len(encoded)) + encoded


def _matrix(rows: int, cols: int, values: Iterable[float]) -> bytes:
    values_tuple = tuple(values)
    if len(values_tuple) != rows * cols:
        raise AssertionError("fixture matrix shape does not match values")
    return _u64(rows) + _u64(cols) + b"".join(_f64(v) for v in values_tuple)


def _layout(blocks: Sequence[tuple[int, int, int]] = ((0, 0, 2),)) -> bytes:
    return _u64(len(blocks)) + b"".join(
        _u64(local) + _u64(covariance) + _u64(size)
        for local, covariance, size in blocks
    )


def _system(*, available: bool, duration_ns: int) -> bytes:
    if available:
        return b"".join(
            (
                _boolean(True),
                _u64(duration_ns),
                _layout(),
                _matrix(1, 2, (1.0, -0.5)),
                _matrix(1, 1, (0.25,)),
                _matrix(1, 1, (2.25,)),
            )
        )
    return b"".join(
        (
            _boolean(False),
            _u64(0),
            _layout(()),
            _matrix(0, 0, ()),
            _matrix(0, 1, ()),
            _matrix(0, 0, ()),
        )
    )


def _prefactor(
    *, ordinal: int, prefilter_recorded: int, lifecycle_status: int
) -> bytes:
    observations = b"".join(
        (
            _u64(0),
            _f64(10.0),
            _f64(100.0),
            _f64(120.0),
            _f64(0.1),
            _f64(0.2),
            _u64(0),
            _f64(10.1),
            _f64(105.0),
            _f64(123.0),
            _f64(0.15),
            _f64(0.23),
        )
    )
    return b"".join(
        (
            _u64(ordinal),
            _u64(42),
            _u64(1),  # lost candidate reason
            _u64(lifecycle_status),
            _u64(2),  # raw observation count
            _u64(2),
            _boolean(prefilter_recorded),
            _boolean(True),
            _u64(100),
            _boolean(True),
            _f64(10.0),
            _f64(10.1),
            _f64(0.1),
            _u64(2),
            observations,
            _boolean(True),
            _f64(0.05),
            _boolean(True),
            _f64(5.831),
            _boolean(True),
            _f64(5.831),
            _boolean(True),
            _f64(0.15),
            _f64(0.23),
            _boolean(False),
            _u64(0),
            _f64(0.0),
            _f64(0.0),
        )
    )


def _track(
    *,
    ordinal: int = 0,
    accepted: bool = True,
    prefilter_recorded: int = 1,
    global_row_start: int = 0,
    lifecycle_status: int = int(TrackLifecycle.ACCUMULATED),
) -> bytes:
    return b"".join(
        (
            _u64(1),  # prefilter group
            _prefactor(
                ordinal=ordinal,
                prefilter_recorded=prefilter_recorded,
                lifecycle_status=lifecycle_status,
            ),
            _u64(2),  # geometry group
            _boolean(True),
            _boolean(True),
            _boolean(True),
            _boolean(True),
            _boolean(True),
            _boolean(True),
            _u64(200),
            _matrix(3, 1, (1.0, 2.0, 4.0)),
            _boolean(True),
            _f64(4.0),
            _boolean(True),
            _f64(0.2),
            _u64(3),  # raw-factor group
            _boolean(True),
            _u64(300),
            _f64(1.5),
            _f64(2.25),
            _u64(1),  # scalar isotropic whitening representation
            _layout(),
            _matrix(4, 2, (1.0, 0.0, 0.0, 1.0, 0.5, -0.5, 1.0, 1.0)),
            _matrix(
                4,
                3,
                (1.0, 0.0, 0.0, 0.0, 1.0, 0.0,
                 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            ),
            _matrix(4, 1, (0.1, -0.2, 0.3, -0.4)),
            _u64(4),  # reduction group
            _boolean(True),
            _u64(2),  # Schur reducer
            _u64(0),
            _u64(0),
            _boolean(True),
            _matrix(3, 1, (4.0, 2.0, 0.5)),
            _i64(3),
            _boolean(True),
            _f64(0.125),
            _matrix(1, 2, (1.0, -0.5)),
            _matrix(1, 1, (0.25,)),
            _u64(400),
            _u64(5),  # gate group
            _boolean(True),
            _u64(0),
            _i64(1),
            _boolean(True),
            _f64(1.25),
            _boolean(True),
            _f64(9.49),
            _boolean(True),
            _boolean(accepted),
            _boolean(accepted),
            _u64(500),
            _u64(6),  # accumulation group
            _boolean(accepted),
            _u64(global_row_start if accepted else 0),
            _u64(1 if accepted else 0),
            _u64(600),
        )
    )


def _camera() -> bytes:
    intrinsics = (450.0, 451.0, 320.0, 240.0, 0.1, -0.01, 0.001, -0.001)
    return b"".join(
        (
            _u64(0),  # callback group
            _u64(0),
            _u64(1),  # radtan
            _i64(640),
            _i64(480),
            _i64(0),
            _i64(0),
            _matrix(7, 1, (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
            _matrix(7, 1, (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
            _matrix(8, 1, intrinsics),
            _matrix(8, 1, intrinsics),
            _matrix(8, 1, intrinsics),
        )
    )


def _state_and_prior() -> bytes:
    return b"".join(
        (
            _u64(1),  # semantic block count
            _u64(0),  # callback group
            _u64(0),  # ordinal
            _u64(7),  # camera-time-offset block type
            _u64(0),  # no key
            _u64(0),
            _f64(0.0),
            _i64(0),  # variable ID
            _i64(0),  # covariance ID
            _i64(0),  # offset
            _i64(2),  # error dimension
            _u64(3),  # current and FEJ roles
            _matrix(2, 1, (0.1, 0.2)),
            _matrix(2, 1, (0.09, 0.19)),
            _matrix(2, 2, (2.0, 0.1, 0.1, 3.0)),
        )
    )


def _envelope_payload(
    *,
    update_id: int = 0,
    camera_timestamp: float = 10.1,
    zero_candidates: bool = False,
    candidate_ordinal: int = 0,
    identity_stage: int = 0,
    prefilter_recorded: int = 1,
    global_row_start: int = 0,
    lifecycle_status: int = int(TrackLifecycle.ACCUMULATED),
    timeline_available: int = 1,
    timeline_offsets: Sequence[float] = (
        0.0,
        0.001,
        0.002,
        0.003,
        0.004,
        0.005,
        0.006,
    ),
    output_available: int = 1,
    record_construction_duration_ns: int = 125,
    serialization_duration_ns: int = 250,
    encoded_payload_bytes: int | None = None,
) -> bytes:
    committed = not zero_candidates
    status = TerminalStatus.COMMITTED if committed else TerminalStatus.EMPTY_INPUT
    if len(timeline_offsets) != 7:
        raise AssertionError("callback timeline fixture needs seven endpoints")
    prefix = b"".join(
        (
            _utf8(ENVELOPE_DOMAIN),
            _u64(identity_stage),
            _u64(update_id),
            _f64(camera_timestamp),
            _u64(int(status)),
            _u64(0),
            _u64(1),  # max visual passes
            _u64(2),  # Schur landmark elimination
            _boolean(True),
            _boolean(False),
            _boolean(False),
            _boolean(False),
            _boolean(False),
            _boolean(False),
            _i64(0),  # feature representation
            _u64(10),  # callback-finalization group
            _f64(0.001),
            _f64(0.001),
            _f64(0.001),
            _f64(0.001),
            _f64(0.001),
            _f64(0.001),
            _f64(0.006),
            _boolean(timeline_available),
            b"".join(_f64(value) for value in timeline_offsets),
            _u64(11),  # capture-output group
            _boolean(output_available),
            _u64(record_construction_duration_ns),
            _u64(serialization_duration_ns),
        )
    )
    suffix = b"".join(
        (
            _u64(1),  # camera count
            _camera(),
            _state_and_prior(),
            _u64(0 if zero_candidates else 1),
            b""
            if zero_candidates
            else _track(
                ordinal=candidate_ordinal,
                prefilter_recorded=prefilter_recorded,
                global_row_start=global_row_start,
                lifecycle_status=lifecycle_status,
            ),
            _u64(0 if zero_candidates else 1),
            b"" if zero_candidates else _u64(42),
            _u64(6),  # selected-system group
            _system(available=committed, duration_ns=700),
            _u64(7),  # compressed-system group
            _system(available=committed, duration_ns=800),
            _u64(8),  # production-preview group
            _boolean(committed),
            _u64(0),
            _u64(0),
            _boolean(False),
            _boolean(False),
            _f64(0.0),
            _boolean(False),
            _matrix(2, 1, (0.01, -0.02)) if committed else _matrix(0, 1, ()),
            _matrix(2, 2, (1.0, 0.05, 0.05, 2.0))
            if committed
            else _matrix(0, 0, ()),
            _u64(900 if committed else 0),
            _u64(9),  # commit group
            _u64(1 if committed else 0),
            _u64(1 if committed else 0),
            _u64(1 if committed else 0),
            _u64(1000 if committed else 0),
        )
    )
    payload_length = len(prefix) + 8 + len(suffix)
    encoded_length = (
        payload_length if encoded_payload_bytes is None else encoded_payload_bytes
    )
    return prefix + _u64(encoded_length) + suffix


def _header_payload(source_commit: str = "1" * 40) -> bytes:
    return b"".join(
        (
            _u64(2),
            _u64(1),
            _u64(1),
            _utf8(source_commit),
            _utf8("a" * 64),
            _utf8("fixture-run"),
            _utf8("MH_01_easy"),
            _utf8(ENVELOPE_DOMAIN),
        )
    )


def _capture_bytes(
    payloads: Sequence[bytes] | None = None,
    *,
    source_commit: str = "1" * 40,
) -> bytes:
    if payloads is None:
        payloads = (_envelope_payload(),)
    header = _header_payload(source_commit)
    prefix_parts = [MAGIC, _u64(len(header)), header, hashlib.sha256(header).digest()]
    for payload in payloads:
        prefix_parts.extend(
            (_u64(len(payload)), payload, hashlib.sha256(payload).digest())
        )
    prefix = b"".join(prefix_parts)
    return b"".join(
        (
            prefix,
            _u64(TRAILER_SENTINEL),
            _u64(len(payloads)),
            hashlib.sha256(prefix).digest(),
        )
    )


class CaptureReaderTests(unittest.TestCase):
    def test_full_and_zero_candidate_envelopes_round_trip(self) -> None:
        capture = parse_capture(
            io.BytesIO(
                _capture_bytes(
                    (
                        _envelope_payload(),
                        _envelope_payload(
                            update_id=1, camera_timestamp=10.2, zero_candidates=True
                        ),
                    )
                )
            )
        )

        self.assertEqual(capture.header.schema_version, 2)
        self.assertEqual(capture.header.source_commit, "1" * 40)
        self.assertEqual(capture.header.sequence_id, "MH_01_easy")
        self.assertEqual(len(capture.envelopes), 2)
        update = capture.envelopes[0]
        self.assertEqual(update.update_id, 0)
        self.assertEqual(update.p_minus.at(1, 1), 3.0)
        self.assertEqual(update.candidates[0].feature_id, 42)
        self.assertEqual(update.tracks[0].global_row_count, 1)
        self.assertEqual(update.accepted_feature_ids, (42,))
        self.assertEqual(update.terminal_status, TerminalStatus.COMMITTED)
        self.assertEqual(update.mean_commit_count, 1)
        self.assertTrue(update.callback_timeline.available)
        self.assertEqual(update.callback_timeline.callback_begin, 0.0)
        self.assertEqual(update.callback_timeline.finalization_end, 0.006)
        self.assertTrue(update.capture_output.available)
        self.assertEqual(
            update.capture_output.encoded_payload_bytes,
            len(_envelope_payload()),
        )
        self.assertGreater(update.capture_output.serialization_duration_ns, 0)
        self.assertEqual(len(capture.envelopes[1].candidates), 0)

    def test_stream_does_not_require_collecting_prior_envelopes(self) -> None:
        reader = EnvelopeStream(
            io.BytesIO(
                _capture_bytes(
                    (
                        _envelope_payload(),
                        _envelope_payload(
                            update_id=1, camera_timestamp=10.2, zero_candidates=True
                        ),
                    )
                )
            )
        )
        first = next(reader)
        self.assertEqual(first.update_id, 0)
        self.assertFalse(reader.finished)
        reader.finish()
        self.assertTrue(reader.finished)
        self.assertEqual(reader.envelope_count, 2)
        self.assertEqual(len(reader.trailer_sha256), 64)

    def test_each_checksum_layer_is_rejected(self) -> None:
        good = _capture_bytes()

        bad_header = bytearray(good)
        bad_header[bad_header.index(b"fixture-run")] ^= 1
        with self.assertRaisesRegex(CaptureValidationError, "header payload SHA-256"):
            parse_capture(io.BytesIO(bad_header))

        bad_envelope = bytearray(good)
        domain_offset = bad_envelope.index(
            ENVELOPE_DOMAIN.encode(), len(MAGIC) + 8 + len(_header_payload())
        )
        bad_envelope[domain_offset] ^= 1
        with self.assertRaisesRegex(CaptureValidationError, "envelope 0 payload SHA-256"):
            parse_capture(io.BytesIO(bad_envelope))

        bad_trailer = bytearray(good)
        bad_trailer[-1] ^= 1
        with self.assertRaisesRegex(CaptureValidationError, "trailer SHA-256"):
            parse_capture(io.BytesIO(bad_trailer))

    def test_truncation_and_trailing_bytes_are_rejected(self) -> None:
        good = _capture_bytes()
        with self.assertRaisesRegex(CaptureValidationError, "truncated trailer checksum"):
            parse_capture(io.BytesIO(good[:-1]))
        with self.assertRaisesRegex(CaptureValidationError, "trailing bytes"):
            parse_capture(io.BytesIO(good + b"x"))

    def test_wrong_stage_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "identity stage"):
            parse_capture(
                io.BytesIO(_capture_bytes((_envelope_payload(identity_stage=1),)))
            )

    def test_noncanonical_boolean_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "canonical 0 or 1"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes((_envelope_payload(prefilter_recorded=2),))
                )
            )

    def test_source_commit_must_be_exact_lowercase_hex(self) -> None:
        for invalid in ("unknown", "A" * 40, "1" * 39):
            with self.subTest(source_commit=invalid):
                with self.assertRaisesRegex(
                    CaptureValidationError, "exactly lowercase 40-hex"
                ):
                    parse_capture(
                        io.BytesIO(_capture_bytes(source_commit=invalid))
                    )

    def test_callback_timeline_is_required_and_canonical(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "timeline is unavailable"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (_envelope_payload(timeline_available=0),)
                    )
                )
            )
        with self.assertRaisesRegex(CaptureValidationError, r"canonical \+0.0"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (
                            _envelope_payload(
                                timeline_offsets=(
                                    -0.0,
                                    0.001,
                                    0.002,
                                    0.003,
                                    0.004,
                                    0.005,
                                    0.006,
                                )
                            ),
                        )
                    )
                )
            )

    def test_callback_timeline_must_be_monotonic_and_match_costs(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "not monotonic"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (
                            _envelope_payload(
                                timeline_offsets=(
                                    0.0,
                                    0.001,
                                    0.0005,
                                    0.003,
                                    0.004,
                                    0.005,
                                    0.006,
                                )
                            ),
                        )
                    )
                )
            )
        with self.assertRaisesRegex(
            CaptureValidationError, "finalization duration disagrees"
        ):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (
                            _envelope_payload(
                                timeline_offsets=(
                                    0.0,
                                    0.001,
                                    0.002,
                                    0.003,
                                    0.004,
                                    0.005,
                                    0.007,
                                )
                            ),
                        )
                    )
                )
            )

    def test_capture_output_metadata_is_required_and_self_consistent(self) -> None:
        with self.assertRaisesRegex(
            CaptureValidationError, "capture-output metadata is unavailable"
        ):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (
                            _envelope_payload(
                                output_available=0,
                                record_construction_duration_ns=0,
                                serialization_duration_ns=0,
                                encoded_payload_bytes=0,
                            ),
                        )
                    )
                )
            )
        with self.assertRaisesRegex(
            CaptureValidationError, "serialization duration must be positive"
        ):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (_envelope_payload(serialization_duration_ns=0),)
                    )
                )
            )
        with self.assertRaisesRegex(CaptureValidationError, "payload byte count"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes((_envelope_payload(encoded_payload_bytes=1),))
                )
            )

    def test_lifecycle_status_is_bounded_and_stage_coherent(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "unknown lifecycle status"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes((_envelope_payload(lifecycle_status=9),))
                )
            )
        with self.assertRaisesRegex(CaptureValidationError, "is incoherent"):
            parse_capture(
                io.BytesIO(
                    _capture_bytes(
                        (
                            _envelope_payload(
                                lifecycle_status=int(TrackLifecycle.GATE_ACCEPTED)
                            ),
                        )
                    )
                )
            )

    def test_noncontiguous_candidate_ordinal_is_rejected(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "candidate ordinal"):
            parse_capture(
                io.BytesIO(_capture_bytes((_envelope_payload(candidate_ordinal=1),)))
            )

    def test_accepted_row_range_must_tile_selected_system(self) -> None:
        with self.assertRaisesRegex(CaptureValidationError, "row ranges"):
            parse_capture(
                io.BytesIO(_capture_bytes((_envelope_payload(global_row_start=1),)))
            )

    def test_manifest_is_deterministic_relative_and_create_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            captures = root / "captures"
            captures.mkdir()
            capture_path = captures / "mh01.schema2"
            capture_path.write_bytes(_capture_bytes())
            output = root / "manifest.json"

            manifest = create_manifest(output, (capture_path,))
            entry = manifest["captures"][0]
            self.assertEqual(entry["relative_path"], "captures/mh01.schema2")
            self.assertEqual(entry["update_count"], 1)
            self.assertEqual(entry["candidate_count"], 1)
            self.assertEqual(entry["accepted_count"], 1)
            self.assertEqual(entry["strict_validation"], "valid")
            self.assertEqual(entry["sha256"], hashlib.sha256(_capture_bytes()).hexdigest())
            self.assertEqual(json.loads(output.read_text()), manifest)

            with self.assertRaises(FileExistsError):
                create_manifest(output, (capture_path,))

    def test_streaming_summary_accounts_for_zero_and_no_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "capture.bin"
            path.write_bytes(
                _capture_bytes(
                    (
                        _envelope_payload(),
                        _envelope_payload(
                            update_id=1, camera_timestamp=10.2, zero_candidates=True
                        ),
                    )
                )
            )
            summary = validate_capture(path)
            self.assertEqual(summary.update_count, 2)
            self.assertEqual(summary.candidate_count, 1)
            self.assertEqual(summary.accepted_count, 1)
            self.assertEqual(summary.zero_candidate_count, 1)
            self.assertEqual(summary.no_update_count, 1)
            self.assertEqual(summary.first_timestamp, 10.1)
            self.assertEqual(summary.last_timestamp, 10.2)


if __name__ == "__main__":
    unittest.main()
