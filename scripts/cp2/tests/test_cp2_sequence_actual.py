#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic-only tests for CP2-D actual execution parsing seams."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import unittest
import warnings


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

    def test_capsule_command_is_completely_revalidated_before_and_after(self):
        events = []

        class Runtime:
            def revalidate(self):
                events.append("validate")

        class Recorder:
            def __init__(self, failure=None):
                self.failure = failure

            def run(self, *args, **kwargs):
                del args, kwargs
                events.append("run")
                if self.failure is not None:
                    raise self.failure
                return {"exit_code": 0}

        self.assertEqual(
            actual._run_revalidated_capsule_command(
                Runtime(), Recorder(), "synthetic"
            ),
            {"exit_code": 0},
        )
        self.assertEqual(events, ["validate", "run", "validate"])

        events.clear()
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            actual._run_revalidated_capsule_command(
                Runtime(), Recorder(RuntimeError("synthetic failure")), "synthetic"
            )
        self.assertEqual(events, ["validate", "run", "validate"])

        with tempfile.TemporaryDirectory() as temporary:
            trace = Path(temporary) / "trace"
            trace.mkdir(mode=0o700)
            held = actual._HeldRuntimeTrace(trace)
            try:
                held.precreate(("context.json", "callbacks.jsonl"))
                self.assertEqual(
                    actual.fcntl.fcntl(
                        held._member("callbacks.jsonl")[0],
                        actual.fcntl.F_GETFL,
                    )
                    & os.O_ACCMODE,
                    os.O_RDONLY,
                )
                held.fill_parent("context.json", b"synthetic context\n")
                child = subprocess.run(
                    [
                        "/usr/bin/python3",
                        "-I",
                        "-B",
                        "-c",
                        (
                            "import os,sys;"
                            "fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CLOEXEC);"
                            "os.write(fd,b'synthetic callbacks\\n');"
                            "os.fchmod(fd,0o444);os.fsync(fd);os.close(fd)"
                        ),
                        str(held.proc_path("callbacks.jsonl")),
                    ],
                    env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(child.returncode, 0, child.stderr)
                held.accept_external(("callbacks.jsonl",))
                self.assertEqual(
                    held.payload("callbacks.jsonl"), b"synthetic callbacks\n"
                )
                displaced = trace / "displaced-callbacks.jsonl"
                (trace / "callbacks.jsonl").rename(displaced)
                displaced.unlink()
                (trace / "callbacks.jsonl").write_bytes(
                    b"synthetic callbacks\n"
                )
                (trace / "callbacks.jsonl").chmod(0o444)
                with self.assertRaisesRegex(
                    actual.SequenceActualError,
                    "held runtime trace (?:member|bytes) changed",
                ):
                    held.revalidate()
            finally:
                held.close()

            # Path replacement is not the only post-publication mutation
            # surface: a same-UID process can retain a writer opened before
            # chmod(0444).  The held inode identity includes timestamps and
            # size, so a later same-inode write must still be detected.
            same_inode_trace = Path(temporary) / "same-inode-trace"
            same_inode_trace.mkdir(mode=0o700)
            held = actual._HeldRuntimeTrace(same_inode_trace)
            writer = -1
            try:
                held.precreate(("context.json",))
                writer = os.open(
                    held.proc_path("context.json"),
                    os.O_WRONLY | os.O_CLOEXEC,
                )
                held.fill_parent("context.json", b"synthetic context\n")
                os.pwrite(writer, b"X", 0)
                os.fsync(writer)
                with self.assertRaisesRegex(
                    actual.SequenceActualError,
                    "held runtime trace (?:member|bytes) changed",
                ):
                    held.revalidate()
            finally:
                if writer >= 0:
                    os.close(writer)
                held.close()

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
        self.assertEqual(actual.runner.OFFSETS_SECONDS, (40.0, 5.0, 0.0))
        self.assertEqual(
            tuple(float(value) for value in extractor.OFFSETS_SECONDS),
            actual.runner.OFFSETS_SECONDS,
        )
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
            witness_path = Path(temporary) / "pair_selection_witness.jsonl"
            descriptor = os.open(str(bag_path), os.O_RDONLY | os.O_CLOEXEC)
            output_parent_fd = os.open(
                temporary,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            witness_fd = os.open(
                witness_path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=output_parent_fd,
            )
            helper_path = SCRIPT_DIRECTORY / "cp2_pair_index_extract.py"
            helper_fd = os.open(
                str(helper_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
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
                    "--parent-process-id",
                    str(os.getpid()),
                    "--parent-helper-fd",
                    str(helper_fd),
                    "--helper-identity",
                    actual._bag_identity_argument(os.fstat(helper_fd)),
                    "--helper-sha256",
                    hashlib.sha256(helper_path.read_bytes()).hexdigest(),
                    "--witness-output",
                    str(witness_path),
                    "--parent-output-fd",
                    str(output_parent_fd),
                    "--output-parent-identity",
                    actual._bag_identity_argument(os.fstat(output_parent_fd)),
                    "--parent-witness-fd",
                    str(witness_fd),
                    "--witness-identity",
                    actual._bag_identity_argument(os.fstat(witness_fd)),
                )
                output = io.BytesIO()
                extractor._extract(
                    arguments,
                    {extractor.POSTAUTH_ENV: extractor.POSTAUTH_VALUE},
                    output,
                    bag_factory=lambda path, mode: Bag(path),
                    parent_pid=os.getpid(),
                    executed_helper_path="/proc/{}/fd/{}".format(
                        os.getpid(), helper_fd
                    ),
                )
                witness_payload = witness_path.read_bytes()
                witness_mode = stat.S_IMODE(witness_path.stat().st_mode)
            finally:
                os.close(helper_fd)
                os.close(witness_fd)
                os.close(output_parent_fd)
                os.close(descriptor)
        self.assertRegex(observed_paths[0], r"^/proc/self/fd/[0-9]+$")
        self.assertEqual(
            output.getvalue(),
            schema.jsonl_bytes(schema.strict_jsonl_loads(output.getvalue())),
        )
        self.assertEqual(len(schema.strict_jsonl_loads(output.getvalue())), 2)
        witness_rows = schema.strict_jsonl_loads(witness_payload)
        self.assertEqual(len(witness_rows), 4)
        self.assertEqual([row["filtered_index"] for row in witness_rows], [0, 1, 2, 3])
        self.assertEqual(witness_mode, 0o444)

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
                    "LD_LIBRARY_PATH": "/opt/ros/noetic/lib",
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

        # Exercise the real isolated child and its parent-held capabilities,
        # using a wholly synthetic ROS bag.  A child and its own descendant
        # must independently open the runner's read-only bag capability
        # without sharing or changing the runner FD offset.
        ros_python = "/opt/ros/noetic/lib/python3/dist-packages"
        if ros_python not in sys.path:
            sys.path.insert(0, ros_python)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import genpy  # noqa: E402
            import rosbag  # noqa: E402
            from sensor_msgs.msg import Image, Imu  # noqa: E402

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            bag_path = root / "synthetic-child.bag"
            with rosbag.Bag(str(bag_path), "w") as bag:
                imu = Imu()
                imu.header.stamp = genpy.Time(10, 0)
                bag.write("/imu0", imu, t=genpy.Time(10, 0))
                for seconds, nanoseconds, topic in (
                    (50, 0, "/cam0/image_raw"),
                    (50, 1, "/cam1/image_raw"),
                    (52, 0, "/cam0/image_raw"),
                    (52, 1, "/cam1/image_raw"),
                ):
                    image = Image()
                    image.header.stamp = genpy.Time(seconds, nanoseconds + 1)
                    bag.write(topic, image, t=genpy.Time(seconds, nanoseconds))
            authoritative_helper = SCRIPT_DIRECTORY / "cp2_pair_index_extract.py"
            helper_path = root / "cp2_pair_index_extract.py"
            helper_path.write_bytes(authoritative_helper.read_bytes())
            helper_path.chmod(0o555)
            hostile_marker = root / "mutable-module-imported"
            (root / "cp2_sequence_actual.py").write_text(
                "from pathlib import Path\n"
                "Path({!r}).write_text('imported')\n"
                "raise RuntimeError('mutable module imported')\n".format(
                    str(hostile_marker)
                ),
                encoding="utf-8",
            )
            bag_fd = os.open(
                str(bag_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            helper_fd = os.open(
                str(helper_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            parent_fd = os.open(
                str(root),
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            witness_path = root / "pair_selection_witness.jsonl"
            witness_fd = os.open(
                witness_path.name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                self.assertEqual(
                    actual.fcntl.fcntl(bag_fd, actual.fcntl.F_GETFL)
                    & os.O_ACCMODE,
                    os.O_RDONLY,
                )
                capability = "/proc/{}/fd/{}".format(os.getpid(), bag_fd)
                os.lseek(bag_fd, 7, os.SEEK_SET)
                descendant_code = (
                    "import subprocess,sys;"
                    "p=sys.argv[1];a=open(p,'rb').read();"
                    "b=subprocess.run(['/usr/bin/python3','-I','-B','-c',"
                    "'import sys;sys.stdout.buffer.write(open(sys.argv[1],\"rb\").read())',p],"
                    "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True);"
                    "sys.stdout.buffer.write(a+b.stdout)"
                )
                descendant = subprocess.run(
                    ["/usr/bin/python3", "-I", "-B", "-c", descendant_code, capability],
                    env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin"},
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                bag_payload = bag_path.read_bytes()
                self.assertEqual(descendant.returncode, 0, descendant.stderr)
                self.assertEqual(descendant.stdout, bag_payload + bag_payload)
                self.assertEqual(os.lseek(bag_fd, 0, os.SEEK_CUR), 7)

                environment = {
                    actual.PAIR_INDEX_POSTAUTH_ENV: actual.PAIR_INDEX_POSTAUTH_VALUE,
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "LD_LIBRARY_PATH": "/opt/ros/noetic/lib",
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONNOUSERSITE": "1",
                    "PYTHONPATH": ros_python,
                }
                parent_pid = os.getpid()
                helper_capability = "/proc/{}/fd/{}".format(parent_pid, helper_fd)
                completed = subprocess.run(
                    [
                        "/usr/bin/python3", "-I", "-B", helper_capability,
                        "--sequence-index", "0",
                        "--sequence-id", "MH_01_easy",
                        "--bag-path", str(bag_path),
                        "--parent-bag-fd", str(bag_fd),
                        "--bag-identity", actual._bag_identity_argument(os.fstat(bag_fd)),
                        "--parent-process-id", str(parent_pid),
                        "--parent-helper-fd", str(helper_fd),
                        "--helper-identity", actual._bag_identity_argument(os.fstat(helper_fd)),
                        "--helper-sha256", hashlib.sha256(helper_path.read_bytes()).hexdigest(),
                        "--witness-output", str(witness_path),
                        "--parent-output-fd", str(parent_fd),
                        "--output-parent-identity", actual._bag_identity_argument(os.fstat(parent_fd)),
                        "--parent-witness-fd", str(witness_fd),
                        "--witness-identity", actual._bag_identity_argument(os.fstat(witness_fd)),
                    ],
                    cwd=str(root),
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stderr, b"")
                self.assertEqual(
                    completed.stdout,
                    schema.jsonl_bytes(schema.strict_jsonl_loads(completed.stdout)),
                )
                self.assertEqual(
                    witness_path.read_bytes(),
                    schema.jsonl_bytes(
                        schema.strict_jsonl_loads(witness_path.read_bytes())
                    ),
                )
                self.assertFalse(hostile_marker.exists())
            finally:
                os.close(witness_fd)
                os.close(parent_fd)
                os.close(helper_fd)
                os.close(bag_fd)

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
        pair_evidence = actual.select_pair_evidence(
            0, "MH_01_easy", self._messages()
        )
        pair_payload = pair_evidence.pair_index_bytes

        class Authorization:
            prebag_authorized = True

            def __init__(self):
                self.calls = 0

            def revalidate(self):
                self.calls += 1

        class Repository:
            def __init__(self, helper):
                self.helper = helper

            def revalidate(self):
                pass

            def duplicate_tracked_fd(self, relative):
                if relative != "scripts/cp2/cp2_pair_index_extract.py":
                    raise AssertionError("unexpected held source")
                return os.open(
                    str(self.helper),
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                )

        class Recorder:
            def __init__(self, partial, helper=None, substitute_helper=False):
                self.partial = partial
                self.helper = helper
                self.substitute_helper = substitute_helper
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
                if "--witness-output" not in argv:
                    raise AssertionError("witness option differs")
                witness = Path(argv[argv.index("--witness-output") + 1])
                witness.write_bytes(pair_evidence.witness_bytes)
                witness.chmod(0o444)
                if self.substitute_helper:
                    displaced = self.helper.with_name("displaced-helper.py")
                    self.helper.rename(displaced)
                    self.helper.write_bytes(displaced.read_bytes())
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
            partial.mkdir(mode=0o700)
            bag_path = root / "synthetic.bag"
            bag_path.write_bytes(b"synthetic-only\n")
            bound = campaign._bind_regular_file(bag_path)
            authorization = Authorization()
            authorization.repository = Repository(helper)
            recorder = Recorder(partial, helper)
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
        self.assertEqual(observed.pair_index_bytes, pair_payload)
        self.assertEqual(observed.witness_bytes, pair_evidence.witness_bytes)
        self.assertEqual(
            recorder.argv[:7],
            [
                "/usr/bin/python3",
                "-I",
                "-B",
                recorder.argv[3],
                "--sequence-index",
                "0",
                "--sequence-id",
            ],
        )
        self.assertEqual(recorder.argv[7], "MH_01_easy")
        self.assertEqual(recorder.argv[8], "--bag-path")
        self.assertEqual(recorder.argv[10], "--parent-bag-fd")
        self.assertEqual(recorder.argv[12], "--bag-identity")
        self.assertRegex(recorder.argv[3], r"^/proc/[1-9][0-9]*/fd/[0-9]+$")
        self.assertEqual(recorder.argv[14], "--parent-process-id")
        self.assertEqual(recorder.argv[16], "--parent-helper-fd")
        self.assertEqual(recorder.argv[18], "--helper-identity")
        self.assertEqual(recorder.argv[20], "--helper-sha256")
        self.assertEqual(recorder.argv[22], "--witness-output")
        self.assertEqual(Path(recorder.argv[23]).name, "pair_selection_witness.jsonl")
        self.assertEqual(recorder.argv[24], "--parent-output-fd")
        self.assertEqual(recorder.argv[26], "--output-parent-identity")
        self.assertEqual(recorder.argv[28], "--parent-witness-fd")
        self.assertEqual(recorder.argv[30], "--witness-identity")
        self.assertEqual(len(recorder.argv), 32)
        self.assertEqual(recorder.assertions[2], "pair_index_witness_v4")
        self.assertEqual(
            recorder.environment[actual.PAIR_INDEX_POSTAUTH_ENV],
            actual.PAIR_INDEX_POSTAUTH_VALUE,
        )
        self.assertGreaterEqual(authorization.calls, 3)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "artifact"
            source = root / "source"
            helper = source / "scripts/cp2/cp2_pair_index_extract.py"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"# synthetic held helper substitution fixture\n")
            partial.mkdir(mode=0o700)
            bag_path = root / "synthetic.bag"
            bag_path.write_bytes(b"synthetic-only\n")
            bound = campaign._bind_regular_file(bag_path)
            authorization = Authorization()
            authorization.repository = Repository(helper)
            try:
                with self.assertRaisesRegex(
                    actual.SequenceActualError,
                    "held pair-index helper changed across execution",
                ):
                    actual._run_pair_index_command(
                        repo_root=root,
                        partial=partial,
                        build={"source_space": source},
                        recorder=Recorder(
                            partial, helper, substitute_helper=True
                        ),
                        authorization=authorization,
                        bound_bag=bound,
                        sequence_index=0,
                    )
            finally:
                bound.close()
            self.assertFalse(
                (partial / "pair_selection_witness.jsonl").exists()
            )


if __name__ == "__main__":
    unittest.main()
