#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Neutral entry point for the CP2-D evaluator-result protecting suite.

The full corruption corpus remains in ``test_cp2_evo_result.py`` so historical
source bindings can identify it.  Formal CP2-D inventories use this neutral
entry point; it loads that exact local file by descriptor-independent absolute
path and runs the same tests without claiming evo provenance for the approved
equivalent evaluator archive.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


LEGACY_SUITE_PATH = Path(__file__).resolve().with_name("test_cp2_evo_result.py")
SPEC = importlib.util.spec_from_file_location(
    "cp2_evaluator_result_protecting_suite", LEGACY_SUITE_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("evaluator-result protecting suite cannot be loaded")
SUITE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SUITE
SPEC.loader.exec_module(SUITE)


if __name__ == "__main__":
    unittest.main(module=SUITE)
