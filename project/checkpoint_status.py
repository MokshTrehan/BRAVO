#!/usr/bin/env python3
"""Validate and display the schedule registry; this is not a gate oracle."""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment diagnostic
    raise SystemExit("PyYAML is required: python3 -m pip install PyYAML") from exc


ALLOWED_STATUSES = {"pending", "in_progress", "passed", "failed", "blocked"}
EXPECTED_IDS = ["D0"] + [f"CP{index}" for index in range(14)]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--registry",
        type=Path,
        default=Path(__file__).with_name("checkpoints.yaml"),
        help="checkpoint YAML registry",
    )
    parser.add_argument(
        "--at",
        help="ISO-8601 evaluation time; defaults to now in the registry timezone",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit nonzero for an overdue unresolved or explicitly failed gate",
    )
    return parser.parse_args()


def load_registry(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as stream:
            registry = yaml.safe_load(stream)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML: {exc}") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("checkpoints"), list):
        raise ValueError("registry must contain a checkpoints list")
    return registry


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"deadline lacks UTC offset: {value}")
    return parsed


def format_remaining(delta: dt.timedelta) -> str:
    seconds = int(delta.total_seconds())
    prefix = "in" if seconds >= 0 else "overdue by"
    seconds = abs(seconds)
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes = seconds // 60
    return f"{prefix} {days}d {hours:02d}h {minutes:02d}m"


def main() -> int:
    args = parse_args()
    try:
        registry = load_registry(args.registry)
        timezone = ZoneInfo(registry["timezone"])
        now = parse_time(args.at).astimezone(timezone) if args.at else dt.datetime.now(timezone)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        print(f"checkpoint registry error: {exc}", file=sys.stderr)
        return 2

    seen: set[str] = set()
    observed_ids: list[str] = []
    invalid: list[str] = []
    strict_failures: list[str] = []

    print(f"SchurVIO-Lite checkpoints at {now.isoformat(timespec='minutes')}")
    print(f"{'ID':<6} {'STATUS':<12} {'DUE':<22} REMAINING")

    for checkpoint in registry["checkpoints"]:
        if not isinstance(checkpoint, dict):
            invalid.append(f"checkpoint entry is not a mapping: {checkpoint!r}")
            continue
        try:
            checkpoint_id = str(checkpoint["id"])
            status = str(checkpoint["status"])
            due = parse_time(str(checkpoint["due"])).astimezone(timezone)
            evidence = checkpoint.get("evidence", [])
        except (KeyError, ValueError) as exc:
            invalid.append(f"malformed checkpoint {checkpoint!r}: {exc}")
            continue

        if checkpoint_id in seen:
            invalid.append(f"duplicate checkpoint id: {checkpoint_id}")
        seen.add(checkpoint_id)
        observed_ids.append(checkpoint_id)
        if status not in ALLOWED_STATUSES:
            invalid.append(f"{checkpoint_id}: invalid status {status!r}")
        if not isinstance(evidence, list):
            invalid.append(f"{checkpoint_id}: evidence must be a list")
        if status == "passed" and not evidence:
            invalid.append(f"{checkpoint_id}: passed gate has no evidence")

        unresolved_overdue = due < now and status in {"pending", "in_progress", "blocked"}
        if unresolved_overdue:
            strict_failures.append(f"{checkpoint_id}: overdue with status {status}")
        if status == "failed":
            strict_failures.append(f"{checkpoint_id}: explicitly failed")

        print(
            f"{checkpoint_id:<6} {status:<12} "
            f"{due.isoformat(timespec='minutes'):<22} {format_remaining(due - now)}"
        )

    if observed_ids != EXPECTED_IDS:
        invalid.append(
            "checkpoint ids/order must be "
            + ", ".join(EXPECTED_IDS)
            + "; got "
            + ", ".join(observed_ids)
        )

    for problem in invalid:
        print(f"ERROR: {problem}", file=sys.stderr)
    if args.strict:
        for problem in strict_failures:
            print(f"GATE: {problem}", file=sys.stderr)

    if invalid:
        return 2
    if args.strict and strict_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
