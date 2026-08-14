#!/usr/bin/python3
"""Tests for the additive Session-0 baseline digest utility."""

from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parent))
import baseline_digest  # noqa: E402


SOURCE_SHA = "82504db63fafda40dcf44b8e66cbd29609743a1d"


class BaselineDigestTest(unittest.TestCase):
    def write_run(self, root: Path, duration: str = "0.1") -> None:
        (root / "state_estimate.txt").write_text(
            "# state\n1.0 2.0 3.0\n2.0 4.0 5.0\n", encoding="utf-8"
        )
        (root / "state_deviation.txt").write_text(
            "# deviation\n1.0 0.1\n2.0 0.2\n", encoding="utf-8"
        )
        (root / "trajectory_tum.txt").write_text(
            "1.0 0 0 0 0 0 0 1\n2.0 1 0 0 0 0 0 1\n", encoding="utf-8"
        )
        (root / "timing_openvins.csv").write_text(
            "# timing\n"
            f"1.000001,{duration},0,0,0,0,0,0\n"
            f"2.000001,{duration},0,0,0,0,0,0\n",
            encoding="utf-8",
        )

    def test_stable_digest_ignores_duration_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            self.write_run(first, "0.1")
            self.write_run(second, "99.0")

            first_manifest = baseline_digest.build_manifest(first, SOURCE_SHA)
            second_manifest = baseline_digest.build_manifest(second, SOURCE_SHA)

            self.assertEqual(
                first_manifest["combined_stable_sha256"],
                second_manifest["combined_stable_sha256"],
            )
            self.assertEqual(
                first_manifest["stable_fields"], second_manifest["stable_fields"]
            )
            self.assertNotEqual(
                first_manifest["input_file_sha256"]["timing_openvins.csv"],
                second_manifest["input_file_sha256"]["timing_openvins.csv"],
            )

    def test_event_extension_derivatives_do_not_enter_estimator_digest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root)
            before = baseline_digest.build_manifest(root, SOURCE_SHA)
            (root / "t0_events.jsonl.zst").write_bytes(b"event telemetry")
            (root / "CALLBACK_STATE_ASSOCIATION.csv").write_text(
                "callback_id,state_status\n0,MATCHED\n", encoding="utf-8"
            )
            (root / "ASSOCIATION_COVERAGE.json").write_text(
                '{"schema_version":"turnsafe.association_coverage.v1"}\n',
                encoding="utf-8",
            )
            after = baseline_digest.build_manifest(root, SOURCE_SHA)
            self.assertEqual(before["combined_stable_sha256"],
                             after["combined_stable_sha256"])
            self.assertEqual(before["stable_fields"], after["stable_fields"])

    def test_nonfinite_input_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root)
            (root / "state_estimate.txt").write_text(
                "1.0 nan 3.0\n2.0 4.0 5.0\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "non-finite"):
                baseline_digest.build_manifest(root, SOURCE_SHA)

    def test_timestamp_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root)
            (root / "timing_openvins.csv").write_text(
                "1.1,0,0,0,0,0,0,0\n2.1,0,0,0,0,0,0,0\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "timestamp mismatch"):
                baseline_digest.build_manifest(root, SOURCE_SHA)

    def test_required_symlink_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root)
            state = root / "state_estimate.txt"
            target = root / "state_target.txt"
            state.rename(target)
            state.symlink_to(target.name)
            with self.assertRaisesRegex(ValueError, "non-symlink"):
                baseline_digest.build_manifest(root, SOURCE_SHA)

    def test_invalid_source_sha_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.write_run(root)
            with self.assertRaisesRegex(ValueError, "lowercase 40-character"):
                baseline_digest.build_manifest(root, "NOT_A_SHA")


if __name__ == "__main__":
    unittest.main()
