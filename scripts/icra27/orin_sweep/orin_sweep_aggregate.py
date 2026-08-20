#!/usr/bin/python3.8
"""ORIN-SWEEP-1 desktop aggregator (ledger F3). Reads a mirrored sweep root and emits, per dataset family and mode:
p99-vs-budget, compliance-vs-budget, ATE-vs-budget (coverage-welded), energy/update-vs-budget (C5 convention),
the B* table, the universal cell rows, and the mechanical CL-1/CL-2/CL-3 evaluation against the prereg wording.

Accuracy uses the frozen CDSC-1R4 core unchanged (cross_dataset_pair_evaluator -> kaist_pair_evaluator, evo 1.31.1).
All latency statistics are recomputed here from timing_openvins.csv (the board-side summary is a cross-check only).
"""
import argparse, csv, hashlib, json, re, statistics, subprocess, sys, tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(HERE.parent))
import cross_dataset_pair_evaluator as pair_evaluator  # noqa: E402
CORE = pair_evaluator.CORE
CONVERTER = REPO / "scripts" / "cp0" / "openvins_to_tum.py"

F = 200
GRID = [50, 75, 100, 150, 200, 300]
EXT = [35, 25]
NATIVE_PERIOD_MS = {"kaist": 1000.0 / 30.0, "euroc": 50.0}
GT = {("kaist", "rotation_fast"): REPO / "ov_data/kaist_vio/rotation_fast.txt",
      ("kaist", "circle"): REPO / "ov_data/kaist_vio/circle.txt",
      ("kaist", "square_head"): REPO / "ov_data/kaist_vio/square_head.txt",
      ("euroc", "MH_05"): REPO / "ov_data/euroc_mav/MH_05_difficult.txt"}
C5_IDLE_LOG = Path("/home/moksh/schurvio-icra27-artifacts/orin-bringup-20260820T131729Z/power/idle_baseline_tegrastats.txt")
ENERGY_CONVENTION = ("C5 frozen convention: idle mean = mean VDD_IN over the first 60 tegrastats samples of the C5 60 s idle capture "
                     "(3038 mW); energy above idle = sum over VDD_IN samples with timestamp in [capture start, capture start + "
                     "/usr/bin/time wall-clock + 12 s] of (VDD_IN - idle mean) x 1 s (declared 1 Hz); energy per update = energy "
                     "above idle / rows of timing_openvins.csv.")
MAX_STATE_GAP_S = 0.2
NAME_RE = re.compile(r"^(kaist|euroc)-(.+)-(S1|N0)-b(\d+)-r(\d+)(t?)$")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def tegra(path):
    out = []
    for line in Path(path).read_text(errors="replace").splitlines():
        m = re.match(r"(\d\d-\d\d-\d{4} \d\d:\d\d:\d\d).*VDD_IN (\d+)mW", line)
        if m:
            out.append((datetime.strptime(m.group(1), "%m-%d-%Y %H:%M:%S").timestamp(), int(m.group(2))))
    return out


def temps_from_tegra(path):
    mx = defaultdict(lambda: -1.0)
    for line in Path(path).read_text(errors="replace").splitlines():
        for z, v in re.findall(r"(cpu|soc0|soc1|soc2|gpu|tj)@([\d.]+)C", line):
            mx[z] = max(mx[z], float(v))
    return dict(mx)


def idle_mean(path):
    s = tegra(path)[:60]
    return sum(p for _, p in s) / len(s), len(s)


def med_iqr(xs):
    xs = [x for x in xs if x is not None and np.isfinite(x)]
    if not xs:
        return (None, None, None, 0)
    q1, q3 = np.percentile(xs, [25, 75])
    return (float(np.median(xs)), float(q1), float(q3), len(xs))


def fmt(v, d=2):
    return "—" if v is None else ("%.*f" % (d, v))


def mi(t, d=2):
    return "—" if t[0] is None else "%s [%s, %s] (n=%d)" % (fmt(t[0], d), fmt(t[1], d), fmt(t[2], d), t[3])


class Accuracy:
    def __init__(self, work):
        self.work = work; self.cache = {}; self.gt_cache = {}

    def gt_rows(self, key):
        if key not in self.gt_cache:
            rows, proj = pair_evaluator._read_tum_with_bounded_quaternion_projection(GT[key], "ground truth")
            self.gt_cache[key] = rows
        return self.gt_cache[key]

    def evaluate(self, key, state_path, state_sha, system):
        if state_sha in self.cache:
            return dict(self.cache[state_sha], cached=True)
        d = self.work / state_sha[:16]; d.mkdir(parents=True, exist_ok=True)
        tum = d / "estimate_raw.tum"
        res = {"status": None, "ate_m": None, "rpe_t_1m_m": None, "rpe_r_1m_deg": None, "associated": 0, "est_rows": 0, "gt_rows": 0}
        try:
            subprocess.run([sys.executable, str(CONVERTER), str(state_path), str(tum)], check=True, capture_output=True)
            gt = self.gt_rows(key)
            est, _ = pair_evaluator._read_tum_with_bounded_quaternion_projection(tum, "estimate")
            assoc = pair_evaluator._associate_or_empty(gt, est)
            res.update(est_rows=len(est), gt_rows=len(gt), associated=len(assoc))
            if len(assoc) < pair_evaluator.MINIMUM_COMMON_POSES:
                res["status"] = "INSUFFICIENT_POSES"; self.cache[state_sha] = res; return res
            gsel = [gt[a.gt_index] for a in assoc]; esel = [est[a.estimate_index] for a in assoc]
            rp, ep = d / "ref.tum", d / "est.tum"
            CORE.write_tum(rp, gsel, gsel); CORE.write_tum(ep, esel, gsel)
            ref = pair_evaluator._project_loaded_evo_trajectory(CORE.file_interface.read_tum_trajectory_file(str(rp)), "reference")
            e = pair_evaluator._project_loaded_evo_trajectory(CORE.file_interface.read_tum_trajectory_file(str(ep)), "estimate")
            pairs = CORE.metrics.id_pairs_from_delta(ref.poses_se3, CORE.RPE_DELTA_METERS, CORE.metrics.Unit.meters,
                                                    CORE.RPE_RELATIVE_DELTA_TOLERANCE, all_pairs=CORE.RPE_ALL_PAIRS)
            pairs = [(int(a), int(b)) for a, b in pairs]
            if len(pairs) < pair_evaluator.MINIMUM_RPE_PAIRS:
                res["status"] = "INSUFFICIENT_RPE_PAIRS"; self.cache[state_sha] = res; return res
            m = CORE.evaluate_method(system, ref, e, pairs)
            res.update(status="COMPUTED", ate_m=m["primary"]["ate_translation_rmse_m"],
                       rpe_t_1m_m=m["primary"]["rpe_translation_rmse_1m_m"], rpe_r_1m_deg=m["primary"]["rpe_rotation_rmse_1m_deg"])
        except Exception as exc:  # failures are data
            res["status"] = "EVAL_ERROR: %s" % str(exc)[:200]
        self.cache[state_sha] = res
        return res


def coverage(state_ts, span_start, span_end):
    if not state_ts:
        return {"coverage_pct": 0.0, "state_gaps": 0, "max_gap_s": None}
    gaps = 0.0; n = 0; mg = 0.0
    for i in range(1, len(state_ts)):
        d = state_ts[i] - state_ts[i - 1]
        mg = max(mg, d)
        if d > MAX_STATE_GAP_S:
            gaps += d; n += 1
    covered = max(0.0, state_ts[-1] - state_ts[0] - gaps)
    span = max(1e-9, span_end - span_start)
    return {"coverage_pct": 100.0 * covered / span, "state_gaps": n, "max_gap_s": mg}


def read_cell(cell, idle, acc):
    name = cell.name
    m = NAME_RE.match(name)
    if not m:
        return None
    fam, seq, mode, budget, rep, tflag = m.group(1), m.group(2), m.group(3), int(m.group(4)), int(m.group(5)), m.group(6)
    diag = cell / "diagnostics"
    if not (diag / "cell_summary.json").is_file():
        return {"cell": name, "family": fam, "sequence": seq, "mode": mode, "budget": budget, "repeat": rep, "status": "IN_PROGRESS", "thermal": None, "delta_verified": False, "ate_status": None, "max_cpu_C": None, "max_tj_C": None}
    J = lambda f: json.load(open(diag / f)) if (diag / f).is_file() else {}
    delta, summ, therm = J("cell_delta.json"), J("cell_summary.json"), J("thermal.json")
    host = dict(l.split("=", 1) for l in (diag / "host_invocation.txt").read_text().splitlines() if "=" in l) if (diag / "host_invocation.txt").is_file() else {}
    row = {"cell": name, "family": fam, "sequence": seq, "mode": mode, "budget": budget, "repeat": rep, "thermal_repeat": bool(tflag),
           "delta_verified": delta.get("delta_verified"), "effective_num_pts": delta.get("effective_num_pts"),
           "elimination": delta.get("resolved_elimination_ros"), "config_sha256": delta.get("config_sha256"),
           "launch_sha256": delta.get("launch_sha256"), "binary_sha256": delta.get("binary_sha256"),
           "container_exit": int(host.get("container_exit", -1)), "roslaunch_exit": summ.get("roslaunch_exit"),
           "status": summ.get("status"), "thermal": therm.get("verdict"), "throttle_samples": therm.get("throttle_samples"),
           "max_cpu_C": (therm.get("max_temp_mC") or {}).get("cpu-thermal", -1) / 1000.0 if therm else None,
           "max_tj_C": (therm.get("max_temp_mC") or {}).get("tj-thermal", -1) / 1000.0 if therm else None,
           "thermal_gate_wait_s": float(host.get("thermal_gate_wait_s", 0) or 0), "bag_warm_s": (float(host["bag_warm_s"]) if host.get("bag_warm_s") else None), "pre_D9": "bag_warm_s" not in host, "pre_cpu_C": float(host.get("pre_cpu_mC", 0) or 0) / 1000.0,
           "peak_rss_kb": summ.get("peak_rss_kb"), "wall_s": summ.get("wall_elapsed_s"), "state_sha256": summ.get("state_estimate_sha256")}
    # latency, recomputed
    period = NATIVE_PERIOD_MS[fam]
    tp = diag / "timing_openvins.csv"
    rows = []
    if tp.is_file():
        for line in tp.read_text().splitlines():
            if line and not line.startswith("#"):
                try: rows.append([float(x) for x in line.split(",")])
                except ValueError: pass
    if rows:
        arr = np.asarray(rows); total = arr[:, -1] * 1000.0
        miss = int(np.sum(total > period))
        row.update(n_callbacks=len(total), p50_ms=float(np.percentile(total, 50)), p95_ms=float(np.percentile(total, 95)),
                   p99_ms=float(np.percentile(total, 99)), max_ms=float(total.max()), mean_ms=float(total.mean()),
                   track_p50_ms=float(np.percentile(arr[:, 1] * 1000, 50)),
                   deadline_misses=miss, compliance_pct=100.0 * (len(total) - miss) / len(total),
                   callback_span_start=float(arr[0, 0]), callback_span_end=float(arr[-1, 0]))
        bs = summ.get("timing", {}).get("total_ms", {}).get("p99")
        row["board_p99_agrees"] = (bs is not None and abs(bs - row["p99_ms"]) < 1e-6)
    else:
        row.update(n_callbacks=0, compliance_pct=None, p99_ms=None, p50_ms=None, p95_ms=None, max_ms=None, deadline_misses=None)
    # energy (C5 convention)
    tg = diag / ("%s_tegrastats.txt" % name)
    row.update(energy_J=None, energy_per_update_mJ=None, power_mean_mW=None, power_peak_mW=None, power_samples=0)
    if tg.is_file() and row.get("wall_s") is not None:
        s = tegra(tg)
        if s:
            t0 = s[0][0]; w = [p for t, p in s if t <= t0 + row["wall_s"] + 12]
            if w:
                e = sum(p - idle for p in w) / 1000.0
                row.update(energy_J=e, power_mean_mW=sum(w) / len(w), power_peak_mW=max(w), power_samples=len(w),
                           energy_per_update_mJ=(1000.0 * e / row["n_callbacks"]) if row["n_callbacks"] else None)
        tt = temps_from_tegra(tg)
        row["tegra_max_cpu_C"] = tt.get("cpu"); row["tegra_max_tj_C"] = tt.get("tj")
    # accuracy + coverage
    est = cell / "trajectory" / "state_estimate.txt"
    row.update(ate_m=None, rpe_t_1m_m=None, rpe_r_1m_deg=None, ate_status=None, coverage_pct=None, state_rows=0, state_gaps=None, max_state_gap_s=None)
    if est.is_file() and row["status"] == "COMPLETED":
        ts = [float(l.split()[0]) for l in est.read_text().splitlines() if l and not l.startswith("#")]
        row["state_rows"] = len(ts)
        if rows and ts:
            row.update(coverage(ts, row["callback_span_start"], row["callback_span_end"]))
        a = acc.evaluate((fam, seq), est, row["state_sha256"], mode)
        row.update(ate_m=a["ate_m"], rpe_t_1m_m=a["rpe_t_1m_m"], rpe_r_1m_deg=a["rpe_r_1m_deg"], ate_status=a["status"],
                   ate_associated=a["associated"])
    elif est.is_file():
        row["ate_status"] = "NOT_ELIGIBLE(%s)" % row["status"]
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, type=Path); ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--idle-log", type=Path, default=C5_IDLE_LOG)
    a = ap.parse_args(); a.out.mkdir(parents=True, exist_ok=True)
    idle, idle_n = idle_mean(a.idle_log)
    fresh = a.root / "power" / "idle_baseline_sweep_tegrastats.txt"
    fresh_idle = idle_mean(fresh) if fresh.is_file() else (None, 0)
    acc = Accuracy(a.out / "accuracy_work")
    rows = [r for r in (read_cell(c, idle, acc) for c in sorted((a.root / "cells").iterdir()) if c.is_dir()) if r]
    # universal rows
    cols = ["cell", "family", "sequence", "mode", "budget", "repeat", "thermal_repeat", "status", "thermal", "delta_verified", "effective_num_pts", "elimination",
            "n_callbacks", "p50_ms", "p95_ms", "p99_ms", "max_ms", "mean_ms", "track_p50_ms", "deadline_misses", "compliance_pct",
            "energy_J", "energy_per_update_mJ", "power_mean_mW", "power_peak_mW", "power_samples", "peak_rss_kb", "wall_s",
            "ate_m", "rpe_t_1m_m", "rpe_r_1m_deg", "ate_status", "coverage_pct", "state_rows", "state_gaps", "max_state_gap_s",
            "max_cpu_C", "max_tj_C", "throttle_samples", "thermal_gate_wait_s", "pre_cpu_C", "bag_warm_s", "pre_D9", "container_exit", "roslaunch_exit",
            "state_sha256", "config_sha256", "launch_sha256", "binary_sha256", "board_p99_agrees"]
    with open(a.out / "cells_universal.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        for r in rows: w.writerow(r)
    valid = [r for r in rows if r["status"] == "COMPLETED" and r["thermal"] == "VALID" and r["delta_verified"]]
    # per (family, sequence, mode, budget)
    G = defaultdict(list)
    for r in valid: G[(r["family"], r["sequence"], r["mode"], r["budget"])].append(r)
    budgets = sorted(set(GRID + EXT) | {r["budget"] for r in rows})
    seqs = {fam: sorted({r["sequence"] for r in rows if r["family"] == fam}) for fam in ("kaist", "euroc")}
    curves = {}
    for fam in ("kaist", "euroc"):
        for mode in ("S1", "N0"):
            for b in budgets:
                fam_rows = [r for s in seqs[fam] for r in G.get((fam, s, mode, b), [])]
                per_seq = {}
                for s in seqs[fam]:
                    rr = G.get((fam, s, mode, b), [])
                    shas = {r["state_sha256"] for r in rr}
                    per_seq[s] = {"n_runs": len(rr), "p99": med_iqr([r["p99_ms"] for r in rr]), "p50": med_iqr([r["p50_ms"] for r in rr]),
                                  "compliance": med_iqr([r["compliance_pct"] for r in rr]), "energy": med_iqr([r["energy_per_update_mJ"] for r in rr]),
                                  "rss": med_iqr([r["peak_rss_kb"] for r in rr]),
                                  "ate": (rr[0]["ate_m"] if rr else None), "ate_status": (rr[0]["ate_status"] if rr else None),
                                  "coverage": (rr[0]["coverage_pct"] if rr else None), "rpe_t": (rr[0]["rpe_t_1m_m"] if rr else None),
                                  "distinct_trajectories": len(shas), "identical_repeats": "%d/%d" % (len(rr) - len(shas) + (1 if rr else 0), len(rr))}
                curves[(fam, mode, b)] = {"n_runs": len(fam_rows), "p99": med_iqr([r["p99_ms"] for r in fam_rows]),
                                          "compliance": med_iqr([r["compliance_pct"] for r in fam_rows]),
                                          "energy": med_iqr([r["energy_per_update_mJ"] for r in fam_rows]),
                                          "ate_by_seq": {s: per_seq[s]["ate"] for s in seqs[fam]}, "per_seq": per_seq}
    # B*
    bstar = {}
    for fam in ("kaist", "euroc"):
        for mode in ("S1", "N0"):
            ok = [b for b in budgets if curves[(fam, mode, b)]["n_runs"] and curves[(fam, mode, b)]["compliance"][0] is not None and curves[(fam, mode, b)]["compliance"][0] >= 95.0]
            per_seq_b = {}
            for s in seqs[fam]:
                oks = [b for b in budgets if curves[(fam, mode, b)]["per_seq"][s]["compliance"][0] is not None and curves[(fam, mode, b)]["per_seq"][s]["compliance"][0] >= 95.0]
                per_seq_b[s] = max(oks) if oks else None
            bstar[(fam, mode)] = {"B": max(ok) if ok else None, "per_seq": per_seq_b,
                                  "tested": [b for b in budgets if curves[(fam, mode, b)]["n_runs"]]}
    # clauses
    def step_index(b):
        g = sorted(set(GRID + EXT)); return g.index(b)
    cl1 = {"true": False, "detail": {}}
    for fam in ("kaist", "euroc"):
        bs, bn = bstar[(fam, "S1")]["B"], bstar[(fam, "N0")]["B"]
        if bs is not None and bn is not None: gap = step_index(bs) - step_index(bn)
        elif bs is not None and bn is None: gap = "S1 only"
        else: gap = None
        fires = (isinstance(gap, int) and gap >= 1) or gap == "S1 only"
        cl1["detail"][fam] = {"B_S1": bs, "B_N0": bn, "grid_step_gap": gap, "fires": fires,
                              "ate_at_B_S1": curves[(fam, "S1", bs)]["ate_by_seq"] if bs else None,
                              "ate_at_B_N0": curves[(fam, "N0", bn)]["ate_by_seq"] if bn else None}
        cl1["true"] |= fires
    def ratio_clause(metric, thr):
        out = {"true": False, "sequences": {}}
        n_fire = 0
        for fam in ("kaist", "euroc"):
            for s in seqs[fam]:
                a1 = curves[(fam, "S1", F)]["per_seq"][s][metric] if (fam, "S1", F) in curves else (None,) * 4
                a0 = curves[(fam, "N0", F)]["per_seq"][s][metric] if (fam, "N0", F) in curves else (None,) * 4
                r = (a1[0] / a0[0]) if (a1[0] is not None and a0[0]) else None
                fires = r is not None and r <= thr
                n_fire += int(fires)
                out["sequences"][s] = {"S1_median": a1[0], "N0_median": a0[0], "ratio": r, "fires": fires, "n_S1": a1[3], "n_N0": a0[3]}
        out["true"] = n_fire >= 2; out["sequences_firing"] = n_fire; out["threshold"] = thr
        return out
    cl2 = ratio_clause("p99", 0.85); cl3 = ratio_clause("energy", 0.80)
    supported = cl1["true"] or cl2["true"] or cl3["true"]
    # extension rule
    kaist_any = any(curves[("kaist", m, b)]["compliance"][0] is not None and curves[("kaist", m, b)]["compliance"][0] >= 95 for m in ("S1", "N0") for b in GRID)
    result = {"schema": "schurvio.icra27.orin_sweep.aggregate.v1", "root": str(a.root), "idle_mean_mW_C5": idle, "idle_samples": idle_n,
              "fresh_idle_mean_mW": fresh_idle[0], "energy_convention": ENERGY_CONVENTION,
              "gt": {"%s/%s" % k: {"path": str(v), "sha256": sha(v)} for k, v in GT.items()},
              "counts": {"cells_total": len(rows), "completed": sum(r["status"] == "COMPLETED" for r in rows),
                         "failed": sum(r["status"] != "COMPLETED" for r in rows), "invalid_thermal": sum(r["thermal"] == "INVALID_THERMAL" for r in rows),
                         "delta_unverified": sum(not r["delta_verified"] for r in rows), "valid_for_stats": len(valid),
                         "ate_computed": sum(r["ate_status"] == "COMPUTED" for r in rows), "in_progress": sum(r["status"] == "IN_PROGRESS" for r in rows)},
              "bstar": {"%s/%s" % k: v for k, v in bstar.items()},
              "curves": {"%s/%s/%d" % k: v for k, v in curves.items()},
              "CL1": cl1, "CL2": cl2, "CL3": cl3, "F3": "SUPPORTED" if supported else "REFUTED",
              "F3_clauses_true": [c for c, v in (("CL-1", cl1["true"]), ("CL-2", cl2["true"]), ("CL-3", cl3["true"])) if v],
              "kaist_extension_rule_fires": not kaist_any, "max_cpu_C_overall": max([r["max_cpu_C"] or -1 for r in rows] + [-1]),
              "max_tj_C_overall": max([r["max_tj_C"] or -1 for r in rows] + [-1]),
              "throttle_cells": [r["cell"] for r in rows if r["thermal"] == "INVALID_THERMAL"]}
    json.dump(result, open(a.out / "aggregate.json", "w"), indent=1, default=str)
    # markdown tables
    L = []
    def curve_table(title, key, d=2, unit=""):
        L.append("### %s%s\n" % (title, unit))
        for fam in ("kaist", "euroc"):
            L.append("**%s**\n" % fam.upper()); L.append("| budget | S1 (family median [IQR], n) | N0 (family median [IQR], n) | " + " | ".join("S1/%s | N0/%s" % (s, s) for s in seqs[fam]) + " |")
            L.append("|---|---|---|" + "---|" * (2 * len(seqs[fam])))
            for b in budgets:
                c1, c0 = curves[(fam, "S1", b)], curves[(fam, "N0", b)]
                if not c1["n_runs"] and not c0["n_runs"]: continue
                cells = []
                for s in seqs[fam]:
                    cells += [mi(c1["per_seq"][s][key], d), mi(c0["per_seq"][s][key], d)]
                L.append("| %d%s | %s | %s | %s |" % (b, " (F)" if b == F else "", mi(c1[key], d), mi(c0[key], d), " | ".join(cells)))
            L.append("")
    curve_table("p99 updater latency vs budget", "p99", 2, " (ms)")
    curve_table("Deadline compliance vs budget", "compliance", 2, " (% of callbacks with total ≤ native period)")
    curve_table("Energy per update vs budget", "energy", 1, " (mJ above idle; C5 convention; idle 3038 mW)")
    L.append("### ATE vs budget (coverage-welded; one deterministic trajectory per cell; frozen CDSC-1R4 core)\n")
    for fam in ("kaist", "euroc"):
        L.append("**%s**\n" % fam.upper()); L.append("| budget | " + " | ".join("S1/%s ATE m (cov %%; status) | N0/%s ATE m (cov %%; status)" % (s, s) for s in seqs[fam]) + " |")
        L.append("|---|" + "---|" * (2 * len(seqs[fam])))
        for b in budgets:
            cells = []
            for s in seqs[fam]:
                for m in ("S1", "N0"):
                    ps = curves[(fam, m, b)]["per_seq"][s]
                    cells.append("—" if ps["n_runs"] == 0 else "%s (%s; %s; repeats identical %s)" % (fmt(ps["ate"], 3), fmt(ps["coverage"], 1), ps["ate_status"], ps["identical_repeats"]))
            if all(c == "—" for c in cells): continue
            L.append("| %d%s | %s |" % (b, " (F)" if b == F else "", " | ".join(cells)))
        L.append("")
    L.append("### B* table\n"); L.append("| family | B*(S1) | B*(N0) | per-sequence B* S1 | per-sequence B* N0 | ATE at B*(S1) | ATE at B*(N0) |"); L.append("|---|---|---|---|---|---|---|")
    for fam in ("kaist", "euroc"):
        s1, n0 = bstar[(fam, "S1")], bstar[(fam, "N0")]
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (fam, s1["B"] or "NONE", n0["B"] or "NONE", s1["per_seq"], n0["per_seq"],
                 cl1["detail"][fam]["ate_at_B_S1"], cl1["detail"][fam]["ate_at_B_N0"]))
    (a.out / "tables.md").write_text("\n".join(L) + "\n")
    print(json.dumps({k: result[k] for k in ("counts", "bstar", "F3", "F3_clauses_true", "kaist_extension_rule_fires")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
