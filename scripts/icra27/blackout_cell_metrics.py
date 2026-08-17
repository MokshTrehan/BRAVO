#!/usr/bin/python3.8
"""BLACKOUT-1 per-cell ground-truth metrics for the amended prereg (DECISIONS D8).

Descriptive metrics computed AFTER the runner proved estimator closure (ground
truth opened only here):
  * pre-mask alignment: SE(3) Umeyama (no scale, evo 1.31.1) fitted on the
    cell's own estimate-to-GT association restricted to [first state, t_b);
  * per commit: committed position mapped through that alignment vs GT position
    at the commit camera timestamp -> commit_position_error_m and the
    FALSE-COMMIT flag (> 0.5 m; 0.25 m / 1.0 m descriptive), |v_GT| at commit
    (nearest-sample central difference and +-0.5 s window mean) and the
    velocity-assumption exposure flag (> 0.3 m/s);
  * post-mask position error under the PRE-MASK alignment over the first 10 s
    after state output resumes (re-anchor consistency, any system);
  * post-recovery RPE at 1.0 s time delta over [commit state output, +10 s].
The frozen per-cell ATE/RPE (perturbation_cell_evaluator.py) stays the accuracy
metric; nothing here gates completion.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
from evo.core import metrics, sync, trajectory
from evo.core.metrics import PoseRelation, Unit

SCHEMA = "schurvio.icra27.blackout.cell_gt_metrics.v1"
ASSOCIATION_MAX_SECONDS = 0.01
MIN_ALIGN_POSES = 30
FALSE_COMMIT_THRESHOLD_M = 0.5
DESCRIPTIVE_THRESHOLDS_M = (0.25, 0.5, 1.0)
VELOCITY_EXPOSURE_MPS = 0.3
POST_WINDOW_S = 10.0
RPE_DELTA_S = 1.0


class MetricsError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_tum(path: Path, max_columns: Optional[int] = None) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 8:
            raise MetricsError("row with fewer than 8 columns in {}".format(path))
        rows.append([float(x) for x in parts[:8]])
    if not rows:
        raise MetricsError("no rows in {}".format(path))
    data = np.asarray(rows, dtype=float)
    order = np.argsort(data[:, 0], kind="stable")
    data = data[order]
    return data[:, 0], data[:, 1:4], data[:, 4:8]


def to_evo(times: np.ndarray, positions: np.ndarray, quat_xyzw: np.ndarray) -> trajectory.PoseTrajectory3D:
    quat = quat_xyzw / np.linalg.norm(quat_xyzw, axis=1, keepdims=True)
    return trajectory.PoseTrajectory3D(
        positions_xyz=positions, orientations_quat_wxyz=np.roll(quat, 1, axis=1), timestamps=times
    )


def speed_profile(times: np.ndarray, positions: np.ndarray) -> np.ndarray:
    speed = np.empty(times.shape[0])
    speed[1:-1] = np.linalg.norm(positions[2:] - positions[:-2], axis=1) / (times[2:] - times[:-2])
    speed[0] = np.linalg.norm(positions[1] - positions[0]) / (times[1] - times[0])
    speed[-1] = np.linalg.norm(positions[-1] - positions[-2]) / (times[-1] - times[-2])
    return speed


def gt_position_at(times: np.ndarray, positions: np.ndarray, t: float) -> Tuple[Optional[np.ndarray], float]:
    index = int(np.searchsorted(times, t))
    if index <= 0 or index >= times.shape[0]:
        j = min(max(index, 0), times.shape[0] - 1)
        return (positions[j].copy(), abs(times[j] - t)) if abs(times[j] - t) <= 0.05 else (None, abs(times[j] - t))
    t0, t1 = times[index - 1], times[index]
    if t1 - t0 > 0.5:
        return None, min(t - t0, t1 - t)
    w = (t - t0) / (t1 - t0)
    return (1 - w) * positions[index - 1] + w * positions[index], 0.0


def gt_speed_at(times: np.ndarray, speed: np.ndarray, t: float, half: float = 0.5) -> Dict[str, Any]:
    index = int(np.searchsorted(times, t))
    cands = [i for i in (index - 1, index) if 0 <= i < times.shape[0]]
    best = min(cands, key=lambda i: abs(times[i] - t))
    lo = int(np.searchsorted(times, t - half, side="left"))
    hi = int(np.searchsorted(times, t + half, side="right"))
    return {
        "instantaneous_mps": float(speed[best]),
        "nearest_gt_sample_distance_s": float(abs(times[best] - t)),
        "window_mean_mps": float(np.mean(speed[lo:hi])) if hi > lo else None,
        "window_half_width_s": half,
        "window_samples": int(hi - lo),
    }


def associate(ref: trajectory.PoseTrajectory3D, est: trajectory.PoseTrajectory3D):
    try:
        return sync.associate_trajectories(ref, est, max_diff=ASSOCIATION_MAX_SECONDS)
    except Exception as exc:  # evo raises on empty association
        raise MetricsError("association failed: {}".format(exc)) from exc


def evaluate(run_directory: Path, ground_truth: Path, output: Path) -> Dict[str, Any]:
    result_path = run_directory / "sequence_result.json"
    manifest = json.loads(result_path.read_text())
    close = manifest.get("estimator_close_receipt") or {}
    if close.get("estimator_process_group_closed") is not True or close.get("runtime_services_closed") is not True:
        raise MetricsError("estimator process group is not proven closed; ground truth stays sealed")
    if output.exists():
        raise MetricsError("refusing to overwrite {}".format(output))
    blackout = manifest.get("blackout") or {}
    mechanism = manifest.get("robustness_mechanism") or {}
    recovery = mechanism.get("blackout_recovery") or {}
    completion = manifest.get("completion") or {}
    base: Dict[str, Any] = {
        "schema": SCHEMA,
        "campaign_id": "BLACKOUT-1",
        "run_id": manifest.get("run_id"),
        "sequence": manifest.get("sequence"),
        "system": manifest.get("system"),
        "status": manifest.get("status"),
        "evidence_validity": manifest.get("evidence_validity"),
        "sequence_result_sha256": _sha(result_path),
        "ground_truth": {"path": str(ground_truth), "sha256": _sha(ground_truth)},
        "ground_truth_opened_after_estimator_close": True,
        "evo_version": __import__("evo").__version__,
        "parameters": {
            "association_max_seconds": ASSOCIATION_MAX_SECONDS,
            "min_align_poses": MIN_ALIGN_POSES,
            "false_commit_threshold_m": FALSE_COMMIT_THRESHOLD_M,
            "descriptive_thresholds_m": list(DESCRIPTIVE_THRESHOLDS_M),
            "velocity_exposure_mps": VELOCITY_EXPOSURE_MPS,
            "post_window_s": POST_WINDOW_S,
            "rpe_delta_s": RPE_DELTA_S,
        },
        "mask_start_s": blackout.get("mask_start_s"),
        "mask_end_s": blackout.get("mask_end_s"),
        "mask_active": blackout.get("mask_active"),
        "pre_mask_alignment": None,
        "commits": [],
        "false_commit_count": 0,
        "velocity_exposure_count": 0,
        "post_mask_under_pre_mask_alignment": None,
        "post_recovery_rpe_1s": None,
        "metrics_status": None,
    }
    tum_path = run_directory / "trajectory" / "estimate_raw.tum"
    if not tum_path.is_file():
        base["metrics_status"] = "NO_TRAJECTORY"
        _commit(output, base)
        return base
    gt_t, gt_p, gt_q = load_tum(ground_truth)
    est_t, est_p, est_q = load_tum(tum_path)
    gt_speed = speed_profile(gt_t, gt_p)
    mask_start = float(blackout.get("mask_start_s") or math.inf)
    mask_end = float(blackout.get("mask_end_s") or math.inf)
    active = bool(blackout.get("mask_active"))
    ref = to_evo(gt_t, gt_p, gt_q)
    # --- pre-mask alignment
    pre_mask = est_t < mask_start if active else np.ones_like(est_t, dtype=bool)
    if int(pre_mask.sum()) < MIN_ALIGN_POSES:
        base["pre_mask_alignment"] = {"status": "INSUFFICIENT_PRE_MASK_POSES", "estimate_rows_pre_mask": int(pre_mask.sum())}
        base["metrics_status"] = "NO_PRE_MASK_ALIGNMENT"
        _commit(output, base)
        return base
    est_pre = to_evo(est_t[pre_mask], est_p[pre_mask], est_q[pre_mask])
    ref_a, est_a = associate(ref, est_pre)
    if est_a.num_poses < MIN_ALIGN_POSES:
        base["pre_mask_alignment"] = {"status": "INSUFFICIENT_ASSOCIATED_PRE_MASK_POSES", "associated": int(est_a.num_poses)}
        base["metrics_status"] = "NO_PRE_MASK_ALIGNMENT"
        _commit(output, base)
        return base
    est_aligned = copy.deepcopy(est_a)
    R, t, s = est_aligned.align(ref_a, correct_scale=False, correct_only_scale=False, n=-1)
    ape = metrics.APE(PoseRelation.translation_part)
    ape.process_data((ref_a, est_aligned))
    base["pre_mask_alignment"] = {
        "status": "OK",
        "method": "evo SE(3) Umeyama, correct_scale=False, all pre-mask associated poses",
        "estimate_rows_pre_mask": int(pre_mask.sum()),
        "associated_poses": int(est_a.num_poses),
        "interval_s": [float(est_t[pre_mask][0]), float(est_t[pre_mask][-1])],
        "rotation_matrix": np.asarray(R).tolist(),
        "translation_m": np.asarray(t).tolist(),
        "scale": float(s),
        "pre_mask_ate_rmse_m": float(ape.get_statistic(metrics.StatisticsType.rmse)),
        "pre_mask_ate_max_m": float(ape.get_statistic(metrics.StatisticsType.max)),
    }
    R = np.asarray(R)
    t = np.asarray(t).reshape(3)

    def to_gt_frame(p: np.ndarray) -> np.ndarray:
        return R @ p + t

    # --- commits
    covariances = {int(c.get("epoch", -1)): c for c in recovery.get("first_resumed_covariances", [])}
    for c in recovery.get("commit_events", []):
        t_c = float(c["timestamp"])
        p_c = np.asarray([float(c["p_x"]), float(c["p_y"]), float(c["p_z"])])
        mapped = to_gt_frame(p_c)
        gt_pos, dist = gt_position_at(gt_t, gt_p, t_c)
        error = None if gt_pos is None else float(np.linalg.norm(mapped - gt_pos))
        speed = gt_speed_at(gt_t, gt_speed, t_c)
        cov = covariances.get(int(c.get("epoch", -1)))
        record = {
            "epoch": c.get("epoch"),
            "camera_timestamp_s": t_c,
            "state_output_timestamp_s": float(cov["state_output_timestamp"]) if cov else None,
            "seconds_after_mask_end": t_c - mask_end if active else None,
            "committed_position_estimator_frame_m": p_c.tolist(),
            "committed_position_gt_frame_m": mapped.tolist(),
            "gt_position_m": None if gt_pos is None else gt_pos.tolist(),
            "gt_interpolation_gap_s": dist,
            "commit_position_error_m": error,
            "false_commit": (error is not None and error > FALSE_COMMIT_THRESHOLD_M),
            "error_exceeds_m": {str(th): (error is not None and error > th) for th in DESCRIPTIVE_THRESHOLDS_M},
            "gt_speed_at_commit": speed,
            "velocity_exposure": bool(speed["instantaneous_mps"] > VELOCITY_EXPOSURE_MPS),
        }
        base["commits"].append(record)
    base["false_commit_count"] = sum(1 for c in base["commits"] if c["false_commit"])
    base["commit_error_unassessable_count"] = sum(1 for c in base["commits"] if c["commit_position_error_m"] is None)
    base["velocity_exposure_count"] = sum(1 for c in base["commits"] if c["velocity_exposure"])
    # --- post-mask consistency under the pre-mask alignment
    if active:
        after = est_t >= mask_end
        if int(after.sum()) > 0:
            resume = float(est_t[after][0])
            window = after & (est_t <= resume + POST_WINDOW_S)
            est_post = to_evo(est_t[window], est_p[window], est_q[window])
            try:
                ref_w, est_w = associate(ref, est_post)
                mapped = (R @ est_w.positions_xyz.T).T + t
                errors = np.linalg.norm(mapped - ref_w.positions_xyz, axis=1)
                base["post_mask_under_pre_mask_alignment"] = {
                    "resume_timestamp_s": resume,
                    "window_s": [resume, resume + POST_WINDOW_S],
                    "associated_poses": int(est_w.num_poses),
                    "position_error_m": {
                        "first": float(errors[0]),
                        "median": float(np.median(errors)),
                        "rmse": float(np.sqrt(np.mean(errors ** 2))),
                        "max": float(np.max(errors)),
                    },
                }
            except MetricsError as exc:
                base["post_mask_under_pre_mask_alignment"] = {"status": "NO_ASSOCIATION", "reason": str(exc)}
        else:
            base["post_mask_under_pre_mask_alignment"] = {"status": "NO_STATE_AFTER_MASK_END"}
        # --- post-recovery RPE (1 s delta) over [commit output, +10 s]
        outputs = [c["state_output_timestamp_s"] for c in base["commits"] if c["state_output_timestamp_s"] is not None]
        if outputs:
            t0 = min(outputs)
            window = (est_t >= t0) & (est_t <= t0 + POST_WINDOW_S)
            if int(window.sum()) >= 3:
                est_post = to_evo(est_t[window], est_p[window], est_q[window])
                try:
                    ref_w, est_w = associate(ref, est_post)
                    est_w_al = copy.deepcopy(est_w)
                    est_w_al.align(ref_w, correct_scale=False, n=-1)
                    rpe_t = metrics.RPE(PoseRelation.translation_part, delta=RPE_DELTA_S, delta_unit=Unit.seconds, all_pairs=False)
                    rpe_r = metrics.RPE(PoseRelation.rotation_angle_deg, delta=RPE_DELTA_S, delta_unit=Unit.seconds, all_pairs=False)
                    rpe_t.process_data((ref_w, est_w_al))
                    rpe_r.process_data((ref_w, est_w_al))
                    base["post_recovery_rpe_1s"] = {
                        "window_s": [t0, t0 + POST_WINDOW_S],
                        "associated_poses": int(est_w.num_poses),
                        "pairs": int(rpe_t.error.size),
                        "translation_rmse_m": float(rpe_t.get_statistic(metrics.StatisticsType.rmse)),
                        "translation_max_m": float(rpe_t.get_statistic(metrics.StatisticsType.max)),
                        "rotation_rmse_deg": float(rpe_r.get_statistic(metrics.StatisticsType.rmse)),
                        "note": "descriptive; 1 s time-delta RPE on the window-aligned association",
                    }
                except Exception as exc:  # noqa: BLE001 - descriptive metric, never fatal
                    base["post_recovery_rpe_1s"] = {"status": "UNAVAILABLE", "reason": str(exc)}
            else:
                base["post_recovery_rpe_1s"] = {"status": "TOO_FEW_STATE_ROWS", "rows": int(window.sum())}
    base["metrics_status"] = "COMPUTED"
    _commit(output, base)
    return base


def _commit(output: Path, value: Mapping[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=False)
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(output / "blackout_gt_metrics.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
    digest = hashlib.sha256(payload).hexdigest()
    (output / "SHA256SUMS").write_text("{}  blackout_gt_metrics.json\n".format(digest))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--ground-truth", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    try:
        value = evaluate(args.run.resolve(strict=True), args.ground_truth.resolve(strict=True), args.output)
    except (MetricsError, OSError, ValueError, KeyError) as exc:
        print("BLACKOUT_GT_METRICS_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("{}: false_commits={} commits={}".format(value["metrics_status"], value["false_commit_count"], len(value["commits"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
