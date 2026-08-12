#!/usr/bin/python3
"""Run one fixed KAIST-VIO preparation or baseline-evaluation job.

This is an orchestration and evidence tool, not an estimator configuration
surface.  It accepts exactly one of the eleven public KAIST-VIO sequences,
uses :mod:`kaist_vio_adapter` to audit/adapt it, gives only the adapted bag to
the fixed serial launch file, and exposes the public reference trajectory only
after roslaunch has finished.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import zipfile


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
DATA_ROOT = Path("/home/moksh/datasets/KAIST_VIO")
ARTIFACT_ROOT = REPO_ROOT / "artifacts" / "turnsafe"
ADAPTER_PATH = SCRIPT_DIR / "kaist_vio_adapter.py"
CONVERTER_PATH = REPO_ROOT / "scripts" / "cp0" / "openvins_to_tum.py"
FIXED_LAUNCH = REPO_ROOT / "project" / "kaist_vio_serial.launch"
FIXED_CONFIG = (
    REPO_ROOT / "config" / "kaist_vio_turnsafe_baseline" / "estimator_config.yaml"
)
PYTHON = Path("/usr/bin/python3")

# This order is the frozen Session-0.5 smoke/campaign order, not a tuning order.
SEQUENCE_ORDER: Tuple[str, ...] = (
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

SCHEMA = "turnsafe.kaist_vio_campaign.sequence.v1"
SERIAL_KAIST_SUMMARY_PREFIX = "[SERIAL-KAIST]: exact_header_pairs="
SERIAL_KAIST_SUMMARY_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]: exact_header_pairs=(?P<exact_header_pairs>[0-9]+) "
    r"camera0_without_match=(?P<camera0_without_match>[0-9]+) "
    r"camera1_without_match=(?P<camera1_without_match>[0-9]+) "
    r"record_delta_ge_20ms=(?P<record_delta_ge_20ms>[0-9]+) "
    r"maximum_record_delta_ns=(?P<maximum_record_delta_ns>[0-9]+)"
)
CAMERA_ENQUEUE_SUMMARY_PREFIX = "[SERIAL-KAIST]: queued_pairs="
CAMERA_ENQUEUE_SUMMARY_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]: queued_pairs=(?P<queued_pairs>[0-9]+) "
    r"processed_pairs=(?P<processed_pairs>[0-9]+) "
    r"frequency_thinned_pairs=(?P<frequency_thinned_pairs>[0-9]+) "
    r"cam0_decode_failures=(?P<cam0_decode_failures>[0-9]+) "
    r"cam1_decode_failures=(?P<cam1_decode_failures>[0-9]+) "
    r"pending_pairs=(?P<pending_pairs>[0-9]+)"
)
ANSI_CONTROL_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
FORBIDDEN_COMPONENTS = ("holdout", "private")
FORBIDDEN_RUNTIME_TEXT = (
    "/pose_transformed",
    "path_gt",
    "ground_truth",
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


class CampaignError(RuntimeError):
    """A visible, fail-closed campaign error."""


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(path),
        "executable": bool(stat.st_mode & 0o111),
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _reject_forbidden_path(path: Path, label: str) -> None:
    for candidate in (path, path.resolve(strict=False)):
        lowered = [component.lower() for component in candidate.parts]
        for component in lowered:
            if any(word in component for word in FORBIDDEN_COMPONENTS):
                raise CampaignError(
                    "{} contains a forbidden PRIVATE/HOLDOUT component: {}".format(
                        label, path
                    )
                )
        for index in range(len(lowered) - 1):
            if lowered[index : index + 2] == ["scripts", "cp2"]:
                raise CampaignError("{} enters protected scripts/cp2: {}".format(label, path))


def _regular_file(path: Path, label: str, executable: bool = False) -> Path:
    _reject_forbidden_path(path, label)
    resolved = path.resolve(strict=False)
    if not resolved.is_file():
        raise CampaignError("{} is not a regular file: {}".format(label, resolved))
    if executable and not os.access(str(resolved), os.X_OK):
        raise CampaignError("{} is not executable: {}".format(label, resolved))
    return resolved


def _command_path(name: str) -> Path:
    resolved = shutil.which(name)
    if resolved is None:
        raise CampaignError("required command is unavailable: {}".format(name))
    return _regular_file(Path(resolved), name, executable=True)


def _reference_for_sequence(sequence: str) -> Path:
    return REPO_ROOT / "ov_data" / "kaist_vio" / (Path(sequence).stem + ".txt")


def _validate_paths(args: argparse.Namespace) -> Dict[str, Path]:
    source = _regular_file(args.source_bag, "source bag")
    adapted = args.adapted_bag.resolve(strict=False)
    _reject_forbidden_path(adapted, "adapted bag")
    config = _regular_file(args.config, "configuration")
    launch = _regular_file(args.launch, "launch file")
    binary = _regular_file(args.binary, "estimator binary", executable=True)
    reference = _regular_file(args.reference_tum, "reference trajectory")
    output = args.output_dir.resolve(strict=False)
    _reject_forbidden_path(output, "output directory")

    data_root = DATA_ROOT.resolve(strict=False)
    if not _is_within(source, data_root) or not _is_within(adapted, data_root):
        raise CampaignError("source and adapted bags must remain below {}".format(data_root))
    if source == adapted:
        raise CampaignError("source and adapted bag paths must differ")
    if tuple(source.parts[-2:]) != tuple(Path(args.sequence).parts):
        raise CampaignError(
            "source bag suffix does not match sequence {}: {}".format(
                args.sequence, source
            )
        )
    if tuple(adapted.parts[-2:]) != tuple(Path(args.sequence).parts):
        raise CampaignError(
            "adapted bag suffix does not match sequence {}: {}".format(
                args.sequence, adapted
            )
        )

    if launch != FIXED_LAUNCH.resolve():
        raise CampaignError("launch path is not the fixed KAIST launch: {}".format(launch))
    if config != FIXED_CONFIG.resolve():
        raise CampaignError("config path is not the fixed KAIST configuration: {}".format(config))
    expected_reference = _reference_for_sequence(args.sequence).resolve()
    if reference != expected_reference:
        raise CampaignError(
            "reference path does not match sequence; expected {}".format(
                expected_reference
            )
        )
    if not _is_within(binary, REPO_ROOT.resolve()):
        raise CampaignError("estimator binary must be a build input below the worktree")

    allowed_outputs = (data_root, ARTIFACT_ROOT.resolve(strict=False))
    if not any(_is_within(output, root) for root in allowed_outputs):
        raise CampaignError(
            "output directory must be below {} or {}".format(*allowed_outputs)
        )
    if output.exists():
        raise CampaignError("refusing to overwrite output directory: {}".format(output))
    if adapted.exists() and not adapted.is_file():
        raise CampaignError("adapted path exists but is not a regular file: {}".format(adapted))

    return {
        "source_bag": source,
        "adapted_bag": adapted,
        "config": config,
        "launch": launch,
        "binary": binary,
        "reference_tum": reference,
        "output_dir": output,
    }


def _minimal_environment(output_dir: Path, ros_port: Optional[int]) -> Dict[str, str]:
    allowed = {
        "CMAKE_PREFIX_PATH",
        "HOME",
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
    environment["ROS_HOME"] = str(output_dir / "ros-home")
    environment["ROS_LOG_DIR"] = str(output_dir / "ros-logs")
    environment["ROS_HOSTNAME"] = "127.0.0.1"
    environment["ROS_IP"] = "127.0.0.1"
    if ros_port is not None:
        environment["ROS_MASTER_URI"] = "http://127.0.0.1:{}".format(ros_port)
    return dict(sorted(environment.items()))


def _assert_port_available(port: int) -> None:
    if port < 1024 or port > 65535:
        raise CampaignError("ROS port must be in [1024, 65535]")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        probe.bind(("127.0.0.1", port))
    except OSError as exc:
        raise CampaignError("ROS port {} is unavailable: {}".format(port, exc)) from exc
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


def _clean_process_group(process: subprocess.Popen) -> Tuple[List[str], bool]:
    signals_sent: List[str] = []
    for stop_signal, grace in (
        (signal.SIGINT, 5.0),
        (signal.SIGTERM, 3.0),
        (signal.SIGKILL, 2.0),
    ):
        if not _process_group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, stop_signal)
            signals_sent.append(signal.Signals(stop_signal).name)
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
    """Run one argv in a new process group and retain a complete combined log."""

    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise CampaignError("subprocess timeout must be finite and positive")
    started_utc = utc_now()
    started = time.monotonic()
    timed_out = False
    interrupted = False
    error: Optional[str] = None
    exit_code: Optional[int] = None
    signals_sent: List[str] = []
    surviving_group = False
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
                    extra_signals, surviving_group = _clean_process_group(process)
                    signals_sent.extend(extra_signals)
        except OSError as exc:
            error = str(exc)
            stream.write("failed to execute command: {}\n".format(error))
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "argv": list(argv),
        "shell": shlex.join(list(argv)),
        "cwd": str(REPO_ROOT),
        "environment": dict(environment),
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
        "log": str(log_path),
        "log_sha256": sha256_file(log_path),
        "log_size_bytes": log_path.stat().st_size,
    }


def _command_succeeded(record: Mapping[str, Any]) -> bool:
    return (
        record.get("exit_code") == 0
        and not record.get("timed_out", False)
        and not record.get("process_group_survived_cleanup", False)
        and not record.get("signals_sent", [])
        and record.get("error") is None
    )


def _validate_numeric_table(
    path: Path, separator: Optional[str], minimum_columns: int
) -> Dict[str, Any]:
    if not path.is_file():
        raise CampaignError("required numeric output is missing: {}".format(path))
    previous = -math.inf
    rows = 0
    columns: Optional[int] = None
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split(separator) if separator is not None else stripped.split()
        fields = [field.strip() for field in fields]
        if len(fields) < minimum_columns:
            raise CampaignError(
                "{}:{} has {} columns, expected at least {}".format(
                    path, line_number, len(fields), minimum_columns
                )
            )
        try:
            values = [float(field) for field in fields]
        except ValueError as exc:
            raise CampaignError("{}:{} contains a non-numeric value".format(path, line_number)) from exc
        if not all(math.isfinite(value) for value in values):
            raise CampaignError("{}:{} contains a non-finite value".format(path, line_number))
        if values[0] <= previous:
            raise CampaignError("{}:{} timestamps are not strictly increasing".format(path, line_number))
        if columns is None:
            columns = len(fields)
        elif len(fields) != columns:
            raise CampaignError("{}:{} has an inconsistent column count".format(path, line_number))
        previous = values[0]
        rows += 1
    if rows == 0:
        raise CampaignError("numeric output contains no rows: {}".format(path))
    return {
        "path": str(path),
        "rows": rows,
        "columns": columns,
        "timestamps_strictly_increasing": True,
        "all_values_finite": True,
        "first_timestamp": _first_timestamp(path, separator),
        "last_timestamp": previous,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }


def _first_timestamp(path: Path, separator: Optional[str]) -> float:
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split(separator) if separator is not None else stripped.split()
            return float(fields[0].strip())
    raise CampaignError("numeric output contains no rows: {}".format(path))


def _timestamps(path: Path, separator: Optional[str]) -> List[float]:
    result: List[float] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            fields = stripped.split(separator) if separator is not None else stripped.split()
            result.append(float(fields[0].strip()))
    return result


def _validate_output_consistency(
    state: Path, deviation: Path, timing: Path, trajectory: Path
) -> Dict[str, Any]:
    state_times = _timestamps(state, None)
    deviation_times = _timestamps(deviation, None)
    timing_times = _timestamps(timing, ",")
    trajectory_times = _timestamps(trajectory, None)
    if state_times != deviation_times:
        raise CampaignError("state and deviation timestamp sequences differ")
    if state_times != trajectory_times:
        raise CampaignError("state and TUM trajectory timestamp sequences differ")
    if len(timing_times) != len(state_times):
        raise CampaignError(
            "timing row count {} differs from callback/state row count {}".format(
                len(timing_times), len(state_times)
            )
        )
    differences = [
        abs(timing_stamp - state_stamp)
        for timing_stamp, state_stamp in zip(timing_times, state_times)
    ]
    maximum_difference = max(differences)
    if maximum_difference > 1e-5:
        raise CampaignError(
            "timing/state timestamp difference {:.12g} exceeds 1e-5 seconds".format(
                maximum_difference
            )
        )
    return {
        "state_deviation_timestamp_sequences_equal": True,
        "state_trajectory_timestamp_sequences_equal": True,
        "callback_state_rows": len(state_times),
        "timing_rows": len(timing_times),
        "callback_timing_row_counts_equal": True,
        "timing_state_timestamp_tolerance_seconds": 1e-5,
        "timing_state_maximum_absolute_difference_seconds": maximum_difference,
        "timing_state_timestamps_within_tolerance": True,
    }


def _npy_element_count(data: bytes) -> int:
    stream = memoryview(data)
    if bytes(stream[:6]) != b"\x93NUMPY":
        raise CampaignError("metric archive contains invalid NumPy data")
    major, minor = struct.unpack("BB", stream[6:8])
    if (major, minor) == (1, 0):
        header_length = struct.unpack("<H", stream[8:10])[0]
        header_start = 10
    elif major in (2, 3):
        header_length = struct.unpack("<I", stream[8:12])[0]
        header_start = 12
    else:
        raise CampaignError("metric archive uses unsupported NumPy version")
    header = ast.literal_eval(
        bytes(stream[header_start : header_start + header_length])
        .decode("latin1")
        .strip()
    )
    shape = header.get("shape")
    if not isinstance(shape, tuple):
        raise CampaignError("metric error array has no shape")
    count = 1
    for dimension in shape:
        count *= int(dimension)
    return count


def _metric_archive(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise CampaignError("metric archive is missing: {}".format(path))
    try:
        with zipfile.ZipFile(str(path), "r") as archive:
            stats = json.loads(archive.read("stats.json").decode("utf-8"))
            info = json.loads(archive.read("info.json").decode("utf-8"))
            samples = _npy_element_count(archive.read("error_array.npy"))
    except (KeyError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise CampaignError("invalid evo result archive {}: {}".format(path, exc)) from exc
    for key, value in stats.items():
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise CampaignError("metric {} is non-finite in {}".format(key, path))
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "samples": samples,
        "stats": stats,
        "title": info.get("title"),
        "label": info.get("label"),
    }


def _console_diagnostics(path: Path) -> Dict[str, Any]:
    patterns = {
        "reset_lines": re.compile(r"\breset(?:s|ting)?\b", re.IGNORECASE),
        "nonfinite_lines": re.compile(
            r"non[-_ ]?finite|(?<![A-Za-z])nan(?![A-Za-z])|(?<![A-Za-z])inf(?:inity)?(?![A-Za-z])",
            re.IGNORECASE,
        ),
        "unpaired_warning_lines": re.compile(
            r"unpaired|not[ -]paired|unable to find stereo pair|stereo.*(?:skew|sync|drop)",
            re.IGNORECASE,
        ),
        "dropped_message_lines": re.compile(r"\bdrop(?:ped|ping)?\b", re.IGNORECASE),
    }
    counts = dict((name, 0) for name in patterns)
    samples: Dict[str, List[str]] = dict((name, []) for name in patterns)
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for name, pattern in patterns.items():
            if pattern.search(raw):
                counts[name] += 1
                if len(samples[name]) < 10:
                    samples[name].append(raw[:1000])
    return {"counts": counts, "samples": samples}


def _nonnegative_audit_integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CampaignError(
            "adapted audit {} is not a nonnegative integer: {!r}".format(
                label, value
            )
        )
    return value


def _bind_exact_header_runtime_summary(
    path: Path, adapted_audit: Mapping[str, Any]
) -> Dict[str, Any]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = [line for line in lines if SERIAL_KAIST_SUMMARY_PREFIX in line]
    if len(candidates) != 1:
        raise CampaignError(
            "expected exactly one SERIAL-KAIST exact-header summary line; found {}".format(
                len(candidates)
            )
        )
    line = ANSI_CONTROL_PATTERN.sub("", candidates[0])
    match = SERIAL_KAIST_SUMMARY_PATTERN.fullmatch(line)
    if match is None:
        raise CampaignError("malformed SERIAL-KAIST exact-header summary: {}".format(line))
    observed = dict((name, int(value)) for name, value in match.groupdict().items())

    stereo = adapted_audit.get("stereo")
    if not isinstance(stereo, Mapping):
        raise CampaignError("adapted audit has no stereo mapping")
    skew = stereo.get("matched_pair_record_time_absolute_skew")
    if not isinstance(skew, Mapping):
        raise CampaignError("adapted audit has no matched-pair record-skew mapping")
    threshold_ns = _nonnegative_audit_integer(
        stereo.get("record_skew_threshold_ns"), "stereo.record_skew_threshold_ns"
    )
    if threshold_ns != 20_000_000:
        raise CampaignError(
            "adapted audit record-skew threshold is {}, not 20 ms".format(
                threshold_ns
            )
        )
    expected = {
        "exact_header_pairs": _nonnegative_audit_integer(
            stereo.get("exact_header_pair_count"),
            "stereo.exact_header_pair_count",
        ),
        "camera0_without_match": _nonnegative_audit_integer(
            stereo.get("camera0_unmatched_count"),
            "stereo.camera0_unmatched_count",
        ),
        "camera1_without_match": _nonnegative_audit_integer(
            stereo.get("camera1_unmatched_count"),
            "stereo.camera1_unmatched_count",
        ),
        "record_delta_ge_20ms": _nonnegative_audit_integer(
            stereo.get("matched_pair_record_skew_at_or_above_threshold_count"),
            "stereo.matched_pair_record_skew_at_or_above_threshold_count",
        ),
        "maximum_record_delta_ns": _nonnegative_audit_integer(
            skew.get("max_ns"),
            "stereo.matched_pair_record_time_absolute_skew.max_ns",
        ),
    }
    mismatches = [
        "{}: runtime={} audit={}".format(name, observed[name], expected[name])
        for name in expected
        if observed[name] != expected[name]
    ]
    if mismatches:
        raise CampaignError(
            "SERIAL-KAIST exact-header summary disagrees with adapted audit: {}".format(
                "; ".join(mismatches)
            )
        )
    return {
        "line": line,
        "observed": observed,
        "expected_from_adapted_audit": expected,
        "adapted_audit_record_skew_threshold_ns": threshold_ns,
        "matches_adapted_audit": True,
    }


def _bind_camera_enqueue_runtime_summary(
    path: Path, exact_header_pairs: int
) -> Dict[str, Any]:
    if isinstance(exact_header_pairs, bool) or not isinstance(
        exact_header_pairs, int
    ) or exact_header_pairs < 0:
        raise CampaignError(
            "exact-header runtime pair count is not a nonnegative integer: {!r}".format(
                exact_header_pairs
            )
        )
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    candidates = [line for line in lines if CAMERA_ENQUEUE_SUMMARY_PREFIX in line]
    if len(candidates) != 1:
        raise CampaignError(
            "expected exactly one SERIAL-KAIST camera-enqueue summary line; found {}".format(
                len(candidates)
            )
        )
    line = ANSI_CONTROL_PATTERN.sub("", candidates[0])
    match = CAMERA_ENQUEUE_SUMMARY_PATTERN.fullmatch(line)
    if match is None:
        raise CampaignError("malformed SERIAL-KAIST camera-enqueue summary: {}".format(line))
    observed = dict((name, int(value)) for name, value in match.groupdict().items())
    accounted_pairs = (
        observed["queued_pairs"] + observed["frequency_thinned_pairs"]
    )
    if accounted_pairs != exact_header_pairs:
        raise CampaignError(
            "SERIAL-KAIST camera enqueue accounting mismatch: queued {} + frequency-thinned {} != exact-header pairs {}".format(
                observed["queued_pairs"],
                observed["frequency_thinned_pairs"],
                exact_header_pairs,
            )
        )
    if observed["cam0_decode_failures"] != 0 or observed[
        "cam1_decode_failures"
    ] != 0:
        raise CampaignError(
            "SERIAL-KAIST camera decode failure: cam0={} cam1={}".format(
                observed["cam0_decode_failures"],
                observed["cam1_decode_failures"],
            )
        )
    if observed["processed_pairs"] != observed["queued_pairs"]:
        raise CampaignError(
            "SERIAL-KAIST camera processing mismatch: processed {} != queued {}".format(
                observed["processed_pairs"], observed["queued_pairs"]
            )
        )
    if observed["pending_pairs"] != 0:
        raise CampaignError(
            "SERIAL-KAIST camera queue did not drain: pending {}".format(
                observed["pending_pairs"]
            )
        )
    return {
        "line": line,
        "observed": observed,
        "exact_header_pairs": exact_header_pairs,
        "accounted_pairs": accounted_pairs,
        "pair_accounting_complete": True,
        "decode_failures_zero": True,
        "processed_pairs_match_queued": True,
        "queue_drained": True,
        "frequency_thinning_policy": "existing_frozen_baseline_camera_frequency_policy",
    }


def _atomic_write_new_json(path: Path, value: Mapping[str, Any]) -> None:
    if path.exists():
        raise CampaignError("refusing to overwrite JSON result: {}".format(path))
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="." + path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, str(path))
        temporary_name = None
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _adapter_call(
    operation: str, source: Path, adapted: Path
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    # Import only after all paths pass the holdout/protected-path checks.
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import kaist_vio_adapter as adapter
    finally:
        del sys.path[0]

    started = time.monotonic()
    started_utc = utc_now()
    if operation == "adapt":
        report = adapter.adapt_bag(source, adapted)
        source_audit = report["source_audit"]
        adapted_audit = report["adapted_audit"]
    else:
        source_audit = adapter.audit_bag(source, "source")
        adapted_audit = adapter.audit_bag(adapted, "adapted")
        preservation = adapter._assert_adaptation_preservation(
            source_audit, adapted_audit
        )
        report = {
            "source_audit": source_audit,
            "adapted_audit": adapted_audit,
            "preservation": preservation,
            "ground_truth_policy": "source_required_and_audited_output_omitted",
        }
    record = {
        "call": "kaist_vio_adapter.{}_bag".format(operation),
        "arguments": [str(source), str(adapted)],
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "ground_truth_policy": report.get("ground_truth_policy"),
        "preservation": report.get("preservation"),
    }
    return record, source_audit, adapted_audit


def _write_failure_result(
    result_path: Path, manifest: Dict[str, Any], exc: BaseException
) -> None:
    manifest["finished_utc"] = utc_now()
    manifest["status"] = "FAILED"
    manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
    try:
        _atomic_write_new_json(result_path, manifest)
    except Exception:
        pass


def run_campaign(args: argparse.Namespace) -> Dict[str, Any]:
    campaign_started = time.monotonic()
    paths = _validate_paths(args)
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise CampaignError("--timeout-seconds must be finite and positive")
    if not args.prepare_only:
        if args.ros_port is None:
            raise CampaignError("--ros-port is required unless --prepare-only is used")
        _assert_port_available(args.ros_port)

    output = paths["output_dir"]
    output.mkdir(parents=True, exist_ok=False)
    (output / "ros-home").mkdir()
    (output / "ros-logs").mkdir()
    result_path = output / "sequence_result.json"
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "sequence": args.sequence,
        "campaign_index": SEQUENCE_ORDER.index(args.sequence),
        "prepare_only": bool(args.prepare_only),
        "started_utc": utc_now(),
        "status": "RUNNING",
        "commands": {},
        "checks": {},
        "completion": {
            "estimator_started": False,
            "estimator_completed": False,
            "output_validation_passed": False,
            "evaluation_completed": False,
            "timed_out": False,
            "reset_detected": False,
            "nonfinite_detected": False,
            "unpaired_warning_count": 0,
            "dropped_message_count": 0,
        },
    }

    try:
        rosbag = _command_path("rosbag")
        tool_paths = {"rosbag": rosbag}
        if not args.prepare_only:
            tool_paths.update(
                {
                    "catkin_find": _command_path("catkin_find"),
                    "roslaunch": _command_path("roslaunch"),
                    "evo_ape": _command_path("evo_ape"),
                    "evo_rpe": _command_path("evo_rpe"),
                }
            )
        environment = _minimal_environment(output, args.ros_port)
        manifest["runtime"] = {
            "ros_port": args.ros_port,
            "timeout_seconds": args.timeout_seconds,
            "environment": environment,
            "ground_truth_runtime_policy": "reference_used_only_after_roslaunch_finished",
        }
        manifest["diagnostics"] = {
            "yaw": {
                "status": "NOT_AVAILABLE",
                "reason": "no frozen deterministic yaw-diagnostic derivation is defined for Session 0.5",
            },
            "tilt": {
                "status": "NOT_AVAILABLE",
                "reason": "no frozen deterministic tilt-diagnostic derivation is defined for Session 0.5",
            },
        }

        fixed_inputs = {
            "source_bag": paths["source_bag"],
            "config": paths["config"],
            "kalibr_imu_chain": paths["config"].parent / "kalibr_imu_chain.yaml",
            "kalibr_imucam_chain": paths["config"].parent / "kalibr_imucam_chain.yaml",
            "launch": paths["launch"],
            "estimator_binary": paths["binary"],
            "reference_tum": paths["reference_tum"],
            "adapter": ADAPTER_PATH,
            "trajectory_converter": CONVERTER_PATH,
            "python": PYTHON,
        }
        for name, path in list(fixed_inputs.items()):
            fixed_inputs[name] = _regular_file(path, name, name in ("estimator_binary", "python"))
        fixed_inputs.update(tool_paths)
        manifest["inputs_before"] = {
            name: file_identity(path) for name, path in sorted(fixed_inputs.items())
        }
        paths["adapted_bag"].parent.mkdir(parents=True, exist_ok=True)
        operation = "audit" if paths["adapted_bag"].exists() else "adapt"
        adapter_record, source_audit, adapted_audit = _adapter_call(
            operation, paths["source_bag"], paths["adapted_bag"]
        )
        manifest["adapter"] = adapter_record
        manifest["source_audit"] = source_audit
        manifest["adapted_audit"] = adapted_audit
        manifest["checks"]["adapted_bag_has_no_ground_truth"] = (
            "/pose_transformed" not in adapted_audit.get("streams", {})
        )
        if not manifest["checks"]["adapted_bag_has_no_ground_truth"]:
            raise CampaignError("adapted estimator-input bag contains ground truth")

        paths["adapted_bag"] = _regular_file(paths["adapted_bag"], "adapted bag")
        manifest["adapted_bag"] = file_identity(paths["adapted_bag"])
        info_timeout = min(args.timeout_seconds, 300.0)
        for role in ("source", "adapted"):
            bag_path = paths[role + "_bag"]
            record = run_command(
                [str(rosbag), "info", "--yaml", str(bag_path)],
                output / (role + "_rosbag_info.yaml"),
                environment,
                info_timeout,
            )
            manifest["commands"][role + "_rosbag_info"] = record
            if not _command_succeeded(record):
                raise CampaignError("rosbag info failed for {} bag".format(role))

        if args.prepare_only:
            manifest["inputs_after"] = {
                name: file_identity(path) for name, path in sorted(fixed_inputs.items())
            }
            manifest["inputs_after"]["adapted_bag"] = file_identity(paths["adapted_bag"])
            manifest["checks"]["runtime_ground_truth_excluded"] = True
            manifest["status"] = "PREPARED"
            manifest["finished_utc"] = utc_now()
            manifest["runtime"]["total_duration_seconds"] = (
                time.monotonic() - campaign_started
            )
            _atomic_write_new_json(result_path, manifest)
            return manifest

        state = output / "state_estimate.txt"
        deviation = output / "state_deviation.txt"
        timing = output / "timing_openvins.csv"
        trajectory = output / "trajectory_tum.txt"
        launch_arguments = [
            str(paths["launch"]),
            "bag:=" + str(paths["adapted_bag"]),
            "bag_start:=0.0",
            "path_state:=" + str(state),
            "path_std:=" + str(deviation),
            "path_time:=" + str(timing),
            "verbosity:=INFO",
        ]
        joined_runtime = "\n".join(launch_arguments).lower()
        if str(paths["reference_tum"]) in joined_runtime or any(
            token in joined_runtime for token in FORBIDDEN_RUNTIME_TEXT
        ):
            raise CampaignError("ground truth leaked into estimator launch arguments")

        preflight_timeout = min(args.timeout_seconds, 120.0)
        dump_record = run_command(
            [str(tool_paths["roslaunch"]), "--dump-params"] + launch_arguments,
            output / "resolved_ros_parameters.yaml",
            environment,
            preflight_timeout,
        )
        manifest["commands"]["resolve_parameters"] = dump_record
        if not _command_succeeded(dump_record):
            raise CampaignError("roslaunch parameter resolution failed")
        resolved_parameters = Path(dump_record["log"]).read_text(
            encoding="utf-8", errors="replace"
        ).lower()
        leaked_tokens = [token for token in FORBIDDEN_RUNTIME_TEXT if token in resolved_parameters]
        if leaked_tokens:
            raise CampaignError(
                "ground-truth runtime parameter detected: {}".format(
                    ", ".join(leaked_tokens)
                )
            )

        find_record = run_command(
            [
                str(tool_paths["catkin_find"]),
                "--libexec",
                "ov_msckf",
                "ros1_serial_msckf",
            ],
            output / "resolved_estimator_binary.txt",
            environment,
            preflight_timeout,
        )
        manifest["commands"]["resolve_estimator_binary"] = find_record
        if not _command_succeeded(find_record):
            raise CampaignError("catkin_find estimator-binary resolution failed")
        resolved_lines = [
            line.strip()
            for line in Path(find_record["log"])
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
            if line.strip()
        ]
        if not resolved_lines or Path(resolved_lines[-1]).resolve(strict=False) != paths["binary"]:
            raise CampaignError(
                "catkin_find resolved a different estimator binary than --binary"
            )

        launch_argv = [
            str(tool_paths["roslaunch"]),
            "-p",
            str(args.ros_port),
        ] + launch_arguments
        launch_record = run_command(
            launch_argv,
            output / "console.log",
            environment,
            args.timeout_seconds,
        )
        manifest["commands"]["roslaunch"] = launch_record
        manifest["completion"].update(
            {
                "estimator_started": True,
                "estimator_completed": _command_succeeded(launch_record),
                "timed_out": bool(launch_record.get("timed_out", False)),
                "roslaunch_exit_code": launch_record.get("exit_code"),
                "roslaunch_runtime_seconds": launch_record.get("duration_seconds"),
                "process_group_survived_cleanup": bool(
                    launch_record.get("process_group_survived_cleanup", False)
                ),
            }
        )
        manifest["checks"]["runtime_ground_truth_excluded"] = all(
            token not in "\n".join(launch_argv).lower()
            for token in FORBIDDEN_RUNTIME_TEXT
        ) and str(paths["reference_tum"]) not in launch_argv
        manifest["console_diagnostics"] = _console_diagnostics(output / "console.log")
        diagnostic_counts = manifest["console_diagnostics"]["counts"]
        manifest["completion"].update(
            {
                "reset_detected": diagnostic_counts["reset_lines"] != 0,
                "nonfinite_detected": diagnostic_counts["nonfinite_lines"] != 0,
                "unpaired_warning_count": diagnostic_counts[
                    "unpaired_warning_lines"
                ],
                "dropped_message_count": diagnostic_counts[
                    "dropped_message_lines"
                ],
            }
        )
        if not _command_succeeded(launch_record):
            if launch_record.get("timed_out"):
                manifest["status"] = "TIMED_OUT"
            raise CampaignError("fixed KAIST roslaunch did not complete cleanly")

        manifest["exact_header_stereo_runtime"] = (
            _bind_exact_header_runtime_summary(
                output / "console.log", adapted_audit
            )
        )
        manifest["checks"]["exact_header_runtime_matches_adapted_audit"] = True
        manifest["camera_enqueue_runtime"] = _bind_camera_enqueue_runtime_summary(
            output / "console.log",
            manifest["exact_header_stereo_runtime"]["observed"][
                "exact_header_pairs"
            ],
        )
        manifest["checks"]["camera_enqueue_accounts_for_exact_header_pairs"] = True
        manifest["checks"]["camera_decode_failures_zero"] = True
        manifest["checks"]["camera_processed_pairs_match_queued"] = True
        manifest["checks"]["camera_queue_drained"] = True

        # Until roslaunch terminates, only the reference's byte identity is
        # bound. Its numeric ground-truth contents are first parsed here.
        manifest["reference_validation"] = _validate_numeric_table(
            paths["reference_tum"], None, 8
        )

        manifest["outputs"] = {
            "state": _validate_numeric_table(state, None, 8),
            "deviation": _validate_numeric_table(deviation, None, 2),
            "timing": _validate_numeric_table(timing, ",", 2),
        }

        converter_argv = [str(PYTHON), str(CONVERTER_PATH), str(state), str(trajectory)]
        conversion_record = run_command(
            converter_argv,
            output / "trajectory_conversion.log",
            environment,
            min(args.timeout_seconds, 300.0),
        )
        manifest["commands"]["trajectory_conversion"] = conversion_record
        if not _command_succeeded(conversion_record):
            raise CampaignError("OpenVINS-to-TUM conversion failed")
        manifest["outputs"]["trajectory_tum"] = _validate_numeric_table(
            trajectory, None, 8
        )
        manifest["output_consistency"] = _validate_output_consistency(
            state, deviation, timing, trajectory
        )
        manifest["checks"]["output_timestamp_and_row_consistency"] = True
        manifest["completion"]["output_validation_passed"] = True

        metric_specs = {
            "ape_translation": (
                tool_paths["evo_ape"],
                "trans_part",
                output / "evo_ape_translation.zip",
            ),
            "rpe_translation_1m": (
                tool_paths["evo_rpe"],
                "trans_part",
                output / "evo_rpe_translation_1m.zip",
            ),
            "rpe_rotation_1m_deg": (
                tool_paths["evo_rpe"],
                "angle_deg",
                output / "evo_rpe_rotation_1m_deg.zip",
            ),
        }
        manifest["metrics"] = {}
        for name, (tool, relation, archive) in metric_specs.items():
            metric_argv = [
                str(tool),
                "tum",
                str(paths["reference_tum"]),
                str(trajectory),
                "--t_max_diff",
                "0.01",
                "--pose_relation",
                relation,
            ]
            if name.startswith("rpe_"):
                metric_argv.extend(
                    [
                        "--delta",
                        "1.0",
                        "--delta_unit",
                        "m",
                        "--all_pairs",
                        "--pairs_from_reference",
                    ]
                )
            metric_argv.extend(
                ["--align", "--save_results", str(archive), "--no_warnings"]
            )
            record = run_command(
                metric_argv,
                output / (name + ".log"),
                environment,
                min(args.timeout_seconds, 300.0),
            )
            manifest["commands"][name] = record
            if not _command_succeeded(record):
                raise CampaignError("{} evaluation failed".format(name))
            manifest["metrics"][name] = _metric_archive(archive)

        manifest["completion"]["evaluation_completed"] = True
        manifest["checks"].update(
            {
                "no_reset_lines": diagnostic_counts["reset_lines"] == 0,
                "no_nonfinite_lines": diagnostic_counts["nonfinite_lines"] == 0,
                "no_unpaired_warning_lines": diagnostic_counts["unpaired_warning_lines"] == 0,
                "no_dropped_message_lines": diagnostic_counts["dropped_message_lines"] == 0,
            }
        )
        if not all(manifest["checks"].values()):
            raise CampaignError("runtime diagnostics or ground-truth boundary check failed")

        manifest["inputs_after"] = {
            name: file_identity(path) for name, path in sorted(fixed_inputs.items())
        }
        manifest["inputs_after"]["adapted_bag"] = file_identity(paths["adapted_bag"])
        changed = [
            name
            for name in manifest["inputs_before"]
            if manifest["inputs_before"][name] != manifest["inputs_after"][name]
        ]
        manifest["checks"]["fixed_inputs_unchanged"] = not changed
        if changed:
            raise CampaignError("fixed inputs changed during run: {}".format(", ".join(changed)))

        manifest["status"] = "COMPLETED"
        manifest["finished_utc"] = utc_now()
        manifest["runtime"]["total_duration_seconds"] = (
            time.monotonic() - campaign_started
        )
        _atomic_write_new_json(result_path, manifest)
        return manifest
    except BaseException as exc:
        if manifest.get("status") != "TIMED_OUT":
            manifest["status"] = "FAILED"
        manifest["finished_utc"] = utc_now()
        if "runtime" in manifest:
            manifest["runtime"]["total_duration_seconds"] = (
                time.monotonic() - campaign_started
            )
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _atomic_write_new_json(result_path, manifest)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, run, and post-run evaluate one fixed KAIST-VIO sequence."
    )
    parser.add_argument("--sequence", required=True, choices=SEQUENCE_ORDER)
    parser.add_argument("--source-bag", required=True, type=Path)
    parser.add_argument("--adapted-bag", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--reference-tum", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--ros-port", type=int)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="audit/adapt and record rosbag metadata without starting the estimator",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_campaign(args)
    except (RuntimeError, OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print("KAIST_VIO_CAMPAIGN_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(
        "{} {}: {}".format(result["status"], result["sequence"], args.output_dir)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
