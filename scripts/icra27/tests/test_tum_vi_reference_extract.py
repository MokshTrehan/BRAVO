#!/usr/bin/python3
"""Focused tests for the append-only TUM-VI reference extractor."""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_PATH = REPO_ROOT / "scripts" / "icra27" / "tum_vi_reference_extract.py"
MATRIX_PATH = REPO_ROOT / "project" / "icra27_cross_dataset_matrix.yaml"
SPEC = importlib.util.spec_from_file_location("tum_vi_reference_extract", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
EXTRACT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EXTRACT
SPEC.loader.exec_module(EXTRACT)


ROOM4_BAG = Path(
    "/home/moksh/Downloads/tum_vi/calibrated/512_16/dataset-room4_512_16.bag"
)
ROOM4_REFERENCE = (
    REPO_ROOT / "ov_data" / "tum_vi" / "dataset-room4_512_16.txt"
)
ROOM4_REFERENCE_SHA256 = (
    "2e8819fd0371c10125f33879bb2209146c126a2bff101e3c8ef20852e8597b67"
)


def _receipt_payload(run_id: str = "synthetic-r01", closed: bool = True) -> bytes:
    value = {
        "schema": EXTRACT.CLOSE_RECEIPT_SCHEMA,
        "run_id": run_id,
        "estimator_process_group_closed": closed,
        "estimator_closed_utc": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
        "process_group_survived_cleanup": False,
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _records():
    return [
        EXTRACT.PoseRecord(
            timestamp_ns=1_520_531_124_177_875_537,
            translation=(0.8082440112604986, -0.23390652025104472, 1.268850369300663),
            quaternion_xyzw=(
                0.007513811560264726,
                -0.00370857553691296,
                -0.001070930211373808,
                0.9999643204693887,
            ),
            frame_id="world",
            child_frame_id="imu",
        ),
        EXTRACT.PoseRecord(
            timestamp_ns=1_520_531_133_502_875_537,
            # This exact value distinguishes the published ten-decimal
            # intermediate rounding from direct six-decimal float rounding.
            translation=(-0.5422385000499551, -0.1323817979, 1.1987699291),
            quaternion_xyzw=(0.0927484384, 0.0203667325, 0.0219180087, 0.9952399330),
            frame_id="world",
            child_frame_id="imu",
        ),
    ]


def _write_finalized_sequence_result(run_dir: Path, bag: Path) -> Path:
    (run_dir / "diagnostics").mkdir(parents=True)
    console = run_dir / "diagnostics" / "console.log"
    console.write_text("estimator closed\n", encoding="utf-8")
    now = (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    bag_sha256 = hashlib.sha256(bag.read_bytes()).hexdigest()
    result = {
        "schema": "schurvio.icra27.cross_dataset.sequence_result.v1",
        "protocol_id": "CDSC-1R4",
        "run_id": "tumvi-corridor4-u0-r01",
        "status": "NO_INITIALIZATION",
        "run_directory": str(run_dir.resolve()),
        "inputs": {
            "bag": {
                "path": str(bag.resolve()),
                "size_bytes": bag.stat().st_size,
                "sha256": bag_sha256,
            }
        },
        "commands": {
            "estimator": {
                "finished_utc": now,
                "process_group_survived_cleanup": False,
            }
        },
        "outcome_facts": {"teardown_ok": True},
        "checks": {"teardown_complete": True},
        "estimator_close_receipt": {
            "estimator_attempted": True,
            "estimator_process_group_closed": True,
            "runtime_services_closed": True,
            "closed_utc": now,
        },
        "finished_utc": now,
        "publication": {
            "sequence_result": "sequence_result.json",
            "checksums": "SHA256SUMS",
            "append_only_run_directory": True,
        },
    }
    result_path = run_dir / "sequence_result.json"
    result_path.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    entries = {}
    for path in (console, result_path):
        entries[path.relative_to(run_dir).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    (run_dir / "SHA256SUMS").write_text(
        "".join("{}  {}\n".format(entries[name], name) for name in sorted(entries)),
        encoding="ascii",
    )
    return result_path


class RenderingAndIntervalTests(unittest.TestCase):
    def test_tum_format_matches_published_precision_path(self) -> None:
        payload = EXTRACT.render_tum(_records()).decode("ascii").splitlines()
        self.assertEqual(payload[0], "# timestamp(s) tx ty tz qx qy qz qw")
        self.assertEqual(
            payload[1],
            "1520531124.17788 0.808244 -0.233907 1.268850 "
            "0.007514 -0.003709 -0.001071 0.999964",
        )
        self.assertTrue(payload[2].startswith("1520531133.50288 -0.542238 "))

    def test_one_second_gap_rule_preserves_intervals_without_interpolation(self) -> None:
        records = [
            EXTRACT.PoseRecord(1_000_000_000, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            EXTRACT.PoseRecord(1_008_000_000, (1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            EXTRACT.PoseRecord(2_008_000_000, (2.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            EXTRACT.PoseRecord(3_008_000_001, (3.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
        ]
        summary = EXTRACT.build_interval_summary(records, 1_000_000_000)
        self.assertEqual(summary["interval_count"], 2)
        self.assertEqual(summary["intervals"][0]["sample_count"], 3)
        self.assertEqual(summary["intervals"][1]["sample_count"], 1)
        self.assertEqual(summary["discontinuities"][0]["gap_ns"], 1_000_000_001)

    def test_invalid_samples_fail_closed(self) -> None:
        duplicate = [
            EXTRACT.PoseRecord(10, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
            EXTRACT.PoseRecord(10, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)),
        ]
        with self.assertRaisesRegex(EXTRACT.ExtractionError, "strictly increasing"):
            EXTRACT.render_tum(duplicate)


class AppendOnlyExtractionTests(unittest.TestCase):
    def test_close_receipt_precedes_dataset_access_and_publication_is_no_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "synthetic.bag"
            bag.write_bytes(b"synthetic bag identity\n")
            receipt = root / "close.json"
            receipt_bytes = _receipt_payload()
            receipt.write_bytes(receipt_bytes)
            output = root / "reference-evidence"
            bag_sha256 = hashlib.sha256(bag.read_bytes()).hexdigest()

            reader = mock.Mock(return_value=_records())
            manifest = EXTRACT.extract_reference(
                bag_path=bag,
                output_dir=output,
                sequence_id="synthetic-room4",
                close_receipt_path=receipt,
                expected_bag_bytes=bag.stat().st_size,
                expected_bag_sha256=bag_sha256,
                gt_capability="full_trajectory",
                record_reader=reader,
            )
            reader.assert_called_once_with(bag.resolve(), EXTRACT.DEFAULT_TOPIC)
            self.assertEqual(manifest["status"], "COMPLETE")
            self.assertEqual(manifest["execution_stage"], "post_estimator_process_group_close_only")
            self.assertEqual((output / "estimator_close_receipt.json").read_bytes(), receipt_bytes)
            self.assertEqual(
                {path.name for path in output.iterdir()},
                {
                    "SHA256SUMS",
                    "estimator_close_receipt.json",
                    "interval_manifest.json",
                    "reference.tum",
                },
            )
            for line in (output / "SHA256SUMS").read_text(encoding="ascii").splitlines():
                digest, relative = line.split("  ", 1)
                self.assertEqual(digest, hashlib.sha256((output / relative).read_bytes()).hexdigest())

            with self.assertRaisesRegex(EXTRACT.ExtractionError, "refusing overwrite"):
                EXTRACT.extract_reference(
                    bag_path=bag,
                    output_dir=output,
                    sequence_id="synthetic-room4",
                    close_receipt_path=receipt,
                    expected_bag_bytes=bag.stat().st_size,
                    expected_bag_sha256=bag_sha256,
                    gt_capability="full_trajectory",
                    record_reader=reader,
                )

    def test_unclosed_receipt_fails_before_bag_or_output_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = root / "close.json"
            receipt.write_bytes(_receipt_payload(closed=False))
            output = root / "must-not-exist"
            reader = mock.Mock(side_effect=AssertionError("reader must not run"))
            with self.assertRaisesRegex(EXTRACT.ExtractionError, "not proven closed"):
                EXTRACT.extract_reference(
                    bag_path=root / "also-missing.bag",
                    output_dir=output,
                    sequence_id="synthetic-room4",
                    close_receipt_path=receipt,
                    expected_bag_bytes=1,
                    expected_bag_sha256="0" * 64,
                    gt_capability="full_trajectory",
                    record_reader=reader,
                )
            reader.assert_not_called()
            self.assertFalse(output.exists())

    def test_reference_compatibility_mismatch_leaves_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "synthetic.bag"
            bag.write_bytes(b"bag\n")
            receipt = root / "close.json"
            receipt.write_bytes(_receipt_payload())
            output = root / "reference-evidence"
            with self.assertRaisesRegex(EXTRACT.ExtractionError, "expected reference"):
                EXTRACT.extract_reference(
                    bag_path=bag,
                    output_dir=output,
                    sequence_id="synthetic-room4",
                    close_receipt_path=receipt,
                    expected_bag_bytes=bag.stat().st_size,
                    expected_bag_sha256=hashlib.sha256(bag.read_bytes()).hexdigest(),
                    expected_reference_sha256="0" * 64,
                    gt_capability="full_trajectory",
                    record_reader=lambda _bag, _topic: _records(),
                )
            self.assertFalse(output.exists())

    def test_finalized_sequence_result_is_rehashed_and_binds_bag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "corridor4.bag"
            bag.write_bytes(b"frozen corridor bag\n")
            result_path = _write_finalized_sequence_result(root / "run", bag)
            bag_sha256 = hashlib.sha256(bag.read_bytes()).hexdigest()
            output = root / "post-close-reference"
            manifest = EXTRACT.extract_reference(
                bag_path=bag,
                output_dir=output,
                sequence_id="dataset-corridor4_512_16",
                close_receipt_path=result_path,
                expected_bag_bytes=bag.stat().st_size,
                expected_bag_sha256=bag_sha256,
                gt_capability="embedded_partial_intervals",
                record_reader=lambda _bag, _topic: _records(),
            )
            proof = manifest["estimator_close_receipt"]
            self.assertEqual(proof["proof_kind"], "finalized_cross_dataset_sequence_result")
            self.assertEqual(proof["live_checksum_artifact_count"], 2)
            self.assertTrue(proof["source_bag_binding_validated"])

            with self.assertRaisesRegex(EXTRACT.ExtractionError, "outside.*checksummed"):
                EXTRACT.extract_reference(
                    bag_path=bag,
                    output_dir=root / "run" / "forbidden-reference",
                    sequence_id="dataset-corridor4_512_16",
                    close_receipt_path=result_path,
                    expected_bag_bytes=bag.stat().st_size,
                    expected_bag_sha256=bag_sha256,
                    gt_capability="embedded_partial_intervals",
                    record_reader=lambda _bag, _topic: _records(),
                )

    def test_mutated_finalized_run_artifact_is_rejected_before_bag_access(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "room4.bag"
            bag.write_bytes(b"frozen room bag\n")
            result_path = _write_finalized_sequence_result(root / "run", bag)
            (root / "run" / "diagnostics" / "console.log").write_text(
                "mutated after publication\n", encoding="utf-8"
            )
            reader = mock.Mock(side_effect=AssertionError("reader must not run"))
            with self.assertRaisesRegex(EXTRACT.ExtractionError, "artifact identity mismatch"):
                EXTRACT.extract_reference(
                    bag_path=bag,
                    output_dir=root / "post-close-reference",
                    sequence_id="dataset-room4_512_16",
                    close_receipt_path=result_path,
                    expected_bag_bytes=bag.stat().st_size,
                    expected_bag_sha256=hashlib.sha256(bag.read_bytes()).hexdigest(),
                    gt_capability="full_trajectory",
                    record_reader=reader,
                )
            reader.assert_not_called()


class FrozenMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = yaml.safe_load(MATRIX_PATH.read_text(encoding="utf-8"))

    def test_matrix_has_exact_25_sequence_scope_and_alternating_system_first(self) -> None:
        sequences = self.matrix["sequences"]
        self.assertEqual(self.matrix["sequence_count"], 25)
        self.assertEqual(len(sequences), 25)
        self.assertEqual([item["order"] for item in sequences], list(range(1, 26)))
        self.assertEqual(
            {dataset: sum(item["dataset"] == dataset for item in sequences) for dataset in {
                "euroc_mav", "tum_vi", "kaist_vio"
            }},
            {"euroc_mav": 11, "tum_vi": 3, "kaist_vio": 11},
        )
        for item in sequences:
            expected = ["U0", "S1"] if item["order"] % 2 else ["S1", "U0"]
            self.assertEqual(item["system_order"], expected)

    def test_matrix_freezes_protocol_order_starts_and_absolute_identities(self) -> None:
        sequences = self.matrix["sequences"]
        self.assertEqual(
            [item["sequence"] for item in sequences[14:]],
            [
                "infinite/infinite_fast.bag",
                "square/square_fast.bag",
                "square/square.bag",
                "circle/circle_head.bag",
                "rotation/rotation.bag",
                "infinite/infinite.bag",
                "square/square_head.bag",
                "circle/circle.bag",
                "rotation/rotation_fast.bag",
                "circle/circle_fast.bag",
                "infinite/infinite_head.bag",
            ],
        )
        expected_mh_starts = [40.0, 35.0, 5.0, 10.0, 5.0]
        self.assertEqual(
            [item["bag_start_seconds"] for item in sequences[:5]], expected_mh_starts
        )
        self.assertTrue(all(item["bag_start_seconds"] == 0.0 for item in sequences[5:]))
        for item in sequences:
            for identity in (item["bag"], item["ground_truth"]):
                path = Path(identity["path"] if "path" in identity else identity["canonical_path"])
                self.assertTrue(path.is_absolute())
                self.assertRegex(str(identity["sha256"]), re.compile(r"^[0-9a-f]{64}$"))
                if path.exists():
                    self.assertEqual(path.stat().st_size, identity["bytes"])

    def test_only_corridor_and_outdoors_are_partial_interval_references(self) -> None:
        partial = [
            item
            for item in self.matrix["sequences"]
            if item["ground_truth"]["capability"] == "embedded_partial_intervals"
        ]
        self.assertEqual(
            [item["sequence"] for item in partial],
            ["dataset-corridor4_512_16", "dataset-outdoors4_512_16"],
        )
        for item in partial:
            self.assertEqual(item["ground_truth"]["source_topic"], EXTRACT.DEFAULT_TOPIC)
            self.assertEqual(
                item["ground_truth"]["interval_gap_threshold_ns"],
                EXTRACT.DEFAULT_GAP_THRESHOLD_NS,
            )
            self.assertEqual(item["ground_truth"]["canonical_path"], item["bag"]["path"])


@unittest.skipUnless(
    ROOM4_BAG.is_file() and ROOM4_REFERENCE.is_file(),
    "canonical local TUM-VI room4 inputs are unavailable",
)
class CanonicalRoom4CompatibilityTests(unittest.TestCase):
    def test_room4_transform_stream_is_byte_identical_to_tracked_reference(self) -> None:
        records = list(EXTRACT.read_transform_records(ROOM4_BAG, EXTRACT.DEFAULT_TOPIC))
        payload = EXTRACT.render_tum(records)
        self.assertEqual(len(records), 13_075)
        self.assertEqual(len(payload), ROOM4_REFERENCE.stat().st_size)
        self.assertEqual(payload, ROOM4_REFERENCE.read_bytes())
        self.assertEqual(hashlib.sha256(payload).hexdigest(), ROOM4_REFERENCE_SHA256)
        intervals = EXTRACT.build_interval_summary(records, EXTRACT.DEFAULT_GAP_THRESHOLD_NS)
        self.assertEqual(intervals["interval_count"], 1)
        self.assertEqual(intervals["largest_consecutive_gap_ns"], 483_334_000)


if __name__ == "__main__":
    unittest.main()
