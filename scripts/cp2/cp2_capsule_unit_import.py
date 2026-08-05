#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Import the exact frozen CP2-D capsule pair into one unit-gate artifact.

This data-free helper is executed only from the source archive extracted by
``run_unit_gate.sh``.  It descriptor-binds the committed locator and the
private external capsule directory, copies the four externally identified
files without replacement, revalidates every source and destination inode,
and runs both retained synthetic preflights from two independent relocation
roots.  It never reads a registry, bag, trajectory, or recorded result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, Dict, Mapping, Sequence, Tuple


SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_capsule as capsule
import cp2_capsule_builder as builder
import cp2_schema as schema


LOCATOR_RELATIVE = "project/cp2_capsule_unit_import.json"
SOURCE_LOCK_RELATIVE = "project/cp2_capsule_source_lock.json"
RECEIPT_RELATIVE = "capsule_import_receipt.json"
MAX_LOCATOR_BYTES = 16 * 1024
MAX_PROFILE_BYTES = 16 * 1024 * 1024
COPY_CHUNK_BYTES = 1024 * 1024


class CapsuleImportError(RuntimeError):
    """Raised when the exact unit-capsule import cannot be established."""


def _fail(message: str) -> None:
    raise CapsuleImportError(message)


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii", "strict")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise CapsuleImportError("capsule import record is not canonical JSON") from exc


def _identity(status_value: os.stat_result) -> Tuple[int, ...]:
    return (
        status_value.st_dev,
        status_value.st_ino,
        status_value.st_mode,
        status_value.st_nlink,
        status_value.st_uid,
        status_value.st_gid,
        status_value.st_size,
        status_value.st_mtime_ns,
        status_value.st_ctime_ns,
    )


def _inode_record(status_value: os.stat_result) -> Mapping[str, int]:
    return {
        "device": status_value.st_dev,
        "inode": status_value.st_ino,
        "mode": status_value.st_mode,
        "link_count": status_value.st_nlink,
        "uid": status_value.st_uid,
        "gid": status_value.st_gid,
        "size_bytes": status_value.st_size,
        "mtime_ns": status_value.st_mtime_ns,
        "ctime_ns": status_value.st_ctime_ns,
    }


def _read_fd(descriptor: int, expected_size: int, maximum: int, label: str) -> bytes:
    if expected_size < 0 or expected_size > maximum:
        _fail(label + " is outside its byte bound")
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    remaining = expected_size
    while remaining:
        block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
        if not block:
            _fail(label + " became short")
        chunks.append(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        _fail(label + " grew while being read")
    return b"".join(chunks)


def _open_source_file(
    source_fd: int,
    filename: str,
    expected_size: int,
    expected_sha256: str,
) -> Tuple[int, os.stat_result]:
    descriptor = os.open(
        filename,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=source_fd,
    )
    try:
        before = os.fstat(descriptor)
        by_name = os.stat(filename, dir_fd=source_fd, follow_symlinks=False)
        if (
            _identity(before) != _identity(by_name)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size != expected_size
        ):
            _fail("external capsule source identity differs: " + filename)
        digest = hashlib.sha256()
        os.lseek(descriptor, 0, os.SEEK_SET)
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                _fail("external capsule source became short: " + filename)
            digest.update(block)
            remaining -= len(block)
        if os.read(descriptor, 1) or digest.hexdigest() != expected_sha256:
            _fail("external capsule source bytes differ: " + filename)
        if _identity(os.fstat(descriptor)) != _identity(before):
            _fail("external capsule source changed while hashing: " + filename)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, before
    except BaseException:
        os.close(descriptor)
        raise


def _copy_bound_file(
    source_fd: int,
    source_status: os.stat_result,
    destination_fd: int,
    filename: str,
    expected_sha256: str,
) -> Tuple[int, os.stat_result]:
    output = os.open(
        filename,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=destination_fd,
    )
    retained = False
    try:
        source_digest = hashlib.sha256()
        os.lseek(source_fd, 0, os.SEEK_SET)
        remaining = source_status.st_size
        while remaining:
            block = os.read(source_fd, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                _fail("external capsule source became short during copy: " + filename)
            source_digest.update(block)
            offset = 0
            while offset < len(block):
                written = os.write(output, block[offset:])
                if written <= 0:
                    _fail("unit capsule copy made no progress: " + filename)
                offset += written
            remaining -= len(block)
        if os.read(source_fd, 1) or source_digest.hexdigest() != expected_sha256:
            _fail("external capsule source changed during copy: " + filename)
        os.fchmod(output, 0o444)
        os.fsync(output)
        destination_status = os.fstat(output)
        by_name = os.stat(filename, dir_fd=destination_fd, follow_symlinks=False)
        if (
            _identity(destination_status) != _identity(by_name)
            or not stat.S_ISREG(destination_status.st_mode)
            or destination_status.st_uid != os.geteuid()
            or destination_status.st_nlink != 1
            or stat.S_IMODE(destination_status.st_mode) != 0o444
            or destination_status.st_size != source_status.st_size
        ):
            _fail("unit capsule destination identity differs: " + filename)
        os.lseek(output, 0, os.SEEK_SET)
        destination_digest = hashlib.sha256()
        remaining = destination_status.st_size
        while remaining:
            block = os.read(output, min(COPY_CHUNK_BYTES, remaining))
            if not block:
                _fail("unit capsule destination became short: " + filename)
            destination_digest.update(block)
            remaining -= len(block)
        if os.read(output, 1) or destination_digest.hexdigest() != expected_sha256:
            _fail("unit capsule destination bytes differ: " + filename)
        if _identity(os.fstat(output)) != _identity(destination_status):
            _fail("unit capsule destination changed during verification: " + filename)
        retained = True
        return output, destination_status
    finally:
        if not retained:
            os.close(output)


def _read_project_file(source_root: Path, relative: str, maximum: int) -> bytes:
    path = source_root.joinpath(*relative.split("/"))
    descriptor = os.open(
        str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        before = os.fstat(descriptor)
        by_name = os.stat(str(path), follow_symlinks=False)
        if (
            _identity(before) != _identity(by_name)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o022
        ):
            _fail("source-frozen capsule input identity differs: " + relative)
        payload = _read_fd(descriptor, before.st_size, maximum, relative)
        if _identity(os.fstat(descriptor)) != _identity(before):
            _fail("source-frozen capsule input changed: " + relative)
        return payload
    finally:
        os.close(descriptor)


def _validate_locator(payload: bytes) -> Mapping[str, Any]:
    try:
        record = schema.strict_json_loads(payload)
        record = schema.exact_object_keys(
            record,
            (
                "schema_version",
                "record_type",
                "checkpoint",
                "formal_execution_permitted_by_this_record",
                "source_directory",
                "expected_identity_path",
                "expected_identity_sha256",
            ),
            "capsule import locator",
        )
        identity_sha = schema.validate_sha256(
            record["expected_identity_sha256"],
            "capsule import expected identity SHA-256",
        )
    except schema.SchemaError as exc:
        raise CapsuleImportError("capsule import locator is invalid") from exc
    if (
        type(record["schema_version"]) is not int
        or record["schema_version"] != 1
        or record["record_type"] != "cp2_d_capsule_unit_import_locator"
        or record["checkpoint"] != "CP2-D"
        or record["formal_execution_permitted_by_this_record"] is not False
        or record["expected_identity_path"]
        != "project/cp2_capsule_expected_identity.json"
    ):
        _fail("capsule import locator header differs")
    source = record["source_directory"]
    if (
        type(source) is not str
        or not source
        or "\0" in source
        or not os.path.isabs(source)
        or os.path.normpath(source) != source
    ):
        _fail("capsule import source directory is not normalized absolute")
    if _canonical(record) != payload:
        _fail("capsule import locator is not canonical JSON")
    del identity_sha
    return record


def import_unit_capsules(source_root: Path, unit_artifact: Path) -> Mapping[str, Any]:
    """Perform one no-replace import and return its canonical receipt record."""

    source_root = Path(source_root).absolute()
    unit_artifact = Path(unit_artifact).absolute()
    if os.path.normpath(str(source_root)) != str(source_root):
        _fail("source root is not normalized absolute")
    if os.path.normpath(str(unit_artifact)) != str(unit_artifact):
        _fail("unit artifact root is not normalized absolute")
    locator_bytes = _read_project_file(source_root, LOCATOR_RELATIVE, MAX_LOCATOR_BYTES)
    locator = _validate_locator(locator_bytes)
    identity_bytes = _read_project_file(
        source_root, locator["expected_identity_path"], capsule.MAX_SOURCE_LOCK_BYTES
    )
    if hashlib.sha256(identity_bytes).hexdigest() != locator["expected_identity_sha256"]:
        _fail("capsule import locator expected-identity digest differs")
    source_lock_bytes = _read_project_file(
        source_root, SOURCE_LOCK_RELATIVE, capsule.MAX_SOURCE_LOCK_BYTES
    )
    try:
        identity_record = capsule.validate_expected_capsule_identity(
            schema.strict_json_loads(identity_bytes)
        )
        capsule.validate_embedded_source_lock(source_lock_bytes)
    except (schema.SchemaError, capsule.CapsuleError) as exc:
        raise CapsuleImportError("source-frozen capsule identity is invalid") from exc
    if capsule.expected_capsule_identity_bytes(identity_record) != identity_bytes:
        _fail("source-frozen capsule identity is not canonical JSON")
    retained_lock = identity_record["embedded_source_lock"]
    if (
        retained_lock["path"] != SOURCE_LOCK_RELATIVE
        or retained_lock["size_bytes"] != len(source_lock_bytes)
        or retained_lock["sha256"] != hashlib.sha256(source_lock_bytes).hexdigest()
    ):
        _fail("source-frozen capsule identity does not bind its source lock")

    source_directory = Path(locator["source_directory"])
    source_directory_fd = capsule._open_absolute_directory(
        source_directory, "external capsule source directory"
    )
    unit_fd = capsule._open_absolute_directory(unit_artifact, "unit artifact root")
    source_files: Dict[str, Tuple[int, os.stat_result, str, str]] = {}
    destination_files: Dict[str, Tuple[int, os.stat_result, str]] = {}
    capsules_fd = -1
    source_directory_identity = None
    try:
        source_directory_status = capsule._require_private_directory_fd(
            source_directory_fd, "external capsule source directory"
        )
        source_directory_identity = _identity(source_directory_status)
        unit_status = capsule._require_private_directory_fd(
            unit_fd, "unit artifact root"
        )
        del unit_status
        for kind in ("direct_math", "evaluator"):
            pair = identity_record["unit_artifact_members"][kind]
            for label in ("archive", "profile"):
                expected = pair[label]
                filename = Path(expected["path"]).name
                if filename in source_files:
                    _fail("external capsule identity repeats a filename: " + filename)
                descriptor, status_value = _open_source_file(
                    source_directory_fd,
                    filename,
                    expected["size_bytes"],
                    expected["sha256"],
                )
                source_files[filename] = (
                    descriptor,
                    status_value,
                    expected["path"],
                    expected["sha256"],
                )
        if set(source_files) != {
            "direct_math.cp2cap",
            "direct_math.profile.json",
            "evaluator.cp2cap",
            "evaluator.profile.json",
        }:
            _fail("external capsule identity filename inventory differs")
        try:
            os.stat("capsules", dir_fd=unit_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("unit capsule destination already exists")
        os.mkdir("capsules", 0o700, dir_fd=unit_fd)
        os.fsync(unit_fd)
        capsules_fd = os.open(
            "capsules",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=unit_fd,
        )
        capsule._require_private_directory_fd(capsules_fd, "unit capsule directory")
        imported: Dict[str, Dict[str, Any]] = {"direct_math": {}, "evaluator": {}}
        for filename, (
            descriptor,
            source_status,
            unit_relative,
            expected_sha,
        ) in source_files.items():
            destination_descriptor, destination_status = _copy_bound_file(
                descriptor,
                source_status,
                capsules_fd,
                filename,
                expected_sha,
            )
            destination_files[filename] = (
                destination_descriptor,
                destination_status,
                expected_sha,
            )
            kind = "direct_math" if filename.startswith("direct_math.") else "evaluator"
            label = "archive" if filename.endswith(".cp2cap") else "profile"
            imported[kind][label] = {
                "path": unit_relative,
                "size_bytes": source_status.st_size,
                "sha256": expected_sha,
                "source_identity": _inode_record(source_status),
                "unit_identity": _inode_record(destination_status),
            }
        os.fsync(capsules_fd)

        contract_bindings = []
        for relative in capsule.CONTRACT_BINDING_PATHS:
            payload = _read_project_file(
                source_root, relative, capsule.MAX_SOURCE_LOCK_BYTES
            )
            contract_bindings.append(
                {"path": relative, "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
            )

        for kind in ("direct_math", "evaluator"):
            profile_filename = kind + ".profile.json"
            profile_descriptor, profile_status, _ = destination_files[
                profile_filename
            ]
            profile_bytes = _read_fd(
                profile_descriptor,
                profile_status.st_size,
                MAX_PROFILE_BYTES,
                kind + " imported profile",
            )
            try:
                profile_record = schema.strict_json_loads(profile_bytes)
                profile = capsule.validate_capsule_profile(profile_record)
            except (schema.SchemaError, capsule.CapsuleError) as exc:
                raise CapsuleImportError(kind + " imported profile is invalid") from exc
            if (
                profile.canonical_bytes != profile_bytes
                or profile.capsule_kind != kind
                or profile.archive_size != imported[kind]["archive"]["size_bytes"]
                or profile.archive_sha256 != imported[kind]["archive"]["sha256"]
                or profile.profile_sha256 != imported[kind]["profile"]["sha256"]
                or profile.source_lock_size != len(source_lock_bytes)
                or profile.source_lock_sha256 != hashlib.sha256(source_lock_bytes).hexdigest()
                or [
                    {"path": item.path, "size": item.size, "sha256": item.sha256}
                    for item in profile.contract_bindings
                ]
                != contract_bindings
            ):
                _fail(kind + " imported profile/source identity differs")
            rehearsals = builder.relocation_rehearsal(
                kind,
                destination_files[kind + ".cp2cap"][0],
                profile_record,
                {
                    "version": profile_record["version_probe"],
                    "preflight": profile_record["synthetic_preflight"],
                },
            )
            imported[kind].update(
                {
                    "profile_id": profile.profile_id,
                    "inventory_sha256": profile.inventory_sha256,
                    "environment_sha256": profile.environment_sha256,
                    "native_consumers_sha256": profile.native_closure.consumers_sha256,
                    "native_edges_sha256": profile.native_closure.edges_sha256,
                    "numerical_runtime_sha256": profile.numerical_runtime.sha256,
                    "version_probe": profile_record["version_probe"],
                    "synthetic_preflight": profile_record["synthetic_preflight"],
                    "relocation_rehearsals": list(rehearsals),
                }
            )

        for filename, (descriptor, before, _, expected_sha) in source_files.items():
            by_name = os.stat(
                filename, dir_fd=source_directory_fd, follow_symlinks=False
            )
            if _identity(os.fstat(descriptor)) != _identity(before) or _identity(by_name) != _identity(before):
                _fail("external capsule source changed across import: " + filename)
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
                if not block:
                    _fail("external capsule source became short after import: " + filename)
                digest.update(block)
                remaining -= len(block)
            if os.read(descriptor, 1) or digest.hexdigest() != expected_sha:
                _fail("external capsule source bytes changed across import: " + filename)
        for filename, (
            descriptor,
            before,
            expected_sha,
        ) in destination_files.items():
            by_name = os.stat(
                filename, dir_fd=capsules_fd, follow_symlinks=False
            )
            if (
                _identity(os.fstat(descriptor)) != _identity(before)
                or _identity(by_name) != _identity(before)
            ):
                _fail("unit capsule destination changed across import: " + filename)
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = os.read(descriptor, min(COPY_CHUNK_BYTES, remaining))
                if not block:
                    _fail("unit capsule destination became short after import: " + filename)
                digest.update(block)
                remaining -= len(block)
            if os.read(descriptor, 1) or digest.hexdigest() != expected_sha:
                _fail("unit capsule destination bytes changed across import: " + filename)
        fresh_source_fd = capsule._open_absolute_directory(
            source_directory, "external capsule source directory"
        )
        try:
            if _identity(os.fstat(fresh_source_fd)) != source_directory_identity:
                _fail("external capsule source directory path changed across import")
        finally:
            os.close(fresh_source_fd)

        receipt = {
            "schema_version": 1,
            "record_type": "cp2_d_unit_capsule_import_receipt",
            "checkpoint": "CP2-D",
            "data_free": True,
            "recorded_input_accessed": False,
            "expected_identity": {
                "path": locator["expected_identity_path"],
                "size_bytes": len(identity_bytes),
                "sha256": hashlib.sha256(identity_bytes).hexdigest(),
            },
            "source_lock": {
                "path": SOURCE_LOCK_RELATIVE,
                "size_bytes": len(source_lock_bytes),
                "sha256": hashlib.sha256(source_lock_bytes).hexdigest(),
            },
            "source_locator": {
                "path": LOCATOR_RELATIVE,
                "sha256": hashlib.sha256(locator_bytes).hexdigest(),
                "source_directory": str(source_directory),
                "source_directory_identity": _inode_record(source_directory_status),
            },
            "unit_artifact_members": imported,
            "passed": True,
        }
        receipt_bytes = _canonical(receipt)
        receipt_path = unit_artifact / RECEIPT_RELATIVE
        receipt_fd = os.open(
            receipt_path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=unit_fd,
        )
        try:
            offset = 0
            while offset < len(receipt_bytes):
                count = os.write(receipt_fd, receipt_bytes[offset:])
                if count <= 0:
                    _fail("capsule import receipt write made no progress")
                offset += count
            os.fchmod(receipt_fd, 0o444)
            os.fsync(receipt_fd)
        finally:
            os.close(receipt_fd)
        os.fsync(unit_fd)
        return receipt
    except (OSError, capsule.CapsuleError, builder.CapsuleBuildError) as exc:
        if isinstance(exc, CapsuleImportError):
            raise
        raise CapsuleImportError("unit capsule import failed closed: " + str(exc)) from exc
    finally:
        if capsules_fd >= 0:
            os.close(capsules_fd)
        for descriptor, _, _, _ in source_files.values():
            os.close(descriptor)
        for descriptor, _, _ in destination_files.values():
            os.close(descriptor)
        os.close(unit_fd)
        os.close(source_directory_fd)


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--unit-artifact", required=True)
    options = parser.parse_args(arguments)
    receipt = import_unit_capsules(
        Path(options.source_root), Path(options.unit_artifact)
    )
    print(_canonical(receipt).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(os.sys.argv[1:])))
