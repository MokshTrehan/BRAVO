#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Strict canonical CP2-E fixed-clock profile codec.

The historical ``.yaml`` filename is deliberately encoded as canonical JSON,
which is a YAML subset.  The codec accepts no comments, aliases, floating
point values, duplicate keys, alternate whitespace, or unknown fields.  It is
data-free and does not read host controls or recorded input.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


PROFILE_RELPATH = "project/cp2_timing_profile.yaml"
PAIR_ORDER = (("nullspace", "schur"), ("schur", "nullspace"), ("nullspace", "schur"))
COMMON_DOMAIN_TEXT = "SchurVIO-CP2-timing-common-v1\\0"
U64_MAX = (1 << 64) - 1
MAX_PROFILE_BYTES = 1024 * 1024
GUARDIAN_POPULATION_ITEM_LIMIT = 2048
MAX_CPUSET_MEMBERSHIP_BYTES = 256
MAX_GUARDIAN_LINE_BYTES = 64 * 1024 * 1024
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

ROOT_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_id",
    "authorization_sha256", "source_identity_policy", "target", "input",
    "pairing", "runtime", "cpu_plan", "privileged_helper", "cpuset",
    "cpu_controls", "interrupts", "telemetry_plan", "dma_latency",
    "guardians", "restoration", "controls", "observations", "limits",
    "artifact",
)
TARGET_KEYS = (
    "target_class", "architecture", "cpu_vendor", "cpu_family", "cpu_model",
    "cpu_stepping", "cpu_model_name", "logical_cpu_count",
    "physical_core_count", "threads_per_core", "kernel_release",
    "machine_identity_sha256", "boot_identity_policy",
)
INPUT_KEYS = (
    "sequence_index", "sequence_id", "frozen_offset_ns", "bag_sha256",
    "ground_truth_access", "registry_read_count", "input_kind",
    "synthetic_origin_sha256",
)
PAIRING_KEYS = (
    "pair_order", "run_slots", "warmup_ns", "common_payload_domain",
    "quantile_p50", "quantile_p95", "p50_ratio_limit", "p95_ratio_limit",
    "every_pair_must_pass",
)
RUN_SLOT_KEYS = ("run_index", "timing_pair_index", "position_in_pair", "mode")
RUNTIME_KEYS = (
    "cpu_ids", "smt_sibling_groups", "affinity_policy", "environment",
    "scaling_driver", "governor", "minimum_frequency_khz",
    "maximum_frequency_khz", "accepted_current_frequency_khz", "boost_enabled",
    "disabled_idle_state_names", "irqbalance_state", "dma_latency_us",
)
CONTROLS_KEYS = (
    "policy", "noninteractive_sudo_argv", "module_actions", "service_actions", "writes",
    "restore_on_every_exit", "recovery_journal_parent",
    "formal_authority_policy", "legacy_backend_policy",
)
MODULE_KEYS = ("name", "modprobe_argv", "remove_argv", "retain_if_preexisting")
SERVICE_KEYS = ("name", "deactivate_argv", "activate_argv", "retain_if_inactive")
WRITE_KEYS = ("order", "path", "desired_text", "parser", "restore_exact")
OBSERVATION_KEYS = (
    "observation_id", "argv", "environment", "cwd", "timeout_ns",
    "maximum_stdout_bytes", "parser", "expected",
)
LIMIT_KEYS = (
    "maximum_profile_bytes", "maximum_jsonl_line_bytes", "maximum_trace_bytes",
    "maximum_command_output_bytes", "maximum_samples_per_run",
    "maximum_process_seconds", "maximum_run_count", "maximum_pair_count",
    "maximum_guardian_population_items", "maximum_guardian_line_bytes",
    "maximum_guardian_evidence_bytes",
    "maximum_artifact_bytes",
)
ARTIFACT_KEYS = (
    "schema_version", "report_filename", "provenance_filename",
    "commands_filename", "timing_samples_filename", "manifest_filename",
    "publication_parent", "publication_policy",
)

CPU_PLAN_KEYS = (
    "controlled_cpu_ids", "estimator_cpu_ids", "helper_cpu_ids",
    "housekeeping_cpu_ids", "online_cpu_ids", "offline_cpu_ids",
    "smt_cores", "topology_source_sha256", "role_partition_policy",
    "estimator_smt_policy",
)
SMT_CORE_KEYS = (
    "physical_core_id", "sibling_cpu_ids", "online_cpu_ids",
    "offline_cpu_ids", "sibling_disposition",
)
PRIVILEGED_HELPER_KEYS = (
    "authority_policy", "plan_sha256", "source_sha256",
    "protocol_core_sha256", "root_launcher_path", "root_launcher_sha256",
    "import_closure_sha256", "sudoers_sha256", "trusted_root",
    "journal_directory", "invocation_argv", "helper_cpu_ids",
    "root_owner_uid", "socket_family", "socket_type",
    "peer_credential_policy", "nonce_policy", "client_selection_policy",
    "descriptor_transfer_policy", "installed_closure_file_count",
    "production_policy",
)
CPUSET_KEYS = (
    "cgroup_version", "hierarchy_identity_sha256", "mount_path",
    "parent_path", "group_name",
    "group_path", "cpus_path", "mems_path", "tasks_path",
    "effective_cpus_path", "effective_mems_path", "desired_cpu_ids",
    "desired_memory_nodes", "create_policy", "membership_policy",
    "effective_mask_policy", "remove_policy", "lifecycle",
    "guardian_poll_interval_ns",
)
CPU_CONTROLS_KEYS = (
    "application_order", "boost", "policies", "cpus",
)
BOOST_KEYS = (
    "path", "source_identity_sha256", "parser", "desired_text",
    "desired_enabled", "restore_exact",
)
CPUFREQ_POLICY_KEYS = (
    "policy_id", "cpu_ids", "source_identity_sha256", "driver",
    "driver_path", "governor_path", "minimum_frequency_path",
    "maximum_frequency_path", "desired_governor",
    "desired_minimum_frequency_khz", "desired_maximum_frequency_khz",
    "restore_exact",
)
CPU_CONTROL_KEYS = (
    "cpu_id", "role", "physical_core_id", "online_control_kind",
    "online_path", "online_source_identity_sha256", "expected_online",
    "cpufreq_policy_id", "idle_inventory_sha256", "idle_states",
    "restore_exact",
)
IDLE_STATE_KEYS = (
    "state_index", "name", "name_path", "disable_path",
    "desired_disabled", "restore_exact",
)
INTERRUPT_KEYS = (
    "inventory_sha256", "inventory_policy", "default_affinity_path",
    "default_affinity_cpu_ids", "default_source_identity_sha256",
    "irq_records", "excluded_cpu_ids", "irqbalance",
    "continuous_audit_interval_ns", "new_irq_policy",
)
IRQ_RECORD_KEYS = (
    "irq", "affinity_path", "desired_cpu_ids", "source_identity_sha256",
)
IRQBALANCE_KEYS = (
    "service_name", "desired_state", "prior_state_policy",
    "deactivate_action", "activate_action", "sole_authority_policy",
    "restore_exact",
)
TELEMETRY_PLAN_KEYS = (
    "required_cpu_ids", "module_names", "aperf_mperf",
    "frequency_sources", "temperature_source", "throttle_sources",
    "capture_policy", "continuous_temperature_interval_ns",
)
APERF_MPERF_KEYS = (
    "cpu_sources", "reference_frequency_source", "reference_frequency_khz",
    "target_frequency_khz", "minimum_effective_frequency_khz",
    "maximum_effective_frequency_khz", "lower_ratio", "upper_ratio",
    "no_wrap_policy", "maximum_snapshot_skew_ns", "comparison_policy",
    "endpoint_policy",
)
MSR_SOURCE_KEYS = (
    "cpu_id", "path", "source_kind", "aperf_register", "mperf_register",
    "source_identity_sha256",
)
REFERENCE_FREQUENCY_SOURCE_KEYS = (
    "path", "source_kind", "source_identity_sha256",
)
FREQUENCY_SOURCE_KEYS = (
    "cpu_id", "path", "source_kind", "source_identity_sha256",
    "expected_khz",
)
TEMPERATURE_SOURCE_KEYS = (
    "module", "device_path", "name_path", "name_expected", "label_path",
    "label_expected", "input_path", "source_identity_sha256",
    "minimum_millicelsius", "maximum_millicelsius", "sampling_policy",
)
THROTTLE_SOURCE_KEYS = (
    "cpu_id", "provider", "source_kind", "path", "register",
    "source_identity_sha256", "specification_sha256", "value_extraction",
    "counter_width_bits", "semantics", "no_wrap_policy",
)
DMA_LATENCY_KEYS = (
    "path", "desired_latency_us", "owner_policy", "open_policy",
    "descriptor_transfer_policy", "lifetime_policy", "restore_policy",
)
GUARDIAN_KEYS = (
    "policy", "poll_interval_ns", "maximum_observation_gap_ns", "surfaces",
    "control_plane_policy", "descendant_policy", "cpuset_policy",
    "control_drift_policy",
    "irq_drift_policy", "foreign_task_policy", "thermal_policy",
    "failure_latch_policy", "evidence_policy",
    "minimum_observation_spacing_ns", "maximum_campaign_duration_ns",
    "maximum_observation_count", "evidence_filename",
    "evidence_storage_policy", "evidence_chain_policy",
)
RESTORATION_KEYS = (
    "policy", "journal_policy", "operation_order", "restore_triggers",
    "verification_policy", "cpuset_cleanup_policy", "parent_fsync_policy",
    "indeterminate_policy",
)

CPUSET_LIFECYCLE = (
    "capture_parent_and_absence", "exclusive_create", "configure_mems",
    "configure_cpus", "verify_effective_masks", "partition_attested_control_plane",
    "move_stabilized_estimator_descendant_threads",
    "guard_descendant_inheritance", "remove_all_members", "remove_group",
    "verify_parent_and_absence_restored",
)
CPU_CONTROL_APPLICATION_ORDER = (
    "capture_all", "attest_control_plane_chain", "load_telemetry_modules",
    "stop_irqbalance",
    "configure_boost", "configure_cpufreq", "disable_idle_states",
    "configure_interrupts", "place_control_plane_threads", "create_cpuset",
    "move_stabilized_estimator_descendant_threads",
    "offline_estimator_smt_siblings", "hold_dma_latency", "start_guardians",
)
GUARDIAN_SURFACES = (
    "boost", "cpu_idle", "cpu_online", "cpufreq", "cpuset_membership",
    "descendant_affinity", "foreign_affinity_eligibility", "interrupt_affinity",
    "interrupt_population", "irqbalance", "temperature", "throttle_counter",
)


def guardian_line_capacity_bytes(
    controlled_cpu_count: int, surface_count: int, telemetry_source_count: int,
) -> int:
    """Return the checked schema/cardinality-derived guardian line capacity."""

    for value, label in (
        (controlled_cpu_count, "controlled CPU count"),
        (surface_count, "guardian surface count"),
        (telemetry_source_count, "guardian telemetry source count"),
    ):
        if type(value) is not int or value <= 0 or value > 4096:
            _fail(label + " is outside the guardian capacity derivation")
    # The coefficients conservatively cover canonical key spelling, u64
    # decimal widths, commas/brackets, the bounded cpuset-membership string,
    # and every CPU ID in an affinity row.  There are two complete thread-row
    # populations (estimator/control), one control-process population, one
    # foreign population, and three repeated exact TID/PID member arrays.
    thread_row = (
        512 + MAX_CPUSET_MEMBERSHIP_BYTES + 8 * controlled_cpu_count
    )
    process_row = 256
    foreign_row = 256 + 8 * controlled_cpu_count
    repeated_identifiers = 3 * 24
    raw = (
        64 * 1024
        + GUARDIAN_POPULATION_ITEM_LIMIT * (
            2 * thread_row + process_row + foreign_row
            + repeated_identifiers
        )
        + surface_count * 256
        + telemetry_source_count * 4096
    )
    quantum = 1024 * 1024
    rounded = ((raw + quantum - 1) // quantum) * quantum
    if rounded > MAX_GUARDIAN_LINE_BYTES:
        _fail("derived guardian line capacity exceeds its implementation cap")
    return rounded
RESTORE_OPERATION_ORDER = (
    "capture_post_telemetry", "stop_guardians", "release_dma_latency",
    "remove_cpuset_membership", "remove_cpuset", "restore_cpu_online",
    "restore_cpu_idle", "restore_cpufreq", "restore_boost",
    "restore_interrupt_affinity", "restore_irqbalance", "restore_modules",
    "validate_exact_prior", "complete_recovery_journal",
)
RESTORE_TRIGGERS = (
    "success", "failure", "verification_exit", "interrupt", "signal",
    "client_death", "helper_recovery_after_helper_death",
)


class TimingProfileError(ValueError):
    """Raised when timing-profile bytes differ from the frozen schema."""


def _fail(message: str) -> None:
    raise TimingProfileError(message)


def _exact(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        _fail(label + " keys differ from the frozen schema")
    return value


def _plain_int(value: Any, label: str, minimum: int = 0, maximum: int = U64_MAX) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        _fail(label + " is outside its integer domain")
    return value


def _text(value: Any, label: str, maximum: int = 4096) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        _fail(label + " is not bounded nonempty UTF-8 text")
    if any(character in value for character in ("\0", "\r", "\n")):
        _fail(label + " contains a forbidden control character")
    return value


def _sha(value: Any, label: str) -> str:
    text = _text(value, label, 64)
    if HEX64.fullmatch(text) is None:
        _fail(label + " is not lowercase SHA-256")
    return text


def _safe_id(value: Any, label: str) -> str:
    text = _text(value, label, 128)
    if SAFE_ID.fullmatch(text) is None:
        _fail(label + " is not a safe identifier")
    return text


def _absolute(value: Any, label: str, prefixes: Sequence[str] = ()) -> str:
    path = _text(value, label)
    if not os.path.isabs(path) or path == os.path.sep or os.path.normpath(path) != path:
        _fail(label + " is not a normalized non-root absolute path")
    if prefixes and not any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in prefixes):
        _fail(label + " is outside its frozen path prefixes")
    return path


def _strict_json(payload: bytes) -> Any:
    if type(payload) is not bytes or not payload or len(payload) > MAX_PROFILE_BYTES:
        _fail("profile byte count is invalid")

    def pairs(values: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in values:
            if key in result:
                _fail("profile contains a duplicate JSON key")
            result[key] = value
        return result

    def constant(token: str) -> None:
        _fail("profile contains non-JSON numeric constant " + token)

    try:
        document = payload.decode("utf-8", "strict")
        value = json.loads(
            document,
            object_pairs_hook=pairs,
            parse_float=lambda _: _fail("profile contains a floating-point number"),
            parse_constant=constant,
        )
    except TimingProfileError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TimingProfileError("profile is not strict UTF-8 JSON") from exc
    canonical = json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"
    if payload != canonical:
        _fail("profile bytes are not the unique canonical JSON spelling")
    return value


def canonical_profile_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode and validate one profile in its only permitted byte spelling."""

    payload = json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"
    load_profile_bytes(payload)
    return payload


def _environment(value: Any, label: str) -> Mapping[str, str]:
    if type(value) is not dict or not value:
        _fail(label + " must be one nonempty exact mapping")
    for name, item in value.items():
        if type(name) is not str or ENV_NAME.fullmatch(name) is None:
            _fail(label + " contains an invalid environment name")
        _text(item, label + " value", 4096)
    if tuple(value) != tuple(sorted(value)):
        _fail(label + " names are not in canonical sorted order")
    return value


def _argv(value: Any, label: str, *, sudo_prefix: Tuple[str, ...] = ()) -> Tuple[str, ...]:
    if type(value) is not list or not value:
        _fail(label + " must be one nonempty argv list")
    retained = tuple(_text(item, label + " item") for item in value)
    if not os.path.isabs(retained[0]) or os.path.normpath(retained[0]) != retained[0]:
        _fail(label + " executable is not an absolute normalized path")
    if retained[0] in ("/bin/sh", "/bin/bash", "/usr/bin/env"):
        _fail(label + " may not invoke a shell or ambient environment launcher")
    if sudo_prefix and retained[:len(sudo_prefix)] != sudo_prefix:
        _fail(label + " differs from the frozen noninteractive privilege prefix")
    return retained


def _rational(value: Any, expected: Tuple[int, int], label: str) -> None:
    if type(value) is not list or len(value) != 2:
        _fail(label + " is not a two-component rational")
    if tuple(_plain_int(item, label + " component", 1) for item in value) != expected:
        _fail(label + " differs from the frozen exact rational")


def _run_slots(pair_order: Any, slots: Any) -> None:
    if type(pair_order) is not list:
        _fail("pair order is not a list")
    try:
        order = tuple(tuple(pair) for pair in pair_order)
    except TypeError as exc:
        raise TimingProfileError("pair order is not nested") from exc
    if order != PAIR_ORDER:
        _fail("pair order differs from the frozen interleaving")
    if type(slots) is not list or len(slots) != 6:
        _fail("run slots must contain exactly six fresh-process slots")
    expected_modes = tuple(mode for pair in PAIR_ORDER for mode in pair)
    for index, (slot, expected_mode) in enumerate(zip(slots, expected_modes)):
        record = _exact(slot, RUN_SLOT_KEYS, "run slot")
        if (
            _plain_int(record["run_index"], "run index") != index
            or _plain_int(record["timing_pair_index"], "timing-pair index") != index // 2
            or _plain_int(record["position_in_pair"], "position in pair") != index % 2
            or record["mode"] != expected_mode
        ):
            _fail("run slot differs from the frozen six-run interleaving")


def _cpu_list(value: Any, label: str) -> Tuple[int, ...]:
    if type(value) is not list or not value:
        _fail(label + " must be one nonempty CPU list")
    retained = tuple(_plain_int(cpu, label + " CPU", 0, 4095) for cpu in value)
    if retained != tuple(sorted(set(retained))):
        _fail(label + " is not sorted and unique")
    return retained


def _cpu_list_allow_empty(value: Any, label: str) -> Tuple[int, ...]:
    if type(value) is not list:
        _fail(label + " must be one CPU list")
    retained = tuple(_plain_int(cpu, label + " CPU", 0, 4095) for cpu in value)
    if retained != tuple(sorted(set(retained))):
        _fail(label + " is not sorted and unique")
    return retained


def _control_text(value: Any, parser: str, label: str) -> str:
    text = _text(value, label, 4096)
    if parser == "text":
        return text
    if parser == "integer":
        if not text.isdigit() or (len(text) > 1 and text.startswith("0")):
            _fail(label + " is not canonical unsigned decimal")
        _plain_int(int(text), label, 0, U64_MAX)
        return text
    if parser == "cpu_list":
        retained: List[int] = []
        for token in text.split(","):
            pieces = token.split("-")
            if len(pieces) == 1 and pieces[0].isdigit():
                retained.append(int(pieces[0]))
            elif len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
                low, high = map(int, pieces)
                if low > high or high > 4095:
                    _fail(label + " contains an invalid CPU-list range")
                retained.extend(range(low, high + 1))
            else:
                _fail(label + " is not a CPU list")
        if not retained or retained != sorted(set(retained)) or retained[-1] > 4095:
            _fail(label + " CPU list is empty, duplicate, unordered, or unbounded")
        return text
    if parser == "cpu_mask":
        chunks = text.split(",")
        if (
            not chunks or len(chunks) > 128
            or any(
                re.fullmatch(r"[0-9a-f]{8}", chunk) is None
                for chunk in chunks
            )
            or not any(int(chunk, 16) for chunk in chunks)
        ):
            _fail(label + " is not one nonempty canonical CPU mask")
        return text
    _fail(label + " parser is unsupported")


def _integer_list(value: Any, label: str, *, allow_empty: bool = False) -> Tuple[int, ...]:
    if type(value) is not list or (not allow_empty and not value):
        _fail(label + " must be one canonical integer list")
    retained = tuple(_plain_int(item, label + " item") for item in value)
    if retained != tuple(sorted(set(retained))):
        _fail(label + " is not sorted and unique")
    return retained


def _reduced_rational(numerator: int, denominator: int) -> Tuple[int, int]:
    from math import gcd

    divisor = gcd(numerator, denominator)
    return numerator // divisor, denominator // divisor


def _exact_rational(value: Any, expected: Tuple[int, int], label: str) -> None:
    if type(value) is not list or len(value) != 2:
        _fail(label + " is not a two-component rational")
    retained = tuple(_plain_int(item, label + " component", 1) for item in value)
    if retained != expected:
        _fail(label + " differs from its independently derived exact rational")


def _validate_target(value: Any) -> None:
    target = _exact(value, TARGET_KEYS, "target")
    if target["target_class"] != "desktop_x86_64" or target["architecture"] != "x86_64":
        _fail("target is not the approved desktop x86_64 class")
    for key in ("cpu_vendor", "cpu_model_name", "kernel_release"):
        _text(target[key], "target " + key)
    for key in ("cpu_family", "cpu_model", "cpu_stepping"):
        _plain_int(target[key], "target " + key, 0, 65535)
    logical = _plain_int(target["logical_cpu_count"], "logical CPU count", 1, 4096)
    physical = _plain_int(target["physical_core_count"], "physical core count", 1, logical)
    threads = _plain_int(target["threads_per_core"], "threads per core", 1, logical)
    if physical * threads != logical:
        _fail("target topology counts are inconsistent")
    _sha(target["machine_identity_sha256"], "machine identity SHA-256")
    if target["boot_identity_policy"] != "capture_each_campaign_reject_change":
        _fail("boot identity policy differs from the frozen policy")


def _validate_input(value: Any) -> None:
    source = _exact(value, INPUT_KEYS, "input")
    if _plain_int(source["sequence_index"], "sequence index") > 2:
        _fail("sequence index is outside CP2")
    _safe_id(source["sequence_id"], "sequence ID")
    _plain_int(source["frozen_offset_ns"], "frozen offset nanoseconds")
    _sha(source["bag_sha256"], "bag SHA-256")
    if source["ground_truth_access"] is not False:
        _fail("timing input may not access ground truth")
    registry_read_count = _plain_int(
        source["registry_read_count"], "input registry read count", 0, 1,
    )
    kind = source["input_kind"]
    if kind == "recorded_frozen":
        if registry_read_count != 1 or source["synthetic_origin_sha256"] is not None:
            _fail("recorded timing input access/provenance policy differs")
    elif kind == "synthetic_protecting":
        if (
            registry_read_count != 0
            or type(source["sequence_id"]) is not str
            or not source["sequence_id"].startswith("synthetic_")
        ):
            _fail("synthetic timing input is not unmistakably data-independent")
        _sha(source["synthetic_origin_sha256"], "synthetic input origin SHA-256")
    else:
        _fail("timing input kind is unsupported")


def _validate_pairing(value: Any) -> None:
    pairing = _exact(value, PAIRING_KEYS, "pairing")
    _run_slots(pairing["pair_order"], pairing["run_slots"])
    if pairing["warmup_ns"] != 60_000_000_000:
        _fail("warm-up duration differs from sixty seconds")
    if pairing["common_payload_domain"] != COMMON_DOMAIN_TEXT:
        _fail("common payload domain differs")
    _rational(pairing["quantile_p50"], (1, 2), "p50 quantile")
    _rational(pairing["quantile_p95"], (19, 20), "p95 quantile")
    _rational(pairing["p50_ratio_limit"], (11, 10), "p50 ratio limit")
    _rational(pairing["p95_ratio_limit"], (23, 20), "p95 ratio limit")
    if pairing["every_pair_must_pass"] is not True:
        _fail("every-pair pass policy is not enabled")


def _validate_runtime(value: Any, target: Mapping[str, Any]) -> None:
    runtime = _exact(value, RUNTIME_KEYS, "runtime")
    cpus = _cpu_list(runtime["cpu_ids"], "runtime CPU IDs")
    if cpus[-1] >= target["logical_cpu_count"]:
        _fail("runtime CPU ID exceeds target topology")
    groups_value = runtime["smt_sibling_groups"]
    if type(groups_value) is not list or not groups_value:
        _fail("SMT sibling groups are absent")
    flattened: List[int] = []
    retained_groups: List[Tuple[int, ...]] = []
    for group in groups_value:
        siblings = _cpu_list(group, "SMT sibling group")
        if len(siblings) != target["threads_per_core"]:
            _fail("SMT sibling group size differs from target topology")
        flattened.extend(siblings)
        retained_groups.append(siblings)
    complete_topology = tuple(range(target["logical_cpu_count"]))
    if (
        tuple(sorted(flattened)) != complete_topology
        or len(flattened) != len(set(flattened))
    ):
        _fail("SMT sibling groups do not partition the complete logical topology")
    if retained_groups != sorted(retained_groups):
        _fail("SMT sibling groups are not in canonical CPU order")
    if runtime["affinity_policy"] != "inherited_exact_set_with_continuous_process_tree_audit":
        _fail("runtime affinity policy differs")
    environment = _environment(runtime["environment"], "runtime environment")
    required_single_thread = {
        "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }
    if any(environment.get(key) != expected for key, expected in required_single_thread.items()):
        _fail("runtime environment does not freeze numerical libraries to one thread")
    if runtime["scaling_driver"] not in ("acpi-cpufreq", "amd-pstate"):
        _fail("runtime scaling driver is unsupported")
    if runtime["governor"] in ("ondemand", "schedutil") or runtime["governor"] != "performance":
        _fail("runtime governor is not frozen performance")
    minimum = _plain_int(runtime["minimum_frequency_khz"], "minimum frequency", 1)
    maximum = _plain_int(runtime["maximum_frequency_khz"], "maximum frequency", 1)
    if minimum != maximum:
        _fail("runtime minimum and maximum frequencies are not fixed")
    frequencies = runtime["accepted_current_frequency_khz"]
    if type(frequencies) is not list or not frequencies:
        _fail("accepted current frequencies are absent")
    retained_frequencies = tuple(
        _plain_int(item, "accepted current frequency", 1)
        for item in frequencies
    )
    if retained_frequencies != (minimum,):
        _fail(
            "accepted current frequencies must be the singleton frozen "
            "minimum/maximum frequency"
        )
    if runtime["boost_enabled"] is not False:
        _fail("runtime boost must be disabled")
    states = runtime["disabled_idle_state_names"]
    if type(states) is not list or not states or tuple(states) != tuple(sorted(set(states))):
        _fail("disabled idle-state names are not sorted and unique")
    for state_name in states:
        _text(state_name, "idle-state name", 64)
    if runtime["irqbalance_state"] != "inactive":
        _fail("irqbalance must be inactive")
    _plain_int(runtime["dma_latency_us"], "DMA latency", 0, 1_000_000)


def _validate_cpu_plan(
    value: Any, target: Mapping[str, Any], runtime: Mapping[str, Any],
) -> Mapping[str, Any]:
    plan = _exact(value, CPU_PLAN_KEYS, "CPU plan")
    controlled = _cpu_list(plan["controlled_cpu_ids"], "controlled CPU IDs")
    logical = target["logical_cpu_count"]
    if controlled != tuple(range(logical)):
        _fail("controlled CPU IDs do not cover the complete target topology")
    estimator = _cpu_list(plan["estimator_cpu_ids"], "estimator CPU IDs")
    helper = _cpu_list(plan["helper_cpu_ids"], "helper CPU IDs")
    housekeeping = _cpu_list(plan["housekeeping_cpu_ids"], "housekeeping CPU IDs")
    online = _cpu_list(plan["online_cpu_ids"], "online CPU IDs")
    offline = _cpu_list_allow_empty(plan["offline_cpu_ids"], "offline CPU IDs")
    role_sets = (set(estimator), set(helper), set(housekeeping), set(offline))
    if len(estimator) != 1:
        _fail("formal serial estimator CPU role is not a singleton")
    if any(role_sets[left].intersection(role_sets[right]) for left in range(4) for right in range(left + 1, 4)):
        _fail("estimator/helper/housekeeping/offline CPU roles overlap")
    if set().union(*role_sets) != set(controlled):
        _fail("CPU roles do not partition the complete controlled population")
    if set(online) != set(estimator).union(helper, housekeeping):
        _fail("online CPU population differs from the three live CPU roles")
    if tuple(runtime["cpu_ids"]) != estimator:
        _fail("runtime affinity/telemetry CPUs differ from the serial estimator role")
    if plan["role_partition_policy"] != "disjoint_complete_logical_cpu_partition":
        _fail("CPU role partition policy differs")
    if plan["estimator_smt_policy"] != "one_estimator_logical_cpu_all_its_other_siblings_offline":
        _fail("estimator SMT policy differs")
    _sha(plan["topology_source_sha256"], "CPU topology source SHA-256")

    cores_value = plan["smt_cores"]
    if type(cores_value) is not list or len(cores_value) != target["physical_core_count"]:
        _fail("SMT core inventory differs from the complete physical-core count")
    retained_core_ids: List[int] = []
    retained_siblings: List[int] = []
    estimator_core_count = 0
    cpu_to_core: Dict[int, int] = {}
    groups: List[Tuple[int, ...]] = []
    for item in cores_value:
        core = _exact(item, SMT_CORE_KEYS, "SMT core")
        core_id = _plain_int(core["physical_core_id"], "physical core ID", 0, 65535)
        siblings = _cpu_list(core["sibling_cpu_ids"], "SMT core siblings")
        core_online = _cpu_list_allow_empty(core["online_cpu_ids"], "SMT core online CPUs")
        core_offline = _cpu_list_allow_empty(core["offline_cpu_ids"], "SMT core offline CPUs")
        if len(siblings) != target["threads_per_core"]:
            _fail("SMT core sibling count differs from target threads-per-core")
        if set(core_online).intersection(core_offline) or set(core_online).union(core_offline) != set(siblings):
            _fail("SMT core online/offline disposition does not partition its siblings")
        if set(core_online) != set(siblings).intersection(online) or set(core_offline) != set(siblings).intersection(offline):
            _fail("SMT core disposition differs from the global online/offline plan")
        estimator_on_core = set(siblings).intersection(estimator)
        if estimator_on_core:
            estimator_core_count += 1
            if (
                estimator_on_core != set(core_online)
                or core["sibling_disposition"]
                != "estimator_primary_only_other_siblings_offline"
            ):
                _fail("estimator physical core is not isolated from every SMT sibling")
        elif core["sibling_disposition"] != "non_estimator_siblings_online":
            _fail("non-estimator SMT core disposition differs")
        retained_core_ids.append(core_id)
        retained_siblings.extend(siblings)
        groups.append(siblings)
        for cpu in siblings:
            cpu_to_core[cpu] = core_id
    if retained_core_ids != sorted(set(retained_core_ids)):
        _fail("physical core IDs are not sorted and unique")
    if tuple(sorted(retained_siblings)) != controlled or len(retained_siblings) != len(set(retained_siblings)):
        _fail("SMT cores do not partition the controlled CPU population")
    if estimator_core_count != 1:
        _fail("estimator CPU does not belong to exactly one isolated physical core")
    if tuple(sorted(groups)) != tuple(sorted(tuple(group) for group in runtime["smt_sibling_groups"])):
        _fail("runtime SMT inventory differs from the authoritative CPU plan")
    return {**plan, "_cpu_to_core": cpu_to_core}


def _validate_privileged_helper(value: Any, cpu_plan: Mapping[str, Any]) -> None:
    helper = _exact(value, PRIVILEGED_HELPER_KEYS, "privileged helper")
    if helper["authority_policy"] != "sole_privileged_mutation_observation_restoration_authority":
        _fail("privileged helper is not the sole formal authority")
    for key in (
        "plan_sha256", "source_sha256", "protocol_core_sha256",
        "root_launcher_sha256", "import_closure_sha256", "sudoers_sha256",
    ):
        _sha(helper[key], "privileged helper " + key)
    trusted_root = _absolute(helper["trusted_root"], "helper trusted root")
    launcher = _absolute(helper["root_launcher_path"], "root helper launcher")
    if not launcher.startswith(trusted_root.rstrip("/") + "/"):
        _fail("root helper launcher lies outside its trusted root")
    _absolute(
        helper["journal_directory"], "root helper journal directory",
        ("/var/lib", "/run"),
    )
    invocation = _argv(helper["invocation_argv"], "root helper invocation")
    if invocation != (
        "/usr/bin/sudo", "-n", "-C", "4", "--", launcher,
    ):
        _fail(
            "root helper invocation is not exact noninteractive sudo with "
            "only the AF_UNIX SOCK_SEQPACKET descriptor 3 preserved"
        )
    if tuple(_cpu_list(helper["helper_cpu_ids"], "root helper CPU IDs")) != tuple(cpu_plan["helper_cpu_ids"]):
        _fail("root helper CPU role differs from the CPU plan")
    if _plain_int(helper["root_owner_uid"], "root helper owner UID") != 0:
        _fail("root helper installed closure is not root-owned")
    if helper["socket_family"] != "AF_UNIX" or helper["socket_type"] != "SOCK_SEQPACKET":
        _fail("root helper socket contract differs")
    exact_policies = {
        "peer_credential_policy": "exact_so_peercred_pid_uid_gid",
        "nonce_policy": "fresh_helper_and_client_256_bit_nonces_each_session",
        "client_selection_policy": "client_selects_no_paths_commands_modules_services_registers_or_controls",
        "descriptor_transfer_policy": "no_privileged_descriptor_transfer_to_client",
        "production_policy": "pre_python_root_owned_single_link_held_source_profile_launcher_closure_required",
    }
    if any(helper[key] != expected for key, expected in exact_policies.items()):
        _fail("root helper protocol/production policy differs")
    if _plain_int(
        helper["installed_closure_file_count"],
        "installed helper closure file count", 1, 4096,
    ) != 4:
        _fail("installed helper closure is not three sources plus one profile")


def _validate_cpuset(value: Any, cpu_plan: Mapping[str, Any]) -> None:
    cpuset = _exact(value, CPUSET_KEYS, "cpuset plan")
    if cpuset["cgroup_version"] != "v1_cpuset":
        _fail("cpuset plan is not bound to the inventoried cgroup-v1 controller")
    _sha(cpuset["hierarchy_identity_sha256"], "cpuset hierarchy identity SHA-256")
    mount = _absolute(cpuset["mount_path"], "cpuset mount", ("/sys/fs/cgroup",))
    parent = _absolute(cpuset["parent_path"], "cpuset parent", (mount,))
    group_name = _safe_id(cpuset["group_name"], "cpuset group name")
    group = _absolute(cpuset["group_path"], "cpuset group", (parent,))
    if group != os.path.join(parent, group_name):
        _fail("cpuset group path is not the exact named child of its parent")
    expected_paths = {
        "cpus_path": "cpuset.cpus", "mems_path": "cpuset.mems",
        "tasks_path": "tasks", "effective_cpus_path": "cpuset.effective_cpus",
        "effective_mems_path": "cpuset.effective_mems",
    }
    for key, basename in expected_paths.items():
        path = _absolute(cpuset[key], "cpuset " + key, (group,))
        if path != os.path.join(group, basename):
            _fail("cpuset " + key + " differs from its exact lifecycle path")
    if tuple(_cpu_list(cpuset["desired_cpu_ids"], "cpuset desired CPU IDs")) != tuple(cpu_plan["estimator_cpu_ids"]):
        _fail("cpuset CPU mask differs from the estimator CPU role")
    _integer_list(cpuset["desired_memory_nodes"], "cpuset memory nodes")
    exact_policies = {
        "create_policy": "descriptor_relative_exclusive_create_reject_preexisting",
        "membership_policy": "move_exact_peer_all_threads_before_descendant_spawn",
        "effective_mask_policy": "require_exact_cpus_and_mems_before_and_during_each_run",
        "remove_policy": "remove_all_members_then_rmdir_and_verify_prior_absence",
    }
    if any(cpuset[key] != expected for key, expected in exact_policies.items()):
        _fail("cpuset lifecycle policy differs")
    if type(cpuset["lifecycle"]) is not list or tuple(cpuset["lifecycle"]) != CPUSET_LIFECYCLE:
        _fail("cpuset lifecycle order differs")
    _plain_int(cpuset["guardian_poll_interval_ns"], "cpuset guardian poll interval", 1, 100_000_000)


def _validate_cpu_controls(
    value: Any, target: Mapping[str, Any], runtime: Mapping[str, Any],
    cpu_plan: Mapping[str, Any],
) -> None:
    controls = _exact(value, CPU_CONTROLS_KEYS, "typed CPU controls")
    if type(controls["application_order"]) is not list or tuple(controls["application_order"]) != CPU_CONTROL_APPLICATION_ORDER:
        _fail("typed CPU-control application order differs")
    boost = _exact(controls["boost"], BOOST_KEYS, "boost control")
    _absolute(boost["path"], "boost path", ("/sys/devices/system/cpu",))
    _sha(boost["source_identity_sha256"], "boost source identity SHA-256")
    if (
        boost["parser"] != "integer" or boost["desired_text"] != "0"
        or boost["desired_enabled"] is not False or boost["restore_exact"] is not True
    ):
        _fail("boost control does not freeze disabled state with exact restoration")

    policies_value = controls["policies"]
    if type(policies_value) is not list or not policies_value:
        _fail("cpufreq policy inventory is absent")
    policy_ids: List[str] = []
    policy_cpus: List[int] = []
    policy_by_cpu: Dict[int, str] = {}
    for item in policies_value:
        policy = _exact(item, CPUFREQ_POLICY_KEYS, "cpufreq policy")
        policy_id = _safe_id(policy["policy_id"], "cpufreq policy ID")
        cpus = _cpu_list(policy["cpu_ids"], "cpufreq policy CPUs")
        _sha(policy["source_identity_sha256"], "cpufreq policy source identity SHA-256")
        if policy["driver"] != runtime["scaling_driver"]:
            _fail("cpufreq policy driver differs from the frozen runtime")
        prefix = "/sys/devices/system/cpu/cpufreq/" + policy_id
        exact_paths = {
            "driver_path": "scaling_driver", "governor_path": "scaling_governor",
            "minimum_frequency_path": "scaling_min_freq",
            "maximum_frequency_path": "scaling_max_freq",
        }
        for key, basename in exact_paths.items():
            path = _absolute(policy[key], "cpufreq policy " + key, (prefix,))
            if path != prefix + "/" + basename:
                _fail("cpufreq policy path differs from its exact policy file")
        if (
            policy["desired_governor"] != runtime["governor"]
            or policy["desired_minimum_frequency_khz"] != runtime["minimum_frequency_khz"]
            or policy["desired_maximum_frequency_khz"] != runtime["maximum_frequency_khz"]
            or policy["restore_exact"] is not True
        ):
            _fail("cpufreq policy desired state differs from the frozen runtime")
        policy_ids.append(policy_id)
        policy_cpus.extend(cpus)
        for cpu in cpus:
            if cpu in policy_by_cpu:
                _fail("one CPU belongs to more than one cpufreq policy")
            policy_by_cpu[cpu] = policy_id
    if policy_ids != sorted(set(policy_ids)):
        _fail("cpufreq policy IDs are not sorted and unique")
    if tuple(sorted(policy_cpus)) != tuple(cpu_plan["controlled_cpu_ids"]):
        _fail("cpufreq policies do not partition the controlled CPU inventory")

    core_by_cpu = cpu_plan["_cpu_to_core"]
    estimator = set(cpu_plan["estimator_cpu_ids"])
    helper = set(cpu_plan["helper_cpu_ids"])
    housekeeping = set(cpu_plan["housekeeping_cpu_ids"])
    offline = set(cpu_plan["offline_cpu_ids"])
    records = controls["cpus"]
    if type(records) is not list or len(records) != target["logical_cpu_count"]:
        _fail("per-CPU control inventory is incomplete")
    retained_cpu_ids: List[int] = []
    disabled_names = set()
    for item in records:
        record = _exact(item, CPU_CONTROL_KEYS, "per-CPU control")
        cpu = _plain_int(record["cpu_id"], "per-CPU control ID", 0, target["logical_cpu_count"] - 1)
        expected_role = (
            "estimator" if cpu in estimator else "helper" if cpu in helper
            else "housekeeping" if cpu in housekeeping else "offline_smt_sibling"
        )
        physical_core_id = _plain_int(
            record["physical_core_id"], "per-CPU physical core ID", 0, 65535,
        )
        if record["role"] != expected_role or physical_core_id != core_by_cpu[cpu]:
            _fail("per-CPU role/core binding differs from the topology plan")
        expected_online = cpu not in offline
        if record["expected_online"] is not expected_online:
            _fail("per-CPU expected online state differs from the SMT disposition")
        online_control_kind = record["online_control_kind"]
        _sha(
            record["online_source_identity_sha256"],
            "CPU online/immutability source identity SHA-256",
        )
        if online_control_kind == "sysfs_writable_integer":
            online_path = _absolute(
                record["online_path"], "CPU online path",
                ("/sys/devices/system/cpu",),
            )
            if online_path != "/sys/devices/system/cpu/cpu{}/online".format(cpu):
                _fail("CPU online path differs from its CPU")
        elif online_control_kind == "immutable_always_online_no_control_file":
            if (
                record["online_path"] is not None
                or record["expected_online"] is not True
            ):
                _fail("immutable CPU online disposition is not exact")
        else:
            _fail("CPU online control kind is unsupported")
        if record["cpufreq_policy_id"] != policy_by_cpu[cpu]:
            _fail("per-CPU cpufreq policy join differs")
        _sha(record["idle_inventory_sha256"], "CPU-idle inventory SHA-256")
        states = record["idle_states"]
        if type(states) is not list or not states:
            _fail("complete per-CPU idle-state inventory is absent")
        indices: List[int] = []
        names = set()
        for state_value in states:
            state = _exact(state_value, IDLE_STATE_KEYS, "CPU idle state")
            index = _plain_int(state["state_index"], "CPU idle-state index", 0, 4095)
            name = _text(state["name"], "CPU idle-state name", 64)
            prefix = "/sys/devices/system/cpu/cpu{}/cpuidle/state{}".format(cpu, index)
            if (
                _absolute(state["name_path"], "CPU idle-state name path", (prefix,))
                != prefix + "/name"
                or _absolute(state["disable_path"], "CPU idle-state disable path", (prefix,))
                != prefix + "/disable"
            ):
                _fail("CPU idle-state paths differ from their CPU/index")
            if type(state["desired_disabled"]) is not bool or state["restore_exact"] is not True:
                _fail("CPU idle-state desired/restoration policy is invalid")
            if state["desired_disabled"]:
                disabled_names.add(name)
            indices.append(index)
            names.add(name)
        if indices != sorted(set(indices)) or len(names) != len(states):
            _fail("CPU idle-state indices/names are not unique and ordered")
        if cpu in estimator and not any(state["desired_disabled"] for state in states):
            _fail("estimator CPU has no disabled idle state")
        if record["restore_exact"] is not True:
            _fail("per-CPU control does not require exact restoration")
        retained_cpu_ids.append(cpu)
    if retained_cpu_ids != list(cpu_plan["controlled_cpu_ids"]):
        _fail("per-CPU controls are not in complete canonical CPU order")
    if tuple(sorted(disabled_names)) != tuple(runtime["disabled_idle_state_names"]):
        _fail("runtime disabled idle-state names differ from the typed inventory")


def _validate_interrupts(value: Any, cpu_plan: Mapping[str, Any], runtime: Mapping[str, Any]) -> None:
    interrupts = _exact(value, INTERRUPT_KEYS, "interrupt plan")
    _sha(interrupts["inventory_sha256"], "interrupt inventory SHA-256")
    _sha(interrupts["default_source_identity_sha256"], "default interrupt affinity source identity SHA-256")
    if interrupts["inventory_policy"] != "complete_numeric_irq_population_reject_add_remove":
        _fail("interrupt inventory policy differs")
    if interrupts["new_irq_policy"] != "latched_failure_before_next_estimator_operation":
        _fail("new-IRQ policy differs")
    default_path = _absolute(
        interrupts["default_affinity_path"], "default IRQ affinity path", ("/proc/irq",),
    )
    if default_path != "/proc/irq/default_smp_affinity":
        _fail("default IRQ affinity path differs")
    housekeeping = set(cpu_plan["housekeeping_cpu_ids"])
    default_cpus = _cpu_list(interrupts["default_affinity_cpu_ids"], "default IRQ affinity CPUs")
    if not set(default_cpus).issubset(housekeeping):
        _fail("default IRQ affinity is not confined to housekeeping CPUs")
    expected_excluded = tuple(
        cpu for cpu in cpu_plan["controlled_cpu_ids"] if cpu not in housekeeping
    )
    if tuple(_cpu_list(interrupts["excluded_cpu_ids"], "IRQ-excluded CPU IDs")) != expected_excluded:
        _fail("IRQ-excluded population differs from every non-housekeeping CPU")
    records = interrupts["irq_records"]
    if type(records) is not list or not records:
        _fail("complete per-IRQ affinity inventory is absent")
    irq_numbers: List[int] = []
    for item in records:
        record = _exact(item, IRQ_RECORD_KEYS, "IRQ record")
        irq = _plain_int(record["irq"], "IRQ number", 0, (1 << 31) - 1)
        path = _absolute(record["affinity_path"], "IRQ affinity path", ("/proc/irq",))
        if path != "/proc/irq/{}/smp_affinity_list".format(irq):
            _fail("per-IRQ affinity path differs from its IRQ number")
        desired = _cpu_list(record["desired_cpu_ids"], "per-IRQ desired CPU IDs")
        if not set(desired).issubset(housekeeping):
            _fail("per-IRQ affinity is not confined to housekeeping CPUs")
        _sha(record["source_identity_sha256"], "IRQ source identity SHA-256")
        irq_numbers.append(irq)
    if irq_numbers != sorted(set(irq_numbers)):
        _fail("IRQ records are not sorted and unique")
    irqbalance = _exact(interrupts["irqbalance"], IRQBALANCE_KEYS, "irqbalance control")
    expected_irqbalance = {
        "service_name": "irqbalance", "desired_state": "inactive",
        "prior_state_policy": "capture_exact_active_state_before_stop",
        "deactivate_action": "helper_systemctl_stop_irqbalance",
        "activate_action": "helper_systemctl_start_irqbalance_if_preexisting_active",
        "sole_authority_policy": "privileged_helper_only", "restore_exact": True,
    }
    if any(irqbalance[key] != expected for key, expected in expected_irqbalance.items()):
        _fail("irqbalance control policy differs")
    if runtime["irqbalance_state"] != "inactive":
        _fail("runtime irqbalance state differs from the typed interrupt plan")
    _plain_int(
        interrupts["continuous_audit_interval_ns"], "interrupt audit interval",
        1, 100_000_000,
    )


def _validate_telemetry_plan(
    value: Any, cpu_plan: Mapping[str, Any], runtime: Mapping[str, Any],
    legacy_controls: Mapping[str, Any], observations: Sequence[Mapping[str, Any]],
) -> None:
    telemetry = _exact(value, TELEMETRY_PLAN_KEYS, "telemetry plan")
    required_cpus = _cpu_list(telemetry["required_cpu_ids"], "telemetry required CPU IDs")
    if required_cpus != tuple(cpu_plan["estimator_cpu_ids"]):
        _fail("telemetry CPU population differs from the estimator role")
    if telemetry["module_names"] != ["k10temp", "msr"]:
        _fail("telemetry module population differs from the approved exact set")
    module_actions = legacy_controls["module_actions"]
    if sorted(action["name"] for action in module_actions) != telemetry["module_names"]:
        _fail("legacy module actions differ from the typed telemetry module set")
    if telemetry["capture_policy"] != "helper_raw_pre_post_for_each_of_six_runs_plus_continuous_temperature":
        _fail("telemetry capture policy differs")
    continuous_interval = _plain_int(
        telemetry["continuous_temperature_interval_ns"],
        "continuous temperature interval", 1, 1_000_000_000,
    )

    counters = _exact(telemetry["aperf_mperf"], APERF_MPERF_KEYS, "APERF/MPERF plan")
    sources = counters["cpu_sources"]
    if type(sources) is not list or len(sources) != len(required_cpus):
        _fail("APERF/MPERF source population differs from the required CPUs")
    retained_cpus: List[int] = []
    for value_source in sources:
        source = _exact(value_source, MSR_SOURCE_KEYS, "APERF/MPERF CPU source")
        cpu = _plain_int(source["cpu_id"], "APERF/MPERF CPU ID", 0, 4095)
        path = _absolute(source["path"], "APERF/MPERF MSR path", ("/dev/cpu",))
        if (
            path != "/dev/cpu/{}/msr".format(cpu)
            or source["source_kind"] != "msr_u64_le"
            or source["aperf_register"] != 0xE8
            or source["mperf_register"] != 0xE7
        ):
            _fail("APERF/MPERF CPU source path/register semantics differ")
        _sha(source["source_identity_sha256"], "APERF/MPERF source identity SHA-256")
        retained_cpus.append(cpu)
    if tuple(retained_cpus) != required_cpus:
        _fail("APERF/MPERF CPU sources are not in exact required-CPU order")
    reference_source = _exact(
        counters["reference_frequency_source"], REFERENCE_FREQUENCY_SOURCE_KEYS,
        "MPERF reference-frequency source",
    )
    _absolute(
        reference_source["path"], "MPERF reference-frequency path",
        ("/sys/devices/system/cpu",),
    )
    if reference_source["source_kind"] != "text_integer_khz":
        _fail("MPERF reference-frequency source kind differs")
    _sha(
        reference_source["source_identity_sha256"],
        "MPERF reference-frequency source identity SHA-256",
    )
    reference = _plain_int(counters["reference_frequency_khz"], "MPERF reference frequency", 1)
    target = _plain_int(counters["target_frequency_khz"], "target effective frequency", 1)
    minimum = _plain_int(
        counters["minimum_effective_frequency_khz"], "minimum effective frequency", 1,
    )
    maximum = _plain_int(
        counters["maximum_effective_frequency_khz"], "maximum effective frequency", 1,
    )
    if minimum > target or target > maximum:
        _fail("target effective frequency lies outside its exact inclusive bounds")
    if target != runtime["minimum_frequency_khz"] or target != runtime["maximum_frequency_khz"]:
        _fail("APERF/MPERF target differs from the fixed runtime frequency")
    _exact_rational(counters["lower_ratio"], _reduced_rational(minimum, reference), "lower APERF/MPERF ratio")
    _exact_rational(counters["upper_ratio"], _reduced_rational(maximum, reference), "upper APERF/MPERF ratio")
    if counters["no_wrap_policy"] != "reject_nonincreasing_u64_counter":
        _fail("APERF/MPERF no-wrap policy differs")
    _plain_int(counters["maximum_snapshot_skew_ns"], "APERF/MPERF snapshot skew", 1, 1_000_000_000)
    if (
        counters["comparison_policy"]
        != "f_low_times_delta_m_le_f_ref_times_delta_a_le_f_high_times_delta_m"
        or counters["endpoint_policy"] != "inclusive_exact_integer_cross_products"
    ):
        _fail("APERF/MPERF comparison policy differs")

    frequency_sources = telemetry["frequency_sources"]
    if type(frequency_sources) is not list or len(frequency_sources) != len(required_cpus):
        _fail("per-required-CPU frequency source population differs")
    frequency_by_cpu: Dict[int, Mapping[str, Any]] = {}
    for source_value in frequency_sources:
        source = _exact(source_value, FREQUENCY_SOURCE_KEYS, "frequency source")
        cpu = _plain_int(source["cpu_id"], "frequency source CPU", 0, 4095)
        path = _absolute(
            source["path"], "per-CPU current-frequency path",
            ("/sys/devices/system/cpu",),
        )
        if (
            path != "/sys/devices/system/cpu/cpu{}/cpufreq/scaling_cur_freq".format(cpu)
            or source["source_kind"] != "text_integer_khz"
            or source["expected_khz"] != target
            or cpu in frequency_by_cpu
        ):
            _fail("per-CPU frequency source identity/expectation differs")
        _sha(source["source_identity_sha256"], "frequency source identity SHA-256")
        frequency_by_cpu[cpu] = source
    if tuple(frequency_by_cpu) != required_cpus:
        _fail("frequency sources are not in exact required-CPU order")

    temperature = _exact(
        telemetry["temperature_source"], TEMPERATURE_SOURCE_KEYS,
        "k10temp temperature source",
    )
    if temperature["module"] != "k10temp" or temperature["name_expected"] != "k10temp":
        _fail("temperature source is not explicitly bound to k10temp")
    device = _absolute(
        temperature["device_path"], "k10temp device path", ("/sys/devices",),
    )
    for key, basename_suffix in (
        ("name_path", "name"), ("label_path", None), ("input_path", None),
    ):
        path = _absolute(temperature[key], "k10temp " + key, (device,))
        if os.path.dirname(path) != device or (basename_suffix is not None and os.path.basename(path) != basename_suffix):
            _fail("k10temp source path does not identify the exact bound device file")
    label_basename = os.path.basename(temperature["label_path"])
    input_basename = os.path.basename(temperature["input_path"])
    if (
        not re.fullmatch(r"temp[0-9]+_label", label_basename)
        or input_basename != label_basename[:-len("_label")] + "_input"
    ):
        _fail("k10temp label/input files are not one exact sensor pair")
    _text(temperature["label_expected"], "k10temp expected label", 64)
    _sha(temperature["source_identity_sha256"], "k10temp source identity SHA-256")
    thermal_minimum = _plain_int(
        temperature["minimum_millicelsius"], "minimum permitted temperature",
        -273_150, 1_000_000,
    )
    thermal_maximum = _plain_int(
        temperature["maximum_millicelsius"], "maximum permitted temperature",
        -273_150, 1_000_000,
    )
    if thermal_minimum >= thermal_maximum:
        _fail("k10temp permitted temperature interval is empty")
    if temperature["sampling_policy"] != "continuous_helper_read_bound_sensor":
        _fail("k10temp sampling policy differs")

    throttle_sources = telemetry["throttle_sources"]
    if type(throttle_sources) is not list or len(throttle_sources) != len(required_cpus):
        _fail("AMD throttle source population differs from the required CPUs")
    throttle_cpus: List[int] = []
    for source_value in throttle_sources:
        source = _exact(source_value, THROTTLE_SOURCE_KEYS, "AMD throttle source")
        cpu = _plain_int(source["cpu_id"], "AMD throttle source CPU", 0, 4095)
        path = _absolute(
            source["path"], "AMD throttle source path",
            ("/dev/cpu", "/sys/devices/system/cpu", "/sys/devices/platform", "/proc"),
        )
        if source["provider"] != "amd" or source["semantics"] != "monotonic_thermal_throttle_event_counter":
            _fail("throttle source lacks exact AMD monotonic-event semantics")
        if source["no_wrap_policy"] != "reject_decrease_or_wrap":
            _fail("AMD throttle no-wrap policy differs")
        if _plain_int(
            source["counter_width_bits"], "AMD throttle counter width", 64, 64,
        ) != 64:
            _fail("AMD throttle counter is not a complete u64 value")
        if source["source_kind"] == "msr_u64_le":
            if path != "/dev/cpu/{}/msr".format(cpu):
                _fail("AMD throttle MSR path differs from its CPU")
            _plain_int(source["register"], "AMD throttle MSR register", 0, U64_MAX)
            if source["value_extraction"] != "full_u64_little_endian_pread":
                _fail("AMD throttle MSR extraction semantics differ")
        elif source["source_kind"] == "text_u64":
            if (
                source["register"] is not None
                or source["value_extraction"] != "canonical_ascii_decimal_u64_one_line"
                or "/cpu{}/".format(cpu) not in path
            ):
                _fail("text AMD throttle source identity/extraction differs")
        else:
            _fail("AMD throttle source kind is unsupported")
        _sha(source["source_identity_sha256"], "AMD throttle source identity SHA-256")
        _sha(source["specification_sha256"], "AMD throttle specification SHA-256")
        throttle_cpus.append(cpu)
    if tuple(throttle_cpus) != required_cpus:
        _fail("AMD throttle sources are not in exact required-CPU order")

    thermal_observations = [
        item for item in observations
        if item["parser"] == "hwmon_temperature_millicelsius"
    ]
    if (
        len(thermal_observations) != 1
        or thermal_observations[0]["argv"][-1] != temperature["input_path"]
        or thermal_observations[0]["expected"]
        != {"minimum": thermal_minimum, "maximum": thermal_maximum}
    ):
        _fail("legacy thermal observation differs from the typed k10temp source")
    frequency_observations = [
        item for item in observations
        if item["parser"] == "integer"
        and item["observation_id"].startswith("frequency_cpu_")
    ]
    if len(frequency_observations) != len(required_cpus):
        _fail("legacy per-CPU frequency observation population differs")
    for observation, cpu in zip(frequency_observations, required_cpus):
        if (
            observation["observation_id"] != "frequency_cpu_{}".format(cpu)
            or observation["argv"][-1] != frequency_by_cpu[cpu]["path"]
            or observation["expected"] != target
        ):
            _fail("legacy frequency observation differs from its typed CPU source")
    if continuous_interval <= 0:  # domain is checked above; retain explicit contract join.
        _fail("continuous temperature interval is invalid")


def _validate_dma_latency(value: Any, runtime: Mapping[str, Any]) -> None:
    dma = _exact(value, DMA_LATENCY_KEYS, "DMA-latency plan")
    if dma["path"] != "/dev/cpu_dma_latency":
        _fail("DMA-latency device path differs")
    desired_latency = _plain_int(
        dma["desired_latency_us"], "DMA-latency desired value", 0, 1_000_000,
    )
    if desired_latency != runtime["dma_latency_us"]:
        _fail("DMA-latency value differs from the frozen runtime")
    exact = {
        "owner_policy": "privileged_helper_is_sole_descriptor_owner",
        "open_policy": "open_rdwr_cloexec_write_exact_i32_hold_open",
        "descriptor_transfer_policy": "never_transfer_descriptor_to_client",
        "lifetime_policy": "hold_from_validated_apply_through_post_observation",
        "restore_policy": "close_descriptor_on_every_exit_and_verify_helper_closed",
    }
    if any(dma[key] != expected for key, expected in exact.items()):
        _fail("DMA-latency descriptor ownership/lifetime policy differs")


def _validate_guardians(
    value: Any, cpuset: Mapping[str, Any], telemetry: Mapping[str, Any],
    limits: Mapping[str, Any], cpu_plan: Mapping[str, Any],
) -> None:
    guardians = _exact(value, GUARDIAN_KEYS, "guardian plan")
    poll = _plain_int(guardians["poll_interval_ns"], "guardian poll interval", 1, 100_000_000)
    gap = _plain_int(
        guardians["maximum_observation_gap_ns"], "guardian maximum observation gap",
        poll, 1_000_000_000,
    )
    if gap < poll:
        _fail("guardian maximum observation gap is below its polling interval")
    spacing = _plain_int(
        guardians["minimum_observation_spacing_ns"],
        "guardian minimum observation spacing", 1, gap,
    )
    if spacing != poll:
        _fail("guardian minimum observation spacing differs from its cadence")
    maximum_duration = _plain_int(
        guardians["maximum_campaign_duration_ns"],
        "guardian maximum campaign duration", 1, U64_MAX,
    )
    expected_duration = (
        limits["maximum_run_count"] * 4
        * limits["maximum_process_seconds"] * 1_000_000_000
    )
    if expected_duration > U64_MAX or maximum_duration != expected_duration:
        _fail("guardian campaign duration differs from the checked six-slot command bound")
    maximum_count = _plain_int(
        guardians["maximum_observation_count"],
        "guardian maximum observation count", 1, U64_MAX,
    )
    expected_count = maximum_duration // spacing + 1
    if maximum_count != expected_count:
        _fail("guardian observation count differs from duration/cadence derivation")
    expected_line_bytes = guardian_line_capacity_bytes(
        len(cpu_plan["controlled_cpu_ids"]), len(GUARDIAN_SURFACES),
        len(telemetry["throttle_sources"]) + 1,
    )
    if limits["maximum_guardian_line_bytes"] != expected_line_bytes:
        _fail("guardian line bound differs from its schema/cardinality derivation")
    evidence_capacity = maximum_count * expected_line_bytes
    if (
        evidence_capacity > U64_MAX
        or limits["maximum_guardian_evidence_bytes"] != evidence_capacity
    ):
        _fail("guardian evidence cap differs from checked count-times-line capacity")
    if guardians["evidence_filename"] != "guardian_evidence.jsonl":
        _fail("guardian evidence filename differs")
    if guardians["surfaces"] != list(GUARDIAN_SURFACES):
        _fail("guardian surface population differs from the exact complete set")
    exact = {
        "policy": "independent_helper_continuous_guard_until_post_observation",
        "control_plane_policy": "held_proc_variable_length_peer_child_chain_exact_ppid_start_executable_identity",
        "descendant_policy": "raw_peer_closure_partitioned_into_attested_control_branch_and_estimator_pid_tid_closure",
        "cpuset_policy": "child_tasks_equals_estimator_tids_control_tids_outside_on_disjoint_helper_cpus",
        "control_drift_policy": "latch_any_transient_or_persistent_control_drift",
        "irq_drift_policy": "latch_affinity_population_or_irqbalance_drift",
        "foreign_task_policy": "retain_complete_non_gating_foreign_affinity_eligibility_observations",
        "thermal_policy": "continuous_temperature_and_monotonic_throttle_counter",
        "failure_latch_policy": "irreversible_campaign_failure_restore_without_retry",
        "evidence_policy": "retain_complete_chronological_raw_guardian_receipts",
        "evidence_storage_policy": "helper_owned_append_only_held_new_file_no_client_writes",
        "evidence_chain_policy": "canonical_jsonl_previous_record_sha256_chain",
    }
    if any(guardians[key] != expected for key, expected in exact.items()):
        _fail("continuous guardian policy differs")
    if cpuset["guardian_poll_interval_ns"] != poll:
        _fail("cpuset and global guardian polling intervals differ")
    if telemetry["continuous_temperature_interval_ns"] > gap:
        _fail("temperature sampling interval exceeds the guardian evidence gap")


def _validate_restoration(value: Any) -> None:
    restoration = _exact(value, RESTORATION_KEYS, "restoration plan")
    if type(restoration["operation_order"]) is not list or tuple(restoration["operation_order"]) != RESTORE_OPERATION_ORDER:
        _fail("exact restoration operation order differs")
    if type(restoration["restore_triggers"]) is not list or tuple(restoration["restore_triggers"]) != RESTORE_TRIGGERS:
        _fail("restoration trigger population/order differs")
    exact = {
        "policy": "exact_captured_prior_state_or_recovery_indeterminate",
        "journal_policy": "durable_prior_before_first_mutation_hash_chain_retain_until_verified",
        "verification_policy": "typed_semantic_and_raw_byte_exact_reconstruction",
        "cpuset_cleanup_policy": "membership_empty_group_removed_prior_parent_exact",
        "parent_fsync_policy": "fsync_after_create_rename_remove_and_journal_completion",
        "indeterminate_policy": "stop_retain_journal_publish_no_passing_evidence",
    }
    if any(restoration[key] != expected for key, expected in exact.items()):
        _fail("failure-atomic restoration policy differs")


def _validate_controls(value: Any) -> None:
    controls = _exact(value, CONTROLS_KEYS, "controls")
    if controls["policy"] != "capture_apply_validate_restore" or controls["restore_on_every_exit"] is not True:
        _fail("host-control transaction policy differs")
    if (
        controls["formal_authority_policy"]
        != "privileged_helper_is_sole_mutation_observation_restoration_authority"
        or controls["legacy_backend_policy"]
        != "disabled_for_formal_schema_v2_compatibility_tests_only"
    ):
        _fail("formal control authority does not exclude the legacy backend")
    sudo = _argv(controls["noninteractive_sudo_argv"], "noninteractive sudo argv")
    if sudo != ("/usr/bin/sudo", "-n"):
        _fail("privilege prefix is not exact sudo -n")
    modules = controls["module_actions"]
    if type(modules) is not list:
        _fail("module actions are not a list")
    seen_modules = set()
    for module in modules:
        action = _exact(module, MODULE_KEYS, "module action")
        name = _safe_id(action["name"], "module name")
        if name not in ("k10temp", "msr") or name in seen_modules:
            _fail("module action is duplicate or outside the approved telemetry modules")
        seen_modules.add(name)
        _argv(action["modprobe_argv"], "module-load argv", sudo_prefix=sudo)
        _argv(action["remove_argv"], "module-remove argv", sudo_prefix=sudo)
        if action["retain_if_preexisting"] is not True:
            _fail("preexisting module retention is not enabled")
    if tuple(action["name"] for action in modules) != tuple(sorted(seen_modules)):
        _fail("module actions are not in canonical name order")
    services = controls["service_actions"]
    if type(services) is not list:
        _fail("service actions are not a list")
    seen_services = set()
    for service in services:
        action = _exact(service, SERVICE_KEYS, "service action")
        name = _safe_id(action["name"], "service name")
        if name != "irqbalance" or name in seen_services:
            _fail("service action is duplicate or outside the approved irqbalance service")
        seen_services.add(name)
        _argv(action["deactivate_argv"], "service-deactivate argv", sudo_prefix=sudo)
        _argv(action["activate_argv"], "service-activate argv", sudo_prefix=sudo)
        if action["retain_if_inactive"] is not True:
            _fail("preexisting inactive service retention is not enabled")
    writes = controls["writes"]
    if type(writes) is not list or not writes:
        _fail("host-control writes are absent")
    seen_paths = set()
    allowed_prefixes = ("/sys/devices/system/cpu", "/proc/irq", "/sys/fs/cgroup")
    for index, write in enumerate(writes):
        record = _exact(write, WRITE_KEYS, "control write")
        if _plain_int(record["order"], "control-write order") != index:
            _fail("control-write order is not exact and contiguous")
        path = _absolute(record["path"], "control-write path", allowed_prefixes)
        if path in seen_paths:
            _fail("control-write path is duplicated")
        seen_paths.add(path)
        parser = record["parser"]
        if parser not in ("text", "integer", "cpu_list", "cpu_mask"):
            _fail("control parser is unsupported")
        _control_text(record["desired_text"], parser, "control desired text")
        if (
            (path == "/proc/irq/default_smp_affinity" and parser != "cpu_mask")
            or (parser == "cpu_mask" and path != "/proc/irq/default_smp_affinity")
            or (
                re.fullmatch(r"/proc/irq/[0-9]+/smp_affinity_list", path)
                is not None and parser != "cpu_list"
            )
        ):
            _fail("IRQ affinity control parser differs from its kernel surface")
        if record["restore_exact"] is not True:
            _fail("control does not require exact restoration")
    _absolute(controls["recovery_journal_parent"], "recovery-journal parent", ("/tmp",))


def _expected_value(value: Any, parser: str, label: str) -> None:
    if parser == "integer":
        _plain_int(value, label)
    elif parser == "cpu_list":
        _cpu_list(value, label)
    elif parser in ("text", "sha256", "hwmon_temperature_millicelsius"):
        if parser == "sha256":
            _sha(value, label)
        elif parser == "hwmon_temperature_millicelsius":
            if type(value) is not dict or set(value) != {"minimum", "maximum"}:
                _fail(label + " thermal range differs")
            minimum = _plain_int(value["minimum"], label + " minimum", -273_150, 1_000_000)
            maximum = _plain_int(value["maximum"], label + " maximum", -273_150, 1_000_000)
            if minimum >= maximum:
                _fail(label + " thermal range is empty")
        else:
            _text(value, label)
    else:
        _fail("observation parser is unsupported")


def _validate_observations(value: Any) -> None:
    if type(value) is not list or not value:
        _fail("observations must be one nonempty list")
    identifiers = []
    for item in value:
        record = _exact(item, OBSERVATION_KEYS, "observation")
        identifier = _safe_id(record["observation_id"], "observation ID")
        identifiers.append(identifier)
        _argv(record["argv"], "observation argv")
        _environment(record["environment"], "observation environment")
        _absolute(record["cwd"], "observation cwd")
        _plain_int(record["timeout_ns"], "observation timeout", 1, 60_000_000_000)
        _plain_int(record["maximum_stdout_bytes"], "observation output bound", 1, 16 * 1024 * 1024)
        parser = record["parser"]
        if parser not in ("text", "integer", "cpu_list", "sha256", "hwmon_temperature_millicelsius"):
            _fail("observation parser is unsupported")
        _expected_value(record["expected"], parser, "observation expected value")
    if identifiers != sorted(set(identifiers)):
        _fail("observation IDs are not sorted and unique")


def _validate_limits(value: Any) -> None:
    limits = _exact(value, LIMIT_KEYS, "limits")
    for key in LIMIT_KEYS:
        _plain_int(limits[key], "limit " + key, 1, U64_MAX)
    if limits["maximum_profile_bytes"] != MAX_PROFILE_BYTES:
        _fail("profile byte limit differs from the codec")
    if (
        limits["maximum_guardian_population_items"]
        != GUARDIAN_POPULATION_ITEM_LIMIT
    ):
        _fail("guardian population limit differs from the implementation")
    if limits["maximum_run_count"] != 6 or limits["maximum_pair_count"] != 3:
        _fail("run/pair resource limits differ from the frozen campaign")
    if limits["maximum_guardian_line_bytes"] > limits["maximum_jsonl_line_bytes"]:
        _fail("guardian line bound exceeds the general JSONL line bound")
    if (
        limits["maximum_guardian_evidence_bytes"]
        > limits["maximum_artifact_bytes"]
    ):
        _fail("guardian evidence bound exceeds the whole-artifact bound")


def _validate_artifact(value: Any) -> None:
    artifact = _exact(value, ARTIFACT_KEYS, "artifact")
    if _plain_int(artifact["schema_version"], "artifact schema version", 1, 1) != 1:
        _fail("artifact schema version differs")
    expected_names = {
        "report_filename": "cp2_report.json",
        "provenance_filename": "provenance.json",
        "commands_filename": "commands.jsonl",
        "timing_samples_filename": "timing_samples.jsonl",
        "manifest_filename": "SHA256SUMS",
    }
    if any(artifact[key] != expected for key, expected in expected_names.items()):
        _fail("artifact fixed filename differs")
    _absolute(artifact["publication_parent"], "artifact publication parent")
    if artifact["publication_policy"] != "descriptor_bound_noreplace_fsync":
        _fail("artifact publication policy differs")


def profile_plan_sha256(profile_value: Mapping[str, Any]) -> str:
    """Hash all selected behavior without recursive helper-file digests.

    The final profile contains the launcher binary SHA-256, so compiling the
    full profile SHA-256 into that launcher would require a cryptographic fixed
    point.  This projection excludes only helper file-identity digests while
    retaining every selected path, policy, control, telemetry source, limit,
    and failure rule.
    """

    if type(profile_value) is not dict:
        _fail("profile plan input is not a mapping")
    if set(profile_value) != set(ROOT_KEYS):
        _fail("profile plan root keys differ from the complete profile schema")
    helper = profile_value.get("privileged_helper")
    if type(helper) is not dict:
        _fail("profile omits the privileged-helper section")
    policy_keys = (
        "authority_policy", "root_launcher_path", "trusted_root",
        "journal_directory", "invocation_argv", "helper_cpu_ids",
        "root_owner_uid", "socket_family", "socket_type",
        "peer_credential_policy", "nonce_policy",
        "client_selection_policy", "descriptor_transfer_policy",
        "installed_closure_file_count", "production_policy",
    )
    if any(key not in helper for key in policy_keys):
        _fail("profile helper omits one nonrecursive policy field")
    digest_keys = {
        "plan_sha256", "source_sha256", "protocol_core_sha256",
        "root_launcher_sha256", "import_closure_sha256", "sudoers_sha256",
    }
    if set(digest_keys) - set(helper):
        _fail("profile helper omits one recursive identity digest")
    record = {
        "domain": "SchurVIO-CP2-E-privileged-plan-v2",
        "profile_without_recursive_helper_digests": {
            key: (
                {item: helper[item] for item in helper if item not in digest_keys}
                if key == "privileged_helper" else profile_value[key]
            )
            for key in ROOT_KEYS
        },
    }
    encoded = json.dumps(
        record, allow_nan=False, ensure_ascii=False,
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8") + b"\n"
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class FrozenTimingProfile:
    canonical_bytes: bytes
    sha256: str
    value: Mapping[str, Any]


def load_profile_bytes(payload: bytes) -> FrozenTimingProfile:
    """Parse, fully validate, and hash one canonical profile byte string."""

    value = _exact(_strict_json(payload), ROOT_KEYS, "profile")
    if value["schema_version"] != 2 or value["record_type"] != "cp2_timing_profile" or value["checkpoint"] != "CP2-E":
        _fail("profile root identity differs")
    _safe_id(value["profile_id"], "profile ID")
    _sha(value["authorization_sha256"], "authorization SHA-256")
    if value["source_identity_policy"] != "exact_clean_head_bound_by_readiness_and_provenance":
        _fail("source identity policy differs")
    _validate_target(value["target"])
    _validate_input(value["input"])
    _validate_pairing(value["pairing"])
    _validate_runtime(value["runtime"], value["target"])
    cpu_plan = _validate_cpu_plan(value["cpu_plan"], value["target"], value["runtime"])
    _validate_privileged_helper(value["privileged_helper"], cpu_plan)
    _validate_cpuset(value["cpuset"], cpu_plan)
    _validate_cpu_controls(
        value["cpu_controls"], value["target"], value["runtime"], cpu_plan,
    )
    _validate_interrupts(value["interrupts"], cpu_plan, value["runtime"])
    _validate_controls(value["controls"])
    _validate_observations(value["observations"])
    _validate_telemetry_plan(
        value["telemetry_plan"], cpu_plan, value["runtime"],
        value["controls"], value["observations"],
    )
    _validate_dma_latency(value["dma_latency"], value["runtime"])
    _validate_limits(value["limits"])
    _validate_guardians(
        value["guardians"], value["cpuset"], value["telemetry_plan"],
        value["limits"], value["cpu_plan"],
    )
    _validate_restoration(value["restoration"])
    _validate_artifact(value["artifact"])
    if value["privileged_helper"]["plan_sha256"] != profile_plan_sha256(value):
        _fail("privileged helper plan SHA-256 differs from its full projection")
    return FrozenTimingProfile(payload, hashlib.sha256(payload).hexdigest(), value)


def load_profile_path(path: str) -> FrozenTimingProfile:
    """Read a single-link regular profile without following a final symlink."""

    normalized = _absolute(path, "profile path")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(normalized, flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("profile path is not a single-link regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_PROFILE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_PROFILE_BYTES:
                _fail("profile byte count exceeds the resource limit")
        return load_profile_bytes(b"".join(chunks))
    finally:
        os.close(descriptor)


__all__ = [
    "CPUSET_LIFECYCLE", "CPU_CONTROL_APPLICATION_ORDER", "FrozenTimingProfile",
    "GUARDIAN_SURFACES", "MAX_PROFILE_BYTES", "PAIR_ORDER", "PROFILE_RELPATH",
    "RESTORE_OPERATION_ORDER", "RESTORE_TRIGGERS", "TimingProfileError",
    "canonical_profile_bytes", "load_profile_bytes", "load_profile_path",
    "profile_plan_sha256",
]
