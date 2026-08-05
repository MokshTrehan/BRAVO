#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protecting tests for CP2-E orchestration and publication."""

from __future__ import annotations

import contextlib
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(CP2_DIRECTORY))
sys.path.insert(0, str(TEST_DIRECTORY))

import cp2_schema  # noqa: E402
import cp2_timing_artifact as artifact  # noqa: E402
import cp2_timing_controls as controls  # noqa: E402
import cp2_timing_profile as profile_codec  # noqa: E402
import cp2_timing_publication as publication  # noqa: E402
import run_timing_pair as runner  # noqa: E402
import test_cp2_timing_evidence as evidence  # noqa: E402


def sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def make_profile(publication_parent: Path):
    value = evidence.valid_profile_value()
    value["artifact"]["publication_parent"] = str(publication_parent)
    value["privileged_helper"]["plan_sha256"] = (
        profile_codec.profile_plan_sha256(value)
    )
    return profile_codec.load_profile_bytes(
        profile_codec.canonical_profile_bytes(value)
    )


def binding_record(profile):
    templates = {
        "clock_pre": [
            "/usr/bin/true", "--profile", "{profile_sha256}",
            "--run", "{run_index}",
        ],
        "runtime_preflight": [
            "/usr/bin/true", "--profile", "{profile_sha256}",
            "--run", "{run_index}",
        ],
        "ros_run": [
            "/usr/bin/true", "--profile", "{profile_sha256}",
            "--run", "{run_index}", "--pair", "{timing_pair_index}",
            "--mode", "{mode}", "--run-id", "{run_id}",
            "--trace-directory", "{trace_directory}",
            "--sequence-index", "{sequence_index}",
            "--sequence-id", "{sequence_id}",
        ],
        "clock_post": [
            "/usr/bin/true", "--profile", "{profile_sha256}",
            "--run", "{run_index}",
        ],
    }
    return {
        "schema_version": 1,
        "record_type": "cp2_timing_execution_binding",
        "checkpoint": "CP2-E",
        "profile_sha256": profile.sha256,
        "source_commit": "b" * 40,
        "source_tree": "c" * 40,
        "unit_manifest_sha256": "d" * 64,
        "sequence_set_manifest_sha256": "e" * 64,
        "executable_path": "/usr/bin/true",
        "executable_sha256": "1" * 64,
        "dso_closure_sha256": "2" * 64,
        "configuration_sha256": "3" * 64,
        "boot_id_sha256": "5" * 64,
        "machine_identity_sha256": profile.value["target"]["machine_identity_sha256"],
        "cwd": "/tmp",
        "environment": profile.value["runtime"]["environment"],
        "phase_templates": templates,
    }


class SyntheticControlTransaction:
    def __init__(self, profile):
        self.profile = profile
        self.state = "new"
        self.apply_count = 0
        self.validate_count = 0
        self.restore_count = 0
        self.had_abnormal_recovery = False
        self.prior = controls.ControlSnapshot(
            values=(
                (profile.value["controls"]["writes"][0]["path"], "integer", "1"),
                (profile.value["controls"]["writes"][1]["path"], "text", "ondemand"),
            ),
            modules_preexisting=("msr",), services_active=("irqbalance",),
            process_affinity=(0, 1),
        )
        self.applied = evidence.stable_control_state(profile)
        self.prior_receipt = None
        self.applied_receipt = None
        self.restored_receipt = None

    def apply(self):
        if self.state != "new":
            raise RuntimeError("duplicate apply")
        self.apply_count += 1
        self.state = "applied"
        self.prior_receipt = controls.ControlStateReceipt(
            self.profile.sha256, "prior", 1_000, dict(self.prior.as_record()),
        )
        self.applied_receipt = controls.ControlStateReceipt(
            self.profile.sha256, "applied", 5_000, dict(self.applied),
        )
        return self.prior

    def validate_applied(self):
        if self.state != "applied":
            raise RuntimeError("validation outside applied state")
        self.validate_count += 1

    def capture_applied_state(self):
        if self.state != "applied":
            raise RuntimeError("capture outside applied state")
        return dict(self.applied)

    def restore(self):
        self.restore_count += 1
        self.restored_receipt = controls.ControlStateReceipt(
            self.profile.sha256, "restored", 700_000,
            dict(self.prior.as_record()),
        )
        self.state = "restored"

    def control_receipts(self):
        if self.state != "restored":
            raise RuntimeError("receipts before restoration")
        return {
            "prior": self.prior_receipt,
            "applied": self.applied_receipt,
            "restored": self.restored_receipt,
        }

    def helper_evidence(self, require_success):
        if self.state != "restored":
            raise RuntimeError("helper evidence before restoration")
        (
            transcript, terminal_payload, guardian_evidence,
            control_evidence, _provenance,
        ) = evidence.helper_evidence(
            self.profile, dict(self.prior.as_record()), dict(self.applied),
        )
        if not require_success:
            terminal = json.loads(terminal_payload)
            terminal["terminal_status"] = "fail_closed"
            terminal["error_type"] = "SyntheticPhaseFailure"
            terminal_payload = canonical(terminal)
        return {
            "transcript_bytes": transcript,
            "terminal_receipt_bytes": terminal_payload,
            "guardian_evidence_bytes": guardian_evidence,
            "control_evidence_bytes": control_evidence,
        }


class SyntheticPhaseExecutor:
    def __init__(self, profile, binding, transaction):
        self.profile = profile
        self.binding = binding
        self.transaction = transaction
        self.requests = []
        self.next_time = 1
        self.fail_at = None
        self.duplicate_process = False

    def _context(self, request):
        trace = request.trace_directory
        path = lambda name: trace + "/" + name
        parameters = {"/cp2_vio/up_msckf_landmark_elimination": request.mode}
        return {
            "schema_version": 1,
            "record_type": "cp2_runtime_context",
            "checkpoint": "CP2-E",
            "run_id": request.run_id,
            "sequence_index": self.profile.value["input"]["sequence_index"],
            "sequence_id": self.profile.value["input"]["sequence_id"],
            "mode": request.mode,
            "shadow_enabled": False,
            "trace_level": "timing",
            "source_commit": self.binding.source_commit,
            "config_sha256": self.binding.configuration_sha256,
            "bag_sha256": self.profile.value["input"]["bag_sha256"],
            "pair_index_sha256": None,
            "resolved_parameters_sha256": cp2_schema.resolved_parameters_sha256(parameters),
            "trace_directory": trace,
            "serial_trace_path": path("serial.jsonl"),
            "callback_trace_path": path("callbacks.jsonl"),
            "trajectory_trace_path": None,
            "updater_trace_path": path("updater.jsonl"),
            "state_payload_path": None,
            "proposal_payload_path": None,
            "raw_system_payload_path": None,
            "timing_trace_path": path("timing.jsonl"),
            "runtime_parameters_path": path("runtime_parameters.json"),
            "loader_map_before_path": path("loader_before.txt"),
            "loader_map_after_path": path("loader_after.txt"),
            "legacy_state_path": None,
            "legacy_deviation_path": None,
            "legacy_timing_path": None,
        }

    def __call__(self, request):
        self.requests.append(request)
        if self.fail_at == (request.run_index, request.phase):
            raise RuntimeError("injected phase failure")
        started, ended = evidence.phase_interval(request.run_index, request.phase)
        payload = None
        support = {}
        process = None
        raw_telemetry = None
        if request.phase in ("clock_pre", "clock_post"):
            provisional = {
                "boot_id_sha256": self.binding.boot_id_sha256,
                "control_applied_sha256": request.control_applied_sha256,
            }
            payload = evidence.clock_snapshot(
                self.profile,
                provisional,
                request.run_index,
                "pre" if request.phase == "clock_pre" else "post",
                command_start=started,
            )
            raw_telemetry = evidence.raw_telemetry_snapshot(
                request.run_index,
                "pre" if request.phase == "clock_pre" else "post",
                command_start=started,
            )
        elif request.phase == "ros_run":
            serial, callbacks, updater, timing = evidence.raw_trace_rows(
                request.run_index, request.mode, None
            )
            parameters = {"/cp2_vio/up_msckf_landmark_elimination": request.mode}
            loader_before = b"loader-map\n"
            loader_after = b"loader-map\n"
            pid = 1000 if self.duplicate_process and request.run_index == 1 else 1000 + request.run_index
            start_ticks = 2000 if self.duplicate_process and request.run_index == 1 else 2000 + request.run_index
            affinity = canonical({
                "schema_version": 1,
                "record_type": "cp2_timing_affinity_audit",
                "root_pid": pid,
                "expected_cpus": [0],
                "poll_interval_ns": 1_000_000,
                "observation_count": 1,
                "observations": [{
                    "monotonic_ns": started,
                    "tid": pid,
                    "cpu_ids": [0],
                }],
                "passed": True,
            })
            support = {
                "context.json": canonical(self._context(request)),
                "serial.jsonl": b"".join(canonical(row) for row in serial),
                "callbacks.jsonl": b"".join(canonical(row) for row in callbacks),
                "updater.jsonl": b"".join(canonical(row) for row in updater),
                "timing.jsonl": b"".join(canonical(row) for row in timing),
                "runtime_parameters.json": canonical(parameters),
                "loader_before.txt": loader_before,
                "loader_after.txt": loader_after,
                "affinity_audit.json": affinity,
            }
            process = runner.ProcessIdentityObservation(
                observed_monotonic_ns=started + 100,
                pid=pid,
                start_time_ticks=start_ticks,
                executable_sha256=self.binding.executable_sha256,
                loader_before_sha256=sha(loader_before),
                loader_after_sha256=sha(loader_after),
            )
        stdout = b"runtime-preflight-ok\n" if request.phase == "runtime_preflight" else b""
        return runner.TimingPhaseOutcome(
            started, ended, 0, False, True, stdout, b"", payload, support,
            process, raw_telemetry,
        )


def writable_rmtree(path: Path):
    if not path.exists():
        return
    for root, directories, _files in os.walk(path, topdown=False):
        for directory in directories:
            os.chmod(os.path.join(root, directory), 0o700)
        os.chmod(root, 0o700)
    shutil.rmtree(path)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(
            tempfile.mkdtemp(prefix="cp2e-publication-test-", dir="/tmp")
        )

    def tearDown(self):
        writable_rmtree(self.temporary)

    def make_staging(self, suffix="one"):
        staging = self.temporary / (".artifact.partial." + suffix)
        identity = publication.create_staging_directory(staging)
        (staging / "nested").mkdir(mode=0o700)
        (staging / "nested" / "evidence.json").write_bytes(b"{}\n")
        self.assertEqual(publication.seal_artifact_directory(staging), identity)
        return staging, identity

    def test_success_is_held_inode_noreplace_and_durably_validated(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        validated = []
        result = publication.publish_artifact_noreplace(
            staging, destination,
            published_validator=lambda path: validated.append(path),
        )
        self.assertEqual(result.identity, identity)
        self.assertEqual(validated, [destination, destination])
        self.assertFalse(staging.exists())
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), identity)

    def test_name_collision_preserves_both_exact_inodes(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        destination.mkdir(mode=0o700)
        other = (destination.stat().st_dev, destination.stat().st_ino)
        with self.assertRaises(publication.TimingPublicationCollision):
            publication.publish_artifact_noreplace(staging, destination)
        self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), identity)
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), other)

    def test_interruption_at_each_forward_boundary_rolls_back_or_never_renames(self):
        boundaries = (
            "before_parent_fsync", "after_parent_fsync", "before_forward_rename",
            "after_forward_rename", "after_forward_fsync",
            "after_final_identity_validation", "after_published_validation",
        )
        for index, boundary in enumerate(boundaries):
            with self.subTest(boundary=boundary):
                staging, identity = self.make_staging(str(index))
                destination = self.temporary / ("artifact-" + str(index))

                def hook(name):
                    if name == boundary:
                        raise KeyboardInterrupt("injected at " + name)

                with self.assertRaisesRegex(KeyboardInterrupt, boundary):
                    publication.publish_artifact_noreplace(
                        staging, destination, boundary_hook=hook
                    )
                self.assertFalse(destination.exists())
                self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), identity)

    def test_postrename_parent_fsync_failure_rolls_back_and_fsyncs_parent(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        calls = 0

        def fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected post-rename fsync failure")
            os.fsync(descriptor)

        with self.assertRaisesRegex(OSError, "post-rename"):
            publication.publish_artifact_noreplace(
                staging, destination, fsync_operation=fsync
            )
        self.assertEqual(calls, 3)
        self.assertFalse(destination.exists())
        self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), identity)

    def test_rollback_fsync_failure_is_explicitly_indeterminate(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        calls = 0

        def fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls in (2, 3):
                raise OSError("injected fsync failure")
            os.fsync(descriptor)

        with self.assertRaisesRegex(
            publication.TimingPublicationIndeterminate, "indeterminate"
        ):
            publication.publish_artifact_noreplace(
                staging, destination, fsync_operation=fsync
            )
        self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), identity)
        self.assertFalse(destination.exists())

    def test_rollback_collision_is_indeterminate_and_never_overwrites(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        calls = 0

        def rename(parent_fd, old_name, new_name):
            nonlocal calls
            calls += 1
            if calls == 2:
                os.mkdir(new_name, 0o700, dir_fd=parent_fd)
            publication._renameat2_noreplace(parent_fd, old_name, new_name)

        def interrupt(name):
            if name == "after_forward_rename":
                raise KeyboardInterrupt("force rollback")

        with self.assertRaisesRegex(
            publication.TimingPublicationIndeterminate, "indeterminate"
        ):
            publication.publish_artifact_noreplace(
                staging, destination, rename_operation=rename,
                boundary_hook=interrupt,
            )
        self.assertEqual((destination.stat().st_dev, destination.stat().st_ino), identity)
        self.assertTrue(staging.is_dir())

    def test_interruption_at_every_rollback_boundary_is_indeterminate(self):
        for index, target in enumerate((
            "before_rollback_rename", "after_rollback_rename",
            "after_rollback_fsync",
        )):
            with self.subTest(boundary=target):
                staging, identity = self.make_staging("rollback-" + str(index))
                destination = self.temporary / ("rollback-final-" + str(index))

                def hook(name):
                    if name == "after_forward_rename":
                        raise KeyboardInterrupt("start rollback")
                    if name == target:
                        raise KeyboardInterrupt("interrupt " + target)

                with self.assertRaisesRegex(
                    publication.TimingPublicationIndeterminate, "indeterminate"
                ):
                    publication.publish_artifact_noreplace(
                        staging, destination, boundary_hook=hook,
                        expected_staging_identity=identity,
                    )
                locations = []
                for path in (staging, destination):
                    if path.exists() and (path.stat().st_dev, path.stat().st_ino) == identity:
                        locations.append(path)
                self.assertEqual(len(locations), 1)

    def test_published_validator_failure_rolls_back_before_return(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"

        def reject(_path):
            raise ValueError("detached verifier rejected")

        with self.assertRaisesRegex(ValueError, "detached verifier"):
            publication.publish_artifact_noreplace(
                staging, destination, published_validator=reject
            )
        self.assertFalse(destination.exists())
        self.assertEqual((staging.stat().st_dev, staging.stat().st_ino), identity)

    def test_postverification_name_substitution_is_indeterminate(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        displaced = self.temporary / "validated-but-displaced"

        def substitute(path):
            path.rename(displaced)
            path.mkdir(mode=0o555)

        with self.assertRaisesRegex(
            publication.TimingPublicationIndeterminate,
            "irreconcilable exact held inode",
        ):
            publication.publish_artifact_noreplace(
                staging, destination, published_validator=substitute
            )
        self.assertEqual(
            (displaced.stat().st_dev, displaced.stat().st_ino), identity
        )
        self.assertNotEqual(
            (destination.stat().st_dev, destination.stat().st_ino), identity
        )

    def test_exact_caller_cleanup_rejects_substituted_name(self):
        staging, identity = self.make_staging()
        displaced = self.temporary / "displaced"
        staging.rename(displaced)
        staging.mkdir(mode=0o700)
        with self.assertRaisesRegex(publication.TimingPublicationError, "substituted"):
            publication.cleanup_unpublished_artifact(staging, identity)
        self.assertEqual((displaced.stat().st_dev, displaced.stat().st_ino), identity)

    def test_exact_caller_cleanup_removes_only_the_bound_unpublished_inode(self):
        staging, identity = self.make_staging()
        publication.cleanup_unpublished_artifact(staging, identity)
        self.assertFalse(staging.exists())

    def test_coordinated_source_substitution_is_indeterminate(self):
        staging, identity = self.make_staging()
        destination = self.temporary / "artifact"
        displaced = self.temporary / "held-displaced"

        def substitute(parent_fd, old_name, new_name):
            os.rename(
                old_name, displaced.name,
                src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
            )
            os.mkdir(old_name, 0o555, dir_fd=parent_fd)
            publication._renameat2_noreplace(parent_fd, old_name, new_name)

        with self.assertRaisesRegex(
            publication.TimingPublicationIndeterminate, "irreconcilable"
        ):
            publication.publish_artifact_noreplace(
                staging, destination, rename_operation=substitute,
                expected_staging_identity=identity,
            )
        self.assertEqual((displaced.stat().st_dev, displaced.stat().st_ino), identity)
        self.assertNotEqual((destination.stat().st_dev, destination.stat().st_ino), identity)

    def test_posthook_same_root_content_or_mode_mutation_never_returns_success(self):
        for index, mutation in enumerate(("content", "mode")):
            with self.subTest(mutation=mutation):
                staging, identity = self.make_staging("posthook-{}".format(index))
                destination = self.temporary / ("posthook-final-{}".format(index))
                expected = b"{}\n"
                validations = []

                def validator(path):
                    validations.append(path)
                    if (path / "nested/evidence.json").read_bytes() != expected:
                        raise ValueError("detached digest mismatch")

                def mutate_after_first_validation(boundary):
                    if boundary != "after_published_validation":
                        return
                    target = destination / "nested/evidence.json"
                    os.chmod(target, 0o644)
                    if mutation == "content":
                        target.write_bytes(b'{"substituted":true}\n')
                        os.chmod(target, 0o444)

                expected_error = (
                    ValueError if mutation == "content"
                    else publication.TimingPublicationError
                )
                with self.assertRaises(expected_error):
                    publication.publish_artifact_noreplace(
                        staging, destination, published_validator=validator,
                        boundary_hook=mutate_after_first_validation,
                        expected_staging_identity=identity,
                    )
                self.assertFalse(destination.exists())
                self.assertEqual(
                    (staging.stat().st_dev, staging.stat().st_ino), identity,
                )
                self.assertEqual(len(validations), 2 if mutation == "content" else 1)

    def test_caller_identity_rejects_substitution_before_forward_rename(self):
        staging, identity = self.make_staging()
        displaced = self.temporary / "held-displaced"
        staging.rename(displaced)
        replacement, _replacement_identity = self.make_staging("replacement")
        replacement.rename(staging)
        destination = self.temporary / "artifact"
        with self.assertRaisesRegex(publication.TimingPublicationError, "caller-held"):
            publication.publish_artifact_noreplace(
                staging, destination, expected_staging_identity=identity
            )
        self.assertFalse(destination.exists())
        self.assertEqual((displaced.stat().st_dev, displaced.stat().st_ino), identity)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = Path(
            tempfile.mkdtemp(prefix="cp2e-orchestration-test-", dir="/tmp")
        )
        self.profile = make_profile(self.temporary)
        self.binding = runner.load_execution_binding(binding_record(self.profile))

    def tearDown(self):
        writable_rmtree(self.temporary)

    def run_campaign(self, transaction=None, executor=None):
        transaction = transaction or SyntheticControlTransaction(self.profile)
        executor = executor or SyntheticPhaseExecutor(
            self.profile, self.binding, transaction
        )
        result = runner._run_synthetic_orchestration(
            synthetic_authorization=runner.SYNTHETIC_ONLY_AUTHORIZATION,
            frozen_profile=self.profile,
            execution_binding=self.binding,
            campaign_id="synthetic-campaign",
            bag_begin_record_time_ns=1_000_000_000,
            working_root=str(self.temporary / "work"),
            staging_path=str(self.temporary / ".synthetic-campaign.partial.one"),
            destination_path=str(self.temporary / "synthetic-campaign"),
            control_transaction=transaction,
            phase_executor=executor,
        )
        return result, transaction, executor

    def test_complete_six_fresh_process_campaign_assembles_verifies_and_publishes(self):
        result, transaction, executor = self.run_campaign()
        self.assertTrue(result.passed)
        self.assertEqual(len(executor.requests), 24)
        self.assertEqual(
            [(request.run_index, request.phase) for request in executor.requests],
            [(index, phase) for index in range(6) for phase in runner.COMMAND_PHASES],
        )
        self.assertEqual(
            [request.mode for request in executor.requests if request.phase == "ros_run"],
            [mode for pair in runner.PAIR_ORDER for mode in pair],
        )
        self.assertEqual(transaction.apply_count, 1)
        self.assertEqual(transaction.restore_count, 1)
        self.assertEqual(transaction.state, "restored")
        verification = artifact.verify_timing_artifact(
            result.artifact, result.manifest_sha256
        )
        self.assertTrue(verification["passed"])
        self.assertEqual((verification["run_count"], verification["pair_count"]), (6, 3))
        guardian_rows = [
            json.loads(line)
            for line in (result.artifact / "guardian_evidence.jsonl").read_bytes(
            ).splitlines()
        ]
        self.assertTrue(guardian_rows)
        self.assertTrue(all(
            row["foreign_affinity_eligibility"] for row in guardian_rows
        ))
        self.assertEqual(
            (result.artifact / "execution_binding.json").read_bytes(),
            self.binding.canonical_bytes,
        )
        provenance = json.loads((result.artifact / "provenance.json").read_bytes())
        for phase in ("prior", "applied", "restored"):
            payload = (result.artifact / ("control_{}.json".format(phase))).read_bytes()
            self.assertEqual(provenance["control_{}_sha256".format(phase)], sha(payload))
        for name in (
            "process_identity.json", "runtime_preflight.json",
            "telemetry_pre.json", "telemetry_post.json", "telemetry_comparison.json",
        ):
            self.assertTrue((result.artifact / "runs/run_0" / name).is_file())
        self.assertFalse(
            (self.temporary / ".synthetic-campaign.partial.one").exists()
        )

    def test_phase_failure_restores_controls_without_staging_or_retry(self):
        transaction = SyntheticControlTransaction(self.profile)
        executor = SyntheticPhaseExecutor(self.profile, self.binding, transaction)
        executor.fail_at = (2, "ros_run")
        with self.assertRaisesRegex(RuntimeError, "injected phase failure"):
            self.run_campaign(transaction, executor)
        self.assertEqual(transaction.state, "restored")
        self.assertEqual(transaction.restore_count, 1)
        self.assertEqual(
            sum(request.run_index == 2 and request.phase == "ros_run" for request in executor.requests),
            1,
        )
        self.assertFalse(
            (self.temporary / ".synthetic-campaign.partial.one").exists()
        )
        self.assertFalse((self.temporary / "synthetic-campaign").exists())

    def test_clock_phase_without_raw_telemetry_is_rejected_and_restored(self):
        class MissingRawTelemetry(SyntheticPhaseExecutor):
            def __call__(self, request):
                outcome = super().__call__(request)
                if request.run_index == 0 and request.phase == "clock_pre":
                    return replace(outcome, raw_telemetry=None)
                return outcome

        transaction = SyntheticControlTransaction(self.profile)
        executor = MissingRawTelemetry(self.profile, self.binding, transaction)
        with self.assertRaisesRegex(
            runner.TimingOrchestrationError, "raw telemetry",
        ):
            self.run_campaign(transaction, executor)
        self.assertEqual(transaction.state, "restored")
        self.assertFalse((self.temporary / "synthetic-campaign").exists())

    def test_phase_typed_control_receipt_and_exact_restoration_are_mandatory(self):
        class WrongAppliedReceipt(SyntheticControlTransaction):
            def apply(self):
                prior = super().apply()
                self.applied_receipt = controls.ControlStateReceipt(
                    self.profile.sha256, "prior", 5_000, dict(self.applied),
                )
                return prior

        wrong = WrongAppliedReceipt(self.profile)
        wrong_executor = SyntheticPhaseExecutor(self.profile, self.binding, wrong)
        with self.assertRaisesRegex(
            runner.TimingOrchestrationError, "applied control receipt",
        ):
            self.run_campaign(wrong, wrong_executor)
        self.assertEqual(wrong.state, "restored")

        class DriftedRestoration(SyntheticControlTransaction):
            def restore(self):
                self.restore_count += 1
                state = dict(self.prior.as_record())
                state["process_affinity"] = [0, 2]
                self.restored_receipt = controls.ControlStateReceipt(
                    self.profile.sha256, "restored", 700_000, state,
                )
                self.state = "restored"

        drifted = DriftedRestoration(self.profile)
        drifted_executor = SyntheticPhaseExecutor(
            self.profile, self.binding, drifted,
        )
        with self.assertRaisesRegex(
            runner.TimingOrchestrationError, "restoration is inexact",
        ):
            self.run_campaign(drifted, drifted_executor)
        self.assertFalse((self.temporary / "synthetic-campaign").exists())

    def test_duplicate_pid_start_observation_fails_before_artifact_creation(self):
        transaction = SyntheticControlTransaction(self.profile)
        executor = SyntheticPhaseExecutor(self.profile, self.binding, transaction)
        executor.duplicate_process = True
        with self.assertRaisesRegex(
            runner.TimingOrchestrationError, "six fresh process"
        ):
            self.run_campaign(transaction, executor)
        self.assertEqual(transaction.state, "restored")
        self.assertFalse(
            (self.temporary / ".synthetic-campaign.partial.one").exists()
        )

    def test_abnormal_guardian_recovery_cannot_be_reported_as_a_formal_run(self):
        class AbnormalTransaction(SyntheticControlTransaction):
            def restore(self):
                super().restore()
                self.had_abnormal_recovery = True

        transaction = AbnormalTransaction(self.profile)
        executor = SyntheticPhaseExecutor(self.profile, self.binding, transaction)
        with self.assertRaisesRegex(
            runner.TimingOrchestrationIndeterminate, "abnormal guardian"
        ):
            self.run_campaign(transaction, executor)
        self.assertFalse(
            (self.temporary / ".synthetic-campaign.partial.one").exists()
        )

    def test_binding_rejects_arbitrary_ros_executable_and_missing_tokens(self):
        for mutation in ("executable", "token"):
            value = binding_record(self.profile)
            if mutation == "executable":
                value["phase_templates"]["ros_run"][0] = "/usr/bin/false"
            else:
                value["phase_templates"]["ros_run"].remove("{run_id}")
            with self.subTest(mutation=mutation), self.assertRaises(
                runner.TimingOrchestrationError
            ):
                runner.load_execution_binding(value)

    def test_launch_binding_digest_changes_with_any_exact_argv_change(self):
        first = runner.load_execution_binding(binding_record(self.profile))
        changed = binding_record(self.profile)
        changed["phase_templates"]["runtime_preflight"].append("literal")
        second = runner.load_execution_binding(changed)
        self.assertNotEqual(first.launch_sha256, second.launch_sha256)

    def test_synthetic_seam_requires_unforgeable_inprocess_capability(self):
        transaction = SyntheticControlTransaction(self.profile)
        executor = SyntheticPhaseExecutor(self.profile, self.binding, transaction)
        with self.assertRaisesRegex(runner.TimingOrchestrationError, "authorization"):
            runner._run_synthetic_orchestration(
                synthetic_authorization=object(),
                frozen_profile=self.profile,
                execution_binding=self.binding,
                campaign_id="synthetic-campaign",
                bag_begin_record_time_ns=1_000_000_000,
                working_root=str(self.temporary / "work"),
                staging_path=str(self.temporary / ".synthetic-campaign.partial.one"),
                destination_path=str(self.temporary / "synthetic-campaign"),
                control_transaction=transaction,
                phase_executor=executor,
            )
        self.assertEqual(transaction.state, "new")

    def test_actual_entrypoint_stops_before_profile_lookup_or_project_import(self):
        stderr = io.StringIO()
        arguments = (
            "--unit-artifact", "/tmp/unit-artifact",
            "--unit-manifest-sha256", "0" * 64,
        )
        original_lexists = runner.os.path.lexists

        def forbidden_lookup(_path):
            raise AssertionError("profile lookup occurred")

        runner.os.path.lexists = forbidden_lookup
        try:
            with contextlib.redirect_stderr(stderr):
                status = runner._actual_mode(arguments)
        finally:
            runner.os.path.lexists = original_lexists
        self.assertEqual(status, getattr(os, "EX_CONFIG", 78))
        self.assertIn("before registry or bag access", stderr.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
