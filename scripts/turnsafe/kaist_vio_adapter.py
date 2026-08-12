#!/usr/bin/python3
"""Deterministically adapt official KAIST VIO ROS1 bags for serial replay.

The public KAIST VIO bags carry rectified infrared images through compressed
image-transport topics.  OpenVINS' serial reader consumes uncompressed
``sensor_msgs/Image`` messages instead.  This tool validates the four required
input streams, decodes only JPEG/PNG images, and writes a minimal bag with the
two TurnSafe-owned raw image topics plus the unchanged IMU stream.  Ground truth
is mandatory and audited at the source boundary, but is deliberately omitted
from the estimator-input bag.

No estimator behavior or calibration is encoded here.  The audit is semantic:
it is independent of rosbag chunking/compression and records exact timestamp,
message, decoded-image, and stereo-pair hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cv2
import numpy as np
import rosbag
from sensor_msgs.msg import Image


SOURCE_CAMERA0_TOPIC = "/camera/infra1/image_rect_raw/compressed"
SOURCE_CAMERA1_TOPIC = "/camera/infra2/image_rect_raw/compressed"
OUTPUT_CAMERA0_TOPIC = "/turnsafe/kaist/infra1/image_raw"
OUTPUT_CAMERA1_TOPIC = "/turnsafe/kaist/infra2/image_raw"
IMU_TOPIC = "/mavros/imu/data"
GROUND_TRUTH_TOPIC = "/pose_transformed"

EXPECTED_WIDTH = 640
EXPECTED_HEIGHT = 480
OUTPUT_ENCODING = "mono8"
STRICT_MAXIMUM_STEREO_SKEW_NS = 20_000_000
NANOSECONDS_PER_SECOND = 1_000_000_000

AUDIT_SCHEMA = "turnsafe.kaist_vio_adapter.audit.v1"
ADAPTATION_SCHEMA = "turnsafe.kaist_vio_adapter.adaptation.v1"


class AdapterError(RuntimeError):
    """A visible, fail-closed adapter or audit error."""


@dataclass(frozen=True)
class StreamSpec:
    topic: str
    message_type: str
    kind: str
    image_transport: Optional[str] = None


SOURCE_SPECS: Tuple[StreamSpec, ...] = (
    StreamSpec(
        SOURCE_CAMERA0_TOPIC,
        "sensor_msgs/CompressedImage",
        "camera0",
        "compressed",
    ),
    StreamSpec(
        SOURCE_CAMERA1_TOPIC,
        "sensor_msgs/CompressedImage",
        "camera1",
        "compressed",
    ),
    StreamSpec(IMU_TOPIC, "sensor_msgs/Imu", "imu"),
    StreamSpec(GROUND_TRUTH_TOPIC, "geometry_msgs/PoseStamped", "ground_truth"),
)

ADAPTED_SPECS: Tuple[StreamSpec, ...] = (
    StreamSpec(OUTPUT_CAMERA0_TOPIC, "sensor_msgs/Image", "camera0", "raw"),
    StreamSpec(OUTPUT_CAMERA1_TOPIC, "sensor_msgs/Image", "camera1", "raw"),
    StreamSpec(IMU_TOPIC, "sensor_msgs/Imu", "imu"),
)

SOURCE_TO_OUTPUT = {
    SOURCE_CAMERA0_TOPIC: OUTPUT_CAMERA0_TOPIC,
    SOURCE_CAMERA1_TOPIC: OUTPUT_CAMERA1_TOPIC,
    IMU_TOPIC: IMU_TOPIC,
}


def _feed_blob(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(struct.pack(">Q", len(value)))
    digest.update(value)


def _feed_text(digest: "hashlib._Hash", value: str) -> None:
    _feed_blob(digest, value.encode("utf-8"))


def _feed_u64(digest: "hashlib._Hash", value: int) -> None:
    if value < 0 or value > (1 << 64) - 1:
        raise AdapterError("semantic hash integer is outside uint64")
    digest.update(struct.pack(">Q", value))


def _time_to_ns(value: object, label: str) -> int:
    try:
        seconds = int(getattr(value, "secs"))
        nanoseconds = int(getattr(value, "nsecs"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} is not a ROS time".format(label)) from exc
    if seconds < 0 or not 0 <= nanoseconds < NANOSECONDS_PER_SECOND:
        raise AdapterError("{} is outside the supported ROS time range".format(label))
    timestamp = seconds * NANOSECONDS_PER_SECOND + nanoseconds
    if timestamp == 0:
        raise AdapterError("{} must be nonzero".format(label))
    return timestamp


def _serialized_message(message: object, topic: str) -> bytes:
    output = io.BytesIO()
    try:
        message.serialize(output)
    except Exception as exc:  # ROS messages expose several serialization errors.
        raise AdapterError("{} message cannot be serialized: {}".format(topic, exc)) from exc
    return output.getvalue()


def _header_values(message: object, topic: str) -> Tuple[int, int, str]:
    header = getattr(message, "header", None)
    if header is None:
        raise AdapterError("{} message has no header".format(topic))
    stamp_ns = _time_to_ns(header.stamp, "{} header stamp".format(topic))
    try:
        sequence = int(header.seq)
        frame_id = str(header.frame_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} has a malformed header".format(topic)) from exc
    if sequence < 0 or sequence > 0xFFFFFFFF:
        raise AdapterError("{} header sequence is outside uint32".format(topic))
    return stamp_ns, sequence, frame_id


def _compressed_codec(message: object, topic: str) -> str:
    image_format = str(getattr(message, "format", "")).strip().lower()
    codecs = re.findall(r"(?<![a-z0-9])(?:jpeg|jpg|png)(?![a-z0-9])", image_format)
    normalized = {"jpeg" if codec == "jpg" else codec for codec in codecs}
    if len(normalized) != 1:
        raise AdapterError(
            "{} compressed format must name exactly one JPEG or PNG codec; got {!r}".format(
                topic, getattr(message, "format", "")
            )
        )
    return next(iter(normalized))


def _decode_compressed_image(message: object, topic: str) -> Tuple[np.ndarray, str]:
    codec = _compressed_codec(message, topic)
    try:
        encoded = bytes(message.data)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} compressed payload is malformed".format(topic)) from exc
    if codec == "jpeg" and not encoded.startswith(b"\xff\xd8\xff"):
        raise AdapterError("{} declares JPEG but has no JPEG signature".format(topic))
    if codec == "png" and not encoded.startswith(b"\x89PNG\r\n\x1a\n"):
        raise AdapterError("{} declares PNG but has no PNG signature".format(topic))
    try:
        encoded_array = np.frombuffer(encoded, dtype=np.uint8)
        image = cv2.imdecode(encoded_array, cv2.IMREAD_UNCHANGED)
    except (cv2.error, TypeError, ValueError) as exc:
        raise AdapterError("{} {} decompression failed: {}".format(topic, codec, exc)) from exc
    if image is None:
        raise AdapterError("{} {} decompression returned no image".format(topic, codec))
    image = np.asarray(image)
    if image.dtype != np.uint8 or image.ndim != 2:
        raise AdapterError(
            "{} must decode to a two-dimensional uint8 mono image; got shape {} dtype {}".format(
                topic, image.shape, image.dtype
            )
        )
    if image.shape != (EXPECTED_HEIGHT, EXPECTED_WIDTH):
        raise AdapterError(
            "{} decoded dimensions are {}x{}, expected {}x{}".format(
                topic,
                image.shape[1],
                image.shape[0],
                EXPECTED_WIDTH,
                EXPECTED_HEIGHT,
            )
        )
    return np.ascontiguousarray(image), codec


def _validate_raw_image(message: object, topic: str) -> np.ndarray:
    try:
        height = int(message.height)
        width = int(message.width)
        encoding = str(message.encoding)
        is_bigendian = int(message.is_bigendian)
        step = int(message.step)
        payload = bytes(message.data)
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} raw image fields are malformed".format(topic)) from exc
    if (width, height) != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
        raise AdapterError(
            "{} raw dimensions are {}x{}, expected {}x{}".format(
                topic, width, height, EXPECTED_WIDTH, EXPECTED_HEIGHT
            )
        )
    if encoding != OUTPUT_ENCODING:
        raise AdapterError(
            "{} raw encoding is {!r}, expected {!r}".format(
                topic, encoding, OUTPUT_ENCODING
            )
        )
    if is_bigendian != 0:
        raise AdapterError("{} mono8 output must use is_bigendian=0".format(topic))
    if step != EXPECTED_WIDTH:
        raise AdapterError(
            "{} raw step is {}, expected {}".format(topic, step, EXPECTED_WIDTH)
        )
    expected_bytes = EXPECTED_WIDTH * EXPECTED_HEIGHT
    if len(payload) != expected_bytes:
        raise AdapterError(
            "{} raw payload is {} bytes, expected {}".format(
                topic, len(payload), expected_bytes
            )
        )
    return np.frombuffer(payload, dtype=np.uint8).reshape(
        (EXPECTED_HEIGHT, EXPECTED_WIDTH)
    )


def _raw_image(image: np.ndarray, source: object) -> Image:
    output = Image()
    output.header.seq = source.header.seq
    output.header.stamp = source.header.stamp
    output.header.frame_id = source.header.frame_id
    output.height = EXPECTED_HEIGHT
    output.width = EXPECTED_WIDTH
    output.encoding = OUTPUT_ENCODING
    output.is_bigendian = 0
    output.step = EXPECTED_WIDTH
    output.data = np.ascontiguousarray(image).tobytes()
    return output


def _validate_imu(message: object, topic: str) -> Dict[str, int]:
    try:
        values = (
            float(message.angular_velocity.x),
            float(message.angular_velocity.y),
            float(message.angular_velocity.z),
            float(message.linear_acceleration.x),
            float(message.linear_acceleration.y),
            float(message.linear_acceleration.z),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} runtime IMU fields are malformed".format(topic)) from exc
    if not all(math.isfinite(value) for value in values):
        raise AdapterError("{} has a non-finite runtime IMU component".format(topic))
    try:
        optional = {
            "orientation": (
                float(message.orientation.x),
                float(message.orientation.y),
                float(message.orientation.z),
                float(message.orientation.w),
            ),
            "orientation_covariance": tuple(
                float(value) for value in message.orientation_covariance
            ),
            "angular_velocity_covariance": tuple(
                float(value) for value in message.angular_velocity_covariance
            ),
            "linear_acceleration_covariance": tuple(
                float(value) for value in message.linear_acceleration_covariance
            ),
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} optional IMU fields are malformed".format(topic)) from exc
    return {
        label: sum(not math.isfinite(value) for value in field_values)
        for label, field_values in optional.items()
    }


def _validate_ground_truth(message: object, topic: str) -> None:
    try:
        position = (
            float(message.pose.position.x),
            float(message.pose.position.y),
            float(message.pose.position.z),
        )
        quaternion = (
            float(message.pose.orientation.x),
            float(message.pose.orientation.y),
            float(message.pose.orientation.z),
            float(message.pose.orientation.w),
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise AdapterError("{} pose fields are malformed".format(topic)) from exc
    if not all(math.isfinite(value) for value in position + quaternion):
        raise AdapterError("{} has a non-finite pose component".format(topic))
    if not any(value != 0.0 for value in quaternion):
        raise AdapterError("{} has a zero quaternion".format(topic))


class _StreamAudit:
    def __init__(self, spec: StreamSpec, connections: int) -> None:
        self.spec = spec
        self.connections = connections
        self.count = 0
        self.record_times: List[int] = []
        self.header_times: List[int] = []
        self.frame_ids = set()  # type: set
        self.message_digest = hashlib.sha256()
        _feed_text(self.message_digest, "turnsafe.kaist_vio.recorded_message.v1")
        _feed_text(self.message_digest, spec.message_type)
        self.image_digest = hashlib.sha256()
        _feed_text(self.image_digest, "turnsafe.kaist_vio.decoded_mono8.v1")
        self.formats = set()  # type: set
        self.codecs = set()  # type: set
        self.optional_imu_nonfinite_counts = {
            "orientation": 0,
            "orientation_covariance": 0,
            "angular_velocity_covariance": 0,
            "linear_acceleration_covariance": 0,
        }

    def observe(
        self,
        message: object,
        record_time: object,
        decoded_image: Optional[np.ndarray] = None,
        codec: Optional[str] = None,
    ) -> Optional[np.ndarray]:
        actual_type = getattr(message, "_type", None)
        if actual_type != self.spec.message_type:
            raise AdapterError(
                "{} has type {!r}, expected {!r}".format(
                    self.spec.topic, actual_type, self.spec.message_type
                )
            )
        record_ns = _time_to_ns(record_time, "{} bag record time".format(self.spec.topic))
        header_ns, sequence, frame_id = _header_values(message, self.spec.topic)
        if self.spec.kind == "imu":
            optional_nonfinite = _validate_imu(message, self.spec.topic)
            for label, count in optional_nonfinite.items():
                self.optional_imu_nonfinite_counts[label] += count
        elif self.spec.kind == "ground_truth":
            _validate_ground_truth(message, self.spec.topic)
        if self.record_times and record_ns <= self.record_times[-1]:
            raise AdapterError(
                "{} bag record timestamps are not strictly increasing at message {}".format(
                    self.spec.topic, self.count
                )
            )
        if self.header_times and header_ns <= self.header_times[-1]:
            raise AdapterError(
                "{} header timestamps are not strictly increasing at message {}".format(
                    self.spec.topic, self.count
                )
            )

        if self.spec.image_transport == "compressed":
            if decoded_image is None or codec is None:
                decoded_image, codec = _decode_compressed_image(
                    message, self.spec.topic
                )
            self.formats.add(str(message.format))
            self.codecs.add(codec)
        elif self.spec.image_transport == "raw":
            if decoded_image is None:
                decoded_image = _validate_raw_image(message, self.spec.topic)

        serialized = _serialized_message(message, self.spec.topic)
        _feed_u64(self.message_digest, record_ns)
        _feed_blob(self.message_digest, serialized)

        if decoded_image is not None:
            if decoded_image.dtype != np.uint8 or decoded_image.shape != (
                EXPECTED_HEIGHT,
                EXPECTED_WIDTH,
            ):
                raise AdapterError("{} supplied decoded image is invalid".format(self.spec.topic))
            _feed_u64(self.image_digest, record_ns)
            _feed_u64(self.image_digest, header_ns)
            _feed_u64(self.image_digest, sequence)
            _feed_text(self.image_digest, frame_id)
            _feed_u64(self.image_digest, EXPECTED_WIDTH)
            _feed_u64(self.image_digest, EXPECTED_HEIGHT)
            _feed_text(self.image_digest, OUTPUT_ENCODING)
            _feed_blob(self.image_digest, np.ascontiguousarray(decoded_image).tobytes())

        self.record_times.append(record_ns)
        self.header_times.append(header_ns)
        self.frame_ids.add(frame_id)
        self.count += 1
        return decoded_image

    def report(self) -> Dict[str, object]:
        if self.count == 0:
            raise AdapterError("required topic {} is empty".format(self.spec.topic))
        record_duration = self.record_times[-1] - self.record_times[0]
        header_duration = self.header_times[-1] - self.header_times[0]
        record_rate = None
        header_rate = None
        if self.count > 1:
            if record_duration <= 0:
                raise AdapterError("{} has no positive record duration".format(self.spec.topic))
            if header_duration <= 0:
                raise AdapterError("{} has no positive header duration".format(self.spec.topic))
            record_rate = round(
                (self.count - 1) * NANOSECONDS_PER_SECOND / record_duration, 9
            )
            header_rate = round(
                (self.count - 1) * NANOSECONDS_PER_SECOND / header_duration, 9
            )
        result: Dict[str, object] = {
            "kind": self.spec.kind,
            "type": self.spec.message_type,
            "connection_count": self.connections,
            "count": self.count,
            "record_time": {
                "start_ns": self.record_times[0],
                "end_ns": self.record_times[-1],
                "duration_ns": record_duration,
                "strictly_increasing": True,
                "rate_hz": record_rate,
            },
            "header_time": {
                "start_ns": self.header_times[0],
                "end_ns": self.header_times[-1],
                "duration_ns": header_duration,
                "strictly_increasing": True,
                "rate_hz": header_rate,
            },
            "frame_ids": sorted(self.frame_ids),
            "recorded_message_semantic_sha256": self.message_digest.hexdigest(),
        }
        if self.spec.image_transport is not None:
            image: Dict[str, object] = {
                "transport": self.spec.image_transport,
                "decoded_width": EXPECTED_WIDTH,
                "decoded_height": EXPECTED_HEIGHT,
                "decoded_encoding": OUTPUT_ENCODING,
                "decoded_image_semantic_sha256": self.image_digest.hexdigest(),
            }
            if self.spec.image_transport == "compressed":
                image["compressed_formats"] = sorted(self.formats)
                image["codecs"] = sorted(self.codecs)
            else:
                image.update(
                    {
                        "width": EXPECTED_WIDTH,
                        "height": EXPECTED_HEIGHT,
                        "encoding": OUTPUT_ENCODING,
                        "is_bigendian": 0,
                        "step": EXPECTED_WIDTH,
                    }
                )
            result["image"] = image
        if self.spec.kind == "imu":
            result["finite_runtime_components"] = True
            result["optional_nonfinite_counts"] = dict(
                sorted(self.optional_imu_nonfinite_counts.items())
            )
            result["optional_nonfinite_total"] = sum(
                self.optional_imu_nonfinite_counts.values()
            )
        elif self.spec.kind == "ground_truth":
            result["finite_pose_components"] = True
            result["nonzero_quaternion"] = True
        return result


class _AuditCollector:
    def __init__(
        self,
        role: str,
        specs: Sequence[StreamSpec],
        metadata: Mapping[str, Mapping[str, int]],
        ignored_topics: Sequence[Mapping[str, object]],
    ) -> None:
        self.role = role
        self.specs = tuple(specs)
        self.streams = {
            spec.topic: _StreamAudit(
                spec, int(metadata.get(spec.topic, {}).get("connections", 1))
            )
            for spec in self.specs
        }
        self.metadata = metadata
        self.ignored_topics = list(ignored_topics)
        self.logical_digest = hashlib.sha256()
        _feed_text(self.logical_digest, "turnsafe.kaist_vio.logical_order.v1")
        self.estimator_input_digest = hashlib.sha256()
        _feed_text(
            self.estimator_input_digest,
            "turnsafe.kaist_vio.estimator_input_order.v1",
        )

    def observe(self, topic: str, message: object, record_time: object) -> Optional[np.ndarray]:
        if topic not in self.streams:
            raise AdapterError("unexpected observed topic {}".format(topic))
        stream = self.streams[topic]
        image = None
        codec = None
        if stream.spec.image_transport == "compressed":
            image, codec = _decode_compressed_image(message, topic)
        image = stream.observe(message, record_time, image, codec)

        record_ns = stream.record_times[-1]
        header_ns, sequence, frame_id = _header_values(message, topic)
        digests = [self.logical_digest]
        if stream.spec.kind != "ground_truth":
            digests.append(self.estimator_input_digest)
        for digest in digests:
            _feed_text(digest, stream.spec.kind)
            _feed_u64(digest, record_ns)
            _feed_u64(digest, header_ns)
            _feed_u64(digest, sequence)
            _feed_text(digest, frame_id)
            if image is None:
                _feed_blob(digest, _serialized_message(message, topic))
            else:
                _feed_text(digest, OUTPUT_ENCODING)
                _feed_u64(digest, EXPECTED_WIDTH)
                _feed_u64(digest, EXPECTED_HEIGHT)
                _feed_blob(digest, np.ascontiguousarray(image).tobytes())
        return image

    def finalize(self) -> Dict[str, object]:
        stream_reports = {}
        for spec in self.specs:
            stream = self.streams[spec.topic]
            expected_count = self.metadata.get(spec.topic, {}).get("message_count")
            if expected_count is not None and stream.count != int(expected_count):
                raise AdapterError(
                    "{} yielded {} messages but rosbag metadata declares {}".format(
                        spec.topic, stream.count, expected_count
                    )
                )
            stream_reports[spec.topic] = stream.report()

        camera0 = next(stream for stream in self.streams.values() if stream.spec.kind == "camera0")
        camera1 = next(stream for stream in self.streams.values() if stream.spec.kind == "camera1")
        stereo = _stereo_report(camera0, camera1)
        return {
            "schema": AUDIT_SCHEMA,
            "role": self.role,
            "required_topic_count": len(self.specs),
            "total_required_messages": sum(stream.count for stream in self.streams.values()),
            "ignored_topics": self.ignored_topics,
            "streams": stream_reports,
            "stereo": stereo,
            "logical_order_semantic_sha256": self.logical_digest.hexdigest(),
            "estimator_input_order_semantic_sha256":
                self.estimator_input_digest.hexdigest(),
        }


def _skew_report(values: Sequence[int]) -> Dict[str, object]:
    if not values:
        raise AdapterError("stereo stream has no pairs")
    total = sum(values)
    return {
        "min_ns": min(values),
        "max_ns": max(values),
        "sum_ns": total,
        "mean_ns": round(total / len(values), 3),
    }


def _stereo_report(camera0: _StreamAudit, camera1: _StreamAudit) -> Dict[str, object]:
    if camera0.count != camera1.count:
        raise AdapterError(
            "stereo streams have unequal counts: {} has {}, {} has {}".format(
                camera0.spec.topic,
                camera0.count,
                camera1.spec.topic,
                camera1.count,
            )
        )
    if camera0.count == 0:
        raise AdapterError("stereo streams are empty")
    record_skews = [
        abs(left - right)
        for left, right in zip(camera0.record_times, camera1.record_times)
    ]
    header_skews = [
        abs(left - right)
        for left, right in zip(camera0.header_times, camera1.header_times)
    ]
    for index, (record_skew, header_skew) in enumerate(zip(record_skews, header_skews)):
        if record_skew >= STRICT_MAXIMUM_STEREO_SKEW_NS:
            raise AdapterError(
                "stereo pair {} record-time skew {} ns is not below {} ns".format(
                    index, record_skew, STRICT_MAXIMUM_STEREO_SKEW_NS
                )
            )
        if header_skew != 0:
            raise AdapterError(
                "stereo pair {} header-time skew is {} ns; exact equality is required".format(
                    index, header_skew
                )
            )

    digest = hashlib.sha256()
    _feed_text(digest, "turnsafe.kaist_vio.ordinal_stereo_pairs.v1")
    for index in range(camera0.count):
        _feed_u64(digest, index)
        _feed_u64(digest, camera0.record_times[index])
        _feed_u64(digest, camera1.record_times[index])
        _feed_u64(digest, camera0.header_times[index])
        _feed_u64(digest, camera1.header_times[index])
    return {
        "policy": "ordinal_no_reuse",
        "complete": True,
        "pair_count": camera0.count,
        "strict_maximum_skew_ns": STRICT_MAXIMUM_STEREO_SKEW_NS,
        "header_stamps_exactly_equal": True,
        "camera0_topic": camera0.spec.topic,
        "camera1_topic": camera1.spec.topic,
        "record_time_absolute_skew": _skew_report(record_skews),
        "header_time_absolute_skew": _skew_report(header_skews),
        "pair_semantic_sha256": digest.hexdigest(),
    }


def _specs_for_role(role: str) -> Tuple[StreamSpec, ...]:
    if role == "source":
        return SOURCE_SPECS
    if role == "adapted":
        return ADAPTED_SPECS
    raise AdapterError("unknown bag role {!r}".format(role))


def _detect_role(topic_names: Iterable[str]) -> str:
    names = set(topic_names)
    has_source = all(spec.topic in names for spec in SOURCE_SPECS)
    has_adapted = all(spec.topic in names for spec in ADAPTED_SPECS)
    if has_source == has_adapted:
        raise AdapterError(
            "cannot uniquely detect bag role; specify --kind source or adapted"
        )
    return "source" if has_source else "adapted"


def _validate_metadata(
    bag: rosbag.Bag, role: str
) -> Tuple[Tuple[StreamSpec, ...], Dict[str, Dict[str, int]], List[Dict[str, object]]]:
    info = bag.get_type_and_topic_info()
    if role == "auto":
        role = _detect_role(info.topics.keys())
    specs = _specs_for_role(role)
    required = {spec.topic for spec in specs}
    metadata: Dict[str, Dict[str, int]] = {}
    for spec in specs:
        topic_info = info.topics.get(spec.topic)
        if topic_info is None:
            raise AdapterError("required topic {} is absent".format(spec.topic))
        if topic_info.msg_type != spec.message_type:
            raise AdapterError(
                "{} has type {!r}, expected {!r}".format(
                    spec.topic, topic_info.msg_type, spec.message_type
                )
            )
        metadata[spec.topic] = {
            "message_count": int(topic_info.message_count),
            "connections": int(topic_info.connections),
        }
    ignored = [
        {
            "topic": topic,
            "type": topic_info.msg_type,
            "message_count": int(topic_info.message_count),
            "connections": int(topic_info.connections),
        }
        for topic, topic_info in sorted(info.topics.items())
        if topic not in required
    ]
    if role == "adapted" and ignored:
        raise AdapterError(
            "adapted estimator-input bag contains unexpected topics: {}".format(
                ", ".join(item["topic"] for item in ignored)
            )
        )
    return specs, metadata, ignored


def audit_bag(path: Path, role: str = "auto") -> Dict[str, object]:
    """Audit one source or adapted ROS1 bag in one streaming pass."""

    bag_path = Path(path)
    if not bag_path.is_file():
        raise AdapterError("input bag is not a regular file: {}".format(bag_path))
    try:
        with rosbag.Bag(str(bag_path), "r") as bag:
            specs, metadata, ignored = _validate_metadata(bag, role)
            resolved_role = "source" if specs == SOURCE_SPECS else "adapted"
            collector = _AuditCollector(resolved_role, specs, metadata, ignored)
            topics = [spec.topic for spec in specs]
            for topic, message, record_time in bag.read_messages(topics=topics):
                collector.observe(topic, message, record_time)
            return collector.finalize()
    except AdapterError:
        raise
    except rosbag.ROSBagException as exc:
        raise AdapterError("unable to audit ROS1 bag {}: {}".format(bag_path, exc)) from exc


def _assert_adaptation_preservation(
    source: Mapping[str, object], adapted: Mapping[str, object]
) -> Dict[str, object]:
    source_streams = source["streams"]
    adapted_streams = adapted["streams"]
    checks = {
        "camera0_decoded_image_semantics":
            source_streams[SOURCE_CAMERA0_TOPIC]["image"]["decoded_image_semantic_sha256"]
            == adapted_streams[OUTPUT_CAMERA0_TOPIC]["image"]["decoded_image_semantic_sha256"],
        "camera1_decoded_image_semantics":
            source_streams[SOURCE_CAMERA1_TOPIC]["image"]["decoded_image_semantic_sha256"]
            == adapted_streams[OUTPUT_CAMERA1_TOPIC]["image"]["decoded_image_semantic_sha256"],
        "imu_recorded_message_semantics":
            source_streams[IMU_TOPIC]["recorded_message_semantic_sha256"]
            == adapted_streams[IMU_TOPIC]["recorded_message_semantic_sha256"],
        "source_ground_truth_validated":
            source_streams[GROUND_TRUTH_TOPIC]["finite_pose_components"]
            and source_streams[GROUND_TRUTH_TOPIC]["nonzero_quaternion"],
        "ground_truth_intentionally_omitted": GROUND_TRUTH_TOPIC
        not in adapted_streams,
        "stereo_pair_semantics": source["stereo"]["pair_semantic_sha256"]
        == adapted["stereo"]["pair_semantic_sha256"],
        "estimator_input_order_semantics":
            source["estimator_input_order_semantic_sha256"]
            == adapted["estimator_input_order_semantic_sha256"],
        "retained_message_count":
            sum(
                source_streams[topic]["count"]
                for topic in (
                    SOURCE_CAMERA0_TOPIC,
                    SOURCE_CAMERA1_TOPIC,
                    IMU_TOPIC,
                )
            )
            == adapted["total_required_messages"],
    }
    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise AdapterError(
            "adapted semantic preservation checks failed: {}".format(", ".join(failed))
        )
    return {"all_passed": True, "checks": checks}


def adapt_bag(input_path: Path, output_path: Path) -> Dict[str, object]:
    """Validate and adapt a KAIST VIO bag, atomically producing ``output_path``."""

    source_path = Path(input_path)
    destination = Path(output_path)
    if not source_path.is_file():
        raise AdapterError("input bag is not a regular file: {}".format(source_path))
    if destination.exists():
        raise AdapterError("refusing to overwrite output bag: {}".format(destination))
    if not destination.parent.is_dir():
        raise AdapterError("output directory does not exist: {}".format(destination.parent))
    if source_path.resolve() == destination.resolve():
        raise AdapterError("input and output bag paths must differ")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(destination.name),
        suffix=".tmp.bag",
        dir=str(destination.parent),
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with rosbag.Bag(str(source_path), "r") as source_bag:
            specs, metadata, ignored = _validate_metadata(source_bag, "source")
            source_audit = _AuditCollector("source", specs, metadata, ignored)
            output_metadata = {
                spec.topic: {"connections": 1} for spec in ADAPTED_SPECS
            }
            adapted_audit = _AuditCollector(
                "adapted", ADAPTED_SPECS, output_metadata, []
            )
            with rosbag.Bag(str(temporary_path), "w") as output_bag:
                topics = [spec.topic for spec in specs]
                for topic, message, record_time in source_bag.read_messages(topics=topics):
                    decoded = source_audit.observe(topic, message, record_time)
                    if topic == GROUND_TRUTH_TOPIC:
                        continue
                    output_topic = SOURCE_TO_OUTPUT[topic]
                    output_message = message
                    if decoded is not None:
                        output_message = _raw_image(decoded, message)
                    adapted_audit.observe(output_topic, output_message, record_time)
                    output_bag.write(output_topic, output_message, record_time)

            source_report = source_audit.finalize()
            adapted_report = adapted_audit.finalize()
            preservation = _assert_adaptation_preservation(
                source_report, adapted_report
            )
        os.replace(str(temporary_path), str(destination))
    except AdapterError:
        temporary_path.unlink(missing_ok=True)
        raise
    except (OSError, rosbag.ROSBagException) as exc:
        temporary_path.unlink(missing_ok=True)
        raise AdapterError("KAIST VIO adaptation failed: {}".format(exc)) from exc
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return {
        "schema": ADAPTATION_SCHEMA,
        "source_audit": source_report,
        "adapted_audit": adapted_report,
        "preservation": preservation,
        "output_topics": {
            "camera0": OUTPUT_CAMERA0_TOPIC,
            "camera1": OUTPUT_CAMERA1_TOPIC,
            "imu": IMU_TOPIC,
        },
        "ground_truth_policy": "source_required_and_audited_output_omitted",
    }


def _emit_json(report: Mapping[str, object], output: Optional[Path]) -> None:
    payload = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if output is None:
        sys.stdout.buffer.write(payload)
        return
    destination = Path(output)
    if destination.exists():
        raise AdapterError("refusing to overwrite JSON report: {}".format(destination))
    if not destination.parent.is_dir():
        raise AdapterError("JSON output directory does not exist: {}".format(destination.parent))
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-".format(destination.name),
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(destination))
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate or adapt an official KAIST VIO ROS1 bag."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    adapt = subparsers.add_parser(
        "adapt", help="convert official compressed infrared topics to raw mono8"
    )
    adapt.add_argument("input_bag", type=Path)
    adapt.add_argument("output_bag", type=Path)
    adapt.add_argument("--json-output", type=Path)

    audit = subparsers.add_parser(
        "audit", help="emit a deterministic semantic stream audit"
    )
    audit.add_argument("input_bag", type=Path)
    audit.add_argument(
        "--kind", choices=("auto", "source", "adapted"), default="auto"
    )
    audit.add_argument("--json-output", type=Path)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.json_output is not None and args.json_output.exists():
            raise AdapterError(
                "refusing to overwrite JSON report: {}".format(args.json_output)
            )
        if args.command == "adapt":
            report = adapt_bag(args.input_bag, args.output_bag)
        else:
            report = audit_bag(args.input_bag, args.kind)
        _emit_json(report, args.json_output)
    except AdapterError as exc:
        print("KAIST_VIO_ADAPTER_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
