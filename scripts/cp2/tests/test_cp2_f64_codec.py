#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic tests for the canonical CP2-D finite-binary64 IPC codec."""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
import struct
import sys
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_f64_codec as codec  # noqa: E402


class ScalarCodecTests(unittest.TestCase):
    def test_known_big_endian_ieee_patterns_and_signed_zero(self):
        cases = (
            (0.0, "0000000000000000"),
            (-0.0, "8000000000000000"),
            (1.0, "3ff0000000000000"),
            (-2.5, "c004000000000000"),
            (float.fromhex("0x0.0000000000001p-1022"), "0000000000000001"),
            (float.fromhex("0x1.fffffffffffffp+1023"), "7fefffffffffffff"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(codec.f64_to_bits_hex(value), expected)
                decoded = codec.bits_hex_to_f64(expected)
                self.assertEqual(struct.pack(">d", decoded).hex(), expected)

        self.assertEqual(math.copysign(1.0, codec.bits_hex_to_f64("8000000000000000")), -1.0)
        # The frozen network-byte-order answer does not depend on host byte order.
        self.assertEqual(codec.f64_to_bits_hex(1.0), struct.pack(">d", 1.0).hex())
        self.assertEqual(codec.f64_to_bits_hex(1.0), "3ff0000000000000")

    def test_scalar_type_syntax_and_finiteness_are_strict(self):
        for value in (1, True, "1.0", None):
            with self.subTest(value=value):
                with self.assertRaises(codec.F64CodecError):
                    codec.f64_to_bits_hex(value)
        for value in (math.inf, -math.inf, math.nan):
            with self.subTest(value=value):
                with self.assertRaises(codec.F64CodecError):
                    codec.f64_to_bits_hex(value)

        rejected = (
            "3FF0000000000000",
            "3ff000000000000",
            "03ff0000000000000",
            "3ff000000000000g",
            " 3ff0000000000000",
            b"3ff0000000000000",
            0,
        )
        for bits in rejected:
            with self.subTest(bits=bits):
                with self.assertRaises(codec.F64CodecError):
                    codec.bits_hex_to_f64(bits)

        for bits in (
            "7ff0000000000000",  # +Inf
            "fff0000000000000",  # -Inf
            "7ff8000000000000",  # quiet NaN
            "7ff0000000000001",  # signalling-NaN bit pattern
            "fff8000000000001",  # negative NaN payload
        ):
            with self.subTest(bits=bits):
                with self.assertRaises(codec.F64CodecError):
                    codec.bits_hex_to_f64(bits)


class ShapeAndArrayTests(unittest.TestCase):
    def test_checked_u64_shape_count_and_zero_extent(self):
        self.assertEqual(codec.checked_element_count([]), 1)
        self.assertEqual(codec.checked_element_count([2, 3, 4]), 24)
        self.assertEqual(codec.checked_element_count([codec.MAX_ELEMENTS]), codec.MAX_ELEMENTS)
        self.assertEqual(codec.checked_element_count([0, codec.U64_MAX]), 0)

        rejected = (
            [codec.U64_MAX, 2],
            [2, codec.U64_MAX],
            [codec.MAX_ELEMENTS + 1],
            [2] * (codec.MAX_RANK + 1),
            [-1],
            [codec.U64_MAX + 1],
            [True],
            [1.0],
            ["1"],
        )
        for shape in rejected:
            with self.subTest(shape=shape):
                with self.assertRaises(codec.F64CodecError):
                    codec.checked_element_count(shape)
        for shape in (None, 1, "2"):
            with self.subTest(shape=shape):
                with self.assertRaises(codec.F64CodecError):
                    codec.checked_element_count(shape)

    def test_deterministic_encode_decode_row_major_bits_and_digest(self):
        values = [1.0, -0.0, 2.5, float.fromhex("0x0.0000000000001p-1022")]
        expected = (
            b'{"bits":["3ff0000000000000","8000000000000000",'
            b'"4004000000000000","0000000000000001"],"shape":[2,2]}\n'
        )
        document = codec.encode_f64_array([2, 2], values)
        self.assertEqual(document, expected)
        self.assertEqual(codec.encode_f64_array((2, 2), tuple(values)), expected)

        decoded = codec.decode_f64_array(document)
        self.assertEqual(decoded.shape, (2, 2))
        self.assertEqual(
            decoded.bits,
            (
                "3ff0000000000000",
                "8000000000000000",
                "4004000000000000",
                "0000000000000001",
            ),
        )
        self.assertEqual(decoded.canonical_bytes, expected)
        self.assertEqual(
            tuple(struct.pack(">d", value).hex() for value in decoded.values),
            decoded.bits,
        )

        expected_digest = hashlib.sha256(expected).hexdigest()
        self.assertEqual(
            expected_digest,
            "99850c6bc90027cde883e30f21c3a65657fc46cfc2d07ffdd4ce93cff9bfe3dc",
        )
        self.assertEqual(decoded.sha256, expected_digest)
        self.assertEqual(codec.canonical_f64_array_sha256(document), expected_digest)
        paired_document, paired_digest = codec.encode_f64_array_with_sha256([2, 2], values)
        self.assertEqual((paired_document, paired_digest), (expected, expected_digest))

    def test_scalar_empty_and_zero_extent_arrays(self):
        scalar = codec.encode_f64_array([], [3.0])
        self.assertEqual(scalar, b'{"bits":["4008000000000000"],"shape":[]}\n')
        self.assertEqual(codec.decode_f64_array(scalar).values, (3.0,))

        empty = codec.encode_f64_array([2, 0, 7], [])
        self.assertEqual(empty, b'{"bits":[],"shape":[2,0,7]}\n')
        self.assertEqual(codec.decode_f64_array(empty).values, ())

    def test_flat_count_native_float_and_finiteness_are_enforced(self):
        invalid_cases = (
            ([2], [1.0]),
            ([1], []),
            ([1], [1]),
            ([1], [True]),
            ([1], [math.inf]),
            ([1], [math.nan]),
        )
        for shape, values in invalid_cases:
            with self.subTest(shape=shape, values=values):
                with self.assertRaises(codec.F64CodecError):
                    codec.encode_f64_array(shape, values)
        with self.assertRaises(codec.F64CodecError):
            codec.encode_f64_array([1], (value for value in (1.0,)))

        with self.assertRaises(codec.F64CodecError):
            codec.encode_f64_bits_array([1], [])
        with self.assertRaises(codec.F64CodecError):
            codec.encode_f64_bits_array([1], ["7ff0000000000000"])

        valid = codec.F64Array(shape=[1], bits=["3ff0000000000000"])
        self.assertEqual(valid.shape, (1,))
        self.assertEqual(valid.bits, ("3ff0000000000000",))
        with self.assertRaises(codec.F64CodecError):
            codec.F64Array(shape=(2,), bits=("3ff0000000000000",))
        with self.assertRaises(codec.F64CodecError):
            codec.F64Array(shape=(1,), bits=("7ff8000000000000",))


class CanonicalDocumentRejectionTests(unittest.TestCase):
    CANONICAL = b'{"bits":["3ff0000000000000"],"shape":[1]}\n'

    def assert_rejected(self, document):
        with self.assertRaises(codec.F64CodecError):
            codec.decode_f64_array(document)

    def test_utf8_bom_newline_and_json_framing_rejection(self):
        documents = (
            self.CANONICAL[:-1],
            self.CANONICAL + b"\n",
            self.CANONICAL[:-1] + b"\r\n",
            b"\xef\xbb\xbf" + self.CANONICAL,
            self.CANONICAL + b" ",
            b"\xff" + self.CANONICAL,
            b"",
            self.CANONICAL.decode("utf-8"),
            b"[]\n",
            b"null\n",
            self.CANONICAL + b"{}\n",
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_duplicate_missing_extra_and_noncanonical_keys_rejected(self):
        documents = (
            b'{"bits":["3ff0000000000000"],"bits":["3ff0000000000000"],"shape":[1]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[1],"shape":[1]}\n',
            b'{"bits":["3ff0000000000000"]}\n',
            b'{"shape":[1]}\n',
            b'{"bits":["3ff0000000000000"],"extra":0,"shape":[1]}\n',
            b'{"\\u0062its":["3ff0000000000000"],"shape":[1]}\n',
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_whitespace_key_order_and_integer_spelling_rejected(self):
        documents = (
            b'{"shape":[1],"bits":["3ff0000000000000"]}\n',
            b'{ "bits":["3ff0000000000000"],"shape":[1]}\n',
            b'{"bits": ["3ff0000000000000"],"shape":[1]}\n',
            b'{"bits":["3ff0000000000000"], "shape":[1]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[ 1]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[1 ]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[-0]}\n',
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_json_floats_constants_types_counts_and_nonfinite_bits_rejected(self):
        documents = (
            b'{"bits":["3ff0000000000000"],"shape":[1.0]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[1e0]}\n',
            b'{"bits":[NaN],"shape":[1]}\n',
            b'{"bits":[Infinity],"shape":[1]}\n',
            b'{"bits":["3ff0000000000000"],"shape":[true]}\n',
            b'{"bits":"3ff0000000000000","shape":[1]}\n',
            b'{"bits":["3ff0000000000000"],"shape":1}\n',
            b'{"bits":[],"shape":[1]}\n',
            b'{"bits":["3ff0000000000000","4000000000000000"],"shape":[1]}\n',
            b'{"bits":["7ff0000000000000"],"shape":[1]}\n',
            b'{"bits":["7ff8000000000000"],"shape":[1]}\n',
            b'{"bits":["3FF0000000000000"],"shape":[1]}\n',
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_shape_u64_overflow_and_range_rejected_during_decode(self):
        documents = (
            b'{"bits":[],"shape":[18446744073709551616]}\n',
            b'{"bits":[],"shape":[-1]}\n',
            b'{"bits":[],"shape":[18446744073709551615,2]}\n',
            b'{"bits":[],"shape":[32769]}\n',
            b'{"bits":[],"shape":[1,1,1,1,1,1,1,1,1]}\n',
        )
        for document in documents:
            with self.subTest(document=document):
                self.assert_rejected(document)

    def test_document_resource_bound_is_checked_before_json_parsing(self):
        oversized = b"{" + b" " * codec.MAX_DOCUMENT_BYTES
        self.assertGreater(len(oversized), codec.MAX_DOCUMENT_BYTES)
        self.assert_rejected(oversized)


if __name__ == "__main__":
    unittest.main()
