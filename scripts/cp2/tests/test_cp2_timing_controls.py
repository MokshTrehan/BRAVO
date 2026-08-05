#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free fault and recovery tests for the CP2-E host-control boundary."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(CP2_DIRECTORY))
sys.path.insert(0, str(TEST_DIRECTORY))

import cp2_timing_controls as controls  # noqa: E402
import cp2_timing_profile as profile_codec  # noqa: E402
from test_cp2_timing_evidence import valid_profile_value  # noqa: E402


def frozen_profile(*, journal_parent="/tmp", mutate=None):
    value = valid_profile_value()
    value["controls"]["recovery_journal_parent"] = journal_parent
    if mutate is not None:
        mutate(value)
    value["privileged_helper"]["plan_sha256"] = (
        profile_codec.profile_plan_sha256(value)
    )
    return profile_codec.load_profile_bytes(profile_codec.canonical_profile_bytes(value))


class OneShotFault:
    def __init__(self, target=None):
        self.target = target
        self.seen = []
        self.fired = False

    def __call__(self, boundary):
        self.seen.append(boundary)
        if boundary == self.target and not self.fired:
            self.fired = True
            raise controls.TimingControlError("injected boundary " + boundary)


class ExactSyntheticBackend:
    external_recovery_supported = False

    def __init__(self):
        self.values = {
            "/sys/devices/system/cpu/cpufreq/boost": "1",
            "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor": "ondemand",
        }
        self.modules = {"msr"}
        self.services = {"irqbalance"}
        self.cpus = (0, 1)
        self.dma = False
        self.privilege = True
        self.topology = ((0, 2), (1, 3))
        self.post_failure = None
        self.telemetry = {}

    def require_privilege(self):
        if not self.privilege:
            raise controls.TimingControlError("synthetic privilege unavailable")

    def read_text(self, path):
        return self.values[path]

    def write_text(self, path, value):
        self.values[path] = value
        if self.post_failure == ("write", path):
            self.post_failure = None
            raise controls.TimingControlError("synthetic post-write failure")

    def module_loaded(self, name):
        return name in self.modules

    def service_active(self, name):
        return name in self.services

    def command(self, argv):
        name = argv[-1]
        if "/usr/sbin/modprobe" in argv:
            if "-r" in argv:
                self.modules.discard(name)
            else:
                self.modules.add(name)
            kind = "module"
        elif "stop" in argv:
            self.services.discard(name)
            kind = "service"
        elif "start" in argv:
            self.services.add(name)
            kind = "service"
        else:
            raise controls.TimingControlError("synthetic command is unsupported")
        if self.post_failure == (kind, name):
            self.post_failure = None
            raise controls.TimingControlError("synthetic post-command failure")

    def affinity(self):
        return self.cpus

    def set_affinity(self, cpus):
        self.cpus = tuple(cpus)
        if self.post_failure == ("affinity", "process"):
            self.post_failure = None
            raise controls.TimingControlError("synthetic post-affinity failure")

    def hold_dma_latency(self, _value):
        self.dma = True
        if self.post_failure == ("dma", "hold"):
            self.post_failure = None
            raise controls.TimingControlError("synthetic post-DMA failure")

    def dma_latency_active(self):
        return self.dma

    def release_dma_latency(self):
        self.dma = False

    def smt_sibling_groups(self, _cpus):
        return self.topology

    def read_telemetry_raw(self, specification):
        return self.telemetry[specification.metric_id]

    def prior_record(self):
        return {
            "values": copy.deepcopy(self.values),
            "modules": set(self.modules),
            "services": set(self.services),
            "cpus": self.cpus,
            "dma": self.dma,
        }

    def assert_prior(self, testcase, prior):
        testcase.assertEqual(self.values, prior["values"])
        testcase.assertEqual(self.modules, prior["modules"])
        testcase.assertEqual(self.services, prior["services"])
        testcase.assertEqual(self.cpus, prior["cpus"])
        testcase.assertEqual(self.dma, prior["dma"])


class SharedFileBackend(ExactSyntheticBackend):
    """Fork-visible synthetic state used to exercise caller-death recovery."""

    external_recovery_supported = True

    def __init__(self, state_path):
        super().__init__()
        self.state_path = state_path
        if not os.path.exists(state_path):
            self._save({
                "values": self.values,
                "modules": sorted(self.modules),
                "services": sorted(self.services),
            })

    def _load(self):
        with open(self.state_path, "r", encoding="utf-8") as stream:
            return json.load(stream)

    def _save(self, value):
        temporary = self.state_path + ".tmp-" + str(os.getpid())
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(value, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.state_path)

    def read_text(self, path):
        return self._load()["values"][path]

    def write_text(self, path, value):
        state = self._load()
        state["values"][path] = value
        self._save(state)

    def module_loaded(self, name):
        return name in self._load()["modules"]

    def service_active(self, name):
        return name in self._load()["services"]

    def command(self, argv):
        state = self._load()
        name = argv[-1]
        if "/usr/sbin/modprobe" in argv:
            retained = set(state["modules"])
            retained.discard(name) if "-r" in argv else retained.add(name)
            state["modules"] = sorted(retained)
        elif "stop" in argv or "start" in argv:
            retained = set(state["services"])
            retained.discard(name) if "stop" in argv else retained.add(name)
            state["services"] = sorted(retained)
        else:
            raise controls.TimingControlError("shared synthetic command is unsupported")
        self._save(state)


def telemetry_specs(cpus=(0, 1)):
    retained = []
    for cpu in cpus:
        retained.extend((
            controls.RawTelemetrySpec(
                "cpu{}.aperf".format(cpu), "aperf", cpu, "msr_u64_le",
                "/dev/cpu/{}/msr".format(cpu), controls.MSR_APERF,
            ),
            controls.RawTelemetrySpec(
                "cpu{}.frequency".format(cpu), "scaling_current_frequency_khz", cpu,
                "text_integer",
                "/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq".format(cpu),
            ),
            controls.RawTelemetrySpec(
                "cpu{}.mperf".format(cpu), "mperf", cpu, "msr_u64_le",
                "/dev/cpu/{}/msr".format(cpu), controls.MSR_MPERF,
            ),
            controls.RawTelemetrySpec(
                "cpu{}.throttle".format(cpu), "thermal_throttle_count", cpu,
                "text_integer",
                "/sys/devices/system/cpu/cpu{}/thermal_throttle/core_throttle_count".format(cpu),
            ),
        ))
    retained.append(controls.RawTelemetrySpec(
        "package.temperature", "temperature_millicelsius", None,
        "text_integer", "/sys/class/hwmon/hwmon0/temp1_input",
    ))
    return tuple(sorted(retained, key=lambda item: item.metric_id))


def telemetry_payload(specification, *, phase, bad=None):
    role = specification.role
    cpu = specification.cpu_id or 0
    values = {
        "aperf": 10_000 + cpu * 100 + phase * 1_000,
        "mperf": 20_000 + cpu * 100 + phase * 1_000,
        "thermal_throttle_count": 7,
        "scaling_current_frequency_khz": 3_000_000,
        "temperature_millicelsius": 55_000 + phase * 1_000,
    }
    value = values[role] if bad is None else bad
    if specification.source_kind == "msr_u64_le":
        return int(value).to_bytes(8, "little")
    return (str(value) + "\n").encode("ascii")


class TransactionStateMachineTests(unittest.TestCase):
    def test_exact_apply_snapshot_validate_capture_and_restore(self):
        backend = ExactSyntheticBackend()
        prior = backend.prior_record()
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        snapshot = transaction.apply()
        state = transaction.capture_applied_state()
        self.assertEqual(
            state["process_affinity"],
            frozen_profile().value["runtime"]["cpu_ids"],
        )
        self.assertEqual(state["modules_loaded"], ["k10temp", "msr"])
        self.assertEqual(state["services_active"], [])
        self.assertEqual(len(controls.snapshot_digest(snapshot)), 64)
        journal = transaction.journal_path
        self.assertTrue(os.path.exists(journal))
        status = os.stat(journal, follow_symlinks=False)
        self.assertEqual(status.st_mode & 0o777, 0o600)
        self.assertEqual(status.st_nlink, 1)
        transaction.restore()
        backend.assert_prior(self, prior)
        self.assertEqual(transaction.state, "restored")
        self.assertFalse(os.path.exists(journal))
        receipts = transaction.control_receipts()
        self.assertEqual(receipts["prior"].state, receipts["restored"].state)
        self.assertNotEqual(receipts["prior"].sha256, receipts["applied"].sha256)
        self.assertTrue(receipts["restored"].canonical_bytes.endswith(b"\n"))
        self.assertEqual(len(receipts["restored"].state_sha256), 64)

    def test_every_observed_apply_boundary_is_failure_atomic(self):
        recorder = OneShotFault()
        clean_backend = ExactSyntheticBackend()
        clean = controls.HostControlTransaction(
            frozen_profile(), clean_backend, fault_injector=recorder,
        )
        clean.apply()
        clean.restore()
        boundaries = tuple(dict.fromkeys(
            item for item in recorder.seen if item.startswith("apply:")
        ))
        self.assertGreaterEqual(len(boundaries), 14)
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                backend = ExactSyntheticBackend()
                prior = backend.prior_record()
                fault = OneShotFault(boundary)
                transaction = controls.HostControlTransaction(
                    frozen_profile(), backend, fault_injector=fault,
                )
                with self.assertRaises(controls.TimingControlError):
                    transaction.apply()
                self.assertTrue(fault.fired)
                backend.assert_prior(self, prior)
                if transaction.journal_path is not None:
                    self.assertFalse(os.path.exists(transaction.journal_path))

    def test_every_observed_restore_boundary_retains_failure_and_can_reconcile(self):
        recorder = OneShotFault()
        clean = controls.HostControlTransaction(
            frozen_profile(), ExactSyntheticBackend(), fault_injector=recorder,
        )
        clean.apply()
        clean.restore()
        boundaries = tuple(dict.fromkeys(
            item for item in recorder.seen if item.startswith("restore:")
        ))
        self.assertGreaterEqual(len(boundaries), 10)
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                backend = ExactSyntheticBackend()
                prior = backend.prior_record()
                fault = OneShotFault()
                transaction = controls.HostControlTransaction(
                    frozen_profile(), backend, fault_injector=fault,
                )
                transaction.apply()
                journal = transaction.journal_path
                fault.target = boundary
                with self.assertRaises(controls.TimingControlIndeterminate):
                    transaction.restore()
                self.assertTrue(fault.fired)
                backend.assert_prior(self, prior)
                self.assertTrue(os.path.exists(journal))
                fault.target = None
                transaction.restore()
                self.assertEqual(transaction.state, "restored")
                self.assertTrue(transaction.had_abnormal_recovery)
                self.assertFalse(os.path.exists(journal))

    def test_post_effect_failures_for_every_mutation_class_restore_prior(self):
        failures = (
            ("module", "k10temp"), ("service", "irqbalance"),
            ("write", "/sys/devices/system/cpu/cpufreq/boost"),
            ("affinity", "process"), ("dma", "hold"),
        )
        for failure in failures:
            with self.subTest(failure=failure):
                backend = ExactSyntheticBackend()
                prior = backend.prior_record()
                backend.post_failure = failure
                transaction = controls.HostControlTransaction(frozen_profile(), backend)
                with self.assertRaises(controls.TimingControlError):
                    transaction.apply()
                backend.assert_prior(self, prior)
                self.assertEqual(transaction.state, "restored")

    def test_topology_mismatch_rejects_before_journal_or_mutation(self):
        backend = ExactSyntheticBackend()
        prior = backend.prior_record()
        backend.topology = ((0, 1), (2, 3))
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        with self.assertRaisesRegex(controls.TimingControlError, "SMT sibling"):
            transaction.apply()
        backend.assert_prior(self, prior)
        self.assertIsNone(transaction.journal_path)

    def test_semantically_equal_but_textually_different_control_rejects(self):
        self.assertEqual(
            controls.parse_control_value("00000008", "cpu_mask"), (3,),
        )
        self.assertEqual(
            controls.parse_control_value(
                "00000001,00000000", "cpu_mask",
            ),
            (32,),
        )
        for malformed in (
            "3", "00000000", "00000008,00000000,", "0000000G",
            "000000000", "0000000A",
        ):
            with self.subTest(malformed_cpu_mask=malformed):
                with self.assertRaises(controls.TimingControlError):
                    controls.parse_control_value(malformed, "cpu_mask")

        def add_cpu_list(value):
            value["controls"]["writes"].append({
                "order": 2, "path": "/sys/fs/cgroup/cpuset/cp2/cpuset.cpus",
                "desired_text": "0-3", "parser": "cpu_list", "restore_exact": True,
            })

        class NormalizingBackend(ExactSyntheticBackend):
            def __init__(self):
                super().__init__()
                self.values["/sys/fs/cgroup/cpuset/cp2/cpuset.cpus"] = "0-1"

            def write_text(self, path, value):
                self.values[path] = "0,1,2,3" if value == "0-3" else value

        backend = NormalizingBackend()
        transaction = controls.HostControlTransaction(frozen_profile(mutate=add_cpu_list), backend)
        with self.assertRaisesRegex(controls.TimingControlError, "exact requested text"):
            transaction.apply()
        self.assertEqual(backend.values["/sys/fs/cgroup/cpuset/cp2/cpuset.cpus"], "0-1")

    def test_symlinked_journal_parent_rejects_before_mutation(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            real = os.path.join(directory, "real")
            alias = os.path.join(directory, "alias")
            os.mkdir(real)
            os.symlink(real, alias)
            backend = ExactSyntheticBackend()
            prior = backend.prior_record()
            transaction = controls.HostControlTransaction(
                frozen_profile(journal_parent=alias), backend,
            )
            with self.assertRaises(OSError):
                transaction.apply()
            backend.assert_prior(self, prior)

    def test_journal_name_substitution_cannot_prevent_exact_host_restore(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            backend = ExactSyntheticBackend()
            prior = backend.prior_record()
            transaction = controls.HostControlTransaction(
                frozen_profile(journal_parent=directory), backend,
            )
            transaction.apply()
            journal = transaction.journal_path
            displaced = journal + ".displaced"
            os.rename(journal, displaced)
            with open(journal, "wb") as stream:
                stream.write(b"substitution\n")
            os.chmod(journal, 0o600)
            with self.assertRaises(controls.TimingControlIndeterminate):
                transaction.restore()
            backend.assert_prior(self, prior)
            transaction._journal.close()
            transaction._journal = None

    def test_live_journal_lock_rejects_then_manual_dead_owner_recovery_is_exact(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            backend = ExactSyntheticBackend()
            prior = backend.prior_record()
            profile = frozen_profile(journal_parent=directory)
            transaction = controls.HostControlTransaction(profile, backend)
            transaction.apply()
            journal = transaction.journal_path
            with self.assertRaisesRegex(controls.TimingControlError, "live transaction"):
                controls.recover_control_journal(profile, journal, backend)
            # Simulate the process-local effects disappearing with the owner;
            # only global controls remain for a separately started recovery.
            backend.dma = False
            backend.cpus = prior["cpus"]
            transaction._restore_signal_handlers()
            transaction._journal.close()
            transaction._journal = None
            result = controls.recover_control_journal(profile, journal, backend)
            backend.assert_prior(self, prior)
            self.assertTrue(result["record"]["passed"])
            with open(journal, "rb") as stream:
                self.assertIn(b"manual_recovery_verified", stream.read())

    def test_forked_guardian_restores_global_state_after_sigkill_equivalent_exit(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            state_path = os.path.join(directory, "state.json")
            backend = SharedFileBackend(state_path)
            expected = backend._load()
            profile = frozen_profile(journal_parent=directory)
            worker = os.fork()
            if worker == 0:  # pragma: no cover - parent observes the durable result
                try:
                    transaction = controls.HostControlTransaction(
                        profile, SharedFileBackend(state_path), enable_guardian=True,
                    )
                    transaction.apply()
                finally:
                    os._exit(0)
            os.waitpid(worker, 0)
            deadline = time.monotonic() + 5.0
            journal = None
            while time.monotonic() < deadline:
                candidates = [
                    os.path.join(directory, name) for name in os.listdir(directory)
                    if name.startswith("schurvio-cp2e-control-")
                ]
                if candidates and SharedFileBackend(state_path)._load() == expected:
                    with open(candidates[0], "rb") as stream:
                        if b"guardian_recovery_verified" in stream.read():
                            journal = candidates[0]
                            break
                time.sleep(0.01)
            self.assertIsNotNone(journal)
            self.assertEqual(SharedFileBackend(state_path)._load(), expected)


class CoverageAndPrivilegeTests(unittest.TestCase):
    def test_incomplete_profile_is_reported_without_claiming_population_completeness(self):
        report = controls.validate_control_coverage(frozen_profile())
        self.assertFalse(report["passed"])
        self.assertIn("cpuset", report["missing_categories"])
        self.assertIn("runtime_population_requires_raw_receipts", report["scope"])
        with self.assertRaises(controls.TimingControlError):
            controls.require_complete_control_coverage(frozen_profile())

    def test_all_authorized_surface_categories_can_be_structurally_bound(self):
        def complete(value):
            additions = (
                ("/sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq", "3000000", "integer"),
                ("/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq", "3000000", "integer"),
                ("/sys/devices/system/cpu/cpu1/online", "1", "integer"),
                ("/sys/devices/system/cpu/cpu0/cpuidle/state1/disable", "1", "integer"),
                ("/proc/irq/24/smp_affinity_list", "0-3", "cpu_list"),
                ("/sys/fs/cgroup/cpuset/cp2/cpuset.cpus", "0-3", "cpu_list"),
            )
            for path, text, parser in additions:
                value["controls"]["writes"].append({
                    "order": len(value["controls"]["writes"]), "path": path,
                    "desired_text": text, "parser": parser, "restore_exact": True,
                })
            command_environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
            for identifier in ("aperf", "mperf", "throttle"):
                value["observations"].append({
                    "observation_id": identifier,
                    "argv": ["/usr/bin/head", "-n", "1", "/proc/cpuinfo"],
                    "environment": command_environment, "cwd": "/tmp",
                    "timeout_ns": 1_000_000_000, "maximum_stdout_bytes": 4096,
                    "parser": "integer", "expected": 1,
                })
            value["observations"].sort(key=lambda item: item["observation_id"])

        report = controls.require_complete_control_coverage(frozen_profile(mutate=complete))
        self.assertTrue(report["passed"])
        self.assertEqual(report["missing_categories"], [])

    def test_sudo_command_authority_does_not_imply_root_descriptor_authority(self):
        backend = controls.HostBackend(("/usr/bin/sudo", "-n"))
        backend._run = lambda _argv, **_kwargs: b""
        with mock.patch("os.geteuid", return_value=1000):
            with self.assertRaisesRegex(controls.TimingControlError, "descriptor authority"):
                backend.require_privilege()

    def test_production_command_allowlist_is_exact_and_shell_free(self):
        backend = controls.HostBackend(("/usr/bin/sudo", "-n"))
        called = []
        backend._run = lambda argv, **_kwargs: called.append(tuple(argv)) or b""
        backend.command(("/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "--", "k10temp"))
        self.assertEqual(len(called), 1)
        for argv in (
            ("/usr/bin/sudo", "-n", "/bin/sh", "-c", "true"),
            ("/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "--", "nouveau"),
            ("/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "unrelated"),
        ):
            with self.assertRaises(controls.TimingControlError):
                backend.command(argv)


class RawTelemetryTests(unittest.TestCase):
    def snapshots(self, *, mutate_post=None):
        specifications = telemetry_specs()
        backend = ExactSyntheticBackend()
        backend.telemetry = {
            item.metric_id: telemetry_payload(item, phase=0) for item in specifications
        }
        pre = controls.capture_raw_telemetry(specifications, "pre", backend)
        backend.telemetry = {
            item.metric_id: telemetry_payload(item, phase=1) for item in specifications
        }
        if mutate_post is not None:
            mutate_post(backend.telemetry, specifications)
        post = controls.capture_raw_telemetry(specifications, "post", backend)
        return pre, post

    def test_raw_bytes_hashes_sources_and_exact_deltas_are_retained(self):
        pre, post = self.snapshots()
        result = controls.compare_raw_telemetry(
            pre, post, (0, 1), (3_000_000,), (0, 120_000),
            ((99, 100), (101, 100)),
        )
        self.assertTrue(result["record"]["passed"])
        self.assertEqual(result["record"]["per_cpu"][0]["aperf_delta"], 1_000)
        self.assertEqual(len(result["sha256"]), 64)
        receipt = pre.receipts[0].as_record()
        self.assertEqual(
            receipt["raw_sha256"],
            __import__("hashlib").sha256(bytes.fromhex(receipt["raw_hex"])).hexdigest(),
        )

    def test_raw_receipt_reparses_bytes_instead_of_trusting_value(self):
        specification = telemetry_specs((0,))[0]
        raw = telemetry_payload(specification, phase=0)
        with self.assertRaisesRegex(controls.TimingControlError, "differs"):
            controls.RawTelemetryReceipt(specification, "pre", 1, 2, raw, 999)

    def test_counter_frequency_temperature_and_throttle_fail_closed(self):
        cases = {
            "aperf": lambda values, specs: values.__setitem__(
                "cpu0.aperf", telemetry_payload(
                    next(item for item in specs if item.metric_id == "cpu0.aperf"),
                    phase=0,
                ),
            ),
            "frequency": lambda values, specs: values.__setitem__(
                "cpu0.frequency", b"2999999\n",
            ),
            "temperature": lambda values, specs: values.__setitem__(
                "package.temperature", b"120001\n",
            ),
            "throttle": lambda values, specs: values.__setitem__(
                "cpu0.throttle", b"8\n",
            ),
            "ratio": lambda values, specs: values.__setitem__(
                "cpu0.aperf", (12_000).to_bytes(8, "little"),
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                pre, post = self.snapshots(mutate_post=mutation)
                with self.assertRaises(controls.TimingControlError):
                    controls.compare_raw_telemetry(
                        pre, post, (0, 1), (3_000_000,), (0, 120_000),
                        ((99, 100), (101, 100)),
                    )

    def test_missing_substituted_or_duplicate_cpu_metric_population_rejects(self):
        pre, post = self.snapshots()
        with self.assertRaisesRegex(controls.TimingControlError, "omits"):
            controls.compare_raw_telemetry(
                pre, post, (0, 1, 2), (3_000_000,), (0, 120_000),
                ((99, 100), (101, 100)),
            )
        substituted = list(post.receipts)
        item = substituted[0]
        other_spec = controls.RawTelemetrySpec(
            item.specification.metric_id, item.specification.role,
            item.specification.cpu_id, item.specification.source_kind,
            item.specification.path, item.specification.register,
        )
        substituted[0] = controls.RawTelemetryReceipt(
            other_spec, "post", item.started_monotonic_ns,
            item.ended_monotonic_ns, item.raw, item.parsed_value,
        )
        # Equal specs are intentionally accepted; an actual path/register
        # substitution is rejected by RawTelemetrySpec before comparison.
        self.assertEqual(substituted[0].specification, item.specification)
        with self.assertRaises(controls.TimingControlError):
            controls.RawTelemetrySpec(
                "cpu0.aperf", "aperf", 0, "msr_u64_le", "/dev/cpu/1/msr",
                controls.MSR_APERF,
            )

    def test_overlapping_captures_and_extra_cpu_metrics_reject(self):
        pre, post = self.snapshots()
        boundary = max(item.ended_monotonic_ns for item in pre.receipts)
        overlapping = controls.RawTelemetrySnapshot("post", tuple(
            controls.RawTelemetryReceipt(
                item.specification, "post", boundary, boundary,
                item.raw, item.parsed_value,
            )
            for item in post.receipts
        ))
        with self.assertRaisesRegex(controls.TimingControlError, "overlap"):
            controls.compare_raw_telemetry(
                pre, overlapping, (0, 1), (3_000_000,), (0, 120_000),
                ((99, 100), (101, 100)),
            )

        specifications = telemetry_specs((0, 1, 2))
        backend = ExactSyntheticBackend()
        backend.telemetry = {
            item.metric_id: telemetry_payload(item, phase=0) for item in specifications
        }
        extra_pre = controls.capture_raw_telemetry(specifications, "pre", backend)
        backend.telemetry = {
            item.metric_id: telemetry_payload(item, phase=1) for item in specifications
        }
        extra_post = controls.capture_raw_telemetry(specifications, "post", backend)
        with self.assertRaisesRegex(controls.TimingControlError, "unexpected"):
            controls.compare_raw_telemetry(
                extra_pre, extra_post, (0, 1), (3_000_000,), (0, 120_000),
                ((99, 100), (101, 100)),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
