#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protecting tests for formal CP2-D pair-witness replay."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_schema as schema  # noqa: E402
import verify_report as verifier  # noqa: E402


SEQUENCE_INDEX = 0
SEQUENCE_ID = "MH_01_easy"


class FormalPairWitnessReplayTests(unittest.TestCase):
    """Exercise the independent verifier without a bag or recorded values."""

    def setUp(self):
        # One combined chronological fixture covers:
        #   * first eligible candidate followed by another eligible candidate;
        #   * a used first candidate followed by an otherwise eligible one;
        #   * the exact excluded 20 ms boundary;
        #   * camera/IMU interleaving and equal record-time ordering; and
        #   * a terminal camera whose opposite-camera candidates are exhausted.
        self.messages = (
            ("cam0", 0, 100),
            ("cam1", 1, 101),
            ("cam1", 2, 102),
            ("cam0", 3, 103),
            ("cam0", 100, 104),
            ("cam0", 101, 105),
            ("cam1", 102, 106),
            ("cam1", 103, 107),
            ("cam0", 104, 108),
            ("cam0", 30_000_000, 109),
            ("cam1", 50_000_000, 110),
            ("cam0", 51_000_000, 111),
            ("imu", 60_000_000, 0),
            ("cam0", 60_000_000, 113),
            ("cam1", 60_000_000, 114),
            ("cam0", 61_000_000, 115),
        )
        self.witness_rows = [
            {
                "schema_version": 1,
                "record_type": "filtered_message",
                "sequence_index": SEQUENCE_INDEX,
                "sequence_id": SEQUENCE_ID,
                "filtered_index": index,
                "kind": kind,
                "camera_id": (
                    0 if kind == "cam0" else (1 if kind == "cam1" else None)
                ),
                "record_time_ns": record_time,
                "header_time_ns": header_time,
            }
            for index, (kind, record_time, header_time) in enumerate(self.messages)
        ]
        # Explicit expected output; it is intentionally not produced by the
        # implementation under test.
        selected = (
            (0, 0, 1),
            (2, 3, 2),
            (4, 4, 6),
            (7, 8, 7),
            (10, 11, 10),
            (13, 13, 14),
        )
        self.pair_rows = [
            self._pair_row(pair_index, anchor, cam0, cam1)
            for pair_index, (anchor, cam0, cam1) in enumerate(selected)
        ]
        self.witness_payload = schema.jsonl_bytes(self.witness_rows)
        self.pair_payload = schema.jsonl_bytes(self.pair_rows)
        self.report = {
            "sequence_index": SEQUENCE_INDEX,
            "sequence_id": SEQUENCE_ID,
        }

    def _pair_row(self, pair_index, anchor, cam0, cam1):
        anchor_kind = self.messages[anchor][0]
        cam0_message = self.messages[cam0]
        cam1_message = self.messages[cam1]
        return {
            "schema_version": 1,
            "record_type": "pair_index",
            "sequence_index": SEQUENCE_INDEX,
            "sequence_id": SEQUENCE_ID,
            "pair_index": pair_index,
            "anchor_filtered_index": anchor,
            "anchor_camera_id": 0 if anchor_kind == "cam0" else 1,
            "cam0_filtered_index": cam0,
            "cam1_filtered_index": cam1,
            "cam0_record_time_ns": cam0_message[1],
            "cam1_record_time_ns": cam1_message[1],
            "cam0_header_time_ns": cam0_message[2],
            "cam1_header_time_ns": cam1_message[2],
            "absolute_record_delta_ns": abs(
                cam0_message[1] - cam1_message[1]
            ),
        }

    def _replay(self, witness_payload=None, pair_rows=None, pair_payload=None):
        if witness_payload is None:
            witness_payload = self.witness_payload
        if pair_rows is None:
            pair_rows = self.pair_rows
        if pair_payload is None:
            pair_payload = self.pair_payload
        with tempfile.TemporaryDirectory(
            prefix="cp2-d-formal-pair-witness-", dir="/tmp"
        ) as temporary:
            artifact = Path(temporary)
            (artifact / "pair_selection_witness.jsonl").write_bytes(
                witness_payload
            )
            verifier._actual_replay_pair_selection_witness(
                artifact,
                self.report,
                pair_rows,
                pair_payload,
            )

    def _command_join(self, *, mutate=None, stdout_payload=None):
        environment = dict(verifier.ACTUAL_D_PAIR_INDEX_ENVIRONMENT)
        environment_sha = schema.command_environment_sha256(environment)
        workspace = Path("/tmp/cp2-d-synthetic-command-workspace")
        bag_path = "/tmp/cp2-d-synthetic-command.bag"
        parent_identity = (
            1,
            100,
            stat.S_IFDIR | 0o700,
            2,
            os.geteuid(),
            os.getegid(),
            4096,
            10,
            11,
        )
        witness_identity = (
            1,
            101,
            stat.S_IFREG | 0o600,
            1,
            os.geteuid(),
            os.getegid(),
            0,
            12,
            13,
        )
        bag_identity = (
            1,
            102,
            stat.S_IFREG | 0o400,
            1,
            os.geteuid(),
            os.getegid(),
            17,
            14,
            15,
        )
        helper_identity = (
            1,
            103,
            stat.S_IFREG | 0o555,
            1,
            os.geteuid(),
            os.getegid(),
            1234,
            16,
            17,
        )
        helper_sha256 = "4" * 64
        parent_pid = 4242

        def encoded(identity):
            return ":".join(str(value) for value in identity)

        required_sinks = {
            "serial_trace", "callback_trace", "trajectory_trace",
            "runtime_parameters", "loader_map_before", "loader_map_after",
            "legacy_state", "legacy_deviation", "legacy_timing",
        }
        sink_fields = (
            "serial_trace", "callback_trace", "trajectory_trace", "updater_trace",
            "state_payload", "proposal_payload", "raw_system_payload", "timing_trace",
            "runtime_parameters", "loader_map_before", "loader_map_after",
            "legacy_state", "legacy_deviation", "legacy_timing",
        )

        def runtime_argv(run_index):
            result = ["roslaunch", "bag:=/proc/4242/fd/10"]
            capabilities = {}
            for ordinal, field in enumerate(sink_fields):
                if field not in required_sinks:
                    value = "null"
                else:
                    descriptor = 20 + run_index * 20 + ordinal
                    identity = (
                        1,
                        200 + run_index * 20 + ordinal,
                        stat.S_IFREG | 0o600,
                        1,
                        os.geteuid(),
                        os.getegid(),
                        0,
                        30 + ordinal,
                        40 + ordinal,
                    )
                    value = "v1:{}:{}:{}".format(
                        parent_pid, descriptor, encoded(identity)
                    )
                    capabilities[field] = descriptor
                result.append(
                    "cp2_{}_sink_capability:={}".format(field, value)
                )
            result.extend(
                (
                    "path_state:=/proc/{}/fd/{}".format(
                        parent_pid, capabilities["legacy_state"]
                    ),
                    "path_std:=/proc/{}/fd/{}".format(
                        parent_pid, capabilities["legacy_deviation"]
                    ),
                    "path_time:=/proc/{}/fd/{}".format(
                        parent_pid, capabilities["legacy_timing"]
                    ),
                )
            )
            return result

        with tempfile.TemporaryDirectory(
            prefix="cp2-d-pair-command-join-", dir="/tmp"
        ) as temporary:
            artifact = Path(temporary)
            stdout = self.pair_payload if stdout_payload is None else stdout_payload
            files = {
                "logs/001_pair_index.stdout": stdout,
                "logs/001_pair_index.stderr": b"",
                "pair_index.jsonl": self.pair_payload,
                "pair_selection_witness.jsonl": self.witness_payload,
            }
            for relative, payload in files.items():
                path = artifact / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)
            manifest = {
                relative: hashlib.sha256(payload).hexdigest()
                for relative, payload in files.items()
            }
            command = {
                "command_id": 1,
                "phase": "pair_index",
                "sequence_index": SEQUENCE_INDEX,
                "pair_index": None,
                "run_index": None,
                "argv": [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    "/proc/{}/fd/9".format(parent_pid),
                    "--sequence-index",
                    str(SEQUENCE_INDEX),
                    "--sequence-id",
                    SEQUENCE_ID,
                    "--bag-path",
                    bag_path,
                    "--parent-bag-fd",
                    "10",
                    "--bag-identity",
                    encoded(bag_identity),
                    "--parent-process-id",
                    str(parent_pid),
                    "--parent-helper-fd",
                    "9",
                    "--helper-identity",
                    encoded(helper_identity),
                    "--helper-sha256",
                    helper_sha256,
                    "--witness-output",
                    "/tmp/.synthetic.partial/pair_selection_witness.jsonl",
                    "--parent-output-fd",
                    "11",
                    "--output-parent-identity",
                    encoded(parent_identity),
                    "--parent-witness-fd",
                    "12",
                    "--witness-identity",
                    encoded(witness_identity),
                ],
                "environment_sha256": environment_sha,
                "exit_code": 0,
                "timed_out": False,
                "stdout": "logs/001_pair_index.stdout",
                "stdout_sha256": manifest["logs/001_pair_index.stdout"],
                "stderr": "logs/001_pair_index.stderr",
                "stderr_sha256": manifest["logs/001_pair_index.stderr"],
            }
            environment_record = {
                "environment_id": "pair_index_witness_v4",
                "canonical_sha256": environment_sha,
                "variables": [
                    {"name": name, "value": environment[name]}
                    for name in sorted(environment, key=lambda item: item.encode("utf-8"))
                ],
            }
            common = {
                "commands": [
                    command,
                    {
                        "command_id": 2,
                        "phase": "runtime_preflight",
                        "sequence_index": SEQUENCE_INDEX,
                        "run_index": 0,
                        "argv": runtime_argv(0),
                    },
                    {
                        "command_id": 3,
                        "phase": "ros_run",
                        "sequence_index": SEQUENCE_INDEX,
                        "run_index": 0,
                        "argv": runtime_argv(0),
                    },
                    {
                        "command_id": 4,
                        "phase": "runtime_preflight",
                        "sequence_index": SEQUENCE_INDEX,
                        "run_index": 1,
                        "argv": runtime_argv(1),
                    },
                    {
                        "command_id": 5,
                        "phase": "ros_run",
                        "sequence_index": SEQUENCE_INDEX,
                        "run_index": 1,
                        "argv": runtime_argv(1),
                    },
                ],
                "provenance": {
                    "build": {"workspace": str(workspace)},
                    "contracts": [
                        {
                            "path": "scripts/cp2/cp2_pair_index_extract.py",
                            "sha256": helper_sha256,
                        }
                    ],
                    "environment": {"classes": [environment_record]},
                    "inputs": [{"bag_path": bag_path, "bag_size": 17}],
                },
                "environment_classes": {environment_sha: environment},
            }
            if mutate is not None:
                mutate(artifact, common, manifest, command)
            verifier._actual_validate_sequence_pair_index_command(
                artifact, common, manifest, self.report, self.pair_rows
            )

    def test_combined_authorized_edge_fixture_replays_byte_exactly(self):
        self.assertEqual(
            verifier._actual_canonical_jsonl_bytes(
                self.witness_rows, "synthetic witness"
            ),
            self.witness_payload,
        )
        self.assertEqual(
            verifier._actual_canonical_jsonl_bytes(
                self.pair_rows, "synthetic pair index"
            ),
            self.pair_payload,
        )
        self._replay()
        self._command_join()
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "environment is not exact",
        ):
            self._command_join(
                mutate=lambda _artifact, common, _manifest, _command: common[
                    "provenance"
                ]["environment"]["classes"][0].update(
                    {"environment_id": "pair_index_witness_v3"}
                )
            )

    def test_coordinated_later_candidate_substitution_is_rejected(self):
        changed = copy.deepcopy(self.pair_rows)
        # Move pair zero to the later, still-in-bound cam1 at index 2, then
        # reassign the skipped earlier cam1 at index 1 to pair one.  Indices,
        # deltas, order, and no-reuse remain locally valid.
        changed[0].update(
            {
                "cam1_filtered_index": 2,
                "cam1_record_time_ns": 2,
                "cam1_header_time_ns": 102,
                "absolute_record_delta_ns": 2,
            }
        )
        changed[1].update(
            {
                "anchor_filtered_index": 1,
                "cam1_filtered_index": 1,
                "cam1_record_time_ns": 1,
                "cam1_header_time_ns": 101,
                "absolute_record_delta_ns": 2,
            }
        )
        changed_payload = schema.jsonl_bytes(changed)
        with self.assertRaisesRegex(
            verifier.ActualVerificationError, "exact strict first-forward"
        ):
            self._replay(pair_rows=changed, pair_payload=changed_payload)

    def test_noncanonical_pair_jsonl_is_rejected(self):
        noncanonical = self.pair_payload.replace(b"{", b"{ ", 1)
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "pair_index.jsonl is not canonical JSONL",
        ):
            self._replay(pair_payload=noncanonical)
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "stdout/witness retained join differs",
        ):
            self._command_join(stdout_payload=noncanonical)

    def test_noncanonical_witness_jsonl_is_rejected(self):
        noncanonical = self.witness_payload.replace(b"{", b"{ ", 1)
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "pair_selection_witness.jsonl is not canonical JSONL",
        ):
            self._replay(witness_payload=noncanonical)
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "input/witness identity",
        ):
            self._command_join(
                mutate=lambda _artifact, _common, _manifest, command: command[
                    "argv"
                ].__setitem__(23, "/tmp/.synthetic.partial/substituted.jsonl")
            )


if __name__ == "__main__":
    unittest.main()
