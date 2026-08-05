#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stdlib-only binding for the capsule-local CP2 x86 FP control probe."""

from __future__ import annotations

import ctypes
from pathlib import Path
import stat


EXPECTED_MXCSR = 0x00001F80
EXPECTED_X87_CW = 0x027F
PERMITTED_MXCSR_STATUS = 0x00000032
PERMITTED_X87_STATUS = 0x0032
CAPSULE_NATIVE = False


class FloatingPointControlError(RuntimeError):
    """Raised when the capsule process does not retain its frozen FP state."""


def _library_path() -> Path:
    module = Path(__file__).resolve(strict=True)
    try:
        capsule_root = module.parents[4]
    except IndexError as exc:
        raise FloatingPointControlError("FP probe module is outside capsule layout") from exc
    library = capsule_root / "native" / "libcp2_fp_control.so"
    try:
        status = library.lstat()
    except OSError as exc:
        raise FloatingPointControlError("capsule FP probe library is absent") from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or stat.S_IMODE(status.st_mode) != 0o444
    ):
        raise FloatingPointControlError("capsule FP probe library identity differs")
    return library


def _probe(symbol: str) -> tuple[int, int, int]:
    try:
        library = ctypes.CDLL(str(_library_path()), mode=ctypes.RTLD_LOCAL)
        function = getattr(library, symbol)
        function.argtypes = (
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint16),
            ctypes.POINTER(ctypes.c_uint16),
        )
        function.restype = ctypes.c_int
        mxcsr = ctypes.c_uint32()
        x87_cw = ctypes.c_uint16()
        x87_sw = ctypes.c_uint16()
        result = function(
            ctypes.byref(mxcsr), ctypes.byref(x87_cw), ctypes.byref(x87_sw)
        )
    except (AttributeError, OSError) as exc:
        raise FloatingPointControlError("capsule FP probe cannot be loaded") from exc
    if result != 0:
        raise FloatingPointControlError(
            "floating-point controls differ: MXCSR=0x{:08x}, "
            "X87_CW=0x{:04x}, X87_SW=0x{:04x}".format(
                mxcsr.value, x87_cw.value, x87_sw.value
            )
        )
    return mxcsr.value, x87_cw.value, x87_sw.value


def establish() -> tuple[int, int, int]:
    """Set and verify the frozen controls before numerical imports."""

    return _probe("cp2_fp_establish")


def verify() -> tuple[int, int, int]:
    """Verify controls and normalize only sticky exception-status bits."""

    return _probe("cp2_fp_verify")


def openblas_threads(_path: str) -> int:
    """The native capsule module replaces this construction-time stub."""

    raise FloatingPointControlError("native OpenBLAS thread probe is unavailable")


def openblas_identity(_path: str) -> dict[str, object]:
    """The native capsule module replaces this construction-time stub."""

    raise FloatingPointControlError("native OpenBLAS identity probe is unavailable")


def x86_cpuid_identity() -> dict[str, object]:
    """The native capsule module replaces this construction-time stub."""

    raise FloatingPointControlError("native CPUID identity probe is unavailable")


__all__ = [
    "EXPECTED_MXCSR",
    "EXPECTED_X87_CW",
    "PERMITTED_MXCSR_STATUS",
    "PERMITTED_X87_STATUS",
    "CAPSULE_NATIVE",
    "FloatingPointControlError",
    "establish",
    "verify",
    "openblas_threads",
    "openblas_identity",
    "x86_cpuid_identity",
]
