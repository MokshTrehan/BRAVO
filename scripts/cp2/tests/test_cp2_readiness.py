#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic, artifact-free tests for the CP2 readiness engine."""

from __future__ import annotations

import copy
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_readiness as readiness  # noqa: E402

VERIFY_REPORT_PATH = CP2_DIRECTORY / "verify_report.py"
VERIFY_REPORT_SPEC = importlib.util.spec_from_file_location(
    "cp2_readiness_test_verify_report", VERIFY_REPORT_PATH
)
verify_report = importlib.util.module_from_spec(VERIFY_REPORT_SPEC)
sys.modules[VERIFY_REPORT_SPEC.name] = verify_report
VERIFY_REPORT_SPEC.loader.exec_module(verify_report)


def run_git(root, *arguments):
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root.parent / "git-author-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    Path(environment["HOME"]).mkdir(mode=0o700, exist_ok=True)
    return subprocess.check_output(
        ["/usr/bin/git", *arguments],
        cwd=str(root),
        env=environment,
        stderr=subprocess.STDOUT,
    )


def passing_result(entrypoint, suffix="default", names=None):
    cases = []
    names = tuple(names or readiness.COMMON_SELF_TEST_CASES)
    for index, name in enumerate(names):
        negative = name != "valid_minimal_fixture"
        cases.append({
            "index": index,
            "name": name,
            "expected_rejection": negative,
            "observed_rejection": negative,
            "passed": True,
        })
    return {
        "schema_version": 1,
        "record_type": "self_test_result",
        "entrypoint": entrypoint,
        "temporary_root": "/tmp/cp2-readiness-selftest-" + suffix,
        "bag_provider_calls": 0,
        "cases": cases,
        "case_count": len(cases),
        "passed": True,
    }


def python_entrypoint_text(entrypoint, result, verifier=False):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    actual = "raise SystemExit(0)" if verifier else "raise SystemExit(2)"
    return """#!/usr/bin/python3
import sys
if sys.argv[1:] == [\"--self-test\"]:
    print({encoded!r})
    raise SystemExit(0)
{actual}
""".format(encoded=encoded, actual=actual)


def shell_entrypoint_text(result):
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    return """#!/usr/bin/env bash
set -Eeuo pipefail
[[ \"$#\" -eq 1 && \"$1\" == \"--self-test\" ]] || exit 2
printf '%s\\n' {quoted}
""".format(quoted="'" + encoded.replace("'", "'\\''") + "'")


class SyntheticRepository:
    """A wholly synthetic Git repository; no workspace byte is copied."""

    def __init__(self, parent):
        self.root = Path(parent) / "repo"
        self.root.mkdir()
        run_git(self.root, "init", "-q")
        run_git(self.root, "config", "user.name", "CP2 Synthetic Test")
        run_git(self.root, "config", "user.email", "cp2@example.invalid")
        run_git(self.root, "config", "core.filemode", "true")
        run_git(self.root, "symbolic-ref", "HEAD", "refs/heads/" + readiness.EXPECTED_BRANCH)
        self.registry_sentinel = b"DO_NOT_PARSE_THIS_DECOY_REGISTRY:\xff:\x00\n"
        registry = self.root / "project/datasets.yaml"
        registry.parent.mkdir(parents=True)
        registry.write_bytes(self.registry_sentinel)
        for index, relative in enumerate(readiness.ENTRYPOINTS):
            path = self.root.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            result = passing_result(
                relative, str(index), readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT[relative]
            )
            if relative.endswith("run_unit_gate.sh"):
                path.write_text(shell_entrypoint_text(result), encoding="utf-8")
                path.chmod(0o755)
            else:
                path.write_text(
                    python_entrypoint_text(
                        relative, result, verifier=relative.endswith("verify_report.py")
                    ),
                    encoding="utf-8",
                )
                path.chmod(0o644)
        run_git(self.root, "add", "--", ".")
        run_git(self.root, "commit", "-q", "-m", "synthetic readiness fixture")
        self.commit = run_git(self.root, "rev-parse", "HEAD").decode("ascii").strip()
        self.tree = run_git(self.root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()

    def unit_artifact(self, parent):
        artifact = Path(parent) / "unit-artifact"
        artifact.mkdir()
        report = {
            "schema_version": 1,
            "source": {"commit": self.commit, "tree": self.tree},
        }
        report_bytes = (json.dumps(report, sort_keys=True) + "\n").encode("utf-8")
        (artifact / "cp2_report.json").write_bytes(report_bytes)
        manifest = "{}  cp2_report.json\n".format(hashlib.sha256(report_bytes).hexdigest())
        (artifact / "SHA256SUMS").write_text(manifest, encoding="utf-8")
        return artifact, hashlib.sha256(manifest.encode("utf-8")).hexdigest()


class RealPrevalidatedFixture:
    """Synthetic source/data with the actual detached unit verifier and artifact."""

    def __init__(self, parent):
        self.root = Path(parent) / "real-repo"
        self.root.mkdir()
        verify_report.create_synthetic_repo(self.root)
        self.registry_sentinel = b"DO_NOT_PARSE_SYNTHETIC_REGISTRY:\xff:\x00\n"
        self.commit = verify_report.git_text(self.root, "rev-parse", "HEAD")
        self.tree = verify_report.git_text(self.root, "rev-parse", "HEAD^{tree}")

    @staticmethod
    def _make_writable(root):
        paths = [root] + list(root.rglob("*"))
        for path in sorted(paths, key=lambda item: len(item.parts)):
            try:
                mode = stat.S_IMODE(path.lstat().st_mode)
                if not path.is_symlink():
                    path.chmod(mode | stat.S_IRWXU)
            except FileNotFoundError:
                pass

    def unit_artifact(self, parent):
        partial = Path(parent) / "real-unit-partial"
        with contextlib.redirect_stdout(io.StringIO()):
            verify_report.create_synthetic_artifact(partial, self.root)
            verify_report.assemble_unit_report(partial, self.root, allow_synthetic=True)
        artifact = Path(parent) / "real-unit-final"
        verify_report.finalize_staging_noreplace(
            partial, artifact, repo_root=self.root, allow_synthetic=True
        )
        verify_report.set_synthetic_tree_modes(artifact, True)

        workspace = json.loads((artifact / "workspace.json").read_text(encoding="utf-8"))
        workspace["ceres_source_commit"] = verify_report.CERES_COMMIT
        workspace["ceres_archive_argv"][-1] = verify_report.CERES_COMMIT
        verify_report.write_json_fixture(artifact / "workspace.json", workspace)

        dependency_path = artifact / verify_report.DEPENDENCY_INVENTORY_NAME
        dependency = json.loads(dependency_path.read_text(encoding="utf-8"))
        dependency["ceres"]["source_commit"] = verify_report.CERES_COMMIT
        verify_report.write_json_fixture(dependency_path, dependency)

        report_path = artifact / verify_report.REPORT_NAME
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["workspace"] = workspace
        report["dependency_inventory"] = dependency
        report["third_party_sources_and_notices"]["ceres"]["commit"] = (
            verify_report.CERES_COMMIT
        )
        verify_report.write_json_fixture(report_path, report)
        verify_report.reseal_synthetic(artifact)
        verify_report.set_synthetic_tree_modes(artifact, False)

        # Detached step 8 is artifact-only. A retained extracted build tree
        # would itself be an untracked preauthorization control surface.
        build = self.root / "build"
        self._make_writable(build)
        shutil.rmtree(str(build))
        manifest = (artifact / verify_report.MANIFEST_NAME).read_bytes()
        return artifact, hashlib.sha256(manifest).hexdigest()


class SnapshotCodecTests(unittest.TestCase):
    def test_snapshot_golden_vector_is_byte_exact(self):
        payload = readiness.encode_snapshot(
            "build",
            True,
            (
                readiness.SnapshotEntry(
                    "x", "f", 0o640, 3, hashlib.sha256(b"abc").hexdigest()
                ),
            ),
        )
        self.assertEqual(
            payload.hex(),
            "536368757256494f2d4350322d72656164696e6573732d736e617073686f742d"
            "76310000000000000000056275696c6401000000000000000100000000000000"
            "01786600000000000001a00000000000000003ba7816bf8f01cfea414140de5d"
            "ae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "9a0338d371cfc5ccaff95e0bdf8d03c000f1f1e995561e189ba35da3b4ab36e2",
        )

    def test_snapshot_known_shape_roundtrips_and_rejects_corruption(self):
        entries = (
            readiness.SnapshotEntry("a", "d", 0o755, 0, "0" * 64),
            readiness.SnapshotEntry("a/x.bin", "f", 0o640, 3, hashlib.sha256(b"abc").hexdigest()),
            readiness.SnapshotEntry("z", "l", 0o777, 1, hashlib.sha256(b"q").hexdigest()),
        )
        payload = readiness.encode_snapshot("build", True, entries)
        self.assertTrue(payload.startswith(readiness.SNAPSHOT_DOMAIN))
        self.assertEqual(readiness.parse_snapshot(payload), ("build", True, entries))
        with self.assertRaises(readiness.ReadinessError):
            readiness.parse_snapshot(payload + b"x")
        with self.assertRaises(readiness.ReadinessError):
            readiness.encode_snapshot("build", True, tuple(reversed(entries)))
        with self.assertRaises(readiness.ReadinessError):
            readiness.encode_snapshot("build", False, entries)

    def test_source_snapshot_binds_exact_entrypoint_order(self):
        entries = []
        identities = []
        for index, path in enumerate(readiness.ENTRYPOINTS):
            digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
            entries.append(readiness.SnapshotEntry(path, "f", 0o100644, len(path), digest))
            identities.append(readiness.EntrypointIdentity(path, "{:040x}".format(index + 1), digest, 0o100644))
        entries.sort(key=lambda item: item.path.encode("utf-8"))
        identity = readiness.SourceIdentity("1" * 40, "2" * 40, "3" * 40, b"", tuple(identities))
        payload = readiness.encode_source_snapshot("source", entries, identity)
        tag, decoded_entries, decoded_identity = readiness.parse_source_snapshot(payload)
        self.assertEqual(tag, "source")
        self.assertEqual(decoded_entries, tuple(entries))
        self.assertEqual(decoded_identity, identity)
        broken = bytearray(payload)
        broken[-1] ^= 1
        with self.assertRaises(readiness.ReadinessError):
            readiness.parse_source_snapshot(bytes(broken))

    def test_filesystem_snapshot_never_follows_symlink(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-snapshot-", dir="/tmp") as temporary:
            root = Path(temporary) / "build"
            root.mkdir()
            (root / "file").write_bytes(b"opaque")
            (root / "link").symlink_to("file")
            payload = readiness._snapshot_directory(root, "build")
            _, exists, entries = readiness.parse_snapshot(payload)
            self.assertTrue(exists)
            by_path = {entry.path: entry for entry in entries}
            self.assertEqual(by_path["file"].sha256, hashlib.sha256(b"opaque").hexdigest())
            self.assertEqual(by_path["link"].sha256, hashlib.sha256(b"file").hexdigest())
            fifo = root / "fifo"
            os.mkfifo(str(fifo))
            with self.assertRaises(readiness.ReadinessError):
                readiness._snapshot_directory(root, "build")


class SelfTestContractTests(unittest.TestCase):
    def test_exact_result_passes_and_mutations_fail(self):
        entrypoint = readiness.ENTRYPOINTS[0]
        valid = passing_result(entrypoint, "validator")
        self.assertEqual(
            readiness.validate_self_test_result(valid, entrypoint, readiness.COMMON_SELF_TEST_CASES),
            valid,
        )
        mutations = []
        extra = copy.deepcopy(valid)
        extra["extra"] = 1
        mutations.append(extra)
        calls = copy.deepcopy(valid)
        calls["bag_provider_calls"] = 1
        mutations.append(calls)
        skipped = copy.deepcopy(valid)
        skipped["cases"][1]["observed_rejection"] = False
        mutations.append(skipped)
        reordered = copy.deepcopy(valid)
        reordered["cases"][1], reordered["cases"][2] = reordered["cases"][2], reordered["cases"][1]
        mutations.append(reordered)
        identity = copy.deepcopy(valid)
        identity["record_type"] = "forged"
        mutations.append(identity)
        wrong_entrypoint = copy.deepcopy(valid)
        wrong_entrypoint["entrypoint"] = readiness.ENTRYPOINTS[1]
        mutations.append(wrong_entrypoint)
        escaped_root = copy.deepcopy(valid)
        escaped_root["temporary_root"] = "/var/tmp/cp2-readiness-selftest"
        mutations.append(escaped_root)
        bool_count = copy.deepcopy(valid)
        bool_count["case_count"] = True
        mutations.append(bool_count)
        bool_index = copy.deepcopy(valid)
        bool_index["cases"][0]["index"] = False
        mutations.append(bool_index)
        case_extra = copy.deepcopy(valid)
        case_extra["cases"][0]["extra"] = None
        mutations.append(case_extra)
        wrong_expected = copy.deepcopy(valid)
        wrong_expected["cases"][1]["expected_rejection"] = False
        mutations.append(wrong_expected)
        aggregate = copy.deepcopy(valid)
        aggregate["passed"] = False
        mutations.append(aggregate)
        for value in mutations:
            with self.subTest(value=value):
                with self.assertRaises(readiness.ReadinessError):
                    readiness.validate_self_test_result(
                        value, entrypoint, readiness.COMMON_SELF_TEST_CASES
                    )

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        with self.assertRaises(readiness.ReadinessError):
            readiness.strict_json_loads(b'{"x":1,"x":2}')
        with self.assertRaises(readiness.ReadinessError):
            readiness.strict_json_loads(b'{"x":NaN}')
        with self.assertRaises(readiness.ReadinessError):
            readiness.strict_json_loads(b'{"x":1e999}')

    def test_process_group_timeout_is_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-timeout-", dir="/tmp") as temporary:
            root = Path(temporary)
            script = root / "hang.py"
            child_pid_path = root / "child.pid"
            script.write_text(
                """#!/usr/bin/python3
import os, subprocess, time
p = subprocess.Popen(['/usr/bin/python3','-c','import time; time.sleep(60)'])
open({pid!r}, 'w').write(str(p.pid))
time.sleep(60)
""".format(pid=str(child_pid_path)),
                encoding="utf-8",
            )
            identities = []
            for relative in readiness.ENTRYPOINTS:
                identities.append(readiness.EntrypointIdentity(relative, "1" * 40, "2" * 64, 0o100644))
            fake_repo = root / "repo"
            for relative in readiness.ENTRYPOINTS:
                target = fake_repo.joinpath(*relative.split("/"))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(str(script), str(target))
                if relative.endswith(".sh"):
                    target.chmod(0o755)
            expected = readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT

            def descriptor_provider(relative):
                flags = os.O_RDONLY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                return os.open(str(fake_repo.joinpath(*relative.split("/"))), flags)

            with self.assertRaises(readiness.ReadinessError):
                readiness.run_self_tests(
                    fake_repo,
                    identities,
                    expected,
                    root / "attachments",
                    0.2,
                    descriptor_provider=descriptor_provider,
                )
            if child_pid_path.exists():
                child_pid = int(child_pid_path.read_text(encoding="ascii"))
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("timed-out self-test descendant survived process-group kill")

    def test_isolated_python_ignores_sitecustomize_pythonpath_and_sibling_hashlib(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-python-isolation-", dir="/tmp") as temporary:
            root = Path(temporary)
            fake_repo = root / "repo"
            poison_directory = fake_repo / "scripts/cp2"
            poison_directory.mkdir(parents=True)
            marker = root / "poison-imported"
            (poison_directory / "sitecustomize.py").write_text(
                "open({!r}, 'ab').write(b'sitecustomize\\n')\n".format(str(marker)),
                encoding="utf-8",
            )
            (poison_directory / "hashlib.py").write_text(
                "open({!r}, 'ab').write(b'hashlib\\n')\n".format(str(marker)),
                encoding="utf-8",
            )

            poison_environment = dict(os.environ)
            poison_environment["PYTHONPATH"] = str(poison_directory)
            subprocess.run(
                ["/usr/bin/python3", "-c", "import hashlib"],
                cwd=str(poison_directory),
                env=poison_environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
            )
            self.assertIn(b"sitecustomize", marker.read_bytes())
            self.assertIn(b"hashlib", marker.read_bytes())
            marker.unlink()

            identities = []
            for index, relative in enumerate(readiness.ENTRYPOINTS):
                target = fake_repo.joinpath(*relative.split("/"))
                result = passing_result(
                    relative,
                    "isolation-{}".format(index),
                    readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT[relative],
                )
                if relative.endswith(".sh"):
                    target.write_text(shell_entrypoint_text(result), encoding="utf-8")
                    target.chmod(0o755)
                else:
                    content = python_entrypoint_text(
                        relative, result, verifier=relative.endswith("verify_report.py")
                    ).replace(
                        "import sys\n",
                        "import sys\nimport hashlib\nassert callable(hashlib.sha256)\n",
                        1,
                    )
                    target.write_text(content, encoding="utf-8")
                    target.chmod(0o644)
                identities.append(
                    readiness.EntrypointIdentity(
                        relative,
                        "{:040x}".format(index + 1),
                        hashlib.sha256(target.read_bytes()).hexdigest(),
                        0o100755 if relative.endswith(".sh") else 0o100644,
                    )
                )

            def descriptor_provider(relative):
                flags = os.O_RDONLY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                return os.open(
                    str(fake_repo.joinpath(*relative.split("/"))), flags
                )

            with mock.patch.dict(
                readiness.SELF_TEST_ENVIRONMENT,
                {"PYTHONPATH": str(poison_directory)},
            ):
                records, _ = readiness.run_self_tests(
                    fake_repo,
                    identities,
                    readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT,
                    root / "attachments",
                    timeout_seconds=10.0,
                    descriptor_provider=descriptor_provider,
                )
            self.assertFalse(marker.exists())
            for record in records:
                if record["path"].endswith(".py"):
                    self.assertEqual(
                        record["argv"][:3], ["/usr/bin/python3", "-I", "-B"]
                    )


class LockTests(unittest.TestCase):
    def test_lock_identity_and_contention(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-lock-", dir="/tmp") as temporary:
            lock = Path("/tmp") / ("cp2-lock-" + Path(temporary).name)
            descriptor = readiness.acquire_data_lock(lock)
            try:
                with self.assertRaises(readiness.ReadinessError):
                    readiness.acquire_data_lock(lock)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)
                lock.unlink()
            target = Path(temporary) / "target"
            target.write_text("x", encoding="ascii")
            lock.symlink_to(target)
            with self.assertRaises(readiness.ReadinessError):
                readiness.acquire_data_lock(lock)

    def test_lock_rejects_special_hardlinked_and_wrong_mode_paths(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-lock-shape-", dir="/tmp") as temporary:
            root = Path(temporary)
            for case in ("fifo", "hardlink", "mode"):
                with self.subTest(case=case):
                    lock = Path("/tmp") / ("cp2-{}-{}".format(case, root.name))
                    if case == "fifo":
                        os.mkfifo(str(lock), 0o600)
                    elif case == "hardlink":
                        target = root / "hardlink-target"
                        target.write_bytes(b"")
                        target.chmod(0o600)
                        os.link(str(target), str(lock))
                    else:
                        lock.write_bytes(b"")
                        lock.chmod(0o644)
                    try:
                        with self.assertRaises(readiness.ReadinessError):
                            readiness.acquire_data_lock(lock)
                    finally:
                        lock.unlink()

    def test_postacquisition_lock_replacement_is_detected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-lock-replace-", dir="/tmp") as temporary:
            lock = Path("/tmp") / ("cp2-replaced-" + Path(temporary).name)
            descriptor = readiness.acquire_data_lock(lock)
            repository = mock.Mock()
            private = Path(tempfile.mkdtemp(prefix="schurvio-cp2-readiness-", dir="/tmp"))
            authorization = readiness.ReadinessAuthorization(
                record={},
                attachments={},
                unit_verification_command={},
                frozen_unit_artifact=private / "unit-artifact-frozen",
                temporary_root=private,
                lock_fd=descriptor,
                lock_path=lock,
                repository=repository,
                private_tree_seal=None,
            )
            lock.unlink()
            lock.write_bytes(b"")
            lock.chmod(0o600)
            try:
                with self.assertRaises(readiness.ReadinessError):
                    authorization.revalidate()
            finally:
                authorization.close()
                lock.unlink()
            repository.close.assert_called_once_with()

        # A successful barrier retains a descriptor-held exact inventory of its
        # private root, frozen unit copy, and readiness attachments.  Mutating
        # any one of those after the barrier must fail before the sole registry
        # read is reached.  Cleanup must still remove the descriptor-held private
        # tree after descendant mutation; a substituted symlink's victim must be
        # preserved rather than followed.
        mutations = (
            "private_root_mode",
            "private_root_substitution",
            "frozen_unit_bytes",
            "attachment_bytes",
            "attachment_inventory",
            "attachment_mapping",
            "attachment_symlink_victim",
        )
        with tempfile.TemporaryDirectory(
            prefix="cp2-readiness-private-seal-test-", dir="/tmp"
        ) as outer_temporary:
            outer = Path(outer_temporary)
            victim = outer / "cleanup-victim"
            victim.write_bytes(b"victim-must-survive")
            for mutation in mutations:
                with self.subTest(post_barrier_mutation=mutation):
                    displaced_private = None
                    replacement_marker = None
                    private = Path(tempfile.mkdtemp(
                        prefix="schurvio-cp2-readiness-", dir="/tmp"
                    ))
                    private.chmod(0o700)
                    for name in ("git-home", "git-tmp", "readiness"):
                        (private / name).mkdir(mode=0o700)
                    attachment = private / "readiness/synthetic.bin"
                    attachment.write_bytes(b"readiness")
                    attachment.chmod(0o600)
                    frozen = private / "unit-artifact-frozen"
                    frozen.mkdir(mode=0o700)
                    frozen_report = frozen / "cp2_report.json"
                    frozen_report.write_bytes(b"frozen-unit")
                    frozen_report.chmod(0o444)
                    frozen.chmod(0o555)
                    attachments = {"readiness/synthetic.bin": attachment}
                    seal = readiness._PrivateTreeSeal.capture(
                        private, attachments, frozen
                    )
                    test_lock = Path("/tmp") / (
                        "cp2-private-seal-lock-" + private.name
                    )
                    lock_descriptor = readiness.acquire_data_lock(test_lock)
                    guarded_repository = mock.Mock()
                    guarded_repository.read_preauthorized_registry_once.side_effect = (
                        AssertionError("registry read was reached after private-state mutation")
                    )
                    authorization = readiness.ReadinessAuthorization(
                        record={},
                        attachments=attachments,
                        unit_verification_command={},
                        frozen_unit_artifact=frozen,
                        temporary_root=private,
                        lock_fd=lock_descriptor,
                        lock_path=test_lock,
                        repository=guarded_repository,
                        private_tree_seal=seal,
                    )
                    if mutation == "private_root_mode":
                        private.chmod(0o755)
                    elif mutation == "private_root_substitution":
                        displaced_private = private.with_name(private.name + "-held")
                        private.rename(displaced_private)
                        private.mkdir(mode=0o700)
                        replacement_marker = private / "replacement-must-survive"
                        replacement_marker.write_bytes(b"replacement-must-survive")
                        replacement_marker.chmod(0o600)
                    elif mutation == "frozen_unit_bytes":
                        frozen_report.chmod(0o600)
                        frozen_report.write_bytes(b"changed-unit")
                        frozen_report.chmod(0o444)
                    elif mutation == "attachment_bytes":
                        attachment.write_bytes(b"changed-readiness")
                    elif mutation == "attachment_inventory":
                        extra = private / "readiness/extra.bin"
                        extra.write_bytes(b"extra")
                        extra.chmod(0o600)
                    elif mutation == "attachment_mapping":
                        authorization.attachments["readiness/synthetic.bin"] = victim
                    elif mutation == "attachment_symlink_victim":
                        attachment.unlink()
                        attachment.symlink_to(victim)
                    else:  # pragma: no cover - the tuple above is closed.
                        self.fail("unknown private-state mutation")

                    try:
                        with self.assertRaises(readiness.ReadinessError):
                            authorization.read_registry_once()
                        guarded_repository.read_preauthorized_registry_once.assert_not_called()
                        if mutation == "private_root_substitution":
                            with self.assertRaises(readiness.ReadinessError):
                                authorization.close()
                            self.assertEqual(
                                replacement_marker.read_bytes(),
                                b"replacement-must-survive",
                            )
                            self.assertTrue(displaced_private.exists())
                        else:
                            authorization.close()
                            self.assertFalse(private.exists())
                        self.assertEqual(victim.read_bytes(), b"victim-must-survive")
                    finally:
                        if private.exists():
                            readiness._remove_private_tree(private)
                        if displaced_private is not None and displaced_private.exists():
                            readiness._remove_private_tree(displaced_private)
                        if test_lock.exists():
                            test_lock.unlink()


class OpaqueRepositoryTests(unittest.TestCase):
    def test_git_config_dangerous_controls_are_rejected(self):
        valid = b"[core]\n\tfilemode = true\n"
        self.assertEqual(
            readiness._parse_git_config(valid)[("core", "", "filemode")], "true"
        )
        dangerous = (
            b"[include]\n\tpath = /tmp/elsewhere\n",
            b"[filter \"x\"]\n\tclean = /bin/true\n",
            b"[core]\n\tfilemode = true\n\tworktree = /tmp/elsewhere\n",
            b"[core]\n\tfilemode = true\n\tbare = true\n",
            b"[core]\n\tfilemode = false\n",
            b"[core]\n\tfilemode = true\n\tsparseCheckout = true\n",
            b"[core]\n\tfilemode = true\n\tignoreCase = true\n",
            b"[core]\n\tfilemode = true\n[extensions]\n\tpartialClone = origin\n",
            b"[core]\n\tfilemode = true\n[remote \"origin\"]\n\tpromisor = true\n",
            b"[core]\n\tfilemode = true\n[submodule]\n\trecurse = true\n",
        )
        for content in dangerous:
            with self.subTest(content=content):
                with self.assertRaises(readiness.ReadinessError):
                    readiness._parse_git_config(content)

    def test_index_gitlink_flags_version_and_extension_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-index-codec-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            original = (fixture.root / ".git/index").read_bytes()

            def resign(content):
                content = bytearray(content)
                content[-20:] = hashlib.sha1(content[:-20]).digest()
                return bytes(content)

            mutations = []
            version = bytearray(original)
            version[4:8] = (4).to_bytes(4, "big")
            mutations.append(resign(version))
            gitlink = bytearray(original)
            gitlink[36:40] = (0o160000).to_bytes(4, "big")
            mutations.append(resign(gitlink))
            staged = bytearray(original)
            flags = int.from_bytes(staged[72:74], "big") | 0x1000
            staged[72:74] = flags.to_bytes(2, "big")
            mutations.append(resign(staged))
            assume_valid = bytearray(original)
            flags = int.from_bytes(assume_valid[72:74], "big") | 0x8000
            assume_valid[72:74] = flags.to_bytes(2, "big")
            mutations.append(resign(assume_valid))
            link_extension = original[:-20] + b"link\0\0\0\0"
            link_extension += hashlib.sha1(link_extension).digest()
            mutations.append(link_extension)
            for content in mutations:
                with self.subTest(digest=hashlib.sha256(content).hexdigest()):
                    with self.assertRaises(readiness.ReadinessError):
                        readiness._parse_git_index(content)

    def test_readiness_git_argv_environment_and_private_modes_are_exact(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-git-contract-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            with readiness.OpaqueGitRepository(fixture.root, guard_temp) as repository:
                prefix = repository._git_prefix()
                self.assertEqual(prefix[:5], [
                    "/usr/bin/git", "--no-pager", "--no-optional-locks",
                    "--git-dir=/proc/self/fd/4", "--work-tree=/proc/self/fd/3",
                ])
                self.assertEqual(prefix[5::2], ["-c"] * 17)
                self.assertEqual(
                    prefix[6::2],
                    [
                        "color.ui=false", "core.attributesFile=/dev/null",
                        "core.commitGraph=false", "core.excludesFile=/dev/null",
                        "core.fileMode=true", "core.fsmonitor=false",
                        "core.hooksPath=/dev/null", "core.ignoreCase=false",
                        "core.sparseCheckout=false", "core.sparseCheckoutCone=false",
                        "core.untrackedCache=false", "diff.external=",
                        "pager.status=false", "protocol.allow=never",
                        "protocol.file.allow=never", "status.submoduleSummary=false",
                        "submodule.recurse=false",
                    ],
                )
                environment = repository.readiness_git_environment
                self.assertEqual(set(environment), {
                    "GIT_ALLOW_PROTOCOL", "GIT_ATTR_NOSYSTEM", "GIT_CONFIG_NOSYSTEM",
                    "GIT_LITERAL_PATHSPECS", "GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS",
                    "GIT_OPTIONAL_LOCKS", "GIT_PAGER", "GIT_PROTOCOL_FROM_USER",
                    "GIT_TERMINAL_PROMPT", "HOME", "LANG", "LC_ALL", "PATH", "TMPDIR",
                })
                self.assertEqual(environment["PATH"], "/usr/bin:/bin")
                self.assertEqual(environment["GIT_ALLOW_PROTOCOL"], "none")
                for name in ("git-home", "git-tmp"):
                    self.assertEqual(
                        stat.S_IMODE((guard_temp / name).lstat().st_mode), 0o700
                    )

    def test_raw_fork_interrupt_timeout_and_survivor_cleanup_is_atomic(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-raw-fork-", dir="/tmp") as temporary:
            root = Path(temporary)
            fixture = SyntheticRepository(temporary)
            guard_temp = root / "guard"
            guard_temp.mkdir(mode=0o700)
            real_fork = os.fork
            real_waitpid = os.waitpid

            def assert_process_absent(process_group):
                with self.assertRaises(ChildProcessError):
                    os.waitpid(process_group, os.WNOHANG)
                with self.assertRaises(ProcessLookupError):
                    os.killpg(process_group, 0)

            def interrupt_first_parent_wait(invocation):
                captured = []
                interrupted = [False]

                def capture_fork():
                    child = real_fork()
                    if child > 0:
                        captured.append(child)
                    return child

                def interrupt_waitpid(child, options):
                    if options == os.WNOHANG and not interrupted[0]:
                        interrupted[0] = True
                        raise KeyboardInterrupt("synthetic parent interruption")
                    return real_waitpid(child, options)

                with mock.patch.object(
                    readiness.os, "fork", side_effect=capture_fork
                ), mock.patch.object(
                    readiness.os, "waitpid", side_effect=interrupt_waitpid
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        invocation()
                self.assertTrue(interrupted[0])
                self.assertEqual(len(captured), 1)
                assert_process_absent(captured[0])

            def invoke_and_capture_fork(invocation, message):
                captured = []

                def capture_fork():
                    child = real_fork()
                    if child > 0:
                        captured.append(child)
                    return child

                with mock.patch.object(
                    readiness.os, "fork", side_effect=capture_fork
                ):
                    with self.assertRaisesRegex(readiness.ReadinessError, message):
                        invocation()
                self.assertEqual(len(captured), 1)
                assert_process_absent(captured[0])

            hang = root / "hang-git"
            hang.write_text(
                "#!/usr/bin/python3\nimport time\ntime.sleep(60)\n",
                encoding="utf-8",
            )
            hang.chmod(0o700)

            def survivor_program(path, marker):
                path.write_text(
                    """#!/usr/bin/python3
import os
import time
child = os.fork()
if child == 0:
    descriptor = os.open({marker!r}, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(descriptor, str(os.getpid()).encode('ascii'))
    os.close(descriptor)
    time.sleep(60)
    os._exit(0)
deadline = time.monotonic() + 2.0
while not os.path.exists({marker!r}) and time.monotonic() < deadline:
    time.sleep(0.005)
os._exit(0)
""".format(marker=str(marker)),
                    encoding="utf-8",
                )
                path.chmod(0o700)

            with readiness.OpaqueGitRepository(fixture.root, guard_temp) as repository:
                command_count = len(repository.readiness_git_commands)
                interrupt_first_parent_wait(
                    lambda: repository._git(("rev-parse", "--verify", "HEAD"))
                )
                self.assertEqual(len(repository.readiness_git_commands), command_count)
                interrupt_first_parent_wait(repository._cat_file_digests)
                self.assertEqual(len(repository.readiness_git_commands), command_count)
                self.assertFalse(any(
                    thread.name == "cp2-cat-file-reader" and thread.is_alive()
                    for thread in readiness.threading.enumerate()
                ))

                with mock.patch.object(
                    repository, "_git_prefix", return_value=[str(hang)]
                ), mock.patch.object(
                    readiness, "_READINESS_GIT_TIMEOUT_SECONDS", 0.05
                ):
                    invoke_and_capture_fork(
                        lambda: repository._git(("rev-parse", "--verify", "HEAD")),
                        "timed out",
                    )
                    invoke_and_capture_fork(
                        repository._cat_file_digests,
                        "timed out",
                    )
                self.assertEqual(len(repository.readiness_git_commands), command_count)
                self.assertFalse(any(
                    thread.name == "cp2-cat-file-reader" and thread.is_alive()
                    for thread in readiness.threading.enumerate()
                ))

                for label, invocation in (
                    (
                        "git",
                        lambda: repository._git(("rev-parse", "--verify", "HEAD")),
                    ),
                    ("cat", repository._cat_file_digests),
                ):
                    marker = root / (label + "-survivor.pid")
                    program = root / (label + "-survivor-git")
                    survivor_program(program, marker)
                    with mock.patch.object(
                        repository, "_git_prefix", return_value=[str(program)]
                    ):
                        invoke_and_capture_fork(invocation, "surviving process group")
                    self.assertTrue(marker.is_file())
                self.assertEqual(len(repository.readiness_git_commands), command_count)
                self.assertFalse(any(
                    thread.name == "cp2-cat-file-reader" and thread.is_alive()
                    for thread in readiness.threading.enumerate()
                ))

    def test_git_admin_alternates_links_modes_and_indirection_are_rejected(self):
        attacks = (
            "commondir", "config_worktree", "shallow", "grafts", "alternates",
            "head_symlink", "config_hardlink", "object_world_writable",
        )
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-admin-", dir="/tmp") as temporary:
            outer = Path(temporary)
            for attack in attacks:
                with self.subTest(attack=attack):
                    parent = outer / attack
                    parent.mkdir()
                    fixture = SyntheticRepository(parent)
                    git_dir = fixture.root / ".git"
                    if attack == "commondir":
                        (git_dir / "commondir").write_text("../other\n", encoding="ascii")
                    elif attack == "config_worktree":
                        (git_dir / "config.worktree").write_text(
                            "[core]\n\tbare=false\n", encoding="ascii"
                        )
                    elif attack == "shallow":
                        (git_dir / "shallow").write_text(fixture.commit + "\n", encoding="ascii")
                    elif attack == "grafts":
                        (git_dir / "info/grafts").write_text(
                            fixture.commit + "\n", encoding="ascii"
                        )
                    elif attack == "alternates":
                        (git_dir / "objects/info/alternates").write_text(
                            "/tmp/forbidden\n", encoding="ascii"
                        )
                    elif attack == "head_symlink":
                        head = git_dir / "HEAD"
                        head.rename(git_dir / "HEAD.real")
                        head.symlink_to("HEAD.real")
                    elif attack == "config_hardlink":
                        os.link(str(git_dir / "config"), str(parent / "config-peer"))
                    else:
                        loose = next(
                            path
                            for path in (git_dir / "objects").glob("[0-9a-f][0-9a-f]/*")
                            if path.is_file()
                        )
                        loose.chmod(0o666)
                    guard_temp = parent / "guard"
                    guard_temp.mkdir(mode=0o700)
                    with self.assertRaises(readiness.ReadinessError):
                        readiness.OpaqueGitRepository(fixture.root, guard_temp)

    def test_ignored_build_symlink_is_snapshotted_without_following_target(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-build-link-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            ignore = fixture.root / ".gitignore"
            ignore.write_text("/build/\n/results/\n/Testing/\n", encoding="ascii")
            run_git(fixture.root, "add", "--", ".gitignore")
            run_git(fixture.root, "commit", "-q", "-m", "ignore snapshotted roots")
            outside = Path(temporary) / "outside-secret"
            outside.write_bytes(b"must-not-be-followed")
            outside.chmod(0)
            build = fixture.root / "build"
            build.mkdir()
            link_target = str(outside)
            (build / "opaque-link").symlink_to(link_target)
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            try:
                with readiness.OpaqueGitRepository(fixture.root, guard_temp) as repository:
                    _, exists, entries = readiness.parse_snapshot(
                        repository.capture_repository_root("build", "build")
                    )
                    self.assertTrue(exists)
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0].entry_type, "l")
                    self.assertEqual(
                        entries[0].sha256,
                        hashlib.sha256(os.fsencode(link_target)).hexdigest(),
                    )
                    staging = repository._create_postauthorization_output_staging(
                        "synthetic_output"
                    )
                    marker = staging.partial / "authorized-marker"
                    marker.write_bytes(b"authorized output\n")
                    repository.revalidate()
                    sibling = staging.parent / "unauthorized-sibling"
                    sibling.write_bytes(b"must be rejected\n")
                    with self.assertRaises(readiness.ReadinessError):
                        repository.revalidate()
            finally:
                outside.chmod(0o600)

    def test_valid_repository_hashes_registry_only_as_opaque_bytes(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-git-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            real_open = open

            def parser_spy(path, *args, **kwargs):
                if str(path).endswith("project/datasets.yaml"):
                    raise AssertionError("registry reached a semantic file parser")
                return real_open(path, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=parser_spy), mock.patch.object(
                subprocess, "Popen", side_effect=AssertionError("unexpected provider subprocess")
            ):
                with readiness.OpaqueGitRepository(fixture.root, guard_temp) as repository:
                    payload = repository.capture_source()
                    _, entries, identity = readiness.parse_source_snapshot(payload)
                    registry = next(item for item in entries if item.path == "project/datasets.yaml")
                    self.assertEqual(
                        registry.sha256, hashlib.sha256(fixture.registry_sentinel).hexdigest()
                    )
                    self.assertEqual(identity.commit, fixture.commit)
                    self.assertNotIn(fixture.registry_sentinel, payload)

    def test_untracked_source_and_tracked_component_symlink_fail_before_content_use(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-attacks-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            secret = fixture.root / "untracked-secret"
            secret.write_bytes(b"must-not-be-opened")
            secret.chmod(0)
            guard_temp = Path(temporary) / "guard-a"
            guard_temp.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp)
            secret.chmod(0o600)
            secret.unlink()
            scripts = fixture.root / "scripts"
            displaced = fixture.root / "scripts-real"
            scripts.rename(displaced)
            scripts.symlink_to(displaced.name)
            guard_temp_b = Path(temporary) / "guard-b"
            guard_temp_b.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp_b)

    def test_dangerous_config_and_gitfile_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-config-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            config = fixture.root / ".git/config"
            with config.open("a", encoding="utf-8") as stream:
                stream.write("[include]\n\tpath = /tmp/forbidden\n")
            guard_temp = Path(temporary) / "guard-a"
            guard_temp.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp)

    def test_index_corruption_and_object_symlink_are_rejected_pre_git(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-index-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            index = fixture.root / ".git/index"
            content = bytearray(index.read_bytes())
            content[16] ^= 1
            index.write_bytes(content)
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp)

        with tempfile.TemporaryDirectory(prefix="cp2-readiness-object-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            loose = next(
                path
                for path in (fixture.root / ".git/objects").glob("[0-9a-f][0-9a-f]/*")
                if path.is_file()
            )
            displaced = loose.with_name(loose.name + ".displaced")
            loose.rename(displaced)
            loose.symlink_to(displaced.name)
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp)

        with tempfile.TemporaryDirectory(prefix="cp2-readiness-gitfile-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            actual_git = fixture.root / ".actual-git"
            (fixture.root / ".git").rename(actual_git)
            (fixture.root / ".git").write_text("gitdir: .actual-git\n", encoding="ascii")
            guard_temp = Path(temporary) / "guard"
            guard_temp.mkdir(mode=0o700)
            with self.assertRaises(readiness.ReadinessError):
                readiness.OpaqueGitRepository(fixture.root, guard_temp)

    def test_repository_close_attempts_every_fd_after_baseexception(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-readiness-close-", dir="/tmp"
        ) as temporary:
            fixture = SyntheticRepository(temporary)
            guard_temp = Path(temporary) / "guard-close"
            guard_temp.mkdir(mode=0o700)
            repository = readiness.OpaqueGitRepository(
                fixture.root, guard_temp
            )
            inventory = repository.descriptor_inventory()
            descriptors = [record["fd"] for record in inventory.values()]
            self.assertGreater(len(descriptors), 3)
            interrupted_descriptor = descriptors[len(descriptors) // 2]
            attempted = []
            real_close = os.close

            def close_then_interrupt(descriptor):
                attempted.append(descriptor)
                real_close(descriptor)
                if descriptor == interrupted_descriptor:
                    raise KeyboardInterrupt("injected close interruption")

            with mock.patch.object(
                readiness.os, "close", side_effect=close_then_interrupt
            ), self.assertRaisesRegex(
                KeyboardInterrupt, "injected close interruption"
            ):
                repository.close()
            self.assertEqual(set(attempted), set(descriptors))
            for descriptor in descriptors:
                with self.assertRaises(OSError) as closed:
                    os.fstat(descriptor)
                self.assertEqual(closed.exception.errno, errno.EBADF)
            # Ownership slots were cleared before close, so idempotent close
            # cannot target a later unrelated reuse of the same fd number.
            repository.close()

    def test_publication_failed_rollback_reconciles_exact_final_inode(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-readiness-publish-reconcile-", dir="/tmp"
        ) as temporary:
            fixture = SyntheticRepository(temporary)
            guard_temp = Path(temporary) / "guard-publish"
            guard_temp.mkdir(mode=0o700)
            with readiness.OpaqueGitRepository(
                fixture.root, guard_temp
            ) as repository:
                staging = repository._create_postauthorization_output_staging(
                    "cp2_reconcile"
                )
                held_inode = staging.partial.stat().st_ino
                (staging.partial / "payload").write_bytes(b"sealed\n")
                (staging.partial / "payload").chmod(0o444)
                staging.partial.chmod(0o555)
                real_rename = repository._renameat2_noreplace
                rename_calls = 0

                def fail_rollback(parent_fd, source_name, destination_name):
                    nonlocal rename_calls
                    rename_calls += 1
                    if rename_calls == 2:
                        raise OSError(errno.EIO, "synthetic rollback failure")
                    return real_rename(
                        parent_fd, source_name, destination_name
                    )

                with mock.patch.object(
                    repository,
                    "_renameat2_noreplace",
                    side_effect=fail_rollback,
                ), mock.patch.object(
                    repository,
                    "_rebaseline_postauthorization_output_parent",
                    side_effect=readiness.ReadinessError(
                        "synthetic forward rebaseline failure"
                    ),
                ), self.assertRaisesRegex(
                    readiness.ReadinessError, "exact rollback failed"
                ):
                    repository._publish_postauthorization_output(staging)
                self.assertEqual(rename_calls, 2)
                self.assertFalse(staging.partial.exists())
                self.assertEqual(staging.final.stat().st_ino, held_inode)
                self.assertEqual(
                    repository._output_publication_location(staging),
                    ("published", 0o555),
                )
                descriptor = repository._duplicate_output_root_fd(staging)
                try:
                    self.assertEqual(os.fstat(descriptor).st_ino, held_inode)
                finally:
                    os.close(descriptor)
                def rollback_then_interrupt(
                    parent_fd, source_name, destination_name
                ):
                    real_rename(parent_fd, source_name, destination_name)
                    raise KeyboardInterrupt(
                        "synthetic rollback/latch interruption"
                    )

                with mock.patch.object(
                    repository,
                    "_renameat2_noreplace",
                    side_effect=rollback_then_interrupt,
                ), self.assertRaisesRegex(
                    KeyboardInterrupt, "rollback/latch interruption"
                ):
                    repository._rollback_published_postauthorization_output(
                        staging
                    )
                self.assertEqual(
                    repository._output_publication_location(staging),
                    ("hidden", 0o555),
                )
                self.assertFalse(staging.final.exists())
                self.assertEqual(staging.partial.stat().st_ino, held_inode)


class WorkerMountNamespaceRebindingTests(unittest.TestCase):
    @staticmethod
    def _worker_identity(origin):
        return (origin[0] + 1000000, origin[1] + 1, origin[2] + 1)

    @staticmethod
    def _complete_private_tree(root):
        readiness_dir = root / "readiness"
        readiness_dir.mkdir(mode=0o700)
        attachment = readiness_dir / "synthetic.bin"
        attachment.write_bytes(b"synthetic readiness attachment\n")
        attachment.chmod(0o600)
        frozen = root / "unit-artifact-frozen"
        frozen.mkdir(mode=0o700)
        report = frozen / "cp2_report.json"
        report.write_bytes(b"synthetic frozen unit report\n")
        report.chmod(0o444)
        frozen.chmod(0o555)
        attachments = {"readiness/synthetic.bin": attachment}
        return attachments, frozen

    def test_repository_rebind_is_exact_one_shot_and_failure_atomic(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-readiness-rebind-repository-", dir="/tmp"
        ) as temporary:
            root = Path(temporary)
            fixture = SyntheticRepository(root)
            for case in ("injected_failure", "success"):
                with self.subTest(case=case):
                    guard = root / ("guard-" + case)
                    guard.mkdir(mode=0o700)
                    with readiness.OpaqueGitRepository(
                        fixture.root, guard
                    ) as repository:
                        repository._create_postauthorization_output_staging(
                            "cp2_" + case
                        )
                        before = repository.descriptor_inventory()
                        before_process_fds = set(os.listdir("/proc/self/fd"))
                        with self.assertRaisesRegex(
                            readiness.ReadinessError, "child mount namespace"
                        ):
                            repository.rebind_worker_mount_namespace()
                        self.assertEqual(repository.descriptor_inventory(), before)

                        worker_identity = self._worker_identity(
                            repository._descriptor_origin
                        )
                        if case == "injected_failure":
                            real_reopen = (
                                readiness._reopen_descriptor_on_current_mount
                            )
                            calls = 0

                            def fail_second_reopen(*args, **kwargs):
                                nonlocal calls
                                calls += 1
                                if calls == 2:
                                    raise readiness.ReadinessError(
                                        "injected atomic rebind failure"
                                    )
                                return real_reopen(*args, **kwargs)

                            with mock.patch.object(
                                readiness,
                                "_process_mount_namespace_identity",
                                return_value=worker_identity,
                            ), mock.patch.object(
                                readiness,
                                "_reopen_descriptor_on_current_mount",
                                side_effect=fail_second_reopen,
                            ):
                                with self.assertRaisesRegex(
                                    readiness.ReadinessError,
                                    "injected atomic rebind failure",
                                ):
                                    repository.rebind_worker_mount_namespace()
                            self.assertEqual(calls, 2)
                            self.assertEqual(repository.descriptor_inventory(), before)
                            self.assertEqual(
                                set(os.listdir("/proc/self/fd")), before_process_fds
                            )
                            self.assertFalse(
                                repository._worker_mount_namespace_rebound
                            )
                            repository.revalidate()
                            continue

                        with mock.patch.object(
                            readiness,
                            "_process_mount_namespace_identity",
                            return_value=worker_identity,
                        ):
                            repository.rebind_worker_mount_namespace()
                            with self.assertRaisesRegex(
                                readiness.ReadinessError, "already rebound"
                            ):
                                repository.rebind_worker_mount_namespace()
                        after = repository.descriptor_inventory()
                        self.assertEqual(set(after), set(before))
                        for label in before:
                            self.assertNotEqual(
                                after[label]["fd"], before[label]["fd"]
                            )
                            self.assertEqual(
                                after[label]["target"], before[label]["target"]
                            )
                            self.assertEqual(
                                after[label]["stat_signature"],
                                before[label]["stat_signature"],
                            )
                            self.assertEqual(
                                after[label]["access_mode"], os.O_RDONLY
                            )
                            self.assertTrue(after[label]["close_on_exec"])
                            with self.assertRaises(OSError) as closed:
                                os.fstat(before[label]["fd"])
                            self.assertEqual(closed.exception.errno, errno.EBADF)

    def test_authorization_worker_mode_removes_lock_and_publication(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-readiness-rebind-authorization-", dir="/tmp"
        ) as temporary:
            outer = Path(temporary)
            fixture = SyntheticRepository(outer)
            private = Path(tempfile.mkdtemp(
                prefix="schurvio-cp2-readiness-worker-", dir="/tmp"
            ))
            repository = readiness.OpaqueGitRepository(fixture.root, private)
            attachments, frozen = self._complete_private_tree(private)
            seal = readiness._PrivateTreeSeal.capture(private, attachments, frozen)
            lock = Path("/tmp") / (
                "cp2-worker-rebind-lock-" + outer.name
            )
            lock_fd = readiness.acquire_data_lock(lock)
            authorization = readiness.ReadinessAuthorization(
                record={},
                attachments=attachments,
                unit_verification_command={},
                frozen_unit_artifact=frozen,
                temporary_root=private,
                lock_fd=lock_fd,
                lock_path=lock,
                repository=repository,
                private_tree_seal=seal,
            )
            try:
                staging = authorization.create_output_staging(
                    "cp2_worker_rebind"
                )
                expected_identity = authorization.output_staging_identity(staging)
                parent_duplicate = authorization.duplicate_output_partial_fd(
                    staging
                )
                try:
                    held = authorization.repository.descriptor_inventory()[
                        "repository.output_partial"
                    ]
                    self.assertEqual(
                        readiness._stat_signature(os.fstat(parent_duplicate)),
                        held["stat_signature"],
                    )
                    self.assertEqual(
                        os.readlink("/proc/self/fd/{}".format(parent_duplicate)),
                        held["target"],
                    )
                    self.assertEqual(
                        fcntl.fcntl(parent_duplicate, fcntl.F_GETFL)
                        & os.O_ACCMODE,
                        os.O_RDONLY,
                    )
                    self.assertTrue(
                        fcntl.fcntl(parent_duplicate, fcntl.F_GETFD)
                        & fcntl.FD_CLOEXEC
                    )
                finally:
                    os.close(parent_duplicate)

                parent_inventory = authorization.descriptor_inventory()
                self.assertEqual(
                    parent_inventory["authorization.data_lock"]["access_mode"],
                    os.O_RDWR,
                )
                worker_identity = self._worker_identity(
                    repository._descriptor_origin
                )
                with mock.patch.object(
                    readiness,
                    "_process_mount_namespace_identity",
                    return_value=worker_identity,
                ):
                    authorization.rebind_worker_mount_namespace(staging)
                self.assertTrue(authorization._worker_mode)
                self.assertEqual(authorization.lock_fd, -1)
                with self.assertRaises(OSError) as closed_lock:
                    os.fstat(lock_fd)
                self.assertEqual(closed_lock.exception.errno, errno.EBADF)
                self.assertEqual(
                    authorization.output_staging_identity(staging),
                    expected_identity,
                )
                worker_inventory = authorization.descriptor_inventory()
                self.assertNotIn("authorization.data_lock", worker_inventory)
                self.assertEqual(
                    set(worker_inventory),
                    set(parent_inventory) - {"authorization.data_lock"},
                )
                self.assertTrue(all(
                    record["access_mode"] == os.O_RDONLY
                    and record["close_on_exec"] is True
                    for record in worker_inventory.values()
                ))
                with mock.patch.object(
                    readiness.fcntl,
                    "flock",
                    side_effect=AssertionError(
                        "worker revalidation reached flock"
                    ),
                ):
                    authorization.revalidate()
                for operation in (
                    lambda: authorization.create_output_staging("second"),
                    lambda: authorization.publish_output(staging),
                    lambda: authorization.duplicate_output_partial_fd(staging),
                    lambda: authorization.output_publication_state(staging),
                    lambda: authorization.current_output_publication_state(),
                    lambda: authorization.duplicate_output_root_fd(staging),
                    lambda: authorization.rebind_worker_mount_namespace(staging),
                ):
                    with self.assertRaises(readiness.ReadinessError):
                        operation()

                authorization.close()
                self.assertTrue(private.is_dir())
            finally:
                if not authorization._closed:
                    authorization.close()
                if private.exists():
                    readiness._remove_private_tree(private)
                if lock.exists():
                    lock.unlink()


class PrevalidatedSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="cp2-readiness-prevalidated-", dir="/tmp"
        )
        cls.root = Path(cls.temporary.name)
        cls.fixture = RealPrevalidatedFixture(cls.root)
        cls.artifact, cls.anchor = cls.fixture.unit_artifact(cls.root)
        guard_root = cls.root / "context-guard"
        guard_root.mkdir(mode=0o700)
        with readiness.OpaqueGitRepository(cls.fixture.root, guard_root) as repository:
            cls.context = repository.prevalidated_source_context()

    @classmethod
    def tearDownClass(cls):
        RealPrevalidatedFixture._make_writable(cls.root)
        cls.temporary.cleanup()

    def test_context_and_archive_bind_exact_stage_zero_identity(self):
        errors = []
        validated = verify_report.validate_prevalidated_source_context(
            copy.deepcopy(self.context), errors
        )
        self.assertEqual(errors, [])
        self.assertEqual(validated, self.context)
        archive_errors = []
        verify_report.validate_prevalidated_source_archive(
            self.artifact / verify_report.SOURCE_ARCHIVE_NAME,
            self.context,
            archive_errors,
        )
        self.assertEqual(archive_errors, [])

    def test_wrong_path_mode_commit_tree_and_order_are_rejected(self):
        unsafe_path = copy.deepcopy(self.context)
        unsafe_path["entries"][0]["path"] = "../escape"
        errors = []
        verify_report.validate_prevalidated_source_context(unsafe_path, errors)
        self.assertTrue(any("unsafe path" in error for error in errors), errors)

        wrong_tree = copy.deepcopy(self.context)
        wrong_tree["index_tree"] = "0" * 40
        errors = []
        verify_report.validate_prevalidated_source_context(wrong_tree, errors)
        self.assertTrue(any("index tree differs" in error for error in errors), errors)

        reordered = copy.deepcopy(self.context)
        reordered["entries"][0], reordered["entries"][1] = (
            reordered["entries"][1], reordered["entries"][0]
        )
        errors = []
        verify_report.validate_prevalidated_source_context(reordered, errors)
        self.assertTrue(any("strictly UTF-8 sorted" in error for error in errors), errors)

        wrong_mode = copy.deepcopy(self.context)
        mode_entry = next(
            entry
            for entry in wrong_mode["entries"]
            if entry["path"] != verify_report.PREAUTHORIZATION_REGISTRY_PATH
        )
        mode_entry["mode"] = 0o100755 if mode_entry["mode"] == 0o100644 else 0o100644
        errors = []
        verify_report.validate_prevalidated_source_archive(
            self.artifact / verify_report.SOURCE_ARCHIVE_NAME,
            wrong_mode,
            errors,
        )
        self.assertTrue(any("member mode differs" in error for error in errors), errors)

        wrong_commit = copy.deepcopy(self.context)
        wrong_commit["commit"] = "0" * 40
        errors = []
        verify_report.validate_prevalidated_source_archive(
            self.artifact / verify_report.SOURCE_ARCHIVE_NAME,
            wrong_commit,
            errors,
        )
        self.assertTrue(any("commit binding is wrong" in error for error in errors), errors)

    def test_registry_member_is_rejected_without_extracting_its_bytes(self):
        archive_path = self.root / "registry-member.tar"
        payload = b"MUST_NOT_BE_READ:\xff:\x00\n"
        with tarfile.open(
            str(archive_path),
            mode="w:",
            format=tarfile.PAX_FORMAT,
            pax_headers={"comment": self.context["commit"]},
        ) as archive:
            member = tarfile.TarInfo(verify_report.PREAUTHORIZATION_REGISTRY_PATH)
            member.mode = 0o664
            member.uid = 0
            member.gid = 0
            member.size = len(payload)
            member.pax_headers = {"comment": self.context["commit"]}
            archive.addfile(member, io.BytesIO(payload))

        original_extractfile = tarfile.TarFile.extractfile

        def extraction_guard(archive, member):
            if member.name == verify_report.PREAUTHORIZATION_REGISTRY_PATH:
                raise AssertionError("registry bytes reached tar extraction")
            return original_extractfile(archive, member)

        errors = []
        with mock.patch.object(tarfile.TarFile, "extractfile", new=extraction_guard):
            verify_report.validate_prevalidated_source_archive(
                archive_path, self.context, errors
            )
        self.assertTrue(
            any("contains the preauthorization registry" in error for error in errors),
            errors,
        )

    def test_prevalidated_verifier_uses_neither_git_nor_live_source_files(self):
        source_root = self.fixture.root.absolute()

        def beneath_source(value):
            if isinstance(value, int):
                return False
            try:
                candidate = Path(value).absolute()
                return candidate == source_root or source_root in candidate.parents
            except (TypeError, ValueError):
                return False

        real_builtin_open = open
        real_os_open = os.open
        real_path_open = Path.open

        def guarded_builtin_open(path, *args, **kwargs):
            if beneath_source(path):
                raise AssertionError("prevalidated verifier opened live source")
            return real_builtin_open(path, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            if beneath_source(path):
                raise AssertionError("prevalidated verifier os.opened live source")
            return real_os_open(path, *args, **kwargs)

        def guarded_path_open(path, *args, **kwargs):
            if beneath_source(path):
                raise AssertionError("prevalidated verifier Path.opened live source")
            return real_path_open(path, *args, **kwargs)

        with mock.patch.object(
            verify_report, "git_text", side_effect=AssertionError("prevalidated verifier invoked Git")
        ), mock.patch("builtins.open", side_effect=guarded_builtin_open), mock.patch.object(
            os, "open", side_effect=guarded_os_open
        ), mock.patch.object(Path, "open", new=guarded_path_open):
            status_value, errors = verify_report.verify_unit_anchor_prevalidated(
                self.artifact,
                self.anchor,
                copy.deepcopy(self.context),
                quiet=True,
            )
        self.assertEqual((status_value, errors), (0, []))


class FullBarrierTests(unittest.TestCase):
    def test_full_synthetic_barrier_returns_held_authorization(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-full-", dir="/tmp") as temporary:
            fixture = RealPrevalidatedFixture(temporary)
            artifact, anchor = fixture.unit_artifact(temporary)
            expected = readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT
            lock = Path("/tmp") / ("cp2-full-lock-" + Path(temporary).name)
            with mock.patch.object(readiness, "CP1_AUTHORIZATION_COMMIT", fixture.commit):
                authorization = readiness.run_readiness_barrier(
                    fixture.root,
                    artifact,
                    anchor,
                    expected,
                    fixture.commit,
                    lock_path=lock,
                    self_test_timeout_seconds=30,
                )
            scratch = authorization.temporary_root
            published_final = None
            try:
                self.assertTrue(authorization.prebag_authorized)
                self.assertTrue(authorization.record["passed"])
                self.assertEqual(authorization.record["bag_provider_calls"], 0)
                self.assertEqual(authorization.record["unit_tested_commit"], fixture.commit)
                self.assertEqual(authorization.record["unit_tested_tree"], fixture.tree)
                self.assertEqual(len(authorization.record["self_tests"]), 5)
                self.assertEqual(
                    [item["result"]["case_count"] for item in authorization.record["self_tests"]],
                    [22, 48, 36, 43, 83],
                )
                for index, item in enumerate(authorization.record["self_tests"]):
                    self.assertEqual(item["index"], index)
                    self.assertEqual(item["path"], readiness.ENTRYPOINTS[index])
                    self.assertTrue(any(token.startswith("/proc/self/fd/") for token in item["argv"]))
                    self.assertEqual(
                        item["expected_case_names"],
                        list(readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT[item["path"]]),
                    )
                self.assertTrue(all(path.is_file() for path in authorization.attachments.values()))
                self.assertEqual(
                    authorization.unit_verification_command["argv"][3].split("/")[:4],
                    ["", "proc", "self", "fd"],
                )
                self.assertRegex(
                    authorization.unit_verification_command["source_context_sha256"],
                    r"^[0-9a-f]{64}$",
                )
                self.assertEqual(
                    authorization.unit_verification_command["environment"],
                    {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                )
                self.assertEqual(
                    authorization.frozen_unit_artifact,
                    authorization.temporary_root / "unit-artifact-frozen",
                )
                self.assertTrue(authorization.frozen_unit_artifact.is_dir())
                authorization.revalidate()
                held_contract = "docs/cp2_one_pass_contract.md"
                with self.assertRaises(readiness.ReadinessError):
                    authorization.repository.duplicate_tracked_fd(held_contract)
                contract_fd = authorization.duplicate_source_fd(held_contract)
                try:
                    contract_bytes = os.read(contract_fd, 64 * 1024 * 1024)
                finally:
                    os.close(contract_fd)
                self.assertEqual(
                    contract_bytes,
                    (fixture.root / held_contract).read_bytes(),
                )
                with self.assertRaises(readiness.ReadinessError):
                    authorization.duplicate_source_fd(
                        readiness.PREAUTHORIZATION_REGISTRY_PATH
                    )
                with self.assertRaises(readiness.ReadinessError):
                    authorization.duplicate_source_fd("LICENSE")
                with self.assertRaises(readiness.ReadinessError):
                    authorization.read_registry_once("project/not-the-registry.yaml")
                registry_buffer = authorization.read_registry_once()
                self.assertEqual(registry_buffer, fixture.registry_sentinel)
                with self.assertRaises(readiness.ReadinessError):
                    authorization.read_registry_once()
                serialized_record = json.dumps(authorization.record, sort_keys=True).encode("utf-8")
                self.assertNotIn(fixture.registry_sentinel, serialized_record)
                competitor = os.open(str(lock), os.O_RDWR | os.O_CLOEXEC)
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(competitor)
                with self.assertRaises(readiness.ReadinessError):
                    authorization.create_output_staging("../unsafe")

                # A failure in the authorization-level transition check must
                # leave neither a consumed capability nor any output path.
                output_parent = (
                    fixture.root / "results/staging/cp2/recorded"
                )
                real_revalidate = authorization.revalidate
                revalidate_calls = 0

                def fail_second_revalidation():
                    nonlocal revalidate_calls
                    revalidate_calls += 1
                    if revalidate_calls == 2:
                        raise readiness.ReadinessError(
                            "injected post-registration rejection"
                        )
                    return real_revalidate()

                with mock.patch.object(
                    authorization,
                    "revalidate",
                    side_effect=fail_second_revalidation,
                ):
                    with self.assertRaisesRegex(
                        readiness.ReadinessError,
                        "injected post-registration rejection",
                    ):
                        authorization.create_output_staging(
                            "cp2_rollback_probe"
                        )
                self.assertEqual(revalidate_calls, 2)
                self.assertIsNone(authorization._output_staging)
                self.assertIsNone(
                    authorization.repository._postauthorization_output_relative
                )
                if output_parent.exists():
                    self.assertEqual(list(output_parent.iterdir()), [])
                authorization.revalidate()

                staging = authorization.create_output_staging(
                    "cp2_synthetic_recorded"
                )
                self.assertEqual(
                    authorization.output_publication_state(staging), "hidden"
                )
                self.assertEqual(
                    staging.parent,
                    fixture.root / "results/staging/cp2/recorded",
                )
                output_marker = staging.partial / "authorized-marker"
                output_marker.write_bytes(b"authorized output\n")
                authorization.revalidate()
                with self.assertRaises(readiness.ReadinessError):
                    authorization.create_output_staging("second_output")
                wrong_token = readiness.AuthorizedOutputStaging(
                    run_id=staging.run_id,
                    parent=staging.parent,
                    partial=staging.partial,
                    final=staging.parent / "wrong-final",
                )
                with self.assertRaises(readiness.ReadinessError):
                    authorization.publish_output(wrong_token)
                partial_inode = staging.partial.stat().st_ino
                staging.partial.chmod(0o555)

                # Inject a post-rename transition failure.  Publication must
                # restore the exact hidden name/inode and a usable binding;
                # no final name may survive the failed attempt.
                real_rebaseline = (
                    authorization.repository.
                    _rebaseline_postauthorization_output_parent
                )
                publication_rebaseline_calls = 0

                def fail_first_publication_rebaseline(before_namespace):
                    nonlocal publication_rebaseline_calls
                    publication_rebaseline_calls += 1
                    if publication_rebaseline_calls == 1:
                        raise readiness.ReadinessError(
                            "injected post-rename rejection"
                        )
                    return real_rebaseline(before_namespace)

                with mock.patch.object(
                    authorization.repository,
                    "_rebaseline_postauthorization_output_parent",
                    side_effect=fail_first_publication_rebaseline,
                ):
                    with self.assertRaisesRegex(
                        readiness.ReadinessError,
                        "injected post-rename rejection",
                    ):
                        authorization.publish_output(staging)
                self.assertEqual(publication_rebaseline_calls, 2)
                self.assertFalse(staging.final.exists())
                self.assertEqual(staging.partial.stat().st_ino, partial_inode)
                authorization.revalidate()

                # If the forward rename completes but the exact rollback
                # syscall fails, the held inode must remain queryable under
                # its real final name as an uncommitted failure root.
                rename_calls = 0
                real_rename = (
                    authorization.repository._renameat2_noreplace
                )

                def forward_then_reject_rollback(
                    parent_fd, source_name, destination_name
                ):
                    nonlocal rename_calls
                    rename_calls += 1
                    if rename_calls == 2:
                        raise OSError(
                            errno.EIO, "injected rollback rename failure"
                        )
                    return real_rename(
                        parent_fd, source_name, destination_name
                    )

                with mock.patch.object(
                    authorization.repository,
                    "_renameat2_noreplace",
                    side_effect=forward_then_reject_rollback,
                ), mock.patch.object(
                    authorization.repository,
                    "_rebaseline_postauthorization_output_parent",
                    side_effect=readiness.ReadinessError(
                        "injected forward post-rename failure"
                    ),
                ):
                    with self.assertRaisesRegex(
                        readiness.ReadinessError,
                        "exact rollback failed",
                    ):
                        authorization.publish_output(staging)
                self.assertEqual(rename_calls, 2)
                self.assertEqual(
                    authorization.output_publication_state(staging),
                    "published_uncommitted",
                )
                self.assertFalse(staging.partial.exists())
                self.assertEqual(staging.final.stat().st_ino, partial_inode)
                interrupted_root_fd = authorization.duplicate_output_root_fd(
                    staging
                )
                try:
                    self.assertEqual(
                        os.fstat(interrupted_root_fd).st_ino, partial_inode
                    )
                finally:
                    os.close(interrupted_root_fd)
                authorization.repository._rollback_published_postauthorization_output(
                    staging
                )
                self.assertEqual(
                    authorization.output_publication_state(staging), "hidden"
                )
                self.assertFalse(staging.final.exists())
                self.assertEqual(staging.partial.stat().st_ino, partial_inode)

                real_postpublish_revalidate = authorization.revalidate
                postpublish_revalidate_calls = 0

                def fail_postpublication_revalidation():
                    nonlocal postpublish_revalidate_calls
                    postpublish_revalidate_calls += 1
                    if postpublish_revalidate_calls == 2:
                        raise readiness.ReadinessError(
                            "injected post-publication authorization rejection"
                        )
                    return real_postpublish_revalidate()

                with mock.patch.object(
                    authorization,
                    "revalidate",
                    side_effect=fail_postpublication_revalidation,
                ):
                    with self.assertRaisesRegex(
                        readiness.ReadinessError,
                        "post-publication authorization rejection",
                    ):
                        authorization.publish_output(staging)
                self.assertEqual(postpublish_revalidate_calls, 2)
                self.assertFalse(authorization._output_published)
                self.assertFalse(staging.final.exists())
                self.assertEqual(staging.partial.stat().st_ino, partial_inode)
                self.assertEqual(
                    authorization.output_publication_state(staging), "hidden"
                )
                authorization.revalidate()

                # Interrupt exactly after the repository has durably renamed
                # and rebound the held inode, but before the authorization can
                # commit its success bit.  The authoritative query must expose
                # the final-name root as an uncommitted failure target.
                real_repository_publish = (
                    authorization.repository._publish_postauthorization_output
                )

                def publish_then_interrupt(token):
                    real_repository_publish(token)
                    raise KeyboardInterrupt("injected publish/return gap")

                with mock.patch.object(
                    authorization.repository,
                    "_publish_postauthorization_output",
                    side_effect=publish_then_interrupt,
                ):
                    with self.assertRaisesRegex(
                        KeyboardInterrupt, "injected publish/return gap"
                    ):
                        authorization.publish_output(staging)
                self.assertEqual(
                    authorization.output_publication_state(staging),
                    "published_uncommitted",
                )
                self.assertFalse(staging.partial.exists())
                self.assertEqual(staging.final.stat().st_ino, partial_inode)
                interrupted_root_fd = authorization.duplicate_output_root_fd(
                    staging
                )
                try:
                    self.assertEqual(os.fstat(interrupted_root_fd).st_ino, partial_inode)
                finally:
                    os.close(interrupted_root_fd)
                authorization.repository._rollback_published_postauthorization_output(
                    staging
                )
                self.assertEqual(
                    authorization.output_publication_state(staging), "hidden"
                )
                self.assertFalse(staging.final.exists())
                self.assertEqual(staging.partial.stat().st_ino, partial_inode)

                authorization.publish_output(staging)
                published_final = staging.final
                self.assertFalse(staging.partial.exists())
                self.assertEqual(staging.final.stat().st_ino, partial_inode)
                self.assertEqual(
                    authorization.output_publication_state(staging), "published"
                )
                published_root_fd = authorization.duplicate_output_root_fd(
                    staging
                )
                try:
                    os.fchmod(published_root_fd, 0o700)
                    with self.assertRaisesRegex(
                        readiness.ReadinessError,
                        "sealed final binding",
                    ):
                        authorization.output_publication_state(staging)
                    os.fchmod(published_root_fd, 0o555)
                finally:
                    os.close(published_root_fd)
                self.assertEqual(
                    authorization.output_publication_state(staging), "published"
                )
                authorization.revalidate()
                with self.assertRaises(readiness.ReadinessError):
                    authorization.publish_output(staging)
                (fixture.root / held_contract).write_bytes(b"postauthorization mutation\n")
                # Publication is the commit point: later workspace drift is a
                # diagnostic source failure but cannot retroactively make the
                # already sealed, exact-source artifact unpublished.
                self.assertEqual(
                    authorization.output_publication_state(staging), "published"
                )
                with self.assertRaises(readiness.ReadinessError):
                    authorization.duplicate_source_fd(held_contract)
            finally:
                authorization.close()
            with self.assertRaises(readiness.ReadinessError):
                authorization.duplicate_source_fd("docs/cp2_one_pass_contract.md")
            self.assertFalse(scratch.exists())
            lock.unlink()
            if published_final is not None:
                published_final.chmod(0o700)

    def test_self_test_mutation_prevents_authorization_and_cleans_temporary_state(self):
        with tempfile.TemporaryDirectory(prefix="cp2-readiness-mutation-", dir="/tmp") as temporary:
            fixture = SyntheticRepository(temporary)
            mutator = fixture.root / readiness.ENTRYPOINTS[1]
            result = passing_result(
                readiness.ENTRYPOINTS[1],
                "mutator",
                readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT[readiness.ENTRYPOINTS[1]],
            )
            build_path = fixture.root / "build/mutated"
            mutator.write_text(
                """#!/usr/bin/python3
from pathlib import Path
import sys
if sys.argv[1:] == ['--self-test']:
    p = Path({build!r}); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(b'x')
    print({result!r})
    raise SystemExit(0)
raise SystemExit(2)
""".format(
                    build=str(build_path),
                    result=json.dumps(result, sort_keys=True, separators=(",", ":")),
                ),
                encoding="utf-8",
            )
            run_git(fixture.root, "add", "--", readiness.ENTRYPOINTS[1])
            run_git(fixture.root, "commit", "-q", "-m", "synthetic mutator")
            fixture.commit = run_git(fixture.root, "rev-parse", "HEAD").decode().strip()
            fixture.tree = run_git(fixture.root, "rev-parse", "HEAD^{tree}").decode().strip()
            artifact, anchor = fixture.unit_artifact(temporary)
            expected = readiness.EXPECTED_CASE_NAMES_BY_ENTRYPOINT
            lock = Path("/tmp") / ("cp2-mutation-lock-" + Path(temporary).name)
            private_root = Path("/tmp") / (
                "schurvio-cp2-readiness-test-" + Path(temporary).name
            )

            def create_private_root(*, prefix, dir):
                self.assertEqual(prefix, "schurvio-cp2-readiness-")
                self.assertEqual(dir, "/tmp")
                private_root.mkdir(mode=0o700)
                return str(private_root)

            with mock.patch.object(
                readiness, "CP1_AUTHORIZATION_COMMIT", fixture.commit
            ), mock.patch.object(
                readiness.tempfile, "mkdtemp", side_effect=create_private_root
            ):
                with self.assertRaises(readiness.ReadinessError):
                    readiness.run_readiness_barrier(
                        fixture.root, artifact, anchor, expected, fixture.commit,
                        lock_path=lock, self_test_timeout_seconds=10,
                    )
            self.assertFalse(private_root.exists())
            self.assertFalse(lock.exists())


if __name__ == "__main__":
    unittest.main()
