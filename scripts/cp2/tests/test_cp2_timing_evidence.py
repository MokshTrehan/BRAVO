#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free protecting tests for the complete CP2-E Python evidence layer."""

from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


CP2_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CP2_DIRECTORY))

import cp2_timing_artifact as artifact  # noqa: E402
import cp2_timing_controls as controls  # noqa: E402
import cp2_timing_math as timing_math  # noqa: E402
import cp2_timing_privileged_helper as helper_protocol  # noqa: E402
import cp2_timing_profile as profile_codec  # noqa: E402
import cp2_schema  # noqa: E402


def canonical(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode() + b"\n"


def valid_profile_value():
    slots = []
    for run_index, mode in enumerate(mode for pair in profile_codec.PAIR_ORDER for mode in pair):
        slots.append({
            "run_index": run_index,
            "timing_pair_index": run_index // 2,
            "position_in_pair": run_index % 2,
            "mode": mode,
        })
    runtime_environment = {
        "LANG": "C", "LC_ALL": "C", "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1", "PATH": "/usr/bin:/bin",
        "VECLIB_MAXIMUM_THREADS": "1",
    }
    command_environment = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}

    def synthetic_sha(label):
        return hashlib.sha256(("cp2e-unmistakably-synthetic-" + label).encode("ascii")).hexdigest()

    cpu_roles = {0: "estimator", 1: "helper", 2: "offline_smt_sibling", 3: "housekeeping"}
    cpu_cores = {0: 0, 1: 1, 2: 0, 3: 1}
    cpu_policies = {0: "policy0", 1: "policy1", 2: "policy0", 3: "policy1"}
    cpu_control_records = []
    for cpu in range(4):
        idle_states = []
        for state_index, state_name in enumerate(("C1", "C2")):
            state_root = "/sys/devices/system/cpu/cpu{}/cpuidle/state{}".format(
                cpu, state_index,
            )
            idle_states.append({
                "state_index": state_index,
                "name": state_name,
                "name_path": state_root + "/name",
                "disable_path": state_root + "/disable",
                "desired_disabled": True,
                "restore_exact": True,
            })
        cpu_control_records.append({
            "cpu_id": cpu,
            "role": cpu_roles[cpu],
            "physical_core_id": cpu_cores[cpu],
            "online_control_kind": (
                "immutable_always_online_no_control_file"
                if cpu == 0 else "sysfs_writable_integer"
            ),
            "online_path": (
                None if cpu == 0
                else "/sys/devices/system/cpu/cpu{}/online".format(cpu)
            ),
            "online_source_identity_sha256": (
                synthetic_sha("cpu{}-online-{}-source".format(
                    cpu, "absence" if cpu == 0 else "control",
                ))
            ),
            "expected_online": cpu != 2,
            "cpufreq_policy_id": cpu_policies[cpu],
            "idle_inventory_sha256": synthetic_sha("cpu{}-idle-inventory".format(cpu)),
            "idle_states": idle_states,
            "restore_exact": True,
        })
    value = {
        "schema_version": 2,
        "record_type": "cp2_timing_profile",
        "checkpoint": "CP2-E",
        "profile_id": "synthetic-schema-v2-fixed-3000",
        "authorization_sha256": synthetic_sha("authorization"),
        "source_identity_policy": "exact_clean_head_bound_by_readiness_and_provenance",
        "target": {
            "target_class": "desktop_x86_64", "architecture": "x86_64",
            "cpu_vendor": "AuthenticAMD", "cpu_family": 26, "cpu_model": 68,
            "cpu_stepping": 0, "cpu_model_name": "Synthetic AMD",
            "logical_cpu_count": 4, "physical_core_count": 2, "threads_per_core": 2,
            "kernel_release": "synthetic", "machine_identity_sha256": "b" * 64,
            "boot_identity_policy": "capture_each_campaign_reject_change",
        },
        "input": {
            "sequence_index": 0, "sequence_id": "synthetic_sequence_zero",
            "frozen_offset_ns": 123_456_789,
            "bag_sha256": synthetic_sha("bag-bytes"),
            "ground_truth_access": False, "registry_read_count": 0,
            "input_kind": "synthetic_protecting",
            "synthetic_origin_sha256": synthetic_sha("fixture-generator-origin"),
        },
        "pairing": {
            "pair_order": [list(pair) for pair in profile_codec.PAIR_ORDER],
            "run_slots": slots, "warmup_ns": 60_000_000_000,
            "common_payload_domain": "SchurVIO-CP2-timing-common-v1\\0",
            "quantile_p50": [1, 2], "quantile_p95": [19, 20],
            "p50_ratio_limit": [11, 10], "p95_ratio_limit": [23, 20],
            "every_pair_must_pass": True,
        },
        "runtime": {
            "cpu_ids": [0], "smt_sibling_groups": [[0, 2], [1, 3]],
            "affinity_policy": "inherited_exact_set_with_continuous_process_tree_audit",
            "environment": runtime_environment, "scaling_driver": "acpi-cpufreq",
            "governor": "performance", "minimum_frequency_khz": 3_000_000,
            "maximum_frequency_khz": 3_000_000,
            "accepted_current_frequency_khz": [3_000_000], "boost_enabled": False,
            "disabled_idle_state_names": ["C1", "C2"],
            "irqbalance_state": "inactive", "dma_latency_us": 0,
        },
        "cpu_plan": {
            "controlled_cpu_ids": [0, 1, 2, 3],
            "estimator_cpu_ids": [0],
            "helper_cpu_ids": [1],
            "housekeeping_cpu_ids": [3],
            "online_cpu_ids": [0, 1, 3],
            "offline_cpu_ids": [2],
            "smt_cores": [
                {
                    "physical_core_id": 0,
                    "sibling_cpu_ids": [0, 2],
                    "online_cpu_ids": [0],
                    "offline_cpu_ids": [2],
                    "sibling_disposition": "estimator_primary_only_other_siblings_offline",
                },
                {
                    "physical_core_id": 1,
                    "sibling_cpu_ids": [1, 3],
                    "online_cpu_ids": [1, 3],
                    "offline_cpu_ids": [],
                    "sibling_disposition": "non_estimator_siblings_online",
                },
            ],
            "topology_source_sha256": synthetic_sha("complete-topology-source"),
            "role_partition_policy": "disjoint_complete_logical_cpu_partition",
            "estimator_smt_policy": "one_estimator_logical_cpu_all_its_other_siblings_offline",
        },
        "privileged_helper": {
            "authority_policy": "sole_privileged_mutation_observation_restoration_authority",
            "plan_sha256": synthetic_sha("privileged-plan"),
            "source_sha256": synthetic_sha("privileged-source"),
            "protocol_core_sha256": synthetic_sha("privileged-protocol-core"),
            "root_launcher_path": "/opt/schurvio-cp2e-synthetic/bin/cp2e-helper",
            "root_launcher_sha256": synthetic_sha("root-launcher"),
            "import_closure_sha256": synthetic_sha("root-import-closure"),
            "sudoers_sha256": synthetic_sha("sudoers-rule"),
            "trusted_root": "/opt/schurvio-cp2e-synthetic",
            "journal_directory": "/var/lib/schurvio-cp2e-synthetic",
            "invocation_argv": [
                "/usr/bin/sudo", "-n", "-C", "4", "--",
                "/opt/schurvio-cp2e-synthetic/bin/cp2e-helper",
            ],
            "helper_cpu_ids": [1],
            "root_owner_uid": 0,
            "socket_family": "AF_UNIX",
            "socket_type": "SOCK_SEQPACKET",
            "peer_credential_policy": "exact_so_peercred_pid_uid_gid",
            "nonce_policy": "fresh_helper_and_client_256_bit_nonces_each_session",
            "client_selection_policy": "client_selects_no_paths_commands_modules_services_registers_or_controls",
            "descriptor_transfer_policy": "no_privileged_descriptor_transfer_to_client",
            "installed_closure_file_count": 4,
            "production_policy": "pre_python_root_owned_single_link_held_source_profile_launcher_closure_required",
        },
        "cpuset": {
            "cgroup_version": "v1_cpuset",
            "hierarchy_identity_sha256": synthetic_sha("cgroup-v1-cpuset-hierarchy"),
            "mount_path": "/sys/fs/cgroup/cpuset",
            "parent_path": "/sys/fs/cgroup/cpuset",
            "group_name": "schurvio-cp2e-synthetic",
            "group_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic",
            "cpus_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic/cpuset.cpus",
            "mems_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic/cpuset.mems",
            "tasks_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic/tasks",
            "effective_cpus_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic/cpuset.effective_cpus",
            "effective_mems_path": "/sys/fs/cgroup/cpuset/schurvio-cp2e-synthetic/cpuset.effective_mems",
            "desired_cpu_ids": [0],
            "desired_memory_nodes": [0],
            "create_policy": "descriptor_relative_exclusive_create_reject_preexisting",
            "membership_policy": "move_exact_peer_all_threads_before_descendant_spawn",
            "effective_mask_policy": "require_exact_cpus_and_mems_before_and_during_each_run",
            "remove_policy": "remove_all_members_then_rmdir_and_verify_prior_absence",
            "lifecycle": list(profile_codec.CPUSET_LIFECYCLE),
            "guardian_poll_interval_ns": 10_000_000,
        },
        "cpu_controls": {
            "application_order": list(profile_codec.CPU_CONTROL_APPLICATION_ORDER),
            "boost": {
                "path": "/sys/devices/system/cpu/cpufreq/boost",
                "source_identity_sha256": synthetic_sha("boost-source"),
                "parser": "integer", "desired_text": "0",
                "desired_enabled": False, "restore_exact": True,
            },
            "policies": [
                {
                    "policy_id": "policy0", "cpu_ids": [0, 2],
                    "source_identity_sha256": synthetic_sha("policy0-source"),
                    "driver": "acpi-cpufreq",
                    "driver_path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_driver",
                    "governor_path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor",
                    "minimum_frequency_path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_min_freq",
                    "maximum_frequency_path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq",
                    "desired_governor": "performance",
                    "desired_minimum_frequency_khz": 3_000_000,
                    "desired_maximum_frequency_khz": 3_000_000,
                    "restore_exact": True,
                },
                {
                    "policy_id": "policy1", "cpu_ids": [1, 3],
                    "source_identity_sha256": synthetic_sha("policy1-source"),
                    "driver": "acpi-cpufreq",
                    "driver_path": "/sys/devices/system/cpu/cpufreq/policy1/scaling_driver",
                    "governor_path": "/sys/devices/system/cpu/cpufreq/policy1/scaling_governor",
                    "minimum_frequency_path": "/sys/devices/system/cpu/cpufreq/policy1/scaling_min_freq",
                    "maximum_frequency_path": "/sys/devices/system/cpu/cpufreq/policy1/scaling_max_freq",
                    "desired_governor": "performance",
                    "desired_minimum_frequency_khz": 3_000_000,
                    "desired_maximum_frequency_khz": 3_000_000,
                    "restore_exact": True,
                },
            ],
            "cpus": cpu_control_records,
        },
        "interrupts": {
            "inventory_sha256": synthetic_sha("complete-irq-inventory"),
            "inventory_policy": "complete_numeric_irq_population_reject_add_remove",
            "default_affinity_path": "/proc/irq/default_smp_affinity",
            "default_affinity_cpu_ids": [3],
            "default_source_identity_sha256": synthetic_sha("default-irq-affinity-source"),
            "irq_records": [{
                "irq": 24,
                "affinity_path": "/proc/irq/24/smp_affinity_list",
                "desired_cpu_ids": [3],
                "source_identity_sha256": synthetic_sha("irq24-source"),
            }],
            "excluded_cpu_ids": [0, 1, 2],
            "irqbalance": {
                "service_name": "irqbalance", "desired_state": "inactive",
                "prior_state_policy": "capture_exact_active_state_before_stop",
                "deactivate_action": "helper_systemctl_stop_irqbalance",
                "activate_action": "helper_systemctl_start_irqbalance_if_preexisting_active",
                "sole_authority_policy": "privileged_helper_only",
                "restore_exact": True,
            },
            "continuous_audit_interval_ns": 1_000_000,
            "new_irq_policy": "latched_failure_before_next_estimator_operation",
        },
        "telemetry_plan": {
            "required_cpu_ids": [0],
            "module_names": ["k10temp", "msr"],
            "aperf_mperf": {
                "cpu_sources": [{
                    "cpu_id": 0, "path": "/dev/cpu/0/msr",
                    "source_kind": "msr_u64_le", "aperf_register": 0xE8,
                    "mperf_register": 0xE7,
                    "source_identity_sha256": synthetic_sha("cpu0-aperf-mperf-source"),
                }],
                "reference_frequency_source": {
                    "path": "/sys/devices/system/cpu/cpu0/cpufreq/base_frequency",
                    "source_kind": "text_integer_khz",
                    "source_identity_sha256": synthetic_sha("mperf-reference-source"),
                },
                "reference_frequency_khz": 3_000_000,
                "target_frequency_khz": 3_000_000,
                "minimum_effective_frequency_khz": 2_970_000,
                "maximum_effective_frequency_khz": 3_030_000,
                "lower_ratio": [99, 100], "upper_ratio": [101, 100],
                "no_wrap_policy": "reject_nonincreasing_u64_counter",
                "maximum_snapshot_skew_ns": 100_000,
                "comparison_policy": "f_low_times_delta_m_le_f_ref_times_delta_a_le_f_high_times_delta_m",
                "endpoint_policy": "inclusive_exact_integer_cross_products",
            },
            "frequency_sources": [{
                "cpu_id": 0,
                "path": "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
                "source_kind": "text_integer_khz",
                "source_identity_sha256": synthetic_sha("cpu0-frequency-source"),
                "expected_khz": 3_000_000,
            }],
            "temperature_source": {
                "module": "k10temp",
                "device_path": "/sys/devices/platform/synthetic-k10temp/hwmon/hwmon-synthetic",
                "name_path": "/sys/devices/platform/synthetic-k10temp/hwmon/hwmon-synthetic/name",
                "name_expected": "k10temp",
                "label_path": "/sys/devices/platform/synthetic-k10temp/hwmon/hwmon-synthetic/temp1_label",
                "label_expected": "Tctl",
                "input_path": "/sys/devices/platform/synthetic-k10temp/hwmon/hwmon-synthetic/temp1_input",
                "source_identity_sha256": synthetic_sha("k10temp-tctl-source"),
                "minimum_millicelsius": 0,
                "maximum_millicelsius": 120_000,
                "sampling_policy": "continuous_helper_read_bound_sensor",
            },
            "throttle_sources": [{
                "cpu_id": 0, "provider": "amd", "source_kind": "text_u64",
                "path": "/sys/devices/platform/synthetic-amd-throttle/cpu0/thermal_throttle_counter",
                "register": None,
                "source_identity_sha256": synthetic_sha("amd-throttle-source"),
                "specification_sha256": synthetic_sha("amd-throttle-specification"),
                "value_extraction": "canonical_ascii_decimal_u64_one_line",
                "counter_width_bits": 64,
                "semantics": "monotonic_thermal_throttle_event_counter",
                "no_wrap_policy": "reject_decrease_or_wrap",
            }],
            "capture_policy": "helper_raw_pre_post_for_each_of_six_runs_plus_continuous_temperature",
            "continuous_temperature_interval_ns": 10_000_000,
        },
        "dma_latency": {
            "path": "/dev/cpu_dma_latency", "desired_latency_us": 0,
            "owner_policy": "privileged_helper_is_sole_descriptor_owner",
            "open_policy": "open_rdwr_cloexec_write_exact_i32_hold_open",
            "descriptor_transfer_policy": "never_transfer_descriptor_to_client",
            "lifetime_policy": "hold_from_validated_apply_through_post_observation",
            "restore_policy": "close_descriptor_on_every_exit_and_verify_helper_closed",
        },
        "guardians": {
            "policy": "independent_helper_continuous_guard_until_post_observation",
            "control_plane_policy": "held_proc_variable_length_peer_child_chain_exact_ppid_start_executable_identity",
            "poll_interval_ns": 10_000_000,
            "maximum_observation_gap_ns": 20_000_000,
            "surfaces": list(profile_codec.GUARDIAN_SURFACES),
            "descendant_policy": "raw_peer_closure_partitioned_into_attested_control_branch_and_estimator_pid_tid_closure",
            "cpuset_policy": "child_tasks_equals_estimator_tids_control_tids_outside_on_disjoint_helper_cpus",
            "control_drift_policy": "latch_any_transient_or_persistent_control_drift",
            "irq_drift_policy": "latch_affinity_population_or_irqbalance_drift",
            "foreign_task_policy": "retain_complete_non_gating_foreign_affinity_eligibility_observations",
            "thermal_policy": "continuous_temperature_and_monotonic_throttle_counter",
            "failure_latch_policy": "irreversible_campaign_failure_restore_without_retry",
            "evidence_policy": "retain_complete_chronological_raw_guardian_receipts",
            "minimum_observation_spacing_ns": 10_000_000,
            "maximum_campaign_duration_ns": 172_800_000_000_000,
            "maximum_observation_count": 17_280_001,
            "evidence_filename": "guardian_evidence.jsonl",
            "evidence_storage_policy": "helper_owned_append_only_held_new_file_no_client_writes",
            "evidence_chain_policy": "canonical_jsonl_previous_record_sha256_chain",
        },
        "restoration": {
            "policy": "exact_captured_prior_state_or_recovery_indeterminate",
            "journal_policy": "durable_prior_before_first_mutation_hash_chain_retain_until_verified",
            "operation_order": list(profile_codec.RESTORE_OPERATION_ORDER),
            "restore_triggers": list(profile_codec.RESTORE_TRIGGERS),
            "verification_policy": "typed_semantic_and_raw_byte_exact_reconstruction",
            "cpuset_cleanup_policy": "membership_empty_group_removed_prior_parent_exact",
            "parent_fsync_policy": "fsync_after_create_rename_remove_and_journal_completion",
            "indeterminate_policy": "stop_retain_journal_publish_no_passing_evidence",
        },
        "controls": {
            "policy": "capture_apply_validate_restore",
            "noninteractive_sudo_argv": ["/usr/bin/sudo", "-n"],
            "module_actions": [
                {
                    "name": "k10temp",
                    "modprobe_argv": ["/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "--", "k10temp"],
                    "remove_argv": ["/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "-r", "--", "k10temp"],
                    "retain_if_preexisting": True,
                },
                {
                    "name": "msr",
                    "modprobe_argv": ["/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "--", "msr"],
                    "remove_argv": ["/usr/bin/sudo", "-n", "/usr/sbin/modprobe", "-r", "--", "msr"],
                    "retain_if_preexisting": True,
                },
            ],
            "service_actions": [{
                "name": "irqbalance",
                "deactivate_argv": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "irqbalance"],
                "activate_argv": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "start", "irqbalance"],
                "retain_if_inactive": True,
            }],
            "writes": [
                {"order": 0, "path": "/sys/devices/system/cpu/cpufreq/boost", "desired_text": "0", "parser": "integer", "restore_exact": True},
                {"order": 1, "path": "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "desired_text": "performance", "parser": "text", "restore_exact": True},
            ],
            "restore_on_every_exit": True, "recovery_journal_parent": "/tmp",
            "formal_authority_policy": "privileged_helper_is_sole_mutation_observation_restoration_authority",
            "legacy_backend_policy": "disabled_for_formal_schema_v2_compatibility_tests_only",
        },
        "observations": [
            {
                "observation_id": "boost",
                "argv": ["/usr/bin/head", "-n", "1", "/sys/devices/system/cpu/cpufreq/boost"],
                "environment": command_environment, "cwd": "/tmp",
                "timeout_ns": 1_000_000_000, "maximum_stdout_bytes": 4096,
                "parser": "integer", "expected": 0,
            },
            {
                "observation_id": "frequency_cpu_0",
                "argv": ["/usr/bin/head", "-n", "1", "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"],
                "environment": command_environment, "cwd": "/tmp",
                "timeout_ns": 1_000_000_000, "maximum_stdout_bytes": 4096,
                "parser": "integer", "expected": 3_000_000,
            },
            {
                "observation_id": "temperature_millicelsius",
                "argv": [
                    "/usr/bin/head", "-n", "1",
                    "/sys/devices/platform/synthetic-k10temp/hwmon/hwmon-synthetic/temp1_input",
                ],
                "environment": command_environment, "cwd": "/tmp",
                "timeout_ns": 1_000_000_000, "maximum_stdout_bytes": 4096,
                "parser": "hwmon_temperature_millicelsius",
                "expected": {"minimum": 0, "maximum": 120_000},
            },
        ],
        "limits": {
            "maximum_profile_bytes": 1_048_576, "maximum_jsonl_line_bytes": 1_048_576,
            "maximum_trace_bytes": 262_144, "maximum_command_output_bytes": 4096,
            "maximum_samples_per_run": 1_000_000, "maximum_process_seconds": 7200,
            "maximum_run_count": 6, "maximum_pair_count": 3,
            "maximum_guardian_population_items": 2048,
            "maximum_guardian_line_bytes": 8192,
            "maximum_guardian_evidence_bytes": 206_158_430_208,
            "maximum_artifact_bytes": 274_877_906_944,
        },
        "artifact": {
            "schema_version": 1, "report_filename": "cp2_report.json",
            "provenance_filename": "provenance.json", "commands_filename": "commands.jsonl",
            "timing_samples_filename": "timing_samples.jsonl", "manifest_filename": "SHA256SUMS",
            "publication_parent": "/tmp/cp2-timing", "publication_policy": "descriptor_bound_noreplace_fsync",
        },
    }
    guardian_line_bytes = profile_codec.guardian_line_capacity_bytes(
        len(value["cpu_plan"]["controlled_cpu_ids"]),
        len(profile_codec.GUARDIAN_SURFACES),
        len(value["telemetry_plan"]["throttle_sources"]) + 1,
    )
    guardian_evidence_bytes = (
        guardian_line_bytes
        * value["guardians"]["maximum_observation_count"]
    )
    value["limits"].update({
        "maximum_jsonl_line_bytes": guardian_line_bytes,
        "maximum_guardian_line_bytes": guardian_line_bytes,
        "maximum_guardian_evidence_bytes": guardian_evidence_bytes,
        "maximum_artifact_bytes": guardian_evidence_bytes + (1 << 30),
    })
    value["privileged_helper"]["plan_sha256"] = (
        profile_codec.profile_plan_sha256(value)
    )
    return value


def frozen_profile():
    return profile_codec.load_profile_bytes(profile_codec.canonical_profile_bytes(valid_profile_value()))


class SyntheticBackend:
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
        self.fail_post_write = None
        self.fail_restore_path = None
        self.fail_post_module_load = False

    def require_privilege(self):
        if not self.privilege:
            raise controls.TimingControlError("no privilege")

    def read_text(self, path):
        return self.values[path]

    def write_text(self, path, value):
        self.values[path] = value
        if self.fail_post_write == path:
            self.fail_post_write = None
            raise controls.TimingControlError("post-write failure")
        if self.fail_restore_path == path and value in ("1", "ondemand"):
            raise controls.TimingControlError("restore failure")

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
                if self.fail_post_module_load:
                    self.fail_post_module_load = False
                    raise controls.TimingControlError("post-module-load failure")
        elif "stop" in argv:
            self.services.discard(name)
        elif "start" in argv:
            self.services.add(name)

    def affinity(self):
        return self.cpus

    def set_affinity(self, cpus):
        self.cpus = tuple(cpus)

    def hold_dma_latency(self, value):
        self.dma = True

    def release_dma_latency(self):
        self.dma = False


class ProfileCodecTests(unittest.TestCase):
    def assert_profile_rejects(self, mutation, pattern=None):
        value = valid_profile_value()
        prior_plan = value["privileged_helper"]["plan_sha256"]
        mutation(value)
        if value["privileged_helper"]["plan_sha256"] == prior_plan:
            value["privileged_helper"]["plan_sha256"] = (
                profile_codec.profile_plan_sha256(value)
            )
        context = (
            self.assertRaisesRegex(profile_codec.TimingProfileError, pattern)
            if pattern is not None else self.assertRaises(profile_codec.TimingProfileError)
        )
        with context:
            profile_codec.canonical_profile_bytes(value)

    def test_valid_profile_is_canonical_and_hash_bound(self):
        frozen = frozen_profile()
        self.assertEqual(frozen.sha256, hashlib.sha256(frozen.canonical_bytes).hexdigest())
        self.assertTrue(frozen.canonical_bytes.endswith(b"\n"))
        self.assertEqual(frozen.value["schema_version"], 2)
        self.assertEqual(frozen.value["input"]["input_kind"], "synthetic_protecting")
        self.assertTrue(frozen.value["input"]["sequence_id"].startswith("synthetic_"))

    def test_duplicate_noncanonical_and_float_profiles_reject(self):
        good = frozen_profile().canonical_bytes
        with self.assertRaises(profile_codec.TimingProfileError):
            profile_codec.load_profile_bytes(good.replace(b'"checkpoint":"CP2-E"', b'"checkpoint":"CP2-E","checkpoint":"CP2-E"'))
        with self.assertRaises(profile_codec.TimingProfileError):
            profile_codec.load_profile_bytes(b" " + good)
        with self.assertRaises(profile_codec.TimingProfileError):
            profile_codec.load_profile_bytes(good.replace(b'"warmup_ns":60000000000', b'"warmup_ns":60000000000.0'))

    def test_dynamic_governor_boost_and_wrong_pair_order_reject(self):
        for mutate in (
            lambda value: value["runtime"].__setitem__("governor", "ondemand"),
            lambda value: value["runtime"].__setitem__("boost_enabled", True),
            lambda value: value["pairing"]["pair_order"].__setitem__(0, ["schur", "nullspace"]),
        ):
            value = valid_profile_value()
            mutate(value)
            with self.assertRaises(profile_codec.TimingProfileError):
                profile_codec.canonical_profile_bytes(value)

    def test_accepted_frequency_must_be_exact_frozen_singleton(self):
        for frequencies in ([2_999_999], [3_000_000, 3_000_001]):
            value = valid_profile_value()
            value["runtime"]["accepted_current_frequency_khz"] = frequencies
            with self.assertRaisesRegex(
                profile_codec.TimingProfileError, "singleton frozen"
            ):
                profile_codec.canonical_profile_bytes(value)

    def test_shell_relative_command_and_unknown_control_path_reject(self):
        values = []
        first = valid_profile_value()
        first["observations"][0]["argv"] = ["/bin/sh", "-c", "true"]
        values.append(first)
        second = valid_profile_value()
        second["observations"][0]["argv"] = ["head", "-n", "1"]
        values.append(second)
        third = valid_profile_value()
        third["controls"]["writes"][0]["path"] = "/etc/forbidden"
        values.append(third)
        for value in values:
            with self.assertRaises(profile_codec.TimingProfileError):
                profile_codec.canonical_profile_bytes(value)

    def test_schema_v2_cpu_roles_and_smt_disposition_are_complete_and_disjoint(self):
        cases = {
            "legacy schema": lambda value: value.__setitem__("schema_version", 1),
            "two estimator CPUs": lambda value: value["cpu_plan"].__setitem__(
                "estimator_cpu_ids", [0, 1]
            ),
            "runtime includes helper CPU": lambda value: value["runtime"].__setitem__(
                "cpu_ids", [0, 1]
            ),
            "runtime SMT inventory omits a core": lambda value: value["runtime"].__setitem__(
                "smt_sibling_groups", [[0, 2]]
            ),
            "helper overlap": lambda value: value["cpu_plan"].__setitem__(
                "helper_cpu_ids", [0, 1]
            ),
            "missing controlled CPU": lambda value: value["cpu_plan"].__setitem__(
                "controlled_cpu_ids", [0, 1, 2]
            ),
            "online offline overlap": lambda value: value["cpu_plan"].__setitem__(
                "online_cpu_ids", [0, 1, 2, 3]
            ),
            "estimator sibling online": lambda value: value["cpu_plan"]["smt_cores"][0].__setitem__(
                "online_cpu_ids", [0, 2]
            ),
            "wrong sibling disposition": lambda value: value["cpu_plan"]["smt_cores"][0].__setitem__(
                "sibling_disposition", "non_estimator_siblings_online"
            ),
            "reordered runtime SMT groups": lambda value: value["runtime"]["smt_sibling_groups"].reverse(),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

    def test_cpuset_and_typed_cpu_control_populations_are_exact(self):
        cases = {
            "wrong cpuset CPUs": lambda value: value["cpuset"].__setitem__(
                "desired_cpu_ids", [1]
            ),
            "wrong cgroup version": lambda value: value["cpuset"].__setitem__(
                "cgroup_version", "v2_unified"
            ),
            "invalid cgroup hierarchy identity": lambda value: value["cpuset"].__setitem__(
                "hierarchy_identity_sha256", "0" * 63
            ),
            "wrong effective path": lambda value: value["cpuset"].__setitem__(
                "effective_cpus_path", value["cpuset"]["group_path"] + "/cpuset.cpus"
            ),
            "missing lifecycle boundary": lambda value: value["cpuset"]["lifecycle"].pop(),
            "missing CPU control": lambda value: value["cpu_controls"]["cpus"].pop(),
            "wrong online state": lambda value: value["cpu_controls"]["cpus"][2].__setitem__(
                "expected_online", True
            ),
            "immutable CPU names a control file": lambda value: value["cpu_controls"]["cpus"][0].__setitem__(
                "online_path", "/sys/devices/system/cpu/cpu0/online"
            ),
            "immutable CPU lacks an absence identity": lambda value: value["cpu_controls"]["cpus"][0].__setitem__(
                "online_source_identity_sha256", None
            ),
            "writable CPU omits its control file": lambda value: value["cpu_controls"]["cpus"][1].__setitem__(
                "online_path", None
            ),
            "offline CPU marked immutable": lambda value: value["cpu_controls"]["cpus"][2].update({
                "online_control_kind": "immutable_always_online_no_control_file",
                "online_path": None,
            }),
            "wrong policy join": lambda value: value["cpu_controls"]["cpus"][0].__setitem__(
                "cpufreq_policy_id", "policy1"
            ),
            "empty idle inventory": lambda value: value["cpu_controls"]["cpus"][0].__setitem__(
                "idle_states", []
            ),
            "incomplete policy population": lambda value: value["cpu_controls"]["policies"][0].__setitem__(
                "cpu_ids", [0]
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

    def test_interrupt_inventory_and_irqbalance_are_complete_and_isolated(self):
        def append_wrong_default_parser(value):
            value["controls"]["writes"].append({
                "order": 2,
                "path": "/proc/irq/default_smp_affinity",
                "desired_text": "3", "parser": "cpu_list",
                "restore_exact": True,
            })

        def append_mask_parser_on_list_surface(value):
            value["controls"]["writes"].append({
                "order": 2,
                "path": "/proc/irq/24/smp_affinity_list",
                "desired_text": "00000008", "parser": "cpu_mask",
                "restore_exact": True,
            })

        def append_malformed_default_mask(value):
            value["controls"]["writes"].append({
                "order": 2,
                "path": "/proc/irq/default_smp_affinity",
                "desired_text": "3", "parser": "cpu_mask",
                "restore_exact": True,
            })

        cases = {
            "empty IRQ inventory": lambda value: value["interrupts"].__setitem__(
                "irq_records", []
            ),
            "IRQ on estimator": lambda value: value["interrupts"]["irq_records"][0].__setitem__(
                "desired_cpu_ids", [0]
            ),
            "default IRQ on helper": lambda value: value["interrupts"].__setitem__(
                "default_affinity_cpu_ids", [1]
            ),
            "wrong excluded set": lambda value: value["interrupts"].__setitem__(
                "excluded_cpu_ids", [0, 1]
            ),
            "irqbalance active": lambda value: value["interrupts"]["irqbalance"].__setitem__(
                "desired_state", "active"
            ),
            "permit new IRQ": lambda value: value["interrupts"].__setitem__(
                "new_irq_policy", "accept"
            ),
            "default affinity parsed as CPU list": append_wrong_default_parser,
            "per-IRQ list parsed as CPU mask": append_mask_parser_on_list_surface,
            "malformed default affinity mask": append_malformed_default_mask,
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

    def test_helper_binding_is_digest_complete_and_sole_authority(self):
        cases = {
            "nonsole authority": lambda value: value["privileged_helper"].__setitem__(
                "authority_policy", "shared"
            ),
            "invalid plan digest": lambda value: value["privileged_helper"].__setitem__(
                "plan_sha256", "0" * 63
            ),
            "launcher outside root": lambda value: value["privileged_helper"].__setitem__(
                "root_launcher_path", "/usr/local/bin/cp2e-helper"
            ),
            "generic sudo": lambda value: value["privileged_helper"].__setitem__(
                "invocation_argv", ["/usr/bin/sudo", "-n", "/usr/bin/true"]
            ),
            "helper CPU drift": lambda value: value["privileged_helper"].__setitem__(
                "helper_cpu_ids", [3]
            ),
            "descriptor transfer": lambda value: value["privileged_helper"].__setitem__(
                "descriptor_transfer_policy", "allow_scm_rights"
            ),
            "legacy formal authority": lambda value: value["controls"].__setitem__(
                "legacy_backend_policy", "enabled"
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

    def test_telemetry_sources_and_aperf_mperf_integer_contract_are_bound(self):
        cases = {
            "wrong APERF register": lambda value: value["telemetry_plan"]["aperf_mperf"]["cpu_sources"][0].__setitem__(
                "aperf_register", 0xE9
            ),
            "unbound ratio": lambda value: value["telemetry_plan"]["aperf_mperf"].__setitem__(
                "lower_ratio", [1, 1]
            ),
            "target outside bounds": lambda value: value["telemetry_plan"]["aperf_mperf"].__setitem__(
                "minimum_effective_frequency_khz", 3_000_001
            ),
            "zero skew": lambda value: value["telemetry_plan"]["aperf_mperf"].__setitem__(
                "maximum_snapshot_skew_ns", 0
            ),
            "allow wrap": lambda value: value["telemetry_plan"]["aperf_mperf"].__setitem__(
                "no_wrap_policy", "modular"
            ),
            "wrong frequency CPU": lambda value: value["telemetry_plan"]["frequency_sources"][0].__setitem__(
                "cpu_id", 1
            ),
            "wrong k10temp name": lambda value: value["telemetry_plan"]["temperature_source"].__setitem__(
                "name_expected", "acpitz"
            ),
            "mismatched label input": lambda value: value["telemetry_plan"]["temperature_source"].__setitem__(
                "input_path", value["telemetry_plan"]["temperature_source"]["device_path"] + "/temp2_input"
            ),
            "non-AMD throttle": lambda value: value["telemetry_plan"]["throttle_sources"][0].__setitem__(
                "provider", "generic"
            ),
            "throttle CPU path mismatch": lambda value: value["telemetry_plan"]["throttle_sources"][0].__setitem__(
                "path", "/sys/devices/platform/synthetic-amd-throttle/cpu1/thermal_throttle_counter"
            ),
            "unbound throttle specification": lambda value: value["telemetry_plan"]["throttle_sources"][0].__setitem__(
                "specification_sha256", "0" * 63
            ),
            "wrong throttle extraction": lambda value: value["telemetry_plan"]["throttle_sources"][0].__setitem__(
                "value_extraction", "decimal"
            ),
            "missing throttle CPU": lambda value: value["telemetry_plan"].__setitem__(
                "throttle_sources", []
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

        value = valid_profile_value()
        counters = value["telemetry_plan"]["aperf_mperf"]
        counters["minimum_effective_frequency_khz"] = 2_940_000
        counters["lower_ratio"] = [49, 50]
        value["privileged_helper"]["plan_sha256"] = (
            profile_codec.profile_plan_sha256(value)
        )
        profile_codec.canonical_profile_bytes(value)

    def test_dma_guardian_and_restoration_failure_contract_is_exact(self):
        cases = {
            "client owns DMA": lambda value: value["dma_latency"].__setitem__(
                "owner_policy", "client"
            ),
            "missing guarded surface": lambda value: value["guardians"]["surfaces"].pop(),
            "temperature gap": lambda value: value["telemetry_plan"].__setitem__(
                "continuous_temperature_interval_ns", 20_000_001
            ),
            "retry on drift": lambda value: value["guardians"].__setitem__(
                "failure_latch_policy", "retry"
            ),
            "guardian count drift": lambda value: value["guardians"].__setitem__(
                "maximum_observation_count",
                value["guardians"]["maximum_observation_count"] + 1,
            ),
            "guardian duration drift": lambda value: value["guardians"].__setitem__(
                "maximum_campaign_duration_ns",
                value["guardians"]["maximum_campaign_duration_ns"] + 1,
            ),
            "guardian evidence too small": lambda value: value["limits"].__setitem__(
                "maximum_guardian_evidence_bytes", 1,
            ),
            "guardian spacing drift": lambda value: value["guardians"].__setitem__(
                "minimum_observation_spacing_ns", 1_000_000,
            ),
            "restoration order": lambda value: value["restoration"]["operation_order"].reverse(),
            "missing death trigger": lambda value: value["restoration"]["restore_triggers"].pop(),
            "discard journal": lambda value: value["restoration"].__setitem__(
                "indeterminate_policy", "discard"
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

    def test_synthetic_input_is_unmistakable_and_recorded_variant_is_typed(self):
        cases = {
            "project-like sequence": lambda value: value["input"].__setitem__(
                "sequence_id", "MH_01_easy"
            ),
            "registry read": lambda value: value["input"].__setitem__(
                "registry_read_count", 1
            ),
            "missing origin": lambda value: value["input"].__setitem__(
                "synthetic_origin_sha256", None
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)

        recorded = valid_profile_value()
        recorded["input"].update({
            "input_kind": "recorded_frozen",
            "sequence_id": "formal_sequence_zero",
            "registry_read_count": 1,
            "synthetic_origin_sha256": None,
        })
        recorded["privileged_helper"]["plan_sha256"] = (
            profile_codec.profile_plan_sha256(recorded)
        )
        profile_codec.canonical_profile_bytes(recorded)

    def test_integer_domains_reject_json_booleans(self):
        cases = {
            "registry count": lambda value: value["input"].__setitem__(
                "registry_read_count", False
            ),
            "root owner UID": lambda value: value["privileged_helper"].__setitem__(
                "root_owner_uid", False
            ),
            "physical core ID": lambda value: value["cpu_controls"]["cpus"][0].__setitem__(
                "physical_core_id", False
            ),
            "DMA latency": lambda value: value["dma_latency"].__setitem__(
                "desired_latency_us", False
            ),
            "control write order": lambda value: value["controls"]["writes"][0].__setitem__(
                "order", False
            ),
            "artifact schema": lambda value: value["artifact"].__setitem__(
                "schema_version", True
            ),
        }
        for label, mutation in cases.items():
            with self.subTest(label=label):
                self.assert_profile_rejects(mutation)


class ControlTransactionTests(unittest.TestCase):
    def test_valid_transaction_applies_validates_and_restores_exactly(self):
        backend = SyntheticBackend()
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        snapshot = transaction.apply()
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/boost"], "0")
        self.assertNotIn("irqbalance", backend.services)
        self.assertEqual(backend.cpus, (0,))
        self.assertTrue(backend.dma)
        transaction.validate_applied()
        transaction.restore()
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/boost"], "1")
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"], "ondemand")
        self.assertEqual(backend.modules, {"msr"})
        self.assertEqual(backend.services, {"irqbalance"})
        self.assertEqual(backend.cpus, (0, 1))
        self.assertFalse(backend.dma)
        self.assertIsNone(transaction.journal_path)
        self.assertEqual(len(snapshot.values), 2)

    def test_profile_validation_is_mandatory(self):
        with self.assertRaisesRegex(controls.TimingControlError, "FrozenTimingProfile"):
            controls.HostControlTransaction(valid_profile_value(), SyntheticBackend())

    def test_privilege_failure_precedes_journal_and_mutation(self):
        backend = SyntheticBackend()
        backend.privilege = False
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        with self.assertRaises(controls.TimingControlError):
            transaction.apply()
        self.assertEqual(transaction.state, "new")
        self.assertIsNone(transaction.journal_path)
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/boost"], "1")

    def test_post_write_failure_rolls_back_current_write_and_prior_actions(self):
        backend = SyntheticBackend()
        backend.fail_post_write = "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        with self.assertRaises(controls.TimingControlError):
            transaction.apply()
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/boost"], "1")
        self.assertEqual(backend.values["/sys/devices/system/cpu/cpufreq/policy0/scaling_governor"], "ondemand")
        self.assertEqual(backend.modules, {"msr"})
        self.assertEqual(backend.services, {"irqbalance"})
        self.assertEqual(transaction.state, "restored")

    def test_post_module_command_failure_still_unloads_new_module(self):
        backend = SyntheticBackend()
        backend.fail_post_module_load = True
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        with self.assertRaises(controls.TimingControlError):
            transaction.apply()
        self.assertEqual(backend.modules, {"msr"})
        self.assertEqual(transaction.state, "restored")

    def test_indeterminate_restore_retains_recovery_journal(self):
        backend = SyntheticBackend()
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        transaction.apply()
        journal = transaction.journal_path
        backend.fail_restore_path = "/sys/devices/system/cpu/cpufreq/boost"
        with self.assertRaises(controls.TimingControlIndeterminate):
            transaction.restore()
        self.assertEqual(transaction.state, "indeterminate")
        self.assertIsNotNone(journal)
        self.assertTrue(os.path.exists(journal))
        os.unlink(journal)

    def test_applied_drift_is_rejected(self):
        backend = SyntheticBackend()
        transaction = controls.HostControlTransaction(frozen_profile(), backend)
        transaction.apply()
        backend.values["/sys/devices/system/cpu/cpufreq/boost"] = "1"
        with self.assertRaisesRegex(controls.TimingControlError, "drifted"):
            transaction.validate_applied()
        backend.values["/sys/devices/system/cpu/cpufreq/boost"] = "0"
        transaction.restore()


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def observation_record(specification, value, instant):
    stdout = (str(value) + "\n").encode("ascii")
    stderr = b""
    return {
        "observation_id": specification["observation_id"],
        "argv": specification["argv"], "environment": specification["environment"],
        "cwd": specification["cwd"], "started_monotonic_ns": instant,
        "ended_monotonic_ns": instant + 1, "exit_code": 0, "timed_out": False,
        "stdout_hex": stdout.hex(), "stdout_sha256": sha(stdout),
        "stderr_hex": stderr.hex(), "stderr_sha256": sha(stderr),
        "parsed_value": value,
    }


def phase_interval(run_index, phase):
    base = 10_000 + run_index * 100_000
    start, end = {
        "clock_pre": (0, 1_000),
        "runtime_preflight": (2_000, 2_100),
        "ros_run": (3_000, 4_000),
        "clock_post": (5_000, 6_000),
    }[phase]
    return base + start, base + end


def stable_control_state(profile):
    return {
        "values": [
            {"path": write["path"], "parser": write["parser"],
             "text": write["desired_text"]}
            for write in profile.value["controls"]["writes"]
        ],
        "modules_loaded": ["k10temp", "msr"], "services_active": [],
        "process_affinity": [0], "dma_latency_us": 0,
    }


def synthetic_counter_values(phase, effective_edge=None):
    if phase == "pre":
        return 1_000, 1_000
    if effective_edge == "lower":
        return 1_990, 2_000
    if effective_edge == "upper":
        return 2_010, 2_000
    return 2_000, 2_000


def clock_snapshot(
    profile, provenance, run_index, phase, command_start=None,
    effective_edge=None,
):
    temperature = 50_000 + (1_000 if phase == "post" else 0)
    values = {"boost": 0, "frequency_cpu_0": 3_000_000,
              "temperature_millicelsius": temperature}
    if command_start is None:
        command_start = phase_interval(
            run_index, "clock_pre" if phase == "pre" else "clock_post",
        )[0]
    base = command_start + 100
    observations = [
        observation_record(specification, values[specification["observation_id"]], base + index * 10)
        for index, specification in enumerate(profile.value["observations"])
    ]
    aperf, mperf = synthetic_counter_values(phase, effective_edge)
    applied_state = stable_control_state(profile)
    return canonical({
        "schema_version": 1, "record_type": "cp2_timing_clock_snapshot",
        "checkpoint": "CP2-E", "profile_sha256": profile.sha256,
        "run_index": run_index, "phase": phase,
        "captured_monotonic_ns": base + 100,
        "boot_id_sha256": provenance["boot_id_sha256"],
        "control_applied_sha256": provenance["control_applied_sha256"],
        "stable_control_state": applied_state,
        "stable_control_state_sha256": sha(canonical(applied_state)),
        "observations": observations,
        "telemetry": {
            "temperature_sensor_id": "temperature_millicelsius",
            "temperature_millicelsius": temperature,
            "cpu_counters": [
                {
                    "cpu_id": cpu, "thermal_throttle_count": 0,
                    "aperf": aperf + cpu, "mperf": mperf + cpu,
                    "reference_frequency_khz": 3_000_000,
                    "scaling_current_frequency_khz": 3_000_000,
                }
                for cpu in (0,)
            ],
        },
        "passed": True,
    })


def execution_binding_record(profile):
    return {
        "schema_version": 1, "record_type": "cp2_timing_execution_binding",
        "checkpoint": "CP2-E", "profile_sha256": profile.sha256,
        "source_commit": "b" * 40, "source_tree": "c" * 40,
        "unit_manifest_sha256": "d" * 64,
        "sequence_set_manifest_sha256": "e" * 64,
        "executable_path": "/usr/bin/true", "executable_sha256": "1" * 64,
        "dso_closure_sha256": "2" * 64, "configuration_sha256": "3" * 64,
        "boot_id_sha256": "5" * 64,
        "machine_identity_sha256": profile.value["target"]["machine_identity_sha256"],
        "cwd": "/tmp", "environment": profile.value["runtime"]["environment"],
        "phase_templates": {
            "clock_pre": ["/usr/bin/true", "--profile", "{profile_sha256}",
                          "--run", "{run_index}"],
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
            "clock_post": ["/usr/bin/true", "--profile", "{profile_sha256}",
                           "--run", "{run_index}"],
        },
    }


def render_binding(binding, phase, run_index, mode, run_id, trace_directory):
    values = {
        "{profile_sha256}": binding["profile_sha256"],
        "{run_index}": str(run_index),
        "{timing_pair_index}": str(run_index // 2), "{mode}": mode,
        "{run_id}": run_id, "{trace_directory}": trace_directory,
        "{sequence_index}": "0", "{sequence_id}": "synthetic_sequence_zero",
    }
    return [values.get(item, item) for item in binding["phase_templates"][phase]]


def control_receipt(profile_sha256, phase, instant, state):
    return canonical({
        "schema_version": 1,
        "record_type": "cp2_timing_control_state_receipt",
        "profile_sha256": profile_sha256, "phase": phase,
        "captured_monotonic_ns": instant, "state": state,
        "state_sha256": sha(canonical(state)), "passed": True,
    })


def raw_telemetry_snapshot(
    run_index, phase, command_start=None, effective_edge=None,
):
    if command_start is None:
        command_start = phase_interval(
            run_index, "clock_pre" if phase == "pre" else "clock_post",
        )[0]
    aperf, mperf = synthetic_counter_values(phase, effective_edge)
    temperature = 50_000 if phase == "pre" else 51_000
    specifications = []
    for cpu in (0,):
        specifications.extend((
            controls.RawTelemetrySpec(
                "cpu{}.aperf".format(cpu), "aperf", cpu, "msr_u64_le",
                "/dev/cpu/{}/msr".format(cpu), 0xE8,
            ),
            controls.RawTelemetrySpec(
                "cpu{}.frequency".format(cpu),
                "scaling_current_frequency_khz", cpu, "text_integer",
                "/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq".format(cpu),
                None,
            ),
            controls.RawTelemetrySpec(
                "cpu{}.mperf".format(cpu), "mperf", cpu, "msr_u64_le",
                "/dev/cpu/{}/msr".format(cpu), 0xE7,
            ),
            controls.RawTelemetrySpec(
                "cpu{}.throttle".format(cpu), "thermal_throttle_count", cpu,
                "text_integer",
                "/sys/devices/system/cpu/cpu{}/thermal_throttle/core_throttle_count".format(cpu),
                None,
            ),
        ))
    specifications.append(controls.RawTelemetrySpec(
        "package.temperature", "temperature_millicelsius", None,
        "text_integer", "/sys/class/hwmon/hwmon0/temp1_input", None,
    ))
    receipts = []
    for index, specification in enumerate(sorted(
        specifications, key=lambda item: item.metric_id,
    )):
        if specification.role in ("aperf", "mperf"):
            value = (
                aperf if specification.role == "aperf" else mperf
            ) + specification.cpu_id
            raw = value.to_bytes(8, "little")
        elif specification.role == "scaling_current_frequency_khz":
            value, raw = 3_000_000, b"3000000\n"
        elif specification.role == "thermal_throttle_count":
            value, raw = 0, b"0\n"
        else:
            value = temperature
            raw = (str(value) + "\n").encode("ascii")
        started = command_start + 300 + index * 10
        receipts.append(controls.RawTelemetryReceipt(
            specification, phase, started, started + 1, raw, value,
        ))
    return controls.RawTelemetrySnapshot(phase, tuple(receipts))


def helper_evidence(profile, prior_state, applied_state, effective_edge=None):
    binding = artifact._helper_binding(profile)
    binding_digest = sha(canonical(binding))
    peer = {"gid": 1000, "pid": 4242, "uid": 1000}
    challenge_nonce = "1" * 64
    client_nonce = "2" * 64
    nonce_sha = sha(bytes.fromhex(challenge_nonce) + bytes.fromhex(client_nonce))
    session_id = sha(canonical({
        "binding_digest": binding_digest,
        "challenge_nonce": challenge_nonce,
        "client_nonce": client_nonce,
        "peer": peer,
    }))
    journal_id = "3" * 32
    journal_sha = sha(canonical({
        "journal_id": journal_id, "session_id": session_id,
        "terminal": "restoration_validated",
    }))
    surfaces = profile.value["guardians"]["surfaces"]
    final_snapshot_end = phase_interval(5, "clock_post")[0] + 500
    source_specs = artifact._helper_source_specifications(profile)

    def source_row(source_id, value, started):
        specification = source_specs[source_id]
        extraction = specification["extraction"]
        raw = (
            value.to_bytes(8, "little")
            if extraction == "full_u64_little_endian_pread"
            else (str(value) + "\n").encode("ascii")
        )
        return {
            "cpu_id": specification["cpu_id"],
            "extraction": extraction,
            "measurement_kind": specification["measurement_kind"],
            "parsed_value": value,
            "raw_hex": raw.hex(),
            "raw_sha256": sha(raw),
            "read_ended_monotonic_ns": started + 1,
            "read_started_monotonic_ns": started,
            "register": specification["register"],
            "source_id": source_id,
            "source_kind": specification["source_kind"],
            "source_path": specification["source_path"],
            "source_spec_sha256": specification["source_spec_sha256"],
        }

    guardian_sources = []
    for index, source_id in enumerate(sorted(
        source_id for source_id, specification in source_specs.items()
        if specification["measurement_kind"] in ("temperature", "amd_throttle")
    )):
        value = 50_000 if source_id == "shared.temperature" else 0
        guardian_sources.append(source_row(source_id, value, 5_100 + index * 10))
    control_pid = peer["pid"] + 1
    foreign_rows = [{
        "effective_affinity_cpu_ids": [0],
        "process_start_time_ticks": 1,
        "tid": 13,
    }, {
        "effective_affinity_cpu_ids": [0],
        "process_start_time_ticks": 1,
        "tid": 15,
    }]
    scheduler_witness = {
        "child_cpuset_members": [peer["pid"]],
        "complete_control_plane_processes": [{
            "executable_device": 1,
            "executable_inode": 2,
            "parent_pid": peer["pid"],
            "pid": control_pid,
            "process_gid": 0,
            "process_uid": 0,
            "start_time_ticks": 20,
        }],
        "complete_control_plane_threads": [{
            "affinity_cpu_ids": [1],
            "cpuset_membership": "/",
            "process_id": control_pid,
            "start_time_ticks": 20,
            "tid": control_pid,
        }],
        "complete_descendant_population": [peer["pid"]],
        "complete_descendant_threads": [{
            "affinity_cpu_ids": [0],
            "cpuset_membership": "/schurvio-cp2e",
            "process_id": peer["pid"],
            "start_time_ticks": 10,
            "tid": peer["pid"],
        }],
        "complete_descendant_tid_population": [peer["pid"]],
        "foreign_affinity_eligibility": foreign_rows,
    }
    guardian_observation = {
        **scheduler_witness,
        "complete_descendant_population": [peer["pid"]],
        "complete_descendant_tid_population": [peer["pid"]],
        "drift": False,
        "ended_monotonic_ns": final_snapshot_end,
        # Kernel-like singleton rows are deliberately positive evidence: a
        # nonempty foreign eligibility population is observational, not a gate.
        "foreign_affinity_eligibility": foreign_rows,
        "irq_population_sha256": sha(b"synthetic-irq-population\n"),
        "previous_record_sha256": "0" * 64,
        "record_type": "cp2e_guardian_observation",
        "schema_version": 2,
        "scheduler_witness_sha256": sha(canonical(scheduler_witness)),
        "sequence": 0,
        "started_monotonic_ns": 5_000,
        "surface_states": [
            {
                "passed": True,
                "state_sha256": sha(("synthetic-guardian-" + surface).encode()),
                "surface_id": surface,
            }
            for surface in surfaces
        ],
        "thermal_or_throttle_event": False,
        "typed_telemetry": guardian_sources,
    }
    guardian_evidence = canonical(guardian_observation)
    seal = {
        "bound_peer": peer,
        "coverage_ended_monotonic_ns": final_snapshot_end,
        "coverage_started_monotonic_ns": 5_000,
        "drift": False,
        "evidence_sha256": sha(guardian_evidence),
        "evidence_size_bytes": len(guardian_evidence),
        "final_child_cpuset_member_count": 1,
        "final_control_plane_process_count": 1,
        "final_control_plane_tid_count": 1,
        "final_descendant_process_count": 1,
        "final_descendant_tid_count": 1,
        "final_foreign_affinity_eligibility_count": len(foreign_rows),
        "final_foreign_affinity_eligibility_sha256": sha(canonical({
            "rows": foreign_rows,
        })),
        "final_scheduler_witness_sha256": guardian_observation[
            "scheduler_witness_sha256"
        ],
        "first_record_sha256": sha(guardian_evidence),
        "irq_population_changed": False,
        "last_record_sha256": sha(guardian_evidence),
        "maximum_observation_gap_ns": profile.value["guardians"]["maximum_observation_gap_ns"],
        "observation_count": 1,
        "required_surfaces": surfaces,
        "schema_version": 2,
        "session_id": session_id,
        "thermal_or_throttle_event": False,
    }
    seal_sha = sha(canonical(seal))

    def receipt(run_index, phase):
        command_phase = "clock_pre" if phase == "pre" else "clock_post"
        command_start = phase_interval(run_index, command_phase)[0]
        snapshot_start = command_start + 250
        snapshot_end = command_start + 500
        aperf, mperf = synthetic_counter_values(phase, effective_edge)
        temperature = 50_000 if phase == "pre" else 51_000
        sources = []
        for index, source_id in enumerate(sorted(source_specs)):
            specification = source_specs[source_id]
            kind = specification["measurement_kind"]
            if kind in ("aperf", "mperf"):
                value = aperf if kind == "aperf" else mperf
            elif kind in ("scaling_cur_freq", "f_ref"):
                value = 3_000_000
            elif kind == "amd_throttle":
                value = 0
            else:
                value = temperature
            started = command_start + 300 + index * 10
            sources.append(source_row(source_id, value, started))
        return {
            "phase": phase,
            "run_index": run_index,
            "schema_version": 2,
            "snapshot_ended_monotonic_ns": snapshot_end,
            "snapshot_skew_ns": snapshot_end - snapshot_start,
            "snapshot_started_monotonic_ns": snapshot_start,
            "sources": sources,
        }

    population_binding = {
        "peer": peer, "session_id": session_id,
        "policy": "synthetic-complete-descendant-guardian",
    }
    control_lines = []
    control_receipts = []
    control_previous = "0" * 64
    control_size = 0
    control_digest = hashlib.sha256()
    for sequence, (phase, state) in enumerate((
        ("prior", prior_state), ("applied", applied_state),
        ("restored", prior_state),
    )):
        state_sha = sha(canonical(state))
        line = canonical({
            "binding_digest": binding_digest, "journal_id": journal_id,
            "phase": phase, "previous_record_sha256": control_previous,
            "record_type": "cp2e_control_state", "schema_version": 2,
            "sequence": sequence, "session_id": session_id,
            "state": state, "state_sha256": state_sha,
        })
        control_lines.append(line)
        control_digest.update(line)
        control_size += len(line)
        control_previous = sha(line)
        control_receipts.append({
            "end_offset_bytes": control_size, "phase": phase,
            "record_sha256": control_previous, "sequence": sequence,
            "state_sha256": state_sha,
        })
    control_evidence = b"".join(control_lines)
    applied_control_checkpoint = {
        "device": 1, "evidence_sha256": sha(b"".join(control_lines[:2])),
        "evidence_size_bytes": sum(len(item) for item in control_lines[:2]),
        "inode": 2, "last_record_sha256": sha(control_lines[1]),
        "row_count": 2, "rows": control_receipts[:2],
    }
    control_seal = {
        "binding_digest": binding_digest, "device": 1,
        "evidence_sha256": sha(control_evidence),
        "evidence_size_bytes": len(control_evidence),
        "first_record_sha256": sha(control_lines[0]), "inode": 2,
        "journal_id": journal_id,
        "last_record_sha256": sha(control_lines[-1]),
        "phases": ["prior", "applied", "restored"],
        "record_type": "cp2e_control_evidence_terminal",
        "restoration_matches_prior": True, "row_count": 3,
        "rows": control_receipts, "schema_version": 2,
        "session_id": session_id,
    }
    control_seal_sha = sha(canonical(control_seal))
    events = [
        ("challenge", {
            "binding": binding, "binding_digest": binding_digest,
            "challenge_nonce": challenge_nonce, "peer": peer,
        }),
        ("hello", {
            "binding": binding, "challenge_nonce": challenge_nonce,
            "client_nonce": client_nonce, "nonce_sha256": nonce_sha,
            "peer": peer, "session_id": session_id,
        }),
        ("apply", {
            "applied_state_sha256": sha(canonical(applied_state)),
            "control_evidence_checkpoint": applied_control_checkpoint,
            "control_evidence_checkpoint_sha256": sha(canonical(
                applied_control_checkpoint
            )),
            "journal_id": journal_id,
            "population_binding": population_binding,
            "population_binding_sha256": sha(canonical(population_binding)),
            "population_state": {"passed": True, "phase": "applied"},
            "prior_state_sha256": sha(canonical(prior_state)),
        }),
    ]
    for run_index, slot in enumerate(profile.value["pairing"]["run_slots"]):
        for phase in ("pre", "post"):
            raw_receipt = receipt(run_index, phase)
            population = {
                "passed": True, "phase": "run_{}_{}".format(run_index, phase),
                "peer": peer,
            }
            terminal = run_index == 5 and phase == "post"
            events.append(("run_slot_observation", {
                "phase": phase,
                "population": population,
                "population_seal": seal if terminal else None,
                "population_seal_sha256": seal_sha if terminal else None,
                "population_sha256": sha(canonical(population)),
                "receipt": raw_receipt,
                "receipt_sha256": sha(canonical(raw_receipt)),
                "run_slot": slot,
            }))
    events.append(("restore", {
        "abnormal_recovery": False,
        "control_evidence_seal": control_seal,
        "control_evidence_seal_sha256": control_seal_sha,
        "population_seal_sha256": seal_sha,
        "prior_state_sha256": sha(canonical(prior_state)),
        "prior_state_sha256_matches": True,
        "restored_state_sha256": sha(canonical(prior_state)),
        "terminal_journal_sha256": journal_sha,
    }))
    lines = []
    previous = "0" * 64
    for event_index, (event, payload) in enumerate(events):
        line = canonical({
            "event": event,
            "event_index": event_index,
            "payload": payload,
            "previous_record_sha256": previous,
            "record_type": "cp2e_privileged_transcript_event",
            "schema_version": 2,
        })
        lines.append(line)
        previous = sha(line)
    transcript = b"".join(lines)
    terminal = canonical({
        "abnormal_recovery": False,
        "binding": binding,
        "binding_digest": binding_digest,
        "control_evidence_seal_sha256": control_seal_sha,
        "descriptor_closure": {
            "attempted": True, "error_type": None, "passed": True,
        },
        "error_type": None,
        "event_count": 16,
        "first_record_sha256": sha(lines[0]),
        "journal_id": journal_id,
        "last_record_sha256": sha(lines[-1]),
        "population_seal_sha256": seal_sha,
        "population_stop_count": 1,
        "record_type": "cp2e_privileged_transcript_terminal",
        "restoration_proved": True,
        "schema_version": 2,
        "session_id": session_id,
        "terminal_journal_sha256": journal_sha,
        "terminal_status": "success",
        "transcript_sha256": sha(transcript),
        "transcript_size_bytes": len(transcript),
    })
    return transcript, terminal, guardian_evidence, control_evidence, {
        "helper_transcript_sha256": sha(transcript),
        "helper_terminal_receipt_sha256": sha(terminal),
        "helper_binding_digest": binding_digest,
        "helper_terminal_journal_sha256": journal_sha,
        "helper_population_seal_sha256": seal_sha,
        "helper_guardian_evidence_sha256": sha(guardian_evidence),
        "helper_control_evidence_sha256": sha(control_evidence),
        "helper_control_evidence_seal_sha256": control_seal_sha,
    }


def fresh_process_identity(record, profile_sha256, run_index):
    return sha(canonical({
        "domain": "SchurVIO-CP2-E-fresh-process-v1",
        "profile_sha256": profile_sha256, "run_index": run_index,
        "pid": record["pid"], "start_time_ticks": record["start_time_ticks"],
        "executable_sha256": record["executable_sha256"],
        "loader_before_sha256": record["loader_before_sha256"],
        "loader_after_sha256": record["loader_after_sha256"],
    }))


def raw_trace_rows(run_index, mode, failing_pair):
    sequence_id = "synthetic_sequence_zero"
    serial_rows = []
    callback_rows = []
    updater_rows = []
    timing_rows = []
    for pair_index, timestamp in enumerate((5, 10, 20, 30)):
        cam0_record = 100_999_999_999 if pair_index == 0 else 101_000_000_000 + pair_index
        cam1_record = cam0_record + 100
        cam0_filtered = pair_index * 2
        cam1_filtered = cam0_filtered + 1
        serial = {
            "absolute_record_delta_ns": 100, "anchor_camera_id": 0,
            "anchor_filtered_index": cam0_filtered,
            "cam0_filtered_index": cam0_filtered, "cam0_header_time_ns": timestamp,
            "cam0_record_time_ns": cam0_record, "cam1_filtered_index": cam1_filtered,
            "cam1_header_time_ns": timestamp + 1, "cam1_record_time_ns": cam1_record,
            "pair_index": pair_index, "record_type": "pair_index", "schema_version": 1,
            "sequence_id": sequence_id, "sequence_index": 0,
        }
        serial_rows.append(serial)
        callback_rows.append({
            "anchor_filtered_index": cam0_filtered, "callback_index": pair_index,
            "cam0_filtered_index": cam0_filtered, "cam0_header_time_ns": timestamp,
            "cam0_record_time_ns": cam0_record, "cam1_filtered_index": cam1_filtered,
            "cam1_header_time_ns": timestamp + 1, "cam1_record_time_ns": cam1_record,
            "camera_timestamp_ns": timestamp, "enqueue_entered": True,
            "enqueue_returned": True, "enqueue_status": "queued", "mode": mode,
            "pair_index": pair_index, "processing_entered": True,
            "processing_returned": True, "processing_status": "processed",
            "record_type": "serial_callback", "schema_version": 1,
            "sequence_id": sequence_id, "sequence_index": 0,
            "state_row_emitted": False, "trajectory_index": None,
            "updater_invocation_ids": [pair_index], "updater_invoked": True,
        })
        empty = pair_index == 0
        terminal_status = "empty_input" if empty else "committed_counted"
        terminal_subreason = "input_empty" if empty else "none"
        nonempty = not empty
        preflight_attempted = not empty
        preflight_accepted = not empty
        committed = not empty
        primary = not empty
        input_count = 0 if empty else 3
        raw_count = 0 if empty else 2
        duration = 100 if mode == "nullspace" else 105
        if failing_pair == run_index // 2 and mode == "schur" and not empty:
            duration = 200
        timer_start = 10_000 + pair_index * 1_000
        updater = {
            "baseline_preflight_attempted": preflight_attempted,
            "camera_timestamp_ns": timestamp, "committed": committed,
            "duration_ns": duration, "input_feature_count": input_count,
            "invocation_id": pair_index, "mode": mode, "nonempty": nonempty,
            "pair_index": pair_index, "preflight_accepted": preflight_accepted,
            "primary": primary, "raw_system_count": raw_count,
            "record_type": "updater_event", "schema_version": 1,
            "sequence_id": sequence_id, "sequence_index": 0,
            "terminal_status": terminal_status, "terminal_subreason": terminal_subreason,
            "timer_clock": "std::chrono::steady_clock",
            "timer_end_ns": timer_start + duration, "timer_start_ns": timer_start,
        }
        updater_rows.append(updater)
        timing_rows.append({
            "cam0_record_time_ns": cam0_record, "camera_timestamp_ns": timestamp,
            "committed": committed, "duration_ns": duration,
            "invocation_id": pair_index, "mode": mode, "nonempty": nonempty,
            "preflight_accepted": preflight_accepted, "primary": primary,
            "record_type": "updater_timing", "schema_version": 1,
            "sequence_id": sequence_id, "sequence_index": 0,
            "serial_pair_index": pair_index, "terminal_status": terminal_status,
            "timer_clock": "std::chrono::steady_clock",
            "timer_end_ns": timer_start + duration, "timer_start_ns": timer_start,
        })
    return tuple(serial_rows), tuple(callback_rows), tuple(updater_rows), tuple(timing_rows)


def build_fixture(root, failing_pair=None, effective_edge=None):
    profile = frozen_profile()
    binding = execution_binding_record(profile)
    binding_bytes = canonical(binding)
    prior_state = {
        "schema_version": 1,
        "record_type": "cp2_timing_control_prior_state",
        "values": [
            {
                "path": profile.value["controls"]["writes"][0]["path"],
                "parser": "integer", "text": "1",
            },
            {
                "path": profile.value["controls"]["writes"][1]["path"],
                "parser": "text", "text": "ondemand",
            },
        ],
        "modules_preexisting": ["msr"],
        "services_active": ["irqbalance"],
        "process_affinity": [0, 1],
    }
    prior_bytes = control_receipt(profile.sha256, "prior", 1_000, prior_state)
    applied_bytes = control_receipt(
        profile.sha256, "applied", 5_000, stable_control_state(profile),
    )
    restored_bytes = control_receipt(
        profile.sha256, "restored", 700_000, prior_state,
    )
    provenance = {
        "schema_version": 1, "record_type": "cp2_timing_provenance", "checkpoint": "CP2-E",
        "profile_sha256": profile.sha256, "source_commit": "b" * 40,
        "source_tree": "c" * 40, "unit_manifest_sha256": "d" * 64,
        "sequence_set_manifest_sha256": "e" * 64,
        "bag_sha256": profile.value["input"]["bag_sha256"],
        "executable_sha256": "1" * 64, "dso_closure_sha256": "2" * 64,
        "configuration_sha256": "3" * 64, "launch_sha256": sha(binding_bytes),
        "boot_id_sha256": "5" * 64,
        "machine_identity_sha256": profile.value["target"]["machine_identity_sha256"],
        "run_identity_sha256": "7" * 64,
        "control_prior_sha256": sha(prior_bytes),
        "control_applied_sha256": sha(applied_bytes),
        "control_restored_sha256": sha(restored_bytes),
    }
    support = {}
    runs = []
    commands = []
    fresh_identities = []
    for run_index in range(6):
        mode = artifact.PAIR_ORDER[run_index // 2][run_index % 2]
        prefix = "runs/run_{}/".format(run_index)
        trace_directory = "/tmp/cp2e-synthetic/run_{}".format(run_index)
        path = lambda name: trace_directory + "/" + name
        parameters = {"/cp2_vio/up_msckf_landmark_elimination": mode}
        parameters_bytes = canonical(parameters)
        resolved_sha = cp2_schema.resolved_parameters_sha256(parameters)
        context = {
            "schema_version": 1, "record_type": "cp2_runtime_context",
            "checkpoint": "CP2-E", "run_id": "run-{}".format(run_index),
            "sequence_index": profile.value["input"]["sequence_index"],
            "sequence_id": profile.value["input"]["sequence_id"], "mode": mode,
            "shadow_enabled": False, "trace_level": "timing",
            "source_commit": provenance["source_commit"],
            "config_sha256": provenance["configuration_sha256"],
            "bag_sha256": provenance["bag_sha256"], "pair_index_sha256": None,
            "resolved_parameters_sha256": resolved_sha,
            "trace_directory": trace_directory, "serial_trace_path": path("serial.jsonl"),
            "callback_trace_path": path("callbacks.jsonl"), "trajectory_trace_path": None,
            "updater_trace_path": path("updater.jsonl"), "state_payload_path": None,
            "proposal_payload_path": None, "raw_system_payload_path": None,
            "timing_trace_path": path("timing.jsonl"),
            "runtime_parameters_path": path("runtime_parameters.json"),
            "loader_map_before_path": path("loader_before.txt"),
            "loader_map_after_path": path("loader_after.txt"),
            "legacy_state_path": None, "legacy_deviation_path": None,
            "legacy_timing_path": None,
        }
        serial, callbacks, updater, timing = raw_trace_rows(run_index, mode, failing_pair)
        pre_clock = clock_snapshot(
            profile, provenance, run_index, "pre",
            effective_edge=effective_edge,
        )
        post_clock = clock_snapshot(
            profile, provenance, run_index, "post",
            effective_edge=effective_edge,
        )
        pre_raw = raw_telemetry_snapshot(
            run_index, "pre", effective_edge=effective_edge,
        )
        post_raw = raw_telemetry_snapshot(
            run_index, "post", effective_edge=effective_edge,
        )
        comparison = controls.compare_raw_telemetry(
            pre_raw, post_raw, (0,), (3_000_000,),
            (0, 120_000), ((99, 100), (101, 100)),
        )
        loader_before = b"loader-map\n"
        loader_after = b"loader-map\n"
        ros_start, ros_end = phase_interval(run_index, "ros_run")
        process_identity = {
            "schema_version": 1,
            "record_type": "cp2_timing_process_identity_observation",
            "checkpoint": "CP2-E", "profile_sha256": profile.sha256,
            "execution_binding_sha256": sha(binding_bytes),
            "run_index": run_index,
            "observed_monotonic_ns": ros_start + 100,
            "pid": 1000 + run_index, "start_time_ticks": 2000 + run_index,
            "executable_sha256": binding["executable_sha256"],
            "loader_before_sha256": sha(loader_before),
            "loader_after_sha256": sha(loader_after),
        }
        process_identity_bytes = canonical(process_identity)
        fresh_identity = fresh_process_identity(
            process_identity, profile.sha256, run_index,
        )
        fresh_identities.append(fresh_identity)
        runtime_start, runtime_end = phase_interval(run_index, "runtime_preflight")
        runtime_stdout = b"runtime-preflight-ok\n"
        runtime_stderr = b""
        runtime_argv = render_binding(
            binding, "runtime_preflight", run_index, mode,
            "run-{}".format(run_index), trace_directory,
        )
        runtime_preflight = canonical({
            "schema_version": 1,
            "record_type": "cp2_timing_runtime_preflight_receipt",
            "checkpoint": "CP2-E", "profile_sha256": profile.sha256,
            "execution_binding_sha256": sha(binding_bytes),
            "run_index": run_index, "argv": runtime_argv,
            "environment": profile.value["runtime"]["environment"],
            "cwd": "/tmp", "started_monotonic_ns": runtime_start,
            "ended_monotonic_ns": runtime_end, "exit_code": 0,
            "timed_out": False, "process_group_complete": True,
            "stdout_hex": runtime_stdout.hex(),
            "stdout_sha256": sha(runtime_stdout),
            "stderr_hex": runtime_stderr.hex(),
            "stderr_sha256": sha(runtime_stderr), "passed": True,
        })
        affinity = canonical({
            "schema_version": 1, "record_type": "cp2_timing_affinity_audit",
            "root_pid": 1000 + run_index, "expected_cpus": [0],
            "poll_interval_ns": 1_000_000, "observation_count": 1,
            "observations": [{"monotonic_ns": ros_start + 200,
                              "tid": 1000 + run_index,
                              "cpu_ids": [0]}], "passed": True,
        })
        run_support = {
            prefix + "context.json": canonical(context),
            prefix + "serial.jsonl": b"".join(canonical(row) for row in serial),
            prefix + "callbacks.jsonl": b"".join(canonical(row) for row in callbacks),
            prefix + "updater.jsonl": b"".join(canonical(row) for row in updater),
            prefix + "timing.jsonl": b"".join(canonical(row) for row in timing),
            prefix + "clock_pre.json": pre_clock, prefix + "clock_post.json": post_clock,
            prefix + "runtime_parameters.json": parameters_bytes,
            prefix + "loader_before.txt": loader_before,
            prefix + "loader_after.txt": loader_after,
            prefix + "stdout.bin": b"", prefix + "stderr.bin": b"",
            prefix + "affinity_audit.json": affinity,
            prefix + "process_identity.json": process_identity_bytes,
            prefix + "runtime_preflight.json": runtime_preflight,
            prefix + "telemetry_pre.json": pre_raw.canonical_bytes,
            prefix + "telemetry_post.json": post_raw.canonical_bytes,
            prefix + "telemetry_comparison.json": comparison["canonical_bytes"],
        }
        support.update(run_support)
        runs.append(artifact.TimingRun(
            run_index=run_index, profile_sha256=profile.sha256,
            run_id="run-{}".format(run_index),
            fresh_process_identity=fresh_identity,
            pre_snapshot_sha256=sha(pre_clock), post_snapshot_sha256=sha(post_clock),
            process_identity_sha256=sha(process_identity_bytes),
            runtime_preflight_sha256=sha(runtime_preflight),
            telemetry_pre_sha256=pre_raw.sha256,
            telemetry_post_sha256=post_raw.sha256,
            telemetry_comparison_sha256=comparison["sha256"],
            trace_bundle_sha256=artifact.trace_bundle_sha256(run_support, run_index),
        ))
        for phase in artifact.COMMAND_PHASES:
            command_id = len(commands)
            started, ended = phase_interval(run_index, phase)
            stdout_sha = sha(runtime_stdout) if phase == "runtime_preflight" else sha(b"")
            commands.append({
                "schema_version": 1, "record_type": "timing_command",
                "command_id": command_id, "run_index": run_index,
                "timing_pair_index": run_index // 2, "phase": phase,
                "argv": render_binding(
                    binding, phase, run_index, mode,
                    "run-{}".format(run_index), trace_directory,
                ),
                "environment": profile.value["runtime"]["environment"],
                "cwd": "/tmp", "started_monotonic_ns": started,
                "ended_monotonic_ns": ended, "exit_code": 0,
                "timed_out": False, "process_group_complete": True,
                "stdout_sha256": stdout_sha, "stderr_sha256": sha(b""),
            })
    (
        helper_transcript, helper_terminal, helper_guardian_evidence,
        helper_control_evidence, helper_provenance,
    ) = helper_evidence(
        profile, prior_state, stable_control_state(profile), effective_edge,
    )
    provenance.update(helper_provenance)
    provenance["run_identity_sha256"] = sha(canonical({
        "domain": "SchurVIO-CP2-E-six-fresh-processes-v1",
        "profile_sha256": profile.sha256,
        "fresh_process_identities": fresh_identities,
    }))
    source = artifact.TimingAssemblyInput(
        profile_bytes=profile.canonical_bytes, profile_sha256=profile.sha256,
        execution_binding_bytes=binding_bytes,
        control_prior_bytes=prior_bytes, control_applied_bytes=applied_bytes,
        control_restored_bytes=restored_bytes,
        helper_transcript_bytes=helper_transcript,
        helper_terminal_receipt_bytes=helper_terminal,
        helper_guardian_evidence_bytes=helper_guardian_evidence,
        helper_control_evidence_bytes=helper_control_evidence,
        sequence_index=profile.value["input"]["sequence_index"],
        sequence_id=profile.value["input"]["sequence_id"],
        bag_begin_record_time_ns=1_000_000_000,
        frozen_offset_ns=profile.value["input"]["frozen_offset_ns"],
        runs=tuple(runs), provenance=provenance, commands=tuple(commands),
        support_files=support,
    )
    result = artifact.assemble_timing_artifact(root, source)
    return source, result


def mutate_jsonl(payload, row_index, mutation):
    rows = [json.loads(line) for line in payload.splitlines()]
    mutation(rows[row_index])
    return b"".join(canonical(row) for row in rows)


def mutate_run_support(source, run_index, filename, payload):
    support = dict(source.support_files)
    relative = "runs/run_{}/{}".format(run_index, filename)
    support[relative] = payload
    run_support = {
        path: support[path] for path in artifact._required_run_support(run_index)
    }
    runs = list(source.runs)
    digest_fields = {
        "clock_pre.json": "pre_snapshot_sha256",
        "clock_post.json": "post_snapshot_sha256",
        "process_identity.json": "process_identity_sha256",
        "runtime_preflight.json": "runtime_preflight_sha256",
        "telemetry_pre.json": "telemetry_pre_sha256",
        "telemetry_post.json": "telemetry_post_sha256",
        "telemetry_comparison.json": "telemetry_comparison_sha256",
    }
    changes = {
        "trace_bundle_sha256": artifact.trace_bundle_sha256(run_support, run_index),
    }
    if filename in digest_fields:
        changes[digest_fields[filename]] = sha(payload)
    runs[run_index] = replace(runs[run_index], **changes)
    return replace(source, runs=tuple(runs), support_files=support)


def coordinated_helper_mutation(source, mutation):
    rows = [json.loads(line) for line in source.helper_transcript_bytes.splitlines()]
    terminal = json.loads(source.helper_terminal_receipt_bytes)
    guardian_rows = [
        json.loads(line) for line in source.helper_guardian_evidence_bytes.splitlines()
    ]
    mutation(rows, terminal, guardian_rows)
    guardian_previous = "0" * 64
    guardian_lines = []
    for index, row in enumerate(guardian_rows):
        row["sequence"] = index
        row["previous_record_sha256"] = guardian_previous
        line = canonical(row)
        guardian_lines.append(line)
        guardian_previous = sha(line)
    guardian_evidence = b"".join(guardian_lines)
    if guardian_lines and len(rows) >= 2:
        seal = rows[-2]["payload"].get("population_seal")
        if type(seal) is dict:
            seal["observation_count"] = len(guardian_lines)
            seal["evidence_size_bytes"] = len(guardian_evidence)
            seal["evidence_sha256"] = sha(guardian_evidence)
            seal["first_record_sha256"] = sha(guardian_lines[0])
            seal["last_record_sha256"] = sha(guardian_lines[-1])
            seal_sha = sha(canonical(seal))
            rows[-2]["payload"]["population_seal_sha256"] = seal_sha
            rows[-1]["payload"]["population_seal_sha256"] = seal_sha
            terminal["population_seal_sha256"] = seal_sha
    previous = "0" * 64
    lines = []
    for index, row in enumerate(rows):
        row["event_index"] = index
        row["previous_record_sha256"] = previous
        line = canonical(row)
        lines.append(line)
        previous = sha(line)
    transcript = b"".join(lines)
    terminal["event_count"] = len(rows)
    terminal["transcript_size_bytes"] = len(transcript)
    terminal["transcript_sha256"] = sha(transcript)
    terminal["first_record_sha256"] = None if not lines else sha(lines[0])
    terminal["last_record_sha256"] = None if not lines else sha(lines[-1])
    terminal_payload = canonical(terminal)
    provenance = {
        **source.provenance,
        "helper_transcript_sha256": sha(transcript),
        "helper_terminal_receipt_sha256": sha(terminal_payload),
        "helper_binding_digest": terminal["binding_digest"],
        "helper_terminal_journal_sha256": terminal["terminal_journal_sha256"],
        "helper_population_seal_sha256": terminal["population_seal_sha256"],
        "helper_guardian_evidence_sha256": sha(guardian_evidence),
    }
    return replace(
        source, helper_transcript_bytes=transcript,
        helper_terminal_receipt_bytes=terminal_payload,
        helper_guardian_evidence_bytes=guardian_evidence,
        provenance=provenance,
    )


def rebind_control_evidence_staging_inode(source, device, inode):
    """Reseal only the synthetic helper metadata bound to a staged inode."""

    rows = [json.loads(line) for line in source.helper_transcript_bytes.splitlines()]
    terminal = json.loads(source.helper_terminal_receipt_bytes)
    checkpoint = rows[2]["payload"]["control_evidence_checkpoint"]
    checkpoint["device"] = device
    checkpoint["inode"] = inode
    rows[2]["payload"]["control_evidence_checkpoint_sha256"] = sha(
        canonical(checkpoint)
    )
    seal = rows[-1]["payload"]["control_evidence_seal"]
    seal["device"] = device
    seal["inode"] = inode
    seal_sha = sha(canonical(seal))
    rows[-1]["payload"]["control_evidence_seal_sha256"] = seal_sha
    terminal["control_evidence_seal_sha256"] = seal_sha
    previous = "0" * 64
    lines = []
    for index, row in enumerate(rows):
        row["event_index"] = index
        row["previous_record_sha256"] = previous
        line = canonical(row)
        lines.append(line)
        previous = sha(line)
    transcript = b"".join(lines)
    terminal["first_record_sha256"] = sha(lines[0])
    terminal["last_record_sha256"] = sha(lines[-1])
    terminal["transcript_sha256"] = sha(transcript)
    terminal["transcript_size_bytes"] = len(transcript)
    terminal_payload = canonical(terminal)
    provenance = {
        **source.provenance,
        "helper_transcript_sha256": sha(transcript),
        "helper_terminal_receipt_sha256": sha(terminal_payload),
        "helper_control_evidence_seal_sha256": seal_sha,
    }
    return replace(
        source, helper_transcript_bytes=transcript,
        helper_terminal_receipt_bytes=terminal_payload,
        provenance=provenance,
    )


def reseal_manifest(root):
    report = root / "cp2_report.json"
    if report.exists():
        value = json.loads(report.read_bytes())
        report.write_bytes(canonical(value))
    rows = []
    for path in sorted(path for path in root.rglob("*") if path.is_file() and path.name != "SHA256SUMS"):
        rows.append("{}  {}\n".format(sha(path.read_bytes()), path.relative_to(root).as_posix()))
    payload = "".join(rows).encode("ascii")
    (root / "SHA256SUMS").write_bytes(payload)
    return sha(payload)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="cp2e-artifact-test-", dir="/tmp"))

    def tearDown(self):
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_complete_six_run_artifact_passes_detached_raw_recomputation(self):
        source, result = build_fixture(self.temp / "artifact")
        self.assertTrue(result.passed)
        verification = artifact.verify_timing_artifact(self.temp / "artifact", result.manifest_sha256)
        self.assertTrue(verification["passed"])
        self.assertEqual((verification["run_count"], verification["pair_count"]), (6, 3))
        self.assertEqual(verification["sample_count"], 24)
        rows = [json.loads(line) for line in (self.temp / "artifact/timing_samples.jsonl").read_bytes().splitlines()]
        self.assertEqual(len(rows), 24)
        self.assertFalse(rows[0]["primary"])
        self.assertEqual(rows[0]["terminal_status"], "empty_input")
        profile = frozen_profile()
        guardian_contract = helper_protocol.GuardianEvidenceContract(
            profile.value["guardians"]["poll_interval_ns"],
            profile.value["guardians"]["maximum_observation_gap_ns"],
            profile.value["guardians"]["maximum_campaign_duration_ns"],
            profile.value["guardians"]["maximum_observation_count"],
            profile.value["limits"]["maximum_guardian_line_bytes"],
            profile.value["limits"]["maximum_guardian_evidence_bytes"],
            tuple(profile.value["guardians"]["surfaces"]),
        )
        helper_verified = helper_protocol.verify_transcript_bytes(
            source.helper_transcript_bytes,
            source.helper_terminal_receipt_bytes,
            helper_protocol.Binding.from_record(
                dict(artifact._helper_binding(profile))
            ),
            tuple(
                helper_protocol.FrozenRunSlot.from_record(slot)
                for slot in profile.value["pairing"]["run_slots"]
            ),
            source.helper_guardian_evidence_bytes,
            guardian_contract,
            source.helper_control_evidence_bytes,
        )
        self.assertEqual(len(helper_verified.events), 16)

    def test_helper_written_held_guardian_inode_is_adopted_without_rewrite(self):
        source, _result = build_fixture(self.temp / "byte-source")
        guardian_payload = source.helper_guardian_evidence_bytes
        formal_root = self.temp / "held-staging"
        formal_root.mkdir(mode=0o700)
        guardian_path = formal_root / "guardian_evidence.jsonl"
        guardian_path.write_bytes(guardian_payload)
        guardian_path.chmod(0o440)
        before = guardian_path.stat()
        held = artifact.HeldGuardianEvidence(
            guardian_path, sha(guardian_payload), len(guardian_payload),
        )
        try:
            held_source = replace(
                source, helper_guardian_evidence_bytes=held,
            )
            result = artifact.assemble_timing_artifact(formal_root, held_source)
            after = guardian_path.stat()
            self.assertEqual(
                (after.st_dev, after.st_ino, after.st_size),
                (before.st_dev, before.st_ino, before.st_size),
            )
            self.assertEqual(guardian_path.read_bytes(), guardian_payload)
            self.assertTrue(
                artifact.verify_timing_artifact(
                    formal_root, result.manifest_sha256,
                )["passed"]
            )
        finally:
            held.close()

    def test_both_helper_written_evidence_inodes_are_adopted_without_rewrite(self):
        source, _result = build_fixture(self.temp / "dual-byte-source")
        formal_root = self.temp / "dual-held-staging"
        formal_root.mkdir(mode=0o700)
        guardian_path = formal_root / "guardian_evidence.jsonl"
        control_path = formal_root / "control_evidence.jsonl"
        guardian_path.write_bytes(source.helper_guardian_evidence_bytes)
        control_path.write_bytes(source.helper_control_evidence_bytes)
        guardian_path.chmod(0o440)
        control_path.chmod(0o440)
        control_status = control_path.stat()
        source = rebind_control_evidence_staging_inode(
            source, control_status.st_dev, control_status.st_ino,
        )
        before_guardian = guardian_path.stat()
        before_control = control_path.stat()
        held_guardian = artifact.HeldGuardianEvidence(
            guardian_path, sha(source.helper_guardian_evidence_bytes),
            len(source.helper_guardian_evidence_bytes),
        )
        held_control = artifact.HeldControlEvidence(
            control_path, sha(source.helper_control_evidence_bytes),
            len(source.helper_control_evidence_bytes),
        )
        try:
            held_source = replace(
                source, helper_guardian_evidence_bytes=held_guardian,
                helper_control_evidence_bytes=held_control,
            )
            result = artifact.assemble_timing_artifact(formal_root, held_source)
            after_guardian = guardian_path.stat()
            after_control = control_path.stat()
            self.assertEqual(
                (after_guardian.st_dev, after_guardian.st_ino, after_guardian.st_size),
                (before_guardian.st_dev, before_guardian.st_ino, before_guardian.st_size),
            )
            self.assertEqual(
                (after_control.st_dev, after_control.st_ino, after_control.st_size),
                (before_control.st_dev, before_control.st_ino, before_control.st_size),
            )
            self.assertTrue(
                artifact.verify_timing_artifact(
                    formal_root, result.manifest_sha256,
                )["passed"]
            )
        finally:
            held_guardian.close()
            held_control.close()

    def test_effective_frequency_exact_inclusive_endpoints_pass(self):
        for edge in ("lower", "upper"):
            with self.subTest(edge=edge):
                root = self.temp / ("frequency-" + edge)
                _source, result = build_fixture(root, effective_edge=edge)
                self.assertTrue(result.passed)
                self.assertTrue(
                    artifact.verify_timing_artifact(
                        root, result.manifest_sha256,
                    )["passed"]
                )

    def test_coordinated_helper_substitution_reorder_missing_and_duplicate_reject(self):
        source, _result = build_fixture(self.temp / "source")

        def substitute(rows, _terminal, _guardian):
            receipt = rows[3]["payload"]["receipt"]
            item = next(
                value for value in receipt["sources"]
                if value["source_id"] == "cpu0.aperf"
            )
            raw = (999).to_bytes(8, "little")
            item["parsed_value"] = 999
            item["raw_hex"] = raw.hex()
            item["raw_sha256"] = sha(raw)
            rows[3]["payload"]["receipt_sha256"] = sha(canonical(receipt))

        def reorder(rows, _terminal, _guardian):
            rows[3], rows[4] = rows[4], rows[3]

        def missing(rows, _terminal, _guardian):
            rows.pop(4)

        def duplicate(rows, _terminal, _guardian):
            receipt = rows[3]["payload"]["receipt"]
            receipt["sources"].append(dict(receipt["sources"][0]))
            rows[3]["payload"]["receipt_sha256"] = sha(canonical(receipt))

        def boolean_cpu(rows, _terminal, _guardian):
            receipt = rows[3]["payload"]["receipt"]
            item = next(
                value for value in receipt["sources"]
                if value["source_id"] == "cpu0.aperf"
            )
            item["cpu_id"] = False
            rows[3]["payload"]["receipt_sha256"] = sha(canonical(receipt))

        for name, mutation in (
            ("substitution", substitute), ("reorder", reorder),
            ("missing", missing), ("duplicate", duplicate),
            ("boolean-cpu", boolean_cpu),
        ):
            mutated = coordinated_helper_mutation(source, mutation)
            with self.subTest(name=name), self.assertRaises(
                artifact.TimingArtifactError
            ):
                artifact.assemble_timing_artifact(
                    self.temp / ("helper-" + name), mutated,
                )

    def test_helper_terminal_stop_seal_guardian_gap_and_fail_closed_reject(self):
        source, _result = build_fixture(self.temp / "source")

        def duplicate_seal(rows, _terminal, _guardian):
            terminal_seal = rows[14]["payload"]["population_seal"]
            rows[3]["payload"]["population_seal"] = terminal_seal
            rows[3]["payload"]["population_seal_sha256"] = sha(
                canonical(terminal_seal)
            )

        def double_stop(_rows, terminal, _guardian):
            terminal["population_stop_count"] = 2

        def guardian_duration(rows, _terminal, guardian):
            seal = rows[14]["payload"]["population_seal"]
            observation = guardian[0]
            observation["ended_monotonic_ns"] = (
                observation["started_monotonic_ns"]
                + seal["maximum_observation_gap_ns"] + 1
            )
            seal["coverage_ended_monotonic_ns"] = observation[
                "ended_monotonic_ns"
            ]

        def fail_closed(_rows, terminal, _guardian):
            terminal["terminal_status"] = "fail_closed"
            terminal["error_type"] = "SyntheticClientDeath"

        def foreign_substitution(rows, _terminal, guardian):
            # Keep the guardian chain and terminal population seal coherent so
            # the artifact verifier, rather than a transport hash, rejects a
            # canonical-looking row that no longer intersects the estimator.
            substituted = copy.deepcopy(
                guardian[-1]["foreign_affinity_eligibility"]
            )
            substituted[0]["effective_affinity_cpu_ids"] = [1]
            guardian[-1]["foreign_affinity_eligibility"] = substituted
            rows[-2]["payload"]["population_seal"][
                "final_foreign_affinity_eligibility"
            ] = copy.deepcopy(substituted)

        for name, mutation in (
            ("duplicate-seal", duplicate_seal),
            ("double-stop", double_stop),
            ("guardian-duration", guardian_duration),
            ("foreign-substitution", foreign_substitution),
            ("fail-closed", fail_closed),
        ):
            mutated = coordinated_helper_mutation(source, mutation)
            with self.subTest(name=name), self.assertRaises(
                artifact.TimingArtifactError
            ):
                artifact.assemble_timing_artifact(
                    self.temp / ("helper-terminal-" + name), mutated,
                )

    def test_one_failed_pair_cannot_be_hidden_by_median(self):
        _, result = build_fixture(self.temp / "failed", failing_pair=1)
        self.assertFalse(result.passed)
        verification = artifact.verify_timing_artifact(self.temp / "failed", result.manifest_sha256)
        self.assertFalse(verification["passed"])
        report = json.loads((self.temp / "failed/cp2_report.json").read_text())
        self.assertFalse(report["every_pair_passed"])

    def test_external_anchor_manifest_and_common_payload_corruption_reject(self):
        _, result = build_fixture(self.temp / "artifact")
        with self.assertRaises(artifact.TimingArtifactError):
            artifact.verify_timing_artifact(self.temp / "artifact", "0" * 64)
        common = self.temp / "artifact/common/pair_0.bin"
        common.write_bytes(common.read_bytes() + b"x")
        with self.assertRaises(artifact.TimingArtifactError):
            artifact.verify_timing_artifact(self.temp / "artifact", result.manifest_sha256)

    def test_caller_supplied_and_coordinated_forged_reduced_rows_reject(self):
        source, _ = build_fixture(self.temp / "source")
        retained = json.loads((self.temp / "source/timing_samples.jsonl").read_bytes().splitlines()[0])
        forged = artifact.sample_from_record(retained)
        runs = list(source.runs)
        runs[0] = replace(runs[0], samples=(forged,))
        with self.assertRaisesRegex(artifact.TimingArtifactError, "caller-supplied"):
            artifact.assemble_timing_artifact(self.temp / "caller-forged", replace(source, runs=tuple(runs)))

        timing_path = self.temp / "source/timing_samples.jsonl"
        rows = [json.loads(line) for line in timing_path.read_bytes().splitlines()]
        rows[1]["timer_end_ns"] += 1
        rows[1]["duration_ns"] += 1
        forged_payload = b"".join(canonical(row) for row in rows)
        timing_path.write_bytes(forged_payload)
        report_path = self.temp / "source/cp2_report.json"
        report = json.loads(report_path.read_bytes())
        report["timing_samples_sha256"] = sha(forged_payload)
        report_path.write_bytes(canonical(report))
        forged_anchor = reseal_manifest(self.temp / "source")
        with self.assertRaisesRegex(artifact.TimingArtifactError, "raw trace derivation"):
            artifact.verify_timing_artifact(self.temp / "source", forged_anchor)

    def test_endpoint_omission_status_callback_and_serial_mutations_reject(self):
        source, _ = build_fixture(self.temp / "source")
        cases = []
        timing = source.support_files["runs/run_0/timing.jsonl"]
        cases.append(("endpoint", mutate_run_support(
            source, 0, "timing.jsonl",
            mutate_jsonl(timing, 1, lambda row: row.__setitem__("timer_end_ns", row["timer_end_ns"] + 1)),
        ), "raw timing"))
        cases.append(("omitted", mutate_run_support(
            source, 0, "timing.jsonl", b"\n".join(timing.splitlines()[1:]) + b"\n",
        ), "omits or adds"))
        cases.append(("status", mutate_run_support(
            source, 0, "timing.jsonl",
            mutate_jsonl(timing, 1, lambda row: row.__setitem__("terminal_status", "preflight_rejected")),
        ), "raw timing"))
        callbacks = source.support_files["runs/run_0/callbacks.jsonl"]
        cases.append(("callback", mutate_run_support(
            source, 0, "callbacks.jsonl",
            mutate_jsonl(callbacks, 1, lambda row: row.__setitem__("updater_invocation_ids", [99])),
        ), "call-entry"))
        serial = source.support_files["runs/run_0/serial.jsonl"]
        def drift(row):
            row["cam0_record_time_ns"] += 1
            row["absolute_record_delta_ns"] -= 1
        cases.append(("serial", mutate_run_support(
            source, 0, "serial.jsonl", mutate_jsonl(serial, 1, drift),
        ), "serial/callback"))
        for name, mutated, message in cases:
            with self.subTest(name=name), self.assertRaisesRegex(artifact.TimingArtifactError, message):
                artifact.assemble_timing_artifact(self.temp / name, mutated)

    def test_clock_telemetry_and_stable_control_mutations_reject(self):
        source, _ = build_fixture(self.temp / "source")
        post = source.support_files["runs/run_0/clock_post.json"]
        mutations = []
        mutations.append(("throttle", lambda row: row["telemetry"]["cpu_counters"][0].__setitem__("thermal_throttle_count", 1)))
        mutations.append(("aperf", lambda row: row["telemetry"]["cpu_counters"][0].__setitem__("aperf", 999)))
        mutations.append(("stable", lambda row: row.__setitem__("stable_control_state_sha256", "a" * 64)))
        mutations.append(("frequency", lambda row: row["telemetry"]["cpu_counters"][0].__setitem__("scaling_current_frequency_khz", 2_999_999)))
        for name, mutation in mutations:
            payload = mutate_jsonl(post, 0, mutation)
            mutated = mutate_run_support(source, 0, "clock_post.json", payload)
            runs = list(mutated.runs)
            runs[0] = replace(runs[0], post_snapshot_sha256=sha(payload))
            mutated = replace(mutated, runs=tuple(runs))
            with self.subTest(name=name), self.assertRaises(artifact.TimingArtifactError):
                artifact.assemble_timing_artifact(self.temp / ("clock-" + name), mutated)

    def test_new_retained_receipt_hash_and_cross_join_mutations_reject(self):
        source, _ = build_fixture(self.temp / "source")
        cases = []

        binding = json.loads(source.execution_binding_bytes)
        binding["phase_templates"]["runtime_preflight"].append("changed")
        binding_payload = canonical(binding)
        cases.append(("binding-render", replace(
            source, execution_binding_bytes=binding_payload,
            provenance={**source.provenance, "launch_sha256": sha(binding_payload)},
        )))

        prior = json.loads(source.control_prior_bytes)
        prior["captured_monotonic_ns"] += 1
        cases.append(("prior-hash", replace(
            source, control_prior_bytes=canonical(prior),
        )))

        applied = json.loads(source.control_applied_bytes)
        applied["state"]["values"][0]["text"] = "1"
        applied["state_sha256"] = sha(canonical(applied["state"]))
        applied_payload = canonical(applied)
        cases.append(("applied-state", replace(
            source, control_applied_bytes=applied_payload,
            provenance={
                **source.provenance,
                "control_applied_sha256": sha(applied_payload),
            },
        )))

        applied_boolean = json.loads(source.control_applied_bytes)
        applied_boolean["state"]["dma_latency_us"] = False
        applied_boolean["state_sha256"] = sha(canonical(applied_boolean["state"]))
        applied_boolean_payload = canonical(applied_boolean)
        cases.append(("applied-boolean-number", replace(
            source, control_applied_bytes=applied_boolean_payload,
            provenance={
                **source.provenance,
                "control_applied_sha256": sha(applied_boolean_payload),
            },
        )))

        restored = json.loads(source.control_restored_bytes)
        restored["state"]["process_affinity"] = [0, 2]
        restored["state_sha256"] = sha(canonical(restored["state"]))
        restored_payload = canonical(restored)
        cases.append(("restored-exact", replace(
            source, control_restored_bytes=restored_payload,
            provenance={
                **source.provenance,
                "control_restored_sha256": sha(restored_payload),
            },
        )))

        process = json.loads(source.support_files["runs/run_0/process_identity.json"])
        process["observed_monotonic_ns"] = 999_999
        cases.append(("process-command-interval", mutate_run_support(
            source, 0, "process_identity.json", canonical(process),
        )))

        preflight = json.loads(source.support_files["runs/run_0/runtime_preflight.json"])
        preflight["argv"][-1] = "999"
        cases.append(("preflight-command-join", mutate_run_support(
            source, 0, "runtime_preflight.json", canonical(preflight),
        )))

        raw_pre = json.loads(source.support_files["runs/run_0/telemetry_pre.json"])
        raw_pre["receipts"][0]["parsed_value"] += 1
        cases.append(("raw-telemetry-parse", mutate_run_support(
            source, 0, "telemetry_pre.json", canonical(raw_pre),
        )))

        comparison = json.loads(
            source.support_files["runs/run_0/telemetry_comparison.json"]
        )
        comparison["per_cpu"][0]["aperf_delta"] += 1
        cases.append(("telemetry-recomputation", mutate_run_support(
            source, 0, "telemetry_comparison.json", canonical(comparison),
        )))

        comparison_boolean = json.loads(
            source.support_files["runs/run_0/telemetry_comparison.json"]
        )
        comparison_boolean["expected_cpu_ids"][:2] = [False, True]
        cases.append(("telemetry-boolean-cpus", mutate_run_support(
            source, 0, "telemetry_comparison.json", canonical(comparison_boolean),
        )))

        commands = list(source.commands)
        commands[0] = {**commands[0], "argv": commands[0]["argv"] + ["changed"]}
        cases.append(("command-binding", replace(source, commands=tuple(commands))))
        boolean_commands = list(source.commands)
        boolean_commands[0] = {**boolean_commands[0], "run_index": False}
        cases.append(("command-boolean-index", replace(
            source, commands=tuple(boolean_commands),
        )))
        cases.append(("run-population", replace(
            source,
            provenance={**source.provenance, "run_identity_sha256": "0" * 64},
        )))

        for name, mutated in cases:
            with self.subTest(name=name), self.assertRaises(artifact.TimingArtifactError):
                artifact.assemble_timing_artifact(self.temp / name, mutated)

    def test_detached_verifier_recomputes_coordinated_raw_telemetry_mutation(self):
        _, result = build_fixture(self.temp / "artifact")
        root = self.temp / "artifact"
        comparison_path = root / "runs/run_0/telemetry_comparison.json"
        comparison = json.loads(comparison_path.read_bytes())
        comparison["per_cpu"][0]["mperf_delta"] += 1
        comparison_payload = canonical(comparison)
        comparison_path.write_bytes(comparison_payload)
        report_path = root / "cp2_report.json"
        report = json.loads(report_path.read_bytes())
        report["runs"][0]["telemetry_comparison_sha256"] = sha(comparison_payload)
        run_support = {
            relative: (root / relative).read_bytes()
            for relative in artifact._required_run_support(0)
        }
        report["runs"][0]["trace_bundle_sha256"] = artifact.trace_bundle_sha256(
            run_support, 0,
        )
        report_path.write_bytes(canonical(report))
        forged_anchor = reseal_manifest(root)
        with self.assertRaisesRegex(
            artifact.TimingArtifactError, "independent raw recomputation",
        ):
            artifact.verify_timing_artifact(root, forged_anchor)

    def test_command_phase_order_count_profile_and_resource_limits_reject(self):
        source, _ = build_fixture(self.temp / "source")
        with self.assertRaisesRegex(artifact.TimingArtifactError, "24-command"):
            artifact.assemble_timing_artifact(self.temp / "short-command", replace(source, commands=source.commands[:-1]))
        commands = list(source.commands)
        commands[1] = {**commands[1], "phase": "clock_pre"}
        with self.assertRaisesRegex(artifact.TimingArtifactError, "phase/order"):
            artifact.assemble_timing_artifact(self.temp / "phase", replace(source, commands=tuple(commands)))
        commands = list(source.commands)
        commands[0] = {**commands[0], "environment": {"LANG": "C"}}
        with self.assertRaisesRegex(artifact.TimingArtifactError, "execution binding"):
            artifact.assemble_timing_artifact(self.temp / "environment", replace(source, commands=tuple(commands)))
        oversized = mutate_run_support(source, 0, "loader_before.txt", b"x" * 262_145)
        with self.assertRaisesRegex(artifact.TimingArtifactError, "resource limit"):
            artifact.assemble_timing_artifact(self.temp / "oversized", oversized)

    def test_missing_extra_or_substituted_support_file_rejects(self):
        source, _ = build_fixture(self.temp / "source")
        missing = dict(source.support_files)
        missing.pop("runs/run_0/updater.jsonl")
        with self.assertRaisesRegex(artifact.TimingArtifactError, "support-file population"):
            artifact.assemble_timing_artifact(self.temp / "missing", replace(source, support_files=missing))
        extra = dict(source.support_files)
        extra["runs/run_0/extra.bin"] = b"x"
        with self.assertRaisesRegex(artifact.TimingArtifactError, "support-file population"):
            artifact.assemble_timing_artifact(self.temp / "extra", replace(source, support_files=extra))
        substituted = dict(source.support_files)
        substituted["runs/run_0/updater.jsonl"] += b"x"
        with self.assertRaisesRegex(artifact.TimingArtifactError, "trace-bundle"):
            artifact.assemble_timing_artifact(self.temp / "substituted", replace(source, support_files=substituted))

    def test_full_terminal_status_subreason_vocabulary_is_typed(self):
        profile_sha = frozen_profile().sha256
        for status, subreasons in artifact.TERMINAL_MAPPING.items():
            for subreason in subreasons:
                empty = status == "empty_input"
                committed = status == "committed_counted"
                sample = artifact.TimingSample(
                    profile_sha256=profile_sha, timing_pair_index=0, run_index=0,
                    mode="nullspace", sequence_index=0, sequence_id="synthetic",
                    serial_pair_index=0, cam0_record_time_ns=1,
                    camera_timestamp_ns=1, invocation_id=0,
                    terminal_status=status, terminal_subreason=subreason,
                    input_feature_count=0 if empty else 1,
                    raw_system_count=1 if committed else 0,
                    baseline_preflight_attempted=committed,
                    nonempty=not empty, preflight_accepted=committed,
                    committed=committed, primary=committed,
                    timer_clock="std::chrono::steady_clock", timer_start_ns=1,
                    timer_end_ns=2, duration_ns=1,
                )
                self.assertEqual(artifact.sample_from_record(sample.record()), sample)


class CampaignMathBoundaryTests(unittest.TestCase):
    def test_u256_sha_codec_preserves_sixty_four_digit_leading_zeroes(self):
        self.assertEqual(timing_math.u256_to_hex(1), "0" * 63 + "1")
        self.assertEqual(timing_math.u256_from_hex("0" * 63 + "1"), 1)
        for invalid in ("1", "0" * 63 + "A", "0" * 65, True):
            with self.assertRaises(timing_math.TimingMathError):
                timing_math.u256_from_hex(invalid)

    def test_common_payload_binds_pair_index_count_order_and_timestamps(self):
        payload = timing_math.canonical_common_population_payload(0, (1, 2, 3))
        self.assertTrue(payload.startswith(timing_math.TIMING_COMMON_DOMAIN))
        self.assertNotEqual(payload, timing_math.canonical_common_population_payload(1, (1, 2, 3)))
        with self.assertRaises(timing_math.TimingMathError):
            timing_math.canonical_common_population_payload(0, (1, 1, 3))

    def test_campaign_revalidates_nested_pairs_and_every_pair_bit(self):
        pairs = tuple(timing_math.timing_pair_result(index, (10, 20), (100, 100), (105, 105)) for index in range(3))
        result = timing_math.timing_campaign_result(pairs)
        self.assertTrue(result.passed)
        with self.assertRaises(timing_math.TimingMathError):
            timing_math.TimingCampaignResult(result.pair_results, result.median_p50, result.median_p95, False, True)
        forged = object.__new__(timing_math.TimingPairResult)
        for name, value in pairs[0].__dict__.items():
            object.__setattr__(forged, name, value)
        object.__setattr__(forged, "common_payload_sha256", 0)
        with self.assertRaises(timing_math.TimingMathError):
            timing_math.timing_campaign_result((forged, pairs[1], pairs[2]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
