#!/usr/bin/env python3
"""Validate and display the D0 external-dependency gate."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment diagnostic
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


DEFAULT_REGISTRY = Path(__file__).with_name("dependencies.yaml")
TARGET_FIELDS = (
    "board_model",
    "board_os",
    "toolchain",
    "access_method",
    "available_cpu_cores",
    "power_reading_interface",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help="D0 dependency YAML registry",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero until every D0 dependency passes",
    )
    return parser.parse_args()


def load_registry(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            registry = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(registry, dict):
        raise ValueError("registry root must be a mapping")
    if registry.get("gate", {}).get("id") != "D0":
        raise ValueError("registry gate.id must be D0")
    if not isinstance(registry.get("dependencies"), dict):
        raise ValueError("registry must contain a dependencies mapping")
    return registry


def nonempty(mapping: dict, field: str) -> bool:
    value = mapping.get(field)
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value is not None


def evaluate(registry: dict) -> list[tuple[str, bool, str]]:
    dependencies = registry["dependencies"]
    missing = [
        name
        for name in (
            "dataset_readiness",
            "target_board_and_power",
            "mathematical_reviewer",
            "gplv3_derivative_acceptance",
        )
        if not isinstance(dependencies.get(name), dict)
    ]
    if missing:
        raise ValueError("missing dependency mappings: " + ", ".join(missing))

    dataset = dependencies["dataset_readiness"]
    dataset_ok = (
        dataset.get("status") == "pass"
        and dataset.get("euroc_local_sequences_verified") == 11
        and nonempty(dataset, "second_dataset")
    )

    target = dependencies["target_board_and_power"]
    missing_target_fields = [field for field in TARGET_FIELDS if not nonempty(target, field)]
    target_ok = target.get("status") == "pass" and not missing_target_fields
    target_detail = (
        "complete"
        if not missing_target_fields
        else "missing " + ", ".join(missing_target_fields)
    )

    reviewer = dependencies["mathematical_reviewer"]
    reviewer_ok = reviewer.get("status") == "pass" and nonempty(reviewer, "reviewer")
    reviewer_detail = (
        f"reviewer={reviewer.get('reviewer')}" if nonempty(reviewer, "reviewer") else "reviewer unassigned"
    )

    license_decision = dependencies["gplv3_derivative_acceptance"]
    license_ok = (
        license_decision.get("status") == "pass"
        and license_decision.get("accepted") is True
    )
    license_detail = (
        "accepted=true" if license_decision.get("accepted") is True else "author acceptance missing"
    )

    return [
        ("dataset_readiness", dataset_ok, f"status={dataset.get('status')}"),
        ("target_board_and_power", target_ok, target_detail),
        ("mathematical_reviewer", reviewer_ok, reviewer_detail),
        ("gplv3_derivative_acceptance", license_ok, license_detail),
    ]


def main() -> int:
    args = parse_args()
    try:
        registry = load_registry(args.registry)
        results = evaluate(registry)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"D0 registry error: {exc}", file=sys.stderr)
        return 2

    print(f"D0 external dependencies ({registry.get('as_of', 'unknown time')})")
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'OPEN':<5} {name:<32} {detail}")

    all_passed = all(passed for _, passed, _ in results)
    declared_status = registry["gate"].get("overall_status")
    if all_passed and declared_status != "pass":
        print("D0 registry error: all dependencies pass but gate.overall_status is not pass", file=sys.stderr)
        return 2
    if not all_passed and declared_status == "pass":
        print("D0 registry error: gate is declared pass with unresolved dependencies", file=sys.stderr)
        return 2
    if args.strict and not all_passed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
