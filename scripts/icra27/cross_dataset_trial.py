#!/usr/bin/python3.8
"""Append-only EuRoC/TUM-VI OpenVINS U0/S1 trial runner.

The estimator is run without a ground-truth path or ground-truth parameter.
Scored and qualitative-capture attempts are separate estimator replays.  A
capture attempt retains the raw five-topic sparse-geometry stream and binds it
to a previously closed scored result; it never substitutes its trajectory for
the scored trajectory.

U0 is the native upstream system: its launch may bind only the config, input
window, and passive evidence outputs.  S1 differs only by selecting Schur
landmark elimination with one visual pass.  Long-gap recovery must remain
default-off, and any recovery log event invalidates the runtime contract.
"""

from __future__ import annotations

import argparse
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
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PYTHON = Path("/usr/bin/python3.8")
CONVERTER = REPO_ROOT / "scripts" / "cp0" / "openvins_to_tum.py"
PAIRING_TOOL = SCRIPT_DIR / "kaist_pairing_census.py"
RUNTIME_IDENTITY_TOOL = SCRIPT_DIR / "kaist_runtime_identity.py"

# Reuse the already-tested native record-time selector and bounded-memory raw
# ROS Header reader.  Despite the historical filename, these functions are
# dataset-neutral when supplied the standard EuRoC/TUM-VI topics below.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import kaist_pairing_census as pairing  # noqa: E402
import kaist_runtime_identity as pinned_runtime  # noqa: E402


SCHEMA = "schurvio.icra27.cross_dataset.sequence_result.v1"
NATIVE_CENSUS_SCHEMA = "schurvio.icra27.cross_dataset.native_pair_census.v3"
SYSTEMS = ("U0", "S1")
# PERTURB-1 (docs/icra27/PERTURBATION_PREREG.md): N0 is the matched nullspace
# control on the frozen S1 executable; it is accepted only in perturbation mode.
S1_LIKE_SYSTEMS = ("S1", "N0")
LANDMARK_ELIMINATION = {"S1": "schur", "N0": "nullspace"}
PERTURBATION_SYSTEMS = ("U0", "S1", "N0")
PERTURBATION_CAMPAIGN_ID = "PERTURB-1"
PERTURBATION_SEED_LABELS = ("frozen",)
MODES = ("scored", "capture")
DATASETS = ("euroc_mav", "tum_vi")
ELIGIBLE_STATUSES = ("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT")
STATUSES = (
    "ACTIVE",
    "COMPLETED",
    "COMPLETED_WITH_TEARDOWN_DEFECT",
    "NO_INITIALIZATION",
    "ESTIMATOR_CRASH",
    "TRACKING_LOSS",
    "NUMERIC_FAILURE",
    "PARTIAL",
    "TIMED_OUT",
    "TEARDOWN_FAILED",
    "INVALID_OUTPUT",
    "CAPTURE_INCOMPLETE",
    "INVALID_LINKAGE",
    "INFRASTRUCTURE_FAILED",
    "INTERRUPTED",
)

NODE_NAME = "icra27_cross_dataset"
NODE_NAMESPACE = "/" + NODE_NAME
CAMERA0_TOPIC = "/cam0/image_raw"
CAMERA1_TOPIC = "/cam1/image_raw"
IMU_TOPIC = "/imu0"
GEOMETRY_TOPICS = (
    NODE_NAMESPACE + "/poseimu",
    NODE_NAMESPACE + "/points_slam",
    NODE_NAMESPACE + "/points_msckf",
    NODE_NAMESPACE + "/points_aruco",
    NODE_NAMESPACE + "/loop_feats",
)

COMMON_NODE_PARAMETERS = frozenset(
    (
        "config_path",
        "path_bag",
        "bag_start",
        "bag_durr",
        "save_total_state",
        "filepath_est",
        "filepath_std",
        "record_timing_information",
        "record_timing_filepath",
    )
)
S1_NODE_PARAMETERS = frozenset(
    (*COMMON_NODE_PARAMETERS, "up_msckf_landmark_elimination", "up_msckf_max_visual_passes")
)
LAUNCH_ARGUMENTS = frozenset(
    (
        "config_path",
        "bag",
        "bag_start",
        "bag_durr",
        "path_state",
        "path_std",
        "path_time",
        "record_timing",
    )
)

SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CHILD_EXIT_RE = re.compile(
    r"process(?:\[[^\]]+\])?\s+has died\s+\[pid\s+\d+,\s+exit code\s+(-?\d+)",
    re.IGNORECASE,
)
CHILD_START_RE = re.compile(
    r"process\[([^\]]+)\]:\s+started with pid\s+\[(\d+)\]", re.IGNORECASE
)
TEARDOWN_CLASS_RE = re.compile(r"class_loader::LibraryUnloadException", re.IGNORECASE)
TEARDOWN_LIBRARY_RE = re.compile(
    r"Attempt to unload library[^\n]*compressed_depth_image_transport\.so",
    re.IGNORECASE,
)
NONFINITE_RE = re.compile(r"\b(?:nan|inf|nonfinite|non-finite)\b", re.IGNORECASE)
COVARIANCE_FAILURE_RE = re.compile(
    r"negative covariance|covariance.*(?:invalid|failure|not positive)", re.IGNORECASE
)
DECODE_FAILURE_RE = re.compile(r"cv_bridge\s+exception", re.IGNORECASE)
RESET_RE = re.compile(r"\b(?:resetting|estimator reset|state reset)\b", re.IGNORECASE)
RECOVERY_PREFIX = "[LONG-GAP-RECOVERY]:"

FIXED_ENVIRONMENT = {
    "CUDA_VISIBLE_DEVICES": "",
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
ALLOWED_ENVIRONMENT = frozenset(
    (
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
    )
)

TAIL_GAP_MAX_SECONDS = 0.10
NEGATIVE_TAIL_TOLERANCE_SECONDS = 0.01
INITIALIZATION_DELAY_MAX_FRACTION = 0.10
MAXIMUM_STATE_GAP_SECONDS = 0.20

U0_SOURCE_ROOT = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/open_vins"
)
U0_PACKAGE_ROOT = U0_SOURCE_ROOT / "ov_msckf"
U0_SOURCE_COMMIT = "69488123ed9362dd44b6f28e7f4680abbff1442b"
U0_SOURCE_TREE = "12ab1c94ccae78ad50fa7376f47f0e55e2672257"
S1_SOURCE_COMMIT = "2751bcdc0fae25c993b3224dc5fa40aaab6571d7"
S1_SOURCE_TREE = "b3f9191b8bce3852ebff71e1ebf180181bcd6d05"
S1_CONFIGURE_PROVENANCE = (
    REPO_ROOT
    / "build"
    / "cp0-ws"
    / "build"
    / "ov_msckf"
    / "turnsafe-generated"
    / "configure_provenance.json"
)
S1_CONFIGURE_PROVENANCE_SHA256 = (
    "f4b34d99a7ed281c4a54e134c22b0fbb898864f52fd4be05ec03c2b5e18d5396"
)
S1_BUILD_PROVENANCE = REPO_ROOT / "build" / "cp0-ws" / "CP0_BUILD_PROVENANCE.json"
S1_BUILD_PROVENANCE_SHA256 = (
    "4f221f2c62fbf07caae8ad959eedfa44f889e7c0cb6b05054291b60d3bc465a9"
)
CANONICAL_PROTOCOL = REPO_ROOT / "docs" / "icra27" / "CROSS_DATASET_SYSTEM_COMPARISON_PROTOCOL.md"
CANONICAL_MATRIX = REPO_ROOT / "project" / "icra27_cross_dataset_matrix.yaml"
ROSBAG_INDEX_MODULE = Path(
    "/opt/ros/noetic/lib/python3/dist-packages/rosbag/bag.py"
)
ROSBAG_INDEX_MODULE_SIZE = 115588
ROSBAG_INDEX_MODULE_SHA256 = (
    "8c2e1f4b0e1bead5e03694c493a35b10f898bb7b051ca96aef219611b5253422"
)
CANONICAL_LAUNCHES: Mapping[str, Tuple[Path, str]] = {
    "U0": (
        REPO_ROOT / "project" / "icra27_cross_dataset_u0_serial.launch",
        "09f69b330144587da5fc1eae4196a78c2011d5e1f19c9a4bcf5ee5606a0fb57b",
    ),
    "S1": (
        REPO_ROOT / "project" / "icra27_cross_dataset_s1_serial.launch",
        "68a15b64495cb34a1ec00321e9002d3c6ed8d277ff1d3969c0d51750f0f96255",
    ),
    "N0": (
        REPO_ROOT / "project" / "icra27_cross_dataset_n0_serial.launch",
        "59498adb9dab301de90ca3181d9a4eda5c985f8914175b2b8491e660058934ab",
    ),
}
DATASET_CONFIG_SHA256: Mapping[str, str] = {
    "euroc_mav": "b706f0082106e49e20c3292147d238b7e225b0df414106b9d4ac009bbb123f3b",
    "tum_vi": "be7df3758fb38fbee102efdb6f2c98a7250416fe54e6045ff13b64a58972a060",
}

# This policy is the current frozen C2 runtime, not the historical A0 policy
# embedded in kaist_runtime_identity.py.  The validator itself is reused so
# executable, critical OpenVINS DSOs, Ceres, every loader resolution, and the
# CP0 build-provenance declaration receive the same fail-closed treatment.
S1_RUNTIME_POLICY: Mapping[str, Any] = {
    "policy_id": "cdsc1-c2-s1-runtime-20260816-v1",
    "approved_external_roots": ["/usr/lib", "/opt/ros/noetic/lib"],
    "systems": {
        "S1": {
            "executable": {
                "loader_path": str(
                    REPO_ROOT
                    / "build"
                    / "cp0-ws"
                    / "devel"
                    / "lib"
                    / "ov_msckf"
                    / "ros1_serial_msckf"
                ),
                "canonical_path": str(
                    REPO_ROOT
                    / "build"
                    / "cp0-ws"
                    / "devel"
                    / "lib"
                    / "ov_msckf"
                    / "ros1_serial_msckf"
                ),
                "size_bytes": 38000088,
                "sha256": "0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e",
                "build_id": "c51f77d0ad5b25941847b3971b5ba474c002aca0",
            },
            "critical_dependencies": {
                "libov_msckf_lib.so": {
                    "loader_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_msckf_lib.so"
                    ),
                    "canonical_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_msckf_lib.so"
                    ),
                    "size_bytes": 312116688,
                    "sha256": "cb1b252a07654840b46bca053fab6a356e82b85385e5815afc31faad88a0cae7",
                    "build_id": "a1217311cfe8424982ab60729a298d6966ca252f",
                },
                "libov_core_lib.so": {
                    "loader_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_core_lib.so"
                    ),
                    "canonical_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_core_lib.so"
                    ),
                    "size_bytes": 89213056,
                    "sha256": "200e7fada337ac0fa7d39e88a53bb88ed72310edbe64677d56d633feaf4c233f",
                    "build_id": "056006845d452c93b83ed55244bf85516576db2f",
                },
                "libov_init_lib.so": {
                    "loader_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_init_lib.so"
                    ),
                    "canonical_path": str(
                        REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "libov_init_lib.so"
                    ),
                    "size_bytes": 101192080,
                    "sha256": "ec9cf2df97813c9f4d6ff912a48c1e59c9cc52875cfd5e05b91333ecd6810394",
                    "build_id": "997dc4e7d8c224053be21a4a55b158553613bcce",
                },
                "libceres.so.1": {
                    "loader_path": str(
                        REPO_ROOT / "build" / "vendor" / "ceres-install" / "lib" / "libceres.so.1"
                    ),
                    "canonical_path": str(
                        REPO_ROOT
                        / "build"
                        / "vendor"
                        / "ceres-install"
                        / "lib"
                        / "libceres.so.1.14.0"
                    ),
                    "size_bytes": 3294168,
                    "sha256": "a85e4691c24d265d0940ef9f03ca4e3c633ac1e5c5a46dae693dfb7eea5ee56c",
                    "build_id": "96e4428ac408a0e64ced7b302810290007a0790a",
                },
            },
            "provenance": {
                "path": str(S1_BUILD_PROVENANCE),
                "size_bytes": 42185,
                "sha256": S1_BUILD_PROVENANCE_SHA256,
                "schema_version": 1,
                "source_commit": S1_SOURCE_COMMIT,
                "runtime_dependency_sonames": [
                    "libov_core_lib.so",
                    "libov_init_lib.so",
                    "libov_msckf_lib.so",
                ],
                "ceres_soname": "libceres.so.1",
            },
        }
    },
}


class TrialError(RuntimeError):
    """A fail-closed harness or evidence error."""


class NoRowsError(TrialError):
    """A numeric output exists but contains no data rows."""


@dataclass
class ManagedProcess:
    name: str
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
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> Dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise TrialError("not a regular file: {}".format(resolved))
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": sha256_file(resolved),
        "mtime_ns": stat.st_mtime_ns,
        "executable": bool(stat.st_mode & 0o111),
    }


def _identity_from_census(path: Path, census_identity: Mapping[str, Any]) -> Dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    stat = resolved.stat()
    if str(resolved) != census_identity.get("path"):
        raise TrialError("pair census returned a different bag path")
    if stat.st_size != census_identity.get("size_bytes"):
        raise TrialError("bag size changed during pair census")
    digest = str(census_identity.get("sha256", ""))
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise TrialError("pair census returned an invalid bag SHA-256")
    return {
        "path": str(resolved),
        "size_bytes": stat.st_size,
        "sha256": digest,
        "mtime_ns": stat.st_mtime_ns,
        "executable": False,
    }


def _regular_file(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as exc:
        raise TrialError("{} does not resolve: {}".format(label, path)) from exc
    if not resolved.is_file():
        raise TrialError("{} is not a regular file: {}".format(label, resolved))
    return resolved


def _atomic_write_new_bytes(path: Path, payload: bytes) -> None:
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
    payload = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    _atomic_write_new_bytes(path, payload)


def write_sha256sums(run_dir: Path) -> Dict[str, str]:
    destination = run_dir / "SHA256SUMS"
    if destination.exists() or destination.is_symlink():
        raise TrialError("refusing to overwrite SHA256SUMS")
    identities: Dict[str, str] = {}
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            raise TrialError(
                "refusing checksum publication with symlink: {}".format(
                    path.relative_to(run_dir).as_posix()
                )
            )
        if not path.is_file() or path == destination:
            continue
        relative = path.relative_to(run_dir).as_posix()
        if "\n" in relative or "\r" in relative or relative.startswith("/"):
            raise TrialError("unsafe artifact path in checksum set")
        identities[relative] = sha256_file(path)
    lines = ["{}  {}\n".format(identities[name], name) for name in sorted(identities)]
    _atomic_write_new_bytes(destination, "".join(lines).encode("utf-8"))
    return identities


def remove_run_owned_ros_latest_symlink(run_dir: Path) -> Dict[str, Any]:
    """Remove only ROS's ephemeral ``ros-logs/latest`` link before closure."""

    ros_logs = run_dir / "ros-logs"
    latest = ros_logs / "latest"
    if latest.is_symlink():
        target = os.readlink(str(latest))
        try:
            resolved_logs = ros_logs.resolve(strict=True)
            resolved_target = latest.resolve(strict=True)
            resolved_target.relative_to(resolved_logs)
        except (OSError, ValueError) as exc:
            raise TrialError(
                "ros-logs/latest does not target a retained run-owned log directory"
            ) from exc
        if not resolved_target.is_dir():
            raise TrialError("ros-logs/latest target is not a directory")
        latest.unlink()
        directory_descriptor = os.open(str(ros_logs), os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        if latest.exists() or latest.is_symlink():
            raise TrialError("ROS latest symlink survived removal")
        return {
            "path": "ros-logs/latest",
            "status": "REMOVED_RUN_OWNED_EPHEMERAL_SYMLINK",
            "link_target": target,
            "resolved_target": str(resolved_target),
            "run_tree_symlink_policy": "NO_SYMLINKS_AT_CHECKSUM_CLOSURE",
        }
    if latest.exists():
        raise TrialError("ros-logs/latest exists but is not ROS's expected symlink")
    return {
        "path": "ros-logs/latest",
        "status": "ABSENT",
        "link_target": None,
        "resolved_target": None,
        "run_tree_symlink_policy": "NO_SYMLINKS_AT_CHECKSUM_CLOSURE",
    }


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
        "geometry",
        "isolated-home",
        "ros-home",
        "ros-logs",
    ):
        (run_dir / relative).mkdir()
    return run_dir


def minimal_environment(run_dir: Path, ros_port: int) -> Dict[str, str]:
    environment = {
        key: os.environ[key] for key in sorted(ALLOWED_ENVIRONMENT) if key in os.environ
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


def _wait_for_port(port: int, expected_open: bool, timeout_seconds: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _port_open(port) == expected_open:
            return
        time.sleep(0.1)
    raise TrialError(
        "ROS port {} did not become {}".format(port, "open" if expected_open else "closed")
    )


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _wait_for_process_group_exit(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.05)
    return not _process_group_exists(process_group)


def _clean_process_group(
    process: subprocess.Popen, first_signal: signal.Signals = signal.SIGINT
) -> Tuple[List[str], bool]:
    order: List[Tuple[signal.Signals, float]] = []
    if first_signal == signal.SIGINT:
        order.append((signal.SIGINT, 10.0))
    order.extend(((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)))
    signals_sent: List[str] = []
    for stop_signal, grace in order:
        if not _process_group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, stop_signal)
            signals_sent.append(stop_signal.name)
        except ProcessLookupError:
            break
        if _wait_for_process_group_exit(process.pid, grace):
            break
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    return signals_sent, _process_group_exists(process.pid)


def run_command(
    argv: Sequence[str],
    log_path: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    cwd: Path = REPO_ROOT,
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
        process: Optional[subprocess.Popen] = None
        try:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
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
            else:
                if _process_group_exists(process.pid):
                    extra, surviving_group = _clean_process_group(process)
                    signals_sent.extend(extra)
        except OSError as exc:
            error = str(exc)
            stream.write("failed to execute command: {}\n".format(exc))
            if process is not None and _process_group_exists(process.pid):
                extra, surviving_group = _clean_process_group(process)
                signals_sent.extend(extra)
        except BaseException:
            # A parser, I/O, or unexpected Python exception must not strand a
            # descendant merely because it was not a timeout/interrupt.
            if process is not None and _process_group_exists(process.pid):
                extra, surviving_group = _clean_process_group(process)
                signals_sent.extend(extra)
            raise
        stream.flush()
        os.fsync(stream.fileno())
    return {
        "argv": list(argv),
        "shell": shlex.join(list(argv)),
        "cwd": str(cwd),
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
    return bool(
        record.get("exit_code") == 0
        and not record.get("timed_out")
        and not record.get("interrupted")
        and not record.get("process_group_survived_cleanup")
        and record.get("error") is None
    )


def start_managed_process(
    name: str, argv: Sequence[str], log_path: Path, environment: Mapping[str, str]
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
        name=name,
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
        "name": managed.name,
        "argv": managed.argv,
        "shell": shlex.join(managed.argv),
        "started_utc": managed.started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - managed.started_monotonic,
        "exit_code": exit_code,
        "signals_sent": signals_sent,
        "process_group_survived_cleanup": survived,
        "log": file_identity(managed.log_path),
    }


def _which(name: str) -> Path:
    value = shutil.which(name)
    if value is None:
        raise TrialError("required command is unavailable: {}".format(name))
    return Path(value).resolve(strict=True)


def _opencv_yaml_mapping(path: Path) -> Tuple[Mapping[str, Any], str]:
    text = path.read_text(encoding="utf-8", errors="strict")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].startswith("%YAML:"):
        if not lines[0].startswith("%YAML:1.0"):
            raise TrialError("unsupported OpenCV YAML directive in {}".format(path))
        text_for_yaml = "".join(lines[1:])
    else:
        text_for_yaml = text
    try:
        value = yaml.safe_load(text_for_yaml)
    except yaml.YAMLError as exc:
        raise TrialError("invalid YAML: {}".format(path)) from exc
    if not isinstance(value, dict):
        raise TrialError("configuration is not a YAML mapping: {}".format(path))
    return value, text


def _strict_boolean_key(text: str, key: str, default: bool) -> bool:
    matches = re.findall(
        r"(?m)^{}\s*:\s*([^#\r\n]+)".format(re.escape(key)), text
    )
    if not matches:
        return default
    if len(matches) != 1:
        raise TrialError("configuration contains duplicate {} keys".format(key))
    token = matches[0].strip()
    if token not in ("true", "false"):
        raise TrialError("{} must use the exact Boolean token true or false".format(key))
    return token == "true"


def validate_config_contract(system: str, config: Path) -> Dict[str, Any]:
    mapping, text = _opencv_yaml_mapping(config)
    recovery_enabled = _strict_boolean_key(text, "long_gap_recovery_enabled", False)
    if recovery_enabled:
        raise TrialError("cross-dataset long-gap recovery must remain default-off")
    selector_keys = (
        "up_msckf_landmark_elimination",
        "up_msckf_max_visual_passes",
    )
    present_selectors = [name for name in selector_keys if name in mapping]
    if present_selectors:
        raise TrialError(
            "dataset config must not embed launch-owned updater selectors: {}".format(
                present_selectors
            )
        )
    if mapping.get("use_stereo") is not True or mapping.get("max_cameras") != 2:
        raise TrialError("cross-dataset config must retain native two-camera stereo")
    raw_track_frequency = mapping.get("track_frequency")
    if (
        isinstance(raw_track_frequency, bool)
        or not isinstance(raw_track_frequency, (int, float))
    ):
        raise TrialError("cross-dataset config must define numeric track_frequency")
    track_frequency_hz = float(raw_track_frequency)
    if not math.isfinite(track_frequency_hz) or track_frequency_hz <= 0.0:
        raise TrialError("track_frequency must be finite and positive")

    external: Dict[str, Any] = {}
    for config_key, label in (
        ("relative_config_imu", "imu"),
        ("relative_config_imucam", "imucam"),
    ):
        relative = mapping.get(config_key)
        if not isinstance(relative, str) or not relative:
            raise TrialError("configuration lacks {}".format(config_key))
        path = _regular_file(config.parent / relative, label + " calibration")
        external[label] = {"identity": file_identity(path)}
        external_mapping, _ = _opencv_yaml_mapping(path)
        if label == "imu":
            observed = external_mapping.get("imu0", {}).get("rostopic")
            expected = IMU_TOPIC
            external[label]["topics"] = {"imu0": observed}
        else:
            observed = (
                external_mapping.get("cam0", {}).get("rostopic"),
                external_mapping.get("cam1", {}).get("rostopic"),
            )
            expected = (CAMERA0_TOPIC, CAMERA1_TOPIC)
            external[label]["topics"] = {"cam0": observed[0], "cam1": observed[1]}
        if observed != expected:
            raise TrialError(
                "{} calibration topics differ from native cross-dataset topics".format(label)
            )

    masks: Dict[str, Any] = {}
    if mapping.get("use_mask") is True:
        for key in ("mask0", "mask1"):
            relative = mapping.get(key)
            if not isinstance(relative, str) or not relative:
                raise TrialError("enabled mask config lacks {}".format(key))
            masks[key] = file_identity(_regular_file(config.parent / relative, key))
    return {
        "system": system,
        "recovery_enabled": False,
        "recovery_source": (
            "explicit_false" if "long_gap_recovery_enabled" in mapping else "compiled_default_false"
        ),
        "updater_selectors_absent_from_dataset_config": True,
        "native_stereo": True,
        "track_frequency_hz": track_frequency_hz,
        "track_frequency_source": "canonical_dataset_config",
        "external_configs": external,
        "masks": masks,
    }


def validate_canonical_paths_and_hashes(
    dataset: str,
    system: str,
    config: Path,
    launch: Path,
    protocol: Path,
    matrix: Path,
) -> Dict[str, Any]:
    expected_config = (
        U0_SOURCE_ROOT / "config" / dataset / "estimator_config.yaml"
        if system == "U0"
        else REPO_ROOT / "config" / dataset / "estimator_config.yaml"
    ).resolve(strict=True)
    expected_launch, expected_launch_sha = CANONICAL_LAUNCHES[system]
    expected_launch = expected_launch.resolve(strict=True)
    if config != expected_config:
        raise TrialError("estimator config is not the canonical {} {} path".format(dataset, system))
    if launch != expected_launch:
        raise TrialError("launch is not the canonical {} path".format(system))
    if protocol != CANONICAL_PROTOCOL.resolve(strict=True):
        raise TrialError("protocol is not the canonical CDSC-1R4 path")
    if matrix != CANONICAL_MATRIX.resolve(strict=True):
        raise TrialError("matrix is not the canonical CDSC-1R4 path")
    config_identity = file_identity(config)
    launch_identity = file_identity(launch)
    if config_identity["sha256"] != DATASET_CONFIG_SHA256[dataset]:
        raise TrialError("canonical {} config SHA-256 drift".format(dataset))
    if launch_identity["sha256"] != expected_launch_sha:
        raise TrialError("canonical {} launch SHA-256 drift".format(system))
    return {
        "config": config_identity,
        "launch": launch_identity,
        "protocol_path": str(protocol),
        "matrix_path": str(matrix),
        "all_canonical_paths_and_frozen_hashes_match": True,
    }


def validate_campaign_bindings(
    protocol_id: str,
    protocol_path: Path,
    matrix_path: Path,
    dataset: str,
    sequence: str,
    system: str,
    bag: Path,
    bag_start: float,
) -> Dict[str, Any]:
    """Bind one run request to the live prospective protocol and matrix bytes."""

    protocol_text = protocol_path.read_text(encoding="utf-8", errors="strict")
    match = re.search(r"(?m)^- Protocol ID:\s*`([^`]+)`\s*$", protocol_text)
    if match is None or match.group(1) != protocol_id:
        raise TrialError("protocol file ID does not match --protocol-id")
    if "PROSPECTIVE_NOT_RUN" not in protocol_text:
        raise TrialError("protocol file lacks the prospective freeze marker")
    try:
        matrix = yaml.safe_load(matrix_path.read_text(encoding="utf-8", errors="strict"))
    except yaml.YAMLError as exc:
        raise TrialError("campaign matrix is invalid YAML") from exc
    if not isinstance(matrix, dict) or matrix.get("schema") != (
        "schurvio.icra27.cross_dataset_matrix.v1"
    ):
        raise TrialError("campaign matrix schema mismatch")
    rows = matrix.get("sequences")
    if not isinstance(rows, list) or len(rows) != matrix.get("sequence_count"):
        raise TrialError("campaign matrix sequence count does not close")
    matches = [
        row
        for row in rows
        if isinstance(row, dict)
        and row.get("dataset") == dataset
        and row.get("sequence") == sequence
    ]
    if len(matches) != 1:
        raise TrialError("run request does not identify exactly one campaign matrix row")
    row = matches[0]
    if system not in row.get("system_order", []):
        raise TrialError("requested system is absent from campaign matrix row")
    matrix_bag = row.get("bag")
    if not isinstance(matrix_bag, dict):
        raise TrialError("campaign matrix row lacks bag identity")
    if Path(str(matrix_bag.get("path", ""))).resolve(strict=False) != bag:
        raise TrialError("requested bag path differs from campaign matrix")
    if float(row.get("bag_start_seconds", math.nan)) != bag_start:
        raise TrialError("requested bag start differs from campaign matrix")
    row_material = {
        "order": row.get("order"),
        "dataset": dataset,
        "sequence": sequence,
        "system_order": row.get("system_order"),
        "bag_start_seconds": row.get("bag_start_seconds"),
        "bag": matrix_bag,
    }
    return {
        "protocol": file_identity(protocol_path),
        "matrix": file_identity(matrix_path),
        "matrix_schema": matrix["schema"],
        "matrix_status": matrix.get("status"),
        "matrix_sequence_count": len(rows),
        "row_order": row.get("order"),
        "row_sha256": hashlib.sha256(
            json.dumps(row_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "expected_bag": {
            "path": str(bag),
            "size_bytes": int(matrix_bag.get("bytes", -1)),
            "sha256": str(matrix_bag.get("sha256", "")),
        },
    }


def validate_matrix_bag_identity(
    campaign: Mapping[str, Any], bag_identity: Mapping[str, Any]
) -> None:
    expected = campaign.get("expected_bag")
    if not isinstance(expected, dict):
        raise TrialError("campaign binding lacks expected bag identity")
    for key in ("path", "size_bytes", "sha256"):
        if expected.get(key) != bag_identity.get(key):
            raise TrialError(
                "bag {} differs from matrix: {} != {}".format(
                    key, bag_identity.get(key), expected.get(key)
                )
            )


def validate_launch_contract(system: str, launch: Path) -> Dict[str, Any]:
    try:
        root = ET.parse(str(launch)).getroot()
    except (ET.ParseError, OSError) as exc:
        raise TrialError("invalid ROS launch XML: {}".format(launch)) from exc
    if root.tag != "launch":
        raise TrialError("launch root element is not <launch>")
    arguments = [element.get("name") for element in root.findall("./arg")]
    if None in arguments or set(arguments) != LAUNCH_ARGUMENTS or len(arguments) != len(
        LAUNCH_ARGUMENTS
    ):
        raise TrialError("cross-dataset launch argument contract drift")
    nodes = root.findall("./node")
    if len(nodes) != 1:
        raise TrialError("cross-dataset launch must contain exactly one node")
    node = nodes[0]
    if (
        node.get("name") != NODE_NAME
        or node.get("pkg") != "ov_msckf"
        or node.get("type") != "ros1_serial_msckf"
        or node.get("clear_params") != "true"
        or node.get("required") != "true"
    ):
        raise TrialError("cross-dataset estimator node identity drift")
    parameters = node.findall("./param")
    names = [element.get("name") for element in parameters]
    expected = COMMON_NODE_PARAMETERS if system == "U0" else S1_NODE_PARAMETERS
    if None in names or set(names) != expected or len(names) != len(expected):
        raise TrialError(
            "{} launch parameter set differs: observed={} expected={}".format(
                system, sorted(str(name) for name in names), sorted(expected)
            )
        )
    by_name = {str(element.get("name")): element for element in parameters}
    if system in S1_LIKE_SYSTEMS:
        if by_name["up_msckf_landmark_elimination"].get("value") != LANDMARK_ELIMINATION[system]:
            raise TrialError(
                "{} launch does not select {} elimination".format(
                    system, LANDMARK_ELIMINATION[system]
                )
            )
        if (
            by_name["up_msckf_max_visual_passes"].get("type") != "int"
            or by_name["up_msckf_max_visual_passes"].get("value") != "1"
        ):
            raise TrialError("S1 launch does not select exactly one visual pass")
    return {
        "node_name": NODE_NAME,
        "argument_names": sorted(arguments),
        "parameter_names": sorted(names),
        "u0_native_only_bindings": system == "U0",
        "s1_only_algorithm_deltas": (
            [
                "up_msckf_landmark_elimination=" + LANDMARK_ELIMINATION[system],
                "up_msckf_max_visual_passes=1",
            ]
            if system in S1_LIKE_SYSTEMS
            else []
        ),
        "n0_matched_nullspace_control": system == "N0",
        "recovery_parameter_absent": "long_gap_recovery_enabled" not in names,
    }


def _launch_arguments(
    launch: Path,
    config: Path,
    bag: Path,
    bag_start: float,
    bag_duration: float,
    run_dir: Path,
) -> List[str]:
    return [
        str(launch),
        "config_path:=" + str(config),
        "bag:=" + str(bag),
        "bag_start:=" + format(bag_start, ".17g"),
        "bag_durr:=" + format(bag_duration, ".17g"),
        "path_state:=" + str(run_dir / "trajectory" / "state_estimate.txt"),
        "path_std:=" + str(run_dir / "trajectory" / "state_deviation.txt"),
        "path_time:=" + str(run_dir / "diagnostics" / "timing_openvins.csv"),
        "record_timing:=true",
    ]


def validate_resolved_parameters(
    system: str,
    raw: str,
    config: Path,
    bag: Path,
    bag_start: float,
    bag_duration: float,
    run_dir: Path,
) -> Dict[str, Any]:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TrialError("resolved ROS parameter dump is invalid YAML") from exc
    if not isinstance(value, dict):
        raise TrialError("resolved ROS parameter dump is not a mapping")
    normalized: Dict[str, Any] = {}
    path_suffixes = {
        "config_path",
        "path_bag",
        "filepath_est",
        "filepath_std",
        "record_timing_filepath",
    }
    for key, observed in value.items():
        name = str(key)
        suffix = name.rsplit("/", 1)[-1]
        if suffix in path_suffixes:
            if not isinstance(observed, str):
                raise TrialError("resolved path parameter is not a string: {}".format(name))
            observed = str(Path(observed).resolve(strict=False))
        normalized[name] = observed
    namespace = NODE_NAMESPACE + "/"
    expected: Dict[str, Any] = {
        namespace + "config_path": str(config),
        namespace + "path_bag": str(bag),
        namespace + "bag_start": bag_start,
        namespace + "bag_durr": bag_duration,
        namespace + "save_total_state": True,
        namespace + "filepath_est": str(run_dir / "trajectory" / "state_estimate.txt"),
        namespace + "filepath_std": str(run_dir / "trajectory" / "state_deviation.txt"),
        namespace + "record_timing_information": True,
        namespace + "record_timing_filepath": str(
            run_dir / "diagnostics" / "timing_openvins.csv"
        ),
    }
    if system in S1_LIKE_SYSTEMS:
        expected.update(
            {
                namespace + "up_msckf_landmark_elimination": LANDMARK_ELIMINATION[system],
                namespace + "up_msckf_max_visual_passes": 1,
            }
        )
    if normalized != expected:
        missing = sorted(set(expected) - set(normalized))
        extra = sorted(set(normalized) - set(expected))
        changed = {
            key: {"observed": normalized[key], "expected": expected[key]}
            for key in sorted(set(normalized).intersection(expected))
            if normalized[key] != expected[key]
        }
        raise TrialError(
            "resolved parameter contract drift: missing={} extra={} changed={}".format(
                missing, extra, changed
            )
        )
    forbidden = [
        key
        for key in normalized
        if key.rsplit("/", 1)[-1]
        in ("path_gt", "initialize_with_gt", "long_gap_recovery_enabled")
    ]
    if forbidden:
        raise TrialError("forbidden runtime parameter present: {}".format(forbidden))
    return {
        "parameter_count": len(normalized),
        "exact_typed_map_match": True,
        "parameters": normalized,
        "ground_truth_parameters_absent": True,
        "recovery_parameter_absent": True,
    }


def _duration_nanoseconds(seconds: float, label: str) -> int:
    """Convert a nonnegative ROS duration to its integer-nanosecond value."""

    if not math.isfinite(seconds) or seconds < 0:
        raise TrialError("{} must be finite and nonnegative".format(label))
    # ros::Duration(double) rounds to the nearest integer nanosecond.  Frozen
    # campaign offsets are integral seconds, but retaining this conversion here
    # keeps the projection defined for any future finite fractional offset.
    whole_seconds = int(math.floor(seconds))
    nanoseconds = int(
        math.floor((seconds - float(whole_seconds)) * 1.0e9 + 0.5)
    )
    if nanoseconds == 1_000_000_000:
        whole_seconds += 1
        nanoseconds = 0
    return whole_seconds * 1_000_000_000 + nanoseconds


def _ros_time_nanoseconds(value: Any) -> int:
    """Return an exact nonnegative ROS time from a Python rosbag index entry."""

    try:
        seconds = int(value.secs)
        nanoseconds = int(value.nsecs)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TrialError("rosbag index entry has an invalid time") from exc
    if seconds < 0 or nanoseconds < 0 or nanoseconds >= 1_000_000_000:
        raise TrialError("rosbag index entry time is outside the ROS1 domain")
    return seconds * 1_000_000_000 + nanoseconds


def rosbag_view_full_bounds(opened: Any) -> Dict[str, Any]:
    """Recover the all-topic bounds used by C++ ``rosbag::View``.

    Python ``Bag.get_start_time()`` and ``get_end_time()`` use the first and
    last chunk-info headers for ROS bag v2 files.  Those diagnostic headers are
    not authoritative for message selection and can disagree with the actual
    connection index entries.  The serial OpenVINS executables construct an
    unfiltered C++ ``rosbag::View`` and use its index-entry extrema, so the
    prospective census must do the same.
    """

    public_start: Optional[float]
    public_end: Optional[float]
    try:
        public_start = float(opened.get_start_time())
        if not math.isfinite(public_start):
            public_start = None
    except (AttributeError, TypeError, ValueError):
        public_start = None
    try:
        public_end = float(opened.get_end_time())
        if not math.isfinite(public_end):
            public_end = None
    except (AttributeError, TypeError, ValueError):
        public_end = None

    get_indexes = getattr(opened, "_get_indexes", None)
    if not callable(get_indexes):
        raise TrialError("ROS1 rosbag does not expose connection index entries")
    try:
        indexes = get_indexes(None)
    except Exception as exc:
        raise TrialError("unable to read all-topic rosbag connection indexes") from exc

    first_ns: Optional[int] = None
    last_ns: Optional[int] = None
    entry_count = 0
    connection_count = 0
    for index in indexes:
        if not index:
            continue
        connection_count += 1
        previous_ns: Optional[int] = None
        for entry in index:
            timestamp_ns = _ros_time_nanoseconds(entry.time)
            if previous_ns is not None and timestamp_ns < previous_ns:
                raise TrialError("rosbag connection index timestamps reverse")
            previous_ns = timestamp_ns
            first_ns = timestamp_ns if first_ns is None else min(first_ns, timestamp_ns)
            last_ns = timestamp_ns if last_ns is None else max(last_ns, timestamp_ns)
            entry_count += 1
    if first_ns is None or last_ns is None or last_ns < first_ns:
        raise TrialError("rosbag has no valid all-topic index-entry bounds")

    first_s = pairing._cpp_ros_time_to_sec(first_ns)
    last_s = pairing._cpp_ros_time_to_sec(last_ns)
    public_matches = bool(
        public_start is not None
        and public_end is not None
        and public_start == first_s
        and public_end == last_s
    )
    public_start_delta = (
        None if public_start is None else public_start - first_s
    )
    public_end_delta = None if public_end is None else public_end - last_s
    return {
        "source": "all_topic_connection_index_extrema_matching_cpp_rosbag_view",
        "first_record_timestamp_ns": first_ns,
        "last_record_timestamp_ns": last_ns,
        "first_record_timestamp_s": first_s,
        "last_record_timestamp_s": last_s,
        "connection_count": connection_count,
        "entry_count": entry_count,
        "python_public_start_record_timestamp_s": public_start,
        "python_public_end_record_timestamp_s": public_end,
        "python_public_start_minus_index_start_s": public_start_delta,
        "python_public_end_minus_index_end_s": public_end_delta,
        "python_public_bounds_match_index_extrema": public_matches,
    }


def project_native_bag_view_exact(
    messages: Sequence[Any],
    full_start_ns: int,
    full_end_ns: int,
    bag_start: float,
    bag_duration: float,
) -> Tuple[List[Any], Any, int, int]:
    """Apply the serial runner's exact integer ROS-time View before pairing."""

    if not isinstance(full_start_ns, int) or not isinstance(full_end_ns, int):
        raise TrialError("full bag bounds must be integer nanoseconds")
    if full_start_ns < 0 or full_end_ns < full_start_ns:
        raise TrialError("full bag bounds are invalid")
    if not math.isfinite(bag_start) or bag_start < 0:
        raise TrialError("bag start must be finite and nonnegative")
    if not math.isfinite(bag_duration) or bag_duration == 0:
        raise TrialError("bag duration must be finite and nonzero")
    view_start_ns = full_start_ns + _duration_nanoseconds(bag_start, "bag start")
    view_end_ns = (
        full_end_ns
        if bag_duration < 0
        else view_start_ns + _duration_nanoseconds(bag_duration, "bag duration")
    )
    if view_end_ns <= view_start_ns:
        raise TrialError("selected bag view is empty")
    selected_messages = [
        message
        for message in messages
        if view_start_ns <= message.record_time_ns <= view_end_ns
    ]
    if not selected_messages:
        raise TrialError("selected bag view has no native sensor messages")
    native = pairing.select_upstream_native(selected_messages)
    if not native.pairs:
        raise TrialError("selected bag view has no native stereo pair")
    return selected_messages, native, view_start_ns, view_end_ns


def project_native_bag_view(
    messages: Sequence[Any],
    full_start: float,
    full_end: float,
    bag_start: float,
    bag_duration: float,
) -> Tuple[List[Any], Any, float, float]:
    """Compatibility wrapper for synthetic callers supplying second bounds."""

    if not math.isfinite(full_start) or not math.isfinite(full_end):
        raise TrialError("full bag bounds must be finite")
    selected, native, view_start_ns, view_end_ns = project_native_bag_view_exact(
        messages,
        _duration_nanoseconds(full_start, "full bag start"),
        _duration_nanoseconds(full_end, "full bag end"),
        bag_start,
        bag_duration,
    )
    return (
        selected,
        native,
        pairing._cpp_ros_time_to_sec(view_start_ns),
        pairing._cpp_ros_time_to_sec(view_end_ns),
    )


def _ordered_stereo_callback_digest(pairs: Sequence[Any]) -> str:
    digest = hashlib.sha256()
    digest.update(b"schurvio.icra27.ordered_stereo_callback_sequence.v1\0")
    for dispatch_index, pair in enumerate(pairs):
        digest.update(
            struct.pack(
                "<QQQQQQQQ",
                dispatch_index,
                int(pair.selection_index),
                int(pair.camera0_filtered_index),
                int(pair.camera1_filtered_index),
                int(pair.camera0_record_time_ns),
                int(pair.camera1_record_time_ns),
                int(pair.camera0_header_time_ns),
                int(pair.camera1_header_time_ns),
            )
        )
    return digest.hexdigest()


def apply_visualizer_track_frequency_gate(
    pairs: Sequence[Any], track_frequency_hz: float
) -> Tuple[Tuple[Any, ...], Dict[str, Any]]:
    """Mirror the stock ROS1Visualizer stereo callback's pre-decode gate.

    The native serial reader can invoke ``callback_stereo`` more than once
    with the same camera-0 message because its future-candidate lookup does
    not reject an already-used candidate. The unchanged visualizer applies
    its track-frequency gate before decoding or feeding the estimator. The
    estimator-facing input population is therefore the accepted callback
    sequence, while the raw dispatch population remains valuable evidence.
    Python ``float`` has the same binary64 arithmetic used by the pinned C++
    expression, and ``_cpp_ros_time_to_sec`` mirrors ``ros::Time::toSec``.
    """

    if (
        isinstance(track_frequency_hz, bool)
        or not isinstance(track_frequency_hz, (int, float))
    ):
        raise TrialError("visualizer track frequency must be numeric")
    frequency = float(track_frequency_hz)
    if not math.isfinite(frequency) or frequency <= 0.0:
        raise TrialError("visualizer track frequency must be finite and positive")
    time_delta = 1.0 / frequency
    if not math.isfinite(time_delta) or time_delta <= 0.0:
        raise TrialError("visualizer track-frequency period is invalid")

    raw_timestamps_ns = [int(pair.camera_timestamp_ns) for pair in pairs]
    raw_equal_adjacent_count = sum(
        right == left
        for left, right in zip(raw_timestamps_ns, raw_timestamps_ns[1:])
    )
    raw_reversal_adjacent_count = sum(
        right < left
        for left, right in zip(raw_timestamps_ns, raw_timestamps_ns[1:])
    )

    accepted: List[Any] = []
    dropped: List[Dict[str, Any]] = []
    last_accepted_timestamp_s: Optional[float] = None
    last_accepted_timestamp_ns: Optional[int] = None
    for dispatch_index, pair in enumerate(pairs):
        timestamp_ns = int(pair.camera_timestamp_ns)
        timestamp_s = pairing._cpp_ros_time_to_sec(timestamp_ns)
        threshold_s = (
            None
            if last_accepted_timestamp_s is None
            else last_accepted_timestamp_s + time_delta
        )
        if threshold_s is not None and timestamp_s < threshold_s:
            if last_accepted_timestamp_ns is None:
                raise TrialError("visualizer gate lost its accepted timestamp state")
            dropped.append(
                {
                    "raw_dispatch_index": dispatch_index,
                    "raw_selection_index": int(pair.selection_index),
                    "camera0_filtered_index": int(pair.camera0_filtered_index),
                    "camera1_filtered_index": int(pair.camera1_filtered_index),
                    "camera0_record_time_ns": int(pair.camera0_record_time_ns),
                    "camera1_record_time_ns": int(pair.camera1_record_time_ns),
                    "camera0_header_time_ns": int(pair.camera0_header_time_ns),
                    "camera1_header_time_ns": int(pair.camera1_header_time_ns),
                    "camera_timestamp_ns": timestamp_ns,
                    "camera_timestamp_s": timestamp_s,
                    "previous_accepted_timestamp_ns": last_accepted_timestamp_ns,
                    "previous_accepted_timestamp_s": last_accepted_timestamp_s,
                    "minimum_accepted_timestamp_s": threshold_s,
                    "delta_from_previous_accepted_ns": (
                        timestamp_ns - last_accepted_timestamp_ns
                    ),
                    "reason": (
                        "timestamp_less_than_previous_accepted_plus_"
                        "inverse_track_frequency"
                    ),
                }
            )
            continue
        accepted.append(pair)
        last_accepted_timestamp_s = timestamp_s
        last_accepted_timestamp_ns = timestamp_ns

    if not accepted:
        raise TrialError("visualizer track-frequency gate accepted no stereo callback")
    accepted_timestamps_ns = [int(pair.camera_timestamp_ns) for pair in accepted]
    if any(
        right <= left
        for left, right in zip(
            accepted_timestamps_ns, accepted_timestamps_ns[1:]
        )
    ):
        raise TrialError(
            "visualizer-accepted camera timestamps are not strictly increasing"
        )
    return tuple(accepted), {
        "policy": "stock_ros1_visualizer_stereo_track_frequency_gate_v1",
        "timestamp_source": "camera0_ros_header_stamp_toSec_binary64",
        "expression": (
            "drop_if_timestamp_less_than_previous_accepted_timestamp_plus_"
            "inverse_track_frequency"
        ),
        "comparison": "strict_less_than",
        "gate_position": "before_image_decode_and_estimator_feed",
        "track_frequency_hz": frequency,
        "track_frequency_source": "canonical_dataset_config",
        "implementation_binding": (
            "system_runtime_source_and_binary_identity_in_sequence_result"
        ),
        "minimum_period_seconds": time_delta,
        "raw_serial_dispatch_count": len(pairs),
        "raw_serial_dispatch_sequence_sha256": _ordered_stereo_callback_digest(
            pairs
        ),
        "raw_adjacent_equal_camera_timestamp_count": raw_equal_adjacent_count,
        "raw_adjacent_reversed_camera_timestamp_count": (
            raw_reversal_adjacent_count
        ),
        "raw_final_camera_timestamp_ns": (
            raw_timestamps_ns[-1] if raw_timestamps_ns else None
        ),
        "raw_maximum_camera_timestamp_ns": (
            max(raw_timestamps_ns) if raw_timestamps_ns else None
        ),
        "accepted_visualizer_callback_count": len(accepted),
        "accepted_visualizer_callback_sequence_sha256": (
            _ordered_stereo_callback_digest(accepted)
        ),
        "accepted_final_camera_timestamp_ns": accepted_timestamps_ns[-1],
        "accepted_maximum_camera_timestamp_ns": max(accepted_timestamps_ns),
        "frequency_dropped_dispatch_count": len(dropped),
        "accepted_camera_timestamps_strictly_increasing": True,
        "dropped_dispatches": dropped,
    }


def native_pair_census(
    bag: Path,
    bag_start: float,
    bag_duration: float,
    track_frequency_hz: float,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    messages, bag_identity, topic_identity = pairing.read_bag(
        bag, CAMERA0_TOPIC, CAMERA1_TOPIC, IMU_TOPIC
    )
    try:
        import rosbag  # type: ignore
    except ImportError as exc:
        raise TrialError("ROS1 rosbag Python bindings are unavailable") from exc
    with rosbag.Bag(str(bag), "r") as opened:
        full_bounds = rosbag_view_full_bounds(opened)
    try:
        rosbag_module_path = Path(rosbag.bag.__file__)  # type: ignore[attr-defined]
    except (AttributeError, TypeError) as exc:
        raise TrialError("cannot identify the pinned Python rosbag.bag module") from exc
    rosbag_module_identity = file_identity(rosbag_module_path)
    if Path(rosbag_module_identity["path"]) != ROSBAG_INDEX_MODULE:
        raise TrialError("Python rosbag.bag module path drift")
    if (
        rosbag_module_identity["size_bytes"] != ROSBAG_INDEX_MODULE_SIZE
        or rosbag_module_identity["sha256"] != ROSBAG_INDEX_MODULE_SHA256
    ):
        raise TrialError("Python rosbag.bag module identity drift")
    rosbag_index_reader = {
        "api": "rosbag.bag.Bag._get_indexes(None)",
        "module": rosbag_module_identity,
        "frozen_module_identity_match": True,
    }
    selected_messages, native, view_start_ns, view_end_ns = (
        project_native_bag_view_exact(
            messages,
            int(full_bounds["first_record_timestamp_ns"]),
            int(full_bounds["last_record_timestamp_ns"]),
            bag_start,
            bag_duration,
        )
    )
    accepted_pairs, visualizer_gate = apply_visualizer_track_frequency_gate(
        native.pairs, track_frequency_hz
    )
    timestamps_ns = [pair.camera_timestamp_ns for pair in accepted_pairs]
    reused = {
        index: count for index, count in native.candidate_use_counts.items() if count > 1
    }
    gaps_over_threshold = [
        {
            "start_timestamp_s": pairing._cpp_ros_time_to_sec(left),
            "end_timestamp_s": pairing._cpp_ros_time_to_sec(right),
            "duration_s": (
                pairing._cpp_ros_time_to_sec(right)
                - pairing._cpp_ros_time_to_sec(left)
            ),
        }
        for left, right in zip(timestamps_ns, timestamps_ns[1:])
        if (
            pairing._cpp_ros_time_to_sec(right)
            - pairing._cpp_ros_time_to_sec(left)
            > MAXIMUM_STATE_GAP_SECONDS
        )
    ]
    interval = {
        "source": (
            "native_record_time_stereo_dispatch_plus_"
            "stock_visualizer_frequency_gate"
        ),
        "first_selected_input_timestamp_s": pairing._cpp_ros_time_to_sec(
            timestamps_ns[0]
        ),
        "last_selected_input_timestamp_s": pairing._cpp_ros_time_to_sec(
            timestamps_ns[-1]
        ),
        "first_selected_input_timestamp_ns": timestamps_ns[0],
        "last_selected_input_timestamp_ns": timestamps_ns[-1],
        "selected_pair_count": len(accepted_pairs),
        "raw_serial_dispatch_pair_count": len(native.pairs),
        "visualizer_frequency_dropped_pair_count": (
            visualizer_gate["frequency_dropped_dispatch_count"]
        ),
        "bag_view_bounds_source": full_bounds["source"],
        "bag_full_index_start_record_timestamp_ns": full_bounds[
            "first_record_timestamp_ns"
        ],
        "bag_full_index_end_record_timestamp_ns": full_bounds[
            "last_record_timestamp_ns"
        ],
        "bag_full_index_start_record_timestamp_s": full_bounds[
            "first_record_timestamp_s"
        ],
        "bag_full_index_end_record_timestamp_s": full_bounds[
            "last_record_timestamp_s"
        ],
        "bag_public_start_record_timestamp_s": full_bounds[
            "python_public_start_record_timestamp_s"
        ],
        "bag_public_end_record_timestamp_s": full_bounds[
            "python_public_end_record_timestamp_s"
        ],
        "bag_public_start_minus_full_index_start_s": full_bounds[
            "python_public_start_minus_index_start_s"
        ],
        "bag_public_end_minus_full_index_end_s": full_bounds[
            "python_public_end_minus_index_end_s"
        ],
        "bag_public_bounds_match_full_index_extrema": full_bounds[
            "python_public_bounds_match_index_extrema"
        ],
        "bag_view_start_record_timestamp_ns": view_start_ns,
        "bag_view_end_record_timestamp_ns": view_end_ns,
        "bag_view_start_record_timestamp_s": pairing._cpp_ros_time_to_sec(
            view_start_ns
        ),
        "bag_view_end_record_timestamp_s": pairing._cpp_ros_time_to_sec(view_end_ns),
        "gaps_over_threshold": gaps_over_threshold,
    }
    census = {
        "schema": NATIVE_CENSUS_SCHEMA,
        "topics": {
            "camera0": CAMERA0_TOPIC,
            "camera1": CAMERA1_TOPIC,
            "imu": IMU_TOPIC,
        },
        "bag": bag_identity,
        "rosbag_index_reader": rosbag_index_reader,
        "visualizer_track_frequency_gate": visualizer_gate,
        "topic_identity": topic_identity,
        "input_interval": interval,
        "diagnostics": {
            "filtered_message_count": len(selected_messages),
            "all_topic_index_connection_count": full_bounds["connection_count"],
            "all_topic_index_entry_count": full_bounds["entry_count"],
            "used_index_outer_skip_count": native.used_index_outer_skip_count,
            "no_pair_outer_skip_count": native.no_pair_outer_skip_count,
            "reused_candidate_message_count": len(reused),
            "candidate_reuse_occurrence_count": sum(count - 1 for count in reused.values()),
            "residual_used_index_count": len(native.residual_used_indices),
            "raw_dispatch_adjacent_equal_camera_timestamp_count": (
                visualizer_gate["raw_adjacent_equal_camera_timestamp_count"]
            ),
            "raw_dispatch_adjacent_reversed_camera_timestamp_count": (
                visualizer_gate["raw_adjacent_reversed_camera_timestamp_count"]
            ),
            "visualizer_accepted_callback_count": len(accepted_pairs),
            "visualizer_frequency_dropped_dispatch_count": (
                visualizer_gate["frequency_dropped_dispatch_count"]
            ),
        },
    }
    return census, _identity_from_census(bag, bag_identity)


def validate_numeric_table(path: Path, minimum_columns: int) -> Dict[str, Any]:
    if not path.is_file():
        raise TrialError("missing numeric output: {}".format(path))
    rows = 0
    columns: Optional[int] = None
    first: Optional[float] = None
    last: Optional[float] = None
    previous: Optional[float] = None
    maximum_gap = 0.0
    gaps_over_threshold: List[Dict[str, float]] = []
    timestamp_digest = hashlib.sha256()
    with path.open(encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                values = [float(token) for token in stripped.replace(",", " ").split()]
            except ValueError as exc:
                raise TrialError("nonnumeric row {}:{}".format(path, line_number)) from exc
            if len(values) < minimum_columns:
                raise TrialError("too few columns at {}:{}".format(path, line_number))
            if not all(math.isfinite(value) for value in values):
                raise TrialError("nonfinite value at {}:{}".format(path, line_number))
            if columns is None:
                columns = len(values)
            elif columns != len(values):
                raise TrialError("ragged numeric table: {}".format(path))
            timestamp = values[0]
            if previous is not None and timestamp <= previous:
                raise TrialError("timestamps are not strictly increasing: {}".format(path))
            if previous is not None:
                maximum_gap = max(maximum_gap, timestamp - previous)
                if timestamp - previous > MAXIMUM_STATE_GAP_SECONDS:
                    gaps_over_threshold.append(
                        {
                            "start_timestamp_s": previous,
                            "end_timestamp_s": timestamp,
                            "duration_s": timestamp - previous,
                        }
                    )
            timestamp_digest.update(struct.pack("<d", timestamp))
            previous = timestamp
            if first is None:
                first = timestamp
            last = timestamp
            rows += 1
    if rows == 0 or columns is None or first is None or last is None:
        raise NoRowsError("numeric output has no rows: {}".format(path))
    identity = file_identity(path)
    identity.update(
        {
            "rows": rows,
            "columns": columns,
            "first_timestamp_s": first,
            "last_timestamp_s": last,
            "maximum_timestamp_gap_s": maximum_gap,
            "gaps_over_threshold": gaps_over_threshold,
            "timestamp_sequence_sha256": timestamp_digest.hexdigest(),
            "finite": True,
            "timestamps_strictly_increasing": True,
        }
    )
    return identity


def assess_output_coverage(
    input_interval: Mapping[str, Any], state: Mapping[str, Any]
) -> Dict[str, Any]:
    selected_start = float(input_interval["first_selected_input_timestamp_s"])
    selected_end = float(input_interval["last_selected_input_timestamp_s"])
    first_state = float(state["first_timestamp_s"])
    last_state = float(state["last_timestamp_s"])
    input_span = selected_end - selected_start
    if not math.isfinite(input_span) or input_span <= 0:
        raise TrialError("selected input interval is empty")
    initialization_delay = first_state - selected_start
    initialization_fraction = initialization_delay / input_span
    tail_gap = selected_end - last_state
    maximum_gap = float(state["maximum_timestamp_gap_s"])
    # Initialization latency is descriptive only. The user explicitly chose
    # post-initialization passage robustness, so late initialization cannot
    # fail passage completion or suppress common-population evaluation.
    initialization_delay_legacy_pass = bool(
        initialization_delay >= -NEGATIVE_TAIL_TOLERANCE_SECONDS
        and initialization_fraction <= INITIALIZATION_DELAY_MAX_FRACTION
    )
    input_gaps = input_interval.get("gaps_over_threshold", [])
    if not isinstance(input_gaps, list):
        raise TrialError("input gap census is not a list")
    supported = 0
    used_input_gap_indices = set()
    unsupported_state_gaps: List[Mapping[str, Any]] = []
    for state_gap in state.get("gaps_over_threshold", []):
        matched = False
        for input_gap_index, input_gap in enumerate(input_gaps):
            if input_gap_index in used_input_gap_indices:
                continue
            start_shift = float(state_gap["start_timestamp_s"]) - float(
                input_gap["start_timestamp_s"]
            )
            end_shift = float(state_gap["end_timestamp_s"]) - float(
                input_gap["end_timestamp_s"]
            )
            duration_delta = float(state_gap["duration_s"]) - float(
                input_gap["duration_s"]
            )
            if (
                abs(start_shift) <= TAIL_GAP_MAX_SECONDS
                and abs(end_shift) <= TAIL_GAP_MAX_SECONDS
                and abs(duration_delta) <= 1.0e-6
                and abs(start_shift - end_shift) <= 1.0e-6
            ):
                matched = True
                used_input_gap_indices.add(input_gap_index)
                break
        if matched:
            supported += 1
        else:
            unsupported_state_gaps.append(state_gap)
    tail_gap_pass = bool(
        -NEGATIVE_TAIL_TOLERANCE_SECONDS <= tail_gap <= TAIL_GAP_MAX_SECONDS
    )
    continuity_pass = not unsupported_state_gaps
    return {
        "first_selected_input_timestamp_s": selected_start,
        "last_selected_input_timestamp_s": selected_end,
        "selected_input_span_s": input_span,
        "first_state_timestamp_s": first_state,
        "last_state_timestamp_s": last_state,
        "initialization_delay_s": initialization_delay,
        "initialization_delay_fraction": initialization_fraction,
        "initialization_delay_max_fraction": INITIALIZATION_DELAY_MAX_FRACTION,
        "initialization_delay_pass": initialization_delay_legacy_pass,
        "initialization_delay_is_descriptive_only": True,
        "maximum_state_gap_s": maximum_gap,
        "maximum_state_gap_allowed_s": MAXIMUM_STATE_GAP_SECONDS,
        "tail_gap_s": tail_gap,
        "tail_gap_max_s": TAIL_GAP_MAX_SECONDS,
        "negative_tail_tolerance_s": NEGATIVE_TAIL_TOLERANCE_SECONDS,
        "input_gap_count": len(input_gaps),
        "state_gap_count": len(state.get("gaps_over_threshold", [])),
        "supported_input_gap_count": supported,
        "unsupported_state_gaps": unsupported_state_gaps,
        "maximum_state_gap_pass": continuity_pass,
        "tail_gap_pass": tail_gap_pass,
        "pass": continuity_pass and tail_gap_pass,
    }


def passage_record(
    input_interval: Mapping[str, Any],
    state: Optional[Mapping[str, Any]],
    completion: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Return the stable post-initialization passage summary for aggregation."""

    del input_interval  # Its selected bounds are already materialized in completion.
    if state is None or completion is None:
        return {
            "eligible": False,
            "complete": False,
            "initialization_timestamp_s": None,
            "initialization_delay_s": None,
            "final_state_timestamp_s": None,
            "tail_gap_s": None,
            "max_state_gap_s": None,
            "supported_input_gap_count": 0,
            "reason": "NO_VALID_STATE_OUTPUT",
        }
    if not completion.get("tail_gap_pass"):
        reason = "FINAL_STATE_DOES_NOT_REACH_SELECTED_INPUT_TAIL"
    elif not completion.get("maximum_state_gap_pass"):
        reason = "UNSUPPORTED_POST_INITIALIZATION_STATE_GAP"
    else:
        reason = "COMPLETE_POST_INITIALIZATION_PASSAGE"
    return {
        "eligible": True,
        "complete": bool(completion.get("pass")),
        "initialization_timestamp_s": float(state["first_timestamp_s"]),
        "initialization_delay_s": float(completion["initialization_delay_s"]),
        "final_state_timestamp_s": float(state["last_timestamp_s"]),
        "tail_gap_s": float(completion["tail_gap_s"]),
        "max_state_gap_s": float(state["maximum_timestamp_gap_s"]),
        "supported_input_gap_count": int(completion["supported_input_gap_count"]),
        "reason": reason,
    }


def parse_console(path: Path) -> Dict[str, Any]:
    text = ANSI_RE.sub("", path.read_text(encoding="utf-8", errors="replace"))
    child_exits = [int(value) for value in CHILD_EXIT_RE.findall(text)]
    child_starts = [
        {"name": name, "pid": int(pid)} for name, pid in CHILD_START_RE.findall(text)
    ]
    recovery_lines = [line for line in text.splitlines() if RECOVERY_PREFIX in line]
    decode_failure_lines = [
        line for line in text.splitlines() if DECODE_FAILURE_RE.search(line)
    ]
    return {
        "child_starts": child_starts,
        "child_exit_codes": child_exits,
        "teardown_exception_class_pattern": bool(TEARDOWN_CLASS_RE.search(text)),
        "teardown_compressed_depth_library_pattern": bool(TEARDOWN_LIBRARY_RE.search(text)),
        "post_coverage_teardown_pattern": bool(
            TEARDOWN_CLASS_RE.search(text) and TEARDOWN_LIBRARY_RE.search(text)
        ),
        "nonfinite_pattern": bool(NONFINITE_RE.search(text)),
        "reset_pattern": bool(RESET_RE.search(text)),
        "covariance_failure_pattern": bool(COVARIANCE_FAILURE_RE.search(text)),
        "image_decode_failure_pattern": bool(decode_failure_lines),
        "image_decode_failure_count": len(decode_failure_lines),
        "image_decode_failure_lines": decode_failure_lines,
        "recovery_event_line_count": len(recovery_lines),
        "recovery_event_lines": recovery_lines,
    }


def classify_outcome(facts: Mapping[str, Any]) -> str:
    """Classify estimator science separately from wrapper and teardown quirks."""

    if facts.get("interrupted"):
        return "INTERRUPTED"
    if facts.get("timed_out"):
        return "TIMED_OUT"
    if not facts.get("teardown_ok", True):
        return "TEARDOWN_FAILED"
    if not facts.get("runtime_contract_valid", False):
        return "INFRASTRUCTURE_FAILED"
    if not facts.get("numeric_integrity_valid", True):
        return "NUMERIC_FAILURE"
    state_kind = facts.get("state_kind")
    abnormal = bool(facts.get("launch_abnormal"))
    if state_kind in (None, "missing", "empty"):
        return "ESTIMATOR_CRASH" if abnormal else "NO_INITIALIZATION"
    if state_kind == "invalid":
        return "INVALID_OUTPUT"
    if not facts.get("outputs_valid", False):
        return "PARTIAL" if abnormal else "INVALID_OUTPUT"
    if not facts.get("input_decode_valid", True):
        return "TRACKING_LOSS"
    if not facts.get("continuity_pass", False):
        return "TRACKING_LOSS"
    if not facts.get("tail_pass", False):
        return "PARTIAL"
    exact_u0_teardown = bool(facts.get("exact_u0_teardown"))
    if abnormal and not exact_u0_teardown:
        return "PARTIAL"
    if facts.get("mode") == "capture":
        if not facts.get("capture_closed", False):
            return "CAPTURE_INCOMPLETE"
        if not facts.get("linkage_valid", False):
            return "INVALID_LINKAGE"
    return "COMPLETED_WITH_TEARDOWN_DEFECT" if exact_u0_teardown else "COMPLETED"


def classify_evidence_validity(
    status: str,
    runtime_contract_valid: bool,
    runtime_inputs_unchanged: Optional[bool],
    teardown_ok: bool,
    mode: Optional[str] = None,
    capture_closed: Optional[bool] = None,
    linkage_valid: Optional[bool] = None,
) -> str:
    """Classify evidence integrity independently from the estimator outcome."""

    if status == "INVALID_LINKAGE" or (
        mode == "capture" and linkage_valid is False
    ):
        return "CAPTURE_LINK_INVALID"
    if runtime_inputs_unchanged is False:
        return "INVALID_PROVENANCE"
    if (
        runtime_inputs_unchanged is not True
        or not runtime_contract_valid
        or not teardown_ok
        or (mode == "capture" and capture_closed is not True)
    ):
        return "INVALID_INFRA"
    return "VALID"


def _artifact_record(run_dir: Path, relative: str) -> Dict[str, Any]:
    path = run_dir / relative
    record: Dict[str, Any] = {"relative_path": relative, "identity": None}
    if path.is_file():
        record["identity"] = file_identity(path)
    return record


def _initial_artifacts() -> Dict[str, Any]:
    return {
        "state": {"relative_path": "trajectory/state_estimate.txt", "identity": None},
        "deviation": {
            "relative_path": "trajectory/state_deviation.txt",
            "identity": None,
        },
        "tum": {"relative_path": "trajectory/estimate_raw.tum", "identity": None},
        "timing": {"relative_path": "diagnostics/timing_openvins.csv", "identity": None},
        "raw_geometry": {
            "relative_path": "geometry/feature_stream.bag",
            "identity": None,
            "active_identity": None,
            "topics": list(GEOMETRY_TOPICS),
        },
    }


def refresh_artifacts(run_dir: Path) -> Dict[str, Any]:
    artifacts = _initial_artifacts()
    for name, relative in (
        ("state", "trajectory/state_estimate.txt"),
        ("deviation", "trajectory/state_deviation.txt"),
        ("tum", "trajectory/estimate_raw.tum"),
        ("timing", "diagnostics/timing_openvins.csv"),
    ):
        artifacts[name] = _artifact_record(run_dir, relative)
    raw = run_dir / "geometry" / "feature_stream.bag"
    active = run_dir / "geometry" / "feature_stream.bag.active"
    if raw.is_file():
        artifacts["raw_geometry"]["identity"] = file_identity(raw)
    if active.is_file():
        artifacts["raw_geometry"]["active_identity"] = file_identity(active)
    return artifacts


def _git_runtime_identity(package_path: Path) -> Dict[str, Any]:
    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(package_path), *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if completed.returncode != 0:
            raise TrialError(
                "git runtime identity failed: {}".format(completed.stderr.strip())
            )
        return completed.stdout.strip()

    root = Path(git("rev-parse", "--show-toplevel")).resolve(strict=True)
    status = git("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "repository": str(root),
        "head_sha": git("rev-parse", "HEAD"),
        "head_tree": git("rev-parse", "HEAD^{tree}"),
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
    }


def _validate_s1_compiled_snapshot() -> Dict[str, Any]:
    configure_identity = file_identity(
        _regular_file(S1_CONFIGURE_PROVENANCE, "S1 configure provenance")
    )
    if configure_identity["sha256"] != S1_CONFIGURE_PROVENANCE_SHA256:
        raise TrialError("S1 configure provenance SHA-256 drift")
    try:
        value = json.loads(S1_CONFIGURE_PROVENANCE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialError("S1 configure provenance is invalid JSON") from exc
    snapshot = value.get("source_snapshot")
    if not isinstance(snapshot, dict):
        raise TrialError("S1 configure provenance lacks source snapshot")
    if snapshot.get("head_sha") != S1_SOURCE_COMMIT:
        raise TrialError("S1 compiled source commit drift")
    if snapshot.get("head_tree") != S1_SOURCE_TREE:
        raise TrialError("S1 compiled source tree drift")
    expected_aggregate = "9972e5a6a8daafa05120f0e0306cf262a589a6fe33a4cd726be821ac75a16790"
    if snapshot.get("aggregate_source_snapshot_sha256") != expected_aggregate:
        raise TrialError("S1 compiled source aggregate drift")
    records: List[Mapping[str, Any]] = []
    for key in ("compiled_inputs", "untracked_compiled_inputs"):
        group = snapshot.get(key)
        if not isinstance(group, list):
            raise TrialError("S1 source snapshot {} is not a list".format(key))
        if not all(isinstance(record, dict) for record in group):
            raise TrialError("S1 source snapshot contains a non-object record")
        records.extend(group)
    observed_paths = set()
    for record in records:
        relative_text = str(record.get("path", ""))
        relative = Path(relative_text)
        if not relative_text or relative.is_absolute() or ".." in relative.parts:
            raise TrialError("unsafe S1 compiled-input path")
        if relative_text in observed_paths:
            raise TrialError("duplicate S1 compiled-input path")
        observed_paths.add(relative_text)
        identity = file_identity(_regular_file(REPO_ROOT / relative, "S1 compiled input"))
        if identity["size_bytes"] != record.get("size_bytes"):
            raise TrialError("S1 compiled-input size drift: {}".format(relative_text))
        if identity["sha256"] != record.get("sha256"):
            raise TrialError("S1 compiled-input SHA-256 drift: {}".format(relative_text))
    return {
        "configure_provenance": configure_identity,
        "source_commit": S1_SOURCE_COMMIT,
        "source_tree": S1_SOURCE_TREE,
        "aggregate_source_snapshot_sha256": expected_aggregate,
        "compiled_input_count": len(records),
        "all_compiled_inputs_match_live_bytes": True,
    }


def resolve_runtime_identity(
    system: str,
    binary: Path,
    run_dir: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    commands: MutableMapping[str, Any],
) -> Dict[str, Any]:
    tools = {name: _which(name) for name in ("catkin_find", "rospack")}
    catkin_log = run_dir / "diagnostics" / "resolved_estimator_binary.txt"
    record = run_command(
        [
            str(tools["catkin_find"]),
            "--first-only",
            "--without-underlays",
            "--libexec",
            "ov_msckf",
            "ros1_serial_msckf",
        ],
        catkin_log,
        environment,
        min(timeout_seconds, 120.0),
    )
    commands["resolve_estimator_binary"] = record
    if not command_succeeded(record):
        raise TrialError("catkin_find could not resolve the estimator binary")
    lines = [line.strip() for line in catkin_log.read_text().splitlines() if line.strip()]
    if len(lines) != 1 or Path(lines[0]).resolve(strict=False) != binary:
        raise TrialError(
            "runtime package resolves {}, not declared binary {}".format(lines, binary)
        )

    package_log = run_dir / "diagnostics" / "resolved_ov_msckf_package.txt"
    record = run_command(
        [str(tools["rospack"]), "find", "ov_msckf"],
        package_log,
        environment,
        min(timeout_seconds, 120.0),
    )
    commands["resolve_ov_msckf_package"] = record
    if not command_succeeded(record):
        raise TrialError("rospack could not resolve ov_msckf")
    package_lines = [
        line.strip() for line in package_log.read_text().splitlines() if line.strip()
    ]
    if len(package_lines) != 1:
        raise TrialError("rospack returned an ambiguous package path")
    package_path = Path(package_lines[0]).resolve(strict=True)
    expected_package = U0_PACKAGE_ROOT if system == "U0" else REPO_ROOT / "ov_msckf"
    expected_package = expected_package.resolve(strict=True)
    if package_path != expected_package:
        raise TrialError(
            "sourced runtime package root differs: {} != {}".format(
                package_path, expected_package
            )
        )
    try:
        if system == "U0":
            pinned = pinned_runtime.validate_runtime_identity(
                "U0", environment=environment
            )
        else:
            pinned = pinned_runtime.validate_runtime_identity(
                "S1", environment=environment, policy=S1_RUNTIME_POLICY
            )
    except (OSError, subprocess.SubprocessError, pinned_runtime.RuntimeIdentityError) as exc:
        raise TrialError("pinned runtime identity validation failed: {}".format(exc)) from exc
    if Path(pinned["executable"]["canonical_path"]) != binary:
        raise TrialError("pinned runtime executable differs from declared binary")
    source = _git_runtime_identity(package_path)
    if system == "U0":
        if Path(source["repository"]) != U0_SOURCE_ROOT.resolve(strict=True):
            raise TrialError("U0 package does not belong to the pinned upstream clone")
        if source["head_sha"] != U0_SOURCE_COMMIT or source["head_tree"] != U0_SOURCE_TREE:
            raise TrialError("U0 source commit/tree drift")
        if source["dirty"]:
            raise TrialError("U0 source clone is dirty")
        compiled_snapshot = None
    else:
        if Path(source["repository"]) != REPO_ROOT.resolve(strict=True):
            raise TrialError("S1 package does not belong to the current repository")
        compiled_snapshot = _validate_s1_compiled_snapshot()
    evidence = {
        "system": system,
        "declared_binary": file_identity(binary),
        "catkin_resolved_binary": str(binary),
        "catkin_binary_matches_declared": True,
        "package_path": str(package_path),
        "source": source,
        "compiled_source_snapshot": compiled_snapshot,
        "pinned_runtime": pinned,
        "tools": {name: file_identity(path) for name, path in tools.items()},
    }
    evidence_path = run_dir / "diagnostics" / "pinned_runtime_identity.json"
    atomic_write_new_json(evidence_path, evidence)
    evidence["artifact"] = file_identity(evidence_path)
    return evidence


def _start_roscore(
    run_dir: Path, environment: Mapping[str, str], ros_port: int
) -> ManagedProcess:
    roscore = start_managed_process(
        "roscore",
        [str(_which("roscore")), "-p", str(ros_port)],
        run_dir / "diagnostics" / "roscore.log",
        environment,
    )
    try:
        _wait_for_port(ros_port, True)
        return roscore
    except BaseException:
        finish_managed_process(roscore)
        raise


def _start_geometry_recorder(
    run_dir: Path, environment: Mapping[str, str]
) -> ManagedProcess:
    recorder = start_managed_process(
        "geometry_recorder",
        [
            str(_which("rosbag")),
            "record",
            "--buffsize=0",
            "--chunksize=768",
            "-O",
            str(run_dir / "geometry" / "feature_stream.bag"),
            *GEOMETRY_TOPICS,
            "__name:=icra27_cross_dataset_geometry_recorder",
        ],
        run_dir / "diagnostics" / "geometry_recorder.log",
        environment,
    )
    try:
        deadline = time.monotonic() + 15.0
        rosnode = _which("rosnode")
        while time.monotonic() < deadline:
            if recorder.process.poll() is not None:
                raise TrialError("geometry recorder exited during startup")
            probe = subprocess.run(
                [
                    str(rosnode),
                    "info",
                    "/icra27_cross_dataset_geometry_recorder",
                ],
                env=dict(environment),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                return recorder
            time.sleep(0.1)
        raise TrialError("geometry recorder did not become ready")
    except BaseException:
        finish_managed_process(recorder)
        raise


def _validate_identity_at_path(record: Mapping[str, Any]) -> Dict[str, Any]:
    identity = record.get("identity")
    if not isinstance(identity, dict):
        raise TrialError("linked scored artifact has no identity")
    path = Path(str(identity.get("path", "")))
    observed = file_identity(_regular_file(path, "linked scored artifact"))
    if observed != identity:
        raise TrialError("linked scored artifact identity changed: {}".format(path))
    return observed


def load_scored_linkage(
    path: Path, protocol_id: str, dataset: str, sequence: str, system: str
) -> Dict[str, Any]:
    result_path = _regular_file(path, "scored sequence result")
    try:
        value = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialError("linked scored result is invalid JSON") from exc
    expected = {
        "schema": SCHEMA,
        "protocol_id": protocol_id,
        "dataset": dataset,
        "sequence": sequence,
        "system": system,
        "mode": "scored",
    }
    differences = {
        key: {"observed": value.get(key), "expected": wanted}
        for key, wanted in expected.items()
        if value.get(key) != wanted
    }
    if differences:
        raise TrialError("linked scored result identity differs: {}".format(differences))
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise TrialError("linked scored result lacks artifacts")
    validated: Dict[str, Any] = {}
    for name in ("state", "deviation", "tum"):
        record = artifacts.get(name)
        if not isinstance(record, dict):
            raise TrialError("linked scored result lacks {} artifact record".format(name))
        if record.get("identity") is not None:
            validated[name] = _validate_identity_at_path(record)
        else:
            validated[name] = None
    return {
        "status": "PENDING_CAPTURE_REPLAY",
        "sequence_result": file_identity(result_path),
        "sequence_result_path": str(result_path),
        "scored_status": value.get("status"),
        "scored_accuracy_eligible": bool(value.get("accuracy_eligible")),
        "expected_artifacts": validated,
    }


def assess_capture_linkage(
    linkage: MutableMapping[str, Any], artifacts: Mapping[str, Any]
) -> bool:
    comparisons: Dict[str, Any] = {}
    valid = True
    source_unchanged = True
    post_capture_expected: Dict[str, Any] = {}
    for name in ("state", "deviation", "tum"):
        expected = linkage.get("expected_artifacts", {}).get(name)
        observed_record = artifacts.get(name, {})
        observed = observed_record.get("identity") if isinstance(observed_record, dict) else None
        if isinstance(expected, dict):
            live_expected = file_identity(
                _regular_file(Path(expected["path"]), "linked scored artifact")
            )
            expected_stable = _same_file_identity(expected, live_expected)
            post_capture_expected[name] = live_expected
        else:
            live_expected = None
            expected_stable = expected is None
            post_capture_expected[name] = None
        source_unchanged = source_unchanged and expected_stable
        # Capture outputs live in a different append-only directory.  Content
        # linkage therefore compares bytes, not provenance-only path/mtime.
        exact = bool(
            (live_expected is None and observed is None)
            or (
                isinstance(live_expected, dict)
                and isinstance(observed, dict)
                and live_expected.get("size_bytes") == observed.get("size_bytes")
                and live_expected.get("sha256") == observed.get("sha256")
            )
        )
        comparisons[name] = {
            "expected_sha256": (
                live_expected.get("sha256") if isinstance(live_expected, dict) else None
            ),
            "observed_sha256": observed.get("sha256") if isinstance(observed, dict) else None,
            "expected_source_unchanged_during_capture": expected_stable,
            "byte_exact": exact,
        }
        valid = valid and exact
    result_path = _regular_file(
        Path(str(linkage["sequence_result_path"])), "linked scored sequence result"
    )
    result_after = file_identity(result_path)
    result_unchanged = _same_file_identity(linkage["sequence_result"], result_after)
    source_unchanged = source_unchanged and result_unchanged
    linkage["artifact_comparisons"] = comparisons
    linkage["post_capture_expected_artifacts"] = post_capture_expected
    linkage["sequence_result_after"] = result_after
    linkage["source_unchanged_during_capture"] = source_unchanged
    linkage["status"] = "LINKED_EXACT" if valid and source_unchanged else "OUTPUT_MISMATCH"
    linkage["byte_exact"] = valid and source_unchanged
    return valid and source_unchanged


def _same_file_identity(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    return all(before.get(key) == after.get(key) for key in ("path", "size_bytes", "sha256"))


def _record_error(result: MutableMapping[str, Any], stage: str, exc: BaseException) -> None:
    result.setdefault("stage_errors", []).append(
        {"stage": stage, "type": type(exc).__name__, "message": str(exc)}
    )


def _validate_request(args: argparse.Namespace) -> None:
    for value, label in (
        (args.protocol_id, "protocol ID"),
        (args.sequence, "sequence ID"),
        (args.run_id, "run ID"),
    ):
        if SAFE_ID_RE.fullmatch(value) is None:
            raise TrialError("{} must match {}".format(label, SAFE_ID_RE.pattern))
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise TrialError("timeout must be finite and positive")
    if not math.isfinite(args.bag_start) or args.bag_start < 0:
        raise TrialError("bag start must be finite and nonnegative")
    if not math.isfinite(args.bag_duration) or args.bag_duration == 0:
        raise TrialError("bag duration must be finite and nonzero")
    if args.attempt_index < 1:
        raise TrialError("attempt index must be positive")
    if args.mode == "capture" and args.scored_result is None:
        raise TrialError("capture mode requires --scored-result")
    if args.mode == "scored" and args.scored_result is not None:
        raise TrialError("scored mode forbids --scored-result")
    if perturbation_requested(args):
        perturbation_record(args)
    elif args.system not in SYSTEMS:
        raise TrialError("system {} requires the PERTURB-1 perturbation mode".format(args.system))


def perturbation_requested(args: argparse.Namespace) -> bool:
    return getattr(args, "perturbation_campaign_id", None) is not None


def perturbation_record(args: argparse.Namespace) -> Dict[str, Any]:
    """Validate and describe one PERTURB-1 request; raise TrialError if unrunnable.

    ``args.bag_start`` keeps the frozen CDSC-1R4 matrix start (matrix binding);
    the estimator receives ``frozen + offset_frames / frame_rate_hz``.  A
    negative shifted start has no lead-in data and is rejected.
    """

    campaign_id = getattr(args, "perturbation_campaign_id", None)
    if campaign_id != PERTURBATION_CAMPAIGN_ID:
        raise TrialError("unknown perturbation campaign id: {}".format(campaign_id))
    offset_frames = getattr(args, "perturbation_offset_frames", None)
    frame_rate_hz = getattr(args, "perturbation_frame_rate_hz", None)
    seed_label = getattr(args, "perturbation_seed_label", None)
    if isinstance(offset_frames, bool) or not isinstance(offset_frames, int):
        raise TrialError("perturbation offset must be an integer frame count")
    if (
        isinstance(frame_rate_hz, bool)
        or not isinstance(frame_rate_hz, (int, float))
        or not math.isfinite(float(frame_rate_hz))
        or float(frame_rate_hz) <= 0.0
    ):
        raise TrialError("perturbation frame rate must be finite and positive")
    if seed_label not in PERTURBATION_SEED_LABELS:
        raise TrialError(
            "perturbation seed label {!r} is not runnable: the frozen estimator has no "
            "runtime RNG seed parameter (cv::setRNGSeed(0) is compiled in)".format(seed_label)
        )
    if args.system not in PERTURBATION_SYSTEMS:
        raise TrialError("unknown perturbation system: {}".format(args.system))
    frozen_start = float(args.bag_start)
    shift_seconds = float(offset_frames) / float(frame_rate_hz)
    estimator_start = frozen_start + shift_seconds
    if not math.isfinite(estimator_start) or estimator_start < 0.0:
        raise TrialError(
            "NOT_RUNNABLE_NEGATIVE_OFFSET: shifted start {!r} s precedes the bag begin; "
            "no lead-in data".format(estimator_start)
        )
    return {
        "campaign_id": campaign_id,
        "axis": getattr(args, "perturbation_axis", "offset"),
        "offset_frames": offset_frames,
        "frame_rate_hz": float(frame_rate_hz),
        "shift_seconds": shift_seconds,
        "shift_seconds_repr": repr(shift_seconds),
        "seed_label": seed_label,
        "seed_delta": 0,
        "seed_mechanism": "compiled_constant_cv_setRNGSeed_0_not_a_runtime_parameter",
        "frozen_matrix_bag_start_seconds": frozen_start,
        "estimator_bag_start_seconds": estimator_start,
        "estimator_bag_start_seconds_repr": repr(estimator_start),
        "estimator_bag_start_launch_argument": format(estimator_start, ".17g"),
        "imu_and_camera_share_the_shifted_view": True,
        "system": args.system,
        "landmark_elimination": (
            LANDMARK_ELIMINATION.get(args.system) if args.system in S1_LIKE_SYSTEMS else "upstream_native"
        ),
    }


def run_trial(args: argparse.Namespace) -> Tuple[Dict[str, Any], Path]:
    _validate_request(args)
    perturbation = perturbation_record(args) if perturbation_requested(args) else None
    estimator_bag_start = (
        float(perturbation["estimator_bag_start_seconds"]) if perturbation else args.bag_start
    )
    run_dir = create_run_directory(args.output_root, args.run_id)
    started = time.monotonic()
    result: Dict[str, Any] = {
        "schema": SCHEMA,
        "perturbation": perturbation,
        "protocol_id": args.protocol_id,
        "run_id": args.run_id,
        "attempt_index": args.attempt_index,
        "dataset": args.dataset,
        "sequence": args.sequence,
        "system": args.system,
        "mode": args.mode,
        "status": "ACTIVE",
        "evidence_validity": "INVALID_INFRA",
        "accuracy_eligible": False,
        "qualitative_eligible": False,
        "strict_process_health": False,
        "started_utc": utc_now(),
        "finished_utc": None,
        "duration_seconds": None,
        "run_directory": str(run_dir),
        "input_interval": {
            "source": (
                "native_record_time_stereo_dispatch_plus_"
                "stock_visualizer_frequency_gate"
            ),
            "first_selected_input_timestamp_s": None,
            "last_selected_input_timestamp_s": None,
            "first_selected_input_timestamp_ns": None,
            "last_selected_input_timestamp_ns": None,
            "selected_pair_count": None,
            "raw_serial_dispatch_pair_count": None,
            "visualizer_frequency_dropped_pair_count": None,
            "bag_view_bounds_source": None,
            "bag_full_index_start_record_timestamp_ns": None,
            "bag_full_index_end_record_timestamp_ns": None,
            "bag_full_index_start_record_timestamp_s": None,
            "bag_full_index_end_record_timestamp_s": None,
            "bag_public_start_record_timestamp_s": None,
            "bag_public_end_record_timestamp_s": None,
            "bag_public_start_minus_full_index_start_s": None,
            "bag_public_end_minus_full_index_end_s": None,
            "bag_public_bounds_match_full_index_extrema": None,
            "bag_view_start_record_timestamp_ns": None,
            "bag_view_end_record_timestamp_ns": None,
            "bag_view_start_record_timestamp_s": None,
            "bag_view_end_record_timestamp_s": None,
            "gaps_over_threshold": [],
        },
        "inputs": {
            "ground_truth": None,
            "protocol": None,
            "matrix": None,
            "bag": None,
            "config": None,
            "launch": None,
            "binary": None,
            "runner": None,
            "converter": None,
            "pairing_census_tool": None,
            "runtime_identity_validator": None,
            "config_dependencies": None,
        },
        "artifacts": _initial_artifacts(),
        "runtime_identity": None,
        "resolved_parameters": None,
        "scored_linkage": None,
        "commands": {},
        "checks": {
            "ground_truth_never_opened_by_runner": True,
            "recovery_default_off": False,
            "recovery_runtime_event_count_zero": False,
            "input_decode_failure_count_zero": None,
            "runtime_inputs_unchanged": None,
        },
        "completion": None,
        "passage": passage_record({}, None, None),
        "estimator_close_receipt": {
            "estimator_attempted": False,
            "estimator_process_group_closed": False,
            "runtime_services_closed": False,
            "closed_utc": None,
        },
        "console_classification": None,
        "failure": None,
        "stage_errors": [],
    }

    environment = minimal_environment(run_dir, args.ros_port)
    result["runtime_environment"] = environment
    result["ros_isolation"] = {
        "port": args.ros_port,
        "master_uri": environment["ROS_MASTER_URI"],
        "home": environment["HOME"],
        "ros_home": environment["ROS_HOME"],
        "ros_log_dir": environment["ROS_LOG_DIR"],
    }
    managed: List[ManagedProcess] = []
    teardown_ok = True
    runtime_contract_valid = False
    launch_record: Optional[Dict[str, Any]] = None
    state: Optional[Dict[str, Any]] = None
    state_kind = "missing"
    outputs_valid = False
    coverage_pass = False
    continuity_pass = False
    tail_pass = False
    numeric_integrity_valid = True
    input_decode_valid: Optional[bool] = None
    capture_closed = args.mode == "scored"
    linkage_valid = args.mode == "scored"
    exact_u0_teardown = False
    input_paths: Dict[str, Path] = {}
    input_identities_before: Dict[str, Dict[str, Any]] = {}
    stage = "preflight"

    try:
        assert_port_available(args.ros_port)
        bag = _regular_file(args.bag, "input bag")
        config = _regular_file(args.config, "estimator config")
        launch = _regular_file(args.launch, "cross-dataset launch")
        binary = _regular_file(args.binary, "estimator binary")
        protocol = _regular_file(args.protocol_file, "campaign protocol")
        matrix = _regular_file(args.matrix_file, "campaign matrix")
        runner = _regular_file(Path(__file__).resolve(), "cross-dataset trial runner")
        converter = _regular_file(CONVERTER, "state-to-TUM converter")
        pairing_tool = _regular_file(PAIRING_TOOL, "native pairing census tool")
        runtime_identity_tool = _regular_file(
            RUNTIME_IDENTITY_TOOL, "runtime identity validator"
        )
        input_paths = {
            "bag": bag,
            "config": config,
            "launch": launch,
            "binary": binary,
            "protocol": protocol,
            "matrix": matrix,
            "runner": runner,
            "converter": converter,
            "pairing_census_tool": pairing_tool,
            "runtime_identity_validator": runtime_identity_tool,
        }
        result["inputs"].update(
            {
                "config": file_identity(config),
                "launch": file_identity(launch),
                "binary": file_identity(binary),
                "protocol": file_identity(protocol),
                "matrix": file_identity(matrix),
                "runner": file_identity(runner),
                "converter": file_identity(converter),
                "pairing_census_tool": file_identity(pairing_tool),
                "runtime_identity_validator": file_identity(runtime_identity_tool),
            }
        )
        canonical = validate_canonical_paths_and_hashes(
            args.dataset,
            args.system,
            config,
            launch,
            protocol,
            matrix,
        )
        result["canonical_inputs"] = canonical
        campaign = validate_campaign_bindings(
            args.protocol_id,
            protocol,
            matrix,
            args.dataset,
            args.sequence,
            "S1" if args.system == "N0" else args.system,
            bag,
            args.bag_start,
        )
        result["protocol_identity"] = campaign["protocol"]
        result["matrix_identity"] = campaign["matrix"]
        result["campaign_binding"] = campaign
        launch_contract = validate_launch_contract(args.system, launch)
        config_contract = validate_config_contract(args.system, config)
        result["launch_contract"] = launch_contract
        result["config_contract"] = config_contract
        result["checks"]["recovery_default_off"] = True
        dependencies: Dict[str, Any] = {}
        for group in config_contract["external_configs"].values():
            identity = group["identity"]
            dependencies[identity["path"]] = identity
            input_paths["dependency:" + identity["path"]] = Path(identity["path"])
        for identity in config_contract["masks"].values():
            dependencies[identity["path"]] = identity
            input_paths["dependency:" + identity["path"]] = Path(identity["path"])
        result["inputs"]["config_dependencies"] = dependencies

        stage = "native_pair_census"
        census, bag_identity = native_pair_census(
            bag,
            estimator_bag_start,
            args.bag_duration,
            config_contract["track_frequency_hz"],
        )
        validate_matrix_bag_identity(campaign, bag_identity)
        result["inputs"]["bag"] = bag_identity
        result["input_interval"] = census["input_interval"]
        census_path = run_dir / "diagnostics" / "native_pair_census.json"
        atomic_write_new_json(census_path, census)
        result["native_pair_census"] = file_identity(census_path)

        stage = "resolved_parameters"
        launch_arguments = _launch_arguments(
            launch, config, bag, estimator_bag_start, args.bag_duration, run_dir
        )
        result["estimator_launch_arguments"] = launch_arguments
        dump_path = run_dir / "diagnostics" / "resolved_ros_parameters.yaml"
        dump_record = run_command(
            [str(_which("roslaunch")), "--dump-params", *launch_arguments],
            dump_path,
            environment,
            min(args.timeout_seconds, 120.0),
        )
        result["commands"]["resolve_parameters"] = dump_record
        if not command_succeeded(dump_record):
            raise TrialError("ROS parameter resolution failed")
        resolved = validate_resolved_parameters(
            args.system,
            dump_path.read_text(encoding="utf-8", errors="strict"),
            config,
            bag,
            estimator_bag_start,
            args.bag_duration,
            run_dir,
        )
        resolved["artifact"] = file_identity(dump_path)
        result["resolved_parameters"] = resolved

        stage = "runtime_identity"
        result["runtime_identity"] = resolve_runtime_identity(
            args.system,
            binary,
            run_dir,
            environment,
            args.timeout_seconds,
            result["commands"],
        )

        stage = "cpu_affinity_preflight"
        affinity = run_command(
            ["/usr/bin/taskset", "--cpu-list", args.cpu_list, "/usr/bin/true"],
            run_dir / "diagnostics" / "cpu_affinity_preflight.log",
            environment,
            30.0,
        )
        result["commands"]["cpu_affinity_preflight"] = affinity
        if not command_succeeded(affinity):
            raise TrialError("requested CPU affinity is unavailable")

        if args.mode == "capture":
            stage = "scored_linkage_preflight"
            result["scored_linkage"] = load_scored_linkage(
                args.scored_result,
                args.protocol_id,
                args.dataset,
                args.sequence,
                args.system,
            )

        for name, path in input_paths.items():
            if name == "bag":
                input_identities_before[name] = bag_identity
            elif name.startswith("dependency:"):
                input_identities_before[name] = dependencies[str(path)]
            else:
                input_identities_before[name] = result["inputs"][name]

        runtime_contract_valid = True
        stage = "runtime_services"
        roscore = _start_roscore(run_dir, environment, args.ros_port)
        managed.append(roscore)
        if args.mode == "capture":
            recorder = _start_geometry_recorder(run_dir, environment)
            managed.append(recorder)

        stage = "estimator"
        resource_path = run_dir / "diagnostics" / "resource_usage.txt"
        estimator_argv = [
            "/usr/bin/time",
            "--verbose",
            "--output=" + str(resource_path),
            "--",
            "/usr/bin/taskset",
            "--cpu-list",
            args.cpu_list,
            str(_which("roslaunch")),
            *launch_arguments,
        ]
        result["estimator_argv"] = estimator_argv
        launch_record = run_command(
            estimator_argv,
            run_dir / "diagnostics" / "console.log",
            environment,
            args.timeout_seconds,
        )
        result["commands"]["estimator"] = launch_record
        if resource_path.is_file():
            result["resource_usage"] = file_identity(resource_path)

        stage = "runtime_close"
        for process in reversed(managed):
            close_record = finish_managed_process(process)
            result["commands"][process.name] = close_record
            teardown_ok = teardown_ok and not close_record["process_group_survived_cleanup"]
        managed = []
        result["estimator_close_receipt"].update(
            {
                "estimator_attempted": True,
                "estimator_process_group_closed": not bool(
                    launch_record.get("process_group_survived_cleanup")
                ),
                "runtime_services_closed": teardown_ok,
                "closed_utc": utc_now() if teardown_ok else None,
            }
        )

        stage = "console_classification"
        console = parse_console(run_dir / "diagnostics" / "console.log")
        result["console_classification"] = console
        expected_starts = [
            value for value in console["child_starts"] if value["name"].startswith(NODE_NAME + "-")
        ]
        if len(expected_starts) != 1:
            runtime_contract_valid = False
            _record_error(
                result,
                stage,
                TrialError("roslaunch did not report exactly one expected estimator start"),
            )
        if console["recovery_event_line_count"] != 0:
            runtime_contract_valid = False
            _record_error(
                result,
                stage,
                TrialError("long-gap recovery emitted events in a default-off campaign"),
            )
        result["checks"]["recovery_runtime_event_count_zero"] = (
            console["recovery_event_line_count"] == 0
        )
        numeric_integrity_valid = not any(
            console[name]
            for name in (
                "nonfinite_pattern",
                "reset_pattern",
                "covariance_failure_pattern",
            )
        )
        result["checks"]["numeric_integrity_valid"] = numeric_integrity_valid
        input_decode_valid = not console["image_decode_failure_pattern"]
        result["checks"]["input_decode_failure_count_zero"] = input_decode_valid
        exact_u0_teardown = bool(
            args.system == "U0"
            and console["post_coverage_teardown_pattern"]
            and console["child_exit_codes"] == [-6]
        )

        stage = "output_validation"
        state_path = run_dir / "trajectory" / "state_estimate.txt"
        deviation_path = run_dir / "trajectory" / "state_deviation.txt"
        tum_path = run_dir / "trajectory" / "estimate_raw.tum"
        if not state_path.is_file():
            state_kind = "missing"
        else:
            try:
                state = validate_numeric_table(state_path, 8)
                state_kind = "valid"
            except NoRowsError as exc:
                state_kind = "empty"
                _record_error(result, stage, exc)
            except TrialError as exc:
                state_kind = "invalid"
                _record_error(result, stage, exc)

        deviation: Optional[Dict[str, Any]] = None
        tum: Optional[Dict[str, Any]] = None
        if state is not None:
            try:
                deviation = validate_numeric_table(deviation_path, 8)
            except TrialError as exc:
                _record_error(result, "deviation_validation", exc)
            conversion = run_command(
                [str(PYTHON), str(converter), str(state_path), str(tum_path)],
                run_dir / "diagnostics" / "trajectory_conversion.log",
                environment,
                min(args.timeout_seconds, 300.0),
            )
            result["commands"]["trajectory_conversion"] = conversion
            if command_succeeded(conversion):
                try:
                    tum = validate_numeric_table(tum_path, 8)
                except TrialError as exc:
                    _record_error(result, "tum_validation", exc)
            else:
                _record_error(
                    result,
                    "trajectory_conversion",
                    TrialError("state-to-TUM conversion failed"),
                )
            outputs_valid = bool(
                deviation is not None
                and tum is not None
                and state["rows"] == deviation["rows"] == tum["rows"]
                and state["timestamp_sequence_sha256"]
                == deviation["timestamp_sequence_sha256"]
                == tum["timestamp_sequence_sha256"]
            )
            if not outputs_valid:
                _record_error(
                    result,
                    "output_consistency",
                    TrialError("state, deviation, and TUM row/timestamp identity differs"),
                )
            result["output_validation"] = {
                "state": state,
                "deviation": deviation,
                "tum": tum,
                "consistent": outputs_valid,
            }
            try:
                result["completion"] = assess_output_coverage(result["input_interval"], state)
                coverage_pass = bool(result["completion"]["pass"])
                continuity_pass = bool(result["completion"]["maximum_state_gap_pass"])
                tail_pass = bool(result["completion"]["tail_gap_pass"])
                result["passage"] = passage_record(
                    result["input_interval"], state, result["completion"]
                )
                if input_decode_valid is False:
                    result["completion"]["pass"] = False
                    result["completion"]["input_decode_valid"] = False
                    result["passage"]["complete"] = False
                    result["passage"]["reason"] = "INPUT_DECODE_FAILURE"
                    coverage_pass = False
                    continuity_pass = False
            except TrialError as exc:
                _record_error(result, "completion", exc)

        stage = "capture_validation"
        result["artifacts"] = refresh_artifacts(run_dir)
        if args.mode == "capture":
            raw = result["artifacts"]["raw_geometry"]
            capture_closed = bool(raw["identity"] is not None and raw["identity"]["size_bytes"] > 0)
            if capture_closed:
                info = run_command(
                    [str(_which("rosbag")), "info", "--yaml", raw["identity"]["path"]],
                    run_dir / "diagnostics" / "feature_stream_info.yaml",
                    environment,
                    min(args.timeout_seconds, 300.0),
                )
                result["commands"]["feature_stream_info"] = info
                capture_closed = command_succeeded(info)
            linkage_valid = assess_capture_linkage(
                result["scored_linkage"], result["artifacts"]
            )

        stage = "input_postflight"
        input_identities_after: Dict[str, Any] = {}
        unchanged = True
        for name, path in input_paths.items():
            observed = file_identity(path)
            input_identities_after[name] = observed
            unchanged = unchanged and _same_file_identity(input_identities_before[name], observed)
        result["input_identities_after"] = input_identities_after
        result["checks"]["runtime_inputs_unchanged"] = unchanged
        if not unchanged:
            runtime_contract_valid = False
            _record_error(result, stage, TrialError("runtime input identity changed during trial"))

    except KeyboardInterrupt as exc:
        _record_error(result, stage, exc)
        if launch_record is None:
            launch_record = {"interrupted": True, "timed_out": False, "exit_code": None}
    except BaseException as exc:
        runtime_contract_valid = False
        _record_error(result, stage, exc)
        result["failure"] = {
            "stage": stage,
            "type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        for process in reversed(managed):
            try:
                close_record = finish_managed_process(process)
                result["commands"][process.name] = close_record
                teardown_ok = teardown_ok and not close_record[
                    "process_group_survived_cleanup"
                ]
            except BaseException as exc:
                teardown_ok = False
                _record_error(result, process.name + "_cleanup", exc)
        managed = []
        estimator_group_closed = bool(
            launch_record is not None
            and not launch_record.get("process_group_survived_cleanup", False)
        )
        runtime_services_closed = bool(teardown_ok and not managed)
        result["estimator_close_receipt"] = {
            "estimator_attempted": launch_record is not None,
            "estimator_process_group_closed": estimator_group_closed,
            "runtime_services_closed": runtime_services_closed,
            "closed_utc": (
                utc_now()
                if launch_record is not None
                and estimator_group_closed
                and runtime_services_closed
                else None
            ),
        }
        try:
            result["artifacts"] = refresh_artifacts(run_dir)
        except BaseException as exc:
            _record_error(result, "artifact_refresh", exc)

        console = result.get("console_classification") or {}
        launch_abnormal = bool(
            launch_record is not None
            and (
                launch_record.get("exit_code") not in (0, None)
                or bool(console.get("child_exit_codes"))
            )
        )
        facts = {
            "mode": args.mode,
            "interrupted": bool(launch_record and launch_record.get("interrupted")),
            "timed_out": bool(launch_record and launch_record.get("timed_out")),
            "teardown_ok": teardown_ok
            and not bool(launch_record and launch_record.get("process_group_survived_cleanup")),
            "runtime_contract_valid": runtime_contract_valid,
            "numeric_integrity_valid": numeric_integrity_valid,
            "input_decode_valid": input_decode_valid,
            "state_kind": state_kind,
            "outputs_valid": outputs_valid,
            "continuity_pass": continuity_pass,
            "tail_pass": tail_pass,
            "launch_abnormal": launch_abnormal,
            "exact_u0_teardown": exact_u0_teardown and coverage_pass,
            "capture_closed": capture_closed,
            "linkage_valid": linkage_valid,
        }
        result["outcome_facts"] = facts
        result["status"] = classify_outcome(facts)
        result["evidence_validity"] = classify_evidence_validity(
            result["status"],
            runtime_contract_valid,
            result["checks"].get("runtime_inputs_unchanged"),
            bool(facts["teardown_ok"]),
            mode=args.mode,
            capture_closed=capture_closed,
            linkage_valid=linkage_valid,
        )
        result["strict_process_health"] = bool(
            runtime_contract_valid
            and numeric_integrity_valid
            and input_decode_valid
            and facts["teardown_ok"]
            and not launch_abnormal
            and not facts["timed_out"]
            and not facts["interrupted"]
        )
        result["accuracy_eligible"] = bool(
            args.mode == "scored"
            and result["status"] in ELIGIBLE_STATUSES
            and result["passage"]["complete"]
        )
        result["qualitative_eligible"] = bool(
            args.mode == "capture"
            and result["status"] in ELIGIBLE_STATUSES
            and capture_closed
            and linkage_valid
        )
        result["finished_utc"] = utc_now()
        result["duration_seconds"] = time.monotonic() - started
        result["checks"]["status_known"] = result["status"] in STATUSES
        result["checks"]["teardown_complete"] = facts["teardown_ok"]
        result["checks"]["native_failure_retained"] = result["status"] not in ELIGIBLE_STATUSES
        result["checks"]["ros_latest_symlink_cleanup"] = (
            remove_run_owned_ros_latest_symlink(run_dir)
        )
        if result["failure"] is None and result["status"] not in ELIGIBLE_STATUSES:
            result["failure"] = {
                "stage": "classification",
                "type": result["status"],
                "message": "trial retained with status {}".format(result["status"]),
            }
        result["publication"] = {
            "sequence_result": "sequence_result.json",
            "checksums": "SHA256SUMS",
            "append_only_run_directory": True,
            "overwrite_policy": "exclusive unique run directory and atomic new-file publication",
        }
        atomic_write_new_json(run_dir / "sequence_result.json", result)
        write_sha256sums(run_dir)
    return result, run_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", choices=("run",), help=argparse.SUPPRESS)
    parser.add_argument("--protocol-id", required=True)
    parser.add_argument("--protocol-file", required=True, type=Path)
    parser.add_argument("--matrix-file", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt-index", type=int, default=1)
    parser.add_argument("--dataset", required=True, choices=DATASETS)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--system", required=True, choices=PERTURBATION_SYSTEMS)
    parser.add_argument("--mode", choices=MODES, default="scored")
    parser.add_argument("--bag", required=True, type=Path)
    parser.add_argument("--bag-start", type=float, default=0.0)
    parser.add_argument("--bag-duration", type=float, default=-1.0)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--ros-port", required=True, type=int)
    parser.add_argument("--timeout-seconds", type=float, default=21600.0)
    parser.add_argument("--cpu-list", default="8-15")
    parser.add_argument("--scored-result", type=Path)
    # PERTURB-1 thin extension (docs/icra27/PERTURBATION_PREREG.md).  Absent
    # by default so CDSC-1R4 behaviour is unchanged.
    parser.add_argument("--perturbation-campaign-id", default=None)
    parser.add_argument("--perturbation-offset-frames", type=int, default=0)
    parser.add_argument("--perturbation-frame-rate-hz", type=float, default=20.0)
    parser.add_argument("--perturbation-seed-label", default="frozen")
    parser.add_argument("--perturbation-axis", choices=("offset", "seed"), default="offset")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        args = _parser().parse_args(argv)
        result, run_dir = run_trial(args)
    except (OSError, TrialError, ValueError, json.JSONDecodeError) as exc:
        print("CROSS_DATASET_TRIAL_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("{} {} {} {}: {}".format(result["status"], result["dataset"], result["sequence"], result["system"], run_dir))
    return 0 if result["status"] in ELIGIBLE_STATUSES else 2


if __name__ == "__main__":
    raise SystemExit(main())
