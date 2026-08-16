#!/usr/bin/python3
"""Append-only KAIST development runner for the S1 rotation-outage study.

This runner is intentionally separate from the frozen G0.5 campaign.  It
allows an explicitly named candidate launch/configuration while preserving the
serial exact-header input seam.  Ground truth is not opened until the complete
estimator process group (and, for capture replays, recorder/master groups) has
closed.

The ``scored`` and ``capture`` modes are separate estimator replays.  Capture
mode records the sparse-map ROS topics and then builds the deterministic atlas;
it is not substituted for the scored trajectory.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import zipfile

import numpy as np
import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PYTHON = Path("/usr/bin/python3")
CONVERTER = REPO_ROOT / "scripts" / "cp0" / "openvins_to_tum.py"
PAIRING_CENSUS = SCRIPT_DIR / "kaist_pairing_census.py"
GEOMETRY_BUNDLE = SCRIPT_DIR / "kaist_geometry_bundle.py"
SOURCE_SNAPSHOT = REPO_ROOT / "scripts" / "turnsafe" / "source_snapshot.py"
DEFAULT_LAUNCH = REPO_ROOT / "project" / "rotation_robustness_serial.launch"
DEFAULT_CONFIG = (
    REPO_ROOT / "config" / "kaist_vio_turnsafe_baseline" / "estimator_config.yaml"
)
DEFAULT_BUILD_ROOT = REPO_ROOT / "build" / "cp0-ws"
DEFAULT_BINARY = DEFAULT_BUILD_ROOT / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
DEFAULT_ARTIFACT_ROOT = Path(
    "/home/moksh/schurvio-icra27-artifacts/rotation-robustness"
)

SCHEMA = "schurvio.icra27.rotation_robustness_trial.v1"
GAP_SCHEMA = "schurvio.icra27.rotation_gap_metrics.v1"

SEQUENCES: Tuple[str, ...] = (
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

# Frozen from the selected-stereo census and the byte-identical S1 control.
ROTATION_GAP: Mapping[str, Any] = {
    "last_pre_gap_selected_header_stamp_ns": 1599131266390101559,
    "first_post_gap_selected_header_stamp_ns": 1599131279832098734,
    "selected_stereo_gap_seconds": 13.441997175,
    "last_pre_gap_estimator_timestamp_s": 1599131266.36014,
    "first_post_gap_estimator_timestamp_s": 1599131279.80214,
    "anchor_search_limit_seconds": 0.10,
    "association_max_difference_seconds": 0.01,
}

SERIAL_SUMMARY_PREFIX = "[SERIAL-KAIST]: exact_header_pairs="
SERIAL_SUMMARY_RE = re.compile(
    r"\[SERIAL-KAIST\]: exact_header_pairs=(?P<exact_header_pairs>[0-9]+) "
    r"camera0_without_match=(?P<camera0_without_match>[0-9]+) "
    r"camera1_without_match=(?P<camera1_without_match>[0-9]+) "
    r"record_delta_ge_20ms=(?P<record_delta_ge_20ms>[0-9]+) "
    r"maximum_record_delta_ns=(?P<maximum_record_delta_ns>[0-9]+)"
)
ENQUEUE_SUMMARY_PREFIX = "[SERIAL-KAIST]: queued_pairs="
ENQUEUE_SUMMARY_RE = re.compile(
    r"\[SERIAL-KAIST\]: queued_pairs=(?P<queued_pairs>[0-9]+) "
    r"processed_pairs=(?P<processed_pairs>[0-9]+) "
    r"frequency_thinned_pairs=(?P<frequency_thinned_pairs>[0-9]+) "
    r"cam0_decode_failures=(?P<cam0_decode_failures>[0-9]+) "
    r"cam1_decode_failures=(?P<cam1_decode_failures>[0-9]+) "
    r"pending_pairs=(?P<pending_pairs>[0-9]+)"
)
REQUIRED_PROCESS_DIED_RE = re.compile(
    r"=+REQUIRED process \[(?P<node>[^\]]+)\] has died!"
)
PROCESS_DIED_RE = re.compile(
    r"process has died \[pid (?P<pid>[0-9]+), exit code (?P<exit_code>-?[0-9]+), "
    r"cmd (?P<command>.+)\]\."
)
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}")
FORBIDDEN_RUNTIME_TOKENS = (
    "/pose_transformed",
    "ground_truth",
    "groundtruth",
    "path_gt",
    "initialize_with_gt",
)
FIXED_ENVIRONMENT = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONHASHSEED": "0",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "MPLBACKEND": "Agg",
}
GEOMETRY_TOPICS = (
    "/kaist_vio_turnsafe_baseline/poseimu",
    "/kaist_vio_turnsafe_baseline/points_slam",
    "/kaist_vio_turnsafe_baseline/points_msckf",
    "/kaist_vio_turnsafe_baseline/points_aruco",
    "/kaist_vio_turnsafe_baseline/loop_feats",
)


class TrialError(RuntimeError):
    """A visible, fail-closed trial error."""


class NoRowsError(TrialError):
    """A numeric output exists but contains no data rows."""


@dataclass(frozen=True)
class Pose:
    timestamp: float
    position: np.ndarray
    quaternion_xyzw: np.ndarray


@dataclass
class ManagedProcess:
    argv: List[str]
    process: subprocess.Popen
    stream: Any
    log_path: Path
    started_utc: str
    started_monotonic: float


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
        "executable": bool(stat.st_mode & 0o111),
        "mtime_ns": stat.st_mtime_ns,
    }


def _regular_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise TrialError("{} does not resolve: {}".format(label, path)) from exc
    if not resolved.is_file():
        raise TrialError("{} is not a regular file: {}".format(label, resolved))
    return resolved


def _directory(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise TrialError("{} does not resolve: {}".format(label, path)) from exc
    if not resolved.is_dir():
        raise TrialError("{} is not a directory: {}".format(label, resolved))
    return resolved


def _require_within(path: Path, root: Path, label: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise TrialError("{} is outside the current worktree: {}".format(label, path)) from exc


def _atomic_write_new_bytes(path: Path, payload: bytes) -> None:
    """Publish complete bytes atomically, without an overwrite race."""

    if path.exists() or path.is_symlink():
        raise TrialError("refusing to overwrite {}".format(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: Optional[str] = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".{}.".format(path.name), suffix=".tmp", dir=str(path.parent)
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, str(path))
        except FileExistsError as exc:
            raise TrialError("refusing to overwrite {}".format(path)) from exc
        os.unlink(temporary_name)
        temporary_name = None
        directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def atomic_write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode("utf-8")
    _atomic_write_new_bytes(path, payload)


def write_sha256sums(run_dir: Path) -> Dict[str, str]:
    destination = run_dir / "SHA256SUMS"
    if destination.exists() or destination.is_symlink():
        raise TrialError("refusing to overwrite SHA256SUMS")
    entries: Dict[str, str] = {}
    for path in run_dir.rglob("*"):
        if not path.is_file() or path == destination:
            continue
        relative = path.relative_to(run_dir).as_posix()
        if "\n" in relative or "\r" in relative or relative.startswith("/"):
            raise TrialError("unsafe artifact path in checksum set")
        entries[relative] = sha256_file(path)
    lines = ["{}  {}\n".format(entries[name], name) for name in sorted(entries)]
    _atomic_write_new_bytes(destination, "".join(lines).encode("utf-8"))
    return entries


def create_run_directory(output_root: Path, run_id: str) -> Path:
    if SAFE_ID_RE.fullmatch(run_id) is None:
        raise TrialError("run ID must match {}".format(SAFE_ID_RE.pattern))
    root = output_root.expanduser().resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    run_dir = root / run_id
    try:
        run_dir.mkdir(mode=0o755)
    except FileExistsError as exc:
        raise TrialError("run ID already exists; refusing overwrite: {}".format(run_id)) from exc
    for relative in (
        "diagnostics",
        "trajectory",
        "metrics",
        "source",
        "isolated-home",
        "ros-home",
        "ros-logs",
    ):
        (run_dir / relative).mkdir()
    return run_dir


def minimal_environment(run_dir: Path, ros_port: int) -> Dict[str, str]:
    allowed = {
        "CMAKE_PREFIX_PATH",
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
    environment = {
        key: os.environ[key] for key in sorted(allowed) if key in os.environ
    }
    environment.update(FIXED_ENVIRONMENT)
    environment.update(
        {
            "HOME": str(run_dir / "isolated-home"),
            "ROS_HOME": str(run_dir / "ros-home"),
            "ROS_LOG_DIR": str(run_dir / "ros-logs"),
            "ROS_HOSTNAME": "127.0.0.1",
            "ROS_IP": "127.0.0.1",
            "ROS_MASTER_URI": "http://127.0.0.1:{}".format(ros_port),
        }
    )
    return dict(sorted(environment.items()))


def assert_port_available(port: int) -> None:
    if port < 1024 or port > 65535:
        raise TrialError("ROS port must be in [1024, 65535]")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise TrialError("ROS port {} is unavailable: {}".format(port, exc)) from exc
    finally:
        probe.close()


def _port_open(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.1)
        return probe.connect_ex(("127.0.0.1", port)) == 0
    finally:
        probe.close()


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _clean_process_group(
    process: subprocess.Popen, first_signal: signal.Signals = signal.SIGINT
) -> Tuple[List[str], bool]:
    order: List[Tuple[signal.Signals, float]] = []
    if first_signal == signal.SIGINT:
        order.append((signal.SIGINT, 5.0))
    order.extend(((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)))
    signals_sent: List[str] = []
    for stop_signal, grace in order:
        if not _process_group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, stop_signal)
            signals_sent.append(stop_signal.name)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            continue
    return signals_sent, _process_group_exists(process.pid)


def run_command(
    argv: Sequence[str],
    log_path: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> Dict[str, Any]:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise TrialError("command timeout must be finite and positive")
    started_utc = utc_now()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    error: Optional[str] = None
    exit_code: Optional[int] = None
    signals_sent: List[str] = []
    surviving_group = False
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("x", encoding="utf-8", errors="replace") as stream:
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(REPO_ROOT),
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                signals_sent, surviving_group = _clean_process_group(process)
                exit_code = process.poll()
            except KeyboardInterrupt:
                interrupted = True
                signals_sent, surviving_group = _clean_process_group(process)
                exit_code = process.poll()
                raise
            else:
                if _process_group_exists(process.pid):
                    extra, surviving_group = _clean_process_group(process)
                    signals_sent.extend(extra)
        except OSError as exc:
            error = str(exc)
            stream.write("failed to execute command: {}\n".format(exc))
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "argv": list(argv),
        "shell": shlex.join(list(argv)),
        "cwd": str(REPO_ROOT),
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "timeout_seconds": timeout_seconds,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "interrupted": interrupted,
        "signals_sent": signals_sent,
        "process_group_survived_cleanup": surviving_group,
        "error": error,
        "log": file_identity(log_path),
    }


def command_succeeded(record: Mapping[str, Any]) -> bool:
    return (
        record.get("exit_code") == 0
        and not record.get("timed_out", False)
        and not record.get("interrupted", False)
        and not record.get("process_group_survived_cleanup", False)
        and record.get("error") is None
    )


def classify_trial_status(
    launch_record: Optional[Mapping[str, Any]],
    estimator_child_died: bool,
    state_valid: bool,
    output_valid: bool,
    seam_valid: bool,
    provenance_valid: bool,
    evaluation_valid: bool,
    capture_valid: bool,
) -> str:
    """Classify scientific outcome independently of roslaunch wrapper quirks."""

    launch_success = (
        launch_record is not None
        and command_succeeded(launch_record)
        and not estimator_child_died
    )
    if launch_record is not None and launch_record.get("timed_out"):
        return "TIMED_OUT"
    if not state_valid:
        return "NO_INITIALIZATION" if launch_success else "ESTIMATOR_FAILED"
    if not output_valid:
        return "INVALID_OUTPUT"
    if not launch_success:
        return "PARTIAL"
    if not seam_valid:
        return "INVALID_RUNTIME_EVIDENCE"
    if not provenance_valid:
        return "INVALID_PROVENANCE"
    if not evaluation_valid:
        return "EVALUATION_FAILED"
    if not capture_valid:
        return "CAPTURE_FAILED"
    return "COMPLETED"


def start_managed_process(
    argv: Sequence[str], log_path: Path, environment: Mapping[str, str]
) -> ManagedProcess:
    stream = log_path.open("x", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            list(argv),
            cwd=str(REPO_ROOT),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except BaseException:
        stream.close()
        raise
    return ManagedProcess(
        argv=list(argv),
        process=process,
        stream=stream,
        log_path=log_path,
        started_utc=utc_now(),
        started_monotonic=time.monotonic(),
    )


def finish_managed_process(managed: ManagedProcess) -> Dict[str, Any]:
    signals_sent, survived = _clean_process_group(managed.process)
    exit_code = managed.process.poll()
    managed.stream.flush()
    os.fsync(managed.stream.fileno())
    managed.stream.close()
    return {
        "argv": managed.argv,
        "shell": shlex.join(managed.argv),
        "cwd": str(REPO_ROOT),
        "started_utc": managed.started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - managed.started_monotonic,
        "exit_code": exit_code,
        "signals_sent": signals_sent,
        "process_group_survived_cleanup": survived,
        "log": file_identity(managed.log_path),
    }


def _wait_for_port(port: int, expected_open: bool, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_open(port) == expected_open:
            return
        time.sleep(0.1)
    raise TrialError(
        "ROS port {} did not become {}".format(port, "open" if expected_open else "closed")
    )


def _which(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise TrialError("required command is unavailable: {}".format(name))
    return Path(value).resolve(strict=True)


def _git_source_state(run_dir: Path, suffix: str) -> Dict[str, Any]:
    commands = {
        "status": [
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        "diff": ["git", "-C", str(REPO_ROOT), "diff", "--binary", "--no-ext-diff"],
        "diff_cached": [
            "git",
            "-C",
            str(REPO_ROOT),
            "diff",
            "--cached",
            "--binary",
            "--no-ext-diff",
        ],
    }
    environment = dict(os.environ)
    environment.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"})
    records: Dict[str, Any] = {}
    for name, argv in commands.items():
        completed = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=environment,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        path = run_dir / "source" / "{}_{}.txt".format(name, suffix)
        payload = completed.stdout
        if completed.stderr:
            payload += b"\n[stderr]\n" + completed.stderr
        _atomic_write_new_bytes(path, payload)
        records[name] = {
            "argv": argv,
            "exit_code": completed.returncode,
            "artifact": file_identity(path),
        }
        if completed.returncode != 0:
            raise TrialError("git source-state command failed: {}".format(name))
    head = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD^{tree}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()
    return {
        "head_sha": head,
        "head_tree": tree,
        "dirty": records["status"]["artifact"]["size_bytes"] != 0,
        "commands": records,
    }


def _run_source_snapshot(
    run_dir: Path,
    suffix: str,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    output = run_dir / "source" / "source_snapshot_{}.json".format(suffix)
    record = run_command(
        [str(PYTHON), str(SOURCE_SNAPSHOT), str(REPO_ROOT), str(output)],
        run_dir / "source" / "source_snapshot_{}.log".format(suffix),
        environment,
        timeout_seconds,
    )
    if not command_succeeded(record) or not output.is_file():
        raise TrialError("source snapshot generation failed")
    value = json.loads(output.read_text(encoding="utf-8"))
    if value.get("schema_version") != "turnsafe.source_snapshot.v1":
        raise TrialError("live source snapshot schema mismatch")
    return record, value


def _compiled_records(snapshot: Mapping[str, Any]) -> Dict[str, Tuple[int, str]]:
    result: Dict[str, Tuple[int, str]] = {}
    for field in ("compiled_inputs", "untracked_compiled_inputs"):
        values = snapshot.get(field, [])
        if not isinstance(values, list):
            raise TrialError("source snapshot {} is not a list".format(field))
        for value in values:
            if not isinstance(value, dict):
                raise TrialError("source snapshot compiled record is not an object")
            path = str(value.get("path", ""))
            record = (int(value.get("size_bytes", -1)), str(value.get("sha256", "")))
            if not path or path in result:
                raise TrialError("invalid or duplicate compiled-input path")
            result[path] = record
    return result


def validate_build_binding(
    build_root: Path,
    binary: Path,
    live_snapshot: Mapping[str, Any],
    explicit_build_manifest: Optional[Path],
) -> Dict[str, Any]:
    _require_within(binary, build_root, "estimator binary")
    configure_path = (
        build_root
        / "build"
        / "ov_msckf"
        / "turnsafe-generated"
        / "configure_provenance.json"
    )
    configure_path = _regular_file(configure_path, "configure provenance")
    configure = json.loads(configure_path.read_text(encoding="utf-8"))
    if configure.get("schema_version") != "turnsafe.configure_provenance.v1":
        raise TrialError("configure provenance schema mismatch")
    configured_snapshot = configure.get("source_snapshot")
    if not isinstance(configured_snapshot, dict):
        raise TrialError("configure provenance lacks its source snapshot")
    configured_records = _compiled_records(configured_snapshot)
    live_records = _compiled_records(live_snapshot)
    if configured_records != live_records:
        missing = sorted(set(live_records) - set(configured_records))
        extra = sorted(set(configured_records) - set(live_records))
        changed = sorted(
            path
            for path in set(configured_records).intersection(live_records)
            if configured_records[path] != live_records[path]
        )
        raise TrialError(
            "compiled sources differ from configured build provenance: "
            "missing={} extra={} changed={}".format(missing, extra, changed)
        )
    configuration = configure.get("descriptor", {}).get("configuration", {})
    source_dir = Path(str(configuration.get("cmake_source_directory", ""))).resolve(
        strict=False
    )
    if source_dir != (REPO_ROOT / "ov_msckf").resolve(strict=True):
        raise TrialError("build provenance points at a different source worktree")
    latest_source_mtime = max(
        (REPO_ROOT / path).stat().st_mtime_ns for path in configured_records
    )
    result: Dict[str, Any] = {
        "configure_provenance": file_identity(configure_path),
        "build_provenance_id": configure.get("build_provenance_id"),
        "configured_source": {
            "head_sha": configured_snapshot.get("head_sha"),
            "head_tree": configured_snapshot.get("head_tree"),
            "aggregate_source_snapshot_sha256": configured_snapshot.get(
                "aggregate_source_snapshot_sha256"
            ),
        },
        "compiled_input_count": len(configured_records),
        "compiled_inputs_match_live_bytes": True,
        "binary_not_older_than_live_compiled_inputs": (
            binary.stat().st_mtime_ns >= latest_source_mtime
        ),
        "binary": file_identity(binary),
    }
    if explicit_build_manifest is not None:
        manifest_path = _regular_file(explicit_build_manifest, "build manifest")
        _require_within(manifest_path, build_root, "build manifest")
        result["build_manifest"] = file_identity(manifest_path)
    else:
        result["build_manifest"] = {
            "status": "NOT_PROVIDED",
            "reason": "configure provenance and live compiled-byte binding retained",
        }
    return result


def parse_runtime_summaries(text: str) -> Dict[str, Any]:
    clean_lines = [ANSI_RE.sub("", line) for line in text.splitlines()]
    selection_lines = [line for line in clean_lines if SERIAL_SUMMARY_PREFIX in line]
    enqueue_lines = [line for line in clean_lines if ENQUEUE_SUMMARY_PREFIX in line]
    if len(selection_lines) != 1:
        raise TrialError(
            "expected one exact-header summary; found {}".format(len(selection_lines))
        )
    if len(enqueue_lines) != 1:
        raise TrialError(
            "expected one camera-enqueue summary; found {}".format(len(enqueue_lines))
        )
    selection_match = SERIAL_SUMMARY_RE.fullmatch(selection_lines[0])
    enqueue_match = ENQUEUE_SUMMARY_RE.fullmatch(enqueue_lines[0])
    if selection_match is None or enqueue_match is None:
        raise TrialError("malformed SERIAL-KAIST runtime summary")
    selection = {key: int(value) for key, value in selection_match.groupdict().items()}
    enqueue = {key: int(value) for key, value in enqueue_match.groupdict().items()}
    if enqueue["queued_pairs"] + enqueue["frequency_thinned_pairs"] != selection[
        "exact_header_pairs"
    ]:
        raise TrialError("exact-header queue/thinning accounting does not close")
    if enqueue["processed_pairs"] != enqueue["queued_pairs"]:
        raise TrialError("processed and queued pair counts differ")
    if enqueue["pending_pairs"] != 0:
        raise TrialError("camera queue did not drain")
    if enqueue["cam0_decode_failures"] or enqueue["cam1_decode_failures"]:
        raise TrialError("camera decode failure reported")
    return {
        "exact_header": {"line": selection_lines[0], "counts": selection},
        "camera_enqueue": {"line": enqueue_lines[0], "counts": enqueue},
        "checks": {
            "pair_accounting_complete": True,
            "processed_pairs_match_queued": True,
            "queue_drained": True,
            "decode_failures_zero": True,
        },
    }


def parse_roslaunch_child_deaths(text: str) -> Dict[str, Any]:
    """Extract required-child deaths that roslaunch may mask with exit zero."""

    clean_lines = [ANSI_RE.sub("", line) for line in text.splitlines()]
    required_nodes: List[str] = []
    deaths: List[Dict[str, Any]] = []
    for line in clean_lines:
        required_match = REQUIRED_PROCESS_DIED_RE.search(line)
        if required_match is not None:
            required_nodes.append(required_match.group("node"))
        death_match = PROCESS_DIED_RE.search(line)
        if death_match is not None:
            deaths.append(
                {
                    "pid": int(death_match.group("pid")),
                    "exit_code": int(death_match.group("exit_code")),
                    "command": death_match.group("command"),
                    "line": line,
                }
            )
    estimator_required_death = any(
        node.split("-", 1)[0] == "kaist_vio_turnsafe_baseline"
        for node in required_nodes
    )
    return {
        "required_process_death_detected": bool(required_nodes),
        "required_nodes": required_nodes,
        "process_deaths": deaths,
        "estimator_required_child_died": estimator_required_death,
        "wrapper_exit_code_is_not_estimator_success": estimator_required_death,
    }


def bind_pairing_census(
    summaries: Mapping[str, Any], census: Mapping[str, Any]
) -> Dict[str, Any]:
    if census.get("schema") != "schurvio.icra27.kaist_pairing_census.v1":
        raise TrialError("pairing census schema mismatch")
    values = census.get("census")
    if not isinstance(values, dict):
        raise TrialError("pairing census lacks flat census values")
    static_selection = {
        "selected_input": census.get("selection_bounds", {}).get("s1_exact_header"),
        "selected_first_header_stamp_ns": values.get(
            "s1_first_selected_header_stamp_ns"
        ),
        "selected_last_header_stamp_ns": values.get("s1_last_selected_header_stamp_ns"),
    }
    if "exact_header" not in summaries or "camera_enqueue" not in summaries:
        return {
            "status": "UNAVAILABLE",
            "reason": "runtime_summary_unavailable",
            "static_census_retained": True,
            **static_selection,
        }
    observed = summaries["exact_header"]["counts"]
    expected = {
        "exact_header_pairs": values.get("s1_exact_pair_count"),
        "camera0_without_match": values.get("camera0_unmatched_count"),
        "camera1_without_match": values.get("camera1_unmatched_count"),
    }
    mismatches = {
        key: {"runtime": observed[key], "static": value}
        for key, value in expected.items()
        if observed[key] != value
    }
    if mismatches:
        raise TrialError("runtime/static exact-header mismatch: {}".format(mismatches))
    enqueue = summaries["camera_enqueue"]["counts"]
    return {
        "status": "AVAILABLE",
        "reason": "NONE",
        "runtime_matches_static_census": True,
        "static_expected": expected,
        **static_selection,
        "runtime_delivery": {
            "s1_queued_pair_count": enqueue["queued_pairs"],
            "s1_processed_pair_count": enqueue["processed_pairs"],
            "s1_frequency_thinned_pair_count": enqueue["frequency_thinned_pairs"],
            "s1_pending_pair_count": enqueue["pending_pairs"],
        },
    }


def _unique_parameter(parameters: Mapping[str, Any], suffix: str) -> Any:
    matches = [value for key, value in parameters.items() if key.endswith("/" + suffix)]
    if len(matches) != 1:
        raise TrialError(
            "resolved parameter {} occurs {} times".format(suffix, len(matches))
        )
    return matches[0]


def validate_resolved_parameters(
    raw: str, bag: Path, config: Path
) -> Dict[str, Any]:
    try:
        parameters = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TrialError("resolved ROS parameter YAML is invalid") from exc
    if not isinstance(parameters, dict):
        raise TrialError("resolved ROS parameters are not a mapping")
    expected: Mapping[str, Any] = {
        "path_bag": str(bag),
        "config_path": str(config),
        "kaist_vio_exact_header_stereo": True,
        "cam0_rostopic": "/turnsafe/kaist/infra1/image_raw",
        "cam1_rostopic": "/turnsafe/kaist/infra2/image_raw",
        "imu0_rostopic": "/mavros/imu/data",
        "use_fej": True,
        "use_stereo": True,
        "max_cameras": 2,
        "feat_rep_msckf": "GLOBAL_3D",
        "up_msckf_landmark_elimination": "schur",
        "up_msckf_max_visual_passes": 1,
        "calib_cam_extrinsics": False,
        "calib_cam_intrinsics": False,
        "calib_cam_timeoffset": False,
        "calib_imu_intrinsics": False,
        "calib_imu_g_sensitivity": False,
        "num_opencv_threads": 0,
        "multi_threading_pubs": False,
        "multi_threading_subs": False,
        "save_total_state": True,
        "record_timing_information": True,
    }
    observed: Dict[str, Any] = {}
    mismatches: Dict[str, Any] = {}
    for name, wanted in expected.items():
        value = _unique_parameter(parameters, name)
        if name in ("path_bag", "config_path"):
            value = str(Path(str(value)).resolve(strict=False))
        observed[name] = value
        if value != wanted:
            mismatches[name] = {"observed": value, "expected": wanted}
    if mismatches:
        raise TrialError("resolved S1 seam mismatch: {}".format(mismatches))
    return {
        "parameter_count": len(parameters),
        "critical_parameters": observed,
        "exact_header_seam_preserved": True,
        "serial_execution_seam_preserved": True,
    }


def validate_runtime_firewall(
    launch_argv: Sequence[str],
    resolved_parameters: str,
    candidate_config_text: str,
    bag_info_text: str,
    reference_path: Path,
) -> Dict[str, Any]:
    joined_argv = "\n".join(launch_argv).lower()
    reference = str(reference_path.resolve(strict=False)).lower()
    surfaces = {
        "launch_argv": joined_argv,
        "resolved_parameters": resolved_parameters.lower(),
        "candidate_config": candidate_config_text.lower(),
        "adapted_bag_metadata": bag_info_text.lower(),
    }
    findings: Dict[str, List[str]] = {}
    for name, value in surfaces.items():
        hits = [token for token in FORBIDDEN_RUNTIME_TOKENS if token in value]
        if reference and reference in value:
            hits.append("exact_reference_path")
        if hits:
            findings[name] = sorted(set(hits))
    if findings:
        raise TrialError("ground truth reached a runtime surface: {}".format(findings))
    return {
        "reference_not_opened_before_estimator_close": True,
        "launch_argv_clean": True,
        "resolved_parameters_clean": True,
        "candidate_config_clean": True,
        "adapted_bag_metadata_clean": True,
        "forbidden_tokens": list(FORBIDDEN_RUNTIME_TOKENS),
    }


def validate_numeric_table(
    path: Path, minimum_columns: int, separator: Optional[str] = None
) -> Dict[str, Any]:
    if not path.is_file():
        raise TrialError("numeric output is missing: {}".format(path))
    rows = 0
    columns: Optional[int] = None
    first: Optional[float] = None
    last: Optional[float] = None
    maximum_gap = 0.0
    previous = -math.inf
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, raw in enumerate(stream, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = (
                [field.strip() for field in stripped.split(separator)]
                if separator is not None
                else stripped.split()
            )
            try:
                values = [float(field) for field in fields]
            except ValueError as exc:
                raise TrialError(
                    "nonnumeric output at {}:{}".format(path, line_number)
                ) from exc
            if len(values) < minimum_columns:
                raise TrialError("too few columns at {}:{}".format(path, line_number))
            if not all(math.isfinite(value) for value in values):
                raise TrialError("nonfinite output at {}:{}".format(path, line_number))
            if values[0] <= previous:
                raise TrialError(
                    "timestamps are not strictly increasing at {}:{}".format(
                        path, line_number
                    )
                )
            if columns is None:
                columns = len(values)
                first = values[0]
            elif columns != len(values):
                raise TrialError("inconsistent column count at {}:{}".format(path, line_number))
            if rows:
                maximum_gap = max(maximum_gap, values[0] - previous)
            previous = values[0]
            last = values[0]
            rows += 1
    if rows == 0:
        raise NoRowsError("numeric output contains no rows: {}".format(path))
    identity = file_identity(path)
    return {
        **identity,
        "rows": rows,
        "columns": columns,
        "first_timestamp": first,
        "last_timestamp": last,
        "maximum_timestamp_gap": maximum_gap,
        "timestamps_strictly_increasing": True,
        "all_values_finite": True,
    }


def _table_timestamps(path: Path, separator: Optional[str] = None) -> List[float]:
    result: List[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split(separator) if separator is not None else stripped.split()
        result.append(float(fields[0].strip()))
    return result


def validate_output_consistency(
    state: Path, deviation: Path, timing: Path, trajectory: Path
) -> Dict[str, Any]:
    state_times = _table_timestamps(state)
    deviation_times = _table_timestamps(deviation)
    timing_times = _table_timestamps(timing, ",")
    trajectory_times = _table_timestamps(trajectory)
    if state_times != deviation_times or state_times != trajectory_times:
        raise TrialError("state/deviation/trajectory timestamp sequences differ")
    if len(timing_times) != len(state_times):
        raise TrialError("timing and state row counts differ")
    maximum = max(abs(a - b) for a, b in zip(timing_times, state_times))
    if maximum > 1.0e-5:
        raise TrialError("timing/state timestamps differ by more than 1e-5 s")
    return {
        "row_count": len(state_times),
        "state_deviation_trajectory_timestamps_exact": True,
        "timing_state_maximum_absolute_difference_seconds": maximum,
        "timing_state_tolerance_seconds": 1.0e-5,
        "consistent": True,
    }


def read_tum(path: Path) -> List[Pose]:
    poses: List[Pose] = []
    previous = -math.inf
    with path.open("r", encoding="utf-8", errors="strict") as stream:
        for line_number, raw in enumerate(stream, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) < 8:
                raise TrialError("TUM row has fewer than eight fields")
            try:
                values = np.asarray([float(value) for value in fields[:8]], dtype=float)
            except ValueError as exc:
                raise TrialError("nonnumeric TUM row {}".format(line_number)) from exc
            if not np.all(np.isfinite(values)):
                raise TrialError("nonfinite TUM row {}".format(line_number))
            timestamp = float(values[0])
            if timestamp <= previous:
                raise TrialError("TUM timestamps are not strictly increasing")
            quaternion = values[4:8]
            norm = float(np.linalg.norm(quaternion))
            if not 0.999 <= norm <= 1.001:
                raise TrialError("invalid TUM quaternion norm")
            poses.append(Pose(timestamp, values[1:4].copy(), quaternion / norm))
            previous = timestamp
    if not poses:
        raise NoRowsError("TUM trajectory contains no poses")
    return poses


def associate_poses(
    reference: Sequence[Pose], estimate: Sequence[Pose], max_difference: float
) -> List[Tuple[Pose, Pose, float]]:
    if max_difference <= 0 or not math.isfinite(max_difference):
        raise TrialError("association tolerance must be finite and positive")
    reference_times = [pose.timestamp for pose in reference]
    candidates: List[Tuple[float, int, int]] = []
    for estimate_index, pose in enumerate(estimate):
        lower = bisect.bisect_left(reference_times, pose.timestamp - max_difference)
        upper = bisect.bisect_right(reference_times, pose.timestamp + max_difference)
        for reference_index in range(lower, upper):
            delta = abs(reference_times[reference_index] - pose.timestamp)
            if delta <= max_difference:
                candidates.append((delta, reference_index, estimate_index))
    candidates.sort()
    used_reference = set()
    used_estimate = set()
    selected: List[Tuple[int, int, float]] = []
    for delta, reference_index, estimate_index in candidates:
        if reference_index in used_reference or estimate_index in used_estimate:
            continue
        used_reference.add(reference_index)
        used_estimate.add(estimate_index)
        selected.append((reference_index, estimate_index, delta))
    selected.sort(key=lambda value: value[1])
    return [
        (reference[reference_index], estimate[estimate_index], delta)
        for reference_index, estimate_index, delta in selected
    ]


def _fit_se3(
    pairs: Sequence[Tuple[Pose, Pose, float]]
) -> Tuple[np.ndarray, np.ndarray]:
    if len(pairs) < 3:
        raise TrialError("SE(3) alignment requires at least three associations")
    reference = np.vstack([pair[0].position for pair in pairs])
    estimate = np.vstack([pair[1].position for pair in pairs])
    reference_mean = np.mean(reference, axis=0)
    estimate_mean = np.mean(estimate, axis=0)
    covariance = (estimate - estimate_mean).T @ (reference - reference_mean)
    u_matrix, _, vt_matrix = np.linalg.svd(covariance)
    correction = np.eye(3)
    if np.linalg.det(vt_matrix.T @ u_matrix.T) < 0:
        correction[2, 2] = -1.0
    rotation = vt_matrix.T @ correction @ u_matrix.T
    translation = reference_mean - rotation @ estimate_mean
    if not np.all(np.isfinite(rotation)) or not np.all(np.isfinite(translation)):
        raise TrialError("SE(3) alignment is nonfinite")
    return rotation, translation


def _aligned_translation_metrics(
    pairs: Sequence[Tuple[Pose, Pose, float]],
    alignment: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> Dict[str, Any]:
    if len(pairs) < 3:
        return {
            "status": "UNAVAILABLE",
            "reason": "fewer_than_three_associations",
            "samples": len(pairs),
        }
    try:
        rotation, translation = alignment or _fit_se3(pairs)
    except (TrialError, np.linalg.LinAlgError) as exc:
        return {"status": "UNAVAILABLE", "reason": str(exc), "samples": len(pairs)}
    errors = np.asarray(
        [
            np.linalg.norm(rotation @ estimate.position + translation - reference.position)
            for reference, estimate, _ in pairs
        ],
        dtype=float,
    )
    return {
        "status": "AVAILABLE",
        "samples": len(pairs),
        "rmse_m": float(math.sqrt(float(np.mean(errors * errors)))),
        "mean_m": float(np.mean(errors)),
        "median_m": float(np.median(errors)),
        "max_m": float(np.max(errors)),
        "alignment": {
            "rotation_row_major": [float(value) for value in rotation.reshape(-1)],
            "translation_xyz_m": [float(value) for value in translation],
            "scale": 1.0,
        },
    }


def _quat_to_rotation(quaternion_xyzw: np.ndarray) -> np.ndarray:
    x_value, y_value, z_value, w_value = quaternion_xyzw
    return np.asarray(
        [
            [
                1 - 2 * (y_value * y_value + z_value * z_value),
                2 * (x_value * y_value - z_value * w_value),
                2 * (x_value * z_value + y_value * w_value),
            ],
            [
                2 * (x_value * y_value + z_value * w_value),
                1 - 2 * (x_value * x_value + z_value * z_value),
                2 * (y_value * z_value - x_value * w_value),
            ],
            [
                2 * (x_value * z_value - y_value * w_value),
                2 * (y_value * z_value + x_value * w_value),
                1 - 2 * (x_value * x_value + y_value * y_value),
            ],
        ],
        dtype=float,
    )


def _nearest_pose(poses: Sequence[Pose], timestamp: float, tolerance: float) -> Pose:
    times = [pose.timestamp for pose in poses]
    index = bisect.bisect_left(times, timestamp)
    candidates = []
    if index < len(poses):
        candidates.append(poses[index])
    if index:
        candidates.append(poses[index - 1])
    if not candidates:
        raise TrialError("trajectory contains no anchor candidate")
    selected = min(candidates, key=lambda pose: abs(pose.timestamp - timestamp))
    if abs(selected.timestamp - timestamp) > tolerance:
        raise TrialError("no reference pose within anchor association tolerance")
    return selected


def _interpolate_pose(
    poses: Sequence[Pose], timestamp: float, maximum_bracket_seconds: float = 0.10
) -> Pose:
    """Interpolate a reference pose at a frozen anchor timestamp.

    KAIST ground-truth sampling around the outage endpoints is irregular and
    can be farther than evo's trajectory-association tolerance.  Cross-gap
    anchors therefore use a bounded bracketing interpolation, while all ATE
    segments retain the frozen 0.01 s association rule.
    """

    times = [pose.timestamp for pose in poses]
    right = bisect.bisect_left(times, timestamp)
    if right < len(poses) and poses[right].timestamp == timestamp:
        return poses[right]
    if right == 0 or right == len(poses):
        raise TrialError("reference anchor is not bracketed")
    before = poses[right - 1]
    after = poses[right]
    span = after.timestamp - before.timestamp
    if span <= 0 or span > maximum_bracket_seconds:
        raise TrialError("reference anchor bracket exceeds frozen 0.10 s limit")
    fraction = (timestamp - before.timestamp) / span
    position = before.position + fraction * (after.position - before.position)
    first = before.quaternion_xyzw
    second = after.quaternion_xyzw.copy()
    dot = float(np.dot(first, second))
    if dot < 0.0:
        second = -second
        dot = -dot
    dot = max(-1.0, min(1.0, dot))
    if dot > 0.9995:
        quaternion = first + fraction * (second - first)
        quaternion /= np.linalg.norm(quaternion)
    else:
        angle = math.acos(dot)
        sine = math.sin(angle)
        quaternion = (
            math.sin((1.0 - fraction) * angle) / sine * first
            + math.sin(fraction * angle) / sine * second
        )
        quaternion /= np.linalg.norm(quaternion)
    return Pose(timestamp, position, quaternion)


def _estimate_boundary_anchor(
    estimate: Sequence[Pose], target: float, side: str, limit: float
) -> Pose:
    epsilon = 1.0e-3
    if side == "pre":
        candidates = [pose for pose in estimate if pose.timestamp <= target + epsilon]
        if not candidates:
            raise TrialError("no pre-gap estimate anchor")
        selected = candidates[-1]
    elif side == "post":
        candidates = [pose for pose in estimate if pose.timestamp >= target - epsilon]
        if not candidates:
            raise TrialError("no post-gap estimate anchor")
        selected = candidates[0]
    else:
        raise TrialError("unknown gap anchor side")
    if abs(selected.timestamp - target) > limit:
        raise TrialError("estimate {} anchor is outside frozen search limit".format(side))
    return selected


def _rotation_angle_degrees(rotation: np.ndarray) -> float:
    cosine = max(-1.0, min(1.0, float((np.trace(rotation) - 1.0) / 2.0)))
    return math.degrees(math.acos(cosine))


def evaluate_rotation_gap(
    trajectory_path: Path, reference_path: Path
) -> Dict[str, Any]:
    estimate = read_tum(trajectory_path)
    reference = read_tum(reference_path)
    tolerance = float(ROTATION_GAP["association_max_difference_seconds"])
    associations = associate_poses(reference, estimate, tolerance)
    pre_boundary = float(ROTATION_GAP["last_pre_gap_estimator_timestamp_s"])
    post_boundary = float(ROTATION_GAP["first_post_gap_estimator_timestamp_s"])
    pre_pairs = [pair for pair in associations if pair[1].timestamp <= pre_boundary + 1.0e-3]
    post_pairs = [pair for pair in associations if pair[1].timestamp >= post_boundary - 1.0e-3]
    full_metric = _aligned_translation_metrics(associations)
    pre_metric = _aligned_translation_metrics(pre_pairs)
    post_metric = _aligned_translation_metrics(post_pairs)
    post_under_pre: Dict[str, Any]
    if pre_metric.get("status") == "AVAILABLE":
        rotation = np.asarray(
            pre_metric["alignment"]["rotation_row_major"], dtype=float
        ).reshape(3, 3)
        translation = np.asarray(
            pre_metric["alignment"]["translation_xyz_m"], dtype=float
        )
        post_under_pre = _aligned_translation_metrics(
            post_pairs, alignment=(rotation, translation)
        )
    else:
        post_under_pre = {
            "status": "UNAVAILABLE",
            "reason": "pre_gap_alignment_unavailable",
            "samples": len(post_pairs),
        }

    cross_gap: Dict[str, Any]
    try:
        limit = float(ROTATION_GAP["anchor_search_limit_seconds"])
        if not pre_pairs or not post_pairs:
            raise TrialError("no associated estimate/reference pair at a gap boundary")
        reference_pre, estimate_pre, _ = pre_pairs[-1]
        reference_post, estimate_post, _ = post_pairs[0]
        if abs(estimate_pre.timestamp - pre_boundary) > limit:
            raise TrialError("associated pre-gap anchor exceeds frozen 0.10 s limit")
        if abs(estimate_post.timestamp - post_boundary) > limit:
            raise TrialError("associated post-gap anchor exceeds frozen 0.10 s limit")
        estimate_pre_rotation = _quat_to_rotation(estimate_pre.quaternion_xyzw)
        estimate_post_rotation = _quat_to_rotation(estimate_post.quaternion_xyzw)
        reference_pre_rotation = _quat_to_rotation(reference_pre.quaternion_xyzw)
        reference_post_rotation = _quat_to_rotation(reference_post.quaternion_xyzw)
        estimate_relative_translation = estimate_pre_rotation.T @ (
            estimate_post.position - estimate_pre.position
        )
        reference_relative_translation = reference_pre_rotation.T @ (
            reference_post.position - reference_pre.position
        )
        estimate_relative_rotation = estimate_pre_rotation.T @ estimate_post_rotation
        reference_relative_rotation = reference_pre_rotation.T @ reference_post_rotation
        rotation_error = reference_relative_rotation.T @ estimate_relative_rotation
        cross_gap = {
            "status": "AVAILABLE",
            "estimate_pre_timestamp_s": estimate_pre.timestamp,
            "estimate_post_timestamp_s": estimate_post.timestamp,
            "reference_pre_timestamp_s": reference_pre.timestamp,
            "reference_post_timestamp_s": reference_post.timestamp,
            "estimate_gap_seconds": estimate_post.timestamp - estimate_pre.timestamp,
            "reference_anchor_policy": (
                "last/first one-to-one 0.01 s trajectory association on the "
                "respective side of the frozen outage boundaries; each estimate "
                "anchor must be within 0.10 s of its boundary"
            ),
            "estimate_displacement_m": float(
                np.linalg.norm(estimate_post.position - estimate_pre.position)
            ),
            "reference_displacement_m": float(
                np.linalg.norm(reference_post.position - reference_pre.position)
            ),
            "estimate_relative_translation_pre_body_xyz_m": [
                float(value) for value in estimate_relative_translation
            ],
            "reference_relative_translation_pre_body_xyz_m": [
                float(value) for value in reference_relative_translation
            ],
            "relative_translation_error_m": float(
                np.linalg.norm(
                    estimate_relative_translation - reference_relative_translation
                )
            ),
            "relative_rotation_error_deg": _rotation_angle_degrees(rotation_error),
        }
    except TrialError as exc:
        cross_gap = {"status": "UNAVAILABLE", "reason": str(exc)}

    maximum_association_delta = max(
        (pair[2] for pair in associations), default=None
    )
    return {
        "schema": GAP_SCHEMA,
        "sequence": "rotation/rotation.bag",
        "frozen_gap": dict(ROTATION_GAP),
        "association": {
            "policy": (
                "global minimum absolute timestamp difference, one-to-one, "
                "then estimator-time order"
            ),
            "max_difference_seconds": tolerance,
            "samples": len(associations),
            "maximum_observed_difference_seconds": maximum_association_delta,
        },
        "segments": {
            "full_independent_se3_alignment": full_metric,
            "pre_gap_independent_se3_alignment": pre_metric,
            "post_gap_independent_se3_alignment": post_metric,
            "post_gap_under_pre_gap_alignment": post_under_pre,
        },
        "cross_gap_relative_transform": cross_gap,
        "inputs": {
            "trajectory": file_identity(trajectory_path),
            "reference": file_identity(reference_path),
        },
    }


def metric_archive(path: Path) -> Dict[str, Any]:
    path = _regular_file(path, "evo metric archive")
    try:
        with zipfile.ZipFile(str(path), "r") as archive:
            stats = json.loads(archive.read("stats.json").decode("utf-8"))
            info = json.loads(archive.read("info.json").decode("utf-8"))
            errors = np.load(io.BytesIO(archive.read("error_array.npy")), allow_pickle=False)
    except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise TrialError("invalid evo archive: {}".format(path)) from exc
    numeric_values = [
        float(value) for value in stats.values() if isinstance(value, (int, float))
    ]
    if not all(math.isfinite(value) for value in numeric_values):
        raise TrialError("evo metric archive contains a nonfinite statistic")
    return {
        **file_identity(path),
        "samples": int(errors.size),
        "stats": stats,
        "title": info.get("title"),
        "label": info.get("label"),
    }


def _console_diagnostics(path: Path) -> Dict[str, Any]:
    patterns = {
        "recovery_lines": re.compile(
            r"recovery|reanchor|relocali[sz]|long.?gap|degraded", re.IGNORECASE
        ),
        "reset_lines": re.compile(r"\breset(?:s|ting)?\b", re.IGNORECASE),
        "nonfinite_lines": re.compile(
            r"non[-_ ]?finite|(?<![A-Za-z])nan(?![A-Za-z])|"
            r"(?<![A-Za-z])inf(?:inity)?(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "covariance_failure_lines": re.compile(
            r"covariance.*(?:fail|invalid|negative)|not positive definite",
            re.IGNORECASE,
        ),
    }
    counts = {name: 0 for name in patterns}
    samples: Dict[str, List[str]] = {name: [] for name in patterns}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for name, pattern in patterns.items():
            if pattern.search(raw):
                counts[name] += 1
                if len(samples[name]) < 20:
                    samples[name].append(raw[:1000])
    return {"counts": counts, "samples": samples}


def _start_capture_services(
    args: argparse.Namespace,
    run_dir: Path,
    environment: Mapping[str, str],
    tools: Mapping[str, Path],
) -> Tuple[ManagedProcess, ManagedProcess]:
    (run_dir / "geometry").mkdir()
    roscore = start_managed_process(
        [str(tools["roscore"]), "-p", str(args.ros_port)],
        run_dir / "diagnostics" / "roscore.log",
        environment,
    )
    recorder: Optional[ManagedProcess] = None
    try:
        _wait_for_port(args.ros_port, True)
        bag = run_dir / "geometry" / "feature_stream.bag"
        recorder_argv = [
            str(tools["rosbag"]),
            "record",
            "--buffsize=0",
            "--chunksize=768",
            "-O",
            str(bag),
            *GEOMETRY_TOPICS,
            "__name:=rotation_robustness_geometry_recorder",
        ]
        recorder = start_managed_process(
            recorder_argv,
            run_dir / "diagnostics" / "geometry_recorder.log",
            environment,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if recorder.process.poll() is not None:
                raise TrialError("geometry recorder exited during startup")
            probe = subprocess.run(
                [
                    str(tools["rosnode"]),
                    "info",
                    "/rotation_robustness_geometry_recorder",
                ],
                env=dict(environment),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                return roscore, recorder
            time.sleep(0.1)
        raise TrialError("geometry recorder did not become ready")
    except BaseException:
        if recorder is not None:
            finish_managed_process(recorder)
        finish_managed_process(roscore)
        raise


def _post_close_command(
    manifest: Dict[str, Any],
    name: str,
    argv: Sequence[str],
    log_path: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
) -> bool:
    record = run_command(argv, log_path, environment, timeout_seconds)
    manifest["commands"][name] = record
    if command_succeeded(record):
        return True
    manifest["stage_errors"].append(
        {"stage": name, "type": "CommandFailure", "message": record}
    )
    return False


def _tools() -> Dict[str, Path]:
    return {
        name: _which(name)
        for name in (
            "catkin_find",
            "evo_ape",
            "evo_rpe",
            "ldd",
            "rosbag",
            "roscore",
            "roslaunch",
            "rosnode",
        )
    }


def run_trial(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    if SAFE_ID_RE.fullmatch(args.candidate_id) is None:
        raise TrialError("candidate ID must match {}".format(SAFE_ID_RE.pattern))
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise TrialError("timeout must be finite and positive")
    run_dir = create_run_directory(args.output_root, args.run_id)
    started = time.monotonic()
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "status": "ACTIVE",
        "started_utc": utc_now(),
        "run_id": args.run_id,
        "candidate": {
            "id": args.candidate_id,
            "description": args.candidate_description,
            "attempt_index": args.attempt_index,
        },
        "sequence": args.sequence,
        "mode": args.mode,
        "run_directory": str(run_dir),
        "commands": {},
        "checks": {},
        "inputs_before": {},
        "inputs_after": {},
        "outputs": {},
        "metrics": {},
        "stage_errors": [],
        "completion": {
            "estimator_started": False,
            "estimator_process_group_closed": False,
            "reference_opened_after_close": False,
        },
    }
    final_status = "INFRASTRUCTURE_FAILED"
    capture_services: List[Tuple[str, ManagedProcess]] = []
    environment: Optional[Dict[str, str]] = None
    paths: Dict[str, Path] = {}
    estimator_attempted = False
    state_valid = False
    output_valid = False
    evaluation_valid = False
    seam_valid = False
    capture_valid = args.mode == "scored"
    launch_record: Optional[Dict[str, Any]] = None
    estimator_child_died = False
    source_before_snapshot: Optional[Dict[str, Any]] = None
    try:
        assert_port_available(args.ros_port)
        config = _regular_file(args.config, "candidate config")
        launch = _regular_file(args.launch, "candidate launch")
        binary = _regular_file(args.binary, "estimator binary")
        adapted_bag = _regular_file(args.adapted_bag, "adapted input bag")
        build_root = _directory(args.build_root, "build root")
        reference = args.reference_tum.expanduser().resolve(strict=False)
        for value, label in ((config, "candidate config"), (launch, "candidate launch")):
            _require_within(value, REPO_ROOT, label)
        _require_within(build_root, REPO_ROOT, "build root")
        if reference == adapted_bag:
            raise TrialError("reference and estimator input bag are the same path")
        paths.update(
            {
                "config": config,
                "launch": launch,
                "binary": binary,
                "adapted_bag": adapted_bag,
                "build_root": build_root,
                "reference": reference,
                "state": run_dir / "trajectory" / "state_estimate.txt",
                "deviation": run_dir / "trajectory" / "state_deviation.txt",
                "timing": run_dir / "diagnostics" / "timing_openvins.csv",
                "trajectory": run_dir / "trajectory" / "estimate_raw.tum",
            }
        )
        environment = minimal_environment(run_dir, args.ros_port)
        manifest["runtime_environment"] = environment
        manifest["ros_isolation"] = {
            "port": args.ros_port,
            "master_uri": environment["ROS_MASTER_URI"],
            "home": environment["HOME"],
            "ros_home": environment["ROS_HOME"],
            "ros_log_dir": environment["ROS_LOG_DIR"],
        }

        manifest["source_before"] = _git_source_state(run_dir, "before")
        snapshot_record, source_before_snapshot = _run_source_snapshot(
            run_dir, "before", environment, min(args.timeout_seconds, 300.0)
        )
        manifest["commands"]["source_snapshot_before"] = snapshot_record
        manifest["source_before"]["snapshot"] = file_identity(
            run_dir / "source" / "source_snapshot_before.json"
        )
        manifest["inputs_before"] = {
            "candidate_config": file_identity(config),
            "candidate_launch": file_identity(launch),
            "estimator_binary": file_identity(binary),
            "adapted_bag": file_identity(adapted_bag),
            "converter": file_identity(CONVERTER),
            "pairing_census": file_identity(PAIRING_CENSUS),
            "geometry_bundle": file_identity(GEOMETRY_BUNDLE),
        }
        manifest["build_provenance"] = validate_build_binding(
            build_root, binary, source_before_snapshot, args.build_manifest
        )
        tools = _tools()
        tools["time"] = _regular_file(Path("/usr/bin/time"), "GNU time")
        manifest["tools"] = {name: file_identity(path) for name, path in tools.items()}

        bag_info_record = run_command(
            [str(tools["rosbag"]), "info", "--yaml", str(adapted_bag)],
            run_dir / "diagnostics" / "adapted_bag_info.yaml",
            environment,
            min(args.timeout_seconds, 300.0),
        )
        manifest["commands"]["adapted_bag_info"] = bag_info_record
        if not command_succeeded(bag_info_record):
            raise TrialError("rosbag metadata inspection failed")
        bag_info_text = (run_dir / "diagnostics" / "adapted_bag_info.yaml").read_text(
            encoding="utf-8", errors="replace"
        )

        launch_arguments = [
            str(launch),
            "bag:=" + str(adapted_bag),
            "candidate_config:=" + str(config),
            "bag_start:=0.0",
            "path_state:=" + str(paths["state"]),
            "path_std:=" + str(paths["deviation"]),
            "path_time:=" + str(paths["timing"]),
            "verbosity:=INFO",
        ]
        estimator_argv = [
            str(tools["roslaunch"]), "-p", str(args.ros_port), *launch_arguments
        ]
        manifest["estimator_argv"] = estimator_argv
        dump_record = run_command(
            [str(tools["roslaunch"]), "--dump-params", *launch_arguments],
            run_dir / "diagnostics" / "resolved_ros_parameters.yaml",
            environment,
            min(args.timeout_seconds, 120.0),
        )
        manifest["commands"]["resolve_parameters"] = dump_record
        if not command_succeeded(dump_record):
            raise TrialError("ROS parameter resolution failed")
        resolved_text = (
            run_dir / "diagnostics" / "resolved_ros_parameters.yaml"
        ).read_text(encoding="utf-8", errors="replace")
        manifest["resolved_parameter_contract"] = validate_resolved_parameters(
            resolved_text, adapted_bag, config
        )
        manifest["ground_truth_firewall"] = validate_runtime_firewall(
            estimator_argv,
            resolved_text,
            config.read_text(encoding="utf-8", errors="strict"),
            bag_info_text,
            reference,
        )
        manifest["checks"]["ground_truth_absent_at_runtime"] = True
        manifest["checks"]["exact_header_parameter_enabled"] = True

        find_record = run_command(
            [
                str(tools["catkin_find"]),
                "--libexec",
                "ov_msckf",
                "ros1_serial_msckf",
            ],
            run_dir / "diagnostics" / "resolved_estimator_binary.txt",
            environment,
            min(args.timeout_seconds, 120.0),
        )
        manifest["commands"]["resolve_estimator_binary"] = find_record
        if not command_succeeded(find_record):
            raise TrialError("catkin_find failed")
        resolved_binary_lines = [
            line.strip()
            for line in (
                run_dir / "diagnostics" / "resolved_estimator_binary.txt"
            ).read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not resolved_binary_lines or Path(resolved_binary_lines[-1]).resolve(
            strict=False
        ) != binary:
            raise TrialError("catkin_find resolved a different estimator binary")
        manifest["checks"]["runtime_binary_matches_declared_binary"] = True

        ldd_record = run_command(
            [str(tools["ldd"]), str(binary)],
            run_dir / "diagnostics" / "resolved_dynamic_libraries.txt",
            environment,
            min(args.timeout_seconds, 120.0),
        )
        manifest["commands"]["resolved_dynamic_libraries"] = ldd_record
        if not command_succeeded(ldd_record):
            raise TrialError("dynamic library resolution failed")
        library_identities: Dict[str, Any] = {}
        for raw in (
            run_dir / "diagnostics" / "resolved_dynamic_libraries.txt"
        ).read_text(encoding="utf-8", errors="replace").splitlines():
            if "=>" not in raw:
                continue
            name, remainder = raw.split("=>", 1)
            target = remainder.strip().split(" (", 1)[0].strip()
            if target == "not found":
                raise TrialError("dynamic library was not found: {}".format(name.strip()))
            if target.startswith("/"):
                library_identities[name.strip()] = file_identity(Path(target))
        manifest["dynamic_libraries"] = library_identities

        if args.mode == "capture":
            roscore, recorder = _start_capture_services(
                args, run_dir, environment, tools
            )
            capture_services = [("roscore", roscore), ("geometry_recorder", recorder)]

        resource_path = run_dir / "diagnostics" / "resource_usage.txt"
        wrapped_argv = [
            str(tools["time"]),
            "--verbose",
            "--output=" + str(resource_path),
            "--",
            *estimator_argv,
        ]
        estimator_attempted = True
        manifest["completion"]["estimator_started"] = True
        launch_record = run_command(
            wrapped_argv,
            run_dir / "diagnostics" / "console.log",
            environment,
            args.timeout_seconds,
        )
        launch_record["estimator_argv"] = estimator_argv
        manifest["commands"]["estimator"] = launch_record
        if resource_path.is_file():
            manifest["resource_usage"] = file_identity(resource_path)

        # Close capture-side runtime processes before any ground-truth access.
        for name, managed in reversed(capture_services):
            manifest["commands"][name] = finish_managed_process(managed)
        capture_services = []
        manifest["completion"]["estimator_process_group_closed"] = not bool(
            launch_record.get("process_group_survived_cleanup")
        )
        manifest["completion"]["estimator_closed_utc"] = utc_now()
        if not manifest["completion"]["estimator_process_group_closed"]:
            raise TrialError("estimator process group survived cleanup")

        console_path = run_dir / "diagnostics" / "console.log"
        manifest["console_diagnostics"] = _console_diagnostics(console_path)
        child_outcome = parse_roslaunch_child_deaths(
            console_path.read_text(encoding="utf-8", errors="replace")
        )
        manifest["roslaunch_child_outcome"] = child_outcome
        estimator_child_died = bool(child_outcome["estimator_required_child_died"])
        manifest["completion"]["estimator_required_child_died"] = estimator_child_died
        manifest["completion"]["estimator_wrapper_exit_code"] = launch_record.get(
            "exit_code"
        )
        manifest["completion"]["estimator_completed"] = (
            command_succeeded(launch_record) and not estimator_child_died
        )
        try:
            summaries = parse_runtime_summaries(
                console_path.read_text(encoding="utf-8", errors="replace")
            )
            manifest["exact_header_runtime"] = summaries
            seam_valid = True
        except TrialError as exc:
            manifest["stage_errors"].append(
                {"stage": "exact_header_runtime", "type": type(exc).__name__, "message": str(exc)}
            )
            summaries = {}

        census_path = run_dir / "diagnostics" / "pairing_census.json"
        if _post_close_command(
            manifest,
            "pairing_census",
            [
                str(PYTHON),
                str(PAIRING_CENSUS),
                "--bag",
                str(adapted_bag),
                "--output",
                str(census_path),
            ],
            run_dir / "diagnostics" / "pairing_census.log",
            environment,
            min(args.timeout_seconds, 600.0),
        ):
            try:
                census = json.loads(census_path.read_text(encoding="utf-8"))
                census_binding = bind_pairing_census(summaries, census)
                manifest["pairing_census"] = {
                    "artifact": file_identity(census_path),
                    "binding": census_binding,
                }
                seam_valid = seam_valid and census_binding.get("status") == "AVAILABLE"
            except (TrialError, json.JSONDecodeError) as exc:
                seam_valid = False
                manifest["stage_errors"].append(
                    {"stage": "pairing_census_binding", "type": type(exc).__name__, "message": str(exc)}
                )
        else:
            seam_valid = False
        manifest["checks"]["exact_header_runtime_and_static_census_match"] = seam_valid

        # Ground truth is first resolved/opened here, after every runtime group closes.
        reference = _regular_file(reference, "reference trajectory")
        paths["reference"] = reference
        manifest["completion"]["reference_opened_after_close"] = True
        manifest["completion"]["reference_first_open_utc"] = utc_now()
        manifest["reference"] = file_identity(reference)
        manifest["checks"]["reference_opened_only_post_close"] = True

        try:
            manifest["outputs"]["state"] = validate_numeric_table(paths["state"], 8)
            state_valid = True
        except NoRowsError as exc:
            manifest["stage_errors"].append(
                {"stage": "state_validation", "type": "NoInitialization", "message": str(exc)}
            )
        except TrialError as exc:
            manifest["stage_errors"].append(
                {"stage": "state_validation", "type": type(exc).__name__, "message": str(exc)}
            )
        for name, path, columns, separator in (
            ("deviation", paths["deviation"], 2, None),
            ("timing", paths["timing"], 2, ","),
        ):
            try:
                manifest["outputs"][name] = validate_numeric_table(
                    path, columns, separator
                )
            except TrialError as exc:
                manifest["stage_errors"].append(
                    {"stage": name + "_validation", "type": type(exc).__name__, "message": str(exc)}
                )

        if state_valid and _post_close_command(
            manifest,
            "trajectory_conversion",
            [str(PYTHON), str(CONVERTER), str(paths["state"]), str(paths["trajectory"])],
            run_dir / "diagnostics" / "trajectory_conversion.log",
            environment,
            min(args.timeout_seconds, 300.0),
        ):
            try:
                manifest["outputs"]["trajectory"] = validate_numeric_table(
                    paths["trajectory"], 8
                )
                if all(
                    name in manifest["outputs"]
                    for name in ("state", "deviation", "timing", "trajectory")
                ):
                    manifest["output_consistency"] = validate_output_consistency(
                        paths["state"],
                        paths["deviation"],
                        paths["timing"],
                        paths["trajectory"],
                    )
                    output_valid = True
            except TrialError as exc:
                manifest["stage_errors"].append(
                    {"stage": "output_consistency", "type": type(exc).__name__, "message": str(exc)}
                )

        metric_successes: Dict[str, bool] = {}
        if paths["trajectory"].is_file():
            metric_specs = {
                "ape_translation": (tools["evo_ape"], "trans_part"),
                "rpe_translation_1m": (tools["evo_rpe"], "trans_part"),
                "rpe_rotation_1m_deg": (tools["evo_rpe"], "angle_deg"),
            }
            for name, (tool, relation) in metric_specs.items():
                archive = run_dir / "metrics" / (name + ".zip")
                argv = [
                    str(tool),
                    "tum",
                    str(reference),
                    str(paths["trajectory"]),
                    "--t_max_diff",
                    "0.01",
                    "--pose_relation",
                    relation,
                ]
                if name.startswith("rpe_"):
                    argv.extend(
                        [
                            "--delta",
                            "1.0",
                            "--delta_unit",
                            "m",
                            "--all_pairs",
                            "--pairs_from_reference",
                        ]
                    )
                argv.extend(["--align", "--save_results", str(archive), "--no_warnings"])
                succeeded = _post_close_command(
                    manifest,
                    name,
                    argv,
                    run_dir / "metrics" / (name + ".log"),
                    environment,
                    min(args.timeout_seconds, 300.0),
                )
                metric_successes[name] = succeeded
                if succeeded:
                    try:
                        manifest["metrics"][name] = metric_archive(archive)
                    except TrialError as exc:
                        metric_successes[name] = False
                        manifest["stage_errors"].append(
                            {"stage": name + "_archive", "type": type(exc).__name__, "message": str(exc)}
                        )
            if args.sequence == "rotation/rotation.bag":
                try:
                    gap = evaluate_rotation_gap(paths["trajectory"], reference)
                    gap_path = run_dir / "metrics" / "rotation_gap_metrics.json"
                    atomic_write_new_json(gap_path, gap)
                    manifest["metrics"]["rotation_gap"] = {
                        "artifact": file_identity(gap_path),
                        "value": gap,
                    }
                    metric_successes["rotation_gap"] = True
                except (TrialError, ValueError, np.linalg.LinAlgError) as exc:
                    metric_successes["rotation_gap"] = False
                    manifest["stage_errors"].append(
                        {"stage": "rotation_gap", "type": type(exc).__name__, "message": str(exc)}
                    )
            evaluation_valid = bool(metric_successes) and all(metric_successes.values())

        if args.mode == "capture":
            feature_bag = run_dir / "geometry" / "feature_stream.bag"
            active_bag = run_dir / "geometry" / "feature_stream.bag.active"
            if feature_bag.is_file() and feature_bag.stat().st_size:
                manifest["geometry_capture"] = {
                    "status": "CLOSED_BAG_AVAILABLE",
                    "feature_stream": file_identity(feature_bag),
                    "topics": list(GEOMETRY_TOPICS),
                }
                if paths["trajectory"].is_file():
                    atlas_dir = run_dir / "qualitative"
                    atlas_ok = _post_close_command(
                        manifest,
                        "geometry_atlas",
                        [
                            str(PYTHON),
                            str(GEOMETRY_BUNDLE),
                            "--feature-bag",
                            str(feature_bag),
                            "--capture-trajectory",
                            str(paths["trajectory"]),
                            "--ground-truth",
                            str(reference),
                            "--run-dir",
                            str(atlas_dir),
                            "--namespace",
                            "/kaist_vio_turnsafe_baseline",
                        ],
                        run_dir / "diagnostics" / "geometry_atlas.log",
                        environment,
                        min(args.timeout_seconds, 1800.0),
                    )
                    if atlas_ok and (atlas_dir / "geometry_manifest.json").is_file():
                        manifest["geometry_capture"]["atlas_manifest"] = file_identity(
                            atlas_dir / "geometry_manifest.json"
                        )
                        manifest["geometry_capture"]["atlas_checksums"] = file_identity(
                            atlas_dir / "SHA256SUMS"
                        )
                        capture_valid = True
            elif active_bag.is_file():
                manifest["geometry_capture"] = {
                    "status": "INCOMPLETE_ACTIVE_BAG_RETAINED",
                    "feature_stream_active": file_identity(active_bag),
                    "topics": list(GEOMETRY_TOPICS),
                }
            else:
                manifest["geometry_capture"] = {
                    "status": "NO_BAG_PRODUCED",
                    "topics": list(GEOMETRY_TOPICS),
                }

        # Bind immutable runtime inputs and source state again after all work.
        for name, path in (
            ("candidate_config", config),
            ("candidate_launch", launch),
            ("estimator_binary", binary),
            ("adapted_bag", adapted_bag),
        ):
            manifest["inputs_after"][name] = file_identity(path)
        manifest["checks"]["runtime_inputs_unchanged"] = all(
            manifest["inputs_before"][name] == manifest["inputs_after"][name]
            for name in manifest["inputs_after"]
        )
        manifest["source_after"] = _git_source_state(run_dir, "after")
        source_after_record, source_after_snapshot = _run_source_snapshot(
            run_dir, "after", environment, min(args.timeout_seconds, 300.0)
        )
        manifest["commands"]["source_snapshot_after"] = source_after_record
        manifest["source_after"]["snapshot"] = file_identity(
            run_dir / "source" / "source_snapshot_after.json"
        )
        manifest["checks"]["source_snapshot_unchanged_during_trial"] = (
            source_before_snapshot.get("aggregate_source_snapshot_sha256")
            == source_after_snapshot.get("aggregate_source_snapshot_sha256")
        )

        final_status = classify_trial_status(
            launch_record=launch_record,
            estimator_child_died=estimator_child_died,
            state_valid=state_valid,
            output_valid=output_valid,
            seam_valid=seam_valid,
            provenance_valid=(
                manifest["checks"]["runtime_inputs_unchanged"]
                and manifest["checks"]["source_snapshot_unchanged_during_trial"]
            ),
            evaluation_valid=evaluation_valid,
            capture_valid=capture_valid,
        )
    except KeyboardInterrupt as exc:
        final_status = "INTERRUPTED"
        manifest["stage_errors"].append(
            {"stage": "runner", "type": type(exc).__name__, "message": "keyboard interrupt"}
        )
    except BaseException as exc:
        if estimator_attempted and launch_record is not None and launch_record.get("timed_out"):
            final_status = "TIMED_OUT"
        manifest["stage_errors"].append(
            {"stage": "runner", "type": type(exc).__name__, "message": str(exc)}
        )
    finally:
        for name, managed in reversed(capture_services):
            try:
                manifest["commands"][name] = finish_managed_process(managed)
            except BaseException as exc:
                manifest["stage_errors"].append(
                    {"stage": name + "_cleanup", "type": type(exc).__name__, "message": str(exc)}
                )
        manifest["status"] = final_status
        manifest["finished_utc"] = utc_now()
        manifest["duration_seconds"] = time.monotonic() - started
        manifest["completion"]["negative_results_retained"] = final_status != "COMPLETED"
        manifest["publication"] = {
            "manifest": "manifest.json",
            "checksums": "SHA256SUMS",
            "manifest_published_atomically": True,
            "checksum_file_published_atomically_after_manifest": True,
            "overwrite_policy": "unique run directory plus exclusive publication",
        }
        manifest_path = run_dir / "manifest.json"
        atomic_write_new_json(manifest_path, manifest)
        write_sha256sums(run_dir)
    return manifest, run_dir


def _run_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "run", help="run one append-only scored or qualitative-capture trial"
    )
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--candidate-description", default="")
    parser.add_argument("--attempt-index", type=int, default=1)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--sequence", required=True, choices=SEQUENCES)
    parser.add_argument("--mode", choices=("scored", "capture"), default="scored")
    parser.add_argument("--adapted-bag", required=True, type=Path)
    parser.add_argument("--reference-tum", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--launch", type=Path, default=DEFAULT_LAUNCH)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BINARY)
    parser.add_argument("--build-root", type=Path, default=DEFAULT_BUILD_ROOT)
    parser.add_argument("--build-manifest", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--ros-port", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)


def _gap_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "gap-evaluate", help="evaluate an already-closed rotation trajectory"
    )
    parser.add_argument("--trajectory", required=True, type=Path)
    parser.add_argument("--reference-tum", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    _run_parser(subparsers)
    _gap_parser(subparsers)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.operation == "gap-evaluate":
        try:
            trajectory = _regular_file(args.trajectory, "trajectory")
            reference = _regular_file(args.reference_tum, "reference trajectory")
            result = evaluate_rotation_gap(trajectory, reference)
            atomic_write_new_json(args.output.expanduser().resolve(strict=False), result)
        except (OSError, TrialError, ValueError, np.linalg.LinAlgError) as exc:
            print("ROTATION_GAP_EVALUATION_ERROR: {}".format(exc), file=sys.stderr)
            return 2
        print("wrote {}".format(args.output))
        return 0
    try:
        result, run_dir = run_trial(args)
    except (OSError, TrialError, ValueError, json.JSONDecodeError) as exc:
        print("ROTATION_ROBUSTNESS_TRIAL_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("{} {}: {}".format(result["status"], result["sequence"], run_dir))
    return 0 if result["status"] == "COMPLETED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
