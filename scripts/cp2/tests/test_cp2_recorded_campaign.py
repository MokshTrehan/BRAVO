#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Protecting tests for CP2-C postauthorization campaign mechanics."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
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
TEST_DIRECTORY = Path(__file__).resolve().parent
if str(TEST_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(TEST_DIRECTORY))

import cp2_recorded_campaign as campaign  # noqa: E402
import cp2_capsule as capsule  # noqa: E402
from test_cp2_capsule import (  # noqa: E402
    direct_math_members,
    evaluator_members,
    profile_for,
)


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

    def test_cp2_c_launch_surface_and_held_output_population_are_exact(self):
        repository = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory(
            prefix="cp2-c-held-runtime-", dir="/tmp"
        ) as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            trace = root / "trace"
            trace.mkdir(mode=0o700)
            held = campaign._HeldRuntimeOutputs(trace)
            required = tuple(
                name
                for name in campaign.C_SINK_NAMES.values()
                if name is not None
            )
            support = (
                "context.json",
                "prelaunch_parameters.yaml",
                "parameters_canonical.bin",
            )
            try:
                held.precreate(required + support)
                capabilities = {
                    field: (
                        "null"
                        if campaign.C_SINK_NAMES[field] is None
                        else held.capability(campaign.C_SINK_NAMES[field])
                    )
                    for field in campaign.SINK_CAPABILITY_FIELDS
                }
                arguments = campaign._launch_arguments(
                    {"source_space": str(repository)},
                    Path("/proc/self/fd/99"),
                    40.0,
                    trace,
                    trace / "context.json",
                    0,
                    capabilities,
                )
                import xml.etree.ElementTree as element_tree

                launch = element_tree.parse(
                    repository / "project/cp2_serial.launch"
                ).getroot()
                declared = {
                    element.attrib["name"]
                    for element in launch.findall("arg")
                }
                supplied = {
                    item.split(":=", 1)[0] for item in arguments[1:]
                }
                self.assertEqual(supplied, declared)
                capability_arguments = {
                    item.split(":=", 1)[0][len("cp2_") : -len("_sink_capability")]:
                    item.split(":=", 1)[1]
                    for item in arguments[1:]
                    if item.startswith("cp2_")
                    and "_sink_capability:=" in item
                }
                self.assertEqual(
                    tuple(capability_arguments),
                    campaign.SINK_CAPABILITY_FIELDS,
                )
                self.assertEqual(capability_arguments, capabilities)
                for field, name in campaign.C_SINK_NAMES.items():
                    if name is None:
                        self.assertEqual(capabilities[field], "null")
                    else:
                        fields = capabilities[field].split(":")
                        self.assertEqual(fields[0], "v1")
                        self.assertEqual(int(fields[1]), os.getpid())
                        self.assertEqual(len(fields), 12)

                held.fill_parent("context.json", b"context\n")
                held.fill_parent(
                    "prelaunch_parameters.yaml", b"parameters\n"
                )
                held.fill_parent("parameters_canonical.bin", b"canonical\n")
                for name in required:
                    writer = os.open(
                        str(held.proc_path(name)),
                        os.O_WRONLY | os.O_CLOEXEC,
                    )
                    try:
                        os.write(writer, (name + "\n").encode("ascii"))
                        os.fchmod(writer, 0o444)
                        os.fsync(writer)
                    finally:
                        os.close(writer)
                held.accept_external(required)
                held.revalidate()

                # Owner DAC is not a same-UID security boundary.  The
                # protecting claim is instead that any post-accept byte drift
                # is detected, even when the inode, size, and final mode are
                # restored by a coordinating same-UID writer.
                victim = required[0]
                original = held.payload(victim)
                os.chmod(trace / victim, 0o600)
                descriptor = os.open(
                    trace / victim,
                    os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC,
                )
                try:
                    replacement = b"X" + original[1:]
                    self.assertEqual(len(replacement), len(original))
                    os.write(descriptor, replacement)
                    os.fchmod(descriptor, 0o444)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                with self.assertRaisesRegex(
                    campaign.CampaignError,
                    "held runtime trace (?:member|bytes) changed",
                ):
                    held.revalidate()
            finally:
                held.close()

    def test_unit_capsules_stage_exact_profiles_and_reject_mutation_or_substitution(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-campaign-capsules-", dir="/tmp"
        ) as raw:
            root = Path(raw)
            os.chmod(root, 0o700)
            unit_artifact = root / "unit"
            capsules = unit_artifact / "capsules"
            capsules.mkdir(mode=0o700, parents=True)
            fixtures = {
                "direct_math": direct_math_members(),
                "evaluator": evaluator_members(),
            }
            for kind, members in fixtures.items():
                archive_bytes = capsule.encode_capsule(members)
                profile_record = profile_for(kind, archive_bytes)
                profile = capsule.validate_capsule_profile(profile_record)
                archive_relative, profile_relative = campaign.UNIT_CAPSULE_PATHS[kind]
                campaign._write_new(
                    unit_artifact / archive_relative, archive_bytes, mode=0o444
                )
                campaign._write_new(
                    unit_artifact / profile_relative,
                    profile.canonical_bytes,
                    mode=0o444,
                )

            stage_parent = root / "staged"
            private_parent = root / "private"
            stage_parent.mkdir(mode=0o700)
            private_parent.mkdir(mode=0o700)
            runtimes = {
                kind: campaign._stage_unit_capsule(
                    unit_artifact, kind, stage_parent, private_parent
                )
                for kind in fixtures
            }
            for kind, runtime in runtimes.items():
                self.assertEqual(runtime.kind, kind)
                runtime.revalidate()

            runtime = runtimes["evaluator"]
            member = runtime.staged.root / "notices/GPL-3.0.txt"
            original = member.read_bytes()
            os.chmod(member, 0o644)
            member.write_bytes(b"X" + original[1:])
            os.chmod(member, 0o444)
            with self.assertRaises(campaign.CampaignError):
                runtime.revalidate()

            direct_runtime = runtimes["direct_math"]
            direct_member = direct_runtime.staged.root / "notices/BSD-3-Clause.txt"
            displaced_member = root / "displaced-license"
            os.rename(direct_member, displaced_member)
            shutil.copy2(displaced_member, direct_member)
            capsule.revalidate_staged_capsule(
                direct_runtime.staged.root, direct_runtime.staged.entries
            )
            with self.assertRaises(campaign.CampaignError):
                direct_runtime.revalidate()

            second_stage_parent = root / "staged-second"
            second_private_parent = root / "private-second"
            second_stage_parent.mkdir(mode=0o700)
            second_private_parent.mkdir(mode=0o700)
            runtime = campaign._stage_unit_capsule(
                unit_artifact,
                "evaluator",
                second_stage_parent,
                second_private_parent,
            )
            displaced = root / "displaced-evaluator"
            os.rename(runtime.staged.root, displaced)
            shutil.copytree(displaced, runtime.staged.root, copy_function=shutil.copy2)
            capsule.revalidate_staged_capsule(
                runtime.staged.root, runtime.staged.entries
            )
            with self.assertRaises(campaign.CampaignError):
                runtime.revalidate()

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
            for source in (
                "ov_msckf/src/ros1_serial_msckf.cpp",
                "ov_msckf/src/update/CP2OutputCapability.cpp",
                "ov_msckf/src/update/CP2TimingClock.cpp",
            ):
                changed = json.loads(json.dumps(rows))
                selected = next(
                    row
                    for row in changed
                    if Path(row["file"]).relative_to(source_space).as_posix()
                    == source
                )
                selected["arguments"].remove("-fsigned-zeros")
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
            source_relative = Path(wrong_target[0]["file"]).relative_to(
                source_space
            ).as_posix()
            expected_target = campaign.STRICT_FP_SOURCE_TARGETS[
                source_relative
            ]
            wrong_output = wrong_target[0]["arguments"][output_index].replace(
                expected_target + ".dir", "spoof_target.dir")
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

            sealed_snapshot = campaign._sealed_artifact_snapshot(destination)
            mutated_leaf = destination / "commands.jsonl"
            os.chmod(str(mutated_leaf), 0o600)
            mutated_leaf.write_bytes(b"[]\n")
            os.chmod(str(mutated_leaf), 0o444)
            self.assertNotEqual(
                campaign._sealed_artifact_snapshot(destination),
                sealed_snapshot,
            )

            failed_partial = parent / "failed-partial"
            failed_partial.mkdir(mode=0o700)
            failed_partial_fd = os.open(
                str(failed_partial),
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
            )
            try:
                injected_failure = campaign.CampaignError(
                    "synthetic campaign failure"
                )
                campaign._retain_campaign_failure(
                    failed_partial,
                    failed_partial_fd,
                    "1" * 40,
                    "2" * 40,
                    injected_failure,
                )
                retained = json.loads(
                    (failed_partial / "failure.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(retained["error"], str(injected_failure))
                self.assertEqual(
                    retained["primary_failure"]["type"], "CampaignError"
                )
                self.assertEqual(retained["cleanup_failures"], [])
                self.assertIsNone(
                    retained["quarantined_untrusted_failure_name"]
                )
                marker_before = (failed_partial / "failure.json").read_bytes()
                with self.assertRaises(campaign.CampaignError):
                    campaign._retain_campaign_failure(
                        failed_partial,
                        failed_partial_fd,
                        "1" * 40,
                        "2" * 40,
                        campaign.CampaignError("must not replace"),
                    )
                self.assertEqual(
                    (failed_partial / "failure.json").read_bytes(),
                    marker_before,
                )
                self.assertFalse(any(
                    path.name.startswith(".failure.json.partial.")
                    for path in failed_partial.iterdir()
                ))
            finally:
                os.close(failed_partial_fd)

            outside_collision_victim = parent / "outside-collision-victim"
            outside_collision_victim.write_bytes(b"preserve collision target\n")
            for collision_kind in (
                "regular", "symlink", "directory", "hardlink"
            ):
                with self.subTest(collision_kind=collision_kind):
                    collision_root = parent / ("collision-" + collision_kind)
                    collision_root.mkdir(mode=0o700)
                    collision = collision_root / "failure.json"
                    if collision_kind == "regular":
                        collision.write_bytes(b"untrusted regular\n")
                    elif collision_kind == "symlink":
                        collision.symlink_to(outside_collision_victim)
                    elif collision_kind == "directory":
                        collision.mkdir()
                        (collision / "nested").write_bytes(b"untrusted nested\n")
                    else:
                        os.link(outside_collision_victim, collision)
                    collision_before = collision.lstat()
                    descriptor = os.open(
                        str(collision_root),
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )
                    try:
                        campaign._retain_campaign_failure(
                            collision_root,
                            descriptor,
                            "1" * 40,
                            "2" * 40,
                            campaign.CampaignError(
                                "trusted collision disposition"
                            ),
                            quarantine_untrusted_collision=True,
                        )
                    finally:
                        os.close(descriptor)
                    trusted = json.loads(
                        (collision_root / "failure.json").read_text(
                            encoding="utf-8"
                        )
                    )
                    quarantine_name = trusted[
                        "quarantined_untrusted_failure_name"
                    ]
                    self.assertRegex(
                        quarantine_name,
                        r"^\.untrusted-failure\.json\.[0-9a-f]{32}$",
                    )
                    quarantined = collision_root / quarantine_name
                    self.assertEqual(
                        (quarantined.lstat().st_dev, quarantined.lstat().st_ino),
                        (collision_before.st_dev, collision_before.st_ino),
                    )
                    self.assertEqual(
                        outside_collision_victim.read_bytes(),
                        b"preserve collision target\n",
                    )

            outside = parent / "outside-victim"
            outside.write_bytes(b"preserve\n")
            workspace = campaign.OwnedTemporaryWorkspace.create()
            workspace_descriptor = workspace.descriptor
            nested = workspace.path / "nested"
            nested.mkdir()
            (nested / "payload").write_bytes(b"owned\n")
            (workspace.path / "outside-link").symlink_to(outside)
            workspace.revalidate()
            workspace_path = workspace.path
            workspace.remove()
            self.assertFalse(workspace_path.exists())
            self.assertEqual(outside.read_bytes(), b"preserve\n")
            with self.assertRaises(OSError):
                os.fstat(workspace_descriptor)

            workspace_names_before = {
                name
                for name in os.listdir("/tmp")
                if name.startswith("schurvio-cp2-recorded-build-")
            }
            descriptors_before = len(os.listdir("/proc/self/fd"))
            with mock.patch.object(
                campaign.OwnedTemporaryWorkspace,
                "revalidate",
                side_effect=campaign.CampaignError(
                    "injected acquisition revalidation failure"
                ),
            ):
                with self.assertRaisesRegex(
                    campaign.CampaignError,
                    "injected acquisition revalidation failure",
                ):
                    campaign.OwnedTemporaryWorkspace.create()
            self.assertEqual(
                {
                    name
                    for name in os.listdir("/tmp")
                    if name.startswith("schurvio-cp2-recorded-build-")
                },
                workspace_names_before,
            )
            self.assertEqual(
                len(os.listdir("/proc/self/fd")), descriptors_before
            )

            copy_source = parent / "copy-source"
            copy_destination = parent / "copy-destination"
            copy_source.write_bytes(b"copy source\n")
            real_open = os.open

            def reject_copy_destination(path, *args, **kwargs):
                if str(path) == str(copy_destination):
                    raise OSError("injected destination acquisition failure")
                return real_open(path, *args, **kwargs)

            descriptors_before = len(os.listdir("/proc/self/fd"))
            with mock.patch.object(
                campaign.os,
                "open",
                side_effect=reject_copy_destination,
            ):
                with self.assertRaisesRegex(
                    OSError, "injected destination acquisition failure"
                ):
                    campaign._copy_new(copy_source, copy_destination)
            self.assertEqual(
                len(os.listdir("/proc/self/fd")), descriptors_before
            )
            self.assertFalse(copy_destination.exists())

            failed_build_owner = campaign.OwnedTemporaryWorkspace.create()
            failed_build_path = failed_build_owner.path
            failed_build_descriptor = failed_build_owner.descriptor

            def fail_inside_workspace(*args):
                build_workspace = Path(args[-1])
                (build_workspace / "partial-build").mkdir()
                (build_workspace / "partial-build/object").write_bytes(
                    b"incomplete\n"
                )
                raise campaign.CampaignError("injected build failure")

            with mock.patch.object(
                campaign.OwnedTemporaryWorkspace,
                "create",
                return_value=failed_build_owner,
            ), mock.patch.object(
                campaign,
                "_build_runtime_in_workspace",
                side_effect=fail_inside_workspace,
            ):
                with self.assertRaisesRegex(
                    campaign.CampaignError, "injected build failure"
                ):
                    campaign._build_runtime(
                        parent,
                        destination,
                        parent,
                        object(),
                        mock.Mock(),
                        "0" * 40,
                    )
            self.assertFalse(failed_build_path.exists())
            with self.assertRaises(OSError):
                os.fstat(failed_build_descriptor)

            substituted = campaign.OwnedTemporaryWorkspace.create()
            substituted_path = substituted.path
            displaced = substituted_path.with_name(
                substituted_path.name + "-displaced"
            )
            substituted_path.rename(displaced)
            substituted_path.mkdir(mode=0o700)
            with self.assertRaises(campaign.CampaignError):
                substituted.remove()
            self.assertTrue(substituted_path.is_dir())
            self.assertTrue(displaced.is_dir())
            substituted_path.rmdir()
            displaced.rename(substituted_path)
            substituted.remove()
            self.assertFalse(substituted_path.exists())

            # The actual CP2-C worker receives only the exact hidden partial
            # and build workspace as writable mounts.  Every inherited host
            # capability is either rebound through the new mount namespace or
            # closed, bags are exact read-only inode binds, and even a setsid
            # grandchild is killed when PID-namespace init exits.
            sandbox_repo = parent / "sandbox-repo"
            sandbox_parent = sandbox_repo / "results/staging/cp2/recorded"
            sandbox_parent.mkdir(mode=0o700, parents=True)
            sandbox_partial = sandbox_parent / (
                ".synthetic.partial." + "a" * 32
            )
            sandbox_partial.mkdir(mode=0o700)
            sandbox_final = sandbox_parent / "synthetic"
            sandbox_staging = type("SyntheticStaging", (), {})()
            sandbox_staging.run_id = "synthetic"
            sandbox_staging.parent = sandbox_parent
            sandbox_staging.partial = sandbox_partial
            sandbox_staging.final = sandbox_final
            sandbox_victim = parent / "sandbox-victim"
            sandbox_victim.write_bytes(b"preserve exactly\n")
            sandbox_victim_mode = stat.S_IMODE(sandbox_victim.stat().st_mode)
            inherited_escape_fd = os.open(
                str(sandbox_victim), os.O_RDWR | os.O_CLOEXEC
            )
            sandbox_build = campaign.OwnedTemporaryWorkspace.create()
            sandbox_bag_owner = campaign.OwnedTemporaryWorkspace.create(
                "schurvio-cp2-bag-bind-"
            )

            class SandboxRepository:
                def __init__(self):
                    self.root_fd = os.open(
                        str(sandbox_repo),
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )

            class SandboxAuthorization:
                def __init__(self):
                    self.repository = SandboxRepository()
                    self.partial_fd = os.open(
                        str(sandbox_partial),
                        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                    )
                    self.escape_fd = inherited_escape_fd
                    self.worker_mode = False

                def output_staging_identity(self, staging):
                    del staging
                    held = os.fstat(self.partial_fd)
                    by_path = os.lstat(str(sandbox_partial))
                    self.assert_same(held, by_path)
                    return campaign._directory_object_identity(held)

                @staticmethod
                def assert_same(left, right):
                    if not campaign._same_stat(left, right):
                        raise campaign.CampaignError(
                            "synthetic authorization binding differs"
                        )

                def rebind_worker_mount_namespace(self, staging):
                    del staging
                    replacements = []
                    for owner, attribute, path, directory in (
                        (self.repository, "root_fd", sandbox_repo, True),
                        (self, "partial_fd", sandbox_partial, True),
                    ):
                        old = getattr(owner, attribute)
                        flags = os.O_RDONLY | os.O_CLOEXEC
                        if directory:
                            flags |= os.O_DIRECTORY
                        replacement = os.open(str(path), flags)
                        self.assert_same(os.fstat(old), os.fstat(replacement))
                        replacements.append((owner, attribute, old, replacement))
                    for owner, attribute, old, replacement in replacements:
                        setattr(owner, attribute, replacement)
                        os.close(old)
                    # Deliberately retain this unrelated writable descriptor.
                    # The campaign's generic worker-FD scrub, not this mock
                    # authorization transition, must close it.
                    self.worker_mode = True

                def descriptor_inventory(self):
                    if not self.worker_mode:
                        raise campaign.CampaignError(
                            "synthetic authorization was not rebound"
                        )
                    return {
                        "repository": {"fd": self.repository.root_fd},
                        "partial": {"fd": self.partial_fd},
                    }

                def publish_output(self, staging):
                    os.rename(str(staging.partial), str(staging.final))

            sandbox_authorization = SandboxAuthorization()
            source_descriptors = []
            bag_mounts = []
            original_bag_paths = []
            for index in range(3):
                original = parent / "sandbox-bag-{}.bag".format(index)
                original.write_bytes(
                    "held-bag-{}\n".format(index).encode("ascii")
                )
                source_fd = os.open(str(original), os.O_RDONLY | os.O_CLOEXEC)
                source_descriptors.append(source_fd)
                target = sandbox_bag_owner.path / "{:02d}.bag".format(index)
                target.write_bytes(b"")
                os.chmod(str(target), 0o600)
                bag_mounts.append(campaign._BagMountBinding(
                    original_path=original,
                    target_path=target,
                    source_descriptor=source_fd,
                    source_identity=os.fstat(source_fd),
                    target_identity=target.lstat(),
                ))
                original_bag_paths.append(original)

            attacker_pid = os.fork()
            if attacker_pid == 0:
                ready = sandbox_partial / "swap-ready"
                done = sandbox_partial / "read-done"
                deadline = time.monotonic() + 10.0
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                original = original_bag_paths[0]
                saved = original.with_name(original.name + ".held")
                try:
                    original.rename(saved)
                    original.write_bytes(b"substitute-bag\n")
                    (sandbox_partial / "swapped").write_bytes(b"ready\n")
                    while not done.exists() and time.monotonic() < deadline:
                        time.sleep(0.005)
                    original.unlink()
                    saved.rename(original)
                    os._exit(0 if done.exists() else 2)
                except BaseException:
                    try:
                        if original.exists():
                            original.unlink()
                        if saved.exists():
                            saved.rename(original)
                    finally:
                        os._exit(3)

            def synthetic_worker(*args):
                worker_authorization = args[3]
                worker_staging = args[4]
                worker_partial = Path(worker_staging.partial)
                worker_mounts = args[6]
                if os.getpid() != 1:
                    raise campaign.CampaignError(
                        "campaign worker is not PID 1"
                    )
                capability_root = worker_partial / "pid1-output-capability"
                capability_root.mkdir(mode=0o700)
                held_output = campaign._HeldRuntimeOutputs(capability_root)
                try:
                    held_output.precreate(("sink",))
                    encoded = held_output.capability("sink")
                    if not encoded.startswith("v1:1:"):
                        raise campaign.CampaignError(
                            "PID-namespace output capability does not bind PID 1"
                        )
                    writer_pid = os.fork()
                    if writer_pid == 0:
                        try:
                            writer = os.open(
                                str(held_output.proc_path("sink")),
                                os.O_WRONLY | os.O_CLOEXEC,
                            )
                            try:
                                os.write(writer, b"pid1-held-output\n")
                                os.fchmod(writer, 0o444)
                                os.fsync(writer)
                            finally:
                                os.close(writer)
                            os._exit(0)
                        except BaseException:
                            os._exit(1)
                    observed_writer, writer_status = os.waitpid(writer_pid, 0)
                    if (
                        observed_writer != writer_pid
                        or not os.WIFEXITED(writer_status)
                        or os.WEXITSTATUS(writer_status) != 0
                    ):
                        raise campaign.CampaignError(
                            "PID-namespace output-capability writer failed"
                        )
                    held_output.accept_external(("sink",))
                    if held_output.payload("sink") != b"pid1-held-output\n":
                        raise campaign.CampaignError(
                            "PID-namespace held output bytes differ"
                        )
                finally:
                    held_output.close()
                for descriptor in os.listdir("/proc/1/fd"):
                    try:
                        target = os.readlink("/proc/1/fd/" + descriptor)
                    except FileNotFoundError:
                        continue
                    if target == str(sandbox_victim):
                        raise campaign.CampaignError(
                            "worker retained the inherited writable victim fd"
                        )
                try:
                    os.ftruncate(inherited_escape_fd, 0)
                except OSError:
                    pass
                else:
                    raise campaign.CampaignError(
                        "worker retained a truncatable inherited host fd"
                    )
                for operation in (
                    lambda: os.open(
                        "forbidden", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600, dir_fd=worker_authorization.repository.root_fd,
                    ),
                    lambda: os.fchmod(
                        worker_authorization.repository.root_fd, 0o777
                    ),
                    lambda: worker_authorization.publish_output(worker_staging),
                ):
                    try:
                        operation()
                    except OSError:
                        pass
                    else:
                        raise campaign.CampaignError(
                            "worker retained a repository/publication capability"
                        )
                escape = worker_partial / "escape"
                escape.symlink_to(sandbox_victim)
                for operation in (
                    lambda: os.truncate(str(escape), 0),
                    lambda: os.chmod(str(escape), 0o777),
                ):
                    try:
                        operation()
                    except OSError as exc:
                        if exc.errno != getattr(os, "EROFS", 30):
                            raise
                    else:
                        raise campaign.CampaignError(
                            "read-only namespace allowed a symlink escape"
                        )
                (worker_partial / "allowed").write_bytes(b"allowed\n")
                (worker_partial / "swap-ready").write_bytes(b"ready\n")
                deadline = time.monotonic() + 5.0
                while not (worker_partial / "swapped").exists():
                    if time.monotonic() >= deadline:
                        raise campaign.CampaignError("bag substitution timed out")
                    time.sleep(0.005)
                if Path(worker_mounts[0].target_path).read_bytes() != b"held-bag-0\n":
                    raise campaign.CampaignError(
                        "read-only bag bind followed a pathname substitute"
                    )
                (worker_partial / "read-done").write_bytes(b"done\n")
                ready_read, ready_write = os.pipe()
                escaped_pid = os.fork()
                if escaped_pid == 0:
                    os.close(ready_read)
                    os.setsid()
                    os.write(ready_write, b"1")
                    os.close(ready_write)
                    time.sleep(0.25)
                    (worker_partial / "late-write").write_bytes(b"escaped\n")
                    os._exit(0)
                os.close(ready_write)
                if os.read(ready_read, 1) != b"1":
                    raise campaign.CampaignError(
                        "setsid descendant did not complete its handshake"
                    )
                os.close(ready_read)
                return "b" * 64

            try:
                with mock.patch.object(
                    campaign,
                    "_execute_recorded_campaign_worker",
                    side_effect=synthetic_worker,
                ):
                    isolated = campaign._run_campaign_in_namespaces(
                        sandbox_repo,
                        {},
                        b"",
                        sandbox_authorization,
                        sandbox_staging,
                        sandbox_build,
                        tuple(bag_mounts),
                    )
                self.assertTrue(isolated["passed"])
                self.assertTrue(isolated["descendants_absent"])
                self.assertEqual(isolated["manifest_sha256"], "b" * 64)
                time.sleep(0.35)
                self.assertFalse((sandbox_partial / "late-write").exists())
                self.assertEqual(
                    sandbox_victim.read_bytes(), b"preserve exactly\n"
                )
                self.assertEqual(
                    stat.S_IMODE(sandbox_victim.stat().st_mode),
                    sandbox_victim_mode,
                )
                observed_attacker, attacker_status = os.waitpid(attacker_pid, 0)
                self.assertEqual(observed_attacker, attacker_pid)
                self.assertTrue(os.WIFEXITED(attacker_status))
                self.assertEqual(os.WEXITSTATUS(attacker_status), 0)
                self.assertEqual(original_bag_paths[0].read_bytes(), b"held-bag-0\n")
                self.assertFalse(sandbox_final.exists())
            finally:
                try:
                    os.kill(attacker_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(attacker_pid, 0)
                except ChildProcessError:
                    pass
                for descriptor in source_descriptors:
                    os.close(descriptor)
                os.close(inherited_escape_fd)
                os.close(sandbox_authorization.partial_fd)
                os.close(sandbox_authorization.repository.root_fd)
                sandbox_bag_owner.remove()
                sandbox_build.remove()

    def test_failure_bundle_preserves_primary_and_ordered_cleanup_records(self):
        primary_message = "p" * campaign.MAX_FAILURE_MESSAGE_BYTES
        bundle = campaign._append_cleanup_failures(
            RuntimeError(primary_message),
            (
                ValueError("first cleanup"),
                KeyboardInterrupt("second cleanup"),
            ),
        )
        self.assertEqual(bundle.primary_failure["type"], "RuntimeError")
        self.assertEqual(bundle.primary_failure["message"], primary_message)
        self.assertFalse(bundle.primary_failure["message_truncated"])
        self.assertEqual(
            [record["type"] for record in bundle.cleanup_failures],
            ["ValueError", "KeyboardInterrupt"],
        )
        self.assertEqual(
            [record["message"] for record in bundle.cleanup_failures],
            ["first cleanup", "second cleanup"],
        )

    def test_owned_close_clears_slots_before_baseexception(self):
        with tempfile.TemporaryDirectory(
            prefix="cp2-campaign-close-slots-", dir="/tmp"
        ) as raw:
            root = Path(raw)
            leaf = root / "bound"
            leaf.write_bytes(b"bound\n")
            bound = campaign._bind_regular_file(leaf)
            bound_descriptor = bound.descriptor
            real_close = os.close

            def interrupt_bound_close(descriptor):
                real_close(descriptor)
                raise KeyboardInterrupt("synthetic bound close gap")

            with mock.patch.object(
                campaign.os,
                "close",
                side_effect=interrupt_bound_close,
            ), self.assertRaisesRegex(
                KeyboardInterrupt, "bound close gap"
            ):
                bound.close()
            self.assertEqual(bound.descriptor, -1)
            with self.assertRaises(OSError):
                os.fstat(bound_descriptor)
            bound.close()

            workspace = campaign.OwnedTemporaryWorkspace.create()
            workspace_descriptor = workspace.descriptor

            def interrupt_workspace_close(descriptor):
                real_close(descriptor)
                if descriptor == workspace_descriptor:
                    raise KeyboardInterrupt("synthetic workspace close gap")

            with mock.patch.object(
                campaign.os,
                "close",
                side_effect=interrupt_workspace_close,
            ), self.assertRaisesRegex(
                KeyboardInterrupt, "workspace close gap"
            ):
                workspace.remove()
            self.assertEqual(workspace.descriptor, -1)
            self.assertFalse(workspace.path.exists())
            with self.assertRaises(OSError):
                os.fstat(workspace_descriptor)
            workspace.remove()

    def test_publication_commit_gap_and_uncommitted_final_are_distinct(self):
        class FakeOwner:
            def remove(self):
                return None

        class Repository:
            commit = "1" * 40
            tree = "2" * 40

        for committed in (True, False):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory(
                prefix="cp2-campaign-publication-", dir="/tmp"
            ) as raw:
                repo_root = Path(raw)

                class Authorization:
                    def __init__(self):
                        self.repository = Repository()
                        self.state = "absent"
                        self.staging = None

                    def revalidate(self):
                        return None

                    def create_output_staging(self, run_id):
                        parent = (
                            repo_root / "results/staging/cp2/recorded"
                        )
                        parent.mkdir(parents=True)
                        partial = parent / (
                            "." + run_id + ".partial." + "a" * 32
                        )
                        partial.mkdir(mode=0o700)
                        self.staging = type("Staging", (), {
                            "run_id": run_id,
                            "parent": parent,
                            "partial": partial,
                            "final": parent / run_id,
                        })()
                        self.state = "hidden"
                        return self.staging

                    def publish_output(self, staging):
                        os.rename(staging.partial, staging.final)
                        self.state = (
                            "published" if committed
                            else "published_uncommitted"
                        )
                        raise KeyboardInterrupt(
                            "synthetic post-rename publication gap"
                        )

                    def output_publication_state(self, staging):
                        self.assert_token(staging)
                        return self.state

                    def duplicate_output_root_fd(self, staging):
                        self.assert_token(staging)
                        root = (
                            staging.partial
                            if self.state == "hidden"
                            else staging.final
                        )
                        return os.open(
                            str(root),
                            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                        )

                    def assert_token(self, staging):
                        if staging is not self.staging:
                            raise campaign.CampaignError(
                                "synthetic staging token differs"
                            )

                authorization = Authorization()
                manifest_bytes = b"synthetic manifest\n"
                manifest_sha256 = hashlib.sha256(
                    manifest_bytes
                ).hexdigest()

                def synthetic_worker(*args, **kwargs):
                    del args, kwargs
                    partial = authorization.staging.partial
                    (partial / "payload").write_bytes(b"sealed payload\n")
                    (partial / "SHA256SUMS").write_bytes(manifest_bytes)
                    (partial / "payload").chmod(0o444)
                    (partial / "SHA256SUMS").chmod(0o444)
                    partial.chmod(0o555)
                    return {
                        "schema_version": 1,
                        "record_type": "cp2_isolated_worker_result",
                        "passed": True,
                        "worker_started": True,
                        "descendants_absent": True,
                        "manifest_sha256": manifest_sha256,
                        "error_type": None,
                        "error": None,
                        "primary_failure": None,
                        "cleanup_failures": [],
                        "failure_marker_retained": False,
                    }

                patches = (
                    mock.patch.object(
                        campaign.OwnedTemporaryWorkspace,
                        "create",
                        side_effect=(FakeOwner(), FakeOwner(), FakeOwner()),
                    ),
                    mock.patch.object(
                        campaign,
                        "_prepare_parent_bag_mounts",
                        return_value=((), ()),
                    ),
                    mock.patch.object(
                        campaign,
                        "_run_campaign_in_namespaces",
                        side_effect=synthetic_worker,
                    ),
                    mock.patch.object(
                        campaign,
                        "_trusted_parent_detached_verify_recorded",
                        return_value=None,
                    ),
                )
                with patches[0], patches[1], patches[2], patches[3]:
                    if committed:
                        self.assertEqual(
                            campaign.execute_recorded_campaign(
                                repo_root,
                                {"--run-id": "synthetic_publication"},
                                b"synthetic registry",
                                authorization,
                            ),
                            0,
                        )
                    else:
                        with self.assertRaisesRegex(
                            KeyboardInterrupt,
                            "post-rename publication gap",
                        ):
                            campaign.execute_recorded_campaign(
                                repo_root,
                                {"--run-id": "synthetic_publication"},
                                b"synthetic registry",
                                authorization,
                            )
                final = authorization.staging.final
                self.assertTrue(final.is_dir())
                self.assertEqual(
                    (final / "payload").read_bytes(), b"sealed payload\n"
                )
                if committed:
                    self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o555)
                    self.assertFalse((final / "failure.json").exists())
                else:
                    self.assertEqual(stat.S_IMODE(final.stat().st_mode), 0o700)
                    failure = json.loads(
                        (final / "failure.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(
                        failure["error_type"], "KeyboardInterrupt"
                    )

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

        pair_rows = []
        for pair_index, base in enumerate(
            (100_000_000, 200_000_000, 300_000_000)
        ):
            pair_rows.append({
                "schema_version": 1,
                "record_type": "pair_index",
                "sequence_index": 0,
                "sequence_id": campaign.SEQUENCES[0],
                "pair_index": pair_index,
                "anchor_filtered_index": pair_index * 2,
                "anchor_camera_id": 0,
                "cam0_filtered_index": pair_index * 2,
                "cam1_filtered_index": pair_index * 2 + 1,
                "cam0_record_time_ns": base,
                "cam1_record_time_ns": base + 1_000_000,
                "cam0_header_time_ns": base,
                "cam1_header_time_ns": base + 1_000_000,
                "absolute_record_delta_ns": 1_000_000,
            })
        canonical_pairs = campaign.schema.jsonl_bytes(pair_rows)
        self.assertEqual(
            campaign._validate_pair_index_bytes(
                canonical_pairs, 0, campaign.SEQUENCES[0]
            ),
            tuple(pair_rows),
        )
        noncanonical_pairs = b"".join(
            (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            for row in pair_rows
        )
        self.assertNotEqual(noncanonical_pairs, canonical_pairs)
        with self.assertRaises(campaign.CampaignError):
            campaign._validate_pair_index_bytes(
                noncanonical_pairs, 0, campaign.SEQUENCES[0]
            )
        compact_wrong_key_order = b"".join(
            (
                json.dumps(
                    row,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=False,
                )
                + "\n"
            ).encode("utf-8")
            for row in pair_rows
        )
        self.assertNotEqual(compact_wrong_key_order, canonical_pairs)
        with self.assertRaises(campaign.CampaignError):
            campaign._validate_pair_index_bytes(
                compact_wrong_key_order, 0, campaign.SEQUENCES[0]
            )
        for invalid_request in (False, 0.0):
            with self.subTest(pair_request=invalid_request):
                with self.assertRaises(campaign.CampaignError):
                    campaign._validate_pair_index_bytes(
                        canonical_pairs,
                        invalid_request,
                        campaign.SEQUENCES[0],
                    )
        for invalid_schema in (True, 1.0):
            corrupted = [dict(row) for row in pair_rows]
            corrupted[0]["schema_version"] = invalid_schema
            with self.assertRaises(campaign.CampaignError):
                campaign._validate_pair_index_bytes(
                    campaign.schema.jsonl_bytes(corrupted),
                    0,
                    campaign.SEQUENCES[0],
                )
        threshold = [dict(row) for row in pair_rows]
        threshold[0]["cam1_record_time_ns"] = (
            threshold[0]["cam0_record_time_ns"]
            + campaign.STRICT_PAIR_DELTA_NS
        )
        threshold[0]["absolute_record_delta_ns"] = (
            campaign.STRICT_PAIR_DELTA_NS
        )
        with self.assertRaises(campaign.CampaignError):
            campaign._validate_pair_index_bytes(
                campaign.schema.jsonl_bytes(threshold),
                0,
                campaign.SEQUENCES[0],
            )

        population_mutations = {}

        pair_index_boolean = [dict(row) for row in pair_rows]
        pair_index_boolean[1]["pair_index"] = True
        population_mutations["pair-index-boolean"] = pair_index_boolean

        delta_mismatch = [dict(row) for row in pair_rows]
        delta_mismatch[0]["absolute_record_delta_ns"] += 1
        population_mutations["delta-mismatch"] = delta_mismatch

        reused_camera = [dict(row) for row in pair_rows]
        reused_camera[0]["cam1_filtered_index"] = 3
        population_mutations["reused-camera"] = reused_camera

        reversed_anchors = [dict(row) for row in pair_rows]
        for row, anchor in zip(reversed_anchors, (4, 2, 6)):
            row["anchor_filtered_index"] = anchor
            row["cam0_filtered_index"] = anchor
            row["cam1_filtered_index"] = anchor + 1
        population_mutations["reversed-anchor-order"] = reversed_anchors

        reversed_times = [dict(row) for row in pair_rows]
        reversed_times[1]["cam0_record_time_ns"] = 90_000_000
        reversed_times[1]["cam1_record_time_ns"] = 91_000_000
        population_mutations["reversed-cam0-time"] = reversed_times

        zero_duration = [dict(row) for row in pair_rows]
        for row in zero_duration:
            row["cam0_record_time_ns"] = 100_000_000
            row["cam1_record_time_ns"] = 101_000_000
        population_mutations["zero-duration"] = zero_duration

        candidate_not_forward = [dict(row) for row in pair_rows]
        candidate_not_forward[0]["anchor_camera_id"] = 1
        candidate_not_forward[0]["anchor_filtered_index"] = 1
        population_mutations["candidate-not-forward"] = candidate_not_forward

        for label, corrupted in population_mutations.items():
            with self.subTest(pair_population=label):
                with self.assertRaises(campaign.CampaignError):
                    campaign._validate_pair_index_bytes(
                        campaign.schema.jsonl_bytes(corrupted),
                        0,
                        campaign.SEQUENCES[0],
                    )

        serial_rows = []
        for row in pair_rows:
            serial = dict(row)
            serial.update({
                "record_type": "serial_pair",
                "camera_timestamp_ns": row["cam0_header_time_ns"],
                "selected": True,
                "enqueue_entered": True,
                "enqueue_returned": True,
                "enqueue_status": "returned",
                "processing_entered": True,
                "processing_returned": True,
                "processing_status": "returned",
                "updater_invocation_ids": [],
            })
            serial_rows.append(serial)
        canonical_serial = campaign.schema.jsonl_bytes(serial_rows)
        self.assertEqual(
            campaign._project_serial_pairs_to_pair_index_bytes(
                canonical_serial, 0, campaign.SEQUENCES[0]
            ),
            canonical_pairs,
        )
        noncanonical_serial = b"".join(
            (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
            for row in serial_rows
        )
        with self.assertRaises(campaign.CampaignError):
            campaign._project_serial_pairs_to_pair_index_bytes(
                noncanonical_serial, 0, campaign.SEQUENCES[0]
            )
        compact_serial_wrong_key_order = b"".join(
            (
                json.dumps(
                    row,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=False,
                )
                + "\n"
            ).encode("utf-8")
            for row in serial_rows
        )
        self.assertNotEqual(compact_serial_wrong_key_order, canonical_serial)
        with self.assertRaises(campaign.CampaignError):
            campaign._project_serial_pairs_to_pair_index_bytes(
                compact_serial_wrong_key_order, 0, campaign.SEQUENCES[0]
            )

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

            # Prepared capsule execution records the frozen logical API while
            # execve receives only the descriptor-held transport executable.
            held_true = os.open(
                "/usr/bin/true", os.O_RDONLY | os.O_CLOEXEC
            )
            try:
                transport = "/proc/{}/fd/{}".format(
                    os.getpid(), held_true
                )
                logical_cwd = Path("/tmp/logical-capsule-work")
                logical_environment = dict(
                    environment, HOME="${PRIVATE_ROOT}/home"
                )
                prepared = recorder.run(
                    "evaluation",
                    [
                        "/staged/bin/launcher",
                        "--input",
                        "/staged/work/request.json",
                        "--output",
                        "/staged/work/response.json",
                    ],
                    Path("/tmp"),
                    "synthetic_prepared_capsule",
                    environment,
                    process_argv=(transport,),
                    process_executable=transport,
                    process_pass_fds=(held_true,),
                    evidence_variables=logical_environment,
                    evidence_cwd=logical_cwd,
                )
            finally:
                os.close(held_true)
            self.assertEqual(prepared["argv"][0], "/staged/bin/launcher")
            self.assertEqual(prepared["cwd"], str(logical_cwd))
            self.assertEqual(
                prepared["environment_sha256"],
                campaign.schema.command_environment_sha256(
                    logical_environment
                ),
            )

            fault_root = root / "stderr-open-fault"
            fault_root.mkdir()
            recorder = campaign.CommandRecorder(
                fault_root, _SyntheticAuthorization())
            real_open = os.open

            def reject_stderr(path, *args, **kwargs):
                if str(path).endswith("_verification.stderr"):
                    raise OSError("injected stderr acquisition failure")
                return real_open(path, *args, **kwargs)

            descriptors_before = len(os.listdir("/proc/self/fd"))
            with mock.patch.object(
                campaign.os, "open", side_effect=reject_stderr
            ):
                with self.assertRaisesRegex(
                    OSError, "injected stderr acquisition failure"
                ):
                    recorder.run(
                        "verification",
                        ["/usr/bin/true"],
                        Path("/tmp"),
                        "synthetic_stderr_failure",
                        environment,
                        timeout=5.0,
                    )
            self.assertEqual(
                len(os.listdir("/proc/self/fd")), descriptors_before
            )

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

            trusted_partial = root / "trusted-partial"
            trusted_partial.mkdir()
            verifier_owner = campaign.OwnedTemporaryWorkspace.create(
                "schurvio-cp2-final-verifier-"
            )
            delayed_path = verifier_owner.path / "delayed-mutation"
            sentinel_path = root / "unrelated-inherited-fd"
            sentinel_path.write_bytes(b"sentinel\n")
            sentinel_fd = os.open(
                str(sentinel_path), os.O_RDONLY | os.O_CLOEXEC
            )
            delayed_child = (
                "import os,sys,time;"
                "assert os.getsid(0)==os.getpid();"
                "payload=(str(os.getpid())+' '+str(os.getsid(0))+' '+"
                "os.readlink('/proc/self/ns/pid')+'\\n').encode('ascii');"
                "os.write(int(sys.argv[1]),payload);"
                "time.sleep(1.0);"
                "open(sys.argv[2],'wb').write(b'delayed\\n');"
                "os.write(1,b'DELAYED-STDOUT\\n')"
            )
            verifier.write_text(
                "import os,subprocess,sys\n"
                "status={}\n"
                "for line in open('/proc/self/status',encoding='ascii'):\n"
                "    if ':' in line:\n"
                "        key,value=line.split(':',1);status[key]=value.strip()\n"
                "assert os.getpid()==1\n"
                "assert all(status.get(key)=='0000000000000000' for key in "
                "('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))\n"
                "assert status.get('NoNewPrivs')=='1'\n"
                "sentinel=" + repr(str(sentinel_path)) + "\n"
                "for name in os.listdir('/proc/self/fd'):\n"
                "    try: target=os.readlink('/proc/self/fd/'+name)\n"
                "    except OSError: continue\n"
                "    assert target!=sentinel\n"
                "ready_read,ready_write=os.pipe()\n"
                "child=subprocess.Popen([sys.executable,'-c'," +
                repr(delayed_child) + ",str(ready_write)," +
                repr(str(delayed_path)) + "],pass_fds=(ready_write,),"
                "start_new_session=True)\n"
                "os.close(ready_write)\n"
                "with os.fdopen(ready_read,'rb') as stream:\n"
                "    ready=stream.readline().decode('ascii').strip()\n"
                "print('TRUSTED_DESCENDANT '+ready,flush=True)\n"
                "partial=sys.argv[sys.argv.index('--verify-recorded')+1]\n"
                "print('CP2-C recorded artifact independently verified: '+"
                "partial,flush=True)\n",
                encoding="utf-8",
            )

            class HeldVerifierAuthorization:
                @staticmethod
                def revalidate():
                    return None

                @staticmethod
                def duplicate_source_fd(relative):
                    if relative != "scripts/cp2/verify_report.py":
                        raise campaign.CampaignError("unexpected held source")
                    return os.open(
                        str(verifier), os.O_RDONLY | os.O_CLOEXEC
                    )

            try:
                campaign._trusted_parent_detached_verify_recorded(
                    trusted_partial,
                    "0" * 64,
                    HeldVerifierAuthorization(),
                    verifier_owner,
                )
                stdout_path = verifier_owner.path / "trusted-parent.stdout"
                stdout_before_delay = stdout_path.read_bytes()
                descendant_lines = [
                    line for line in stdout_before_delay.decode("utf-8").splitlines()
                    if line.startswith("TRUSTED_DESCENDANT ")
                ]
                self.assertEqual(len(descendant_lines), 1)
                _, namespace_pid, session_id, namespace_identity = (
                    descendant_lines[0].split()
                )
                self.assertEqual(namespace_pid, session_id)
                self.assertEqual(namespace_pid, "2")
                for namespace_path in Path("/proc").glob("[0-9]*/ns/pid"):
                    try:
                        observed_namespace = os.readlink(str(namespace_path))
                    except OSError:
                        continue
                    self.assertNotEqual(
                        observed_namespace, namespace_identity,
                        "setsid verifier descendant survived namespace exit",
                    )
                self.assertFalse(delayed_path.exists())
                time.sleep(1.25)
                self.assertFalse(delayed_path.exists())
                self.assertEqual(stdout_path.read_bytes(), stdout_before_delay)
                self.assertNotIn(b"DELAYED-STDOUT", stdout_before_delay)
            finally:
                os.close(sentinel_fd)
                verifier_owner.remove()


if __name__ == "__main__":
    unittest.main()
