#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free protection for the detached CP2-D verifier/capsule seams."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(CP2_DIRECTORY))
sys.path.insert(0, str(TEST_DIRECTORY))

import cp2_capsule as capsule  # noqa: E402
import cp2_evaluator_result as evaluator_result  # noqa: E402
import test_cp2_capsule as capsule_fixture  # noqa: E402
import test_cp2_evo_result as evaluator_fixture  # noqa: E402
import verify_report as verifier  # noqa: E402


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _replace_immutable(path, payload):
    if path.exists():
        os.chmod(str(path), 0o600)
    path.write_bytes(payload)
    os.chmod(str(path), 0o444)


class _SyntheticUnitIdentity:
    def __init__(self, root):
        self.root = root
        self.unit = root / "unit"
        self.capsules = self.unit / "capsules"
        self.capsules.mkdir(parents=True, mode=0o700)
        self.source_lock = capsule_fixture.AcyclicIdentityTests._source_lock()
        helper = capsule_fixture.AcyclicIdentityTests()
        self.artifacts = helper._artifacts(self.source_lock)
        self.identity = capsule.make_expected_capsule_identity(
            self.source_lock, self.artifacts
        )
        self.identity_bytes = capsule.expected_capsule_identity_bytes(self.identity)
        self.manifest = {}
        for kind, pair in self.artifacts.items():
            for suffix, payload in zip(("cp2cap", "profile.json"), pair):
                relative = "capsules/{}.{}".format(kind, suffix)
                path = self.unit / relative
                _replace_immutable(path, payload)
                self.manifest[relative] = _sha(payload)
        self.archive = root / "source_snapshot.tar"
        self.write_source_archive(self.source_lock)
        entries = [
            {
                "path": "project/cp2_capsule_expected_identity.json",
                "mode": 0o100644,
                "size": len(self.identity_bytes),
                "sha256": _sha(self.identity_bytes),
            },
            {
                "path": "project/cp2_capsule_source_lock.json",
                "mode": 0o100644,
                "size": len(self.source_lock),
                "sha256": _sha(self.source_lock),
            },
        ]
        # The synthetic profile fixture binds each governing document to this
        # exact one-byte/constant-digest source-context identity.  The detached
        # unit validator consumes the identities, never the document contents.
        entries.extend(
            {
                "path": relative,
                "mode": 0o100644,
                "size": 1,
                "sha256": "ab" * 32,
            }
            for relative in capsule.CONTRACT_BINDING_PATHS
        )
        self.common = {
            "source_context": {"entries": entries},
            "source_archive": self.archive,
            "unit_artifact": self.unit,
        }

    def write_source_archive(self, source_lock):
        if self.archive.exists():
            os.chmod(str(self.archive), 0o600)
        with tarfile.open(str(self.archive), "w:") as stream:
            for relative, payload in (
                (
                    "project/cp2_capsule_expected_identity.json",
                    self.identity_bytes,
                ),
                ("project/cp2_capsule_source_lock.json", source_lock),
            ):
                member = tarfile.TarInfo(relative)
                member.mode = 0o644
                member.size = len(payload)
                stream.addfile(member, io.BytesIO(payload))
        os.chmod(str(self.archive), 0o444)


class SourceBindingTests(unittest.TestCase):
    def test_source_bound_capsule_loader_rejects_preloaded_schema_alias(self):
        code = (
            "import sys;sys.path.insert(0,{path!r});"
            "import verify_report as v;"
            "v._actual_install_self_test_module_source_binding();"
            "sys.modules['cp2_schema']=object();"
            "\ntry:v._actual_load_module('cp2_capsule')\n"
            "except v.ActualVerificationError:raise SystemExit(0)\n"
            "raise SystemExit(1)"
        ).format(path=str(CP2_DIRECTORY))
        completed = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", "-c", code],
            cwd="/tmp",
            env={"LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


class UnitCapsuleIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-detached-unit-identity-", dir="/tmp"
        )
        self.root = Path(self.temporary.name)
        os.chmod(str(self.root), 0o700)
        self.fixture = _SyntheticUnitIdentity(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _validate(self):
        with mock.patch.object(
            verifier, "_actual_load_module", return_value=capsule
        ):
            return verifier._actual_validate_unit_capsule_identity(
                self.fixture.common, self.fixture.manifest
            )

    def test_exact_external_identity_accepts_both_synthetic_capsules(self):
        result = self._validate()
        self.assertEqual(set(result), {"direct_math", "evaluator"})
        self.assertEqual(result["direct_math"]["profile"].capsule_kind, "direct_math")
        self.assertEqual(result["evaluator"]["profile"].capsule_kind, "evaluator")

    def test_external_identity_rejects_archive_mutation(self):
        path = self.fixture.unit / "capsules/direct_math.cp2cap"
        _replace_immutable(path, path.read_bytes() + b"mutation")
        with self.assertRaises(verifier.ActualVerificationError):
            self._validate()

    def test_external_identity_rejects_profile_mutation(self):
        path = self.fixture.unit / "capsules/evaluator.profile.json"
        _replace_immutable(path, path.read_bytes() + b"\n")
        with self.assertRaises(verifier.ActualVerificationError):
            self._validate()

    def test_external_identity_rejects_source_lock_mutation(self):
        self.fixture.write_source_archive(self.fixture.source_lock + b"mutation")
        with self.assertRaises(verifier.ActualVerificationError):
            self._validate()


class DetachedReplayTests(unittest.TestCase):
    RESPONSE = b'{"synthetic":"response"}\n'
    REQUEST = b'{"synthetic":"request"}\n'
    VERSION_STDOUT = b"version-ok\n"
    PREFLIGHT_STDOUT = b"preflight!\n"
    PREFLIGHT_RESPONSE = b'{"preflight":true}\n'
    EVALUATOR_VERSION_STDOUT = b"evaluator-version-ok\n"
    EVALUATOR_PREFLIGHT_STDOUT = b"evaluator-preflight-ok\n"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-detached-replay-", dir="/tmp"
        )
        self.root = Path(self.temporary.name)
        os.chmod(str(self.root), 0o700)
        self.fixture = _SyntheticUnitIdentity(self.root)
        direct_profile_record = json.loads(
            self.fixture.artifacts["direct_math"][1].decode("ascii")
        )
        direct_profile_record["version_probe"]["stdout_size"] = len(
            self.VERSION_STDOUT
        )
        direct_profile_record["version_probe"]["stdout_sha256"] = _sha(
            self.VERSION_STDOUT
        )
        direct_profile_record["synthetic_preflight"]["command"][
            "stdout_size"
        ] = len(self.PREFLIGHT_STDOUT)
        direct_profile_record["synthetic_preflight"]["command"][
            "stdout_sha256"
        ] = _sha(self.PREFLIGHT_STDOUT)
        direct_profile_record["synthetic_preflight"]["output_size"] = len(
            self.PREFLIGHT_RESPONSE
        )
        direct_profile_record["synthetic_preflight"]["output_sha256"] = _sha(
            self.PREFLIGHT_RESPONSE
        )
        self.direct_profile = capsule.validate_capsule_profile(
            direct_profile_record
        )
        direct_profile_bytes = self.direct_profile.canonical_bytes
        direct_profile_path = (
            self.fixture.unit / "capsules/direct_math.profile.json"
        )
        _replace_immutable(direct_profile_path, direct_profile_bytes)
        self.fixture.manifest["capsules/direct_math.profile.json"] = _sha(
            direct_profile_bytes
        )
        evaluator_profile_bytes = self.fixture.artifacts["evaluator"][1]
        self.evaluator_profile = capsule.validate_capsule_profile(
            json.loads(evaluator_profile_bytes.decode("ascii"))
        )
        self.evaluator_result = evaluator_fixture.full_result_archive(
            (0.0, 0.125, 0.25, 0.5)
        )
        parsed_evaluator_result = evaluator_result.parse_evaluator_result_archive(
            self.evaluator_result
        )
        self.evaluator_known = (
            json.dumps(
                {
                    "archive_sha256": _sha(self.evaluator_result),
                    "error_bits": list(parsed_evaluator_result.error_bits),
                    "rmse_bits": __import__("struct").pack(
                        ">d", parsed_evaluator_result.statistics.rmse
                    ).hex(),
                },
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("ascii")
        evaluator_version = replace(
            self.evaluator_profile.version_probe,
            stdout_size=len(self.EVALUATOR_VERSION_STDOUT),
            stdout_sha256=_sha(self.EVALUATOR_VERSION_STDOUT),
        )
        evaluator_preflight_command = replace(
            self.evaluator_profile.synthetic_preflight.command,
            stdout_size=len(self.EVALUATOR_PREFLIGHT_STDOUT),
            stdout_sha256=_sha(self.EVALUATOR_PREFLIGHT_STDOUT),
        )
        evaluator_preflight = replace(
            self.evaluator_profile.synthetic_preflight,
            command=evaluator_preflight_command,
            input_paths=(
                "fixtures/estimate.tum",
                "fixtures/expected-result-bits.json",
                "fixtures/ground-truth.tum",
            ),
            output_size=len(self.evaluator_result),
            output_sha256=_sha(self.evaluator_result),
            known_answer_bits_sha256=_sha(self.evaluator_known),
        )
        # The detached replay consumes a profile already validated by the
        # independently tested source/unit identity seam.  These focused tests
        # replace that seam with a minimal canonical profile document while
        # retaining every field used by the evaluator replay itself.
        detached_evaluator_profile_bytes = b"{}\n"
        self.detached_evaluator_profile = replace(
            self.evaluator_profile,
            version_probe=evaluator_version,
            synthetic_preflight=evaluator_preflight,
            canonical_bytes=detached_evaluator_profile_bytes,
            profile_sha256=_sha(detached_evaluator_profile_bytes),
        )
        evaluator_profile_path = (
            self.fixture.unit / "capsules/evaluator.profile.json"
        )
        _replace_immutable(
            evaluator_profile_path, detached_evaluator_profile_bytes
        )
        self.fixture.manifest["capsules/evaluator.profile.json"] = _sha(
            detached_evaluator_profile_bytes
        )
        self.validated = {
            "direct_math": {
                "archive_path": self.fixture.unit / "capsules/direct_math.cp2cap",
                "profile_path": (
                    self.fixture.unit / "capsules/direct_math.profile.json"
                ),
                "profile_bytes": direct_profile_bytes,
                "profile": self.direct_profile,
            },
            "evaluator": {
                "archive_path": self.fixture.unit / "capsules/evaluator.cp2cap",
                "profile_path": (
                    self.fixture.unit / "capsules/evaluator.profile.json"
                ),
                "profile_bytes": detached_evaluator_profile_bytes,
                "profile": self.detached_evaluator_profile,
            },
        }
        self.common = {
            "unit_artifact": self.fixture.unit,
            "unit_anchor": {"manifest_sha256": "11" * 32},
        }

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _fake_capsule_module(known_answer=None):
        fake = types.SimpleNamespace()
        fake.CapsuleError = capsule.CapsuleError

        def stage_capsule(_archive, _profile, destination):
            destination.mkdir(mode=0o700)
            binary = destination / "bin"
            binary.mkdir(mode=0o700)
            launcher = binary / "launcher"
            launcher.write_bytes(b"synthetic held launcher\n")
            os.chmod(str(launcher), 0o555)
            if known_answer is not None:
                fixtures = destination / "fixtures"
                fixtures.mkdir(mode=0o700)
                for name, payload in (
                    ("ground-truth.tum", b"1 0 0 0 0 0 0 1\n"),
                    ("estimate.tum", b"1 0 0 0 0 0 0 1\n"),
                    ("expected-result-bits.json", known_answer),
                ):
                    path = fixtures / name
                    path.write_bytes(payload)
                    os.chmod(str(path), 0o444)
            return types.SimpleNamespace(root=destination, entries=())

        fake.stage_capsule = stage_capsule
        fake.revalidate_staged_capsule = lambda _root, _entries: None

        class SealedCapsuleSandbox:
            def __init__(self, staged):
                self.staged = staged

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return None

            def read_member(self, relative, maximum):
                if (
                    relative != "fixtures/expected-result-bits.json"
                    or known_answer is None
                    or len(known_answer) > maximum
                ):
                    raise capsule.CapsuleError("synthetic sealed member differs")
                return known_answer

        fake.SealedCapsuleSandbox = SealedCapsuleSandbox
        return fake

    def _replay(
        self,
        wait_result,
        *,
        mutate_request=False,
        response=None,
        preflight_response=None,
        preflight_stream_mismatch=False,
    ):
        response_payload = self.RESPONSE if response is None else response
        preflight_payload = (
            self.PREFLIGHT_RESPONSE
            if preflight_response is None
            else preflight_response
        )
        fake_capsule = self._fake_capsule_module()
        self.expectation_labels = []

        def run_sealed(
            _sandbox,
            command,
            _environment,
            _timeout,
            label,
            *,
            read_only_inputs=None,
            output_destination=None,
            output_maximum=None,
            expected_streams=None,
        ):
            self.expectation_labels.append(label)
            if label.endswith("version probe"):
                if len(self.expectation_labels) != 1:
                    verifier._actual_fail("detached version probe order differs")
                stdout = self.VERSION_STDOUT
                output = None
            elif label.endswith("known-answer preflight"):
                if len(self.expectation_labels) != 2:
                    verifier._actual_fail("detached preflight order differs")
                if preflight_stream_mismatch:
                    verifier._actual_fail(label + " retained stream identity differs")
                stdout = self.PREFLIGHT_STDOUT
                output = preflight_payload
            elif label == "detached direct-math capsule":
                if len(self.expectation_labels) != 3:
                    verifier._actual_fail("detached direct execution order differs")
                if wait_result[1]:
                    verifier._actual_fail(label + " timed out")
                if wait_result[2]:
                    verifier._actual_fail(label + " retained a process-group descendant")
                if wait_result[0] != 0:
                    verifier._actual_fail(label + " returned nonzero")
                if read_only_inputs != {
                    "/private/work/request.json": self.REQUEST
                }:
                    verifier._actual_fail("detached sealed request differs")
                if mutate_request:
                    # A caller-side replacement cannot alter the immutable
                    # bytes already handed to the sealed invocation.
                    request_copy = b'{"mutated":true}\n'
                    self.assertNotEqual(
                        request_copy, read_only_inputs["/private/work/request.json"]
                    )
                stdout = b""
                output = response_payload
            else:
                verifier._actual_fail("unexpected detached capsule expectation")
            stderr = b""
            if expected_streams is not None and (
                len(stdout) != expected_streams.stdout_size
                or _sha(stdout) != expected_streams.stdout_sha256
                or len(stderr) != expected_streams.stderr_size
                or _sha(stderr) != expected_streams.stderr_sha256
            ):
                verifier._actual_fail(label + " retained stream identity differs")
            if output_destination is None:
                self.assertIsNone(output_maximum)
                self.assertIsNone(output)
            else:
                self.assertIsInstance(output_maximum, int)
            return stdout, stderr, output

        with mock.patch.object(
            verifier, "_actual_load_module", return_value=fake_capsule
        ), mock.patch.object(
            verifier,
            "_actual_scan_and_verify_manifest",
            return_value=(self.fixture.manifest, {}, {}),
        ), mock.patch.object(
            verifier,
            "_actual_validate_unit_capsule_identity",
            return_value=self.validated,
        ), mock.patch.object(
            verifier,
            "_actual_run_sealed_capsule",
            side_effect=run_sealed,
        ):
            return verifier._actual_replay_direct_capsule(
                self.common, self.REQUEST
            )

    def _replay_evaluator(
        self,
        *,
        result=None,
        known_answer=None,
        preflight_stream_mismatch=False,
    ):
        result_payload = self.evaluator_result if result is None else result
        known_payload = self.evaluator_known if known_answer is None else known_answer
        fake_capsule = self._fake_capsule_module(known_payload)
        self.expectation_labels = []

        def run_sealed(
            _sandbox,
            command,
            _environment,
            _timeout,
            label,
            *,
            read_only_inputs=None,
            output_destination=None,
            output_maximum=None,
            expected_streams=None,
        ):
            del read_only_inputs
            self.expectation_labels.append(label)
            if label == "detached evaluator version probe":
                if len(self.expectation_labels) != 1:
                    verifier._actual_fail("detached evaluator version order differs")
                stdout = self.EVALUATOR_VERSION_STDOUT
                output = None
            elif label == "detached evaluator known-answer preflight":
                if len(self.expectation_labels) != 2:
                    verifier._actual_fail("detached evaluator preflight order differs")
                if preflight_stream_mismatch:
                    verifier._actual_fail(label + " retained stream identity differs")
                stdout = self.EVALUATOR_PREFLIGHT_STDOUT
                output = result_payload
            else:
                verifier._actual_fail("unexpected detached evaluator expectation")
            stderr = b""
            if (
                expected_streams is not None
                and (
                    len(stdout) != expected_streams.stdout_size
                    or _sha(stdout) != expected_streams.stdout_sha256
                    or len(stderr) != expected_streams.stderr_size
                    or _sha(stderr) != expected_streams.stderr_sha256
                )
            ):
                verifier._actual_fail(label + " retained stream identity differs")
            if output_destination is None:
                self.assertIsNone(output_maximum)
                self.assertIsNone(output)
            else:
                self.assertIsInstance(output_maximum, int)
            return stdout, stderr, output

        with mock.patch.object(
            verifier,
            "_actual_load_module",
            side_effect=lambda name: (
                fake_capsule if name == "cp2_capsule" else evaluator_result
            ),
        ), mock.patch.object(
            verifier,
            "_actual_scan_and_verify_manifest",
            return_value=(self.fixture.manifest, {}, {}),
        ), mock.patch.object(
            verifier,
            "_actual_validate_unit_capsule_identity",
            return_value=self.validated,
        ), mock.patch.object(
            verifier,
            "_actual_run_sealed_capsule",
            side_effect=run_sealed,
        ):
            return verifier._actual_replay_evaluator_capsule_preflight(self.common)

    def test_successful_replay_returns_exact_capsule_response_and_environments(self):
        response, direct_environment, evaluator_environment = self._replay(
            (0, False, False)
        )
        self.assertEqual(response, self.RESPONSE)
        self.assertEqual(direct_environment, self.direct_profile.environment_sha256)
        self.assertEqual(
            evaluator_environment, self.evaluator_profile.environment_sha256
        )
        self.assertEqual(
            self.expectation_labels,
            [
                "detached direct-math version probe",
                "detached direct-math known-answer preflight",
                "detached direct-math capsule",
            ],
        )

    def test_replay_rejects_preflight_output_mismatch(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "known-answer response differs",
        ):
            self._replay(
                (0, False, False),
                preflight_response=b'{"wrong":true}\n',
            )

    def test_replay_rejects_preflight_stream_mismatch(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "retained stream identity differs",
        ):
            self._replay(
                (0, False, False),
                preflight_stream_mismatch=True,
            )

    def test_replay_rejects_nonzero_exit(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError, "returned nonzero"
        ):
            self._replay((7, False, False))

    def test_replay_rejects_timeout(self):
        with self.assertRaisesRegex(verifier.ActualVerificationError, "timed out"):
            self._replay((0, True, False))

    def test_replay_binds_request_bytes_in_sealed_input(self):
        response, _, _ = self._replay(
            (0, False, False), mutate_request=True
        )
        self.assertEqual(response, self.RESPONSE)

    def test_evaluator_replay_runs_only_version_and_synthetic_preflight(self):
        environment_sha256 = self._replay_evaluator()
        self.assertEqual(
            environment_sha256,
            self.detached_evaluator_profile.environment_sha256,
        )
        self.assertEqual(
            self.expectation_labels,
            [
                "detached evaluator version probe",
                "detached evaluator known-answer preflight",
            ],
        )

    def test_evaluator_replay_rejects_preflight_output_substitution(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "known-answer result differs",
        ):
            self._replay_evaluator(result=b"substituted evaluator result")

    def test_evaluator_replay_rejects_known_answer_substitution(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "known-answer identity differs",
        ):
            self._replay_evaluator(
                known_answer=self.evaluator_known.replace(
                    _sha(self.evaluator_result).encode("ascii"), b"00" * 32
                )
            )

    def test_evaluator_replay_rejects_preflight_stream_substitution(self):
        with self.assertRaisesRegex(
            verifier.ActualVerificationError,
            "retained stream identity differs",
        ):
            self._replay_evaluator(preflight_stream_mismatch=True)

    def test_retained_response_must_equal_source_bound_replay(self):
        artifact = self.root / "artifact"
        direct = artifact / "direct_math"
        direct.mkdir(parents=True, mode=0o700)
        request_path = direct / "request.json"
        response_path = direct / "response.json"
        _replace_immutable(request_path, self.REQUEST)
        _replace_immutable(response_path, self.RESPONSE)
        report = {
            "direct_math_request_sha256": _sha(self.REQUEST),
            "direct_math_response_sha256": _sha(self.RESPONSE),
        }
        manifest = {
            "direct_math/request.json": _sha(self.REQUEST),
            "direct_math/response.json": _sha(self.RESPONSE),
        }
        fake_codec = types.SimpleNamespace(MAX_DOCUMENT_BYTES=1024)
        with mock.patch.object(
            verifier, "_actual_load_module", return_value=fake_codec
        ), mock.patch.object(
            verifier,
            "_actual_replay_direct_capsule",
            return_value=(b'{"substituted":true}\n', "22" * 32, "33" * 32),
        ), mock.patch.object(
            verifier,
            "_actual_replay_evaluator_capsule_preflight",
            return_value="33" * 32,
        ):
            with self.assertRaisesRegex(
                verifier.ActualVerificationError, "not byte-identical"
            ):
                verifier._actual_sequence_shared_math(
                    artifact, manifest, report, {}, {}
                )


if __name__ == "__main__":
    unittest.main()
