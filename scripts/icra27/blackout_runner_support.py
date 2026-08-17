#!/usr/bin/python3.8
"""BLACKOUT-1 support shared by the two CDSC-1R4 trial runners (DECISIONS D4, D6, D7).

Contains only replay-layer / evidence logic:
  * manifest binding: the runner keeps binding the FROZEN source bag to the
    matrix; the masked bag (what the estimator replays) is verified against the
    manifest written by blackout_mask.py and handed to the launch/census;
  * descriptive long-gap recovery parsing (every event line, attempt-reason
    histogram, per-attempt correspondence counts, commits, failures);
  * the declared recovery-supported completion seam (D7) on top of the frozen
    passage rule.
Nothing here edits estimator source, configs or thresholds.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

CAMPAIGN_ID = "BLACKOUT-1"
ARMS = ("A", "B")
DURATIONS = (0.0, 2.0, 5.0, 10.0, 15.0, 20.0)
MANIFEST_SCHEMA = "schurvio.icra27.blackout.mask_manifest.v1"
RECOVERY_PREFIX = "[LONG-GAP-RECOVERY]:"
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
KV_RE = re.compile(r"([a-z_0-9]+)=([^\s]+)")
GAP_SEAM_TOLERANCE_S = 0.10  # = frozen TAIL_GAP_MAX_SECONDS (input-support start tolerance) reused for the seam start
COMMIT_MATCH_TOLERANCE_S = 1.0e-5
CLOSURE_HORIZON_S = 30.0
NS = 1_000_000_000

ATTEMPT_REASONS = (
    "accepted",
    "invalid_calibration",
    "invalid_orientation_prior",
    "duplicate_correspondence_id",
    "insufficient_correspondences",
    "noncollinear_support_required",
    "pnp_failed",
    "insufficient_inliers",
    "inlier_ratio_too_low",
    "nonfinite_pose",
    "nonpositive_depth",
    "nonfinite_residual",
    "reprojection_error_too_high",
    "image_bounds_x_too_small",
    "image_bounds_y_too_small",
    "orientation_disagreement_too_large",
)


class BlackoutError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def requested(args: Any) -> bool:
    """Masked BLACKOUT-1 cell: campaign id BLACKOUT-1 with a manifest.  Integrity /
    inertness-free replicas run under the same campaign id WITHOUT a manifest and
    take the frozen (unmasked) path; the driver checks the result matches the plan."""
    return getattr(args, "perturbation_campaign_id", None) == CAMPAIGN_ID and getattr(args, "blackout_manifest", None) is not None


def load_manifest(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except (OSError, ValueError) as exc:
        raise BlackoutError("blackout manifest unreadable: {}".format(exc)) from exc
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise BlackoutError("blackout manifest schema mismatch")
    for key in ("source_bag", "masked_bag", "mask", "camera_topics", "imu_topic", "dropped_per_topic"):
        if key not in value:
            raise BlackoutError("blackout manifest lacks {}".format(key))
    if int(value.get("imu_messages_dropped", -1)) != 0 or int(value.get("non_camera_messages_dropped", -1)) != 0:
        raise BlackoutError("blackout manifest reports non-camera drops")
    return value


def bind(args: Any, frozen_bag: Path) -> Dict[str, Any]:
    """Validate the request and return the blackout record (before any launch)."""
    manifest_path = getattr(args, "blackout_manifest", None)
    arm = getattr(args, "blackout_arm", None)
    k = getattr(args, "blackout_k_seconds", None)
    if manifest_path is None or arm not in ARMS or k is None:
        raise BlackoutError("BLACKOUT-1 requires --blackout-manifest, --blackout-arm and --blackout-k-seconds")
    k = float(k)
    if not math.isfinite(k) or k < 0.0 or k not in DURATIONS:
        raise BlackoutError("blackout k must be one of {}".format(DURATIONS))
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = load_manifest(manifest_path)
    source = manifest["source_bag"]
    if Path(str(source["path"])).resolve(strict=False) != frozen_bag.resolve(strict=False):
        raise BlackoutError("manifest source bag path differs from the frozen bag")
    if abs(float(manifest["mask"]["seconds"]) - k) > 1e-9:
        raise BlackoutError("manifest mask length differs from --blackout-k-seconds")
    if bool(manifest["mask"]["active"]) != (k > 0.0):
        raise BlackoutError("manifest mask activity inconsistent with k")
    masked_path = Path(str(manifest["masked_bag"]["path"])).resolve(strict=True)
    if not masked_path.is_file():
        raise BlackoutError("masked bag missing: {}".format(masked_path))
    if masked_path.stat().st_size != int(manifest["masked_bag"]["size_bytes"]):
        raise BlackoutError("masked bag size differs from the manifest")
    return {
        "campaign_id": CAMPAIGN_ID,
        "arm": arm,
        "k_seconds": k,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "manifest": manifest,
        "masked_bag_path": str(masked_path),
        "source_bag_path": str(frozen_bag),
        "mask_start_s": float(manifest["mask"]["start_header_stamp_s"]),
        "mask_end_s": float(manifest["mask"]["end_header_stamp_s"]),
        "mask_active": bool(manifest["mask"]["active"]),
        "dropped_per_topic": dict(manifest["dropped_per_topic"]),
        "last_pre_mask_header_stamp_ns": manifest.get("last_pre_mask_header_stamp_ns"),
        "first_post_mask_header_stamp_ns": manifest.get("first_post_mask_header_stamp_ns"),
        "masking_layer": "replay_bag_rewrite_camera_frames_dropped_imu_untouched",
    }


def verify_masked_identity(record: Mapping[str, Any], live_identity: Mapping[str, Any]) -> Dict[str, Any]:
    """The census hashed the masked bag it read; it must equal the manifest's masked identity."""
    expected = record["manifest"]["masked_bag"]
    checks = {
        "path": Path(str(live_identity.get("path"))).resolve(strict=False) == Path(str(expected["path"])).resolve(strict=False),
        "size_bytes": int(live_identity.get("size_bytes", -1)) == int(expected["size_bytes"]),
        "sha256": str(live_identity.get("sha256")) == str(expected["sha256"]),
    }
    if not all(checks.values()):
        raise BlackoutError("masked bag identity differs from the manifest: {}".format(checks))
    return {"masked_bag_matches_manifest": True, "checks": checks, "masked_bag_identity": dict(live_identity)}


def verify_source_identity(record: Mapping[str, Any], expected_bag: Mapping[str, Any]) -> Dict[str, Any]:
    """The manifest's source identity must equal the matrix-bound frozen bag identity."""
    source = record["manifest"]["source_bag"]
    checks = {
        "path": Path(str(source["path"])).resolve(strict=False) == Path(str(expected_bag.get("path"))).resolve(strict=False),
        "size_bytes": int(source["size_bytes"]) == int(expected_bag.get("size_bytes", -1)),
        "sha256": str(source["sha256"]) == str(expected_bag.get("sha256")),
    }
    if not all(checks.values()):
        raise BlackoutError("manifest source bag differs from the frozen matrix bag: {}".format(checks))
    return {"source_bag_matches_matrix": True, "checks": checks}


def _num(value: str) -> Any:
    try:
        if re.fullmatch(r"[-+]?[0-9]+", value):
            return int(value)
        return float(value)
    except ValueError:
        return value


def describe_recovery(console_text: str, enabled: bool) -> Dict[str, Any]:
    """Descriptive, never-gating record of every recovery line (D6)."""
    clean = [ANSI_RE.sub("", line) for line in console_text.splitlines()]
    lines = [line.strip() for line in clean if RECOVERY_PREFIX in line]
    events: List[Dict[str, Any]] = []
    for line in lines:
        payload = line.split(RECOVERY_PREFIX, 1)[1].strip()
        fields = {k: _num(v) for k, v in KV_RE.findall(payload)}
        events.append({"event": fields.get("event", "unparsed"), "fields": fields, "line": line})
    names = [e["event"] for e in events]
    counts = {name: names.count(name) for name in sorted(set(names))}
    attempts = [e["fields"] for e in events if e["event"] == "attempt"]
    histogram = {reason: 0 for reason in ATTEMPT_REASONS}
    for a in attempts:
        histogram[str(a.get("reason", "unknown"))] = histogram.get(str(a.get("reason", "unknown")), 0) + 1
    histogram = {k: v for k, v in histogram.items() if v > 0}
    commits = [e["fields"] for e in events if e["event"] == "relocalization_commit"]
    covariances = [e["fields"] for e in events if e["event"] == "first_resumed_covariance"]
    triggers = [e["fields"] for e in events if e["event"] == "trigger"]
    failures = [e["fields"] for e in events if e["event"] == "recovery_failed"]
    consensus_rejects = [e["fields"] for e in events if e["event"] == "consensus_reject"]
    consensus_passes = [e["fields"] for e in events if e["event"] == "consensus_pass"]
    accepted = [e["fields"] for e in events if e["event"] == "accepted_pose"]
    summary = next((e["fields"] for e in events if e["event"] == "summary"), None)
    contract = next((e["fields"] for e in events if e["event"] == "contract_validated"), None)
    contract_ok = (
        (counts.get("contract_validated", 0) == 1 and counts.get("summary", 0) == 1)
        if enabled
        else len(lines) == 0
    )
    state_preserved = all(int(a.get("state_unchanged", 0)) == 1 for a in attempts) and all(
        int(e["fields"].get("state_unchanged", 1)) == 1 for e in events if e["event"] in ("degraded_frame", "recovery_failed")
    )
    return {
        "descriptive_only_never_gates_completion": True,
        "enabled_expected": enabled,
        "contract_lines_ok": contract_ok,
        "event_line_count": len(lines),
        "event_counts": counts,
        "contract_validated": contract,
        "summary": summary,
        "activations": int(summary["activations"]) if summary else 0,
        "commits": int(summary["commits"]) if summary else 0,
        "failures": int(summary["failures"]) if summary else 0,
        "triggers": triggers,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "attempt_reason_histogram": histogram,
        "attempt_supplied_counts": [int(a.get("supplied", 0)) for a in attempts],
        "attempt_valid_counts": [int(a.get("valid", 0)) for a in attempts],
        "attempt_inlier_counts": [int(a.get("inliers", 0)) for a in attempts],
        "accepted_poses": accepted,
        "consensus_passes": consensus_passes,
        "consensus_rejects": consensus_rejects,
        "commit_events": commits,
        "first_resumed_covariances": covariances,
        "recovery_failed_events": failures,
        "degraded_frame_count": counts.get("degraded_frame", 0),
        "warmup_complete_count": counts.get("warmup_complete", 0),
        "state_preserved_on_every_attempt": state_preserved,
        "event_lines": lines,
    }


def blackout_completion(
    completion: Dict[str, Any],
    record: Mapping[str, Any],
    recovery: Mapping[str, Any],
    state_timestamps: Sequence[float],
) -> Dict[str, Any]:
    """Apply the D7 recovery-supported seam to the frozen passage result (in place, recorded)."""
    result = dict(completion)
    result["blackout_seam"] = {
        "rule": (
            "state gap starting within 0.10 s of the masked input gap start and ending at a "
            "relocalization_commit's first_resumed_covariance state_output_timestamp (+-1e-5 s) is "
            "RECOVERY-SUPPORTED (D7); tail rule unchanged"
        ),
        "applied": False,
        "supported_gaps": [],
    }
    mask_start = float(record["mask_start_s"])
    mask_end = float(record["mask_end_s"])
    active = bool(record["mask_active"])
    commit_outputs = [
        float(c["state_output_timestamp"]) for c in recovery.get("first_resumed_covariances", []) if "state_output_timestamp" in c
    ]
    unsupported = list(result.get("unsupported_state_gaps") or [])
    remaining: List[Mapping[str, Any]] = []
    supported: List[Dict[str, Any]] = []
    for gap in unsupported:
        start = float(gap["start_timestamp_s"])
        end = float(gap["end_timestamp_s"])
        match = None
        if active and abs(start - mask_start) <= GAP_SEAM_TOLERANCE_S:
            for output in commit_outputs:
                if abs(end - output) <= COMMIT_MATCH_TOLERANCE_S:
                    match = output
                    break
        if match is None:
            remaining.append(gap)
        else:
            supported.append({"state_gap": dict(gap), "commit_state_output_timestamp": match})
    if supported:
        result["unsupported_state_gaps"] = remaining
        result["maximum_state_gap_pass"] = not remaining
        result["supported_input_gap_count"] = int(result.get("supported_input_gap_count", 0)) + len(supported)
        result["recovery_supported_state_gap_count"] = len(supported)
        result["recovery_supported_state_gaps"] = supported
        result["pass"] = bool(result["tail_gap_pass"]) and not remaining
        result["blackout_seam"]["applied"] = True
        result["blackout_seam"]["supported_gaps"] = supported
    else:
        result.setdefault("recovery_supported_state_gap_count", 0)
        result.setdefault("recovery_supported_state_gaps", [])
    # gap-closure and timing metrics (descriptive)
    horizon_end = mask_end + CLOSURE_HORIZON_S
    intersecting = [
        g for g in (result.get("unsupported_state_gaps") or [])
        if not (float(g["end_timestamp_s"]) < mask_start or float(g["start_timestamp_s"]) > horizon_end)
    ]
    first_after = None
    for t in state_timestamps:
        if float(t) >= mask_end:
            first_after = float(t)
            break
    result["blackout_metrics"] = {
        "mask_start_s": mask_start,
        "mask_end_s": mask_end,
        "mask_active": active,
        "gap_closure_success": (not intersecting) and bool(result["tail_gap_pass"]),
        "unsupported_gaps_intersecting_mask_horizon": intersecting,
        "first_state_after_mask_end_s": first_after,
        "time_to_resume_s": (None if first_after is None else first_after - mask_end),
        "time_to_recover_s": (
            (min(commit_outputs) - mask_end) if commit_outputs and active else None
        ),
        "commit_state_output_timestamps_s": commit_outputs,
    }
    return result


def read_timestamps(path: Path) -> List[float]:
    values: List[float] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        values.append(float(line.split()[0].rstrip(",")))
    return values


def timing_descriptive(state_times: Sequence[float], timing_times: Sequence[float]) -> Dict[str, Any]:
    """Descriptive replacement for the rotation-only C2 timing contract (D6)."""
    return {
        "timing_contract": "BLACKOUT_DESCRIPTIVE_ONLY",
        "consistent": None,
        "state_row_count": len(state_times),
        "timing_row_count": len(timing_times),
        "untimed_state_row_count": len(state_times) - len(timing_times),
        "note": "frozen KAIST rotation-only C2 timing contract not applied in BLACKOUT-1 (D6)",
    }
