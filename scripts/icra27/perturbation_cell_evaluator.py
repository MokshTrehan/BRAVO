#!/usr/bin/python3.8
"""Per-cell single-system accuracy for the PERTURB-1 perturbation campaign.

PERTURB-1 (docs/icra27/PERTURBATION_PREREG.md) treats every cell as an
independent sample, so accuracy is evaluated per cell against the full
ground truth rather than on a U0/S1 intersected common population.  The math
is the frozen CDSC-1R4 core reused unchanged (kaist_pair_evaluator via
cross_dataset_pair_evaluator): bounded quaternion projection for evo objects
only, evo v1.31.1 unique nearest association within 0.01 s, SE(3) Umeyama
alignment without scale, translation ATE RMSE, and 1 m all-pairs RPE tuples
selected once from the associated reference.

Ground truth is opened only here, after the estimator process group closed
(the caller passes a closed, checksum-published run directory).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Dict, Mapping, Optional
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import cross_dataset_pair_evaluator as pair_evaluator  # noqa: E402

CORE = pair_evaluator.CORE
SCHEMA = "schurvio.icra27.perturbation.cell_metrics.v1"
MINIMUM_POSES = pair_evaluator.MINIMUM_COMMON_POSES
MINIMUM_RPE_PAIRS = pair_evaluator.MINIMUM_RPE_PAIRS
ELIGIBLE_STATUSES = pair_evaluator.ELIGIBLE_STATUSES


class CellEvaluationError(RuntimeError):
    """A fail-closed per-cell accuracy contract violation."""


def _identity(path: Path) -> Dict[str, Any]:
    return pair_evaluator.identity(path)


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(str(path), flags, 0o640)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _write_sha256sums(directory: Path) -> None:
    lines = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise CellEvaluationError("refusing to checksum a symlink: {}".format(path))
        if path.is_file() and path.name != "SHA256SUMS":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append("{}  {}".format(digest, path.relative_to(directory).as_posix()))
    (directory / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")


def evaluate_cell(run_directory: Path, ground_truth: Path, output: Path) -> Dict[str, Any]:
    run_directory = run_directory.resolve(strict=True)
    result_path = run_directory / "sequence_result.json"
    manifest = pair_evaluator.load_json(result_path, "sequence result")
    manifest_identity = _identity(result_path)
    close = manifest.get("estimator_close_receipt") or {}
    if close.get("estimator_process_group_closed") is not True or close.get(
        "runtime_services_closed"
    ) is not True:
        raise CellEvaluationError("estimator process group is not proven closed; ground truth stays sealed")
    gt_identity = _identity(ground_truth)
    if output.exists():
        raise CellEvaluationError("refusing to overwrite cell metrics: {}".format(output))
    staging = output.parent / (".staging-" + uuid.uuid4().hex)
    staging.mkdir(parents=True, mode=0o750)
    committed = False
    try:
        base: Dict[str, Any] = {
            "schema": SCHEMA,
            "campaign_id": "PERTURB-1",
            "protocol_id": manifest.get("protocol_id"),
            "run_id": manifest.get("run_id"),
            "dataset": manifest.get("dataset"),
            "sequence": manifest.get("sequence"),
            "system": manifest.get("system"),
            "perturbation": manifest.get("perturbation"),
            "status": manifest.get("status"),
            "evidence_validity": manifest.get("evidence_validity"),
            "passage_complete": bool((manifest.get("passage") or {}).get("complete")),
            "accuracy_eligible": bool(manifest.get("accuracy_eligible")),
            "sequence_result_identity": manifest_identity,
            "ground_truth_identity": gt_identity,
            "ground_truth_opened_after_estimator_close": True,
            "evaluation": {
                "population": "per_cell_single_system_association_to_full_reference",
                "association_max_seconds": CORE.ASSOCIATION_MAX_SECONDS,
                "alignment": "evo SE(3) Umeyama, correct_scale=False, all poses",
                "rpe": {
                    "delta_m": CORE.RPE_DELTA_METERS,
                    "relative_tolerance": CORE.RPE_RELATIVE_DELTA_TOLERANCE,
                    "all_pairs": CORE.RPE_ALL_PAIRS,
                    "pairs_from_reference": CORE.RPE_PAIRS_FROM_REFERENCE,
                },
                "minimum_poses": MINIMUM_POSES,
                "minimum_rpe_pairs": MINIMUM_RPE_PAIRS,
                "math_core": _identity(pair_evaluator.CORE_PATH),
                "pair_evaluator_module": _identity(Path(pair_evaluator.__file__)),
                "cell_evaluator_module": _identity(Path(__file__)),
            },
            "metrics": None,
            "metrics_status": None,
        }
        runtime, runtime_digest = pair_evaluator._runtime_provenance()
        base["evaluation"]["runtime_pins_sha256"] = runtime_digest
        base["evaluation"]["evo_version"] = runtime.get("evo", {}).get("version")
        if not base["accuracy_eligible"] or manifest.get("status") not in ELIGIBLE_STATUSES:
            base["metrics_status"] = "NOT_ACCURACY_ELIGIBLE"
            base["metrics_reason"] = "cell status {} / passage complete {}".format(
                manifest.get("status"), base["passage_complete"]
            )
            _write_json(staging / "cell_metrics.json", base)
            _write_sha256sums(staging)
            os.rename(str(staging), str(output))
            committed = True
            return base
        artifacts = manifest.get("artifacts") or {}
        tum_record = artifacts.get("tum") or artifacts.get("trajectory_tum") or {}
        tum_path = run_directory / "trajectory" / "estimate_raw.tum"
        recorded = tum_record.get("identity") if isinstance(tum_record, dict) else None
        live = _identity(tum_path)
        if isinstance(recorded, dict) and recorded.get("sha256") not in (None, live["sha256"]):
            raise CellEvaluationError("estimate TUM bytes differ from the published manifest identity")
        base["estimate_identity"] = live
        gt_rows, gt_projection = pair_evaluator._read_tum_with_bounded_quaternion_projection(
            ground_truth, "ground truth"
        )
        est_rows, est_projection = pair_evaluator._read_tum_with_bounded_quaternion_projection(
            tum_path, "estimate"
        )
        base["quaternion_projection"] = {"ground_truth": gt_projection, "estimate": est_projection}
        associations = pair_evaluator._associate_or_empty(gt_rows, est_rows)
        gt_selected = [gt_rows[item.gt_index] for item in associations]
        est_selected = [est_rows[item.estimate_index] for item in associations]
        population = {
            "associated_pose_count": len(associations),
            "reference_row_count": len(gt_rows),
            "estimate_row_count": len(est_rows),
            "reference_fraction_retained": (len(associations) / len(gt_rows)) if gt_rows else 0.0,
            "first_timestamp": gt_selected[0].timestamp if gt_selected else None,
            "last_timestamp": gt_selected[-1].timestamp if gt_selected else None,
        }
        base["population"] = population
        if len(associations) < MINIMUM_POSES:
            base["metrics_status"] = "INSUFFICIENT_POSES"
            _write_json(staging / "cell_metrics.json", base)
            _write_sha256sums(staging)
            os.rename(str(staging), str(output))
            committed = True
            return base
        common_dir = staging / "population"
        common_dir.mkdir()
        ref_path = common_dir / "reference_associated.tum"
        est_path = common_dir / "estimate_associated.tum"
        CORE.write_tum(ref_path, gt_selected, gt_selected)
        CORE.write_tum(est_path, est_selected, gt_selected)
        reference = pair_evaluator._project_loaded_evo_trajectory(
            CORE.file_interface.read_tum_trajectory_file(str(ref_path)), "reference"
        )
        estimate = pair_evaluator._project_loaded_evo_trajectory(
            CORE.file_interface.read_tum_trajectory_file(str(est_path)), "estimate"
        )
        if not CORE.np.array_equal(estimate.timestamps, reference.timestamps):
            raise CellEvaluationError("normalized estimate timestamps differ from reference")
        reference_pairs = CORE.metrics.id_pairs_from_delta(
            reference.poses_se3,
            CORE.RPE_DELTA_METERS,
            CORE.metrics.Unit.meters,
            CORE.RPE_RELATIVE_DELTA_TOLERANCE,
            all_pairs=CORE.RPE_ALL_PAIRS,
        )
        reference_pairs = [(int(start), int(end)) for start, end in reference_pairs]
        base["rpe_reference_pairs"] = {
            "count": len(reference_pairs),
            "minimum_required": MINIMUM_RPE_PAIRS,
            "sha256": CORE.canonical_digest(reference_pairs),
        }
        if len(reference_pairs) < MINIMUM_RPE_PAIRS:
            base["metrics_status"] = "INSUFFICIENT_RPE_PAIRS"
            _write_json(staging / "cell_metrics.json", base)
            _write_sha256sums(staging)
            os.rename(str(staging), str(output))
            committed = True
            return base
        method = CORE.evaluate_method(str(manifest.get("system")), reference, estimate, reference_pairs)
        base["metrics"] = {
            "ate_translation_rmse_m": method["primary"]["ate_translation_rmse_m"],
            "rpe_translation_rmse_1m_m": method["primary"]["rpe_translation_rmse_1m_m"],
            "rpe_rotation_rmse_1m_deg": method["primary"]["rpe_rotation_rmse_1m_deg"],
        }
        base["statistics"] = method["statistics"]
        base["alignment"] = method["alignment"]
        base["metrics_status"] = "COMPUTED"
        _write_json(staging / "cell_metrics.json", base)
        _write_sha256sums(staging)
        os.rename(str(staging), str(output))
        committed = True
        return base
    finally:
        if not committed and staging.exists():
            shutil.rmtree(staging)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--run", required=True, type=Path)
    value.add_argument("--ground-truth", required=True, type=Path)
    value.add_argument("--output", required=True, type=Path)
    return value


def main(argv: Optional[Any] = None) -> int:
    args = parser().parse_args(argv)
    try:
        result = evaluate_cell(args.run, args.ground_truth, args.output)
    except (OSError, ValueError, CellEvaluationError, pair_evaluator.EvaluationError,
            CORE.PairEvaluationError) as exc:
        print("PERTURBATION_CELL_EVALUATION_ERROR: {}".format(exc), file=sys.stderr)
        return 2
    print("{}: {}".format(result["metrics_status"], args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
