#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wholly synthetic protecting tests for the proposed CP2-D capsule boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_capsule as capsule  # noqa: E402


ZERO_SHA = "00" * 32


def lp(payload):
    return len(payload).to_bytes(8, "big") + payload


def raw_capsule(rows):
    output = bytearray(capsule.CAPSULE_MAGIC)
    output.extend(len(rows).to_bytes(8, "big"))
    for path, role, mode, payload, claimed_digest in rows:
        output.extend(lp(path))
        output.extend(lp(role))
        output.extend(mode.to_bytes(8, "big"))
        output.extend(len(payload).to_bytes(8, "big"))
        output.extend(bytes.fromhex(claimed_digest))
        output.extend(payload)
    return bytes(output)


def evaluator_members():
    rows = (
        ("bin/launcher", "capsule_launcher", 0o555, b"synthetic relocatable launcher\n"),
        ("bin/sandbox", "capsule_sandbox", 0o555, b"synthetic static sandbox\n"),
        ("fixtures/estimate.tum", "preflight_fixture", 0o444, b"1 0 0 0 0 0 0 1\n"),
        (
            "fixtures/expected-stats-bits.json",
            "preflight_known_answer",
            0o444,
            b'{"rmse_bits":"0000000000000000"}\n',
        ),
        ("fixtures/ground-truth.tum", "preflight_fixture", 0o444, b"1 0 0 0 0 0 0 1\n"),
        ("native/ld-linux-x86-64.so.2", "native_loader", 0o555, b"synthetic-loader"),
        ("native/libmath.so", "native_library", 0o444, b"synthetic-native-library"),
        ("notices/GPL-3.0.txt", "license_notice", 0o444, b"GPL-3.0-or-later\n"),
        ("notices/cp2-capsule-source-lock.json", "source_lock", 0o444, b'{"active":true}\n'),
        ("python/bin/python3.11", "python_interpreter", 0o555, b"synthetic-python-elf"),
        (
            "python/lib/python3.11/cp2_equivalent_evaluator.py",
            "evaluator_module",
            0o444,
            b"def main(): pass\n",
        ),
        ("python/lib/python3.11/stdlib.py", "python_stdlib", 0o444, b"# synthetic stdlib\n"),
    )
    return tuple(capsule.make_capsule_member(*row) for row in rows)


def direct_math_members():
    rows = (
        ("bin/launcher", "capsule_launcher", 0o555, b"synthetic direct launcher\n"),
        ("bin/sandbox", "capsule_sandbox", 0o555, b"synthetic static sandbox\n"),
        (
            "direct/known-answers.json",
            "direct_math_known_answer",
            0o444,
            b'{"rmse_bits":"419279a74590331c"}\n',
        ),
        ("direct/main.py", "direct_math_module", 0o444, b"def main(): pass\n"),
        ("fixtures/request.json", "preflight_fixture", 0o444, b"{}\n"),
        ("native/ld-linux-x86-64.so.2", "native_loader", 0o555, b"synthetic-loader"),
        ("notices/BSD-3-Clause.txt", "license_notice", 0o444, b"BSD-3-Clause\n"),
        ("notices/cp2-capsule-source-lock.json", "source_lock", 0o444, b'{"active":true}\n'),
        ("python/bin/python3.11", "python_interpreter", 0o555, b"synthetic-python-elf"),
        ("python/lib/python3.11/numpy-2.4.6.dist-info/METADATA", "numpy_distribution_metadata", 0o444, b"Name: numpy\n"),
        (
            "python/lib/python3.11/numpy.libs/libscipy_openblas64_-32a4b2a6.so",
            "native_library",
            0o444,
            b"synthetic-numpy-library",
        ),
        ("python/lib/python3.11/numpy/__init__.py", "numpy_module", 0o444, b"__version__ = '2.4.6'\n"),
        ("python/lib/python3.11/numpy/core.so", "numpy_extension", 0o444, b"synthetic-extension"),
        ("python/lib/python3.11/stdlib.py", "python_stdlib", 0o444, b"# synthetic stdlib\n"),
    )
    return tuple(capsule.make_capsule_member(*row) for row in rows)


def entry_rows(members):
    return [
        {
            "path": member.entry.path,
            "role": member.entry.role,
            "mode": member.entry.mode,
            "size": member.entry.size,
            "sha256": member.entry.sha256,
        }
        for member in members
    ]


def subset_digest(members, paths):
    by_path = {member.entry.path: member.entry for member in members}
    return capsule.inventory_sha256(tuple(by_path[path] for path in paths))


def consumers_digest(rows):
    values = tuple(
        capsule.NativeConsumer(
            item["path"], item["elf_type"], item["linkage"], item["interpreter"],
            tuple(item["rpath"]), tuple(item["runpath"]), item["soname"],
            tuple(item["needed"]),
        )
        for item in rows
    )
    return hashlib.sha256(capsule._native_consumers_bytes(values)).hexdigest()


def edges_digest(rows):
    values = tuple(
        capsule.NativeEdge(item["consumer"], item["needed"], item["provider"])
        for item in rows
    )
    return hashlib.sha256(capsule._native_edges_bytes(values)).hexdigest()


def native_closure(members):
    paths = [
        member.entry.path
        for member in members
        if member.entry.role in ("native_loader", "native_library")
    ]
    by_path = {member.entry.path: member.entry for member in members}
    dependency_paths = [
        path for path in paths if by_path[path].role == "native_library"
    ]
    native_consumer_paths = [
        member.entry.path
        for member in members
        if member.entry.role in capsule.NATIVE_CONSUMER_ROLES
    ]
    native_consumer_paths.sort()
    python_path = "python/bin/python3.11"
    needed_by_consumer = {
        path: ([Path(item).name for item in dependency_paths] if path == python_path else [])
        for path in native_consumer_paths
    }
    consumers = []
    for path in native_consumer_paths:
        executable = by_path[path].role in (
            "capsule_sandbox", "capsule_launcher", "python_interpreter"
        )
        if by_path[path].role in ("capsule_sandbox", "capsule_launcher"):
            linkage = "static-executable"
        elif executable:
            linkage = "dynamic-executable"
        elif by_path[path].role == "native_loader":
            linkage = "dynamic-loader"
        else:
            linkage = "shared-object"
        consumers.append(
            {
                "path": path,
                "elf_type": "ET_EXEC" if executable else "ET_DYN",
                "linkage": linkage,
                "interpreter": (
                    "/lib64/ld-linux-x86-64.so.2"
                    if by_path[path].role == "python_interpreter"
                    else "none"
                ),
                "rpath": [],
                "runpath": [],
                "soname": Path(path).name if by_path[path].role in ("native_library", "python_extension") else "none",
                "needed": sorted(needed_by_consumer[path]),
            }
        )
    edges = [
        {"consumer": python_path, "needed": Path(path).name, "provider": path}
        for path in dependency_paths
    ]
    edges.sort(key=lambda item: (item["consumer"], item["needed"], item["provider"]))
    native_edges = tuple(
        capsule.NativeEdge(item["consumer"], item["needed"], item["provider"])
        for item in edges
    )
    return {
        "loader_path": "native/ld-linux-x86-64.so.2",
        "loader_argv_prefix": [
            "${HELD_LOADER_FD}",
            "--inhibit-cache",
            "--library-path",
            (
                "${HELD_NATIVE_DIR}:${HELD_PYTHON_LIB_DIR}:"
                "${HELD_NUMPY_LIBS_DIR}"
                if any(member.entry.role == "numpy_module" for member in members)
                else "${HELD_NATIVE_DIR}:${HELD_PYTHON_LIB_DIR}"
            ),
            "${HELD_PYTHON_FD}",
            "-I",
            "-S",
            "-B",
            "-m",
            (
                "direct.main"
                if any(member.entry.role == "direct_math_module" for member in members)
                else "cp2_equivalent_evaluator"
            ),
        ],
        "effective_library_directories": (
            ["native", "python/lib", "python/lib/python3.11/numpy.libs"]
            if any(member.entry.role == "numpy_module" for member in members)
            else ["native", "python/lib"]
        ),
        "cache_policy": "inhibit-cache",
        "default_search_policy": "runtime-maps-reject-outside-capsule",
        "pathname_policy": "descriptor-held-loader-python-and-library-directories",
        "mapped_paths": paths,
        "consumers": consumers,
        "needed_edges": edges,
        "inventory_sha256": subset_digest(members, paths),
        "consumers_sha256": consumers_digest(consumers),
        "edges_sha256": hashlib.sha256(capsule._native_edges_bytes(native_edges)).hexdigest(),
    }


def command(argv, stdout_sha="22" * 32):
    return {
        "argv": list(argv),
        "cwd": "${PRIVATE_ROOT}/work",
        "timeout_seconds": 60,
        "stdin_policy": "devnull",
        "exit_code": 0,
        "stdout_size": 11,
        "stdout_sha256": stdout_sha,
        "stderr_size": 0,
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }


def profile_for(kind="evaluator", document=None, members_override=None):
    members = (
        evaluator_members() if kind == "evaluator" else direct_math_members()
    ) if members_override is None else tuple(members_override)
    if document is None:
        document = capsule.encode_capsule(members)
    environment = dict(capsule.EXACT_ENVIRONMENT)
    launcher = "${CAPSULE_ROOT}/bin/launcher"
    if kind == "evaluator":
        distribution_specs = ()
        entry_point = {
            "launcher_path": "bin/launcher",
            "interpreter_path": "python/bin/python3.11",
            "module_path": "python/lib/python3.11/cp2_equivalent_evaluator.py",
            "module": "cp2_equivalent_evaluator",
            "callable": "main",
        }
        argv_template = [
            launcher, "tum", "{GT_SHARED}", "{MODE_SHARED_ALIGNED}", "-r",
            "trans_part", "--t_max_diff", "0.01", "--save_results",
            "{ABS_RESULT_ZIP}", "--no_warnings",
        ]
        preflight_argv = [
            launcher, "tum", "${CAPSULE_ROOT}/fixtures/ground-truth.tum",
            "${CAPSULE_ROOT}/fixtures/estimate.tum", "-r", "trans_part",
            "--t_max_diff", "0.01", "--save_results",
            "${PRIVATE_ROOT}/preflight/results.zip", "--no_warnings",
        ]
        preflight_paths = [
            "fixtures/estimate.tum",
            "fixtures/expected-stats-bits.json",
            "fixtures/ground-truth.tum",
        ]
        output_codec = "cp2_translation_rmse_result_zip_v1"
        known_answer_path = "fixtures/expected-stats-bits.json"
        numerical_identity = {
            "runtime_kind": "stdlib-binary64-independent-v1",
            "implementation": "CPython-math-no-NumPy-no-SciPy-no-evo",
        }
        cpu_dispatch_policy = "stdlib-binary64-fixed-x86-64"
    else:
        distribution_specs = (
            (
                "numpy",
                "2.4.6",
                [
                    "python/lib/python3.11/numpy-2.4.6.dist-info/METADATA",
                    "python/lib/python3.11/numpy/__init__.py",
                    "python/lib/python3.11/numpy/core.so",
                ],
            ),
        )
        entry_point = {
            "launcher_path": "bin/launcher",
            "interpreter_path": "python/bin/python3.11",
            "module_path": "direct/main.py",
            "module": "direct.main",
            "callable": "main",
        }
        argv_template = [
            launcher, "--input", "{ABS_REQUEST}", "--output", "{ABS_RESPONSE}",
        ]
        preflight_argv = [
            launcher, "--input", "${CAPSULE_ROOT}/fixtures/request.json",
            "--known-answers", "${CAPSULE_ROOT}/direct/known-answers.json",
            "--output", "${PRIVATE_ROOT}/preflight/response.json",
        ]
        preflight_paths = ["direct/known-answers.json", "fixtures/request.json"]
        output_codec = "cp2_f64_known_answer_bundle_v1"
        known_answer_path = "direct/known-answers.json"
        openblas_path = (
            "python/lib/python3.11/numpy.libs/"
            "libscipy_openblas64_-32a4b2a6.so"
        )
        openblas_sha = next(
            member.entry.sha256 for member in members if member.entry.path == openblas_path
        )
        numerical_identity = {
            "runtime_kind": "numpy-openblas-fixed-dispatch-v1",
            "numpy_version": "2.4.6",
            "numpy_cpu_baseline": ["X86_V2"],
            "numpy_cpu_dispatch_targets": [
                "X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"
            ],
            "numpy_disabled_targets": [
                "X86_V3", "X86_V4", "AVX512_ICL", "AVX512_SPR"
            ],
            "numpy_effective_target_states": {
                "X86_V2": True,
                "X86_V3": False,
                "X86_V4": False,
                "AVX512_ICL": False,
                "AVX512_SPR": False,
            },
            "openblas_library_path": openblas_path,
            "openblas_library_sha256": openblas_sha,
            "openblas_version": "0.3.31.188.0",
            "openblas_config": (
                "OpenBLAS 0.3.31.188.0  USE64BITINT DYNAMIC_ARCH "
                "NO_AFFINITY SkylakeX MAX_THREADS=64"
            ),
            "openblas_corename": "SkylakeX",
            "openblas_threads": 1,
            "openblas_parallel": 1,
            "cpuid": {
                "vendor": "AuthenticAMD",
                "family": 26,
                "model": 68,
                "stepping": 0,
                "leaf1_eax": 11800384,
                "leaf1_ecx": 2128097803,
                "leaf1_edx": 395049983,
                "leaf7_ebx": 4055865259,
                "leaf7_ecx": 423649246,
                "leaf7_edx": 268435728,
                "extended_leaf1_ecx": 1975662591,
                "extended_leaf1_edx": 802421759,
                "xcr0": 743,
            },
        }
        cpu_dispatch_policy = "numpy-x86-v2-openblas-skylakex"
    distributions = [
        {
            "name": name,
            "version": version,
            "paths": paths,
            "inventory_sha256": subset_digest(members, paths),
        }
        for name, version, paths in distribution_specs
    ]
    return {
        "schema_version": 2,
        "record_type": "cp2_d_capsule_profile",
        "checkpoint": "CP2-D",
        "contract_bindings": [
            {"path": path, "size": 1, "sha256": "ab" * 32}
            for path in capsule.CONTRACT_BINDING_PATHS
        ],
        "profile_id": "synthetic-{}-x86_64".format(kind),
        "capsule_kind": kind,
        "target": {
            "os": "linux",
            "machine": "x86_64",
            "elf_class": 64,
            "endianness": "little",
            "libc_abi": "synthetic-glibc",
            "loader_abi": "synthetic-ld-linux",
            "elf_interpreter": "/lib64/ld-linux-x86-64.so.2",
            "cpu_dispatch_policy": cpu_dispatch_policy,
        },
        "source_lock": {
            "path": "notices/cp2-capsule-source-lock.json",
            "size": next(
                member.entry.size
                for member in members
                if member.entry.role == "source_lock"
            ),
            "sha256": next(
                member.entry.sha256
                for member in members
                if member.entry.role == "source_lock"
            ),
        },
        "archive": {"size": len(document), "sha256": hashlib.sha256(document).hexdigest()},
        "inventory": entry_rows(members),
        "inventory_sha256": capsule.inventory_sha256(tuple(member.entry for member in members)),
        "entry_point": entry_point,
        "distributions": distributions,
        "native_closure": native_closure(members),
        "environment": {
            "variables": environment,
            "sha256": capsule.environment_sha256(tuple(sorted(environment.items()))),
        },
        "execution": {
            "argv_template": argv_template,
            "cwd": "${PRIVATE_ROOT}/work",
            "timeout_seconds": 120,
            "stdin_policy": "devnull",
            "environment_sha256": capsule.environment_sha256(tuple(sorted(environment.items()))),
            "injection_denylist": list(capsule.INJECTION_DENYLIST),
            "thread_policy": "verified-single-thread",
            "hash_seed_policy": "hash-order-independent",
        },
        "floating_point": {
            "format": "IEEE-754-binary64",
            "rounding_mode": "FE_TONEAREST",
            "subnormal_policy": "preserve",
            "control_register": "MXCSR",
            "control_value_hex": "0000000000001f80",
            "secondary_control_register": "X87_CW",
            "secondary_control_value_hex": "000000000000027f",
            "numpy_error_policy": (
                "invalid-divide-over-raise_under-ignore"
                if kind == "direct_math"
                else "not-applicable"
            ),
        },
        "numerical_runtime": {
            "identity": numerical_identity,
            "sha256": hashlib.sha256(
                capsule._canonical_profile_document(numerical_identity)
            ).hexdigest(),
        },
        "version_probe": command([launcher, "--version"]),
        "synthetic_preflight": {
            "command": command(preflight_argv, stdout_sha="33" * 32),
            "input_paths": preflight_paths,
            "output_codec": output_codec,
            "output_size": 91,
            "output_sha256": "44" * 32,
            "known_answer_bits_sha256": next(
                member.entry.sha256 for member in members if member.entry.path == known_answer_path
            ),
        },
    }


class EncodingTests(unittest.TestCase):
    def test_streaming_encoder_matches_in_memory_bytes_and_fails_atomic(self):
        members = direct_math_members()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            os.chmod(root, 0o700)
            sources = []
            for index, member in enumerate(members):
                path = root / "source-{:02d}".format(index)
                path.write_bytes(member.payload)
                os.chmod(path, member.entry.mode)
                sources.append(capsule.CapsuleFileSource(member.entry, path))
            output = root / "streamed.cp2cap"
            size, digest = capsule.encode_capsule_file(tuple(sources), output)
            expected = capsule.encode_capsule(members)
            self.assertEqual(output.read_bytes(), expected)
            self.assertEqual(size, len(expected))
            self.assertEqual(digest, hashlib.sha256(expected).hexdigest())
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o444)

            changed = root / "changed-source"
            changed.write_bytes(b"changed")
            os.chmod(changed, members[0].entry.mode)
            bad_sources = list(sources)
            bad_sources[0] = capsule.CapsuleFileSource(members[0].entry, changed)
            failed_output = root / "failed.cp2cap"
            with self.assertRaises(capsule.CapsuleError):
                capsule.encode_capsule_file(tuple(bad_sources), failed_output)
            self.assertFalse(failed_output.exists())

    def test_canonical_archive_and_inventory_have_frozen_known_answers(self):
        members = evaluator_members()
        document = capsule.encode_capsule(members)
        parsed = capsule.parse_capsule_bytes(document)
        self.assertEqual(parsed.entries, tuple(member.entry for member in members))
        self.assertEqual(parsed.payloads, tuple(member.payload for member in members))
        # Constants are independent tripwires over the complete synthetic bytes.
        self.assertEqual(
            hashlib.sha256(document).hexdigest(),
            "c73d833b24876e9f28db62677572bdffbef7f6a7533596a65f590b30c18cb27a",
        )
        self.assertEqual(
            parsed.inventory_sha256,
            "14ea47338617b3d0448a22394f876f5bccaa0bc61b0d1f4786a457cac0ab42b9",
        )

        first = members[0]
        independent_prefix = (
            capsule.CAPSULE_MAGIC + len(members).to_bytes(8, "big")
            + lp(first.entry.path.encode("ascii")) + lp(first.entry.role.encode("ascii"))
            + first.entry.mode.to_bytes(8, "big") + first.entry.size.to_bytes(8, "big")
            + bytes.fromhex(first.entry.sha256) + first.payload
        )
        self.assertTrue(document.startswith(independent_prefix))

    def test_infinite_member_iterable_stops_at_count_bound(self):
        member = capsule.make_capsule_member("x", "python_stdlib", 0o444, b"x")

        def forever():
            while True:
                yield member

        with self.assertRaises(capsule.CapsuleError):
            capsule.encode_capsule(forever())

    def test_parser_rejects_type_truncation_trailing_magic_digest_and_binding(self):
        payload = b"payload"
        digest = hashlib.sha256(payload).hexdigest()
        good = raw_capsule([(b"x", b"python_stdlib", 0o444, payload, digest)])

        class BytesSubclass(bytes):
            pass

        candidates = (
            bytearray(good), BytesSubclass(good), good[:-1], good + b"x",
            b"X" + good[1:], raw_capsule([(b"x", b"python_stdlib", 0o444, payload, ZERO_SHA)]),
        )
        for candidate in candidates:
            with self.assertRaises(capsule.CapsuleError):
                capsule.parse_capsule_bytes(candidate)
        with self.assertRaises(capsule.CapsuleError):
            capsule.parse_capsule_bytes(good, expected_inventory_sha256="ff" * 32)

    def test_parser_rejects_unsafe_unsorted_duplicate_prefix_role_and_mode(self):
        def row(path, role=b"python_stdlib", mode=0o444):
            payload = path
            return path, role, mode, payload, hashlib.sha256(payload).hexdigest()

        cases = (
            [row(b"../x")], [row(b"/x")], [row(b"a b")], [row(b"e\xcc\x81")],
            [row(b"b"), row(b"a")], [row(b"a"), row(b"a")],
            [row(b"a"), row(b"a/b")], [row(b"x", b"unknown")],
            [row(b"x", mode=0o644)], [row(b"x", b"capsule_launcher", 0o444)],
            [row(b"x", b"python_stdlib", 0o555)],
        )
        for rows in cases:
            with self.assertRaises(capsule.CapsuleError):
                capsule.parse_capsule_bytes(raw_capsule(rows))

    def test_member_constructor_rejects_mutable_payload_and_inconsistent_digest(self):
        with self.assertRaises(capsule.CapsuleError):
            capsule.make_capsule_member("x", "python_stdlib", 0o444, bytearray(b"x"))
        entry = capsule.CapsuleEntry(
            "x", "python_stdlib", 0o444, 1, hashlib.sha256(b"x").hexdigest()
        )
        with self.assertRaises(capsule.CapsuleError):
            capsule.CapsuleMember(entry, b"y")


class AcyclicIdentityTests(unittest.TestCase):
    @staticmethod
    def _source_lock(extra=None):
        record = {
            "schema_version": 2,
            "record_type": "cp2_d_minimal_two_capsule_source_lock",
            "checkpoint": "CP2-D",
            "formal_execution_permitted_by_this_record": False,
        }
        if extra is not None:
            record["candidate"] = extra
        return (
            json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        ).encode("ascii")

    @staticmethod
    def _members_with_lock(kind, source_lock):
        original = evaluator_members() if kind == "evaluator" else direct_math_members()
        return tuple(
            capsule.make_capsule_member(
                member.entry.path,
                member.entry.role,
                member.entry.mode,
                source_lock if member.entry.role == "source_lock" else member.payload,
            )
            for member in original
        )

    def _artifacts(self, source_lock):
        result = {}
        for kind in ("direct_math", "evaluator"):
            members = self._members_with_lock(kind, source_lock)
            archive = capsule.encode_capsule(members)
            profile = capsule.validate_capsule_profile(
                profile_for(kind, archive, members)
            )
            result[kind] = (archive, profile.canonical_bytes)
        return result

    def test_embedded_lock_rejects_archive_or_profile_self_identity(self):
        capsule.validate_embedded_source_lock(self._source_lock())
        mutations = (
            {"archive_sha256": "00" * 32},
            {"profile_size_bytes": 1},
            {"expected_capsule_identity": {}},
            {"archive": {"size_bytes": 1, "sha256": "00" * 32}},
            {"profile": {"size": 1, "sha256": "00" * 32}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(capsule.CapsuleError):
                    capsule.validate_embedded_source_lock(
                        self._source_lock(mutation)
                    )

    def test_external_identity_reconstructs_and_rejects_every_output_drift(self):
        source_lock = self._source_lock()
        artifacts = self._artifacts(source_lock)
        identity = capsule.make_expected_capsule_identity(source_lock, artifacts)
        encoded = capsule.expected_capsule_identity_bytes(identity)
        self.assertEqual(
            capsule.expected_capsule_identity_bytes(
                capsule.schema.strict_json_loads(encoded)
            ),
            encoded,
        )
        capsule.verify_expected_capsule_identity(identity, source_lock, artifacts)

        for kind in ("direct_math", "evaluator"):
            for label in ("archive", "profile"):
                changed_identity = json.loads(json.dumps(identity))
                changed_identity["unit_artifact_members"][kind][label]["sha256"] = (
                    "ff" * 32
                )
                with self.subTest(kind=kind, label=label, surface="identity"):
                    with self.assertRaises(capsule.CapsuleError):
                        capsule.verify_expected_capsule_identity(
                            changed_identity, source_lock, artifacts
                        )

                changed_artifacts = dict(artifacts)
                pair = list(changed_artifacts[kind])
                pair[0 if label == "archive" else 1] += b"x"
                changed_artifacts[kind] = tuple(pair)
                with self.subTest(kind=kind, label=label, surface="artifact"):
                    with self.assertRaises(capsule.CapsuleError):
                        capsule.verify_expected_capsule_identity(
                            identity, source_lock, changed_artifacts
                        )


class ProfileTests(unittest.TestCase):
    def test_exact_evaluator_profile_binds_full_surface_and_digest(self):
        profile = capsule.validate_capsule_profile(profile_for())
        self.assertEqual(profile.capsule_kind, "evaluator")
        self.assertEqual(profile.target.machine, "x86_64")
        self.assertEqual(profile.execution.argv_template[0], "${CAPSULE_ROOT}/bin/launcher")
        self.assertEqual(
            tuple(item.path for item in profile.contract_bindings),
            capsule.CONTRACT_BINDING_PATHS,
        )
        self.assertEqual(len(profile.execution.argv_template), 11)
        self.assertEqual(
            profile.native_closure.mapped_paths,
            ("native/ld-linux-x86-64.so.2", "native/libmath.so"),
        )
        self.assertEqual(hashlib.sha256(profile.canonical_bytes).hexdigest(), profile.profile_sha256)
        self.assertNotIn(b"REPLACE", profile.canonical_bytes)

    def test_profile_digest_changes_when_known_answer_binding_changes(self):
        first_record = profile_for()
        second_record = profile_for()
        second_record["synthetic_preflight"]["output_sha256"] = "99" * 32
        first = capsule.validate_capsule_profile(first_record)
        second = capsule.validate_capsule_profile(second_record)
        self.assertEqual(first.archive_sha256, second.archive_sha256)
        self.assertNotEqual(first.profile_sha256, second.profile_sha256)

    def test_direct_math_requires_and_accepts_kind_specific_stack(self):
        evaluator = profile_for()
        evaluator["capsule_kind"] = "direct_math"
        with self.assertRaises(capsule.CapsuleError):
            capsule.validate_capsule_profile(evaluator)
        direct = capsule.validate_capsule_profile(profile_for("direct_math"))
        self.assertEqual(direct.capsule_kind, "direct_math")
        self.assertEqual(direct.entry_module_path, "direct/main.py")
        self.assertEqual(len(direct.execution.argv_template), 5)

    def test_exact_integer_types_fixed_fields_and_target_control_are_enforced(self):
        mutations = []
        for key, value in (
            ("schema_version", 1.0), ("record_type", "other"), ("checkpoint", "CP2-C"),
            ("contract_bindings", []), ("profile_id", None),
            ("capsule_kind", []),
        ):
            changed = profile_for()
            changed[key] = value
            mutations.append(changed)
        changed = profile_for()
        changed["version_probe"]["exit_code"] = 0.0
        mutations.append(changed)
        changed = profile_for()
        changed["target"]["machine"] = "aarch64"
        mutations.append(changed)  # FPCR would be required, not MXCSR.
        changed = profile_for()
        changed["contract_bindings"][0]["path"] = "docs/substitute.md"
        mutations.append(changed)
        changed = profile_for()
        changed["contract_bindings"][0]["size"] = 0
        mutations.append(changed)
        changed = profile_for()
        changed["contract_bindings"][0]["sha256"] = "not-a-sha"
        mutations.append(changed)
        for candidate in mutations:
            with self.assertRaises(capsule.CapsuleError):
                capsule.validate_capsule_profile(candidate)

    def test_missing_extra_unsafe_module_and_command_drift_are_rejected(self):
        mutations = []
        changed = profile_for(); del changed["archive"]; mutations.append(changed)
        changed = profile_for(); changed["authority"] = True; mutations.append(changed)
        changed = profile_for(); changed["entry_point"]["module"] = "../../bad\n--flag"; mutations.append(changed)
        changed = profile_for(); changed["execution"]["argv_template"][0] = "evo_ape"; mutations.append(changed)
        changed = profile_for(); changed["execution"]["argv_template"].append("--align"); mutations.append(changed)
        changed = profile_for(); changed["version_probe"]["argv"] = ["evo_ape", "--version"]; mutations.append(changed)
        changed = profile_for(); changed["synthetic_preflight"]["command"]["argv"].append("--align"); mutations.append(changed)
        changed = profile_for(); changed["synthetic_preflight"]["known_answer_bits_sha256"] = "ff" * 32; mutations.append(changed)
        for candidate in mutations:
            with self.assertRaises(capsule.CapsuleError):
                capsule.validate_capsule_profile(candidate)

    def test_distribution_and_native_closure_digests_are_derived_not_opaque(self):
        mutations = []
        changed = profile_for("direct_math"); changed["distributions"][0]["inventory_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for("direct_math"); changed["distributions"][0]["paths"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["mapped_paths"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["consumers"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["consumers_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["edges_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["needed_edges"].pop(); mutations.append(changed)
        changed = profile_for()
        duplicate_edge = dict(changed["native_closure"]["needed_edges"][0])
        duplicate_edge["provider"] = "native/ld-linux-x86-64.so.2"
        changed["native_closure"]["needed_edges"].append(duplicate_edge)
        changed["native_closure"]["needed_edges"].sort(
            key=lambda item: (item["consumer"], item["needed"], item["provider"])
        )
        changed["native_closure"]["edges_sha256"] = edges_digest(
            changed["native_closure"]["needed_edges"]
        )
        mutations.append(changed)
        changed = profile_for()
        changed["native_closure"]["needed_edges"][0]["provider"] = "native/ld-linux-x86-64.so.2"
        changed["native_closure"]["edges_sha256"] = edges_digest(
            changed["native_closure"]["needed_edges"]
        )
        mutations.append(changed)
        changed = profile_for()
        python_consumer = next(
            item for item in changed["native_closure"]["consumers"]
            if item["path"] == "python/bin/python3.11"
        )
        python_consumer["interpreter"] = "/ambient/ld.so"
        changed["native_closure"]["consumers_sha256"] = consumers_digest(
            changed["native_closure"]["consumers"]
        )
        mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["loader_argv_prefix"][2] = "--library-path-changed"; mutations.append(changed)
        changed = profile_for(); changed["source_lock"]["sha256"] = "ff" * 32; mutations.append(changed)
        for candidate in mutations:
            with self.assertRaises(capsule.CapsuleError):
                capsule.validate_capsule_profile(candidate)

    def test_environment_is_exact_private_and_has_no_injection_escape(self):
        mutations = []
        for key, value in (
            ("PATH", "/ambient/bin"),
            ("PYTHONWARNINGS", "ignore"),
        ):
            changed = profile_for(); changed["environment"]["variables"][key] = value; mutations.append(changed)
        changed = profile_for(); changed["environment"]["variables"]["HOME"] = "${PRIVATE_ROOT}/../ambient"; mutations.append(changed)
        changed = profile_for(); changed["environment"]["variables"]["OMP_NUM_THREADS"] = "2"; mutations.append(changed)
        changed = profile_for(); changed["environment"]["sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["execution"]["hash_seed_policy"] = "PYTHONHASHSEED-env"; mutations.append(changed)
        for candidate in mutations:
            with self.assertRaises(capsule.CapsuleError):
                capsule.validate_capsule_profile(candidate)

    def test_floating_control_words_forbid_ftz_daz_rounding_and_reserved_drift(self):
        for value in (
            "0000000000009fc0",  # MXCSR FTZ+DAZ
            "0000000000007f80",  # non-nearest rounding bits
            "ffffffffffffffff",  # reserved/invalid bits
            "0000000000000000",  # missing exact masks/policy
        ):
            changed = profile_for()
            changed["floating_point"]["control_value_hex"] = value
            with self.subTest(value=value):
                with self.assertRaises(capsule.CapsuleError):
                    capsule.validate_capsule_profile(changed)
        changed = profile_for()
        changed["floating_point"]["secondary_control_value_hex"] = "000000000000037f"
        with self.assertRaises(capsule.CapsuleError):
            capsule.validate_capsule_profile(changed)

    def test_native_search_paths_and_output_sizes_are_resource_and_root_bound(self):
        # glibc's libc.so.6 and libpthread.so.0 are ET_DYN shared objects that
        # intentionally retain the platform PT_INTERP because they are also
        # directly executable diagnostics.  Retain and bind that fact rather
        # than falsely claiming that every shared object lacks PT_INTERP.
        shared_object_interpreter = profile_for()
        native_library = next(
            item
            for item in shared_object_interpreter["native_closure"]["consumers"]
            if item["path"] == "native/libmath.so"
        )
        native_library["interpreter"] = "/lib64/ld-linux-x86-64.so.2"
        shared_object_interpreter["native_closure"]["consumers_sha256"] = consumers_digest(
            shared_object_interpreter["native_closure"]["consumers"]
        )
        capsule.validate_capsule_profile(shared_object_interpreter)

        valid_search = profile_for()
        python_consumer = next(
            item
            for item in valid_search["native_closure"]["consumers"]
            if item["path"] == "python/bin/python3.11"
        )
        python_consumer["runpath"] = ["$ORIGIN/../../native"]
        valid_search["native_closure"]["consumers_sha256"] = consumers_digest(
            valid_search["native_closure"]["consumers"]
        )
        self.assertEqual(
            next(
                item.runpath
                for item in capsule.validate_capsule_profile(valid_search).native_closure.consumers
                if item.path == "python/bin/python3.11"
            ),
            ("$ORIGIN/../../native",),
        )

        static_launcher = profile_for()
        launcher_consumer = next(
            item
            for item in static_launcher["native_closure"]["consumers"]
            if item["path"] == "bin/launcher"
        )
        launcher_consumer["linkage"] = "static-executable"
        launcher_consumer["interpreter"] = "none"
        static_launcher["native_closure"]["consumers_sha256"] = consumers_digest(
            static_launcher["native_closure"]["consumers"]
        )
        capsule.validate_capsule_profile(static_launcher)

        for search_path in ("/ambient/lib", "$ORIGIN/../../../ambient"):
            changed = profile_for()
            changed["native_closure"]["consumers"][0]["rpath"] = [search_path]
            changed["native_closure"]["consumers_sha256"] = consumers_digest(
                changed["native_closure"]["consumers"]
            )
            with self.subTest(search_path=search_path):
                with self.assertRaises(capsule.CapsuleError):
                    capsule.validate_capsule_profile(changed)
        for field, value in (
            ("stdout_size", (1 << 64) - 1),
            ("stderr_size", capsule.MAX_COMMAND_OUTPUT_BYTES + 1),
        ):
            changed = profile_for()
            changed["version_probe"][field] = value
            with self.assertRaises(capsule.CapsuleError):
                capsule.validate_capsule_profile(changed)
        changed = profile_for()
        changed["synthetic_preflight"]["output_size"] = (1 << 64) - 1
        with self.assertRaises(capsule.CapsuleError):
            capsule.validate_capsule_profile(changed)


class StagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cp2-capsule-", dir="/tmp")
        self.root = Path(self.temporary.name)
        os.chmod(str(self.root), 0o700)
        self.document = capsule.encode_capsule(evaluator_members())
        self.archive = self.root / "input.cp2cap"
        self.archive.write_bytes(self.document)
        os.chmod(str(self.archive), 0o600)
        self.profile = profile_for(document=self.document)

    def tearDown(self):
        self.temporary.cleanup()

    def test_stream_stage_twice_is_transport_relocatable_and_fully_bound(self):
        first = capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "stage-a"))
        second = capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "stage-b"))
        self.assertNotEqual(first.root, second.root)
        self.assertEqual(first.profile_sha256, second.profile_sha256)
        self.assertEqual(first.archive_sha256, hashlib.sha256(self.document).hexdigest())
        self.assertEqual(first.capsule_kind, "evaluator")
        for entry in first.entries:
            left = first.root.joinpath(*entry.path.split("/"))
            right = second.root.joinpath(*entry.path.split("/"))
            self.assertEqual(left.read_bytes(), right.read_bytes())
            self.assertEqual(stat.S_IMODE(os.lstat(str(left)).st_mode), entry.mode)
        capsule.revalidate_staged_capsule(str(first.root), first.entries)
        capsule.revalidate_staged_capsule(str(second.root), second.entries)

        # Descriptor mode consumes the exact retained inode.  Moving that
        # inode and coordinating a different same-size pathname cannot redirect
        # staging to the substitute bytes.
        held = os.open(str(self.archive), os.O_RDONLY | os.O_CLOEXEC)
        try:
            displaced = self.root / "input-held.cp2cap"
            self.archive.rename(displaced)
            self.archive.write_bytes(b"x" * len(self.document))
            os.chmod(str(self.archive), 0o600)
            third = capsule.stage_capsule(
                held, self.profile, str(self.root / "stage-held")
            )
            self.assertEqual(
                third.archive_sha256, hashlib.sha256(self.document).hexdigest()
            )
            capsule.revalidate_staged_capsule(third.root, third.entries)
        finally:
            os.close(held)

    def test_raw_profile_is_always_revalidated_and_constructed_profile_is_rejected(self):
        validated = capsule.validate_capsule_profile(self.profile)
        forged = replace(validated, environment=(), environment_sha256="ff" * 32)
        destination = self.root / "forged"
        with self.assertRaises(capsule.CapsuleError):
            capsule.stage_capsule(str(self.archive), forged, str(destination))
        self.assertFalse(destination.exists())

    def test_digest_size_mode_symlink_and_hardlink_fail_before_publication(self):
        cases = []
        wrong_digest = profile_for(document=self.document)
        wrong_digest["archive"]["sha256"] = "ff" * 32
        cases.append((self.archive, wrong_digest))
        wrong_size = profile_for(document=self.document)
        wrong_size["archive"]["size"] += 1
        cases.append((self.archive, wrong_size))

        same_size_drift = self.root / "drift.cp2cap"
        drift = bytearray(self.document); drift[-1] ^= 1
        same_size_drift.write_bytes(drift); os.chmod(str(same_size_drift), 0o600)
        cases.append((same_size_drift, self.profile))
        writable = self.root / "writable.cp2cap"
        writable.write_bytes(self.document); os.chmod(str(writable), 0o622)
        cases.append((writable, self.profile))
        link_source = self.root / "link-source.cp2cap"
        link_source.write_bytes(self.document); os.chmod(str(link_source), 0o600)
        linked = self.root / "linked.cp2cap"; os.link(str(link_source), str(linked))
        cases.append((linked, self.profile))
        symlink = self.root / "symlink.cp2cap"; symlink.symlink_to(self.archive)
        cases.append((symlink, self.profile))

        for index, (source, profile) in enumerate(cases):
            destination = self.root / "failed-{}".format(index)
            with self.assertRaises(capsule.CapsuleError):
                capsule.stage_capsule(str(source), profile, str(destination))
            self.assertFalse(destination.exists())

    def test_destination_is_new_portable_and_parent_is_private_nonsymlink(self):
        existing = self.root / "existing"; existing.mkdir(mode=0o700)
        with self.assertRaises(capsule.CapsuleError):
            capsule.stage_capsule(str(self.archive), self.profile, str(existing))
        with self.assertRaises(capsule.CapsuleError):
            capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "bad name"))

        unsafe_parent = self.root / "unsafe"; unsafe_parent.mkdir(mode=0o755)
        os.chmod(str(unsafe_parent), 0o755)
        with self.assertRaises(capsule.CapsuleError):
            capsule.stage_capsule(str(self.archive), self.profile, str(unsafe_parent / "destination"))
        real_parent = self.root / "real-parent"; real_parent.mkdir(mode=0o700)
        alias = self.root / "alias-parent"; alias.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(capsule.CapsuleError):
            capsule.stage_capsule(str(self.archive), self.profile, str(alias / "destination"))

    def test_hidden_partial_is_removed_and_public_destination_never_appears_on_failure(self):
        destination = self.root / "partial-failure"
        original = capsule._stream_member

        def failing_stream(archive, root_fd, expected, index):
            if index == 1:
                raise capsule.CapsuleError("synthetic stream failure")
            return original(archive, root_fd, expected, index)

        with mock.patch.object(capsule, "_stream_member", side_effect=failing_stream):
            with self.assertRaisesRegex(capsule.CapsuleError, "synthetic stream failure"):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

    def test_empty_partial_is_removed_if_open_fails_or_umask_filters_owner_bits(self):
        original_open = capsule.os.open
        state = {"failed": False}

        def fail_partial_open(path, flags, *args, **kwargs):
            if (
                type(path) is str
                and path.startswith(".cp2-stage-")
                and not state["failed"]
            ):
                state["failed"] = True
                raise PermissionError("synthetic partial open failure")
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(capsule.os, "open", side_effect=fail_partial_open):
            with self.assertRaises(capsule.CapsuleError):
                capsule.stage_capsule(
                    str(self.archive), self.profile, str(self.root / "open-failure")
                )
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

        previous_umask = os.umask(0o777)
        try:
            with self.assertRaisesRegex(capsule.CapsuleError, "mode was filtered"):
                capsule.stage_capsule(
                    str(self.archive), self.profile, str(self.root / "umask-failure")
                )
        finally:
            os.umask(previous_umask)
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

    def test_atomic_noreplace_publication_failure_cleans_hidden_partial(self):
        destination = self.root / "publish-failure"
        with mock.patch.object(
            capsule, "_rename_noreplace", side_effect=capsule.CapsuleError("synthetic publish race")
        ):
            with self.assertRaisesRegex(capsule.CapsuleError, "synthetic publish race"):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

    def test_postrename_parent_fsync_failure_removes_only_exact_published_capsule(self):
        destination = self.root / "postrename-fsync-failure"
        real_fsync = capsule._publication_parent_fsync
        calls = 0

        def fail_second_parent_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic postrename parent fsync failure")
            real_fsync(descriptor)

        with mock.patch.object(
            capsule, "_publication_parent_fsync", side_effect=fail_second_parent_fsync
        ):
            with self.assertRaisesRegex(capsule.CapsuleError, "capsule staging failed"):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertEqual(calls, 2)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

    def test_prerename_parent_fsync_interruption_cleans_exact_hidden_partial(self):
        destination = self.root / "prerename-fsync-interruption"
        with mock.patch.object(
            capsule,
            "_publication_parent_fsync",
            side_effect=KeyboardInterrupt("synthetic pre-rename fsync interruption"),
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "pre-rename"):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertFalse(destination.exists())
        self.assertFalse(
            any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir())
        )

    def test_caller_reconciles_partial_after_rollback_failure(self):
        destination = self.root / "partial-rollback-failure"
        original_stream = capsule._stream_member

        def fail_stream(archive, root_fd, expected, index):
            if index == 1:
                raise capsule.CapsuleError("synthetic member failure")
            return original_stream(archive, root_fd, expected, index)

        with mock.patch.object(capsule, "_stream_member", side_effect=fail_stream), mock.patch.object(
            capsule,
            "_remove_tree_at",
            side_effect=capsule.CapsuleError("synthetic rollback failure"),
        ):
            with self.assertRaises(capsule.CapsulePublicationIncident) as caught:
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        incident = caught.exception
        self.assertEqual(incident.authoritative_state, "partial")
        self.assertFalse(destination.exists())
        self.assertTrue((self.root / incident.partial_name).is_dir())
        self.assertEqual(capsule.reconcile_capsule_publication(incident), "partial")
        self.assertFalse((self.root / incident.partial_name).exists())

    def test_caller_reconciles_destination_after_rollback_failure(self):
        destination = self.root / "destination-rollback-failure"
        real_parent_fsync = capsule._publication_parent_fsync
        fsync_calls = 0

        def fail_second_parent_fsync(descriptor):
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError("synthetic post-rename fsync failure")
            real_parent_fsync(descriptor)

        with mock.patch.object(
            capsule,
            "_publication_parent_fsync",
            side_effect=fail_second_parent_fsync,
        ), mock.patch.object(
            capsule,
            "_remove_tree_at",
            side_effect=capsule.CapsuleError("synthetic destination rollback failure"),
        ):
            with self.assertRaises(capsule.CapsulePublicationIncident) as caught:
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        incident = caught.exception
        self.assertEqual(fsync_calls, 2)
        self.assertEqual(incident.authoritative_state, "destination")
        self.assertTrue(destination.is_dir())
        self.assertEqual(capsule.reconcile_capsule_publication(incident), "destination")
        self.assertFalse(destination.exists())

    def test_indeterminate_publication_incident_never_authorizes_deletion(self):
        destination = self.root / "indeterminate-destination"
        diverted = self.root / "diverted-exact-capsule"
        real_rename = capsule._rename_noreplace

        def divert_after_rename(parent_fd, old_name, new_name):
            real_rename(parent_fd, old_name, new_name)
            os.rename(
                new_name,
                diverted.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir(new_name, 0o700, dir_fd=parent_fd)
            raise KeyboardInterrupt("synthetic namespace substitution")

        with mock.patch.object(
            capsule, "_rename_noreplace", side_effect=divert_after_rename
        ):
            with self.assertRaises(capsule.CapsulePublicationIncident) as caught:
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        incident = caught.exception
        self.assertEqual(incident.authoritative_state, "indeterminate")
        self.assertTrue(destination.is_dir())
        self.assertTrue(diverted.is_dir())
        with self.assertRaisesRegex(capsule.CapsuleError, "indeterminate"):
            capsule.reconcile_capsule_publication(incident)
        self.assertTrue(destination.is_dir())
        self.assertTrue(diverted.is_dir())

    def test_return_boundary_rejects_same_root_inode_coordinated_content_substitution(self):
        destination = self.root / "return-content-substitution"
        original = capsule._publication_return_revalidate
        invoked = False

        def substitute_then_revalidate(root_fd, entries):
            nonlocal invoked
            if not invoked:
                invoked = True
                target = destination / "python/lib/python3.11/cp2_equivalent_evaluator.py"
                target.unlink()
                target.write_bytes(b"X" * len(b"def main(): pass\n"))
                os.chmod(str(target), 0o444)
            return original(root_fd, entries)

        with mock.patch.object(
            capsule,
            "_publication_return_revalidate",
            side_effect=substitute_then_revalidate,
        ):
            with self.assertRaises(capsule.CapsuleError):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertTrue(invoked)
        self.assertFalse(destination.exists())
        self.assertFalse(
            any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir())
        )

    def test_interruption_after_completed_capsule_rename_is_reconciled_and_cleaned(self):
        destination = self.root / "rename-interruption"
        real_rename = capsule._rename_noreplace

        def interrupt_after_rename(parent_fd, old_name, new_name):
            real_rename(parent_fd, old_name, new_name)
            raise KeyboardInterrupt("synthetic interruption after rename")

        with mock.patch.object(
            capsule, "_rename_noreplace", side_effect=interrupt_after_rename
        ):
            with self.assertRaisesRegex(KeyboardInterrupt, "after rename"):
                capsule.stage_capsule(str(self.archive), self.profile, str(destination))
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.name.startswith(".cp2-stage-") for path in self.root.iterdir()))

    def test_revalidator_detects_extra_mode_byte_symlink_and_hardlink_drift(self):
        staged = capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "stage-check"))
        extra = staged.root / "extra"; extra.write_bytes(b"extra")
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        extra.unlink()

        target = staged.root / "python/lib/python3.11/cp2_equivalent_evaluator.py"
        os.chmod(str(target), 0o644)
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        os.chmod(str(target), 0o444)
        target.unlink(); target.write_bytes(b"drift"); os.chmod(str(target), 0o444)
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        target.unlink(); target.symlink_to(staged.root / "python/bin/python3.11")
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        target.unlink(); os.link(str(staged.root / "python/bin/python3.11"), str(target))
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)

    def test_revalidator_rejects_directory_swap_after_held_child_open(self):
        staged = capsule.stage_capsule(
            str(self.archive), self.profile, str(self.root / "stage-race")
        )
        original_open = capsule._open_child_directory
        state = {"swapped": False}

        def swap_after_open(parent_fd, path):
            descriptor = original_open(parent_fd, path)
            if path == "fixtures" and not state["swapped"]:
                state["swapped"] = True
                original = staged.root / "fixtures"
                held = staged.root / "fixtures-held"
                original.rename(held)
                original.mkdir(mode=0o700)
                for source in held.iterdir():
                    payload = source.read_bytes()
                    if source.name == "estimate.tum":
                        payload = b"X" * len(payload)
                    (original / source.name).write_bytes(payload)
                    os.chmod(str(original / source.name), 0o444)
            return descriptor

        with mock.patch.object(capsule, "_open_child_directory", side_effect=swap_after_open):
            with self.assertRaises(capsule.CapsuleError) as caught:
                capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        self.assertTrue(state["swapped"], str(caught.exception))

    def test_cleanup_rejects_descendant_swap_without_deleting_victim(self):
        partial = self.root / "cleanup-partial"
        partial.mkdir(mode=0o700)
        original_child = partial / "sub"
        original_child.mkdir(mode=0o700)
        (original_child / "created").write_bytes(b"created by stager")
        os.chmod(str(original_child / "created"), 0o600)
        victim = self.root / "victim"
        victim.mkdir(mode=0o700)
        (victim / "important").write_bytes(b"must survive")
        os.chmod(str(victim / "important"), 0o600)

        parent_fd = os.open(
            str(self.root),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        expected_identity = capsule._inode_identity(os.lstat(str(partial)))
        original_open = capsule._open_child_directory
        state = {"swapped": False}

        def swap_before_open(directory_fd, path):
            if path == "sub" and not state["swapped"]:
                state["swapped"] = True
                original_child.rename(partial / "sub-held")
                victim.rename(original_child)
            return original_open(directory_fd, path)

        try:
            with mock.patch.object(
                capsule, "_open_child_directory", side_effect=swap_before_open
            ):
                with self.assertRaisesRegex(capsule.CapsuleError, "identity changed"):
                    capsule._remove_tree_at(
                        parent_fd, partial.name, expected_identity
                    )
        finally:
            os.close(parent_fd)

        self.assertTrue(state["swapped"])
        self.assertEqual((partial / "sub/important").read_bytes(), b"must survive")

    def test_cleanup_rejects_regular_swap_without_deleting_victim(self):
        partial = self.root / "cleanup-file-partial"
        partial.mkdir(mode=0o700)
        created = partial / "created"
        created.write_bytes(b"created by stager")
        os.chmod(str(created), 0o600)
        victim = self.root / "important"
        victim.write_bytes(b"must survive")
        os.chmod(str(victim), 0o600)

        parent_fd = os.open(
            str(self.root),
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        expected_identity = capsule._inode_identity(os.lstat(str(partial)))
        original_open = capsule.os.open
        state = {"swapped": False}

        def swap_before_open(path, flags, *args, **kwargs):
            if (
                path == "created"
                and flags & os.O_PATH
                and not state["swapped"]
            ):
                state["swapped"] = True
                created.rename(partial / "created-held")
                victim.rename(created)
            return original_open(path, flags, *args, **kwargs)

        try:
            with mock.patch.object(capsule.os, "open", side_effect=swap_before_open):
                with self.assertRaisesRegex(capsule.CapsuleError, "identity changed"):
                    capsule._remove_tree_at(
                        parent_fd, partial.name, expected_identity
                    )
        finally:
            os.close(parent_fd)

        self.assertTrue(state["swapped"])
        self.assertEqual(created.read_bytes(), b"must survive")

    def test_missing_descriptor_capability_fails_closed(self):
        with mock.patch.object(capsule.os, "name", "nt"):
            with self.assertRaises(capsule.CapsuleError):
                capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "no-capability"))


if __name__ == "__main__":
    unittest.main()
