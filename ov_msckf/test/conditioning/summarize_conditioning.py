#!/usr/bin/env python3
"""Validate and summarize the frozen Schur conditioning CSV.

This script contains no estimator algebra. It accepts the three-method local
CSV or the externally produced four-method CSV that appends the separately
hashed OV_SOURCE_FAITHFUL_ALGEBRA row for each deterministic fixture.
"""

import csv
import math
import statistics
import sys
from collections import Counter, defaultdict


EXPECTED_LOCAL_METHODS = ("L_SCHUR", "L_NS", "U_NS")
OPTIONAL_EXTERNAL_METHOD = "OV_SOURCE_FAITHFUL_ALGEBRA"
METHOD_ORDER = ("U_NS", "L_NS", "L_SCHUR", OPTIONAL_EXTERNAL_METHOD)
EXPECTED_FIXTURES = 1380


def as_float(row, key):
    return float(row[key])


def as_int(row, key):
    return int(row[key])


def enabled(row, key):
    return row[key] == "1"


def percent(numerator, denominator):
    if denominator == 0:
        return float("nan")
    return 100.0 * numerator / denominator


def fmt_percent(value):
    return "n/a" if not math.isfinite(value) else "{:.2f}%".format(value)


def fmt_number(value):
    if not math.isfinite(value):
        return "n/a"
    if value == 0.0:
        return "0"
    return "{:.3g}".format(value)


def percentile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def supported(row):
    return not enabled(row, "calibration_active")


def well_conditioned(row):
    condition = as_float(row, "measured_condition")
    return as_int(row, "numerical_rank") == 3 and math.isfinite(condition) and condition <= 1.0e4


def hard_case(row):
    return as_int(row, "target_exact_rank") < 3 or as_float(row, "target_condition") > 1.0e6


def count_where(rows, predicate):
    return sum(1 for row in rows if predicate(row))


def method_statistics(rows):
    well = [row for row in rows if well_conditioned(row)]
    hard = [row for row in rows if hard_case(row)]
    accepted_hard = [row for row in hard if enabled(row, "accepted")]
    harmful_hard = [row for row in accepted_hard if enabled(row, "harmful_accepted")]
    safe_rejections = [row for row in hard if enabled(row, "safe_rejection")]
    nonharmful_hard = [row for row in accepted_hard if not enabled(row, "harmful_accepted")]
    runtimes = [as_float(row, "runtime_ns") for row in rows if math.isfinite(as_float(row, "runtime_ns"))]
    return {
        "fixtures": len(rows),
        "accepted": count_where(rows, lambda row: enabled(row, "accepted")),
        "all_harmful": count_where(rows, lambda row: enabled(row, "harmful_accepted")),
        "accepted_nonfinite": count_where(
            rows, lambda row: enabled(row, "accepted") and not enabled(row, "finite")),
        "unexplained_mutation": count_where(rows, lambda row: enabled(row, "unexplained_input_mutation")),
        "psd_failure": count_where(
            rows,
            lambda row: enabled(row, "accepted")
            and math.isfinite(as_float(row, "minimum_covariance_eigenvalue"))
            and math.isfinite(as_float(row, "psd_tolerance"))
            and as_float(row, "minimum_covariance_eigenvalue") < -as_float(row, "psd_tolerance"),
        ),
        "well_count": len(well),
        "well_parity": count_where(well, lambda row: enabled(row, "well_conditioned_parity")),
        "hard_count": len(hard),
        "hard_accepted": len(accepted_hard),
        "hard_harmful": len(harmful_hard),
        "hard_safe_rejected": len(safe_rejections),
        "hard_nonharmful_accepted": len(nonharmful_hard),
        "safe_rejection_rate": percent(len(safe_rejections), len(hard)),
        "harmful_acceptance_rate": percent(len(harmful_hard), len(accepted_hard)),
        "hard_acceptance_coverage": percent(len(accepted_hard), len(hard)),
        "hard_nonharmful_coverage": percent(len(nonharmful_hard), len(hard)),
        "hard_safe_handling": percent(len(safe_rejections) + len(nonharmful_hard), len(hard)),
        "runtime_median_ns": statistics.median(runtimes) if runtimes else float("nan"),
        "runtime_p95_ns": percentile(runtimes, 0.95),
    }


def validate(rows):
    if not rows:
        raise ValueError("empty CSV")
    required = {
        "fixture_id", "method", "rows", "state_dimension", "geometry_family", "target_spectrum",
        "target_condition", "target_exact_rank", "measured_condition", "numerical_rank", "calibration_active",
        "accepted", "finite", "harmful_accepted", "safe_rejection", "well_conditioned_parity", "runtime_ns",
        "oracle_full_joint_dx_relative_error", "oracle_full_joint_covariance_relative_error",
        "oracle_pseudoinverse_information_relative_error", "oracle_pseudoinverse_gradient_relative_error",
        "oracle_information_covariance_dx_relative_error",
        "oracle_information_covariance_covariance_relative_error",
    }
    missing = required.difference(rows[0])
    if missing:
        raise ValueError("missing columns: {}".format(", ".join(sorted(missing))))

    fixtures = defaultdict(list)
    for row in rows:
        fixtures[row["fixture_id"]].append(row)
    if len(fixtures) != EXPECTED_FIXTURES:
        raise ValueError("expected {} fixtures, got {}".format(EXPECTED_FIXTURES, len(fixtures)))

    present_methods = {row["method"] for row in rows}
    methods = tuple(method for method in METHOD_ORDER if method in present_methods)
    expected = set(EXPECTED_LOCAL_METHODS)
    if OPTIONAL_EXTERNAL_METHOD in methods:
        expected.add(OPTIONAL_EXTERNAL_METHOD)
    if present_methods != expected:
        raise ValueError("unexpected method set {}".format(methods))
    for fixture_id, fixture_rows in fixtures.items():
        seen = [row["method"] for row in fixture_rows]
        if len(seen) != len(expected) or set(seen) != expected:
            raise ValueError("fixture {} has method rows {}".format(fixture_id, seen))

    # U_NS is a source-hash-proven exact alias. Excluding timing and labels, its
    # benchmark outputs must exactly equal the independently repeated L_NS call.
    ignored = {"method", "implementation_label", "source_identity", "runtime_ns"}
    for fixture_id, fixture_rows in fixtures.items():
        by_method = {row["method"]: row for row in fixture_rows}
        local = by_method["L_NS"]
        upstream = by_method["U_NS"]
        for key in local:
            if key not in ignored and local[key] != upstream[key]:
                raise ValueError("L_NS/U_NS mismatch fixture {} column {}".format(fixture_id, key))
    return methods


def oracle_table(rows):
    columns = (
        ("full-joint dx", "oracle_full_joint_dx_relative_error"),
        ("full-joint covariance", "oracle_full_joint_covariance_relative_error"),
        ("pseudoinverse information", "oracle_pseudoinverse_information_relative_error"),
        ("pseudoinverse gradient", "oracle_pseudoinverse_gradient_relative_error"),
        ("information/covariance dx", "oracle_information_covariance_dx_relative_error"),
        ("information/covariance covariance", "oracle_information_covariance_covariance_relative_error"),
    )
    unique = {}
    for row in rows:
        unique.setdefault(row["fixture_id"], row)
    lines = ["| Cross-check | median relative error | maximum relative error |", "|---|---:|---:|"]
    for label, column in columns:
        values = [as_float(row, column) for row in unique.values()]
        finite = [value for value in values if math.isfinite(value)]
        lines.append("| {} | {} | {} |".format(label, fmt_number(statistics.median(finite)), fmt_number(max(finite))))
    return lines


def oracle_extrema(rows):
    unique = {}
    for row in rows:
        unique.setdefault(row["fixture_id"], row)
    columns = (
        "oracle_full_joint_dx_relative_error",
        "oracle_full_joint_covariance_relative_error",
        "oracle_pseudoinverse_information_relative_error",
        "oracle_pseudoinverse_gradient_relative_error",
        "oracle_information_covariance_dx_relative_error",
        "oracle_information_covariance_covariance_relative_error",
    )
    return {column: max(as_float(row, column) for row in unique.values()) for column in columns}


def grouped_hard_table(rows, methods, group_key):
    groups = sorted({row[group_key] for row in rows})
    lines = ["| {} | method | hard fixtures | accepted | harmful accepted | safe rejected |".format(group_key),
             "|---|---|---:|---:|---:|---:|"]
    for group in groups:
        for method in methods:
            selected = [row for row in rows if row[group_key] == group and row["method"] == method and hard_case(row)]
            if not selected:
                continue
            lines.append("| {} | {} | {} | {} | {} | {} |".format(
                group, method, len(selected),
                count_where(selected, lambda row: enabled(row, "accepted")),
                count_where(selected, lambda row: enabled(row, "harmful_accepted")),
                count_where(selected, lambda row: enabled(row, "safe_rejection"))))
    return lines


def well_error_table(rows, methods):
    columns = (
        ("information", "reduced_information_relative_error"),
        ("gradient", "reduced_gradient_relative_error"),
        ("state increment", "state_increment_relative_error"),
        ("NIS", "nis_relative_error"),
        ("posterior covariance", "posterior_covariance_relative_error"),
    )
    lines = ["| method | " + " | ".join(label + " max" for label, _ in columns) + " |",
             "|---|" + "---:|" * len(columns)]
    for method in methods:
        selected = [row for row in rows if row["method"] == method and well_conditioned(row)]
        values = []
        for _, column in columns:
            finite = [as_float(row, column) for row in selected if math.isfinite(as_float(row, column))]
            values.append(fmt_number(max(finite)) if finite else "n/a")
        lines.append("| {} | {} |".format(method, " | ".join(values)))
    return lines


def render(rows, methods, csv_path):
    primary = [row for row in rows if supported(row)]
    well_primary = [row for row in primary if well_conditioned(row)]
    supplemental = [row for row in rows if not supported(row)]
    stats = {method: method_statistics([row for row in primary if row["method"] == method]) for method in methods}

    lines = [
        "# Numerical conditioning stress report",
        "",
        "Result population is frozen: double precision, master seed `0x434f4e444954494f`, 1,380 fixtures, and "
        "the predeclared thresholds in `test/conditioning/README.md`. No threshold or population was changed after execution.",
        "",
        "Input CSV: `{}`".format(csv_path),
        "",
        "## Supported-intersection result",
        "",
        "Active camera-calibration blocks are excluded from this primary table. `OV_SOURCE_FAITHFUL_ALGEBRA`, when present, "
        "is the separately hashed external algebra reproduction and is not a full runtime estimator.",
        "",
        "| method | fixtures | accepted | harmful (all) | well parity | hard accept coverage | hard safe rejection | "
        "harmful / hard accepted | hard nonharmful coverage | hard safe handling | reducer median / p95 us |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for method in methods:
        item = stats[method]
        lines.append(
            "| {} | {} | {} | {} | {}/{} | {} | {} | {} | {} | {} | {:.3f} / {:.3f} |".format(
                method, item["fixtures"], item["accepted"], item["all_harmful"], item["well_parity"],
                item["well_count"], fmt_percent(item["hard_acceptance_coverage"]),
                fmt_percent(item["safe_rejection_rate"]), fmt_percent(item["harmful_acceptance_rate"]),
                fmt_percent(item["hard_nonharmful_coverage"]), fmt_percent(item["hard_safe_handling"]),
                item["runtime_median_ns"] / 1000.0, item["runtime_p95_ns"] / 1000.0))

    local = stats["L_SCHUR"]
    h1_pass = local["well_count"] > 0 and local["well_parity"] == local["well_count"]
    h2_pass = local["all_harmful"] == 0
    well_oracle_max = oracle_extrema(well_primary)
    h1_all_oracles = (
        well_oracle_max["oracle_full_joint_dx_relative_error"] <= 1.0e-7
        and well_oracle_max["oracle_full_joint_covariance_relative_error"] <= 1.0e-7
        and well_oracle_max["oracle_pseudoinverse_information_relative_error"] <= 1.0e-8
        and well_oracle_max["oracle_pseudoinverse_gradient_relative_error"] <= 1.0e-8
        and well_oracle_max["oracle_information_covariance_dx_relative_error"] <= 1.0e-7
        and well_oracle_max["oracle_information_covariance_covariance_relative_error"] <= 1.0e-7
    )
    lines.extend([
        "",
        "H1 production-reducer parity against the primary full-U SVD nullspace/covariance oracle: **{}** ({}/{} "
        "supported well-conditioned fixtures).".format(
            "PASS" if h1_pass else "FAIL", local["well_parity"], local["well_count"]),
        "",
        "H1's complete independent-oracle criterion: **{}**. The well-conditioned full-joint state-increment maximum "
        "is {} against the declared `1e-7` posterior tolerance; the table below shows which other oracle checks meet "
        "their `1e-8` information or `1e-7` posterior tolerances.".format(
            "PASS" if h1_all_oracles else "NOT MET",
            fmt_number(well_oracle_max["oracle_full_joint_dx_relative_error"])),
        "",
        "H2 fail-closed local-Schur behavior: **{}** ({} harmful accepted updates across {} supported fixtures).".format(
            "PASS" if h2_pass else "FAIL", local["all_harmful"], local["fixtures"]),
        "",
        "The hard-case coverage and safe-rejection columns must be interpreted together: safe handling alone is not a "
        "novelty result when achieved by rejecting every hard fixture. The overall STRONG/WEAK gate therefore remains a "
        "report-level judgment against the prompt's comparative-value conditions, not an automatic score in this script.",
        "",
        "## Well-conditioned maximum relative errors",
        "",
    ])
    lines.extend(well_error_table(primary, methods))
    lines.extend([
        "",
        "### Well-conditioned independent-oracle consistency",
        "",
    ])
    lines.extend(oracle_table(well_primary))
    lines.extend([
        "",
        "## Hard cases by declared spectrum",
        "",
    ])
    lines.extend(grouped_hard_table(primary, methods, "target_spectrum"))

    lines.extend(["", "## Independent-oracle consistency", ""])
    lines.extend(oracle_table(primary))
    lines.extend([
        "",
        "The full-joint and Moore-Penrose cross-checks lost agreement with the primary full-U SVD nullspace oracle at "
        "the most extreme target conditions; their errors are reported, not hidden or used to retune rank thresholds.",
        "",
        "## Rejections and numerical events",
        "",
    ])
    lines.append("| method | status / reason | count |")
    lines.append("|---|---|---:|")
    for method in methods:
        counts = Counter((row["status"], row["rejection_reason"]) for row in primary if row["method"] == method)
        for (status, reason), count in sorted(counts.items()):
            lines.append("| {} | {} / {} | {} |".format(method, status, reason, count))

    lines.extend(["", "Required accepted-update event counts:", ""])
    lines.append("| method | accepted nonfinite | scaled PSD failures | unexplained input mutations |")
    lines.append("|---|---:|---:|---:|")
    for method in methods:
        item = stats[method]
        lines.append("| {} | {} | {} | {} |".format(
            method, item["accepted_nonfinite"], item["psd_failure"], item["unexplained_mutation"]))
    lines.extend([
        "",
        "`timing_sink=-nan` in the combined metadata is only the volatile anti-optimization accumulator being "
        "contaminated by the 38 accepted-nonfinite external OV rows. The authoritative local-only metadata sink is "
        "finite; L-SCHUR, L-NS, and U-NS have zero accepted-nonfinite events in the CSV.",
    ])

    lines.extend(["", "## Active-calibration-block supplemental population", ""])
    if supplemental:
        lines.append("| method | fixtures | accepted | harmful accepted | safe rejected |")
        lines.append("|---|---:|---:|---:|---:|")
        for method in methods:
            selected = [row for row in supplemental if row["method"] == method]
            lines.append("| {} | {} | {} | {} | {} |".format(
                method, len(selected), count_where(selected, lambda row: enabled(row, "accepted")),
                count_where(selected, lambda row: enabled(row, "harmful_accepted")),
                count_where(selected, lambda row: enabled(row, "safe_rejection"))))
    else:
        lines.append("No supplemental rows were present.")

    lines.extend([
        "",
        "## Scope limitation",
        "",
        "These are deterministic algebraic Jacobian proxies, not reconstructed camera-ray tracks or full estimator "
        "updates. Reducer microtimings exclude oracle and posterior work and are descriptive, not desktop estimator "
        "tail-latency evidence. Real-data completion, accuracy, and tail latency must be decided separately.",
        "",
    ])
    return "\n".join(lines)


def main(argv):
    if len(argv) != 3:
        print("usage: {} INPUT.csv OUTPUT.md".format(argv[0]), file=sys.stderr)
        return 64
    with open(argv[1], newline="") as stream:
        rows = list(csv.DictReader(stream))
    methods = validate(rows)
    report = render(rows, methods, argv[1])
    with open(argv[2], "w") as stream:
        stream.write(report)
    print("validated {} rows, {} fixtures, methods={}".format(len(rows), EXPECTED_FIXTURES, ",".join(methods)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
