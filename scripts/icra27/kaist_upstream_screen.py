#!/usr/bin/python3
"""Validate and execute the diagnostic ICRA 2027 KAIST U0/S1 screen.

This runner intentionally compares two pinned systems with their native serial
delivery behavior. It is not a matched Schur-attribution harness. Ground truth
is not opened until the estimator process has terminated.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PROTOCOL_PATH = REPO_ROOT / "docs" / "icra27" / "KAIST_UPSTREAM_SCREEN.yaml"
MATRIX_PATH = REPO_ROOT / "docs" / "icra27" / "RUN_MATRIX_G0_5.csv"
DATA_ROOT = Path("/home/moksh/datasets/KAIST_VIO")
DATA_INVENTORY = DATA_ROOT / "manifests" / "BAG_INVENTORY.csv"
ADAPTER_AUDIT_ROOT = DATA_ROOT / "manifests" / "adapter"
U0_REPO = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/open_vins"
)
U0_SETUP = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
    "build/open_vins_ws/devel/setup.bash"
)
U0_BINARY = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/"
    "build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
)
U0_CONFIG = U0_REPO / "config" / "kaist_vio" / "estimator_config.yaml"
U0_LAUNCH = REPO_ROOT / "project" / "icra27_kaist_u0_serial.launch"

S1_SETUP = REPO_ROOT / "build" / "cp0-ws" / "devel" / "setup.bash"
S1_BINARY = REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
S1_CONFIG = REPO_ROOT / "config" / "kaist_vio_turnsafe_baseline" / "estimator_config.yaml"
S1_LAUNCH = REPO_ROOT / "project" / "kaist_vio_serial.launch"

CONVERTER = REPO_ROOT / "scripts" / "cp0" / "openvins_to_tum.py"
PAIRING_TOOL = SCRIPT_DIR / "kaist_pairing_census.py"
GEOMETRY_TOOL = SCRIPT_DIR / "kaist_geometry_bundle.py"
PAIR_EVALUATOR = SCRIPT_DIR / "kaist_pair_evaluator.py"

SCHEMA = "schurvio.icra27.kaist_upstream_screen.v1"
RESULT_SCHEMA = "schurvio.icra27.kaist_upstream_screen.result.v1"
PAIRING_SCHEMA = "schurvio.icra27.kaist_pairing_census.v1"
GEOMETRY_SCHEMA = "schurvio.icra27.kaist_geometry_bundle.v1"
SEED_PREFIX = b"icra27-g0.5-stock-screen-v1\0"
SYSTEMS = ("U0", "S1")
ATTEMPT_KINDS = ("SCORED", "CAPTURE")
ALLOWED_STATUS = {
    "COMPLETE",
    "COMPLETE_WITH_TEARDOWN_DEFECT",
    "PARTIAL",
    "NO_INIT",
    "TRACKING_LOSS",
    "NUMERIC_FAILURE",
    "CRASH",
    "TIMEOUT",
    "INVALID_INFRA",
}
FIXED_ENV = {
    "CUDA_VISIBLE_DEVICES": "",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "MKL_NUM_THREADS": "1",
    "MPLBACKEND": "Agg",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "PYTHONHASHSEED": "0",
    "ROS_HOSTNAME": "127.0.0.1",
    "ROS_IP": "127.0.0.1",
    "TZ": "UTC",
    "VECLIB_MAXIMUM_THREADS": "1",
}
SAFE_ENV_KEYS = {
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
TEARDOWN_CLASS_PATTERN = re.compile(
    r"class_loader::LibraryUnloadException", re.IGNORECASE
)
TEARDOWN_LIBRARY_PATTERN = re.compile(
    r"Attempt to unload library[^\n]*compressed_depth_image_transport\.so",
    re.IGNORECASE,
)
CHILD_EXIT_PATTERN = re.compile(
    r"process(?:\[[^\]]+\])?\s+has died\s+\[pid\s+\d+,\s+"
    r"exit code\s+(-?\d+)",
    re.IGNORECASE,
)
CHILD_START_PATTERN = re.compile(
    r"process\[([^\]]+)\]:\s+started with pid\s+\[(\d+)\]",
    re.IGNORECASE,
)
NONFINITE_PATTERN = re.compile(r"\b(?:nan|inf|nonfinite|non-finite)\b", re.IGNORECASE)
RESET_PATTERN = re.compile(r"\b(?:resetting|estimator reset|state reset)\b", re.IGNORECASE)
COVARIANCE_FAILURE_PATTERN = re.compile(
    r"negative covariance|covariance.*(?:invalid|failure|not positive)", re.IGNORECASE
)
S1_SELECTION_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]:\s*exact_header_pairs=(\d+)\s+"
    r"camera0_without_match=(\d+)\s+camera1_without_match=(\d+)\s+"
    r"record_delta_ge_20ms=(\d+)\s+maximum_record_delta_ns=(\d+)"
)
S1_RUNTIME_PATTERN = re.compile(
    r"\[SERIAL-KAIST\]:\s*queued_pairs=(\d+)\s+processed_pairs=(\d+)\s+"
    r"frequency_thinned_pairs=(\d+)\s+cam0_decode_failures=(\d+)\s+"
    r"cam1_decode_failures=(\d+)\s+pending_pairs=(\d+)"
)


class ScreenError(RuntimeError):
    """Fail-closed screen orchestration error."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(path: Path) -> Dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ScreenError(f"not a regular file: {resolved}")
    return {
        "path": str(resolved),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def identity_beneath(root: Path, relative: Path) -> Dict[str, Any]:
    resolved_root = root.resolve(strict=True)
    resolved = (resolved_root / relative).resolve(strict=True)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ScreenError(f"run artifact escapes its directory: {relative}") from exc
    return identity(resolved)


def require_sha(path: Path, expected: str, label: str) -> Dict[str, Any]:
    observed = identity(path)
    if observed["sha256"] != expected:
        raise ScreenError(
            f"{label} SHA-256 drift: {observed['sha256']} != {expected}"
        )
    return observed


def git_text(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def load_protocol() -> Dict[str, Any]:
    value = yaml.safe_load(PROTOCOL_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ScreenError("KAIST upstream-screen protocol schema mismatch")
    if value.get("status") != "FROZEN_BEFORE_FIRST_U0_LAUNCH":
        raise ScreenError("KAIST upstream-screen protocol is not frozen")
    return value


def load_matrix() -> List[Dict[str, str]]:
    with MATRIX_PATH.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "run_order",
        "sequence_order",
        "sequence",
        "system",
        "attempt_kind",
        "primary_run_id",
        "first_system",
        "seed_sort_sha256",
        "status",
    }
    if not rows or set(rows[0]) != required:
        raise ScreenError("G0.5 run matrix schema mismatch")
    return rows


def validate_matrix(rows: Sequence[Mapping[str, str]]) -> Dict[str, Any]:
    if len(rows) != 44:
        raise ScreenError(f"run matrix must contain 44 rows, observed {len(rows)}")
    orders = [int(row["run_order"]) for row in rows]
    if orders != list(range(1, 45)):
        raise ScreenError("run_order must be the exact sequence 1..44")
    if any(row["status"] != "PENDING" for row in rows):
        raise ScreenError("source run matrix statuses must remain PENDING")

    scored = [row for row in rows if row["attempt_kind"] == "SCORED"]
    capture = [row for row in rows if row["attempt_kind"] == "CAPTURE"]
    if len(scored) != 22 or len(capture) != 22:
        raise ScreenError("matrix must contain 22 scored and 22 capture rows")
    if any(row["attempt_kind"] not in ATTEMPT_KINDS for row in rows):
        raise ScreenError("unknown attempt_kind in run matrix")
    if any(row["system"] not in SYSTEMS for row in rows):
        raise ScreenError("unknown system in run matrix")

    scored_cells = {(row["sequence"], row["system"]) for row in scored}
    capture_cells = {(row["sequence"], row["system"]) for row in capture}
    if len(scored_cells) != 22 or scored_cells != capture_cells:
        raise ScreenError("scored and capture cells are not one-to-one")
    if len({row["primary_run_id"] for row in scored}) != 22:
        raise ScreenError("scored primary_run_id values must be unique")
    scored_by_cell = {(row["sequence"], row["system"]): row for row in scored}
    capture_by_cell = {(row["sequence"], row["system"]): row for row in capture}
    mirrored_fields = (
        "sequence_order",
        "sequence",
        "system",
        "primary_run_id",
        "first_system",
        "seed_sort_sha256",
    )
    for cell in sorted(scored_cells):
        scored_row = scored_by_cell[cell]
        capture_row = capture_by_cell[cell]
        if any(scored_row[field] != capture_row[field] for field in mirrored_fields):
            raise ScreenError(f"capture row does not mirror scored row: {cell}")
        if int(capture_row["run_order"]) != int(scored_row["run_order"]) + 22:
            raise ScreenError(f"capture row order is not scored order + 22: {cell}")
    sequences = sorted({row["sequence"] for row in scored})
    if len(sequences) != 11:
        raise ScreenError("matrix must contain exactly 11 sequences")

    expected_sorted = sorted(
        sequences,
        key=lambda sequence: hashlib.sha256(
            SEED_PREFIX + sequence.encode("utf-8")
        ).hexdigest(),
    )
    observed_sorted: List[str] = []
    for index in range(1, 12):
        group = [row for row in scored if int(row["sequence_order"]) == index]
        if len(group) != 2 or {row["system"] for row in group} != set(SYSTEMS):
            raise ScreenError(f"sequence_order {index} is not a U0/S1 pair")
        sequence = group[0]["sequence"]
        if any(row["sequence"] != sequence for row in group):
            raise ScreenError(f"sequence_order {index} contains mixed sequences")
        digest = hashlib.sha256(SEED_PREFIX + sequence.encode("utf-8")).hexdigest()
        if any(row["seed_sort_sha256"] != digest for row in group):
            raise ScreenError(f"sequence_order {index} seed hash drift")
        expected_first = "U0" if index % 2 == 1 else "S1"
        ordered_group = sorted(group, key=lambda row: int(row["run_order"]))
        if ordered_group[0]["system"] != expected_first:
            raise ScreenError(f"sequence_order {index} method alternation drift")
        if any(row["first_system"] != expected_first for row in group):
            raise ScreenError(f"sequence_order {index} first_system drift")
        observed_sorted.append(sequence)
    if observed_sorted != expected_sorted:
        raise ScreenError("seeded sequence permutation drift")

    if [row["attempt_kind"] for row in rows[:22]] != ["SCORED"] * 22:
        raise ScreenError("all scored rows must precede capture rows")
    if [row["attempt_kind"] for row in rows[22:]] != ["CAPTURE"] * 22:
        raise ScreenError("all capture rows must follow scored rows")
    return {
        "rows": len(rows),
        "scored_cells": len(scored_cells),
        "capture_cells": len(capture_cells),
        "sequences": len(sequences),
        "seeded_sequence_order": observed_sorted,
    }


def validate_run_request(
    args: argparse.Namespace,
    protocol: Mapping[str, Any],
    rows: Sequence[Mapping[str, str]],
) -> Dict[str, Any]:
    if args.dry_run:
        expected_sequence = protocol["schedule"]["dry_run_sequence"]
        if args.sequence != expected_sequence:
            raise ScreenError(
                f"dry run must use frozen sequence {expected_sequence}"
            )
        sequence_stem = Path(expected_sequence).stem
        suffix = "scored" if args.attempt_kind == "SCORED" else "capture"
        expected_run_id = (
            f"g05-dry-{sequence_stem}-{args.system.lower()}-{suffix}"
        )
        if args.run_id != expected_run_id:
            raise ScreenError(
                f"dry-run ID drift: {args.run_id} != {expected_run_id}"
            )
        return {
            "kind": "FROZEN_DRY_RUN",
            "sequence": expected_sequence,
            "system": args.system,
            "attempt_kind": args.attempt_kind,
            "run_id": expected_run_id,
            "linked_primary_run_id": (
                f"g05-dry-{sequence_stem}-{args.system.lower()}-scored"
            ),
        }

    matching = [
        row
        for row in rows
        if row["sequence"] == args.sequence
        and row["system"] == args.system
        and row["attempt_kind"] == args.attempt_kind
    ]
    if len(matching) != 1:
        raise ScreenError("run request does not resolve to exactly one frozen matrix row")
    row = dict(matching[0])
    expected_run_id = row["primary_run_id"]
    if args.attempt_kind == "CAPTURE":
        expected_run_id += "-capture"
    if args.run_id != expected_run_id:
        raise ScreenError(f"matrix run-id drift: {args.run_id} != {expected_run_id}")
    row.update(
        {
            "kind": "PRIMARY_MATRIX",
            "run_id": expected_run_id,
            "linked_primary_run_id": row["primary_run_id"],
        }
    )
    return row


def inventory_rows() -> Dict[str, Dict[str, str]]:
    with DATA_INVENTORY.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    result: Dict[str, Dict[str, str]] = {}
    for row in rows:
        sequence = row["sequence"]
        if sequence in result:
            raise ScreenError(f"duplicate dataset inventory sequence: {sequence}")
        result[sequence] = row
    return result


def validate_common_assets(protocol: Mapping[str, Any], hash_bags: bool) -> Dict[str, Any]:
    systems = protocol["systems"]
    u0 = systems["U0"]
    s1 = systems["S1"]
    if git_text(U0_REPO, "rev-parse", "HEAD") != u0["source_commit"]:
        raise ScreenError("U0 source commit drift")
    if git_text(U0_REPO, "rev-parse", "HEAD^{tree}") != u0["source_tree"]:
        raise ScreenError("U0 source tree drift")
    if git_text(U0_REPO, "status", "--porcelain"):
        raise ScreenError("U0 source checkout is dirty")

    harness = protocol.get("harness")
    if not isinstance(harness, dict):
        raise ScreenError("frozen harness identity is absent from protocol")
    harness_paths = {
        "runner": Path(__file__).resolve(),
        "matrix": MATRIX_PATH,
        "u0_launch": U0_LAUNCH,
        "s1_launch": S1_LAUNCH,
        "converter": CONVERTER,
        "pairing_census": PAIRING_TOOL,
        "geometry_bundle": GEOMETRY_TOOL,
        "runtime_identity": SCRIPT_DIR / "kaist_runtime_identity.py",
        "pair_evaluator": PAIR_EVALUATOR,
    }
    expected_hashes = harness.get("file_sha256")
    if not isinstance(expected_hashes, dict) or set(expected_hashes) != set(
        harness_paths
    ):
        raise ScreenError("frozen harness file set is incomplete")
    harness_files = {
        name: require_sha(path, expected_hashes[name], f"harness {name}")
        for name, path in harness_paths.items()
    }
    if git_text(REPO_ROOT, "status", "--porcelain", "--untracked-files=all"):
        raise ScreenError("G0.5 harness repository is dirty")
    harness_head = git_text(REPO_ROOT, "rev-parse", "HEAD")
    harness_ref = harness.get("git_ref")
    if not isinstance(harness_ref, str) or not harness_ref:
        raise ScreenError("frozen harness Git ref is absent")
    if git_text(REPO_ROOT, "rev-parse", harness_ref) != harness_head:
        raise ScreenError("G0.5 harness HEAD differs from its frozen Git ref")
    harness_git = {
        "ref": harness_ref,
        "commit": harness_head,
        "tree": git_text(REPO_ROOT, "rev-parse", "HEAD^{tree}"),
        "clean": True,
        "files": harness_files,
    }

    evidence = {
        "protocol": identity(PROTOCOL_PATH),
        "matrix": identity(MATRIX_PATH),
        "data_inventory": identity(DATA_INVENTORY),
        "U0": {
            "binary": require_sha(U0_BINARY, u0["executable_sha256"], "U0 binary"),
            "config": require_sha(U0_CONFIG, u0["estimator_config_sha256"], "U0 config"),
            "imu_config": require_sha(
                U0_CONFIG.parent / "kalibr_imu_chain.yaml",
                u0["imu_config_sha256"],
                "U0 IMU config",
            ),
            "camera_imu_config": require_sha(
                U0_CONFIG.parent / "kalibr_imucam_chain.yaml",
                u0["camera_imu_config_sha256"],
                "U0 camera/IMU config",
            ),
            "launch": identity(U0_LAUNCH),
            "setup": identity(U0_SETUP),
            "source_commit": u0["source_commit"],
            "source_tree": u0["source_tree"],
            "source_clean": True,
        },
        "S1": {
            "binary": require_sha(S1_BINARY, s1["executable_sha256"], "S1 binary"),
            "config": require_sha(S1_CONFIG, s1["estimator_config_sha256"], "S1 config"),
            "imu_config": require_sha(
                S1_CONFIG.parent / "kalibr_imu_chain.yaml",
                s1["imu_config_sha256"],
                "S1 IMU config",
            ),
            "camera_imu_config": require_sha(
                S1_CONFIG.parent / "kalibr_imucam_chain.yaml",
                s1["camera_imu_config_sha256"],
                "S1 camera/IMU config",
            ),
            "launch": identity(S1_LAUNCH),
            "setup": identity(S1_SETUP),
            "build_provenance": require_sha(
                REPO_ROOT / "build" / "cp0-ws" / "CP0_BUILD_PROVENANCE.json",
                s1["build_provenance_sha256"],
                "S1 build provenance",
            ),
            "estimator_source_commit": s1["estimator_source_commit"],
            "estimator_source_tree": s1["estimator_source_tree"],
        },
        "converter": identity(CONVERTER),
        "pairing_census_tool": identity(PAIRING_TOOL),
        "geometry_bundle_tool": identity(GEOMETRY_TOOL),
        "runtime_identity_tool": identity(SCRIPT_DIR / "kaist_runtime_identity.py"),
        "pair_evaluator_tool": identity(PAIR_EVALUATOR),
        "harness_git": harness_git,
    }
    rows = inventory_rows()
    matrix_sequences = {row["sequence"] for row in load_matrix()}
    if matrix_sequences != set(rows):
        raise ScreenError("run matrix and KAIST data inventory sequence sets differ")
    evidence["bags"] = {}
    for sequence in sorted(rows):
        row = rows[sequence]
        bag = Path(row["adapted_path"])
        if not bag.is_file() or bag.stat().st_size != int(row["adapted_size_bytes"]):
            raise ScreenError(f"adapted bag missing or size drift: {sequence}")
        bag_value = {
            "path": str(bag.resolve()),
            "size_bytes": bag.stat().st_size,
            "expected_sha256": row["adapted_sha256"],
        }
        if hash_bags:
            observed_hash = sha256_file(bag)
            if observed_hash != row["adapted_sha256"]:
                raise ScreenError(f"adapted bag SHA-256 drift: {sequence}")
            bag_value["sha256"] = observed_hash
        evidence["bags"][sequence] = bag_value
    return evidence


def source_environment(setup: Path, output: Path, port: int) -> Dict[str, str]:
    command = [
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        'source "$1"; exec env -0',
        "g05-env",
        str(setup),
    ]
    result = subprocess.run(command, check=True, stdout=subprocess.PIPE)
    environment: Dict[str, str] = {}
    for entry in result.stdout.split(b"\0"):
        if not entry or b"=" not in entry:
            continue
        key, value = entry.split(b"=", 1)
        decoded_key = key.decode()
        if decoded_key in SAFE_ENV_KEYS:
            environment[decoded_key] = value.decode()
    environment.update(FIXED_ENV)
    environment["ROS_MASTER_URI"] = f"http://127.0.0.1:{port}"
    environment["ROS_HOME"] = str(output / "ros-home")
    environment["ROS_LOG_DIR"] = str(output / "ros-logs")
    return dict(sorted(environment.items()))


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def wait_for_port(port: int, open_state: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        available = port_available(port)
        if (not available) == open_state:
            return
        time.sleep(0.1)
    state = "open" if open_state else "closed"
    raise ScreenError(f"ROS port {port} did not become {state}")


def process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_for_process_group_exit(process_group_id: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_group_exists(process_group_id):
            return True
        time.sleep(0.05)
    return not process_group_exists(process_group_id)


def terminate(process: Optional[subprocess.Popen[Any]], grace: float = 10.0) -> None:
    if process is None:
        return
    process_group_id = process.pid
    for action, timeout in (
        (signal.SIGINT, grace),
        (signal.SIGTERM, 5.0),
        (signal.SIGKILL, 5.0),
    ):
        if not process_group_exists(process_group_id):
            break
        try:
            os.killpg(process_group_id, action)
        except ProcessLookupError:
            break
        if wait_for_process_group_exit(process_group_id, timeout):
            break
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as exc:
            raise ScreenError("process group cleanup did not reap its leader") from exc
    if process_group_exists(process_group_id):
        raise ScreenError(f"process group {process_group_id} survived cleanup")


def run_logged(
    argv: Sequence[str],
    log_path: Path,
    environment: Mapping[str, str],
    timeout: float,
    cwd: Path = REPO_ROOT,
) -> Dict[str, Any]:
    started = time.monotonic()
    started_utc = utc_now()
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(environment),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        timed_out = False
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            exit_code = None
        finally:
            # Always sweep the whole new-session process group. A wrapper or
            # roslaunch leader can exit while leaving a descendant alive.
            terminate(process)
        if timed_out:
            exit_code = process.returncode
    return {
        "argv": list(argv),
        "cwd": str(cwd),
        "started_utc": started_utc,
        "finished_utc": utc_now(),
        "duration_seconds": time.monotonic() - started,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "log": identity(log_path),
    }


def launch_arguments(system: str, bag: Path, output: Path) -> Tuple[Path, List[str]]:
    state = output / "trajectory" / "state_estimate.txt"
    deviation = output / "trajectory" / "state_deviation.txt"
    timing = output / "diagnostics" / "timing.csv"
    if system == "U0":
        return U0_LAUNCH, [
            f"config_path:={U0_CONFIG}",
            f"bag:={bag}",
            "bag_start:=0.0",
            "bag_durr:=-1.0",
            f"path_state:={state}",
            f"path_std:={deviation}",
            f"path_time:={timing}",
            "record_timing:=true",
        ]
    if system == "S1":
        return S1_LAUNCH, [
            f"bag:={bag}",
            "bag_start:=0.0",
            f"path_state:={state}",
            f"path_std:={deviation}",
            f"path_time:={timing}",
            "verbosity:=INFO",
        ]
    raise ScreenError(f"unknown system: {system}")


def expected_resolved_parameters(
    system: str, bag: Path, output: Path
) -> Dict[str, Any]:
    common_paths = {
        "path_bag": str(bag.resolve()),
        "filepath_est": str((output / "trajectory" / "state_estimate.txt").resolve()),
        "filepath_std": str((output / "trajectory" / "state_deviation.txt").resolve()),
        "record_timing_filepath": str(
            (output / "diagnostics" / "timing.csv").resolve()
        ),
    }
    if system == "U0":
        namespace = "/icra27_kaist_u0/"
        values: Dict[str, Any] = {
            "bag_durr": -1.0,
            "bag_start": 0.0,
            "cam0_rostopic": "/turnsafe/kaist/infra1/image_raw",
            "cam1_rostopic": "/turnsafe/kaist/infra2/image_raw",
            "config_path": str(U0_CONFIG.resolve()),
            "imu0_rostopic": "/mavros/imu/data",
            "record_timing_information": True,
            "save_total_state": True,
            **common_paths,
        }
    elif system == "S1":
        namespace = "/kaist_vio_turnsafe_baseline/"
        values = {
            "bag_durr": -1.0,
            "bag_start": 0.0,
            "calib_cam_extrinsics": False,
            "calib_cam_intrinsics": False,
            "calib_cam_timeoffset": False,
            "calib_imu_g_sensitivity": False,
            "calib_imu_intrinsics": False,
            "cam0_distortion_model": "radtan",
            "cam0_rostopic": "/turnsafe/kaist/infra1/image_raw",
            "cam1_distortion_model": "radtan",
            "cam1_rostopic": "/turnsafe/kaist/infra2/image_raw",
            "config_path": str(S1_CONFIG.resolve()),
            "feat_rep_msckf": "GLOBAL_3D",
            "imu0_rostopic": "/mavros/imu/data",
            "kaist_vio_exact_header_stereo": True,
            "max_cameras": 2,
            "multi_threading_pubs": False,
            "multi_threading_subs": False,
            "num_opencv_threads": 0,
            "record_timing_information": True,
            "save_total_state": True,
            "turnsafe_t0_binary_sha256": "",
            "turnsafe_t0_build_manifest_sha256": "",
            "turnsafe_t0_build_provenance_id": "",
            "turnsafe_t0_calibration_sha256": "",
            "turnsafe_t0_capture": False,
            "turnsafe_t0_capture_causal_imu_intervals": False,
            "turnsafe_t0_capture_group_bearing_provenance": False,
            "turnsafe_t0_capture_outcome_association_keys": False,
            "turnsafe_t0_config_sha256": "",
            "turnsafe_t0_diagnostic_schema_sha256": "",
            "turnsafe_t0_event_extension_schema": "turnsafe.t0.event_extension.v1",
            "turnsafe_t0_frozen_base_sha": "",
            "turnsafe_t0_output_path": "",
            "turnsafe_t0_require_target_stereo_range": True,
            "turnsafe_t0_run_identity": "",
            "turnsafe_t0_schema": "turnsafe.t0.v1",
            "turnsafe_t0_source_sha": "",
            "turnsafe_t0_source_snapshot_sha256": "",
            "turnsafe_t0_source_tree": "",
            "up_msckf_landmark_elimination": "schur",
            "up_msckf_max_visual_passes": 1,
            "use_fej": True,
            "use_stereo": True,
            "verbosity": "INFO",
            **common_paths,
        }
    else:
        raise ScreenError(f"unknown system: {system}")
    return {namespace + name: value for name, value in values.items()}


def validate_resolved_parameters(
    system: str, path: Path, bag: Path, output: Path
) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8", errors="strict")
    observed = yaml.safe_load(raw)
    if not isinstance(observed, dict):
        raise ScreenError("resolved ROS parameters are not a mapping")
    if any(
        "path_gt" in str(key).lower() or "groundtruth" in str(key).lower()
        for key in observed
    ):
        raise ScreenError("ground-truth parameter leaked into estimator launch")
    path_suffixes = {
        "config_path",
        "filepath_est",
        "filepath_std",
        "path_bag",
        "record_timing_filepath",
    }
    normalized: Dict[str, Any] = {}
    for key, value in observed.items():
        suffix = str(key).rsplit("/", 1)[-1]
        if suffix in path_suffixes:
            if not isinstance(value, str):
                raise ScreenError(f"resolved path parameter is not a string: {key}")
            value = str(Path(value).resolve(strict=False))
        normalized[str(key)] = value
    expected = expected_resolved_parameters(system, bag, output)
    if normalized != expected:
        missing = sorted(set(expected) - set(normalized))
        extra = sorted(set(normalized) - set(expected))
        changed = {
            key: {"observed": normalized[key], "expected": expected[key]}
            for key in sorted(set(normalized).intersection(expected))
            if normalized[key] != expected[key]
        }
        raise ScreenError(
            "resolved parameter contract drift: "
            f"missing={missing}, extra={extra}, changed={changed}"
        )
    return {
        "artifact": identity(path),
        "parameter_count": len(normalized),
        "exact_typed_map_match": True,
    }


def recorder_topics(system: str) -> List[str]:
    namespace = "/icra27_kaist_u0" if system == "U0" else "/kaist_vio_turnsafe_baseline"
    return [
        namespace + "/poseimu",
        namespace + "/points_slam",
        namespace + "/points_msckf",
        namespace + "/points_aruco",
        namespace + "/loop_feats",
    ]


def validate_numeric_table(path: Path, minimum_columns: int) -> Dict[str, Any]:
    if not path.is_file():
        raise ScreenError(f"missing numeric output: {path}")
    rows = 0
    columns: Optional[int] = None
    first: Optional[float] = None
    last: Optional[float] = None
    previous: Optional[float] = None
    maximum_gap = 0.0
    with path.open(encoding="utf-8") as stream:
        for line_number, raw in enumerate(stream, 1):
            stripped = raw.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                values = [float(token) for token in stripped.replace(",", " ").split()]
            except ValueError as exc:
                raise ScreenError(f"nonnumeric row {path}:{line_number}") from exc
            if len(values) < minimum_columns:
                raise ScreenError(f"too few columns at {path}:{line_number}")
            if not all(math.isfinite(value) for value in values):
                raise ScreenError(f"nonfinite value at {path}:{line_number}")
            if columns is None:
                columns = len(values)
            elif columns != len(values):
                raise ScreenError(f"ragged numeric table: {path}")
            timestamp = values[0]
            if previous is not None and timestamp <= previous:
                raise ScreenError(f"timestamps not strictly increasing: {path}")
            if previous is not None:
                maximum_gap = max(maximum_gap, timestamp - previous)
            previous = timestamp
            first = timestamp if first is None else first
            last = timestamp
            rows += 1
    if rows == 0 or first is None or last is None or columns is None:
        raise ScreenError(f"empty numeric output: {path}")
    value = identity(path)
    value.update(
        {
            "rows": rows,
            "columns": columns,
            "first_timestamp": first,
            "last_timestamp": last,
            "maximum_timestamp_gap": maximum_gap,
            "finite": True,
            "timestamps_strictly_increasing": True,
        }
    )
    return value


def adapter_audit(sequence: str) -> Dict[str, Any]:
    name = Path(sequence).stem + ".json"
    path = ADAPTER_AUDIT_ROOT / name
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "turnsafe.kaist_vio_adapter.adaptation.v1":
        raise ScreenError(f"adapter audit schema mismatch: {sequence}")
    if not value.get("preservation", {}).get("all_passed"):
        raise ScreenError(f"adapter preservation failed: {sequence}")
    return {"artifact": identity(path), "value": value}


def classify_console(console: Path) -> Dict[str, Any]:
    text = console.read_text(encoding="utf-8", errors="replace")
    child_exits = [int(value) for value in CHILD_EXIT_PATTERN.findall(text)]
    child_starts = [
        {"name": name, "pid": int(pid)}
        for name, pid in CHILD_START_PATTERN.findall(text)
    ]
    return {
        "child_starts": child_starts,
        "child_exit_codes": child_exits,
        "teardown_exception_class_pattern": bool(TEARDOWN_CLASS_PATTERN.search(text)),
        "teardown_compressed_depth_library_pattern": bool(
            TEARDOWN_LIBRARY_PATTERN.search(text)
        ),
        "post_coverage_teardown_pattern": bool(
            TEARDOWN_CLASS_PATTERN.search(text)
            and TEARDOWN_LIBRARY_PATTERN.search(text)
        ),
        "nonfinite_pattern": bool(NONFINITE_PATTERN.search(text)),
        "reset_pattern": bool(RESET_PATTERN.search(text)),
        "covariance_failure_pattern": bool(COVARIANCE_FAILURE_PATTERN.search(text)),
    }


def assess_completion(
    protocol: Mapping[str, Any],
    selected_start_ns: int,
    selected_end_ns: int,
    state: Mapping[str, Any],
) -> Dict[str, Any]:
    """Apply the prospectively frozen initialization, continuity, and tail gates."""

    selected_start = selected_start_ns / 1e9
    selected_end = selected_end_ns / 1e9
    input_span = selected_end - selected_start
    if not math.isfinite(input_span) or input_span <= 0.0:
        raise ScreenError("selected camera interval is empty or nonfinite")
    first_state = float(state["first_timestamp"])
    last_state = float(state["last_timestamp"])
    output_span = last_state - first_state
    initialization_delay = first_state - selected_start
    initialization_fraction = initialization_delay / input_span
    tail_gap = selected_end - last_state
    attempt = protocol["attempt_status"]
    negative_tolerance = float(attempt["negative_tail_tolerance_seconds"])
    initialization_max_fraction = float(
        attempt["initialization_delay_max_fraction_of_selected_input"]
    )
    maximum_state_gap = float(
        attempt["post_initialization_max_state_gap_seconds"]
    )
    tail_max = float(attempt["tail_gap_max_seconds"])
    return {
        "first_selected_camera_timestamp_ns": selected_start_ns,
        "last_selected_camera_timestamp_ns": selected_end_ns,
        "first_selected_camera_timestamp": selected_start,
        "last_selected_camera_timestamp": selected_end,
        "first_state_timestamp": first_state,
        "last_state_timestamp": last_state,
        "initialization_delay_seconds": initialization_delay,
        "initialization_delay_fraction_of_selected_input": initialization_fraction,
        "maximum_initialization_delay_fraction": initialization_max_fraction,
        "maximum_initialization_delay_seconds": initialization_max_fraction * input_span,
        "initialization_delay_pass": (
            -negative_tolerance <= initialization_delay
            and initialization_fraction <= initialization_max_fraction
        ),
        "selected_input_span_seconds": input_span,
        "state_output_span_seconds": output_span,
        "state_span_fraction_of_selected_input": output_span / input_span,
        "maximum_post_initialization_state_gap_seconds": float(
            state["maximum_timestamp_gap"]
        ),
        "maximum_state_gap_pass": (
            float(state["maximum_timestamp_gap"]) <= maximum_state_gap
        ),
        "tail_gap_seconds": tail_gap,
        "tail_gap_pass": -negative_tolerance <= tail_gap <= tail_max,
    }


def parse_s1_delivery(console: Path) -> Dict[str, int]:
    text = console.read_text(encoding="utf-8", errors="replace")
    selection_matches = S1_SELECTION_PATTERN.findall(text)
    runtime_matches = S1_RUNTIME_PATTERN.findall(text)
    if len(selection_matches) != 1 or len(runtime_matches) != 1:
        raise ScreenError(
            "S1 console must contain exactly one selection and one runtime census"
        )
    selection = tuple(int(value) for value in selection_matches[0])
    runtime = tuple(int(value) for value in runtime_matches[0])
    return {
        "exact_header_pair_count": selection[0],
        "camera0_unmatched_count": selection[1],
        "camera1_unmatched_count": selection[2],
        "record_delta_at_or_above_20ms_count": selection[3],
        "maximum_record_delta_ns": selection[4],
        "queued_pair_count": runtime[0],
        "processed_pair_count": runtime[1],
        "frequency_thinned_pair_count": runtime[2],
        "camera0_decode_failure_count": runtime[3],
        "camera1_decode_failure_count": runtime[4],
        "pending_pair_count": runtime[5],
    }


def merge_attempt_census(
    system: str,
    pairing_record: Mapping[str, Any],
    console: Path,
    state: Mapping[str, Any],
    required_fields: Sequence[str],
) -> Dict[str, Any]:
    static_value = pairing_record.get("static_value")
    if not isinstance(static_value, dict) or static_value.get("schema") != PAIRING_SCHEMA:
        raise ScreenError("pairing-census schema mismatch")
    raw_census = static_value.get("census")
    if not isinstance(raw_census, dict):
        raise ScreenError("pairing census lacks its required flat field set")
    census = dict(raw_census)
    census["estimator_output_first_timestamp_ns"] = int(
        round(float(state["first_timestamp"]) * 1_000_000_000)
    )
    census["estimator_output_last_timestamp_ns"] = int(
        round(float(state["last_timestamp"]) * 1_000_000_000)
    )

    runtime: Optional[Dict[str, int]] = None
    if system == "S1":
        runtime = parse_s1_delivery(console)
        expected_static = {
            "exact_header_pair_count": census["s1_exact_pair_count"],
            "camera0_unmatched_count": census["camera0_unmatched_count"],
            "camera1_unmatched_count": census["camera1_unmatched_count"],
        }
        for name, expected in expected_static.items():
            if runtime[name] != expected:
                raise ScreenError(
                    f"S1 runtime/static pairing mismatch for {name}: "
                    f"{runtime[name]} != {expected}"
                )
        if runtime["processed_pair_count"] != runtime["queued_pair_count"]:
            raise ScreenError("S1 processed/queued pair counts differ")
        if (
            runtime["queued_pair_count"]
            + runtime["frequency_thinned_pair_count"]
            != runtime["exact_header_pair_count"]
        ):
            raise ScreenError("S1 queue/thinning accounting does not close")
        if runtime["pending_pair_count"] != 0:
            raise ScreenError("S1 camera queue did not drain")
        if (
            runtime["camera0_decode_failure_count"] != 0
            or runtime["camera1_decode_failure_count"] != 0
        ):
            raise ScreenError("S1 reports a camera decode failure")
        census.update(
            {
                "s1_queued_pair_count": runtime["queued_pair_count"],
                "s1_processed_pair_count": runtime["processed_pair_count"],
                "s1_frequency_thinned_pair_count": runtime[
                    "frequency_thinned_pair_count"
                ],
                "s1_pending_pair_count": runtime["pending_pair_count"],
            }
        )
    elif system != "U0":
        raise ScreenError(f"unknown system for pairing census: {system}")

    expected_fields = set(required_fields)
    if set(census) != expected_fields:
        missing = sorted(expected_fields - set(census))
        extra = sorted(set(census) - expected_fields)
        raise ScreenError(
            f"attempt pairing-census field drift: missing={missing}, extra={extra}"
        )
    always_required = (
        "source_camera0_count",
        "source_camera1_count",
        "u0_native_pair_count",
        "s1_exact_pair_count",
        "u0_first_selected_header_stamp_ns",
        "u0_last_selected_header_stamp_ns",
        "s1_first_selected_header_stamp_ns",
        "s1_last_selected_header_stamp_ns",
        "estimator_output_first_timestamp_ns",
        "estimator_output_last_timestamp_ns",
    )
    if any(census[name] is None for name in always_required):
        raise ScreenError("pairing census contains a null required boundary/count")
    if system == "S1" and any(
        census[name] is None
        for name in (
            "s1_queued_pair_count",
            "s1_processed_pair_count",
            "s1_frequency_thinned_pair_count",
            "s1_pending_pair_count",
        )
    ):
        raise ScreenError("S1 pairing census contains a null runtime count")
    return {
        "artifact": pairing_record["artifact"],
        "command": pairing_record["command"],
        "static_schema": static_value["schema"],
        "census": census,
        "selection_bounds": static_value.get("selection_bounds"),
        "runtime_s1": runtime,
        "availability": {
            "static_input_census": "COMPLETE",
            "s1_runtime_counters": "COMPLETE" if system == "S1" else "NOT_APPLICABLE",
            "estimator_output_timestamps": "COMPLETE",
        },
    }


def prepare_static_attempt_census(
    system: str,
    pairing_record: Mapping[str, Any],
    required_fields: Sequence[str],
) -> Dict[str, Any]:
    static_value = pairing_record.get("static_value")
    if not isinstance(static_value, dict) or static_value.get("schema") != PAIRING_SCHEMA:
        raise ScreenError("pairing-census schema mismatch")
    raw_census = static_value.get("census")
    if not isinstance(raw_census, dict):
        raise ScreenError("pairing census lacks its required flat field set")
    census = dict(raw_census)
    census["estimator_output_first_timestamp_ns"] = None
    census["estimator_output_last_timestamp_ns"] = None
    if set(census) != set(required_fields):
        raise ScreenError("static pairing census does not match frozen field set")
    return {
        "artifact": pairing_record["artifact"],
        "command": pairing_record["command"],
        "static_schema": static_value["schema"],
        "census": census,
        "selection_bounds": static_value.get("selection_bounds"),
        "runtime_s1": None,
        "availability": {
            "static_input_census": "COMPLETE",
            "s1_runtime_counters": (
                "PENDING_ATTEMPT" if system == "S1" else "NOT_APPLICABLE"
            ),
            "estimator_output_timestamps": "PENDING_ATTEMPT",
        },
    }


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    if path.exists() or temporary.exists():
        raise ScreenError(f"refusing to overwrite JSON: {path}")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def start_roscore(
    output: Path, port: int, environment: Mapping[str, str]
) -> Tuple[subprocess.Popen[Any], Any]:
    if not port_available(port):
        raise ScreenError(f"ROS port already in use: {port}")
    stream = (output / "diagnostics" / "roscore.log").open("wb")
    process: Optional[subprocess.Popen[Any]] = None
    try:
        process = subprocess.Popen(
            ["roscore", "-p", str(port)],
            cwd=str(REPO_ROOT),
            env=dict(environment),
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_for_port(port, True)
    except BaseException:
        try:
            terminate(process)
        finally:
            stream.close()
        raise
    if process is None:
        raise ScreenError("roscore process was not created")
    return process, stream


def start_recorder(
    system: str, output: Path, environment: Mapping[str, str]
) -> Tuple[subprocess.Popen[Any], Any]:
    geometry = output / "geometry"
    stream = (output / "diagnostics" / "geometry_recorder.log").open("wb")
    bag = geometry / "feature_stream.bag"
    argv = [
        "rosbag",
        "record",
        "--buffsize=0",
        "--chunksize=768",
        "-O",
        str(bag),
        *recorder_topics(system),
        "__name:=icra27_geometry_recorder",
    ]
    process: Optional[subprocess.Popen[Any]] = None
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(REPO_ROOT),
            env=dict(environment),
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            probe = subprocess.run(
                ["rosnode", "info", "/icra27_geometry_recorder"],
                env=dict(environment),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                return process, stream
            if process.poll() is not None:
                raise ScreenError("geometry recorder exited before becoming ready")
            time.sleep(0.1)
        raise ScreenError("geometry recorder did not become ready")
    except BaseException:
        try:
            terminate(process)
        finally:
            stream.close()
        raise
    if process is None:
        raise ScreenError("geometry recorder process was not created")


def run_pairing_census(
    bag: Path, output: Path, environment: Mapping[str, str]
) -> Optional[Dict[str, Any]]:
    if not PAIRING_TOOL.is_file():
        return None
    census_path = output / "diagnostics" / "pairing_census.json"
    record = run_logged(
        [
            sys.executable,
            str(PAIRING_TOOL),
            "--bag",
            str(bag),
            "--output",
            str(census_path),
        ],
        output / "diagnostics" / "pairing_census.log",
        environment,
        300.0,
    )
    if record["exit_code"] != 0 or not census_path.is_file():
        raise ScreenError("pairing census failed")
    value = json.loads(census_path.read_text(encoding="utf-8"))
    return {
        "command": record,
        "artifact": identity(census_path),
        "static_value": value,
    }


def run_geometry_bundle(
    output: Path, reference: Path, environment: Mapping[str, str]
) -> Optional[Dict[str, Any]]:
    if not GEOMETRY_TOOL.is_file():
        return None
    feature_bag = output / "geometry" / "feature_stream.bag"
    trajectory = output / "trajectory" / "estimate_raw.tum"
    record = run_logged(
        [
            "/usr/bin/python3",
            "-B",
            str(GEOMETRY_TOOL),
            "--feature-bag",
            str(feature_bag),
            "--capture-trajectory",
            str(trajectory),
            "--ground-truth",
            str(reference),
            "--run-dir",
            str(output),
        ],
        output / "diagnostics" / "geometry_bundle.log",
        environment,
        900.0,
    )
    manifest_path = output / "geometry_manifest.json"
    checksums_path = output / "SHA256SUMS"
    if (
        record["exit_code"] != 0
        or not manifest_path.is_file()
        or not checksums_path.is_file()
    ):
        raise ScreenError("qualitative geometry bundle failed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != GEOMETRY_SCHEMA or manifest.get("status") != "COMPLETE":
        raise ScreenError("qualitative geometry manifest is not complete")
    return {
        "status": "COMPLETE",
        "tool": identity(GEOMETRY_TOOL),
        "command": record,
        "manifest": identity(manifest_path),
        "checksums": identity(checksums_path),
    }


def harvest_incomplete_capture(
    output: Path, attempt_status: str, reason: str
) -> Dict[str, Any]:
    retained: Dict[str, Any] = {}
    for root_name in ("geometry", "figures"):
        root = output / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.name != "incomplete_capture.json":
                retained[path.relative_to(output).as_posix()] = identity(path)
    manifest_path = output / "geometry" / "incomplete_capture.json"
    value = {
        "schema": "schurvio.icra27.incomplete_geometry_capture.v1",
        "status": "INCOMPLETE",
        "attempt_status": attempt_status,
        "reason": reason,
        "retained_files": retained,
        "feature_stream_status": (
            "RETAINED"
            if "geometry/feature_stream.bag" in retained
            else (
                "RETAINED_UNCLOSED_ACTIVE"
                if "geometry/feature_stream.bag.active" in retained
                else "NOT_EMITTED_BEFORE_FAILURE"
            )
        ),
        "postprocessor_status": "NOT_RUN_OR_INCOMPLETE",
    }
    atomic_json(manifest_path, value)
    return {
        "status": "INCOMPLETE",
        "manifest": identity(manifest_path),
        "retained_files": retained,
    }


def run_one(args: argparse.Namespace) -> Dict[str, Any]:
    protocol = load_protocol()
    rows = load_matrix()
    validate_matrix(rows)
    if args.system not in SYSTEMS:
        raise ScreenError("--system must be U0 or S1")
    if args.attempt_kind not in ATTEMPT_KINDS:
        raise ScreenError("--attempt-kind must be SCORED or CAPTURE")
    schedule_record = validate_run_request(args, protocol, rows)
    inventory = inventory_rows()
    if args.sequence not in inventory:
        raise ScreenError(f"sequence is outside frozen matrix: {args.sequence}")

    linked: Optional[Path] = None
    linked_result: Optional[Dict[str, Any]] = None
    linked_result_identity: Optional[Dict[str, Any]] = None
    linked_state_identity: Optional[Dict[str, Any]] = None
    linked_trajectory_identity: Optional[Dict[str, Any]] = None
    if args.attempt_kind == "CAPTURE":
        if args.linked_scored is None:
            raise ScreenError("capture replay requires --linked-scored")
        linked = args.linked_scored.resolve(strict=True)
        if not linked.is_dir():
            raise ScreenError("linked scored path is not a run directory")
        linked_result_path = linked / "sequence_result.json"
        linked_result = json.loads(linked_result_path.read_text(encoding="utf-8"))
        if (
            linked_result.get("schema") != RESULT_SCHEMA
            or linked_result.get("protocol_id") != protocol["protocol_id"]
            or linked_result.get("system") != args.system
            or linked_result.get("sequence") != args.sequence
            or linked_result.get("attempt_kind") != "SCORED"
            or linked_result.get("run_id")
            != schedule_record["linked_primary_run_id"]
            or linked_result.get("status")
            not in {"COMPLETE", "COMPLETE_WITH_TEARDOWN_DEFECT"}
            or bool(linked_result.get("dry_run")) != bool(args.dry_run)
            or not linked_result.get("checks", {}).get(
                "trajectory_ready_for_paired_evaluation"
            )
            or "state" not in linked_result.get("outputs", {})
            or "trajectory" not in linked_result.get("outputs", {})
        ):
            raise ScreenError("linked scored run identity/status does not match capture")
        linked_result_identity = identity_beneath(
            linked, Path("sequence_result.json")
        )
        linked_state_identity = identity_beneath(
            linked, Path("trajectory/state_estimate.txt")
        )
        linked_trajectory_identity = identity_beneath(
            linked, Path("trajectory/estimate_raw.tum")
        )
        if (
            linked_state_identity["sha256"]
            != linked_result["outputs"]["state"]["sha256"]
            or linked_trajectory_identity["sha256"]
            != linked_result["outputs"]["trajectory"]["sha256"]
        ):
            raise ScreenError("linked scored bytes no longer match their manifest")
    elif args.linked_scored is not None:
        raise ScreenError("--linked-scored is valid only for CAPTURE attempts")

    output = args.output.resolve(strict=False)
    if output.exists():
        raise ScreenError(f"refusing to overwrite output: {output}")
    artifact_root = Path(protocol["artifact_policy"]["root"]).resolve()
    try:
        relative_output = output.relative_to(artifact_root)
    except ValueError as exc:
        raise ScreenError(f"output must be below {artifact_root}") from exc
    if relative_output == Path("."):
        raise ScreenError(f"output must be below {artifact_root}")
    output.mkdir(parents=True)
    for directory in ("trajectory", "diagnostics", "geometry", "figures", "metrics", "ros-home", "ros-logs"):
        (output / directory).mkdir()

    result_path = output / "sequence_result.json"
    result: Dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "protocol_id": protocol["protocol_id"],
        "run_id": args.run_id,
        "sequence": args.sequence,
        "system": args.system,
        "attempt_kind": args.attempt_kind,
        "dry_run": bool(args.dry_run),
        "schedule": schedule_record,
        "started_utc": utc_now(),
        "status": "INVALID_INFRA",
        "commands": {},
        "checks": {},
    }
    roscore: Optional[subprocess.Popen[Any]] = None
    roscore_stream: Any = None
    recorder: Optional[subprocess.Popen[Any]] = None
    recorder_stream: Any = None
    try:
        common = validate_common_assets(protocol, hash_bags=False)
        row = inventory[args.sequence]
        bag = Path(row["adapted_path"]).resolve(strict=True)
        bag_identity = require_sha(
            bag, row["adapted_sha256"], f"adapted bag {args.sequence}"
        )
        common["selected_bag"] = bag_identity
        result["inputs"] = common

        setup = U0_SETUP if args.system == "U0" else S1_SETUP
        environment = source_environment(setup, output, args.ros_port)
        result["environment"] = environment
        launch, launch_args = launch_arguments(args.system, bag, output)
        runtime_identity_path = output / "diagnostics" / "runtime_identity.json"
        runtime_identity_record = run_logged(
            [
                "/usr/bin/python3",
                "-B",
                str(SCRIPT_DIR / "kaist_runtime_identity.py"),
                "--system",
                args.system,
            ],
            runtime_identity_path,
            environment,
            180.0,
        )
        result["commands"]["runtime_identity"] = runtime_identity_record
        if runtime_identity_record["exit_code"] != 0:
            raise ScreenError("runtime identity validation failed")
        runtime_identity = json.loads(runtime_identity_path.read_text(encoding="utf-8"))
        if (
            runtime_identity.get("schema")
            != "schurvio.icra27.kaist_runtime_identity.v1"
            or runtime_identity.get("status") != "PASS"
            or runtime_identity.get("system") != args.system
        ):
            raise ScreenError("runtime identity evidence schema/status mismatch")
        result["inputs"][args.system]["runtime_identity"] = runtime_identity

        static_pairing = run_pairing_census(bag, output, environment)
        if static_pairing is None:
            raise ScreenError("mandatory pairing-census tool is unavailable")
        static_bag = static_pairing["static_value"].get("bag", {})
        if static_bag.get("sha256") != bag_identity["sha256"]:
            raise ScreenError("pairing census was computed from different bag bytes")
        result["pairing_census"] = prepare_static_attempt_census(
            args.system, static_pairing, protocol["pairing_census"]["fields"]
        )
        result["checks"]["static_pairing_census_present"] = True

        resolved_binary_record = run_logged(
            ["catkin_find", "--libexec", "ov_msckf", "ros1_serial_msckf"],
            output / "diagnostics" / "resolved_binary.txt",
            environment,
            30.0,
        )
        result["commands"]["resolve_binary"] = resolved_binary_record
        resolved_binary_lines = [
            line.strip()
            for line in (output / "diagnostics" / "resolved_binary.txt").read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        ]
        expected_binary = U0_BINARY if args.system == "U0" else S1_BINARY
        if resolved_binary_record["exit_code"] != 0 or resolved_binary_lines != [
            str(expected_binary.resolve())
        ]:
            raise ScreenError(
                f"catkin resolved unexpected estimator binary: {resolved_binary_lines}"
            )
        result["checks"]["binary_and_dynamic_libraries_resolved"] = True

        attempt_started_path = output / "attempt_started.json"
        atomic_json(
            attempt_started_path,
            {
                "schema": "schurvio.icra27.kaist_attempt_started.v1",
                "started_utc": utc_now(),
                "protocol_id": protocol["protocol_id"],
                "protocol": common["protocol"],
                "matrix": common["matrix"],
                "run_id": args.run_id,
                "schedule": schedule_record,
                "system": args.system,
                "sequence": args.sequence,
                "attempt_kind": args.attempt_kind,
                "dry_run": bool(args.dry_run),
                "bag": bag_identity,
                "runtime_identity_policy_sha256": runtime_identity[
                    "policy_sha256"
                ],
            },
        )
        result["attempt_started"] = identity(attempt_started_path)
        roscore, roscore_stream = start_roscore(output, args.ros_port, environment)
        if args.attempt_kind == "CAPTURE":
            recorder, recorder_stream = start_recorder(args.system, output, environment)

        dump_command = ["roslaunch", "--dump-params", str(launch), *launch_args]
        dump = run_logged(
            dump_command,
            output / "diagnostics" / "resolved_parameters.yaml",
            environment,
            60.0,
        )
        result["commands"]["resolve_parameters"] = dump
        if dump["exit_code"] != 0:
            raise ScreenError("roslaunch parameter resolution failed")
        resolved_parameter_record = validate_resolved_parameters(
            args.system,
            output / "diagnostics" / "resolved_parameters.yaml",
            bag,
            output,
        )
        result["resolved_parameters"] = resolved_parameter_record
        result["checks"]["ground_truth_absent_from_launch"] = True
        result["checks"]["resolved_parameter_map_exact"] = True

        timeout = float(protocol["schedule"]["outer_timeout_seconds"])
        affinity_check = run_logged(
            [
                "/usr/bin/taskset",
                "--cpu-list",
                protocol["schedule"]["cpu_affinity"],
                "/bin/true",
            ],
            output / "diagnostics" / "cpu_affinity_preflight.log",
            environment,
            30.0,
        )
        result["commands"]["cpu_affinity_preflight"] = affinity_check
        if affinity_check["exit_code"] != 0:
            raise ScreenError("frozen CPU affinity is unavailable on this host")
        result["checks"]["cpu_affinity_preflight_passed"] = True
        resource = output / "diagnostics" / "resource_usage.txt"
        launch_command = [
            "/usr/bin/time",
            "--verbose",
            "--output",
            str(resource),
            "/usr/bin/taskset",
            "--cpu-list",
            protocol["schedule"]["cpu_affinity"],
            "roslaunch",
            str(launch),
            *launch_args,
        ]
        launch_record = run_logged(
            launch_command,
            output / "diagnostics" / "console.log",
            environment,
            timeout,
        )
        result["commands"]["estimator"] = launch_record
        result["resource_usage"] = identity(resource) if resource.is_file() else None
        if recorder is not None:
            terminate(recorder, grace=20.0)
            recorder = None
        if recorder_stream is not None:
            recorder_stream.close()
            recorder_stream = None
        terminate(roscore)
        roscore = None
        if roscore_stream is not None:
            roscore_stream.close()
            roscore_stream = None

        console = classify_console(output / "diagnostics" / "console.log")
        result["console_classification"] = console
        expected_node_prefix = (
            "icra27_kaist_u0-"
            if args.system == "U0"
            else "kaist_vio_turnsafe_baseline-"
        )
        estimator_starts = [
            value
            for value in console["child_starts"]
            if value["name"].startswith(expected_node_prefix)
        ]
        if len(estimator_starts) != 1:
            result["status"] = "INVALID_INFRA"
            raise ScreenError(
                "wrapper did not report exactly one expected estimator process start"
            )
        result["estimator_process_start"] = estimator_starts[0]
        candidate_teardown = (
            args.system == "U0"
            and console["post_coverage_teardown_pattern"]
            and console["child_exit_codes"] == [-6]
        )
        if launch_record["timed_out"]:
            result["status"] = "TIMEOUT"
            raise ScreenError("estimator timed out")
        abnormal_exit = (
            launch_record["exit_code"] not in (0, None)
            or bool(console["child_exit_codes"])
        )
        if abnormal_exit and not candidate_teardown:
            result["status"] = "CRASH"
            raise ScreenError(
                "estimator process exited abnormally: "
                f"roslaunch={launch_record['exit_code']}, "
                f"children={console['child_exit_codes']}"
            )

        state_path = output / "trajectory" / "state_estimate.txt"
        deviation_path = output / "trajectory" / "state_deviation.txt"
        timing_path = output / "diagnostics" / "timing.csv"
        if not state_path.is_file() or state_path.stat().st_size == 0:
            result["status"] = (
                "CRASH"
                if abnormal_exit
                else "NO_INIT"
            )
            raise ScreenError("estimator produced no state trajectory")
        try:
            state = validate_numeric_table(state_path, 8)
        except ScreenError:
            result["status"] = "NUMERIC_FAILURE"
            raise
        try:
            deviation = validate_numeric_table(deviation_path, 8)
        except ScreenError as exc:
            result["status"] = (
                "NUMERIC_FAILURE" if "nonfinite" in str(exc) else "INVALID_INFRA"
            )
            raise
        if state["rows"] != deviation["rows"]:
            result["status"] = "INVALID_INFRA"
            raise ScreenError("state and deviation row counts differ")
        timing: Dict[str, Any]
        try:
            timing = validate_numeric_table(timing_path, 2)
            timing["row_count_matches_state"] = timing["rows"] == state["rows"]
        except ScreenError as exc:
            timing = {
                "status": "UNAVAILABLE_DIAGNOSTIC_ONLY",
                "error": str(exc),
            }
        result["outputs"] = {
            "state": state,
            "deviation": deviation,
            "timing": timing,
        }
        result["checks"]["numeric_estimator_outputs_valid"] = True
        result["checks"]["timing_diagnostic_available"] = (
            timing.get("status") != "UNAVAILABLE_DIAGNOSTIC_ONLY"
        )

        result["pairing_census"] = merge_attempt_census(
            args.system,
            static_pairing,
            output / "diagnostics" / "console.log",
            state,
            protocol["pairing_census"]["fields"],
        )
        result["checks"]["pairing_census_present"] = True

        # Estimator has closed: ground truth and adapter audit may now be opened.
        audit = adapter_audit(args.sequence)
        result["adapter_audit"] = audit["artifact"]
        prefix = "u0" if args.system == "U0" else "s1"
        selected_start_field = f"{prefix}_first_selected_header_stamp_ns"
        selected_end_field = f"{prefix}_last_selected_header_stamp_ns"
        selected_start_ns = result["pairing_census"]["census"][selected_start_field]
        selected_end_ns = result["pairing_census"]["census"][selected_end_field]
        result["completion"] = assess_completion(
            protocol,
            selected_start_ns,
            selected_end_ns,
            state,
        )
        result["completion"].update({
            "first_selected_timestamp_policy": selected_start_field,
            "selected_timestamp_policy": selected_end_field,
        })
        if not result["completion"]["tail_gap_pass"]:
            result["status"] = "PARTIAL"
            raise ScreenError(
                "trajectory tail gap outside frozen bound: "
                f"{result['completion']['tail_gap_seconds']}"
            )
        if not result["completion"]["initialization_delay_pass"]:
            result["status"] = "PARTIAL"
            raise ScreenError(
                "initialization occurs outside the first 10 percent of the "
                "selected-input span"
            )
        if not result["completion"]["maximum_state_gap_pass"]:
            result["status"] = "TRACKING_LOSS"
            raise ScreenError(
                "post-initialization state trajectory contains a gap above the "
                "frozen bound"
            )
        if console["nonfinite_pattern"] or console["covariance_failure_pattern"]:
            result["status"] = "NUMERIC_FAILURE"
            raise ScreenError("console reports a numerical/covariance failure")
        if console["reset_pattern"]:
            result["status"] = "TRACKING_LOSS"
            raise ScreenError("console reports an estimator reset")

        trajectory = output / "trajectory" / "estimate_raw.tum"
        conversion = run_logged(
            ["/usr/bin/python3", str(CONVERTER), str(state_path), str(trajectory)],
            output / "diagnostics" / "trajectory_conversion.log",
            environment,
            120.0,
        )
        result["commands"]["trajectory_conversion"] = conversion
        if conversion["exit_code"] != 0:
            raise ScreenError("OpenVINS-to-TUM conversion failed")
        converted = validate_numeric_table(trajectory, 8)
        if (
            converted["rows"] != state["rows"]
            or converted["first_timestamp"] != state["first_timestamp"]
            or converted["last_timestamp"] != state["last_timestamp"]
        ):
            result["status"] = "INVALID_INFRA"
            raise ScreenError(
                "converted trajectory row count/timestamp bounds differ from state output"
            )
        result["outputs"]["trajectory"] = converted

        reference = REPO_ROOT / "ov_data" / "kaist_vio" / (Path(args.sequence).stem + ".txt")
        result["inputs"]["ground_truth"] = identity(reference)
        if args.attempt_kind == "SCORED":
            result["evaluation"] = {
                "status": "PENDING_PAIRED_COMMON_POPULATION",
                "reason": (
                    "primary metrics are computed only after U0 and S1 close, "
                    "on their exact common ground-truth row population"
                ),
            }
            result["checks"]["trajectory_ready_for_paired_evaluation"] = True

        if args.attempt_kind == "CAPTURE":
            feature_bag = output / "geometry" / "feature_stream.bag"
            if not feature_bag.is_file() or feature_bag.stat().st_size == 0:
                raise ScreenError("capture replay produced no feature-stream bag")
            if (
                linked is None
                or linked_result is None
                or linked_result_identity is None
                or linked_state_identity is None
                or linked_trajectory_identity is None
            ):
                raise ScreenError("capture linkage was not prevalidated")
            linked_result_after = identity_beneath(
                linked, Path("sequence_result.json")
            )
            linked_state_after = identity_beneath(
                linked, Path("trajectory/state_estimate.txt")
            )
            linked_trajectory_after = identity_beneath(
                linked, Path("trajectory/estimate_raw.tum")
            )
            if (
                linked_result_after != linked_result_identity
                or linked_state_after != linked_state_identity
                or linked_trajectory_after != linked_trajectory_identity
                or linked_state_after["sha256"]
                != linked_result["outputs"]["state"]["sha256"]
                or linked_trajectory_after["sha256"]
                != linked_result["outputs"]["trajectory"]["sha256"]
            ):
                raise ScreenError("linked scored evidence changed during capture replay")
            linked_state_sha = linked_state_after["sha256"]
            linked_trajectory_sha = linked_trajectory_after["sha256"]
            exact = (
                state["sha256"] == linked_state_sha
                and result["outputs"]["trajectory"]["sha256"]
                == linked_trajectory_sha
            )
            linked_record = {
                "path": str(linked),
                "result": linked_result_after,
                "state": linked_state_after,
                "trajectory": linked_trajectory_after,
                "state_sha256": linked_state_sha,
                "trajectory_sha256": linked_trajectory_sha,
                "byte_exact": exact,
            }
            if not exact:
                raise ScreenError("capture replay trajectory is not byte-identical to scored run")
            result["checks"]["capture_trajectory_byte_exact"] = True
            postprocessor = run_geometry_bundle(output, reference, environment)
            if postprocessor is None:
                raise ScreenError("mandatory qualitative geometry tool is unavailable")
            missing_geometry = [
                relative
                for relative in protocol["geometry"]["required_outputs"]
                if not (output / relative).is_file()
            ]
            if missing_geometry:
                raise ScreenError(
                    f"qualitative geometry output set is incomplete: {missing_geometry}"
                )
            result["geometry"] = {
                "feature_stream": identity(feature_bag),
                "linked_scored": linked_record,
                "postprocessor": postprocessor,
            }
            result["checks"]["qualitative_geometry_bundle_complete"] = True

        post_coverage_teardown = (
            candidate_teardown
            and result["completion"]["tail_gap_pass"]
        )
        result["status"] = (
            "COMPLETE_WITH_TEARDOWN_DEFECT" if post_coverage_teardown else "COMPLETE"
        )
        result["execution_health"] = not post_coverage_teardown
        result["finished_utc"] = utc_now()
        result["checks"]["status_allowed"] = result["status"] in ALLOWED_STATUS
        atomic_json(result_path, result)
        return result
    except BaseException as exc:
        cleanup_errors: List[Dict[str, str]] = []
        for name, process in (("recorder", recorder), ("roscore", roscore)):
            try:
                terminate(process)
            except BaseException as cleanup_exc:
                cleanup_errors.append(
                    {"resource": name, "error": str(cleanup_exc)}
                )
        for name, stream in (
            ("recorder_log", recorder_stream),
            ("roscore_log", roscore_stream),
        ):
            if stream is None:
                continue
            try:
                stream.close()
            except BaseException as cleanup_exc:
                cleanup_errors.append(
                    {"resource": name, "error": str(cleanup_exc)}
                )
        result["finished_utc"] = utc_now()
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if result["status"] not in ALLOWED_STATUS:
            result["status"] = "INVALID_INFRA"
        pairing = result.get("pairing_census")
        if isinstance(pairing, dict) and isinstance(pairing.get("availability"), dict):
            for field in ("s1_runtime_counters", "estimator_output_timestamps"):
                if pairing["availability"].get(field) == "PENDING_ATTEMPT":
                    pairing["availability"][field] = (
                        f"UNAVAILABLE_ATTEMPT_STATUS_{result['status']}"
                    )
            pairing["unavailable_reason"] = str(exc)
        if args.attempt_kind == "CAPTURE":
            try:
                result["geometry"] = harvest_incomplete_capture(
                    output, result["status"], str(exc)
                )
            except BaseException as harvest_exc:
                cleanup_errors.append(
                    {"resource": "capture_harvest", "error": str(harvest_exc)}
                )
        if cleanup_errors:
            result["cleanup_errors"] = cleanup_errors
        if not result_path.exists():
            try:
                atomic_json(result_path, result)
            except Exception:
                pass
        raise


def validate_command(args: argparse.Namespace) -> int:
    protocol = load_protocol()
    matrix = validate_matrix(load_matrix())
    assets = validate_common_assets(protocol, hash_bags=args.hash_bags)
    value = {
        "schema": "schurvio.icra27.kaist_upstream_screen.validation.v1",
        "validated_utc": utc_now(),
        "matrix": matrix,
        "assets": assets,
        "hash_bags": bool(args.hash_bags),
        "status": "PASS",
    }
    if args.output:
        output = args.output.resolve(strict=False)
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            raise ScreenError(f"refusing to overwrite validation output: {output}")
        atomic_json(output, value)
    print(json.dumps(value, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--hash-bags", action="store_true")
    validate.add_argument("--output", type=Path)

    one = subparsers.add_parser("run-one")
    one.add_argument("--system", required=True, choices=SYSTEMS)
    one.add_argument("--sequence", required=True)
    one.add_argument("--attempt-kind", required=True, choices=ATTEMPT_KINDS)
    one.add_argument("--run-id", required=True)
    one.add_argument("--output", required=True, type=Path)
    one.add_argument("--ros-port", required=True, type=int)
    one.add_argument("--linked-scored", type=Path)
    one.add_argument("--dry-run", action="store_true")
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "validate":
            return validate_command(args)
        if args.command == "run-one":
            result = run_one(args)
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        raise ScreenError(f"unknown command: {args.command}")
    except (ScreenError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
