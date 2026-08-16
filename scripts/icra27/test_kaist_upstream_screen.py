#!/usr/bin/python3
"""Focused tests for the diagnostic KAIST U0/S1 screen runner."""

from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest


MODULE_PATH = Path(__file__).with_name("kaist_upstream_screen.py")
SPEC = importlib.util.spec_from_file_location("kaist_upstream_screen", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
screen = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(screen)


class ProtocolTests(unittest.TestCase):
    def test_protocol_and_matrix_are_frozen_and_complete(self) -> None:
        protocol = screen.load_protocol()
        self.assertEqual(protocol["evidence_class"], "PREVIEW")
        self.assertFalse(protocol["publication_claim_eligible"])
        result = screen.validate_matrix(screen.load_matrix())
        self.assertEqual(result["rows"], 44)
        self.assertEqual(result["scored_cells"], 22)
        self.assertEqual(result["capture_cells"], 22)
        self.assertEqual(result["sequences"], 11)

    def test_seed_rule_is_materialized_exactly(self) -> None:
        rows = [
            row for row in screen.load_matrix() if row["attempt_kind"] == "SCORED"
        ]
        for row in rows:
            expected = screen.hashlib.sha256(
                screen.SEED_PREFIX + row["sequence"].encode("utf-8")
            ).hexdigest()
            self.assertEqual(row["seed_sort_sha256"], expected)

    def test_scored_and_capture_cells_are_one_to_one(self) -> None:
        rows = screen.load_matrix()
        scored = {
            (row["sequence"], row["system"], row["primary_run_id"])
            for row in rows
            if row["attempt_kind"] == "SCORED"
        }
        capture = {
            (row["sequence"], row["system"], row["primary_run_id"])
            for row in rows
            if row["attempt_kind"] == "CAPTURE"
        }
        self.assertEqual(scored, capture)

    def test_run_request_is_bound_to_dry_id_and_primary_matrix(self) -> None:
        protocol = screen.load_protocol()
        rows = screen.load_matrix()
        dry = SimpleNamespace(
            dry_run=True,
            sequence=protocol["schedule"]["dry_run_sequence"],
            system="U0",
            attempt_kind="SCORED",
            run_id="g05-dry-rotation_fast-u0-scored",
        )
        self.assertEqual(
            screen.validate_run_request(dry, protocol, rows)["kind"],
            "FROZEN_DRY_RUN",
        )
        dry.run_id = "arbitrary"
        with self.assertRaises(screen.ScreenError):
            screen.validate_run_request(dry, protocol, rows)

        row = rows[0]
        primary = SimpleNamespace(
            dry_run=False,
            sequence=row["sequence"],
            system=row["system"],
            attempt_kind=row["attempt_kind"],
            run_id=row["primary_run_id"],
        )
        self.assertEqual(
            screen.validate_run_request(primary, protocol, rows)["kind"],
            "PRIMARY_MATRIX",
        )


class OutputValidationTests(unittest.TestCase):
    def test_terminate_sweeps_descendant_after_group_leader_exits(self) -> None:
        process = subprocess.Popen(
            [
                "/usr/bin/python3",
                "-c",
                "import os,time; pid=os.fork(); time.sleep(60) if pid == 0 else None",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            process.wait(timeout=5.0)
            self.assertTrue(screen.process_group_exists(process.pid))
            screen.terminate(process, grace=0.2)
            self.assertFalse(screen.process_group_exists(process.pid))
        finally:
            if screen.process_group_exists(process.pid):
                screen.terminate(process, grace=0.2)

    def test_completion_requires_initialization_in_first_ten_percent(self) -> None:
        protocol = screen.load_protocol()
        state = {
            "first_timestamp": 10.0,
            "last_timestamp": 99.95,
            "maximum_timestamp_gap": 0.05,
        }
        accepted = screen.assess_completion(
            protocol, 0, 100_000_000_000, state
        )
        self.assertTrue(accepted["initialization_delay_pass"])
        self.assertTrue(accepted["tail_gap_pass"])
        self.assertAlmostEqual(
            accepted["state_span_fraction_of_selected_input"], 0.8995
        )

        state["first_timestamp"] = 10.000001
        rejected = screen.assess_completion(
            protocol, 0, 100_000_000_000, state
        )
        self.assertFalse(rejected["initialization_delay_pass"])

    def test_numeric_table_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.txt"
            path.write_text("# comment\n1 2 3\n2 3 4\n", encoding="utf-8")
            value = screen.validate_numeric_table(path, 3)
            self.assertEqual(value["rows"], 2)
            self.assertEqual(value["columns"], 3)
            self.assertEqual(value["first_timestamp"], 1.0)
            self.assertEqual(value["last_timestamp"], 2.0)

    def test_numeric_table_rejects_nonfinite_and_nonmonotonic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            nonfinite = Path(directory) / "nonfinite.txt"
            nonfinite.write_text("1 2 3\n2 nan 4\n", encoding="utf-8")
            with self.assertRaises(screen.ScreenError):
                screen.validate_numeric_table(nonfinite, 3)
            nonmonotonic = Path(directory) / "nonmonotonic.txt"
            nonmonotonic.write_text("2 2 3\n1 3 4\n", encoding="utf-8")
            with self.assertRaises(screen.ScreenError):
                screen.validate_numeric_table(nonmonotonic, 3)

    def test_teardown_is_separate_from_generic_child_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.log"
            path.write_text(
                "\x1b[1mprocess[icra27_kaist_u0-1]: started with pid [123]\x1b[0m\n"
                "terminate called after throwing class_loader::LibraryUnloadException\n"
                "what(): Attempt to unload library /opt/ros/noetic/lib/"
                "libcompressed_depth_image_transport.so\n"
                "process has died [pid 123, exit code -6, cmd x]\n",
                encoding="utf-8",
            )
            value = screen.classify_console(path)
            self.assertTrue(value["post_coverage_teardown_pattern"])
            self.assertEqual(value["child_exit_codes"], [-6])
            self.assertEqual(value["child_starts"][0]["name"], "icra27_kaist_u0-1")

    def test_unrelated_class_loader_abort_is_not_expected_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "console.log"
            path.write_text(
                "class_loader::LibraryUnloadException\n"
                "what(): Attempt to unload library /tmp/libunrelated.so\n"
                "process has died [pid 123, exit code -6, cmd x]\n",
                encoding="utf-8",
            )
            value = screen.classify_console(path)
            self.assertFalse(value["post_coverage_teardown_pattern"])

    def _pairing_record(self) -> dict:
        fields = screen.load_protocol()["pairing_census"]["fields"]
        census = {name: 0 for name in fields}
        census.update(
            {
                "source_camera0_count": 100,
                "source_camera1_count": 100,
                "exact_header_pair_count": 99,
                "camera0_unmatched_count": 1,
                "camera1_unmatched_count": 1,
                "u0_native_pair_count": 98,
                "s1_exact_pair_count": 99,
                "s1_queued_pair_count": None,
                "s1_processed_pair_count": None,
                "s1_frequency_thinned_pair_count": None,
                "s1_pending_pair_count": None,
                "u0_first_selected_header_stamp_ns": 1_000_000_000,
                "u0_last_selected_header_stamp_ns": 9_000_000_000,
                "s1_first_selected_header_stamp_ns": 1_000_000_000,
                "s1_last_selected_header_stamp_ns": 9_000_000_000,
            }
        )
        return {
            "artifact": {"sha256": "a" * 64},
            "command": {"exit_code": 0},
            "static_value": {
                "schema": screen.PAIRING_SCHEMA,
                "census": census,
                "selection_bounds": {},
            },
        }

    def test_s1_runtime_census_closes_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.log"
            console.write_text(
                "[SERIAL-KAIST]: exact_header_pairs=99 camera0_without_match=1 "
                "camera1_without_match=1 record_delta_ge_20ms=4 "
                "maximum_record_delta_ns=53519649\n"
                "[SERIAL-KAIST]: queued_pairs=80 processed_pairs=80 "
                "frequency_thinned_pairs=19 cam0_decode_failures=0 "
                "cam1_decode_failures=0 pending_pairs=0\n",
                encoding="utf-8",
            )
            result = screen.merge_attempt_census(
                "S1",
                self._pairing_record(),
                console,
                {"first_timestamp": 2.0, "last_timestamp": 8.0},
                screen.load_protocol()["pairing_census"]["fields"],
            )
            self.assertEqual(result["census"]["s1_processed_pair_count"], 80)
            self.assertEqual(
                result["census"]["estimator_output_last_timestamp_ns"],
                8_000_000_000,
            )

    def test_s1_runtime_census_rejects_unclosed_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            console = Path(directory) / "console.log"
            console.write_text(
                "[SERIAL-KAIST]: exact_header_pairs=99 camera0_without_match=1 "
                "camera1_without_match=1 record_delta_ge_20ms=4 "
                "maximum_record_delta_ns=53519649\n"
                "[SERIAL-KAIST]: queued_pairs=80 processed_pairs=79 "
                "frequency_thinned_pairs=19 cam0_decode_failures=0 "
                "cam1_decode_failures=0 pending_pairs=1\n",
                encoding="utf-8",
            )
            with self.assertRaises(screen.ScreenError):
                screen.merge_attempt_census(
                    "S1",
                    self._pairing_record(),
                    console,
                    {"first_timestamp": 2.0, "last_timestamp": 8.0},
                    screen.load_protocol()["pairing_census"]["fields"],
                )

    def test_resolved_parameter_contract_is_exact_for_both_systems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bag = root / "input.bag"
            bag.touch()
            for system in ("U0", "S1"):
                output = root / system
                (output / "diagnostics").mkdir(parents=True)
                (output / "trajectory").mkdir()
                expected = screen.expected_resolved_parameters(system, bag, output)
                parameters = output / "diagnostics" / "resolved.yaml"
                parameters.write_text(
                    screen.yaml.safe_dump(expected, sort_keys=True),
                    encoding="utf-8",
                )
                result = screen.validate_resolved_parameters(
                    system, parameters, bag, output
                )
                self.assertTrue(result["exact_typed_map_match"])
                mutated = dict(expected)
                mutated[next(iter(mutated))] = "forbidden-drift"
                parameters.write_text(
                    screen.yaml.safe_dump(mutated, sort_keys=True),
                    encoding="utf-8",
                )
                with self.assertRaises(screen.ScreenError):
                    screen.validate_resolved_parameters(
                        system, parameters, bag, output
                    )


if __name__ == "__main__":
    unittest.main()
