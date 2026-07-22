#!/usr/bin/python3
"""Write or verify the CP0 build-to-source provenance marker."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKSPACE = REPO_ROOT / "build" / "cp0-ws"
CERES_LIBRARY = REPO_ROOT / "build" / "vendor" / "ceres-install" / "lib" / "libceres.so.1.14.0"
SOURCE_ROOTS = ("ov_core", "ov_eval", "ov_init", "ov_msckf")
SOURCE_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".cmake")
SOURCE_NAMES = ("CMakeLists.txt", "package.xml")
EXPECTED_RUNTIME_ARTIFACTS = {
    "ros1_serial_msckf",
    "libov_core_lib.so",
    "libov_init_lib.so",
    "libov_msckf_lib.so",
}
EXPECTED_RUNTIME_PATHS = {
    (WORKSPACE / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf").resolve(),
    (WORKSPACE / "devel" / "lib" / "libov_core_lib.so").resolve(),
    (WORKSPACE / "devel" / "lib" / "libov_init_lib.so").resolve(),
    (WORKSPACE / "devel" / "lib" / "libov_msckf_lib.so").resolve(),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", type=Path, metavar="MARKER")
    action.add_argument("--verify", type=Path, metavar="MARKER")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--ceres-library", type=Path)
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def source_paths() -> Iterable[Path]:
    for root_name in SOURCE_ROOTS:
        root = REPO_ROOT / root_name
        for path in sorted(root.rglob("*")):
            if path.is_file() and (path.name in SOURCE_NAMES or path.suffix.lower() in SOURCE_SUFFIXES):
                yield path


def source_identity() -> Tuple[str, List[Dict[str, Any]], int]:
    digest = hashlib.sha256()
    entries: List[Dict[str, Any]] = []
    newest_mtime_ns = 0
    for path in source_paths():
        relative = path.relative_to(REPO_ROOT).as_posix()
        file_hash = sha256_file(path)
        stat = path.stat()
        newest_mtime_ns = max(newest_mtime_ns, stat.st_mtime_ns)
        digest.update(relative.encode("utf-8") + b"\0" + file_hash.encode("ascii") + b"\n")
        entries.append({"path": relative, "sha256": file_hash, "size_bytes": stat.st_size})
    return digest.hexdigest(), entries, newest_mtime_ns


def git_commit() -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError("unable to read source commit: " + completed.stderr.strip())
    return completed.stdout.strip()


def file_identity(path: Path) -> Dict[str, Any]:
    path = path.resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("missing or empty build artifact: " + str(path))
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def atomic_write_json(path: Path, value: Dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, str(path))
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def write_marker(
    path: Path, executable: Path, ceres_library: Path, artifacts: List[Path]
) -> None:
    tree_hash, files, newest_source_mtime_ns = source_identity()
    executable_identity = file_identity(executable)
    if executable_identity["mtime_ns"] < newest_source_mtime_ns:
        raise ValueError("estimator executable is older than a build input; rebuild did not refresh it")
    runtime_paths = sorted(
        {executable.resolve(), *(artifact.resolve() for artifact in artifacts)}, key=str
    )
    runtime_names = {artifact.name for artifact in runtime_paths}
    if runtime_names != EXPECTED_RUNTIME_ARTIFACTS:
        raise ValueError(
            "runtime artifact set must be exactly {}; got {}".format(
                sorted(EXPECTED_RUNTIME_ARTIFACTS), sorted(runtime_names)
            )
        )
    if set(runtime_paths) != EXPECTED_RUNTIME_PATHS:
        raise ValueError("runtime artifact paths are not the canonical CP0 workspace paths")
    if ceres_library.resolve() != CERES_LIBRARY.resolve():
        raise ValueError("Ceres library is not the canonical pinned local path")
    if not os.access(str(executable.resolve()), os.X_OK):
        raise ValueError("estimator executable lacks execute permission")
    marker = {
        "schema_version": 1,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_commit": git_commit(),
        "source_tree_sha256": tree_hash,
        "source_files": files,
        "newest_source_mtime_ns": newest_source_mtime_ns,
        "estimator_executable": executable_identity,
        "runtime_artifacts": [file_identity(artifact) for artifact in runtime_paths],
        "ceres_library": file_identity(ceres_library),
        "python_executable": sys.executable,
    }
    atomic_write_json(path, marker)
    print("wrote CP0 build provenance: " + str(path.resolve()))


def verify_marker(path: Path) -> None:
    with path.resolve().open("r", encoding="utf-8") as stream:
        marker = json.load(stream)
    if marker.get("schema_version") != 1:
        raise ValueError("unsupported build provenance schema")
    if marker.get("source_commit") != git_commit():
        raise ValueError("build provenance source commit differs from current project HEAD")
    tree_hash, files, _newest_source_mtime_ns = source_identity()
    failures: List[str] = []
    if marker.get("source_tree_sha256") != tree_hash:
        failures.append("build source tree hash changed")
    if marker.get("source_files") != files:
        failures.append("build source file identities changed")
    for field in ("estimator_executable", "ceres_library"):
        recorded = marker.get(field, {})
        current = file_identity(Path(recorded.get("path", "")))
        # Paths and mtimes document the build host but are deliberately not
        # identity fields: content-identical clean copies remain verifiable.
        for key in ("sha256", "size_bytes"):
            if recorded.get(key) != current.get(key):
                failures.append("{} {} changed".format(field, key))
    runtime_artifacts = marker.get("runtime_artifacts", [])
    recorded_runtime_paths = {Path(recorded.get("path", "")).resolve() for recorded in runtime_artifacts}
    if recorded_runtime_paths != EXPECTED_RUNTIME_PATHS:
        failures.append("runtime artifacts do not point to canonical CP0 workspace paths")
    if Path(marker.get("ceres_library", {}).get("path", "")).resolve() != CERES_LIBRARY.resolve():
        failures.append("Ceres marker path is not canonical")
    if Path(marker.get("estimator_executable", {}).get("path", "")).resolve() != (
        WORKSPACE / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
    ).resolve():
        failures.append("estimator marker path is not canonical")
    elif not os.access(marker["estimator_executable"]["path"], os.X_OK):
        failures.append("estimator executable lacks execute permission")
    runtime_names = {Path(recorded.get("path", "")).name for recorded in runtime_artifacts}
    if runtime_names != EXPECTED_RUNTIME_ARTIFACTS or len(runtime_artifacts) != len(
        EXPECTED_RUNTIME_ARTIFACTS
    ):
        failures.append(
            "runtime artifact set differs: expected {} got {}".format(
                sorted(EXPECTED_RUNTIME_ARTIFACTS), sorted(runtime_names)
            )
        )
    for index, recorded in enumerate(runtime_artifacts):
        current = file_identity(Path(recorded.get("path", "")))
        for key in ("sha256", "size_bytes"):
            if recorded.get(key) != current.get(key):
                failures.append("runtime_artifacts[{}] {} changed".format(index, key))
    if failures:
        raise ValueError("; ".join(failures))
    print(
        "CP0 build provenance verified: source_tree={} executable={}".format(
            tree_hash, marker["estimator_executable"]["sha256"]
        )
    )


def main() -> int:
    args = parse_args()
    try:
        if args.write:
            if args.executable is None or args.ceres_library is None:
                raise ValueError("--write requires --executable and --ceres-library")
            write_marker(args.write, args.executable, args.ceres_library, args.artifact)
        else:
            verify_marker(args.verify)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("CP0 build provenance error: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
