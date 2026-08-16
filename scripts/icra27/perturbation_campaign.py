#!/usr/bin/python3.8
"""PERTURB-1 perturbation-campaign driver (docs/icra27/PERTURBATION_PREREG.md).

Thin extension of the CDSC-1R4 orchestrator: it reuses the frozen CDSC-1R4
matrix (bag identities, frozen native start offsets, ground-truth paths), the
CDSC-1R4 trial runners (with their PERTURB-1 arguments), the CDSC event log
and checksum-closure validator, and evaluates every closed cell with the
frozen per-cell accuracy math.  It adds exactly the declared perturbation
family: replay start offset in camera frames and the RANSAC-seed axis (which
is NOT runnable on the frozen binaries and is recorded as such, see
DECISIONS.md), plus the N0 matched nullspace control.

Rules enforced here: estimator source, configs, thresholds and the prereg are
never touched; a failed run is retained and typed, never rerun; every cell
records its command line, config/launch/binary identities, offset, seed,
runner git identity and wall-clock timestamps; cells whose negative offset has
no lead-in data are recorded NOT_RUNNABLE without launching.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_campaign as campaign  # noqa: E402

PYTHON = campaign.PYTHON
CAMPAIGN_ID = "PERTURB-1"
PREREG_FILE = REPO_ROOT / "docs" / "icra27" / "PERTURBATION_PREREG.md"
CELL_EVALUATOR = SCRIPT_DIR / "perturbation_cell_evaluator.py"
AGGREGATOR = SCRIPT_DIR / "perturbation_aggregate.py"
CDSC1R4_ROOT = Path(
    "/home/moksh/schurvio-icra27-artifacts/cross-dataset-system-comparison/cdsc1r4-20260816T173621Z"
)
CELL_SCHEMA = "schurvio.icra27.perturbation.cell_record.v1"

FRAME_RATE_HZ = {"kaist_vio": 30.0, "euroc_mav": 20.0}
OFFSETS_EXECUTION_ORDER: Tuple[int, ...] = (0, 5, 10, -5, -10)
OFFSETS_DECLARED: Tuple[int, ...] = (-10, -5, 0, 5, 10)
SEED_LABELS: Tuple[str, ...] = ("frozen", "+1", "+2", "+3", "+4")
RUNNABLE_SEED_LABELS: Tuple[str, ...] = ("frozen",)
SYSTEMS: Tuple[str, ...] = ("S1", "N0")
S1_LIKE = ("S1", "N0")
FOCUS_SEQUENCES: Tuple[str, ...] = (
    "rotation/rotation.bag",
    "rotation/rotation_fast.bag",
    "square/square_fast.bag",
    "circle/circle_fast.bag",
    "square/square_head.bag",
)
EUROC_FORKS: Tuple[str, ...] = ("MH_05_difficult", "V1_01_easy", "V2_03_difficult")
SETS: Tuple[str, ...] = ("integrity", "kaist-focus", "kaist-full-remaining", "euroc-forks")
TRAJECTORY_FILES = (
    "trajectory/state_estimate.txt",
    "trajectory/state_deviation.txt",
    "trajectory/estimate_raw.tum",
)
SEED_NOT_RUNNABLE_REASON = (
    "NO_RUNTIME_SEED_PARAMETER: the frozen S1 executable compiles cv::setRNGSeed(0) "
    "(ov_msckf/src/core/VioManager.cpp) and exposes no RANSAC/RNG seed parameter; varying it "
    "requires an estimator source edit and rebuild, prohibited for this campaign (DECISIONS.md D2)"
)


class PerturbationError(RuntimeError):
    pass


def utc_now() -> str:
    return campaign.utc_now()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, payload: bytes) -> None:
    campaign._atomic_write_new(path, payload)


def _write_sha256sums(directory: Path) -> None:
    lines = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise PerturbationError("refusing to checksum a symlink: {}".format(path))
        if path.is_file() and path.name != "SHA256SUMS":
            lines.append(
                "{}  {}".format(campaign.sha256_file(path), path.relative_to(directory).as_posix())
            )
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def _safe(sequence: str) -> str:
    return campaign.safe_sequence(sequence)


def _offset_token(offset: int) -> str:
    if offset == 0:
        return "off0"
    return "off{}{}".format("p" if offset > 0 else "m", abs(offset))


def _seed_token(label: str) -> str:
    return "seed" + ("frozen" if label == "frozen" else "p" + label.lstrip("+"))


class Cell:
    def __init__(
        self,
        set_name: str,
        row: campaign.MatrixRow,
        system: str,
        axis: str,
        offset_frames: int,
        seed_label: str,
    ) -> None:
        self.set_name = set_name
        self.row = row
        self.system = system
        self.axis = axis
        self.offset_frames = offset_frames
        self.seed_label = seed_label
        self.frame_rate_hz = FRAME_RATE_HZ[row.dataset]
        self.frozen_start = float(row.bag_start_seconds)
        self.shift_seconds = float(offset_frames) / self.frame_rate_hz
        self.estimator_start = self.frozen_start + self.shift_seconds
        token = _offset_token(offset_frames) if axis == "offset" else _seed_token(seed_label)
        self.run_id = "perturb1-{}{:02d}-{}-{}-{}-{}-a1".format(
            "integrity-" if set_name == "integrity" else "",
            row.order, _safe(row.sequence), system.lower(), axis, token
        )
        self.key = campaign.row_key(row)

    @property
    def runnable(self) -> Tuple[bool, Optional[str]]:
        if self.axis == "seed" and self.seed_label not in RUNNABLE_SEED_LABELS:
            return False, SEED_NOT_RUNNABLE_REASON
        if self.estimator_start < 0.0:
            return False, (
                "NO_LEAD_IN_DATA: frozen replay start {!r} s + {} frames / {} Hz = {!r} s precedes "
                "the all-topic bag begin; a negative start has no data to replay (DECISIONS.md D3)".format(
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
    kaist_rows = [row for row in rows if row.dataset == "kaist_vio"]
    cells: List[Cell] = []

    def systems_for(row: campaign.MatrixRow) -> Tuple[str, ...]:
        # Alternate the first system by matrix-order parity, as CDSC-1R4 did.
        return ("S1", "N0") if row.order % 2 == 1 else ("N0", "S1")

    def offset_cells(set_name: str, row: campaign.MatrixRow) -> List[Cell]:
        result = []
        for offset in OFFSETS_EXECUTION_ORDER:
            for system in systems_for(row):
                result.append(Cell(set_name, row, system, "offset", offset, "frozen"))
        return result

    def seed_cells(set_name: str, row: campaign.MatrixRow) -> List[Cell]:
        result = []
        for label in SEED_LABELS:
            for system in systems_for(row):
                result.append(Cell(set_name, row, system, "seed", 0, label))
        return result

    if "integrity" in sets:
        cells.append(Cell("integrity", by_sequence["rotation/rotation.bag"], "S1", "offset", 0, "frozen"))
        cells.append(Cell("integrity", by_sequence["MH_05_difficult"], "S1", "offset", 0, "frozen"))
    if "kaist-focus" in sets:
        for sequence in [row.sequence for row in kaist_rows if row.sequence in FOCUS_SEQUENCES]:
            row = by_sequence[sequence]
            cells.extend(offset_cells("kaist-focus", row))
            cells.extend(seed_cells("kaist-focus", row))
    if "kaist-full-remaining" in sets:
        for row in kaist_rows:
            if row.sequence in FOCUS_SEQUENCES:
                continue
            cells.extend(offset_cells("kaist-full-remaining", row))
    if "euroc-forks" in sets:
        for sequence in EUROC_FORKS:
            cells.extend(offset_cells("euroc-forks", by_sequence[sequence]))
    return cells


def build_command(paths: campaign.RuntimePaths, cell: Cell, root: Path, options: Mapping[str, Any]) -> List[str]:
    row = cell.row
    location = cell.paths(root)
    runner = paths.kaist_runner if row.dataset == "kaist_vio" else paths.generic_runner
    binary_system = "S1" if cell.system in S1_LIKE else "U0"
    if row.dataset == "kaist_vio":
        config = campaign._config_path(paths, row.dataset, binary_system)
        launch = (
            REPO_ROOT / "project" / "icra27_kaist_n0_serial.launch"
            if cell.system == "N0"
            else campaign._launch_path(paths, row.dataset, binary_system)
        )
    else:
        config = campaign._config_path(paths, row.dataset, binary_system)
        launch = (
            REPO_ROOT / "project" / "icra27_cross_dataset_n0_serial.launch"
            if cell.system == "N0"
            else campaign._launch_path(paths, row.dataset, binary_system)
        )
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
    values: Dict[str, str] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        if "  " in line:
            digest, relative = line.split("  ", 1)
            values[relative] = digest
    return values


def cdsc1r4_scored_directory(row: campaign.MatrixRow, system: str) -> Optional[Path]:
    location = campaign.run_location(CDSC1R4_ROOT, "scored", row, system)
    return location.run_directory if location.result_path.is_file() else None


def determinism_check(cell: Cell, run_directory: Path, root: Path) -> Dict[str, Any]:
    """Byte-identity checks required by the prereg (offset 0, frozen seed)."""

    fresh = _read_sums(run_directory / "SHA256SUMS")
    fresh_files = {name: fresh.get(name) for name in TRAJECTORY_FILES}
    record: Dict[str, Any] = {
        "fresh_trajectory_sha256": fresh_files,
        "against_cdsc1r4": None,
        "against_offset0_replicate": None,
    }
    if cell.estimator_start == cell.frozen_start and cell.system == "S1":
        reference = cdsc1r4_scored_directory(cell.row, "S1")
        if reference is None:
            record["against_cdsc1r4"] = {"status": "REFERENCE_ABSENT"}
        else:
            expected = _read_sums(reference / "SHA256SUMS")
            expected_files = {name: expected.get(name) for name in TRAJECTORY_FILES}
            live = {
                name: (campaign.sha256_file(reference / name) if (reference / name).is_file() else None)
                for name in TRAJECTORY_FILES
            }
            record["against_cdsc1r4"] = {
                "reference_run_directory": str(reference),
                "expected_trajectory_sha256": expected_files,
                "reference_live_sha256_matches_recorded": live == expected_files,
                "byte_identical": bool(
                    all(fresh_files[name] is not None for name in TRAJECTORY_FILES)
                    and fresh_files == expected_files
                ),
                "status": (
                    "BYTE_IDENTICAL"
                    if all(fresh_files[name] is not None for name in TRAJECTORY_FILES)
                    and fresh_files == expected_files
                    else "MISMATCH"
                ),
            }
    if cell.axis == "seed" and cell.seed_label == "frozen":
        sibling = Cell(cell.set_name, cell.row, cell.system, "offset", 0, "frozen")
        sibling_dir = sibling.paths(root)["run_directory"]
        if (sibling_dir / "SHA256SUMS").is_file():
            expected = _read_sums(sibling_dir / "SHA256SUMS")
            expected_files = {name: expected.get(name) for name in TRAJECTORY_FILES}
            record["against_offset0_replicate"] = {
                "reference_run_directory": str(sibling_dir),
                "expected_trajectory_sha256": expected_files,
                "status": (
                    "BYTE_IDENTICAL"
                    if all(fresh_files[name] is not None for name in TRAJECTORY_FILES)
                    and fresh_files == expected_files
                    else "MISMATCH"
                ),
            }
        else:
            record["against_offset0_replicate"] = {"status": "REFERENCE_ABSENT"}
    return record


def validate_closed_result(cell: Cell, result_path: Path) -> Dict[str, Any]:
    if result_path.is_symlink() or result_path.parent.is_symlink():
        raise PerturbationError("sequence result path is a symlink")
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
        raise PerturbationError("sequence result binding mismatch: {}".format(differences))
    perturbation = value.get("perturbation")
    if not isinstance(perturbation, dict):
        raise PerturbationError("sequence result lacks the perturbation record")
    if (
        perturbation.get("offset_frames") != cell.offset_frames
        or perturbation.get("seed_label") != cell.seed_label
        or perturbation.get("axis") != cell.axis
        or float(perturbation.get("estimator_bag_start_seconds", math.nan)) != cell.estimator_start
    ):
        raise PerturbationError("sequence result perturbation record differs from the planned cell")
    status = value.get("status")
    if status not in campaign.ALL_STATUSES or status == "ACTIVE":
        raise PerturbationError("sequence result status is unknown or not terminal: {}".format(status))
    validity = value.get("evidence_validity")
    if validity not in campaign.EVIDENCE_VALIDITIES:
        raise PerturbationError("sequence result evidence_validity is unknown")
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
        raise PerturbationError("retained outcome lacks unambiguous estimator closure")
    return {
        "value": value,
        "identity": identity,
        "classification": classification,
        "checksummed_paths": sorted(checksummed),
        "run_directory": run_directory,
    }


def typed_failure_class(value: Mapping[str, Any]) -> Optional[str]:
    status = value.get("status")
    if status in campaign.ELIGIBLE_STATUSES and (value.get("passage") or {}).get("complete"):
        return None
    if status in campaign.ELIGIBLE_STATUSES:
        return "PASSAGE_INCOMPLETE_" + str(status)
    return str(status)


class Driver:
    def __init__(self, root: Path, options: Mapping[str, Any]) -> None:
        self.root = root
        self.options = dict(options)
        self.paths = campaign.RuntimePaths()
        self.rows = campaign.load_matrix(self.paths.matrix)
        self.events = campaign.EventLog(root)
        self.ledger = root / "control" / "cells.jsonl"
        self.log_dir = root / "control" / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.last_end = 0.0
        self.port_index = 0

    def _git(self) -> Mapping[str, Any]:
        identity = campaign.git_identity(REPO_ROOT)
        expected = self.options.get("expected_tooling_commit")
        if identity["dirty"]:
            raise PerturbationError("tooling repository is dirty (including untracked files); refusing to run")
        if expected and identity["head_sha"] != expected:
            raise PerturbationError("tooling HEAD {} differs from the pinned {}".format(identity["head_sha"], expected))
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
                "input_interval_selected_pair_count": (value.get("input_interval") or {}).get("selected_pair_count"),
                "stage_errors": value.get("stage_errors"),
                "failure": value.get("failure"),
            }
        )
        if returncode is not None:
            expected_exit = 0 if value.get("status") in campaign.ELIGIBLE_STATUSES else 2
            record["runner_exit_contract_ok"] = returncode == expected_exit
        record["determinism"] = determinism_check(cell, run_directory, self.root)
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
        det = (record.get("determinism") or {}).get("against_cdsc1r4") or {}
        checks.append(
            {
                "run_id": record.get("run_id"),
                "sequence": record.get("sequence"),
                "system": record.get("system"),
                "status": record.get("status"),
                "evidence_validity": record.get("evidence_validity"),
                "determinism_status": det.get("status"),
                "reference_run_directory": det.get("reference_run_directory"),
                "expected_trajectory_sha256": det.get("expected_trajectory_sha256"),
                "fresh_trajectory_sha256": (record.get("determinism") or {}).get("fresh_trajectory_sha256"),
            }
        )
    verdict = "PASS" if checks and all(c["determinism_status"] == "BYTE_IDENTICAL" for c in checks) else "FAIL"
    result = {
        "schema": "schurvio.icra27.perturbation.integrity_gate.v1",
        "campaign_id": CAMPAIGN_ID,
        "recorded_utc": utc_now(),
        "verdict": verdict,
        "cells": checks,
        "rule": "both offset-0 frozen-seed S1 reproductions must be byte-identical (state_estimate.txt, state_deviation.txt, estimate_raw.tum) to the CDSC-1R4 recorded SHA256SUMS; otherwise the campaign STOPs",
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
    value.add_argument("--sets", default="kaist-focus,kaist-full-remaining,euroc-forks")
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
                print(
                    "{} {} {} {} {} -> {} ({})".format(
                        utc_now(), set_name, cell.run_id, cell.system, cell.axis,
                        record.get("status"), record.get("outcome"),
                    ),
                    flush=True,
                )
            if set_name == "integrity":
                gate = integrity_gate(root, records)
                driver.events.append("integrity-gate", gate)
                print("INTEGRITY_GATE {}".format(gate["verdict"]), flush=True)
                if gate["verdict"] != "PASS":
                    exit_code = 3
                    break
            if not args.no_checkpoint:
                receipt = driver.checkpoint(set_name)
                print("CHECKPOINT {} rc={}".format(receipt["output"], receipt["returncode"]), flush=True)
    except (PerturbationError, campaign.CampaignError) as exc:
        driver.events.append("campaign-error", {"error": str(exc)})
        print("PERTURBATION_CAMPAIGN_ERROR: {}".format(exc), file=sys.stderr, flush=True)
        return 4
    driver.events.append("campaign-end-" + "-".join(sets), {"exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
