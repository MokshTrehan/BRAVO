#!/usr/bin/python3
"""Tests for the deterministic KAIST VIO ROS1 bag adapter."""

from pathlib import Path
import io
import json
import sys
import tempfile
import unittest
from unittest import mock

import cv2
from geometry_msgs.msg import PoseStamped
import numpy as np
import rosbag
import rospy
from sensor_msgs.msg import CompressedImage, Image, Imu


sys.path.insert(0, str(Path(__file__).resolve().parent))
import kaist_vio_adapter as adapter  # noqa: E402


def ros_time(nanoseconds: int) -> rospy.Time:
    return rospy.Time(
        nanoseconds // adapter.NANOSECONDS_PER_SECOND,
        nanoseconds % adapter.NANOSECONDS_PER_SECOND,
    )


def serialize(message: object) -> bytes:
    stream = io.BytesIO()
    message.serialize(stream)
    return stream.getvalue()


class KaistVioAdapterTest(unittest.TestCase):
    @staticmethod
    def image(index: int, width: int = adapter.EXPECTED_WIDTH) -> np.ndarray:
        rows = np.arange(adapter.EXPECTED_HEIGHT, dtype=np.uint16)[:, None]
        columns = np.arange(width, dtype=np.uint16)[None, :]
        return ((rows * 7 + columns * 11 + index * 29) % 256).astype(np.uint8)

    @staticmethod
    def compressed(
        image: np.ndarray,
        codec: str,
        sequence: int,
        stamp_ns: int,
        frame_id: str,
    ) -> CompressedImage:
        extension = ".jpg" if codec == "jpeg" else ".png"
        parameters = [cv2.IMWRITE_JPEG_QUALITY, 93] if codec == "jpeg" else []
        success, encoded = cv2.imencode(extension, image, parameters)
        if not success:
            raise RuntimeError("test image encoding failed")
        message = CompressedImage()
        message.header.seq = sequence
        message.header.stamp = ros_time(stamp_ns)
        message.header.frame_id = frame_id
        message.format = "mono8; {} compressed mono8".format(codec)
        message.data = encoded.tobytes()
        return message

    @staticmethod
    def raw(
        image: np.ndarray,
        sequence: int,
        stamp_ns: int,
        frame_id: str,
    ) -> Image:
        message = Image()
        message.header.seq = sequence
        message.header.stamp = ros_time(stamp_ns)
        message.header.frame_id = frame_id
        message.height = image.shape[0]
        message.width = image.shape[1]
        message.encoding = adapter.OUTPUT_ENCODING
        message.is_bigendian = 0
        message.step = image.shape[1]
        message.data = np.ascontiguousarray(image).tobytes()
        return message

    @staticmethod
    def imu(sequence: int, stamp_ns: int) -> Imu:
        message = Imu()
        message.header.seq = sequence
        message.header.stamp = ros_time(stamp_ns)
        message.header.frame_id = "imu_link"
        message.orientation.x = 0.01 * sequence
        message.orientation.y = -0.02 * sequence
        message.orientation.z = 0.03 * sequence
        message.orientation.w = 1.0
        message.angular_velocity.x = 1.0 + sequence
        message.angular_velocity.y = 2.0 + sequence
        message.angular_velocity.z = 3.0 + sequence
        message.linear_acceleration.x = -1.0 - sequence
        message.linear_acceleration.y = -2.0 - sequence
        message.linear_acceleration.z = 9.8
        message.orientation_covariance[0] = 0.001 + sequence
        message.angular_velocity_covariance[4] = 0.002 + sequence
        message.linear_acceleration_covariance[8] = 0.003 + sequence
        return message

    @staticmethod
    def pose(sequence: int, stamp_ns: int) -> PoseStamped:
        message = PoseStamped()
        message.header.seq = sequence
        message.header.stamp = ros_time(stamp_ns)
        message.header.frame_id = "world"
        message.pose.position.x = 0.1 * sequence
        message.pose.position.y = -0.2 * sequence
        message.pose.position.z = 1.0 + sequence
        message.pose.orientation.x = 0.01 * sequence
        message.pose.orientation.y = 0.02 * sequence
        message.pose.orientation.z = 0.03 * sequence
        message.pose.orientation.w = 1.0
        return message

    def write_source(
        self,
        path: Path,
        *,
        width: int = adapter.EXPECTED_WIDTH,
        malformed: bool = False,
        wrong_type: bool = False,
        reversed_header: bool = False,
        unmatched_stereo_stamp: bool = False,
        camera1_record_offset_ns: int = 4_000_000,
        nonfinite_imu: bool = False,
        optional_nonfinite_imu: bool = False,
        zero_pose_quaternion: bool = False,
        zero_header_stamp: bool = False,
        profile: str = adapter.COMPRESSED_SOURCE_PROFILE,
        include_other_camera0: bool = False,
    ) -> None:
        base = 1_600_000_000 * adapter.NANOSECONDS_PER_SECOND
        records = []
        for index in range(2):
            interval = index * 40_000_000
            camera0_stamp = base + interval + 3_000_000
            if reversed_header and index == 1:
                camera0_stamp = base + 2_000_000
            if zero_header_stamp and index == 0:
                camera0_stamp = 0
            camera1_header_offset = (
                adapter.STEREO_RECORD_SKEW_THRESHOLD_NS
                if unmatched_stereo_stamp and index == 1
                else 0
            )
            camera1_stamp = base + interval + 3_000_000 + camera1_header_offset
            camera0_pixels = self.image(index, width)
            camera1_pixels = np.flipud(self.image(index + 10))
            if profile == adapter.COMPRESSED_SOURCE_PROFILE:
                camera0_topic = adapter.COMPRESSED_SOURCE_CAMERA0_TOPIC
                camera1_topic = adapter.COMPRESSED_SOURCE_CAMERA1_TOPIC
                camera0 = self.compressed(
                    camera0_pixels,
                    "png" if index == 0 else "jpeg",
                    10 + index,
                    camera0_stamp,
                    "infra1_frame",
                )
                camera1 = self.compressed(
                    camera1_pixels,
                    "jpeg" if index == 0 else "png",
                    20 + index,
                    camera1_stamp,
                    "infra2_frame",
                )
            elif profile == adapter.RAW_SOURCE_PROFILE:
                camera0_topic = adapter.RAW_SOURCE_CAMERA0_TOPIC
                camera1_topic = adapter.RAW_SOURCE_CAMERA1_TOPIC
                camera0 = self.raw(
                    camera0_pixels,
                    10 + index,
                    camera0_stamp,
                    "infra1_frame",
                )
                camera1 = self.raw(
                    camera1_pixels,
                    20 + index,
                    camera1_stamp,
                    "infra2_frame",
                )
            else:
                raise ValueError("unsupported test source profile")
            if malformed and index == 1:
                camera0.format = "mono8; png compressed mono8"
                camera0.data = b"\x89PNG\r\n\x1a\nmalformed"
            if wrong_type:
                raw_image = self.image(index)
                camera0 = Image()
                camera0.header.seq = 10 + index
                camera0.header.stamp = ros_time(camera0_stamp)
                camera0.header.frame_id = "infra1_frame"
                camera0.height = adapter.EXPECTED_HEIGHT
                camera0.width = adapter.EXPECTED_WIDTH
                camera0.encoding = adapter.OUTPUT_ENCODING
                camera0.is_bigendian = 0
                camera0.step = adapter.EXPECTED_WIDTH
                camera0.data = raw_image.tobytes()
            pose = self.pose(index, base + interval + 5_000_000)
            if zero_pose_quaternion and index == 1:
                pose.pose.orientation.x = 0.0
                pose.pose.orientation.y = 0.0
                pose.pose.orientation.z = 0.0
                pose.pose.orientation.w = 0.0
            records.extend(
                [
                    (
                        base + interval + 3_000_000,
                        camera0_topic,
                        camera0,
                    ),
                    (
                        base + interval + camera1_record_offset_ns,
                        camera1_topic,
                        camera1,
                    ),
                    (
                        base + interval + 5_000_000,
                        adapter.GROUND_TRUTH_TOPIC,
                        pose,
                    ),
                ]
            )
            if include_other_camera0:
                if profile == adapter.COMPRESSED_SOURCE_PROFILE:
                    other_topic = adapter.RAW_SOURCE_CAMERA0_TOPIC
                    other_message = self.raw(
                        camera0_pixels,
                        30 + index,
                        camera0_stamp,
                        "infra1_frame",
                    )
                else:
                    other_topic = adapter.COMPRESSED_SOURCE_CAMERA0_TOPIC
                    other_message = self.compressed(
                        camera0_pixels,
                        "png",
                        30 + index,
                        camera0_stamp,
                        "infra1_frame",
                    )
                records.append(
                    (base + interval + 3_500_000, other_topic, other_message)
                )
        for index in range(6):
            stamp = base + index * 10_000_000
            imu = self.imu(index, stamp)
            if nonfinite_imu and index == 3:
                imu.angular_velocity.y = float("nan")
            if optional_nonfinite_imu and index == 4:
                imu.orientation_covariance[2] = float("inf")
            records.append((stamp, adapter.IMU_TOPIC, imu))
        records.sort(key=lambda item: item[0])
        with rosbag.Bag(str(path), "w") as bag:
            for record_ns, topic, message in records:
                bag.write(topic, message, ros_time(record_ns))

    @staticmethod
    def messages(path: Path, topic: str):
        with rosbag.Bag(str(path), "r") as bag:
            return [
                (record_time.to_nsec(), message)
                for _, message, record_time in bag.read_messages(topics=[topic])
            ]

    def test_adapt_preserves_messages_pixels_stereo_and_json_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bag"
            output = root / "adapted.bag"
            adaptation_json = root / "adaptation.json"
            json_output = root / "audit.json"
            self.write_source(source)

            self.assertEqual(
                adapter.main(
                    [
                        "adapt",
                        str(source),
                        str(output),
                        "--json-output",
                        str(adaptation_json),
                    ]
                ),
                0,
            )
            report = json.loads(adaptation_json.read_text(encoding="utf-8"))
            self.assertTrue(output.is_file())
            self.assertEqual(report["schema"], adapter.ADAPTATION_SCHEMA)
            self.assertEqual(
                report["source_profile"], adapter.COMPRESSED_SOURCE_PROFILE
            )
            self.assertTrue(report["preservation"]["all_passed"])
            self.assertTrue(all(report["preservation"]["checks"].values()))

            source_audit = adapter.audit_bag(source, "source")
            output_audit = adapter.audit_bag(output, "adapted")
            self.assertEqual(report["source_audit"], source_audit)
            self.assertEqual(report["adapted_audit"], output_audit)
            self.assertEqual(
                source_audit["source_profile"], adapter.COMPRESSED_SOURCE_PROFILE
            )
            self.assertIsNone(output_audit["source_profile"])
            self.assertEqual(output_audit["stereo"]["exact_header_pair_count"], 2)
            self.assertEqual(
                output_audit["stereo"][
                    "matched_pair_record_time_absolute_skew"
                ]["max_ns"],
                1_000_000,
            )
            self.assertEqual(output_audit["stereo"]["camera0_unmatched_count"], 0)
            self.assertEqual(output_audit["stereo"]["camera1_unmatched_count"], 0)
            self.assertEqual(
                output_audit["stereo"][
                    "matched_pair_record_skew_at_or_above_threshold_count"
                ],
                0,
            )
            self.assertEqual(source_audit["total_required_messages"], 12)
            self.assertEqual(output_audit["total_required_messages"], 10)
            self.assertEqual(output_audit["ignored_topics"], [])
            self.assertEqual(
                source_audit["streams"][adapter.COMPRESSED_SOURCE_CAMERA0_TOPIC][
                    "image"
                ]["codecs"],
                ["jpeg", "png"],
            )

            self.assertEqual(
                adapter.main(
                    [
                        "audit",
                        str(output),
                        "--kind",
                        "adapted",
                        "--json-output",
                        str(json_output),
                    ]
                ),
                0,
            )
            self.assertEqual(
                json.loads(json_output.read_text(encoding="utf-8")), output_audit
            )

            with rosbag.Bag(str(output), "r") as bag:
                topic_info = bag.get_type_and_topic_info().topics
                self.assertEqual(
                    set(topic_info),
                    {
                        adapter.OUTPUT_CAMERA0_TOPIC,
                        adapter.OUTPUT_CAMERA1_TOPIC,
                        adapter.IMU_TOPIC,
                    },
                )
                self.assertNotIn(adapter.GROUND_TRUTH_TOPIC, topic_info)
                self.assertEqual(
                    topic_info[adapter.OUTPUT_CAMERA0_TOPIC].msg_type,
                    "sensor_msgs/Image",
                )
                self.assertEqual(
                    topic_info[adapter.OUTPUT_CAMERA1_TOPIC].msg_type,
                    "sensor_msgs/Image",
                )

            source_messages = self.messages(source, adapter.IMU_TOPIC)
            output_messages = self.messages(output, adapter.IMU_TOPIC)
            self.assertEqual(len(source_messages), len(output_messages))
            for (source_time, source_message), (
                output_time,
                output_message,
            ) in zip(source_messages, output_messages):
                self.assertEqual(source_time, output_time)
                self.assertEqual(serialize(source_message), serialize(output_message))
            self.assertEqual(self.messages(output, adapter.GROUND_TRUTH_TOPIC), [])

            for source_topic, output_topic in (
                (
                    adapter.COMPRESSED_SOURCE_CAMERA0_TOPIC,
                    adapter.OUTPUT_CAMERA0_TOPIC,
                ),
                (
                    adapter.COMPRESSED_SOURCE_CAMERA1_TOPIC,
                    adapter.OUTPUT_CAMERA1_TOPIC,
                ),
            ):
                source_messages = self.messages(source, source_topic)
                output_messages = self.messages(output, output_topic)
                self.assertEqual(len(source_messages), len(output_messages))
                for (source_time, compressed), (output_time, raw) in zip(
                    source_messages, output_messages
                ):
                    self.assertEqual(source_time, output_time)
                    self.assertEqual(raw._type, Image._type)
                    self.assertEqual(raw.header.seq, compressed.header.seq)
                    self.assertEqual(raw.header.stamp, compressed.header.stamp)
                    self.assertEqual(raw.header.frame_id, compressed.header.frame_id)
                    self.assertEqual(raw.width, adapter.EXPECTED_WIDTH)
                    self.assertEqual(raw.height, adapter.EXPECTED_HEIGHT)
                    self.assertEqual(raw.encoding, adapter.OUTPUT_ENCODING)
                    expected = cv2.imdecode(
                        np.frombuffer(bytes(compressed.data), dtype=np.uint8),
                        cv2.IMREAD_UNCHANGED,
                    )
                    actual = np.frombuffer(bytes(raw.data), dtype=np.uint8).reshape(
                        (adapter.EXPECTED_HEIGHT, adapter.EXPECTED_WIDTH)
                    )
                    np.testing.assert_array_equal(actual, expected)

    def test_official_raw_profile_retopics_original_images_byte_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "official-raw.bag"
            output = root / "adapted.bag"
            self.write_source(
                source,
                profile=adapter.RAW_SOURCE_PROFILE,
                unmatched_stereo_stamp=True,
                camera1_record_offset_ns=30_000_000,
            )

            report = adapter.adapt_bag(source, output)
            auto_audit = adapter.audit_bag(source, "auto")
            self.assertEqual(report["source_profile"], adapter.RAW_SOURCE_PROFILE)
            self.assertEqual(auto_audit["source_profile"], adapter.RAW_SOURCE_PROFILE)
            self.assertEqual(
                report["source_audit"]["source_profile"],
                adapter.RAW_SOURCE_PROFILE,
            )
            self.assertTrue(report["preservation"]["all_passed"])
            self.assertTrue(
                report["preservation"]["checks"][
                    "camera0_recorded_message_semantics"
                ]
            )
            self.assertTrue(
                report["preservation"]["checks"][
                    "camera1_recorded_message_semantics"
                ]
            )
            source_stereo = report["source_audit"]["stereo"]
            adapted_stereo = report["adapted_audit"]["stereo"]
            for field in (
                "exact_header_pair_count",
                "camera0_unmatched_header_stamps_ns",
                "camera1_unmatched_header_stamps_ns",
                "matched_pair_record_skew_at_or_above_threshold_count",
                "matched_pair_record_time_absolute_skew",
                "pair_semantic_sha256",
            ):
                self.assertEqual(source_stereo[field], adapted_stereo[field])
            self.assertEqual(source_stereo["exact_header_pair_count"], 1)
            self.assertEqual(source_stereo["camera0_unmatched_count"], 1)
            self.assertEqual(source_stereo["camera1_unmatched_count"], 1)
            base = 1_600_000_000 * adapter.NANOSECONDS_PER_SECOND
            self.assertEqual(
                source_stereo["camera0_unmatched_header_stamps_ns"],
                [base + 43_000_000],
            )
            self.assertEqual(
                source_stereo["camera1_unmatched_header_stamps_ns"],
                [base + 63_000_000],
            )
            self.assertEqual(
                source_stereo[
                    "matched_pair_record_skew_at_or_above_threshold_count"
                ],
                1,
            )
            self.assertEqual(
                source_stereo["matched_pair_record_time_absolute_skew"]["max_ns"],
                27_000_000,
            )

            for source_topic, output_topic in (
                (adapter.RAW_SOURCE_CAMERA0_TOPIC, adapter.OUTPUT_CAMERA0_TOPIC),
                (adapter.RAW_SOURCE_CAMERA1_TOPIC, adapter.OUTPUT_CAMERA1_TOPIC),
            ):
                source_messages = self.messages(source, source_topic)
                output_messages = self.messages(output, output_topic)
                self.assertEqual(len(source_messages), 2)
                self.assertEqual(len(source_messages), len(output_messages))
                for (source_time, source_image), (output_time, output_image) in zip(
                    source_messages, output_messages
                ):
                    self.assertEqual(source_time, output_time)
                    self.assertEqual(serialize(source_image), serialize(output_image))
                    self.assertEqual(source_image.header, output_image.header)
                    self.assertEqual(bytes(source_image.data), bytes(output_image.data))

    def test_mixed_source_profiles_are_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mixed.bag"
            self.write_source(source, include_other_camera0=True)
            self.assert_adaptation_failure(source, "mixed or ambiguous.*profiles")

    def test_incomplete_source_profile_is_rejected_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "incomplete.bag"
            base = 1_600_000_000 * adapter.NANOSECONDS_PER_SECOND
            image = self.raw(
                self.image(0), 1, base + 1_000_000, "infra1_frame"
            )
            with rosbag.Bag(str(source), "w") as bag:
                bag.write(
                    adapter.RAW_SOURCE_CAMERA0_TOPIC,
                    image,
                    ros_time(base + 1_000_000),
                )
                bag.write(adapter.IMU_TOPIC, self.imu(0, base), ros_time(base))
                bag.write(
                    adapter.GROUND_TRUTH_TOPIC,
                    self.pose(0, base + 2_000_000),
                    ros_time(base + 2_000_000),
                )
            self.assert_adaptation_failure(source, "profile.*incomplete.*missing")

    def test_independent_adaptations_have_identical_semantic_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bag"
            first = root / "adapted-first.bag"
            second = root / "adapted-second.bag"
            self.write_source(source)

            first_report = adapter.adapt_bag(source, first)
            second_report = adapter.adapt_bag(source, second)

            for field in (
                "logical_order_semantic_sha256",
                "estimator_input_order_semantic_sha256",
            ):
                self.assertEqual(
                    first_report["adapted_audit"][field],
                    second_report["adapted_audit"][field],
                )
            self.assertEqual(
                first_report["adapted_audit"]["stereo"]["pair_semantic_sha256"],
                second_report["adapted_audit"]["stereo"]["pair_semantic_sha256"],
            )
            self.assertEqual(
                first_report["preservation"], second_report["preservation"]
            )

    def test_destination_created_during_publication_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bag"
            output = root / "adapted.bag"
            self.write_source(source, profile=adapter.RAW_SOURCE_PROFILE)

            def collide(_temporary_name: str, destination_name: str) -> None:
                Path(destination_name).write_bytes(b"racing publisher\n")
                raise FileExistsError(destination_name)

            with mock.patch.object(adapter.os, "link", side_effect=collide):
                with self.assertRaisesRegex(
                    adapter.AdapterError, "refusing to overwrite output bag"
                ):
                    adapter.adapt_bag(source, output)

            self.assertEqual(output.read_bytes(), b"racing publisher\n")
            self.assertEqual(list(root.glob(".adapted.bag-*.tmp.bag")), [])

    def assert_adaptation_failure(self, source: Path, pattern: str) -> None:
        output = source.with_name("must-not-exist.bag")
        with self.assertRaisesRegex(adapter.AdapterError, pattern):
            adapter.adapt_bag(source, output)
        self.assertFalse(output.exists())
        self.assertEqual(list(source.parent.glob(".must-not-exist.bag-*.tmp.bag")), [])

    def test_malformed_compressed_payload_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "malformed.bag"
            self.write_source(source, malformed=True)
            self.assert_adaptation_failure(source, "decompression|signature")

    def test_wrong_dimensions_fail_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "dimensions.bag"
            self.write_source(source, width=adapter.EXPECTED_WIDTH - 1)
            self.assert_adaptation_failure(source, "decoded dimensions")

    def test_wrong_message_type_fails_before_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "wrong-type.bag"
            self.write_source(source, wrong_type=True)
            self.assert_adaptation_failure(source, "has type.*expected")

    def test_nonmonotonic_header_timestamp_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "timestamp.bag"
            self.write_source(source, reversed_header=True)
            self.assert_adaptation_failure(source, "header timestamps.*strictly increasing")

    def test_unmatched_header_stamps_and_large_record_skew_are_not_filtered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "skew.bag"
            output = root / "adapted.bag"
            self.write_source(
                source,
                unmatched_stereo_stamp=True,
                camera1_record_offset_ns=23_000_000,
            )

            report = adapter.adapt_bag(source, output)
            stereo = report["adapted_audit"]["stereo"]
            self.assertEqual(stereo["exact_header_pair_count"], 1)
            self.assertEqual(stereo["camera0_unmatched_count"], 1)
            self.assertEqual(stereo["camera1_unmatched_count"], 1)
            self.assertEqual(
                stereo["matched_pair_record_skew_at_or_above_threshold_count"], 1
            )
            self.assertEqual(
                stereo["matched_pair_record_time_absolute_skew"]["max_ns"],
                adapter.STEREO_RECORD_SKEW_THRESHOLD_NS,
            )
            self.assertEqual(
                len(self.messages(output, adapter.OUTPUT_CAMERA0_TOPIC)), 2
            )
            self.assertEqual(
                len(self.messages(output, adapter.OUTPUT_CAMERA1_TOPIC)), 2
            )

    def test_nonfinite_runtime_imu_component_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "nonfinite-imu.bag"
            self.write_source(source, nonfinite_imu=True)
            self.assert_adaptation_failure(source, "non-finite runtime IMU component")

    def test_optional_imu_nonfinite_is_reported_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "optional-nonfinite-imu.bag"
            output = root / "adapted.bag"
            self.write_source(source, optional_nonfinite_imu=True)
            report = adapter.adapt_bag(source, output)
            source_imu = report["source_audit"]["streams"][adapter.IMU_TOPIC]
            output_imu = report["adapted_audit"]["streams"][adapter.IMU_TOPIC]
            self.assertEqual(source_imu["optional_nonfinite_total"], 1)
            self.assertEqual(
                source_imu["optional_nonfinite_counts"]["orientation_covariance"],
                1,
            )
            self.assertEqual(
                source_imu["optional_nonfinite_counts"],
                output_imu["optional_nonfinite_counts"],
            )

    def test_zero_ground_truth_quaternion_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "zero-quaternion.bag"
            self.write_source(source, zero_pose_quaternion=True)
            self.assert_adaptation_failure(source, "zero quaternion")

    def test_zero_header_stamp_fails_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "zero-stamp.bag"
            self.write_source(source, zero_header_stamp=True)
            self.assert_adaptation_failure(source, "header stamp.*must be nonzero")


if __name__ == "__main__":
    unittest.main()
