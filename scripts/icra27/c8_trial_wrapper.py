#!/usr/bin/env python3
"""C8 wrapper around the frozen trial runners.

Runs cross_dataset_trial.py / cross_dataset_kaist_trial.py IN-PROCESS with the
frozen runner code untouched, extending only the environment allowlist so that
LD_PRELOAD (the C8 fault-injection shim) and C8_* controls reach the estimator
process. Nothing else in the frozen runners is modified.

Usage: c8_trial_wrapper.py {euroc|kaist} <runner args...>
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import cross_dataset_trial as common  # noqa: E402

EXTRA = frozenset(k for k in os.environ if k == "LD_PRELOAD" or k.startswith("C8_"))
common.ALLOWED_ENVIRONMENT = frozenset(common.ALLOWED_ENVIRONMENT) | EXTRA


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in ("euroc", "kaist"):
        print("usage: c8_trial_wrapper.py {euroc|kaist} <runner args>", file=sys.stderr)
        return 2
    kind = sys.argv[1]
    argv = sys.argv[2:]
    if kind == "kaist":
        import cross_dataset_kaist_trial as kaist  # noqa: E402
        return int(kaist.main(argv))
    return int(common.main(argv))


if __name__ == "__main__":
    sys.exit(main())
