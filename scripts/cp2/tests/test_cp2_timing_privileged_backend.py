#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free tests for the profile-bound CP2-E privileged backend candidate."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cp2_timing_privileged_backend as backend  # noqa: E402
import cp2_timing_privileged_helper as protocol  # noqa: E402
import cp2_timing_profile as profile_codec  # noqa: E402
import cp2_timing_artifact as artifact  # noqa: E402
from test_cp2_timing_evidence import valid_profile_value  # noqa: E402


def canonical(value):
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def frozen_backend_profile():
    value = valid_profile_value()
    # This suite exercises the backend state machine rather than Python-host
    # scheduling latency; the formal candidate will freeze a measured bound.
    value["telemetry_plan"]["aperf_mperf"][
        "maximum_snapshot_skew_ns"
    ] = 1_000_000_000
    value["interrupts"]["inventory_sha256"] = hashlib.sha256(canonical({
        "domain": "SchurVIO-CP2-E-numeric-IRQ-population-v1",
        "irq_numbers": [24],
    })).hexdigest()
    value["privileged_helper"]["plan_sha256"] = (
        profile_codec.profile_plan_sha256(value)
    )
    initial = profile_codec.load_profile_bytes(
        profile_codec.canonical_profile_bytes(value)
    )
    controls = backend.derive_typed_controls(initial)
    writable = [item for item in controls if item.writable]
    value["controls"]["writes"] = [
        {
            "order": index, "path": item.path, "parser": item.parser,
            "desired_text": item.desired_text, "restore_exact": True,
        }
        for index, item in enumerate(writable)
    ]
    value["privileged_helper"]["plan_sha256"] = (
        profile_codec.profile_plan_sha256(value)
    )
    return profile_codec.load_profile_bytes(
        profile_codec.canonical_profile_bytes(value)
    )


class FakeRootSystem:
    """Complete in-memory Linux primitive model; performs no host access."""

    def __init__(self, profile, peer):
        self.profile = profile.value
        self.peer = peer
        self.root_checks = 0
        self.values = {}
        for item in backend.derive_typed_controls(profile):
            if item.control_id == "boost":
                text = "1"
            elif item.control_id.endswith(".driver"):
                text = self.profile["runtime"]["scaling_driver"]
            elif item.control_id.endswith(".governor"):
                text = "ondemand"
            elif item.control_id.endswith(".minimum_frequency_khz"):
                text = "3000000"
            elif item.control_id.endswith(".maximum_frequency_khz"):
                text = "4300000"
            elif item.control_id.endswith(".online"):
                text = "1"
            elif item.control_id.endswith(".name"):
                cpu, state = item.control_id.split(".")[1], item.control_id.split(".")[3]
                text = self.profile["cpu_controls"]["cpus"][int(cpu)]["idle_states"][int(state)]["name"]
            elif item.control_id.endswith(".disabled"):
                text = "0"
            elif item.control_id == "irq.default_affinity":
                text = "0000000f"
            elif item.control_id.startswith("irq."):
                text = "0-3"
            else:  # pragma: no cover - protects fixture completeness
                raise AssertionError(item.control_id)
            self.values[item.path] = text
        telemetry = self.profile["telemetry_plan"]
        for source in telemetry["frequency_sources"]:
            self.values[source["path"]] = str(source["expected_khz"])
        reference = telemetry["aperf_mperf"]["reference_frequency_source"]
        self.values[reference["path"]] = str(
            telemetry["aperf_mperf"]["reference_frequency_khz"]
        )
        temperature = telemetry["temperature_source"]
        self.values[temperature["name_path"]] = temperature["name_expected"]
        self.values[temperature["label_path"]] = temperature["label_expected"]
        self.values[temperature["input_path"]] = "55000"
        for source in telemetry["throttle_sources"]:
            self.values[source["path"]] = "7"
        self.modules = {"msr"}
        self.services = {"irqbalance"}
        self.helper_pid = peer.pid + 10_000
        self.sudo_pid = peer.pid + 9_000
        self.control_chain_pids = (self.sudo_pid, self.helper_pid)
        self.affinities = {
            0: (1,), peer.pid: (0, 1, 2, 3),
            self.sudo_pid: (1,), self.helper_pid: (1,),
        }
        self.descendant_pids = (peer.pid,)
        self.threads_by_pid = {
            peer.pid: (peer.pid,), self.sudo_pid: (self.sudo_pid,),
            self.helper_pid: (self.helper_pid,),
        }
        self.start_ticks = {
            peer.pid: 123456, self.sudo_pid: 223456,
            self.helper_pid: 323456,
        }
        self.memberships = {
            peer.pid: "/", self.sudo_pid: "/", self.helper_pid: "/",
        }
        self.process_parents = {
            self.sudo_pid: peer.pid, self.helper_pid: self.sudo_pid,
        }
        self.process_identity_overrides = {}
        self.held_processes = set()
        self.cpuset_exists = False
        self.cpuset_members_value = ()
        self.dma = False
        self.irqs = (24,)
        self.msr_values = {0xE8: 1000, 0xE7: 1000}
        self.command_history = []
        self.boundaries = []
        self.identity_overrides = {}
        self.foreign_rows = ({
            "effective_affinity_cpu_ids": [0],
            "process_start_time_ticks": 1,
            "tid": 13,
        }, {
            "effective_affinity_cpu_ids": [0],
            "process_start_time_ticks": 1,
            "tid": 15,
        })
        self.foreign_observations = []
        self.unstable_descendants = False
        self.descendant_calls = 0
        controlled = self.profile["cpu_plan"]["controlled_cpu_ids"]
        self.values["/sys/devices/system/cpu/possible"] = "0-3"
        self.values["/sys/devices/system/cpu/present"] = "0-3"
        self.values["/sys/devices/system/cpu/online"] = "0-3"
        for core in self.profile["cpu_plan"]["smt_cores"]:
            siblings = ",".join(str(item) for item in core["sibling_cpu_ids"])
            for cpu_id in core["sibling_cpu_ids"]:
                root = "/sys/devices/system/cpu/cpu{}/topology".format(cpu_id)
                self.values[root + "/core_id"] = str(core["physical_core_id"])
                self.values[root + "/thread_siblings_list"] = siblings
        if controlled != [0, 1, 2, 3]:
            raise AssertionError("fake topology is only valid for four CPUs")

    def require_root(self):
        self.root_checks += 1

    @staticmethod
    def monotonic_ns():
        return time.monotonic_ns()

    def read_raw(self, path, maximum=64 * 1024):
        value = self.values[path]
        payload = (value + "\n").encode("ascii")
        if len(payload) > maximum:
            raise backend.PrivilegedBackendError("fake read oversized")
        return payload

    def read_text(self, path, _parser):
        return self.values[path]

    def write_text(self, path, text):
        self.values[path] = text

    def cpuset_mountinfo_line(self, mount_path):
        if mount_path != self.profile["cpuset"]["mount_path"]:
            raise backend.PrivilegedBackendError("wrong fake cpuset mount")
        return "41 30 0:35 / {} rw - cgroup cgroup rw,cpuset".format(
            mount_path
        )

    def source_identity_sha256(self, domain, _paths, _structural):
        if domain in self.identity_overrides:
            return self.identity_overrides[domain]
        prefix = "SchurVIO-CP2-E-"
        suffix = "-source-identity-v1"
        if not domain.startswith(prefix) or not domain.endswith(suffix):
            raise backend.PrivilegedBackendError("unknown fake identity domain")
        name = domain[len(prefix):-len(suffix)]
        if name == "CPU-topology":
            return self.profile["cpu_plan"]["topology_source_sha256"]
        if name == "cpuset-hierarchy":
            return self.profile["cpuset"]["hierarchy_identity_sha256"]
        if name == "boost":
            return self.profile["cpu_controls"]["boost"][
                "source_identity_sha256"
            ]
        for policy in self.profile["cpu_controls"]["policies"]:
            if name == "cpufreq-" + policy["policy_id"]:
                return policy["source_identity_sha256"]
        for cpu in self.profile["cpu_controls"]["cpus"]:
            if name == "cpu{}-online".format(cpu["cpu_id"]):
                return cpu["online_source_identity_sha256"]
            if name == "cpu{}-idle-inventory".format(cpu["cpu_id"]):
                return cpu["idle_inventory_sha256"]
        interrupts = self.profile["interrupts"]
        if name == "IRQ-default-affinity":
            return interrupts["default_source_identity_sha256"]
        for irq in interrupts["irq_records"]:
            if name == "IRQ{}-affinity".format(irq["irq"]):
                return irq["source_identity_sha256"]
        telemetry = self.profile["telemetry_plan"]
        for source in telemetry["aperf_mperf"]["cpu_sources"]:
            if name == "cpu{}-APERF-MPERF".format(source["cpu_id"]):
                return source["source_identity_sha256"]
        reference = telemetry["aperf_mperf"]["reference_frequency_source"]
        if name == "MPERF-reference-frequency":
            return reference["source_identity_sha256"]
        for source in telemetry["frequency_sources"]:
            if name == "cpu{}-current-frequency".format(source["cpu_id"]):
                return source["source_identity_sha256"]
        if name == "k10temp-temperature":
            return telemetry["temperature_source"]["source_identity_sha256"]
        for source in telemetry["throttle_sources"]:
            if name == "cpu{}-AMD-throttle".format(source["cpu_id"]):
                return source["source_identity_sha256"]
        raise backend.PrivilegedBackendError("unknown fake identity domain")

    def module_loaded(self, name):
        return name in self.modules

    def service_active(self, name):
        return name in self.services

    def command(self, argv):
        command = tuple(argv)
        self.command_history.append(command)
        if command[0] == "/usr/sbin/modprobe":
            if "-r" in command:
                self.modules.remove(command[-1])
            else:
                self.modules.add(command[-1])
        elif command[1] == "stop":
            self.services.remove(command[-1])
        else:
            self.services.add(command[-1])

    def affinity(self, tid, _process_id=None):
        return self.affinities[tid]

    def set_affinity(self, tid, cpus, _process_id=None):
        self.affinities[tid] = tuple(cpus)

    def thread_ids(self, pid):
        return self.threads_by_pid.get(pid, (pid,))

    def start_time_ticks(self, tid, _process_id=None):
        return self.start_ticks.get(tid, 100000 + tid)

    def cpuset_membership(self, tid, _process_id=None):
        return self.memberships[tid]

    def current_process_id(self):
        return self.helper_pid

    def hold_process_identity(self, pid):
        if pid not in self.start_ticks:
            raise ProcessLookupError(pid)
        self.held_processes.add(pid)

    def close_process_identity_descriptors(self):
        self.held_processes.clear()

    def process_identity(self, pid):
        if pid in self.process_identity_overrides:
            return dict(self.process_identity_overrides[pid])
        if pid not in self.process_parents:
            raise ProcessLookupError(pid)
        self.hold_process_identity(pid)
        return {
            "executable_device": 7,
            "executable_inode": 1_000_000 + pid,
            "parent_pid": self.process_parents[pid],
            "pid": pid,
            "process_gid": 0,
            "process_uid": 0,
            "start_time_ticks": self.start_ticks[pid],
        }

    def path_exists(self, path):
        if path == self.profile["cpuset"]["group_path"]:
            return self.cpuset_exists
        return path in self.values

    def create_cpuset(self, path):
        if self.cpuset_exists or path != self.profile["cpuset"]["group_path"]:
            raise backend.PrivilegedBackendError("fake cpuset collision")
        self.cpuset_exists = True
        cpuset = self.profile["cpuset"]
        self.values[cpuset["cpus_path"]] = "0"
        self.values[cpuset["mems_path"]] = "0"
        self.values[cpuset["effective_cpus_path"]] = "0"
        self.values[cpuset["effective_mems_path"]] = "0"

    def cpuset_members(self, _path):
        return self.cpuset_members_value

    def move_to_cpuset(self, _path, tid, _process_id=None):
        self.memberships[tid] = "/" + self.profile["cpuset"]["group_name"]
        self.cpuset_members_value = tuple(sorted(set(
            self.cpuset_members_value + (tid,)
        )))

    def move_to_cpuset_path(
        self, mount_path, relative, tid, _process_id=None,
    ):
        if mount_path != self.profile["cpuset"]["mount_path"]:
            raise backend.PrivilegedBackendError("wrong fake cpuset mount")
        self.memberships[tid] = relative
        self.cpuset_members_value = tuple(
            item for item in self.cpuset_members_value if item != tid
        )

    def remove_cpuset(self, path):
        if path != self.profile["cpuset"]["group_path"]:
            raise backend.PrivilegedBackendError("wrong fake cpuset")
        if self.cpuset_members_value:
            raise backend.PrivilegedBackendError("fake cpuset still occupied")
        self.cpuset_exists = False
        for key in (
            "cpus_path", "mems_path", "effective_cpus_path", "effective_mems_path",
        ):
            self.values.pop(self.profile["cpuset"][key], None)

    def list_irqs(self):
        return self.irqs

    def open_dma_latency(self, _value):
        if self.dma:
            raise backend.PrivilegedBackendError("fake DMA already held")
        self.dma = True

    def dma_latency_held(self):
        return self.dma

    def close_dma_latency(self):
        self.dma = False

    def read_msr(self, _path, register):
        value = self.msr_values[register]
        self.msr_values[register] += 100
        return value.to_bytes(8, "little")

    def descendants(self, root):
        if root == self.control_chain_pids[0]:
            return tuple(sorted(self.control_chain_pids))
        if root != self.peer.pid:
            raise backend.PrivilegedBackendError("wrong fake descendant root")
        if self.unstable_descendants:
            self.descendant_calls += 1
            return (
                (root,) if self.descendant_calls % 2
                else tuple(sorted(set(self.descendant_pids + (999,))))
            )
        return tuple(sorted(set(
            self.descendant_pids + self.control_chain_pids
        )))

    def foreign_affinity_eligibility(self, _cpus, _allowed):
        if self.foreign_observations:
            return self.foreign_observations.pop(0)
        return self.foreign_rows

    @staticmethod
    def wait_until(deadline, stop_event):
        while not stop_event.is_set():
            remaining = deadline - time.monotonic_ns()
            if remaining <= 0:
                return
            stop_event.wait(remaining / 1_000_000_000)


def fixture():
    profile = frozen_backend_profile()
    peer = protocol.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
    system = FakeRootSystem(profile, peer)
    candidate = backend.PrivilegedTimingBackend(profile, peer, system)
    return profile, peer, system, candidate


class PrivilegedBackendTests(unittest.TestCase):
    def test_held_proc_identity_rejects_exit_and_numeric_reopen(self):
        read_fd, write_fd = os.pipe()
        child = os.fork()
        if child == 0:  # pragma: no cover - asserted by the parent process
            os.close(write_fd)
            try:
                os.read(read_fd, 1)
            finally:
                os._exit(0)
        os.close(read_fd)
        system = backend.LinuxRootSystem()
        try:
            retained = system.process_identity(child)
            self.assertEqual(retained["pid"], child)
            held_descriptor = system._proc_descriptors[child][0]
            os.close(write_fd)
            waited, status = os.waitpid(child, 0)
            self.assertEqual(waited, child)
            self.assertTrue(os.WIFEXITED(status))
            os.fstat(held_descriptor)
            with self.assertRaises(OSError):
                system.process_identity(child)
        finally:
            try:
                os.close(write_fd)
            except OSError:
                pass
            try:
                os.waitpid(child, 0)
            except ChildProcessError:
                pass
            system.close_process_identity_descriptors()

    def test_partial_legacy_projection_is_rejected_before_root_access(self):
        profile = profile_codec.load_profile_bytes(
            profile_codec.canonical_profile_bytes(valid_profile_value())
        )
        peer = protocol.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        system = FakeRootSystem(profile, peer)
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "complete ordered typed-v2",
        ):
            backend.PrivilegedTimingBackend(profile, peer, system)
        self.assertEqual(system.root_checks, 0)

        bound_profile = frozen_backend_profile()
        bound_system = FakeRootSystem(bound_profile, peer)
        bound_system.identity_overrides[
            "SchurVIO-CP2-E-boost-source-identity-v1"
        ] = "f" * 64
        bound_candidate = backend.PrivilegedTimingBackend(
            bound_profile, peer, bound_system
        )
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "source identities",
        ):
            bound_candidate.capture_prior()
        self.assertEqual(bound_system.root_checks, 1)
        self.assertEqual(bound_system.command_history, [])

    def test_complete_capture_apply_validate_restore_is_byte_exact(self):
        _profile, _peer, system, candidate = fixture()
        prior = candidate.capture_prior()
        prior_bytes = canonical(prior)
        boundaries = []
        candidate.apply(prior, boundaries.append)
        applied = candidate.validate_applied()
        self.assertTrue(applied["privileged_v2"]["dma_latency_held_by_helper"])
        self.assertTrue(applied["privileged_v2"]["cpuset"]["exists"])
        self.assertEqual(applied["process_affinity"], [0])
        default_irq = next(
            item for item in applied["values"]
            if item["control_id"] == "irq.default_affinity"
        )
        self.assertEqual(default_irq["parser"], "cpu_mask")
        self.assertEqual(default_irq["desired_text"], "00000008")
        self.assertEqual(default_irq["text"], "00000008")
        feasibility = candidate.validate_data_free_feasibility()
        self.assertEqual(feasibility["foreign_affinity_eligibility_count"], 2)
        self.assertTrue(feasibility["formal_execution_locked"])
        candidate.restore(prior, boundaries.append)
        restored = candidate.validate_restored(prior)
        self.assertEqual(canonical(restored), prior_bytes)
        self.assertFalse(system.cpuset_exists)
        self.assertFalse(system.dma)
        self.assertEqual(system.modules, {"msr"})
        self.assertEqual(system.services, {"irqbalance"})
        self.assertTrue(any(item.startswith("before_") for item in boundaries))

    def test_fresh_backend_can_recover_only_from_complete_durable_prior(self):
        profile, peer, system, first = fixture()
        prior = first.capture_prior()
        first.apply(prior, lambda _label: None)
        replacement = backend.PrivilegedTimingBackend(profile, peer, system)
        replacement.restore(prior, lambda _label: None)
        self.assertEqual(replacement.validate_restored(prior), prior)

        altered = copy.deepcopy(prior)
        altered["privileged_v2"]["descendant_threads"][0]["start_time_ticks"] += 1
        with self.assertRaises(backend.PrivilegedBackendError):
            replacement.restore(altered, lambda _label: None)

    def test_irq_population_change_rejects_applied_and_restored_state(self):
        _profile, _peer, system, candidate = fixture()
        prior = candidate.capture_prior()
        candidate.apply(prior, lambda _label: None)
        system.irqs = (24, 25)
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "IRQ population",
        ):
            candidate.validate_applied()
        system.irqs = (24,)
        system.identity_overrides[
            "SchurVIO-CP2-E-boost-source-identity-v1"
        ] = "e" * 64
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "source identities",
        ):
            candidate.validate_applied()
        system.identity_overrides.clear()
        system.identity_overrides[
            "SchurVIO-CP2-E-k10temp-temperature-source-identity-v1"
        ] = "d" * 64
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "source identities",
        ):
            candidate.validate_applied()

    def test_guardian_stream_and_typed_observations_are_profile_bound(self):
        profile, peer, system, candidate = fixture()
        prior = candidate.capture_prior()
        candidate.apply(prior, lambda _label: None)
        contract = protocol.GuardianEvidenceContract(
            profile.value["guardians"]["poll_interval_ns"],
            profile.value["guardians"]["maximum_observation_gap_ns"],
            profile.value["guardians"]["maximum_campaign_duration_ns"],
            profile.value["guardians"]["maximum_observation_count"],
            profile.value["limits"]["maximum_guardian_line_bytes"],
            profile.value["limits"]["maximum_guardian_evidence_bytes"],
            tuple(profile.value["guardians"]["surfaces"]),
        )
        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = os.path.join(directory, "guardian.jsonl")
            descriptor = os.open(
                path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
                | os.O_CLOEXEC, 0o600,
            )
            writer = protocol.GuardianEvidenceWriter(
                contract, descriptor, expected_uid=os.getuid(),
                retain_bytes_after_close=True,
            )
            writer.bind_identity(peer, "9" * 64)
            candidate.start_population_guard(peer, "9" * 64, writer.append)
            # The synchronous first observation is the invariant under test;
            # stop before a scheduler-dependent second synthetic cadence slot.
            time.sleep(0.001)
            population = candidate.validate_population("run_0_pre")
            receipt = candidate.observe("pre", 0)
            seal = writer.seal(candidate.stop_population_guard())
            self.assertGreaterEqual(population["samples"], 1)
            self.assertEqual(receipt["phase"], "pre")
            self.assertEqual(
                [item["source_id"] for item in receipt["sources"]],
                sorted(item["source_id"] for item in receipt["sources"]),
            )
            verified = protocol.verify_guardian_evidence_bytes(
                writer.payload, seal, peer, "9" * 64, contract,
            )
            self.assertEqual(len(verified.records), writer.count)
            self.assertEqual(
                len(verified.records[0]["foreign_affinity_eligibility"]), 2,
            )
            writer.close()
        candidate.restore(prior, lambda _label: None)
        candidate.validate_restored(prior)

        # Population changes and nonempty kernel-like rows are observations,
        # not timing/control gates.
        _profile, _peer, changing_system, changing_candidate = fixture()
        changing_prior = changing_candidate.capture_prior()
        changing_candidate.apply(changing_prior, lambda _label: None)
        changing_system.foreign_observations = [
            (changing_system.foreign_rows[0],),
            changing_system.foreign_rows,
        ]
        changing = changing_candidate.validate_data_free_feasibility()
        self.assertEqual(changing["foreign_affinity_eligibility_count"], 2)
        self.assertNotEqual(
            changing["foreign_affinity_eligibility_before_sha256"],
            changing["foreign_affinity_eligibility_after_sha256"],
        )
        changing_candidate.restore(changing_prior, lambda _label: None)
        changing_candidate.validate_restored(changing_prior)

    def test_forked_multithread_descendant_inherits_and_matches_complete_cpuset(self):
        _profile, peer, system, candidate = fixture()
        prior = candidate.capture_prior()
        child = peer.pid + 100
        child_thread = child + 1
        # Simulate a fork plus a second thread after durable-prior capture but
        # before the child cpuset is created.  The apply path must sweep the
        # complete stabilized closure, not just the originally bound peer.
        system.descendant_pids = tuple(sorted((peer.pid, child)))
        system.threads_by_pid[child] = (child, child_thread)
        for tid in (child, child_thread):
            system.affinities[tid] = (0, 1, 2, 3)
            system.start_ticks[tid] = 200000 + tid
            system.memberships[tid] = "/"
        candidate.apply(prior, lambda _label: None)

        applied = candidate.validate_applied()
        extension = applied["privileged_v2"]
        self.assertEqual(extension["descendant_process_ids"], sorted((peer.pid, child)))
        self.assertEqual(
            [row["tid"] for row in extension["descendant_threads"]],
            sorted((peer.pid, child, child_thread)),
        )
        witness = candidate._validate_scheduler_population()
        self.assertEqual(
            witness["complete_descendant_population"],
            list(sorted((peer.pid, child))),
        )
        self.assertEqual(
            witness["complete_descendant_tid_population"],
            list(sorted((peer.pid, child, child_thread))),
        )
        self.assertEqual(
            system.cpuset_members_value,
            tuple(sorted((peer.pid, child, child_thread))),
        )

        system.descendant_pids = (peer.pid,)
        system.threads_by_pid.pop(child)
        for tid in (child, child_thread):
            system.affinities.pop(tid)
            system.start_ticks.pop(tid)
            system.memberships.pop(tid)
        system.cpuset_members_value = (peer.pid,)
        candidate.restore(prior, lambda _label: None)
        candidate.validate_restored(prior)

    def test_descendant_race_is_indeterminate_and_foreign_substitution_rejects(self):
        _profile, _peer, system, candidate = fixture()
        prior = candidate.capture_prior()
        candidate.apply(prior, lambda _label: None)
        system.unstable_descendants = True
        with self.assertRaisesRegex(
            backend.PrivilegedBackendIndeterminate, "did not stabilize",
        ):
            candidate.validate_applied()
        system.unstable_descendants = False
        system.foreign_rows = ({
            "effective_affinity_cpu_ids": [1],
            "process_start_time_ticks": 1,
            "tid": 13,
        },)
        with self.assertRaisesRegex(
            backend.PrivilegedBackendError, "identity/order",
        ):
            candidate.validate_data_free_feasibility()
        system.foreign_rows = ()
        candidate.restore(prior, lambda _label: None)
        candidate.validate_restored(prior)

    def test_apply_fault_at_every_journal_boundary_restores_exact_prior(self):
        _profile, _peer, _system, probe = fixture()
        probe_prior = probe.capture_prior()
        labels = []
        probe.apply(probe_prior, labels.append)
        probe.restore(probe_prior, lambda _label: None)
        self.assertLess(
            labels.index("after_cpuset"),
            labels.index("before_control_cpu.2.online"),
        )
        self.assertLess(
            labels.index("after_helper_affinity"),
            labels.index("before_control_boost"),
        )
        for failed_label in labels:
            with self.subTest(boundary=failed_label):
                _profile, _peer, _system, candidate = fixture()
                prior = candidate.capture_prior()

                def boundary(label):
                    if label == failed_label:
                        raise RuntimeError("synthetic apply journal fault")

                with self.assertRaises(RuntimeError):
                    candidate.apply(prior, boundary)
                candidate.restore(prior, lambda _label: None)
                self.assertEqual(candidate.validate_restored(prior), prior)

    def test_restore_boundary_faults_are_indeterminate_but_state_is_exact(self):
        _profile, _peer, _system, probe = fixture()
        prior = probe.capture_prior()
        probe.apply(prior, lambda _label: None)
        labels = []
        probe.restore(prior, labels.append)
        for failed_label in labels:
            with self.subTest(boundary=failed_label):
                _profile, _peer, _system, candidate = fixture()
                prior = candidate.capture_prior()
                candidate.apply(prior, lambda _label: None)

                def boundary(label):
                    if label == failed_label:
                        raise RuntimeError("synthetic restore journal fault")

                with self.assertRaises(backend.PrivilegedBackendIndeterminate):
                    candidate.restore(prior, boundary)
                self.assertEqual(candidate.validate_restored(prior), prior)

    def test_transient_restore_operation_faults_are_recoverable_from_prior(self):
        method_names = (
            "close_dma_latency", "move_to_cpuset_path", "remove_cpuset",
            "write_text", "set_affinity", "command",
        )
        for method_name in method_names:
            with self.subTest(operation=method_name):
                profile, peer, system, candidate = fixture()
                prior = candidate.capture_prior()
                candidate.apply(prior, lambda _label: None)
                original = getattr(system, method_name)
                calls = {"count": 0}

                def fail_once(*args, _original=original, **kwargs):
                    calls["count"] += 1
                    if calls["count"] == 1:
                        raise RuntimeError("synthetic one-shot operation fault")
                    return _original(*args, **kwargs)

                setattr(system, method_name, fail_once)
                with self.assertRaises(backend.PrivilegedBackendIndeterminate):
                    candidate.restore(prior, lambda _label: None)
                setattr(system, method_name, original)
                replacement = backend.PrivilegedTimingBackend(
                    profile, peer, system,
                )
                replacement.restore(prior, lambda _label: None)
                self.assertEqual(replacement.validate_restored(prior), prior)

    def test_malformed_durable_prior_is_rejected_before_recovery_mutation(self):
        mutations = {
            "profile": lambda value: value["privileged_v2"].__setitem__(
                "profile_sha256", "0" * 64,
            ),
            "path": lambda value: value["values"][0].__setitem__(
                "path", "/tmp/substituted",
            ),
            "membership": lambda value: value["privileged_v2"][
                "descendant_threads"
            ][0].__setitem__("cpuset_membership", "/../escape"),
            "irq": lambda value: value["privileged_v2"].__setitem__(
                "irq_population_sha256", "0" * 64,
            ),
            "helper_affinity": lambda value: value["privileged_v2"].__setitem__(
                "helper_affinity_cpu_ids", [1, 1],
            ),
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name):
                profile, peer, system, candidate = fixture()
                prior = candidate.capture_prior()
                candidate.apply(prior, lambda _label: None)
                malformed = copy.deepcopy(prior)
                mutation(malformed)
                before = copy.deepcopy(system.values)
                replacement = backend.PrivilegedTimingBackend(
                    profile, peer, system,
                )
                with self.assertRaises(backend.PrivilegedBackendError):
                    replacement.restore(malformed, lambda _label: None)
                self.assertEqual(system.values, before)
                replacement.restore(prior, lambda _label: None)
                replacement.validate_restored(prior)

    def test_detached_artifact_schema_accepts_exact_backend_v2_states(self):
        profile, _peer, _system, candidate = fixture()
        prior = candidate.capture_prior()
        candidate.apply(prior, lambda _label: None)
        applied = candidate.validate_applied()
        self.assertEqual(
            artifact._validate_v2_control_state(
                prior, profile, "backend prior", applied=False,
            ), prior,
        )
        self.assertEqual(
            artifact._validate_v2_control_state(
                applied, profile, "backend applied", applied=True,
            ), applied,
        )
        candidate.restore(prior, lambda _label: None)
        candidate.validate_restored(prior)


if __name__ == "__main__":
    unittest.main(verbosity=2)
