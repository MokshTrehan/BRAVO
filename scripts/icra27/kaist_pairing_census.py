#!/usr/bin/env python3
"""Deterministic KAIST U0/S1 stereo-pairing census.

This tool reads metadata from one unchanged adapted KAIST rosbag.  It mirrors
the pinned upstream ``ros1_serial_msckf`` two-camera branch, including its
binary64 record-time comparison and its unusual ``used_index`` behavior, and
independently evaluates the SchurVIO-Lite exact-header selector.

Image payloads are not decoded.  The ROS-serialized ``std_msgs/Header`` prefix
is parsed directly so a multi-gigabyte bag can be audited with bounded memory.
"""

from __future__ import print_function

import argparse
import collections
import dataclasses
import hashlib
import json
import os
import struct
import sys
import tempfile
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Set, Tuple


SCHEMA = "schurvio.icra27.kaist_pairing_census.v1"
CAMERA0_TOPIC = "/turnsafe/kaist/infra1/image_raw"
CAMERA1_TOPIC = "/turnsafe/kaist/infra2/image_raw"
IMU_TOPIC = "/mavros/imu/data"
IMAGE_DATATYPE = "sensor_msgs/Image"
IMAGE_MD5 = "060021388200f6f0f447d0fcd9c64743"
IMU_DATATYPE = "sensor_msgs/Imu"
IMU_MD5 = "6a62c6daae103f4ff57a132d6f95cec2"
NANOSECONDS_PER_SECOND = 1_000_000_000
NATIVE_LIMIT_SECONDS = 0.02
NOMINAL_LIMIT_NS = 20_000_000

KIND_IMU = "imu"
KIND_CAMERA0 = "camera0"
KIND_CAMERA1 = "camera1"
CAMERA_KINDS = frozenset((KIND_CAMERA0, KIND_CAMERA1))


class CensusError(RuntimeError):
    """An input or pairing invariant prevents an auditable census."""


@dataclasses.dataclass(frozen=True)
class FilteredMessage:
    """Metadata for one message in the topic-filtered rosbag view."""

    kind: str
    record_time_ns: int
    header_time_ns: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind not in (KIND_IMU, KIND_CAMERA0, KIND_CAMERA1):
            raise ValueError("invalid filtered-message kind: {!r}".format(self.kind))
        if self.record_time_ns < 0:
            raise ValueError("negative record timestamp")
        if self.kind in CAMERA_KINDS:
            if self.header_time_ns is None or self.header_time_ns < 0:
                raise ValueError("camera message has no nonnegative header timestamp")
        elif self.header_time_ns is not None:
            raise ValueError("IMU header time is intentionally not retained")


@dataclasses.dataclass(frozen=True)
class StereoPair:
    """One selected pair, normalized to camera-0/camera-1 order."""

    selection_index: int
    anchor_filtered_index: int
    anchor_camera_id: int
    camera0_filtered_index: int
    camera1_filtered_index: int
    camera0_record_time_ns: int
    camera1_record_time_ns: int
    camera0_header_time_ns: int
    camera1_header_time_ns: int

    @property
    def identity(self) -> Tuple[int, int]:
        return (self.camera0_filtered_index, self.camera1_filtered_index)

    @property
    def record_skew_ns(self) -> int:
        return abs(self.camera0_record_time_ns - self.camera1_record_time_ns)

    @property
    def camera_timestamp_ns(self) -> int:
        """Timestamp actually used by both runners: camera-0's header."""

        return self.camera0_header_time_ns


@dataclasses.dataclass(frozen=True)
class NativeSelection:
    pairs: Tuple[StereoPair, ...]
    used_index_outer_skip_count: int
    no_pair_outer_skip_count: int
    candidate_use_counts: Mapping[int, int]
    residual_used_indices: Tuple[int, ...]


@dataclasses.dataclass(frozen=True)
class ExactHeaderSelection:
    pairs: Tuple[StereoPair, ...]
    camera0_count: int
    camera1_count: int
    camera0_unmatched_headers_ns: Tuple[int, ...]
    camera1_unmatched_headers_ns: Tuple[int, ...]


def _validate_filtered_view(messages: Sequence[FilteredMessage]) -> None:
    previous: Optional[int] = None
    for index, message in enumerate(messages):
        if previous is not None and message.record_time_ns < previous:
            raise CensusError(
                "filtered rosbag record time reverses at index {}".format(index)
            )
        previous = message.record_time_ns


def _cpp_ros_time_to_sec(timestamp_ns: int) -> float:
    """Mirror ``ros::TimeBase::toSec`` from ROS Noetic's ``time.h``."""

    seconds, nanoseconds = divmod(timestamp_ns, NANOSECONDS_PER_SECOND)
    return float(seconds) + 1e-9 * float(nanoseconds)


def select_upstream_native(messages: Sequence[FilteredMessage]) -> NativeSelection:
    """Reproduce pinned upstream's native first-forward stereo branch.

    In particular, the upstream set is consulted only when an outer-loop
    index is visited.  Candidate selection does *not* reject an index already
    present in that set, so a future camera image can be reused by multiple
    earlier anchors.  This function preserves that behavior deliberately.
    """

    _validate_filtered_view(messages)
    used_index: Set[int] = set()
    pairs: List[StereoPair] = []
    candidate_use_counts: MutableMapping[int, int] = collections.Counter()
    used_index_outer_skip_count = 0
    no_pair_outer_skip_count = 0

    for anchor_index, anchor in enumerate(messages):
        if anchor_index in used_index:
            used_index.remove(anchor_index)
            if anchor.kind in CAMERA_KINDS:
                used_index_outer_skip_count += 1
            continue
        if anchor.kind not in CAMERA_KINDS:
            continue

        other_kind = KIND_CAMERA1 if anchor.kind == KIND_CAMERA0 else KIND_CAMERA0
        candidate_index: Optional[int] = None
        for index in range(anchor_index, len(messages)):
            candidate = messages[index]
            if candidate.kind != other_kind:
                continue
            anchor_seconds = _cpp_ros_time_to_sec(anchor.record_time_ns)
            candidate_seconds = _cpp_ros_time_to_sec(candidate.record_time_ns)
            if abs(candidate_seconds - anchor_seconds) < NATIVE_LIMIT_SECONDS:
                candidate_index = index
            # The source breaks after the first later opposite-camera message,
            # whether or not it passes the threshold.
            break

        if candidate_index is None:
            no_pair_outer_skip_count += 1
            continue

        candidate = messages[candidate_index]
        if anchor.kind == KIND_CAMERA0:
            camera0_index, camera1_index = anchor_index, candidate_index
            anchor_camera_id = 0
        else:
            camera0_index, camera1_index = candidate_index, anchor_index
            anchor_camera_id = 1
        camera0 = messages[camera0_index]
        camera1 = messages[camera1_index]
        assert camera0.header_time_ns is not None
        assert camera1.header_time_ns is not None
        pairs.append(
            StereoPair(
                selection_index=len(pairs),
                anchor_filtered_index=anchor_index,
                anchor_camera_id=anchor_camera_id,
                camera0_filtered_index=camera0_index,
                camera1_filtered_index=camera1_index,
                camera0_record_time_ns=camera0.record_time_ns,
                camera1_record_time_ns=camera1.record_time_ns,
                camera0_header_time_ns=camera0.header_time_ns,
                camera1_header_time_ns=camera1.header_time_ns,
            )
        )
        candidate_use_counts[candidate_index] += 1
        # These are the two exact insertions in upstream.  Adding the current
        # anchor leaves a harmless residual entry because it is never revisited.
        used_index.add(camera0_index)
        used_index.add(camera1_index)

    return NativeSelection(
        pairs=tuple(pairs),
        used_index_outer_skip_count=used_index_outer_skip_count,
        no_pair_outer_skip_count=no_pair_outer_skip_count,
        candidate_use_counts=dict(sorted(candidate_use_counts.items())),
        residual_used_indices=tuple(sorted(used_index)),
    )


def _camera_header_index(
    messages: Sequence[FilteredMessage], kind: str
) -> Tuple[Dict[int, int], int]:
    by_header: Dict[int, int] = {}
    previous_header: Optional[int] = None
    count = 0
    for index, message in enumerate(messages):
        if message.kind != kind:
            continue
        count += 1
        header = message.header_time_ns
        assert header is not None
        if header == 0:
            raise CensusError("{} has a zero header timestamp".format(kind))
        if previous_header is not None and header == previous_header:
            raise CensusError("{} has a duplicate header timestamp".format(kind))
        if previous_header is not None and header < previous_header:
            raise CensusError("{} header order reverses".format(kind))
        if header in by_header:
            raise CensusError("{} has a nonadjacent duplicate header".format(kind))
        by_header[header] = index
        previous_header = header
    return by_header, count


def select_exact_header(messages: Sequence[FilteredMessage]) -> ExactHeaderSelection:
    """Mirror the frozen S1 KAIST exact-header selector."""

    _validate_filtered_view(messages)
    camera0_by_header, camera0_count = _camera_header_index(messages, KIND_CAMERA0)
    camera1_by_header, camera1_count = _camera_header_index(messages, KIND_CAMERA1)
    common_headers = sorted(set(camera0_by_header).intersection(camera1_by_header))
    if not common_headers:
        raise CensusError("camera streams have no exact-header pair")

    pairs: List[StereoPair] = []
    for header in common_headers:
        camera0_index = camera0_by_header[header]
        camera1_index = camera1_by_header[header]
        camera0 = messages[camera0_index]
        camera1 = messages[camera1_index]
        anchor_index = min(camera0_index, camera1_index)
        anchor_kind = messages[anchor_index].kind
        pairs.append(
            StereoPair(
                selection_index=0,
                anchor_filtered_index=anchor_index,
                anchor_camera_id=0 if anchor_kind == KIND_CAMERA0 else 1,
                camera0_filtered_index=camera0_index,
                camera1_filtered_index=camera1_index,
                camera0_record_time_ns=camera0.record_time_ns,
                camera1_record_time_ns=camera1.record_time_ns,
                camera0_header_time_ns=header,
                camera1_header_time_ns=header,
            )
        )

    pairs.sort(key=lambda pair: pair.anchor_filtered_index)
    normalized: List[StereoPair] = []
    previous_timestamp: Optional[int] = None
    for selection_index, pair in enumerate(pairs):
        if previous_timestamp is not None and pair.camera_timestamp_ns <= previous_timestamp:
            raise CensusError("exact-header dispatch timestamp is not increasing")
        normalized.append(dataclasses.replace(pair, selection_index=selection_index))
        previous_timestamp = pair.camera_timestamp_ns

    camera0_headers = set(camera0_by_header)
    camera1_headers = set(camera1_by_header)
    return ExactHeaderSelection(
        pairs=tuple(normalized),
        camera0_count=camera0_count,
        camera1_count=camera1_count,
        camera0_unmatched_headers_ns=tuple(sorted(camera0_headers - camera1_headers)),
        camera1_unmatched_headers_ns=tuple(sorted(camera1_headers - camera0_headers)),
    )


def _median(values: Sequence[int]):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    numerator = ordered[middle - 1] + ordered[middle]
    return numerator // 2 if numerator % 2 == 0 else numerator / 2.0


def _nearest_rank(values: Sequence[int], numerator: int, denominator: int) -> Optional[int]:
    """Return the exact nearest-rank quantile ``ceil(n*p)``."""

    if not values:
        return None
    if numerator <= 0 or numerator > denominator:
        raise ValueError("nearest-rank probability is outside (0, 1]")
    ordered = sorted(values)
    rank = (len(ordered) * numerator + denominator - 1) // denominator
    return ordered[rank - 1]


def _pair_identity_list(identities: Iterable[Tuple[int, int]]) -> List[List[int]]:
    return [[camera0, camera1] for camera0, camera1 in sorted(identities)]


def _pair_set_digest(identities: Iterable[Tuple[int, int]]) -> str:
    digest = hashlib.sha256()
    digest.update(b"schurvio.icra27.kaist_pair_identity_set.v1\0")
    for camera0, camera1 in sorted(identities):
        digest.update(struct.pack("<QQ", camera0, camera1))
    return digest.hexdigest()


def _selection_summary(pairs: Sequence[StereoPair]) -> Mapping[str, object]:
    if not pairs:
        return {
            "pair_count": 0,
            "first_camera0_header_stamp_ns": None,
            "first_camera1_header_stamp_ns": None,
            "last_camera0_header_stamp_ns": None,
            "last_camera1_header_stamp_ns": None,
            "record_skew_min_ns": None,
            "record_skew_median_ns": None,
            "record_skew_p95_ns": None,
            "record_skew_max_ns": None,
        }
    skews = [pair.record_skew_ns for pair in pairs]
    return {
        "pair_count": len(pairs),
        "first_camera0_header_stamp_ns": pairs[0].camera0_header_time_ns,
        "first_camera1_header_stamp_ns": pairs[0].camera1_header_time_ns,
        "last_camera0_header_stamp_ns": pairs[-1].camera0_header_time_ns,
        "last_camera1_header_stamp_ns": pairs[-1].camera1_header_time_ns,
        "record_skew_min_ns": min(skews),
        "record_skew_median_ns": _median(skews),
        "record_skew_p95_ns": _nearest_rank(skews, 95, 100),
        "record_skew_max_ns": max(skews),
    }


def build_census(
    messages: Sequence[FilteredMessage], bag_identity: Mapping[str, object],
    topic_identity: Mapping[str, object]
) -> Mapping[str, object]:
    """Build the complete deterministic, JSON-serializable census."""

    native = select_upstream_native(messages)
    exact = select_exact_header(messages)
    native_identities = {pair.identity for pair in native.pairs}
    exact_identities = {pair.identity for pair in exact.pairs}
    intersection = native_identities.intersection(exact_identities)
    native_only = native_identities - exact_identities
    exact_only = exact_identities - native_identities
    native_skews = [pair.record_skew_ns for pair in native.pairs]
    camera_message_count = exact.camera0_count + exact.camera1_count
    native_non_dispatch_count = camera_message_count - len(native.pairs)
    if native_non_dispatch_count != (
        native.used_index_outer_skip_count + native.no_pair_outer_skip_count
    ):
        raise CensusError("native outer-loop camera accounting is inconsistent")

    participant_indices: Set[int] = set()
    for pair in native.pairs:
        participant_indices.update(pair.identity)
    reused_candidates = {
        index: count for index, count in native.candidate_use_counts.items() if count > 1
    }
    accepted_at_or_above_nominal_limit = sum(
        pair.record_skew_ns >= NOMINAL_LIMIT_NS for pair in native.pairs
    )

    required_fields = {
        "source_camera0_count": exact.camera0_count,
        "source_camera1_count": exact.camera1_count,
        "exact_header_pair_count": len(exact.pairs),
        "camera0_unmatched_count": len(exact.camera0_unmatched_headers_ns),
        "camera1_unmatched_count": len(exact.camera1_unmatched_headers_ns),
        "u0_native_pair_count": len(native.pairs),
        "u0_native_skipped_count": native.no_pair_outer_skip_count,
        "u0_native_non_dispatch_camera_visit_count": native_non_dispatch_count,
        "u0_reused_candidate_count": len(reused_candidates),
        "u0_record_skew_min_ns": min(native_skews) if native_skews else None,
        "u0_record_skew_median_ns": _median(native_skews),
        "u0_record_skew_p95_ns": _nearest_rank(native_skews, 95, 100),
        "u0_record_skew_max_ns": max(native_skews) if native_skews else None,
        "s1_exact_pair_count": len(exact.pairs),
        # These five fields are observable only from an actual S1 attempt.
        "s1_queued_pair_count": None,
        "s1_processed_pair_count": None,
        "s1_frequency_thinned_pair_count": None,
        "s1_pending_pair_count": None,
        "pair_set_intersection_count": len(intersection),
        "u0_only_pair_count": len(native_only),
        "s1_only_pair_count": len(exact_only),
        "u0_first_selected_header_stamp_ns": (
            native.pairs[0].camera_timestamp_ns if native.pairs else None
        ),
        "u0_last_selected_header_stamp_ns": (
            native.pairs[-1].camera_timestamp_ns if native.pairs else None
        ),
        "s1_first_selected_header_stamp_ns": exact.pairs[0].camera_timestamp_ns,
        "s1_last_selected_header_stamp_ns": exact.pairs[-1].camera_timestamp_ns,
    }

    return {
        "schema": SCHEMA,
        "bag": dict(bag_identity),
        "topics": dict(topic_identity),
        "policy": {
            "filtered_view": "rosbag chronological view over the three bound topics",
            "u0_native": (
                "pinned upstream first later opposite-camera record time; "
                "accept when abs(binary64(toSec(candidate)-toSec(anchor))) < 0.02; "
                "used_index checked and erased only at outer-loop entry"
            ),
            "s1_exact_header": (
                "unique nonzero equal integer-nanosecond camera headers; "
                "dispatch at earlier filtered index"
            ),
            "pair_identity": (
                "[camera0_filtered_index,camera1_filtered_index] in the shared "
                "three-topic bag view"
            ),
            "u0_native_skipped_count": (
                "camera outer-loop visits that reach upstream's unable-to-find-pair "
                "branch; used_index skips are counted separately"
            ),
            "u0_record_skew": "absolute integer record-time difference of native callbacks",
            "median": "middle value, or arithmetic mean of the two middle values",
            "p95": "nearest-rank ceil(0.95*N)",
            "runtime_nulls": (
                "queue, processing, frequency-thinning, and pending counters are not "
                "inferable from bag metadata and must be filled from attempt logs"
            ),
        },
        "census": required_fields,
        "selection_bounds": {
            "u0_native": _selection_summary(native.pairs),
            "s1_exact_header": _selection_summary(exact.pairs),
        },
        "exact_header_unmatched": {
            "camera0_header_stamps_ns": list(exact.camera0_unmatched_headers_ns),
            "camera1_header_stamps_ns": list(exact.camera1_unmatched_headers_ns),
        },
        "pair_sets": {
            "u0_native": {
                "count": len(native_identities),
                "sha256": _pair_set_digest(native_identities),
                "identities": _pair_identity_list(native_identities),
            },
            "s1_exact_header": {
                "count": len(exact_identities),
                "sha256": _pair_set_digest(exact_identities),
                "identities": _pair_identity_list(exact_identities),
            },
            "intersection": {
                "count": len(intersection),
                "sha256": _pair_set_digest(intersection),
                "identities": _pair_identity_list(intersection),
            },
            "u0_only": {
                "count": len(native_only),
                "sha256": _pair_set_digest(native_only),
                "identities": _pair_identity_list(native_only),
            },
            "s1_only": {
                "count": len(exact_only),
                "sha256": _pair_set_digest(exact_only),
                "identities": _pair_identity_list(exact_only),
            },
        },
        "u0_native_diagnostics": {
            "used_index_outer_skip_count": native.used_index_outer_skip_count,
            "no_pair_outer_skip_count": native.no_pair_outer_skip_count,
            "non_dispatch_camera_outer_visit_count": native_non_dispatch_count,
            "unique_participating_camera_message_count": len(participant_indices),
            "unique_unpaired_camera_message_count": camera_message_count - len(participant_indices),
            "reused_candidate_message_count": len(reused_candidates),
            "candidate_reuse_occurrence_count": sum(
                count - 1 for count in reused_candidates.values()
            ),
            "reused_candidate_filtered_indices": [
                {"filtered_index": index, "use_count": count}
                for index, count in sorted(reused_candidates.items())
            ],
            "accepted_record_skew_at_or_above_nominal_20ms_count": (
                accepted_at_or_above_nominal_limit
            ),
            "residual_used_index_count": len(native.residual_used_indices),
        },
    }


def _serialized_bytes(raw_message) -> bytes:
    # ROS Noetic returns (datatype, data, md5, position, PythonType).  Some ROS
    # distributions/documentation wrap the data tuple one level deeper.
    if not isinstance(raw_message, tuple):
        raise CensusError("rosbag raw message is not a tuple")
    if len(raw_message) >= 2 and isinstance(raw_message[1], (bytes, bytearray)):
        return bytes(raw_message[1])
    if (
        len(raw_message) >= 2
        and isinstance(raw_message[1], tuple)
        and raw_message[1]
        and isinstance(raw_message[1][0], (bytes, bytearray))
    ):
        return bytes(raw_message[1][0])
    raise CensusError("unsupported rosbag raw-message layout")


def _raw_datatype_and_md5(raw_message) -> Tuple[str, str]:
    if len(raw_message) >= 2 and isinstance(raw_message[1], tuple):
        nested = raw_message[1]
        if len(nested) >= 2:
            return raw_message[0], nested[1]
    if (
        len(raw_message) >= 3
        and isinstance(raw_message[0], str)
        and isinstance(raw_message[2], str)
    ):
        return raw_message[0], raw_message[2]
    raise CensusError("cannot extract datatype/MD5 from rosbag raw message")


def parse_serialized_header_stamp_ns(serialized_message: bytes) -> int:
    """Read ``std_msgs/Header.stamp`` from a ROS1 serialized message."""

    if len(serialized_message) < 12:
        raise CensusError("serialized camera message is shorter than Header stamp")
    seconds, nanoseconds = struct.unpack_from("<II", serialized_message, 4)
    if nanoseconds >= NANOSECONDS_PER_SECOND:
        raise CensusError("serialized camera header nanoseconds are invalid")
    return seconds * NANOSECONDS_PER_SECOND + nanoseconds


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def read_bag(
    bag_path: Path, camera0_topic: str, camera1_topic: str, imu_topic: str
) -> Tuple[List[FilteredMessage], Mapping[str, object], Mapping[str, object]]:
    """Read the exact three-topic metadata view from a ROS1 bag."""

    try:
        import rosbag  # type: ignore
    except ImportError as error:
        raise CensusError(
            "ROS1 rosbag Python bindings are unavailable; source the ROS environment"
        ) from error

    resolved = bag_path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise CensusError("bag path is not a regular file: {}".format(resolved))
    role_by_topic = {
        camera0_topic: KIND_CAMERA0,
        camera1_topic: KIND_CAMERA1,
        imu_topic: KIND_IMU,
    }
    if len(role_by_topic) != 3:
        raise CensusError("camera0, camera1, and IMU topics must be distinct")

    messages: List[FilteredMessage] = []
    observations: Dict[str, Dict[str, object]] = {
        role: {
            "name": topic,
            "message_count": 0,
            "datatypes": set(),
            "md5s": set(),
            "first_record_stamp_ns": None,
            "last_record_stamp_ns": None,
            "first_header_stamp_ns": None,
            "last_header_stamp_ns": None,
        }
        for topic, role in role_by_topic.items()
    }
    format_version = None
    compression = None
    with rosbag.Bag(str(resolved), "r") as bag:
        format_version = getattr(bag, "version", None)
        compression = getattr(bag, "compression", None)
        for topic, raw_message, record_time in bag.read_messages(
            topics=list(role_by_topic), raw=True
        ):
            if topic not in role_by_topic:
                raise CensusError("filtered rosbag view returned an unknown topic")
            role = role_by_topic[topic]
            record_time_ns = int(record_time.secs) * NANOSECONDS_PER_SECOND + int(
                record_time.nsecs
            )
            datatype, md5sum = _raw_datatype_and_md5(raw_message)
            observed = observations[role]
            observed["message_count"] = int(observed["message_count"]) + 1
            observed["datatypes"].add(datatype)  # type: ignore[union-attr]
            observed["md5s"].add(md5sum)  # type: ignore[union-attr]
            if observed["first_record_stamp_ns"] is None:
                observed["first_record_stamp_ns"] = record_time_ns
            observed["last_record_stamp_ns"] = record_time_ns

            if role in CAMERA_KINDS:
                serialized = _serialized_bytes(raw_message)
                header_time_ns = parse_serialized_header_stamp_ns(serialized)
                if observed["first_header_stamp_ns"] is None:
                    observed["first_header_stamp_ns"] = header_time_ns
                observed["last_header_stamp_ns"] = header_time_ns
                messages.append(FilteredMessage(role, record_time_ns, header_time_ns))
            else:
                messages.append(FilteredMessage(role, record_time_ns))

    expected = {
        KIND_CAMERA0: (IMAGE_DATATYPE, IMAGE_MD5),
        KIND_CAMERA1: (IMAGE_DATATYPE, IMAGE_MD5),
        KIND_IMU: (IMU_DATATYPE, IMU_MD5),
    }
    topic_identity: Dict[str, object] = {}
    for role in (KIND_CAMERA0, KIND_CAMERA1, KIND_IMU):
        observed = observations[role]
        count = int(observed["message_count"])
        if count == 0:
            raise CensusError("required topic is empty or absent: {}".format(observed["name"]))
        datatypes = sorted(observed.pop("datatypes"))  # type: ignore[arg-type]
        md5s = sorted(observed.pop("md5s"))  # type: ignore[arg-type]
        expected_datatype, expected_md5 = expected[role]
        if datatypes != [expected_datatype] or md5s != [expected_md5]:
            raise CensusError(
                "{} identity differs: datatypes={} md5s={}".format(
                    role, datatypes, md5s
                )
            )
        observed["datatype"] = datatypes[0]
        observed["md5"] = md5s[0]
        topic_identity[role] = observed

    _validate_filtered_view(messages)
    bag_identity = {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": _sha256_file(resolved),
        "rosbag_version": format_version,
        "compression": compression,
        "filtered_message_count": len(messages),
        "first_filtered_record_stamp_ns": messages[0].record_time_ns,
        "last_filtered_record_stamp_ns": messages[-1].record_time_ns,
    }
    return messages, bag_identity, topic_identity


def _write_json(value: Mapping[str, object], output: Optional[Path]) -> None:
    encoded = json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    if output is None:
        sys.stdout.write(encoded)
        return
    target = output.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}.".format(target.name), dir=str(target.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, str(target))
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", required=True, type=Path, help="adapted KAIST ROS1 bag")
    parser.add_argument("--camera0-topic", default=CAMERA0_TOPIC)
    parser.add_argument("--camera1-topic", default=CAMERA1_TOPIC)
    parser.add_argument("--imu-topic", default=IMU_TOPIC)
    parser.add_argument(
        "--output", type=Path, help="write JSON atomically here instead of stdout"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        messages, bag_identity, topic_identity = read_bag(
            args.bag, args.camera0_topic, args.camera1_topic, args.imu_topic
        )
        census = build_census(messages, bag_identity, topic_identity)
        _write_json(census, args.output)
    except (CensusError, OSError, ValueError) as error:
        print("kaist_pairing_census: error: {}".format(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
