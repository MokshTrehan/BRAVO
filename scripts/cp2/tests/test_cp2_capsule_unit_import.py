#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic-only tests for the source-frozen unit capsule importer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))
sys.path.insert(0, str(TEST_DIRECTORY))

import cp2_capsule as capsule  # noqa: E402
import cp2_capsule_builder as builder  # noqa: E402
import cp2_capsule_unit_import as unit_import  # noqa: E402
import test_cp2_capsule as capsule_fixture  # noqa: E402


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _write(path, payload, mode=0o444):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.chmod(str(path), mode)


class UnitCapsuleImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-unit-capsule-import-", dir="/tmp"
        )
        self.root = Path(self.temporary.name)
        os.chmod(str(self.root), 0o700)
        self.source_root = self.root / "source"
        self.source_root.mkdir(mode=0o700)
        self.external = self.root / "external"
        self.external.mkdir(mode=0o700)
        self.unit = self.root / "unit"
        self.unit.mkdir(mode=0o700)
        self.source_lock = capsule_fixture.AcyclicIdentityTests._source_lock()
        contract_bindings = []
        for index, relative in enumerate(capsule.CONTRACT_BINDING_PATHS):
            payload = ("synthetic contract {}\n".format(index)).encode("ascii")
            _write(self.source_root / relative, payload)
            contract_bindings.append(
                {"path": relative, "size": len(payload), "sha256": _sha(payload)}
            )
        helper = capsule_fixture.AcyclicIdentityTests()
        original = helper._artifacts(self.source_lock)
        self.artifacts = {}
        for kind, (archive, profile_bytes) in original.items():
            profile_record = json.loads(profile_bytes.decode("ascii"))
            profile_record["contract_bindings"] = contract_bindings
            profile = capsule.validate_capsule_profile(profile_record)
            self.artifacts[kind] = (archive, profile.canonical_bytes)
            _write(self.external / (kind + ".cp2cap"), archive)
            _write(self.external / (kind + ".profile.json"), profile.canonical_bytes)
        self.identity = capsule.make_expected_capsule_identity(
            self.source_lock, self.artifacts
        )
        self.identity_bytes = capsule.expected_capsule_identity_bytes(self.identity)
        locator = {
            "schema_version": 1,
            "record_type": "cp2_d_capsule_unit_import_locator",
            "checkpoint": "CP2-D",
            "formal_execution_permitted_by_this_record": False,
            "source_directory": str(self.external),
            "expected_identity_path": "project/cp2_capsule_expected_identity.json",
            "expected_identity_sha256": _sha(self.identity_bytes),
        }
        self.locator_bytes = unit_import._canonical(locator)
        _write(
            self.source_root / "project/cp2_capsule_unit_import.json",
            self.locator_bytes,
        )
        _write(
            self.source_root / "project/cp2_capsule_expected_identity.json",
            self.identity_bytes,
        )
        _write(
            self.source_root / "project/cp2_capsule_source_lock.json",
            self.source_lock,
        )

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _relocation(kind, archive_path, profile_record, expected):
        profile = capsule.validate_capsule_profile(profile_record)
        status_value = os.fstat(archive_path)
        archive_bytes = os.pread(archive_path, status_value.st_size, 0)
        if _sha(archive_bytes) != profile.archive_sha256:
            raise builder.CapsuleBuildError("synthetic archive mismatch")
        records = []
        for stage_index in (1, 2):
            records.append(
                {
                    "stage_index": stage_index,
                    "capsule_kind": kind,
                    "profile_id": profile.profile_id,
                    "profile_sha256": profile.profile_sha256,
                    "archive_sha256": profile.archive_sha256,
                    "inventory_sha256": profile.inventory_sha256,
                    "environment_sha256": profile.environment_sha256,
                    "version_probe": expected["version"],
                    "synthetic_preflight": expected["preflight"],
                }
            )
        return records

    def _import(self):
        with mock.patch.object(
            unit_import.builder,
            "relocation_rehearsal",
            side_effect=self._relocation,
        ):
            return unit_import.import_unit_capsules(self.source_root, self.unit)

    def test_exact_no_replace_import_retains_both_pairs_and_two_relocations(self):
        receipt = self._import()
        self.assertTrue(receipt["passed"])
        self.assertFalse(receipt["recorded_input_accessed"])
        self.assertEqual(set(receipt["unit_artifact_members"]), {"direct_math", "evaluator"})
        for kind, pair in self.artifacts.items():
            self.assertEqual(
                (self.unit / "capsules" / (kind + ".cp2cap")).read_bytes(),
                pair[0],
            )
            self.assertEqual(
                (self.unit / "capsules" / (kind + ".profile.json")).read_bytes(),
                pair[1],
            )
            self.assertEqual(
                len(receipt["unit_artifact_members"][kind]["relocation_rehearsals"]),
                2,
            )
        retained = (self.unit / unit_import.RECEIPT_RELATIVE).read_bytes()
        self.assertEqual(retained, unit_import._canonical(receipt))

        # A coordinated pathname replacement while the relocation consumer is
        # running cannot redirect that consumer away from the held archive FD,
        # and the importer's final held-FD/name join must still reject it.
        original_unit = self.unit
        self.unit = self.root / "coordinated-unit"
        self.unit.mkdir(mode=0o700)
        replaced = False

        def replace_during_rehearsal(kind, archive_fd, profile_record, expected):
            nonlocal replaced
            records = self._relocation(kind, archive_fd, profile_record, expected)
            if not replaced:
                replaced = True
                path = self.unit / "capsules" / (kind + ".cp2cap")
                displaced = path.with_name(path.name + ".displaced")
                path.rename(displaced)
                _write(path, b"x" * displaced.stat().st_size)
            return records

        try:
            with mock.patch.object(
                unit_import.builder,
                "relocation_rehearsal",
                side_effect=replace_during_rehearsal,
            ):
                with self.assertRaisesRegex(
                    unit_import.CapsuleImportError,
                    "destination changed across import",
                ):
                    unit_import.import_unit_capsules(
                        self.source_root, self.unit
                    )
            self.assertFalse((self.unit / unit_import.RECEIPT_RELATIVE).exists())
        finally:
            self.unit = original_unit

    def test_locator_noncanonical_or_identity_digest_drift_rejects(self):
        locator_path = self.source_root / unit_import.LOCATOR_RELATIVE
        os.chmod(str(locator_path), 0o600)
        locator_path.write_bytes(self.locator_bytes[:-1] + b" \n")
        os.chmod(str(locator_path), 0o444)
        with self.assertRaises(unit_import.CapsuleImportError):
            self._import()

    def test_external_archive_mutation_rejects_before_receipt(self):
        archive = self.external / "direct_math.cp2cap"
        os.chmod(str(archive), 0o600)
        archive.write_bytes(archive.read_bytes() + b"mutation")
        os.chmod(str(archive), 0o444)
        with self.assertRaises(unit_import.CapsuleImportError):
            self._import()
        self.assertFalse((self.unit / unit_import.RECEIPT_RELATIVE).exists())

    def test_destination_collision_and_preflight_failure_fail_closed(self):
        (self.unit / "capsules").mkdir(mode=0o700)
        with self.assertRaisesRegex(
            unit_import.CapsuleImportError, "destination already exists"
        ):
            self._import()
        (self.unit / "capsules").rmdir()
        with mock.patch.object(
            unit_import.builder,
            "relocation_rehearsal",
            side_effect=builder.CapsuleBuildError("synthetic preflight failure"),
        ):
            with self.assertRaisesRegex(
                unit_import.CapsuleImportError, "failed closed"
            ):
                unit_import.import_unit_capsules(self.source_root, self.unit)
        self.assertFalse((self.unit / unit_import.RECEIPT_RELATIVE).exists())


if __name__ == "__main__":
    unittest.main()
