#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic in-memory tests for the shared postauthorization resolver."""

from __future__ import annotations

from pathlib import Path
import sys
import types
import unittest
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = SCRIPT_DIRECTORY.parents[1]
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_postauth_registry as registry  # noqa: E402
import cp2_sequence_runner as sequence_runner  # noqa: E402


def synthetic_registry(*, extra_ground_truth: bool = False, wrong_hash: bool = False,
                       duplicate_key: bool = False, duplicate_ground_truth: bool = False) -> bytes:
    lines = ["schema_version: 1", "outer_decoy: /not/the/minimum/scope.txt", "datasets:"]
    for index, (sequence, bag_hash, ground_truth_hash) in enumerate(
        zip(registry.SEQUENCES, registry.BAG_SHA256, registry.GROUND_TRUTH_SHA256)
    ):
        if wrong_hash and index == 1:
            ground_truth_hash = "0" * 64
        ground_truth_path = "/synthetic/{}.csv".format(sequence)
        if duplicate_ground_truth and index == 1:
            ground_truth_path = "/synthetic/{}.csv".format(registry.SEQUENCES[0])
        lines.extend(
            [
                "  - wrapper:",
                "      unrelated_parent_path: /parent/decoy-{}.txt".format(index),
                "      smallest:",
                "        sequence_id: {}".format(sequence),
                "        bag_path: /synthetic/{}.bag".format(sequence),
                "        bag_sha256: {}".format(bag_hash),
                "        ground_truth_path: {}".format(ground_truth_path),
                "        ground_truth_sha256: {}".format(ground_truth_hash),
            ]
        )
        if extra_ground_truth and index == 0:
            lines.append("        second_absolute_path: /synthetic/ambiguous.txt")
        if duplicate_key and index == 0:
            lines.append("        bag_path: /synthetic/duplicate-key.bag")
    return ("\n".join(lines) + "\n").encode("utf-8")


class PostauthorizationRegistryTests(unittest.TestCase):
    def test_nested_smallest_hash_bound_scopes_resolve_without_opening_paths(self):
        with mock.patch("builtins.open", side_effect=AssertionError("path access forbidden")):
            result = registry.resolve_postauthorized_sequence_inputs(synthetic_registry())
        self.assertEqual(tuple(item.sequence_id for item in result), registry.SEQUENCES)
        self.assertEqual(tuple(item.offset_seconds for item in result), registry.OFFSETS_SECONDS)
        self.assertEqual(
            tuple(str(item.bag_path) for item in result),
            tuple("/synthetic/{}.bag".format(sequence) for sequence in registry.SEQUENCES),
        )
        self.assertEqual(
            tuple(str(item.ground_truth_path) for item in result),
            tuple("/synthetic/{}.csv".format(sequence) for sequence in registry.SEQUENCES),
        )

    def test_ambiguous_minimum_ground_truth_scope_is_rejected(self):
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(
                synthetic_registry(extra_ground_truth=True)
            )

    def test_duplicate_yaml_key_is_rejected(self):
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(
                synthetic_registry(duplicate_key=True)
            )

    def test_yaml_container_alias_is_rejected(self):
        aliased = b"root:\n  first: &shared\n    value: 1\n  second: *shared\n"
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(aliased)

    def test_wrong_frozen_hash_scope_is_rejected(self):
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(
                synthetic_registry(wrong_hash=True)
            )

    def test_duplicate_cross_sequence_ground_truth_path_is_rejected(self):
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(
                synthetic_registry(duplicate_ground_truth=True)
            )

    def test_empty_nonbytes_nonmapping_and_oversized_buffers_are_rejected(self):
        for value in (b"", "not bytes", b"- list-root\n"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(registry.RegistryResolutionError):
                    registry.resolve_postauthorized_sequence_inputs(value)  # type: ignore[arg-type]
        with self.assertRaises(registry.RegistryResolutionError):
            registry.resolve_postauthorized_sequence_inputs(
                b"x" * (registry.MAX_REGISTRY_BYTES + 1)
            )

    def test_sequence_hook_resolves_only_after_held_authorization_then_fails_closed(self):
        class Authorization:
            prebag_authorized = True

            def __init__(self):
                self.revalidations = 0

            def revalidate(self):
                self.revalidations += 1

        authorization = Authorization()
        observed = []

        def stop_before_input(**kwargs):
            observed.append(kwargs["resolved_input"])
            raise sequence_runner.SequenceRunnerError("synthetic executor stop")

        actual = types.SimpleNamespace(
            __file__=str(SCRIPT_DIRECTORY / "cp2_sequence_actual.py"),
            execute_authorized_sequence=stop_before_input,
        )
        with mock.patch.dict(sys.modules, {"cp2_sequence_actual": actual}):
            with mock.patch("builtins.open", side_effect=AssertionError("input path access forbidden")):
                with self.assertRaisesRegex(
                    sequence_runner.SequenceRunnerError, "synthetic executor stop"
                ):
                    sequence_runner.run_authorized_sequence(
                        repo_root=str(REPOSITORY_ROOT),
                        parsed_cli={"--sequence": "MH_01_easy"},
                        authorization=authorization,
                        registry_bytes=synthetic_registry(),
                    )
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0].sequence_id, "MH_01_easy")
        self.assertEqual(authorization.revalidations, 3)


if __name__ == "__main__":
    unittest.main()
