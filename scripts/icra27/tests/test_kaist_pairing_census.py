#!/usr/bin/env python3
"""Focused tests for the G0.5 KAIST pairing census."""

import importlib.util
import json
import struct
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "kaist_pairing_census.py"
SPEC = importlib.util.spec_from_file_location("kaist_pairing_census", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
census = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(census)


def message(kind, record_time_ns, header_time_ns=None):
    return census.FilteredMessage(kind, record_time_ns, header_time_ns)


class NativeSelectionTests(unittest.TestCase):
    def test_candidate_already_in_used_set_is_reused_like_upstream(self):
        base = 1_624_247_022_000_000_000
        messages = [
            message(census.KIND_CAMERA0, base + 0, 10),
            message(census.KIND_CAMERA0, base + 1_000_000, 20),
            message(census.KIND_CAMERA1, base + 2_000_000, 10),
        ]

        selected = census.select_upstream_native(messages)

        self.assertEqual([pair.identity for pair in selected.pairs], [(0, 2), (1, 2)])
        self.assertEqual(selected.candidate_use_counts, {2: 2})
        self.assertEqual(selected.used_index_outer_skip_count, 1)
        self.assertEqual(selected.no_pair_outer_skip_count, 0)

    def test_first_opposite_candidate_failure_does_not_search_again(self):
        messages = [
            message(census.KIND_CAMERA0, 0, 10),
            message(census.KIND_CAMERA1, 30_000_000, 11),
            message(census.KIND_CAMERA1, 31_000_000, 12),
        ]
        selected = census.select_upstream_native(messages)
        self.assertEqual(selected.pairs, ())
        self.assertEqual(selected.no_pair_outer_skip_count, 3)

    def test_cpp_binary64_epoch_comparison_controls_strict_boundary(self):
        # At the KAIST epoch, ros::Time::toSec binary64 quantization makes this
        # exact 20,000,001 ns integer separation appear below 0.02 seconds.
        base = 1_624_247_022_100_000_000
        messages = [
            message(census.KIND_CAMERA0, base, 10),
            message(census.KIND_CAMERA1, base + 20_000_001, 10),
        ]
        selected = census.select_upstream_native(messages)
        self.assertEqual(len(selected.pairs), 1)
        self.assertEqual(selected.pairs[0].record_skew_ns, 20_000_001)


class ExactHeaderTests(unittest.TestCase):
    def test_exact_header_join_counts_unmatched_and_uses_earlier_anchor(self):
        messages = [
            message(census.KIND_CAMERA1, 0, 100),
            message(census.KIND_IMU, 1),
            message(census.KIND_CAMERA0, 2, 100),
            message(census.KIND_CAMERA0, 3, 200),
            message(census.KIND_CAMERA1, 4, 300),
        ]
        selected = census.select_exact_header(messages)
        self.assertEqual(len(selected.pairs), 1)
        self.assertEqual(selected.pairs[0].identity, (2, 0))
        self.assertEqual(selected.pairs[0].anchor_filtered_index, 0)
        self.assertEqual(selected.pairs[0].anchor_camera_id, 1)
        self.assertEqual(selected.camera0_unmatched_headers_ns, (200,))
        self.assertEqual(selected.camera1_unmatched_headers_ns, (300,))

    def test_duplicate_header_fails_closed(self):
        messages = [
            message(census.KIND_CAMERA0, 0, 100),
            message(census.KIND_CAMERA0, 1, 100),
            message(census.KIND_CAMERA1, 2, 100),
        ]
        with self.assertRaisesRegex(census.CensusError, "duplicate header"):
            census.select_exact_header(messages)


class ReportTests(unittest.TestCase):
    def test_report_has_frozen_fields_directional_sets_and_exact_quantiles(self):
        base = 1_624_247_022_000_000_000
        messages = [
            message(census.KIND_CAMERA0, base + 0, 100),
            message(census.KIND_CAMERA1, base + 1_000_000, 100),
            message(census.KIND_CAMERA0, base + 40_000_000, 200),
            message(census.KIND_CAMERA1, base + 43_000_000, 300),
        ]
        result = census.build_census(
            messages,
            {"path": "/fixture.bag", "sha256": "0" * 64},
            {"camera0": {"name": "/c0"}},
        )
        frozen = result["census"]
        self.assertEqual(frozen["source_camera0_count"], 2)
        self.assertEqual(frozen["source_camera1_count"], 2)
        self.assertEqual(frozen["exact_header_pair_count"], 1)
        self.assertEqual(frozen["u0_native_pair_count"], 2)
        self.assertEqual(frozen["u0_native_skipped_count"], 0)
        self.assertEqual(frozen["u0_native_non_dispatch_camera_visit_count"], 2)
        self.assertEqual(frozen["u0_reused_candidate_count"], 0)
        self.assertEqual(frozen["u0_first_selected_header_stamp_ns"], 100)
        self.assertEqual(frozen["u0_last_selected_header_stamp_ns"], 200)
        self.assertEqual(frozen["s1_first_selected_header_stamp_ns"], 100)
        self.assertEqual(frozen["s1_last_selected_header_stamp_ns"], 100)
        self.assertNotIn("first_processed_header_stamp_ns", frozen)
        self.assertNotIn("last_processed_header_stamp_ns", frozen)
        self.assertEqual(
            result["u0_native_diagnostics"]["non_dispatch_camera_outer_visit_count"],
            2,
        )
        self.assertEqual(frozen["u0_record_skew_median_ns"], 2_000_000)
        self.assertEqual(frozen["u0_record_skew_p95_ns"], 3_000_000)
        self.assertEqual(frozen["pair_set_intersection_count"], 1)
        self.assertEqual(frozen["u0_only_pair_count"], 1)
        self.assertEqual(frozen["s1_only_pair_count"], 0)
        self.assertIsNone(frozen["s1_processed_pair_count"])
        self.assertEqual(result["pair_sets"]["intersection"]["identities"], [[0, 1]])
        self.assertEqual(
            result["selection_bounds"]["s1_exact_header"]["record_skew_max_ns"],
            1_000_000,
        )
        # The report is valid deterministic strict JSON, with no NaN fallback.
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_serialized_header_stamp_parser(self):
        serialized = struct.pack("<III", 7, 123, 456) + b"rest"
        self.assertEqual(
            census.parse_serialized_header_stamp_ns(serialized),
            123_000_000_456,
        )

    def test_nearest_rank_p95(self):
        self.assertEqual(census._nearest_rank(list(range(1, 21)), 95, 100), 19)
        self.assertEqual(census._median([1, 2, 10, 11]), 6)
        self.assertEqual(census._median([1, 2]), 1.5)


if __name__ == "__main__":
    unittest.main()
