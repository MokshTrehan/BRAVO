#!/usr/bin/python3
"""Run one declared conditioning profile through the ordinary ROS1 serial node."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

from verify_replay_profiles import (
    SCHEMA2_PROFILE_IDS,
    ProfileError,
    validate_profiles,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LAUNCH_PATH = Path(__file__).resolve().with_name("ros1_conditioning_replay.launch")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
ESTIMATOR_DIED_PATTERN = re.compile(
    r"\[ov_msckf(?:-\d+)?\]\s+process has died .*?exit code (-?\d+)",
    re.IGNORECASE,
)
ESTIMATOR_CLEAN_PATTERN = re.compile(
    r"\[ov_msckf(?:-\d+)?\]\s+process has finished cleanly", re.IGNORECASE
)
SCHEMA2_READER = (
    REPOSITORY_ROOT / "experiments/anytime_information/capture_reader.py"
)


class RunError(RuntimeError):
    """A precondition or replay outcome is invalid."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def bounded_sha256(path: Path, timeout_seconds: float) -> str:
    try:
        result = subprocess.run(
            ("/usr/bin/sha256sum", "--", str(path)),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunError(f"SHA-256 timed out after {timeout_seconds}s: {path}") from exc
    if result.returncode != 0:
        raise RunError(f"SHA-256 failed for {path}: {result.stderr.strip()}")
    actual = result.stdout.split(maxsplit=1)[0].lower()
    if not HASH_PATTERN.fullmatch(actual):
        raise RunError(f"invalid SHA-256 output for {path}: {result.stdout!r}")
    return actual


def validate_schema2_capture(
    path: Path, timeout_seconds: float
) -> tuple[bool, Dict[str, Any] | None, str | None]:
    """Run the strict Schema-2 reader with a finite wall-clock bound."""

    try:
        result = subprocess.run(
            ("/usr/bin/python3", str(SCHEMA2_READER), "validate", str(path)),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            cwd=str(REPOSITORY_ROOT),
        )
    except subprocess.TimeoutExpired:
        return False, None, f"strict reader timed out after {timeout_seconds}s"
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        return False, None, detail or "strict reader rejected capture"
    try:
        summary = json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        return False, None, f"strict reader emitted invalid JSON: {exc}"
    if not isinstance(summary, dict) or summary.get("status") != "valid":
        return False, None, "strict reader did not report valid status"
    return True, summary, None


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def checked_output(argv: Sequence[str], timeout_seconds: float = 15.0) -> str:
    result = subprocess.run(
        argv,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
    )
    if result.returncode != 0:
        raise RunError(
            f"command failed ({result.returncode}): {list(argv)!r}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_output(*args: str) -> str:
    return checked_output(("git", "-C", str(REPOSITORY_ROOT), *args))


def source_provenance() -> Dict[str, Any]:
    return {
        "root": git_output("rev-parse", "--show-toplevel"),
        "branch": git_output("branch", "--show-current"),
        "head": git_output("rev-parse", "HEAD"),
        "status": git_output("status", "--porcelain=v1").splitlines(),
        "remote_v": git_output("remote", "-v").splitlines(),
    }


def validate_workspace(setup: Path) -> Path:
    if not setup.is_file():
        raise RunError(f"workspace setup does not exist: {setup}")
    prefix = setup.parent
    estimator = prefix / "lib" / "ov_msckf" / "ros1_serial_msckf"
    if not estimator.is_file() or not os.access(str(estimator), os.X_OK):
        raise RunError(f"ordinary serial estimator is missing or not executable: {estimator}")
    package_path = checked_output(
        (
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            'source "$1"; rospack find ov_msckf',
            "conditioning-workspace-check",
            str(setup),
        )
    )
    expected = (REPOSITORY_ROOT / "ov_msckf").resolve()
    if Path(package_path).resolve() != expected:
        raise RunError(f"workspace resolves ov_msckf to {package_path}, expected {expected}")
    return estimator


def require_free_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RunError(f"ROS master port {port} is unavailable: {exc}") from exc


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_group(process: subprocess.Popen, grace_seconds: float) -> None:
    pgid = process.pid
    if not group_alive(pgid):
        process.poll()
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        if not group_alive(pgid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        raise RunError(f"process group {pgid} survived SIGKILL") from exc


def estimator_child_exit(log_path: Path) -> int | None:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    died = ESTIMATOR_DIED_PATTERN.findall(text)
    if died:
        return int(died[-1])
    if ESTIMATOR_CLEAN_PATTERN.search(text):
        return 0
    return None


def output_hashes(output: Path, timeout_seconds: float) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "result.json":
            result[str(path.relative_to(output))] = bounded_sha256(
                path, timeout_seconds
            )
    return result


def write_tum_trajectory(state_path: Path, trajectory_path: Path) -> int:
    """Create a deterministic TUM pose stream from the native total-state log.

    The estimator writes ``timestamp qx qy qz qw px py pz ...``. TUM expects
    ``timestamp px py pz qx qy qz qw``. Tokens are reordered without numeric
    reformatting so byte comparisons remain meaningful.
    """

    pose_count = 0
    with state_path.open("r", encoding="utf-8") as source, trajectory_path.open(
        "x", encoding="utf-8", newline="\n"
    ) as destination:
        for line_number, raw_line in enumerate(source, 1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split()
            if len(fields) < 8:
                raise RunError(
                    f"state line {line_number} has {len(fields)} fields, expected at least 8"
                )
            pose_tokens = (
                fields[0],
                fields[5],
                fields[6],
                fields[7],
                fields[1],
                fields[2],
                fields[3],
                fields[4],
            )
            if not all(math.isfinite(float(token)) for token in pose_tokens):
                raise RunError(f"state line {line_number} contains a non-finite pose")
            destination.write(" ".join(pose_tokens) + "\n")
            pose_count += 1
    if pose_count == 0:
        raise RunError("state output contains no poses for the TUM trajectory")
    return pose_count


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-setup", required=True, type=Path)
    parser.add_argument("--bag", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--capture",
        action="store_true",
        help="enable nonmutating conditioning capture (default: disabled)",
    )
    parser.add_argument(
        "--capture-path",
        type=Path,
        help="absolute create-new capture file; required with --capture",
    )
    parser.add_argument(
        "--capture-update-envelopes-v2",
        action="store_true",
        help="enable nonmutating Schema-2 update-envelope capture (default: disabled)",
    )
    parser.add_argument(
        "--update-envelope-capture-path",
        type=Path,
        help=(
            "absolute create-new Schema-2 capture file; required with "
            "--capture-update-envelopes-v2"
        ),
    )
    parser.add_argument("--update-envelope-run-id")
    parser.add_argument("--update-envelope-sequence-id")
    parser.add_argument("--bag-start-seconds", type=float, default=0.0)
    parser.add_argument("--bag-duration-seconds", type=float, default=-1.0)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--termination-grace-seconds", type=float, default=15.0)
    parser.add_argument("--bag-hash-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--output-hash-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--expected-bag-sha256")
    parser.add_argument("--ros-master-port", required=True, type=int)
    return parser.parse_args(argv)


def validate_args(
    args: argparse.Namespace,
) -> tuple[Path, Path, Path, Path | None, Path | None]:
    bag = args.bag.resolve()
    config = args.config.resolve()
    output = args.output_dir.resolve()
    if not bag.is_file():
        raise RunError(f"bag does not exist: {bag}")
    if not LAUNCH_PATH.is_file():
        raise RunError(f"launch file does not exist: {LAUNCH_PATH}")
    if output.exists():
        raise RunError(f"refusing to overwrite output directory: {output}")
    if is_within(output, REPOSITORY_ROOT):
        raise RunError("generated replay output must remain outside the Git worktree")
    if args.timeout_seconds <= 0 or args.termination_grace_seconds <= 0:
        raise RunError("run timeout and termination grace must be positive")
    if args.bag_hash_timeout_seconds <= 0 or args.output_hash_timeout_seconds <= 0:
        raise RunError("bag and output hash timeouts must be positive")
    if args.bag_start_seconds < 0:
        raise RunError("bag start must be nonnegative")
    if args.bag_duration_seconds == 0 or args.bag_duration_seconds < -1:
        raise RunError("bag duration must be -1 or positive")
    if not 1024 <= args.ros_master_port <= 65535:
        raise RunError("ROS master port must be in [1024, 65535]")
    if args.expected_bag_sha256 is not None and not HASH_PATTERN.fullmatch(
        args.expected_bag_sha256.lower()
    ):
        raise RunError("--expected-bag-sha256 must be 64 lowercase hexadecimal digits")

    profile_report = validate_profiles(config)
    if len(profile_report) != 1:
        raise RunError(f"config did not resolve to exactly one replay profile: {config}")
    profile_id = next(iter(profile_report))
    schema2_profile = profile_id in SCHEMA2_PROFILE_IDS
    if args.capture and args.capture_update_envelopes_v2:
        raise RunError("Schema-1 and Schema-2 capture modes are mutually exclusive")
    if args.capture and schema2_profile:
        raise RunError("--capture requires a declared Schema-1 conditioning profile")

    capture_path: Path | None = None
    if args.capture:
        if args.capture_path is None or not args.capture_path.is_absolute():
            raise RunError("--capture requires an absolute --capture-path")
        capture_path = args.capture_path.resolve()
        if capture_path.exists():
            raise RunError(f"refusing to overwrite capture file: {capture_path}")
        if is_within(capture_path, REPOSITORY_ROOT):
            raise RunError("conditioning capture must remain outside the Git worktree")
        if not is_within(capture_path, output) and not capture_path.parent.is_dir():
            raise RunError("capture parent must exist unless capture is inside --output-dir")
    elif args.capture_path is not None:
        raise RunError("--capture-path is invalid unless --capture is supplied")

    update_envelope_capture_path: Path | None = None
    if args.capture_update_envelopes_v2:
        if not schema2_profile:
            raise RunError(
                "--capture-update-envelopes-v2 requires a declared Schema-2 profile"
            )
        if (
            args.update_envelope_capture_path is None
            or not args.update_envelope_capture_path.is_absolute()
        ):
            raise RunError(
                "--capture-update-envelopes-v2 requires an absolute "
                "--update-envelope-capture-path"
            )
        for option, value in (
            ("--update-envelope-run-id", args.update_envelope_run_id),
            ("--update-envelope-sequence-id", args.update_envelope_sequence_id),
        ):
            if value is None or not IDENTIFIER_PATTERN.fullmatch(value):
                raise RunError(
                    f"{option} must match {IDENTIFIER_PATTERN.pattern!r} for Schema-2 capture"
                )
        update_envelope_capture_path = args.update_envelope_capture_path.resolve()
        if update_envelope_capture_path.exists():
            raise RunError(
                "refusing to overwrite Schema-2 update-envelope capture file: "
                f"{update_envelope_capture_path}"
            )
        if is_within(update_envelope_capture_path, REPOSITORY_ROOT):
            raise RunError("Schema-2 capture must remain outside the Git worktree")
        if (
            not is_within(update_envelope_capture_path, output)
            and not update_envelope_capture_path.parent.is_dir()
        ):
            raise RunError(
                "Schema-2 capture parent must exist unless capture is inside --output-dir"
            )
    elif any(
        value is not None
        for value in (
            args.update_envelope_capture_path,
            args.update_envelope_run_id,
            args.update_envelope_sequence_id,
        )
    ):
        raise RunError(
            "Schema-2 path/run/sequence options require "
            "--capture-update-envelopes-v2"
        )

    return bag, config, output, capture_path, update_envelope_capture_path


def run(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        bag, config, output, capture_path, update_envelope_capture_path = validate_args(args)
        setup = args.workspace_setup.resolve()
        estimator = validate_workspace(setup)
        require_free_port(args.ros_master_port)
        bag_hash = bounded_sha256(bag, args.bag_hash_timeout_seconds)
        if (
            args.expected_bag_sha256 is not None
            and bag_hash != args.expected_bag_sha256.lower()
        ):
            raise RunError(
                f"bag SHA-256 {bag_hash} != expected {args.expected_bag_sha256.lower()}"
            )

        output.mkdir(parents=True, exist_ok=False)
        if capture_path is not None and is_within(capture_path, output):
            capture_path.parent.mkdir(parents=True, exist_ok=True)
        if (
            update_envelope_capture_path is not None
            and is_within(update_envelope_capture_path, output)
        ):
            update_envelope_capture_path.parent.mkdir(parents=True, exist_ok=True)
        state_path = output / "state_estimate.txt"
        deviation_path = output / "state_deviation.txt"
        trajectory_path = output / "trajectory_tum.txt"
        timing_path = output / "timing.csv"
        log_path = output / "run.log"
        resource_path = output / "resource_usage.txt"
        ros_home = output / "ros_home"
        ros_logs = output / "ros_logs"
        ros_home.mkdir()
        ros_logs.mkdir()

        launch_args = (
            "roslaunch",
            str(LAUNCH_PATH),
            f"config_path:={config}",
            f"bag:={bag}",
            f"bag_start:={args.bag_start_seconds}",
            f"bag_duration:={args.bag_duration_seconds}",
            "verbosity:=INFO",
            f"state_path:={state_path}",
            f"deviation_path:={deviation_path}",
            f"timing_path:={timing_path}",
            f"capture:={'true' if args.capture else 'false'}",
            f"capture_path:={capture_path if capture_path is not None else ''}",
        )
        if args.capture_update_envelopes_v2:
            launch_args += (
                "capture_update_envelopes_v2:=true",
                f"update_envelope_capture_path:={update_envelope_capture_path}",
                f"update_envelope_run_id:={args.update_envelope_run_id}",
                f"update_envelope_sequence_id:={args.update_envelope_sequence_id}",
            )
        command = (
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            'source "$1"; shift; exec "$@"',
            "conditioning-replay",
            str(setup),
            "/usr/bin/time",
            "--verbose",
            "--output",
            str(resource_path),
            *launch_args,
        )
        environment = os.environ.copy()
        environment.update(
            {
                "ROS_MASTER_URI": f"http://127.0.0.1:{args.ros_master_port}",
                "ROS_HOSTNAME": "127.0.0.1",
                "ROS_IP": "127.0.0.1",
                "ROS_HOME": str(ros_home),
                "ROS_LOG_DIR": str(ros_logs),
                "CUDA_VISIBLE_DEVICES": "",
                "OMP_NUM_THREADS": "1",
                "OPENBLAS_NUM_THREADS": "1",
                "MKL_NUM_THREADS": "1",
                "NUMEXPR_NUM_THREADS": "1",
                "VECLIB_MAXIMUM_THREADS": "1",
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
            }
        )
        profile_report = validate_profiles(config)
        command_record = {
            "command": list(command),
            "environment_overrides": {
                key: environment[key]
                for key in (
                    "ROS_MASTER_URI",
                    "ROS_HOSTNAME",
                    "ROS_IP",
                    "ROS_HOME",
                    "ROS_LOG_DIR",
                    "CUDA_VISIBLE_DEVICES",
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                    "VECLIB_MAXIMUM_THREADS",
                    "PYTHONHASHSEED",
                    "TZ",
                    "LANG",
                    "LC_ALL",
                )
            },
            "source": source_provenance(),
            "profile": profile_report,
            "config_sha256": sha256(config),
            "bag_path": str(bag),
            "bag_sha256": bag_hash,
            "bag_size_bytes": bag.stat().st_size,
            "estimator_binary": str(estimator),
            "estimator_binary_sha256": sha256(estimator),
            "capture_enabled": args.capture,
            "capture_path": str(capture_path) if capture_path is not None else None,
            "update_envelope_capture_enabled": args.capture_update_envelopes_v2,
            "update_envelope_capture_path": (
                str(update_envelope_capture_path)
                if update_envelope_capture_path is not None
                else None
            ),
            "update_envelope_run_id": args.update_envelope_run_id,
            "update_envelope_sequence_id": args.update_envelope_sequence_id,
            "bag_start_seconds": args.bag_start_seconds,
            "bag_duration_seconds": args.bag_duration_seconds,
            "timeout_seconds": args.timeout_seconds,
            "termination_grace_seconds": args.termination_grace_seconds,
            "output_hash_timeout_seconds": args.output_hash_timeout_seconds,
            "started_utc": utc_now(),
        }
        (output / "command.json").write_text(
            json.dumps(command_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        timed_out = False
        interrupted_signal: int | None = None
        roslaunch_exit: int | None = None
        start_monotonic = time.monotonic()
        process: subprocess.Popen | None = None
        received_signal: int | None = None

        def interrupt_handler(signum: int, _frame: Any) -> None:
            nonlocal received_signal
            received_signal = signum
            raise KeyboardInterrupt

        prior_handlers = {
            signum: signal.getsignal(signum)
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        with log_path.open("wb") as log_stream:
            for signum in prior_handlers:
                signal.signal(signum, interrupt_handler)
            try:
                process = subprocess.Popen(
                    command,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    cwd=str(REPOSITORY_ROOT),
                    start_new_session=True,
                )
                try:
                    roslaunch_exit = process.wait(timeout=args.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_group(process, args.termination_grace_seconds)
                    roslaunch_exit = 124
                except KeyboardInterrupt:
                    interrupted_signal = received_signal or signal.SIGINT
                    terminate_group(process, args.termination_grace_seconds)
                    roslaunch_exit = 128 + int(interrupted_signal)
            finally:
                if process is not None and group_alive(process.pid):
                    terminate_group(process, args.termination_grace_seconds)
                if process is not None and process.poll() is None:
                    process.wait(timeout=5)
                for signum, prior_handler in prior_handlers.items():
                    signal.signal(signum, prior_handler)

        child_exit = estimator_child_exit(log_path)
        trajectory_pose_count = 0
        if state_path.is_file() and state_path.stat().st_size > 0:
            trajectory_pose_count = write_tum_trajectory(state_path, trajectory_path)
        required_outputs = (state_path, deviation_path, trajectory_path, timing_path)
        outputs_valid = all(path.is_file() and path.stat().st_size > 0 for path in required_outputs)
        conditioning_capture_valid = bool(
            not args.capture
            or (
                capture_path is not None
                and capture_path.is_file()
                and capture_path.stat().st_size > 0
            )
        )
        update_envelope_capture_valid = not args.capture_update_envelopes_v2
        update_envelope_capture_summary: Dict[str, Any] | None = None
        update_envelope_capture_error: str | None = None
        if args.capture_update_envelopes_v2:
            if (
                update_envelope_capture_path is None
                or not update_envelope_capture_path.is_file()
                or update_envelope_capture_path.stat().st_size == 0
            ):
                update_envelope_capture_valid = False
                update_envelope_capture_error = "capture is absent or empty"
            else:
                (
                    update_envelope_capture_valid,
                    update_envelope_capture_summary,
                    update_envelope_capture_error,
                ) = validate_schema2_capture(
                    update_envelope_capture_path,
                    args.output_hash_timeout_seconds,
                )
        capture_valid = conditioning_capture_valid and update_envelope_capture_valid
        if timed_out:
            effective_exit = 124
        elif interrupted_signal is not None:
            effective_exit = 128 + int(interrupted_signal)
        elif child_exit not in (None, 0):
            effective_exit = 1
        elif roslaunch_exit != 0 or not outputs_valid or not capture_valid:
            effective_exit = int(roslaunch_exit or 1)
        else:
            effective_exit = 0
        (output / "exit_status.txt").write_text(f"{effective_exit}\n", encoding="utf-8")
        (output / "timed_out.txt").write_text(
            f"{1 if timed_out else 0}\n", encoding="utf-8"
        )
        result = {
            "completed": effective_exit == 0,
            "effective_exit_code": effective_exit,
            "roslaunch_exit_code": roslaunch_exit,
            "estimator_child_exit": child_exit,
            "timed_out": timed_out,
            "interrupted_signal": interrupted_signal,
            "elapsed_wall_seconds": time.monotonic() - start_monotonic,
            "finished_utc": utc_now(),
            "state_output_valid": state_path.is_file() and state_path.stat().st_size > 0,
            "deviation_output_valid": deviation_path.is_file()
            and deviation_path.stat().st_size > 0,
            "timing_output_valid": timing_path.is_file() and timing_path.stat().st_size > 0,
            "trajectory_output_valid": trajectory_path.is_file()
            and trajectory_path.stat().st_size > 0,
            "trajectory_pose_count": trajectory_pose_count,
            "capture_output_valid": capture_valid,
            "conditioning_capture_output_valid": conditioning_capture_valid,
            "update_envelope_capture_output_valid": update_envelope_capture_valid,
            "update_envelope_capture_summary": update_envelope_capture_summary,
            "update_envelope_capture_error": update_envelope_capture_error,
            "output_sha256": output_hashes(output, args.output_hash_timeout_seconds),
        }
        (output / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return effective_exit
    except (OSError, ProfileError, RunError, subprocess.SubprocessError, ValueError) as exc:
        print(f"conditioning replay failed before completion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
