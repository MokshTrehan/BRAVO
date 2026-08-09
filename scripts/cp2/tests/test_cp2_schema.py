#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic unit tests for the pure-stdlib CP2 schema primitives."""

from __future__ import annotations

import hashlib
import io
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_schema as schema  # noqa: E402


def u64(value):
    return value.to_bytes(8, "big", signed=False)


def i64(value):
    return value.to_bytes(8, "big", signed=True)


def lp(value):
    return u64(len(value)) + value


class StrictJsonTests(unittest.TestCase):
    def test_valid_utf8_json_and_negative_zero_round_trip(self):
        value = schema.strict_json_loads(b'{"name":"Moksh","nested":{"x":-0.0}}')
        self.assertEqual(value["name"], "Moksh")
        self.assertEqual(math.copysign(1.0, value["nested"]["x"]), -1.0)

    def test_duplicate_keys_are_rejected_at_every_depth(self):
        for document in ('{"x":1,"x":2}', '{"outer":{"x":1,"\\u0078":2}}'):
            with self.subTest(document=document):
                with self.assertRaises(schema.SchemaError):
                    schema.strict_json_loads(document)

    def test_non_json_and_overflowed_numbers_are_rejected(self):
        for document in ("NaN", "Infinity", "-Infinity", "1e400", '{"x":-1e999}'):
            with self.subTest(document=document):
                with self.assertRaises(schema.SchemaError):
                    schema.strict_json_loads(document)

    def test_only_utf8_input_is_accepted(self):
        with self.assertRaises(schema.SchemaError):
            schema.strict_json_loads(b"\xff")
        with self.assertRaises(schema.SchemaError):
            schema.strict_json_loads('{"x":"\ud800"}')
        with self.assertRaises(schema.SchemaError):
            schema.strict_json_loads('{"x":1}'.encode("utf-16"))

    def test_stream_parser_and_compact_jsonl_are_exact(self):
        self.assertEqual(schema.strict_json_load(io.StringIO('{"x":1}')), {"x": 1})
        expected = '{"a":"é","b":1}\n'.encode("utf-8")
        self.assertEqual(schema.json_line_bytes({"b": 1, "a": "é"}), expected)
        self.assertEqual(schema.jsonl_bytes([{"b": 1}, {"a": 2}]), b'{"b":1}\n{"a":2}\n')
        self.assertEqual(schema.jsonl_bytes([]), b"")
        with self.assertRaises(schema.SchemaError):
            schema.json_line_bytes([1, 2])
        with self.assertRaises(schema.SchemaError):
            schema.json_line_bytes({"x": float("nan")})

    def test_strict_jsonl_requires_objects_lf_and_no_blank_lines(self):
        self.assertEqual(schema.strict_jsonl_loads(b'{"x":1}\n{"y":2}\n'), [{"x": 1}, {"y": 2}])
        self.assertEqual(schema.strict_jsonl_loads(b""), [])
        for document in (
            b'{"x":1}',
            b'{"x":1}\r\n',
            b'{"x":1}\n\n',
            b'[1]\n',
            b'{"x":1,"x":2}\n',
        ):
            with self.subTest(document=document):
                with self.assertRaises(schema.SchemaError):
                    schema.strict_jsonl_loads(document)

    def test_exact_object_key_inventory_rejects_missing_extra_and_bad_declaration(self):
        value = {"a": 1, "b": 2}
        self.assertIs(schema.exact_object_keys(value, ("a", "b")), value)
        for candidate in ({"a": 1}, {"a": 1, "b": 2, "c": 3}, []):
            with self.subTest(candidate=candidate):
                with self.assertRaises(schema.SchemaError):
                    schema.exact_object_keys(candidate, ("a", "b"))
        with self.assertRaises(schema.SchemaError):
            schema.exact_object_keys(value, ("a", "a"))


class ValidatorTests(unittest.TestCase):
    def test_safe_id_boundaries(self):
        self.assertEqual(schema.validate_safe_id("A"), "A")
        maximum = "a" + "-" * 127
        self.assertEqual(schema.validate_safe_id(maximum), maximum)
        for value in ("", "_bad", "a/b", "a b", "a" + "-" * 128, 1):
            with self.subTest(value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_safe_id(value)

    def test_relpath_is_normalized_posix_and_safe(self):
        self.assertEqual(schema.validate_relpath("logs/é.jsonl"), "logs/é.jsonl")
        for value in (
            "",
            ".",
            "..",
            "/a",
            "./a",
            "a/.",
            "a/../b",
            "a//b",
            "a/",
            "a/b/",
            "a\\b",
            "a\0b",
        ):
            with self.subTest(value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_relpath(value)

    def test_sha256_validator_is_lowercase_exact(self):
        digest = "01" * 32
        self.assertEqual(schema.validate_sha256(digest), digest)
        for value in ("0" * 63, "0" * 65, "AB" * 32, "gg" * 32, b"0" * 64):
            with self.subTest(value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_sha256(value)

    def test_integer_boundaries_and_boolean_rejection(self):
        for value in (0, schema.U64_MAX):
            self.assertEqual(schema.validate_u64(value), value)
        for value in (-1, schema.U64_MAX + 1, True, 1.0):
            with self.subTest(kind="u64", value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_u64(value)
        for value in (schema.I64_MIN, 0, schema.I64_MAX):
            self.assertEqual(schema.validate_i64(value), value)
        for value in (schema.I64_MIN - 1, schema.I64_MAX + 1, False, 1.0):
            with self.subTest(kind="i64", value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_i64(value)

    def test_f64_accepts_json_numbers_only_and_requires_finite(self):
        self.assertEqual(schema.validate_f64(1), 1.0)
        self.assertEqual(math.copysign(1.0, schema.validate_f64(-0.0)), -1.0)
        for value in (True, None, "1", float("nan"), float("inf"), 10**10000):
            with self.subTest(value=repr(value)):
                with self.assertRaises(schema.SchemaError):
                    schema.validate_f64(value)


class CheckedArithmeticTests(unittest.TestCase):
    def test_checked_u64_add_multiply_and_sum_boundaries(self):
        self.assertEqual(schema.checked_u64_add(schema.U64_MAX - 1, 1), schema.U64_MAX)
        self.assertEqual(schema.checked_u64_multiply(schema.U64_MAX, 0), 0)
        self.assertEqual(schema.checked_u64_multiply(3, 7), 21)
        self.assertEqual(schema.checked_u64_sum([1, 2, 3]), 6)
        with self.assertRaises(schema.SchemaError):
            schema.checked_u64_add(schema.U64_MAX, 1)
        with self.assertRaises(schema.SchemaError):
            schema.checked_u64_multiply(schema.U64_MAX, 2)
        with self.assertRaises(schema.SchemaError):
            schema.checked_u64_sum([schema.U64_MAX, 1])

    def test_agreement_gate_uses_exact_cross_product(self):
        self.assertTrue(schema.agreement_gate_999_per_1000(999, 1000))
        self.assertTrue(schema.agreement_gate_999_per_1000(schema.U64_MAX, schema.U64_MAX))
        self.assertFalse(schema.agreement_gate_999_per_1000(998, 999))
        self.assertFalse(schema.agreement_gate_999_per_1000(1, 0))
        self.assertFalse(schema.agreement_gate_999_per_1000(2, 1))

    def test_diagnostic_ratio_rejects_inexact_integer_conversion(self):
        self.assertEqual(schema.exact_count_ratio(1, 4), 0.25)
        self.assertEqual(
            schema.exact_count_ratio(
                schema.EXACT_BINARY64_INTEGER_MAX,
                schema.EXACT_BINARY64_INTEGER_MAX,
            ),
            1.0,
        )
        for numerator, denominator in (
            (1, 0),
            (schema.EXACT_BINARY64_INTEGER_MAX + 1, 1),
            (1, schema.EXACT_BINARY64_INTEGER_MAX + 1),
        ):
            with self.subTest(numerator=numerator, denominator=denominator):
                with self.assertRaises(schema.SchemaError):
                    schema.exact_count_ratio(numerator, denominator)

    def test_decimal_timestamp_conversion_is_exact_and_checked(self):
        self.assertEqual(schema.decimal_seconds_to_ns("0"), 0)
        self.assertEqual(schema.decimal_seconds_to_ns("1.2"), 1_200_000_000)
        self.assertEqual(schema.decimal_seconds_to_ns("1.000000001"), 1_000_000_001)
        maximum_seconds = schema.U64_MAX // 1_000_000_000
        maximum_fraction = schema.U64_MAX % 1_000_000_000
        maximum_text = "{}.{:09d}".format(maximum_seconds, maximum_fraction)
        self.assertEqual(schema.decimal_seconds_to_ns(maximum_text), schema.U64_MAX)
        for value in ("", "-1", "+1", ".1", "1.", "1.0000000000", "1e3", 1.0):
            with self.subTest(value=value):
                with self.assertRaises(schema.SchemaError):
                    schema.decimal_seconds_to_ns(value)
        with self.assertRaises(schema.SchemaError):
            schema.decimal_seconds_to_ns(str(maximum_seconds + 1))

    def test_integer_linear_quantile_matches_frozen_interpolation(self):
        values = [10, 20, 30, 40]
        self.assertEqual(schema.linear_quantile_integer_ns(values, 0.5), 25.0)
        self.assertEqual(schema.linear_quantile_integer_ns(values, 0.95), 38.5)
        self.assertEqual(schema.linear_quantile_integer_ns([7], 0.95), 7.0)
        for values, q in (([], 0.5), ([2, 1], 0.5), ([1], -0.1), ([1], 1.1)):
            with self.subTest(values=values, q=q):
                with self.assertRaises(schema.SchemaError):
                    schema.linear_quantile_integer_ns(values, q)
        with self.assertRaises(schema.SchemaError):
            schema.linear_quantile_integer_ns([schema.EXACT_BINARY64_INTEGER_MAX + 1], 0.5)


class DigestAndAcceptedIdTests(unittest.TestCase):
    def test_sha256_stream_and_file_match_known_digest(self):
        expected = hashlib.sha256(b"abc").hexdigest()
        self.assertEqual(schema.sha256_stream(io.BytesIO(b"abc"), chunk_size=2), expected)
        with tempfile.TemporaryDirectory(prefix="cp2-schema-", dir="/tmp") as temporary:
            path = Path(temporary) / "input.bin"
            path.write_bytes(b"abc")
            self.assertEqual(schema.sha256_file(path, chunk_size=1), expected)
        with self.assertRaises(schema.SchemaError):
            schema.sha256_stream(io.BytesIO(b"abc"), chunk_size=0)
        with self.assertRaises(schema.SchemaError):
            schema.sha256_stream(io.StringIO("abc"))

    def test_accepted_set_and_sequence_bytes_are_exact(self):
        set_expected = schema.ACCEPTED_SET_DOMAIN + u64(3) + u64(1) + u64(5) + u64(9)
        sequence_expected = schema.ACCEPTED_SEQUENCE_DOMAIN + u64(3) + u64(9) + u64(1) + u64(5)
        self.assertEqual(schema.encode_accepted_set([9, 1, 5]), set_expected)
        self.assertEqual(schema.encode_accepted_sequence([9, 1, 5]), sequence_expected)
        self.assertEqual(schema.accepted_set_sha256([9, 1, 5]), hashlib.sha256(set_expected).hexdigest())
        self.assertEqual(
            schema.accepted_sequence_sha256([9, 1, 5]),
            hashlib.sha256(sequence_expected).hexdigest(),
        )

    def test_accepted_ids_must_be_unique_u64(self):
        for encoder in (schema.encode_accepted_set, schema.encode_accepted_sequence):
            self.assertEqual(encoder([])[-8:], u64(0))
            for values in ([1, 1], [-1], [schema.U64_MAX + 1], [True], "12"):
                with self.subTest(encoder=encoder.__name__, values=values):
                    with self.assertRaises(schema.SchemaError):
                        encoder(values)

    def test_manifest_encoding_and_parser_are_exact_and_bytewise_sorted(self):
        entries = {"z/file": "11" * 32, "a": "00" * 32, "é": "22" * 32}
        expected = (
            ("00" * 32 + "  a\n").encode("ascii")
            + ("11" * 32 + "  z/file\n").encode("ascii")
            + ("22" * 32 + "  ").encode("ascii")
            + "é\n".encode("utf-8")
        )
        self.assertEqual(schema.manifest_bytes(entries), expected)
        self.assertEqual(schema.parse_manifest_bytes(expected), entries)

    def test_manifest_parser_rejects_every_noncanonical_population(self):
        digest = "ab" * 32
        invalid = (
            (digest + "  a").encode("ascii"),
            (digest + " *a\n").encode("ascii"),
            (digest + "  ../a\n").encode("ascii"),
            (digest + "  SHA256SUMS\n").encode("ascii"),
            (digest + "  b\n" + digest + "  a\n").encode("ascii"),
            (digest + "  a\n" + digest + "  a\n").encode("ascii"),
            (digest.upper() + "  a\n").encode("ascii"),
            b"00  a\n",
            (digest + "  ").encode("ascii") + b"\xff\n",
        )
        for document in invalid:
            with self.subTest(document=document):
                with self.assertRaises(schema.SchemaError):
                    schema.parse_manifest_bytes(document)


class CommandEnvironmentTests(unittest.TestCase):
    def test_environment_encoding_is_utf8_byte_sorted_and_exact(self):
        variables = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        expected = (
            schema.COMMAND_ENVIRONMENT_DOMAIN
            + u64(2)
            + lp(b"LANG")
            + lp(b"C.UTF-8")
            + lp(b"PATH")
            + lp(b"/usr/bin:/bin")
        )
        self.assertEqual(schema.encode_command_environment(variables), expected)
        self.assertEqual(
            schema.command_environment_sha256(variables), hashlib.sha256(expected).hexdigest()
        )

    def test_environment_rejects_unallowlisted_or_nonstring_values(self):
        for variables in (
            {"NOT_CP2_ALLOWLISTED": "1"},
            {"PATH": 1},
            {"PATH": "x\0y"},
            [("PATH", "x")],
        ):
            with self.subTest(variables=variables):
                with self.assertRaises(schema.SchemaError):
                    schema.encode_command_environment(variables)

    def test_pair_index_environment_names_are_explicitly_allowlisted(self):
        variables = {
            "CP2_POSTAUTH_PAIR_INDEX": "held-readiness-bound-fd-v1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        encoded = schema.encode_command_environment(variables)
        self.assertEqual(
            schema.command_environment_sha256(variables),
            hashlib.sha256(encoded).hexdigest(),
        )

    def test_readiness_git_environment_names_are_explicitly_allowlisted(self):
        variables = {
            "GIT_ALLOW_PROTOCOL": "none",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_LITERAL_PATHSPECS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "",
            "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": "/tmp/schurvio-cp2-readiness-test/git-home",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": "/tmp/schurvio-cp2-readiness-test/git-tmp",
        }
        encoded = schema.encode_command_environment(variables)
        self.assertEqual(
            schema.command_environment_sha256(variables),
            hashlib.sha256(encoded).hexdigest(),
        )


class ResolvedParameterTests(unittest.TestCase):
    def test_recursive_parameter_encoding_is_exact(self):
        parameters = {
            "/cp2_vio/z": [True, -2, -0.0, "é"],
            "/cp2_vio/a": {"β": 1, "a": False},
        }
        nested_map = (
            b"m"
            + u64(2)
            + lp(b"a")
            + b"b\x00"
            + lp("β".encode("utf-8"))
            + b"i"
            + i64(1)
        )
        nested_list = (
            b"l"
            + u64(4)
            + b"b\x01"
            + b"i"
            + i64(-2)
            + b"f"
            + bytes.fromhex("8000000000000000")
            + b"s"
            + lp("é".encode("utf-8"))
        )
        expected = (
            schema.RESOLVED_PARAMETERS_DOMAIN
            + b"m"
            + u64(2)
            + lp(b"/cp2_vio/a")
            + nested_map
            + lp(b"/cp2_vio/z")
            + nested_list
        )
        self.assertEqual(schema.encode_resolved_parameters(parameters), expected)
        self.assertEqual(
            schema.resolved_parameters_sha256(parameters), hashlib.sha256(expected).hexdigest()
        )

    def test_typed_json_shape_and_double_bits_are_exact(self):
        typed = schema.typed_resolved_parameters(
            {"/cp2_vio/z": -0.0, "/cp2_vio/a": [False, 7, "x"]}
        )
        self.assertEqual(
            typed,
            {
                "type": "map",
                "value": [
                    {
                        "name": "/cp2_vio/a",
                        "value": {
                            "type": "list",
                            "value": [
                                {"type": "bool", "value": False},
                                {"type": "int", "value": 7},
                                {"type": "string", "value": "x"},
                            ],
                        },
                    },
                    {
                        "name": "/cp2_vio/z",
                        "value": {"type": "double", "bits": "8000000000000000"},
                    },
                ],
            },
        )
        self.assertEqual(schema.strict_json_loads(schema.json_line_bytes(typed)), typed)

    def test_resolved_parameter_rejections_are_strict(self):
        invalid = (
            None,
            [],
            {"relative": 1},
            {"/cp2_vio/": 1},
            {"/cp2_vio/x": None},
            {"/cp2_vio/x": float("nan")},
            {"/cp2_vio/x": float("inf")},
            {"/cp2_vio/x": schema.I64_MAX + 1},
            {"/cp2_vio/x": (1, 2)},
            {"/cp2_vio/x": b"bytes"},
            {"/cp2_vio/x": {"bad\0key": 1}},
        )
        for parameters in invalid:
            with self.subTest(parameters=repr(parameters)):
                with self.assertRaises(schema.SchemaError):
                    schema.encode_resolved_parameters(parameters)
                with self.assertRaises(schema.SchemaError):
                    schema.typed_resolved_parameters(parameters)


if __name__ == "__main__":
    unittest.main()
