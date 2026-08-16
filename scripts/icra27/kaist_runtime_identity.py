#!/usr/bin/python3
"""Fail-closed runtime identity validation for the KAIST U0/S1 screen.

The protocol pins the estimator executables, but an ELF executable is only one
part of the code that runs.  This helper also pins the locally built OpenVINS
libraries and Ceres selected by the dynamic loader.  For S1 it independently
revalidates every runtime artifact declared by CP0_BUILD_PROVENANCE.json.

The public integration point is ``validate_runtime_identity``.  Its return
value is JSON serializable and contains no environment values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA = "schurvio.icra27.kaist_runtime_identity.v1"
POLICY_ID = "g05-kaist-u0-s1-runtime-20260815-v1"
LDD = Path("/usr/bin/ldd")
READELF = Path("/usr/bin/readelf")
FORBIDDEN_LOADER_ENV = (
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_DEBUG_OUTPUT",
    "LD_PRELOAD",
    "LD_PROFILE",
    "LD_TRACE_LOADED_OBJECTS",
)


PINNED_RUNTIME_POLICY: Dict[str, Any] = {
    "policy_id": POLICY_ID,
    "approved_external_roots": [
        "/usr/lib",
        "/opt/ros/noetic/lib",
    ],
    "systems": {
        "S1": {
            "executable": {
                "loader_path": (
                    "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/devel/"
                    "lib/ov_msckf/ros1_serial_msckf"
                ),
                "canonical_path": (
                    "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/devel/"
                    "lib/ov_msckf/ros1_serial_msckf"
                ),
                "size_bytes": 37908552,
                "sha256": (
                    "8de5970c9654c1bb5c2fcdc5646fd5dd8675198d4ba41c6bdae8e5edba2b80ed"
                ),
                "build_id": "5e2538a1e79e913df3c3e288d083df4f683099e3",
            },
            "critical_dependencies": {
                "libov_msckf_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_msckf_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_msckf_lib.so"
                    ),
                    "size_bytes": 286177800,
                    "sha256": (
                        "1c65c441d3e06d24fc107595dc33bce600d426931aa28c4bead0c5b24b0d4aa2"
                    ),
                    "build_id": "33a9d8558942b6ed1363fe52bb1d55ed62517e2b",
                },
                "libov_core_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_core_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_core_lib.so"
                    ),
                    "size_bytes": 89212536,
                    "sha256": (
                        "c800d30905fa856fe033dcd263c97615a75d94a1016a1d9eee7a4656d2c1db5e"
                    ),
                    "build_id": "34fd8b7ad7414649db4cc93841917f70054e5eb9",
                },
                "libov_init_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_init_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                        "devel/lib/libov_init_lib.so"
                    ),
                    "size_bytes": 101191384,
                    "sha256": (
                        "01c1437794dd0e9777a8da8979ddb069ed16e5bd5a715f5827b8d777da264edf"
                    ),
                    "build_id": "c2ea0d725c3845cd0ff84fece1a6d909211c29d4",
                },
                "libceres.so.1": {
                    "loader_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/vendor/"
                        "ceres-install/lib/libceres.so.1"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-lite-icra27-a0/build/vendor/"
                        "ceres-install/lib/libceres.so.1.14.0"
                    ),
                    "size_bytes": 3294168,
                    "sha256": (
                        "24cfda7121265b24ede3a3a853920a2413f531c772ad0f3cc990a7f621212a33"
                    ),
                    "build_id": "6b0bb3df363dd2c5651906ae7772143fc8de8579",
                },
            },
            "provenance": {
                "path": (
                    "/home/moksh/schurvio-lite-icra27-a0/build/cp0-ws/"
                    "CP0_BUILD_PROVENANCE.json"
                ),
                "size_bytes": 41578,
                "sha256": (
                    "87b88fbe67273153f3a5c079a8d8f2e9a9b98fbce789b6f35e242af1e6949e0c"
                ),
                "schema_version": 1,
                "source_commit": "4d2f2d275437ead9496b848730d5a9eb0e402864",
                "runtime_dependency_sonames": [
                    "libov_core_lib.so",
                    "libov_init_lib.so",
                    "libov_msckf_lib.so",
                ],
                "ceres_soname": "libceres.so.1",
            },
        },
        "U0": {
            "executable": {
                "loader_path": (
                    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                    "build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
                ),
                "canonical_path": (
                    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                    "build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
                ),
                "size_bytes": 38136136,
                "sha256": (
                    "c0e2203d0c01822fd089b32fb25d2b81dc850abc03c273d169b812801788d57b"
                ),
                "build_id": "49428bb7cf13fe81ff529b23f197d944a5dacd20",
            },
            "critical_dependencies": {
                "libov_msckf_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_msckf_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_msckf_lib.so"
                    ),
                    "size_bytes": 239694000,
                    "sha256": (
                        "26a02314cfd6a2db29bcd61f817c07af0b7abd17dee64c28d0f98dce23b8d3a8"
                    ),
                    "build_id": "c2695242e23fd9316624c5c8cfe9d4749da81f72",
                },
                "libov_core_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_core_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_core_lib.so"
                    ),
                    "size_bytes": 93383392,
                    "sha256": (
                        "d126c2617cad9f5c632bedd600068786ce01941d4af2fbc11942004ee559db8d"
                    ),
                    "build_id": "7eb9810cf2165f6a39502e71d7fd5bf5a5c09ae7",
                },
                "libov_init_lib.so": {
                    "loader_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_init_lib.so"
                    ),
                    "canonical_path": (
                        "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
                        "build/open_vins_ws/devel/lib/libov_init_lib.so"
                    ),
                    "size_bytes": 111335584,
                    "sha256": (
                        "fb1c037bcca03b044db32e2e05a3d0951b62d32f1137e61fb8647c271c8ac1d3"
                    ),
                    "build_id": "49bb7eb1cc932bc1284c704e85dc4f6f9173797c",
                },
                "libceres.so.1": {
                    "loader_path": (
                        "/home/moksh/newSlam variant/build/vendor/ceres-install/"
                        "lib/libceres.so.1"
                    ),
                    "canonical_path": (
                        "/home/moksh/newSlam variant/build/vendor/ceres-install/"
                        "lib/libceres.so.1.14.0"
                    ),
                    "size_bytes": 3294168,
                    "sha256": (
                        "bec3b211a1fda35a44986a4000b666e4d218d84190b545805461b1652e086aad"
                    ),
                    "build_id": "b8b3ea689da20dd04fe174e6df812556c0440ee6",
                },
            },
        },
    },
}


class RuntimeIdentityError(RuntimeError):
    """A runtime input failed its pinned identity contract."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _path_is_below(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _strict_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeIdentityError(f"duplicate JSON key in build provenance: {key}")
        result[key] = value
    return result


def _elf_build_id(path: Path) -> str:
    if not READELF.is_file() or not os.access(str(READELF), os.X_OK):
        raise RuntimeIdentityError(f"readelf is unavailable: {READELF}")
    result = subprocess.run(
        [str(READELF), "-n", "--", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={"LANG": "C", "LC_ALL": "C"},
        timeout=30.0,
    )
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeIdentityError(f"readelf failed for {path}: {detail}")
    build_ids = re.findall(r"^\s*Build ID:\s*([0-9a-fA-F]+)\s*$", result.stdout, re.M)
    if len(build_ids) != 1:
        raise RuntimeIdentityError(
            f"expected exactly one GNU Build ID in {path}, observed {len(build_ids)}"
        )
    return build_ids[0].lower()


class _FileInspector:
    """Hash each large artifact once per validation call."""

    def __init__(self) -> None:
        self._cache: Dict[str, Dict[str, Any]] = {}

    def inspect(self, path: Path, include_build_id: bool = False) -> Dict[str, Any]:
        try:
            resolved = path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise RuntimeIdentityError(f"runtime artifact is missing: {path}") from exc
        key = str(resolved)
        cached = self._cache.get(key)
        if cached is None:
            with resolved.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise RuntimeIdentityError(f"not a regular file: {resolved}")
                digest = hashlib.sha256()
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                after = os.fstat(stream.fileno())
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RuntimeIdentityError(f"runtime artifact changed while hashing: {resolved}")
            cached = {
                "canonical_path": key,
                "size_bytes": before.st_size,
                "mtime_ns": before.st_mtime_ns,
                "sha256": digest.hexdigest(),
            }
            self._cache[key] = cached
        if include_build_id and "build_id" not in cached:
            cached["build_id"] = _elf_build_id(resolved)
        return dict(cached)


def _require_string(mapping: Mapping[str, Any], field: str, label: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeIdentityError(f"{label} has invalid {field}")
    return value


def _validate_file_pin(
    pin: Mapping[str, Any],
    inspector: _FileInspector,
    label: str,
    loader_path: Optional[str] = None,
    require_build_id: bool = True,
) -> Dict[str, Any]:
    expected_loader = _require_string(pin, "loader_path", label)
    selected_loader = loader_path if loader_path is not None else expected_loader
    if selected_loader != expected_loader:
        raise RuntimeIdentityError(
            f"{label} loader path drift: {selected_loader} != {expected_loader}"
        )
    if not Path(selected_loader).is_absolute():
        raise RuntimeIdentityError(f"{label} loader path is not absolute")
    expected_canonical = _require_string(pin, "canonical_path", label)
    observed = inspector.inspect(Path(selected_loader), include_build_id=require_build_id)
    if observed["canonical_path"] != expected_canonical:
        raise RuntimeIdentityError(
            f"{label} canonical path drift: "
            f"{observed['canonical_path']} != {expected_canonical}"
        )
    for field in ("size_bytes", "sha256"):
        if field not in pin or observed[field] != pin[field]:
            raise RuntimeIdentityError(
                f"{label} {field} drift: {observed[field]} != {pin.get(field)}"
            )
    if require_build_id:
        expected_build_id = _require_string(pin, "build_id", label).lower()
        if observed["build_id"] != expected_build_id:
            raise RuntimeIdentityError(
                f"{label} Build ID drift: "
                f"{observed['build_id']} != {expected_build_id}"
            )
    observed["loader_path"] = selected_loader
    return observed


_LDD_RESOLVED = re.compile(
    r"^\s*(?P<soname>\S+)\s+=>\s+(?P<path>/.*?)\s+"
    r"\((?:0x)?[0-9a-fA-F]+\)\s*$"
)
_LDD_MISSING = re.compile(r"^\s*(?P<soname>\S+)\s+=>\s+not found\s*$")
_LDD_DIRECT = re.compile(
    r"^\s*(?P<path>/.*?)\s+\((?:0x)?[0-9a-fA-F]+\)\s*$"
)
_LDD_VIRTUAL = re.compile(
    r"^\s*(?P<soname>linux-(?:vdso|gate)\.so\.\d+)\s+"
    r"\((?:0x)?[0-9a-fA-F]+\)\s*$"
)


def _parse_ldd_output(text: str) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    resolved: Dict[str, Dict[str, str]] = {}
    virtual: List[str] = []
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        if not raw_line.strip():
            continue
        missing = _LDD_MISSING.match(raw_line)
        if missing:
            raise RuntimeIdentityError(
                f"dynamic dependency is unresolved: {missing.group('soname')}"
            )
        match = _LDD_RESOLVED.match(raw_line)
        if match:
            soname = match.group("soname")
            loader_path = match.group("path")
        else:
            match = _LDD_DIRECT.match(raw_line)
            if match:
                loader_path = match.group("path")
                soname = Path(loader_path).name
            else:
                virtual_match = _LDD_VIRTUAL.match(raw_line)
                if virtual_match:
                    virtual.append(virtual_match.group("soname"))
                    continue
                raise RuntimeIdentityError(
                    f"unrecognized ldd output at line {line_number}: {raw_line!r}"
                )
        if soname in resolved:
            raise RuntimeIdentityError(f"duplicate dependency in ldd output: {soname}")
        path = Path(loader_path)
        if not path.is_absolute():
            raise RuntimeIdentityError(f"ldd returned a non-absolute path: {loader_path}")
        try:
            canonical = path.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as exc:
            raise RuntimeIdentityError(
                f"ldd dependency target is missing: {loader_path}"
            ) from exc
        if not canonical.is_file():
            raise RuntimeIdentityError(f"ldd dependency is not a file: {canonical}")
        resolved[soname] = {
            "loader_path": loader_path,
            "canonical_path": str(canonical),
        }
    if not resolved:
        raise RuntimeIdentityError("ldd returned no resolved dependencies")
    return resolved, sorted(virtual)


def _resolve_dynamic_libraries(
    executable: Path, environment: Optional[Mapping[str, str]]
) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    if not LDD.is_file() or not os.access(str(LDD), os.X_OK):
        raise RuntimeIdentityError(f"ldd is unavailable: {LDD}")
    source = os.environ if environment is None else environment
    command_environment: Dict[str, str] = {}
    for key, value in source.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeIdentityError("ldd environment must contain only strings")
        command_environment[key] = value
    for key in FORBIDDEN_LOADER_ENV:
        if command_environment.get(key):
            raise RuntimeIdentityError(f"forbidden loader environment variable: {key}")
    command_environment["LANG"] = "C"
    command_environment["LC_ALL"] = "C"
    result = subprocess.run(
        [str(LDD), str(executable)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=command_environment,
        timeout=30.0,
    )
    if result.returncode != 0 or result.stderr.strip():
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise RuntimeIdentityError(f"ldd failed for {executable}: {detail}")
    return _parse_ldd_output(result.stdout)


def _validate_declared_artifact(
    record: Any, inspector: _FileInspector, label: str
) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise RuntimeIdentityError(f"{label} is not an object")
    path_text = _require_string(record, "path", label)
    path = Path(path_text)
    if not path.is_absolute():
        raise RuntimeIdentityError(f"{label} path is not absolute")
    observed = inspector.inspect(path)
    if observed["canonical_path"] != path_text:
        raise RuntimeIdentityError(
            f"{label} path is not canonical: {path_text} -> {observed['canonical_path']}"
        )
    for field in ("size_bytes", "mtime_ns", "sha256"):
        if field not in record or observed[field] != record[field]:
            raise RuntimeIdentityError(
                f"{label} {field} drift: {observed[field]} != {record.get(field)}"
            )
    return observed


def _same_artifact(
    left: Mapping[str, Any], right: Mapping[str, Any]
) -> bool:
    """Compare content identity while ignoring evidence-only ELF/loader fields."""

    fields = ("canonical_path", "size_bytes", "mtime_ns", "sha256")
    return all(left.get(field) == right.get(field) for field in fields)


def _validate_s1_provenance(
    system_policy: Mapping[str, Any],
    inspector: _FileInspector,
    executable_identity: Mapping[str, Any],
    dependency_identities: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    provenance_policy = system_policy.get("provenance")
    if not isinstance(provenance_policy, dict):
        raise RuntimeIdentityError("S1 provenance policy is absent")
    provenance_path = Path(_require_string(provenance_policy, "path", "S1 provenance"))
    provenance_pin = {
        "loader_path": str(provenance_path),
        "canonical_path": str(provenance_path),
        "size_bytes": provenance_policy.get("size_bytes"),
        "sha256": provenance_policy.get("sha256"),
    }
    provenance_identity = _validate_file_pin(
        provenance_pin,
        inspector,
        "S1 build provenance",
        require_build_id=False,
    )
    raw = provenance_path.read_bytes()
    if _sha256_bytes(raw) != provenance_identity["sha256"]:
        raise RuntimeIdentityError("S1 build provenance changed while reading")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeIdentityError("S1 build provenance is not strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeIdentityError("S1 build provenance root is not an object")
    if value.get("schema_version") != provenance_policy.get("schema_version"):
        raise RuntimeIdentityError("S1 build provenance schema version drift")
    if value.get("source_commit") != provenance_policy.get("source_commit"):
        raise RuntimeIdentityError("S1 build provenance source commit drift")

    records = value.get("runtime_artifacts")
    if not isinstance(records, list) or not records:
        raise RuntimeIdentityError("S1 build provenance has no runtime artifacts")
    declared: Dict[str, Dict[str, Any]] = {}
    for index, record in enumerate(records):
        observed = _validate_declared_artifact(
            record, inspector, f"S1 runtime artifact {index}"
        )
        path = observed["canonical_path"]
        if path in declared:
            raise RuntimeIdentityError(f"duplicate S1 runtime artifact: {path}")
        declared[path] = observed

    dependency_sonames = provenance_policy.get("runtime_dependency_sonames")
    if not isinstance(dependency_sonames, list) or not all(
        isinstance(name, str) for name in dependency_sonames
    ):
        raise RuntimeIdentityError("invalid S1 provenance runtime dependency policy")
    expected_runtime_paths = {executable_identity["canonical_path"]}
    for soname in dependency_sonames:
        identity = dependency_identities.get(soname)
        if identity is None:
            raise RuntimeIdentityError(
                f"S1 provenance dependency is absent from ldd: {soname}"
            )
        expected_runtime_paths.add(identity["canonical_path"])
    if set(declared) != expected_runtime_paths:
        raise RuntimeIdentityError(
            "S1 runtime artifact set drift: "
            f"{sorted(declared)} != {sorted(expected_runtime_paths)}"
        )

    estimator_record = _validate_declared_artifact(
        value.get("estimator_executable"), inspector, "S1 declared estimator"
    )
    if not _same_artifact(estimator_record, executable_identity):
        raise RuntimeIdentityError("S1 declared estimator does not match executed ELF")
    ceres_soname = provenance_policy.get("ceres_soname")
    if not isinstance(ceres_soname, str) or ceres_soname not in dependency_identities:
        raise RuntimeIdentityError("S1 Ceres provenance policy is invalid")
    ceres_record = _validate_declared_artifact(
        value.get("ceres_library"), inspector, "S1 declared Ceres"
    )
    if not _same_artifact(ceres_record, dependency_identities[ceres_soname]):
        raise RuntimeIdentityError("S1 declared Ceres does not match loader-selected ELF")
    return {
        "file": provenance_identity,
        "schema_version": value["schema_version"],
        "source_commit": value["source_commit"],
        "runtime_artifact_count": len(declared),
        "runtime_artifacts": [declared[path] for path in sorted(declared)],
        "estimator_executable_verified": True,
        "ceres_library_verified": True,
    }


def _policy_fingerprint(policy: Mapping[str, Any], system: str) -> str:
    material = {
        "policy_id": policy.get("policy_id"),
        "approved_external_roots": policy.get("approved_external_roots"),
        "system": system,
        "system_policy": policy.get("systems", {}).get(system),
    }
    return _sha256_bytes(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def validate_runtime_identity(
    system: str,
    environment: Optional[Mapping[str, str]] = None,
    policy: Mapping[str, Any] = PINNED_RUNTIME_POLICY,
) -> Dict[str, Any]:
    """Validate and return the complete pinned runtime identity for one arm.

    ``environment`` should be the same sanitized, workspace-sourced environment
    that will launch the estimator.  It is used only for ldd and is never
    included in the returned evidence.
    """

    systems = policy.get("systems")
    if not isinstance(systems, dict) or system not in systems:
        raise RuntimeIdentityError(f"unknown runtime-identity system: {system}")
    system_policy = systems[system]
    if not isinstance(system_policy, dict):
        raise RuntimeIdentityError(f"invalid runtime policy for {system}")
    if policy.get("policy_id") != POLICY_ID and policy is PINNED_RUNTIME_POLICY:
        raise RuntimeIdentityError("built-in runtime policy ID drift")

    inspector = _FileInspector()
    executable_pin = system_policy.get("executable")
    if not isinstance(executable_pin, dict):
        raise RuntimeIdentityError(f"{system} executable policy is absent")
    executable_identity = _validate_file_pin(
        executable_pin, inspector, f"{system} executable"
    )
    if not os.access(executable_identity["canonical_path"], os.X_OK):
        raise RuntimeIdentityError(f"{system} executable is not executable")

    resolutions, virtual = _resolve_dynamic_libraries(
        Path(executable_pin["loader_path"]), environment
    )
    critical_policy = system_policy.get("critical_dependencies")
    if not isinstance(critical_policy, dict) or not critical_policy:
        raise RuntimeIdentityError(f"{system} critical dependency policy is absent")
    missing = set(critical_policy) - set(resolutions)
    if missing:
        raise RuntimeIdentityError(
            f"{system} critical dependencies are absent from ldd: {sorted(missing)}"
        )

    external_roots_value = policy.get("approved_external_roots")
    if not isinstance(external_roots_value, list) or not external_roots_value:
        raise RuntimeIdentityError("approved external loader roots are absent")
    external_roots = [Path(root).resolve(strict=True) for root in external_roots_value]
    critical_identities: Dict[str, Dict[str, Any]] = {}
    for soname, resolution in sorted(resolutions.items()):
        if soname in critical_policy:
            pin = critical_policy[soname]
            if not isinstance(pin, dict):
                raise RuntimeIdentityError(f"invalid dependency policy: {soname}")
            identity = _validate_file_pin(
                pin,
                inspector,
                f"{system} {soname}",
                loader_path=resolution["loader_path"],
            )
            if identity["canonical_path"] != resolution["canonical_path"]:
                raise RuntimeIdentityError(
                    f"{system} {soname} changed between ldd and hashing"
                )
            critical_identities[soname] = identity
            continue
        canonical = Path(resolution["canonical_path"])
        if not any(_path_is_below(canonical, root) for root in external_roots):
            raise RuntimeIdentityError(
                f"{system} unpinned dependency outside approved roots: "
                f"{soname} -> {canonical}"
            )
    if set(critical_identities) != set(critical_policy):
        raise RuntimeIdentityError(f"{system} critical dependency validation incomplete")

    normalized_resolutions = [
        {
            "soname": soname,
            "loader_path": resolutions[soname]["loader_path"],
            "canonical_path": resolutions[soname]["canonical_path"],
        }
        for soname in sorted(resolutions)
    ]
    loader_evidence: Dict[str, Any] = {
        "tool": str(LDD),
        "resolved_dependency_count": len(normalized_resolutions),
        "virtual_dependencies": virtual,
        "resolutions": normalized_resolutions,
        "resolution_sha256": _sha256_bytes(
            json.dumps(
                {"resolved": normalized_resolutions, "virtual": virtual},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ),
        "all_dependencies_resolved": True,
        "all_noncritical_dependencies_in_approved_roots": True,
    }
    evidence: Dict[str, Any] = {
        "schema": SCHEMA,
        "status": "PASS",
        "policy_id": policy.get("policy_id"),
        "policy_sha256": _policy_fingerprint(policy, system),
        "system": system,
        "executable": executable_identity,
        "dynamic_loader": loader_evidence,
        "critical_local_dependencies": {
            name: critical_identities[name] for name in sorted(critical_identities)
        },
    }
    if system == "S1":
        evidence["build_provenance"] = _validate_s1_provenance(
            system_policy,
            inspector,
            executable_identity,
            critical_identities,
        )
    return evidence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", choices=("U0", "S1"), required=True)
    args = parser.parse_args(argv)
    try:
        evidence = validate_runtime_identity(args.system)
    except (OSError, subprocess.SubprocessError, RuntimeIdentityError) as exc:
        print(f"runtime identity validation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
