#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Legacy import shim for the evaluator-neutral CP2-D result parser.

No formal artifact, profile, receipt, or provenance identity may use ``evo``.
The active producer is the capsule-local equivalent evaluator.  This module
exists only so construction-time callers can migrate without weakening the
canonical parser in :mod:`cp2_evaluator_result`.
"""

import cp2_evaluator_result as _canonical

for _name in dir(_canonical):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_canonical, _name)
