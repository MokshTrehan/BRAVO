#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wholly synthetic protection for the CP2-D capsule math boundary."""

from __future__ import annotations

import copy
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_direct_kat as direct_kat  # noqa: E402
import cp2_sequence_math_codec as codec  # noqa: E402
import cp2_sequence_math_worker as worker  # noqa: E402


def sequence_fixture():
    positions = [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (1.0, 1.0, 0.0),
        (0.25, 0.75, 0.5),
    ]
    ground_truth = [(x + 1.0, y - 2.0, z + 0.5) for x, y, z in positions]
    # A non-rigid dyadic perturbation keeps baseline ATE strictly positive,
    # while identical modes make all parity differences exactly zero.
    ground_truth[-1] = (
        ground_truth[-1][0] + 0.001953125,
        ground_truth[-1][1],
        ground_truth[-1][2],
    )
    quaternions = [(0.0, 0.0, 0.0, 1.0)] * len(positions)
    timestamps = [1_000_000_000 + 10_000_000 * index for index in range(len(positions))]
    return codec.encode_sequence_request(
        sequence_index=0,
        sequence_id="MH_01_easy",
        nullspace_timestamps_ns=timestamps,
        nullspace_positions=positions,
        nullspace_quaternions_xyzw=quaternions,
        schur_timestamps_ns=timestamps,
        schur_positions=positions,
        schur_quaternions_xyzw=quaternions,
        ground_truth_timestamps_ns=timestamps,
        ground_truth_positions=ground_truth,
        ground_truth_quaternions_xyzw=quaternions,
    )


def exact_environment(private_root="/tmp/cp2-worker-test"):
    result = dict(worker.REQUIRED_FIXED_ENVIRONMENT)
    result.update(
        {
            name: private_root + "/" + suffix
            for name, suffix in worker.PRIVATE_ENVIRONMENT_SUFFIXES.items()
        }
    )
    return result


class SequenceTransportTests(unittest.TestCase):
    def test_stdlib_codec_import_does_not_load_numpy(self):
        code = (
            "import sys;sys.path.insert(0,{!r});"
            "import cp2_sequence_math_codec;"
            "raise SystemExit(1 if 'numpy' in sys.modules else 0)"
        ).format(str(CP2_DIRECTORY))
        completed = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", "-c", code],
            env={"LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_request_response_are_canonical_repeatable_and_exactly_joined(self):
        request = sequence_fixture()
        self.assertEqual(request, sequence_fixture())
        response = worker.evaluate_sequence_request(request)
        self.assertEqual(response, worker.evaluate_sequence_request(request))
        decoded = codec.decode_sequence_response(response, request)
        self.assertEqual(decoded.request_sha256, codec.decode_sequence_request(request).sha256)
        self.assertEqual(decoded.shared_timestamps_ns, tuple(1_000_000_000 + 10_000_000 * i for i in range(5)))
        self.assertGreater(decoded.metric("ate_nullspace_m_bits"), 0.0)
        self.assertEqual(decoded.metric_bits["ate_nullspace_m_bits"], decoded.metric_bits["ate_schur_m_bits"])
        self.assertEqual(decoded.metric("relative_ate_difference_bits"), 0.0)

        # The formal worker policy permits gradual underflow while retaining
        # fail-closed invalid/divide/overflow handling.  Exercise every
        # normative subnormal site inside this existing protecting case.
        prior = worker.np.seterr(
            divide="raise", over="raise", invalid="raise", under="ignore"
        )
        try:
            np = worker.np
            math_impl = worker.sequence_math
            minsub = np.nextafter(np.float64(0.0), np.float64(1.0))
            self.assertEqual(
                codec.f64_to_bits(float(math_impl.linear_p95([0.0, minsub]))),
                "0000000000000001",
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(math_impl.linear_p95([np.float64(-0.0), 0.0]))
                ),
                "0000000000000000",
            )

            axes = np.asarray(
                (
                    (1.0, 0.0, 0.0),
                    (-1.0, 0.0, 0.0),
                    (0.0, 1.0, 0.0),
                    (0.0, -1.0, 0.0),
                    (0.0, 0.0, 1.0),
                    (0.0, 0.0, -1.0),
                ),
                dtype=np.float64,
            )
            tiny_target = np.float64(1.0e-307) * axes
            tiny_alignment = math_impl.baseline_kabsch_alignment(
                axes, tiny_target
            )
            self.assertEqual(
                [
                    codec.f64_to_bits(float(value))
                    for value in tiny_alignment.cross_covariance_singular_values
                ],
                ["0017f8203b010811"] * 3,
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(tiny_alignment.cross_covariance_rank_threshold)
                ),
                "0000000000000009",
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(
                        math_impl.validate_source_singular_values(
                            [3.0e-308, 3.0e-308, 0.0], 3
                        )[1]
                    )
                ),
                "0000000000000004",
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(
                        math_impl.validate_cross_covariance_singular_values(
                            [3.0e-308, 3.0e-308, 0.0], 3, False
                        )[1]
                    )
                ),
                "0000000000000004",
            )

            seven_rows = np.vstack((axes, np.asarray(((0.0, 0.0, minsub),))))
            math_impl.baseline_kabsch_alignment(seven_rows, seven_rows)
            t = np.float64(1.0e-160)
            perturbed = np.vstack((axes, np.asarray(((t, -t, t),))))
            math_impl.baseline_kabsch_alignment(perturbed, perturbed)
            self.assertEqual(
                codec.f64_to_bits(
                    float(
                        math_impl.position_differences_m(
                            [[0.0, 0.0, 0.0]], [[t, 0.0, 0.0]]
                        )[0]
                    )
                ),
                "1eb67e93ddbc0e73",
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(
                        math_impl.translation_rmse_m(
                            [[0.0, 0.0, 0.0]], [[t, 0.0, 0.0]]
                        )
                    )
                ),
                "1eb67e93ddbc0e73",
            )
            positive = math_impl.jpl_stored_xyzw_to_hamilton_inverse_rotation(
                [t, 0.0, 0.0, 1.0]
            )
            negative = math_impl.jpl_stored_xyzw_to_hamilton_inverse_rotation(
                [-t, 0.0, 0.0, 1.0]
            )
            self.assertEqual(
                codec.f64_to_bits(
                    float(math_impl.orientation_differences_deg([positive], [negative])[0])
                ),
                "0000000000000000",
            )
        finally:
            worker.np.seterr(**prior)

    def test_request_and_response_mutations_fail_closed(self):
        request = sequence_fixture()
        response = worker.evaluate_sequence_request(request)
        request_value = json.loads(request.decode("ascii"))
        request_value["nullspace"]["positions"]["bits"][0] = "0000000000000001"
        changed_request = (
            json.dumps(request_value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        with self.assertRaises(codec.SequenceMathCodecError):
            codec.decode_sequence_response(response, changed_request)

        response_value = json.loads(response.decode("ascii"))
        response_value["associations"][0]["ground_truth_index"] = 1
        changed_response = (
            json.dumps(response_value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        with self.assertRaises(codec.SequenceMathCodecError):
            codec.decode_sequence_response(changed_response, request)
        with self.assertRaises(codec.SequenceMathCodecError):
            codec.decode_sequence_request(request.replace(b'"schema_version":1', b'"schema_version":1.0'))
        with self.assertRaises(codec.SequenceMathCodecError):
            codec.decode_sequence_request(b" " + request)

    def test_full_rank_source_with_coincident_target_fails_before_response(self):
        request_value = json.loads(sequence_fixture().decode("ascii"))
        ground_truth_bits = request_value["ground_truth"]["positions"]["bits"]
        ground_truth_bits[:] = ["0000000000000000"] * len(ground_truth_bits)
        changed_request = (
            json.dumps(request_value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        with self.assertRaisesRegex(
            worker.sequence_math.SequenceMathError, "cross-covariance"
        ):
            worker.evaluate_sequence_request(changed_request)

    def test_reflection_with_repeated_smallest_singular_values_fails_before_response(self):
        root_three = math.sqrt(3.0)
        source = [
            (3.0, 0.0, 0.0),
            (-3.0, 0.0, 0.0),
            (0.0, root_three, 0.0),
            (0.0, -root_three, 0.0),
            (0.0, 0.0, root_three),
            (0.0, 0.0, -root_three),
        ]
        target = [(x, y, -z) for x, y, z in source]
        quaternions = [(0.0, 0.0, 0.0, 1.0)] * len(source)
        timestamps = [
            1_000_000_000 + 10_000_000 * index
            for index in range(len(source))
        ]
        request = codec.encode_sequence_request(
            sequence_index=0,
            sequence_id="MH_01_easy",
            nullspace_timestamps_ns=timestamps,
            nullspace_positions=source,
            nullspace_quaternions_xyzw=quaternions,
            schur_timestamps_ns=timestamps,
            schur_positions=source,
            schur_quaternions_xyzw=quaternions,
            ground_truth_timestamps_ns=timestamps,
            ground_truth_positions=target,
            ground_truth_quaternions_xyzw=quaternions,
        )
        with self.assertRaisesRegex(
            worker.sequence_math.SequenceMathError,
            "unique smallest singular direction",
        ):
            worker.evaluate_sequence_request(request)

    def test_large_transport_array_is_not_limited_by_small_kat_codec(self):
        count = 40_000
        record = {"shape": [count], "bits": ["0000000000000000"] * count}
        decoded = codec._array_record(record, (count,), "large synthetic array")
        self.assertEqual(decoded.shape, (count,))
        self.assertEqual(len(decoded.bits), count)

    def test_binary64_hostile_timestamps_keep_exact_integer_10ms_associations(self):
        base = (1 << 53) + 1
        timestamps = [base + 20_000_000 * index for index in range(5)]
        positions = [
            (0.0, 0.0, 0.0),
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (1.0, 1.0, 0.0),
            (0.25, 0.75, 0.5),
        ]
        ground_truth = [(x + 1.0, y - 2.0, z + 0.5) for x, y, z in positions]
        ground_truth[-1] = (
            ground_truth[-1][0] + 0.001953125,
            ground_truth[-1][1],
            ground_truth[-1][2],
        )
        quaternions = [(0.0, 0.0, 0.0, 1.0)] * 5
        request = codec.encode_sequence_request(
            sequence_index=0,
            sequence_id="MH_01_easy",
            nullspace_timestamps_ns=timestamps,
            nullspace_positions=positions,
            nullspace_quaternions_xyzw=quaternions,
            schur_timestamps_ns=timestamps,
            schur_positions=positions,
            schur_quaternions_xyzw=quaternions,
            ground_truth_timestamps_ns=[item + 10_000_000 for item in timestamps],
            ground_truth_positions=ground_truth,
            ground_truth_quaternions_xyzw=quaternions,
        )
        decoded = codec.decode_sequence_response(
            worker.evaluate_sequence_request(request), request
        )
        self.assertEqual(
            [item["absolute_difference_ns"] for item in decoded.associations],
            [10_000_000] * 5,
        )
        self.assertEqual(
            [item["ground_truth_timestamp_ns"] for item in decoded.associations],
            [timestamps[0] + 10_000_000]
            + [item + 10_000_000 for item in timestamps[:-1]],
        )


class DirectKatWorkerTests(unittest.TestCase):
    def test_selected_stack_derivation_then_two_repeat_preflight(self):
        request = direct_kat.encode_frozen_request()
        expectation, construction_response = worker.derive_direct_kat_expectation(request)
        formal_response = worker.evaluate_direct_kat(request, expectation)
        self.assertEqual(formal_response, construction_response)
        validation = direct_kat.validate_pair(expectation, formal_response)
        self.assertTrue(validation.passed)
        self.assertEqual(validation.repeat_count, 2)

    def test_known_answer_and_request_substitution_reject(self):
        request = direct_kat.encode_frozen_request()
        expectation, _ = worker.derive_direct_kat_expectation(request)
        value = json.loads(expectation.decode("ascii"))
        value["cases"][0]["outputs"][0]["bits"][0] = "0000000000000001"
        changed = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        with self.assertRaises((worker.SequenceMathWorkerError, direct_kat.DirectKatError)):
            worker.evaluate_direct_kat(request, changed)
        request_value = json.loads(request.decode("ascii"))
        request_value["cases"][0]["inputs"][0]["bits"][0] = "0000000000000001"
        changed_request = (
            json.dumps(request_value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("ascii")
        with self.assertRaises(direct_kat.DirectKatError):
            worker.evaluate_direct_kat(changed_request, expectation)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_exact_valid_environment_and_each_injection_or_extra_key(self):
        valid = exact_environment()
        worker.validate_runtime_environment(valid)
        for key in worker.FORBIDDEN_INJECTION_ENVIRONMENT:
            mutated = dict(valid)
            mutated[key] = "synthetic"
            with self.subTest(key=key), self.assertRaises(worker.SequenceMathWorkerError):
                worker.validate_runtime_environment(mutated)
        extra = dict(valid)
        extra["UNDECLARED"] = "1"
        with self.assertRaises(worker.SequenceMathWorkerError):
            worker.validate_runtime_environment(extra)
        missing = dict(valid)
        del missing["OPENBLAS_NUM_THREADS"]
        with self.assertRaises(worker.SequenceMathWorkerError):
            worker.validate_runtime_environment(missing)

    def test_private_paths_and_backend_thread_count_fail_closed(self):
        invalid = exact_environment()
        invalid["TMPDIR"] = "/different-root/tmp"
        with self.assertRaises(worker.SequenceMathWorkerError):
            worker.validate_runtime_environment(invalid)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = (
                root
                / "python"
                / "lib"
                / "python3.11"
                / "numpy.libs"
                / "libscipy_openblas64_-32a4b2a6.so"
            )
            library.parent.mkdir(parents=True)
            library_payload = b"synthetic regular library"
            library.write_bytes(library_payload)
            os.chmod(library, 0o444)
            openblas_identity = dict(
                worker.EXPECTED_OPENBLAS_IDENTITY, num_procs=32
            )
            machine_identity = dict(
                worker.EXPECTED_X86_MACHINE,
                leaf1_ecx=0xFFFFFFFF,
                leaf7_ebx=0xFFFFFFFF,
                xcr0=0xE6,
            )
            cpu_features = {
                "X86_V2": True,
                "X86_V3": False,
                "X86_V4": False,
                "AVX512_ICL": False,
                "AVX512_SPR": False,
            }
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(worker, "_capsule_root", return_value=root)
                )
                stack.enter_context(mock.patch.object(
                    worker,
                    "OPENBLAS_LIBRARY_SHA256",
                    hashlib.sha256(library_payload).hexdigest(),
                ))
                stack.enter_context(mock.patch.object(
                    worker.fp_control,
                    "x86_cpuid_identity",
                    return_value=machine_identity,
                ))
                stack.enter_context(mock.patch.object(
                    worker.np_cpu_dispatch,
                    "__cpu_baseline__",
                    list(worker.EXPECTED_NUMPY_BASELINE),
                    create=True,
                ))
                stack.enter_context(mock.patch.object(
                    worker.np_cpu_dispatch,
                    "__cpu_dispatch__",
                    list(worker.EXPECTED_NUMPY_DISPATCH),
                    create=True,
                ))
                stack.enter_context(mock.patch.object(
                    worker.np_cpu_dispatch,
                    "__cpu_features__",
                    cpu_features,
                    create=True,
                ))
                with mock.patch.object(
                    worker.fp_control,
                    "openblas_identity",
                    return_value=dict(openblas_identity, threads=2),
                ):
                    with self.assertRaises(worker.SequenceMathWorkerError):
                        worker.validate_numerical_thread_state()
                with mock.patch.object(
                    worker.fp_control,
                    "openblas_identity",
                    return_value=openblas_identity,
                ):
                    worker.validate_numerical_thread_state()


if __name__ == "__main__":
    unittest.main()
