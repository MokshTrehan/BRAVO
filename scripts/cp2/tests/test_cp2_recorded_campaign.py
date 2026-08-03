#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Protecting tests for CP2-C postauthorization campaign mechanics."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import cp2_recorded_campaign as campaign  # noqa: E402


class _SyntheticAuthorization:
    def __init__(self):
        self.repository = object()
        self.revalidations = 0

    def revalidate(self):
        self.revalidations += 1


STUBBORN_CHILD = (
    "import signal,time;"
    "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
    "time.sleep(60)"
)
STUBBORN_TREE = (
    "import os,subprocess,sys,time;"
    "child=subprocess.Popen([sys.executable,'-c'," +
    repr(STUBBORN_CHILD) + "]);"
    "stream=open(sys.argv[1],'w',encoding='ascii');"
    "stream.write(str(os.getpid())+' '+str(child.pid));"
    "stream.close();"
    "time.sleep(60)"
)


class RecordedCampaignTests(unittest.TestCase):
    def _wait_for_process_ids(self, path):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                values = tuple(int(value) for value in path.read_text(
                    encoding="ascii").split())
            except (FileNotFoundError, ValueError):
                time.sleep(0.01)
                continue
            if len(values) == 2:
                return values
            time.sleep(0.01)
        self.fail("synthetic process tree did not publish both PIDs")

    def _assert_group_absent(self, process_group_id):
        self.assertFalse(campaign._process_group_exists(process_group_id))

    def test_static_bundle_is_frozen_and_domain_separated(self):
        root = Path(__file__).resolve().parents[3]
        payload, records = campaign.encode_static_bundle(root)
        self.assertTrue(payload.startswith(campaign.STATIC_BUNDLE_DOMAIN))
        self.assertEqual([row["path"] for row in records],
                         list(campaign.STATIC_PATHS))
        self.assertEqual([row["sha256"] for row in records],
                         list(campaign.STATIC_SHA256))

    def test_registry_resolution_is_hash_bound_and_ambiguity_rejected(self):
        rows = []
        for sequence, digest in zip(campaign.SEQUENCES, campaign.BAG_SHA256):
            rows.append({
                "sequence": sequence, "sha256": digest,
                "bag": "/tmp/{}.bag".format(sequence),
            })
        payload = json.dumps({"datasets": rows}).encode("utf-8")
        self.assertEqual(
            campaign._registry_bag_paths(payload),
            tuple(Path(row["bag"]) for row in rows),
        )
        rows[0]["duplicate"] = "/tmp/duplicate.bag"
        with self.assertRaises(campaign.CampaignError):
            campaign._registry_bag_paths(
                json.dumps({"datasets": rows}).encode("utf-8"))
        with self.assertRaises(campaign.CampaignError):
            campaign._registry_bag_paths(
                b"shared: &x [MH_01_easy, " + campaign.BAG_SHA256[0].encode("ascii") +
                b", /tmp/MH_01_easy.bag]\na: *x\nb: *x\n")

    def test_strict_fp_compile_command_audit(self):
        with tempfile.TemporaryDirectory(prefix="cp2-campaign-fp-", dir="/tmp") as raw:
            workspace = Path(raw)
            source_space = workspace / "src"
            build_space = workspace / "build"
            directory = build_space / "ov_msckf"
            source_space.mkdir()
            directory.mkdir(parents=True)
            strict = [
                "-fno-fast-math", "-ffp-contract=off", "-fsigned-zeros",
                "-DEIGEN_DONT_VECTORIZE=1",
                "-DEIGEN_MAX_ALIGN_BYTES=16",
                "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",
                "-ffile-prefix-map={}=/cp2/reproducible-root".format(workspace),
                "-fdebug-prefix-map={}=/cp2/reproducible-root".format(workspace),
                "-fmacro-prefix-map={}=/cp2/reproducible-root".format(workspace),
            ]
            rows = []
            for source, target in campaign.STRICT_FP_SOURCE_TARGETS.items():
                source_path = source_space / source
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_bytes(b"// synthetic compile source\n")
                output = (build_space / "ov_msckf" / "CMakeFiles" /
                          (target + ".dir") /
                          (source[len("ov_msckf/"):] + ".o"))
                rows.append({
                    "directory": str(directory), "file": str(source_path),
                    "output": str(output),
                    "arguments": (["/usr/bin/c++", "-fno-signed-zeros"] +
                                  strict + ["-c", str(source_path),
                                            "-o", str(output)]),
                })
            path = workspace / "compile_commands.json"

            def audit(candidate):
                path.write_text(json.dumps(candidate), encoding="utf-8")
                campaign._audit_strict_fp_compile_commands(
                    path, source_space, build_space, workspace)

            audit(rows)
            corruptions = []
            for injected in (
                    "-ffast-math", "@opaque.rsp", "-include=opaque.h",
                    "-specs=opaque.specs", "-UEIGEN_DONT_VECTORIZE"):
                changed = json.loads(json.dumps(rows))
                changed[0]["arguments"].append(injected)
                corruptions.append(changed)
            duplicate = json.loads(json.dumps(rows))
            duplicate.append(dict(duplicate[0]))
            corruptions.append(duplicate)
            spoof = json.loads(json.dumps(rows))
            spoof_path = source_space / "spoof" / Path(spoof[0]["file"]).name
            spoof_path.parent.mkdir()
            spoof_path.write_bytes(b"// basename spoof\n")
            spoof[0]["file"] = str(spoof_path)
            compile_index = spoof[0]["arguments"].index("-c") + 1
            spoof[0]["arguments"][compile_index] = str(spoof_path)
            corruptions.append(spoof)
            wrong_target = json.loads(json.dumps(rows))
            output_index = wrong_target[0]["arguments"].index("-o") + 1
            wrong_output = wrong_target[0]["arguments"][output_index].replace(
                "ov_msckf_lib.dir", "spoof_target.dir")
            wrong_target[0]["arguments"][output_index] = wrong_output
            wrong_target[0]["output"] = wrong_output
            corruptions.append(wrong_target)
            dual_argv = json.loads(json.dumps(rows))
            dual_argv[0]["command"] = "true"
            corruptions.append(dual_argv)
            nul_token = json.loads(json.dumps(rows))
            nul_token[0]["arguments"].append("opaque\0flag")
            corruptions.append(nul_token)
            quoted_map = json.loads(json.dumps(rows))
            prefix_index = next(
                index for index, token in enumerate(quoted_map[0]["arguments"])
                if token.startswith("-ffile-prefix-map="))
            quoted_map[0]["arguments"][prefix_index] = (
                '"' + quoted_map[0]["arguments"][prefix_index] + '"')
            corruptions.append(quoted_map)
            for candidate in corruptions:
                with self.assertRaises(campaign.CampaignError):
                    audit(candidate)

    def test_elf_identity_reads_build_id_and_soname_in_process(self):
        executable = Path("/usr/bin/python3").resolve(strict=True)
        build_id, soname = campaign._elf_identity(executable, False)
        self.assertRegex(build_id, r"^[0-9a-f]+$")
        self.assertIsNone(soname)
        libraries = tuple(Path("/usr/lib/x86_64-linux-gnu").glob("libm-*.so"))
        if not libraries:
            self.skipTest("system libm image is unavailable")
        library_build_id, library_soname = campaign._elf_identity(libraries[0], True)
        self.assertRegex(library_build_id, r"^[0-9a-f]+$")
        self.assertEqual(library_soname, "libm.so.6")

    def test_manifest_inventory_modes_and_noreplace(self):
        with tempfile.TemporaryDirectory(prefix="cp2-campaign-seal-", dir="/tmp") as raw:
            parent = Path(raw)
            source = parent / "source"
            source.mkdir(mode=0o700)
            campaign._write_new(source / "commands.jsonl", b"{}\n")
            campaign._write_new(source / "updates.jsonl", b"{}\n")
            campaign._freeze_existing_files(source)
            inventory = campaign._inventory(source)
            self.assertEqual([row["path"] for row in inventory],
                             ["commands.jsonl", "updates.jsonl"])
            self.assertTrue(all(row["mode"] == 0o444 for row in inventory))
            anchor = campaign._write_manifest(source)
            self.assertEqual(anchor,
                             hashlib.sha256((source / "SHA256SUMS").read_bytes()).hexdigest())
            campaign._seal_directories(source)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o555)
            destination = parent / "final"
            campaign._rename_noreplace(source, destination)
            self.assertTrue(destination.is_dir())

            second = parent / "second"
            second.mkdir(mode=0o700)
            with self.assertRaises(OSError):
                campaign._rename_noreplace(second, destination)
            self.assertTrue(second.is_dir())

    def test_closed_summary_rejects_nonpassing_math(self):
        summary = {name: 0 for name in campaign.ASSEMBLER_SUMMARY_KEYS}
        summary.update({
            "sequence_summaries": [], "minimum_committing_updates": 1000,
            "gate_passed": True, "math_passed": True,
            "passed_pre_replay": False,
        })
        with tempfile.TemporaryDirectory(prefix="cp2-campaign-report-", dir="/tmp") as raw:
            root = Path(raw)
            for name in (
                    "provenance.json", "commands.jsonl", "serial_pairs.jsonl",
                    "updates.jsonl", "features.jsonl", "state_blocks.jsonl",
                    "covariance_blocks.jsonl", "state_snapshot_payloads.bin",
                    "proposal_payloads.bin", "raw_system_payloads.bin",
                    "replay_report.json"):
                (root / name).write_bytes(b"{}\n")
            with self.assertRaises(campaign.CampaignError):
                campaign._build_report(
                    root, summary, {"passed": True}, "2026-08-02T00:00:00.000000Z")

    def test_command_recorder_cleans_timeout_and_baseexception_paths(self):
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        }
        with tempfile.TemporaryDirectory(
                prefix="cp2-campaign-process-success-", dir="/tmp") as raw:
            root = Path(raw)
            recorder = campaign.CommandRecorder(
                root, _SyntheticAuthorization())
            record = recorder.run(
                "verification", ["/usr/bin/true"], Path("/tmp"),
                "synthetic_success", environment, timeout=5.0)
            self.assertFalse(record["timed_out"])
            self.assertEqual(record["exit_code"], 0)
            self.assertEqual(record["stdout_sha256"],
                             campaign.schema.sha256_file(root / record["stdout"]))
            self.assertEqual(record["stderr_sha256"],
                             campaign.schema.sha256_file(root / record["stderr"]))

        with tempfile.TemporaryDirectory(
                prefix="cp2-campaign-process-timeout-", dir="/tmp") as raw:
            root = Path(raw)
            pid_path = root / "timeout-pids"
            recorder = campaign.CommandRecorder(
                root, _SyntheticAuthorization())
            record = recorder.run(
                "verification",
                ["/usr/bin/python3", "-c", STUBBORN_TREE, str(pid_path)],
                Path("/tmp"), "synthetic_timeout", environment, timeout=1.0)
            parent_pid, child_pid = self._wait_for_process_ids(pid_path)
            self.assertTrue(record["timed_out"])
            self.assertEqual(record["exit_code"], -signal.SIGKILL)
            self.assertEqual(recorder.records, [record])
            self.assertEqual(record["stdout_sha256"],
                             campaign.schema.sha256_file(root / record["stdout"]))
            self.assertEqual(record["stderr_sha256"],
                             campaign.schema.sha256_file(root / record["stderr"]))
            self._assert_group_absent(parent_pid)
            self.assertFalse(Path("/proc/{}".format(parent_pid)).exists())
            self.assertFalse(Path("/proc/{}".format(child_pid)).exists())

        exception_cases = (
            ("runtime", RuntimeError("synthetic communicate failure")),
            ("keyboard", KeyboardInterrupt()),
        )
        for name, injected_error in exception_cases:
            with self.subTest(exception=name), tempfile.TemporaryDirectory(
                    prefix="cp2-campaign-process-{}-".format(name),
                    dir="/tmp") as raw:
                root = Path(raw)
                pid_path = root / "exception-pids"
                recorder = campaign.CommandRecorder(
                    root, _SyntheticAuthorization())

                def fail_communicate(process, *args, **kwargs):
                    del args, kwargs
                    parent_pid, _ = self._wait_for_process_ids(pid_path)
                    self.assertEqual(parent_pid, process.pid)
                    raise injected_error

                with mock.patch.object(
                        campaign.subprocess.Popen, "communicate",
                        new=fail_communicate), self.assertRaises(
                            type(injected_error)):
                    recorder.run(
                        "verification",
                        ["/usr/bin/python3", "-c", STUBBORN_TREE,
                         str(pid_path)],
                        Path("/tmp"), "synthetic_" + name, environment,
                        timeout=5.0)
                parent_pid, child_pid = self._wait_for_process_ids(pid_path)
                self.assertEqual(recorder.records, [])
                self._assert_group_absent(parent_pid)
                self.assertFalse(Path("/proc/{}".format(parent_pid)).exists())
                self.assertFalse(Path("/proc/{}".format(child_pid)).exists())

    def test_detached_verifier_reaps_stubborn_descendant_before_failure(self):
        with tempfile.TemporaryDirectory(
                prefix="cp2-campaign-detached-process-", dir="/tmp") as raw:
            root = Path(raw)
            source_space = root / "source"
            partial = root / "partial"
            verifier = source_space / "scripts/cp2/verify_report.py"
            verifier.parent.mkdir(parents=True)
            partial.mkdir()
            verifier.write_text(
                "import os,subprocess,sys\n"
                "child=subprocess.Popen([sys.executable,'-c'," +
                repr(STUBBORN_CHILD) + "])\n"
                "partial=sys.argv[sys.argv.index('--verify-recorded')+1]\n"
                "with open(os.path.join(partial,'detached-pids'),'w',"
                "encoding='ascii') as stream:\n"
                "    stream.write(str(os.getpid())+' '+str(child.pid))\n",
                encoding="utf-8")
            verifier_digest = campaign.schema.sha256_file(verifier)
            authorization = _SyntheticAuthorization()
            with mock.patch.object(
                    campaign, "_held_source_hash",
                    return_value=verifier_digest), self.assertRaisesRegex(
                        campaign.CampaignError, "left a process group"):
                campaign._detached_verify_recorded(
                    partial, {"source_space": str(source_space)}, "0" * 64,
                    authorization)
            parent_pid, child_pid = self._wait_for_process_ids(
                partial / "detached-pids")
            self._assert_group_absent(parent_pid)
            self.assertFalse(Path("/proc/{}".format(parent_pid)).exists())
            self.assertFalse(Path("/proc/{}".format(child_pid)).exists())


if __name__ == "__main__":
    unittest.main()
