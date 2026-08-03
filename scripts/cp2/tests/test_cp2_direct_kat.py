#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protecting tests for the closed CP2-D direct-math KAT bundle."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import struct
import sys
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_direct_kat as direct_kat  # noqa: E402


def bits(value):
    return struct.pack(">d", float(value)).hex()


def canonical(value):
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def output(name, shape, values):
    return {"name": name, "shape": list(shape), "bits": list(values)}


def repeated(value, count):
    return [bits(value)] * count


def cases_fixture():
    source = [
        bits(value)
        for value in (
            3, 0, 0, -3, 0, 0, 0, 2, 0, 0, -2, 0, 0, 0, 1, 0, 0, -1,
        )
    ]
    input_values = (
        (
            source,
            [
                bits(value)
                for value in (
                    1, 1, 0.5, 1, -5, 0.5, -1, -2, 0.5,
                    3, -2, 0.5, 1, -2, 1.5, 1, -2, -0.5,
                )
            ],
        ),
        (
            source,
            [
                bits(value)
                for value in (
                    -4, 2, -0.5, 2, 2, -0.5, -1, 4, -0.5,
                    -1, 0, -0.5, -1, 2, 0.5, -1, 2, -1.5,
                )
            ],
        ),
        (
            source,
            [bits(value) for value in (0.125, -0.25, 0.5)],
            [bits(value) for value in (0.0, 0.0, 0.6, 0.8)],
        ),
        (
            [
                bits(value)
                for value in ((1 << 27), 1, 1, 1, 1, 1, 1, 1, 1)
            ],
            repeated(0.0, 9),
        ),
        (
            ["bff539d94973bf31", "3ff1c926addad2ec"],
            ["3fee666666666666"],
            [bits(value) for value in (9, 1, 7, 3, 5)],
        ),
    )
    identity = [bits(value) for value in (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0)]
    translation = [bits(value) for value in (1.0, -2.0, 0.5)]
    common_specs = direct_kat.OUTPUT_SPECS
    output_values = (
        (
            identity,
            translation,
            [bits(3.0), bits(2.0), bits(1.0)],
            [bits(1.0e-15)],
            [bits(1.0)],
            [bits(0.0)],
        ),
        (
            [bits(value) for value in (-1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, -1.0)],
            [bits(value) for value in (-1.0, 2.0, -0.5)],
            [bits(3.0), bits(2.0), bits(1.0)],
            [bits(1.0e-15)],
            [bits(1.0)],
            [bits(0.0)],
        ),
        (
            identity,
            translation,
            repeated(0.0, 6 * 3),
            repeated(0.125, 6 * 3),
            repeated(0.0, 6 * 3 * 3),
            repeated(0.0, 6 * 3 * 3),
            repeated(0.25, 3 * 3),
        ),
        ((direct_kat.FROZEN_OUTPUT_BITS[(3, "translation_rmse")],),),
        (
            (direct_kat.FROZEN_OUTPUT_BITS[(4, "boundary_p95")],),
            (direct_kat.FROZEN_OUTPUT_BITS[(4, "unsorted_companion_p95")],),
        ),
    )
    cases = []
    for case_index, (name, input_specs, inputs, specs, values) in enumerate(
        zip(
            direct_kat.CASE_ORDER,
            direct_kat.INPUT_SPECS,
            input_values,
            common_specs,
            output_values,
        )
    ):
        cases.append(
            {
                "case_index": case_index,
                "name": name,
                "inputs": [
                    output(input_name, shape, value)
                    for (input_name, shape), value in zip(input_specs, inputs)
                ],
                "outputs": [
                    output(output_name, shape, value)
                    for (output_name, shape), value in zip(specs, values)
                ],
            }
        )
    return cases


def expectation_fixture():
    return {
        "schema_version": 1,
        "record_type": direct_kat.EXPECTATION_RECORD_TYPE,
        "codec": direct_kat.CODEC,
        "repeat_count": 2,
        "case_order": list(direct_kat.CASE_ORDER),
        "cases": cases_fixture(),
    }


def response_fixture():
    cases = cases_fixture()
    return {
        "schema_version": 1,
        "record_type": direct_kat.RESPONSE_RECORD_TYPE,
        "codec": direct_kat.CODEC,
        "repeat_count": 2,
        "case_order": list(direct_kat.CASE_ORDER),
        "repeats": [
            {"repeat_index": 0, "cases": copy.deepcopy(cases)},
            {"repeat_index": 1, "cases": copy.deepcopy(cases)},
        ],
    }


class DirectKatValidTests(unittest.TestCase):
    def test_exact_five_case_two_repeat_pair_passes(self):
        expectation = canonical(expectation_fixture())
        response = canonical(response_fixture())
        result = direct_kat.validate_pair(expectation, response)
        self.assertTrue(result.passed)
        self.assertEqual(result.case_count, 5)
        self.assertEqual(result.repeat_count, 2)
        self.assertEqual(len(result.expectation_sha256), 64)
        self.assertEqual(len(result.response_sha256), 64)

    def test_decode_retains_exact_case_and_output_order(self):
        decoded = direct_kat.decode_expectation(canonical(expectation_fixture()))
        self.assertEqual(tuple(case.name for case in decoded.cases), direct_kat.CASE_ORDER)
        for case, input_specs, specs in zip(
            decoded.cases, direct_kat.INPUT_SPECS, direct_kat.OUTPUT_SPECS
        ):
            self.assertEqual(
                tuple(item.name for item in case.inputs),
                tuple(name for name, _ in input_specs),
            )
            self.assertEqual(
                tuple(item.shape for item in case.inputs),
                tuple(shape for _, shape in input_specs),
            )
            self.assertEqual(tuple(item.name for item in case.outputs), tuple(name for name, _ in specs))
            self.assertEqual(tuple(item.shape for item in case.outputs), tuple(shape for _, shape in specs))


class DirectKatSchemaTests(unittest.TestCase):
    def assert_expectation_rejected(self, value):
        with self.assertRaises(direct_kat.DirectKatError):
            direct_kat.decode_expectation(canonical(value))

    def assert_response_rejected(self, value):
        with self.assertRaises(direct_kat.DirectKatError):
            direct_kat.decode_response(canonical(value))

    def test_noncanonical_duplicate_float_constant_and_mutable_documents_reject(self):
        valid = canonical(expectation_fixture())
        variants = (
            b" " + valid,
            valid[:-1],
            valid + b"\n",
            b'{' + valid[1:-2] + b',"schema_version":1}\n',
            valid.replace(b'"schema_version":1', b'"schema_version":1.0'),
            valid.replace(b'"schema_version":1', b'"schema_version":NaN'),
            bytearray(valid),
        )
        for document in variants:
            with self.subTest(document_type=type(document).__name__):
                with self.assertRaises(direct_kat.DirectKatError):
                    direct_kat.decode_expectation(document)

    def test_top_level_identity_missing_extra_bool_and_repeat_drift_reject(self):
        mutations = []
        changed = expectation_fixture(); del changed["codec"]; mutations.append(changed)
        changed = expectation_fixture(); changed["authority"] = True; mutations.append(changed)
        changed = expectation_fixture(); changed["schema_version"] = True; mutations.append(changed)
        changed = expectation_fixture(); changed["record_type"] = direct_kat.RESPONSE_RECORD_TYPE; mutations.append(changed)
        changed = expectation_fixture(); changed["repeat_count"] = 1; mutations.append(changed)
        changed = expectation_fixture(); changed["case_order"].reverse(); mutations.append(changed)
        for changed in mutations:
            self.assert_expectation_rejected(changed)

    def test_missing_extra_permuted_duplicate_and_wrong_case_index_reject(self):
        mutations = []
        changed = expectation_fixture(); changed["cases"].pop(); mutations.append(changed)
        changed = expectation_fixture(); changed["cases"].append(copy.deepcopy(changed["cases"][-1])); mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0], changed["cases"][1] = changed["cases"][1], changed["cases"][0]; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][2]["case_index"] = 1; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][2]["name"] = "other"; mutations.append(changed)
        for changed in mutations:
            self.assert_expectation_rejected(changed)

    def test_output_name_order_shape_count_bits_and_nonfinite_reject(self):
        mutations = []
        changed = expectation_fixture(); changed["cases"][0]["inputs"].pop(); mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["inputs"][0], changed["cases"][0]["inputs"][1] = changed["cases"][0]["inputs"][1], changed["cases"][0]["inputs"][0]; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["inputs"][0]["shape"] = [18]; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"].pop(); mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"][0], changed["cases"][0]["outputs"][1] = changed["cases"][0]["outputs"][1], changed["cases"][0]["outputs"][0]; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"][0]["shape"] = [9]; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"][0]["bits"].pop(); mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"][0]["bits"][0] = "3ff000000000000"; mutations.append(changed)
        changed = expectation_fixture(); changed["cases"][0]["outputs"][0]["bits"][0] = "7ff0000000000000"; mutations.append(changed)
        for changed in mutations:
            self.assert_expectation_rejected(changed)


class DirectKatMathAndRepeatTests(unittest.TestCase):
    def test_frozen_rmse_and_both_p95_answers_cannot_drift(self):
        for case_index, output_index in ((3, 0), (4, 0), (4, 1)):
            changed = expectation_fixture()
            changed["cases"][case_index]["outputs"][output_index]["bits"][0] = bits(0.0)
            with self.subTest(case_index=case_index, output_index=output_index):
                with self.assertRaises(direct_kat.DirectKatError):
                    direct_kat.decode_expectation(canonical(changed))

    def test_common_alignment_must_reuse_case_one_bytes_exactly(self):
        for output_index in (0, 1):
            changed = expectation_fixture()
            original = changed["cases"][2]["outputs"][output_index]["bits"][0]
            integer = int(original, 16)
            changed["cases"][2]["outputs"][output_index]["bits"][0] = "{:016x}".format(integer + 1)
            with self.subTest(output_index=output_index):
                with self.assertRaises(direct_kat.DirectKatError):
                    direct_kat.decode_expectation(canonical(changed))
        for case_index in range(len(direct_kat.CASE_ORDER)):
            changed = expectation_fixture()
            target = changed["cases"][case_index]["inputs"][0]["bits"]
            target[0] = "{:016x}".format(int(target[0], 16) + 1)
            with self.subTest(input_case_index=case_index):
                with self.assertRaisesRegex(direct_kat.DirectKatError, "frozen request"):
                    direct_kat.decode_expectation(canonical(changed))

    def test_response_requires_two_complete_contiguous_repeats(self):
        mutations = []
        changed = response_fixture(); changed["repeats"].pop(); mutations.append(changed)
        changed = response_fixture(); changed["repeats"].append(copy.deepcopy(changed["repeats"][-1])); mutations.append(changed)
        changed = response_fixture(); changed["repeats"][1]["repeat_index"] = 0; mutations.append(changed)
        changed = response_fixture(); changed["repeats"][1]["cases"].pop(); mutations.append(changed)
        for changed in mutations:
            with self.assertRaises(direct_kat.DirectKatError):
                direct_kat.decode_response(canonical(changed))

    def test_each_repeat_is_compared_to_reviewed_bits(self):
        expectation = canonical(expectation_fixture())
        for repeat_index in (0, 1):
            changed = response_fixture()
            target = changed["repeats"][repeat_index]["cases"][0]["outputs"][0]["bits"]
            target[0] = "{:016x}".format(int(target[0], 16) + 1)
            with self.subTest(repeat_index=repeat_index):
                with self.assertRaisesRegex(direct_kat.DirectKatError, "differs from reviewed"):
                    direct_kat.validate_pair(expectation, canonical(changed))
        changed = response_fixture()
        request_bits = changed["repeats"][1]["cases"][4]["inputs"][1]["bits"]
        request_bits[0] = "{:016x}".format(int(request_bits[0], 16) + 1)
        with self.assertRaisesRegex(direct_kat.DirectKatError, "frozen request"):
            direct_kat.validate_pair(expectation, canonical(changed))

    def test_empty_or_noop_response_cannot_pass(self):
        changed = response_fixture()
        changed["repeats"] = []
        with self.assertRaises(direct_kat.DirectKatError):
            direct_kat.validate_pair(canonical(expectation_fixture()), canonical(changed))


if __name__ == "__main__":
    unittest.main()
