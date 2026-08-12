#!/usr/bin/python3
"""Consolidate frozen KAIST VIO Session-0.5 evidence.

The consolidator is deliberately a pure, deterministic boundary around
already-produced evidence.  It performs no ROS replay and no evaluation.  It
verifies every referenced file against an explicit input-root allowlist,
validates the complete ordered eleven-sequence campaign, derives the
compatibility classification, and atomically emits the fixed Session-0.5 data
artifacts.  Existing outputs are never overwritten.

Input schemas
-------------

``download-evidence.json``::

    {
      "schema": "turnsafe.kaist_vio.download_evidence.v1",
      "retrieval_utc": "...",
      "extraction_utc": "...",
      "source_urls": [
        {"url": "https://...", "route": "primary_archive",
         "attempted": true, "succeeded": true}
      ],
      "fallback_status": "not_used",
      "http": {...},
      "archive": {"path": "/abs/...zip", "size_bytes": 1,
                  "sha256": "...", "unzip_test_passed": true},
      "official_metadata": {
        "repository_url": "https://github.com/url-kaist/kaistviodataset.git",
        "commit_sha": "...", "retrieval_utc": "...",
        "license": {...},
        "files": {"README.md": {"path": "/abs/...", "sha256": "..."}, ...}
      },
      "failures_and_retries": []
    }

``evidence-index.json``::

    {
      "schema": "turnsafe.kaist_vio_reports.evidence_index.v1",
      "adapter_required": true,
      "baseline_source_identity": {
        "path": "/abs/.../baseline-source.json", "size_bytes": 1,
        "sha256": "..."
      },
      "mh01_parity": {"passed": true, "expected_digest": "...",
                      "observed_digest": "...",
                      "parity_evidence": {
                        "path": "/abs/.../parity-evidence.json",
                        "size_bytes": 1, "sha256": "..."
                      }},
      "unsupported_reasons": [],
      "engineering_blockers": [],
      "sequences": [
        {"sequence": "rotation/rotation_fast.bag",
         "source_bag": {"path": "/abs/...", "size_bytes": 1,
                        "sha256": "..."},
         "adapted_bag": {"path": "/abs/...", "size_bytes": 1,
                         "sha256": "..."},
         "adapter_report": "/abs/...json",
         "rosbag_info": "/abs/...json-or-yaml",
         "campaign_manifest": "/abs/...json"}, ...]
    }

``baseline-source.json``::

    {
      "schema": "turnsafe.kaist_vio_reports.baseline_source_identity.v1",
      "source_commit": "<40 lowercase hex>",
      "source_tree": "<40 lowercase hex>",
      "tracked_tree_clean": true,
      "source_files": {
        "config": {"path": "/abs/...", "size_bytes": 1, "sha256": "..."},
        "kalibr_imu_chain": {...}, "kalibr_imucam_chain": {...},
        "launch": {...}, "adapter": {...}, "trajectory_converter": {...}
      },
      "estimator_binary": {"path": "/abs/...", "size_bytes": 1,
                           "sha256": "..."}
    }

``parity-evidence.json`` binds two ``turnsafe.baseline_digest.v1`` JSON file
identities and byte comparisons for ``state``, ``deviation``, ``trajectory``,
and ``timing``.  State/deviation/trajectory must compare byte-identically;
timing identities are still verified but timing bytes may differ.

All paths, including the two input documents, must resolve below a repeated
``--input-root``.  Path components named ``scripts/cp2`` or containing
``holdout``/``private`` are rejected before any file is opened.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse


DOWNLOAD_INPUT_SCHEMA = "turnsafe.kaist_vio.download_evidence.v1"
INDEX_SCHEMA = "turnsafe.kaist_vio_reports.evidence_index.v1"
DOWNLOAD_OUTPUT_SCHEMA = "turnsafe.kaist_vio.download_manifest.v1"
SEQUENCE_OUTPUT_SCHEMA = "turnsafe.kaist_vio.sequence_manifest.v1"
ADAPTATION_SCHEMA = "turnsafe.kaist_vio_adapter.adaptation.v1"
BASELINE_SOURCE_SCHEMA = (
    "turnsafe.kaist_vio_reports.baseline_source_identity.v1"
)
PARITY_EVIDENCE_SCHEMA = "turnsafe.kaist_vio_reports.mh01_parity_evidence.v1"
BASELINE_DIGEST_SCHEMA = "turnsafe.baseline_digest.v1"
OFFICIAL_METADATA_COMMIT = "ae672591bab119651be1aa9fc3db5af3e599f49a"

REQUIRED_BASELINE_SOURCE_FILES: Tuple[str, ...] = (
    "adapter",
    "config",
    "kalibr_imu_chain",
    "kalibr_imucam_chain",
    "launch",
    "trajectory_converter",
)

PARITY_DIGEST_INPUTS: Mapping[str, str] = {
    "state": "state_estimate.txt",
    "deviation": "state_deviation.txt",
    "trajectory": "trajectory_tum.txt",
    "timing": "timing_openvins.csv",
}

ORDERED_SEQUENCES: Tuple[str, ...] = (
    "rotation/rotation_fast.bag",
    "rotation/rotation.bag",
    "circle/circle_head.bag",
    "infinite/infinite_head.bag",
    "square/square_head.bag",
    "circle/circle_fast.bag",
    "infinite/infinite_fast.bag",
    "square/square_fast.bag",
    "circle/circle.bag",
    "infinite/infinite.bag",
    "square/square.bag",
)

RAW_SOURCE_TOPICS: Mapping[str, str] = {
    "/camera/infra1/image_rect_raw": "sensor_msgs/Image",
    "/camera/infra2/image_rect_raw": "sensor_msgs/Image",
    "/mavros/imu/data": "sensor_msgs/Imu",
    "/pose_transformed": "geometry_msgs/PoseStamped",
}
COMPRESSED_SOURCE_TOPICS: Mapping[str, str] = {
    "/camera/infra1/image_rect_raw/compressed": "sensor_msgs/CompressedImage",
    "/camera/infra2/image_rect_raw/compressed": "sensor_msgs/CompressedImage",
    "/mavros/imu/data": "sensor_msgs/Imu",
    "/pose_transformed": "geometry_msgs/PoseStamped",
}
SOURCE_TOPICS_BY_PROFILE: Mapping[str, Mapping[str, str]] = {
    "official_raw": RAW_SOURCE_TOPICS,
    "documented_compressed": COMPRESSED_SOURCE_TOPICS,
}
# The downloaded official archive uses the raw profile.  Retain this alias for
# fixture consumers while validating either declared adapter profile below.
SOURCE_TOPICS = RAW_SOURCE_TOPICS
ADAPTED_TOPICS: Mapping[str, str] = {
    "/turnsafe/kaist/infra1/image_raw": "sensor_msgs/Image",
    "/turnsafe/kaist/infra2/image_raw": "sensor_msgs/Image",
    "/mavros/imu/data": "sensor_msgs/Imu",
}

EXPECTED_STREAM_KINDS: Mapping[str, str] = {
    "/camera/infra1/image_rect_raw": "camera0",
    "/camera/infra2/image_rect_raw": "camera1",
    "/camera/infra1/image_rect_raw/compressed": "camera0",
    "/camera/infra2/image_rect_raw/compressed": "camera1",
    "/turnsafe/kaist/infra1/image_raw": "camera0",
    "/turnsafe/kaist/infra2/image_raw": "camera1",
    "/mavros/imu/data": "imu",
    "/pose_transformed": "ground_truth",
}

CLASSIFICATIONS: Tuple[str, ...] = (
    "RUNNABLE",
    "RUNNABLE_WITH_DECLARED_ADAPTER",
    "BLOCKED_ENGINEERING",
    "UNSUPPORTED",
)

ALLOWED_NETWORK_HOSTS = {
    "urserver.kaist.ac.kr",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "urobot.kaist.ac.kr",
    "huggingface.co",
    "cdn-lfs.huggingface.co",
}

PRIMARY_ARCHIVE_URL = (
    "https://urserver.kaist.ac.kr/publicdata/KAIST_VIO_Dataset/"
    "kaist_vio_dataset.zip"
)

REQUIRED_METADATA_FILES: Tuple[str, ...] = (
    "README.md",
    "License",
    "config/cam-imu.yaml",
    "config/imu-params.yaml",
    "config/trans-mat.yaml",
)

ARTIFACT_OUTPUT_NAMES: Mapping[str, str] = {
    "download": "KAIST_DOWNLOAD_MANIFEST.json",
    "inventory": "KAIST_BAG_INVENTORY.csv",
    "compatibility": "KAIST_COMPATIBILITY_REPORT.md",
    "baseline": "KAIST_BASELINE_RESULTS.csv",
    "sequences": "KAIST_SEQUENCE_MANIFEST.json",
}
DATASET_OUTPUT_NAMES: Mapping[str, str] = {
    "download": "DOWNLOAD_MANIFEST.json",
    "inventory": "BAG_INVENTORY.csv",
}

HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class ReportError(RuntimeError):
    """A fail-closed evidence or output error."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ReportError("{} must be a JSON object".format(label))
    return value


def _list(value: Any, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise ReportError("{} must be a JSON array".format(label))
    return value


def _string(value: Any, label: str, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise ReportError("{} must be {}string".format(label, "a nonempty " if nonempty else "a "))
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ReportError("{} must be a boolean".format(label))
    return value


def _integer(value: Any, label: str, minimum: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ReportError("{} must be an integer".format(label))
    if minimum is not None and value < minimum:
        raise ReportError("{} must be at least {}".format(label, minimum))
    return value


def _finite_number(value: Any, label: str, minimum: Optional[float] = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReportError("{} must be numeric".format(label))
    result = float(value)
    if not math.isfinite(result):
        raise ReportError("{} must be finite".format(label))
    if minimum is not None and result < minimum:
        raise ReportError("{} must be at least {}".format(label, minimum))
    return result


def _sha256_text(value: Any, label: str) -> str:
    result = _string(value, label)
    if not HEX_SHA256.fullmatch(result):
        raise ReportError("{} is not a lowercase SHA-256".format(label))
    return result


def _git_sha(value: Any, label: str) -> str:
    result = _string(value, label)
    if not HEX_GIT_SHA.fullmatch(result):
        raise ReportError("{} is not a lowercase 40-character Git SHA".format(label))
    return result


def _json_bytes(value: Any) -> bytes:
    _assert_data_domain(value, "output")
    return (
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _compact_json(value: Any) -> str:
    _assert_data_domain(value, "CSV value")
    return json.dumps(
        value, allow_nan=False, separators=(",", ":"), sort_keys=True
    )


def _reject_json_constant(value: str) -> None:
    raise ReportError("JSON contains forbidden non-finite constant {}".format(value))


def _assert_data_domain(value: Any, label: str) -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ReportError("{} contains a non-finite number".format(label))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _assert_data_domain(item, "{}[{}]".format(label, index))
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ReportError("{} contains a non-string object key".format(label))
            _assert_data_domain(item, "{}.{}".format(label, key))
        return
    raise ReportError(
        "{} contains unsupported value type {}".format(label, type(value).__name__)
    )


def _forbidden_path(path: Path) -> Optional[str]:
    lowered = [part.lower() for part in path.parts]
    for part in lowered:
        if "holdout" in part or "private" in part:
            return "private/holdout path component"
    for first, second in zip(lowered, lowered[1:]):
        if first == "scripts" and second == "cp2":
            return "scripts/cp2 path"
    return None


def _within(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            pass
    return False


def _resolve_roots(values: Sequence[Path]) -> Tuple[Path, ...]:
    if not values:
        raise ReportError("at least one --input-root is required")
    roots: List[Path] = []
    for value in values:
        reason = _forbidden_path(value)
        if reason:
            raise ReportError("input root {} is forbidden: {}".format(value, reason))
        try:
            root = value.resolve(strict=True)
        except OSError as exc:
            raise ReportError("input root cannot be resolved: {}: {}".format(value, exc)) from exc
        if not root.is_dir():
            raise ReportError("input root is not a directory: {}".format(root))
        reason = _forbidden_path(root)
        if reason:
            raise ReportError("input root {} is forbidden: {}".format(root, reason))
        if root not in roots:
            roots.append(root)
    return tuple(sorted(roots, key=str))


def _input_path(value: Any, label: str, roots: Sequence[Path]) -> Path:
    text = _string(value, label)
    candidate = Path(text)
    if not candidate.is_absolute():
        raise ReportError("{} must be an absolute path".format(label))
    reason = _forbidden_path(candidate)
    if reason:
        raise ReportError("{} is forbidden: {}".format(label, reason))
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ReportError("{} cannot be resolved: {}".format(label, exc)) from exc
    reason = _forbidden_path(resolved)
    if reason:
        raise ReportError("{} resolves to a forbidden path: {}".format(label, reason))
    if not resolved.is_file():
        raise ReportError("{} is not a regular file: {}".format(label, resolved))
    if not _within(resolved, roots):
        raise ReportError("{} is outside every --input-root: {}".format(label, resolved))
    return resolved


def _read_json_path(path: Path, label: str, roots: Sequence[Path]) -> Mapping[str, Any]:
    resolved = _input_path(str(path), label, roots)
    try:
        with resolved.open("r", encoding="utf-8") as stream:
            value = json.load(stream, parse_constant=_reject_json_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReportError("{} is not readable JSON: {}".format(label, exc)) from exc
    _assert_data_domain(value, label)
    return _mapping(value, label)


def _read_info_path(path: Path, label: str, roots: Sequence[Path]) -> Mapping[str, Any]:
    resolved = _input_path(str(path), label, roots)
    try:
        text = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReportError("{} cannot be read: {}".format(label, exc)) from exc
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise ReportError(
                "{} is not JSON and PyYAML is unavailable for rosbag YAML".format(label)
            ) from exc
        try:
            value = yaml.safe_load(text)
        except Exception as exc:
            raise ReportError("{} is not valid rosbag YAML: {}".format(label, exc)) from exc
    _assert_data_domain(value, label)
    return _mapping(value, label)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(8 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class _IdentityVerifier:
    def __init__(self, roots: Sequence[Path]) -> None:
        self.roots = tuple(roots)
        self._cache: Dict[Path, Tuple[int, str]] = {}

    def verify(
        self, value: Any, label: str, minimum_size: int = 1
    ) -> Dict[str, Any]:
        identity = _mapping(value, label)
        path = _input_path(identity.get("path"), label + ".path", self.roots)
        expected_size = _integer(
            identity.get("size_bytes"), label + ".size_bytes", minimum_size
        )
        expected_hash = _sha256_text(identity.get("sha256"), label + ".sha256")
        if path not in self._cache:
            try:
                size = path.stat().st_size
                digest = _sha256_file(path)
            except OSError as exc:
                raise ReportError("unable to verify {}: {}".format(label, exc)) from exc
            self._cache[path] = (size, digest)
        actual_size, actual_hash = self._cache[path]
        if actual_size != expected_size:
            raise ReportError(
                "{} size mismatch: evidence {}, actual {}".format(
                    label, expected_size, actual_size
                )
            )
        if actual_hash != expected_hash:
            raise ReportError(
                "{} SHA-256 mismatch: evidence {}, actual {}".format(
                    label, expected_hash, actual_hash
                )
            )
        return {"path": str(path), "size_bytes": actual_size, "sha256": actual_hash}

    def identify(self, path: Path, label: str) -> Dict[str, Any]:
        resolved = _input_path(str(path), label, self.roots)
        if resolved not in self._cache:
            try:
                self._cache[resolved] = (resolved.stat().st_size, _sha256_file(resolved))
            except OSError as exc:
                raise ReportError("unable to identify {}: {}".format(label, exc)) from exc
        size, digest = self._cache[resolved]
        return {"path": str(resolved), "size_bytes": size, "sha256": digest}

    def verify_record(
        self, value: Any, label: str, require_size: bool = True
    ) -> Dict[str, Any]:
        record = _mapping(value, label)
        path = _input_path(record.get("path"), label + ".path", self.roots)
        actual = self.identify(path, label + ".path")
        expected_hash = _sha256_text(record.get("sha256"), label + ".sha256")
        if expected_hash != actual["sha256"]:
            raise ReportError("{} SHA-256 disagrees with the referenced file".format(label))
        if require_size:
            expected_size = _integer(record.get("size_bytes"), label + ".size_bytes", 1)
            if expected_size != actual["size_bytes"]:
                raise ReportError("{} size disagrees with the referenced file".format(label))
        return actual


def _identity_equal(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    return all(
        first.get(key) == second.get(key)
        for key in ("path", "size_bytes", "sha256")
    )


def _validate_baseline_source_identity(
    record: Any, verifier: _IdentityVerifier
) -> Dict[str, Any]:
    """Open and hash the common clean source/binary identity evidence."""

    evidence_identity = verifier.verify(
        record, "index.baseline_source_identity"
    )
    value = _read_json_path(
        Path(evidence_identity["path"]),
        "index.baseline_source_identity.document",
        verifier.roots,
    )
    if value.get("schema") != BASELINE_SOURCE_SCHEMA:
        raise ReportError(
            "baseline source identity schema must be {}".format(
                BASELINE_SOURCE_SCHEMA
            )
        )
    source_commit = _git_sha(
        value.get("source_commit"), "baseline_source.source_commit"
    )
    source_tree = _git_sha(
        value.get("source_tree"), "baseline_source.source_tree"
    )
    if _bool(
        value.get("tracked_tree_clean"), "baseline_source.tracked_tree_clean"
    ) is not True:
        raise ReportError("baseline source tracked tree was not clean")

    raw_files = _mapping(value.get("source_files"), "baseline_source.source_files")
    missing = sorted(set(REQUIRED_BASELINE_SOURCE_FILES) - set(raw_files))
    if missing:
        raise ReportError(
            "baseline source files are missing: {}".format(", ".join(missing))
        )
    source_files: Dict[str, Any] = {}
    seen_paths = set()
    for raw_name in sorted(raw_files):
        name = _string(raw_name, "baseline_source.source_files key")
        identity = verifier.verify(
            raw_files[raw_name],
            "baseline_source.source_files[{}]".format(name),
        )
        if identity["path"] in seen_paths:
            raise ReportError("baseline source files reuse a path")
        seen_paths.add(identity["path"])
        source_files[name] = identity

    estimator_binary = verifier.verify(
        value.get("estimator_binary"), "baseline_source.estimator_binary"
    )
    if not os.access(estimator_binary["path"], os.X_OK):
        raise ReportError("baseline source estimator binary is not executable")
    return {
        "schema": BASELINE_SOURCE_SCHEMA,
        "evidence_identity": evidence_identity,
        "source_commit": source_commit,
        "source_tree": source_tree,
        "tracked_tree_clean": True,
        "source_files": source_files,
        "estimator_binary": estimator_binary,
    }


def _validate_baseline_digest(
    record: Any, label: str, verifier: _IdentityVerifier
) -> Dict[str, Any]:
    identity = verifier.verify(record, label + ".identity")
    value = _read_json_path(
        Path(identity["path"]), label + ".document", verifier.roots
    )
    if value.get("schema") != BASELINE_DIGEST_SCHEMA:
        raise ReportError(
            "{} schema must be {}".format(label, BASELINE_DIGEST_SCHEMA)
        )
    combined = _sha256_text(
        value.get("combined_stable_sha256"),
        label + ".combined_stable_sha256",
    )
    source_sha = _git_sha(value.get("source_sha"), label + ".source_sha")
    raw_inputs = _mapping(
        value.get("input_file_sha256"), label + ".input_file_sha256"
    )
    expected_input_names = set(PARITY_DIGEST_INPUTS.values())
    if set(raw_inputs) != expected_input_names:
        raise ReportError(
            "{} input-file set must be exactly {}".format(
                label, ", ".join(sorted(expected_input_names))
            )
        )
    input_hashes = {
        name: _sha256_text(raw_inputs[name], "{}.input_file_sha256[{}]".format(label, name))
        for name in sorted(raw_inputs)
    }
    validation = _mapping(value.get("validation"), label + ".validation")
    for key in (
        "all_numeric_fields_finite",
        "row_counts_equal",
        "state_deviation_trajectory_timestamps_exact",
        "timestamps_strictly_increasing",
    ):
        if _bool(validation.get(key), "{}.validation.{}".format(label, key)) is not True:
            raise ReportError("{}.validation.{} did not pass".format(label, key))
    normalized = dict(value)
    normalized.update(
        {
            "combined_stable_sha256": combined,
            "source_sha": source_sha,
            "input_file_sha256": input_hashes,
        }
    )
    return {"identity": identity, "digest": normalized}


def _validate_mh01_parity_evidence(
    record: Any,
    expected_digest: str,
    observed_digest: str,
    baseline_source: Mapping[str, Any],
    verifier: _IdentityVerifier,
) -> Dict[str, Any]:
    """Verify both digest documents and every byte-comparison input."""

    evidence_identity = verifier.verify(record, "index.mh01_parity.parity_evidence")
    value = _read_json_path(
        Path(evidence_identity["path"]),
        "index.mh01_parity.parity_evidence.document",
        verifier.roots,
    )
    if value.get("schema") != PARITY_EVIDENCE_SCHEMA:
        raise ReportError(
            "MH_01 parity evidence schema must be {}".format(
                PARITY_EVIDENCE_SCHEMA
            )
        )
    expected = _validate_baseline_digest(
        value.get("expected_digest"), "mh01_parity.expected_digest", verifier
    )
    observed = _validate_baseline_digest(
        value.get("observed_digest"), "mh01_parity.observed_digest", verifier
    )
    if expected["digest"]["combined_stable_sha256"] != expected_digest:
        raise ReportError(
            "MH_01 expected digest document disagrees with index.expected_digest"
        )
    if observed["digest"]["combined_stable_sha256"] != observed_digest:
        raise ReportError(
            "MH_01 observed digest document disagrees with index.observed_digest"
        )
    if observed["digest"]["source_sha"] != baseline_source["source_commit"]:
        raise ReportError(
            "MH_01 observed digest source SHA disagrees with baseline source commit"
        )

    comparisons_raw = _mapping(
        value.get("byte_comparisons"), "mh01_parity.byte_comparisons"
    )
    if set(comparisons_raw) != set(PARITY_DIGEST_INPUTS):
        raise ReportError(
            "MH_01 byte comparisons must contain exactly state, deviation, "
            "trajectory, and timing"
        )
    comparisons: Dict[str, Any] = {}
    for name in ("state", "deviation", "trajectory", "timing"):
        comparison = _mapping(
            comparisons_raw[name], "mh01_parity.byte_comparisons.{}".format(name)
        )
        expected_file = verifier.verify(
            comparison.get("expected"),
            "mh01_parity.byte_comparisons.{}.expected".format(name),
        )
        observed_file = verifier.verify(
            comparison.get("observed"),
            "mh01_parity.byte_comparisons.{}.observed".format(name),
        )
        digest_input_name = PARITY_DIGEST_INPUTS[name]
        if (
            expected_file["sha256"]
            != expected["digest"]["input_file_sha256"][digest_input_name]
        ):
            raise ReportError(
                "MH_01 {} expected identity disagrees with digest input hash".format(
                    name
                )
            )
        if (
            observed_file["sha256"]
            != observed["digest"]["input_file_sha256"][digest_input_name]
        ):
            raise ReportError(
                "MH_01 {} observed identity disagrees with digest input hash".format(
                    name
                )
            )
        bytes_equal = expected_file["sha256"] == observed_file["sha256"]
        normalized = dict(comparison)
        normalized.update({"expected": expected_file, "observed": observed_file})
        if name == "timing":
            declared_equal = _bool(
                comparison.get("bytes_equal"),
                "mh01_parity.byte_comparisons.timing.bytes_equal",
            )
            if declared_equal != bytes_equal:
                raise ReportError(
                    "MH_01 timing byte-comparison flag disagrees with identities"
                )
            normalized["bytes_equal"] = declared_equal
        else:
            if _bool(
                comparison.get("passed"),
                "mh01_parity.byte_comparisons.{}.passed".format(name),
            ) is not True:
                raise ReportError(
                    "MH_01 {} byte comparison did not pass".format(name)
                )
            if not bytes_equal:
                raise ReportError(
                    "MH_01 {} byte-comparison identities differ".format(name)
                )
            normalized["passed"] = True
        comparisons[name] = normalized
    return {
        "schema": PARITY_EVIDENCE_SCHEMA,
        "evidence_identity": evidence_identity,
        "expected_digest": expected,
        "observed_digest": observed,
        "byte_comparisons": comparisons,
    }


def _validate_source_urls(value: Any) -> List[Dict[str, Any]]:
    entries = _list(value, "download.source_urls")
    if not entries:
        raise ReportError("download.source_urls must not be empty")
    result: List[Dict[str, Any]] = []
    primary_seen = False
    for index, raw in enumerate(entries):
        item = _mapping(raw, "download.source_urls[{}]".format(index))
        url = _string(item.get("url"), "download.source_urls[{}].url".format(index))
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_NETWORK_HOSTS:
            raise ReportError("download source URL is outside the Session-0.5 allowlist: {}".format(url))
        path = parsed.path
        authorized_path = (
            (parsed.hostname == "urserver.kaist.ac.kr" and path.startswith("/publicdata/KAIST_VIO_Dataset/"))
            or (
                parsed.hostname == "github.com"
                and (
                    path.rstrip("/") in (
                        "/url-kaist/kaistviodataset",
                        "/url-kaist/kaistviodataset.git",
                    )
                    or path.startswith("/url-kaist/kaistviodataset/")
                )
            )
            or (
                parsed.hostname == "api.github.com"
                and (
                    path.rstrip("/") == "/repos/url-kaist/kaistviodataset"
                    or path.startswith("/repos/url-kaist/kaistviodataset/")
                )
            )
            or (parsed.hostname == "raw.githubusercontent.com" and path.startswith("/url-kaist/kaistviodataset/"))
            or parsed.hostname == "urobot.kaist.ac.kr"
            or (parsed.hostname == "huggingface.co" and path.startswith("/datasets/zinuok/KAIST-VIO-Dataset"))
            or parsed.hostname == "cdn-lfs.huggingface.co"
        )
        if not authorized_path:
            raise ReportError("download source URL path is outside the Session-0.5 purpose allowlist: {}".format(url))
        attempted = _bool(item.get("attempted"), "download.source_urls[{}].attempted".format(index))
        succeeded = _bool(item.get("succeeded"), "download.source_urls[{}].succeeded".format(index))
        if succeeded and not attempted:
            raise ReportError("a successful source URL must have attempted=true")
        route = _string(item.get("route"), "download.source_urls[{}].route".format(index))
        if url == PRIMARY_ARCHIVE_URL:
            primary_seen = True
        normalized = dict(item)
        normalized.update({"url": url, "route": route, "attempted": attempted, "succeeded": succeeded})
        result.append(normalized)
    if not primary_seen:
        raise ReportError("download.source_urls omits the exact official primary archive URL")
    if not any(item["succeeded"] for item in result):
        raise ReportError("download.source_urls records no successful official route")
    return result


def _validate_download(
    value: Mapping[str, Any], verifier: _IdentityVerifier
) -> Dict[str, Any]:
    if value.get("schema") != DOWNLOAD_INPUT_SCHEMA:
        raise ReportError("download evidence schema must be {}".format(DOWNLOAD_INPUT_SCHEMA))
    retrieval_utc = _string(value.get("retrieval_utc"), "download.retrieval_utc")
    extraction_utc = _string(value.get("extraction_utc"), "download.extraction_utc")
    source_urls = _validate_source_urls(value.get("source_urls"))
    fallback_status = _string(value.get("fallback_status"), "download.fallback_status")
    http = dict(_mapping(value.get("http"), "download.http"))
    if not http:
        raise ReportError("download.http must record HTTP metadata or explicit null fields")
    header_capture = verifier.verify(
        {
            "path": http.get("header_capture_path"),
            "size_bytes": http.get("header_capture_size_bytes"),
            "sha256": http.get("header_capture_sha256"),
        },
        "download.http.header_capture",
    )
    download_header_capture = verifier.verify(
        http.get("download_header_capture"),
        "download.http.download_header_capture",
    )
    effective_url_capture = verifier.verify(
        http.get("effective_url_capture"),
        "download.http.effective_url_capture",
    )
    http.update(
        {
            "header_capture_path": header_capture["path"],
            "header_capture_size_bytes": header_capture["size_bytes"],
            "header_capture_sha256": header_capture["sha256"],
            "download_header_capture": download_header_capture,
            "effective_url_capture": effective_url_capture,
        }
    )

    archive_raw = _mapping(value.get("archive"), "download.archive")
    archive = verifier.verify(archive_raw, "download.archive")
    if not _bool(archive_raw.get("unzip_test_passed"), "download.archive.unzip_test_passed"):
        raise ReportError("download archive did not pass unzip validation")
    archive["unzip_test_passed"] = True
    for key in sorted(set(archive_raw) - {"path", "size_bytes", "sha256", "unzip_test_passed"}):
        archive[key] = archive_raw[key]

    metadata_raw = _mapping(value.get("official_metadata"), "download.official_metadata")
    repository_url = _string(
        metadata_raw.get("repository_url"), "download.official_metadata.repository_url"
    )
    if repository_url.rstrip("/") not in (
        "https://github.com/url-kaist/kaistviodataset",
        "https://github.com/url-kaist/kaistviodataset.git",
    ):
        raise ReportError("official metadata repository URL is not the authorized repository")
    commit_sha = _git_sha(metadata_raw.get("commit_sha"), "download.official_metadata.commit_sha")
    if commit_sha != OFFICIAL_METADATA_COMMIT:
        raise ReportError(
            "official metadata commit does not match the Session-0.5 contract"
        )
    metadata_retrieval = _string(
        metadata_raw.get("retrieval_utc"), "download.official_metadata.retrieval_utc"
    )
    license_value = dict(_mapping(metadata_raw.get("license"), "download.official_metadata.license"))
    if not license_value:
        raise ReportError("official metadata license evidence must not be empty")
    files_raw = _mapping(metadata_raw.get("files"), "download.official_metadata.files")
    missing = sorted(set(REQUIRED_METADATA_FILES) - set(files_raw))
    if missing:
        raise ReportError("official metadata files are missing: {}".format(", ".join(missing)))
    files: Dict[str, Any] = {}
    for name in sorted(files_raw):
        files[name] = verifier.verify(files_raw[name], "download.official_metadata.files[{}]".format(name))

    retries = _list(value.get("failures_and_retries"), "download.failures_and_retries")
    result = {
        "schema": DOWNLOAD_OUTPUT_SCHEMA,
        "retrieval_utc": retrieval_utc,
        "extraction_utc": extraction_utc,
        "source_urls": source_urls,
        "fallback_status": fallback_status,
        "http": http,
        "archive": archive,
        "official_metadata": {
            "repository_url": repository_url,
            "commit_sha": commit_sha,
            "retrieval_utc": metadata_retrieval,
            "license": license_value,
            "files": files,
        },
        "failures_and_retries": retries,
    }
    extra = sorted(
        set(value)
        - {
            "schema", "retrieval_utc", "extraction_utc", "source_urls",
            "fallback_status", "http", "archive", "official_metadata",
            "failures_and_retries",
        }
    )
    if extra:
        result["additional_evidence"] = {key: value[key] for key in extra}
    return result


def _validate_stream(
    stream: Mapping[str, Any], expected_type: str, expected_kind: str, label: str
) -> Dict[str, Any]:
    if stream.get("type") != expected_type:
        raise ReportError("{} type mismatch".format(label))
    if stream.get("kind") != expected_kind:
        raise ReportError("{} kind mismatch".format(label))
    count = _integer(stream.get("count"), label + ".count", 1)
    record = _mapping(stream.get("record_time"), label + ".record_time")
    header = _mapping(stream.get("header_time"), label + ".header_time")
    if record.get("strictly_increasing") is not True:
        raise ReportError("{} record timestamps are not strictly increasing".format(label))
    if header.get("strictly_increasing") is not True:
        raise ReportError("{} header timestamps are not strictly increasing".format(label))
    for timing_name, timing in (("record_time", record), ("header_time", header)):
        start = _integer(timing.get("start_ns"), "{}.{}.start_ns".format(label, timing_name), 1)
        end = _integer(timing.get("end_ns"), "{}.{}.end_ns".format(label, timing_name), 1)
        duration = _integer(timing.get("duration_ns"), "{}.{}.duration_ns".format(label, timing_name), 0)
        if end < start or duration != end - start:
            raise ReportError("{}.{} has inconsistent bounds/duration".format(label, timing_name))
        if count > 1:
            _finite_number(timing.get("rate_hz"), "{}.{}.rate_hz".format(label, timing_name), 0.0)
    return dict(stream)


def _streams_by_kind(
    streams: Mapping[str, Any], label: str
) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for topic, raw in streams.items():
        stream = _mapping(raw, "{} stream {}".format(label, topic))
        kind = _string(stream.get("kind"), "{} stream {} kind".format(label, topic))
        if kind in result:
            raise ReportError("{} contains duplicate stream kind {}".format(label, kind))
        result[kind] = stream
    return result


def _nonnegative_integer_list(value: Any, label: str) -> List[int]:
    items = _list(value, label)
    result = [
        _integer(item, "{}[{}]".format(label, index), 1)
        for index, item in enumerate(items)
    ]
    if result != sorted(set(result)):
        raise ReportError("{} must be strictly increasing and unique".format(label))
    return result


def _validate_exact_header_stereo(
    value: Any,
    streams_by_kind: Mapping[str, Mapping[str, Any]],
    label: str,
) -> Dict[str, Any]:
    stereo = _mapping(value, label)
    if stereo.get("policy") != "exact_header_stamp_no_filter_no_retime":
        raise ReportError("{} policy mismatch".format(label))
    pair_count = _integer(stereo.get("pair_count"), label + ".pair_count", 1)
    exact_pair_count = _integer(
        stereo.get("exact_header_pair_count"),
        label + ".exact_header_pair_count",
        1,
    )
    if pair_count != exact_pair_count:
        raise ReportError("{} pair-count fields disagree".format(label))
    camera0_count = _integer(
        stereo.get("camera0_count"), label + ".camera0_count", 1
    )
    camera1_count = _integer(
        stereo.get("camera1_count"), label + ".camera1_count", 1
    )
    if camera0_count != streams_by_kind["camera0"]["count"]:
        raise ReportError("{} camera0 count disagrees with its stream".format(label))
    if camera1_count != streams_by_kind["camera1"]["count"]:
        raise ReportError("{} camera1 count disagrees with its stream".format(label))

    unmatched0 = _nonnegative_integer_list(
        stereo.get("camera0_unmatched_header_stamps_ns"),
        label + ".camera0_unmatched_header_stamps_ns",
    )
    unmatched1 = _nonnegative_integer_list(
        stereo.get("camera1_unmatched_header_stamps_ns"),
        label + ".camera1_unmatched_header_stamps_ns",
    )
    unmatched0_count = _integer(
        stereo.get("camera0_unmatched_count"),
        label + ".camera0_unmatched_count",
        0,
    )
    unmatched1_count = _integer(
        stereo.get("camera1_unmatched_count"),
        label + ".camera1_unmatched_count",
        0,
    )
    if unmatched0_count != len(unmatched0) or unmatched1_count != len(unmatched1):
        raise ReportError("{} unmatched counts disagree with timestamp lists".format(label))
    if pair_count + unmatched0_count != camera0_count:
        raise ReportError("{} camera0 exact-pair accounting is inconsistent".format(label))
    if pair_count + unmatched1_count != camera1_count:
        raise ReportError("{} camera1 exact-pair accounting is inconsistent".format(label))

    threshold = _integer(
        stereo.get("record_skew_threshold_ns"),
        label + ".record_skew_threshold_ns",
        1,
    )
    if threshold != 20_000_000:
        raise ReportError("{} record-skew threshold is not 20 ms".format(label))
    above_threshold = _integer(
        stereo.get("matched_pair_record_skew_at_or_above_threshold_count"),
        label + ".matched_pair_record_skew_at_or_above_threshold_count",
        0,
    )
    if above_threshold > pair_count:
        raise ReportError("{} over-threshold count exceeds pair count".format(label))
    skew = _mapping(
        stereo.get("matched_pair_record_time_absolute_skew"),
        label + ".matched_pair_record_time_absolute_skew",
    )
    minimum = _integer(skew.get("min_ns"), label + ".skew.min_ns", 0)
    maximum = _integer(skew.get("max_ns"), label + ".skew.max_ns", 0)
    total = _integer(skew.get("sum_ns"), label + ".skew.sum_ns", 0)
    mean = _finite_number(skew.get("mean_ns"), label + ".skew.mean_ns", 0.0)
    if maximum < minimum or total < minimum * pair_count or total > maximum * pair_count:
        raise ReportError("{} matched-pair skew statistics are inconsistent".format(label))
    if abs(mean - round(total / pair_count, 3)) > 1e-12:
        raise ReportError("{} matched-pair skew mean is inconsistent".format(label))
    _sha256_text(stereo.get("pair_semantic_sha256"), label + ".pair_semantic_sha256")
    return dict(stereo)


def _validate_adapter_report(value: Mapping[str, Any], sequence: str) -> Dict[str, Any]:
    label = "adapter[{}]".format(sequence)
    if value.get("schema") != ADAPTATION_SCHEMA:
        raise ReportError("{} schema must be {}".format(label, ADAPTATION_SCHEMA))
    preservation = _mapping(value.get("preservation"), label + ".preservation")
    if preservation.get("all_passed") is not True:
        raise ReportError("{} semantic preservation did not pass".format(label))
    checks = _mapping(preservation.get("checks"), label + ".preservation.checks")
    if not checks or any(item is not True for item in checks.values()):
        raise ReportError("{} contains a failed or malformed preservation check".format(label))
    if value.get("ground_truth_policy") != "source_required_and_audited_output_omitted":
        raise ReportError("{} ground-truth policy mismatch".format(label))

    source_audit_raw = _mapping(value.get("source_audit"), label + ".source_audit")
    source_profile = _string(
        source_audit_raw.get("source_profile"), label + ".source_audit.source_profile"
    )
    if source_profile not in SOURCE_TOPICS_BY_PROFILE:
        raise ReportError("{} has unsupported source profile {}".format(label, source_profile))
    if value.get("source_profile") != source_profile:
        raise ReportError("{} outer/source-audit profile mismatch".format(label))

    audits: Dict[str, Dict[str, Any]] = {}
    stereo_reports: Dict[str, Dict[str, Any]] = {}
    stream_kinds: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    for role, topics in (
        ("source", SOURCE_TOPICS_BY_PROFILE[source_profile]),
        ("adapted", ADAPTED_TOPICS),
    ):
        audit = _mapping(value.get(role + "_audit"), "{}.{}_audit".format(label, role))
        if audit.get("schema") != "turnsafe.kaist_vio_adapter.audit.v1":
            raise ReportError("{}.{}_audit schema mismatch".format(label, role))
        if audit.get("role") != role:
            raise ReportError("{}.{}_audit role mismatch".format(label, role))
        if role == "source" and audit.get("source_profile") != source_profile:
            raise ReportError("{}.source_audit source profile mismatch".format(label))
        if role == "adapted" and audit.get("source_profile") is not None:
            raise ReportError("{}.adapted_audit must not declare a source profile".format(label))
        streams = _mapping(audit.get("streams"), "{}.{}_audit.streams".format(label, role))
        if set(streams) != set(topics):
            raise ReportError("{}.{}_audit stream set mismatch".format(label, role))
        validated_streams: Dict[str, Any] = {}
        for topic, message_type in topics.items():
            validated_streams[topic] = _validate_stream(
                _mapping(streams[topic], "{}.{} stream {}".format(label, role, topic)),
                message_type,
                EXPECTED_STREAM_KINDS[topic],
                "{}.{} stream {}".format(label, role, topic),
            )
        kinds = _streams_by_kind(
            validated_streams, "{}.{}_audit".format(label, role)
        )
        expected_kinds = {"camera0", "camera1", "imu"}
        if role == "source":
            expected_kinds.add("ground_truth")
        if set(kinds) != expected_kinds:
            raise ReportError("{}.{}_audit stream-kind set mismatch".format(label, role))
        stereo = _validate_exact_header_stereo(
            audit.get("stereo"), kinds, "{}.{}_audit.stereo".format(label, role)
        )
        _sha256_text(
            audit.get("logical_order_semantic_sha256"),
            "{}.{}_audit.logical_order_semantic_sha256".format(label, role),
        )
        _sha256_text(
            audit.get("estimator_input_order_semantic_sha256"),
            "{}.{}_audit.estimator_input_order_semantic_sha256".format(
                label, role
            ),
        )
        audits[role] = dict(audit)
        stereo_reports[role] = stereo
        stream_kinds[role] = kinds

    source = audits["source"]
    adapted = audits["adapted"]
    source_by_kind = stream_kinds["source"]
    adapted_by_kind = stream_kinds["adapted"]
    for kind in ("camera0", "camera1", "imu"):
        if source_by_kind[kind]["count"] != adapted_by_kind[kind]["count"]:
            raise ReportError("{} retained count mismatch for {}".format(label, kind))
    stereo_equality_fields = (
        "policy",
        "pair_count",
        "exact_header_pair_count",
        "camera0_count",
        "camera1_count",
        "camera0_unmatched_count",
        "camera1_unmatched_count",
        "camera0_unmatched_header_stamps_ns",
        "camera1_unmatched_header_stamps_ns",
        "record_skew_threshold_ns",
        "matched_pair_record_skew_at_or_above_threshold_count",
        "matched_pair_record_time_absolute_skew",
        "pair_semantic_sha256",
    )
    stereo_mismatches = [
        field
        for field in stereo_equality_fields
        if stereo_reports["source"].get(field)
        != stereo_reports["adapted"].get(field)
    ]
    if stereo_mismatches:
        raise ReportError(
            "{} source/adapted exact-header stereo evidence differs: {}".format(
                label, ", ".join(stereo_mismatches)
            )
        )
    if (
        source["estimator_input_order_semantic_sha256"]
        != adapted["estimator_input_order_semantic_sha256"]
    ):
        raise ReportError("{} source/adapted estimator-input digest mismatch".format(label))
    for kind in ("camera0", "camera1"):
        source_image = _mapping(
            source_by_kind[kind].get("image"),
            "{} source image {}".format(label, kind),
        )
        adapted_image = _mapping(
            adapted_by_kind[kind].get("image"),
            "{} adapted image {}".format(label, kind),
        )
        source_digest = _sha256_text(
            source_image.get("decoded_image_semantic_sha256"),
            "{} source image digest {}".format(label, kind),
        )
        adapted_digest = _sha256_text(
            adapted_image.get("decoded_image_semantic_sha256"),
            "{} adapted image digest {}".format(label, kind),
        )
        if source_digest != adapted_digest:
            raise ReportError(
                "{} decoded image digest mismatch for {}".format(label, kind)
            )
    source_imu_digest = _sha256_text(
        source_by_kind["imu"].get(
            "recorded_message_semantic_sha256"
        ),
        label + " source IMU semantic digest",
    )
    adapted_imu_digest = _sha256_text(
        adapted_by_kind["imu"].get(
            "recorded_message_semantic_sha256"
        ),
        label + " adapted IMU semantic digest",
    )
    if source_imu_digest != adapted_imu_digest:
        raise ReportError("{} IMU semantic digest mismatch".format(label))
    ground_truth = source_by_kind["ground_truth"]
    if ground_truth.get("finite_pose_components") is not True:
        raise ReportError("{} ground-truth pose components were not finite".format(label))
    if ground_truth.get("nonzero_quaternion") is not True:
        raise ReportError("{} ground-truth quaternion was zero".format(label))
    if (
        source_by_kind["imu"].get("finite_runtime_components")
        is not True
    ):
        raise ReportError("{} source runtime IMU components were not finite".format(label))
    return dict(value)


def _rosbag_topic_map(value: Mapping[str, Any], label: str) -> Dict[str, Mapping[str, Any]]:
    raw_topics = _list(value.get("topics"), label + ".topics")
    topics: Dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(raw_topics):
        item = _mapping(raw, "{}.topics[{}]".format(label, index))
        name = _string(item.get("topic"), "{}.topics[{}].topic".format(label, index))
        if name in topics:
            raise ReportError("{} contains duplicate topic {}".format(label, name))
        topics[name] = item
    return topics


def _validate_rosbag_info(
    value: Mapping[str, Any], source_audit: Mapping[str, Any], sequence: str
) -> Dict[str, Any]:
    label = "rosbag_info[{}]".format(sequence)
    duration = _finite_number(value.get("duration"), label + ".duration", 0.0)
    start = _finite_number(value.get("start"), label + ".start")
    end = _finite_number(value.get("end"), label + ".end")
    if duration <= 0.0 or end <= start:
        raise ReportError("{} has nonpositive duration or timestamp bounds".format(label))
    total_messages = _integer(value.get("messages"), label + ".messages", 1)
    topics = _rosbag_topic_map(value, label)
    audit_streams = _mapping(source_audit.get("streams"), label + ".audit_streams")
    required_topics = {
        topic: _string(
            _mapping(stream, "{} audit stream {}".format(label, topic)).get("type"),
            "{} audit stream {} type".format(label, topic),
        )
        for topic, stream in audit_streams.items()
    }
    missing = sorted(set(required_topics) - set(topics))
    if missing:
        raise ReportError("{} is missing required topics: {}".format(label, ", ".join(missing)))
    for topic, expected_type in required_topics.items():
        item = topics[topic]
        if item.get("type") != expected_type:
            raise ReportError("{} topic {} type mismatch".format(label, topic))
        messages = _integer(item.get("messages"), "{} topic {} messages".format(label, topic), 1)
        if messages != audit_streams[topic]["count"]:
            raise ReportError("{} topic {} count disagrees with adapter audit".format(label, topic))
        if "frequency" in item and item["frequency"] is not None:
            _finite_number(item["frequency"], "{} topic {} frequency".format(label, topic), 0.0)
    required_messages = sum(
        audit_streams[topic]["count"] for topic in required_topics
    )
    if total_messages < required_messages:
        raise ReportError(
            "{} total message count is below its required-stream counts".format(
                label
            )
        )
    normalized = dict(value)
    normalized["required_topics_validated"] = True
    return normalized


def _validate_exact_header_runtime(
    value: Any, adapted_stereo: Mapping[str, Any], label: str
) -> Dict[str, Any]:
    runtime = _mapping(value, label)
    if runtime.get("matches_adapted_audit") is not True:
        raise ReportError("{} does not match the adapted audit".format(label))
    threshold = _integer(
        runtime.get("adapted_audit_record_skew_threshold_ns"),
        label + ".adapted_audit_record_skew_threshold_ns",
        1,
    )
    if threshold != adapted_stereo["record_skew_threshold_ns"]:
        raise ReportError("{} record-skew threshold mismatch".format(label))
    skew = _mapping(
        adapted_stereo.get("matched_pair_record_time_absolute_skew"),
        label + ".adapted_stereo.skew",
    )
    expected = {
        "exact_header_pairs": adapted_stereo["exact_header_pair_count"],
        "camera0_without_match": adapted_stereo["camera0_unmatched_count"],
        "camera1_without_match": adapted_stereo["camera1_unmatched_count"],
        "record_delta_ge_20ms": adapted_stereo[
            "matched_pair_record_skew_at_or_above_threshold_count"
        ],
        "maximum_record_delta_ns": skew["max_ns"],
    }
    declared_expected = _mapping(
        runtime.get("expected_from_adapted_audit"),
        label + ".expected_from_adapted_audit",
    )
    observed = _mapping(runtime.get("observed"), label + ".observed")
    if declared_expected != expected:
        raise ReportError("{} declared expected values mismatch adapted audit".format(label))
    if observed != expected:
        raise ReportError("{} observed values mismatch adapted audit".format(label))
    _string(runtime.get("line"), label + ".line")
    return dict(runtime)


def _validate_camera_enqueue_runtime(
    value: Any, exact_header_pair_count: int, label: str
) -> Dict[str, Any]:
    runtime = _mapping(value, label)
    if runtime.get("frequency_thinning_policy") != (
        "existing_frozen_baseline_camera_frequency_policy"
    ):
        raise ReportError("{} frequency-thinning policy mismatch".format(label))
    declared_exact = _integer(
        runtime.get("exact_header_pairs"), label + ".exact_header_pairs", 1
    )
    if declared_exact != exact_header_pair_count:
        raise ReportError("{} exact-header pair count mismatch".format(label))
    observed = _mapping(runtime.get("observed"), label + ".observed")
    queued = _integer(
        observed.get("queued_pairs"), label + ".observed.queued_pairs", 0
    )
    thinned = _integer(
        observed.get("frequency_thinned_pairs"),
        label + ".observed.frequency_thinned_pairs",
        0,
    )
    processed = _integer(
        observed.get("processed_pairs"),
        label + ".observed.processed_pairs",
        0,
    )
    decode0 = _integer(
        observed.get("cam0_decode_failures"),
        label + ".observed.cam0_decode_failures",
        0,
    )
    decode1 = _integer(
        observed.get("cam1_decode_failures"),
        label + ".observed.cam1_decode_failures",
        0,
    )
    pending = _integer(
        observed.get("pending_pairs"), label + ".observed.pending_pairs", 0
    )
    if set(observed) != {
        "queued_pairs",
        "processed_pairs",
        "frequency_thinned_pairs",
        "cam0_decode_failures",
        "cam1_decode_failures",
        "pending_pairs",
    }:
        raise ReportError("{} observed field set mismatch".format(label))
    accounted = _integer(
        runtime.get("accounted_pairs"), label + ".accounted_pairs", 0
    )
    if accounted != queued + thinned or accounted != declared_exact:
        raise ReportError("{} exact-pair enqueue accounting is incomplete".format(label))
    if _bool(
        runtime.get("pair_accounting_complete"),
        label + ".pair_accounting_complete",
    ) is not True:
        raise ReportError("{} pair-accounting check did not pass".format(label))
    if processed != queued:
        raise ReportError("{} processed/queued pair counts disagree".format(label))
    if _bool(
        runtime.get("processed_pairs_match_queued"),
        label + ".processed_pairs_match_queued",
    ) is not True:
        raise ReportError("{} processed/queued equality check did not pass".format(label))
    if pending != 0:
        raise ReportError("{} retains pending stereo pairs".format(label))
    if _bool(runtime.get("queue_drained"), label + ".queue_drained") is not True:
        raise ReportError("{} queue-drained check did not pass".format(label))
    if decode0 != 0 or decode1 != 0:
        raise ReportError("{} records camera decode failures".format(label))
    if _bool(
        runtime.get("decode_failures_zero"), label + ".decode_failures_zero"
    ) is not True:
        raise ReportError("{} zero-decode-failure check did not pass".format(label))
    _string(runtime.get("line"), label + ".line")
    normalized = dict(runtime)
    normalized["observed"] = dict(observed)
    return normalized


def _validate_campaign(
    value: Mapping[str, Any],
    sequence: str,
    source: Mapping[str, Any],
    adapted: Mapping[str, Any],
    adapter_report: Mapping[str, Any],
    baseline_source: Mapping[str, Any],
    verifier: _IdentityVerifier,
) -> Dict[str, Any]:
    label = "campaign[{}]".format(sequence)
    schema = _string(value.get("schema"), label + ".schema")
    if schema != "turnsafe.kaist_vio_campaign.sequence.v1":
        raise ReportError("{} has unsupported schema {}".format(label, schema))
    if value.get("sequence") != sequence:
        raise ReportError("{} sequence mismatch".format(label))
    if value.get("campaign_index") != ORDERED_SEQUENCES.index(sequence):
        raise ReportError("{} campaign index mismatch".format(label))
    if value.get("prepare_only") is not False:
        raise ReportError("{} is a preparation record, not a baseline run".format(label))
    status = _string(value.get("status"), label + ".status")
    if status not in ("COMPLETED", "FAILED", "TIMED_OUT"):
        raise ReportError("{} has nonterminal status {}".format(label, status))
    _string(value.get("started_utc"), label + ".started_utc")
    _string(value.get("finished_utc"), label + ".finished_utc")

    completion = _mapping(value.get("completion"), label + ".completion")
    for key in (
        "estimator_started", "estimator_completed", "output_validation_passed",
        "evaluation_completed", "timed_out", "reset_detected",
        "nonfinite_detected",
    ):
        _bool(completion.get(key), "{}.completion.{}".format(label, key))
    for key in ("unpaired_warning_count", "dropped_message_count"):
        _integer(completion.get(key), "{}.completion.{}".format(label, key), 0)
    if completion.get("roslaunch_exit_code") is not None:
        _integer(completion.get("roslaunch_exit_code"), label + ".completion.roslaunch_exit_code")
    if completion.get("roslaunch_runtime_seconds") is not None:
        _finite_number(
            completion.get("roslaunch_runtime_seconds"),
            label + ".completion.roslaunch_runtime_seconds",
            0.0,
        )

    runtime = _mapping(value.get("runtime"), label + ".runtime")
    _finite_number(runtime.get("total_duration_seconds"), label + ".runtime.total_duration_seconds", 0.0)
    if runtime.get("ground_truth_runtime_policy") != "reference_used_only_after_roslaunch_finished":
        raise ReportError("{} runtime ground-truth policy mismatch".format(label))

    before = _mapping(value.get("inputs_before"), label + ".inputs_before")
    required_inputs = (
        "source_bag", "config", "kalibr_imu_chain", "kalibr_imucam_chain",
        "launch", "estimator_binary", "reference_tum", "adapter",
        "trajectory_converter", "python", "rosbag", "roslaunch", "evo_ape",
        "evo_rpe", "catkin_find",
    )
    missing_inputs = sorted(set(required_inputs) - set(before))
    if missing_inputs:
        raise ReportError("{} inputs_before is missing {}".format(label, ", ".join(missing_inputs)))
    verified_inputs = {
        name: verifier.verify_record(before[name], "{}.inputs_before.{}".format(label, name))
        for name in required_inputs
    }
    for name in required_inputs:
        _bool(
            _mapping(before[name], "{}.inputs_before.{}".format(label, name)).get(
                "executable"
            ),
            "{}.inputs_before.{}.executable".format(label, name),
        )
    for name in (
        "estimator_binary", "python", "rosbag", "roslaunch", "evo_ape",
        "evo_rpe", "catkin_find",
    ):
        if before[name].get("executable") is not True or not os.access(
            verified_inputs[name]["path"], os.X_OK
        ):
            raise ReportError("{} executable input {} is not executable".format(label, name))
    if verified_inputs["source_bag"]["sha256"] != source["sha256"]:
        raise ReportError("{} source bag hash mismatch".format(label))
    if not _identity_equal(
        verified_inputs["estimator_binary"], baseline_source["estimator_binary"]
    ):
        raise ReportError(
            "{} estimator binary differs from the common baseline source identity".format(
                label
            )
        )
    for name in REQUIRED_BASELINE_SOURCE_FILES:
        source_file = baseline_source["source_files"][name]
        campaign_file = verified_inputs[name]
        if (
            source_file["size_bytes"] != campaign_file["size_bytes"]
            or source_file["sha256"] != campaign_file["sha256"]
        ):
            raise ReportError(
                "{} input {} differs from the common baseline source identity".format(
                    label, name
                )
            )
    adapted_record = verifier.verify_record(value.get("adapted_bag"), label + ".adapted_bag")
    if adapted_record["sha256"] != adapted["sha256"]:
        raise ReportError("{} adapted bag hash mismatch".format(label))

    campaign_source_audit = _mapping(value.get("source_audit"), label + ".source_audit")
    campaign_adapted_audit = _mapping(value.get("adapted_audit"), label + ".adapted_audit")
    for role, campaign_audit in (
        ("source", campaign_source_audit), ("adapted", campaign_adapted_audit)
    ):
        expected = adapter_report[role + "_audit"]
        for digest_key in (
            "logical_order_semantic_sha256", "estimator_input_order_semantic_sha256"
        ):
            if campaign_audit.get(digest_key) != expected.get(digest_key):
                raise ReportError("{} {} audit {} mismatch".format(label, role, digest_key))
        if campaign_audit.get("stereo") != expected.get("stereo"):
            raise ReportError("{} {} exact-header stereo audit mismatch".format(label, role))

    exact_header_runtime: Optional[Dict[str, Any]] = None
    if value.get("exact_header_stereo_runtime") is not None:
        exact_header_runtime = _validate_exact_header_runtime(
            value.get("exact_header_stereo_runtime"),
            _mapping(
                campaign_adapted_audit.get("stereo"),
                label + ".adapted_audit.stereo",
            ),
            label + ".exact_header_stereo_runtime",
        )
    elif status == "COMPLETED":
        raise ReportError("{} completed run has no exact-header runtime evidence".format(label))

    camera_enqueue_runtime = _validate_camera_enqueue_runtime(
        value.get("camera_enqueue_runtime"),
        _integer(
            _mapping(
                campaign_adapted_audit.get("stereo"),
                label + ".adapted_audit.stereo",
            ).get("exact_header_pair_count"),
            label + ".adapted_audit.stereo.exact_header_pair_count",
            1,
        ),
        label + ".camera_enqueue_runtime",
    )

    commands = _mapping(value.get("commands"), label + ".commands")
    required_commands = (
        "source_rosbag_info", "adapted_rosbag_info", "resolve_parameters",
        "resolve_estimator_binary", "roslaunch", "trajectory_conversion",
        "ape_translation", "rpe_translation_1m", "rpe_rotation_1m_deg",
    )
    missing_commands = sorted(set(required_commands) - set(commands))
    if status == "COMPLETED" and missing_commands:
        raise ReportError("{} commands is missing {}".format(label, ", ".join(missing_commands)))
    normalized_commands: Dict[str, Any] = {}
    for name in sorted(commands):
        command = _mapping(commands[name], "{}.commands.{}".format(label, name))
        argv = _list(command.get("argv"), "{}.commands.{}.argv".format(label, name))
        if not argv or not all(isinstance(item, str) and item for item in argv):
            raise ReportError("{}.commands.{}.argv is malformed".format(label, name))
        log_identity = verifier.verify(
            {
                "path": command.get("log"),
                "size_bytes": command.get("log_size_bytes"),
                "sha256": command.get("log_sha256"),
            },
            "{}.commands.{}.log".format(label, name),
            minimum_size=0,
        )
        normalized = dict(command)
        normalized["log_identity"] = log_identity
        normalized_commands[name] = normalized
    if status == "COMPLETED":
        for name in required_commands:
            command = normalized_commands[name]
            if (
                command.get("exit_code") != 0
                or command.get("timed_out") is not False
                or command.get("interrupted") is not False
                or command.get("process_group_survived_cleanup") is not False
                or command.get("signals_sent") != []
                or command.get("error") is not None
            ):
                raise ReportError(
                    "{} completed run has unsuccessful command {}".format(
                        label, name
                    )
                )

    outputs_raw = _mapping(value.get("outputs", {}), label + ".outputs")
    required_outputs = ("state", "deviation", "timing", "trajectory_tum")
    if status == "COMPLETED" and any(name not in outputs_raw for name in required_outputs):
        raise ReportError("{} completed run is missing a required numeric output".format(label))
    outputs: Dict[str, Any] = {}
    for name in sorted(outputs_raw):
        record = _mapping(outputs_raw[name], "{}.outputs.{}".format(label, name))
        identity = verifier.verify_record(record, "{}.outputs.{}".format(label, name))
        normalized = dict(record)
        normalized.update(identity)
        rows = _integer(record.get("rows"), "{}.outputs.{}.rows".format(label, name), 1)
        _integer(record.get("columns"), "{}.outputs.{}.columns".format(label, name), 1)
        if record.get("timestamps_strictly_increasing") is not True:
            raise ReportError("{}.outputs.{} timestamps are not monotonic".format(label, name))
        if record.get("all_values_finite") is not True:
            raise ReportError("{}.outputs.{} contains non-finite values".format(label, name))
        first = _finite_number(
            record.get("first_timestamp"),
            "{}.outputs.{}.first_timestamp".format(label, name),
        )
        last = _finite_number(
            record.get("last_timestamp"),
            "{}.outputs.{}.last_timestamp".format(label, name),
        )
        if rows > 1 and last <= first:
            raise ReportError("{}.outputs.{} timestamp bounds are invalid".format(label, name))
        outputs[name] = normalized

    metrics_raw = _mapping(value.get("metrics", {}), label + ".metrics")
    required_metrics = ("ape_translation", "rpe_translation_1m", "rpe_rotation_1m_deg")
    if status == "COMPLETED" and any(name not in metrics_raw for name in required_metrics):
        raise ReportError("{} completed run is missing a required metric".format(label))
    metrics: Dict[str, Any] = {}
    for name in sorted(metrics_raw):
        record = _mapping(metrics_raw[name], "{}.metrics.{}".format(label, name))
        identity = verifier.verify_record(record, "{}.metrics.{}".format(label, name), require_size=False)
        normalized = dict(record)
        normalized.update(identity)
        _integer(record.get("samples"), "{}.metrics.{}.samples".format(label, name), 1)
        stats = _mapping(record.get("stats"), "{}.metrics.{}.stats".format(label, name))
        _finite_number(stats.get("rmse"), "{}.metrics.{}.stats.rmse".format(label, name), 0.0)
        metrics[name] = normalized

    diagnostics = _mapping(value.get("diagnostics"), label + ".diagnostics")
    if set(diagnostics) != {"yaw", "tilt"}:
        raise ReportError("{} diagnostics must contain exactly yaw and tilt".format(label))
    for name in ("yaw", "tilt"):
        diagnostic = _mapping(diagnostics[name], "{}.diagnostics.{}".format(label, name))
        if diagnostic.get("status") != "NOT_AVAILABLE":
            raise ReportError("{}.diagnostics.{} has an unsupported status".format(label, name))
        _string(diagnostic.get("reason"), "{}.diagnostics.{}.reason".format(label, name))

    output_consistency = _mapping(
        value.get("output_consistency", {}), label + ".output_consistency"
    )
    if status == "COMPLETED" and not output_consistency:
        raise ReportError("{} completed run has no output-consistency evidence".format(label))
    if output_consistency:
        for key in (
            "state_deviation_timestamp_sequences_equal",
            "state_trajectory_timestamp_sequences_equal",
            "callback_timing_row_counts_equal",
            "timing_state_timestamps_within_tolerance",
        ):
            if not _bool(
                output_consistency.get(key),
                "{}.output_consistency.{}".format(label, key),
            ):
                raise ReportError("{}.output_consistency.{} did not pass".format(label, key))
        state_rows = _integer(
            output_consistency.get("callback_state_rows"),
            label + ".output_consistency.callback_state_rows",
            1,
        )
        timing_rows = _integer(
            output_consistency.get("timing_rows"),
            label + ".output_consistency.timing_rows",
            1,
        )
        if state_rows != timing_rows:
            raise ReportError("{} output consistency row counts disagree".format(label))
        tolerance = _finite_number(
            output_consistency.get("timing_state_timestamp_tolerance_seconds"),
            label + ".output_consistency.timing_state_timestamp_tolerance_seconds",
            0.0,
        )
        maximum_difference = _finite_number(
            output_consistency.get("timing_state_maximum_absolute_difference_seconds"),
            label + ".output_consistency.timing_state_maximum_absolute_difference_seconds",
            0.0,
        )
        if maximum_difference > tolerance:
            raise ReportError("{} timing/state timestamp tolerance is exceeded".format(label))

    checks = _mapping(value.get("checks"), label + ".checks")
    if not all(isinstance(check, bool) for check in checks.values()):
        raise ReportError("{} checks must all be booleans".format(label))
    if status == "COMPLETED" and checks.get(
        "exact_header_runtime_matches_adapted_audit"
    ) is not True:
        raise ReportError("{} exact-header runtime equality check did not pass".format(label))
    if checks.get("camera_enqueue_accounts_for_exact_header_pairs") is not True:
        raise ReportError("{} camera enqueue accounting check did not pass".format(label))
    if checks.get("camera_decode_failures_zero") is not True:
        raise ReportError("{} camera decode-failure check did not pass".format(label))
    if checks.get("camera_processed_pairs_match_queued") is not True:
        raise ReportError("{} processed/queued campaign check did not pass".format(label))
    if checks.get("camera_queue_drained") is not True:
        raise ReportError("{} queue-drained campaign check did not pass".format(label))

    reference_validation_raw = value.get("reference_validation")
    reference_validation = None
    if reference_validation_raw is not None:
        reference_validation = dict(
            _mapping(reference_validation_raw, label + ".reference_validation")
        )
        verified_reference = verifier.verify_record(
            reference_validation, label + ".reference_validation"
        )
        if verified_reference["sha256"] != verified_inputs["reference_tum"]["sha256"]:
            raise ReportError("{} reference validation identity mismatch".format(label))
        _integer(
            reference_validation.get("rows"),
            label + ".reference_validation.rows",
            1,
        )
        if reference_validation.get("timestamps_strictly_increasing") is not True:
            raise ReportError("{} reference timestamps are not monotonic".format(label))
        if reference_validation.get("all_values_finite") is not True:
            raise ReportError("{} reference contains non-finite values".format(label))
        reference_validation.update(verified_reference)
    elif status == "COMPLETED":
        raise ReportError("{} completed run has no reference validation".format(label))
    after = _mapping(value.get("inputs_after", {}), label + ".inputs_after")
    if status == "COMPLETED":
        if any(name not in after for name in required_inputs):
            raise ReportError("{} inputs_after is incomplete".format(label))
        for name in required_inputs:
            if before[name] != after[name]:
                raise ReportError("{} fixed input {} changed during the run".format(label, name))
        if "adapted_bag" not in after or after["adapted_bag"] != value.get("adapted_bag"):
            raise ReportError("{} adapted input identity changed during the run".format(label))
        if checks.get("fixed_inputs_unchanged") is not True:
            raise ReportError("{} did not pass the fixed-input check".format(label))

    resolved_parameters: Optional[Dict[str, Any]] = None
    if "resolve_parameters" in normalized_commands:
        identity = normalized_commands["resolve_parameters"]["log_identity"]
        parameter_path = _input_path(identity["path"], label + ".resolved_parameters", verifier.roots)
        try:
            content = parameter_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ReportError("{} resolved parameters are unreadable: {}".format(label, exc)) from exc
        resolved_parameters = dict(identity)
        resolved_parameters["content"] = content

    normalized_campaign = dict(value)
    normalized_campaign["inputs_verified"] = verified_inputs
    normalized_campaign["adapted_bag_verified"] = adapted_record
    normalized_campaign["commands"] = normalized_commands
    normalized_campaign["outputs"] = outputs
    normalized_campaign["metrics"] = metrics
    normalized_campaign["diagnostics"] = dict(diagnostics)
    normalized_campaign["output_consistency"] = dict(output_consistency)
    normalized_campaign["resolved_parameters"] = resolved_parameters
    normalized_campaign["reference_validation"] = reference_validation
    normalized_campaign["exact_header_stereo_runtime"] = exact_header_runtime
    normalized_campaign["camera_enqueue_runtime"] = camera_enqueue_runtime
    return normalized_campaign


def _run_passed(campaign: Mapping[str, Any]) -> bool:
    completion = _mapping(campaign.get("completion"), "campaign.completion")
    checks = _mapping(campaign.get("checks"), "campaign.checks")
    return (
        campaign.get("status") == "COMPLETED"
        and completion.get("estimator_started") is True
        and completion.get("estimator_completed") is True
        and completion.get("output_validation_passed") is True
        and completion.get("evaluation_completed") is True
        and completion.get("timed_out") is False
        and completion.get("roslaunch_exit_code") == 0
        and completion.get("process_group_survived_cleanup") is False
        and completion.get("reset_detected") is False
        and completion.get("nonfinite_detected") is False
        and completion.get("unpaired_warning_count") == 0
        and completion.get("dropped_message_count") == 0
        and bool(checks)
        and all(checks.values())
    )


def classify_compatibility(
    adapter_required: bool,
    campaign_pass: Mapping[str, bool],
    mh01_parity_passed: bool,
    unsupported_reasons: Sequence[str] = (),
    engineering_blockers: Sequence[str] = (),
) -> str:
    """Derive the sole compatibility classification without optimistic fallback."""

    if unsupported_reasons:
        return "UNSUPPORTED"
    if engineering_blockers:
        return "BLOCKED_ENGINEERING"
    if tuple(campaign_pass) != ORDERED_SEQUENCES:
        return "BLOCKED_ENGINEERING"
    if not mh01_parity_passed:
        return "BLOCKED_ENGINEERING"
    if not campaign_pass.get("rotation/rotation_fast.bag", False):
        return "BLOCKED_ENGINEERING"
    head_sequences = (
        "circle/circle_head.bag",
        "infinite/infinite_head.bag",
        "square/square_head.bag",
    )
    if not any(campaign_pass.get(name, False) for name in head_sequences):
        return "BLOCKED_ENGINEERING"
    if not all(campaign_pass.values()):
        return "BLOCKED_ENGINEERING"
    return "RUNNABLE_WITH_DECLARED_ADAPTER" if adapter_required else "RUNNABLE"


def _reasons(value: Any, label: str) -> List[str]:
    items = _list(value, label)
    result = []
    for index, item in enumerate(items):
        result.append(_string(item, "{}[{}]".format(label, index)))
    return result


def _campaign_summary(campaign: Mapping[str, Any]) -> Dict[str, Any]:
    result = {
        "schema": campaign["schema"],
        "status": campaign["status"],
        "started_utc": campaign["started_utc"],
        "finished_utc": campaign["finished_utc"],
        "inputs": campaign["inputs_verified"],
        "adapted_bag": campaign["adapted_bag_verified"],
        "commands": campaign["commands"],
        "resolved_parameters": campaign.get("resolved_parameters"),
        "completion": campaign["completion"],
        "runtime": campaign["runtime"],
        "checks": campaign["checks"],
        "reference_validation": campaign["reference_validation"],
        "exact_header_stereo_runtime": campaign[
            "exact_header_stereo_runtime"
        ],
        "camera_enqueue_runtime": campaign["camera_enqueue_runtime"],
        "outputs": campaign["outputs"],
        "metrics": campaign["metrics"],
        "output_consistency": campaign["output_consistency"],
    }
    result["diagnostics"] = campaign["diagnostics"]
    if "console_diagnostics" in campaign:
        result["console_diagnostics"] = campaign["console_diagnostics"]
    if "error" in campaign:
        result["error"] = campaign["error"]
    return result


def _validate_index(
    value: Mapping[str, Any], roots: Sequence[Path], verifier: _IdentityVerifier
) -> Dict[str, Any]:
    if value.get("schema") != INDEX_SCHEMA:
        raise ReportError("evidence index schema must be {}".format(INDEX_SCHEMA))
    adapter_required = _bool(value.get("adapter_required"), "index.adapter_required")
    if adapter_required is not True:
        raise ReportError(
            "index.adapter_required must be true for the Session-0.5 KAIST adapter"
        )
    baseline_source = _validate_baseline_source_identity(
        value.get("baseline_source_identity"), verifier
    )
    unsupported = _reasons(value.get("unsupported_reasons", []), "index.unsupported_reasons")
    blockers = _reasons(value.get("engineering_blockers", []), "index.engineering_blockers")
    parity = _mapping(value.get("mh01_parity"), "index.mh01_parity")
    parity_passed = _bool(parity.get("passed"), "index.mh01_parity.passed")
    expected_digest = _sha256_text(parity.get("expected_digest"), "index.mh01_parity.expected_digest")
    observed_digest = _sha256_text(parity.get("observed_digest"), "index.mh01_parity.observed_digest")
    if parity_passed != (expected_digest == observed_digest):
        raise ReportError("MH_01 parity boolean disagrees with the two digests")
    parity_evidence = _validate_mh01_parity_evidence(
        parity.get("parity_evidence"),
        expected_digest,
        observed_digest,
        baseline_source,
        verifier,
    )

    raw_sequences = _list(value.get("sequences"), "index.sequences")
    observed_names = []
    for index, raw in enumerate(raw_sequences):
        item = _mapping(raw, "index.sequences[{}]".format(index))
        observed_names.append(_string(item.get("sequence"), "index.sequences[{}].sequence".format(index)))
    if len(set(observed_names)) != len(observed_names):
        raise ReportError("index.sequences contains a duplicate sequence")
    if tuple(observed_names) != ORDERED_SEQUENCES:
        raise ReportError(
            "index.sequences must equal the exact frozen order; got {}".format(
                _compact_json(observed_names)
            )
        )

    sequences: List[Dict[str, Any]] = []
    campaign_pass: Dict[str, bool] = {}
    source_paths = set()
    adapted_paths = set()
    for index, raw in enumerate(raw_sequences):
        item = _mapping(raw, "index.sequences[{}]".format(index))
        sequence = observed_names[index]
        source = verifier.verify(item.get("source_bag"), "sequence[{}].source_bag".format(sequence))
        adapted = verifier.verify(item.get("adapted_bag"), "sequence[{}].adapted_bag".format(sequence))
        if source["path"] == adapted["path"]:
            raise ReportError("{} source and adapted bags resolve to the same file".format(sequence))
        if tuple(Path(source["path"]).parts[-2:]) != tuple(Path(sequence).parts):
            raise ReportError("{} source bag path suffix does not match sequence".format(sequence))
        if tuple(Path(adapted["path"]).parts[-2:]) != tuple(Path(sequence).parts):
            raise ReportError("{} adapted bag path suffix does not match sequence".format(sequence))
        if source["path"] in source_paths or adapted["path"] in adapted_paths:
            raise ReportError("index.sequences reuses a source or adapted bag path")
        source_paths.add(source["path"])
        adapted_paths.add(adapted["path"])

        adapter_path = _input_path(item.get("adapter_report"), "sequence[{}].adapter_report".format(sequence), roots)
        adapter_identity = verifier.identify(
            adapter_path, "sequence[{}].adapter_report_identity".format(sequence)
        )
        adapter_report = _validate_adapter_report(
            _read_json_path(adapter_path, "sequence[{}].adapter_report".format(sequence), roots),
            sequence,
        )

        info_path = _input_path(item.get("rosbag_info"), "sequence[{}].rosbag_info".format(sequence), roots)
        info_identity = verifier.identify(
            info_path, "sequence[{}].rosbag_info_identity".format(sequence)
        )
        info = _validate_rosbag_info(
            _read_info_path(info_path, "sequence[{}].rosbag_info".format(sequence), roots),
            adapter_report["source_audit"],
            sequence,
        )

        campaign_path = _input_path(item.get("campaign_manifest"), "sequence[{}].campaign_manifest".format(sequence), roots)
        campaign_identity = verifier.identify(
            campaign_path, "sequence[{}].campaign_manifest_identity".format(sequence)
        )
        campaign = _validate_campaign(
            _read_json_path(campaign_path, "sequence[{}].campaign_manifest".format(sequence), roots),
            sequence,
            source,
            adapted,
            adapter_report,
            baseline_source,
            verifier,
        )
        campaign_info = campaign["commands"]["source_rosbag_info"][
            "log_identity"
        ]
        if campaign_info != info_identity:
            raise ReportError(
                "{} indexed rosbag-info capture is not the campaign capture".format(
                    sequence
                )
            )
        passed = _run_passed(campaign)
        campaign_pass[sequence] = passed
        sequences.append(
            {
                "order": index + 1,
                "sequence": sequence,
                "source_bag": source,
                "adapted_bag": adapted,
                "adapter_report": adapter_identity,
                "adapter_audit": adapter_report,
                "rosbag_info_evidence": info_identity,
                "rosbag_info": info,
                "campaign_manifest": campaign_identity,
                "campaign": _campaign_summary(campaign),
                "campaign_passed": passed,
                "baseline_source_evidence": baseline_source[
                    "evidence_identity"
                ],
                "baseline_source_commit": baseline_source["source_commit"],
                "baseline_source_tree": baseline_source["source_tree"],
            }
        )

    classification = classify_compatibility(
        adapter_required,
        campaign_pass,
        parity_passed,
        unsupported,
        blockers,
    )
    return {
        "schema": SEQUENCE_OUTPUT_SCHEMA,
        "ordered_sequence_allowlist": list(ORDERED_SEQUENCES),
        "adapter_required": adapter_required,
        "baseline_source": baseline_source,
        "mh01_parity": {
            "passed": parity_passed,
            "expected_digest": expected_digest,
            "observed_digest": observed_digest,
            "parity_evidence": parity_evidence,
            **{
                key: parity[key]
                for key in sorted(parity)
                if key
                not in {
                    "passed",
                    "expected_digest",
                    "observed_digest",
                    "parity_evidence",
                }
            },
        },
        "unsupported_reasons": unsupported,
        "engineering_blockers": blockers,
        "sequences": sequences,
        "compatibility_classification": classification,
    }


INVENTORY_COLUMNS: Tuple[str, ...] = (
    "order", "sequence", "bag_readable", "source_profile", "source_path",
    "source_size_bytes", "source_sha256",
    "adapted_path", "adapted_size_bytes", "adapted_sha256", "start_time_ns",
    "end_time_ns", "duration_ns", "left_topic", "left_type", "left_count",
    "left_rate_hz", "right_topic", "right_type", "right_count",
    "right_rate_hz", "color_topic", "color_type", "color_count",
    "color_rate_hz", "imu_type", "imu_count",
    "imu_rate_hz", "ground_truth_type", "ground_truth_count",
    "ground_truth_rate_hz", "stereo_policy", "stereo_pair_count",
    "stereo_exact_header_stamps", "left_unmatched_count",
    "right_unmatched_count", "record_skew_threshold_ns",
    "record_skew_at_or_above_threshold_count", "maximum_record_skew_ns",
    "source_semantic_sha256",
    "adapted_semantic_sha256", "preservation_all_passed",
)

BASELINE_COLUMNS: Tuple[str, ...] = (
    "order", "sequence", "baseline_source_evidence",
    "baseline_source_commit", "baseline_source_tree", "status", "passed",
    "error", "command",
    "source_bag_sha256",
    "adapted_bag_sha256", "config_sha256", "kalibr_imu_chain_sha256",
    "kalibr_imucam_chain_sha256", "launch_sha256", "binary_sha256",
    "reference_sha256", "resolved_parameters", "process_exit_code",
    "completed", "reset_detected", "nonfinite_detected", "timed_out",
    "runtime_seconds", "dropped_message_count", "unpaired_warning_count",
    "exact_header_pair_count", "queued_pair_count", "processed_pair_count",
    "frequency_thinned_pair_count", "camera0_decode_failure_count",
    "camera1_decode_failure_count", "pending_pair_count",
    "camera_enqueue_accounting_passed", "processed_pairs_match_queued",
    "camera_queue_drained", "camera_decode_failures_zero",
    "state_output", "trajectory_output", "covariance_deviation_output",
    "timing_output", "ate", "translation_rpe", "rotation_rpe", "yaw", "tilt",
)


def _csv_bytes(columns: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})
    return output.getvalue().encode("utf-8")


def _inventory_rows(sequence_manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in sequence_manifest["sequences"]:
        source_audit = item["adapter_audit"]["source_audit"]
        adapted_audit = item["adapter_audit"]["adapted_audit"]
        streams = source_audit["streams"]
        by_kind = {
            stream["kind"]: (topic, stream)
            for topic, stream in streams.items()
        }
        left_topic, left = by_kind["camera0"]
        right_topic, right = by_kind["camera1"]
        _, imu = by_kind["imu"]
        _, gt = by_kind["ground_truth"]
        stereo = source_audit["stereo"]
        info_topics = {
            topic["topic"]: topic for topic in item["rosbag_info"]["topics"]
        }
        color_topic = next(
            (
                topic
                for topic in (
                    "/camera/color/image_raw",
                    "/camera/color/image_raw/compressed",
                )
                if topic in info_topics
            ),
            "",
        )
        color = info_topics.get(color_topic, {})
        all_record_starts = [stream["record_time"]["start_ns"] for stream in streams.values()]
        all_record_ends = [stream["record_time"]["end_ns"] for stream in streams.values()]
        start = min(all_record_starts)
        end = max(all_record_ends)
        rows.append(
            {
                "order": item["order"],
                "sequence": item["sequence"],
                "bag_readable": True,
                "source_profile": source_audit["source_profile"],
                "source_path": item["source_bag"]["path"],
                "source_size_bytes": item["source_bag"]["size_bytes"],
                "source_sha256": item["source_bag"]["sha256"],
                "adapted_path": item["adapted_bag"]["path"],
                "adapted_size_bytes": item["adapted_bag"]["size_bytes"],
                "adapted_sha256": item["adapted_bag"]["sha256"],
                "start_time_ns": start,
                "end_time_ns": end,
                "duration_ns": end - start,
                "left_topic": left_topic,
                "left_type": left["type"], "left_count": left["count"],
                "left_rate_hz": left["header_time"]["rate_hz"],
                "right_topic": right_topic,
                "right_type": right["type"], "right_count": right["count"],
                "right_rate_hz": right["header_time"]["rate_hz"],
                "color_topic": color_topic,
                "color_type": color.get("type", ""),
                "color_count": color.get("messages", ""),
                "color_rate_hz": color.get("frequency", ""),
                "imu_type": imu["type"], "imu_count": imu["count"],
                "imu_rate_hz": imu["header_time"]["rate_hz"],
                "ground_truth_type": gt["type"], "ground_truth_count": gt["count"],
                "ground_truth_rate_hz": gt["header_time"]["rate_hz"],
                "stereo_policy": stereo["policy"],
                "stereo_pair_count": stereo["pair_count"],
                "stereo_exact_header_stamps": True,
                "left_unmatched_count": stereo["camera0_unmatched_count"],
                "right_unmatched_count": stereo["camera1_unmatched_count"],
                "record_skew_threshold_ns": stereo["record_skew_threshold_ns"],
                "record_skew_at_or_above_threshold_count": stereo[
                    "matched_pair_record_skew_at_or_above_threshold_count"
                ],
                "maximum_record_skew_ns": stereo[
                    "matched_pair_record_time_absolute_skew"
                ]["max_ns"],
                "source_semantic_sha256": source_audit["estimator_input_order_semantic_sha256"],
                "adapted_semantic_sha256": adapted_audit["estimator_input_order_semantic_sha256"],
                "preservation_all_passed": item["adapter_audit"]["preservation"]["all_passed"],
            }
        )
    return rows


def _baseline_rows(sequence_manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in sequence_manifest["sequences"]:
        campaign = item["campaign"]
        completion = campaign["completion"]
        inputs = campaign["inputs"]
        outputs = campaign["outputs"]
        metrics = campaign["metrics"]
        diagnostics = campaign.get("diagnostics", {})
        camera_enqueue = campaign["camera_enqueue_runtime"]
        camera_enqueue_observed = camera_enqueue["observed"]
        launch_command = campaign["commands"].get("roslaunch")
        rows.append(
            {
                "order": item["order"],
                "sequence": item["sequence"],
                "baseline_source_evidence": _compact_json(
                    item["baseline_source_evidence"]
                ),
                "baseline_source_commit": item["baseline_source_commit"],
                "baseline_source_tree": item["baseline_source_tree"],
                "status": campaign["status"],
                "passed": item["campaign_passed"],
                "error": _compact_json(campaign.get("error")),
                "command": _compact_json(launch_command),
                "source_bag_sha256": inputs["source_bag"]["sha256"],
                "adapted_bag_sha256": campaign["adapted_bag"]["sha256"],
                "config_sha256": inputs["config"]["sha256"],
                "kalibr_imu_chain_sha256": inputs["kalibr_imu_chain"]["sha256"],
                "kalibr_imucam_chain_sha256": inputs["kalibr_imucam_chain"]["sha256"],
                "launch_sha256": inputs["launch"]["sha256"],
                "binary_sha256": inputs["estimator_binary"]["sha256"],
                "reference_sha256": inputs["reference_tum"]["sha256"],
                "resolved_parameters": _compact_json(campaign["resolved_parameters"]),
                "process_exit_code": completion.get("roslaunch_exit_code"),
                "completed": completion.get("estimator_completed"),
                "reset_detected": completion.get("reset_detected"),
                "nonfinite_detected": completion.get("nonfinite_detected"),
                "timed_out": completion.get("timed_out"),
                "runtime_seconds": completion.get("roslaunch_runtime_seconds"),
                "dropped_message_count": completion.get("dropped_message_count"),
                "unpaired_warning_count": completion.get("unpaired_warning_count"),
                "exact_header_pair_count": camera_enqueue[
                    "exact_header_pairs"
                ],
                "queued_pair_count": camera_enqueue_observed["queued_pairs"],
                "processed_pair_count": camera_enqueue_observed[
                    "processed_pairs"
                ],
                "frequency_thinned_pair_count": camera_enqueue_observed[
                    "frequency_thinned_pairs"
                ],
                "camera0_decode_failure_count": camera_enqueue_observed[
                    "cam0_decode_failures"
                ],
                "camera1_decode_failure_count": camera_enqueue_observed[
                    "cam1_decode_failures"
                ],
                "pending_pair_count": camera_enqueue_observed["pending_pairs"],
                "camera_enqueue_accounting_passed": camera_enqueue[
                    "pair_accounting_complete"
                ],
                "camera_decode_failures_zero": camera_enqueue[
                    "decode_failures_zero"
                ],
                "processed_pairs_match_queued": camera_enqueue[
                    "processed_pairs_match_queued"
                ],
                "camera_queue_drained": camera_enqueue["queue_drained"],
                "state_output": _compact_json(outputs.get("state")),
                "trajectory_output": _compact_json(outputs.get("trajectory_tum")),
                "covariance_deviation_output": _compact_json(outputs.get("deviation")),
                "timing_output": _compact_json(outputs.get("timing")),
                "ate": _compact_json(metrics.get("ape_translation")),
                "translation_rpe": _compact_json(metrics.get("rpe_translation_1m")),
                "rotation_rpe": _compact_json(metrics.get("rpe_rotation_1m_deg")),
                "yaw": _compact_json(diagnostics.get("yaw")),
                "tilt": _compact_json(diagnostics.get("tilt")),
            }
        )
    return rows


def _compatibility_bytes(sequence_manifest: Mapping[str, Any]) -> bytes:
    classification = sequence_manifest["compatibility_classification"]
    passed = sum(item["campaign_passed"] for item in sequence_manifest["sequences"])
    rotation_fast = next(
        item for item in sequence_manifest["sequences"]
        if item["sequence"] == "rotation/rotation_fast.bag"
    )
    head_passed = [
        item["sequence"] for item in sequence_manifest["sequences"]
        if "_head.bag" in item["sequence"] and item["campaign_passed"]
    ]
    lines = [
        "# KAIST VIO compatibility report",
        "",
        "Evidence schema: `{}`".format(sequence_manifest["schema"]),
        "",
        "The frozen campaign contains all 11 official sequences in the required order. "
        "Ground truth was audited at the source boundary and omitted from every estimator-input bag.",
        "",
        "- Declared adapter required: `{}`".format(str(sequence_manifest["adapter_required"]).lower()),
        "- Baseline source commit: `{}`".format(
            sequence_manifest["baseline_source"]["source_commit"]
        ),
        "- Baseline source tree: `{}`".format(
            sequence_manifest["baseline_source"]["source_tree"]
        ),
        "- Baseline source evidence SHA-256: `{}`".format(
            sequence_manifest["baseline_source"]["evidence_identity"]["sha256"]
        ),
        "- Completed frozen baselines: `{}/11`".format(passed),
        "- `rotation/rotation_fast.bag` passed: `{}`".format(str(rotation_fast["campaign_passed"]).lower()),
        "- Passing head-rotation trajectories: `{}`".format(", ".join(head_passed) if head_passed else "none"),
        "- MH_01 deterministic parity: `{}`".format(str(sequence_manifest["mh01_parity"]["passed"]).lower()),
        "- Unsupported reasons: `{}`".format(_compact_json(sequence_manifest["unsupported_reasons"])),
        "- Engineering blockers: `{}`".format(_compact_json(sequence_manifest["engineering_blockers"])),
        "",
        "## Sequence outcomes",
        "",
        "| Order | Sequence | Baseline |",
        "|---:|---|---|",
    ]
    for item in sequence_manifest["sequences"]:
        lines.append("| {} | `{}` | {} |".format(
            item["order"], item["sequence"], "PASS" if item["campaign_passed"] else "FAIL"
        ))
    lines.extend(["", "## Classification", "", classification])
    payload = "\n".join(lines) + "\n"
    if [line for line in payload.splitlines() if line.strip()][-1] != classification:
        raise ReportError("internal error: compatibility classification is not the last nonempty line")
    if classification not in CLASSIFICATIONS:
        raise ReportError("internal error: invalid compatibility classification")
    return payload.encode("utf-8")


def build_outputs(
    download_evidence: Mapping[str, Any],
    evidence_index: Mapping[str, Any],
    roots: Sequence[Path],
) -> Mapping[str, bytes]:
    verifier = _IdentityVerifier(roots)
    download = _validate_download(download_evidence, verifier)
    sequences = _validate_index(evidence_index, roots, verifier)
    download["bags"] = [
        {"sequence": item["sequence"], "source_bag": item["source_bag"]}
        for item in sequences["sequences"]
    ]
    inventory = _csv_bytes(INVENTORY_COLUMNS, _inventory_rows(sequences))
    return {
        ARTIFACT_OUTPUT_NAMES["download"]: _json_bytes(download),
        ARTIFACT_OUTPUT_NAMES["inventory"]: inventory,
        ARTIFACT_OUTPUT_NAMES["compatibility"]: _compatibility_bytes(sequences),
        ARTIFACT_OUTPUT_NAMES["baseline"]: _csv_bytes(BASELINE_COLUMNS, _baseline_rows(sequences)),
        ARTIFACT_OUTPUT_NAMES["sequences"]: _json_bytes(sequences),
        DATASET_OUTPUT_NAMES["download"]: _json_bytes(download),
        DATASET_OUTPUT_NAMES["inventory"]: inventory,
    }


def _output_directory(value: Path, label: str) -> Path:
    if not value.is_absolute():
        raise ReportError("{} must be absolute".format(label))
    reason = _forbidden_path(value)
    if reason:
        raise ReportError("{} is forbidden: {}".format(label, reason))
    try:
        resolved = value.resolve(strict=True)
    except OSError as exc:
        raise ReportError("{} cannot be resolved: {}".format(label, exc)) from exc
    if not resolved.is_dir():
        raise ReportError("{} is not a directory".format(label))
    return resolved


def _write_atomic_group(outputs: Mapping[Path, bytes]) -> None:
    destinations = sorted(outputs, key=str)
    if len(destinations) != len(set(destinations)):
        raise ReportError("output destinations are not unique")
    existing = [path for path in destinations if path.exists()]
    if existing:
        raise ReportError("refusing to overwrite existing output: {}".format(existing[0]))
    staged: List[Tuple[Path, Path]] = []
    created: List[Path] = []
    try:
        for destination in destinations:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".{}-".format(destination.name),
                suffix=".tmp",
                dir=str(destination.parent),
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(outputs[destination])
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((temporary, destination))
        if any(destination.exists() for _, destination in staged):
            raise ReportError("an output appeared while evidence was being staged")
        for temporary, destination in staged:
            try:
                os.link(str(temporary), str(destination))
            except FileExistsError as exc:
                raise ReportError(
                    "refusing to overwrite output created during emission: {}".format(
                        destination
                    )
                ) from exc
            created.append(destination)
            temporary.unlink()
    except Exception:
        for destination in created:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for temporary, _ in staged:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        raise


def consolidate(
    download_path: Path,
    index_path: Path,
    input_roots: Sequence[Path],
    artifact_output_root: Path,
    dataset_manifest_root: Path,
) -> Mapping[str, str]:
    roots = _resolve_roots(input_roots)
    download = _read_json_path(download_path, "--download-evidence", roots)
    index = _read_json_path(index_path, "--evidence-index", roots)
    artifact_root = _output_directory(artifact_output_root, "--artifact-output-root")
    dataset_root = _output_directory(dataset_manifest_root, "--dataset-manifest-root")
    if artifact_root == dataset_root:
        raise ReportError("artifact and dataset manifest roots must differ")
    payloads = build_outputs(download, index, roots)
    destinations: Dict[Path, bytes] = {}
    for name in ARTIFACT_OUTPUT_NAMES.values():
        destinations[artifact_root / name] = payloads[name]
    destinations[dataset_root / DATASET_OUTPUT_NAMES["download"]] = payloads[DATASET_OUTPUT_NAMES["download"]]
    destinations[dataset_root / DATASET_OUTPUT_NAMES["inventory"]] = payloads[DATASET_OUTPUT_NAMES["inventory"]]
    _write_atomic_group(destinations)
    return {str(path): hashlib.sha256(payload).hexdigest() for path, payload in sorted(destinations.items(), key=lambda item: str(item[0]))}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Consolidate deterministic KAIST VIO Session-0.5 evidence.")
    parser.add_argument("--download-evidence", type=Path, required=True)
    parser.add_argument("--evidence-index", type=Path, required=True)
    parser.add_argument("--input-root", type=Path, action="append", required=True)
    parser.add_argument("--artifact-output-root", type=Path, required=True)
    parser.add_argument("--dataset-manifest-root", type=Path, required=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = consolidate(
            args.download_evidence,
            args.evidence_index,
            args.input_root,
            args.artifact_output_root,
            args.dataset_manifest_root,
        )
    except ReportError as exc:
        print("KAIST VIO report consolidation failed: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
