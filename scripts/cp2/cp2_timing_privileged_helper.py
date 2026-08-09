#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Fail-closed protocol core for the CP2-E privileged control guardian.

This module is executable through only the separately audited, root-owned,
single-link launcher and held source/profile closure.  Its production entry
point exposes one data-free apply/validate/restore feasibility transaction;
the formal recorded-workload protocol remains deliberately unavailable.

The code here defines and tests the privilege-separation contract:

* a fixed, bounded, canonical protocol over AF_UNIX/SOCK_SEQPACKET;
* exact SO_PEERCRED, 256-bit nonce, profile, plan, source, protocol-core,
  root-launcher, import-closure, and sudoers SHA-256 binding;
* a one-way state machine (hello, apply, pre observation, unprivileged run,
  post observation, restore);
* a single restoration authority that owns every privileged descriptor;
* durable-prior-before-mutation and fail-closed recovery semantics; and
* a helper-owned held-file, bounded, hash-chained continuous-guardian stream;
* no client-selected paths, commands, modules, services, or MSR registers.

The estimator is a separate unprivileged client process.  The helper never
executes estimator code and never transfers an MSR or cpu_dma_latency file
descriptor to it.  A frozen backend returns only plan-selected raw telemetry
receipts.  Tests use an in-memory backend and do not inspect or mutate the
host.
"""

from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import threading
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 2
FORMAL_RUN_COUNT = 6
RESTORE_SEQUENCE = 2 + 2 * FORMAL_RUN_COUNT
MAX_PACKET_BYTES = 64 * 1024
MAX_RECEIVED_RIGHTS = 16
MAX_STRING_CHARACTERS = 16 * 1024
MAX_CONTAINER_ITEMS = 2048
MAX_GUARDIAN_POPULATION_ITEMS = 2048
MAX_GUARDIAN_LINE_BYTES = 64 * 1024 * 1024
MAX_JSON_DEPTH = 16
MAX_JOURNAL_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_RECORDS = 4096
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_TRANSCRIPT_RECORDS = 18
MAX_CONTROL_STATE_BYTES = 8 * 1024 * 1024
MAX_CONTROL_EVIDENCE_ROWS = 3
MAX_CONTROL_EVIDENCE_LINE_BYTES = MAX_CONTROL_STATE_BYTES + 4096
MAX_CONTROL_EVIDENCE_BYTES = (
    MAX_CONTROL_EVIDENCE_ROWS * MAX_CONTROL_EVIDENCE_LINE_BYTES
)
U64_MAX = (1 << 64) - 1
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SESSION_PATTERN = HASH_PATTERN
NONCE_PATTERN = re.compile(r"^[0-9a-f]{64}$")

HANDSHAKE_BOUNDARIES = (
    "handshake.before_peer_credentials",
    "handshake.after_peer_credentials",
    "handshake.before_challenge",
    "handshake.after_challenge",
    "handshake.before_receive",
    "handshake.after_receive",
    "handshake.after_message_validation",
    "handshake.after_binding_validation",
    "handshake.before_ack",
    "handshake.after_ack",
)

APPLY_BOUNDARIES = (
    "apply.before_capture_prior",
    "apply.after_capture_prior",
    "apply.after_journal_durable",
    "apply.before_backend_apply",
    "apply.after_backend_apply",
    "apply.after_validate_applied",
    "apply.before_ack",
    "apply.after_ack",
)

OBSERVE_BOUNDARIES = (
    "observe.run_{run_index}.{phase}.before_capture",
    "observe.run_{run_index}.{phase}.after_capture",
    "observe.run_{run_index}.{phase}.after_journal_durable",
    "observe.run_{run_index}.{phase}.before_ack",
    "observe.run_{run_index}.{phase}.after_ack",
)

RESTORE_BOUNDARIES = (
    "restore.before_journal",
    "restore.after_journal_durable",
    "restore.before_backend_restore",
    "restore.after_backend_restore",
    "restore.after_validate_restored",
    "restore.before_journal_complete",
    "restore.after_journal_complete",
    "restore.before_ack",
    "restore.after_ack",
)


class PrivilegedHelperError(RuntimeError):
    """The helper contract could not be satisfied."""


class ProtocolError(PrivilegedHelperError):
    """A peer or message violated the frozen wire protocol."""


class PeerClosed(PrivilegedHelperError):
    """The client disappeared before the normal restoration request."""


class RecoveryIndeterminate(PrivilegedHelperError):
    """Exact restoration could not be independently proved."""


class ProductionHelperUnavailable(PrivilegedHelperError):
    """The audited root-owned launcher/import closure is not available."""


class DataFreeReversibilityIndeterminate(RecoveryIndeterminate):
    """The data-free feasibility transaction did not prove exact restoration."""


def _fail(message: str) -> None:
    raise PrivilegedHelperError(message)


def _require_hash(value: Any, label: str) -> str:
    if type(value) is not str or HASH_PATTERN.fullmatch(value) is None:
        _fail(label + " is not one lowercase SHA-256")
    return value


def _require_u64(value: Any, label: str) -> int:
    if type(value) is not int or value < 0 or value > U64_MAX:
        _fail(label + " is outside the unsigned 64-bit domain")
    return value


def _require_i64(value: Any, label: str) -> int:
    if type(value) is not int or value < I64_MIN or value > I64_MAX:
        _fail(label + " is outside the signed 64-bit domain")
    return value


def _validate_json_domain(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail("canonical JSON exceeds its nesting bound")
    if value is None or type(value) is bool:
        return
    if type(value) is int:
        if value < -U64_MAX or value > U64_MAX:
            _fail("canonical JSON integer exceeds its bounded domain")
        return
    if type(value) is str:
        if len(value) > MAX_STRING_CHARACTERS or "\x00" in value:
            _fail("canonical JSON string is oversized or contains NUL")
        return
    if type(value) is list:
        if len(value) > MAX_CONTAINER_ITEMS:
            _fail("canonical JSON list exceeds its item bound")
        for item in value:
            _validate_json_domain(item, depth + 1)
        return
    if type(value) is dict:
        if len(value) > MAX_CONTAINER_ITEMS:
            _fail("canonical JSON mapping exceeds its item bound")
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 192:
                _fail("canonical JSON has an invalid mapping key")
            _validate_json_domain(item, depth + 1)
        return
    _fail("canonical JSON contains a forbidden value type")


def _bounded_canonical_json_bytes(
    value: Mapping[str, Any], maximum_bytes: int, label: str,
) -> bytes:
    if type(value) is not dict:
        _fail(label + " is not a mapping")
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        _fail(label + " byte bound is invalid")
    _validate_json_domain(value)
    payload = json.dumps(
        value, allow_nan=False, ensure_ascii=False, separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    if len(payload) > maximum_bytes:
        _fail(label + " exceeds its byte bound")
    return payload


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return _bounded_canonical_json_bytes(
        value, MAX_PACKET_BYTES, "canonical protocol record",
    )


def canonical_control_state_bytes(value: Mapping[str, Any]) -> bytes:
    """Canonical bytes for one bounded full control-state mapping."""

    return _bounded_canonical_json_bytes(
        value, MAX_CONTROL_STATE_BYTES, "canonical control state",
    )


def _strict_bounded_json_bytes(
    payload: bytes, maximum_bytes: int, label: str,
) -> Mapping[str, Any]:
    if type(payload) is not bytes or not payload or len(payload) > maximum_bytes:
        raise ProtocolError(label + " is empty or oversized")
    if not payload.endswith(b"\n") or b"\x00" in payload:
        raise ProtocolError(label + " is not one terminated JSON record")

    def pairs(items: List[Tuple[str, Any]]) -> Dict[str, Any]:
        retained: Dict[str, Any] = {}
        for key, item in items:
            if key in retained:
                raise ProtocolError("protocol packet contains a duplicate JSON key")
            retained[key] = item
        return retained

    def reject_float(_value: str) -> Any:
        raise ProtocolError("protocol packet contains a floating-point value")

    try:
        value = json.loads(
            payload.decode("utf-8", "strict"), object_pairs_hook=pairs,
            parse_float=reject_float, parse_constant=reject_float,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError("protocol packet is not strict UTF-8 JSON") from exc
    try:
        encoded = _bounded_canonical_json_bytes(value, maximum_bytes, label)
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if encoded != payload:
        raise ProtocolError(label + " is not canonically encoded")
    return value


def strict_json_bytes(payload: bytes) -> Mapping[str, Any]:
    return _strict_bounded_json_bytes(
        payload, MAX_PACKET_BYTES, "protocol packet",
    )


def _socket_type(connection: socket.socket) -> int:
    return int(connection.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE))


def recv_packet(connection: socket.socket) -> Mapping[str, Any]:
    if connection.family != socket.AF_UNIX or _socket_type(connection) != socket.SOCK_SEQPACKET:
        raise ProtocolError("helper requires AF_UNIX SOCK_SEQPACKET")
    ancillary_bound = socket.CMSG_SPACE(MAX_RECEIVED_RIGHTS * struct.calcsize("i"))
    payload, ancillary, flags, _address = connection.recvmsg(
        MAX_PACKET_BYTES + 1, ancillary_bound
    )
    # The protocol never accepts descriptor or credential control messages.
    # Close every received SCM_RIGHTS duplicate before rejecting so a hostile
    # peer cannot leak privileged-helper descriptors. MSG_CTRUNC is itself a
    # hard failure because the complete ancillary population is then unknown.
    for level, message_type, data in ancillary:
        if level == socket.SOL_SOCKET and message_type == socket.SCM_RIGHTS:
            width = struct.calcsize("i")
            for offset in range(0, len(data) - (len(data) % width), width):
                received_fd = struct.unpack_from("i", data, offset)[0]
                try:
                    os.close(received_fd)
                except OSError:
                    pass
    if not payload:
        raise PeerClosed("privileged helper peer closed its session")
    if flags & getattr(socket, "MSG_TRUNC", 0) or len(payload) > MAX_PACKET_BYTES:
        raise ProtocolError("protocol packet was truncated or oversized")
    if flags & getattr(socket, "MSG_CTRUNC", 0) or ancillary:
        raise ProtocolError("protocol packet contains forbidden or truncated ancillary data")
    return strict_json_bytes(payload)


def send_packet(connection: socket.socket, value: Mapping[str, Any]) -> None:
    payload = canonical_json_bytes(value)
    count = connection.send(payload)
    if count != len(payload):
        raise ProtocolError("SOCK_SEQPACKET send was incomplete")


@dataclass(frozen=True)
class Binding:
    """Every immutable input used by the root-side helper."""

    profile_sha256: str
    plan_sha256: str
    source_sha256: str
    protocol_core_sha256: str
    root_launcher_sha256: str
    import_closure_sha256: str
    sudoers_sha256: str

    def __post_init__(self) -> None:
        for label, value in self.as_record().items():
            _require_hash(value, label)

    def as_record(self) -> Mapping[str, str]:
        return {
            "import_closure_sha256": self.import_closure_sha256,
            "plan_sha256": self.plan_sha256,
            "profile_sha256": self.profile_sha256,
            "protocol_core_sha256": self.protocol_core_sha256,
            "root_launcher_sha256": self.root_launcher_sha256,
            "source_sha256": self.source_sha256,
            "sudoers_sha256": self.sudoers_sha256,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(dict(self.as_record()))).hexdigest()

    @classmethod
    def from_record(cls, value: Any) -> "Binding":
        expected = {
            "import_closure_sha256", "plan_sha256", "profile_sha256",
            "protocol_core_sha256", "root_launcher_sha256", "source_sha256",
            "sudoers_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ProtocolError("hello binding keys differ from the frozen schema")
        try:
            return cls(
                value["profile_sha256"], value["plan_sha256"],
                value["source_sha256"], value["protocol_core_sha256"],
                value["root_launcher_sha256"], value["import_closure_sha256"],
                value["sudoers_sha256"],
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc


@dataclass(frozen=True)
class FrozenRunSlot:
    """One plan/profile-selected timing slot; the client cannot alter it."""

    run_index: int
    timing_pair_index: int
    position_in_pair: int
    mode: str

    def __post_init__(self) -> None:
        _require_u64(self.run_index, "run-slot index")
        _require_u64(self.timing_pair_index, "run-slot timing-pair index")
        _require_u64(self.position_in_pair, "run-slot position")
        if (
            self.run_index >= FORMAL_RUN_COUNT
            or self.timing_pair_index != self.run_index // 2
            or self.position_in_pair != self.run_index % 2
            or self.mode not in ("nullspace", "schur")
        ):
            _fail("run-slot identity differs from the frozen six-slot domain")

    def as_record(self) -> Mapping[str, Any]:
        return {
            "mode": self.mode,
            "position_in_pair": self.position_in_pair,
            "run_index": self.run_index,
            "timing_pair_index": self.timing_pair_index,
        }

    @classmethod
    def from_record(cls, value: Any) -> "FrozenRunSlot":
        expected = {
            "mode", "position_in_pair", "run_index", "timing_pair_index",
        }
        if type(value) is not dict or set(value) != expected:
            raise ProtocolError("run-slot keys differ from the frozen schema")
        try:
            return cls(
                value["run_index"], value["timing_pair_index"],
                value["position_in_pair"], value["mode"],
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc


def _frozen_run_slots(value: Sequence[FrozenRunSlot]) -> Tuple[FrozenRunSlot, ...]:
    retained = tuple(value)
    if len(retained) != FORMAL_RUN_COUNT or any(
        type(slot) is not FrozenRunSlot or slot.run_index != index
        for index, slot in enumerate(retained)
    ):
        _fail("frozen run-slot population is not exactly indexed zero through five")
    return retained


@dataclass(frozen=True)
class GuardianEvidenceContract:
    """Trusted profile/plan-derived bounds for streamed guardian evidence."""

    poll_interval_ns: int
    maximum_observation_gap_ns: int
    maximum_campaign_duration_ns: int
    maximum_observation_count: int
    maximum_line_bytes: int
    maximum_evidence_bytes: int
    required_surfaces: Tuple[str, ...]
    maximum_population_items: int = MAX_GUARDIAN_POPULATION_ITEMS

    def __post_init__(self) -> None:
        for label, value in (
            ("guardian poll interval", self.poll_interval_ns),
            ("guardian maximum observation gap", self.maximum_observation_gap_ns),
            ("guardian maximum campaign duration", self.maximum_campaign_duration_ns),
            ("guardian maximum observation count", self.maximum_observation_count),
            ("guardian maximum line bytes", self.maximum_line_bytes),
            ("guardian maximum evidence bytes", self.maximum_evidence_bytes),
            ("guardian maximum population items", self.maximum_population_items),
        ):
            _require_u64(value, label)
            if value == 0:
                _fail(label + " is zero")
        if not (
            self.poll_interval_ns <= self.maximum_observation_gap_ns
            <= self.maximum_campaign_duration_ns
        ):
            _fail("guardian cadence/gap/duration ordering differs")
        exact_count = self.maximum_campaign_duration_ns // self.poll_interval_ns + 1
        if self.maximum_observation_count != exact_count:
            _fail("guardian maximum observation count differs from floor(D/P)+1")
        if (
            self.maximum_line_bytes > MAX_GUARDIAN_LINE_BYTES
            or self.maximum_observation_count * self.maximum_line_bytes
            > self.maximum_evidence_bytes
            or self.maximum_population_items
            != MAX_GUARDIAN_POPULATION_ITEMS
        ):
            _fail("guardian line/count capacity exceeds its frozen evidence-byte cap")
        surfaces = tuple(self.required_surfaces)
        if (
            not surfaces or len(surfaces) > 256 or surfaces != tuple(sorted(set(surfaces)))
            or any(type(item) is not str or re.fullmatch(r"[a-z0-9_.-]{1,128}", item) is None
                   for item in surfaces)
        ):
            _fail("guardian required-surface population differs")
        object.__setattr__(self, "required_surfaces", surfaces)

    def as_record(self) -> Mapping[str, Any]:
        return {
            "maximum_campaign_duration_ns": self.maximum_campaign_duration_ns,
            "maximum_evidence_bytes": self.maximum_evidence_bytes,
            "maximum_line_bytes": self.maximum_line_bytes,
            "maximum_observation_count": self.maximum_observation_count,
            "maximum_observation_gap_ns": self.maximum_observation_gap_ns,
            "maximum_population_items": self.maximum_population_items,
            "poll_interval_ns": self.poll_interval_ns,
            "required_surfaces": list(self.required_surfaces),
        }

    @classmethod
    def from_record(cls, value: Any) -> "GuardianEvidenceContract":
        expected = {
            "maximum_campaign_duration_ns", "maximum_evidence_bytes",
            "maximum_line_bytes", "maximum_observation_count", "maximum_observation_gap_ns",
            "maximum_population_items", "poll_interval_ns", "required_surfaces",
        }
        if type(value) is not dict or set(value) != expected or type(value["required_surfaces"]) is not list:
            raise ProtocolError("guardian evidence-contract keys differ")
        try:
            return cls(
                value["poll_interval_ns"], value["maximum_observation_gap_ns"],
                value["maximum_campaign_duration_ns"],
                value["maximum_observation_count"], value["maximum_line_bytes"],
                value["maximum_evidence_bytes"],
                tuple(value["required_surfaces"]),
                value["maximum_population_items"],
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc


class GuardianEvidenceWriter:
    """Helper-owned held-file writer for the complete guardian JSONL chain."""

    PAYLOAD_KEYS = {
        "child_cpuset_members", "complete_control_plane_processes",
        "complete_control_plane_threads", "complete_descendant_population",
        "complete_descendant_threads", "complete_descendant_tid_population",
        "drift", "ended_monotonic_ns", "foreign_affinity_eligibility",
        "irq_population_sha256", "scheduler_witness_sha256",
        "started_monotonic_ns", "surface_states",
        "thermal_or_throttle_event", "typed_telemetry",
    }

    def __init__(
        self, contract: GuardianEvidenceContract, descriptor: int, *,
        expected_uid: int = 0, retain_bytes_after_close: bool = False,
    ) -> None:
        if type(contract) is not GuardianEvidenceContract:
            _fail("guardian evidence contract is not frozen")
        if type(descriptor) is not int or descriptor < 0:
            _fail("guardian evidence descriptor is invalid")
        if type(expected_uid) is not int or expected_uid < 0:
            _fail("guardian evidence owner UID is invalid")
        if type(retain_bytes_after_close) is not bool:
            _fail("guardian evidence retention policy is not boolean")
        self.contract = contract
        self.descriptor = descriptor
        self.expected_uid = expected_uid
        self.retain_bytes_after_close = retain_bytes_after_close
        self._previous_sha256 = "0" * 64
        self._first_sha256: Optional[str] = None
        self._count = 0
        self._size = 0
        self._digest = hashlib.sha256()
        self._sealed = False
        self._closed = False
        self._late_append_attempted = False
        self._retained_payload: Optional[bytes] = None
        self._expected_peer: Optional[PeerCredentials] = None
        self._session_id: Optional[str] = None
        self._previous_start: Optional[int] = None
        self._previous_end: Optional[int] = None
        self._first_start: Optional[int] = None
        self._final_descendants: Optional[List[int]] = None
        self._final_descendant_tids: Optional[List[int]] = None
        self._final_foreign_affinity_eligibility: Optional[
            List[Mapping[str, Any]]
        ] = None
        self._final_child_cpuset_member_count: Optional[int] = None
        self._final_control_plane_process_count: Optional[int] = None
        self._final_control_plane_tid_count: Optional[int] = None
        self._final_scheduler_witness_sha256: Optional[str] = None
        self._frozen_sources: Optional[Mapping[str, Tuple[Any, ...]]] = None
        self._previous_throttles: Optional[Mapping[str, int]] = None
        self._irq_population_sha256: Optional[str] = None
        self._lock = threading.Lock()
        status = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        self.access_mode = flags & os.O_ACCMODE
        descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
        if (
            not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
            or status.st_uid != expected_uid or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_size != 0 or not flags & os.O_APPEND
            or self.access_mode not in (os.O_WRONLY, os.O_RDWR)
            or (retain_bytes_after_close and self.access_mode != os.O_RDWR)
            or not descriptor_flags & fcntl.FD_CLOEXEC
        ):
            _fail("guardian evidence held file metadata/flags differ")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PrivilegedHelperError("guardian evidence held file lock failed") from exc
        self.device = status.st_dev
        self.inode = status.st_ino

    def bind_identity(self, peer: PeerCredentials, session_id: str) -> None:
        if type(peer) is not PeerCredentials:
            _fail("guardian evidence peer identity is invalid")
        _require_hash(session_id, "guardian evidence session ID")
        with self._lock:
            if self._expected_peer is not None or self._count != 0:
                _fail("guardian evidence identity was bound late or twice")
            self._expected_peer = peer
            self._session_id = session_id

    def _verify_held(self) -> os.stat_result:
        if self._closed:
            _fail("guardian evidence descriptor is closed")
        status = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(status.st_mode) or status.st_dev != self.device
            or status.st_ino != self.inode or status.st_nlink != 1
            or status.st_uid != self.expected_uid or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_size != self._size
        ):
            _fail("guardian evidence held inode changed")
        return status

    def append(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            if type(payload) is not dict or set(payload) != self.PAYLOAD_KEYS:
                _fail("guardian evidence payload keys differ from the frozen schema")
            if self._expected_peer is None or self._session_id is None:
                raise RecoveryIndeterminate("guardian evidence identity is not bound")
            if self._sealed:
                self._late_append_attempted = True
                raise RecoveryIndeterminate("guardian evidence append followed its terminal seal")
            if self._count >= self.contract.maximum_observation_count:
                raise RecoveryIndeterminate("guardian evidence observation-count bound is exhausted")
            try:
                started = _require_u64(payload["started_monotonic_ns"], "guardian observation start")
                ended = _require_u64(payload["ended_monotonic_ns"], "guardian observation end")
                _require_hash(payload["irq_population_sha256"], "guardian IRQ population digest")
            except PrivilegedHelperError:
                raise
            if (
                ended < started
                or ended - started > self.contract.maximum_observation_gap_ns
                or (self._previous_end is not None and (
                    started < self._previous_end
                    or started - self._previous_end > self.contract.maximum_observation_gap_ns
                ))
                or (self._previous_start is not None and
                    started - self._previous_start < self.contract.poll_interval_ns)
            ):
                raise RecoveryIndeterminate("guardian observation cadence/gap/duration differs")
            try:
                witness = _validated_scheduler_witness(
                    payload, self._expected_peer,
                )
            except ProtocolError as exc:
                raise RecoveryIndeterminate(str(exc)) from exc
            descendants = witness["complete_descendant_population"]
            descendant_tids = witness["complete_descendant_tid_population"]
            foreign = _foreign_affinity_eligibility_rows(
                witness["foreign_affinity_eligibility"],
                "guardian foreign affinity eligibility",
            )
            if (
                _exact_bool(payload["drift"], "guardian drift")
                or _exact_bool(payload["thermal_or_throttle_event"], "guardian thermal event")
            ):
                raise RecoveryIndeterminate("guardian task/drift state differs")
            surfaces = payload["surface_states"]
            if type(surfaces) is not list or len(surfaces) != len(self.contract.required_surfaces):
                raise RecoveryIndeterminate("guardian surface-state population differs")
            surface_ids = []
            for state in surfaces:
                item = _exact_keys(
                    state, ("passed", "state_sha256", "surface_id"),
                    "guardian surface state",
                )
                if not _exact_bool(item["passed"], "guardian surface pass"):
                    raise RecoveryIndeterminate("guardian surface did not pass")
                _require_hash(item["state_sha256"], "guardian surface-state digest")
                surface_ids.append(item["surface_id"])
            if surface_ids != list(self.contract.required_surfaces):
                raise RecoveryIndeterminate("guardian required-surface coverage differs")
            source_bindings, parsed = _guardian_typed_telemetry(
                payload["typed_telemetry"], started, ended,
            )
            if self._frozen_sources is not None and source_bindings != self._frozen_sources:
                raise RecoveryIndeterminate("guardian typed source binding changed")
            throttles = {
                source_id: parsed[source_id]
                for source_id, binding in source_bindings.items()
                if binding[0] == "amd_throttle"
            }
            if self._previous_throttles is not None and any(
                value != self._previous_throttles[source_id]
                for source_id, value in throttles.items()
            ):
                raise RecoveryIndeterminate("guardian throttle counter changed")
            if (
                self._irq_population_sha256 is not None
                and payload["irq_population_sha256"] != self._irq_population_sha256
            ):
                raise RecoveryIndeterminate("guardian IRQ population changed")
            record = dict(payload)
            record.update({
                "previous_record_sha256": self._previous_sha256,
                "record_type": "cp2e_guardian_observation",
                "schema_version": SCHEMA_VERSION,
                "sequence": self._count,
            })
            try:
                encoded = _bounded_canonical_json_bytes(
                    record, self.contract.maximum_line_bytes,
                    "guardian evidence record",
                )
            except PrivilegedHelperError as exc:
                raise RecoveryIndeterminate(str(exc)) from exc
            if len(encoded) > self.contract.maximum_line_bytes:
                raise RecoveryIndeterminate("guardian evidence line-byte bound is exhausted")
            if self._size + len(encoded) > self.contract.maximum_evidence_bytes:
                raise RecoveryIndeterminate("guardian evidence byte bound is exhausted")
            self._verify_held()
            offset = 0
            while offset < len(encoded):
                count = os.write(self.descriptor, encoded[offset:])
                if count <= 0:
                    raise RecoveryIndeterminate("guardian evidence write made no progress")
                offset += count
            self._size += len(encoded)
            self._digest.update(encoded)
            self._previous_sha256 = hashlib.sha256(encoded).hexdigest()
            if self._first_sha256 is None:
                self._first_sha256 = self._previous_sha256
                self._first_start = started
            self._count += 1
            self._previous_start, self._previous_end = started, ended
            self._final_descendants = list(descendants)
            self._final_descendant_tids = list(descendant_tids)
            self._final_foreign_affinity_eligibility = [
                dict(item) for item in foreign
            ]
            self._final_child_cpuset_member_count = len(
                witness["child_cpuset_members"]
            )
            self._final_control_plane_process_count = len(
                witness["complete_control_plane_processes"]
            )
            self._final_control_plane_tid_count = len(
                witness["complete_control_plane_threads"]
            )
            self._final_scheduler_witness_sha256 = payload[
                "scheduler_witness_sha256"
            ]
            self._frozen_sources = source_bindings
            self._previous_throttles = throttles
            self._irq_population_sha256 = payload["irq_population_sha256"]

    def seal(self, terminal_state: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            if self._sealed:
                _fail("guardian evidence was sealed twice")
            if self._count == 0 or self._expected_peer is None or self._session_id is None:
                raise RecoveryIndeterminate("guardian evidence cannot seal an empty/unbound stream")
            terminal = _exact_keys(terminal_state, (
                "bound_peer", "coverage_ended_monotonic_ns",
                "coverage_started_monotonic_ns", "drift",
                "final_child_cpuset_member_count",
                "final_control_plane_process_count",
                "final_control_plane_tid_count",
                "final_descendant_process_count", "final_descendant_tid_count",
                "final_foreign_affinity_eligibility_count",
                "final_foreign_affinity_eligibility_sha256",
                "final_scheduler_witness_sha256",
                "irq_population_changed", "maximum_observation_gap_ns",
                "required_surfaces", "schema_version", "session_id",
                "thermal_or_throttle_event",
            ), "guardian terminal state")
            try:
                coverage_start = _require_u64(
                    terminal["coverage_started_monotonic_ns"], "guardian coverage start",
                )
                coverage_end = _require_u64(
                    terminal["coverage_ended_monotonic_ns"], "guardian coverage end",
                )
            except PrivilegedHelperError:
                raise
            if (
                type(terminal["schema_version"]) is not int
                or terminal["schema_version"] != SCHEMA_VERSION
                or terminal["session_id"] != self._session_id
                or PeerCredentials.from_record(terminal["bound_peer"]) != self._expected_peer
                or terminal["maximum_observation_gap_ns"]
                != self.contract.maximum_observation_gap_ns
                or terminal["required_surfaces"] != list(self.contract.required_surfaces)
                or coverage_start > self._first_start
                or self._first_start - coverage_start > self.contract.maximum_observation_gap_ns
                or coverage_end < self._previous_end
                or coverage_end - self._previous_end > self.contract.maximum_observation_gap_ns
                or coverage_end - coverage_start > self.contract.maximum_campaign_duration_ns
                or terminal["final_child_cpuset_member_count"]
                != self._final_child_cpuset_member_count
                or terminal["final_control_plane_process_count"]
                != self._final_control_plane_process_count
                or terminal["final_control_plane_tid_count"]
                != self._final_control_plane_tid_count
                or terminal["final_descendant_process_count"]
                != len(self._final_descendants)
                or terminal["final_descendant_tid_count"]
                != len(self._final_descendant_tids)
                or terminal["final_foreign_affinity_eligibility_count"]
                != len(self._final_foreign_affinity_eligibility)
                or terminal["final_foreign_affinity_eligibility_sha256"]
                != hashlib.sha256(_bounded_canonical_json_bytes(
                    {"rows": self._final_foreign_affinity_eligibility},
                    self.contract.maximum_line_bytes,
                    "terminal foreign-affinity witness",
                )).hexdigest()
                or terminal["final_scheduler_witness_sha256"]
                != self._final_scheduler_witness_sha256
                or _exact_bool(terminal["drift"], "terminal guardian drift")
                or _exact_bool(terminal["irq_population_changed"], "terminal guardian IRQ change")
                or _exact_bool(
                    terminal["thermal_or_throttle_event"],
                    "terminal guardian thermal event",
                )
            ):
                raise RecoveryIndeterminate("guardian terminal state/coverage differs")
            self._verify_held()
            os.fsync(self.descriptor)
            status = self._verify_held()
            if self.retain_bytes_after_close:
                payload = os.pread(self.descriptor, status.st_size, 0)
                if len(payload) != status.st_size:
                    raise RecoveryIndeterminate("guardian evidence retained read ended early")
                self._retained_payload = payload
            seal = dict(terminal)
            seal.update({
                "evidence_sha256": self._digest.hexdigest(),
                "evidence_size_bytes": self._size,
                "first_record_sha256": self._first_sha256,
                "last_record_sha256": self._previous_sha256,
                "observation_count": self._count,
            })
            canonical_json_bytes(seal)
            self._sealed = True
            return seal

    @property
    def payload(self) -> bytes:
        if self._retained_payload is not None:
            return self._retained_payload
        if not self._sealed:
            if self._count == 0:
                return b""
            _fail("guardian evidence is not sealed")
        if self._closed:
            _fail("guardian evidence bytes were not retained before descriptor closure")
        if self.access_mode != os.O_RDWR:
            _fail("write-only guardian evidence descriptor cannot expose bytes")
        payload = os.pread(self.descriptor, self._size, 0)
        if len(payload) != self._size:
            _fail("guardian evidence read ended early")
        return payload

    @property
    def count(self) -> int:
        return self._count

    @property
    def size(self) -> int:
        return self._size

    @property
    def first_sha256(self) -> Optional[str]:
        return self._first_sha256

    @property
    def last_sha256(self) -> Optional[str]:
        return None if self._count == 0 else self._previous_sha256

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self.retain_bytes_after_close and self._retained_payload is None:
                    payload = os.pread(self.descriptor, self._size, 0)
                    if len(payload) != self._size:
                        raise RecoveryIndeterminate(
                            "guardian evidence close-time retained read ended early"
                        )
                    self._retained_payload = payload
                os.close(self.descriptor)
                self._closed = True
                if self._late_append_attempted:
                    raise RecoveryIndeterminate(
                        "guardian backend attempted evidence append after terminal seal"
                    )


class ControlEvidenceWriter:
    """Held-inode writer for bounded prior/applied/restored control states."""

    _SUCCESS_PHASES = ("prior", "applied", "restored")
    _FAIL_CLOSED_PHASES = ("prior", "restored")

    def __init__(
        self, descriptor: int, *, expected_uid: int = 0,
        retain_bytes_after_close: bool = False,
    ) -> None:
        if type(descriptor) is not int or descriptor < 0:
            _fail("control evidence descriptor is invalid")
        if type(expected_uid) is not int or expected_uid < 0:
            _fail("control evidence owner UID is invalid")
        if type(retain_bytes_after_close) is not bool:
            _fail("control evidence retention policy is not boolean")
        self.descriptor = descriptor
        self.expected_uid = expected_uid
        self.retain_bytes_after_close = retain_bytes_after_close
        self._binding_digest: Optional[str] = None
        self._session_id: Optional[str] = None
        self._journal_id: Optional[str] = None
        self._rows: List[Mapping[str, Any]] = []
        self._phases: List[str] = []
        self._previous_sha256 = "0" * 64
        self._first_sha256: Optional[str] = None
        self._size = 0
        self._digest = hashlib.sha256()
        self._state_sha256: Dict[str, str] = {}
        self._sealed = False
        self._closed = False
        self._retained_payload: Optional[bytes] = None
        self._seal: Optional[Mapping[str, Any]] = None
        self._lock = threading.Lock()
        status = os.fstat(descriptor)
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
        self.access_mode = flags & os.O_ACCMODE
        if (
            not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
            or status.st_uid != expected_uid or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_size != 0 or not flags & os.O_APPEND
            or self.access_mode != os.O_RDWR
            or not descriptor_flags & fcntl.FD_CLOEXEC
        ):
            _fail("control evidence held file metadata/flags differ")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PrivilegedHelperError("control evidence held file lock failed") from exc
        self.device = status.st_dev
        self.inode = status.st_ino

    def bind_identity(
        self, binding: Binding, session_id: str, journal_id: str,
    ) -> None:
        if type(binding) is not Binding:
            _fail("control evidence binding is not frozen")
        _require_hash(session_id, "control evidence session ID")
        if type(journal_id) is not str or re.fullmatch(r"[0-9a-f]{32}", journal_id) is None:
            _fail("control evidence journal ID is invalid")
        with self._lock:
            if self._binding_digest is not None or self._rows:
                _fail("control evidence identity was bound late or twice")
            self._binding_digest = binding.digest
            self._session_id = session_id
            self._journal_id = journal_id

    def _verify_held(self, expected_size: Optional[int] = None) -> os.stat_result:
        if self._closed:
            _fail("control evidence descriptor is closed")
        size = self._size if expected_size is None else expected_size
        status = os.fstat(self.descriptor)
        flags = fcntl.fcntl(self.descriptor, fcntl.F_GETFL)
        descriptor_flags = fcntl.fcntl(self.descriptor, fcntl.F_GETFD)
        if (
            not stat.S_ISREG(status.st_mode) or status.st_dev != self.device
            or status.st_ino != self.inode or status.st_nlink != 1
            or status.st_uid != self.expected_uid or stat.S_IMODE(status.st_mode) != 0o600
            or status.st_size != size or not flags & os.O_APPEND
            or (flags & os.O_ACCMODE) != os.O_RDWR
            or not descriptor_flags & fcntl.FD_CLOEXEC
        ):
            raise RecoveryIndeterminate("control evidence held inode changed")
        return status

    def _read_exact(self, expected_size: int, expected_sha256: str) -> bytes:
        self._verify_held(expected_size)
        payload = os.pread(self.descriptor, expected_size, 0)
        if (
            len(payload) != expected_size
            or hashlib.sha256(payload).hexdigest() != expected_sha256
        ):
            raise RecoveryIndeterminate("control evidence exact bytes/digest changed")
        return payload

    def _checkpoint(self) -> Mapping[str, Any]:
        return {
            "device": self.device,
            "evidence_sha256": self._digest.hexdigest(),
            "evidence_size_bytes": self._size,
            "inode": self.inode,
            "last_record_sha256": (
                None if not self._rows else self._previous_sha256
            ),
            "row_count": len(self._rows),
            "rows": [dict(item) for item in self._rows],
        }

    def append(self, phase: str, state: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._lock:
            if self._binding_digest is None or self._session_id is None or self._journal_id is None:
                raise RecoveryIndeterminate("control evidence identity is not bound")
            if self._sealed:
                raise RecoveryIndeterminate("control evidence append followed its terminal seal")
            if len(self._rows) >= MAX_CONTROL_EVIDENCE_ROWS:
                raise RecoveryIndeterminate("control evidence row-count bound is exhausted")
            if type(state) is not dict:
                _fail("full control state is not a mapping")
            allowed_next = (
                ("prior",) if not self._phases
                else ("applied", "restored") if self._phases == ["prior"]
                else ("restored",) if self._phases == ["prior", "applied"]
                else ()
            )
            if phase not in allowed_next:
                raise RecoveryIndeterminate("control evidence phase order differs")
            state_bytes = canonical_control_state_bytes(dict(state))
            state_sha256 = hashlib.sha256(state_bytes).hexdigest()
            record = {
                "binding_digest": self._binding_digest,
                "journal_id": self._journal_id,
                "phase": phase,
                "previous_record_sha256": self._previous_sha256,
                "record_type": "cp2e_control_state",
                "schema_version": SCHEMA_VERSION,
                "sequence": len(self._rows),
                "session_id": self._session_id,
                "state": dict(state),
                "state_sha256": state_sha256,
            }
            encoded = _bounded_canonical_json_bytes(
                record, MAX_CONTROL_EVIDENCE_LINE_BYTES,
                "canonical control evidence row",
            )
            if self._size + len(encoded) > MAX_CONTROL_EVIDENCE_BYTES:
                raise RecoveryIndeterminate("control evidence total-byte bound is exhausted")
            current_sha256 = self._digest.hexdigest()
            self._read_exact(self._size, current_sha256)
            candidate_size = self._size + len(encoded)
            candidate_digest = self._digest.copy()
            candidate_digest.update(encoded)
            offset = 0
            while offset < len(encoded):
                count = os.write(self.descriptor, encoded[offset:])
                if count <= 0:
                    raise RecoveryIndeterminate("control evidence write made no progress")
                offset += count
            os.fsync(self.descriptor)
            candidate_sha256 = candidate_digest.hexdigest()
            self._read_exact(candidate_size, candidate_sha256)
            record_sha256 = hashlib.sha256(encoded).hexdigest()
            row_receipt = {
                "end_offset_bytes": candidate_size,
                "phase": phase,
                "record_sha256": record_sha256,
                "sequence": len(self._rows),
                "state_sha256": state_sha256,
            }
            self._size = candidate_size
            self._digest = candidate_digest
            self._previous_sha256 = record_sha256
            if self._first_sha256 is None:
                self._first_sha256 = record_sha256
            self._rows.append(row_receipt)
            self._phases.append(phase)
            self._state_sha256[phase] = state_sha256
            checkpoint = self._checkpoint()
            canonical_json_bytes(dict(checkpoint))
            return checkpoint

    def seal(self) -> Mapping[str, Any]:
        with self._lock:
            if self._sealed:
                _fail("control evidence was sealed twice")
            phases = tuple(self._phases)
            if phases not in (self._SUCCESS_PHASES, self._FAIL_CLOSED_PHASES):
                raise RecoveryIndeterminate("control evidence phase population cannot seal")
            restoration_matches = (
                self._state_sha256["prior"] == self._state_sha256["restored"]
            )
            if not restoration_matches:
                raise RecoveryIndeterminate("control evidence prior/restored digests differ")
            self._read_exact(self._size, self._digest.hexdigest())
            os.fsync(self.descriptor)
            payload = self._read_exact(self._size, self._digest.hexdigest())
            seal = {
                "binding_digest": self._binding_digest,
                "device": self.device,
                "evidence_sha256": self._digest.hexdigest(),
                "evidence_size_bytes": self._size,
                "first_record_sha256": self._first_sha256,
                "inode": self.inode,
                "journal_id": self._journal_id,
                "last_record_sha256": self._previous_sha256,
                "phases": list(phases),
                "record_type": "cp2e_control_evidence_terminal",
                "restoration_matches_prior": restoration_matches,
                "row_count": len(self._rows),
                "rows": [dict(item) for item in self._rows],
                "schema_version": SCHEMA_VERSION,
                "session_id": self._session_id,
            }
            canonical_json_bytes(seal)
            self._seal = seal
            self._sealed = True
            if self.retain_bytes_after_close:
                self._retained_payload = payload
            return dict(seal)

    @property
    def payload(self) -> bytes:
        if self._retained_payload is not None:
            return self._retained_payload
        if self._closed:
            _fail("control evidence bytes were not retained before descriptor closure")
        if not self._rows:
            return b""
        return self._read_exact(self._size, self._digest.hexdigest())

    @property
    def checkpoint(self) -> Mapping[str, Any]:
        with self._lock:
            return self._checkpoint()

    @property
    def seal_receipt(self) -> Optional[Mapping[str, Any]]:
        return None if self._seal is None else dict(self._seal)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            error: Optional[BaseException] = None
            try:
                payload = self._read_exact(self._size, self._digest.hexdigest())
                if self.retain_bytes_after_close:
                    self._retained_payload = payload
            except BaseException as exc:
                error = exc
            finally:
                os.close(self.descriptor)
                self._closed = True
            if error is not None:
                raise RecoveryIndeterminate(
                    "control evidence close-time verification failed: " + str(error)
                ) from error


def _record_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(dict(value))).hexdigest()


class _TranscriptBuilder:
    """Bounded canonical JSONL transcript with an exact previous-record chain."""

    def __init__(self) -> None:
        self._lines: List[bytes] = []
        self._previous_sha256 = "0" * 64

    def append(self, event: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if (
            type(event) is not str
            or re.fullmatch(r"[a-z0-9_]+", event) is None
            or len(self._lines) >= MAX_TRANSCRIPT_RECORDS
        ):
            _fail("privileged transcript event is invalid or exceeds its count bound")
        if type(payload) is not dict:
            _fail("privileged transcript payload is not a mapping")
        value = {
            "event": event,
            "event_index": len(self._lines),
            "payload": dict(payload),
            "previous_record_sha256": self._previous_sha256,
            "record_type": "cp2e_privileged_transcript_event",
            "schema_version": SCHEMA_VERSION,
        }
        encoded = canonical_json_bytes(value)
        if sum(len(line) for line in self._lines) + len(encoded) > MAX_TRANSCRIPT_BYTES:
            _fail("privileged transcript exceeds its byte bound")
        self._lines.append(encoded)
        self._previous_sha256 = hashlib.sha256(encoded).hexdigest()
        return value

    @property
    def payload(self) -> bytes:
        return b"".join(self._lines)

    @property
    def count(self) -> int:
        return len(self._lines)

    @property
    def first_sha256(self) -> Optional[str]:
        return None if not self._lines else hashlib.sha256(self._lines[0]).hexdigest()

    @property
    def last_sha256(self) -> Optional[str]:
        return None if not self._lines else self._previous_sha256


@dataclass(frozen=True)
class VerifiedTranscript:
    events: Tuple[Mapping[str, Any], ...]
    terminal: Mapping[str, Any]


def _exact_keys(value: Any, expected: Sequence[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != set(expected):
        raise ProtocolError(label + " keys differ from the frozen schema")
    return value


def _exact_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ProtocolError(label + " is not a JSON boolean")
    return value


def _foreign_affinity_eligibility_rows(
    value: Any, label: str,
) -> Tuple[Mapping[str, Any], ...]:
    """Validate canonical, non-gating foreign affinity observations."""

    if type(value) is not list or len(value) > MAX_CONTAINER_ITEMS:
        raise ProtocolError(label + " is not a bounded row population")
    retained: List[Mapping[str, Any]] = []
    previous_tid = 0
    for raw in value:
        row = _exact_keys(raw, (
            "effective_affinity_cpu_ids", "process_start_time_ticks", "tid",
        ), label + " row")
        try:
            tid = _require_u64(row["tid"], label + " TID")
            start = _require_u64(
                row["process_start_time_ticks"], label + " process start time",
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        affinity = row["effective_affinity_cpu_ids"]
        if (
            tid == 0 or tid <= previous_tid or type(affinity) is not list
            or not affinity or len(affinity) > MAX_CONTAINER_ITEMS
            or any(
                type(cpu) is not int or cpu < 0 or cpu > 1_048_575
                for cpu in affinity
            )
            or affinity != sorted(set(affinity))
        ):
            raise ProtocolError(label + " row identity/affinity/order differs")
        retained.append({
            "effective_affinity_cpu_ids": list(affinity),
            "process_start_time_ticks": start,
            "tid": tid,
        })
        previous_tid = tid
    return tuple(retained)


_CONTROL_PROCESS_KEYS = (
    "executable_device", "executable_inode", "parent_pid", "pid",
    "process_gid", "process_uid", "start_time_ticks",
)
_SCHEDULER_THREAD_KEYS = (
    "affinity_cpu_ids", "cpuset_membership", "process_id",
    "start_time_ticks", "tid",
)
_SCHEDULER_WITNESS_KEYS = (
    "child_cpuset_members", "complete_control_plane_processes",
    "complete_control_plane_threads", "complete_descendant_population",
    "complete_descendant_threads", "complete_descendant_tid_population",
    "foreign_affinity_eligibility",
)


def _positive_sorted_population(value: Any, label: str) -> Tuple[int, ...]:
    if (
        type(value) is not list or not value
        or len(value) > MAX_GUARDIAN_POPULATION_ITEMS
        or any(type(item) is not int or item <= 0 for item in value)
        or value != sorted(set(value))
    ):
        raise ProtocolError(label + " is not a bounded sorted unique population")
    return tuple(value)


def _control_process_population(
    value: Any, expected_peer: PeerCredentials, label: str,
) -> Tuple[Mapping[str, Any], ...]:
    if (
        type(value) is not list or not value
        or len(value) > MAX_GUARDIAN_POPULATION_ITEMS
    ):
        raise ProtocolError(label + " is not a bounded nonempty chain")
    rows: List[Mapping[str, Any]] = []
    parent = expected_peer.pid
    pids = []
    for raw in value:
        row = _exact_keys(raw, _CONTROL_PROCESS_KEYS, label + " row")
        retained = {}
        for key in _CONTROL_PROCESS_KEYS:
            try:
                retained[key] = _require_u64(row[key], label + " " + key)
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
        if (
            retained["pid"] == 0 or retained["parent_pid"] != parent
            or retained["process_uid"] != 0
            or retained["process_gid"] != 0
        ):
            raise ProtocolError(label + " PPID/root-credential chain differs")
        parent = retained["pid"]
        pids.append(parent)
        rows.append(retained)
    if len(pids) != len(set(pids)):
        raise ProtocolError(label + " contains duplicate process IDs")
    return tuple(rows)


def _scheduler_thread_population(
    value: Any, process_ids: Sequence[int], label: str,
) -> Tuple[Mapping[str, Any], ...]:
    process_set = set(process_ids)
    if (
        type(value) is not list or not value
        or len(value) > MAX_GUARDIAN_POPULATION_ITEMS
    ):
        raise ProtocolError(label + " is not a bounded nonempty population")
    rows: List[Mapping[str, Any]] = []
    for raw in value:
        row = _exact_keys(raw, _SCHEDULER_THREAD_KEYS, label + " row")
        try:
            tid = _require_u64(row["tid"], label + " TID")
            process_id = _require_u64(
                row["process_id"], label + " process ID",
            )
            started = _require_u64(
                row["start_time_ticks"], label + " start time",
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        affinity = row["affinity_cpu_ids"]
        membership = row["cpuset_membership"]
        if (
            tid == 0 or process_id not in process_set
            or type(affinity) is not list or not affinity
            or len(affinity) > MAX_GUARDIAN_POPULATION_ITEMS
            or any(type(cpu) is not int or cpu < 0 for cpu in affinity)
            or affinity != sorted(set(affinity))
            or type(membership) is not str or not membership.startswith("/")
            or os.path.normpath(membership) != membership
            or len(membership.encode("utf-8")) > 256
        ):
            raise ProtocolError(label + " identity/affinity/membership differs")
        rows.append({
            "affinity_cpu_ids": list(affinity),
            "cpuset_membership": membership,
            "process_id": process_id,
            "start_time_ticks": started,
            "tid": tid,
        })
    tids = [item["tid"] for item in rows]
    if (
        tids != sorted(set(tids))
        or {item["process_id"] for item in rows} != process_set
        or any(
            sum(
                item["process_id"] == pid and item["tid"] == pid
                for item in rows
            ) != 1
            for pid in process_set
        )
    ):
        raise ProtocolError(
            label + " is unordered/duplicate or violates the process-leader join"
        )
    return tuple(rows)


def _validated_scheduler_witness(
    payload: Mapping[str, Any], expected_peer: PeerCredentials,
) -> Mapping[str, Any]:
    witness = {
        key: payload[key] for key in _SCHEDULER_WITNESS_KEYS
    }
    descendants = _positive_sorted_population(
        witness["complete_descendant_population"],
        "guardian descendant process population",
    )
    descendant_tids = _positive_sorted_population(
        witness["complete_descendant_tid_population"],
        "guardian descendant TID population",
    )
    child_members = _positive_sorted_population(
        witness["child_cpuset_members"], "guardian child cpuset members",
    )
    if expected_peer.pid not in descendants or child_members != descendant_tids:
        raise ProtocolError("guardian estimator peer/cpuset population differs")
    descendant_threads = _scheduler_thread_population(
        witness["complete_descendant_threads"], descendants,
        "guardian descendant threads",
    )
    if tuple(item["tid"] for item in descendant_threads) != descendant_tids:
        raise ProtocolError("guardian descendant thread/TID join differs")
    control_processes = _control_process_population(
        witness["complete_control_plane_processes"], expected_peer,
        "guardian control-plane processes",
    )
    control_pids = tuple(item["pid"] for item in control_processes)
    control_threads = _scheduler_thread_population(
        witness["complete_control_plane_threads"], control_pids,
        "guardian control-plane threads",
    )
    control_tids = {item["tid"] for item in control_threads}
    if control_tids.intersection(descendant_tids):
        raise ProtocolError("guardian estimator/control TID partitions overlap")
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
        raise ProtocolError("guardian estimator/control scheduler roles overlap")
    foreign = _foreign_affinity_eligibility_rows(
        witness["foreign_affinity_eligibility"],
        "guardian foreign affinity eligibility",
    )
    if any(
        item["tid"] in set(descendant_tids).union(control_tids)
        for item in foreign
    ):
        raise ProtocolError("guardian foreign rows overlap controlled TIDs")
    try:
        encoded = _bounded_canonical_json_bytes(
            witness, MAX_GUARDIAN_LINE_BYTES, "guardian scheduler witness",
        )
        expected_sha256 = _require_hash(
            payload["scheduler_witness_sha256"],
            "guardian scheduler witness digest",
        )
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if hashlib.sha256(encoded).hexdigest() != expected_sha256:
        raise ProtocolError("guardian scheduler witness digest differs")
    return witness


def _exact_nullable_hash(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    try:
        return _require_hash(value, label)
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc


def source_specification_record(
    source_id: str, measurement_kind: str, source_kind: str, source_path: str,
    extraction: str, cpu_id: Optional[int], register: Optional[int],
    profile_source: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Canonical digest input independently derivable from schema-v2 profile data."""

    value = {
        "cpu_id": cpu_id,
        "extraction": extraction,
        "measurement_kind": measurement_kind,
        "profile_source": dict(profile_source) if type(profile_source) is dict else profile_source,
        "register": register,
        "source_id": source_id,
        "source_kind": source_kind,
        "source_path": source_path,
    }
    canonical_json_bytes(value)
    return value


def source_specification_sha256(value: Mapping[str, Any]) -> str:
    expected = {
        "cpu_id", "extraction", "measurement_kind", "profile_source", "register",
        "source_id", "source_kind", "source_path",
    }
    if type(value) is not dict or set(value) != expected or type(value["profile_source"]) is not dict:
        _fail("canonical source specification keys differ")
    return _record_sha256(value)


def _validate_raw_observation_receipt(
    value: Any, slot: FrozenRunSlot, phase: str,
) -> Mapping[str, Any]:
    receipt = _exact_keys(value, (
        "phase", "run_index", "schema_version", "snapshot_ended_monotonic_ns",
        "snapshot_skew_ns", "snapshot_started_monotonic_ns", "sources",
    ), "raw observation receipt")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != SCHEMA_VERSION:
        raise ProtocolError("raw observation receipt schema version differs")
    try:
        run_index = _require_u64(receipt["run_index"], "raw observation run index")
        started = _require_u64(
            receipt["snapshot_started_monotonic_ns"], "snapshot start",
        )
        ended = _require_u64(
            receipt["snapshot_ended_monotonic_ns"], "snapshot end",
        )
        skew = _require_u64(receipt["snapshot_skew_ns"], "snapshot skew")
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if run_index != slot.run_index or receipt["phase"] != phase:
        raise ProtocolError("raw observation receipt slot or phase differs")
    if ended < started or skew != ended - started:
        raise ProtocolError("raw observation snapshot interval/skew differs")
    sources = receipt["sources"]
    if type(sources) is not list or not sources or len(sources) > 256:
        raise ProtocolError("raw observation source population is empty or oversized")
    source_ids: List[str] = []
    identities: List[Tuple[Any, str]] = []
    for source in sources:
        row = _exact_keys(source, (
            "cpu_id", "extraction", "measurement_kind", "parsed_value",
            "raw_hex", "raw_sha256", "read_ended_monotonic_ns",
            "read_started_monotonic_ns", "register", "source_id", "source_kind",
            "source_path", "source_spec_sha256",
        ), "raw observation source")
        source_id = row["source_id"]
        if type(source_id) is not str or re.fullmatch(r"[a-z0-9_.-]{1,128}", source_id) is None:
            raise ProtocolError("raw observation source ID is invalid")
        source_ids.append(source_id)
        kind = row["measurement_kind"]
        if kind not in (
            "aperf", "mperf", "scaling_cur_freq", "amd_throttle", "f_ref",
            "temperature",
        ):
            raise ProtocolError("raw observation measurement kind differs")
        cpu_id = row["cpu_id"]
        if cpu_id is not None:
            try:
                _require_u64(cpu_id, "raw observation CPU ID")
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
        if kind in ("aperf", "mperf", "scaling_cur_freq", "amd_throttle") and cpu_id is None:
            raise ProtocolError("per-CPU raw observation lacks a CPU ID")
        if kind in ("f_ref", "temperature") and cpu_id is not None:
            raise ProtocolError("shared raw observation unexpectedly has a CPU ID")
        register = row["register"]
        if register is not None:
            try:
                _require_u64(register, "raw observation register")
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
        expected_register = 0xE8 if kind == "aperf" else 0xE7 if kind == "mperf" else None
        if register != expected_register or (register is not None and type(register) is not int):
            raise ProtocolError("raw observation register differs from its typed source")
        for key in ("extraction", "source_kind", "source_path"):
            if type(row[key]) is not str or not row[key] or len(row[key]) > 4096:
                raise ProtocolError("raw observation " + key + " is invalid")
        try:
            _require_hash(row["source_spec_sha256"], "raw source-spec digest")
            read_started = _require_u64(row["read_started_monotonic_ns"], "read start")
            read_ended = _require_u64(row["read_ended_monotonic_ns"], "read end")
            parsed = (
                _require_i64(row["parsed_value"], "raw parsed temperature")
                if kind == "temperature"
                else _require_u64(row["parsed_value"], "raw parsed integer")
            )
            _require_hash(row["raw_sha256"], "raw byte digest")
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        raw_hex = row["raw_hex"]
        if (
            type(raw_hex) is not str or len(raw_hex) > 2 * MAX_PACKET_BYTES
            or len(raw_hex) % 2 != 0 or re.fullmatch(r"[0-9a-f]*", raw_hex) is None
        ):
            raise ProtocolError("raw observation bytes are not bounded lowercase hex")
        raw = bytes.fromhex(raw_hex)
        if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
            raise ProtocolError("raw observation byte digest differs")
        expected_source_shape = {
            "aperf": ("msr_u64_le", "full_u64_little_endian_pread"),
            "mperf": ("msr_u64_le", "full_u64_little_endian_pread"),
            "scaling_cur_freq": (
                "text_integer_khz", "canonical_ascii_decimal_u64_one_line",
            ),
            "f_ref": (
                "text_integer_khz", "canonical_ascii_decimal_u64_one_line",
            ),
            "temperature": (
                "k10temp_text_integer", "canonical_ascii_decimal_i64_one_line",
            ),
        }
        if kind in expected_source_shape and (
            row["source_kind"], row["extraction"]
        ) != expected_source_shape[kind]:
            raise ProtocolError("raw observation source kind/extraction differs")
        if kind == "amd_throttle" and (
            (row["source_kind"], row["extraction"])
            not in (
                ("msr_u64_le", "full_u64_little_endian_pread"),
                ("text_u64", "canonical_ascii_decimal_u64_one_line"),
            )
        ):
            raise ProtocolError("raw throttle source kind/extraction differs")
        if row["extraction"] == "full_u64_little_endian_pread":
            if len(raw) != 8:
                raise ProtocolError("raw u64 MSR receipt is not exactly eight bytes")
            extracted = int.from_bytes(raw, "little")
        elif row["extraction"] == "canonical_ascii_decimal_u64_one_line":
            if re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", raw) is None:
                raise ProtocolError("raw unsigned text receipt is not canonical")
            extracted = int(raw[:-1])
            if extracted > U64_MAX:
                raise ProtocolError("raw unsigned text receipt exceeds u64")
        elif row["extraction"] == "canonical_ascii_decimal_i64_one_line":
            if re.fullmatch(rb"(?:0|-?[1-9][0-9]*)\n", raw) is None:
                raise ProtocolError("raw signed text receipt is not canonical")
            extracted = int(raw[:-1])
            if extracted < I64_MIN or extracted > I64_MAX:
                raise ProtocolError("raw signed text receipt exceeds i64")
        else:
            raise ProtocolError("raw observation extraction is unsupported")
        if extracted != parsed:
            raise ProtocolError("raw observation parsed integer differs from exact bytes")
        if read_started < started or read_ended < read_started or read_ended > ended:
            raise ProtocolError("raw observation read interval escapes its snapshot")
        if kind == "f_ref" and parsed == 0:
            raise ProtocolError("raw observation reference frequency is zero")
        expected_source_id = (
            "shared." + kind
            if cpu_id is None
            else "cpu{}.{}".format(cpu_id, kind)
        )
        if source_id != expected_source_id:
            raise ProtocolError("raw observation source ID differs from its typed identity")
        identities.append((cpu_id, kind))
    if source_ids != sorted(set(source_ids)) or len(identities) != len(set(identities)):
        raise ProtocolError("raw observation source identities are not sorted and unique")
    cpu_population = sorted(set(
        cpu_id for cpu_id, kind in identities if kind == "aperf"
    ))
    if not cpu_population or set(identities) != set(
        (cpu_id, kind)
        for cpu_id in cpu_population
        for kind in ("aperf", "mperf", "scaling_cur_freq", "amd_throttle")
    ).union({(None, "f_ref"), (None, "temperature")}):
        raise ProtocolError("raw observation typed source population is incomplete")
    return receipt


@dataclass(frozen=True)
class VerifiedGuardianEvidence:
    records: Tuple[Mapping[str, Any], ...]
    seal: Mapping[str, Any]


def _guardian_typed_telemetry(
    value: Any, started: int, ended: int,
) -> Tuple[Mapping[str, Tuple[Any, ...]], Mapping[str, int]]:
    if type(value) is not list or len(value) < 2 or len(value) > 256:
        raise ProtocolError("guardian typed-telemetry population differs")
    bindings: Dict[str, Tuple[Any, ...]] = {}
    parsed_values: Dict[str, int] = {}
    temperature_count = 0
    throttle_count = 0
    for item in value:
        row = _exact_keys(item, (
            "cpu_id", "extraction", "measurement_kind", "parsed_value",
            "raw_hex", "raw_sha256", "read_ended_monotonic_ns",
            "read_started_monotonic_ns", "register", "source_id", "source_kind",
            "source_path", "source_spec_sha256",
        ), "guardian typed-telemetry source")
        kind = row["measurement_kind"]
        cpu_id = row["cpu_id"]
        if kind == "temperature":
            temperature_count += 1
            if cpu_id is not None:
                raise ProtocolError("guardian temperature unexpectedly has a CPU ID")
            expected_id = "shared.temperature"
            expected_shape = (
                "k10temp_text_integer", "canonical_ascii_decimal_i64_one_line",
            )
        elif kind == "amd_throttle":
            throttle_count += 1
            try:
                _require_u64(cpu_id, "guardian throttle CPU ID")
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
            expected_id = "cpu{}.amd_throttle".format(cpu_id)
            expected_shape = None
        else:
            raise ProtocolError("guardian typed telemetry is not temperature/throttle")
        source_id = row["source_id"]
        if source_id != expected_id:
            raise ProtocolError("guardian typed-telemetry source ID differs")
        if source_id in bindings:
            raise ProtocolError("guardian typed-telemetry source ID is duplicated")
        register = row["register"]
        if register is not None:
            try:
                _require_u64(register, "guardian typed-telemetry register")
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
        for key in ("extraction", "source_kind", "source_path"):
            if type(row[key]) is not str or not row[key] or len(row[key]) > 4096:
                raise ProtocolError("guardian typed-telemetry " + key + " is invalid")
        if expected_shape is not None and (
            row["source_kind"], row["extraction"]
        ) != expected_shape:
            raise ProtocolError("guardian temperature source shape differs")
        if kind == "amd_throttle" and (
            (row["source_kind"], row["extraction"])
            not in (
                ("msr_u64_le", "full_u64_little_endian_pread"),
                ("text_u64", "canonical_ascii_decimal_u64_one_line"),
            )
        ):
            raise ProtocolError("guardian throttle source shape differs")
        expected_register = None
        if row["source_kind"] == "msr_u64_le" and kind == "amd_throttle":
            if register is None:
                raise ProtocolError("guardian MSR throttle register is absent")
            expected_register = register
        if register != expected_register:
            raise ProtocolError("guardian typed-telemetry register differs")
        try:
            _require_hash(row["source_spec_sha256"], "guardian source-spec digest")
            _require_hash(row["raw_sha256"], "guardian raw-byte digest")
            read_start = _require_u64(row["read_started_monotonic_ns"], "guardian read start")
            read_end = _require_u64(row["read_ended_monotonic_ns"], "guardian read end")
            parsed = (
                _require_i64(row["parsed_value"], "guardian parsed temperature")
                if kind == "temperature"
                else _require_u64(row["parsed_value"], "guardian parsed throttle")
            )
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        if read_start < started or read_end < read_start or read_end > ended:
            raise ProtocolError("guardian typed-telemetry read interval escapes its row")
        raw_hex = row["raw_hex"]
        if (
            type(raw_hex) is not str or len(raw_hex) > 2 * MAX_PACKET_BYTES
            or len(raw_hex) % 2 or re.fullmatch(r"[0-9a-f]*", raw_hex) is None
        ):
            raise ProtocolError("guardian raw bytes are not bounded lowercase hex")
        raw = bytes.fromhex(raw_hex)
        if hashlib.sha256(raw).hexdigest() != row["raw_sha256"]:
            raise ProtocolError("guardian raw-byte digest differs")
        if row["extraction"] == "full_u64_little_endian_pread":
            if len(raw) != 8:
                raise ProtocolError("guardian MSR value is not eight bytes")
            extracted = int.from_bytes(raw, "little")
        elif row["extraction"] == "canonical_ascii_decimal_u64_one_line":
            if re.fullmatch(rb"(?:0|[1-9][0-9]*)\n", raw) is None:
                raise ProtocolError("guardian unsigned text is noncanonical")
            extracted = int(raw[:-1])
            if extracted > U64_MAX:
                raise ProtocolError("guardian unsigned text exceeds u64")
        elif row["extraction"] == "canonical_ascii_decimal_i64_one_line":
            if re.fullmatch(rb"(?:0|-?[1-9][0-9]*)\n", raw) is None:
                raise ProtocolError("guardian signed text is noncanonical")
            extracted = int(raw[:-1])
            if extracted < I64_MIN or extracted > I64_MAX:
                raise ProtocolError("guardian signed text exceeds i64")
        else:
            raise ProtocolError("guardian extraction is unsupported")
        if extracted != parsed:
            raise ProtocolError("guardian parsed value differs from exact bytes")
        bindings[source_id] = (
            kind, row["source_kind"], row["source_path"],
            row["source_spec_sha256"], row["extraction"], cpu_id, register,
        )
        parsed_values[source_id] = parsed
    if temperature_count != 1 or throttle_count == 0 or list(bindings) != sorted(bindings):
        raise ProtocolError("guardian typed telemetry is incomplete or unordered")
    return bindings, parsed_values


def verify_guardian_evidence_bytes(
    evidence_jsonl: bytes, value: Any, expected_peer: PeerCredentials,
    session_id: str, expected_contract: GuardianEvidenceContract,
) -> VerifiedGuardianEvidence:
    """Replay the complete streamed guardian chain without a host backend."""

    if type(expected_contract) is not GuardianEvidenceContract:
        raise ProtocolError("expected guardian evidence contract is not frozen")
    if type(expected_peer) is not PeerCredentials:
        raise ProtocolError("expected guardian peer identity is invalid")
    try:
        _require_hash(session_id, "expected guardian session ID")
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if type(evidence_jsonl) is not bytes or len(evidence_jsonl) > expected_contract.maximum_evidence_bytes:
        raise ProtocolError("guardian evidence byte domain differs")
    seal = _exact_keys(value, (
        "bound_peer", "coverage_ended_monotonic_ns", "coverage_started_monotonic_ns",
        "drift", "evidence_sha256", "evidence_size_bytes",
        "final_child_cpuset_member_count", "final_control_plane_process_count",
        "final_control_plane_tid_count", "final_descendant_process_count",
        "final_descendant_tid_count",
        "final_foreign_affinity_eligibility_count",
        "final_foreign_affinity_eligibility_sha256",
        "final_scheduler_witness_sha256", "first_record_sha256",
        "irq_population_changed", "last_record_sha256",
        "maximum_observation_gap_ns", "observation_count", "required_surfaces",
        "schema_version", "session_id", "thermal_or_throttle_event",
    ), "population guardian seal")
    if (
        type(seal["schema_version"]) is not int
        or seal["schema_version"] != SCHEMA_VERSION
        or seal["session_id"] != session_id
        or PeerCredentials.from_record(seal["bound_peer"]) != expected_peer
        or seal["required_surfaces"] != list(expected_contract.required_surfaces)
        or seal["maximum_observation_gap_ns"]
        != expected_contract.maximum_observation_gap_ns
    ):
        raise ProtocolError("population guardian seal identity/contract differs")
    try:
        coverage_start = _require_u64(seal["coverage_started_monotonic_ns"], "guardian coverage start")
        coverage_end = _require_u64(seal["coverage_ended_monotonic_ns"], "guardian coverage end")
        observation_count = _require_u64(seal["observation_count"], "guardian observation count")
        evidence_size = _require_u64(seal["evidence_size_bytes"], "guardian evidence size")
        _require_hash(seal["evidence_sha256"], "guardian evidence digest")
        _require_hash(seal["first_record_sha256"], "guardian first-record digest")
        _require_hash(seal["last_record_sha256"], "guardian last-record digest")
        _require_hash(
            seal["final_foreign_affinity_eligibility_sha256"],
            "guardian final foreign-affinity digest",
        )
        _require_hash(
            seal["final_scheduler_witness_sha256"],
            "guardian final scheduler-witness digest",
        )
        for key in (
            "final_child_cpuset_member_count",
            "final_control_plane_process_count", "final_control_plane_tid_count",
            "final_descendant_process_count", "final_descendant_tid_count",
            "final_foreign_affinity_eligibility_count",
        ):
            _require_u64(seal[key], "guardian " + key)
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    lines = evidence_jsonl.splitlines(keepends=True)
    if (
        not lines or b"".join(lines) != evidence_jsonl
        or any(len(line) > expected_contract.maximum_line_bytes for line in lines)
        or len(lines) > expected_contract.maximum_observation_count
        or observation_count != len(lines) or evidence_size != len(evidence_jsonl)
        or seal["evidence_sha256"] != hashlib.sha256(evidence_jsonl).hexdigest()
        or seal["first_record_sha256"] != hashlib.sha256(lines[0]).hexdigest()
    ):
        raise ProtocolError("guardian evidence count/size/file digest differs")
    if coverage_end < coverage_start or coverage_end - coverage_start > expected_contract.maximum_campaign_duration_ns:
        raise ProtocolError("guardian coverage duration differs")
    previous_hash = "0" * 64
    previous_start: Optional[int] = None
    previous_end = coverage_start
    frozen_sources: Optional[Mapping[str, Tuple[Any, ...]]] = None
    previous_throttles: Optional[Mapping[str, int]] = None
    frozen_irq_sha256: Optional[str] = None
    records: List[Mapping[str, Any]] = []
    final_witness = None
    final_foreign_affinity_eligibility = None
    exact_keys = GuardianEvidenceWriter.PAYLOAD_KEYS.union({
        "previous_record_sha256", "record_type", "schema_version", "sequence",
    })
    for index, line in enumerate(lines):
        row = _strict_bounded_json_bytes(
            line, expected_contract.maximum_line_bytes,
            "guardian evidence record",
        )
        if set(row) != exact_keys:
            raise ProtocolError("guardian observation keys differ")
        if (
            row["record_type"] != "cp2e_guardian_observation"
            or type(row["schema_version"]) is not int
            or row["schema_version"] != SCHEMA_VERSION
            or type(row["sequence"]) is not int or row["sequence"] != index
            or row["previous_record_sha256"] != previous_hash
        ):
            raise ProtocolError("guardian observation identity/hash chain differs")
        try:
            started = _require_u64(row["started_monotonic_ns"], "guardian observation start")
            ended = _require_u64(row["ended_monotonic_ns"], "guardian observation end")
            _require_hash(row["irq_population_sha256"], "guardian IRQ population digest")
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        if (
            ended < started
            or ended - started > expected_contract.maximum_observation_gap_ns
            or started < previous_end
            or started - previous_end > expected_contract.maximum_observation_gap_ns
            or (previous_start is not None and started - previous_start < expected_contract.poll_interval_ns)
        ):
            raise ProtocolError("guardian observation cadence/gap/duration differs")
        witness = _validated_scheduler_witness(row, expected_peer)
        descendants = witness["complete_descendant_population"]
        descendant_tids = witness["complete_descendant_tid_population"]
        foreign = _foreign_affinity_eligibility_rows(
            witness["foreign_affinity_eligibility"],
            "guardian foreign affinity eligibility",
        )
        if (
            _exact_bool(row["drift"], "guardian drift")
            or _exact_bool(row["thermal_or_throttle_event"], "guardian thermal event")
        ):
            raise ProtocolError("guardian task/drift state differs")
        surfaces = row["surface_states"]
        if type(surfaces) is not list or len(surfaces) != len(expected_contract.required_surfaces):
            raise ProtocolError("guardian surface-state population differs")
        surface_ids = []
        for state in surfaces:
            item = _exact_keys(state, ("passed", "state_sha256", "surface_id"), "guardian surface state")
            if not _exact_bool(item["passed"], "guardian surface pass"):
                raise ProtocolError("guardian surface did not pass")
            try:
                _require_hash(item["state_sha256"], "guardian surface-state digest")
            except PrivilegedHelperError as exc:
                raise ProtocolError(str(exc)) from exc
            surface_ids.append(item["surface_id"])
        if surface_ids != list(expected_contract.required_surfaces):
            raise ProtocolError("guardian required-surface coverage differs")
        source_bindings, parsed = _guardian_typed_telemetry(
            row["typed_telemetry"], started, ended,
        )
        if frozen_sources is None:
            frozen_sources = source_bindings
        elif source_bindings != frozen_sources:
            raise ProtocolError("guardian typed source binding changed")
        throttles = {
            source_id: parsed[source_id] for source_id, binding in source_bindings.items()
            if binding[0] == "amd_throttle"
        }
        if previous_throttles is not None and any(
            value != previous_throttles[source_id] for source_id, value in throttles.items()
        ):
            raise ProtocolError("guardian throttle counter changed")
        previous_throttles = throttles
        if frozen_irq_sha256 is None:
            frozen_irq_sha256 = row["irq_population_sha256"]
        elif row["irq_population_sha256"] != frozen_irq_sha256:
            raise ProtocolError("guardian IRQ population changed")
        previous_hash = hashlib.sha256(line).hexdigest()
        previous_start, previous_end = started, ended
        final_witness = witness
        final_foreign_affinity_eligibility = [dict(item) for item in foreign]
        records.append(row)
    if (
        coverage_start > records[0]["started_monotonic_ns"]
        or records[0]["started_monotonic_ns"] - coverage_start
        > expected_contract.maximum_observation_gap_ns
        or coverage_end < previous_end
        or coverage_end - previous_end > expected_contract.maximum_observation_gap_ns
        or previous_hash != seal["last_record_sha256"]
        or final_witness is None
        or seal["final_child_cpuset_member_count"]
        != len(final_witness["child_cpuset_members"])
        or seal["final_control_plane_process_count"]
        != len(final_witness["complete_control_plane_processes"])
        or seal["final_control_plane_tid_count"]
        != len(final_witness["complete_control_plane_threads"])
        or seal["final_descendant_process_count"]
        != len(final_witness["complete_descendant_population"])
        or seal["final_descendant_tid_count"]
        != len(final_witness["complete_descendant_tid_population"])
        or seal["final_foreign_affinity_eligibility_count"]
        != len(final_foreign_affinity_eligibility)
        or seal["final_foreign_affinity_eligibility_sha256"]
        != hashlib.sha256(_bounded_canonical_json_bytes(
            {"rows": final_foreign_affinity_eligibility},
            expected_contract.maximum_line_bytes,
            "terminal foreign-affinity witness",
        )).hexdigest()
        or seal["final_scheduler_witness_sha256"]
        != records[-1]["scheduler_witness_sha256"]
        or _exact_bool(seal["drift"], "terminal guardian drift")
        or _exact_bool(seal["irq_population_changed"], "terminal guardian IRQ change")
        or _exact_bool(seal["thermal_or_throttle_event"], "terminal guardian thermal event")
    ):
        raise ProtocolError("population guardian terminal coverage/state differs")
    return VerifiedGuardianEvidence(tuple(records), seal)


@dataclass(frozen=True)
class VerifiedControlEvidence:
    records: Tuple[Mapping[str, Any], ...]
    seal: Mapping[str, Any]


_CONTROL_ROW_RECEIPT_KEYS = (
    "end_offset_bytes", "phase", "record_sha256", "sequence", "state_sha256",
)


def verify_control_evidence_bytes(
    evidence_jsonl: bytes, value: Any, expected_binding: Binding,
    expected_session_id: str, expected_journal_id: str,
) -> VerifiedControlEvidence:
    """Replay a sealed helper-owned full control-state stream."""

    if (
        type(evidence_jsonl) is not bytes
        or not evidence_jsonl
        or len(evidence_jsonl) > MAX_CONTROL_EVIDENCE_BYTES
    ):
        raise ProtocolError("control evidence byte domain differs")
    if type(expected_binding) is not Binding:
        raise ProtocolError("expected control evidence binding is not frozen")
    try:
        _require_hash(expected_session_id, "expected control evidence session ID")
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if (
        type(expected_journal_id) is not str
        or re.fullmatch(r"[0-9a-f]{32}", expected_journal_id) is None
    ):
        raise ProtocolError("expected control evidence journal ID differs")
    seal = _exact_keys(value, (
        "binding_digest", "device", "evidence_sha256", "evidence_size_bytes",
        "first_record_sha256", "inode", "journal_id", "last_record_sha256",
        "phases", "record_type", "restoration_matches_prior", "row_count",
        "rows", "schema_version", "session_id",
    ), "control evidence terminal seal")
    if (
        seal["record_type"] != "cp2e_control_evidence_terminal"
        or type(seal["schema_version"]) is not int
        or seal["schema_version"] != SCHEMA_VERSION
        or seal["binding_digest"] != expected_binding.digest
        or seal["session_id"] != expected_session_id
        or seal["journal_id"] != expected_journal_id
    ):
        raise ProtocolError("control evidence seal identity differs")
    try:
        device = _require_u64(seal["device"], "control evidence device")
        inode = _require_u64(seal["inode"], "control evidence inode")
        count = _require_u64(seal["row_count"], "control evidence row count")
        size = _require_u64(seal["evidence_size_bytes"], "control evidence size")
        _require_hash(seal["evidence_sha256"], "control evidence digest")
        _require_hash(seal["first_record_sha256"], "control first-record digest")
        _require_hash(seal["last_record_sha256"], "control last-record digest")
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    del device, inode
    phases = seal["phases"]
    if (
        type(phases) is not list
        or tuple(phases) not in (
            ControlEvidenceWriter._SUCCESS_PHASES,
            ControlEvidenceWriter._FAIL_CLOSED_PHASES,
        )
        or not _exact_bool(
            seal["restoration_matches_prior"],
            "control evidence restoration comparison",
        )
    ):
        raise ProtocolError("control evidence sealed phase/restoration state differs")
    lines = evidence_jsonl.splitlines(keepends=True)
    if (
        b"".join(lines) != evidence_jsonl
        or any(len(line) > MAX_CONTROL_EVIDENCE_LINE_BYTES for line in lines)
        or len(lines) > MAX_CONTROL_EVIDENCE_ROWS
        or count != len(lines)
        or size != len(evidence_jsonl)
        or len(phases) != len(lines)
        or seal["evidence_sha256"] != hashlib.sha256(evidence_jsonl).hexdigest()
    ):
        raise ProtocolError("control evidence count/size/file digest differs")
    seal_rows = seal["rows"]
    if type(seal_rows) is not list or len(seal_rows) != len(lines):
        raise ProtocolError("control evidence row-receipt population differs")
    previous = "0" * 64
    offset = 0
    records: List[Mapping[str, Any]] = []
    state_sha256: Dict[str, str] = {}
    exact_row_keys = {
        "binding_digest", "journal_id", "phase", "previous_record_sha256",
        "record_type", "schema_version", "sequence", "session_id", "state",
        "state_sha256",
    }
    for sequence, line in enumerate(lines):
        row = _strict_bounded_json_bytes(
            line, MAX_CONTROL_EVIDENCE_LINE_BYTES, "control evidence row",
        )
        if set(row) != exact_row_keys:
            raise ProtocolError("control evidence row keys differ")
        phase = phases[sequence]
        if (
            row["record_type"] != "cp2e_control_state"
            or type(row["schema_version"]) is not int
            or row["schema_version"] != SCHEMA_VERSION
            or type(row["sequence"]) is not int
            or row["sequence"] != sequence
            or row["phase"] != phase
            or row["previous_record_sha256"] != previous
            or row["binding_digest"] != expected_binding.digest
            or row["session_id"] != expected_session_id
            or row["journal_id"] != expected_journal_id
            or type(row["state"]) is not dict
        ):
            raise ProtocolError("control evidence row identity/phase chain differs")
        try:
            state_bytes = canonical_control_state_bytes(row["state"])
            _require_hash(row["state_sha256"], "control state digest")
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        if hashlib.sha256(state_bytes).hexdigest() != row["state_sha256"]:
            raise ProtocolError("control evidence full-state digest differs")
        record_sha256 = hashlib.sha256(line).hexdigest()
        offset += len(line)
        receipt = _exact_keys(
            seal_rows[sequence], _CONTROL_ROW_RECEIPT_KEYS,
            "control evidence row receipt",
        )
        try:
            end_offset = _require_u64(
                receipt["end_offset_bytes"], "control row end offset",
            )
            receipt_sequence = _require_u64(
                receipt["sequence"], "control row receipt sequence",
            )
            _require_hash(receipt["record_sha256"], "control row digest")
            _require_hash(receipt["state_sha256"], "control row state digest")
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
        if (
            receipt["phase"] != phase
            or receipt_sequence != sequence
            or end_offset != offset
            or receipt["record_sha256"] != record_sha256
            or receipt["state_sha256"] != row["state_sha256"]
        ):
            raise ProtocolError("control evidence exact row receipt differs")
        previous = record_sha256
        state_sha256[phase] = row["state_sha256"]
        records.append(row)
    if (
        seal["first_record_sha256"] != hashlib.sha256(lines[0]).hexdigest()
        or seal["last_record_sha256"] != previous
        or state_sha256["prior"] != state_sha256["restored"]
    ):
        raise ProtocolError("control evidence terminal endpoints/restoration differ")
    return VerifiedControlEvidence(tuple(records), seal)


def _verify_control_checkpoint(
    value: Any, verified: VerifiedControlEvidence, evidence_jsonl: bytes,
) -> Mapping[str, Any]:
    checkpoint = _exact_keys(value, (
        "device", "evidence_sha256", "evidence_size_bytes", "inode",
        "last_record_sha256", "row_count", "rows",
    ), "control evidence checkpoint")
    try:
        count = _require_u64(checkpoint["row_count"], "control checkpoint row count")
        size = _require_u64(
            checkpoint["evidence_size_bytes"], "control checkpoint size",
        )
        _require_u64(checkpoint["device"], "control checkpoint device")
        _require_u64(checkpoint["inode"], "control checkpoint inode")
        _require_hash(checkpoint["evidence_sha256"], "control checkpoint digest")
        _require_hash(
            checkpoint["last_record_sha256"], "control checkpoint last digest",
        )
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    rows = checkpoint["rows"]
    seal = verified.seal
    if (
        type(rows) is not list or count == 0 or count > len(verified.records)
        or len(rows) != count or rows != seal["rows"][:count]
        or checkpoint["device"] != seal["device"]
        or checkpoint["inode"] != seal["inode"]
        or size != rows[-1]["end_offset_bytes"]
        or checkpoint["last_record_sha256"] != rows[-1]["record_sha256"]
        or hashlib.sha256(evidence_jsonl[:size]).hexdigest()
        != checkpoint["evidence_sha256"]
    ):
        raise ProtocolError("control evidence checkpoint differs from its sealed prefix")
    return checkpoint


def verify_transcript_bytes(
    transcript_jsonl: bytes, terminal_receipt_json: bytes,
    expected_binding: Binding, expected_run_slots: Sequence[FrozenRunSlot],
    guardian_evidence_jsonl: bytes,
    expected_guardian_contract: GuardianEvidenceContract,
    control_evidence_jsonl: Optional[bytes] = None,
) -> VerifiedTranscript:
    """Pure detached verification; this function never constructs a backend."""

    if type(transcript_jsonl) is not bytes or len(transcript_jsonl) > MAX_TRANSCRIPT_BYTES:
        raise ProtocolError("privileged transcript byte domain differs")
    slots = _frozen_run_slots(expected_run_slots)
    lines = transcript_jsonl.splitlines(keepends=True)
    if len(lines) > MAX_TRANSCRIPT_RECORDS or b"".join(lines) != transcript_jsonl:
        raise ProtocolError("privileged transcript record population differs")
    events: List[Mapping[str, Any]] = []
    previous = "0" * 64
    for index, line in enumerate(lines):
        value = strict_json_bytes(line)
        _exact_keys(value, (
            "event", "event_index", "payload", "previous_record_sha256",
            "record_type", "schema_version",
        ), "privileged transcript event")
        if (
            value["record_type"] != "cp2e_privileged_transcript_event"
            or type(value["schema_version"]) is not int
            or value["schema_version"] != SCHEMA_VERSION
            or type(value["event_index"]) is not int
            or value["event_index"] != index
            or value["previous_record_sha256"] != previous
            or type(value["payload"]) is not dict
        ):
            raise ProtocolError("privileged transcript identity/hash chain differs")
        previous = hashlib.sha256(line).hexdigest()
        events.append(value)
    terminal = strict_json_bytes(terminal_receipt_json)
    legacy_terminal_keys = (
        "abnormal_recovery", "binding", "binding_digest", "descriptor_closure",
        "error_type", "event_count", "first_record_sha256", "journal_id",
        "last_record_sha256", "population_seal_sha256", "population_stop_count",
        "record_type", "restoration_proved", "schema_version", "session_id",
        "terminal_journal_sha256", "terminal_status", "transcript_sha256",
        "transcript_size_bytes",
    )
    control_terminal_keys = legacy_terminal_keys + ("control_evidence_seal_sha256",)
    if set(terminal) == set(control_terminal_keys):
        has_control_evidence_schema = True
    elif set(terminal) == set(legacy_terminal_keys):
        # Detached schema-v2 fixtures predating the helper-owned stream remain
        # parseable, but HelperSession no longer emits this legacy shape.
        has_control_evidence_schema = False
    else:
        raise ProtocolError("privileged transcript terminal receipt keys differ from the frozen schema")
    if (
        terminal["record_type"] != "cp2e_privileged_transcript_terminal"
        or type(terminal["schema_version"]) is not int
        or terminal["schema_version"] != SCHEMA_VERSION
        or Binding.from_record(terminal["binding"]) != expected_binding
        or terminal["binding_digest"] != expected_binding.digest
    ):
        raise ProtocolError("privileged transcript terminal identity differs")
    try:
        count = _require_u64(terminal["event_count"], "terminal event count")
        size = _require_u64(terminal["transcript_size_bytes"], "terminal transcript size")
        stop_count = _require_u64(
            terminal["population_stop_count"], "terminal population-stop count",
        )
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc
    if count != len(events) or size != len(transcript_jsonl):
        raise ProtocolError("privileged transcript terminal count/size differs")
    if terminal["transcript_sha256"] != hashlib.sha256(transcript_jsonl).hexdigest():
        raise ProtocolError("privileged transcript whole-file digest differs")
    first = None if not lines else hashlib.sha256(lines[0]).hexdigest()
    last = None if not lines else previous
    if terminal["first_record_sha256"] != first or terminal["last_record_sha256"] != last:
        raise ProtocolError("privileged transcript terminal endpoint digest differs")
    nullable_hashes = [
        "first_record_sha256", "last_record_sha256", "population_seal_sha256",
        "terminal_journal_sha256",
    ]
    if has_control_evidence_schema:
        nullable_hashes.append("control_evidence_seal_sha256")
    for key in nullable_hashes:
        _exact_nullable_hash(terminal[key], "terminal " + key)
    if terminal["session_id"] is not None:
        try:
            _require_hash(terminal["session_id"], "terminal session ID")
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc
    if terminal["journal_id"] is not None and (
        type(terminal["journal_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{32}", terminal["journal_id"]) is None
    ):
        raise ProtocolError("terminal journal ID differs")
    descriptor = _exact_keys(
        terminal["descriptor_closure"], ("attempted", "error_type", "passed"),
        "descriptor-closure receipt",
    )
    attempted = _exact_bool(descriptor["attempted"], "descriptor closure attempted")
    passed = _exact_bool(descriptor["passed"], "descriptor closure passed")
    abnormal = _exact_bool(terminal["abnormal_recovery"], "terminal abnormal recovery")
    restored = _exact_bool(terminal["restoration_proved"], "terminal restoration proof")
    for value, label in ((descriptor["error_type"], "descriptor error type"),
                         (terminal["error_type"], "terminal error type")):
        if value is not None and (type(value) is not str or not value or len(value) > 192):
            raise ProtocolError(label + " is invalid")
    if terminal["terminal_status"] not in ("success", "fail_closed", "indeterminate"):
        raise ProtocolError("terminal status differs")

    expected_success_events = ["challenge", "hello", "apply"] + [
        "run_slot_observation" for _slot in slots for _phase in ("pre", "post")
    ] + ["restore"]
    event_names = [item["event"] for item in events]
    success_prefix = expected_success_events[:-1]
    has_restore = bool(event_names) and event_names[-1] == "restore"
    retained_prefix = event_names[:-1] if has_restore else event_names
    if retained_prefix != success_prefix[:len(retained_prefix)] or (
        "restore" in event_names and not has_restore
    ):
        raise ProtocolError("privileged transcript event order is not one frozen prefix")
    if terminal["terminal_status"] == "success":
        if (
            event_names != expected_success_events or len(events) != 16
            or abnormal or not restored or not attempted or not passed
            or stop_count != 1 or terminal["error_type"] is not None
            or descriptor["error_type"] is not None
            or (
                has_control_evidence_schema
                and terminal["control_evidence_seal_sha256"] is None
            )
        ):
            raise ProtocolError("successful privileged transcript terminal invariants differ")
    elif terminal["terminal_status"] == "fail_closed":
        if not restored or not attempted or not passed or terminal["error_type"] is None:
            raise ProtocolError("fail-closed privileged transcript lacks exact closure")
    elif restored and passed:
        raise ProtocolError("indeterminate privileged transcript claims exact closure")

    if len(events) >= 1:
        challenge = _exact_keys(events[0]["payload"], (
            "binding", "binding_digest", "challenge_nonce", "peer",
        ), "transcript challenge payload")
        if events[0]["event"] != "challenge" or Binding.from_record(challenge["binding"]) != expected_binding:
            raise ProtocolError("transcript challenge binding differs")
        if challenge["binding_digest"] != expected_binding.digest:
            raise ProtocolError("transcript challenge binding digest differs")
        if type(challenge["challenge_nonce"]) is not str or NONCE_PATTERN.fullmatch(challenge["challenge_nonce"]) is None:
            raise ProtocolError("transcript challenge nonce differs")
        peer = PeerCredentials.from_record(challenge["peer"])
    else:
        peer = None
    if len(events) >= 2:
        hello = _exact_keys(events[1]["payload"], (
            "binding", "challenge_nonce", "client_nonce", "nonce_sha256", "peer",
            "session_id",
        ), "transcript hello payload")
        if events[1]["event"] != "hello" or Binding.from_record(hello["binding"]) != expected_binding:
            raise ProtocolError("transcript hello binding differs")
        if PeerCredentials.from_record(hello["peer"]) != peer:
            raise ProtocolError("transcript SO_PEERCRED changes across handshake")
        for key in ("challenge_nonce", "client_nonce"):
            if type(hello[key]) is not str or NONCE_PATTERN.fullmatch(hello[key]) is None:
                raise ProtocolError("transcript hello nonce differs")
        if hello["challenge_nonce"] != challenge["challenge_nonce"]:
            raise ProtocolError("transcript challenge nonce is not echoed")
        expected_nonce = hashlib.sha256(
            bytes.fromhex(hello["challenge_nonce"]) + bytes.fromhex(hello["client_nonce"])
        ).hexdigest()
        material = {
            "binding_digest": expected_binding.digest,
            "challenge_nonce": hello["challenge_nonce"],
            "client_nonce": hello["client_nonce"],
            "peer": peer.as_record(),
        }
        expected_session = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        if hello["nonce_sha256"] != expected_nonce or hello["session_id"] != expected_session:
            raise ProtocolError("transcript nonce/session derivation differs")
        if terminal["session_id"] != expected_session:
            raise ProtocolError("terminal session ID differs from the transcript")

    if terminal["terminal_status"] == "success":
        verified_control: Optional[VerifiedControlEvidence] = None
        if has_control_evidence_schema:
            apply = _exact_keys(events[2]["payload"], (
                "applied_state_sha256", "control_evidence_checkpoint",
                "control_evidence_checkpoint_sha256", "journal_id",
                "population_binding", "population_binding_sha256",
                "population_state", "prior_state_sha256",
            ), "transcript apply payload")
            restore = _exact_keys(events[-1]["payload"], (
                "abnormal_recovery", "control_evidence_seal",
                "control_evidence_seal_sha256", "population_seal_sha256",
                "prior_state_sha256", "prior_state_sha256_matches",
                "restored_state_sha256", "terminal_journal_sha256",
            ), "transcript restore payload")
            if type(control_evidence_jsonl) is not bytes:
                raise ProtocolError("successful transcript lacks control evidence bytes")
            verified_control = verify_control_evidence_bytes(
                control_evidence_jsonl, restore["control_evidence_seal"],
                expected_binding, terminal["session_id"], terminal["journal_id"],
            )
            if tuple(verified_control.seal["phases"]) != ControlEvidenceWriter._SUCCESS_PHASES:
                raise ProtocolError("successful control evidence lacks exactly three states")
            checkpoint = _verify_control_checkpoint(
                apply["control_evidence_checkpoint"], verified_control,
                control_evidence_jsonl,
            )
            if (
                checkpoint["row_count"] != 2
                or [item["phase"] for item in checkpoint["rows"]]
                != ["prior", "applied"]
                or _record_sha256(checkpoint)
                != apply["control_evidence_checkpoint_sha256"]
                or apply["prior_state_sha256"]
                != checkpoint["rows"][0]["state_sha256"]
                or apply["applied_state_sha256"]
                != checkpoint["rows"][1]["state_sha256"]
                or _record_sha256(restore["control_evidence_seal"])
                != restore["control_evidence_seal_sha256"]
                or restore["control_evidence_seal_sha256"]
                != terminal["control_evidence_seal_sha256"]
            ):
                raise ProtocolError("transcript control evidence checkpoint/seal differs")
        else:
            apply = _exact_keys(events[2]["payload"], (
                "applied_state", "applied_state_sha256", "journal_id",
                "population_binding", "population_binding_sha256", "population_state",
                "prior_state", "prior_state_sha256",
            ), "transcript apply payload")
            for state_key, hash_key in (
                ("prior_state", "prior_state_sha256"),
                ("applied_state", "applied_state_sha256"),
            ):
                if type(apply[state_key]) is not dict or _record_sha256(apply[state_key]) != apply[hash_key]:
                    raise ProtocolError("transcript apply state digest differs")
        if (
            type(apply["population_binding"]) is not dict
            or _record_sha256(apply["population_binding"])
            != apply["population_binding_sha256"]
        ):
            raise ProtocolError("transcript population binding digest differs")
        if apply["journal_id"] != terminal["journal_id"]:
            raise ProtocolError("transcript journal identity differs")
        frozen_source_bindings: Optional[Mapping[str, Tuple[Any, ...]]] = None
        paired_pre_values: Dict[int, Mapping[str, int]] = {}
        snapshot_intervals: List[Tuple[int, int]] = []
        verified_guardian: Optional[VerifiedGuardianEvidence] = None
        for offset, slot in enumerate(slots):
            for phase_offset, phase in enumerate(("pre", "post")):
                event = events[3 + 2 * offset + phase_offset]
                payload = _exact_keys(event["payload"], (
                    "phase", "population", "population_seal", "population_seal_sha256",
                    "population_sha256", "receipt", "receipt_sha256", "run_slot",
                ), "transcript observation payload")
                if FrozenRunSlot.from_record(payload["run_slot"]) != slot or payload["phase"] != phase:
                    raise ProtocolError("transcript observation frozen slot/phase differs")
                if type(payload["population"]) is not dict or _record_sha256(payload["population"]) != payload["population_sha256"]:
                    raise ProtocolError("transcript observation population digest differs")
                receipt = _validate_raw_observation_receipt(payload["receipt"], slot, phase)
                snapshot_intervals.append((
                    receipt["snapshot_started_monotonic_ns"],
                    receipt["snapshot_ended_monotonic_ns"],
                ))
                if _record_sha256(receipt) != payload["receipt_sha256"]:
                    raise ProtocolError("transcript observation receipt digest differs")
                source_bindings = {
                    row["source_id"]: (
                        row["measurement_kind"], row["source_kind"],
                        row["source_path"], row["source_spec_sha256"],
                        row["extraction"], row["cpu_id"], row["register"],
                    )
                    for row in receipt["sources"]
                }
                if frozen_source_bindings is None:
                    frozen_source_bindings = source_bindings
                elif source_bindings != frozen_source_bindings:
                    raise ProtocolError("raw observation source binding changes across slots")
                parsed_values = {
                    row["source_id"]: row["parsed_value"] for row in receipt["sources"]
                }
                if phase == "pre":
                    paired_pre_values[slot.run_index] = parsed_values
                else:
                    before = paired_pre_values.get(slot.run_index)
                    if before is None or set(before) != set(parsed_values):
                        raise ProtocolError("raw observation pre/post source population differs")
                    for source_id, value in parsed_values.items():
                        kind = source_bindings[source_id][0]
                        if kind in ("aperf", "mperf", "amd_throttle") and value < before[source_id]:
                            raise ProtocolError("raw observation counter decreased or wrapped")
                        if kind == "f_ref" and value != before[source_id]:
                            raise ProtocolError("raw observation reference frequency changed")
                is_terminal_post = slot.run_index == FORMAL_RUN_COUNT - 1 and phase == "post"
                if is_terminal_post:
                    verified_guardian = verify_guardian_evidence_bytes(
                        guardian_evidence_jsonl, payload["population_seal"], peer,
                        terminal["session_id"], expected_guardian_contract,
                    )
                    if _record_sha256(payload["population_seal"]) != payload["population_seal_sha256"]:
                        raise ProtocolError("terminal population seal digest differs")
                    if payload["population_seal_sha256"] != terminal["population_seal_sha256"]:
                        raise ProtocolError("terminal receipt population seal differs")
                elif payload["population_seal"] is not None or payload["population_seal_sha256"] is not None:
                    raise ProtocolError("nonterminal observation contains a population seal")
        if verified_guardian is None or frozen_source_bindings is None:
            raise ProtocolError("successful transcript lacks guardian/source evidence")
        coverage_start = verified_guardian.seal["coverage_started_monotonic_ns"]
        coverage_end = verified_guardian.seal["coverage_ended_monotonic_ns"]
        if any(started < coverage_start or ended > coverage_end
               for started, ended in snapshot_intervals):
            raise ProtocolError("guardian coverage does not enclose every run-slot snapshot")
        guardian_sources, _values = _guardian_typed_telemetry(
            verified_guardian.records[0]["typed_telemetry"],
            verified_guardian.records[0]["started_monotonic_ns"],
            verified_guardian.records[0]["ended_monotonic_ns"],
        )
        if any(
            source_id not in frozen_source_bindings
            or frozen_source_bindings[source_id] != binding
            for source_id, binding in guardian_sources.items()
        ):
            raise ProtocolError("guardian typed sources differ from pre/post telemetry")
        if events[-1]["event"] != "restore":
            raise ProtocolError("successful transcript lacks terminal restore event")
        if has_control_evidence_schema:
            if verified_control is None or (
                _exact_bool(restore["abnormal_recovery"], "restore abnormal flag")
                or not _exact_bool(
                    restore["prior_state_sha256_matches"],
                    "prior/restored comparison",
                )
                or restore["prior_state_sha256"] != apply["prior_state_sha256"]
                or restore["restored_state_sha256"] != apply["prior_state_sha256"]
                or restore["restored_state_sha256"]
                != verified_control.seal["rows"][-1]["state_sha256"]
                or restore["population_seal_sha256"]
                != terminal["population_seal_sha256"]
                or restore["terminal_journal_sha256"]
                != terminal["terminal_journal_sha256"]
            ):
                raise ProtocolError("successful transcript restoration proof differs")
        else:
            restore = _exact_keys(events[-1]["payload"], (
                "abnormal_recovery", "population_seal_sha256", "prior_state_sha256",
                "prior_state_sha256_matches", "restored_state", "restored_state_sha256",
                "terminal_journal_sha256",
            ), "transcript restore payload")
            if (
                _exact_bool(restore["abnormal_recovery"], "restore abnormal flag")
                or not _exact_bool(restore["prior_state_sha256_matches"], "prior/restored comparison")
                or type(restore["restored_state"]) is not dict
                or _record_sha256(restore["restored_state"]) != restore["restored_state_sha256"]
                or restore["restored_state_sha256"] != apply["prior_state_sha256"]
                or restore["prior_state_sha256"] != apply["prior_state_sha256"]
                or restore["population_seal_sha256"] != terminal["population_seal_sha256"]
                or restore["terminal_journal_sha256"] != terminal["terminal_journal_sha256"]
            ):
                raise ProtocolError("successful transcript restoration proof differs")
    elif (
        has_control_evidence_schema
        and terminal["control_evidence_seal_sha256"] is not None
    ):
        if not has_restore or type(control_evidence_jsonl) is not bytes:
            raise ProtocolError("sealed fail-closed transcript lacks control evidence")
        restore = _exact_keys(events[-1]["payload"], (
            "abnormal_recovery", "control_evidence_seal",
            "control_evidence_seal_sha256", "population_seal_sha256",
            "prior_state_sha256", "prior_state_sha256_matches",
            "restored_state_sha256", "terminal_journal_sha256",
        ), "transcript restore payload")
        verified_control = verify_control_evidence_bytes(
            control_evidence_jsonl, restore["control_evidence_seal"],
            expected_binding, terminal["session_id"], terminal["journal_id"],
        )
        if (
            not _exact_bool(
                restore["prior_state_sha256_matches"],
                "fail-closed prior/restored comparison",
            )
            or restore["prior_state_sha256"]
            != verified_control.seal["rows"][0]["state_sha256"]
            or restore["restored_state_sha256"]
            != verified_control.seal["rows"][-1]["state_sha256"]
            or restore["prior_state_sha256"] != restore["restored_state_sha256"]
            or _record_sha256(restore["control_evidence_seal"])
            != restore["control_evidence_seal_sha256"]
            or restore["control_evidence_seal_sha256"]
            != terminal["control_evidence_seal_sha256"]
            or restore["terminal_journal_sha256"]
            != terminal["terminal_journal_sha256"]
        ):
            raise ProtocolError("fail-closed transcript control restoration proof differs")
    return VerifiedTranscript(tuple(events), terminal)


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int
    gid: int

    def __post_init__(self) -> None:
        for label, value in (("pid", self.pid), ("uid", self.uid), ("gid", self.gid)):
            if type(value) is not int or value < 0 or value > (1 << 31) - 1:
                _fail("peer " + label + " is invalid")

    def as_record(self) -> Mapping[str, int]:
        return {"gid": self.gid, "pid": self.pid, "uid": self.uid}

    @classmethod
    def from_record(cls, value: Any) -> "PeerCredentials":
        if type(value) is not dict or set(value) != {"gid", "pid", "uid"}:
            raise ProtocolError("SO_PEERCRED keys differ from the frozen schema")
        try:
            return cls(value["pid"], value["uid"], value["gid"])
        except PrivilegedHelperError as exc:
            raise ProtocolError(str(exc)) from exc


def socket_peer_credentials(connection: socket.socket) -> PeerCredentials:
    if not hasattr(socket, "SO_PEERCRED"):
        raise ProtocolError("Linux SO_PEERCRED is unavailable")
    size = struct.calcsize("3i")
    payload = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size)
    if len(payload) != size:
        raise ProtocolError("SO_PEERCRED returned an unexpected byte count")
    pid, uid, gid = struct.unpack("3i", payload)
    try:
        return PeerCredentials(pid, uid, gid)
    except PrivilegedHelperError as exc:
        raise ProtocolError(str(exc)) from exc


def protocol_core_sha256() -> str:
    """Hash this source for evidence, not as a pre-execution trust mechanism."""

    path = os.path.realpath(__file__)
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        status = os.fstat(descriptor)
        if status.st_size <= 0 or status.st_size > 4 * 1024 * 1024:
            _fail("protocol-core source size is outside its bound")
        payload = b""
        while len(payload) < status.st_size:
            chunk = os.read(descriptor, min(65536, status.st_size - len(payload)))
            if not chunk:
                _fail("protocol-core source read ended early")
            payload += chunk
        if len(payload) != status.st_size:
            _fail("protocol-core source byte count changed")
        return hashlib.sha256(payload).hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class InstalledFile:
    relative_path: str
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.relative_path) is not str or not self.relative_path
            or os.path.isabs(self.relative_path)
            or os.path.normpath(self.relative_path) != self.relative_path
            or any(item in ("", ".", "..") for item in self.relative_path.split("/"))
        ):
            _fail("installed-closure relative path is unsafe")
        _require_hash(self.sha256, "installed-closure file SHA-256")


def attest_installed_closure(
    trusted_root: str, files: Sequence[InstalledFile], *, expected_uid: int = 0,
    boundary: Optional[Callable[[str], None]] = None,
) -> Mapping[str, Any]:
    """Reference attestation for the future pre-Python static launcher.

    Calling this *from* the Python helper is too late to establish code trust.
    The audited native launcher must implement or invoke an equivalent check
    before initializing Python.  This implementation exists to freeze the
    invariants and provide synthetic protecting tests.
    """

    if (
        type(trusted_root) is not str or not trusted_root.startswith("/")
        or trusted_root == "/" or os.path.normpath(trusted_root) != trusted_root
    ):
        _fail("installed-closure root is not a normalized non-root absolute path")
    if type(expected_uid) is not int or expected_uid < 0:
        _fail("installed-closure owner UID is invalid")
    retained = tuple(files)
    if not retained or len(retained) > 256:
        _fail("installed-closure population is empty or oversized")
    if tuple(item.relative_path for item in retained) != tuple(sorted(
        set(item.relative_path for item in retained)
    )):
        _fail("installed-closure paths are not sorted and unique")

    def mark(label: str) -> None:
        if boundary is not None:
            boundary(label)

    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        root_flags |= os.O_NOFOLLOW
    mark("closure.before_root_open")
    root_fd = os.open(trusted_root, root_flags)
    try:
        mark("closure.after_root_open")
        root_status = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_status.st_mode)
            or root_status.st_uid != expected_uid
            or stat.S_IMODE(root_status.st_mode) & 0o022
        ):
            _fail("installed-closure root ownership or mode is unsafe")
        records = []
        for index, item in enumerate(retained):
            descriptor = os.dup(root_fd)
            try:
                components = item.relative_path.split("/")
                for component in components[:-1]:
                    following = os.open(component, root_flags, dir_fd=descriptor)
                    status = os.fstat(following)
                    if (
                        not stat.S_ISDIR(status.st_mode)
                        or status.st_uid != expected_uid
                        or stat.S_IMODE(status.st_mode) & 0o022
                    ):
                        os.close(following)
                        _fail("installed-closure directory ownership or mode is unsafe")
                    os.close(descriptor)
                    descriptor = following
                flags = os.O_RDONLY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                mark("closure.file.{}.before_open".format(index))
                file_fd = os.open(components[-1], flags, dir_fd=descriptor)
                try:
                    mark("closure.file.{}.after_open".format(index))
                    status = os.fstat(file_fd)
                    if (
                        not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                        or status.st_uid != expected_uid
                        or stat.S_IMODE(status.st_mode) & 0o022
                        or status.st_size <= 0 or status.st_size > 16 * 1024 * 1024
                    ):
                        _fail("installed-closure file inode metadata is unsafe")
                    digest = hashlib.sha256()
                    total = 0
                    while total < status.st_size:
                        chunk = os.read(file_fd, min(65536, status.st_size - total))
                        if not chunk:
                            _fail("installed-closure file read ended early")
                        digest.update(chunk)
                        total += len(chunk)
                    mark("closure.file.{}.after_hash".format(index))
                    named = os.stat(components[-1], dir_fd=descriptor, follow_symlinks=False)
                    held = os.fstat(file_fd)
                    if (
                        named.st_dev != held.st_dev or named.st_ino != held.st_ino
                        or named.st_nlink != 1 or held.st_nlink != 1
                    ):
                        _fail("installed-closure name no longer resolves to the held inode")
                    if digest.hexdigest() != item.sha256:
                        _fail("installed-closure file digest differs")
                    mark("closure.file.{}.after_name_recheck".format(index))
                    records.append({
                        "device": held.st_dev, "inode": held.st_ino,
                        "relative_path": item.relative_path,
                        "sha256": item.sha256, "size": held.st_size,
                    })
                finally:
                    os.close(file_fd)
            finally:
                os.close(descriptor)
        receipt = {
            "files": records,
            "owner_uid": expected_uid,
            "record_type": "cp2e_installed_helper_closure",
            "schema_version": SCHEMA_VERSION,
            "trusted_root": trusted_root,
        }
        canonical_json_bytes(receipt)
        return receipt
    finally:
        os.close(root_fd)


class _DurableJournal:
    """Held-inode append-only recovery journal with an exact hash chain."""

    def __init__(
        self, store: "DurableJournalStore", descriptor: int, name: str,
        identifier: str, binding: Binding, peer: PeerCredentials,
        nonce_sha256: str, prior: Mapping[str, Any], records: int = 0,
        previous_sha256: str = "0" * 64,
    ) -> None:
        self.store = store
        self.descriptor = descriptor
        self.name = name
        self.identifier = identifier
        self.binding = binding
        self.peer = peer
        self.nonce_sha256 = nonce_sha256
        self.prior = dict(prior)
        self.records = records
        self.previous_sha256 = previous_sha256
        self.closed = False
        status = os.fstat(descriptor)
        self.device = status.st_dev
        self.inode = status.st_ino

    def _verify_name(self) -> None:
        if self.closed:
            _fail("privileged journal handle is closed")
        named = os.stat(self.name, dir_fd=self.store.directory_fd, follow_symlinks=False)
        held = os.fstat(self.descriptor)
        if (
            named.st_dev != self.device or named.st_ino != self.inode
            or held.st_dev != self.device or held.st_ino != self.inode
            or named.st_nlink != 1 or held.st_nlink != 1
            or not stat.S_ISREG(held.st_mode)
            or stat.S_IMODE(held.st_mode) != 0o600
            or held.st_uid != self.store.expected_uid
        ):
            _fail("privileged journal name/inode metadata differs")

    def append(self, event: str, payload: Optional[Mapping[str, Any]]) -> None:
        if (
            type(event) is not str or not event or len(event) > 128
            or re.fullmatch(r"[a-z0-9_]+", event) is None
        ):
            _fail("privileged journal event name is invalid")
        if payload is not None:
            _bounded_canonical_json_bytes(
                dict(payload), MAX_JOURNAL_BYTES,
                "canonical privileged journal payload",
            )
        self._verify_name()
        if self.records >= MAX_JOURNAL_RECORDS:
            _fail("privileged journal record bound is exhausted")
        record = {
            "binding": dict(self.binding.as_record()),
            "event": event,
            "journal_id": self.identifier,
            "nonce_sha256": self.nonce_sha256,
            "payload": None if payload is None else dict(payload),
            "peer": self.peer.as_record(),
            "previous_sha256": self.previous_sha256,
            "record_type": "cp2e_privileged_journal_event",
            "schema_version": SCHEMA_VERSION,
            "sequence": self.records,
        }
        encoded = _bounded_canonical_json_bytes(
            record, MAX_JOURNAL_BYTES, "canonical privileged journal record",
        )
        size = os.lseek(self.descriptor, 0, os.SEEK_END)
        if size + len(encoded) > MAX_JOURNAL_BYTES:
            _fail("privileged journal exceeds its byte bound")
        self.store._mark("journal.append.before_write")
        offset = 0
        while offset < len(encoded):
            count = os.write(self.descriptor, encoded[offset:])
            if count <= 0:
                _fail("privileged journal write made no progress")
            offset += count
        self.store._mark("journal.append.after_write")
        self.store._mark("journal.append.before_file_fsync")
        os.fsync(self.descriptor)
        self.store._mark("journal.append.after_file_fsync")
        self.previous_sha256 = hashlib.sha256(encoded).hexdigest()
        self.records += 1

    def _verify_complete_chain(self) -> None:
        self._verify_name()
        status = os.fstat(self.descriptor)
        if status.st_size <= 0 or status.st_size > MAX_JOURNAL_BYTES:
            _fail("completed privileged journal size differs")
        raw = os.pread(self.descriptor, status.st_size, 0)
        if len(raw) != status.st_size or not raw.endswith(b"\n"):
            _fail("completed privileged journal bytes are incomplete")
        lines = raw.splitlines(keepends=True)
        if len(lines) != self.records:
            _fail("completed privileged journal record count differs")
        previous = "0" * 64
        last_event = None
        exact_keys = {
            "binding", "event", "journal_id", "nonce_sha256", "payload",
            "peer", "previous_sha256", "record_type", "schema_version", "sequence",
        }
        for sequence, line in enumerate(lines):
            value = _strict_bounded_json_bytes(
                line, MAX_JOURNAL_BYTES, "privileged journal record",
            )
            if set(value) != exact_keys:
                _fail("completed privileged journal record keys differ")
            if (
                value["record_type"] != "cp2e_privileged_journal_event"
                or type(value["schema_version"]) is not int
                or value["schema_version"] != SCHEMA_VERSION
                or type(value["sequence"]) is not int
                or value["sequence"] != sequence
                or value["journal_id"] != self.identifier
                or value["previous_sha256"] != previous
                or Binding.from_record(value["binding"]) != self.binding
                or PeerCredentials.from_record(value["peer"]) != self.peer
                or value["nonce_sha256"] != self.nonce_sha256
                or type(value["event"]) is not str
                or re.fullmatch(r"[a-z0-9_]+", value["event"]) is None
                or (value["payload"] is not None and type(value["payload"]) is not dict)
            ):
                _fail("completed privileged journal identity/hash chain differs")
            previous = hashlib.sha256(line).hexdigest()
            last_event = value["event"]
        if previous != self.previous_sha256 or last_event != "journal_completed":
            _fail("completed privileged journal terminal seal differs")

    def complete(self) -> None:
        self.append("journal_completed", None)
        self._verify_complete_chain()
        self._verify_name()
        restored_name = self.name[:-len(".pending")] + ".restored"
        try:
            os.stat(restored_name, dir_fd=self.store.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("privileged completed-journal name already exists")
        self.store._mark("journal.complete.before_rename")
        os.rename(
            self.name, restored_name,
            src_dir_fd=self.store.directory_fd, dst_dir_fd=self.store.directory_fd,
        )
        self.name = restored_name
        self.store._mark("journal.complete.after_rename")
        self.store._mark("journal.complete.before_parent_fsync")
        os.fsync(self.store.directory_fd)
        self.store._mark("journal.complete.after_parent_fsync")
        self.close()

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


class DurableJournalStore:
    """Root-owned production journal shape, exercised only in temp fixtures."""

    NAME = re.compile(r"^cp2e-privileged-([0-9a-f]{32})\.pending$")

    def __init__(
        self, directory: str, *, expected_uid: int = 0,
        fault_injector: Optional[Callable[[str], None]] = None,
    ) -> None:
        if (
            type(directory) is not str or not directory.startswith("/")
            or directory == "/" or os.path.normpath(directory) != directory
        ):
            _fail("privileged journal directory is unsafe")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self.directory = directory
        self.expected_uid = expected_uid
        self.fault_injector = fault_injector
        self.directory_fd = os.open(directory, flags)
        status = os.fstat(self.directory_fd)
        if (
            not stat.S_ISDIR(status.st_mode) or status.st_uid != expected_uid
            or stat.S_IMODE(status.st_mode) & 0o022
        ):
            os.close(self.directory_fd)
            _fail("privileged journal directory ownership or mode is unsafe")
        try:
            fcntl.flock(self.directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.directory_fd)
            _fail("another privileged helper owns the recovery directory")

    def _mark(self, label: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(label)

    def create(
        self, binding: Binding, peer: PeerCredentials, nonce_sha256: str,
        prior: Mapping[str, Any],
    ) -> _DurableJournal:
        _require_hash(nonce_sha256, "privileged journal nonce digest")
        canonical_control_state_bytes(dict(prior))
        pending = self.pending()
        if pending:
            for item in pending:
                item.close()
            raise RecoveryIndeterminate("a pending privileged journal already exists")
        identifier = os.urandom(16).hex()
        name = "cp2e-privileged-{}.pending".format(identifier)
        flags = os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        self._mark("journal.create.before_open")
        descriptor = os.open(name, flags, 0o600, dir_fd=self.directory_fd)
        journal: Optional[_DurableJournal] = None
        try:
            self._mark("journal.create.after_open")
            os.fchmod(descriptor, 0o600)
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != self.expected_uid
                or stat.S_IMODE(status.st_mode) != 0o600
            ):
                _fail("new privileged journal inode metadata is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            journal = _DurableJournal(
                self, descriptor, name, identifier, binding, peer,
                nonce_sha256, prior,
            )
            journal.append("prior_state_captured", {"prior": dict(prior)})
            self._mark("journal.create.before_parent_fsync")
            os.fsync(self.directory_fd)
            self._mark("journal.create.after_parent_fsync")
            return journal
        except BaseException:
            if journal is None:
                os.close(descriptor)
            else:
                journal.close()
            # Retain even an incomplete inode.  Ambiguous durability must block
            # later execution rather than silently discard possible prior state.
            raise

    def _load(self, name: str, identifier: str) -> _DurableJournal:
        flags = os.O_RDWR | os.O_APPEND | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=self.directory_fd)
        try:
            status = os.fstat(descriptor)
            if (
                not stat.S_ISREG(status.st_mode) or status.st_nlink != 1
                or status.st_uid != self.expected_uid
                or stat.S_IMODE(status.st_mode) != 0o600
                or status.st_size <= 0 or status.st_size > MAX_JOURNAL_BYTES
            ):
                _fail("pending privileged journal inode metadata is unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raw = os.pread(descriptor, status.st_size, 0)
            if len(raw) != status.st_size or not raw.endswith(b"\n"):
                _fail("pending privileged journal bytes are incomplete")
            lines = raw.splitlines(keepends=True)
            if not lines or len(lines) > MAX_JOURNAL_RECORDS:
                _fail("pending privileged journal record count is invalid")
            previous = "0" * 64
            binding = None
            peer = None
            nonce_sha256 = None
            prior = None
            exact_keys = {
                "binding", "event", "journal_id", "nonce_sha256", "payload",
                "peer", "previous_sha256", "record_type", "schema_version", "sequence",
            }
            for sequence, line in enumerate(lines):
                value = _strict_bounded_json_bytes(
                    line, MAX_JOURNAL_BYTES, "privileged journal record",
                )
                if set(value) != exact_keys:
                    _fail("pending privileged journal record keys differ")
                if (
                    value["record_type"] != "cp2e_privileged_journal_event"
                    or type(value["schema_version"]) is not int
                    or value["schema_version"] != SCHEMA_VERSION
                    or type(value["sequence"]) is not int
                    or value["sequence"] != sequence
                    or value["journal_id"] != identifier
                    or value["previous_sha256"] != previous
                    or type(value["event"]) is not str
                    or re.fullmatch(r"[a-z0-9_]+", value["event"]) is None
                    or (value["payload"] is not None and type(value["payload"]) is not dict)
                ):
                    _fail("pending privileged journal identity/hash chain differs")
                current_binding = Binding.from_record(value["binding"])
                peer_value = value["peer"]
                if type(peer_value) is not dict or set(peer_value) != {"gid", "pid", "uid"}:
                    _fail("pending privileged journal peer keys differ")
                current_peer = PeerCredentials(
                    peer_value["pid"], peer_value["uid"], peer_value["gid"],
                )
                current_nonce = _require_hash(
                    value["nonce_sha256"], "pending journal nonce digest",
                )
                if sequence == 0:
                    if value["event"] != "prior_state_captured":
                        _fail("pending privileged journal lacks its prior record")
                    payload = value["payload"]
                    if type(payload) is not dict or set(payload) != {"prior"}:
                        _fail("pending privileged journal prior payload differs")
                    if type(payload["prior"]) is not dict:
                        _fail("pending privileged journal prior is not a mapping")
                    binding, peer, nonce_sha256 = current_binding, current_peer, current_nonce
                    prior = dict(payload["prior"])
                elif (
                    current_binding != binding or current_peer != peer
                    or current_nonce != nonce_sha256
                ):
                    _fail("pending privileged journal binding changes within its chain")
                previous = hashlib.sha256(line).hexdigest()
            if binding is None or peer is None or nonce_sha256 is None or prior is None:
                _fail("pending privileged journal is incomplete")
            return _DurableJournal(
                self, descriptor, name, identifier, binding, peer,
                nonce_sha256, prior, len(lines), previous,
            )
        except BaseException:
            os.close(descriptor)
            raise

    def pending(self) -> Tuple[_DurableJournal, ...]:
        names = sorted(name for name in os.listdir(self.directory_fd) if self.NAME.fullmatch(name))
        if len(names) > 1:
            raise RecoveryIndeterminate(
                "multiple pending privileged journals make recovery order ambiguous"
            )
        retained = []
        for name in names:
            match = self.NAME.fullmatch(name)
            retained.append(self._load(name, match.group(1)))
        return tuple(retained)

    def close(self) -> None:
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1


def _message_identity(value: Any, record_type: str, sequence: int) -> Mapping[str, Any]:
    if type(value) is not dict:
        raise ProtocolError("protocol message is not a mapping")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("protocol schema version differs")
    if value.get("record_type") != record_type:
        raise ProtocolError("protocol record type differs")
    if type(value.get("sequence")) is not int or value.get("sequence") != sequence:
        raise ProtocolError("protocol sequence differs")
    return value


def hello_record(
    binding: Binding, client_nonce: str, challenge_nonce: str,
) -> Mapping[str, Any]:
    if type(client_nonce) is not str or NONCE_PATTERN.fullmatch(client_nonce) is None:
        _fail("client nonce is not exactly 256 bits of lowercase hexadecimal")
    if type(challenge_nonce) is not str or NONCE_PATTERN.fullmatch(challenge_nonce) is None:
        _fail("challenge nonce is not exactly 256 bits of lowercase hexadecimal")
    return {
        "binding": dict(binding.as_record()),
        "challenge_nonce": challenge_nonce,
        "client_nonce": client_nonce,
        "record_type": "cp2e_privileged_hello",
        "schema_version": SCHEMA_VERSION,
        "sequence": 0,
    }


def command_record(record_type: str, sequence: int, session_id: str, **extra: Any) -> Mapping[str, Any]:
    if record_type not in (
        "cp2e_privileged_apply", "cp2e_privileged_observe",
        "cp2e_privileged_restore",
    ):
        _fail("client command record type is unsupported")
    _require_u64(sequence, "client command sequence")
    _require_hash(session_id, "client command session ID")
    value: Dict[str, Any] = {
        "record_type": record_type, "schema_version": SCHEMA_VERSION,
        "sequence": sequence, "session_id": session_id,
    }
    if set(extra).intersection(value):
        _fail("client command extra keys overlap its immutable identity")
    value.update(extra)
    canonical_json_bytes(value)
    return value


_OBSERVATION_STATES = tuple(
    (
        "run_{}_pre_observed".format(run_index),
        "run_{}_post_observed".format(run_index),
    )
    for run_index in range(FORMAL_RUN_COUNT)
)

_ALLOWED_TRANSITIONS = {
    "new": frozenset(("authenticated", "closed")),
    "authenticated": frozenset(("capturing_prior", "restoring", "closed")),
    "capturing_prior": frozenset(("prior_captured", "restoring", "indeterminate", "closed")),
    "prior_captured": frozenset(("applying", "restoring", "indeterminate")),
    "applying": frozenset(("applied", "restoring", "indeterminate")),
    "applied": frozenset((_OBSERVATION_STATES[0][0], "restoring", "indeterminate")),
    "restoring": frozenset(("restored", "indeterminate")),
    "restored": frozenset(("closed",)),
    "indeterminate": frozenset(("closed",)),
    "closed": frozenset(),
}
for _run_index, (_pre_state, _post_state) in enumerate(_OBSERVATION_STATES):
    _ALLOWED_TRANSITIONS[_pre_state] = frozenset(
        (_post_state, "restoring", "indeterminate")
    )
    _following = (
        "restoring"
        if _run_index + 1 == FORMAL_RUN_COUNT
        else _OBSERVATION_STATES[_run_index + 1][0]
    )
    _ALLOWED_TRANSITIONS[_post_state] = frozenset(
        (_following, "restoring", "indeterminate")
    )


class HelperSession:
    """One exact client/helper transaction; the helper is sole restorer."""

    def __init__(
        self, connection: socket.socket, expected_binding: Binding,
        expected_peer: PeerCredentials, backend: Any, journal_store: Any,
        expected_run_slots: Sequence[FrozenRunSlot],
        guardian_contract: GuardianEvidenceContract,
        guardian_evidence_writer: GuardianEvidenceWriter,
        control_evidence_writer: ControlEvidenceWriter,
        fault_injector: Optional[Callable[[str], None]] = None,
        peer_credentials_reader: Callable[[socket.socket], PeerCredentials] = socket_peer_credentials,
    ) -> None:
        self.connection = connection
        self.expected_binding = expected_binding
        self.expected_peer = expected_peer
        self.backend = backend
        self.journal_store = journal_store
        self.expected_run_slots = _frozen_run_slots(expected_run_slots)
        if type(guardian_contract) is not GuardianEvidenceContract:
            _fail("guardian evidence contract is not frozen")
        if (
            type(guardian_evidence_writer) is not GuardianEvidenceWriter
            or guardian_evidence_writer.contract != guardian_contract
        ):
            _fail("guardian evidence writer differs from its trusted contract")
        self.guardian_contract = guardian_contract
        self.guardian_evidence_writer = guardian_evidence_writer
        if type(control_evidence_writer) is not ControlEvidenceWriter:
            _fail("control evidence writer is not a held helper writer")
        self.control_evidence_writer = control_evidence_writer
        self.fault_injector = fault_injector
        self.peer_credentials_reader = peer_credentials_reader
        self.state = "new"
        self.state_history = ["new"]
        self.session_id: Optional[str] = None
        self.nonce_sha256: Optional[str] = None
        self.challenge_nonce: Optional[str] = None
        self.client_nonce: Optional[str] = None
        self.actual_peer: Optional[PeerCredentials] = None
        self.prior: Optional[Mapping[str, Any]] = None
        self.journal: Optional[Any] = None
        self.abnormal_recovery = False
        self._recovery_preflight_completed = False
        self._mutation_possible = False
        self._population_guard_started = False
        self._population_stop_attempted = False
        self._population_stop_count = 0
        self._population_seal: Optional[Mapping[str, Any]] = None
        self._population_seal_sha256: Optional[str] = None
        self._control_evidence_seal: Optional[Mapping[str, Any]] = None
        self._control_evidence_seal_sha256: Optional[str] = None
        self._journal_id: Optional[str] = None
        self._terminal_journal_sha256: Optional[str] = None
        self._transcript = _TranscriptBuilder()
        self._terminal_receipt_bytes: Optional[bytes] = None

    @property
    def transcript_bytes(self) -> bytes:
        return self._transcript.payload

    @property
    def terminal_receipt_bytes(self) -> bytes:
        if self._terminal_receipt_bytes is None:
            _fail("privileged transcript terminal receipt is not sealed")
        return self._terminal_receipt_bytes

    @property
    def guardian_evidence_bytes(self) -> bytes:
        return self.guardian_evidence_writer.payload

    @property
    def control_evidence_bytes(self) -> bytes:
        return self.control_evidence_writer.payload

    def _append_transcript(self, event: str, payload: Mapping[str, Any]) -> None:
        self._transcript.append(event, payload)

    def _boundary(self, label: str, *, enabled: bool = True) -> None:
        if enabled and self.fault_injector is not None:
            self.fault_injector(label)

    def _transition(self, following: str) -> None:
        if following not in _ALLOWED_TRANSITIONS[self.state]:
            _fail("forbidden helper state transition {} -> {}".format(self.state, following))
        self.state = following
        self.state_history.append(following)

    def _send_ack(
        self, record_type: str, sequence: int, payload: Mapping[str, Any],
    ) -> None:
        if self.session_id is None:
            _fail("session ID is absent")
        value = {
            "payload": dict(payload),
            "record_type": record_type,
            "schema_version": SCHEMA_VERSION,
            "sequence": sequence,
            "session_id": self.session_id,
            "state": self.state,
        }
        send_packet(self.connection, value)

    def _handshake(self) -> None:
        self._boundary(HANDSHAKE_BOUNDARIES[0])
        peer = self.peer_credentials_reader(self.connection)
        self.actual_peer = peer
        self._boundary(HANDSHAKE_BOUNDARIES[1])
        if peer != self.expected_peer:
            raise ProtocolError("SO_PEERCRED differs from the expected client identity")
        self.challenge_nonce = os.urandom(32).hex()
        self._boundary(HANDSHAKE_BOUNDARIES[2])
        send_packet(self.connection, {
            "binding_digest": self.expected_binding.digest,
            "challenge_nonce": self.challenge_nonce,
            "record_type": "cp2e_privileged_challenge",
            "schema_version": SCHEMA_VERSION,
            "sequence": 0,
        })
        self._append_transcript("challenge", {
            "binding": dict(self.expected_binding.as_record()),
            "binding_digest": self.expected_binding.digest,
            "challenge_nonce": self.challenge_nonce,
            "peer": peer.as_record(),
        })
        self._boundary(HANDSHAKE_BOUNDARIES[3])
        self._boundary(HANDSHAKE_BOUNDARIES[4])
        message = recv_packet(self.connection)
        self._boundary(HANDSHAKE_BOUNDARIES[5])
        expected_keys = {
            "binding", "challenge_nonce", "client_nonce", "record_type",
            "schema_version", "sequence",
        }
        _message_identity(message, "cp2e_privileged_hello", 0)
        if set(message) != expected_keys:
            raise ProtocolError("hello keys differ from the frozen schema")
        client_nonce = message["client_nonce"]
        challenge_nonce = message["challenge_nonce"]
        if type(client_nonce) is not str or NONCE_PATTERN.fullmatch(client_nonce) is None:
            raise ProtocolError("client nonce is not exactly 256 bits")
        if challenge_nonce != self.challenge_nonce:
            raise ProtocolError("hello does not echo the fresh helper challenge")
        binding = Binding.from_record(message["binding"])
        self._boundary(HANDSHAKE_BOUNDARIES[6])
        if binding != self.expected_binding:
            raise ProtocolError("hello binding differs from the frozen helper identity")
        self._boundary(HANDSHAKE_BOUNDARIES[7])
        material = {
            "binding_digest": binding.digest,
            "challenge_nonce": challenge_nonce,
            "client_nonce": client_nonce,
            "peer": peer.as_record(),
        }
        self.session_id = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
        self.client_nonce = client_nonce
        self.nonce_sha256 = hashlib.sha256(
            bytes.fromhex(challenge_nonce) + bytes.fromhex(client_nonce)
        ).hexdigest()
        self._append_transcript("hello", {
            "binding": dict(binding.as_record()),
            "challenge_nonce": challenge_nonce,
            "client_nonce": client_nonce,
            "nonce_sha256": self.nonce_sha256,
            "peer": peer.as_record(),
            "session_id": self.session_id,
        })
        self._transition("authenticated")
        self._boundary(HANDSHAKE_BOUNDARIES[8])
        self._send_ack("cp2e_privileged_hello_ack", 0, {
            "binding_digest": binding.digest,
            "nonce_sha256": self.nonce_sha256,
            "peer": peer.as_record(),
        })
        self._boundary(HANDSHAKE_BOUNDARIES[9])

    def _receive_exact_command(
        self, record_type: str, sequence: int, extra_keys: Sequence[str] = (),
    ) -> Mapping[str, Any]:
        value = recv_packet(self.connection)
        _message_identity(value, record_type, sequence)
        expected = {
            "record_type", "schema_version", "sequence", "session_id",
        }.union(extra_keys)
        if set(value) != expected:
            raise ProtocolError("client command keys differ from the frozen schema")
        if value["session_id"] != self.session_id:
            raise ProtocolError("client command session ID differs")
        return value

    def _apply(self) -> None:
        self._receive_exact_command("cp2e_privileged_apply", 1)
        self._boundary(APPLY_BOUNDARIES[0])
        self._transition("capturing_prior")
        self.prior = self.backend.capture_prior()
        prior_bytes = canonical_control_state_bytes(dict(self.prior))
        prior_sha256 = hashlib.sha256(prior_bytes).hexdigest()
        self._boundary(APPLY_BOUNDARIES[1])
        if self.nonce_sha256 is None:
            _fail("nonce digest is absent")
        self.journal = self.journal_store.create(
            self.expected_binding, self.expected_peer, self.nonce_sha256, self.prior,
        )
        self._journal_id = self.journal.identifier
        if self.session_id is None:
            _fail("session ID is absent while binding control evidence")
        self.control_evidence_writer.bind_identity(
            self.expected_binding, self.session_id, self.journal.identifier,
        )
        prior_checkpoint = self.control_evidence_writer.append("prior", self.prior)
        prior_checkpoint_sha256 = _record_sha256(prior_checkpoint)
        self.journal.append("prior_control_evidence_durable", {
            "control_evidence_checkpoint": dict(prior_checkpoint),
            "control_evidence_checkpoint_sha256": prior_checkpoint_sha256,
            "state_sha256": prior_sha256,
        })
        self._transition("prior_captured")
        self._boundary(APPLY_BOUNDARIES[2])
        self._boundary(APPLY_BOUNDARIES[3])
        self._transition("applying")
        self._mutation_possible = True
        self.backend.apply(self.prior, lambda label: self._boundary("apply.backend." + label))
        self._boundary(APPLY_BOUNDARIES[4])
        applied = self.backend.validate_applied()
        applied_bytes = canonical_control_state_bytes(dict(applied))
        applied_sha256 = hashlib.sha256(applied_bytes).hexdigest()
        applied_checkpoint = self.control_evidence_writer.append("applied", applied)
        applied_checkpoint_sha256 = _record_sha256(applied_checkpoint)
        self.guardian_evidence_writer.bind_identity(
            self.expected_peer, self.session_id,
        )
        population_binding = self.backend.start_population_guard(
            self.expected_peer, self.session_id,
            self.guardian_evidence_writer.append,
        )
        self._population_guard_started = True
        canonical_json_bytes(dict(population_binding))
        population_binding_sha256 = _record_sha256(population_binding)
        population_state = self.backend.validate_population("applied")
        canonical_json_bytes(dict(population_state))
        self._boundary(APPLY_BOUNDARIES[5])
        self.journal.append("applied_validated", {
            "control_evidence_checkpoint": dict(applied_checkpoint),
            "control_evidence_checkpoint_sha256": applied_checkpoint_sha256,
            "population_binding": dict(population_binding),
            "population_binding_sha256": population_binding_sha256,
            "state_sha256": applied_sha256,
        })
        self._transition("applied")
        self._append_transcript("apply", {
            "applied_state_sha256": applied_sha256,
            "control_evidence_checkpoint": dict(applied_checkpoint),
            "control_evidence_checkpoint_sha256": applied_checkpoint_sha256,
            "journal_id": self.journal.identifier,
            "population_binding": dict(population_binding),
            "population_binding_sha256": population_binding_sha256,
            "population_state": dict(population_state),
            "prior_state_sha256": prior_sha256,
        })
        self._boundary(APPLY_BOUNDARIES[6])
        self._send_ack("cp2e_privileged_apply_ack", 1, {
            "applied_state_sha256": applied_sha256,
            "control_evidence_checkpoint": dict(applied_checkpoint),
            "control_evidence_checkpoint_sha256": applied_checkpoint_sha256,
            "journal_id": self.journal.identifier,
            "population_binding": dict(population_binding),
            "population_state": dict(population_state),
            "prior_state_sha256": prior_sha256,
        })
        self._boundary(APPLY_BOUNDARIES[7])

    def _stop_population_guard_once(self) -> Optional[Mapping[str, Any]]:
        if not self._population_guard_started:
            return None
        if self._population_seal is not None:
            return self._population_seal
        if self._population_stop_attempted:
            raise RecoveryIndeterminate(
                "population guardian stop was already attempted without a proved seal"
            )
        self._population_stop_attempted = True
        self._population_stop_count += 1
        terminal_state = self.backend.stop_population_guard()
        canonical_json_bytes(dict(terminal_state))
        if self.actual_peer is None or self.session_id is None:
            _fail("population guardian identity is absent")
        seal = self.guardian_evidence_writer.seal(terminal_state)
        if self._population_stop_count != 1:
            _fail("population guardian was stopped more than once")
        self._population_seal = dict(seal)
        self._population_seal_sha256 = _record_sha256(seal)
        return self._population_seal

    def _observe(self, phase: str, run_index: int, sequence: int) -> None:
        value = self._receive_exact_command(
            "cp2e_privileged_observe", sequence, ("phase", "run_index"),
        )
        if (
            value["phase"] != phase
            or type(value["phase"]) is not str
            or type(value["run_index"]) is not int
            or value["run_index"] != run_index
            or type(run_index) is not int
            or run_index < 0
            or run_index >= FORMAL_RUN_COUNT
        ):
            raise ProtocolError(
                "telemetry phase/run index differs from the exact state-machine slot"
            )
        boundaries = tuple(
            item.format(phase=phase, run_index=run_index)
            for item in OBSERVE_BOUNDARIES
        )
        self._boundary(boundaries[0])
        population_phase = "run_{}_{}".format(run_index, phase)
        population = self.backend.validate_population(population_phase)
        canonical_json_bytes(dict(population))
        receipt = self.backend.observe(phase, run_index)
        slot = self.expected_run_slots[run_index]
        _validate_raw_observation_receipt(receipt, slot, phase)
        receipt_sha256 = _record_sha256(receipt)
        population_sha256 = _record_sha256(population)
        sealed_population = None
        if phase == "post" and run_index + 1 == FORMAL_RUN_COUNT:
            sealed_population = self._stop_population_guard_once()
        self._boundary(boundaries[1])
        self.journal.append(population_phase + "_telemetry_captured", {
            "population_seal_sha256": (
                None if sealed_population is None else self._population_seal_sha256
            ),
            "population_sha256": population_sha256,
            "receipt_sha256": receipt_sha256,
            "run_slot": dict(slot.as_record()),
        })
        self._boundary(boundaries[2])
        self._transition(_OBSERVATION_STATES[run_index][0 if phase == "pre" else 1])
        transcript_payload = {
            "phase": phase,
            "population": dict(population),
            "population_seal": (
                None if sealed_population is None else dict(sealed_population)
            ),
            "population_seal_sha256": (
                None if sealed_population is None else self._population_seal_sha256
            ),
            "population_sha256": population_sha256,
            "receipt": dict(receipt),
            "receipt_sha256": receipt_sha256,
            "run_slot": dict(slot.as_record()),
        }
        self._append_transcript("run_slot_observation", transcript_payload)
        self._boundary(boundaries[3])
        self._send_ack("cp2e_privileged_observe_ack", sequence, {
            "phase": phase, "run_index": run_index,
            "run_slot": dict(slot.as_record()),
            "population": dict(population),
            "population_seal": (
                None if sealed_population is None else dict(sealed_population)
            ),
            "receipt": dict(receipt),
            "receipt_sha256": receipt_sha256,
        })
        self._boundary(boundaries[4])

    def _restore(self, *, send_ack: bool, inject_faults: bool, reason: str) -> None:
        if self.state == "restored":
            return
        if self.prior is None or self.journal is None:
            return
        if self.state != "restoring":
            self._transition("restoring")
        self._boundary(RESTORE_BOUNDARIES[0], enabled=inject_faults)
        self.journal.append("restoration_started", {"reason": reason})
        self._boundary(RESTORE_BOUNDARIES[1], enabled=inject_faults)
        self._boundary(RESTORE_BOUNDARIES[2], enabled=inject_faults)
        population_error: Optional[BaseException] = None
        try:
            self._stop_population_guard_once()
        except BaseException as exc:
            population_error = exc
        self.backend.restore(
            self.prior,
            lambda label: self._boundary("restore.backend." + label, enabled=inject_faults),
        )
        self._boundary(RESTORE_BOUNDARIES[3], enabled=inject_faults)
        restored = self.backend.validate_restored(self.prior)
        restored_bytes = canonical_control_state_bytes(dict(restored))
        if population_error is not None:
            raise population_error
        prior_sha256 = hashlib.sha256(
            canonical_control_state_bytes(dict(self.prior))
        ).hexdigest()
        restored_sha256 = hashlib.sha256(restored_bytes).hexdigest()
        if restored_sha256 != prior_sha256:
            _fail("restored full state differs from the canonical prior state")
        self._mutation_possible = False
        self._boundary(RESTORE_BOUNDARIES[4], enabled=inject_faults)
        if self._control_evidence_seal is None:
            current_checkpoint = self.control_evidence_writer.checkpoint
            current_rows = current_checkpoint["rows"]
            if current_rows and current_rows[-1]["phase"] == "restored":
                if current_rows[-1]["state_sha256"] != restored_sha256:
                    raise RecoveryIndeterminate(
                        "retried restoration differs from durable control evidence"
                    )
                restored_checkpoint = current_checkpoint
            else:
                restored_checkpoint = self.control_evidence_writer.append(
                    "restored", restored,
                )
            control_evidence_seal = self.control_evidence_writer.seal()
            self._control_evidence_seal = dict(control_evidence_seal)
            self._control_evidence_seal_sha256 = _record_sha256(control_evidence_seal)
        else:
            control_evidence_seal = self._control_evidence_seal
            restored_checkpoint = self.control_evidence_writer.checkpoint
        restored_checkpoint_sha256 = _record_sha256(restored_checkpoint)
        self.journal.append("restoration_validated", {
            "control_evidence_checkpoint": dict(restored_checkpoint),
            "control_evidence_checkpoint_sha256": restored_checkpoint_sha256,
            "control_evidence_seal": dict(control_evidence_seal),
            "control_evidence_seal_sha256": self._control_evidence_seal_sha256,
            "population_seal_sha256": self._population_seal_sha256,
            "state_sha256": restored_sha256,
        })
        self._boundary(RESTORE_BOUNDARIES[5], enabled=inject_faults)
        self.journal.complete()
        terminal_journal_sha256 = getattr(self.journal, "previous_sha256", None)
        self._terminal_journal_sha256 = _require_hash(
            terminal_journal_sha256, "terminal privileged journal digest",
        )
        self._transition("restored")
        restore_payload = {
            "abnormal_recovery": self.abnormal_recovery,
            "control_evidence_seal": dict(control_evidence_seal),
            "control_evidence_seal_sha256": self._control_evidence_seal_sha256,
            "population_seal_sha256": self._population_seal_sha256,
            "prior_state_sha256": prior_sha256,
            "prior_state_sha256_matches": restored_sha256 == prior_sha256,
            "restored_state_sha256": restored_sha256,
            "terminal_journal_sha256": self._terminal_journal_sha256,
        }
        self._append_transcript("restore", restore_payload)
        self._boundary(RESTORE_BOUNDARIES[6], enabled=inject_faults)
        if send_ack:
            self._boundary(RESTORE_BOUNDARIES[7], enabled=inject_faults)
            self._send_ack("cp2e_privileged_restore_ack", RESTORE_SEQUENCE, {
                "abnormal_recovery": self.abnormal_recovery,
                "control_evidence_seal": dict(control_evidence_seal),
                "control_evidence_seal_sha256": self._control_evidence_seal_sha256,
                "prior_state_sha256": prior_sha256,
                "restored_state_sha256": restored_sha256,
                "terminal_journal_sha256": self._terminal_journal_sha256,
            })
            self._boundary(RESTORE_BOUNDARIES[8], enabled=inject_faults)

    def _emergency_restore(self, reason: str) -> None:
        self.abnormal_recovery = True
        if self.prior is None or self.journal is None or self.state in ("restored", "closed"):
            return
        try:
            self.journal.append("abnormal_recovery_requested", {"reason": reason})
        except BaseException:
            # A journal failure cannot justify skipping host restoration.
            pass
        try:
            self._restore(send_ack=False, inject_faults=False, reason=reason)
        except BaseException as exc:
            if self.state != "indeterminate":
                if "indeterminate" in _ALLOWED_TRANSITIONS[self.state]:
                    self._transition("indeterminate")
            raise RecoveryIndeterminate(
                "exact emergency restoration could not be proved: " + str(exc)
            ) from exc

    def _seal_terminal_receipt(
        self, original_error: Optional[BaseException],
        closure_error: Optional[BaseException],
    ) -> None:
        closure_passed = closure_error is None
        restoration_proved = (
            self._recovery_preflight_completed and not self._mutation_possible
            and not isinstance(original_error, RecoveryIndeterminate)
        )
        event_names = [
            strict_json_bytes(line)["event"]
            for line in self._transcript.payload.splitlines(keepends=True)
        ]
        exact_success_names = ["challenge", "hello", "apply"] + [
            "run_slot_observation"
            for _slot in self.expected_run_slots
            for _phase in ("pre", "post")
        ] + ["restore"]
        success = (
            original_error is None and closure_passed and restoration_proved
            and not self.abnormal_recovery and self._population_stop_count == 1
            and self._population_seal_sha256 is not None
            and self._control_evidence_seal_sha256 is not None
            and self._control_evidence_seal is not None
            and self._control_evidence_seal["phases"]
            == list(ControlEvidenceWriter._SUCCESS_PHASES)
            and self._terminal_journal_sha256 is not None
            and event_names == exact_success_names
        )
        if success:
            terminal_status = "success"
            error_type = None
        elif restoration_proved and closure_passed:
            terminal_status = "fail_closed"
            error_type = (
                type(original_error).__name__
                if original_error is not None else "TerminalInvariantError"
            )
        else:
            terminal_status = "indeterminate"
            cause = closure_error if closure_error is not None else original_error
            error_type = type(cause).__name__ if cause is not None else "RecoveryIndeterminate"
        transcript = self._transcript.payload
        receipt = {
            "abnormal_recovery": self.abnormal_recovery,
            "binding": dict(self.expected_binding.as_record()),
            "binding_digest": self.expected_binding.digest,
            "control_evidence_seal_sha256": self._control_evidence_seal_sha256,
            "descriptor_closure": {
                "attempted": True,
                "error_type": (
                    None if closure_error is None else type(closure_error).__name__
                ),
                "passed": closure_passed,
            },
            "error_type": error_type,
            "event_count": self._transcript.count,
            "first_record_sha256": self._transcript.first_sha256,
            "journal_id": self._journal_id,
            "last_record_sha256": self._transcript.last_sha256,
            "population_seal_sha256": self._population_seal_sha256,
            "population_stop_count": self._population_stop_count,
            "record_type": "cp2e_privileged_transcript_terminal",
            "restoration_proved": restoration_proved,
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "terminal_journal_sha256": self._terminal_journal_sha256,
            "terminal_status": terminal_status,
            "transcript_sha256": hashlib.sha256(transcript).hexdigest(),
            "transcript_size_bytes": len(transcript),
        }
        self._terminal_receipt_bytes = canonical_json_bytes(receipt)

    def run(self) -> Mapping[str, Any]:
        """Serve exactly one session and always attempt restoration on exit."""

        original_error: Optional[BaseException] = None
        try:
            pending = self.journal_store.pending()
            if pending:
                for item in pending:
                    close = getattr(item, "close", None)
                    if close is not None:
                        close()
                raise RecoveryIndeterminate(
                    "pending privileged recovery must be reconciled before a new session"
                )
            self._recovery_preflight_completed = True
            self._handshake()
            self._apply()
            for run_index in range(FORMAL_RUN_COUNT):
                sequence = 2 + 2 * run_index
                self._observe("pre", run_index, sequence)
                # The peer runs this indexed estimator slot in its own
                # unprivileged process between the paired observations.
                self._observe("post", run_index, sequence + 1)
            self._receive_exact_command(
                "cp2e_privileged_restore", RESTORE_SEQUENCE
            )
            try:
                self._restore(send_ack=True, inject_faults=True, reason="normal_request")
            except BaseException:
                self._emergency_restore("normal_restore_failure")
                raise
            return {
                "abnormal_recovery": self.abnormal_recovery,
                "binding_digest": self.expected_binding.digest,
                "control_evidence_seal_sha256": self._control_evidence_seal_sha256,
                "session_id": self.session_id,
                "state": self.state,
                "state_history": list(self.state_history),
            }
        except BaseException as exc:
            original_error = exc
            try:
                self._emergency_restore(type(exc).__name__)
            except RecoveryIndeterminate as recovery_error:
                original_error = recovery_error
                raise
            raise
        finally:
            closure_errors: List[BaseException] = []
            for closer in (
                self.backend.close, self.guardian_evidence_writer.close,
                self.control_evidence_writer.close,
            ):
                try:
                    closer()
                except BaseException as exc:
                    closure_errors.append(exc)
            closure_error: Optional[BaseException] = None
            if len(closure_errors) == 1:
                closure_error = closure_errors[0]
            elif closure_errors:
                closure_error = RecoveryIndeterminate(
                    "multiple privileged descriptor-closure operations failed: "
                    + ",".join(type(item).__name__ for item in closure_errors)
                )
            if self.state not in ("indeterminate", "closed"):
                if "closed" in _ALLOWED_TRANSITIONS[self.state]:
                    self._transition("closed")
            self._seal_terminal_receipt(original_error, closure_error)
            if closure_error is not None:
                raise RecoveryIndeterminate(
                    "privileged descriptor closure failed after {}: {}".format(
                        "normal execution" if original_error is None else repr(original_error),
                        closure_error,
                    )
                ) from closure_error


def recover_pending(
    expected_binding: Binding, backend: Any, journal_store: Any,
) -> Tuple[Mapping[str, Any], ...]:
    """Recover exact prior state after helper death, before a new session.

    A journal with another binding, an unparseable journal, or an unverifiable
    restore is indeterminate and remains present.  No new transaction may run.
    """

    results: List[Mapping[str, Any]] = []
    pending = journal_store.pending()
    if len(pending) > 1:
        raise RecoveryIndeterminate(
            "multiple pending privileged journals make prior-state order ambiguous"
        )
    for journal in pending:
        if journal.binding != expected_binding:
            raise RecoveryIndeterminate("pending recovery binding differs")
        prior = journal.prior
        try:
            prior_payload = canonical_control_state_bytes(dict(prior))
            prepare_recovery = getattr(
                backend, "prepare_external_recovery", None,
            )
            if prepare_recovery is None:
                raise RecoveryIndeterminate(
                    "backend lacks replacement-control-plane authentication"
                )
            recovery_identity = prepare_recovery(prior, journal.peer)
            canonical_json_bytes(dict(recovery_identity))
            journal.append("external_recovery_started", None)
            recover_guard = getattr(backend, "recover_population_guard", None)
            if recover_guard is None:
                raise RecoveryIndeterminate(
                    "backend lacks an explicit external population-guard recovery operation"
                )
            population_seal = recover_guard()
            if population_seal is not None:
                canonical_json_bytes(dict(population_seal))
            backend.restore(prior, lambda _label: None)
            validate_recovery = getattr(
                backend, "validate_external_recovery", None,
            )
            if validate_recovery is None:
                raise RecoveryIndeterminate(
                    "backend lacks observed external-recovery reconciliation"
                )
            reconciliation = _exact_keys(
                validate_recovery(prior), (
                    "persistent_state", "persistent_state_sha256",
                    "prior_state_sha256", "record_type", "schema_version",
                ), "external recovery reconciliation",
            )
            reconciliation_payload = canonical_control_state_bytes(
                dict(reconciliation)
            )
            if (
                reconciliation["record_type"]
                != "cp2e_external_recovery_validation"
                or reconciliation["schema_version"] != SCHEMA_VERSION
                or reconciliation["prior_state_sha256"]
                != hashlib.sha256(prior_payload).hexdigest()
                or reconciliation["persistent_state_sha256"]
                != hashlib.sha256(canonical_control_state_bytes(
                    reconciliation["persistent_state"]
                )).hexdigest()
            ):
                raise RecoveryIndeterminate(
                    "external recovery reconciliation identity/digest differs"
                )
            journal.append("external_recovery_validated", {
                "reconciliation_sha256": hashlib.sha256(
                    reconciliation_payload
                ).hexdigest(),
            })
            identifier = journal.identifier
            journal.complete()
            results.append({
                "journal_id": identifier,
                "prior_state_sha256": hashlib.sha256(prior_payload).hexdigest(),
                "reconciled_state_sha256": reconciliation[
                    "persistent_state_sha256"
                ],
            })
        except BaseException as exc:
            raise RecoveryIndeterminate(
                "pending privileged state could not be recovered exactly: " + str(exc)
            ) from exc
    return tuple(results)


_PRODUCTION_ARGUMENTS = (
    "--production-held-closure", "--data-free-reversibility-v1",
)
_PRODUCTION_FDS = {
    "CP2E_CLIENT_SOCKET_FD": 3,
    "CP2E_HELPER_SOURCE_FD": 198,
    "CP2E_BACKEND_SOURCE_FD": 199,
    "CP2E_PROFILE_CODEC_SOURCE_FD": 200,
    "CP2E_CANONICAL_PROFILE_FD": 202,
    "CP2E_LAUNCHER_FD": 203,
}
_PRODUCTION_SOURCE_DESTINATIONS = (
    "/opt/schurvio-cp2e/lib/cp2_timing_privileged_helper.py",
    "/opt/schurvio-cp2e/lib/cp2_timing_privileged_backend.py",
    "/opt/schurvio-cp2e/lib/cp2_timing_profile.py",
)


def _held_regular_payload(
    descriptor: int, maximum_bytes: int, label: str,
) -> Tuple[bytes, os.stat_result]:
    """Read one already-held root inode and prove it stayed identical."""

    try:
        before = os.fstat(descriptor)
    except OSError as exc:
        raise ProductionHelperUnavailable(label + " descriptor is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode) or before.st_uid != 0
        or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_size <= 0 or before.st_size > maximum_bytes
    ):
        raise ProductionHelperUnavailable(label + " held inode metadata differs")
    chunks: List[bytes] = []
    offset = 0
    while offset < before.st_size:
        chunk = os.pread(
            descriptor, min(65536, before.st_size - offset), offset,
        )
        if not chunk:
            raise ProductionHelperUnavailable(label + " held inode read ended early")
        chunks.append(chunk)
        offset += len(chunk)
    after = os.fstat(descriptor)
    identity = (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
        "st_size", "st_mtime_ns", "st_ctime_ns",
    )
    if any(getattr(before, key) != getattr(after, key) for key in identity):
        raise ProductionHelperUnavailable(label + " held inode changed while read")
    return b"".join(chunks), before


def _load_held_module(name: str, payload: bytes, descriptor: int) -> Any:
    """Execute an exact held source payload without consulting an import path."""

    if name in sys.modules:
        raise ProductionHelperUnavailable("held module name was already populated: " + name)
    module = type(sys)(name)
    module.__file__ = "/proc/self/fd/{}".format(descriptor)
    module.__package__ = ""
    module.__loader__ = None
    sys.modules[name] = module
    try:
        code = compile(payload, module.__file__, "exec", dont_inherit=True, optimize=0)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _binding_from_profile(profile: Any) -> Binding:
    helper = profile.value["privileged_helper"]
    return Binding(
        profile.sha256, helper["plan_sha256"], helper["source_sha256"],
        helper["protocol_core_sha256"], helper["root_launcher_sha256"],
        helper["import_closure_sha256"], helper["sudoers_sha256"],
    )


def _load_production_held_closure() -> Mapping[str, Any]:
    """Reconstruct every Python/profile/launcher binding from held FDs."""

    for name, descriptor in _PRODUCTION_FDS.items():
        if os.environ.get(name) != str(descriptor):
            raise ProductionHelperUnavailable(
                "root-owned launcher held-descriptor environment differs"
            )
    compiled_plan = os.environ.get("CP2E_PROFILE_PLAN_SHA256")
    try:
        _require_hash(compiled_plan, "compiled profile-plan digest")
    except PrivilegedHelperError as exc:
        raise ProductionHelperUnavailable(str(exc)) from exc

    source_payloads = []
    source_stats = []
    for descriptor, label in (
        (198, "privileged helper source"),
        (199, "privileged backend source"),
        (200, "profile codec source"),
    ):
        payload, status = _held_regular_payload(descriptor, 8 * 1024 * 1024, label)
        source_payloads.append(payload)
        source_stats.append(status)
    profile_payload, _profile_status = _held_regular_payload(
        202, 1024 * 1024, "canonical timing profile",
    )
    launcher_payload, _launcher_status = _held_regular_payload(
        203, 64 * 1024 * 1024, "root launcher",
    )

    current = sys.modules.get("cp2_timing_privileged_helper")
    if current is not None and current is not sys.modules[__name__]:
        raise ProductionHelperUnavailable("protocol-core module alias is already occupied")
    sys.modules["cp2_timing_privileged_helper"] = sys.modules[__name__]
    profile_codec = _load_held_module(
        "cp2_timing_profile", source_payloads[2], 200,
    )
    backend_module = _load_held_module(
        "cp2_timing_privileged_backend", source_payloads[1], 199,
    )
    frozen_profile = profile_codec.load_profile_bytes(profile_payload)
    helper = frozen_profile.value["privileged_helper"]
    if (
        helper["trusted_root"] != "/opt/schurvio-cp2e"
        or helper["root_launcher_path"]
        != "/opt/schurvio-cp2e/bin/cp2e-helper"
        or helper["installed_closure_file_count"] != 4
    ):
        raise ProductionHelperUnavailable("held profile production-root closure differs")
    if (
        profile_codec.profile_plan_sha256(frozen_profile.value) != compiled_plan
        or helper["plan_sha256"] != compiled_plan
    ):
        raise ProductionHelperUnavailable("held profile differs from compiled plan digest")

    source_rows = [
        {
            "destination": destination,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size_bytes": status.st_size,
        }
        for destination, payload, status in zip(
            _PRODUCTION_SOURCE_DESTINATIONS, source_payloads, source_stats,
        )
    ]
    closure = hashlib.sha256(canonical_json_bytes({
        "domain": "SchurVIO-CP2-E-root-Python-source-closure-v1",
        "files": source_rows,
    })).hexdigest()
    launcher_sha256 = hashlib.sha256(launcher_payload).hexdigest()
    if (
        helper["source_sha256"] != closure
        or helper["import_closure_sha256"] != closure
        or helper["protocol_core_sha256"] != source_rows[0]["sha256"]
        or helper["root_launcher_sha256"] != launcher_sha256
    ):
        raise ProductionHelperUnavailable("held source/launcher closure differs from profile")
    return {
        "backend_module": backend_module,
        "binding": _binding_from_profile(frozen_profile),
        "profile": frozen_profile,
        "source_rows": tuple(source_rows),
    }


def run_data_free_reversibility(
    binding: Binding, peer: PeerCredentials, backend: Any, journal_store: Any,
) -> Mapping[str, Any]:
    """Run one mutation-capable but recorded-input-free restoration trial.

    The durable prior precedes the first mutation.  A control/apply failure is
    a valid failed feasibility result only if the complete prior is restored
    byte-for-byte.  Any ambiguity leaves the pending journal authoritative and
    raises ``DataFreeReversibilityIndeterminate``.
    """

    pending = journal_store.pending()
    if pending:
        for item in pending:
            item.close()
        raise DataFreeReversibilityIndeterminate(
            "pending privileged journal requires recovery, not a new trial"
        )
    prior = backend.capture_prior()
    prior_bytes = canonical_control_state_bytes(dict(prior))
    journal = journal_store.create(
        binding, peer, hashlib.sha256(os.urandom(32)).hexdigest(), prior,
    )
    primary_error: Optional[BaseException] = None
    applied_sha256: Optional[str] = None
    feasibility_probe_sha256: Optional[str] = None
    try:
        journal.append("data_free_apply_started", None)

        def apply_boundary(label: str) -> None:
            journal.append("data_free_apply_boundary", {"label": label})

        backend.apply(prior, apply_boundary)
        applied = backend.validate_applied()
        applied_sha256 = hashlib.sha256(
            canonical_control_state_bytes(dict(applied))
        ).hexdigest()
        journal.append(
            "data_free_applied_validated", {"state_sha256": applied_sha256},
        )
        probe_operation = getattr(backend, "validate_data_free_feasibility", None)
        if probe_operation is None:
            raise PrivilegedHelperError(
                "backend lacks the exact data-free feasibility probe"
            )
        probe = probe_operation()
        if type(probe) is not dict:
            raise PrivilegedHelperError(
                "data-free feasibility probe is not a canonical mapping"
            )
        feasibility_probe_sha256 = hashlib.sha256(
            canonical_json_bytes(probe)
        ).hexdigest()
        journal.append(
            "data_free_feasibility_validated",
            {"probe_sha256": feasibility_probe_sha256},
        )
    except BaseException as exc:
        primary_error = exc

    try:
        journal.append("data_free_restore_started", None)

        def restore_boundary(label: str) -> None:
            journal.append("data_free_restore_boundary", {"label": label})

        backend.restore(prior, restore_boundary)
        restored = backend.validate_restored(prior)
        restored_bytes = canonical_control_state_bytes(dict(restored))
        if restored_bytes != prior_bytes:
            raise DataFreeReversibilityIndeterminate(
                "restored full state differs byte-for-byte from durable prior"
            )
        backend.close()
        restored_sha256 = hashlib.sha256(restored_bytes).hexdigest()
        journal.append(
            "data_free_restoration_validated",
            {"state_sha256": restored_sha256},
        )
        journal_id = journal.identifier
        journal.complete()
    except BaseException as exc:
        try:
            journal.close()
        except BaseException:
            pass
        raise DataFreeReversibilityIndeterminate(
            "data-free feasibility restoration is indeterminate"
        ) from exc

    return {
        "applied_state_sha256": applied_sha256,
        "binding": dict(binding.as_record()),
        "binding_digest": binding.digest,
        "checkpoint": "CP2-E",
        "formal_execution_locked": True,
        "feasibility_probe_sha256": feasibility_probe_sha256,
        "journal_id": journal_id,
        "prior_state_sha256": hashlib.sha256(prior_bytes).hexdigest(),
        "record_type": "cp2e_data_free_reversibility_receipt",
        "restored_state_sha256": restored_sha256,
        "restoration_byte_exact": True,
        "schema_version": SCHEMA_VERSION,
        "transaction_status": (
            "passed" if primary_error is None else "failed_but_exactly_restored"
        ),
    }


def _temporary_restoration_signal_handlers() -> Mapping[int, Any]:
    retained: Dict[int, Any] = {}

    def interrupt(signum: int, _frame: Any) -> None:
        raise InterruptedError(
            "data-free feasibility interrupted by signal {}".format(signum)
        )

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
        retained[signum] = signal.getsignal(signum)
        signal.signal(signum, interrupt)
    return retained


def production_main(_argv: Optional[Sequence[str]] = None) -> int:
    """Run only the fixed held-closure data-free reversibility transaction."""

    arguments = tuple(sys.argv[1:] if _argv is None else _argv)
    if arguments != _PRODUCTION_ARGUMENTS:
        raise ProductionHelperUnavailable(
            "exact root-owned held launcher/source/profile closure is required; "
            "formal CP2-E execution is locked"
        )
    if os.getuid() != 0 or os.geteuid() != 0:
        raise ProductionHelperUnavailable(
            "root-owned launcher requires real/effective uid zero"
        )
    closure = _load_production_held_closure()
    connection = socket.socket(fileno=_PRODUCTION_FDS["CP2E_CLIENT_SOCKET_FD"])
    store = None
    backend = None
    handlers: Optional[Mapping[int, Any]] = None
    try:
        if connection.family != socket.AF_UNIX or _socket_type(connection) != socket.SOCK_SEQPACKET:
            raise ProductionHelperUnavailable("held client socket type differs")
        peer = socket_peer_credentials(connection)
        if peer.pid <= 0 or peer.uid == 0:
            raise ProductionHelperUnavailable("held client peer credentials differ")
        profile = closure["profile"]
        binding = closure["binding"]
        backend = closure["backend_module"].PrivilegedTimingBackend(profile, peer)
        store = DurableJournalStore(
            profile.value["privileged_helper"]["journal_directory"],
            expected_uid=0,
        )
        pending = store.pending()
        if pending:
            for item in pending:
                item.close()
            recovery = recover_pending(binding, backend, store)
            send_packet(connection, {
                "binding": dict(binding.as_record()),
                "checkpoint": "CP2-E",
                "formal_execution_locked": True,
                "record_type": "cp2e_data_free_recovery_receipt",
                "recovered": [dict(item) for item in recovery],
                "schema_version": SCHEMA_VERSION,
                "transaction_status": "recovered_prior_only_no_retry",
            })
            return 75
        handlers = _temporary_restoration_signal_handlers()
        receipt = run_data_free_reversibility(binding, peer, backend, store)
        send_packet(connection, receipt)
        return 0 if receipt["transaction_status"] == "passed" else 75
    except RecoveryIndeterminate:
        try:
            send_packet(connection, {
                "checkpoint": "CP2-E", "formal_execution_locked": True,
                "record_type": "cp2e_data_free_reversibility_indeterminate",
                "schema_version": SCHEMA_VERSION,
                "transaction_status": "indeterminate_pending_journal_retained",
            })
        except BaseException:
            pass
        return 76
    finally:
        if handlers is not None:
            for signum, prior_handler in handlers.items():
                signal.signal(signum, prior_handler)
        if backend is not None:
            try:
                backend.close()
            except BaseException:
                pass
        if store is not None:
            store.close()
        connection.close()


if __name__ == "__main__":  # pragma: no cover - root-held execution seam
    raise SystemExit(production_main())
