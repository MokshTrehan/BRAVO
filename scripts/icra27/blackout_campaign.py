#!/usr/bin/python3.8
"""BLACKOUT-1 controlled-outage campaign driver (docs/icra27/BLACKOUT_PREREG.md, amended).

Thin extension of the ABLATE-REC-1 driver.  It reuses the frozen CDSC-1R4
matrix, the CDSC-1R4 trial runners (BLACKOUT-1 mode: replay-layer masked bag
+ descriptive recovery evidence + the D7 seam), the CDSC event log, the frozen
per-cell accuracy evaluator, and adds:

  * the committed injection-point table (docs/icra27/BLACKOUT_INJECTION_POINTS.json)
    -> per (sequence, arm, k) masked bag written by blackout_mask.py (transient,
    deterministic; manifest + sha256 retained per cell);
  * mechanical NOT_RUNNABLE rules (D3) applied before launching;
  * per-cell: masked-range manifest, resolved recovery parameter, full recovery
    event log, attempt-reason histogram, correspondence counts, GT speed at every
    commit, false-commit flag (D8), single-delta diffs, byte identity of
    integrity/inertness replicas, command line and input identities.

Rules: estimator source/configs/thresholds/prereg untouched; failures retained
and typed, never rerun; serialized; RUN_LOG.md append-only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import shutil
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
import ablation_campaign as ablate  # noqa: E402

PYTHON = campaign.PYTHON
CAMPAIGN_ID = "BLACKOUT-1"
PREREG_FILE = REPO_ROOT / "docs" / "icra27" / "BLACKOUT_PREREG.md"
TABLE_FILE = REPO_ROOT / "docs" / "icra27" / "BLACKOUT_INJECTION_POINTS.json"
CELL_EVALUATOR = SCRIPT_DIR / "perturbation_cell_evaluator.py"
GT_METRICS = SCRIPT_DIR / "blackout_cell_metrics.py"
MASK_TOOL = SCRIPT_DIR / "blackout_mask.py"
AGGREGATOR = SCRIPT_DIR / "blackout_aggregate.py"
CDSC1R4_ROOT = perturb.CDSC1R4_ROOT
PERTURB1_ROOT = ablate.PERTURB1_ROOT
ABLATE1_ROOT = Path("/home/moksh/schurvio-icra27-artifacts/recovery-ablation/ablate1-20260817T011231Z")
CELL_SCHEMA = "schurvio.icra27.blackout.cell_record.v1"
NS = 1_000_000_000

FRAME_RATE_HZ = perturb.FRAME_RATE_HZ
DURATIONS: Tuple[int, ...] = (2, 5, 10, 15, 20)
KAIST_SEQUENCES: Tuple[str, ...] = ("circle/circle.bag", "infinite/infinite.bag", "square/square.bag")
EUROC_SEQUENCES: Tuple[str, ...] = ("MH_05_difficult", "V2_02_medium")
ALL_SEQUENCES: Tuple[str, ...] = KAIST_SEQUENCES + EUROC_SEQUENCES
SETS: Tuple[str, ...] = (
    "step0",
    "integrity",
    "armB-crux",
    "armA-crux",
    "armB-remaining",
    "armA-remaining",
    "euroc-remaining",
    "u0-context",
)
RECOVERY_SWITCH_PARAMETER = "long_gap_recovery_enabled"
TRAJECTORY_FILES = perturb.TRAJECTORY_FILES
LAUNCH_FILES = {
    ("kaist_vio", "S1"): REPO_ROOT / "project" / "rotation_robustness_serial.launch",
    ("kaist_vio", "S1-recOFF"): REPO_ROOT / "project" / "icra27_kaist_s1_recoff_serial.launch",
    ("kaist_vio", "U0"): REPO_ROOT / "project" / "icra27_kaist_u0_serial.launch",
    ("euroc_mav", "S1"): REPO_ROOT / "project" / "icra27_cross_dataset_s1_serial.launch",
    ("euroc_mav", "S1-recON-euroc"): REPO_ROOT / "project" / "icra27_cross_dataset_s1_recon_euroc_serial.launch",
    ("euroc_mav", "S1-recOFF"): REPO_ROOT / "project" / "icra27_cross_dataset_s1_recoff_serial.launch",
}
ROLE = {"S1": "recON", "S1-recON-euroc": "recON", "S1-recOFF": "recOFF", "U0": "U0"}
RUN_OWNED_OUTPUT_SUFFIXES = frozenset(("filepath_est", "filepath_std", "record_timing_filepath"))
REPLAY_INPUT_SUFFIXES = frozenset(("path_bag",))
STRICT_BOOL_RE = re.compile(r"^\s*long_gap_recovery_enabled\s*:\s*(true|false)\s*(#.*)?$", re.M)


class BlackoutCampaignError(RuntimeError):
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


def load_table() -> Dict[str, Dict[str, Any]]:
    value = json.loads(TABLE_FILE.read_text())
    return {row["sequence"]: row for row in value["sequences"]}


def recon_system(dataset: str) -> str:
    return "S1" if dataset == "kaist_vio" else "S1-recON-euroc"


class Cell:
    def __init__(
        self,
        set_name: str,
        row: campaign.MatrixRow,
        system: str,
        arm: Optional[str],
        k: int,
        table_row: Optional[Mapping[str, Any]],
        replica_of: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.set_name = set_name
        self.row = row
        self.system = system
        self.arm = arm  # None -> un-injected replica
        self.k = k
        self.table_row = table_row
        self.replica_of = replica_of
        self.frozen_start = float(row.bag_start_seconds)
        self.key = campaign.row_key(row)
        if arm is None:
            self.run_id = "blackout1-integrity-{:02d}-{}-{}-off0-a1".format(row.order, _safe(row.sequence), system.lower())
        else:
            self.run_id = "blackout1-{:02d}-{}-{}-arm{}-k{:02d}-a1".format(row.order, _safe(row.sequence), system.lower(), arm, k)

    @property
    def role(self) -> str:
        return ROLE[self.system]

    @property
    def masked(self) -> bool:
        return self.arm is not None

    @property
    def mask_key(self) -> Optional[str]:
        if self.arm is None:
            return None
        return "{}-arm{}-k{:02d}".format(_safe(self.row.sequence), self.arm, self.k)

    @property
    def injection(self) -> Optional[Mapping[str, Any]]:
        if self.arm is None or self.table_row is None:
            return None
        return self.table_row["arm_" + self.arm]

    @property
    def mask_start_ns(self) -> Optional[int]:
        inj = self.injection
        return None if inj is None else int(inj["header_stamp_ns"])

    @property
    def runnable(self) -> Tuple[bool, Optional[str]]:
        if self.arm is None:
            return True, None
        inj = self.injection
        if inj is None:
            return False, "NO_INJECTION_POINT"
        if self.k == 0:
            return True, None
        per_k = inj["per_k"][str(self.k)]
        view_end = int(self.table_row["view_end_header_stamp_ns"])
        if per_k.get("mask_past_end"):
            return False, (
                "NOT_RUNNABLE_MASK_PAST_END: t_b + k = {:.3f} s >= last cam0 frame {:.3f} s (no post-mask frame; D3a)".format(
                    (self.mask_start_ns + self.k * NS) / NS, view_end / NS
                )
            )
        if self.arm == "B" and per_k.get("overlaps_arm_a"):
            a = self.table_row["arm_A"]["header_stamp_ns"]
            return False, (
                "NOT_RUNNABLE_ARM_OVERLAP: arm-B mask [{:.3f}, {:.3f}] intersects arm-A mask [{:.3f}, {:.3f}] at k={} (D3b)".format(
                    self.mask_start_ns / NS, (self.mask_start_ns + self.k * NS) / NS, a / NS, (a + self.k * NS) / NS, self.k
                )
            )
        if self.mask_start_ns < int(self.table_row["view_start_header_stamp_ns"]):
            return False, "NOT_RUNNABLE_NO_LEAD_IN (D3c)"
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
        inj = self.injection
        return {
            "set": self.set_name,
            "order": self.row.order,
            "dataset": self.row.dataset,
            "sequence": self.row.sequence,
            "sequence_key": self.key,
            "system": self.system,
            "role": self.role,
            "arm": self.arm,
            "k_seconds": self.k,
            "masked": self.masked,
            "mask_key": self.mask_key,
            "mask_start_header_stamp_ns": self.mask_start_ns,
            "mask_start_s": None if self.mask_start_ns is None else self.mask_start_ns / NS,
            "mask_end_s": None if self.mask_start_ns is None else (self.mask_start_ns + self.k * NS) / NS,
            "injection_point": None if inj is None else {
                k: inj.get(k) for k in ("header_stamp_s", "seconds_from_view_start", "gt_speed_instantaneous_mps", "gt_speed_3s_window_mean_mps")
            },
            "frozen_matrix_bag_start_seconds": self.frozen_start,
            "estimator_bag_start_seconds": self.frozen_start,
            "replica_of": self.replica_of,
            "runnable": runnable,
            "not_runnable_reason": reason,
            "run_id": self.run_id,
        }


def plan_cells(rows: Sequence[campaign.MatrixRow], sets: Sequence[str]) -> List[Cell]:
    by_sequence = {row.sequence: row for row in rows}
    table = load_table()
    cells: List[Cell] = []

    def T(seq: str) -> Mapping[str, Any]:
        return table[seq]

    if "step0" in sets:
        # masking-inertness: k=0 through the masking path (D10)
        cells.append(Cell("step0", by_sequence["circle/circle.bag"], "S1", "A", 0, T("circle/circle.bag")))
    if "integrity" in sets:
        cells.append(Cell("integrity", by_sequence["circle/circle.bag"], "S1", None, 0, None, {"reference": "CDSC-1R4", "system": "S1"}))
        cells.append(Cell("integrity", by_sequence["MH_05_difficult"], "S1", None, 0, None, {"reference": "PERTURB-1+CDSC-1R4", "system": "S1"}))
        cells.append(
            Cell(
                "integrity",
                by_sequence["rotation/rotation.bag"],
                "S1-recOFF",
                None,
                0,
                None,
                {
                    "reference": "ABLATE-REC-1",
                    "system": "S1-recOFF",
                    "run_directory": str(
                        ABLATE1_ROOT / "cells" / "p1-crux" / "19-rotation_rotation.bag" / "S1-recOFF" / "ablate1-19-rotation_rotation.bag-s1-recoff-offset-off0-a1"
                    ),
                },
            )
        )
    if "armB-crux" in sets:
        for seq in ALL_SEQUENCES:
            for k in (2, 5):
                cells.append(Cell("armB-crux", by_sequence[seq], recon_system(by_sequence[seq].dataset), "B", k, T(seq)))
    if "armA-crux" in sets:
        for seq in KAIST_SEQUENCES:
            for system in ("S1", "S1-recOFF"):
                cells.append(Cell("armA-crux", by_sequence[seq], system, "A", 10, T(seq)))
    if "armB-remaining" in sets:
        for seq in ALL_SEQUENCES:
            row = by_sequence[seq]
            for k in (10, 15, 20):
                cells.append(Cell("armB-remaining", row, recon_system(row.dataset), "B", k, T(seq)))
            for k in DURATIONS:
                cells.append(Cell("armB-remaining", row, "S1-recOFF", "B", k, T(seq)))
    if "armA-remaining" in sets:
        for seq in KAIST_SEQUENCES:
            for k in (2, 5, 15, 20):
                for system in ("S1", "S1-recOFF"):
                    cells.append(Cell("armA-remaining", by_sequence[seq], system, "A", k, T(seq)))
    if "euroc-remaining" in sets:
        for seq in EUROC_SEQUENCES:
            for k in DURATIONS:
                for system in ("S1-recON-euroc", "S1-recOFF"):
                    cells.append(Cell("euroc-remaining", by_sequence[seq], system, "A", k, T(seq)))
    if "u0-context" in sets:
        for seq in KAIST_SEQUENCES:
            for k in (5, 15):
                cells.append(Cell("u0-context", by_sequence[seq], "U0", "A", k, T(seq)))
    return cells


def build_command(
    paths: campaign.RuntimePaths, cell: Cell, root: Path, options: Mapping[str, Any], manifest: Optional[Path]
) -> List[str]:
    row = cell.row
    location = cell.paths(root)
    runner = paths.kaist_runner if row.dataset == "kaist_vio" else paths.generic_runner
    binary_system = "U0" if cell.system == "U0" else "S1"
    config = campaign._config_path(paths, row.dataset, binary_system)
    launch = LAUNCH_FILES[(row.dataset, cell.system)]
    inner = [
        str(PYTHON), str(runner), "run",
        "--protocol-id", campaign.PROTOCOL_ID,
        "--protocol-file", str(paths.protocol),
        "--matrix-file", str(paths.matrix),
        "--run-id", cell.run_id,
        "--attempt-index", "1",
        "--dataset", row.dataset,
        "--sequence", row.sequence,
        "--system", cell.system,
        "--mode", "scored",
        "--bag", str(row.bag["path"]),
        "--bag-start", format(row.bag_start_seconds, ".17g"),
        "--bag-duration", "-1",
        "--config", str(config),
        "--launch", str(launch),
        "--binary", str(campaign._binary_path(paths, binary_system)),
        "--output-root", str(location["output_root"]),
        "--ros-port", str(options["ros_port"]),
        "--timeout-seconds", format(float(options["timeout_seconds"]), ".17g"),
        "--cpu-list", str(options["cpu_list"]),
        "--perturbation-campaign-id", CAMPAIGN_ID,
        "--perturbation-offset-frames", "0",
        "--perturbation-frame-rate-hz", format(FRAME_RATE_HZ[row.dataset], ".17g"),
        "--perturbation-seed-label", "frozen",
        "--perturbation-axis", "offset",
    ]
    if manifest is not None:
        inner += [
            "--blackout-manifest", str(manifest),
            "--blackout-arm", str(cell.arm),
            "--blackout-k-seconds", format(float(cell.k), ".17g"),
        ]
    return campaign.sourced_command(campaign._setup_path(paths, binary_system), inner)


class MaskedBags:
    """Transient masked bags under <root>/masked-bags (D4)."""

    def __init__(self, root: Path, log_dir: Path, table: Mapping[str, Mapping[str, Any]]) -> None:
        self.dir = root / "masked-bags"
        self.manifests = self.dir / "manifests"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifests.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        self.table = table
        self.current_key: Optional[str] = None
        self.current_bag: Optional[Path] = None
        self.current_manifest: Optional[Path] = None
        self.generation = 0
        # stale bags from an interrupted invocation are removed (manifests kept)
        for stale in self.dir.glob("*.bag*"):
            stale.unlink()

    def _first_sha(self, key: str) -> Optional[str]:
        shas = []
        for path in sorted(self.manifests.glob(key + ".*.json")):
            try:
                shas.append(json.loads(path.read_text())["masked_bag"]["sha256"])
            except (OSError, ValueError, KeyError):
                continue
        return shas[0] if shas else None

    def ensure(self, cell: Cell) -> Tuple[Path, Dict[str, Any]]:
        key = cell.mask_key
        assert key is not None
        if self.current_key == key and self.current_bag is not None and self.current_bag.is_file():
            return self.current_manifest, json.loads(self.current_manifest.read_text())
        self.release()
        row = self.table[cell.row.sequence]
        self.generation += 1
        existing = len(list(self.manifests.glob(key + ".*.json")))
        manifest = self.manifests / "{}.{:03d}.json".format(key, existing + 1)
        bag = self.dir / (key + ".bag")
        command = [
            str(PYTHON), str(MASK_TOOL), "mask",
            "--source", str(row["bag"]["path"]),
            "--output", str(bag),
            "--manifest", str(manifest),
            "--camera-topics", ",".join(row["camera_topics"]),
            "--imu-topic", str(row["imu_topic"]),
            "--mask-start-ns", str(cell.mask_start_ns),
            "--mask-seconds", format(float(cell.k), ".17g"),
            "--label", "campaign=" + CAMPAIGN_ID,
            "--label", "sequence=" + cell.row.sequence,
            "--label", "arm=" + str(cell.arm),
            "--label", "k_seconds=" + str(cell.k),
            "--label", "mask_key=" + key,
        ]
        log = self.log_dir / ("mask-{}.{:03d}.log".format(key, existing + 1))
        with log.open("ab") as stream:
            stream.write(("{} {}\n".format(utc_now(), " ".join(command))).encode())
            stream.flush()
            completed = subprocess.run(command, check=False, stdout=stream, stderr=subprocess.STDOUT)
        if completed.returncode != 0 or not manifest.is_file() or not bag.is_file():
            raise BlackoutCampaignError("mask tool failed for {} (rc={}, log {})".format(key, completed.returncode, log))
        value = json.loads(manifest.read_text())
        first = self._first_sha(key)
        value["_driver"] = {
            "generation_in_invocation": self.generation,
            "manifest_index": existing + 1,
            "first_recorded_masked_sha256": first,
            "deterministic_regeneration": (first == value["masked_bag"]["sha256"]) if first else None,
            "log": str(log),
        }
        if first is not None and first != value["masked_bag"]["sha256"]:
            raise BlackoutCampaignError("masked bag regeneration is not deterministic for {}".format(key))
        self.current_key, self.current_bag, self.current_manifest = key, bag, manifest
        return manifest, value

    def release(self) -> None:
        if self.current_bag is not None and self.current_bag.is_file():
            self.current_bag.unlink()
        self.current_key, self.current_bag, self.current_manifest = None, None, None


def _resolved_map(run_directory: Path) -> Optional[Dict[str, Any]]:
    return ablate._resolved_map(run_directory)


def single_delta_diff(fresh_dir: Path, counterpart_dir: Path, expected_value: Any, counterpart_expected: Any) -> Dict[str, Any]:
    """Resolved-parameter diff: only the recovery switch may differ (fresh=expected_value, counterpart=counterpart_expected);
    run-owned outputs and the replay input path (masked vs frozen/other bag) are compared by tail and recorded."""
    fresh = _resolved_map(fresh_dir)
    ref = _resolved_map(counterpart_dir)
    if fresh is None or ref is None:
        return {"status": "RESOLVED_PARAMETERS_ABSENT", "fresh_present": fresh is not None, "counterpart_present": ref is not None}
    changed: Dict[str, Any] = {}
    tails: Dict[str, Any] = {}
    replay: Dict[str, Any] = {}
    for name in sorted(set(fresh) | set(ref)):
        suffix = name.rsplit("/", 1)[-1]
        if suffix in RUN_OWNED_OUTPUT_SUFFIXES:
            f_tail = "/".join(str(fresh.get(name)).split("/")[-2:]) if name in fresh else None
            r_tail = "/".join(str(ref.get(name)).split("/")[-2:]) if name in ref else None
            tails[name] = {"fresh_tail": f_tail, "counterpart_tail": r_tail, "equal": f_tail == r_tail}
            continue
        if suffix in REPLAY_INPUT_SUFFIXES:
            replay[name] = {"fresh": fresh.get(name), "counterpart": ref.get(name), "equal": fresh.get(name) == ref.get(name)}
            continue
        if (name in fresh) != (name in ref) or fresh.get(name) != ref.get(name):
            changed[name] = {"fresh": fresh.get(name, "<absent>") if name in fresh else "<absent>", "counterpart": ref.get(name, "<absent>") if name in ref else "<absent>"}
    switch_keys = [k for k in changed if k.rsplit("/", 1)[-1] == RECOVERY_SWITCH_PARAMETER]
    if expected_value == counterpart_expected:
        single = len(changed) == 0 and all(v["equal"] for v in tails.values())
    else:
        single = (
            len(changed) == 1
            and len(switch_keys) == 1
            and changed[switch_keys[0]]["fresh"] == expected_value
            and changed[switch_keys[0]]["counterpart"] == counterpart_expected
            and all(v["equal"] for v in tails.values())
        )
    return {
        "status": "SINGLE_DELTA" if single else "NOT_SINGLE_DELTA",
        "single_delta": bool(single),
        "changed_parameters_excluding_run_owned_and_replay_input": changed,
        "run_owned_output_path_tails": tails,
        "replay_input_path": replay,
        "expected_delta": {"fresh": expected_value, "counterpart": counterpart_expected},
        "fresh_parameter_count": len(fresh),
        "counterpart_parameter_count": len(ref),
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
        effective, source = bool(ros_value), "ROS_PARAMETER_OVERRIDE"
    elif yaml_value != "<absent>":
        effective, source = bool(yaml_value), "YAML"
    else:
        effective, source = False, "EXECUTABLE_DEFAULT_FALSE"
    expected = {"recON": True, "recOFF": False, "U0": None}[cell.role]
    if cell.system == "S1" and cell.row.dataset == "euroc_mav":
        expected = False  # frozen EuRoC replica: key absent
    return {
        "parameter": RECOVERY_SWITCH_PARAMETER,
        "ros_parameter_value": ros_value,
        "yaml_value": yaml_value,
        "effective_value": effective,
        "effective_source": source,
        "expected_effective_value": expected,
        "matches_expectation": (expected is None) or (effective == expected),
    }


def _compare(fresh: Mapping[str, Optional[str]], reference_dir: Optional[Path]) -> Dict[str, Any]:
    return ablate._compare(fresh, reference_dir)


def determinism_check(cell: Cell, run_directory: Path, root: Path) -> Dict[str, Any]:
    fresh_live = ablate._live_trajectory_sums(run_directory)
    recorded = ablate._trajectory_sums(run_directory)
    record: Dict[str, Any] = {
        "fresh_trajectory_sha256": recorded,
        "fresh_live_sha256_matches_recorded": fresh_live == recorded,
        "against_cdsc1r4": None,
        "against_perturb1_same_system": None,
        "against_ablate1": None,
        "against_recon_counterpart_this_campaign": None,
    }
    unmasked_like = (not cell.masked) or cell.k == 0
    if unmasked_like and cell.system in ("S1", "U0"):
        record["against_cdsc1r4"] = _compare(recorded, perturb.cdsc1r4_scored_directory(cell.row, cell.system))
    if unmasked_like and cell.system == "S1":
        counterpart = ablate.perturb1_counterpart(cell.row.sequence, "S1", 0)
        record["against_perturb1_same_system"] = _compare(recorded, Path(str(counterpart["run_directory"])) if counterpart else None)
        if counterpart:
            record["against_perturb1_same_system"]["perturb1_run_id"] = counterpart.get("run_id")
    if cell.replica_of and cell.replica_of.get("run_directory"):
        record["against_ablate1"] = _compare(recorded, Path(str(cell.replica_of["run_directory"])))
    if cell.masked and cell.role == "recOFF":
        counterpart_dir = _recon_counterpart_dir(cell, root)
        record["against_recon_counterpart_this_campaign"] = _compare(recorded, counterpart_dir)
    return record


def _recon_counterpart_dir(cell: Cell, root: Path) -> Optional[Path]:
    """The recON cell with the same (sequence, arm, k) anywhere under the root (any set)."""
    system = recon_system(cell.row.dataset)
    run_id = "blackout1-{:02d}-{}-{}-arm{}-k{:02d}-a1".format(cell.row.order, _safe(cell.row.sequence), system.lower(), cell.arm, cell.k)
    for candidate in (root / "cells").glob("*/{}/{}/{}".format(cell.key, system, run_id)):
        if (candidate / "sequence_result.json").is_file():
            return candidate
    return None


def validate_closed_result(cell: Cell, result_path: Path) -> Dict[str, Any]:
    if result_path.is_symlink() or result_path.parent.is_symlink():
        raise BlackoutCampaignError("sequence result path is a symlink")
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
        raise BlackoutCampaignError("sequence result binding mismatch: {}".format(differences))
    perturbation = value.get("perturbation")
    if not isinstance(perturbation, dict) or perturbation.get("campaign_id") != CAMPAIGN_ID or perturbation.get("offset_frames") != 0:
        raise BlackoutCampaignError("sequence result perturbation record differs from the planned cell")
    blackout = value.get("blackout")
    if cell.masked:
        if not isinstance(blackout, dict) or blackout.get("arm") != cell.arm or float(blackout.get("k_seconds", math.nan)) != float(cell.k):
            raise BlackoutCampaignError("sequence result blackout record differs from the planned cell")
        start_ns = ((blackout.get("manifest_summary") or {}).get("mask") or {}).get("start_header_stamp_ns")
        if start_ns is None or int(start_ns) != cell.mask_start_ns:
            raise BlackoutCampaignError("sequence result mask start differs from the committed injection point")
    elif blackout is not None:
        raise BlackoutCampaignError("un-injected replica unexpectedly ran through the masking path")
    status = value.get("status")
    if status not in campaign.ALL_STATUSES or status == "ACTIVE":
        raise BlackoutCampaignError("sequence result status is unknown or not terminal: {}".format(status))
    validity = value.get("evidence_validity")
    if validity not in campaign.EVIDENCE_VALIDITIES:
        raise BlackoutCampaignError("sequence result evidence_validity is unknown")
    close = value.get("estimator_close_receipt") or {}
    classification = "retained_algorithm_outcome" if status in campaign.RETAINED_ALGORITHM_STATUSES and validity == "VALID" else "fatal"
    if classification == "retained_algorithm_outcome" and (
        close.get("estimator_process_group_closed") is not True or close.get("runtime_services_closed") is not True
    ):
        raise BlackoutCampaignError("retained outcome lacks unambiguous estimator closure")
    return {"value": value, "identity": identity, "classification": classification, "checksummed_paths": sorted(checksummed), "run_directory": run_directory}


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
        self.run_log = root / "RUN_LOG.md"
        self.table = load_table()
        self.bags = MaskedBags(root, self.log_dir, self.table)
        self.last_end = 0.0
        self.port_index = 0

    def log(self, line: str) -> None:
        with self.run_log.open("a", encoding="utf-8") as stream:
            stream.write("- {} {}\n".format(utc_now(), line))
        print(line, flush=True)

    def _git(self) -> Mapping[str, Any]:
        identity = campaign.git_identity(REPO_ROOT)
        expected = self.options.get("expected_tooling_commit")
        if identity["dirty"]:
            raise BlackoutCampaignError("tooling repository is dirty (including untracked files); refusing to run")
        if expected and identity["head_sha"] != expected:
            raise BlackoutCampaignError("tooling HEAD {} differs from the pinned {}".format(identity["head_sha"], expected))
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
            return value
        record: Dict[str, Any] = {"schema": CELL_SCHEMA, "campaign_id": CAMPAIGN_ID, **cell.describe(), "planned_utc": utc_now()}
        runnable, reason = cell.runnable
        if not runnable:
            record.update({"outcome": "NOT_RUNNABLE", "status": "NOT_RUNNABLE", "not_runnable_reason": reason, "estimator_attempted": False, "started_utc": None, "finished_utc": None})
            self._publish_driver_record(cell, record)
            self.log("{} {} NOT_RUNNABLE ({})".format(cell.set_name, cell.run_id, reason))
            return record
        if location["run_directory"].exists() and not location["result"].is_file():
            record.update({"outcome": "UNCLOSED_RUN_DIRECTORY_RETAINED", "status": "INTERRUPTED", "estimator_attempted": None, "note": "run directory exists without a published sequence_result.json; retained, not rerun"})
            self._publish_driver_record(cell, record)
            return record
        if location["result"].is_file():
            validated = validate_closed_result(cell, location["result"])
            record["note"] = "closed sequence_result found without a driver record; adopted after validation"
            return self._close_cell(cell, record, None, validated, None)
        manifest_path: Optional[Path] = None
        manifest_value: Optional[Dict[str, Any]] = None
        if cell.masked:
            manifest_path, manifest_value = self.bags.ensure(cell)
            record["mask_manifest"] = {"path": str(manifest_path), "sha256": campaign.sha256_file(manifest_path), "masked_bag_sha256": manifest_value["masked_bag"]["sha256"], "dropped_per_topic": manifest_value["dropped_per_topic"], "driver": manifest_value.get("_driver")}
        self._cooldown()
        git_before = self._git()
        port = self._next_port()
        command = build_command(self.paths, cell, self.root, {**self.options, "ros_port": port}, manifest_path)
        stdout_path = self.log_dir / (cell.run_id + ".stdout.log")
        stderr_path = self.log_dir / (cell.run_id + ".stderr.log")
        record.update({"command": command, "ros_port": port, "tooling_git_before_launch": git_before, "started_utc": utc_now(), "started_monotonic": time.monotonic()})
        with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
            completed = subprocess.run(command, check=False, stdout=out, stderr=err)
        self.last_end = time.monotonic()
        record.update({
            "finished_utc": utc_now(),
            "wall_seconds": time.monotonic() - record.pop("started_monotonic"),
            "returncode": completed.returncode,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "tooling_git_after_close": campaign.git_identity(REPO_ROOT),
        })
        if not location["result"].is_file():
            record.update({"outcome": "RUNNER_PUBLISHED_NO_RESULT", "status": "INFRASTRUCTURE_FAILED", "estimator_attempted": None, "stderr_tail": stderr_path.read_text(errors="replace")[-4000:]})
            self._publish_driver_record(cell, record)
            self.log("{} {} RUNNER_PUBLISHED_NO_RESULT rc={}".format(cell.set_name, cell.run_id, completed.returncode))
            return record
        validated = validate_closed_result(cell, location["result"])
        return self._close_cell(cell, record, completed.returncode, validated, manifest_path)

    def _close_cell(self, cell: Cell, record: Dict[str, Any], returncode: Optional[int], validated: Mapping[str, Any], manifest_path: Optional[Path]) -> Mapping[str, Any]:
        value = validated["value"]
        run_directory = validated["run_directory"]
        inputs = value.get("inputs") or {}
        mechanism = value.get("robustness_mechanism") or {}
        recovery = mechanism.get("blackout_recovery") or {}
        completion = value.get("completion") if isinstance(value.get("completion"), dict) else {}
        driver_dir = cell.paths(self.root)["driver"]
        driver_dir.mkdir(parents=True, exist_ok=True)
        # full recovery event log, verbatim
        if recovery.get("event_lines"):
            (driver_dir / "recovery_events.log").write_text("\n".join(recovery["event_lines"]) + "\n", encoding="utf-8")
        if manifest_path is None and isinstance(value.get("blackout"), dict):
            candidate = Path(str(value["blackout"].get("manifest_path")))
            manifest_path = candidate if candidate.is_file() else None
        if manifest_path is not None and manifest_path.is_file():
            shutil.copyfile(str(manifest_path), str(driver_dir / "mask_manifest.json"))
            record.setdefault("mask_manifest", {"path": str(manifest_path), "sha256": campaign.sha256_file(manifest_path)})
        record.update({
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
            "bag_identity_estimator": inputs.get("bag"),
            "bag_identity_frozen_source": inputs.get("blackout_source_bag") or inputs.get("bag"),
            "config_dependencies": inputs.get("config_dependencies"),
            "runner_identity": inputs.get("runner"),
            "estimator_argv": value.get("estimator_argv"),
            "resolved_parameters_sha256": ((value.get("resolved_parameters") or {}).get("artifact", {}) or {}).get("sha256"),
            "runner_started_utc": value.get("started_utc"),
            "runner_finished_utc": value.get("finished_utc"),
            "runner_duration_seconds": value.get("duration_seconds"),
            "blackout": value.get("blackout"),
            "robustness_mechanism_status": mechanism.get("status"),
            "robustness_mechanism_reason": mechanism.get("reason"),
            "recovery": {
                "enabled_expected": recovery.get("enabled_expected"),
                "contract_lines_ok": recovery.get("contract_lines_ok"),
                "event_line_count": recovery.get("event_line_count"),
                "event_counts": recovery.get("event_counts"),
                "activations": recovery.get("activations"),
                "commits": recovery.get("commits"),
                "failures": recovery.get("failures"),
                "attempt_count": recovery.get("attempt_count"),
                "attempt_reason_histogram": recovery.get("attempt_reason_histogram"),
                "attempt_supplied_counts": recovery.get("attempt_supplied_counts"),
                "attempt_valid_counts": recovery.get("attempt_valid_counts"),
                "attempt_inlier_counts": recovery.get("attempt_inlier_counts"),
                "consensus_reject_count": len(recovery.get("consensus_rejects") or []),
                "consensus_pass_count": len(recovery.get("consensus_passes") or []),
                "recovery_failed_count": len(recovery.get("recovery_failed_events") or []),
                "degraded_frame_count": recovery.get("degraded_frame_count"),
                "state_preserved_on_every_attempt": recovery.get("state_preserved_on_every_attempt"),
                "triggers": recovery.get("triggers"),
                "commit_events": recovery.get("commit_events"),
                "first_resumed_covariances": recovery.get("first_resumed_covariances"),
                "recovery_failed_events": recovery.get("recovery_failed_events"),
                "summary": recovery.get("summary"),
                "event_log_file": str(driver_dir / "recovery_events.log") if recovery.get("event_lines") else None,
            } if recovery else None,
            "completion": {k: completion.get(k) for k in ("pass", "tail_gap_pass", "maximum_state_gap_pass", "maximum_state_gap_s", "tail_gap_s", "unsupported_state_gaps", "supported_input_gap_count", "recovery_supported_state_gap_count", "recovery_supported_state_gaps", "input_gap_count", "state_gap_count", "first_state_timestamp_s", "last_state_timestamp_s")} if completion else None,
            "blackout_seam": completion.get("blackout_seam") if completion else None,
            "blackout_metrics": completion.get("blackout_metrics") if completion else None,
            "passage": value.get("passage"),
            "outcome_facts": value.get("outcome_facts"),
            "input_interval": {k: (value.get("input_interval") or {}).get(k) for k in ("first_selected_input_timestamp_s", "last_selected_input_timestamp_s", "selected_pair_count", "gaps_over_threshold")},
            "stage_errors": value.get("stage_errors"),
            "failure": value.get("failure"),
        })
        if returncode is not None:
            expected_exit = 0 if value.get("status") in campaign.ELIGIBLE_STATUSES else 2
            record["runner_exit_contract_ok"] = returncode == expected_exit
        config_identity = inputs.get("config") or {}
        record["resolved_recovery_parameter"] = resolved_recovery_parameter(cell, run_directory, config_identity.get("path") if isinstance(config_identity, dict) else None)
        record["determinism"] = determinism_check(cell, run_directory, self.root)
        # single-delta diffs
        if cell.masked and cell.role == "recOFF":
            counterpart = _recon_counterpart_dir(cell, self.root)
            if counterpart is None:
                record["single_delta_vs_recon_counterpart"] = {"status": "COUNTERPART_NOT_YET_RUN"}
            else:
                diff = single_delta_diff(run_directory, counterpart, False, "<absent>" if cell.row.dataset == "kaist_vio" else True)
                diff["counterpart_run_directory"] = str(counterpart)
                record["single_delta_vs_recon_counterpart"] = diff
        if cell.system == "S1-recON-euroc":
            reference = perturb.cdsc1r4_scored_directory(cell.row, "S1")
            if reference is not None:
                diff = single_delta_diff(run_directory, reference, True, "<absent>")
                diff["counterpart_run_directory"] = str(reference)
                ref_result = json.loads((reference / "sequence_result.json").read_text())
                ref_inputs = ref_result.get("inputs") or {}
                diff["config_identical"] = (ref_inputs.get("config") or {}).get("sha256") == config_identity.get("sha256")
                diff["binary_identical"] = (ref_inputs.get("binary") or {}).get("sha256") == (inputs.get("binary") or {}).get("sha256")
                diff["machine_verified_single_delta"] = bool(diff.get("single_delta") and diff["config_identical"] and diff["binary_identical"])
                record["single_delta_vs_frozen_s1_euroc"] = diff
        # ground truth is opened only now
        gt_path = Path(str(cell.row.ground_truth["canonical_path"]))
        metrics_dir = driver_dir / "metrics"
        eval_cmd = [str(PYTHON), str(CELL_EVALUATOR), "--run", str(run_directory), "--ground-truth", str(gt_path), "--output", str(metrics_dir)]
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
        gt_dir = driver_dir / "gt-metrics"
        gt_cmd = [str(PYTHON), str(GT_METRICS), "--run", str(run_directory), "--ground-truth", str(gt_path), "--output", str(gt_dir)]
        gt_log = self.log_dir / (cell.run_id + ".gt-metrics.log")
        with gt_log.open("ab") as stream:
            gt_completed = subprocess.run(gt_cmd, check=False, stdout=stream, stderr=subprocess.STDOUT)
        record["gt_metrics_command"] = gt_cmd
        record["gt_metrics_returncode"] = gt_completed.returncode
        gt_json = gt_dir / "blackout_gt_metrics.json"
        if gt_json.is_file():
            gt_value, gt_identity = campaign.load_json(gt_json, "blackout gt metrics")
            record["gt_metrics_status"] = gt_value.get("metrics_status")
            record["gt_metrics_sha256"] = gt_identity.get("sha256")
            record["gt_metrics"] = {k: gt_value.get(k) for k in ("pre_mask_alignment", "commits", "false_commit_count", "commit_error_unassessable_count", "velocity_exposure_count", "post_mask_under_pre_mask_alignment", "post_recovery_rpe_1s")}
        else:
            record["gt_metrics_status"] = "GT_METRICS_FAILED"
            record["gt_metrics"] = None
            record["gt_metrics_error_tail"] = gt_log.read_text(errors="replace")[-2000:]
        self._publish_driver_record(cell, record)
        rec = record.get("recovery") or {}
        self.log(
            "{} {} -> {} ({}, {}) passage={} commits={} failures={} hist={} false_commits={} ttr={}".format(
                cell.set_name, cell.run_id, record.get("status"), record.get("evidence_validity"), record.get("passage_reason"),
                record.get("passage_complete"), rec.get("commits"), rec.get("failures"), rec.get("attempt_reason_histogram"),
                (record.get("gt_metrics") or {}).get("false_commit_count"), (record.get("blackout_metrics") or {}).get("time_to_recover_s"),
            )
        )
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


def integrity_gate(root: Path, records: Sequence[Mapping[str, Any]], label: str) -> Dict[str, Any]:
    checks = []
    for record in records:
        det = record.get("determinism") or {}
        checks.append({
            "run_id": record.get("run_id"),
            "sequence": record.get("sequence"),
            "system": record.get("system"),
            "status": record.get("status"),
            "evidence_validity": record.get("evidence_validity"),
            "cdsc1r4_status": (det.get("against_cdsc1r4") or {}).get("status") if det.get("against_cdsc1r4") else "NOT_APPLICABLE",
            "perturb1_status": (det.get("against_perturb1_same_system") or {}).get("status") if det.get("against_perturb1_same_system") else "NOT_APPLICABLE",
            "ablate1_status": (det.get("against_ablate1") or {}).get("status") if det.get("against_ablate1") else "NOT_APPLICABLE",
            "fresh_trajectory_sha256": det.get("fresh_trajectory_sha256"),
        })

    def ok(c: Mapping[str, Any]) -> bool:
        statuses = [c["cdsc1r4_status"], c["perturb1_status"], c["ablate1_status"]]
        return c["status"] != "NOT_RUNNABLE" and any(s == "BYTE_IDENTICAL" for s in statuses) and all(s in ("BYTE_IDENTICAL", "NOT_APPLICABLE") for s in statuses)

    verdict = "PASS" if checks and all(ok(c) for c in checks) else "FAIL"
    result = {
        "schema": "schurvio.icra27.blackout.integrity_gate.v1",
        "campaign_id": CAMPAIGN_ID,
        "gate": label,
        "recorded_utc": utc_now(),
        "verdict": verdict,
        "cells": checks,
        "rule": "every replica must be BYTE_IDENTICAL (state_estimate.txt, state_deviation.txt, estimate_raw.tum) to at least one recorded reference and never MISMATCH any; otherwise STOP",
    }
    directory = root / "integrity"
    directory.mkdir(parents=True, exist_ok=True)
    _write_new(directory / "{}_GATE.json".format(label.upper()), _json_bytes(result))
    perturb._write_sha256sums(directory)
    return result


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("action", choices=("run", "plan"))
    value.add_argument("--artifact-root", required=True, type=Path)
    value.add_argument("--sets", default=",".join(SETS))
    value.add_argument("--timeout-seconds", type=float, default=1800.0)
    value.add_argument("--cpu-list", default="8-15")
    value.add_argument("--cooldown-seconds", type=float, default=5.0)
    value.add_argument("--base-ros-port", type=int, default=18300)
    value.add_argument("--expected-tooling-commit", default=None)
    value.add_argument("--no-checkpoint", action="store_true")
    value.add_argument("--only", default=None, help="comma-separated run_id substrings (smoke tests / partial re-entry)")
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
    if args.only:
        needles = [n for n in args.only.split(",") if n]
        cells = [c for c in cells if any(n in c.run_id for n in needles)]
    if args.action == "plan":
        for cell in cells:
            print(json.dumps(cell.describe(), sort_keys=True))
        return 0
    prereg = campaign.file_identity(PREREG_FILE)
    table = campaign.file_identity(TABLE_FILE)
    driver = Driver(root, {
        "timeout_seconds": args.timeout_seconds,
        "cpu_list": args.cpu_list,
        "cooldown_seconds": args.cooldown_seconds,
        "base_ros_port": args.base_ros_port,
        "expected_tooling_commit": args.expected_tooling_commit,
    })
    driver.events.append("campaign-start-" + "-".join(sets), {"prereg": prereg, "injection_table": table, "tooling_git": campaign.git_identity(REPO_ROOT), "planned_cells": [c.describe() for c in cells], "options": driver.options})
    exit_code = 0
    try:
        for set_name in sets:
            set_cells = [c for c in cells if c.set_name == set_name]
            driver.log("set {} start ({} cells; prereg sha256 {}; table sha256 {}; tooling {})".format(set_name, len(set_cells), prereg["sha256"], table["sha256"], campaign.git_identity(REPO_ROOT)["head_sha"]))
            records = []
            for cell in set_cells:
                records.append(driver.run_cell(cell))
            driver.bags.release()
            if set_name in ("step0", "integrity"):
                gate = integrity_gate(root, records, "inertness" if set_name == "step0" else "integrity")
                driver.events.append(set_name + "-gate", {"gate": gate})
                driver.log("{} GATE {}".format(set_name.upper(), gate["verdict"]))
                if gate["verdict"] != "PASS":
                    exit_code = 3
                    break
            if not args.no_checkpoint:
                receipt = driver.checkpoint(set_name)
                driver.log("set {} closed; checkpoint {} rc={}".format(set_name, receipt["output"], receipt["returncode"]))
    except (BlackoutCampaignError, ablate.AblationError, perturb.PerturbationError, campaign.CampaignError) as exc:
        driver.bags.release()
        driver.events.append("campaign-error", {"error": str(exc)})
        driver.log("BLACKOUT_CAMPAIGN_ERROR: {}".format(exc))
        return 4
    driver.events.append("campaign-end-" + "-".join(sets), {"exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
