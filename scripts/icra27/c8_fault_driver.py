#!/usr/bin/env python3
"""C8 fault-injection driver (DESKTOP_EVIDENCE_SESSION v2, Item 1 / ledger F4).

Replays the FROZEN estimator binary over the frozen CDSC-1R4 matrix sequences
(EuRoC + KAIST) with the C8 LD_PRELOAD shim attached, mirroring the frozen
trial runner's roscore/roslaunch argv and minimal environment exactly. The
frozen trial runner itself refuses LD_PRELOAD by design (pinned runtime
identity guard), so this driver reproduces its launch recipe rather than
patching that guard; every run records the shim identity explicitly.

Run kinds:
  capture  shim attached, no injection  -> inertness gate vs CDSC-1R4 hashes
  cycle    classes {1,2,3,4,5,6,8} cycling every C8_CYCLE_STRIDE-th update
  single   one class, every --period-th update (optional supplement)
  t0       class 7: TurnSafe T0 diagnostics stream fault at one stage/kind
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

REPO = Path("/home/moksh/schurvio-lite-rotation-robustness")
MATRIX = REPO / "project" / "icra27_cross_dataset_matrix.yaml"
BINARY = REPO / "build" / "cp0-ws" / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"
LIB = REPO / "build" / "cp0-ws" / "devel" / "lib" / "libov_msckf_lib.so"
SHIM = REPO / "tools" / "c8_faultinj" / "libc8_faultinj_shim.so"
FROZEN_BINARY_SHA256 = "0e46fa3e6ced2ff3eee568f6401ede3399e1a6f93e392c80db7a634cb50c700e"
CDSC_ROOT = Path(
    "/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z/scored"
)
KAIST_LAUNCH = REPO / "project" / "rotation_robustness_serial.launch"
KAIST_CONFIG = REPO / "config" / "kaist_vio_rotation_robustness" / "estimator_config.yaml"
EUROC_LAUNCH = REPO / "project" / "icra27_cross_dataset_s1_serial.launch"
EUROC_CONFIG = REPO / "config" / "euroc_mav" / "estimator_config.yaml"
YIELD = REPO / "scripts" / "icra27" / "c8_yield_check.sh"

ALLOWED_ENVIRONMENT = (
    "CMAKE_PREFIX_PATH", "LD_LIBRARY_PATH", "LOGNAME", "PATH", "PKG_CONFIG_PATH", "PYTHONPATH",
    "ROS_DISTRO", "ROS_ETC_DIR", "ROS_PACKAGE_PATH", "ROS_PYTHON_VERSION", "ROS_ROOT", "ROS_VERSION",
    "SHELL", "TMPDIR", "USER",
)
FIXED_ENVIRONMENT = {
    "CUDA_VISIBLE_DEVICES": "", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
    "PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "MPLBACKEND": "Agg",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_matrix() -> List[Dict[str, Any]]:
    m = yaml.safe_load(open(MATRIX))
    return [s for s in m["sequences"] if s["dataset"] in ("euroc_mav", "kaist_vio")]


def seq_slug(seq: Dict[str, Any]) -> str:
    return "{:02d}-{}".format(seq["order"], seq["sequence"].replace("/", "_"))


def reference_cell(seq: Dict[str, Any]) -> Optional[Path]:
    d = CDSC_ROOT / seq_slug(seq) / "S1"
    if not d.is_dir():
        return None
    runs = sorted(p for p in d.iterdir() if p.is_dir())
    return runs[0] if runs else None


def port_open(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.1)
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def build_env(run_dir: Path, port: int, extra: Dict[str, str]) -> Dict[str, str]:
    env = {k: os.environ[k] for k in ALLOWED_ENVIRONMENT if k in os.environ}
    env.update(FIXED_ENVIRONMENT)
    env.update({
        "HOME": str(run_dir / "isolated-home"),
        "ROS_HOME": str(run_dir / "ros-home"),
        "ROS_LOG_DIR": str(run_dir / "ros-logs"),
        "ROS_HOSTNAME": "127.0.0.1",
        "ROS_IP": "127.0.0.1",
        "ROS_MASTER_URI": "http://127.0.0.1:{}".format(port),
    })
    env.update(extra)
    return dict(sorted(env.items()))


T0_CONFIG_DIR = REPO / "tools" / "c8_faultinj" / "config_t0"


def materialize_t0_config(seq: Dict[str, Any], run_dir: Path) -> Path:
    """Copy the class-7 study config (frozen config + T0 capture keys) into the
    run dir, binding the T0 output path to this run. Returns the config path."""
    name = "kaist_vio_rotation_robustness" if seq["dataset"] == "kaist_vio" else "euroc_mav"
    src = T0_CONFIG_DIR / name
    dst = run_dir / "config" / name
    dst.mkdir(parents=True)
    out_path = run_dir / "diagnostics" / "turnsafe_t0.jsonl"
    for f in sorted(src.iterdir()):
        text = f.read_text()
        text = text.replace("__T0_OUTPUT__", str(out_path))
        (dst / f.name).write_text(text)
    return dst / "estimator_config.yaml"


def launch_argv(seq: Dict[str, Any], run_dir: Path, config_override: Optional[Path] = None) -> List[str]:
    traj = run_dir / "trajectory"
    diag = run_dir / "diagnostics"
    if seq["dataset"] == "kaist_vio":
        return [
            "/opt/ros/noetic/bin/roslaunch", str(KAIST_LAUNCH),
            "bag:={}".format(seq["bag"]["path"]),
            "candidate_config:={}".format(config_override or KAIST_CONFIG),
            "bag_start:={}".format(int(seq["bag_start_seconds"])),
            "path_state:={}".format(traj / "state_estimate.txt"),
            "path_std:={}".format(traj / "state_deviation.txt"),
            "path_time:={}".format(diag / "timing_openvins.csv"),
            "verbosity:=INFO",
        ]
    bs = seq["bag_start_seconds"]
    bs_str = str(int(bs)) if float(bs).is_integer() else str(bs)
    return [
        "/opt/ros/noetic/bin/roslaunch", str(EUROC_LAUNCH),
        "config_path:={}".format(config_override or EUROC_CONFIG),
        "bag:={}".format(seq["bag"]["path"]),
        "bag_start:={}".format(bs_str),
        "bag_durr:=-1",
        "path_state:={}".format(traj / "state_estimate.txt"),
        "path_std:={}".format(traj / "state_deviation.txt"),
        "path_time:={}".format(diag / "timing_openvins.csv"),
        "record_timing:=true",
    ]


def kill_group(proc: subprocess.Popen, timeout: float = 20.0) -> Dict[str, Any]:
    sent = []
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGINT)
            sent.append("SIGINT")
        except ProcessLookupError:
            pass
        t0 = time.monotonic()
        while proc.poll() is None and time.monotonic() - t0 < timeout:
            time.sleep(0.2)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            sent.append("SIGKILL")
        except ProcessLookupError:
            pass
        proc.wait(timeout=10)
    return {"exit_code": proc.returncode, "signals_sent": sent}


def run_one(seq: Dict[str, Any], kind: str, run_root: Path, port: int, cpu_list: str,
            shim_env: Dict[str, str], run_id: str, timeout_s: float, run_log: Optional[Path],
            t0_config: bool = False, console_devfull: bool = False) -> Dict[str, Any]:
    run_dir = run_root / run_id
    if run_dir.exists():
        raise SystemExit("refusing to overwrite existing run dir {}".format(run_dir))
    for sub in ("trajectory", "diagnostics", "isolated-home", "ros-home", "ros-logs"):
        (run_dir / sub).mkdir(parents=True)
    shim_log = run_dir / "diagnostics" / "shim.jsonl"
    extra = dict(shim_env)
    extra["LD_PRELOAD"] = str(SHIM)
    extra["C8_LOG"] = str(shim_log)
    env = build_env(run_dir, port, extra)
    if port_open(port):
        raise SystemExit("ROS port {} busy".format(port))
    if run_log is not None:
        subprocess.run([str(YIELD), str(run_log), run_id], check=False)
    config_override = materialize_t0_config(seq, run_dir) if t0_config else None

    rec: Dict[str, Any] = {
        "schema": "schurvio.icra27.c8.fault_run.v1",
        "run_id": run_id, "kind": kind, "dataset": seq["dataset"], "sequence": seq["sequence"],
        "order": seq["order"], "bag": seq["bag"], "bag_start_seconds": seq["bag_start_seconds"],
        "started_utc": utc(),
        "binary": {"path": str(BINARY), "sha256": sha256_file(BINARY), "frozen_sha256": FROZEN_BINARY_SHA256},
        "library": {"path": str(LIB), "sha256": sha256_file(LIB)},
        "shim": {"path": str(SHIM), "sha256": sha256_file(SHIM), "env": {k: v for k, v in extra.items() if k != "LD_PRELOAD"}},
        "config": {"path": str(config_override or (KAIST_CONFIG if seq["dataset"] == "kaist_vio" else EUROC_CONFIG)),
                   "frozen_path": str(KAIST_CONFIG if seq["dataset"] == "kaist_vio" else EUROC_CONFIG),
                   "study_delta": "turnsafe_t0_capture=true + turnsafe_t0_output_path (class-7 study config)" if t0_config else None},
        "console_devfull": console_devfull,
        "launch": {"path": str(KAIST_LAUNCH if seq["dataset"] == "kaist_vio" else EUROC_LAUNCH)},
        "runtime_environment": env, "ros_port": port, "cpu_list": cpu_list,
    }
    rec["config"]["sha256"] = sha256_file(Path(rec["config"]["path"]))
    rec["config"]["frozen_sha256"] = sha256_file(Path(rec["config"]["frozen_path"]))
    rec["launch"]["sha256"] = sha256_file(Path(rec["launch"]["path"]))
    if rec["binary"]["sha256"] != FROZEN_BINARY_SHA256:
        raise SystemExit("frozen binary hash mismatch")

    roscore_log = open(run_dir / "diagnostics" / "roscore.log", "wb")
    roscore = subprocess.Popen(["/opt/ros/noetic/bin/roscore", "-p", str(port)], env=env,
                               stdout=roscore_log, stderr=subprocess.STDOUT, start_new_session=True,
                               cwd=str(REPO))
    t0 = time.monotonic()
    while not port_open(port) and time.monotonic() - t0 < 30:
        time.sleep(0.2)
    if not port_open(port):
        kill_group(roscore)
        raise SystemExit("roscore did not come up")

    argv = ["/usr/bin/time", "--verbose", "--output={}".format(run_dir / "diagnostics" / "resource_usage.txt"),
            "--", "/usr/bin/taskset", "--cpu-list", cpu_list] + launch_argv(seq, run_dir, config_override)
    rec["estimator_argv"] = argv
    # class 7a: every console write of the estimator process tree fails (ENOSPC)
    console = open("/dev/full" if console_devfull else run_dir / "diagnostics" / "console.log", "wb")
    t_start = time.monotonic()
    est = subprocess.Popen(argv, env=env, stdout=console, stderr=subprocess.STDOUT,
                           start_new_session=True, cwd=str(REPO))
    timed_out = False
    try:
        est.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(est)
    rec["estimator"] = {"exit_code": est.returncode, "timed_out": timed_out,
                        "duration_seconds": time.monotonic() - t_start}
    console.close()
    rec["roscore_close"] = kill_group(roscore)
    roscore_log.close()

    # Products
    products = {}
    for rel in ("trajectory/state_estimate.txt", "trajectory/state_deviation.txt",
                "diagnostics/timing_openvins.csv", "diagnostics/console.log", "diagnostics/shim.jsonl",
                "diagnostics/turnsafe_t0.jsonl", "diagnostics/turnsafe_t0.jsonl.tmp"):
        p = run_dir / rel
        products[rel] = {"present": p.is_file(), "sha256": sha256_file(p) if p.is_file() else None,
                         "size_bytes": p.stat().st_size if p.is_file() else None}
    rec["products"] = products

    # Reference comparison (CDSC-1R4 S1 scored cell)
    ref = reference_cell(seq)
    cmp: Dict[str, Any] = {"reference_run_directory": str(ref) if ref else None}
    if ref is not None:
        for rel in ("trajectory/state_estimate.txt", "trajectory/state_deviation.txt"):
            rp = ref / rel
            cmp[rel] = {"reference_sha256": sha256_file(rp) if rp.is_file() else None,
                        "fresh_sha256": products[rel]["sha256"],
                        "byte_identical": rp.is_file() and products[rel]["present"]
                        and sha256_file(rp) == products[rel]["sha256"]}
        cmp["status"] = "BYTE_IDENTICAL" if all(cmp[r]["byte_identical"] for r in
                                                ("trajectory/state_estimate.txt", "trajectory/state_deviation.txt")) else "DIFFERENT"
    rec["reference_comparison"] = cmp
    rec["finished_utc"] = utc()

    # remove empty isolation dirs content we do not need to keep large (ros logs kept)
    with open(run_dir / "run_record.json", "w") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    with open(run_dir / "SHA256SUMS", "w") as f:
        for rel in sorted(products):
            if products[rel]["present"]:
                f.write("{}  {}\n".format(products[rel]["sha256"], rel))
        f.write("{}  {}\n".format(sha256_file(run_dir / "run_record.json"), "run_record.json"))
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="C8 artifact root")
    ap.add_argument("--kind", required=True, choices=("capture", "cycle", "single", "t0"))
    ap.add_argument("--orders", default="", help="comma list of matrix orders (default: all EuRoC+KAIST)")
    ap.add_argument("--port", type=int, default=18400)
    ap.add_argument("--cpu-list", default="8-15")
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--stride", type=int, default=8, help="cycle mode stride")
    ap.add_argument("--cls", type=int, default=0)
    ap.add_argument("--period", type=int, default=0)
    ap.add_argument("--phase", type=int, default=0)
    ap.add_argument("--t0-stage", type=int, default=-1)
    ap.add_argument("--t0-kind", type=int, default=2)
    ap.add_argument("--t0-at", type=int, default=200)
    ap.add_argument("--tag", default="")
    ap.add_argument("--run-log", default="")
    ap.add_argument("--t0-config", action="store_true", help="use the class-7 study config (T0 stream enabled)")
    ap.add_argument("--console-devfull", action="store_true", help="class 7a: estimator console writes fail (/dev/full)")
    args = ap.parse_args()

    root = Path(args.root)
    seqs = load_matrix()
    if args.orders:
        want = {int(x) for x in args.orders.split(",")}
        seqs = [s for s in seqs if s["order"] in want]
    shim_env = {"C8_MODE": {"capture": "capture", "cycle": "cycle", "single": "single", "t0": "capture"}[args.kind]}
    if args.kind == "cycle":
        shim_env["C8_CYCLE_STRIDE"] = str(args.stride)
    if args.kind == "single":
        shim_env.update({"C8_CLASS": str(args.cls), "C8_PERIOD": str(args.period), "C8_PHASE": str(args.phase)})
    if args.kind == "t0":
        shim_env.update({"C8_T0_STAGE": str(args.t0_stage), "C8_T0_KIND": str(args.t0_kind), "C8_T0_AT": str(args.t0_at)})
    run_log = Path(args.run_log) if args.run_log else None
    tag = args.tag or args.kind
    for seq in seqs:
        run_id = "c8-{}-{}-{}-a1".format(seq_slug(seq), "s1", tag)
        run_root = root / "cells" / tag / seq_slug(seq) / "S1"
        run_root.mkdir(parents=True, exist_ok=True)
        rec = run_one(seq, args.kind, run_root, args.port, args.cpu_list, shim_env, run_id, args.timeout, run_log,
                      t0_config=args.t0_config or args.kind == "t0", console_devfull=args.console_devfull)
        line = "- {} c8-run {} kind={} exit={} dur={:.1f}s ref={} shim_lines={}".format(
            utc(), run_id, args.kind, rec["estimator"]["exit_code"], rec["estimator"]["duration_seconds"],
            rec["reference_comparison"].get("status"), rec["products"]["diagnostics/shim.jsonl"]["size_bytes"])
        print(line, flush=True)
        if run_log is not None:
            with open(run_log, "a") as f:
                f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
