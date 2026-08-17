#!/usr/bin/python3.8
"""BLACKOUT-1 replay-layer camera-frame masking (docs/icra27/BLACKOUT_PREREG.md).

The frozen serial estimator reads its rosbag directly, so the replay input IS
the bag.  This tool writes a masked copy of a frozen bag: every message is
copied raw (same topic, same connection header, same record time, same
serialized bytes) except messages on the camera topics whose HEADER stamp lies
in the closed interval [t_b, t_b + k]; those are dropped.  IMU and every other
topic are untouched.  k == 0 masks nothing (empty interval; the masking-
inertness check).  Estimator source is never touched (DECISIONS D4).

Sub-commands
  scan   -- list camera header stamps inside the frozen replay view (used by
            the injection-point table and by NOT_RUNNABLE checks)
  mask   -- write the masked bag and its manifest (JSON)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_MANIFEST = "schurvio.icra27.blackout.mask_manifest.v1"
SCHEMA_SCAN = "schurvio.icra27.blackout.camera_scan.v1"
NS = 1_000_000_000


class MaskError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {"path": str(path), "size_bytes": stat.st_size, "sha256": sha256_file(path)}


def header_stamp_ns(data: bytes) -> int:
    """std_msgs/Header is the first field of sensor_msgs/Image and Imu: seq, secs, nsecs."""
    if len(data) < 12:
        raise MaskError("message too short for a std_msgs/Header")
    secs, nsecs = struct.unpack_from("<II", data, 4)
    return secs * NS + nsecs


def _open_bag(path: Path):
    try:
        import rosbag  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise MaskError("ROS1 rosbag Python bindings are unavailable") from exc
    return rosbag, rosbag.Bag(str(path), "r")


def scan(bag_path: Path, camera_topics: Sequence[str], view_start_seconds: float) -> Dict[str, Any]:
    """Camera header stamps (ns) per topic, split by the frozen replay view [begin + view_start, end]."""
    rosbag, bag = _open_bag(bag_path)
    with bag:
        begin_ns = bag.get_start_time()
        # exact integer bounds as the C++ rosbag::View sees them
        first_record_ns = None
        last_record_ns = None
        per_topic: Dict[str, List[Tuple[int, int]]] = {topic: [] for topic in camera_topics}
        counts: Dict[str, int] = {}
        for topic, raw, t in bag.read_messages(raw=True):
            record_ns = t.secs * NS + t.nsecs
            if first_record_ns is None or record_ns < first_record_ns:
                first_record_ns = record_ns
            if last_record_ns is None or record_ns > last_record_ns:
                last_record_ns = record_ns
            counts[topic] = counts.get(topic, 0) + 1
            if topic in per_topic:
                per_topic[topic].append((record_ns, header_stamp_ns(raw[1])))
    if first_record_ns is None:
        raise MaskError("bag is empty")
    view_start_ns = first_record_ns + int(round(view_start_seconds * NS))
    result: Dict[str, Any] = {
        "schema": SCHEMA_SCAN,
        "bag": str(bag_path),
        "record_first_ns": first_record_ns,
        "record_last_ns": last_record_ns,
        "view_start_seconds": view_start_seconds,
        "view_start_record_ns": view_start_ns,
        "message_counts": counts,
        "topics": {},
    }
    for topic, rows in per_topic.items():
        rows.sort()
        in_view = [h for r, h in rows if r >= view_start_ns]
        result["topics"][topic] = {
            "count_total": len(rows),
            "count_in_view": len(in_view),
            "header_stamps_in_view_ns": sorted(in_view),
            "first_header_in_view_ns": min(in_view) if in_view else None,
            "last_header_in_view_ns": max(in_view) if in_view else None,
        }
    return result


def mask(
    source: Path,
    output: Path,
    manifest_path: Path,
    camera_topics: Sequence[str],
    imu_topic: str,
    mask_start_ns: int,
    mask_seconds: float,
    labels: Dict[str, Any],
) -> Dict[str, Any]:
    if output.exists() or manifest_path.exists():
        raise MaskError("refusing to overwrite an existing masked bag or manifest")
    if mask_seconds < 0 or mask_start_ns < 0:
        raise MaskError("mask parameters must be non-negative")
    mask_len_ns = int(round(mask_seconds * NS))
    mask_end_ns = mask_start_ns + mask_len_ns
    active = mask_len_ns > 0
    started = time.monotonic()
    source_identity = file_identity(source)
    rosbag, bag = _open_bag(source)
    kept: Dict[str, int] = {}
    dropped: Dict[str, int] = {}
    first_dropped: Dict[str, Optional[Tuple[int, int]]] = {t: None for t in camera_topics}
    last_dropped: Dict[str, Optional[Tuple[int, int]]] = {t: None for t in camera_topics}
    last_pre: Dict[str, Optional[int]] = {t: None for t in camera_topics}
    first_post: Dict[str, Optional[int]] = {t: None for t in camera_topics}
    total_in = 0
    total_out = 0
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".partial")
    if tmp.exists():
        tmp.unlink()
    with bag, rosbag.Bag(str(tmp), "w") as out:
        for topic, raw, t, header in bag.read_messages(raw=True, return_connection_header=True):
            total_in += 1
            if topic in first_dropped:
                stamp = header_stamp_ns(raw[1])
                if active and mask_start_ns <= stamp <= mask_end_ns:
                    dropped[topic] = dropped.get(topic, 0) + 1
                    record_ns = t.secs * NS + t.nsecs
                    if first_dropped[topic] is None or stamp < first_dropped[topic][0]:
                        first_dropped[topic] = (stamp, record_ns)
                    if last_dropped[topic] is None or stamp > last_dropped[topic][0]:
                        last_dropped[topic] = (stamp, record_ns)
                    continue
                if stamp < mask_start_ns and (last_pre[topic] is None or stamp > last_pre[topic]):
                    last_pre[topic] = stamp
                if stamp > mask_end_ns and (first_post[topic] is None or stamp < first_post[topic]):
                    first_post[topic] = stamp
            out.write(topic, raw, t, raw=True, connection_header=header)
            kept[topic] = kept.get(topic, 0) + 1
            total_out += 1
    os.replace(str(tmp), str(output))
    masked_identity = file_identity(output)
    manifest = {
        "schema": SCHEMA_MANIFEST,
        "tool": {"path": str(Path(__file__).resolve()), "sha256": sha256_file(Path(__file__).resolve())},
        "labels": dict(labels),
        "source_bag": source_identity,
        "masked_bag": masked_identity,
        "camera_topics": list(camera_topics),
        "imu_topic": imu_topic,
        "mask": {
            "active": active,
            "start_header_stamp_ns": mask_start_ns,
            "start_header_stamp_s": mask_start_ns / NS,
            "seconds": mask_seconds,
            "length_ns": mask_len_ns,
            "end_header_stamp_ns": mask_end_ns,
            "end_header_stamp_s": mask_end_ns / NS,
            "interval": "closed [start, end] on message header stamps; k=0 masks nothing",
        },
        "messages_in": total_in,
        "messages_out": total_out,
        "kept_per_topic": kept,
        "dropped_per_topic": {t: dropped.get(t, 0) for t in camera_topics},
        "imu_messages_kept": kept.get(imu_topic, 0),
        "imu_messages_dropped": 0,
        "non_camera_messages_dropped": total_in - total_out - sum(dropped.values()),
        "first_dropped_per_topic": {
            t: (None if v is None else {"header_stamp_ns": v[0], "record_ns": v[1]}) for t, v in first_dropped.items()
        },
        "last_dropped_per_topic": {
            t: (None if v is None else {"header_stamp_ns": v[0], "record_ns": v[1]}) for t, v in last_dropped.items()
        },
        "last_pre_mask_header_stamp_ns": last_pre,
        "first_post_mask_header_stamp_ns": first_post,
        "wall_seconds": time.monotonic() - started,
    }
    if manifest["non_camera_messages_dropped"] != 0:
        raise MaskError("non-camera messages were dropped; masking is not camera-only")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(manifest, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(manifest_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="action", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--bag", required=True, type=Path)
    s.add_argument("--camera-topics", required=True)
    s.add_argument("--view-start-seconds", type=float, default=0.0)
    s.add_argument("--output", required=True, type=Path)
    m = sub.add_parser("mask")
    m.add_argument("--source", required=True, type=Path)
    m.add_argument("--output", required=True, type=Path)
    m.add_argument("--manifest", required=True, type=Path)
    m.add_argument("--camera-topics", required=True)
    m.add_argument("--imu-topic", required=True)
    m.add_argument("--mask-start-ns", required=True, type=int)
    m.add_argument("--mask-seconds", required=True, type=float)
    m.add_argument("--label", action="append", default=[], help="key=value labels copied into the manifest")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "scan":
            result = scan(args.bag, args.camera_topics.split(","), args.view_start_seconds)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, allow_nan=False, indent=1, sort_keys=True) + "\n")
            summary = {t: {k: v for k, v in d.items() if k != "header_stamps_in_view_ns"} for t, d in result["topics"].items()}
            print(json.dumps(summary, sort_keys=True))
            return 0
        labels = {}
        for item in args.label:
            key, _, val = item.partition("=")
            labels[key] = val
        manifest = mask(
            args.source, args.output, args.manifest, args.camera_topics.split(","), args.imu_topic,
            args.mask_start_ns, args.mask_seconds, labels,
        )
        print(json.dumps({k: manifest[k] for k in ("dropped_per_topic", "kept_per_topic", "masked_bag", "wall_seconds")}, sort_keys=True))
        return 0
    except (MaskError, OSError, ValueError) as exc:
        print("BLACKOUT_MASK_ERROR: {}".format(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
