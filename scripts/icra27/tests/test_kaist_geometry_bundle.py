#!/usr/bin/env python3
"""Focused tests for the deterministic KAIST geometry evidence builder."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import struct
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "kaist_geometry_bundle.py"
SPEC = importlib.util.spec_from_file_location("kaist_geometry_bundle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "geometry_stream.json"

try:
    import genpy
    import rosbag
    from geometry_msgs.msg import Point32, PoseWithCovarianceStamped
    from sensor_msgs.msg import ChannelFloat32, PointCloud, PointCloud2, PointField
except Exception:  # pragma: no cover - exercised only on non-ROS developer hosts.
    genpy = None
    rosbag = None


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _quaternion(yaw: float) -> tuple[float, float, float, float]:
    return 0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)


def _gt_position(fixture: dict, position: list[float]) -> tuple[float, float, float]:
    yaw = fixture["gt_alignment_yaw_radians"]
    translation = fixture["gt_alignment_translation"]
    cosine = math.cos(yaw)
    sine = math.sin(yaw)
    return (
        cosine * position[0] - sine * position[1] + translation[0],
        sine * position[0] + cosine * position[1] + translation[1],
        position[2] + translation[2],
    )


def _pointcloud2(points: list[list[float]], frame: str, wall_stamp: float) -> PointCloud2:
    message = PointCloud2()
    message.header.frame_id = frame
    message.header.stamp = genpy.Time.from_sec(wall_stamp)
    message.height = 1
    message.width = len(points)
    message.fields = [
        PointField(name="x", offset=0, datatype=PointField.FLOAT32, count=1),
        PointField(name="y", offset=4, datatype=PointField.FLOAT32, count=1),
        PointField(name="z", offset=8, datatype=PointField.FLOAT32, count=1),
    ]
    message.is_bigendian = False
    message.point_step = 12
    message.row_step = 12 * len(points)
    message.data = b"".join(struct.pack("<fff", *point) for point in points)
    message.is_dense = False
    return message


def _legacy_cloud(points: list[list[float]], frame: str, wall_stamp: float) -> PointCloud:
    message = PointCloud()
    message.header.frame_id = frame
    message.header.stamp = genpy.Time.from_sec(wall_stamp)
    for index, point in enumerate(points):
        message.points.append(Point32(*point))
        message.channels.append(
            ChannelFloat32(name=f"feature_{index}", values=[0.0, 0.0, 1.0, 2.0, float(index)])
        )
    return message


def _write_inputs(
    root: Path,
    fixture: dict,
    *,
    omit_last_topic: str | None = None,
    bad_frame_topic: str | None = None,
    nonfinite_topic: str | None = None,
    trajectory_row_delta: int = 0,
    bag_start_index: int = 0,
    omit_loop_indices: frozenset[int] = frozenset(),
    loop_header_offsets_ns: tuple[int, ...] = (29_958_486, 29_958_487),
    topic_start_indices: dict[str, int] | None = None,
    omit_topic_indices: dict[str, frozenset[int]] | None = None,
    topic_record_offsets_ns: dict[str, int] | None = None,
    topic_header_offsets_ns: dict[str, int] | None = None,
) -> tuple[Path, Path, Path]:
    bag_path = root / "feature_stream.bag"
    trajectory_path = root / "estimate_raw.tum"
    gt_path = root / "ground_truth_shared.tum"
    namespace = fixture["namespace"]
    suffix_to_fixture = {
        "points_slam": "slam_points",
        "points_msckf": "msckf_points",
        "points_aruco": "aruco_points",
        "loop_feats": "loop_points",
    }
    topic_start_indices = topic_start_indices or {}
    omit_topic_indices = omit_topic_indices or {}
    topic_record_offsets_ns = topic_record_offsets_ns or {}
    topic_header_offsets_ns = topic_header_offsets_ns or {}
    with rosbag.Bag(str(bag_path), "w") as bag:
        for index, sensor_timestamp in enumerate(fixture["sensor_timestamps"]):
            if index < bag_start_index:
                continue
            record_timestamp_ns = (1000 + index) * 1_000_000_000
            record_timestamp = genpy.Time(
                record_timestamp_ns // 1_000_000_000,
                record_timestamp_ns % 1_000_000_000,
            )
            position = fixture["positions"][index]
            quaternion = _quaternion(fixture["yaw_radians"][index])
            pose = PoseWithCovarianceStamped()
            pose.header.seq = index
            pose.header.stamp = genpy.Time.from_sec(sensor_timestamp)
            pose.header.frame_id = (
                "wrong" if bad_frame_topic == "poseimu" and index == 0 else fixture["frame_id"]
            )
            pose.pose.pose.position.x, pose.pose.pose.position.y, pose.pose.pose.position.z = position
            (
                pose.pose.pose.orientation.x,
                pose.pose.pose.orientation.y,
                pose.pose.pose.orientation.z,
                pose.pose.pose.orientation.w,
            ) = quaternion
            bag.write(f"{namespace}/poseimu", pose, record_timestamp)

            for suffix, fixture_key in suffix_to_fixture.items():
                if index < topic_start_indices.get(suffix, 0):
                    continue
                if index in omit_topic_indices.get(suffix, frozenset()):
                    continue
                if omit_last_topic == suffix and index == len(fixture["sensor_timestamps"]) - 1:
                    continue
                if suffix == "loop_feats" and index in omit_loop_indices:
                    continue
                points = copy.deepcopy(fixture[fixture_key][index])
                if nonfinite_topic == suffix and index == 0:
                    if not points:
                        points = [[0.0, 0.0, 0.0]]
                    points[0][0] = float("nan")
                frame = "wrong" if bad_frame_topic == suffix and index == 0 else fixture["frame_id"]
                message_stamp = (
                    sensor_timestamp
                    if suffix == "loop_feats"
                    else 1000.0 + index - 0.0001
                )
                message = (
                    _legacy_cloud(points, frame, message_stamp)
                    if suffix == "loop_feats"
                    else _pointcloud2(points, frame, message_stamp)
                )
                message.header.seq = index
                if suffix == "loop_feats":
                    stamp_ns = (
                        int(round(sensor_timestamp * 1e9))
                        + loop_header_offsets_ns[index % len(loop_header_offsets_ns)]
                    )
                    message.header.stamp = genpy.Time(
                        stamp_ns // 1_000_000_000, stamp_ns % 1_000_000_000
                    )
                elif suffix in topic_header_offsets_ns:
                    stamp_ns = (
                        message.header.stamp.secs * 1_000_000_000
                        + message.header.stamp.nsecs
                        + topic_header_offsets_ns[suffix]
                    )
                    message.header.stamp = genpy.Time(
                        stamp_ns // 1_000_000_000, stamp_ns % 1_000_000_000
                    )
                topic_record_ns = (
                    record_timestamp_ns + topic_record_offsets_ns.get(suffix, 0)
                )
                topic_record_timestamp = genpy.Time(
                    topic_record_ns // 1_000_000_000,
                    topic_record_ns % 1_000_000_000,
                )
                bag.write(
                    f"{namespace}/{suffix}", message, topic_record_timestamp
                )

    trajectory_rows = []
    limit = len(fixture["sensor_timestamps"]) + trajectory_row_delta
    for index in range(max(0, limit)):
        timestamp = fixture["sensor_timestamps"][index]
        position = fixture["positions"][index]
        quaternion = _quaternion(fixture["yaw_radians"][index])
        trajectory_rows.append(
            " ".join(
                [
                    f"{timestamp:.5f}",
                    *(f"{value:.9f}" for value in position),
                    *(f"{value:.9f}" for value in quaternion),
                ]
            )
        )
    trajectory_path.write_text(
        "# timestamp tx ty tz qx qy qz qw\n"
        + "\n".join(trajectory_rows)
        + "\n",
        encoding="utf-8",
    )
    gt_rows = []
    for index, timestamp in enumerate(fixture["sensor_timestamps"]):
        position = _gt_position(fixture, fixture["positions"][index])
        quaternion = _quaternion(
            fixture["yaw_radians"][index]
            + fixture["gt_alignment_yaw_radians"]
        )
        gt_rows.append(
            " ".join(
                [
                    f"{timestamp:.5f}",
                    *(f"{value:.9f}" for value in position),
                    *(f"{value:.9f}" for value in quaternion),
                ]
            )
        )
    gt_path.write_text(
        "# timestamp tx ty tz qx qy qz qw\n" + "\n".join(gt_rows) + "\n",
        encoding="utf-8",
    )
    return bag_path, trajectory_path, gt_path


@unittest.skipUnless(rosbag is not None, "ROS1 Python messages and rosbag are required")
class GeometryBundleIntegrationTests(unittest.TestCase):
    def test_complete_bundle_is_deterministic_and_semantically_labeled(self) -> None:
        fixture = _load_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(root, fixture)
            run_a = root / "run_a"
            run_b = root / "run_b"
            manifest_a = MODULE.build_bundle(bag, trajectory, gt, run_a)
            manifest_b = MODULE.build_bundle(bag, trajectory, gt, run_b)

            self.assertEqual(manifest_a, manifest_b)
            self.assertEqual(manifest_a["status"], "COMPLETE")
            self.assertEqual(
                manifest_a["schema"], "schurvio.icra27.kaist_geometry_bundle.v3"
            )
            self.assertEqual(manifest_a["recording"]["message_counts"]["poseimu"], 5)
            self.assertEqual(
                manifest_a["recording"]["association"]["points_policy"],
                "per_topic_publish_ordinal_after_exact_count_or_proven_"
                "contiguous_terminal_poseimu_suffix",
            )
            point_associations = manifest_a["recording"]["association"][
                "points_topic_associations"
            ]
            self.assertEqual(
                {
                    value["mode"]
                    for value in point_associations.values()
                },
                {"V2_EXACT_COUNT_PUBLISH_ORDINAL"},
            )
            association = manifest_a["recording"]["association"]
            self.assertEqual(
                association["loop_feats_mode"], "FULL_COUNT_PUBLISH_ORDINAL"
            )
            self.assertTrue(association["loop_feats_count_parity"])
            self.assertEqual(association["loop_feats_snapshot_associated_count"], 5)
            self.assertEqual(association["loop_feats_unassociated_raw_message_count"], 0)
            header_offsets = association["loop_feats_header_offset_diagnostics"]
            self.assertEqual(header_offsets["minimum_ns"], 29_958_486)
            self.assertEqual(header_offsets["maximum_ns"], 29_958_487)
            self.assertEqual(header_offsets["range_ns"], 1)
            self.assertTrue(
                header_offsets["within_declared_near_invariant_range_tolerance"]
            )
            self.assertTrue(header_offsets["diagnostic_only_not_association_gate"])
            final_selection = manifest_a["selection"]["final_slam_message"]
            self.assertEqual(final_selection["poseimu_ordinal_zero_based"], 4)
            self.assertTrue(final_selection["final_empty"])
            self.assertEqual(
                final_selection["qualitative_flag"],
                "FINAL_ACTIVE_SLAM_EMPTY_FEATURE_COLLAPSE",
            )
            self.assertEqual(
                manifest_a["outputs"]["geometry/slam_landmarks_final.ply"]["point_count"],
                0,
            )
            self.assertEqual(
                manifest_a["outputs"]["geometry/msckf_update_points.ply"]["point_count"],
                5,
            )
            self.assertEqual(
                manifest_a["outputs"]["geometry/loop_active_tracks_aggregate.ply"]["point_count"],
                5,
            )
            self.assertEqual(
                manifest_a["selection"]["snapshots"]["selections"]["max_angular_rate"]["poseimu_ordinal_zero_based"],
                4,
            )
            alignment = manifest_a["render_alignment"]
            self.assertEqual(alignment["scale"], 1.0)
            self.assertTrue(alignment["fit_once"])
            self.assertEqual(
                alignment["timestamp_association"]["association_count"], 5
            )
            self.assertEqual(
                alignment["rendered_layers_using_this_exact_transform"],
                [
                    "capture_estimate_trajectory",
                    "final_points_slam",
                    "aggregate_points_msckf",
                ],
            )
            expected_yaw = fixture["gt_alignment_yaw_radians"]
            expected_rotation = (
                (math.cos(expected_yaw), -math.sin(expected_yaw), 0.0),
                (math.sin(expected_yaw), math.cos(expected_yaw), 0.0),
                (0.0, 0.0, 1.0),
            )
            for observed_row, expected_row in zip(
                alignment["rotation_matrix"], expected_rotation
            ):
                for observed, expected in zip(observed_row, expected_row):
                    self.assertAlmostEqual(observed, expected, places=7)
            for observed, expected in zip(
                alignment["translation_m"], fixture["gt_alignment_translation"]
            ):
                self.assertAlmostEqual(observed, expected, places=7)

            for relative in MODULE.OUTPUT_PATHS:
                self.assertTrue((run_a / relative).is_file(), relative)
                self.assertEqual(_sha256(run_a / relative), _sha256(run_b / relative), relative)
            slam_text = (run_a / "geometry/slam_landmarks_final.ply").read_text(encoding="utf-8")
            msckf_text = (run_a / "geometry/msckf_update_points.ply").read_text(encoding="utf-8")
            loop_text = (run_a / "geometry/loop_active_tracks_aggregate.ply").read_text(
                encoding="utf-8"
            )
            self.assertIn("not a persistent map", slam_text)
            self.assertIn("literal final points_slam", slam_text)
            self.assertIn("final_empty true", slam_text)
            self.assertIn("element vertex 0", slam_text)
            self.assertIn("transient last-update MSCKF", msckf_text)
            self.assertIn("raw estimator global frame", msckf_text)
            self.assertIn("transient active loop tracks", loop_text)
            self.assertIn("deliberately unassociated", loop_text)
            self.assertIn("not a persistent map", loop_text)
            top_svg = (run_a / "figures/top.svg").read_text(encoding="utf-8")
            self.assertIn("GT-derived bounds", top_svg)
            self.assertIn("gaps preserved", top_svg)
            self.assertIn("one frozen evo SE(3), scale=1", top_svg)
            self.assertIn("FINAL ACTIVE SLAM EMPTY — FEATURE COLLAPSE", top_svg)
            self.assertIn(alignment["alignment_id"], top_svg)
            self.assertTrue(manifest_a["views"]["top"]["final_slam_empty"])
            self.assertEqual(
                {
                    manifest_a["views"][view]["render_alignment_id"]
                    for view in ("top", "side", "oblique")
                },
                {alignment["alignment_id"]},
            )
            self.assertEqual(manifest_a["views"]["top"]["detected_time_gaps"]["ground_truth"], 1)
            association_lines = (
                run_a / "geometry/alignment_associations.csv"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(association_lines), 6)
            self.assertEqual(
                association_lines[1].split(",")[:3], ["0", "0", "0"]
            )
            checksum_lines = (run_a / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
            checksum_names = [line.split("  ", 1)[1] for line in checksum_lines]
            self.assertEqual(checksum_names, sorted(checksum_names))
            self.assertIn("geometry_manifest.json", checksum_names)

    def test_one_frozen_alignment_transforms_estimate_and_feature_points(self) -> None:
        fixture = _load_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, trajectory_path, gt_path = _write_inputs(root, fixture)
            estimate, _ = MODULE.read_trajectory(trajectory_path, "tum")
            ground_truth, _ = MODULE.read_trajectory(gt_path, "tum")
            alignment = MODULE.fit_evo_no_scale_alignment(
                ground_truth, estimate, max_diff_s=0.01
            )
            self.assertEqual(alignment.scale, 1.0)
            estimate_aligned = MODULE.apply_alignment_pose_position(
                estimate[2], alignment
            ).position
            expected_estimate = _gt_position(fixture, fixture["positions"][2])
            for observed, expected in zip(estimate_aligned, expected_estimate):
                self.assertAlmostEqual(observed, expected, places=7)

            feature = tuple(fixture["msckf_points"][1][1])
            feature_aligned = MODULE.apply_alignment_point(feature, alignment)
            expected_feature = _gt_position(fixture, list(feature))
            for observed, expected in zip(feature_aligned, expected_feature):
                self.assertAlmostEqual(observed, expected, places=7)

    def test_count_mismatch_fails_closed_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), omit_last_topic="points_slam"
            )
            run_dir = root / "run"
            with self.assertRaisesRegex(MODULE.GeometryError, "cannot be proved"):
                MODULE.build_bundle(bag, trajectory, gt, run_dir)
            self.assertFalse((run_dir / "geometry_manifest.json").exists())

    def test_per_topic_pose_prefix_uses_direct_and_peer_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root,
                _load_fixture(),
                topic_start_indices={
                    "points_slam": 1,
                    "points_aruco": 1,
                },
                topic_record_offsets_ns={"points_slam": 600_000_000},
            )
            run_a = root / "run_a"
            run_b = root / "run_b"
            manifest = MODULE.build_bundle(bag, trajectory, gt, run_a)
            replay = MODULE.build_bundle(bag, trajectory, gt, run_b)
            self.assertEqual(manifest, replay)
            for relative in MODULE.OUTPUT_PATHS:
                self.assertEqual(
                    _sha256(run_a / relative),
                    _sha256(run_b / relative),
                    relative,
                )
            association = manifest["recording"]["association"]
            topics = association["points_topic_associations"]
            self.assertFalse(association["points_all_streams_exact_count_parity"])
            self.assertEqual(
                topics["points_msckf"]["mode"],
                "V2_EXACT_COUNT_PUBLISH_ORDINAL",
            )
            self.assertEqual(
                topics["points_aruco"]["mode"],
                "PROVEN_CONTIGUOUS_POSE_SUFFIX_BAG_RECORD_TIME",
            )
            self.assertEqual(
                topics["points_slam"]["mode"],
                "PROVEN_CONTIGUOUS_POSE_SUFFIX_HEADER_TO_POSE_RECORD_"
                "AND_PEER_HEADER_TIME",
            )
            self.assertFalse(
                topics["points_slam"]["direct_bag_record_proof"]["accepted"]
            )
            self.assertIn(
                "points_aruco", topics["points_slam"]["accepted_peer_topics"]
            )
            for suffix, expected_prefix in (
                ("points_slam", 1),
                ("points_msckf", 0),
                ("points_aruco", 1),
            ):
                self.assertEqual(
                    topics[suffix]["unobserved_leading_poseimu_count"],
                    expected_prefix,
                )
                self.assertEqual(
                    topics[suffix]["last_associated_poseimu_ordinal_zero_based"],
                    4,
                )
            self.assertTrue(
                manifest["selection"]["snapshots"]["points_stream_coverage_gate"][
                    "every_required_snapshot_at_or_after_complete_points_coverage"
                ]
            )

    def test_internal_points_gap_cannot_masquerade_as_recorder_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root,
                _load_fixture(),
                omit_topic_indices={"points_slam": frozenset({2})},
            )
            run_dir = root / "run"
            with self.assertRaisesRegex(MODULE.GeometryError, "cannot be proved"):
                MODULE.build_bundle(bag, trajectory, gt, run_dir)
            self.assertFalse((run_dir / "geometry_manifest.json").exists())

    def test_peer_header_delta_above_declared_bound_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root,
                _load_fixture(),
                topic_start_indices={"points_slam": 1},
                topic_record_offsets_ns={"points_slam": 600_000_000},
                topic_header_offsets_ns={"points_slam": 2_000_000},
            )
            run_dir = root / "run"
            with self.assertRaisesRegex(MODULE.GeometryError, "cannot be proved"):
                MODULE.build_bundle(bag, trajectory, gt, run_dir)
            self.assertFalse((run_dir / "geometry_manifest.json").exists())

    def test_peer_cycle_without_exact_or_direct_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root,
                _load_fixture(),
                topic_start_indices={
                    "points_slam": 1,
                    "points_msckf": 1,
                    "points_aruco": 1,
                },
                topic_record_offsets_ns={
                    "points_slam": 600_000_000,
                    "points_msckf": 600_000_000,
                    "points_aruco": 600_000_000,
                },
            )
            with self.assertRaisesRegex(MODULE.GeometryError, "cannot be proved"):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_required_snapshot_inside_unobserved_prefix_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root,
                _load_fixture(),
                topic_start_indices={
                    "points_slam": 4,
                    "points_msckf": 4,
                    "points_aruco": 4,
                },
            )
            with self.assertRaisesRegex(
                MODULE.GeometryError, "snapshot precedes complete points"
            ):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_sparse_loop_features_remain_raw_only_and_all_snapshot_slots_are_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), omit_loop_indices=frozenset({1})
            )
            manifest = MODULE.build_bundle(bag, trajectory, gt, root / "run")
            association = manifest["recording"]["association"]
            self.assertEqual(
                association["loop_feats_mode"], "SPARSE_RAW_ONLY_UNASSOCIATED"
            )
            self.assertFalse(association["loop_feats_count_parity"])
            self.assertEqual(association["loop_feats_raw_message_count"], 4)
            self.assertEqual(association["loop_feats_snapshot_associated_count"], 0)
            self.assertEqual(association["loop_feats_snapshot_explicit_empty_count"], 5)
            self.assertEqual(association["loop_feats_unassociated_raw_message_count"], 4)
            self.assertIsNone(association["loop_feats_header_offset_diagnostics"])
            self.assertIsNone(association["loop_feats_bag_record_offset_diagnostics"])
            self.assertEqual(
                manifest["outputs"]["geometry/loop_active_tracks_aggregate.ply"]["point_count"],
                4,
            )
            for label in ("25", "50", "75", "max_angular_rate"):
                snapshot = manifest["outputs"][f"geometry/snapshots/{label}.ply"]
                self.assertEqual(snapshot["semantic_counts"]["loop_feats"], 0)

    def test_frame_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), bad_frame_topic="points_msckf"
            )
            with self.assertRaisesRegex(MODULE.GeometryError, "frame mismatch"):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_nonfinite_cloud_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), nonfinite_topic="points_slam"
            )
            with self.assertRaisesRegex(MODULE.GeometryError, "non-finite"):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_capture_trajectory_missing_recorded_pose_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), trajectory_row_delta=-1
            )
            with self.assertRaisesRegex(MODULE.GeometryError, "no unique row"):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_feature_recording_may_be_a_validated_trajectory_subset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(
                root, _load_fixture(), bag_start_index=1
            )
            manifest = MODULE.build_bundle(bag, trajectory, gt, root / "run")
            validation = manifest["capture_trajectory_validation"]
            self.assertFalse(validation["exact_row_count_parity"])
            self.assertEqual(validation["poseimu_row_count"], 4)
            self.assertEqual(validation["capture_trajectory_row_count"], 5)
            self.assertEqual(
                validation["first_matched_capture_trajectory_index_zero_based"], 1
            )
            self.assertEqual(
                validation["last_matched_capture_trajectory_index_zero_based"], 4
            )
            self.assertEqual(validation["unmatched_capture_rows_before_first_match"], 1)

    def test_unreadable_bag_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag = root / "feature_stream.bag"
            bag.write_bytes(b"not a rosbag")
            trajectory = root / "estimate_raw.tum"
            gt = root / "ground_truth_shared.tum"
            text = "# timestamp tx ty tz qx qy qz qw\n1 0 0 0 0 0 0 1\n2 1 0 0 0 0 0 1\n"
            trajectory.write_text(text, encoding="utf-8")
            gt.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(MODULE.GeometryError, "unreadable feature bag"):
                MODULE.build_bundle(bag, trajectory, gt, root / "run")

    def test_nested_snapshot_symlink_cannot_redirect_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag, trajectory, gt = _write_inputs(root, _load_fixture())
            run_dir = root / "run"
            external = root / "external"
            (run_dir / "geometry").mkdir(parents=True)
            external.mkdir()
            (run_dir / "geometry" / "snapshots").symlink_to(
                external, target_is_directory=True
            )
            with self.assertRaisesRegex(MODULE.GeometryError, "symlinked directory"):
                MODULE.build_bundle(bag, trajectory, gt, run_dir)
            self.assertEqual(list(external.iterdir()), [])


class PureGeometryTests(unittest.TestCase):
    def test_peer_proof_rejects_header_after_own_record_time(self) -> None:
        proof = MODULE._peer_header_suffix_proof(
            (1_000_000, 4_000_000),
            (999_999, 3_999_999),
            (1_000_000, 4_000_000),
            0,
            0,
        )
        self.assertFalse(proof["accepted"])
        self.assertFalse(proof["every_point_header_not_after_own_bag_record_time"])

    def test_direct_suffix_proof_rejects_ambiguous_nearest_record_time(self) -> None:
        proof = MODULE._direct_points_suffix_proof(
            (150,),
            (100, 200),
            1,
        )
        self.assertFalse(proof["accepted"])
        self.assertEqual(proof["nearest_tie_count"], 1)
        self.assertFalse(proof["unique_nearest_for_every_message"])

    def test_openvins_state_format_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.txt"
            path.write_text(
                "# timestamp(s) q p v\n1.00000 0 0 0 1 2 3 4 0 0 0\n",
                encoding="utf-8",
            )
            poses, resolved = MODULE.read_trajectory(path)
            self.assertEqual(resolved, "openvins-state")
            self.assertEqual(poses[0].position, (2.0, 3.0, 4.0))

    def test_nonfinite_trajectory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.tum"
            path.write_text("1 0 nan 0 0 0 0 1\n", encoding="utf-8")
            with self.assertRaisesRegex(MODULE.GeometryError, "non-finite"):
                MODULE.read_trajectory(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
