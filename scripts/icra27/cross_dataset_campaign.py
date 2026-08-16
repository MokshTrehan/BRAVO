#!/usr/bin/python3.8
"""Resume-safe CDSC-1R4 U0/S1 campaign orchestrator.

The estimator-facing runners remain the sole owners of a trial directory.  This
driver only chooses the frozen matrix order, launches a runner from the correct
catkin environment, validates the append-only result, and then advances.  A
runner exit code of 2 is deliberately *not* a campaign failure when it published
a valid retained algorithm outcome (for example ``NO_INITIALIZATION``).

Ground truth is not passed to, opened by, or identity-checked by this module
until both scored cells in a pair have published terminal close receipts.  The
same boundary gates TUM-VI reference extraction.  Qualitative capture is a
second replay lane and cannot begin until all 50 scored cells are closed.
"""

from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
import uuid

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
PYTHON = Path("/usr/bin/python3.8")
BASH = Path("/bin/bash")
PROTOCOL_ID = "CDSC-1R4"
PROTOCOL_FILE = REPO_ROOT / "docs" / "icra27" / "CROSS_DATASET_SYSTEM_COMPARISON_PROTOCOL.md"
MATRIX_FILE = REPO_ROOT / "project" / "icra27_cross_dataset_matrix.yaml"

GENERIC_RUNNER = SCRIPT_DIR / "cross_dataset_trial.py"
KAIST_RUNNER = SCRIPT_DIR / "cross_dataset_kaist_trial.py"
PAIR_EVALUATOR = SCRIPT_DIR / "cross_dataset_pair_evaluator.py"
TUM_EXTRACTOR = SCRIPT_DIR / "tum_vi_reference_extract.py"
GEOMETRY_BUNDLER = SCRIPT_DIR / "cross_dataset_geometry_bundle.py"

U0_SOURCE_ROOT = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/external/open_vins"
)
U0_SETUP = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/build/open_vins_ws/devel/setup.bash"
)
U0_BINARY = Path(
    "/home/moksh/schurvio-baseline-triad/20260809T190830Z/build/open_vins_ws/devel/lib/ov_msckf/ros1_serial_msckf"
)
U0_COMMIT = "69488123ed9362dd44b6f28e7f4680abbff1442b"
U0_TREE = "12ab1c94ccae78ad50fa7376f47f0e55e2672257"
S1_SETUP = REPO_ROOT / "build" / "cp0-ws" / "devel" / "setup.bash"
S1_BINARY = REPO_ROOT / "build" / "cp0-ws" / "devel" / "lib" / "ov_msckf" / "ros1_serial_msckf"

GENERIC_LAUNCH = {
    "U0": REPO_ROOT / "project" / "icra27_cross_dataset_u0_serial.launch",
    "S1": REPO_ROOT / "project" / "icra27_cross_dataset_s1_serial.launch",
}
KAIST_LAUNCH = {
    "U0": REPO_ROOT / "project" / "icra27_kaist_u0_serial.launch",
    "S1": REPO_ROOT / "project" / "rotation_robustness_serial.launch",
}
KAIST_CONFIG = {
    "U0": U0_SOURCE_ROOT / "config" / "kaist_vio" / "estimator_config.yaml",
    "S1": REPO_ROOT / "config" / "kaist_vio_rotation_robustness" / "estimator_config.yaml",
}

RUN_SCHEMA = "schurvio.icra27.cross_dataset.sequence_result.v1"
PAIR_SCHEMA = "schurvio.icra27.cross_dataset.pair_result.v1"
KAIST_CENSUS_SCHEMA = "schurvio.icra27.cross_dataset.kaist_pair_census.v2"
KAIST_FULL_CENSUS_SCHEMA = "schurvio.icra27.kaist_pairing_census.v1"
TUM_REFERENCE_SCHEMA = "schurvio.icra27.tum_vi_reference_extract.v1"
MATRIX_SCHEMA = "schurvio.icra27.cross_dataset_matrix.v1"
EVENT_SCHEMA = "schurvio.icra27.cross_dataset.campaign_event.v1"
STATE_SCHEMA = "schurvio.icra27.cross_dataset.campaign_state.v1"

SYSTEMS = ("U0", "S1")
LANES = ("scored", "capture")
DATASETS = ("euroc_mav", "tum_vi", "kaist_vio")
ELIGIBLE_STATUSES = frozenset(("COMPLETED", "COMPLETED_WITH_TEARDOWN_DEFECT"))
RETAINED_ALGORITHM_STATUSES = frozenset(
    (
        "COMPLETED",
        "COMPLETED_WITH_TEARDOWN_DEFECT",
        "NO_INITIALIZATION",
        "ESTIMATOR_CRASH",
        "TRACKING_LOSS",
        "NUMERIC_FAILURE",
        "PARTIAL",
        "TIMED_OUT",
    )
)
FATAL_STATUSES = frozenset(
    (
        "ACTIVE",
        "TEARDOWN_FAILED",
        "CAPTURE_INCOMPLETE",
        "INVALID_LINKAGE",
        "INVALID_OUTPUT",
        "INFRASTRUCTURE_FAILED",
        "INTERRUPTED",
    )
)
ALL_STATUSES = RETAINED_ALGORITHM_STATUSES | FATAL_STATUSES
EVIDENCE_VALIDITIES = frozenset(
    ("VALID", "INVALID_INFRA", "INVALID_PROVENANCE", "CAPTURE_LINK_INVALID")
)
SHA256_RE = re.compile(r"[0-9a-f]{64}")
GIT_OBJECT_RE = re.compile(r"[0-9a-f]{40}")
SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")

EXPECTED_ROWS: Tuple[Tuple[str, str, float], ...] = (
    ("euroc_mav", "MH_01_easy", 40.0),
    ("euroc_mav", "MH_02_easy", 35.0),
    ("euroc_mav", "MH_03_medium", 5.0),
    ("euroc_mav", "MH_04_difficult", 10.0),
    ("euroc_mav", "MH_05_difficult", 5.0),
    ("euroc_mav", "V1_01_easy", 0.0),
    ("euroc_mav", "V1_02_medium", 0.0),
    ("euroc_mav", "V1_03_difficult", 0.0),
    ("euroc_mav", "V2_01_easy", 0.0),
    ("euroc_mav", "V2_02_medium", 0.0),
    ("euroc_mav", "V2_03_difficult", 0.0),
    ("tum_vi", "dataset-room4_512_16", 0.0),
    ("tum_vi", "dataset-corridor4_512_16", 0.0),
    ("tum_vi", "dataset-outdoors4_512_16", 0.0),
    ("kaist_vio", "infinite/infinite_fast.bag", 0.0),
    ("kaist_vio", "square/square_fast.bag", 0.0),
    ("kaist_vio", "square/square.bag", 0.0),
    ("kaist_vio", "circle/circle_head.bag", 0.0),
    ("kaist_vio", "rotation/rotation.bag", 0.0),
    ("kaist_vio", "infinite/infinite.bag", 0.0),
    ("kaist_vio", "square/square_head.bag", 0.0),
    ("kaist_vio", "circle/circle.bag", 0.0),
    ("kaist_vio", "rotation/rotation_fast.bag", 0.0),
    ("kaist_vio", "circle/circle_fast.bag", 0.0),
    ("kaist_vio", "infinite/infinite_head.bag", 0.0),
)

# Frozen, read-only projection of the exact estimator-facing KAIST callback
# populations.  Each tuple is raw dispatch count, gate-accepted count,
# frequency-drop count, first accepted camera-header nanoseconds, last accepted
# camera-header nanoseconds, and the exact accepted >0.20 s gap endpoint pairs.
# The accompanying frozen record digest additionally binds both ordered
# raw/accepted callback digests.
EXPECTED_KAIST_GATE_POPULATIONS: Mapping[
    str, Mapping[str, Tuple[int, int, int, int, int, Tuple[Tuple[int, int], ...]]]
] = {
    "infinite/infinite_fast.bag": {
        "U0": (6307, 5444, 863, 1598871941366774760, 1598872152875608572, ()),
        "S1": (6335, 5471, 864, 1598871941366774760, 1598872152875608572, ()),
    },
    "square/square_fast.bag": {
        "U0": (4338, 3660, 678, 1599132677029715590, 1599132822230981657, ()),
        "S1": (4352, 3670, 682, 1599132677029715590, 1599132822230981657, ()),
    },
    "square/square.bag": {
        "U0": (4812, 4089, 723, 1599132361270392782, 1599132521949611397, ()),
        "S1": (4821, 4102, 719, 1599132361270392782, 1599132521949611397, ()),
    },
    "circle/circle_head.bag": {
        "U0": (4437, 3766, 671, 1598871243198335600, 1598871392076656683, ()),
        "S1": (4462, 3782, 680, 1598871243198335600, 1598871392076656683, ()),
    },
    "rotation/rotation.bag": {
        "U0": (
            4671,
            3898,
            773,
            1599131118058088320,
            1599131288008853966,
            ((1599131266390101559, 1599131279804864812),),
        ),
        "S1": (
            4687,
            3920,
            767,
            1599131118058088320,
            1599131288008853966,
            ((1599131266390101559, 1599131279832098734),),
        ),
    },
    "infinite/infinite.bag": {
        "U0": (4456, 3841, 615, 1598871591153193119, 1598871740590119893, ()),
        "S1": (4476, 3854, 622, 1598871591153193119, 1598871740590119893, ()),
    },
    "square/square_head.bag": {
        "U0": (8346, 7051, 1295, 1599132943663684321, 1599133223160248982, ()),
        "S1": (8377, 7078, 1299, 1599132943663684321, 1599133223160248982, ()),
    },
    "circle/circle.bag": {
        "U0": (4736, 4024, 712, 1598870181880224435, 1598870340716870986, ()),
        "S1": (4761, 4048, 713, 1598870181880224435, 1598870340716870986, ()),
    },
    "rotation/rotation_fast.bag": {
        "U0": (3745, 3169, 576, 1599131363257816311, 1599131488634830502, ()),
        "S1": (3760, 3182, 578, 1599131363257816311, 1599131488634830502, ()),
    },
    "circle/circle_fast.bag": {
        "U0": (4508, 3906, 602, 1598870816620848170, 1598870967371229865, ()),
        "S1": (4522, 3923, 599, 1598870816620848170, 1598870967371229865, ()),
    },
    "infinite/infinite_head.bag": {
        "U0": (4740, 4088, 652, 1598872236838785409, 1598872395623072618, ()),
        "S1": (4757, 4104, 653, 1598872236838785409, 1598872395623072618, ()),
    },
}
EXPECTED_KAIST_GATE_PREFLIGHT_SHA256 = (
    "915d816de64a408d10c0e362dad1368454a7ba465c6dba1302d565de06efef4b"
)


class CampaignError(RuntimeError):
    """A fail-closed campaign orchestration error."""


class _UniqueLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> Mapping[str, Any]:
    result: Dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise CampaignError("duplicate YAML key: {}".format(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


@dataclass(frozen=True)
class MatrixRow:
    order: int
    dataset: str
    sequence: str
    system_order: Tuple[str, str]
    bag_start_seconds: float
    bag: Mapping[str, Any]
    ground_truth: Mapping[str, Any]


@dataclass(frozen=True)
class RuntimePaths:
    repo_root: Path = REPO_ROOT
    protocol: Path = PROTOCOL_FILE
    matrix: Path = MATRIX_FILE
    generic_runner: Path = GENERIC_RUNNER
    kaist_runner: Path = KAIST_RUNNER
    pair_evaluator: Path = PAIR_EVALUATOR
    tum_extractor: Path = TUM_EXTRACTOR
    geometry_bundler: Path = GEOMETRY_BUNDLER
    u0_source_root: Path = U0_SOURCE_ROOT
    u0_setup: Path = U0_SETUP
    u0_binary: Path = U0_BINARY
    s1_setup: Path = S1_SETUP
    s1_binary: Path = S1_BINARY


@dataclass(frozen=True)
class CampaignOptions:
    artifact_root: Path
    lane: str
    dataset: str = "all"
    start_order: Optional[int] = None
    end_order: Optional[int] = None
    timeout_seconds: float = 21600.0
    cpu_list: str = "8-15"
    cooldown_seconds: float = 5.0
    base_ros_port: int = 18100
    expected_tooling_commit: Optional[str] = None
    expected_tooling_tree: Optional[str] = None


@dataclass(frozen=True)
class RunLocation:
    key: str
    run_id: str
    output_root: Path
    run_directory: Path
    result_path: Path


@dataclass(frozen=True)
class ValidatedResult:
    path: Path
    value: Mapping[str, Any]
    identity: Mapping[str, Any]
    classification: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


Executor = Callable[[Sequence[str]], CommandResult]
SleepFunction = Callable[[float], None]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_identity(path: Path) -> Mapping[str, Any]:
    if path.is_symlink():
        raise CampaignError("refusing symlink identity: {}".format(path))
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CampaignError("file does not resolve: {}".format(path)) from exc
    if not resolved.is_file():
        raise CampaignError("not a regular file: {}".format(resolved))
    before = resolved.stat()
    digest = sha256_file(resolved)
    after = resolved.stat()
    fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in fields):
        raise CampaignError("file changed while hashing: {}".format(resolved))
    return {"path": str(resolved), "size_bytes": after.st_size, "sha256": digest}


def _strict_json_object(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CampaignError("duplicate JSON key: {}".format(key))
        result[key] = value
    return result


def load_json(path: Path, label: str) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
    identity_before = file_identity(path)
    try:
        payload = path.resolve(strict=True).read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CampaignError("nonfinite JSON token {}".format(token))
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignError("cannot parse {}: {}".format(label, path)) from exc
    if not isinstance(value, dict):
        raise CampaignError("{} is not a JSON object".format(label))
    observed = {
        "path": str(path.resolve(strict=True)),
        "size_bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }
    if observed != identity_before or file_identity(path) != observed:
        raise CampaignError("{} changed while reading".format(label))
    return value, observed


def _validate_identity_record(value: Any, label: str) -> None:
    if not isinstance(value, dict):
        raise CampaignError("{} is not an identity object".format(label))
    if not isinstance(value.get("path"), str) or not value["path"]:
        raise CampaignError("{} path is malformed".format(label))
    size = value.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise CampaignError("{} size is malformed".format(label))
    digest = value.get("sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise CampaignError("{} SHA-256 is malformed".format(label))


def _identity_projection(value: Any, label: str) -> Mapping[str, Any]:
    """Compare the stable common subset of runner and campaign identities."""

    _validate_identity_record(value, label)
    return {
        "path": str(Path(str(value["path"])).resolve(strict=True)),
        "size_bytes": value["size_bytes"],
        "sha256": value["sha256"],
    }


def _same_identity(left: Any, right: Any, label: str) -> bool:
    return _identity_projection(left, label + " left") == _identity_projection(
        right, label + " right"
    )


def safe_sequence(sequence: str) -> str:
    value = SAFE_COMPONENT_RE.sub("_", sequence).strip("._-")
    if not value or len(value) > 100:
        raise CampaignError("sequence cannot form a safe artifact component")
    return value


def row_key(row: MatrixRow) -> str:
    return "{:02d}-{}".format(row.order, safe_sequence(row.sequence))


def run_location(root: Path, lane: str, row: MatrixRow, system: str) -> RunLocation:
    key = row_key(row)
    run_id = "cdsc1r4-{:02d}-{}-{}-{}-a1".format(
        row.order, safe_sequence(row.sequence), system.lower(), lane
    )
    output_root = root / lane / key / system
    run_directory = output_root / run_id
    return RunLocation(
        key=key,
        run_id=run_id,
        output_root=output_root,
        run_directory=run_directory,
        result_path=run_directory / "sequence_result.json",
    )


def _validate_file_record(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CampaignError("{} must be an object".format(label))
    path = value.get("path", value.get("canonical_path"))
    if not isinstance(path, str) or not path:
        raise CampaignError("{} path is malformed".format(label))
    size = value.get("bytes", value.get("size_bytes"))
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise CampaignError("{} byte count is malformed".format(label))
    digest = value.get("sha256")
    if not isinstance(digest, str) or SHA256_RE.fullmatch(digest) is None:
        raise CampaignError("{} SHA-256 is malformed".format(label))
    return value


def load_matrix(path: Path, require_canonical: bool = True) -> Tuple[MatrixRow, ...]:
    if path.is_symlink():
        raise CampaignError("refusing symlink campaign matrix")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise CampaignError("campaign matrix does not resolve") from exc
    if require_canonical and resolved != MATRIX_FILE.resolve(strict=True):
        raise CampaignError("campaign matrix is not the canonical CDSC-1R4 path")
    try:
        value = yaml.load(resolved.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise CampaignError("cannot parse campaign matrix") from exc
    if not isinstance(value, dict) or value.get("schema") != MATRIX_SCHEMA:
        raise CampaignError("campaign matrix schema mismatch")
    if value.get("sequence_count") != 25:
        raise CampaignError("campaign matrix does not declare 25 sequences")
    rows_raw = value.get("sequences")
    if not isinstance(rows_raw, list) or len(rows_raw) != 25:
        raise CampaignError("campaign matrix must contain exactly 25 rows")
    rows: List[MatrixRow] = []
    for index, raw in enumerate(rows_raw, 1):
        if not isinstance(raw, dict):
            raise CampaignError("matrix row {} is not an object".format(index))
        expected_dataset, expected_sequence, expected_start = EXPECTED_ROWS[index - 1]
        if raw.get("order") != index:
            raise CampaignError("matrix order is not exactly 1..25")
        if raw.get("dataset") != expected_dataset or raw.get("sequence") != expected_sequence:
            raise CampaignError("matrix frozen sequence order drift at {}".format(index))
        expected_system_order = ["U0", "S1"] if index % 2 else ["S1", "U0"]
        if raw.get("system_order") != expected_system_order:
            raise CampaignError("matrix within-pair system order drift at {}".format(index))
        start = raw.get("bag_start_seconds")
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise CampaignError("matrix bag start is malformed at {}".format(index))
        if not math.isfinite(float(start)) or float(start) != expected_start:
            raise CampaignError("matrix bag start drift at {}".format(index))
        bag = _validate_file_record(raw.get("bag"), "row {} bag".format(index))
        ground_truth = _validate_file_record(
            raw.get("ground_truth"), "row {} ground truth".format(index)
        )
        capability = ground_truth.get("capability")
        expected_capability = (
            "embedded_partial_intervals" if index in (13, 14) else "full_trajectory"
        )
        if capability != expected_capability:
            raise CampaignError("matrix GT capability drift at {}".format(index))
        rows.append(
            MatrixRow(
                order=index,
                dataset=expected_dataset,
                sequence=expected_sequence,
                system_order=tuple(expected_system_order),
                bag_start_seconds=float(start),
                bag=bag,
                ground_truth=ground_truth,
            )
        )
    return tuple(rows)


def select_rows(rows: Sequence[MatrixRow], options: CampaignOptions) -> Tuple[MatrixRow, ...]:
    if options.lane not in LANES:
        raise CampaignError("invalid lane: {}".format(options.lane))
    if options.dataset != "all" and options.dataset not in DATASETS:
        raise CampaignError("invalid dataset: {}".format(options.dataset))
    start = 1 if options.start_order is None else options.start_order
    end = 25 if options.end_order is None else options.end_order
    if isinstance(start, bool) or isinstance(end, bool) or not (1 <= start <= end <= 25):
        raise CampaignError("order range must satisfy 1 <= start <= end <= 25")
    selected = tuple(
        row
        for row in rows
        if start <= row.order <= end
        and (options.dataset == "all" or row.dataset == options.dataset)
    )
    if not selected:
        raise CampaignError("campaign selection is empty")
    if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0:
        raise CampaignError("timeout must be finite and positive")
    if not math.isfinite(options.cooldown_seconds) or options.cooldown_seconds < 5.0:
        raise CampaignError("cooldown must be finite and at least 5 seconds")
    if options.base_ros_port < 1024 or options.base_ros_port + 99 > 65535:
        raise CampaignError("base ROS port cannot provide the isolated 100-port map")
    if not options.cpu_list or "\n" in options.cpu_list or "\r" in options.cpu_list:
        raise CampaignError("CPU list is malformed")
    return selected


def deterministic_port(base: int, row: MatrixRow, lane: str, system: str) -> int:
    lane_offset = 0 if lane == "scored" else 2
    system_offset = 0 if system == "U0" else 1
    return base + (row.order - 1) * 4 + lane_offset + system_offset


def _config_path(paths: RuntimePaths, dataset: str, system: str) -> Path:
    if dataset == "kaist_vio":
        if system == "U0":
            return paths.u0_source_root / "config" / "kaist_vio" / "estimator_config.yaml"
        return paths.repo_root / "config" / "kaist_vio_rotation_robustness" / "estimator_config.yaml"
    base = paths.u0_source_root if system == "U0" else paths.repo_root
    return base / "config" / dataset / "estimator_config.yaml"


def _launch_path(paths: RuntimePaths, dataset: str, system: str) -> Path:
    if dataset == "kaist_vio":
        name = "icra27_kaist_u0_serial.launch" if system == "U0" else "rotation_robustness_serial.launch"
    else:
        name = "icra27_cross_dataset_u0_serial.launch" if system == "U0" else "icra27_cross_dataset_s1_serial.launch"
    return paths.repo_root / "project" / name


def _binary_path(paths: RuntimePaths, system: str) -> Path:
    return paths.u0_binary if system == "U0" else paths.s1_binary


def _setup_path(paths: RuntimePaths, system: str) -> Path:
    return paths.u0_setup if system == "U0" else paths.s1_setup


def sourced_command(setup: Path, command: Sequence[str]) -> List[str]:
    # Positional parameters keep paths/data out of shell syntax.
    return [
        str(BASH),
        "--noprofile",
        "--norc",
        "-c",
        'source "$1" || exit $?; shift; exec "$@"',
        "cdsc1r4-source",
        str(setup),
        *[str(item) for item in command],
    ]


def build_trial_command(
    paths: RuntimePaths,
    options: CampaignOptions,
    row: MatrixRow,
    system: str,
) -> List[str]:
    location = run_location(options.artifact_root, options.lane, row, system)
    runner = paths.kaist_runner if row.dataset == "kaist_vio" else paths.generic_runner
    inner = [
        str(PYTHON),
        str(runner),
        "run",
        "--protocol-id",
        PROTOCOL_ID,
        "--protocol-file",
        str(paths.protocol),
        "--matrix-file",
        str(paths.matrix),
        "--run-id",
        location.run_id,
        "--attempt-index",
        "1",
        "--dataset",
        row.dataset,
        "--sequence",
        row.sequence,
        "--system",
        system,
        "--mode",
        options.lane,
        "--bag",
        str(row.bag["path"]),
        "--bag-start",
        format(row.bag_start_seconds, ".17g"),
        "--bag-duration",
        "-1",
        "--config",
        str(_config_path(paths, row.dataset, system)),
        "--launch",
        str(_launch_path(paths, row.dataset, system)),
        "--binary",
        str(_binary_path(paths, system)),
        "--output-root",
        str(location.output_root),
        "--ros-port",
        str(deterministic_port(options.base_ros_port, row, options.lane, system)),
        "--timeout-seconds",
        format(options.timeout_seconds, ".17g"),
        "--cpu-list",
        options.cpu_list,
    ]
    if options.lane == "capture":
        scored = run_location(options.artifact_root, "scored", row, system)
        inner.extend(("--scored-result", str(scored.result_path)))
    return sourced_command(_setup_path(paths, system), inner)


def build_pair_command(paths: RuntimePaths, root: Path, row: MatrixRow) -> List[str]:
    return [
        str(PYTHON),
        str(paths.pair_evaluator),
        "--u0-run",
        str(run_location(root, "scored", row, "U0").run_directory),
        "--s1-run",
        str(run_location(root, "scored", row, "S1").run_directory),
        "--ground-truth",
        str(row.ground_truth["canonical_path"]),
        "--matrix",
        str(paths.matrix),
        "--output",
        str(root / "pair" / row_key(row)),
    ]


def build_kaist_rotation_post_pair_command(
    paths: RuntimePaths, root: Path, row: MatrixRow
) -> List[str]:
    if row.dataset != "kaist_vio" or row.sequence != "rotation/rotation.bag":
        raise CampaignError("C2 rotation post-pair command requested for the wrong row")
    return [
        str(PYTHON),
        str(paths.kaist_runner),
        "post-pair-rotation",
        "--u0-result",
        str(run_location(root, "scored", row, "U0").result_path),
        "--s1-result",
        str(run_location(root, "scored", row, "S1").result_path),
        "--ground-truth",
        str(row.ground_truth["canonical_path"]),
        "--matrix",
        str(paths.matrix),
        "--output",
        str(root / "mechanism" / row_key(row) / "kaist_rotation_post_pair.json"),
    ]


def build_tum_extractor_command(paths: RuntimePaths, root: Path, row: MatrixRow) -> List[str]:
    command = [
        str(PYTHON),
        str(paths.tum_extractor),
        "--bag",
        str(row.bag["path"]),
        "--output-dir",
        str(root / "reference" / row_key(row)),
        "--sequence-id",
        row.sequence,
        "--estimator-close-receipt",
        str(run_location(root, "scored", row, "U0").result_path),
        "--expected-bag-bytes",
        str(row.bag["bytes"]),
        "--expected-bag-sha256",
        str(row.bag["sha256"]),
        "--gt-capability",
        str(row.ground_truth["capability"]),
    ]
    if row.ground_truth["capability"] == "full_trajectory":
        command.extend(("--expected-reference-sha256", str(row.ground_truth["sha256"])))
    return command


def build_geometry_command(
    paths: RuntimePaths, root: Path, row: MatrixRow, system: str
) -> List[str]:
    command = [
        str(PYTHON),
        str(paths.geometry_bundler),
        "build",
        "--capture-result",
        str(run_location(root, "capture", row, system).result_path),
        "--matrix-file",
        str(paths.matrix),
        "--output-dir",
        str(root / "geometry" / row_key(row) / system),
    ]
    return command


def _validate_checksum_set(run_directory: Path, result_path: Path) -> frozenset:
    checksum_path = run_directory / "SHA256SUMS"
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise CampaignError("result checksum set is absent: {}".format(checksum_path))
    try:
        lines = checksum_path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise CampaignError("result checksum set is not strict ASCII") from exc
    if not lines:
        raise CampaignError("result checksum set is empty")
    seen = set()
    result_digest: Optional[str] = None
    for line in lines:
        if "  " not in line:
            raise CampaignError("malformed checksum line")
        digest, relative_text = line.split("  ", 1)
        if SHA256_RE.fullmatch(digest) is None:
            raise CampaignError("malformed checksum digest")
        relative = Path(relative_text)
        if (
            not relative_text
            or relative.is_absolute()
            or ".." in relative.parts
            or relative_text in seen
        ):
            raise CampaignError("unsafe or duplicate checksummed path")
        seen.add(relative_text)
        candidate = run_directory / relative
        prefix = run_directory
        for part in relative.parts:
            prefix = prefix / part
            if prefix.is_symlink():
                raise CampaignError("checksummed artifact path contains a symlink")
        try:
            artifact = candidate.resolve(strict=True)
            artifact.relative_to(run_directory.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise CampaignError("checksummed path escapes or is missing") from exc
        if not artifact.is_file() or sha256_file(artifact) != digest:
            raise CampaignError("checksummed artifact mismatch: {}".format(relative_text))
        if artifact == result_path.resolve(strict=True):
            result_digest = digest
    if result_digest is None or result_digest != sha256_file(result_path):
        raise CampaignError("sequence result is not bound by its checksum set")
    return frozenset(seen)


def _live_result_artifact(
    result: Mapping[str, Any], run_directory: Path, name: str, relative: str
) -> Optional[Mapping[str, Any]]:
    artifacts = result.get("artifacts")
    record = artifacts.get(name) if isinstance(artifacts, dict) else None
    if not isinstance(record, dict) or record.get("relative_path") != relative:
        raise CampaignError("{} artifact path contract mismatch".format(name))
    recorded = record.get("identity")
    path = run_directory / relative
    if recorded is None:
        if path.exists() or path.is_symlink():
            raise CampaignError("{} artifact exists but is recorded absent".format(name))
        return None
    live = file_identity(path)
    if not _same_identity(recorded, live, "{} artifact".format(name)):
        raise CampaignError("{} artifact identity differs from live bytes".format(name))
    return live


def _optional_identity_matches(recorded: Any, live: Optional[Mapping[str, Any]], label: str) -> bool:
    if recorded is None or live is None:
        return recorded is None and live is None
    return _same_identity(recorded, live, label)


def _validate_capture_linkage(
    capture: Mapping[str, Any],
    capture_path: Path,
    row: MatrixRow,
    system: str,
    expected_runner: Optional[Path],
    mismatch_expected: bool,
) -> None:
    link = capture.get("scored_linkage")
    if not isinstance(link, dict):
        raise CampaignError("capture outcome lacks scored linkage")
    artifact_root = capture_path.parents[4]
    expected_path = run_location(artifact_root, "scored", row, system).result_path
    linked_path_value = link.get("sequence_result_path")
    if not isinstance(linked_path_value, str) or linked_path_value != str(
        expected_path.resolve(strict=True)
    ):
        raise CampaignError("capture does not link the exact canonical scored result")
    scored = validate_sequence_result(
        expected_path,
        row,
        system,
        "scored",
        expected_runner=expected_runner,
    )
    if scored.classification == "fatal":
        raise CampaignError("capture links a fatal scored result")
    for key in ("sequence_result", "sequence_result_after"):
        if not _same_identity(link.get(key), scored.identity, "scored manifest {}".format(key)):
            raise CampaignError("scored manifest changed before/after capture")
    if link.get("source_unchanged_during_capture") is not True:
        raise CampaignError("scored source changed during capture")

    capture_directory = capture_path.parent.resolve(strict=True)
    scored_directory = scored.path.parent.resolve(strict=True)
    comparisons = link.get("artifact_comparisons")
    expected_before = link.get("expected_artifacts")
    expected_after = link.get("post_capture_expected_artifacts")
    if not all(isinstance(value, dict) for value in (comparisons, expected_before, expected_after)):
        raise CampaignError("capture linkage comparison records are incomplete")
    mismatch_count = 0
    for name, relative in (
        ("state", "trajectory/state_estimate.txt"),
        ("deviation", "trajectory/state_deviation.txt"),
        ("tum", "trajectory/estimate_raw.tum"),
    ):
        scored_identity = _live_result_artifact(scored.value, scored_directory, name, relative)
        capture_identity = _live_result_artifact(capture, capture_directory, name, relative)
        if not _optional_identity_matches(
            expected_before.get(name), scored_identity, "{} pre-capture scored artifact".format(name)
        ) or not _optional_identity_matches(
            expected_after.get(name), scored_identity, "{} post-capture scored artifact".format(name)
        ):
            raise CampaignError("scored {} artifact changed during capture".format(name))
        exact = bool(
            (scored_identity is None and capture_identity is None)
            or (
                scored_identity is not None
                and capture_identity is not None
                and scored_identity["size_bytes"] == capture_identity["size_bytes"]
                and scored_identity["sha256"] == capture_identity["sha256"]
            )
        )
        comparison = comparisons.get(name)
        if not isinstance(comparison, dict):
            raise CampaignError("capture {} comparison is absent".format(name))
        expected_sha = scored_identity["sha256"] if scored_identity is not None else None
        observed_sha = capture_identity["sha256"] if capture_identity is not None else None
        if (
            comparison.get("expected_sha256") != expected_sha
            or comparison.get("observed_sha256") != observed_sha
            or comparison.get("expected_source_unchanged_during_capture") is not True
            or comparison.get("byte_exact") is not exact
        ):
            raise CampaignError("capture {} comparison is not truthful".format(name))
        mismatch_count += int(not exact)

    if mismatch_expected:
        if (
            link.get("status") != "OUTPUT_MISMATCH"
            or link.get("byte_exact") is not False
            or mismatch_count < 1
        ):
            raise CampaignError("capture mismatch evidence is not a proven byte mismatch")
    elif (
        link.get("status") != "LINKED_EXACT"
        or link.get("byte_exact") is not True
        or mismatch_count != 0
    ):
        raise CampaignError("capture outcome lacks exact scored linkage")


def _validate_kaist_census_evidence(
    result: Mapping[str, Any],
    run_directory: Path,
    checksummed_paths: Sequence[str],
    system: str,
) -> None:
    inputs = result.get("inputs")
    after = result.get("input_identities_after")
    if not isinstance(inputs, Mapping) or not isinstance(after, Mapping):
        raise CampaignError("KAIST input pre/postflight identities are absent")
    for name, path in (
        ("visualizer_gate_projection_module", GENERIC_RUNNER),
        ("runtime_summary_parser_module", SCRIPT_DIR / "rotation_robustness_trial.py"),
    ):
        live = file_identity(path)
        if (
            not _same_identity(inputs.get(name), live, name + " preflight")
            or not _same_identity(after.get(name), live, name + " postflight")
        ):
            raise CampaignError("KAIST scientific helper identity drift: {}".format(name))

    records: Dict[str, Mapping[str, Any]] = {}
    values: Dict[str, Mapping[str, Any]] = {}
    for name, relative in (
        ("native_pair_census", "diagnostics/native_pair_census.json"),
        ("kaist_pairing_census", "diagnostics/kaist_pairing_census.json"),
    ):
        recorded = result.get(name)
        _validate_identity_record(recorded, name)
        path = run_directory / relative
        live = file_identity(path)
        if not _same_identity(recorded, live, name):
            raise CampaignError("{} identity differs from live bytes".format(name))
        if relative not in checksummed_paths:
            raise CampaignError("{} is absent from checksum closure".format(name))
        value, loaded_identity = load_json(path, name)
        if not _same_identity(live, loaded_identity, name + " loaded identity"):
            raise CampaignError("{} changed while being validated".format(name))
        records[name] = live
        values[name] = value

    normalized = values["native_pair_census"]
    full = values["kaist_pairing_census"]
    if (
        normalized.get("schema") != KAIST_CENSUS_SCHEMA
        or normalized.get("system") != system
        or full.get("schema") != KAIST_FULL_CENSUS_SCHEMA
    ):
        raise CampaignError("KAIST census schema/system binding mismatch")
    interval = normalized.get("input_interval")
    gate = normalized.get("visualizer_track_frequency_gate")
    config = result.get("config_contract")
    full_bag = full.get("bag")
    full_topics = full.get("topics")
    full_census = full.get("census")
    full_bounds = full.get("selection_bounds")
    if not all(
        isinstance(item, Mapping)
        for item in (
            interval,
            gate,
            config,
            full_bag,
            full_topics,
            full_census,
            full_bounds,
        )
    ):
        raise CampaignError("KAIST census lacks interval/gate/config evidence")
    if not _same_identity(full_bag, inputs.get("bag"), "KAIST full census bag"):
        raise CampaignError("KAIST full census bag differs from estimator input")
    expected_topics = {
        "camera0": "/turnsafe/kaist/infra1/image_raw",
        "camera1": "/turnsafe/kaist/infra2/image_raw",
        "imu": "/mavros/imu/data",
    }
    if set(full_topics) != set(expected_topics) or any(
        not isinstance(full_topics[name], Mapping)
        or full_topics[name].get("name") != topic
        for name, topic in expected_topics.items()
    ):
        raise CampaignError("KAIST full census topic binding mismatch")
    if dict(interval) != result.get("input_interval"):
        raise CampaignError("KAIST census interval differs from sequence result")
    config_frequency = config.get("track_frequency_hz")
    gate_frequency = gate.get("track_frequency_hz")
    if (
        config.get("track_frequency_source") != "canonical_kaist_config"
        or gate.get("track_frequency_source") != "canonical_dataset_config"
        or isinstance(config_frequency, bool)
        or not isinstance(config_frequency, (int, float))
        or isinstance(gate_frequency, bool)
        or not isinstance(gate_frequency, (int, float))
        or not math.isfinite(float(config_frequency))
        or not math.isfinite(float(gate_frequency))
        or float(config_frequency) != float(gate_frequency)
        or float(gate_frequency) != 31.0
    ):
        raise CampaignError("KAIST visualizer gate frequency binding mismatch")

    integer_fields = (
        interval.get("raw_serial_dispatch_pair_count"),
        interval.get("selected_pair_count"),
        interval.get("visualizer_frequency_dropped_pair_count"),
        gate.get("raw_serial_dispatch_count"),
        gate.get("accepted_visualizer_callback_count"),
        gate.get("frequency_dropped_dispatch_count"),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_fields):
        raise CampaignError("KAIST visualizer gate counts are not integers")
    raw_count, accepted_count, dropped_count, gate_raw, gate_accepted, gate_dropped = (
        int(value) for value in integer_fields
    )
    selector_contract = {
        "U0": {
            "source": "upstream_native_record_time_first_forward_stereo",
            "count": "u0_native_pair_count",
            "first": "u0_first_selected_header_stamp_ns",
            "last": "u0_last_selected_header_stamp_ns",
            "bounds": "u0_native",
        },
        "S1": {
            "source": "frozen_s1_exact_header_stereo",
            "count": "s1_exact_pair_count",
            "first": "s1_first_selected_header_stamp_ns",
            "last": "s1_last_selected_header_stamp_ns",
            "bounds": "s1_exact_header",
        },
    }[system]
    selector_bounds = full_bounds.get(selector_contract["bounds"])
    expected_delivery = (
        selector_contract["source"] + "_plus_stock_visualizer_frequency_gate"
    )
    if (
        not isinstance(selector_bounds, Mapping)
        or normalized.get("delivery") != expected_delivery
        or interval.get("source") != expected_delivery
        or interval.get("selector_source") != selector_contract["source"]
        or full_census.get(selector_contract["count"]) != raw_count
        or selector_bounds.get("pair_count") != raw_count
        or full_census.get(selector_contract["first"])
        != interval.get("first_selected_input_timestamp_ns")
        or selector_bounds.get("first_camera0_header_stamp_ns")
        != interval.get("first_selected_input_timestamp_ns")
        or full_census.get(selector_contract["last"])
        != gate.get("raw_final_camera_timestamp_ns")
        or selector_bounds.get("last_camera0_header_stamp_ns")
        != gate.get("raw_final_camera_timestamp_ns")
    ):
        raise CampaignError("KAIST raw selector/gate population join mismatch")
    dropped = gate.get("dropped_dispatches")
    if (
        raw_count <= 0
        or accepted_count <= 0
        or dropped_count < 0
        or raw_count != accepted_count + dropped_count
        or (raw_count, accepted_count, dropped_count)
        != (gate_raw, gate_accepted, gate_dropped)
        or not isinstance(dropped, list)
        or len(dropped) != dropped_count
    ):
        raise CampaignError("KAIST visualizer gate population does not close")
    if any(
        not isinstance(record, Mapping)
        or record.get("reason")
        != "timestamp_less_than_previous_accepted_plus_inverse_track_frequency"
        for record in dropped
    ):
        raise CampaignError("KAIST visualizer drop ledger policy mismatch")
    for name in (
        "raw_serial_dispatch_sequence_sha256",
        "accepted_visualizer_callback_sequence_sha256",
    ):
        if SHA256_RE.fullmatch(str(gate.get(name, ""))) is None:
            raise CampaignError("KAIST visualizer gate digest is invalid")
    if (
        gate.get("accepted_camera_timestamps_strictly_increasing") is not True
        or gate.get("accepted_final_camera_timestamp_ns")
        != gate.get("accepted_maximum_camera_timestamp_ns")
        or gate.get("accepted_final_camera_timestamp_ns")
        != interval.get("last_selected_input_timestamp_ns")
        or interval.get("first_selected_input_timestamp_ns") is None
        or interval.get("last_selected_input_timestamp_ns") is None
    ):
        raise CampaignError("KAIST accepted callback endpoint contract mismatch")
    if gate.get("raw_adjacent_reversed_camera_timestamp_count") != 0:
        raise CampaignError("KAIST raw callback order reverses")
    if system == "S1" and gate.get("raw_adjacent_equal_camera_timestamp_count") != 0:
        raise CampaignError("S1 exact-header raw callbacks are not strict")

    gaps = interval.get("gaps_over_threshold")
    if not isinstance(gaps, list):
        raise CampaignError("KAIST accepted input gaps are malformed")
    for gap in gaps:
        if not isinstance(gap, Mapping):
            raise CampaignError("KAIST accepted input gap is not an object")
        start_ns = gap.get("start_timestamp_ns")
        end_ns = gap.get("end_timestamp_ns")
        if (
            isinstance(start_ns, bool)
            or not isinstance(start_ns, int)
            or isinstance(end_ns, bool)
            or not isinstance(end_ns, int)
            or end_ns <= start_ns
        ):
            raise CampaignError("KAIST accepted input gap endpoints are malformed")
        start_s = (start_ns // 1_000_000_000) + (start_ns % 1_000_000_000) * 1.0e-9
        end_s = (end_ns // 1_000_000_000) + (end_ns % 1_000_000_000) * 1.0e-9
        if (
            gap.get("start_timestamp_s") != start_s
            or gap.get("end_timestamp_s") != end_s
            or gap.get("duration_s") != end_s - start_s
            or end_s - start_s <= 0.20
        ):
            raise CampaignError("KAIST accepted input gap arithmetic mismatch")

    if (
        normalized.get("static_census_schema") != full.get("schema")
        or normalized.get("static_census") != full.get("census")
        or normalized.get("selection_bounds") != full.get("selection_bounds")
        or normalized.get("pair_sets") != full.get("pair_sets")
        or normalized.get("u0_native_diagnostics") != full.get("u0_native_diagnostics")
    ):
        raise CampaignError("KAIST normalized census does not bind the full raw census")

    if system == "S1" and result.get("status") in ELIGIBLE_STATUSES:
        runtime = result.get("pairing_runtime")
        binding = (
            runtime.get("visualizer_gate_binding")
            if isinstance(runtime, Mapping)
            else None
        )
        accepted_input = (
            binding.get("accepted_input") if isinstance(binding, Mapping) else None
        )
        expected_accepted_input = {
            "first_header_stamp_ns": interval.get("first_selected_input_timestamp_ns"),
            "last_header_stamp_ns": interval.get("last_selected_input_timestamp_ns"),
            "callback_count": accepted_count,
            "ordered_callback_sequence_sha256": gate.get(
                "accepted_visualizer_callback_sequence_sha256"
            ),
        }
        if (
            not isinstance(runtime, Mapping)
            or runtime.get("status") != "AVAILABLE"
            or not isinstance(binding, Mapping)
            or binding.get("status") != "PASS"
            or binding.get("runtime_matches_projected_visualizer_gate") is not True
            or accepted_input != expected_accepted_input
        ):
            raise CampaignError("S1 runtime does not bind the accepted KAIST population")


def validate_sequence_result(
    path: Path,
    row: MatrixRow,
    system: str,
    lane: str,
    expected_runner: Optional[Path] = None,
) -> ValidatedResult:
    if path.is_symlink() or path.parent.is_symlink():
        raise CampaignError("canonical sequence result/run directory cannot be a symlink")
    location = run_location(path.parents[4], lane, row, system)
    # The caller may use a fixture root whose lexical path differs after resolve;
    # bind through the deterministic suffix as well as the manifest declaration.
    expected_suffix = Path(lane) / row_key(row) / system / location.run_id / "sequence_result.json"
    try:
        path.resolve(strict=True).relative_to(path.parents[4].resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise CampaignError("sequence result path is not inside its artifact root") from exc
    if Path(*path.parts[-5:]) != expected_suffix:
        raise CampaignError("sequence result is not at the canonical campaign path")
    value, identity = load_json(path, "sequence result")
    run_directory = path.parent.resolve(strict=True)
    checksummed_paths = _validate_checksum_set(run_directory, path)
    expected = {
        "schema": RUN_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "run_id": location.run_id,
        "attempt_index": 1,
        "dataset": row.dataset,
        "sequence": row.sequence,
        "system": system,
        "mode": lane,
    }
    differences = {
        key: {"observed": value.get(key), "expected": wanted}
        for key, wanted in expected.items()
        if value.get(key) != wanted
    }
    if differences:
        raise CampaignError("sequence result binding mismatch: {}".format(differences))
    if value.get("run_directory") != str(run_directory):
        raise CampaignError("sequence result run_directory binding mismatch")
    publication = value.get("publication")
    if not isinstance(publication, dict) or (
        publication.get("sequence_result") != "sequence_result.json"
        or publication.get("checksums") != "SHA256SUMS"
        or publication.get("append_only_run_directory") is not True
    ):
        raise CampaignError("sequence result publication contract mismatch")
    status = value.get("status")
    if status not in ALL_STATUSES:
        raise CampaignError("unknown sequence result status: {}".format(status))
    if status == "ACTIVE":
        raise CampaignError("sequence result is not terminal")
    evidence_validity = value.get("evidence_validity")
    if evidence_validity not in EVIDENCE_VALIDITIES:
        raise CampaignError("sequence result evidence_validity is missing or unknown")
    if expected_runner is not None:
        inputs = value.get("inputs")
        runner_record = inputs.get("runner") if isinstance(inputs, dict) else None
        live_runner = file_identity(expected_runner)
        if not _same_identity(runner_record, live_runner, "sequence result runner"):
            raise CampaignError("sequence result runner identity differs from live final bytes")
    retained_capture_mismatch = bool(
        lane == "capture" and evidence_validity == "CAPTURE_LINK_INVALID"
    )
    if retained_capture_mismatch:
        classification = "retained_capture_link_mismatch"
    elif status in FATAL_STATUSES or evidence_validity != "VALID":
        classification = "fatal"
    else:
        classification = "retained_algorithm_outcome"
    if status in RETAINED_ALGORITHM_STATUSES and evidence_validity == "VALID":
        pass
    elif (
        status in RETAINED_ALGORITHM_STATUSES
        and evidence_validity != "VALID"
        and not retained_capture_mismatch
    ):
        classification = "fatal"
    elif status == "INVALID_LINKAGE" and evidence_validity != "CAPTURE_LINK_INVALID":
        raise CampaignError("invalid capture linkage lacks its evidence classification")
    if row.dataset == "kaist_vio" and classification in (
        "retained_algorithm_outcome",
        "retained_capture_link_mismatch",
    ):
        _validate_kaist_census_evidence(
            value, run_directory, checksummed_paths, system
        )
    close = value.get("estimator_close_receipt")
    if not isinstance(close, dict):
        raise CampaignError("sequence result lacks a close receipt")
    if classification in (
        "retained_algorithm_outcome",
        "retained_capture_link_mismatch",
    ):
        if (
            close.get("estimator_attempted") is not True
            or close.get("estimator_process_group_closed") is not True
            or close.get("runtime_services_closed") is not True
            or not isinstance(close.get("closed_utc"), str)
            or not close.get("closed_utc")
        ):
            raise CampaignError("retained outcome lacks unambiguous estimator closure")
        checks = value.get("checks")
        facts = value.get("outcome_facts")
        if (
            not isinstance(checks, dict)
            or checks.get("status_known") is not True
            or checks.get("runtime_inputs_unchanged") is not True
            or not isinstance(facts, dict)
            or facts.get("runtime_contract_valid") is not True
            or facts.get("teardown_ok") is not True
        ):
            raise CampaignError("retained outcome has ambiguous provenance or teardown")
    if status in ELIGIBLE_STATUSES:
        expected_accuracy = lane == "scored" and bool(value.get("passage", {}).get("complete"))
        if bool(value.get("accuracy_eligible")) != expected_accuracy:
            raise CampaignError("sequence result accuracy eligibility is inconsistent")
    if lane == "capture":
        artifacts = value.get("artifacts")
        raw_record = artifacts.get("raw_geometry") if isinstance(artifacts, dict) else None
        raw_identity = raw_record.get("identity") if isinstance(raw_record, dict) else None
        _validate_identity_record(raw_identity, "closed raw geometry bag")
        if raw_record.get("active_identity") is not None:
            raise CampaignError("capture retains an unclosed active geometry bag")
        raw_path = Path(str(raw_identity["path"]))
        if not _same_identity(raw_identity, file_identity(raw_path), "closed raw geometry bag"):
            raise CampaignError("closed raw geometry bag identity changed")
        if raw_path.stat().st_size <= 0:
            raise CampaignError("closed raw geometry bag is empty")
        try:
            raw_relative = raw_path.resolve(strict=True).relative_to(run_directory).as_posix()
        except ValueError as exc:
            raise CampaignError("raw geometry bag escapes the capture run directory") from exc
        if raw_relative != "geometry/feature_stream.bag" or raw_relative not in checksummed_paths:
            raise CampaignError("raw geometry bag is not bound by the capture checksum set")
        if (run_directory / "geometry" / "feature_stream.bag.active").exists():
            raise CampaignError("capture has a live .active geometry artifact")
        _validate_capture_linkage(
            value,
            path,
            row,
            system,
            expected_runner,
            mismatch_expected=retained_capture_mismatch,
        )
        if status in ELIGIBLE_STATUSES and value.get("qualitative_eligible") is not True:
            if not retained_capture_mismatch:
                raise CampaignError("eligible capture is not qualitative-eligible")
    return ValidatedResult(path=path.resolve(strict=True), value=value, identity=identity, classification=classification)


def _validate_simple_published_result(
    directory: Path,
    manifest_name: str,
    schema: str,
    row: MatrixRow,
    label: str,
    accepted_statuses: Sequence[str] = ("COMPLETE", "COMPLETED"),
) -> Mapping[str, Any]:
    if not directory.is_dir():
        raise CampaignError("{} directory is absent".format(label))
    manifest = directory / manifest_name
    value, _ = load_json(manifest, label)
    _validate_checksum_set(directory, manifest)
    if value.get("schema") != schema or value.get("status") not in accepted_statuses:
        raise CampaignError("{} status/schema mismatch".format(label))
    observed_sequence = value.get("sequence", value.get("sequence_id"))
    if observed_sequence != row.sequence:
        raise CampaignError("{} sequence binding mismatch".format(label))
    if "dataset" in value and value.get("dataset") != row.dataset:
        raise CampaignError("{} dataset binding mismatch".format(label))
    return value


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise CampaignError("git identity failed for {}: {}".format(repo, completed.stderr.strip()))
    return completed.stdout.strip()


def git_identity(repo: Path) -> Mapping[str, Any]:
    resolved = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if resolved != repo.resolve(strict=True):
        raise CampaignError("git repository root differs: {}".format(repo))
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "repository": str(resolved),
        "head_sha": _git(repo, "rev-parse", "HEAD"),
        "head_tree": _git(repo, "rev-parse", "HEAD^{tree}"),
        "dirty": bool(status),
        "status_sha256": sha256_bytes(status.encode("utf-8")),
    }


def validate_git_pins(
    paths: RuntimePaths, expected_tooling_commit: str, expected_tooling_tree: str
) -> Mapping[str, Any]:
    if GIT_OBJECT_RE.fullmatch(expected_tooling_commit) is None:
        raise CampaignError("expected tooling commit must be a full lowercase Git object ID")
    if GIT_OBJECT_RE.fullmatch(expected_tooling_tree) is None:
        raise CampaignError("expected tooling tree must be a full lowercase Git object ID")
    s1 = git_identity(paths.repo_root)
    u0 = git_identity(paths.u0_source_root)
    if s1["dirty"]:
        raise CampaignError("S1 repository is dirty, including untracked files")
    if s1["head_sha"] != expected_tooling_commit or s1["head_tree"] != expected_tooling_tree:
        raise CampaignError("S1 tooling HEAD/tree differs from the explicit campaign pin")
    if u0["dirty"]:
        raise CampaignError("U0 upstream clone is dirty, including untracked files")
    if u0["head_sha"] != U0_COMMIT or u0["head_tree"] != U0_TREE:
        raise CampaignError("U0 upstream HEAD/tree differs from the original pin")
    return {"S1": s1, "U0": u0}


def _regular_required(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise CampaignError("required {} is absent or not a regular file: {}".format(label, path))


def validate_static_inputs(
    paths: RuntimePaths, rows: Sequence[MatrixRow], lane: str
) -> Mapping[str, Any]:
    required = {
        "protocol": paths.protocol,
        "matrix": paths.matrix,
        "generic runner": paths.generic_runner,
        "KAIST runner": paths.kaist_runner,
        "pair evaluator": paths.pair_evaluator,
        "TUM extractor": paths.tum_extractor,
        "U0 setup": paths.u0_setup,
        "S1 setup": paths.s1_setup,
        "U0 binary": paths.u0_binary,
        "S1 binary": paths.s1_binary,
    }
    if lane == "capture":
        required["geometry bundler"] = paths.geometry_bundler
    for label, path in required.items():
        _regular_required(path, label)
    for row in rows:
        bag = Path(str(row.bag["path"]))
        _regular_required(bag, "{} input bag".format(row.sequence))
        if bag.stat().st_size != row.bag["bytes"]:
            raise CampaignError("input bag byte count differs from matrix: {}".format(row.sequence))
        for system in SYSTEMS:
            _regular_required(_config_path(paths, row.dataset, system), "{} {} config".format(row.dataset, system))
            _regular_required(_launch_path(paths, row.dataset, system), "{} {} launch".format(row.dataset, system))
    protocol_text = paths.protocol.read_text(encoding="utf-8", errors="strict")
    if "PROSPECTIVE_NOT_RUN" not in protocol_text or "Protocol ID: `CDSC-1R4`" not in protocol_text:
        raise CampaignError("protocol identity/freeze marker is absent")
    return {label: file_identity(path) for label, path in required.items()}


def _kaist_preflight_projection_digest(cells: Sequence[Mapping[str, Any]]) -> str:
    payload = json.dumps(
        list(cells),
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return sha256_bytes(payload)


def validate_kaist_gate_preflight_record(
    cells: Sequence[Mapping[str, Any]],
    expected_record_sha256: Optional[str] = EXPECTED_KAIST_GATE_PREFLIGHT_SHA256,
) -> Mapping[str, Any]:
    """Fail closed on the complete frozen 22-arm KAIST gate projection."""

    if not isinstance(cells, Sequence) or isinstance(cells, (str, bytes)):
        raise CampaignError("KAIST gate preflight cells are malformed")
    expected_keys = [
        (sequence, system)
        for sequence in EXPECTED_KAIST_GATE_POPULATIONS
        for system in SYSTEMS
    ]
    observed_keys: List[Tuple[str, str]] = []
    normalized: List[Mapping[str, Any]] = []
    for cell in cells:
        if not isinstance(cell, Mapping):
            raise CampaignError("KAIST gate preflight cell is not an object")
        sequence = cell.get("sequence")
        system = cell.get("system")
        if not isinstance(sequence, str) or system not in SYSTEMS:
            raise CampaignError("KAIST gate preflight cell identity is malformed")
        observed_keys.append((sequence, str(system)))
        expected = EXPECTED_KAIST_GATE_POPULATIONS.get(sequence, {}).get(str(system))
        if expected is None:
            raise CampaignError("unexpected KAIST gate preflight cell")
        gaps_raw = cell.get("accepted_gaps_over_threshold_ns")
        if not isinstance(gaps_raw, list):
            raise CampaignError("KAIST gate preflight gap list is malformed")
        gaps: List[Tuple[int, int]] = []
        for gap in gaps_raw:
            if (
                not isinstance(gap, list)
                or len(gap) != 2
                or any(isinstance(value, bool) or not isinstance(value, int) for value in gap)
            ):
                raise CampaignError("KAIST gate preflight gap endpoint is malformed")
            if gap[1] <= gap[0]:
                raise CampaignError("KAIST gate preflight gap endpoint order is invalid")
            gaps.append((int(gap[0]), int(gap[1])))
        observed = (
            cell.get("raw_dispatch_count"),
            cell.get("accepted_callback_count"),
            cell.get("frequency_dropped_count"),
            cell.get("first_accepted_camera_timestamp_ns"),
            cell.get("last_accepted_camera_timestamp_ns"),
            tuple(gaps),
        )
        if observed != expected:
            raise CampaignError(
                "KAIST gate preflight differs for {} {}: observed={} expected={}".format(
                    sequence, system, observed, expected
                )
            )
        for name in (
            "raw_dispatch_sequence_sha256",
            "accepted_callback_sequence_sha256",
        ):
            if SHA256_RE.fullmatch(str(cell.get(name, ""))) is None:
                raise CampaignError("KAIST gate preflight digest is malformed")
        if cell.get("track_frequency_hz") != 31.0:
            raise CampaignError("KAIST gate preflight frequency drift")
        normalized.append(dict(cell))
    if observed_keys != expected_keys or len(set(observed_keys)) != len(expected_keys):
        raise CampaignError("KAIST gate preflight does not contain the frozen 22 arms")
    projection_sha256 = _kaist_preflight_projection_digest(normalized)
    if (
        expected_record_sha256 is not None
        and projection_sha256 != expected_record_sha256
    ):
        raise CampaignError(
            "KAIST gate preflight record digest drift: observed={} expected={}".format(
                projection_sha256, expected_record_sha256
            )
        )
    return {
        "schema": "schurvio.icra27.cross_dataset.kaist_gate_preflight.v1",
        "protocol_id": PROTOCOL_ID,
        "status": "PASS",
        "all_11_sequences_both_arms_projected_before_estimator_launch": True,
        "cell_count": len(normalized),
        "track_frequency_hz": 31.0,
        "projection_sha256": projection_sha256,
        "cells": normalized,
    }


def preflight_all_kaist_gate_populations(
    paths: RuntimePaths, rows: Sequence[MatrixRow]
) -> Mapping[str, Any]:
    """Read only all KAIST metadata and freeze estimator-facing populations."""

    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import cross_dataset_kaist_trial as kaist
    except ImportError as exc:
        raise CampaignError("cannot import the frozen KAIST runner for preflight") from exc
    kaist_rows = [row for row in rows if row.dataset == "kaist_vio"]
    if [row.sequence for row in kaist_rows] != list(EXPECTED_KAIST_GATE_POPULATIONS):
        raise CampaignError("KAIST preflight matrix population/order drift")
    cells: List[Mapping[str, Any]] = []
    for row in kaist_rows:
        bag = Path(str(row.bag["path"]))
        for system in SYSTEMS:
            try:
                config = kaist.validate_config_contract(
                    system, _config_path(paths, row.dataset, system)
                )
                normalized, _full, bag_identity = kaist.kaist_pair_census(
                    system,
                    bag,
                    row.bag_start_seconds,
                    -1.0,
                    float(config["track_frequency_hz"]),
                )
            except Exception as exc:
                raise CampaignError(
                    "KAIST gate preflight failed for {} {}: {}".format(
                        row.sequence, system, exc
                    )
                ) from exc
            if (
                str(Path(str(bag_identity.get("path"))).resolve(strict=True))
                != str(Path(str(row.bag["path"])).resolve(strict=True))
                or bag_identity.get("size_bytes") != row.bag.get("bytes")
                or bag_identity.get("sha256") != row.bag.get("sha256")
            ):
                raise CampaignError("KAIST preflight bag identity differs from matrix")
            interval = normalized.get("input_interval")
            gate = normalized.get("visualizer_track_frequency_gate")
            if not isinstance(interval, Mapping) or not isinstance(gate, Mapping):
                raise CampaignError("KAIST preflight lacks interval/gate evidence")
            gaps = interval.get("gaps_over_threshold")
            if not isinstance(gaps, list):
                raise CampaignError("KAIST preflight gap evidence is malformed")
            cells.append(
                {
                    "sequence": row.sequence,
                    "system": system,
                    "track_frequency_hz": float(config["track_frequency_hz"]),
                    "raw_dispatch_count": interval.get(
                        "raw_serial_dispatch_pair_count"
                    ),
                    "accepted_callback_count": interval.get("selected_pair_count"),
                    "frequency_dropped_count": interval.get(
                        "visualizer_frequency_dropped_pair_count"
                    ),
                    "first_accepted_camera_timestamp_ns": interval.get(
                        "first_selected_input_timestamp_ns"
                    ),
                    "last_accepted_camera_timestamp_ns": interval.get(
                        "last_selected_input_timestamp_ns"
                    ),
                    "accepted_gaps_over_threshold_ns": [
                        [gap.get("start_timestamp_ns"), gap.get("end_timestamp_ns")]
                        for gap in gaps
                        if isinstance(gap, Mapping)
                    ],
                    "raw_dispatch_sequence_sha256": gate.get(
                        "raw_serial_dispatch_sequence_sha256"
                    ),
                    "accepted_callback_sequence_sha256": gate.get(
                        "accepted_visualizer_callback_sequence_sha256"
                    ),
                }
            )
    return validate_kaist_gate_preflight_record(cells)


def _subprocess_executor(command: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(command),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _atomic_write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(path), flags, 0o640)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


class EventLog:
    """Append state and receipt together via one atomic directory rename."""

    def __init__(self, artifact_root: Path) -> None:
        self.root = artifact_root / "control"
        self.events = self.root / "events"
        self._state: Dict[str, Any] = {"completed_actions": [], "last_event_index": 0}
        self._load()

    def _load(self) -> None:
        if not self.root.exists():
            return
        if not self.root.is_dir() or self.root.is_symlink():
            raise CampaignError("campaign control path is ambiguous")
        ambiguous = [path for path in self.root.iterdir() if path.name.startswith(".staging-")]
        if ambiguous:
            raise CampaignError("unfinished atomic campaign event exists: {}".format(ambiguous[0]))
        if not self.events.exists():
            other = [path for path in self.root.iterdir() if path.name != "events"]
            if other:
                raise CampaignError("campaign control directory has ambiguous leftovers")
            return
        if not self.events.is_dir() or self.events.is_symlink():
            raise CampaignError("campaign events path is ambiguous")
        directories = sorted(self.events.iterdir(), key=lambda item: item.name)
        expected = 1
        for directory in directories:
            if not directory.is_dir() or directory.is_symlink() or directory.name != "{:06d}".format(expected):
                raise CampaignError("campaign event sequence is ambiguous")
            receipt, _ = load_json(directory / "receipt.json", "campaign receipt")
            state, _ = load_json(directory / "state.json", "campaign state")
            if receipt.get("schema") != EVENT_SCHEMA or receipt.get("event_index") != expected:
                raise CampaignError("campaign receipt schema/index mismatch")
            if state.get("schema") != STATE_SCHEMA or state.get("last_event_index") != expected:
                raise CampaignError("campaign state schema/index mismatch")
            if state.get("last_action") != receipt.get("action"):
                raise CampaignError("campaign receipt/state action mismatch")
            self._state = {
                "completed_actions": list(state.get("completed_actions", [])),
                "last_event_index": expected,
            }
            expected += 1

    def append(self, action: str, receipt_fields: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.events.mkdir(parents=True, exist_ok=True)
        index = int(self._state["last_event_index"]) + 1
        final = self.events / "{:06d}".format(index)
        if final.exists():
            raise CampaignError("campaign event destination already exists")
        staging = self.root / (".staging-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o750)
        receipt = {
            "schema": EVENT_SCHEMA,
            "event_index": index,
            "recorded_utc": utc_now(),
            "action": action,
            **dict(receipt_fields),
        }
        completed = list(self._state["completed_actions"])
        completed.append(action)
        state = {
            "schema": STATE_SCHEMA,
            "last_event_index": index,
            "last_action": action,
            "completed_actions": completed,
        }
        try:
            _atomic_write_new(staging / "receipt.json", _json_bytes(receipt))
            _atomic_write_new(staging / "state.json", _json_bytes(state))
            descriptor = os.open(str(staging), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(str(staging), str(final))
            descriptor = os.open(str(self.events), os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        self._state = {"completed_actions": completed, "last_event_index": index}


class Campaign:
    def __init__(
        self,
        paths: RuntimePaths,
        options: CampaignOptions,
        rows: Sequence[MatrixRow],
        executor: Executor = _subprocess_executor,
        sleep_fn: SleepFunction = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_time_fn: Callable[[], float] = time.time,
        git_validator: Callable[[RuntimePaths, str, str], Mapping[str, Any]] = validate_git_pins,
    ) -> None:
        self.paths = paths
        self.options = options
        self.rows = tuple(rows)
        self.selected = select_rows(rows, options)
        self.executor = executor
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.wall_time_fn = wall_time_fn
        self.git_validator = git_validator
        self.last_estimator_end: Optional[float] = None
        self.last_estimator_end_wall: Optional[float] = None
        self.events: Optional[EventLog] = None

    def _pins(self) -> Tuple[str, str]:
        commit = self.options.expected_tooling_commit
        tree = self.options.expected_tooling_tree
        if commit is None or tree is None:
            raise CampaignError("run requires --expected-tooling-commit and --expected-tooling-tree")
        return commit, tree

    def _check_git(self) -> Mapping[str, Any]:
        commit, tree = self._pins()
        return self.git_validator(self.paths, commit, tree)

    def _cooldown(self) -> None:
        if self.last_estimator_end is None and self.last_estimator_end_wall is None:
            return
        remaining = self._cooldown_remaining()
        while remaining > 0:
            chunk = min(remaining, 60.0)
            self.sleep_fn(chunk)
            remaining = self._cooldown_remaining()

    def _cooldown_remaining(self) -> float:
        remaining = 0.0
        if self.last_estimator_end is not None:
            remaining = max(
                remaining,
                self.options.cooldown_seconds
                - (self.monotonic_fn() - self.last_estimator_end),
            )
        if self.last_estimator_end_wall is not None:
            remaining = max(
                remaining,
                self.options.cooldown_seconds
                - (self.wall_time_fn() - self.last_estimator_end_wall),
            )
        return remaining

    def _note_adopted_estimator_close(self, result: ValidatedResult) -> None:
        close = result.value.get("estimator_close_receipt")
        text = close.get("closed_utc") if isinstance(close, dict) else None
        if not isinstance(text, str) or not text:
            raise CampaignError("adopted retained result has no estimator close timestamp")
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            closed = dt.datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise CampaignError("adopted estimator close timestamp is malformed") from exc
        if closed.tzinfo is None:
            raise CampaignError("adopted estimator close timestamp lacks timezone")
        timestamp = closed.astimezone(dt.timezone.utc).timestamp()
        if timestamp > self.wall_time_fn() + 1.0:
            raise CampaignError("adopted estimator close timestamp is in the future")
        self.last_estimator_end_wall = max(
            timestamp,
            self.last_estimator_end_wall if self.last_estimator_end_wall is not None else timestamp,
        )

    def _append(self, action: str, fields: Mapping[str, Any]) -> None:
        if self.events is None:
            self.events = EventLog(self.options.artifact_root)
        self.events.append(action, fields)

    def _existing_result(
        self, row: MatrixRow, system: str, lane: str
    ) -> Optional[ValidatedResult]:
        location = run_location(self.options.artifact_root, lane, row, system)
        if location.result_path.exists():
            expected_runner = (
                self.paths.kaist_runner
                if row.dataset == "kaist_vio"
                else self.paths.generic_runner
            )
            validated = validate_sequence_result(
                location.result_path,
                row,
                system,
                lane,
                expected_runner=expected_runner,
            )
            if validated.classification == "fatal":
                raise CampaignError(
                    "fatal published result {} for {} {} {}".format(
                        validated.value.get("status"), row.dataset, row.sequence, system
                    )
                )
            return validated
        if location.run_directory.exists():
            raise CampaignError("ambiguous incomplete run directory: {}".format(location.run_directory))
        return None

    def validate_capture_prerequisites(self) -> Mapping[Tuple[int, str], ValidatedResult]:
        scored: Dict[Tuple[int, str], ValidatedResult] = {}
        # CDSC-1R4 freezes the complete 50-cell scored lane before *any* capture
        # replay, even when this invocation selects only one dataset/range.
        for row in self.rows:
            for system in row.system_order:
                result = self._existing_result(row, system, "scored")
                if result is None:
                    raise CampaignError(
                        "capture is gated until all 50 scored cells close; missing {} {}".format(
                            row_key(row), system
                        )
                    )
                scored[(row.order, system)] = result
        return scored

    def validate_only(self, enforce_static: bool = True) -> Mapping[str, Any]:
        static = validate_static_inputs(self.paths, self.selected, self.options.lane) if enforce_static else {}
        kaist_preflight = (
            preflight_all_kaist_gate_populations(self.paths, self.rows)
            if enforce_static
            else None
        )
        git = self._check_git()
        existing: Dict[str, Any] = {}
        if self.options.lane == "capture":
            self.validate_capture_prerequisites()
        for row in self.selected:
            for system in row.system_order:
                result = self._existing_result(row, system, self.options.lane)
                existing["{}:{}".format(row.order, system)] = (
                    None if result is None else result.value.get("status")
                )
        return {
            "status": "VALID",
            "selected_orders": [row.order for row in self.selected],
            "lane": self.options.lane,
            "static_input_count": len(static),
            "kaist_gate_preflight": kaist_preflight,
            "git": git,
            "existing": existing,
        }

    def dry_run(self, enforce_static: bool = True) -> Mapping[str, Any]:
        validation = self.validate_only(enforce_static=enforce_static)
        commands: List[Mapping[str, Any]] = []
        for row in self.selected:
            for system in row.system_order:
                result = self._existing_result(row, system, self.options.lane)
                commands.append(
                    {
                        "kind": "estimator",
                        "order": row.order,
                        "system": system,
                        "action": "adopt" if result is not None else "run",
                        "command": build_trial_command(self.paths, self.options, row, system),
                    }
                )
            if self.options.lane == "scored":
                commands.extend(self._dry_post_close_commands(row))
            else:
                for system in row.system_order:
                    commands.append(
                        {
                            "kind": "geometry",
                            "order": row.order,
                            "system": system,
                            "action": "post_capture",
                            "command": build_geometry_command(
                                self.paths, self.options.artifact_root, row, system
                            ),
                        }
                    )
        return {**validation, "status": "DRY_RUN", "commands": commands}

    def _dry_post_close_commands(self, row: MatrixRow) -> List[Mapping[str, Any]]:
        commands: List[Mapping[str, Any]] = []
        if row.ground_truth["capability"] == "full_trajectory":
            commands.append(
                {
                    "kind": "pair_evaluation",
                    "order": row.order,
                    "boundary": "only_after_both_scored_results_close_and_are_accuracy_eligible",
                    "command": build_pair_command(self.paths, self.options.artifact_root, row),
                }
            )
            if row.dataset == "kaist_vio" and row.sequence == "rotation/rotation.bag":
                commands.append(
                    {
                        "kind": "kaist_rotation_mechanism",
                        "order": row.order,
                        "boundary": "only_after_both_scored_results_close_and_ordinary_pair_evaluation",
                        "command": build_kaist_rotation_post_pair_command(
                            self.paths, self.options.artifact_root, row
                        ),
                    }
                )
        if row.dataset == "tum_vi":
            commands.append(
                {
                    "kind": "tum_reference_extraction",
                    "order": row.order,
                    "boundary": "only_after_both_scored_results_close",
                    "command": build_tum_extractor_command(
                        self.paths, self.options.artifact_root, row
                    ),
                }
            )
        return commands

    def run(self, enforce_static: bool = True) -> Mapping[str, Any]:
        kaist_preflight = None
        if enforce_static:
            validate_static_inputs(self.paths, self.selected, self.options.lane)
            kaist_preflight = preflight_all_kaist_gate_populations(
                self.paths, self.rows
            )
        git_identity_record = self._check_git()
        self.events = EventLog(self.options.artifact_root)
        self._append(
            "campaign-start-{}".format(self.options.lane),
            {
                "lane": self.options.lane,
                "selected_orders": [row.order for row in self.selected],
                "tooling_git": git_identity_record,
                "matrix": file_identity(self.paths.matrix),
                "protocol": file_identity(self.paths.protocol),
                "kaist_gate_preflight": kaist_preflight,
            },
        )
        scored_prerequisites = (
            self.validate_capture_prerequisites() if self.options.lane == "capture" else {}
        )
        completed: Dict[str, str] = {}
        for row in self.selected:
            pair_results: Dict[str, ValidatedResult] = {}
            for system in row.system_order:
                result = self._existing_result(row, system, self.options.lane)
                if result is None:
                    result = self._run_cell(row, system)
                else:
                    self._note_adopted_estimator_close(result)
                    self._append(
                        "adopt-{}-{}-{}".format(self.options.lane, row.order, system.lower()),
                        {
                            "status": result.value.get("status"),
                            "sequence_result": result.identity,
                        },
                    )
                pair_results[system] = result
                completed["{}:{}".format(row.order, system)] = str(result.value.get("status"))
            if self.options.lane == "scored":
                self._post_scored_pair(row, pair_results)
            else:
                for system in row.system_order:
                    self._post_capture_geometry(row, system, pair_results[system])
        self._append(
            "campaign-finish-{}".format(self.options.lane),
            {"lane": self.options.lane, "completed": completed},
        )
        return {
            "status": "COMPLETE",
            "lane": self.options.lane,
            "selected_orders": [row.order for row in self.selected],
            "results": completed,
            "capture_prerequisite_count": len(scored_prerequisites),
            "kaist_gate_preflight": kaist_preflight,
        }

    def _run_cell(self, row: MatrixRow, system: str) -> ValidatedResult:
        location = run_location(self.options.artifact_root, self.options.lane, row, system)
        if location.run_directory.exists():
            raise CampaignError("refusing overwrite of run directory: {}".format(location.run_directory))
        self._cooldown()
        # This is intentionally immediately before subprocess creation.  The
        # runner checks byte identities; the campaign driver additionally pins
        # the clean tooling worktree that the S1 runner itself only reports.
        git_before = self._check_git()
        command = build_trial_command(self.paths, self.options, row, system)
        try:
            process = self.executor(command)
        except OSError as exc:
            self._append(
                "launch-failed-{}-{}-{}".format(self.options.lane, row.order, system.lower()),
                {"command": command, "error": str(exc)},
            )
            raise CampaignError("failed to launch estimator runner") from exc
        finally:
            self.last_estimator_end = self.monotonic_fn()
            self.last_estimator_end_wall = self.wall_time_fn()
        receipt: Dict[str, Any] = {
            "command": command,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "tooling_git_before_launch": git_before,
        }
        try:
            git_after = self._check_git()
        except CampaignError as exc:
            receipt["tooling_git_postflight_error"] = str(exc)
            self._append(
                "tooling-drift-{}-{}-{}".format(
                    self.options.lane, row.order, system.lower()
                ),
                receipt,
            )
            raise
        receipt["tooling_git_after_runner_close"] = git_after
        if not location.result_path.is_file():
            self._append(
                "missing-result-{}-{}-{}".format(self.options.lane, row.order, system.lower()),
                receipt,
            )
            raise CampaignError(
                "runner did not publish canonical sequence_result (exit {})".format(
                    process.returncode
                )
            )
        expected_runner = (
            self.paths.kaist_runner
            if row.dataset == "kaist_vio"
            else self.paths.generic_runner
        )
        result = validate_sequence_result(
            location.result_path,
            row,
            system,
            self.options.lane,
            expected_runner=expected_runner,
        )
        receipt.update(
            {
                "status": result.value.get("status"),
                "classification": result.classification,
                "sequence_result": result.identity,
            }
        )
        self._append(
            "run-{}-{}-{}".format(self.options.lane, row.order, system.lower()), receipt
        )
        expected_exit = 0 if result.value.get("status") in ELIGIBLE_STATUSES else 2
        if process.returncode != expected_exit:
            raise CampaignError(
                "runner exit/status contract is ambiguous: exit {} status {}".format(
                    process.returncode, result.value.get("status")
                )
            )
        if result.classification == "fatal":
            raise CampaignError(
                "fatal runner status: {}".format(result.value.get("status"))
            )
        return result

    def _post_scored_pair(
        self, row: MatrixRow, results: Mapping[str, ValidatedResult]
    ) -> None:
        # Both values reach this method only after strict close validation.
        if set(results) != set(SYSTEMS):
            raise CampaignError("paired post-processing called before both scored cells closed")
        if row.ground_truth["capability"] == "full_trajectory":
            if all(bool(results[system].value.get("accuracy_eligible")) for system in SYSTEMS):
                self._run_or_adopt_pair_evaluation(row)
            else:
                self._append(
                    "skip-pair-evaluation-{}".format(row.order),
                    {
                        "reason": "one_or_both_scored_runs_not_accuracy_eligible",
                        "statuses": {
                            system: results[system].value.get("status") for system in SYSTEMS
                        },
                        "ground_truth_opened": False,
                    },
                )
            # This is a separate S1 C2 mechanism check, not a common-accuracy
            # metric.  A validly closed incomplete U0 rotation arm therefore
            # does not suppress it.
            if row.dataset == "kaist_vio" and row.sequence == "rotation/rotation.bag":
                self._run_or_adopt_kaist_rotation_mechanism(row, results)
        if row.dataset == "tum_vi":
            self._run_or_adopt_tum_extraction(row)

    def _validate_kaist_rotation_mechanism(
        self,
        path: Path,
        row: MatrixRow,
        results: Mapping[str, ValidatedResult],
    ) -> Mapping[str, Any]:
        value, _ = load_json(path, "KAIST rotation mechanism result")
        if (
            value.get("schema")
            != "schurvio.icra27.cross_dataset.kaist_rotation_post_pair.v1"
            or value.get("dataset") != "kaist_vio"
            or value.get("sequence") != "rotation/rotation.bag"
            or value.get("protocol_id") != PROTOCOL_ID
            or value.get("status") not in ("PASS", "C2_VALIDATION_FAILURE")
            or value.get("pass") is not (value.get("status") == "PASS")
            or value.get("both_scored_estimator_groups_closed_before_ground_truth_open") is not True
            or value.get("accuracy_eligibility_unchanged_by_c2_target_thresholds") is not True
            or value.get("c2_validation_is_a_separate_robustness_mechanism_result") is not True
        ):
            raise CampaignError("KAIST rotation mechanism result contract mismatch")
        source_runs = value.get("source_runs")
        if not isinstance(source_runs, dict):
            raise CampaignError("KAIST rotation mechanism source runs are absent")
        for system in SYSTEMS:
            source = source_runs.get(system)
            recorded = source.get("result") if isinstance(source, dict) else None
            if not _same_identity(
                recorded,
                file_identity(results[system].path),
                "{} mechanism source result".format(system),
            ):
                raise CampaignError("KAIST rotation mechanism source result drift")
        binding = value.get("matrix_binding")
        matrix_record = binding.get("matrix") if isinstance(binding, dict) else None
        if not _same_identity(
            matrix_record, file_identity(self.paths.matrix), "mechanism matrix"
        ):
            raise CampaignError("KAIST rotation mechanism matrix drift")
        ground_truth_opened = value.get("ground_truth_opened")
        if not isinstance(ground_truth_opened, bool):
            raise CampaignError("KAIST rotation mechanism lacks a GT-open state")
        if binding.get("ground_truth_opened") is not ground_truth_opened:
            raise CampaignError("KAIST rotation mechanism GT-open bindings differ")
        declaration = binding.get("ground_truth_expected_from_matrix")
        expected_declaration = {
            "canonical_path": row.ground_truth.get("canonical_path"),
            "size_bytes": row.ground_truth.get("bytes"),
            "sha256": row.ground_truth.get("sha256"),
            "capability": row.ground_truth.get("capability"),
            "format": row.ground_truth.get("format"),
            "source": "FROZEN_MATRIX_DECLARATION_NOT_LIVE_FILE",
            "live_file_opened": False,
        }
        if declaration != expected_declaration:
            raise CampaignError("KAIST rotation matrix-declared GT identity differs")
        if not ground_truth_opened:
            # Do not resolve, stat, or hash the declared GT path in this branch.
            # It is intentionally only a byte declaration copied from the
            # already validated matrix after a retained S1 prerequisite failure.
            if value.get("status") != "C2_VALIDATION_FAILURE":
                raise CampaignError("KAIST rotation PASS cannot omit GT evaluation")
            if binding.get("ground_truth") is not None:
                raise CampaignError("unopened KAIST GT unexpectedly has a live identity")
            prerequisite = value.get("c2_prerequisite")
            failure = value.get("c2_validation_failure")
            if (
                not isinstance(prerequisite, dict)
                or prerequisite.get("pass") is not False
                or not isinstance(prerequisite.get("reason_code"), str)
                or not prerequisite.get("reason_code")
                or not isinstance(failure, dict)
                or failure.get("algorithm_failure_retained") is not True
                or failure.get("infrastructure_failure") is not False
            ):
                raise CampaignError("early KAIST C2 failure evidence is incomplete")
        else:
            gt_record = binding.get("ground_truth")
            if not _same_identity(
                gt_record,
                file_identity(Path(str(row.ground_truth["canonical_path"]))),
                "mechanism ground truth",
            ):
                raise CampaignError("KAIST rotation mechanism ground-truth drift")
        return value

    def _run_or_adopt_kaist_rotation_mechanism(
        self, row: MatrixRow, results: Mapping[str, ValidatedResult]
    ) -> None:
        output = (
            self.options.artifact_root
            / "mechanism"
            / row_key(row)
            / "kaist_rotation_post_pair.json"
        )
        output_directory = output.parent
        if output.exists():
            value = self._validate_kaist_rotation_mechanism(output, row, results)
            self._append(
                "adopt-kaist-rotation-mechanism-{}".format(row.order),
                {"status": value.get("status"), "result": file_identity(output)},
            )
            return
        if output_directory.exists():
            raise CampaignError(
                "ambiguous KAIST rotation mechanism directory: {}".format(output_directory)
            )
        output_directory.mkdir(parents=True)
        command = build_kaist_rotation_post_pair_command(
            self.paths, self.options.artifact_root, row
        )
        try:
            process = self.executor(command)
        except OSError as exc:
            self._append(
                "kaist-rotation-mechanism-launch-failed-{}".format(row.order),
                {"command": command, "error": str(exc)},
            )
            raise CampaignError("KAIST rotation mechanism evaluator could not launch") from exc
        receipt = {
            "command": command,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        if not output.is_file():
            self._append(
                "kaist-rotation-mechanism-failed-{}".format(row.order), receipt
            )
            raise CampaignError("KAIST rotation mechanism evaluator published no result")
        value = self._validate_kaist_rotation_mechanism(output, row, results)
        expected_exit = 0 if value.get("status") == "PASS" else 2
        receipt.update({"status": value.get("status"), "result": file_identity(output)})
        self._append("kaist-rotation-mechanism-{}".format(row.order), receipt)
        if process.returncode != expected_exit:
            raise CampaignError("KAIST rotation mechanism exit/status contract is ambiguous")

    def _run_or_adopt_pair_evaluation(self, row: MatrixRow) -> None:
        output = self.options.artifact_root / "pair" / row_key(row)
        manifest = output / "pair_result.json"
        if manifest.exists():
            value = _validate_simple_published_result(
                output,
                "pair_result.json",
                PAIR_SCHEMA,
                row,
                "pair result",
                ("COMPLETE", "UNASSESSABLE"),
            )
            self._append(
                "adopt-pair-evaluation-{}".format(row.order),
                {"status": value.get("status"), "pair_result": file_identity(manifest)},
            )
            return
        if output.exists():
            raise CampaignError("ambiguous pair-evaluation output directory: {}".format(output))
        command = build_pair_command(self.paths, self.options.artifact_root, row)
        process = self.executor(command)
        receipt = {
            "command": command,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        if process.returncode != 0 or not manifest.is_file():
            self._append("pair-evaluation-failed-{}".format(row.order), receipt)
            raise CampaignError("paired evaluator failed for {}".format(row_key(row)))
        value = _validate_simple_published_result(
            output,
            "pair_result.json",
            PAIR_SCHEMA,
            row,
            "pair result",
            ("COMPLETE", "UNASSESSABLE"),
        )
        receipt.update({"status": value.get("status"), "pair_result": file_identity(manifest)})
        self._append("pair-evaluation-{}".format(row.order), receipt)

    def _run_or_adopt_tum_extraction(self, row: MatrixRow) -> None:
        output = self.options.artifact_root / "reference" / row_key(row)
        manifest = output / "interval_manifest.json"
        if manifest.exists():
            value = _validate_simple_published_result(
                output, "interval_manifest.json", TUM_REFERENCE_SCHEMA, row, "TUM reference"
            )
            self._append(
                "adopt-tum-reference-{}".format(row.order),
                {"status": value.get("status"), "manifest": file_identity(manifest)},
            )
            return
        if output.exists():
            raise CampaignError("ambiguous TUM reference output directory: {}".format(output))
        command = build_tum_extractor_command(self.paths, self.options.artifact_root, row)
        process = self.executor(command)
        receipt = {
            "command": command,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        if process.returncode != 0 or not manifest.is_file():
            self._append("tum-reference-failed-{}".format(row.order), receipt)
            raise CampaignError("TUM reference extraction failed for {}".format(row_key(row)))
        value = _validate_simple_published_result(
            output, "interval_manifest.json", TUM_REFERENCE_SCHEMA, row, "TUM reference"
        )
        receipt.update({"status": value.get("status"), "manifest": file_identity(manifest)})
        self._append("tum-reference-{}".format(row.order), receipt)

    def _post_capture_geometry(
        self, row: MatrixRow, system: str, result: ValidatedResult
    ) -> None:
        output = self.options.artifact_root / "geometry" / row_key(row) / system
        # The bundler publishes qualitative_manifest.json for both successful maps
        # and explicit failure tiles.  Adoption is still checksum-bound.
        manifest = output / "qualitative_manifest.json"
        if manifest.exists():
            value, _ = load_json(manifest, "geometry bundle manifest")
            _validate_checksum_set(output, manifest)
            if not self._valid_geometry_manifest_binding(value, row, system):
                raise CampaignError("geometry bundle binding mismatch")
            self._append(
                "adopt-geometry-{}-{}".format(row.order, system.lower()),
                {"status": value.get("status"), "manifest": file_identity(manifest)},
            )
            return
        if output.exists():
            raise CampaignError("ambiguous geometry output directory: {}".format(output))
        command = build_geometry_command(self.paths, self.options.artifact_root, row, system)
        process = self.executor(command)
        receipt = {
            "command": command,
            "returncode": process.returncode,
            "stdout": process.stdout,
            "stderr": process.stderr,
            "capture_result": result.identity,
        }
        if process.returncode != 0 or not manifest.is_file():
            self._append(
                "geometry-failed-{}-{}".format(row.order, system.lower()), receipt
            )
            raise CampaignError("geometry bundling failed for {} {}".format(row_key(row), system))
        value, _ = load_json(manifest, "geometry bundle manifest")
        _validate_checksum_set(output, manifest)
        if not self._valid_geometry_manifest_binding(value, row, system):
            raise CampaignError("geometry bundle binding mismatch")
        receipt.update({"status": value.get("status"), "manifest": file_identity(manifest)})
        self._append("geometry-{}-{}".format(row.order, system.lower()), receipt)

    @staticmethod
    def _valid_geometry_manifest_binding(
        value: Mapping[str, Any], row: MatrixRow, system: str
    ) -> bool:
        status = value.get("status")
        bundle_mode = value.get("bundle_mode")
        if status == "FAILURE_EVIDENCE_COMPLETE" and bundle_mode != "FAILURE_TILE_ONLY":
            return False
        if status == "GEOMETRY_EVIDENCE_COMPLETE":
            expected_mode = (
                "REFERENCE_BACKED_VALIDATED_V3"
                if row.ground_truth["capability"] == "full_trajectory"
                else "NATIVE_ESTIMATOR_FRAME_PARTIAL_REFERENCE"
            )
            if bundle_mode != expected_mode:
                return False
        return bool(
            value.get("schema")
            == "schurvio.icra27.cross_dataset_geometry_bundle.v1"
            and status in ("GEOMETRY_EVIDENCE_COMPLETE", "FAILURE_EVIDENCE_COMPLETE")
            and value.get("dataset") == row.dataset
            and value.get("sequence") == row.sequence
            and value.get("system") == system
        )


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("action", choices=("run", "dry-run", "validate"))
    value.add_argument("--artifact-root", required=True, type=Path)
    value.add_argument("--lane", required=True, choices=LANES)
    value.add_argument("--dataset", default="all", choices=(*DATASETS, "all"))
    value.add_argument("--start-order", type=int)
    value.add_argument("--end-order", type=int)
    value.add_argument("--timeout-seconds", type=float, default=21600.0)
    value.add_argument("--cpu-list", default="8-15")
    value.add_argument("--cooldown-seconds", type=float, default=5.0)
    value.add_argument("--base-ros-port", type=int, default=18100)
    value.add_argument("--expected-tooling-commit", required=True)
    value.add_argument("--expected-tooling-tree", required=True)
    return value


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    options = CampaignOptions(
        artifact_root=args.artifact_root.expanduser().resolve(strict=False),
        lane=args.lane,
        dataset=args.dataset,
        start_order=args.start_order,
        end_order=args.end_order,
        timeout_seconds=args.timeout_seconds,
        cpu_list=args.cpu_list,
        cooldown_seconds=args.cooldown_seconds,
        base_ros_port=args.base_ros_port,
        expected_tooling_commit=args.expected_tooling_commit,
        expected_tooling_tree=args.expected_tooling_tree,
    )
    try:
        rows = load_matrix(MATRIX_FILE, require_canonical=True)
        campaign = Campaign(RuntimePaths(), options, rows)
        if args.action == "run":
            result = campaign.run()
        elif args.action == "dry-run":
            result = campaign.dry_run()
        else:
            result = campaign.validate_only()
    except (CampaignError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("CROSS_DATASET_CAMPAIGN_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, allow_nan=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
