#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protecting tests for the strict CP2-D evo-result transport."""

from __future__ import annotations

import json
import math
from pathlib import Path
import stat
import struct
import sys
import unittest
import zlib


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_evo_result as evo_result  # noqa: E402


def stats_document(**changes):
    values = {
        "max": 0.5,
        "mean": 0.1,
        "median": 0.1,
        "min": 0.0,
        "rmse": 0.12345678901234567,
        "sse": 1.25,
        "std": 0.125,
    }
    values.update(changes)
    return json.dumps(values, separators=(",", ":"), sort_keys=True).encode("utf-8")


def raw_deflate(payload):
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    return compressor.compress(payload) + compressor.flush()


def build_zip(entries, *, eocd_comment=b""):
    """Build a deterministic classic ZIP and expose record offsets for mutation."""

    local_chunks = []
    central_chunks = []
    metadata = []
    local_offset = 0
    for entry in entries:
        name = entry[0]
        payload = entry[1]
        compression = entry[2] if len(entry) > 2 else evo_result.STORE
        flags = entry[3] if len(entry) > 3 else 0
        extra = entry[4] if len(entry) > 4 else b""
        external = entry[5] if len(entry) > 5 else ((stat.S_IFREG | 0o600) << 16)
        encoded_name = name if isinstance(name, bytes) else name.encode("utf-8")
        compressed = payload if compression == evo_result.STORE else raw_deflate(payload)
        crc = zlib.crc32(payload) & 0xFFFFFFFF
        local_header = struct.pack(
            "<I5H3I2H",
            evo_result.LOCAL_SIGNATURE,
            20,
            flags,
            compression,
            0,
            0,
            crc,
            len(compressed),
            len(payload),
            len(encoded_name),
            len(extra),
        )
        local_chunks.append(local_header + encoded_name + extra + compressed)
        central_header = struct.pack(
            "<I6H3I5H2I",
            evo_result.CENTRAL_SIGNATURE,
            (3 << 8) | 20,
            20,
            flags,
            compression,
            0,
            0,
            crc,
            len(compressed),
            len(payload),
            len(encoded_name),
            len(extra),
            0,
            0,
            0,
            external,
            local_offset,
        )
        central_chunks.append(central_header + encoded_name + extra)
        metadata.append((local_offset, len(local_header), len(encoded_name), len(extra), len(compressed)))
        local_offset += len(local_chunks[-1])
    central = b"".join(central_chunks)
    eocd = struct.pack(
        "<I4H2IH",
        evo_result.EOCD_SIGNATURE,
        0,
        0,
        len(entries),
        len(entries),
        len(central),
        local_offset,
        len(eocd_comment),
    )
    return b"".join(local_chunks) + central + eocd + eocd_comment, metadata


def mutate_u16(document, offset, value):
    changed = bytearray(document)
    struct.pack_into("<H", changed, offset, value)
    return bytes(changed)


def mutate_u32(document, offset, value):
    changed = bytearray(document)
    struct.pack_into("<I", changed, offset, value)
    return bytes(changed)


def next_up(value):
    bits = struct.unpack(">Q", struct.pack(">d", value))[0]
    return struct.unpack(">d", struct.pack(">Q", bits + 1))[0]


class ValidArchiveTests(unittest.TestCase):
    def test_store_archive_returns_exact_seven_statistics(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        stats = evo_result.parse_evo_result_zip(archive)
        self.assertEqual(tuple(stats.as_mapping()), evo_result.STAT_KEYS)
        self.assertEqual(stats.rmse, 0.12345678901234567)
        self.assertEqual(stats.min, 0.0)

    def test_raw_deflate_stats_and_nonsemantic_member_are_accepted(self):
        archive, _ = build_zip(
            [
                ("stats.json", stats_document(rmse=0.25), evo_result.DEFLATE),
                ("info.json", b"opaque evaluator metadata", evo_result.DEFLATE),
            ]
        )
        self.assertEqual(evo_result.parse_evo_result_zip(archive).rmse, 0.25)

    def test_utf8_nfc_nested_regular_name_is_structurally_accepted(self):
        archive, _ = build_zip(
            [
                ("stats.json", stats_document()),
                ("nested/\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"opaque", evo_result.STORE, evo_result.UTF8_FLAG),
            ]
        )
        self.assertEqual(evo_result.parse_evo_result_zip(archive).max, 0.5)


class ContainerBoundaryTests(unittest.TestCase):
    def test_archive_requires_immutable_bounded_bytes(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        class BytesSubclass(bytes):
            pass

        for value in (bytearray(archive), memoryview(archive), BytesSubclass(archive), b"", b"x" * 22):
            with self.subTest(kind=type(value).__name__, length=len(value)):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(value)

    def test_eocd_comment_trailing_bytes_and_preamble_are_rejected(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        commented, _ = build_zip([("stats.json", stats_document())], eocd_comment=b"x")
        for changed in (commented, archive + b"x", b"x" + archive):
            with self.subTest(length=len(changed)):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(changed)

    def test_multidisk_entry_mismatch_and_zip64_eocd_are_rejected(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        eocd = len(archive) - 22
        corruptions = (
            mutate_u16(archive, eocd + 4, 1),
            mutate_u16(archive, eocd + 8, 0),
            mutate_u16(archive, eocd + 10, 0xFFFF),
            mutate_u32(archive, eocd + 12, 0xFFFFFFFF),
            mutate_u32(archive, eocd + 16, 0xFFFFFFFF),
        )
        for changed in corruptions:
            with self.subTest(corruption=corruptions.index(changed)):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(changed)

    def test_central_gap_size_and_offset_corruptions_are_rejected(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        eocd = len(archive) - 22
        central_size = struct.unpack_from("<I", archive, eocd + 12)[0]
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        for changed in (
            mutate_u32(archive, eocd + 12, central_size - 1),
            mutate_u32(archive, eocd + 16, central_offset - 1),
        ):
            with self.assertRaises(evo_result.EvoResultError):
                evo_result.parse_evo_result_zip(changed)

    def test_local_gap_overlap_and_duplicate_offset_are_rejected(self):
        archive, _ = build_zip(
            [("stats.json", stats_document()), ("info.json", b"opaque")]
        )
        eocd = len(archive) - 22
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        first_central_size = 46 + len("stats.json")
        second_offset_field = central_offset + first_central_size + 42
        for replacement in (1, 0):
            changed = mutate_u32(archive, second_offset_field, replacement)
            with self.subTest(replacement=replacement):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(changed)


class HeaderAndNameTests(unittest.TestCase):
    def test_encryption_descriptor_unknown_flags_and_method_are_rejected(self):
        for flags in (1, 1 << 3, 1 << 14):
            archive, _ = build_zip([("stats.json", stats_document(), evo_result.STORE, flags)])
            with self.subTest(flags=flags):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)
        archive, _ = build_zip([("stats.json", stats_document(), 12)])
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(archive)

    def test_member_and_eocd_comments_are_rejected(self):
        archive, _ = build_zip([("stats.json", stats_document())])
        eocd = len(archive) - 22
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        changed = mutate_u16(archive, central_offset + 32, 1)
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(changed)

    def test_zip64_extra_and_truncated_extra_are_rejected(self):
        duplicate_extra = struct.pack("<HHHH", 0x5455, 0, 0x5455, 0)
        for extra in (struct.pack("<HH", evo_result.ZIP64_EXTRA_ID, 0), b"x", duplicate_extra):
            archive, _ = build_zip([("stats.json", stats_document(), evo_result.STORE, 0, extra)])
            with self.subTest(extra=extra):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_every_extra_field_and_creator_host_spoof_are_rejected(self):
        archive, _ = build_zip(
            [("stats.json", stats_document()), ("opaque", b"x")]
        )
        eocd = len(archive) - 22
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        first_central_size = 46 + len("stats.json")
        second_central = central_offset + first_central_size
        host_spoof = mutate_u16(archive, second_central + 4, 20)
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(host_spoof)

        unknown_extra = struct.pack("<HH", 0xCAFE, 0)
        with_extra, _ = build_zip(
            [("stats.json", stats_document(), evo_result.STORE, 0, unknown_extra)]
        )
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(with_extra)

    def test_deflate_requires_zip20_extraction_version(self):
        archive, _ = build_zip([("stats.json", stats_document(), evo_result.DEFLATE)])
        eocd = len(archive) - 22
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        changed = mutate_u16(mutate_u16(archive, 4, 10), central_offset + 6, 10)
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(changed)

    def test_unsafe_non_utf8_and_non_nfc_names_are_rejected(self):
        unsafe = (
            "/stats.json",
            "../stats.json",
            "x//stats.json",
            "x\\stats.json",
            "C:stats.json",
            "stats.json/",
            "e\N{COMBINING ACUTE ACCENT}.txt",
            b"\xff",
        )
        for name in unsafe:
            archive, _ = build_zip([(name, stats_document())])
            with self.subTest(name=name):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

        unflagged_utf8, _ = build_zip(
            [("stats.json", stats_document()), ("\N{LATIN SMALL LETTER E WITH ACUTE}.txt", b"opaque")]
        )
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.parse_evo_result_zip(unflagged_utf8)

    def test_duplicate_names_and_missing_root_stats_are_rejected(self):
        duplicate, _ = build_zip(
            [("stats.json", stats_document()), ("stats.json", stats_document())]
        )
        missing, _ = build_zip([("nested/stats.json", stats_document())])
        for archive in (duplicate, missing):
            with self.assertRaises(evo_result.EvoResultError):
                evo_result.parse_evo_result_zip(archive)

    def test_symlink_directory_and_special_unix_members_are_rejected(self):
        modes = (stat.S_IFLNK | 0o777, stat.S_IFDIR | 0o700, stat.S_IFCHR | 0o600)
        for mode in modes:
            archive, _ = build_zip(
                [("stats.json", stats_document()), ("other", b"opaque", evo_result.STORE, 0, b"", mode << 16)]
            )
            with self.subTest(mode=mode):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_local_central_name_crc_size_and_extra_mismatch_are_rejected(self):
        archive, metadata = build_zip([("stats.json", stats_document())])
        local_offset, header_size, name_size, _, _ = metadata[0]
        name_start = local_offset + header_size
        changed_name = bytearray(archive)
        changed_name[name_start] = ord("S")
        corruptions = (
            bytes(changed_name),
            mutate_u32(archive, local_offset + 14, 0),
            mutate_u32(archive, local_offset + 18, 0),
            mutate_u16(archive, local_offset + 28, 1),
        )
        self.assertEqual(name_size, len("stats.json"))
        for changed in corruptions:
            with self.assertRaises(evo_result.EvoResultError):
                evo_result.parse_evo_result_zip(changed)


class StatsPayloadTests(unittest.TestCase):
    def test_crc_size_and_malformed_deflate_are_rejected(self):
        stored, _ = build_zip([("stats.json", stats_document())])
        eocd = len(stored) - 22
        central_offset = struct.unpack_from("<I", stored, eocd + 16)[0]
        crc_corrupt = mutate_u32(mutate_u32(stored, 14, 0), central_offset + 16, 0)

        compressed, metadata = build_zip([("stats.json", stats_document(), evo_result.DEFLATE)])
        compressed_eocd = len(compressed) - 22
        compressed_central = struct.unpack_from("<I", compressed, compressed_eocd + 16)[0]
        local_offset, header_size, name_size, extra_size, compressed_size = metadata[0]
        payload_start = local_offset + header_size + name_size + extra_size
        self.assertGreater(compressed_size, 2)
        malformed = bytearray(compressed)
        malformed[payload_start + compressed_size // 2] ^= 0xFF
        declared_size = len(stats_document()) + 1
        size_corrupt = mutate_u32(
            mutate_u32(compressed, local_offset + 22, declared_size),
            compressed_central + 24,
            declared_size,
        )
        for archive in (crc_corrupt, bytes(malformed), size_corrupt):
            with self.assertRaises(evo_result.EvoResultError):
                evo_result.parse_evo_result_zip(archive)

    def test_invalid_utf8_and_invalid_json_are_rejected(self):
        for payload in (b"\xff", b"{", b"[]"):
            archive, _ = build_zip([("stats.json", payload)])
            with self.subTest(payload=payload):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_missing_extra_and_duplicate_stat_keys_are_rejected(self):
        base = '"max":1,"mean":1,"median":1,"min":0,"rmse":1,"sse":1,"std":1'
        payloads = (
            ("{" + base.replace(',"std":1', '') + "}").encode(),
            ("{" + base + ',"other":1}').encode(),
            ("{" + base + ',"rmse":1}').encode(),
        )
        for payload in payloads:
            archive, _ = build_zip([("stats.json", payload)])
            with self.subTest(payload=payload):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_boolean_string_null_nan_and_infinity_values_are_rejected(self):
        for value in (True, "1", None, float("nan"), float("inf"), -float("inf")):
            archive, _ = build_zip([("stats.json", stats_document(rmse=value))])
            with self.subTest(value=value):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_negative_negative_zero_overflow_and_underflow_are_rejected(self):
        replacements = ("-1", "-0", "-0.0", "1e309", "1e-9999")
        for token in replacements:
            payload = (
                '{"max":1,"mean":1,"median":1,"min":0,"rmse":'
                + token
                + ',"sse":1,"std":1}'
            ).encode()
            archive, _ = build_zip([("stats.json", payload)])
            with self.subTest(token=token):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_impossible_statistic_orderings_are_rejected(self):
        impossible = (
            stats_document(min=0.4, median=0.2),
            stats_document(mean=0.75),
            stats_document(rmse=0.75),
            stats_document(mean=0.25, rmse=next_up(0.0)),
            stats_document(max=0.0, mean=0.0, median=0.0, min=0.0, rmse=0.0, sse=1.0, std=0.0),
        )
        for payload in impossible:
            archive, _ = build_zip([("stats.json", payload)])
            with self.subTest(payload=payload):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.parse_evo_result_zip(archive)

    def test_rmse_mean_exact_boundary_and_one_ulp_inversion(self):
        mean = 0.25
        equal, _ = build_zip(
            [("stats.json", stats_document(mean=mean, median=mean, rmse=mean))]
        )
        above, _ = build_zip(
            [("stats.json", stats_document(mean=mean, median=mean, rmse=next_up(mean)))]
        )
        self.assertEqual(evo_result.parse_evo_result_zip(equal).rmse, mean)
        self.assertEqual(evo_result.parse_evo_result_zip(above).rmse, next_up(mean))

        mean_bits = struct.unpack(">Q", struct.pack(">d", mean))[0]
        one_ulp_below = struct.unpack(">d", struct.pack(">Q", mean_bits - 1))[0]
        inverted, _ = build_zip(
            [("stats.json", stats_document(mean=mean, median=mean, rmse=one_ulp_below))]
        )
        with self.assertRaisesRegex(
            evo_result.EvoResultError, "RMSE is below the arithmetic mean"
        ):
            evo_result.parse_evo_result_zip(inverted)


class NumericJoinTests(unittest.TestCase):
    def test_console_token_uses_exact_evo_six_decimal_rendering(self):
        self.assertEqual(evo_result.expected_console_rmse(0.12345678901234567), "0.123457")
        self.assertEqual(
            evo_result.require_console_rmse_match("0.123457", 0.12345678901234567),
            "0.123457",
        )
        for token in ("0.123456789", "0.123456", "rmse 0.123457", 0.123457):
            with self.subTest(token=token):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.require_console_rmse_match(token, 0.12345678901234567)

    def test_archive_direct_agreement_retains_frozen_terms_and_boundary(self):
        direct = 0.25
        permitted = (
            evo_result.ARCHIVE_DIRECT_ABSOLUTE_TOLERANCE
            + evo_result.ARCHIVE_DIRECT_RELATIVE_TOLERANCE * direct
        )
        at_boundary = direct + permitted
        agreement = evo_result.require_archive_direct_rmse_agreement(at_boundary, direct)
        self.assertEqual(agreement.archive_rmse, at_boundary)
        self.assertEqual(agreement.direct_rmse, direct)
        self.assertEqual(agreement.permitted_difference, permitted)
        self.assertLessEqual(agreement.absolute_difference, agreement.permitted_difference)
        above = next_up(at_boundary)
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.require_archive_direct_rmse_agreement(above, direct)

    def test_rmse_agreement_record_cannot_be_forged_directly(self):
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.RmseAgreement(0.25, 0.25, 1.0, 2.0)

    def test_console_rounding_ties_and_decimal_carry_are_frozen(self):
        self.assertEqual(evo_result.expected_console_rmse(0.0000005), "0.000000")
        self.assertEqual(evo_result.expected_console_rmse(0.0000015), "0.000002")
        self.assertEqual(evo_result.expected_console_rmse(0.9999996), "1.000000")

    def test_archive_direct_drift_and_invalid_metrics_fail_closed(self):
        with self.assertRaises(evo_result.EvoResultError):
            evo_result.require_archive_direct_rmse_agreement(0.250000001, 0.25)
        for archive_value, direct_value in (
            (-0.0, 0.0),
            (0.0, -0.0),
            (float("nan"), 0.0),
            (0.0, float("inf")),
            (True, 0.0),
        ):
            with self.subTest(archive=archive_value, direct=direct_value):
                with self.assertRaises(evo_result.EvoResultError):
                    evo_result.require_archive_direct_rmse_agreement(archive_value, direct_value)


if __name__ == "__main__":
    unittest.main()
