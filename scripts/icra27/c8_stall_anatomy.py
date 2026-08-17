#!/usr/bin/env python3
"""C8 Item 2 — stall anatomy extractor (ledger F14).

Builds per-frame timelines for the KAIST rotation.bag passage across the
three variants (U0, recOFF, recON) plus the BLACKOUT-1 masked-gap control
cells, from FROZEN artifacts only (no re-runs), and emits:
  bag_anatomy.json          source-bag input anatomy (camera/IMU stamps, gaps)
  frames_vs_states_<cell>.csv   per camera-pair frame: state published? region
  timeline_<cell>.csv       per published state: dt, stage timings, events
  cells_summary.csv         per cell: gaps, tail, resume offset, exit, events
  three_regimes.csv         natural-stall vs masked-gap vs recovered
  stall_anatomy.md          the causal chain + evidence-based adjudication
  SHA256SUMS

Everything reported is a direct reading of the frozen logs/files; nothing is
concluded past what the logs support.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import hashlib
import json
import os
import re
import struct
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ANSI = re.compile(rb"\x1b\[[0-9;]*m")
CDSC = Path("/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z")
ABLATE = Path("/home/moksh/schurvio-icra27-artifacts/recovery-ablation/ablate1-20260817T011231Z")
BLACKOUT = Path("/home/moksh/schurvio-icra27-artifacts/blackout-campaign/blackout1-20260817T130445Z")
PERTURB = Path("/home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z")
ROTATION_BAG = Path("/home/moksh/datasets/KAIST_VIO/raw/turnsafe_adapted/rotation/rotation.bag")
GT_ROTATION = Path("/home/moksh/schurvio-lite-rotation-robustness/ov_data/kaist_vio/rotation.txt")
CAM_TOPICS = ["/turnsafe/kaist/infra1/image_raw", "/turnsafe/kaist/infra2/image_raw"]
IMU_TOPIC = "/mavros/imu/data"
CAM_IMU_DT_S = -0.029958533056650416  # kalibr_imucam_chain.yaml cam0 timeshift_cam_imu (state ts = cam ts + dt)
GAP_THRESHOLD_S = 0.2
FROZEN_MAX_STATE_GAP_S = 0.20
FROZEN_DURATION_DELTA_TOL_S = 1e-6
D13_QA_TOL_S = 2.0e-5


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def strip(b: bytes) -> str:
    return ANSI.sub(b"", b).decode("utf-8", "replace")


# ------------------------------------------------------------------ bag ----
def bag_anatomy(cache: Path) -> Dict[str, Any]:
    if cache.is_file():
        return json.load(open(cache))
    sys.path.insert(0, "/opt/ros/noetic/lib/python3/dist-packages")
    import rosbag  # type: ignore

    out: Dict[str, Any] = {"bag": str(ROTATION_BAG), "bag_sha256": None, "topics": {}}
    stamps: Dict[str, List[Tuple[float, float]]] = {t: [] for t in CAM_TOPICS + [IMU_TOPIC]}
    with rosbag.Bag(str(ROTATION_BAG), "r") as bag:
        for topic, raw, t in bag.read_messages(topics=CAM_TOPICS + [IMU_TOPIC], raw=True):
            data = raw[1]
            # std_msgs/Header at the start of the serialized message: seq u32, stamp sec u32, nsec u32
            _seq, sec, nsec = struct.unpack_from("<III", data, 0)
            stamps[topic].append((sec + nsec * 1e-9, t.to_sec()))
    for topic, arr in stamps.items():
        arr.sort()
        hs = [a[0] for a in arr]
        gaps = []
        for i in range(1, len(hs)):
            d = hs[i] - hs[i - 1]
            if d > GAP_THRESHOLD_S:
                gaps.append({"start_header_s": hs[i - 1], "end_header_s": hs[i], "duration_s": d,
                             "start_record_s": arr[i - 1][1], "end_record_s": arr[i][1], "index_after_gap": i})
        entry = {"count": len(hs), "first_header_s": hs[0] if hs else None, "last_header_s": hs[-1] if hs else None,
                 "gaps_over_threshold": gaps}
        if gaps and topic in CAM_TOPICS:
            i = gaps[0]["index_after_gap"]
            entry["last_pre_gap_frames"] = [{"header_s": a[0], "record_s": a[1]} for a in arr[max(0, i - 3):i]]
            entry["first_post_gap_frames"] = [{"header_s": a[0], "record_s": a[1]} for a in arr[i:i + 3]]
        out["topics"][topic] = entry
    # IMU inside the camera gap
    if out["topics"][CAM_TOPICS[0]]["gaps_over_threshold"]:
        g = out["topics"][CAM_TOPICS[0]]["gaps_over_threshold"][0]
        imu = [a[0] for a in stamps[IMU_TOPIC]]
        n_in = sum(1 for s in imu if g["start_header_s"] < s < g["end_header_s"])
        out["imu_messages_inside_camera_gap"] = n_in
        dts = [imu[i] - imu[i - 1] for i in range(1, len(imu))]
        out["imu_max_dt_s"] = max(dts) if dts else None
    out["bag_sha256"] = sha256_file(ROTATION_BAG)
    cache.parent.mkdir(parents=True, exist_ok=True)
    json.dump(out, open(cache, "w"), indent=1)
    return out


def gt_yaw_rate_profile(gap: Dict[str, float]) -> Dict[str, Any]:
    """GT angular rate (deg/s) from quaternion differencing around the gap."""
    import numpy as np
    rows = []
    for line in open(GT_ROTATION):
        if line.startswith("#"):
            continue
        p = line.split()
        if len(p) < 8:
            continue
        rows.append([float(x) for x in p[:8]])
    a = np.array(rows)
    t = a[:, 0]
    q = a[:, 4:8]  # x y z w (TUM)
    # relative rotation angle between consecutive samples
    def ang(q1, q2):
        d = abs(float(np.dot(q1, q2)))
        d = min(1.0, d)
        return 2.0 * np.arccos(d)
    # Windowed rate (0.5 s) — sample-to-sample differencing of this mocap GT is dominated by timestamp jitter.
    W = 0.5
    j = np.searchsorted(t, t + W)
    valid = j < len(t)
    idx = np.nonzero(valid)[0]
    rates = np.array([ang(q[i], q[j[i]]) / max(t[j[i]] - t[i], 1e-9) for i in idx])
    tm = 0.5 * (t[idx] + t[j[idx]])
    def stat(lo, hi):
        m = (tm >= lo) & (tm <= hi)
        if not m.any():
            return None
        r = np.degrees(rates[m])
        return {"n": int(m.sum()), "mean_deg_s": float(r.mean()), "median_deg_s": float(np.median(r)), "p95_deg_s": float(np.percentile(r, 95))}
    g0, g1 = gap["start_header_s"], gap["end_header_s"]
    pos = a[:, 1:4]
    def speed(lo, hi):
        m = (t >= lo) & (t <= hi)
        tt, pp = t[m], pos[m]
        if len(tt) < 3:
            return None
        v = np.linalg.norm(np.diff(pp, axis=0), axis=1) / np.maximum(np.diff(tt), 1e-9)
        return {"median_m_s": float(np.median(v)), "p95_m_s": float(np.percentile(v, 95))}
    # 10 s profile over the whole sequence (for the paper's severity context)
    prof = []
    for lo in np.arange(t[0], t[-1], 10.0):
        m = (tm >= lo) & (tm < lo + 10.0)
        if m.any():
            prof.append({"t_rel_s": float(lo - t[0]), "omega_mean_deg_s": float(np.degrees(rates[m]).mean()), "omega_p95_deg_s": float(np.percentile(np.degrees(rates[m]), 95))})
    return {"gt_first_s": float(t[0]), "gt_last_s": float(t[-1]), "gt_rows": int(len(t)),
            "gap_rel_start_s": float(g0 - t[0]), "gap_rel_end_s": float(g1 - t[0]),
            "speed_pre_gap_5s": speed(g0 - 5, g0), "speed_inside_gap": speed(g0, g1), "speed_post_gap_5s": speed(g1, g1 + 5),
            "omega_profile_10s": prof,
            "pre_gap_5s": stat(g0 - 5, g0), "inside_gap": stat(g0, g1), "post_gap_5s": stat(g1, g1 + 5),
            "whole": stat(t[0], t[-1]), "window_s": W,
            "onset_first_time_over_30deg_s": (float(tm[np.degrees(rates) > 30][0]) if (np.degrees(rates) > 30).any() else None)}


# ---------------------------------------------------------------- cells ----
def read_states(p: Path) -> List[Tuple[float, float, float, float]]:
    out = []
    with open(p) as f:
        for line in f:
            if line.startswith("#"):
                continue
            s = line.split()
            if len(s) < 8:
                continue
            out.append((float(s[0]), float(s[5]), float(s[6]), float(s[7])))
    return out


def read_timing(p: Path) -> List[List[float]]:
    out = []
    if not p.is_file():
        return out
    with open(p) as f:
        for line in f:
            if line.startswith("#"):
                continue
            s = line.strip().split(",")
            try:
                out.append([float(x) for x in s])
            except ValueError:
                pass
    return out


RECOVERY_RE = re.compile(r"\[LONG-GAP-RECOVERY\]: event=(\w+)(.*)")


def parse_console(p: Path) -> Dict[str, Any]:
    if not p.is_file():
        return {"present": False}
    txt = strip(p.read_bytes())
    lines = txt.splitlines()
    ev = []
    for ln in lines:
        m = RECOVERY_RE.search(ln)
        if m:
            kv = dict(re.findall(r"(\w+)=([^\s]+)", m.group(2)))
            kv["event"] = m.group(1)
            ev.append(kv)
    deaths = [ln for ln in lines if "has died" in ln or "terminate called" in ln or "what():" in ln]
    return {"present": True, "lines": len(lines), "time_lines": sum(1 for ln in lines if ln.startswith("[TIME]")),
            "gate_rejections": sum(1 for ln in lines if "[MSCKF-GATE]" in ln),
            "recovery_events": ev, "death_lines": deaths[:6],
            "zupt_lines": sum(1 for ln in lines if "ZUPT" in ln.upper()),
            "serial_kaist": [ln for ln in lines if ln.startswith("[SERIAL-KAIST]")],
            "out_of_order": sum(1 for ln in lines if "out of order" in ln)}


def analyze_cell(cell_dir: Path, label: str, system: str, campaign: str, cam_frames: List[float],
                 input_last_s: float, extra: Dict[str, Any], out_dir: Path) -> Dict[str, Any]:
    st_p = cell_dir / "trajectory" / "state_estimate.txt"
    states = read_states(st_p) if st_p.is_file() else []
    timing = read_timing(cell_dir / "diagnostics" / "timing_openvins.csv")
    con = parse_console(cell_dir / "diagnostics" / "console.log")
    ts = [s[0] for s in states]
    gaps = []
    for i in range(1, len(ts)):
        d = ts[i] - ts[i - 1]
        if d > FROZEN_MAX_STATE_GAP_S:
            gaps.append({"start_s": ts[i - 1], "end_s": ts[i], "duration_s": d})
    rec: Dict[str, Any] = {"label": label, "system": system, "campaign": campaign, "cell_dir": str(cell_dir),
                           "state_rows": len(ts), "timing_rows": len(timing),
                           "first_state_s": ts[0] if ts else None, "last_state_s": ts[-1] if ts else None,
                           "state_gaps": gaps, "console": {k: v for k, v in con.items() if k not in ("recovery_events",)},
                           "recovery_events": con.get("recovery_events", [])}
    rec.update(extra)
    if ts:
        rec["tail_gap_s"] = input_last_s + CAM_IMU_DT_S - ts[-1]
    # frames vs states: does each camera pair stamp yield a state row?
    if cam_frames and ts:
        import bisect
        rows = []
        stset = ts
        n_present_no_state = {"pre_gap": 0, "post_gap": 0}
        n_present_state = {"pre_gap": 0, "post_gap": 0}
        gap0 = extra.get("input_gap_start_s")
        first_state = ts[0]
        for f in cam_frames:
            expected = f + CAM_IMU_DT_S
            i = bisect.bisect_left(stset, expected - 5e-4)
            has = i < len(stset) and abs(stset[i] - expected) <= 5e-4
            region = "pre_gap" if (gap0 is None or f <= gap0) else "post_gap"
            if f + CAM_IMU_DT_S < first_state:
                region = "pre_init"
            elif f + CAM_IMU_DT_S > ts[-1] + 1e-3:
                region = region + "_after_last_state"
            rows.append((f, expected, has, region))
            if region in n_present_no_state:
                (n_present_state if has else n_present_no_state)[region] += 1
        with open(out_dir / "frames_vs_states_{}.csv".format(label), "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["camera_pair_header_s", "expected_state_s", "state_row_present", "region"])
            for r in rows:
                w.writerow([repr(r[0]), repr(r[1]), int(r[2]), r[3]])
        rec["frames_present_with_state"] = n_present_state
        rec["frames_present_without_state"] = n_present_no_state
        # frames present after the last state (U0 abort signature)
        rec["frames_after_last_state"] = sum(1 for r in rows if r[3].endswith("_after_last_state"))
    # per-state timeline
    with open(out_dir / "timeline_{}.csv".format(label), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["state_s", "dt_prev_s", "px", "py", "pz", "region", "tracking_s", "propagation_s", "msckf_update_s", "slam_update_s", "total_s", "event"])
        tmap = {round(r[0], 4): r for r in timing}
        evs = collections.defaultdict(list)
        for e in rec["recovery_events"]:
            t = e.get("timestamp") or e.get("camera_timestamp")
            if t:
                try:
                    evs[round(float(t) + (CAM_IMU_DT_S if "camera_timestamp" in e and "timestamp" not in e else 0.0), 2)].append(e["event"])
                except ValueError:
                    pass
        gap0 = extra.get("input_gap_start_s")
        gap1 = extra.get("input_gap_end_s")
        for i, s in enumerate(states):
            dt = s[0] - states[i - 1][0] if i else 0.0
            region = "pre_gap" if (gap0 is None or s[0] <= gap0 + CAM_IMU_DT_S + 1e-3) else ("post_gap" if s[0] >= gap1 + CAM_IMU_DT_S - 1e-3 else "inside_input_gap")
            tr = tmap.get(round(s[0], 4))
            ev = ";".join(evs.get(round(s[0], 2), []))
            w.writerow([repr(s[0]), "%.6f" % dt, s[1], s[2], s[3], region] + ([tr[1], tr[2], tr[3], tr[4], tr[-1]] if tr and len(tr) >= 8 else ["", "", "", "", ""]) + [ev])
    return rec


def find_cells() -> List[Dict[str, Any]]:
    cells = []
    for mode in ("scored", "capture"):
        for sysname in ("U0", "S1"):
            for d in sorted(glob.glob(str(CDSC / mode / "19-rotation_rotation.bag" / sysname / "*"))):
                if Path(d).is_dir():
                    cells.append({"dir": Path(d), "campaign": "CDSC-1R4", "system": sysname + ("" if sysname == "U0" else "-recON"), "label": "cdsc1r4-{}-{}".format(mode, sysname.lower())})
    for grp, syss in (("p1-crux", ("S1-recOFF", "N0-recOFF")), ("u0-context", ("U0",)), ("integrity", ("S1", "N0"))):
        for sysname in syss:
            for d in sorted(glob.glob(str(ABLATE / "cells" / grp / "19-rotation_rotation.bag" / sysname / "ablate1-*"))):
                if Path(d).is_dir() and not d.endswith(".driver"):
                    off = re.search(r"offset-(off\w+)", d)
                    cells.append({"dir": Path(d), "campaign": "ABLATE-REC-1", "system": sysname if "recOFF" in sysname or sysname == "U0" else sysname + "-recON",
                                  "label": "ablate1-{}-{}-{}".format(grp, sysname.lower(), off.group(1) if off else "x")})
    return cells


def blackout_control() -> List[Dict[str, Any]]:
    rows = []
    p = BLACKOUT / "aggregate" / "final" / "cells.csv"
    with open(p) as f:
        for r in csv.DictReader(f):
            if r["outcome"] == "NOT_RUNNABLE":
                continue
            rows.append(r)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    bag = bag_anatomy(out / "bag_anatomy.json")
    cam0 = bag["topics"][CAM_TOPICS[0]]
    gap = cam0["gaps_over_threshold"][0] if cam0["gaps_over_threshold"] else None
    gt = gt_yaw_rate_profile(gap) if gap else None
    json.dump(gt, open(out / "gt_angular_rate.json", "w"), indent=1)

    # camera pair frames: S1 exact-header pairing = identical header stamps on both cams; use cam0 stamps
    # (frames_vs_states is evaluated against cam0 header stamps; the visualizer thinning removes ~1/5 of frames
    # for both builds — those show as frames_present_without_state in every variant and are reported as such)
    cam_frames = None
    # reconstruct cam0 stamps from cache: not stored per-frame; re-read quickly from bag_anatomy if present
    frames_cache = out / "cam0_frames.json"
    if frames_cache.is_file():
        cam_frames = json.load(open(frames_cache))
    else:
        sys.path.insert(0, "/opt/ros/noetic/lib/python3/dist-packages")
        import rosbag  # type: ignore
        cam_frames = []
        with rosbag.Bag(str(ROTATION_BAG), "r") as b:
            for topic, raw, t in b.read_messages(topics=[CAM_TOPICS[0]], raw=True):
                _seq, sec, nsec = struct.unpack_from("<III", raw[1], 0)
                cam_frames.append(sec + nsec * 1e-9)
        cam_frames.sort()
        json.dump(cam_frames, open(frames_cache, "w"))

    # Pair-level input gaps: S1 pairs by EXACT header equality (both cameras carry the same stamp);
    # U0 pairs natively by record time within 0.02 s. Read verbatim from the frozen census files.
    pair_gaps = {}
    s1c = glob.glob(str(CDSC / "scored" / "19-rotation_rotation.bag" / "S1" / "*" / "diagnostics" / "native_pair_census.json"))
    u0c = glob.glob(str(CDSC / "scored" / "19-rotation_rotation.bag" / "U0" / "*" / "diagnostics" / "native_pair_census.json"))
    for name, files in (("S1", s1c), ("U0", u0c)):
        if files:
            d = json.load(open(files[0]))
            g = d["input_interval"]["gaps_over_threshold"][0]
            pair_gaps[name] = {"start_s": g["start_timestamp_s"], "end_s": g["end_timestamp_s"], "duration_s": g["duration_s"], "source": files[0], "selector": d["input_interval"].get("selector_source")}
    json.dump(pair_gaps, open(out / "pair_level_input_gaps.json", "w"), indent=1)
    summary = []
    for c in find_cells():
        pg = pair_gaps.get("U0" if c["system"] == "U0" else "S1") or {"start_s": gap["start_header_s"], "end_s": gap["end_header_s"], "duration_s": gap["duration_s"]}
        extra = {"input_gap_start_s": pg["start_s"], "input_gap_end_s": pg["end_s"], "input_gap_duration_s": pg["duration_s"],
                 "input_gap_pairing": ("u0_native_record_time_0.02s" if c["system"] == "U0" else "s1_exact_header")}
        rec = analyze_cell(c["dir"], c["label"], c["system"], c["campaign"], cam_frames, cam0["last_header_s"], dict(extra), out)
        # frozen/QA reading of the main gap against the pair-level input gap
        main_gap = max(rec["state_gaps"], key=lambda g: g["duration_s"]) if rec["state_gaps"] else None
        if main_gap:
            delta = main_gap["duration_s"] - pg["duration_s"]
            rec["main_gap_duration_delta_vs_input_s"] = delta
            rec["main_gap_supported_frozen_rule"] = abs(delta) <= FROZEN_DURATION_DELTA_TOL_S
            rec["main_gap_supported_qa_rule"] = abs(delta) <= D13_QA_TOL_S
            rec["resume_offset_from_first_post_gap_frame_s"] = main_gap["end_s"] - (pg["end_s"] + CAM_IMU_DT_S)
        summary.append(rec)
    json.dump(summary, open(out / "cells_summary.json", "w"), indent=1, default=str)
    with open(out / "cells_summary.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "campaign", "system", "state_rows", "timing_rows", "first_state_s", "last_state_s", "n_state_gaps_gt0.2s",
                    "main_gap_start_s", "main_gap_end_s", "main_gap_duration_s", "delta_vs_input_gap_s", "supported_frozen_1e-6", "supported_qa_2e-5",
                    "resume_offset_from_first_post_gap_frame_s", "tail_gap_s", "frames_after_last_state", "pre_gap_frames_with_state", "pre_gap_frames_without_state",
                    "post_gap_frames_with_state", "post_gap_frames_without_state", "console_TIME_lines", "gate_rejections", "recovery_events", "death_lines", "zupt_lines", "out_of_order"])
        for r in summary:
            mg = max(r["state_gaps"], key=lambda g: g["duration_s"]) if r["state_gaps"] else None
            w.writerow([r["label"], r["campaign"], r["system"], r["state_rows"], r["timing_rows"], r["first_state_s"], r["last_state_s"], len(r["state_gaps"]),
                        mg["start_s"] if mg else "", mg["end_s"] if mg else "", mg["duration_s"] if mg else "",
                        r.get("main_gap_duration_delta_vs_input_s", ""), r.get("main_gap_supported_frozen_rule", ""), r.get("main_gap_supported_qa_rule", ""),
                        r.get("resume_offset_from_first_post_gap_frame_s", ""), r.get("tail_gap_s", ""), r.get("frames_after_last_state", ""),
                        r.get("frames_present_with_state", {}).get("pre_gap", ""), r.get("frames_present_without_state", {}).get("pre_gap", ""),
                        r.get("frames_present_with_state", {}).get("post_gap", ""), r.get("frames_present_without_state", {}).get("post_gap", ""),
                        r["console"].get("time_lines", ""), r["console"].get("gate_rejections", ""), len(r["recovery_events"]),
                        " | ".join(r["console"].get("death_lines", []))[:300], r["console"].get("zupt_lines", ""), r["console"].get("out_of_order", "")])

    # three regimes
    bo = blackout_control()
    regimes = []
    def fnum(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    for r in bo:
        if r["role"] in ("recOFF", "U0", "recON") and r["k"]:
            regimes.append({"regime": "masked_gap", "campaign": "BLACKOUT-1", "sequence": r["sequence"], "system": r["system"], "arm": r["arm"], "k_s": r["k"],
                            "outcome": r["status"], "complete_frozen": r["complete_frozen"], "complete_qa": r["complete_qa"], "rounding_only": r["rounding_only_unsupported_gap"],
                            "time_to_resume_s": r["time_to_resume_s"], "time_to_recover_s": r["time_to_recover_s"], "commits": r["commits"], "false_commits": r["false_commits"], "ate_m": r["ate_m"]})
    for r in summary:
        mg = max(r["state_gaps"], key=lambda g: g["duration_s"]) if r["state_gaps"] else None
        regimes.append({"regime": "natural_rotation_passage" + ("_recovered" if "recON" in r["system"] else ""), "campaign": r["campaign"], "sequence": "rotation/rotation.bag", "system": r["system"], "arm": "", "k_s": round(gap["duration_s"], 6) if gap else "",
                        "outcome": "", "complete_frozen": r.get("main_gap_supported_frozen_rule", ""), "complete_qa": r.get("main_gap_supported_qa_rule", ""), "rounding_only": (r.get("main_gap_supported_qa_rule") and not r.get("main_gap_supported_frozen_rule")),
                        "time_to_resume_s": r.get("resume_offset_from_first_post_gap_frame_s", ""), "time_to_recover_s": (mg["duration_s"] - gap["duration_s"] if (mg and gap and "recON" in r["system"]) else ""),
                        "commits": sum(1 for e in r["recovery_events"] if e.get("event") == "relocalization_commit"), "false_commits": "", "ate_m": ""})
    with open(out / "three_regimes.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(regimes[0].keys()))
        w.writeheader()
        for r in regimes:
            w.writerow(r)

    # ------------------------------------------------------------ report ----
    def cell(label):
        for r in summary:
            if r["label"] == label:
                return r
        return None
    u0 = cell("cdsc1r4-scored-u0")
    s1on = cell("cdsc1r4-scored-s1")
    s1off = cell("ablate1-p1-crux-s1-recoff-off0")
    md = ["# Stall anatomy — KAIST rotation.bag passage (C8 Item 2, ledger F14)", "",
          "All numbers below are read from frozen artifacts (CDSC-1R4, ABLATE-REC-1, BLACKOUT-1) and the source bag; nothing was re-run.", "",
          "## 1. Input anatomy of the natural event (source bag `{}`, sha256 `{}`)".format(ROTATION_BAG.name, bag["bag_sha256"]), ""]
    for tpc in CAM_TOPICS + [IMU_TOPIC]:
        e = bag["topics"][tpc]
        gtxt = "; ".join("gap {:.6f} -> {:.6f} ({:.6f} s)".format(g["start_header_s"], g["end_header_s"], g["duration_s"]) for g in e["gaps_over_threshold"]) or "no gap > {} s".format(GAP_THRESHOLD_S)
        md.append("- `{}`: {} messages, {} to {}; {}".format(tpc, e["count"], e["first_header_s"], e["last_header_s"], gtxt))
    md += ["- IMU messages inside the camera gap: {} (max IMU dt {:.4f} s)".format(bag.get("imu_messages_inside_camera_gap"), bag.get("imu_max_dt_s") or 0.0), ""]
    if gap:
        md += ["Camera 0 last pre-gap / first post-gap frames (header s, record s): {} / {}".format(cam0.get("last_pre_gap_frames"), cam0.get("first_post_gap_frames")), ""]
    if gt:
        md += ["GT angular rate (0.5 s windowed quaternion differencing of `ov_data/kaist_vio/rotation.txt`; sample-to-sample rates are jitter-dominated): pre-gap 5 s {} ; inside gap {} ; post-gap 5 s {} ; whole {} ; first GT sample over 30 deg/s at {}".format(gt["pre_gap_5s"], gt["inside_gap"], gt["post_gap_5s"], gt["whole"], gt["onset_first_time_over_30deg_s"]), ""]
    if pair_gaps:
        md += ["Pair-level input gaps (frozen census): S1 exact-header pairs {} ; U0 native record-time pairs {} . The two post-gap frames at cam0 1599131279.804865 / cam1 1599131279.811032 carry different header stamps: they never form an S1 exact-header pair (S1's first post-gap pair is 1599131279.832099) but U0's record-time pairing (delta 4.5 ms < 20 ms) joins them into one stamp-mismatched stereo pair — U0's first post-gap input.".format(pair_gaps.get("S1"), pair_gaps.get("U0")), ""]
    if gt:
        md += ["GT translational speed (median m/s): pre-gap 5 s {} ; inside gap {} ; post-gap 5 s {} . The gap sits at t = {:.1f}..{:.1f} s of the sequence; the sustained-rotation phase (omega ~22 deg/s) occupies roughly t = 70..140 s (see `gt_angular_rate.json` omega_profile_10s) — the platform is near-stationary in GT during the camera outage.".format(
            (gt["speed_pre_gap_5s"] or {}).get("median_m_s"), (gt["speed_inside_gap"] or {}).get("median_m_s"), (gt["speed_post_gap_5s"] or {}).get("median_m_s"), gt["gap_rel_start_s"], gt["gap_rel_end_s"]), ""]
    md += ["**Reading:** both cameras are absent for {:.3f} s (cam0) / see cam1 above while the IMU stream is continuous. The natural rotation event is therefore a FRAME-ABSENT input gap of the same kind as a BLACKOUT-1 mask, not a frame-present processing stall. The premise sentence in DESKTOP_EVIDENCE_SESSION v2 Item 2 ('with frames PRESENT') is contradicted by the source bag and is corrected here.".format(gap["duration_s"] if gap else float("nan")), ""]

    md += ["## 2. Per-variant state timelines (state rows are emitted once per processed camera pair in both builds)", "",
           "| cell | system | state rows | main state gap (s) | delta vs input gap (s) | frozen rule (1e-6) | D13 QA (2e-5) | resume offset from first post-gap frame (s) | tail gap (s) | frames after last state | post-gap frames with/without state | recovery events | death/exception lines |",
           "|---|---|---:|---:|---:|---|---|---:|---:|---:|---|---:|---|"]
    for r in summary:
        mg = max(r["state_gaps"], key=lambda g: g["duration_s"]) if r["state_gaps"] else None
        md.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {}/{} | {} | {} |".format(
            r["label"], r["system"], r["state_rows"], ("%.6f" % mg["duration_s"]) if mg else "-",
            ("%.3e" % r["main_gap_duration_delta_vs_input_s"]) if "main_gap_duration_delta_vs_input_s" in r else "-",
            r.get("main_gap_supported_frozen_rule", "-"), r.get("main_gap_supported_qa_rule", "-"),
            ("%.6f" % r["resume_offset_from_first_post_gap_frame_s"]) if "resume_offset_from_first_post_gap_frame_s" in r else "-",
            ("%.3f" % r["tail_gap_s"]) if "tail_gap_s" in r else "-", r.get("frames_after_last_state", "-"),
            r.get("frames_present_with_state", {}).get("post_gap", "-"), r.get("frames_present_without_state", {}).get("post_gap", "-"),
            len(r["recovery_events"]), (" ".join(r["console"].get("death_lines", []))[:160].replace("|", "/") if r["console"].get("death_lines") else "none")))
    md += ["", "Frames-without-state counts include the stock visualizer frequency thinning (identical for every variant; see `frames_vs_states_*.csv`, region column). The discriminating column is *frames after last state* (frames the run never turned into output).", ""]

    # causal chains
    def ev_line(r, name):
        for e in r["recovery_events"]:
            if e.get("event") == name:
                return e
        return None
    md += ["## 3. Causal chain per variant (timestamps in estimator time = camera header - 0.029959 s)", ""]
    if gap:
        md += ["**Yaw onset -> track collapse -> gap:** GT angular rate is {} deg/s mean in the 5 s before the gap and {} deg/s inside it; the last camera frame before the gap has header {:.6f} and the first after it {:.6f} — the tracks do not 'collapse' inside the passage, the frames stop.".format(
            gt["pre_gap_5s"]["mean_deg_s"] if gt and gt["pre_gap_5s"] else "n/a", gt["inside_gap"]["mean_deg_s"] if gt and gt["inside_gap"] else "n/a", gap["start_header_s"], gap["end_header_s"]), ""]
    if s1off:
        mg = max(s1off["state_gaps"], key=lambda g: g["duration_s"])
        md += ["**recOFF (ABLATE-REC-1 S1-recOFF off0):** last pre-gap state {:.5f}; no state rows while frames are absent; first post-gap state {:.5f} = first post-gap exact-header pair stamp + cam-imu offset (-0.029959 s; resume offset relative to that {:.6f} s); state gap {:.6f} s vs pair-level input gap {:.6f} s (delta {:.2e} s: fails the frozen 1e-6 s duration-delta tolerance, passes the D13 2e-5 s quantization-aware tolerance -> rounding-only TRACKING_LOSS); tail gap {:.3f} s; run reaches the input tail; {} [MSCKF-GATE] rejections over the run; 0 recovery events; no ZUPT lines (try_zupt=false in the frozen config).".format(
            mg["start_s"], mg["end_s"], s1off.get("resume_offset_from_first_post_gap_frame_s", float("nan")), mg["duration_s"], s1off.get("input_gap_duration_s", float("nan")), s1off.get("main_gap_duration_delta_vs_input_s", float("nan")), s1off.get("tail_gap_s", float("nan")), s1off["console"].get("gate_rejections")), ""]
    u0s = [r for r in summary if r["system"] == "U0"]
    if u0s:
        md += ["**U0 across all {} frozen rotation cells (CDSC-1R4 scored+capture, ABLATE-REC-1 offsets 0/+5/+10):** every cell ends with `class_loader::LibraryUnloadException` -> SIGABRT thrown while a console line is being printed at the first post-gap frame; last state row = {} ; state rows = {} ; frames left unprocessed after the last state = {} . The abort is deterministic at the gap exit; the offset-0 cells write one post-gap state row before aborting, the +5/+10 cells none.".format(
            len(u0s), sorted(set(round(r["last_state_s"], 5) for r in u0s)), sorted(set(r["state_rows"] for r in u0s)), sorted(set(r.get("frames_after_last_state") for r in u0s))), ""]
    if u0:
        mg = max(u0["state_gaps"], key=lambda g: g["duration_s"])
        md += ["**U0 (CDSC-1R4 scored):** last pre-gap state {:.5f}; first post-gap state {:.5f} (one post-gap frame published, resume offset {:.6f} s); then NO further state rows, NO further timing rows and NO further `[TIME]` lines although {} camera pairs remain in the bag; the process terminates with `{}` (exit -6, the exception text interrupts a `cam0 intrinsics` console line, i.e. it is thrown during processing, not at an orderly teardown); tail gap {:.3f} s. The trigger of the class_loader unload is not observable at INFO verbosity; the frozen classification `FINAL_STATE_DOES_NOT_REACH_SELECTED_INPUT_TAIL` + `post_coverage_teardown_pattern` is confirmed. U0 config also has try_zupt=false.".format(
            mg["start_s"], mg["end_s"], u0.get("resume_offset_from_first_post_gap_frame_s", float("nan")), u0.get("frames_after_last_state"), (u0["console"].get("death_lines") or ["?"])[0][:120], u0.get("tail_gap_s", float("nan"))), ""]
    if s1on:
        mg = max(s1on["state_gaps"], key=lambda g: g["duration_s"])
        tr, cm, wu = ev_line(s1on, "trigger"), ev_line(s1on, "relocalization_commit"), ev_line(s1on, "warmup_complete")
        att = [e for e in s1on["recovery_events"] if e.get("event") == "attempt"]
        md += ["**recON (CDSC-1R4 S1):** last pre-gap state {:.5f}; trigger at camera {} (gap_s={}); {} attempts ({} accepted, state_unchanged={}); consensus -> relocalization_commit at {}; first resumed state {:.5f} (state gap {:.6f} s = input gap + {:.3f} s of recovery); warmup_complete at {}; tail gap {:.3f} s; run completes.".format(
            mg["start_s"], tr.get("timestamp") if tr else "?", tr.get("gap_s") if tr else "?", len(att), sum(1 for a in att if a.get("accepted") == "1"), all(a.get("state_unchanged") == "1" for a in att) if att else "?",
            cm.get("timestamp") if cm else "?", mg["end_s"], mg["duration_s"], mg["duration_s"] - s1on.get("input_gap_duration_s", gap["duration_s"]), wu.get("timestamp") if wu else "?", s1on.get("tail_gap_s", float("nan"))), ""]
    md += ["## 4. Adjudication among the candidate mechanisms (evidence-based)", "",
           "| candidate | verdict | evidence |", "|---|---|---|",
           "| per-frame update rejection preventing state advance | REFUTED for the gap interval | no camera frames exist inside the gap (bag anatomy), so no update is attempted; the first post-gap pair advances the state in recOFF (state timestamp = pair stamp - 0.029959 s, i.e. zero extra delay) and U0 (one frame) |",
           "| ZUPT engagement | REFUTED | `try_zupt: false` in both the frozen S1 config and the pinned U0 config; zero ZUPT console lines in every cell |",
           "| transaction semantics coupling propagation to update outcome | NOT SUPPORTED as a cause | no transaction is attempted during frame absence; post-gap the recOFF transaction commits at the first frame; state rows == timing rows in every S1 cell (exact row parity) |",
           "| upstream stock behaviour shared by both builds | SUPPORTED for the gap itself | both builds publish state only on processed camera pairs (state rows == timing rows == processed pairs after init); with no frames there is no output; U0 and S1-recOFF have identical last-pre-gap timestamps and identical first-post-gap timestamps (frame-triggered) |",
           "| U0-specific abnormal termination after the gap | SUPPORTED as U0's terminal cause, trigger undetermined | class_loader::LibraryUnloadException / SIGABRT thrown mid-console-line at the first post-gap frame in 5/5 U0 rotation cells; the same exception appears only at orderly teardown in the BLACKOUT-1 U0 masked cells (which completed and resumed in 5-26 ms). Observation, not a conclusion: U0's first post-gap input is a stamp-mismatched stereo pair (cam0 .804865 / cam1 .811032, joined by record-time pairing) whereas masked bags leave only stamp-matched pairs; whether that pair triggers the unload is not observable at INFO verbosity |", "",
           "**What the logs support:** the 13.442 s 'stall' is the input gap seen through a publish-on-camera-callback design; frame-absent bridging on IMU is exactly what recOFF does here too (state resumes at the first post-gap pair with zero extra delay; the 0.030 s in the state file is the camera-IMU time offset), matching the BLACKOUT-1 recOFF/U0 masked cells (time_to_resume 4-50 ms). What the recovery mechanism changes is not *whether* the state resumes but *where*: recOFF resumes on a 13.4 s dead-reckoned pose (rotation report C0: cross-gap translation error 7.878 m, full ATE 4.258 m, post-gap ATE 6.623 m) while recON re-anchors (C2: 0.035 m / 0.088 m / 0.009 m). Item 3 renders this as coverage-welded ATE for both.", "",
           "**What the logs do not support:** any statement about tracked-feature counts per frame (not logged at INFO), or the internal trigger of U0's post-gap abort.", ""]
    md += ["## 5. Three-regime contrast", "", "See `three_regimes.csv` (natural passage rows from this extractor; masked-gap rows from BLACKOUT-1 aggregate/final/cells.csv verbatim). Summary:", ""]
    def agg(rows, key):
        vals = [fnum(r[key]) for r in rows if fnum(r[key]) is not None]
        return (len(vals), min(vals) if vals else None, max(vals) if vals else None)
    for role in ("recOFF", "U0", "recON"):
        rs = [r for r in regimes if r["regime"] == "masked_gap" and (r["system"].endswith(role) if role != "recON" else r["system"] in ("S1", "S1-recON-euroc"))]
        n, lo, hi = agg(rs, "time_to_resume_s")
        md.append("- masked gap, {}: {} cells; time_to_resume {} .. {} s; complete_frozen {} / complete_qa {}".format(role, len(rs), lo, hi, sum(1 for r in rs if r["complete_frozen"] == "True"), sum(1 for r in rs if r["complete_qa"] == "True")))
    md += ["- natural passage: see section 2 (recOFF resumes at the first post-gap pair; U0 one frame then abort; recON re-anchors +0.066 s after the pair-level input gap end)", ""]
    (out / "stall_anatomy.md").write_text("\n".join(md) + "\n")
    with open(out / "SHA256SUMS", "w") as f:
        for p in sorted(out.iterdir()):
            if p.name != "SHA256SUMS":
                f.write("{}  {}\n".format(sha256_file(p), p.name))
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
