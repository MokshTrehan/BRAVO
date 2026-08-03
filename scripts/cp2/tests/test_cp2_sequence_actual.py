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
        # The used cam1 at ordinal 3 is not reconsidered, and ordinal 4 uses
        # the first later cam0 at 5; no nearest-message search occurs.
        self.assertEqual(
            (rows[1]["anchor_filtered_index"], rows[1]["cam0_filtered_index"], rows[1]["cam1_filtered_index"]),
            (4, 5, 4),
        )

    def test_pair_boundary_reverse_time_and_bad_kind_are_rejected(self):
        cases = (
            (
                actual.FilteredMessage("cam0", 0, 1),
                actual.FilteredMessage("cam1", 20_000_000, 2),
            ),
            (
                actual.FilteredMessage("imu", 2, 0),
                actual.FilteredMessage("imu", 1, 0),
            ),
            (actual.FilteredMessage("other", 1, 0),),
        )
        for rows in cases:
            with self.subTest(rows=rows):
                with self.assertRaises(actual.SequenceActualError):
                    actual.select_pair_index_bytes(0, "MH_01_easy", rows)

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
        pair_payload = actual.select_pair_index_bytes(
            0, "MH_01_easy", self._messages()
        )
        serial_rows = []
        for pair in schema.strict_jsonl_loads(pair_payload):
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
        serial_rows[0]["selected"] = False
        with self.assertRaises(actual.SequenceActualError):
            actual.project_serial_pairs_to_pair_index_bytes(
                schema.jsonl_bytes(serial_rows), 0, "MH_01_easy"
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
