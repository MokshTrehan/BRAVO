#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later

import math
from pathlib import Path
import struct
import sys
import unittest


SCRIPT_ROOT = Path(__file__).resolve().parents[1]
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import cp2_equivalent_evaluator as evaluator
import cp2_evo_result as evo_result


def tum(rows):
    payload = bytearray(evaluator.TUM_HEADER)
    for timestamp, position in rows:
        payload.extend(
            (
                timestamp
                + " "
                + " ".join(format(value, ".17g").lower() for value in position)
                + " 0 0 0 1\n"
            ).encode("ascii")
        )
    return bytes(payload)


class EquivalentEvaluatorTests(unittest.TestCase):
    def test_exact_population_archive_and_rmse(self):
        reference = tum(
            (
                ("1.000000000", (0.0, 0.0, 0.0)),
                ("2.000000000", (1.0, 2.0, 3.0)),
                ("3.000000000", (-1.0, 4.0, 2.0)),
            )
        )
        estimate = tum(
            (
                ("1.000000000", (3.0, 4.0, 0.0)),
                ("2.000000000", (1.0, 2.0, 3.0)),
                ("3.000000000", (-1.0, 4.0, 14.0)),
            )
        )
        first = evaluator.evaluate_documents(reference, estimate)
        second = evaluator.evaluate_documents(reference, estimate)
        self.assertEqual(first, second)
        parsed = evo_result.parse_evo_result_archive(first)
        self.assertEqual(parsed.error_count, 3)
        self.assertEqual(
            parsed.error_bits,
            tuple(struct.pack(">d", value).hex() for value in (5.0, 0.0, 12.0)),
        )
        self.assertEqual(
            struct.pack(">d", parsed.statistics.rmse),
            struct.pack(">d", math.sqrt((25.0 + 144.0) / 3.0)),
        )

    def test_timestamp_or_population_substitution_is_rejected(self):
        reference = tum(
            (
                ("1.000000000", (0.0, 0.0, 0.0)),
                ("2.000000000", (0.0, 0.0, 0.0)),
            )
        )
        later = reference.replace(b"2.000000000", b"3.000000000")
        with self.assertRaisesRegex(
            evaluator.EquivalentEvaluatorError, "timestamp populations"
        ):
            evaluator.evaluate_documents(reference, later)
        with self.assertRaisesRegex(
            evaluator.EquivalentEvaluatorError, "populations differ"
        ):
            evaluator.evaluate_documents(reference, evaluator.TUM_HEADER + reference.splitlines(True)[1])

    def test_noncanonical_number_and_duplicate_time_are_rejected(self):
        base = tum(
            (
                ("1.000000000", (1.0, 0.0, 0.0)),
                ("2.000000000", (2.0, 0.0, 0.0)),
            )
        )
        with self.assertRaisesRegex(
            evaluator.EquivalentEvaluatorError, "canonical"
        ):
            evaluator.evaluate_documents(base.replace(b"1 0 0", b"1.0 0 0", 1), base)
        duplicate = base.replace(b"2.000000000", b"1.000000000")
        with self.assertRaisesRegex(
            evaluator.EquivalentEvaluatorError, "strictly increasing"
        ):
            evaluator.evaluate_documents(duplicate, duplicate)


if __name__ == "__main__":
    unittest.main()
