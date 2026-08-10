#!/usr/bin/env python3
"""Tests for deterministic run-level Schema-2 provenance manifests."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if __package__:
    from . import capture_manifest
    from .capture_manifest import ManifestError, create_manifest_csv
    from .capture_reader import MAGIC
    from .test_capture_reader import _capture_bytes
else:
    import capture_manifest
    from capture_manifest import ManifestError, create_manifest_csv
    from capture_reader import MAGIC
    from test_capture_reader import _capture_bytes


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_with_config(
    config_sha256: str, *, source_commit: str = "2" * 40
) -> bytes:
    original = _capture_bytes(source_commit=source_commit)
    header_length = struct.unpack(">Q", original[len(MAGIC) : len(MAGIC) + 8])[0]
    header_start = len(MAGIC) + 8
    header_end = header_start + header_length
    header = original[header_start:header_end]
    updated_header = header.replace(b"a" * 64, config_sha256.encode("ascii"), 1)
    if updated_header == header:
        raise AssertionError("fixture config digest was not found")
    prefix = b"".join(
        (
            original[:header_start],
            updated_header,
            hashlib.sha256(updated_header).digest(),
            original[header_end + 32 : -48],
        )
    )
    return prefix + original[-48:-32] + hashlib.sha256(prefix).digest()


class CaptureManifestTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path]:
        config = root / "config.yaml"
        config.write_text("max_visual_passes: 1\n", encoding="utf-8")
        bag = root / "MH_01_easy.bag"
        bag.write_bytes(b"immutable bag fixture\n")
        ground_truth = root / "MH_01_easy.txt"
        ground_truth.write_text("0 0 0 0 0 0 0 1\n", encoding="utf-8")
        evaluation = root / "ate.json"
        evaluation.write_text('{"ate_rmse":0.1}\n', encoding="utf-8")
        estimator = root / "run_serial_msckf"
        estimator.write_bytes(b"estimator binary fixture\n")

        result_directory = root / "run"
        result_directory.mkdir()
        capture = result_directory / "updates.schema2"
        capture.write_bytes(_capture_with_config(_sha256(config)))
        for filename in (
            "state_estimate.txt",
            "state_deviation.txt",
            "trajectory_tum.txt",
            "timing.csv",
            "resource_usage.txt",
            "run.log",
        ):
            (result_directory / filename).write_text(filename + "\n", encoding="utf-8")
        (result_directory / "exit_status.txt").write_text("0\n", encoding="utf-8")
        (result_directory / "timed_out.txt").write_text("0\n", encoding="utf-8")
        ros_run_directory = result_directory / "ros_logs" / "fixture-ros-run"
        ros_run_directory.mkdir(parents=True)
        (result_directory / "ros_logs" / "latest").symlink_to(
            ros_run_directory, target_is_directory=True
        )

        command = {
            "bag_path": str(bag),
            "bag_sha256": _sha256(bag),
            "bag_size_bytes": bag.stat().st_size,
            "command": [
                "roslaunch",
                f"config_path:={config}",
                f"bag:={bag}",
                "capture_update_envelopes_v2:=true",
                f"update_envelope_capture_path:={capture}",
                "update_envelope_run_id:=fixture-run",
                "update_envelope_sequence_id:=MH_01_easy",
            ],
            "config_sha256": _sha256(config),
            "estimator_binary": str(estimator),
            "estimator_binary_sha256": _sha256(estimator),
            "source": {
                "branch": "schurvio-lite/anytime-information-study",
                "head": "1" * 40,
                "status": [],
            },
            "update_envelope_capture_enabled": True,
            "update_envelope_capture_path": str(capture),
            "update_envelope_run_id": "fixture-run",
            "update_envelope_sequence_id": "MH_01_easy",
        }
        command_path = result_directory / "command.json"
        command_path.write_text(json.dumps(command, sort_keys=True) + "\n", encoding="utf-8")
        output_hashes = {
            path.relative_to(result_directory).as_posix(): _sha256(path)
            for path in sorted(result_directory.rglob("*"))
            if path.is_file()
        }
        result = {
            "completed": True,
            "effective_exit_code": 0,
            "elapsed_wall_seconds": 12.5,
            "estimator_child_exit": 0,
            "output_sha256": output_hashes,
            "roslaunch_exit_code": 0,
            "timed_out": False,
            "update_envelope_capture_output_valid": True,
        }
        (result_directory / "result.json").write_text(
            json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
        )
        spec = {
            "runs": [
                {
                    "dataset_family": "euroc_mav",
                    "evaluation_paths": [str(evaluation)],
                    "ground_truth_path": str(ground_truth),
                    "result_directory": str(result_directory),
                    "timing_classification": "CONTAMINATED",
                }
            ],
            "schema_version": 1,
        }
        spec_path = root / "manifest_spec.json"
        spec_path.write_text(json.dumps(spec, sort_keys=True) + "\n", encoding="utf-8")
        return spec_path, result_directory

    def test_manifest_binds_capture_command_dataset_and_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            first = root / "first.csv"
            second = root / "second.csv"
            rows = create_manifest_csv(first, spec)
            create_manifest_csv(second, spec)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(len(rows), 1)
            parsed = next(csv.DictReader(io.StringIO(first.read_text(encoding="utf-8"))))
            self.assertEqual(parsed["sequence_id"], "MH_01_easy")
            self.assertEqual(parsed["timing_classification"], "CONTAMINATED")
            self.assertEqual(parsed["replay_head_commit"], "1" * 40)
            self.assertEqual(parsed["estimator_source_commit"], "2" * 40)
            self.assertEqual(
                json.loads(parsed["replay_source_status_json"]), []
            )
            self.assertEqual(parsed["dataset_sha256"], _sha256(root / "MH_01_easy.bag"))
            self.assertEqual(parsed["capture_update_count"], "1")
            current = json.loads(parsed["current_output_sha256_json"])
            self.assertIn("result.json", current)
            self.assertNotIn("ros_logs/latest", current)
            self.assertEqual(
                parsed["resource_output_path"],
                str(result_directory / "resource_usage.txt"),
            )
            with mock.patch.object(
                capture_manifest,
                "build_manifest_rows",
                side_effect=AssertionError("existing output must fail before validation"),
            ):
                with self.assertRaises(FileExistsError):
                    create_manifest_csv(first, spec)

    def test_large_capture_receives_one_full_file_sha256_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            capture = result_directory / "updates.schema2"
            hashed_paths = []
            original_sha256 = capture_manifest._sha256

            def counted_sha256(path: Path) -> str:
                hashed_paths.append(path)
                return original_sha256(path)

            with mock.patch.object(
                capture_manifest, "_sha256", side_effect=counted_sha256
            ):
                create_manifest_csv(root / "manifest.csv", spec)
            self.assertEqual(hashed_paths.count(capture), 1)

    def test_changed_recorded_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            (result_directory / "state_estimate.txt").write_text(
                "tampered\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ManifestError, "hash mismatch"):
                create_manifest_csv(root / "manifest.csv", spec)

    def test_capture_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            command_path = result_directory / "command.json"
            command = json.loads(command_path.read_text(encoding="utf-8"))
            command["update_envelope_sequence_id"] = "MH_05_difficult"
            command["command"] = [
                "update_envelope_sequence_id:=MH_05_difficult"
                if item.startswith("update_envelope_sequence_id:=")
                else item
                for item in command["command"]
            ]
            command_path.write_text(json.dumps(command, sort_keys=True) + "\n", encoding="utf-8")
            result_path = result_directory / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["output_sha256"]["command.json"] = _sha256(command_path)
            result_path.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "sequence ID"):
                create_manifest_csv(root / "manifest.csv", spec)

    def test_launch_identity_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            command_path = result_directory / "command.json"
            command = json.loads(command_path.read_text(encoding="utf-8"))
            command["command"] = [
                "bag:=/different/input.bag" if item.startswith("bag:=") else item
                for item in command["command"]
            ]
            command_path.write_text(
                json.dumps(command, sort_keys=True) + "\n", encoding="utf-8"
            )
            result_path = result_directory / "result.json"
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["output_sha256"]["command.json"] = _sha256(command_path)
            result_path.write_text(
                json.dumps(result, sort_keys=True) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ManifestError, "command bag:= value"):
                create_manifest_csv(root / "manifest.csv", spec)

    def test_unknown_spec_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, _ = self._fixture(root)
            value = json.loads(spec.read_text(encoding="utf-8"))
            value["runs"][0]["typo"] = True
            spec.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ManifestError, "keys mismatch"):
                create_manifest_csv(root / "manifest.csv", spec)

    def test_unexpected_result_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            (result_directory / "unexpected-link").symlink_to(
                result_directory / "run.log"
            )
            with self.assertRaisesRegex(ManifestError, "contains a symlink"):
                create_manifest_csv(root / "manifest.csv", spec)

    def test_ros_latest_symlink_must_resolve_within_result_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            spec, result_directory = self._fixture(root)
            latest = result_directory / "ros_logs" / "latest"
            latest.unlink()
            latest.symlink_to(root, target_is_directory=True)
            with self.assertRaisesRegex(ManifestError, "contains a symlink"):
                create_manifest_csv(root / "manifest.csv", spec)


if __name__ == "__main__":
    unittest.main()
