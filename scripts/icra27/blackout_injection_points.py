#!/usr/bin/python3.8
"""BLACKOUT-1 Step-0 injection-point table (docs/icra27/BLACKOUT_PREREG.md, DECISIONS D1-D3).

For every campaign sequence this tool computes, deterministically and without
any hand adjustment:

  arm A  t_A  = view_start + 0.40 x view_duration, snapped to the nearest cam0
                header stamp (view = frozen CDSC-1R4 replay view; start/end are
                the first/last cam0 header stamps inside it);
  arm B  t_B  = centre of the minimum-mean-GT-speed 3 s window whose centre is
                a cam0 header stamp and whose window lies inside the initialized
                region of the frozen reference run [first_state_timestamp of the
                CDSC-1R4 S1 offset-0 run, view_end] (D2); ties -> earliest.
                The unconstrained global minimum is reported alongside.

GT speed = |central finite difference| of GT position per sample; window mean =
mean of per-sample speeds with sample times inside [c-1.5, c+1.5].  The table
also records the GT speed at each point and, per k, the mechanical NOT_RUNNABLE
flags of D3 (mask past end; arm overlap).  Output: JSON + Markdown, committed
before any run.  Ground truth is read here only (never by the estimator).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import blackout_mask  # noqa: E402
import cross_dataset_campaign as campaign  # noqa: E402
import perturbation_campaign as perturb  # noqa: E402

SCHEMA = "schurvio.icra27.blackout.injection_points.v1"
NS = blackout_mask.NS
SEQUENCES: Tuple[str, ...] = (
    "circle/circle.bag",
    "infinite/infinite.bag",
    "square/square.bag",
    "MH_05_difficult",
    "V2_02_medium",
)
DURATIONS: Tuple[int, ...] = (2, 5, 10, 15, 20)
WINDOW_SECONDS = 3.0
ARM_A_FRACTION = 0.40
TOPICS = {
    "kaist_vio": {
        "camera": ("/turnsafe/kaist/infra1/image_raw", "/turnsafe/kaist/infra2/image_raw"),
        "imu": "/mavros/imu/data",
    },
    "euroc_mav": {"camera": ("/cam0/image_raw", "/cam1/image_raw"), "imu": "/imu0"},
}


def load_ground_truth(path: Path) -> Tuple[np.ndarray, np.ndarray]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        rows.append([float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])])
    data = np.asarray(rows, dtype=float)
    if data.shape[0] < 3:
        raise RuntimeError("ground truth too short: {}".format(path))
    order = np.argsort(data[:, 0], kind="stable")
    data = data[order]
    return data[:, 0], data[:, 1:4]


def speed_profile(times: np.ndarray, positions: np.ndarray) -> np.ndarray:
    """Per-sample |v| by central finite differences (one-sided at the ends)."""
    speed = np.empty(times.shape[0], dtype=float)
    dt_c = times[2:] - times[:-2]
    dp_c = np.linalg.norm(positions[2:] - positions[:-2], axis=1)
    speed[1:-1] = dp_c / dt_c
    speed[0] = np.linalg.norm(positions[1] - positions[0]) / (times[1] - times[0])
    speed[-1] = np.linalg.norm(positions[-1] - positions[-2]) / (times[-1] - times[-2])
    return speed


class SpeedField:
    def __init__(self, times: np.ndarray, speed: np.ndarray) -> None:
        self.times = times
        self.speed = speed
        self.prefix = np.concatenate([[0.0], np.cumsum(speed)])

    def window_mean(self, center: float, half: float) -> Tuple[float, int]:
        lo = int(np.searchsorted(self.times, center - half, side="left"))
        hi = int(np.searchsorted(self.times, center + half, side="right"))
        n = hi - lo
        if n <= 0:
            return float("nan"), 0
        return float((self.prefix[hi] - self.prefix[lo]) / n), n

    def at(self, t: float) -> Tuple[float, float, float]:
        index = int(np.searchsorted(self.times, t))
        candidates = [i for i in (index - 1, index) if 0 <= i < self.times.shape[0]]
        best = min(candidates, key=lambda i: abs(self.times[i] - t))
        return float(self.speed[best]), float(self.times[best]), float(abs(self.times[best] - t))


def nearest_stamp(stamps_ns: Sequence[int], target_ns: int) -> int:
    array = np.asarray(stamps_ns, dtype=np.int64)
    index = int(np.searchsorted(array, target_ns))
    candidates = [i for i in (index - 1, index) if 0 <= i < array.shape[0]]
    # ties -> earlier frame (min over |d| with stable order favours the earlier index)
    best = min(candidates, key=lambda i: (abs(int(array[i]) - target_ns), i))
    return int(array[best])


def argmin_window(field: SpeedField, centers_ns: Sequence[int], lo_ns: int, hi_ns: int) -> Optional[Dict[str, Any]]:
    """Earliest centre with the minimum 3 s window mean, window inside [lo, hi] (all in ns)."""
    half_ns = int(WINDOW_SECONDS / 2 * NS)
    best: Optional[Dict[str, Any]] = None
    evaluated = 0
    for c in centers_ns:
        if c - half_ns < lo_ns or c + half_ns > hi_ns:
            continue
        mean, n = field.window_mean(c / NS, WINDOW_SECONDS / 2)
        if n == 0 or not np.isfinite(mean):
            continue
        evaluated += 1
        if best is None or mean < best["window_mean_speed_mps"] - 1e-12:
            best = {"center_header_stamp_ns": int(c), "window_mean_speed_mps": mean, "window_sample_count": n}
    if best is not None:
        best["candidate_centers_evaluated"] = evaluated
    return best


def reference_first_state(row: campaign.MatrixRow) -> Tuple[float, str]:
    directory = perturb.cdsc1r4_scored_directory(row, "S1")
    if directory is None:
        raise RuntimeError("no CDSC-1R4 S1 scored run for {}".format(row.sequence))
    value = json.loads((directory / "sequence_result.json").read_text())
    completion = value.get("completion") or {}
    first = completion.get("first_state_timestamp_s")
    if first is None:
        raise RuntimeError("CDSC-1R4 S1 run for {} lacks first_state_timestamp_s".format(row.sequence))
    return float(first), str(directory)


def build(rows: Sequence[campaign.MatrixRow], scan_dir: Path) -> Dict[str, Any]:
    by_sequence = {row.sequence: row for row in rows}
    table: List[Dict[str, Any]] = []
    for sequence in SEQUENCES:
        row = by_sequence[sequence]
        topics = TOPICS[row.dataset]
        bag = Path(str(row.bag["path"]))
        scan_path = scan_dir / (campaign.safe_sequence(sequence) + ".scan.json")
        if scan_path.is_file():
            scan = json.loads(scan_path.read_text())
        else:
            scan = blackout_mask.scan(bag, topics["camera"], float(row.bag_start_seconds))
            scan_path.parent.mkdir(parents=True, exist_ok=True)
            scan_path.write_text(json.dumps(scan, allow_nan=False, indent=1, sort_keys=True) + "\n")
        cam0 = scan["topics"][topics["camera"][0]]
        stamps = [int(v) for v in cam0["header_stamps_in_view_ns"]]
        view_start_ns = stamps[0]
        view_end_ns = stamps[-1]
        duration_ns = view_end_ns - view_start_ns
        gt_path = Path(str(row.ground_truth["canonical_path"]))
        times, positions = load_ground_truth(gt_path)
        field = SpeedField(times, speed_profile(times, positions))
        first_state, reference_dir = reference_first_state(row)
        init_ns = int(round(first_state * NS))

        target_a = view_start_ns + int(round(ARM_A_FRACTION * duration_ns))
        t_a = nearest_stamp(stamps, target_a)
        constrained = argmin_window(field, stamps, init_ns, view_end_ns)
        unconstrained = argmin_window(field, stamps, view_start_ns, view_end_ns)
        if constrained is None:
            raise RuntimeError("no feasible arm-B window for {}".format(sequence))
        t_b = int(constrained["center_header_stamp_ns"])

        def point(name: str, t_ns: int) -> Dict[str, Any]:
            inst, gt_t, gt_dt = field.at(t_ns / NS)
            mean3, n3 = field.window_mean(t_ns / NS, WINDOW_SECONDS / 2)
            return {
                "arm": name,
                "header_stamp_ns": t_ns,
                "header_stamp_s": t_ns / NS,
                "seconds_from_view_start": (t_ns - view_start_ns) / NS,
                "fraction_of_view": (t_ns - view_start_ns) / duration_ns,
                "seconds_after_reference_initialization": (t_ns - init_ns) / NS,
                "gt_speed_instantaneous_mps": inst,
                "gt_sample_time_s": gt_t,
                "gt_sample_distance_s": gt_dt,
                "gt_speed_3s_window_mean_mps": mean3,
                "gt_speed_3s_window_samples": n3,
                "per_k": {
                    str(k): {
                        "mask_end_ns": t_ns + k * NS,
                        "mask_past_end": t_ns + k * NS >= view_end_ns,
                        "post_mask_seconds": (view_end_ns - (t_ns + k * NS)) / NS,
                    }
                    for k in DURATIONS
                },
            }

        arm_a = point("A", t_a)
        arm_b = point("B", t_b)
        for k in DURATIONS:
            a0, a1 = t_a, t_a + k * NS
            b0, b1 = t_b, t_b + k * NS
            overlap = not (b1 < a0 or a1 < b0)
            arm_b["per_k"][str(k)]["overlaps_arm_a"] = overlap
            arm_a["per_k"][str(k)]["overlaps_arm_b"] = overlap
        table.append(
            {
                "order": row.order,
                "dataset": row.dataset,
                "sequence": sequence,
                "sequence_key": campaign.row_key(row),
                "bag": {"path": str(bag), "size_bytes": row.bag.get("size_bytes") or row.bag.get("bytes"), "sha256": row.bag.get("sha256")},
                "ground_truth": {"path": str(gt_path), "sha256": hashlib.sha256(gt_path.read_bytes()).hexdigest(), "rows": int(times.shape[0])},
                "camera_topics": list(topics["camera"]),
                "imu_topic": topics["imu"],
                "frozen_bag_start_seconds": float(row.bag_start_seconds),
                "view_start_header_stamp_ns": view_start_ns,
                "view_end_header_stamp_ns": view_end_ns,
                "view_duration_s": duration_ns / NS,
                "cam0_frames_in_view": len(stamps),
                "reference_first_state_timestamp_s": first_state,
                "reference_run_directory": reference_dir,
                "arm_A_target_ns_before_snap": target_a,
                "arm_A": arm_a,
                "arm_B": arm_b,
                "arm_B_search": {
                    "constrained": constrained,
                    "unconstrained": unconstrained,
                    "constrained_equals_unconstrained": bool(
                        unconstrained is not None
                        and unconstrained["center_header_stamp_ns"] == constrained["center_header_stamp_ns"]
                    ),
                    "feasible_center_range_ns": [init_ns + int(WINDOW_SECONDS / 2 * NS), view_end_ns - int(WINDOW_SECONDS / 2 * NS)],
                },
                "scan_file": str(scan_path),
            }
        )
    return {
        "schema": SCHEMA,
        "generated_utc": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rules": {
            "arm_A": "view_start + 0.40 x view_duration on cam0 header stamps inside the frozen replay view, snapped to the nearest cam0 frame (ties earlier)",
            "arm_B": "centre of the minimum-mean-GT-speed 3 s window; centres are cam0 header stamps; window inside [reference first_state_timestamp, view_end] (DECISIONS D2); ties earliest; unconstrained minimum reported alongside",
            "gt_speed": "|central finite difference| of GT position per sample; window mean over samples in [c-1.5, c+1.5]",
            "not_runnable": "mask_past_end: t_b + k >= view_end; overlaps_arm_a: arm-B masked interval intersects arm-A masked interval at the same k (D3)",
        },
        "durations_s": list(DURATIONS),
        "window_seconds": WINDOW_SECONDS,
        "sequences": table,
    }


def render_markdown(value: Dict[str, Any]) -> str:
    lines = [
        "# BLACKOUT-1 injection-point table (Step 0, committed before any run)",
        "",
        "Generated {} by scripts/icra27/blackout_injection_points.py. Rules: {}".format(value["generated_utc"], json.dumps(value["rules"], sort_keys=True)),
        "",
        "| seq | view start (s) | view dur (s) | ref init (+s) | arm A t_A (s) | +s from start | GT |v| at t_A (m/s) | 3 s mean at t_A | arm B t_B (s) | +s from start | GT |v| at t_B (m/s) | 3 s mean at t_B (min) | unconstrained min centre (+s) / mean | same? |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ]
    for s in value["sequences"]:
        a, b = s["arm_A"], s["arm_B"]
        u = s["arm_B_search"]["unconstrained"]
        lines.append(
            "| {} | {:.6f} | {:.3f} | {:.3f} | {:.6f} | {:.3f} | {:.4f} | {:.4f} | {:.6f} | {:.3f} | {:.4f} | {:.4f} | {:.3f} / {:.4f} | {} |".format(
                s["sequence"], s["view_start_header_stamp_ns"] / NS, s["view_duration_s"],
                s["reference_first_state_timestamp_s"] - s["view_start_header_stamp_ns"] / NS,
                a["header_stamp_s"], a["seconds_from_view_start"], a["gt_speed_instantaneous_mps"], a["gt_speed_3s_window_mean_mps"],
                b["header_stamp_s"], b["seconds_from_view_start"], b["gt_speed_instantaneous_mps"], b["gt_speed_3s_window_mean_mps"],
                (u["center_header_stamp_ns"] - s["view_start_header_stamp_ns"]) / NS if u else float("nan"), u["window_mean_speed_mps"] if u else float("nan"),
                s["arm_B_search"]["constrained_equals_unconstrained"],
            )
        )
    lines += ["", "Per-k runnability (D3): mask_past_end / overlaps_arm_a", "", "| seq | arm | k=2 | k=5 | k=10 | k=15 | k=20 |", "|---|---|---|---|---|---|---|"]
    for s in value["sequences"]:
        for arm in ("arm_A", "arm_B"):
            cells = []
            for k in value["durations_s"]:
                pk = s[arm]["per_k"][str(k)]
                flags = []
                if pk["mask_past_end"]:
                    flags.append("PAST_END")
                if arm == "arm_B" and pk.get("overlaps_arm_a"):
                    flags.append("OVERLAP")
                cells.append("ok ({:.1f} s after)".format(pk["post_mask_seconds"]) if not flags else "NOT_RUNNABLE " + "+".join(flags))
            lines.append("| {} | {} | {} |".format(s["sequence"], arm[-1], " | ".join(cells)))
    return "\n".join(lines) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-json", required=True, type=Path)
    p.add_argument("--output-md", required=True, type=Path)
    p.add_argument("--scan-dir", required=True, type=Path)
    args = p.parse_args(argv)
    rows = campaign.load_matrix(campaign.RuntimePaths().matrix)
    value = build(rows, args.scan_dir)
    args.output_json.write_text(json.dumps(value, allow_nan=False, indent=1, sort_keys=True) + "\n")
    args.output_md.write_text(render_markdown(value))
    print(render_markdown(value))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
