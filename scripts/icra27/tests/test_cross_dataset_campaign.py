#!/usr/bin/python3.8
"""Focused non-ROS tests for the resume-safe CDSC-1 campaign driver."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple


SCRIPT = Path(__file__).resolve().parents[1] / "cross_dataset_campaign.py"
SPEC = importlib.util.spec_from_file_location("cross_dataset_campaign_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
campaign = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = campaign
SPEC.loader.exec_module(campaign)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _publish_checksums(directory: Path, manifest_name: str) -> None:
    del manifest_name
    files = sorted(
        path for path in directory.rglob("*") if path.is_file() and path.name != "SHA256SUMS"
    )
    payload = "".join(
        "{}  {}\n".format(_digest(path.read_bytes()), path.relative_to(directory).as_posix())
        for path in files
    )
    (directory / "SHA256SUMS").write_text(payload, encoding="ascii")


def _row(
    order: int,
    dataset: str = "euroc_mav",
    sequence: Optional[str] = None,
    capability: str = "embedded_partial_intervals",
) -> campaign.MatrixRow:
    if sequence is None:
        sequence = "sequence_{}".format(order)
    system_order = ("U0", "S1") if order % 2 else ("S1", "U0")
    return campaign.MatrixRow(
        order=order,
        dataset=dataset,
        sequence=sequence,
        system_order=system_order,
        bag_start_seconds=0.0,
        bag={"path": "/unopened/input_{}.bag".format(order), "bytes": 123, "sha256": "b" * 64},
        ground_truth={
            "capability": capability,
            "canonical_path": "/must/not/open/gt_{}.tum".format(order),
            "bytes": 456,
            "sha256": "c" * 64,
        },
    )


def _publish_sequence_result(
    root: Path,
    lane: str,
    row: campaign.MatrixRow,
    system: str,
    status: str = "COMPLETED",
    linkage_valid: bool = True,
    runner_path: Optional[Path] = None,
    include_raw_geometry: bool = True,
    closed_utc: str = "2026-08-16T12:00:00Z",
) -> Path:
    location = campaign.run_location(root, lane, row, system)
    location.run_directory.mkdir(parents=True)
    retained = status in campaign.RETAINED_ALGORITHM_STATUSES
    closed_capture_mismatch = bool(lane == "capture" and not linkage_valid)
    runtime_valid = retained or closed_capture_mismatch
    eligible = status in campaign.ELIGIBLE_STATUSES
    artifacts: Dict[str, Any] = {
        "state": {"relative_path": "trajectory/state_estimate.txt", "identity": None},
        "deviation": {"relative_path": "trajectory/state_deviation.txt", "identity": None},
        "tum": {"relative_path": "trajectory/estimate_raw.tum", "identity": None},
    }
    if lane == "capture" and include_raw_geometry:
        raw = location.run_directory / "geometry" / "feature_stream.bag"
        raw.parent.mkdir()
        raw.write_bytes(b"closed fake ROS geometry bag\n")
        artifacts["raw_geometry"] = {
            "relative_path": "geometry/feature_stream.bag",
            "identity": campaign.file_identity(raw),
            "active_identity": None,
        }
    if closed_capture_mismatch:
        state = location.run_directory / "trajectory" / "state_estimate.txt"
        state.parent.mkdir(exist_ok=True)
        state.write_text("capture differs from scored\n", encoding="ascii")
        artifacts["state"]["identity"] = campaign.file_identity(state)
    runner_identity = campaign.file_identity(runner_path) if runner_path is not None else None
    if runner_identity is not None:
        # Real runners publish a richer record.  The driver deliberately
        # compares the stable path/size/SHA projection.
        runner_identity = {**runner_identity, "mtime_ns": 123456789, "executable": True}
    evidence_validity = (
        "VALID"
        if runtime_valid and linkage_valid
        else ("CAPTURE_LINK_INVALID" if lane == "capture" and not linkage_valid else "INVALID_INFRA")
    )
    scored_linkage = None
    if lane == "capture":
        scored_path = campaign.run_location(root, "scored", row, system).result_path.resolve(
            strict=True
        )
        scored_value = json.loads(scored_path.read_text(encoding="utf-8"))
        scored_artifacts = scored_value["artifacts"]
        comparisons: Dict[str, Any] = {}
        expected_artifacts: Dict[str, Any] = {}
        for name in ("state", "deviation", "tum"):
            expected = scored_artifacts[name]["identity"]
            observed = artifacts[name]["identity"]
            exact = bool(
                (expected is None and observed is None)
                or (
                    isinstance(expected, dict)
                    and isinstance(observed, dict)
                    and expected["size_bytes"] == observed["size_bytes"]
                    and expected["sha256"] == observed["sha256"]
                )
            )
            expected_artifacts[name] = expected
            comparisons[name] = {
                "expected_sha256": expected["sha256"] if isinstance(expected, dict) else None,
                "observed_sha256": observed["sha256"] if isinstance(observed, dict) else None,
                "expected_source_unchanged_during_capture": True,
                "byte_exact": exact,
            }
        scored_identity = campaign.file_identity(scored_path)
        scored_linkage = {
            "status": "LINKED_EXACT" if linkage_valid else "OUTPUT_MISMATCH",
            "byte_exact": linkage_valid,
            "sequence_result": scored_identity,
            "sequence_result_path": str(scored_path),
            "sequence_result_after": scored_identity,
            "expected_artifacts": expected_artifacts,
            "post_capture_expected_artifacts": expected_artifacts,
            "source_unchanged_during_capture": True,
            "artifact_comparisons": comparisons,
        }
    value: Dict[str, Any] = {
        "schema": campaign.RUN_SCHEMA,
        "protocol_id": campaign.PROTOCOL_ID,
        "run_id": location.run_id,
        "attempt_index": 1,
        "dataset": row.dataset,
        "sequence": row.sequence,
        "system": system,
        "mode": lane,
        "status": status,
        "evidence_validity": evidence_validity,
        "run_directory": str(location.run_directory.resolve()),
        "accuracy_eligible": bool(lane == "scored" and eligible),
        "qualitative_eligible": bool(lane == "capture" and eligible and linkage_valid),
        "passage": {"complete": bool(eligible)},
        "estimator_close_receipt": {
            "estimator_attempted": bool(runtime_valid),
            "estimator_process_group_closed": bool(runtime_valid),
            "runtime_services_closed": bool(runtime_valid),
            "closed_utc": closed_utc if runtime_valid else None,
        },
        "checks": {
            "status_known": True,
            "runtime_inputs_unchanged": bool(runtime_valid),
        },
        "outcome_facts": {
            "runtime_contract_valid": bool(runtime_valid),
            "teardown_ok": bool(runtime_valid),
        },
        "inputs": {"runner": runner_identity},
        "artifacts": artifacts,
        "publication": {
            "sequence_result": "sequence_result.json",
            "checksums": "SHA256SUMS",
            "append_only_run_directory": True,
        },
        "scored_linkage": scored_linkage,
    }
    _write_json(location.result_path, value)
    _publish_checksums(location.run_directory, "sequence_result.json")
    return location.result_path


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.wall = 2_000_000_000.0
        self.sleeps = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.wall += seconds

    def wall_time(self) -> float:
        return self.wall


class FakeExecutor:
    def __init__(
        self,
        root: Path,
        rows: Sequence[campaign.MatrixRow],
        statuses: Optional[Mapping[Tuple[str, str, str], str]] = None,
        invalid_capture_linkage: bool = False,
        omit_capture_geometry: bool = False,
        mechanism_status: str = "C2_VALIDATION_FAILURE",
        mechanism_ground_truth_opened: bool = True,
        pair_status: str = "COMPLETE",
    ) -> None:
        self.root = root
        self.rows = {row.sequence: row for row in rows}
        self.statuses = dict(statuses or {})
        self.invalid_capture_linkage = invalid_capture_linkage
        self.omit_capture_geometry = omit_capture_geometry
        self.mechanism_status = mechanism_status
        self.mechanism_ground_truth_opened = mechanism_ground_truth_opened
        self.pair_status = pair_status
        self.calls = []
        self.kinds = []
        self.trial_count = 0

    @staticmethod
    def _argument(command: Sequence[str], name: str) -> str:
        index = list(command).index(name)
        return str(command[index + 1])

    def __call__(self, command: Sequence[str]) -> campaign.CommandResult:
        command = list(command)
        self.calls.append(command)
        if "--protocol-id" in command:
            self.kinds.append("trial")
            self.trial_count += 1
            lane = self._argument(command, "--mode")
            sequence = self._argument(command, "--sequence")
            system = self._argument(command, "--system")
            row = self.rows[sequence]
            status_key = (lane, sequence, system)
            status = self.statuses.get(status_key, "COMPLETED")
            if (
                lane == "capture"
                and self.invalid_capture_linkage
                and status_key not in self.statuses
            ):
                status = "INVALID_LINKAGE"
            protocol_index = command.index("--protocol-id")
            runner_path = Path(command[protocol_index - 2])
            _publish_sequence_result(
                self.root,
                lane,
                row,
                system,
                status=status,
                linkage_valid=not self.invalid_capture_linkage,
                runner_path=runner_path,
                include_raw_geometry=not self.omit_capture_geometry,
            )
            return campaign.CommandResult(
                0 if status in campaign.ELIGIBLE_STATUSES else 2,
                "runner {}".format(status),
                "",
            )
        if "post-pair-rotation" in command:
            self.kinds.append("kaist_mechanism")
            output = Path(self._argument(command, "--output"))
            u0_result = Path(self._argument(command, "--u0-result"))
            s1_result = Path(self._argument(command, "--s1-result"))
            matrix = Path(self._argument(command, "--matrix"))
            ground_truth = Path(self._argument(command, "--ground-truth"))
            row = self.rows["rotation/rotation.bag"]
            output.parent.mkdir(parents=True, exist_ok=True)
            status = self.mechanism_status
            ground_truth_opened = self.mechanism_ground_truth_opened
            if status == "PASS" and not ground_truth_opened:
                raise AssertionError("synthetic PASS mechanism must open GT")
            _write_json(
                output,
                {
                    "schema": "schurvio.icra27.cross_dataset.kaist_rotation_post_pair.v1",
                    "status": status,
                    "pass": status == "PASS",
                    "protocol_id": campaign.PROTOCOL_ID,
                    "dataset": "kaist_vio",
                    "sequence": "rotation/rotation.bag",
                    "source_runs": {
                        "U0": {"result": campaign.file_identity(u0_result)},
                        "S1": {"result": campaign.file_identity(s1_result)},
                    },
                    "both_scored_estimator_groups_closed_before_ground_truth_open": True,
                    "ground_truth_opened": ground_truth_opened,
                    "matrix_binding": {
                        "matrix": campaign.file_identity(matrix),
                        "ground_truth_expected_from_matrix": {
                            "canonical_path": row.ground_truth.get("canonical_path"),
                            "size_bytes": row.ground_truth.get("bytes"),
                            "sha256": row.ground_truth.get("sha256"),
                            "capability": row.ground_truth.get("capability"),
                            "format": row.ground_truth.get("format"),
                            "source": "FROZEN_MATRIX_DECLARATION_NOT_LIVE_FILE",
                            "live_file_opened": False,
                        },
                        "ground_truth": (
                            campaign.file_identity(ground_truth)
                            if ground_truth_opened
                            else None
                        ),
                        "ground_truth_opened": ground_truth_opened,
                    },
                    "c2_prerequisite": {
                        "pass": ground_truth_opened,
                        "reason_code": (
                            "EVALUABLE"
                            if ground_truth_opened
                            else "S1_MECHANISM_CONTRACT_FAILED"
                        ),
                    },
                    "c2_validation_failure": (
                        None
                        if status == "PASS"
                        else {
                            "reason_code": (
                                "C2_GAP_OR_NUMERIC_GATE_FAILED"
                                if ground_truth_opened
                                else "S1_MECHANISM_CONTRACT_FAILED"
                            ),
                            "algorithm_failure_retained": True,
                            "infrastructure_failure": False,
                        }
                    ),
                    "accuracy_eligibility_unchanged_by_c2_target_thresholds": True,
                    "c2_validation_is_a_separate_robustness_mechanism_result": True,
                },
            )
            return campaign.CommandResult(0 if status == "PASS" else 2, status, "")
        if "--u0-run" in command:
            self.kinds.append("pair")
            output = Path(self._argument(command, "--output"))
            row = next(row for row in self.rows.values() if campaign.row_key(row) == output.name)
            output.mkdir(parents=True)
            _write_json(
                output / "pair_result.json",
                {
                    "schema": campaign.PAIR_SCHEMA,
                    "status": self.pair_status,
                    "dataset": row.dataset,
                    "sequence": row.sequence,
                },
            )
            _publish_checksums(output, "pair_result.json")
            return campaign.CommandResult(0, "pair", "")
        if "--estimator-close-receipt" in command:
            self.kinds.append("tum_reference")
            output = Path(self._argument(command, "--output-dir"))
            sequence = self._argument(command, "--sequence-id")
            output.mkdir(parents=True)
            _write_json(
                output / "interval_manifest.json",
                {
                    "schema": campaign.TUM_REFERENCE_SCHEMA,
                    "status": "COMPLETE",
                    "sequence_id": sequence,
                },
            )
            _publish_checksums(output, "interval_manifest.json")
            return campaign.CommandResult(0, "reference", "")
        if "--capture-result" in command:
            self.kinds.append("geometry")
            output = Path(self._argument(command, "--output-dir"))
            capture = Path(self._argument(command, "--capture-result"))
            value = json.loads(capture.read_text(encoding="utf-8"))
            row = self.rows[value["sequence"]]
            mismatch = value.get("evidence_validity") == "CAPTURE_LINK_INVALID"
            output.mkdir(parents=True)
            _write_json(
                output / "qualitative_manifest.json",
                {
                    "schema": "schurvio.icra27.cross_dataset_geometry_bundle.v1",
                    "status": (
                        "FAILURE_EVIDENCE_COMPLETE"
                        if mismatch
                        else "GEOMETRY_EVIDENCE_COMPLETE"
                    ),
                    "bundle_mode": (
                        "FAILURE_TILE_ONLY"
                        if mismatch
                        else (
                            "REFERENCE_BACKED_VALIDATED_V3"
                            if row.ground_truth["capability"] == "full_trajectory"
                            else "NATIVE_ESTIMATOR_FRAME_PARTIAL_REFERENCE"
                        )
                    ),
                    "dataset": value["dataset"],
                    "sequence": value["sequence"],
                    "system": value["system"],
                },
            )
            _publish_checksums(output, "qualitative_manifest.json")
            return campaign.CommandResult(0, "geometry", "")
        raise AssertionError("unexpected command: {}".format(command))


class CampaignTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.artifacts = self.base / "artifacts"
        repo = self.base / "repo"
        u0 = self.base / "u0"
        repo.mkdir()
        u0.mkdir()
        protocol = self.base / "protocol.md"
        matrix = self.base / "matrix.yaml"
        protocol.write_text("- Protocol ID: `CDSC-1`\nPROSPECTIVE_NOT_RUN\n", encoding="utf-8")
        matrix.write_text("fixture: true\n", encoding="utf-8")
        self.paths = campaign.RuntimePaths(
            repo_root=repo,
            protocol=protocol,
            matrix=matrix,
            generic_runner=self.base / "cross_dataset_trial.py",
            kaist_runner=self.base / "cross_dataset_kaist_trial.py",
            pair_evaluator=self.base / "pair.py",
            tum_extractor=self.base / "extract.py",
            geometry_bundler=self.base / "geometry.py",
            u0_source_root=u0,
            u0_setup=self.base / "u0_setup.bash",
            u0_binary=self.base / "u0_binary",
            s1_setup=self.base / "s1_setup.bash",
            s1_binary=self.base / "s1_binary",
        )
        for path in (
            self.paths.generic_runner,
            self.paths.kaist_runner,
            self.paths.pair_evaluator,
            self.paths.tum_extractor,
            self.paths.geometry_bundler,
        ):
            path.write_text("# fake tool\n", encoding="utf-8")
        self.clock = Clock()
        self.git_calls = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git_validator(self, paths: campaign.RuntimePaths, commit: str, tree: str) -> Mapping[str, Any]:
        self.git_calls += 1
        self.assertEqual(commit, "a" * 40)
        self.assertEqual(tree, "d" * 40)
        return {
            "S1": {"head_sha": commit, "head_tree": tree, "dirty": False},
            "U0": {"head_sha": campaign.U0_COMMIT, "head_tree": campaign.U0_TREE, "dirty": False},
        }

    def _options(
        self,
        lane: str = "scored",
        dataset: str = "all",
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> campaign.CampaignOptions:
        return campaign.CampaignOptions(
            artifact_root=self.artifacts,
            lane=lane,
            dataset=dataset,
            start_order=start,
            end_order=end,
            expected_tooling_commit="a" * 40,
            expected_tooling_tree="d" * 40,
        )

    def _campaign(
        self,
        rows: Sequence[campaign.MatrixRow],
        executor: FakeExecutor,
        options: Optional[campaign.CampaignOptions] = None,
    ) -> campaign.Campaign:
        return campaign.Campaign(
            self.paths,
            options or self._options(),
            rows,
            executor=executor,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            wall_time_fn=self.clock.wall_time,
            git_validator=self._git_validator,
        )

    def test_resume_adopts_terminal_result_and_runs_only_missing_cell(self) -> None:
        row = _row(1)
        _publish_sequence_result(
            self.artifacts, "scored", row, "U0", runner_path=self.paths.generic_runner
        )
        fake = FakeExecutor(self.artifacts, [row])
        result = self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(result["status"], "COMPLETE")
        trial_systems = [FakeExecutor._argument(call, "--system") for call in fake.calls]
        self.assertEqual(trial_systems, ["S1"])

    def test_resume_honors_remaining_cross_process_cooldown(self) -> None:
        row = _row(1)
        _publish_sequence_result(
            self.artifacts,
            "scored",
            row,
            "U0",
            runner_path=self.paths.generic_runner,
            closed_utc="2033-05-18T03:33:19Z",
        )
        fake = FakeExecutor(self.artifacts, [row])
        self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(self.clock.sleeps, [4.0])

    def test_exit_two_algorithm_outcome_is_retained_and_campaign_continues(self) -> None:
        row = _row(1)
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={("scored", row.sequence, "U0"): "NO_INITIALIZATION"},
        )
        result = self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(result["results"]["1:U0"], "NO_INITIALIZATION")
        self.assertEqual(result["results"]["1:S1"], "COMPLETED")
        self.assertEqual(fake.kinds, ["trial", "trial"])

    def test_infrastructure_result_stops_before_second_system(self) -> None:
        row = _row(1)
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={("scored", row.sequence, "U0"): "INFRASTRUCTURE_FAILED"},
        )
        with self.assertRaisesRegex(campaign.CampaignError, "fatal runner status"):
            self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(fake.trial_count, 1)

    def test_invalid_output_stops_even_when_evidence_observation_is_valid(self) -> None:
        row = _row(1)
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={("scored", row.sequence, "U0"): "INVALID_OUTPUT"},
        )
        # The fake publisher describes INVALID_OUTPUT as invalid infrastructure;
        # rewrite just that orthogonal evidence classification to VALID and
        # confirm the ambiguous outcome status still stops the campaign.
        original = fake.__call__

        def publish_valid_observation(command: Sequence[str]) -> campaign.CommandResult:
            result = original(command)
            if "--protocol-id" in command:
                system = FakeExecutor._argument(command, "--system")
                if system == "U0":
                    location = campaign.run_location(self.artifacts, "scored", row, system)
                    value = json.loads(location.result_path.read_text(encoding="utf-8"))
                    value["evidence_validity"] = "VALID"
                    _write_json(location.result_path, value)
                    (location.run_directory / "SHA256SUMS").unlink()
                    _publish_checksums(location.run_directory, "sequence_result.json")
            return result

        driver = campaign.Campaign(
            self.paths,
            self._options(),
            [row],
            executor=publish_valid_observation,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            wall_time_fn=self.clock.wall_time,
            git_validator=self._git_validator,
        )
        with self.assertRaisesRegex(campaign.CampaignError, "fatal runner status"):
            driver.run(enforce_static=False)
        self.assertEqual(fake.trial_count, 1)

    def test_frozen_row_and_within_pair_order_and_five_second_cooldown(self) -> None:
        rows = [_row(1), _row(2)]
        fake = FakeExecutor(self.artifacts, rows)
        self._campaign(rows, fake).run(enforce_static=False)
        observed = [
            (
                FakeExecutor._argument(call, "--sequence"),
                FakeExecutor._argument(call, "--system"),
            )
            for call in fake.calls
        ]
        self.assertEqual(
            observed,
            [
                ("sequence_1", "U0"),
                ("sequence_1", "S1"),
                ("sequence_2", "S1"),
                ("sequence_2", "U0"),
            ],
        )
        self.assertEqual(self.clock.sleeps, [5.0, 5.0, 5.0])
        ports = [int(FakeExecutor._argument(call, "--ros-port")) for call in fake.calls]
        self.assertEqual(ports, [18100, 18101, 18105, 18104])

    def test_ground_truth_consumers_are_invoked_only_after_both_scored_cells(self) -> None:
        euroc = _row(1, capability="full_trajectory")
        fake = FakeExecutor(self.artifacts, [euroc])
        self._campaign([euroc], fake).run(enforce_static=False)
        self.assertEqual(fake.kinds, ["trial", "trial", "pair"])

        # TUM extraction has the same pair-close boundary, including a partial
        # reference row that is intentionally not sent to the pair evaluator.
        other_root = self.base / "tum-artifacts"
        tum = _row(
            13,
            dataset="tum_vi",
            sequence="dataset-corridor4_512_16",
            capability="embedded_partial_intervals",
        )
        other_fake = FakeExecutor(other_root, [tum])
        options = campaign.CampaignOptions(
            artifact_root=other_root,
            lane="scored",
            dataset="tum_vi",
            expected_tooling_commit="a" * 40,
            expected_tooling_tree="d" * 40,
        )
        self._campaign([tum], other_fake, options).run(enforce_static=False)
        self.assertEqual(other_fake.kinds, ["trial", "trial", "tum_reference"])

    def test_pair_command_has_exactly_one_matrix_binding(self) -> None:
        row = _row(1, capability="full_trajectory")
        command = campaign.build_pair_command(self.paths, self.artifacts, row)
        self.assertEqual(command.count("--matrix"), 1)
        self.assertEqual(command.count(str(self.paths.matrix)), 1)
        matrix_index = command.index("--matrix")
        self.assertEqual(command[matrix_index + 1], str(self.paths.matrix))
        self.assertEqual(command[matrix_index + 2], "--output")

    def test_accuracy_unassessable_pair_is_retained_without_stopping(self) -> None:
        row = _row(1, capability="full_trajectory")
        fake = FakeExecutor(self.artifacts, [row], pair_status="UNASSESSABLE")
        result = self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(fake.kinds, ["trial", "trial", "pair"])
        pair_result = (
            self.artifacts / "pair" / campaign.row_key(row) / "pair_result.json"
        )
        self.assertEqual(json.loads(pair_result.read_text())["status"], "UNASSESSABLE")

        adopted = FakeExecutor(self.artifacts, [row])
        resumed = self._campaign([row], adopted).run(enforce_static=False)
        self.assertEqual(resumed["status"], "COMPLETE")
        self.assertEqual(adopted.calls, [])

    def test_kaist_rotation_c2_failure_is_retained_after_ordinary_pair(self) -> None:
        ground_truth = self.base / "rotation.txt"
        ground_truth.write_text("0 0 0 0 0 0 0 1\n", encoding="ascii")
        base = _row(
            19,
            dataset="kaist_vio",
            sequence="rotation/rotation.bag",
            capability="full_trajectory",
        )
        row = campaign.MatrixRow(
            order=base.order,
            dataset=base.dataset,
            sequence=base.sequence,
            system_order=base.system_order,
            bag_start_seconds=base.bag_start_seconds,
            bag=base.bag,
            ground_truth={
                **dict(base.ground_truth),
                "canonical_path": str(ground_truth),
                "bytes": ground_truth.stat().st_size,
                "sha256": _digest(ground_truth.read_bytes()),
            },
        )
        fake = FakeExecutor(
            self.artifacts, [row], mechanism_status="C2_VALIDATION_FAILURE"
        )
        options = self._options(dataset="kaist_vio", start=19, end=19)
        result = self._campaign([row], fake, options).run(enforce_static=False)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(fake.kinds, ["trial", "trial", "pair", "kaist_mechanism"])
        mechanism = (
            self.artifacts
            / "mechanism"
            / campaign.row_key(row)
            / "kaist_rotation_post_pair.json"
        )
        self.assertEqual(json.loads(mechanism.read_text())["status"], "C2_VALIDATION_FAILURE")

    def test_kaist_rotation_mechanism_runs_when_u0_accuracy_is_ineligible(self) -> None:
        ground_truth = self.base / "rotation-partial-u0.txt"
        ground_truth.write_text("0 0 0 0 0 0 0 1\n", encoding="ascii")
        base = _row(
            19,
            dataset="kaist_vio",
            sequence="rotation/rotation.bag",
            capability="full_trajectory",
        )
        row = campaign.MatrixRow(
            order=base.order,
            dataset=base.dataset,
            sequence=base.sequence,
            system_order=base.system_order,
            bag_start_seconds=base.bag_start_seconds,
            bag=base.bag,
            ground_truth={
                **dict(base.ground_truth),
                "canonical_path": str(ground_truth),
                "bytes": ground_truth.stat().st_size,
                "sha256": _digest(ground_truth.read_bytes()),
            },
        )
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={("scored", row.sequence, "U0"): "PARTIAL"},
            mechanism_status="PASS",
        )
        options = self._options(dataset="kaist_vio", start=19, end=19)
        result = self._campaign([row], fake, options).run(enforce_static=False)
        self.assertEqual(result["results"]["19:U0"], "PARTIAL")
        self.assertEqual(fake.kinds, ["trial", "trial", "kaist_mechanism"])

    def test_early_kaist_c2_failure_continues_without_opening_gt(self) -> None:
        missing_ground_truth = self.base / "must-not-be-opened-rotation.txt"
        base = _row(
            19,
            dataset="kaist_vio",
            sequence="rotation/rotation.bag",
            capability="full_trajectory",
        )
        row = campaign.MatrixRow(
            order=base.order,
            dataset=base.dataset,
            sequence=base.sequence,
            system_order=base.system_order,
            bag_start_seconds=base.bag_start_seconds,
            bag=base.bag,
            ground_truth={
                **dict(base.ground_truth),
                "canonical_path": str(missing_ground_truth),
                "bytes": 987654,
                "sha256": "d" * 64,
                "format": "tum",
            },
        )
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={
                ("scored", row.sequence, "S1"): "NO_INITIALIZATION",
            },
            mechanism_status="C2_VALIDATION_FAILURE",
            mechanism_ground_truth_opened=False,
        )
        options = self._options(dataset="kaist_vio", start=19, end=19)
        result = self._campaign([row], fake, options).run(enforce_static=False)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["results"]["19:S1"], "NO_INITIALIZATION")
        self.assertEqual(fake.kinds, ["trial", "trial", "kaist_mechanism"])
        self.assertFalse(missing_ground_truth.exists())
        mechanism = (
            self.artifacts
            / "mechanism"
            / campaign.row_key(row)
            / "kaist_rotation_post_pair.json"
        )
        value = json.loads(mechanism.read_text(encoding="utf-8"))
        self.assertFalse(value["ground_truth_opened"])
        self.assertIsNone(value["matrix_binding"]["ground_truth"])
        self.assertEqual(
            value["c2_prerequisite"]["reason_code"],
            "S1_MECHANISM_CONTRACT_FAILED",
        )

    def test_ambiguous_existing_run_directory_is_never_overwritten(self) -> None:
        row = _row(1)
        location = campaign.run_location(self.artifacts, "scored", row, "U0")
        location.run_directory.mkdir(parents=True)
        (location.run_directory / "leftover.txt").write_text("ambiguous", encoding="utf-8")
        fake = FakeExecutor(self.artifacts, [row])
        with self.assertRaisesRegex(campaign.CampaignError, "ambiguous incomplete"):
            self._campaign([row], fake).run(enforce_static=False)
        self.assertEqual(fake.calls, [])

    def test_checksummed_symlink_member_is_rejected(self) -> None:
        row = _row(1)
        result_path = _publish_sequence_result(
            self.artifacts,
            "scored",
            row,
            "U0",
            runner_path=self.paths.generic_runner,
        )
        target = result_path.parent / "target.txt"
        target.write_text("target\n", encoding="ascii")
        (result_path.parent / "linked.txt").symlink_to(target.name)
        (result_path.parent / "SHA256SUMS").unlink()
        _publish_checksums(result_path.parent, "sequence_result.json")
        with self.assertRaisesRegex(campaign.CampaignError, "contains a symlink"):
            campaign.validate_sequence_result(
                result_path,
                row,
                "U0",
                "scored",
                expected_runner=self.paths.generic_runner,
            )

    def test_capture_requires_full_scored_lane_and_links_exact_result(self) -> None:
        rows = [_row(1), _row(2)]
        # Selecting only row 1 must still be blocked by missing row-2 scored
        # cells, because the protocol freezes all 50 scored cells first.
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                rows[0],
                system,
                runner_path=self.paths.generic_runner,
            )
        blocked_fake = FakeExecutor(self.artifacts, rows)
        options = self._options(lane="capture", start=1, end=1)
        with self.assertRaisesRegex(campaign.CampaignError, "all 50 scored cells close"):
            self._campaign(rows, blocked_fake, options).run(enforce_static=False)
        self.assertEqual(blocked_fake.calls, [])

        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                rows[1],
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(self.artifacts, rows)
        result = self._campaign(rows, fake, options).run(enforce_static=False)
        self.assertEqual(result["capture_prerequisite_count"], 4)
        trials = [call for call in fake.calls if "--protocol-id" in call]
        self.assertEqual(len(trials), 2)
        for command in trials:
            system = FakeExecutor._argument(command, "--system")
            expected = campaign.run_location(self.artifacts, "scored", rows[0], system).result_path
            self.assertEqual(Path(FakeExecutor._argument(command, "--scored-result")), expected)
        self.assertEqual(fake.kinds, ["trial", "trial", "geometry", "geometry"])

    def test_capture_linkage_failure_is_retained_bundled_and_campaign_continues(self) -> None:
        row = _row(1)
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                row,
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={
                ("capture", row.sequence, "U0"): "NO_INITIALIZATION",
                ("capture", row.sequence, "S1"): "PARTIAL",
            },
            invalid_capture_linkage=True,
        )
        options = self._options(lane="capture")
        result = self._campaign([row], fake, options).run(enforce_static=False)
        self.assertEqual(result["results"]["1:U0"], "NO_INITIALIZATION")
        self.assertEqual(result["results"]["1:S1"], "PARTIAL")
        self.assertEqual(fake.kinds, ["trial", "trial", "geometry", "geometry"])
        for system in campaign.SYSTEMS:
            manifest = (
                self.artifacts
                / "geometry"
                / campaign.row_key(row)
                / system
                / "qualitative_manifest.json"
            )
            value = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(value["status"], "FAILURE_EVIDENCE_COMPLETE")
            self.assertEqual(value["bundle_mode"], "FAILURE_TILE_ONLY")

    def test_capture_mismatch_with_noncanonical_scored_link_is_fatal(self) -> None:
        row = _row(1)
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                row,
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(self.artifacts, [row], invalid_capture_linkage=True)

        def tamper(command: Sequence[str]) -> campaign.CommandResult:
            result = fake(command)
            if "--protocol-id" in command:
                system = FakeExecutor._argument(command, "--system")
                location = campaign.run_location(self.artifacts, "capture", row, system)
                value = json.loads(location.result_path.read_text(encoding="utf-8"))
                canonical = value["scored_linkage"]["sequence_result_path"]
                value["scored_linkage"]["sequence_result_path"] = str(
                    Path(canonical).parent / ".." / Path(canonical).parent.name / "sequence_result.json"
                )
                _write_json(location.result_path, value)
                (location.run_directory / "SHA256SUMS").unlink()
                _publish_checksums(location.run_directory, "sequence_result.json")
            return result

        driver = campaign.Campaign(
            self.paths,
            self._options(lane="capture"),
            [row],
            executor=tamper,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            wall_time_fn=self.clock.wall_time,
            git_validator=self._git_validator,
        )
        with self.assertRaisesRegex(campaign.CampaignError, "exact canonical scored result"):
            driver.run(enforce_static=False)
        self.assertEqual(fake.kinds, ["trial"])

    def test_capture_mismatch_with_scored_source_drift_is_fatal(self) -> None:
        row = _row(1)
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                row,
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(self.artifacts, [row], invalid_capture_linkage=True)

        def tamper(command: Sequence[str]) -> campaign.CommandResult:
            result = fake(command)
            if "--protocol-id" in command:
                system = FakeExecutor._argument(command, "--system")
                location = campaign.run_location(self.artifacts, "capture", row, system)
                value = json.loads(location.result_path.read_text(encoding="utf-8"))
                value["scored_linkage"]["source_unchanged_during_capture"] = False
                _write_json(location.result_path, value)
                (location.run_directory / "SHA256SUMS").unlink()
                _publish_checksums(location.run_directory, "sequence_result.json")
            return result

        driver = campaign.Campaign(
            self.paths,
            self._options(lane="capture"),
            [row],
            executor=tamper,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            wall_time_fn=self.clock.wall_time,
            git_validator=self._git_validator,
        )
        with self.assertRaisesRegex(campaign.CampaignError, "scored source changed"):
            driver.run(enforce_static=False)
        self.assertEqual(fake.kinds, ["trial"])

    def test_capture_mismatch_with_untruthful_comparison_is_fatal(self) -> None:
        row = _row(1)
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                row,
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(self.artifacts, [row], invalid_capture_linkage=True)

        def tamper(command: Sequence[str]) -> campaign.CommandResult:
            result = fake(command)
            if "--protocol-id" in command:
                system = FakeExecutor._argument(command, "--system")
                location = campaign.run_location(self.artifacts, "capture", row, system)
                value = json.loads(location.result_path.read_text(encoding="utf-8"))
                value["scored_linkage"]["artifact_comparisons"]["state"]["byte_exact"] = True
                _write_json(location.result_path, value)
                (location.run_directory / "SHA256SUMS").unlink()
                _publish_checksums(location.run_directory, "sequence_result.json")
            return result

        driver = campaign.Campaign(
            self.paths,
            self._options(lane="capture"),
            [row],
            executor=tamper,
            sleep_fn=self.clock.sleep,
            monotonic_fn=self.clock.monotonic,
            wall_time_fn=self.clock.wall_time,
            git_validator=self._git_validator,
        )
        with self.assertRaisesRegex(campaign.CampaignError, "comparison is not truthful"):
            driver.run(enforce_static=False)
        self.assertEqual(fake.kinds, ["trial"])

    def test_noneligible_capture_without_closed_raw_geometry_stops(self) -> None:
        row = _row(1)
        for system in campaign.SYSTEMS:
            _publish_sequence_result(
                self.artifacts,
                "scored",
                row,
                system,
                runner_path=self.paths.generic_runner,
            )
        fake = FakeExecutor(
            self.artifacts,
            [row],
            statuses={("capture", row.sequence, "U0"): "NO_INITIALIZATION"},
            omit_capture_geometry=True,
        )
        with self.assertRaisesRegex(campaign.CampaignError, "closed raw geometry bag"):
            self._campaign([row], fake, self._options(lane="capture")).run(
                enforce_static=False
            )
        self.assertEqual(fake.kinds, ["trial"])


if __name__ == "__main__":
    unittest.main()
