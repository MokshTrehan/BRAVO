#!/usr/bin/python3.8
"""ABLATE-REC-1 recovery-mechanism ablation driver (docs/icra27/ABLATION_PREREG.md).

Thin extension of the PERTURB-1 driver: it reuses the frozen CDSC-1R4 matrix
(bag identities, frozen native start offsets, ground-truth paths), the CDSC-1R4
trial runners with their PERTURB-1 arguments (offset in camera frames), the
CDSC event log and checksum-closure validator, and evaluates every closed cell
with the frozen per-cell accuracy math (D8).  It adds exactly the declared
single-delta systems: S1-recOFF and N0-recOFF (frozen S1 / N0 launch with the
one added ROS parameter long_gap_recovery_enabled=false) plus the U0 stock
context rows on the rotation family.

Per recOFF cell the driver records (a) the machine-verified single-delta
resolved-ROS-parameter diff against the PERTURB-1 recON counterpart cell,
(b) byte-identity of the trajectory products against that counterpart (P2),
(c) the resolved recovery parameter value.  recON integrity replicas are
byte-compared against the PERTURB-1 recorded SHA256SUMS (P3) and, for S1,
CDSC-1R4.  U0 offset-0 cells are byte-compared against CDSC-1R4.

Rules enforced here: estimator source, configs, thresholds and the prereg are
never touched; a failed run is retained and typed, never rerun; every cell
records its command line, config/launch/binary identities, offset, runner git
identity and wall-clock timestamps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_campaign as campaign  # noqa: E402
import perturbation_campaign as perturb  # noqa: E402

PYTHON = campaign.PYTHON
CAMPAIGN_ID = "ABLATE-REC-1"
PREREG_FILE = REPO_ROOT / "docs" / "icra27" / "ABLATION_PREREG.md"
DISTRIBUTION_FILE = REPO_ROOT / "docs" / "icra27" / "recovery_event_distribution.md"
CELL_EVALUATOR = SCRIPT_DIR / "perturbation_cell_evaluator.py"
AGGREGATOR = SCRIPT_DIR / "ablation_aggregate.py"
CDSC1R4_ROOT = perturb.CDSC1R4_ROOT
PERTURB1_ROOT = Path(
    "/home/moksh/schurvio-icra27-artifacts/perturbation-campaign/perturb1-20260816T205717Z"
)
CELL_SCHEMA = "schurvio.icra27.ablation.cell_record.v1"

FRAME_RATE_HZ = perturb.FRAME_RATE_HZ
OFFSETS: Tuple[int, ...] = (0, 5, 10)
RECOFF_SYSTEMS: Tuple[str, ...] = ("S1-recOFF", "N0-recOFF")
RECON_OF = {"S1-recOFF": "S1", "N0-recOFF": "N0"}
S1_BINARY_SYSTEMS = ("S1", "N0", "S1-recOFF", "N0-recOFF")
RECOVERY_SWITCH_PARAMETER = "long_gap_recovery_enabled"
ROTATION_FAMILY: Tuple[str, ...] = ("rotation/rotation.bag", "rotation/rotation_fast.bag")
REMAINING_SEQUENCES: Tuple[str, ...] = (
    "square/square_fast.bag",
    "circle/circle_fast.bag",
    "square/square_head.bag",
    "MH_05_difficult",
    "V2_03_difficult",
)
SETS: Tuple[str, ...] = ("integrity", "p1-crux", "u0-context", "recoff-remaining")
TRAJECTORY_FILES = perturb.TRAJECTORY_FILES
LAUNCH_FILES = {
    ("kaist_vio", "S1-recOFF"): REPO_ROOT / "project" / "icra27_kaist_s1_recoff_serial.launch",
    ("kaist_vio", "N0-recOFF"): REPO_ROOT / "project" / "icra27_kaist_n0_recoff_serial.launch",
    ("euroc_mav", "S1-recOFF"): REPO_ROOT / "project" / "icra27_cross_dataset_s1_recoff_serial.launch",
    ("euroc_mav", "N0-recOFF"): REPO_ROOT / "project" / "icra27_cross_dataset_n0_recoff_serial.launch",
    ("kaist_vio", "N0"): REPO_ROOT / "project" / "icra27_kaist_n0_serial.launch",
    ("euroc_mav", "N0"): REPO_ROOT / "project" / "icra27_cross_dataset_n0_serial.launch",
}
RUN_OWNED_OUTPUT_SUFFIXES = frozenset(("filepath_est", "filepath_std", "record_timing_filepath"))
STRICT_BOOL_RE = re.compile(r"^\s*long_gap_recovery_enabled\s*:\s*(true|false)\s*(#.*)?$", re.M)


class AblationError(RuntimeError):
    pass


def utc_now() -> str:
    return campaign.utc_now()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, payload: bytes) -> None:
    campaign._atomic_write_new(path, payload)


def _write_sha256sums(directory: Path) -> None:
    perturb._write_sha256sums(directory)


def _safe(sequence: str) -> str:
    return campaign.safe_sequence(sequence)


def _offset_token(offset: int) -> str:
    return perturb._offset_token(offset)


class Cell:
    def __init__(self, set_name: str, row: campaign.MatrixRow, system: str, offset_frames: int) -> None:
        self.set_name = set_name
        self.row = row
        self.system = system
        self.axis = "offset"
        self.seed_label = "frozen"
        self.offset_frames = offset_frames
        self.frame_rate_hz = FRAME_RATE_HZ[row.dataset]
        self.frozen_start = float(row.bag_start_seconds)
        self.shift_seconds = float(offset_frames) / self.frame_rate_hz
        self.estimator_start = self.frozen_start + self.shift_seconds
        self.run_id = "ablate1-{}{:02d}-{}-{}-offset-{}-a1".format(
            "integrity-" if set_name == "integrity" else "",
            row.order,
            _safe(row.sequence),
            system.lower(),
            _offset_token(offset_frames),
        )
        self.key = campaign.row_key(row)

    @property
    def recoff(self) -> bool:
        return self.system in RECOFF_SYSTEMS

    @property
    def runnable(self) -> Tuple[bool, Optional[str]]:
        if self.estimator_start < 0.0:
            return False, (
                "NO_LEAD_IN_DATA: frozen replay start {!r} s + {} frames / {} Hz = {!r} s precedes "
                "the all-topic bag begin (PERTURB-1 D3)".format(
                    self.frozen_start, self.offset_frames, self.frame_rate_hz, self.estimator_start
                )
            )
        return True, None

    def paths(self, root: Path) -> Dict[str, Path]:
        base = root / "cells" / self.set_name / self.key / self.system
        return {
            "output_root": base,
            "run_directory": base / self.run_id,
            "result": base / self.run_id / "sequence_result.json",
            "driver": base / (self.run_id + ".driver"),
        }

    def describe(self) -> Dict[str, Any]:
        runnable, reason = self.runnable
        return {
            "set": self.set_name,
            "order": self.row.order,
            "dataset": self.row.dataset,
            "sequence": self.row.sequence,
            "sequence_key": self.key,
            "system": self.system,
            "recovery_ablated": self.recoff,
            "recon_counterpart_system": RECON_OF.get(self.system),
            "axis": self.axis,
            "offset_frames": self.offset_frames,
            "frame_rate_hz": self.frame_rate_hz,
            "shift_seconds": self.shift_seconds,
            "seed_label": self.seed_label,
            "frozen_matrix_bag_start_seconds": self.frozen_start,
            "estimator_bag_start_seconds": self.estimator_start,
            "estimator_bag_start_seconds_repr": repr(self.estimator_start),
            "runnable": runnable,
            "not_runnable_reason": reason,
            "run_id": self.run_id,
        }


def plan_cells(rows: Sequence[campaign.MatrixRow], sets: Sequence[str]) -> List[Cell]:
    by_sequence = {row.sequence: row for row in rows}
    cells: List[Cell] = []
    if "integrity" in sets:
        cells.append(Cell("integrity", by_sequence["rotation/rotation.bag"], "S1", 0))
        cells.append(Cell("integrity", by_sequence["rotation/rotation.bag"], "N0", 0))
        cells.append(Cell("integrity", by_sequence["MH_05_difficult"], "S1", 0))
    if "p1-crux" in sets:
        for sequence in ROTATION_FAMILY:
            for offset in OFFSETS:
                for system in RECOFF_SYSTEMS:
                    cells.append(Cell("p1-crux", by_sequence[sequence], system, offset))
    if "u0-context" in sets:
        for sequence in ROTATION_FAMILY:
            for offset in OFFSETS:
                cells.append(Cell("u0-context", by_sequence[sequence], "U0", offset))
    if "recoff-remaining" in sets:
        for sequence in REMAINING_SEQUENCES:
            for offset in OFFSETS:
                for system in RECOFF_SYSTEMS:
                    cells.append(Cell("recoff-remaining", by_sequence[sequence], system, offset))
    return cells


def build_command(paths: campaign.RuntimePaths, cell: Cell, root: Path, options: Mapping[str, Any]) -> List[str]:
    row = cell.row
    location = cell.paths(root)
    runner = paths.kaist_runner if row.dataset == "kaist_vio" else paths.generic_runner
    binary_system = "S1" if cell.system in S1_BINARY_SYSTEMS else "U0"
    config = campaign._config_path(paths, row.dataset, binary_system)
    launch = LAUNCH_FILES.get((row.dataset, cell.system)) or campaign._launch_path(paths, row.dataset, binary_system)
    inner = [
        str(PYTHON),
        str(runner),
        "run",
        "--protocol-id",
        campaign.PROTOCOL_ID,
        "--protocol-file",
        str(paths.protocol),
        "--matrix-file",
        str(paths.matrix),
        "--run-id",
        cell.run_id,
        "--attempt-index",
        "1",
        "--dataset",
        row.dataset,
        "--sequence",
        row.sequence,
        "--system",
        cell.system,
        "--mode",
        "scored",
        "--bag",
        str(row.bag["path"]),
        "--bag-start",
        format(row.bag_start_seconds, ".17g"),
        "--bag-duration",
        "-1",
        "--config",
        str(config),
        "--launch",
        str(launch),
        "--binary",
        str(campaign._binary_path(paths, binary_system)),
        "--output-root",
        str(location["output_root"]),
        "--ros-port",
        str(options["ros_port"]),
        "--timeout-seconds",
        format(float(options["timeout_seconds"]), ".17g"),
        "--cpu-list",
        str(options["cpu_list"]),
        "--perturbation-campaign-id",
        CAMPAIGN_ID,
        "--perturbation-offset-frames",
        str(cell.offset_frames),
        "--perturbation-frame-rate-hz",
        format(cell.frame_rate_hz, ".17g"),
        "--perturbation-seed-label",
        cell.seed_label,
        "--perturbation-axis",
        cell.axis,
    ]
    return campaign.sourced_command(campaign._setup_path(paths, binary_system), inner)


def _read_sums(path: Path) -> Dict[str, str]:
    return perturb._read_sums(path)


def _trajectory_sums(run_directory: Path) -> Dict[str, Optional[str]]:
    sums = _read_sums(run_directory / "SHA256SUMS") if (run_directory / "SHA256SUMS").is_file() else {}
    return {name: sums.get(name) for name in TRAJECTORY_FILES}


def _live_trajectory_sums(run_directory: Path) -> Dict[str, Optional[str]]:
    return {
        name: (campaign.sha256_file(run_directory / name) if (run_directory / name).is_file() else None)
        for name in TRAJECTORY_FILES
    }


def _compare(fresh: Mapping[str, Optional[str]], reference_dir: Optional[Path]) -> Dict[str, Any]:
    if reference_dir is None or not (reference_dir / "SHA256SUMS").is_file():
        return {"status": "REFERENCE_ABSENT", "reference_run_directory": None if reference_dir is None else str(reference_dir)}
    expected = _trajectory_sums(reference_dir)
    live = _live_trajectory_sums(reference_dir)
    complete = all(fresh[name] is not None for name in TRAJECTORY_FILES) and all(
        expected[name] is not None for name in TRAJECTORY_FILES
    )
    identical = complete and dict(fresh) == expected
    return {
        "reference_run_directory": str(reference_dir),
        "expected_trajectory_sha256": expected,
        "reference_live_sha256_matches_recorded": live == expected,
        "fresh_products_complete": all(fresh[name] is not None for name in TRAJECTORY_FILES),
        "reference_products_complete": all(expected[name] is not None for name in TRAJECTORY_FILES),
        "byte_identical": bool(identical),
        "status": "BYTE_IDENTICAL" if identical else "MISMATCH",
    }


_PERTURB1_INDEX: Optional[Dict[Tuple[str, str, int], Mapping[str, Any]]] = None


def perturb1_counterpart(sequence: str, system: str, offset: int) -> Optional[Mapping[str, Any]]:
    """PERTURB-1 offset-axis matrix cell (never the integrity lane) for (sequence, recON system, offset)."""

    global _PERTURB1_INDEX
    if _PERTURB1_INDEX is None:
        index: Dict[Tuple[str, str, int], Mapping[str, Any]] = {}
        ledger = PERTURB1_ROOT / "driver" / "cells.jsonl"
        for line in ledger.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("outcome") != "CLOSED" or record.get("axis") != "offset" or record.get("set") == "integrity":
                continue
            key = (str(record["sequence"]), str(record["system"]), int(record["offset_frames"]))
            if key in index:
                raise AblationError("PERTURB-1 ledger has duplicate matrix cell {}".format(key))
            index[key] = record
        _PERTURB1_INDEX = index
    return _PERTURB1_INDEX.get((sequence, system, offset))


def _resolved_map(run_directory: Path) -> Optional[Dict[str, Any]]:
    dump = run_directory / "diagnostics" / "resolved_ros_parameters.yaml"
    if not dump.is_file():
        return None
    value = yaml.safe_load(dump.read_text())
    return dict(value) if isinstance(value, dict) else None


def single_delta_diff(fresh_dir: Path, counterpart_dir: Path, namespace_hint: Optional[str] = None) -> Dict[str, Any]:
    """Machine-verified resolved-parameter diff recOFF vs recON counterpart.

    Passes iff the only difference (ignoring the run-owned output paths, whose
    relative tails must nevertheless agree) is the one added key
    <namespace>/long_gap_recovery_enabled == False.
    """

    fresh = _resolved_map(fresh_dir)
    ref = _resolved_map(counterpart_dir)
    if fresh is None or ref is None:
        return {"status": "RESOLVED_PARAMETERS_ABSENT", "fresh_present": fresh is not None, "counterpart_present": ref is not None}
    changed: Dict[str, Any] = {}
    output_tails: Dict[str, Any] = {}
    for name in sorted(set(fresh) | set(ref)):
        suffix = name.rsplit("/", 1)[-1]
        if suffix in RUN_OWNED_OUTPUT_SUFFIXES:
            f_tail = "/".join(str(fresh.get(name)).split("/")[-2:]) if name in fresh else None
            r_tail = "/".join(str(ref.get(name)).split("/")[-2:]) if name in ref else None
            output_tails[name] = {"fresh_tail": f_tail, "counterpart_tail": r_tail, "equal": f_tail == r_tail}
            continue
        if (name in fresh) != (name in ref) or fresh.get(name) != ref.get(name):
            changed[name] = {
                "recOFF": fresh.get(name, "<absent>") if name in fresh else "<absent>",
                "recON": ref.get(name, "<absent>") if name in ref else "<absent>",
            }
    switch_keys = [k for k in changed if k.rsplit("/", 1)[-1] == RECOVERY_SWITCH_PARAMETER]
    single = (
        len(changed) == 1
        and len(switch_keys) == 1
        and changed[switch_keys[0]]["recOFF"] is False
        and changed[switch_keys[0]]["recON"] == "<absent>"
        and all(v["equal"] for v in output_tails.values())
    )
    return {
        "status": "SINGLE_DELTA" if single else "NOT_SINGLE_DELTA",
        "single_delta": bool(single),
        "changed_parameters_excluding_run_owned_output_paths": changed,
        "run_owned_output_path_tails": output_tails,
        "fresh_parameter_count": len(fresh),
        "counterpart_parameter_count": len(ref),
        "expected_delta": "{<ns>/long_gap_recovery_enabled: recOFF=False, recON=<absent (YAML governs)>}",
    }


def resolved_recovery_parameter(cell: Cell, run_directory: Path, config_path: Optional[str]) -> Dict[str, Any]:
    resolved = _resolved_map(run_directory) or {}
    ros_value: Any = "<absent>"
    for name, value in resolved.items():
        if name.rsplit("/", 1)[-1] == RECOVERY_SWITCH_PARAMETER:
            ros_value = value
    yaml_value: Any = "<absent>"
    if config_path and Path(config_path).is_file():
        match = STRICT_BOOL_RE.search(Path(config_path).read_text(errors="replace"))
        if match:
            yaml_value = match.group(1) == "true"
    if ros_value != "<absent>":
        effective = bool(ros_value)
        source = "ROS_PARAMETER_OVERRIDE"
    elif yaml_value != "<absent>":
        effective = bool(yaml_value)
        source = "YAML"
    else:
        effective = False
        source = "EXECUTABLE_DEFAULT_FALSE"
    return {
        "parameter": RECOVERY_SWITCH_PARAMETER,
        "ros_parameter_value": ros_value,
        "yaml_value": yaml_value,
        "effective_value": effective,
        "effective_source": source,
        "expected_effective_value": (False if cell.recoff else (True if cell.row.dataset == "kaist_vio" and cell.system in ("S1", "N0") else False)),
    }


def determinism_check(cell: Cell, run_directory: Path, root: Path) -> Dict[str, Any]:
    fresh = _live_trajectory_sums(run_directory)
    recorded = _trajectory_sums(run_directory)
    record: Dict[str, Any] = {
        "fresh_trajectory_sha256": recorded,
        "fresh_live_sha256_matches_recorded": fresh == recorded,
        "against_cdsc1r4": None,
        "against_perturb1_same_system": None,
        "against_perturb1_recon_counterpart": None,
    }
    if cell.offset_frames == 0 and cell.system in ("S1", "U0"):
        reference = perturb.cdsc1r4_scored_directory(cell.row, cell.system)
        record["against_cdsc1r4"] = _compare(recorded, reference)
    if cell.system in ("S1", "N0"):
        counterpart = perturb1_counterpart(cell.row.sequence, cell.system, cell.offset_frames)
        record["against_perturb1_same_system"] = _compare(
            recorded, Path(str(counterpart["run_directory"])) if counterpart else None
        )
        if counterpart:
            record["against_perturb1_same_system"]["perturb1_run_id"] = counterpart.get("run_id")
    if cell.recoff:
        counterpart = perturb1_counterpart(cell.row.sequence, RECON_OF[cell.system], cell.offset_frames)
        comparison = _compare(recorded, Path(str(counterpart["run_directory"])) if counterpart else None)
        if counterpart:
            comparison["perturb1_run_id"] = counterpart.get("run_id")
            descriptive = counterpart.get("perturbation_recovery_descriptive")
            summary = (descriptive or {}).get("summary") if isinstance(descriptive, dict) else None
            activations = summary.get("activations") if isinstance(summary, dict) else None
            comparison["counterpart_recovery_activations"] = activations
            comparison["counterpart_recovery_descriptive_present"] = descriptive is not None
            # EuRoC counterparts have no descriptive block; the frozen EuRoC S1
            # configuration never enables recovery, so zero events is a fact
            # (docs/icra27/recovery_event_distribution.md).
            comparison["counterpart_zero_recovery_events"] = bool(
                activations == 0 or (descriptive is None and cell.row.dataset == "euroc_mav")
            )
        record["against_perturb1_recon_counterpart"] = comparison
    return record


def validate_closed_result(cell: Cell, result_path: Path) -> Dict[str, Any]:
    if result_path.is_symlink() or result_path.parent.is_symlink():
        raise AblationError("sequence result path is a symlink")
    value, identity = campaign.load_json(result_path, "sequence result")
    run_directory = result_path.parent.resolve(strict=True)
    checksummed = campaign._validate_checksum_set(run_directory, result_path)
    expected = {
        "schema": campaign.RUN_SCHEMA,
        "protocol_id": campaign.PROTOCOL_ID,
        "run_id": cell.run_id,
        "attempt_index": 1,
        "dataset": cell.row.dataset,
        "sequence": cell.row.sequence,
        "system": cell.system,
        "mode": "scored",
    }
    differences = {k: (value.get(k), v) for k, v in expected.items() if value.get(k) != v}
    if differences:
        raise AblationError("sequence result binding mismatch: {}".format(differences))
    perturbation = value.get("perturbation")
    if not isinstance(perturbation, dict):
        raise AblationError("sequence result lacks the perturbation record")
    if (
        perturbation.get("campaign_id") != CAMPAIGN_ID
        or perturbation.get("offset_frames") != cell.offset_frames
        or perturbation.get("seed_label") != cell.seed_label
        or perturbation.get("axis") != cell.axis
        or float(perturbation.get("estimator_bag_start_seconds", math.nan)) != cell.estimator_start
        or bool(perturbation.get("recovery_ablated")) != cell.recoff
    ):
        raise AblationError("sequence result perturbation record differs from the planned cell")
    status = value.get("status")
    if status not in campaign.ALL_STATUSES or status == "ACTIVE":
        raise AblationError("sequence result status is unknown or not terminal: {}".format(status))
    validity = value.get("evidence_validity")
    if validity not in campaign.EVIDENCE_VALIDITIES:
        raise AblationError("sequence result evidence_validity is unknown")
    close = value.get("estimator_close_receipt") or {}
    classification = (
        "retained_algorithm_outcome"
        if status in campaign.RETAINED_ALGORITHM_STATUSES and validity == "VALID"
        else "fatal"
    )
    if classification == "retained_algorithm_outcome" and (
        close.get("estimator_process_group_closed") is not True
        or close.get("runtime_services_closed") is not True
    ):
        raise AblationError("retained outcome lacks unambiguous estimator closure")
    return {
        "value": value,
        "identity": identity,
        "classification": classification,
        "checksummed_paths": sorted(checksummed),
        "run_directory": run_directory,
    }


def typed_failure_class(value: Mapping[str, Any]) -> Optional[str]:
    return perturb.typed_failure_class(value)


class Driver:
    def __init__(self, root: Path, options: Mapping[str, Any]) -> None:
        self.root = root
        self.options = dict(options)
        self.paths = campaign.RuntimePaths()
        self.rows = campaign.load_matrix(self.paths.matrix)
        self.events = campaign.EventLog(root)
        self.ledger = root / "driver" / "cells.jsonl"
        self.log_dir = root / "driver" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.last_end = 0.0
        self.port_index = 0

    def _git(self) -> Mapping[str, Any]:
        identity = campaign.git_identity(REPO_ROOT)
        expected = self.options.get("expected_tooling_commit")
        if identity["dirty"]:
            raise AblationError("tooling repository is dirty (including untracked files); refusing to run")
        if expected and identity["head_sha"] != expected:
            raise AblationError("tooling HEAD {} differs from the pinned {}".format(identity["head_sha"], expected))
        return identity

    def _cooldown(self) -> None:
        remaining = self.options["cooldown_seconds"] - (time.monotonic() - self.last_end)
        if remaining > 0:
            time.sleep(remaining)

    def _append_ledger(self, record: Mapping[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, allow_nan=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _publish_driver_record(self, cell: Cell, record: Mapping[str, Any]) -> None:
        driver_dir = cell.paths(self.root)["driver"]
        driver_dir.mkdir(parents=True, exist_ok=True)
        _write_new(driver_dir / "cell_record.json", _json_bytes(record))
        _write_sha256sums(driver_dir)
        self._append_ledger(record)
        self.events.append("cell-" + cell.run_id, {"cell": record})

    def _next_port(self) -> int:
        port = int(self.options["base_ros_port"]) + self.port_index
        self.port_index += 1
        return port

    def run_cell(self, cell: Cell) -> Mapping[str, Any]:
        location = cell.paths(self.root)
        driver_record_path = location["driver"] / "cell_record.json"
        if driver_record_path.is_file():
            value, _ = campaign.load_json(driver_record_path, "cell record")
            return value  # already closed in an earlier invocation
        record: Dict[str, Any] = {
            "schema": CELL_SCHEMA,
            "campaign_id": CAMPAIGN_ID,
            **cell.describe(),
            "planned_utc": utc_now(),
        }
        runnable, reason = cell.runnable
        if not runnable:
            record.update(
                {
                    "outcome": "NOT_RUNNABLE",
                    "status": "NOT_RUNNABLE",
                    "not_runnable_reason": reason,
                    "estimator_attempted": False,
                    "started_utc": None,
                    "finished_utc": None,
                }
            )
            self._publish_driver_record(cell, record)
            return record
        if location["run_directory"].exists() and not location["result"].is_file():
            record.update(
                {
                    "outcome": "UNCLOSED_RUN_DIRECTORY_RETAINED",
                    "status": "INTERRUPTED",
                    "estimator_attempted": None,
                    "note": "run directory exists without a published sequence_result.json; retained, not rerun",
                }
            )
            self._publish_driver_record(cell, record)
            return record
        if location["result"].is_file():
            validated = validate_closed_result(cell, location["result"])
            record["note"] = "closed sequence_result found without a driver record; adopted after validation"
            return self._close_cell(cell, record, None, validated)
        self._cooldown()
        git_before = self._git()
        port = self._next_port()
        command = build_command(self.paths, cell, self.root, {**self.options, "ros_port": port})
        stdout_path = self.log_dir / (cell.run_id + ".stdout.log")
        stderr_path = self.log_dir / (cell.run_id + ".stderr.log")
        record.update(
            {
                "command": command,
                "ros_port": port,
                "tooling_git_before_launch": git_before,
                "started_utc": utc_now(),
                "started_monotonic": time.monotonic(),
            }
        )
        with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
            completed = subprocess.run(command, check=False, stdout=out, stderr=err)
        self.last_end = time.monotonic()
        record.update(
            {
                "finished_utc": utc_now(),
                "wall_seconds": time.monotonic() - record.pop("started_monotonic"),
                "returncode": completed.returncode,
                "stdout_log": str(stdout_path),
                "stderr_log": str(stderr_path),
                "tooling_git_after_close": campaign.git_identity(REPO_ROOT),
            }
        )
        if not location["result"].is_file():
            record.update(
                {
                    "outcome": "RUNNER_PUBLISHED_NO_RESULT",
                    "status": "INFRASTRUCTURE_FAILED",
                    "estimator_attempted": None,
                    "stderr_tail": stderr_path.read_text(errors="replace")[-4000:],
                }
            )
            self._publish_driver_record(cell, record)
            return record
        validated = validate_closed_result(cell, location["result"])
        return self._close_cell(cell, record, completed.returncode, validated)

    def _close_cell(
        self,
        cell: Cell,
        record: Dict[str, Any],
        returncode: Optional[int],
        validated: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        value = validated["value"]
        run_directory = validated["run_directory"]
        inputs = value.get("inputs") or {}
        record.update(
            {
                "outcome": "CLOSED",
                "estimator_attempted": bool((value.get("estimator_close_receipt") or {}).get("estimator_attempted")),
                "run_directory": str(run_directory),
                "sequence_result_sha256": validated["identity"].get("sha256"),
                "classification": validated["classification"],
                "status": value.get("status"),
                "evidence_validity": value.get("evidence_validity"),
                "strict_process_health": value.get("strict_process_health"),
                "passage_complete": bool((value.get("passage") or {}).get("complete")),
                "passage_reason": (value.get("passage") or {}).get("reason"),
                "typed_failure_class": typed_failure_class(value),
                "accuracy_eligible": bool(value.get("accuracy_eligible")),
                "config_identity": inputs.get("config"),
                "launch_identity": inputs.get("launch"),
                "binary_identity": inputs.get("binary"),
                "config_dependencies": inputs.get("config_dependencies"),
                "runner_identity": inputs.get("runner"),
                "estimator_argv": value.get("estimator_argv"),
                "resolved_parameters_sha256": (
                    (value.get("resolved_parameters") or {}).get("artifact", {}) or {}
                ).get("sha256"),
                "resolved_parameters_count": (value.get("resolved_parameters") or {}).get("parameter_count"),
                "runner_started_utc": value.get("started_utc"),
                "runner_finished_utc": value.get("finished_utc"),
                "runner_duration_seconds": value.get("duration_seconds"),
                "robustness_mechanism_status": (value.get("robustness_mechanism") or {}).get("status"),
                "robustness_mechanism_reason": (value.get("robustness_mechanism") or {}).get("reason"),
                "perturbation_recovery_descriptive": value.get("perturbation_recovery_descriptive"),
                "completion": {
                    key: (value.get("completion") or {}).get(key)
                    for key in (
                        "pass",
                        "tail_gap_pass",
                        "maximum_state_gap_pass",
                        "maximum_state_gap_s",
                        "tail_gap_s",
                        "unsupported_state_gaps",
                        "supported_input_gap_count",
                        "recovery_supported_state_gap_count",
                    )
                }
                if isinstance(value.get("completion"), dict)
                else None,
                "passage": value.get("passage"),
                "outcome_facts": value.get("outcome_facts"),
                "input_interval_selected_pair_count": (value.get("input_interval") or {}).get("selected_pair_count"),
                "stage_errors": value.get("stage_errors"),
                "failure": value.get("failure"),
            }
        )
        if returncode is not None:
            expected_exit = 0 if value.get("status") in campaign.ELIGIBLE_STATUSES else 2
            record["runner_exit_contract_ok"] = returncode == expected_exit
        config_identity = inputs.get("config") or {}
        record["resolved_recovery_parameter"] = resolved_recovery_parameter(
            cell, run_directory, config_identity.get("path") if isinstance(config_identity, dict) else None
        )
        record["determinism"] = determinism_check(cell, run_directory, self.root)
        if cell.recoff:
            counterpart = perturb1_counterpart(cell.row.sequence, RECON_OF[cell.system], cell.offset_frames)
            if counterpart is None:
                record["single_delta_vs_recon_counterpart"] = {"status": "COUNTERPART_ABSENT"}
            else:
                diff = single_delta_diff(run_directory, Path(str(counterpart["run_directory"])))
                diff["counterpart_run_id"] = counterpart.get("run_id")
                diff["counterpart_run_directory"] = counterpart.get("run_directory")
                diff["counterpart_config_sha256"] = (counterpart.get("config_identity") or {}).get("sha256")
                diff["fresh_config_sha256"] = config_identity.get("sha256") if isinstance(config_identity, dict) else None
                diff["config_identical"] = (
                    diff["counterpart_config_sha256"] is not None
                    and diff["counterpart_config_sha256"] == diff["fresh_config_sha256"]
                )
                diff["counterpart_binary_sha256"] = (counterpart.get("binary_identity") or {}).get("sha256")
                fresh_binary = inputs.get("binary") or {}
                diff["binary_identical"] = (
                    diff["counterpart_binary_sha256"] is not None
                    and diff["counterpart_binary_sha256"] == fresh_binary.get("sha256")
                )
                diff["machine_verified_single_delta"] = bool(
                    diff.get("single_delta") and diff["config_identical"] and diff["binary_identical"]
                )
                record["single_delta_vs_recon_counterpart"] = diff
        # Ground truth is opened only now, after the runner proved estimator closure.
        metrics_dir = cell.paths(self.root)["driver"] / "metrics"
        cell.paths(self.root)["driver"].mkdir(parents=True, exist_ok=True)
        gt_path = Path(str(cell.row.ground_truth["canonical_path"]))
        eval_cmd = [
            str(PYTHON),
            str(CELL_EVALUATOR),
            "--run",
            str(run_directory),
            "--ground-truth",
            str(gt_path),
            "--output",
            str(metrics_dir),
        ]
        eval_log = self.log_dir / (cell.run_id + ".metrics.log")
        with eval_log.open("ab") as stream:
            eval_completed = subprocess.run(eval_cmd, check=False, stdout=stream, stderr=subprocess.STDOUT)
        record["metrics_command"] = eval_cmd
        record["metrics_returncode"] = eval_completed.returncode
        metrics_path = metrics_dir / "cell_metrics.json"
        if metrics_path.is_file():
            metrics_value, metrics_identity = campaign.load_json(metrics_path, "cell metrics")
            record["metrics_status"] = metrics_value.get("metrics_status")
            record["metrics"] = metrics_value.get("metrics")
            record["metrics_population"] = metrics_value.get("population")
            record["cell_metrics_sha256"] = metrics_identity.get("sha256")
        else:
            record["metrics_status"] = "EVALUATOR_FAILED"
            record["metrics"] = None
            record["metrics_error_tail"] = eval_log.read_text(errors="replace")[-2000:]
        self._publish_driver_record(cell, record)
        return record

    def checkpoint(self, label: str) -> Mapping[str, Any]:
        output = self.root / "aggregate" / "checkpoint-{}-{}".format(label, dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"))
        command = [str(PYTHON), str(AGGREGATOR), "--artifact-root", str(self.root), "--output", str(output)]
        log = self.log_dir / ("aggregate-{}.log".format(output.name))
        with log.open("ab") as stream:
            completed = subprocess.run(command, check=False, stdout=stream, stderr=subprocess.STDOUT)
        receipt = {"command": command, "returncode": completed.returncode, "output": str(output), "log": str(log)}
        self.events.append("checkpoint-" + output.name, receipt)
        return receipt


def integrity_gate(root: Path, records: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    checks = []
    for record in records:
        det = record.get("determinism") or {}
        p1 = det.get("against_perturb1_same_system") or {}
        cd = det.get("against_cdsc1r4") or {}
        checks.append(
            {
                "run_id": record.get("run_id"),
                "sequence": record.get("sequence"),
                "system": record.get("system"),
                "status": record.get("status"),
                "evidence_validity": record.get("evidence_validity"),
                "perturb1_status": p1.get("status"),
                "perturb1_reference_run_directory": p1.get("reference_run_directory"),
                "perturb1_expected_trajectory_sha256": p1.get("expected_trajectory_sha256"),
                "cdsc1r4_status": cd.get("status") if cd else "NOT_APPLICABLE",
                "fresh_trajectory_sha256": det.get("fresh_trajectory_sha256"),
            }
        )
    verdict = (
        "PASS"
        if checks
        and all(c["perturb1_status"] == "BYTE_IDENTICAL" for c in checks)
        and all(c["cdsc1r4_status"] in ("BYTE_IDENTICAL", "NOT_APPLICABLE") for c in checks)
        else "FAIL"
    )
    result = {
        "schema": "schurvio.icra27.ablation.integrity_gate.v1",
        "campaign_id": CAMPAIGN_ID,
        "recorded_utc": utc_now(),
        "verdict": verdict,
        "cells": checks,
        "rule": (
            "P3: every recON offset-0 replica (S1 rotation.bag, N0 rotation.bag, S1 MH_05_difficult) must be "
            "byte-identical (state_estimate.txt, state_deviation.txt, estimate_raw.tum) to the PERTURB-1 recorded "
            "SHA256SUMS (and, for S1, to CDSC-1R4); otherwise the campaign STOPs (environment drift)"
        ),
    }
    directory = root / "integrity"
    directory.mkdir(parents=True, exist_ok=True)
    _write_new(directory / "INTEGRITY_GATE.json", _json_bytes(result))
    _write_sha256sums(directory)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("action", choices=("run", "plan"))
    value.add_argument("--artifact-root", required=True, type=Path)
    value.add_argument("--sets", default="p1-crux,u0-context,recoff-remaining")
    value.add_argument("--timeout-seconds", type=float, default=1800.0)
    value.add_argument("--cpu-list", default="8-15")
    value.add_argument("--cooldown-seconds", type=float, default=5.0)
    value.add_argument("--base-ros-port", type=int, default=18300)
    value.add_argument("--expected-tooling-commit", default=None)
    value.add_argument("--no-checkpoint", action="store_true")
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    sets = [item for item in args.sets.split(",") if item]
    unknown = [item for item in sets if item not in SETS]
    if unknown:
        print("unknown sets: {}".format(unknown), file=sys.stderr)
        return 2
    root = args.artifact_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    rows = campaign.load_matrix(campaign.RuntimePaths().matrix)
    cells = plan_cells(rows, sets)
    if args.action == "plan":
        for cell in cells:
            print(json.dumps(cell.describe(), sort_keys=True))
        return 0
    prereg = campaign.file_identity(PREREG_FILE)
    distribution = campaign.file_identity(DISTRIBUTION_FILE)
    driver = Driver(
        root,
        {
            "timeout_seconds": args.timeout_seconds,
            "cpu_list": args.cpu_list,
            "cooldown_seconds": args.cooldown_seconds,
            "base_ros_port": args.base_ros_port,
            "expected_tooling_commit": args.expected_tooling_commit,
        },
    )
    driver.events.append(
        "campaign-start-" + "-".join(sets),
        {
            "prereg": prereg,
            "recovery_event_distribution": distribution,
            "tooling_git": campaign.git_identity(REPO_ROOT),
            "planned_cells": [cell.describe() for cell in cells],
            "options": driver.options,
        },
    )
    exit_code = 0
    try:
        for set_name in sets:
            set_cells = [cell for cell in cells if cell.set_name == set_name]
            records = []
            for cell in set_cells:
                record = driver.run_cell(cell)
                records.append(record)
                det = record.get("determinism") or {}
                extra = ""
                if record.get("recovery_ablated"):
                    extra = " p2={} delta={}".format(
                        (det.get("against_perturb1_recon_counterpart") or {}).get("status"),
                        (record.get("single_delta_vs_recon_counterpart") or {}).get("status"),
                    )
                print(
                    "{} {} {} {} off{:+d} -> {} ({}) passage={}{}".format(
                        utc_now(), set_name, cell.run_id, cell.system, cell.offset_frames,
                        record.get("status"), record.get("outcome"), record.get("passage_complete"), extra,
                    ),
                    flush=True,
                )
            if set_name == "integrity":
                gate = integrity_gate(root, records)
                driver.events.append("integrity-gate", {"integrity_gate": gate})
                print("INTEGRITY_GATE {}".format(gate["verdict"]), flush=True)
                if gate["verdict"] != "PASS":
                    exit_code = 3
                    break
            if not args.no_checkpoint:
                receipt = driver.checkpoint(set_name)
                print("CHECKPOINT {} rc={}".format(receipt["output"], receipt["returncode"]), flush=True)
    except (AblationError, perturb.PerturbationError, campaign.CampaignError) as exc:
        driver.events.append("campaign-error", {"error": str(exc)})
        print("ABLATION_CAMPAIGN_ERROR: {}".format(exc), file=sys.stderr, flush=True)
        return 4
    driver.events.append("campaign-end-" + "-".join(sets), {"exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
