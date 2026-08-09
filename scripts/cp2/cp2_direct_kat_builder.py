#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generate the data-free CP2-D KAT expectation inside its exact capsule.

This module is a construction-only surface.  It creates the frozen five-case
synthetic request directly from the reviewed codec and writes that request, a
new expectation, and the independently parseable observed response into one
caller-owned private directory.  It cannot execute a sequence request and is
not retained in the finished runtime capsule.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Sequence

import cp2_fp_control as fp_control


class DirectKatBuildError(RuntimeError):
    """Raised whenever the construction-only KAT boundary differs."""


def _fail(message: str) -> None:
    raise DirectKatBuildError(message)


def _capsule_root() -> Path:
    module = Path(__file__).resolve(strict=True)
    if (
        module.parent.name != "python3.11"
        or module.parent.parent.name != "lib"
        or module.parent.parent.parent.name != "python"
    ):
        _fail("KAT builder is outside the exact capsule layout")
    return module.parents[3]


def main(arguments: Sequence[str]) -> int:
    try:
        if not sys.flags.isolated:
            _fail("KAT builder requires isolated Python (-I)")
        if len(arguments) != 6 or tuple(arguments[0::2]) != (
            "--request-output",
            "--expectation",
            "--response",
        ):
            _fail("KAT builder rejected its exact CLI")
        root = _capsule_root()
        request = Path(arguments[1])
        expectation = Path(arguments[3])
        response = Path(arguments[5])
        for label, path in (
            ("request", request),
            ("expectation", expectation),
            ("response", response),
        ):
            text = str(path)
            if not path.is_absolute() or os.path.normpath(text) != text:
                _fail("KAT {} output is not normalized absolute".format(label))
        if (
            request.parent != expectation.parent
            or request.parent != response.parent
            or len({request, expectation, response}) != 3
        ):
            _fail("KAT outputs must be distinct files in one private directory")

        # Establish the exact binary64 controls before NumPy or the math worker
        # is imported.  The native extension is part of the candidate capsule.
        fp_control.establish()
        import cp2_sequence_math_worker as worker

        worker.validate_runtime_environment(os.environ)
        worker.validate_import_isolation()
        worker.validate_loader_maps()
        worker.np.seterr(
            divide="raise", over="raise", invalid="raise", under="ignore"
        )
        worker.validate_numerical_thread_state()
        request_document = worker.direct_kat.encode_frozen_request()
        expectation_document, response_document = worker.derive_direct_kat_expectation(
            request_document
        )
        worker._write_new(request, request_document)
        worker._write_new(expectation, expectation_document)
        worker._write_new(response, response_document)
        worker.validate_loader_maps()
        fp_control.verify()
    except BaseException as exc:
        sys.stderr.write(
            "CP2-D KAT construction failed closed: {}: {}\n".format(
                type(exc).__name__, exc
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(tuple(sys.argv[1:])))
