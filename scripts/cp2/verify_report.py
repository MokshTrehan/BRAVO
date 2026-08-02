#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble and independently verify SchurVIO-Lite CP2-A/B unit evidence."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import xml.etree.ElementTree as ET


REPORT_NAME = "cp2_report.json"
MANIFEST_NAME = "SHA256SUMS"
EXPECTED_BRANCH = "schurvio-lite/cp2-one-pass"
EVIDENCE_SCOPE = "cp2_a_b_unit_math_with_complete_cp1_regression"
OVERALL_STATUS = "in_progress_cp2_c_cp2_d_cp2_e_unexecuted"
CP1_AUTHORIZATION_COMMIT = "8d80f483752411d34a3bc4c1ff6330b3a5c0fef3"
CERES_COMMIT = "facb199f3eda902360f9e1d5271372b7e54febe1"
CERES_TAG = "1.14.0"
CERES_LICENSE_SHA256 = "065e9b9f40b65dfaeb421a8a1c0559d8305e3ce9394aa8b7dec609fa04e8318a"
GOOGLETEST_LICENSE_SHA256 = "9702de7e4117a8e2b20dafab11ffda58c198aede066406496bef670d40a22138"
GOOGLETEST_ARCHIVE_SHA256 = "53d536bbe4f5a4007a23ac1abdd58946fe0f0f30e70c2ddd24fd084a789a9b63"
GOOGLETEST_ARCHIVE_SIZE_BYTES = 4454400

CP1_TESTS = {
    "test_cp1_schur_equivalence": 1,
    "test_cp1_rank_rejection": 2,
    "test_cp1_projection_jacobian": 2,
    "test_cp1_prior_and_compression": 2,
}
CP2_TESTS = {
    "test_cp2_production_schur_reducer": 5,
    "test_cp2_fej_golden": 1,
    "test_cp2_state_update_semantics": 2,
    "test_cp2_configuration_contract": 10,
    "test_cp2_updater_msckf_end_to_end": 8,
    "test_cp2_canonical": 5,
    "test_cp2_feature_gate": 13,
    "test_cp2_updater_msckf_preview_snapshot": 4,
    "test_cp2_shadow_math": 14,
    "test_cp2_trace_codec": 8,
}
ALL_TESTS = dict(CP1_TESTS)
ALL_TESTS.update(CP2_TESTS)

TEST_SOURCE_BY_BINARY = {
    "test_cp1_schur_equivalence": "ov_msckf/test/cp1/test_schur_equivalence.cpp",
    "test_cp1_rank_rejection": "ov_msckf/test/cp1/test_rank_rejection.cpp",
    "test_cp1_projection_jacobian": "ov_msckf/test/cp1/test_projection_jacobian.cpp",
    "test_cp1_prior_and_compression": "ov_msckf/test/cp1/test_prior_and_compression.cpp",
    "test_cp2_production_schur_reducer": "ov_msckf/test/cp2/test_production_schur_reducer.cpp",
    "test_cp2_fej_golden": "ov_msckf/test/cp2/test_fej_golden.cpp",
    "test_cp2_state_update_semantics": "ov_msckf/test/cp2/test_state_update_semantics.cpp",
    "test_cp2_configuration_contract": "ov_msckf/test/cp2/test_configuration_contract.cpp",
    "test_cp2_updater_msckf_end_to_end": "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp",
    "test_cp2_canonical": "ov_msckf/test/cp2/test_cp2_canonical.cpp",
    "test_cp2_feature_gate": "ov_msckf/test/cp2/test_cp2_feature_gate.cpp",
    "test_cp2_updater_msckf_preview_snapshot": (
        "ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp"
    ),
    "test_cp2_shadow_math": "ov_msckf/test/cp2/test_cp2_shadow_math.cpp",
    "test_cp2_trace_codec": "ov_msckf/test/cp2/test_cp2_trace_codec.cpp",
}

EXPECTED_TEST_CASES = {
    "CP1Compression.ProductionTruncationPreservesLambdaEtaButNotGamma",
    "CP1Prior.SemidefiniteCloneAugmentationMatchesInnovationUpdate",
    "CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences",
    "CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction",
    "CP1Schur.BoundaryAndInvalidInputsHaveExplicitStatus",
    "CP1Schur.DegenerateLandmarksAreRejectedDeterministically",
    "CP1Schur.FullJointNullspaceAndReducedSystemsAgree",
    "CP2Configuration.InvalidEnumFailsStartupWithoutSelectingAStringMode",
    "CP2Configuration.InvalidSpellingsCannotMutateASelectedModeOrFallBack",
    "CP2Configuration.NonfiniteAndNonpositiveSigmaFailBeforeVarianceMaterialization",
    "CP2Configuration.NonfiniteChi2MultiplierFailsEveryStartupSeam",
    "CP2Configuration.OptionalModeUsesDefaultAndExactExplicitSpellings",
    "CP2Configuration.PositiveRepresentableVarianceIsMaterializedExactly",
    "CP2Configuration.RuntimeInvalidSigmaNeverInvokesRepairOrSilentFallback",
    "CP2Configuration.SchurRequiresGlobal3DWhileNullspaceRetainsExistingRepresentations",
    "CP2Configuration.UnderflowingAndOverflowingVarianceFailStartup",
    "CP2Configuration.UnknownAndFixedTwoPassSpellingsFailStartup",
    "CP2FejGolden.MixedCurrentAndFirstEstimateProductionJacobianIsFrozen",
    "CP2ProductionSchurReducer.DeterministicGivensStatisticsNisAndPosteriorParity",
    "CP2ProductionSchurReducer.DeterministicRejectedCorpusHasNoPublishedOutputs",
    "CP2ProductionSchurReducer.OrderedValidityAndRankBoundariesAreExact",
    "CP2ProductionSchurReducer.ReducedOutputAndStatisticsOverflowRejectAtomically",
    "CP2ProductionSchurReducer.StrictNisDecisionBoundaryContract",
    "CP2StateUpdateSemantics.ClonePreviewCompressionAndLiveCommitParity",
    "CP2StateUpdateSemantics.InvalidPreviewInputsAreBitwiseReadOnlyAndNeverRepaired",
    "CP2UpdaterMSCKFEndToEnd.ActualNullspaceAndSchurModesCommitEquivalentFullStateUpdates",
    "CP2UpdaterMSCKFEndToEnd.SelectedReducersRejectNonfiniteProductionRowsWithoutSilentFallback",
    "CP2UpdaterMSCKFEndToEnd.SharedInvalidPreflightLeavesBothModeStatesBitwiseUnchanged",
    "CP2UpdaterMSCKFEndToEnd.NullspaceShadowPublishesBothPrecommitProposalsThenOneCommittedEvent",
    "CP2UpdaterMSCKFEndToEnd.NonfiniteGammaEvidenceCannotStopLiveTraversalOrBaselineCommit",
    "CP2UpdaterMSCKFEndToEnd.SchurModeRejectsShadowEnableWithoutReplacingExistingObserver",
    "CP2UpdaterMSCKFEndToEnd.ObserverExceptionCannotVetoAnAcceptedBaselineCommit",
    "CP2UpdaterMSCKFEndToEnd.AllRejectedRawSystemHasExactTerminalTaxonomyAndNoBaselineWrite",
    "CP2CanonicalSha256.MatchesPublishedVectorsUnderIncrementalChunking",
    "CP2CanonicalBytes.IntegerBinary64AndUtf8EncodingIsExact",
    "CP2CanonicalBytes.MatrixAndVectorUseLogicalRowMajorBinary64Order",
    "CP2CanonicalBytes.Utf8ValidationRejectsMalformedSequencesWithoutAppending",
    "CP2CanonicalBytes.SelfAppendStagesAliasedStorageBeforeGrowth",
    "CP2FeatureGate.StageNamesAreFrozen",
    "CP2FeatureGate.ReductionUnavailablePublishesNoNumericOrDecisionEvidence",
    "CP2FeatureGate.GammaOverflowCanInvalidateEvidenceWithoutChangingBaselineLifecycleGate",
    "CP2FeatureGate.EvidenceUnavailableEmittedSystemRetainsTheNormalLifecycleDecision",
    "CP2FeatureGate.MarginalIsCopiedFromImmutablePriorInDeclaredLayoutOrder",
    "CP2FeatureGate.InvalidOrOverlappingLayoutCannotFormInnovation",
    "CP2FeatureGate.NonfiniteInnovationPrecedesFactorization",
    "CP2FeatureGate.NonPositiveDefiniteInnovationFailsDefaultLowerLLT",
    "CP2FeatureGate.NonfiniteDotIsSolveOrNISFailure",
    "CP2FeatureGate.EqualityIsAcceptedAndStrictExcessRejected",
    "CP2FeatureGate.NonfiniteThresholdNullsEvidenceButPreservesIEEEComparison",
    "CP2FeatureGate.MissingConstructorTableEntryIsUnavailableNotSubstituted",
    "CP2FeatureGate.FiveHundredRowsUseDynamicBoostQuantile",
    "CP2PreviewSnapshot.ExactAdapterParityAndInputImmutability",
    "CP2PreviewSnapshot.RejectsMalformedOwningStateLayout",
    "CP2PreviewSnapshot.RejectsMalformedValueOnlyJacobianLayout",
    "CP2PreviewSnapshot.FiniteAndLLTBoundariesAreOrdered",
    "CP2ShadowMath.FullRankPathsAgreeAndProduceIndependentGlobalProposals",
    "CP2ShadowMath.CandidateRankFailureIsAOneSidedGateAttempt",
    "CP2ShadowMath.DuplicateFeatureIdentityInvalidatesButDoesNotShortCircuitMath",
    "CP2ShadowMath.RawAndPriorLayoutDisconnectsAreStructurallyInvalid",
    "CP2ShadowMath.NullspaceStatisticsFailureCannotChangeLiveLifecycleAcceptance",
    "CP2ShadowMath.CandidateGammaOverflowSuppressesOnlyCandidateProposal",
    "CP2ShadowMath.LambdaDiagnosticOverflowCannotRemoveBaselineGateAttempt",
    "CP2ShadowMath.WhitenedJacobianOverflowCannotRemoveFiniteGammaBaselineGate",
    "CP2ShadowMath.NonfiniteWhitenedResidualStopsDiagnosticPipelineInOrder",
    "CP2ShadowMath.RawLambdaOverflowStopsEtaButFiniteGammaDefinesModeValidity",
    "CP2ShadowMath.DifferentLocalBlockOrdersUseFirstSeenGlobalLayoutExactly",
    "CP2ShadowMath.CandidateGammaOverflowStillTraversesAndStacksLaterFeatures",
    "CP2ShadowMath.EmptyAndAllRejectedGammaStatesAreExact",
    "CP2ShadowMath.StatisticComparisonFirstFailurePrecedenceIsExact",
    "CP2TraceRawPayload.FrozenDomainRowMajorBitsAndLayoutRoundTripExactly",
    "CP2TraceRawPayload.CorruptionAndAllocationBoundsFailClosed",
    "CP2TraceAcceptedDigests.FrozenSetAndSequenceDomainsAreIndependent",
    "CP2TraceRawFile.CompleteFrameAndHeaderMatchIndependentKnownAnswer",
    "CP2TraceRawFile.FrameContextOffsetsOrderingAndFeatureIdentityAreExact",
    "CP2TraceProposal.FrozenPayloadAndRoleFramingRejectAllStructuralCorruption",
    "CP2TraceReplay.OwningDecodedFramesDriveTheSoleShadowMathKernel",
    "CP2TraceReplay.ContextDigestAndPriorLayoutDisconnectsFailBeforeMath",
}

EXPECTED_SUMMARIES = {
    "CP1_COMPRESSION",
    "CP1_EQUIVALENCE",
    "CP1_PROJECTION",
    "CP1_PSD_PRIOR",
    "CP1_RANK",
    "CP1_RETRACTION",
    "CP2_A_ACCEPTED",
    "CP2_A_REJECTED",
    "CP2_B_PREVIEW_REJECTION",
    "CP2_B_STATE_UPDATE",
}

SUMMARIES_BY_BINARY = {
    "test_cp1_schur_equivalence": {"CP1_EQUIVALENCE"},
    "test_cp1_rank_rejection": {"CP1_RANK"},
    "test_cp1_projection_jacobian": {"CP1_PROJECTION", "CP1_RETRACTION"},
    "test_cp1_prior_and_compression": {"CP1_COMPRESSION", "CP1_PSD_PRIOR"},
    "test_cp2_production_schur_reducer": {"CP2_A_ACCEPTED", "CP2_A_REJECTED"},
    "test_cp2_fej_golden": set(),
    "test_cp2_state_update_semantics": {"CP2_B_PREVIEW_REJECTION", "CP2_B_STATE_UPDATE"},
    "test_cp2_configuration_contract": set(),
    "test_cp2_updater_msckf_end_to_end": set(),
    "test_cp2_canonical": set(),
    "test_cp2_feature_gate": set(),
    "test_cp2_updater_msckf_preview_snapshot": set(),
    "test_cp2_shadow_math": set(),
    "test_cp2_trace_codec": set(),
}

SOURCE_INPUTS = {
    "LICENSE",
    "config/euroc_mav/estimator_config.yaml",
    "config/euroc_mav/kalibr_imu_chain.yaml",
    "config/euroc_mav/kalibr_imucam_chain.yaml",
    "docs/checkpoints.md",
    "docs/conventions.md",
    "docs/cp2_artifact_schema.md",
    "docs/cp2_math_implementation_audit.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "docs/schurvio_lite_execution_plan.md",
    "ov_core/CMakeLists.txt",
    "ov_core/src/cam/CamBase.h",
    "ov_core/src/cam/CamRadtan.h",
    "ov_core/src/types/JPLQuat.h",
    "ov_core/src/types/PoseJPL.h",
    "ov_core/src/utils/quat_ops.h",
    "ov_init/CMakeLists.txt",
    "ov_msckf/CMakeLists.txt",
    "ov_msckf/cmake/CP2Tests.cmake",
    "ov_msckf/cmake/ROS1.cmake",
    "ov_msckf/cmake/ROS2.cmake",
    "ov_msckf/package.xml",
    "ov_msckf/src/core/VioManagerOptions.h",
    "ov_msckf/src/state/State.cpp",
    "ov_msckf/src/state/State.h",
    "ov_msckf/src/state/StateHelper.cpp",
    "ov_msckf/src/state/StateHelper.h",
    "ov_msckf/src/update/CP2Canonical.cpp",
    "ov_msckf/src/update/CP2Canonical.h",
    "ov_msckf/src/update/CP2FeatureGate.cpp",
    "ov_msckf/src/update/CP2FeatureGate.h",
    "ov_msckf/src/update/CP2ShadowMath.cpp",
    "ov_msckf/src/update/CP2ShadowMath.h",
    "ov_msckf/src/update/CP2TraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceCodec.h",
    "ov_msckf/src/update/SchurUpdate.cpp",
    "ov_msckf/src/update/SchurUpdate.h",
    "ov_msckf/src/update/UpdaterHelper.cpp",
    "ov_msckf/src/update/UpdaterHelper.h",
    "ov_msckf/src/update/UpdaterMSCKF.cpp",
    "ov_msckf/src/update/UpdaterMSCKF.h",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp",
    "ov_msckf/src/update/UpdaterMSCKFPreview.h",
    "ov_msckf/src/update/UpdaterOptions.h",
    "ov_msckf/test/cp1/cp1_fixture_utils.h",
    "ov_msckf/test/cp1/gtest_main.cpp",
    "ov_msckf/test/cp1/test_prior_and_compression.cpp",
    "ov_msckf/test/cp1/test_projection_jacobian.cpp",
    "ov_msckf/test/cp1/test_rank_rejection.cpp",
    "ov_msckf/test/cp1/test_schur_equivalence.cpp",
    "ov_msckf/test/cp2/gtest_main.cpp",
    "ov_msckf/test/cp2/test_configuration_contract.cpp",
    "ov_msckf/test/cp2/test_cp2_canonical.cpp",
    "ov_msckf/test/cp2/test_cp2_feature_gate.cpp",
    "ov_msckf/test/cp2/test_cp2_shadow_math.cpp",
    "ov_msckf/test/cp2/test_cp2_trace_codec.cpp",
    "ov_msckf/test/cp2/test_fej_golden.cpp",
    "ov_msckf/test/cp2/test_production_schur_reducer.cpp",
    "ov_msckf/test/cp2/test_state_update_semantics.cpp",
    "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp",
    "ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp",
    "project/cp0_baseline.json",
    "project/cp1_gate.yaml",
    "project/cp2_gate.yaml",
    "project/cp2_serial.launch",
    "scripts/cp0/bootstrap_ceres_1_14.sh",
    "scripts/cp1/make_report.py",
    "scripts/cp1/run_cp1.sh",
    "scripts/cp1/verify_report.py",
    "scripts/cp2/run_unit_gate.sh",
    "scripts/cp2/verify_report.py",
}

CONTRACT_INPUTS = {
    "docs/cp2_artifact_schema.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "project/cp1_gate.yaml",
    "project/cp2_gate.yaml",
}
CONFIG_INPUTS = {
    "config/euroc_mav/estimator_config.yaml",
    "config/euroc_mav/kalibr_imu_chain.yaml",
    "config/euroc_mav/kalibr_imucam_chain.yaml",
}
FROZEN_CONFIG_SHA256 = {
    "config/euroc_mav/estimator_config.yaml":
        "b706f0082106e49e20c3292147d238b7e225b0df414106b9d4ac009bbb123f3b",
    "config/euroc_mav/kalibr_imu_chain.yaml":
        "408ea8b60b5f9e7c8251e6d302f04c0675bfefdd31229bb1afd139bc8f4a0287",
    "config/euroc_mav/kalibr_imucam_chain.yaml":
        "b9e11b7bcda102f7c8c384c97318d67f3916b58942f9073722f83c22bd7073f7",
}

BUILD_STEPS = (
    "ceres_configure",
    "ceres_build",
    "ceres_install",
    "catkin_config",
    "catkin_build",
    "test_targets_build",
)
STRICT_TARGETS = set(ALL_TESTS)
STRICT_PRODUCTION_SOURCES = (
    "ov_msckf/src/update/SchurUpdate.cpp",
    "ov_msckf/src/update/CP2Canonical.cpp",
    "ov_msckf/src/update/CP2FeatureGate.cpp",
    "ov_msckf/src/update/CP2ShadowMath.cpp",
    "ov_msckf/src/update/CP2TraceCodec.cpp",
    "ov_msckf/src/update/UpdaterHelper.cpp",
    "ov_msckf/src/update/UpdaterMSCKF.cpp",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp",
    "ov_msckf/src/state/StateHelper.cpp",
)
STRICT_REQUIRED_FLAGS = ("-fno-fast-math", "-ffp-contract=off", "-fsigned-zeros")
STRICT_REQUIRED_MACRO_DEFINITIONS = {
    "EIGEN_DONT_VECTORIZE": (
        "-DEIGEN_DONT_VECTORIZE",
        "-DEIGEN_DONT_VECTORIZE=1",
    ),
    "EIGEN_MAX_ALIGN_BYTES": ("-DEIGEN_MAX_ALIGN_BYTES=16",),
    "EIGEN_MAX_STATIC_ALIGN_BYTES": ("-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",),
}
DEPENDENCY_COMPILE_COMMAND_ARTIFACTS = {
    "ov_core": "compile_commands_ov_core.json",
    "ov_init": "compile_commands_ov_init.json",
}
DEPENDENCY_EXPECTED_COMMAND_COUNTS = {
    "ov_core": 21,
    "ov_init": 16,
}
DEPENDENCY_REQUIRED_MACRO_DEFINITIONS = {
    "EIGEN_DONT_VECTORIZE": "-DEIGEN_DONT_VECTORIZE=1",
    "EIGEN_MAX_ALIGN_BYTES": "-DEIGEN_MAX_ALIGN_BYTES=16",
    "EIGEN_MAX_STATIC_ALIGN_BYTES": "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",
}
STRICT_FORBIDDEN_FLAGS = {
    "-Ofast",
    "-fassociative-math",
    "-fcx-limited-range",
    "-ffast-math",
    "-ffinite-math-only",
    "-fno-rounding-math",
    "-fno-signaling-nans",
    "-fno-trapping-math",
    "-freciprocal-math",
    "-funsafe-math-optimizations",
}
SNAPSHOTTED_LIBRARY_ORDER = [
    "libov_msckf_lib.so",
    "libov_core_lib.so",
    "libov_init_lib.so",
    "libgtest.so",
    "libceres.so.1",
]
SNAPSHOTTED_LIBRARIES = {
    "libceres.so.1",
    "libgtest.so",
    "libov_core_lib.so",
    "libov_init_lib.so",
    "libov_msckf_lib.so",
}
TESTS_REQUIRING_PRODUCTION = set(ALL_TESTS) - {
    "test_cp1_rank_rejection",
    "test_cp1_schur_equivalence",
}
ARCHIVE_ROOTS = [
    "LICENSE",
    "ov_core",
    "ov_init",
    "ov_msckf",
    "scripts",
    "config/euroc_mav",
    "docs/checkpoints.md",
    "docs/conventions.md",
    "docs/cp2_artifact_schema.md",
    "docs/cp2_math_implementation_audit.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "docs/schurvio_lite_execution_plan.md",
    "project/cp0_baseline.json",
    "project/cp1_gate.yaml",
    "project/cp2_gate.yaml",
    "project/cp2_serial.launch",
]
WORKSPACE_RECORD_NAME = "workspace.json"
DEPENDENCY_INVENTORY_NAME = "dependency_inventory.json"
SOURCE_ARCHIVE_NAME = "source_snapshot.tar"
CATKIN_PACKAGE_CMAKE_LOG_NAME = "catkin_ov_msckf_cmake.log"
GOOGLETEST_DISCOVERY_PREFIX = "Found gtest sources under"
GOOGLETEST_DISCOVERY_LINE = (
    "-- Found gtest sources under '/usr/src/googletest': gtests will be built"
)
ANSI_SGR_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
CONTROLLED_TEST_ENVIRONMENT = {
    "CPATH": "unset",
    "CPLUS_INCLUDE_PATH": "unset",
    "GTEST_ALSO_RUN_DISABLED_TESTS": "unset",
    "GTEST_BREAK_ON_FAILURE": "unset",
    "GTEST_BRIEF": "unset",
    "GTEST_CATCH_EXCEPTIONS": "unset",
    "GTEST_COLOR": "unset",
    "GTEST_FAIL_FAST": "unset",
    "GTEST_FILTER": "unset",
    "GTEST_LIST_TESTS": "unset",
    "GTEST_OUTPUT": "unset",
    "GTEST_PRINT_TIME": "unset",
    "GTEST_RANDOM_SEED": "unset",
    "GTEST_REPEAT": "unset",
    "GTEST_SHARD_INDEX": "unset",
    "GTEST_SHUFFLE": "unset",
    "GTEST_THROW_ON_FAILURE": "unset",
    "GTEST_TOTAL_SHARDS": "unset",
    "LD_PRELOAD": "unset",
    "LIBRARY_PATH": "unset",
    "PYTHONPATH": "unset",
    "LANG": "C",
    "LC_ALL": "C",
    "LD_LIBRARY_PATH": "binaries:/opt/ros/noetic/lib",
    "MKL_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "PATH": "/usr/bin:/bin",
    "TZ": "UTC",
}
GTEST_OPTIONS = [
    "--gtest_color=no",
    "--gtest_filter=*",
    "--gtest_repeat=1",
    "--gtest_shuffle=0",
]
MAX_ARTIFACT_FILE_BYTES = 512 * 1024 * 1024
SUMMARY_PATTERN = re.compile(r"^(CP[12]_[A-Z][A-Z0-9_]*)\s+(.*)$")
HEX64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
HEX40_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(content):
    return hashlib.sha256(content).hexdigest()


def atomic_write_bytes(path, content):
    if path.exists():
        raise ValueError("refusing to overwrite: " + str(path))
    temporary = path.with_name("." + path.name + ".tmp." + str(os.getpid()))
    with temporary.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))


def atomic_write_json(path, value):
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    atomic_write_bytes(path, content)


def git_text(repo_root, *args):
    return subprocess.check_output(
        ["git", *args], cwd=str(repo_root), text=True, stderr=subprocess.STDOUT
    ).rstrip("\n")


def committed_blob(repo_root, commit, relative):
    return subprocess.check_output(
        ["git", "show", commit + ":" + relative], cwd=str(repo_root), stderr=subprocess.STDOUT
    )


def read_json(path, errors, label=None):
    label = label or str(path)
    if not path.is_file():
        errors.append("missing " + label)
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append("cannot parse {}: {}".format(label, exc))
        return {}
    if not isinstance(value, dict):
        errors.append(label + " must contain a JSON object")
        return {}
    return value


def parse_value(text):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def parse_summaries(artifact_dir, errors):
    summaries = {}
    for test_name in ALL_TESTS:
        file_summary_names = set()
        log_path = artifact_dir / (test_name + ".log")
        if not log_path.is_file():
            errors.append("missing test log: " + log_path.name)
            continue
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            errors.append("cannot read {}: {}".format(log_path.name, exc))
            continue
        for line in lines:
            match = SUMMARY_PATTERN.match(line)
            if not match:
                continue
            fields = {}
            for token in match.group(2).split():
                if "=" in token:
                    key, value = token.split("=", 1)
                    if key in fields:
                        errors.append("duplicate summary field: {}.{}".format(match.group(1), key))
                    fields[key] = parse_value(value)
            name = match.group(1)
            file_summary_names.add(name)
            if name not in SUMMARIES_BY_BINARY[test_name]:
                errors.append("{} owns unexpected summary {}".format(test_name, name))
            if name in summaries:
                errors.append("duplicate summary: " + name)
            summaries[name] = fields
        if file_summary_names != SUMMARIES_BY_BINARY[test_name]:
            errors.append(
                "{} summary ownership mismatch: expected {} got {}".format(
                    test_name,
                    sorted(SUMMARIES_BY_BINARY[test_name]),
                    sorted(file_summary_names),
                )
            )
    if set(summaries) != EXPECTED_SUMMARIES:
        errors.append(
            "summary inventory mismatch: expected {} got {}".format(
                sorted(EXPECTED_SUMMARIES), sorted(summaries)
            )
        )
    validate_summary_contract(summaries, errors)
    return summaries


def require_exact(summary, name, expected, errors):
    fields = summary.get(name, {})
    if not isinstance(fields, dict):
        errors.append(name + " summary is not an object")
        return
    for key, expected_value in expected.items():
        if fields.get(key) != expected_value:
            errors.append(
                "{}.{}: expected {!r}, got {!r}".format(name, key, expected_value, fields.get(key))
            )


def require_finite_fields(summary, name, fields, errors, minimum=None, maximum=None):
    values = summary.get(name, {})
    if not isinstance(values, dict):
        return
    for field in fields:
        value = values.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            errors.append("{}.{} is missing or nonfinite".format(name, field))
            continue
        if minimum is not None and value < minimum:
            errors.append("{}.{} is below {}".format(name, field, minimum))
        if maximum is not None and value > maximum:
            errors.append("{}.{} exceeds {}".format(name, field, maximum))


def validate_summary_contract(summaries, errors):
    require_exact(summaries, "CP1_COMPRESSION", {"fixtures": 128, "seed": 443998030361}, errors)
    require_exact(
        summaries,
        "CP1_EQUIVALENCE",
        {"fixtures": 128, "near_column_space_fixtures": 16, "seed": 20260728},
        errors,
    )
    require_exact(summaries, "CP1_PSD_PRIOR", {"fixtures": 128, "seed": 21320732}, errors)
    require_exact(summaries, "CP1_PROJECTION", {"fixtures": 256, "seed": 1900496914}, errors)
    require_exact(
        summaries,
        "CP1_RANK",
        {"fixtures": 128, "rank_deficient": 96, "ill_conditioned": 32, "seed": 1934903571},
        errors,
    )
    require_exact(summaries, "CP1_RETRACTION", {"fixtures": 256, "seed": 32199698170528780}, errors)

    require_exact(
        summaries,
        "CP2_A_ACCEPTED",
        {
            "fixtures": 1024,
            "near_column_space_fixtures": 128,
            "near_conditioning_fixtures": 64,
            "nonunit_sigma_fixtures": 910,
            "min_sigma": 0.125,
            "max_sigma": 12.0,
            "seed": 20260801,
        },
        errors,
    )
    require_exact(
        summaries,
        "CP2_A_REJECTED",
        {
            "fixtures": 128,
            "rank_deficient": 16,
            "ill_conditioned": 16,
            "insufficient_rows": 16,
            "nonfinite": 80,
            "seed": 125779917685941,
        },
        errors,
    )
    require_exact(
        summaries,
        "CP2_B_STATE_UPDATE",
        {
            "fixtures": 128,
            "seed": 4850432059125285204,
            "clone_calls": 256,
            "global_compressions": 256,
            "accepted_previews": 256,
            "live_commits": 256,
            "preview_nominal_block_checks": 1792,
            "preview_covariance_block_checks": 12544,
            "mode_nominal_block_checks": 896,
            "mode_covariance_block_checks": 6272,
        },
        errors,
    )
    require_exact(
        summaries,
        "CP2_B_PREVIEW_REJECTION",
        {
            "cases": 5,
            "seed": 729130154250274049,
            "state_mutations": 0,
            "jitter_count": 0,
            "repair_count": 0,
            "alternate_solve_count": 0,
            "clamp_count": 0,
            "regularization_count": 0,
            "fallback_count": 0,
        },
        errors,
    )

    require_finite_fields(
        summaries,
        "CP1_PROJECTION",
        (
            "max_landmark_normalized_frobenius",
            "max_residual_sign_normalized_frobenius",
            "max_state_normalized_frobenius",
        ),
        errors,
        minimum=0.0,
        maximum=1.0e-5,
    )
    require_finite_fields(
        summaries, "CP1_PROJECTION", ("max_residual_definition_error",), errors,
        minimum=0.0, maximum=1.0e-12
    )
    require_finite_fields(
        summaries, "CP1_EQUIVALENCE", ("max_symmetry_ratio",), errors,
        minimum=0.0, maximum=1.0e-10
    )
    require_finite_fields(
        summaries, "CP1_RETRACTION", ("max_normalized_frobenius",), errors,
        minimum=0.0, maximum=1.0e-7
    )
    min_eigenvalue = summaries.get("CP1_EQUIVALENCE", {}).get("min_normalized_eigenvalue")
    if not isinstance(min_eigenvalue, (int, float)) or not math.isfinite(min_eigenvalue) or min_eigenvalue < -1e-10:
        errors.append("CP1_EQUIVALENCE.min_normalized_eigenvalue violates -1e-10")
    for name, fields in {
        "CP1_COMPRESSION": (
            "max_eta_tolerance_ratio",
            "max_gamma_reconstruction_tolerance_ratio",
            "max_lambda_tolerance_ratio",
        ),
        "CP1_PSD_PRIOR": (
            "max_clone_nullspace_tolerance_ratio",
            "max_known_covariance_tolerance_ratio",
            "max_known_nis_tolerance_ratio",
            "max_known_state_tolerance_ratio",
            "max_spectral_covariance_tolerance_ratio",
            "max_spectral_nis_tolerance_ratio",
            "max_spectral_state_tolerance_ratio",
            "max_zero_eigenvalue_tolerance_ratio",
        ),
    }.items():
        require_finite_fields(summaries, name, fields, errors, minimum=0.0, maximum=1.0)
    discarded = summaries.get("CP1_COMPRESSION", {}).get("min_discarded_energy")
    if not isinstance(discarded, (int, float)) or not math.isfinite(discarded) or discarded <= 0.1:
        errors.append("CP1_COMPRESSION.min_discarded_energy must exceed 0.1")
    posterior_eigenvalue = summaries.get("CP1_PSD_PRIOR", {}).get("min_posterior_normalized_eigenvalue")
    if (
        not isinstance(posterior_eigenvalue, (int, float))
        or not math.isfinite(posterior_eigenvalue)
        or posterior_eigenvalue < -1e-10
    ):
        errors.append("CP1_PSD_PRIOR.min_posterior_normalized_eigenvalue violates -1e-10")

    require_finite_fields(
        summaries,
        "CP2_A_ACCEPTED",
        (
            "max_lambda_error",
            "max_eta_error",
            "max_gamma_error",
            "max_nis_error",
            "max_increment_error",
            "max_covariance_error",
        ),
        errors,
        minimum=0.0,
    )
    require_finite_fields(
        summaries,
        "CP2_A_ACCEPTED",
        (
            "max_lambda_tolerance_ratio",
            "max_eta_tolerance_ratio",
            "max_gamma_tolerance_ratio",
            "max_nis_tolerance_ratio",
            "max_increment_tolerance_ratio",
            "max_covariance_tolerance_ratio",
        ),
        errors,
        minimum=0.0,
        maximum=1.0,
    )
    state = summaries.get("CP2_B_STATE_UPDATE", {})
    require_finite_fields(
        summaries,
        "CP2_B_STATE_UPDATE",
        (
            "max_preview_nominal_tolerance_ratio",
            "max_preview_covariance_tolerance_ratio",
            "max_identity_nominal_tolerance_ratio",
            "max_identity_covariance_tolerance_ratio",
            "max_mode_nominal_tolerance_ratio",
            "max_mode_covariance_block_tolerance_ratio",
        ),
        errors,
        minimum=0.0,
        maximum=1.0,
    )
    require_finite_fields(
        summaries, "CP2_B_STATE_UPDATE", ("max_clone_prior_error",), errors,
        minimum=0.0, maximum=0.0
    )
    require_finite_fields(
        summaries, "CP2_B_STATE_UPDATE", ("max_clone_nullspace_ratio",), errors,
        minimum=0.0, maximum=64.0
    )
    unobserved = state.get("min_unobserved_velocity_bias_increment")
    if not isinstance(unobserved, (int, float)) or not math.isfinite(unobserved) or unobserved <= 1e-12:
        errors.append("CP2_B_STATE_UPDATE unobserved correlated-block increment is not demonstrated")


def parse_gtest_xml(artifact_dir, errors):
    totals = {"tests": 0, "failures": 0, "errors": 0, "disabled": 0}
    suites = {}
    cases = []
    for test_name, expected_count in ALL_TESTS.items():
        xml_path = artifact_dir / (test_name + ".xml")
        if not xml_path.is_file():
            errors.append("missing gtest XML: " + xml_path.name)
            continue
        try:
            root = ET.parse(str(xml_path)).getroot()
        except (ET.ParseError, OSError) as exc:
            errors.append("cannot parse {}: {}".format(xml_path.name, exc))
            continue
        values = {}
        for field in totals:
            try:
                values[field] = int(root.attrib.get(field, "0"))
            except ValueError:
                errors.append("{} has noninteger {}".format(xml_path.name, field))
                values[field] = -1
            totals[field] += values[field]
        if values["tests"] != expected_count:
            errors.append(
                "{}: expected {} tests, got {}".format(xml_path.name, expected_count, values["tests"])
            )
        if (values["failures"], values["errors"], values["disabled"]) != (0, 0, 0):
            errors.append(xml_path.name + " is not a clean gtest pass")
        file_cases = []
        for test_case in root.findall(".//testcase"):
            case_name = "{}.{}".format(
                test_case.attrib.get("classname", ""), test_case.attrib.get("name", "")
            )
            cases.append(case_name)
            file_cases.append(case_name)
            if test_case.find("failure") is not None or test_case.find("error") is not None:
                errors.append(xml_path.name + " contains a failed/error testcase")
            if test_case.find("skipped") is not None:
                errors.append(xml_path.name + " contains a skipped testcase")
            if test_case.attrib.get("status") != "run":
                errors.append(xml_path.name + " contains a testcase that did not run")
            if test_case.attrib.get("result") != "completed":
                errors.append(xml_path.name + " contains a testcase that did not complete")
        if len(file_cases) != expected_count:
            errors.append(
                "{} contains {} testcase elements, expected {}".format(
                    xml_path.name, len(file_cases), expected_count
                )
            )
        expected_cases = set(TEST_CASES_BY_BINARY[test_name])
        if set(file_cases) != expected_cases or len(file_cases) != len(set(file_cases)):
            errors.append(
                "{} testcase ownership mismatch: expected {} got {}".format(
                    xml_path.name, sorted(expected_cases), sorted(file_cases)
                )
            )
        suites[test_name] = {
            "disabled": values["disabled"],
            "errors": values["errors"],
            "failures": values["failures"],
            "file": xml_path.name,
            "test_cases": sorted(file_cases),
            "tests": values["tests"],
        }
    duplicates = sorted({name for name in cases if cases.count(name) > 1})
    if duplicates:
        errors.append("duplicate gtest cases: " + repr(duplicates))
    if set(cases) != EXPECTED_TEST_CASES:
        errors.append(
            "gtest case inventory mismatch: expected {} got {}".format(
                sorted(EXPECTED_TEST_CASES), sorted(cases)
            )
        )
    return {
        "disabled": totals["disabled"],
        "errors": totals["errors"],
        "failures": totals["failures"],
        "suites": suites,
        "test_cases": sorted(cases),
        "tests": totals["tests"],
    }


def command_tokens(entry):
    arguments = entry.get("arguments")
    if isinstance(arguments, list) and all(isinstance(token, str) for token in arguments):
        return list(arguments)
    command = entry.get("command")
    if isinstance(command, str):
        try:
            return shlex.split(command)
        except ValueError:
            return []
    return []


def required_macro_record(tokens):
    definition_positions = {
        macro: {
            definition: [
                index for index, token in enumerate(tokens) if token == definition
            ]
            for definition in accepted_definitions
        }
        for macro, accepted_definitions in STRICT_REQUIRED_MACRO_DEFINITIONS.items()
    }
    macro_events = {macro: [] for macro in STRICT_REQUIRED_MACRO_DEFINITIONS}
    unsupported_macro_tokens = {
        macro: [] for macro in STRICT_REQUIRED_MACRO_DEFINITIONS
    }
    for index, token in enumerate(tokens):
        for macro, accepted_definitions in STRICT_REQUIRED_MACRO_DEFINITIONS.items():
            if token in accepted_definitions:
                macro_events[macro].append({
                    "index": index,
                    "state": "defined_accepted",
                    "token": token,
                })
            elif token == "-U" + macro:
                macro_events[macro].append({
                    "index": index,
                    "state": "undefined",
                    "token": token,
                })
            elif (
                token == "-D" + macro
                or token.startswith("-D" + macro + "=")
                or token.startswith("-D" + macro + "(")
            ):
                macro_events[macro].append({
                    "index": index,
                    "state": "defined_rejected",
                    "token": token,
                })
            elif macro in token:
                # Reject split, driver-forwarded, and otherwise opaque spellings.
                # Their ordering/effect cannot be proved from compiler argv.
                unsupported_macro_tokens[macro].append({
                    "index": index,
                    "token": token,
                })
    effective_macro_definitions = {}
    for macro, accepted_definitions in STRICT_REQUIRED_MACRO_DEFINITIONS.items():
        events = macro_events[macro]
        effective_macro = events[-1] if events else None
        effective_macro_definitions[macro] = (
            effective_macro["token"]
            if effective_macro is not None
            and effective_macro["state"] == "defined_accepted"
            and effective_macro["token"] in accepted_definitions
            else None
        )
    hidden_or_shell_tokens = [
        {"index": index, "token": token}
        for index, token in enumerate(tokens)
        if token.startswith("@")
        or any(marker in token for marker in (";", "&&", "||", "`", "$(", "\n", "\r"))
        or token in {"|", "<", ">", "2>", "2>&1"}
    ]
    passed = (
        all(
            effective_macro_definitions[macro] in accepted_definitions
            for macro, accepted_definitions in STRICT_REQUIRED_MACRO_DEFINITIONS.items()
        )
        and not any(unsupported_macro_tokens.values())
        and not hidden_or_shell_tokens
    )
    return {
        "definition_positions": definition_positions,
        "effective_definitions": effective_macro_definitions,
        "hidden_or_shell_tokens": hidden_or_shell_tokens,
        "macro_events": macro_events,
        "passed": bool(passed),
        "unsupported_macro_tokens": unsupported_macro_tokens,
    }


def strict_flag_record(tokens, workspace=None):
    positions = {flag: [index for index, token in enumerate(tokens) if token == flag]
                 for flag in STRICT_REQUIRED_FLAGS}
    macro_record = required_macro_record(tokens)
    conflicts = {
        flag: [index for index, token in enumerate(tokens) if token == flag]
        for flag in sorted(STRICT_FORBIDDEN_FLAGS)
    }
    conflicts["unsafe_macro_definition"] = [
        index for index, token in enumerate(tokens)
        if token.startswith("-D__FAST_MATH__") or token.startswith("-D__FINITE_MATH_ONLY__")
    ]
    hidden_or_shell_tokens = [
        {"index": index, "token": token}
        for index, token in enumerate(tokens)
        if token.startswith("@")
        or any(marker in token for marker in (";", "&&", "||", "`", "$(", "\n", "\r"))
        or token in {"|", "<", ">", "2>", "2>&1"}
    ]
    contract_kinds = {
        "fast_math": ("-ffast-math", "-fno-fast-math"),
        "signed_zeros": ("-fno-signed-zeros", "-fsigned-zeros"),
    }
    effective = {}
    for name, options in contract_kinds.items():
        occurrences = [(index, token) for index, token in enumerate(tokens) if token in options]
        effective[name] = max(occurrences)[1] if occurrences else None
    fp_contract = [(index, token) for index, token in enumerate(tokens)
                   if token.startswith("-ffp-contract=")]
    effective["fp_contract"] = max(fp_contract)[1] if fp_contract else None
    effective["required_macro_definitions"] = macro_record["effective_definitions"]
    expected_prefix_maps = []
    prefix_map_positions = {}
    unexpected_prefix_maps = []
    if workspace is not None:
        expected_prefix_maps = [
            "-ffile-prefix-map={}=/cp2/reproducible-root".format(workspace),
            "-fdebug-prefix-map={}=/cp2/reproducible-root".format(workspace),
            "-fmacro-prefix-map={}=/cp2/reproducible-root".format(workspace),
        ]
        normalized_tokens = [token.replace('"', "") for token in tokens]
        prefix_map_positions = {
            flag: [
                index for index, token in enumerate(normalized_tokens) if token == flag
            ]
            for flag in expected_prefix_maps
        }
        prefixes = ("-ffile-prefix-map=", "-fdebug-prefix-map=", "-fmacro-prefix-map=")
        unexpected_prefix_maps = [
            {"index": index, "token": token}
            for index, token in enumerate(normalized_tokens)
            if token.startswith(prefixes) and token not in expected_prefix_maps
        ]
    passed = (
        all(positions[flag] for flag in STRICT_REQUIRED_FLAGS)
        and effective["fast_math"] == "-fno-fast-math"
        and effective["signed_zeros"] == "-fsigned-zeros"
        and effective["fp_contract"] == "-ffp-contract=off"
        and macro_record["passed"]
        and not any(conflicts.values())
        and not hidden_or_shell_tokens
        and all(prefix_map_positions.get(flag) for flag in expected_prefix_maps)
        and not unexpected_prefix_maps
    )
    return {
        "command_sha256": sha256_bytes("\0".join(tokens).encode("utf-8")),
        "conflict_positions": conflicts,
        "definition_positions": macro_record["definition_positions"],
        "effective": effective,
        "hidden_or_shell_tokens": hidden_or_shell_tokens,
        "macro_events": macro_record["macro_events"],
        "passed": bool(passed),
        "prefix_map_positions": prefix_map_positions,
        "required_flag_positions": positions,
        "unsupported_macro_tokens": macro_record["unsupported_macro_tokens"],
        "unexpected_prefix_maps": unexpected_prefix_maps,
    }


def relative_source(file_value, directory_value, repo_root):
    if not isinstance(file_value, str):
        return None
    path = Path(file_value)
    if not path.is_absolute() and isinstance(directory_value, str):
        path = Path(directory_value) / path
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def analyze_compile_commands(path, repo_root, errors, compiler_record=None):
    if not path.is_file():
        errors.append("missing compile_commands.json")
        return {
            "compile_commands_sha256": None,
            "passed": False,
            "production_translation_units": {
                source: [] for source in STRICT_PRODUCTION_SOURCES
            },
            "required_flags": list(STRICT_REQUIRED_FLAGS),
            "required_macro_definitions": {
                macro: list(accepted_definitions)
                for macro, accepted_definitions
                in STRICT_REQUIRED_MACRO_DEFINITIONS.items()
            },
            "unit_test_targets": {},
        }
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append("cannot parse compile_commands.json: " + str(exc))
        entries = []
    if not isinstance(entries, list):
        errors.append("compile_commands.json root must be an array")
        entries = []
    production = {source: [] for source in STRICT_PRODUCTION_SOURCES}
    targets = {name: [] for name in sorted(STRICT_TARGETS)}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tokens = command_tokens(entry)
        source = relative_source(entry.get("file"), entry.get("directory"), repo_root)
        record = strict_flag_record(tokens, workspace=repo_root.parent)
        structure_errors = []
        expected_compiler = None
        if isinstance(compiler_record, dict):
            expected_compiler = compiler_record.get("resolved_path")
        actual_compiler = None
        if tokens:
            candidate = Path(tokens[0])
            if not candidate.is_absolute():
                resolved_candidate = shutil.which(tokens[0])
                candidate = Path(resolved_candidate) if resolved_candidate else candidate
            try:
                actual_compiler = str(candidate.resolve())
            except OSError:
                actual_compiler = str(candidate)
        if expected_compiler is None or actual_compiler != expected_compiler:
            structure_errors.append("compiler does not match CMakeCache.txt")

        compile_positions = [index for index, token in enumerate(tokens) if token == "-c"]
        command_source = None
        if len(compile_positions) != 1 or compile_positions[0] + 1 >= len(tokens):
            structure_errors.append("command does not contain exactly one -c source")
        else:
            command_source = relative_source(
                tokens[compile_positions[0] + 1], entry.get("directory"), repo_root
            )
            if command_source != source:
                structure_errors.append("entry file differs from actual -c source")

        output_positions = [index for index, token in enumerate(tokens) if token == "-o"]
        output = None
        target = None
        if len(output_positions) != 1 or output_positions[0] + 1 >= len(tokens):
            structure_errors.append("command does not contain exactly one -o output")
        else:
            output = tokens[output_positions[0] + 1]
            target_match = re.search(r"(?:^|/)CMakeFiles/([^/\s]+)\.dir/", output)
            target = target_match.group(1) if target_match else None
            if target is None:
                structure_errors.append("object output does not identify a CMake target")
            declared_output = entry.get("output")
            if isinstance(declared_output, str):
                declared = Path(declared_output)
                actual = Path(output)
                directory = Path(str(entry.get("directory", ".")))
                if not declared.is_absolute():
                    declared = directory / declared
                if not actual.is_absolute():
                    actual = directory / actual
                if declared.resolve() != actual.resolve():
                    structure_errors.append("entry output differs from actual -o output")

        if record["hidden_or_shell_tokens"]:
            structure_errors.append("response-file or shell-control token is forbidden")
        record["passed"] = bool(record["passed"] and not structure_errors)
        record.update({
            "actual_compiler": actual_compiler,
            "command_source": command_source,
            "output": output,
            "source": source,
            "structure_errors": structure_errors,
            "target": target,
        })
        if source in production and target == "ov_msckf_lib":
            production[source].append(record)
        if target in targets:
            targets[target].append(record)
    for source, records in production.items():
        if len(records) != 1:
            errors.append(
                "strict-FP evidence requires exactly one production command for " + source
            )
        elif not records[0]["passed"]:
            errors.append(source + " compile command is not effectively strict-FP")
    expected_target_sources = {
        name: {
            "ov_msckf/test/cp1/gtest_main.cpp",
            TEST_SOURCE_BY_BINARY[name],
        }
        for name in CP1_TESTS
    }
    expected_target_sources.update({
        name: {
            "ov_msckf/test/cp2/gtest_main.cpp",
            TEST_SOURCE_BY_BINARY[name],
        }
        for name in CP2_TESTS
    })
    for target, records in targets.items():
        actual_sources = {record["source"] for record in records}
        if actual_sources != expected_target_sources[target]:
            errors.append(
                "{} strict-FP source inventory mismatch: expected {} got {}".format(
                    target, sorted(expected_target_sources[target]), sorted(str(v) for v in actual_sources)
                )
            )
        for record in records:
            if not record["passed"]:
                errors.append("{} has a non-strict compile command for {}".format(target, record["source"]))
    passed = (
        all(
            len(records) == 1 and records[0].get("passed") is True
            for records in production.values()
        )
        and all(
            {record["source"] for record in targets[target]} == expected_target_sources[target]
            and all(record["passed"] for record in targets[target])
            for target in targets
        )
    )
    return {
        "compile_commands_sha256": sha256_file(path),
        "passed": bool(passed),
        "production_translation_units": production,
        "required_flags": list(STRICT_REQUIRED_FLAGS),
        "required_macro_definitions": {
            macro: list(accepted_definitions)
            for macro, accepted_definitions
            in STRICT_REQUIRED_MACRO_DEFINITIONS.items()
        },
        "unit_test_targets": targets,
    }


def analyze_dependency_compile_commands(
    path, package, repo_root, workspace_build_root, errors
):
    label = package + " compile_commands.json"
    expected_count = DEPENDENCY_EXPECTED_COMMAND_COUNTS[package]
    if not path.is_file():
        errors.append("missing " + label)
        return {
            "artifact": path.name,
            "command_count": 0,
            "commands": [],
            "compile_commands_sha256": None,
            "expected_command_count": expected_count,
            "passed": False,
            "unique_source_count": 0,
        }
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append("cannot parse {}: {}".format(label, exc))
        entries = []
    if not isinstance(entries, list):
        errors.append(label + " root must be an array")
        entries = []
    if len(entries) != expected_count:
        errors.append(
            "{} must contain exactly {} compile commands, got {}".format(
                label, expected_count, len(entries)
            )
        )

    records = []
    expected_build_directory = (workspace_build_root / package).resolve()
    workspace_source_root = repo_root.resolve()
    googletest_source_root = Path("/usr/src/googletest").resolve()
    for index, entry in enumerate(entries):
        structure_errors = []
        output = None
        source_binding = None
        target = None
        if not isinstance(entry, dict):
            tokens = []
            source = None
            structure_errors.append("entry is not an object")
        else:
            tokens = command_tokens(entry)
            source = relative_source(entry.get("file"), entry.get("directory"), repo_root)
            if not tokens:
                structure_errors.append("entry has no parseable compiler argv")

            directory_value = entry.get("directory")
            if not isinstance(directory_value, str):
                structure_errors.append("entry has no build directory")
                command_directory = None
            else:
                command_directory = Path(directory_value).resolve()
                try:
                    command_directory.relative_to(expected_build_directory)
                except ValueError:
                    structure_errors.append("entry directory is outside the package build root")

            declared_source = entry.get("file")
            canonical_source = None
            if isinstance(declared_source, str):
                declared_path = Path(declared_source)
                if not declared_path.is_absolute() and command_directory is not None:
                    declared_path = command_directory / declared_path
                try:
                    canonical_source = declared_path.resolve(strict=True)
                except OSError:
                    structure_errors.append("declared source is not an existing regular file")
            else:
                structure_errors.append("entry has no declared source")
            if canonical_source is not None:
                if not canonical_source.is_file():
                    structure_errors.append("declared source is not a regular file")
                else:
                    try:
                        package_relative = canonical_source.relative_to(workspace_source_root)
                        if not package_relative.parts or package_relative.parts[0] != package:
                            structure_errors.append(
                                "workspace source is outside the dependency package"
                            )
                        else:
                            source_binding = "workspace_source"
                    except ValueError:
                        try:
                            canonical_source.relative_to(googletest_source_root)
                            source_binding = "controlled_googletest_source"
                        except ValueError:
                            structure_errors.append(
                                "source is outside workspace and controlled GoogleTest roots"
                            )

            compile_positions = [
                position for position, token in enumerate(tokens) if token == "-c"
            ]
            if len(compile_positions) != 1 or compile_positions[0] + 1 >= len(tokens):
                structure_errors.append("command does not contain exactly one -c source")
            else:
                command_source_value = tokens[compile_positions[0] + 1]
                command_source = relative_source(
                    command_source_value, entry.get("directory"), repo_root
                )
                command_source_path = Path(command_source_value)
                if not command_source_path.is_absolute() and command_directory is not None:
                    command_source_path = command_directory / command_source_path
                try:
                    canonical_command_source = command_source_path.resolve(strict=True)
                except OSError:
                    canonical_command_source = None
                if command_source != source or canonical_command_source != canonical_source:
                    structure_errors.append("entry file differs from actual -c source")

            output_positions = [
                position for position, token in enumerate(tokens) if token == "-o"
            ]
            if len(output_positions) != 1 or output_positions[0] + 1 >= len(tokens):
                structure_errors.append("command does not contain exactly one -o output")
            elif command_directory is not None:
                output_value = tokens[output_positions[0] + 1]
                output_path = Path(output_value)
                if not output_path.is_absolute():
                    output_path = command_directory / output_path
                try:
                    output_relative = output_path.resolve().relative_to(
                        expected_build_directory
                    )
                    output = output_relative.as_posix()
                except ValueError:
                    output_relative = None
                    structure_errors.append("object output is outside the package build root")
                if output_relative is not None:
                    parts = output_relative.parts
                    cmake_positions = [
                        position for position, part in enumerate(parts)
                        if part == "CMakeFiles"
                    ]
                    if len(cmake_positions) != 1:
                        structure_errors.append(
                            "object output does not have CMakeFiles/<target>.dir structure"
                        )
                    else:
                        cmake_position = cmake_positions[0]
                        if (
                            cmake_position + 2 >= len(parts)
                            or not parts[cmake_position + 1].endswith(".dir")
                        ):
                            structure_errors.append(
                                "object output does not have CMakeFiles/<target>.dir structure"
                            )
                        else:
                            target = parts[cmake_position + 1][:-4]
                declared_output = entry.get("output")
                if isinstance(declared_output, str):
                    declared_output_path = Path(declared_output)
                    if not declared_output_path.is_absolute():
                        declared_output_path = command_directory / declared_output_path
                    if declared_output_path.resolve() != output_path.resolve():
                        structure_errors.append("entry output differs from actual -o output")
        macro_record = required_macro_record(tokens)
        exact_definitions = all(
            macro_record["effective_definitions"].get(macro) == definition
            for macro, definition in DEPENDENCY_REQUIRED_MACRO_DEFINITIONS.items()
        )
        passed = bool(
            macro_record["passed"] and exact_definitions and not structure_errors
        )
        record = {
            "command_sha256": sha256_bytes("\0".join(tokens).encode("utf-8")),
            "index": index,
            "macro_contract": macro_record,
            "output": output,
            "passed": passed,
            "source": source,
            "source_binding": source_binding,
            "structure_errors": structure_errors,
            "target": target,
        }
        records.append(record)
        if not passed:
            errors.append(
                "{} command {} for {} fails dependency ABI/source/output proof".format(
                    package, index, source
                )
            )
    normalized_sources = [record["source"] for record in records]
    unique_source_count = len(set(normalized_sources))
    unique_sources = (
        None not in normalized_sources and unique_source_count == len(normalized_sources)
    )
    if not unique_sources:
        errors.append(package + " compile commands do not have unique normalized sources")
    return {
        "artifact": path.name,
        "command_count": len(entries),
        "commands": records,
        "compile_commands_sha256": sha256_file(path),
        "expected_command_count": expected_count,
        "passed": bool(
            len(entries) == expected_count
            and unique_sources
            and all(record["passed"] for record in records)
        ),
        "unique_source_count": unique_source_count,
    }


def analyze_dependency_eigen_abi(
    artifact_dir, repo_root, workspace_build_root, errors
):
    packages = {
        package: analyze_dependency_compile_commands(
            artifact_dir / artifact,
            package,
            repo_root,
            workspace_build_root,
            errors,
        )
        for package, artifact in DEPENDENCY_COMPILE_COMMAND_ARTIFACTS.items()
    }
    return {
        "packages": packages,
        "passed": bool(
            all(record.get("passed") is True for record in packages.values())
        ),
        "required_macro_definitions": {
            macro: [definition]
            for macro, definition in DEPENDENCY_REQUIRED_MACRO_DEFINITIONS.items()
        },
    }


def expected_artifact_files():
    files = {
        REPORT_NAME,
        "CMakeCache.txt",
        CATKIN_PACKAGE_CMAKE_LOG_NAME,
        "compile_commands.json",
        DEPENDENCY_INVENTORY_NAME,
        "ceres_source_snapshot.tar",
        "googletest_source_snapshot.tar",
        "source_before.json",
        "source_after.json",
        SOURCE_ARCHIVE_NAME,
        "verifier_self_test.json",
        "verifier_self_test.log",
        WORKSPACE_RECORD_NAME,
        "THIRD_PARTY_NOTICES/Ceres-LICENSE",
        "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
    }
    files.update(DEPENDENCY_COMPILE_COMMAND_ARTIFACTS.values())
    files.update("binaries/" + name for name in SNAPSHOTTED_LIBRARIES)
    for step in BUILD_STEPS:
        files.add("build_" + step + ".json")
        files.add("build_" + step + ".log")
    for test_name in ALL_TESTS:
        files.add(test_name + ".json")
        files.add(test_name + ".log")
        files.add(test_name + ".xml")
        files.add("binaries/" + test_name)
    return files


def regular_artifact_files(artifact_dir, errors=None, finalized=False):
    files = set()
    if not artifact_dir.is_dir():
        if errors is not None:
            errors.append("artifact directory does not exist")
        return files
    paths = [artifact_dir] + list(artifact_dir.rglob("*"))
    for path in paths:
        if path == artifact_dir:
            relative = "."
        else:
            relative = path.relative_to(artifact_dir).as_posix()
        try:
            status = path.lstat()
            mode = status.st_mode
        except OSError as exc:
            if errors is not None:
                errors.append("cannot stat artifact path {}: {}".format(relative, exc))
            continue
        if stat.S_ISLNK(mode):
            if errors is not None:
                errors.append("artifact symlink is forbidden: " + relative)
        elif stat.S_ISREG(mode):
            if status.st_nlink != 1 and errors is not None:
                errors.append("artifact regular file must have link count one: " + relative)
            if status.st_size <= 0 and errors is not None:
                errors.append("artifact regular file is empty: " + relative)
            if status.st_size > MAX_ARTIFACT_FILE_BYTES and errors is not None:
                errors.append("artifact regular file exceeds size limit: " + relative)
            executable = relative.startswith("binaries/")
            permissions = stat.S_IMODE(mode)
            if finalized:
                expected_mode = 0o555 if executable else 0o444
                if permissions != expected_mode and errors is not None:
                    errors.append(
                        "finalized artifact mode for {} is {:04o}, expected {:04o}".format(
                            relative, permissions, expected_mode
                        )
                    )
            else:
                if mode & (stat.S_IWGRP | stat.S_IWOTH) and errors is not None:
                    errors.append("group/other-writable artifact file is forbidden: " + relative)
                if executable and not mode & stat.S_IXUSR and errors is not None:
                    errors.append("snapshotted binary/library is not owner-executable: " + relative)
            if relative != MANIFEST_NAME:
                files.add(relative)
        elif stat.S_ISDIR(mode):
            if finalized and stat.S_IMODE(mode) != 0o555 and errors is not None:
                errors.append("finalized artifact directory mode is not 0555: " + relative)
        elif errors is not None:
            errors.append("nonregular artifact path is forbidden: " + relative)
    return files


def generate_manifest(artifact_dir):
    destination = artifact_dir / MANIFEST_NAME
    if destination.exists():
        raise ValueError("refusing to overwrite " + str(destination))
    errors = []
    files = regular_artifact_files(artifact_dir, errors)
    if errors:
        raise ValueError("; ".join(errors))
    lines = ["{}  {}\n".format(sha256_file(artifact_dir / relative), relative)
             for relative in sorted(files)]
    atomic_write_bytes(destination, "".join(lines).encode("utf-8"))


def verify_manifest(artifact_dir, errors):
    manifest = artifact_dir / MANIFEST_NAME
    if not manifest.is_file():
        errors.append("missing " + MANIFEST_NAME)
        return {}
    expected = {}
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        errors.append("cannot read SHA256SUMS: " + str(exc))
        return {}
    for line_number, line in enumerate(lines, 1):
        fields = line.split("  ", 1)
        if len(fields) != 2 or not HEX64_PATTERN.fullmatch(fields[0]):
            errors.append("SHA256SUMS:{}: malformed entry".format(line_number))
            continue
        digest, relative = fields
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or relative in expected or "\\" in relative:
            errors.append("SHA256SUMS:{}: unsafe or duplicate path".format(line_number))
            continue
        expected[relative] = digest
    actual = regular_artifact_files(artifact_dir, errors)
    if actual != set(expected):
        errors.append(
            "artifact file set differs from SHA256SUMS: expected {} got {}".format(
                sorted(expected), sorted(actual)
            )
        )
    for relative, digest in expected.items():
        path = artifact_dir / relative
        if not path.is_file() or sha256_file(path) != digest:
            errors.append("checksum mismatch: " + relative)
    canonical = "".join(
        "{}  {}\n".format(expected[relative], relative) for relative in sorted(expected)
    ).encode("utf-8")
    try:
        if manifest.read_bytes() != canonical:
            errors.append("SHA256SUMS is not in canonical sorted form")
    except OSError as exc:
        errors.append("cannot reread SHA256SUMS: " + str(exc))
    return expected


def validate_manifest_anchor(artifact_dir, expected_digest, errors):
    manifest = artifact_dir / MANIFEST_NAME
    actual = sha256_file(manifest) if manifest.is_file() else None
    if expected_digest is not None:
        if not isinstance(expected_digest, str) or not HEX64_PATTERN.fullmatch(expected_digest):
            errors.append("external SHA256SUMS anchor must be 64 lowercase hexadecimal characters")
        elif actual != expected_digest:
            errors.append("SHA256SUMS differs from the supplied external digest anchor")
    return {
        "claim": (
            "external_sha256_anchor_matched"
            if expected_digest is not None and actual == expected_digest
            else "internal_consistency_only_no_external_anchor"
        ),
        "expected_sha256": expected_digest,
        "sha256": actual,
    }


def parse_utc_timestamp(value, errors, label):
    if not isinstance(value, str) or not value.endswith("Z"):
        errors.append(label + " is not a UTC Z timestamp")
        return None
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        errors.append(label + " is not an ISO-8601 timestamp")
        return None
    if parsed.utcoffset() != dt.timedelta(0):
        errors.append(label + " is not UTC")
        return None
    return parsed


def validate_interval(record, errors, label):
    started = parse_utc_timestamp(record.get("started_utc"), errors, label + ".started_utc")
    finished = parse_utc_timestamp(record.get("finished_utc"), errors, label + ".finished_utc")
    if started is not None and finished is not None and finished < started:
        errors.append(label + " finishes before it starts")
    return started, finished


def expected_build_environment(workspace_record, repo_root, commit, errors):
    workspace = workspace_record.get("workspace")
    if not isinstance(workspace, str):
        workspace = ""
    try:
        source_epoch = git_text(repo_root, "show", "-s", "--format=%ct", commit)
    except subprocess.CalledProcessError as exc:
        errors.append("cannot determine SOURCE_DATE_EPOCH: " + str(exc))
        source_epoch = ""
    return {
        "CC": "/usr/bin/cc",
        "CMAKE_PREFIX_PATH": "/opt/ros/noetic",
        "CXX": "/usr/bin/c++",
        "HOME": workspace + "/home",
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": "/opt/ros/noetic/lib",
        "LOGNAME": "moksh",
        "PATH": "/usr/bin:/bin",
        "PKG_CONFIG_PATH": "/opt/ros/noetic/lib/pkgconfig",
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
        "ROSLISP_PACKAGE_DIRECTORIES": "",
        "ROS_DISTRO": "noetic",
        "ROS_ETC_DIR": "/opt/ros/noetic/etc/ros",
        "ROS_MASTER_URI": "http://localhost:11311",
        "ROS_PACKAGE_PATH": "/opt/ros/noetic/share",
        "ROS_PYTHON_VERSION": "3",
        "ROS_ROOT": "/opt/ros/noetic/share/ros",
        "ROS_VERSION": "1",
        "SOURCE_DATE_EPOCH": source_epoch,
        "TMPDIR": workspace + "/tmp",
        "TZ": "UTC",
        "USER": "moksh",
    }


def expected_build_argv(step, workspace_record, repo_root):
    workspace = workspace_record.get("workspace", "")
    source_root = workspace_record.get("source_root", "")
    workspace_build_root = workspace_record.get("workspace_build_root", "")
    ceres_source = workspace_record.get("ceres_source_root", "")
    ceres_build = workspace_record.get("ceres_build_root", "")
    ceres_prefix = workspace_record.get("ceres_install_prefix", "")
    reproducible_prefix = workspace_record.get("reproducible_prefix", "")
    runtime_rpath = workspace_record.get("runtime_rpath", "")
    deterministic_compile_flags = (
        '-ffile-prefix-map="{}"={} -fdebug-prefix-map="{}"={} '
        '-fmacro-prefix-map="{}"={}'.format(
            workspace, reproducible_prefix, workspace, reproducible_prefix,
            workspace, reproducible_prefix,
        )
    )
    deterministic_link_flags = "-Wl,--build-id=sha1"
    if step == "ceres_configure":
        return [
            "/usr/bin/cmake", "-S", ceres_source, "-B", ceres_build,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DCMAKE_VERBOSE_MAKEFILE=ON",
            "-DCMAKE_INSTALL_PREFIX=" + ceres_prefix,
            "-DBUILD_SHARED_LIBS=ON",
            "-DBUILD_TESTING=OFF",
            "-DBUILD_EXAMPLES=OFF",
            "-DBUILD_DOCUMENTATION=OFF",
            "-DMINIGLOG=ON",
            "-DGFLAGS=OFF",
            "-DLAPACK=OFF",
            "-DSUITESPARSE=OFF",
            "-DCXSPARSE=OFF",
            "-DCUSTOM_BLAS=ON",
            "-DCMAKE_C_FLAGS=" + deterministic_compile_flags,
            "-DCMAKE_CXX_FLAGS=" + deterministic_compile_flags,
            "-DCMAKE_EXE_LINKER_FLAGS=" + deterministic_link_flags,
            "-DCMAKE_SHARED_LINKER_FLAGS=" + deterministic_link_flags,
            "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
            "-DCMAKE_BUILD_RPATH=" + runtime_rpath,
            "-DCMAKE_INSTALL_RPATH=" + runtime_rpath,
            "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE",
        ]
    if step == "ceres_build":
        return [
            "/usr/bin/cmake", "--build", ceres_build, "--parallel", "1", "--verbose",
        ]
    if step == "ceres_install":
        return ["/usr/bin/cmake", "--install", ceres_build]
    if step == "catkin_config":
        return [
            "/usr/bin/catkin", "config", "--workspace", workspace,
            "--source-space", source_root,
            "--build-space", workspace_build_root,
            "--devel-space", workspace + "/devel",
            "--log-space", workspace + "/logs",
            "--extend", "/opt/ros/noetic", "--merge-devel", "--cmake-args",
            "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
            "-DCMAKE_VERBOSE_MAKEFILE=ON",
            "-DCATKIN_ENABLE_TESTING=ON",
            "-DBUILD_TESTING=ON",
            "-DDISABLE_MATPLOTLIB=ON",
            "-DPYTHON_EXECUTABLE=/usr/bin/python3",
            "-DPython_EXECUTABLE=/usr/bin/python3",
            "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
            "-DCMAKE_C_FLAGS=" + deterministic_compile_flags,
            "-DCMAKE_CXX_FLAGS=" + deterministic_compile_flags,
            "-DCMAKE_EXE_LINKER_FLAGS=" + deterministic_link_flags,
            "-DCMAKE_SHARED_LINKER_FLAGS=" + deterministic_link_flags,
            "-DCMAKE_BUILD_WITH_INSTALL_RPATH=ON",
            "-DCMAKE_BUILD_RPATH=" + runtime_rpath,
            "-DCMAKE_INSTALL_RPATH=" + runtime_rpath,
            "-DCMAKE_INSTALL_RPATH_USE_LINK_PATH=FALSE",
            "-DCeres_DIR=" + ceres_prefix + "/lib/cmake/Ceres",
        ]
    if step == "catkin_build":
        return [
            "/usr/bin/catkin", "build", "--workspace", workspace,
            "ov_msckf", "--jobs", "1", "--no-status", "--summarize",
        ]
    return [
        "/usr/bin/cmake", "--build", workspace_build_root + "/ov_msckf", "--target",
        *ALL_TESTS.keys(), "--", "-j1", "VERBOSE=1",
    ]


def validate_build_command(step, record, repo_root, workspace_record, commit, errors):
    required_fields = {
        "argv", "cwd", "environment", "environment_mode", "exit_status",
        "finished_utc", "name", "serialized", "started_utc",
    }
    if set(record) != required_fields:
        errors.append("build_{}.json field inventory is not exact".format(step))
    if record.get("name") != step:
        errors.append("build_{}.json has the wrong name".format(step))
    if record.get("serialized") is not True:
        errors.append("build step {} is not recorded as serialized".format(step))
    if record.get("exit_status") != 0:
        errors.append("build step {} exit status is not zero".format(step))
    if record.get("cwd") != "." or record.get("environment_mode") != "env-i":
        errors.append("build step {} does not record repo-root env-i execution".format(step))
    if record.get("environment") != expected_build_environment(
        workspace_record, repo_root, commit, errors
    ):
        errors.append("build step {} environment differs from the exact contract".format(step))
    validate_interval(record, errors, "build_" + step)
    argv = record.get("argv")
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        errors.append("build step {} argv is invalid".format(step))
        return
    if argv != expected_build_argv(step, workspace_record, repo_root):
        errors.append("build step {} argv differs from the exact serialized contract".format(step))


def collect_build_records(artifact_dir, repo_root, workspace_record, commit, errors):
    records = []
    previous_finished = None
    for step in BUILD_STEPS:
        record_path = artifact_dir / ("build_" + step + ".json")
        record = read_json(record_path, errors, record_path.name)
        validate_build_command(step, record, repo_root, workspace_record, commit, errors)
        started, finished = validate_interval(record, [], "build_" + step)
        if previous_finished is not None and started is not None and started < previous_finished:
            errors.append("serialized build intervals overlap or are out of order at " + step)
        if finished is not None:
            previous_finished = finished
        log_path = artifact_dir / ("build_" + step + ".log")
        if not log_path.is_file():
            errors.append("missing build log: " + log_path.name)
            log_digest = None
        else:
            log_digest = sha256_file(log_path)
        enriched = dict(record)
        enriched["log"] = log_path.name
        enriched["log_sha256"] = log_digest
        records.append(enriched)
    return records


def collect_workspace_record(
    artifact_dir, repo_root, source, errors, allow_synthetic=False
):
    record = read_json(artifact_dir / WORKSPACE_RECORD_NAME, errors, WORKSPACE_RECORD_NAME)
    required_fields = {
        "archive_argv", "archive_artifact", "archive_roots", "archive_sha256",
        "archive_size_bytes", "ceres_archive_argv", "ceres_archive_artifact",
        "ceres_archive_sha256", "ceres_archive_size_bytes", "ceres_build_root",
        "ceres_install_prefix", "ceres_source_commit",
        "ceres_source_read_only_after_build", "ceres_source_read_only_before_build",
        "ceres_source_root", "ceres_source_tag", "created_utc", "fresh",
        "googletest_archive_argv", "googletest_archive_artifact",
        "googletest_archive_sha256", "googletest_archive_sha256_after_build",
        "googletest_archive_sha256_before_build", "googletest_archive_size_bytes",
        "googletest_archive_size_bytes_after_build",
        "googletest_archive_size_bytes_before_build", "googletest_source_root",
        "googletest_unchanged_after_build", "kind", "repo_root", "repository_build_root",
        "reproducible_prefix", "reused", "runtime_rpath", "schema_version",
        "source_commit", "source_read_only_after_build",
        "source_read_only_before_build", "source_root", "source_tree", "workspace",
        "workspace_build_root",
    }
    if set(record) != required_fields:
        errors.append("workspace.json field inventory is not exact")
    workspace_text = record.get("workspace")
    workspace = Path(workspace_text) if isinstance(workspace_text, str) else Path("/")
    expected_repository_build_root = repo_root / "build"
    shared_ceres_checkout = expected_repository_build_root / "vendor/ceres-src"
    if allow_synthetic and shared_ceres_checkout.is_dir():
        try:
            expected_ceres_commit = git_text(
                shared_ceres_checkout, "rev-parse", "--verify", "HEAD"
            )
        except subprocess.CalledProcessError:
            expected_ceres_commit = record.get("ceres_source_commit")
    else:
        expected_ceres_commit = CERES_COMMIT
    expected = {
        "archive_artifact": SOURCE_ARCHIVE_NAME,
        "archive_roots": list(ARCHIVE_ROOTS),
        "ceres_archive_artifact": "ceres_source_snapshot.tar",
        "ceres_build_root": str(workspace / "ceres-build"),
        "ceres_install_prefix": str(workspace / "ceres-install"),
        "ceres_source_commit": expected_ceres_commit,
        "ceres_source_read_only_after_build": True,
        "ceres_source_read_only_before_build": True,
        "ceres_source_root": str(workspace / "ceres-src"),
        "ceres_source_tag": CERES_TAG,
        "fresh": True,
        "googletest_archive_artifact": "googletest_source_snapshot.tar",
        "googletest_archive_sha256": GOOGLETEST_ARCHIVE_SHA256,
        "googletest_archive_sha256_after_build": GOOGLETEST_ARCHIVE_SHA256,
        "googletest_archive_sha256_before_build": GOOGLETEST_ARCHIVE_SHA256,
        "googletest_archive_size_bytes": GOOGLETEST_ARCHIVE_SIZE_BYTES,
        "googletest_archive_size_bytes_after_build": GOOGLETEST_ARCHIVE_SIZE_BYTES,
        "googletest_archive_size_bytes_before_build": GOOGLETEST_ARCHIVE_SIZE_BYTES,
        "googletest_source_root": "/usr/src/googletest",
        "googletest_unchanged_after_build": True,
        "kind": "unique_git_archive_cp2_catkin_workspace",
        "repo_root": str(repo_root),
        "repository_build_root": str(expected_repository_build_root),
        "reproducible_prefix": "/cp2/reproducible-root",
        "reused": False,
        "runtime_rpath": "$ORIGIN:/opt/ros/noetic/lib",
        "schema_version": 1,
        "source_commit": source.get("commit"),
        "source_read_only_after_build": True,
        "source_read_only_before_build": True,
        "source_root": str(workspace / "src"),
        "source_tree": source.get("tree"),
        "workspace_build_root": str(workspace / "build"),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            errors.append("workspace.json {} differs from the fresh-workspace contract".format(key))
    parse_utc_timestamp(record.get("created_utc"), errors, "workspace.created_utc")
    workspace_parent = expected_repository_build_root / "cp2-unit-workspaces"
    try:
        workspace.relative_to(workspace_parent)
    except ValueError:
        errors.append("workspace path is outside the dedicated CP2 workspace parent")
    if not workspace.is_absolute() or workspace.resolve() != workspace:
        errors.append("workspace path is not canonical and absolute")
    if not workspace.name.startswith(".cp2-unit-") or ".workspace." not in workspace.name:
        errors.append("workspace path does not have the unique CP2 workspace form")
    archive_argv = [
        "/usr/bin/git", "-C", str(repo_root), "archive", "--format=tar",
        "--output=" + str(workspace / SOURCE_ARCHIVE_NAME), source.get("commit"), "--",
        *ARCHIVE_ROOTS,
    ]
    if record.get("archive_argv") != archive_argv:
        errors.append("workspace archive argv differs from the exact Git archive command")
    ceres_archive_argv = [
        "/usr/bin/git", "-C", str(shared_ceres_checkout), "archive", "--format=tar",
        "--output=" + str(workspace / "ceres_source_snapshot.tar"),
        expected_ceres_commit,
    ]
    if record.get("ceres_archive_argv") != ceres_archive_argv:
        errors.append("Ceres archive argv differs from the exact pinned Git archive command")
    googletest_archive_argv = [
        "/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
        "--numeric-owner", "--format=gnu", "--create",
        "--file=" + str(workspace / "googletest_source_snapshot.tar"),
        "--directory=/usr/src", "googletest",
    ]
    if record.get("googletest_archive_argv") != googletest_archive_argv:
        errors.append("GoogleTest archive argv differs from the deterministic tar command")

    for artifact_name, hash_field, size_field, label in (
        (SOURCE_ARCHIVE_NAME, "archive_sha256", "archive_size_bytes", "workspace source"),
        (
            "ceres_source_snapshot.tar", "ceres_archive_sha256",
            "ceres_archive_size_bytes", "Ceres source",
        ),
        (
            "googletest_source_snapshot.tar", "googletest_archive_sha256",
            "googletest_archive_size_bytes", "GoogleTest source",
        ),
    ):
        artifact_archive = artifact_dir / artifact_name
        archive_sha = sha256_file(artifact_archive) if artifact_archive.is_file() else None
        archive_size = artifact_archive.stat().st_size if artifact_archive.is_file() else None
        if record.get(hash_field) != archive_sha or record.get(size_field) != archive_size:
            errors.append("{} archive hash/size differs from {}".format(label, artifact_name))
        retained_archive = workspace / artifact_name
        if retained_archive.is_file() and sha256_file(retained_archive) != archive_sha:
            errors.append("retained {} archive differs from evidence".format(label))

    for retained_root, label in (
        (workspace / "src", "workspace source"),
        (workspace / "ceres-src", "Ceres source"),
    ):
        if retained_root.is_dir():
            for path in [retained_root] + list(retained_root.rglob("*")):
                try:
                    status = path.lstat()
                except OSError as exc:
                    errors.append("cannot stat retained {} path: {}".format(label, exc))
                    continue
                if stat.S_ISLNK(status.st_mode):
                    errors.append("retained {} archive contains a symlink: {}".format(label, path))
                if status.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
                    errors.append("retained {} archive is writable: {}".format(label, path))
    return record


def tar_member_sha256(path, member_name, errors, label):
    if not path.is_file():
        errors.append("missing " + label)
        return None
    try:
        with tarfile.open(str(path), mode="r:") as archive:
            all_members = archive.getmembers()
            seen = set()
            for member in all_members:
                pure = PurePosixPath(member.name)
                if (
                    pure.is_absolute() or ".." in pure.parts or "\\" in member.name
                    or member.name in seen
                ):
                    errors.append(label + " has an unsafe or duplicate member: " + member.name)
                seen.add(member.name)
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    errors.append(label + " has a link or special member: " + member.name)
            members = [member for member in all_members if member.name == member_name]
            if len(members) != 1 or not members[0].isfile():
                errors.append("{} must contain exactly one regular {}".format(label, member_name))
                return None
            stream = archive.extractfile(members[0])
            return sha256_bytes(stream.read()) if stream is not None else None
    except (OSError, tarfile.TarError) as exc:
        errors.append("cannot inspect {}: {}".format(label, exc))
        return None


def collect_third_party_sources_and_notices(
    artifact_dir, repo_root, workspace_record, errors, allow_synthetic=False
):
    ceres_archive = artifact_dir / "ceres_source_snapshot.tar"
    googletest_archive = artifact_dir / "googletest_source_snapshot.tar"
    ceres_notice = artifact_dir / "THIRD_PARTY_NOTICES/Ceres-LICENSE"
    googletest_notice = artifact_dir / "THIRD_PARTY_NOTICES/GoogleTest-LICENSE"
    for path, hash_field, size_field, label in (
        (ceres_archive, "ceres_archive_sha256", "ceres_archive_size_bytes", "Ceres"),
        (
            googletest_archive, "googletest_archive_sha256",
            "googletest_archive_size_bytes", "GoogleTest",
        ),
    ):
        if (
            not path.is_file()
            or workspace_record.get(hash_field) != sha256_file(path)
            or workspace_record.get(size_field) != path.stat().st_size
        ):
            errors.append(label + " archive hash/size differs from workspace provenance")
    for path, expected, label in (
        (ceres_notice, CERES_LICENSE_SHA256, "Ceres license notice"),
        (googletest_notice, GOOGLETEST_LICENSE_SHA256, "GoogleTest license notice"),
    ):
        if not path.is_file() or sha256_file(path) != expected:
            errors.append(label + " is absent or differs from the pinned bytes")
    if tar_member_sha256(
        ceres_archive, "LICENSE", errors, "ceres_source_snapshot.tar"
    ) != CERES_LICENSE_SHA256:
        errors.append("Ceres source archive LICENSE differs from its staged notice")
    if tar_member_sha256(
        googletest_archive, "googletest/googlemock/LICENSE", errors,
        "googletest_source_snapshot.tar",
    ) != GOOGLETEST_LICENSE_SHA256:
        errors.append("GoogleTest source archive LICENSE differs from its staged notice")

    expected_ceres_commit = workspace_record.get("ceres_source_commit")
    if not allow_synthetic and expected_ceres_commit != CERES_COMMIT:
        errors.append("workspace Ceres source commit differs from the pinned commit")
    checkout = repo_root / "build/vendor/ceres-src"
    reproduced_ceres_sha = None
    if checkout.is_dir() and isinstance(expected_ceres_commit, str):
        try:
            actual_commit = git_text(checkout, "rev-parse", "--verify", "HEAD")
            actual_tag = git_text(checkout, "describe", "--tags", "--exact-match")
            actual_status = git_text(
                checkout, "status", "--porcelain=v1", "--untracked-files=all"
            ).splitlines()
            expected_commit = actual_commit if allow_synthetic else CERES_COMMIT
            if (actual_commit, actual_tag, actual_status) != (
                expected_commit, CERES_TAG, []
            ):
                errors.append("live Ceres checkout is not the clean pinned commit/tag")
            reproduced = subprocess.check_output(
                ["/usr/bin/git", "-C", str(checkout), "archive", "--format=tar",
                 expected_ceres_commit], stderr=subprocess.STDOUT,
            )
            reproduced_ceres_sha = sha256_bytes(reproduced)
            if ceres_archive.is_file() and reproduced_ceres_sha != sha256_file(ceres_archive):
                errors.append("Ceres source archive differs from the pinned live Git object")
        except subprocess.CalledProcessError as exc:
            errors.append("cannot reproduce Ceres source archive: " + str(exc))

    installed_gtest_version = None
    dpkg_query = Path("/usr/bin/dpkg-query")
    if dpkg_query.is_file():
        try:
            installed_gtest_version = subprocess.check_output(
                [str(dpkg_query), "-W", "-f=${Version}", "googletest"],
                text=True, stderr=subprocess.STDOUT,
                env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
            ).strip()
        except subprocess.CalledProcessError:
            installed_gtest_version = None
    if installed_gtest_version is not None and installed_gtest_version != "1.10.0-2":
        errors.append("installed GoogleTest package version differs from 1.10.0-2")
    return {
        "ceres": {
            "archive_artifact": "ceres_source_snapshot.tar",
            "archive_sha256": sha256_file(ceres_archive) if ceres_archive.is_file() else None,
            "archive_size_bytes": ceres_archive.stat().st_size if ceres_archive.is_file() else None,
            "commit": expected_ceres_commit,
            "license_artifact": "THIRD_PARTY_NOTICES/Ceres-LICENSE",
            "license_sha256": CERES_LICENSE_SHA256,
            "git_archive_sha256": sha256_file(ceres_archive) if ceres_archive.is_file() else None,
            "tag": workspace_record.get("ceres_source_tag"),
            "version": CERES_TAG,
        },
        "googletest": {
            "archive_artifact": "googletest_source_snapshot.tar",
            "archive_sha256": (
                sha256_file(googletest_archive) if googletest_archive.is_file() else None
            ),
            "archive_size_bytes": (
                googletest_archive.stat().st_size if googletest_archive.is_file() else None
            ),
            "debian_package": "googletest",
            "debian_version": "1.10.0-2",
            "license_artifact": "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
            "license_sha256": GOOGLETEST_LICENSE_SHA256,
        },
    }


def validate_test_status(test_name, record, artifact_dir, expected_loader_map, errors):
    required_fields = {
        "argv", "checkpoint", "cwd", "environment", "environment_mode",
        "execution_inputs_sha256_after", "execution_inputs_sha256_before",
        "exit_status", "finished_utc", "loader_map", "name", "serialized",
        "started_utc",
    }
    if set(record) != required_fields:
        errors.append(test_name + " status field inventory is not exact")
    checkpoint = "CP1" if test_name in CP1_TESTS else "CP2"
    if record.get("name") != test_name or record.get("checkpoint") != checkpoint:
        errors.append(test_name + " status identity/checkpoint is wrong")
    if record.get("serialized") is not True:
        errors.append(test_name + " was not recorded as serialized")
    if record.get("exit_status") != 0:
        errors.append(test_name + " exit status is not zero")
    if record.get("cwd") != "." or record.get("environment_mode") != "env-i":
        errors.append(test_name + " did not run from artifact root under env-i")
    if record.get("environment") != CONTROLLED_TEST_ENVIRONMENT:
        errors.append(test_name + " environment differs from the deterministic test contract")
    expected_argv = [
        "binaries/" + test_name,
        *GTEST_OPTIONS,
        "--gtest_output=xml:" + test_name + ".xml",
    ]
    if record.get("argv") != expected_argv:
        errors.append(test_name + " argv differs from the unfiltered one-shot gtest contract")
    expected_inputs = execution_input_hashes(artifact_dir, test_name, errors)
    if record.get("execution_inputs_sha256_before") != expected_inputs:
        errors.append(test_name + " pre-execution input hashes differ from artifact inputs")
    if record.get("execution_inputs_sha256_after") != expected_inputs:
        errors.append(test_name + " post-execution input hashes differ from artifact inputs")
    if record.get("loader_map") != expected_loader_map:
        errors.append(test_name + " recorded loader map differs from independent loader proof")
    validate_interval(record, errors, test_name)


def collect_test_records(artifact_dir, linkage, errors):
    records = {}
    binaries = {}
    previous_finished = None
    for test_name in ALL_TESTS:
        status_path = artifact_dir / (test_name + ".json")
        status_record = read_json(status_path, errors, status_path.name)
        expected_loader = linkage.get("executables", {}).get(test_name, {}).get("loader", {})
        validate_test_status(test_name, status_record, artifact_dir, expected_loader, errors)
        started, finished = validate_interval(status_record, [], test_name)
        if previous_finished is not None and started is not None and started < previous_finished:
            errors.append("serialized test intervals overlap or are out of order at " + test_name)
        if finished is not None:
            previous_finished = finished
        log_path = artifact_dir / (test_name + ".log")
        xml_path = artifact_dir / (test_name + ".xml")
        binary_path = artifact_dir / "binaries" / test_name
        for label, path in (("log", log_path), ("XML", xml_path), ("binary", binary_path)):
            if not path.is_file():
                errors.append("missing {} for {}: {}".format(label, test_name, path.name))
        if binary_path.is_file() and not os.access(str(binary_path), os.X_OK):
            errors.append("snapshotted test binary is not executable: " + test_name)
        record = dict(status_record)
        record.update({
            "binary": "binaries/" + test_name,
            "binary_sha256": sha256_file(binary_path) if binary_path.is_file() else None,
            "binary_size_bytes": binary_path.stat().st_size if binary_path.is_file() else None,
            "log": log_path.name,
            "log_sha256": sha256_file(log_path) if log_path.is_file() else None,
            "xml": xml_path.name,
            "xml_sha256": sha256_file(xml_path) if xml_path.is_file() else None,
        })
        records[test_name] = record
        binaries[test_name] = {
            "artifact_path": "binaries/" + test_name,
            "checkpoint": "CP1" if test_name in CP1_TESTS else "CP2",
            "sha256": record["binary_sha256"],
            "size_bytes": record["binary_size_bytes"],
        }
    library_path = artifact_dir / "binaries/libov_msckf_lib.so"
    if not library_path.is_file():
        errors.append("missing snapshotted production library")
        library = {"artifact_path": "binaries/libov_msckf_lib.so", "sha256": None, "size_bytes": None}
    else:
        library = {
            "artifact_path": "binaries/libov_msckf_lib.so",
            "sha256": sha256_file(library_path),
            "size_bytes": library_path.stat().st_size,
        }
    return records, binaries, library


def collect_dependency_inventory(
    artifact_dir, repo_root, workspace_record, linkage, errors, allow_synthetic=False
):
    record = read_json(
        artifact_dir / DEPENDENCY_INVENTORY_NAME, errors, DEPENDENCY_INVENTORY_NAME
    )
    if set(record) != {
        "ceres", "copied_dsos", "copied_test_executables", "distribution_status",
        "googletest", "independent_source_to_binary_attestation", "loader_maps",
        "required_copied_dsos", "schema_version", "third_party_notices", "threat_model",
    }:
        errors.append("dependency_inventory.json field inventory is not exact")
    if record.get("schema_version") != 1:
        errors.append("dependency inventory schema version is not 1")
    if record.get("required_copied_dsos") != SNAPSHOTTED_LIBRARY_ORDER:
        errors.append("required copied DSO order/inventory differs from the runtime contract")
    if record.get("distribution_status") != "internal_non_conveyable_staging":
        errors.append("dependency inventory distribution status is not internal staging")
    if record.get("threat_model") != "trusted_runner_local_staging":
        errors.append("dependency inventory threat model is overstated or wrong")
    if record.get("independent_source_to_binary_attestation") is not False:
        errors.append("dependency inventory must not claim independent source-to-binary attestation")

    workspace = workspace_record.get("workspace", "")
    workspace_path = Path(workspace) if isinstance(workspace, str) else Path("/")
    source_candidates = {
        "libov_msckf_lib.so": workspace_path / "devel/lib/libov_msckf_lib.so",
        "libov_core_lib.so": workspace_path / "devel/lib/libov_core_lib.so",
        "libov_init_lib.so": workspace_path / "devel/lib/libov_init_lib.so",
        "libgtest.so": workspace_path / "build/ov_msckf/gtest/lib/libgtest.so",
        "libceres.so.1": workspace_path / "ceres-install/lib/libceres.so.1",
    }
    test_source_candidates = {
        name: workspace_path / "devel/lib/ov_msckf" / name for name in ALL_TESTS
    }
    copy_fields = {
        "artifact_path", "artifact_sha256", "build_id", "exact_copy", "mode",
        "rpath", "runpath", "size_bytes", "soname", "source_path", "source_sha256",
    }

    def expected_copy_entry(name, source_candidate, elf, recorded_entry):
        artifact_path = artifact_dir / "binaries" / name
        artifact_sha = sha256_file(artifact_path) if artifact_path.is_file() else None
        try:
            canonical_source = source_candidate.resolve(strict=True)
        except OSError:
            recorded_source = recorded_entry.get("source_path")
            canonical_source = Path(recorded_source) if isinstance(recorded_source, str) else Path("/")
            if (
                not canonical_source.is_absolute()
                or ".." in canonical_source.parts
                or (canonical_source != workspace_path and workspace_path not in canonical_source.parents)
            ):
                errors.append(
                    "unretained source path is outside the recorded fresh workspace: " + name
                )
        if source_candidate.exists() and not source_candidate.is_file():
            errors.append("retained build output is not regular: " + str(source_candidate))
        if canonical_source.is_file() and artifact_sha is not None:
            if sha256_file(canonical_source) != artifact_sha:
                errors.append("artifact differs from retained fresh build output: " + name)
        return {
            "artifact_path": "binaries/" + name,
            "artifact_sha256": artifact_sha,
            "build_id": elf.get("build_id"),
            "exact_copy": True,
            "mode": "0555",
            "rpath": elf.get("rpath", []),
            "runpath": elf.get("runpath", []),
            "size_bytes": artifact_path.stat().st_size if artifact_path.is_file() else None,
            "soname": elf.get("soname"),
            "source_path": str(canonical_source),
            "source_sha256": artifact_sha,
        }

    copied = record.get("copied_dsos")
    if not isinstance(copied, list) or len(copied) != len(SNAPSHOTTED_LIBRARY_ORDER):
        errors.append("copied DSO inventory does not contain exactly five entries")
        copied = []
    for index, name in enumerate(SNAPSHOTTED_LIBRARY_ORDER):
        entry = copied[index] if index < len(copied) and isinstance(copied[index], dict) else {}
        if set(entry) != copy_fields:
            errors.append("copied DSO entry field inventory is not exact for " + name)
        elf = linkage.get("libraries", {}).get(name, {})
        expected_entry = expected_copy_entry(name, source_candidates[name], elf, entry)
        if entry != expected_entry:
            errors.append("copied DSO record differs from independent evidence for " + name)
        if elf.get("soname") != name:
            errors.append("snapshotted DSO has an unexpected or missing SONAME: " + name)

    copied_tests = record.get("copied_test_executables")
    if not isinstance(copied_tests, list) or len(copied_tests) != len(ALL_TESTS):
        errors.append("copied test executable inventory does not contain exactly thirteen entries")
        copied_tests = []
    for index, name in enumerate(ALL_TESTS):
        entry = (
            copied_tests[index]
            if index < len(copied_tests) and isinstance(copied_tests[index], dict) else {}
        )
        if set(entry) != copy_fields:
            errors.append("copied test entry field inventory is not exact for " + name)
        elf = linkage.get("executables", {}).get(name, {}).get("elf", {})
        expected_entry = expected_copy_entry(name, test_source_candidates[name], elf, entry)
        if entry != expected_entry:
            errors.append("copied test record differs from independent evidence for " + name)
        if elf.get("soname") is not None:
            errors.append("snapshotted test executable unexpectedly has a SONAME: " + name)
    expected_loader_maps = {
        name: linkage.get("executables", {}).get(name, {}).get("loader", {})
        for name in ALL_TESTS
    }
    if record.get("loader_maps") != expected_loader_maps:
        errors.append("dependency inventory loader maps differ from independent loader traces")
    artifact_ceres = artifact_dir / "binaries/libceres.so.1"
    ceres_source = workspace_path / "ceres-src"
    copied_ceres = copied[SNAPSHOTTED_LIBRARY_ORDER.index("libceres.so.1")] if copied else {}
    ceres_library_text = copied_ceres.get("source_path")
    ceres_library = Path(ceres_library_text) if isinstance(ceres_library_text, str) else Path("/")
    ceres_prefix = workspace_path / "ceres-install"
    if (
        not ceres_library.is_absolute() or ".." in ceres_library.parts
        or (ceres_library != ceres_prefix and ceres_prefix not in ceres_library.parents)
    ):
        errors.append("recorded Ceres library path is outside the fresh install prefix")
    expected_ceres = {
        "archive_artifact": "ceres_source_snapshot.tar",
        "archive_sha256": workspace_record.get("ceres_archive_sha256"),
        "snapshotted_soname": "libceres.so.1",
        "source_checkout": str(ceres_source.resolve()) if ceres_source.exists() else str(ceres_source),
        "source_commit": workspace_record.get("ceres_source_commit"),
        "source_library_path": str(ceres_library),
        "source_library_sha256": (
            sha256_file(ceres_library) if ceres_library.is_file()
            else sha256_file(artifact_ceres) if artifact_ceres.is_file() else None
        ),
        "source_tag": CERES_TAG,
    }
    if record.get("ceres") != expected_ceres:
        errors.append("Ceres provenance differs from the pinned 1.14.0 checkout/library")

    copyright_path = Path("/usr/share/doc/googletest/copyright")
    expected_googletest = {
        "archive_artifact": "googletest_source_snapshot.tar",
        "archive_sha256_after_build": GOOGLETEST_ARCHIVE_SHA256,
        "archive_sha256_before_build": GOOGLETEST_ARCHIVE_SHA256,
        "archive_size_bytes_after_build": GOOGLETEST_ARCHIVE_SIZE_BYTES,
        "archive_size_bytes_before_build": GOOGLETEST_ARCHIVE_SIZE_BYTES,
        "build_source_root": "/usr/src/googletest",
        "debian_copyright_path": str(copyright_path),
        "debian_copyright_sha256": (
            sha256_file(copyright_path) if copyright_path.is_file() else None
        ),
        "package": "googletest",
        "package_version": "1.10.0-2",
        "source_root": "/usr/src/googletest",
        "unchanged_after_build": True,
    }
    if record.get("googletest") != expected_googletest:
        errors.append("GoogleTest provenance differs from the pinned read-only 1.10.0-2 input")
    cache_path = artifact_dir / "CMakeCache.txt"
    expected_cache_line = "gtest_SOURCE_DIR:STATIC=/usr/src/googletest/googletest"
    if cache_path.is_file():
        cache_lines = cache_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if cache_lines.count(expected_cache_line) != 1:
            errors.append("CMakeCache does not bind exactly once to /usr/src/googletest")
    catkin_log = artifact_dir / CATKIN_PACKAGE_CMAKE_LOG_NAME
    if not catkin_log.is_file():
        errors.append("missing dedicated Catkin package CMake log")
    else:
        catkin_log_text = catkin_log.read_text(encoding="utf-8", errors="replace")
        normalized_lines = [
            ANSI_SGR_PATTERN.sub("", line) for line in catkin_log_text.splitlines()
        ]
        discovery_lines = [
            line for line in normalized_lines if GOOGLETEST_DISCOVERY_PREFIX in line
        ]
        if discovery_lines != [GOOGLETEST_DISCOVERY_LINE]:
            errors.append(
                "dedicated Catkin package CMake log GoogleTest discovery lines are not "
                "the exact controlled binding"
            )
        retained_log = workspace_path / "logs/ov_msckf/build.cmake.log"
        if workspace_path.is_dir():
            if not retained_log.is_file() or retained_log.is_symlink():
                errors.append("retained fresh workspace lacks its regular Catkin package CMake log")
            elif catkin_log.read_bytes() != retained_log.read_bytes():
                errors.append(
                    "dedicated Catkin package CMake log is not the exact retained workspace log"
                )

    expected_notices = {
        "ceres": {
            "artifact_path": "THIRD_PARTY_NOTICES/Ceres-LICENSE",
            "component_version": CERES_TAG,
            "sha256": CERES_LICENSE_SHA256,
            "source_path": str(ceres_source / "LICENSE"),
        },
        "googletest": {
            "artifact_path": "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
            "component_version": "1.10.0-2",
            "sha256": GOOGLETEST_LICENSE_SHA256,
            "source_path": "/usr/src/googletest/googlemock/LICENSE",
        },
    }
    if record.get("third_party_notices") != expected_notices:
        errors.append("third-party notice inventory differs from pinned source/license bytes")
    return record


def source_snapshot_contract(snapshot, expected_commit, expected_tree, errors, label):
    if snapshot.get("commit") != expected_commit:
        errors.append(label + " commit differs from the evidence commit")
    if snapshot.get("tree") != expected_tree:
        errors.append(label + " tree differs from the evidence tree")
    if snapshot.get("branch") != EXPECTED_BRANCH:
        errors.append(label + " branch differs from the CP2 branch")
    if snapshot.get("status_porcelain_v1") != []:
        errors.append(label + " records dirty source")


def git_commit_metadata(repo_root, commit, errors):
    try:
        fields = git_text(
            repo_root,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%aI%x00%cn%x00%ce%x00%cI",
            commit,
        ).split("\0")
    except subprocess.CalledProcessError as exc:
        errors.append("cannot read commit metadata: " + str(exc))
        fields = [None] * 6
    if len(fields) != 6:
        errors.append("commit metadata field count is invalid")
        fields = (fields + [None] * 6)[:6]
    return {
        "author_date": fields[2],
        "author_email": fields[1],
        "author_name": fields[0],
        "committer_date": fields[5],
        "committer_email": fields[4],
        "committer_name": fields[3],
    }


def collect_source_metadata(artifact_dir, repo_root, errors, allow_synthetic=False):
    before = read_json(artifact_dir / "source_before.json", errors, "source_before.json")
    after = read_json(artifact_dir / "source_after.json", errors, "source_after.json")
    commit = before.get("commit")
    tree = before.get("tree")
    if not isinstance(commit, str) or not HEX40_PATTERN.fullmatch(commit):
        errors.append("source commit is not a full 40-hex object ID")
        commit = ""
    if not isinstance(tree, str) or not HEX40_PATTERN.fullmatch(tree):
        errors.append("source tree is not a full 40-hex object ID")
        tree = ""
    if before != after:
        # recorded_utc is intentionally different; compare the identity fields only.
        identity_fields = ("commit", "tree", "branch", "status_porcelain_v1")
        if any(before.get(field) != after.get(field) for field in identity_fields):
            errors.append("source identity/cleanliness changed while the gate ran")
    source_snapshot_contract(before, commit, tree, errors, "source_before")
    source_snapshot_contract(after, commit, tree, errors, "source_after")
    current = {"branch": None, "commit": None, "status_porcelain_v1": None, "tree": None}
    try:
        actual_tree = git_text(repo_root, "rev-parse", commit + "^{tree}") if commit else ""
        if actual_tree != tree:
            errors.append("recorded source tree does not belong to the recorded commit")
        current = {
            "branch": git_text(repo_root, "branch", "--show-current"),
            "commit": git_text(repo_root, "rev-parse", "--verify", "HEAD"),
            "status_porcelain_v1": git_text(
                repo_root, "status", "--porcelain=v1", "--untracked-files=all"
            ).splitlines(),
            "tree": git_text(repo_root, "rev-parse", "--verify", "HEAD^{tree}"),
        }
        expected_current = {
            "branch": EXPECTED_BRANCH,
            "commit": commit,
            "status_porcelain_v1": [],
            "tree": tree,
        }
        if current != expected_current:
            errors.append("live repository HEAD/tree/branch/cleanliness differs from the evidence source")
        if not allow_synthetic:
            ancestry = subprocess.run(
                ["git", "merge-base", "--is-ancestor", CP1_AUTHORIZATION_COMMIT, commit],
                cwd=str(repo_root), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            ) if commit else None
            if ancestry is None or ancestry.returncode != 0:
                errors.append("evidence commit is not descended from the CP1 authorization commit")
    except subprocess.CalledProcessError as exc:
        errors.append("recorded source commit is unavailable: " + str(exc))
    input_hashes = {}
    if commit:
        for relative in sorted(SOURCE_INPUTS):
            try:
                input_hashes[relative] = sha256_bytes(committed_blob(repo_root, commit, relative))
            except subprocess.CalledProcessError as exc:
                errors.append("cannot hash committed source {}: {}".format(relative, exc))
    config_hashes = {name: input_hashes.get(name) for name in sorted(CONFIG_INPUTS)}
    for relative, expected in FROZEN_CONFIG_SHA256.items():
        if config_hashes.get(relative) != expected:
            errors.append("frozen configuration hash mismatch: " + relative)
    contract_hashes = {name: input_hashes.get(name) for name in sorted(CONTRACT_INPUTS)}
    archive_path = artifact_dir / SOURCE_ARCHIVE_NAME
    archive_record = {
        "archive_roots": list(ARCHIVE_ROOTS),
        "artifact_path": SOURCE_ARCHIVE_NAME,
        "sha256": sha256_file(archive_path) if archive_path.is_file() else None,
        "size_bytes": archive_path.stat().st_size if archive_path.is_file() else None,
    }
    if commit:
        try:
            expected_archive = subprocess.check_output(
                ["git", "archive", "--format=tar", commit, "--", *ARCHIVE_ROOTS],
                cwd=str(repo_root), stderr=subprocess.STDOUT,
            )
            archive_record["expected_sha256"] = sha256_bytes(expected_archive)
            archive_record["expected_size_bytes"] = len(expected_archive)
            if (
                archive_record["sha256"] != archive_record["expected_sha256"]
                or archive_record["size_bytes"] != archive_record["expected_size_bytes"]
            ):
                errors.append("source_snapshot.tar differs from the exact committed archive roots")
        except subprocess.CalledProcessError as exc:
            archive_record["expected_sha256"] = None
            archive_record["expected_size_bytes"] = None
            errors.append("cannot reproduce source_snapshot.tar: " + str(exc))
    else:
        archive_record["expected_sha256"] = None
        archive_record["expected_size_bytes"] = None

    if archive_path.is_file():
        archived_input_hashes = {}
        try:
            with tarfile.open(str(archive_path), mode="r:") as archive:
                seen_names = set()
                for member in archive.getmembers():
                    pure = PurePosixPath(member.name)
                    if pure.is_absolute() or ".." in pure.parts or member.name in seen_names:
                        errors.append("source archive has an unsafe or duplicate member: " + member.name)
                        continue
                    seen_names.add(member.name)
                    if member.issym() or member.islnk():
                        errors.append("source archive links are forbidden: " + member.name)
                    if member.name in SOURCE_INPUTS:
                        if not member.isfile():
                            errors.append("source archive input is not regular: " + member.name)
                            continue
                        stream = archive.extractfile(member)
                        archived_input_hashes[member.name] = (
                            sha256_bytes(stream.read()) if stream is not None else None
                        )
        except (OSError, tarfile.TarError) as exc:
            errors.append("cannot inspect source_snapshot.tar: " + str(exc))
        if set(archived_input_hashes) != SOURCE_INPUTS:
            errors.append("source archive does not contain every curated SOURCE_INPUTS file")
        for relative in SOURCE_INPUTS:
            if archived_input_hashes.get(relative) != input_hashes.get(relative):
                errors.append("source archive byte mismatch: " + relative)
        archive_record["input_sha256"] = archived_input_hashes
    else:
        archive_record["input_sha256"] = {}

    verifier_relative = "scripts/cp2/verify_report.py"
    try:
        committed_verifier_sha256 = sha256_bytes(committed_blob(repo_root, commit, verifier_relative))
    except subprocess.CalledProcessError as exc:
        committed_verifier_sha256 = None
        errors.append("cannot read the committed CP2 verifier: " + str(exc))
    running_verifier_sha256 = sha256_file(Path(__file__).resolve())
    if committed_verifier_sha256 != running_verifier_sha256:
        errors.append("running verifier bytes differ from the verifier committed at the evidence commit")
    return {
        "after": after,
        "before": before,
        "branch": before.get("branch"),
        "commit": commit,
        "commit_metadata": git_commit_metadata(repo_root, commit, errors) if commit else {},
        "configuration_sha256": config_hashes,
        "contract_sha256": contract_hashes,
        "current_repository": current,
        "dirty": bool(before.get("status_porcelain_v1")) or bool(after.get("status_porcelain_v1")),
        "input_sha256": input_hashes,
        "source_archive": archive_record,
        "tree": tree,
        "verifier": {
            "committed_sha256": committed_verifier_sha256,
            "running_sha256": running_verifier_sha256,
        },
    }


def parse_cmake_compiler(cache_path, errors):
    if not cache_path.is_file():
        errors.append("missing CMakeCache.txt")
        return {}
    compiler = None
    for line in cache_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("CMAKE_CXX_COMPILER:FILEPATH="):
            compiler = line.split("=", 1)[1]
            break
    if not compiler:
        errors.append("CMakeCache.txt does not identify CMAKE_CXX_COMPILER")
        return {}
    compiler_path = Path(compiler)
    if not compiler_path.is_file():
        errors.append("recorded C++ compiler is unavailable: " + compiler)
        return {"path": compiler}
    try:
        version = subprocess.check_output(
            [str(compiler_path), "--version"], text=True, stderr=subprocess.STDOUT
        ).rstrip("\n")
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append("cannot query C++ compiler version: " + str(exc))
        version = None
    resolved = compiler_path.resolve()
    return {
        "path": str(compiler_path),
        "resolved_path": str(resolved),
        "sha256": sha256_file(resolved),
        "version": version,
    }


def os_release_record():
    path = Path("/etc/os-release")
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    return {
        "fields": values,
        "path": str(path),
        "sha256": sha256_file(path) if path.is_file() else None,
    }


def collect_host(cache_path, errors):
    return {
        "architecture": platform.machine(),
        "compiler": parse_cmake_compiler(cache_path, errors),
        "hostname": socket.gethostname(),
        "logical_cpu_count": os.cpu_count(),
        "os_release": os_release_record(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uname": list(platform.uname()),
    }


def controlled_runtime_environment(binary_dir):
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "LD_LIBRARY_PATH": str(binary_dir.resolve()) + ":/opt/ros/noetic/lib",
        "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "PATH": "/usr/bin:/bin",
        "TZ": "UTC",
    }


def readelf_record(path, errors, label):
    record = {
        "build_id": None,
        "elf": False,
        "machine": None,
        "needed": [],
        "rpath": [],
        "runpath": [],
        "soname": None,
        "sha256": sha256_file(path) if path.is_file() else None,
        "size_bytes": path.stat().st_size if path.is_file() else None,
        "type": None,
    }
    if not path.is_file():
        errors.append("missing ELF input: " + label)
        return record
    try:
        with path.open("rb") as stream:
            record["elf"] = stream.read(4) == b"\x7fELF"
    except OSError as exc:
        errors.append("cannot read ELF input {}: {}".format(label, exc))
        return record
    if not record["elf"]:
        errors.append(label + " is not an ELF object")
        return record
    readelf = shutil.which("readelf")
    if not readelf:
        errors.append("readelf is unavailable for ELF verification")
        return record
    try:
        header = subprocess.check_output(
            [readelf, "-h", str(path)], text=True, stderr=subprocess.STDOUT,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
        dynamic = subprocess.check_output(
            [readelf, "-d", str(path)], text=True, stderr=subprocess.STDOUT,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
        notes = subprocess.check_output(
            [readelf, "-n", str(path)], text=True, stderr=subprocess.STDOUT,
            env={"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        errors.append("readelf failed for {}: {}".format(label, exc))
        return record
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.startswith("Type:"):
            record["type"] = stripped.split(":", 1)[1].strip().split()[0]
        elif stripped.startswith("Machine:"):
            record["machine"] = stripped.split(":", 1)[1].strip()
    record["needed"] = sorted(set(re.findall(r"Shared library: \[([^]]+)\]", dynamic)))
    record["rpath"] = sorted(set(re.findall(r"Library rpath: \[([^]]*)\]", dynamic)))
    record["runpath"] = sorted(set(re.findall(r"Library runpath: \[([^]]*)\]", dynamic)))
    sonames = re.findall(r"Library soname: \[([^]]+)\]", dynamic)
    record["soname"] = sonames[0] if len(sonames) == 1 else None
    build_ids = re.findall(r"Build ID: ([0-9a-f]+)", notes)
    if len(build_ids) != 1:
        errors.append(label + " does not contain exactly one GNU build ID")
    else:
        record["build_id"] = build_ids[0]
    return record


def loader_map(path, binary_dir, errors, label):
    environment = controlled_runtime_environment(binary_dir)
    ldd = shutil.which("ldd")
    if not ldd:
        errors.append("ldd is unavailable for loader verification")
        return {}
    try:
        completed = subprocess.run(
            [ldd, str(path)], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=environment, cwd=str(binary_dir.parent), timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        errors.append("loader trace failed for {}: {}".format(label, exc))
        return {}
    if completed.returncode != 0:
        errors.append("loader trace returned nonzero for {}".format(label))
    result = {}
    artifact_root = binary_dir.parent.resolve()
    for raw in completed.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if " => not found" in line:
            errors.append("{} has unresolved loader input: {}".format(label, line))
            continue
        if " => " in line:
            soname, remainder = line.split(" => ", 1)
            resolved_text = remainder.rsplit(" (", 1)[0]
        elif line.startswith("linux-vdso"):
            soname = line.split(None, 1)[0]
            result[soname] = {
                "artifact_path": None,
                "resolved_path": "linux-vdso",
                "sha256": None,
            }
            continue
        else:
            resolved_text = line.rsplit(" (", 1)[0]
            soname = Path(resolved_text).name
        resolved = Path(resolved_text)
        if not resolved.is_absolute():
            resolved = artifact_root / resolved
        try:
            canonical = resolved.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            errors.append("cannot resolve loader input for {}: {}".format(label, exc))
            continue
        try:
            artifact_path = canonical.relative_to(artifact_root).as_posix()
            resolved_record = artifact_path
        except ValueError:
            artifact_path = None
            resolved_record = str(canonical)
        if artifact_path is not None:
            if artifact_path not in {"binaries/" + name for name in SNAPSHOTTED_LIBRARIES}:
                errors.append("{} loads an unexpected artifact-local object: {}".format(label, artifact_path))
        else:
            allowed_roots = [Path("/usr/lib"), Path("/lib"), Path("/opt/ros/noetic/lib")]
            if not any(
                canonical == root or root in canonical.parents for root in allowed_roots
            ):
                errors.append(
                    "{} loads a non-artifact object outside approved system/ROS roots: {}".format(
                        label, canonical
                    )
                )
        if soname in result:
            errors.append("{} loader map contains duplicate SONAME {}".format(label, soname))
        result[soname] = {
            "artifact_path": artifact_path,
            "resolved_path": resolved_record,
            "sha256": sha256_file(canonical),
        }
    return {name: result[name] for name in sorted(result)}


def collect_elf_linkage(artifact_dir, errors):
    binary_dir = artifact_dir / "binaries"
    libraries = {}
    for name in sorted(SNAPSHOTTED_LIBRARIES):
        path = binary_dir / name
        libraries[name] = readelf_record(path, errors, "binaries/" + name)
        for field in ("rpath", "runpath"):
            for value in libraries[name].get(field, []):
                entries = value.split(":")
                if any(
                    entry not in {"$ORIGIN", "${ORIGIN}", "/opt/ros/noetic/lib"}
                    for entry in entries
                ):
                    errors.append("binaries/{} has an escaping {}: {}".format(name, field, value))
    executables = {}
    aggregate_resolved = set()
    for test_name in ALL_TESTS:
        path = binary_dir / test_name
        elf = readelf_record(path, errors, "binaries/" + test_name)
        for field in ("rpath", "runpath"):
            for value in elf.get(field, []):
                entries = value.split(":")
                if any(
                    entry not in {"$ORIGIN", "${ORIGIN}", "/opt/ros/noetic/lib"}
                    for entry in entries
                ):
                    errors.append("{} has an escaping {}: {}".format(test_name, field, value))
        loader = loader_map(path, binary_dir, errors, test_name) if elf.get("elf") else {}
        resolved_names = {
            name for name, record in loader.items()
            if name in SNAPSHOTTED_LIBRARIES
            and isinstance(record, dict)
            and record.get("artifact_path") == "binaries/" + name
        }
        aggregate_resolved.update(resolved_names)
        if "libgtest.so" not in resolved_names:
            errors.append(test_name + " does not load the snapshotted libgtest.so")
        if test_name in TESTS_REQUIRING_PRODUCTION and "libov_msckf_lib.so" not in resolved_names:
            errors.append(test_name + " does not load the snapshotted production library")
        executables[test_name] = {"elf": elf, "loader": loader}
    if aggregate_resolved != SNAPSHOTTED_LIBRARIES:
        errors.append(
            "aggregate loader proof does not resolve every snapshotted library: expected {} got {}".format(
                sorted(SNAPSHOTTED_LIBRARIES), sorted(aggregate_resolved)
            )
        )
    return {
        "aggregate_resolved_snapshot_libraries": sorted(aggregate_resolved),
        "executables": executables,
        "libraries": libraries,
    }


def execution_input_hashes(artifact_dir, test_name, errors=None):
    paths = ["binaries/" + test_name]
    paths.extend("binaries/" + name for name in sorted(SNAPSHOTTED_LIBRARIES))
    result = {}
    for relative in paths:
        path = artifact_dir / relative
        if not path.is_file():
            if errors is not None:
                errors.append("missing execution input: " + relative)
            result[relative] = None
        else:
            result[relative] = sha256_file(path)
    return result


def rerun_snapshotted_tests(artifact_dir, errors):
    binary_dir = artifact_dir / "binaries"
    before = {
        test_name: execution_input_hashes(artifact_dir, test_name) for test_name in ALL_TESTS
    }
    exit_statuses = {}
    with tempfile.TemporaryDirectory(prefix="cp2-independent-rerun-", dir="/tmp") as temporary:
        output_root = Path(temporary)
        environment = controlled_runtime_environment(binary_dir)
        for test_name in ALL_TESTS:
            xml_path = output_root / (test_name + ".xml")
            argv = [
                str((binary_dir / test_name).resolve()),
                *GTEST_OPTIONS,
                "--gtest_output=xml:" + str(xml_path),
            ]
            try:
                completed = subprocess.run(
                    argv, cwd=str(output_root), env=environment, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, timeout=300, check=False,
                )
                exit_statuses[test_name] = completed.returncode
                (output_root / (test_name + ".log")).write_text(
                    completed.stdout, encoding="utf-8"
                )
                if completed.returncode != 0:
                    errors.append("independent rerun failed: " + test_name)
            except (OSError, subprocess.TimeoutExpired) as exc:
                exit_statuses[test_name] = None
                errors.append("independent rerun could not execute {}: {}".format(test_name, exc))
                (output_root / (test_name + ".log")).write_text("", encoding="utf-8")
            if not xml_path.is_file():
                errors.append("independent rerun did not create XML: " + test_name)
        gtest = parse_gtest_xml(output_root, errors)
        summaries = parse_summaries(output_root, errors)
    after = {
        test_name: execution_input_hashes(artifact_dir, test_name) for test_name in ALL_TESTS
    }
    if before != after:
        errors.append("snapshotted execution inputs changed during independent rerun")
    return {
        "environment": {
            **environment,
            "LD_LIBRARY_PATH": "binaries:/opt/ros/noetic/lib",
        },
        "exit_statuses": exit_statuses,
        "gtest": gtest,
        "input_sha256_after": after,
        "input_sha256_before": before,
        "summaries": summaries,
    }


def expected_pre_report_files():
    files = expected_artifact_files()
    files.remove(REPORT_NAME)
    return files


def collect_verifier_self_test(artifact_dir, repo_root, errors):
    record = read_json(artifact_dir / "verifier_self_test.json", errors, "verifier_self_test.json")
    required_fields = {
        "argv", "cwd", "environment", "environment_mode", "exit_status", "finished_utc",
        "name", "started_utc", "synthetic_artifact_root_policy",
    }
    if set(record) != required_fields:
        errors.append("verifier_self_test.json field inventory is not exact")
    expected = {
        "argv": [
            "/usr/bin/python3", str(repo_root / "scripts/cp2/verify_report.py"), "--self-test"
        ],
        "cwd": ".",
        "environment": {
            "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": "0", "TZ": "UTC",
        },
        "environment_mode": "env-i",
        "exit_status": 0,
        "name": "verifier_self_test",
        "synthetic_artifact_root_policy": (
            "tempfile.TemporaryDirectory_outside_repository_results"
        ),
    }
    for key, value in expected.items():
        if record.get(key) != value:
            errors.append("verifier self-test {} differs from the exact contract".format(key))
    validate_interval(record, errors, "verifier_self_test")
    log_path = artifact_dir / "verifier_self_test.log"
    if not log_path.is_file():
        errors.append("missing verifier self-test log")
        log_text = ""
    else:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if "CP2 verifier self-test passed using only: /tmp/" not in log_text:
        errors.append("verifier self-test log lacks its successful /tmp-only execution claim")
    if "Synthetic corruptions rejected:" not in log_text:
        errors.append("verifier self-test log lacks negative-corruption results")
    enriched = dict(record)
    enriched.update({
        "log": "verifier_self_test.log",
        "log_sha256": sha256_file(log_path) if log_path.is_file() else None,
    })
    return enriched


def validate_record_timeline(source, build_records, test_records, self_test, errors):
    before = parse_utc_timestamp(
        source.get("before", {}).get("recorded_utc"), errors, "source_before.recorded_utc"
    )
    after = parse_utc_timestamp(
        source.get("after", {}).get("recorded_utc"), errors, "source_after.recorded_utc"
    )
    self_finished = parse_utc_timestamp(
        self_test.get("finished_utc"), errors, "verifier_self_test.finished_utc"
    )
    build_started = [
        parse_utc_timestamp(record.get("started_utc"), [], "") for record in build_records
    ]
    build_finished = [
        parse_utc_timestamp(record.get("finished_utc"), [], "") for record in build_records
    ]
    tests = list(test_records.values())
    test_started = [parse_utc_timestamp(record.get("started_utc"), [], "") for record in tests]
    test_finished = [parse_utc_timestamp(record.get("finished_utc"), [], "") for record in tests]
    if self_finished is not None and before is not None and self_finished > before:
        errors.append("verifier self-test did not finish before source_before")
    if build_started and before is not None and build_started[0] is not None and before > build_started[0]:
        errors.append("source_before was recorded after the build began")
    if (
        build_finished and test_started and build_finished[-1] is not None
        and test_started[0] is not None and build_finished[-1] > test_started[0]
    ):
        errors.append("serialized tests began before the serialized build finished")
    if test_finished and after is not None and test_finished[-1] is not None and test_finished[-1] > after:
        errors.append("source_after was recorded before serialized tests finished")
    if before is not None and after is not None and after < before:
        errors.append("source snapshot timeline is reversed")


def artifact_policy():
    return {
        "dataset_or_bag_accessed": False,
        "distribution_status": "internal_non_conveyable_staging",
        "eligible_for_cp2_seal": False,
        "finalization": "read_only_staging_finalization_only",
        "no_overwrite": True,
        "scope": "cp2_a_b_unit_only",
        "serialized_build_and_tests": True,
        "stage": "staging_pending_cp2_c_cp2_d_cp2_e",
        "trust_model": "trusted_committed_runner_not_malicious_forgery_resistant",
    }


def expected_checkpoint_status(passed=True):
    return {
        "CP2-A": "passed" if passed else "not_established",
        "CP2-B": "passed" if passed else "not_established",
        "CP2-C": "not_run",
        "CP2-D": "not_run",
        "CP2-E": "not_run_blocked_pending_fixed_clock_profile",
    }


def assemble_unit_report(artifact_dir, repo_root, allow_synthetic=False):
    artifact_dir = artifact_dir.resolve()
    repo_root = repo_root.resolve()
    if not artifact_dir.is_dir():
        raise ValueError("artifact directory does not exist: " + str(artifact_dir))
    if (artifact_dir / REPORT_NAME).exists() or (artifact_dir / MANIFEST_NAME).exists():
        raise ValueError("report/manifest already exists; refusing overwrite")
    errors = []
    actual_before_report = regular_artifact_files(artifact_dir, errors)
    if actual_before_report != expected_pre_report_files():
        errors.append(
            "pre-report artifact inventory mismatch: expected {} got {}".format(
                sorted(expected_pre_report_files()), sorted(actual_before_report)
            )
        )
    source = collect_source_metadata(
        artifact_dir, repo_root, errors, allow_synthetic=allow_synthetic
    )
    workspace = collect_workspace_record(
        artifact_dir, repo_root, source, errors, allow_synthetic=allow_synthetic
    )
    third_party_sources_and_notices = collect_third_party_sources_and_notices(
        artifact_dir, repo_root, workspace, errors, allow_synthetic=allow_synthetic
    )
    build_records = collect_build_records(
        artifact_dir, repo_root, workspace, source.get("commit", ""), errors
    )
    self_test = collect_verifier_self_test(artifact_dir, repo_root, errors)
    gtest = parse_gtest_xml(artifact_dir, errors)
    summaries = parse_summaries(artifact_dir, errors)
    host = collect_host(artifact_dir / "CMakeCache.txt", errors)
    strict_fp = analyze_compile_commands(
        artifact_dir / "compile_commands.json", Path(workspace.get("source_root", repo_root)),
        errors, host.get("compiler")
    )
    dependency_eigen_abi = analyze_dependency_eigen_abi(
        artifact_dir,
        Path(workspace.get("source_root", repo_root)),
        Path(workspace.get("workspace_build_root", "")),
        errors,
    )
    linkage = collect_elf_linkage(artifact_dir, errors)
    dependency_inventory = collect_dependency_inventory(
        artifact_dir, repo_root, workspace, linkage, errors,
        allow_synthetic=allow_synthetic,
    )
    test_records, binaries, production_library = collect_test_records(
        artifact_dir, linkage, errors
    )
    independent_reexecution = rerun_snapshotted_tests(artifact_dir, errors)
    if independent_reexecution.get("gtest") != gtest:
        errors.append("independent re-execution gtest results differ from captured XML")
    if independent_reexecution.get("summaries") != summaries:
        errors.append("independent re-execution summaries differ from captured logs")
    validate_record_timeline(source, build_records, test_records, self_test, errors)
    passed = not errors
    report = {
        "artifact_policy": artifact_policy(),
        "binary_sha256": binaries,
        "build": {
            "cmake_cache_sha256": (
                sha256_file(artifact_dir / "CMakeCache.txt")
                if (artifact_dir / "CMakeCache.txt").is_file() else None
            ),
            "commands": build_records,
            "serialized": True,
        },
        "checkpoint": "CP2-A/B-unit",
        "checkpoint_status": expected_checkpoint_status(passed),
        "dependency_eigen_abi": dependency_eigen_abi,
        "dependency_inventory": dependency_inventory,
        "eligible_for_cp2_seal": False,
        "elf_and_linkage": linkage,
        "evidence_class": "trusted_runner_local_staging_evidence",
        "evidence_scope": EVIDENCE_SCOPE,
        "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "gtest": gtest,
        "host": host,
        "independent_source_to_binary_attestation": False,
        "independent_reexecution": independent_reexecution,
        "integrity": {
            "sha256sums_claim": (
                "internal_consistency_only_until_external_digest_is_retained_and_supplied"
            )
        },
        "overall_checkpoint_status": OVERALL_STATUS,
        "production_library": production_library,
        "schema_version": 1,
        "source": source,
        "status": "passed_cp2_a_b_unit_only" if passed else "failed_cp2_a_b_unit",
        "strict_floating_point": strict_fp,
        "summaries": summaries,
        "test_invocations": test_records,
        "third_party_sources_and_notices": third_party_sources_and_notices,
        "validation_errors": errors,
        "verifier_self_test": self_test,
        "workspace": workspace,
    }
    atomic_write_json(artifact_dir / REPORT_NAME, report)
    generate_manifest(artifact_dir)
    print("CP2 unit report {}: {}".format(report["status"], artifact_dir / REPORT_NAME))
    return 0


def require_report_fields(report, errors):
    required = {
        "artifact_policy",
        "binary_sha256",
        "build",
        "checkpoint",
        "checkpoint_status",
        "dependency_eigen_abi",
        "dependency_inventory",
        "eligible_for_cp2_seal",
        "elf_and_linkage",
        "evidence_class",
        "evidence_scope",
        "generated_utc",
        "gtest",
        "host",
        "independent_source_to_binary_attestation",
        "independent_reexecution",
        "integrity",
        "overall_checkpoint_status",
        "production_library",
        "schema_version",
        "source",
        "status",
        "strict_floating_point",
        "summaries",
        "test_invocations",
        "third_party_sources_and_notices",
        "validation_errors",
        "verifier_self_test",
        "workspace",
    }
    if set(report) != required:
        errors.append(
            "report field inventory differs: expected {} got {}".format(
                sorted(required), sorted(report)
            )
        )


def verify_unit_report(
    artifact_dir, repo_root, quiet=False, expected_manifest_sha256=None,
    require_finalized=True, allow_synthetic=False,
):
    artifact_dir = artifact_dir.resolve()
    repo_root = repo_root.resolve()
    errors = []
    verify_manifest(artifact_dir, errors)
    anchor = validate_manifest_anchor(artifact_dir, expected_manifest_sha256, errors)
    actual_files = regular_artifact_files(artifact_dir, errors, finalized=require_finalized)
    if actual_files != expected_artifact_files():
        errors.append(
            "passing artifact inventory mismatch: expected {} got {}".format(
                sorted(expected_artifact_files()), sorted(actual_files)
            )
        )
    report = read_json(artifact_dir / REPORT_NAME, errors, REPORT_NAME)
    require_report_fields(report, errors)
    if report.get("schema_version") != 1:
        errors.append("unsupported CP2 report schema (expected 1)")
    if report.get("checkpoint") != "CP2-A/B-unit":
        errors.append("report checkpoint is not the CP2-A/B unit sub-gate")
    if report.get("evidence_scope") != EVIDENCE_SCOPE:
        errors.append("report evidence_scope is not the CP2-A/B unit scope")
    if report.get("overall_checkpoint_status") != OVERALL_STATUS:
        errors.append("report incorrectly changes the overall CP2 status")
    if report.get("status") != "passed_cp2_a_b_unit_only":
        errors.append("report does not record a passed CP2-A/B unit run")
    if report.get("eligible_for_cp2_seal") is not False:
        errors.append("CP2-A/B unit evidence must be ineligible for CP2 sealing")
    if report.get("evidence_class") != "trusted_runner_local_staging_evidence":
        errors.append("report evidence class overstates the local trusted-runner scope")
    if report.get("independent_source_to_binary_attestation") is not False:
        errors.append("report must not claim independent source-to-binary attestation")
    if report.get("validation_errors") != []:
        errors.append("report contains validation errors")
    if report.get("checkpoint_status") != expected_checkpoint_status(True):
        errors.append("checkpoint status must pass only CP2-A/B and leave CP2-C/D/E unpassed")
    if report.get("artifact_policy") != artifact_policy():
        errors.append("artifact policy differs from the unit-only no-overwrite contract")
    if report.get("integrity") != {
        "sha256sums_claim": (
            "internal_consistency_only_until_external_digest_is_retained_and_supplied"
        )
    }:
        errors.append("report overstates or misstates SHA256SUMS integrity")
    parse_utc_timestamp(report.get("generated_utc"), errors, "report.generated_utc")

    independent_source = collect_source_metadata(
        artifact_dir, repo_root, errors, allow_synthetic=allow_synthetic
    )
    if report.get("source") != independent_source:
        errors.append("reported source provenance differs from committed Git evidence")
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    if source.get("dirty") is not False:
        errors.append("report source provenance is dirty")
    if set(source.get("input_sha256", {})) != SOURCE_INPUTS:
        errors.append("source hash inventory differs from the frozen CP2-A/B inventory")
    if source.get("configuration_sha256") != {
        name: FROZEN_CONFIG_SHA256[name] for name in sorted(CONFIG_INPUTS)
    }:
        errors.append("reported frozen configuration hashes are wrong")
    if set(source.get("contract_sha256", {})) != CONTRACT_INPUTS:
        errors.append("contract hash inventory differs from the CP2 contract")

    independent_workspace = collect_workspace_record(
        artifact_dir, repo_root, independent_source, errors,
        allow_synthetic=allow_synthetic,
    )
    if report.get("workspace") != independent_workspace:
        errors.append("reported fresh workspace differs from workspace.json")
    independent_third_party = collect_third_party_sources_and_notices(
        artifact_dir, repo_root, independent_workspace, errors,
        allow_synthetic=allow_synthetic,
    )
    if report.get("third_party_sources_and_notices") != independent_third_party:
        errors.append("reported third-party source/license evidence differs from artifacts")
    independent_build = collect_build_records(
        artifact_dir, repo_root, independent_workspace,
        independent_source.get("commit", ""), errors,
    )
    build = report.get("build") if isinstance(report.get("build"), dict) else {}
    if build.get("serialized") is not True or build.get("commands") != independent_build:
        errors.append("reported build commands/results differ from captured serialized records")
    cache_path = artifact_dir / "CMakeCache.txt"
    actual_cache_hash = sha256_file(cache_path) if cache_path.is_file() else None
    if build.get("cmake_cache_sha256") != actual_cache_hash:
        errors.append("reported CMakeCache.txt hash is wrong")

    independent_self_test = collect_verifier_self_test(artifact_dir, repo_root, errors)
    if report.get("verifier_self_test") != independent_self_test:
        errors.append("verifier self-test evidence is missing, failed, or misreported")

    independent_gtest = parse_gtest_xml(artifact_dir, errors)
    if report.get("gtest") != independent_gtest:
        errors.append("reported gtest totals/cases differ from independently parsed XML")
    expected_total = sum(ALL_TESTS.values())
    if (
        independent_gtest.get("tests"),
        independent_gtest.get("failures"),
        independent_gtest.get("errors"),
        independent_gtest.get("disabled"),
    ) != (expected_total, 0, 0, 0):
        errors.append(
            f"gtest totals do not prove a clean {expected_total}-test CP1+CP2 run"
        )

    independent_summaries = parse_summaries(artifact_dir, errors)
    if report.get("summaries") != independent_summaries:
        errors.append("reported counters/fixtures differ from captured test logs")
    independent_host = collect_host(cache_path, errors)
    if report.get("host") != independent_host:
        errors.append("reported host/compiler evidence differs from the current verified host")
    independent_fp = analyze_compile_commands(
        artifact_dir / "compile_commands.json",
        Path(independent_workspace.get("source_root", repo_root)), errors,
        independent_host.get("compiler"),
    )
    if report.get("strict_floating_point") != independent_fp:
        errors.append("reported strict-FP evidence differs from compile_commands.json")
    if independent_fp.get("passed") is not True:
        errors.append("effective strict-FP evidence did not pass")

    independent_dependency_eigen_abi = analyze_dependency_eigen_abi(
        artifact_dir,
        Path(independent_workspace.get("source_root", repo_root)),
        Path(independent_workspace.get("workspace_build_root", "")),
        errors,
    )
    if report.get("dependency_eigen_abi") != independent_dependency_eigen_abi:
        errors.append(
            "reported dependency Eigen ABI evidence differs from dependency compile commands"
        )
    if independent_dependency_eigen_abi.get("passed") is not True:
        errors.append("effective dependency Eigen ABI evidence did not pass")

    independent_linkage = collect_elf_linkage(artifact_dir, errors)
    if report.get("elf_and_linkage") != independent_linkage:
        errors.append("reported ELF/linkage evidence differs from independent inspection")
    independent_dependencies = collect_dependency_inventory(
        artifact_dir, repo_root, independent_workspace, independent_linkage, errors,
        allow_synthetic=allow_synthetic,
    )
    if report.get("dependency_inventory") != independent_dependencies:
        errors.append("reported dependency inventory differs from captured dependency evidence")
    independent_tests, independent_binaries, independent_library = collect_test_records(
        artifact_dir, independent_linkage, errors
    )
    if report.get("test_invocations") != independent_tests:
        errors.append("reported per-test commands/status/log hashes differ from captured records")
    if report.get("binary_sha256") != independent_binaries:
        errors.append("reported CP2/CP1 binary SHA-256 inventory is wrong")
    if len({name for name in independent_binaries if name in CP2_TESTS}) != len(CP2_TESTS):
        errors.append(
            f"binary evidence does not contain exactly {len(CP2_TESTS)} CP2 test executables"
        )
    if len({name for name in independent_binaries if name in CP1_TESTS}) != 4:
        errors.append("binary evidence does not contain exactly four CP1 test executables")
    if report.get("production_library") != independent_library:
        errors.append("reported production ov_msckf library hash is wrong")

    rerun = rerun_snapshotted_tests(artifact_dir, errors)
    if report.get("independent_reexecution") != rerun:
        errors.append("reported independent re-execution differs from a fresh re-execution")
    if rerun.get("gtest") != independent_gtest or rerun.get("summaries") != independent_summaries:
        errors.append("fresh re-execution differs from captured XML/log semantics")
    validate_record_timeline(
        independent_source, independent_build, independent_tests,
        independent_self_test, errors,
    )

    host = independent_host
    required_host_fields = {
        "architecture", "compiler", "hostname", "logical_cpu_count", "os_release",
        "platform", "python", "uname"
    }
    if set(host) != required_host_fields:
        errors.append("host/OS/compiler field inventory is incomplete")
    compiler = host.get("compiler") if isinstance(host.get("compiler"), dict) else {}
    required_compiler_fields = {"path", "resolved_path", "sha256", "version"}
    if set(compiler) != required_compiler_fields or not HEX64_PATTERN.fullmatch(str(compiler.get("sha256", ""))):
        errors.append("compiler identity/version/hash evidence is incomplete")
    os_release = host.get("os_release") if isinstance(host.get("os_release"), dict) else {}
    if not os_release.get("fields") or not HEX64_PATTERN.fullmatch(str(os_release.get("sha256", ""))):
        errors.append("OS release evidence is incomplete")

    if errors:
        if not quiet:
            for error in errors:
                print("ERROR: " + error)
        return 1, errors
    if not quiet:
        print("CP2-A/B unit evidence verified: " + str(artifact_dir))
        print("SHA256SUMS SHA-256: " + str(anchor["sha256"]))
        if anchor["claim"] == "external_sha256_anchor_matched":
            print("External SHA256SUMS digest anchor matched.")
        else:
            print("Integrity scope: internal consistency only; no external digest anchor supplied.")
        print("This staging artifact is not eligible for a CP2 seal.")
        print(
            "Evidence class: trusted runner local staging; independent source-to-binary "
            "attestation is false."
        )
        print("Distribution status: internal non-conveyable staging.")
        print("CP2-C, CP2-D, and CP2-E remain unexecuted and unpassed.")
    return 0, []


def atomic_rename_noreplace(source, destination):
    source = source.absolute()
    destination = destination.absolute()
    if source.is_symlink() or not source.is_dir():
        raise OSError(errno.ENOENT, "source staging directory does not exist", str(source))
    source_parent = source.parent.resolve(strict=True)
    canonical_source = source_parent / source.name
    if canonical_source != source:
        raise OSError(errno.EINVAL, "source path is not canonical", str(source))
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise OSError(errno.ENOENT, "destination parent does not exist", str(destination.parent))
    destination_parent = destination.parent.resolve(strict=True)
    destination = destination_parent / destination.name
    if os.path.lexists(str(destination)):
        raise OSError(errno.EEXIST, "destination already exists", str(destination))
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for atomic no-overwrite finalization")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    at_fdcwd = -100
    rename_noreplace = 1
    result = renameat2(
        at_fdcwd,
        os.fsencode(str(canonical_source)),
        at_fdcwd,
        os.fsencode(str(destination)),
        rename_noreplace,
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def fsync_path(path, directory=False):
    flags = os.O_RDONLY
    if directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def finalize_staging_noreplace(source, destination, repo_root=None, allow_synthetic=False):
    repo_root = (repo_root or Path(__file__).resolve().parents[2]).resolve()
    source = source.absolute()
    destination = destination.absolute()
    expected_parent = (repo_root / "results/staging/cp2/unit").resolve()
    if not allow_synthetic:
        if source.parent.absolute() != expected_parent or not source.parent.is_dir():
            raise ValueError("source is outside the CP2-A/B staging parent")
        if destination.parent.absolute() != expected_parent or not destination.parent.is_dir():
            raise ValueError("destination is outside the CP2-A/B staging parent")
        if not source.name.startswith(".cp2_unit_") or ".partial." not in source.name:
            raise ValueError("source is not a CP2 partial staging directory")
        if not destination.name.startswith("cp2_unit_"):
            raise ValueError("destination is not a CP2 staging run directory")
        source_run_id = source.name[1:].split(".partial.", 1)[0]
        if source_run_id != destination.name:
            raise ValueError("source/destination do not identify the same CP2 staging run")
    status, errors = verify_unit_report(
        source, repo_root, quiet=True, require_finalized=False,
        allow_synthetic=allow_synthetic,
    )
    if status != 0:
        raise ValueError("refusing to finalize invalid staging evidence: " + "; ".join(errors))
    inventory_errors = []
    if regular_artifact_files(source, inventory_errors) != expected_artifact_files():
        inventory_errors.append("staging inventory is not exact before finalization")
    verify_manifest(source, inventory_errors)
    if inventory_errors:
        raise ValueError("refusing to finalize staging evidence: " + "; ".join(inventory_errors))

    file_paths = sorted(
        (path for path in source.rglob("*") if path.is_file()), key=lambda path: path.as_posix()
    )
    directory_paths = sorted(
        (path for path in source.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts), reverse=True,
    )
    for path in file_paths:
        fsync_path(path)
        relative = path.relative_to(source).as_posix()
        os.chmod(str(path), 0o555 if relative.startswith("binaries/") else 0o444)
        fsync_path(path)
    for path in directory_paths:
        fsync_path(path, directory=True)
        os.chmod(str(path), 0o555)
        fsync_path(path, directory=True)
    fsync_path(source, directory=True)
    os.chmod(str(source), 0o555)
    fsync_path(source, directory=True)
    fsync_path(source.parent, directory=True)

    frozen_errors = []
    if regular_artifact_files(source, frozen_errors, finalized=True) != expected_artifact_files():
        frozen_errors.append("frozen staging inventory is not exact")
    verify_manifest(source, frozen_errors)
    if frozen_errors:
        raise ValueError("read-only staging freeze failed: " + "; ".join(frozen_errors))
    atomic_rename_noreplace(source, destination)
    fsync_path(destination.parent, directory=True)

    final_errors = []
    if regular_artifact_files(destination, final_errors, finalized=True) != expected_artifact_files():
        final_errors.append("finalized staging inventory is not exact")
    verify_manifest(destination, final_errors)
    if final_errors:
        raise ValueError("post-rename staging verification failed: " + "; ".join(final_errors))


TEST_CASES_BY_BINARY = {
    "test_cp1_schur_equivalence": [
        "CP1Schur.FullJointNullspaceAndReducedSystemsAgree",
    ],
    "test_cp1_rank_rejection": [
        "CP1Schur.BoundaryAndInvalidInputsHaveExplicitStatus",
        "CP1Schur.DegenerateLandmarksAreRejectedDeterministically",
    ],
    "test_cp1_projection_jacobian": [
        "CP1Projection.ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences",
        "CP1Retraction.FixedPriorChartDifferentialMatchesExactJPLRetraction",
    ],
    "test_cp1_prior_and_compression": [
        "CP1Compression.ProductionTruncationPreservesLambdaEtaButNotGamma",
        "CP1Prior.SemidefiniteCloneAugmentationMatchesInnovationUpdate",
    ],
    "test_cp2_production_schur_reducer": [
        "CP2ProductionSchurReducer.DeterministicGivensStatisticsNisAndPosteriorParity",
        "CP2ProductionSchurReducer.DeterministicRejectedCorpusHasNoPublishedOutputs",
        "CP2ProductionSchurReducer.OrderedValidityAndRankBoundariesAreExact",
        "CP2ProductionSchurReducer.ReducedOutputAndStatisticsOverflowRejectAtomically",
        "CP2ProductionSchurReducer.StrictNisDecisionBoundaryContract",
    ],
    "test_cp2_fej_golden": [
        "CP2FejGolden.MixedCurrentAndFirstEstimateProductionJacobianIsFrozen",
    ],
    "test_cp2_state_update_semantics": [
        "CP2StateUpdateSemantics.ClonePreviewCompressionAndLiveCommitParity",
        "CP2StateUpdateSemantics.InvalidPreviewInputsAreBitwiseReadOnlyAndNeverRepaired",
    ],
    "test_cp2_configuration_contract": [
        "CP2Configuration.OptionalModeUsesDefaultAndExactExplicitSpellings",
        "CP2Configuration.InvalidSpellingsCannotMutateASelectedModeOrFallBack",
        "CP2Configuration.UnknownAndFixedTwoPassSpellingsFailStartup",
        "CP2Configuration.SchurRequiresGlobal3DWhileNullspaceRetainsExistingRepresentations",
        "CP2Configuration.NonfiniteAndNonpositiveSigmaFailBeforeVarianceMaterialization",
        "CP2Configuration.NonfiniteChi2MultiplierFailsEveryStartupSeam",
        "CP2Configuration.UnderflowingAndOverflowingVarianceFailStartup",
        "CP2Configuration.PositiveRepresentableVarianceIsMaterializedExactly",
        "CP2Configuration.InvalidEnumFailsStartupWithoutSelectingAStringMode",
        "CP2Configuration.RuntimeInvalidSigmaNeverInvokesRepairOrSilentFallback",
    ],
    "test_cp2_updater_msckf_end_to_end": [
        "CP2UpdaterMSCKFEndToEnd.ActualNullspaceAndSchurModesCommitEquivalentFullStateUpdates",
        "CP2UpdaterMSCKFEndToEnd.SelectedReducersRejectNonfiniteProductionRowsWithoutSilentFallback",
        "CP2UpdaterMSCKFEndToEnd.SharedInvalidPreflightLeavesBothModeStatesBitwiseUnchanged",
        "CP2UpdaterMSCKFEndToEnd.NullspaceShadowPublishesBothPrecommitProposalsThenOneCommittedEvent",
        "CP2UpdaterMSCKFEndToEnd.NonfiniteGammaEvidenceCannotStopLiveTraversalOrBaselineCommit",
        "CP2UpdaterMSCKFEndToEnd.SchurModeRejectsShadowEnableWithoutReplacingExistingObserver",
        "CP2UpdaterMSCKFEndToEnd.ObserverExceptionCannotVetoAnAcceptedBaselineCommit",
        "CP2UpdaterMSCKFEndToEnd.AllRejectedRawSystemHasExactTerminalTaxonomyAndNoBaselineWrite",
    ],
    "test_cp2_canonical": [
        "CP2CanonicalSha256.MatchesPublishedVectorsUnderIncrementalChunking",
        "CP2CanonicalBytes.IntegerBinary64AndUtf8EncodingIsExact",
        "CP2CanonicalBytes.MatrixAndVectorUseLogicalRowMajorBinary64Order",
        "CP2CanonicalBytes.Utf8ValidationRejectsMalformedSequencesWithoutAppending",
        "CP2CanonicalBytes.SelfAppendStagesAliasedStorageBeforeGrowth",
    ],
    "test_cp2_feature_gate": [
        "CP2FeatureGate.StageNamesAreFrozen",
        "CP2FeatureGate.ReductionUnavailablePublishesNoNumericOrDecisionEvidence",
        "CP2FeatureGate.GammaOverflowCanInvalidateEvidenceWithoutChangingBaselineLifecycleGate",
        "CP2FeatureGate.EvidenceUnavailableEmittedSystemRetainsTheNormalLifecycleDecision",
        "CP2FeatureGate.MarginalIsCopiedFromImmutablePriorInDeclaredLayoutOrder",
        "CP2FeatureGate.InvalidOrOverlappingLayoutCannotFormInnovation",
        "CP2FeatureGate.NonfiniteInnovationPrecedesFactorization",
        "CP2FeatureGate.NonPositiveDefiniteInnovationFailsDefaultLowerLLT",
        "CP2FeatureGate.NonfiniteDotIsSolveOrNISFailure",
        "CP2FeatureGate.EqualityIsAcceptedAndStrictExcessRejected",
        "CP2FeatureGate.NonfiniteThresholdNullsEvidenceButPreservesIEEEComparison",
        "CP2FeatureGate.MissingConstructorTableEntryIsUnavailableNotSubstituted",
        "CP2FeatureGate.FiveHundredRowsUseDynamicBoostQuantile",
    ],
    "test_cp2_updater_msckf_preview_snapshot": [
        "CP2PreviewSnapshot.ExactAdapterParityAndInputImmutability",
        "CP2PreviewSnapshot.RejectsMalformedOwningStateLayout",
        "CP2PreviewSnapshot.RejectsMalformedValueOnlyJacobianLayout",
        "CP2PreviewSnapshot.FiniteAndLLTBoundariesAreOrdered",
    ],
    "test_cp2_shadow_math": [
        "CP2ShadowMath.FullRankPathsAgreeAndProduceIndependentGlobalProposals",
        "CP2ShadowMath.CandidateRankFailureIsAOneSidedGateAttempt",
        "CP2ShadowMath.DuplicateFeatureIdentityInvalidatesButDoesNotShortCircuitMath",
        "CP2ShadowMath.RawAndPriorLayoutDisconnectsAreStructurallyInvalid",
        "CP2ShadowMath.NullspaceStatisticsFailureCannotChangeLiveLifecycleAcceptance",
        "CP2ShadowMath.CandidateGammaOverflowSuppressesOnlyCandidateProposal",
        "CP2ShadowMath.LambdaDiagnosticOverflowCannotRemoveBaselineGateAttempt",
        "CP2ShadowMath.WhitenedJacobianOverflowCannotRemoveFiniteGammaBaselineGate",
        "CP2ShadowMath.NonfiniteWhitenedResidualStopsDiagnosticPipelineInOrder",
        "CP2ShadowMath.RawLambdaOverflowStopsEtaButFiniteGammaDefinesModeValidity",
        "CP2ShadowMath.DifferentLocalBlockOrdersUseFirstSeenGlobalLayoutExactly",
        "CP2ShadowMath.CandidateGammaOverflowStillTraversesAndStacksLaterFeatures",
        "CP2ShadowMath.EmptyAndAllRejectedGammaStatesAreExact",
        "CP2ShadowMath.StatisticComparisonFirstFailurePrecedenceIsExact",
    ],
    "test_cp2_trace_codec": [
        "CP2TraceRawPayload.FrozenDomainRowMajorBitsAndLayoutRoundTripExactly",
        "CP2TraceRawPayload.CorruptionAndAllocationBoundsFailClosed",
        "CP2TraceAcceptedDigests.FrozenSetAndSequenceDomainsAreIndependent",
        "CP2TraceRawFile.CompleteFrameAndHeaderMatchIndependentKnownAnswer",
        "CP2TraceRawFile.FrameContextOffsetsOrderingAndFeatureIdentityAreExact",
        "CP2TraceProposal.FrozenPayloadAndRoleFramingRejectAllStructuralCorruption",
        "CP2TraceReplay.OwningDecodedFramesDriveTheSoleShadowMathKernel",
        "CP2TraceReplay.ContextDigestAndPriorLayoutDisconnectsFailBeforeMath",
    ],
}


SYNTHETIC_SUMMARY_LINES = {
    "test_cp1_schur_equivalence": [
        "CP1_EQUIVALENCE fixtures=128 near_column_space_fixtures=16 seed=20260728 "
        "max_covariance_error=0 max_landmark_error=0 max_nis_error=0 max_state_error=0 "
        "max_symmetry_ratio=0 min_normalized_eigenvalue=0",
    ],
    "test_cp1_rank_rejection": [
        "CP1_RANK fixtures=128 rank_deficient=96 ill_conditioned=32 seed=1934903571",
    ],
    "test_cp1_projection_jacobian": [
        "CP1_PROJECTION fixtures=256 seed=1900496914 max_landmark_normalized_frobenius=0 "
        "max_residual_definition_error=0 max_residual_sign_normalized_frobenius=0 "
        "max_state_normalized_frobenius=0",
        "CP1_RETRACTION fixtures=256 seed=32199698170528780 max_normalized_frobenius=0",
    ],
    "test_cp1_prior_and_compression": [
        "CP1_COMPRESSION fixtures=128 seed=443998030361 max_eta_tolerance_ratio=0 "
        "max_gamma_reconstruction_tolerance_ratio=0 max_lambda_tolerance_ratio=0 "
        "min_discarded_energy=1",
        "CP1_PSD_PRIOR fixtures=128 seed=21320732 max_clone_nullspace_tolerance_ratio=0 "
        "max_known_covariance_tolerance_ratio=0 max_known_nis_tolerance_ratio=0 "
        "max_known_state_tolerance_ratio=0 max_spectral_covariance_tolerance_ratio=0 "
        "max_spectral_nis_tolerance_ratio=0 max_spectral_state_tolerance_ratio=0 "
        "max_zero_eigenvalue_tolerance_ratio=0 min_posterior_normalized_eigenvalue=0",
    ],
    "test_cp2_production_schur_reducer": [
        "CP2_A_ACCEPTED fixtures=1024 near_column_space_fixtures=128 "
        "near_conditioning_fixtures=64 nonunit_sigma_fixtures=910 min_sigma=0.125 "
        "max_sigma=12 seed=20260801 max_lambda_error=0 max_eta_error=0 "
        "max_gamma_error=0 max_nis_error=0 max_increment_error=0 max_covariance_error=0 "
        "max_lambda_tolerance_ratio=0 max_eta_tolerance_ratio=0 "
        "max_gamma_tolerance_ratio=0 max_nis_tolerance_ratio=0 "
        "max_increment_tolerance_ratio=0 max_covariance_tolerance_ratio=0",
        "CP2_A_REJECTED fixtures=128 rank_deficient=16 ill_conditioned=16 "
        "insufficient_rows=16 nonfinite=80 seed=125779917685941",
    ],
    "test_cp2_state_update_semantics": [
        "CP2_B_STATE_UPDATE fixtures=128 seed=4850432059125285204 clone_calls=256 "
        "global_compressions=256 accepted_previews=256 live_commits=256 max_clone_prior_error=0 "
        "max_clone_nullspace_ratio=0 max_preview_nominal_tolerance_ratio=0 "
        "max_preview_covariance_tolerance_ratio=0 max_identity_nominal_tolerance_ratio=0 "
        "max_identity_covariance_tolerance_ratio=0 max_mode_nominal_tolerance_ratio=0 "
        "max_mode_covariance_block_tolerance_ratio=0 preview_nominal_block_checks=1792 "
        "preview_covariance_block_checks=12544 mode_nominal_block_checks=896 "
        "mode_covariance_block_checks=6272 min_unobserved_velocity_bias_increment=0.01",
        "CP2_B_PREVIEW_REJECTION cases=5 seed=729130154250274049 state_mutations=0 "
        "jitter_count=0 repair_count=0 alternate_solve_count=0 clamp_count=0 "
        "regularization_count=0 fallback_count=0",
    ],
}


def write_synthetic_xml(path, cases):
    root = ET.Element(
        "testsuites",
        tests=str(len(cases)),
        failures="0",
        errors="0",
        disabled="0",
        name=path.stem,
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        tests=str(len(cases)),
        failures="0",
        errors="0",
        disabled="0",
        name=path.stem,
    )
    for full_name in cases:
        class_name, test_name = full_name.split(".", 1)
        ET.SubElement(
            suite, "testcase", classname=class_name, name=test_name,
            status="run", result="completed",
        )
    ET.ElementTree(root).write(str(path), encoding="utf-8", xml_declaration=True)


def synthetic_source_snapshot(repo_root, recorded_utc):
    return {
        "branch": git_text(repo_root, "branch", "--show-current"),
        "commit": git_text(repo_root, "rev-parse", "HEAD"),
        "recorded_utc": recorded_utc,
        "status_porcelain_v1": [],
        "tree": git_text(repo_root, "rev-parse", "HEAD^{tree}"),
    }


def create_synthetic_repo(repo_root):
    actual_repo = Path(__file__).resolve().parents[2]
    (repo_root / ".gitignore").write_text("/build/\n/results/\n", encoding="utf-8")
    for relative in SOURCE_INPUTS:
        destination = repo_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        actual = actual_repo / relative
        if relative in CONFIG_INPUTS or relative in {"LICENSE", "scripts/cp2/verify_report.py"}:
            if not actual.is_file():
                raise RuntimeError("self-test needs frozen configuration: " + str(actual))
            shutil.copyfile(str(actual), str(destination))
        else:
            destination.write_text("synthetic source: " + relative + "\n", encoding="utf-8")
    for package, count in DEPENDENCY_EXPECTED_COMMAND_COUNTS.items():
        for index in range(count):
            source = repo_root / package / "src" / (
                "synthetic_abi_probe_{}.cpp".format(index)
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(
                "int synthetic_abi_probe_{}() {{ return {}; }}\n".format(index, index),
                encoding="utf-8",
            )
    synthetic_ov_init = repo_root / "ov_init"
    synthetic_ov_init.mkdir(parents=True, exist_ok=True)
    (synthetic_ov_init / ".synthetic-archive-root").write_text("fixture\n", encoding="utf-8")
    subprocess.check_call(["git", "init", "-q"], cwd=str(repo_root))
    subprocess.check_call(["git", "checkout", "-q", "-b", EXPECTED_BRANCH], cwd=str(repo_root))
    subprocess.check_call(["git", "config", "user.name", "CP2 verifier self-test"], cwd=str(repo_root))
    subprocess.check_call(["git", "config", "user.email", "cp2-self-test@example.invalid"], cwd=str(repo_root))
    subprocess.check_call(["git", "add", "--all"], cwd=str(repo_root))
    environment = os.environ.copy()
    environment.update({
        "GIT_AUTHOR_DATE": "2030-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2030-01-01T00:00:00+00:00",
    })
    subprocess.check_call(
        ["git", "commit", "-q", "-m", "synthetic CP2 verifier fixture"],
        cwd=str(repo_root),
        env=environment,
    )


def create_synthetic_compile_commands(path, source_root, workspace_build_root):
    entries = []

    def add(source, target):
        output = str(workspace_build_root / "ov_msckf/CMakeFiles" / (
            target + ".dir") / (Path(source).name + ".o"))
        vectorization_definition = STRICT_REQUIRED_MACRO_DEFINITIONS[
            "EIGEN_DONT_VECTORIZE"
        ][
            len(entries) % len(
                STRICT_REQUIRED_MACRO_DEFINITIONS["EIGEN_DONT_VECTORIZE"]
            )
        ]
        tokens = [
            "/usr/bin/c++", "-O3", "-fno-signed-zeros", "-fno-fast-math",
            "-ffp-contract=off", "-fsigned-zeros", vectorization_definition,
            "-DEIGEN_MAX_ALIGN_BYTES=16",
            "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",
            "-ffile-prefix-map={}=/cp2/reproducible-root".format(source_root.parent),
            "-fdebug-prefix-map={}=/cp2/reproducible-root".format(source_root.parent),
            "-fmacro-prefix-map={}=/cp2/reproducible-root".format(source_root.parent),
            "-o", output, "-c", str(source_root / source),
        ]
        entries.append({
            "command": " ".join(shlex.quote(token) for token in tokens),
            "directory": str(workspace_build_root / "ov_msckf"),
            "file": str(source_root / source),
            "output": output,
        })

    for source in STRICT_PRODUCTION_SOURCES:
        add(source, "ov_msckf_lib")
    for target in CP1_TESTS:
        add("ov_msckf/test/cp1/gtest_main.cpp", target)
        add(TEST_SOURCE_BY_BINARY[target], target)
    for target in CP2_TESTS:
        add("ov_msckf/test/cp2/gtest_main.cpp", target)
        add(TEST_SOURCE_BY_BINARY[target], target)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def create_synthetic_dependency_compile_commands(
    path, source_root, workspace_build_root, package
):
    entries = []
    package_build_root = workspace_build_root / package
    package_build_root.mkdir(parents=True, exist_ok=True)
    for index in range(DEPENDENCY_EXPECTED_COMMAND_COUNTS[package]):
        source = "{}/src/synthetic_abi_probe_{}.cpp".format(package, index)
        output = str(
            Path("CMakeFiles") / (package + "_lib.dir")
            / ("src/synthetic_abi_probe_{}.cpp.o".format(index))
        )
        tokens = [
            "/usr/bin/c++",
            "-DEIGEN_DONT_VECTORIZE=1",
            "-DEIGEN_MAX_ALIGN_BYTES=16",
            "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",
            "-o", output,
            "-c", str(source_root / source),
        ]
        entries.append({
            "command": " ".join(shlex.quote(token) for token in tokens),
            "directory": str(package_build_root),
            "file": str(source_root / source),
            "output": output,
        })
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def synthetic_xml_text(test_name):
    cases = TEST_CASES_BY_BINARY[test_name]
    case_text = "".join(
        '<testcase classname="{}" name="{}" status="run" result="completed"/>'.format(
            full_name.split(".", 1)[0], full_name.split(".", 1)[1]
        ) for full_name in cases
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<testsuites tests="{0}" failures="0" errors="0" disabled="0" name="{1}">'
        '<testsuite tests="{0}" failures="0" errors="0" disabled="0" name="{1}">'
        '{2}</testsuite></testsuites>\n'
    ).format(len(cases), test_name, case_text)


def compile_synthetic_elf_closure(artifact_dir, repo_root, workspace):
    compiler = shutil.which("cc") or "/usr/bin/cc"
    build_dir = workspace / "elf-fixture-build"
    build_dir.mkdir(parents=True)
    project_lib = workspace / "devel/lib"
    gtest_lib = workspace / "build/ov_msckf/gtest/lib"
    binary_output = workspace / "devel/lib/ov_msckf"
    ceres_prefix = workspace / "ceres-install/lib"
    for directory in (project_lib, gtest_lib, binary_output, ceres_prefix):
        directory.mkdir(parents=True, exist_ok=True)

    sources = {
        "core.c": "int ov_core_dummy(void) { return 1; }\n",
        "init.c": "extern int ov_core_dummy(void); int ov_init_dummy(void) { return ov_core_dummy()+1; }\n",
        "ceres.c": "int ceres_dummy(void) { return 3; }\n",
        "msckf.c": (
            "extern int ov_init_dummy(void); extern int ceres_dummy(void); "
            "int ov_msckf_dummy(void) { return ov_init_dummy()+ceres_dummy(); }\n"
        ),
        "gtest.c": "int gtest_dummy(void) { return 7; }\n",
    }
    for name, content in sources.items():
        (build_dir / name).write_text(content, encoding="utf-8")

    ceres_version = ceres_prefix / "libceres.so.1.14.0"
    deterministic_link = ["-Wl,--build-id=sha1", "-Wl,-rpath,$ORIGIN:/opt/ros/noetic/lib"]
    commands = [
        [compiler, "-shared", "-fPIC", str(build_dir / "core.c"),
         "-Wl,-soname,libov_core_lib.so", *deterministic_link,
         "-o", str(project_lib / "libov_core_lib.so")],
        [compiler, "-shared", "-fPIC", str(build_dir / "init.c"), "-Wl,--no-as-needed",
         str(project_lib / "libov_core_lib.so"), "-Wl,-soname,libov_init_lib.so",
         *deterministic_link, "-o", str(project_lib / "libov_init_lib.so")],
        [compiler, "-shared", "-fPIC", str(build_dir / "ceres.c"),
         "-Wl,-soname,libceres.so.1", *deterministic_link, "-o", str(ceres_version)],
        [compiler, "-shared", "-fPIC", str(build_dir / "msckf.c"), "-Wl,--no-as-needed",
         str(project_lib / "libov_init_lib.so"), str(ceres_version),
         "-Wl,-soname,libov_msckf_lib.so", *deterministic_link,
         "-o", str(project_lib / "libov_msckf_lib.so")],
        [compiler, "-shared", "-fPIC", str(build_dir / "gtest.c"),
         "-Wl,-soname,libgtest.so", *deterministic_link,
         "-o", str(gtest_lib / "libgtest.so")],
    ]
    clean_env = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    for command in commands:
        subprocess.check_call(command, env=clean_env)
    (ceres_prefix / "libceres.so.1").symlink_to(ceres_version.name)

    fixtures = []
    for test_name in ALL_TESTS:
        summaries = "\n".join(SYNTHETIC_SUMMARY_LINES.get(test_name, []))
        if summaries:
            summaries += "\n"
        fixtures.append(
            "  {{ {}, {}, {} }}".format(
                json.dumps(test_name), json.dumps(synthetic_xml_text(test_name)),
                json.dumps(summaries),
            )
        )
    main_source = r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
extern int ov_msckf_dummy(void);
extern int gtest_dummy(void);
struct fixture { const char *name; const char *xml; const char *summary; };
static const struct fixture fixtures[] = {
FIXTURES
};
int main(int argc, char **argv) {
  const char *base = strrchr(argv[0], '/');
  base = base ? base + 1 : argv[0];
  const struct fixture *selected = NULL;
  const char *prefix = "--gtest_output=xml:";
  const char *output = NULL;
  size_t i;
  for (i = 0; i < sizeof(fixtures)/sizeof(fixtures[0]); ++i)
    if (strcmp(base, fixtures[i].name) == 0) selected = &fixtures[i];
  for (i = 1; i < (size_t)argc; ++i)
    if (strncmp(argv[i], prefix, strlen(prefix)) == 0) output = argv[i] + strlen(prefix);
  if (!selected || !output) return 2;
  FILE *stream = fopen(output, "wb");
  if (!stream) return 3;
  fputs(selected->xml, stream);
  if (fclose(stream) != 0) return 4;
  fputs("[==========] synthetic ELF gtest execution\n", stdout);
  fputs(selected->summary, stdout);
  return (ov_msckf_dummy() + gtest_dummy() == 12) ? 0 : 5;
}
'''.replace("FIXTURES", ",\n".join(fixtures))
    (build_dir / "main.c").write_text(main_source, encoding="utf-8")
    generic = binary_output / "cp2_synthetic_gtest"
    subprocess.check_call(
        [compiler, str(build_dir / "main.c"), "-Wl,--no-as-needed",
         str(project_lib / "libov_msckf_lib.so"), str(project_lib / "libov_init_lib.so"),
         str(project_lib / "libov_core_lib.so"), str(gtest_lib / "libgtest.so"),
         str(ceres_version), *deterministic_link, "-o", str(generic)],
        env=clean_env,
    )
    source_paths = {
        "libov_msckf_lib.so": project_lib / "libov_msckf_lib.so",
        "libov_core_lib.so": project_lib / "libov_core_lib.so",
        "libov_init_lib.so": project_lib / "libov_init_lib.so",
        "libgtest.so": gtest_lib / "libgtest.so",
        "libceres.so.1": ceres_version,
    }
    for name, source in source_paths.items():
        destination = artifact_dir / "binaries" / name
        shutil.copyfile(str(source), str(destination))
        destination.chmod(0o555)
    test_source_paths = {}
    for test_name in ALL_TESTS:
        source = binary_output / test_name
        shutil.copyfile(str(generic), str(source))
        source.chmod(0o555)
        destination = artifact_dir / "binaries" / test_name
        shutil.copyfile(str(source), str(destination))
        destination.chmod(0o555)
        test_source_paths[test_name] = source
    return source_paths, test_source_paths


def create_synthetic_ceres_checkout(repo_root):
    checkout = repo_root / "build/vendor/ceres-src"
    checkout.mkdir(parents=True)
    (checkout / "README.synthetic").write_text("synthetic Ceres provenance\n", encoding="utf-8")
    shutil.copyfile(
        str(Path(__file__).resolve().parents[2] / "build/vendor/ceres-src/LICENSE"),
        str(checkout / "LICENSE"),
    )
    subprocess.check_call(["git", "init", "-q"], cwd=str(checkout))
    subprocess.check_call(["git", "config", "user.name", "CP2 self-test"], cwd=str(checkout))
    subprocess.check_call(
        ["git", "config", "user.email", "cp2-self-test@example.invalid"], cwd=str(checkout)
    )
    subprocess.check_call(["git", "add", "--all"], cwd=str(checkout))
    subprocess.check_call(["git", "commit", "-q", "-m", "synthetic Ceres"], cwd=str(checkout))
    subprocess.check_call(["git", "tag", "1.14.0"], cwd=str(checkout))


def write_json_fixture(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def create_synthetic_artifact(artifact_dir, repo_root):
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "binaries").mkdir()
    commit = git_text(repo_root, "rev-parse", "HEAD")
    tree = git_text(repo_root, "rev-parse", "HEAD^{tree}")
    workspace = repo_root / "build/cp2-unit-workspaces/.cp2-unit-synthetic.workspace.fixture"
    source_root = workspace / "src"
    workspace_build_root = workspace / "build"
    ceres_source_root = workspace / "ceres-src"
    ceres_build_root = workspace / "ceres-build"
    ceres_install_prefix = workspace / "ceres-install"
    for directory in (source_root, workspace_build_root, workspace / "home", workspace / "tmp"):
        directory.mkdir(parents=True, exist_ok=True)
    workspace_archive = workspace / SOURCE_ARCHIVE_NAME
    subprocess.check_call(
        ["/usr/bin/git", "-C", str(repo_root), "archive", "--format=tar",
         "--output=" + str(workspace_archive), commit, "--", *ARCHIVE_ROOTS]
    )
    subprocess.check_call(
        ["/usr/bin/tar", "--extract", "--file", str(workspace_archive),
         "--directory", str(source_root), "--no-same-owner", "--no-same-permissions",
         "--warning=no-timestamp"]
    )
    for path in sorted(source_root.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        path.chmod(0o555 if path.is_dir() else 0o444)
    source_root.chmod(0o555)
    workspace_archive.chmod(0o444)
    shutil.copyfile(str(workspace_archive), str(artifact_dir / SOURCE_ARCHIVE_NAME))
    (artifact_dir / SOURCE_ARCHIVE_NAME).chmod(0o444)

    create_synthetic_ceres_checkout(repo_root)
    ceres_checkout = repo_root / "build/vendor/ceres-src"
    ceres_archive = workspace / "ceres_source_snapshot.tar"
    subprocess.check_call(
        ["/usr/bin/git", "-C", str(ceres_checkout), "archive", "--format=tar",
         "--output=" + str(ceres_archive), git_text(ceres_checkout, "rev-parse", "HEAD")]
    )
    ceres_source_root.mkdir()
    subprocess.check_call(
        ["/usr/bin/tar", "--extract", "--file", str(ceres_archive),
         "--directory", str(ceres_source_root), "--no-same-owner", "--no-same-permissions",
         "--warning=no-timestamp"]
    )
    for path in sorted(
        ceres_source_root.rglob("*"), key=lambda value: len(value.parts), reverse=True
    ):
        path.chmod(0o555 if path.is_dir() else 0o444)
    ceres_source_root.chmod(0o555)
    ceres_archive.chmod(0o444)
    shutil.copyfile(str(ceres_archive), str(artifact_dir / "ceres_source_snapshot.tar"))
    googletest_archive = workspace / "googletest_source_snapshot.tar"
    subprocess.check_call(
        ["/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
         "--numeric-owner", "--format=gnu", "--create", "--file=" + str(googletest_archive),
         "--directory=/usr/src", "googletest"]
    )
    shutil.copyfile(
        str(googletest_archive), str(artifact_dir / "googletest_source_snapshot.tar")
    )
    source_paths, test_source_paths = compile_synthetic_elf_closure(
        artifact_dir, repo_root, workspace
    )
    notices = artifact_dir / "THIRD_PARTY_NOTICES"
    notices.mkdir()
    actual_repo = Path(__file__).resolve().parents[2]
    shutil.copyfile(
        str(ceres_source_root / "LICENSE"),
        str(notices / "Ceres-LICENSE"),
    )
    shutil.copyfile(
        "/usr/src/googletest/googlemock/LICENSE",
        str(notices / "GoogleTest-LICENSE"),
    )
    compiler = shutil.which("c++") or "/usr/bin/c++"
    (artifact_dir / "CMakeCache.txt").write_text(
        "CMAKE_CXX_COMPILER:FILEPATH=" + compiler + "\n"
        "gtest_SOURCE_DIR:STATIC=/usr/src/googletest/googletest\n",
        encoding="utf-8",
    )
    create_synthetic_compile_commands(
        artifact_dir / "compile_commands.json", source_root, workspace_build_root
    )
    for package, artifact in DEPENDENCY_COMPILE_COMMAND_ARTIFACTS.items():
        create_synthetic_dependency_compile_commands(
            artifact_dir / artifact,
            source_root,
            workspace_build_root,
            package,
        )

    before = synthetic_source_snapshot(repo_root, "2030-01-01T00:00:01Z")
    # Keep the synthetic source-after snapshot strictly after the complete
    # serialized test inventory. The last test finishes at 14 + 2*N seconds.
    synthetic_epoch = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)
    source_after_utc = (
        synthetic_epoch + dt.timedelta(seconds=16 + 2 * len(ALL_TESTS))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    after = synthetic_source_snapshot(repo_root, source_after_utc)
    write_json_fixture(artifact_dir / "source_before.json", before)
    write_json_fixture(artifact_dir / "source_after.json", after)
    self_test = {
        "argv": ["/usr/bin/python3", str(repo_root / "scripts/cp2/verify_report.py"), "--self-test"],
        "cwd": ".",
        "environment": {
            "LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin",
            "PYTHONHASHSEED": "0", "TZ": "UTC",
        },
        "environment_mode": "env-i",
        "exit_status": 0,
        "finished_utc": "2030-01-01T00:00:00Z",
        "name": "verifier_self_test",
        "started_utc": "2029-12-31T23:59:59Z",
        "synthetic_artifact_root_policy": "tempfile.TemporaryDirectory_outside_repository_results",
    }
    write_json_fixture(artifact_dir / "verifier_self_test.json", self_test)
    (artifact_dir / "verifier_self_test.log").write_text(
        "CP2 verifier self-test passed using only: /tmp/synthetic\n"
        "Synthetic corruptions rejected: synthetic-bootstrap\n", encoding="utf-8"
    )

    workspace_record = {
        "archive_argv": [
            "/usr/bin/git", "-C", str(repo_root), "archive", "--format=tar",
            "--output=" + str(workspace_archive), commit, "--", *ARCHIVE_ROOTS,
        ],
        "archive_artifact": SOURCE_ARCHIVE_NAME,
        "archive_roots": list(ARCHIVE_ROOTS),
        "archive_sha256": sha256_file(workspace_archive),
        "archive_size_bytes": workspace_archive.stat().st_size,
        "ceres_archive_argv": [
            "/usr/bin/git", "-C", str(ceres_checkout), "archive", "--format=tar",
            "--output=" + str(ceres_archive), git_text(ceres_checkout, "rev-parse", "HEAD"),
        ],
        "ceres_archive_artifact": "ceres_source_snapshot.tar",
        "ceres_archive_sha256": sha256_file(ceres_archive),
        "ceres_archive_size_bytes": ceres_archive.stat().st_size,
        "ceres_build_root": str(ceres_build_root),
        "ceres_install_prefix": str(ceres_install_prefix),
        "ceres_source_commit": git_text(ceres_checkout, "rev-parse", "HEAD"),
        "ceres_source_read_only_after_build": True,
        "ceres_source_read_only_before_build": True,
        "ceres_source_root": str(ceres_source_root),
        "ceres_source_tag": CERES_TAG,
        "created_utc": "2030-01-01T00:00:01Z",
        "fresh": True,
        "googletest_archive_argv": [
            "/usr/bin/tar", "--sort=name", "--mtime=@0", "--owner=0", "--group=0",
            "--numeric-owner", "--format=gnu", "--create",
            "--file=" + str(googletest_archive), "--directory=/usr/src", "googletest",
        ],
        "googletest_archive_artifact": "googletest_source_snapshot.tar",
        "googletest_archive_sha256": sha256_file(googletest_archive),
        "googletest_archive_sha256_after_build": sha256_file(googletest_archive),
        "googletest_archive_sha256_before_build": sha256_file(googletest_archive),
        "googletest_archive_size_bytes": googletest_archive.stat().st_size,
        "googletest_archive_size_bytes_after_build": googletest_archive.stat().st_size,
        "googletest_archive_size_bytes_before_build": googletest_archive.stat().st_size,
        "googletest_source_root": "/usr/src/googletest",
        "googletest_unchanged_after_build": True,
        "kind": "unique_git_archive_cp2_catkin_workspace",
        "repo_root": str(repo_root),
        "repository_build_root": str(repo_root / "build"),
        "reproducible_prefix": "/cp2/reproducible-root",
        "reused": False,
        "runtime_rpath": "$ORIGIN:/opt/ros/noetic/lib",
        "schema_version": 1,
        "source_commit": commit,
        "source_read_only_after_build": True,
        "source_read_only_before_build": True,
        "source_root": str(source_root),
        "source_tree": tree,
        "workspace": str(workspace),
        "workspace_build_root": str(workspace_build_root),
    }
    write_json_fixture(artifact_dir / WORKSPACE_RECORD_NAME, workspace_record)
    build_environment = expected_build_environment(workspace_record, repo_root, commit, [])
    for index, step in enumerate(BUILD_STEPS):
        record = {
            "argv": expected_build_argv(step, workspace_record, repo_root),
            "cwd": ".",
            "environment": build_environment,
            "environment_mode": "env-i",
            "exit_status": 0,
            "finished_utc": "2030-01-01T00:00:{:02d}Z".format(3 + 2 * index),
            "name": step,
            "serialized": True,
            "started_utc": "2030-01-01T00:00:{:02d}Z".format(2 + 2 * index),
        }
        write_json_fixture(artifact_dir / ("build_" + step + ".json"), record)
        log_text = "synthetic serialized env-i ELF build: " + step + "\n"
        (artifact_dir / ("build_" + step + ".log")).write_text(
            log_text, encoding="utf-8"
        )
    synthetic_catkin_cmake_log = (
        "synthetic package CMake output\n" + GOOGLETEST_DISCOVERY_LINE + "\n"
    )
    retained_catkin_cmake_log = workspace / "logs/ov_msckf/build.cmake.log"
    retained_catkin_cmake_log.parent.mkdir(parents=True, exist_ok=True)
    retained_catkin_cmake_log.write_text(
        synthetic_catkin_cmake_log, encoding="utf-8"
    )
    (artifact_dir / CATKIN_PACKAGE_CMAKE_LOG_NAME).write_text(
        synthetic_catkin_cmake_log, encoding="utf-8"
    )

    linkage_errors = []
    linkage = collect_elf_linkage(artifact_dir, linkage_errors)
    if linkage_errors:
        raise RuntimeError("synthetic ELF closure is invalid: " + "; ".join(linkage_errors))
    runtime_environment = controlled_runtime_environment(artifact_dir / "binaries")
    for index, test_name in enumerate(ALL_TESTS):
        started_second = 15 + 2 * index
        xml_name = test_name + ".xml"
        argv = ["binaries/" + test_name, *GTEST_OPTIONS, "--gtest_output=xml:" + xml_name]
        before_hashes = execution_input_hashes(artifact_dir, test_name)
        completed = subprocess.run(
            argv, cwd=str(artifact_dir), env=runtime_environment, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=30, check=False,
        )
        (artifact_dir / (test_name + ".log")).write_text(completed.stdout, encoding="utf-8")
        after_hashes = execution_input_hashes(artifact_dir, test_name)
        record = {
            "argv": argv,
            "checkpoint": "CP1" if test_name in CP1_TESTS else "CP2",
            "cwd": ".",
            "environment": dict(CONTROLLED_TEST_ENVIRONMENT),
            "environment_mode": "env-i",
            "execution_inputs_sha256_after": after_hashes,
            "execution_inputs_sha256_before": before_hashes,
            "exit_status": completed.returncode,
            "finished_utc": "2030-01-01T00:00:{:02d}Z".format(started_second + 1),
            "loader_map": linkage["executables"][test_name]["loader"],
            "name": test_name,
            "serialized": True,
            "started_utc": "2030-01-01T00:00:{:02d}Z".format(started_second),
        }
        write_json_fixture(artifact_dir / (test_name + ".json"), record)
        if completed.returncode != 0:
            raise RuntimeError("synthetic ELF test failed: " + test_name)

    def synthetic_copy_entry(name, source_path, elf):
        artifact_path = artifact_dir / "binaries" / name
        return {
            "artifact_path": "binaries/" + name,
            "artifact_sha256": sha256_file(artifact_path),
            "build_id": elf["build_id"],
            "exact_copy": True,
            "mode": "0555",
            "rpath": elf["rpath"],
            "runpath": elf["runpath"],
            "size_bytes": artifact_path.stat().st_size,
            "soname": elf["soname"],
            "source_path": str(source_path.resolve()),
            "source_sha256": sha256_file(source_path.resolve()),
        }

    copied = [
        synthetic_copy_entry(name, source_paths[name], linkage["libraries"][name])
        for name in SNAPSHOTTED_LIBRARY_ORDER
    ]
    copied_tests = [
        synthetic_copy_entry(
            name, test_source_paths[name], linkage["executables"][name]["elf"]
        )
        for name in ALL_TESTS
    ]
    ceres_checkout = repo_root / "build/vendor/ceres-src"
    ceres_source = source_paths["libceres.so.1"].resolve()
    copyright_path = Path("/usr/share/doc/googletest/copyright")
    dependency = {
        "ceres": {
            "archive_artifact": "ceres_source_snapshot.tar",
            "archive_sha256": sha256_file(ceres_archive),
            "snapshotted_soname": "libceres.so.1",
            "source_checkout": str(ceres_source_root.resolve()),
            "source_commit": git_text(ceres_checkout, "rev-parse", "HEAD"),
            "source_library_path": str(ceres_source),
            "source_library_sha256": sha256_file(ceres_source),
            "source_tag": git_text(ceres_checkout, "describe", "--tags", "--exact-match"),
        },
        "copied_dsos": copied,
        "copied_test_executables": copied_tests,
        "distribution_status": "internal_non_conveyable_staging",
        "googletest": {
            "archive_artifact": "googletest_source_snapshot.tar",
            "archive_sha256_after_build": sha256_file(googletest_archive),
            "archive_sha256_before_build": sha256_file(googletest_archive),
            "archive_size_bytes_after_build": googletest_archive.stat().st_size,
            "archive_size_bytes_before_build": googletest_archive.stat().st_size,
            "build_source_root": "/usr/src/googletest",
            "debian_copyright_path": str(copyright_path),
            "debian_copyright_sha256": sha256_file(copyright_path),
            "package": "googletest",
            "package_version": "1.10.0-2",
            "source_root": "/usr/src/googletest",
            "unchanged_after_build": True,
        },
        "independent_source_to_binary_attestation": False,
        "loader_maps": {
            name: linkage["executables"][name]["loader"] for name in ALL_TESTS
        },
        "required_copied_dsos": list(SNAPSHOTTED_LIBRARY_ORDER),
        "schema_version": 1,
        "third_party_notices": {
            "ceres": {
                "artifact_path": "THIRD_PARTY_NOTICES/Ceres-LICENSE",
                "component_version": CERES_TAG,
                "sha256": CERES_LICENSE_SHA256,
                "source_path": str(ceres_source_root / "LICENSE"),
            },
            "googletest": {
                "artifact_path": "THIRD_PARTY_NOTICES/GoogleTest-LICENSE",
                "component_version": "1.10.0-2",
                "sha256": GOOGLETEST_LICENSE_SHA256,
                "source_path": "/usr/src/googletest/googlemock/LICENSE",
            },
        },
        "threat_model": "trusted_runner_local_staging",
    }
    write_json_fixture(artifact_dir / DEPENDENCY_INVENTORY_NAME, dependency)
    for path in artifact_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o555 if path.parent == artifact_dir / "binaries" else 0o600)


def reseal_synthetic(artifact_dir):
    manifest = artifact_dir / MANIFEST_NAME
    if manifest.exists():
        manifest.unlink()
    generate_manifest(artifact_dir)


def set_synthetic_tree_modes(artifact_dir, writable):
    directories = [artifact_dir] + [path for path in artifact_dir.rglob("*") if path.is_dir()]
    if writable:
        for path in directories:
            path.chmod(0o700)
        for path in artifact_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o700 if path.parent == artifact_dir / "binaries" else 0o600)
    else:
        for path in artifact_dir.rglob("*"):
            if path.is_file():
                path.chmod(0o555 if path.parent == artifact_dir / "binaries" else 0o444)
        for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            path.chmod(0o555)


def mutate_json(path, callback):
    value = json.loads(path.read_text(encoding="utf-8"))
    callback(value)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_self_test():
    # Force all writes under /tmp. The self-test never creates, removes, or
    # changes anything under a repository results/ directory.
    with tempfile.TemporaryDirectory(prefix="cp2-verifier-self-test-", dir="/tmp") as temporary:
        temporary_root = Path(temporary).resolve()
        if "results" in temporary_root.parts:
            raise RuntimeError("self-test temporary root unexpectedly contains a results component")
        repo_root = temporary_root / "repo"
        repo_root.mkdir()
        create_synthetic_repo(repo_root)
        partial = temporary_root / "artifact-partial"
        create_synthetic_artifact(partial, repo_root)
        assemble_unit_report(partial, repo_root, allow_synthetic=True)
        base = temporary_root / "artifact-finalized"
        finalize_staging_noreplace(
            partial, base, repo_root=repo_root, allow_synthetic=True
        )
        status, errors = verify_unit_report(
            base, repo_root, quiet=True, allow_synthetic=True
        )
        if status != 0:
            raise RuntimeError("valid synthetic artifact was rejected: " + "; ".join(errors))

        corruptions = []

        def corruption(name, edit, reseal=True, expected_error=None):
            destination = temporary_root / ("corrupt-" + name)
            shutil.copytree(str(base), str(destination))
            set_synthetic_tree_modes(destination, writable=True)
            edit(destination)
            if reseal:
                reseal_synthetic(destination)
            set_synthetic_tree_modes(destination, writable=False)
            result, result_errors = verify_unit_report(
                destination, repo_root, quiet=True, allow_synthetic=True
            )
            if result == 0:
                raise RuntimeError("corruption was accepted: " + name)
            if expected_error is not None and not any(
                expected_error in error for error in result_errors
            ):
                raise RuntimeError(
                    "corruption {} lacked expected diagnostic: {}".format(
                        name, expected_error
                    )
                )
            corruptions.append({"name": name, "detected_errors": len(result_errors)})

        corruption(
            "manifest-log-byte",
            lambda root: (root / "test_cp2_fej_golden.log").write_text(
                "corrupted without manifest update\n", encoding="utf-8"
            ),
            reseal=False,
        )

        def remove_dedicated_gtest_binding(root):
            path = root / CATKIN_PACKAGE_CMAKE_LOG_NAME
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    GOOGLETEST_DISCOVERY_LINE,
                    "synthetic GoogleTest discovery binding removed",
                ),
                encoding="utf-8",
            )

        corruption(
            "dedicated-gtest-cmake-log-binding-coordinated",
            remove_dedicated_gtest_binding,
            expected_error=(
                "dedicated Catkin package CMake log GoogleTest discovery lines are not "
                "the exact controlled binding"
            ),
        )

        def remove_tree(root):
            mutate_json(root / REPORT_NAME, lambda report: report["source"].pop("tree", None))

        corruption("missing-source-tree", remove_tree)

        def remove_recorded_contract_from_archive(root):
            archive_path = root / SOURCE_ARCHIVE_NAME
            replacement = root / ("." + SOURCE_ARCHIVE_NAME + ".without-contract")
            removed = 0
            with tarfile.open(str(archive_path), mode="r:") as source_archive:
                with tarfile.open(str(replacement), mode="w:") as target_archive:
                    for member in source_archive.getmembers():
                        if member.name == "docs/cp2_recorded_evidence_contract.md":
                            removed += 1
                            continue
                        stream = source_archive.extractfile(member) if member.isfile() else None
                        target_archive.addfile(member, stream)
            if removed != 1:
                raise RuntimeError("synthetic source archive lost the recorded contract")
            os.replace(str(replacement), str(archive_path))
            archive_path.chmod(0o600)

        corruption(
            "missing-recorded-contract-archive-member",
            remove_recorded_contract_from_archive,
            expected_error="source archive does not contain every curated SOURCE_INPUTS file",
        )

        def corrupt_binary(root):
            binary = root / "binaries/test_cp2_production_schur_reducer"
            binary.chmod(0o755)
            with binary.open("ab") as stream:
                stream.write(b"corruption")

        corruption("binary-hash", corrupt_binary)

        def remove_fp_flag(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries[0]["command"] = entries[0]["command"].replace(" -ffp-contract=off", "")
            path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")

        corruption("strict-fp", remove_fp_flag)

        def remove_preview_fp_flag(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if str(entry.get("file", "")).endswith(
                    "/ov_msckf/src/update/UpdaterMSCKFPreview.cpp"
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost the preview production command")
            matching[0]["command"] = matching[0]["command"].replace(
                " -fsigned-zeros", "", 1
            )
            write_json_fixture(path, entries)

        corruption(
            "preview-production-strict-fp",
            remove_preview_fp_flag,
            expected_error=(
                "ov_msckf/src/update/UpdaterMSCKFPreview.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def remove_updater_helper_fp_flag(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if str(entry.get("file", "")).endswith(
                    "/ov_msckf/src/update/UpdaterHelper.cpp"
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost the updater-helper production command")
            matching[0]["command"] = matching[0]["command"].replace(
                " -ffp-contract=off", "", 1
            )
            write_json_fixture(path, entries)

        corruption(
            "updater-helper-production-strict-fp",
            remove_updater_helper_fp_flag,
            expected_error=(
                "ov_msckf/src/update/UpdaterHelper.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def remove_state_helper_production_command(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            retained = [
                entry for entry in entries
                if not str(entry.get("file", "")).endswith(
                    "/ov_msckf/src/state/StateHelper.cpp"
                )
            ]
            if len(retained) != len(entries) - 1:
                raise RuntimeError("synthetic fixture lost the state-helper production command")
            write_json_fixture(path, retained)

        corruption(
            "state-helper-production-inventory",
            remove_state_helper_production_command,
            expected_error=(
                "strict-FP evidence requires exactly one production command for "
                "ov_msckf/src/state/StateHelper.cpp"
            ),
        )

        def remove_eigen_definition(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if str(entry.get("file", "")).endswith(
                    "/ov_msckf/src/update/CP2Canonical.cpp"
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost the canonical production command")
            tokens = shlex.split(matching[0]["command"])
            retained = [
                token for token in tokens
                if token not in STRICT_REQUIRED_MACRO_DEFINITIONS[
                    "EIGEN_DONT_VECTORIZE"
                ]
            ]
            if len(retained) != len(tokens) - 1:
                raise RuntimeError("synthetic fixture lacks one exact Eigen scalar definition")
            matching[0]["command"] = " ".join(
                shlex.quote(token) for token in retained
            )
            write_json_fixture(path, entries)

        corruption(
            "missing-eigen-dont-vectorize",
            remove_eigen_definition,
            expected_error=(
                "ov_msckf/src/update/CP2Canonical.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def undefine_eigen_after_definition(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if str(entry.get("file", "")).endswith(
                    "/ov_msckf/src/state/StateHelper.cpp"
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost the state-helper production command")
            tokens = shlex.split(matching[0]["command"])
            output_index = tokens.index("-o")
            tokens.insert(output_index, "-UEIGEN_DONT_VECTORIZE")
            matching[0]["command"] = " ".join(shlex.quote(token) for token in tokens)
            write_json_fixture(path, entries)

        corruption(
            "eigen-dont-vectorize-later-undefined",
            undefine_eigen_after_definition,
            expected_error=(
                "ov_msckf/src/state/StateHelper.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def override_eigen_after_definition(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if (
                    "CMakeFiles/test_cp2_shadow_math.dir/" in str(entry.get("output", ""))
                    and str(entry.get("file", "")).endswith(
                        "/ov_msckf/test/cp2/test_cp2_shadow_math.cpp"
                    )
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost the shadow-math test command")
            tokens = shlex.split(matching[0]["command"])
            output_index = tokens.index("-o")
            tokens.insert(output_index, "-DEIGEN_DONT_VECTORIZE=0")
            matching[0]["command"] = " ".join(shlex.quote(token) for token in tokens)
            write_json_fixture(path, entries)

        corruption(
            "eigen-dont-vectorize-later-overridden",
            override_eigen_after_definition,
            expected_error=(
                "test_cp2_shadow_math has a non-strict compile command for "
                "ov_msckf/test/cp2/test_cp2_shadow_math.cpp"
            ),
        )

        def rewrite_synthetic_compile_tokens(root, source_suffix, transform):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if str(entry.get("file", "")).endswith("/" + source_suffix)
            ]
            if len(matching) != 1:
                raise RuntimeError(
                    "synthetic fixture lost exactly one command for " + source_suffix
                )
            original = shlex.split(matching[0]["command"])
            changed = transform(list(original))
            if changed == original:
                raise RuntimeError("synthetic compile-command transform made no change")
            matching[0]["command"] = " ".join(
                shlex.quote(token) for token in changed
            )
            write_json_fixture(path, entries)

        def remove_exact_token(tokens, token):
            if tokens.count(token) != 1:
                raise RuntimeError("synthetic command lacks exactly one " + token)
            tokens.remove(token)
            return tokens

        def insert_before_output(tokens, token):
            output_positions = [
                index for index, value in enumerate(tokens) if value == "-o"
            ]
            if len(output_positions) != 1:
                raise RuntimeError("synthetic command lacks exactly one -o")
            tokens.insert(output_positions[0], token)
            return tokens

        def replace_exact_token(tokens, old, new):
            if tokens.count(old) != 1:
                raise RuntimeError("synthetic command lacks exactly one " + old)
            tokens[tokens.index(old)] = new
            return tokens

        def remove_max_align_bytes(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/CP2FeatureGate.cpp",
                lambda tokens: remove_exact_token(
                    tokens, "-DEIGEN_MAX_ALIGN_BYTES=16"
                ),
            )

        corruption(
            "missing-eigen-max-align-bytes",
            remove_max_align_bytes,
            expected_error=(
                "ov_msckf/src/update/CP2FeatureGate.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def remove_max_static_align_bytes(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/SchurUpdate.cpp",
                lambda tokens: remove_exact_token(
                    tokens, "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16"
                ),
            )

        corruption(
            "missing-eigen-max-static-align-bytes",
            remove_max_static_align_bytes,
            expected_error=(
                "ov_msckf/src/update/SchurUpdate.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def undefine_max_static_align_bytes(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/UpdaterHelper.cpp",
                lambda tokens: insert_before_output(
                    tokens, "-UEIGEN_MAX_STATIC_ALIGN_BYTES"
                ),
            )

        corruption(
            "eigen-max-static-align-bytes-later-undefined",
            undefine_max_static_align_bytes,
            expected_error=(
                "ov_msckf/src/update/UpdaterHelper.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def zero_max_align_bytes(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/test/cp2/test_cp2_canonical.cpp",
                lambda tokens: insert_before_output(
                    tokens, "-DEIGEN_MAX_ALIGN_BYTES=0"
                ),
            )

        corruption(
            "eigen-max-align-bytes-later-zero",
            zero_max_align_bytes,
            expected_error=(
                "test_cp2_canonical has a non-strict compile command for "
                "ov_msckf/test/cp2/test_cp2_canonical.cpp"
            ),
        )

        def override_max_static_align_bytes(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/test/cp2/test_cp2_feature_gate.cpp",
                lambda tokens: insert_before_output(
                    tokens, "-DEIGEN_MAX_STATIC_ALIGN_BYTES=32"
                ),
            )

        corruption(
            "eigen-max-static-align-bytes-later-other",
            override_max_static_align_bytes,
            expected_error=(
                "test_cp2_feature_gate has a non-strict compile command for "
                "ov_msckf/test/cp2/test_cp2_feature_gate.cpp"
            ),
        )

        def make_max_align_bytes_opaque(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp",
                lambda tokens: replace_exact_token(
                    tokens,
                    "-DEIGEN_MAX_ALIGN_BYTES=16",
                    "-Wp,-DEIGEN_MAX_ALIGN_BYTES=16",
                ),
            )

        corruption(
            "eigen-max-align-bytes-opaque",
            make_max_align_bytes_opaque,
            expected_error=(
                "test_cp2_updater_msckf_preview_snapshot has a non-strict compile "
                "command for ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp"
            ),
        )

        def rewrite_dependency_compile_tokens(root, package, transform):
            path = root / DEPENDENCY_COMPILE_COMMAND_ARTIFACTS[package]
            entries = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(entries, list)
                or len(entries) != DEPENDENCY_EXPECTED_COMMAND_COUNTS[package]
            ):
                raise RuntimeError(
                    "synthetic dependency compile database lost its exact fixture inventory"
                )
            original = shlex.split(entries[0]["command"])
            changed = transform(list(original))
            if changed == original:
                raise RuntimeError("dependency compile-command transform made no change")
            entries[0]["command"] = " ".join(
                shlex.quote(token) for token in changed
            )
            write_json_fixture(path, entries)

        def corrupt_ov_core_eigen_abi(root):
            rewrite_dependency_compile_tokens(
                root,
                "ov_core",
                lambda tokens: remove_exact_token(
                    tokens, "-DEIGEN_MAX_ALIGN_BYTES=16"
                ),
            )

        corruption(
            "ov-core-dependency-eigen-abi",
            corrupt_ov_core_eigen_abi,
            expected_error=(
                "ov_core command 0 for ov_core/src/synthetic_abi_probe_0.cpp fails "
                "dependency ABI/source/output proof"
            ),
        )

        def make_ov_core_dont_vectorize_bare(root):
            rewrite_dependency_compile_tokens(
                root,
                "ov_core",
                lambda tokens: replace_exact_token(
                    tokens,
                    "-DEIGEN_DONT_VECTORIZE=1",
                    "-DEIGEN_DONT_VECTORIZE",
                ),
            )

        corruption(
            "ov-core-dependency-dont-vectorize-bare",
            make_ov_core_dont_vectorize_bare,
            expected_error=(
                "ov_core command 0 for ov_core/src/synthetic_abi_probe_0.cpp fails "
                "dependency ABI/source/output proof"
            ),
        )

        def corrupt_ov_init_eigen_abi(root):
            rewrite_dependency_compile_tokens(
                root,
                "ov_init",
                lambda tokens: insert_before_output(
                    tokens, "-UEIGEN_MAX_STATIC_ALIGN_BYTES"
                ),
            )

        corruption(
            "ov-init-dependency-eigen-abi",
            corrupt_ov_init_eigen_abi,
            expected_error=(
                "ov_init command 0 for ov_init/src/synthetic_abi_probe_0.cpp fails "
                "dependency ABI/source/output proof"
            ),
        )

        def truncate_ov_core_compile_commands(root):
            path = root / DEPENDENCY_COMPILE_COMMAND_ARTIFACTS["ov_core"]
            entries = json.loads(path.read_text(encoding="utf-8"))
            if len(entries) != DEPENDENCY_EXPECTED_COMMAND_COUNTS["ov_core"]:
                raise RuntimeError("synthetic ov_core command inventory changed")
            entries.pop()
            write_json_fixture(path, entries)

        corruption(
            "ov-core-dependency-command-count",
            truncate_ov_core_compile_commands,
            expected_error="ov_core compile_commands.json must contain exactly 21 compile commands",
        )

        def duplicate_ov_init_source(root):
            path = root / DEPENDENCY_COMPILE_COMMAND_ARTIFACTS["ov_init"]
            entries = json.loads(path.read_text(encoding="utf-8"))
            if len(entries) != DEPENDENCY_EXPECTED_COMMAND_COUNTS["ov_init"]:
                raise RuntimeError("synthetic ov_init command inventory changed")
            duplicated_source = entries[0]["file"]
            tokens = shlex.split(entries[1]["command"])
            tokens[tokens.index("-c") + 1] = duplicated_source
            entries[1]["file"] = duplicated_source
            entries[1]["command"] = " ".join(shlex.quote(token) for token in tokens)
            write_json_fixture(path, entries)

        corruption(
            "ov-init-dependency-duplicate-source",
            duplicate_ov_init_source,
            expected_error="ov_init compile commands do not have unique normalized sources",
        )

        def escape_ov_core_output(root):
            rewrite_dependency_compile_tokens(
                root,
                "ov_core",
                lambda tokens: replace_exact_token(
                    tokens,
                    tokens[tokens.index("-o") + 1],
                    "../escaped-object.o",
                ),
            )

        corruption(
            "ov-core-dependency-output-root",
            escape_ov_core_output,
            expected_error=(
                "ov_core command 0 for ov_core/src/synthetic_abi_probe_0.cpp fails "
                "dependency ABI/source/output proof"
            ),
        )

        def remove_shadow_math_test_source(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            retained = [
                entry for entry in entries
                if not (
                    "CMakeFiles/test_cp2_shadow_math.dir/" in str(entry.get("output", ""))
                    and str(entry.get("file", "")).endswith(
                        "/ov_msckf/test/cp2/test_cp2_shadow_math.cpp"
                    )
                )
            ]
            if len(retained) != len(entries) - 1:
                raise RuntimeError("synthetic fixture lost the shadow-math target source")
            write_json_fixture(path, retained)

        corruption(
            "shadow-math-target-source-inventory",
            remove_shadow_math_test_source,
            expected_error="test_cp2_shadow_math strict-FP source inventory mismatch",
        )

        def fail_status(root):
            mutate_json(
                root / "test_cp2_fej_golden.json",
                lambda record: record.__setitem__("exit_status", 9),
            )

        corruption("test-exit-status", fail_status)

        def bad_fixture_count(root):
            path = root / "test_cp2_production_schur_reducer.log"
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "CP2_A_ACCEPTED fixtures=1024", "CP2_A_ACCEPTED fixtures=1023"
                ),
                encoding="utf-8",
            )

        corruption("fixture-counter", bad_fixture_count)

        def refresh_log_evidence(root, test_names):
            parse_errors = []
            summaries = parse_summaries(root, parse_errors)
            gtest = parse_gtest_xml(root, parse_errors)
            report = json.loads((root / REPORT_NAME).read_text(encoding="utf-8"))
            report["summaries"] = summaries
            report["gtest"] = gtest
            for test_name in test_names:
                invocation = report["test_invocations"][test_name]
                invocation["log_sha256"] = sha256_file(root / (test_name + ".log"))
                invocation["xml_sha256"] = sha256_file(root / (test_name + ".xml"))
            write_json_fixture(root / REPORT_NAME, report)

        def outside_execution_root(root):
            test_name = "test_cp2_fej_golden"
            mutate_json(
                root / (test_name + ".json"),
                lambda record: record.__setitem__(
                    "argv",
                    [
                        "/tmp/outside/binaries/" + test_name, *GTEST_OPTIONS,
                        "--gtest_output=xml:/tmp/outside/" + test_name + ".xml",
                    ],
                ),
            )
            report = json.loads((root / REPORT_NAME).read_text(encoding="utf-8"))
            report["test_invocations"][test_name]["argv"] = json.loads(
                (root / (test_name + ".json")).read_text(encoding="utf-8")
            )["argv"]
            write_json_fixture(root / REPORT_NAME, report)

        corruption("outside-execution-root-coordinated", outside_execution_root)

        def swap_same_count_xml(root):
            first = root / "test_cp1_rank_rejection.xml"
            second = root / "test_cp1_prior_and_compression.xml"
            first_bytes, second_bytes = first.read_bytes(), second.read_bytes()
            first.write_bytes(second_bytes)
            second.write_bytes(first_bytes)
            refresh_log_evidence(
                root, ["test_cp1_rank_rejection", "test_cp1_prior_and_compression"]
            )

        corruption("same-count-xml-swap-coordinated", swap_same_count_xml)

        def skipped_xml(root):
            test_name = "test_cp2_fej_golden"
            path = root / (test_name + ".xml")
            tree = ET.parse(str(path))
            case = tree.getroot().find(".//testcase")
            case.set("result", "skipped")
            ET.SubElement(case, "skipped")
            tree.write(str(path), encoding="utf-8", xml_declaration=True)
            refresh_log_evidence(root, [test_name])

        corruption("skipped-testcase-coordinated", skipped_xml)

        def relocate_summary(root):
            source_log = root / "test_cp1_schur_equivalence.log"
            destination_log = root / "test_cp1_rank_rejection.log"
            lines = source_log.read_text(encoding="utf-8").splitlines()
            moved = [line for line in lines if line.startswith("CP1_EQUIVALENCE ")]
            source_log.write_text(
                "\n".join(line for line in lines if line not in moved) + "\n", encoding="utf-8"
            )
            with destination_log.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(moved) + "\n")
            refresh_log_evidence(
                root, ["test_cp1_schur_equivalence", "test_cp1_rank_rejection"]
            )

        corruption("summary-relocation-coordinated", relocate_summary)

        def unsafe_fp_after_strict(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries[0]["command"] += " -funsafe-math-optimizations"
            write_json_fixture(path, entries)

        corruption("unsafe-fp-after-strict", unsafe_fp_after_strict)

        def response_file_fp(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            entries[0]["command"] += " @hidden-flags.rsp"
            write_json_fixture(path, entries)

        corruption("strict-fp-response-file", response_file_fp)

        def actual_source_spoof(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            tokens = shlex.split(entries[0]["command"])
            tokens[tokens.index("-c") + 1] = "/tmp/spoofed-SchurUpdate.cpp"
            entries[0]["command"] = " ".join(shlex.quote(token) for token in tokens)
            write_json_fixture(path, entries)

        corruption("actual-c-source-spoof", actual_source_spoof)

        def tolerance_ratio_exceeded(root):
            test_name = "test_cp2_production_schur_reducer"
            path = root / (test_name + ".log")
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "max_lambda_tolerance_ratio=0", "max_lambda_tolerance_ratio=1.0001"
                ),
                encoding="utf-8",
            )
            refresh_log_evidence(root, [test_name])

        corruption("cp2-a-tolerance-ratio-exceeded", tolerance_ratio_exceeded)

        def escalate_checkpoint(root):
            def edit(report):
                report["checkpoint_status"]["CP2-C"] = "passed"
                report["eligible_for_cp2_seal"] = True
                report["artifact_policy"]["eligible_for_cp2_seal"] = True
            mutate_json(root / REPORT_NAME, edit)

        corruption("cp2-cde-status-escalation", escalate_checkpoint)

        def coordinated_binary_replacement(root, replacement):
            test_name = "test_cp2_fej_golden"
            binary = root / "binaries" / test_name
            shutil.copyfile(str(replacement), str(binary))
            binary.chmod(0o700)
            linkage_errors = []
            linkage = collect_elf_linkage(root, linkage_errors)
            status_path = root / (test_name + ".json")
            status_record = json.loads(status_path.read_text(encoding="utf-8"))
            hashes = execution_input_hashes(root, test_name)
            status_record["execution_inputs_sha256_before"] = hashes
            status_record["execution_inputs_sha256_after"] = hashes
            status_record["loader_map"] = linkage["executables"][test_name]["loader"]
            write_json_fixture(status_path, status_record)
            dependency_path = root / DEPENDENCY_INVENTORY_NAME
            dependency = json.loads(dependency_path.read_text(encoding="utf-8"))
            dependency["loader_maps"][test_name] = status_record["loader_map"]
            write_json_fixture(dependency_path, dependency)
            records, binaries, library = collect_test_records(root, linkage, [])
            report = json.loads((root / REPORT_NAME).read_text(encoding="utf-8"))
            report["elf_and_linkage"] = linkage
            report["dependency_inventory"] = dependency
            report["test_invocations"] = records
            report["binary_sha256"] = binaries
            report["production_library"] = library
            write_json_fixture(root / REPORT_NAME, report)

        corruption(
            "text-executable-coordinated",
            lambda root: coordinated_binary_replacement(root, root / "test_cp2_fej_golden.log"),
        )
        corruption(
            "bin-true-coordinated",
            lambda root: coordinated_binary_replacement(root, Path("/bin/true")),
        )

        def historical_source_substitution(root):
            parent = git_text(repo_root, "rev-parse", "HEAD^")
            parent_tree = git_text(repo_root, "rev-parse", parent + "^{tree}")
            for name in ("source_before.json", "source_after.json"):
                mutate_json(
                    root / name,
                    lambda snapshot: snapshot.update({"commit": parent, "tree": parent_tree}),
                )
            source_errors = []
            substituted = collect_source_metadata(
                root, repo_root, source_errors, allow_synthetic=True
            )
            mutate_json(
                root / REPORT_NAME,
                lambda report: report.__setitem__("source", substituted),
            )

        # The synthetic repository has a parent via the real project history only
        # when available; use a branch mismatch otherwise.
        try:
            git_text(repo_root, "rev-parse", "HEAD^")
        except subprocess.CalledProcessError:
            corruption(
                "live-source-binding",
                lambda root: mutate_json(
                    root / "source_before.json",
                    lambda snapshot: snapshot.__setitem__("branch", "forged/branch"),
                ),
            )
        else:
            corruption("historical-source-substitution-coordinated", historical_source_substitution)

        anchor_status, anchor_errors = verify_unit_report(
            base, repo_root, quiet=True, expected_manifest_sha256="0" * 64,
            allow_synthetic=True,
        )
        if anchor_status == 0:
            raise RuntimeError("incorrect external manifest anchor was accepted")
        corruptions.append({"name": "external-manifest-anchor", "detected_errors": len(anchor_errors)})

        hardlink_root = temporary_root / "corrupt-hardlink"
        shutil.copytree(str(base), str(hardlink_root))
        set_synthetic_tree_modes(hardlink_root, writable=True)
        linked = hardlink_root / "test_cp2_fej_golden.log"
        external_link_source = temporary_root / "hardlink-source.log"
        shutil.copyfile(str(linked), str(external_link_source))
        linked.unlink()
        os.link(str(external_link_source), str(linked))
        set_synthetic_tree_modes(hardlink_root, writable=False)
        hardlink_status, hardlink_errors = verify_unit_report(
            hardlink_root, repo_root, quiet=True, allow_synthetic=True
        )
        if hardlink_status == 0:
            raise RuntimeError("hardlinked finalized artifact was accepted")
        corruptions.append({"name": "hardlink-nlink", "detected_errors": len(hardlink_errors)})

        writable_root = temporary_root / "corrupt-writable-mode"
        shutil.copytree(str(base), str(writable_root))
        (writable_root / "cp2_report.json").chmod(0o644)
        writable_status, writable_errors = verify_unit_report(
            writable_root, repo_root, quiet=True, allow_synthetic=True
        )
        if writable_status == 0:
            raise RuntimeError("writable finalized artifact was accepted")
        corruptions.append({"name": "writable-final-mode", "detected_errors": len(writable_errors)})

        source = temporary_root / "rename-source"
        source.mkdir()
        destination = temporary_root / "rename-destination"
        destination.mkdir()
        try:
            atomic_rename_noreplace(source, destination)
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
        else:
            raise RuntimeError("atomic no-overwrite self-test replaced an existing destination")
        if not source.is_dir() or not destination.is_dir():
            raise RuntimeError("atomic no-overwrite failure damaged a source/destination")

        passing_source = temporary_root / "rename-passing-source"
        passing_source.mkdir()
        passing_destination = temporary_root / "rename-passing-destination"
        atomic_rename_noreplace(passing_source, passing_destination)
        if passing_source.exists() or not passing_destination.is_dir():
            raise RuntimeError("atomic no-overwrite passing rename did not complete")

        print("CP2 verifier self-test passed using only: " + str(temporary_root))
        print("Synthetic corruptions rejected: " + ", ".join(item["name"] for item in corruptions))
    return 0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument(
        "--assemble-unit", nargs=2, metavar=("ARTIFACT_DIR", "REPO_ROOT"), type=Path,
        help="assemble a schema-1 unit report and SHA256SUMS in a new artifact directory",
    )
    parser.add_argument(
        "--finalize-staging-noreplace", nargs=2,
        metavar=("SOURCE_DIR", "DESTINATION_DIR"), type=Path,
        help="validate, freeze, fsync, and atomically finalize CP2-A/B staging only",
    )
    parser.add_argument(
        "--expected-manifest-sha256", metavar="HEX",
        help="require SHA256SUMS to match this externally retained SHA-256 digest",
    )
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    selected_modes = (
        int(args.self_test) + int(args.assemble_unit is not None)
        + int(args.finalize_staging_noreplace is not None)
    )
    if selected_modes > 1 or (selected_modes and args.paths):
        raise ValueError("select exactly one verifier mode")
    if args.self_test:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("--expected-manifest-sha256 is verification-only")
        return run_self_test()
    if args.assemble_unit is not None:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("--expected-manifest-sha256 is verification-only")
        return assemble_unit_report(args.assemble_unit[0], args.assemble_unit[1])
    if args.finalize_staging_noreplace is not None:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("--expected-manifest-sha256 is verification-only")
        finalize_staging_noreplace(
            args.finalize_staging_noreplace[0], args.finalize_staging_noreplace[1]
        )
        print(
            "Read-only CP2-A/B staging finalized without overwrite (not a CP2 seal): "
            + str(args.finalize_staging_noreplace[1])
        )
        return 0
    if len(args.paths) != 2:
        raise ValueError("verification requires ARTIFACT_DIR REPO_ROOT")
    status, _ = verify_unit_report(
        args.paths[0], args.paths[1],
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERROR: " + type(exc).__name__ + ": " + str(exc))
        raise SystemExit(2)
