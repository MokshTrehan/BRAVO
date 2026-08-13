#!/usr/bin/env python3
"""Bind TurnSafe configure provenance to built binaries and cache bytes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


CONFIGURE_SCHEMA = "turnsafe.configure_provenance.v1"
BUILD_SCHEMA = "turnsafe.build_manifest.v1"


class ManifestError(RuntimeError):
    """A fail-closed build-manifest error."""


FORBIDDEN_COMPONENTS = ("private", "holdout")


def _reject_forbidden_path(path: Path, label: str) -> None:
    def reject_components(candidate: Path) -> None:
        lowered = [component.lower() for component in candidate.parts]
        if any(
            any(word in component for word in FORBIDDEN_COMPONENTS)
            for component in lowered
        ):
            raise ManifestError("{} contains a forbidden path component".format(label))
        for index in range(len(lowered) - 1):
            if lowered[index : index + 2] == ["scripts", "cp2"]:
                raise ManifestError("{} enters protected scripts/cp2".format(label))

    reject_components(path)
    reject_components(path.resolve(strict=False))


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path, label: str = "artifact") -> Dict[str, Any]:
    _reject_forbidden_path(path, label)
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ManifestError("artifact is not a regular file: {}".format(path))
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": _sha256_file(path),
        "executable": bool(stat.st_mode & 0o111),
    }


def _validate_configure(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != CONFIGURE_SCHEMA:
        raise ManifestError("invalid configure provenance schema")
    descriptor = value.get("descriptor")
    if not isinstance(descriptor, dict):
        raise ManifestError("configure descriptor is missing")
    actual_id = hashlib.sha256(_canonical_bytes(descriptor)).hexdigest()
    if value.get("build_provenance_id") != actual_id:
        raise ManifestError("configure build-provenance ID mismatch")
    return value


def _parse_cmake_cache(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="strict").splitlines():
        if not raw or raw.startswith("#") or raw.startswith("//") or "=" not in raw:
            continue
        typed_key, value = raw.split("=", 1)
        key = typed_key.split(":", 1)[0]
        if key in values:
            raise ManifestError("duplicate CMake cache key: {}".format(key))
        values[key] = value
    return values


def _normal_bool(value: str) -> str:
    upper = value.upper()
    if upper in ("1", "ON", "TRUE", "YES", "Y"):
        return "ON"
    if upper in ("0", "OFF", "FALSE", "NO", "N", ""):
        return "OFF"
    raise ManifestError("invalid CMake Boolean value: {}".format(value))


def _same_path(left: str, right: str) -> bool:
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _validate_target_compile_flags(
    binary_directory: Path, configuration: Mapping[str, Any]
) -> Path:
    if not isinstance(configuration.get("cmake_cxx_flags_cache"), str) or not isinstance(
        configuration.get("cmake_cxx_flags_effective"), str
    ):
        raise ManifestError("configure descriptor has incomplete C++ flags")
    flags_path = binary_directory / "CMakeFiles/ov_msckf_lib.dir/flags.make"
    _reject_forbidden_path(flags_path, "target compile flags")
    lines = [
        line.split("=", 1)[1].strip()
        for line in flags_path.read_text(encoding="utf-8", errors="strict").splitlines()
        if line.startswith("CXX_FLAGS =")
    ]
    if len(lines) != 1:
        raise ManifestError("target compile flags are missing or ambiguous")
    try:
        expected = shlex.split(str(configuration["cmake_cxx_flags_effective"]))
        actual = shlex.split(lines[0])
    except ValueError as exc:
        raise ManifestError("invalid target compile flags: {}".format(exc))
    if actual[: len(expected)] != expected:
        raise ManifestError("target compile flags differ from configure descriptor")
    return flags_path.resolve(strict=True)


def _validate_cache_binding(
    cache_path: Path, configure_path: Path, configure: Mapping[str, Any]
) -> Tuple[Path, Path, Path]:
    descriptor = configure["descriptor"]
    configuration = descriptor.get("configuration")
    if not isinstance(configuration, dict):
        raise ManifestError("configure descriptor has no configuration")
    binary_directory = Path(
        str(configuration.get("cmake_binary_directory", ""))
    ).resolve(strict=True)
    source_directory = Path(
        str(configuration.get("cmake_source_directory", ""))
    ).resolve(strict=True)
    cache_path = cache_path.resolve(strict=True)
    if cache_path != binary_directory / "CMakeCache.txt":
        raise ManifestError("CMake cache is outside the configured binary directory")
    expected_configure = (
        binary_directory / "turnsafe-generated" / "configure_provenance.json"
    )
    if configure_path != expected_configure:
        raise ManifestError("configure manifest is outside its configured build")
    cache = _parse_cmake_cache(cache_path)

    exact = {
        "CMAKE_BUILD_TYPE": str(configuration.get("build_type", "")),
        "CMAKE_CXX_FLAGS": str(configuration.get("cmake_cxx_flags_cache", "")),
        "CMAKE_GENERATOR": str(configuration.get("cmake_generator", "")),
    }
    for key, expected in exact.items():
        if cache.get(key) != expected:
            raise ManifestError("CMake cache {} differs from configure descriptor".format(key))
    path_values = {
        "CMAKE_CXX_COMPILER": str(configuration.get("cxx_compiler", "")),
        "Ceres_DIR": str(configuration.get("ceres_dir", "")),
        "CMAKE_HOME_DIRECTORY": str(source_directory),
        "ov_msckf_BINARY_DIR": str(binary_directory),
    }
    for key, expected in path_values.items():
        actual = cache.get(key)
        if actual is None or not _same_path(actual, expected):
            raise ManifestError("CMake cache {} differs from configure descriptor".format(key))
    boolean_values = {
        "ENABLE_ROS": str(configuration.get("enable_ros", "")),
        "CATKIN_ENABLE_TESTING": str(
            configuration.get("catkin_enable_testing", "")
        ),
    }
    for key, expected in boolean_values.items():
        actual = cache.get(key)
        if actual is None or _normal_bool(actual) != _normal_bool(expected):
            raise ManifestError("CMake cache {} differs from configure descriptor".format(key))
    flags_path = _validate_target_compile_flags(binary_directory, configuration)
    return binary_directory, binary_directory.parent.parent, flags_path


def _artifact_argument(value: str) -> Tuple[str, Path]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("artifact must be NAME=PATH")
    name, raw_path = value.split("=", 1)
    if not name or not raw_path or not name.replace("_", "").isalnum():
        raise argparse.ArgumentTypeError("invalid artifact NAME=PATH")
    return name, Path(raw_path)


def finalize(args: argparse.Namespace) -> Dict[str, Any]:
    configure_path = Path(args.configure_manifest)
    _reject_forbidden_path(configure_path, "configure manifest")
    configure_path = configure_path.resolve(strict=True)
    configure = _validate_configure(
        json.loads(configure_path.read_text(encoding="ascii"))
    )
    cache_path = Path(args.cmake_cache)
    _reject_forbidden_path(cache_path, "CMake cache")
    binary_directory, workspace_root, flags_path = _validate_cache_binding(
        cache_path, configure_path, configure
    )
    schema_path = Path(args.schema)
    _reject_forbidden_path(schema_path, "diagnostic schema")
    schema_path = schema_path.resolve(strict=True)
    expected_schema = configure["descriptor"].get(
        "diagnostic_schema_sha256"
    )
    if _sha256_file(schema_path) != expected_schema:
        raise ManifestError("diagnostic schema differs from configured schema")
    artifacts: Dict[str, Any] = {}
    for name, path in args.artifact:
        if name in artifacts:
            raise ManifestError("duplicate artifact name: {}".format(name))
        artifacts[name] = _identity(path, "build artifact {}".format(name))
    required_artifacts = {
        "estimator_binary",
        "ov_msckf_library",
        "ov_core_library",
    }
    if not required_artifacts.issubset(artifacts):
        raise ManifestError(
            "estimator_binary, ov_msckf_library, and ov_core_library "
            "artifacts are required"
        )
    expected_artifacts = {
        "estimator_binary": workspace_root
        / "devel/lib/ov_msckf/ros1_serial_msckf",
        "ov_msckf_library": workspace_root / "devel/lib/libov_msckf_lib.so",
        "ov_core_library": workspace_root / "devel/lib/libov_core_lib.so",
    }
    for name, expected in expected_artifacts.items():
        if Path(str(artifacts[name]["path"])).resolve(strict=True) != expected:
            raise ManifestError(
                "build artifact {} is outside the configured workspace".format(name)
            )
    body: Dict[str, Any] = {
        "schema_version": BUILD_SCHEMA,
        "build_provenance_id": configure["build_provenance_id"],
        "configure_manifest": _identity(configure_path, "configure manifest"),
        "cmake_cache": _identity(cache_path, "CMake cache"),
        "target_compile_flags": _identity(flags_path, "target compile flags"),
        "diagnostic_schema": _identity(schema_path, "diagnostic schema"),
        "descriptor": configure["descriptor"],
        "source_snapshot": configure["source_snapshot"],
        "artifacts": dict(sorted(artifacts.items())),
    }
    body["build_manifest_payload_sha256"] = hashlib.sha256(
        _canonical_bytes(body)
    ).hexdigest()
    output = Path(args.output).resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(body, sort_keys=True, indent=2) + "\n"
    temporary = output.with_name(output.name + ".tmp.{}".format(os.getpid()))
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(output))
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return body


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure-manifest", required=True)
    parser.add_argument("--cmake-cache", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--artifact", action="append", type=_artifact_argument, default=[]
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        finalize(_parser().parse_args(argv))
    except (OSError, ValueError, KeyError, ManifestError) as exc:
        print("BUILD_MANIFEST_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
