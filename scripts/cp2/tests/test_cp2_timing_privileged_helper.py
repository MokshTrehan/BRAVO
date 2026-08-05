#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic protecting tests for the CP2-E privileged helper protocol."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_timing_privileged_helper as helper  # noqa: E402


class InjectedFault(helper.PrivilegedHelperError):
    pass


class FaultOnce:
    def __init__(self, target=None):
        self.target = target
        self.seen = []
        self.fired = False

    def __call__(self, label):
        self.seen.append(label)
        if label == self.target and not self.fired:
            self.fired = True
            raise InjectedFault("injected boundary " + label)


class DeathOnce(FaultOnce):
    def __call__(self, label):
        self.seen.append(label)
        if label == self.target and not self.fired:
            self.fired = True
            raise helper.PeerClosed("synthetic peer death at " + label)


def frozen_binding(character="a"):
    digest = lambda label: hashlib.sha256(  # noqa: E731 - compact frozen fixture
        ("unmistakably-synthetic-binding-" + character + "-" + label).encode("ascii")
    ).hexdigest()
    return helper.Binding(
        profile_sha256=digest("profile"), plan_sha256=digest("plan"),
        source_sha256=digest("source"),
        protocol_core_sha256=digest("protocol-core"),
        root_launcher_sha256=digest("root-launcher"),
        import_closure_sha256=digest("import-closure"),
        sudoers_sha256=digest("sudoers"),
    )


def frozen_run_slots():
    pair_order = (("nullspace", "schur"), ("schur", "nullspace"),
                  ("nullspace", "schur"))
    return tuple(
        helper.FrozenRunSlot(index, index // 2, index % 2,
                             pair_order[index // 2][index % 2])
        for index in range(helper.FORMAL_RUN_COUNT)
    )


def frozen_guardian_contract():
    poll = 10
    duration = 1_000_000
    return helper.GuardianEvidenceContract(
        poll_interval_ns=poll,
        maximum_observation_gap_ns=50,
        maximum_campaign_duration_ns=duration,
        maximum_observation_count=duration // poll + 1,
        maximum_line_bytes=8192,
        maximum_evidence_bytes=1024 * 1024 * 1024,
        required_surfaces=(
            "boost", "cpu_idle", "cpu_online", "cpufreq", "cpuset_membership",
            "descendant_affinity", "foreign_affinity_eligibility",
            "interrupt_affinity", "interrupt_population", "irqbalance",
            "temperature", "throttle_counter",
        ),
    )


def transcript_fixture(harness):
    events = [
        dict(helper.strict_json_bytes(line))
        for line in harness.session.transcript_bytes.splitlines(keepends=True)
    ]
    terminal = dict(helper.strict_json_bytes(harness.session.terminal_receipt_bytes))
    return events, terminal


def reseal_transcript(events, terminal, *, preserve_event_indices=False):
    lines = []
    previous = "0" * 64
    for index, source in enumerate(events):
        value = copy.deepcopy(source)
        if not preserve_event_indices:
            value["event_index"] = index
        value["previous_record_sha256"] = previous
        encoded = helper.canonical_json_bytes(value)
        lines.append(encoded)
        previous = hashlib.sha256(encoded).hexdigest()
    transcript = b"".join(lines)
    receipt = copy.deepcopy(terminal)
    receipt["event_count"] = len(lines)
    receipt["first_record_sha256"] = (
        None if not lines else hashlib.sha256(lines[0]).hexdigest()
    )
    receipt["last_record_sha256"] = None if not lines else previous
    receipt["transcript_sha256"] = hashlib.sha256(transcript).hexdigest()
    receipt["transcript_size_bytes"] = len(transcript)
    return transcript, helper.canonical_json_bytes(receipt)


def guardian_fixture(harness):
    return [
        dict(helper.strict_json_bytes(line))
        for line in harness.session.guardian_evidence_bytes.splitlines(keepends=True)
    ]


def reseal_guardian(records, seal):
    lines = []
    previous = "0" * 64
    for index, source in enumerate(records):
        row = copy.deepcopy(source)
        row["sequence"] = index
        row["previous_record_sha256"] = previous
        encoded = helper.canonical_json_bytes(row)
        lines.append(encoded)
        previous = hashlib.sha256(encoded).hexdigest()
    evidence = b"".join(lines)
    value = copy.deepcopy(seal)
    value["observation_count"] = len(lines)
    value["evidence_size_bytes"] = len(evidence)
    value["evidence_sha256"] = hashlib.sha256(evidence).hexdigest()
    value["first_record_sha256"] = hashlib.sha256(lines[0]).hexdigest()
    value["last_record_sha256"] = previous
    return evidence, value


class MemoryJournal:
    def __init__(self, store, binding, peer, nonce_sha256, prior):
        self.store = store
        self.binding = binding
        self.peer = peer
        self.nonce_sha256 = nonce_sha256
        self.prior = copy.deepcopy(prior)
        self.identifier = os.urandom(16).hex()
        self.events = []
        self.previous_sha256 = "0" * 64
        self.closed = False
        self.append("prior_state_captured", {"prior": copy.deepcopy(prior)})

    def append(self, event, payload):
        if self.closed:
            raise helper.PrivilegedHelperError("memory journal is closed")
        self.events.append((event, copy.deepcopy(payload)))
        record = {
            "event": event, "payload": copy.deepcopy(payload),
            "previous_sha256": self.previous_sha256,
            "sequence": len(self.events) - 1,
        }
        self.previous_sha256 = hashlib.sha256(
            helper._bounded_canonical_json_bytes(
                record, helper.MAX_JOURNAL_BYTES,
                "synthetic memory journal record",
            )
        ).hexdigest()

    def complete(self):
        if self.closed:
            raise helper.PrivilegedHelperError("memory journal is closed")
        self.events.append(("journal_completed", None))
        self.closed = True
        self.store.active.remove(self)
        self.store.completed.append(self)

    def close(self):
        self.closed = True


class MemoryJournalStore:
    def __init__(self):
        self.active = []
        self.completed = []

    def create(self, binding, peer, nonce_sha256, prior):
        if self.active:
            raise helper.RecoveryIndeterminate("synthetic pending journal exists")
        journal = MemoryJournal(self, binding, peer, nonce_sha256, prior)
        self.active.append(journal)
        return journal

    def pending(self):
        return tuple(self.active)


class SyntheticPrivilegedBackend:
    """Exact in-memory model; it never reads or writes a host surface."""

    def __init__(self):
        self.state = {
            "cpuset": {"exists": False, "members": [], "cpus": None, "mems": None},
            "dma_latency_held": False,
            "modules": ["msr"],
            "service_irqbalance": True,
            "text": {
                "/sys/devices/system/cpu/cpufreq/boost": "1",
                "/sys/devices/system/cpu/cpufreq/policy2/scaling_governor": "ondemand",
            },
        }
        self.original = copy.deepcopy(self.state)
        self.population_active = False
        self.population_peer = None
        self.population_samples = 0
        self.population_session_id = None
        self.guardian_append = None
        self.population_observations = []
        self.population_clock_ns = 1_000_000
        self.population_stop_calls = 0
        self.population_stop_failure = False
        self.data_free_feasibility_calls = 0
        self.guardian_surfaces = [
            "boost", "cpu_idle", "cpu_online", "cpufreq", "cpuset_membership",
            "descendant_affinity", "foreign_affinity_eligibility",
            "interrupt_affinity", "interrupt_population", "irqbalance",
            "temperature", "throttle_counter",
        ]
        self.population_seal = None
        self.population_drift = False
        self.foreign_affinity_rows = [{
            "effective_affinity_cpu_ids": [2],
            "process_start_time_ticks": 1,
            "tid": 13,
        }, {
            "effective_affinity_cpu_ids": [2],
            "process_start_time_ticks": 1,
            "tid": 15,
        }]
        self.persistent_restore_failure = False
        self.close_failure = False
        self.closed = False

    def capture_prior(self):
        return copy.deepcopy(self.state)

    @staticmethod
    def _step(boundary, label, operation):
        boundary("before_" + label)
        operation()
        boundary("after_" + label)

    def apply(self, _prior, boundary):
        self._step(boundary, "module", lambda: self.state.__setitem__(
            "modules", ["k10temp", "msr"],
        ))
        self._step(boundary, "service", lambda: self.state.__setitem__(
            "service_irqbalance", False,
        ))
        self._step(boundary, "text_control", lambda: self.state["text"].__setitem__(
            "/sys/devices/system/cpu/cpufreq/boost", "0",
        ))

        def make_cpuset():
            self.state["cpuset"] = {
                "exists": True, "members": [], "cpus": "2", "mems": "0",
            }

        self._step(boundary, "cpuset_create_configure", make_cpuset)
        self._step(boundary, "dma_latency", lambda: self.state.__setitem__(
            "dma_latency_held", True,
        ))

    def validate_applied(self):
        if (
            self.state["modules"] != ["k10temp", "msr"]
            or self.state["service_irqbalance"] is not False
            or self.state["text"]["/sys/devices/system/cpu/cpufreq/boost"] != "0"
            or self.state["cpuset"] != {
                "exists": True, "members": [], "cpus": "2", "mems": "0",
            }
            or self.state["dma_latency_held"] is not True
        ):
            raise helper.PrivilegedHelperError("synthetic applied state differs")
        return copy.deepcopy(self.state)

    def validate_data_free_feasibility(self):
        self.validate_applied()
        self.data_free_feasibility_calls += 1
        return {
            "checkpoint": "CP2-E", "formal_execution_locked": True,
            "record_type": "unmistakably_synthetic_data_free_probe",
            "schema_version": helper.SCHEMA_VERSION,
        }

    def prepare_external_recovery(self, prior, journal_peer):
        if prior != self.original:
            raise helper.RecoveryIndeterminate(
                "synthetic recovery prior differs"
            )
        return {
            "journal_peer": journal_peer.as_record(),
            "replacement_control_plane_processes": [{
                "executable_device": 1, "executable_inode": 2,
                "parent_pid": journal_peer.pid, "pid": journal_peer.pid + 1,
                "process_gid": 0, "process_uid": 0,
                "start_time_ticks": 3,
            }],
            "replacement_peer": journal_peer.as_record(),
        }

    def validate_external_recovery(self, prior):
        if self.state != prior:
            raise helper.RecoveryIndeterminate(
                "synthetic external recovery state differs"
            )
        persistent = copy.deepcopy(self.state)
        return {
            "persistent_state": persistent,
            "persistent_state_sha256": hashlib.sha256(
                helper.canonical_control_state_bytes(persistent)
            ).hexdigest(),
            "prior_state_sha256": hashlib.sha256(
                helper.canonical_control_state_bytes(prior)
            ).hexdigest(),
            "record_type": "cp2e_external_recovery_validation",
            "schema_version": helper.SCHEMA_VERSION,
        }

    def start_population_guard(self, peer, session_id, append_observation):
        if self.population_active:
            raise helper.PrivilegedHelperError("population guard already active")
        self.population_active = True
        self.population_peer = peer
        self.population_session_id = session_id
        self.guardian_append = append_observation
        self.population_samples = 1
        self.population_drift = False
        self.population_observations = []
        self.population_clock_ns = 1_000_000
        self.population_seal = None
        self.state["cpuset"]["members"] = [peer.pid]
        return {
            "bound_gid": peer.gid, "bound_pid": peer.pid, "bound_uid": peer.uid,
            "cpuset": "synthetic-cp2e", "session_id": session_id,
        }

    def _guardian_observation(self):
        started = self.population_clock_ns
        ended = started + 2
        sequence = len(self.population_observations)
        surface_states = []
        for surface_id in self.guardian_surfaces:
            material = {
                "sequence": sequence, "state": self.state,
                "surface_id": surface_id,
            }
            surface_states.append({
                "passed": True, "state_sha256": hashlib.sha256(
                    helper.canonical_control_state_bytes(material)
                ).hexdigest(), "surface_id": surface_id,
            })
        throttle_raw = b"7\n"
        throttle_specification = helper.source_specification_record(
            "cpu2.amd_throttle", "amd_throttle", "text_u64",
            "/sys/devices/platform/unmistakably-synthetic-amd/cpu2/throttle",
            "canonical_ascii_decimal_u64_one_line", 2, None, {
                "counter_width_bits": 64, "cpu_id": 2,
                "no_wrap_policy": "reject_decrease_or_wrap",
                "path": "/sys/devices/platform/unmistakably-synthetic-amd/cpu2/throttle",
                "provider": "amd", "register": None,
                "semantics": "monotonic_thermal_throttle_event_counter",
                "source_identity_sha256": "1" * 64, "source_kind": "text_u64",
                "specification_sha256": "2" * 64,
                "value_extraction": "canonical_ascii_decimal_u64_one_line",
            },
        )
        temperature_raw = b"55000\n"
        temperature_specification = helper.source_specification_record(
            "shared.temperature", "temperature", "k10temp_text_integer",
            "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_input",
            "canonical_ascii_decimal_i64_one_line", None, None, {
                "device_path": "/sys/devices/platform/unmistakably-synthetic-k10temp",
                "input_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_input",
                "label_expected": "Tctl",
                "label_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_label",
                "maximum_millicelsius": 120000, "minimum_millicelsius": -40000,
                "module": "k10temp", "name_expected": "k10temp",
                "name_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/name",
                "sampling_policy": "continuous_helper_read_bound_sensor",
                "source_identity_sha256": "6" * 64,
            },
        )
        typed_telemetry = [{
            "cpu_id": 2, "extraction": "canonical_ascii_decimal_u64_one_line",
            "measurement_kind": "amd_throttle", "parsed_value": 7,
            "raw_hex": throttle_raw.hex(),
            "raw_sha256": hashlib.sha256(throttle_raw).hexdigest(),
            "read_ended_monotonic_ns": ended,
            "read_started_monotonic_ns": started, "register": None,
            "source_id": "cpu2.amd_throttle", "source_kind": "text_u64",
            "source_path": "/sys/devices/platform/unmistakably-synthetic-amd/cpu2/throttle",
            "source_spec_sha256": helper.source_specification_sha256(
                throttle_specification
            ),
        }, {
            "cpu_id": None, "extraction": "canonical_ascii_decimal_i64_one_line",
            "measurement_kind": "temperature", "parsed_value": 55000,
            "raw_hex": temperature_raw.hex(),
            "raw_sha256": hashlib.sha256(temperature_raw).hexdigest(),
            "read_ended_monotonic_ns": ended,
            "read_started_monotonic_ns": started, "register": None,
            "source_id": "shared.temperature",
            "source_kind": "k10temp_text_integer",
            "source_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_input",
            "source_spec_sha256": helper.source_specification_sha256(
                temperature_specification
            ),
        }]
        control_pid = self.population_peer.pid + 10_000
        witness = {
            "child_cpuset_members": [self.population_peer.pid],
            "complete_control_plane_processes": [{
                "executable_device": 7,
                "executable_inode": 1_000_000 + control_pid,
                "parent_pid": self.population_peer.pid,
                "pid": control_pid, "process_gid": 0, "process_uid": 0,
                "start_time_ticks": 20_000,
            }],
            "complete_control_plane_threads": [{
                "affinity_cpu_ids": [1], "cpuset_membership": "/",
                "process_id": control_pid, "start_time_ticks": 20_000,
                "tid": control_pid,
            }],
            "complete_descendant_population": [self.population_peer.pid],
            "complete_descendant_threads": [{
                "affinity_cpu_ids": [2],
                "cpuset_membership": "/synthetic-cp2e",
                "process_id": self.population_peer.pid,
                "start_time_ticks": 10_000,
                "tid": self.population_peer.pid,
            }],
            "complete_descendant_tid_population": [self.population_peer.pid],
            "foreign_affinity_eligibility": copy.deepcopy(
                self.foreign_affinity_rows[:1]
                if sequence % 2 else self.foreign_affinity_rows
            ),
        }
        row = {
            **witness,
            "drift": False, "ended_monotonic_ns": ended,
            # Alternate between nonempty kernel-like populations to prove that
            # population changes are retained but never gate the campaign.
            "irq_population_sha256": hashlib.sha256(
                b"synthetic-irq-population\n"
            ).hexdigest(),
            "scheduler_witness_sha256": hashlib.sha256(
                helper._bounded_canonical_json_bytes(
                    witness, helper.MAX_GUARDIAN_LINE_BYTES,
                    "synthetic scheduler witness",
                )
            ).hexdigest(),
            "started_monotonic_ns": started,
            "surface_states": surface_states,
            "thermal_or_throttle_event": False,
            "typed_telemetry": typed_telemetry,
        }
        self.population_observations.append(row)
        self.population_clock_ns = ended + 10
        self.guardian_append(copy.deepcopy(row))
        return row

    def validate_population(self, phase):
        if not self.population_active or self.population_peer is None:
            raise helper.PrivilegedHelperError("population guard is inactive")
        self.population_samples += 1
        if self.population_drift:
            raise helper.PrivilegedHelperError("synthetic ROS descendant escaped cpuset")
        if self.state["cpuset"]["members"] != [self.population_peer.pid]:
            raise helper.PrivilegedHelperError("synthetic cpuset membership differs")
        self._guardian_observation()
        return {
            "complete_control_plane_population": [
                self.population_peer.pid + 10_000
            ],
            "complete_control_plane_tid_population": [
                self.population_peer.pid + 10_000
            ],
            "complete_descendant_population": [self.population_peer.pid],
            "complete_descendant_tid_population": [self.population_peer.pid],
            "continuous_guard_active": True, "drift": False,
            "foreign_affinity_eligibility_count": len(
                self.population_observations[-1]["foreign_affinity_eligibility"]
            ),
            "phase": phase, "samples": self.population_samples,
            "scheduler_witness_sha256": self.population_observations[-1][
                "scheduler_witness_sha256"
            ],
        }

    def stop_population_guard(self):
        self.population_stop_calls += 1
        if self.population_stop_failure:
            raise helper.PrivilegedHelperError("synthetic population stop failure")
        if not self.population_active:
            raise helper.PrivilegedHelperError("population guard stopped twice")
        self.population_samples += 1
        self._guardian_observation()
        coverage_start = self.population_observations[0]["started_monotonic_ns"]
        coverage_end = self.population_observations[-1]["ended_monotonic_ns"] + 10
        final = self.population_observations[-1]
        final_foreign = copy.deepcopy(final["foreign_affinity_eligibility"])
        self.population_seal = {
            "bound_peer": self.population_peer.as_record(),
            "coverage_ended_monotonic_ns": coverage_end,
            "coverage_started_monotonic_ns": coverage_start,
            "drift": False,
            "final_child_cpuset_member_count": 1,
            "final_control_plane_process_count": 1,
            "final_control_plane_tid_count": 1,
            "final_descendant_process_count": 1,
            "final_descendant_tid_count": 1,
            "final_foreign_affinity_eligibility_count": len(final_foreign),
            "final_foreign_affinity_eligibility_sha256": hashlib.sha256(
                helper._bounded_canonical_json_bytes(
                    {"rows": final_foreign}, helper.MAX_GUARDIAN_LINE_BYTES,
                    "synthetic final foreign witness",
                )
            ).hexdigest(),
            "final_scheduler_witness_sha256": final[
                "scheduler_witness_sha256"
            ],
            "irq_population_changed": False,
            "maximum_observation_gap_ns": 50,
            "required_surfaces": list(self.guardian_surfaces),
            "schema_version": helper.SCHEMA_VERSION,
            "session_id": self.population_session_id,
            "thermal_or_throttle_event": False,
        }
        self.population_active = False
        return copy.deepcopy(self.population_seal)

    def recover_population_guard(self):
        if not self.population_active:
            return None
        self.population_active = False
        return {"external_recovery": "synthetic_population_guard_stopped"}

    def observe(self, phase, run_index):
        if (
            phase not in ("pre", "post")
            or type(run_index) is not int
            or run_index < 0
            or run_index >= helper.FORMAL_RUN_COUNT
        ):
            raise helper.PrivilegedHelperError("synthetic telemetry phase differs")
        increment = run_index * 1000 + (100 if phase == "post" else 0)
        snapshot_start = self.population_clock_ns
        cursor = snapshot_start
        receipts = []

        def append_source(source_id, measurement_kind, source_kind, source_path,
                          extraction, cpu_id, register, parsed_value, raw,
                          profile_source):
            nonlocal cursor
            started = cursor
            ended = started + 2
            cursor = ended + 1
            specification = helper.source_specification_record(
                source_id, measurement_kind, source_kind, source_path,
                extraction, cpu_id, register, profile_source,
            )
            receipts.append({
                "cpu_id": cpu_id, "extraction": extraction,
                "measurement_kind": measurement_kind,
                "parsed_value": parsed_value, "raw_hex": raw.hex(),
                "raw_sha256": hashlib.sha256(raw).hexdigest(),
                "read_ended_monotonic_ns": ended,
                "read_started_monotonic_ns": started, "register": register,
                "source_id": source_id, "source_kind": source_kind,
                "source_path": source_path,
                "source_spec_sha256": helper.source_specification_sha256(
                    specification
                ),
            })

        throttle = 7
        append_source(
            "cpu2.amd_throttle", "amd_throttle", "text_u64",
            "/sys/devices/platform/unmistakably-synthetic-amd/cpu2/throttle",
            "canonical_ascii_decimal_u64_one_line", 2, None, throttle,
            b"7\n", {
                "counter_width_bits": 64, "cpu_id": 2,
                "no_wrap_policy": "reject_decrease_or_wrap",
                "path": "/sys/devices/platform/unmistakably-synthetic-amd/cpu2/throttle",
                "provider": "amd", "register": None,
                "semantics": "monotonic_thermal_throttle_event_counter",
                "source_identity_sha256": "1" * 64, "source_kind": "text_u64",
                "specification_sha256": "2" * 64,
                "value_extraction": "canonical_ascii_decimal_u64_one_line",
            },
        )
        for metric_id, register, value in (
            ("cpu2.aperf", 0xE8, 1000 + increment),
            ("cpu2.mperf", 0xE7, 2000 + increment),
        ):
            append_source(
                metric_id, metric_id.split(".")[-1], "msr_u64_le",
                "/dev/cpu/2/msr", "full_u64_little_endian_pread", 2,
                register, value, value.to_bytes(8, "little"), {
                    "aperf_register": 0xE8, "cpu_id": 2,
                    "mperf_register": 0xE7, "path": "/dev/cpu/2/msr",
                    "source_identity_sha256": "3" * 64,
                    "source_kind": "msr_u64_le",
                },
            )
        append_source(
            "cpu2.scaling_cur_freq", "scaling_cur_freq", "text_integer_khz",
            "/sys/devices/system/cpu/cpu2/cpufreq/scaling_cur_freq",
            "canonical_ascii_decimal_u64_one_line", 2, None, 3_000_000,
            b"3000000\n", {
                "cpu_id": 2, "expected_khz": 3_000_000,
                "path": "/sys/devices/system/cpu/cpu2/cpufreq/scaling_cur_freq",
                "source_identity_sha256": "4" * 64,
                "source_kind": "text_integer_khz",
            },
        )
        append_source(
            "shared.f_ref", "f_ref", "text_integer_khz",
            "/sys/devices/system/cpu/cpu2/cpufreq/base_frequency",
            "canonical_ascii_decimal_u64_one_line", None, None, 3_000_000,
            b"3000000\n", {
                "reference_frequency_khz": 3_000_000,
                "reference_frequency_source": {
                    "path": "/sys/devices/system/cpu/cpu2/cpufreq/base_frequency",
                    "source_identity_sha256": "5" * 64,
                    "source_kind": "text_integer_khz",
                },
            },
        )
        temperature = 55000 + run_index * 10 + (10 if phase == "post" else 0)
        append_source(
            "shared.temperature", "temperature", "k10temp_text_integer",
            "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_input",
            "canonical_ascii_decimal_i64_one_line", None, None, temperature,
            (str(temperature) + "\n").encode("ascii"), {
                "device_path": "/sys/devices/platform/unmistakably-synthetic-k10temp",
                "input_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_input",
                "label_expected": "Tctl",
                "label_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/temp1_label",
                "maximum_millicelsius": 120000, "minimum_millicelsius": -40000,
                "module": "k10temp", "name_expected": "k10temp",
                "name_path": "/sys/devices/platform/unmistakably-synthetic-k10temp/name",
                "sampling_policy": "continuous_helper_read_bound_sensor",
                "source_identity_sha256": "6" * 64,
            },
        )
        receipts.sort(key=lambda item: item["source_id"])
        snapshot_end = max(item["read_ended_monotonic_ns"] for item in receipts)
        self.population_clock_ns = snapshot_end + 10
        return {
            "phase": phase, "run_index": run_index,
            "schema_version": helper.SCHEMA_VERSION,
            "snapshot_ended_monotonic_ns": snapshot_end,
            "snapshot_skew_ns": snapshot_end - snapshot_start,
            "snapshot_started_monotonic_ns": snapshot_start,
            "sources": receipts,
        }

    def restore(self, prior, boundary):
        if self.persistent_restore_failure:
            raise helper.PrivilegedHelperError("persistent synthetic restore failure")
        self._step(boundary, "dma_latency", lambda: self.state.__setitem__(
            "dma_latency_held", prior["dma_latency_held"],
        ))
        self._step(boundary, "cpuset_membership_remove", lambda: self.state.__setitem__(
            "cpuset", copy.deepcopy(prior["cpuset"]),
        ))
        self._step(boundary, "text_control", lambda: self.state.__setitem__(
            "text", copy.deepcopy(prior["text"]),
        ))
        self._step(boundary, "service", lambda: self.state.__setitem__(
            "service_irqbalance", prior["service_irqbalance"],
        ))
        self._step(boundary, "module", lambda: self.state.__setitem__(
            "modules", copy.deepcopy(prior["modules"]),
        ))

    def validate_restored(self, prior):
        if self.state != prior:
            raise helper.PrivilegedHelperError("synthetic exact prior state was not restored")
        if self.population_active:
            raise helper.PrivilegedHelperError("population guard remains active")
        return copy.deepcopy(self.state)

    def state_digest(self):
        return hashlib.sha256(
            helper.canonical_control_state_bytes(self.state)
        ).hexdigest()

    def close(self):
        self.closed = True
        if self.close_failure:
            raise helper.PrivilegedHelperError("synthetic descriptor close failure")

    def assert_prior(self, testcase):
        testcase.assertEqual(self.state, self.original)
        testcase.assertFalse(self.population_active)


class SessionHarness:
    def __init__(self, *, fault=None, binding=None, peer=None, backend=None, store=None):
        self.binding = binding or frozen_binding()
        self.peer = peer or helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        self.backend = backend or SyntheticPrivilegedBackend()
        self.store = store or MemoryJournalStore()
        self.fault = fault
        self.guardian_contract = frozen_guardian_contract()
        self.guardian_directory = tempfile.TemporaryDirectory(dir="/tmp")
        guardian_path = os.path.join(
            self.guardian_directory.name, "unmistakably-synthetic-guardian.jsonl",
        )
        guardian_fd = os.open(
            guardian_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.guardian_writer = helper.GuardianEvidenceWriter(
            self.guardian_contract, guardian_fd, expected_uid=os.getuid(),
            retain_bytes_after_close=True,
        )
        self.control_path = os.path.join(
            self.guardian_directory.name,
            "unmistakably-synthetic-control-evidence.jsonl",
        )
        control_fd = os.open(
            self.control_path,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        self.control_writer = helper.ControlEvidenceWriter(
            control_fd, expected_uid=os.getuid(), retain_bytes_after_close=True,
        )
        self.client, self.server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.client.settimeout(2.0)
        self.server.settimeout(2.0)
        self.session = helper.HelperSession(
            self.server, self.binding, self.peer, self.backend, self.store,
            frozen_run_slots(),
            self.guardian_contract, self.guardian_writer,
            self.control_writer,
            fault_injector=fault,
        )
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            self.result = self.session.run()
        except BaseException as exc:
            self.error = exc
        finally:
            self.server.close()

    def handshake(self, *, binding=None, client_nonce="9" * 64, mutate=None):
        challenge = helper.recv_packet(self.client)
        value = dict(helper.hello_record(
            binding or self.binding, client_nonce, challenge["challenge_nonce"],
        ))
        if mutate is not None:
            mutate(value, challenge)
        helper.send_packet(self.client, value)
        return helper.recv_packet(self.client)

    def drive(self, *, after_pre=None):
        hello = self.handshake()
        session_id = hello["session_id"]
        helper.send_packet(self.client, helper.command_record(
            "cp2e_privileged_apply", 1, session_id,
        ))
        applied = helper.recv_packet(self.client)
        records = [hello, applied]
        for run_index in range(helper.FORMAL_RUN_COUNT):
            sequence = 2 + 2 * run_index
            helper.send_packet(self.client, helper.command_record(
                "cp2e_privileged_observe", sequence, session_id,
                phase="pre", run_index=run_index,
            ))
            records.append(helper.recv_packet(self.client))
            if run_index == 0 and after_pre is not None:
                after_pre(self)
            helper.send_packet(self.client, helper.command_record(
                "cp2e_privileged_observe", sequence + 1, session_id,
                phase="post", run_index=run_index,
            ))
            records.append(helper.recv_packet(self.client))
        helper.send_packet(self.client, helper.command_record(
            "cp2e_privileged_restore", helper.RESTORE_SEQUENCE, session_id,
        ))
        records.append(helper.recv_packet(self.client))
        return tuple(records)

    def drive_tolerant(self):
        try:
            self.drive()
        except (OSError, helper.PrivilegedHelperError, TimeoutError):
            pass
        finally:
            try:
                self.client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def finish(self):
        self.client.close()
        self.thread.join(4.0)
        if self.thread.is_alive():
            raise AssertionError("helper server thread did not terminate")
        self.guardian_directory.cleanup()


class CanonicalProtocolTests(unittest.TestCase):
    def test_exact_success_protocol_and_plan_bound_raw_receipts(self):
        harness = SessionHarness()
        records = harness.drive()
        harness.finish()
        self.assertIsNone(harness.error)
        self.assertEqual(harness.result["state"], "restored")
        harness.backend.assert_prior(self)
        self.assertEqual(len(harness.store.completed), 1)
        self.assertEqual(len(records), 2 + 2 * helper.FORMAL_RUN_COUNT + 1)
        for run_index in range(helper.FORMAL_RUN_COUNT):
            pre = records[2 + 2 * run_index]
            post = records[3 + 2 * run_index]
            self.assertEqual(
                (pre["payload"]["run_index"], pre["payload"]["phase"]),
                (run_index, "pre"),
            )
            self.assertEqual(
                (post["payload"]["run_index"], post["payload"]["phase"]),
                (run_index, "post"),
            )
            self.assertEqual(pre["payload"]["receipt"]["run_index"], run_index)
            self.assertEqual(post["payload"]["receipt"]["run_index"], run_index)
            self.assertEqual(
                pre["payload"]["run_slot"], frozen_run_slots()[run_index].as_record()
            )
            self.assertIsNone(pre["payload"]["population_seal"])
            if run_index + 1 == helper.FORMAL_RUN_COUNT:
                self.assertIsNotNone(post["payload"]["population_seal"])
            else:
                self.assertIsNone(post["payload"]["population_seal"])
        pre_receipts = records[2]["payload"]["receipt"]["sources"]
        self.assertEqual(
            {item["measurement_kind"] for item in pre_receipts},
            {"aperf", "mperf", "scaling_cur_freq", "amd_throttle", "f_ref",
             "temperature"},
        )
        self.assertNotIn("argv", json.dumps(records))
        self.assertEqual(harness.backend.population_stop_calls, 1)
        verified = helper.verify_transcript_bytes(
            harness.session.transcript_bytes,
            harness.session.terminal_receipt_bytes,
            harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )
        self.assertEqual(len(verified.events), 16)
        self.assertEqual(verified.terminal["terminal_status"], "success")
        self.assertEqual(verified.terminal["population_stop_count"], 1)
        seal = verified.events[-2]["payload"]["population_seal"]
        self.assertGreaterEqual(seal["observation_count"], 14)
        self.assertEqual(
            seal["evidence_sha256"],
            hashlib.sha256(harness.session.guardian_evidence_bytes).hexdigest(),
        )
        self.assertGreaterEqual(
            seal["final_foreign_affinity_eligibility_count"], 1,
        )
        control = helper.verify_control_evidence_bytes(
            harness.session.control_evidence_bytes,
            verified.events[-1]["payload"]["control_evidence_seal"],
            harness.binding, verified.terminal["session_id"],
            verified.terminal["journal_id"],
        )
        self.assertEqual(
            [row["phase"] for row in control.records],
            ["prior", "applied", "restored"],
        )
        self.assertNotIn("prior_state", verified.events[2]["payload"])
        self.assertNotIn("applied_state", verified.events[2]["payload"])
        self.assertNotIn("restored_state", verified.events[-1]["payload"])

    def test_full_states_above_packet_limit_live_only_in_control_stream(self):
        backend = SyntheticPrivilegedBackend()
        backend.state["large_control_population"] = [
            chr(ord("a") + index) * helper.MAX_STRING_CHARACTERS
            for index in range(6)
        ]
        backend.original = copy.deepcopy(backend.state)
        self.assertGreater(
            len(helper.canonical_control_state_bytes(backend.state)),
            helper.MAX_PACKET_BYTES,
        )
        harness = SessionHarness(backend=backend)
        records = harness.drive()
        harness.finish()
        self.assertIsNone(harness.error)
        backend.assert_prior(self)
        apply_ack = records[1]["payload"]
        restore_ack = records[-1]["payload"]
        self.assertNotIn("prior_state", apply_ack)
        self.assertNotIn("applied_state", apply_ack)
        self.assertNotIn("restored_state", restore_ack)
        self.assertLessEqual(
            len(helper.canonical_json_bytes(records[1])), helper.MAX_PACKET_BYTES,
        )
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        transcript = helper.verify_transcript_bytes(
            harness.session.transcript_bytes,
            harness.session.terminal_receipt_bytes,
            harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )
        control = helper.verify_control_evidence_bytes(
            harness.session.control_evidence_bytes,
            transcript.events[-1]["payload"]["control_evidence_seal"],
            harness.binding, terminal["session_id"], terminal["journal_id"],
        )
        self.assertEqual(
            control.records[0]["state"]["large_control_population"],
            backend.original["large_control_population"],
        )
        self.assertEqual(
            control.records[-1]["state_sha256"],
            control.records[0]["state_sha256"],
        )

    def test_noncanonical_duplicate_unknown_float_and_wrong_type_reject(self):
        payloads = (
            b'{"schema_version":1, "record_type":"x"}\n',
            b'{"a":1,"a":1}\n',
            b'{"a":1.0}\n',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(helper.ProtocolError):
                    helper.strict_json_bytes(payload)
        harness = SessionHarness()
        challenge = helper.recv_packet(harness.client)
        value = dict(helper.hello_record(
            harness.binding, "9" * 64, challenge["challenge_nonce"],
        ))
        value["unknown"] = 1
        helper.send_packet(harness.client, value)
        with self.assertRaises((helper.PeerClosed, OSError)):
            helper.recv_packet(harness.client)
        harness.finish()
        self.assertIsInstance(harness.error, helper.ProtocolError)

    def test_oversized_or_truncated_packet_rejects(self):
        harness = SessionHarness()
        helper.recv_packet(harness.client)
        harness.client.send(b"x" * (helper.MAX_PACKET_BYTES + 1))
        with self.assertRaises((helper.PeerClosed, OSError)):
            helper.recv_packet(harness.client)
        harness.finish()
        self.assertIsInstance(harness.error, helper.ProtocolError)

    def test_ancillary_descriptors_and_control_truncation_reject(self):
        harness = SessionHarness()
        challenge = helper.recv_packet(harness.client)
        hello = helper.hello_record(
            harness.binding, "9" * 64, challenge["challenge_nonce"]
        )
        read_fd, write_fd = os.pipe()
        try:
            sent = harness.client.sendmsg(
                [helper.canonical_json_bytes(hello)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", read_fd))],
            )
            self.assertGreater(sent, 0)
        finally:
            os.close(read_fd)
            os.close(write_fd)
        with self.assertRaises((helper.PeerClosed, OSError)):
            helper.recv_packet(harness.client)
        harness.finish()
        self.assertIsInstance(harness.error, helper.ProtocolError)

        class TruncatedControlSocket:
            family = socket.AF_UNIX

            @staticmethod
            def getsockopt(_level, _option):
                return socket.SOCK_SEQPACKET

            @staticmethod
            def recvmsg(_payload_size, _ancillary_size):
                return (b"{}\n", [], getattr(socket, "MSG_CTRUNC", 8), None)

        with self.assertRaisesRegex(helper.ProtocolError, "ancillary"):
            helper.recv_packet(TruncatedControlSocket())

    def test_wrong_record_type_sequence_and_nonce_reject(self):
        mutations = (
            lambda value, _challenge: value.__setitem__("record_type", "wrong"),
            lambda value, _challenge: value.__setitem__("sequence", 1),
            lambda value, _challenge: value.__setitem__("sequence", False),
            lambda value, _challenge: value.__setitem__("schema_version", True),
            lambda value, _challenge: value.__setitem__("client_nonce", "0" * 63),
            lambda value, _challenge: value.__setitem__("challenge_nonce", "f" * 64),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                harness = SessionHarness()
                with self.assertRaises((helper.PeerClosed, OSError)):
                    harness.handshake(mutate=mutation)
                harness.finish()
                self.assertIsInstance(harness.error, helper.ProtocolError)

    def test_boolean_observation_index_rejects_and_restores(self):
        harness = SessionHarness()
        hello = harness.handshake()
        helper.send_packet(harness.client, helper.command_record(
            "cp2e_privileged_apply", 1, hello["session_id"],
        ))
        helper.recv_packet(harness.client)
        helper.send_packet(harness.client, {
            "phase": "pre", "record_type": "cp2e_privileged_observe",
            "run_index": False, "schema_version": helper.SCHEMA_VERSION,
            "sequence": 2, "session_id": hello["session_id"],
        })
        with self.assertRaises((helper.PeerClosed, OSError)):
            helper.recv_packet(harness.client)
        harness.finish()
        self.assertIsInstance(harness.error, helper.ProtocolError)
        harness.backend.assert_prior(self)
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        self.assertEqual(terminal["terminal_status"], "fail_closed")

    def test_fresh_helper_challenge_rejects_transcript_replay(self):
        first = SessionHarness()
        challenge = helper.recv_packet(first.client)
        replay = helper.hello_record(first.binding, "9" * 64, challenge["challenge_nonce"])
        helper.send_packet(first.client, replay)
        helper.recv_packet(first.client)
        first.client.close()
        first.finish()
        first.backend.assert_prior(self)

        second = SessionHarness()
        helper.recv_packet(second.client)
        helper.send_packet(second.client, replay)
        with self.assertRaises((helper.PeerClosed, OSError)):
            helper.recv_packet(second.client)
        second.finish()
        self.assertIsInstance(second.error, helper.ProtocolError)

    def test_each_peer_credential_field_mismatch_rejects_before_mutation(self):
        actual = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        mismatches = (
            replace(actual, pid=actual.pid + 1),
            replace(actual, uid=actual.uid + 1),
            replace(actual, gid=actual.gid + 1),
        )
        for peer in mismatches:
            with self.subTest(peer=peer):
                harness = SessionHarness(peer=peer)
                with self.assertRaises((helper.PeerClosed, OSError)):
                    helper.recv_packet(harness.client)
                harness.finish()
                harness.backend.assert_prior(self)

    def test_every_frozen_hash_substitution_rejects(self):
        fields = (
            "profile_sha256", "plan_sha256", "source_sha256",
            "protocol_core_sha256", "root_launcher_sha256",
            "import_closure_sha256", "sudoers_sha256",
        )
        for field in fields:
            with self.subTest(field=field):
                harness = SessionHarness()
                substituted = replace(harness.binding, **{field: "9" * 64})
                with self.assertRaises((helper.PeerClosed, OSError)):
                    harness.handshake(binding=substituted)
                harness.finish()
                self.assertIsInstance(harness.error, helper.ProtocolError)
                harness.backend.assert_prior(self)

    def test_client_cannot_select_path_argv_module_service_or_msr(self):
        forbidden = {
            "argv": ["/bin/sh"], "module": "nouveau",
            "path": "/etc/shadow", "register": 0x10,
            "service": "ssh",
        }
        for key, value in forbidden.items():
            with self.subTest(key=key):
                harness = SessionHarness()
                hello = harness.handshake()
                command = dict(helper.command_record(
                    "cp2e_privileged_apply", 1, hello["session_id"],
                ))
                command[key] = value
                helper.send_packet(harness.client, command)
                with self.assertRaises((helper.PeerClosed, OSError)):
                    helper.recv_packet(harness.client)
                harness.finish()
                self.assertIsInstance(harness.error, helper.ProtocolError)
                harness.backend.assert_prior(self)


class TranscriptProtectingTests(unittest.TestCase):
    def completed(self):
        harness = SessionHarness()
        harness.drive()
        harness.finish()
        self.assertIsNone(harness.error)
        self.guardian_evidence = harness.session.guardian_evidence_bytes
        self.control_evidence = harness.session.control_evidence_bytes
        self.guardian_contract = harness.guardian_contract
        return harness

    def assert_rejects(self, events, terminal, guardian_evidence=None):
        transcript, receipt = reseal_transcript(events, terminal)
        with self.assertRaises(helper.PrivilegedHelperError):
            helper.verify_transcript_bytes(
                transcript, receipt, frozen_binding(), frozen_run_slots(),
                (self.guardian_evidence if guardian_evidence is None else guardian_evidence),
                self.guardian_contract,
                self.control_evidence,
            )

    def test_reordered_missing_and_duplicate_event_reject(self):
        harness = self.completed()
        source_events, terminal = transcript_fixture(harness)
        cases = []
        reordered = copy.deepcopy(source_events)
        reordered[3], reordered[4] = reordered[4], reordered[3]
        cases.append(reordered)
        cases.append(copy.deepcopy(source_events[:7] + source_events[8:]))
        cases.append(copy.deepcopy(source_events[:5] + [source_events[4]] + source_events[5:]))
        for events in cases:
            with self.subTest(event_count=len(events)):
                self.assert_rejects(events, terminal)

    def test_coordinated_frozen_slot_and_source_binding_substitution_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)
        slot_changed = copy.deepcopy(events)
        slot_changed[3]["payload"]["run_slot"]["mode"] = "schur"
        self.assert_rejects(slot_changed, terminal)

        source_changed = copy.deepcopy(events)
        source = source_changed[4]["payload"]["receipt"]["sources"][1]
        source["source_spec_sha256"] = "9" * 64
        source_changed[4]["payload"]["receipt_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(source_changed[4]["payload"]["receipt"])
        ).hexdigest()
        self.assert_rejects(source_changed, terminal)

    def test_control_state_digest_checkpoint_and_terminal_seal_substitution_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)

        changed = copy.deepcopy(events)
        changed[2]["payload"]["prior_state_sha256"] = "9" * 64
        self.assert_rejects(changed, terminal)

        changed = copy.deepcopy(events)
        checkpoint = changed[2]["payload"]["control_evidence_checkpoint"]
        checkpoint["rows"][0]["state_sha256"] = "8" * 64
        changed[2]["payload"]["control_evidence_checkpoint_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(checkpoint)
        ).hexdigest()
        self.assert_rejects(changed, terminal)

        changed = copy.deepcopy(events)
        changed_terminal = copy.deepcopy(terminal)
        seal = changed[-1]["payload"]["control_evidence_seal"]
        seal["rows"][-1]["state_sha256"] = "7" * 64
        seal_sha256 = hashlib.sha256(helper.canonical_json_bytes(seal)).hexdigest()
        changed[-1]["payload"]["control_evidence_seal_sha256"] = seal_sha256
        changed_terminal["control_evidence_seal_sha256"] = seal_sha256
        self.assert_rejects(changed, changed_terminal)

    def test_raw_byte_substitution_and_noncanonical_extraction_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)
        changed = copy.deepcopy(events)
        source = changed[3]["payload"]["receipt"]["sources"][1]
        substituted = (source["parsed_value"] + 1).to_bytes(8, "little")
        source["raw_hex"] = substituted.hex()
        source["raw_sha256"] = hashlib.sha256(substituted).hexdigest()
        changed[3]["payload"]["receipt_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(changed[3]["payload"]["receipt"])
        ).hexdigest()
        self.assert_rejects(changed, terminal)

        noncanonical = copy.deepcopy(events)
        frequency = next(
            row for row in noncanonical[3]["payload"]["receipt"]["sources"]
            if row["measurement_kind"] == "scaling_cur_freq"
        )
        raw = b"03000000\n"
        frequency["raw_hex"] = raw.hex()
        frequency["raw_sha256"] = hashlib.sha256(raw).hexdigest()
        noncanonical[3]["payload"]["receipt_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(noncanonical[3]["payload"]["receipt"])
        ).hexdigest()
        self.assert_rejects(noncanonical, terminal)

    def test_signed_temperature_is_accepted_but_bool_integer_seams_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)
        for event in events[3:15]:
            temperature = next(
                row for row in event["payload"]["receipt"]["sources"]
                if row["measurement_kind"] == "temperature"
            )
            raw = b"-5000\n"
            temperature["parsed_value"] = -5000
            temperature["raw_hex"] = raw.hex()
            temperature["raw_sha256"] = hashlib.sha256(raw).hexdigest()
            event["payload"]["receipt_sha256"] = hashlib.sha256(
                helper.canonical_json_bytes(event["payload"]["receipt"])
            ).hexdigest()
        transcript, receipt = reseal_transcript(events, terminal)
        helper.verify_transcript_bytes(
            transcript, receipt, harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )

        mutations = []
        value = copy.deepcopy(events)
        value[0]["event_index"] = False
        mutations.append((value, terminal, True))
        value = copy.deepcopy(events)
        value[3]["payload"]["receipt"]["run_index"] = False
        value[3]["payload"]["receipt_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(value[3]["payload"]["receipt"])
        ).hexdigest()
        mutations.append((value, terminal, False))
        value = copy.deepcopy(events)
        value[3]["payload"]["receipt"]["sources"][1]["parsed_value"] = False
        value[3]["payload"]["receipt_sha256"] = hashlib.sha256(
            helper.canonical_json_bytes(value[3]["payload"]["receipt"])
        ).hexdigest()
        mutations.append((value, terminal, False))
        for mutated_events, mutated_terminal, preserve_indices in mutations:
            with self.subTest(preserve_indices=preserve_indices):
                transcript_bytes, terminal_bytes = reseal_transcript(
                    mutated_events, mutated_terminal,
                    preserve_event_indices=preserve_indices,
                )
                with self.assertRaises(helper.PrivilegedHelperError):
                    helper.verify_transcript_bytes(
                        transcript_bytes, terminal_bytes, harness.binding,
                        frozen_run_slots(),
                        harness.session.guardian_evidence_bytes,
                        harness.guardian_contract,
                        harness.session.control_evidence_bytes,
                    )

    def test_guardian_gap_interval_surface_and_population_substitution_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)
        guardian_records = guardian_fixture(harness)
        cases = []
        for mutation in ("gap", "interval", "surface", "foreign", "throttle"):
            changed = copy.deepcopy(events)
            payload = changed[14]["payload"]
            records = copy.deepcopy(guardian_records)
            if mutation == "gap":
                records[1]["started_monotonic_ns"] += 1000
            elif mutation == "interval":
                records[1]["ended_monotonic_ns"] += 1000
            elif mutation == "surface":
                records[1]["surface_states"].pop()
            elif mutation == "foreign":
                records[1]["foreign_affinity_eligibility"] = [{
                    "effective_affinity_cpu_ids": [2], "tid": 999999,
                }]
            else:
                throttle = records[1]["typed_telemetry"][0]
                raw = b"8\n"
                throttle["parsed_value"] = 8
                throttle["raw_hex"] = raw.hex()
                throttle["raw_sha256"] = hashlib.sha256(raw).hexdigest()
            evidence, seal = reseal_guardian(records, payload["population_seal"])
            payload["population_seal"] = seal
            payload["population_seal_sha256"] = hashlib.sha256(
                helper.canonical_json_bytes(seal)
            ).hexdigest()
            changed[15]["payload"]["population_seal_sha256"] = payload[
                "population_seal_sha256"
            ]
            changed_terminal = copy.deepcopy(terminal)
            changed_terminal["population_seal_sha256"] = payload[
                "population_seal_sha256"
            ]
            cases.append((changed, changed_terminal, evidence, mutation))
        for changed, changed_terminal, evidence, mutation in cases:
            with self.subTest(mutation=mutation):
                self.assert_rejects(changed, changed_terminal, evidence)

    def test_terminal_closure_and_boolean_counter_substitution_reject(self):
        harness = self.completed()
        events, terminal = transcript_fixture(harness)
        mutations = (
            ("event_count", False),
            ("population_stop_count", False),
            ("abnormal_recovery", 0),
            ("restoration_proved", 1),
        )
        for key, value in mutations:
            with self.subTest(key=key):
                changed = copy.deepcopy(terminal)
                changed[key] = value
                if key == "event_count":
                    transcript = harness.session.transcript_bytes
                    receipt = helper.canonical_json_bytes(changed)
                else:
                    transcript, receipt = reseal_transcript(events, changed)
                with self.assertRaises(helper.PrivilegedHelperError):
                    helper.verify_transcript_bytes(
                        transcript, receipt, harness.binding, frozen_run_slots(),
                        harness.session.guardian_evidence_bytes,
                        harness.guardian_contract,
                        harness.session.control_evidence_bytes,
                    )
        changed = copy.deepcopy(terminal)
        changed["descriptor_closure"]["passed"] = False
        transcript, receipt = reseal_transcript(events, changed)
        with self.assertRaises(helper.PrivilegedHelperError):
            helper.verify_transcript_bytes(
                transcript, receipt, harness.binding, frozen_run_slots(),
                harness.session.guardian_evidence_bytes,
                harness.guardian_contract,
                harness.session.control_evidence_bytes,
            )


class GuardianEvidenceWriterTests(unittest.TestCase):
    def open_writer(self, contract=None):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        path = os.path.join(directory.name, "synthetic-guardian.jsonl")
        descriptor = os.open(
            path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        writer = helper.GuardianEvidenceWriter(
            contract or frozen_guardian_contract(), descriptor,
            expected_uid=os.getuid(), retain_bytes_after_close=True,
        )
        return directory, path, writer

    def test_nonappend_wrong_mode_hardlink_and_nonregular_fd_reject(self):
        for case in ("nonappend", "readonly", "wrong_mode", "hardlink", "pipe"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                if case == "pipe":
                    descriptor, other = os.pipe()
                    os.close(other)
                else:
                    path = os.path.join(directory, "guardian.jsonl")
                    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                    if case != "nonappend":
                        flags |= os.O_APPEND
                    descriptor = os.open(path, flags, 0o600)
                    if case == "readonly":
                        os.close(descriptor)
                        descriptor = os.open(
                            path, os.O_RDONLY | os.O_APPEND | os.O_CLOEXEC,
                        )
                    elif case == "wrong_mode":
                        os.fchmod(descriptor, 0o640)
                    elif case == "hardlink":
                        os.link(path, path + ".other")
                try:
                    with self.assertRaises(helper.PrivilegedHelperError):
                        helper.GuardianEvidenceWriter(
                            frozen_guardian_contract(), descriptor,
                            expected_uid=os.getuid(), retain_bytes_after_close=True,
                        )
                finally:
                    os.close(descriptor)

        with tempfile.TemporaryDirectory(dir="/tmp") as directory:
            path = os.path.join(directory, "production-write-only.jsonl")
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                0o600,
            )
            writer = helper.GuardianEvidenceWriter(
                frozen_guardian_contract(), descriptor,
                expected_uid=os.getuid(), retain_bytes_after_close=False,
            )
            writer.close()

    def test_unlinked_held_inode_and_profile_byte_bound_reject_before_append(self):
        directory, path, writer = self.open_writer()
        backend = SyntheticPrivilegedBackend()
        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        try:
            writer.bind_identity(peer, "a" * 64)
            backend.start_population_guard(peer, "a" * 64, writer.append)
            os.unlink(path)
            with self.assertRaises(helper.PrivilegedHelperError):
                backend.validate_population("synthetic_unlinked_inode")
            self.assertEqual(writer.size, 0)
        finally:
            writer.close()
            directory.cleanup()

        contract = frozen_guardian_contract()
        with self.assertRaises(helper.PrivilegedHelperError):
            replace(
                contract,
                maximum_evidence_bytes=(
                    contract.maximum_observation_count
                    * contract.maximum_line_bytes - 1
                ),
            )

        line_bounded = replace(frozen_guardian_contract(), maximum_line_bytes=1)
        directory, _path, writer = self.open_writer(line_bounded)
        backend = SyntheticPrivilegedBackend()
        try:
            writer.bind_identity(peer, "e" * 64)
            backend.start_population_guard(peer, "e" * 64, writer.append)
            with self.assertRaises(helper.RecoveryIndeterminate):
                backend.validate_population("synthetic_line_bound")
            self.assertEqual(writer.size, 0)
        finally:
            writer.close()
            directory.cleanup()

    def test_count_bound_and_late_append_after_seal_are_fail_closed(self):
        count_contract = helper.GuardianEvidenceContract(
            poll_interval_ns=10, maximum_observation_gap_ns=10,
            maximum_campaign_duration_ns=10, maximum_observation_count=2,
            maximum_line_bytes=8192, maximum_evidence_bytes=1024 * 1024,
            required_surfaces=frozen_guardian_contract().required_surfaces,
        )
        directory, _path, writer = self.open_writer(count_contract)
        backend = SyntheticPrivilegedBackend()
        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        try:
            writer.bind_identity(peer, "c" * 64)
            backend.start_population_guard(peer, "c" * 64, writer.append)
            backend.validate_population("synthetic_count_0")
            backend.validate_population("synthetic_count_1")
            with self.assertRaises(helper.RecoveryIndeterminate):
                backend.validate_population("synthetic_count_2")
            self.assertEqual(writer.count, 2)
        finally:
            writer.close()
            directory.cleanup()

        directory, _path, writer = self.open_writer()
        backend = SyntheticPrivilegedBackend()
        try:
            writer.bind_identity(peer, "d" * 64)
            backend.start_population_guard(peer, "d" * 64, writer.append)
            backend.validate_population("synthetic_seal")
            terminal = backend.stop_population_guard()
            writer.seal(terminal)
            last = dict(helper.strict_json_bytes(
                writer.payload.splitlines(keepends=True)[-1]
            ))
            for key in (
                "previous_record_sha256", "record_type", "schema_version", "sequence",
            ):
                del last[key]
            with self.assertRaises(helper.RecoveryIndeterminate):
                writer.append(last)
            with self.assertRaises(helper.RecoveryIndeterminate):
                writer.close()
        finally:
            if not writer._closed:
                os.close(writer.descriptor)
            directory.cleanup()


class ControlEvidenceWriterTests(unittest.TestCase):
    def open_writer(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        path = os.path.join(directory.name, "synthetic-control-evidence.jsonl")
        descriptor = os.open(
            path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
            0o600,
        )
        writer = helper.ControlEvidenceWriter(
            descriptor, expected_uid=os.getuid(), retain_bytes_after_close=True,
        )
        writer.bind_identity(frozen_binding(), "a" * 64, "b" * 32)
        return directory, path, writer

    def test_preopened_fd_must_be_empty_append_rdwr_single_link_regular(self):
        for case in (
            "nonappend", "writeonly", "wrong_mode", "prepopulated", "hardlink",
            "pipe",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                if case == "pipe":
                    descriptor, other = os.pipe()
                    os.close(other)
                else:
                    path = os.path.join(directory, "control.jsonl")
                    access = os.O_WRONLY if case == "writeonly" else os.O_RDWR
                    flags = access | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                    if case != "nonappend":
                        flags |= os.O_APPEND
                    descriptor = os.open(path, flags, 0o600)
                    if case == "wrong_mode":
                        os.fchmod(descriptor, 0o640)
                    elif case == "prepopulated":
                        os.write(descriptor, b"{}\n")
                    elif case == "hardlink":
                        os.link(path, path + ".other")
                try:
                    with self.assertRaises(helper.PrivilegedHelperError):
                        helper.ControlEvidenceWriter(
                            descriptor, expected_uid=os.getuid(),
                            retain_bytes_after_close=False,
                        )
                finally:
                    os.close(descriptor)

    def append_complete(self, writer):
        prior = {"population": [1, 2, 3], "value": "prior"}
        applied = {"population": [1, 2, 3], "value": "applied"}
        writer.append("prior", prior)
        writer.append("applied", applied)
        writer.append("restored", prior)
        return prior, applied, writer.seal()

    def test_partial_writes_are_completed_and_exactly_sealed(self):
        directory, _path, writer = self.open_writer()
        original_write = os.write
        calls = []

        def short_write(descriptor, payload):
            calls.append(len(payload))
            return original_write(descriptor, payload[:min(11, len(payload))])

        try:
            with mock.patch.object(helper.os, "write", side_effect=short_write):
                writer.append("prior", {"exact": "prior", "population": [1, 2]})
            writer.append("applied", {"exact": "applied", "population": [1, 2]})
            writer.append("restored", {"exact": "prior", "population": [1, 2]})
            seal = writer.seal()
            self.assertGreater(len(calls), 1)
            verified = helper.verify_control_evidence_bytes(
                writer.payload, seal, frozen_binding(), "a" * 64, "b" * 32,
            )
            self.assertEqual(len(verified.records), 3)
            self.assertEqual(
                seal["evidence_size_bytes"], len(writer.payload),
            )
        finally:
            writer.close()
            directory.cleanup()

    def test_partial_write_followed_by_no_progress_is_indeterminate(self):
        directory, _path, writer = self.open_writer()
        original_write = os.write
        calls = []

        def partial_then_zero(descriptor, payload):
            calls.append(len(payload))
            if len(calls) == 1:
                return original_write(descriptor, payload[:7])
            return 0

        try:
            with mock.patch.object(
                helper.os, "write", side_effect=partial_then_zero,
            ), self.assertRaisesRegex(
                helper.RecoveryIndeterminate, "no progress",
            ):
                writer.append("prior", {"exact": "prior"})
            self.assertEqual(writer.checkpoint["row_count"], 0)
            self.assertGreater(os.fstat(writer.descriptor).st_size, 0)
            with self.assertRaises(helper.RecoveryIndeterminate):
                writer.close()
        finally:
            if not writer._closed:
                os.close(writer.descriptor)
            directory.cleanup()

    def test_truncation_replacement_and_link_population_are_detected(self):
        for case in ("truncate", "replace", "hardlink"):
            with self.subTest(case=case):
                directory, path, writer = self.open_writer()
                try:
                    writer.append("prior", {"exact": "prior"})
                    if case == "truncate":
                        os.ftruncate(writer.descriptor, 0)
                    elif case == "replace":
                        replacement = path + ".replacement"
                        replacement_fd = os.open(
                            replacement,
                            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL
                            | os.O_CLOEXEC,
                            0o600,
                        )
                        os.close(replacement_fd)
                        os.replace(replacement, path)
                    else:
                        os.link(path, path + ".linked")
                    with self.assertRaisesRegex(
                        helper.RecoveryIndeterminate, "held inode",
                    ):
                        writer.append("applied", {"exact": "applied"})
                    if case == "hardlink":
                        os.unlink(path + ".linked")
                        writer.close()
                    else:
                        with self.assertRaises(helper.RecoveryIndeterminate):
                            writer.close()
                finally:
                    if not writer._closed:
                        os.close(writer.descriptor)
                    directory.cleanup()

    def test_oversized_state_and_population_are_rejected_before_write(self):
        cases = (
            {"population": list(range(helper.MAX_CONTAINER_ITEMS + 1))},
            {
                "population": [
                    "x" * helper.MAX_STRING_CHARACTERS
                    for _ in range(
                        helper.MAX_CONTROL_STATE_BYTES
                        // helper.MAX_STRING_CHARACTERS + 2
                    )
                ],
            },
        )
        for state in cases:
            with self.subTest(population=len(state["population"])):
                directory, _path, writer = self.open_writer()
                try:
                    with self.assertRaises(helper.PrivilegedHelperError):
                        writer.append("prior", state)
                    self.assertEqual(writer.checkpoint["row_count"], 0)
                finally:
                    writer.close()
                    directory.cleanup()

    def test_same_size_digest_substitution_and_seal_substitution_reject(self):
        directory, path, writer = self.open_writer()
        try:
            prior, _applied, seal = self.append_complete(writer)
            evidence = writer.payload
            changed = bytearray(evidence)
            changed[1] = ord("z") if changed[1] != ord("z") else ord("y")
            with self.assertRaisesRegex(helper.ProtocolError, "digest"):
                helper.verify_control_evidence_bytes(
                    bytes(changed), seal, frozen_binding(), "a" * 64, "b" * 32,
                )
            with self.assertRaises(helper.ProtocolError):
                helper.verify_control_evidence_bytes(
                    evidence[:-1], seal, frozen_binding(), "a" * 64, "b" * 32,
                )
            substituted = copy.deepcopy(seal)
            substituted["rows"][0]["state_sha256"] = "9" * 64
            with self.assertRaises(helper.ProtocolError):
                helper.verify_control_evidence_bytes(
                    evidence, substituted, frozen_binding(), "a" * 64, "b" * 32,
                )
            self.assertEqual(prior, {"population": [1, 2, 3], "value": "prior"})
        finally:
            writer.close()
            directory.cleanup()

    def test_concurrent_same_size_overwrite_is_detected_before_seal(self):
        directory, path, writer = self.open_writer()
        try:
            writer.append("prior", {"exact": "prior"})
            writer.append("applied", {"exact": "applied"})
            writer.append("restored", {"exact": "prior"})
            tamper = os.open(path, os.O_RDWR | os.O_CLOEXEC)
            try:
                original = os.pread(tamper, 1, 0)
                os.pwrite(tamper, b"[" if original != b"[" else b"{", 0)
                os.fsync(tamper)
            finally:
                os.close(tamper)
            with self.assertRaisesRegex(
                helper.RecoveryIndeterminate, "digest changed",
            ):
                writer.seal()
            with self.assertRaises(helper.RecoveryIndeterminate):
                writer.close()
        finally:
            if not writer._closed:
                os.close(writer.descriptor)
            directory.cleanup()


class FailureAtomicityTests(unittest.TestCase):
    def assert_fail_closed_terminal(self, harness):
        verified = helper.verify_transcript_bytes(
            harness.session.transcript_bytes,
            harness.session.terminal_receipt_bytes,
            harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )
        self.assertEqual(verified.terminal["terminal_status"], "fail_closed")
        if verified.terminal["control_evidence_seal_sha256"] is not None:
            restore = verified.events[-1]["payload"]
            control = helper.verify_control_evidence_bytes(
                harness.session.control_evidence_bytes,
                restore["control_evidence_seal"], harness.binding,
                verified.terminal["session_id"], verified.terminal["journal_id"],
            )
            self.assertIn(
                tuple(row["phase"] for row in control.records),
                (("prior", "restored"), ("prior", "applied", "restored")),
            )
            self.assertEqual(
                control.records[0]["state_sha256"],
                control.records[-1]["state_sha256"],
            )

    def _observed_boundaries(self):
        recorder = FaultOnce()
        harness = SessionHarness(fault=recorder)
        harness.drive()
        harness.finish()
        self.assertIsNone(harness.error)
        return tuple(dict.fromkeys(recorder.seen))

    def test_every_handshake_boundary_fails_before_mutation(self):
        observed = self._observed_boundaries()
        self.assertTrue(set(helper.HANDSHAKE_BOUNDARIES).issubset(observed))
        for boundary in helper.HANDSHAKE_BOUNDARIES:
            with self.subTest(boundary=boundary):
                fault = FaultOnce(boundary)
                harness = SessionHarness(fault=fault)
                harness.drive_tolerant()
                harness.finish()
                self.assertTrue(fault.fired)
                harness.backend.assert_prior(self)
                self.assertFalse(harness.store.active)
                self.assert_fail_closed_terminal(harness)

    def test_every_apply_and_observation_boundary_restores_exact_prior(self):
        observed = self._observed_boundaries()
        boundaries = tuple(item for item in observed if (
            item.startswith("apply.") or item.startswith("observe.")
        ))
        self.assertGreaterEqual(len(boundaries), 28)
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                fault = FaultOnce(boundary)
                harness = SessionHarness(fault=fault)
                harness.drive_tolerant()
                harness.finish()
                self.assertTrue(fault.fired)
                harness.backend.assert_prior(self)
                self.assertFalse(harness.store.active)
                self.assert_fail_closed_terminal(harness)

    def test_every_restore_boundary_reconciles_exact_prior(self):
        observed = self._observed_boundaries()
        boundaries = tuple(item for item in observed if item.startswith("restore."))
        self.assertGreaterEqual(len(boundaries), 19)
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                fault = FaultOnce(boundary)
                harness = SessionHarness(fault=fault)
                harness.drive_tolerant()
                harness.finish()
                self.assertTrue(fault.fired)
                harness.backend.assert_prior(self)
                self.assertFalse(harness.store.active)
                self.assert_fail_closed_terminal(harness)

    def test_synthetic_peer_death_at_every_observed_boundary_is_fail_closed(self):
        for boundary in self._observed_boundaries():
            with self.subTest(boundary=boundary):
                fault = DeathOnce(boundary)
                harness = SessionHarness(fault=fault)
                harness.drive_tolerant()
                harness.finish()
                self.assertTrue(fault.fired)
                self.assertIsInstance(harness.error, helper.PeerClosed)
                harness.backend.assert_prior(self)
                self.assertFalse(harness.store.active)
                self.assert_fail_closed_terminal(harness)

    def test_continuous_descendant_population_drift_fails_and_restores(self):
        harness = SessionHarness()
        harness.drive_tolerant = None
        try:
            with self.assertRaises((helper.PeerClosed, OSError)):
                harness.drive(after_pre=lambda item: setattr(
                    item.backend, "population_drift", True,
                ))
        finally:
            harness.client.close()
            harness.thread.join(4.0)
            harness.guardian_directory.cleanup()
        self.assertIsInstance(harness.error, helper.PrivilegedHelperError)
        harness.backend.population_drift = False
        harness.backend.assert_prior(self)

    def test_persistent_restore_failure_is_indeterminate_and_journal_retained(self):
        backend = SyntheticPrivilegedBackend()
        store = MemoryJournalStore()
        harness = SessionHarness(backend=backend, store=store)

        def make_restore_fail(_item):
            backend.persistent_restore_failure = True

        try:
            harness.drive(after_pre=make_restore_fail)
        except (helper.PrivilegedHelperError, OSError):
            pass
        harness.finish()
        self.assertIsInstance(harness.error, helper.RecoveryIndeterminate)
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        self.assertEqual(terminal["terminal_status"], "indeterminate")
        self.assertFalse(terminal["restoration_proved"])
        helper.verify_transcript_bytes(
            harness.session.transcript_bytes, harness.session.terminal_receipt_bytes,
            harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )
        self.assertEqual(len(store.active), 1)
        backend.persistent_restore_failure = False
        result = helper.recover_pending(harness.binding, backend, store)
        self.assertEqual(len(result), 1)
        backend.assert_prior(self)
        self.assertFalse(store.active)

    def test_population_stop_failure_is_never_retried_in_one_session(self):
        backend = SyntheticPrivilegedBackend()
        backend.population_stop_failure = True
        store = MemoryJournalStore()
        harness = SessionHarness(backend=backend, store=store)
        harness.drive_tolerant()
        harness.finish()
        self.assertIsInstance(harness.error, helper.RecoveryIndeterminate)
        self.assertEqual(backend.population_stop_calls, 1)
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        self.assertEqual(terminal["terminal_status"], "indeterminate")
        self.assertEqual(terminal["population_stop_count"], 1)
        self.assertEqual(len(store.active), 1)
        backend.population_stop_failure = False
        helper.recover_pending(harness.binding, backend, store)
        backend.assert_prior(self)

    def test_control_stream_partial_write_retains_recoverable_journal(self):
        backend = SyntheticPrivilegedBackend()
        store = MemoryJournalStore()
        harness = SessionHarness(backend=backend, store=store)
        original_write = os.write
        control_writes = []

        def partial_then_zero(descriptor, payload):
            if descriptor != harness.control_writer.descriptor:
                return original_write(descriptor, payload)
            control_writes.append(len(payload))
            if len(control_writes) == 1:
                return original_write(descriptor, payload[:13])
            return 0

        with mock.patch.object(helper.os, "write", side_effect=partial_then_zero):
            harness.drive_tolerant()
        harness.finish()
        self.assertGreaterEqual(len(control_writes), 2)
        self.assertIsInstance(harness.error, helper.RecoveryIndeterminate)
        backend.assert_prior(self)
        self.assertEqual(len(store.active), 1)
        helper.recover_pending(harness.binding, backend, store)
        backend.assert_prior(self)
        self.assertFalse(store.active)

    def test_descriptor_close_failure_is_never_suppressed(self):
        backend = SyntheticPrivilegedBackend()
        backend.close_failure = True
        harness = SessionHarness(backend=backend)
        harness.drive()
        harness.finish()
        self.assertIsInstance(harness.error, helper.RecoveryIndeterminate)
        self.assertIn("descriptor closure", str(harness.error))
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        self.assertEqual(terminal["terminal_status"], "indeterminate")
        self.assertFalse(terminal["descriptor_closure"]["passed"])
        backend.assert_prior(self)

    def test_multiple_pending_priors_are_rejected_as_ambiguous(self):
        backend = SyntheticPrivilegedBackend()
        store = MemoryJournalStore()
        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        store.active.extend((
            MemoryJournal(store, frozen_binding(), peer, "1" * 64, backend.capture_prior()),
            MemoryJournal(store, frozen_binding(), peer, "2" * 64, backend.capture_prior()),
        ))
        with self.assertRaisesRegex(helper.RecoveryIndeterminate, "multiple pending"):
            helper.recover_pending(frozen_binding(), backend, store)

    def test_pending_recovery_preflight_is_terminally_indeterminate(self):
        backend = SyntheticPrivilegedBackend()
        store = MemoryJournalStore()
        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        store.active.append(MemoryJournal(
            store, frozen_binding(), peer, "1" * 64, backend.capture_prior(),
        ))
        harness = SessionHarness(backend=backend, store=store)
        harness.drive_tolerant()
        harness.finish()
        self.assertIsInstance(harness.error, helper.RecoveryIndeterminate)
        terminal = helper.strict_json_bytes(harness.session.terminal_receipt_bytes)
        self.assertEqual(terminal["terminal_status"], "indeterminate")
        self.assertFalse(terminal["restoration_proved"])
        helper.verify_transcript_bytes(
            harness.session.transcript_bytes, harness.session.terminal_receipt_bytes,
            harness.binding, frozen_run_slots(),
            harness.session.guardian_evidence_bytes, harness.guardian_contract,
            harness.session.control_evidence_bytes,
        )

    @unittest.skipUnless(hasattr(os, "fork"), "Linux fork is required")
    def test_real_peer_process_death_at_every_phase_triggers_external_restore(self):
        phases = ("challenge", "hello", "apply", "pre", "post")
        for phase in phases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir="/tmp") as directory:
                path = os.path.join(directory, "helper.sock")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                listener.bind(path)
                listener.listen(1)
                binding = frozen_binding()
                child = os.fork()
                if child == 0:  # pragma: no cover - parent verifies durable outcome
                    try:
                        client = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                        client.connect(path)
                        challenge = helper.recv_packet(client)
                        if phase == "challenge":
                            os._exit(0)
                        helper.send_packet(client, helper.hello_record(
                            binding, "9" * 64, challenge["challenge_nonce"],
                        ))
                        hello = helper.recv_packet(client)
                        if phase == "hello":
                            os._exit(0)
                        session_id = hello["session_id"]
                        helper.send_packet(client, helper.command_record(
                            "cp2e_privileged_apply", 1, session_id,
                        ))
                        helper.recv_packet(client)
                        if phase == "apply":
                            os._exit(0)
                        helper.send_packet(client, helper.command_record(
                            "cp2e_privileged_observe", 2, session_id,
                            phase="pre", run_index=0,
                        ))
                        helper.recv_packet(client)
                        if phase == "pre":
                            os._exit(0)
                        helper.send_packet(client, helper.command_record(
                            "cp2e_privileged_observe", 3, session_id,
                            phase="post", run_index=0,
                        ))
                        helper.recv_packet(client)
                        os._exit(0)
                    except BaseException:
                        os._exit(31)
                connection, _address = listener.accept()
                listener.close()
                peer = helper.socket_peer_credentials(connection)
                backend = SyntheticPrivilegedBackend()
                store = MemoryJournalStore()
                guardian_contract = frozen_guardian_contract()
                guardian_fd = os.open(
                    os.path.join(directory, "synthetic-guardian.jsonl"),
                    os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                guardian_writer = helper.GuardianEvidenceWriter(
                    guardian_contract, guardian_fd, expected_uid=os.getuid(),
                    retain_bytes_after_close=True,
                )
                control_fd = os.open(
                    os.path.join(directory, "synthetic-control-evidence.jsonl"),
                    os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                    0o600,
                )
                control_writer = helper.ControlEvidenceWriter(
                    control_fd, expected_uid=os.getuid(),
                    retain_bytes_after_close=True,
                )
                session = helper.HelperSession(
                    connection, binding, peer, backend, store,
                    frozen_run_slots(),
                    guardian_contract, guardian_writer, control_writer,
                )
                with self.assertRaises(helper.PeerClosed):
                    session.run()
                connection.close()
                _pid, status = os.waitpid(child, 0)
                self.assertTrue(os.WIFEXITED(status))
                self.assertEqual(os.WEXITSTATUS(status), 0)
                backend.assert_prior(self)
                self.assertFalse(store.active)
                verified = helper.verify_transcript_bytes(
                    session.transcript_bytes, session.terminal_receipt_bytes,
                    binding, frozen_run_slots(),
                    session.guardian_evidence_bytes, guardian_contract,
                    session.control_evidence_bytes,
                )
                self.assertEqual(
                    verified.terminal["terminal_status"], "fail_closed"
                )


class InstalledClosureTests(unittest.TestCase):
    def _fixture(self):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        os.chmod(directory.name, 0o700)
        launcher = os.path.join(directory.name, "launcher")
        core = os.path.join(directory.name, "protocol.py")
        for path, payload in ((launcher, b"launcher\n"), (core, b"core\n")):
            with open(path, "wb") as stream:
                stream.write(payload)
            os.chmod(path, 0o500)
        files = tuple(sorted((
            helper.InstalledFile("launcher", hashlib.sha256(b"launcher\n").hexdigest()),
            helper.InstalledFile("protocol.py", hashlib.sha256(b"core\n").hexdigest()),
        ), key=lambda item: item.relative_path))
        return directory, launcher, core, files

    def test_exact_single_link_nonsymlink_readonly_closure_attests(self):
        directory, _launcher, _core, files = self._fixture()
        try:
            receipt = helper.attest_installed_closure(
                directory.name, files, expected_uid=os.getuid(),
            )
            self.assertEqual(len(receipt["files"]), 2)
        finally:
            directory.cleanup()

    def test_symlink_hardlink_writable_and_wrong_owner_refuse(self):
        cases = ("symlink", "hardlink", "writable", "owner")
        for case in cases:
            with self.subTest(case=case):
                directory, launcher, _core, files = self._fixture()
                try:
                    expected_uid = os.getuid()
                    if case == "symlink":
                        os.rename(launcher, launcher + ".real")
                        os.symlink("launcher.real", launcher)
                    elif case == "hardlink":
                        os.link(launcher, launcher + ".other")
                    elif case == "writable":
                        os.chmod(launcher, 0o720)
                    else:
                        expected_uid = os.getuid() + 1
                    with self.assertRaises((helper.PrivilegedHelperError, OSError)):
                        helper.attest_installed_closure(
                            directory.name, files, expected_uid=expected_uid,
                        )
                finally:
                    directory.cleanup()

    def test_name_substitution_race_is_detected_against_held_inode(self):
        directory, launcher, _core, files = self._fixture()
        fired = []

        def substitute(label):
            if label == "closure.file.0.after_hash" and not fired:
                fired.append(label)
                os.rename(launcher, launcher + ".old")
                with open(launcher, "wb") as stream:
                    stream.write(b"replacement\n")
                os.chmod(launcher, 0o500)

        try:
            with self.assertRaises(helper.PrivilegedHelperError):
                helper.attest_installed_closure(
                    directory.name, files, expected_uid=os.getuid(), boundary=substitute,
                )
            self.assertTrue(fired)
        finally:
            directory.cleanup()

    def test_production_entrypoint_and_data_free_adapter_are_fail_closed(self):
        with self.assertRaisesRegex(
            helper.ProductionHelperUnavailable, "root-owned.*launcher",
        ):
            helper.production_main([])

        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        successful_backend = SyntheticPrivilegedBackend()
        successful_store = MemoryJournalStore()
        passed = helper.run_data_free_reversibility(
            frozen_binding(), peer, successful_backend, successful_store,
        )
        self.assertEqual(passed["transaction_status"], "passed")
        self.assertTrue(passed["formal_execution_locked"])
        self.assertEqual(successful_backend.data_free_feasibility_calls, 1)
        self.assertEqual(
            passed["prior_state_sha256"], passed["restored_state_sha256"],
        )
        successful_backend.assert_prior(self)
        self.assertFalse(successful_store.active)

        failed_backend = SyntheticPrivilegedBackend()
        failed_store = MemoryJournalStore()

        def reject_applied():
            raise helper.PrivilegedHelperError("synthetic applied rejection")

        failed_backend.validate_applied = reject_applied
        failed = helper.run_data_free_reversibility(
            frozen_binding(), peer, failed_backend, failed_store,
        )
        self.assertEqual(
            failed["transaction_status"], "failed_but_exactly_restored",
        )
        self.assertIsNone(failed["applied_state_sha256"])
        failed_backend.assert_prior(self)
        self.assertFalse(failed_store.active)

        indeterminate_backend = SyntheticPrivilegedBackend()
        indeterminate_backend.persistent_restore_failure = True
        indeterminate_store = MemoryJournalStore()
        with self.assertRaises(helper.DataFreeReversibilityIndeterminate):
            helper.run_data_free_reversibility(
                frozen_binding(), peer, indeterminate_backend,
                indeterminate_store,
            )
        self.assertEqual(len(indeterminate_store.active), 1)


class DurableJournalTests(unittest.TestCase):
    def fixture(self, fault=None):
        directory = tempfile.TemporaryDirectory(dir="/tmp")
        os.chmod(directory.name, 0o700)
        store = helper.DurableJournalStore(
            directory.name, expected_uid=os.getuid(), fault_injector=fault,
        )
        peer = helper.PeerCredentials(os.getpid(), os.getuid(), os.getgid())
        prior = {"exact": "prior", "value": 1}
        return directory, store, peer, prior

    def test_create_append_fsync_rename_parent_fsync_and_reload(self):
        directory, store, peer, prior = self.fixture()
        try:
            journal = store.create(frozen_binding(), peer, "1" * 64, prior)
            journal.append("applied_validated", {"state_sha256": "2" * 64})
            journal.close()
            pending = store.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].prior, prior)
            pending[0].complete()
            self.assertFalse(store.pending())
            self.assertEqual(
                len([name for name in os.listdir(directory.name) if name.endswith(".restored")]),
                1,
            )
        finally:
            store.close()
            directory.cleanup()

    def test_large_prior_survives_helper_death_and_recovers_exactly(self):
        directory, store, peer, _prior = self.fixture()
        prior = {
            "large_population": [
                chr(ord("a") + index) * helper.MAX_STRING_CHARACTERS
                for index in range(6)
            ],
            "marker": "exact-prior",
        }
        self.assertGreater(
            len(helper.canonical_control_state_bytes(prior)),
            helper.MAX_PACKET_BYTES,
        )

        class DeathRecoveryBackend:
            def __init__(self):
                self.state = {"marker": "mutated"}

            @staticmethod
            def recover_population_guard():
                return None

            @staticmethod
            def prepare_external_recovery(_prior, journal_peer):
                return {
                    "journal_peer": journal_peer.as_record(),
                    "replacement_control_plane_processes": [],
                    "replacement_peer": journal_peer.as_record(),
                }

            def restore(self, retained_prior, boundary):
                boundary("before_exact_restore")
                self.state = copy.deepcopy(retained_prior)
                boundary("after_exact_restore")

            def validate_restored(self, retained_prior):
                if self.state != retained_prior:
                    raise helper.PrivilegedHelperError("large prior was not restored")
                return copy.deepcopy(self.state)

            def validate_external_recovery(self, retained_prior):
                persistent = self.validate_restored(retained_prior)
                return {
                    "persistent_state": persistent,
                    "persistent_state_sha256": hashlib.sha256(
                        helper.canonical_control_state_bytes(persistent)
                    ).hexdigest(),
                    "prior_state_sha256": hashlib.sha256(
                        helper.canonical_control_state_bytes(retained_prior)
                    ).hexdigest(),
                    "record_type": "cp2e_external_recovery_validation",
                    "schema_version": helper.SCHEMA_VERSION,
                }

        try:
            journal = store.create(frozen_binding(), peer, "1" * 64, prior)
            journal.close()  # synthetic abrupt helper death after durable prior
            pending = store.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].prior, prior)
            pending[0].close()
            backend = DeathRecoveryBackend()
            result = helper.recover_pending(frozen_binding(), backend, store)
            self.assertEqual(len(result), 1)
            self.assertEqual(backend.state, prior)
            self.assertFalse(store.pending())
        finally:
            store.close()
            directory.cleanup()

    def test_every_observed_journal_io_boundary_fault_fails_closed(self):
        recorder = FaultOnce()
        directory, store, peer, prior = self.fixture(recorder)
        try:
            journal = store.create(frozen_binding(), peer, "1" * 64, prior)
            journal.append("applied_validated", None)
            journal.complete()
            boundaries = tuple(dict.fromkeys(recorder.seen))
        finally:
            store.close()
            directory.cleanup()
        self.assertIn("journal.append.before_write", boundaries)
        self.assertIn("journal.append.before_file_fsync", boundaries)
        self.assertIn("journal.complete.before_rename", boundaries)
        self.assertIn("journal.complete.after_rename", boundaries)
        self.assertIn("journal.complete.before_parent_fsync", boundaries)

        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                fault = FaultOnce(boundary)
                directory, store, peer, prior = self.fixture(fault)
                try:
                    try:
                        journal = store.create(frozen_binding(), peer, "1" * 64, prior)
                        journal.append("applied_validated", None)
                        journal.complete()
                    except InjectedFault:
                        pass
                    self.assertTrue(fault.fired)
                    retained = os.listdir(directory.name)
                    if boundary == "journal.create.before_open":
                        self.assertFalse(retained)
                    else:
                        self.assertTrue(retained)
                        self.assertTrue(any(
                            name.endswith(".pending") or name.endswith(".restored")
                            for name in retained
                        ))
                finally:
                    store.close()
                    directory.cleanup()

    def test_completed_name_refuses_unverified_or_injected_journal_bytes(self):
        directory, store, peer, prior = self.fixture()
        try:
            journal = store.create(frozen_binding(), peer, "1" * 64, prior)
            os.write(journal.descriptor, b"{}\n")
            os.fsync(journal.descriptor)
            with self.assertRaises(helper.PrivilegedHelperError):
                journal.complete()
            self.assertTrue(journal.name.endswith(".pending"))
            self.assertFalse(any(
                name.endswith(".restored") for name in os.listdir(directory.name)
            ))
            journal.close()
        finally:
            store.close()
            directory.cleanup()


if __name__ == "__main__":
    unittest.main(verbosity=2)
