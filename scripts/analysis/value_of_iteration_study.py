#!/usr/bin/env python3
"""Leakage-controlled value-of-iteration study for SchurVIO-Lite.

This script is deliberately offline.  It reads the immutable phase4/phase5
repeat-1 artifacts, builds frame-level Pass-2 benefit labels, and evaluates a
small predeclared policy family with nested grouped validation.  It never
modifies the artifact tree.
"""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeClassifier


SEED_DEFAULT = 1729
OOD_SEQUENCE = "MH_04"
TIMESTAMP_TOLERANCE_SECONDS = 1.0e-4
DEADLINE_MS = 50.0
STABLE_TEN: Tuple[str, ...] = (
    "MH_01", "MH_02", "MH_03", "MH_05",
    "V1_01", "V1_02", "V1_03", "V2_01", "V2_02", "V2_03",
)

# Frozen before looking at Pass-2 outcomes.  Every item is available after
# Pass 1 and before deciding whether to execute Pass 2.
CAUSAL_FEATURES: Tuple[str, ...] = (
    "accepted_tracks_pass1",
    "reduced_rows_pass1",
    "pass1_global_nis",
    "pass1_nis_per_row",
    "pass1_max_gate_ratio",
    "pass1_cpix",
    "pass1_cpost",
    "pass1_dx_norm",
)

# Shadow mode adds these scalar summaries before Pass 2 starts.  They are
# candidates, not a globally selected feature set: model feature selection is
# repeated using each training partition only and is capped at eight fields.
SHADOW_ONLY_CAUSAL_CANDIDATES: Tuple[str, ...] = (
    "pass1_reduced_whitened_residual_rms",
    "pass1_prior_whitened_correction_norm",
    "pass1_orientation_correction_norm",
    "pass1_position_correction_norm",
    "pass1_velocity_correction_norm",
    "pass1_gyro_bias_correction_norm",
    "pass1_accelerometer_bias_correction_norm",
    "pass1_clone_aggregate_correction_norm",
    "pass1_minimum_schur_singular_ratio",
    "pass1_median_track_observations",
)
SHADOW_CAUSAL_CANDIDATES: Tuple[str, ...] = (
    CAUSAL_FEATURES + SHADOW_ONLY_CAUSAL_CANDIDATES
)
ALL_CAUSAL_FEATURES = frozenset(SHADOW_CAUSAL_CANDIDATES)
MODEL_FEATURE_LIMIT = 8

SHADOW_REQUIRED_PROVENANCE_FIELDS: Tuple[str, ...] = (
    "one_total_source",
    "two_total_source",
    "source_commit",
    "config_sha256",
)

POLICY_ORDER = (
    "P0_NEVER",
    "P1_ALWAYS",
    "P2_RANDOM_RATE_MATCHED",
    "P3_SINGLE_THRESHOLD",
    "P4_TWO_RULE",
    "P5_L1_LOGISTIC",
    "P6_DEPTH3_TREE",
    "ORACLE_STRICT",
    "ORACLE_SELECTED",
)

DEFAULT_SEARCH_GRID: Mapping[str, Sequence[Any]] = {
    "quantiles": (0.15, 0.30, 0.50, 0.70, 0.85),
    "logistic_c": (0.05, 0.20, 1.0),
    "probability_thresholds": (0.35, 0.50, 0.65, 0.80),
    "tree_depths": (1, 2, 3),
    "p4_top_rules": (5,),
}

# Tests exercise the same nested fitting path with a smaller deterministic grid.
TEST_SEARCH_GRID: Mapping[str, Sequence[Any]] = {
    "quantiles": (0.30, 0.70),
    "logistic_c": (0.20,),
    "probability_thresholds": (0.50,),
    "tree_depths": (2,),
    "p4_top_rules": (3,),
}

TIMING_COLUMNS = (
    "timestamp",
    "tracking",
    "propagation",
    "msckf_update",
    "slam_update",
    "slam_delayed",
    "retri_marg",
    "total",
)

LABEL_COLUMNS = (
    "STRICT_BENEFIT",
    "ORACLE_SELECTED",
    "NONHARMFUL",
    "INVALID",
    "HARMFUL",
    "PIXEL_ONLY_HELP",
    "POSTERIOR_ONLY_HELP",
    "EQUAL_WITHIN_TOLERANCE",
)


def validate_causal_features(
    features: Sequence[str],
    max_features: Optional[int] = MODEL_FEATURE_LIMIT,
) -> None:
    """Fail closed if a policy attempts to use a non-predeclared field."""
    if max_features is not None and len(features) > max_features:
        raise ValueError("policy feature count exceeds the frozen limit of eight")
    unknown = sorted(set(features) - ALL_CAUSAL_FEATURES)
    if unknown:
        raise ValueError("non-causal or non-predeclared policy fields: %s" % unknown)
    forbidden_tokens = (
        "pass2",
        "selected_pass",
        "selection_reason",
        "terminal_status",
        "gain",
        "label",
        "future",
        "ground_truth",
        "sequence",
        "segment",
        "two_total",
    )
    for field in features:
        if any(token in field.lower() for token in forbidden_tokens):
            raise ValueError("leakage-prone policy field: %s" % field)


def _numeric(frame: pd.DataFrame, columns: Iterable[str]) -> None:
    for column in columns:
        if column in frame:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")


def _bool_series(values: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False).astype(bool)
    return values.astype(str).str.strip().str.lower().isin(("1", "true", "yes"))


def compute_labels(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply the frozen tolerance, labels, and normalized gain definitions."""
    required = (
        "pass2_valid",
        "selected_pass",
        "pass1_cpix",
        "pass2_cpix",
        "pass1_cpost",
        "pass2_cpost",
    )
    missing = sorted(set(required) - set(frame.columns))
    if missing:
        raise ValueError("missing label inputs: %s" % missing)

    out = frame.copy()
    _numeric(out, required[1:])
    p1_pix = out["pass1_cpix"]
    p2_pix = out["pass2_cpix"]
    p1_post = out["pass1_cpost"]
    p2_post = out["pass2_cpost"]
    out["tau_pix"] = 1.0e-9 * np.maximum(1.0, np.abs(p1_pix))
    out["tau_post"] = 1.0e-9 * np.maximum(1.0, np.abs(p1_post))

    costs_finite = np.isfinite(p1_pix) & np.isfinite(p2_pix)
    costs_finite &= np.isfinite(p1_post) & np.isfinite(p2_post)
    valid = _bool_series(out["pass2_valid"]) & costs_finite
    pix_help = valid & (p2_pix < p1_pix - out["tau_pix"])
    post_help = valid & (p2_post < p1_post - out["tau_post"])
    pix_harm = valid & (p2_pix > p1_pix + out["tau_pix"])
    post_harm = valid & (p2_post > p1_post + out["tau_post"])
    pix_equal = valid & ((p2_pix - p1_pix).abs() <= out["tau_pix"])
    post_equal = valid & ((p2_post - p1_post).abs() <= out["tau_post"])

    out["pass2_production_valid"] = valid.astype(np.int8)
    out["STRICT_BENEFIT"] = (pix_help & post_help).astype(np.int8)
    # Historical closed-loop artifacts exposed only selected_pass.  A shadow
    # run deliberately commits live Pass 1, so its oracle decision must come
    # from the separate oracle_selected_pass field and never selected_pass.
    selector_field = "oracle_selected_pass" if "oracle_selected_pass" in out else "selected_pass"
    if selector_field != "selected_pass":
        _numeric(out, (selector_field,))
    out["ORACLE_SELECTED"] = (valid & (out[selector_field] == 2)).astype(np.int8)
    out["NONHARMFUL"] = (valid & ~pix_harm & ~post_harm).astype(np.int8)
    out["INVALID"] = (~valid).astype(np.int8)
    out["HARMFUL"] = (valid & (pix_harm | post_harm)).astype(np.int8)
    out["PIXEL_ONLY_HELP"] = (pix_help & ~post_help).astype(np.int8)
    out["POSTERIOR_ONLY_HELP"] = (post_help & ~pix_help).astype(np.int8)
    out["EQUAL_WITHIN_TOLERANCE"] = (pix_equal & post_equal).astype(np.int8)

    pix_denominator = np.maximum(np.abs(p1_pix), out["tau_pix"])
    post_denominator = np.maximum(np.abs(p1_post), out["tau_post"])
    out["rel_gain_pix"] = np.where(valid, (p1_pix - p2_pix) / pix_denominator, np.nan)
    out["rel_gain_post"] = np.where(
        valid, (p1_post - p2_post) / post_denominator, np.nan
    )
    out["joint_gain"] = np.minimum(
        out["rel_gain_pix"], out["rel_gain_post"]
    )
    # Readable aliases retained for downstream notebooks; the rel_gain_* names
    # above are the canonical machine schema requested by the study protocol.
    out["relative_pixel_gain"] = out["rel_gain_pix"]
    out["relative_posterior_gain"] = out["rel_gain_post"]
    out["positive_joint_gain"] = np.where(
        np.isfinite(out["joint_gain"]), np.maximum(out["joint_gain"], 0.0), np.nan
    )
    return out


def _canonical_sequence(directory_name: str) -> str:
    match = re.match(r"^(MH|V1|V2)_\d{2}", directory_name)
    if not match:
        raise ValueError("cannot infer EuRoC sequence from %s" % directory_name)
    return match.group(0)


def discover_repeat1_runs(artifact_root: Path) -> List[Dict[str, Path]]:
    """Discover only phase4/phase5 full runs (the frozen repeat-1 population)."""
    runs: List[Dict[str, Path]] = []
    seen: set = set()
    for phase in ("phase4", "phase5"):
        phase_root = artifact_root / phase
        if not phase_root.is_dir():
            raise FileNotFoundError("required artifact directory missing: %s" % phase_root)
        for two_diag in sorted(phase_root.glob("*/full_two/diagnostics.csv")):
            sequence_dir = two_diag.parent.parent
            sequence = _canonical_sequence(sequence_dir.name)
            if sequence in seen:
                raise ValueError("duplicate repeat-1 full_two run for %s" % sequence)
            seen.add(sequence)
            one_dir = sequence_dir / "full_one"
            two_dir = sequence_dir / "full_two"
            paths = {
                "sequence": Path(sequence),
                "phase": Path(phase),
                "sequence_dir": sequence_dir,
                "one_diagnostics": one_dir / "diagnostics.csv",
                "one_timing": one_dir / "timing.csv",
                "two_diagnostics": two_dir / "diagnostics.csv",
                "two_timing": two_dir / "timing.csv",
                "two_stdout": two_dir / "stdout.log",
            }
            for key in (
                "one_diagnostics", "one_timing", "two_diagnostics", "two_timing", "two_stdout"
            ):
                if not paths[key].is_file():
                    raise FileNotFoundError("paired artifact missing: %s" % paths[key])
            runs.append(paths)
    expected = {
        "MH_01", "MH_02", "MH_03", "MH_04", "MH_05",
        "V1_01", "V1_02", "V1_03", "V2_01", "V2_02", "V2_03",
    }
    if seen != expected:
        raise ValueError("repeat-1 sequence set mismatch: got %s" % sorted(seen))
    return sorted(runs, key=lambda item: str(item["sequence"]))


def _read_timing(path: Path) -> pd.DataFrame:
    timing = pd.read_csv(
        path, comment="#", header=None, names=TIMING_COLUMNS, skip_blank_lines=True
    )
    _numeric(timing, TIMING_COLUMNS)
    timing = timing.dropna(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    if timing["timestamp"].duplicated().any():
        raise ValueError("duplicate timing timestamps in %s" % path)
    return timing


def _read_pass1_log_allowlist(path: Path) -> pd.DataFrame:
    """Read only timestamp/rows/NIS from accepted Pass-1 iteration log lines.

    In particular, accepted_set_hash and completed_passes are intentionally
    neither captured by the regular expression nor retained in the dataset.
    """
    allowed = re.compile(r"(?:^|\s)(timestamp|rows|global_proposal_nis)=([^\s]+)")
    records: List[Dict[str, float]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if "[MSCKF-ITER]" not in line or "pass=1" not in line or "status=accepted" not in line:
                continue
            tokens = {match.group(1): match.group(2) for match in allowed.finditer(line)}
            if set(tokens) != {"timestamp", "rows", "global_proposal_nis"}:
                raise ValueError("malformed accepted Pass-1 log line in %s" % path)
            records.append({
                "timestamp": float(tokens["timestamp"]),
                "reduced_rows_pass1": float(tokens["rows"]),
                "logged_pass1_global_nis": float(tokens["global_proposal_nis"]),
            })
    frame = pd.DataFrame(records)
    if frame.empty:
        raise ValueError("no accepted Pass-1 allowlist records in %s" % path)
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    if frame["timestamp"].duplicated().any():
        raise ValueError("duplicate accepted Pass-1 timestamps in %s" % path)
    return frame


def _nearest_attach(
    left: pd.DataFrame,
    right: pd.DataFrame,
    columns: Sequence[str],
    prefix: str,
    tolerance: float = TIMESTAMP_TOLERANCE_SECONDS,
) -> pd.DataFrame:
    """Attach nearest timestamp fields without silently accepting loose matches."""
    result = left.copy()
    left_time = pd.to_numeric(result["timestamp"], errors="coerce").to_numpy(float)
    right_time = pd.to_numeric(right["timestamp"], errors="coerce").to_numpy(float)
    if not len(right_time):
        raise ValueError("cannot align against an empty frame")
    positions = np.searchsorted(right_time, left_time, side="left")
    chosen = np.empty(len(left_time), dtype=int)
    deltas = np.empty(len(left_time), dtype=float)
    for i, (timestamp, position) in enumerate(zip(left_time, positions)):
        candidates = [j for j in (position - 1, position) if 0 <= j < len(right_time)]
        best = min(candidates, key=lambda j: (abs(right_time[j] - timestamp), j))
        chosen[i] = best
        deltas[i] = abs(right_time[best] - timestamp)
    if np.any(~np.isfinite(deltas)) or np.any(deltas > tolerance):
        worst = float(np.nanmax(deltas))
        raise ValueError("timestamp alignment exceeds %.3g s (worst %.9g s)" % (tolerance, worst))
    if len(set(chosen.tolist())) != len(chosen):
        raise ValueError("timestamp alignment is not one-to-one")
    for column in columns:
        values = right.iloc[chosen][column].reset_index(drop=True)
        result[prefix + column] = values.to_numpy()
    result[prefix + "timestamp_delta_seconds"] = deltas
    return result


def _load_sequence(run: Mapping[str, Path], artifact_root: Path) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    two = pd.read_csv(run["two_diagnostics"])
    one = pd.read_csv(run["one_diagnostics"])
    if "timestamp" not in two or "timestamp" not in one:
        raise ValueError("diagnostics missing timestamp for %s" % run["sequence"])
    two["pass1_valid"] = _bool_series(two["pass1_valid"])
    two["pass2_valid"] = _bool_series(two["pass2_valid"])
    if "pass1_valid" in one:
        one["pass1_valid"] = _bool_series(one["pass1_valid"])
    if "pass2_valid" in one:
        one["pass2_valid"] = _bool_series(one["pass2_valid"])
    two = two.sort_values("timestamp").reset_index(drop=True)
    one = one.sort_values("timestamp").reset_index(drop=True)
    # completed_passes is post-decision and explicitly excluded from ingestion.
    two = two.drop(columns=["completed_passes"], errors="ignore")

    two_timing = _read_timing(run["two_timing"])
    one_timing = _read_timing(run["one_timing"])
    timing_fields = tuple(column for column in TIMING_COLUMNS if column != "timestamp")
    two = _nearest_attach(two, two_timing, timing_fields, "two_")
    two = _nearest_attach(two, one_timing, timing_fields, "one_")

    one_diag_fields = tuple(
        column for column in (
            "accepted_tracks_pass1", "pass1_global_nis", "pass1_max_feature_nis",
            "pass1_gate_threshold", "pass1_cpix", "pass1_cpost", "pass1_dx_norm",
            "pass1_processing_ms",
        ) if column in one.columns
    )
    two = _nearest_attach(two, one, one_diag_fields, "one_diag_")

    two["sequence"] = str(run["sequence"])
    two["source_phase"] = str(run["phase"])
    two["source_sequence_dir"] = str(run["sequence_dir"].relative_to(artifact_root))
    _numeric(two, (
        "accepted_tracks_pass1", "pass1_global_nis", "pass1_max_feature_nis",
        "pass1_gate_threshold", "pass1_cpix", "pass1_cpost", "pass1_dx_norm",
        "pass1_processing_ms", "attempted_passes", "selected_pass",
    ))
    two["pass1_max_gate_ratio"] = two["pass1_max_feature_nis"] / two[
        "pass1_gate_threshold"
    ].replace(0, np.nan)

    eligible = two["pass1_valid"].fillna(False) & (two["attempted_passes"] >= 2)
    two = two.loc[eligible].sort_values("timestamp").reset_index(drop=True)
    if two.empty:
        raise ValueError("no Pass-2 opportunity rows for %s" % run["sequence"])
    pass1_log = _read_pass1_log_allowlist(run["two_stdout"])
    two = _nearest_attach(
        two, pass1_log,
        ("reduced_rows_pass1", "logged_pass1_global_nis"), "log_",
    )
    two["reduced_rows_pass1"] = two.pop("log_reduced_rows_pass1")
    logged_nis = two.pop("log_logged_pass1_global_nis")
    nis_error = np.abs(logged_nis - two["pass1_global_nis"])
    nis_bound = 1.0e-10 * np.maximum(1.0, np.abs(two["pass1_global_nis"]))
    if np.any(nis_error > nis_bound):
        raise ValueError("diagnostics/log Pass-1 NIS disagreement for %s" % run["sequence"])
    two["pass1_nis_per_row"] = two["pass1_global_nis"] / two[
        "reduced_rows_pass1"
    ].replace(0, np.nan)
    progress = (np.arange(len(two), dtype=float) + 0.5) / len(two)
    two["segment"] = np.where(progress < 1.0 / 3.0, "early", np.where(
        progress < 2.0 / 3.0, "middle", "late"
    ))
    two["population"] = np.where(two["sequence"] == OOD_SEQUENCE, "ood", "primary")
    two = compute_labels(two)
    two["dataset_row_id"] = ["%s:%06d" % (run["sequence"], i) for i in range(len(two))]

    manifest: List[Dict[str, Any]] = []
    for role in (
        "one_diagnostics", "one_timing", "two_diagnostics", "two_timing", "two_stdout"
    ):
        path = run[role]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest.append({
            "sequence": str(run["sequence"]),
            "source_phase": str(run["phase"]),
            "role": role,
            "relative_path": str(path.relative_to(artifact_root)),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    return two, manifest


def load_artifact_dataset(artifact_root: Path) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    validate_causal_features(CAUSAL_FEATURES)
    frames: List[pd.DataFrame] = []
    manifests: List[Dict[str, Any]] = []
    input_diagnostic_fields: set = set()
    for run in discover_repeat1_runs(artifact_root):
        raw_header = pd.read_csv(run["two_diagnostics"], nrows=0)
        input_diagnostic_fields.update(raw_header.columns.tolist())
        frame, manifest = _load_sequence(run, artifact_root)
        frames.append(frame)
        manifests.extend(manifest)
    dataset = pd.concat(frames, ignore_index=True, sort=False)
    dataset = dataset.sort_values(["sequence", "timestamp"]).reset_index(drop=True)
    return dataset, pd.DataFrame(manifests), sorted(input_diagnostic_fields)


def _normalize_shadow_causal_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the logger's causal names and honor explicit availability."""
    out = frame.copy()
    aliases = {
        "pass1_compressed_rows": "reduced_rows_pass1",
        "pass1_compressed_nis_per_row": "pass1_nis_per_row",
    }
    for source, target in aliases.items():
        if target not in out and source in out:
            out[target] = out[source]
    if "pass1_max_gate_ratio" not in out and {
        "pass1_max_feature_nis", "pass1_gate_threshold"
    }.issubset(out.columns):
        numerator = pd.to_numeric(out["pass1_max_feature_nis"], errors="coerce")
        denominator = pd.to_numeric(out["pass1_gate_threshold"], errors="coerce")
        out["pass1_max_gate_ratio"] = numerator / denominator.replace(0, np.nan)
    if "pass1_global_nis" in out and "reduced_rows_pass1" in out:
        if "pass1_nis_per_row" not in out:
            numerator = pd.to_numeric(out["pass1_global_nis"], errors="coerce")
            denominator = pd.to_numeric(out["reduced_rows_pass1"], errors="coerce")
            out["pass1_nis_per_row"] = numerator / denominator.replace(0, np.nan)

    availability = {
        "pass1_nis_per_row": "pass1_compressed_nis_per_row_available",
        "pass1_reduced_whitened_residual_rms": (
            "pass1_reduced_whitened_residual_rms_available"
        ),
        "pass1_prior_whitened_correction_norm": (
            "pass1_prior_whitened_correction_norm_available"
        ),
        "pass1_orientation_correction_norm": "pass1_imu_block_norms_available",
        "pass1_position_correction_norm": "pass1_imu_block_norms_available",
        "pass1_velocity_correction_norm": "pass1_imu_block_norms_available",
        "pass1_gyro_bias_correction_norm": "pass1_imu_block_norms_available",
        "pass1_accelerometer_bias_correction_norm": "pass1_imu_block_norms_available",
        "pass1_clone_aggregate_correction_norm": (
            "pass1_clone_aggregate_correction_norm_available"
        ),
        "pass1_minimum_schur_singular_ratio": (
            "pass1_minimum_schur_singular_ratio_available"
        ),
        "pass1_median_track_observations": (
            "pass1_median_track_observations_available"
        ),
    }
    for feature, flag in availability.items():
        if feature in out and flag in out:
            out.loc[~_bool_series(out[flag]), feature] = np.nan
    return out


def _read_provenance_token(path: Path, description: str) -> str:
    if not path.is_file():
        raise ValueError("missing shadow %s: %s" % (description, path))
    tokens = path.read_text(encoding="utf-8", errors="replace").strip().split()
    if not tokens:
        raise ValueError("blank shadow %s: %s" % (description, path))
    return tokens[0]


def _attach_shadow_provenance(frame: pd.DataFrame, diagnostics_path: Path) -> pd.DataFrame:
    """Attach strict runner provenance without requiring duplicated CSV text."""
    out = frame.copy()
    run_dir = diagnostics_path.parent
    if "source_commit" not in out:
        out["source_commit"] = _read_provenance_token(
            run_dir / "source_commit.txt", "source commit"
        )
    if "config_sha256" not in out:
        out["config_sha256"] = _read_provenance_token(
            run_dir / "config_sha256.txt", "configuration hash"
        )
    if "one_total_source" not in out:
        if "shadow_skip_total" not in out:
            raise ValueError(
                "shadow diagnostics need one_total_source or shadow_skip_total: %s"
                % diagnostics_path
            )
        out["one_total_source"] = "same_callback_shadow_total_minus_pass2_processing"
    if "two_total_source" not in out:
        if "shadow_trigger_total" not in out:
            raise ValueError(
                "shadow diagnostics need two_total_source or shadow_trigger_total: %s"
                % diagnostics_path
            )
        out["two_total_source"] = "same_callback_observed_shadow_total"
    return out


def load_shadow_dataset(
    shadow_artifact_root: Path,
    require_stable_ten: bool = True,
) -> pd.DataFrame:
    """Load an explicit shadow-only root without conflating live/oracle choice.

    This is the stable Phase-2 ingestion seam.  A producer must export the
    extended diagnostics CSV contract containing ``shadow_only`` and
    ``oracle_selected_pass``.  The live ``selected_pass`` is asserted to be 1;
    ORACLE_SELECTED is then derived by :func:`compute_labels` from the separate
    oracle field.  ``selected_pass=0`` is legal outside the Pass-2 opportunity
    population, but no shadow row may ever select live Pass 2.
    """
    root = shadow_artifact_root.expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError("shadow artifact root does not exist: %s" % root)
    frames: List[pd.DataFrame] = []
    manifest: List[Dict[str, Any]] = []
    sequence_sources: Dict[str, Path] = {}
    for path in sorted(root.rglob("diagnostics.csv")):
        header = pd.read_csv(path, nrows=0).columns
        if "shadow_only" not in header:
            continue
        required = {
            "timestamp", "shadow_only", "requested_passes", "attempted_passes",
            "pass1_valid", "pass2_valid", "selected_pass", "oracle_selected_pass",
            "pass1_cpix", "pass2_cpix", "pass1_cpost", "pass2_cpost",
            "one_total", "two_total",
        }
        missing = sorted(required - set(header))
        if missing:
            raise ValueError("shadow diagnostics %s missing fields %s" % (path, missing))
        frame = _attach_shadow_provenance(
            _normalize_shadow_causal_columns(pd.read_csv(path)), path
        )
        missing_causal = sorted(set(SHADOW_CAUSAL_CANDIDATES) - set(frame.columns))
        missing_provenance = sorted(
            set(SHADOW_REQUIRED_PROVENANCE_FIELDS) - set(frame.columns)
        )
        if missing_causal or missing_provenance:
            raise ValueError(
                "shadow diagnostics %s missing causal=%s provenance=%s"
                % (path, missing_causal, missing_provenance)
            )
        if not _bool_series(frame["shadow_only"]).all():
            raise ValueError("mixed shadow_only values in shadow diagnostics: %s" % path)
        if frame.empty:
            continue
        _numeric(frame, (
            "timestamp", "requested_passes", "attempted_passes", "selected_pass",
            "oracle_selected_pass", "pass1_cpix", "pass2_cpix", "pass1_cpost",
            "pass2_cpost", "one_total", "two_total",
        ) + SHADOW_CAUSAL_CANDIDATES)
        if frame["timestamp"].isna().any():
            raise ValueError("shadow diagnostics contain nonnumeric timestamps: %s" % path)
        if frame["timestamp"].duplicated().any():
            raise ValueError("duplicate shadow timestamps within %s" % path)
        if (frame["selected_pass"] == 2).any():
            raise ValueError("shadow-only live state selected Pass 2: %s" % path)
        if not frame["selected_pass"].isin((0, 1)).all():
            raise ValueError("shadow selected_pass must be 0 or 1: %s" % path)
        for field in SHADOW_REQUIRED_PROVENANCE_FIELDS:
            values = frame[field].astype(str).str.strip()
            if frame[field].isna().any() or (values == "").any():
                raise ValueError("blank shadow provenance field %s in %s" % (field, path))
            if values.nunique() != 1:
                raise ValueError("mixed shadow provenance field %s in %s" % (field, path))
        if not re.fullmatch(r"[0-9a-fA-F]{40}", str(frame["source_commit"].iloc[0])):
            raise ValueError("invalid shadow source commit in %s" % path)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(frame["config_sha256"].iloc[0])):
            raise ValueError("invalid shadow configuration hash in %s" % path)
        for alias, canonical in (
            ("shadow_skip_total", "one_total"),
            ("shadow_trigger_total", "two_total"),
        ):
            if alias in frame:
                alias_values = pd.to_numeric(frame[alias], errors="coerce").to_numpy(float)
                canonical_values = frame[canonical].to_numpy(float)
                if not np.allclose(alias_values, canonical_values, rtol=0.0, atol=1.0e-12):
                    raise ValueError("shadow timing alias %s disagrees in %s" % (alias, path))
        if (~np.isfinite(frame["one_total"]) | ~np.isfinite(frame["two_total"])).any():
            raise ValueError("shadow timing columns must be finite: %s" % path)
        if (frame[["one_total", "two_total"]] < 0.0).any().any():
            raise ValueError("shadow timing columns must be nonnegative: %s" % path)
        for field in SHADOW_CAUSAL_CANDIDATES:
            if not np.isfinite(frame.loc[_bool_series(frame["pass1_valid"]), field]).any():
                raise ValueError("shadow causal field %s is wholly unavailable in %s" % (field, path))
        frame["pass1_valid"] = _bool_series(frame["pass1_valid"])
        frame["pass2_valid"] = _bool_series(frame["pass2_valid"])
        if not (frame["requested_passes"] == 2).all():
            raise ValueError("shadow diagnostics include requested_passes != 2: %s" % path)
        eligible = frame["pass1_valid"] & (frame["attempted_passes"] >= 2)
        if not (frame.loc[eligible, "selected_pass"] == 1).all():
            raise ValueError("eligible shadow rows must commit live Pass 1: %s" % path)
        frame = frame.loc[eligible].copy()
        if frame.empty:
            continue
        if "sequence" in frame and frame["sequence"].notna().any():
            distinct = frame["sequence"].dropna().astype(str).unique()
            if len(distinct) != 1:
                raise ValueError("mixed sequence values in %s" % path)
            sequence_text = str(distinct[0])
            match = re.search(r"(MH|V1|V2)_\d{2}", sequence_text)
            sequence = match.group(0) if match else sequence_text
        else:
            sequence = next(
                (_canonical_sequence(part) for part in path.parts if re.match(r"^(MH|V1|V2)_\d{2}", part)),
                None,
            )
            if sequence is None:
                raise ValueError("cannot infer shadow sequence for %s" % path)
        if sequence in sequence_sources:
            raise ValueError(
                "multiple shadow diagnostics sources for %s: %s and %s"
                % (sequence, sequence_sources[sequence], path)
            )
        sequence_sources[sequence] = path
        frame["sequence"] = sequence
        frame["population"] = "primary"
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        progress = (np.arange(len(frame), dtype=float) + 0.5) / len(frame)
        frame["segment"] = np.where(
            progress < 1.0 / 3.0, "early",
            np.where(progress < 2.0 / 3.0, "middle", "late"),
        )
        frame["shadow_source"] = str(path.relative_to(root))
        frame = compute_labels(frame)
        frame["dataset_row_id"] = [
            "shadow:%s:%.9f" % (sequence, value) for value in frame["timestamp"]
        ]
        frames.append(frame)
        manifest.append({
            "sequence": sequence,
            "role": "shadow_diagnostics",
            "relative_path": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_commit": str(frame["source_commit"].iloc[0]),
            "config_sha256": str(frame["config_sha256"].iloc[0]),
            "one_total_source": str(frame["one_total_source"].iloc[0]),
            "two_total_source": str(frame["two_total_source"].iloc[0]),
        })
    if not frames:
        raise ValueError(
            "no diagnostics.csv with the shadow_only/oracle_selected_pass contract under %s" % root
        )
    result = pd.concat(frames, ignore_index=True, sort=False)
    if result["dataset_row_id"].duplicated().any():
        raise ValueError("duplicate shadow sequence/timestamp rows")
    observed = set(result["sequence"].unique())
    if require_stable_ten and observed != set(STABLE_TEN):
        raise ValueError(
            "shadow sequence set must be the stable ten (got %s)" % sorted(observed)
        )
    if OOD_SEQUENCE in observed:
        raise ValueError("MH_04 is forbidden in the shadow training population")
    result = result.sort_values(["sequence", "timestamp"]).reset_index(drop=True)
    result.attrs["input_manifest"] = manifest
    result.attrs["causal_candidates"] = list(SHADOW_CAUSAL_CANDIDATES)
    return result


def load_mode_audit_frame(artifact_root: Path) -> pd.DataFrame:
    """Align all repeat-1 diagnostic rows before the eligibility filter.

    This population is intentionally separate from the learning dataset.  It
    permits a closed-loop distribution audit (including Pass-1-validity
    disagreement) over chronological thirds without conditioning on a valid
    Pass 1 or attempted Pass 2.
    """
    frames: List[pd.DataFrame] = []
    for run in discover_repeat1_runs(artifact_root):
        two = pd.read_csv(run["two_diagnostics"])
        one = pd.read_csv(run["one_diagnostics"])
        two["pass1_valid"] = _bool_series(two["pass1_valid"])
        one["pass1_valid"] = _bool_series(one["pass1_valid"])
        _numeric(two, (
            "timestamp", "accepted_tracks_pass1", "pass1_dx_norm", "pass1_processing_ms",
        ))
        _numeric(one, (
            "timestamp", "accepted_tracks_pass1", "pass1_dx_norm", "pass1_processing_ms",
        ))
        two = two.sort_values("timestamp").reset_index(drop=True)
        one = one.sort_values("timestamp").reset_index(drop=True)
        two = _nearest_attach(
            two, one,
            ("pass1_valid", "accepted_tracks_pass1", "pass1_dx_norm", "pass1_processing_ms"),
            "one_diag_",
        )
        timing_fields = tuple(column for column in TIMING_COLUMNS if column != "timestamp")
        two = _nearest_attach(two, _read_timing(run["one_timing"]), timing_fields, "one_")
        two = _nearest_attach(two, _read_timing(run["two_timing"]), timing_fields, "two_")
        progress = (np.arange(len(two), dtype=float) + 0.5) / len(two)
        two["segment"] = np.where(
            progress < 1.0 / 3.0, "early",
            np.where(progress < 2.0 / 3.0, "middle", "late"),
        )
        sequence = str(run["sequence"])
        two["sequence"] = sequence
        two["population"] = "ood" if sequence == OOD_SEQUENCE else "primary"
        frames.append(two)
    return pd.concat(frames, ignore_index=True, sort=False)


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else float("nan")


def _quantile(values: np.ndarray, probability: float) -> float:
    finite = values[np.isfinite(values)]
    return float(np.quantile(finite, probability)) if len(finite) else float("nan")


def _metric_row(frame: pd.DataFrame, trigger: np.ndarray, score: np.ndarray) -> Dict[str, Any]:
    trigger = np.asarray(trigger, dtype=bool)
    score = np.asarray(score, dtype=float)
    truth = frame["STRICT_BENEFIT"].to_numpy(int).astype(bool)
    harmful = frame["HARMFUL"].to_numpy(int).astype(bool)
    invalid = frame["INVALID"].to_numpy(int).astype(bool)
    gains = frame["positive_joint_gain"].to_numpy(float)
    tp = int(np.sum(trigger & truth))
    fp = int(np.sum(trigger & ~truth))
    positives = int(np.sum(truth))
    triggered = int(np.sum(trigger))
    precision = _safe_div(tp, triggered)
    recall = _safe_div(tp, positives)
    if np.isfinite(precision) and np.isfinite(recall):
        f1 = 0.0 if precision + recall == 0.0 else 2.0 * precision * recall / (precision + recall)
    else:
        f1 = float("nan")
    try:
        ap = float(average_precision_score(truth.astype(int), score)) if positives else float("nan")
    except ValueError:
        ap = float("nan")
    total_gain = float(np.nansum(gains))
    captured_gain = float(np.nansum(gains[trigger]))
    row: Dict[str, Any] = {
        "frames": int(len(frame)),
        "strict_positive_events": positives,
        "captured_strict_events": tp,
        "false_trigger_events": fp,
        "triggered_events": triggered,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "average_precision": ap,
        "trigger_rate": _safe_div(triggered, len(frame)),
        "false_trigger_rate": _safe_div(fp, triggered),
        "harmful_trigger_events": int(np.sum(trigger & harmful)),
        "harmful_trigger_rate": _safe_div(np.sum(trigger & harmful), triggered),
        "invalid_trigger_events": int(np.sum(trigger & invalid)),
        "invalid_trigger_rate": _safe_div(np.sum(trigger & invalid), triggered),
        "positive_joint_gain_total": total_gain,
        "captured_positive_joint_gain": captured_gain,
        "captured_positive_joint_gain_fraction": _safe_div(captured_gain, total_gain),
    }
    timing_mask = np.isfinite(frame["one_total"].to_numpy(float)) & np.isfinite(
        frame["two_total"].to_numpy(float)
    )
    one_ms = 1000.0 * frame.loc[timing_mask, "one_total"].to_numpy(float)
    two_ms = 1000.0 * frame.loc[timing_mask, "two_total"].to_numpy(float)
    selected_ms = np.where(trigger[timing_mask], two_ms, one_ms)
    always_delta = two_ms - one_ms
    selected_delta = np.where(trigger[timing_mask], always_delta, 0.0)
    row["timing_pair_frames"] = int(len(selected_ms))
    for name, values in (("counterfactual_total", selected_ms), ("added_latency", selected_delta)):
        row[name + "_mean_ms"] = float(np.mean(values)) if len(values) else float("nan")
        for percentile in (50, 95, 99):
            row["%s_p%d_ms" % (name, percentile)] = _quantile(values, percentile / 100.0)
        row[name + "_max_ms"] = float(np.max(values)) if len(values) else float("nan")
    always_p95 = _quantile(always_delta, 0.95)
    row["always_two_added_latency_mean_ms"] = float(np.mean(always_delta)) if len(always_delta) else float("nan")
    row["always_two_added_latency_p95_ms"] = always_p95
    row["overhead_retained_fraction"] = _safe_div(np.sum(selected_delta), np.sum(always_delta))
    one_total_p95 = _quantile(one_ms, 0.95)
    fixed_two_total_p95 = _quantile(two_ms, 0.95)
    estimated_total_p95 = row["counterfactual_total_p95_ms"]
    row["one_total_p95_ms"] = one_total_p95
    row["fixed_two_total_p95_ms"] = fixed_two_total_p95
    row["estimated_p95_overhead_ms"] = estimated_total_p95 - one_total_p95
    row["fixed_two_p95_overhead_ms"] = fixed_two_total_p95 - one_total_p95
    # Gate definition: differences of total-time p95s, not the p95 of
    # per-frame differences.  The latter remains reported above as
    # added_latency_p95_ms for distributional diagnostics only.
    row["p95_overhead_fraction_of_always_two"] = _safe_div(
        row["estimated_p95_overhead_ms"], row["fixed_two_p95_overhead_ms"]
    )
    row["deadline_ms"] = DEADLINE_MS
    row["deadline_miss_events"] = int(np.sum(selected_ms > DEADLINE_MS))
    row["deadline_miss_rate"] = _safe_div(np.sum(selected_ms > DEADLINE_MS), len(selected_ms))
    return row


def _finite_or(value: Any, fallback: float) -> float:
    number = float(value)
    return number if np.isfinite(number) else fallback


def _gate_classification(metrics: Mapping[str, Any]) -> str:
    """Apply the immutable gates; a zero-trigger policy cannot pass a gate."""
    trigger = _finite_or(metrics.get("trigger_rate", np.nan), 1.0)
    if int(metrics.get("triggered_events", 0)) <= 0:
        return "NO_GO"
    recall = _finite_or(metrics.get("recall", np.nan), 0.0)
    macro_recall = _finite_or(metrics.get("macro_recall", np.nan), 0.0)
    gain = _finite_or(
        metrics.get("captured_positive_joint_gain_fraction", np.nan), 0.0
    )
    harmful = _finite_or(metrics.get("harmful_trigger_rate", np.nan), 1.0)
    p95_ratio = _finite_or(
        metrics.get("p95_overhead_fraction_of_always_two", np.nan), np.inf
    )
    below_50 = int(metrics.get("sequences_ge20_below_50pct_recall", 10 ** 9))
    strong = (
        trigger <= 0.35
        and recall >= 0.75
        and macro_recall >= 0.70
        and gain >= 0.75
        and harmful <= 0.10
        and p95_ratio <= 0.50
        and below_50 <= 2
    )
    borderline = (
        trigger <= 0.45
        and recall >= 0.60
        and gain >= 0.60
        and p95_ratio <= 0.70
    )
    return "STRONG" if strong else ("BORDERLINE" if borderline else "NO_GO")


def _grouped_metric_row(
    frame: pd.DataFrame,
    trigger: np.ndarray,
    score: np.ndarray,
) -> Dict[str, Any]:
    """Add sequence-macro gate components to one out-of-fold metric row."""
    metrics = _metric_row(frame, trigger, score)
    folds: List[Dict[str, Any]] = []
    groups = frame["sequence"].to_numpy()
    for sequence in sorted(frame["sequence"].unique()):
        mask = groups == sequence
        folds.append(_metric_row(
            frame.loc[mask], np.asarray(trigger)[mask], np.asarray(score)[mask]
        ))
    fold_frame = pd.DataFrame(folds)
    metrics["macro_precision"] = fold_frame["precision"].mean(skipna=True)
    metrics["macro_recall"] = fold_frame["recall"].mean(skipna=True)
    metrics["macro_f1"] = fold_frame["f1"].mean(skipna=True)
    populated = fold_frame[fold_frame["strict_positive_events"] >= 20]
    metrics["worst_recall_ge20"] = populated["recall"].min(skipna=True)
    metrics["sequences_ge20_below_50pct_recall"] = int(
        np.sum(populated["recall"] < 0.50)
    )
    metrics["gate_result"] = _gate_classification(metrics)
    return metrics


def _dominates(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """True when left is no worse on every honest policy objective."""
    left_values = (
        _finite_or(left.get("recall", np.nan), 0.0),
        _finite_or(left.get("macro_recall", np.nan), 0.0),
        _finite_or(left.get("captured_positive_joint_gain_fraction", np.nan), 0.0),
        -_finite_or(left.get("trigger_rate", np.nan), 1.0),
        -_finite_or(left.get("harmful_trigger_rate", np.nan), 1.0),
        -_finite_or(left.get("p95_overhead_fraction_of_always_two", np.nan), np.inf),
        -float(left.get("sequences_ge20_below_50pct_recall", 10 ** 9)),
    )
    right_values = (
        _finite_or(right.get("recall", np.nan), 0.0),
        _finite_or(right.get("macro_recall", np.nan), 0.0),
        _finite_or(right.get("captured_positive_joint_gain_fraction", np.nan), 0.0),
        -_finite_or(right.get("trigger_rate", np.nan), 1.0),
        -_finite_or(right.get("harmful_trigger_rate", np.nan), 1.0),
        -_finite_or(right.get("p95_overhead_fraction_of_always_two", np.nan), np.inf),
        -float(right.get("sequences_ge20_below_50pct_recall", 10 ** 9)),
    )
    return all(a >= b for a, b in zip(left_values, right_values)) and any(
        a > b for a, b in zip(left_values, right_values)
    )


def _pareto_ranks(metrics: Sequence[Mapping[str, Any]]) -> List[int]:
    """Deterministic non-dominated sorting; zero-trigger candidates rank last."""
    ranks = [-1] * len(metrics)
    remaining = {
        index for index, item in enumerate(metrics)
        if int(item.get("triggered_events", 0)) > 0
    }
    front = 0
    while remaining:
        members = sorted(
            index for index in remaining
            if not any(
                _dominates(metrics[other], metrics[index])
                for other in remaining if other != index
            )
        )
        if not members:  # Defensive only: strict dominance cannot form a cycle.
            members = [min(remaining)]
        for index in members:
            ranks[index] = front
            remaining.remove(index)
        front += 1
    worst = front + 1
    return [worst if rank < 0 else rank for rank in ranks]


def _selection_key(
    metrics: Mapping[str, Any], ordinal: int, pareto_rank: int = 0
) -> Tuple[float, ...]:
    gate_tier = {"STRONG": 2.0, "BORDERLINE": 1.0, "NO_GO": 0.0}.get(
        str(metrics.get("gate_result", "NO_GO")), 0.0
    )
    if int(metrics.get("triggered_events", 0)) <= 0:
        gate_tier = -1.0
    trigger = _finite_or(metrics.get("trigger_rate", np.nan), 1.0)
    recall = _finite_or(metrics.get("recall", np.nan), 0.0)
    macro_recall = _finite_or(metrics.get("macro_recall", np.nan), 0.0)
    gain = _finite_or(
        metrics.get("captured_positive_joint_gain_fraction", np.nan), 0.0
    )
    f1 = _finite_or(metrics.get("f1", np.nan), 0.0)
    precision = _finite_or(metrics.get("precision", np.nan), 0.0)
    harmful = _finite_or(metrics.get("harmful_trigger_rate", np.nan), 1.0)
    p95_ratio = _finite_or(
        metrics.get("p95_overhead_fraction_of_always_two", np.nan), np.inf
    )
    below_50 = float(metrics.get("sequences_ge20_below_50pct_recall", 10 ** 9))

    def at_least(value: float, target: float) -> float:
        return min(1.0, max(0.0, value) / target)

    def at_most(value: float, target: float) -> float:
        if value <= target:
            return 1.0
        return target / value if value > 0.0 and np.isfinite(value) else 0.0

    # When no specification clears a whole tier, use the minimum normalized
    # attainment of its unchanged predeclared gates.  This deterministic
    # Chebyshev tie-break prevents an extreme always/never trigger from winning
    # merely by maximizing recall or latency in isolation; no gate is moved.
    borderline_attainment = min(
        at_most(trigger, 0.45), at_least(recall, 0.60),
        at_least(gain, 0.60), at_most(p95_ratio, 0.70),
    )
    strong_attainment = min(
        at_most(trigger, 0.35), at_least(recall, 0.75),
        at_least(macro_recall, 0.70), at_least(gain, 0.75),
        at_most(harmful, 0.10), at_most(p95_ratio, 0.50),
        at_most(below_50, 2.0),
    )
    return (
        gate_tier, -float(pareto_rank),
        borderline_attainment, strong_attainment,
        (recall + macro_recall + gain) / 3.0,
        f1, precision, -trigger, -float(ordinal),
    )


def _median(frame: pd.DataFrame, feature: str) -> float:
    values = pd.to_numeric(frame[feature], errors="coerce").to_numpy(float)
    finite = values[np.isfinite(values)]
    return float(np.median(finite)) if len(finite) else 0.0


def _select_model_features(
    train: pd.DataFrame,
    causal_candidates: Sequence[str],
    limit: int = MODEL_FEATURE_LIMIT,
) -> Tuple[str, ...]:
    """Select at most eight fields using this training partition only.

    Absolute point-biserial correlation is used only as a compact deterministic
    screen.  Calling this inside every inner/outer fit prevents a validation or
    held-out sequence from influencing the selected model columns.
    """
    validate_causal_features(causal_candidates, max_features=None)
    missing = sorted(set(causal_candidates) - set(train.columns))
    if missing:
        raise ValueError("causal candidate columns missing: %s" % missing)
    truth = train["STRICT_BENEFIT"].to_numpy(float)
    ranked: List[Tuple[float, int, str]] = []
    for ordinal, feature in enumerate(causal_candidates):
        values = pd.to_numeric(train[feature], errors="coerce").to_numpy(float)
        finite = np.isfinite(values)
        median = float(np.median(values[finite])) if finite.any() else 0.0
        values = np.where(finite, values, median)
        if len(values) < 2 or np.std(values) == 0.0 or np.std(truth) == 0.0:
            association = 0.0
        else:
            association = float(abs(np.corrcoef(values, truth)[0, 1]))
            if not np.isfinite(association):
                association = 0.0
        ranked.append((-association, ordinal, feature))
    ranked.sort()
    selected = tuple(item[2] for item in ranked[: min(limit, len(ranked))])
    validate_causal_features(selected)
    return selected


def _threshold_predict(
    train: pd.DataFrame, test: pd.DataFrame, spec: Mapping[str, Any]
) -> Tuple[np.ndarray, np.ndarray, Dict[str, float]]:
    feature = str(spec["feature"])
    median = _median(train, feature)
    train_values = pd.to_numeric(train[feature], errors="coerce").fillna(median).to_numpy(float)
    test_values = pd.to_numeric(test[feature], errors="coerce").fillna(median).to_numpy(float)
    threshold = float(np.quantile(train_values, float(spec["quantile"])))
    if spec["direction"] == "ge":
        pred = test_values >= threshold
    else:
        pred = test_values <= threshold
    # Outer folds may select different physical features.  A binary score is
    # deliberately used so pooled out-of-fold AP never compares unlike units.
    score = pred.astype(float)
    fitted = {
        "feature": feature,
        "median": median,
        "threshold": threshold,
        "direction": str(spec["direction"]),
    }
    return pred, score, fitted


def _two_rule_predict(
    train: pd.DataFrame, test: pd.DataFrame, spec: Mapping[str, Any]
) -> Tuple[np.ndarray, np.ndarray, Dict[str, float]]:
    left_pred, left_score, left_fitted = _threshold_predict(train, test, spec["left"])
    right_pred, right_score, right_fitted = _threshold_predict(train, test, spec["right"])
    if spec["operator"] == "AND":
        pred = left_pred & right_pred
    else:
        pred = left_pred | right_pred
    # Rule-satisfaction count is comparable across heterogeneous feature units
    # and provides a deterministic ranking for average precision.
    score = 0.5 * (left_pred.astype(float) + right_pred.astype(float))
    fitted = {
        "left": left_fitted,
        "right": right_fitted,
        "operator": str(spec["operator"]),
    }
    return pred, score, fitted


def _model_predict(
    train: pd.DataFrame,
    test: pd.DataFrame,
    spec: Mapping[str, Any],
    model_kind: str,
    seed: int,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Tuple[np.ndarray, np.ndarray, Any]:
    selected_features = _select_model_features(train, causal_candidates)
    x_train = train.loc[:, selected_features]
    x_test = test.loc[:, selected_features]
    y_train = train["STRICT_BENEFIT"].to_numpy(int)
    probability_threshold = float(spec["probability_threshold"])
    if len(np.unique(y_train)) < 2:
        probability = np.full(len(test), float(y_train[0]) if len(y_train) else 0.0)
        return probability >= probability_threshold, probability, {
            "model": None,
            "features": selected_features,
            "constant_probability": float(probability[0]) if len(probability) else 0.0,
        }
    if model_kind == "logistic":
        model = Pipeline((
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("scaler", StandardScaler()),
            ("model", LogisticRegression(
                penalty="l1", solver="liblinear", C=float(spec["C"]),
                class_weight="balanced", random_state=seed, max_iter=2000,
            )),
        ))
    elif model_kind == "tree":
        min_leaf = max(1, int(math.ceil(0.05 * len(train))))
        model = Pipeline((
            ("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
            ("model", DecisionTreeClassifier(
                max_depth=int(spec["max_depth"]), min_samples_leaf=min_leaf,
                class_weight="balanced", random_state=seed,
            )),
        ))
    else:
        raise ValueError("unknown model kind %s" % model_kind)
    model.fit(x_train, y_train)
    probability = model.predict_proba(x_test)[:, 1]
    return probability >= probability_threshold, probability, {
        "model": model,
        "features": selected_features,
    }


def _inner_predictions(
    train: pd.DataFrame,
    spec: Mapping[str, Any],
    kind: str,
    seed: int,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Tuple[np.ndarray, np.ndarray]:
    prediction = np.zeros(len(train), dtype=bool)
    score = np.zeros(len(train), dtype=float)
    groups = sorted(train["sequence"].unique())
    if len(groups) < 2:
        raise ValueError("grouped inner validation requires at least two training sequences")
    for fold_index, held_out in enumerate(groups):
        validation_mask = train["sequence"].to_numpy() == held_out
        inner_train = train.loc[~validation_mask]
        inner_validation = train.loc[validation_mask]
        if kind == "threshold":
            pred, fold_score, _ = _threshold_predict(inner_train, inner_validation, spec)
        elif kind == "two_rule":
            pred, fold_score, _ = _two_rule_predict(inner_train, inner_validation, spec)
        else:
            pred, fold_score, _ = _model_predict(
                inner_train, inner_validation, spec, kind, seed + fold_index,
                causal_candidates,
            )
        positions = np.flatnonzero(validation_mask)
        prediction[positions] = pred
        score[positions] = fold_score
    return prediction, score


def _rank_specs(
    train: pd.DataFrame,
    specs: Sequence[Mapping[str, Any]],
    kind: str,
    seed: int,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> List[Tuple[Mapping[str, Any], Dict[str, Any]]]:
    evaluated: List[Tuple[int, Mapping[str, Any], Dict[str, Any]]] = []
    for ordinal, spec in enumerate(specs):
        pred, score = _inner_predictions(
            train, spec, kind, seed, causal_candidates
        )
        metrics = _grouped_metric_row(train, pred, score)
        evaluated.append((ordinal, spec, metrics))
    fronts = _pareto_ranks([item[2] for item in evaluated])
    ranked: List[Tuple[Tuple[float, ...], Mapping[str, Any], Dict[str, Any]]] = []
    for (ordinal, spec, metrics), front in zip(evaluated, fronts):
        metrics["pareto_rank"] = front
        ranked.append((_selection_key(metrics, ordinal, front), spec, metrics))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [(spec, metrics) for _, spec, metrics in ranked]


def _select_policy_specs(
    train: pd.DataFrame,
    seed: int,
    search_grid: Mapping[str, Sequence[Any]],
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Dict[str, Mapping[str, Any]]:
    validate_causal_features(causal_candidates, max_features=None)
    threshold_specs = [
        {"feature": feature, "quantile": float(quantile), "direction": direction}
        for feature in causal_candidates
        for quantile in search_grid["quantiles"]
        for direction in ("ge", "le")
    ]
    ranked_thresholds = _rank_specs(
        train, threshold_specs, "threshold", seed, causal_candidates
    )
    p3 = ranked_thresholds[0][0]
    top_count = int(search_grid["p4_top_rules"][0])
    top_rules = [item[0] for item in ranked_thresholds[:top_count]]
    pair_specs: List[Mapping[str, Any]] = []
    for left_index, left in enumerate(top_rules):
        for right in top_rules[left_index + 1:]:
            if left["feature"] == right["feature"]:
                continue
            for operator in ("AND", "OR"):
                pair_specs.append({"left": left, "right": right, "operator": operator})
    if pair_specs:
        p4 = _rank_specs(
            train, pair_specs, "two_rule", seed + 101, causal_candidates
        )[0][0]
    else:
        p4 = {"left": p3, "right": p3, "operator": "AND"}

    logistic_specs = [
        {"C": float(c_value), "probability_threshold": float(threshold)}
        for c_value in search_grid["logistic_c"]
        for threshold in search_grid["probability_thresholds"]
    ]
    p5 = _rank_specs(
        train, logistic_specs, "logistic", seed + 211, causal_candidates
    )[0][0]
    tree_specs = [
        {"max_depth": int(depth), "probability_threshold": float(threshold)}
        for depth in search_grid["tree_depths"]
        for threshold in search_grid["probability_thresholds"]
    ]
    p6 = _rank_specs(
        train, tree_specs, "tree", seed + 307, causal_candidates
    )[0][0]
    return {
        "P3_SINGLE_THRESHOLD": p3,
        "P4_TWO_RULE": p4,
        "P5_L1_LOGISTIC": p5,
        "P6_DEPTH3_TREE": p6,
    }


def _stable_uniform(seed: int, sequence: str, timestamp: float) -> float:
    # Keep the legacy signature for callers, but sequence identity is not an
    # input to the random baseline.  Current timestamp is causal; future frame
    # population and sequence/difficulty labels are forbidden policy inputs.
    del sequence
    payload = "%d|%.9f" % (seed, timestamp)
    digest = hashlib.blake2b(payload.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(2 ** 64)


def _rate_matched_random(
    frame: pd.DataFrame, rate: float, seed: int
) -> Tuple[np.ndarray, np.ndarray]:
    uniform = np.asarray([
        _stable_uniform(seed, str(sequence), float(timestamp))
        for sequence, timestamp in zip(frame["sequence"], frame["timestamp"])
    ])
    # Each decision depends only on the frozen training rate and this frame's
    # deterministic draw.  Do not rank a complete held-out sequence to force an
    # exact count: that would let future frames change an earlier decision.
    trigger = uniform < max(0.0, min(1.0, rate))
    return trigger, 1.0 - uniform


def _predict_policy(
    policy: str,
    train: pd.DataFrame,
    test: pd.DataFrame,
    specs: Mapping[str, Mapping[str, Any]],
    seed: int,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Tuple[np.ndarray, np.ndarray, Any]:
    if policy == "P0_NEVER":
        return np.zeros(len(test), bool), np.zeros(len(test)), None
    if policy == "P1_ALWAYS":
        return np.ones(len(test), bool), np.ones(len(test)), None
    if policy == "P3_SINGLE_THRESHOLD":
        pred, score, fitted = _threshold_predict(train, test, specs[policy])
        return pred, score, fitted
    if policy == "P4_TWO_RULE":
        pred, score, fitted = _two_rule_predict(train, test, specs[policy])
        return pred, score, fitted
    if policy == "P5_L1_LOGISTIC":
        return _model_predict(
            train, test, specs[policy], "logistic", seed, causal_candidates
        )
    if policy == "P6_DEPTH3_TREE":
        return _model_predict(
            train, test, specs[policy], "tree", seed, causal_candidates
        )
    if policy == "ORACLE_STRICT":
        values = test["STRICT_BENEFIT"].to_numpy(int).astype(bool)
        return values, values.astype(float), None
    if policy == "ORACLE_SELECTED":
        values = test["ORACLE_SELECTED"].to_numpy(int).astype(bool)
        return values, values.astype(float), None
    raise ValueError("unsupported policy %s" % policy)


def _importance_rows(
    outer_sequence: str,
    policy: str,
    spec: Mapping[str, Any],
    fitted: Any,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if policy == "P3_SINGLE_THRESHOLD":
        rows.append({"feature": spec["feature"], "coefficient": np.nan, "importance": 1.0})
    elif policy == "P4_TWO_RULE":
        for side in ("left", "right"):
            rows.append({"feature": spec[side]["feature"], "coefficient": np.nan, "importance": 1.0})
    elif fitted is not None and fitted.get("model") is not None and policy == "P5_L1_LOGISTIC":
        coefficients = fitted["model"].named_steps["model"].coef_[0]
        for feature, coefficient in zip(fitted["features"], coefficients):
            rows.append({"feature": feature, "coefficient": coefficient, "importance": abs(coefficient)})
    elif fitted is not None and fitted.get("model") is not None and policy == "P6_DEPTH3_TREE":
        importances = fitted["model"].named_steps["model"].feature_importances_
        for feature, importance in zip(fitted["features"], importances):
            rows.append({"feature": feature, "coefficient": np.nan, "importance": importance})
    for row in rows:
        row.update({
            "outer_sequence": outer_sequence,
            "policy": policy,
            "selected": int(float(row["importance"]) > 0.0),
            "spec_json": json.dumps(spec, sort_keys=True),
        })
    return rows


def _attach_macro_and_gate(
    policy_results: pd.DataFrame, loso_results: pd.DataFrame
) -> pd.DataFrame:
    output = policy_results.copy()
    for index, row in output.iterrows():
        policy = row["policy"]
        folds = loso_results[loso_results["policy"] == policy]
        output.loc[index, "macro_precision"] = folds["precision"].mean(skipna=True)
        output.loc[index, "macro_recall"] = folds["recall"].mean(skipna=True)
        output.loc[index, "macro_f1"] = folds["f1"].mean(skipna=True)
        populated = folds[folds["strict_positive_events"] >= 20]
        output.loc[index, "worst_recall_ge20"] = populated["recall"].min(skipna=True)
        output.loc[index, "sequences_ge20_below_50pct_recall"] = int(
            np.sum(populated["recall"] < 0.50)
        )
        if policy not in {
            "P3_SINGLE_THRESHOLD", "P4_TWO_RULE", "P5_L1_LOGISTIC", "P6_DEPTH3_TREE"
        }:
            output.loc[index, "gate_result"] = "NOT_APPLICABLE"
            continue
        gate_row = output.loc[index].to_dict()
        output.loc[index, "gate_result"] = _gate_classification(gate_row)
    return output


def run_loso(
    primary: pd.DataFrame,
    seed: int = SEED_DEFAULT,
    search_grid: Optional[Mapping[str, Sequence[Any]]] = None,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Dict[str, pd.DataFrame]:
    """Run honest outer LOSO with grouped inner model/threshold selection."""
    validate_causal_features(causal_candidates, max_features=None)
    missing_candidates = sorted(set(causal_candidates) - set(primary.columns))
    if missing_candidates:
        raise ValueError("primary data missing causal candidates: %s" % missing_candidates)
    grid = DEFAULT_SEARCH_GRID if search_grid is None else search_grid
    sequences = sorted(primary["sequence"].unique())
    if len(sequences) < 3:
        raise ValueError("outer LOSO requires at least three sequence groups")
    prediction_parts: List[pd.DataFrame] = []
    loso_rows: List[Dict[str, Any]] = []
    importance: List[Dict[str, Any]] = []

    for outer_index, held_out in enumerate(sequences):
        test = primary[primary["sequence"] == held_out].reset_index(drop=True)
        train = primary[primary["sequence"] != held_out].reset_index(drop=True)
        specs = _select_policy_specs(
            train, seed + 1000 * outer_index, grid, causal_candidates
        )
        p3_train, _, _ = _predict_policy(
            "P3_SINGLE_THRESHOLD", train, train, specs, seed + outer_index,
            causal_candidates,
        )
        random_rate = float(np.mean(p3_train))
        for policy_index, policy in enumerate(POLICY_ORDER):
            if policy == "P2_RANDOM_RATE_MATCHED":
                pred, score = _rate_matched_random(
                    test, random_rate, seed + 100000 + outer_index
                )
                fitted = None
                spec: Mapping[str, Any] = {
                    "matched_to": "P3_SINGLE_THRESHOLD",
                    "outer_training_trigger_rate": random_rate,
                }
            else:
                pred, score, fitted = _predict_policy(
                    policy, train, test, specs,
                    seed + 10000 * outer_index + policy_index,
                    causal_candidates,
                )
                spec = specs.get(policy, {})
            metrics = _metric_row(test, pred, score)
            metrics.update({
                "outer_sequence": held_out,
                "policy": policy,
                "train_sequences": "|".join(sorted(set(sequences) - {held_out})),
                "test_sequences": held_out,
                "seed": seed,
                "spec_json": json.dumps(spec, sort_keys=True),
            })
            loso_rows.append(metrics)
            importance.extend(_importance_rows(held_out, policy, spec, fitted))
            part = test[[
                "dataset_row_id", "sequence", "timestamp", "STRICT_BENEFIT",
                "HARMFUL", "INVALID", "positive_joint_gain", "one_total", "two_total",
            ]].copy()
            part["outer_sequence"] = held_out
            part["policy"] = policy
            part["trigger"] = pred.astype(np.int8)
            part["score"] = score
            prediction_parts.append(part)

    predictions = pd.concat(prediction_parts, ignore_index=True)
    loso = pd.DataFrame(loso_rows)
    aggregate_rows: List[Dict[str, Any]] = []
    for policy in POLICY_ORDER:
        pred = predictions[predictions["policy"] == policy].copy()
        order = primary[["dataset_row_id"]].copy()
        ordered = order.merge(pred, on="dataset_row_id", validate="one_to_one")
        frame = primary.merge(
            ordered[["dataset_row_id", "trigger", "score"]],
            on="dataset_row_id", validate="one_to_one"
        )
        metrics = _metric_row(
            frame, frame["trigger"].to_numpy(bool), frame["score"].to_numpy(float)
        )
        metrics.update({"policy": policy, "seed": seed})
        aggregate_rows.append(metrics)
    policy_results = _attach_macro_and_gate(pd.DataFrame(aggregate_rows), loso)
    return {
        "policy_results": policy_results,
        "loso_results": loso,
        "per_sequence_results": loso.copy(),
        "feature_importance": pd.DataFrame(importance),
        "predictions": predictions,
    }


def evaluate_ood(
    primary: pd.DataFrame,
    ood: pd.DataFrame,
    seed: int,
    search_grid: Optional[Mapping[str, Sequence[Any]]] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    grid = DEFAULT_SEARCH_GRID if search_grid is None else search_grid
    specs = _select_policy_specs(primary.reset_index(drop=True), seed + 700001, grid)
    p3_train, _, _ = _predict_policy(
        "P3_SINGLE_THRESHOLD", primary, primary, specs, seed + 700002
    )
    random_rate = float(np.mean(p3_train))
    rows: List[Dict[str, Any]] = []
    importance: List[Dict[str, Any]] = []
    for index, policy in enumerate(POLICY_ORDER):
        if policy == "P2_RANDOM_RATE_MATCHED":
            pred, score = _rate_matched_random(ood, random_rate, seed + 700003)
            fitted = None
            spec: Mapping[str, Any] = {
                "matched_to": "P3_SINGLE_THRESHOLD",
                "training_trigger_rate": random_rate,
            }
        else:
            pred, score, fitted = _predict_policy(
                policy, primary, ood, specs, seed + 700100 + index
            )
            spec = specs.get(policy, {})
        row = _metric_row(ood, pred, score)
        row.update({
            "sequence": OOD_SEQUENCE,
            "policy": policy,
            "training_sequences": "|".join(sorted(primary["sequence"].unique())),
            "excluded_from_training_and_primary_metrics": 1,
            "spec_json": json.dumps(spec, sort_keys=True),
        })
        rows.append(row)
        importance.extend(_importance_rows(OOD_SEQUENCE + "_OOD", policy, spec, fitted))
    return pd.DataFrame(rows), pd.DataFrame(importance)


def distribution_shift(dataset: pd.DataFrame) -> pd.DataFrame:
    """Paired full-one/full-two mode shift, globally and by trajectory third."""
    pairs = (
        ("accepted_tracks_pass1", "one_diag_accepted_tracks_pass1", "accepted_tracks_pass1"),
        ("pass1_dx_norm", "one_diag_pass1_dx_norm", "pass1_dx_norm"),
        ("pass1_processing_ms", "one_diag_pass1_processing_ms", "pass1_processing_ms"),
        ("tracking_ms", "one_tracking", "two_tracking"),
        ("propagation_ms", "one_propagation", "two_propagation"),
        ("msckf_update_ms", "one_msckf_update", "two_msckf_update"),
        ("total_ms", "one_total", "two_total"),
    )
    rows: List[Dict[str, Any]] = []
    for population in ("primary", "ood"):
        population_frame = dataset[dataset["population"] == population]
        for segment in ("all", "early", "middle", "late"):
            subset = population_frame if segment == "all" else population_frame[
                population_frame["segment"] == segment
            ]
            if len(subset) and "one_diag_pass1_valid" in subset:
                one_valid = _bool_series(subset["one_diag_pass1_valid"]).to_numpy(bool)
                two_valid = _bool_series(subset["pass1_valid"]).to_numpy(bool)
                rows.append({
                    "population": population,
                    "segment": segment,
                    "field": "pass1_valid",
                    "paired_frames": len(subset),
                    "full_one_mean": float(np.mean(one_valid)),
                    "full_two_mean": float(np.mean(two_valid)),
                    "paired_mean_difference_two_minus_one": float(
                        np.mean(two_valid.astype(float) - one_valid.astype(float))
                    ),
                    "paired_median_absolute_difference": float(
                        np.median(np.abs(two_valid.astype(float) - one_valid.astype(float)))
                    ),
                    "standardized_mean_difference": np.nan,
                    "ks_statistic": np.nan,
                    "paired_disagreement_rate": float(np.mean(one_valid != two_valid)),
                    "audit_scope": "all aligned diagnostics rows before eligibility filtering",
                })
            for field, one_column, two_column in pairs:
                if one_column not in subset or two_column not in subset:
                    continue
                one = pd.to_numeric(subset[one_column], errors="coerce").to_numpy(float)
                two = pd.to_numeric(subset[two_column], errors="coerce").to_numpy(float)
                valid = np.isfinite(one) & np.isfinite(two)
                one, two = one[valid], two[valid]
                if not len(one):
                    continue
                # Timing files store seconds; present shift magnitudes in ms.
                scale = 1000.0 if field.endswith("_ms") and "processing" not in field else 1.0
                one, two = scale * one, scale * two
                pooled_sd = math.sqrt(0.5 * (float(np.var(one)) + float(np.var(two))))
                rows.append({
                    "population": population,
                    "segment": segment,
                    "field": field,
                    "paired_frames": len(one),
                    "full_one_mean": float(np.mean(one)),
                    "full_two_mean": float(np.mean(two)),
                    "paired_mean_difference_two_minus_one": float(np.mean(two - one)),
                    "paired_median_absolute_difference": float(np.median(np.abs(two - one))),
                    "standardized_mean_difference": _safe_div(
                        float(np.mean(two) - np.mean(one)), pooled_sd
                    ),
                    "ks_statistic": float(ks_2samp(one, two, method="asymp").statistic),
                    "paired_disagreement_rate": np.nan,
                    "audit_scope": "all aligned diagnostics rows before eligibility filtering",
                })
    return pd.DataFrame(rows)


def artifact_mode_summary(artifact_root: Path) -> pd.DataFrame:
    """Raw repeat-1 row/timestamp/attempt counts by sequence and mode."""
    rows: List[Dict[str, Any]] = []
    for run in discover_repeat1_runs(artifact_root):
        for mode, role in (("full_one", "one_diagnostics"), ("full_two", "two_diagnostics")):
            frame = pd.read_csv(run[role])
            attempted = pd.to_numeric(frame.get("attempted_passes"), errors="coerce")
            p1_valid = _bool_series(frame["pass1_valid"])
            p2_valid = _bool_series(frame["pass2_valid"])
            selected = pd.to_numeric(frame.get("selected_pass"), errors="coerce")
            rows.append({
                "source_phase": str(run["phase"]),
                "sequence": str(run["sequence"]),
                "mode": mode,
                "rows": len(frame),
                "unique_timestamps": frame["timestamp"].nunique(dropna=True),
                "pass1_valid_rows": int(p1_valid.sum()),
                "pass2_attempt_rows": int((attempted >= 2).sum()),
                "pass2_valid_rows": int(p2_valid.sum()),
                "selected_pass2_rows": int((selected == 2).sum()),
            })
    return pd.DataFrame(rows)


def _field_classification(field: str) -> Tuple[str, str, str]:
    if field in ALL_CAUSAL_FEATURES:
        return "policy_feature", "yes", "available after Pass 1; frozen whitelist"
    lower = field.lower()
    if field in LABEL_COLUMNS or field in {
        "tau_pix", "tau_post", "relative_pixel_gain", "relative_posterior_gain",
        "rel_gain_pix", "rel_gain_post", "joint_gain", "positive_joint_gain",
        "pass2_production_valid",
    }:
        return "outcome_label", "no", "Pass-2-derived target; forbidden leakage"
    if lower.startswith("pass2") or "pass2" in lower or field in {
        "accepted_tracks_pass2",
        "selected_pass", "selection_reason", "terminal_status", "completed_passes",
        "fallback_reason", "oracle_selected_pass", "oracle_reason",
        "absolute_cost_change", "percentage_cost_change",
        "cpix_reduction", "cpix_reduction_percent", "cpost_reduction",
        "cpost_reduction_percent", "affine_correction_norm",
    }:
        return "post_decision_outcome", "no", "unavailable before Pass-2 decision"
    if lower.startswith("one_") or lower.startswith("one_diag_"):
        return "paired_mode_audit", "no", "counterfactual mode field; audit/timing only"
    if lower.startswith("two_"):
        return "paired_timing_audit", "no", "full-two timing; counterfactual only"
    if field in {"sequence", "segment", "population", "source_phase", "source_sequence_dir"}:
        return "group_or_provenance", "no", "grouping/audit only; identity excluded from model"
    if field in {"timestamp", "dataset_row_id"}:
        return "identifier", "no", "alignment only"
    if field.startswith("pass1") or field == "accepted_tracks_pass1":
        return "causal_not_predeclared", "no", "not in compact frozen feature whitelist"
    return "cohort_or_diagnostic", "no", "retained for audit; not a model input"


def _field_units(field: str, source: str) -> str:
    lower = field.lower()
    if "timestamp" in lower or lower == "duration_s":
        return "seconds"
    if lower in {"one_total", "two_total", "shadow_total", "shadow_skip_total", "shadow_trigger_total"}:
        return "seconds"
    if lower.endswith("_ms"):
        return "milliseconds"
    if source.endswith("timing.csv") and field != "timestamp":
        return "seconds"
    if lower.endswith("_m"):
        return "metres"
    if lower.endswith("_deg"):
        return "degrees"
    if "percent" in lower or lower == "completion":
        return "percent or categorical"
    if any(token in lower for token in ("count", "tracks", "rows", "passes", "commits", "frames")):
        return "count"
    if "time" in lower and "sha" not in lower:
        return "implementation-defined time"
    if any(token in lower for token in (
        "nis", "ratio", "gain", "norm", "rms", "cpix", "cpost", "cost"
    )):
        return "dimensionless/objective units"
    return "categorical or unitless"


def _field_definition(field: str) -> str:
    definitions = {
        "reduced_rows_pass1": "Reduced whitened visual-system row count logged for accepted Pass 1.",
        "pass1_nis_per_row": "Pass-1 global proposal NIS divided by reduced_rows_pass1.",
        "pass1_max_gate_ratio": "Pass-1 maximum feature NIS divided by its gate threshold.",
        "rel_gain_pix": "(Cpix1-Cpix2)/max(abs(Cpix1), tau_pix).",
        "rel_gain_post": "(Cpost1-Cpost2)/max(abs(Cpost1), tau_post).",
        "joint_gain": "Minimum of rel_gain_pix and rel_gain_post.",
        "positive_joint_gain": "max(joint_gain, 0); remains NaN when Pass 2 is invalid.",
        "STRICT_BENEFIT": "Production-valid Pass 2 strictly improves both objectives beyond tolerance.",
        "ORACLE_SELECTED": "Historical selector choice, or shadow oracle_selected_pass when shadow_only.",
        "pass1_valid": "Whether Pass 1 produced a production-valid candidate.",
        "pass2_valid": "Whether Pass 2 produced a production-valid candidate.",
        "selected_pass": "Pass committed by the historical live selector; never an input feature.",
        "oracle_selected_pass": "Separate shadow-oracle decision; live shadow selected_pass remains 1.",
    }
    return definitions.get(field, field.replace("_", " ") + ".")


def _audit_availability(field: str) -> Tuple[str, str, str, str, str]:
    role, eligible, rationale = _field_classification(field)
    lower = field.lower()
    before = "yes" if (
        field in ALL_CAUSAL_FEATURES
        or lower.startswith("pass1_")
        or field in {"accepted_tracks_pass1", "reduced_rows_pass1", "timestamp", "requested_passes"}
    ) else "no"
    after = "yes" if role in {"post_decision_outcome", "outcome_label", "paired_timing_audit"} else "no"
    label_only = "yes" if role in {"post_decision_outcome", "outcome_label"} else "no"
    if eligible == "yes":
        leakage = "none under frozen decision-time contract"
        intended = "predeclared causal policy feature"
    elif field in {"sequence", "segment", "population", "source_phase", "source_sequence_dir"}:
        leakage = "high if modeled: identity/difficulty proxy"
        intended = "grouping, segmentation, or provenance only"
    elif after == "yes" or lower.startswith("one_"):
        leakage = "high if modeled: post-decision/counterfactual information"
        intended = "label, distribution audit, or latency estimate only"
    else:
        leakage = "excluded by fail-closed whitelist"
        intended = rationale
    return before, after, eligible, label_only, leakage + "; " + intended


def _csv_audit_records(
    paths: Sequence[Path], source: str, timing: bool = False
) -> List[Dict[str, Any]]:
    accumulator: Dict[str, Dict[str, Any]] = {}
    for csv_path in paths:
        if timing:
            frame = _read_timing(csv_path)
        else:
            frame = pd.read_csv(csv_path, dtype=str)
        path_mode = csv_path.parent.name
        for field in frame.columns:
            stats = accumulator.setdefault(field, {"total": 0, "missing": 0, "modes": set()})
            missing = frame[field].isna() | (frame[field].astype(str).str.strip() == "")
            stats["total"] += len(frame)
            stats["missing"] += int(missing.sum())
            if "mode" in frame and field != "mode":
                stats["modes"].update(frame.loc[~missing, "mode"].dropna().astype(str).unique())
            elif (~missing).any():
                stats["modes"].add(path_mode)
    records: List[Dict[str, Any]] = []
    for field, stats in sorted(accumulator.items()):
        records.append({
            "field": field,
            "source": source,
            "populated_modes": ", ".join(sorted(stats["modes"])),
            "missingness": "%d/%d (%.3f%%)" % (
                stats["missing"], stats["total"],
                100.0 * stats["missing"] / stats["total"] if stats["total"] else 0.0,
            ),
        })
    return records


def write_field_audit(path: Path, artifact_root: Path, dataset: pd.DataFrame) -> None:
    records: List[Dict[str, Any]] = []
    for filename in ("FRAME_COSTS.csv", "METRICS.csv"):
        csv_path = artifact_root / filename
        if csv_path.is_file():
            records.extend(_csv_audit_records((csv_path,), filename))
    diagnostic_paths = sorted(
        path for phase in ("phase4", "phase5", "phase6")
        for path in (artifact_root / phase).glob("*/*/diagnostics.csv")
    )
    timing_paths = sorted(
        path for phase in ("phase4", "phase5", "phase6")
        for path in (artifact_root / phase).glob("*/*/timing.csv")
    )
    records.extend(_csv_audit_records(
        diagnostic_paths, "phase4/phase5/phase6 diagnostics.csv"
    ))
    records.extend(_csv_audit_records(
        timing_paths, "phase4/phase5/phase6 timing.csv", timing=True
    ))
    raw_diagnostic_fields = {
        record["field"] for record in records
        if record["source"] == "phase4/phase5/phase6 diagnostics.csv"
    }
    for field in dataset.columns:
        if field in raw_diagnostic_fields:
            continue
        missing = int(dataset[field].isna().sum())
        records.append({
            "field": field,
            "source": "derived VALUE_ITERATION_DATASET.csv",
            "populated_modes": "repeat-1 full_two primary/OOD rows",
            "missingness": "%d/%d (%.3f%%)" % (
                missing, len(dataset), 100.0 * missing / len(dataset),
            ),
        })

    lines = [
        "# Field audit",
        "",
        "Scanned structured fields in `FRAME_COSTS.csv`, `METRICS.csv`, and phase4/phase5/phase6 "
        "diagnostics/timing CSVs. `FRAME_ANALYSIS.md` was treated as narrative analysis, not a "
        "machine-field source. The learning dataset remains phase4/phase5 repeat-1 only.",
        "",
        "The policy matrix is fail-closed: only the eight rows marked causal `yes` are passed to "
        "models. `completed_passes` is audited as an available raw field but is intentionally not "
        "ingested into the learning dataset; `accepted_set_hash` is not parsed from logs.",
        "",
        "| Field | Source | Definition | Units | Populated modes | Available before Pass 2? | Available only after Pass 2? | Causal policy feature? | Label-only? | Leakage risk | Missingness | Intended use |",
        "|---|---|---|---|---|---:|---:|---:|---:|---|---|---|",
    ]
    for record in sorted(records, key=lambda item: (item["source"], item["field"])):
        field = record["field"]
        before, after, causal, label_only, risk_and_use = _audit_availability(field)
        risk, intended = risk_and_use.split("; ", 1)
        values = (
            "`%s`" % field,
            record["source"],
            _field_definition(field),
            _field_units(field, record["source"]),
            record["populated_modes"] or "none",
            before, after, causal, label_only, risk,
            record["missingness"], intended,
        )
        lines.append("| " + " | ".join(str(value).replace("|", "/") for value in values) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dataset_schema(
    path: Path,
    dataset: pd.DataFrame,
    output_frames: Optional[Mapping[str, pd.DataFrame]] = None,
) -> None:
    descriptions = {
        "STRICT_BENEFIT": "P2 production-valid and both objectives improve beyond frozen tolerances.",
        "ORACLE_SELECTED": "Production selector chose Pass 2 and Pass 2 is production-valid.",
        "NONHARMFUL": "Neither objective worsens beyond tolerance for a production-valid Pass 2.",
        "INVALID": "Pass 2 is invalid or lacks finite before/after objective values.",
        "HARMFUL": "At least one objective worsens beyond tolerance for a production-valid Pass 2.",
        "PIXEL_ONLY_HELP": "Pixel objective improves strictly; posterior objective does not.",
        "POSTERIOR_ONLY_HELP": "Posterior objective improves strictly; pixel objective does not.",
        "EQUAL_WITHIN_TOLERANCE": "Both objective changes are within their frozen tolerances.",
        "tau_pix": "1e-9 * max(1, abs(pass1_cpix)).",
        "tau_post": "1e-9 * max(1, abs(pass1_cpost)).",
        "relative_pixel_gain": "(Cpix1-Cpix2)/max(abs(Cpix1), tau_pix); NaN when invalid.",
        "relative_posterior_gain": "(Cpost1-Cpost2)/max(abs(Cpost1), tau_post); NaN when invalid.",
        "rel_gain_pix": "Canonical (Cpix1-Cpix2)/max(abs(Cpix1), tau_pix); NaN when invalid.",
        "rel_gain_post": "Canonical (Cpost1-Cpost2)/max(abs(Cpost1), tau_post); NaN when invalid.",
        "joint_gain": "Minimum of the two normalized objective gains.",
        "positive_joint_gain": "max(joint_gain, 0), remaining NaN for invalid Pass 2.",
        "segment": "Early/middle/late third by ordered eligible frames within sequence.",
        "population": "primary except MH_04, which is held out as ood.",
        "one_total": (
            "Skip counterfactual in seconds: aligned full-one time for historical data, "
            "or same-callback shadow total minus measured Pass-2 processing for shadow data."
        ),
        "two_total": (
            "Trigger counterfactual in seconds: aligned full-two time for historical data, "
            "or observed same-callback shadow total for shadow data."
        ),
        "p95_overhead_fraction_of_always_two": (
            "(estimated counterfactual total p95 - one-pass total p95) / "
            "(fixed-two total p95 - one-pass total p95)."
        ),
    }
    lines = [
        "# Dataset schema",
        "",
        "One row is a repeat-1 full-two frame with valid Pass 1 and an attempted Pass 2. "
        "MH_04 rows are tagged `ood` and never enter training, inner selection, LOSO, or primary metrics.",
        "",
        "| Column | pandas dtype | Description |",
        "|---|---|---|",
    ]
    for column in dataset.columns:
        role, eligible, rationale = _field_classification(column)
        description = descriptions.get(column, "%s; policy_input=%s. %s" % (role, eligible, rationale))
        lines.append("| `%s` | `%s` | %s |" % (column, dataset[column].dtype, description))
    if output_frames:
        lines.extend([
            "", "# Summary CSV schemas", "",
            "Every generated summary-CSV column is listed below. Metric fields retain the "
            "definitions above; `precision` and `recall` in aggregate policy files are micro "
            "metrics, while `macro_*` fields are unweighted sequence means.",
        ])
        for output_name, frame in output_frames.items():
            lines.extend([
                "", "## `%s`" % output_name, "",
                "| Column | pandas dtype | Description |", "|---|---|---|",
            ])
            for column in frame.columns:
                role, eligible, rationale = _field_classification(column)
                description = descriptions.get(
                    column, "%s; policy_input=%s. %s" % (role, eligible, rationale)
                )
                lines.append(
                    "| `%s` | `%s` | %s |"
                    % (column, frame[column].dtype, description)
                )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return None if not np.isfinite(value) else float(value)
    if isinstance(value, Path):
        return str(value)
    return value


def _select_recommendation(policy_results: pd.DataFrame) -> Dict[str, Any]:
    adaptive = policy_results[policy_results["policy"].isin(POLICY_ORDER[3:7])].copy()
    for gate in ("STRONG", "BORDERLINE"):
        eligible = adaptive[adaptive["gate_result"] == gate]
        if not eligible.empty:
            records = eligible.to_dict(orient="records")
            fronts = _pareto_ranks(records)
            frontier = eligible.loc[np.asarray(fronts) == 0].copy()
            order = {policy: index for index, policy in enumerate(POLICY_ORDER)}
            selected = frontier.sort_values(
                "policy", key=lambda values: values.map(order)
            ).iloc[0]
            return {
                "classification": "STRONG GO" if gate == "STRONG" else "BORDERLINE",
                "selected_policy": selected["policy"],
                "pareto_frontier": sorted(
                    frontier["policy"].tolist(), key=lambda value: order[value]
                ),
                "passed_stable_ten_gate": True,
            }
    # The stress protocol is frozen before its labels are inspected.  Even when
    # no adaptive policy passes a stable-ten gate, retain the least-complex
    # non-dominated diagnostic candidate so recovery-stress can evaluate it
    # without tuning.  This does not upgrade the NO-GO classification.
    records = adaptive.to_dict(orient="records")
    fronts = _pareto_ranks(records)
    frontier = adaptive.loc[np.asarray(fronts) == 0].copy()
    order = {policy: index for index, policy in enumerate(POLICY_ORDER)}
    selected_policy = None
    frontier_names: List[str] = []
    if not frontier.empty:
        frontier_names = sorted(
            frontier["policy"].tolist(), key=lambda value: order[value]
        )
        selected_policy = frontier_names[0]
    return {
        "classification": "NO_GO",
        "selected_policy": selected_policy,
        "pareto_frontier": frontier_names,
        "passed_stable_ten_gate": False,
    }


def _numeric_with_median(frame: pd.DataFrame, feature: str, median: float) -> np.ndarray:
    values = pd.to_numeric(frame[feature], errors="coerce").to_numpy(float)
    return np.where(np.isfinite(values), values, float(median))


def predict_materialized_policy(
    policy_spec: Mapping[str, Any], frame: pd.DataFrame
) -> Tuple[np.ndarray, np.ndarray]:
    """Evaluate a JSON-serializable frozen policy specification."""
    policy = str(policy_spec.get("policy"))
    if policy == "P3_SINGLE_THRESHOLD":
        rule = policy_spec["rule"]
        values = _numeric_with_median(frame, rule["feature"], rule["missing_median"])
        trigger = values >= rule["threshold"] if rule["direction"] == "ge" else (
            values <= rule["threshold"]
        )
        return trigger, trigger.astype(float)
    if policy == "P4_TWO_RULE":
        predicates: List[np.ndarray] = []
        for side in ("left", "right"):
            rule = policy_spec[side]
            values = _numeric_with_median(frame, rule["feature"], rule["missing_median"])
            predicates.append(values >= rule["threshold"] if rule["direction"] == "ge" else (
                values <= rule["threshold"]
            ))
        trigger = predicates[0] & predicates[1] if policy_spec["operator"] == "AND" else (
            predicates[0] | predicates[1]
        )
        return trigger, 0.5 * (
            predicates[0].astype(float) + predicates[1].astype(float)
        )
    if policy_spec.get("constant_probability") is not None:
        probability = np.full(len(frame), float(policy_spec["constant_probability"]))
        return probability >= float(policy_spec["probability_threshold"]), probability

    features = tuple(policy_spec["features"])
    medians = np.asarray(policy_spec["imputer_medians"], dtype=float)
    matrix = np.column_stack([
        _numeric_with_median(frame, feature, median)
        for feature, median in zip(features, medians)
    ])
    probability_threshold = float(policy_spec["probability_threshold"])
    if policy == "P5_L1_LOGISTIC":
        means = np.asarray(policy_spec["scaler_means"], dtype=float)
        scales = np.asarray(policy_spec["scaler_scales"], dtype=float)
        coefficients = np.asarray(policy_spec["coefficients"], dtype=float)
        linear = float(policy_spec["intercept"]) + ((matrix - means) / scales).dot(coefficients)
        probability = np.empty(len(linear), dtype=float)
        nonnegative = linear >= 0.0
        probability[nonnegative] = 1.0 / (1.0 + np.exp(-linear[nonnegative]))
        exponent = np.exp(linear[~nonnegative])
        probability[~nonnegative] = exponent / (1.0 + exponent)
        return probability >= probability_threshold, probability
    if policy == "P6_DEPTH3_TREE":
        children_left = np.asarray(policy_spec["children_left"], dtype=int)
        children_right = np.asarray(policy_spec["children_right"], dtype=int)
        split_features = np.asarray(policy_spec["split_feature_indices"], dtype=int)
        thresholds = np.asarray(policy_spec["split_thresholds"], dtype=float)
        values = np.asarray(policy_spec["node_class_weights"], dtype=float)
        positive_class_index = int(policy_spec["positive_class_index"])
        probability = np.zeros(len(frame), dtype=float)
        for row_index, row in enumerate(matrix):
            node = 0
            while children_left[node] != children_right[node]:
                node = children_left[node] if row[split_features[node]] <= thresholds[node] else (
                    children_right[node]
                )
            total = float(np.sum(values[node]))
            probability[row_index] = values[node, positive_class_index] / total if total else 0.0
        return probability >= probability_threshold, probability
    raise ValueError("unsupported materialized policy %s" % policy)


def materialize_policy(
    primary: pd.DataFrame,
    recommendation: Mapping[str, Any],
    seed: int,
    search_grid: Optional[Mapping[str, Sequence[Any]]] = None,
    causal_candidates: Sequence[str] = CAUSAL_FEATURES,
) -> Dict[str, Any]:
    """Tune on all stable-ten groups and serialize the exact frozen policy."""
    selected_policy = recommendation.get("selected_policy")
    base: Dict[str, Any] = {
        "classification": recommendation.get("classification", "NO_GO"),
        "policy": selected_policy,
        "seed": seed,
        "training_sequences": sorted(primary["sequence"].unique().tolist()),
        "training_frames": int(len(primary)),
        "causal_candidate_pool": list(causal_candidates),
        "model_feature_limit": MODEL_FEATURE_LIMIT,
        "pareto_frontier": list(recommendation.get("pareto_frontier", [])),
    }
    if selected_policy is None:
        base.update({
            "formula": "No adaptive policy passed the unchanged stable-ten LOSO gates.",
            "missing_value_handling": "not applicable",
            "operation_count": 0,
            "expected_complexity": "O(1)",
        })
        return base

    grid = DEFAULT_SEARCH_GRID if search_grid is None else search_grid
    specs = _select_policy_specs(primary.reset_index(drop=True), seed + 900001, grid, causal_candidates)
    prediction, _, fitted = _predict_policy(
        str(selected_policy), primary, primary, specs, seed + 900002, causal_candidates
    )
    base["training_selection_spec"] = dict(specs[str(selected_policy)])
    if selected_policy == "P3_SINGLE_THRESHOLD":
        base.update({
            "rule": {
                "feature": fitted["feature"],
                "units": _field_units(fitted["feature"], "shadow diagnostics.csv"),
                "direction": fitted["direction"],
                "threshold": fitted["threshold"],
                "missing_median": fitted["median"],
            },
            "formula": "trigger = imputed_feature %s threshold" % (
                ">=" if fitted["direction"] == "ge" else "<="
            ),
            "missing_value_handling": "Replace the feature with the stored training median.",
            "operation_count": 2,
            "expected_complexity": "O(1) per frame",
        })
    elif selected_policy == "P4_TWO_RULE":
        for side in ("left", "right"):
            rule = fitted[side]
            base[side] = {
                "feature": rule["feature"],
                "units": _field_units(rule["feature"], "shadow diagnostics.csv"),
                "direction": rule["direction"],
                "threshold": rule["threshold"],
                "missing_median": rule["median"],
            }
        base.update({
            "operator": fitted["operator"],
            "formula": "trigger = left_rule %s right_rule" % fitted["operator"],
            "missing_value_handling": "Replace each feature with its stored training median.",
            "operation_count": 5,
            "expected_complexity": "O(1) per frame",
        })
    else:
        features = list(fitted["features"])
        model = fitted["model"]
        base["features"] = features
        base["probability_threshold"] = float(specs[str(selected_policy)]["probability_threshold"])
        if model is None:
            base.update({
                "constant_probability": float(fitted["constant_probability"]),
                "imputer_medians": [0.0] * len(features),
                "formula": "trigger = constant_probability >= probability_threshold",
                "missing_value_handling": "No features are evaluated by the constant model.",
                "operation_count": 1,
                "expected_complexity": "O(1) per frame",
            })
        else:
            imputer = model.named_steps["imputer"]
            base["imputer_medians"] = np.asarray(imputer.statistics_, dtype=float).tolist()
            base["constant_probability"] = None
            if selected_policy == "P5_L1_LOGISTIC":
                scaler = model.named_steps["scaler"]
                classifier = model.named_steps["model"]
                base.update({
                    "scaler_means": np.asarray(scaler.mean_, dtype=float).tolist(),
                    "scaler_scales": np.asarray(scaler.scale_, dtype=float).tolist(),
                    "coefficients": np.asarray(classifier.coef_[0], dtype=float).tolist(),
                    "intercept": float(classifier.intercept_[0]),
                    "formula": (
                        "p=sigmoid(intercept + sum_i coefficient_i * "
                        "((imputed_x_i-scaler_mean_i)/scaler_scale_i)); "
                        "trigger = p >= probability_threshold"
                    ),
                    "missing_value_handling": "Replace each feature with its stored training median.",
                    "operation_count": 4 * len(features) + 4,
                    "expected_complexity": "O(k) per frame, k<=8",
                })
            else:
                classifier = model.named_steps["model"]
                tree = classifier.tree_
                classes = np.asarray(classifier.classes_).tolist()
                positive_index = classes.index(1)
                base.update({
                    "classes": classes,
                    "positive_class_index": positive_index,
                    "children_left": tree.children_left.astype(int).tolist(),
                    "children_right": tree.children_right.astype(int).tolist(),
                    "split_feature_indices": tree.feature.astype(int).tolist(),
                    "split_thresholds": tree.threshold.astype(float).tolist(),
                    "node_class_weights": tree.value[:, 0, :].astype(float).tolist(),
                    "formula": "Traverse the complete stored tree, compute leaf class probability, and compare with probability_threshold.",
                    "missing_value_handling": "Replace each feature with its stored training median before tree traversal.",
                    "operation_count": len(features) + int(classifier.get_depth()) + 2,
                    "expected_complexity": "O(k + depth), k<=8 and depth<=3",
                })
    replay_prediction, _ = predict_materialized_policy(base, primary)
    if not np.array_equal(np.asarray(prediction, dtype=bool), replay_prediction):
        raise AssertionError("materialized policy does not reproduce its fitted predictions")
    return _json_safe(base)


def write_frozen_policy_markdown(path: Path, policy_spec: Mapping[str, Any]) -> None:
    lines = [
        "# Frozen policy specification", "",
        "- Classification: `%s`" % policy_spec.get("classification"),
        "- Policy: `%s`" % policy_spec.get("policy"),
        "- Formula: %s" % policy_spec.get("formula"),
        "- Missing values: %s" % policy_spec.get("missing_value_handling"),
        "- Operation count estimate: `%s`" % policy_spec.get("operation_count"),
        "- Expected complexity: `%s`" % policy_spec.get("expected_complexity"),
        "- Training sequences: `%s`" % "|".join(policy_spec.get("training_sequences", [])),
        "", "## Machine-readable specification", "", "```json",
        json.dumps(_json_safe(policy_spec), indent=2, sort_keys=True), "```", "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def compare_existing_and_shadow(
    existing: pd.DataFrame,
    shadow: pd.DataFrame,
    existing_policies: pd.DataFrame,
    shadow_policies: pd.DataFrame,
) -> pd.DataFrame:
    """Long-form cohort, paired-label, feature, and policy comparison."""
    rows: List[Dict[str, Any]] = []
    old_recommendation = _select_recommendation(existing_policies)
    new_recommendation = _select_recommendation(shadow_policies)
    for metric in ("classification", "selected_policy"):
        rows.append({
            "scope": "recommendation", "sequence": "all", "metric": metric,
            "existing_value": np.nan, "shadow_value": np.nan,
            "shadow_minus_existing": np.nan,
            "existing_text": old_recommendation.get(metric),
            "shadow_text": new_recommendation.get(metric),
            "existing_rows": len(existing), "shadow_rows": len(shadow),
            "paired_rows": np.nan, "ks_statistic": np.nan,
            "notes": "unchanged gates and least-complex honest Pareto selection",
        })
    scopes = [("all", existing, shadow)] + [
        (
            sequence,
            existing[existing["sequence"] == sequence],
            shadow[shadow["sequence"] == sequence],
        ) for sequence in STABLE_TEN
    ]
    for sequence, old, new in scopes:
        for metric in LABEL_COLUMNS:
            old_value = float(old[metric].mean()) if len(old) else np.nan
            new_value = float(new[metric].mean()) if len(new) else np.nan
            rows.append({
                "scope": "cohort", "sequence": sequence, "metric": metric + "_prevalence",
                "existing_value": old_value, "shadow_value": new_value,
                "shadow_minus_existing": new_value - old_value,
                "existing_rows": len(old), "shadow_rows": len(new),
                "paired_rows": np.nan, "ks_statistic": np.nan,
                "notes": "independent eligible populations",
            })
        old_keyed = old.assign(
            _timestamp_key=np.rint(old["timestamp"].to_numpy(float) * 1.0e6).astype(np.int64)
        )
        new_keyed = new.assign(
            _timestamp_key=np.rint(new["timestamp"].to_numpy(float) * 1.0e6).astype(np.int64)
        )
        if old_keyed.duplicated(["sequence", "_timestamp_key"]).any() or new_keyed.duplicated(
            ["sequence", "_timestamp_key"]
        ).any():
            raise ValueError("timestamp microsecond key is not unique for %s" % sequence)
        paired = old_keyed.merge(
            new_keyed, on=["sequence", "_timestamp_key"], suffixes=("_existing", "_shadow")
        )
        for label in ("STRICT_BENEFIT", "ORACLE_SELECTED", "INVALID", "HARMFUL"):
            disagreement = float(np.mean(
                paired[label + "_existing"].to_numpy(int) !=
                paired[label + "_shadow"].to_numpy(int)
            )) if len(paired) else np.nan
            rows.append({
                "scope": "paired_label", "sequence": sequence,
                "metric": label + "_disagreement_rate",
                "existing_value": np.nan, "shadow_value": disagreement,
                "shadow_minus_existing": np.nan,
                "existing_rows": len(old), "shadow_rows": len(new),
                "paired_rows": len(paired),
                "ks_statistic": np.nan, "notes": "timestamp matched at sensor microsecond resolution",
            })
    for feature in SHADOW_CAUSAL_CANDIDATES:
        old_values = pd.to_numeric(existing.get(feature), errors="coerce").to_numpy(float) if feature in existing else np.asarray([])
        new_values = pd.to_numeric(shadow.get(feature), errors="coerce").to_numpy(float) if feature in shadow else np.asarray([])
        old_values = old_values[np.isfinite(old_values)]
        new_values = new_values[np.isfinite(new_values)]
        old_mean = float(np.mean(old_values)) if len(old_values) else np.nan
        new_mean = float(np.mean(new_values)) if len(new_values) else np.nan
        rows.append({
            "scope": "feature_distribution", "sequence": "all", "metric": feature,
            "existing_value": old_mean, "shadow_value": new_mean,
            "shadow_minus_existing": new_mean - old_mean,
            "existing_rows": len(old_values), "shadow_rows": len(new_values),
            "paired_rows": np.nan,
            "ks_statistic": float(ks_2samp(old_values, new_values, method="asymp").statistic)
            if len(old_values) and len(new_values) else np.nan,
            "notes": "unpaired marginal distribution; NaN marks unavailable historical field",
        })
    for policy in POLICY_ORDER:
        old_rows = existing_policies[existing_policies["policy"] == policy]
        new_rows = shadow_policies[shadow_policies["policy"] == policy]
        if old_rows.empty or new_rows.empty:
            continue
        for metric in (
            "trigger_rate", "recall", "macro_recall",
            "captured_positive_joint_gain_fraction", "harmful_trigger_rate",
            "counterfactual_total_p95_ms", "p95_overhead_fraction_of_always_two",
        ):
            old_value = float(old_rows.iloc[0][metric])
            new_value = float(new_rows.iloc[0][metric])
            rows.append({
                "scope": "policy", "sequence": policy, "metric": metric,
                "existing_value": old_value, "shadow_value": new_value,
                "shadow_minus_existing": new_value - old_value,
                "existing_rows": int(old_rows.iloc[0]["frames"]),
                "shadow_rows": int(new_rows.iloc[0]["frames"]),
                "paired_rows": np.nan, "ks_statistic": np.nan,
                "notes": "honest outer-LOSO aggregate",
            })
    return pd.DataFrame(rows)


def run_study(
    artifact_root: Path,
    output_dir: Path,
    seed: int,
    shadow_artifact_root: Optional[Path] = None,
) -> Dict[str, Any]:
    artifact_root = artifact_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not artifact_root.is_dir():
        raise FileNotFoundError("artifact root does not exist: %s" % artifact_root)
    if output_dir == artifact_root or artifact_root in output_dir.parents:
        raise ValueError("output directory must be external to the immutable artifact root")
    resolved_shadow_root: Optional[Path] = None
    if shadow_artifact_root is not None:
        resolved_shadow_root = shadow_artifact_root.expanduser().resolve()
        if output_dir == resolved_shadow_root or resolved_shadow_root in output_dir.parents:
            raise ValueError("output directory must be external to the immutable shadow root")
    output_dir.mkdir(parents=True, exist_ok=True)
    required_outputs = (
        "VALUE_ITERATION_DATASET.csv", "FIELD_AUDIT.md", "DATASET_SCHEMA.md",
        "POLICY_RESULTS.csv", "LOSO_RESULTS.csv", "FEATURE_IMPORTANCE.csv",
        "PER_SEQUENCE_RESULTS.csv", "INPUT_MANIFEST.csv", "OOD_RESULTS.csv",
        "DISTRIBUTION_SHIFT.csv", "ARTIFACT_MODE_SUMMARY.csv",
        "DATASET_SUMMARY.csv", "MISSINGNESS.csv", "ALIGNMENT_SUMMARY.csv",
        "STUDY_SUMMARY.json",
    )
    if resolved_shadow_root is not None:
        required_outputs += (
            "SHADOW_INPUT_MANIFEST.csv", "SHADOW_DATASET.csv",
            "SHADOW_DATASET_SUMMARY.csv", "SHADOW_POLICY_RESULTS.csv",
            "SHADOW_LOSO_RESULTS.csv", "SHADOW_PER_SEQUENCE_RESULTS.csv",
            "SHADOW_FEATURE_IMPORTANCE.csv", "SHADOW_PREDICTIONS.csv",
            "EXISTING_VS_SHADOW_COMPARISON.csv", "FROZEN_POLICY_SPEC.json",
            "FROZEN_POLICY_SPEC.md",
        )
    existing = [name for name in required_outputs if (output_dir / name).exists()]
    if existing:
        raise FileExistsError("refusing to overwrite existing study outputs: %s" % existing)

    dataset, manifest, diagnostic_fields = load_artifact_dataset(artifact_root)
    primary = dataset[dataset["population"] == "primary"].reset_index(drop=True)
    ood = dataset[dataset["population"] == "ood"].reset_index(drop=True)
    if OOD_SEQUENCE in set(primary["sequence"]):
        raise AssertionError("MH_04 leaked into primary population")
    if set(ood["sequence"]) != {OOD_SEQUENCE}:
        raise AssertionError("MH_04 OOD population is missing or contaminated")

    results = run_loso(primary, seed=seed)
    ood_results, ood_importance = evaluate_ood(primary, ood, seed=seed)
    importance = pd.concat(
        [results["feature_importance"], ood_importance], ignore_index=True, sort=False
    )
    mode_audit = load_mode_audit_frame(artifact_root)
    shift = distribution_shift(mode_audit)
    mode_summary = artifact_mode_summary(artifact_root)

    shadow_summary: Optional[Dict[str, Any]] = None
    shadow: Optional[pd.DataFrame] = None
    shadow_results: Optional[Dict[str, pd.DataFrame]] = None
    shadow_sequence_summary: Optional[pd.DataFrame] = None
    shadow_manifest = pd.DataFrame()
    shadow_comparison = pd.DataFrame()
    shadow_recommendation: Optional[Dict[str, Any]] = None
    frozen_policy: Optional[Dict[str, Any]] = None
    if resolved_shadow_root is not None:
        shadow = load_shadow_dataset(resolved_shadow_root)
        shadow_manifest = pd.DataFrame(shadow.attrs.get("input_manifest", []))
        shadow_results = run_loso(
            shadow, seed=seed, causal_candidates=SHADOW_CAUSAL_CANDIDATES
        )
        shadow_recommendation = _select_recommendation(
            shadow_results["policy_results"]
        )
        frozen_policy = materialize_policy(
            shadow, shadow_recommendation, seed,
            causal_candidates=SHADOW_CAUSAL_CANDIDATES,
        )
        shadow_comparison = compare_existing_and_shadow(
            primary, shadow, results["policy_results"],
            shadow_results["policy_results"],
        )
        shadow.to_csv(output_dir / "SHADOW_DATASET.csv", index=False)
        shadow_sequence_summary = shadow.groupby("sequence", as_index=False).agg(
            frames=("dataset_row_id", "size"),
            unique_timestamps=("timestamp", "nunique"),
            pass2_attempt_events=("attempted_passes", lambda values: int((values >= 2).sum())),
            pass2_valid_events=("pass2_production_valid", "sum"),
            strict_benefit_events=("STRICT_BENEFIT", "sum"),
            oracle_selected_events=("ORACLE_SELECTED", "sum"),
            nonharmful_events=("NONHARMFUL", "sum"),
            invalid_events=("INVALID", "sum"),
            harmful_events=("HARMFUL", "sum"),
            equal_within_tolerance_events=("EQUAL_WITHIN_TOLERANCE", "sum"),
            pixel_only_help_events=("PIXEL_ONLY_HELP", "sum"),
            posterior_only_help_events=("POSTERIOR_ONLY_HELP", "sum"),
            positive_joint_gain=("positive_joint_gain", "sum"),
        )
        shadow_sequence_summary.to_csv(
            output_dir / "SHADOW_DATASET_SUMMARY.csv", index=False
        )
        shadow_manifest.to_csv(output_dir / "SHADOW_INPUT_MANIFEST.csv", index=False)
        shadow_results["policy_results"].to_csv(
            output_dir / "SHADOW_POLICY_RESULTS.csv", index=False
        )
        shadow_results["loso_results"].to_csv(
            output_dir / "SHADOW_LOSO_RESULTS.csv", index=False
        )
        shadow_results["per_sequence_results"].to_csv(
            output_dir / "SHADOW_PER_SEQUENCE_RESULTS.csv", index=False
        )
        shadow_results["feature_importance"].to_csv(
            output_dir / "SHADOW_FEATURE_IMPORTANCE.csv", index=False
        )
        shadow_results["predictions"].to_csv(
            output_dir / "SHADOW_PREDICTIONS.csv", index=False
        )
        shadow_comparison.to_csv(
            output_dir / "EXISTING_VS_SHADOW_COMPARISON.csv", index=False
        )
        (output_dir / "FROZEN_POLICY_SPEC.json").write_text(
            json.dumps(_json_safe(frozen_policy), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        write_frozen_policy_markdown(
            output_dir / "FROZEN_POLICY_SPEC.md", frozen_policy
        )
        shadow_summary = {
            "artifact_root": str(resolved_shadow_root),
            "frames": len(shadow),
            "sequences": sorted(shadow["sequence"].unique().tolist()),
            "strict_benefit_events": int(shadow["STRICT_BENEFIT"].sum()),
            "oracle_selected_events": int(shadow["ORACLE_SELECTED"].sum()),
            "invalid_events": int(shadow["INVALID"].sum()),
            "harmful_events": int(shadow["HARMFUL"].sum()),
            "oracle_selector_field": "oracle_selected_pass",
            "live_selected_pass_never_two": True,
            "eligible_live_selected_pass_asserted": 1,
            "recommendation": shadow_recommendation,
        }

    dataset.to_csv(output_dir / "VALUE_ITERATION_DATASET.csv", index=False)
    manifest.to_csv(output_dir / "INPUT_MANIFEST.csv", index=False)
    results["policy_results"].to_csv(output_dir / "POLICY_RESULTS.csv", index=False)
    results["loso_results"].to_csv(output_dir / "LOSO_RESULTS.csv", index=False)
    results["per_sequence_results"].to_csv(
        output_dir / "PER_SEQUENCE_RESULTS.csv", index=False
    )
    importance.to_csv(output_dir / "FEATURE_IMPORTANCE.csv", index=False)
    ood_results.to_csv(output_dir / "OOD_RESULTS.csv", index=False)
    shift.to_csv(output_dir / "DISTRIBUTION_SHIFT.csv", index=False)
    mode_summary.to_csv(output_dir / "ARTIFACT_MODE_SUMMARY.csv", index=False)

    sequence_summary = dataset.groupby(["population", "sequence"], as_index=False).agg(
        frames=("dataset_row_id", "size"),
        unique_timestamps=("timestamp", "nunique"),
        pass2_attempt_events=("attempted_passes", lambda values: int((values >= 2).sum())),
        pass2_valid_events=("pass2_production_valid", "sum"),
        selected_pass2_events=("ORACLE_SELECTED", "sum"),
        strict_benefit_events=("STRICT_BENEFIT", "sum"),
        nonharmful_events=("NONHARMFUL", "sum"),
        invalid_events=("INVALID", "sum"),
        harmful_events=("HARMFUL", "sum"),
        equal_within_tolerance_events=("EQUAL_WITHIN_TOLERANCE", "sum"),
        pixel_only_help_events=("PIXEL_ONLY_HELP", "sum"),
        posterior_only_help_events=("POSTERIOR_ONLY_HELP", "sum"),
        positive_joint_gain=("positive_joint_gain", "sum"),
    )
    sequence_summary.to_csv(output_dir / "DATASET_SUMMARY.csv", index=False)
    missingness = pd.DataFrame({
        "field": dataset.columns,
        "missing_count": [int(dataset[column].isna().sum()) for column in dataset.columns],
        "row_count": len(dataset),
        "missing_fraction": [float(dataset[column].isna().mean()) for column in dataset.columns],
    })
    missingness.to_csv(output_dir / "MISSINGNESS.csv", index=False)
    alignment_summary = dataset.groupby(["population", "sequence"], as_index=False).agg(
        eligible_rows=("dataset_row_id", "size"),
        unique_timestamps=("timestamp", "nunique"),
        max_full_one_timing_delta_seconds=("one_timestamp_delta_seconds", "max"),
        max_full_two_timing_delta_seconds=("two_timestamp_delta_seconds", "max"),
        max_full_one_diagnostics_delta_seconds=("one_diag_timestamp_delta_seconds", "max"),
        max_pass1_log_delta_seconds=("log_timestamp_delta_seconds", "max"),
    )
    alignment_summary.to_csv(output_dir / "ALIGNMENT_SUMMARY.csv", index=False)
    write_field_audit(output_dir / "FIELD_AUDIT.md", artifact_root, dataset)
    output_frames: Dict[str, pd.DataFrame] = {
        "INPUT_MANIFEST.csv": manifest,
        "POLICY_RESULTS.csv": results["policy_results"],
        "LOSO_RESULTS.csv": results["loso_results"],
        "PER_SEQUENCE_RESULTS.csv": results["per_sequence_results"],
        "FEATURE_IMPORTANCE.csv": importance,
        "OOD_RESULTS.csv": ood_results,
        "DISTRIBUTION_SHIFT.csv": shift,
        "ARTIFACT_MODE_SUMMARY.csv": mode_summary,
        "DATASET_SUMMARY.csv": sequence_summary,
        "MISSINGNESS.csv": missingness,
        "ALIGNMENT_SUMMARY.csv": alignment_summary,
    }
    if shadow_results is not None and shadow is not None and shadow_sequence_summary is not None:
        output_frames.update({
            "SHADOW_INPUT_MANIFEST.csv": shadow_manifest,
            "SHADOW_DATASET.csv": shadow,
            "SHADOW_DATASET_SUMMARY.csv": shadow_sequence_summary,
            "SHADOW_POLICY_RESULTS.csv": shadow_results["policy_results"],
            "SHADOW_LOSO_RESULTS.csv": shadow_results["loso_results"],
            "SHADOW_PER_SEQUENCE_RESULTS.csv": shadow_results["per_sequence_results"],
            "SHADOW_FEATURE_IMPORTANCE.csv": shadow_results["feature_importance"],
            "SHADOW_PREDICTIONS.csv": shadow_results["predictions"],
            "EXISTING_VS_SHADOW_COMPARISON.csv": shadow_comparison,
        })
    write_dataset_schema(
        output_dir / "DATASET_SCHEMA.md", dataset, output_frames
    )

    recommendation = _select_recommendation(results["policy_results"])
    summary = {
        "artifact_root": str(artifact_root),
        "output_dir": str(output_dir),
        "seed": seed,
        "input_scope": "phase4/phase5 repeat-1 full_one/full_two only",
        "causal_features": list(CAUSAL_FEATURES),
        "primary_sequences": sorted(primary["sequence"].unique().tolist()),
        "ood_sequence": OOD_SEQUENCE,
        "primary_frames": len(primary),
        "ood_frames": len(ood),
        "primary_strict_benefit_events": int(primary["STRICT_BENEFIT"].sum()),
        "primary_invalid_events": int(primary["INVALID"].sum()),
        "primary_harmful_events": int(primary["HARMFUL"].sum()),
        "mh04_excluded_from_training_selection_and_primary_metrics": True,
        "timestamp_alignment_tolerance_seconds": TIMESTAMP_TOLERANCE_SECONDS,
        "deadline_ms": DEADLINE_MS,
        "recommendation": recommendation,
        "shadow_recommendation": shadow_recommendation,
        "frozen_policy": frozen_policy,
        "policy_results": results["policy_results"].to_dict(orient="records"),
        "notes": [
            "All preprocessing, thresholds, feature-rule selection, and model selection are fit inside grouped training folds.",
            "P2 uses an independent deterministic per-frame hash draw below the outer-training P3 trigger rate; it never ranks future held-out frames.",
            "The paired modes are closed-loop trajectories; DISTRIBUTION_SHIFT.csv quantifies rather than erases that limitation.",
            "Distribution shift uses all aligned diagnostics rows before Pass-1/Pass-2 eligibility filtering and chronological per-sequence thirds.",
        ],
        "shadow_ingestion": shadow_summary,
    }
    (output_dir / "STUDY_SUMMARY.json").write_text(
        json.dumps(_json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return _json_safe(summary)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact-root", required=True, type=Path,
        help="read-only overnight artifact root containing phase4 and phase5",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="new output directory external to the artifact root",
    )
    parser.add_argument("--seed", type=int, default=SEED_DEFAULT)
    parser.add_argument(
        "--shadow-artifact-root", type=Path,
        help=("optional explicit shadow-run root; diagnostics must expose shadow_only "
              "and oracle_selected_pass; live selected_pass may be 0 outside "
              "opportunities but must never be 2"),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    summary = run_study(
        args.artifact_root, args.output_dir, args.seed, args.shadow_artifact_root
    )
    print(json.dumps({
        "output_dir": summary["output_dir"],
        "primary_frames": summary["primary_frames"],
        "ood_frames": summary["ood_frames"],
        "recommendation": summary["recommendation"],
        "shadow_recommendation": summary.get("shadow_recommendation"),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
