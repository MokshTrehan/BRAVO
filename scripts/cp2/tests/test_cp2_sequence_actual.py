#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic-only tests for CP2-D actual execution parsing seams."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_schema as schema  # noqa: E402
import cp2_pair_index_extract as extractor  # noqa: E402
import cp2_recorded_campaign as campaign  # noqa: E402
import cp2_sequence_actual as actual  # noqa: E402


class SequenceActualPureTests(unittest.TestCase):
    @staticmethod
    def _messages():
        return (
            actual.FilteredMessage("cam0", 100, 1_000),
            actual.FilteredMessage("cam1", 110, 1_001),
            actual.FilteredMessage("cam0", 200, 2_000),
            actual.FilteredMessage("cam1", 210, 2_001),
        )

    def test_first_forward_pair_selection_and_filtered_ordinals_are_exact(self):
        messages = (
            actual.FilteredMessage("imu", 1, 0),
            actual.FilteredMessage("cam0", 100, 1_000),
            actual.FilteredMessage("imu", 101, 0),
            actual.FilteredMessage("cam1", 110, 1_001),
            actual.FilteredMessage("cam1", 111, 1_002),
            actual.FilteredMessage("cam0", 112, 2_000),
            actual.FilteredMessage("cam1", 113, 2_001),
        )
        payload = actual.select_pair_index_bytes(0, "MH_01_easy", messages)
        rows = list(schema.strict_jsonl_loads(payload))
        self.assertEqual([row["pair_index"] for row in rows], [0, 1])
        self.assertEqual(
            (rows[0]["anchor_filtered_index"], rows[0]["cam0_filtered_index"], rows[0]["cam1_filtered_index"]),
            (1, 1, 3),
        )
        # The used cam1 at ordinal 3 is not reconsidered as an anchor, and
        # ordinal 4 uses the first later cam0 at ordinal 5.
        self.assertEqual(
            (rows[1]["anchor_filtered_index"], rows[1]["cam0_filtered_index"], rows[1]["cam1_filtered_index"]),
            (4, 5, 4),
        )

    def test_strict_record_boundary_and_record_not_header_selection_are_exact(self):
        self.assertEqual(actual.runner.STRICT_PAIR_DELTA_NS, 20_000_000)
        messages = (
            # The record delta is one nanosecond inside the strict boundary,
            # while the header delta is outside it. This pair must be retained.
            actual.FilteredMessage("cam0", 0, 0),
            actual.FilteredMessage("cam1", 19_999_999, 50_000_000),
            # The record delta is exactly the excluded boundary, while the
            # header delta is only one nanosecond. This pair must be omitted.
            actual.FilteredMessage("cam0", 100_000_000, 1_000),
            actual.FilteredMessage("cam1", 120_000_000, 1_001),
            # Keep a second accepted pair so neither result can be masked by
            # the selector's independent minimum-population requirement.
            actual.FilteredMessage("cam0", 200_000_000, 2_000),
            actual.FilteredMessage("cam1", 200_000_001, 2_001),
        )
        payload = actual.select_pair_index_bytes(0, "MH_01_easy", messages)
        rows = list(schema.strict_jsonl_loads(payload))
        self.assertEqual(payload, schema.jsonl_bytes(rows))
        self.assertEqual([row["pair_index"] for row in rows], [0, 1])
        self.assertEqual(
            [row["anchor_filtered_index"] for row in rows], [0, 4]
        )
        self.assertEqual(
            [row["absolute_record_delta_ns"] for row in rows],
            [19_999_999, 1],
        )

    def test_invalid_metadata_rejection_is_not_masked_by_minimum_population(self):
        cases = {
            "reverse-record-time": (
                actual.FilteredMessage("cam0", 100, 1_000),
                actual.FilteredMessage("cam1", 110, 1_001),
                actual.FilteredMessage("imu", 50, 0),
                actual.FilteredMessage("cam0", 200, 2_000),
                actual.FilteredMessage("cam1", 210, 2_001),
            ),
            "invalid-kind": (
                actual.FilteredMessage("cam0", 100, 1_000),
                actual.FilteredMessage("cam1", 110, 1_001),
                actual.FilteredMessage("other", 150, 0),
                actual.FilteredMessage("cam0", 200, 2_000),
                actual.FilteredMessage("cam1", 210, 2_001),
            ),
            "imu-header": (
                actual.FilteredMessage("cam0", 100, 1_000),
                actual.FilteredMessage("cam1", 110, 1_001),
                actual.FilteredMessage("imu", 150, 1),
                actual.FilteredMessage("cam0", 200, 2_000),
                actual.FilteredMessage("cam1", 210, 2_001),
            ),
            "boolean-record-time": (
                actual.FilteredMessage("cam0", True, 1_000),
                actual.FilteredMessage("cam1", 2, 1_001),
                actual.FilteredMessage("cam0", 3, 2_000),
                actual.FilteredMessage("cam1", 4, 2_001),
            ),
            "boolean-header-time": (
                actual.FilteredMessage("cam0", 1, False),
                actual.FilteredMessage("cam1", 2, 1_001),
                actual.FilteredMessage("cam0", 3, 2_000),
                actual.FilteredMessage("cam1", 4, 2_001),
            ),
        }
        for label, messages in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(actual.SequenceActualError):
                    actual.select_pair_index_bytes(
                        0, "MH_01_easy", messages
                    )

        with self.assertRaises(actual.SequenceActualError):
            actual.select_pair_index_bytes(False, "MH_01_easy", self._messages())

    def test_used_first_forward_candidate_is_not_searched_past(self):
        messages = (
            actual.FilteredMessage("cam0", 100, 1_000),
            actual.FilteredMessage("cam0", 101, 1_001),
            actual.FilteredMessage("cam1", 102, 1_002),
            actual.FilteredMessage("cam1", 103, 1_003),
            actual.FilteredMessage("cam0", 104, 1_004),
        )
        rows = list(
            schema.strict_jsonl_loads(
                actual.select_pair_index_bytes(0, "MH_01_easy", messages)
            )
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [
                (
                    row["anchor_filtered_index"],
                    row["cam0_filtered_index"],
                    row["cam1_filtered_index"],
                )
                for row in rows
            ],
            [(0, 0, 2), (3, 4, 3)],
        )

    def test_selected_pair_population_requires_positive_cam0_duration(self):
        messages = (
            actual.FilteredMessage("cam0", 100, 1_000),
            actual.FilteredMessage("cam1", 100, 1_001),
            actual.FilteredMessage("cam0", 100, 2_000),
            actual.FilteredMessage("cam1", 100, 2_001),
        )
        with self.assertRaises(actual.SequenceActualError):
            actual.select_pair_index_bytes(0, "MH_01_easy", messages)

    def test_exact_ground_truth_decimal_parser(self):
        payload = (
            b"# arbitrary synthetic trajectory comment\n"
            b"# the parser does not depend on comment spelling\n"
            b"1  0 0   0 0 0 0 1\n"
            b"1.000000001 1 0 0 0 0 0 1\n"
            b"2.25    0 1 0 0 0 0 1\n"
        )
        rows = actual.parse_ground_truth_tum_bytes(payload)
        self.assertEqual([row.timestamp_ns for row in rows], [1_000_000_000, 1_000_000_001, 2_250_000_000])
        self.assertEqual(rows[1].position, (1.0, 0.0, 0.0))

    def test_ground_truth_format_timestamp_and_quaternion_corruption_reject(self):
        cases = (
            b"# synthetic\n1.0000000001 0 0 0 0 0 0 1\n",
            b"# synthetic\n01.0 0 0 0 0 0 0 1\n",
            b"# synthetic\n1 0 0 0 0 0 0 2\n",
            b"# synthetic\n1 0 0 0 0 0 .0 1\n",
            b"# synthetic\n1 0 0 0 0 0 1\n",
            b"# synthetic\n1\t0 0 0 0 0 0 1\n",
            b"# synthetic\n1 0 0 0 0 0 0 1",
            b"# synthetic \xff\n1 0 0 0 0 0 0 1\n",
        )
        for payload in cases:
            with self.subTest(payload=payload[:24]):
                with self.assertRaises(actual.SequenceActualError):
                    actual.parse_ground_truth_tum_bytes(payload)

    def test_rosbag_adapter_is_injectable_and_closes_provider(self):
        class Time:
            def __init__(self, secs, nsecs):
                self.secs = secs
                self.nsecs = nsecs

        class Header:
            def __init__(self, stamp):
                self.stamp = stamp

        class Image:
            _type = "sensor_msgs/Image"

            def __init__(self, stamp):
                self.header = Header(stamp)

        class Bag:
            def __init__(self):
                self.closed = False
                self.start = None

            def read_messages(self, raw=False, topics=None, start_time=None):
                if raw:
                    return iter((("/decoy", b"raw", Time(10, 5)),))
                self.start = start_time
                del topics
                return iter(
                    (
                        ("/cam0/image_raw", Image(Time(50, 1)), Time(50, 0)),
                        ("/cam1/image_raw", Image(Time(50, 2)), Time(50, 1)),
                        ("/imu0", object(), Time(51, 0)),
                        ("/cam0/image_raw", Image(Time(52, 1)), Time(52, 0)),
                        ("/cam1/image_raw", Image(Time(52, 2)), Time(52, 1)),
                    )
                )

            def close(self):
                self.closed = True

        class Authorization:
            prebag_authorized = True

            def __init__(self):
                self.calls = 0

            def revalidate(self):
                self.calls += 1

        bag = Bag()
        authorization = Authorization()
        payload = actual.read_pair_index_from_bag(
            Path("/synthetic/not-opened.bag"),
            0,
            authorization,
            bag_factory=lambda path, mode: bag,
        )
        self.assertEqual(len(list(schema.strict_jsonl_loads(payload))), 2)
        self.assertTrue(bag.closed)
        self.assertEqual((bag.start.secs, bag.start.nsecs), (50, 5))
        self.assertEqual(authorization.calls, 2)

    def test_recorded_serial_projection_is_exact_source_key_jsonl(self):
        expected_sequences = (
            "MH_01_easy",
            "MH_03_medium",
            "V1_01_easy",
        )
        self.assertEqual(campaign.SEQUENCES, expected_sequences)
        self.assertEqual(actual.runner.SEQUENCES, expected_sequences)
        self.assertEqual(extractor.SEQUENCES, expected_sequences)
        self.assertEqual(
            campaign.STRICT_PAIR_DELTA_NS,
            actual.runner.STRICT_PAIR_DELTA_NS,
        )
        self.assertEqual(actual.runner.STRICT_PAIR_DELTA_NS, 20_000_000)
        self.assertEqual(campaign.PAIR_INDEX_KEYS, actual.runner.PAIR_KEYS)
        self.assertEqual(
            campaign.PAIR_INDEX_POSTAUTH_ENV,
            actual.PAIR_INDEX_POSTAUTH_ENV,
        )
        self.assertEqual(
            campaign.PAIR_INDEX_POSTAUTH_VALUE,
            actual.PAIR_INDEX_POSTAUTH_VALUE,
        )
        self.assertEqual(campaign.BAG_IDENTITY_FIELDS, actual.BAG_IDENTITY_FIELDS)
        pair_payload = actual.select_pair_index_bytes(
            0, "MH_01_easy", self._messages()
        )
        pair_rows = list(schema.strict_jsonl_loads(pair_payload))
        self.assertEqual(
            actual.runner._validate_pairs(pair_payload, 0, "MH_01_easy"),
            tuple(pair_rows),
        )
        for invalid_schema in (True, 1.0):
            corrupted_pairs = [dict(row) for row in pair_rows]
            corrupted_pairs[0]["schema_version"] = invalid_schema
            with self.subTest(runner_schema=invalid_schema):
                with self.assertRaises(actual.runner.SequenceRunnerError):
                    actual.runner._validate_pairs(
                        schema.jsonl_bytes(corrupted_pairs),
                        0,
                        "MH_01_easy",
                    )
        for invalid_request in (False, 0.0):
            with self.subTest(runner_request=invalid_request):
                with self.assertRaises(actual.runner.SequenceRunnerError):
                    actual.runner._validate_pairs(
                        pair_payload, invalid_request, "MH_01_easy"
                    )

        serial_rows = []
        for pair in pair_rows:
            serial_rows.append(
                {
                    **pair,
                    "record_type": "serial_pair",
                    "camera_timestamp_ns": pair["cam0_header_time_ns"],
                    "selected": True,
                    "enqueue_entered": True,
                    "enqueue_returned": True,
                    "enqueue_status": "queued",
                    "processing_entered": True,
                    "processing_returned": True,
                    "processing_status": "processed",
                    "updater_invocation_ids": [pair["pair_index"]],
                }
            )
        serial_payload = schema.jsonl_bytes(serial_rows)
        self.assertEqual(
            actual.project_serial_pairs_to_pair_index_bytes(
                serial_payload, 0, "MH_01_easy"
            ),
            pair_payload,
        )

        corruptions = (
            ("selected-false", "selected", False),
            ("selected-integer", "selected", 1),
            ("event-integer", "enqueue_entered", 1),
            ("schema-boolean", "schema_version", True),
            ("schema-float", "schema_version", 1.0),
            ("sequence-boolean", "sequence_index", False),
            (
                "duplicate-invocation",
                "updater_invocation_ids",
                [0, 0],
            ),
        )
        for label, field, value in corruptions:
            corrupted_serial = [dict(row) for row in serial_rows]
            corrupted_serial[0][field] = value
            with self.subTest(serial_corruption=label):
                with self.assertRaises(actual.SequenceActualError):
                    actual.project_serial_pairs_to_pair_index_bytes(
                        schema.jsonl_bytes(corrupted_serial),
                        0,
                        "MH_01_easy",
                    )

        with self.assertRaises(actual.SequenceActualError):
            actual.project_serial_pairs_to_pair_index_bytes(
                schema.jsonl_bytes(list(reversed(serial_rows))),
                0,
                "MH_01_easy",
            )

    def test_pair_index_cli_binds_parent_fd_and_writes_only_canonical_jsonl(self):
        class Time:
            def __init__(self, secs, nsecs):
                self.secs = secs
                self.nsecs = nsecs

        class Header:
            def __init__(self, stamp):
                self.stamp = stamp

        class Image:
            _type = "sensor_msgs/Image"

            def __init__(self, stamp):
                self.header = Header(stamp)

        observed_paths = []

        class Bag:
            def __init__(self, path):
                observed_paths.append(path)
                self.closed = False

            def read_messages(self, raw=False, topics=None, start_time=None):
                del topics, start_time
                if raw:
                    return iter((("/decoy", b"raw", Time(10, 0)),))
                return iter(
                    (
                        ("/cam0/image_raw", Image(Time(50, 1)), Time(50, 0)),
                        ("/cam1/image_raw", Image(Time(50, 2)), Time(50, 1)),
                        ("/cam0/image_raw", Image(Time(52, 1)), Time(52, 0)),
                        ("/cam1/image_raw", Image(Time(52, 2)), Time(52, 1)),
                    )
                )

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as temporary:
            bag_path = Path(temporary) / "synthetic.bag"
            bag_path.write_bytes(b"synthetic-only\n")
            descriptor = os.open(str(bag_path), os.O_RDONLY | os.O_CLOEXEC)
            try:
                identity = actual._bag_identity_argument(os.fstat(descriptor))
                arguments = (
                    "--sequence-index",
                    "0",
                    "--sequence-id",
                    "MH_01_easy",
                    "--bag-path",
                    str(bag_path),
                    "--parent-bag-fd",
                    str(descriptor),
                    "--bag-identity",
                    identity,
                )
                output = io.BytesIO()
                extractor._extract(
                    arguments,
                    {extractor.POSTAUTH_ENV: extractor.POSTAUTH_VALUE},
                    output,
                    bag_factory=lambda path, mode: Bag(path),
                    parent_pid=os.getpid(),
                )
            finally:
                os.close(descriptor)
        self.assertRegex(observed_paths[0], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(
            output.getvalue(),
            schema.jsonl_bytes(schema.strict_jsonl_loads(output.getvalue())),
        )
        self.assertEqual(len(schema.strict_jsonl_loads(output.getvalue())), 2)

    def test_pair_index_cli_requires_capability_before_input_or_rosbag(self):
        arguments = (
            "--sequence-index",
            "0",
            "--sequence-id",
            "MH_01_easy",
            "--bag-path",
            "/synthetic/not-opened.bag",
            "--parent-bag-fd",
            "999",
            "--bag-identity",
            "0:0:0:0:0:0:0:0:0",
        )
        output = io.BytesIO()
        with self.assertRaises(extractor.PairIndexCLIError) as caught:
            extractor._extract(
                arguments,
                {},
                output,
                bag_factory=lambda path, mode: self.fail("provider was reached"),
            )
        self.assertIn("authorization capability", str(caught.exception))
        self.assertEqual(output.getvalue(), b"")

    def test_pair_index_cli_process_failure_keeps_stdout_empty(self):
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        }
        completed = subprocess.run(
            (
                "/usr/bin/python3",
                "-I",
                "-B",
                str(SCRIPT_DIRECTORY / "cp2_pair_index_extract.py"),
                "--sequence-index",
                "0",
                "--sequence-id",
                "MH_01_easy",
                "--bag-path",
                "/synthetic/not-opened.bag",
                "--parent-bag-fd",
                "999",
                "--bag-identity",
                "0:0:0:0:0:0:0:0:0",
            ),
            cwd=str(SCRIPT_DIRECTORY),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 1)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"failed closed", completed.stderr)

    def test_pair_index_cli_process_success_is_exact_jsonl_only(self):
        provider_source = b'''\
class Time:
    def __init__(self, secs, nsecs):
        self.secs = secs
        self.nsecs = nsecs

class Header:
    def __init__(self, stamp):
        self.stamp = stamp

class Image:
    _type = "sensor_msgs/Image"
    def __init__(self, stamp):
        self.header = Header(stamp)

class Bag:
    def __init__(self, path, mode):
        assert path.startswith("/proc/self/fd/")
        assert mode == "r"
    def read_messages(self, raw=False, topics=None, start_time=None):
        if raw:
            return iter((("/decoy", b"raw", Time(10, 0)),))
        assert topics == ["/imu0", "/cam0/image_raw", "/cam1/image_raw"]
        assert (start_time.secs, start_time.nsecs) == (50, 0)
        return iter((
            ("/cam0/image_raw", Image(Time(50, 1)), Time(50, 0)),
            ("/cam1/image_raw", Image(Time(50, 2)), Time(50, 1)),
            ("/cam0/image_raw", Image(Time(52, 1)), Time(52, 0)),
            ("/cam1/image_raw", Image(Time(52, 2)), Time(52, 1)),
        ))
    def close(self):
        pass
'''
        provider_namespace = {}
        exec(compile(provider_source, "<synthetic-rosbag>", "exec"), provider_namespace)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bag_path = root / "synthetic.bag"
            bag_path.write_bytes(b"synthetic-only\n")
            descriptor = os.open(str(bag_path), os.O_RDONLY | os.O_CLOEXEC)
            try:
                environment = {
                    actual.PAIR_INDEX_POSTAUTH_ENV: actual.PAIR_INDEX_POSTAUTH_VALUE,
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                }
                output = io.BytesIO()
                extractor._extract(
                    (
                        "--sequence-index", "0",
                        "--sequence-id", "MH_01_easy",
                        "--bag-path", str(bag_path),
                        "--parent-bag-fd", str(descriptor),
                        "--bag-identity",
                        actual._bag_identity_argument(os.fstat(descriptor)),
                    ),
                    environment,
                    output,
                    bag_factory=provider_namespace["Bag"],
                    parent_pid=os.getpid(),
                )
            finally:
                os.close(descriptor)
        rows = schema.strict_jsonl_loads(output.getvalue())
        self.assertEqual(len(rows), 2)
        self.assertEqual(output.getvalue(), schema.jsonl_bytes(rows))

    def test_pair_index_cli_rejects_unapproved_v1_02_identity(self):
        arguments = (
            "--sequence-index",
            "2",
            "--sequence-id",
            "V1_02_medium",
            "--bag-path",
            "/synthetic/not-opened.bag",
            "--parent-bag-fd",
            "999",
            "--bag-identity",
            "0:0:0:0:0:0:0:0:0",
        )
        output = io.BytesIO()
        with self.assertRaises(extractor.PairIndexCLIError) as caught:
            extractor._extract(
                arguments,
                {extractor.POSTAUTH_ENV: extractor.POSTAUTH_VALUE},
                output,
                bag_factory=lambda path, mode: self.fail("provider was reached"),
            )
        self.assertIn("sequence identity", str(caught.exception))
        self.assertEqual(output.getvalue(), b"")

    def test_pair_index_command_retains_exact_stdout_and_exact_argv(self):
        pair_payload = actual.select_pair_index_bytes(
            0, "MH_01_easy", self._messages()
        )

        class Authorization:
            prebag_authorized = True

            def __init__(self):
                self.calls = 0

            def revalidate(self):
                self.calls += 1

        class Recorder:
            def __init__(self, partial):
                self.partial = partial
                self.argv = None
                self.environment = None

            def run(
                self,
                phase,
                argv,
                cwd,
                environment_id,
                variables,
                sequence_index=None,
            ):
                self.argv = argv
                self.environment = variables
                self.assertions = (
                    phase,
                    cwd,
                    environment_id,
                    sequence_index,
                )
                stdout = self.partial / "logs/001_pair_index.stdout"
                stderr = self.partial / "logs/001_pair_index.stderr"
                stdout.parent.mkdir(parents=True)
                stdout.write_bytes(pair_payload)
                stderr.write_bytes(b"")
                return {
                    "exit_code": 0,
                    "timed_out": False,
                    "stdout": stdout.relative_to(self.partial).as_posix(),
                    "stdout_sha256": hashlib.sha256(pair_payload).hexdigest(),
                    "stderr": stderr.relative_to(self.partial).as_posix(),
                    "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "artifact"
            source = root / "source"
            helper = source / "scripts/cp2/cp2_pair_index_extract.py"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"# synthetic helper fixture\n")
            partial.mkdir()
            bag_path = root / "synthetic.bag"
            bag_path.write_bytes(b"synthetic-only\n")
            bound = campaign._bind_regular_file(bag_path)
            authorization = Authorization()
            recorder = Recorder(partial)
            try:
                observed = actual._run_pair_index_command(
                    repo_root=root,
                    partial=partial,
                    build={"source_space": source},
                    recorder=recorder,
                    authorization=authorization,
                    bound_bag=bound,
                    sequence_index=0,
                )
            finally:
                bound.close()
        self.assertEqual(observed, pair_payload)
        self.assertEqual(
            recorder.argv[:7],
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                str(helper),
                "--sequence-index",
                "0",
                "--sequence-id",
            ],
        )
        self.assertEqual(recorder.argv[7], "MH_01_easy")
        self.assertEqual(recorder.argv[8], "--bag-path")
        self.assertEqual(recorder.argv[10], "--parent-bag-fd")
        self.assertEqual(recorder.argv[12], "--bag-identity")
        self.assertEqual(
            recorder.environment[actual.PAIR_INDEX_POSTAUTH_ENV],
            actual.PAIR_INDEX_POSTAUTH_VALUE,
        )
        self.assertGreaterEqual(authorization.calls, 3)


if __name__ == "__main__":
    unittest.main()
