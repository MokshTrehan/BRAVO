#!/usr/bin/python3
"""Export validated captures and deterministically summarize offline comparators.

Numerical comparison is performed by the C++ runner that calls production
OpenVINS/SchurVIO-Lite routines.  This module contains serialization, schema
checks, grouping, and reporting only; it contains no estimator algebra.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import os
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

if __package__:
    from .capture_reader import Matrix, read_capture
else:
    from capture_reader import Matrix, read_capture


INTERCHANGE_MAGIC = b"SCVIOCCSYSTEMS1"
INTERCHANGE_SCHEMA = 1
METHODS = (
    "U_NS",
    "L_NS",
    "GUARDED_NS_DROP",
    "RANK_AWARE_NS",
    "L_SCHUR",
    "FULL_JOINT_ORACLE",
    "FULL_U_NULLSPACE_ORACLE",
)
METHOD_INDEX = {method: index for index, method in enumerate(METHODS)}

REQUIRED_RESULT_COLUMNS = {
    "sequence", "capture_sha256", "record_index", "method", "accepted",
    "finite", "harmful_accepted", "scaled_psd_failure",
    "oracle_safe_opportunity", "unguarded_nullspace_harmful",
    "numerical_rank", "singular_ratio_available", "singular_ratio",
    "observation_count", "track_length", "maximum_parallax_rad",
    "minimum_depth", "camera_model", "supported_subspace_trace",
    "log_pseudodeterminant",
    "half_logdet_identity_plus_information", "effective_rank",
    "correction_norm", "nis", "state_increment_relative_error",
    "posterior_covariance_relative_error", "nis_relative_error",
}


class SummaryError(ValueError):
    """Comparator input/output is incomplete or internally inconsistent."""


def _u64(value: int) -> bytes:
    if not 0 <= value < (1 << 64):
        raise SummaryError(f"u64 value out of range: {value}")
    return struct.pack(">Q", value)


def _i64(value: int) -> bytes:
    if not -(1 << 63) <= value < (1 << 63):
        raise SummaryError(f"i64 value out of range: {value}")
    return struct.pack(">q", value)


def _f64(value: float) -> bytes:
    return struct.pack(">d", value)


def _string(value: str) -> bytes:
    encoded = value.encode("utf-8", errors="strict")
    return _u64(len(encoded)) + encoded


def _matrix(value: Matrix) -> bytes:
    return b"".join(
        (_u64(value.rows), _u64(value.cols),
         b"".join(_f64(item) for item in value.values))
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _capture_spec(value: str) -> Tuple[str, Path]:
    if "=" not in value:
        raise SummaryError("capture must be declared as SEQUENCE=PATH")
    sequence, raw_path = value.split("=", 1)
    if not sequence or any(character in sequence for character in "\r\n"):
        raise SummaryError(f"invalid sequence label: {sequence!r}")
    path = Path(raw_path).expanduser().resolve()
    return sequence, path


def export_interchange(capture_specs: Sequence[str], output: Path) -> int:
    """Validate captures and atomically write deterministic CCSYSTEMS1 bytes."""

    declared = sorted((_capture_spec(spec) for spec in capture_specs), key=lambda item: item[0])
    labels = [label for label, _ in declared]
    if len(labels) != len(set(labels)):
        raise SummaryError("sequence labels must be unique")

    captures = []
    record_count = 0
    for sequence, path in declared:
        capture = read_capture(path)
        captures.append((sequence, path, _file_sha256(path), capture))
        record_count += len(capture.records)

    parts: List[bytes] = [INTERCHANGE_MAGIC, _u64(INTERCHANGE_SCHEMA), _u64(record_count)]
    for sequence, _path, capture_sha256, capture in captures:
        for record in capture.records:
            camera_models = sorted({observation.camera_model for observation in record.observations})
            if len(camera_models) != 1:
                raise SummaryError(
                    f"{sequence} record {record.record_index}: expected one camera-model family, "
                    f"got {camera_models}"
                )
            parts.extend((
                _string(sequence),
                _string(capture_sha256),
                _string(capture.trailer_sha256),
                _string(capture.header.source_commit),
                _string(capture.header.config_sha256),
                _u64(record.record_index),
                _u64(record.update_index),
                _u64(record.feature_ordinal),
                _u64(record.feature_id),
                _u64(len(record.observations)),
                _u64(camera_models[0]),
                _u64(int(record.fej_enabled)),
                _u64(int(record.geometry_valid)),
                _u64(record.calibration_flags),
                _f64(record.update_timestamp),
                _f64(record.minimum_depth),
                _f64(record.maximum_parallax_rad),
                _i64(record.numerical_rank),
                _u64(int(record.singular_ratio_available)),
                _f64(record.singular_ratio),
                _f64(record.sigma_px),
                _matrix(record.h_x),
                _matrix(record.h_f),
                _matrix(record.residual),
                _matrix(record.p_active),
            ))

    payload = b"".join(parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + f".tmp.{os.getpid()}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return record_count


def _bool(row: Mapping[str, str], key: str) -> bool:
    if row[key] not in ("0", "1"):
        raise SummaryError(f"{key} must be 0 or 1, got {row[key]!r}")
    return row[key] == "1"


def _float(row: Mapping[str, str], key: str) -> float:
    try:
        return float(row[key])
    except ValueError as error:
        raise SummaryError(f"{key} is not numeric: {row[key]!r}") from error


def _integer(row: Mapping[str, str], key: str) -> int:
    try:
        return int(row[key])
    except ValueError as error:
        raise SummaryError(f"{key} is not an integer: {row[key]!r}") from error


def _system_key(row: Mapping[str, str]) -> Tuple[str, str, int]:
    return row["sequence"], row["capture_sha256"], _integer(row, "record_index")


def _percent(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{100.0 * numerator / denominator:.2f}%"


def _percentile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return math.nan
    position = probability * (len(ordered) - 1)
    low = int(math.floor(position))
    high = int(math.ceil(position))
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _median(values: Iterable[float]) -> float:
    finite = [value for value in values if math.isfinite(value)]
    return statistics.median(finite) if finite else math.nan


def _fmt(value: float) -> str:
    return "n/a" if not math.isfinite(value) else f"{value:.6g}"


def read_results(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not REQUIRED_RESULT_COLUMNS.issubset(reader.fieldnames):
            missing = sorted(REQUIRED_RESULT_COLUMNS - set(reader.fieldnames or ()))
            raise SummaryError(f"result CSV missing columns: {missing}")
        rows = list(reader)
    seen = set()
    per_system: MutableMapping[Tuple[str, str, int], set] = defaultdict(set)
    for row in rows:
        if row["method"] not in METHOD_INDEX:
            raise SummaryError(f"unknown method {row['method']!r}")
        key = _system_key(row) + (row["method"],)
        if key in seen:
            raise SummaryError(f"duplicate method row: {key}")
        seen.add(key)
        per_system[_system_key(row)].add(row["method"])
    for key, methods in per_system.items():
        if methods != set(METHODS):
            raise SummaryError(f"system {key} has method set {sorted(methods)}")
    rows.sort(key=lambda row: (_system_key(row), METHOD_INDEX[row["method"]]))
    return rows


def read_timing(path: Path) -> List[Dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"sequence", "capture_sha256", "record_index", "method",
                    "offline_end_to_end_median_runtime_ns"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise SummaryError("timing CSV has the wrong schema")
        rows = list(reader)
    rows.sort(key=lambda row: (
        row["sequence"], row["capture_sha256"], int(row["record_index"]),
        METHOD_INDEX.get(row["method"], len(METHODS))))
    return rows


def _safety_counts(rows: Sequence[Mapping[str, str]]) -> Dict[str, object]:
    accepted = [row for row in rows if _bool(row, "accepted")]
    oracle_safe = [row for row in rows if _bool(row, "oracle_safe_opportunity")]
    safe_accepted = [row for row in accepted if not _bool(row, "harmful_accepted")]
    harmful_accepted = [row for row in accepted if _bool(row, "harmful_accepted")]
    oracle_safe_accepted = [row for row in oracle_safe
                            if _bool(row, "accepted") and not _bool(row, "harmful_accepted")]
    oracle_safe_rejected = [row for row in oracle_safe if not _bool(row, "accepted")]
    finite_psd = [row for row in accepted if _bool(row, "finite")
                  and not _bool(row, "scaled_psd_failure")]
    return {
        "total": len(rows), "accepted": len(accepted),
        "rejected": len(rows) - len(accepted),
        "safe_accepted": len(safe_accepted),
        "harmful_accepted": len(harmful_accepted),
        "oracle_safe": len(oracle_safe),
        "oracle_safe_accepted": len(oracle_safe_accepted),
        "oracle_safe_rejected": len(oracle_safe_rejected),
        "finite_psd": len(finite_psd),
        "harmful_acceptance_rate": _percent(len(harmful_accepted), len(accepted)),
        "useful_retention": _percent(len(oracle_safe_accepted), len(oracle_safe)),
        "false_rejection": _percent(len(oracle_safe_rejected), len(oracle_safe)),
        "finite_psd_rate": _percent(len(finite_psd), len(accepted)),
    }


def _guard_counts(rows: Sequence[Mapping[str, str]]) -> Dict[str, object]:
    hazard = [row for row in rows if _bool(row, "unguarded_nullspace_harmful")]
    nonhazard = [row for row in rows if not _bool(row, "unguarded_nullspace_harmful")]
    hazard_rejected = [row for row in hazard if not _bool(row, "accepted")]
    nonhazard_safe_accepted = [row for row in nonhazard if _bool(row, "accepted")
                               and not _bool(row, "harmful_accepted")]
    return {
        "hazard": len(hazard), "hazard_accepted": len(hazard) - len(hazard_rejected),
        "hazard_rejected": len(hazard_rejected), "nonhazard": len(nonhazard),
        "nonhazard_safe_accepted": len(nonhazard_safe_accepted),
        "nonhazard_rejected": sum(not _bool(row, "accepted") for row in nonhazard),
        "harmful_rejection_recall": _percent(len(hazard_rejected), len(hazard)),
        "nonhazard_safe_retention": _percent(len(nonhazard_safe_accepted), len(nonhazard)),
    }


def _ratio_band(row: Mapping[str, str]) -> str:
    if not _bool(row, "singular_ratio_available"):
        return "unavailable"
    value = _float(row, "singular_ratio")
    boundaries = ((1e-12, "[0,1e-12)"), (1e-9, "[1e-12,1e-9)"),
                  (1e-6, "[1e-9,1e-6)"), (1e-4, "[1e-6,1e-4)"),
                  (1e-2, "[1e-4,1e-2)"))
    for upper, label in boundaries:
        if value < upper:
            return label
    return "[1e-2,1]"


def _length_band(value: int) -> str:
    if value <= 4:
        return str(value)
    if value <= 7:
        return "5-7"
    if value <= 15:
        return "8-15"
    return "16+"


def _quantile_label(value: float, boundaries: Sequence[float]) -> str:
    if not math.isfinite(value):
        return "nonfinite"
    index = sum(value > boundary for boundary in boundaries)
    return f"Q{index + 1}"


def write_condition_bins(rows: Sequence[Dict[str, str]], output: Path) -> None:
    representative = { _system_key(row): row for row in rows if row["method"] == "U_NS" }
    parallax_boundaries = [_percentile((_float(row, "maximum_parallax_rad")
                                        for row in representative.values()), probability)
                           for probability in (0.25, 0.5, 0.75)]
    depth_boundaries = [_percentile((_float(row, "minimum_depth")
                                     for row in representative.values()), probability)
                        for probability in (0.25, 0.5, 0.75)]
    grouped: MutableMapping[Tuple[str, str, str], List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        dimensions = (
            ("H_f_rank", str(_integer(row, "numerical_rank"))),
            ("singular_ratio_band", _ratio_band(row)),
            ("observation_count", _length_band(_integer(row, "observation_count"))),
            ("track_length", _length_band(_integer(row, "track_length"))),
            ("parallax_quantile", _quantile_label(
                _float(row, "maximum_parallax_rad"), parallax_boundaries)),
            ("depth_quantile", _quantile_label(
                _float(row, "minimum_depth"), depth_boundaries)),
            ("sequence", row["sequence"]),
            ("camera_model", row["camera_model"]),
        )
        for dimension, label in dimensions:
            grouped[(dimension, label, row["method"])].append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "dimension", "bin", "method", "total", "accepted", "rejected",
        "safe_accepted", "harmful_accepted", "oracle_safe_opportunities",
        "harmful_acceptance_rate",
        "useful_information_retention", "false_rejection_rate",
        "u_ns_hazard_total", "u_ns_hazard_rejected", "harmful_rejection_recall",
        "median_supported_subspace_trace", "median_half_logdet_identity_plus_information",
        "median_log_pseudodeterminant", "median_effective_rank",
        "median_correction_norm", "median_nis",
    )
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for key in sorted(grouped, key=lambda item: (item[0], item[1], METHOD_INDEX[item[2]])):
            selected = grouped[key]
            safety = _safety_counts(selected)
            guard = _guard_counts(selected)
            writer.writerow({
                "dimension": key[0], "bin": key[1], "method": key[2],
                "total": safety["total"], "accepted": safety["accepted"],
                "rejected": safety["rejected"], "safe_accepted": safety["safe_accepted"],
                "harmful_accepted": safety["harmful_accepted"],
                "harmful_acceptance_rate": safety["harmful_acceptance_rate"],
                "oracle_safe_opportunities": safety["oracle_safe"],
                "useful_information_retention": safety["useful_retention"],
                "false_rejection_rate": safety["false_rejection"],
                "u_ns_hazard_total": guard["hazard"],
                "u_ns_hazard_rejected": guard["hazard_rejected"],
                "harmful_rejection_recall": guard["harmful_rejection_recall"],
                "median_supported_subspace_trace": _fmt(_median(
                    _float(row, "supported_subspace_trace") for row in selected
                    if _bool(row, "accepted"))),
                "median_half_logdet_identity_plus_information": _fmt(_median(
                    _float(row, "half_logdet_identity_plus_information") for row in selected
                    if _bool(row, "accepted"))),
                "median_log_pseudodeterminant": _fmt(_median(
                    _float(row, "log_pseudodeterminant") for row in selected
                    if _bool(row, "accepted"))),
                "median_effective_rank": _fmt(_median(
                    float(_integer(row, "effective_rank")) for row in selected
                    if _bool(row, "accepted"))),
                "median_correction_norm": _fmt(_median(
                    _float(row, "correction_norm") for row in selected
                    if _bool(row, "accepted"))),
                "median_nis": _fmt(_median(_float(row, "nis") for row in selected
                                           if _bool(row, "accepted"))),
            })


def write_report(rows: Sequence[Dict[str, str]], timing: Sequence[Dict[str, str]],
                 results_path: Path, timing_path: Path, output: Path) -> None:
    by_method = {method: [row for row in rows if row["method"] == method]
                 for method in METHODS}
    timing_by_method = {method: [float(row["offline_end_to_end_median_runtime_ns"])
                                 for row in timing if row["method"] == method]
                        for method in METHODS}
    system_count = len({_system_key(row) for row in rows})
    boundary_count = len({_system_key(row) for row in rows
                          if _integer(row, "numerical_rank") < 3 or
                          (math.isfinite(_float(row, "singular_ratio")) and
                           _float(row, "singular_ratio") < 1e-6)})
    lines = [
        "# Real camera-conditioning retention and safety report", "",
        f"Validated systems: **{system_count}**; rank/condition-boundary systems: **{boundary_count}**.",
        "", f"Numerical CSV: `{results_path}`", "", f"Separate timing CSV: `{timing_path}`", "",
        "Safety and guard classification are intentionally not combined into one score.", "",
        "## Actual-output safety and oracle-safe retention", "",
        "A harmful accepted output uses the frozen six clauses against FULL_JOINT. "
        "An oracle-safe opportunity requires FULL_JOINT/FULL_U agreement first.", "",
        "| method | total | accept / reject | safe accepted | harmful accepted | harmful / accepted | oracle-safe | useful retention | false rejection | finite+PSD / accepted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for method in METHODS:
        item = _safety_counts(by_method[method])
        lines.append(
            f"| {method} | {item['total']} | {item['accepted']} / {item['rejected']} | "
            f"{item['safe_accepted']} | {item['harmful_accepted']} | {item['harmful_acceptance_rate']} | "
            f"{item['oracle_safe']} | "
            f"{item['useful_retention']} | {item['false_rejection']} | {item['finite_psd_rate']} |"
        )
    lines.extend((
        "", "## Guard classification against fixed U_NS hazard label", "",
        "The hazard label is whether exact unguarded U_NS is harmful versus FULL_JOINT. "
        "This table measures guard discrimination; it does not relabel safe rank-aware outputs as harmful.", "",
        "| method | U_NS-hazard | hazard accepted / rejected | nonhazard | nonhazard safe accepted / rejected | harmful-rejection recall | nonhazard safe retention |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ))
    for method in METHODS:
        item = _guard_counts(by_method[method])
        lines.append(
            f"| {method} | {item['hazard']} | {item['hazard_accepted']} / {item['hazard_rejected']} | "
            f"{item['nonhazard']} | {item['nonhazard_safe_accepted']} / {item['nonhazard_rejected']} | "
            f"{item['harmful_rejection_recall']} | {item['nonhazard_safe_retention']} |"
        )
    lines.extend((
        "", "## Retained-information diagnostics (accepted outputs)", "",
        "| method | supported trace median | log-pdet median | 0.5 logdet(I+J) median | effective rank median | correction norm median | NIS median |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ))
    for method in METHODS:
        selected = [row for row in by_method[method] if _bool(row, "accepted")]
        lines.append(
            f"| {method} | {_fmt(_median(_float(row, 'supported_subspace_trace') for row in selected))} | "
            f"{_fmt(_median(_float(row, 'log_pseudodeterminant') for row in selected))} | "
            f"{_fmt(_median(_float(row, 'half_logdet_identity_plus_information') for row in selected))} | "
            f"{_fmt(_median(float(_integer(row, 'effective_rank')) for row in selected))} | "
            f"{_fmt(_median(_float(row, 'correction_norm') for row in selected))} | "
            f"{_fmt(_median(_float(row, 'nis') for row in selected))} |"
        )
    lines.extend(("", "## Offline comparator timing (separate, non-deterministic observations)", "",
                  "| method | samples | median / p95 us |", "|---|---:|---:|"))
    for method in METHODS:
        values = timing_by_method[method]
        lines.append(f"| {method} | {len(values)} | {_fmt(_median(values) / 1000.0)} / "
                     f"{_fmt(_percentile(values, 0.95) / 1000.0)} |")
    lines.extend((
        "", "## Metric definitions", "",
        "- Useful-information retention: safely accepted outputs divided by all FULL_JOINT/FULL_U-agreed oracle-safe opportunities.",
        "- Harmful-rejection recall: rejected systems divided by systems where fixed U_NS is harmful versus FULL_JOINT.",
        "- Supported trace and `0.5*logdet(I+J)` use the frozen PSD prior support; log-pseudodeterminant, effective rank, correction norm, and NIS remain separate columns.",
        "- Runtime is offline end-to-end comparator runtime (including prior support/posterior diagnostics), not estimator latency.",
        "",
    ))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")


def summarize(results_path: Path, timing_path: Path, bins_path: Path,
              report_path: Path) -> int:
    rows = read_results(results_path)
    timing = read_timing(timing_path)
    write_condition_bins(rows, bins_path)
    write_report(rows, timing, results_path, timing_path, report_path)
    return len({_system_key(row) for row in rows})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="validate capture(s) and export CCSYSTEMS1")
    export.add_argument("--capture", action="append", required=True, metavar="SEQUENCE=PATH")
    export.add_argument("--output", required=True, type=Path)
    summary = commands.add_parser("summarize", help="validate and summarize comparator CSVs")
    summary.add_argument("--results", required=True, type=Path)
    summary.add_argument("--timing", required=True, type=Path)
    summary.add_argument("--condition-bins", required=True, type=Path)
    summary.add_argument("--report", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            count = export_interchange(args.capture, args.output)
            print(f"validated capture export complete records={count} output={args.output}")
        else:
            count = summarize(args.results, args.timing, args.condition_bins, args.report)
            print(f"conditioning summary complete systems={count} report={args.report}")
        return 0
    except (OSError, SummaryError, ValueError) as error:
        print(f"conditioning analysis failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
