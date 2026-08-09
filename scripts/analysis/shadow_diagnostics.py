#!/usr/bin/env python3
"""Extract fail-closed frame diagnostics from one shadow-only EuRoC run.

The shadow estimator always commits the legacy Pass-1 proposal.  Its offline
Pass-2 oracle decision is therefore exported separately and is never allowed
to overwrite ``selected_pass``.  Timing counterfactuals use only measurements
from the same shadow callback: triggering is the observed total time and
skipping is the observed total time minus the measured Pass-2 processing time.
"""

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
KEY_VALUE_RE = re.compile(r"(?:^|\s)([A-Za-z][A-Za-z0-9_]*)=([^\s]+)")
ITER_MARKER = "[MSCKF-ITER]:"
CAUSAL_MARKER = "[MSCKF-SHADOW-CAUSAL]:"
TIMESTAMP_TOLERANCE_SECONDS = 1.0e-4

# Preserve the historical Gate-D frame schema before appending fields whose
# meaning is specific to shadow execution.
STANDARD_COLUMNS: Tuple[str, ...] = (
    "sequence",
    "mode",
    "timestamp",
    "requested_passes",
    "attempted_passes",
    "completed_passes",
    "pass1_valid",
    "pass2_valid",
    "selected_pass",
    "accepted_tracks_pass1",
    "accepted_tracks_pass2",
    "pass1_global_nis",
    "pass1_max_feature_nis",
    "pass1_gate_threshold",
    "pass2_max_feature_nis",
    "pass2_gate_threshold",
    "pass1_cpix",
    "pass2_cpix",
    "cpix_reduction",
    "cpix_reduction_percent",
    "pass1_cpost",
    "pass2_cpost",
    "cpost_reduction",
    "cpost_reduction_percent",
    "pass1_dx_norm",
    "pass2_dx_norm",
    "pass1_processing_ms",
    "pass2_processing_ms",
    "affine_correction_norm",
    "selection_reason",
    "terminal_status",
    "mean_commits",
    "covariance_commits",
    "feature_finalizations",
)

SHADOW_COLUMNS: Tuple[str, ...] = (
    "shadow_only",
    "oracle_selected_pass",
    "oracle_reason",
    "oracle_effect_on_live",
    "cost_difference_available",
    "pixel_difference",
    "posterior_difference",
    "pixel_tolerance",
    "posterior_tolerance",
    "pass2_invalid_reason",
    "reduced_rows_pass1",
    "pass1_nis_per_row",
    "pass1_max_gate_ratio",
    "pass1_causal_diagnostics_available",
    "pass1_reduced_whitened_residual_rms_available",
    "pass1_reduced_whitened_residual_rms",
    "pass1_prior_whitened_correction_norm_available",
    "pass1_prior_whitened_correction_norm",
    "pass1_imu_block_norms_available",
    "pass1_orientation_correction_norm",
    "pass1_position_correction_norm",
    "pass1_velocity_correction_norm",
    "pass1_gyro_bias_correction_norm",
    "pass1_accelerometer_bias_correction_norm",
    "pass1_clone_aggregate_correction_norm_available",
    "pass1_clone_aggregate_correction_norm",
    "pass1_minimum_schur_singular_ratio_available",
    "pass1_minimum_schur_singular_ratio",
    "pass1_median_track_observations_available",
    "pass1_median_track_observations",
    "timing_timestamp_delta_seconds",
    "shadow_tracking",
    "shadow_propagation",
    "shadow_msckf_update",
    "shadow_slam_update",
    "shadow_slam_delayed",
    "shadow_retri_marg",
    "shadow_total",
    "shadow_observed_total",
    "shadow_pass2_added_seconds",
    "shadow_skip_total",
    "shadow_trigger_total",
    # These aliases let the existing value-of-iteration metrics consume the
    # honest same-callback counterfactual without changing its public seam.
    "one_total",
    "two_total",
)

OUTPUT_COLUMNS = STANDARD_COLUMNS + SHADOW_COLUMNS
FORBIDDEN_OUTPUT_COLUMNS = frozenset(("accepted_set_hash",))


class ExtractionError(ValueError):
    """Raised when a run cannot satisfy the shadow diagnostics contract."""


def _clean_line(line: str) -> str:
    return ANSI_RE.sub("", line.rstrip("\n"))


def _finite_float(value: Optional[str], context: str, required: bool = False) -> Optional[float]:
    if value in (None, "", "unavailable"):
        if required:
            raise ExtractionError("%s is missing" % context)
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ExtractionError("%s is not numeric: %r" % (context, value))
    if not math.isfinite(result):
        raise ExtractionError("%s is nonfinite: %r" % (context, value))
    return result


def _integer(value: Optional[str], context: str, required: bool = False) -> Optional[int]:
    if value in (None, ""):
        if required:
            raise ExtractionError("%s is missing" % context)
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ExtractionError("%s is not an integer: %r" % (context, value))
    return result


def _parse_payload(payload: str, line_number: int) -> Dict[str, str]:
    record: Dict[str, str] = {}
    for match in KEY_VALUE_RE.finditer(payload):
        key, value = match.group(1), match.group(2).rstrip(",")
        # The feature-population hash is post-selection identity information.
        # It is deliberately never retained by this extractor.
        if key == "accepted_set_hash":
            continue
        if key in record:
            raise ExtractionError(
                "line %d repeats key %s" % (line_number, key)
            )
        record[key] = value
    record["_line_number"] = str(line_number)
    record["_payload"] = payload
    return record


def _read_log_records(path: Path) -> Tuple[List[str], List[Dict[str, str]], List[Dict[str, str]]]:
    if not path.is_file():
        raise ExtractionError("missing stdout log: %s" % path)
    lines: List[str] = []
    iterations: List[Dict[str, str]] = []
    causal: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line_number, raw in enumerate(stream, start=1):
            line = _clean_line(raw)
            lines.append(line)
            if ITER_MARKER in line:
                iterations.append(
                    _parse_payload(line.split(ITER_MARKER, 1)[1].strip(), line_number)
                )
            if CAUSAL_MARKER in line:
                causal.append(
                    _parse_payload(line.split(CAUSAL_MARKER, 1)[1].strip(), line_number)
                )
    if not iterations:
        raise ExtractionError("stdout contains no %s records" % ITER_MARKER)
    return lines, iterations, causal


def _record_timestamp(record: Mapping[str, str], marker: str) -> float:
    line = record.get("_line_number", "?")
    return _finite_float(record.get("timestamp"), "%s line %s timestamp" % (marker, line), True)  # type: ignore


def _group_by_timestamp(records: Iterable[Dict[str, str]], marker: str) -> "OrderedDict[float, List[Dict[str, str]]]":
    grouped: "OrderedDict[float, List[Dict[str, str]]]" = OrderedDict()
    for record in records:
        timestamp = _record_timestamp(record, marker)
        grouped.setdefault(timestamp, []).append(record)
    return grouped


def _single(records: Sequence[Dict[str, str]], description: str) -> Dict[str, str]:
    if len(records) != 1:
        raise ExtractionError(
            "%s has %d records; expected exactly one" % (description, len(records))
        )
    return records[0]


def _optional_single(records: Sequence[Dict[str, str]], description: str) -> Optional[Dict[str, str]]:
    if len(records) > 1:
        raise ExtractionError(
            "%s has %d records; expected at most one" % (description, len(records))
        )
    return records[0] if records else None


def _require_equal(actual: object, expected: object, context: str) -> None:
    if actual != expected:
        raise ExtractionError("%s is %r; expected %r" % (context, actual, expected))


def _bool_flag(record: Mapping[str, str], key: str, context: str, required: bool = True) -> Optional[bool]:
    value = _integer(record.get(key), "%s %s" % (context, key), required)
    if value is None:
        return None
    if value not in (0, 1):
        raise ExtractionError("%s %s is not 0/1" % (context, key))
    return bool(value)


def _available_value(
    record: Mapping[str, str], availability_key: str, value_key: str, context: str
) -> Tuple[bool, Optional[float]]:
    available = _bool_flag(record, availability_key, context, True)
    value = _finite_float(record.get(value_key), "%s %s" % (context, value_key), True)
    # C++ logs a finite zero placeholder when an item is unavailable.  Preserve
    # the availability bit and export the semantic value as missing.
    return bool(available), value if available else None


def _reduction(first: Optional[float], second: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if first is None or second is None:
        return None, None
    absolute = first - second
    percent = None if first == 0.0 else 100.0 * absolute / abs(first)
    return absolute, percent


TIMING_INPUT_COLUMNS: Tuple[str, ...] = (
    "timestamp",
    "tracking",
    "propagation",
    "msckf_update",
    "slam_update",
    "slam_delayed",
    "retri_marg",
    "total",
)


def _read_timing(path: Path) -> List[Dict[str, float]]:
    if not path.is_file():
        raise ExtractionError("missing timing CSV: %s" % path)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        rows = list(csv.reader(stream))
    rows = [row for row in rows if row and any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise ExtractionError("timing CSV has no data rows: %s" % path)
    expected_header = (
        "timestamp (sec)", "tracking", "propagation", "msckf update",
        "slam update", "slam delayed", "re-tri & marg", "total",
    )
    header = tuple(cell.strip().lstrip("#").strip() for cell in rows[0])
    if header != expected_header:
        raise ExtractionError("unexpected timing CSV header in %s: %r" % (path, header))
    output: List[Dict[str, float]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(TIMING_INPUT_COLUMNS):
            raise ExtractionError("timing row %d has %d fields" % (row_number, len(row)))
        values = [
            _finite_float(cell.strip(), "timing row %d column %s" % (row_number, name), True)
            for name, cell in zip(TIMING_INPUT_COLUMNS, row)
        ]
        if any(value is None for value in values):
            raise AssertionError("required timing conversion returned None")
        record = dict(zip(TIMING_INPUT_COLUMNS, values))  # type: ignore
        if any(record[name] < 0.0 for name in TIMING_INPUT_COLUMNS[1:]):
            raise ExtractionError("timing row %d contains a negative duration" % row_number)
        output.append(record)
    timestamps = [row["timestamp"] for row in output]
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        raise ExtractionError("timing timestamps are duplicate or nonincreasing")
    return output


def _join_timing(frames: List[Dict[str, object]], timing: List[Dict[str, float]]) -> None:
    if len(frames) != len(timing):
        raise ExtractionError(
            "terminal/timing row count differs: %d versus %d" % (len(frames), len(timing))
        )
    ordered_frames = sorted(frames, key=lambda row: float(row["timestamp"]))
    if ordered_frames != frames:
        raise ExtractionError("terminal timestamps are not strictly increasing in log order")
    previous: Optional[float] = None
    for frame, timing_row in zip(frames, timing):
        timestamp = float(frame["timestamp"])
        if previous is not None and timestamp <= previous:
            raise ExtractionError("terminal timestamps are duplicate or nonincreasing")
        previous = timestamp
        delta = abs(timestamp - timing_row["timestamp"])
        if delta > TIMESTAMP_TOLERANCE_SECONDS:
            raise ExtractionError(
                "terminal/timing timestamp mismatch %.9g exceeds %.9g at %.17g"
                % (delta, TIMESTAMP_TOLERANCE_SECONDS, timestamp)
            )
        frame["timing_timestamp_delta_seconds"] = delta
        for name in TIMING_INPUT_COLUMNS[1:-1]:
            output_name = "shadow_%s" % name
            frame[output_name] = timing_row[name]
        observed = timing_row["total"]
        frame["shadow_total"] = observed
        frame["shadow_observed_total"] = observed
        frame["shadow_trigger_total"] = observed
        frame["two_total"] = observed
        attempted = int(frame["attempted_passes"])
        pass2_ms = frame.get("pass2_processing_ms")
        if attempted >= 2:
            if pass2_ms is None:
                raise ExtractionError(
                    "timestamp %.17g attempted Pass 2 without measured processing_ms" % timestamp
                )
            added = float(pass2_ms) * 1.0e-3
            skip = observed - added
            if skip < -1.0e-12:
                raise ExtractionError(
                    "timestamp %.17g Pass-2 time exceeds observed total" % timestamp
                )
            skip = max(0.0, skip)
            frame["shadow_pass2_added_seconds"] = added
            frame["shadow_skip_total"] = skip
            frame["one_total"] = skip
        else:
            frame["shadow_pass2_added_seconds"] = None
            frame["shadow_skip_total"] = observed
            frame["one_total"] = observed


def _build_frame(
    timestamp: float,
    records: Sequence[Dict[str, str]],
    causal_record: Optional[Dict[str, str]],
    sequence: str,
    mode: str,
) -> Dict[str, object]:
    context = "timestamp %.17g" % timestamp
    terminal = _single(
        [record for record in records if record.get("terminal") == "1"],
        context + " terminal",
    )
    if terminal.get("shadow_only") != "1":
        raise ExtractionError(context + " terminal does not assert shadow_only=1")

    pass_one = _optional_single(
        [record for record in records if record.get("pass") == "1"],
        context + " Pass-1",
    )
    pass_two = _optional_single(
        [record for record in records if record.get("pass") == "2"],
        context + " Pass-2",
    )
    decisions = [
        record for record in records
        if record.get("terminal") != "1"
        and "selected_pass" in record
        and "oracle_selected_pass" in record
    ]
    decision = _optional_single(decisions, context + " oracle decision")

    requested = _integer(terminal.get("requested_passes"), context + " requested_passes", True)
    attempted = _integer(terminal.get("attempted_passes"), context + " attempted_passes", True)
    completed = _integer(terminal.get("completed_passes"), context + " completed_passes", True)
    selected = _integer(terminal.get("selected_pass"), context + " selected_pass", True)
    oracle_selected = _integer(
        terminal.get("oracle_selected_pass"), context + " oracle_selected_pass", True
    )
    oracle_reason = terminal.get("oracle_reason")
    status = terminal.get("status")
    if not oracle_reason or not status:
        raise ExtractionError(context + " terminal lacks oracle_reason or status")
    _require_equal(requested, 2, context + " requested_passes")
    if attempted not in (0, 1, 2) or completed not in (0, 1, 2):
        raise ExtractionError(context + " attempted/completed passes are outside 0..2")
    if completed > attempted:
        raise ExtractionError(context + " completed more passes than attempted")
    if oracle_selected not in (0, 1, 2):
        raise ExtractionError(context + " oracle_selected_pass is outside 0..2")

    mean_commits = _integer(terminal.get("mean_commits"), context + " mean_commits", True)
    covariance_commits = _integer(
        terminal.get("covariance_commits"), context + " covariance_commits", True
    )
    feature_finalizations = _integer(
        terminal.get("feature_finalizations"), context + " feature_finalizations", True
    )
    if status == "committed":
        if selected != 1:
            raise ExtractionError(context + " shadow live commit did not select Pass 1")
        if (mean_commits, covariance_commits, feature_finalizations) != (1, 1, 1):
            raise ExtractionError(context + " shadow commit mutation counts are not 1/1/1")
    else:
        if selected != 0:
            raise ExtractionError(context + " rejected shadow update selected a live pass")
        if mean_commits != 0 or covariance_commits != 0:
            raise ExtractionError(context + " rejected shadow update reports a state commit")

    if decision is not None:
        if decision.get("shadow_only") != "1":
            raise ExtractionError(context + " decision does not assert shadow_only=1")
        checks = (
            ("requested_passes", requested),
            ("attempted_passes", attempted),
            ("completed_passes", completed),
            ("selected_pass", selected),
            ("oracle_selected_pass", oracle_selected),
        )
        for key, expected in checks:
            actual = _integer(decision.get(key), context + " decision " + key, True)
            _require_equal(actual, expected, context + " decision/terminal " + key)
        _require_equal(
            decision.get("oracle_reason"), oracle_reason,
            context + " decision/terminal oracle_reason",
        )
        _require_equal(
            _integer(decision.get("oracle_effect_on_live"), context + " oracle_effect_on_live", True),
            0, context + " oracle_effect_on_live",
        )
    elif attempted > 0 and status == "committed":
        raise ExtractionError(context + " committed shadow update lacks an oracle decision")

    if attempted >= 2:
        if pass_one is None or pass_two is None or causal_record is None:
            raise ExtractionError(context + " attempted Pass 2 lacks Pass-1, Pass-2, or causal record")
        if pass_one.get("status") != "accepted":
            raise ExtractionError(context + " attempted Pass 2 without accepted Pass 1")
        pass2_accepted = pass_two.get("status") == "accepted"
        pass2_invalid = pass_two.get("status") == "invalid"
        if not (pass2_accepted or pass2_invalid):
            raise ExtractionError(context + " Pass-2 status is neither accepted nor invalid")
        _require_equal(completed, 2 if pass2_accepted else 1, context + " completed_passes")
        if pass2_accepted and oracle_selected not in (1, 2):
            raise ExtractionError(context + " valid Pass 2 has unavailable oracle decision")
        if pass2_invalid and oracle_selected != 1:
            raise ExtractionError(context + " invalid Pass 2 was not rejected by oracle")
        if oracle_selected == 2 and oracle_reason != "dual_cost_accepted":
            raise ExtractionError(context + " oracle selected Pass 2 for a contradictory reason")
    else:
        if pass_two is not None:
            raise ExtractionError(context + " contains Pass-2 record without a Pass-2 attempt")
        pass2_accepted = False
        pass2_invalid = False
        if causal_record is not None:
            raise ExtractionError(context + " has causal record without a Pass-2 attempt")

    cpix_one = _finite_float(pass_one.get("Cpix"), context + " Pass-1 Cpix", True) if pass_one else None
    cpix_two = _finite_float(pass_two.get("Cpix"), context + " Pass-2 Cpix", True) if pass2_accepted else None
    cpost_one = _finite_float(pass_one.get("Cpost"), context + " Pass-1 Cpost", True) if pass_one else None
    cpost_two = _finite_float(pass_two.get("Cpost"), context + " Pass-2 Cpost", True) if pass2_accepted else None
    cpix_reduction, cpix_percent = _reduction(cpix_one, cpix_two)
    cpost_reduction, cpost_percent = _reduction(cpost_one, cpost_two)

    frame: Dict[str, object] = {
        "sequence": sequence,
        "mode": mode,
        "timestamp": timestamp,
        "requested_passes": requested,
        "attempted_passes": attempted,
        "completed_passes": completed,
        "pass1_valid": bool(pass_one and pass_one.get("status") == "accepted") or status == "committed",
        "pass2_valid": pass2_accepted,
        "selected_pass": selected,
        "accepted_tracks_pass1": _integer(
            pass_one.get("accepted_features") if pass_one else terminal.get("accepted_features"),
            context + " accepted_tracks_pass1",
            status == "committed",
        ),
        "accepted_tracks_pass2": _integer(
            pass_two.get("accepted_features"), context + " accepted_tracks_pass2", True
        ) if pass_two else None,
        "pass1_global_nis": _finite_float(pass_one.get("global_proposal_nis"), context + " Pass-1 NIS", True) if pass_one else None,
        "pass1_max_feature_nis": _finite_float(pass_one.get("max_feature_gate_nis"), context + " Pass-1 max feature NIS", True) if pass_one else None,
        "pass1_gate_threshold": _finite_float(pass_one.get("threshold_at_max_feature_nis"), context + " Pass-1 gate threshold", True) if pass_one else None,
        "pass2_max_feature_nis": _finite_float(
            pass_two.get("max_feature_nis", pass_two.get("feature_nis")),
            context + " Pass-2 max feature NIS",
            False,
        ) if pass_two else None,
        "pass2_gate_threshold": _finite_float(
            pass_two.get("threshold_at_max_feature_nis", pass_two.get("threshold_at_feature_nis")),
            context + " Pass-2 gate threshold",
            False,
        ) if pass_two else None,
        "pass1_cpix": cpix_one,
        "pass2_cpix": cpix_two,
        "cpix_reduction": cpix_reduction,
        "cpix_reduction_percent": cpix_percent,
        "pass1_cpost": cpost_one,
        "pass2_cpost": cpost_two,
        "cpost_reduction": cpost_reduction,
        "cpost_reduction_percent": cpost_percent,
        "pass1_dx_norm": _finite_float(pass_one.get("dx_norm"), context + " Pass-1 dx_norm", True) if pass_one else _finite_float(terminal.get("dx_norm"), context + " terminal dx_norm", False),
        "pass2_dx_norm": _finite_float(pass_two.get("dx_norm"), context + " Pass-2 dx_norm", pass2_accepted) if pass_two else None,
        "pass1_processing_ms": _finite_float(pass_one.get("processing_ms"), context + " Pass-1 processing_ms", True) if pass_one else None,
        "pass2_processing_ms": _finite_float(pass_two.get("processing_ms"), context + " Pass-2 processing_ms", attempted >= 2) if pass_two else None,
        "affine_correction_norm": _finite_float(pass_two.get("affine_correction_norm"), context + " affine correction", pass2_accepted) if pass_two else None,
        "selection_reason": terminal.get("reason"),
        "terminal_status": status,
        "mean_commits": mean_commits,
        "covariance_commits": covariance_commits,
        "feature_finalizations": feature_finalizations,
        "shadow_only": 1,
        "oracle_selected_pass": oracle_selected,
        "oracle_reason": oracle_reason,
        "oracle_effect_on_live": 0,
        "cost_difference_available": False,
        "pixel_difference": None,
        "posterior_difference": None,
        "pixel_tolerance": None,
        "posterior_tolerance": None,
        "pass2_invalid_reason": pass_two.get("reason") if pass2_invalid else None,
    }

    if decision is not None:
        cost_available = bool(_bool_flag(decision, "cost_difference_available", context, False) or False)
        frame["cost_difference_available"] = cost_available
        for key in ("pixel_difference", "posterior_difference", "pixel_tolerance", "posterior_tolerance"):
            present = key in decision
            value = _finite_float(
                decision.get(key), context + " " + key, cost_available or present
            )
            frame[key] = value if present else None

    if causal_record is not None:
        if causal_record.get("shadow_only") != "1" or causal_record.get("finalized_before_pass2") != "1":
            raise ExtractionError(context + " causal record is not finalized shadow-only data")
        diagnostic_available = _bool_flag(causal_record, "diagnostics_available", context, True)
        if not diagnostic_available:
            raise ExtractionError(context + " causal diagnostic construction failed")
        causal_tracks = _integer(causal_record.get("accepted_tracks_pass1"), context + " causal accepted tracks", True)
        rows = _integer(causal_record.get("pass1_compressed_rows"), context + " causal compressed rows", True)
        _require_equal(causal_tracks, frame["accepted_tracks_pass1"], context + " causal/pass1 tracks")
        pass_one_rows = _integer(pass_one.get("rows"), context + " Pass-1 rows", True)  # type: ignore
        _require_equal(rows, pass_one_rows, context + " causal/pass1 rows")
        nis_available, nis_per_row = _available_value(
            causal_record, "pass1_compressed_nis_per_row_available",
            "pass1_compressed_nis_per_row", context,
        )
        if not nis_available or nis_per_row is None or not rows or rows <= 0:
            raise ExtractionError(context + " causal NIS-per-row is unavailable")
        expected_nis_per_row = float(frame["pass1_global_nis"]) / rows
        bound = 1.0e-10 * max(1.0, abs(expected_nis_per_row))
        if abs(nis_per_row - expected_nis_per_row) > bound:
            raise ExtractionError(context + " causal NIS-per-row contradicts Pass-1 NIS")
        frame["reduced_rows_pass1"] = rows
        frame["pass1_nis_per_row"] = nis_per_row
        gate_threshold = float(frame["pass1_gate_threshold"])
        if gate_threshold <= 0.0:
            raise ExtractionError(context + " Pass-1 gate threshold is not positive")
        frame["pass1_max_gate_ratio"] = float(frame["pass1_max_feature_nis"]) / gate_threshold
        frame["pass1_causal_diagnostics_available"] = True

        availability_pairs = (
            ("pass1_reduced_whitened_residual_rms_available", "pass1_reduced_whitened_residual_rms"),
            ("pass1_prior_whitened_correction_norm_available", "pass1_prior_whitened_correction_norm"),
            ("pass1_clone_aggregate_correction_norm_available", "pass1_clone_aggregate_correction_norm"),
            ("pass1_minimum_schur_singular_ratio_available", "pass1_minimum_schur_singular_ratio"),
            ("pass1_median_track_observations_available", "pass1_median_track_observations"),
        )
        for availability_key, value_key in availability_pairs:
            available, value = _available_value(causal_record, availability_key, value_key, context)
            frame[availability_key] = available
            frame[value_key] = value
        imu_available = _bool_flag(causal_record, "pass1_imu_block_norms_available", context, True)
        frame["pass1_imu_block_norms_available"] = bool(imu_available)
        for key in (
            "pass1_orientation_correction_norm", "pass1_position_correction_norm",
            "pass1_velocity_correction_norm", "pass1_gyro_bias_correction_norm",
            "pass1_accelerometer_bias_correction_norm",
        ):
            value = _finite_float(causal_record.get(key), context + " " + key, True)
            frame[key] = value if imu_available else None
    else:
        frame["pass1_causal_diagnostics_available"] = False

    unknown = FORBIDDEN_OUTPUT_COLUMNS.intersection(frame)
    if unknown:
        raise AssertionError("forbidden output fields were retained: %s" % sorted(unknown))
    return frame


def _read_zero_status(path: Path, label: str) -> None:
    if not path.is_file():
        raise ExtractionError("missing %s: %s" % (label, path))
    value = path.read_text(encoding="utf-8").strip()
    if value != "0":
        raise ExtractionError("%s is %r; expected '0'" % (label, value))


def extract_run(run_dir: Path, sequence: str, mode: str) -> List[Dict[str, object]]:
    run_dir = run_dir.expanduser().resolve()
    if not run_dir.is_dir():
        raise ExtractionError("run directory does not exist: %s" % run_dir)
    _read_zero_status(run_dir / "exit_status.txt", "runner exit status")
    _read_zero_status(run_dir / "timed_out.txt", "runner timeout status")
    lines, iterations, causal_records = _read_log_records(run_dir / "stdout.log")
    if not any(re.search(r"max_visual_passes:\s*2(?:\s|$)", line) for line in lines):
        raise ExtractionError("startup log does not assert max_visual_passes: 2")
    if not any(re.search(r"pass2_shadow_only:\s*1(?:\s|$)", line) for line in lines):
        raise ExtractionError("startup log does not assert pass2_shadow_only: 1")

    iteration_groups = _group_by_timestamp(iterations, ITER_MARKER)
    causal_groups = _group_by_timestamp(causal_records, CAUSAL_MARKER)
    extra_causal = sorted(set(causal_groups) - set(iteration_groups))
    if extra_causal:
        raise ExtractionError("causal records have no iteration group: %s" % extra_causal[:3])

    frames: List[Dict[str, object]] = []
    for timestamp, records in iteration_groups.items():
        causal = _optional_single(
            causal_groups.get(timestamp, []),
            "timestamp %.17g causal" % timestamp,
        )
        frames.append(_build_frame(timestamp, records, causal, sequence, mode))
    if not frames:
        raise ExtractionError("no timestamped shadow frames were built")
    _join_timing(frames, _read_timing(run_dir / "timing.csv"))
    return frames


def write_diagnostics(path: Path, frames: Sequence[Mapping[str, object]]) -> str:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError("refusing to overwrite diagnostics CSV: %s" % path)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=OUTPUT_COLUMNS, extrasaction="raise")
        writer.writeheader()
        for frame in frames:
            writer.writerow({column: frame.get(column) for column in OUTPUT_COLUMNS})
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--sequence", required=True)
    parser.add_argument("--mode", default="schur_shadow")
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        frames = extract_run(args.run_dir, args.sequence, args.mode)
        digest = write_diagnostics(args.output, frames)
    except (ExtractionError, FileExistsError, OSError) as error:
        sys.stderr.write("shadow diagnostics extraction failed: %s\n" % error)
        return 2
    summary = {
        "schema": "schurvio_shadow_diagnostics_v1",
        "run_dir": str(args.run_dir.expanduser().resolve()),
        "output": str(args.output.expanduser().resolve()),
        "sequence": args.sequence,
        "mode": args.mode,
        "frames": len(frames),
        "pass2_attempts": sum(int(frame["attempted_passes"]) >= 2 for frame in frames),
        "pass2_valid": sum(bool(frame["pass2_valid"]) for frame in frames),
        "oracle_selected_pass2": sum(frame["oracle_selected_pass"] == 2 for frame in frames),
        "sha256": digest,
    }
    sys.stdout.write(json.dumps(summary, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
