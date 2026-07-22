#!/usr/bin/python3
"""Run, evaluate, validate, and atomically seal the CP0 MH_01 baseline."""

from __future__ import annotations

import argparse
import ast
import ctypes
import datetime as dt
import errno
import hashlib
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
SPEC_PATH = REPO_ROOT / "project" / "cp0_baseline.json"
WORKSPACE = REPO_ROOT / "build" / "cp0-ws"
CERES_PREFIX = REPO_ROOT / "build" / "vendor" / "ceres-install"
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ACTIVE_FAILURE_CONTEXT: Dict[str, Any] = {}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the pinned MH_01 CP0 baseline and seal only passing evidence."
    )
    parser.add_argument(
        "--bag",
        type=Path,
        help="absolute or relative path to MH_01_easy.bag (or set CP0_MH01_BAG)",
    )
    parser.add_argument(
        "--run-id",
        help="artifact directory name; defaults to a UTC timestamp plus source commit",
    )
    parser.add_argument(
        "--affinity",
        help="optional fixed Linux CPU list, for example 0-3 or 2,4-6",
    )
    parser.add_argument(
        "--ros-port",
        type=int,
        help="isolated ROS master port (default comes from project/cp0_baseline.json)",
    )
    parser.add_argument(
        "--no-seal",
        action="store_true",
        help="validate a passing run but leave it under results/staging",
    )
    return parser.parse_args()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[str] = None
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
            stream.write(text)
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


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = stream.name
            stream.write(value)
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


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_capture(argv: Sequence[str], env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    try:
        completed = subprocess.run(
            list(argv),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=30,
        )
        return {"argv": list(argv), "exit_code": completed.returncode, "output": completed.stdout}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": list(argv), "exit_code": None, "output": "", "error": str(exc)}


def run_streamed(
    argv: Sequence[str],
    log_path: Path,
    env: Dict[str, str],
    echo: bool = True,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    started_utc = utc_now()
    started = time.monotonic()
    interrupted = False
    return_code = 125
    error: Optional[str] = None
    timed_out = threading.Event()
    finished = threading.Event()
    with log_path.open("w", encoding="utf-8", errors="replace") as log_stream:
        try:
            process = subprocess.Popen(
                list(argv),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            assert process.stdout is not None

            def watchdog() -> None:
                if timeout_seconds is None or finished.wait(timeout_seconds):
                    return
                timed_out.set()
                for stop_signal, grace_seconds in (
                    (signal.SIGINT, 10.0),
                    (signal.SIGTERM, 5.0),
                    (signal.SIGKILL, 1.0),
                ):
                    try:
                        os.killpg(process.pid, stop_signal)
                    except ProcessLookupError:
                        return
                    if finished.wait(grace_seconds):
                        return

            watchdog_thread = threading.Thread(target=watchdog, daemon=True)
            watchdog_thread.start()
            try:
                for line in process.stdout:
                    log_stream.write(line)
                    log_stream.flush()
                    if echo:
                        sys.stdout.write(line)
                        sys.stdout.flush()
                return_code = process.wait()
            except KeyboardInterrupt:
                interrupted = True
                os.killpg(process.pid, signal.SIGINT)
                try:
                    return_code = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        return_code = process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        return_code = process.wait()
            finally:
                finished.set()
                watchdog_thread.join(timeout=0.1)
        except OSError as exc:
            error = str(exc)
            log_stream.write("failed to execute command: " + error + "\n")
        log_stream.flush()
        os.fsync(log_stream.fileno())
    return {
        "argv": list(argv),
        "shell": shlex.join(list(argv)),
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "exit_code": return_code,
        "interrupted": interrupted,
        "timed_out": timed_out.is_set(),
        "timeout_seconds": timeout_seconds,
        "error": error,
        "log": log_path.name,
    }


def parse_cpu_list(value: str) -> Set[int]:
    cpus: Set[int] = set()
    if not value or any(character.isspace() for character in value):
        raise ValueError("CPU list must be nonempty and contain no whitespace")
    for component in value.split(","):
        if not component:
            raise ValueError("CPU list contains an empty component")
        if "-" in component:
            parts = component.split("-")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValueError("invalid CPU range: " + component)
            first, last = (int(part) for part in parts)
            if first > last:
                raise ValueError("descending CPU range: " + component)
            cpus.update(range(first, last + 1))
        elif component.isdigit():
            cpus.add(int(component))
        else:
            raise ValueError("invalid CPU identifier: " + component)
    return cpus


def read_os_release() -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        with Path("/etc/os-release").open("r", encoding="utf-8") as stream:
            for line in stream:
                if "=" not in line:
                    continue
                key, value = line.rstrip("\n").split("=", 1)
                result[key] = value.strip().strip('"')
    except OSError:
        pass
    return result


def read_governors(cpus: Iterable[int]) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}
    for cpu in sorted(cpus):
        path = Path("/sys/devices/system/cpu") / ("cpu" + str(cpu)) / "cpufreq" / "scaling_governor"
        try:
            result[str(cpu)] = path.read_text(encoding="utf-8").strip()
        except OSError:
            result[str(cpu)] = None
    return result


def minimal_runtime_environment() -> Dict[str, str]:
    # roslaunch records its complete environment in its internal log. Copy
    # only toolchain/runtime fields so credentials and desktop/session state
    # cannot leak into immutable evidence.
    allowed = {
        "CMAKE_PREFIX_PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LD_LIBRARY_PATH",
        "LOGNAME",
        "PATH",
        "PKG_CONFIG_PATH",
        "PYTHONPATH",
        "ROS_DISTRO",
        "ROS_ETC_DIR",
        "ROS_PACKAGE_PATH",
        "ROS_PYTHON_VERSION",
        "ROS_ROOT",
        "ROS_VERSION",
        "SHELL",
        "TMPDIR",
        "USER",
    }
    return {key: os.environ[key] for key in sorted(allowed) if key in os.environ}


def load_spec() -> Dict[str, Any]:
    with SPEC_PATH.open("r", encoding="utf-8") as stream:
        spec = json.load(stream)
    if spec.get("checkpoint") != "CP0" or spec.get("schema_version") != 1:
        raise ValueError("unsupported CP0 baseline specification")
    return spec


def pinned_commit() -> str:
    text = (REPO_ROOT / "UPSTREAM_REVISION").read_text(encoding="utf-8")
    match = re.search(r"^Pinned commit:\s*([0-9a-f]{40})\s*$", text, re.MULTILINE)
    if match is None:
        raise ValueError("UPSTREAM_REVISION has no valid pinned commit")
    return match.group(1)


def git_snapshot() -> Tuple[Dict[str, Any], bytes, bytes]:
    upstream_base = pinned_commit()
    commit_result = run_capture(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    status_result = run_capture(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1", "--untracked-files=normal"]
    )
    diff_result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "diff", "--binary", upstream_base, "--"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    commit = commit_result.get("output", "").strip()
    status = status_result.get("output", "").splitlines()
    changed_result = run_capture(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "diff",
            "--name-only",
            upstream_base,
            "--",
            "ov_core",
            "ov_eval",
            "ov_init",
            "ov_msckf",
        ]
    )
    build_source_changes = sorted(
        line for line in changed_result.get("output", "").splitlines() if line
    )
    serial_patch_result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "diff",
            "--binary",
            upstream_base,
            "--",
            "ov_msckf/src/ros1_serial_msckf.cpp",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    ancestor_result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", upstream_base, commit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    metadata = {
        "commit": commit,
        "pinned_upstream_base": upstream_base,
        "head_equals_pinned_base": commit == upstream_base,
        "pinned_base_is_ancestor": ancestor_result.returncode == 0,
        "dirty": bool(status),
        "status_porcelain_v1": status,
        "tracked_delta_from_pinned_base_sha256": sha256_bytes(diff_result.stdout),
        "tracked_delta_from_pinned_base_artifact": "source_tracked_binary.diff",
        "tracked_delta_from_pinned_base_exit_code": diff_result.returncode,
        "build_source_changes_from_pinned_base": build_source_changes,
        "allowed_build_source_changes": ["ov_msckf/src/ros1_serial_msckf.cpp"],
        "serial_runner_patch_sha256": sha256_bytes(serial_patch_result.stdout),
        "serial_runner_patch_artifact": "serial_runner_patch.diff",
    }
    return metadata, diff_result.stdout, serial_patch_result.stdout


def file_identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "sha256": sha256_file(path),
    }


def reproducibility_input_identities() -> Dict[str, Dict[str, Any]]:
    paths = [
        REPO_ROOT / "UPSTREAM_REVISION",
        SPEC_PATH,
        REPO_ROOT / "project" / "cp0_serial.launch",
        REPO_ROOT / "config" / "euroc_mav" / "estimator_config.yaml",
        REPO_ROOT / "config" / "euroc_mav" / "kalibr_imu_chain.yaml",
        REPO_ROOT / "config" / "euroc_mav" / "kalibr_imucam_chain.yaml",
        REPO_ROOT / "ov_data" / "euroc_mav" / "MH_01_easy.txt",
    ]
    paths.extend(
        path
        for path in (REPO_ROOT / "scripts" / "cp0").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    )
    result: Dict[str, Dict[str, Any]] = {}
    for path in sorted(set(paths)):
        stat = path.stat()
        result[path.relative_to(REPO_ROOT).as_posix()] = {
            "sha256": sha256_file(path),
            "size_bytes": stat.st_size,
        }
    return result


def snapshot_reproducibility_inputs(
    run_dir: Path, identities: Dict[str, Dict[str, Any]]
) -> Dict[str, str]:
    snapshot: Dict[str, str] = {}
    for relative, expected in sorted(identities.items()):
        source = REPO_ROOT / relative
        data = source.read_bytes()
        if sha256_bytes(data) != expected["sha256"] or len(data) != expected["size_bytes"]:
            raise ValueError("reproducibility input changed during preflight: " + relative)
        destination = run_dir / "reproducibility" / relative
        atomic_write_bytes(destination, data)
        snapshot[relative] = destination.relative_to(run_dir).as_posix()
    return snapshot


def parse_rosbag_info(text: str) -> Dict[str, Any]:
    values: Dict[str, Any] = {}
    patterns = {
        "version": r"^version:\s*(\S+)",
        "duration_seconds": r"^duration:\s*([0-9.eE+-]+)",
        "start_seconds": r"^start:\s*([0-9.eE+-]+)",
        "end_seconds": r"^end:\s*([0-9.eE+-]+)",
        "size_bytes": r"^size:\s*([0-9]+)",
        "messages": r"^messages:\s*([0-9]+)",
        "indexed": r"^indexed:\s*(\S+)",
        "compression": r"^compression:\s*(\S+)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.MULTILINE)
        if match is None:
            continue
        raw = match.group(1)
        if key in ("duration_seconds", "start_seconds", "end_seconds"):
            values[key] = float(raw)
        elif key in ("size_bytes", "messages"):
            values[key] = int(raw)
        elif key == "indexed":
            values[key] = raw.lower() == "true"
        else:
            values[key] = raw
    return values


def data_rows(path: Path, minimum_columns: int) -> Tuple[int, float, float]:
    count = 0
    first_timestamp = math.nan
    previous_timestamp = -math.inf
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split()
            if len(fields) < minimum_columns:
                raise ValueError(
                    "{}:{}: expected at least {} columns".format(path, line_number, minimum_columns)
                )
            try:
                values = [float(field) for field in fields]
            except ValueError as exc:
                raise ValueError("{}:{}: non-numeric value".format(path, line_number)) from exc
            if not all(math.isfinite(value) for value in values):
                raise ValueError("{}:{}: non-finite value".format(path, line_number))
            timestamp = values[0]
            if timestamp <= previous_timestamp:
                raise ValueError("{}:{}: timestamps are not strictly increasing".format(path, line_number))
            if count == 0:
                first_timestamp = timestamp
            previous_timestamp = timestamp
            count += 1
    return count, first_timestamp, previous_timestamp


def timing_rows(path: Path) -> Tuple[int, float, float]:
    expected_header = (
        "# timestamp (sec),tracking,propagation,msckf update,slam update,"
        "slam delayed,re-tri & marg,total"
    )
    count = 0
    first_timestamp = math.nan
    previous_timestamp = -math.inf
    saw_header = False
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line == expected_header:
                    saw_header = True
                continue
            fields = line.split(",")
            if len(fields) != 8:
                raise ValueError("{}:{}: expected exactly 8 comma-separated columns".format(path, line_number))
            try:
                values = [float(field) for field in fields]
            except ValueError as exc:
                raise ValueError("{}:{}: non-numeric timing value".format(path, line_number)) from exc
            if not all(math.isfinite(value) for value in values):
                raise ValueError("{}:{}: non-finite timing value".format(path, line_number))
            timestamp = values[0]
            if timestamp <= previous_timestamp:
                raise ValueError("{}:{}: timestamps are not strictly increasing".format(path, line_number))
            if count == 0:
                first_timestamp = timestamp
            previous_timestamp = timestamp
            count += 1
    if not saw_header:
        raise ValueError("timing log is missing the exact OpenVINS eight-column header")
    return count, first_timestamp, previous_timestamp


def total_state_rows(path: Path, minimum_columns: int) -> Tuple[int, float, float, int]:
    count = 0
    first_timestamp = math.nan
    previous_timestamp = -math.inf
    column_count: Optional[int] = None
    saw_header = False
    with path.open("r", encoding="utf-8") as stream:
        for line_number, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                if line.startswith("# timestamp(s) q p v bg ba cam_imu_dt num_cam"):
                    saw_header = True
                continue
            fields = line.split()
            if len(fields) < minimum_columns:
                raise ValueError(
                    "{}:{}: expected at least {} total-state columns".format(
                        path, line_number, minimum_columns
                    )
                )
            if column_count is None:
                column_count = len(fields)
            elif len(fields) != column_count:
                raise ValueError("{}:{}: inconsistent total-state column count".format(path, line_number))
            try:
                values = [float(field) for field in fields]
            except ValueError as exc:
                raise ValueError("{}:{}: non-numeric total-state value".format(path, line_number)) from exc
            if not all(math.isfinite(value) for value in values):
                raise ValueError("{}:{}: non-finite total-state value".format(path, line_number))
            timestamp = values[0]
            if timestamp <= previous_timestamp:
                raise ValueError("{}:{}: timestamps are not strictly increasing".format(path, line_number))
            if count == 0:
                first_timestamp = timestamp
            previous_timestamp = timestamp
            count += 1
    if not saw_header:
        raise ValueError("total-state log lacks the exact OpenVINS header family")
    return count, first_timestamp, previous_timestamp, column_count or 0


def timestamps_match(first: Path, second: Path, tolerance: float = 1e-4) -> bool:
    def timestamps(path: Path) -> Iterable[float]:
        with path.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                line = raw_line.strip()
                if line and not line.startswith("#"):
                    yield float(line.split(",", 1)[0].split()[0])

    first_iterator = iter(timestamps(first))
    second_iterator = iter(timestamps(second))
    while True:
        first_value = next(first_iterator, None)
        second_value = next(second_iterator, None)
        if first_value is None or second_value is None:
            return first_value is None and second_value is None
        if abs(first_value - second_value) > tolerance:
            return False


def projected_rows_match(source: Path, projected: Path) -> bool:
    def rows(path: Path) -> Iterable[List[str]]:
        with path.open("r", encoding="utf-8") as stream:
            for raw_line in stream:
                line = raw_line.strip()
                if line and not line.startswith("#"):
                    yield line.split()

    source_iterator = iter(rows(source))
    projected_iterator = iter(rows(projected))
    while True:
        source_row = next(source_iterator, None)
        projected_row = next(projected_iterator, None)
        if source_row is None or projected_row is None:
            return source_row is None and projected_row is None
        expected = [
            source_row[0],
            source_row[5],
            source_row[6],
            source_row[7],
            source_row[1],
            source_row[2],
            source_row[3],
            source_row[4],
        ]
        if expected != projected_row or len(projected_row) != 8:
            return False


def npy_element_count(data: bytes) -> int:
    stream = io.BytesIO(data)
    if stream.read(6) != b"\x93NUMPY":
        raise ValueError("invalid NumPy magic")
    major, minor = struct.unpack("BB", stream.read(2))
    if (major, minor) == (1, 0):
        header_length = struct.unpack("<H", stream.read(2))[0]
    elif major in (2, 3):
        header_length = struct.unpack("<I", stream.read(4))[0]
    else:
        raise ValueError("unsupported NumPy file version {}.{}".format(major, minor))
    header = ast.literal_eval(stream.read(header_length).decode("latin1").strip())
    shape = header.get("shape")
    if not isinstance(shape, tuple):
        raise ValueError("NumPy header has no shape tuple")
    count = 1
    for dimension in shape:
        count *= int(dimension)
    return count


def metric_stats(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(str(path), "r") as archive:
        stats = json.loads(archive.read("stats.json").decode("utf-8"))
        info = json.loads(archive.read("info.json").decode("utf-8"))
        samples = npy_element_count(archive.read("error_array.npy"))
    for key, value in stats.items():
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise ValueError("metric contains non-finite statistic: " + key)
    return {"stats": stats, "samples": samples, "title": info.get("title"), "label": info.get("label")}


def add_check(checks: List[Dict[str, Any]], name: str, passed: bool, details: Any) -> None:
    checks.append({"name": name, "passed": bool(passed), "details": details})


def validate_run(
    run_dir: Path,
    spec: Dict[str, Any],
    command_records: Dict[str, Any],
    dataset_before: Dict[str, Any],
    dataset_after: Dict[str, Any],
    source: Dict[str, Any],
) -> Dict[str, Any]:
    checks: List[Dict[str, Any]] = []
    errors: List[str] = []

    def checked(name: str, operation: Any) -> Any:
        try:
            value = operation()
            add_check(checks, name, True, value)
            return value
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            message = str(exc)
            add_check(checks, name, False, message)
            errors.append(name + ": " + message)
            return None

    governance_passed = (
        source.get("build_source_changes_from_pinned_base")
        == source.get("allowed_build_source_changes")
        and source.get("serial_runner_patch_sha256")
        == spec["frozen_source"]["serial_runner_patch_sha256"]
        and sha256_file(run_dir / source["serial_runner_patch_artifact"])
        == spec["frozen_source"]["serial_runner_patch_sha256"]
        and sha256_file(run_dir / "source_tracked_binary.diff")
        == source.get("tracked_delta_from_pinned_base_sha256")
    )
    add_check(
        checks,
        "declared_serial_runner_source_delta_only",
        governance_passed,
        {
            "changes": source.get("build_source_changes_from_pinned_base"),
            "allowed": source.get("allowed_build_source_changes"),
            "serial_runner_patch_sha256": source.get("serial_runner_patch_sha256"),
            "expected_serial_runner_patch_sha256": spec["frozen_source"]["serial_runner_patch_sha256"],
            "persisted_serial_runner_patch_sha256": sha256_file(
                run_dir / source["serial_runner_patch_artifact"]
            ),
            "persisted_diff_sha256": sha256_file(run_dir / "source_tracked_binary.diff"),
        },
    )
    if not governance_passed:
        errors.append("declared serial-runner-only source delta check failed")

    launch_exit = command_records["roslaunch"]["exit_code"]
    launch_timed_out = bool(command_records["roslaunch"].get("timed_out"))
    add_check(
        checks,
        "roslaunch_exit_zero_without_timeout",
        launch_exit == 0 and not launch_timed_out,
        {"exit_code": launch_exit, "timed_out": launch_timed_out},
    )

    console_path = run_dir / "console.log"

    def validate_console() -> Dict[str, Any]:
        text = console_path.read_text(encoding="utf-8", errors="replace")
        plain = ANSI_ESCAPE.sub("", text)
        clean_blocks = re.findall(
            r"REQUIRED process \[(cp0_vio-[0-9]+)\] has died!\s*\n"
            r"process has finished cleanly",
            plain,
        )
        starts = re.findall(r"process\[cp0_vio-[0-9]+\]: started with pid", plain)
        if len(clean_blocks) != 1:
            raise ValueError(
                "expected one clean required-node shutdown block, found {}".format(len(clean_blocks))
            )
        if len(starts) != 1:
            raise ValueError("expected exactly one cp0_vio start, found {}".format(len(starts)))
        fatal_patterns = {
            "nonzero process exit": r"process \[[^\]]+\] has died[^\n]*exit code [1-9]",
            "segmentation fault": r"(?i)segmentation fault",
            "uncaught termination": r"(?i)terminate called|uncaught exception",
            "fatal log": r"(?i)(?:\[FATAL\]|\bFATAL:) ",
            "non-finite token": r"(?i)(?<![A-Za-z])(?:nan|[-+]?inf(?:inity)?)(?![A-Za-z])",
            "covariance failure": r"(?i)covariance[^\n]*(?:invalid|failed|failure|not positive)",
        }
        hits = [name for name, pattern in fatal_patterns.items() if re.search(pattern, plain)]
        if hits:
            raise ValueError("fatal console markers: " + ", ".join(hits))
        return {
            "required_process_banner": "expected because cp0_vio is required",
            "required_process_result": "process has finished cleanly",
            "cp0_vio_start_count": len(starts),
            "state_capture": "synchronous ROS1Visualizer save_total_state",
        }

    checked("clean_console_and_single_estimator_lifetime", validate_console)

    def validate_resolved_parameters() -> Dict[str, Any]:
        text = (run_dir / "resolved_ros_parameters.yaml").read_text(encoding="utf-8")
        expected = {
            "num_opencv_threads": r"^/cp0_vio/num_opencv_threads:\s*0\s*$",
            "multi_threading_pubs": r"^/cp0_vio/multi_threading_pubs:\s*(?:false|False)\s*$",
            "multi_threading_subs": r"^/cp0_vio/multi_threading_subs:\s*(?:false|False)\s*$",
            "calib_cam_extrinsics": r"^/cp0_vio/calib_cam_extrinsics:\s*(?:false|False)\s*$",
            "calib_cam_intrinsics": r"^/cp0_vio/calib_cam_intrinsics:\s*(?:false|False)\s*$",
            "calib_cam_timeoffset": r"^/cp0_vio/calib_cam_timeoffset:\s*(?:false|False)\s*$",
            "save_total_state": r"^/cp0_vio/save_total_state:\s*(?:true|True)\s*$",
            "filepath_est": r"^/cp0_vio/filepath_est:\s*.+state_estimate\.txt\s*$",
            "filepath_std": r"^/cp0_vio/filepath_std:\s*.+state_deviation\.txt\s*$",
        }
        missing = [name for name, pattern in expected.items() if re.search(pattern, text, re.MULTILINE) is None]
        if missing:
            raise ValueError("missing deterministic ROS parameters: " + ", ".join(missing))
        if re.search(r"^/cp0_vio/path_gt:", text, re.MULTILINE):
            raise ValueError("path_gt was supplied to the estimator (ground-truth initialization risk)")
        return {
            "num_opencv_threads": 0,
            "multi_threading_pubs": False,
            "multi_threading_subs": False,
            "calib_cam_extrinsics": False,
            "calib_cam_intrinsics": False,
            "calib_cam_timeoffset": False,
            "save_total_state": True,
            "capture_transport": "in-process synchronous file write",
            "ground_truth_parameter_present": False,
        }

    checked("deterministic_parameters_and_no_ground_truth_initialization", validate_resolved_parameters)

    resolved_exit = command_records["resolve_parameters"].get("exit_code")
    add_check(
        checks,
        "roslaunch_parameter_resolution_exit_zero",
        resolved_exit == 0,
        {"exit_code": resolved_exit},
    )
    if resolved_exit != 0:
        errors.append("roslaunch parameter resolution exited " + str(resolved_exit))

    post_build_exit = command_records["verify_build_after_run"].get("exit_code")
    add_check(
        checks,
        "build_provenance_unchanged_after_run",
        post_build_exit == 0,
        {
            "exit_code": post_build_exit,
            "output": command_records["verify_build_after_run"].get("output", "").strip(),
        },
    )
    if post_build_exit != 0:
        errors.append("build provenance changed during execution")

    required_files = {
        "console.log": 1,
        "state_estimate.txt": 1,
        "state_deviation.txt": 1,
        "trajectory_tum.txt": 1,
        "timing_openvins.csv": 1,
        "evo_ape_translation.txt": 1,
        "evo_ape_translation.zip": 1,
        "evo_rpe_translation_1m.txt": 1,
        "evo_rpe_translation_1m.zip": 1,
        "evo_rpe_rotation_1m_deg.txt": 1,
        "evo_rpe_rotation_1m_deg.zip": 1,
    }
    missing_files = []
    sizes: Dict[str, int] = {}
    for relative, minimum_size in required_files.items():
        path = run_dir / relative
        try:
            size = path.stat().st_size
            sizes[relative] = size
            if not path.is_file() or size < minimum_size:
                missing_files.append(relative)
        except OSError:
            missing_files.append(relative)
    add_check(checks, "required_nonempty_artifacts", not missing_files, {"sizes": sizes, "missing": missing_files})
    if missing_files:
        errors.append("required_nonempty_artifacts: " + ", ".join(missing_files))

    minimum_poses = int(spec["evaluation"]["minimum_trajectory_poses"])
    minimum_timing = int(spec["evaluation"]["minimum_timing_rows"])
    state_estimate = checked(
        "full_state_estimate_is_finite_monotonic_and_complete",
        lambda: total_state_rows(run_dir / "state_estimate.txt", 19),
    )
    state_deviation = checked(
        "full_state_deviation_is_finite_monotonic_and_complete",
        lambda: total_state_rows(run_dir / "state_deviation.txt", 18),
    )
    trajectory = checked(
        "trajectory_is_finite_monotonic_and_long_enough",
        lambda: data_rows(run_dir / "trajectory_tum.txt", 8),
    )
    if trajectory is not None:
        count, first, last = trajectory
        passed = count >= minimum_poses
        add_check(
            checks,
            "minimum_trajectory_length",
            passed,
            {"rows": count, "minimum": minimum_poses, "first_timestamp": first, "last_timestamp": last},
        )
        if not passed:
            errors.append("minimum_trajectory_length: {} < {}".format(count, minimum_poses))

    timing = checked(
        "timing_is_finite_monotonic_and_long_enough",
        lambda: timing_rows(run_dir / "timing_openvins.csv"),
    )
    if timing is not None:
        count, first, last = timing
        passed = count >= minimum_timing
        add_check(
            checks,
            "minimum_timing_length",
            passed,
            {"rows": count, "minimum": minimum_timing, "first_timestamp": first, "last_timestamp": last},
        )
        if not passed:
            errors.append("minimum_timing_length: {} < {}".format(count, minimum_timing))

    if (
        state_estimate is not None
        and state_deviation is not None
        and trajectory is not None
        and timing is not None
    ):
        state_count, state_first, state_last, state_columns = state_estimate
        std_count, std_first, std_last, std_columns = state_deviation
        trajectory_count, trajectory_first, trajectory_last = trajectory
        timing_count, timing_first, timing_last = timing
        synchronous_capture_complete = (
            state_count == std_count == trajectory_count == timing_count
            and timestamps_match(
                run_dir / "state_estimate.txt", run_dir / "state_deviation.txt"
            )
            and timestamps_match(
                run_dir / "state_estimate.txt", run_dir / "timing_openvins.csv"
            )
            and timestamps_match(
                run_dir / "state_estimate.txt", run_dir / "trajectory_tum.txt"
            )
        )
        add_check(
            checks,
            "synchronous_state_and_std_cover_every_timed_update",
            synchronous_capture_complete,
            {
                "state_rows": state_count,
                "state_columns": state_columns,
                "std_rows": std_count,
                "std_columns": std_columns,
                "trajectory_rows": trajectory_count,
                "timing_rows": timing_count,
                "state_first_timestamp_seconds": state_first,
                "state_last_timestamp_seconds": state_last,
                "std_first_timestamp_seconds": std_first,
                "std_last_timestamp_seconds": std_last,
                "timing_first_timestamp_seconds": timing_first,
                "timing_last_timestamp_seconds": timing_last,
            },
        )
        if not synchronous_capture_complete:
            errors.append("synchronous state/std capture is incomplete or timestamp-misaligned")

        completion = spec["dataset"]["completion_witness"]
        completion_passed = (
            timing_count == int(completion["expected_timing_rows"])
            and abs(timing_last - float(completion["final_selected_camera_timestamp_seconds"]))
            <= float(completion["timestamp_tolerance_seconds"])
        )
        add_check(
            checks,
            "full_selected_camera_interval_processed",
            completion_passed,
            {
                "timing_rows": timing_count,
                "expected_timing_rows": completion["expected_timing_rows"],
                "last_timing_timestamp_seconds": timing_last,
                "expected_final_selected_camera_timestamp_seconds": completion[
                    "final_selected_camera_timestamp_seconds"
                ],
                "tolerance_seconds": completion["timestamp_tolerance_seconds"],
            },
        )
        if not completion_passed:
            errors.append("run did not reach the frozen final selected-camera timestamp")

    projection_matches = checked(
        "tum_trajectory_reorders_total_state_pose_columns",
        lambda: projected_rows_match(
            run_dir / "state_estimate.txt", run_dir / "trajectory_tum.txt"
        ),
    )
    if projection_matches is False:
        errors.append("tum_trajectory_reorders_total_state_pose_columns: mismatch")
        checks[-1]["passed"] = False

    for command_name in ("trajectory_conversion", "ape_translation", "rpe_translation_1m", "rpe_rotation_1m"):
        exit_code = command_records[command_name]["exit_code"]
        timed_out = bool(command_records[command_name].get("timed_out"))
        passed = exit_code == 0 and not timed_out
        add_check(
            checks,
            command_name + "_exit_zero_without_timeout",
            passed,
            {"exit_code": exit_code, "timed_out": timed_out},
        )
        if not passed:
            errors.append(command_name + " exited " + str(exit_code))

    metrics: Dict[str, Any] = {}
    metric_files = {
        "ape_translation": "evo_ape_translation.zip",
        "rpe_translation_1m": "evo_rpe_translation_1m.zip",
        "rpe_rotation_1m_deg": "evo_rpe_rotation_1m_deg.zip",
    }
    for metric_name, filename in metric_files.items():
        value = checked("valid_" + metric_name + "_archive", lambda filename=filename: metric_stats(run_dir / filename))
        if value is not None:
            metrics[metric_name] = value

    threshold = float(spec["evaluation"]["ape_translation_rmse_max_m"])
    ate = metrics.get("ape_translation", {}).get("stats", {}).get("rmse")
    ate_passed = isinstance(ate, (int, float)) and math.isfinite(float(ate)) and float(ate) <= threshold
    add_check(
        checks,
        "ape_translation_rmse_gate",
        ate_passed,
        {"rmse_m": ate, "maximum_m": threshold, "alignment": "SE(3), no scale"},
    )
    if not ate_passed:
        errors.append("ape_translation_rmse_gate failed: {} > {}".format(ate, threshold))

    unchanged_keys = ("size_bytes", "mtime_ns", "device", "inode", "sha256")
    dataset_unchanged = all(dataset_before.get(key) == dataset_after.get(key) for key in unchanged_keys)
    add_check(
        checks,
        "dataset_unchanged_during_run",
        dataset_unchanged,
        {"before": dataset_before, "after": dataset_after},
    )
    if not dataset_unchanged:
        errors.append("dataset changed during the run")

    if launch_exit != 0 or launch_timed_out:
        errors.append(
            "roslaunch exited {} (timed_out={})".format(launch_exit, launch_timed_out)
        )
    return {
        "schema_version": 1,
        "checkpoint": "CP0",
        "generated_utc": utc_now(),
        "passed": not errors and all(check["passed"] for check in checks),
        "errors": errors,
        "checks": checks,
        "metrics": metrics,
    }


def dependency_metadata() -> Dict[str, Any]:
    tools: Dict[str, Any] = {}
    for name, argv in {
        "cmake": ["cmake", "--version"],
        "c_compiler": ["gcc", "--version"],
        "cxx_compiler": ["g++", "--version"],
        "roslaunch": ["rosversion", "roslaunch"],
        "ros_distro": ["rosversion", "-d"],
        "catkin": ["catkin", "--version"],
        "evo_ape": ["evo_ape", "--help"],
    }.items():
        result = run_capture(argv)
        output = result.get("output", "").strip().splitlines()
        tools[name] = {
            "executable": shutil.which(argv[0]),
            "exit_code": result.get("exit_code"),
            "first_line": output[0] if output else None,
        }
    try:
        tools["evo"] = {"version": importlib.metadata.version("evo")}
    except importlib.metadata.PackageNotFoundError:
        tools["evo"] = {"version": None}

    ceres_header = CERES_PREFIX / "include" / "ceres" / "version.h"
    ceres_library = CERES_PREFIX / "lib" / "libceres.so.1.14.0"
    tools["ceres"] = {
        "prefix": str(CERES_PREFIX),
        "version": "1.14.0",
        "version_header_sha256": sha256_file(ceres_header),
        "library_sha256": sha256_file(ceres_library),
    }
    return tools


def generate_checksums(run_dir: Path) -> None:
    lines: List[str] = []
    for path in sorted(run_dir.rglob("*"), key=lambda item: item.relative_to(run_dir).as_posix()):
        if path.name == "SHA256SUMS":
            continue
        if path.is_symlink():
            raise ValueError("artifact tree contains a symlink: " + str(path.relative_to(run_dir)))
        if path.is_file():
            relative = path.relative_to(run_dir).as_posix()
            if "\n" in relative or "\r" in relative:
                raise ValueError("artifact filename contains a newline")
            lines.append("{}  {}".format(sha256_file(path), relative))
    atomic_write_text(run_dir / "SHA256SUMS", "\n".join(lines) + "\n")


def cleanup_ros_bookkeeping(run_dir: Path) -> Dict[str, Any]:
    removed: List[str] = []
    latest = run_dir / "ros-logs" / "latest"
    if latest.is_symlink():
        resolved = latest.resolve(strict=True)
        logs_root = (run_dir / "ros-logs").resolve()
        try:
            resolved.relative_to(logs_root)
        except ValueError as exc:
            raise ValueError("ROS latest symlink escapes the run log root: " + str(resolved)) from exc
        latest.unlink()
        removed.append("ros-logs/latest")
    elif latest.exists():
        raise ValueError("ROS latest bookkeeping path is not a symlink: " + str(latest))

    ros_home = run_dir / "ros-home"
    if ros_home.exists():
        if ros_home.is_symlink() or not ros_home.is_dir():
            raise ValueError("unexpected ROS_HOME artifact type: " + str(ros_home))
        shutil.rmtree(str(ros_home))
        removed.append("ros-home/")
    return {"removed_before_sealing": removed}


def finalize_failure_artifacts(
    run_dir: Path,
    run_id: str,
    source: Dict[str, Any],
    reason: str,
    details: Optional[str] = None,
) -> None:
    cleanup: Dict[str, Any]
    try:
        cleanup = cleanup_ros_bookkeeping(run_dir)
    except (OSError, ValueError) as exc:
        cleanup = {"error": str(exc)}
    failure_manifest = {
        "schema_version": 1,
        "checkpoint": "CP0",
        "run_id": run_id,
        "status": "failed",
        "finished_utc": utc_now(),
        "failure": {"reason": reason, "details": details},
        "source": source,
        "runtime": {"ros_bookkeeping": cleanup},
        "artifact_locations": {
            "staging": str(run_dir.relative_to(REPO_ROOT)),
            "immutable": None,
            "seal_requested": False,
        },
    }
    atomic_write_json(run_dir / "manifest.json", failure_manifest)
    generate_checksums(run_dir)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_rename_noreplace(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev != destination.parent.stat().st_dev:
        raise OSError(errno.EXDEV, "staging and immutable roots are on different filesystems")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for atomic no-overwrite sealing")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(str(source)),
        at_fdcwd,
        os.fsencode(str(destination)),
        rename_noreplace,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(destination))
    fsync_directory(destination.parent)


def command_available(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ValueError("required command is unavailable: " + name)
    return path


def assert_port_available(port: int) -> None:
    if port < 1024 or port > 65535:
        raise ValueError("ROS port must be between 1024 and 65535")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError("ROS port {} is unavailable: {}".format(port, exc)) from exc


def resolve_bag(argument: Optional[Path]) -> Path:
    value: Optional[Path] = argument
    if value is None and os.environ.get("CP0_MH01_BAG"):
        value = Path(os.environ["CP0_MH01_BAG"])
    if value is None:
        raise ValueError("provide --bag or set CP0_MH01_BAG")
    path = value.expanduser().resolve()
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("MH_01 bag is missing or empty: " + str(path))
    if path.name != "MH_01_easy.bag":
        raise ValueError("CP0 requires a file named MH_01_easy.bag (got: {})".format(path.name))
    return path


def main() -> int:
    try:
        args = parse_args()
        spec = load_spec()
        bag = resolve_bag(args.bag)
        expected_bag = spec["dataset"]["bag_identity"]
        if bag.stat().st_size != int(expected_bag["size_bytes"]):
            raise ValueError(
                "MH_01 bag size mismatch: expected {}, got {}".format(
                    expected_bag["size_bytes"], bag.stat().st_size
                )
            )
        dataset_before = file_identity(bag)
        if dataset_before["sha256"] != expected_bag["sha256"]:
            raise ValueError(
                "MH_01 bag SHA-256 mismatch: expected {}, got {}".format(
                    expected_bag["sha256"], dataset_before["sha256"]
                )
            )
        source, source_diff, serial_runner_patch = git_snapshot()
        if not source["pinned_base_is_ancestor"]:
            raise ValueError(
                "pinned upstream base {} is not an ancestor of project HEAD {}".format(
                    source["pinned_upstream_base"], source["commit"]
                )
            )
        if source["build_source_changes_from_pinned_base"] != source["allowed_build_source_changes"]:
            raise ValueError(
                "CP0 build-source delta must be exactly {}; got {}".format(
                    source["allowed_build_source_changes"],
                    source["build_source_changes_from_pinned_base"],
                )
            )
        expected_patch_hash = spec["frozen_source"]["serial_runner_patch_sha256"]
        if source["serial_runner_patch_sha256"] != expected_patch_hash:
            raise ValueError(
                "serial-runner lifecycle patch hash mismatch: expected {}, got {}".format(
                    expected_patch_hash, source["serial_runner_patch_sha256"]
                )
            )
        for relative, expected_hash in spec["frozen_source"]["input_sha256"].items():
            input_path = REPO_ROOT / relative
            actual_hash = sha256_file(input_path)
            if actual_hash != expected_hash:
                raise ValueError(
                    "frozen baseline input changed: {} expected {}, got {}".format(
                        relative, expected_hash, actual_hash
                    )
                )

        build_marker = WORKSPACE / "CP0_BUILD_PROVENANCE.json"
        build_provenance_tool = SCRIPT_DIR / "build_provenance.py"
        required_paths = [
            WORKSPACE / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf",
            CERES_PREFIX / "lib" / "libceres.so.1.14.0",
            REPO_ROOT / spec["dataset"]["ground_truth"],
            REPO_ROOT / spec["estimator"]["config_file"],
            REPO_ROOT / spec["estimator"]["launch_file"],
            build_provenance_tool,
            build_marker,
        ]
        for path in required_paths:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError("required CP0 input is missing: " + str(path))
        for command in ("git", "rosbag", "roslaunch", "rosversion", "evo_ape", "evo_rpe", "taskset"):
            command_available(command)
        build_provenance_record = run_capture(
            ["/usr/bin/python3", str(build_provenance_tool), "--verify", str(build_marker)]
        )
        if build_provenance_record.get("exit_code") != 0:
            raise ValueError(
                "stale or unidentified build: "
                + build_provenance_record.get("output", "").strip()
            )
        run_input_identities = reproducibility_input_identities()

        affinity_set: Optional[Set[int]] = None
        allowed_cpus = set(os.sched_getaffinity(0))
        if args.affinity:
            affinity_set = parse_cpu_list(args.affinity)
            unavailable = affinity_set - allowed_cpus
            if unavailable:
                raise ValueError("requested CPUs are unavailable: " + ",".join(str(cpu) for cpu in sorted(unavailable)))

        ros_port = args.ros_port or int(spec["runtime"]["ros_master_port"])
        assert_port_available(ros_port)
        run_id = args.run_id
        if run_id is None:
            timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = "cp0_mh01_{}-g{}".format(timestamp, source["commit"][:12])
        if SAFE_RUN_ID.fullmatch(run_id) is None or run_id in (".", ".."):
            raise ValueError("unsafe run id: " + run_id)

        staging_parent = REPO_ROOT / "results" / "staging" / "baseline"
        immutable_parent = REPO_ROOT / "results" / "immutable" / "baseline"
        run_dir = staging_parent / run_id
        final_dir = immutable_parent / run_id
        if final_dir.exists():
            raise ValueError("immutable destination already exists: " + str(final_dir))
        run_dir.mkdir(parents=True, exist_ok=False)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("CP0 preflight failed: " + str(exc), file=sys.stderr)
        return 2

    created_utc = utc_now()
    ACTIVE_FAILURE_CONTEXT.update(
        {"run_dir": run_dir, "run_id": run_id, "source": source}
    )
    print("CP0 staging directory: " + str(run_dir))
    atomic_write_bytes(run_dir / "source_tracked_binary.diff", source_diff)
    atomic_write_bytes(run_dir / "serial_runner_patch.diff", serial_runner_patch)
    reproducibility_snapshot = snapshot_reproducibility_inputs(
        run_dir, run_input_identities
    )
    build_marker_snapshot = run_dir / "reproducibility" / "build" / "CP0_BUILD_PROVENANCE.json"
    atomic_write_bytes(build_marker_snapshot, build_marker.read_bytes())
    print("Verified the frozen MH_01 dataset identity; starting launch preflight.")
    rosbag_info_record = run_capture(["rosbag", "info", "--yaml", str(bag)])
    atomic_write_text(run_dir / "dataset_rosbag_info.yaml", rosbag_info_record.get("output", ""))
    rosbag_info = parse_rosbag_info(rosbag_info_record.get("output", ""))

    fixed_env = minimal_runtime_environment()
    fixed_env.update({str(key): str(value) for key, value in spec["runtime"]["environment"].items()})
    fixed_env.update(
        {
            "MPLBACKEND": "Agg",
            "ROS_HOME": str(run_dir / "ros-home"),
            "ROS_LOG_DIR": str(run_dir / "ros-logs"),
            "ROS_MASTER_URI": "http://127.0.0.1:{}".format(ros_port),
            "ROS_HOSTNAME": "127.0.0.1",
            "ROS_IP": "127.0.0.1",
        }
    )
    (run_dir / "ros-home").mkdir()
    (run_dir / "ros-logs").mkdir()

    launch_file_source = REPO_ROOT / spec["estimator"]["launch_file"]
    config_file_source = REPO_ROOT / spec["estimator"]["config_file"]
    gt_file_source = REPO_ROOT / spec["dataset"]["ground_truth"]
    launch_file = run_dir / "reproducibility" / spec["estimator"]["launch_file"]
    config_file = run_dir / "reproducibility" / spec["estimator"]["config_file"]
    gt_file = run_dir / "reproducibility" / spec["dataset"]["ground_truth"]
    state_estimate = run_dir / "state_estimate.txt"
    state_deviation = run_dir / "state_deviation.txt"
    trajectory_tum = run_dir / "trajectory_tum.txt"
    timing_file = run_dir / "timing_openvins.csv"
    launch_arguments = [
        str(launch_file),
        "bag:=" + str(bag),
        "bag_start:=" + str(spec["dataset"]["bag_start_seconds"]),
        "config_path:=" + str(config_file),
        "path_state:=" + str(state_estimate),
        "path_std:=" + str(state_deviation),
        "path_time:=" + str(timing_file),
        "max_cameras:=" + str(spec["estimator"]["max_cameras"]),
        "use_stereo:=" + str(spec["estimator"]["stereo"]).lower(),
        "verbosity:=INFO",
    ]

    resolved_command = [command_available("roslaunch"), "--dump-params"] + launch_arguments
    resolved_record = run_capture(resolved_command, env=fixed_env)
    atomic_write_text(run_dir / "resolved_ros_parameters.yaml", resolved_record.get("output", ""))
    resolved_record["shell"] = shlex.join(resolved_command)
    if resolved_record.get("exit_code") != 0:
        atomic_write_text(
            run_dir / "preflight_error.txt",
            "roslaunch --dump-params failed; dataset execution was not started\n",
        )
        print(
            "CP0 launch preflight failed; no dataset replay occurred. Diagnostics: " + str(run_dir),
            file=sys.stderr,
        )
        finalize_failure_artifacts(
            run_dir,
            run_id,
            source,
            "roslaunch_parameter_resolution_failed",
            resolved_record.get("output", ""),
        )
        return 2

    launch_command = [command_available("roslaunch"), "-p", str(ros_port)] + launch_arguments
    if affinity_set is not None:
        launch_command = [command_available("taskset"), "--cpu-list", args.affinity] + launch_command

    command_records: Dict[str, Any] = {"resolve_parameters": resolved_record}
    command_records["roslaunch"] = run_streamed(
        launch_command,
        run_dir / "console.log",
        fixed_env,
        echo=False,
        timeout_seconds=float(spec["runtime"]["command_timeout_seconds"]),
    )
    print(
        "Serial MH_01 processing finished: exit={} timed_out={} wall={:.2f}s".format(
            command_records["roslaunch"]["exit_code"],
            command_records["roslaunch"].get("timed_out", False),
            command_records["roslaunch"]["duration_seconds"],
        )
    )

    converter_command = [
        "/usr/bin/python3",
        str(run_dir / "reproducibility" / "scripts" / "cp0" / "openvins_to_tum.py"),
        str(state_estimate),
        str(trajectory_tum),
    ]
    if state_estimate.is_file():
        command_records["trajectory_conversion"] = run_streamed(
            converter_command,
            run_dir / "trajectory_conversion.log",
            fixed_env,
            echo=True,
            timeout_seconds=float(spec["runtime"]["command_timeout_seconds"]),
        )
    else:
        command_records["trajectory_conversion"] = {
            "argv": converter_command,
            "shell": shlex.join(converter_command),
            "exit_code": 125,
            "error": "state_estimate.txt was not produced",
            "log": "trajectory_conversion.log",
        }
        atomic_write_text(run_dir / "trajectory_conversion.log", "total-state estimate was not produced\n")

    metric_commands = {
        "ape_translation": [
            command_available("evo_ape"),
            "tum",
            str(gt_file),
            str(trajectory_tum),
            "--t_max_diff",
            str(spec["evaluation"]["timestamp_association_max_difference_seconds"]),
            "--pose_relation",
            "trans_part",
            "--align",
            "--save_results",
            str(run_dir / "evo_ape_translation.zip"),
            "--no_warnings",
        ],
        "rpe_translation_1m": [
            command_available("evo_rpe"),
            "tum",
            str(gt_file),
            str(trajectory_tum),
            "--t_max_diff",
            str(spec["evaluation"]["timestamp_association_max_difference_seconds"]),
            "--pose_relation",
            "trans_part",
            "--delta",
            str(spec["evaluation"]["rpe_delta"]),
            "--delta_unit",
            str(spec["evaluation"]["rpe_delta_unit"]),
            "--all_pairs",
            "--pairs_from_reference",
            "--align",
            "--save_results",
            str(run_dir / "evo_rpe_translation_1m.zip"),
            "--no_warnings",
        ],
        "rpe_rotation_1m": [
            command_available("evo_rpe"),
            "tum",
            str(gt_file),
            str(trajectory_tum),
            "--t_max_diff",
            str(spec["evaluation"]["timestamp_association_max_difference_seconds"]),
            "--pose_relation",
            "angle_deg",
            "--delta",
            str(spec["evaluation"]["rpe_delta"]),
            "--delta_unit",
            str(spec["evaluation"]["rpe_delta_unit"]),
            "--all_pairs",
            "--pairs_from_reference",
            "--align",
            "--save_results",
            str(run_dir / "evo_rpe_rotation_1m_deg.zip"),
            "--no_warnings",
        ],
    }
    metric_logs = {
        "ape_translation": "evo_ape_translation.txt",
        "rpe_translation_1m": "evo_rpe_translation_1m.txt",
        "rpe_rotation_1m": "evo_rpe_rotation_1m_deg.txt",
    }
    for name, command in metric_commands.items():
        if trajectory_tum.is_file():
            command_records[name] = run_streamed(
                command,
                run_dir / metric_logs[name],
                fixed_env,
                echo=True,
                timeout_seconds=float(spec["runtime"]["command_timeout_seconds"]),
            )
        else:
            command_records[name] = {
                "argv": command,
                "shell": shlex.join(command),
                "exit_code": 125,
                "error": "trajectory_tum.txt was not produced",
                "log": metric_logs[name],
            }
            atomic_write_text(run_dir / metric_logs[name], "TUM trajectory was not produced\n")

    post_build_provenance_record = run_capture(
        ["/usr/bin/python3", str(build_provenance_tool), "--verify", str(build_marker)]
    )
    command_records["verify_build_after_run"] = post_build_provenance_record
    dataset_after = file_identity(bag)
    validation = validate_run(
        run_dir, spec, command_records, dataset_before, dataset_after, source
    )
    try:
        ros_cleanup = cleanup_ros_bookkeeping(run_dir)
        add_check(validation["checks"], "ros_bookkeeping_removed_before_sealing", True, ros_cleanup)
    except (OSError, ValueError) as exc:
        ros_cleanup = {"error": str(exc)}
        add_check(validation["checks"], "ros_bookkeeping_removed_before_sealing", False, ros_cleanup)
        validation["errors"].append("ROS bookkeeping cleanup failed: " + str(exc))
        validation["passed"] = False
    atomic_write_json(run_dir / "validation.json", validation)

    config_files = [
        config_file_source,
        config_file_source.parent / "kalibr_imu_chain.yaml",
        config_file_source.parent / "kalibr_imucam_chain.yaml",
        launch_file_source,
        SPEC_PATH,
    ]
    executable = WORKSPACE / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
    build_config = WORKSPACE / ".catkin_tools" / "profiles" / "default" / "config.yaml"
    cmake_cache = WORKSPACE / "build" / "ov_msckf" / "CMakeCache.txt"
    effective_cpus = affinity_set if affinity_set is not None else allowed_cpus
    source["persisted_delta_artifact_identity"] = file_identity(
        run_dir / "source_tracked_binary.diff"
    )
    manifest = {
        "schema_version": 1,
        "checkpoint": "CP0",
        "run_id": run_id,
        "status": "passed" if validation["passed"] else "failed",
        "created_utc": created_utc,
        "finished_utc": utc_now(),
        "artifact_locations": {
            "staging": str(run_dir.relative_to(REPO_ROOT)),
            "immutable": str(final_dir.relative_to(REPO_ROOT)),
            "seal_requested": not args.no_seal,
            "seal_preconditions_passed": bool(validation["passed"]),
            "seal_method": "Linux renameat2(RENAME_NOREPLACE)",
        },
        "source": source,
        "reproducibility_inputs": run_input_identities,
        "reproducibility_snapshot": reproducibility_snapshot,
        "dataset": {
            "name": spec["dataset"]["name"],
            "configuration": spec["dataset"]["configuration"],
            "identity": dataset_before,
            "rosbag_info": rosbag_info,
            "rosbag_info_exit_code": rosbag_info_record.get("exit_code"),
            "bag_start_seconds": spec["dataset"]["bag_start_seconds"],
            "ground_truth": file_identity(gt_file_source),
            "ground_truth_snapshot": file_identity(gt_file),
            "ground_truth_used_by_estimator": False,
        },
        "configuration": {
            "baseline_spec": spec,
            "files": {str(path.relative_to(REPO_ROOT)): sha256_file(path) for path in config_files},
            "resolved_parameters_file": "resolved_ros_parameters.yaml",
        },
        "build": {
            "workspace": str(WORKSPACE),
            "python_executable": sys.executable,
            "python_version": platform.python_version(),
            "estimator_executable": file_identity(executable),
            "catkin_profile_sha256": sha256_file(build_config) if build_config.is_file() else None,
            "ov_msckf_cmake_cache_sha256": sha256_file(cmake_cache) if cmake_cache.is_file() else None,
            "provenance_marker": file_identity(build_marker_snapshot),
            "provenance_verification": build_provenance_record,
            "dependencies": dependency_metadata(),
        },
        "runtime": {
            "fixed_environment": {key: fixed_env[key] for key in sorted(spec["runtime"]["environment"])},
            "allowlisted_subprocess_environment": {
                key: fixed_env[key] for key in sorted(fixed_env)
            },
            "additional_environment": {
                "MPLBACKEND": fixed_env["MPLBACKEND"],
                "ROS_MASTER_URI": fixed_env["ROS_MASTER_URI"],
                "ROS_HOSTNAME": fixed_env["ROS_HOSTNAME"],
                "ROS_IP": fixed_env["ROS_IP"],
            },
            "affinity_requested": args.affinity,
            "affinity_effective_cpus": sorted(effective_cpus),
            "cpu_governors": read_governors(effective_cpus),
            "commands": command_records,
            "ros_bookkeeping": ros_cleanup,
            "environment_policy": "minimal allowlist; arbitrary inherited variables excluded",
            "expected_roslaunch_wording": (
                "REQUIRED process [cp0_vio-N] has died! followed by "
                "process has finished cleanly is a successful required-node shutdown"
            ),
        },
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "uname": list(platform.uname()),
            "os_release": read_os_release(),
            "logical_cpu_count": os.cpu_count(),
            "initial_process_affinity": sorted(allowed_cpus),
        },
        "validation": validation,
    }
    atomic_write_json(run_dir / "manifest.json", manifest)
    try:
        generate_checksums(run_dir)
    except (OSError, ValueError) as exc:
        print("CP0 checksum generation failed: " + str(exc), file=sys.stderr)
        return 1

    fsync_directory(run_dir)
    fsync_directory(run_dir.parent)
    if not validation["passed"]:
        print("CP0 FAILED; diagnostic artifacts remain in staging: " + str(run_dir), file=sys.stderr)
        for error_message in validation["errors"]:
            print("  - " + error_message, file=sys.stderr)
        return 1
    if args.no_seal:
        print("CP0 PASSED; --no-seal left the artifacts in staging: " + str(run_dir))
        return 0

    try:
        atomic_rename_noreplace(run_dir, final_dir)
    except OSError as exc:
        print("CP0 passed but atomic sealing failed: " + str(exc), file=sys.stderr)
        print("Passing artifacts remain in staging: " + str(run_dir), file=sys.stderr)
        return 1
    checksum_anchor = sha256_file(final_dir / "SHA256SUMS")
    print("CP0 PASSED and was atomically sealed without overwrite:")
    print("  " + str(final_dir))
    print("Record this digest in the checkpoint registry/Git:")
    print("  SHA256SUMS SHA-256: " + checksum_anchor)
    return 0


def entrypoint() -> int:
    try:
        return main()
    except Exception as exc:  # Preserve evidence for every post-staging failure.
        diagnostic = traceback.format_exc()
        context = ACTIVE_FAILURE_CONTEXT
        if context:
            try:
                finalize_failure_artifacts(
                    context["run_dir"],
                    context["run_id"],
                    context["source"],
                    type(exc).__name__ + ": " + str(exc),
                    diagnostic,
                )
            except Exception as finalizer_exc:
                print(
                    "CP0 failure finalizer also failed: " + str(finalizer_exc),
                    file=sys.stderr,
                )
        print("CP0 execution failed unexpectedly:\n" + diagnostic, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
