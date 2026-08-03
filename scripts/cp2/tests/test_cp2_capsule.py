#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Wholly synthetic protecting tests for the proposed CP2-D capsule boundary."""

from __future__ import annotations

from dataclasses import replace
import hashlib
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
CLARIFICATION_COMMIT = "ab" * 20


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
        ("bin/python", "python_interpreter", 0o555, b"synthetic-python-elf"),
        ("evo/dist-info/METADATA", "evo_distribution_metadata", 0o444, b"Name: evo\nVersion: 1.31.1\n"),
        ("evo/main.py", "evo_module", 0o444, b"def main(): pass\n"),
        ("fixtures/estimate.tum", "preflight_fixture", 0o444, b"1 0 0 0 0 0 0 1\n"),
        (
            "fixtures/expected-stats-bits.json",
            "preflight_known_answer",
            0o444,
            b'{"rmse_bits":"0000000000000000"}\n',
        ),
        ("fixtures/ground-truth.tum", "preflight_fixture", 0o444, b"1 0 0 0 0 0 0 1\n"),
        ("lib/ld-linux.so", "native_loader", 0o555, b"synthetic-loader"),
        ("lib/libmath.so", "native_library", 0o444, b"synthetic-native-library"),
        ("licenses/GPL-3.0.txt", "license_notice", 0o444, b"GPL-3.0-or-later\n"),
        ("python/dependency.py", "python_dependency", 0o444, b"VALUE = 1\n"),
        ("python/stdlib.py", "python_stdlib", 0o444, b"# synthetic stdlib\n"),
    )
    return tuple(capsule.make_capsule_member(*row) for row in rows)


def direct_math_members():
    rows = (
        ("bin/launcher", "capsule_launcher", 0o555, b"synthetic direct launcher\n"),
        ("bin/python", "python_interpreter", 0o555, b"synthetic-python-elf"),
        (
            "direct/known-answers.json",
            "direct_math_known_answer",
            0o444,
            b'{"rmse_bits":"419279a74590331c"}\n',
        ),
        ("direct/main.py", "direct_math_module", 0o444, b"def main(): pass\n"),
        ("fixtures/request.json", "preflight_fixture", 0o444, b"{}\n"),
        ("lib/ld-linux.so", "native_loader", 0o555, b"synthetic-loader"),
        ("lib/libnumpy.so", "native_library", 0o444, b"synthetic-numpy-library"),
        ("licenses/BSD-3-Clause.txt", "license_notice", 0o444, b"BSD-3-Clause\n"),
        ("numpy/__init__.py", "numpy_module", 0o444, b"__version__ = 'approved'\n"),
        ("numpy/core.so", "numpy_extension", 0o444, b"synthetic-extension"),
        ("numpy/dist-info/METADATA", "numpy_distribution_metadata", 0o444, b"Name: numpy\n"),
        ("python/stdlib.py", "python_stdlib", 0o444, b"# synthetic stdlib\n"),
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
    needed_by_consumer = {
        path: ([Path(item).name for item in dependency_paths] if path == "bin/python" else [])
        for path in native_consumer_paths
    }
    consumers = []
    for path in native_consumer_paths:
        executable = by_path[path].role in ("capsule_launcher", "python_interpreter")
        if executable:
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
                "interpreter": "lib/ld-linux.so" if executable else "none",
                "rpath": [],
                "runpath": ["$ORIGIN/../lib"] if path == "bin/python" else [],
                "soname": Path(path).name if by_path[path].role in ("native_library", "python_extension") else "none",
                "needed": sorted(needed_by_consumer[path]),
            }
        )
    edges = [
        {"consumer": "bin/python", "needed": Path(path).name, "provider": path}
        for path in dependency_paths
    ]
    edges.sort(key=lambda item: (item["consumer"], item["needed"], item["provider"]))
    native_edges = tuple(
        capsule.NativeEdge(item["consumer"], item["needed"], item["provider"])
        for item in edges
    )
    return {
        "loader_path": "lib/ld-linux.so",
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


def profile_for(kind="evaluator", document=None):
    members = evaluator_members() if kind == "evaluator" else direct_math_members()
    if document is None:
        document = capsule.encode_capsule(members)
    environment = dict(capsule.EXACT_ENVIRONMENT)
    launcher = "${CAPSULE_ROOT}/bin/launcher"
    if kind == "evaluator":
        distribution_specs = (
            ("evo", "1.31.1", ["evo/dist-info/METADATA", "evo/main.py"]),
            ("support", "1", ["python/dependency.py"]),
        )
        entry_point = {
            "launcher_path": "bin/launcher",
            "interpreter_path": "bin/python",
            "module_path": "evo/main.py",
            "module": "evo.main",
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
        output_codec = "evo_result_zip_v1"
        known_answer_path = "fixtures/expected-stats-bits.json"
    else:
        distribution_specs = (
            (
                "numpy",
                "approved",
                ["numpy/__init__.py", "numpy/core.so", "numpy/dist-info/METADATA"],
            ),
        )
        entry_point = {
            "launcher_path": "bin/launcher",
            "interpreter_path": "bin/python",
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
        "schema_version": 1,
        "record_type": "cp2_d_capsule_profile",
        "checkpoint": "CP2-D",
        "clarification_commit": CLARIFICATION_COMMIT,
        "profile_id": "synthetic-{}-x86_64".format(kind),
        "capsule_kind": kind,
        "target": {
            "os": "linux",
            "machine": "x86_64",
            "elf_class": 64,
            "endianness": "little",
            "libc_abi": "synthetic-glibc",
            "loader_abi": "synthetic-ld-linux",
            "cpu_dispatch_policy": "synthetic-fixed-core",
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
            "numpy_error_policy": "raise",
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
    def test_canonical_archive_and_inventory_have_frozen_known_answers(self):
        members = evaluator_members()
        document = capsule.encode_capsule(members)
        parsed = capsule.parse_capsule_bytes(document)
        self.assertEqual(parsed.entries, tuple(member.entry for member in members))
        self.assertEqual(parsed.payloads, tuple(member.payload for member in members))
        # Constants are independent tripwires over the complete synthetic bytes.
        self.assertEqual(
            hashlib.sha256(document).hexdigest(),
            "e572673728231b6a1c3dac44c1c50cdc5f2cb51014a68681599a0b259fe177a5",
        )
        self.assertEqual(
            parsed.inventory_sha256,
            "4e5b93c2dd58a6af44fbc11b76d516fc187482311db24c238b6b12b6e0b945c6",
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


class ProfileTests(unittest.TestCase):
    def test_exact_evaluator_profile_binds_full_surface_and_digest(self):
        profile = capsule.validate_capsule_profile(profile_for())
        self.assertEqual(profile.capsule_kind, "evaluator")
        self.assertEqual(profile.target.machine, "x86_64")
        self.assertEqual(profile.execution.argv_template[0], "${CAPSULE_ROOT}/bin/launcher")
        self.assertEqual(len(profile.execution.argv_template), 11)
        self.assertEqual(profile.native_closure.mapped_paths, ("lib/ld-linux.so", "lib/libmath.so"))
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
            ("clarification_commit", "A" * 40), ("profile_id", None),
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
        changed = profile_for(); changed["distributions"][0]["inventory_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["distributions"][0]["paths"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["mapped_paths"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["consumers"].pop(); mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["consumers_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["edges_sha256"] = "ff" * 32; mutations.append(changed)
        changed = profile_for(); changed["native_closure"]["needed_edges"].pop(); mutations.append(changed)
        changed = profile_for()
        duplicate_edge = dict(changed["native_closure"]["needed_edges"][0])
        duplicate_edge["provider"] = "lib/ld-linux.so"
        changed["native_closure"]["needed_edges"].append(duplicate_edge)
        changed["native_closure"]["needed_edges"].sort(
            key=lambda item: (item["consumer"], item["needed"], item["provider"])
        )
        changed["native_closure"]["edges_sha256"] = edges_digest(
            changed["native_closure"]["needed_edges"]
        )
        mutations.append(changed)
        changed = profile_for()
        changed["native_closure"]["needed_edges"][0]["provider"] = "lib/ld-linux.so"
        changed["native_closure"]["edges_sha256"] = edges_digest(
            changed["native_closure"]["needed_edges"]
        )
        mutations.append(changed)
        changed = profile_for()
        python_consumer = next(
            item for item in changed["native_closure"]["consumers"]
            if item["path"] == "bin/python"
        )
        python_consumer["runpath"] = []
        changed["native_closure"]["consumers_sha256"] = consumers_digest(
            changed["native_closure"]["consumers"]
        )
        mutations.append(changed)
        changed = profile_for()
        changed["distributions"][0]["paths"], changed["distributions"][1]["paths"] = (
            changed["distributions"][1]["paths"], changed["distributions"][0]["paths"]
        )
        for distribution in changed["distributions"]:
            distribution["inventory_sha256"] = subset_digest(
                evaluator_members(), distribution["paths"]
            )
        mutations.append(changed)
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
        valid_search = profile_for()
        python_consumer = next(
            item
            for item in valid_search["native_closure"]["consumers"]
            if item["path"] == "bin/python"
        )
        python_consumer["runpath"] = ["$ORIGIN/../lib"]
        valid_search["native_closure"]["consumers_sha256"] = consumers_digest(
            valid_search["native_closure"]["consumers"]
        )
        self.assertEqual(
            capsule.validate_capsule_profile(valid_search).native_closure.consumers[1].runpath,
            ("$ORIGIN/../lib",),
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

    def test_revalidator_detects_extra_mode_byte_symlink_and_hardlink_drift(self):
        staged = capsule.stage_capsule(str(self.archive), self.profile, str(self.root / "stage-check"))
        extra = staged.root / "extra"; extra.write_bytes(b"extra")
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        extra.unlink()

        target = staged.root / "evo/main.py"
        os.chmod(str(target), 0o644)
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        os.chmod(str(target), 0o444)
        target.unlink(); target.write_bytes(b"drift"); os.chmod(str(target), 0o444)
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        target.unlink(); target.symlink_to(staged.root / "bin/python")
        with self.assertRaises(capsule.CapsuleError):
            capsule.revalidate_staged_capsule(str(staged.root), staged.entries)
        target.unlink(); os.link(str(staged.root / "bin/python"), str(target))
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
            if path == "evo" and not state["swapped"]:
                state["swapped"] = True
                original = staged.root / "evo"
                held = staged.root / "evo-held"
                original.rename(held)
                original.mkdir(mode=0o700)
                (original / "dist-info").mkdir(mode=0o700)
                metadata = (held / "dist-info/METADATA").read_bytes()
                (original / "dist-info/METADATA").write_bytes(metadata)
                os.chmod(str(original / "dist-info/METADATA"), 0o444)
                original_main = (held / "main.py").read_bytes()
                (original / "main.py").write_bytes(b"X" * len(original_main))
                os.chmod(str(original / "main.py"), 0o444)
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
