#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Post-authorization CP2 pair-index subprocess.

Success writes only canonical ``pair_index`` JSONL to stdout.  The caller
retains that stream as a command log.  The helper opens the bag through the
parent runner's already-held descriptor and independently joins the original
path and descriptor identities before and after rosbag access.
"""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2 pair-index extractor requires isolated Python (-I)")

import os
from pathlib import Path
import stat
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


POSTAUTH_ENV = "CP2_POSTAUTH_PAIR_INDEX"
POSTAUTH_VALUE = "held-readiness-bound-fd-v1"
IDENTITY_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
SEQUENCES = ("MH_01_easy", "MH_03_medium", "V1_01_easy")


class PairIndexCLIError(RuntimeError):
    """Raised when the post-authorization CLI contract is not exact."""


def _fail(message: str) -> None:
    raise PairIndexCLIError(message)


def _canonical_uint(text: Any, label: str) -> int:
    if not isinstance(text, str) or not text or not text.isascii() or not text.isdecimal():
        _fail(label + " is not an unsigned decimal integer")
    value = int(text, 10)
    if str(value) != text or value > (1 << 64) - 1:
        _fail(label + " is not a canonical u64")
    return value


def _parse_cli(arguments: Sequence[str]) -> Mapping[str, str]:
    expected_flags = (
        "--sequence-index",
        "--sequence-id",
        "--bag-path",
        "--parent-bag-fd",
        "--bag-identity",
    )
    if len(arguments) != 10 or tuple(arguments[0::2]) != expected_flags:
        _fail("arguments differ from the exact post-authorization CLI")
    values = dict(zip(arguments[0::2], arguments[1::2]))
    if any(not isinstance(value, str) or not value or "\0" in value for value in values.values()):
        _fail("an argument value is empty or invalid")
    return values


def _parse_identity(text: str) -> Tuple[int, ...]:
    fields = text.split(":")
    if len(fields) != len(IDENTITY_FIELDS):
        _fail("bag identity has the wrong field population")
    return tuple(
        _canonical_uint(value, "bag identity " + name)
        for name, value in zip(IDENTITY_FIELDS, fields)
    )


def _observed_identity(value: os.stat_result) -> Tuple[int, ...]:
    return tuple(getattr(value, field) for field in IDENTITY_FIELDS)


def _revalidate(
    original_path: Path,
    descriptor: int,
    expected: Tuple[int, ...],
) -> None:
    by_descriptor = os.fstat(descriptor)
    by_path = os.stat(str(original_path), follow_symlinks=False)
    if (
        _observed_identity(by_descriptor) != expected
        or _observed_identity(by_path) != expected
        or not stat.S_ISREG(by_descriptor.st_mode)
        or by_descriptor.st_nlink != 1
    ):
        _fail("held bag descriptor or original path identity changed")


def _extract(
    arguments: Sequence[str],
    environment: Mapping[str, str],
    output: Any,
    *,
    bag_factory: Optional[Callable[..., Any]] = None,
    parent_pid: Optional[int] = None,
) -> None:
    parsed = _parse_cli(arguments)
    if environment.get(POSTAUTH_ENV) != POSTAUTH_VALUE:
        _fail("explicit held-readiness authorization capability is absent")

    sequence_index = _canonical_uint(parsed["--sequence-index"], "sequence index")
    if sequence_index >= len(SEQUENCES) or parsed["--sequence-id"] != SEQUENCES[sequence_index]:
        _fail("sequence identity differs from the frozen inventory")
    bag_path = Path(parsed["--bag-path"])
    bag_text = str(bag_path)
    if (
        not bag_path.is_absolute()
        or os.path.normpath(bag_text) != bag_text
        or bag_text == os.path.sep
    ):
        _fail("bag path is not normalized absolute")
    parent_fd = _canonical_uint(parsed["--parent-bag-fd"], "parent bag descriptor")
    if parent_fd > (1 << 31) - 1:
        _fail("parent bag descriptor is outside the supported range")
    expected = _parse_identity(parsed["--bag-identity"])
    if parent_pid is None:
        parent_pid = os.getppid()
    if isinstance(parent_pid, bool) or not isinstance(parent_pid, int) or parent_pid <= 0:
        _fail("parent process identity is invalid")

    parent_descriptor_path = "/proc/{}/fd/{}".format(parent_pid, parent_fd)
    descriptor = os.open(parent_descriptor_path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        _revalidate(bag_path, descriptor, expected)

        # Importing the execution helper, and therefore the lazy rosbag seam,
        # occurs only after the explicit post-authorization capability and the
        # parent-held descriptor/path join have both passed.
        if bag_factory is None:
            scripts_directory = str(Path(__file__).resolve(strict=True).parent)
            ros_python_directory = "/opt/ros/noetic/lib/python3/dist-packages"
            local_names = (
                "cp2_recorded_campaign", "cp2_schema", "cp2_sequence_actual",
                "cp2_sequence_math", "cp2_sequence_runner",
            )
            if any(name in sys.modules for name in local_names):
                _fail("a postauthorization local module was preloaded")
            sys.path.insert(0, ros_python_directory)
            sys.path.insert(0, scripts_directory)
            import cp2_sequence_actual as actual
            if Path(actual.__file__).resolve(strict=True) != (
                Path(scripts_directory) / "cp2_sequence_actual.py"
            ):
                _fail("pair-index execution helper identity differs")
            try:
                bag_factory = __import__("rosbag").Bag
            except (ImportError, AttributeError) as exc:
                raise PairIndexCLIError("ROS1 rosbag provider is unavailable") from exc
        else:
            # Synthetic tests inject the provider directly and import the same
            # pure extraction helper through their already-bound test module.
            import cp2_sequence_actual as actual
        payload = actual._read_pair_index_with_provider(
            Path("/proc/self/fd/{}".format(descriptor)),
            sequence_index,
            bag_factory,
        )
        _revalidate(bag_path, descriptor, expected)
    finally:
        os.close(descriptor)
    written = output.write(payload)
    if written is not None and written != len(payload):
        _fail("stdout did not accept the complete canonical JSONL payload")
    output.flush()


def main(arguments: Sequence[str]) -> int:
    try:
        _extract(arguments, os.environ, sys.stdout.buffer)
    except BaseException as exc:
        # stdout is deliberately untouched until extraction and every identity
        # recheck have succeeded.  Failure diagnostics are never mixed with
        # the retained canonical JSONL stream.
        sys.stderr.write(
            "cp2_pair_index_extract failed closed: {}: {}\n".format(
                type(exc).__name__, exc
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
