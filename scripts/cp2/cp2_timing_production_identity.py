#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free CP2-E production-closure identity candidate.

The final profile cannot exist until the privileged feasibility transaction
passes.  This module nevertheless makes every non-host-dependent binding
deterministic now: the three root-owned Python sources, the pre-Python
launcher, the fixed interpreter, and the sudoers fragment.  It emits a
canonical candidate receipt and validates a profile if one is supplied; it
never installs files, invokes sudo, opens recorded input, or changes a host
control.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SOURCE_FILES = (
    ("scripts/cp2/cp2_timing_privileged_helper.py",
     "/opt/schurvio-cp2e/lib/cp2_timing_privileged_helper.py"),
    ("scripts/cp2/cp2_timing_privileged_backend.py",
     "/opt/schurvio-cp2e/lib/cp2_timing_privileged_backend.py"),
    ("scripts/cp2/cp2_timing_profile.py",
     "/opt/schurvio-cp2e/lib/cp2_timing_profile.py"),
)
LAUNCHER_SOURCE = "scripts/cp2/cp2_timing_root_launcher.c"
SUDOERS_SOURCE = "packaging/cp2e/schurvio-cp2e.sudoers"
INSTALLED_LAUNCHER = "/opt/schurvio-cp2e/bin/cp2e-helper"
INSTALLED_PROFILE = "/opt/schurvio-cp2e/profile/cp2_timing_profile.yaml"
DEFAULT_INTERPRETER = "/usr/bin/python3.8"


class ProductionIdentityError(RuntimeError):
    """The candidate closure or supplied profile is not exact."""


def _fail(message: str) -> None:
    raise ProductionIdentityError(message)


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            value, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise ProductionIdentityError("candidate identity is not canonical JSON") from exc
    return payload


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _held_file(path: Path, label: str) -> Mapping[str, Any]:
    path = Path(path).absolute()
    if path == Path("/") or Path(os.path.normpath(str(path))) != path:
        _fail(label + " path is not normalized absolute syntax")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        status = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
            or named.st_dev != status.st_dev or named.st_ino != status.st_ino
            or named.st_nlink != 1 or status.st_size <= 0
        ):
            _fail(label + " is not one stable single-link regular inode")
        digest = hashlib.sha256()
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(
                descriptor, min(1024 * 1024, status.st_size - offset), offset,
            )
            if not chunk:
                _fail(label + " read ended early")
            digest.update(chunk)
            offset += len(chunk)
        following = os.fstat(descriptor)
        if (
            following.st_dev != status.st_dev or following.st_ino != status.st_ino
            or following.st_size != status.st_size or following.st_nlink != 1
        ):
            _fail(label + " identity changed while hashing")
        return {
            "device": status.st_dev, "gid": status.st_gid,
            "inode": status.st_ino, "mode_octal": format(
                stat.S_IMODE(status.st_mode), "04o",
            ),
            "nlink": status.st_nlink, "path": str(path),
            "sha256": digest.hexdigest(), "size_bytes": status.st_size,
            "uid": status.st_uid,
        }
    finally:
        os.close(descriptor)


def _closure_sha256(domain: str, rows: Sequence[Mapping[str, Any]]) -> str:
    record = {
        "domain": domain,
        "files": [
            {
                "destination": row["destination"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
            }
            for row in rows
        ],
    }
    return _sha_bytes(canonical_json_bytes(record))


def _reopen_exact_payload(identity: Mapping[str, Any], label: str) -> bytes:
    """Reopen a named candidate and require the previously held inode/hash."""

    path = Path(identity["path"])
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        status = os.fstat(descriptor)
        if (
            status.st_dev != identity["device"]
            or status.st_ino != identity["inode"]
            or status.st_size != identity["size_bytes"]
            or status.st_nlink != identity["nlink"]
        ):
            _fail(label + " inode changed between identity reads")
        chunks = []
        offset = 0
        while offset < status.st_size:
            chunk = os.pread(
                descriptor, min(1024 * 1024, status.st_size - offset), offset,
            )
            if not chunk:
                _fail(label + " read ended early")
            chunks.append(chunk)
            offset += len(chunk)
        payload = b"".join(chunks)
        following = os.fstat(descriptor)
        if (
            following.st_dev != status.st_dev
            or following.st_ino != status.st_ino
            or following.st_size != status.st_size
            or hashlib.sha256(payload).hexdigest() != identity["sha256"]
        ):
            _fail(label + " changed while its compiled binding was inspected")
        return payload
    finally:
        os.close(descriptor)


def _compiled_plan_from_launcher(payload: bytes) -> str:
    matches = re.findall(
        rb"CP2E_PROFILE_PLAN_SHA256=([0-9a-f]{64})\x00", payload,
    )
    if len(matches) != 1:
        _fail("launcher does not retain exactly one compiled profile-plan digest")
    return matches[0].decode("ascii")


def profile_plan_sha256(profile_value: Mapping[str, Any]) -> str:
    """Delegate to the held profile codec's nonrecursive projection."""

    try:
        import cp2_timing_profile as profile_codec
        return profile_codec.profile_plan_sha256(profile_value)
    except Exception as exc:
        raise ProductionIdentityError(
            "profile plan projection failed"
        ) from exc


def candidate_identity(
    repo_root: Path, launcher_binary: Path,
    interpreter: Path = Path(DEFAULT_INTERPRETER),
    profile_value: Optional[Mapping[str, Any]] = None,
    profile_path: Optional[Path] = None,
) -> Mapping[str, Any]:
    root = Path(repo_root).absolute()
    source_rows = []
    for relative, destination in SOURCE_FILES:
        identity = dict(_held_file(root / relative, relative))
        identity["destination"] = destination
        identity["source_relative_path"] = relative
        source_rows.append(identity)
    source_closure = _closure_sha256(
        "SchurVIO-CP2-E-root-Python-source-closure-v1", source_rows,
    )
    launcher_source = _held_file(root / LAUNCHER_SOURCE, LAUNCHER_SOURCE)
    launcher = _held_file(launcher_binary, "launcher binary")
    launcher_payload = _reopen_exact_payload(launcher, "launcher binary")
    compiled_plan = _compiled_plan_from_launcher(launcher_payload)
    sudoers = _held_file(root / SUDOERS_SOURCE, SUDOERS_SOURCE)
    python = _held_file(interpreter, "fixed Python interpreter")
    bindings: Dict[str, Any] = {
        "import_closure_sha256": source_closure,
        "installed_closure_file_count": len(source_rows) + 1,
        "plan_sha256": (
            compiled_plan if profile_value is None else profile_plan_sha256(profile_value)
        ),
        "protocol_core_sha256": source_rows[0]["sha256"],
        "root_launcher_path": INSTALLED_LAUNCHER,
        "root_launcher_sha256": launcher["sha256"],
        "source_sha256": source_closure,
        "sudoers_sha256": sudoers["sha256"],
    }
    profile_binding_valid: Optional[bool] = None
    if profile_value is not None:
        helper = profile_value["privileged_helper"]
        profile_binding_valid = compiled_plan == bindings["plan_sha256"] and all(
            helper.get(key) == expected for key, expected in bindings.items()
        )
    build_definitions = {
        "CP2E_BACKEND_SHA256": source_rows[1]["sha256"],
        "CP2E_HELPER_SHA256": source_rows[0]["sha256"],
        "CP2E_PROFILE_CODEC_SHA256": source_rows[2]["sha256"],
        "CP2E_PROFILE_PLAN_SHA256": compiled_plan,
        "CP2E_PYTHON": str(Path(interpreter).absolute()),
        "CP2E_PYTHON_SHA256": python["sha256"],
        "CP2E_TRUSTED_ROOT": "/opt/schurvio-cp2e",
    }
    compiled_values = (
        source_rows[0]["sha256"], source_rows[1]["sha256"],
        source_rows[2]["sha256"], python["sha256"], compiled_plan,
        str(Path(interpreter).absolute()), "/opt/schurvio-cp2e",
    )
    launcher_compiled_binding_valid = all(
        value.encode("ascii") in launcher_payload for value in compiled_values
    )
    profile_candidate = None
    if profile_value is not None:
        profile_payload = canonical_json_bytes(profile_value)
        profile_candidate = {
            "destination": INSTALLED_PROFILE,
            "path": None if profile_path is None else str(Path(profile_path).absolute()),
            "sha256": _sha_bytes(profile_payload),
            "size_bytes": len(profile_payload),
        }
        if profile_path is not None:
            held_profile = _held_file(Path(profile_path), "canonical profile candidate")
            if (
                held_profile["sha256"] != profile_candidate["sha256"]
                or held_profile["size_bytes"] != profile_candidate["size_bytes"]
            ):
                _fail("profile candidate mapping differs from its held inode")
            profile_candidate["held_inode"] = held_profile
    return {
        "bindings": bindings,
        "build_definitions": build_definitions,
        "checkpoint": "CP2-E",
        "launcher_binary": launcher,
        "launcher_compiled_binding_valid": launcher_compiled_binding_valid,
        "launcher_compiled_plan_sha256": compiled_plan,
        "launcher_source": launcher_source,
        "profile_binding_valid": profile_binding_valid,
        "profile_candidate": profile_candidate,
        "python_interpreter": python,
        "record_type": "cp2e_production_identity_candidate",
        "schema_version": 1,
        "source_closure": source_rows,
        "status": (
            "candidate_missing_root_held_profile_binding"
            if profile_value is None
            else "candidate_profile_binding_valid_privileged_feasibility_unproved"
            if profile_binding_valid and launcher_compiled_binding_valid
            else "candidate_profile_binding_mismatch"
        ),
        "sudoers": sudoers,
    }


def _load_profile(path: Path) -> Mapping[str, Any]:
    payload = Path(path).read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductionIdentityError("candidate profile is not strict JSON") from exc
    if type(value) is not dict or canonical_json_bytes(value) != payload:
        _fail("candidate profile is not canonical JSON")
    try:
        import cp2_timing_profile as profile_codec
        return profile_codec.load_profile_bytes(payload).value
    except Exception as exc:
        raise ProductionIdentityError(
            "candidate profile fails the complete timing-profile codec"
        ) from exc


def _write_new_profile(path: Path, payload: bytes) -> None:
    """Publish one candidate profile by held-parent O_EXCL plus fsync."""

    target = Path(path).absolute()
    if target == Path("/") or Path(os.path.normpath(str(target))) != target:
        _fail("bound profile output path is not normalized absolute syntax")
    parent = target.parent
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    directory_fd = os.open(str(parent), flags)
    descriptor = -1
    created_identity = None
    try:
        create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            create_flags |= os.O_NOFOLLOW
        descriptor = os.open(
            target.name, create_flags, 0o600, dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        created_identity = os.fstat(descriptor)
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail("bound profile candidate write made no progress")
            offset += count
        os.fsync(descriptor)
        following = os.fstat(descriptor)
        if (
            not stat.S_ISREG(following.st_mode) or following.st_nlink != 1
            or following.st_dev != created_identity.st_dev
            or following.st_ino != created_identity.st_ino
            or following.st_size != len(payload)
        ):
            _fail("bound profile candidate inode changed during publication")
        os.fsync(directory_fd)
    except BaseException:
        if descriptor >= 0 and created_identity is not None:
            try:
                named = os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    named.st_dev == created_identity.st_dev
                    and named.st_ino == created_identity.st_ino
                    and named.st_nlink == 1
                ):
                    os.unlink(target.name, dir_fd=directory_fd)
                    os.fsync(directory_fd)
            except BaseException:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def bind_profile_candidate(
    repo_root: Path, draft_path: Path, launcher_path: Path, output_path: Path,
) -> Mapping[str, Any]:
    """Resolve the nonrecursive launcher/profile digest cycle exactly once."""

    draft = _load_profile(draft_path)
    plan = profile_plan_sha256(draft)
    if draft["privileged_helper"]["plan_sha256"] != plan:
        _fail("draft profile plan digest differs")
    candidate = candidate_identity(
        repo_root, launcher_path, profile_value=draft, profile_path=draft_path,
    )
    if (
        not candidate["launcher_compiled_binding_valid"]
        or candidate["launcher_compiled_plan_sha256"] != plan
    ):
        _fail("launcher was not compiled from this draft plan/source closure")
    rebound = json.loads(canonical_json_bytes(draft))
    rebound["privileged_helper"].update(candidate["bindings"])
    payload = canonical_json_bytes(rebound)
    try:
        import cp2_timing_profile as profile_codec
        frozen = profile_codec.load_profile_bytes(payload)
    except Exception as exc:
        raise ProductionIdentityError("bound profile fails its complete codec") from exc
    if frozen.value["privileged_helper"]["plan_sha256"] != plan:
        _fail("recursive binding unexpectedly changed the compiled plan")
    _write_new_profile(output_path, payload)
    final_identity = candidate_identity(
        repo_root, launcher_path, profile_value=frozen.value,
        profile_path=output_path,
    )
    if not final_identity["profile_binding_valid"]:
        _fail("published bound profile does not close the launcher identity")
    return {
        "bound_profile": final_identity["profile_candidate"],
        "checkpoint": "CP2-E",
        "formal_execution_locked": True,
        "launcher_sha256": final_identity["launcher_binary"]["sha256"],
        "plan_sha256": plan,
        "profile_binding_valid": True,
        "record_type": "cp2e_bound_profile_candidate_receipt",
        "schema_version": 1,
        "status": "candidate_only_privileged_feasibility_unproved",
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    repo_root = Path(__file__).resolve().parents[2]
    if len(arguments) == 2 and arguments[0] == "--profile-plan":
        path = Path(arguments[1])
        if not path.is_absolute():
            _fail("profile-plan candidate path is not absolute")
        os.write(
            sys.stdout.fileno(),
            (profile_plan_sha256(_load_profile(path)) + "\n").encode("ascii"),
        )
        return 0
    if len(arguments) == 5 and arguments[0] == "--bind-profile":
        draft = Path(arguments[1])
        launcher = Path(arguments[2])
        if arguments[3] != "--output":
            _fail("bound profile output option differs")
        output = Path(arguments[4])
        if not all(path.is_absolute() for path in (draft, launcher, output)):
            _fail("bound profile paths must all be absolute")
        receipt = bind_profile_candidate(
            repo_root, draft, launcher, output,
        )
        os.write(sys.stdout.fileno(), canonical_json_bytes(receipt))
        return 0
    if len(arguments) not in (2, 4) or arguments[0] != "--candidate":
        _fail(
            "usage: cp2_timing_production_identity.py --candidate "
            "/absolute/launcher [--profile /absolute/profile] | "
            "--profile-plan /absolute/draft | --bind-profile "
            "/absolute/draft /absolute/launcher --output /absolute/final"
        )
    launcher = Path(arguments[1])
    if not launcher.is_absolute():
        _fail("launcher candidate path is not absolute")
    profile = None
    if len(arguments) == 4:
        if arguments[2] != "--profile" or not Path(arguments[3]).is_absolute():
            _fail("profile candidate argument differs")
        profile = _load_profile(Path(arguments[3]))
    receipt = candidate_identity(
        repo_root, launcher, profile_value=profile,
        profile_path=None if profile is None else Path(arguments[3]),
    )
    os.write(sys.stdout.fileno(), canonical_json_bytes(receipt))
    return 0 if receipt["profile_binding_valid"] is not False else 1


if __name__ == "__main__":
    main()


__all__ = [
    "ProductionIdentityError", "bind_profile_candidate", "candidate_identity",
    "canonical_json_bytes", "profile_plan_sha256",
]
