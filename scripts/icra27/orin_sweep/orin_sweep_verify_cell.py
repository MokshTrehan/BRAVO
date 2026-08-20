#!/usr/bin/env python3
"""ORIN-SWEEP-1 per-cell machine verification (runs on the Orin HOST after a cell).

Writes into <cell>/diagnostics/:
  cell_delta.json   resolved budget + elimination flag, override evidence, config/launch/binary hashes
  cell_summary.json per-callback latency stats, deadline ledger at the native period, RSS, rows
  thermal.json      SoC temperature maxima and throttle verdict over the cell window
Prints one RUN_LOG line. Exit 0 always (failures are data); verdicts live in the JSON.
"""
import argparse, hashlib, json, os, re, sys, time
from pathlib import Path

import yaml
import numpy as np

NATIVE_PERIOD_MS = {"kaist": 1000.0 / 30.0, "euroc": 50.0}
FROZEN_NUM_PTS = 200
FROZEN_MODE = "schur"
REPO_MAP = ("/repo", "/data/schurvio-lite-frozen")
BINARY = "/data/schurvio-lite-frozen/build/cp0-ws/devel/lib/ov_msckf/ros1_serial_msckf"
CPU_PASSIVE_TRIP_MC = 70000
FAN_DEVICE = "pwm-fan"


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def flatten(d, prefix=""):
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            out.update(flatten(v, prefix + "/" + str(k)))
    else:
        out[prefix] = d
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cell", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--dataset", required=True, choices=["kaist", "euroc"])
    ap.add_argument("--budget", required=True, help="grid budget or C5 (gate replay, no override)")
    ap.add_argument("--mode", required=True, help="S1|N0|C5")
    ap.add_argument("--launch", required=True, type=Path)
    ap.add_argument("--cooling", required=True, type=Path, help="host cooling/thermal sampler log")
    ap.add_argument("--t-start", required=True, type=float)
    ap.add_argument("--t-end", required=True, type=float)
    a = ap.parse_args()
    diag = a.cell / "diagnostics"
    diag.mkdir(exist_ok=True)
    problems = []

    # ---- resolved deltas -------------------------------------------------
    exp_mode = {"S1": "schur", "N0": "nullspace", "C5": "schur"}[a.mode]
    exp_budget = FROZEN_NUM_PTS if a.budget == "C5" else int(a.budget)
    resolved_path = diag / "resolved_ros_parameters.yaml"
    flat = {}
    if resolved_path.is_file():
        try:
            flat = flatten(yaml.safe_load(resolved_path.read_text()) or {})
        except Exception as exc:  # noqa
            problems.append("resolved_params_unreadable:%s" % exc)
    else:
        problems.append("resolved_params_missing")
    def pick(suffix):
        hits = {k: v for k, v in flat.items() if k.endswith("/" + suffix)}
        return hits
    np_hits = pick("num_pts")
    el_hits = pick("up_msckf_landmark_elimination")
    cp_hits = pick("config_path")
    resolved_num_pts = list(np_hits.values())[0] if len(np_hits) == 1 else None
    resolved_mode = list(el_hits.values())[0] if len(el_hits) == 1 else None
    config_path = list(cp_hits.values())[0] if len(cp_hits) == 1 else None
    console = (a.cell / "console.log").read_text(errors="replace") if (a.cell / "console.log").is_file() else ""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", console)
    override_num_pts = "overriding node num_pts with value from ROS!" in clean
    override_mode = "overriding node up_msckf_landmark_elimination with value from ROS!" in clean
    cfg_host = config_path.replace(REPO_MAP[0], REPO_MAP[1], 1) if config_path else None
    cfg_sha = sha(cfg_host) if cfg_host and os.path.isfile(cfg_host) else None
    cfg_num_pts = None
    if cfg_sha:
        m = re.search(r"^num_pts:\s*(\d+)", Path(cfg_host).read_text(), re.M)
        cfg_num_pts = int(m.group(1)) if m else None
    if a.budget == "C5":
        budget_ok = (resolved_num_pts is None) and (cfg_num_pts == FROZEN_NUM_PTS) and not override_num_pts
        effective_budget = cfg_num_pts
    else:
        budget_ok = (resolved_num_pts == exp_budget) and override_num_pts
        effective_budget = resolved_num_pts
    mode_ok = (resolved_mode == exp_mode) and override_mode
    # the only other tuning-capable params must sit at frozen values
    passes = pick("up_msckf_max_visual_passes")
    passes_ok = list(passes.values()) == [1]
    delta = {
        "schema": "schurvio.icra27.orin_sweep.cell_delta.v1",
        "cell": a.name, "dataset": a.dataset, "requested_budget": a.budget, "requested_mode": a.mode,
        "expected_num_pts": exp_budget, "expected_elimination": exp_mode,
        "resolved_num_pts_ros": resolved_num_pts, "config_num_pts_yaml": cfg_num_pts,
        "effective_num_pts": effective_budget,
        "resolved_elimination_ros": resolved_mode,
        "console_override_num_pts": override_num_pts, "console_override_elimination": override_mode,
        "up_msckf_max_visual_passes": list(passes.values()),
        "config_path": config_path, "config_sha256": cfg_sha,
        "launch_path": str(a.launch), "launch_sha256": sha(a.launch) if a.launch.is_file() else None,
        "binary_path": BINARY, "binary_sha256": sha(BINARY) if os.path.isfile(BINARY) else None,
        "budget_verified": bool(budget_ok), "mode_verified": bool(mode_ok), "passes_verified": bool(passes_ok),
        "delta_verified": bool(budget_ok and mode_ok and passes_ok),
    }
    (diag / "cell_delta.json").write_text(json.dumps(delta, indent=1, sort_keys=True) + "\n")

    # ---- timing / deadline ledger ---------------------------------------
    period = NATIVE_PERIOD_MS[a.dataset]
    tpath = diag / "timing_openvins.csv"
    stats = {"n_callbacks": 0}
    if tpath.is_file():
        rows = []
        for line in tpath.read_text().splitlines():
            if not line or line.startswith("#"):
                continue
            parts = line.split(",")
            try:
                rows.append([float(x) for x in parts])
            except ValueError:
                continue
        if rows:
            arr = np.asarray(rows)
            total = arr[:, -1] * 1000.0
            track = arr[:, 1] * 1000.0
            upd = (arr[:, 3] + arr[:, 4] + arr[:, 5]) * 1000.0
            miss = int(np.sum(total > period))
            stats = {
                "n_callbacks": int(len(total)),
                "first_callback_ts": float(arr[0, 0]), "last_callback_ts": float(arr[-1, 0]),
                "total_ms": {"p50": float(np.percentile(total, 50)), "p95": float(np.percentile(total, 95)),
                             "p99": float(np.percentile(total, 99)), "max": float(total.max()), "mean": float(total.mean()),
                             "sum_s": float(total.sum() / 1000.0)},
                "tracking_ms_p50": float(np.percentile(track, 50)),
                "update_ms_p50": float(np.percentile(upd, 50)),
                "native_period_ms": period,
                "deadline_misses": miss, "deadline_compliance_pct": 100.0 * (len(total) - miss) / len(total),
                "percentile_method": "numpy.percentile linear interpolation; miss := total > native period (strict)",
            }
    else:
        problems.append("timing_missing")
    ru = diag / "resource_usage.txt"
    rss_kb = None; elapsed = None; exit_status = None
    if ru.is_file():
        t = ru.read_text()
        m = re.search(r"Maximum resident set size \(kbytes\): (\d+)", t); rss_kb = int(m.group(1)) if m else None
        m = re.search(r"Elapsed \(wall clock\) time.*: ([\d:.]+)", t)
        if m:
            p = m.group(1).split(":"); elapsed = sum(float(x) * 60 ** i for i, x in enumerate(reversed(p)))
        m = re.search(r"Exit status: (\d+)", t); exit_status = int(m.group(1)) if m else None
    inv = diag / "invocation.txt"
    roslaunch_exit = None
    if inv.is_file():
        m = re.search(r"roslaunch_exit=(\d+)", inv.read_text()); roslaunch_exit = int(m.group(1)) if m else None
    est = a.cell / "trajectory" / "state_estimate.txt"
    dev = a.cell / "trajectory" / "state_deviation.txt"
    est_rows = sum(1 for l in est.read_text().splitlines() if l and not l.startswith("#")) if est.is_file() else 0
    summary = {
        "schema": "schurvio.icra27.orin_sweep.cell_summary.v1", "cell": a.name,
        "timing": stats, "peak_rss_kb": rss_kb, "wall_elapsed_s": elapsed, "time_exit_status": exit_status,
        "roslaunch_exit": roslaunch_exit, "state_rows": est_rows,
        "state_estimate_sha256": sha(est) if est.is_file() else None,
        "state_deviation_sha256": sha(dev) if dev.is_file() else None,
        "host_t_start": a.t_start, "host_t_end": a.t_end, "host_wall_s": a.t_end - a.t_start,
        "problems": problems,
        "status": "COMPLETED" if (roslaunch_exit == 0 and est_rows > 0 and stats.get("n_callbacks", 0) > 0) else "FAILED",
    }
    (diag / "cell_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")

    # ---- thermal / throttle --------------------------------------------
    th = {"schema": "schurvio.icra27.orin_sweep.cell_thermal.v1", "cell": a.name, "samples": 0,
          "throttle_samples": 0, "cpu_ge_passive_trip_samples": 0, "max_temp_mC": {}, "verdict": "NO_SAMPLES",
          "rule": "INVALID_THERMAL iff any sample in [t_start, t_end+13s] has a non-fan cooling_device cur_state>0 "
                  "or cpu-thermal >= 70000 mC (passive trip_point_2)"}
    if a.cooling.is_file():
        hdr = None; maxes = {}; n = 0; thr = 0; hot = 0; thr_devs = set()
        for line in a.cooling.read_text().splitlines():
            if line.startswith("#"):
                hdr = line[1:].split(); continue
            f = line.split()
            if not hdr or len(f) != len(hdr):
                continue
            try:
                ts = float(f[0])
            except ValueError:
                continue
            if ts < a.t_start or ts > a.t_end + 13.0:
                continue
            n += 1
            for k, v in zip(hdr[1:], f[1:]):
                if v in ("NA", ""):
                    continue
                iv = int(float(v))
                if k.startswith("temp:"):
                    maxes[k[5:]] = max(maxes.get(k[5:], -1), iv)
                elif k.startswith("cd:") and FAN_DEVICE not in k and iv > 0:
                    thr += 1; thr_devs.add(k[3:])
            try:
                cidx = hdr.index("temp:cpu-thermal")
                if int(float(f[cidx])) >= CPU_PASSIVE_TRIP_MC:
                    hot += 1
            except (ValueError, IndexError):
                pass
        th.update({"samples": n, "throttle_samples": thr, "throttle_devices": sorted(thr_devs),
                   "cpu_ge_passive_trip_samples": hot, "max_temp_mC": maxes,
                   "verdict": ("INVALID_THERMAL" if (thr > 0 or hot > 0) else "VALID") if n else "NO_SAMPLES"})
    (diag / "thermal.json").write_text(json.dumps(th, indent=1, sort_keys=True) + "\n")

    t = stats.get("total_ms", {})
    print("RUNLOG {name} status={st} delta_verified={dv} num_pts={b} elim={m} n={n} p50={p50:.2f} p99={p99:.2f} "
          "miss={miss} compl={c:.1f}% rss_kb={rss} wall={w} thermal={tv} maxcpu_mC={mc}".format(
              name=a.name, st=summary["status"], dv=delta["delta_verified"], b=effective_budget, m=resolved_mode,
              n=stats.get("n_callbacks", 0), p50=t.get("p50", float("nan")), p99=t.get("p99", float("nan")),
              miss=stats.get("deadline_misses", -1), c=stats.get("deadline_compliance_pct", float("nan")),
              rss=rss_kb, w=elapsed, tv=th["verdict"], mc=th["max_temp_mC"].get("cpu-thermal")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
