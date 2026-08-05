#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free tests for the CP2-E root launcher and closure identity."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
REPO_ROOT = CP2_DIRECTORY.parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cp2_timing_production_identity as identity  # noqa: E402
import cp2_timing_profile as profile_codec  # noqa: E402
import cp2_timing_reversibility_client as reversibility_client  # noqa: E402
from test_cp2_timing_evidence import valid_profile_value  # noqa: E402


SOURCE = CP2_DIRECTORY / "cp2_timing_root_launcher.c"
SUDOERS = REPO_ROOT / "packaging/cp2e/schurvio-cp2e.sudoers"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def production_profile_value():
    profile = valid_profile_value()
    profile["privileged_helper"].update({
        "trusted_root": "/opt/schurvio-cp2e",
        "root_launcher_path": "/opt/schurvio-cp2e/bin/cp2e-helper",
        "journal_directory": "/var/lib/schurvio-cp2e",
        "invocation_argv": [
            "/usr/bin/sudo", "-n", "-C", "4", "--",
            "/opt/schurvio-cp2e/bin/cp2e-helper",
        ],
    })
    profile["privileged_helper"]["plan_sha256"] = (
        identity.profile_plan_sha256(profile)
    )
    return profile


def build_bound_launcher(output, profile=None):
    profile = valid_profile_value() if profile is None else profile
    definitions = {
        "CP2E_HELPER_SHA256": sha256_file(
            CP2_DIRECTORY / "cp2_timing_privileged_helper.py"
        ),
        "CP2E_BACKEND_SHA256": sha256_file(
            CP2_DIRECTORY / "cp2_timing_privileged_backend.py"
        ),
        "CP2E_PROFILE_CODEC_SHA256": sha256_file(
            CP2_DIRECTORY / "cp2_timing_profile.py"
        ),
        "CP2E_PYTHON_SHA256": sha256_file("/usr/bin/python3.8"),
        "CP2E_PROFILE_PLAN_SHA256": identity.profile_plan_sha256(profile),
    }
    argv = [
        "/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
        "-pedantic", "-fstack-protector-strong", "-D_FORTIFY_SOURCE=2",
        "-fPIE", "-pie", "-Wl,-z,relro,-z,now",
    ]
    argv.extend(
        '-D{}="{}"'.format(key, value)
        for key, value in sorted(definitions.items())
    )
    argv.extend(("-o", str(output), str(SOURCE)))
    subprocess.run(argv, check=True, stdin=subprocess.DEVNULL)
    os.chmod(output, 0o755)


class RootLauncherTests(unittest.TestCase):
    def test_internal_sha256_matches_fips_known_answer(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            root = Path(directory)
            wrapper = root / "sha-kat.c"
            executable = root / "sha-kat"
            wrapper.write_text(
                '#define main cp2e_launcher_main\n'
                '#include "{}"\n'.format(str(SOURCE).replace('"', '\\"'))
                + '#undef main\n'
                + 'int main(void) {\n'
                + '  static const unsigned char input[] = "abc";\n'
                + '  static const unsigned char expected[32] = {'
                + ','.join(
                    '0x' + hashlib.sha256(b"abc").hexdigest()[index:index + 2]
                    for index in range(0, 64, 2)
                )
                + '};\n'
                + '  struct sha256_context context; unsigned char result[32];\n'
                + '  sha256_initialize(&context);\n'
                + '  sha256_update(&context, input, 3);\n'
                + '  sha256_finish(&context, result);\n'
                + '  return memcmp(result, expected, 32) == 0 ? 0 : 1;\n'
                + '}\n',
                encoding="ascii",
            )
            subprocess.run([
                "/usr/bin/cc", "-std=c11", "-O2", "-Wall", "-Wextra",
                "-Werror", "-pedantic", "-o", str(executable), str(wrapper),
            ], check=True, stdin=subprocess.DEVNULL)
            subprocess.run([str(executable)], check=True, stdin=subprocess.DEVNULL)

    def test_bound_launcher_compiles_and_refuses_unprivileged_execution(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            executable = Path(directory) / "cp2e-helper"
            build_bound_launcher(executable)
            completed = subprocess.run(
                [str(executable)], stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            self.assertEqual(completed.returncode, 78)
            self.assertEqual(completed.stdout, b"")
            self.assertIn(b"real/effective uid zero", completed.stderr)
            receipt = {
                "checkpoint": "CP2-E", "formal_execution_locked": True,
                "record_type": "cp2e_data_free_reversibility_receipt",
                "schema_version": 2, "transaction_status": "passed",
            }
            encoded = json.dumps(
                receipt, allow_nan=False, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            ).encode("utf-8") + b"\n"
            self.assertEqual(
                reversibility_client.validate_receipt_bytes(encoded), receipt,
            )
            with self.assertRaises(
                reversibility_client.ReversibilityClientError,
            ):
                reversibility_client.validate_receipt_bytes(b"{}\n")

    def test_sudoers_candidate_has_valid_parser_syntax(self):
        completed = subprocess.run(
            ["/usr/sbin/visudo", "-c", "-f", str(SUDOERS)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_candidate_identity_and_profile_bindings_are_exact(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            executable = Path(directory) / "cp2e-helper"
            build_bound_launcher(executable)
            candidate = identity.candidate_identity(REPO_ROOT, executable)
            self.assertEqual(
                candidate["status"],
                "candidate_missing_root_held_profile_binding",
            )
            self.assertEqual(
                candidate["bindings"]["installed_closure_file_count"], 4,
            )
            profile = production_profile_value()
            # Rebuild against the production-root policy projection.  Recursive
            # source/launcher digests do not alter this plan digest.
            build_bound_launcher(executable, profile)
            candidate = identity.candidate_identity(REPO_ROOT, executable)
            draft_path = Path(directory) / "draft-profile.yaml"
            final_path = Path(directory) / "bound-profile.yaml"
            draft_path.write_bytes(profile_codec.canonical_profile_bytes(profile))
            bind_receipt = identity.bind_profile_candidate(
                REPO_ROOT, draft_path, executable, final_path,
            )
            self.assertTrue(bind_receipt["profile_binding_valid"])
            profile = json.loads(final_path.read_bytes())
            rebound = identity.candidate_identity(
                REPO_ROOT, executable, profile_value=profile,
                profile_path=final_path,
            )
            self.assertTrue(rebound["profile_binding_valid"])
            self.assertTrue(rebound["launcher_compiled_binding_valid"])
            self.assertEqual(
                rebound["status"],
                "candidate_profile_binding_valid_privileged_feasibility_unproved",
            )
            profile["privileged_helper"]["source_sha256"] = "0" * 64
            self.assertFalse(identity.candidate_identity(
                REPO_ROOT, executable, profile_value=profile,
            )["profile_binding_valid"])
            behavioral_drift = json.loads(final_path.read_bytes())
            behavioral_drift["limits"]["maximum_process_seconds"] += 1
            behavioral_drift["privileged_helper"]["plan_sha256"] = (
                identity.profile_plan_sha256(behavioral_drift)
            )
            self.assertFalse(identity.candidate_identity(
                REPO_ROOT, executable, profile_value=behavioral_drift,
            )["profile_binding_valid"])
            with self.assertRaises(FileExistsError):
                identity.bind_profile_candidate(
                    REPO_ROOT, draft_path, executable, final_path,
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
