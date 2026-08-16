#!/usr/bin/python3.8
"""Focused tests for the generic append-only qualitative postprocessor."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "cross_dataset_geometry_bundle.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_geometry_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


NAMESPACE = "/icra27_cross_dataset"
TOPICS = [NAMESPACE + "/" + suffix for suffix in MODULE.EXPECTED_SUFFIXES]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path) -> dict:
    resolved = path.resolve(strict=True)
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256(resolved),
    }


def _artifact(run: Path, relative: str, present: bool = True) -> dict:
    path = run / relative
    return {
        "relative_path": relative,
        "identity": _identity(path) if present else None,
    }


def _write_checksums(run: Path) -> None:
    members = {}
    for path in run.rglob("*"):
        if path.is_file() and path.name != "SHA256SUMS":
            members[path.relative_to(run).as_posix()] = _sha256(path)
    (run / "SHA256SUMS").write_text(
        "".join("{}  {}\n".format(members[name], name) for name in sorted(members)),
        encoding="ascii",
    )


def _base_result(run: Path, mode: str, status: str, artifacts: dict) -> dict:
    return {
        "schema": MODULE.RESULT_SCHEMA,
        "protocol_id": "CDSC-1R3",
        "run_id": "fixture-{}-{}".format(mode, status.lower().replace("_", "-")),
        "attempt_index": 1,
        "dataset": "tum_vi",
        "sequence": "fixture-sequence",
        "system": "S1",
        "mode": mode,
        "status": status,
        "run_directory": str(run.resolve()),
        "started_utc": "2026-08-16T12:00:00Z",
        "finished_utc": "2026-08-16T12:00:03Z",
        "inputs": {},
        "artifacts": artifacts,
        "commands": {
            "estimator": {
                "finished_utc": "2026-08-16T12:00:01Z",
                "process_group_survived_cleanup": False,
            }
        },
        "checks": {"teardown_complete": True},
        "outcome_facts": {"teardown_ok": True},
        "estimator_close_receipt": {
            "estimator_attempted": True,
            "estimator_process_group_closed": True,
            "runtime_services_closed": True,
            "closed_utc": "2026-08-16T12:00:02Z",
        },
        "publication": {
            "sequence_result": "sequence_result.json",
            "checksums": "SHA256SUMS",
            "append_only_run_directory": True,
        },
    }


def _publish_result(run: Path, value: dict) -> Path:
    path = run / "sequence_result.json"
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    _write_checksums(run)
    return path


class CampaignFixture:
    def __init__(
        self,
        root: Path,
        *,
        capability: str = "full_trajectory",
        status: str = "COMPLETED",
        geometry: bool = True,
        trajectory: bool = True,
        active_geometry: bool = False,
    ) -> None:
        self.root = root
        self.dataset_bag = root / "dataset.bag"
        self.dataset_bag.write_bytes(b"frozen source bag\n")
        self.ground_truth = root / "ground_truth.tum"
        self.ground_truth.write_text(
            "# timestamp tx ty tz qx qy qz qw\n"
            "1 0 0 0 0 0 0 1\n2 1 0 0 0 0 0 1\n",
            encoding="utf-8",
        )
        self.matrix = root / "matrix.yaml"
        gt = (
            {
                "capability": "full_trajectory",
                "canonical_path": str(self.ground_truth.resolve()),
                "bytes": self.ground_truth.stat().st_size,
                "sha256": _sha256(self.ground_truth),
                "format": "tum",
            }
            if capability == "full_trajectory"
            else {
                "capability": "embedded_partial_intervals",
                "canonical_path": str(self.dataset_bag.resolve()),
                "bytes": self.dataset_bag.stat().st_size,
                "sha256": _sha256(self.dataset_bag),
                "container": "rosbag",
                "source_topic": "/vrpn_client/raw_transform",
                "source_type": "geometry_msgs/TransformStamped",
            }
        )
        matrix_value = {
            "schema": MODULE.MATRIX_SCHEMA,
            "status": "INPUT_IDENTITIES_VERIFIED",
            "sequence_count": 1,
            "sequences": [
                {
                    "order": 13,
                    "dataset": "tum_vi",
                    "sequence": "fixture-sequence",
                    "system_order": ["S1", "U0"],
                    "bag_start_seconds": 0.0,
                    "bag": {
                        "path": str(self.dataset_bag.resolve()),
                        "bytes": self.dataset_bag.stat().st_size,
                        "sha256": _sha256(self.dataset_bag),
                    },
                    "ground_truth": gt,
                }
            ],
        }
        self.matrix.write_text(yaml.safe_dump(matrix_value, sort_keys=False), encoding="utf-8")

        self.scored_run = root / "scored"
        self.capture_run = root / "capture"
        for run in (self.scored_run, self.capture_run):
            (run / "trajectory").mkdir(parents=True)
            (run / "geometry").mkdir()
        trajectory_text = (
            "# timestamp tx ty tz qx qy qz qw\n"
            "1 0 0 0 0 0 0 1\n"
            "2 1 0 0 0 0 0 1\n"
            "3 2 1 0 0 0 0 1\n"
            "4 3 1 1 0 0 0 1\n"
        )
        if trajectory:
            for run in (self.scored_run, self.capture_run):
                (run / "trajectory/estimate_raw.tum").write_text(
                    trajectory_text, encoding="utf-8"
                )
                (run / "trajectory/state_estimate.txt").write_text(
                    trajectory_text, encoding="utf-8"
                )
                (run / "trajectory/state_deviation.txt").write_text(
                    trajectory_text, encoding="utf-8"
                )
        if geometry:
            (self.capture_run / "geometry/feature_stream.bag").write_bytes(
                b"synthetic raw feature bag\n"
            )
        if active_geometry:
            (self.capture_run / "geometry/feature_stream.bag.active").write_bytes(
                b"unclosed active bag\n"
            )

        scored_artifacts = {
            name: _artifact(self.scored_run, relative, trajectory)
            for name, relative in (
                ("state", "trajectory/state_estimate.txt"),
                ("deviation", "trajectory/state_deviation.txt"),
                ("tum", "trajectory/estimate_raw.tum"),
            )
        }
        scored_artifacts["raw_geometry"] = {
            "relative_path": "geometry/feature_stream.bag",
            "identity": None,
            "active_identity": None,
            "topics": TOPICS,
        }
        scored_value = _base_result(
            self.scored_run,
            "scored",
            "COMPLETED" if trajectory else "NO_INITIALIZATION",
            scored_artifacts,
        )
        self.scored_result = _publish_result(self.scored_run, scored_value)

        capture_artifacts = {
            name: _artifact(self.capture_run, relative, trajectory)
            for name, relative in (
                ("state", "trajectory/state_estimate.txt"),
                ("deviation", "trajectory/state_deviation.txt"),
                ("tum", "trajectory/estimate_raw.tum"),
            )
        }
        capture_artifacts["raw_geometry"] = {
            "relative_path": "geometry/feature_stream.bag",
            "identity": (
                _identity(self.capture_run / "geometry/feature_stream.bag")
                if geometry
                else None
            ),
            "active_identity": (
                _identity(self.capture_run / "geometry/feature_stream.bag.active")
                if active_geometry
                else None
            ),
            "topics": TOPICS,
        }
        capture_value = _base_result(
            self.capture_run, "capture", status, capture_artifacts
        )
        bag_identity = _identity(self.dataset_bag)
        capture_value["inputs"] = {
            "matrix": _identity(self.matrix),
            "bag": bag_identity,
        }
        capture_value["input_identities_after"] = {"bag": bag_identity}
        comparisons = {
            name: {
                "expected_sha256": (
                    scored_artifacts[name]["identity"]["sha256"] if trajectory else None
                ),
                "observed_sha256": (
                    capture_artifacts[name]["identity"]["sha256"] if trajectory else None
                ),
                "expected_source_unchanged_during_capture": True,
                "byte_exact": True,
            }
            for name in ("state", "deviation", "tum")
        }
        capture_value["scored_linkage"] = {
            "status": "LINKED_EXACT",
            "byte_exact": True,
            "source_unchanged_during_capture": True,
            "sequence_result": _identity(self.scored_result),
            "sequence_result_after": _identity(self.scored_result),
            "sequence_result_path": str(self.scored_result.resolve()),
            "artifact_comparisons": comparisons,
        }
        self.capture_result = _publish_result(self.capture_run, capture_value)


def _recorded_stream() -> types.SimpleNamespace:
    poses = tuple(
        MODULE.geometry_v3.Pose(
            float(index + 1),
            (float(index), float(index % 2), float(index // 3)),
            (0.0, 0.0, 0.0, 1.0),
        )
        for index in range(4)
    )
    clouds = {
        "points_slam": (
            ((0.0, 0.0, 0.0),),
            ((0.5, 0.0, 0.0),),
            ((1.0, 0.5, 0.0),),
            tuple(),
        ),
        "points_msckf": tuple(
            ((float(index), 0.25, 0.0),) for index in range(4)
        ),
        "points_aruco": tuple(tuple() for _ in range(4)),
        "loop_feats": tuple(
            ((float(index), -0.25, 0.0),) for index in range(4)
        ),
    }
    point_associations = {
        suffix: {"unobserved_leading_poseimu_count": 0}
        for suffix in ("points_slam", "points_msckf", "points_aruco")
    }
    return types.SimpleNamespace(
        namespace=NAMESPACE,
        topics={suffix: NAMESPACE + "/" + suffix for suffix in MODULE.EXPECTED_SUFFIXES},
        message_types={suffix: "fixture/Type" for suffix in MODULE.EXPECTED_SUFFIXES},
        record_counts={suffix: 4 for suffix in MODULE.EXPECTED_SUFFIXES},
        poses=poses,
        clouds=clouds,
        raw_loop_clouds=clouds["loop_feats"],
        association={
            "points_topic_associations": point_associations,
            "loop_feats_mode": "FULL_COUNT_PUBLISH_ORDINAL",
        },
    )


class QualitativeBundleTests(unittest.TestCase):
    def test_namespace_is_derived_for_generic_and_both_kaist_systems(self) -> None:
        for namespace in (
            "/icra27_cross_dataset",
            "/icra27_kaist_u0",
            "/kaist_vio_turnsafe_baseline",
        ):
            record = {
                "topics": [
                    namespace + "/" + suffix for suffix in MODULE.EXPECTED_SUFFIXES
                ]
            }
            self.assertEqual(MODULE._derive_namespace(record), namespace)
        with self.assertRaisesRegex(MODULE.BundleError, "semantic order"):
            MODULE._derive_namespace(
                {
                    "topics": list(
                        reversed(
                            [
                                "/icra27_cross_dataset/" + suffix
                                for suffix in MODULE.EXPECTED_SUFFIXES
                            ]
                        )
                    )
                }
            )

    def test_full_reference_row_delegates_exact_gt_namespace_and_retains_v3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary), capability="full_trajectory")
            output = fixture.root / "qualitative"

            def fake_v3(**kwargs):
                run = kwargs["run_dir"]
                (run / "geometry").mkdir(parents=True)
                (run / "figures").mkdir()
                (run / "geometry/slam_landmarks_final.ply").write_text(
                    "ply\n", encoding="utf-8"
                )
                (run / "figures/top.svg").write_text("<svg/>\n", encoding="utf-8")
                manifest = {"schema": MODULE.V3_SCHEMA, "status": "COMPLETE"}
                (run / "geometry_manifest.json").write_text(
                    json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
                )
                _write_checksums(run)
                return manifest

            with mock.patch.object(
                MODULE.geometry_v3, "build_bundle", side_effect=fake_v3
            ) as delegated:
                manifest = MODULE.build_bundle(
                    fixture.capture_result, fixture.matrix, output
                )
            self.assertEqual(manifest["bundle_mode"], "REFERENCE_BACKED_VALIDATED_V3")
            kwargs = delegated.call_args.kwargs
            self.assertEqual(kwargs["ground_truth_path"], fixture.ground_truth.resolve())
            self.assertEqual(kwargs["namespace"], NAMESPACE)
            self.assertEqual(kwargs["trajectory_format"], "tum")
            self.assertEqual(kwargs["display_dataset_label"], "TUM-VI")
            self.assertTrue((output / "validated_v3/geometry/slam_landmarks_final.ply").is_file())
            self.assertTrue((output / "validated_v3/figures/top.svg").is_file())
            self.assertTrue((output / "validated_v3/SHA256SUMS").is_file())
            self.assertEqual(manifest["png_policy"], MODULE.PNG_POLICY)
            self.assertEqual(manifest["png_files_produced"], 0)
            self.assertEqual(list(output.rglob("*.png")), [])

    def test_partial_reference_row_stays_native_and_never_opens_gt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(
                Path(temporary), capability="embedded_partial_intervals"
            )
            recorded = _recorded_stream()
            trajectory = recorded.poses
            output = fixture.root / "native"
            with mock.patch.object(
                MODULE.geometry_v3, "read_feature_bag", return_value=recorded
            ) as read_bag, mock.patch.object(
                MODULE.geometry_v3,
                "read_trajectory",
                return_value=(trajectory, "tum"),
            ) as read_trajectory, mock.patch.object(
                MODULE.geometry_v3,
                "validate_capture_trajectory",
                return_value={"row_count": 4},
            ):
                manifest = MODULE.build_bundle(
                    fixture.capture_result, fixture.matrix, output
                )
            read_bag.assert_called_once_with(
                fixture.capture_run / "geometry/feature_stream.bag",
                namespace=NAMESPACE,
                expected_frame="global",
            )
            read_trajectory.assert_called_once_with(
                fixture.capture_run / "trajectory/estimate_raw.tum", "tum"
            )
            self.assertEqual(
                manifest["bundle_mode"], "NATIVE_ESTIMATOR_FRAME_PARTIAL_REFERENCE"
            )
            self.assertIsNone(manifest["inputs"]["ground_truth"])
            self.assertFalse(manifest["reference_policy"]["opened"])
            self.assertFalse(manifest["reference_policy"]["alignment_fitted"])
            for relative in MODULE.NATIVE_OUTPUTS:
                self.assertTrue((output / relative).is_file(), relative)
            svg = (output / "figures/top.svg").read_text(encoding="utf-8")
            self.assertIn("native estimator frame", svg)
            self.assertIn("illustration-only", svg)
            self.assertIn("no GT/reference/alignment", svg)
            self.assertNotIn("GT-derived bounds", svg)
            ply = (output / "geometry/slam_landmarks_final.ply").read_text(
                encoding="utf-8"
            )
            self.assertIn("literal final active points_slam", ply)
            self.assertIn("final_empty true", ply)
            self.assertIn("element vertex 0", ply)

    def test_no_initialization_without_geometry_publishes_explicit_failure_tile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(
                Path(temporary),
                capability="full_trajectory",
                status="NO_INITIALIZATION",
                geometry=False,
                trajectory=False,
            )
            output = fixture.root / "failure"
            with mock.patch.object(MODULE.geometry_v3, "build_bundle") as delegated:
                manifest = MODULE.build_bundle(
                    fixture.capture_result, fixture.matrix, output
                )
            delegated.assert_not_called()
            self.assertEqual(manifest["status"], "FAILURE_EVIDENCE_COMPLETE")
            self.assertEqual(manifest["bundle_mode"], "FAILURE_TILE_ONLY")
            self.assertEqual(manifest["qualitative_review_status"], "QUALITATIVE_UNASSESSABLE")
            self.assertIn("NO_INITIALIZATION", manifest["qualitative_flags"])
            self.assertIn("MAP_NOT_EXPOSED", manifest["qualitative_flags"])
            tile = (output / "figures/failure.svg").read_text(encoding="utf-8")
            self.assertIn("No point, pose, map, or alignment was invented", tile)
            self.assertTrue((output / "qualitative_manifest.json").is_file())
            self.assertTrue((output / "SHA256SUMS").is_file())

    def test_unclosed_active_bag_is_not_opened_and_gets_failure_tile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(
                Path(temporary), status="CAPTURE_INCOMPLETE", active_geometry=True
            )
            output = fixture.root / "active-failure"
            with mock.patch.object(MODULE.geometry_v3, "read_feature_bag") as reader:
                manifest = MODULE.build_bundle(
                    fixture.capture_result, fixture.matrix, output
                )
            reader.assert_not_called()
            self.assertIn("RAW_GEOMETRY_NOT_CLOSED", manifest["qualitative_flags"])
            self.assertEqual(manifest["bundle_mode"], "FAILURE_TILE_ONLY")

    def test_mutated_checksummed_capture_artifact_fails_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary))
            raw = fixture.capture_run / "geometry/feature_stream.bag"
            raw.write_bytes(b"tampered after capture publication\n")
            output = fixture.root / "must-not-exist"
            with mock.patch.object(MODULE.geometry_v3, "build_bundle") as delegated:
                with self.assertRaisesRegex(MODULE.BundleError, "identity mismatch"):
                    MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            delegated.assert_not_called()
            self.assertFalse(output.exists())

    def test_sequence_result_symlink_is_rejected_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary))
            link = fixture.root / "capture-result-link.json"
            link.symlink_to(fixture.capture_result)
            output = fixture.root / "must-not-exist"
            with self.assertRaisesRegex(MODULE.BundleError, "sequence result is a symlink"):
                MODULE.build_bundle(link, fixture.matrix, output)
            self.assertFalse(output.exists())

    def test_raw_geometry_symlink_checksum_member_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary))
            raw = fixture.capture_run / "geometry/feature_stream.bag"
            external = fixture.root / "external-feature-stream.bag"
            external.write_bytes(raw.read_bytes())
            raw.unlink()
            raw.symlink_to(external)
            output = fixture.root / "must-not-exist"
            with self.assertRaisesRegex(MODULE.BundleError, "artifact is a symlink"):
                MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            self.assertFalse(output.exists())

    def test_nongeometry_checksum_member_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary))
            deviation = fixture.capture_run / "trajectory/state_deviation.txt"
            external = fixture.root / "external-deviation.txt"
            external.write_bytes(deviation.read_bytes())
            deviation.unlink()
            deviation.symlink_to(external)
            output = fixture.root / "must-not-exist"
            with self.assertRaisesRegex(MODULE.BundleError, "artifact is a symlink"):
                MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            self.assertFalse(output.exists())

    def test_matrix_drift_fails_before_geometry_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary))
            fixture.matrix.write_text(
                fixture.matrix.read_text(encoding="utf-8") + "# drift\n",
                encoding="utf-8",
            )
            output = fixture.root / "must-not-exist"
            with mock.patch.object(MODULE.geometry_v3, "build_bundle") as delegated:
                with self.assertRaisesRegex(MODULE.BundleError, "matrix.*differs"):
                    MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            delegated.assert_not_called()
            self.assertFalse(output.exists())

    def test_input_mutation_during_delegate_is_caught_by_postflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary), capability="full_trajectory")
            output = fixture.root / "must-not-exist"

            def mutate_input(**kwargs):
                raw = fixture.capture_run / "geometry/feature_stream.bag"
                raw.write_bytes(b"delegate-time mutation\n")
                run = kwargs["run_dir"]
                run.mkdir(parents=True)
                manifest = {"schema": MODULE.V3_SCHEMA, "status": "COMPLETE"}
                (run / "geometry_manifest.json").write_text(
                    json.dumps(manifest) + "\n", encoding="utf-8"
                )
                (run / "SHA256SUMS").write_text(
                    "{}  geometry_manifest.json\n".format(
                        _sha256(run / "geometry_manifest.json")
                    ),
                    encoding="ascii",
                )
                return manifest

            with mock.patch.object(
                MODULE.geometry_v3, "build_bundle", side_effect=mutate_input
            ):
                with self.assertRaisesRegex(MODULE.BundleError, "changed during"):
                    MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(
                Path(temporary),
                status="NO_INITIALIZATION",
                geometry=False,
                trajectory=False,
            )
            output = fixture.root / "failure"
            MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            manifest_before = (output / "qualitative_manifest.json").read_bytes()
            with self.assertRaisesRegex(MODULE.BundleError, "already exists"):
                MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            self.assertEqual(
                (output / "qualitative_manifest.json").read_bytes(), manifest_before
            )

    def test_mid_publication_failure_rolls_back_owned_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage = root / "stage"
            stage.mkdir()
            (stage / "geometry").mkdir()
            (stage / "geometry/a.ply").write_text("a\n", encoding="utf-8")
            (stage / "geometry/b.ply").write_text("b\n", encoding="utf-8")
            (stage / "qualitative_manifest.json").write_text("{}\n", encoding="utf-8")
            (stage / "SHA256SUMS").write_text("fixture\n", encoding="ascii")
            output = root / "final"
            real_link = MODULE.os.link
            calls = 0

            def fail_second_link(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected publication failure")
                return real_link(source, destination)

            with mock.patch.object(MODULE.os, "link", side_effect=fail_second_link):
                with self.assertRaisesRegex(MODULE.BundleError, "publication failed"):
                    MODULE._publish(stage, output)
            self.assertFalse(output.exists())
            self.assertTrue((stage / "geometry/a.ply").is_file())

    def test_completed_capture_semantic_failure_is_retained_unassessable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(Path(temporary), capability="full_trajectory")
            output = fixture.root / "unassessable"
            with mock.patch.object(
                MODULE.geometry_v3,
                "build_bundle",
                side_effect=MODULE.geometry_v3.GeometryError("bad point semantics"),
            ):
                manifest = MODULE.build_bundle(
                    fixture.capture_result, fixture.matrix, output
                )
            self.assertEqual(manifest["status"], "FAILURE_EVIDENCE_COMPLETE")
            self.assertEqual(manifest["bundle_mode"], "FAILURE_TILE_ONLY")
            self.assertEqual(
                manifest["qualitative_review_status"], "QUALITATIVE_UNASSESSABLE"
            )
            self.assertIn("GEOMETRY_VALIDATION_FAILED", manifest["qualitative_flags"])
            self.assertEqual(
                manifest["processing"]["validation_error"], "bad point semantics"
            )
            self.assertTrue((output / "figures/failure.svg").is_file())
            self.assertTrue((output / "qualitative_manifest.json").is_file())
            self.assertTrue((output / "SHA256SUMS").is_file())

    def test_root_checksum_set_is_complete_and_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = CampaignFixture(
                Path(temporary),
                status="NO_INITIALIZATION",
                geometry=False,
                trajectory=False,
            )
            output = fixture.root / "failure"
            MODULE.build_bundle(fixture.capture_result, fixture.matrix, output)
            lines = (output / "SHA256SUMS").read_text(encoding="ascii").splitlines()
            recorded = {}
            for line in lines:
                digest, relative = line.split("  ", 1)
                recorded[relative] = digest
                self.assertEqual(digest, _sha256(output / relative))
            live = {
                path.relative_to(output).as_posix()
                for path in output.rglob("*")
                if path.is_file() and path.name != "SHA256SUMS"
            }
            self.assertEqual(set(recorded), live)
            self.assertIn("qualitative_manifest.json", recorded)
            self.assertIn("evidence/capture_sequence_result.json", recorded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
