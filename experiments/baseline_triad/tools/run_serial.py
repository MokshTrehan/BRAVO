#!/usr/bin/python3
"""Run one supported-intersection method with bounded process-group cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = PACKAGE_ROOT.parents[1]
MANIFEST_PATH = PACKAGE_ROOT / "method_manifest.json"
LAUNCH_PATH = PACKAGE_ROOT / "launch" / "serial_baseline.launch"
VERIFY_PATH = PACKAGE_ROOT / "tools" / "verify_profiles.py"
PYTHON = Path("/usr/bin/python3")
TIME = Path("/usr/bin/time")
TASKSET = Path("/usr/bin/taskset")
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
ESTIMATOR_DIED_PATTERN = re.compile(
    r"\[ov_msckf(?:-\d+)?\]\s+process has died .*?exit code (-?\d+)",
    re.IGNORECASE,
)
ESTIMATOR_REQUIRED_DIED_PATTERN = re.compile(
    r"REQUIRED\s+process\s+\[ov_msckf(?:-\d+)?\]\s+has died!"
    r".{0,4096}?process has died\s+\[[^\]\r\n]{0,2048}?"
    r"exit code\s+(-?\d+)",
    re.IGNORECASE | re.DOTALL,
)
ESTIMATOR_CLEAN_PATTERN = re.compile(
    r"\[ov_msckf(?:-\d+)?\]\s+process has finished cleanly",
    re.IGNORECASE,
)


class RunError(RuntimeError):
    pass


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
        raise RunError(
            f"SHA-256 timed out after {timeout_seconds}s: {path}"
        ) from exc
    if result.returncode != 0:
        raise RunError(f"SHA-256 failed for {path}: {result.stderr.strip()}")
    actual = result.stdout.split(maxsplit=1)[0].lower()
    if not HASH_PATTERN.fullmatch(actual):
        raise RunError(f"invalid SHA-256 output for {path}: {result.stdout!r}")
    return actual


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def checked_output(argv: Sequence[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RunError(
            f"command failed ({result.returncode}): {list(argv)!r}: "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def git_output(repo: Path, *args: str) -> str:
    return checked_output(("git", "-C", str(repo), *args))


def load_method(method_id: str) -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for method in manifest["methods"]:
        if method["id"] == method_id:
            return method
    raise RunError(f"unknown method {method_id}")


def validate_source(method: dict[str, Any], source_repo: Path) -> dict[str, Any]:
    if not (source_repo / ".git").exists():
        # Linked worktrees use a .git file, while ordinary clones use a folder.
        if not (source_repo / ".git").is_file():
            raise RunError(f"not a Git worktree: {source_repo}")
    head = git_output(source_repo, "rev-parse", "HEAD")
    expected = method["commit"]
    status = git_output(source_repo, "status", "--porcelain=v1")

    if method["id"] in ("U-NS", "OV-SCHUR"):
        if head != expected:
            raise RunError(f"{method['id']} HEAD {head} != frozen {expected}")
        if status:
            raise RunError(f"{method['id']} external source is dirty")
    else:
        ancestor = subprocess.run(
            ("git", "-C", str(source_repo), "merge-base", "--is-ancestor", expected, head),
            check=False,
            timeout=15,
        )
        if ancestor.returncode != 0:
            raise RunError(f"local algorithm freeze {expected} is not an ancestor of {head}")
        estimator_diff = subprocess.run(
            (
                "git",
                "-C",
                str(source_repo),
                "diff",
                "--quiet",
                expected,
                "--",
                "ov_core",
                "ov_init",
                "ov_msckf",
            ),
            check=False,
            timeout=15,
        )
        if estimator_diff.returncode != 0:
            raise RunError("local estimator source differs from the algorithm freeze")
        tracked_source_status = git_output(
            source_repo,
            "status",
            "--porcelain=v1",
            "--",
            "ov_core",
            "ov_init",
            "ov_msckf",
        )
        if tracked_source_status:
            raise RunError("local estimator source has uncommitted changes")

    return {
        "head": head,
        "expected_algorithm_commit": expected,
        "dirty": bool(status),
        "status": status.splitlines(),
        "remote_v": git_output(source_repo, "remote", "-v").splitlines(),
    }


def validate_workspace(setup: Path, source_repo: Path) -> tuple[Path, Path]:
    if not setup.is_file():
        raise RunError(f"workspace setup file does not exist: {setup}")
    prefix = setup.parent
    estimator = prefix / "lib" / "ov_msckf" / "ros1_serial_msckf"
    if not estimator.is_file() or not os.access(estimator, os.X_OK):
        raise RunError(f"serial estimator binary missing or not executable: {estimator}")

    source_script = 'source "$1"; rospack find ov_msckf'
    package_path = checked_output(
        (
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            source_script,
            "baseline-triad-rospack",
            str(setup),
        )
    )
    expected_package = (source_repo / "ov_msckf").resolve()
    if Path(package_path).resolve() != expected_package:
        raise RunError(
            f"workspace resolves ov_msckf to {package_path}, expected {expected_package}"
        )
    return prefix, estimator


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


def terminate_group(pgid: int, grace_seconds: float) -> None:
    if not group_alive(pgid):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not group_alive(pgid):
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return


def output_hashes(output: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name not in {"result.json"}:
            result[path.name] = sha256(path)
    return result


def estimator_child_exit(log_path: Path) -> int | None:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    required_died = ESTIMATOR_REQUIRED_DIED_PATTERN.findall(text)
    if required_died:
        return int(required_died[-1])
    died = ESTIMATOR_DIED_PATTERN.findall(text)
    if died:
        return int(died[-1])
    if ESTIMATOR_CLEAN_PATTERN.search(text):
        return 0
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--method", required=True, choices=("U-NS", "L-NS", "L-SCHUR", "OV-SCHUR"))
    parser.add_argument("--workspace-setup", required=True, type=Path)
    parser.add_argument("--source-repo", required=True, type=Path)
    parser.add_argument("--bag", required=True, type=Path)
    parser.add_argument("--bag-sha256", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cpu-list", required=True)
    parser.add_argument("--ros-master-port", required=True, type=int)
    parser.add_argument("--timeout-seconds", required=True, type=float)
    parser.add_argument("--termination-grace-seconds", type=float, default=15.0)
    parser.add_argument("--bag-hash-timeout-seconds", type=float, default=1800.0)
    parser.add_argument(
        "--bag-duration-seconds",
        type=float,
        default=-1.0,
        help="dataset-time duration; -1 processes to the end, positive values are smoke runs",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    method = load_method(args.method)
    source_repo = args.source_repo.resolve()
    setup = args.workspace_setup.resolve()
    bag = args.bag.resolve()
    output = args.output_dir.resolve()
    config = (PACKAGE_ROOT / method["config"]).resolve()

    try:
        if not all(path.is_file() for path in (PYTHON, TIME, TASKSET, LAUNCH_PATH, VERIFY_PATH, config)):
            raise RunError("required runner executable/config/launch is missing")
        if not bag.is_file():
            raise RunError(f"bag does not exist: {bag}")
        if not HASH_PATTERN.fullmatch(args.bag_sha256.lower()):
            raise RunError("--bag-sha256 must be a lowercase 64-digit SHA-256")
        if (
            args.timeout_seconds <= 0
            or args.termination_grace_seconds <= 0
            or args.bag_hash_timeout_seconds <= 0
        ):
            raise RunError("run timeout, hash timeout, and termination grace must be positive")
        if args.bag_duration_seconds == 0 or args.bag_duration_seconds < -1:
            raise RunError("bag duration must be -1 or positive")
        if not 1024 <= args.ros_master_port <= 65535:
            raise RunError("ROS master port must be in [1024, 65535]")
        if is_within(output, REPOSITORY_ROOT):
            raise RunError("generated run output must be outside the Git worktree")
        if output.exists():
            raise RunError(f"refusing to overwrite existing output directory: {output}")

        verify = subprocess.run(
            (str(PYTHON), str(VERIFY_PATH)),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
        )
        if verify.returncode != 0:
            raise RunError(f"profile verification failed: {verify.stderr.strip()}")

        provenance = validate_source(method, source_repo)
        _, estimator_binary = validate_workspace(setup, source_repo)
        require_free_port(args.ros_master_port)
        actual_bag_sha256 = bounded_sha256(bag, args.bag_hash_timeout_seconds)
        if actual_bag_sha256 != args.bag_sha256.lower():
            raise RunError(
                f"bag SHA-256 {actual_bag_sha256} != supplied {args.bag_sha256.lower()}"
            )

        output.mkdir(parents=True, exist_ok=False)
        state_path = output / "state_estimate.txt"
        deviation_path = output / "state_deviation.txt"
        timing_path = output / "timing.csv"
        log_path = output / "run.log"
        resource_path = output / "resource_usage.txt"
        ros_home = output / "ros_home"
        ros_log_dir = output / "ros_logs"
        ros_home.mkdir()
        ros_log_dir.mkdir()

        launch_args = (
            "roslaunch",
            str(LAUNCH_PATH),
            f"config_path:={config}",
            f"bag:={bag}",
            "bag_start:=0.0",
            f"bag_durr:={args.bag_duration_seconds}",
            "verbosity:=INFO",
            "dotime:=true",
            f"path_est:={state_path}",
            f"path_std:={deviation_path}",
            f"path_time:={timing_path}",
        )
        shell_script = 'source "$1"; shift; exec "$@"'
        command = (
            str(TIME),
            "--verbose",
            "--output",
            str(resource_path),
            str(TASKSET),
            "--cpu-list",
            args.cpu_list,
            "/bin/bash",
            "--noprofile",
            "--norc",
            "-c",
            shell_script,
            "baseline-triad-runner",
            str(setup),
            *launch_args,
        )

        environment = os.environ.copy()
        environment.update(
            {
                "ROS_MASTER_URI": f"http://127.0.0.1:{args.ros_master_port}",
                "ROS_HOSTNAME": "127.0.0.1",
                "ROS_IP": "127.0.0.1",
                "ROS_HOME": str(ros_home),
                "ROS_LOG_DIR": str(ros_log_dir),
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
        started_utc = utc_now()
        command_record = {
            "method": args.method,
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
            "started_utc": started_utc,
            "timeout_seconds": args.timeout_seconds,
            "termination_grace_seconds": args.termination_grace_seconds,
            "bag_duration_seconds": args.bag_duration_seconds,
            "bag_start_seconds": 0.0,
            "cpu_list": args.cpu_list,
            "config_sha256": sha256(config),
            "bag_sha256": actual_bag_sha256,
            "bag_hash_timeout_seconds": args.bag_hash_timeout_seconds,
            "bag_size_bytes": bag.stat().st_size,
            "estimator_binary_sha256": sha256(estimator_binary),
            "source": provenance,
        }
        (output / "command.json").write_text(
            json.dumps(command_record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        timed_out = False
        interrupted_signal: int | None = None
        exit_code: int | None = None
        start_monotonic = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        with log_path.open("wb") as log_stream:
            received_signal: int | None = None

            def interrupt_handler(signum: int, _frame: Any) -> None:
                nonlocal received_signal
                received_signal = signum
                raise KeyboardInterrupt

            prior_handlers = {
                signum: signal.getsignal(signum)
                for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
            }
            for signum in prior_handlers:
                signal.signal(signum, interrupt_handler)
            try:
                process = subprocess.Popen(
                    command,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                )
                try:
                    exit_code = process.wait(timeout=args.timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    terminate_group(process.pid, args.termination_grace_seconds)
                    process.wait(timeout=args.termination_grace_seconds + 5)
                    exit_code = 124
                except KeyboardInterrupt:
                    interrupted_signal = received_signal or signal.SIGINT
                    terminate_group(process.pid, args.termination_grace_seconds)
                    process.wait(timeout=args.termination_grace_seconds + 5)
                    exit_code = 128 + int(interrupted_signal)
            finally:
                if process is not None:
                    terminate_group(process.pid, args.termination_grace_seconds)
                    if process.poll() is None:
                        process.wait(timeout=5)
                for signum, prior_handler in prior_handlers.items():
                    signal.signal(signum, prior_handler)

        finished_utc = utc_now()
        child_exit = estimator_child_exit(log_path)
        outputs_exist = (
            state_path.is_file()
            and deviation_path.is_file()
            and timing_path.is_file()
        )
        completed = bool(
            exit_code == 0
            and not timed_out
            and interrupted_signal is None
            and child_exit in (None, 0)
            and outputs_exist
        )
        if timed_out:
            effective_exit_code = 124
        elif interrupted_signal is not None:
            effective_exit_code = 128 + int(interrupted_signal)
        elif child_exit not in (None, 0):
            effective_exit_code = 1
        elif exit_code != 0:
            effective_exit_code = int(exit_code or 1)
        elif not outputs_exist:
            effective_exit_code = 1
        else:
            effective_exit_code = 0
        (output / "exit_status.txt").write_text(
            f"{effective_exit_code}\n", encoding="utf-8"
        )
        (output / "timed_out.txt").write_text(
            f"{1 if timed_out else 0}\n", encoding="utf-8"
        )
        result = {
            "method": args.method,
            "roslaunch_exit_code": exit_code,
            "estimator_child_exit": child_exit,
            "effective_exit_code": effective_exit_code,
            "completed": completed,
            "timed_out": timed_out,
            "interrupted_signal": interrupted_signal,
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "elapsed_wall_seconds": time.monotonic() - start_monotonic,
            "state_output_exists": state_path.is_file(),
            "deviation_output_exists": deviation_path.is_file(),
            "timing_output_exists": timing_path.is_file(),
            "coverage_completed": None,
            "coverage_assessment": "evaluate state timestamps against requested bag interval",
            "output_sha256": output_hashes(output),
        }
        (output / "result.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return effective_exit_code
    except (OSError, RunError, subprocess.SubprocessError, ValueError) as exc:
        print(f"baseline triad run failed before completion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
