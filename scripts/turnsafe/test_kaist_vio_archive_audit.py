#!/usr/bin/env python3
"""Unit tests for the extraction-free KAIST VIO ZIP safety audit."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import kaist_vio_archive_audit as audit


class KaistVioArchiveAuditTest(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def _archive(self, names=None, prefix=""):
        path = self.root / "dataset.zip"
        members = names
        if members is None:
            members = [
                "/".join(item for item in (prefix, relative) if item)
                for relative in audit.EXPECTED_RELATIVE_BAGS
            ]
        with zipfile.ZipFile(os.fspath(path), "w", zipfile.ZIP_DEFLATED) as archive:
            for name in members:
                archive.writestr(name, b"bag-bytes")
        return path

    def _assert_rejected(self, path, code):
        with self.assertRaises(audit.ArchiveAuditError) as context:
            audit.audit_archive(path)
        self.assertEqual(code, context.exception.code)

    def test_valid_single_root_layout_and_sorted_json(self):
        path = self._archive(prefix="kaist_vio_dataset")
        report = audit.audit_archive(path)
        self.assertEqual("kaist_vio_dataset", report["root_prefix"])
        self.assertEqual(11, report["archive_member_count"])
        self.assertEqual(
            "kaist_vio_dataset/rotation/rotation_fast.bag",
            report["expected_bag_members"]["rotation/rotation_fast.bag"],
        )
        self.assertTrue(all(report["safety_flags"].values()))

        output = self.root / "audit.json"
        audit.write_json_atomic_no_overwrite(output, report)
        encoded = output.read_text(encoding="utf-8")
        decoded = json.loads(encoded)
        self.assertEqual(report, decoded)
        self.assertEqual(sorted(decoded), list(decoded))
        self.assertTrue(encoded.endswith("\n"))

    def test_valid_layout_without_root_prefix(self):
        report = audit.audit_archive(self._archive())
        self.assertEqual("", report["root_prefix"])
        self.assertEqual(set(audit.EXPECTED_RELATIVE_BAGS), set(report["expected_bag_members"]))

    def test_rejects_absolute_path(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["/absolute.txt"]
        self._assert_rejected(self._archive(names), "absolute_member_path")

    def test_rejects_drive_path(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["C:/absolute.txt"]
        self._assert_rejected(self._archive(names), "drive_member_path")

    def test_rejects_empty_path_component(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["metadata//ambiguous.txt"]
        self._assert_rejected(self._archive(names), "empty_path_component")

    def test_rejects_parent_component(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["metadata/../escape.txt"]
        self._assert_rejected(self._archive(names), "dot_path_component")

    def test_rejects_backslash_ambiguity(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["metadata\\ambiguous.txt"]
        self._assert_rejected(self._archive(names), "backslash_in_member_name")

    def test_rejects_symlink(self):
        path = self._archive()
        path.unlink()
        link = zipfile.ZipInfo("metadata/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(os.fspath(path), "w") as archive:
            for name in audit.EXPECTED_RELATIVE_BAGS:
                archive.writestr(name, b"bag")
            archive.writestr(link, b"target")
        self._assert_rejected(path, "symlink_member")

    def test_rejects_non_regular_special_entry(self):
        path = self._archive()
        path.unlink()
        fifo = zipfile.ZipInfo("metadata/fifo")
        fifo.create_system = 3
        fifo.external_attr = (stat.S_IFIFO | 0o600) << 16
        with zipfile.ZipFile(os.fspath(path), "w") as archive:
            for name in audit.EXPECTED_RELATIVE_BAGS:
                archive.writestr(name, b"bag")
            archive.writestr(fifo, b"")
        self._assert_rejected(path, "non_regular_member")

    def test_rejects_encrypted_member_metadata(self):
        encrypted = zipfile.ZipInfo("metadata/encrypted.bin")
        encrypted.flag_bits |= 0x1
        with self.assertRaises(audit.ArchiveAuditError) as context:
            audit._validated_member_path(encrypted)
        self.assertEqual("encrypted_member", context.exception.code)

    def test_rejects_normalized_duplicate(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["README", "README"]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            path = self._archive(names)
        self._assert_rejected(path, "normalized_member_collision")

    def test_rejects_casefold_collision(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["README", "readme"]
        self._assert_rejected(self._archive(names), "casefold_member_collision")

    def test_rejects_missing_expected_bag(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS[:-1])
        self._assert_rejected(self._archive(names), "missing_expected_bags")

    def test_rejects_unexpected_bag(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS) + ["bonus/bonus.bag"]
        self._assert_rejected(self._archive(names), "unexpected_bag_member")

    def test_rejects_mixed_root_prefixes(self):
        names = list(audit.EXPECTED_RELATIVE_BAGS)
        names[0] = "root/{}".format(names[0])
        self._assert_rejected(self._archive(names), "inconsistent_archive_root_prefix")

    def test_rejects_nested_root_prefix(self):
        self._assert_rejected(
            self._archive(prefix="outer/inner"), "nested_archive_root_prefix"
        )

    def test_atomic_writer_refuses_overwrite(self):
        output = self.root / "existing.json"
        output.write_text("keep\n", encoding="utf-8")
        with self.assertRaises(audit.ArchiveAuditError) as context:
            audit.write_json_atomic_no_overwrite(output, {"new": True})
        self.assertEqual("output_exists", context.exception.code)
        self.assertEqual("keep\n", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
