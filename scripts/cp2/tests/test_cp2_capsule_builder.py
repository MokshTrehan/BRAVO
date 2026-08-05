#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protection for data-free capsule construction and rollback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_capsule as capsule  # noqa: E402
import cp2_capsule_builder as builder  # noqa: E402


VALID_CONTRACT_BINDINGS = [
    {"path": path, "size": 1, "sha256": "ab" * 32}
    for path in capsule.CONTRACT_BINDING_PATHS
]
VALID_SOURCE_LOCK = (
    json.dumps(
        {
            "checkpoint": "CP2-D",
            "formal_execution_permitted_by_this_record": False,
            "profile_contract_bindings": VALID_CONTRACT_BINDINGS,
            "record_type": "cp2_d_minimal_two_capsule_source_lock",
            "schema_version": 2,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode("ascii")


class ConstructionOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-capsule-builder-", dir="/tmp"
        )
        self.root = Path(self.temporary.name)
        os.chmod(self.root, 0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def test_bound_output_cleanup_removes_only_exact_immutable_inode(self):
        path = self.root / "output"
        payload = b"exact construction output\n"
        digest = hashlib.sha256(payload).hexdigest()
        builder._write_new(path, payload)
        identity = builder._bind_created_output(path, len(payload), digest)
        builder._remove_bound_output(path, identity, digest)
        self.assertFalse(path.exists())

        builder._write_new(path, payload)
        identity = builder._bind_created_output(path, len(payload), digest)
        held = self.root / "held"
        path.rename(held)
        builder._write_new(path, payload)
        with self.assertRaisesRegex(
            builder.CapsuleBuildError, "replacement construction output"
        ):
            builder._remove_bound_output(path, identity, digest)
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(held.read_bytes(), payload)

    def test_downstream_validation_failure_rolls_back_constructed_archive(self):
        input_root = self.root / "input"
        output_root = self.root / "output"
        input_root.mkdir(mode=0o700)
        output_root.mkdir(mode=0o700)
        entry = capsule.CapsuleEntry(
            "synthetic",
            "python_stdlib",
            0o444,
            1,
            hashlib.sha256(b"x").hexdigest(),
        )

        def encode(_sources, destination):
            payload = b"synthetic archive\n"
            builder._write_new(Path(destination), payload)
            return len(payload), hashlib.sha256(payload).hexdigest()

        with mock.patch.object(builder, "inventory_root", return_value=(entry,)), mock.patch.object(
            builder, "_read_regular", return_value=VALID_SOURCE_LOCK
        ), mock.patch.object(
            builder.capsule, "encode_capsule_file", side_effect=encode
        ), mock.patch.object(
            builder,
            "build_profile",
            side_effect=builder.CapsuleBuildError("synthetic profile failure"),
        ), mock.patch.object(
            builder, "_contract_bindings", return_value=VALID_CONTRACT_BINDINGS
        ):
            with self.assertRaisesRegex(
                builder.CapsuleBuildError, "synthetic profile failure"
            ):
                builder.construct(
                    "evaluator",
                    input_root,
                    output_root,
                )
        self.assertEqual(tuple(output_root.iterdir()), ())

    def test_preexisting_output_rejects_without_modification(self):
        input_root = self.root / "input-existing"
        output_root = self.root / "output-existing"
        input_root.mkdir(mode=0o700)
        output_root.mkdir(mode=0o700)
        existing = output_root / "evaluator.profile.json"
        existing.write_bytes(b"belongs to caller\n")
        os.chmod(existing, 0o444)
        with mock.patch.object(builder, "inventory_root", return_value=()), mock.patch.object(
            builder, "_read_regular", return_value=VALID_SOURCE_LOCK
        ), mock.patch.object(
            builder, "_contract_bindings", return_value=VALID_CONTRACT_BINDINGS
        ):
            with self.assertRaisesRegex(
                builder.CapsuleBuildError, "output already exists"
            ):
                builder.construct(
                    "evaluator",
                    input_root,
                    output_root,
                )
        self.assertEqual(existing.read_bytes(), b"belongs to caller\n")


if __name__ == "__main__":
    unittest.main()
