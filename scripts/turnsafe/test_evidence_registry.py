#!/usr/bin/python3
"""Repository-level checks for tracked historical evidence dependencies."""

import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
import kaist_vio_campaign as campaign  # noqa: E402


LEDGER = (
    REPO_ROOT
    / "project"
    / "evidence"
    / "kaist"
    / "session_0_5"
    / "KAIST_BASELINE_RESULTS.csv"
)
LEDGER_SHA256 = (
    "99f94546bfe1d807af04c47df82ff5bd0ed63d8c1eb500e5e10bed3fa95bf8b5"
)
SOURCE_COMMIT = "6e289fc2d1e8c86f0930605fc6b234df3cea1e84"
SOURCE_TREE = "40d59b7c4a6341dcc7ae1565f386cbbeb607db39"
SOURCE_IDENTITY_SHA256 = (
    "1ea6f6c50364ff8eb6248473f996df51a32aa760f88c8f8c79fe2d38bed3995f"
)
REQUIRED_INPUT_HASHES = (
    "source_bag_sha256",
    "adapted_bag_sha256",
    "config_sha256",
    "kalibr_imu_chain_sha256",
    "kalibr_imucam_chain_sha256",
    "reference_sha256",
)


class EvidenceRegistryTest(unittest.TestCase):
    def test_kaist_session_0_5_ledger_is_tracked_and_byte_exact(self) -> None:
        self.assertTrue(LEDGER.is_file())
        self.assertFalse(LEDGER.is_symlink())
        self.assertEqual(LEDGER.stat().st_size, 126368)
        self.assertEqual(hashlib.sha256(LEDGER.read_bytes()).hexdigest(), LEDGER_SHA256)
        tracked = subprocess.run(
            [
                "git",
                "ls-files",
                "--error-unmatch",
                "--",
                LEDGER.relative_to(REPO_ROOT).as_posix(),
            ],
            cwd=str(REPO_ROOT),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)

    def test_kaist_session_0_5_rows_bind_frozen_inputs(self) -> None:
        with LEDGER.open("r", encoding="utf-8", errors="strict", newline="") as stream:
            rows = list(csv.DictReader(stream))

        self.assertEqual(
            [row["sequence"] for row in rows], list(campaign.SEQUENCE_ORDER)
        )
        self.assertEqual(len({row["sequence"] for row in rows}), len(rows))
        self.assertEqual({row["baseline_source_commit"] for row in rows}, {SOURCE_COMMIT})
        self.assertEqual({row["baseline_source_tree"] for row in rows}, {SOURCE_TREE})

        for row in rows:
            for key in REQUIRED_INPUT_HASHES:
                self.assertRegex(row[key], re.compile(r"^[0-9a-f]{64}$"))
            source_identity = json.loads(row["baseline_source_evidence"])
            self.assertEqual(source_identity["sha256"], SOURCE_IDENTITY_SHA256)
            self.assertEqual(source_identity["size_bytes"], 1939)
            self.assertEqual(
                campaign._frozen_sequence_inputs(row["sequence"]),
                {key: row[key] for key in REQUIRED_INPUT_HASHES},
            )


if __name__ == "__main__":
    unittest.main()
