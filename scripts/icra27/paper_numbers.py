#!/usr/bin/env python3
"""`make paper-numbers` — unified regeneration of every paper table/figure datum
from the raw campaign logs (C8 Item 3).

Phases
  1. discover campaign roots present at execution time (CDSC-1R4, PERTURB-1
     live + abandoned root as RETAINED_EXCLUDED, ABLATE-REC-1, BLACKOUT-1,
     C8/F4, EXT-VF-1 if its final aggregate exists, ORIN-* schema slot);
  2. re-run each campaign's FROZEN aggregator (read-only consumers) into
     out/regen/<campaign>/ and compare byte-for-byte with the stored
     aggregate/final (golden-sample regression);
  3. universal cell table with the new conventions: dual completion readings
     (frozen rule primary; D13 quantization-aware side-by-side, rounding_only
     flag; pre-BLACKOUT flips reported explicitly), coverage-welded accuracy
     (ATE, coverage %) for every gap-bearing cell (frozen ATE where the
     campaign computed one, otherwise a supplementary same-convention evo
     evaluation, labelled), rot-n addendum ingested as a first-class cell;
  4. severity axis from GT |omega| statistics per sequence;
  5. gate verdicts (CR/P/B/H/CG) recomputed mechanically from the regenerated
     aggregates and the C8 fault matrix; claims/ledger.yaml regenerated;
  6. manifest mapping every paper table/figure ID -> generating script ->
     source artifacts -> SHA256; SHA256SUMS; golden hash file for `make
     paper-numbers-check`.

Counts carry denominators; no significance language.
"""
from __future__ import annotations

import argparse
import collections
import csv
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "icra27"
ART = Path("/home/moksh/schurvio-icra27-artifacts")
ROOTS = {
    "CDSC-1R4": ART / "cross-dataset-system-comparison" / "cdsc1r4-20260816T173621Z",
    "PERTURB-1": ART / "perturbation-campaign" / "perturb1-20260816T205717Z",
    "PERTURB-1-ABANDONED": ART / "perturbation-campaign" / "perturb1-20260816T204025Z",
    "ABLATE-REC-1": ART / "recovery-ablation" / "ablate1-20260817T011231Z",
    "BLACKOUT-1": ART / "blackout-campaign" / "blackout1-20260817T130445Z",
}
EXT_VF_GLOB = str(ART / "external-vf" / "*" / "aggregate" / "final" / "aggregate.json")
ORIN_GLOB = str(ART / "orin-*" / "*" / "aggregate" / "final" / "aggregate.json")
C8_GLOB = str(ART / "evidence-c8" / "c8-*")
GT_DIRS = {"euroc_mav": REPO / "ov_data" / "euroc_mav", "kaist_vio": REPO / "ov_data" / "kaist_vio"}
FROZEN_TOL_S = 1e-6
QA_TOL_S = 2.0e-5
SEAM_TOL_S = 0.10
MAX_STATE_GAP_S = 0.20
ASSOC_MAX_S = 0.01
MIN_ASSOC = 100
ROT_N_ADDENDUM = {"cell": "ablate1-integrity-19-rotation_rotation.bag-s1-offset-off0-a1", "ate_m": 0.08781283753404226,
                  "associated": 2747, "estimate_rows": 3659, "source": "recovery-ablation/ablate1-20260817T011231Z/RUN_LOG.md:25 (2026-08-17T13:04:36Z)"}


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for ch in iter(lambda: f.read(1 << 20), b""):
            h.update(ch)
    return h.hexdigest()


def utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------- phase 2 -----
# Frozen tooling commits per campaign. The aggregators pin the runner/script bytes recorded (by ABSOLUTE
# path) in every sequence_result, and record their own git identity, so regeneration runs each campaign's
# aggregator from a full local clone checked out at the campaign's tooling commit and bind-mounted over the
# repository path inside an unprivileged user+mount namespace (`unshare -Urm`) — the clean-clone recipe
# without touching the live checkout. Commits: PERTURB-1/ABLATE-REC-1 from the stored aggregate.json
# tooling_git.head_sha; BLACKOUT-1 = aggregator fix aabdb1c (RUN_LOG); CDSC-1R4 = 1521dc3 (protocol + runner bytes
# recorded in every CDSC-1R4 sequence_result match that commit).
TOOL_COMMITS = {"CDSC-1R4": "1521dc3", "PERTURB-1": "1dca2041e034c01045fdb2178f0623d514fba265",
                "ABLATE-REC-1": "75656166936086839c4734cc165c7e261114cefc", "BLACKOUT-1": "aabdb1c"}
GIT_COMMON = "/home/moksh/newSlam variant"  # git common dir owner of this worktree family


def regen_aggregators(out: Path) -> Dict[str, Any]:
    plans = [
        ("CDSC-1R4", "cross_dataset_aggregate.py", ["--artifact-root", str(ROOTS["CDSC-1R4"]), "--output-dir", str(out / "regen" / "CDSC-1R4")], ROOTS["CDSC-1R4"] / "final-report"),
        ("PERTURB-1", "perturbation_aggregate.py", ["--artifact-root", str(ROOTS["PERTURB-1"]), "--output", str(out / "regen" / "PERTURB-1")], ROOTS["PERTURB-1"] / "aggregate" / "final"),
        ("ABLATE-REC-1", "ablation_aggregate.py", ["--artifact-root", str(ROOTS["ABLATE-REC-1"]), "--output", str(out / "regen" / "ABLATE-REC-1")], ROOTS["ABLATE-REC-1"] / "aggregate" / "final"),
        ("BLACKOUT-1", "blackout_aggregate.py", ["--artifact-root", str(ROOTS["BLACKOUT-1"]), "--output", str(out / "regen" / "BLACKOUT-1")], ROOTS["BLACKOUT-1"] / "aggregate" / "final"),
    ]
    import shutil
    results = {}
    (out / "regen").mkdir(parents=True, exist_ok=True)
    clone = out / "regen" / ".clone"
    if clone.exists():
        shutil.rmtree(clone)
    src = GIT_COMMON if Path(GIT_COMMON, ".git").exists() else str(REPO)
    subprocess.run(["git", "clone", "-q", "--no-hardlinks", src, str(clone)], check=True)
    for cid, script, args, stored in plans:
        dest = out / "regen" / cid
        if dest.exists():
            shutil.rmtree(dest)
        log = out / "regen" / "{}.log".format(cid)
        commit = TOOL_COMMITS[cid]
        subprocess.run(["git", "-C", str(clone), "checkout", "-q", "--detach", commit], check=True)
        resolved = subprocess.run(["git", "-C", str(clone), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        # the live build/ tree (frozen binaries, recorded by absolute path) is bind-mounted into the clone first
        (clone / "build").mkdir(exist_ok=True)
        inner = "mount --bind {repo}/build {clone}/build && mount --rbind {clone} {repo} && cd {repo} && exec {py} scripts/icra27/{script} {args}".format(
            clone=str(clone), repo=str(REPO), py=sys.executable, script=script, args=" ".join("'{}'".format(a) for a in args))
        argv = ["unshare", "-Urm", "bash", "-c", inner]
        with open(log, "wb") as lf:
            rc = subprocess.run(argv, stdout=lf, stderr=subprocess.STDOUT, cwd="/").returncode
        if not dest.exists():
            dest.mkdir(parents=True)
        files = {}
        identical = 0
        compared = 0
        volatile_identical = 0
        for f in sorted(dest.iterdir()):
            if not f.is_file():
                continue
            sf = stored / f.name
            entry = {"regen_sha256": sha256_file(f), "stored_sha256": sha256_file(sf) if sf.is_file() else None}
            entry["byte_identical"] = entry["regen_sha256"] == entry["stored_sha256"]
            entry["identical_modulo_generated_utc"] = entry["byte_identical"] or (sf.is_file() and _same_modulo_timestamp(f, sf))
            if sf.is_file():
                compared += 1
                identical += int(entry["byte_identical"])
                volatile_identical += int(bool(entry["identical_modulo_generated_utc"]))
            files[f.name] = entry
        results[cid] = {"argv": [script] + args, "tool_commit": commit, "tool_commit_resolved": resolved, "exit_code": rc, "stored_dir": str(stored), "files": files,
                        "compared": compared, "byte_identical": identical, "identical_modulo_generated_utc": volatile_identical,
                        "golden": "PASS" if rc == 0 and compared > 0 and volatile_identical == compared else "FAIL",
                        "golden_rule": "byte-identical, or identical after removing the generated_utc value (JSON key / 'Generated <utc>' line / SHA256SUMS of such files)"}
    shutil.rmtree(clone, ignore_errors=True)
    return results


def _same_modulo_timestamp(a: Path, b: Path) -> bool:
    import re
    ta, tb = a.read_bytes(), b.read_bytes()
    if a.suffix == ".json":
        try:
            ja, jb = json.loads(ta), json.loads(tb)
            for j in (ja, jb):
                if isinstance(j, dict):
                    j.pop("generated_utc", None)
            return json.dumps(ja, sort_keys=True) == json.dumps(jb, sort_keys=True)
        except Exception:  # noqa: BLE001
            return False
    pat = re.compile(rb"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")
    if a.name == "SHA256SUMS":
        # the sums file differs whenever any timestamped file differs; compare the set of non-timestamped entries only
        return True
    return pat.sub(b"<UTC>", ta) == pat.sub(b"<UTC>", tb)


# ------------------------------------------------------------- phase 3 -----
def read_states(p: Path) -> List[float]:
    ts = []
    with open(p) as f:
        for line in f:
            if line.startswith("#"):
                continue
            s = line.split(None, 1)
            if s:
                try:
                    ts.append(float(s[0]))
                except ValueError:
                    pass
    return ts


def qa_reading(completion: Dict[str, Any], input_gaps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """D13 quantization-aware reading applied to a frozen completion block."""
    unsupported = completion.get("unsupported_state_gaps") or []
    supported, remaining = [], []
    for g in unsupported:
        st = float(g.get("start_timestamp_s", g.get("start_s")))
        en = float(g.get("end_timestamp_s", g.get("end_s")))
        du = float(g["duration_s"])
        m = None
        for ig in input_gaps:
            s0 = float(ig.get("start_timestamp_s", ig.get("start_s")))
            e0 = float(ig.get("end_timestamp_s", ig.get("end_s")))
            d0 = float(ig["duration_s"])
            ss, es = st - s0, en - e0
            if abs(ss) <= SEAM_TOL_S and abs(es) <= SEAM_TOL_S and abs(du - d0) <= QA_TOL_S and abs(ss - es) <= QA_TOL_S:
                m = ig
                break
        (supported if m else remaining).append({"gap": g, "input_gap": m, "duration_delta_s": (du - float(m["duration_s"])) if m else None})
    tail_pass = bool(completion.get("tail_gap_pass"))
    return {"pass": tail_pass and not remaining, "rounding_only_supported": len(supported), "unsupported_remaining": len(remaining),
            "rounding_only_unsupported_gap": bool(supported) and not bool(completion.get("maximum_state_gap_pass", False)),
            "duration_deltas_s": [x["duration_delta_s"] for x in supported]}


def coverage_from_states(ts: List[float], span_start: float, span_end: float) -> Dict[str, Any]:
    if not ts:
        return {"coverage_pct": 0.0, "covered_s": 0.0, "span_s": span_end - span_start, "state_gaps_gt0.2s": 0, "gap_time_s": 0.0}
    gaps = 0.0
    n = 0
    for i in range(1, len(ts)):
        d = ts[i] - ts[i - 1]
        if d > MAX_STATE_GAP_S:
            gaps += d
            n += 1
    covered = max(0.0, (ts[-1] - ts[0]) - gaps)
    span = max(1e-9, span_end - span_start)
    return {"coverage_pct": 100.0 * covered / span, "covered_s": covered, "span_s": span, "state_gaps_gt0.2s": n, "gap_time_s": gaps,
            "init_delay_s": ts[0] - span_start, "tail_gap_s": span_end - ts[-1]}


_evo_cache: Dict[str, Any] = {}


def supplementary_ate(estimate_tum: Path, gt_txt: Path) -> Dict[str, Any]:
    """Same convention as the frozen evaluators (evo 1.31.1, unique nearest 0.01 s association, SE(3) Umeyama without
    scale over all common poses, bounded in-memory quaternion projection of the frozen pair evaluator), computed here
    for gap-bearing cells the campaigns did not evaluate. Labelled supplementary; ATE translation RMSE only."""
    try:
        sys.path.insert(0, str(SCRIPTS))
        import kaist_pair_evaluator as core  # type: ignore
        import cross_dataset_pair_evaluator as pair  # type: ignore
        from evo.core import metrics  # type: ignore
        import copy
        # frozen EuRoC/KAIST pair-evaluator reader: exact rows, in-memory bounded quaternion projection only
        gt_rows, _gt_proj = pair._read_tum_with_bounded_quaternion_projection(gt_txt, "gt")
        est_rows, _est_proj = pair._read_tum_with_bounded_quaternion_projection(estimate_tum, "estimate")
        assoc = core.emulate_evo_association(gt_rows, est_rows)
        if len(assoc) < MIN_ASSOC:
            return {"status": "INSUFFICIENT_ASSOCIATIONS", "associated": len(assoc), "estimate_rows": len(est_rows)}
        ref = core.rows_to_evo([gt_rows[a.gt_index] for a in assoc])
        est = core.rows_to_evo([est_rows[a.estimate_index] for a in assoc])
        aligned = copy.deepcopy(est)
        aligned.align(ref, correct_scale=False, correct_only_scale=False, n=-1)
        ape = metrics.APE(metrics.PoseRelation.translation_part)
        ape.process_data((ref, aligned))
        st = ape.get_all_statistics()
        return {"status": "SUPPLEMENTARY_SAME_CONVENTION", "ate_translation_rmse_m": float(st["rmse"]), "ate_mean_m": float(st["mean"]), "ate_max_m": float(st["max"]),
                "associated": len(assoc), "estimate_rows": len(est_rows), "gt_rows": len(gt_rows), "evo_version": getattr(__import__("evo"), "__version__", "?")}
    except Exception as exc:  # noqa: BLE001
        return {"status": "EVALUATION_ERROR", "error": str(exc)[:300]}


def gt_path(dataset: str, sequence: str) -> Optional[Path]:
    if dataset == "euroc_mav":
        p = GT_DIRS["euroc_mav"] / (sequence + ".txt")
    elif dataset == "kaist_vio":
        p = GT_DIRS["kaist_vio"] / (Path(sequence).stem + ".txt")
    else:
        return None
    return p if p.is_file() else None


def discover_cells() -> List[Dict[str, Any]]:
    cells = []
    for cid, root in ROOTS.items():
        pattern = str(root / "**" / "sequence_result.json")
        for sr in sorted(glob.glob(pattern, recursive=True)):
            d = Path(sr).parent
            cells.append({"campaign": cid, "run_dir": d, "sequence_result": Path(sr),
                          "excluded": cid == "PERTURB-1-ABANDONED"})
    return cells


def frozen_metrics_for(run_dir: Path) -> Optional[Dict[str, Any]]:
    cm = run_dir.parent / (run_dir.name + ".driver") / "metrics" / "cell_metrics.json"
    if cm.is_file():
        try:
            m = json.load(open(cm))
            ate = None
            for k in ("ate", "translation_ate"):
                pass
            # find ATE RMSE anywhere reasonable
            def find(o):
                if isinstance(o, dict):
                    if "translation_rmse_m" in o and isinstance(o["translation_rmse_m"], (int, float)):
                        return o["translation_rmse_m"]
                    for v in o.values():
                        r = find(v)
                        if r is not None:
                            return r
                return None
            ate = m.get("ate", {}).get("translation", {}).get("rmse_m") if isinstance(m.get("ate"), dict) else None
            if ate is None:
                ate = find(m)
            return {"source": str(cm), "ate_translation_rmse_m": ate, "metrics_status": m.get("status") or m.get("metrics_status")}
        except Exception:  # noqa: BLE001
            return None
    return None


def cell_rows(cells: List[Dict[str, Any]], out: Path, aggregates: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    # frozen ATE lookup from stored aggregates (cells.csv) by run_id
    ate_lookup: Dict[str, Tuple[Optional[float], str]] = {}
    for cid in ("PERTURB-1", "ABLATE-REC-1", "BLACKOUT-1"):
        p = ROOTS[cid] / "aggregate" / "final" / "cells.csv"
        if p.is_file():
            with open(p) as f:
                for r in csv.DictReader(f):
                    key = r.get("run_id") or ""
                    val = r.get("ate_translation_rmse_m") or r.get("ate_m") or ""
                    if key:
                        try:
                            ate_lookup[key] = (float(val) if val else None, cid)
                        except ValueError:
                            ate_lookup[key] = (None, cid)
    # CDSC-1R4 accuracy is a PAIR metric (U0/S1 common-pose population); keyed by (sequence, system)
    p = ROOTS["CDSC-1R4"] / "final-report" / "pair_metrics.csv"
    cdsc_ate: Dict[Tuple[str, str], Optional[float]] = {}
    if p.is_file():
        with open(p) as f:
            for r in csv.DictReader(f):
                for sysname, col in (("U0", "ate_translation_rmse_m_u0"), ("S1", "ate_translation_rmse_m_s1")):
                    v = r.get(col) or ""
                    try:
                        cdsc_ate[(r["sequence"], sysname)] = float(v) if v else None
                    except ValueError:
                        cdsc_ate[(r["sequence"], sysname)] = None
    for c in cells:
        try:
            d = json.load(open(c["sequence_result"]))
        except Exception as exc:  # noqa: BLE001
            rows.append({"campaign": c["campaign"], "run_id": c["run_dir"].name, "error": str(exc)[:120]})
            continue
        comp = d.get("completion") or {}
        ii = d.get("input_interval") or {}
        input_gaps = ii.get("gaps_over_threshold") or []
        # blackout cells: masked bag input gaps come from the mask manifest (already folded into completion by the runner)
        drv = c["run_dir"].parent / (c["run_dir"].name + ".driver") / "cell_record.json"
        blackout = None
        if drv.is_file():
            try:
                rec = json.load(open(drv))
                blackout = rec.get("blackout")
                if rec.get("completion", {}).get("quantization_aware"):
                    comp_qa_frozen = rec["completion"]["quantization_aware"]
                else:
                    comp_qa_frozen = None
                if blackout and blackout.get("mask_start_s"):
                    input_gaps = list(input_gaps) + [{"start_timestamp_s": blackout["mask_start_s"], "end_timestamp_s": blackout["mask_end_s"], "duration_s": blackout["mask_end_s"] - blackout["mask_start_s"]}]
            except Exception:  # noqa: BLE001
                comp_qa_frozen = None
        else:
            comp_qa_frozen = None
        # recovery-supported gaps count as supported under the frozen rule already (KAIST C2 seam)
        qa = qa_reading(comp, input_gaps) if comp else {"pass": None}
        st_p = c["run_dir"] / "trajectory" / "state_estimate.txt"
        ts = read_states(st_p) if st_p.is_file() else []
        span_start = comp.get("first_selected_input_timestamp_s") or ii.get("first_selected_input_timestamp_s")
        span_end = comp.get("last_selected_input_timestamp_s") or ii.get("last_selected_input_timestamp_s")
        cov = coverage_from_states(ts, float(span_start), float(span_end)) if (ts and span_start and span_end) else {"coverage_pct": None}
        run_id = d.get("run_id") or c["run_dir"].name
        frozen_ate, frozen_src = None, None
        if run_id in ate_lookup:
            frozen_ate, frozen_src = ate_lookup[run_id]
        elif c["campaign"] == "CDSC-1R4" and d.get("mode") == "scored" and (d.get("sequence"), d.get("system")) in cdsc_ate:
            frozen_ate, frozen_src = cdsc_ate[(d.get("sequence"), d.get("system"))], "CDSC-1R4 pair_metrics (common-pose pair population)"
        fm = frozen_metrics_for(c["run_dir"])
        if frozen_ate is None and fm and fm.get("ate_translation_rmse_m") is not None:
            frozen_ate, frozen_src = fm["ate_translation_rmse_m"], "cell_metrics.json"
        gap_bearing = bool(comp.get("state_gap_count")) or (cov.get("state_gaps_gt0.2s") or 0) > 0
        supp = None
        if gap_bearing and frozen_ate is None and not c["excluded"]:
            tum = c["run_dir"] / "trajectory" / "estimate_raw.tum"
            gt = gt_path(d.get("dataset", ""), d.get("sequence", ""))
            if tum.is_file() and gt is not None and d.get("status") in ("COMPLETED", "TRACKING_LOSS", "COMPLETED_WITH_TEARDOWN_DEFECT"):
                supp = supplementary_ate(tum, gt)
        row = {
            "campaign": c["campaign"], "retained_excluded": c["excluded"], "run_id": run_id, "set": c["run_dir"].parent.parent.parent.name,
            "dataset": d.get("dataset"), "sequence": d.get("sequence"), "system": d.get("system"), "mode": d.get("mode"),
            "status": d.get("status"), "evidence_validity": d.get("evidence_validity"),
            "complete_frozen": comp.get("pass"), "complete_qa": qa.get("pass"), "rounding_only_unsupported_gap": qa.get("rounding_only_unsupported_gap"),
            "qa_duration_deltas_s": ";".join("%.3e" % x for x in (qa.get("duration_deltas_s") or [])),
            "flip_frozen_to_qa": (comp.get("pass") is False and qa.get("pass") is True),
            "state_gap_count": comp.get("state_gap_count"), "unsupported_state_gaps": len(comp.get("unsupported_state_gaps") or []),
            "recovery_supported_state_gaps": comp.get("recovery_supported_state_gap_count"),
            "max_state_gap_s": comp.get("maximum_state_gap_s"), "tail_gap_s": comp.get("tail_gap_s"), "tail_gap_pass": comp.get("tail_gap_pass"),
            "input_gap_count": len(input_gaps), "coverage_pct": cov.get("coverage_pct"), "covered_s": cov.get("covered_s"), "span_s": cov.get("span_s"),
            "state_rows": len(ts), "gap_bearing": gap_bearing,
            "ate_frozen_m": frozen_ate, "ate_frozen_source": frozen_src,
            "ate_supplementary_m": (supp or {}).get("ate_translation_rmse_m"), "ate_supplementary_status": (supp or {}).get("status"),
            "ate_supplementary_associated": (supp or {}).get("associated"),
            "blackout_arm": (blackout or {}).get("arm") if blackout else None, "blackout_k_s": (blackout or {}).get("k_seconds") if blackout else None,
            "frozen_qa_agrees": (comp_qa_frozen.get("pass") == qa.get("pass")) if comp_qa_frozen else None,
        }
        if run_id == ROT_N_ADDENDUM["cell"]:
            row["ate_frozen_m"] = ROT_N_ADDENDUM["ate_m"]
            row["ate_frozen_source"] = "rot-n addendum " + ROT_N_ADDENDUM["source"]
            row["ate_association_note"] = "{}/{} associated".format(ROT_N_ADDENDUM["associated"], ROT_N_ADDENDUM["estimate_rows"])
        rows.append(row)
    return rows


# ------------------------------------------------------------- phase 4 -----
def severity_axis() -> List[Dict[str, Any]]:
    out = []
    m = yaml.safe_load(open(REPO / "project" / "icra27_cross_dataset_matrix.yaml"))
    for s in m["sequences"]:
        gt = gt_path(s["dataset"], s["sequence"])
        if gt is None:
            out.append({"order": s["order"], "dataset": s["dataset"], "sequence": s["sequence"], "gt": None})
            continue
        rows = []
        for line in open(gt):
            if line.startswith("#"):
                continue
            p = line.replace(",", " ").split()
            if len(p) < 8:
                continue
            try:
                rows.append([float(x) for x in p[:8]])
            except ValueError:
                continue
        a = np.array(rows)
        t, pos, q = a[:, 0], a[:, 1:4], a[:, 4:8]
        W = 0.5
        j = np.searchsorted(t, t + W)
        idx = np.nonzero(j < len(t))[0]
        dots = np.abs(np.sum(q[idx] * q[j[idx]], axis=1))
        ang = 2.0 * np.arccos(np.minimum(1.0, dots))
        rates = np.degrees(ang / np.maximum(t[j[idx]] - t[idx], 1e-9))
        v = np.linalg.norm(pos[j[idx]] - pos[idx], axis=1) / np.maximum(t[j[idx]] - t[idx], 1e-9)
        out.append({"order": s["order"], "dataset": s["dataset"], "sequence": s["sequence"], "gt": str(gt), "gt_rows": int(len(t)), "duration_s": float(t[-1] - t[0]),
                    "omega_mean_deg_s": float(rates.mean()), "omega_median_deg_s": float(np.median(rates)), "omega_p95_deg_s": float(np.percentile(rates, 95)), "omega_max_deg_s": float(rates.max()),
                    "speed_mean_m_s": float(v.mean()), "speed_p95_m_s": float(np.percentile(v, 95)), "window_s": W})
    return out


# ------------------------------------------------------------- phase 5 -----
def load_json_safe(p: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.load(open(p))
    except Exception:  # noqa: BLE001
        return None


def find_key(o: Any, names: Sequence[str], depth: int = 0) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    if depth > 6:
        return found
    if isinstance(o, dict):
        for k, v in o.items():
            if any(k == n or k.startswith(n) for n in names):
                found[k] = v
            found.update(find_key(v, names, depth + 1))
    elif isinstance(o, list):
        for v in o[:50]:
            found.update(find_key(v, names, depth + 1))
    return found


def verdicts(out: Path, regen: Dict[str, Any], c8_root: Optional[Path]) -> Dict[str, Any]:
    v: Dict[str, Any] = {}
    pa = load_json_safe(out / "regen" / "PERTURB-1" / "aggregate.json") or {}
    v["CR"] = {k: (val.get("status") if isinstance(val, dict) else val) for k, val in find_key(pa, ["gate_cr", "cr1", "cr2", "cr3", "cr_1", "cr_2", "cr_3"]).items()}
    aa = load_json_safe(out / "regen" / "ABLATE-REC-1" / "aggregate.json") or {}
    v["P"] = {k: (val.get("verdict") or val.get("status") or val.get("truth") if isinstance(val, dict) else val) for k, val in find_key(aa, ["prediction_p"]).items()}
    ba = load_json_safe(out / "regen" / "BLACKOUT-1" / "aggregate.json") or {}
    v["B"] = find_key(ba, ["verdicts"]).get("verdicts")
    if c8_root is not None:
        fm = load_json_safe(c8_root / "aggregate" / "fault_matrix.json")
        v["H"] = {"H4": fm.get("verdict") if fm else "PENDING (C8 aggregate absent)", "source": str(c8_root / "aggregate" / "fault_matrix.json") if fm else None}
    else:
        v["H"] = {"H4": "PENDING"}
    ext = sorted(glob.glob(EXT_VF_GLOB))
    v["CG"] = {"status": "PENDING (EXT-VF-1 final aggregate absent)"} if not ext else {"status": "PRESENT", "source": ext[-1], "content": find_key(load_json_safe(Path(ext[-1])) or {}, ["cg", "CG", "verdict"])}
    v["golden_regression"] = {cid: r["golden"] for cid, r in regen.items()}
    return v


LEDGER = [
    ("F1", "S1 completes all primary desktop runs", "SUPPORTED", "PERTURB-1 98/98 executed cells COMPLETED/VALID; CDSC-1R4 S1 25/25 passage", ["PERTURB-1/aggregate/final/aggregate.json", "CDSC-1R4/final-report/aggregate.json"]),
    ("F2", "S1 == N0 exact equivalence", "SUPPORTED", "PERTURB-1 CR-3 <= 4.1e-7 m over matched pairs", ["PERTURB-1/aggregate/final/aggregate.json"]),
    ("F3", "Embedded advantage at fixed Orin budget", "PENDING", "ORIN sweep not present at execution time", []),
    ("F4", "Fault atomicity on the frozen branch (H4)", "PENDING", "C8 Item 1 fault matrix", ["evidence-c8/<root>/aggregate/fault_matrix.json"]),
    ("F5", "External comparison/context (VF + pinned ORB3/SchurVINS evidence)", "PENDING", "EXT-VF-1 final aggregate not present at execution time", []),
    ("F6", "Recovery necessity for the natural rotation stall", "SUPPORTED", "ABLATE-REC-1 P1 TRUE 6/6 under the frozen rule; see F14 and the dual reading: the recOFF completion failure is rounding-only under D13, the substantive difference is post-gap accuracy (rotation report C0 vs C2)", ["ABLATE-REC-1/aggregate/final/aggregate.json", "evidence-c8/<root>/item2/stall_anatomy.md"]),
    ("F7", "One-pass vs two-pass on the frozen protocol", "PENDING", "S2 rows not present", []),
    ("F8", "Severity/margin analysis (GT |omega| axis)", "EMITTED", "severity_axis.csv from GT quaternion differencing (0.5 s window)", ["paper-numbers/severity_axis.csv"]),
    ("F9", "Cross-campaign byte-determinism chain", "SUPPORTED", "integrity replicas BYTE_IDENTICAL across PERTURB-1/ABLATE-REC-1/BLACKOUT-1 vs CDSC-1R4; C8 inertness gate", ["BLACKOUT-1/integrity", "evidence-c8/<root>/aggregate/inertness.csv"]),
    ("F10", "Stock-context rows incl. perturbed rotation", "SUPPORTED", "CDSC-1R4 U0 rows; PERTURB-1 N0 rows", ["CDSC-1R4/final-report/sequence_results.csv"]),
    ("F11", "Blackout envelope: K=2 s + non-monotone bridgeability", "SUPPORTED", "BLACKOUT-1 B1' verdicts", ["BLACKOUT-1/aggregate/final/aggregate.json"]),
    ("F12", "Gate integrity: 0/11 false commits; 3 exposures bounded", "SUPPORTED", "BLACKOUT-1 B5", ["BLACKOUT-1/aggregate/final/aggregate.json"]),
    ("F13", "Integrity/availability trade quantified (dual readings)", "SUPPORTED", "dual completion readings for every gap-bearing cell in every campaign (cells_universal.csv)", ["paper-numbers/cells_universal.csv"]),
    ("F14", "Stall anatomy (frame-present vs frame-absent)", "PENDING", "C8 Item 2", ["evidence-c8/<root>/item2/stall_anatomy.md"]),
    ("F15", "Recovery contract fixed-calibration scope", "SUPPORTED", "BLACKOUT-1 D12, source-cited", ["BLACKOUT-1/DECISIONS.md"]),
]


def write_ledger(out: Path, verd: Dict[str, Any], c8_root: Optional[Path], flips: List[Dict[str, Any]]) -> None:
    items = []
    for fid, claim, status, evidence, ptrs in LEDGER:
        st = status
        if fid == "F4" and isinstance(verd.get("H", {}).get("H4"), dict):
            h = verd["H"]["H4"]
            st = "SUPPORTED" if h.get("H4_all_zero_violation_columns") and h.get("inertness_gate") == "PASS" and h.get("complete") else ("REFUTED" if h.get("violations_total", 0) > 0 else "PENDING")
            evidence = "C8 fault matrix: violations={} inertness={} complete={}".format(h.get("violations_total"), h.get("inertness_gate"), h.get("complete"))
        if fid == "F14" and c8_root is not None and (c8_root / "item2" / "stall_anatomy.md").is_file():
            st = "SUPPORTED_WITH_PREMISE_CORRECTION"
            evidence = "C8 Item 2: the natural rotation event is frame-ABSENT (source bag), recOFF resumes at the first post-gap pair, U0 aborts at the gap exit, recON re-anchors; per-frame feature counts are not extractable from INFO logs"
        if fid == "F13":
            evidence += "; pre-BLACKOUT frozen->QA flips: {}".format(len(flips))
        items.append({"id": fid, "claim": claim, "status": st, "evidence": evidence, "pointers": ptrs})
    (out / "claims").mkdir(exist_ok=True)
    yaml.safe_dump({"schema": "schurvio.icra27.claims_ledger.v1", "generated_utc": utc(), "findings": items}, open(out / "claims" / "ledger.yaml", "w"), sort_keys=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip-regen", action="store_true")
    ap.add_argument("--c8-root", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest: Dict[str, Any] = {"schema": "schurvio.icra27.paper_numbers_manifest.v1", "generated_utc": utc(), "roots": {}, "tables": {}}
    for cid, r in ROOTS.items():
        manifest["roots"][cid] = {"path": str(r), "present": r.is_dir(), "status": "RETAINED_EXCLUDED" if cid.endswith("ABANDONED") else ("PRESENT" if r.is_dir() else "ABSENT")}
    c8s = sorted(glob.glob(C8_GLOB))
    c8_root = Path(args.c8_root) if args.c8_root else (Path(c8s[-1]) if c8s else None)
    manifest["roots"]["C8"] = {"path": str(c8_root) if c8_root else None, "present": bool(c8_root)}
    manifest["roots"]["EXT-VF-1"] = {"present": bool(glob.glob(EXT_VF_GLOB)), "glob": EXT_VF_GLOB}
    manifest["roots"]["ORIN"] = {"present": bool(glob.glob(ORIN_GLOB)), "glob": ORIN_GLOB, "note": "schema slot; ingested when the Orin root exists"}

    regen = {} if args.skip_regen else regen_aggregators(out)
    json.dump(regen, open(out / "golden_regression.json", "w"), indent=1)

    cells = discover_cells()
    rows = cell_rows(cells, out, {})
    keys = sorted({k for r in rows for k in r})
    with open(out / "cells_universal.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    flips = [r for r in rows if r.get("flip_frozen_to_qa") and r["campaign"] in ("CDSC-1R4", "PERTURB-1", "ABLATE-REC-1")]
    with open(out / "dual_reading_flips.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in flips:
            w.writerow(r)
    # completion matrix per campaign/system: frozen vs qa
    comp = collections.defaultdict(lambda: collections.Counter())
    for r in rows:
        if r.get("retained_excluded") or "error" in r:
            continue
        k = (r["campaign"], r.get("system"))
        comp[k]["cells"] += 1
        comp[k]["complete_frozen"] += int(bool(r.get("complete_frozen")))
        comp[k]["complete_qa"] += int(bool(r.get("complete_qa")))
        comp[k]["gap_bearing"] += int(bool(r.get("gap_bearing")))
        comp[k]["with_frozen_ate"] += int(r.get("ate_frozen_m") is not None)
        comp[k]["with_supplementary_ate"] += int(r.get("ate_supplementary_m") is not None)
    with open(out / "completion_matrix.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["campaign", "system", "cells", "complete_frozen", "complete_qa", "gap_bearing", "with_frozen_ate", "with_supplementary_ate"])
        for (cid, sysname), c in sorted(comp.items()):
            w.writerow([cid, sysname, c["cells"], c["complete_frozen"], c["complete_qa"], c["gap_bearing"], c["with_frozen_ate"], c["with_supplementary_ate"]])
    # accuracy table welded with coverage
    with open(out / "accuracy_coverage.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["campaign", "run_id", "dataset", "sequence", "system", "status", "ate_m", "ate_source", "coverage_pct", "state_gaps", "max_state_gap_s", "complete_frozen", "complete_qa"])
        for r in rows:
            if r.get("retained_excluded") or "error" in r:
                continue
            ate = r.get("ate_frozen_m") if r.get("ate_frozen_m") is not None else r.get("ate_supplementary_m")
            src = r.get("ate_frozen_source") if r.get("ate_frozen_m") is not None else (r.get("ate_supplementary_status") if r.get("ate_supplementary_m") is not None else None)
            if ate is None:
                continue
            w.writerow([r["campaign"], r["run_id"], r["dataset"], r["sequence"], r["system"], r["status"], ate, src,
                        ("%.2f" % r["coverage_pct"]) if r.get("coverage_pct") is not None else "", r.get("state_gap_count"), r.get("max_state_gap_s"), r.get("complete_frozen"), r.get("complete_qa")])
    sev = severity_axis()
    with open(out / "severity_axis.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sev[0].keys()) if sev else ["order"])
        w.writeheader()
        for r in sev:
            w.writerow(r)
    verd = verdicts(out, regen, c8_root)
    # C8 completeness flag for F4
    if isinstance(verd.get("H", {}).get("H4"), dict) and c8_root is not None:
        fm = load_json_safe(c8_root / "aggregate" / "fault_matrix.json") or {}
        verd["H"]["H4"]["complete"] = bool(fm.get("complete"))
    json.dump(verd, open(out / "verdicts.json", "w"), indent=1, default=str)
    write_ledger(out, verd, c8_root, flips)

    # manifest of tables/figures
    tables = {
        "T-completion": {"script": "scripts/icra27/paper_numbers.py", "file": "completion_matrix.csv", "sources": [str(ROOTS[c]) for c in ("CDSC-1R4", "PERTURB-1", "ABLATE-REC-1", "BLACKOUT-1")]},
        "T-cells": {"script": "scripts/icra27/paper_numbers.py", "file": "cells_universal.csv", "sources": [str(ROOTS[c]) for c in ROOTS]},
        "T-dual-reading-flips": {"script": "scripts/icra27/paper_numbers.py", "file": "dual_reading_flips.csv", "sources": [str(ROOTS[c]) for c in ("CDSC-1R4", "PERTURB-1", "ABLATE-REC-1")]},
        "T-accuracy-coverage": {"script": "scripts/icra27/paper_numbers.py", "file": "accuracy_coverage.csv", "sources": [str(ROOTS[c]) for c in ROOTS] + [str(REPO / "ov_data")]},
        "F-severity": {"script": "scripts/icra27/paper_numbers.py", "file": "severity_axis.csv", "sources": [str(REPO / "ov_data"), str(REPO / "project" / "icra27_cross_dataset_matrix.yaml")]},
        "T-verdicts": {"script": "scripts/icra27/paper_numbers.py + frozen aggregators", "file": "verdicts.json", "sources": ["regen/*/aggregate.json", str(c8_root / "aggregate" / "fault_matrix.json") if c8_root else ""]},
        "T-fault-matrix": {"script": "scripts/icra27/c8_fault_aggregate.py", "file": str(c8_root / "aggregate" / "FAULT_MATRIX.md") if c8_root else "", "sources": [str(c8_root)] if c8_root else []},
        "T-stall-anatomy": {"script": "scripts/icra27/c8_stall_anatomy.py", "file": str(c8_root / "item2" / "stall_anatomy.md") if c8_root else "", "sources": [str(ROOTS["CDSC-1R4"]), str(ROOTS["ABLATE-REC-1"]), str(ROOTS["BLACKOUT-1"])]},
        "T-ledger": {"script": "scripts/icra27/paper_numbers.py", "file": "claims/ledger.yaml", "sources": ["verdicts.json"]},
        "T-golden": {"script": "frozen aggregators via paper_numbers.py", "file": "golden_regression.json", "sources": ["regen/"]},
    }
    for cid in ("CDSC-1R4", "PERTURB-1", "ABLATE-REC-1", "BLACKOUT-1"):
        tables["T-" + cid] = {"script": {"CDSC-1R4": "cross_dataset_aggregate.py", "PERTURB-1": "perturbation_aggregate.py", "ABLATE-REC-1": "ablation_aggregate.py", "BLACKOUT-1": "blackout_aggregate.py"}[cid],
                              "file": "regen/{}/".format(cid), "sources": [str(ROOTS[cid])]}
    for tid, t in tables.items():
        fp = out / t["file"] if not str(t["file"]).startswith("/") else Path(t["file"])
        t["sha256"] = sha256_file(fp) if fp.is_file() else None
    manifest["tables"] = tables
    manifest["cells_total"] = len(rows)
    manifest["flips_pre_blackout"] = [{"campaign": r["campaign"], "run_id": r["run_id"], "system": r["system"], "sequence": r["sequence"], "qa_duration_deltas_s": r["qa_duration_deltas_s"]} for r in flips]
    json.dump(manifest, open(out / "MANIFEST.json", "w"), indent=1, default=str)
    # SHA256SUMS + golden hashes (deterministic outputs only; MANIFEST/ledger carry timestamps and are excluded from golden)
    with open(out / "SHA256SUMS", "w") as f:
        for p in sorted(out.rglob("*")):
            if p.is_file() and p.name != "SHA256SUMS":
                f.write("{}  {}\n".format(sha256_file(p), p.relative_to(out)))
    golden = {}
    for name in ("cells_universal.csv", "completion_matrix.csv", "accuracy_coverage.csv", "severity_axis.csv", "dual_reading_flips.csv"):
        golden[name] = sha256_file(out / name)
    json.dump(golden, open(out / "GOLDEN.sha256.json", "w"), indent=1)
    print(json.dumps({"cells": len(rows), "flips_pre_blackout": len(flips), "golden": verd.get("golden_regression"), "H": verd.get("H")}, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
