#!/usr/bin/python3
"""Strictly convert OpenVINS total-state rows to the eight-column TUM format."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import tempfile


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Reorder timestamp/quaternion/position columns from an OpenVINS "
            "total-state log into a TUM trajectory."
        )
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    return parser.parse_args()


def convert(source: Path, destination: Path) -> int:
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination must differ")
    if not source.is_file():
        raise ValueError(f"source is not a regular file: {source}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    previous_timestamp = -math.inf
    rows = 0
    saw_total_state_header = False
    temporary_name: str | None = None

    try:
        with source.open("r", encoding="utf-8") as input_stream, tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as output_stream:
            temporary_name = output_stream.name
            output_stream.write("# timestamp tx ty tz qx qy qz qw\n")

            for line_number, raw_line in enumerate(input_stream, start=1):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                if stripped.startswith("#"):
                    if stripped.startswith("# timestamp(s) q p v bg ba cam_imu_dt num_cam"):
                        saw_total_state_header = True
                    continue
                fields = stripped.split()
                if len(fields) < 8:
                    raise ValueError(
                        f"{source}:{line_number}: expected at least 8 columns, got {len(fields)}"
                    )
                try:
                    values = [float(field) for field in fields[:8]]
                except ValueError as exc:
                    raise ValueError(
                        f"{source}:{line_number}: non-numeric trajectory value"
                    ) from exc
                if not all(math.isfinite(value) for value in values):
                    raise ValueError(f"{source}:{line_number}: non-finite trajectory value")

                timestamp = values[0]
                if timestamp <= previous_timestamp:
                    raise ValueError(
                        f"{source}:{line_number}: timestamps are not strictly increasing"
                    )
                quaternion_norm = math.sqrt(sum(value * value for value in values[1:5]))
                if not 0.999 <= quaternion_norm <= 1.001:
                    raise ValueError(
                        f"{source}:{line_number}: quaternion norm {quaternion_norm:.9g} is invalid"
                    )

                # Total state is [t,qx,qy,qz,qw,px,py,pz,...]. Preserve the
                # text exactly while reordering to TUM [t,px,py,pz,qx,qy,qz,qw].
                output_stream.write(
                    " ".join(
                        [fields[0], fields[5], fields[6], fields[7], fields[1], fields[2], fields[3], fields[4]]
                    )
                    + "\n"
                )
                previous_timestamp = timestamp
                rows += 1

            if rows == 0:
                raise ValueError(f"source contains no trajectory rows: {source}")
            if not saw_total_state_header:
                raise ValueError(f"source lacks the OpenVINS total-state header: {source}")
            output_stream.flush()
            os.fsync(output_stream.fileno())

        os.replace(temporary_name, destination)
        temporary_name = None
        return rows
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def main() -> int:
    args = parse_args()
    try:
        rows = convert(args.source, args.destination)
    except (OSError, ValueError) as exc:
        print(f"trajectory conversion failed: {exc}", file=sys.stderr)
        return 1
    print(f"converted {rows} poses to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
