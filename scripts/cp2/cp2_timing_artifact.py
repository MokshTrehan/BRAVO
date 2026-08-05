#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Data-free CP2-E timing artifact assembly and detached verification.

The authoritative timing population is the exact C++ ``kTiming`` support
trace.  A caller cannot supply reduced timing samples: this module derives
them by independently joining ``serial -> callback -> updater -> timing`` and
the detached verifier repeats that derivation from the retained raw files.

This module only assembles an already-created staging directory.  It does not
run ROS, touch host controls, read recorded input, or publish/replace a final
artifact name.  Those orchestration and failure-atomic publication steps must
remain blocked until a separately audited runner exists.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import cp2_schema
import cp2_timing_math as timing_math
import cp2_timing_profile as profile_codec


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
HEX40_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,255}$")
SAFE_RELPATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
PAIR_ORDER = (("nullspace", "schur"), ("schur", "nullspace"), ("nullspace", "schur"))
COMMAND_PHASES = ("clock_pre", "runtime_preflight", "ros_run", "clock_post")
TIMER_CLOCK = "std::chrono::steady_clock"
U64_MAX = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
U128_MAX = (1 << 128) - 1
HELPER_SCHEMA_VERSION = 2
MAX_JSON_BYTES = 64 * 1024 * 1024
MAX_JSONL_LINE_BYTES = 1024 * 1024
MAX_ARTIFACT_FILES = 2048
MAX_CONTROL_STATE_BYTES = 2 * 1024 * 1024
MAX_CONTROL_EVIDENCE_ROWS = 3
MAX_CONTROL_EVIDENCE_LINE_BYTES = MAX_CONTROL_STATE_BYTES + 4096
MAX_CONTROL_EVIDENCE_BYTES = (
    MAX_CONTROL_EVIDENCE_ROWS * MAX_CONTROL_EVIDENCE_LINE_BYTES
)

TERMINAL_MAPPING = {
    "empty_input": frozenset(("input_empty",)),
    "all_rejected": frozenset((
        "no_features_after_cleaning", "no_features_after_triangulation",
        "no_raw_systems", "all_baseline_features_rejected",
    )),
    "empty_after_compression": frozenset(("measurement_compression_empty",)),
    "preflight_rejected": frozenset(("baseline_preflight_rejected",)),
    "committed_counted": frozenset(("none",)),
    "internal_failure": frozenset((
        "invalid_live_mode", "snapshot_mismatch", "trace_invariant_failure",
    )),
}

SERIAL_KEYS = (
    "absolute_record_delta_ns", "anchor_camera_id", "anchor_filtered_index",
    "cam0_filtered_index", "cam0_header_time_ns", "cam0_record_time_ns",
    "cam1_filtered_index", "cam1_header_time_ns", "cam1_record_time_ns",
    "pair_index", "record_type", "schema_version", "sequence_id", "sequence_index",
)
CALLBACK_KEYS = (
    "anchor_filtered_index", "callback_index", "cam0_filtered_index",
    "cam0_header_time_ns", "cam0_record_time_ns", "cam1_filtered_index",
    "cam1_header_time_ns", "cam1_record_time_ns", "camera_timestamp_ns",
    "enqueue_entered", "enqueue_returned", "enqueue_status", "mode", "pair_index",
    "processing_entered", "processing_returned", "processing_status", "record_type",
    "schema_version", "sequence_id", "sequence_index", "state_row_emitted",
    "trajectory_index", "updater_invocation_ids", "updater_invoked",
)
UPDATER_KEYS = (
    "baseline_preflight_attempted", "camera_timestamp_ns", "committed",
    "duration_ns", "input_feature_count", "invocation_id", "mode", "nonempty",
    "pair_index", "preflight_accepted", "primary", "raw_system_count", "record_type",
    "schema_version", "sequence_id", "sequence_index", "terminal_status",
    "terminal_subreason", "timer_clock", "timer_end_ns", "timer_start_ns",
)
RAW_TIMING_KEYS = (
    "cam0_record_time_ns", "camera_timestamp_ns", "committed", "duration_ns",
    "invocation_id", "mode", "nonempty", "preflight_accepted", "primary",
    "record_type", "schema_version", "sequence_id", "sequence_index",
    "serial_pair_index", "terminal_status", "timer_clock", "timer_end_ns",
    "timer_start_ns",
)
SAMPLE_KEYS = (
    "schema_version", "record_type", "profile_sha256", "timing_pair_index",
    "run_index", "mode", "sequence_index", "sequence_id", "serial_pair_index",
    "cam0_record_time_ns", "camera_timestamp_ns", "invocation_id",
    "terminal_status", "terminal_subreason", "input_feature_count",
    "raw_system_count", "baseline_preflight_attempted", "nonempty",
    "preflight_accepted", "committed", "primary", "timer_clock",
    "timer_start_ns", "timer_end_ns", "duration_ns",
)
CONTEXT_KEYS = (
    "schema_version", "record_type", "checkpoint", "run_id", "sequence_index",
    "sequence_id", "mode", "shadow_enabled", "trace_level", "source_commit",
    "config_sha256", "bag_sha256", "pair_index_sha256",
    "resolved_parameters_sha256", "trace_directory", "serial_trace_path",
    "callback_trace_path", "trajectory_trace_path", "updater_trace_path",
    "state_payload_path", "proposal_payload_path", "raw_system_payload_path",
    "timing_trace_path", "runtime_parameters_path", "loader_map_before_path",
    "loader_map_after_path", "legacy_state_path", "legacy_deviation_path",
    "legacy_timing_path",
)
CLOCK_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256", "run_index",
    "phase", "captured_monotonic_ns", "boot_id_sha256",
    "control_applied_sha256", "stable_control_state", "stable_control_state_sha256",
    "observations", "telemetry", "passed",
)
CLOCK_OBSERVATION_KEYS = (
    "observation_id", "argv", "environment", "cwd", "started_monotonic_ns",
    "ended_monotonic_ns", "exit_code", "timed_out", "stdout_hex",
    "stdout_sha256", "stderr_hex", "stderr_sha256", "parsed_value",
)
TELEMETRY_KEYS = (
    "temperature_sensor_id", "temperature_millicelsius", "cpu_counters",
)
CPU_COUNTER_KEYS = (
    "cpu_id", "thermal_throttle_count", "aperf", "mperf",
    "reference_frequency_khz", "scaling_current_frequency_khz",
)
STABLE_CONTROL_KEYS = (
    "values", "modules_loaded", "services_active", "process_affinity",
    "dma_latency_us",
)
STABLE_CONTROL_VALUE_KEYS = ("path", "parser", "text")
EXECUTION_BINDING_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256",
    "source_commit", "source_tree", "unit_manifest_sha256",
    "sequence_set_manifest_sha256", "executable_path", "executable_sha256",
    "dso_closure_sha256", "configuration_sha256", "boot_id_sha256",
    "machine_identity_sha256", "cwd", "environment", "phase_templates",
)
TEMPLATE_TOKENS = frozenset((
    "{profile_sha256}", "{run_index}", "{timing_pair_index}", "{mode}",
    "{run_id}", "{trace_directory}", "{sequence_index}", "{sequence_id}",
))
CONTROL_RECEIPT_KEYS = (
    "schema_version", "record_type", "profile_sha256", "phase",
    "captured_monotonic_ns", "state", "state_sha256", "passed",
)
CONTROL_PRIOR_KEYS = (
    "schema_version", "record_type", "values", "modules_preexisting",
    "services_active", "process_affinity",
)
CONTROL_V2_PRIOR_KEYS = CONTROL_PRIOR_KEYS + ("privileged_v2",)
CONTROL_V2_APPLIED_KEYS = (
    "dma_latency_us", "modules_loaded", "process_affinity", "record_type",
    "schema_version", "services_active", "values", "privileged_v2",
)
CONTROL_V2_VALUE_KEYS = (
    "control_id", "desired_text", "parser", "path", "text", "writable",
)
CONTROL_V2_EXTENSION_KEYS = (
    "control_plane_processes", "control_plane_threads", "cpuset",
    "dma_latency_held_by_helper", "helper_affinity_cpu_ids",
    "descendant_process_ids", "descendant_threads", "irq_numbers",
    "irq_population_sha256", "peer", "peer_start_time_ticks", "profile_sha256",
    "record_type", "schema_version",
)
CONTROL_V2_CPUSET_KEYS = (
    "cpus", "effective_cpus", "effective_mems", "exists", "group_path",
    "members", "mems",
)
CONTROL_V2_THREAD_KEYS = (
    "affinity_cpu_ids", "cpuset_membership", "process_id",
    "start_time_ticks", "tid",
)
CONTROL_V2_PROCESS_KEYS = (
    "executable_device", "executable_inode", "parent_pid", "pid",
    "process_gid", "process_uid", "start_time_ticks",
)
RAW_TELEMETRY_SNAPSHOT_KEYS = (
    "schema_version", "record_type", "phase", "receipts",
)
RAW_TELEMETRY_RECEIPT_KEYS = (
    "schema_version", "record_type", "specification", "phase",
    "started_monotonic_ns", "ended_monotonic_ns", "raw_hex", "raw_sha256",
    "parsed_value",
)
RAW_TELEMETRY_SPEC_KEYS = (
    "metric_id", "role", "cpu_id", "source_kind", "path", "register",
)
RAW_TELEMETRY_COMPARISON_KEYS = (
    "schema_version", "record_type", "pre_snapshot_sha256",
    "post_snapshot_sha256", "expected_cpu_ids", "accepted_frequency_khz",
    "temperature_range_millicelsius", "aperf_mperf_ratio_bounds", "per_cpu",
    "temperatures", "passed",
)
RAW_TELEMETRY_CPU_COMPARISON_KEYS = (
    "cpu_id", "thermal_throttle_count_pre", "thermal_throttle_count_post",
    "aperf_pre", "aperf_post", "aperf_delta", "mperf_pre", "mperf_post",
    "mperf_delta", "scaling_current_frequency_khz_pre",
    "scaling_current_frequency_khz_post", "aperf_mperf_ratio",
)
RAW_TELEMETRY_TEMPERATURE_KEYS = ("cpu_id", "pre", "post")
PROCESS_IDENTITY_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256",
    "execution_binding_sha256", "run_index", "observed_monotonic_ns", "pid",
    "start_time_ticks", "executable_sha256", "loader_before_sha256",
    "loader_after_sha256",
)
RUNTIME_PREFLIGHT_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256",
    "execution_binding_sha256", "run_index", "argv", "environment", "cwd",
    "started_monotonic_ns", "ended_monotonic_ns", "exit_code", "timed_out",
    "process_group_complete", "stdout_hex", "stdout_sha256", "stderr_hex",
    "stderr_sha256", "passed",
)
AFFINITY_KEYS = (
    "schema_version", "record_type", "root_pid", "expected_cpus", "poll_interval_ns",
    "observation_count", "observations", "passed",
)
AFFINITY_OBSERVATION_KEYS = ("monotonic_ns", "tid", "cpu_ids")
PROVENANCE_KEYS = (
    "schema_version", "record_type", "checkpoint", "profile_sha256",
    "source_commit", "source_tree", "unit_manifest_sha256",
    "sequence_set_manifest_sha256", "bag_sha256", "executable_sha256",
    "dso_closure_sha256", "configuration_sha256", "launch_sha256",
    "boot_id_sha256", "machine_identity_sha256", "run_identity_sha256",
    "control_prior_sha256", "control_applied_sha256", "control_restored_sha256",
    "helper_transcript_sha256", "helper_terminal_receipt_sha256",
    "helper_binding_digest", "helper_terminal_journal_sha256",
    "helper_population_seal_sha256", "helper_guardian_evidence_sha256",
    "helper_control_evidence_sha256", "helper_control_evidence_seal_sha256",
)
COMMAND_KEYS = (
    "schema_version", "record_type", "command_id", "run_index",
    "timing_pair_index", "phase", "argv", "environment", "cwd",
    "started_monotonic_ns", "ended_monotonic_ns", "exit_code", "timed_out",
    "process_group_complete", "stdout_sha256", "stderr_sha256",
)
RUN_KEYS = (
    "run_index", "timing_pair_index", "position_in_pair", "mode", "profile_sha256",
    "run_id", "fresh_process_identity", "pre_snapshot_sha256",
    "post_snapshot_sha256", "trace_bundle_sha256", "sample_count",
    "eligible_sample_count", "process_identity_sha256",
    "runtime_preflight_sha256", "telemetry_pre_sha256",
    "telemetry_post_sha256", "telemetry_comparison_sha256",
)
PAIR_KEYS = (
    "timing_pair_index", "common_count", "common_payload_path",
    "common_payload_sha256", "baseline_p50", "candidate_p50", "p50_ratio",
    "p50_left_cross_product_hex", "p50_right_cross_product_hex", "p50_passed",
    "baseline_p95", "candidate_p95", "p95_ratio", "p95_left_cross_product_hex",
    "p95_right_cross_product_hex", "p95_passed", "passed",
)
REPORT_KEYS = (
    "schema_version", "record_type", "checkpoint", "status", "profile_sha256",
    "provenance_sha256", "sequence_index", "sequence_id",
    "bag_begin_record_time_ns", "frozen_offset_ns", "warmup_boundary_ns",
    "timing_samples_sha256", "pair_order", "runs", "pair_results",
    "helper_transcript_sha256", "helper_terminal_receipt_sha256",
    "helper_guardian_evidence_sha256",
    "helper_control_evidence_sha256", "helper_control_evidence_seal_sha256",
    "median_of_three_median_ratios", "median_of_three_p95_ratios",
    "every_pair_passed", "passed",
)

HELPER_BINDING_KEYS = (
    "profile_sha256", "plan_sha256", "source_sha256",
    "protocol_core_sha256", "root_launcher_sha256",
    "import_closure_sha256", "sudoers_sha256",
)
HELPER_EVENT_KEYS = (
    "event", "event_index", "payload", "previous_record_sha256",
    "record_type", "schema_version",
)
HELPER_TERMINAL_KEYS = (
    "abnormal_recovery", "binding", "binding_digest", "descriptor_closure",
    "control_evidence_seal_sha256", "error_type", "event_count",
    "first_record_sha256", "journal_id",
    "last_record_sha256", "population_seal_sha256", "population_stop_count",
    "record_type", "restoration_proved", "schema_version", "session_id",
    "terminal_journal_sha256", "terminal_status", "transcript_sha256",
    "transcript_size_bytes",
)
HELPER_CONTROL_ROW_KEYS = (
    "binding_digest", "journal_id", "phase", "previous_record_sha256",
    "record_type", "schema_version", "sequence", "session_id", "state",
    "state_sha256",
)
HELPER_CONTROL_RECEIPT_KEYS = (
    "end_offset_bytes", "phase", "record_sha256", "sequence",
    "state_sha256",
)
HELPER_CONTROL_CHECKPOINT_KEYS = (
    "device", "evidence_sha256", "evidence_size_bytes", "inode",
    "last_record_sha256", "row_count", "rows",
)
HELPER_CONTROL_SEAL_KEYS = (
    "binding_digest", "device", "evidence_sha256", "evidence_size_bytes",
    "first_record_sha256", "inode", "journal_id", "last_record_sha256",
    "phases", "record_type", "restoration_matches_prior", "row_count",
    "rows", "schema_version", "session_id",
)
HELPER_SOURCE_KEYS = (
    "cpu_id", "extraction", "measurement_kind", "parsed_value", "raw_hex",
    "raw_sha256", "read_ended_monotonic_ns", "read_started_monotonic_ns",
    "register", "source_id", "source_kind", "source_path",
    "source_spec_sha256",
)
HELPER_RECEIPT_KEYS = (
    "phase", "run_index", "schema_version", "snapshot_ended_monotonic_ns",
    "snapshot_skew_ns", "snapshot_started_monotonic_ns", "sources",
)
HELPER_RUN_SLOT_KEYS = (
    "mode", "position_in_pair", "run_index", "timing_pair_index",
)
HELPER_OBSERVATION_PAYLOAD_KEYS = (
    "phase", "population", "population_seal", "population_seal_sha256",
    "population_sha256", "receipt", "receipt_sha256", "run_slot",
)
HELPER_SEAL_KEYS = (
    "bound_peer", "coverage_ended_monotonic_ns",
    "coverage_started_monotonic_ns", "drift", "evidence_sha256",
    "evidence_size_bytes", "final_child_cpuset_member_count",
    "final_control_plane_process_count", "final_control_plane_tid_count",
    "final_descendant_process_count", "final_descendant_tid_count",
    "final_foreign_affinity_eligibility_count",
    "final_foreign_affinity_eligibility_sha256",
    "final_scheduler_witness_sha256", "first_record_sha256",
    "irq_population_changed", "last_record_sha256",
    "maximum_observation_gap_ns", "observation_count", "required_surfaces",
    "schema_version", "session_id", "thermal_or_throttle_event",
)
HELPER_GUARDIAN_ROW_KEYS = (
    "child_cpuset_members", "complete_control_plane_processes",
    "complete_control_plane_threads", "complete_descendant_population",
    "complete_descendant_threads", "complete_descendant_tid_population",
    "drift", "ended_monotonic_ns", "foreign_affinity_eligibility",
    "irq_population_sha256", "scheduler_witness_sha256",
    "previous_record_sha256", "record_type", "schema_version", "sequence",
    "started_monotonic_ns", "surface_states", "thermal_or_throttle_event",
    "typed_telemetry",
)


class TimingArtifactError(ValueError):
    """The CP2-E timing evidence is incomplete, malformed, or inconsistent."""


def _fail(message: str) -> None:
    raise TimingArtifactError(message)


def _u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > U64_MAX:
        _fail(label + " is outside u64")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        _fail(label + " is not Boolean")
    return value


def _sha(value: Any, label: str) -> str:
    if type(value) is not str or SHA256_RE.fullmatch(value) is None:
        _fail(label + " is not lowercase SHA-256")
    return value


def _safe_id(value: Any, label: str) -> str:
    if type(value) is not str or SAFE_ID_RE.fullmatch(value) is None:
        _fail(label + " is not a bounded safe identifier")
    return value


def _exact(value: Any, keys: Iterable[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(keys):
        _fail(label + " keys differ from the frozen schema")
    return value


def _foreign_affinity_eligibility_rows(
    value: Any, profile: profile_codec.FrozenTimingProfile, label: str,
) -> Tuple[Mapping[str, Any], ...]:
    if type(value) is not list or len(value) > 65_536:
        _fail(label + " is not a bounded row population")
    controlled = set(profile.value["cpu_plan"]["controlled_cpu_ids"])
    estimator = set(profile.value["runtime"]["cpu_ids"])
    retained = []
    previous_tid = 0
    for raw in value:
        row = _exact(raw, (
            "effective_affinity_cpu_ids", "process_start_time_ticks", "tid",
        ), label + " row")
        tid = _u64(row["tid"], label + " TID")
        start = _u64(
            row["process_start_time_ticks"], label + " process start time",
        )
        affinity = row["effective_affinity_cpu_ids"]
        if (
            tid == 0 or tid <= previous_tid or type(affinity) is not list
            or not affinity or affinity != sorted(set(affinity))
            or any(type(cpu) is not int or cpu not in controlled for cpu in affinity)
            or not set(affinity).intersection(estimator)
        ):
            _fail(label + " row identity/affinity/order differs")
        retained.append({
            "effective_affinity_cpu_ids": list(affinity),
            "process_start_time_ticks": start,
            "tid": tid,
        })
        previous_tid = tid
    return tuple(retained)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8") + b"\n"


def _strict_json(payload: bytes, label: str, maximum: int = MAX_JSON_BYTES) -> Any:
    if type(payload) is not bytes or not payload or len(payload) > maximum:
        _fail(label + " byte count is invalid")

    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(label + " has a duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8", "strict"), object_pairs_hook=pairs,
            parse_float=lambda _: _fail(label + " contains a float"),
            parse_constant=lambda token: _fail(label + " contains " + token),
        )
    except TimingArtifactError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise TimingArtifactError(label + " is not strict JSON") from exc
    if payload != _canonical_json(value):
        _fail(label + " is not canonical JSON")
    return value


def _jsonl(rows: Sequence[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical_json(row) for row in rows)


def _strict_jsonl(
    payload: bytes, label: str, *, maximum_line_bytes: int = MAX_JSONL_LINE_BYTES,
) -> Tuple[Mapping[str, Any], ...]:
    if type(payload) is not bytes or not payload:
        _fail(label + " is empty")
    lines = payload.splitlines(keepends=True)
    if any(
        not line.endswith(b"\n") or len(line) > maximum_line_bytes
        for line in lines
    ):
        _fail(label + " contains a noncanonical or oversized line")
    return tuple(_strict_json(line, label + " line", maximum_line_bytes) for line in lines)


def _safe_relpath(value: Any) -> str:
    if type(value) is not str or SAFE_RELPATH.fullmatch(value) is None:
        _fail("artifact relative path is unsafe")
    if any(part in ("", ".", "..") for part in value.split("/")):
        _fail("artifact relative path is unsafe")
    return value


def _sha_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _i64(value: Any, label: str) -> int:
    if type(value) is not int or value < I64_MIN or value > I64_MAX:
        _fail(label + " is outside i64")
    return value


def _helper_binding(
    profile: profile_codec.FrozenTimingProfile,
) -> Mapping[str, str]:
    helper = profile.value["privileged_helper"]
    value = {
        "profile_sha256": profile.sha256,
        "plan_sha256": helper["plan_sha256"],
        "source_sha256": helper["source_sha256"],
        "protocol_core_sha256": helper["protocol_core_sha256"],
        "root_launcher_sha256": helper["root_launcher_sha256"],
        "import_closure_sha256": helper["import_closure_sha256"],
        "sudoers_sha256": helper["sudoers_sha256"],
    }
    _exact(value, HELPER_BINDING_KEYS, "derived helper binding")
    return value


def _helper_source_specifications(
    profile: profile_codec.FrozenTimingProfile,
) -> Mapping[str, Mapping[str, Any]]:
    """Independently derive every privileged raw-source digest from profile v2."""

    telemetry = profile.value["telemetry_plan"]
    counters = telemetry["aperf_mperf"]
    result: Dict[str, Mapping[str, Any]] = {}

    def retain(
        source_id: str, kind: str, source_kind: str, path: str,
        extraction: str, cpu_id: Optional[int], register: Optional[int],
        profile_source: Mapping[str, Any],
    ) -> None:
        specification = {
            "source_id": source_id,
            "measurement_kind": kind,
            "source_kind": source_kind,
            "source_path": path,
            "extraction": extraction,
            "cpu_id": cpu_id,
            "register": register,
            "profile_source": dict(profile_source),
        }
        if source_id in result:
            _fail("derived helper source ID is duplicate")
        result[source_id] = {
            "measurement_kind": kind,
            "source_kind": source_kind,
            "source_path": path,
            "extraction": extraction,
            "cpu_id": cpu_id,
            "register": register,
            "source_spec_sha256": _sha_bytes(_canonical_json(specification)),
        }

    for source in counters["cpu_sources"]:
        cpu = source["cpu_id"]
        for kind, register in (
            ("aperf", source["aperf_register"]),
            ("mperf", source["mperf_register"]),
        ):
            retain(
                "cpu{}.{}".format(cpu, kind), kind, source["source_kind"],
                source["path"], "full_u64_little_endian_pread", cpu,
                register, source,
            )
    for source in telemetry["frequency_sources"]:
        cpu = source["cpu_id"]
        retain(
            "cpu{}.scaling_cur_freq".format(cpu), "scaling_cur_freq",
            source["source_kind"], source["path"],
            "canonical_ascii_decimal_u64_one_line", cpu, None, source,
        )
    reference_source = counters["reference_frequency_source"]
    retain(
        "shared.f_ref", "f_ref", reference_source["source_kind"],
        reference_source["path"],
        "canonical_ascii_decimal_u64_one_line", None, None,
        {
            "reference_frequency_source": dict(reference_source),
            "reference_frequency_khz": counters["reference_frequency_khz"],
        },
    )
    temperature = telemetry["temperature_source"]
    retain(
        "shared.temperature", "temperature", "k10temp_text_integer",
        temperature["input_path"],
        "canonical_ascii_decimal_i64_one_line", None, None, temperature,
    )
    for source in telemetry["throttle_sources"]:
        cpu = source["cpu_id"]
        retain(
            "cpu{}.amd_throttle".format(cpu), "amd_throttle",
            source["source_kind"], source["path"], source["value_extraction"],
            cpu, source["register"], source,
        )
    return dict(sorted(result.items()))


def _helper_raw_integer(row: Mapping[str, Any], label: str) -> int:
    raw_hex = row["raw_hex"]
    if (
        type(raw_hex) is not str or len(raw_hex) > 8192
        or len(raw_hex) % 2 or re.fullmatch(r"[0-9a-f]*", raw_hex) is None
    ):
        _fail(label + " raw bytes are not bounded lowercase hex")
    raw = bytes.fromhex(raw_hex)
    if not raw or _sha_bytes(raw) != _sha(row["raw_sha256"], label + " raw SHA-256"):
        _fail(label + " raw byte digest differs")
    extraction = row["extraction"]
    if extraction == "full_u64_little_endian_pread":
        if len(raw) != 8:
            _fail(label + " MSR value is not exactly eight bytes")
        parsed = int.from_bytes(raw, "little", signed=False)
    elif extraction in (
        "canonical_ascii_decimal_u64_one_line",
        "canonical_ascii_decimal_i64_one_line",
    ):
        try:
            text = raw.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise TimingArtifactError(label + " text is not ASCII") from exc
        if not text.endswith("\n") or text.count("\n") != 1 or "\r" in text or "\0" in text:
            _fail(label + " text is not one canonical newline-terminated line")
        token = text[:-1]
        signed = extraction.endswith("i64_one_line")
        digits = token[1:] if signed and token.startswith("-") else token
        if (
            not digits or not digits.isdigit()
            or (len(digits) > 1 and digits.startswith("0"))
            or token == "-0"
        ):
            _fail(label + " text is not canonical decimal")
        parsed = int(token)
        if signed:
            _i64(parsed, label + " parsed integer")
        else:
            _u64(parsed, label + " parsed integer")
    else:
        _fail(label + " extraction is unsupported")
    if type(row["parsed_value"]) is not int or row["parsed_value"] != parsed:
        _fail(label + " parsed value differs from the raw bytes")
    return parsed


def _helper_peer(value: Any, label: str) -> Mapping[str, int]:
    row = _exact(value, ("gid", "pid", "uid"), label)
    for key in ("gid", "pid", "uid"):
        if type(row[key]) is not int or row[key] < 0 or row[key] > (1 << 31) - 1:
            _fail(label + " " + key + " is invalid")
    return row


def _guardian_source_size(value: Any) -> int:
    if type(value) is bytes:
        return len(value)
    if isinstance(value, HeldGuardianEvidence):
        value.verify_held()
        return value.size_bytes
    if isinstance(value, Path):
        status = value.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("guardian evidence source is not a single-link regular file")
        return status.st_size
    _fail("guardian evidence source is neither exact bytes nor a retained path")


def _guardian_source_rows(
    value: Any, maximum_line_bytes: int,
) -> Iterable[Tuple[Mapping[str, Any], bytes]]:
    if type(value) is bytes:
        stream: Any = io.BytesIO(value)
    else:
        if isinstance(value, HeldGuardianEvidence):
            value.verify_held()
            descriptor = os.dup(value.descriptor)
            os.set_inheritable(descriptor, False)
            os.lseek(descriptor, 0, os.SEEK_SET)
        elif isinstance(value, Path):
            flags = os.O_RDONLY | os.O_CLOEXEC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(str(value), flags)
        else:
            _fail("guardian evidence source type differs")
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            os.close(descriptor)
            _fail("guardian evidence held source is unsafe")
        stream = os.fdopen(descriptor, "rb", buffering=0)
    try:
        while True:
            line = stream.readline(maximum_line_bytes + 1)
            if not line:
                break
            if len(line) > maximum_line_bytes or not line.endswith(b"\n"):
                _fail("guardian evidence contains an oversized or unterminated row")
            row = _strict_json(line, "guardian evidence row", maximum_line_bytes)
            if type(row) is not dict:
                _fail("guardian evidence row is not a mapping")
            yield row, line
    finally:
        stream.close()
        if isinstance(value, HeldGuardianEvidence):
            value.verify_held()


def _guardian_source_sha256(value: Any) -> str:
    if type(value) is bytes:
        return _sha_bytes(value)
    if isinstance(value, HeldGuardianEvidence):
        value.verify_held()
        return value.sha256
    if isinstance(value, Path):
        return _sha_file(value)
    _fail("guardian evidence source type differs")


def _validate_helper_guardian_evidence(
    evidence_source: Any, value: Any,
    profile: profile_codec.FrozenTimingProfile, peer: Mapping[str, int],
    session_id: str, applied_ns: int, final_observation_ns: int,
) -> Mapping[str, Any]:
    seal = _exact(value, HELPER_SEAL_KEYS, "helper population seal")
    guardians = profile.value["guardians"]
    limits = profile.value["limits"]
    if (
        type(seal["schema_version"]) is not int
        or seal["schema_version"] != HELPER_SCHEMA_VERSION
        or seal["session_id"] != session_id
        or _helper_peer(seal["bound_peer"], "guardian bound peer") != peer
    ):
        _fail("helper population-seal identity differs")
    start = _u64(seal["coverage_started_monotonic_ns"], "guardian coverage start")
    end = _u64(seal["coverage_ended_monotonic_ns"], "guardian coverage end")
    maximum_gap = _u64(seal["maximum_observation_gap_ns"], "guardian maximum gap")
    if (
        end < start or start > applied_ns or end < final_observation_ns
        or end - start > guardians["maximum_campaign_duration_ns"]
        or maximum_gap != guardians["maximum_observation_gap_ns"]
    ):
        _fail("guardian coverage interval/profile binding differs")
    required = seal["required_surfaces"]
    if required != guardians["surfaces"]:
        _fail("guardian surface population differs from the frozen profile")
    if type(required) is not list or required != sorted(set(required)):
        _fail("guardian surface population is not canonical and unique")
    count = _u64(seal["observation_count"], "guardian observation count")
    size = _u64(seal["evidence_size_bytes"], "guardian evidence size")
    for key in (
        "evidence_sha256", "first_record_sha256", "last_record_sha256",
        "final_foreign_affinity_eligibility_sha256",
        "final_scheduler_witness_sha256",
    ):
        _sha(seal[key], "guardian " + key)
    for key in (
        "final_child_cpuset_member_count",
        "final_control_plane_process_count", "final_control_plane_tid_count",
        "final_descendant_process_count", "final_descendant_tid_count",
        "final_foreign_affinity_eligibility_count",
    ):
        _u64(seal[key], "guardian " + key)
    source_size = _guardian_source_size(evidence_source)
    if (
        source_size == 0
        or source_size > limits["maximum_guardian_evidence_bytes"]
        or size != source_size or count == 0
        or count > guardians["maximum_observation_count"]
    ):
        _fail("guardian evidence size/count exceeds its frozen resource bounds")
    expected_sources = {
        source_id: specification
        for source_id, specification in _helper_source_specifications(profile).items()
        if specification["measurement_kind"] in ("temperature", "amd_throttle")
    }
    previous_end = start
    previous_start: Any = None
    previous_hash = "0" * 64
    final_scheduler_witness: Any = None
    final_foreign_affinity_eligibility: Any = None
    frozen_irq: Any = None
    previous_throttles: Any = None
    observed_count = 0
    observed_size = 0
    whole_digest = hashlib.sha256()
    first_digest: Any = None
    for index, (item, line) in enumerate(_guardian_source_rows(
        evidence_source, limits["maximum_guardian_line_bytes"],
    )):
        observed_count += 1
        observed_size += len(line)
        if observed_count > count or observed_size > size:
            _fail("guardian evidence exceeds its sealed count or size")
        whole_digest.update(line)
        if first_digest is None:
            first_digest = _sha_bytes(line)
        row = _exact(item, HELPER_GUARDIAN_ROW_KEYS, "guardian observation")
        sequence = _u64(row["sequence"], "guardian sequence")
        observed_start = _u64(row["started_monotonic_ns"], "guardian observation start")
        observed_end = _u64(row["ended_monotonic_ns"], "guardian observation end")
        if (
            row["record_type"] != "cp2e_guardian_observation"
            or type(row["schema_version"]) is not int
            or row["schema_version"] != HELPER_SCHEMA_VERSION
            or row["previous_record_sha256"] != previous_hash
            or sequence != index or observed_start < previous_end
            or observed_end < observed_start
            or observed_end - observed_start > maximum_gap
            or observed_start - previous_end > maximum_gap
            or (
                previous_start is not None
                and observed_start - previous_start < guardians["poll_interval_ns"]
            )
        ):
            _fail("guardian observation chronology/gap differs")
        descendants = row["complete_descendant_population"]
        descendant_tids = row["complete_descendant_tid_population"]
        child_members = row["child_cpuset_members"]
        foreign = _foreign_affinity_eligibility_rows(
            row["foreign_affinity_eligibility"], profile,
            "guardian foreign affinity eligibility",
        )
        if (
            type(descendants) is not list or not descendants
            or any(type(pid) is not int or pid <= 0 for pid in descendants)
            or descendants != sorted(set(descendants))
            or peer["pid"] not in descendants
            or type(descendant_tids) is not list or not descendant_tids
            or any(type(tid) is not int or tid <= 0 for tid in descendant_tids)
            or descendant_tids != sorted(set(descendant_tids))
            or peer["pid"] not in descendant_tids
            or child_members != descendant_tids
            or row["drift"] is not False
            or row["thermal_or_throttle_event"] is not False
        ):
            _fail("guardian task/drift/thermal state differs")
        allowed_cpus = profile.value["cpu_plan"]["controlled_cpu_ids"]
        descendant_threads = row["complete_descendant_threads"]
        if type(descendant_threads) is not list or not descendant_threads:
            _fail("guardian descendant thread population is empty")
        retained_descendant_tids = []
        for raw_thread in descendant_threads:
            thread = _exact(
                raw_thread, CONTROL_V2_THREAD_KEYS,
                "guardian descendant thread",
            )
            tid = _u64(thread["tid"], "guardian descendant TID")
            process_id = _u64(
                thread["process_id"], "guardian descendant process ID",
            )
            _u64(thread["start_time_ticks"], "guardian descendant start")
            _v2_cpu_population(
                thread["affinity_cpu_ids"], allowed_cpus,
                "guardian descendant affinity",
            )
            membership = thread["cpuset_membership"]
            if (
                process_id not in descendants or type(membership) is not str
                or not membership.startswith("/")
                or os.path.normpath(membership) != membership
            ):
                _fail("guardian descendant thread identity differs")
            retained_descendant_tids.append(tid)
        if (
            retained_descendant_tids != descendant_tids
            or {item["process_id"] for item in descendant_threads}
            != set(descendants)
            or any(
                sum(
                    item["process_id"] == pid and item["tid"] == pid
                    for item in descendant_threads
                ) != 1
                for pid in descendants
            )
        ):
            _fail("guardian descendant process/thread join differs")
        control_processes = row["complete_control_plane_processes"]
        if type(control_processes) is not list or not control_processes:
            _fail("guardian control-plane process chain is empty")
        control_pids = []
        parent_pid = peer["pid"]
        for raw_process in control_processes:
            process = _exact(
                raw_process, CONTROL_V2_PROCESS_KEYS,
                "guardian control process",
            )
            for key in CONTROL_V2_PROCESS_KEYS:
                _u64(process[key], "guardian control process " + key)
            if (
                process["pid"] == 0 or process["parent_pid"] != parent_pid
                or process["process_uid"] != 0 or process["process_gid"] != 0
            ):
                _fail("guardian control-plane process chain differs")
            parent_pid = process["pid"]
            control_pids.append(parent_pid)
        if control_pids != list(dict.fromkeys(control_pids)):
            _fail("guardian control-plane process IDs are duplicate")
        control_threads = row["complete_control_plane_threads"]
        if type(control_threads) is not list or not control_threads:
            _fail("guardian control-plane thread population is empty")
        control_tids = []
        for raw_thread in control_threads:
            thread = _exact(
                raw_thread, CONTROL_V2_THREAD_KEYS,
                "guardian control thread",
            )
            tid = _u64(thread["tid"], "guardian control TID")
            process_id = _u64(
                thread["process_id"], "guardian control process ID",
            )
            _u64(thread["start_time_ticks"], "guardian control start")
            affinity = _v2_cpu_population(
                thread["affinity_cpu_ids"], allowed_cpus,
                "guardian control affinity",
            )
            membership = thread["cpuset_membership"]
            if (
                process_id not in control_pids or type(membership) is not str
                or not membership.startswith("/")
                or os.path.normpath(membership) != membership
            ):
                _fail("guardian control thread identity differs")
            control_tids.append(tid)
        if (
            control_tids != sorted(set(control_tids))
            or set(control_tids).intersection(descendant_tids)
            or {item["process_id"] for item in control_threads}
            != set(control_pids)
            or any(
                sum(
                    item["process_id"] == pid and item["tid"] == pid
                    for item in control_threads
                ) != 1
                for pid in control_pids
            )
            or any(item["tid"] in set(descendant_tids).union(control_tids)
                   for item in foreign)
        ):
            _fail("guardian scheduler populations overlap or do not join")
        estimator_memberships = {
            item["cpuset_membership"] for item in descendant_threads
        }
        estimator_cpus = {
            cpu for item in descendant_threads for cpu in item["affinity_cpu_ids"]
        }
        control_cpus = {
            cpu for item in control_threads for cpu in item["affinity_cpu_ids"]
        }
        if (
            len(estimator_memberships) != 1
            or estimator_cpus.intersection(control_cpus)
            or any(
                item["cpuset_membership"] in estimator_memberships
                for item in control_threads
            )
        ):
            _fail("guardian estimator/control scheduler roles overlap")
        scheduler_witness = {
            "child_cpuset_members": child_members,
            "complete_control_plane_processes": control_processes,
            "complete_control_plane_threads": control_threads,
            "complete_descendant_population": descendants,
            "complete_descendant_threads": descendant_threads,
            "complete_descendant_tid_population": descendant_tids,
            "foreign_affinity_eligibility": [dict(item) for item in foreign],
        }
        _sha(row["scheduler_witness_sha256"], "guardian scheduler witness")
        if _sha_bytes(_canonical_json(scheduler_witness)) != row[
            "scheduler_witness_sha256"
        ]:
            _fail("guardian scheduler-witness digest differs")
        _sha(row["irq_population_sha256"], "guardian IRQ population SHA-256")
        if frozen_irq is None:
            frozen_irq = row["irq_population_sha256"]
        elif row["irq_population_sha256"] != frozen_irq:
            _fail("guardian IRQ population digest changed")
        surfaces = row["surface_states"]
        if type(surfaces) is not list or len(surfaces) != len(required):
            _fail("guardian surface-state population differs")
        surface_ids = []
        for surface in surfaces:
            state = _exact(
                surface, ("passed", "state_sha256", "surface_id"),
                "guardian surface state",
            )
            if state["passed"] is not True:
                _fail("guardian surface state did not pass")
            _sha(state["state_sha256"], "guardian surface-state SHA-256")
            surface_ids.append(state["surface_id"])
        if surface_ids != required:
            _fail("guardian observation omits or reorders a required surface")
        typed = row["typed_telemetry"]
        if type(typed) is not list or len(typed) != len(expected_sources):
            _fail("guardian typed telemetry population differs from the profile")
        retained_ids = []
        throttles = {}
        for source_value in typed:
            source = _exact(
                source_value, HELPER_SOURCE_KEYS,
                "guardian typed telemetry source",
            )
            source_id = source["source_id"]
            if type(source_id) is not str or source_id not in expected_sources:
                _fail("guardian typed source ID differs from the profile")
            retained_ids.append(source_id)
            expected = expected_sources[source_id]
            if source["cpu_id"] is not None:
                _u64(source["cpu_id"], "guardian telemetry CPU ID")
            if source["register"] is not None:
                _u64(source["register"], "guardian telemetry register")
            for key in (
                "measurement_kind", "source_kind", "source_path", "extraction",
                "cpu_id", "register", "source_spec_sha256",
            ):
                if source[key] != expected[key]:
                    _fail("guardian typed source differs from its derived specification")
            read_start = _u64(
                source["read_started_monotonic_ns"], "guardian telemetry read start",
            )
            read_end = _u64(
                source["read_ended_monotonic_ns"], "guardian telemetry read end",
            )
            if read_start < observed_start or read_end < read_start or read_end > observed_end:
                _fail("guardian telemetry read interval escapes its observation")
            parsed = _helper_raw_integer(source, "guardian source " + source_id)
            if expected["measurement_kind"] == "temperature":
                temperature = profile.value["telemetry_plan"]["temperature_source"]
                if not (
                    temperature["minimum_millicelsius"]
                    <= parsed <= temperature["maximum_millicelsius"]
                ):
                    _fail("guardian temperature lies outside the frozen range")
            else:
                throttles[source_id] = parsed
        if retained_ids != sorted(expected_sources):
            _fail("guardian typed telemetry is not complete, sorted, and unique")
        if previous_throttles is not None and throttles != previous_throttles:
            _fail("guardian AMD throttle counter changed")
        previous_throttles = throttles
        previous_hash = _sha_bytes(line)
        previous_start, previous_end = observed_start, observed_end
        final_scheduler_witness = scheduler_witness
        final_foreign_affinity_eligibility = [dict(item) for item in foreign]
    if (
        observed_count != count or observed_size != size
        or whole_digest.hexdigest() != seal["evidence_sha256"]
        or first_digest != seal["first_record_sha256"]
        or end < previous_end or end - previous_end > maximum_gap
        or previous_hash != seal["last_record_sha256"]
    ):
        _fail("guardian terminal coverage gap differs")
    if (
        final_scheduler_witness is None
        or seal["final_child_cpuset_member_count"]
        != len(final_scheduler_witness["child_cpuset_members"])
        or seal["final_control_plane_process_count"]
        != len(final_scheduler_witness["complete_control_plane_processes"])
        or seal["final_control_plane_tid_count"]
        != len(final_scheduler_witness["complete_control_plane_threads"])
        or seal["final_descendant_process_count"]
        != len(final_scheduler_witness["complete_descendant_population"])
        or seal["final_descendant_tid_count"]
        != len(final_scheduler_witness["complete_descendant_tid_population"])
        or seal["final_foreign_affinity_eligibility_count"]
        != len(final_foreign_affinity_eligibility)
        or seal["final_foreign_affinity_eligibility_sha256"]
        != _sha_bytes(_canonical_json({
            "rows": final_foreign_affinity_eligibility,
        }))
        or seal["final_scheduler_witness_sha256"]
        != _sha_bytes(_canonical_json(final_scheduler_witness))
        or seal["drift"] is not False
        or seal["irq_population_changed"] is not False
        or seal["thermal_or_throttle_event"] is not False
    ):
        _fail("guardian terminal population or latch differs")
    return seal


def _validate_helper_control_evidence(
    evidence_source: Any, seal_value: Any, checkpoint_value: Any,
    binding_digest: str, session_id: str, journal_id: str,
    expected_states: Sequence[Mapping[str, Any]], expected_peer: Mapping[str, int],
) -> Tuple[Mapping[str, Any], Tuple[Mapping[str, Any], ...]]:
    """Independently replay the complete helper-owned schema-v2 lifecycle."""

    seal = _exact(
        seal_value, HELPER_CONTROL_SEAL_KEYS, "helper control evidence seal",
    )
    if (
        seal["record_type"] != "cp2e_control_evidence_terminal"
        or type(seal["schema_version"]) is not int
        or seal["schema_version"] != HELPER_SCHEMA_VERSION
        or seal["binding_digest"] != binding_digest
        or seal["session_id"] != session_id
        or seal["journal_id"] != journal_id
        or seal["phases"] != ["prior", "applied", "restored"]
        or seal["restoration_matches_prior"] is not True
    ):
        _fail("helper control evidence terminal identity/lifecycle differs")
    count = _u64(seal["row_count"], "helper control evidence row count")
    size = _u64(seal["evidence_size_bytes"], "helper control evidence size")
    device = _u64(seal["device"], "helper control evidence device")
    inode = _u64(seal["inode"], "helper control evidence inode")
    if (
        count != MAX_CONTROL_EVIDENCE_ROWS or size == 0
        or size > MAX_CONTROL_EVIDENCE_BYTES
        or _guardian_source_size(evidence_source) != size
    ):
        _fail("helper control evidence size/count differs")
    for key in (
        "evidence_sha256", "first_record_sha256", "last_record_sha256",
    ):
        _sha(seal[key], "helper control evidence " + key)
    if isinstance(evidence_source, HeldControlEvidence) and (
        evidence_source.device != device or evidence_source.inode != inode
    ):
        _fail("helper control evidence seal differs from its held staging inode")

    sealed_receipts = seal["rows"]
    if type(sealed_receipts) is not list or len(sealed_receipts) != count:
        _fail("helper control evidence sealed receipt population differs")
    states: List[Mapping[str, Any]] = []
    receipts: List[Mapping[str, Any]] = []
    prefix_digests: List[str] = []
    cumulative = hashlib.sha256()
    cumulative_size = 0
    previous = "0" * 64
    first: Any = None
    phases = ("prior", "applied", "restored")
    for index, (raw, line) in enumerate(_guardian_source_rows(
        evidence_source, MAX_CONTROL_EVIDENCE_LINE_BYTES,
    )):
        if index >= count or len(line) > MAX_CONTROL_EVIDENCE_LINE_BYTES:
            _fail("helper control evidence exceeds its exact row/line bound")
        row = _exact(raw, HELPER_CONTROL_ROW_KEYS, "helper control evidence row")
        if _canonical_json(dict(row)) != line:
            _fail("helper control evidence row is not canonical JSON")
        if (
            row["record_type"] != "cp2e_control_state"
            or type(row["schema_version"]) is not int
            or row["schema_version"] != HELPER_SCHEMA_VERSION
            or type(row["sequence"]) is not int or row["sequence"] != index
            or row["phase"] != phases[index]
            or row["previous_record_sha256"] != previous
            or row["binding_digest"] != binding_digest
            or row["session_id"] != session_id
            or row["journal_id"] != journal_id
            or type(row["state"]) is not dict
        ):
            _fail("helper control evidence row identity/chain differs")
        state_bytes = _canonical_json(dict(row["state"]))
        if (
            len(state_bytes) > MAX_CONTROL_STATE_BYTES
            or row["state_sha256"] != _sha_bytes(state_bytes)
        ):
            _fail("helper control evidence full-state digest/bound differs")
        line_sha = _sha_bytes(line)
        cumulative.update(line)
        cumulative_size += len(line)
        receipt = {
            "end_offset_bytes": cumulative_size,
            "phase": phases[index],
            "record_sha256": line_sha,
            "sequence": index,
            "state_sha256": row["state_sha256"],
        }
        if _exact(
            sealed_receipts[index], HELPER_CONTROL_RECEIPT_KEYS,
            "sealed helper control row receipt",
        ) != receipt:
            _fail("helper control evidence sealed row receipt differs")
        if first is None:
            first = line_sha
        previous = line_sha
        states.append(row["state"])
        receipts.append(receipt)
        prefix_digests.append(cumulative.hexdigest())
    if (
        len(states) != count or cumulative_size != size
        or cumulative.hexdigest() != seal["evidence_sha256"]
        or _guardian_source_sha256(evidence_source) != seal["evidence_sha256"]
        or first != seal["first_record_sha256"]
        or previous != seal["last_record_sha256"]
        or receipts != sealed_receipts
    ):
        _fail("helper control evidence terminal byte/endpoint binding differs")
    if (
        len(expected_states) != 3
        or any(type(item) is not dict for item in expected_states)
        or any(states[index] != expected_states[index] for index in range(3))
        or _canonical_json(dict(states[0])) != _canonical_json(dict(states[2]))
    ):
        _fail("helper control lifecycle differs from retained prior/applied/restored states")
    for state in states:
        if state.get("schema_version") == 2 and state["privileged_v2"]["peer"] != expected_peer:
            _fail("helper control lifecycle peer differs from SO_PEERCRED")

    checkpoint = _exact(
        checkpoint_value, HELPER_CONTROL_CHECKPOINT_KEYS,
        "helper control evidence applied checkpoint",
    )
    checkpoint_count = _u64(
        checkpoint["row_count"], "helper control checkpoint row count",
    )
    checkpoint_size = _u64(
        checkpoint["evidence_size_bytes"], "helper control checkpoint size",
    )
    _sha(checkpoint["evidence_sha256"], "helper control checkpoint evidence SHA-256")
    _sha(checkpoint["last_record_sha256"], "helper control checkpoint last SHA-256")
    if (
        checkpoint_count != 2
        or checkpoint["rows"] != receipts[:2]
        or checkpoint["device"] != device or checkpoint["inode"] != inode
        or checkpoint_size != receipts[1]["end_offset_bytes"]
        or checkpoint["last_record_sha256"] != receipts[1]["record_sha256"]
        or checkpoint["evidence_sha256"] != prefix_digests[1]
    ):
        _fail("helper control evidence applied checkpoint differs from its prefix")
    return seal, tuple(states)


def _validate_helper_observation_receipt(
    value: Any, expected_run_index: int, expected_phase: str,
    profile: profile_codec.FrozenTimingProfile,
    expected_sources: Mapping[str, Mapping[str, Any]],
    command: Mapping[str, Any], ros_command: Mapping[str, Any],
) -> Mapping[str, Mapping[str, Any]]:
    receipt = _exact(value, HELPER_RECEIPT_KEYS, "helper observation receipt")
    if (
        type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != HELPER_SCHEMA_VERSION
        or receipt["run_index"] != expected_run_index
        or type(receipt["run_index"]) is not int
        or receipt["phase"] != expected_phase
    ):
        _fail("helper observation receipt slot/phase differs")
    started = _u64(receipt["snapshot_started_monotonic_ns"], "helper snapshot start")
    ended = _u64(receipt["snapshot_ended_monotonic_ns"], "helper snapshot end")
    skew = _u64(receipt["snapshot_skew_ns"], "helper snapshot skew")
    if (
        ended < started or skew != ended - started
        or skew > profile.value["telemetry_plan"]["aperf_mperf"]["maximum_snapshot_skew_ns"]
        or started < command["started_monotonic_ns"]
        or ended > command["ended_monotonic_ns"]
    ):
        _fail("helper observation interval/skew escapes its frozen clock command")
    if expected_phase == "pre" and ended > ros_command["started_monotonic_ns"]:
        _fail("helper pre observation does not precede the ROS command")
    if expected_phase == "post" and started < ros_command["ended_monotonic_ns"]:
        _fail("helper post observation does not follow the ROS command")
    sources = receipt["sources"]
    if type(sources) is not list or len(sources) != len(expected_sources):
        _fail("helper raw-source population differs from the profile")
    retained: Dict[str, Mapping[str, Any]] = {}
    source_ids = []
    for source_value in sources:
        source = _exact(source_value, HELPER_SOURCE_KEYS, "helper raw source")
        source_id = source["source_id"]
        if type(source_id) is not str or source_id not in expected_sources:
            _fail("helper raw source ID differs from the derived profile population")
        source_ids.append(source_id)
        expected = expected_sources[source_id]
        if source["cpu_id"] is not None:
            _u64(source["cpu_id"], "helper raw-source CPU ID")
        if source["register"] is not None:
            _u64(source["register"], "helper raw-source register")
        for key in (
            "measurement_kind", "source_kind", "source_path", "extraction",
        ):
            if type(source[key]) is not str or not source[key]:
                _fail("helper raw-source " + key + " is not bounded text")
        for key in (
            "measurement_kind", "source_kind", "source_path", "extraction",
            "cpu_id", "register", "source_spec_sha256",
        ):
            if source[key] != expected[key]:
                _fail("helper raw source differs from its independently derived specification")
        if not os.path.isabs(source["source_path"]) or os.path.normpath(source["source_path"]) != source["source_path"]:
            _fail("helper raw source path is not normalized absolute syntax")
        read_started = _u64(
            source["read_started_monotonic_ns"], "helper raw-source read start",
        )
        read_ended = _u64(
            source["read_ended_monotonic_ns"], "helper raw-source read end",
        )
        if read_started < started or read_ended < read_started or read_ended > ended:
            _fail("helper raw-source interval escapes its snapshot")
        parsed = _helper_raw_integer(source, "helper source " + source_id)
        if source["measurement_kind"] == "temperature":
            _i64(parsed, "helper temperature")
        else:
            _u64(parsed, "helper unsigned telemetry")
        retained[source_id] = {
            "value": parsed,
            "raw_hex": source["raw_hex"],
            "raw_sha256": source["raw_sha256"],
            "started": read_started,
            "ended": read_ended,
        }
    if source_ids != sorted(expected_sources) or source_ids != sorted(set(source_ids)):
        _fail("helper raw-source IDs are not complete, sorted, and unique")
    return retained


def _checked_u128_product(left: int, right: int, label: str) -> int:
    _u64(left, label + " left operand")
    _u64(right, label + " right operand")
    value = left * right
    if value > U128_MAX:
        _fail(label + " exceeds the frozen u128 checked-arithmetic domain")
    return value


def _cross_validate_helper_run(
    run_index: int, observations: Mapping[str, Mapping[str, Mapping[str, Any]]],
    profile: profile_codec.FrozenTimingProfile,
    support_files: Mapping[str, bytes],
) -> None:
    pre_values = observations["pre"]
    post_values = observations["post"]
    telemetry = profile.value["telemetry_plan"]
    counters = telemetry["aperf_mperf"]
    expected_cpus = telemetry["required_cpu_ids"]
    reference = counters["reference_frequency_khz"]
    minimum = counters["minimum_effective_frequency_khz"]
    maximum = counters["maximum_effective_frequency_khz"]
    target = counters["target_frequency_khz"]
    temperature = telemetry["temperature_source"]

    for phase_values in (pre_values, post_values):
        if phase_values["shared.f_ref"]["value"] != reference:
            _fail("helper reference frequency differs from the frozen profile")
        measured_temperature = phase_values["shared.temperature"]["value"]
        if not (
            temperature["minimum_millicelsius"]
            <= measured_temperature
            <= temperature["maximum_millicelsius"]
        ):
            _fail("helper temperature lies outside the inclusive frozen range")
    for cpu in expected_cpus:
        aperf_id = "cpu{}.aperf".format(cpu)
        mperf_id = "cpu{}.mperf".format(cpu)
        throttle_id = "cpu{}.amd_throttle".format(cpu)
        frequency_id = "cpu{}.scaling_cur_freq".format(cpu)
        aperf_pre = pre_values[aperf_id]["value"]
        aperf_post = post_values[aperf_id]["value"]
        mperf_pre = pre_values[mperf_id]["value"]
        mperf_post = post_values[mperf_id]["value"]
        if aperf_post <= aperf_pre or mperf_post <= mperf_pre:
            _fail("helper APERF/MPERF counters did not strictly advance without wrap")
        if pre_values[throttle_id]["value"] != post_values[throttle_id]["value"]:
            _fail("helper AMD throttle counter changed during a timing run")
        if (
            pre_values[frequency_id]["value"] != target
            or post_values[frequency_id]["value"] != target
        ):
            _fail("helper current frequency differs from the exact frozen target")
        delta_a = aperf_post - aperf_pre
        delta_m = mperf_post - mperf_pre
        left = _checked_u128_product(minimum, delta_m, "effective-frequency lower product")
        middle = _checked_u128_product(reference, delta_a, "effective-frequency measured product")
        right = _checked_u128_product(maximum, delta_m, "effective-frequency upper product")
        if not left <= middle <= right:
            _fail("helper APERF/MPERF effective frequency violates the exact inclusive bounds")

    prefix = "runs/run_{}/".format(run_index)
    legacy_by_phase = {}
    for phase in ("pre", "post"):
        _snapshot, legacy = _parse_raw_telemetry_snapshot(
            support_files[prefix + "telemetry_{}.json".format(phase)],
            phase, "helper/legacy telemetry join",
        )
        helper = observations[phase]
        expected_values: Dict[Tuple[str, Optional[int]], int] = {
            ("aperf", cpu): helper["cpu{}.aperf".format(cpu)]["value"]
            for cpu in expected_cpus
        }
        expected_values.update({
            ("mperf", cpu): helper["cpu{}.mperf".format(cpu)]["value"]
            for cpu in expected_cpus
        })
        expected_values.update({
            ("scaling_current_frequency_khz", cpu):
            helper["cpu{}.scaling_cur_freq".format(cpu)]["value"]
            for cpu in expected_cpus
        })
        expected_values.update({
            ("thermal_throttle_count", cpu):
            helper["cpu{}.amd_throttle".format(cpu)]["value"]
            for cpu in expected_cpus
        })
        expected_values[("temperature_millicelsius", None)] = helper[
            "shared.temperature"
        ]["value"]
        if set(legacy) != set(expected_values) or any(
            legacy[key]["value"] != value for key, value in expected_values.items()
        ):
            _fail("legacy timing telemetry differs from the authoritative helper receipt")
        clock = _exact(
            _strict_json(
                support_files[prefix + "clock_{}.json".format(phase)],
                "helper clock join",
            ), CLOCK_KEYS, "helper clock join",
        )
        if any(
            item["reference_frequency_khz"] != helper["shared.f_ref"]["value"]
            for item in clock["telemetry"]["cpu_counters"]
        ):
            _fail("legacy clock reference frequency differs from the helper receipt")
        legacy_by_phase[phase] = legacy


def _validate_helper_evidence(
    transcript_bytes: bytes, terminal_bytes: bytes,
    guardian_evidence_source: Any, control_evidence_source: Any,
    profile: profile_codec.FrozenTimingProfile,
    provenance: Mapping[str, Any], commands: Sequence[Mapping[str, Any]],
    support_files: Mapping[str, bytes], prior_receipt: Mapping[str, Any],
    applied_receipt: Mapping[str, Any], restored_receipt: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Replay privileged evidence without importing or trusting helper code."""

    events = _strict_jsonl(transcript_bytes, "privileged-helper transcript")
    if len(events) != 16:
        _fail("successful helper transcript does not contain exactly 16 events")
    previous = "0" * 64
    event_lines = transcript_bytes.splitlines(keepends=True)
    for index, (event, line) in enumerate(zip(events, event_lines)):
        row = _exact(event, HELPER_EVENT_KEYS, "privileged-helper event")
        if (
            row["record_type"] != "cp2e_privileged_transcript_event"
            or type(row["schema_version"]) is not int
            or row["schema_version"] != HELPER_SCHEMA_VERSION
            or type(row["event_index"]) is not int
            or row["event_index"] != index
            or row["previous_record_sha256"] != previous
            or type(row["payload"]) is not dict
        ):
            _fail("privileged-helper event identity/hash chain differs")
        previous = _sha_bytes(line)
    expected_events = ["challenge", "hello", "apply"] + [
        "run_slot_observation" for _run in range(6) for _phase in ("pre", "post")
    ] + ["restore"]
    if [event["event"] for event in events] != expected_events:
        _fail("privileged-helper event sequence differs from the exact success path")

    terminal = _exact(
        _strict_json(terminal_bytes, "privileged-helper terminal receipt"),
        HELPER_TERMINAL_KEYS, "privileged-helper terminal receipt",
    )
    binding = _helper_binding(profile)
    binding_digest = _sha_bytes(_canonical_json(binding))
    if (
        terminal["record_type"] != "cp2e_privileged_transcript_terminal"
        or type(terminal["schema_version"]) is not int
        or terminal["schema_version"] != HELPER_SCHEMA_VERSION
        or terminal["binding"] != binding
        or terminal["binding_digest"] != binding_digest
        or terminal["terminal_status"] != "success"
        or terminal["abnormal_recovery"] is not False
        or terminal["restoration_proved"] is not True
        or terminal["error_type"] is not None
        or type(terminal["event_count"]) is not int
        or terminal["event_count"] != 16
        or type(terminal["transcript_size_bytes"]) is not int
        or terminal["transcript_size_bytes"] != len(transcript_bytes)
        or terminal["transcript_sha256"] != _sha_bytes(transcript_bytes)
        or terminal["first_record_sha256"] != _sha_bytes(event_lines[0])
        or terminal["last_record_sha256"] != previous
        or type(terminal["population_stop_count"]) is not int
        or terminal["population_stop_count"] != 1
    ):
        _fail("privileged-helper successful terminal receipt differs")
    for key in (
        "binding_digest", "transcript_sha256", "first_record_sha256",
        "last_record_sha256", "population_seal_sha256",
        "terminal_journal_sha256", "session_id",
        "control_evidence_seal_sha256",
    ):
        _sha(terminal[key], "helper terminal " + key)
    if type(terminal["journal_id"]) is not str or re.fullmatch(r"[0-9a-f]{32}", terminal["journal_id"]) is None:
        _fail("helper terminal journal ID differs")
    closure = _exact(
        terminal["descriptor_closure"], ("attempted", "error_type", "passed"),
        "helper descriptor closure",
    )
    if closure != {"attempted": True, "error_type": None, "passed": True}:
        _fail("helper descriptor closure is not exact and normal")

    challenge = _exact(
        events[0]["payload"],
        ("binding", "binding_digest", "challenge_nonce", "peer"),
        "helper challenge",
    )
    if challenge["binding"] != binding or challenge["binding_digest"] != binding_digest:
        _fail("helper challenge binding differs")
    if type(challenge["challenge_nonce"]) is not str or re.fullmatch(r"[0-9a-f]{64}", challenge["challenge_nonce"]) is None:
        _fail("helper challenge nonce is not exactly 256 bits")
    peer = _helper_peer(challenge["peer"], "helper challenge peer")
    hello = _exact(
        events[1]["payload"],
        ("binding", "challenge_nonce", "client_nonce", "nonce_sha256", "peer", "session_id"),
        "helper hello",
    )
    if (
        hello["binding"] != binding or hello["peer"] != peer
        or hello["challenge_nonce"] != challenge["challenge_nonce"]
        or type(hello["client_nonce"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", hello["client_nonce"]) is None
    ):
        _fail("helper hello peer/binding/nonce differs")
    expected_nonce = _sha_bytes(
        bytes.fromhex(hello["challenge_nonce"]) + bytes.fromhex(hello["client_nonce"])
    )
    expected_session = _sha_bytes(_canonical_json({
        "binding_digest": binding_digest,
        "challenge_nonce": hello["challenge_nonce"],
        "client_nonce": hello["client_nonce"],
        "peer": peer,
    }))
    if (
        hello["nonce_sha256"] != expected_nonce
        or hello["session_id"] != expected_session
        or terminal["session_id"] != expected_session
    ):
        _fail("helper nonce/session derivation differs")

    applied = _exact(
        events[2]["payload"],
        ("applied_state_sha256", "control_evidence_checkpoint",
         "control_evidence_checkpoint_sha256", "journal_id",
         "population_binding", "population_binding_sha256", "population_state",
         "prior_state_sha256"),
        "helper apply",
    )
    if (
        type(applied["population_binding"]) is not dict
        or applied["population_binding_sha256"]
        != _sha_bytes(_canonical_json(applied["population_binding"]))
        or type(applied["population_state"]) is not dict
        or applied["journal_id"] != terminal["journal_id"]
        or applied["prior_state_sha256"] != prior_receipt["state_sha256"]
        or applied["applied_state_sha256"] != applied_receipt["state_sha256"]
        or type(applied["control_evidence_checkpoint"]) is not dict
        or applied["control_evidence_checkpoint_sha256"]
        != _sha_bytes(_canonical_json(applied["control_evidence_checkpoint"]))
    ):
        _fail("helper apply checkpoint/state/journal binding differs")

    expected_sources = _helper_source_specifications(profile)
    run_observations: Dict[int, Dict[str, Mapping[str, Mapping[str, Any]]]] = {}
    final_seal: Any = None
    for run_index in range(6):
        slot = profile.value["pairing"]["run_slots"][run_index]
        if set(slot) != set(HELPER_RUN_SLOT_KEYS):
            _fail("profile helper run slot keys differ")
        run_observations[run_index] = {}
        for phase_index, phase in enumerate(("pre", "post")):
            event = events[3 + 2 * run_index + phase_index]
            payload = _exact(
                event["payload"], HELPER_OBSERVATION_PAYLOAD_KEYS,
                "helper run observation",
            )
            retained_slot = _exact(
                payload["run_slot"], HELPER_RUN_SLOT_KEYS,
                "helper transcript run slot",
            )
            for key in (
                "run_index", "timing_pair_index", "position_in_pair",
            ):
                _u64(retained_slot[key], "helper run-slot " + key)
            if (
                retained_slot != slot or type(retained_slot["mode"]) is not str
                or payload["phase"] != phase or type(payload["phase"]) is not str
            ):
                _fail("helper observation slot or phase differs")
            if (
                type(payload["population"]) is not dict
                or payload["population_sha256"]
                != _sha_bytes(_canonical_json(payload["population"]))
            ):
                _fail("helper observation population digest differs")
            receipt = payload["receipt"]
            if type(receipt) is not dict or payload["receipt_sha256"] != _sha_bytes(_canonical_json(receipt)):
                _fail("helper observation receipt digest differs")
            command_index = run_index * len(COMMAND_PHASES) + (0 if phase == "pre" else 3)
            values = _validate_helper_observation_receipt(
                receipt, run_index, phase, profile, expected_sources,
                commands[command_index], commands[run_index * len(COMMAND_PHASES) + 2],
            )
            run_observations[run_index][phase] = values
            terminal_post = run_index == 5 and phase == "post"
            if terminal_post:
                if (
                    type(payload["population_seal"]) is not dict
                    or payload["population_seal_sha256"]
                    != _sha_bytes(_canonical_json(payload["population_seal"]))
                    or payload["population_seal_sha256"] != terminal["population_seal_sha256"]
                ):
                    _fail("helper terminal population seal digest differs")
                final_seal = _validate_helper_guardian_evidence(
                    guardian_evidence_source, payload["population_seal"],
                    profile, peer, expected_session,
                    applied_receipt["captured_monotonic_ns"],
                    receipt["snapshot_ended_monotonic_ns"],
                )
            elif payload["population_seal"] is not None or payload["population_seal_sha256"] is not None:
                _fail("helper nonterminal observation contains a guardian seal")
        _cross_validate_helper_run(
            run_index, run_observations[run_index], profile, support_files,
        )
    if final_seal is None:
        _fail("helper transcript lacks its unique terminal guardian seal")
    if any(
        command["started_monotonic_ns"]
        < final_seal["coverage_started_monotonic_ns"]
        or command["ended_monotonic_ns"]
        > final_seal["coverage_ended_monotonic_ns"]
        for command in commands if command["phase"] == "ros_run"
    ):
        _fail("guardian seal does not enclose every estimator command interval")

    restored = _exact(
        events[-1]["payload"],
        ("abnormal_recovery", "control_evidence_seal",
         "control_evidence_seal_sha256", "population_seal_sha256",
         "prior_state_sha256", "prior_state_sha256_matches",
         "restored_state_sha256", "terminal_journal_sha256"),
        "helper restore",
    )
    if type(restored["control_evidence_seal"]) is not dict:
        _fail("helper restoration omits the full control-evidence seal")
    control_seal, control_states = _validate_helper_control_evidence(
        control_evidence_source, restored["control_evidence_seal"],
        applied["control_evidence_checkpoint"], binding_digest,
        expected_session, terminal["journal_id"],
        (
            prior_receipt["state"], applied_receipt["state"],
            restored_receipt["state"],
        ), peer,
    )
    if (
        restored["abnormal_recovery"] is not False
        or restored["prior_state_sha256_matches"] is not True
        or restored["prior_state_sha256"] != applied["prior_state_sha256"]
        or restored["restored_state_sha256"] != applied["prior_state_sha256"]
        or restored["restored_state_sha256"]
        != control_seal["rows"][-1]["state_sha256"]
        or restored["control_evidence_seal_sha256"]
        != _sha_bytes(_canonical_json(restored["control_evidence_seal"]))
        or restored["control_evidence_seal_sha256"]
        != terminal["control_evidence_seal_sha256"]
        or restored["population_seal_sha256"] != terminal["population_seal_sha256"]
        or restored["terminal_journal_sha256"] != terminal["terminal_journal_sha256"]
        or control_states[0] != control_states[2]
    ):
        _fail("helper restoration/journal proof differs")
    expected_provenance = {
        "helper_transcript_sha256": _sha_bytes(transcript_bytes),
        "helper_terminal_receipt_sha256": _sha_bytes(terminal_bytes),
        "helper_binding_digest": binding_digest,
        "helper_terminal_journal_sha256": terminal["terminal_journal_sha256"],
        "helper_population_seal_sha256": terminal["population_seal_sha256"],
        "helper_guardian_evidence_sha256": final_seal["evidence_sha256"],
        "helper_control_evidence_sha256": control_seal["evidence_sha256"],
        "helper_control_evidence_seal_sha256": terminal[
            "control_evidence_seal_sha256"
        ],
    }
    if any(provenance[key] != value for key, value in expected_provenance.items()):
        _fail("helper transcript identity differs from retained provenance")
    return terminal


def _required_run_support(run_index: int) -> Tuple[str, ...]:
    prefix = "runs/run_{}/".format(run_index)
    return tuple(prefix + name for name in (
        "context.json", "serial.jsonl", "callbacks.jsonl", "updater.jsonl",
        "timing.jsonl", "clock_pre.json", "clock_post.json",
        "runtime_parameters.json", "loader_before.txt", "loader_after.txt",
        "stdout.bin", "stderr.bin", "affinity_audit.json",
        "process_identity.json", "runtime_preflight.json",
        "telemetry_pre.json", "telemetry_post.json",
        "telemetry_comparison.json",
    ))


def trace_bundle_sha256(support_files: Mapping[str, bytes], run_index: int) -> str:
    """Hash the exact closed support inventory for one run."""

    _u64(run_index, "trace-bundle run index")
    required = _required_run_support(run_index)
    if set(support_files) != set(required):
        _fail("run support-file population differs from the frozen inventory")
    digest = hashlib.sha256()
    digest.update(b"SchurVIO-CP2-E-run-trace-bundle-v3\0")
    digest.update(run_index.to_bytes(8, "big"))
    for relative in sorted(required):
        payload = support_files[relative]
        if type(payload) is not bytes:
            _fail("run support-file payload is not bytes")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


@dataclass(frozen=True)
class TimingSample:
    profile_sha256: str
    timing_pair_index: int
    run_index: int
    mode: str
    sequence_index: int
    sequence_id: str
    serial_pair_index: int
    cam0_record_time_ns: int
    camera_timestamp_ns: int
    invocation_id: int
    terminal_status: str
    terminal_subreason: str
    input_feature_count: int
    raw_system_count: int
    baseline_preflight_attempted: bool
    nonempty: bool
    preflight_accepted: bool
    committed: bool
    primary: bool
    timer_clock: str
    timer_start_ns: int
    timer_end_ns: int
    duration_ns: int

    def __post_init__(self) -> None:
        _sha(self.profile_sha256, "sample profile SHA-256")
        pair = _u64(self.timing_pair_index, "sample timing-pair index")
        run = _u64(self.run_index, "sample run index")
        if pair > 2 or run > 5 or run // 2 != pair:
            _fail("sample pair/run indices disagree")
        if self.mode not in ("nullspace", "schur") or self.mode != PAIR_ORDER[pair][run % 2]:
            _fail("sample mode differs from the frozen run slot")
        if _u64(self.sequence_index, "sample sequence index") > 2:
            _fail("sample sequence index is outside CP2")
        _safe_id(self.sequence_id, "sample sequence ID")
        for value, label in (
            (self.serial_pair_index, "sample serial-pair index"),
            (self.cam0_record_time_ns, "sample cam0 record time"),
            (self.camera_timestamp_ns, "sample camera timestamp"),
            (self.invocation_id, "sample invocation ID"),
            (self.input_feature_count, "sample input-feature count"),
            (self.raw_system_count, "sample raw-system count"),
            (self.timer_start_ns, "sample timer start"),
            (self.timer_end_ns, "sample timer end"),
            (self.duration_ns, "sample duration"),
        ):
            _u64(value, label)
        for name in (
            "baseline_preflight_attempted", "nonempty", "preflight_accepted",
            "committed", "primary",
        ):
            _boolean(getattr(self, name), "sample " + name)
        if (
            self.terminal_status not in TERMINAL_MAPPING
            or self.terminal_subreason not in TERMINAL_MAPPING[self.terminal_status]
        ):
            _fail("sample terminal status/subreason mapping is invalid")
        if self.timer_clock != TIMER_CLOCK:
            _fail("sample timer clock differs from the frozen clock")
        if self.timer_end_ns < self.timer_start_ns:
            _fail("sample timer endpoints are reverse ordered")
        if self.duration_ns != self.timer_end_ns - self.timer_start_ns:
            _fail("sample duration differs from checked endpoint subtraction")
        expected_nonempty = self.input_feature_count != 0
        expected_committed = self.terminal_status == "committed_counted"
        expected_primary = expected_nonempty and self.preflight_accepted and expected_committed
        if self.nonempty is not expected_nonempty:
            _fail("sample nonempty flag differs from the input-feature count")
        if self.committed is not expected_committed:
            _fail("sample committed flag differs from terminal status")
        if self.primary is not expected_primary:
            _fail("sample primary flag differs from reconstructed lifecycle")
        if self.preflight_accepted and not self.baseline_preflight_attempted:
            _fail("sample accepts a preflight that was not attempted")
        if self.raw_system_count > self.input_feature_count:
            _fail("sample raw-system count exceeds input-feature count")
        if self.terminal_status == "empty_input" and self.nonempty:
            _fail("empty-input terminal status has a nonempty input")
        if self.terminal_status != "empty_input" and not self.nonempty:
            _fail("non-empty-input terminal status has an empty input")
        if expected_committed and (
            self.raw_system_count == 0
            or not self.baseline_preflight_attempted
            or not self.preflight_accepted
        ):
            _fail("committed sample lacks the required nonempty accepted lifecycle")

    def record(self) -> Mapping[str, Any]:
        return {
            "schema_version": 1, "record_type": "updater_timing_derived",
            **{key: getattr(self, key) for key in SAMPLE_KEYS
               if key not in ("schema_version", "record_type")},
        }


def sample_from_record(value: Any) -> TimingSample:
    row = _exact(value, SAMPLE_KEYS, "timing sample")
    if row["schema_version"] != 1 or row["record_type"] != "updater_timing_derived":
        _fail("timing sample identity differs")
    return TimingSample(**{
        key: row[key] for key in SAMPLE_KEYS
        if key not in ("schema_version", "record_type")
    })


@dataclass(frozen=True)
class TimingRun:
    run_index: int
    profile_sha256: str
    run_id: str
    fresh_process_identity: str
    pre_snapshot_sha256: str
    post_snapshot_sha256: str
    trace_bundle_sha256: str
    process_identity_sha256: str
    runtime_preflight_sha256: str
    telemetry_pre_sha256: str
    telemetry_post_sha256: str
    telemetry_comparison_sha256: str
    samples: Tuple[TimingSample, ...] = ()

    def __post_init__(self) -> None:
        run = _u64(self.run_index, "timing run index")
        if run > 5:
            _fail("timing run index exceeds five")
        _sha(self.profile_sha256, "run profile SHA-256")
        _safe_id(self.run_id, "run ID")
        _sha(self.fresh_process_identity, "fresh-process identity")
        for value, label in (
            (self.pre_snapshot_sha256, "pre snapshot"),
            (self.post_snapshot_sha256, "post snapshot"),
            (self.trace_bundle_sha256, "trace bundle"),
            (self.process_identity_sha256, "process identity"),
            (self.runtime_preflight_sha256, "runtime preflight"),
            (self.telemetry_pre_sha256, "pre telemetry"),
            (self.telemetry_post_sha256, "post telemetry"),
            (self.telemetry_comparison_sha256, "telemetry comparison"),
        ):
            _sha(value, label)
        if type(self.samples) is not tuple:
            _fail("timing run samples are not an exact tuple")
        previous_invocation: Optional[int] = None
        seen = set()
        for sample in self.samples:
            if type(sample) is not TimingSample or sample.run_index != run:
                _fail("timing sample does not belong to its run")
            identity = (sample.serial_pair_index, sample.invocation_id)
            if identity in seen:
                _fail("timing run contains a duplicate joined sample identity")
            seen.add(identity)
            if previous_invocation is not None and sample.invocation_id <= previous_invocation:
                _fail("timing invocation IDs are not strictly increasing")
            previous_invocation = sample.invocation_id

    @property
    def timing_pair_index(self) -> int:
        return self.run_index // 2

    @property
    def mode(self) -> str:
        return PAIR_ORDER[self.timing_pair_index][self.run_index % 2]

    def record(self) -> Mapping[str, Any]:
        return {
            "run_index": self.run_index,
            "timing_pair_index": self.timing_pair_index,
            "position_in_pair": self.run_index % 2,
            "mode": self.mode,
            "profile_sha256": self.profile_sha256,
            "run_id": self.run_id,
            "fresh_process_identity": self.fresh_process_identity,
            "pre_snapshot_sha256": self.pre_snapshot_sha256,
            "post_snapshot_sha256": self.post_snapshot_sha256,
            "trace_bundle_sha256": self.trace_bundle_sha256,
            "process_identity_sha256": self.process_identity_sha256,
            "runtime_preflight_sha256": self.runtime_preflight_sha256,
            "telemetry_pre_sha256": self.telemetry_pre_sha256,
            "telemetry_post_sha256": self.telemetry_post_sha256,
            "telemetry_comparison_sha256": self.telemetry_comparison_sha256,
            "sample_count": len(self.samples),
            "eligible_sample_count": sum(sample.primary for sample in self.samples),
        }


@dataclass(frozen=True)
class TimingAssemblyInput:
    profile_bytes: bytes
    profile_sha256: str
    execution_binding_bytes: bytes
    control_prior_bytes: bytes
    control_applied_bytes: bytes
    control_restored_bytes: bytes
    helper_transcript_bytes: bytes
    helper_terminal_receipt_bytes: bytes
    # Exact bytes are accepted only by synthetic protecting profiles.  Formal
    # recorded assembly must supply HeldGuardianEvidence for the helper-written
    # file already resident at guardian_evidence.jsonl.
    helper_guardian_evidence_bytes: Any
    # The full prior/applied/restored schema-v2 lifecycle follows the same
    # direct-held formal staging rule at control_evidence.jsonl.
    helper_control_evidence_bytes: Any
    sequence_index: int
    sequence_id: str
    bag_begin_record_time_ns: int
    frozen_offset_ns: int
    runs: Tuple[TimingRun, TimingRun, TimingRun, TimingRun, TimingRun, TimingRun]
    provenance: Mapping[str, Any]
    commands: Tuple[Mapping[str, Any], ...]
    support_files: Mapping[str, bytes]


@dataclass(frozen=True)
class TimingAssemblyResult:
    manifest_sha256: str
    passed: bool


class HeldGuardianEvidence:
    """Read-only held identity for helper-written formal guardian evidence.

    Formal assembly adopts the helper-created file already located at the
    frozen artifact-relative name.  It never rewrites, copies, truncates, or
    materializes that file.  The held descriptor and pathname must continue to
    identify the same sealed, single-link regular inode for the whole assembly.
    """

    def __init__(
        self, path: Path, expected_sha256: str, expected_size_bytes: int,
        *, _evidence_label: str = "guardian",
    ) -> None:
        self._evidence_label = _evidence_label
        path = Path(path).absolute()
        if path == Path("/") or path != Path(os.path.normpath(str(path))):
            _fail("held " + _evidence_label + " evidence path is not normalized absolute syntax")
        _sha(expected_sha256, "held " + _evidence_label + " evidence SHA-256")
        _u64(expected_size_bytes, "held " + _evidence_label + " evidence size")
        if expected_size_bytes == 0:
            _fail("held " + _evidence_label + " evidence is empty")
        flags = os.O_RDONLY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(str(path), flags)
        try:
            status = os.fstat(descriptor)
            named = path.lstat()
            if (
                not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or named.st_dev != status.st_dev or named.st_ino != status.st_ino
                or named.st_nlink != 1 or status.st_size != expected_size_bytes
                or stat.S_IMODE(status.st_mode) & 0o022
            ):
                _fail("held " + _evidence_label + " evidence inode/permissions differ")
            digest = hashlib.sha256()
            offset = 0
            while offset < status.st_size:
                chunk = os.pread(
                    descriptor, min(1024 * 1024, status.st_size - offset), offset,
                )
                if not chunk:
                    _fail("held " + _evidence_label + " evidence read ended early")
                digest.update(chunk)
                offset += len(chunk)
            if digest.hexdigest() != expected_sha256:
                _fail("held " + _evidence_label + " evidence digest differs from its seal")
        except BaseException:
            os.close(descriptor)
            raise
        self.path = path
        self.descriptor = descriptor
        self.device = status.st_dev
        self.inode = status.st_ino
        self.size_bytes = status.st_size
        self.sha256 = expected_sha256
        self._closed = False

    def verify_held(self) -> None:
        if self._closed:
            _fail("held " + self._evidence_label + " evidence descriptor is closed")
        held = os.fstat(self.descriptor)
        named = self.path.lstat()
        if (
            not stat.S_ISREG(held.st_mode) or held.st_nlink != 1
            or held.st_dev != self.device or held.st_ino != self.inode
            or named.st_dev != self.device or named.st_ino != self.inode
            or named.st_nlink != 1 or held.st_size != self.size_bytes
            or stat.S_IMODE(held.st_mode) & 0o022
        ):
            _fail("held " + self._evidence_label + " evidence identity changed")

    def close(self) -> None:
        if not self._closed:
            os.close(self.descriptor)
            self._closed = True

    def __enter__(self) -> "HeldGuardianEvidence":
        self.verify_held()
        return self

    def __exit__(self, _kind: Any, _value: Any, _traceback: Any) -> bool:
        self.close()
        return False


class HeldControlEvidence(HeldGuardianEvidence):
    """Read-only held identity for the helper-written control-state stream."""

    def __init__(
        self, path: Path, expected_sha256: str, expected_size_bytes: int,
    ) -> None:
        super().__init__(
            path, expected_sha256, expected_size_bytes,
            _evidence_label="control",
        )


def _checked_boundary(begin: int, offset: int, warmup: int) -> int:
    begin_value = _u64(begin, "bag-begin record time")
    offset_value = _u64(offset, "frozen offset")
    warmup_value = _u64(warmup, "warm-up duration")
    if (
        begin_value > U64_MAX - offset_value
        or begin_value + offset_value > U64_MAX - warmup_value
    ):
        _fail("warm-up boundary overflows u64")
    return begin_value + offset_value + warmup_value


def _validate_provenance(
    value: Any, profile: profile_codec.FrozenTimingProfile,
) -> Mapping[str, Any]:
    row = _exact(value, PROVENANCE_KEYS, "timing provenance")
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_provenance"
        or row["checkpoint"] != "CP2-E"
    ):
        _fail("timing provenance identity differs")
    for key in PROVENANCE_KEYS:
        if key.endswith("sha256"):
            _sha(row[key], "provenance " + key)
    for key in ("source_commit", "source_tree"):
        if type(row[key]) is not str or HEX40_RE.fullmatch(row[key]) is None:
            _fail("provenance " + key + " is not a lowercase Git object ID")
    if row["profile_sha256"] != profile.sha256:
        _fail("timing provenance profile identity differs")
    if row["bag_sha256"] != profile.value["input"]["bag_sha256"]:
        _fail("timing provenance bag identity differs from the frozen profile")
    if row["machine_identity_sha256"] != profile.value["target"]["machine_identity_sha256"]:
        _fail("timing provenance machine identity differs from the frozen profile")
    return row


def _bounded_text(value: Any, label: str, maximum: int = 4096) -> str:
    if (
        type(value) is not str or not value
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\0\r\n")
    ):
        _fail(label + " is not bounded nonempty text")
    return value


def _absolute_text(value: Any, label: str) -> str:
    text = _bounded_text(value, label)
    if not os.path.isabs(text) or os.path.normpath(text) != text or text == os.path.sep:
        _fail(label + " is not normalized absolute non-root syntax")
    return text


def _validate_execution_binding(
    payload: bytes, profile: profile_codec.FrozenTimingProfile,
    provenance: Mapping[str, Any],
) -> Tuple[Mapping[str, Any], str]:
    row = _exact(
        _strict_json(payload, "execution binding"), EXECUTION_BINDING_KEYS,
        "execution binding",
    )
    if (
        row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_execution_binding"
        or row["checkpoint"] != "CP2-E"
        or row["profile_sha256"] != profile.sha256
    ):
        _fail("execution-binding root identity differs")
    for key in (
        "profile_sha256", "unit_manifest_sha256", "sequence_set_manifest_sha256",
        "executable_sha256", "dso_closure_sha256", "configuration_sha256",
        "boot_id_sha256", "machine_identity_sha256",
    ):
        _sha(row[key], "execution binding " + key)
    for key in ("source_commit", "source_tree"):
        if type(row[key]) is not str or HEX40_RE.fullmatch(row[key]) is None:
            _fail("execution binding " + key + " is not a lowercase Git object ID")
    executable = _absolute_text(row["executable_path"], "binding executable")
    if executable in ("/bin/sh", "/bin/bash", "/usr/bin/env"):
        _fail("execution binding invokes a forbidden launcher")
    _absolute_text(row["cwd"], "binding cwd")
    if row["environment"] != profile.value["runtime"]["environment"]:
        _fail("execution-binding environment differs from the frozen profile")
    if list(row["environment"]) != sorted(row["environment"]):
        _fail("execution-binding environment is not canonically ordered")
    templates = _exact(row["phase_templates"], COMMAND_PHASES, "phase templates")
    common_required = frozenset(("{profile_sha256}", "{run_index}"))
    for phase in COMMAND_PHASES:
        argv = templates[phase]
        if type(argv) is not list or not argv:
            _fail("execution-binding phase template is empty")
        retained = tuple(_bounded_text(item, "phase-template argument") for item in argv)
        _absolute_text(retained[0], "phase-template executable")
        if retained[0] in ("/bin/sh", "/bin/bash", "/usr/bin/env"):
            _fail("phase template invokes a forbidden launcher")
        tokens = frozenset(item for item in retained if "{" in item or "}" in item)
        if not tokens.issubset(TEMPLATE_TOKENS):
            _fail("phase template contains an unknown or embedded token")
        required = common_required
        if phase == "ros_run":
            required = required.union((
                "{timing_pair_index}", "{mode}", "{run_id}",
                "{trace_directory}", "{sequence_index}", "{sequence_id}",
            ))
            if retained[0] != executable:
                _fail("ROS executable differs from the retained execution binding")
        if not required.issubset(tokens):
            _fail("phase template omits a required exact token")
    joins = {
        "source_commit": "source_commit",
        "source_tree": "source_tree",
        "unit_manifest_sha256": "unit_manifest_sha256",
        "sequence_set_manifest_sha256": "sequence_set_manifest_sha256",
        "executable_sha256": "executable_sha256",
        "dso_closure_sha256": "dso_closure_sha256",
        "configuration_sha256": "configuration_sha256",
        "boot_id_sha256": "boot_id_sha256",
        "machine_identity_sha256": "machine_identity_sha256",
    }
    if any(row[binding_key] != provenance[provenance_key] for binding_key, provenance_key in joins.items()):
        _fail("execution binding differs from timing provenance")
    digest = _sha_bytes(payload)
    if provenance["launch_sha256"] != digest:
        _fail("provenance launch identity differs from retained execution-binding bytes")
    return row, digest


def _validate_control_prior_state(
    value: Any, profile: profile_codec.FrozenTimingProfile, label: str,
) -> Mapping[str, Any]:
    if type(value) is dict and value.get("schema_version") == 2:
        return _validate_v2_control_state(value, profile, label, applied=False)
    state = _exact(value, CONTROL_PRIOR_KEYS, label)
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != 1
        or state["record_type"] != "cp2_timing_control_prior_state"
    ):
        _fail(label + " identity differs")
    values = state["values"]
    writes = profile.value["controls"]["writes"]
    if type(values) is not list or len(values) != len(writes):
        _fail(label + " write population differs from the frozen profile")
    for observed, write in zip(values, writes):
        row = _exact(observed, STABLE_CONTROL_VALUE_KEYS, label + " write")
        if row["path"] != write["path"] or row["parser"] != write["parser"]:
            _fail(label + " write path/parser differs")
        _parsed_control_text(row["text"], row["parser"], label + " write text")
    module_population = state["modules_preexisting"]
    allowed_modules = sorted(
        action["name"] for action in profile.value["controls"]["module_actions"]
    )
    if (
        type(module_population) is not list
        or any(
            type(name) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name) is None
            for name in module_population
        )
        or module_population != sorted(set(module_population))
        or not set(module_population).issubset(allowed_modules)
    ):
        _fail(label + " module population is invalid")
    service_population = state["services_active"]
    allowed_services = sorted(
        action["name"] for action in profile.value["controls"]["service_actions"]
    )
    if (
        type(service_population) is not list
        or any(
            type(name) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", name) is None
            for name in service_population
        )
        or service_population != sorted(set(service_population))
        or not set(service_population).issubset(allowed_services)
    ):
        _fail(label + " service population is invalid")
    affinity = state["process_affinity"]
    if (
        type(affinity) is not list or not affinity
        or any(type(cpu) is not int or cpu < 0 or cpu > 4095 for cpu in affinity)
        or affinity != sorted(set(affinity))
    ):
        _fail(label + " prior affinity is invalid")
    return state


def _v2_control_plan(
    profile: profile_codec.FrozenTimingProfile,
) -> Tuple[Mapping[str, Any], ...]:
    value = profile.value
    retained: List[Mapping[str, Any]] = []
    boost = value["cpu_controls"]["boost"]
    retained.append({
        "control_id": "boost", "desired_text": boost["desired_text"],
        "parser": boost["parser"], "path": boost["path"],
        "read_only_text": None, "writable": True,
    })
    for policy in value["cpu_controls"]["policies"]:
        prefix = "cpufreq." + policy["policy_id"] + "."
        retained.extend((
            {
                "control_id": prefix + "driver", "desired_text": None,
                "parser": "text", "path": policy["driver_path"],
                "read_only_text": policy["driver"], "writable": False,
            },
            {
                "control_id": prefix + "governor",
                "desired_text": policy["desired_governor"], "parser": "text",
                "path": policy["governor_path"], "read_only_text": None,
                "writable": True,
            },
            {
                "control_id": prefix + "minimum_frequency_khz",
                "desired_text": str(policy["desired_minimum_frequency_khz"]),
                "parser": "integer", "path": policy["minimum_frequency_path"],
                "read_only_text": None, "writable": True,
            },
            {
                "control_id": prefix + "maximum_frequency_khz",
                "desired_text": str(policy["desired_maximum_frequency_khz"]),
                "parser": "integer", "path": policy["maximum_frequency_path"],
                "read_only_text": None, "writable": True,
            },
        ))
    for cpu in value["cpu_controls"]["cpus"]:
        if cpu["online_path"] is not None:
            retained.append({
                "control_id": "cpu.{}.online".format(cpu["cpu_id"]),
                "desired_text": "1" if cpu["expected_online"] else "0",
                "parser": "integer", "path": cpu["online_path"],
                "read_only_text": None, "writable": True,
            })
        for idle in cpu["idle_states"]:
            prefix = "cpu.{}.idle.{}.".format(
                cpu["cpu_id"], idle["state_index"],
            )
            retained.extend((
                {
                    "control_id": prefix + "name", "desired_text": None,
                    "parser": "text", "path": idle["name_path"],
                    "read_only_text": idle["name"], "writable": False,
                },
                {
                    "control_id": prefix + "disabled",
                    "desired_text": "1" if idle["desired_disabled"] else "0",
                    "parser": "integer", "path": idle["disable_path"],
                    "read_only_text": None, "writable": True,
                },
            ))
    interrupts = value["interrupts"]
    retained.append({
        "control_id": "irq.default_affinity",
        "desired_text": _cpu_mask_text(
            interrupts["default_affinity_cpu_ids"],
            value["cpu_plan"]["controlled_cpu_ids"],
        ),
        "parser": "cpu_mask", "path": interrupts["default_affinity_path"],
        "read_only_text": None, "writable": True,
    })
    for irq in interrupts["irq_records"]:
        retained.append({
            "control_id": "irq.{}.affinity".format(irq["irq"]),
            "desired_text": ",".join(str(cpu) for cpu in irq["desired_cpu_ids"]),
            "parser": "cpu_list", "path": irq["affinity_path"],
            "read_only_text": None, "writable": True,
        })
    return tuple(retained)


def _v2_cpu_population(
    value: Any, allowed: Sequence[int], label: str, *, empty: bool = False,
) -> Tuple[int, ...]:
    if (
        type(value) is not list or any(type(cpu) is not int for cpu in value)
        or value != sorted(set(value)) or (not value and not empty)
        or not set(value).issubset(set(allowed))
    ):
        _fail(label + " CPU population differs")
    return tuple(value)


def _validate_v2_control_state(
    value: Any, profile: profile_codec.FrozenTimingProfile, label: str,
    *, applied: bool,
) -> Mapping[str, Any]:
    expected_keys = CONTROL_V2_APPLIED_KEYS if applied else CONTROL_V2_PRIOR_KEYS
    state = _exact(value, expected_keys, label)
    expected_type = (
        "cp2_timing_control_applied_state"
        if applied else "cp2_timing_control_prior_state"
    )
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] != 2
        or state["record_type"] != expected_type
    ):
        _fail(label + " schema-v2 identity differs")
    plan = _v2_control_plan(profile)
    rows = state["values"]
    if type(rows) is not list or len(rows) != len(plan):
        _fail(label + " typed control population differs")
    for raw, expected in zip(rows, plan):
        row = _exact(raw, CONTROL_V2_VALUE_KEYS, label + " typed control")
        for key in ("control_id", "desired_text", "parser", "path", "writable"):
            if row[key] != expected[key] or type(row["writable"]) is not bool:
                _fail(label + " typed control profile binding differs")
        observed = _parsed_control_text(
            row["text"], row["parser"], label + " typed control text",
        )
        required = (
            expected["desired_text"] if expected["writable"]
            else expected["read_only_text"]
        )
        if (applied or not expected["writable"]) and observed != _parsed_control_text(
            required, row["parser"], label + " required typed control text",
        ):
            _fail(label + " typed control value differs")

    allowed_cpus = profile.value["cpu_plan"]["controlled_cpu_ids"]
    process_affinity = _v2_cpu_population(
        state["process_affinity"], allowed_cpus, label + " process affinity",
    )
    module_key = "modules_loaded" if applied else "modules_preexisting"
    allowed_modules = sorted(
        item["name"] for item in profile.value["controls"]["module_actions"]
    )
    modules = state[module_key]
    if (
        type(modules) is not list or modules != sorted(set(modules))
        or not set(modules).issubset(allowed_modules)
        or (applied and modules != allowed_modules)
    ):
        _fail(label + " module population differs")
    allowed_services = sorted(
        item["name"] for item in profile.value["controls"]["service_actions"]
    )
    services = state["services_active"]
    if (
        type(services) is not list or services != sorted(set(services))
        or not set(services).issubset(allowed_services)
        or (applied and services)
    ):
        _fail(label + " service population differs")
    if applied and (
        process_affinity != tuple(profile.value["runtime"]["cpu_ids"])
        or state["dma_latency_us"] != profile.value["runtime"]["dma_latency_us"]
    ):
        _fail(label + " applied affinity/DMA value differs")

    extension = _exact(
        state["privileged_v2"], CONTROL_V2_EXTENSION_KEYS,
        label + " privileged-v2 extension",
    )
    expected_extension_type = (
        "cp2e_privileged_applied_extension"
        if applied else "cp2e_privileged_prior_extension"
    )
    if (
        extension["record_type"] != expected_extension_type
        or extension["schema_version"] != 2
        or extension["profile_sha256"] != profile.sha256
        or extension["dma_latency_held_by_helper"] is not applied
    ):
        _fail(label + " privileged-v2 profile/state binding differs")
    _v2_cpu_population(
        extension["helper_affinity_cpu_ids"], allowed_cpus,
        label + " helper affinity",
    )
    if applied and extension["helper_affinity_cpu_ids"] != profile.value[
        "cpu_plan"
    ]["helper_cpu_ids"]:
        _fail(label + " applied helper affinity differs")
    expected_irqs = [
        item["irq"] for item in profile.value["interrupts"]["irq_records"]
    ]
    if (
        extension["irq_numbers"] != expected_irqs
        or extension["irq_population_sha256"]
        != profile.value["interrupts"]["inventory_sha256"]
    ):
        _fail(label + " IRQ population/profile binding differs")
    peer = _helper_peer(extension["peer"], label + " peer")
    peer_start = _u64(extension["peer_start_time_ticks"], label + " peer start")
    process_ids = extension["descendant_process_ids"]
    if (
        type(process_ids) is not list or not process_ids
        or process_ids != sorted(set(process_ids))
        or any(type(pid) is not int or pid <= 0 for pid in process_ids)
        or peer["pid"] not in process_ids
        or (not applied and process_ids != [peer["pid"]])
    ):
        _fail(label + " descendant process population differs")
    threads = extension["descendant_threads"]
    if type(threads) is not list or not threads:
        _fail(label + " descendant thread population is empty")
    tids = []
    main: Any = None
    for raw in threads:
        thread = _exact(raw, CONTROL_V2_THREAD_KEYS, label + " descendant thread")
        tid = _u64(thread["tid"], label + " descendant TID")
        process_id = _u64(
            thread["process_id"], label + " descendant process ID",
        )
        if tid == 0:
            _fail(label + " descendant TID is zero")
        _u64(thread["start_time_ticks"], label + " thread start")
        affinity = _v2_cpu_population(
            thread["affinity_cpu_ids"], allowed_cpus, label + " thread affinity",
        )
        membership = thread["cpuset_membership"]
        if (
            process_id not in process_ids
            or type(membership) is not str or not membership.startswith("/")
            or os.path.normpath(membership) != membership
            or len(membership.encode("utf-8")) > 4096
        ):
            _fail(label + " thread cpuset membership syntax differs")
        expected_membership = "/" + os.path.relpath(
            profile.value["cpuset"]["group_path"],
            profile.value["cpuset"]["mount_path"],
        )
        if applied and (
            affinity != tuple(profile.value["runtime"]["cpu_ids"])
            or membership != expected_membership
        ):
            _fail(label + " applied descendant thread placement differs")
        tids.append(tid)
        if tid == peer["pid"]:
            main = thread
    if (
        tids != sorted(set(tids)) or main is None
        or {item["process_id"] for item in threads} != set(process_ids)
        or any(
            sum(
                item["process_id"] == pid and item["tid"] == pid
                for item in threads
            ) != 1
            for pid in process_ids
        )
    ):
        _fail(label + " descendant thread identity population differs")
    if (
        main["start_time_ticks"] != peer_start
        or main["affinity_cpu_ids"] != state["process_affinity"]
    ):
        _fail(label + " main peer-thread identity differs")
    control_processes = extension["control_plane_processes"]
    if type(control_processes) is not list or not control_processes:
        _fail(label + " control-plane process chain is empty")
    control_pids = []
    parent_pid = peer["pid"]
    for raw in control_processes:
        process = _exact(
            raw, CONTROL_V2_PROCESS_KEYS, label + " control process",
        )
        for key in CONTROL_V2_PROCESS_KEYS:
            _u64(process[key], label + " control process " + key)
        if (
            process["pid"] == 0 or process["parent_pid"] != parent_pid
            or process["process_uid"] != 0 or process["process_gid"] != 0
        ):
            _fail(label + " control-plane process chain differs")
        parent_pid = process["pid"]
        control_pids.append(parent_pid)
    if control_pids != list(dict.fromkeys(control_pids)):
        _fail(label + " control-plane process IDs are duplicate")
    control_threads = extension["control_plane_threads"]
    if type(control_threads) is not list or not control_threads:
        _fail(label + " control-plane thread population is empty")
    control_tids = []
    for raw in control_threads:
        thread = _exact(
            raw, CONTROL_V2_THREAD_KEYS, label + " control thread",
        )
        tid = _u64(thread["tid"], label + " control TID")
        process_id = _u64(
            thread["process_id"], label + " control process ID",
        )
        _u64(thread["start_time_ticks"], label + " control thread start")
        affinity = _v2_cpu_population(
            thread["affinity_cpu_ids"], allowed_cpus,
            label + " control thread affinity",
        )
        membership = thread["cpuset_membership"]
        if (
            process_id not in control_pids or type(membership) is not str
            or not membership.startswith("/")
            or os.path.normpath(membership) != membership
            or (applied and (
                affinity != tuple(profile.value["cpu_plan"]["helper_cpu_ids"])
                or membership == expected_membership
            ))
        ):
            _fail(label + " control thread placement/identity differs")
        control_tids.append(tid)
    if (
        control_tids != sorted(set(control_tids))
        or set(control_tids).intersection(tids)
        or {item["process_id"] for item in control_threads}
        != set(control_pids)
        or any(
            sum(
                item["process_id"] == pid and item["tid"] == pid
                for item in control_threads
            ) != 1
            for pid in control_pids
        )
    ):
        _fail(label + " control-plane process/thread join differs")
    cpuset = _exact(
        extension["cpuset"], CONTROL_V2_CPUSET_KEYS, label + " cpuset state",
    )
    if cpuset["group_path"] != profile.value["cpuset"]["group_path"]:
        _fail(label + " cpuset path differs")
    if not applied:
        if cpuset != {
            "cpus": None, "effective_cpus": None, "effective_mems": None,
            "exists": False, "group_path": profile.value["cpuset"]["group_path"],
            "members": [], "mems": None,
        }:
            _fail(label + " prior cpuset absence proof differs")
    else:
        desired_cpus = ",".join(
            str(cpu) for cpu in profile.value["cpuset"]["desired_cpu_ids"]
        )
        desired_mems = ",".join(
            str(node) for node in profile.value["cpuset"]["desired_memory_nodes"]
        )
        if (
            cpuset["exists"] is not True or cpuset["cpus"] != desired_cpus
            or cpuset["effective_cpus"] != desired_cpus
            or cpuset["mems"] != desired_mems
            or cpuset["effective_mems"] != desired_mems
            or cpuset["members"] != tids
        ):
            _fail(label + " applied cpuset state differs")
    return state


def _validate_control_applied_state(
    value: Any, profile: profile_codec.FrozenTimingProfile, label: str,
) -> Mapping[str, Any]:
    if type(value) is dict and value.get("schema_version") == 2:
        return _validate_v2_control_state(value, profile, label, applied=True)
    return _validate_stable_control_state(value, profile, label)


def _validate_control_receipt(
    payload: bytes, expected_phase: str,
    profile: profile_codec.FrozenTimingProfile,
) -> Tuple[Mapping[str, Any], bytes]:
    row = _exact(
        _strict_json(payload, expected_phase + " control receipt"),
        CONTROL_RECEIPT_KEYS, expected_phase + " control receipt",
    )
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_control_state_receipt"
        or row["profile_sha256"] != profile.sha256
        or row["phase"] != expected_phase
        or row["passed"] is not True
    ):
        _fail(expected_phase + " control-receipt identity differs")
    _u64(row["captured_monotonic_ns"], expected_phase + " control receipt instant")
    state = (
        _validate_control_applied_state(
            row["state"], profile, "applied control state",
        )
        if expected_phase == "applied"
        else _validate_control_prior_state(
            row["state"], profile, expected_phase + " control state"
        )
    )
    if (
        profile.value["input"]["input_kind"] == "recorded_frozen"
        and row["state"].get("schema_version") != 2
    ):
        _fail("formal recorded control evidence is not complete schema v2")
    state_bytes = _canonical_json(state)
    if row["state_sha256"] != _sha_bytes(state_bytes):
        _fail(expected_phase + " control state digest differs")
    _sha(row["state_sha256"], expected_phase + " control state SHA-256")
    return row, state_bytes


def _validate_control_evidence(
    prior_payload: bytes, applied_payload: bytes, restored_payload: bytes,
    profile: profile_codec.FrozenTimingProfile, provenance: Mapping[str, Any],
) -> Tuple[
    Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], Mapping[str, Any],
]:
    prior, prior_state = _validate_control_receipt(prior_payload, "prior", profile)
    applied, _applied_state_bytes = _validate_control_receipt(
        applied_payload, "applied", profile
    )
    restored, restored_state = _validate_control_receipt(
        restored_payload, "restored", profile
    )
    if prior_state != restored_state:
        _fail("restored control state differs byte-for-byte from the captured prior state")
    if not (
        prior["captured_monotonic_ns"]
        < applied["captured_monotonic_ns"]
        < restored["captured_monotonic_ns"]
    ):
        _fail("control-state receipt chronology is not strictly increasing")
    for phase, payload in (
        ("prior", prior_payload), ("applied", applied_payload),
        ("restored", restored_payload),
    ):
        if provenance["control_{}_sha256".format(phase)] != _sha_bytes(payload):
            _fail(phase + " control receipt differs from provenance")
    return applied["state"], prior, applied, restored


def _decode_hex_bytes(value: Any, label: str, maximum_bytes: int) -> bytes:
    if (
        type(value) is not str
        or len(value) % 2 != 0
        or len(value) // 2 > maximum_bytes
        or re.fullmatch(r"[0-9a-f]*", value) is None
    ):
        _fail(label + " is not bounded lowercase hexadecimal bytes")
    return bytes.fromhex(value)


def _parse_cpu_list(text: str, label: str) -> List[int]:
    result: List[int] = []
    for token in text.split(","):
        pieces = token.split("-")
        if len(pieces) == 1 and pieces[0].isdigit():
            result.append(int(pieces[0]))
        elif len(pieces) == 2 and all(piece.isdigit() for piece in pieces):
            low, high = map(int, pieces)
            if low > high or high > 4095:
                _fail(label + " contains an invalid CPU range")
            result.extend(range(low, high + 1))
        else:
            _fail(label + " is not a CPU list")
    if not result or result != sorted(set(result)):
        _fail(label + " is empty, duplicate, or unordered")
    return result


def _parse_cpu_mask(text: str, label: str) -> List[int]:
    chunks = text.split(",")
    if (
        not chunks or len(chunks) > 128
        or any(re.fullmatch(r"[0-9a-f]{8}", chunk) is None for chunk in chunks)
    ):
        _fail(label + " is not canonical lowercase 32-bit CPU-mask chunks")
    result = []
    for word_index, chunk in enumerate(reversed(chunks)):
        word = int(chunk, 16)
        result.extend(
            word_index * 32 + bit for bit in range(32) if word & (1 << bit)
        )
    if not result or result[-1] > 4095:
        _fail(label + " CPU mask is empty or exceeds the CPU bound")
    return result


def _cpu_mask_text(
    values: Sequence[int], controlled_cpu_ids: Sequence[int],
) -> str:
    cpus = tuple(values)
    controlled = tuple(controlled_cpu_ids)
    if (
        not cpus or cpus != tuple(sorted(set(cpus)))
        or not controlled or controlled != tuple(range(len(controlled)))
        or not set(cpus).issubset(set(controlled))
    ):
        _fail("CPU-mask population is not a bounded canonical subset")
    words = [0] * ((len(controlled) + 31) // 32)
    for cpu in cpus:
        words[cpu // 32] |= 1 << (cpu % 32)
    encoded = ",".join(format(word, "08x") for word in reversed(words))
    if tuple(_parse_cpu_mask(encoded, "encoded CPU mask")) != cpus:
        _fail("CPU-mask encoding did not round-trip")
    return encoded


def _parsed_observation_value(payload: bytes, parser: str, label: str) -> Any:
    try:
        text = payload.decode("ascii", "strict").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise TimingArtifactError(label + " output is not ASCII") from exc
    if not text or "\0" in text or "\r" in text or "\n" in text:
        _fail(label + " output is not one canonical line")
    if parser == "text":
        return text
    if parser in ("integer", "hwmon_temperature_millicelsius"):
        if not text.isdigit():
            _fail(label + " output is not an unsigned decimal integer")
        return int(text)
    if parser == "cpu_list":
        return _parse_cpu_list(text, label)
    if parser == "sha256":
        return _sha(text, label)
    _fail(label + " parser is unsupported")


def _parsed_control_text(value: Any, parser: str, label: str) -> Any:
    if (
        type(value) is not str or not value
        or any(character in value for character in "\0\r\n")
    ):
        _fail(label + " is not one nonempty control text line")
    if parser == "text":
        return value
    if parser == "integer":
        if not value.isdigit():
            _fail(label + " is not an unsigned decimal integer")
        return int(value)
    if parser == "cpu_list":
        return _parse_cpu_list(value, label)
    if parser == "cpu_mask":
        return _parse_cpu_mask(value, label)
    _fail(label + " parser is unsupported")


def _parse_raw_telemetry_snapshot(
    payload: bytes, expected_phase: str, label: str,
) -> Tuple[Mapping[str, Any], Mapping[Tuple[str, Optional[int]], Mapping[str, Any]]]:
    snapshot = _exact(
        _strict_json(payload, label), RAW_TELEMETRY_SNAPSHOT_KEYS, label,
    )
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != 1
        or snapshot["record_type"] != "cp2_timing_raw_telemetry_snapshot"
        or snapshot["phase"] != expected_phase
    ):
        _fail(label + " identity or phase differs")
    receipts = snapshot["receipts"]
    if type(receipts) is not list or not receipts:
        _fail(label + " receipt population is empty")
    metric_ids: List[str] = []
    by_role_cpu: Dict[Tuple[str, Optional[int]], Mapping[str, Any]] = {}
    for value in receipts:
        receipt = _exact(value, RAW_TELEMETRY_RECEIPT_KEYS, label + " receipt")
        if (
            type(receipt["schema_version"]) is not int
            or receipt["schema_version"] != 1
            or receipt["record_type"] != "cp2_timing_raw_telemetry_receipt"
            or receipt["phase"] != expected_phase
        ):
            _fail(label + " receipt identity or phase differs")
        specification = _exact(
            receipt["specification"], RAW_TELEMETRY_SPEC_KEYS,
            label + " specification",
        )
        metric_id = specification["metric_id"]
        if type(metric_id) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", metric_id) is None:
            _fail(label + " metric ID is invalid")
        metric_ids.append(metric_id)
        role = specification["role"]
        if role not in (
            "aperf", "mperf", "thermal_throttle_count",
            "scaling_current_frequency_khz", "temperature_millicelsius",
        ):
            _fail(label + " telemetry role is unsupported")
        cpu_id = specification["cpu_id"]
        if cpu_id is not None and (
            type(cpu_id) is not int or cpu_id < 0 or cpu_id > 4095
        ):
            _fail(label + " telemetry CPU ID is invalid")
        path = _absolute_text(specification["path"], label + " telemetry path")
        source_kind = specification["source_kind"]
        register = specification["register"]
        if source_kind == "msr_u64_le":
            expected_register = 0xE8 if role == "aperf" else 0xE7 if role == "mperf" else None
            if (
                cpu_id is None or path != "/dev/cpu/{}/msr".format(cpu_id)
                or register != expected_register
            ):
                _fail(label + " MSR path/register/role binding differs")
        elif source_kind == "text_integer":
            if register is not None or not path.startswith((
                "/sys/devices/system/cpu/", "/sys/class/hwmon/",
                "/sys/class/thermal/", "/proc/",
            )):
                _fail(label + " text telemetry source is outside the approved surfaces")
            if (
                cpu_id is not None and role != "temperature_millicelsius"
                and "/cpu{}/".format(cpu_id) not in path
                and not path.startswith("/proc/")
            ):
                _fail(label + " per-CPU telemetry path differs from its CPU")
        else:
            _fail(label + " telemetry source kind is unsupported")
        started = _u64(receipt["started_monotonic_ns"], label + " receipt start")
        ended = _u64(receipt["ended_monotonic_ns"], label + " receipt end")
        if ended < started:
            _fail(label + " telemetry receipt interval is reversed")
        raw = _decode_hex_bytes(receipt["raw_hex"], label + " raw bytes", 128)
        if not raw or receipt["raw_sha256"] != _sha_bytes(raw):
            _fail(label + " raw telemetry digest differs")
        _sha(receipt["raw_sha256"], label + " raw telemetry SHA-256")
        if source_kind == "msr_u64_le":
            if len(raw) != 8:
                _fail(label + " MSR payload is not exactly eight bytes")
            parsed = int.from_bytes(raw, "little")
        else:
            try:
                text = raw.decode("ascii", "strict")
            except UnicodeDecodeError as exc:
                raise TimingArtifactError(label + " raw text is not ASCII") from exc
            if text.endswith("\n"):
                text = text[:-1]
            if not text or any(character in text for character in "\0\r\n"):
                _fail(label + " raw text is not one canonical line")
            signed = role == "temperature_millicelsius"
            digits = text[1:] if signed and text.startswith("-") else text
            if not digits.isdigit() or (len(digits) > 1 and digits.startswith("0")):
                _fail(label + " raw text is not canonical decimal")
            parsed = int(text)
            if signed:
                if parsed < -273_150 or parsed > 1_000_000:
                    _fail(label + " temperature is outside its physical integer domain")
            else:
                _u64(parsed, label + " parsed telemetry")
        if type(receipt["parsed_value"]) is not int or receipt["parsed_value"] != parsed:
            _fail(label + " parsed telemetry differs from retained raw bytes")
        key = (role, cpu_id)
        if key in by_role_cpu:
            _fail(label + " duplicates one role/CPU")
        by_role_cpu[key] = {
            "metric_id": metric_id, "value": parsed, "started": started,
            "ended": ended, "specification": specification,
        }
    if metric_ids != sorted(set(metric_ids)):
        _fail(label + " metric IDs are not sorted and unique")
    return snapshot, by_role_cpu


def _telemetry_temperature_range(
    profile: profile_codec.FrozenTimingProfile,
) -> Tuple[int, int]:
    values = [
        item["expected"] for item in profile.value["observations"]
        if item["parser"] == "hwmon_temperature_millicelsius"
    ]
    if len(values) != 1:
        _fail("frozen profile does not bind exactly one temperature range")
    return values[0]["minimum"], values[0]["maximum"]


def _validate_telemetry_comparison(
    pre_payload: bytes, post_payload: bytes, comparison_payload: bytes,
    profile: profile_codec.FrozenTimingProfile, label: str,
) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    pre, pre_values = _parse_raw_telemetry_snapshot(pre_payload, "pre", label + " pre")
    post, post_values = _parse_raw_telemetry_snapshot(post_payload, "post", label + " post")
    if tuple(pre_values) != tuple(post_values):
        _fail(label + " pre/post source population differs")
    for key in pre_values:
        if pre_values[key]["specification"] != post_values[key]["specification"]:
            _fail(label + " pre/post source identity differs")
    if max(item["ended"] for item in pre_values.values()) >= min(
        item["started"] for item in post_values.values()
    ):
        _fail(label + " pre/post telemetry capture intervals overlap or touch")
    retained = _exact(
        _strict_json(comparison_payload, label + " comparison"),
        RAW_TELEMETRY_COMPARISON_KEYS, label + " comparison",
    )
    if (
        type(retained["schema_version"]) is not int
        or retained["schema_version"] != 1
        or retained["record_type"] != "cp2_timing_raw_telemetry_comparison"
        or retained["passed"] is not True
        or retained["pre_snapshot_sha256"] != _sha_bytes(pre_payload)
        or retained["post_snapshot_sha256"] != _sha_bytes(post_payload)
    ):
        _fail(label + " comparison identity or snapshot binding differs")
    cpus = profile.value["runtime"]["cpu_ids"]
    frequencies = profile.value["runtime"]["accepted_current_frequency_khz"]
    temperature_range = _telemetry_temperature_range(profile)
    if (
        type(retained["expected_cpu_ids"]) is not list
        or any(type(cpu) is not int for cpu in retained["expected_cpu_ids"])
        or type(retained["accepted_frequency_khz"]) is not list
        or any(
            type(frequency) is not int
            for frequency in retained["accepted_frequency_khz"]
        )
        or type(retained["temperature_range_millicelsius"]) is not list
        or any(
            type(value) is not int
            for value in retained["temperature_range_millicelsius"]
        )
    ):
        _fail(label + " comparison populations contain noninteger values")
    if (
        retained["expected_cpu_ids"] != cpus
        or retained["accepted_frequency_khz"] != frequencies
        or retained["temperature_range_millicelsius"] != list(temperature_range)
    ):
        _fail(label + " comparison differs from the frozen CPU/frequency/thermal profile")
    bounds_value = retained["aperf_mperf_ratio_bounds"]
    if type(bounds_value) is not list or len(bounds_value) != 2:
        _fail(label + " APERF/MPERF bounds are malformed")
    bounds: List[Tuple[int, int]] = []
    for value in bounds_value:
        if (
            type(value) is not list or len(value) != 2
            or type(value[0]) is not int or type(value[1]) is not int
            or value[0] <= 0 or value[1] <= 0
            or math.gcd(value[0], value[1]) != 1
        ):
            _fail(label + " APERF/MPERF bound is not a reduced positive rational")
        bounds.append((value[0], value[1]))
    low, high = bounds
    if low[0] * high[1] >= high[0] * low[1]:
        _fail(label + " APERF/MPERF interval is empty")
    expected_per_cpu = []
    for cpu in cpus:
        values: Dict[str, int] = {"cpu_id": cpu}
        for role in (
            "thermal_throttle_count", "aperf", "mperf",
            "scaling_current_frequency_khz",
        ):
            key = (role, cpu)
            if key not in pre_values:
                _fail(label + " omits {} for CPU {}".format(role, cpu))
            before = pre_values[key]["value"]
            after = post_values[key]["value"]
            if role == "thermal_throttle_count" and before != after:
                _fail(label + " thermal throttle count changed")
            if role in ("aperf", "mperf") and after <= before:
                _fail(label + " " + role.upper() + " did not advance without wrap")
            if role == "scaling_current_frequency_khz" and (
                before not in frequencies or after not in frequencies
            ):
                _fail(label + " current frequency differs from the frozen set")
            values[role + "_pre"] = before
            values[role + "_post"] = after
            if role in ("aperf", "mperf"):
                values[role + "_delta"] = after - before
        aperf_delta = values["aperf_delta"]
        mperf_delta = values["mperf_delta"]
        if (
            aperf_delta * low[1] < low[0] * mperf_delta
            or aperf_delta * high[1] > high[0] * mperf_delta
        ):
            _fail(label + " exact APERF/MPERF ratio is outside retained bounds")
        divisor = math.gcd(aperf_delta, mperf_delta)
        values["aperf_mperf_ratio"] = [
            aperf_delta // divisor, mperf_delta // divisor,
        ]
        expected_per_cpu.append(values)
    temperatures = []
    for (role, cpu_id), before_item in pre_values.items():
        if role == "temperature_millicelsius":
            before = before_item["value"]
            after = post_values[(role, cpu_id)]["value"]
            if not (
                temperature_range[0] <= before <= temperature_range[1]
                and temperature_range[0] <= after <= temperature_range[1]
            ):
                _fail(label + " temperature is outside the frozen range")
            temperatures.append({"cpu_id": cpu_id, "pre": before, "post": after})
        elif cpu_id not in cpus:
            _fail(label + " contains an unexpected per-CPU metric")
    if not temperatures:
        _fail(label + " omits bound temperature evidence")
    expected = {
        "schema_version": 1,
        "record_type": "cp2_timing_raw_telemetry_comparison",
        "pre_snapshot_sha256": _sha_bytes(pre_payload),
        "post_snapshot_sha256": _sha_bytes(post_payload),
        "expected_cpu_ids": cpus,
        "accepted_frequency_khz": frequencies,
        "temperature_range_millicelsius": list(temperature_range),
        "aperf_mperf_ratio_bounds": [list(item) for item in bounds],
        "per_cpu": expected_per_cpu,
        "temperatures": temperatures,
        "passed": True,
    }
    if _canonical_json(retained) != _canonical_json(expected):
        _fail(label + " comparison differs from independent raw recomputation")
    return pre, post


def _fresh_process_identity_from_record(
    row: Mapping[str, Any], profile_sha256: str, run_index: int,
) -> str:
    return _sha_bytes(_canonical_json({
        "domain": "SchurVIO-CP2-E-fresh-process-v1",
        "profile_sha256": profile_sha256,
        "run_index": run_index,
        "pid": row["pid"],
        "start_time_ticks": row["start_time_ticks"],
        "executable_sha256": row["executable_sha256"],
        "loader_before_sha256": row["loader_before_sha256"],
        "loader_after_sha256": row["loader_after_sha256"],
    }))


def _validate_process_identity(
    payload: bytes, run: TimingRun, profile: profile_codec.FrozenTimingProfile,
    binding: Mapping[str, Any], binding_sha256: str,
    run_support: Mapping[str, bytes], label: str,
) -> Mapping[str, Any]:
    row = _exact(_strict_json(payload, label), PROCESS_IDENTITY_KEYS, label)
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_process_identity_observation"
        or row["checkpoint"] != "CP2-E"
        or row["profile_sha256"] != profile.sha256
        or row["execution_binding_sha256"] != binding_sha256
        or row["run_index"] != run.run_index
    ):
        _fail(label + " root identity differs")
    if _u64(row["run_index"], label + " run index") != run.run_index:
        _fail(label + " run index is not an exact unsigned integer")
    for key in ("observed_monotonic_ns", "pid", "start_time_ticks"):
        if _u64(row[key], label + " " + key) == 0:
            _fail(label + " " + key + " is zero")
    for key in ("executable_sha256", "loader_before_sha256", "loader_after_sha256"):
        _sha(row[key], label + " " + key)
    if (
        row["executable_sha256"] != binding["executable_sha256"]
        or row["loader_before_sha256"] != _sha_bytes(run_support["runs/run_{}/loader_before.txt".format(run.run_index)])
        or row["loader_after_sha256"] != _sha_bytes(run_support["runs/run_{}/loader_after.txt".format(run.run_index)])
    ):
        _fail(label + " executable/loader identity differs from raw retained support")
    if _fresh_process_identity_from_record(row, profile.sha256, run.run_index) != run.fresh_process_identity:
        _fail(label + " fresh-process digest differs from independent recomputation")
    return row


def _validate_runtime_preflight(
    payload: bytes, run_index: int, profile: profile_codec.FrozenTimingProfile,
    binding_sha256: str, label: str,
) -> Mapping[str, Any]:
    row = _exact(_strict_json(payload, label), RUNTIME_PREFLIGHT_KEYS, label)
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_runtime_preflight_receipt"
        or row["checkpoint"] != "CP2-E"
        or row["profile_sha256"] != profile.sha256
        or row["execution_binding_sha256"] != binding_sha256
        or row["run_index"] != run_index
        or row["passed"] is not True
    ):
        _fail(label + " root identity differs")
    if _u64(row["run_index"], label + " run index") != run_index:
        _fail(label + " run index is not an exact unsigned integer")
    started = _u64(row["started_monotonic_ns"], label + " start")
    ended = _u64(row["ended_monotonic_ns"], label + " end")
    if ended < started:
        _fail(label + " interval is reversed")
    stdout = _decode_hex_bytes(
        row["stdout_hex"], label + " stdout", profile.value["limits"]["maximum_command_output_bytes"]
    )
    stderr = _decode_hex_bytes(
        row["stderr_hex"], label + " stderr", profile.value["limits"]["maximum_command_output_bytes"]
    )
    if row["stdout_sha256"] != _sha_bytes(stdout) or row["stderr_sha256"] != _sha_bytes(stderr):
        _fail(label + " raw output digests differ")
    if (
        type(row["exit_code"]) is not int or isinstance(row["exit_code"], bool)
        or row["exit_code"] != 0 or row["timed_out"] is not False
        or row["process_group_complete"] is not True
    ):
        _fail(label + " command completion differs")
    return row


def _validate_stable_control_state(
    value: Any, profile: profile_codec.FrozenTimingProfile, label: str,
) -> Mapping[str, Any]:
    if type(value) is dict and value.get("schema_version") == 2:
        return _validate_v2_control_state(value, profile, label, applied=True)
    state = _exact(value, STABLE_CONTROL_KEYS, label)
    retained_values = state["values"]
    expected_writes = profile.value["controls"]["writes"]
    if type(retained_values) is not list or len(retained_values) != len(expected_writes):
        _fail(label + " value population differs from the frozen writes")
    for retained, expected in zip(retained_values, expected_writes):
        item = _exact(retained, STABLE_CONTROL_VALUE_KEYS, label + " value")
        if item["path"] != expected["path"] or item["parser"] != expected["parser"]:
            _fail(label + " value path/parser differs from the frozen write")
        observed = _parsed_control_text(item["text"], item["parser"], label + " value text")
        desired = _parsed_control_text(
            expected["desired_text"], expected["parser"], label + " desired text"
        )
        if observed != desired:
            _fail(label + " value differs from the frozen applied control")
    expected_modules = sorted(
        action["name"] for action in profile.value["controls"]["module_actions"]
    )
    if state["modules_loaded"] != expected_modules:
        _fail(label + " loaded-module population differs from the frozen profile")
    if state["services_active"] != []:
        _fail(label + " retains an active service while the profile requires inactive")
    affinity = state["process_affinity"]
    if (
        type(affinity) is not list
        or any(type(cpu) is not int for cpu in affinity)
        or affinity != profile.value["runtime"]["cpu_ids"]
    ):
        _fail(label + " process affinity differs from the frozen CPU set")
    if (
        _u64(state["dma_latency_us"], label + " DMA latency")
        != profile.value["runtime"]["dma_latency_us"]
    ):
        _fail(label + " DMA-latency value differs from the frozen profile")
    return state


def _validate_clock_snapshot(
    payload: bytes, profile: profile_codec.FrozenTimingProfile,
    provenance: Mapping[str, Any], applied_control_state: Mapping[str, Any],
    run_index: int, phase: str, label: str,
) -> Mapping[str, Any]:
    row = _exact(_strict_json(payload, label), CLOCK_KEYS, label)
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_clock_snapshot"
        or row["checkpoint"] != "CP2-E"
        or row["profile_sha256"] != profile.sha256
        or row["run_index"] != run_index
        or row["phase"] != phase
        or row["passed"] is not True
    ):
        _fail(label + " identity, phase, or passing state differs")
    if _u64(row["run_index"], label + " run index") != run_index:
        _fail(label + " run index is not an exact unsigned integer")
    _u64(row["captured_monotonic_ns"], label + " capture time")
    if row["boot_id_sha256"] != provenance["boot_id_sha256"]:
        _fail(label + " boot identity differs from provenance")
    if row["control_applied_sha256"] != provenance["control_applied_sha256"]:
        _fail(label + " applied-control identity differs from provenance")
    stable_state = _validate_stable_control_state(
        row["stable_control_state"], profile, label + " stable-control state"
    )
    if _canonical_json(stable_state) != _canonical_json(applied_control_state):
        _fail(label + " stable control state differs from the retained applied receipt")
    if row["stable_control_state_sha256"] != _sha_bytes(_canonical_json(stable_state)):
        _fail(label + " stable-control state digest differs")
    _sha(row["stable_control_state_sha256"], label + " stable-control state SHA-256")
    observations = row["observations"]
    expected_observations = profile.value["observations"]
    if type(observations) is not list or len(observations) != len(expected_observations):
        _fail(label + " observation population differs from the frozen profile")
    parsed_by_id: Dict[str, Any] = {}
    for index, (value, expected) in enumerate(zip(observations, expected_observations)):
        item = _exact(value, CLOCK_OBSERVATION_KEYS, label + " observation")
        if (
            item["observation_id"] != expected["observation_id"]
            or item["argv"] != expected["argv"]
            or item["environment"] != expected["environment"]
            or item["cwd"] != expected["cwd"]
        ):
            _fail(label + " observation command/profile join differs")
        started = _u64(item["started_monotonic_ns"], label + " observation start")
        ended = _u64(item["ended_monotonic_ns"], label + " observation end")
        if ended < started or ended - started > expected["timeout_ns"]:
            _fail(label + " observation interval is invalid")
        if (
            type(item["exit_code"]) is not int
            or isinstance(item["exit_code"], bool)
            or item["exit_code"] != 0
            or item["timed_out"] is not False
        ):
            _fail(label + " observation command failed")
        stdout = _decode_hex_bytes(
            item["stdout_hex"], label + " observation stdout",
            expected["maximum_stdout_bytes"],
        )
        stderr = _decode_hex_bytes(
            item["stderr_hex"], label + " observation stderr",
            profile.value["limits"]["maximum_command_output_bytes"],
        )
        if (
            item["stdout_sha256"] != _sha_bytes(stdout)
            or item["stderr_sha256"] != _sha_bytes(stderr)
        ):
            _fail(label + " observation output digest differs")
        _sha(item["stdout_sha256"], label + " observation stdout SHA-256")
        _sha(item["stderr_sha256"], label + " observation stderr SHA-256")
        parsed = _parsed_observation_value(
            stdout, expected["parser"], label + " observation parsed value"
        )
        if item["parsed_value"] != parsed:
            _fail(label + " observation retained parsed value differs")
        expected_value = expected["expected"]
        if expected["parser"] == "hwmon_temperature_millicelsius":
            if not (expected_value["minimum"] <= parsed <= expected_value["maximum"]):
                _fail(label + " observed temperature is outside the frozen range")
        elif parsed != expected_value:
            _fail(label + " observed value differs from the frozen expectation")
        parsed_by_id[item["observation_id"]] = parsed
    telemetry = _exact(row["telemetry"], TELEMETRY_KEYS, label + " telemetry")
    _u64(
        telemetry["temperature_millicelsius"],
        label + " telemetry temperature_millicelsius",
    )
    frequency = profile.value["runtime"]["minimum_frequency_khz"]
    counters = telemetry["cpu_counters"]
    expected_cpus = profile.value["runtime"]["cpu_ids"]
    if type(counters) is not list or len(counters) != len(expected_cpus):
        _fail(label + " telemetry CPU-counter population differs from the frozen CPU set")
    for expected_cpu, value in zip(expected_cpus, counters):
        counter = _exact(value, CPU_COUNTER_KEYS, label + " CPU counter")
        if counter["cpu_id"] != expected_cpu:
            _fail(label + " telemetry CPU-counter order/identity differs")
        for key in CPU_COUNTER_KEYS:
            _u64(counter[key], label + " CPU counter " + key)
        if (
            counter["reference_frequency_khz"] != frequency
            or counter["scaling_current_frequency_khz"] != frequency
        ):
            _fail(label + " telemetry CPU frequency differs from the frozen clock")
    thermal_observations = [
        expected["observation_id"] for expected in expected_observations
        if expected["parser"] == "hwmon_temperature_millicelsius"
    ]
    if len(thermal_observations) != 1:
        _fail(label + " profile must contain exactly one thermal-range observation")
    if telemetry["temperature_sensor_id"] != thermal_observations[0]:
        _fail(label + " telemetry temperature sensor identity differs")
    if telemetry["temperature_millicelsius"] != parsed_by_id[thermal_observations[0]]:
        _fail(label + " telemetry temperature differs from its raw observation")
    frequency_observations = [
        expected["observation_id"] for expected in expected_observations
        if expected["parser"] == "integer" and expected["expected"] == frequency
    ]
    if len(frequency_observations) != 1:
        _fail(label + " profile must contain exactly one current-frequency observation")
    if any(
        counter["scaling_current_frequency_khz"]
        != parsed_by_id[frequency_observations[0]]
        for counter in counters
    ):
        _fail(label + " telemetry CPU frequency differs from its raw observation")
    return row


def _cross_validate_clock_raw_telemetry(
    clock: Mapping[str, Any], raw_payload: bytes, phase: str,
    profile: profile_codec.FrozenTimingProfile, label: str,
) -> None:
    _snapshot, values = _parse_raw_telemetry_snapshot(raw_payload, phase, label)
    counters = clock["telemetry"]["cpu_counters"]
    expected_cpus = profile.value["runtime"]["cpu_ids"]
    for cpu, counter in zip(expected_cpus, counters):
        joins = {
            "thermal_throttle_count": "thermal_throttle_count",
            "aperf": "aperf", "mperf": "mperf",
            "scaling_current_frequency_khz": "scaling_current_frequency_khz",
        }
        for role, key in joins.items():
            if (role, cpu) not in values or counter[key] != values[(role, cpu)]["value"]:
                _fail(label + " clock summary differs from raw {} for CPU {}".format(role, cpu))
    temperatures = [
        item["value"] for (role, _cpu), item in values.items()
        if role == "temperature_millicelsius"
    ]
    if len(temperatures) != 1 or clock["telemetry"]["temperature_millicelsius"] != temperatures[0]:
        _fail(label + " clock temperature differs from the unique raw temperature receipt")


def _validate_affinity_audit(
    payload: bytes, profile: profile_codec.FrozenTimingProfile, label: str,
) -> Mapping[str, Any]:
    row = _exact(_strict_json(payload, label), AFFINITY_KEYS, label)
    if (
        type(row["schema_version"]) is not int
        or row["schema_version"] != 1
        or row["record_type"] != "cp2_timing_affinity_audit"
        or row["passed"] is not True
    ):
        _fail(label + " identity or pass bit differs")
    _u64(row["root_pid"], label + " root PID")
    if row["root_pid"] == 0:
        _fail(label + " root PID is zero")
    _u64(row["poll_interval_ns"], label + " poll interval")
    if row["poll_interval_ns"] == 0:
        _fail(label + " poll interval is zero")
    expected_cpus = profile.value["runtime"]["cpu_ids"]
    if (
        type(row["expected_cpus"]) is not list
        or any(type(cpu) is not int for cpu in row["expected_cpus"])
        or row["expected_cpus"] != expected_cpus
    ):
        _fail(label + " CPU set differs from the frozen profile")
    observations = row["observations"]
    if type(observations) is not list or not observations:
        _fail(label + " observations are absent")
    if row["observation_count"] != len(observations):
        _fail(label + " observation count differs")
    previous = None
    for index, value in enumerate(observations):
        item = _exact(value, AFFINITY_OBSERVATION_KEYS, label + " observation")
        instant = _u64(item["monotonic_ns"], label + " observation time")
        if previous is not None and instant < previous:
            _fail(label + " observation times are reverse ordered")
        previous = instant
        if _u64(item["tid"], label + " thread ID") == 0:
            _fail(label + " thread ID is zero")
        if (
            type(item["cpu_ids"]) is not list
            or any(type(cpu) is not int for cpu in item["cpu_ids"])
            or item["cpu_ids"] != expected_cpus
        ):
            _fail(label + " observation escaped the frozen CPU set")
    return row


def _validate_runtime_parameters(
    payload: bytes, mode: str, expected_sha256: str, label: str,
) -> None:
    try:
        parameters = cp2_schema.strict_json_loads(payload)
        canonical = json.dumps(
            parameters, allow_nan=False, ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8") + b"\n"
        digest = cp2_schema.resolved_parameters_sha256(parameters)
    except (cp2_schema.SchemaError, UnicodeError, ValueError, TypeError) as exc:
        raise TimingArtifactError(label + " is not a valid resolved-parameter map") from exc
    if payload != canonical:
        _fail(label + " is not canonical JSON")
    mode_key = "/cp2_vio/up_msckf_landmark_elimination"
    if parameters.get(mode_key) != mode:
        _fail(label + " landmark-elimination mode differs")
    if digest != expected_sha256:
        _fail(label + " digest differs from the runtime context")


def _absolute_child(value: Any, parent: PurePosixPath, label: str) -> PurePosixPath:
    if type(value) is not str or not value.startswith("/") or "\0" in value:
        _fail(label + " is not an absolute path")
    path = PurePosixPath(value)
    if path.as_posix() != value or path == PurePosixPath("/"):
        _fail(label + " is not a normalized non-root absolute path")
    try:
        path.relative_to(parent)
    except ValueError as exc:
        raise TimingArtifactError(label + " is outside trace_directory") from exc
    if path == parent:
        _fail(label + " aliases trace_directory")
    return path


def _validate_context(
    payload: bytes, run: TimingRun, profile: profile_codec.FrozenTimingProfile,
    provenance: Mapping[str, Any], runtime_payload: bytes,
) -> None:
    label = "run {} context".format(run.run_index)
    row = _exact(_strict_json(payload, label), CONTEXT_KEYS, label)
    expected_sequence = profile.value["input"]
    if (
        row["schema_version"] != 1
        or row["record_type"] != "cp2_runtime_context"
        or row["checkpoint"] != "CP2-E"
        or row["run_id"] != run.run_id
        or row["sequence_index"] != expected_sequence["sequence_index"]
        or row["sequence_id"] != expected_sequence["sequence_id"]
        or row["mode"] != run.mode
        or row["shadow_enabled"] is not False
        or row["trace_level"] != "timing"
        or row["source_commit"] != provenance["source_commit"]
        or row["config_sha256"] != provenance["configuration_sha256"]
        or row["bag_sha256"] != provenance["bag_sha256"]
        or row["pair_index_sha256"] is not None
    ):
        _fail(label + " identity/configuration join differs")
    for key in ("config_sha256", "bag_sha256", "resolved_parameters_sha256"):
        _sha(row[key], label + " " + key)
    if HEX40_RE.fullmatch(row["source_commit"]) is None:
        _fail(label + " source commit is invalid")
    if type(row["trace_directory"]) is not str:
        _fail(label + " trace directory is invalid")
    trace_directory = PurePosixPath(row["trace_directory"])
    if (
        not trace_directory.is_absolute()
        or trace_directory == PurePosixPath("/")
        or trace_directory.as_posix() != row["trace_directory"]
    ):
        _fail(label + " trace directory is not normalized absolute")
    required = {
        "serial_trace_path": "serial.jsonl",
        "callback_trace_path": "callbacks.jsonl",
        "updater_trace_path": "updater.jsonl",
        "timing_trace_path": "timing.jsonl",
        "runtime_parameters_path": "runtime_parameters.json",
        "loader_map_before_path": "loader_before.txt",
        "loader_map_after_path": "loader_after.txt",
    }
    forbidden = {
        "trajectory_trace_path", "state_payload_path", "proposal_payload_path",
        "raw_system_payload_path", "legacy_state_path", "legacy_deviation_path",
        "legacy_timing_path",
    }
    children = []
    for key, basename in required.items():
        child = _absolute_child(row[key], trace_directory, label + " " + key)
        if child.name != basename:
            _fail(label + " " + key + " basename differs")
        children.append(child)
    if any(row[key] is not None for key in forbidden):
        _fail(label + " timing trace has a forbidden nonnull path")
    if len(set(children)) != len(children):
        _fail(label + " output paths alias")
    _validate_runtime_parameters(
        runtime_payload, run.mode, row["resolved_parameters_sha256"],
        "run {} runtime parameters".format(run.run_index),
    )


def _serial_callback_transition(row: Mapping[str, Any], label: str) -> None:
    for key in (
        "enqueue_entered", "enqueue_returned", "processing_entered",
        "processing_returned", "state_row_emitted", "updater_invoked",
    ):
        _boolean(row[key], label + " " + key)
    enqueue = row["enqueue_status"]
    processing = row["processing_status"]
    if enqueue not in {
        "queued", "frequency_dropped", "cam0_decode_failed", "cam1_decode_failed",
        "process_terminated", "trace_failure",
    }:
        _fail(label + " enqueue status is unsupported")
    if processing not in {
        "processed", "not_queued", "queued_unprocessed", "process_terminated",
        "trace_failure",
    }:
        _fail(label + " processing status is unsupported")
    flags = (
        row["enqueue_entered"], row["enqueue_returned"],
        row["processing_entered"], row["processing_returned"],
    )
    if enqueue == "queued" and processing == "processed":
        valid = flags == (True, True, True, True)
    elif enqueue == "queued" and processing == "queued_unprocessed":
        valid = flags == (True, True, False, False)
    elif enqueue == "queued" and processing in ("process_terminated", "trace_failure"):
        valid = flags == (True, True, True, False)
    else:
        valid = processing == "not_queued" and flags == (True, True, False, False)
    if not valid:
        _fail(label + " enqueue/processing transition is invalid")
    ids = row["updater_invocation_ids"]
    if type(ids) is not list or len(ids) > 1:
        _fail(label + " updater invocation list is invalid")
    for invocation_id in ids:
        _u64(invocation_id, label + " updater invocation ID")
    if row["updater_invoked"] is not bool(ids):
        _fail(label + " updater_invoked differs from its exact ID population")
    if processing != "processed" and ids:
        _fail(label + " unprocessed callback owns an updater invocation")
    trajectory = row["trajectory_index"]
    if row["state_row_emitted"]:
        _u64(trajectory, label + " trajectory index")
    elif trajectory is not None:
        _fail(label + " has a trajectory index without a state row")


def _derive_run_samples(
    run: TimingRun, support: Mapping[str, bytes],
    profile: profile_codec.FrozenTimingProfile,
    provenance: Mapping[str, Any], applied_control_state: Mapping[str, Any],
    binding: Mapping[str, Any], binding_sha256: str,
    bag_begin_record_time_ns: int,
) -> Tuple[TimingSample, ...]:
    prefix = "runs/run_{}/".format(run.run_index)
    limits = profile.value["limits"]
    if set(support) != set(_required_run_support(run.run_index)):
        _fail("run support-file population differs from the frozen inventory")
    total = 0
    for relative, payload in support.items():
        if type(payload) is not bytes:
            _fail("run support-file payload is not bytes")
        total += len(payload)
        if total > limits["maximum_trace_bytes"]:
            _fail("run support bytes exceed the frozen trace resource limit")
    for name in ("stdout.bin", "stderr.bin"):
        if len(support[prefix + name]) > limits["maximum_command_output_bytes"]:
            _fail("run command output exceeds the frozen resource limit")
    pre_clock = _validate_clock_snapshot(
        support[prefix + "clock_pre.json"], profile, provenance,
        applied_control_state, run.run_index,
        "pre", "run {} pre clock snapshot".format(run.run_index),
    )
    post_clock = _validate_clock_snapshot(
        support[prefix + "clock_post.json"], profile, provenance,
        applied_control_state, run.run_index,
        "post", "run {} post clock snapshot".format(run.run_index),
    )
    _validate_telemetry_comparison(
        support[prefix + "telemetry_pre.json"],
        support[prefix + "telemetry_post.json"],
        support[prefix + "telemetry_comparison.json"], profile,
        "run {} raw telemetry".format(run.run_index),
    )
    _cross_validate_clock_raw_telemetry(
        pre_clock, support[prefix + "telemetry_pre.json"], "pre", profile,
        "run {} pre raw telemetry".format(run.run_index),
    )
    _cross_validate_clock_raw_telemetry(
        post_clock, support[prefix + "telemetry_post.json"], "post", profile,
        "run {} post raw telemetry".format(run.run_index),
    )
    if post_clock["captured_monotonic_ns"] <= pre_clock["captured_monotonic_ns"]:
        _fail("run post clock snapshot does not follow the pre snapshot")
    for key in (
        "boot_id_sha256", "control_applied_sha256",
        "stable_control_state_sha256",
    ):
        if post_clock[key] != pre_clock[key]:
            _fail("run stable clock/control identity drifted across snapshots")
    pre_telemetry = pre_clock["telemetry"]
    post_telemetry = post_clock["telemetry"]
    if post_telemetry["temperature_sensor_id"] != pre_telemetry["temperature_sensor_id"]:
        _fail("run temperature sensor identity drifted")
    for pre_counter, post_counter in zip(
        pre_telemetry["cpu_counters"], post_telemetry["cpu_counters"]
    ):
        if post_counter["cpu_id"] != pre_counter["cpu_id"]:
            _fail("run telemetry CPU identity drifted")
        if post_counter["thermal_throttle_count"] != pre_counter["thermal_throttle_count"]:
            _fail("run thermal throttle counter changed")
        if (
            post_counter["aperf"] <= pre_counter["aperf"]
            or post_counter["mperf"] <= pre_counter["mperf"]
        ):
            _fail("run APERF/MPERF counters did not both advance")
    if (
        _sha_bytes(support[prefix + "clock_pre.json"]) != run.pre_snapshot_sha256
        or _sha_bytes(support[prefix + "clock_post.json"]) != run.post_snapshot_sha256
        or _sha_bytes(support[prefix + "process_identity.json"]) != run.process_identity_sha256
        or _sha_bytes(support[prefix + "runtime_preflight.json"]) != run.runtime_preflight_sha256
        or _sha_bytes(support[prefix + "telemetry_pre.json"]) != run.telemetry_pre_sha256
        or _sha_bytes(support[prefix + "telemetry_post.json"]) != run.telemetry_post_sha256
        or _sha_bytes(support[prefix + "telemetry_comparison.json"]) != run.telemetry_comparison_sha256
    ):
        _fail("run evidence digest differs from retained files")
    process_identity = _validate_process_identity(
        support[prefix + "process_identity.json"], run, profile, binding,
        binding_sha256, support,
        "run {} process identity".format(run.run_index),
    )
    _validate_runtime_preflight(
        support[prefix + "runtime_preflight.json"], run.run_index, profile,
        binding_sha256, "run {} runtime preflight".format(run.run_index),
    )
    affinity = _validate_affinity_audit(
        support[prefix + "affinity_audit.json"], profile,
        "run {} affinity audit".format(run.run_index),
    )
    if affinity["root_pid"] != process_identity["pid"]:
        _fail("run affinity root PID differs from raw process identity")
    if (
        not support[prefix + "loader_before.txt"]
        or support[prefix + "loader_before.txt"] != support[prefix + "loader_after.txt"]
    ):
        _fail("run loader maps are empty or drifted")
    _validate_context(
        support[prefix + "context.json"], run, profile, provenance,
        support[prefix + "runtime_parameters.json"],
    )
    line_limit = limits["maximum_jsonl_line_bytes"]
    serial_rows = _strict_jsonl(
        support[prefix + "serial.jsonl"], "run serial trace",
        maximum_line_bytes=line_limit,
    )
    callback_rows = _strict_jsonl(
        support[prefix + "callbacks.jsonl"], "run callback trace",
        maximum_line_bytes=line_limit,
    )
    updater_rows = _strict_jsonl(
        support[prefix + "updater.jsonl"], "run updater trace",
        maximum_line_bytes=line_limit,
    )
    timing_rows = _strict_jsonl(
        support[prefix + "timing.jsonl"], "run raw timing trace",
        maximum_line_bytes=line_limit,
    )
    if len(serial_rows) != len(callback_rows):
        _fail("serial/callback population counts differ")
    if len(updater_rows) != len(timing_rows):
        _fail("updater/timing population omits or adds an invocation")
    if len(updater_rows) > limits["maximum_samples_per_run"]:
        _fail("updater population exceeds the frozen sample limit")

    sequence_index = profile.value["input"]["sequence_index"]
    sequence_id = profile.value["input"]["sequence_id"]
    serial_by_pair: Dict[int, Mapping[str, Any]] = {}
    callback_ids: List[int] = []
    used_filtered_indices = set()
    previous_anchor: Optional[int] = None
    next_trajectory = 0
    launch_record_boundary = _checked_boundary(
        bag_begin_record_time_ns, profile.value["input"]["frozen_offset_ns"], 0,
    )
    shared_serial_fields = (
        "anchor_filtered_index", "cam0_filtered_index", "cam0_header_time_ns",
        "cam0_record_time_ns", "cam1_filtered_index", "cam1_header_time_ns",
        "cam1_record_time_ns", "pair_index", "sequence_id", "sequence_index",
    )
    for index, (serial_value, callback_value) in enumerate(zip(serial_rows, callback_rows)):
        serial = _exact(serial_value, SERIAL_KEYS, "serial row")
        callback = _exact(callback_value, CALLBACK_KEYS, "callback row")
        if (
            serial["schema_version"] != 1
            or serial["record_type"] != "pair_index"
            or serial["pair_index"] != index
            or serial["sequence_index"] != sequence_index
            or serial["sequence_id"] != sequence_id
        ):
            _fail("serial row identity differs")
        for key in SERIAL_KEYS:
            if key not in ("record_type", "sequence_id"):
                _u64(serial[key], "serial " + key)
        if serial["anchor_camera_id"] not in (0, 1):
            _fail("serial anchor camera ID is outside stereo")
        expected_anchor = serial["cam0_filtered_index"] if serial["anchor_camera_id"] == 0 else serial["cam1_filtered_index"]
        if serial["anchor_filtered_index"] != expected_anchor:
            _fail("serial anchor identity differs")
        if previous_anchor is not None and serial["anchor_filtered_index"] <= previous_anchor:
            _fail("serial anchors are not strictly increasing")
        previous_anchor = serial["anchor_filtered_index"]
        if serial["cam0_filtered_index"] == serial["cam1_filtered_index"]:
            _fail("serial pair reuses one filtered camera index")
        for filtered in (serial["cam0_filtered_index"], serial["cam1_filtered_index"]):
            if filtered in used_filtered_indices:
                _fail("serial population reuses a camera message")
            used_filtered_indices.add(filtered)
        delta = abs(serial["cam0_record_time_ns"] - serial["cam1_record_time_ns"])
        if serial["absolute_record_delta_ns"] != delta or delta >= 20_000_000:
            _fail("serial record-time delta violates the strict 20 ms rule")
        if serial["cam0_record_time_ns"] < launch_record_boundary:
            _fail("serial cam0 record time precedes the frozen launch offset")
        if (
            callback["schema_version"] != 1
            or callback["record_type"] != "serial_callback"
            or callback["callback_index"] != index
            or callback["mode"] != run.mode
            or callback["camera_timestamp_ns"] != serial["cam0_header_time_ns"]
            or any(callback[key] != serial[key] for key in shared_serial_fields)
        ):
            _fail("serial/callback identity join differs")
        _u64(callback["camera_timestamp_ns"], "callback camera timestamp")
        _serial_callback_transition(callback, "callback {}".format(index))
        if callback["state_row_emitted"]:
            if callback["trajectory_index"] != next_trajectory:
                _fail("callback trajectory indices are not exact and contiguous")
            next_trajectory += 1
        callback_ids.extend(callback["updater_invocation_ids"])
        serial_by_pair[index] = serial
    if callback_ids != list(range(len(callback_ids))):
        _fail("callback updater invocation IDs are not exact contiguous call-entry IDs")

    samples: List[TimingSample] = []
    for index, (updater_value, timing_value) in enumerate(zip(updater_rows, timing_rows)):
        updater = _exact(updater_value, UPDATER_KEYS, "updater row")
        raw_timing = _exact(timing_value, RAW_TIMING_KEYS, "raw timing row")
        if (
            updater["schema_version"] != 1
            or updater["record_type"] != "updater_event"
            or updater["sequence_index"] != sequence_index
            or updater["sequence_id"] != sequence_id
            or updater["mode"] != run.mode
            or updater["invocation_id"] != index
            or updater["invocation_id"] != callback_ids[index]
        ):
            _fail("updater row identity/order differs from callback ownership")
        pair_index = _u64(updater["pair_index"], "updater pair index")
        if pair_index not in serial_by_pair:
            _fail("updater row has no serial-pair owner")
        serial = serial_by_pair[pair_index]
        if updater["camera_timestamp_ns"] != serial["cam0_header_time_ns"]:
            _fail("updater camera timestamp differs from its serial owner")
        for key in (
            "camera_timestamp_ns", "duration_ns", "input_feature_count", "invocation_id",
            "pair_index", "raw_system_count", "timer_end_ns", "timer_start_ns",
        ):
            _u64(updater[key], "updater " + key)
        for key in (
            "baseline_preflight_attempted", "committed", "nonempty",
            "preflight_accepted", "primary",
        ):
            _boolean(updater[key], "updater " + key)
        if updater["timer_clock"] != TIMER_CLOCK:
            _fail("updater timer clock differs from the frozen clock")
        if updater["timer_end_ns"] < updater["timer_start_ns"]:
            _fail("updater timing endpoints are reverse ordered")
        if updater["duration_ns"] != updater["timer_end_ns"] - updater["timer_start_ns"]:
            _fail("updater duration differs from checked endpoint subtraction")
        if (
            updater["terminal_status"] not in TERMINAL_MAPPING
            or updater["terminal_subreason"] not in TERMINAL_MAPPING[updater["terminal_status"]]
        ):
            _fail("updater terminal status/subreason mapping is invalid")
        expected_nonempty = updater["input_feature_count"] != 0
        expected_committed = updater["terminal_status"] == "committed_counted"
        expected_primary = expected_nonempty and updater["preflight_accepted"] and expected_committed
        if (
            updater["nonempty"] is not expected_nonempty
            or updater["committed"] is not expected_committed
            or updater["primary"] is not expected_primary
        ):
            _fail("updater lifecycle flags differ from exact reconstruction")
        if updater["preflight_accepted"] and not updater["baseline_preflight_attempted"]:
            _fail("updater accepted a preflight that was not attempted")
        if updater["raw_system_count"] > updater["input_feature_count"]:
            _fail("updater raw-system count exceeds input-feature count")
        if (updater["terminal_status"] == "empty_input") is not (not expected_nonempty):
            _fail("updater empty-input terminal mapping differs")
        if expected_committed and (
            updater["raw_system_count"] == 0
            or not updater["baseline_preflight_attempted"]
            or not updater["preflight_accepted"]
        ):
            _fail("committed updater lacks its required accepted lifecycle")
        timing_join = {
            "camera_timestamp_ns": updater["camera_timestamp_ns"],
            "committed": updater["committed"], "duration_ns": updater["duration_ns"],
            "invocation_id": updater["invocation_id"], "mode": updater["mode"],
            "nonempty": updater["nonempty"],
            "preflight_accepted": updater["preflight_accepted"],
            "primary": updater["primary"], "schema_version": 1,
            "sequence_id": updater["sequence_id"],
            "sequence_index": updater["sequence_index"],
            "serial_pair_index": updater["pair_index"],
            "terminal_status": updater["terminal_status"],
            "timer_clock": updater["timer_clock"],
            "timer_end_ns": updater["timer_end_ns"],
            "timer_start_ns": updater["timer_start_ns"],
            "cam0_record_time_ns": serial["cam0_record_time_ns"],
            "record_type": "updater_timing",
        }
        if dict(raw_timing) != timing_join:
            _fail("raw timing row differs from its serial/updater reconstruction")
        samples.append(TimingSample(
            profile_sha256=profile.sha256,
            timing_pair_index=run.timing_pair_index, run_index=run.run_index,
            mode=run.mode, sequence_index=sequence_index, sequence_id=sequence_id,
            serial_pair_index=pair_index,
            cam0_record_time_ns=serial["cam0_record_time_ns"],
            camera_timestamp_ns=updater["camera_timestamp_ns"],
            invocation_id=updater["invocation_id"],
            terminal_status=updater["terminal_status"],
            terminal_subreason=updater["terminal_subreason"],
            input_feature_count=updater["input_feature_count"],
            raw_system_count=updater["raw_system_count"],
            baseline_preflight_attempted=updater["baseline_preflight_attempted"],
            nonempty=updater["nonempty"],
            preflight_accepted=updater["preflight_accepted"],
            committed=updater["committed"], primary=updater["primary"],
            timer_clock=updater["timer_clock"],
            timer_start_ns=updater["timer_start_ns"],
            timer_end_ns=updater["timer_end_ns"], duration_ns=updater["duration_ns"],
        ))
    if tuple(sample.invocation_id for sample in samples) != tuple(callback_ids):
        _fail("callback updater_invocation_ids differ from the complete updater population")
    if not samples:
        _fail("run updater population is empty")
    return tuple(samples)


def _derived_run(run: TimingRun, samples: Tuple[TimingSample, ...]) -> TimingRun:
    return TimingRun(
        run_index=run.run_index, profile_sha256=run.profile_sha256,
        run_id=run.run_id, fresh_process_identity=run.fresh_process_identity,
        pre_snapshot_sha256=run.pre_snapshot_sha256,
        post_snapshot_sha256=run.post_snapshot_sha256,
        trace_bundle_sha256=run.trace_bundle_sha256,
        process_identity_sha256=run.process_identity_sha256,
        runtime_preflight_sha256=run.runtime_preflight_sha256,
        telemetry_pre_sha256=run.telemetry_pre_sha256,
        telemetry_post_sha256=run.telemetry_post_sha256,
        telemetry_comparison_sha256=run.telemetry_comparison_sha256,
        samples=samples,
    )


def _validate_run_descriptors(runs: Any, profile_sha256: str) -> Tuple[TimingRun, ...]:
    if type(runs) is not tuple or len(runs) != 6:
        _fail("timing artifact requires exactly six run descriptors")
    retained = []
    identities = set()
    for index, run in enumerate(runs):
        if type(run) is not TimingRun or run.run_index != index:
            _fail("timing run descriptors are not contiguous 0..5")
        TimingRun(**run.__dict__)
        if run.profile_sha256 != profile_sha256:
            _fail("run profile identity differs")
        if run.samples:
            _fail("caller-supplied reduced timing samples are forbidden")
        if run.fresh_process_identity in identities:
            _fail("timing runs do not retain six fresh process identities")
        identities.add(run.fresh_process_identity)
        retained.append(run)
    return tuple(retained)


def _validate_run_population_identity(
    runs: Sequence[TimingRun], provenance: Mapping[str, Any],
) -> None:
    expected = _sha_bytes(_canonical_json({
        "domain": "SchurVIO-CP2-E-six-fresh-processes-v1",
        "profile_sha256": runs[0].profile_sha256,
        "fresh_process_identities": [run.fresh_process_identity for run in runs],
    }))
    if provenance["run_identity_sha256"] != expected:
        _fail("provenance run identity differs from the exact six-process population")


def _render_bound_argv(
    binding: Mapping[str, Any], phase: str, run: TimingRun,
    profile: profile_codec.FrozenTimingProfile, trace_directory: str,
) -> List[str]:
    values = {
        "{profile_sha256}": profile.sha256,
        "{run_index}": str(run.run_index),
        "{timing_pair_index}": str(run.timing_pair_index),
        "{mode}": run.mode,
        "{run_id}": run.run_id,
        "{trace_directory}": trace_directory,
        "{sequence_index}": str(profile.value["input"]["sequence_index"]),
        "{sequence_id}": profile.value["input"]["sequence_id"],
    }
    return [values.get(item, item) for item in binding["phase_templates"][phase]]


def _validate_commands(
    rows: Any, profile: profile_codec.FrozenTimingProfile,
    support_files: Mapping[str, bytes], binding: Mapping[str, Any],
    binding_sha256: str, runs: Sequence[TimingRun],
) -> Tuple[Mapping[str, Any], ...]:
    expected_count = 6 * len(COMMAND_PHASES)
    if type(rows) not in (tuple, list) or len(rows) != expected_count:
        _fail("timing commands differ from the exact 24-command schedule")
    retained = []
    previous_end: Optional[int] = None
    maximum_duration_ns = profile.value["limits"]["maximum_process_seconds"] * 1_000_000_000
    empty_sha = _sha_bytes(b"")
    for command_id, value in enumerate(rows):
        row = _exact(value, COMMAND_KEYS, "timing command")
        run_index = command_id // len(COMMAND_PHASES)
        phase = COMMAND_PHASES[command_id % len(COMMAND_PHASES)]
        if (
            type(row["schema_version"]) is not int
            or row["schema_version"] != 1
            or row["record_type"] != "timing_command"
            or row["command_id"] != command_id
            or row["run_index"] != run_index
            or row["timing_pair_index"] != run_index // 2
            or row["phase"] != phase
        ):
            _fail("timing command phase/order/run identity differs")
        if (
            _u64(row["command_id"], "timing command ID") != command_id
            or _u64(row["run_index"], "timing command run index") != run_index
            or _u64(
                row["timing_pair_index"], "timing command pair index",
            ) != run_index // 2
        ):
            _fail("timing command indices are not exact unsigned integers")
        run = runs[run_index]
        prefix = "runs/run_{}/".format(run_index)
        context = _exact(
            _strict_json(support_files[prefix + "context.json"], "command context"),
            CONTEXT_KEYS, "command context",
        )
        trace_directory = _absolute_text(
            context["trace_directory"], "command trace directory",
        )
        expected_argv = _render_bound_argv(
            binding, phase, run, profile, trace_directory,
        )
        argv = row["argv"]
        if type(argv) is not list or not argv or any(type(item) is not str or not item for item in argv):
            _fail("timing command argv is invalid")
        if (
            not os.path.isabs(argv[0])
            or os.path.normpath(argv[0]) != argv[0]
            or argv[0] in ("/bin/sh", "/bin/bash", "/usr/bin/env")
        ):
            _fail("timing command does not bind an absolute normalized nonshell executable")
        if argv != expected_argv:
            _fail("timing command argv differs from independent execution-binding rendering")
        if row["environment"] != binding["environment"]:
            _fail("timing command environment differs from the execution binding")
        if row["cwd"] != binding["cwd"]:
            _fail("timing command cwd differs from the execution binding")
        started = _u64(row["started_monotonic_ns"], "command start time")
        ended = _u64(row["ended_monotonic_ns"], "command end time")
        if ended < started or ended - started > maximum_duration_ns:
            _fail("timing command interval is negative or oversized")
        if previous_end is not None and started < previous_end:
            _fail("timing command intervals overlap or reverse order")
        previous_end = ended
        if type(row["exit_code"]) is not int or isinstance(row["exit_code"], bool) or row["exit_code"] != 0:
            _fail("timing command did not exit successfully")
        if row["timed_out"] is not False or row["process_group_complete"] is not True:
            _fail("timing command timeout/process-group evidence failed")
        stdout_sha = _sha(row["stdout_sha256"], "command stdout SHA-256")
        stderr_sha = _sha(row["stderr_sha256"], "command stderr SHA-256")
        if phase == "ros_run":
            expected_stdout = _sha_bytes(support_files[prefix + "stdout.bin"])
            expected_stderr = _sha_bytes(support_files[prefix + "stderr.bin"])
            identity = _exact(
                _strict_json(
                    support_files[prefix + "process_identity.json"],
                    "command process identity",
                ), PROCESS_IDENTITY_KEYS, "command process identity",
            )
            observed = _u64(
                identity["observed_monotonic_ns"],
                "command process-identity observation time",
            )
            if observed < started or observed > ended:
                _fail("process identity was not observed inside its ROS command interval")
        elif phase == "runtime_preflight":
            receipt = _validate_runtime_preflight(
                support_files[prefix + "runtime_preflight.json"], run_index,
                profile, binding_sha256, "command runtime preflight",
            )
            expected_stdout = receipt["stdout_sha256"]
            expected_stderr = receipt["stderr_sha256"]
            joined = (
                receipt["argv"] == argv
                and receipt["environment"] == row["environment"]
                and receipt["cwd"] == row["cwd"]
                and receipt["started_monotonic_ns"] == started
                and receipt["ended_monotonic_ns"] == ended
                and receipt["exit_code"] == row["exit_code"]
                and receipt["timed_out"] == row["timed_out"]
                and receipt["process_group_complete"] == row["process_group_complete"]
            )
            if not joined:
                _fail("runtime-preflight raw receipt differs from its command row")
        else:
            expected_stdout = empty_sha
            expected_stderr = empty_sha
            raw_phase = "pre" if phase == "clock_pre" else "post"
            raw_payload = support_files[
                prefix + ("telemetry_pre.json" if raw_phase == "pre" else "telemetry_post.json")
            ]
            _snapshot, raw_values = _parse_raw_telemetry_snapshot(
                raw_payload, raw_phase, "command raw telemetry",
            )
            if any(
                item["started"] < started or item["ended"] > ended
                for item in raw_values.values()
            ):
                _fail("raw telemetry receipt lies outside its clock command interval")
            clock = _exact(
                _strict_json(
                    support_files[
                        prefix + ("clock_pre.json" if raw_phase == "pre" else "clock_post.json")
                    ], "command clock snapshot",
                ), CLOCK_KEYS, "command clock snapshot",
            )
            captured = _u64(
                clock["captured_monotonic_ns"], "command clock capture time",
            )
            if captured < started or captured > ended:
                _fail("clock snapshot capture lies outside its command interval")
            for observation in clock["observations"]:
                observation_row = _exact(
                    observation, CLOCK_OBSERVATION_KEYS, "command clock observation",
                )
                if (
                    observation_row["started_monotonic_ns"] < started
                    or observation_row["ended_monotonic_ns"] > ended
                ):
                    _fail("clock observation lies outside its command interval")
        if stdout_sha != expected_stdout or stderr_sha != expected_stderr:
            _fail("timing command output digest differs from its phase support")
        retained.append(row)
    return tuple(retained)


def _eligible_by_timestamp(run: TimingRun, boundary: int) -> Mapping[int, TimingSample]:
    selected: Dict[int, TimingSample] = {}
    for sample in run.samples:
        if not sample.primary or sample.cam0_record_time_ns < boundary:
            continue
        if sample.camera_timestamp_ns in selected:
            _fail("eligible timing run contains duplicate camera timestamps")
        selected[sample.camera_timestamp_ns] = sample
    if not selected:
        _fail("timing run has no post-warmup primary committing population")
    return selected


def _pair_math(
    pair_index: int, left: TimingRun, right: TimingRun, boundary: int,
) -> timing_math.TimingPairResult:
    if (left.mode, right.mode) != PAIR_ORDER[pair_index]:
        _fail("timing pair modes differ from the frozen order")
    populations = {
        left.mode: _eligible_by_timestamp(left, boundary),
        right.mode: _eligible_by_timestamp(right, boundary),
    }
    common = tuple(sorted(
        set(populations["nullspace"]).intersection(populations["schur"])
    ))
    if not common:
        _fail("timing pair has an empty complete common population")
    baseline = tuple(populations["nullspace"][timestamp].duration_ns for timestamp in common)
    candidate = tuple(populations["schur"][timestamp].duration_ns for timestamp in common)
    return timing_math.timing_pair_result(pair_index, common, baseline, candidate)


def _rational_record(value: timing_math.RationalNanoseconds) -> Mapping[str, str]:
    return timing_math.rational_nanoseconds_to_record(value)


def _ratio_record(value: timing_math.ExactRatio) -> Mapping[str, str]:
    return timing_math.exact_ratio_to_record(value)


def _pair_record(result: timing_math.TimingPairResult) -> Mapping[str, Any]:
    record = {
        "timing_pair_index": result.timing_pair_index,
        "common_count": len(result.common_timestamps_ns),
        "common_payload_path": "common/pair_{}.bin".format(result.timing_pair_index),
        "common_payload_sha256": timing_math.u256_to_hex(
            result.common_payload_sha256, "common payload SHA-256"
        ),
        "baseline_p50": _rational_record(result.baseline_quantiles.p50.value_ns),
        "candidate_p50": _rational_record(result.candidate_quantiles.p50.value_ns),
        "p50_ratio": _ratio_record(result.p50_gate.candidate_over_baseline),
        "p50_left_cross_product_hex": timing_math.u128_to_hex(result.p50_gate.left_cross_product),
        "p50_right_cross_product_hex": timing_math.u128_to_hex(result.p50_gate.right_cross_product),
        "p50_passed": result.p50_gate.passed,
        "baseline_p95": _rational_record(result.baseline_quantiles.p95.value_ns),
        "candidate_p95": _rational_record(result.candidate_quantiles.p95.value_ns),
        "p95_ratio": _ratio_record(result.p95_gate.candidate_over_baseline),
        "p95_left_cross_product_hex": timing_math.u128_to_hex(result.p95_gate.left_cross_product),
        "p95_right_cross_product_hex": timing_math.u128_to_hex(result.p95_gate.right_cross_product),
        "p95_passed": result.p95_gate.passed,
        "passed": result.passed,
    }
    _exact(record, PAIR_KEYS, "pair result")
    return record


def _safe_output(root: Path, relative: str) -> Path:
    normalized = _safe_relpath(relative)
    path = root.joinpath(*normalized.split("/"))
    if path == root or root not in path.parents:
        _fail("artifact output escapes its root")
    return path


def _write_new(root: Path, relative: str, payload: bytes) -> None:
    path = _safe_output(root, relative)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            count = os.write(descriptor, payload[offset:])
            if count <= 0:
                _fail("artifact write made no progress")
            offset += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha_file(path: Path) -> str:
    """Hash one retained regular file without materializing it in memory."""

    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(str(path), flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            _fail("artifact contains a nonregular or multiply linked file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(descriptor)


def _inventory(root: Path) -> Mapping[str, str]:
    result: Dict[str, str] = {}
    for directory, names, filenames in os.walk(str(root), followlinks=False):
        names.sort()
        filenames.sort()
        if any(stat.S_ISLNK(os.lstat(os.path.join(directory, name)).st_mode) for name in names):
            _fail("artifact contains a symlink directory")
        for filename in filenames:
            path = Path(directory) / filename
            status = path.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                _fail("artifact contains a nonregular or multiply linked file")
            relative = path.relative_to(root).as_posix()
            if relative != "SHA256SUMS":
                result[relative] = _sha_file(path)
            if len(result) > MAX_ARTIFACT_FILES:
                _fail("artifact file count exceeds the resource bound")
    return result


def _manifest_bytes(inventory: Mapping[str, str]) -> bytes:
    return b"".join(
        (digest + "  " + path + "\n").encode("ascii")
        for path, digest in sorted(inventory.items())
    )


def _parse_manifest(payload: bytes) -> Mapping[str, str]:
    result: Dict[str, str] = {}
    if not payload or not payload.endswith(b"\n"):
        _fail("manifest is empty or lacks its terminal newline")
    for raw in payload.splitlines():
        try:
            line = raw.decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise TimingArtifactError("manifest is not ASCII") from exc
        if len(line) < 67 or line[64:66] != "  ":
            _fail("manifest line is malformed")
        digest, relative = line[:64], line[66:]
        _sha(digest, "manifest digest")
        relative = _safe_relpath(relative)
        if relative == "SHA256SUMS" or relative in result:
            _fail("manifest path is duplicate or self-referential")
        result[relative] = digest
    if payload != _manifest_bytes(result):
        _fail("manifest entries are not in canonical order")
    return result


def _prepare(
    source: TimingAssemblyInput,
) -> Tuple[
    profile_codec.FrozenTimingProfile, Mapping[str, Any], Tuple[Mapping[str, Any], ...],
    Tuple[TimingRun, ...], Tuple[timing_math.TimingPairResult, ...],
    timing_math.TimingCampaignResult, int, bytes,
]:
    if type(source.profile_bytes) is not bytes:
        _fail("timing profile payload is not bytes")
    try:
        profile = profile_codec.load_profile_bytes(source.profile_bytes)
    except profile_codec.TimingProfileError as exc:
        raise TimingArtifactError("timing profile is invalid: " + str(exc)) from exc
    if source.profile_sha256 != profile.sha256:
        _fail("timing profile external identity differs from retained bytes")
    profile_input = profile.value["input"]
    if (
        source.sequence_index != profile_input["sequence_index"]
        or source.sequence_id != profile_input["sequence_id"]
        or source.frozen_offset_ns != profile_input["frozen_offset_ns"]
    ):
        _fail("timing source sequence/offset differs from the frozen profile")
    _u64(source.sequence_index, "sequence index")
    _safe_id(source.sequence_id, "sequence ID")
    guardian_source = source.helper_guardian_evidence_bytes
    control_source = source.helper_control_evidence_bytes
    if profile_input["input_kind"] == "recorded_frozen":
        if not isinstance(guardian_source, HeldGuardianEvidence):
            _fail(
                "formal recorded assembly requires the helper-written held "
                "guardian evidence inode"
            )
        guardian_source.verify_held()
        if not isinstance(control_source, HeldControlEvidence):
            _fail(
                "formal recorded assembly requires the helper-written held "
                "control evidence inode"
            )
        control_source.verify_held()
    elif type(guardian_source) is not bytes and not isinstance(
        guardian_source, HeldGuardianEvidence
    ):
        _fail("synthetic guardian evidence source type differs")
    elif type(control_source) is not bytes and not isinstance(
        control_source, HeldControlEvidence
    ):
        _fail("synthetic control evidence source type differs")
    provenance = _validate_provenance(source.provenance, profile)
    binding, binding_sha256 = _validate_execution_binding(
        source.execution_binding_bytes, profile, provenance,
    )
    applied_control_state, prior_receipt, applied_receipt, restored_receipt = (
        _validate_control_evidence(
            source.control_prior_bytes, source.control_applied_bytes,
            source.control_restored_bytes, profile, provenance,
        )
    )
    descriptors = _validate_run_descriptors(source.runs, profile.sha256)
    _validate_run_population_identity(descriptors, provenance)
    required_support = {
        relative for index in range(6) for relative in _required_run_support(index)
    }
    if type(source.support_files) is not dict or set(source.support_files) != required_support:
        _fail("timing support-file population differs from the closed six-run inventory")
    derived_runs = []
    process_keys = set()
    for descriptor in descriptors:
        run_support = {
            relative: source.support_files[relative]
            for relative in _required_run_support(descriptor.run_index)
        }
        if trace_bundle_sha256(run_support, descriptor.run_index) != descriptor.trace_bundle_sha256:
            _fail("run trace-bundle SHA-256 differs from its support files")
        samples = _derive_run_samples(
            descriptor, run_support, profile, provenance, applied_control_state,
            binding, binding_sha256,
            source.bag_begin_record_time_ns,
        )
        identity = _strict_json(
            run_support[
                "runs/run_{}/process_identity.json".format(descriptor.run_index)
            ], "run process identity",
        )
        process_key = (identity["pid"], identity["start_time_ticks"])
        if process_key in process_keys:
            _fail("six timing slots do not retain six distinct process instances")
        process_keys.add(process_key)
        derived_runs.append(_derived_run(descriptor, samples))
    commands = _validate_commands(
        source.commands, profile, source.support_files, binding,
        binding_sha256, descriptors,
    )
    _validate_helper_evidence(
        source.helper_transcript_bytes, source.helper_terminal_receipt_bytes,
        guardian_source, control_source,
        profile, provenance, commands, source.support_files, prior_receipt,
        applied_receipt, restored_receipt,
    )
    if not (
        applied_receipt["captured_monotonic_ns"]
        < commands[0]["started_monotonic_ns"]
        and commands[-1]["ended_monotonic_ns"]
        < restored_receipt["captured_monotonic_ns"]
    ):
        _fail("command schedule is not strictly enclosed by applied/restored control receipts")
    if prior_receipt["captured_monotonic_ns"] >= commands[0]["started_monotonic_ns"]:
        _fail("prior control receipt does not precede the command schedule")
    boundary = _checked_boundary(
        source.bag_begin_record_time_ns, source.frozen_offset_ns,
        profile.value["pairing"]["warmup_ns"],
    )
    pair_results = tuple(
        _pair_math(index, derived_runs[index * 2], derived_runs[index * 2 + 1], boundary)
        for index in range(3)
    )
    campaign = timing_math.timing_campaign_result(pair_results)
    samples_bytes = _jsonl(tuple(
        sample.record() for run in derived_runs for sample in run.samples
    ))
    return (
        profile, provenance, commands, tuple(derived_runs), pair_results,
        campaign, boundary, samples_bytes,
    )


def assemble_timing_artifact(root: Path, source: TimingAssemblyInput) -> TimingAssemblyResult:
    """Validate all evidence, then create one unpublished staging artifact."""

    root = Path(root).absolute()
    held_guardian = (
        source.helper_guardian_evidence_bytes
        if isinstance(source.helper_guardian_evidence_bytes, HeldGuardianEvidence)
        else None
    )
    held_control = (
        source.helper_control_evidence_bytes
        if isinstance(source.helper_control_evidence_bytes, HeldControlEvidence)
        else None
    )
    held_sources = tuple(
        item for item in (held_guardian, held_control) if item is not None
    )
    if not held_sources:
        if root.exists() and (
            not root.is_dir() or root.is_symlink() or any(root.iterdir())
        ):
            _fail("timing artifact root is not an empty real directory")
    else:
        expected_names = set()
        if held_guardian is not None:
            held_guardian.verify_held()
            if held_guardian.path != root / "guardian_evidence.jsonl":
                _fail("helper-held guardian inode is not at its frozen staging name")
            expected_names.add("guardian_evidence.jsonl")
        if held_control is not None:
            held_control.verify_held()
            if held_control.path != root / "control_evidence.jsonl":
                _fail("helper-held control inode is not at its frozen staging name")
            expected_names.add("control_evidence.jsonl")
        if (
            not root.is_dir() or root.is_symlink()
            or set(item.name for item in root.iterdir()) != expected_names
        ):
            _fail("staging root does not contain exactly its held evidence inodes")
    (
        profile, provenance, commands, runs, pair_results, campaign, boundary,
        samples_bytes,
    ) = _prepare(source)
    provenance_bytes = _canonical_json(dict(provenance))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for relative, payload in source.support_files.items():
        _write_new(root, relative, payload)
    for result in pair_results:
        _write_new(
            root, "common/pair_{}.bin".format(result.timing_pair_index),
            timing_math.canonical_common_population_payload(
                result.timing_pair_index, result.common_timestamps_ns
            ),
        )
    _write_new(root, "cp2_timing_profile.yaml", profile.canonical_bytes)
    _write_new(root, "execution_binding.json", source.execution_binding_bytes)
    _write_new(root, "control_prior.json", source.control_prior_bytes)
    _write_new(root, "control_applied.json", source.control_applied_bytes)
    _write_new(root, "control_restored.json", source.control_restored_bytes)
    _write_new(
        root, "privileged_helper_transcript.jsonl",
        source.helper_transcript_bytes,
    )
    _write_new(
        root, "privileged_helper_terminal.json",
        source.helper_terminal_receipt_bytes,
    )
    if held_guardian is None:
        _write_new(
            root, "guardian_evidence.jsonl",
            source.helper_guardian_evidence_bytes,
        )
    else:
        held_guardian.verify_held()
    if held_control is None:
        _write_new(
            root, "control_evidence.jsonl",
            source.helper_control_evidence_bytes,
        )
    else:
        held_control.verify_held()
    _write_new(root, "timing_samples.jsonl", samples_bytes)
    _write_new(root, "commands.jsonl", _jsonl(commands))
    _write_new(root, "provenance.json", provenance_bytes)
    report = {
        "schema_version": 1, "record_type": "timing_campaign", "checkpoint": "CP2-E",
        "status": "passed" if campaign.passed else "failed",
        "profile_sha256": profile.sha256,
        "provenance_sha256": _sha_bytes(provenance_bytes),
        "sequence_index": source.sequence_index, "sequence_id": source.sequence_id,
        "bag_begin_record_time_ns": source.bag_begin_record_time_ns,
        "frozen_offset_ns": source.frozen_offset_ns,
        "warmup_boundary_ns": boundary,
        "timing_samples_sha256": _sha_bytes(samples_bytes),
        "helper_transcript_sha256": _sha_bytes(source.helper_transcript_bytes),
        "helper_terminal_receipt_sha256": _sha_bytes(
            source.helper_terminal_receipt_bytes
        ),
        "helper_guardian_evidence_sha256": _guardian_source_sha256(
            source.helper_guardian_evidence_bytes
        ),
        "helper_control_evidence_sha256": _guardian_source_sha256(
            source.helper_control_evidence_bytes
        ),
        "helper_control_evidence_seal_sha256": provenance[
            "helper_control_evidence_seal_sha256"
        ],
        "pair_order": [list(pair) for pair in PAIR_ORDER],
        "runs": [run.record() for run in runs],
        "pair_results": [_pair_record(result) for result in pair_results],
        "median_of_three_median_ratios": _ratio_record(campaign.median_p50.median_ratio),
        "median_of_three_p95_ratios": _ratio_record(campaign.median_p95.median_ratio),
        "every_pair_passed": campaign.every_pair_passed,
        "passed": campaign.passed,
    }
    _write_new(root, "cp2_report.json", _canonical_json(report))
    if held_guardian is not None:
        held_guardian.verify_held()
    if held_control is not None:
        held_control.verify_held()
    manifest = _manifest_bytes(_inventory(root))
    _write_new(root, "SHA256SUMS", manifest)
    return TimingAssemblyResult(_sha_bytes(manifest), campaign.passed)


def _load_report_run_descriptors(
    report: Mapping[str, Any], profile_sha256: str,
) -> Tuple[TimingRun, ...]:
    values = report["runs"]
    if type(values) is not list or len(values) != 6:
        _fail("report run records differ from six slots")
    runs = []
    identities = set()
    for index, value in enumerate(values):
        row = _exact(value, RUN_KEYS, "report run")
        if (
            _u64(row["run_index"], "report run index") != index
            or _u64(row["timing_pair_index"], "report timing-pair index")
            != index // 2
            or _u64(row["position_in_pair"], "report pair position")
            != index % 2
        ):
            _fail("report run indices are not exact unsigned integers")
        if (
            row["run_index"] != index
            or row["timing_pair_index"] != index // 2
            or row["position_in_pair"] != index % 2
            or row["mode"] != PAIR_ORDER[index // 2][index % 2]
            or row["profile_sha256"] != profile_sha256
        ):
            _fail("report run slot/profile identity differs")
        _u64(row["sample_count"], "report run sample count")
        _u64(row["eligible_sample_count"], "report run eligible sample count")
        run = TimingRun(
            run_index=index, profile_sha256=row["profile_sha256"],
            run_id=row["run_id"],
            fresh_process_identity=row["fresh_process_identity"],
            pre_snapshot_sha256=row["pre_snapshot_sha256"],
            post_snapshot_sha256=row["post_snapshot_sha256"],
            trace_bundle_sha256=row["trace_bundle_sha256"],
            process_identity_sha256=row["process_identity_sha256"],
            runtime_preflight_sha256=row["runtime_preflight_sha256"],
            telemetry_pre_sha256=row["telemetry_pre_sha256"],
            telemetry_post_sha256=row["telemetry_post_sha256"],
            telemetry_comparison_sha256=row["telemetry_comparison_sha256"],
            samples=(),
        )
        if run.fresh_process_identity in identities:
            _fail("report fresh-process identities are not unique")
        identities.add(run.fresh_process_identity)
        runs.append(run)
    return tuple(runs)


def _expected_inventory() -> set:
    return {
        relative for index in range(6) for relative in _required_run_support(index)
    } | {
        "cp2_report.json", "cp2_timing_profile.yaml", "provenance.json",
        "commands.jsonl", "timing_samples.jsonl", "common/pair_0.bin",
        "common/pair_1.bin", "common/pair_2.bin", "execution_binding.json",
        "control_prior.json", "control_applied.json", "control_restored.json",
        "privileged_helper_transcript.jsonl",
        "privileged_helper_terminal.json",
        "guardian_evidence.jsonl", "control_evidence.jsonl",
    }


def verify_timing_artifact(root: Path, expected_manifest_sha256: str) -> Mapping[str, Any]:
    """Independently rederive every timing decision from a closed tree."""

    root = Path(root).absolute()
    _sha(expected_manifest_sha256, "expected manifest SHA-256")
    if not root.is_dir() or root.is_symlink():
        _fail("timing artifact root is not a real directory")
    manifest_path = root / "SHA256SUMS"
    manifest_status = manifest_path.lstat()
    if not stat.S_ISREG(manifest_status.st_mode) or manifest_status.st_nlink != 1:
        _fail("timing manifest is unsafe")
    manifest_bytes = manifest_path.read_bytes()
    if _sha_bytes(manifest_bytes) != expected_manifest_sha256:
        _fail("external manifest anchor differs")
    declared = _parse_manifest(manifest_bytes)
    observed = _inventory(root)
    if declared != observed:
        _fail("manifest does not close the exact artifact inventory")
    if set(declared) != _expected_inventory():
        _fail("manifest path population differs from the closed timing schema")
    try:
        profile = profile_codec.load_profile_bytes((root / "cp2_timing_profile.yaml").read_bytes())
    except profile_codec.TimingProfileError as exc:
        raise TimingArtifactError("retained timing profile is invalid: " + str(exc)) from exc
    report = _exact(
        _strict_json((root / "cp2_report.json").read_bytes(), "timing report"),
        REPORT_KEYS, "timing report",
    )
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != 1
        or report["record_type"] != "timing_campaign"
        or report["checkpoint"] != "CP2-E"
        or report["profile_sha256"] != profile.sha256
    ):
        _fail("timing report identity/profile differs")
    profile_input = profile.value["input"]
    if (
        report["sequence_index"] != profile_input["sequence_index"]
        or report["sequence_id"] != profile_input["sequence_id"]
        or report["frozen_offset_ns"] != profile_input["frozen_offset_ns"]
    ):
        _fail("timing report sequence/offset differs from retained profile")
    provenance_bytes = (root / "provenance.json").read_bytes()
    provenance = _validate_provenance(
        _strict_json(provenance_bytes, "timing provenance"), profile,
    )
    if report["provenance_sha256"] != _sha_bytes(provenance_bytes):
        _fail("report provenance digest differs")
    helper_transcript_bytes = (
        root / "privileged_helper_transcript.jsonl"
    ).read_bytes()
    helper_terminal_bytes = (
        root / "privileged_helper_terminal.json"
    ).read_bytes()
    helper_guardian_evidence_path = root / "guardian_evidence.jsonl"
    helper_control_evidence_path = root / "control_evidence.jsonl"
    if (
        report["helper_transcript_sha256"]
        != _sha_bytes(helper_transcript_bytes)
        or report["helper_terminal_receipt_sha256"]
        != _sha_bytes(helper_terminal_bytes)
        or report["helper_guardian_evidence_sha256"]
        != _sha_file(helper_guardian_evidence_path)
        or report["helper_control_evidence_sha256"]
        != _sha_file(helper_control_evidence_path)
        or report["helper_control_evidence_seal_sha256"]
        != provenance["helper_control_evidence_seal_sha256"]
    ):
        _fail("report helper-evidence digest differs from retained files")
    binding, binding_sha256 = _validate_execution_binding(
        (root / "execution_binding.json").read_bytes(), profile, provenance,
    )
    applied_control_state, prior_receipt, applied_receipt, restored_receipt = (
        _validate_control_evidence(
            (root / "control_prior.json").read_bytes(),
            (root / "control_applied.json").read_bytes(),
            (root / "control_restored.json").read_bytes(),
            profile, provenance,
        )
    )
    descriptors = _load_report_run_descriptors(report, profile.sha256)
    _validate_run_population_identity(descriptors, provenance)
    support_files = {
        relative: (root / relative).read_bytes()
        for index in range(6) for relative in _required_run_support(index)
    }
    runs = []
    process_keys = set()
    for descriptor in descriptors:
        run_support = {
            relative: support_files[relative]
            for relative in _required_run_support(descriptor.run_index)
        }
        if trace_bundle_sha256(run_support, descriptor.run_index) != descriptor.trace_bundle_sha256:
            _fail("detached run trace-bundle digest differs")
        samples = _derive_run_samples(
            descriptor, run_support, profile, provenance, applied_control_state,
            binding, binding_sha256,
            report["bag_begin_record_time_ns"],
        )
        identity = _strict_json(
            run_support[
                "runs/run_{}/process_identity.json".format(descriptor.run_index)
            ], "detached run process identity",
        )
        process_key = (identity["pid"], identity["start_time_ticks"])
        if process_key in process_keys:
            _fail("detached six-run population reuses one process instance")
        process_keys.add(process_key)
        run = _derived_run(descriptor, samples)
        if run.record() != report["runs"][descriptor.run_index]:
            _fail("report run population differs from raw trace reconstruction")
        runs.append(run)
    commands = _strict_jsonl((root / "commands.jsonl").read_bytes(), "timing commands")
    commands = _validate_commands(
        commands, profile, support_files, binding, binding_sha256, descriptors,
    )
    _validate_helper_evidence(
        helper_transcript_bytes, helper_terminal_bytes,
        helper_guardian_evidence_path, helper_control_evidence_path,
        profile, provenance,
        commands, support_files, prior_receipt, applied_receipt,
        restored_receipt,
    )
    if not (
        applied_receipt["captured_monotonic_ns"]
        < commands[0]["started_monotonic_ns"]
        and commands[-1]["ended_monotonic_ns"]
        < restored_receipt["captured_monotonic_ns"]
    ):
        _fail("detached command schedule is not enclosed by control receipts")
    if prior_receipt["captured_monotonic_ns"] >= commands[0]["started_monotonic_ns"]:
        _fail("detached prior control receipt does not precede commands")
    derived_sample_bytes = _jsonl(tuple(
        sample.record() for run in runs for sample in run.samples
    ))
    retained_sample_bytes = (root / "timing_samples.jsonl").read_bytes()
    if retained_sample_bytes != derived_sample_bytes:
        _fail("retained reduced timing rows differ from raw trace derivation")
    parsed_samples = tuple(
        sample_from_record(row)
        for row in _strict_jsonl(retained_sample_bytes, "timing samples")
    )
    if tuple(sample for run in runs for sample in run.samples) != parsed_samples:
        _fail("retained timing sample types/order differ from reconstruction")
    if report["timing_samples_sha256"] != _sha_bytes(retained_sample_bytes):
        _fail("timing sample digest differs")
    boundary = _checked_boundary(
        report["bag_begin_record_time_ns"], report["frozen_offset_ns"],
        profile.value["pairing"]["warmup_ns"],
    )
    if (
        report["warmup_boundary_ns"] != boundary
        or report["pair_order"] != [list(pair) for pair in PAIR_ORDER]
    ):
        _fail("report warm-up or pair order differs")
    pair_results = tuple(
        _pair_math(index, runs[index * 2], runs[index * 2 + 1], boundary)
        for index in range(3)
    )
    campaign = timing_math.timing_campaign_result(pair_results)
    if report["pair_results"] != [_pair_record(result) for result in pair_results]:
        _fail("report pair results differ from detached exact recomputation")
    for result in pair_results:
        relative = "common/pair_{}.bin".format(result.timing_pair_index)
        expected = timing_math.canonical_common_population_payload(
            result.timing_pair_index, result.common_timestamps_ns
        )
        if (root / relative).read_bytes() != expected:
            _fail("retained common-population payload differs")
    if (
        report["median_of_three_median_ratios"]
        != _ratio_record(campaign.median_p50.median_ratio)
        or report["median_of_three_p95_ratios"]
        != _ratio_record(campaign.median_p95.median_ratio)
    ):
        _fail("report median-of-three result differs")
    if (
        type(report["every_pair_passed"]) is not bool
        or type(report["passed"]) is not bool
        or report["every_pair_passed"] is not campaign.every_pair_passed
        or report["passed"] is not campaign.passed
        or report["status"] != ("passed" if campaign.passed else "failed")
    ):
        _fail("timing report pass decision differs")
    return {
        "schema_version": 1, "record_type": "timing_verification",
        "checkpoint": "CP2-E", "manifest_sha256": expected_manifest_sha256,
        "profile_sha256": profile.sha256, "pair_count": 3, "run_count": 6,
        "sample_count": len(parsed_samples),
        "every_pair_passed": campaign.every_pair_passed,
        "passed": campaign.passed,
    }


__all__ = [
    "COMMAND_PHASES", "PAIR_ORDER", "HeldControlEvidence", "HeldGuardianEvidence",
    "TimingArtifactError", "TimingAssemblyInput", "TimingAssemblyResult",
    "TimingRun", "TimingSample", "assemble_timing_artifact", "sample_from_record",
    "trace_bundle_sha256", "verify_timing_artifact",
]
