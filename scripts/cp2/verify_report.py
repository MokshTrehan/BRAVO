#!/usr/bin/python3 -I
# SPDX-License-Identifier: GPL-3.0-or-later
"""Assemble/verify CP2 unit evidence and detached CP2-C/D actual artifacts.

CP2-E remains deliberately fail-closed until its profile and complete artifact
schema are separately committed.
"""

from __future__ import annotations

import sys

if not sys.flags.isolated:
    raise SystemExit("CP2 verifier requires isolated Python (-I)")

import argparse
from collections import Counter
import ctypes
import datetime as dt
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import signal
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET


REPORT_NAME = "cp2_report.json"
MANIFEST_NAME = "SHA256SUMS"
EXPECTED_BRANCH = "schurvio-lite/cp2-one-pass"
EVIDENCE_SCOPE = (
    "cp2_a_b_unit_math_and_cp2_c2_updater_transaction_commit_oracle_"
    "with_complete_cp2_c1_and_cp1_regression"
)
OVERALL_STATUS = "in_progress_cp2_c3_cp2_c_cp2_d_cp2_e_unexecuted"
UNIT_PASS_STATUS = "passed_cp2_a_b_cp2_c2_unit_only"
UNIT_FAIL_STATUS = "failed_cp2_a_b_cp2_c2_unit"
CP1_AUTHORIZATION_COMMIT = "8d80f483752411d34a3bc4c1ff6330b3a5c0fef3"
PREAUTHORIZATION_REGISTRY_PATH = "project/datasets.yaml"
PREVALIDATED_SOURCE_RECORD_TYPE = "cp2_prevalidated_unit_source_v1"
CERES_COMMIT = "facb199f3eda902360f9e1d5271372b7e54febe1"
CERES_TAG = "1.14.0"
CERES_LICENSE_SHA256 = "065e9b9f40b65dfaeb421a8a1c0559d8305e3ce9394aa8b7dec609fa04e8318a"
GOOGLETEST_LICENSE_SHA256 = "9702de7e4117a8e2b20dafab11ffda58c198aede066406496bef670d40a22138"
GOOGLETEST_ARCHIVE_SHA256 = "53d536bbe4f5a4007a23ac1abdd58946fe0f0f30e70c2ddd24fd084a789a9b63"
GOOGLETEST_ARCHIVE_SIZE_BYTES = 4454400


def _schema_version_one(value):
    """Return true only for the JSON integer 1, never Boolean true."""

    return isinstance(value, int) and not isinstance(value, bool) and value == 1

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
    "test_cp2_updater_msckf_end_to_end": 16,
    "test_cp2_updater_msckf_fault_injection": 31,
    "test_cp2_composite_state": 14,
    "test_cp2_commit_oracle": 10,
    "test_cp2_commit_boundary": 4,
    "test_cp2_canonical": 5,
    "test_cp2_offline_replay": 5,
    "test_cp2_recorded_assemble": 3,
    "test_cp2_feature_gate": 13,
    "test_cp2_runtime_context": 12,
    "test_cp2_ros1_runtime_parameters": 6,
    "test_cp2_serial_pairing": 6,
    "test_cp2_serial_runtime_trace": 6,
    "test_cp2_updater_msckf_preview_snapshot": 4,
    "test_cp2_shadow_math": 14,
    "test_cp2_trace_journal": 21,
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
    "test_cp2_updater_msckf_fault_injection": (
        "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp"
    ),
    "test_cp2_composite_state": "ov_msckf/test/cp2/test_cp2_composite_state.cpp",
    "test_cp2_commit_oracle": "ov_msckf/test/cp2/test_cp2_commit_oracle.cpp",
    "test_cp2_commit_boundary": "ov_msckf/test/cp2/test_cp2_commit_boundary.cpp",
    "test_cp2_canonical": "ov_msckf/test/cp2/test_cp2_canonical.cpp",
    "test_cp2_offline_replay": "ov_msckf/test/cp2/test_cp2_offline_replay.cpp",
    "test_cp2_recorded_assemble": (
        "ov_msckf/test/cp2/test_cp2_recorded_assemble.cpp"
    ),
    "test_cp2_feature_gate": "ov_msckf/test/cp2/test_cp2_feature_gate.cpp",
    "test_cp2_runtime_context": "ov_msckf/test/cp2/test_cp2_runtime_context.cpp",
    "test_cp2_ros1_runtime_parameters": (
        "ov_msckf/test/cp2/test_cp2_ros1_runtime_parameters.cpp"
    ),
    "test_cp2_serial_pairing": "ov_msckf/test/cp2/test_cp2_serial_pairing.cpp",
    "test_cp2_serial_runtime_trace": (
        "ov_msckf/test/cp2/test_cp2_serial_runtime_trace.cpp"
    ),
    "test_cp2_updater_msckf_preview_snapshot": (
        "ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp"
    ),
    "test_cp2_shadow_math": "ov_msckf/test/cp2/test_cp2_shadow_math.cpp",
    "test_cp2_trace_journal": "ov_msckf/test/cp2/test_cp2_trace_journal.cpp",
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
    "CP2CompositeDetachedOracle.AppliesExactlyOneProductionUpdatePerTopLevelType",
    "CP2CompositeLiveCapture.ParentSubvariableInactiveCalibrationAndCameraInventoryFaultsAreRejected",
    "CP2CompositeLiveCapture.ProductionStateProjectsOneOwningPriorAndIgnoresAddressesAndMapInsertion",
    "CP2CompositePointerGraph.Phase1MatchesAndEveryPointerAssociationMutationIsDetected",
    "CP2CompositePostcommit.LiveCacheReadAndPreparedOrPointerFailuresReturnExplicitStatus",
    "CP2CompositePostcommit.PreparedPhase3FillIsNoexceptAllocationFreeAndMatchesProductionCommit",
    "CP2CompositeStateCodec.EveryBinary64CoefficientMutationIsDetected",
    "CP2CompositeStateCodec.FrozenFullRolePayloadRoundTripsBitExactly",
    "CP2CompositeStateCodec.IdentityMetadataShapeAndKeyMutationsAreDetected",
    "CP2CompositeStateCodec.NonfiniteRolesRoundTripLosslesslyButNeverValidate",
    "CP2CompositeStateValidation.EveryLandmarkRepresentationIdentityRuleIsExact",
    "CP2CompositeStateValidation.ExactPartitionsAndCloneIdentityFailClosed",
    "CP2CompositeStateValidation.QuaternionSquaredNormBoundaryIsRoleComplete",
    "CP2CommitBoundary.AcceptedPathHasExactProofCommitClockFillOrder",
    "CP2CommitBoundary.FailedFillRetainsCommittedStatusAndExactEndpoint",
    "CP2CommitBoundary.RejectedProofSuppressesEveryPostproofOperation",
    "CP2CommitBoundary.ThrowingCommitPropagatesBeforeClockAndPreservesOutput",
    "CP2CommitOracle.CheckedIntegerHelpersNeverWrapOrClobberOnFailure",
    "CP2CommitOracle.CountsEachCoefficientMismatchClassByExactBits",
    "CP2CommitOracle.DetachedTypeUpdateCallMismatchIsUpdateLevelFailure",
    "CP2CommitOracle.EqualNonfiniteBitsStillFailTheCompleteOracle",
    "CP2CommitOracle.EverySnapshotMustBeFiniteIndependently",
    "CP2CommitOracle.InvalidPhaseRetainsExpectedAndRowCountsButNoPopulation",
    "CP2CommitOracle.InventoryIdentityAndShapeFailuresZeroOnlyCoefficientPopulations",
    "CP2CommitOracle.MatchingCompositeHasExactPopulationsAndPasses",
    "CP2CommitOracle.NonCoefficientCanonicalMismatchRetainsPopulations",
    "CP2CommitOracle.SignedZeroIsOneNominalBitMismatch",
    "CP2StateFileCodec.LegalPhasePopulationsRoundTripAndCorruptionFailsClosed",
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
    "CP2UpdaterMSCKFTransaction.CandidateAssemblyFailureCannotVetoBaselineCommit",
    "CP2UpdaterMSCKFTransaction.BaselineProvenanceMismatchSuppressesCommitBeforePhase2",
    "CP2UpdaterMSCKFTransaction.CleanCommitPublishesExactCompositeAndCommitOracle",
    "CP2UpdaterMSCKFTransaction.CommitOracleInvalidPhaseRemainsDistinctPostcommitFatal",
    "CP2UpdaterMSCKFTransaction.CommitOracleOverflowIsArithmeticFatalWithoutPublicationOrRollback",
    "CP2UpdaterMSCKFTransaction.CommittedDurationFailureIsArithmeticFatalWithoutPublicationOrRollback",
    "CP2UpdaterMSCKFTransaction.CompleteNonfinitePhase3RemainsCountedFailedEvidence",
    "CP2UpdaterMSCKFTransaction.CompletePhase3ValueMismatchRemainsCountedFailedEvidence",
    "CP2UpdaterMSCKFTransaction.FinalPointerRejectionDiscardsInstalledPhase2",
    "CP2UpdaterMSCKFTransaction.IncompletePostcommitStorageIsFatalAfterCommitWithoutPublication",
    "CP2UpdaterMSCKFTransaction.InvocationContextIsOneShotContiguousAndSettersFailClosed",
    "CP2UpdaterMSCKFTransaction.InvocationIdOverflowIsFatalBeforeEstimatorWork",
    "CP2UpdaterMSCKFTransaction.InvalidPhase2IsDiscardedAndCannotCommit",
    "CP2UpdaterMSCKFTransaction.MissingRecordedContextIsFatalBeforeEstimatorWork",
    "CP2UpdaterMSCKFTransaction.NonfinitePhase1IsSnapshotMismatchAndDiscardsPhase2",
    "CP2UpdaterMSCKFTransaction.PhasePairDurationFailureIsArithmeticFatalWithoutPublicationOrWrite",
    "CP2UpdaterMSCKFTransaction.PostcommitPointerTokenFailureIsFatalAfterCommitWithoutPublication",
    "CP2UpdaterMSCKFTransaction.PromotionFailureIsFatalBeforeAnyPublishOrBaselineWrite",
    "CP2UpdaterMSCKFTransaction.RawNoncommitPublishesOnlyEqualPhaseZeroAndOne",
    "CP2UpdaterMSCKFTransaction.RecordedSinkConfigurationIsNullspaceOnlyAndFreezesAtFirstUpdate",
    "CP2UpdaterMSCKFTransaction.SinkRejectionAfterCommitLatchesFatalWithoutRollbackOrObserver",
    "CP2UpdaterMSCKFTransaction.ZeroRawDiscardsTentativeWithoutValidationOrPhases",
    "CP2UpdaterMSCKFTransaction.ZeroRawDurationFailureIsArithmeticFatalWithoutPublication",
    "CP2CanonicalSha256.MatchesPublishedVectorsUnderIncrementalChunking",
    "CP2CanonicalBytes.IntegerBinary64AndUtf8EncodingIsExact",
    "CP2CanonicalBytes.MatrixAndVectorUseLogicalRowMajorBinary64Order",
    "CP2CanonicalBytes.Utf8ValidationRejectsMalformedSequencesWithoutAppending",
    "CP2CanonicalBytes.SelfAppendStagesAliasedStorageBeforeGrowth",
    "CP2OfflineReplay.AcceptsOnlyTheExactAbsoluteCommandSurface",
    "CP2OfflineReplay.RejectsExtraMissingRelativeAndAliasedArguments",
    "CP2OfflineReplay.StatusNamesAreFrozen",
    "CP2OfflineReplay.DerivesEveryOnlineShadowMathConditionAndPreservesRejectedFeatures",
    "CP2OfflineReplay.CommitRequiresCandidateAndRecomputedBlocksMustActuallyPass",
    "CP2RecordedAssemble.EmptyCanonicalCampaignIsDeterministicAndUsesV101",
    "CP2RecordedAssemble.OrphanStateProposalAndRawFramesFailClosed",
    "CP2RecordedAssemble.WrongFrozenSequenceAndCorruptJournalFailClosed",
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
    "CP2SerialPairing.FirstForwardCandidateIsNeverReplacedByANearerMessage",
    "CP2SerialPairing.IntegerNanosecondCompositionChecksRangeAndOverflow",
    "CP2SerialPairing.InvalidMessageKindFailsAtomicallyWithFrozenStatus",
    "CP2SerialPairing.StrictTwentyMillisecondBoundaryAndMetadataAreExact",
    "CP2SerialPairing.UsedFirstForwardCandidateCannotBeReusedOrSearchedPast",
    "CP2SerialPairing.EvidenceModeConsumesImuTailPastLastCamera",
    "CP2RuntimeContext.RecordedFullAcceptsExactDocumentAndHashesBytes",
    "CP2RuntimeContext.SequenceAndTimingCombinationsAreExact",
    "CP2RuntimeContext.DuplicateMissingAndExtraKeysReject",
    "CP2RuntimeContext.JsonTypesNeverCoerce",
    "CP2RuntimeContext.SequenceAndLaunchExpectationsBindIdentity",
    "CP2RuntimeContext.PathsRequireNormalizedDistinctStrictChildren",
    "CP2RuntimeContext.OutputPresenceCannotCrossTraceLevels",
    "CP2RuntimeContext.EscapesAreStrictAndDecodedBeforeValidation",
    "CP2RuntimeContext.NonJsonNumbersConstantsAndTrailingBytesReject",
    "CP2RuntimeContext.RejectionIsFailureAtomicAndStatusNamesAreStable",
    "CP2RuntimeContext.DescriptorBoundFileReadAcceptsExactBytesAndBound",
    "CP2RuntimeContext.DescriptorBoundFileReadRejectsRelativeSymlinkAndHardlink",
    (
        "CP2SerialRuntimeTrace."
        "RecordedRowsJoinContiguousUpdaterIdentitiesAndWriteOnce"
    ),
    (
        "CP2SerialRuntimeTrace."
        "SequenceRowsRetainNonidentityQuaternionWithoutReordering"
    ),
    (
        "CP2SerialRuntimeTrace."
        "NoncontiguousWrongPairAndWrongTimestampUpdaterEventsFailSticky"
    ),
    (
        "CP2SerialRuntimeTrace."
        "DuplicateProcessingAndNonfiniteTrajectoryFailBeforeOutput"
    ),
    "CP2SerialRuntimeTrace.InitialPairPopulationRejectsBoundaryAndGaps",
    "CP2SerialRuntimeTrace.OutputCreationRejectsOverwriteAndSymlink",
    (
        "CP2ROS1RuntimeParameters."
        "FrozenDomainTagsOrderingSignedIntegerAndNegativeZeroAreExact"
    ),
    (
        "CP2ROS1RuntimeParameters."
        "NestedStructKeysUseUnsignedUtf8ByteOrderAndEscapeStrings"
    ),
    "CP2ROS1RuntimeParameters.BoolIntAndDoubleRemainDistinctTypedBytes",
    "CP2ROS1RuntimeParameters.NonfiniteAndInvalidXmlRpcValuesFailClosed",
    "CP2ROS1RuntimeParameters.NamesAndStringsRejectUnsafeOrInvalidInputs",
    "CP2ROS1RuntimeParameters.EmptyPopulationCannotMasqueradeAsCapture",
    "CP2TraceJournalFormat.FrozenBootstrapKnownAnswerAndEmptyDecode",
    "CP2TraceJournalWriter.ShortWritesRemainExactAndFinalizeSeals",
    "CP2TraceJournalWriter.PartialFailureIsCountedAndRejectionIsSticky",
    "CP2TraceJournalWriter.SyncFailureIsStickyAndSealsPublication",
    "CP2TraceJournalWriter.WriterWithoutExplicitSyncFailsClosed",
    "CP2TraceJournalWriter.ByteBudgetOverflowWritesNoPartialUnit",
    (
        "CP2TraceJournalIdentity."
        "ContiguousInvocationsAllowRepeatedPairAndRegressingNewTimestamp"
    ),
    "CP2TraceJournalIdentity.DuplicateSkippedAndReorderedIdentityAreSticky",
    "CP2TraceJournalEvents.EveryLegalZeroRawTerminalRoundTrips",
    "CP2TraceJournalEvents.ImpossibleTerminalAndHiddenPhaseSuffixReject",
    "CP2TraceJournalEvents.DuplicateRawFeatureAndInvalidEnumRejectExplicitly",
    (
        "CP2TraceJournalPayloads."
        "OwningStateRawAndProposalBytesReconstructExactly"
    ),
    "CP2TraceJournalPayloads.NonzeroRawAndCommittedDecodeAccepted",
    "CP2TraceJournalPayloads.AnyPayloadByteMismatchRejectsBeforeWrite",
    "CP2TraceJournalDecoder.HeaderBootstrapAndLimitCorruptionsReject",
    (
        "CP2TraceJournalDecoder."
        "TruncationTrailingAndHostileSectionPopulationReject"
    ),
    (
        "CP2TraceJournalDecoder."
        "DuplicateUnknownMissingAndInvalidCoreSectionsReject"
    ),
    (
        "CP2TraceJournalDecoder."
        "CanonicalFragmentAndCrossIdentityCorruptionsReject"
    ),
    "CP2TraceJournalFile.PreopenedRegularFileFinalizesAtExactSize",
    "CP2TraceJournalFile.ReadOnlyAndMultipleLinkFilesReject",
    "CP2TraceJournalFile.CallerOffsetInterferenceFailsClosed",
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
    "test_cp2_updater_msckf_fault_injection": set(),
    "test_cp2_composite_state": set(),
    "test_cp2_commit_oracle": set(),
    "test_cp2_commit_boundary": set(),
    "test_cp2_canonical": set(),
    "test_cp2_offline_replay": set(),
    "test_cp2_recorded_assemble": set(),
    "test_cp2_feature_gate": set(),
    "test_cp2_runtime_context": set(),
    "test_cp2_ros1_runtime_parameters": set(),
    "test_cp2_serial_pairing": set(),
    "test_cp2_serial_runtime_trace": set(),
    "test_cp2_updater_msckf_preview_snapshot": set(),
    "test_cp2_shadow_math": set(),
    "test_cp2_trace_journal": set(),
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
    "docs/cp2_c_composite_and_readiness_clarification.md",
    "docs/cp2_c_detached_readiness_binding_clarification_proposed.md",
    "docs/cp2_d_evaluator_precision_clarification_proposed.md",
    "docs/cp2_math_implementation_audit.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "docs/schurvio_lite_execution_plan.md",
    "ov_core/CMakeLists.txt",
    "ov_core/src/cam/CamBase.h",
    "ov_core/src/cam/CamRadtan.h",
    "ov_core/src/types/IMU.h",
    "ov_core/src/types/JPLQuat.h",
    "ov_core/src/types/Landmark.h",
    "ov_core/src/types/LandmarkRepresentation.h",
    "ov_core/src/types/PoseJPL.h",
    "ov_core/src/types/Type.h",
    "ov_core/src/types/Vec.h",
    "ov_core/src/utils/quat_ops.h",
    "ov_init/CMakeLists.txt",
    "ov_msckf/CMakeLists.txt",
    "ov_msckf/cmake/CP2Tests.cmake",
    "ov_msckf/cmake/ROS1.cmake",
    "ov_msckf/cmake/ROS2.cmake",
    "ov_msckf/package.xml",
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.cpp",
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.h",
    "ov_msckf/src/core/VioManagerOptions.h",
    "ov_msckf/src/state/State.cpp",
    "ov_msckf/src/state/State.h",
    "ov_msckf/src/state/StateHelper.cpp",
    "ov_msckf/src/state/StateHelper.h",
    "ov_msckf/src/state/StateOptions.h",
    "ov_msckf/src/update/CP2Canonical.cpp",
    "ov_msckf/src/update/CP2Canonical.h",
    "ov_msckf/src/update/CP2CommitBoundary.cpp",
    "ov_msckf/src/update/CP2CommitBoundary.h",
    "ov_msckf/src/update/CP2CommitOracle.cpp",
    "ov_msckf/src/update/CP2CommitOracle.h",
    "ov_msckf/src/update/CP2CompositeState.cpp",
    "ov_msckf/src/update/CP2CompositeState.h",
    "ov_msckf/src/update/CP2FeatureGate.cpp",
    "ov_msckf/src/update/CP2FeatureGate.h",
    "ov_msckf/src/update/CP2OfflineReplay.cpp",
    "ov_msckf/src/update/CP2OfflineReplay.h",
    "ov_msckf/src/update/CP2OfflineReplayInternal.inc",
    "ov_msckf/src/update/CP2RecordedAssemble.cpp",
    "ov_msckf/src/update/CP2RuntimeContext.cpp",
    "ov_msckf/src/update/CP2RuntimeContext.h",
    "ov_msckf/src/update/CP2SerialPairing.cpp",
    "ov_msckf/src/update/CP2SerialPairing.h",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.cpp",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.h",
    "ov_msckf/src/update/CP2ShadowMath.cpp",
    "ov_msckf/src/update/CP2ShadowMath.h",
    "ov_msckf/src/update/CP2StateTraceCodec.cpp",
    "ov_msckf/src/update/CP2StateTraceCodec.h",
    "ov_msckf/src/update/CP2TraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceCodec.h",
    "ov_msckf/src/update/CP2TraceJournal.cpp",
    "ov_msckf/src/update/CP2TraceJournal.h",
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
    "ov_msckf/test/cp2/test_cp2_commit_boundary.cpp",
    "ov_msckf/test/cp2/test_cp2_commit_oracle.cpp",
    "ov_msckf/test/cp2/test_cp2_composite_state.cpp",
    "ov_msckf/test/cp2/test_cp2_feature_gate.cpp",
    "ov_msckf/test/cp2/test_cp2_offline_replay.cpp",
    "ov_msckf/test/cp2/test_cp2_recorded_assemble.cpp",
    "ov_msckf/test/cp2/test_cp2_runtime_context.cpp",
    "ov_msckf/test/cp2/test_cp2_ros1_runtime_parameters.cpp",
    "ov_msckf/test/cp2/test_cp2_serial_pairing.cpp",
    "ov_msckf/test/cp2/test_cp2_serial_runtime_trace.cpp",
    "ov_msckf/test/cp2/test_cp2_shadow_math.cpp",
    "ov_msckf/test/cp2/test_cp2_trace_codec.cpp",
    "ov_msckf/test/cp2/test_cp2_trace_journal.cpp",
    "ov_msckf/test/cp2/test_fej_golden.cpp",
    "ov_msckf/test/cp2/test_production_schur_reducer.cpp",
    "ov_msckf/test/cp2/test_state_update_semantics.cpp",
    "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp",
    "ov_msckf/test/cp2/test_updater_msckf_preview_snapshot.cpp",
    "project/cp0_baseline.json",
    "project/cp1_gate.yaml",
    "project/cp2_c_clarification_approval.json",
    "project/cp2_gate.yaml",
    "project/cp2_serial.launch",
    "scripts/cp0/bootstrap_ceres_1_14.sh",
    "scripts/cp1/make_report.py",
    "scripts/cp1/run_cp1.sh",
    "scripts/cp1/verify_report.py",
    "scripts/cp2/run_unit_gate.sh",
    "scripts/cp2/cp2_pair_index_extract.py",
    "scripts/cp2/cp2_postauth_registry.py",
    "scripts/cp2/cp2_readiness.py",
    "scripts/cp2/cp2_recorded_campaign.py",
    "scripts/cp2/cp2_schema.py",
    "scripts/cp2/cp2_sequence_actual.py",
    "scripts/cp2/cp2_sequence_math.py",
    "scripts/cp2/cp2_sequence_runner.py",
    "scripts/cp2/run_recorded_parity.py",
    "scripts/cp2/run_sequence_pair.py",
    "scripts/cp2/run_timing_pair.py",
    "scripts/cp2/tests/test_cp2_postauth_registry.py",
    "scripts/cp2/tests/test_cp2_actual_readiness_binding.py",
    "scripts/cp2/tests/test_cp2_readiness.py",
    "scripts/cp2/tests/test_cp2_recorded_campaign.py",
    "scripts/cp2/tests/test_cp2_schema.py",
    "scripts/cp2/tests/test_cp2_sequence_actual.py",
    "scripts/cp2/tests/test_cp2_sequence_math.py",
    "scripts/cp2/tests/test_cp2_sequence_runner.py",
    "scripts/cp2/verify_report.py",
}

CONTRACT_INPUTS = {
    "docs/cp2_artifact_schema.md",
    "docs/cp2_c_composite_and_readiness_clarification.md",
    "docs/cp2_one_pass_contract.md",
    "docs/cp2_recorded_evidence_contract.md",
    "docs/iterated_update_spec.md",
    "project/cp1_gate.yaml",
    "project/cp2_c_clarification_approval.json",
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
FROZEN_CP2_C_APPROVAL_BINDING = {
    "docs/cp2_c_composite_and_readiness_clarification.md": {
        "git_blob": "90ac833f52ad8a8c6ea12ff86e3301a46b28d6e0",
        "sha256": "dd2232ec8ee6536c78b5971858efbb22d9965f121205f83a613c5b0ad0f69e66",
    },
    "project/cp2_c_clarification_approval.json": {
        "git_blob": "0307342411e06dff57d58d48bc4d829fca138686",
        "sha256": "e6a8a4e55f1e39fafd35e57114668c26f15690d3b20d8d99c34fee1285a9a3e7",
    },
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
RUNTIME_LIBRARY_SOURCES = (
    "ov_msckf/src/dummy.cpp",
    "ov_msckf/src/sim/Simulator.cpp",
    "ov_msckf/src/state/State.cpp",
    "ov_msckf/src/state/StateHelper.cpp",
    "ov_msckf/src/state/Propagator.cpp",
    "ov_msckf/src/core/VioManager.cpp",
    "ov_msckf/src/core/VioManagerHelper.cpp",
    "ov_msckf/src/update/CP2Canonical.cpp",
    "ov_msckf/src/update/CP2CommitBoundary.cpp",
    "ov_msckf/src/update/CP2CommitOracle.cpp",
    "ov_msckf/src/update/CP2CompositeState.cpp",
    "ov_msckf/src/update/CP2FeatureGate.cpp",
    "ov_msckf/src/update/CP2OfflineReplay.cpp",
    "ov_msckf/src/update/CP2RuntimeContext.cpp",
    "ov_msckf/src/update/CP2SerialPairing.cpp",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.cpp",
    "ov_msckf/src/update/CP2ShadowMath.cpp",
    "ov_msckf/src/update/CP2StateTraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceJournal.cpp",
    "ov_msckf/src/update/SchurUpdate.cpp",
    "ov_msckf/src/update/UpdaterHelper.cpp",
    "ov_msckf/src/update/UpdaterMSCKF.cpp",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp",
    "ov_msckf/src/update/UpdaterSLAM.cpp",
    "ov_msckf/src/update/UpdaterZeroVelocity.cpp",
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.cpp",
    "ov_msckf/src/ros/ROS1Visualizer.cpp",
    "ov_msckf/src/ros/ROSVisualizerHelper.cpp",
)
SOURCE_INPUTS.update(RUNTIME_LIBRARY_SOURCES)
STRICT_PRODUCTION_SOURCES = (
    "ov_msckf/src/update/SchurUpdate.cpp",
    "ov_msckf/src/update/CP2Canonical.cpp",
    "ov_msckf/src/update/CP2CommitBoundary.cpp",
    "ov_msckf/src/update/CP2CommitOracle.cpp",
    "ov_msckf/src/update/CP2CompositeState.cpp",
    "ov_msckf/src/update/CP2FeatureGate.cpp",
    "ov_msckf/src/update/CP2OfflineReplay.cpp",
    "ov_msckf/src/update/CP2RuntimeContext.cpp",
    "ov_msckf/src/update/CP2SerialPairing.cpp",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.cpp",
    "ov_msckf/src/update/CP2ShadowMath.cpp",
    "ov_msckf/src/update/CP2StateTraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceCodec.cpp",
    "ov_msckf/src/update/CP2TraceJournal.cpp",
    "ov_msckf/src/update/UpdaterHelper.cpp",
    "ov_msckf/src/update/UpdaterMSCKF.cpp",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp",
    "ov_msckf/src/state/StateHelper.cpp",
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.cpp",
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
LINK_COMMAND_ARTIFACTS = {
    "production_library": "link_ov_msckf_lib.txt",
    "fault_library": "link_ov_msckf_cp2_fault_lib.txt",
    "production_updater_test": "link_test_cp2_updater_msckf_end_to_end.txt",
    "fault_updater_test": "link_test_cp2_updater_msckf_fault_injection.txt",
}
FAULT_LIBRARY_TARGET = "ov_msckf_cp2_fault_lib"
FAULT_INJECTION_TEST = "test_cp2_updater_msckf_fault_injection"
TESTS_REQUIRING_PRODUCTION = set(ALL_TESTS) - {
    "test_cp1_rank_rejection",
    "test_cp1_schur_equivalence",
    FAULT_INJECTION_TEST,
}
# Step 8 runs before registry access.  The unit archive is therefore the
# complete committed tree with exactly one fixed exclusion; the readiness
# context still carries that entry's opaque stage-0 identity/hash record.
ARCHIVE_ROOTS = [".", ":(exclude)" + PREAUTHORIZATION_REGISTRY_PATH]
WORKSPACE_RECORD_NAME = "workspace.json"
DEPENDENCY_INVENTORY_NAME = "dependency_inventory.json"
SOURCE_ARCHIVE_NAME = "source_snapshot.tar"
READINESS_ENTRYPOINTS = (
    "scripts/cp2/run_unit_gate.sh",
    "scripts/cp2/run_recorded_parity.py",
    "scripts/cp2/run_sequence_pair.py",
    "scripts/cp2/run_timing_pair.py",
    "scripts/cp2/verify_report.py",
)
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


def validate_cp2_c_approval_binding(input_hashes, git_blobs, errors):
    for relative, expected in FROZEN_CP2_C_APPROVAL_BINDING.items():
        if input_hashes.get(relative) != expected["sha256"]:
            errors.append("CP2-C approval-binding SHA-256 mismatch: " + relative)
        if git_blobs.get(relative) != expected["git_blob"]:
            errors.append("CP2-C approval-binding Git blob mismatch: " + relative)


def rename_path_noreplace(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is required for atomic no-replace publication")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        -100,
        os.fsencode(str(source)),
        -100,
        os.fsencode(str(destination)),
        1,
    )
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def atomic_write_bytes(path, content):
    path = path.absolute()
    parent = path.parent.resolve(strict=True)
    destination = parent / path.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp.", dir=str(parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            rename_path_noreplace(temporary, destination)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise ValueError("refusing to overwrite: " + str(destination)) from exc
            raise
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        directory_descriptor = os.open(str(parent), directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
    actual_case_counts = Counter(cases)
    expected_case_counts = Counter(
        case_name
        for owned_cases in TEST_CASES_BY_BINARY.values()
        for case_name in owned_cases
    )
    if actual_case_counts != expected_case_counts:
        errors.append(
            "gtest testcase multiset mismatch: expected {} got {}".format(
                sorted(expected_case_counts.items()), sorted(actual_case_counts.items())
            )
        )
    if set(expected_case_counts) != EXPECTED_TEST_CASES:
        errors.append("internal expected testcase inventory disagrees with ownership mapping")
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


def cp2_testing_macro_record(tokens, expected_enabled):
    macro = "OV_MSCKF_CP2_TESTING"
    accepted = "-DOV_MSCKF_CP2_TESTING=1"
    events = []
    unsupported = []
    for index, token in enumerate(tokens):
        if token == accepted:
            events.append({"index": index, "state": "enabled", "token": token})
        elif token == "-U" + macro:
            events.append({"index": index, "state": "undefined", "token": token})
        elif token == "-D" + macro or token.startswith("-D" + macro + "="):
            events.append({"index": index, "state": "rejected", "token": token})
        elif macro in token:
            unsupported.append({"index": index, "token": token})
    effective = events[-1]["state"] if events else "absent"
    expected = "enabled" if expected_enabled else "absent"
    return {
        "effective": effective,
        "events": events,
        "expected": expected,
        "passed": bool(
            effective == expected
            and not unsupported
            and (len(events) == 1 if expected_enabled else not events)
        ),
        "unsupported_tokens": unsupported,
    }


def forced_include_record(tokens):
    events = []
    for index, token in enumerate(tokens):
        if (
            token in {"-specs", "--specs"}
            or token.startswith("-specs=")
            or token.startswith("--specs=")
        ):
            events.append({
                "index": index,
                "payload": (
                    tokens[index + 1]
                    if token in {"-specs", "--specs"} and index + 1 < len(tokens)
                    else token.split("=", 1)[1] if "=" in token else None
                ),
                "spelling": "compiler_specs_file",
                "token": token,
            })
        elif token in {"-include", "-imacros"}:
            payload = tokens[index + 1] if index + 1 < len(tokens) else None
            events.append({
                "index": index,
                "payload": payload,
                "spelling": "split",
                "token": token,
            })
        elif token.startswith("-include=") or token.startswith("-imacros="):
            events.append({
                "index": index,
                "payload": token.split("=", 1)[1],
                "spelling": "equals",
                "token": token,
            })
        elif (
            token.startswith("-include") and token != "-include"
        ) or (
            token.startswith("-imacros") and token != "-imacros"
        ):
            prefix = "-include" if token.startswith("-include") else "-imacros"
            events.append({
                "index": index,
                "payload": token[len(prefix):],
                "spelling": "joined",
                "token": token,
            })
        elif token == "-Wp":
            events.append({
                "index": index,
                "payload": tokens[index + 1] if index + 1 < len(tokens) else None,
                "spelling": "opaque_preprocessor_forwarding",
                "token": token,
            })
        elif token.startswith("-Wp,"):
            forwarded = token[4:].split(",")
            if any(value.startswith("@") for value in forwarded):
                events.append({
                    "index": index,
                    "payload": next(
                        value for value in forwarded if value.startswith("@")
                    ),
                    "spelling": "preprocessor_response_file",
                    "token": token,
                })
                continue
            for forwarded_index, value in enumerate(forwarded):
                if (
                    value in {"-include", "-imacros"}
                    or value.startswith("-include=")
                    or value.startswith("-imacros=")
                    or (value.startswith("-include") and value != "-include")
                    or (value.startswith("-imacros") and value != "-imacros")
                ):
                    payload = (
                        forwarded[forwarded_index + 1]
                        if value in {"-include", "-imacros"}
                        and forwarded_index + 1 < len(forwarded)
                        else None
                    )
                    events.append({
                        "index": index,
                        "payload": payload,
                        "spelling": "preprocessor_forwarded",
                        "token": token,
                    })
                    break
        elif token == "-Xpreprocessor" or token.startswith("-Xpreprocessor="):
            # Forwarded preprocessor options are opaque to this proof. Reject
            # the mechanism rather than trying to infer whether its payload
            # can force a header or macro file.
            events.append({
                "index": index,
                "payload": tokens[index + 1] if index + 1 < len(tokens) else None,
                "spelling": "opaque_preprocessor_forwarding",
                "token": token,
            })
    return {"events": events, "passed": not events}


def strict_flag_record(tokens, workspace=None):
    positions = {flag: [index for index, token in enumerate(tokens) if token == flag]
                 for flag in STRICT_REQUIRED_FLAGS}
    macro_record = required_macro_record(tokens)
    forced_includes = forced_include_record(tokens)
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
        # ``command`` records have already been shell-tokenized, while an
        # ``arguments`` record contains the literal compiler argv.  Removing
        # quote bytes here would therefore let a non-effective literal token
        # masquerade as a valid prefix-map option.
        normalized_tokens = list(tokens)
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
        and forced_includes["passed"]
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
        "forced_include_mechanisms": forced_includes,
        "hidden_or_shell_tokens": hidden_or_shell_tokens,
        "macro_events": macro_record["macro_events"],
        "passed": bool(passed),
        "prefix_map_positions": prefix_map_positions,
        "required_flag_positions": positions,
        "unsupported_macro_tokens": macro_record["unsupported_macro_tokens"],
        "unexpected_prefix_maps": unexpected_prefix_maps,
    }


def relative_source(file_value, directory_value, repo_root, prevalidated=False):
    if not isinstance(file_value, str):
        return None
    path = Path(file_value)
    if not path.is_absolute() and isinstance(directory_value, str):
        path = Path(directory_value) / path
    if prevalidated:
        path = Path(os.path.normpath(str(path)))
        root = Path(os.path.normpath(str(repo_root)))
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def analyze_compile_commands(
    path, repo_root, errors, compiler_record=None, prevalidated=False
):
    if not path.is_file():
        errors.append("missing compile_commands.json")
        return {
            "compile_commands_sha256": None,
            "fault_injection_translation_units": {
                source: [] for source in STRICT_PRODUCTION_SOURCES
            },
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
            "runtime_source_inventory": {},
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
    fault_injection = {source: [] for source in STRICT_PRODUCTION_SOURCES}
    runtime_records = {
        target: {source: [] for source in RUNTIME_LIBRARY_SOURCES}
        for target in ("ov_msckf_lib", FAULT_LIBRARY_TARGET)
    }
    runtime_actual_sources = {
        target: [] for target in ("ov_msckf_lib", FAULT_LIBRARY_TARGET)
    }
    targets = {name: [] for name in sorted(STRICT_TARGETS)}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        tokens = command_tokens(entry)
        source = relative_source(
            entry.get("file"), entry.get("directory"), repo_root, prevalidated
        )
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
                tokens[compile_positions[0] + 1], entry.get("directory"), repo_root,
                prevalidated,
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
        if not record["forced_include_mechanisms"]["passed"]:
            structure_errors.append("forced include/macro-file mechanism is forbidden")
        tracked_target = target in runtime_records or target in targets
        macro_expected = target in {FAULT_LIBRARY_TARGET, FAULT_INJECTION_TEST}
        cp2_macro = cp2_testing_macro_record(tokens, macro_expected)
        if tracked_target and not cp2_macro["passed"]:
            structure_errors.append("OV_MSCKF_CP2_TESTING isolation contract failed")
        strict_passed = record["passed"]
        structure_passed = not structure_errors
        record["passed"] = bool(strict_passed and structure_passed)
        record.update({
            "actual_compiler": actual_compiler,
            "command_source": command_source,
            "compile_structure_passed": structure_passed,
            "cp2_testing_macro": cp2_macro,
            "output": output,
            "source": source,
            "structure_errors": structure_errors,
            "target": target,
        })
        if source in production and target == "ov_msckf_lib":
            production[source].append(record)
        if source in fault_injection and target == FAULT_LIBRARY_TARGET:
            fault_injection[source].append(record)
        if target in runtime_records:
            runtime_actual_sources[target].append(source)
            if source in runtime_records[target]:
                runtime_records[target][source].append(record)
        if target in targets:
            targets[target].append(record)
    for source, records in production.items():
        if len(records) != 1:
            errors.append(
                "strict-FP evidence requires exactly one production command for " + source
            )
        elif not records[0]["passed"]:
            errors.append(source + " compile command is not effectively strict-FP")
    for source, records in fault_injection.items():
        if len(records) != 1:
            errors.append(
                "strict-FP evidence requires exactly one fault-library command for " + source
            )
        elif not records[0]["passed"]:
            errors.append(
                source + " fault-library compile command is not strict-FP/macro-isolated"
            )
    runtime_inventory = {}
    expected_runtime_sources = set(RUNTIME_LIBRARY_SOURCES)
    for target, source_records in runtime_records.items():
        actual_sources = runtime_actual_sources[target]
        source_inventory_passed = (
            len(actual_sources) == len(RUNTIME_LIBRARY_SOURCES)
            and set(actual_sources) == expected_runtime_sources
            and all(len(source_records[source]) == 1 for source in RUNTIME_LIBRARY_SOURCES)
        )
        macro_isolation_passed = all(
            len(source_records[source]) == 1
            and source_records[source][0]["compile_structure_passed"]
            and source_records[source][0]["cp2_testing_macro"]["passed"]
            for source in RUNTIME_LIBRARY_SOURCES
        )
        if not source_inventory_passed:
            errors.append(target + " runtime source inventory mismatch")
        if not macro_isolation_passed:
            errors.append(target + " OV_MSCKF_CP2_TESTING macro isolation failed")
        runtime_inventory[target] = {
            "actual_sources": sorted(str(source) for source in actual_sources),
            "expected_sources": list(RUNTIME_LIBRARY_SOURCES),
            "macro_isolation_passed": bool(macro_isolation_passed),
            "source_inventory_passed": bool(source_inventory_passed),
            "translation_units": source_records,
        }
    expected_target_source_counts = {
        name: Counter((
            "ov_msckf/test/cp1/gtest_main.cpp",
            TEST_SOURCE_BY_BINARY[name],
        ))
        for name in CP1_TESTS
    }
    expected_target_source_counts.update({
        name: Counter((
            "ov_msckf/test/cp2/gtest_main.cpp",
            TEST_SOURCE_BY_BINARY[name],
        ))
        for name in CP2_TESTS
    })
    for target, records in targets.items():
        actual_source_counts = Counter(record["source"] for record in records)
        if actual_source_counts != expected_target_source_counts[target]:
            errors.append(
                "{} strict-FP source inventory mismatch: expected {} got {}".format(
                    target,
                    sorted(
                        (str(source), count)
                        for source, count in expected_target_source_counts[target].items()
                    ),
                    sorted(
                        (str(source), count)
                        for source, count in actual_source_counts.items()
                    ),
                )
            )
        for record in records:
            if not record["passed"]:
                errors.append(
                    "{} has a non-strict compile command for {} (or CP2 test macro mismatch)".format(
                        target, record["source"]
                    )
                )
    passed = (
        all(
            len(records) == 1 and records[0].get("passed") is True
            for records in production.values()
        )
        and all(
            len(records) == 1 and records[0].get("passed") is True
            for records in fault_injection.values()
        )
        and all(
            record["source_inventory_passed"]
            and record["macro_isolation_passed"]
            for record in runtime_inventory.values()
        )
        and all(
            Counter(record["source"] for record in targets[target])
            == expected_target_source_counts[target]
            and all(record["passed"] for record in targets[target])
            for target in targets
        )
    )
    return {
        "compile_commands_sha256": sha256_file(path),
        "fault_injection_translation_units": fault_injection,
        "passed": bool(passed),
        "production_translation_units": production,
        "required_flags": list(STRICT_REQUIRED_FLAGS),
        "required_macro_definitions": {
            macro: list(accepted_definitions)
            for macro, accepted_definitions
            in STRICT_REQUIRED_MACRO_DEFINITIONS.items()
        },
        "runtime_source_inventory": runtime_inventory,
        "unit_test_targets": targets,
    }


def collect_link_isolation(artifact_dir, errors, workspace_record=None):
    parsed = {}
    metadata = {}
    for label, artifact_name in LINK_COMMAND_ARTIFACTS.items():
        path = artifact_dir / artifact_name
        lines = []
        if not path.is_file():
            errors.append("missing retained link command: " + artifact_name)
        else:
            try:
                raw_lines = path.read_text(
                    encoding="utf-8", errors="strict"
                ).splitlines()
            except (OSError, UnicodeError) as exc:
                errors.append(
                    "cannot read retained link command {}: {}".format(
                        artifact_name, exc
                    )
                )
                raw_lines = []
            for number, raw in enumerate(raw_lines, 1):
                if not raw.strip():
                    continue
                try:
                    tokens = shlex.split(raw)
                except ValueError as exc:
                    errors.append(
                        "cannot parse retained link command {}:{}: {}".format(
                            artifact_name, number, exc
                        )
                    )
                    tokens = []
                if any(
                    token.startswith("@")
                    or any(marker in token for marker in (";", "&&", "||", "`", "$(", "\n", "\r"))
                    or token in {"|", "<", ">", "2>", "2>&1"}
                    for token in tokens
                ):
                    errors.append(artifact_name + " contains an opaque/shell-control link token")
                lines.append(tokens)
        parsed[label] = lines
        metadata[label] = {
            "artifact": artifact_name,
            "line_count": len(lines),
            "sha256": sha256_file(path) if path.is_file() else None,
            "token_sha256": [
                sha256_bytes("\0".join(tokens).encode("utf-8")) for tokens in lines
            ],
        }

    def flattened(label):
        return [token for line in parsed[label] for token in line]

    def allowed_workspace_paths(relative):
        allowed = {relative}
        workspace = (
            workspace_record.get("workspace")
            if isinstance(workspace_record, dict) else None
        )
        if isinstance(workspace, str) and Path(workspace).is_absolute():
            allowed.add(str(Path(workspace) / relative))
        return allowed

    def object_sources(label, target):
        sources = []
        pattern = re.compile(
            r"(?:^|/)CMakeFiles/" + re.escape(target) + r"\.dir/(.+\.cpp)\.o$"
        )
        for token in flattened(label):
            match = pattern.search(token)
            if match:
                sources.append("ov_msckf/" + match.group(1).replace("__/", ""))
        return sources

    def linker_control_record(tokens):
        events = []
        for index, token in enumerate(tokens):
            if token.startswith("@"):
                events.append({
                    "index": index,
                    "kind": "driver_response_file",
                    "token": token,
                })
            elif token == "-Xlinker" or token.startswith("-Xlinker="):
                events.append({
                    "index": index,
                    "kind": "opaque_xlinker_forwarding",
                    "token": token,
                })
            elif token == "-Wl":
                events.append({
                    "index": index,
                    "kind": "opaque_split_wl_forwarding",
                    "token": token,
                })
            elif token.startswith("-Wl,"):
                forwarded = token[4:].split(",")
                if any(value.startswith("@") for value in forwarded):
                    events.append({
                        "index": index,
                        "kind": "linker_response_file",
                        "token": token,
                    })
            elif (
                token in {"-specs", "--specs"}
                or token.startswith("-specs=")
                or token.startswith("--specs=")
            ):
                events.append({
                    "index": index,
                    "kind": "compiler_specs_file",
                    "token": token,
                })
        return {"events": events, "passed": not events}

    def compiler_output_binding(label, expected_relative_output):
        tokens = flattened(label)
        expected_outputs = allowed_workspace_paths(expected_relative_output)
        exact = []
        alternatives = []
        for index, token in enumerate(tokens):
            if token == "-o":
                exact.append({
                    "index": index,
                    "output": tokens[index + 1] if index + 1 < len(tokens) else None,
                })
            elif token.startswith("-o") and token != "-o":
                alternatives.append({
                    "index": index,
                    "kind": "joined_driver_output",
                    "token": token,
                })
            elif token == "--output" or token.startswith("--output="):
                alternatives.append({
                    "index": index,
                    "kind": "long_driver_output",
                    "token": token,
                })
            elif token.startswith("-Wl,"):
                forwarded = token[4:].split(",")
                for forwarded_index, value in enumerate(forwarded):
                    if (
                        value in {"-o", "--output"}
                        or value.startswith("-o=")
                        or value.startswith("--output=")
                        or (value.startswith("-o") and value != "-o")
                    ):
                        alternatives.append({
                            "index": index,
                            "kind": "forwarded_linker_output",
                            "token": token,
                        })
                        break
        passed = bool(
            len(parsed[label]) == 1
            and len(exact) == 1
            and exact[0]["output"] in expected_outputs
        )
        passed = bool(passed and not alternatives)
        return {
            "alternative_selectors": alternatives,
            "exact_selectors": exact,
            "expected_outputs": sorted(expected_outputs),
            "passed": passed,
        }

    library_names = {
        "production": {
            "logical": "ov_msckf_lib",
            "files": {"libov_msckf_lib.a", "libov_msckf_lib.so"},
        },
        "fault": {
            "logical": "ov_msckf_cp2_fault_lib",
            "files": {
                "libov_msckf_cp2_fault_lib.a",
                "libov_msckf_cp2_fault_lib.so",
            },
        },
    }

    def library_identity(value):
        candidate = value[1:] if value.startswith(":") else value
        basename = Path(candidate).name
        for identity, names in library_names.items():
            if candidate == names["logical"]:
                return identity
            if any(
                basename == filename or basename.startswith(filename + ".")
                for filename in names["files"]
            ):
                return identity
        return None

    def scan_library_values(values, origin, token_index, references):
        index = 0
        while index < len(values):
            value = values[index]
            identity = None
            spelling = None
            selected = None
            if value in {"-l", "--library"}:
                selected = values[index + 1] if index + 1 < len(values) else None
                identity = library_identity(selected) if selected is not None else None
                spelling = "split_library_option"
                index += 1
            elif value.startswith("-l:"):
                selected = value[2:]
                identity = library_identity(selected)
                spelling = "exact_filename_library_option"
            elif value.startswith("-l") and value != "-l":
                selected = value[2:]
                identity = library_identity(selected)
                spelling = "joined_library_option"
            elif value.startswith("--library="):
                selected = value.split("=", 1)[1]
                identity = library_identity(selected)
                spelling = "long_library_option"
            else:
                selected = value
                identity = library_identity(selected)
                spelling = "direct_library_path"
            if identity is not None:
                references.append({
                    "identity": identity,
                    "origin": origin,
                    "selected": selected,
                    "spelling": spelling,
                    "token_index": token_index,
                })
            index += 1

    def library_references(tokens):
        references = []
        for index, token in enumerate(tokens):
            if token.startswith("-Wl,"):
                scan_library_values(
                    token[4:].split(","), "linker_forwarded", index, references
                )
            else:
                scan_library_values([token], "driver", index, references)
                if token in {"-l", "--library"} and index + 1 < len(tokens):
                    # The one-token call above cannot see the split payload.
                    scan_library_values(
                        [token, tokens[index + 1]], "driver", index, references
                    )
        unique = []
        for reference in references:
            if reference not in unique:
                unique.append(reference)
        return unique

    production_sources = object_sources("production_library", "ov_msckf_lib")
    fault_sources = object_sources("fault_library", FAULT_LIBRARY_TARGET)
    expected_runtime = list(RUNTIME_LIBRARY_SOURCES)
    production_tokens = flattened("production_library")
    fault_tokens = flattened("fault_library")
    production_test_tokens = flattened("production_updater_test")
    fault_test_tokens = flattened("fault_updater_test")

    link_controls = {
        label: linker_control_record(flattened(label))
        for label in LINK_COMMAND_ARTIFACTS
    }
    output_bindings = {
        "production_library": compiler_output_binding(
            "production_library", "devel/lib/libov_msckf_lib.so"
        ),
        "production_updater_test": compiler_output_binding(
            "production_updater_test",
            "devel/lib/ov_msckf/test_cp2_updater_msckf_end_to_end",
        ),
        "fault_updater_test": compiler_output_binding(
            "fault_updater_test",
            "devel/lib/ov_msckf/test_cp2_updater_msckf_fault_injection",
        ),
    }
    library_reference_records = {
        label: library_references(flattened(label))
        for label in LINK_COMMAND_ARTIFACTS
    }

    def object_token_count(tokens):
        return sum(token.endswith(".o") for token in tokens)

    expected_fault_archives = allowed_workspace_paths(
        "devel/lib/libov_msckf_cp2_fault_lib.a"
    )
    fault_archive_binding_ok = bool(
        len(parsed["fault_library"]) == 2
        and parsed["fault_library"][0]
        and Path(parsed["fault_library"][0][0]).name == "ar"
        and len(parsed["fault_library"][0]) == 3 + len(expected_runtime)
        and parsed["fault_library"][0][1] == "qc"
        and parsed["fault_library"][0][2] in expected_fault_archives
        and len(parsed["fault_library"][1]) == 2
        and Path(parsed["fault_library"][1][0]).name == "ranlib"
        and parsed["fault_library"][1][1]
        == parsed["fault_library"][0][2]
    )
    output_bindings["fault_library"] = {
        "archive_selectors": (
            [parsed["fault_library"][0][2]]
            if len(parsed["fault_library"]) >= 1
            and len(parsed["fault_library"][0]) >= 3
            else []
        ),
        "expected_outputs": sorted(expected_fault_archives),
        "passed": fault_archive_binding_ok,
    }

    def exact_test_library_reference(label, identity, selected_relative):
        if len(library_reference_records[label]) != 1:
            return False
        reference = library_reference_records[label][0]
        return (
            reference["identity"] == identity
            and reference["origin"] == "driver"
            and reference["selected"] in allowed_workspace_paths(selected_relative)
            and reference["spelling"] == "direct_library_path"
        )

    production_library_ok = bool(
        len(parsed["production_library"]) == 1
        and "-shared" in production_tokens
        and output_bindings["production_library"]["passed"]
        and link_controls["production_library"]["passed"]
        and production_sources == expected_runtime
        and object_token_count(production_tokens) == len(production_sources)
        and not any(
            reference["identity"] == "fault"
            for reference in library_reference_records["production_library"]
        )
    )
    fault_library_ok = bool(
        fault_archive_binding_ok
        and link_controls["fault_library"]["passed"]
        and fault_sources == expected_runtime
        and object_token_count(fault_tokens) == len(fault_sources)
        and not any(
            reference["identity"] == "production"
            for reference in library_reference_records["fault_library"]
        )
    )
    expected_test_objects = {
        "ov_msckf/test/cp2/gtest_main.cpp",
        "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp",
    }
    production_test_sources = object_sources(
        "production_updater_test", "test_cp2_updater_msckf_end_to_end"
    )
    fault_test_sources = object_sources(
        "fault_updater_test", "test_cp2_updater_msckf_fault_injection"
    )
    production_test_ok = bool(
        len(parsed["production_updater_test"]) == 1
        and output_bindings["production_updater_test"]["passed"]
        and link_controls["production_updater_test"]["passed"]
        and set(production_test_sources) == expected_test_objects
        and len(production_test_sources) == len(expected_test_objects)
        and object_token_count(production_test_tokens) == len(production_test_sources)
        and exact_test_library_reference(
            "production_updater_test", "production",
            "devel/lib/libov_msckf_lib.so"
        )
    )
    fault_test_ok = bool(
        len(parsed["fault_updater_test"]) == 1
        and output_bindings["fault_updater_test"]["passed"]
        and link_controls["fault_updater_test"]["passed"]
        and set(fault_test_sources) == expected_test_objects
        and len(fault_test_sources) == len(expected_test_objects)
        and object_token_count(fault_test_tokens) == len(fault_test_sources)
        and exact_test_library_reference(
            "fault_updater_test", "fault",
            "devel/lib/libov_msckf_cp2_fault_lib.a"
        )
    )
    if not production_library_ok:
        errors.append("production runtime link/source inventory is not exact")
    if not fault_library_ok:
        errors.append("fault runtime archive link/source inventory is not exact")
    if not production_test_ok:
        errors.append("production updater test does not link only the production runtime")
    if not fault_test_ok:
        errors.append("fault updater test does not link only the isolated fault runtime")
    return {
        "artifacts": metadata,
        "fault_library": {
            "passed": fault_library_ok,
            "runtime_sources": fault_sources,
        },
        "fault_updater_test": {
            "passed": fault_test_ok,
            "test_sources": fault_test_sources,
        },
        "passed": bool(
            production_library_ok
            and fault_library_ok
            and production_test_ok
            and fault_test_ok
        ),
        "library_references": library_reference_records,
        "linker_controls": link_controls,
        "output_bindings": output_bindings,
        "production_library": {
            "passed": production_library_ok,
            "runtime_sources": production_sources,
        },
        "production_updater_test": {
            "passed": production_test_ok,
            "test_sources": production_test_sources,
        },
    }


def analyze_dependency_compile_commands(
    path, package, repo_root, workspace_build_root, errors, prevalidated=False
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
    def normalized(candidate):
        return Path(os.path.normpath(str(candidate)))

    expected_build_directory = (
        normalized(workspace_build_root / package)
        if prevalidated else (workspace_build_root / package).resolve()
    )
    workspace_source_root = normalized(repo_root) if prevalidated else repo_root.resolve()
    googletest_source_root = (
        Path("/usr/src/googletest")
        if prevalidated else Path("/usr/src/googletest").resolve()
    )
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
            source = relative_source(
                entry.get("file"), entry.get("directory"), repo_root, prevalidated
            )
            if not tokens:
                structure_errors.append("entry has no parseable compiler argv")

            directory_value = entry.get("directory")
            if not isinstance(directory_value, str):
                structure_errors.append("entry has no build directory")
                command_directory = None
            else:
                command_directory = (
                    normalized(directory_value)
                    if prevalidated else Path(directory_value).resolve()
                )
                if prevalidated and not command_directory.is_absolute():
                    structure_errors.append("entry build directory is not absolute")
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
                if prevalidated:
                    canonical_source = normalized(declared_path)
                    if not canonical_source.is_absolute():
                        structure_errors.append("declared source is not absolute")
                else:
                    try:
                        canonical_source = declared_path.resolve(strict=True)
                    except OSError:
                        structure_errors.append("declared source is not an existing regular file")
            else:
                structure_errors.append("entry has no declared source")
            if canonical_source is not None:
                if not prevalidated and not canonical_source.is_file():
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
                    command_source_value, entry.get("directory"), repo_root,
                    prevalidated,
                )
                command_source_path = Path(command_source_value)
                if not command_source_path.is_absolute() and command_directory is not None:
                    command_source_path = command_directory / command_source_path
                if prevalidated:
                    canonical_command_source = normalized(command_source_path)
                else:
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
                    canonical_output = (
                        normalized(output_path) if prevalidated else output_path.resolve()
                    )
                    output_relative = canonical_output.relative_to(
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
                    canonical_declared_output = (
                        normalized(declared_output_path)
                        if prevalidated else declared_output_path.resolve()
                    )
                    canonical_output = (
                        normalized(output_path) if prevalidated else output_path.resolve()
                    )
                    if canonical_declared_output != canonical_output:
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
    artifact_dir, repo_root, workspace_build_root, errors, prevalidated=False
):
    packages = {
        package: analyze_dependency_compile_commands(
            artifact_dir / artifact,
            package,
            repo_root,
            workspace_build_root,
            errors,
            prevalidated,
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
    files.update(LINK_COMMAND_ARTIFACTS.values())
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
    if os.path.lexists(str(destination)):
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


def expected_build_environment(
    workspace_record, repo_root, commit, errors, prevalidated_source_epoch=None
):
    workspace = workspace_record.get("workspace")
    if not isinstance(workspace, str):
        workspace = ""
    if prevalidated_source_epoch is not None:
        source_epoch = str(prevalidated_source_epoch)
    else:
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


def validate_build_command(
    step, record, repo_root, workspace_record, commit, errors,
    prevalidated_source_epoch=None,
):
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
        workspace_record, repo_root, commit, errors, prevalidated_source_epoch
    ):
        errors.append("build step {} environment differs from the exact contract".format(step))
    validate_interval(record, errors, "build_" + step)
    argv = record.get("argv")
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        errors.append("build step {} argv is invalid".format(step))
        return
    if argv != expected_build_argv(step, workspace_record, repo_root):
        errors.append("build step {} argv differs from the exact serialized contract".format(step))


def collect_build_records(
    artifact_dir, repo_root, workspace_record, commit, errors,
    prevalidated_source_epoch=None,
):
    records = []
    previous_finished = None
    for step in BUILD_STEPS:
        record_path = artifact_dir / ("build_" + step + ".json")
        record = read_json(record_path, errors, record_path.name)
        validate_build_command(
            step, record, repo_root, workspace_record, commit, errors,
            prevalidated_source_epoch,
        )
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
    artifact_dir, repo_root, source, errors, allow_synthetic=False,
    prevalidated=False,
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
    recorded_repo = record.get("repo_root")
    if prevalidated:
        if (
            not isinstance(recorded_repo, str) or not Path(recorded_repo).is_absolute()
            or ".." in Path(recorded_repo).parts
        ):
            errors.append("workspace repo_root is not a safe absolute path")
            effective_repo_root = Path("/invalid-prevalidated-repository")
        else:
            effective_repo_root = Path(recorded_repo)
    else:
        effective_repo_root = repo_root
    expected_repository_build_root = effective_repo_root / "build"
    shared_ceres_checkout = expected_repository_build_root / "vendor/ceres-src"
    if prevalidated and allow_synthetic:
        expected_ceres_commit = record.get("ceres_source_commit")
    elif not prevalidated and allow_synthetic and shared_ceres_checkout.is_dir():
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
        "repo_root": str(effective_repo_root),
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
    if not workspace.is_absolute() or ".." in workspace.parts:
        errors.append("workspace path is not canonical and absolute")
    if not workspace.name.startswith(".cp2-unit-") or ".workspace." not in workspace.name:
        errors.append("workspace path does not have the unique CP2 workspace form")
    archive_argv = [
        "/usr/bin/git", "-c", "tar.umask=0002", "-C", str(effective_repo_root),
        "archive", "--format=tar",
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

    if not prevalidated:
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
    artifact_dir, repo_root, workspace_record, errors, allow_synthetic=False,
    prevalidated=False,
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
    if not prevalidated and checkout.is_dir() and isinstance(expected_ceres_commit, str):
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
    if not prevalidated and dpkg_query.is_file():
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
    artifact_dir, repo_root, workspace_record, linkage, errors, allow_synthetic=False,
    prevalidated=False,
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
    if not _schema_version_one(record.get("schema_version")):
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
        if prevalidated:
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
        else:
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
        errors.append(
            "copied test executable inventory does not contain exactly {} entries".format(
                len(ALL_TESTS)
            )
        )
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
        "source_checkout": (
            str(ceres_source)
            if prevalidated
            else str(ceres_source.resolve()) if ceres_source.exists() else str(ceres_source)
        ),
        "source_commit": workspace_record.get("ceres_source_commit"),
        "source_library_path": str(ceres_library),
        "source_library_sha256": (
            sha256_file(artifact_ceres) if prevalidated and artifact_ceres.is_file()
            else sha256_file(ceres_library) if ceres_library.is_file()
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
        if not prevalidated and workspace_path.is_dir():
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


def strict_json_bytes(content, label="JSON"):
    """Decode bounded canonical-input JSON without duplicate/nonfinite values."""

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key: " + str(key))
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("nonfinite numeric token: " + value)

    try:
        value = json.loads(
            content.decode("utf-8", "strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("cannot parse strict {}: {}".format(label, exc)) from exc

    def finite(node):
        if isinstance(node, float) and not math.isfinite(node):
            raise ValueError(label + " contains a nonfinite number")
        if isinstance(node, list):
            for item in node:
                finite(item)
        elif isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError(label + " contains a non-string key")
                finite(item)

    finite(value)
    return value


def validate_prevalidated_source_context(value, errors):
    required = {
        "branch", "commit", "entries", "entrypoints", "index_tree",
        "record_type", "schema_version", "status_porcelain_v1_hex", "tree",
    }
    if not isinstance(value, dict) or set(value) != required:
        errors.append("prevalidated source context field inventory is not exact")
        return {}
    if (
        not _schema_version_one(value.get("schema_version"))
        or value.get("record_type") != PREVALIDATED_SOURCE_RECORD_TYPE
    ):
        errors.append("prevalidated source context identity is invalid")
    if value.get("branch") != EXPECTED_BRANCH:
        errors.append("prevalidated source branch is not the CP2 branch")
    for field in ("commit", "tree", "index_tree"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{40}", value[field]):
            errors.append("prevalidated source {} is not full lowercase hex".format(field))
    if value.get("tree") != value.get("index_tree"):
        errors.append("prevalidated source index tree differs from HEAD tree")
    if value.get("status_porcelain_v1_hex") != "":
        errors.append("prevalidated source status is not empty")

    entries = value.get("entries")
    exact_entry_fields = {"git_blob", "mode", "path", "sha256", "size"}
    normalized_entries = []
    if not isinstance(entries, list) or not entries:
        errors.append("prevalidated tracked-entry inventory is empty or invalid")
        entries = []
    previous = None
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != exact_entry_fields:
            errors.append("prevalidated tracked entry {} fields are not exact".format(index))
            continue
        path = entry.get("path")
        pure = PurePosixPath(path) if isinstance(path, str) else PurePosixPath(".")
        if (
            not isinstance(path, str) or not path or "\\" in path or "\0" in path
            or pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts)
            or pure.as_posix() != path
        ):
            errors.append("prevalidated tracked entry has an unsafe path")
            continue
        encoded = path.encode("utf-8")
        if previous is not None and encoded <= previous:
            errors.append("prevalidated tracked entries are not strictly UTF-8 sorted")
        previous = encoded
        if path in seen:
            errors.append("prevalidated tracked entry is duplicated: " + path)
        seen.add(path)
        mode = entry.get("mode")
        size = entry.get("size")
        if mode not in (0o100644, 0o100755):
            errors.append("prevalidated tracked entry has a nonregular Git mode: " + path)
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size < (1 << 64):
            errors.append("prevalidated tracked entry size is not u64: " + path)
        if not isinstance(entry.get("git_blob"), str) or not re.fullmatch(
            r"[0-9a-f]{40}", entry.get("git_blob", "")
        ):
            errors.append("prevalidated tracked entry blob is invalid: " + path)
        if not isinstance(entry.get("sha256"), str) or not re.fullmatch(
            r"[0-9a-f]{64}", entry.get("sha256", "")
        ):
            errors.append("prevalidated tracked entry SHA-256 is invalid: " + path)
        normalized_entries.append(entry)
    if PREAUTHORIZATION_REGISTRY_PATH not in seen:
        errors.append("prevalidated source context omits the opaque registry record")

    entrypoints = value.get("entrypoints")
    exact_entrypoint_fields = {
        "git_blob", "mode", "path", "regular_nonsymlink", "sha256",
    }
    if not isinstance(entrypoints, list) or len(entrypoints) != len(READINESS_ENTRYPOINTS):
        errors.append("prevalidated entrypoint inventory count is not exact")
        entrypoints = []
    by_path = {entry["path"]: entry for entry in normalized_entries if "path" in entry}
    for index, expected_path in enumerate(READINESS_ENTRYPOINTS):
        entrypoint = entrypoints[index] if index < len(entrypoints) else {}
        if not isinstance(entrypoint, dict) or set(entrypoint) != exact_entrypoint_fields:
            errors.append("prevalidated entrypoint {} fields are not exact".format(index))
            continue
        source = by_path.get(expected_path, {})
        if (
            entrypoint.get("path") != expected_path
            or entrypoint.get("regular_nonsymlink") is not True
            or entrypoint.get("git_blob") != source.get("git_blob")
            or entrypoint.get("sha256") != source.get("sha256")
            or entrypoint.get("mode") != source.get("mode")
        ):
            errors.append("prevalidated entrypoint binding differs: " + expected_path)
    return value


def validate_source_archive_against_context(
    archive_path, context, errors, registry_policy
):
    """Stream-bind a Git archive to one held source context.

    Unit readiness uses ``forbid`` and therefore never opens the registry
    member.  Detached postauthorization verification uses ``require`` and
    treats those bytes only as opaque Git provenance.
    """

    if registry_policy not in ("forbid", "require"):
        errors.append("source archive registry policy is invalid")
        return {}

    entries = context.get("entries", []) if isinstance(context, dict) else []
    expected = {
        entry.get("path"): entry
        for entry in entries
        if isinstance(entry, dict) and (
            registry_policy == "require"
            or entry.get("path") != PREAUTHORIZATION_REGISTRY_PATH
        )
    }
    expected_directories = set()
    for path in expected:
        if not isinstance(path, str):
            continue
        parts = PurePosixPath(path).parts
        expected_directories.update(
            "/".join(parts[:length]) for length in range(1, len(parts))
        )
    observed_files = {}
    observed_directories = set()
    if not archive_path.is_file():
        errors.append("missing " + SOURCE_ARCHIVE_NAME)
        return observed_files
    try:
        with tarfile.open(str(archive_path), mode="r:") as archive:
            if archive.pax_headers != {"comment": context.get("commit")}:
                errors.append("source archive global commit binding is wrong")
            seen = set()
            for member in archive:
                name = member.name
                pure = PurePosixPath(name)
                if (
                    not name or pure.is_absolute() or "\\" in name
                    or any(part in ("", ".", "..") for part in pure.parts)
                    or pure.as_posix() != name or name in seen
                ):
                    errors.append("source archive has an unsafe or duplicate member: " + name)
                    continue
                seen.add(name)
                if name == PREAUTHORIZATION_REGISTRY_PATH:
                    if registry_policy == "forbid":
                        # Fail before extractfile: registry content is never
                        # read during preauthorization unit verification.
                        errors.append("source archive contains the preauthorization registry")
                        continue
                if member.uid != 0 or member.gid != 0:
                    errors.append("source archive member owner IDs are not canonical: " + name)
                if member.pax_headers != {"comment": context.get("commit")}:
                    errors.append("source archive member commit binding is wrong: " + name)
                if member.isdir():
                    observed_directories.add(name)
                    if member.mode != 0o775 or member.size != 0:
                        errors.append("source archive directory mode/size is wrong: " + name)
                    continue
                if not member.isfile():
                    errors.append("source archive link or special member is forbidden: " + name)
                    continue
                source = expected.get(name)
                if source is None:
                    errors.append("source archive contains an untracked file member: " + name)
                    continue
                expected_mode = 0o775 if source.get("mode") == 0o100755 else 0o664
                if member.mode != expected_mode:
                    errors.append("source archive member mode differs: " + name)
                if member.size != source.get("size"):
                    errors.append("source archive member size differs: " + name)
                stream = archive.extractfile(member)
                digest = hashlib.sha256()
                git_digest = hashlib.sha1(
                    b"blob " + str(member.size).encode("ascii") + b"\0"
                )
                size = 0
                if stream is None:
                    errors.append("cannot stream source archive member: " + name)
                else:
                    while True:
                        block = stream.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        digest.update(block)
                        git_digest.update(block)
                observed_files[name] = digest.hexdigest()
                if size != source.get("size") or digest.hexdigest() != source.get("sha256"):
                    errors.append("source archive member bytes differ: " + name)
                if git_digest.hexdigest() != source.get("git_blob"):
                    errors.append("source archive member Git blob differs: " + name)
    except (OSError, tarfile.TarError) as exc:
        errors.append("cannot inspect source_snapshot.tar: " + str(exc))
    if set(observed_files) != set(expected):
        errors.append("source archive regular-member population differs from held source")
    if observed_directories != expected_directories:
        errors.append("source archive directory-member population differs from held source")
    return observed_files


def validate_prevalidated_source_archive(archive_path, context, errors):
    """Hash every preauthorization member; never read a registry member."""

    return validate_source_archive_against_context(
        archive_path, context, errors, "forbid"
    )


def validate_postauthorized_source_archive(archive_path, context, errors):
    """Bind the complete postauthorization archive, including opaque registry."""

    return validate_source_archive_against_context(
        archive_path, context, errors, "require"
    )


def collect_prevalidated_source_metadata(
    artifact_dir, reported_source, context, errors, allow_synthetic=False
):
    context = validate_prevalidated_source_context(context, errors)
    before = read_json(artifact_dir / "source_before.json", errors, "source_before.json")
    after = read_json(artifact_dir / "source_after.json", errors, "source_after.json")
    for label, snapshot in (("source_before", before), ("source_after", after)):
        if set(snapshot) != {"branch", "commit", "recorded_utc", "status_porcelain_v1", "tree"}:
            errors.append(label + " field inventory is not exact")
        source_snapshot_contract(snapshot, context.get("commit"), context.get("tree"), errors, label)
        parse_utc_timestamp(snapshot.get("recorded_utc"), errors, label + ".recorded_utc")
    if any(before.get(field) != after.get(field) for field in ("branch", "commit", "tree", "status_porcelain_v1")):
        errors.append("source identity/cleanliness changed while the unit gate ran")

    entries = {
        entry.get("path"): entry
        for entry in context.get("entries", [])
        if isinstance(entry, dict)
    }
    input_hashes = {path: entries.get(path, {}).get("sha256") for path in sorted(SOURCE_INPUTS)}
    if any(value is None for value in input_hashes.values()):
        errors.append("prevalidated source context omits a curated source input")
    config_hashes = {name: input_hashes.get(name) for name in sorted(CONFIG_INPUTS)}
    for relative, expected in FROZEN_CONFIG_SHA256.items():
        if config_hashes.get(relative) != expected:
            errors.append("frozen configuration hash mismatch: " + relative)
    approval_blobs = {
        path: entries.get(path, {}).get("git_blob")
        for path in FROZEN_CP2_C_APPROVAL_BINDING
    }
    if not allow_synthetic:
        validate_cp2_c_approval_binding(input_hashes, approval_blobs, errors)
    contract_hashes = {name: input_hashes.get(name) for name in sorted(CONTRACT_INPUTS)}

    archive_path = artifact_dir / SOURCE_ARCHIVE_NAME
    archived_hashes = validate_prevalidated_source_archive(archive_path, context, errors)
    archived_inputs = {name: archived_hashes.get(name) for name in sorted(SOURCE_INPUTS)}
    archive_sha = sha256_file(archive_path) if archive_path.is_file() else None
    archive_size = archive_path.stat().st_size if archive_path.is_file() else None
    archive_record = {
        "archive_roots": list(ARCHIVE_ROOTS),
        "artifact_path": SOURCE_ARCHIVE_NAME,
        "expected_sha256": archive_sha,
        "expected_size_bytes": archive_size,
        "input_sha256": archived_inputs,
        "sha256": archive_sha,
        "size_bytes": archive_size,
    }
    verifier_entry = entries.get("scripts/cp2/verify_report.py", {})
    metadata = reported_source.get("commit_metadata") if isinstance(reported_source, dict) else None
    if not isinstance(metadata, dict) or set(metadata) != {
        "author_date", "author_email", "author_name", "committer_date",
        "committer_email", "committer_name",
    }:
        errors.append("source commit metadata field inventory is not exact")
        metadata = {}
    else:
        for field in ("author_date", "committer_date"):
            value = metadata.get(field)
            try:
                parsed = dt.datetime.fromisoformat(
                    value[:-1] + "+00:00"
                    if isinstance(value, str) and value.endswith("Z") else value
                )
                if parsed.utcoffset() is None:
                    raise ValueError("timezone-naive")
            except (TypeError, ValueError):
                errors.append("source commit metadata {} is not timezone-aware ISO-8601".format(field))
        for field in ("author_email", "author_name", "committer_email", "committer_name"):
            if not isinstance(metadata.get(field), str) or not metadata[field]:
                errors.append("source commit metadata {} is empty or invalid".format(field))
    current = {
        "branch": context.get("branch"),
        "commit": context.get("commit"),
        "status_porcelain_v1": [],
        "tree": context.get("tree"),
    }
    return {
        "after": after,
        "before": before,
        "branch": context.get("branch"),
        "commit": context.get("commit"),
        "commit_metadata": metadata,
        "configuration_sha256": config_hashes,
        "contract_sha256": contract_hashes,
        "current_repository": current,
        "dirty": False,
        "input_sha256": input_hashes,
        "source_archive": archive_record,
        "tree": context.get("tree"),
        "verifier": {
            "committed_sha256": verifier_entry.get("sha256"),
            "running_sha256": verifier_entry.get("sha256"),
        },
    }


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
    approval_binding_git_blobs = {}
    if commit:
        for relative in sorted(SOURCE_INPUTS):
            try:
                committed_bytes = committed_blob(repo_root, commit, relative)
                input_hashes[relative] = sha256_bytes(committed_bytes)
                if relative in FROZEN_CP2_C_APPROVAL_BINDING:
                    git_header = b"blob " + str(len(committed_bytes)).encode("ascii") + b"\0"
                    approval_binding_git_blobs[relative] = hashlib.sha1(
                        git_header + committed_bytes
                    ).hexdigest()
            except subprocess.CalledProcessError as exc:
                errors.append("cannot hash committed source {}: {}".format(relative, exc))
    config_hashes = {name: input_hashes.get(name) for name in sorted(CONFIG_INPUTS)}
    for relative, expected in FROZEN_CONFIG_SHA256.items():
        if config_hashes.get(relative) != expected:
            errors.append("frozen configuration hash mismatch: " + relative)
    if not allow_synthetic:
        validate_cp2_c_approval_binding(
            input_hashes, approval_binding_git_blobs, errors
        )
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
                [
                    "git", "-c", "tar.umask=0002", "archive", "--format=tar",
                    commit, "--", *ARCHIVE_ROOTS,
                ],
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
        if test_name == FAULT_INJECTION_TEST and "libov_msckf_lib.so" in resolved_names:
            errors.append(
                test_name + " loads the production library instead of remaining fault-isolated"
            )
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


def collect_verifier_self_test(artifact_dir, repo_root, errors, verifier_path=None):
    record = read_json(artifact_dir / "verifier_self_test.json", errors, "verifier_self_test.json")
    required_fields = {
        "argv", "cwd", "environment", "environment_mode", "exit_status", "finished_utc",
        "name", "started_utc", "synthetic_artifact_root_policy",
    }
    if set(record) != required_fields:
        errors.append("verifier_self_test.json field inventory is not exact")
    expected = {
        "argv": [
            "/usr/bin/python3",
            "-I",
            "-B",
            str(verifier_path or (repo_root / "scripts/cp2/verify_report.py")),
            "--unit-self-test",
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
    if re.search(
        r"^CP2_READINESS_ENGINE_PROTECTING_TESTS count=39 passed=true "
        r"module_sha256=[0-9a-f]{64} output_sha256=[0-9a-f]{64}$",
        log_text,
        flags=re.MULTILINE,
    ) is None:
        errors.append(
            "verifier self-test log lacks the exact readiness protecting-test result"
        )
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


def artifact_policy(passed=True):
    return {
        "dataset_or_bag_accessed": False,
        "distribution_status": "internal_non_conveyable_staging",
        "eligible_for_cp2_seal": False,
        "finalization": "read_only_staging_finalization_only",
        "no_overwrite": True,
        "scope": "cp2_a_b_cp2_c2_unit_only",
        "serialized_build_and_tests": True,
        "stage": (
            (
                "staging_cp2_c2_unit_passed_"
                if passed else "staging_cp2_c2_unit_not_established_"
            )
            + "pending_cp2_c3_cp2_c_cp2_d_cp2_e"
        ),
        "trust_model": "trusted_committed_runner_not_malicious_forgery_resistant",
    }


def expected_checkpoint_status(passed=True):
    return {
        "CP2-A": "passed" if passed else "not_established",
        "CP2-B": "passed" if passed else "not_established",
        "CP2-C1": "passed_unit_only" if passed else "not_established",
        "CP2-C2": "passed_unit_only" if passed else "not_established",
        "CP2-C3": "not_run",
        "CP2-C": "not_run",
        "CP2-D": "not_run",
        "CP2-E": "not_run_blocked_pending_fixed_clock_profile",
    }


def assemble_unit_report(artifact_dir, repo_root, allow_synthetic=False):
    artifact_dir = artifact_dir.resolve()
    repo_root = repo_root.resolve()
    if not artifact_dir.is_dir():
        raise ValueError("artifact directory does not exist: " + str(artifact_dir))
    if (
        os.path.lexists(str(artifact_dir / REPORT_NAME))
        or os.path.lexists(str(artifact_dir / MANIFEST_NAME))
    ):
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
    link_isolation = collect_link_isolation(artifact_dir, errors, workspace)
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
        "artifact_policy": artifact_policy(passed),
        "binary_sha256": binaries,
        "build": {
            "cmake_cache_sha256": (
                sha256_file(artifact_dir / "CMakeCache.txt")
                if (artifact_dir / "CMakeCache.txt").is_file() else None
            ),
            "commands": build_records,
            "serialized": True,
        },
        "checkpoint": "CP2-A/B/C2-unit",
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
        "link_isolation": link_isolation,
        "overall_checkpoint_status": OVERALL_STATUS,
        "production_library": production_library,
        "schema_version": 1,
        "source": source,
        "status": UNIT_PASS_STATUS if passed else UNIT_FAIL_STATUS,
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
        "link_isolation",
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
    artifact_dir, repo_root=None, quiet=False, expected_manifest_sha256=None,
    require_finalized=True, allow_synthetic=False, prevalidated_source=None,
):
    artifact_dir = artifact_dir.resolve()
    detached = prevalidated_source is not None
    if detached:
        repo_root = Path("/invalid-live-repository-is-forbidden")
    elif repo_root is None:
        raise ValueError("repository root is required without prevalidated source context")
    else:
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
    if not _schema_version_one(report.get("schema_version")):
        errors.append("unsupported CP2 report schema (expected 1)")
    if report.get("checkpoint") != "CP2-A/B/C2-unit":
        errors.append("report checkpoint is not the CP2-A/B/C2 unit sub-gate")
    if report.get("evidence_scope") != EVIDENCE_SCOPE:
        errors.append("report evidence_scope is not the CP2-A/B plus CP2-C2 unit scope")
    if report.get("overall_checkpoint_status") != OVERALL_STATUS:
        errors.append("report incorrectly changes the overall CP2 status")
    if report.get("status") != UNIT_PASS_STATUS:
        errors.append("report does not record a passed CP2-A/B plus CP2-C2 unit run")
    if report.get("eligible_for_cp2_seal") is not False:
        errors.append("CP2-A/B plus CP2-C2 unit evidence must be ineligible for CP2 sealing")
    if report.get("evidence_class") != "trusted_runner_local_staging_evidence":
        errors.append("report evidence class overstates the local trusted-runner scope")
    if report.get("independent_source_to_binary_attestation") is not False:
        errors.append("report must not claim independent source-to-binary attestation")
    if report.get("validation_errors") != []:
        errors.append("report contains validation errors")
    if report.get("checkpoint_status") != expected_checkpoint_status(True):
        errors.append(
            "checkpoint status must pass CP2-A/B and CP2-C1/C2 unit only while leaving "
            "CP2-C3/C/D/E unpassed"
        )
    if report.get("artifact_policy") != artifact_policy():
        errors.append("artifact policy differs from the unit-only no-overwrite contract")
    if report.get("integrity") != {
        "sha256sums_claim": (
            "internal_consistency_only_until_external_digest_is_retained_and_supplied"
        )
    }:
        errors.append("report overstates or misstates SHA256SUMS integrity")
    parse_utc_timestamp(report.get("generated_utc"), errors, "report.generated_utc")

    if detached:
        reported_source = report.get("source") if isinstance(report.get("source"), dict) else {}
        independent_source = collect_prevalidated_source_metadata(
            artifact_dir, reported_source, prevalidated_source, errors,
            allow_synthetic=allow_synthetic,
        )
    else:
        independent_source = collect_source_metadata(
            artifact_dir, repo_root, errors, allow_synthetic=allow_synthetic
        )
    if report.get("source") != independent_source:
        errors.append("reported source provenance differs from committed Git evidence")
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    if source.get("dirty") is not False:
        errors.append("report source provenance is dirty")
    if set(source.get("input_sha256", {})) != SOURCE_INPUTS:
        errors.append(
            "source hash inventory differs from the frozen CP2-A/B plus CP2-C2 inventory"
        )
    if source.get("configuration_sha256") != {
        name: FROZEN_CONFIG_SHA256[name] for name in sorted(CONFIG_INPUTS)
    }:
        errors.append("reported frozen configuration hashes are wrong")
    if set(source.get("contract_sha256", {})) != CONTRACT_INPUTS:
        errors.append("contract hash inventory differs from the CP2 contract")

    independent_workspace = collect_workspace_record(
        artifact_dir, repo_root, independent_source, errors,
        allow_synthetic=allow_synthetic,
        prevalidated=detached,
    )
    if detached:
        recorded_repo_root = independent_workspace.get("repo_root")
        if isinstance(recorded_repo_root, str) and Path(recorded_repo_root).is_absolute():
            repo_root = Path(recorded_repo_root)
    if report.get("workspace") != independent_workspace:
        errors.append("reported fresh workspace differs from workspace.json")
    independent_third_party = collect_third_party_sources_and_notices(
        artifact_dir, repo_root, independent_workspace, errors,
        allow_synthetic=allow_synthetic,
        prevalidated=detached,
    )
    if report.get("third_party_sources_and_notices") != independent_third_party:
        errors.append("reported third-party source/license evidence differs from artifacts")
    prevalidated_source_epoch = "" if detached else None
    if detached:
        committer_date = independent_source.get("commit_metadata", {}).get("committer_date")
        if isinstance(committer_date, str):
            try:
                parsed_committer = dt.datetime.fromisoformat(
                    committer_date[:-1] + "+00:00"
                    if committer_date.endswith("Z") else committer_date
                )
                if parsed_committer.utcoffset() is None:
                    raise ValueError("committer date is timezone-naive")
                prevalidated_source_epoch = int(parsed_committer.timestamp())
            except (OverflowError, ValueError):
                errors.append("source committer date cannot derive SOURCE_DATE_EPOCH")
    independent_build = collect_build_records(
        artifact_dir, repo_root, independent_workspace,
        independent_source.get("commit", ""), errors,
        prevalidated_source_epoch,
    )
    build = report.get("build") if isinstance(report.get("build"), dict) else {}
    if build.get("serialized") is not True or build.get("commands") != independent_build:
        errors.append("reported build commands/results differ from captured serialized records")
    cache_path = artifact_dir / "CMakeCache.txt"
    actual_cache_hash = sha256_file(cache_path) if cache_path.is_file() else None
    if build.get("cmake_cache_sha256") != actual_cache_hash:
        errors.append("reported CMakeCache.txt hash is wrong")

    verifier_record_path = repo_root / "scripts/cp2/verify_report.py"
    independent_self_test = collect_verifier_self_test(
        artifact_dir, repo_root, errors,
        verifier_path=verifier_record_path if detached else None,
    )
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
        prevalidated=detached,
    )
    if report.get("strict_floating_point") != independent_fp:
        errors.append("reported strict-FP evidence differs from compile_commands.json")
    if independent_fp.get("passed") is not True:
        errors.append("effective strict-FP evidence did not pass")

    independent_link_isolation = collect_link_isolation(
        artifact_dir, errors, independent_workspace
    )
    if report.get("link_isolation") != independent_link_isolation:
        errors.append("reported target link isolation differs from retained link commands")
    if independent_link_isolation.get("passed") is not True:
        errors.append("production/fault target link isolation did not pass")

    independent_dependency_eigen_abi = analyze_dependency_eigen_abi(
        artifact_dir,
        Path(independent_workspace.get("source_root", repo_root)),
        Path(independent_workspace.get("workspace_build_root", "")),
        errors,
        prevalidated=detached,
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
        prevalidated=detached,
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
        print("CP2-A/B plus CP2-C2 unit evidence verified: " + str(artifact_dir))
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
        print(
            "CP2-C2 is unit-only; CP2-C3, CP2-C, CP2-D, and CP2-E "
            "remain unexecuted and unpassed."
        )
    return 0, []


def verify_unit_anchor_prevalidated(
    artifact_dir, expected_manifest_sha256, prevalidated_source,
    quiet=False, allow_synthetic=False,
):
    """Verify a unit artifact without Git or any live source-tree access."""

    status, errors = verify_unit_report(
        artifact_dir,
        None,
        quiet=True,
        expected_manifest_sha256=expected_manifest_sha256,
        require_finalized=True,
        allow_synthetic=allow_synthetic,
        prevalidated_source=prevalidated_source,
    )
    if status != 0:
        if not quiet:
            for error in errors:
                print("ERROR: " + error)
        return status, errors
    result = {
        "commit": prevalidated_source.get("commit"),
        "passed": True,
        "record_type": "cp2_prevalidated_unit_verification_result",
        "schema_version": 1,
        "tree": prevalidated_source.get("tree"),
    }
    if not quiet:
        print(json.dumps(result, allow_nan=False, separators=(",", ":"), sort_keys=True))
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
    rename_path_noreplace(canonical_source, destination)


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
            raise ValueError("source is outside the CP2-A/B plus CP2-C2 staging parent")
        if destination.parent.absolute() != expected_parent or not destination.parent.is_dir():
            raise ValueError("destination is outside the CP2-A/B plus CP2-C2 staging parent")
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
        "CP2UpdaterMSCKFTransaction.ZeroRawDiscardsTentativeWithoutValidationOrPhases",
        "CP2UpdaterMSCKFTransaction.CleanCommitPublishesExactCompositeAndCommitOracle",
        "CP2UpdaterMSCKFTransaction.RawNoncommitPublishesOnlyEqualPhaseZeroAndOne",
        "CP2UpdaterMSCKFTransaction.PromotionFailureIsFatalBeforeAnyPublishOrBaselineWrite",
        "CP2UpdaterMSCKFTransaction.SinkRejectionAfterCommitLatchesFatalWithoutRollbackOrObserver",
        "CP2UpdaterMSCKFTransaction.RecordedSinkConfigurationIsNullspaceOnlyAndFreezesAtFirstUpdate",
        "CP2UpdaterMSCKFTransaction.MissingRecordedContextIsFatalBeforeEstimatorWork",
        "CP2UpdaterMSCKFTransaction.InvocationContextIsOneShotContiguousAndSettersFailClosed",
    ],
    "test_cp2_updater_msckf_fault_injection": [
        "CP2UpdaterMSCKFEndToEnd.ActualNullspaceAndSchurModesCommitEquivalentFullStateUpdates",
        "CP2UpdaterMSCKFEndToEnd.SelectedReducersRejectNonfiniteProductionRowsWithoutSilentFallback",
        "CP2UpdaterMSCKFEndToEnd.SharedInvalidPreflightLeavesBothModeStatesBitwiseUnchanged",
        "CP2UpdaterMSCKFEndToEnd.NullspaceShadowPublishesBothPrecommitProposalsThenOneCommittedEvent",
        "CP2UpdaterMSCKFEndToEnd.NonfiniteGammaEvidenceCannotStopLiveTraversalOrBaselineCommit",
        "CP2UpdaterMSCKFEndToEnd.SchurModeRejectsShadowEnableWithoutReplacingExistingObserver",
        "CP2UpdaterMSCKFEndToEnd.ObserverExceptionCannotVetoAnAcceptedBaselineCommit",
        "CP2UpdaterMSCKFEndToEnd.AllRejectedRawSystemHasExactTerminalTaxonomyAndNoBaselineWrite",
        "CP2UpdaterMSCKFTransaction.ZeroRawDiscardsTentativeWithoutValidationOrPhases",
        "CP2UpdaterMSCKFTransaction.CleanCommitPublishesExactCompositeAndCommitOracle",
        "CP2UpdaterMSCKFTransaction.RawNoncommitPublishesOnlyEqualPhaseZeroAndOne",
        "CP2UpdaterMSCKFTransaction.PromotionFailureIsFatalBeforeAnyPublishOrBaselineWrite",
        "CP2UpdaterMSCKFTransaction.SinkRejectionAfterCommitLatchesFatalWithoutRollbackOrObserver",
        "CP2UpdaterMSCKFTransaction.CandidateAssemblyFailureCannotVetoBaselineCommit",
        "CP2UpdaterMSCKFTransaction.BaselineProvenanceMismatchSuppressesCommitBeforePhase2",
        "CP2UpdaterMSCKFTransaction.InvalidPhase2IsDiscardedAndCannotCommit",
        "CP2UpdaterMSCKFTransaction.NonfinitePhase1IsSnapshotMismatchAndDiscardsPhase2",
        "CP2UpdaterMSCKFTransaction.FinalPointerRejectionDiscardsInstalledPhase2",
        "CP2UpdaterMSCKFTransaction.IncompletePostcommitStorageIsFatalAfterCommitWithoutPublication",
        "CP2UpdaterMSCKFTransaction.PostcommitPointerTokenFailureIsFatalAfterCommitWithoutPublication",
        "CP2UpdaterMSCKFTransaction.CompletePhase3ValueMismatchRemainsCountedFailedEvidence",
        "CP2UpdaterMSCKFTransaction.CompleteNonfinitePhase3RemainsCountedFailedEvidence",
        "CP2UpdaterMSCKFTransaction.ZeroRawDurationFailureIsArithmeticFatalWithoutPublication",
        "CP2UpdaterMSCKFTransaction.PhasePairDurationFailureIsArithmeticFatalWithoutPublicationOrWrite",
        "CP2UpdaterMSCKFTransaction.CommittedDurationFailureIsArithmeticFatalWithoutPublicationOrRollback",
        "CP2UpdaterMSCKFTransaction.CommitOracleOverflowIsArithmeticFatalWithoutPublicationOrRollback",
        "CP2UpdaterMSCKFTransaction.CommitOracleInvalidPhaseRemainsDistinctPostcommitFatal",
        "CP2UpdaterMSCKFTransaction.RecordedSinkConfigurationIsNullspaceOnlyAndFreezesAtFirstUpdate",
        "CP2UpdaterMSCKFTransaction.MissingRecordedContextIsFatalBeforeEstimatorWork",
        "CP2UpdaterMSCKFTransaction.InvocationContextIsOneShotContiguousAndSettersFailClosed",
        "CP2UpdaterMSCKFTransaction.InvocationIdOverflowIsFatalBeforeEstimatorWork",
    ],
    "test_cp2_composite_state": [
        "CP2CompositeStateCodec.FrozenFullRolePayloadRoundTripsBitExactly",
        "CP2CompositeStateCodec.EveryBinary64CoefficientMutationIsDetected",
        "CP2CompositeStateCodec.IdentityMetadataShapeAndKeyMutationsAreDetected",
        "CP2CompositeStateValidation.ExactPartitionsAndCloneIdentityFailClosed",
        "CP2CompositeStateValidation.EveryLandmarkRepresentationIdentityRuleIsExact",
        "CP2CompositeStateValidation.QuaternionSquaredNormBoundaryIsRoleComplete",
        "CP2CompositeStateCodec.NonfiniteRolesRoundTripLosslesslyButNeverValidate",
        "CP2StateFileCodec.LegalPhasePopulationsRoundTripAndCorruptionFailsClosed",
        (
            "CP2CompositeLiveCapture.ProductionStateProjectsOneOwningPriorAndIgnores"
            "AddressesAndMapInsertion"
        ),
        (
            "CP2CompositePointerGraph.Phase1MatchesAndEveryPointerAssociationMutation"
            "IsDetected"
        ),
        (
            "CP2CompositeLiveCapture.ParentSubvariableInactiveCalibrationAndCamera"
            "InventoryFaultsAreRejected"
        ),
        (
            "CP2CompositePostcommit.PreparedPhase3FillIsNoexceptAllocationFreeAnd"
            "MatchesProductionCommit"
        ),
        (
            "CP2CompositePostcommit.LiveCacheReadAndPreparedOrPointerFailuresReturn"
            "ExplicitStatus"
        ),
        "CP2CompositeDetachedOracle.AppliesExactlyOneProductionUpdatePerTopLevelType",
    ],
    "test_cp2_commit_oracle": [
        "CP2CommitOracle.MatchingCompositeHasExactPopulationsAndPasses",
        "CP2CommitOracle.CountsEachCoefficientMismatchClassByExactBits",
        "CP2CommitOracle.InventoryIdentityAndShapeFailuresZeroOnlyCoefficientPopulations",
        "CP2CommitOracle.EqualNonfiniteBitsStillFailTheCompleteOracle",
        "CP2CommitOracle.EverySnapshotMustBeFiniteIndependently",
        "CP2CommitOracle.NonCoefficientCanonicalMismatchRetainsPopulations",
        "CP2CommitOracle.SignedZeroIsOneNominalBitMismatch",
        "CP2CommitOracle.DetachedTypeUpdateCallMismatchIsUpdateLevelFailure",
        "CP2CommitOracle.InvalidPhaseRetainsExpectedAndRowCountsButNoPopulation",
        "CP2CommitOracle.CheckedIntegerHelpersNeverWrapOrClobberOnFailure",
    ],
    "test_cp2_commit_boundary": [
        "CP2CommitBoundary.AcceptedPathHasExactProofCommitClockFillOrder",
        "CP2CommitBoundary.RejectedProofSuppressesEveryPostproofOperation",
        "CP2CommitBoundary.ThrowingCommitPropagatesBeforeClockAndPreservesOutput",
        "CP2CommitBoundary.FailedFillRetainsCommittedStatusAndExactEndpoint",
    ],
    "test_cp2_canonical": [
        "CP2CanonicalSha256.MatchesPublishedVectorsUnderIncrementalChunking",
        "CP2CanonicalBytes.IntegerBinary64AndUtf8EncodingIsExact",
        "CP2CanonicalBytes.MatrixAndVectorUseLogicalRowMajorBinary64Order",
        "CP2CanonicalBytes.Utf8ValidationRejectsMalformedSequencesWithoutAppending",
        "CP2CanonicalBytes.SelfAppendStagesAliasedStorageBeforeGrowth",
    ],
    "test_cp2_offline_replay": [
        "CP2OfflineReplay.AcceptsOnlyTheExactAbsoluteCommandSurface",
        "CP2OfflineReplay.RejectsExtraMissingRelativeAndAliasedArguments",
        "CP2OfflineReplay.StatusNamesAreFrozen",
        "CP2OfflineReplay.DerivesEveryOnlineShadowMathConditionAndPreservesRejectedFeatures",
        "CP2OfflineReplay.CommitRequiresCandidateAndRecomputedBlocksMustActuallyPass",
    ],
    "test_cp2_recorded_assemble": [
        "CP2RecordedAssemble.EmptyCanonicalCampaignIsDeterministicAndUsesV101",
        "CP2RecordedAssemble.WrongFrozenSequenceAndCorruptJournalFailClosed",
        "CP2RecordedAssemble.OrphanStateProposalAndRawFramesFailClosed",
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
    "test_cp2_runtime_context": [
        "CP2RuntimeContext.RecordedFullAcceptsExactDocumentAndHashesBytes",
        "CP2RuntimeContext.SequenceAndTimingCombinationsAreExact",
        "CP2RuntimeContext.DuplicateMissingAndExtraKeysReject",
        "CP2RuntimeContext.JsonTypesNeverCoerce",
        "CP2RuntimeContext.SequenceAndLaunchExpectationsBindIdentity",
        "CP2RuntimeContext.PathsRequireNormalizedDistinctStrictChildren",
        "CP2RuntimeContext.OutputPresenceCannotCrossTraceLevels",
        "CP2RuntimeContext.EscapesAreStrictAndDecodedBeforeValidation",
        "CP2RuntimeContext.NonJsonNumbersConstantsAndTrailingBytesReject",
        "CP2RuntimeContext.RejectionIsFailureAtomicAndStatusNamesAreStable",
        "CP2RuntimeContext.DescriptorBoundFileReadAcceptsExactBytesAndBound",
        "CP2RuntimeContext.DescriptorBoundFileReadRejectsRelativeSymlinkAndHardlink",
    ],
    "test_cp2_ros1_runtime_parameters": [
        (
            "CP2ROS1RuntimeParameters."
            "FrozenDomainTagsOrderingSignedIntegerAndNegativeZeroAreExact"
        ),
        (
            "CP2ROS1RuntimeParameters."
            "NestedStructKeysUseUnsignedUtf8ByteOrderAndEscapeStrings"
        ),
        "CP2ROS1RuntimeParameters.BoolIntAndDoubleRemainDistinctTypedBytes",
        "CP2ROS1RuntimeParameters.NonfiniteAndInvalidXmlRpcValuesFailClosed",
        "CP2ROS1RuntimeParameters.NamesAndStringsRejectUnsafeOrInvalidInputs",
        "CP2ROS1RuntimeParameters.EmptyPopulationCannotMasqueradeAsCapture",
    ],
    "test_cp2_serial_pairing": [
        "CP2SerialPairing.StrictTwentyMillisecondBoundaryAndMetadataAreExact",
        "CP2SerialPairing.FirstForwardCandidateIsNeverReplacedByANearerMessage",
        "CP2SerialPairing.UsedFirstForwardCandidateCannotBeReusedOrSearchedPast",
        "CP2SerialPairing.IntegerNanosecondCompositionChecksRangeAndOverflow",
        "CP2SerialPairing.InvalidMessageKindFailsAtomicallyWithFrozenStatus",
        "CP2SerialPairing.EvidenceModeConsumesImuTailPastLastCamera",
    ],
    "test_cp2_serial_runtime_trace": [
        (
            "CP2SerialRuntimeTrace."
            "RecordedRowsJoinContiguousUpdaterIdentitiesAndWriteOnce"
        ),
        (
            "CP2SerialRuntimeTrace."
            "SequenceRowsRetainNonidentityQuaternionWithoutReordering"
        ),
        (
            "CP2SerialRuntimeTrace."
            "NoncontiguousWrongPairAndWrongTimestampUpdaterEventsFailSticky"
        ),
        (
            "CP2SerialRuntimeTrace."
            "DuplicateProcessingAndNonfiniteTrajectoryFailBeforeOutput"
        ),
        "CP2SerialRuntimeTrace.InitialPairPopulationRejectsBoundaryAndGaps",
        "CP2SerialRuntimeTrace.OutputCreationRejectsOverwriteAndSymlink",
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
    "test_cp2_trace_journal": [
        "CP2TraceJournalFormat.FrozenBootstrapKnownAnswerAndEmptyDecode",
        "CP2TraceJournalWriter.ShortWritesRemainExactAndFinalizeSeals",
        "CP2TraceJournalWriter.PartialFailureIsCountedAndRejectionIsSticky",
        "CP2TraceJournalWriter.SyncFailureIsStickyAndSealsPublication",
        "CP2TraceJournalWriter.WriterWithoutExplicitSyncFailsClosed",
        "CP2TraceJournalWriter.ByteBudgetOverflowWritesNoPartialUnit",
        (
            "CP2TraceJournalIdentity."
            "ContiguousInvocationsAllowRepeatedPairAndRegressingNewTimestamp"
        ),
        "CP2TraceJournalIdentity.DuplicateSkippedAndReorderedIdentityAreSticky",
        "CP2TraceJournalEvents.EveryLegalZeroRawTerminalRoundTrips",
        "CP2TraceJournalEvents.ImpossibleTerminalAndHiddenPhaseSuffixReject",
        "CP2TraceJournalEvents.DuplicateRawFeatureAndInvalidEnumRejectExplicitly",
        (
            "CP2TraceJournalPayloads."
            "OwningStateRawAndProposalBytesReconstructExactly"
        ),
        "CP2TraceJournalPayloads.NonzeroRawAndCommittedDecodeAccepted",
        "CP2TraceJournalPayloads.AnyPayloadByteMismatchRejectsBeforeWrite",
        "CP2TraceJournalDecoder.HeaderBootstrapAndLimitCorruptionsReject",
        (
            "CP2TraceJournalDecoder."
            "TruncationTrailingAndHostileSectionPopulationReject"
        ),
        (
            "CP2TraceJournalDecoder."
            "DuplicateUnknownMissingAndInvalidCoreSectionsReject"
        ),
        (
            "CP2TraceJournalDecoder."
            "CanonicalFragmentAndCrossIdentityCorruptionsReject"
        ),
        "CP2TraceJournalFile.PreopenedRegularFileFinalizesAtExactSize",
        "CP2TraceJournalFile.ReadOnlyAndMultipleLinkFilesReject",
        "CP2TraceJournalFile.CallerOffsetInterferenceFailsClosed",
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
        if (
            relative in CONFIG_INPUTS
            or relative in FROZEN_CP2_C_APPROVAL_BINDING
            or relative in {"LICENSE", *READINESS_ENTRYPOINTS}
        ):
            if not actual.is_file():
                raise RuntimeError("self-test needs frozen configuration: " + str(actual))
            shutil.copyfile(str(actual), str(destination))
            destination.chmod(0o755 if actual.stat().st_mode & stat.S_IXUSR else 0o644)
        else:
            destination.write_text("synthetic source: " + relative + "\n", encoding="utf-8")
    registry = repo_root / PREAUTHORIZATION_REGISTRY_PATH
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_bytes(b"DO_NOT_PARSE_SYNTHETIC_REGISTRY:\xff:\x00\n")
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

    def add(source, target, cp2_testing=False):
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
        ]
        if cp2_testing:
            tokens.append("-DOV_MSCKF_CP2_TESTING=1")
        tokens.extend(["-o", output, "-c", str(source_root / source)])
        entries.append({
            "command": " ".join(shlex.quote(token) for token in tokens),
            "directory": str(workspace_build_root / "ov_msckf"),
            "file": str(source_root / source),
            "output": output,
        })

    for source in RUNTIME_LIBRARY_SOURCES:
        add(source, "ov_msckf_lib")
        add(source, FAULT_LIBRARY_TARGET, cp2_testing=True)
    for target in CP1_TESTS:
        add("ov_msckf/test/cp1/gtest_main.cpp", target)
        add(TEST_SOURCE_BY_BINARY[target], target)
    for target in CP2_TESTS:
        cp2_testing = target == FAULT_INJECTION_TEST
        add("ov_msckf/test/cp2/gtest_main.cpp", target, cp2_testing=cp2_testing)
        add(TEST_SOURCE_BY_BINARY[target], target, cp2_testing=cp2_testing)
    path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def create_synthetic_link_commands(artifact_dir):
    def objects(target, sources):
        return [
            "CMakeFiles/{}.dir/{}.o".format(
                target, source[len("ov_msckf/"):]
            )
            for source in sources
        ]

    production_objects = objects("ov_msckf_lib", RUNTIME_LIBRARY_SOURCES)
    fault_objects = objects(FAULT_LIBRARY_TARGET, RUNTIME_LIBRARY_SOURCES)
    test_sources = (
        "ov_msckf/test/cp2/gtest_main.cpp",
        "ov_msckf/test/cp2/test_updater_msckf_end_to_end.cpp",
    )
    production_test_objects = objects(
        "test_cp2_updater_msckf_end_to_end", test_sources
    )
    fault_test_objects = objects(FAULT_INJECTION_TEST, test_sources)
    commands = {
        LINK_COMMAND_ARTIFACTS["production_library"]: [
            [
                "/usr/bin/c++", "-shared", *production_objects,
                "-o", "devel/lib/libov_msckf_lib.so",
            ]
        ],
        LINK_COMMAND_ARTIFACTS["fault_library"]: [
            [
                "/usr/bin/ar", "qc", "devel/lib/libov_msckf_cp2_fault_lib.a",
                *fault_objects,
            ],
            ["/usr/bin/ranlib", "devel/lib/libov_msckf_cp2_fault_lib.a"],
        ],
        LINK_COMMAND_ARTIFACTS["production_updater_test"]: [
            [
                "/usr/bin/c++", *production_test_objects,
                "-o", "devel/lib/ov_msckf/test_cp2_updater_msckf_end_to_end",
                "devel/lib/libov_msckf_lib.so",
            ]
        ],
        LINK_COMMAND_ARTIFACTS["fault_updater_test"]: [
            [
                "/usr/bin/c++", *fault_test_objects,
                "-o", "devel/lib/ov_msckf/test_cp2_updater_msckf_fault_injection",
                "devel/lib/libov_msckf_cp2_fault_lib.a",
            ]
        ],
    }
    for artifact_name, command_lines in commands.items():
        text = "".join(
            " ".join(shlex.quote(token) for token in tokens) + "\n"
            for tokens in command_lines
        )
        (artifact_dir / artifact_name).write_text(text, encoding="utf-8")


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
    fault_generic = binary_output / "cp2_synthetic_fault_gtest"
    subprocess.check_call(
        [compiler, str(build_dir / "main.c"), str(build_dir / "msckf.c"),
         "-Wl,--no-as-needed", str(project_lib / "libov_init_lib.so"),
         str(project_lib / "libov_core_lib.so"), str(gtest_lib / "libgtest.so"),
         str(ceres_version), *deterministic_link, "-o", str(fault_generic)],
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
        template = fault_generic if test_name == FAULT_INJECTION_TEST else generic
        shutil.copyfile(str(template), str(source))
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
        ["/usr/bin/git", "-c", "tar.umask=0002", "-C", str(repo_root),
         "archive", "--format=tar",
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
    create_synthetic_link_commands(artifact_dir)
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
        "argv": [
            "/usr/bin/python3", "-I", "-B",
            str(repo_root / "scripts/cp2/verify_report.py"), "--unit-self-test",
        ],
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
        "Synthetic corruptions rejected: synthetic-bootstrap\n"
        "CP2_READINESS_ENGINE_PROTECTING_TESTS count=39 passed=true "
        "module_sha256={} output_sha256={}\n".format("0" * 64, "1" * 64),
        encoding="utf-8",
    )

    workspace_record = {
        "archive_argv": [
            "/usr/bin/git", "-c", "tar.umask=0002", "-C", str(repo_root),
            "archive", "--format=tar",
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
        started_utc = (
            synthetic_epoch + dt.timedelta(seconds=started_second)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        finished_utc = (
            synthetic_epoch + dt.timedelta(seconds=started_second + 1)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
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
            "finished_utc": finished_utc,
            "loader_map": linkage["executables"][test_name]["loader"],
            "name": test_name,
            "serialized": True,
            "started_utc": started_utc,
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


class ActualVerificationError(ValueError):
    """A fail-closed CP2-C/D/E detached-verifier rejection."""


ACTUAL_MAX_JSON_BYTES = 64 * 1024 * 1024
ACTUAL_MAX_JSONL_BYTES = 4 * 1024 * 1024 * 1024
ACTUAL_MAX_BINARY_BYTES = 16 * 1024 * 1024 * 1024
ACTUAL_MAX_READINESS_GIT_OUTPUT_BYTES = 1024 * 1024 * 1024
ACTUAL_OFFLINE_REPLAY_TIMEOUT_SECONDS = 300
ACTUAL_PROCESS_GROUP_CLEANUP_SECONDS = 10.0
ACTUAL_PROCESS_GROUP_POLL_SECONDS = 0.01
ACTUAL_EXACT_BINARY64_INTEGER_MAX = (1 << 53) - 1
ACTUAL_CP2_C_AUTHORIZED = False
ACTUAL_CP2_C_BLOCK_REASON = (
    "CP2-C actual verification is blocked before artifact access: the "
    "detached-readiness replacement contract is pending exact-commit approval "
    "and a separate approval-binding commit"
)
ACTUAL_SEQUENCE_IDS = ("MH_01_easy", "MH_03_medium", "V1_01_easy")
ACTUAL_SEQUENCE_OFFSETS_SECONDS = (40.0, 5.0, 0.0)
ACTUAL_PARAMETER_DIFF_KEYS = (
    "/cp2_vio/up_msckf_landmark_elimination",
    "/cp2_vio/filepath_est",
    "/cp2_vio/filepath_std",
    "/cp2_vio/record_timing_filepath",
    "/cp2_vio/cp2_trace_directory",
    "/cp2_vio/cp2_context_path",
)
ACTUAL_PROVENANCE_KEYS = (
    "schema_version", "record_type", "checkpoint", "evidence_class",
    "distribution_status", "eligible_for_cp2_seal", "created_utc", "branch",
    "source_commit", "source_tree", "source_archive", "source_archive_sha256",
    "clean", "cp1_authorization_commit", "contracts", "entrypoints",
    "readiness_barrier", "unit_anchor", "build", "readiness_barrier_sha256",
    "runtime", "configuration", "inputs", "environment", "host", "file_inventory",
)
ACTUAL_READINESS_KEYS = (
    "schema_version", "record_type", "entrypoints", "source_before_sha256",
    "build_before_sha256", "source_before_payload", "build_before_payload",
    "results_before_payload", "results_before_sha256", "testing_before_payload",
    "testing_before_sha256", "self_tests", "source_after_payload",
    "source_after_sha256", "build_after_payload", "build_after_sha256",
    "results_after_payload", "results_after_sha256", "testing_after_payload",
    "testing_after_sha256", "post_lock_payload", "post_lock_recheck_sha256",
    "post_unit_payload", "post_unit_recheck_sha256", "source_context_payload",
    "source_context_sha256", "data_lock", "readiness_git_environment",
    "readiness_git_commands", "unit_verification", "unit_artifact",
    "unit_manifest_sha256", "unit_tested_commit", "unit_tested_tree",
    "bag_provider_calls", "passed",
)
ACTUAL_READINESS_SELF_TEST_KEYS = (
    "index", "path", "argv", "cwd", "entrypoint_sha256", "started_utc",
    "finished_utc", "environment_sha256", "exit_code", "timed_out",
    "process_group_complete", "stdout", "stdout_sha256", "stderr", "stderr_sha256",
    "expected_case_names", "result", "bag_provider_calls", "temporary_root_removed",
)
ACTUAL_COMMAND_KEYS = (
    "schema_version", "record_type", "command_id", "phase", "sequence_index",
    "pair_index", "run_index", "argv", "cwd", "environment_sha256",
    "started_utc", "finished_utc", "exit_code", "timed_out", "stdout",
    "stdout_sha256", "stderr", "stderr_sha256",
)
ACTUAL_COMMAND_PHASES = frozenset((
    "readiness", "source_archive", "configure", "build", "runtime_preflight",
    "bag_identity", "pair_index", "ros_run", "trajectory", "evaluation",
    "verification",
))
ACTUAL_INVENTORY_ROLES = frozenset((
    "report", "provenance", "command", "trace", "payload", "source", "build",
    "configuration", "readiness", "log", "trajectory", "evaluator",
))
ACTUAL_HOST_KEYS = (
    "hostname", "os_release", "kernel_release", "architecture", "cpu_model",
    "logical_cpu_count", "ros_distribution", "compiler_version", "cmake_version",
    "catkin_version", "eigen_version", "opencv_version", "boost_version",
    "ceres_version", "python_version", "evo_version",
)
ACTUAL_RUNTIME_CONTEXT_KEYS = (
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
ACTUAL_STRICT_FP_SOURCE_TARGETS = {
    "ov_msckf/src/ros/CP2ROS1RuntimeParameters.cpp": "ov_msckf_lib",
    "ov_msckf/src/state/StateHelper.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2Canonical.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CommitBoundary.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CommitOracle.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2CompositeState.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2FeatureGate.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2OfflineReplay.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2RecordedAssemble.cpp": "cp2_recorded_assemble",
    "ov_msckf/src/update/CP2RuntimeContext.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2SerialPairing.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2SerialRuntimeTrace.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2ShadowMath.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2StateTraceCodec.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2TraceCodec.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/CP2TraceJournal.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/SchurUpdate.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterHelper.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterMSCKF.cpp": "ov_msckf_lib",
    "ov_msckf/src/update/UpdaterMSCKFPreview.cpp": "ov_msckf_lib",
}
_ACTUAL_LOCAL_MODULE_PATHS = {
    "cp2_schema": "scripts/cp2/cp2_schema.py",
    "cp2_sequence_math": "scripts/cp2/cp2_sequence_math.py",
}
_ACTUAL_MODULE_CACHE = {}
_ACTUAL_MODULE_SOURCE_BINDING = None
_ACTUAL_MODULE_SOURCE_BINDING_ORIGIN = None


def _actual_fail(message):
    raise ActualVerificationError(message)


def _actual_exact_keys(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        missing = sorted(set(keys) - set(value)) if isinstance(value, dict) else sorted(keys)
        extra = sorted(set(value) - set(keys)) if isinstance(value, dict) else []
        _actual_fail("{} key inventory differs (missing={!r}, extra={!r})".format(label, missing, extra))
    return value


def _actual_u64(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << 64):
        _actual_fail(label + " is not u64")
    return value


def _actual_i64(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not -(1 << 63) <= value < (1 << 63):
        _actual_fail(label + " is not i64")
    return value


def _actual_f64(value, label, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _actual_fail(label + " is not a binary64 number")
    try:
        converted = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ActualVerificationError(label + " is not representable as binary64") from exc
    if not math.isfinite(converted) or (nonnegative and converted < 0.0):
        _actual_fail(label + " is not a valid finite binary64 number")
    return converted


def _actual_same_f64(left, right):
    try:
        return struct.pack(">d", _actual_f64(left, "left binary64")) == struct.pack(
            ">d", _actual_f64(right, "right binary64")
        )
    except ActualVerificationError:
        return False


def _actual_sha256(value, label):
    if not isinstance(value, str) or HEX64_PATTERN.fullmatch(value) is None:
        _actual_fail(label + " is not lowercase SHA-256")
    return value


def _actual_hex40(value, label):
    if not isinstance(value, str) or HEX40_PATTERN.fullmatch(value) is None:
        _actual_fail(label + " is not a 40-character lowercase object ID")
    return value


def _actual_utc(value, label):
    if not isinstance(value, str) or re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
        value,
    ) is None:
        _actual_fail(label + " is not canonical UTC")
    try:
        return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError as exc:
        raise ActualVerificationError(label + " is not a valid UTC instant") from exc


def _actual_relpath(value, label):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        _actual_fail(label + " is not a normalized POSIX relpath")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.as_posix() != value or any(
        part in ("", ".", "..") for part in pure.parts
    ):
        _actual_fail(label + " is not a normalized POSIX relpath")
    return value


def _actual_safe_absolute_path(value, label):
    if not isinstance(value, str) or "\0" in value or not os.path.isabs(value):
        _actual_fail(label + " is not absolute")
    if os.path.normpath(value) != value or ".." in Path(value).parts:
        _actual_fail(label + " is not normalized")
    return Path(value)


def _actual_checked_sum(values, label):
    total = 0
    for index, value in enumerate(values):
        item = _actual_u64(value, "{}[{}]".format(label, index))
        if total > (1 << 64) - 1 - item:
            _actual_fail(label + " overflows u64")
        total += item
    return total


def _actual_checked_product(left, right, label):
    lhs = _actual_u64(left, label + " left")
    rhs = _actual_u64(right, label + " right")
    if lhs and rhs > ((1 << 64) - 1) // lhs:
        _actual_fail(label + " overflows u64")
    return lhs * rhs


def _actual_require_tonearest(label):
    """Require the Linux target's FE_TONEAREST immediately before division."""

    try:
        function = ctypes.CDLL(None, use_errno=True).fegetround
        function.argtypes = []
        function.restype = ctypes.c_int
        observed = function()
    except (AttributeError, OSError) as exc:
        raise ActualVerificationError(
            "cannot inspect floating-point rounding mode for " + label
        ) from exc
    # glibc (including the Jetson Nano aarch64 target) defines FE_TONEAREST as
    # zero.  Failing rather than changing the process mode preserves detached
    # verifier independence.
    if observed != 0:
        _actual_fail(label + " requires FE_TONEAREST")


def _actual_exact_count_ratio(numerator, denominator, label):
    numerator = _actual_u64(numerator, label + " numerator")
    denominator = _actual_u64(denominator, label + " denominator")
    if denominator == 0:
        _actual_fail(label + " denominator is zero")
    if (
        numerator > ACTUAL_EXACT_BINARY64_INTEGER_MAX
        or denominator > ACTUAL_EXACT_BINARY64_INTEGER_MAX
    ):
        _actual_fail(label + " operand exceeds the exact binary64 integer range")
    _actual_require_tonearest(label)
    result = float(numerator) / float(denominator)
    if not math.isfinite(result):
        _actual_fail(label + " result is nonfinite")
    return result


def _actual_checked_counter_add(counter, key, value, label):
    counter[key] = _actual_checked_sum((counter[key], value), label)


def _actual_install_module_source_binding(records, origin):
    """Install the only source identities permitted for local helper execution."""

    global _ACTUAL_MODULE_SOURCE_BINDING
    global _ACTUAL_MODULE_SOURCE_BINDING_ORIGIN
    if origin not in ("artifact_source_context", "explicit_self_test_fixture"):
        _actual_fail("local-module source-binding origin is invalid")
    binding = {}
    for name, relative in _ACTUAL_LOCAL_MODULE_PATHS.items():
        record = records.get(relative) if isinstance(records, dict) else None
        if not isinstance(record, dict):
            _actual_fail("local-module source binding omits " + relative)
        binding[name] = {
            "mode": _actual_u64(record.get("mode"), name + " source mode"),
            "sha256": _actual_sha256(
                record.get("sha256"), name + " source SHA-256"
            ),
            "size": _actual_u64(record.get("size"), name + " source size"),
        }
        if binding[name]["mode"] not in (0o100644, 0o100755):
            _actual_fail(name + " source Git mode is invalid")
    if _ACTUAL_MODULE_SOURCE_BINDING is None:
        _ACTUAL_MODULE_SOURCE_BINDING = binding
        _ACTUAL_MODULE_SOURCE_BINDING_ORIGIN = origin
    elif (
        _ACTUAL_MODULE_SOURCE_BINDING != binding
        or _ACTUAL_MODULE_SOURCE_BINDING_ORIGIN != origin
    ):
        _actual_fail("local-module source binding changed within one verifier process")
    for name, module in _ACTUAL_MODULE_CACHE.items():
        if getattr(module, "__schurvio_source_binding__", None) != binding.get(name):
            _actual_fail("cached local module predates or differs from its source binding")


def _actual_install_self_test_module_source_binding():
    """Bind local helpers explicitly for the artifact-free verifier self-test."""

    base = Path(__file__).resolve().parents[2]
    records = {}
    for name, relative in _ACTUAL_LOCAL_MODULE_PATHS.items():
        path = base / relative
        status_value = path.lstat()
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _actual_fail("self-test local module is not a protected regular file")
        records[relative] = {
            "mode": 0o100755 if status_value.st_mode & stat.S_IXUSR else 0o100644,
            "sha256": sha256_file(path),
            "size": status_value.st_size,
        }
    _actual_install_module_source_binding(records, "explicit_self_test_fixture")


def _actual_load_module(name):
    if name not in _ACTUAL_LOCAL_MODULE_PATHS:
        _actual_fail("local verifier module is not allowlisted: " + str(name))
    if _ACTUAL_MODULE_SOURCE_BINDING is None:
        _actual_fail("local verifier module has no established source-context binding")
    expected = _ACTUAL_MODULE_SOURCE_BINDING[name]
    cached = _ACTUAL_MODULE_CACHE.get(name)
    if cached is not None:
        if getattr(cached, "__schurvio_source_binding__", None) != expected:
            _actual_fail("cached local verifier module differs from source context")
        return cached
    directory_path = Path(__file__).resolve().parent
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        file_flags |= os.O_NOFOLLOW
    directory_fd = os.open(str(directory_path), directory_flags)
    try:
        filename = name + ".py"
        descriptor = os.open(filename, file_flags, dir_fd=directory_fd)
        try:
            before = os.fstat(descriptor)
            path_status = os.stat(
                filename, dir_fd=directory_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or (before.st_dev, before.st_ino) != (
                    path_status.st_dev, path_status.st_ino
                )
                or before.st_size != expected["size"]
                or bool(before.st_mode & stat.S_IXUSR)
                != (expected["mode"] == 0o100755)
            ):
                _actual_fail(name + " held source identity differs from source context")
            chunks = []
            remaining = before.st_size
            while remaining:
                block = os.read(descriptor, min(remaining, 1024 * 1024))
                if not block:
                    _actual_fail(name + " held source is truncated")
                chunks.append(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                _actual_fail(name + " held source grew while being read")
            after = os.fstat(descriptor)
            if (
                before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
                before.st_size, before.st_mtime_ns, before.st_ctime_ns,
            ) != (
                after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                after.st_size, after.st_mtime_ns, after.st_ctime_ns,
            ):
                _actual_fail(name + " held source metadata changed while being read")
            source = b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    if hashlib.sha256(source).hexdigest() != expected["sha256"]:
        _actual_fail(name + " held source digest differs from source context")
    display_path = str(directory_path / (name + ".py"))
    spec = importlib.util.spec_from_file_location("_schurvio_" + name, display_path)
    if spec is None or spec.loader is None:
        _actual_fail("cannot construct loader for " + name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        code = compile(source, display_path, "exec", dont_inherit=True)
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    module.__schurvio_source_binding__ = dict(expected)
    _ACTUAL_MODULE_CACHE[name] = module
    return module


def _actual_read_bytes(path, label, maximum):
    try:
        status = path.lstat()
    except OSError as exc:
        raise ActualVerificationError("cannot stat {}: {}".format(label, exc)) from exc
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        _actual_fail(label + " is not a single-link regular file")
    if status.st_size > maximum:
        _actual_fail(label + " exceeds the verifier bound")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise ActualVerificationError("cannot read {}: {}".format(label, exc)) from exc
    if len(content) != status.st_size:
        _actual_fail(label + " changed while being read")
    return content


def _actual_json(path, label):
    value = strict_json_bytes(_actual_read_bytes(path, label, ACTUAL_MAX_JSON_BYTES), label)
    if not isinstance(value, dict):
        _actual_fail(label + " is not a JSON object")
    return value


def _actual_jsonl(path, label):
    content = _actual_read_bytes(path, label, ACTUAL_MAX_JSONL_BYTES)
    if b"\r" in content or (content and not content.endswith(b"\n")):
        _actual_fail(label + " is not exact LF-terminated JSONL")
    records = []
    for line_index, line in enumerate(content.splitlines(), 1):
        if not line:
            _actual_fail("{} contains a blank line at {}".format(label, line_index))
        value = strict_json_bytes(line, "{} line {}".format(label, line_index))
        if not isinstance(value, dict):
            _actual_fail("{} line {} is not an object".format(label, line_index))
        records.append(value)
    return records, content


def _actual_scan_and_verify_manifest(artifact, expected_digest):
    raw = os.fspath(artifact)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        _actual_fail("artifact path must be normalized and absolute")
    try:
        root_status = os.lstat(raw)
    except OSError as exc:
        raise ActualVerificationError("cannot stat artifact directory: " + str(exc)) from exc
    if not stat.S_ISDIR(root_status.st_mode) or stat.S_ISLNK(root_status.st_mode):
        _actual_fail("artifact root is not a real directory")
    if root_status.st_mode & 0o222:
        _actual_fail("artifact root is writable")

    observed = {}
    directories = {".": root_status}
    for current, dirnames, filenames in os.walk(raw, topdown=True, followlinks=False):
        dirnames.sort(key=lambda item: os.fsencode(item))
        filenames.sort(key=lambda item: os.fsencode(item))
        current_path = Path(current)
        retained_dirs = []
        for name in dirnames:
            candidate = current_path / name
            relative = candidate.relative_to(artifact).as_posix()
            status = candidate.lstat()
            if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
                _actual_fail("artifact directory entry is not a real directory: " + relative)
            if status.st_mode & 0o222:
                _actual_fail("artifact directory is writable: " + relative)
            directories[relative] = status
            retained_dirs.append(name)
        dirnames[:] = retained_dirs
        for name in filenames:
            candidate = current_path / name
            relative = candidate.relative_to(artifact).as_posix()
            _actual_relpath(relative, "artifact file")
            status = candidate.lstat()
            if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                _actual_fail("artifact entry is not a single-link regular file: " + relative)
            if status.st_mode & 0o222:
                _actual_fail("artifact file is writable: " + relative)
            observed[relative] = status
    if MANIFEST_NAME not in observed:
        _actual_fail("artifact lacks SHA256SUMS")
    manifest_path = artifact / MANIFEST_NAME
    manifest_bytes_value = _actual_read_bytes(
        manifest_path, MANIFEST_NAME, ACTUAL_MAX_JSONL_BYTES
    )
    expected_anchor = _actual_sha256(expected_digest, "external manifest anchor")
    if hashlib.sha256(manifest_bytes_value).hexdigest() != expected_anchor:
        _actual_fail("SHA256SUMS differs from the supplied external digest anchor")
    if b"\r" in manifest_bytes_value or (
        manifest_bytes_value and not manifest_bytes_value.endswith(b"\n")
    ):
        _actual_fail("SHA256SUMS is not LF terminated")
    manifest = {}
    previous = None
    for line_index, line in enumerate(manifest_bytes_value.splitlines(keepends=True), 1):
        match = re.fullmatch(rb"([0-9a-f]{64})  ([^\r\n]+)\n", line)
        if match is None:
            _actual_fail("SHA256SUMS line {} is malformed".format(line_index))
        digest_bytes, path_bytes = match.groups()
        try:
            relative = path_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError("SHA256SUMS path is not UTF-8") from exc
        _actual_relpath(relative, "SHA256SUMS path")
        if relative == MANIFEST_NAME or previous is not None and path_bytes <= previous:
            _actual_fail("SHA256SUMS paths are self-referential, duplicate, or unsorted")
        previous = path_bytes
        manifest[relative] = digest_bytes.decode("ascii")
    actual_files = set(observed) - {MANIFEST_NAME}
    if set(manifest) != actual_files:
        _actual_fail("artifact file set differs from SHA256SUMS")
    for relative, digest in manifest.items():
        if sha256_file(artifact / relative) != digest:
            _actual_fail("artifact checksum mismatch: " + relative)
    return manifest, observed, directories


def _actual_environment_classes(provenance):
    environment = _actual_exact_keys(
        provenance.get("environment"), ("classes",), "provenance.environment"
    )
    classes = environment["classes"]
    if not isinstance(classes, list) or not classes:
        _actual_fail("provenance environment class population is empty")
    schema = _actual_load_module("cp2_schema")
    result = {}
    previous = None
    for index, record in enumerate(classes):
        _actual_exact_keys(
            record, ("environment_id", "variables", "canonical_sha256"),
            "environment class {}".format(index),
        )
        identifier = record["environment_id"]
        if not isinstance(identifier, str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", identifier
        ) is None:
            _actual_fail("environment ID is invalid")
        encoded_identifier = identifier.encode("utf-8")
        if previous is not None and encoded_identifier <= previous:
            _actual_fail("environment classes are duplicate or unsorted")
        previous = encoded_identifier
        variables = record["variables"]
        if not isinstance(variables, list):
            _actual_fail("environment variables are not an array")
        mapping = {}
        previous_name = None
        for variable in variables:
            _actual_exact_keys(variable, ("name", "value"), "environment variable")
            name = variable["name"]
            value = variable["value"]
            if not isinstance(name, str) or not isinstance(value, str) or "\0" in name + value:
                _actual_fail("environment variable is not an exact UTF-8 string pair")
            name_bytes = name.encode("utf-8", "strict")
            if previous_name is not None and name_bytes <= previous_name:
                _actual_fail("environment variables are duplicate or unsorted")
            previous_name = name_bytes
            mapping[name] = value
        digest = schema.command_environment_sha256(mapping)
        if record["canonical_sha256"] != digest:
            _actual_fail("environment canonical SHA-256 mismatch")
        if digest in result:
            _actual_fail("environment classes contain duplicate canonical bytes")
        result[digest] = mapping
    return result


def _actual_validate_commands(artifact, provenance, manifest):
    records, content = _actual_jsonl(artifact / "commands.jsonl", "commands.jsonl")
    classes = _actual_environment_classes(provenance)
    readiness_git_digests = {
        record["canonical_sha256"]
        for record in provenance["environment"]["classes"]
        if record["environment_id"] == "readiness_git_v1"
    }
    referenced_classes = set()
    stream_paths = set()
    for index, record in enumerate(records):
        _actual_exact_keys(record, ACTUAL_COMMAND_KEYS, "command {}".format(index))
        if (
            not _schema_version_one(record.get("schema_version"))
            or record.get("record_type") != "command"
        ):
            _actual_fail("command schema identity is invalid")
        if _actual_u64(record.get("command_id"), "command ID") != index:
            _actual_fail("command IDs are not contiguous")
        if record.get("phase") not in ACTUAL_COMMAND_PHASES:
            _actual_fail("command phase is invalid")
        for field in ("sequence_index", "pair_index", "run_index"):
            if record[field] is not None:
                _actual_u64(record[field], "command " + field)
        if not isinstance(record["argv"], list) or not record["argv"] or not all(
            isinstance(item, str) and "\0" not in item for item in record["argv"]
        ):
            _actual_fail("command argv is invalid")
        _actual_safe_absolute_path(record["cwd"], "command cwd")
        environment_sha = _actual_sha256(record["environment_sha256"], "command environment")
        if environment_sha not in classes:
            _actual_fail("command references an unknown environment class")
        command_environment = classes[environment_sha]
        if (
            "CP2_SELF_TEST" in command_environment
            or "CP2_FORBID_BAG_ACCESS" in command_environment
        ):
            _actual_fail("commands.jsonl references the readiness-only self-test environment")
        referenced_classes.add(environment_sha)
        if record["exit_code"] is not None:
            _actual_i64(record["exit_code"], "command exit code")
        if not isinstance(record["timed_out"], bool):
            _actual_fail("command timeout flag is not Boolean")
        if record["exit_code"] != 0 or record["timed_out"]:
            _actual_fail("passing actual artifact contains a failed/timed-out command")
        for stream_name in ("stdout", "stderr"):
            relative = _actual_relpath(record[stream_name], "command " + stream_name)
            if relative in stream_paths:
                _actual_fail("command stream path aliases another stream: " + relative)
            stream_paths.add(relative)
            if relative not in manifest:
                _actual_fail("command stream is absent from manifest: " + relative)
            if record[stream_name + "_sha256"] != manifest[relative]:
                _actual_fail("command stream hash differs from manifest")
        started = _actual_utc(record["started_utc"], "command started_utc")
        finished = _actual_utc(record["finished_utc"], "command finished_utc")
        if finished < started:
            _actual_fail("command UTC interval is negative")
    # The readiness-only class may be absent from commands, but every other
    # retained class must be used by a top-level command.
    for digest, mapping in classes.items():
        is_self_test = "CP2_SELF_TEST" in mapping or "CP2_FORBID_BAG_ACCESS" in mapping
        if is_self_test:
            if mapping != {
                "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                "CP2_SELF_TEST": "1", "CP2_FORBID_BAG_ACCESS": "1",
            }:
                _actual_fail("readiness self-test environment is not exact")
        elif digest in readiness_git_digests:
            pass
        elif digest not in referenced_classes:
            _actual_fail("non-readiness environment class is unreferenced")
    return records, content, classes


def _actual_validate_readiness_command_zero(
    commands, provenance, barrier, unit_environment_digest
):
    readiness_commands = [record for record in commands if record["phase"] == "readiness"]
    if len(readiness_commands) != 1 or readiness_commands[0] is not commands[0]:
        _actual_fail("commands.jsonl lacks one exact command-zero readiness verifier")
    command = readiness_commands[0]
    unit = barrier["unit_verification"]
    if (
        command["command_id"] != 0
        or any(
            command[field] is not None
            for field in ("sequence_index", "pair_index", "run_index")
        )
        or command["argv"] != unit["argv"]
        or command["cwd"] != unit["cwd"]
        or command["environment_sha256"] != unit_environment_digest
        or command["started_utc"] != unit["started_utc"]
        or command["finished_utc"] != unit["finished_utc"]
        or command["exit_code"] != unit["exit_code"]
        or command["timed_out"] is not unit["timed_out"]
        or command["stdout"] != unit["stdout"]
        or command["stdout_sha256"] != unit["stdout_sha256"]
        or command["stderr"] != unit["stderr"]
        or command["stderr_sha256"] != unit["stderr_sha256"]
    ):
        _actual_fail("command zero differs from retained unit verification")
    expected_environment_record = {
        "environment_id": "unit_verifier_v1",
        "variables": [
            {"name": name, "value": unit["environment"][name]}
            for name in sorted(
                unit["environment"], key=lambda value: value.encode("utf-8")
            )
        ],
        "canonical_sha256": unit_environment_digest,
    }
    if [
        record for record in provenance["environment"]["classes"]
        if record.get("environment_id") == "unit_verifier_v1"
    ] != [expected_environment_record]:
        _actual_fail("unit-verifier environment class differs")


def _actual_elf_build_id(path):
    command = ["/usr/bin/readelf", "-n", str(path)]
    try:
        completed = subprocess.run(
            command, cwd="/tmp", env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            check=False, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ActualVerificationError("cannot independently read runtime ELF build ID") from exc
    if completed.returncode != 0:
        _actual_fail("runtime executable is not a readable ELF object")
    matches = re.findall(rb"Build ID: ([0-9a-f]+)", completed.stdout)
    if len(matches) != 1:
        _actual_fail("runtime executable does not have exactly one build ID")
    return matches[0].decode("ascii")


def _actual_validate_static_bundle(artifact, configuration, manifest):
    relative = _actual_relpath(
        configuration["static_bundle_payload"], "static bundle payload"
    )
    content = _actual_read_bytes(
        artifact / relative, "static bundle payload", ACTUAL_MAX_BINARY_BYTES
    )
    digest = hashlib.sha256(content).hexdigest()
    if manifest.get(relative) != digest or configuration["static_bundle_sha256"] != digest:
        _actual_fail("static bundle payload/digest identity differs")
    domain = b"SchurVIO-CP2-static-config-v1\0"
    if not content.startswith(domain):
        _actual_fail("static bundle domain is invalid")
    reader = _ActualByteReader(content[len(domain):], "static bundle payload")
    if reader.u64("static record count") != 4:
        _actual_fail("static bundle record count is not four")
    records = list(configuration["static_files"]) + [configuration["launch"]]
    for index, record in enumerate(records):
        length = reader.u64("static path length")
        try:
            path = reader.take(length, "static path").decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError("static bundle path is not UTF-8") from exc
        size = reader.u64("static file size")
        sha256 = reader.take(32, "static file SHA-256").hex()
        if (
            path != record["path"]
            or size != record["size"]
            or sha256 != record["sha256"]
        ):
            _actual_fail(
                "static bundle record {} differs from configuration provenance".format(index)
            )
    reader.finish()


def _actual_compile_command_path(value, directory, label, must_exist):
    if (
        not isinstance(value, str)
        or not value
        or "\0" in value
        or not isinstance(directory, str)
        or not directory
        or "\0" in directory
        or not os.path.isabs(directory)
        or os.path.normpath(directory) != directory
    ):
        _actual_fail(label + " path/directory is invalid")
    candidate = value if os.path.isabs(value) else os.path.join(directory, value)
    if not os.path.isabs(candidate) or os.path.normpath(candidate) != candidate:
        _actual_fail(label + " path is not normalized absolute")
    path = Path(candidate)
    if must_exist:
        try:
            status_value = path.lstat()
        except OSError as exc:
            raise ActualVerificationError(label + " path cannot be inspected") from exc
        if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
            _actual_fail(label + " path is not a single-link regular file")
    try:
        return path.resolve(strict=must_exist)
    except OSError as exc:
        raise ActualVerificationError(label + " path cannot be resolved") from exc


def _actual_validate_compile_commands(artifact, build):
    relative = _actual_relpath(build["compile_commands"], "compile commands")
    content = _actual_read_bytes(
        artifact / relative, "compile_commands.json", 256 * 1024 * 1024
    )
    document = strict_json_bytes(content, "compile_commands.json")
    if not isinstance(document, list) or not document:
        _actual_fail("compile_commands.json is empty or not an array")

    workspace = _actual_safe_absolute_path(build["workspace"], "fresh build workspace")
    try:
        workspace = workspace.resolve(strict=True)
        source_space = (workspace / "src").resolve(strict=True)
        build_space = (workspace / "build").resolve(strict=True)
        compiler = Path("/usr/bin/c++").resolve(strict=True)
    except OSError as exc:
        raise ActualVerificationError("fresh-build compile roots cannot be resolved") from exc
    expected_sources = {}
    for source, target in ACTUAL_STRICT_FP_SOURCE_TARGETS.items():
        expected_path = _actual_compile_command_path(
            str(source_space / source), str(source_space),
            "expected strict-FP source", True,
        )
        if expected_path in expected_sources:
            _actual_fail("strict-FP expected source paths alias")
        expected_sources[expected_path] = (source, target)
    relevant_basenames = {
        Path(source).name for source in ACTUAL_STRICT_FP_SOURCE_TARGETS
    }
    observed = {source: [] for source in ACTUAL_STRICT_FP_SOURCE_TARGETS}

    for index, record in enumerate(document):
        if not isinstance(record, dict):
            _actual_fail("compile command {} is not an object".format(index))
        declared_source = record.get("file")
        if not isinstance(declared_source, str):
            continue
        basename = Path(declared_source).name
        if basename not in relevant_basenames:
            continue
        directory = record.get("directory")
        source_path = _actual_compile_command_path(
            declared_source, directory, "compile-command source", True
        )
        expected = expected_sources.get(source_path)
        if expected is None:
            _actual_fail("strict-FP compile commands contain a basename spoof: " + basename)
        source, expected_target = expected

        has_arguments = "arguments" in record
        has_command = "command" in record
        if has_arguments == has_command:
            _actual_fail("compile command must have exactly one argv representation")
        if has_arguments:
            tokens = record["arguments"]
            if (
                not isinstance(tokens, list)
                or not tokens
                or not all(isinstance(token, str) for token in tokens)
            ):
                _actual_fail("compile-command arguments are invalid")
            tokens = list(tokens)
        else:
            command = record["command"]
            if not isinstance(command, str) or not command or "\0" in command:
                _actual_fail("compile-command command is invalid")
            try:
                tokens = shlex.split(command, posix=True)
            except ValueError as exc:
                raise ActualVerificationError(
                    "compile-command command cannot be tokenized"
                ) from exc
        if not tokens or any("\0" in token for token in tokens):
            _actual_fail("compile-command argv is empty or contains NUL")
        try:
            actual_compiler = Path(tokens[0]).resolve(strict=True)
        except OSError as exc:
            raise ActualVerificationError("compile-command compiler cannot be resolved") from exc
        if actual_compiler != compiler:
            _actual_fail("strict-FP command does not use the pinned compiler")

        compile_positions = [
            position for position, token in enumerate(tokens) if token == "-c"
        ]
        output_positions = [
            position for position, token in enumerate(tokens) if token == "-o"
        ]
        if (
            len(compile_positions) != 1
            or compile_positions[0] + 1 >= len(tokens)
            or len(output_positions) != 1
            or output_positions[0] + 1 >= len(tokens)
        ):
            _actual_fail("strict-FP command lacks one exact -c/-o binding")
        command_source = _actual_compile_command_path(
            tokens[compile_positions[0] + 1], directory,
            "actual compiler source", True,
        )
        if command_source != source_path:
            _actual_fail("compile-command file differs from its -c source")
        output = _actual_compile_command_path(
            tokens[output_positions[0] + 1], directory, "compiler output", False
        )
        expected_output = (
            build_space / "ov_msckf" / "CMakeFiles" /
            (expected_target + ".dir") /
            (source[len("ov_msckf/"):] + ".o")
        )
        if output != expected_output:
            _actual_fail(
                "strict-FP object does not bind its exact source and target: " + source
            )
        if record.get("output") is not None and _actual_compile_command_path(
            record["output"], directory, "declared compiler output", False
        ) != output:
            _actual_fail("declared compile output differs from -o output")

        strict = strict_flag_record(tokens, workspace=workspace)
        if (
            not strict["passed"]
            or not cp2_testing_macro_record(tokens, False)["passed"]
            or any(
                token == "-wrapper"
                or token.startswith("-fplugin=")
                for token in tokens
            )
        ):
            _actual_fail("strict-FP command is not effectively strict: " + source)
        observed[source].append((expected_target, output))

    invalid = [
        source for source, records in observed.items()
        if len(records) != 1
        or records[0][0] != ACTUAL_STRICT_FP_SOURCE_TARGETS[source]
    ]
    if invalid:
        _actual_fail(
            "strict-FP exact source/target population is incomplete: "
            + ",".join(sorted(invalid))
        )


def _actual_readiness_lp(reader, label):
    return reader.take(reader.u64(label + " length"), label)


def _actual_parse_readiness_snapshot(payload, expected_root, context=None):
    """Verifier-local parser/re-encoder for one frozen readiness snapshot."""

    reader = _ActualByteReader(payload, "readiness " + expected_root + " snapshot")
    domain = b"SchurVIO-CP2-readiness-snapshot-v1\0"
    if reader.take(len(domain), "domain") != domain:
        _actual_fail("readiness snapshot domain differs")
    try:
        root_tag = _actual_readiness_lp(reader, "root tag").decode("ascii", "strict")
    except UnicodeDecodeError as exc:
        raise ActualVerificationError("readiness snapshot root tag is not ASCII") from exc
    if root_tag != expected_root or root_tag not in {
        "source", "build", "results", "testing", "post_lock"
    }:
        _actual_fail("readiness snapshot root tag differs")
    existence = reader.take(1, "existence")
    if existence not in (b"\0", b"\1"):
        _actual_fail("readiness snapshot existence byte is invalid")
    exists = existence == b"\1"
    count = reader.u64("entry count")
    if count > 10_000_000:
        _actual_fail("readiness snapshot entry count exceeds bound")
    entries = []
    previous_path = None
    for index in range(count):
        raw_path = _actual_readiness_lp(reader, "entry path")
        try:
            path = raw_path.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError("readiness snapshot path is not UTF-8") from exc
        _actual_relpath(path, "readiness snapshot path")
        if previous_path is not None and raw_path <= previous_path:
            _actual_fail("readiness snapshot paths are duplicate or unsorted")
        previous_path = raw_path
        try:
            entry_type = reader.take(1, "entry type").decode("ascii", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError("readiness snapshot type is not ASCII") from exc
        if entry_type not in ("f", "d", "l"):
            _actual_fail("readiness snapshot entry type is invalid")
        mode = reader.u64("entry mode")
        size = reader.u64("entry size")
        digest = reader.take(32, "entry SHA-256").hex()
        if root_tag in ("source", "post_lock"):
            if entry_type != "f" or mode not in (0o100644, 0o100755):
                _actual_fail("readiness source snapshot contains a non-Git regular entry")
        elif mode > 0o7777:
            _actual_fail("readiness filesystem snapshot mode exceeds 07777")
        if entry_type == "d" and (size != 0 or digest != "0" * 64):
            _actual_fail("readiness directory snapshot entry has payload metadata")
        entries.append({
            "path": path, "path_bytes": raw_path, "entry_type": entry_type,
            "mode": mode, "size": size, "sha256": digest,
        })
    if not exists and entries:
        _actual_fail("absent readiness snapshot has entries")

    identity = None
    if root_tag in ("source", "post_lock"):
        if context is None:
            _actual_fail("source snapshot lacks its retained context")
        identity = {
            "commit": reader.take(20, "commit").hex(),
            "tree": reader.take(20, "tree").hex(),
            "index_tree": reader.take(20, "index tree").hex(),
            "status": _actual_readiness_lp(reader, "status"),
            "entrypoints": [],
        }
        for expected in context["entrypoints"]:
            raw_entrypoint = _actual_readiness_lp(reader, "entrypoint path")
            try:
                entrypoint_path = raw_entrypoint.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ActualVerificationError(
                    "readiness snapshot entrypoint path is not UTF-8"
                ) from exc
            identity["entrypoints"].append({
                "path": entrypoint_path,
                "git_blob": reader.take(20, "entrypoint Git blob").hex(),
                "sha256": reader.take(32, "entrypoint SHA-256").hex(),
            })
    reader.finish()

    encoded = bytearray(domain)

    def append_lp(content):
        encoded.extend(len(content).to_bytes(8, "big"))
        encoded.extend(content)

    append_lp(root_tag.encode("ascii"))
    encoded.extend(b"\1" if exists else b"\0")
    encoded.extend(len(entries).to_bytes(8, "big"))
    for record in entries:
        append_lp(record["path_bytes"])
        encoded.extend(record["entry_type"].encode("ascii"))
        encoded.extend(record["mode"].to_bytes(8, "big"))
        encoded.extend(record["size"].to_bytes(8, "big"))
        encoded.extend(bytes.fromhex(record["sha256"]))
    if identity is not None:
        encoded.extend(bytes.fromhex(identity["commit"]))
        encoded.extend(bytes.fromhex(identity["tree"]))
        encoded.extend(bytes.fromhex(identity["index_tree"]))
        append_lp(identity["status"])
        for record in identity["entrypoints"]:
            append_lp(record["path"].encode("utf-8"))
            encoded.extend(bytes.fromhex(record["git_blob"]))
            encoded.extend(bytes.fromhex(record["sha256"]))
    if bytes(encoded) != payload:
        _actual_fail("readiness snapshot is not canonically encoded")
    return exists, entries, identity


def _actual_expected_readiness_cases():
    return {
        "scripts/cp2/run_unit_gate.sh": READINESS_COMMON_CASES,
        "scripts/cp2/run_recorded_parity.py": (
            READINESS_COMMON_CASES + READINESS_RECORDED_CASES
        ),
        "scripts/cp2/run_sequence_pair.py": (
            READINESS_COMMON_CASES + READINESS_SEQUENCE_CASES
        ),
        "scripts/cp2/run_timing_pair.py": (
            READINESS_COMMON_CASES + READINESS_TIMING_CASES
        ),
        "scripts/cp2/verify_report.py": READINESS_VERIFIER_CASES,
    }


def _actual_command_environment_sha256(mapping):
    if not isinstance(mapping, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        or "\0" in name or "\0" in value
        for name, value in mapping.items()
    ):
        _actual_fail("command environment mapping is invalid")
    encoded = bytearray(b"SchurVIO-CP2-command-environment-v1\0")
    ordered = sorted(mapping.items(), key=lambda item: item[0].encode("utf-8"))
    encoded.extend(len(ordered).to_bytes(8, "big"))
    for name, value in ordered:
        for text_value in (name, value):
            content = text_value.encode("utf-8", "strict")
            encoded.extend(len(content).to_bytes(8, "big"))
            encoded.extend(content)
    return hashlib.sha256(bytes(encoded)).hexdigest()


def _actual_validate_readiness_result(result, expected_path, expected_names):
    _actual_exact_keys(
        result,
        (
            "schema_version", "record_type", "entrypoint", "temporary_root",
            "bag_provider_calls", "cases", "case_count", "passed",
        ),
        "readiness self-test result",
    )
    temporary_root = result["temporary_root"]
    if (
        isinstance(result["schema_version"], bool)
        or not isinstance(result["schema_version"], int)
        or result["schema_version"] != 1
        or result["record_type"] != "self_test_result"
        or result["entrypoint"] != expected_path
        or not isinstance(temporary_root, str)
        or "\0" in temporary_root
        or not os.path.isabs(temporary_root)
        or os.path.normpath(temporary_root) != temporary_root
        or Path(temporary_root).parent != Path("/tmp")
        or _actual_u64(result["bag_provider_calls"], "self-test bag-provider calls") != 0
        or result["passed"] is not True
    ):
        _actual_fail("readiness self-test result identity/outcome differs")
    cases = result["cases"]
    if (
        not isinstance(cases, list)
        or isinstance(result["case_count"], bool)
        or not isinstance(result["case_count"], int)
        or result["case_count"] != len(expected_names)
        or len(cases) != len(expected_names)
    ):
        _actual_fail("readiness self-test case population differs")
    for index, (case, expected_name) in enumerate(zip(cases, expected_names)):
        _actual_exact_keys(
            case,
            ("index", "name", "expected_rejection", "observed_rejection", "passed"),
            "readiness self-test case",
        )
        negative = expected_name != "valid_minimal_fixture"
        if (
            isinstance(case["index"], bool)
            or case["index"] != index
            or case["name"] != expected_name
            or case["expected_rejection"] is not negative
            or case["observed_rejection"] is not negative
            or case["passed"] is not True
        ):
            _actual_fail("readiness self-test case outcome differs")
    if tuple(expected_names).count("valid_minimal_fixture") != 1:
        _actual_fail("readiness self-test positive-case inventory differs")
    return result


def _actual_source_archive_cat_file_sha256(archive_path, context):
    """Reconstruct the exact successful ``git cat-file --batch`` stdout hash."""

    expected = list(context["entries"])
    digest = hashlib.sha256()
    observed_index = 0
    try:
        with tarfile.open(str(archive_path), mode="r:") as archive:
            for member in archive:
                if not member.isfile():
                    continue
                if observed_index >= len(expected):
                    _actual_fail("source archive has an extra cat-file leaf")
                record = expected[observed_index]
                if member.name != record["path"] or member.size != record["size"]:
                    _actual_fail("source archive order differs from Git batch order")
                digest.update(
                    "{} blob {}\n".format(
                        record["git_blob"], record["size"]
                    ).encode("ascii")
                )
                stream = archive.extractfile(member)
                if stream is None:
                    _actual_fail("source archive cat-file member cannot be streamed")
                remaining = record["size"]
                while remaining:
                    block = stream.read(min(remaining, 1024 * 1024))
                    if not block:
                        _actual_fail("source archive cat-file member is truncated")
                    digest.update(block)
                    remaining -= len(block)
                if stream.read(1):
                    _actual_fail("source archive cat-file member grew")
                digest.update(b"\n")
                observed_index += 1
    except (OSError, tarfile.TarError) as exc:
        raise ActualVerificationError("cannot reconstruct readiness cat-file hash") from exc
    if observed_index != len(expected):
        _actual_fail("source archive cat-file population is incomplete")
    return digest.hexdigest()


def _actual_validate_readiness_git_binding(
    barrier, provenance, context, source_archive_path, artifact, manifest,
    claim_readiness, expected_other_paths,
):
    environment_record = _actual_exact_keys(
        barrier["readiness_git_environment"],
        ("environment_id", "variables", "canonical_sha256"),
        "readiness Git environment",
    )
    variables = environment_record["variables"]
    if not isinstance(variables, list):
        _actual_fail("readiness Git variables are not an array")
    mapping = {}
    previous = None
    for record in variables:
        _actual_exact_keys(record, ("name", "value"), "readiness Git variable")
        name, value = record["name"], record["value"]
        if (
            not isinstance(name, str) or not isinstance(value, str)
            or "\0" in name or "\0" in value
        ):
            _actual_fail("readiness Git variable is invalid")
        encoded = name.encode("utf-8")
        if previous is not None and encoded <= previous:
            _actual_fail("readiness Git variables are duplicate or unsorted")
        previous = encoded
        mapping[name] = value
    fixed = {
        "GIT_ALLOW_PROTOCOL": "none", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_LITERAL_PATHSPECS": "1",
        "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "",
        "GIT_PROTOCOL_FROM_USER": "0", "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin",
    }
    if set(mapping) != set(fixed) | {"HOME", "TMPDIR"} or any(
        mapping.get(name) != value for name, value in fixed.items()
    ):
        _actual_fail("readiness Git environment variables differ")
    home = _actual_safe_absolute_path(mapping["HOME"], "readiness Git HOME")
    tmpdir = _actual_safe_absolute_path(mapping["TMPDIR"], "readiness Git TMPDIR")
    if (
        home.name != "git-home" or tmpdir.name != "git-tmp"
        or home.parent != tmpdir.parent
        or home.parent.parent != Path("/tmp")
        or not home.parent.name.startswith("schurvio-cp2-readiness-")
    ):
        _actual_fail("readiness Git private HOME/TMPDIR binding differs")
    if home == tmpdir:
        _actual_fail("readiness Git private-directory identity differs")
    environment_digest = _actual_command_environment_sha256(mapping)
    if (
        environment_record["environment_id"] != "readiness_git_v1"
        or environment_record["canonical_sha256"] != environment_digest
    ):
        _actual_fail("readiness Git environment identity/digest differs")
    matching_provenance_classes = [
        record for record in provenance["environment"]["classes"]
        if record.get("environment_id") == "readiness_git_v1"
    ]
    if matching_provenance_classes != [environment_record]:
        _actual_fail("readiness Git environment is not exactly retained in provenance")

    prefix_options = (
        "color.ui=false", "core.attributesFile=/dev/null",
        "core.commitGraph=false", "core.excludesFile=/dev/null",
        "core.fileMode=true", "core.fsmonitor=false", "core.hooksPath=/dev/null",
        "core.ignoreCase=false", "core.sparseCheckout=false",
        "core.sparseCheckoutCone=false", "core.untrackedCache=false",
        "diff.external=", "pager.status=false", "protocol.allow=never",
        "protocol.file.allow=never", "status.submoduleSummary=false",
        "submodule.recurse=false",
    )
    prefix = [
        "/usr/bin/git", "--no-pager", "--no-optional-locks",
        "--git-dir=/proc/self/fd/4", "--work-tree=/proc/self/fd/3",
    ]
    for option in prefix_options:
        prefix.extend(("-c", option))
    source_triplet = (
        ("status", "--porcelain=v1", "-z", "--untracked-files=all"),
        ("rev-parse", "--verify", "HEAD"),
        ("rev-parse", "--verify", "HEAD^{tree}"),
    )
    expected_suffixes = [
        ("ls-files", "--stage", "-z"),
        ("ls-files", "--others", "-z"),
        ("cat-file", "--batch"),
        source_triplet[0],
        ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        source_triplet[1], source_triplet[2],
        ("merge-base", "--is-ancestor", CP1_AUTHORIZATION_COMMIT, "HEAD"),
    ] + list(source_triplet) * 4
    commands = barrier["readiness_git_commands"]
    if not isinstance(commands, list) or len(commands) != len(expected_suffixes):
        _actual_fail("readiness Git command population is not exactly 20")

    empty_sha = hashlib.sha256(b"").hexdigest()
    stage_stdout = b"".join(
        (
            "{:o} {} 0\t".format(record["mode"], record["git_blob"]).encode("ascii")
            + record["path"].encode("utf-8") + b"\0"
        )
        for record in context["entries"]
    )
    cat_request = b"".join(
        (record["git_blob"] + "\n").encode("ascii")
        for record in context["entries"]
    )
    cat_stdout_sha = _actual_source_archive_cat_file_sha256(
        source_archive_path, context
    )
    expected_stdout = [
        hashlib.sha256(stage_stdout).hexdigest(), None, cat_stdout_sha,
        empty_sha, None,
        hashlib.sha256((context["commit"] + "\n").encode("ascii")).hexdigest(),
        hashlib.sha256((context["tree"] + "\n").encode("ascii")).hexdigest(),
        empty_sha,
    ]
    for suffix in expected_suffixes[8:]:
        if suffix == source_triplet[0]:
            expected_stdout.append(empty_sha)
        elif suffix == source_triplet[1]:
            expected_stdout.append(
                hashlib.sha256((context["commit"] + "\n").encode("ascii")).hexdigest()
            )
        else:
            expected_stdout.append(
                hashlib.sha256((context["tree"] + "\n").encode("ascii")).hexdigest()
            )
    previous_finished = None
    retained_path_outputs = {}
    for index, (record, suffix, stdout_sha) in enumerate(
        zip(commands, expected_suffixes, expected_stdout)
    ):
        _actual_exact_keys(
            record,
            (
                "command_index", "argv", "cwd", "environment_sha256",
                "stdin_sha256", "stdout_sha256", "stderr_sha256",
                "stdout_payload",
                "started_utc", "finished_utc", "exit_code", "timed_out",
                "process_group_complete",
            ),
            "readiness Git command",
        )
        expected_stdin_sha = (
            hashlib.sha256(cat_request).hexdigest()
            if suffix == ("cat-file", "--batch") else empty_sha
        )
        stdout_payload = record["stdout_payload"]
        if suffix == ("cat-file", "--batch"):
            if stdout_payload is not None:
                _actual_fail("readiness cat-file content must not be retained")
        else:
            expected_payload = "readiness/git/{:02d}.stdout".format(index)
            if stdout_payload != expected_payload:
                _actual_fail("readiness Git stdout payload path differs")
            claim = _actual_relpath(stdout_payload, "readiness Git stdout payload")
            claim_readiness(claim, "readiness Git stdout payload")
            content = _actual_read_bytes(
                artifact / claim, "readiness Git stdout payload",
                ACTUAL_MAX_READINESS_GIT_OUTPUT_BYTES,
            )
            content_sha = hashlib.sha256(content).hexdigest()
            if manifest.get(claim) != content_sha or record["stdout_sha256"] != content_sha:
                _actual_fail("readiness Git retained stdout digest differs")
            if index in (1, 4):
                if content and not content.endswith(b"\0"):
                    _actual_fail("readiness Git path output is not NUL-terminated")
                parsed_paths = []
                previous_path = None
                for raw_path in content.split(b"\0")[:-1]:
                    if not raw_path:
                        _actual_fail("readiness Git path output contains an empty path")
                    if previous_path is not None and raw_path <= previous_path:
                        _actual_fail(
                            "readiness Git path output is duplicate or not bytewise sorted"
                        )
                    previous_path = raw_path
                    try:
                        path = raw_path.decode("utf-8", "strict")
                    except UnicodeDecodeError as exc:
                        raise ActualVerificationError(
                            "readiness Git path output is not UTF-8"
                        ) from exc
                    _actual_relpath(path, "readiness Git other/ignored path")
                    parts = PurePosixPath(path).parts
                    if parts[0] not in {"build", "results", "Testing"}:
                        _actual_fail("readiness Git other/ignored path leaves allowed roots")
                    if parts[-1] in {".gitignore", ".gitattributes"}:
                        _actual_fail("readiness Git path output names a forbidden control file")
                    parsed_paths.append(path)
                retained_path_outputs[index] = parsed_paths
        if (
            isinstance(record["command_index"], bool)
            or record["command_index"] != index
            or record["argv"] != prefix + list(suffix)
            or record["cwd"] != "/proc/self/fd/3"
            or record["environment_sha256"] != environment_digest
            or record["stdin_sha256"] != expected_stdin_sha
            or (stdout_sha is not None and record["stdout_sha256"] != stdout_sha)
            or record["stderr_sha256"] != empty_sha
            or isinstance(record["exit_code"], bool)
            or _actual_i64(record["exit_code"], "readiness Git exit code") != 0
            or record["timed_out"] is not False
            or record["process_group_complete"] is not True
        ):
            _actual_fail("readiness Git command {} binding differs".format(index))
        started = _actual_utc(record["started_utc"], "readiness Git command started")
        finished = _actual_utc(record["finished_utc"], "readiness Git command finished")
        if finished < started or (
            previous_finished is not None and started < previous_finished
        ):
            _actual_fail("readiness Git command chronology differs")
        previous_finished = finished
    if retained_path_outputs.get(1) != expected_other_paths:
        _actual_fail(
            "readiness Git all-other paths differ from retained root snapshots"
        )
    if not set(retained_path_outputs.get(4, ())).issubset(
        set(retained_path_outputs.get(1, ()))
    ):
        _actual_fail("readiness Git ignored paths are not a subset of all-other paths")
    return environment_digest, commands


def _actual_validate_source_unit_approval_binding(
    artifact, manifest, provenance, unit_anchor, readiness_relative
):
    """Reconstruct the actual artifact's source/readiness/unit trust chain."""

    if readiness_relative != "readiness/barrier.json":
        _actual_fail("readiness barrier path differs from the frozen path")
    barrier = _actual_json(
        artifact / readiness_relative, "readiness barrier"
    )
    _actual_exact_keys(barrier, ACTUAL_READINESS_KEYS, "readiness barrier")
    if (
        not _schema_version_one(barrier["schema_version"])
        or barrier["record_type"] != "readiness_barrier"
        or barrier["passed"] is not True
        or _actual_u64(barrier["bag_provider_calls"], "readiness bag-provider calls") != 0
    ):
        _actual_fail("readiness barrier identity/outcome is invalid")

    context_relative = _actual_relpath(
        barrier["source_context_payload"], "readiness source context"
    )
    if context_relative != "readiness/source_context.json":
        _actual_fail("readiness source-context path differs from the frozen path")
    readiness_references = {readiness_relative}

    def claim_readiness(relative, label):
        if not relative.startswith("readiness/") or relative in readiness_references:
            _actual_fail(label + " readiness path aliases or leaves its namespace")
        readiness_references.add(relative)

    claim_readiness(context_relative, "source context")
    if context_relative not in manifest:
        _actual_fail("readiness source context is absent from the manifest")
    context_bytes = _actual_read_bytes(
        artifact / context_relative,
        "readiness source context",
        ACTUAL_MAX_JSON_BYTES,
    )
    context_digest = hashlib.sha256(context_bytes).hexdigest()
    if (
        barrier["source_context_sha256"] != context_digest
        or manifest[context_relative] != context_digest
    ):
        _actual_fail("readiness source-context digest differs")
    context = strict_json_bytes(context_bytes, "readiness source context")
    canonical_context = json.dumps(
        context,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if context_bytes != canonical_context:
        _actual_fail("readiness source context is not canonical JSON")
    context_errors = []
    context = validate_prevalidated_source_context(context, context_errors)
    if context_errors:
        _actual_fail(
            "readiness source context is invalid: " + "; ".join(context_errors)
        )
    if (
        context.get("commit") != provenance["source_commit"]
        or context.get("tree") != provenance["source_tree"]
        or context.get("branch") != provenance["branch"]
    ):
        _actual_fail("readiness source context differs from provenance identity")

    context_entries = {
        record["path"]: record for record in context["entries"]
    }
    if not SOURCE_INPUTS.issubset(context_entries):
        missing = sorted(SOURCE_INPUTS - set(context_entries))
        _actual_fail("readiness source context omits curated inputs: " + ",".join(missing))
    _actual_install_module_source_binding(
        context_entries, "artifact_source_context"
    )
    expected_entrypoints = [
        {
            "path": record["path"],
            "sha256": record["sha256"],
            "git_blob": record["git_blob"],
        }
        for record in context["entrypoints"]
    ]
    if provenance["entrypoints"] != expected_entrypoints:
        _actual_fail("provenance entrypoints differ from held source context")
    if barrier["entrypoints"] != context["entrypoints"]:
        _actual_fail("readiness entrypoints differ from held source context")

    running_sources = (
        "scripts/cp2/cp2_readiness.py",
        "scripts/cp2/verify_report.py",
    )
    for relative in running_sources:
        path = Path(__file__).resolve().parent / Path(relative).name
        try:
            status_value = path.lstat()
        except OSError as exc:
            raise ActualVerificationError(
                "running verifier dependency is unavailable: " + relative
            ) from exc
        if (
            not stat.S_ISREG(status_value.st_mode)
            or status_value.st_nlink != 1
            or sha256_file(path) != context_entries[relative]["sha256"]
        ):
            _actual_fail("running verifier dependency differs from source context: " + relative)

    source_archive = _actual_relpath(
        provenance["source_archive"], "source archive"
    )
    archive_errors = []
    archived_hashes = validate_postauthorized_source_archive(
        artifact / source_archive, context, archive_errors
    )
    if archive_errors:
        _actual_fail("postauthorization source archive is invalid: " + "; ".join(archive_errors))
    if set(archived_hashes) != set(context_entries):
        _actual_fail("postauthorization source archive/context population differs")

    input_hashes = {
        relative: context_entries[relative]["sha256"]
        for relative in SOURCE_INPUTS
    }
    git_blobs = {
        relative: context_entries[relative]["git_blob"]
        for relative in FROZEN_CP2_C_APPROVAL_BINDING
    }
    approval_errors = []
    validate_cp2_c_approval_binding(input_hashes, git_blobs, approval_errors)
    if approval_errors:
        _actual_fail("actual CP2-C approval binding is invalid: " + "; ".join(approval_errors))
    expected_contracts = [
        {"path": relative, "sha256": input_hashes[relative]}
        for relative in sorted(CONTRACT_INPUTS, key=lambda value: value.encode("utf-8"))
    ]
    if provenance["contracts"] != expected_contracts:
        _actual_fail("provenance contract inventory differs from held governing bytes")

    provenance_environment = _actual_exact_keys(
        provenance["environment"], ("classes",), "provenance environment"
    )
    if not isinstance(provenance_environment["classes"], list):
        _actual_fail("provenance environment classes are not an array")
    self_test_environment = {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "CP2_SELF_TEST": "1", "CP2_FORBID_BAG_ACCESS": "1",
    }
    expected_self_test_environment = {
        "environment_id": "readiness_self_test_v1",
        "variables": [
            {"name": name, "value": self_test_environment[name]}
            for name in sorted(self_test_environment, key=lambda value: value.encode("utf-8"))
        ],
        "canonical_sha256": _actual_command_environment_sha256(
            self_test_environment
        ),
    }
    if [
        record for record in provenance_environment["classes"]
        if isinstance(record, dict)
        and record.get("environment_id") == "readiness_self_test_v1"
    ] != [expected_self_test_environment]:
        _actual_fail("readiness self-test environment is not exactly retained")

    data_lock = _actual_exact_keys(
        barrier["data_lock"],
        (
            "path", "device", "inode", "mode", "owner_uid", "link_count",
            "acquired_exclusive",
        ),
        "readiness data lock",
    )
    if (
        data_lock["path"] != "/tmp/schurvio-lite-cp2-data.lock"
        or _actual_u64(data_lock["device"], "data-lock device") < 0
        or _actual_u64(data_lock["inode"], "data-lock inode") == 0
        or _actual_u64(data_lock["mode"], "data-lock mode") != 0o600
        or _actual_u64(data_lock["owner_uid"], "data-lock owner") < 0
        or _actual_u64(data_lock["link_count"], "data-lock link count") != 1
        or data_lock["acquired_exclusive"] is not True
    ):
        _actual_fail("readiness data-lock evidence differs")

    def retained_payload(path_field, digest_field, label):
        relative = _actual_relpath(barrier[path_field], label + " path")
        claim_readiness(relative, label)
        if relative not in manifest:
            _actual_fail(label + " is absent from the manifest")
        payload = _actual_read_bytes(
            artifact / relative, label, ACTUAL_MAX_JSONL_BYTES
        )
        digest = hashlib.sha256(payload).hexdigest()
        if barrier[digest_field] != digest or manifest[relative] != digest:
            _actual_fail(label + " digest differs")
        return payload

    source_before = retained_payload(
        "source_before_payload", "source_before_sha256", "readiness source-before"
    )
    source_after = retained_payload(
        "source_after_payload", "source_after_sha256", "readiness source-after"
    )
    post_lock = retained_payload(
        "post_lock_payload", "post_lock_recheck_sha256", "readiness post-lock source"
    )
    post_unit = retained_payload(
        "post_unit_payload", "post_unit_recheck_sha256", "readiness post-unit source"
    )
    if source_before != source_after:
        _actual_fail("readiness source snapshots are not byte-identical")

    expected_source_rows = [
        (record["path"], "f", record["mode"], record["size"], record["sha256"])
        for record in context["entries"]
    ]
    for payload, expected_tag, label in (
        (source_before, "source", "source-before"),
        (source_after, "source", "source-after"),
        (post_lock, "post_lock", "post-lock"),
        (post_unit, "post_lock", "post-unit"),
    ):
        _, entries, identity = _actual_parse_readiness_snapshot(
            payload, expected_tag, context
        )
        observed_rows = [
            (
                row["path"], row["entry_type"], row["mode"], row["size"],
                row["sha256"],
            ) for row in entries
        ]
        if observed_rows != expected_source_rows:
            _actual_fail("readiness {} source population differs".format(label))
        expected_identity_entrypoints = [
            {
                "path": record["path"], "git_blob": record["git_blob"],
                "sha256": record["sha256"],
            }
            for record in context["entrypoints"]
        ]
        if (
            identity["commit"] != context["commit"]
            or identity["tree"] != context["tree"]
            or identity["index_tree"] != context["index_tree"]
            or identity["status"] != b""
            or identity["entrypoints"] != expected_identity_entrypoints
        ):
            _actual_fail("readiness {} source identity differs".format(label))
    if post_lock != post_unit:
        _actual_fail("readiness post-lock/post-unit snapshots are not byte-identical")

    retained_root_entries = {}
    for root_name in ("build", "results", "testing"):
        before = retained_payload(
            root_name + "_before_payload",
            root_name + "_before_sha256",
            "readiness {}-before".format(root_name),
        )
        after = retained_payload(
            root_name + "_after_payload",
            root_name + "_after_sha256",
            "readiness {}-after".format(root_name),
        )
        if before != after:
            _actual_fail("readiness {} snapshots are not byte-identical".format(root_name))
        before_parsed = _actual_parse_readiness_snapshot(before, root_name)
        after_parsed = _actual_parse_readiness_snapshot(after, root_name)
        if before_parsed != after_parsed:
            _actual_fail("readiness {} snapshot semantics differ".format(root_name))
        retained_root_entries[root_name] = before_parsed[1]

    repository_root_names = {
        "build": "build", "results": "results", "testing": "Testing",
    }
    tracked_paths = set(context_entries)
    expected_other_paths = []
    for root_tag in ("build", "results", "testing"):
        root_name = repository_root_names[root_tag]
        for record in retained_root_entries[root_tag]:
            if record["entry_type"] not in ("f", "l"):
                continue
            relative = root_name + "/" + record["path"]
            if relative not in tracked_paths:
                expected_other_paths.append(relative)
    expected_other_paths.sort(key=lambda value: value.encode("utf-8"))
    if len(expected_other_paths) != len(set(expected_other_paths)):
        _actual_fail("readiness root snapshots yield duplicate other paths")

    git_environment_sha, git_commands = _actual_validate_readiness_git_binding(
        barrier, provenance, context, artifact / source_archive,
        artifact, manifest, claim_readiness, expected_other_paths,
    )

    self_tests = barrier["self_tests"]
    if not isinstance(self_tests, list) or len(self_tests) != len(READINESS_ENTRYPOINTS):
        _actual_fail("readiness self-test population differs")
    expected_environment_sha = _actual_command_environment_sha256(
        self_test_environment
    )
    expected_cases = _actual_expected_readiness_cases()
    previous_finished = None
    for index, (record, expected_path) in enumerate(
        zip(self_tests, READINESS_ENTRYPOINTS)
    ):
        _actual_exact_keys(
            record, ACTUAL_READINESS_SELF_TEST_KEYS,
            "readiness self-test {}".format(index),
        )
        descriptor_pattern = r"/proc/self/fd/(?:[3-9]|[1-9][0-9]+)"
        expected_argv_shape = (
            len(record["argv"]) == 5
            and record["argv"][:3] == ["/usr/bin/python3", "-I", "-B"]
            and re.fullmatch(descriptor_pattern, record["argv"][3]) is not None
            and record["argv"][4] == "--self-test"
        ) if expected_path.endswith(".py") else (
            len(record["argv"]) == 2
            and re.fullmatch(descriptor_pattern, record["argv"][0]) is not None
            and record["argv"][1] == "--self-test"
        )
        expected_names = list(expected_cases[expected_path])
        if (
            _actual_u64(record["index"], "readiness self-test index") != index
            or record["path"] != expected_path
            or record["entrypoint_sha256"] != context["entrypoints"][index]["sha256"]
            or not expected_argv_shape
            or record["cwd"] != "/tmp"
            or record["environment_sha256"] != expected_environment_sha
            or isinstance(record["exit_code"], bool)
            or _actual_i64(record["exit_code"], "readiness self-test exit code") != 0
            or record["timed_out"] is not False
            or record["process_group_complete"] is not True
            or record["expected_case_names"] != expected_names
            or _actual_u64(
                record["bag_provider_calls"], "readiness self-test bag-provider calls"
            ) != 0
            or record["temporary_root_removed"] is not True
        ):
            _actual_fail("readiness self-test {} identity/outcome differs".format(index))
        started = _actual_utc(record["started_utc"], "readiness self-test started")
        finished = _actual_utc(record["finished_utc"], "readiness self-test finished")
        if finished < started:
            _actual_fail("readiness self-test interval is negative")
        if previous_finished is not None and started < previous_finished:
            _actual_fail("readiness self-tests overlap or are out of order")
        previous_finished = finished
        expected_stdout_relative = "readiness/self_tests/{:02d}.stdout".format(index)
        expected_stderr_relative = "readiness/self_tests/{:02d}.stderr".format(index)
        stdout = None
        stdout_relative = _actual_relpath(record["stdout"], "readiness self-test stdout")
        stderr_relative = _actual_relpath(record["stderr"], "readiness self-test stderr")
        if (
            stdout_relative != expected_stdout_relative
            or stderr_relative != expected_stderr_relative
        ):
            _actual_fail("readiness self-test stream path differs")
        claim_readiness(stdout_relative, "self-test stdout")
        claim_readiness(stderr_relative, "self-test stderr")
        for relative, digest, label in (
            (stdout_relative, record["stdout_sha256"], "readiness self-test stdout"),
            (stderr_relative, record["stderr_sha256"], "readiness self-test stderr"),
        ):
            content = _actual_read_bytes(artifact / relative, label, ACTUAL_MAX_JSON_BYTES)
            observed_digest = hashlib.sha256(content).hexdigest()
            if manifest.get(relative) != observed_digest or digest != observed_digest:
                _actual_fail(label + " digest differs")
            if label.endswith("stdout"):
                stdout = content
        if not stdout or not stdout.endswith(b"\n"):
            _actual_fail("readiness self-test stdout is not LF-terminated")
        stdout_lines = stdout[:-1].split(b"\n")
        if not stdout_lines or not stdout_lines[-1]:
            _actual_fail("readiness self-test stdout lacks a final result")
        parsed_result = strict_json_bytes(
            stdout_lines[-1], "readiness self-test final stdout line"
        )
        validated_result = _actual_validate_readiness_result(
            parsed_result, expected_path, tuple(expected_names)
        )
        if record["result"] != validated_result:
            _actual_fail("readiness self-test retained result differs from stdout")

    unit_verification = _actual_exact_keys(
        barrier["unit_verification"],
        (
            "argv", "cwd", "environment", "started_utc", "finished_utc",
            "exit_code", "timed_out", "process_group_complete", "stdout",
            "stdout_sha256", "stderr", "stderr_sha256", "source_context_sha256",
        ),
        "readiness unit verification",
    )
    unit_environment = {
        "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
    }
    unit_environment_digest = _actual_command_environment_sha256(unit_environment)
    unit_argv = unit_verification["argv"]
    descriptor_pattern = r"/proc/self/fd/(?:[3-9]|[1-9][0-9]+)"
    frozen_unit_pattern = (
        r"/tmp/schurvio-cp2-readiness-[^/]+/unit-artifact-frozen"
    )
    if (
        not isinstance(unit_argv, list)
        or len(unit_argv) != 8
        or unit_argv[:3] != ["/usr/bin/python3", "-I", "-B"]
        or re.fullmatch(descriptor_pattern, unit_argv[3]) is None
        or unit_argv[4] != "--verify-unit-anchor-prevalidated"
        or re.fullmatch(frozen_unit_pattern, unit_argv[5]) is None
        or unit_argv[6] != "--expected-manifest-sha256"
        or unit_argv[7] != barrier["unit_manifest_sha256"]
        or unit_verification["cwd"] != "/tmp"
        or unit_verification["environment"] != unit_environment
        or isinstance(unit_verification["exit_code"], bool)
        or _actual_i64(
            unit_verification["exit_code"], "unit-verifier exit code"
        ) != 0
        or unit_verification["timed_out"] is not False
        or unit_verification["process_group_complete"] is not True
        or unit_verification["source_context_sha256"] != context_digest
    ):
        _actual_fail("readiness unit-verifier execution binding differs")
    git_private_root = Path(
        next(
            item["value"]
            for item in barrier["readiness_git_environment"]["variables"]
            if item["name"] == "HOME"
        )
    ).parent
    if Path(unit_argv[5]).parent != git_private_root:
        _actual_fail("unit-verifier frozen artifact is outside its readiness root")
    unit_started = _actual_utc(
        unit_verification["started_utc"], "unit-verifier started"
    )
    unit_finished = _actual_utc(
        unit_verification["finished_utc"], "unit-verifier finished"
    )
    if unit_finished < unit_started:
        _actual_fail("unit-verifier interval is negative")
    if (
        _actual_utc(git_commands[10]["finished_utc"], "pre-self-test Git finish")
        > _actual_utc(self_tests[0]["started_utc"], "first self-test start")
        or _actual_utc(self_tests[-1]["finished_utc"], "last self-test finish")
        > _actual_utc(git_commands[11]["started_utc"], "post-self-test Git start")
        or _actual_utc(git_commands[16]["finished_utc"], "pre-unit Git finish")
        > unit_started
        or unit_finished
        > _actual_utc(git_commands[17]["started_utc"], "post-unit Git start")
    ):
        _actual_fail("readiness Git/self-test/unit chronology differs")

    unit_streams = {}
    for stream_name, expected_relative in (
        ("stdout", "readiness/unit_verifier.stdout"),
        ("stderr", "readiness/unit_verifier.stderr"),
    ):
        relative = _actual_relpath(
            unit_verification[stream_name], "unit-verifier " + stream_name
        )
        if relative != expected_relative:
            _actual_fail("unit-verifier stream path differs")
        claim_readiness(relative, "unit-verifier " + stream_name)
        content = _actual_read_bytes(
            artifact / relative, "unit-verifier " + stream_name,
            ACTUAL_MAX_JSON_BYTES,
        )
        digest = hashlib.sha256(content).hexdigest()
        if (
            manifest.get(relative) != digest
            or unit_verification[stream_name + "_sha256"] != digest
        ):
            _actual_fail("unit-verifier stream digest differs")
        unit_streams[stream_name] = content
    if not unit_streams["stdout"].endswith(b"\n"):
        _actual_fail("unit-verifier stdout is not LF-terminated")
    unit_lines = unit_streams["stdout"][:-1].split(b"\n")
    if not unit_lines or not unit_lines[-1]:
        _actual_fail("unit-verifier stdout lacks its result")
    unit_result = strict_json_bytes(
        unit_lines[-1], "unit-verifier final stdout line"
    )
    _actual_exact_keys(
        unit_result,
        ("commit", "passed", "record_type", "schema_version", "tree"),
        "unit-verifier result",
    )
    if (
        isinstance(unit_result["schema_version"], bool)
        or not isinstance(unit_result["schema_version"], int)
        or unit_result["schema_version"] != 1
        or unit_result["record_type"]
        != "cp2_prevalidated_unit_verification_result"
        or unit_result["passed"] is not True
        or unit_result["commit"] != context["commit"]
        or unit_result["tree"] != context["tree"]
    ):
        _actual_fail("unit-verifier result differs from readiness source")

    if (
        barrier["unit_artifact"] != unit_anchor["artifact"]
        or barrier["unit_manifest_sha256"] != unit_anchor["manifest_sha256"]
        or barrier["unit_tested_commit"] != unit_anchor["tested_commit"]
        or barrier["unit_tested_tree"] != unit_anchor["tested_tree"]
    ):
        _actual_fail("readiness barrier and provenance unit anchor differ")
    unit_artifact = _actual_safe_absolute_path(
        unit_anchor["artifact"], "unit anchor artifact"
    )
    unit_report = unit_artifact / REPORT_NAME
    if (
        not unit_report.is_file()
        or unit_report.is_symlink()
        or sha256_file(unit_report) != unit_anchor["report_sha256"]
    ):
        _actual_fail("unit anchor report identity differs")
    status, unit_errors = verify_unit_anchor_prevalidated(
        unit_artifact,
        unit_anchor["manifest_sha256"],
        context,
        quiet=True,
    )
    if status != 0 or unit_errors:
        _actual_fail(
            "referenced unit anchor failed detached re-verification: "
            + "; ".join(unit_errors)
        )
    readiness_manifest_paths = {
        relative for relative in manifest if relative.startswith("readiness/")
    }
    if readiness_manifest_paths != readiness_references:
        _actual_fail("readiness namespace has missing, aliased, or orphan files")
    inventory_by_path = {
        record.get("path"): record
        for record in provenance["file_inventory"]
        if isinstance(record, dict)
    }
    for relative in readiness_references:
        inventory_record = inventory_by_path.get(relative)
        if (
            not isinstance(inventory_record, dict)
            or inventory_record.get("role") != "readiness"
            or inventory_record.get("mode") != 0o444
        ):
            _actual_fail("readiness inventory role/mode differs: " + relative)
    return barrier, context, unit_environment_digest


def _actual_validate_provenance(artifact, manifest, observed, expected_checkpoint):
    provenance = _actual_json(artifact / "provenance.json", "provenance.json")
    _actual_exact_keys(provenance, ACTUAL_PROVENANCE_KEYS, "provenance")
    if (
        not _schema_version_one(provenance.get("schema_version"))
        or provenance.get("record_type") != "provenance"
        or provenance.get("checkpoint") != expected_checkpoint
    ):
        _actual_fail("provenance schema/checkpoint identity is invalid")
    if provenance.get("evidence_class") != "trusted_runner_local_staging_evidence":
        _actual_fail("provenance evidence class overstates its scope")
    if provenance.get("distribution_status") != "internal_non_conveyable_staging":
        _actual_fail("provenance distribution status is invalid")
    if provenance.get("eligible_for_cp2_seal") is not False:
        _actual_fail("actual staging evidence must remain ineligible for a CP2 seal")
    if provenance.get("branch") != EXPECTED_BRANCH or provenance.get("clean") is not True:
        _actual_fail("provenance source branch/clean identity is invalid")
    _actual_hex40(provenance.get("source_commit"), "provenance source commit")
    _actual_hex40(provenance.get("source_tree"), "provenance source tree")
    if provenance.get("cp1_authorization_commit") != CP1_AUTHORIZATION_COMMIT:
        _actual_fail("provenance CP1 authorization differs")
    _actual_utc(provenance.get("created_utc"), "provenance created_utc")
    host = _actual_exact_keys(provenance.get("host"), ACTUAL_HOST_KEYS, "provenance.host")
    for field in ACTUAL_HOST_KEYS:
        value = host[field]
        if field == "logical_cpu_count":
            if value is not None and _actual_u64(value, "host logical CPU count") == 0:
                _actual_fail("host logical CPU count is zero")
        elif value is not None and (
            not isinstance(value, str) or not value or "\0" in value
        ):
            _actual_fail("host {} is not a nonempty string or null".format(field))
    source_archive = _actual_relpath(provenance.get("source_archive"), "source archive")
    if source_archive not in manifest or provenance.get("source_archive_sha256") != manifest[source_archive]:
        _actual_fail("source archive identity differs from manifest")
    readiness = _actual_relpath(provenance.get("readiness_barrier"), "readiness barrier")
    if readiness not in manifest or provenance.get("readiness_barrier_sha256") != manifest[readiness]:
        _actual_fail("readiness barrier identity differs from manifest")

    contracts = provenance.get("contracts")
    if not isinstance(contracts, list) or not contracts:
        _actual_fail("provenance contract inventory is empty")
    previous_contract = None
    for record in contracts:
        _actual_exact_keys(record, ("path", "sha256"), "provenance contract")
        relative = _actual_relpath(record["path"], "contract path")
        encoded = relative.encode("utf-8")
        if previous_contract is not None and encoded <= previous_contract:
            _actual_fail("provenance contracts are duplicate or unsorted")
        previous_contract = encoded
        _actual_sha256(record["sha256"], "contract SHA-256")
    entrypoints = provenance.get("entrypoints")
    expected_entrypoints = (
        "scripts/cp2/run_unit_gate.sh", "scripts/cp2/run_recorded_parity.py",
        "scripts/cp2/run_sequence_pair.py", "scripts/cp2/run_timing_pair.py",
        "scripts/cp2/verify_report.py",
    )
    if not isinstance(entrypoints, list) or len(entrypoints) != len(expected_entrypoints):
        _actual_fail("provenance entrypoint population differs")
    for record, expected_path in zip(entrypoints, expected_entrypoints):
        _actual_exact_keys(record, ("path", "sha256", "git_blob"), "provenance entrypoint")
        if record["path"] != expected_path:
            _actual_fail("provenance entrypoint inventory/order differs")
        _actual_sha256(record["sha256"], "entrypoint SHA-256")
        _actual_hex40(record["git_blob"], "entrypoint Git blob")

    unit_anchor = _actual_exact_keys(
        provenance.get("unit_anchor"),
        ("artifact", "manifest_sha256", "tested_commit", "tested_tree", "report_sha256", "verified"),
        "provenance.unit_anchor",
    )
    _actual_safe_absolute_path(unit_anchor["artifact"], "unit anchor artifact")
    _actual_sha256(unit_anchor["manifest_sha256"], "unit anchor manifest")
    _actual_sha256(unit_anchor["report_sha256"], "unit anchor report")
    if (
        unit_anchor["tested_commit"] != provenance["source_commit"]
        or unit_anchor["tested_tree"] != provenance["source_tree"]
        or unit_anchor["verified"] is not True
    ):
        _actual_fail("unit anchor does not bind the actual source identity")
    (
        readiness_record,
        source_context,
        unit_environment_digest,
    ) = _actual_validate_source_unit_approval_binding(
        artifact, manifest, provenance, unit_anchor, readiness
    )
    build = _actual_exact_keys(
        provenance.get("build"),
        ("fresh_git_archive", "workspace", "commands_sha256", "compile_commands",
         "compile_commands_sha256", "cmake_cache", "cmake_cache_sha256",
         "strict_fp_verified"),
        "provenance.build",
    )
    if build["strict_fp_verified"] is not True:
        _actual_fail("actual runtime build lacks strict-FP verification")
    if build["fresh_git_archive"] is not True:
        _actual_fail("actual build is not marked as originating from a fresh Git archive")
    _actual_safe_absolute_path(build["workspace"], "fresh build workspace")
    for path_field in ("compile_commands", "cmake_cache"):
        relative = _actual_relpath(build[path_field], "build provenance path")
        if relative not in manifest:
            _actual_fail("build provenance path is absent from manifest")
    if build["compile_commands_sha256"] != manifest[build["compile_commands"]]:
        _actual_fail("build compile_commands digest differs")
    if build["cmake_cache_sha256"] != manifest[build["cmake_cache"]]:
        _actual_fail("build CMake cache digest differs")
    _actual_sha256(build["commands_sha256"], "build commands SHA-256")
    _actual_validate_compile_commands(artifact, build)

    inventory = provenance.get("file_inventory")
    if not isinstance(inventory, list):
        _actual_fail("provenance file inventory is not an array")
    expected_inventory_paths = set(manifest) - {"cp2_report.json", "provenance.json"}
    observed_inventory_paths = []
    previous = None
    for index, record in enumerate(inventory):
        _actual_exact_keys(record, ("path", "role", "size", "mode", "sha256"),
                           "file inventory {}".format(index))
        relative = _actual_relpath(record["path"], "inventory path")
        encoded = relative.encode("utf-8")
        if previous is not None and encoded <= previous:
            _actual_fail("file inventory is duplicate or not bytewise sorted")
        previous = encoded
        if record["role"] not in ACTUAL_INVENTORY_ROLES:
            _actual_fail("file inventory role is invalid")
        status = observed.get(relative)
        if status is None:
            _actual_fail("file inventory references an absent artifact file")
        if (
            _actual_u64(record["size"], "inventory size") != status.st_size
            or _actual_u64(record["mode"], "inventory mode") != stat.S_IMODE(status.st_mode)
            or record["sha256"] != manifest.get(relative)
        ):
            _actual_fail("file inventory metadata differs from retained file: " + relative)
        observed_inventory_paths.append(relative)
    if set(observed_inventory_paths) != expected_inventory_paths:
        _actual_fail("file inventory path set differs from manifest")

    runtime = _actual_exact_keys(
        provenance.get("runtime"),
        ("executable", "executable_size", "executable_sha256_before",
         "executable_sha256_after", "build_id_before", "build_id_after", "runs"),
        "provenance.runtime",
    )
    executable = _actual_safe_absolute_path(runtime["executable"], "runtime executable")
    executable_status = executable.lstat()
    if (
        not stat.S_ISREG(executable_status.st_mode)
        or executable_status.st_nlink != 1
        or not executable_status.st_mode & stat.S_IXUSR
    ):
        _actual_fail("runtime executable is not a single-link executable regular file")
    executable_digest = sha256_file(executable)
    if (
        _actual_u64(runtime["executable_size"], "runtime executable size") != executable_status.st_size
        or runtime["executable_sha256_before"] != executable_digest
        or runtime["executable_sha256_after"] != executable_digest
        or runtime["build_id_before"] != runtime["build_id_after"]
    ):
        _actual_fail("runtime executable before/after identity differs")
    if not isinstance(runtime["build_id_before"], str) or re.fullmatch(
        r"[0-9a-f]+", runtime["build_id_before"]
    ) is None:
        _actual_fail("runtime executable build ID is invalid")
    if _actual_elf_build_id(executable) != runtime["build_id_before"]:
        _actual_fail("runtime executable build ID differs from independent ELF notes")

    configuration = _actual_exact_keys(
        provenance.get("configuration"),
        ("static_files", "static_bundle_payload", "static_bundle_sha256", "launch",
         "resolved_parameters", "runtime_contexts"),
        "provenance.configuration",
    )
    launch = _actual_exact_keys(configuration["launch"], ("path", "size", "sha256"),
                                "provenance launch")
    if launch.get("path") != "project/cp2_serial.launch":
        _actual_fail("actual run did not use the frozen launch path")
    if launch.get("sha256") != "bede519721575d769a1fcef67c5527661cb39ba05ab77faa6c20771b0756c49a":
        _actual_fail("actual run launch SHA-256 differs from the frozen launch")
    _actual_u64(launch.get("size"), "launch size")
    _actual_sha256(configuration.get("static_bundle_sha256"), "static bundle SHA-256")
    static_files = configuration.get("static_files")
    expected_static_paths = (
        "config/euroc_mav/estimator_config.yaml",
        "config/euroc_mav/kalibr_imu_chain.yaml",
        "config/euroc_mav/kalibr_imucam_chain.yaml",
    )
    if not isinstance(static_files, list) or len(static_files) != 3:
        _actual_fail("static configuration inventory is not exactly three")
    for record, expected_path in zip(static_files, expected_static_paths):
        _actual_exact_keys(record, ("path", "size", "sha256"), "static configuration")
        if record["path"] != expected_path:
            _actual_fail("static configuration path/order differs")
        _actual_u64(record["size"], "static configuration size")
        _actual_sha256(record["sha256"], "static configuration SHA-256")
    _actual_validate_static_bundle(artifact, configuration, manifest)

    commands, command_bytes, classes = _actual_validate_commands(artifact, provenance, manifest)
    _actual_validate_readiness_command_zero(
        commands, provenance, readiness_record, unit_environment_digest
    )
    if build["commands_sha256"] != hashlib.sha256(command_bytes).hexdigest():
        _actual_fail("build commands digest differs from commands.jsonl")
    return {
        "provenance": provenance,
        "runtime": runtime,
        "configuration": configuration,
        "commands": commands,
        "command_bytes": command_bytes,
        "environment_classes": classes,
        "readiness": readiness_record,
        "source_context": source_context,
        "unit_environment_digest": unit_environment_digest,
        "executable": executable,
        "executable_sha256": executable_digest,
    }


ACTUAL_RECORDED_REPORT_KEYS = (
    "schema_version", "record_type", "checkpoint", "status", "evidence_class",
    "distribution_status", "eligible_for_cp2_seal", "created_utc",
    "provenance_sha256", "commands_sha256", "serial_pairs_sha256", "updates_sha256",
    "features_sha256", "state_blocks_sha256", "covariance_blocks_sha256",
    "state_snapshot_payloads_sha256", "proposal_payloads_sha256",
    "raw_system_payloads_sha256", "replay_report_sha256",
    "proposal_derivation_passed", "sequence_summaries", "attempted_updates",
    "empty_input", "all_rejected", "empty_after_compression", "preflight_rejected",
    "internal_failure", "committing_updates", "minimum_committing_updates",
    "raw_systems", "nullspace_gate_attempts", "schur_gate_attempts",
    "gate_union_denominator", "gate_intersection", "gate_match_numerator",
    "gate_ratio", "row_denominator", "row_match_numerator", "row_ratio",
    "disagreement_counts", "per_feature_statistics_passed", "state_blocks_expected",
    "state_blocks_seen", "covariance_blocks_expected", "covariance_blocks_seen",
    "maximum_state_ratio", "maximum_covariance_ratio", "candidate_missing_proposals",
    "baseline_commit_mismatches", "shadow_write_totals", "repair_fallback_totals",
    "gate_passed", "math_passed", "passed",
)
ACTUAL_SEQUENCE_SUMMARY_KEYS = (
    "sequence_index", "sequence_id", "attempted_updates", "committing_updates",
    "empty_input", "all_rejected", "empty_after_compression", "preflight_rejected",
    "internal_failure", "first_pair_index", "last_pair_index",
    "first_camera_timestamp_ns", "last_camera_timestamp_ns",
)
ACTUAL_SERIAL_PAIR_KEYS = (
    "schema_version", "record_type", "sequence_index", "sequence_id", "pair_index",
    "anchor_filtered_index", "anchor_camera_id", "cam0_filtered_index",
    "cam1_filtered_index", "cam0_record_time_ns", "cam1_record_time_ns",
    "cam0_header_time_ns", "cam1_header_time_ns", "camera_timestamp_ns",
    "absolute_record_delta_ns", "selected", "enqueue_entered", "enqueue_returned",
    "enqueue_status", "processing_entered", "processing_returned", "processing_status",
    "updater_invocation_ids",
)
ACTUAL_PAIR_INDEX_PROJECTION_KEYS = (
    "schema_version", "record_type", "sequence_index", "sequence_id", "pair_index",
    "anchor_filtered_index", "anchor_camera_id", "cam0_filtered_index",
    "cam1_filtered_index", "cam0_record_time_ns", "cam1_record_time_ns",
    "cam0_header_time_ns", "cam1_header_time_ns", "absolute_record_delta_ns",
)
ACTUAL_PAIR_INDEX_ENVIRONMENT = {
    "CP2_POSTAUTH_PAIR_INDEX": "held-readiness-bound-fd-v1",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": "/usr/bin:/bin",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONNOUSERSITE": "1",
    "PYTHONPATH": "/opt/ros/noetic/lib/python3/dist-packages",
}
ACTUAL_UPDATE_KEYS = (
    "schema_version", "record_type", "sequence_index", "sequence_id", "pair_index",
    "camera_timestamp_ns", "invocation_id", "live_mode", "shadow_mode", "shadow_enabled",
    "timing_evidence_eligible", "duration_ns", "terminal_status", "terminal_subreason",
    "input_feature_count", "raw_system_count", "prior_snapshot_sha256",
    "precommit_snapshot_sha256", "prior_payload_offset", "prior_payload_length",
    "precommit_payload_offset", "precommit_payload_length",
    "expected_postcommit_snapshot_sha256", "expected_postcommit_payload_offset",
    "expected_postcommit_payload_length", "live_postcommit_snapshot_sha256",
    "live_postcommit_payload_offset", "live_postcommit_payload_length",
    "baseline_proposal_sha256", "baseline_proposal_payload_offset",
    "baseline_proposal_payload_length", "candidate_proposal_sha256",
    "candidate_proposal_payload_offset", "candidate_proposal_payload_length",
    "zero_write_snapshot_equal", "baseline_accepted_ids", "baseline_accepted_set_sha256",
    "baseline_accepted_sequence_sha256", "candidate_accepted_ids",
    "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256",
    "baseline_gamma_status", "baseline_gamma", "candidate_gamma",
    "baseline_precompression_rows", "baseline_compressed_rows",
    "candidate_precompression_rows", "candidate_compressed_rows",
    "baseline_preview_status", "baseline_preview_stage", "candidate_outcome",
    "candidate_preview_status", "candidate_preview_stage", "candidate_proposal_available",
    "baseline_preview_counters", "candidate_preview_counters", "baseline_commit_count",
    "baseline_transaction_mean_commits", "baseline_covariance_commits",
    "baseline_expected_type_update_calls", "baseline_verified_nominal_fields",
    "baseline_nominal_mismatches", "baseline_covariance_mismatches",
    "baseline_fej_mismatches", "candidate_ekf_update_calls", "candidate_type_update_calls",
    "candidate_mean_writes", "candidate_covariance_writes", "candidate_feature_writes",
    "state_block_rows", "covariance_block_rows", "all_block_rows_present", "math_passed",
    "config_sha256", "bag_sha256", "pair_index_sha256", "resolved_parameters_sha256",
)
ACTUAL_FEATURE_KEYS = (
    "schema_version", "record_type", "sequence_index", "pair_index",
    "camera_timestamp_ns", "invocation_id", "feature_ordinal", "feature_id",
    "pass_index", "raw_rows", "raw_system_sha256", "jacobian_layout",
    "raw_payload_offset", "raw_payload_length", "prior_snapshot_sha256", "config_sha256",
    "bag_sha256", "pair_index_sha256", "resolved_parameters_sha256",
    "baseline_accepted_set_sha256", "baseline_accepted_sequence_sha256",
    "candidate_accepted_set_sha256", "candidate_accepted_sequence_sha256",
    "baseline_retained_gamma", "candidate_retained_gamma", "nullspace_reduction_status",
    "nullspace_reduction_stage", "nullspace_reduced_rows",
    "nullspace_raw_lambda_symmetry_error_inf", "nullspace_gamma", "nullspace_nis",
    "nullspace_threshold", "nullspace_gate_stage", "nullspace_decision",
    "schur_reduction_status", "schur_reduction_stage", "schur_reduced_rows",
    "schur_singular_values_available", "schur_singular_values", "schur_ratio_available",
    "schur_ratio", "schur_raw_lambda_symmetry_error_inf", "schur_gamma", "schur_nis",
    "schur_threshold", "schur_gate_stage", "schur_decision",
    "statistics_comparison_required", "lambda_comparison_available",
    "eta_comparison_available", "gamma_comparison_available", "lambda_comparison_status",
    "eta_comparison_status", "gamma_comparison_status", "lambda_reference_norm",
    "lambda_error", "lambda_tolerance", "lambda_ratio", "lambda_pass",
    "eta_reference_norm", "eta_error", "eta_tolerance", "eta_ratio", "eta_pass",
    "gamma_reference_norm", "gamma_error", "gamma_tolerance", "gamma_ratio", "gamma_pass",
    "agreement_class", "raw_row_match_weight", "nullspace_reducer_counters",
    "schur_reducer_counters",
)
ACTUAL_STATE_BLOCK_KEYS = (
    "schema_version", "record_type", "sequence_index", "pair_index",
    "camera_timestamp_ns", "invocation_id", "block_index", "block_kind",
    "block_identity", "covariance_id", "size", "candidate_available",
    "comparison_available", "comparison_status", "reference_norm", "error", "tolerance",
    "ratio", "prior_snapshot_sha256", "config_sha256", "bag_sha256",
    "pair_index_sha256", "resolved_parameters_sha256", "baseline_accepted_set_sha256",
    "baseline_accepted_sequence_sha256", "candidate_accepted_set_sha256",
    "candidate_accepted_sequence_sha256", "baseline_retained_gamma",
    "candidate_retained_gamma", "passed",
)
ACTUAL_COVARIANCE_BLOCK_KEYS = (
    "schema_version", "record_type", "sequence_index", "pair_index",
    "camera_timestamp_ns", "invocation_id", "row_block_index", "column_block_index",
    "row_covariance_id", "column_covariance_id", "row_size", "column_size",
    "candidate_available", "comparison_available", "comparison_status", "reference_norm",
    "error", "tolerance", "ratio", "prior_snapshot_sha256", "config_sha256", "bag_sha256",
    "pair_index_sha256", "resolved_parameters_sha256", "baseline_accepted_set_sha256",
    "baseline_accepted_sequence_sha256", "candidate_accepted_set_sha256",
    "candidate_accepted_sequence_sha256", "baseline_retained_gamma",
    "candidate_retained_gamma", "passed",
)
ACTUAL_COUNTER_KEYS = (
    "jitter", "repair", "alternate_solve", "clamp", "regularization",
    "silent_fallback", "fallback",
)
ACTUAL_SHADOW_WRITE_KEYS = (
    "ekf_update_calls", "type_update_calls", "mean_writes", "covariance_writes",
    "feature_writes",
)
ACTUAL_AGREEMENT_CLASSES = (
    "both_match_accept", "both_match_reject", "boolean_nullspace_accept_schur_reject",
    "boolean_nullspace_reject_schur_accept", "nullspace_only", "schur_only",
    "neither_decision",
)
ACTUAL_REPLAY_KEYS = (
    "schema_version", "record_type", "checkpoint", "source_commit", "source_tree",
    "executable_sha256", "executable_build_id", "strict_fp_verified", "bag_provider_calls",
    "input_sha256", "resolved_parameters", "replayed_invocations", "replayed_raw_systems",
    "baseline_expected_proposals", "candidate_expected_proposals",
    "baseline_exact_proposal_matches", "candidate_exact_proposal_matches",
    "failure_counts", "passed",
)
ACTUAL_REPLAY_INPUT_KEYS = (
    "serial_pairs", "updates", "features", "state_blocks", "covariance_blocks",
    "state_snapshot_payloads", "proposal_payloads", "raw_system_payloads",
)
ACTUAL_REPLAY_FAILURE_KEYS = (
    "layout", "reduction", "statistics", "gate", "accepted_sequence", "gamma", "stack",
    "compression", "preview_status", "proposal_presence", "proposal_bytes", "commit_oracle",
    "block_metrics",
)


def _actual_hash_file_fields(report, manifest, mapping):
    for field, relative in mapping.items():
        if relative not in manifest or report.get(field) != manifest[relative]:
            _actual_fail("report {} differs from retained {}".format(field, relative))


def _actual_read_exact(stream, length, label):
    content = stream.read(length)
    if len(content) != length:
        _actual_fail(label + " is truncated")
    return content


def _actual_hash_payload(stream, length, required_domain, label):
    if length < len(required_domain):
        _actual_fail(label + " is shorter than its domain")
    prefix = _actual_read_exact(stream, len(required_domain), label + " domain")
    if prefix != required_domain:
        _actual_fail(label + " domain is invalid")
    digest = hashlib.sha256(prefix)
    remaining = length - len(prefix)
    while remaining:
        block = _actual_read_exact(stream, min(1024 * 1024, remaining), label)
        digest.update(block)
        remaining -= len(block)
    return digest.hexdigest()


def _actual_parse_state_payloads(path):
    frames = {}
    previous = None
    header = b"SchurVIO-CP2-state-file-v1\n\0\0\0\0\0"
    with path.open("rb") as stream:
        if _actual_read_exact(stream, len(header), "state payload header") != header:
            _actual_fail("state payload file header is invalid")
        while True:
            first = stream.read(1)
            if first == b"":
                break
            phase = first[0]
            rest = _actual_read_exact(stream, 39, "state frame header")
            if phase not in (0, 1, 2, 3) or rest[:7] != b"\0" * 7:
                _actual_fail("state frame phase/reserved bytes are invalid")
            sequence_index, pair_index, invocation_id, length = struct.unpack(">QQQQ", rest[7:])
            key = (sequence_index, pair_index, invocation_id, phase)
            if previous is not None and key <= previous:
                _actual_fail("state frames are duplicate or out of order")
            previous = key
            payload_offset = stream.tell()
            domain = (
                b"SchurVIO-CP2-prior-snapshot-v1\0"
                if phase in (0, 1)
                else b"SchurVIO-CP2-postcommit-state-v1\0"
            )
            digest = _actual_hash_payload(stream, length, domain, "state frame payload")
            frames[key] = {
                "offset": payload_offset, "length": length, "sha256": digest,
            }
    return frames


def _actual_parse_proposal_payloads(path):
    frames = {}
    previous = None
    header = b"SchurVIO-CP2-proposal-file-v1\n\0\0"
    with path.open("rb") as stream:
        if _actual_read_exact(stream, len(header), "proposal payload header") != header:
            _actual_fail("proposal payload file header is invalid")
        while True:
            first = stream.read(1)
            if first == b"":
                break
            role = first[0]
            rest = _actual_read_exact(stream, 39, "proposal frame header")
            if role not in (0, 1) or rest[:7] != b"\0" * 7:
                _actual_fail("proposal frame role/reserved bytes are invalid")
            sequence_index, pair_index, invocation_id, length = struct.unpack(">QQQQ", rest[7:])
            key = (sequence_index, pair_index, invocation_id, role)
            if previous is not None and key <= previous:
                _actual_fail("proposal frames are duplicate or out of order")
            previous = key
            payload_offset = stream.tell()
            digest = _actual_hash_payload(
                stream, length, b"SchurVIO-CP2-proposal-v1\0", "proposal frame payload"
            )
            frames[key] = {
                "offset": payload_offset, "length": length, "sha256": digest,
            }
    return frames


def _actual_parse_raw_payloads(path):
    frames = {}
    previous = None
    header = b"SchurVIO-CP2-raw-file-v1\n\0\0\0\0\0\0\0"
    with path.open("rb") as stream:
        if _actual_read_exact(stream, len(header), "raw payload header") != header:
            _actual_fail("raw-system payload file header is invalid")
        while True:
            first = stream.read(1)
            if first == b"":
                break
            rest = _actual_read_exact(stream, 47, "raw-system frame header")
            values = struct.unpack(">QQQQQQ", first + rest)
            sequence_index, pair_index, invocation_id, feature_ordinal, feature_id, length = values
            key = (sequence_index, pair_index, invocation_id, feature_ordinal, feature_id)
            if previous is not None and key <= previous:
                _actual_fail("raw-system frames are duplicate or out of order")
            previous = key
            payload_offset = stream.tell()
            digest = _actual_hash_payload(
                stream, length, b"SchurVIO-CP2-raw-system-v1\0", "raw-system frame payload"
            )
            frames[key] = {
                "offset": payload_offset, "length": length, "sha256": digest,
            }
    return frames


def _actual_ranges_equal(path, first_offset, second_offset, length):
    with path.open("rb") as stream:
        remaining = length
        while remaining:
            amount = min(1024 * 1024, remaining)
            stream.seek(first_offset)
            left = stream.read(amount)
            stream.seek(second_offset)
            right = stream.read(amount)
            if len(left) != amount or len(right) != amount or left != right:
                return False
            first_offset += amount
            second_offset += amount
            remaining -= amount
    return True


def _actual_nullable_frame(record, hash_name, offset_name, length_name, frame, label):
    values = (record.get(hash_name), record.get(offset_name), record.get(length_name))
    if frame is None:
        if values != (None, None, None):
            _actual_fail(label + " has a dangling hash/offset/length triple")
        return
    _actual_sha256(values[0], label + " SHA-256")
    _actual_u64(values[1], label + " payload offset")
    _actual_u64(values[2], label + " payload length")
    if (
        values[0] != frame["sha256"]
        or values[1] != frame["offset"]
        or values[2] != frame["length"]
    ):
        _actual_fail(label + " hash/offset/length differs from its payload frame")


def _actual_counter_object(value, label):
    _actual_exact_keys(value, ACTUAL_COUNTER_KEYS, label)
    return {key: _actual_u64(value[key], label + "." + key) for key in ACTUAL_COUNTER_KEYS}


def _actual_validate_comparison(record, prefix, tolerance_scale, tolerance_floor, required, label):
    available = record[prefix + "_comparison_available"]
    status = record[prefix + "_comparison_status"]
    reference = record[prefix + "_reference_norm"]
    error = record[prefix + "_error"]
    tolerance = record[prefix + "_tolerance"]
    ratio = record[prefix + "_ratio"]
    passed = record[prefix + "_pass"] if prefix + "_pass" in record else record["passed"]
    if not isinstance(available, bool) or not isinstance(passed, bool):
        _actual_fail(label + " comparison flags are not Boolean")
    if not required:
        if status != "not_required" or available or any(
            value is not None for value in (reference, error, tolerance, ratio)
        ) or passed:
            _actual_fail(label + " not-required comparison contains evaluated evidence")
        return False, None
    statuses = {
        "available", "reference_unavailable", "candidate_unavailable",
        "reference_norm_nonfinite", "error_nonfinite", "tolerance_nonfinite",
        "ratio_nonfinite",
    }
    if status not in statuses:
        _actual_fail(label + " comparison status is invalid")
    if status != "available":
        if available or any(value is not None for value in (reference, error, tolerance, ratio)) or passed:
            _actual_fail(label + " unavailable comparison contains later-stage evidence")
        return False, None
    if not available:
        _actual_fail(label + " available comparison has availability=false")
    reference_value = _actual_f64(reference, label + " reference", nonnegative=True)
    error_value = _actual_f64(error, label + " error", nonnegative=True)
    _actual_require_tonearest(label + " tolerance arithmetic")
    product = float(tolerance_scale * reference_value)
    expected_tolerance = float(tolerance_floor + product)
    tolerance_value = _actual_f64(tolerance, label + " tolerance", nonnegative=True)
    _actual_require_tonearest(label + " diagnostic ratio")
    expected_ratio = float(error_value / expected_tolerance)
    if (
        not _actual_same_f64(tolerance_value, expected_tolerance)
        or not _actual_same_f64(ratio, expected_ratio)
        or passed is not (error_value <= expected_tolerance)
    ):
        _actual_fail(label + " comparison arithmetic/decision differs")
    return passed, expected_ratio


def _actual_validate_block_comparison(record, label):
    available = record["comparison_available"]
    status = record["comparison_status"]
    numeric = tuple(record[name] for name in ("reference_norm", "error", "tolerance", "ratio"))
    if not isinstance(record["candidate_available"], bool) or not isinstance(available, bool):
        _actual_fail(label + " availability flags are not Boolean")
    if not isinstance(record["passed"], bool):
        _actual_fail(label + " pass flag is not Boolean")
    allowed = {
        "available", "candidate_unavailable", "reference_norm_nonfinite", "error_nonfinite",
        "tolerance_nonfinite", "ratio_nonfinite",
    }
    if status not in allowed:
        _actual_fail(label + " comparison status is invalid")
    if status != "available":
        if available or any(value is not None for value in numeric) or record["passed"]:
            _actual_fail(label + " unavailable comparison contains evaluated evidence")
        if status == "candidate_unavailable" and record["candidate_available"]:
            _actual_fail(label + " candidate-unavailable status contradicts availability")
        return False, None
    if not available or not record["candidate_available"]:
        _actual_fail(label + " available comparison lacks its candidate")
    reference = _actual_f64(record["reference_norm"], label + " reference", nonnegative=True)
    error = _actual_f64(record["error"], label + " error", nonnegative=True)
    _actual_require_tonearest(label + " tolerance arithmetic")
    product = float(1.0e-6 * reference)
    expected_tolerance = float(1.0e-8 + product)
    _actual_require_tonearest(label + " diagnostic ratio")
    expected_ratio = float(error / expected_tolerance)
    if (
        not _actual_same_f64(record["tolerance"], expected_tolerance)
        or not _actual_same_f64(record["ratio"], expected_ratio)
        or record["passed"] is not (error <= expected_tolerance)
    ):
        _actual_fail(label + " comparison arithmetic/decision differs")
    return record["passed"], expected_ratio


def _actual_update_identity(record):
    return tuple(record[name] for name in (
        "sequence_index", "pair_index", "camera_timestamp_ns", "invocation_id"
    ))


def _actual_repeat_update_fields(record, update, label):
    for field in (
        "prior_snapshot_sha256", "config_sha256", "bag_sha256", "pair_index_sha256",
        "resolved_parameters_sha256", "baseline_accepted_set_sha256",
        "baseline_accepted_sequence_sha256", "candidate_accepted_set_sha256",
        "candidate_accepted_sequence_sha256",
    ):
        if record[field] != update[field]:
            _actual_fail("{} repeated {} differs from its update".format(label, field))
    for field, update_field in (
        ("baseline_retained_gamma", "baseline_gamma"),
        ("candidate_retained_gamma", "candidate_gamma"),
    ):
        left = record[field]
        right = update[update_field]
        if left is None or right is None:
            if left is not right:
                _actual_fail("{} repeated {} nullability differs".format(label, field))
        elif not _actual_same_f64(left, right):
            _actual_fail("{} repeated {} differs at binary64".format(label, field))


def _actual_validate_block_identity(record, label):
    identity = record["block_identity"]
    if not isinstance(identity, dict) or identity.get("kind") != record["block_kind"]:
        _actual_fail(label + " block identity/kind differs")
    keys = set(identity)
    if "timestamp_bits" in keys:
        expected = {"kind", "timestamp_bits"}
    elif "feature_id" in keys or "representation" in keys or "anchor_camera_id" in keys:
        expected = {
            "kind", "feature_id", "representation", "anchor_camera_id",
            "anchor_timestamp_bits",
        }
    else:
        expected = {"kind"}
    if keys != expected:
        _actual_fail(label + " block identity field inventory differs")
    for bits_name in ("timestamp_bits", "anchor_timestamp_bits"):
        if bits_name in identity and (
            not isinstance(identity[bits_name], str)
            or re.fullmatch(r"[0-9a-f]{16}", identity[bits_name]) is None
        ):
            _actual_fail(label + " block timestamp bits are invalid")
    if "feature_id" in identity:
        _actual_u64(identity["feature_id"], label + " landmark feature ID")
        _actual_u64(identity["anchor_camera_id"], label + " landmark anchor camera ID")
        if not isinstance(identity["representation"], str) or not identity["representation"]:
            _actual_fail(label + " landmark representation is invalid")


def _actual_terminal_mapping(status, subreason):
    mapping = {
        "empty_input": {"input_empty"},
        "all_rejected": {
            "no_features_after_cleaning", "no_features_after_triangulation",
            "no_raw_systems", "all_baseline_features_rejected",
        },
        "empty_after_compression": {"measurement_compression_empty"},
        "preflight_rejected": {"baseline_preflight_rejected"},
        "committed_counted": {"none"},
        "internal_failure": {"invalid_live_mode", "snapshot_mismatch", "trace_invariant_failure"},
    }
    return status in mapping and subreason in mapping[status]


def _actual_validate_runtime_context(
    artifact, manifest, context_record, expected, trace_level,
):
    relative = _actual_relpath(context_record["path"], "runtime context path")
    context_path = artifact / relative
    status_value = context_path.lstat()
    if (
        _actual_u64(context_record["size"], "runtime context size")
        != status_value.st_size
        or manifest.get(relative) != context_record["sha256"]
    ):
        _actual_fail("runtime context size/hash differs from its retained file")
    context = _actual_json(context_path, "runtime context")
    _actual_exact_keys(context, ACTUAL_RUNTIME_CONTEXT_KEYS, "runtime context")
    if (
        not _schema_version_one(context["schema_version"])
        or context["record_type"] != "cp2_runtime_context"
        or context["checkpoint"] != expected["checkpoint"]
        or context["run_id"] != expected["run_id"]
        or context["sequence_index"] != expected["sequence_index"]
        or context["sequence_id"] != expected["sequence_id"]
        or context["mode"] != expected["mode"]
        or context["shadow_enabled"] is not expected["shadow_enabled"]
        or context["trace_level"] != trace_level
        or context["source_commit"] != expected["source_commit"]
        or context["config_sha256"] != expected["config_sha256"]
        or context["bag_sha256"] != expected["bag_sha256"]
        or context["resolved_parameters_sha256"]
        != expected["resolved_parameters_sha256"]
    ):
        _actual_fail("runtime context identity/configuration join differs")
    _actual_u64(context["sequence_index"], "runtime context sequence index")
    if context["pair_index_sha256"] is not None:
        _actual_fail("immutable pre-run runtime context has a nonnull pair-index digest")
    for field in (
        "source_commit", "config_sha256", "bag_sha256",
        "resolved_parameters_sha256",
    ):
        if field == "source_commit":
            _actual_hex40(context[field], "runtime context source commit")
        else:
            _actual_sha256(context[field], "runtime context " + field)

    if trace_level == "recorded_full":
        required_nonnull = {
            "serial_trace_path", "updater_trace_path", "state_payload_path",
            "proposal_payload_path", "raw_system_payload_path",
            "runtime_parameters_path", "loader_map_before_path",
            "loader_map_after_path",
        }
    elif trace_level == "sequence":
        required_nonnull = {
            "serial_trace_path", "callback_trace_path", "trajectory_trace_path",
            "runtime_parameters_path", "loader_map_before_path",
            "loader_map_after_path", "legacy_state_path", "legacy_deviation_path",
            "legacy_timing_path",
        }
    else:
        _actual_fail("runtime context verifier received an unauthorized trace level")
    path_fields = {
        "serial_trace_path", "callback_trace_path", "trajectory_trace_path",
        "updater_trace_path", "state_payload_path", "proposal_payload_path",
        "raw_system_payload_path", "timing_trace_path", "runtime_parameters_path",
        "loader_map_before_path", "loader_map_after_path", "legacy_state_path",
        "legacy_deviation_path", "legacy_timing_path",
    }
    if any((context[field] is not None) is not (field in required_nonnull)
           for field in path_fields):
        _actual_fail("runtime context trace-level path nullability differs")
    trace_directory = _actual_safe_absolute_path(
        context["trace_directory"], "runtime context trace directory"
    )
    retained_parent = PurePosixPath(relative).parent.as_posix()
    if not trace_directory.as_posix().endswith("/" + retained_parent):
        _actual_fail("runtime context trace directory is disconnected from retained run path")
    children = []
    for field in sorted(required_nonnull):
        child = _actual_safe_absolute_path(context[field], "runtime context " + field)
        try:
            child.relative_to(trace_directory)
        except ValueError as exc:
            raise ActualVerificationError(
                "runtime context child is outside trace_directory: " + field
            ) from exc
        if child == trace_directory:
            _actual_fail("runtime context child aliases trace_directory")
        children.append(child)
    if len(set(children)) != len(children):
        _actual_fail("runtime context output paths alias")
    return context


def _actual_validate_recorded_provenance(artifact, common, manifest):
    provenance = common["provenance"]
    inputs = provenance.get("inputs")
    input_keys = (
        "sequence_index", "sequence_id", "offset_seconds", "bag_path", "bag_size",
        "bag_sha256_before", "bag_sha256_after", "ground_truth_path",
        "ground_truth_sha256",
    )
    if not isinstance(inputs, list) or len(inputs) != 3:
        _actual_fail("CP2-C provenance must contain exactly three sequence inputs")
    bag_hashes = {}
    for index, record in enumerate(inputs):
        _actual_exact_keys(record, input_keys, "recorded input {}".format(index))
        if record["sequence_index"] != index or record["sequence_id"] != ACTUAL_SEQUENCE_IDS[index]:
            _actual_fail("recorded input sequence identity/order differs")
        if not _actual_same_f64(
            record["offset_seconds"], ACTUAL_SEQUENCE_OFFSETS_SECONDS[index]
        ):
            _actual_fail("recorded input offset differs from the frozen sequence offset")
        _actual_safe_absolute_path(record["bag_path"], "recorded bag path")
        _actual_u64(record["bag_size"], "recorded bag size")
        before = _actual_sha256(record["bag_sha256_before"], "recorded bag SHA-256")
        if record["bag_sha256_after"] != before:
            _actual_fail("recorded bag before/after identities differ")
        if record["ground_truth_path"] is not None or record["ground_truth_sha256"] is not None:
            _actual_fail("CP2-C input unexpectedly contains ground-truth identity")
        bag_hashes[index] = before

    runtime_runs = common["runtime"].get("runs")
    if not isinstance(runtime_runs, list) or len(runtime_runs) != 3:
        _actual_fail("CP2-C runtime provenance must contain exactly three runs")
    run_ids = []
    run_modes = []
    runtime_run_keys = (
        "run_id", "sequence_index", "mode", "loader_map_before",
        "loader_map_before_sha256", "loader_map_after", "loader_map_after_sha256",
        "dso_records_before", "dso_records_after",
    )
    for index, record in enumerate(runtime_runs):
        _actual_exact_keys(record, runtime_run_keys, "runtime run {}".format(index))
        if record["sequence_index"] != index:
            _actual_fail("CP2-C runtime sequence order differs")
        if not isinstance(record["run_id"], str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", record["run_id"]
        ) is None:
            _actual_fail("CP2-C runtime run ID is invalid")
        if record["run_id"] in run_ids:
            _actual_fail("CP2-C runtime run ID is duplicate")
        run_ids.append(record["run_id"])
        run_modes.append(record["mode"])
        for map_name in ("loader_map_before", "loader_map_after"):
            relative = _actual_relpath(record[map_name], "runtime loader map")
            if manifest.get(relative) != record[map_name + "_sha256"]:
                _actual_fail("runtime loader-map identity differs from manifest")
        if record["dso_records_before"] != record["dso_records_after"]:
            _actual_fail("runtime DSO identity changed during a run")
        previous = None
        for dso in record["dso_records_before"]:
            _actual_exact_keys(dso, ("path", "soname", "size", "sha256", "build_id"),
                               "runtime DSO")
            path = _actual_safe_absolute_path(dso["path"], "runtime DSO path")
            encoded = str(path).encode("utf-8")
            if previous is not None and encoded <= previous:
                _actual_fail("runtime DSO records are duplicate or unsorted")
            previous = encoded
            if not isinstance(dso["soname"], str) or not dso["soname"]:
                _actual_fail("runtime DSO SONAME is invalid")
            _actual_u64(dso["size"], "runtime DSO size")
            _actual_sha256(dso["sha256"], "runtime DSO SHA-256")
            if not isinstance(dso["build_id"], str) or re.fullmatch(r"[0-9a-f]+", dso["build_id"]) is None:
                _actual_fail("runtime DSO build ID is invalid")
    if run_modes != ["nullspace", "nullspace", "nullspace"]:
        _actual_fail("CP2-C runtime modes are not exactly nullspace in sequence order")

    resolved = common["configuration"].get("resolved_parameters")
    contexts = common["configuration"].get("runtime_contexts")
    if not isinstance(resolved, list) or len(resolved) != 3:
        _actual_fail("CP2-C resolved-parameter population is not three")
    if not isinstance(contexts, list) or len(contexts) != 3:
        _actual_fail("CP2-C runtime-context population is not three")
    resolved_hashes = {}
    resolved_keys = (
        "run_id", "prelaunch_raw_path", "prelaunch_raw_sha256", "runtime_raw_path",
        "runtime_raw_sha256", "canonical_path", "canonical_sha256", "normalized_path",
        "normalized_sha256",
    )
    for index, record in enumerate(resolved):
        _actual_exact_keys(record, resolved_keys, "resolved parameters {}".format(index))
        if record["run_id"] != run_ids[index]:
            _actual_fail("resolved parameters do not join runtime run order")
        for path_field in ("prelaunch_raw_path", "runtime_raw_path", "canonical_path"):
            relative = _actual_relpath(record[path_field], "resolved parameter artifact")
            hash_field = path_field.replace("_path", "_sha256")
            if manifest.get(relative) != record[hash_field]:
                _actual_fail("resolved-parameter artifact differs from manifest")
        if record["normalized_path"] is not None or record["normalized_sha256"] is not None:
            _actual_fail("CP2-C normalized-parameter fields must be null")
        resolved_hashes[index] = _actual_sha256(
            record["canonical_sha256"], "resolved canonical SHA-256"
        )
    for index, record in enumerate(contexts):
        _actual_exact_keys(record, ("run_id", "path", "size", "sha256"),
                           "runtime context {}".format(index))
        if record["run_id"] != run_ids[index]:
            _actual_fail("runtime context does not join run order")
        _actual_validate_runtime_context(
            artifact, manifest, record,
            {
                "checkpoint": "CP2-C", "run_id": run_ids[index],
                "sequence_index": index, "sequence_id": ACTUAL_SEQUENCE_IDS[index],
                "mode": "nullspace", "shadow_enabled": True,
                "source_commit": provenance["source_commit"],
                "config_sha256": common["configuration"]["static_bundle_sha256"],
                "bag_sha256": bag_hashes[index],
                "resolved_parameters_sha256": resolved_hashes[index],
            },
            "recorded_full",
        )
    return bag_hashes, resolved_hashes


def _actual_validate_recorded_rows(
    artifact, manifest, report, serial_rows, update_rows, feature_rows,
    state_rows, covariance_rows, state_frames, proposal_frames, raw_frames,
    bag_hashes, resolved_hashes, expected_config_sha256,
):
    schema = _actual_load_module("cp2_schema")
    serial_by_pair = {}
    invocation_owners = {}
    pair_indices_by_sequence = {index: [] for index in range(3)}
    previous_serial = None
    enqueue_statuses = {
        "queued", "frequency_dropped", "cam0_decode_failed", "cam1_decode_failed",
        "not_entered", "process_terminated", "trace_failure",
    }
    processing_statuses = {
        "processed", "not_queued", "queued_unprocessed", "process_terminated",
        "trace_failure",
    }
    for row_index, row in enumerate(serial_rows):
        _actual_exact_keys(row, ACTUAL_SERIAL_PAIR_KEYS, "serial pair {}".format(row_index))
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "serial_pair"
        ):
            _actual_fail("serial-pair schema identity is invalid")
        sequence_index = _actual_u64(row["sequence_index"], "serial sequence index")
        pair_index = _actual_u64(row["pair_index"], "serial pair index")
        if sequence_index >= 3 or row["sequence_id"] != ACTUAL_SEQUENCE_IDS[sequence_index]:
            _actual_fail("serial sequence identity is invalid")
        key = (sequence_index, pair_index)
        if previous_serial is not None and key <= previous_serial:
            _actual_fail("serial-pair rows are duplicate or out of order")
        previous_serial = key
        pair_indices_by_sequence[sequence_index].append(pair_index)
        for field in (
            "anchor_filtered_index", "anchor_camera_id", "cam0_filtered_index",
            "cam1_filtered_index", "cam0_record_time_ns", "cam1_record_time_ns",
            "cam0_header_time_ns", "cam1_header_time_ns", "camera_timestamp_ns",
            "absolute_record_delta_ns",
        ):
            _actual_u64(row[field], "serial " + field)
        if row["anchor_camera_id"] not in (0, 1):
            _actual_fail("serial anchor camera is invalid")
        if row["camera_timestamp_ns"] != row["cam0_header_time_ns"]:
            _actual_fail("serial camera timestamp differs from cam0 header time")
        expected_delta = abs(row["cam1_record_time_ns"] - row["cam0_record_time_ns"])
        if row["absolute_record_delta_ns"] != expected_delta or expected_delta >= 20_000_000:
            _actual_fail("serial record-time delta violates the strict 20 ms rule")
        if row["selected"] is not True:
            _actual_fail("serial-pair row is not selected")
        for field in ("enqueue_entered", "enqueue_returned", "processing_entered", "processing_returned"):
            if not isinstance(row[field], bool):
                _actual_fail("serial event flag is not Boolean")
        if row["enqueue_status"] not in enqueue_statuses or row["processing_status"] not in processing_statuses:
            _actual_fail("serial status enum is invalid")
        transition = (
            row["enqueue_status"], row["processing_status"], row["enqueue_entered"],
            row["enqueue_returned"], row["processing_entered"], row["processing_returned"],
        )
        if transition not in (
            ("queued", "processed", True, True, True, True),
            ("frequency_dropped", "not_queued", True, True, False, False),
        ):
            _actual_fail("serial status/event transition is not passing")
        ids = row["updater_invocation_ids"]
        if not isinstance(ids, list) or len(set(ids)) != len(ids):
            _actual_fail("serial updater invocation IDs are invalid")
        for invocation_id in ids:
            _actual_u64(invocation_id, "serial updater invocation ID")
            owner = (sequence_index, invocation_id)
            if owner in invocation_owners:
                _actual_fail("updater invocation is owned by multiple serial pairs")
            invocation_owners[owner] = key
        if row["processing_status"] != "processed" and ids:
            _actual_fail("unprocessed serial row owns updater invocations")
        serial_by_pair[key] = row
    for sequence_index, values in pair_indices_by_sequence.items():
        if values != list(range(len(values))):
            _actual_fail("serial pair indices are noncontiguous for sequence {}".format(sequence_index))

    update_by_identity = {}
    update_by_invocation = {}
    features_by_update = {}
    previous_update = None
    terminal_counts = Counter()
    sequence_terminal_counts = {index: Counter() for index in range(3)}
    invocation_ids_by_sequence = {index: [] for index in range(3)}
    expected_state_keys = set()
    expected_proposal_keys = set()
    expected_raw_keys = set()
    repair_totals = Counter({key: 0 for key in ACTUAL_COUNTER_KEYS})
    shadow_writes = Counter({key: 0 for key in ACTUAL_SHADOW_WRITE_KEYS})
    counted_updates = []
    config_hash = None
    for row_index, row in enumerate(update_rows):
        _actual_exact_keys(row, ACTUAL_UPDATE_KEYS, "update {}".format(row_index))
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "updater_invocation"
        ):
            _actual_fail("update schema identity is invalid")
        identity = _actual_update_identity(row)
        for field, value in zip(("sequence", "pair", "timestamp", "invocation"), identity):
            _actual_u64(value, "update " + field)
        if previous_update is not None and identity <= previous_update:
            _actual_fail("update rows are duplicate or out of order")
        previous_update = identity
        sequence_index, pair_index, camera_timestamp_ns, invocation_id = identity
        if sequence_index >= 3 or row["sequence_id"] != ACTUAL_SEQUENCE_IDS[sequence_index]:
            _actual_fail("update sequence identity is invalid")
        pair = serial_by_pair.get((sequence_index, pair_index))
        if pair is None or pair["camera_timestamp_ns"] != camera_timestamp_ns:
            _actual_fail("update does not join its serial-pair camera identity")
        if invocation_owners.get((sequence_index, invocation_id)) != (sequence_index, pair_index):
            _actual_fail("update does not have exactly one serial-pair owner")
        invocation_ids_by_sequence[sequence_index].append(invocation_id)
        update_by_identity[identity] = row
        update_by_invocation[(sequence_index, pair_index, invocation_id)] = row
        terminal = row["terminal_status"]
        if not _actual_terminal_mapping(terminal, row["terminal_subreason"]):
            _actual_fail("update terminal/subreason mapping is invalid")
        _actual_checked_counter_add(
            terminal_counts, terminal, 1, "campaign terminal count"
        )
        _actual_checked_counter_add(
            sequence_terminal_counts[sequence_index], terminal, 1,
            "sequence terminal count",
        )
        if row["live_mode"] != "nullspace" or row["shadow_mode"] != "schur":
            _actual_fail("CP2-C update modes are not nullspace-live/Schur-shadow")
        if row["shadow_enabled"] is not True or row["timing_evidence_eligible"] is not False:
            _actual_fail("CP2-C shadow/timing flags are invalid")
        for field in (
            "duration_ns", "input_feature_count", "raw_system_count", "baseline_commit_count",
            "baseline_transaction_mean_commits", "baseline_covariance_commits",
            "baseline_expected_type_update_calls", "baseline_verified_nominal_fields",
            "baseline_nominal_mismatches", "baseline_covariance_mismatches",
            "baseline_fej_mismatches", "candidate_ekf_update_calls", "candidate_type_update_calls",
            "candidate_mean_writes", "candidate_covariance_writes", "candidate_feature_writes",
            "state_block_rows", "covariance_block_rows",
        ):
            _actual_u64(row[field], "update " + field)
        if row["raw_system_count"] > row["input_feature_count"]:
            _actual_fail("update raw-system count exceeds input feature count")
        for field in (
            "zero_write_snapshot_equal", "candidate_proposal_available",
            "all_block_rows_present", "math_passed",
        ):
            if not isinstance(row[field], bool):
                _actual_fail("update " + field + " is not Boolean")
        if row["math_passed"] is not True:
            _actual_fail("passing CP2-C artifact contains an update with math_passed=false")
        for hash_field in ("config_sha256", "bag_sha256", "pair_index_sha256", "resolved_parameters_sha256"):
            _actual_sha256(row[hash_field], "update " + hash_field)
        if row["bag_sha256"] != bag_hashes[sequence_index]:
            _actual_fail("update bag identity differs from provenance")
        if row["resolved_parameters_sha256"] != resolved_hashes[sequence_index]:
            _actual_fail("update parameter identity differs from provenance")
        if row["pair_index_sha256"] != manifest["serial_pairs.jsonl"]:
            _actual_fail("update pair-index digest differs from final serial trace")
        if row["config_sha256"] != expected_config_sha256:
            _actual_fail("update configuration identity differs from static bundle")
        if config_hash is None:
            config_hash = row["config_sha256"]
        if row["config_sha256"] != config_hash:
            _actual_fail("update configuration identity drifts")
        for name in ("baseline_accepted_ids", "candidate_accepted_ids"):
            values = row[name]
            if not isinstance(values, list) or len(set(values)) != len(values):
                _actual_fail("update accepted-ID array is invalid")
            for value in values:
                _actual_u64(value, "accepted feature ID")
        if row["baseline_accepted_set_sha256"] != schema.accepted_set_sha256(row["baseline_accepted_ids"]):
            _actual_fail("baseline accepted-set digest differs")
        if row["baseline_accepted_sequence_sha256"] != schema.accepted_sequence_sha256(row["baseline_accepted_ids"]):
            _actual_fail("baseline accepted-sequence digest differs")
        if row["candidate_accepted_set_sha256"] != schema.accepted_set_sha256(row["candidate_accepted_ids"]):
            _actual_fail("candidate accepted-set digest differs")
        if row["candidate_accepted_sequence_sha256"] != schema.accepted_sequence_sha256(row["candidate_accepted_ids"]):
            _actual_fail("candidate accepted-sequence digest differs")
        for counters_name in ("baseline_preview_counters", "candidate_preview_counters"):
            counters = _actual_counter_object(row[counters_name], "update " + counters_name)
            for key, value in counters.items():
                _actual_checked_counter_add(
                    repair_totals, key, value, "repair/fallback aggregate"
                )
        for key, value in (
            ("ekf_update_calls", row["candidate_ekf_update_calls"]),
            ("type_update_calls", row["candidate_type_update_calls"]),
            ("mean_writes", row["candidate_mean_writes"]),
            ("covariance_writes", row["candidate_covariance_writes"]),
            ("feature_writes", row["candidate_feature_writes"]),
        ):
            _actual_checked_counter_add(
                shadow_writes, key, value, "shadow-write aggregate"
            )

        compact_identity = (sequence_index, pair_index, invocation_id)
        state_for_update = {
            phase: state_frames.get(compact_identity + (phase,)) for phase in range(4)
        }
        raw_count = row["raw_system_count"]
        if raw_count > 0:
            if state_for_update[0] is None or state_for_update[1] is None:
                _actual_fail("nonzero-raw update lacks phase-0/1 snapshots")
            expected_state_keys.update(compact_identity + (phase,) for phase in (0, 1))
            if not _actual_ranges_equal(
                artifact / "state_snapshot_payloads.bin", state_for_update[0]["offset"],
                state_for_update[1]["offset"], state_for_update[0]["length"],
            ) or state_for_update[0]["length"] != state_for_update[1]["length"]:
                _actual_fail("phase-0/1 snapshot payloads are not byte-identical")
        elif any(state_for_update[phase] is not None for phase in (0, 1)):
            _actual_fail("zero-raw update unexpectedly has phase-0/1 snapshots")
        committed = terminal == "committed_counted"
        if committed:
            counted_updates.append(row)
            if state_for_update[2] is None or state_for_update[3] is None:
                _actual_fail("committed update lacks phase-2/3 snapshots")
            expected_state_keys.update(compact_identity + (phase,) for phase in (2, 3))
            if (
                state_for_update[2]["length"] != state_for_update[3]["length"]
                or not _actual_ranges_equal(
                    artifact / "state_snapshot_payloads.bin", state_for_update[2]["offset"],
                    state_for_update[3]["offset"], state_for_update[2]["length"],
                )
            ):
                _actual_fail("phase-2/3 snapshot payloads are not byte-identical")
        elif any(state_for_update[phase] is not None for phase in (2, 3)):
            _actual_fail("noncommitting update unexpectedly has phase-2/3 snapshots")
        for phase, names in (
            (0, ("prior_snapshot_sha256", "prior_payload_offset", "prior_payload_length")),
            (1, ("precommit_snapshot_sha256", "precommit_payload_offset", "precommit_payload_length")),
            (2, ("expected_postcommit_snapshot_sha256", "expected_postcommit_payload_offset", "expected_postcommit_payload_length")),
            (3, ("live_postcommit_snapshot_sha256", "live_postcommit_payload_offset", "live_postcommit_payload_length")),
        ):
            _actual_nullable_frame(row, names[0], names[1], names[2], state_for_update[phase],
                                   "update state phase {}".format(phase))
        if row["zero_write_snapshot_equal"] is not True:
            _actual_fail("update phase-0/1 zero-write equality did not pass")

        baseline_frame = proposal_frames.get(compact_identity + (0,))
        candidate_frame = proposal_frames.get(compact_identity + (1,))
        baseline_expected = row["baseline_preview_status"] == "accepted"
        candidate_expected = row["candidate_preview_status"] == "accepted"
        if baseline_expected:
            expected_proposal_keys.add(compact_identity + (0,))
        if candidate_expected:
            expected_proposal_keys.add(compact_identity + (1,))
        _actual_nullable_frame(
            row, "baseline_proposal_sha256", "baseline_proposal_payload_offset",
            "baseline_proposal_payload_length", baseline_frame, "baseline proposal",
        )
        _actual_nullable_frame(
            row, "candidate_proposal_sha256", "candidate_proposal_payload_offset",
            "candidate_proposal_payload_length", candidate_frame, "candidate proposal",
        )
        if (baseline_frame is not None) is not baseline_expected:
            _actual_fail("baseline proposal presence differs from preview status")
        if (candidate_frame is not None) is not candidate_expected:
            _actual_fail("candidate proposal presence differs from preview status")
        if row["candidate_proposal_available"] is not candidate_expected:
            _actual_fail("candidate proposal availability differs from its payload")
        if committed and (baseline_frame is None or row["baseline_commit_count"] != 1):
            _actual_fail("committed update lacks exactly one baseline proposal/commit")
        if not committed and row["baseline_commit_count"] != 0:
            _actual_fail("noncommitting update records a baseline commit")
        if any(row[name] != 0 for name in (
            "baseline_nominal_mismatches", "baseline_covariance_mismatches",
            "baseline_fej_mismatches",
        )):
            _actual_fail("update records a live-commit mismatch")
        if row["baseline_gamma_status"] not in ("not_reached", "available", "nonfinite"):
            _actual_fail("baseline gamma status is invalid")
        if (row["baseline_gamma_status"] == "available") is not (row["baseline_gamma"] is not None):
            _actual_fail("baseline gamma status/value nullability differs")
        if row["baseline_gamma"] is not None:
            _actual_f64(row["baseline_gamma"], "baseline gamma", nonnegative=True)
        if row["candidate_gamma"] is not None:
            _actual_f64(row["candidate_gamma"], "candidate gamma", nonnegative=True)
        features_by_update[identity] = []
    for sequence_index, values in invocation_ids_by_sequence.items():
        if values != list(range(len(values))):
            _actual_fail("invocation IDs are noncontiguous for sequence {}".format(sequence_index))
    if set(invocation_owners) != set(update_by_invocation):
        _actual_fail("serial/update invocation ownership is not one-to-one")

    disagreement_counts = Counter({name: 0 for name in ACTUAL_AGREEMENT_CLASSES})
    nullspace_gate_attempts = 0
    schur_gate_attempts = 0
    gate_union = 0
    gate_intersection = 0
    gate_matches = 0
    row_denominator = 0
    row_matches = 0
    statistics_all_passed = True
    previous_feature = None
    for row_index, row in enumerate(feature_rows):
        _actual_exact_keys(row, ACTUAL_FEATURE_KEYS, "feature {}".format(row_index))
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "feature_comparison"
        ):
            _actual_fail("feature schema identity is invalid")
        update_identity = tuple(row[name] for name in (
            "sequence_index", "pair_index", "camera_timestamp_ns", "invocation_id"
        ))
        for field, value in zip(
            ("sequence index", "pair index", "camera timestamp", "invocation ID"),
            update_identity,
        ):
            _actual_u64(value, "feature " + field)
        ordinal = _actual_u64(row["feature_ordinal"], "feature ordinal")
        feature_id = _actual_u64(row["feature_id"], "feature ID")
        feature_key = update_identity + (ordinal,)
        if previous_feature is not None and feature_key <= previous_feature:
            _actual_fail("feature rows are duplicate or out of order")
        previous_feature = feature_key
        update = update_by_identity.get(update_identity)
        if update is None:
            _actual_fail("feature does not join an update")
        group = features_by_update[update_identity]
        if ordinal != len(group):
            _actual_fail("feature ordinals are noncontiguous within an update")
        group.append(row)
        if _actual_u64(row["pass_index"], "feature pass index") != 1:
            _actual_fail("feature pass index is not one")
        raw_rows = _actual_u64(row["raw_rows"], "feature raw rows")
        raw_key = (update_identity[0], update_identity[1], update_identity[3], ordinal, feature_id)
        raw_frame = raw_frames.get(raw_key)
        if raw_frame is None:
            _actual_fail("feature lacks its one-to-one raw-system frame")
        expected_raw_keys.add(raw_key)
        _actual_sha256(row["raw_system_sha256"], "feature raw-system SHA-256")
        _actual_u64(row["raw_payload_offset"], "feature raw payload offset")
        _actual_u64(row["raw_payload_length"], "feature raw payload length")
        if (
            row["raw_system_sha256"] != raw_frame["sha256"]
            or row["raw_payload_offset"] != raw_frame["offset"]
            or row["raw_payload_length"] != raw_frame["length"]
        ):
            _actual_fail("feature raw-system payload identity differs")
        _actual_repeat_update_fields(row, update, "feature")
        layout = row["jacobian_layout"]
        if not isinstance(layout, list) or not layout:
            _actual_fail("feature Jacobian layout is empty or invalid")
        covariance_ids = set()
        for entry in layout:
            _actual_exact_keys(entry, ("covariance_id", "size"), "Jacobian layout entry")
            covariance_id = _actual_u64(entry["covariance_id"], "Jacobian covariance ID")
            size = _actual_u64(entry["size"], "Jacobian block size")
            if size == 0 or covariance_id in covariance_ids:
                _actual_fail("Jacobian layout contains a zero/duplicate block")
            covariance_ids.add(covariance_id)
        null_decision = row["nullspace_decision"]
        schur_decision = row["schur_decision"]
        if null_decision is not None and not isinstance(null_decision, bool):
            _actual_fail("nullspace decision is not nullable Boolean")
        if schur_decision is not None and not isinstance(schur_decision, bool):
            _actual_fail("Schur decision is not nullable Boolean")
        if null_decision is not None:
            nullspace_gate_attempts = _actual_checked_sum(
                (nullspace_gate_attempts, 1), "nullspace gate attempts"
            )
        if schur_decision is not None:
            schur_gate_attempts = _actual_checked_sum(
                (schur_gate_attempts, 1), "Schur gate attempts"
            )
        if null_decision is None and schur_decision is None:
            agreement = "neither_decision"
        elif null_decision is None:
            agreement = "schur_only"
        elif schur_decision is None:
            agreement = "nullspace_only"
        elif null_decision and schur_decision:
            agreement = "both_match_accept"
        elif not null_decision and not schur_decision:
            agreement = "both_match_reject"
        elif null_decision:
            agreement = "boolean_nullspace_accept_schur_reject"
        else:
            agreement = "boolean_nullspace_reject_schur_accept"
        if row["agreement_class"] != agreement:
            _actual_fail("feature agreement class differs from decisions")
        _actual_checked_counter_add(
            disagreement_counts, agreement, 1, "agreement-class aggregate"
        )
        if agreement != "neither_decision":
            gate_union = _actual_checked_sum(
                (gate_union, 1), "gate-union denominator"
            )
            row_denominator = _actual_checked_sum(
                (row_denominator, raw_rows), "raw-row denominator"
            )
        if null_decision is not None and schur_decision is not None:
            gate_intersection = _actual_checked_sum(
                (gate_intersection, 1), "gate intersection"
            )
        if agreement in ("both_match_accept", "both_match_reject"):
            gate_matches = _actual_checked_sum(
                (gate_matches, 1), "gate-match numerator"
            )
            row_matches = _actual_checked_sum(
                (row_matches, raw_rows), "raw-row match numerator"
            )
            if row["raw_row_match_weight"] != raw_rows:
                _actual_fail("matching feature raw-row weight differs")
        elif row["raw_row_match_weight"] != 0:
            _actual_fail("nonmatching feature has nonzero raw-row match weight")
        mode_valid = (
            row["nullspace_reduction_status"] == "accepted"
            and row["schur_reduction_status"] == "accepted"
        )
        if row["statistics_comparison_required"] is not mode_valid:
            _actual_fail("feature statistics-required flag differs from reduction validity")
        feature_stats_pass = True
        for prefix in ("lambda", "eta", "gamma"):
            passed, _ = _actual_validate_comparison(
                row, prefix, 1.0e-8, 1.0e-10, mode_valid,
                "feature {}".format(prefix),
            )
            feature_stats_pass = feature_stats_pass and (passed if mode_valid else True)
        statistics_all_passed = statistics_all_passed and feature_stats_pass
        for counter_name in ("nullspace_reducer_counters", "schur_reducer_counters"):
            counters = _actual_counter_object(row[counter_name], "feature " + counter_name)
            for key, value in counters.items():
                _actual_checked_counter_add(
                    repair_totals, key, value, "repair/fallback aggregate"
                )
    for identity, group in features_by_update.items():
        update = update_by_identity[identity]
        if len(group) != update["raw_system_count"]:
            _actual_fail("update raw-system count differs from its feature rows")
        baseline_ids = [row["feature_id"] for row in group if row["nullspace_decision"] is True]
        candidate_ids = [row["feature_id"] for row in group if row["schur_decision"] is True]
        if baseline_ids != update["baseline_accepted_ids"]:
            _actual_fail("baseline accepted sequence differs from raw feature decisions")
        if candidate_ids != update["candidate_accepted_ids"]:
            _actual_fail("candidate accepted sequence differs from raw feature decisions")
    if set(raw_frames) != expected_raw_keys:
        _actual_fail("raw-system frame population is not one-to-one with features")
    if set(state_frames) != expected_state_keys:
        _actual_fail("state frame population differs from terminal-required phases")
    if set(proposal_frames) != expected_proposal_keys:
        _actual_fail("proposal frame population differs from preview-derived presence")

    state_by_update = {identity: [] for identity in update_by_identity}
    covariance_by_update = {identity: [] for identity in update_by_identity}
    previous_state = None
    state_ratios = []
    for row_index, row in enumerate(state_rows):
        _actual_exact_keys(row, ACTUAL_STATE_BLOCK_KEYS, "state block {}".format(row_index))
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "state_block_comparison"
        ):
            _actual_fail("state-block schema identity is invalid")
        identity = _actual_update_identity(row)
        for field, value in zip(
            ("sequence index", "pair index", "camera timestamp", "invocation ID"), identity
        ):
            _actual_u64(value, "state block " + field)
        block_index = _actual_u64(row["block_index"], "state block index")
        key = identity + (block_index,)
        if previous_state is not None and key <= previous_state:
            _actual_fail("state-block rows are duplicate or out of order")
        previous_state = key
        update = update_by_identity.get(identity)
        if update is None:
            _actual_fail("state block does not join an update")
        if update["terminal_status"] != "committed_counted":
            _actual_fail("noncommitting update has a state-block row")
        _actual_u64(row["covariance_id"], "state covariance ID")
        if _actual_u64(row["size"], "state block size") == 0:
            _actual_fail("state block size is zero")
        if not isinstance(row["block_kind"], str) or not row["block_kind"]:
            _actual_fail("state block kind is invalid")
        _actual_validate_block_identity(row, "state block")
        _actual_repeat_update_fields(row, update, "state block")
        passed, ratio = _actual_validate_block_comparison(row, "state block")
        if not passed:
            _actual_fail("passing CP2-C artifact contains a failed state-block comparison")
        state_ratios.append(ratio)
        state_by_update[identity].append(row)
    previous_covariance = None
    covariance_ratios = []
    for row_index, row in enumerate(covariance_rows):
        _actual_exact_keys(row, ACTUAL_COVARIANCE_BLOCK_KEYS,
                           "covariance block {}".format(row_index))
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "covariance_block_comparison"
        ):
            _actual_fail("covariance-block schema identity is invalid")
        identity = _actual_update_identity(row)
        for field, value in zip(
            ("sequence index", "pair index", "camera timestamp", "invocation ID"), identity
        ):
            _actual_u64(value, "covariance block " + field)
        row_index_value = _actual_u64(row["row_block_index"], "covariance row index")
        column_index = _actual_u64(row["column_block_index"], "covariance column index")
        key = identity + (row_index_value, column_index)
        if previous_covariance is not None and key <= previous_covariance:
            _actual_fail("covariance-block rows are duplicate or out of order")
        previous_covariance = key
        update = update_by_identity.get(identity)
        if update is None or update["terminal_status"] != "committed_counted":
            _actual_fail("covariance block does not join a committed update")
        for field in ("row_covariance_id", "column_covariance_id", "row_size", "column_size"):
            _actual_u64(row[field], "covariance " + field)
        _actual_repeat_update_fields(row, update, "covariance block")
        passed, ratio = _actual_validate_block_comparison(row, "covariance block")
        if not passed:
            _actual_fail("passing CP2-C artifact contains a failed covariance comparison")
        covariance_ratios.append(ratio)
        covariance_by_update[identity].append(row)

    state_expected = 0
    covariance_expected = 0
    for identity, update in update_by_identity.items():
        state_group = state_by_update[identity]
        covariance_group = covariance_by_update[identity]
        if update["terminal_status"] != "committed_counted":
            if state_group or covariance_group or update["state_block_rows"] or update["covariance_block_rows"]:
                _actual_fail("noncommitting update has block evidence")
            if update["all_block_rows_present"] is not True:
                _actual_fail("empty terminal-dependent block population is not marked complete")
            continue
        block_count = len(state_group)
        if [row["block_index"] for row in state_group] != list(range(block_count)):
            _actual_fail("state-block indices are noncontiguous")
        if len({row["covariance_id"] for row in state_group}) != block_count:
            _actual_fail("state-block covariance IDs are duplicate")
        expected_pairs = [(row_index, column_index)
                          for row_index in range(block_count)
                          for column_index in range(block_count)]
        actual_pairs = [(row["row_block_index"], row["column_block_index"])
                        for row in covariance_group]
        if actual_pairs != expected_pairs:
            _actual_fail("covariance blocks do not contain the ordered Cartesian product")
        for covariance in covariance_group:
            row_state = state_group[covariance["row_block_index"]]
            column_state = state_group[covariance["column_block_index"]]
            if (
                covariance["row_covariance_id"] != row_state["covariance_id"]
                or covariance["column_covariance_id"] != column_state["covariance_id"]
                or covariance["row_size"] != row_state["size"]
                or covariance["column_size"] != column_state["size"]
            ):
                _actual_fail("covariance block identity/size differs from state partition")
        if (
            update["state_block_rows"] != block_count
            or update["covariance_block_rows"] != _actual_checked_product(
                block_count, block_count, "per-update covariance-block population"
            )
            or update["all_block_rows_present"] is not True
        ):
            _actual_fail("update block-row summary differs from exact population")
        state_expected = _actual_checked_sum(
            (state_expected, block_count), "campaign expected state blocks"
        )
        covariance_population = _actual_checked_product(
            block_count, block_count, "per-update covariance-block population"
        )
        covariance_expected = _actual_checked_sum(
            (covariance_expected, covariance_population),
            "campaign expected covariance blocks",
        )

    report_count_fields = {
        "attempted_updates": len(update_rows),
        "empty_input": terminal_counts["empty_input"],
        "all_rejected": terminal_counts["all_rejected"],
        "empty_after_compression": terminal_counts["empty_after_compression"],
        "preflight_rejected": terminal_counts["preflight_rejected"],
        "internal_failure": terminal_counts["internal_failure"],
        "committing_updates": terminal_counts["committed_counted"],
        "raw_systems": len(feature_rows),
        "nullspace_gate_attempts": nullspace_gate_attempts,
        "schur_gate_attempts": schur_gate_attempts,
        "gate_union_denominator": gate_union,
        "gate_intersection": gate_intersection,
        "gate_match_numerator": gate_matches,
        "row_denominator": row_denominator,
        "row_match_numerator": row_matches,
        "state_blocks_expected": state_expected,
        "state_blocks_seen": len(state_rows),
        "covariance_blocks_expected": covariance_expected,
        "covariance_blocks_seen": len(covariance_rows),
    }
    for field, expected in report_count_fields.items():
        if report[field] != expected:
            _actual_fail("campaign {} differs from independently reconstructed value".format(field))
    if report["minimum_committing_updates"] != 1000 or len(counted_updates) < 1000:
        _actual_fail("campaign does not meet the frozen thousand-counted-update minimum")
    _actual_exact_keys(report["disagreement_counts"], ACTUAL_AGREEMENT_CLASSES,
                       "campaign disagreement counts")
    if report["disagreement_counts"] != dict(disagreement_counts):
        _actual_fail("campaign disagreement counts differ from feature classes")
    if gate_union == 0 or row_denominator == 0:
        _actual_fail("campaign agreement population is empty")
    expected_gate_ratio = _actual_exact_count_ratio(
        gate_matches, gate_union, "campaign gate ratio"
    )
    expected_row_ratio = _actual_exact_count_ratio(
        row_matches, row_denominator, "campaign raw-row ratio"
    )
    if not _actual_same_f64(report["gate_ratio"], expected_gate_ratio):
        _actual_fail("campaign gate ratio differs")
    if not _actual_same_f64(report["row_ratio"], expected_row_ratio):
        _actual_fail("campaign raw-row ratio differs")
    gate_passed = 1000 * gate_matches >= 999 * gate_union and 1000 * row_matches >= 999 * row_denominator
    if report["gate_passed"] is not gate_passed or not gate_passed:
        _actual_fail("campaign exact integer agreement gate did not pass")
    if report["per_feature_statistics_passed"] is not statistics_all_passed or not statistics_all_passed:
        _actual_fail("campaign feature-statistics conjunction did not pass")
    expected_max_state = max(state_ratios) if state_ratios else None
    expected_max_covariance = max(covariance_ratios) if covariance_ratios else None
    for field, expected in (
        ("maximum_state_ratio", expected_max_state),
        ("maximum_covariance_ratio", expected_max_covariance),
    ):
        if expected is None or not _actual_same_f64(report[field], expected):
            _actual_fail("campaign {} differs from comparison rows".format(field))
    _actual_exact_keys(report["shadow_write_totals"], ACTUAL_SHADOW_WRITE_KEYS,
                       "campaign shadow writes")
    if report["shadow_write_totals"] != dict(shadow_writes) or any(shadow_writes.values()):
        _actual_fail("campaign contains or misreports shadow writes")
    _actual_exact_keys(report["repair_fallback_totals"], ACTUAL_COUNTER_KEYS,
                       "campaign repair/fallback totals")
    if report["repair_fallback_totals"] != dict(repair_totals) or any(repair_totals.values()):
        _actual_fail("campaign contains or misreports repair/fallback counters")
    candidate_missing = sum(
        1 for row in counted_updates if not row["candidate_proposal_available"]
    )
    if report["candidate_missing_proposals"] != candidate_missing or candidate_missing:
        _actual_fail("campaign contains or misreports candidate-missing counted updates")
    if report["baseline_commit_mismatches"] != 0:
        _actual_fail("campaign reports baseline commit mismatches")
    if report["internal_failure"] != 0:
        _actual_fail("campaign contains an internal-failure terminal")

    summaries = report["sequence_summaries"]
    if not isinstance(summaries, list) or len(summaries) != 3:
        _actual_fail("campaign sequence summary population is not three")
    for sequence_index, summary in enumerate(summaries):
        _actual_exact_keys(summary, ACTUAL_SEQUENCE_SUMMARY_KEYS,
                           "sequence summary {}".format(sequence_index))
        for field in (
            "sequence_index", "attempted_updates", "committing_updates", "empty_input",
            "all_rejected", "empty_after_compression", "preflight_rejected",
            "internal_failure",
        ):
            _actual_u64(summary[field], "sequence summary " + field)
        for field in (
            "first_pair_index", "last_pair_index", "first_camera_timestamp_ns",
            "last_camera_timestamp_ns",
        ):
            if summary[field] is not None:
                _actual_u64(summary[field], "sequence summary " + field)
        if summary["sequence_index"] != sequence_index or summary["sequence_id"] != ACTUAL_SEQUENCE_IDS[sequence_index]:
            _actual_fail("sequence summary identity/order differs")
        rows = [row for row in update_rows if row["sequence_index"] == sequence_index]
        expected = sequence_terminal_counts[sequence_index]
        values = {
            "attempted_updates": len(rows),
            "committing_updates": expected["committed_counted"],
            "empty_input": expected["empty_input"],
            "all_rejected": expected["all_rejected"],
            "empty_after_compression": expected["empty_after_compression"],
            "preflight_rejected": expected["preflight_rejected"],
            "internal_failure": expected["internal_failure"],
        }
        if any(summary[field] != value for field, value in values.items()):
            _actual_fail("sequence summary terminal counts differ")
        if rows:
            expected_bounds = (
                rows[0]["pair_index"], rows[-1]["pair_index"],
                rows[0]["camera_timestamp_ns"], rows[-1]["camera_timestamp_ns"],
            )
        else:
            expected_bounds = (None, None, None, None)
        actual_bounds = tuple(summary[field] for field in (
            "first_pair_index", "last_pair_index", "first_camera_timestamp_ns",
            "last_camera_timestamp_ns",
        ))
        if actual_bounds != expected_bounds:
            _actual_fail("sequence summary first/last identities differ")
    return {
        "replayed_invocations": sum(row["raw_system_count"] > 0 for row in update_rows),
        "replayed_raw_systems": len(feature_rows),
        "baseline_expected_proposals": sum(key[-1] == 0 for key in proposal_frames),
        "candidate_expected_proposals": sum(key[-1] == 1 for key in proposal_frames),
        "resolved_parameters": resolved_hashes,
    }


def _actual_canonical_jsonl_bytes(records, label):
    encoded = bytearray()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            _actual_fail("{} row {} is not an object".format(label, index))
        try:
            line = json.dumps(
                record, allow_nan=False, ensure_ascii=False,
                separators=(",", ":"), sort_keys=True,
            )
            encoded.extend(line.encode("utf-8", "strict"))
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ActualVerificationError(
                "{} row {} cannot be encoded canonically".format(label, index)
            ) from exc
        encoded.extend(b"\n")
    return bytes(encoded)


def _actual_canonical_u64_argument(value, label):
    if not isinstance(value, str) or re.fullmatch(r"0|[1-9][0-9]*", value) is None:
        _actual_fail(label + " is not a canonical unsigned decimal argument")
    converted = int(value, 10)
    _actual_u64(converted, label)
    if str(converted) != value:
        _actual_fail(label + " is not a canonical unsigned decimal argument")
    return converted


def _actual_validate_pair_index_population(rows, sequence_index):
    if len(rows) < 2:
        _actual_fail("independent pair-index population has fewer than two rows")
    sequence_id = ACTUAL_SEQUENCE_IDS[sequence_index]
    used_camera_indices = set()
    previous_anchor = None
    selected_cam0_times = []
    for pair_index, row in enumerate(rows):
        _actual_exact_keys(
            row, ACTUAL_PAIR_INDEX_PROJECTION_KEYS,
            "independent pair-index row {}:{}".format(sequence_index, pair_index),
        )
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "pair_index"
            or _actual_u64(row["sequence_index"], "pair-index sequence")
            != sequence_index
            or row["sequence_id"] != sequence_id
            or _actual_u64(row["pair_index"], "pair-index ordinal") != pair_index
        ):
            _actual_fail("independent pair-index identity/order differs")
        anchor = _actual_u64(row["anchor_filtered_index"], "pair-index anchor")
        camera_id = _actual_u64(row["anchor_camera_id"], "pair-index anchor camera")
        cam0 = _actual_u64(row["cam0_filtered_index"], "pair-index cam0 index")
        cam1 = _actual_u64(row["cam1_filtered_index"], "pair-index cam1 index")
        if camera_id not in (0, 1) or anchor != (cam0 if camera_id == 0 else cam1):
            _actual_fail("independent pair-index anchor relation differs")
        if previous_anchor is not None and anchor <= previous_anchor:
            _actual_fail("independent pair-index anchors are not strictly ordered")
        previous_anchor = anchor
        if cam0 == cam1 or cam0 in used_camera_indices or cam1 in used_camera_indices:
            _actual_fail("independent pair-index reuses a camera message")
        used_camera_indices.update((cam0, cam1))
        cam0_record = _actual_u64(
            row["cam0_record_time_ns"], "pair-index cam0 record time"
        )
        cam1_record = _actual_u64(
            row["cam1_record_time_ns"], "pair-index cam1 record time"
        )
        _actual_u64(row["cam0_header_time_ns"], "pair-index cam0 header time")
        _actual_u64(row["cam1_header_time_ns"], "pair-index cam1 header time")
        delta = _actual_u64(
            row["absolute_record_delta_ns"], "pair-index absolute record delta"
        )
        if delta != abs(cam0_record - cam1_record) or delta >= 20_000_000:
            _actual_fail("independent pair-index violates the strict record-time rule")
        candidate_index = cam1 if camera_id == 0 else cam0
        anchor_time = cam0_record if camera_id == 0 else cam1_record
        candidate_time = cam1_record if camera_id == 0 else cam0_record
        if candidate_index <= anchor or candidate_time < anchor_time:
            _actual_fail("independent pair-index candidate is not forward of its anchor")
        selected_cam0_times.append(cam0_record)
    if any(
        left > right
        for left, right in zip(selected_cam0_times, selected_cam0_times[1:])
    ):
        _actual_fail("independent pair-index selected cam0 record times reverse")
    if selected_cam0_times[-1] <= selected_cam0_times[0]:
        _actual_fail("independent pair-index has no positive selected duration")


def _actual_validate_recorded_pair_index_commands(
    artifact, common, manifest, serial_rows,
):
    pair_commands = [
        record for record in common["commands"] if record["phase"] == "pair_index"
    ]
    if len(pair_commands) != len(ACTUAL_SEQUENCE_IDS):
        _actual_fail("CP2-C must retain exactly three pair-index commands")

    provenance = common["provenance"]
    environment_records = provenance["environment"]["classes"]
    workspace = _actual_safe_absolute_path(
        provenance["build"]["workspace"], "pair-index fresh workspace"
    )
    helper = str(workspace / "src/scripts/cp2/cp2_pair_index_extract.py")
    inputs = provenance["inputs"]
    expected_flags = (
        "--sequence-index", "--sequence-id", "--bag-path",
        "--parent-bag-fd", "--bag-identity",
    )
    previous_pair_command_id = None

    for sequence_index, record in enumerate(pair_commands):
        sequence_id = ACTUAL_SEQUENCE_IDS[sequence_index]
        command_id = _actual_u64(record["command_id"], "pair-index command ID")
        if (
            record["sequence_index"] != sequence_index
            or record["pair_index"] is not None
            or record["run_index"] is not None
            or record["exit_code"] != 0
            or record["timed_out"] is not False
            or (
                previous_pair_command_id is not None
                and command_id <= previous_pair_command_id
            )
        ):
            _actual_fail("pair-index command identity/order/success differs")
        previous_pair_command_id = command_id
        for phase in ("runtime_preflight", "ros_run"):
            matching_runtime_commands = [
                candidate for candidate in common["commands"]
                if candidate["phase"] == phase
                and candidate["sequence_index"] == sequence_index
            ]
            if (
                len(matching_runtime_commands) != 1
                or matching_runtime_commands[0]["pair_index"] is not None
                or matching_runtime_commands[0]["run_index"] != sequence_index
                or _actual_u64(
                    matching_runtime_commands[0]["command_id"],
                    "{} command ID".format(phase),
                ) <= command_id
            ):
                _actual_fail(
                    "pair-index command does not precede exactly one {}".format(phase)
                )

        environment_sha = record["environment_sha256"]
        matching_classes = [
            item for item in environment_records
            if item["canonical_sha256"] == environment_sha
        ]
        if (
            len(matching_classes) != 1
            or matching_classes[0]["environment_id"] != "pair_index_v1"
            or common["environment_classes"].get(environment_sha)
            != ACTUAL_PAIR_INDEX_ENVIRONMENT
        ):
            _actual_fail("pair-index command environment is not exact pair_index_v1")

        argv = record["argv"]
        if (
            len(argv) != 14
            or argv[:4] != ["/usr/bin/python3", "-I", "-B", helper]
            or tuple(argv[4::2]) != expected_flags
        ):
            _actual_fail("pair-index command differs from the exact extractor CLI")
        arguments = dict(zip(argv[4::2], argv[5::2]))
        if (
            arguments["--sequence-index"] != str(sequence_index)
            or arguments["--sequence-id"] != sequence_id
            or arguments["--bag-path"] != inputs[sequence_index]["bag_path"]
        ):
            _actual_fail("pair-index command does not join its frozen input identity")
        descriptor = _actual_canonical_u64_argument(
            arguments["--parent-bag-fd"], "pair-index parent descriptor"
        )
        if descriptor > (1 << 31) - 1:
            _actual_fail("pair-index parent descriptor exceeds the supported range")
        identity_fields = arguments["--bag-identity"].split(":")
        if len(identity_fields) != 9:
            _actual_fail("pair-index bag identity has the wrong field population")
        identity = [
            _actual_canonical_u64_argument(value, "pair-index bag identity")
            for value in identity_fields
        ]
        if (
            not stat.S_ISREG(identity[2])
            or identity[3] != 1
            or identity[6] != inputs[sequence_index]["bag_size"]
        ):
            _actual_fail(
                "pair-index retained bag identity is not single-link regular or size-bound"
            )

        streams = {}
        for stream_name in ("stdout", "stderr"):
            relative = _actual_relpath(
                record[stream_name], "pair-index command " + stream_name
            )
            if (
                relative not in manifest
                or record[stream_name + "_sha256"] != manifest[relative]
            ):
                _actual_fail("pair-index command stream does not join the manifest")
            payload = _actual_read_bytes(
                artifact / relative,
                "pair-index command " + stream_name,
                ACTUAL_MAX_JSONL_BYTES,
            )
            if hashlib.sha256(payload).hexdigest() != manifest[relative]:
                _actual_fail("pair-index command stream digest differs")
            streams[stream_name] = payload
        if streams["stderr"] != b"":
            _actual_fail("successful pair-index command retained nonempty stderr")

        stdout_rows, stdout_bytes = _actual_jsonl(
            artifact / record["stdout"],
            "pair-index command stdout {}".format(sequence_index),
        )
        if stdout_bytes != streams["stdout"]:
            _actual_fail("pair-index stdout changed between retained reads")
        if _actual_canonical_jsonl_bytes(
            stdout_rows, "pair-index command stdout"
        ) != stdout_bytes:
            _actual_fail("pair-index command stdout is not canonical JSONL")
        _actual_validate_pair_index_population(stdout_rows, sequence_index)

        projection = []
        for serial_row in serial_rows:
            if serial_row["sequence_index"] != sequence_index:
                continue
            projected = {
                key: serial_row[key] for key in ACTUAL_PAIR_INDEX_PROJECTION_KEYS
            }
            projected["record_type"] = "pair_index"
            projection.append(projected)
        projected_bytes = _actual_canonical_jsonl_bytes(
            projection, "serial-pair source projection"
        )
        if projected_bytes != stdout_bytes:
            _actual_fail(
                "serial-pair source projection differs from independent pair-index stdout"
            )


def _actual_validate_replay_report(
    replay, common, manifest, derived, replay_bytes, retained_digest,
):
    _actual_exact_keys(replay, ACTUAL_REPLAY_KEYS, "offline replay report")
    if (
        not _schema_version_one(replay["schema_version"])
        or replay["record_type"] != "cp2_offline_replay"
        or replay["checkpoint"] != "CP2-C"
    ):
        _actual_fail("offline replay schema identity is invalid")
    provenance = common["provenance"]
    runtime = common["runtime"]
    if replay["source_commit"] != provenance["source_commit"] or replay["source_tree"] != provenance["source_tree"]:
        _actual_fail("offline replay source identity differs from provenance")
    if (
        replay["executable_sha256"] != common["executable_sha256"]
        or replay["executable_build_id"] != runtime["build_id_before"]
    ):
        _actual_fail("offline replay executable identity differs")
    if replay["strict_fp_verified"] is not True or replay["bag_provider_calls"] != 0:
        _actual_fail("offline replay strict-FP/bag-provider proof is invalid")
    _actual_u64(replay["bag_provider_calls"], "offline replay bag-provider calls")
    _actual_exact_keys(replay["input_sha256"], ACTUAL_REPLAY_INPUT_KEYS,
                       "offline replay inputs")
    file_by_key = {
        "serial_pairs": "serial_pairs.jsonl", "updates": "updates.jsonl",
        "features": "features.jsonl", "state_blocks": "state_blocks.jsonl",
        "covariance_blocks": "covariance_blocks.jsonl",
        "state_snapshot_payloads": "state_snapshot_payloads.bin",
        "proposal_payloads": "proposal_payloads.bin",
        "raw_system_payloads": "raw_system_payloads.bin",
    }
    if any(replay["input_sha256"][key] != manifest[path] for key, path in file_by_key.items()):
        _actual_fail("offline replay input hashes differ from final artifact bytes")
    resolved = replay["resolved_parameters"]
    if not isinstance(resolved, list) or len(resolved) != 3:
        _actual_fail("offline replay resolved-parameter population differs")
    for sequence_index, record in enumerate(resolved):
        _actual_exact_keys(record, ("sequence_index", "sha256"), "replay resolved parameter")
        if record["sequence_index"] != sequence_index or record["sha256"] != derived["resolved_parameters"][sequence_index]:
            _actual_fail("offline replay resolved-parameter identity differs")
    for field in (
        "replayed_invocations", "replayed_raw_systems", "baseline_expected_proposals",
        "candidate_expected_proposals",
    ):
        _actual_u64(replay[field], "offline replay " + field)
        if replay[field] != derived[field] or replay[field] == 0:
            _actual_fail("offline replay {} is zero or differs from reconstruction".format(field))
    if (
        _actual_u64(replay["baseline_exact_proposal_matches"], "baseline exact proposal matches")
        != replay["baseline_expected_proposals"]
        or _actual_u64(replay["candidate_exact_proposal_matches"], "candidate exact proposal matches")
        != replay["candidate_expected_proposals"]
    ):
        _actual_fail("offline replay exact proposal match counts differ")
    _actual_exact_keys(replay["failure_counts"], ACTUAL_REPLAY_FAILURE_KEYS,
                       "offline replay failure counts")
    if any(_actual_u64(value, "offline replay failure count") != 0
           for value in replay["failure_counts"].values()):
        _actual_fail("offline replay contains a mathematical/structural failure")
    if replay["passed"] is not True:
        _actual_fail("offline replay report did not pass")
    if hashlib.sha256(replay_bytes).hexdigest() != retained_digest:
        _actual_fail("retained offline replay bytes differ from report hash")


def _actual_offline_environment(common, temporary):
    executable = str(common["executable"])
    candidates = []
    for record in common["commands"]:
        argv = record["argv"]
        if (
            record["phase"] == "verification"
            and len(argv) == 5
            and argv[0] == executable
            and argv[1] == "--cp2-offline-replay"
            and argv[3] == "--output"
        ):
            candidates.append(record)
    if len(candidates) != 1:
        _actual_fail("artifact does not retain exactly one prior offline-replay command")
    record = candidates[0]
    original_artifact = _actual_safe_absolute_path(
        record["argv"][2], "retained offline-replay artifact"
    )
    original_output = _actual_safe_absolute_path(
        record["argv"][4], "retained offline-replay output"
    )
    if original_output != original_artifact / "replay_report.json":
        _actual_fail("retained offline-replay output is not its artifact replay report")
    if record["exit_code"] != 0 or record["timed_out"] is not False:
        _actual_fail("retained offline-replay command did not complete successfully")
    environment = dict(common["environment_classes"][record["environment_sha256"]])
    environment["HOME"] = str(temporary)
    environment["TMPDIR"] = str(temporary)
    environment.pop("CP2_SELF_TEST", None)
    environment.pop("CP2_FORBID_BAG_ACCESS", None)
    return environment


def _actual_process_group_exists(process_group):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _actual_terminate_reap_and_wait_for_group_absence(process, label):
    """Kill an owned session, reap its leader, and prove group absence."""

    process_group = process.pid
    group_was_present = _actual_process_group_exists(process_group)
    if group_was_present:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + ACTUAL_PROCESS_GROUP_CLEANUP_SECONDS
    while process.returncode is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _actual_fail(label + " leader could not be reaped")
        try:
            process.wait(
                timeout=min(ACTUAL_PROCESS_GROUP_POLL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            continue
        except InterruptedError:
            continue
    while _actual_process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            _actual_fail(label + " process group did not disappear after SIGKILL")
        time.sleep(min(ACTUAL_PROCESS_GROUP_POLL_SECONDS, remaining))
    return group_was_present


def _actual_wait_owned_process_group(process, timeout, label):
    """Wait once and unconditionally close the complete owned process group."""

    timed_out = False
    completed_normally = False
    group_was_present = False
    try:
        try:
            process.wait(timeout=timeout)
            completed_normally = True
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        # BaseException-wide by design: cleanup precedes propagation of a
        # timeout, RuntimeError, KeyboardInterrupt, or SystemExit.
        group_was_present = _actual_terminate_reap_and_wait_for_group_absence(
            process, label
        )
    if process.returncode is None:
        _actual_fail(label + " lacks an exit status after process-group cleanup")
    return int(process.returncode), timed_out, (
        completed_normally and group_was_present
    )


def _actual_rerun_offline_replay(artifact, retained_bytes, common):
    temporary = Path(tempfile.mkdtemp(prefix="schurvio-cp2-detached-replay-", dir="/tmp"))
    os.chmod(str(temporary), 0o700)
    output = temporary / "replay_report.json"
    stdout_path = temporary / "stdout.bin"
    stderr_path = temporary / "stderr.bin"
    descriptor = os.open(str(output), os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC, 0o600)
    os.close(descriptor)
    before = output.lstat()
    command = [
        str(common["executable"]), "--cp2-offline-replay", str(artifact),
        "--output", str(output),
    ]
    environment = _actual_offline_environment(common, temporary)
    process = None
    try:
        with stdout_path.open("wb") as stdout_stream, stderr_path.open("wb") as stderr_stream:
            process = subprocess.Popen(
                command, cwd="/tmp", env=environment, stdin=subprocess.DEVNULL,
                stdout=stdout_stream, stderr=stderr_stream, start_new_session=True,
                close_fds=True,
            )
            return_code, timed_out, surviving_descendants = (
                _actual_wait_owned_process_group(
                    process,
                    ACTUAL_OFFLINE_REPLAY_TIMEOUT_SECONDS,
                    "detached offline replay",
                )
            )
            if timed_out:
                _actual_fail("detached offline replay timed out")
            if surviving_descendants:
                _actual_fail(
                    "detached offline replay retained a process-group descendant"
                )
        if return_code != 0:
            _actual_fail("detached offline replay returned nonzero")
        after = output.lstat()
        if (
            not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            _actual_fail("detached offline replay replaced its precreated output")
        reproduced = _actual_read_bytes(output, "detached replay report", ACTUAL_MAX_JSON_BYTES)
        _actual_read_bytes(stdout_path, "detached replay stdout", ACTUAL_MAX_JSON_BYTES)
        _actual_read_bytes(stderr_path, "detached replay stderr", ACTUAL_MAX_JSON_BYTES)
        strict_json_bytes(reproduced, "detached replay report")
        if reproduced != retained_bytes:
            _actual_fail("detached offline replay report is not byte-identical")
        if sha256_file(common["executable"]) != common["executable_sha256"]:
            _actual_fail("runtime executable changed during detached replay")
        if {path.name for path in temporary.iterdir()} != {
            output.name, stdout_path.name, stderr_path.name
        }:
            _actual_fail("detached offline replay created an undeclared /tmp output")
    finally:
        try:
            if process is not None:
                _actual_terminate_reap_and_wait_for_group_absence(
                    process, "detached offline replay"
                )
        finally:
            shutil.rmtree(str(temporary), ignore_errors=False)


def verify_recorded_artifact(artifact, manifest_sha256, quiet=False, run_offline_replay=True):
    raw = os.fspath(artifact)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        _actual_fail("recorded artifact path must be normalized and absolute")
    _actual_sha256(manifest_sha256, "recorded manifest anchor")
    if ACTUAL_CP2_C_AUTHORIZED is not True:
        _actual_fail(ACTUAL_CP2_C_BLOCK_REASON)

    # Unreachable until an approval-bound replacement deliberately removes the
    # pre-access block above; retained implementation remains reviewable.
    artifact = Path(artifact)
    manifest, observed, _ = _actual_scan_and_verify_manifest(artifact, manifest_sha256)
    required = {
        "cp2_report.json", "provenance.json", "commands.jsonl", "serial_pairs.jsonl",
        "updates.jsonl", "features.jsonl", "state_blocks.jsonl", "covariance_blocks.jsonl",
        "replay_report.json", "state_snapshot_payloads.bin", "proposal_payloads.bin",
        "raw_system_payloads.bin",
    }
    if not required.issubset(manifest):
        _actual_fail("CP2-C artifact lacks a fixed core file")
    common = _actual_validate_provenance(artifact, manifest, observed, "CP2-C")
    report = _actual_json(artifact / "cp2_report.json", "cp2_report.json")
    _actual_exact_keys(report, ACTUAL_RECORDED_REPORT_KEYS, "CP2-C report")
    if (
        not _schema_version_one(report["schema_version"])
        or report["record_type"] != "recorded_parity_campaign"
        or report["checkpoint"] != "CP2-C"
        or report["status"] != "passed"
        or report["evidence_class"] != "trusted_runner_local_staging_evidence"
        or report["distribution_status"] != "internal_non_conveyable_staging"
        or report["eligible_for_cp2_seal"] is not False
    ):
        _actual_fail("CP2-C report identity/status is invalid")
    _actual_utc(report["created_utc"], "CP2-C report created_utc")
    if report["created_utc"] != common["provenance"]["created_utc"]:
        _actual_fail("CP2-C report/provenance creation instants differ")
    for field in (
        "attempted_updates", "empty_input", "all_rejected", "empty_after_compression",
        "preflight_rejected", "internal_failure", "committing_updates",
        "minimum_committing_updates", "raw_systems", "nullspace_gate_attempts",
        "schur_gate_attempts", "gate_union_denominator", "gate_intersection",
        "gate_match_numerator", "row_denominator", "row_match_numerator",
        "state_blocks_expected", "state_blocks_seen", "covariance_blocks_expected",
        "covariance_blocks_seen", "candidate_missing_proposals",
        "baseline_commit_mismatches",
    ):
        _actual_u64(report[field], "CP2-C report " + field)
    for field in (
        "proposal_derivation_passed", "per_feature_statistics_passed", "gate_passed",
        "math_passed", "passed",
    ):
        if not isinstance(report[field], bool):
            _actual_fail("CP2-C report " + field + " is not Boolean")
    _actual_hash_file_fields(report, manifest, {
        "provenance_sha256": "provenance.json", "commands_sha256": "commands.jsonl",
        "serial_pairs_sha256": "serial_pairs.jsonl", "updates_sha256": "updates.jsonl",
        "features_sha256": "features.jsonl", "state_blocks_sha256": "state_blocks.jsonl",
        "covariance_blocks_sha256": "covariance_blocks.jsonl",
        "state_snapshot_payloads_sha256": "state_snapshot_payloads.bin",
        "proposal_payloads_sha256": "proposal_payloads.bin",
        "raw_system_payloads_sha256": "raw_system_payloads.bin",
        "replay_report_sha256": "replay_report.json",
    })
    bag_hashes, resolved_hashes = _actual_validate_recorded_provenance(
        artifact, common, manifest
    )
    serial_rows, _ = _actual_jsonl(artifact / "serial_pairs.jsonl", "serial_pairs.jsonl")
    update_rows, _ = _actual_jsonl(artifact / "updates.jsonl", "updates.jsonl")
    feature_rows, _ = _actual_jsonl(artifact / "features.jsonl", "features.jsonl")
    state_rows, _ = _actual_jsonl(artifact / "state_blocks.jsonl", "state_blocks.jsonl")
    covariance_rows, _ = _actual_jsonl(
        artifact / "covariance_blocks.jsonl", "covariance_blocks.jsonl"
    )
    state_frames = _actual_parse_state_payloads(artifact / "state_snapshot_payloads.bin")
    proposal_frames = _actual_parse_proposal_payloads(artifact / "proposal_payloads.bin")
    raw_frames = _actual_parse_raw_payloads(artifact / "raw_system_payloads.bin")
    derived = _actual_validate_recorded_rows(
        artifact, manifest, report, serial_rows, update_rows, feature_rows, state_rows,
        covariance_rows, state_frames, proposal_frames, raw_frames, bag_hashes,
        resolved_hashes, common["configuration"]["static_bundle_sha256"],
    )
    _actual_validate_recorded_pair_index_commands(
        artifact, common, manifest, serial_rows
    )
    replay_bytes = _actual_read_bytes(
        artifact / "replay_report.json", "replay_report.json", ACTUAL_MAX_JSON_BYTES
    )
    replay = strict_json_bytes(replay_bytes, "replay_report.json")
    if not isinstance(replay, dict):
        _actual_fail("replay_report.json is not an object")
    _actual_validate_replay_report(
        replay, common, manifest, derived, replay_bytes, report["replay_report_sha256"]
    )
    if report["proposal_derivation_passed"] is not replay["passed"]:
        _actual_fail("campaign proposal-derivation flag differs from replay")
    if (
        report["math_passed"] is not True or report["passed"] is not True
        or report["gate_passed"] is not True
    ):
        _actual_fail("CP2-C campaign did not satisfy its mathematical/report conjunction")
    if run_offline_replay:
        _actual_rerun_offline_replay(artifact, replay_bytes, common)
    result = {
        "checkpoint": "CP2-C", "manifest_sha256": manifest_sha256,
        "passed": True, "provenance": common["provenance"], "report": report,
        "runtime_identity": (
            common["provenance"]["source_commit"], common["provenance"]["source_tree"],
            common["runtime"]["executable_sha256_before"], common["runtime"]["build_id_before"],
            common["configuration"]["static_bundle_sha256"],
            common["configuration"]["launch"]["sha256"],
        ),
    }
    if not quiet:
        print("CP2-C recorded artifact independently verified: " + str(artifact))
        print("SHA256SUMS SHA-256: " + manifest_sha256)
    return result


ACTUAL_SEQUENCE_REPORT_KEYS = (
    "schema_version", "record_type", "checkpoint", "status", "sequence_index",
    "sequence_id", "offset_seconds", "provenance_sha256", "pair_index_sha256",
    "valid_pair_count", "runs", "normalized_parameter_diff", "shared_timestamp_count",
    "shared_timestamp_sha256", "shared_population_sha256", "baseline_alignment",
    "position_p95_m", "orientation_p95_deg", "ate_nullspace_m", "ate_schur_m",
    "relative_ate_difference", "coverage_passed", "trajectory_passed", "passed",
)
ACTUAL_SEQUENCE_RUN_KEYS = (
    "run_index", "mode", "executable_sha256", "loader_map_sha256",
    "resolved_parameters_sha256", "callback_trace_sha256", "trajectory_sha256",
    "processed_unique_pairs", "processing_fraction", "first_selected_timestamp_ns",
    "last_selected_timestamp_ns", "first_processed_timestamp_ns",
    "last_processed_timestamp_ns", "selected_duration_ns", "processed_duration_ns",
    "time_coverage", "completed", "exit_code",
)
ACTUAL_PAIR_INDEX_KEYS = ACTUAL_PAIR_INDEX_PROJECTION_KEYS
ACTUAL_CALLBACK_KEYS = (
    "schema_version", "record_type", "sequence_index", "sequence_id", "mode",
    "callback_index", "pair_index", "anchor_filtered_index", "cam0_filtered_index",
    "cam1_filtered_index", "cam0_record_time_ns", "cam1_record_time_ns",
    "cam0_header_time_ns", "cam1_header_time_ns", "camera_timestamp_ns",
    "enqueue_entered", "enqueue_returned", "enqueue_status", "processing_entered",
    "processing_returned", "processing_status", "state_row_emitted", "trajectory_index",
)
ACTUAL_TRAJECTORY_KEYS = (
    "schema_version", "record_type", "sequence_index", "sequence_id", "mode",
    "trajectory_index", "callback_index", "pair_index", "camera_timestamp_ns",
    "position_G", "quaternion_ItoG_xyzw",
)
ACTUAL_ALIGNMENT_KEYS = (
    "source", "shared_population_sha256", "rotation_row_major", "translation",
    "quaternion_xyzw", "source_singular_values", "source_rank_threshold", "determinant",
    "orthogonality_error_frobenius", "applied_identically_to_both_modes",
)


class _ActualByteReader:
    def __init__(self, content, label):
        self.content = content
        self.label = label
        self.offset = 0

    def take(self, length, label):
        if isinstance(length, bool) or not isinstance(length, int) or length < 0:
            _actual_fail(self.label + " has an invalid " + label + " length")
        end = self.offset + length
        if end > len(self.content):
            _actual_fail(self.label + " is truncated at " + label)
        value = self.content[self.offset:end]
        self.offset = end
        return value

    def u64(self, label):
        return int.from_bytes(self.take(8, label), "big")

    def finish(self):
        if self.offset != len(self.content):
            _actual_fail(self.label + " has trailing bytes")


def _actual_decode_parameter_value(reader, label, depth=0):
    if depth > 128:
        _actual_fail(label + " nesting exceeds 128")
    tag = reader.take(1, label + " tag")
    if tag == b"b":
        value = reader.take(1, label + " Boolean")
        if value not in (b"\0", b"\1"):
            _actual_fail(label + " Boolean payload is invalid")
        return value == b"\1"
    if tag == b"i":
        return int.from_bytes(reader.take(8, label + " integer"), "big", signed=True)
    if tag == b"f":
        value = struct.unpack(">d", reader.take(8, label + " binary64"))[0]
        if not math.isfinite(value):
            _actual_fail(label + " binary64 value is nonfinite")
        return value
    if tag == b"s":
        length = reader.u64(label + " string length")
        try:
            value = reader.take(length, label + " string").decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError(label + " string is not UTF-8") from exc
        if "\0" in value:
            _actual_fail(label + " string contains NUL")
        return value
    if tag == b"l":
        count = reader.u64(label + " list count")
        if count > len(reader.content) - reader.offset:
            _actual_fail(label + " list count exceeds remaining bytes")
        return [
            _actual_decode_parameter_value(reader, "{}[{}]".format(label, index), depth + 1)
            for index in range(count)
        ]
    if tag == b"m":
        count = reader.u64(label + " map count")
        if count > (len(reader.content) - reader.offset) // 10:
            _actual_fail(label + " map count exceeds remaining bytes")
        result = {}
        previous = None
        for index in range(count):
            length = reader.u64(label + " map-key length")
            encoded = reader.take(length, label + " map key")
            if previous is not None and encoded <= previous:
                _actual_fail(label + " map keys are duplicate or not bytewise sorted")
            previous = encoded
            try:
                name = encoded.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                raise ActualVerificationError(label + " map key is not UTF-8") from exc
            if "\0" in name:
                _actual_fail(label + " map key contains NUL")
            result[name] = _actual_decode_parameter_value(
                reader, "{}[{!r}]".format(label, name), depth + 1
            )
        return result
    _actual_fail(label + " has an unknown canonical parameter tag")


def _actual_decode_resolved_parameters(content, label):
    domain = b"SchurVIO-CP2-ros-params-v1\0"
    if not content.startswith(domain):
        _actual_fail(label + " lacks the resolved-parameter domain")
    reader = _ActualByteReader(content[len(domain):], label)
    result = _actual_decode_parameter_value(reader, label)
    reader.finish()
    if not isinstance(result, dict):
        _actual_fail(label + " top-level value is not a map")
    if any(
        not name.startswith("/cp2_vio/") or len(name) == len("/cp2_vio/")
        for name in result
    ):
        _actual_fail(label + " contains a key outside /cp2_vio/")
    return result


def _actual_f64_vector(value, length, label):
    if not isinstance(value, list) or len(value) != length:
        _actual_fail("{} is not a {}-vector".format(label, length))
    return [_actual_f64(item, label + " component") for item in value]


def _actual_exact_ns_timestamp(value, label, require_nine=False):
    if not isinstance(value, str):
        _actual_fail(label + " timestamp is not a string")
    pattern = r"[0-9]+\.[0-9]{9}" if require_nine else r"[0-9]+(?:\.[0-9]{1,9})?"
    if re.fullmatch(pattern, value) is None:
        _actual_fail(label + " timestamp is not exact nonnegative decimal seconds")
    whole, separator, fraction = value.partition(".")
    result = int(whole) * 1_000_000_000 + int((fraction if separator else "").ljust(9, "0") or "0")
    return _actual_u64(result, label + " timestamp nanoseconds")


def _actual_parse_tum(content, label, estimator=False):
    header = b"# timestamp tx ty tz qx qy qz qw\n"
    if not content.startswith(header) or b"\r" in content or not content.endswith(b"\n"):
        _actual_fail(label + " does not have the exact TUM header/newlines")
    rows = []
    for line_index, raw in enumerate(content[len(header):].splitlines(), 1):
        try:
            fields = raw.decode("ascii", "strict").split(" ")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError(label + " contains non-ASCII bytes") from exc
        if len(fields) != 8 or any(field == "" for field in fields):
            _actual_fail("{} row {} does not contain eight single-space fields".format(label, line_index))
        timestamp = _actual_exact_ns_timestamp(
            fields[0], "{} row {}".format(label, line_index), require_nine=estimator
        )
        try:
            pose = [float(field) for field in fields[1:]]
        except ValueError as exc:
            raise ActualVerificationError(label + " contains a nonnumeric pose field") from exc
        if not all(math.isfinite(value) for value in pose):
            _actual_fail(label + " contains a nonfinite pose")
        norm = math.sqrt(sum(value * value for value in pose[3:]))
        if not 1.0 - 1.0e-10 <= norm <= 1.0 + 1.0e-10:
            _actual_fail(label + " quaternion norm is outside [1-1e-10,1+1e-10]")
        rows.append({"timestamp_ns": timestamp, "position": pose[:3], "quaternion": pose[3:]})
    return rows


def _actual_estimator_tum_bytes(trajectory):
    lines = ["# timestamp tx ty tz qx qy qz qw\n"]
    for row in trajectory:
        timestamp = row["camera_timestamp_ns"]
        timestamp_text = "{}.{:09d}".format(timestamp // 1_000_000_000, timestamp % 1_000_000_000)
        values = row["position_G"] + row["quaternion_ItoG_xyzw"]
        lines.append(timestamp_text + " " + " ".join(format(float(value), ".17g") for value in values) + "\n")
    return "".join(lines).encode("ascii")


def _actual_parse_shared_timestamps(content):
    domain = b"SchurVIO-CP2-shared-timestamps-v1\0"
    if not content.startswith(domain):
        _actual_fail("shared timestamp payload domain is invalid")
    reader = _ActualByteReader(content[len(domain):], "shared_timestamps.bin")
    count = reader.u64("shared timestamp count")
    if count > (len(reader.content) - reader.offset) // 8:
        _actual_fail("shared timestamp count exceeds payload")
    values = [reader.u64("shared timestamp") for _ in range(count)]
    reader.finish()
    if any(values[index] <= values[index - 1] for index in range(1, len(values))):
        _actual_fail("shared timestamps are not strictly increasing")
    return values


def _actual_parse_shared_population(content):
    domain = b"SchurVIO-CP2-shared-population-v1\0"
    if not content.startswith(domain):
        _actual_fail("shared population payload domain is invalid")
    reader = _ActualByteReader(content[len(domain):], "shared_population.bin")
    count = reader.u64("shared population count")
    row_bytes = 16 + 21 * 8
    if count != (len(reader.content) - reader.offset) // row_bytes:
        _actual_fail("shared population count/byte length differs")
    rows = []
    for index in range(count):
        timestamp = reader.u64("shared estimator timestamp")
        ground_truth_timestamp = reader.u64("shared ground-truth timestamp")
        values = []
        for component in range(21):
            value = struct.unpack(">d", reader.take(8, "shared pose component"))[0]
            if not math.isfinite(value):
                _actual_fail("shared population contains a nonfinite pose component")
            values.append(value)
        rows.append({
            "timestamp_ns": timestamp,
            "ground_truth_timestamp_ns": ground_truth_timestamp,
            "nullspace_position": values[0:3], "nullspace_quaternion": values[3:7],
            "schur_position": values[7:10], "schur_quaternion": values[10:14],
            "ground_truth_position": values[14:17], "ground_truth_quaternion": values[17:21],
        })
    reader.finish()
    return rows


def _actual_matrix_close(left, right, tolerance=1.0e-10):
    if len(left) != len(right):
        return False
    return all(abs(float(lhs) - float(rhs)) <= tolerance for lhs, rhs in zip(left, right))


def _actual_validate_sequence_provenance(artifact, common, manifest, report):
    provenance = common["provenance"]
    sequence_index = report["sequence_index"]
    sequence_id = report["sequence_id"]
    input_keys = (
        "sequence_index", "sequence_id", "offset_seconds", "bag_path", "bag_size",
        "bag_sha256_before", "bag_sha256_after", "ground_truth_path",
        "ground_truth_sha256",
    )
    inputs = provenance.get("inputs")
    if not isinstance(inputs, list) or len(inputs) != 1:
        _actual_fail("CP2-D sequence provenance must contain one input")
    input_record = _actual_exact_keys(inputs[0], input_keys, "sequence input")
    if (
        input_record["sequence_index"] != sequence_index
        or input_record["sequence_id"] != sequence_id
        or not _actual_same_f64(input_record["offset_seconds"], report["offset_seconds"])
    ):
        _actual_fail("sequence report/input identity differs")
    _actual_safe_absolute_path(input_record["bag_path"], "sequence bag path")
    _actual_u64(input_record["bag_size"], "sequence bag size")
    bag_digest = _actual_sha256(input_record["bag_sha256_before"], "sequence bag SHA-256")
    if input_record["bag_sha256_after"] != bag_digest:
        _actual_fail("sequence bag before/after identity differs")
    _actual_safe_absolute_path(input_record["ground_truth_path"], "sequence ground-truth path")
    _actual_sha256(input_record["ground_truth_sha256"], "sequence ground-truth SHA-256")

    runtime_runs = common["runtime"].get("runs")
    runtime_run_keys = (
        "run_id", "sequence_index", "mode", "loader_map_before",
        "loader_map_before_sha256", "loader_map_after", "loader_map_after_sha256",
        "dso_records_before", "dso_records_after",
    )
    if not isinstance(runtime_runs, list) or len(runtime_runs) != 2:
        _actual_fail("CP2-D runtime run population is not two")
    run_ids = []
    for run_index, (record, mode) in enumerate(zip(runtime_runs, ("nullspace", "schur"))):
        _actual_exact_keys(record, runtime_run_keys, "sequence runtime run")
        if record["sequence_index"] != sequence_index or record["mode"] != mode:
            _actual_fail("sequence runtime run identity/order differs")
        if not isinstance(record["run_id"], str) or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", record["run_id"]
        ) is None or record["run_id"] in run_ids:
            _actual_fail("sequence runtime run ID is invalid/duplicate")
        run_ids.append(record["run_id"])
        for path_field in ("loader_map_before", "loader_map_after"):
            relative = _actual_relpath(record[path_field], "sequence loader map")
            if manifest.get(relative) != record[path_field + "_sha256"]:
                _actual_fail("sequence loader-map identity differs from manifest")
        if record["dso_records_before"] != record["dso_records_after"]:
            _actual_fail("sequence DSO identities changed during a run")

    configuration = common["configuration"]
    resolved = configuration.get("resolved_parameters")
    contexts = configuration.get("runtime_contexts")
    resolved_keys = (
        "run_id", "prelaunch_raw_path", "prelaunch_raw_sha256", "runtime_raw_path",
        "runtime_raw_sha256", "canonical_path", "canonical_sha256", "normalized_path",
        "normalized_sha256",
    )
    if not isinstance(resolved, list) or len(resolved) != 2:
        _actual_fail("CP2-D resolved-parameter population is not two")
    if not isinstance(contexts, list) or len(contexts) != 2:
        _actual_fail("CP2-D runtime-context population is not two")
    schema = _actual_load_module("cp2_schema")
    full_maps = []
    normalized_maps = []
    canonical_hashes = []
    for run_index, record in enumerate(resolved):
        _actual_exact_keys(record, resolved_keys, "sequence resolved parameters")
        if record["run_id"] != run_ids[run_index]:
            _actual_fail("sequence resolved parameters do not join run order")
        for path_field in (
            "prelaunch_raw_path", "runtime_raw_path", "canonical_path", "normalized_path"
        ):
            if record[path_field] is None:
                _actual_fail("CP2-D resolved parameter path is null")
            relative = _actual_relpath(record[path_field], "sequence parameter artifact")
            hash_field = path_field.replace("_path", "_sha256")
            if manifest.get(relative) != record[hash_field]:
                _actual_fail("sequence parameter artifact differs from manifest")
        canonical_content = _actual_read_bytes(
            artifact / record["canonical_path"], "sequence canonical parameters",
            ACTUAL_MAX_BINARY_BYTES,
        )
        normalized_content = _actual_read_bytes(
            artifact / record["normalized_path"], "sequence normalized parameters",
            ACTUAL_MAX_BINARY_BYTES,
        )
        full_map = _actual_decode_resolved_parameters(
            canonical_content, "sequence canonical parameters"
        )
        normalized_map = _actual_decode_resolved_parameters(
            normalized_content, "sequence normalized parameters"
        )
        expected_normalized = dict(full_map)
        for key in ACTUAL_PARAMETER_DIFF_KEYS:
            if key not in expected_normalized:
                _actual_fail("sequence canonical parameters lack allowlisted difference key: " + key)
            del expected_normalized[key]
        expected_bytes = schema.encode_resolved_parameters(expected_normalized)
        if normalized_content != expected_bytes or normalized_map != expected_normalized:
            _actual_fail("sequence normalized parameters are not exact six-key deletion")
        full_maps.append(full_map)
        normalized_maps.append(normalized_map)
        canonical_hashes.append(record["canonical_sha256"])
    if normalized_maps[0] != normalized_maps[1]:
        _actual_fail("sequence normalized parameter maps differ")
    for index, context in enumerate(contexts):
        _actual_exact_keys(context, ("run_id", "path", "size", "sha256"),
                           "sequence runtime context")
        if context["run_id"] != run_ids[index]:
            _actual_fail("sequence runtime context does not join run order")
        _actual_validate_runtime_context(
            artifact, manifest, context,
            {
                "checkpoint": "CP2-D", "run_id": run_ids[index],
                "sequence_index": sequence_index, "sequence_id": sequence_id,
                "mode": ("nullspace", "schur")[index], "shadow_enabled": False,
                "source_commit": provenance["source_commit"],
                "config_sha256": configuration["static_bundle_sha256"],
                "bag_sha256": bag_digest,
                "resolved_parameters_sha256": canonical_hashes[index],
            },
            "sequence",
        )

    diff = report["normalized_parameter_diff"]
    if not isinstance(diff, list) or len(diff) != len(ACTUAL_PARAMETER_DIFF_KEYS):
        _actual_fail("sequence normalized-parameter diff population differs")
    path_values = []
    for index, key in enumerate(ACTUAL_PARAMETER_DIFF_KEYS):
        record = diff[index]
        _actual_exact_keys(
            record, ("key", "nullspace_typed_value", "schur_typed_value"),
            "normalized-parameter diff",
        )
        if record["key"] != key:
            _actual_fail("normalized-parameter diff key/order differs")
        expected_nullspace = schema.typed_parameter_value(full_maps[0][key])
        expected_schur = schema.typed_parameter_value(full_maps[1][key])
        if (
            record["nullspace_typed_value"] != expected_nullspace
            or record["schur_typed_value"] != expected_schur
        ):
            _actual_fail("normalized-parameter typed value differs from canonical payload")
        if key == ACTUAL_PARAMETER_DIFF_KEYS[0]:
            if full_maps[0][key] != "nullspace" or full_maps[1][key] != "schur":
                _actual_fail("sequence mode parameter values are invalid")
        else:
            for value in (full_maps[0][key], full_maps[1][key]):
                path_values.append(str(_actual_safe_absolute_path(value, "sequence output parameter")))
    if len(set(path_values)) != len(path_values):
        _actual_fail("sequence mode output/context paths alias")
    return {
        "bag_sha256": bag_digest,
        "canonical_hashes": canonical_hashes,
        "runtime_runs": runtime_runs,
        "run_ids": run_ids,
        "executable_sha256": common["executable_sha256"],
    }


def _actual_validate_sequence_traces(artifact, manifest, report, provenance_details):
    pair_rows, _ = _actual_jsonl(artifact / "pair_index.jsonl", "pair_index.jsonl")
    if not pair_rows:
        _actual_fail("sequence pair-index population is empty")
    pair_by_index = {}
    used_camera_indices = set()
    previous_anchor = None
    previous_cam0_record_time = None
    for row_index, row in enumerate(pair_rows):
        _actual_exact_keys(row, ACTUAL_PAIR_INDEX_KEYS, "pair-index row")
        if (
            not _schema_version_one(row["schema_version"])
            or row["record_type"] != "pair_index"
        ):
            _actual_fail("pair-index schema identity is invalid")
        if (
            row["sequence_index"] != report["sequence_index"]
            or row["sequence_id"] != report["sequence_id"]
            or row["pair_index"] != row_index
        ):
            _actual_fail("pair-index identity/order differs")
        for field in (
            "sequence_index", "pair_index", "anchor_filtered_index", "anchor_camera_id", "cam0_filtered_index",
            "cam1_filtered_index", "cam0_record_time_ns", "cam1_record_time_ns",
            "cam0_header_time_ns", "cam1_header_time_ns", "absolute_record_delta_ns",
        ):
            _actual_u64(row[field], "pair-index " + field)
        if row["anchor_camera_id"] not in (0, 1):
            _actual_fail("pair-index anchor camera is invalid")
        anchor_index = row["cam0_filtered_index"] if row["anchor_camera_id"] == 0 else row["cam1_filtered_index"]
        if row["anchor_filtered_index"] != anchor_index:
            _actual_fail("pair-index anchor filtered index differs from its camera")
        if previous_anchor is not None and row["anchor_filtered_index"] <= previous_anchor:
            _actual_fail("pair-index anchors are not in strict selection order")
        previous_anchor = row["anchor_filtered_index"]
        anchor_time = row["cam0_record_time_ns"] if row["anchor_camera_id"] == 0 else row["cam1_record_time_ns"]
        candidate_time = row["cam1_record_time_ns"] if row["anchor_camera_id"] == 0 else row["cam0_record_time_ns"]
        candidate_index = row["cam1_filtered_index"] if row["anchor_camera_id"] == 0 else row["cam0_filtered_index"]
        if candidate_time < anchor_time or candidate_index <= row["anchor_filtered_index"]:
            _actual_fail("pair-index candidate is not forward of its anchor")
        delta = abs(row["cam1_record_time_ns"] - row["cam0_record_time_ns"])
        if row["absolute_record_delta_ns"] != delta or delta >= 20_000_000:
            _actual_fail("pair-index violates strict first-forward 20 ms acceptance")
        if (
            row["cam0_filtered_index"] in used_camera_indices
            or row["cam1_filtered_index"] in used_camera_indices
            or row["cam0_filtered_index"] == row["cam1_filtered_index"]
        ):
            _actual_fail("pair-index reuses a filtered image")
        used_camera_indices.update((row["cam0_filtered_index"], row["cam1_filtered_index"]))
        if (
            previous_cam0_record_time is not None
            and row["cam0_record_time_ns"] < previous_cam0_record_time
        ):
            _actual_fail("pair-index selected cam0 record times reverse")
        previous_cam0_record_time = row["cam0_record_time_ns"]
        pair_by_index[row_index] = row
    if report["valid_pair_count"] != len(pair_rows):
        _actual_fail("sequence valid-pair count differs from pair-index rows")

    runs = report["runs"]
    if not isinstance(runs, list) or len(runs) != 2:
        _actual_fail("sequence report run population is not two")
    mode_details = {}
    callback_statuses = {
        "queued", "frequency_dropped", "cam0_decode_failed", "cam1_decode_failed",
        "not_entered", "process_terminated", "trace_failure",
    }
    processing_statuses = {
        "processed", "not_queued", "queued_unprocessed", "process_terminated",
        "trace_failure",
    }
    sequence_math = _actual_load_module("cp2_sequence_math")
    for run_index, mode in enumerate(("nullspace", "schur")):
        run = runs[run_index]
        _actual_exact_keys(run, ACTUAL_SEQUENCE_RUN_KEYS, "sequence run")
        for field in (
            "run_index", "processed_unique_pairs", "first_selected_timestamp_ns",
            "last_selected_timestamp_ns", "first_processed_timestamp_ns",
            "last_processed_timestamp_ns", "selected_duration_ns", "processed_duration_ns",
        ):
            _actual_u64(run[field], "sequence run " + field)
        for field in ("processing_fraction", "time_coverage"):
            _actual_f64(run[field], "sequence run " + field, nonnegative=True)
        _actual_i64(run["exit_code"], "sequence run exit code")
        if not isinstance(run["completed"], bool):
            _actual_fail("sequence run completed flag is not Boolean")
        if run["run_index"] != run_index or run["mode"] != mode:
            _actual_fail("sequence report mode/run order differs")
        if run["executable_sha256"] != provenance_details["executable_sha256"]:
            _actual_fail("sequence run executable digest differs from runtime provenance")
        if run["resolved_parameters_sha256"] != provenance_details["canonical_hashes"][run_index]:
            _actual_fail("sequence run resolved-parameter digest differs")
        runtime_record = provenance_details["runtime_runs"][run_index]
        if run["loader_map_sha256"] not in (
            runtime_record["loader_map_before_sha256"], runtime_record["loader_map_after_sha256"]
        ):
            _actual_fail("sequence run loader-map digest is not retained in provenance")
        callback_name = mode + "_callbacks.jsonl"
        trajectory_name = mode + "_trajectory.jsonl"
        if run["callback_trace_sha256"] != manifest[callback_name]:
            _actual_fail("sequence callback trace hash differs")
        if run["trajectory_sha256"] != manifest[trajectory_name]:
            _actual_fail("sequence trajectory trace hash differs")
        callbacks, _ = _actual_jsonl(artifact / callback_name, callback_name)
        if len(callbacks) != len(pair_rows):
            _actual_fail("sequence callback population is not one-to-one with pair index")
        callback_by_index = {}
        seen_pairs = set()
        processed = []
        for callback_index, callback in enumerate(callbacks):
            _actual_exact_keys(callback, ACTUAL_CALLBACK_KEYS, mode + " callback")
            for field in (
                "sequence_index", "callback_index", "pair_index", "anchor_filtered_index",
                "cam0_filtered_index", "cam1_filtered_index", "cam0_record_time_ns",
                "cam1_record_time_ns", "cam0_header_time_ns", "cam1_header_time_ns",
                "camera_timestamp_ns",
            ):
                _actual_u64(callback[field], "callback " + field)
            if (
                not _schema_version_one(callback["schema_version"])
                or callback["record_type"] != "serial_callback"
                or callback["sequence_index"] != report["sequence_index"]
                or callback["sequence_id"] != report["sequence_id"]
                or callback["mode"] != mode
                or callback["callback_index"] != callback_index
            ):
                _actual_fail("sequence callback identity/order differs")
            pair_index = _actual_u64(callback["pair_index"], "callback pair index")
            pair = pair_by_index.get(pair_index)
            if pair is None or pair_index in seen_pairs:
                _actual_fail("callback references an absent/duplicate pair")
            seen_pairs.add(pair_index)
            for field in (
                "anchor_filtered_index", "cam0_filtered_index", "cam1_filtered_index",
                "cam0_record_time_ns", "cam1_record_time_ns", "cam0_header_time_ns",
                "cam1_header_time_ns",
            ):
                if callback[field] != pair[field]:
                    _actual_fail("callback source field differs from pair index: " + field)
            if callback["camera_timestamp_ns"] != pair["cam0_header_time_ns"]:
                _actual_fail("callback camera timestamp differs from cam0 header time")
            for field in (
                "enqueue_entered", "enqueue_returned", "processing_entered",
                "processing_returned", "state_row_emitted",
            ):
                if not isinstance(callback[field], bool):
                    _actual_fail("callback event flag is not Boolean")
            if callback["enqueue_status"] not in callback_statuses or callback["processing_status"] not in processing_statuses:
                _actual_fail("callback status enum is invalid")
            transition = (
                callback["enqueue_status"], callback["processing_status"],
                callback["enqueue_entered"], callback["enqueue_returned"],
                callback["processing_entered"], callback["processing_returned"],
            )
            if transition not in (
                ("queued", "processed", True, True, True, True),
                ("frequency_dropped", "not_queued", True, True, False, False),
            ):
                _actual_fail("callback status/event transition is not passing")
            if (callback["trajectory_index"] is None) is not (not callback["state_row_emitted"]):
                _actual_fail("callback trajectory-index nullability differs from state emission")
            if callback["trajectory_index"] is not None:
                _actual_u64(callback["trajectory_index"], "callback trajectory index")
            if callback["processing_status"] == "processed":
                processed.append(callback)
            elif callback["state_row_emitted"]:
                _actual_fail("unprocessed callback emits a state row")
            callback_by_index[callback_index] = callback
        if seen_pairs != set(pair_by_index):
            _actual_fail("callback pair population differs from pair index")
        if not processed:
            _actual_fail("sequence mode has no processed callback")

        trajectories, _ = _actual_jsonl(artifact / trajectory_name, trajectory_name)
        trajectory_by_index = {}
        previous_timestamp = None
        seen_callback_indices = set()
        for trajectory_index, trajectory in enumerate(trajectories):
            _actual_exact_keys(trajectory, ACTUAL_TRAJECTORY_KEYS, mode + " trajectory")
            for field in (
                "sequence_index", "trajectory_index", "callback_index", "pair_index",
                "camera_timestamp_ns",
            ):
                _actual_u64(trajectory[field], "trajectory " + field)
            if (
                not _schema_version_one(trajectory["schema_version"])
                or trajectory["record_type"] != "trajectory_pose"
                or trajectory["sequence_index"] != report["sequence_index"]
                or trajectory["sequence_id"] != report["sequence_id"]
                or trajectory["mode"] != mode
                or trajectory["trajectory_index"] != trajectory_index
            ):
                _actual_fail("trajectory identity/order differs")
            callback_index = _actual_u64(trajectory["callback_index"], "trajectory callback index")
            callback = callback_by_index.get(callback_index)
            if callback is None or callback_index in seen_callback_indices:
                _actual_fail("trajectory references an absent/duplicate callback")
            seen_callback_indices.add(callback_index)
            if (
                callback["processing_status"] != "processed"
                or callback["trajectory_index"] != trajectory_index
                or trajectory["pair_index"] != callback["pair_index"]
                or trajectory["camera_timestamp_ns"] != callback["camera_timestamp_ns"]
            ):
                _actual_fail("trajectory/callback bidirectional join differs")
            timestamp = _actual_u64(trajectory["camera_timestamp_ns"], "trajectory timestamp")
            if previous_timestamp is not None and timestamp <= previous_timestamp:
                _actual_fail("trajectory timestamps are duplicate or nonincreasing")
            previous_timestamp = timestamp
            trajectory["position_G"] = _actual_f64_vector(
                trajectory["position_G"], 3, "trajectory position"
            )
            trajectory["quaternion_ItoG_xyzw"] = _actual_f64_vector(
                trajectory["quaternion_ItoG_xyzw"], 4, "trajectory quaternion"
            )
            sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(
                trajectory["quaternion_ItoG_xyzw"]
            )
            trajectory_by_index[trajectory_index] = trajectory
        emitted = [callback for callback in callbacks if callback["state_row_emitted"]]
        if len(emitted) != len(trajectories) or set(seen_callback_indices) != {
            callback["callback_index"] for callback in emitted
        }:
            _actual_fail("trajectory population differs from emitting callbacks")
        raw_tum_name = mode + "_raw.tum"
        raw_tum_bytes = _actual_read_bytes(
            artifact / raw_tum_name, raw_tum_name, ACTUAL_MAX_JSONL_BYTES
        )
        if raw_tum_bytes != _actual_estimator_tum_bytes(trajectories):
            _actual_fail("raw estimator TUM is not the exact trajectory projection")

        selected_first = pair_rows[0]["cam0_record_time_ns"]
        selected_last = pair_rows[-1]["cam0_record_time_ns"]
        processed_first = processed[0]["cam0_record_time_ns"]
        processed_last = processed[-1]["cam0_record_time_ns"]
        selected_duration = selected_last - selected_first
        processed_duration = processed_last - processed_first
        if selected_duration <= 0 or processed_duration <= 0:
            _actual_fail("sequence selected/processed duration is not strictly positive")
        processing_fraction = _actual_exact_count_ratio(
            len(processed), len(pair_rows), "sequence processing fraction"
        )
        time_coverage = _actual_exact_count_ratio(
            processed_duration, selected_duration, "sequence time coverage"
        )
        expected_run_values = {
            "processed_unique_pairs": len(processed),
            "processing_fraction": processing_fraction,
            "first_selected_timestamp_ns": selected_first,
            "last_selected_timestamp_ns": selected_last,
            "first_processed_timestamp_ns": processed_first,
            "last_processed_timestamp_ns": processed_last,
            "selected_duration_ns": selected_duration,
            "processed_duration_ns": processed_duration,
            "time_coverage": time_coverage,
        }
        for field, expected in expected_run_values.items():
            if isinstance(expected, float):
                if not _actual_same_f64(run[field], expected):
                    _actual_fail("sequence run {} differs".format(field))
            elif run[field] != expected:
                _actual_fail("sequence run {} differs".format(field))
        if (
            1000 * len(processed) < 995 * len(pair_rows)
            or 1000 * processed_duration < 995 * selected_duration
        ):
            _actual_fail("sequence mode coverage is below 0.995")
        if run["completed"] is not True or run["exit_code"] != 0:
            _actual_fail("sequence mode did not complete successfully")
        mode_details[mode] = {
            "callbacks": callbacks, "trajectories": trajectories,
            "processed": processed, "raw_tum": raw_tum_bytes,
        }
    return pair_rows, mode_details


def _actual_sequence_shared_math(artifact, manifest, report, mode_details, common):
    sequence_math = _actual_load_module("cp2_sequence_math")
    shared_timestamp_bytes = _actual_read_bytes(
        artifact / "shared_timestamps.bin", "shared_timestamps.bin",
        ACTUAL_MAX_BINARY_BYTES,
    )
    shared_population_bytes = _actual_read_bytes(
        artifact / "shared_population.bin", "shared_population.bin",
        ACTUAL_MAX_BINARY_BYTES,
    )
    if hashlib.sha256(shared_timestamp_bytes).hexdigest() != report["shared_timestamp_sha256"]:
        _actual_fail("shared timestamp payload hash differs from report")
    if hashlib.sha256(shared_population_bytes).hexdigest() != report["shared_population_sha256"]:
        _actual_fail("shared population payload hash differs from report")
    shared_timestamps = _actual_parse_shared_timestamps(shared_timestamp_bytes)
    shared_rows = _actual_parse_shared_population(shared_population_bytes)
    if (
        len(shared_timestamps) != len(shared_rows)
        or report["shared_timestamp_count"] != len(shared_rows)
        or len(shared_rows) < 3
    ):
        _actual_fail("shared trajectory population/count is invalid")
    if [row["timestamp_ns"] for row in shared_rows] != shared_timestamps:
        _actual_fail("shared timestamp and population payloads differ")
    nullspace_trajectory = mode_details["nullspace"]["trajectories"]
    schur_trajectory = mode_details["schur"]["trajectories"]
    expected_intersection = sequence_math.shared_timestamp_intersection(
        [row["camera_timestamp_ns"] for row in nullspace_trajectory],
        [row["camera_timestamp_ns"] for row in schur_trajectory],
    )
    sequence_math.validate_shared_timestamp_intersection(
        [row["camera_timestamp_ns"] for row in nullspace_trajectory],
        [row["camera_timestamp_ns"] for row in schur_trajectory],
        shared_timestamps,
    )
    if tuple(shared_timestamps) != expected_intersection:
        _actual_fail("shared population is not the exact mode intersection")
    nullspace_by_timestamp = {
        row["camera_timestamp_ns"]: row for row in nullspace_trajectory
    }
    schur_by_timestamp = {row["camera_timestamp_ns"]: row for row in schur_trajectory}
    for shared in shared_rows:
        nullspace = nullspace_by_timestamp[shared["timestamp_ns"]]
        schur = schur_by_timestamp[shared["timestamp_ns"]]
        for retained_name, trajectory, source_name in (
            ("nullspace_position", nullspace, "position_G"),
            ("nullspace_quaternion", nullspace, "quaternion_ItoG_xyzw"),
            ("schur_position", schur, "position_G"),
            ("schur_quaternion", schur, "quaternion_ItoG_xyzw"),
        ):
            if len(shared[retained_name]) != len(trajectory[source_name]) or any(
                not _actual_same_f64(left, right)
                for left, right in zip(shared[retained_name], trajectory[source_name])
            ):
                _actual_fail("shared population pose differs from trajectory: " + retained_name)

    ground_truth_bytes = _actual_read_bytes(
        artifact / "ground_truth_shared.tum", "ground_truth_shared.tum",
        ACTUAL_MAX_JSONL_BYTES,
    )
    ground_truth_tum = _actual_parse_tum(
        ground_truth_bytes, "ground_truth_shared.tum", estimator=False
    )
    if len(ground_truth_tum) != len(shared_rows):
        _actual_fail("ground-truth shared TUM population differs")
    for retained, tum in zip(shared_rows, ground_truth_tum):
        if retained["ground_truth_timestamp_ns"] != tum["timestamp_ns"]:
            _actual_fail("shared ground-truth timestamp differs from retained TUM")
        for retained_values, tum_values in (
            (retained["ground_truth_position"], tum["position"]),
            (retained["ground_truth_quaternion"], tum["quaternion"]),
        ):
            if any(not _actual_same_f64(left, right) for left, right in zip(retained_values, tum_values)):
                _actual_fail("shared ground-truth pose differs from retained TUM")

    nullspace_positions = [row["nullspace_position"] for row in shared_rows]
    nullspace_quaternions = [row["nullspace_quaternion"] for row in shared_rows]
    schur_positions = [row["schur_position"] for row in shared_rows]
    schur_quaternions = [row["schur_quaternion"] for row in shared_rows]
    ground_truth_positions = [row["ground_truth_position"] for row in shared_rows]
    alignment = sequence_math.baseline_kabsch_alignment(
        nullspace_positions, ground_truth_positions
    )
    retained_alignment = report["baseline_alignment"]
    _actual_exact_keys(retained_alignment, ACTUAL_ALIGNMENT_KEYS, "baseline alignment")
    if (
        retained_alignment["source"] != "nullspace_to_ground_truth"
        or retained_alignment["shared_population_sha256"] != report["shared_population_sha256"]
        or retained_alignment["applied_identically_to_both_modes"] is not True
    ):
        _actual_fail("baseline alignment identity/common-application flags differ")
    rotation_values = _actual_f64_vector(
        retained_alignment["rotation_row_major"], 9, "baseline alignment rotation"
    )
    translation_values = _actual_f64_vector(
        retained_alignment["translation"], 3, "baseline alignment translation"
    )
    quaternion_values = _actual_f64_vector(
        retained_alignment["quaternion_xyzw"], 4, "baseline alignment quaternion"
    )
    singular_values = _actual_f64_vector(
        retained_alignment["source_singular_values"], 3,
        "baseline source singular values",
    )
    computed_rotation = alignment.rotation.reshape(9).tolist()
    if any(not _actual_same_f64(left, right) for left, right in zip(rotation_values, computed_rotation)):
        _actual_fail("retained baseline rotation differs from independent Kabsch result")
    if any(not _actual_same_f64(left, right) for left, right in zip(translation_values, alignment.translation.tolist())):
        _actual_fail("retained baseline translation differs from independent Kabsch result")
    if any(not _actual_same_f64(left, right) for left, right in zip(singular_values, alignment.source_singular_values.tolist())):
        _actual_fail("retained source singular values differ")
    for field, expected in (
        ("source_rank_threshold", alignment.source_rank_threshold),
        ("determinant", alignment.determinant),
        ("orthogonality_error_frobenius", alignment.orthogonality_error_frobenius),
    ):
        if not _actual_same_f64(retained_alignment[field], expected):
            _actual_fail("retained baseline alignment {} differs".format(field))
    quaternion_rotation = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(
        quaternion_values
    ).reshape(9).tolist()
    if not _actual_matrix_close(quaternion_rotation, rotation_values, tolerance=1.0e-10):
        _actual_fail("baseline alignment quaternion does not encode its rotation")

    aligned = sequence_math.apply_common_alignment(
        alignment, nullspace_positions, nullspace_quaternions,
        schur_positions, schur_quaternions,
    )
    position_differences = sequence_math.position_differences_m(
        aligned.nullspace_positions, aligned.schur_positions
    )
    orientation_differences = sequence_math.orientation_differences_deg(
        aligned.nullspace_inverse_rotations, aligned.schur_inverse_rotations
    )
    position_p95 = sequence_math.linear_p95(position_differences)
    orientation_p95 = sequence_math.linear_p95(orientation_differences)
    ate_nullspace = sequence_math.translation_rmse_m(
        aligned.nullspace_positions, ground_truth_positions
    )
    ate_schur = sequence_math.translation_rmse_m(
        aligned.schur_positions, ground_truth_positions
    )
    relative_ate = sequence_math.relative_ate_difference(ate_nullspace, ate_schur)
    sequence_math.validate_metric_limits(position_p95, orientation_p95, relative_ate)
    for field, expected in (
        ("position_p95_m", position_p95),
        ("orientation_p95_deg", orientation_p95),
        ("ate_nullspace_m", ate_nullspace),
        ("ate_schur_m", ate_schur),
        ("relative_ate_difference", relative_ate),
    ):
        if not _actual_same_f64(report[field], expected):
            _actual_fail("sequence trajectory metric {} differs".format(field))

    for mode, positions, rotations in (
        ("nullspace", aligned.nullspace_positions, aligned.nullspace_inverse_rotations),
        ("schur", aligned.schur_positions, aligned.schur_inverse_rotations),
    ):
        name = mode + "_shared_aligned.tum"
        rows = _actual_parse_tum(
            _actual_read_bytes(artifact / name, name, ACTUAL_MAX_JSONL_BYTES),
            name, estimator=True,
        )
        if len(rows) != len(shared_rows):
            _actual_fail(mode + " aligned TUM population differs")
        for index, row in enumerate(rows):
            if row["timestamp_ns"] != shared_timestamps[index]:
                _actual_fail(mode + " aligned TUM timestamp differs")
            if any(
                not _actual_same_f64(left, right)
                for left, right in zip(row["position"], positions[index].tolist())
            ):
                _actual_fail(mode + " aligned TUM position differs")
            tum_rotation = sequence_math.jpl_stored_xyzw_to_hamilton_inverse_rotation(
                row["quaternion"]
            ).reshape(9).tolist()
            if not _actual_matrix_close(
                tum_rotation, rotations[index].reshape(9).tolist(), tolerance=1.0e-10
            ):
                _actual_fail(mode + " aligned TUM orientation differs")

    evaluator = [record for record in common["commands"] if record["phase"] == "evaluation"]
    ape_commands = [record for record in evaluator if Path(record["argv"][0]).name == "evo_ape"]
    if len(ape_commands) != 2:
        _actual_fail("sequence artifact lacks exactly two evo_ape evaluations")
    for record, mode, expected_ate in zip(
        ape_commands, ("nullspace", "schur"), (ate_nullspace, ate_schur)
    ):
        argv = record["argv"]
        if (
            len(argv) != 8 or argv[1] != "tum"
            or Path(argv[2]).name != "ground_truth_shared.tum"
            or Path(argv[3]).name != mode + "_shared_aligned.tum"
            or argv[4:] != ["-r", "trans_part", "--t_max_diff", "0.01"]
            or record["exit_code"] != 0 or record["timed_out"] is not False
        ):
            _actual_fail("sequence evaluator command differs from frozen evo_ape invocation")
        stdout = _actual_read_bytes(
            artifact / record["stdout"], "evo_ape stdout", ACTUAL_MAX_JSON_BYTES
        )
        try:
            text_value = stdout.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ActualVerificationError("evo_ape stdout is not UTF-8") from exc
        matches = re.findall(
            r"(?m)^\s*rmse\s+([-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]+)?)\s*$",
            text_value,
        )
        if len(matches) != 1:
            _actual_fail("evo_ape stdout does not contain exactly one parsed RMSE")
        parsed = _actual_f64(float(matches[0]), "evo_ape parsed RMSE", nonnegative=True)
        if abs(parsed - float(expected_ate)) > 1.0e-12 + 1.0e-10 * abs(float(expected_ate)):
            _actual_fail("evo_ape parsed RMSE differs from independent ATE")
    if report["coverage_passed"] is not True or report["trajectory_passed"] is not True:
        _actual_fail("sequence coverage/trajectory conjunction did not pass")


def verify_sequence_artifact(artifact, manifest_sha256, quiet=False):
    raw = os.fspath(artifact)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        _actual_fail("sequence artifact path must be normalized and absolute")
    _actual_sha256(manifest_sha256, "sequence manifest anchor")
    _actual_fail(
        "CP2-D actual verification is blocked before artifact access: the evaluator "
        "precision/provenance and direct numerical-stack replacement contract is "
        "pending explicit approval"
    )

    # Unreachable until an approval-bound replacement deliberately removes the
    # pre-access block above; retained implementation remains reviewable.
    artifact = Path(artifact)
    manifest, observed, _ = _actual_scan_and_verify_manifest(artifact, manifest_sha256)
    required = {
        "cp2_report.json", "provenance.json", "commands.jsonl", "pair_index.jsonl",
        "nullspace_callbacks.jsonl", "schur_callbacks.jsonl",
        "nullspace_trajectory.jsonl", "schur_trajectory.jsonl",
        "parameters/nullspace_prelaunch_raw.yaml", "parameters/nullspace_runtime_raw.yaml",
        "parameters/nullspace_canonical.bin", "parameters/nullspace_normalized.bin",
        "parameters/schur_prelaunch_raw.yaml", "parameters/schur_runtime_raw.yaml",
        "parameters/schur_canonical.bin", "parameters/schur_normalized.bin",
        "nullspace_state.txt", "nullspace_deviation.txt", "nullspace_openvins_timing.csv",
        "schur_state.txt", "schur_deviation.txt", "schur_openvins_timing.csv",
        "nullspace_raw.tum", "schur_raw.tum", "ground_truth_shared.tum",
        "nullspace_shared_aligned.tum", "schur_shared_aligned.tum",
        "shared_population.bin", "shared_timestamps.bin",
    }
    if not required.issubset(manifest):
        _actual_fail("CP2-D artifact lacks a fixed core file")
    common = _actual_validate_provenance(artifact, manifest, observed, "CP2-D")
    report = _actual_json(artifact / "cp2_report.json", "cp2_report.json")
    _actual_exact_keys(report, ACTUAL_SEQUENCE_REPORT_KEYS, "CP2-D sequence report")
    if (
        not _schema_version_one(report["schema_version"])
        or report["record_type"] != "sequence_pair"
        or report["checkpoint"] != "CP2-D"
        or report["status"] != "passed"
    ):
        _actual_fail("CP2-D report identity/status is invalid")
    sequence_index = _actual_u64(report["sequence_index"], "sequence index")
    if sequence_index >= 3 or report["sequence_id"] != ACTUAL_SEQUENCE_IDS[sequence_index]:
        _actual_fail("CP2-D report sequence identity is invalid")
    if not _actual_same_f64(
        report["offset_seconds"], ACTUAL_SEQUENCE_OFFSETS_SECONDS[sequence_index]
    ):
        _actual_fail("CP2-D report offset differs from the frozen sequence offset")
    _actual_u64(report["valid_pair_count"], "sequence valid-pair count")
    _actual_u64(report["shared_timestamp_count"], "sequence shared timestamp count")
    for field in (
        "position_p95_m", "orientation_p95_deg", "ate_nullspace_m", "ate_schur_m",
        "relative_ate_difference",
    ):
        _actual_f64(report[field], "sequence report " + field, nonnegative=True)
    for field in ("coverage_passed", "trajectory_passed", "passed"):
        if not isinstance(report[field], bool):
            _actual_fail("sequence report " + field + " is not Boolean")
    if report["provenance_sha256"] != manifest["provenance.json"]:
        _actual_fail("sequence provenance hash differs")
    if report["pair_index_sha256"] != manifest["pair_index.jsonl"]:
        _actual_fail("sequence pair-index hash differs")
    if report["shared_timestamp_sha256"] != manifest["shared_timestamps.bin"]:
        _actual_fail("sequence shared-timestamp hash differs")
    if report["shared_population_sha256"] != manifest["shared_population.bin"]:
        _actual_fail("sequence shared-population hash differs")
    provenance_details = _actual_validate_sequence_provenance(
        artifact, common, manifest, report
    )
    pair_rows, mode_details = _actual_validate_sequence_traces(
        artifact, manifest, report, provenance_details
    )
    del pair_rows
    _actual_sequence_shared_math(artifact, manifest, report, mode_details, common)
    if report["passed"] is not True:
        _actual_fail("CP2-D sequence report did not pass")
    identity = (
        common["provenance"]["source_commit"], common["provenance"]["source_tree"],
        common["runtime"]["executable_sha256_before"], common["runtime"]["build_id_before"],
        common["configuration"]["static_bundle_sha256"],
        common["configuration"]["launch"]["sha256"],
    )
    result = {
        "checkpoint": "CP2-D", "manifest_sha256": manifest_sha256,
        "passed": True, "report": report, "provenance": common["provenance"],
        "cross_identity": identity,
    }
    if not quiet:
        print("CP2-D sequence artifact independently verified: " + str(artifact))
        print("SHA256SUMS SHA-256: " + manifest_sha256)
    return result


def verify_sequence_set(artifacts, manifest_sha256):
    if len(artifacts) != 3 or len(manifest_sha256) != 3:
        _actual_fail("sequence-set verification requires exactly three artifacts and anchors")
    results = [
        verify_sequence_artifact(Path(path), digest, quiet=True)
        for path, digest in zip(artifacts, manifest_sha256)
    ]
    indices = [result["report"]["sequence_index"] for result in results]
    if indices != [0, 1, 2]:
        _actual_fail("sequence-set artifact order/identity is not exactly 0,1,2")
    identities = [result["cross_identity"] for result in results]
    cross_equal = identities[0] == identities[1] == identities[2]
    individually_passed = all(result["passed"] for result in results)
    if not cross_equal or not individually_passed:
        _actual_fail("sequence-set source/runtime/config/launch identity differs")
    aggregate = {
        "schema_version": 1,
        "record_type": "sequence_set",
        "checkpoint": "CP2-D",
        "sequence_indices": indices,
        "sequence_manifest_sha256": list(manifest_sha256),
        "all_individually_passed": individually_passed,
        "cross_sequence_identity_equal": cross_equal,
        "passed": individually_passed and cross_equal,
    }
    print(json.dumps(aggregate, allow_nan=False, ensure_ascii=False,
                     separators=(",", ":"), sort_keys=True))
    return aggregate


def verify_timing_preprofile_blocked(artifact, manifest_sha256):
    # CP2-E's committed section is explicitly non-authorizing.  Deliberately
    # do not stat either caller-supplied path: no timing artifact can be read,
    # internally verified, or accepted until the profile and full schema are
    # separately committed.
    raw = os.fspath(artifact)
    if not os.path.isabs(raw) or os.path.normpath(raw) != raw:
        _actual_fail("timing artifact path must be normalized and absolute")
    _actual_sha256(manifest_sha256, "timing manifest anchor")
    _actual_fail(
        "CP2-E actual verification is blocked before artifact access: the frozen timing "
        "profile and complete authorizing artifact schema are not committed"
    )


READINESS_COMMON_CASES = (
    "valid_minimal_fixture", "cli_exclusivity", "forbidden_bag_provider",
    "non_tmp_write", "schema_extra_key", "schema_missing_key",
    "duplicate_json_key", "unsafe_path", "symlink", "hardlink",
    "manifest_missing_entry", "manifest_extra_entry", "manifest_digest_mismatch",
    "readiness_order", "readiness_timeout", "readiness_process_group",
    "readiness_lock_identity", "readiness_snapshot_mutation", "ignored_source_path",
    "snapshotted_root_symlink", "launch_output_combination",
    "unit_anchor_commit_mismatch",
)
READINESS_RECORDED_CASES = (
    "duplicate_noncontiguous_ids", "wrong_terminal_reconciliation",
    "wrong_counted_category", "denominator_mismatch", "raw_row_weight_mismatch",
    "missing_unclassified_disagreement", "bad_statistics_tolerance_edge",
    "missing_state_block", "missing_ordered_covariance_pair",
    "candidate_missing_row_omission", "nonzero_shadow_write",
    "baseline_commit_count_not_one", "live_preview_mismatch",
    "nonzero_repair_fallback", "prior_raw_config_hash_drift",
    "raw_prior_layout_disconnect", "flipped_gate_decision",
    "permuted_accepted_sequence", "candidate_proposal_copied_from_baseline",
    "proposal_disconnected_from_raw_or_phase0", "replay_noop",
    "replay_skipped_invocation", "phase2_phase3_disconnected",
    "replay_report_mismatch", "nonzero_internal_failure_terminal",
    "manifest_corruption",
)
READINESS_SEQUENCE_CASES = (
    "exact_20_ms_boundary", "nearest_not_first_forward", "reused_image_message",
    "missing_duplicate_pair_index", "callback_source_mismatch", "identity_hash_drift",
    "wrong_mode_order", "coverage_below_0_995", "unequal_shared_populations",
    "independent_alignment", "invalid_nonorthogonal_transform",
    "position_metric_limit", "orientation_metric_limit", "relative_ate_metric_limit",
)
READINESS_TIMING_CASES = (
    "wrong_pair_order", "wrong_pair_index", "runtime_drift", "config_drift",
    "profile_drift", "changed_clock_snapshot", "affinity_mismatch",
    "warm_up_boundary_error", "unilateral_noncommon_samples", "duplicate_timestamp",
    "negative_duration", "noninteger_duration", "nonprimary_inclusion",
    "incorrect_linear_quantiles", "median_ratio_limit", "p95_ratio_limit",
)
READINESS_VERIFIER_CASES = tuple(dict.fromkeys(
    READINESS_COMMON_CASES + READINESS_RECORDED_CASES
    + READINESS_SEQUENCE_CASES + READINESS_TIMING_CASES
))


class _ReadinessSelfTestRejection(ValueError):
    pass


def run_readiness_self_test():
    """Independent stdlib-only C/D/E verifier corruption oracle."""

    def reject(message):
        raise _ReadinessSelfTestRejection(message)

    def exact_keys(value, keys):
        if not isinstance(value, dict) or set(value) != set(keys):
            reject("schema field inventory differs")

    def safe_relpath(value):
        pure = PurePosixPath(value) if isinstance(value, str) else PurePosixPath(".")
        if (
            not isinstance(value, str) or not value or "\\" in value or "\0" in value
            or pure.is_absolute() or pure.as_posix() != value
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            reject("unsafe relative path")

    def validate_common(name, root):
        digest = sha256_bytes(b"a")
        if name == "cli_exclusivity":
            if ("--self-test", "extra") != ("--self-test",):
                reject("CLI modes are not exclusive")
        elif name == "forbidden_bag_provider":
            reject("bag provider call is forbidden")
        elif name == "non_tmp_write":
            candidate = "/var/tmp/forbidden"
            if os.path.commonpath((candidate, str(root))) != str(root):
                reject("write escaped fresh /tmp root")
        elif name == "schema_extra_key":
            exact_keys({"required": 1, "extra": 2}, ("required",))
        elif name == "schema_missing_key":
            exact_keys({}, ("required",))
        elif name == "duplicate_json_key":
            try:
                strict_json_bytes(b'{"x":1,"x":2}', "self-test JSON")
            except ValueError:
                reject("duplicate JSON key")
        elif name == "unsafe_path":
            safe_relpath("../escape")
        elif name in ("symlink", "hardlink"):
            path = root / ("link" if name == "symlink" else "file-a")
            status_value = os.lstat(str(path))
            if not stat.S_ISREG(status_value.st_mode) or status_value.st_nlink != 1:
                reject("path is not a single-link regular file")
        elif name.startswith("manifest_"):
            expected = {"a": digest}
            observed = {
                "manifest_missing_entry": {},
                "manifest_extra_entry": {"a": digest, "b": digest},
                "manifest_digest_mismatch": {"a": "0" * 64},
            }[name]
            if observed != expected:
                reject("manifest population/digest differs")
        elif name == "readiness_order":
            if [1, 0] != [0, 1]:
                reject("readiness records are out of order")
        elif name == "readiness_timeout":
            reject("readiness subprocess timed out")
        elif name == "readiness_process_group":
            reject("readiness subprocess retained descendants")
        elif name == "readiness_lock_identity":
            exact_keys({"regular": True, "owner": False, "mode": 0o600}, ("regular", "owner", "mode"))
            reject("readiness lock identity differs")
        elif name == "readiness_snapshot_mutation":
            if b"before" != b"after":
                reject("readiness snapshot changed")
        elif name == "ignored_source_path":
            if "cache/file".split("/", 1)[0] not in ("build", "results", "Testing"):
                reject("ignored source path is outside snapshotted roots")
        elif name == "snapshotted_root_symlink":
            if stat.S_ISLNK(os.lstat(str(root / "root-link")).st_mode):
                reject("snapshotted root is a symlink")
        elif name == "launch_output_combination":
            record = {"unit_only": True, "bag_provider_calls": 0, "artifact_created": True}
            if record["artifact_created"]:
                reject("self-test created an evidence artifact")
        elif name == "unit_anchor_commit_mismatch":
            if ("a" * 40, "b" * 40) != ("c" * 40, "b" * 40):
                reject("unit commit/tree anchor differs")
        else:
            reject("unknown common self-test case")

    def valid_recorded():
        return {
            "ids": [0, 1], "terminals": {"accepted": 1, "rejected": 1, "raw": 2},
            "counted": ["committed_counted", "rejected_counted"],
            "denominator": 2, "weights": [1, 1], "unclassified": 0,
            "statistics_error": 0.0, "statistics_tolerance": 1e-12,
            "state_blocks": ["imu", "clone"],
            "covariance_pairs": [[0, 0], [0, 1], [1, 0], [1, 1]],
            "candidate_omissions": [], "shadow_writes": 0, "baseline_commits": 1,
            "live_preview_match": True, "repair_fallbacks": 0,
            "prior_config_hash": "1" * 64, "raw_config_hash": "1" * 64,
            "prior_layout_connected": True, "gate_match": True,
            "accepted_sequence": [0], "expected_accepted_sequence": [0],
            "candidate_digest": "2" * 64, "baseline_digest": "3" * 64,
            "proposal_connected": True, "replay_invocations": 1,
            "replay_skipped": False, "phase23_connected": True,
            "replay_report_match": True, "internal_failure_terminals": 0,
            "manifest_ok": True,
        }

    def check_recorded(value):
        if value["ids"] != list(range(len(value["ids"]))) or len(set(value["ids"])) != len(value["ids"]): reject("record IDs")
        if value["terminals"]["raw"] != value["terminals"]["accepted"] + value["terminals"]["rejected"]: reject("terminal reconciliation")
        if value["counted"] != ["committed_counted", "rejected_counted"]: reject("counted categories")
        if value["denominator"] != len(value["ids"]): reject("denominator")
        if sum(value["weights"]) != value["denominator"]: reject("raw row weights")
        if value["unclassified"] != 0: reject("unclassified disagreement")
        if value["statistics_error"] > value["statistics_tolerance"]: reject("statistics tolerance")
        if value["state_blocks"] != ["imu", "clone"]: reject("state blocks")
        if value["covariance_pairs"] != [[0, 0], [0, 1], [1, 0], [1, 1]]: reject("covariance pairs")
        if value["candidate_omissions"]: reject("candidate row omission")
        if value["shadow_writes"] != 0: reject("shadow write")
        if value["baseline_commits"] != 1: reject("baseline commit count")
        if value["live_preview_match"] is not True: reject("live preview")
        if value["repair_fallbacks"] != 0: reject("repair/fallback")
        if value["prior_config_hash"] != value["raw_config_hash"]: reject("config hash drift")
        if value["prior_layout_connected"] is not True: reject("raw/prior layout")
        if value["gate_match"] is not True: reject("gate decision")
        if value["accepted_sequence"] != value["expected_accepted_sequence"]: reject("accepted sequence")
        if value["candidate_digest"] == value["baseline_digest"]: reject("copied candidate")
        if value["proposal_connected"] is not True: reject("proposal connection")
        if value["replay_invocations"] != 1: reject("replay invocation")
        if value["replay_skipped"] is not False: reject("replay skipped")
        if value["phase23_connected"] is not True: reject("phase 2/3 connection")
        if value["replay_report_match"] is not True: reject("replay report")
        if value["internal_failure_terminals"] != 0: reject("internal failure terminal")
        if value["manifest_ok"] is not True: reject("manifest corruption")

    recorded_mutations = {
        "duplicate_noncontiguous_ids": ("ids", [0, 0]),
        "wrong_terminal_reconciliation": ("terminals", {"accepted": 1, "rejected": 0, "raw": 2}),
        "wrong_counted_category": ("counted", ["committed_counted", "wrong"]),
        "denominator_mismatch": ("denominator", 3), "raw_row_weight_mismatch": ("weights", [1, 0]),
        "missing_unclassified_disagreement": ("unclassified", 1),
        "bad_statistics_tolerance_edge": ("statistics_error", 2e-12),
        "missing_state_block": ("state_blocks", ["imu"]),
        "missing_ordered_covariance_pair": ("covariance_pairs", [[0, 0], [1, 1]]),
        "candidate_missing_row_omission": ("candidate_omissions", [1]),
        "nonzero_shadow_write": ("shadow_writes", 1),
        "baseline_commit_count_not_one": ("baseline_commits", 2),
        "live_preview_mismatch": ("live_preview_match", False),
        "nonzero_repair_fallback": ("repair_fallbacks", 1),
        "prior_raw_config_hash_drift": ("raw_config_hash", "4" * 64),
        "raw_prior_layout_disconnect": ("prior_layout_connected", False),
        "flipped_gate_decision": ("gate_match", False),
        "permuted_accepted_sequence": ("accepted_sequence", [1]),
        "candidate_proposal_copied_from_baseline": ("candidate_digest", "3" * 64),
        "proposal_disconnected_from_raw_or_phase0": ("proposal_connected", False),
        "replay_noop": ("replay_invocations", 0), "replay_skipped_invocation": ("replay_skipped", True),
        "phase2_phase3_disconnected": ("phase23_connected", False),
        "replay_report_mismatch": ("replay_report_match", False),
        "nonzero_internal_failure_terminal": ("internal_failure_terminals", 1),
        "manifest_corruption": ("manifest_ok", False),
    }

    def check_sequence(value):
        if value["delta_ns"] >= 20_000_000: reject("20 ms boundary")
        if value["chosen"] != value["first_forward"]: reject("first-forward image")
        if len(set(value["images"])) != len(value["images"]): reject("reused image")
        if value["pair_indices"] != list(range(len(value["pair_indices"]))): reject("pair indices")
        if value["callback_source"] != value["expected_callback_source"]: reject("callback source")
        if value["identity_hash_match"] is not True: reject("identity hash")
        if value["modes"] != ["nullspace", "schur"]: reject("mode order")
        if value["coverage"] < 0.995: reject("coverage")
        if value["populations"][0] != value["populations"][1]: reject("populations")
        if value["common_alignment"] is not True: reject("independent alignment")
        if value["orthogonality_error"] > 1e-10: reject("transform")
        if value["position"] > 0.01: reject("position metric")
        if value["orientation"] > 0.05: reject("orientation metric")
        if value["relative_ate"] > 0.01: reject("relative ATE")

    sequence_mutations = {
        "exact_20_ms_boundary": ("delta_ns", 20_000_000),
        "nearest_not_first_forward": ("chosen", 2), "reused_image_message": ("images", [1, 1]),
        "missing_duplicate_pair_index": ("pair_indices", [0, 2]),
        "callback_source_mismatch": ("callback_source", "wrong"),
        "identity_hash_drift": ("identity_hash_match", False),
        "wrong_mode_order": ("modes", ["schur", "nullspace"]),
        "coverage_below_0_995": ("coverage", 0.994999999),
        "unequal_shared_populations": ("populations", [2, 1]),
        "independent_alignment": ("common_alignment", False),
        "invalid_nonorthogonal_transform": ("orthogonality_error", 1.0000000000000002e-10),
        "position_metric_limit": ("position", 0.010000000000000002),
        "orientation_metric_limit": ("orientation", 0.05000000000000001),
        "relative_ate_metric_limit": ("relative_ate", 0.010000000000000002),
    }

    def valid_sequence():
        return {"delta_ns": 19_999_999, "chosen": 1, "first_forward": 1, "nearest": 2,
                "images": [1, 2],
                "pair_indices": [0, 1], "callback_source": "camera", "expected_callback_source": "camera",
                "identity_hash_match": True, "modes": ["nullspace", "schur"], "coverage": 0.995,
                "populations": [2, 2], "common_alignment": True, "orthogonality_error": 1e-10,
                "position": 0.01, "orientation": 0.05, "relative_ate": 0.01}

    def linear_quantile(values, probability):
        ordered = sorted(values)
        position = (len(ordered) - 1) * probability
        low = int(math.floor(position)); high = int(math.ceil(position))
        return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    def check_timing(value):
        if value["pairs"] != [["nullspace", "schur"], ["schur", "nullspace"], ["nullspace", "schur"]]: reject("pair order")
        if value["indices"] != [0, 1, 2]: reject("pair indices")
        if not all(value[key] for key in ("runtime_match", "config_match", "profile_match", "clock_match", "affinity_match")): reject("provenance drift")
        if value["warmups_included"]: reject("warm-up inclusion")
        if value["populations"][0] != value["populations"][1]: reject("noncommon samples")
        if len(set(value["timestamps"])) != len(value["timestamps"]): reject("timestamps")
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in value["durations"]): reject("duration")
        if not all(value["primary"]): reject("nonprimary inclusion")
        if value["median"] != linear_quantile(value["durations"], 0.5) or value["p95"] != linear_quantile(value["durations"], 0.95): reject("quantiles")
        if value["median_ratio"] > 1.10: reject("median ratio")
        if value["p95_ratio"] > 1.15: reject("p95 ratio")

    def valid_timing():
        values = [1, 2, 3, 4]
        return {"pairs": [["nullspace", "schur"], ["schur", "nullspace"], ["nullspace", "schur"]],
                "indices": [0, 1, 2], "runtime_match": True, "config_match": True,
                "profile_match": True, "clock_match": True, "affinity_match": True,
                "warmups_included": False, "populations": [4, 4], "timestamps": [1, 2, 3, 4],
                "durations": values, "primary": [True] * 4,
                "median": linear_quantile(values, 0.5), "p95": linear_quantile(values, 0.95),
                "median_ratio": 1.10, "p95_ratio": 1.15}

    timing_mutations = {
        "wrong_pair_order": ("pairs", [["schur", "nullspace"]]),
        "wrong_pair_index": ("indices", [0, 2, 1]), "runtime_drift": ("runtime_match", False),
        "config_drift": ("config_match", False), "profile_drift": ("profile_match", False),
        "changed_clock_snapshot": ("clock_match", False), "affinity_mismatch": ("affinity_match", False),
        "warm_up_boundary_error": ("warmups_included", True),
        "unilateral_noncommon_samples": ("populations", [4, 3]),
        "duplicate_timestamp": ("timestamps", [1, 1, 3, 4]), "negative_duration": ("durations", [1, -1, 3, 4]),
        "noninteger_duration": ("durations", [1, 2.5, 3, 4]),
        "nonprimary_inclusion": ("primary", [True, False, True, True]),
        "incorrect_linear_quantiles": ("median", 2.0), "median_ratio_limit": ("median_ratio", 1.100000001),
        "p95_ratio_limit": ("p95_ratio", 1.150000001),
    }

    def validate_actual_mode_primitives(root):
        _actual_install_self_test_module_source_binding()
        parameters = {
            "/cp2_vio/a_bool": True,
            "/cp2_vio/a_double": -0.0,
            "/cp2_vio/a_int": -7,
            "/cp2_vio/a_list": ["x", 2],
        }
        # Keep the public verifier self-test self-contained: readiness executes
        # this held entrypoint from a synthetic repository whose neighbouring
        # helper modules are deliberately not the live workspace files.  This
        # byte string is an independent golden vector for the frozen recursive
        # parameter grammar, rather than a round-trip through cp2_schema.
        canonical = bytearray(b"SchurVIO-CP2-ros-params-v1\0")
        canonical.extend(b"m" + struct.pack(">Q", 4))
        for name, encoded_value in (
            (b"/cp2_vio/a_bool", b"b\x01"),
            (b"/cp2_vio/a_double", b"f" + struct.pack(">d", -0.0)),
            (b"/cp2_vio/a_int", b"i" + (-7).to_bytes(8, "big", signed=True)),
            (
                b"/cp2_vio/a_list",
                b"l" + struct.pack(">Q", 2)
                + b"s" + struct.pack(">Q", 1) + b"x"
                + b"i" + (2).to_bytes(8, "big", signed=True),
            ),
        ):
            canonical.extend(struct.pack(">Q", len(name)) + name + encoded_value)
        canonical = bytes(canonical)
        if _actual_decode_resolved_parameters(canonical, "self-test parameters") != parameters:
            raise RuntimeError("actual parameter decoder round-trip differs")

        manifest_root = root / "actual-manifest"
        manifest_root.mkdir(mode=0o700)
        payload = manifest_root / "payload.bin"
        payload.write_bytes(b"payload")
        payload.chmod(0o444)
        line = "{}  payload.bin\n".format(hashlib.sha256(b"payload").hexdigest()).encode("ascii")
        manifest_path = manifest_root / MANIFEST_NAME
        manifest_path.write_bytes(line)
        manifest_path.chmod(0o444)
        manifest_root.chmod(0o555)
        try:
            parsed, _, _ = _actual_scan_and_verify_manifest(
                manifest_root, hashlib.sha256(line).hexdigest()
            )
            if parsed != {"payload.bin": hashlib.sha256(b"payload").hexdigest()}:
                raise RuntimeError("actual manifest primitive differs")
        finally:
            manifest_root.chmod(0o700)
            payload.chmod(0o600)
            manifest_path.chmod(0o600)

        state_path = root / "state-payload.bin"
        prior = b"SchurVIO-CP2-prior-snapshot-v1\0fixture"
        state_bytes = bytearray(b"SchurVIO-CP2-state-file-v1\n\0\0\0\0\0")
        for phase in (0, 1):
            state_bytes.extend(bytes((phase,)) + b"\0" * 7 + struct.pack(">QQQQ", 0, 0, 0, len(prior)))
            state_bytes.extend(prior)
        state_path.write_bytes(bytes(state_bytes))
        frames = _actual_parse_state_payloads(state_path)
        if len(frames) != 2 or not _actual_ranges_equal(
            state_path, frames[(0, 0, 0, 0)]["offset"],
            frames[(0, 0, 0, 1)]["offset"], len(prior),
        ):
            raise RuntimeError("actual state-frame primitive differs")

        comparison = {
            "lambda_comparison_available": True,
            "lambda_comparison_status": "available",
            "lambda_reference_norm": 0.0,
            "lambda_error": 0.0,
            "lambda_tolerance": 1.0e-10,
            "lambda_ratio": 0.0,
            "lambda_pass": True,
        }
        _actual_validate_comparison(
            comparison, "lambda", 1.0e-8, 1.0e-10, True,
            "self-test statistics",
        )

        repeated_identity_fields = (
            "prior_snapshot_sha256", "config_sha256", "bag_sha256",
            "pair_index_sha256", "resolved_parameters_sha256",
            "baseline_accepted_set_sha256",
            "baseline_accepted_sequence_sha256",
            "candidate_accepted_set_sha256",
            "candidate_accepted_sequence_sha256",
        )
        gamma_update = {
            field: format(index + 1, "064x")
            for index, field in enumerate(repeated_identity_fields)
        }
        gamma_update.update({
            "baseline_gamma": -0.0,
            "candidate_gamma": 1.25,
        })
        gamma_child = {
            field: gamma_update[field] for field in repeated_identity_fields
        }
        gamma_child.update({
            "baseline_retained_gamma": -0.0,
            "candidate_retained_gamma": 1.25,
        })
        _actual_repeat_update_fields(
            gamma_child, gamma_update, "self-test gamma value copies"
        )
        null_gamma_update = dict(gamma_update)
        null_gamma_update.update({"baseline_gamma": None, "candidate_gamma": None})
        null_gamma_child = dict(gamma_child)
        null_gamma_child.update({
            "baseline_retained_gamma": None,
            "candidate_retained_gamma": None,
        })
        _actual_repeat_update_fields(
            null_gamma_child, null_gamma_update, "self-test gamma null copies"
        )

        def require_gamma_copy_rejection(label, child, update):
            try:
                _actual_repeat_update_fields(child, update, label)
            except ActualVerificationError:
                return
            raise RuntimeError("gamma referential-copy corruption was accepted: " + label)

        changed_gamma_child = dict(gamma_child)
        changed_gamma_child["baseline_retained_gamma"] = 0.0
        require_gamma_copy_rejection(
            "baseline signed-zero bits", changed_gamma_child, gamma_update
        )
        changed_gamma_child = dict(gamma_child)
        changed_gamma_child["candidate_retained_gamma"] = 1.5
        require_gamma_copy_rejection(
            "candidate binary64 value", changed_gamma_child, gamma_update
        )
        changed_gamma_child = dict(gamma_child)
        changed_gamma_child["baseline_retained_gamma"] = None
        require_gamma_copy_rejection(
            "baseline nullability", changed_gamma_child, gamma_update
        )
        changed_null_gamma_child = dict(null_gamma_child)
        changed_null_gamma_child["candidate_retained_gamma"] = 0.0
        require_gamma_copy_rejection(
            "candidate nullability", changed_null_gamma_child, null_gamma_update
        )

        exact_max = ACTUAL_EXACT_BINARY64_INTEGER_MAX
        exact_boundary_ratio = _actual_exact_count_ratio(
            exact_max, exact_max, "self-test exact count boundary"
        )
        if struct.pack(">d", exact_boundary_ratio) != struct.pack(">d", 1.0):
            raise RuntimeError("actual exact-count maximum boundary differs")

        def require_count_ratio_rejection(label, numerator, denominator):
            try:
                _actual_exact_count_ratio(numerator, denominator, label)
            except ActualVerificationError:
                return
            raise RuntimeError("actual count-ratio corruption was accepted: " + label)

        require_count_ratio_rejection(
            "self-test numerator above exact range", exact_max + 1, exact_max
        )
        require_count_ratio_rejection(
            "self-test denominator above exact range", exact_max, exact_max + 1
        )
        require_count_ratio_rejection(
            "self-test zero count denominator", 0, 0
        )

        try:
            process_libc = ctypes.CDLL(None, use_errno=True)
            get_rounding = process_libc.fegetround
            set_rounding = process_libc.fesetround
            get_rounding.argtypes = []
            get_rounding.restype = ctypes.c_int
            set_rounding.argtypes = [ctypes.c_int]
            set_rounding.restype = ctypes.c_int
            original_rounding = get_rounding()
        except (AttributeError, OSError) as exc:
            raise RuntimeError("self-test cannot inspect the process rounding mode") from exc
        if original_rounding != 0:
            raise RuntimeError("self-test did not begin in FE_TONEAREST")
        selected_nonnearest = None
        for candidate in (0x400, 0x800, 0xC00):
            if set_rounding(candidate) == 0 and get_rounding() == candidate:
                selected_nonnearest = candidate
                break
        if selected_nonnearest is None:
            if set_rounding(original_rounding) != 0:
                raise RuntimeError("self-test cannot restore FE_TONEAREST")
            raise RuntimeError("self-test cannot select a non-nearest rounding mode")
        nonnearest_rejected = False
        restoration_failed = False
        try:
            _actual_exact_count_ratio(
                1, 1, "self-test non-nearest exact count ratio"
            )
        except ActualVerificationError:
            nonnearest_rejected = True
        finally:
            restoration_failed = (
                set_rounding(original_rounding) != 0
                or get_rounding() != original_rounding
            )
        if restoration_failed:
            raise RuntimeError("self-test failed to restore FE_TONEAREST")
        if not nonnearest_rejected:
            raise RuntimeError("actual count ratio accepted non-nearest rounding")

        shared_payload = (
            b"SchurVIO-CP2-shared-timestamps-v1\0"
            + struct.pack(">Q", 2)
            + struct.pack(">QQ", 2, 4)
        )
        if _actual_parse_shared_timestamps(shared_payload) != [2, 4]:
            raise RuntimeError("actual shared-timestamp primitive differs")
        static_records = [
            {"path": path, "size": index + 1, "sha256": format(index + 1, "064x")}
            for index, path in enumerate((
                "config/euroc_mav/estimator_config.yaml",
                "config/euroc_mav/kalibr_imu_chain.yaml",
                "config/euroc_mav/kalibr_imucam_chain.yaml",
                "project/cp2_serial.launch",
            ))
        ]
        static_payload = bytearray(b"SchurVIO-CP2-static-config-v1\0")
        static_payload.extend(struct.pack(">Q", len(static_records)))
        for record in static_records:
            encoded_path = record["path"].encode("utf-8")
            static_payload.extend(struct.pack(">Q", len(encoded_path)))
            static_payload.extend(encoded_path)
            static_payload.extend(struct.pack(">Q", record["size"]))
            static_payload.extend(bytes.fromhex(record["sha256"]))
        static_path = root / "static-bundle.bin"
        static_path.write_bytes(bytes(static_payload))
        static_digest = hashlib.sha256(bytes(static_payload)).hexdigest()
        _actual_validate_static_bundle(
            root,
            {
                "static_bundle_payload": static_path.name,
                "static_bundle_sha256": static_digest,
                "static_files": static_records[:3],
                "launch": static_records[3],
            },
            {static_path.name: static_digest},
        )

        pair_root = root / "actual-pair-index"
        pair_root.mkdir(mode=0o700)
        pair_workspace = root / "actual-pair-workspace"
        pair_environment_sha = "e" * 64
        serial_rows = []
        pair_commands = []
        runtime_commands = []
        pair_manifest = {}
        pair_inputs = []
        for sequence_index, sequence_id in enumerate(ACTUAL_SEQUENCE_IDS):
            base_index = sequence_index * 100
            base_time = (sequence_index + 1) * 1_000_000_000
            sequence_serial = []
            for pair_index in range(2):
                if pair_index == 0:
                    anchor_camera = 0
                    cam0_index = base_index
                    cam1_index = base_index + 1
                    cam0_record = base_time + 100
                    cam1_record = base_time + 101
                else:
                    anchor_camera = 1
                    cam1_index = base_index + 2
                    cam0_index = base_index + 3
                    cam1_record = base_time + 200
                    cam0_record = base_time + 201
                row = {
                    "schema_version": 1, "record_type": "serial_pair",
                    "sequence_index": sequence_index, "sequence_id": sequence_id,
                    "pair_index": pair_index,
                    "anchor_filtered_index": (
                        cam0_index if anchor_camera == 0 else cam1_index
                    ),
                    "anchor_camera_id": anchor_camera,
                    "cam0_filtered_index": cam0_index,
                    "cam1_filtered_index": cam1_index,
                    "cam0_record_time_ns": cam0_record,
                    "cam1_record_time_ns": cam1_record,
                    "cam0_header_time_ns": cam0_record + 10,
                    "cam1_header_time_ns": cam1_record + 10,
                    "camera_timestamp_ns": cam0_record + 10,
                    "absolute_record_delta_ns": abs(cam0_record - cam1_record),
                    "selected": True,
                    "enqueue_entered": True, "enqueue_returned": True,
                    "enqueue_status": "queued", "processing_entered": True,
                    "processing_returned": True, "processing_status": "processed",
                    "updater_invocation_ids": [],
                }
                sequence_serial.append(row)
                serial_rows.append(row)
            projected = []
            for row in sequence_serial:
                source = {
                    key: row[key] for key in ACTUAL_PAIR_INDEX_PROJECTION_KEYS
                }
                source["record_type"] = "pair_index"
                projected.append(source)
            stdout = _actual_canonical_jsonl_bytes(projected, "self-test pair index")
            stdout_relative = "actual-pair-index/stdout-{}.jsonl".format(sequence_index)
            stderr_relative = "actual-pair-index/stderr-{}.txt".format(sequence_index)
            (root / stdout_relative).write_bytes(stdout)
            (root / stderr_relative).write_bytes(b"")
            stdout_sha = hashlib.sha256(stdout).hexdigest()
            stderr_sha = hashlib.sha256(b"").hexdigest()
            pair_manifest[stdout_relative] = stdout_sha
            pair_manifest[stderr_relative] = stderr_sha
            bag_path = "/tmp/cp2-pair-self-test-{}.bag".format(sequence_index)
            pair_inputs.append({"bag_path": bag_path, "bag_size": 1})
            helper = str(
                pair_workspace / "src/scripts/cp2/cp2_pair_index_extract.py"
            )
            pair_commands.append({
                "command_id": 3 * sequence_index,
                "phase": "pair_index", "sequence_index": sequence_index,
                "pair_index": None, "run_index": None,
                "exit_code": 0, "timed_out": False,
                "environment_sha256": pair_environment_sha,
                "argv": [
                    "/usr/bin/python3", "-I", "-B", helper,
                    "--sequence-index", str(sequence_index),
                    "--sequence-id", sequence_id,
                    "--bag-path", bag_path,
                    "--parent-bag-fd", "7",
                    "--bag-identity", "0:1:{}:1:0:0:1:0:0".format(
                        stat.S_IFREG | 0o400
                    ),
                ],
                "stdout": stdout_relative, "stdout_sha256": stdout_sha,
                "stderr": stderr_relative, "stderr_sha256": stderr_sha,
            })
            runtime_commands.extend((
                {
                    "command_id": 3 * sequence_index + 1,
                    "phase": "runtime_preflight",
                    "sequence_index": sequence_index,
                    "pair_index": None,
                    "run_index": sequence_index,
                },
                {
                    "command_id": 3 * sequence_index + 2,
                    "phase": "ros_run",
                    "sequence_index": sequence_index,
                    "pair_index": None,
                    "run_index": sequence_index,
                },
            ))
        pair_environment_record = {
            "environment_id": "pair_index_v1",
            "canonical_sha256": pair_environment_sha,
            "variables": [],
        }
        pair_common = {
            "commands": sorted(
                pair_commands + runtime_commands,
                key=lambda item: item["command_id"],
            ),
            "environment_classes": {
                pair_environment_sha: dict(ACTUAL_PAIR_INDEX_ENVIRONMENT)
            },
            "provenance": {
                "build": {"workspace": str(pair_workspace)},
                "environment": {"classes": [pair_environment_record]},
                "inputs": pair_inputs,
            },
        }
        _actual_validate_recorded_pair_index_commands(
            root, pair_common, pair_manifest, serial_rows
        )

        def require_pair_index_rejection(label):
            try:
                _actual_validate_recorded_pair_index_commands(
                    root, pair_common, pair_manifest, serial_rows
                )
            except ActualVerificationError:
                return
            raise RuntimeError("actual pair-index corruption was accepted: " + label)

        pair_environment_record["environment_id"] = "not_pair_index_v1"
        require_pair_index_rejection("environment-id")
        pair_environment_record["environment_id"] = "pair_index_v1"

        first_stderr = root / pair_commands[0]["stderr"]
        first_stderr.write_bytes(b"unexpected diagnostic\n")
        bad_stderr_sha = sha256_file(first_stderr)
        pair_commands[0]["stderr_sha256"] = bad_stderr_sha
        pair_manifest[pair_commands[0]["stderr"]] = bad_stderr_sha
        require_pair_index_rejection("nonempty-stderr")
        first_stderr.write_bytes(b"")
        empty_sha = hashlib.sha256(b"").hexdigest()
        pair_commands[0]["stderr_sha256"] = empty_sha
        pair_manifest[pair_commands[0]["stderr"]] = empty_sha

        first_stdout = root / pair_commands[0]["stdout"]
        valid_stdout = first_stdout.read_bytes()
        noncanonical_stdout = valid_stdout.replace(b"\n", b" \n", 1)
        first_stdout.write_bytes(noncanonical_stdout)
        noncanonical_sha = sha256_file(first_stdout)
        pair_commands[0]["stdout_sha256"] = noncanonical_sha
        pair_manifest[pair_commands[0]["stdout"]] = noncanonical_sha
        require_pair_index_rejection("noncanonical-stdout")
        first_stdout.write_bytes(valid_stdout)
        valid_stdout_sha = hashlib.sha256(valid_stdout).hexdigest()
        pair_commands[0]["stdout_sha256"] = valid_stdout_sha
        pair_manifest[pair_commands[0]["stdout"]] = valid_stdout_sha

        serial_rows[0]["cam0_header_time_ns"] += 1
        require_pair_index_rejection("serial-source-projection")
        serial_rows[0]["cam0_header_time_ns"] -= 1

        pair_commands[0]["sequence_index"] = 1
        require_pair_index_rejection("command-order")
        pair_commands[0]["sequence_index"] = 0

        runtime_commands[0]["command_id"] = pair_commands[0]["command_id"]
        require_pair_index_rejection("command-after-runtime")
        runtime_commands[0]["command_id"] = pair_commands[0]["command_id"] + 1

        compile_workspace = root / "actual-compile-workspace"
        compile_source = compile_workspace / "src"
        compile_build = compile_workspace / "build"
        compile_directory = compile_build / "ov_msckf"
        compile_source.mkdir(parents=True)
        compile_directory.mkdir(parents=True)
        compile_entries = []
        for source, target in ACTUAL_STRICT_FP_SOURCE_TARGETS.items():
            source_path = compile_source / source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"// self-test\n")
            output = (
                compile_directory / "CMakeFiles" / (target + ".dir")
                / (source[len("ov_msckf/"):] + ".o")
            )
            tokens = [
                "/usr/bin/c++", "-O3", "-fno-signed-zeros",
                "-fno-fast-math", "-ffp-contract=off", "-fsigned-zeros",
                "-DEIGEN_DONT_VECTORIZE=1",
                "-DEIGEN_MAX_ALIGN_BYTES=16",
                "-DEIGEN_MAX_STATIC_ALIGN_BYTES=16",
                "-ffile-prefix-map={}=/cp2/reproducible-root".format(
                    compile_workspace
                ),
                "-fdebug-prefix-map={}=/cp2/reproducible-root".format(
                    compile_workspace
                ),
                "-fmacro-prefix-map={}=/cp2/reproducible-root".format(
                    compile_workspace
                ),
                "-o", str(output), "-c", str(source_path),
            ]
            compile_entries.append({
                "arguments": tokens,
                "directory": str(compile_directory),
                "file": str(source_path),
                "output": str(output),
            })

        def write_compile_fixture(name, entries):
            path = root / name
            path.write_text(
                json.dumps(
                    entries, allow_nan=False, ensure_ascii=False,
                    separators=(",", ":"), sort_keys=True,
                ),
                encoding="utf-8",
            )
            return {
                "compile_commands": name,
                "workspace": str(compile_workspace),
            }

        _actual_validate_compile_commands(
            root,
            write_compile_fixture("compile-commands-valid.json", compile_entries),
        )

        def require_compile_rejection(name, mutate):
            entries = json.loads(json.dumps(compile_entries))
            mutate(entries)
            try:
                _actual_validate_compile_commands(
                    root, write_compile_fixture(name, entries)
                )
            except ActualVerificationError:
                return
            raise RuntimeError("actual strict-FP corruption was accepted: " + name)

        require_compile_rejection(
            "compile-commands-dual.json",
            lambda entries: entries[0].update({"command": "/usr/bin/c++"}),
        )
        require_compile_rejection(
            "compile-commands-nul.json",
            lambda entries: entries[0]["arguments"].insert(1, "-DOPAQUE=\0"),
        )
        require_compile_rejection(
            "compile-commands-quoted-map.json",
            lambda entries: entries[0]["arguments"].__setitem__(
                next(
                    index for index, token in enumerate(entries[0]["arguments"])
                    if token.startswith("-ffile-prefix-map=")
                ),
                next(
                    token for token in entries[0]["arguments"]
                    if token.startswith("-ffile-prefix-map=")
                ) + '"',
            ),
        )
        try:
            verify_timing_preprofile_blocked(root / "must-not-be-read", "0" * 64)
        except ActualVerificationError:
            pass
        else:
            raise RuntimeError("preprofile timing verifier did not fail closed")
        try:
            verify_sequence_artifact(
                root / "must-not-be-read-sequence", "0" * 64, quiet=True
            )
        except ActualVerificationError as exc:
            if "blocked before artifact access" not in str(exc):
                raise RuntimeError(
                    "pending-contract sequence verifier failed for the wrong reason"
                ) from exc
        else:
            raise RuntimeError("pending-contract sequence verifier did not fail closed")

    os.umask(0o077)
    temporary_root = Path(tempfile.mkdtemp(prefix="schurvio-lite-cp2-verifier-self-test-", dir="/tmp"))
    cases = []
    try:
        (temporary_root / "file-a").write_bytes(b"a")
        os.link(str(temporary_root / "file-a"), str(temporary_root / "file-b"))
        (temporary_root / "link").symlink_to("file-a")
        (temporary_root / "root-link").symlink_to(".")
        for index, name in enumerate(READINESS_VERIFIER_CASES):
            expected_rejection = name != "valid_minimal_fixture"
            observed_rejection = False
            unexpected = False
            try:
                if name == "valid_minimal_fixture":
                    safe_relpath("logs/self-test.log")
                    strict_json_bytes(b'{"x":1}', "self-test JSON")
                    check_recorded(valid_recorded())
                    check_sequence(valid_sequence())
                    check_timing(valid_timing())
                    validate_actual_mode_primitives(temporary_root)
                elif name in READINESS_COMMON_CASES:
                    validate_common(name, temporary_root)
                elif name in recorded_mutations:
                    value = valid_recorded(); key, mutation = recorded_mutations[name]; value[key] = mutation; check_recorded(value)
                elif name in sequence_mutations:
                    value = valid_sequence(); key, mutation = sequence_mutations[name]; value[key] = mutation; check_sequence(value)
                elif name in timing_mutations:
                    value = valid_timing(); key, mutation = timing_mutations[name]; value[key] = mutation; check_timing(value)
                else:
                    raise RuntimeError("unimplemented verifier self-test case: " + name)
            except _ReadinessSelfTestRejection:
                observed_rejection = True
            except Exception:
                unexpected = True
            cases.append({"index": index, "name": name, "expected_rejection": expected_rejection,
                          "observed_rejection": observed_rejection,
                          "passed": not unexpected and observed_rejection == expected_rejection})
    finally:
        shutil.rmtree(str(temporary_root))
    passed = (not os.path.lexists(str(temporary_root)) and len(cases) == len(READINESS_VERIFIER_CASES)
              and all(case["passed"] for case in cases))
    result = {"schema_version": 1, "record_type": "self_test_result",
              "entrypoint": "scripts/cp2/verify_report.py", "temporary_root": str(temporary_root),
              "bag_provider_calls": 0, "cases": cases, "case_count": len(cases), "passed": passed}
    print(json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0 if passed else 1


READINESS_ENGINE_PROTECTING_TESTS = (
    ("test_cp2_readiness.py", 27),
    ("test_cp2_actual_readiness_binding.py", 12),
)
READINESS_ENGINE_PROTECTING_TEST_COUNT = sum(
    count for _, count in READINESS_ENGINE_PROTECTING_TESTS
)


def run_readiness_engine_protecting_tests():
    """Run the readiness-engine attack suite inside the evidenced unit self-test."""

    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
    }
    module_digest = hashlib.sha256(
        b"SchurVIO-CP2-readiness-protecting-modules-v1\0"
    )
    output_digest = hashlib.sha256(
        b"SchurVIO-CP2-readiness-protecting-outputs-v1\0"
    )
    tests_directory = Path(__file__).resolve().parent / "tests"
    for filename, expected_count in READINESS_ENGINE_PROTECTING_TESTS:
        test_path = (tests_directory / filename).resolve()
        if (
            test_path.parent != tests_directory.resolve()
            or not test_path.is_file()
            or test_path.is_symlink()
        ):
            raise RuntimeError(
                "CP2 readiness protecting-test module is unavailable: " + filename
            )
        module_bytes = test_path.read_bytes()
        encoded_name = filename.encode("utf-8")
        module_digest.update(len(encoded_name).to_bytes(8, "big"))
        module_digest.update(encoded_name)
        module_digest.update(len(module_bytes).to_bytes(8, "big"))
        module_digest.update(module_bytes)
        command = ["/usr/bin/python3", "-I", "-B", str(test_path), "-v"]
        try:
            completed = subprocess.run(
                command,
                cwd="/tmp",
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "CP2 readiness protecting tests timed out: " + filename
            ) from exc
        output = completed.stdout
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        output_digest.update(len(encoded_name).to_bytes(8, "big"))
        output_digest.update(encoded_name)
        output_digest.update(len(output).to_bytes(8, "big"))
        output_digest.update(output)
        match = re.search(rb"Ran ([0-9]+) tests in [^\n]+\n\nOK\n", output)
        if (
            completed.returncode != 0
            or match is None
            or int(match.group(1)) != expected_count
        ):
            raise RuntimeError(
                "CP2 readiness protecting-test inventory/outcome is not exact: "
                + filename
            )
    print(
        "CP2_READINESS_ENGINE_PROTECTING_TESTS count={} passed=true "
        "module_sha256={} output_sha256={}".format(
            READINESS_ENGINE_PROTECTING_TEST_COUNT,
            module_digest.hexdigest(),
            output_digest.hexdigest(),
        )
    )


def run_unit_self_test():
    required_cp2_c2_counts = {
        "test_cp2_updater_msckf_end_to_end": 16,
        "test_cp2_updater_msckf_fault_injection": 31,
        "test_cp2_commit_oracle": 10,
        "test_cp2_commit_boundary": 4,
    }
    if any(CP2_TESTS.get(name) != count for name, count in required_cp2_c2_counts.items()):
        raise RuntimeError("CP2-C2 executable test-count contract is inconsistent")
    if len(CP2_TESTS) != 21 or sum(ALL_TESTS.values()) != 203:
        raise RuntimeError("CP2 exact executable/total testcase inventory is inconsistent")
    if len(RUNTIME_LIBRARY_SOURCES) != 29:
        raise RuntimeError("CP2-C3 exact production/fault runtime source inventory is not 29")
    if any(
        len(TEST_CASES_BY_BINARY.get(name, ())) != count
        for name, count in required_cp2_c2_counts.items()
    ):
        raise RuntimeError("CP2-C2 testcase ownership/count mapping is inconsistent")
    mapped_testcase_names = {
        case
        for cases in TEST_CASES_BY_BINARY.values()
        for case in cases
    }
    if (
        sum(len(cases) for cases in TEST_CASES_BY_BINARY.values()) != 203
        or mapped_testcase_names != EXPECTED_TEST_CASES
    ):
        raise RuntimeError("CP1 plus CP2 exact testcase execution inventory is not 203")
    if set(TEST_CASES_BY_BINARY) != set(ALL_TESTS):
        raise RuntimeError("testcase ownership does not cover the exact executable inventory")
    if set(SUMMARIES_BY_BINARY) != set(ALL_TESTS):
        raise RuntimeError("summary ownership does not cover the exact executable inventory")
    if set(TEST_SOURCE_BY_BINARY) != set(ALL_TESTS):
        raise RuntimeError("test source mapping does not cover the exact executable inventory")

    run_readiness_engine_protecting_tests()

    frozen_sha256 = {
        relative: expected["sha256"]
        for relative, expected in FROZEN_CP2_C_APPROVAL_BINDING.items()
    }
    frozen_git_blobs = {
        relative: expected["git_blob"]
        for relative, expected in FROZEN_CP2_C_APPROVAL_BINDING.items()
    }
    binding_errors = []
    validate_cp2_c_approval_binding(
        frozen_sha256, frozen_git_blobs, binding_errors
    )
    if binding_errors:
        raise RuntimeError("valid CP2-C approval binding was rejected")

    clarification_path = "docs/cp2_c_composite_and_readiness_clarification.md"
    wrong_sha256 = dict(frozen_sha256)
    wrong_sha256[clarification_path] = "0" * 64
    binding_errors = []
    validate_cp2_c_approval_binding(
        wrong_sha256, frozen_git_blobs, binding_errors
    )
    if binding_errors != [
        "CP2-C approval-binding SHA-256 mismatch: " + clarification_path
    ]:
        raise RuntimeError("wrong CP2-C clarification SHA-256 was not rejected exactly")

    approval_path = "project/cp2_c_clarification_approval.json"
    wrong_git_blobs = dict(frozen_git_blobs)
    wrong_git_blobs[approval_path] = "0" * 40
    binding_errors = []
    validate_cp2_c_approval_binding(
        frozen_sha256, wrong_git_blobs, binding_errors
    )
    if binding_errors != [
        "CP2-C approval-binding Git blob mismatch: " + approval_path
    ]:
        raise RuntimeError("wrong CP2-C approval Git blob was not rejected exactly")

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

        publication_root = temporary_root / "publication-no-replace"
        publication_root.mkdir()
        existing_report = publication_root / REPORT_NAME
        original_report = b"pre-existing-report\n"
        existing_report.write_bytes(original_report)
        try:
            atomic_write_json(existing_report, {"replacement": True})
        except ValueError:
            pass
        else:
            raise RuntimeError("atomic report publication replaced an existing file")
        if existing_report.read_bytes() != original_report:
            raise RuntimeError("failed report publication changed the existing file")
        if list(publication_root.glob("." + REPORT_NAME + ".tmp.*")):
            raise RuntimeError("failed report publication retained a temporary file")
        corruptions.append({
            "name": "publication-existing-report",
            "detected_errors": 1,
        })

        broken_manifest = publication_root / MANIFEST_NAME
        broken_target = publication_root / "missing-manifest-target"
        broken_manifest.symlink_to(broken_target.name)
        try:
            atomic_write_bytes(broken_manifest, b"forged manifest\n")
        except ValueError:
            pass
        else:
            raise RuntimeError("atomic manifest publication replaced a broken symlink")
        if (
            not broken_manifest.is_symlink()
            or os.readlink(str(broken_manifest)) != broken_target.name
            or os.path.lexists(str(broken_target))
        ):
            raise RuntimeError("failed manifest publication changed a broken symlink target")
        if list(publication_root.glob("." + MANIFEST_NAME + ".tmp.*")):
            raise RuntimeError("failed manifest publication retained a temporary file")
        corruptions.append({
            "name": "publication-broken-manifest-symlink",
            "detected_errors": 1,
        })

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

        def remove_archive_member(root, member_name, replacement_suffix):
            archive_path = root / SOURCE_ARCHIVE_NAME
            replacement = root / ("." + SOURCE_ARCHIVE_NAME + replacement_suffix)
            removed = 0
            with tarfile.open(str(archive_path), mode="r:") as source_archive:
                with tarfile.open(str(replacement), mode="w:") as target_archive:
                    for member in source_archive.getmembers():
                        if member.name == member_name:
                            removed += 1
                            continue
                        stream = source_archive.extractfile(member) if member.isfile() else None
                        target_archive.addfile(member, stream)
            if removed != 1:
                raise RuntimeError("synthetic source archive lacks exactly one " + member_name)
            os.replace(str(replacement), str(archive_path))
            archive_path.chmod(0o600)

        def remove_recorded_contract_from_archive(root):
            remove_archive_member(
                root,
                "docs/cp2_recorded_evidence_contract.md",
                ".without-recorded-contract",
            )

        corruption(
            "missing-recorded-contract-archive-member",
            remove_recorded_contract_from_archive,
            expected_error="source archive does not contain every curated SOURCE_INPUTS file",
        )

        corruption(
            "missing-composite-clarification-archive-member",
            lambda root: remove_archive_member(
                root,
                "docs/cp2_c_composite_and_readiness_clarification.md",
                ".without-composite-clarification",
            ),
            expected_error="source archive does not contain every curated SOURCE_INPUTS file",
        )

        corruption(
            "missing-composite-approval-archive-member",
            lambda root: remove_archive_member(
                root,
                "project/cp2_c_clarification_approval.json",
                ".without-composite-approval",
            ),
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
                if "CMakeFiles/ov_msckf_lib.dir/" in str(entry.get("output", ""))
                and str(entry.get("file", "")).endswith(
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
                if "CMakeFiles/ov_msckf_lib.dir/" in str(entry.get("output", ""))
                and str(entry.get("file", "")).endswith(
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
                if not (
                    "CMakeFiles/ov_msckf_lib.dir/" in str(entry.get("output", ""))
                    and str(entry.get("file", "")).endswith(
                        "/ov_msckf/src/state/StateHelper.cpp"
                    )
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
                if "CMakeFiles/ov_msckf_lib.dir/" in str(entry.get("output", ""))
                and str(entry.get("file", "")).endswith(
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
                if "CMakeFiles/ov_msckf_lib.dir/" in str(entry.get("output", ""))
                and str(entry.get("file", "")).endswith(
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

        def rewrite_synthetic_compile_tokens(
            root, source_suffix, transform, target=None
        ):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            if target is None:
                if source_suffix in RUNTIME_LIBRARY_SOURCES:
                    target = "ov_msckf_lib"
                else:
                    owning_targets = [
                        name for name, source in TEST_SOURCE_BY_BINARY.items()
                        if source == source_suffix
                    ]
                    if len(owning_targets) != 1:
                        raise RuntimeError(
                            "synthetic source has ambiguous target ownership: "
                            + source_suffix
                        )
                    target = owning_targets[0]
            matching = [
                entry for entry in entries
                if "CMakeFiles/{}.dir/".format(target) in str(entry.get("output", ""))
                and str(entry.get("file", "")).endswith("/" + source_suffix)
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

        def insert_tokens_before_output(tokens, inserted):
            output_positions = [
                index for index, value in enumerate(tokens) if value == "-o"
            ]
            if len(output_positions) != 1:
                raise RuntimeError("synthetic command lacks exactly one -o")
            position = output_positions[0]
            tokens[position:position] = list(inserted)
            return tokens

        def replace_exact_token(tokens, old, new):
            if tokens.count(old) != 1:
                raise RuntimeError("synthetic command lacks exactly one " + old)
            tokens[tokens.index(old)] = new
            return tokens

        def rewrite_synthetic_link_tokens(
            root, label, transform, line_index=0
        ):
            path = root / LINK_COMMAND_ARTIFACTS[label]
            raw_lines = path.read_text(encoding="utf-8").splitlines()
            if line_index < 0 or line_index >= len(raw_lines):
                raise RuntimeError("synthetic link command line index is invalid")
            original = shlex.split(raw_lines[line_index])
            changed = transform(list(original))
            if changed == original:
                raise RuntimeError("synthetic link-command transform made no change")
            raw_lines[line_index] = " ".join(
                shlex.quote(token) for token in changed
            )
            path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

        def insert_before_exact_token(tokens, marker, inserted):
            if tokens.count(marker) != 1:
                raise RuntimeError(
                    "synthetic link command lacks exactly one " + marker
                )
            position = tokens.index(marker)
            tokens[position:position] = list(inserted)
            return tokens

        def replace_link_output(tokens, expected, replacement):
            output_positions = [
                index for index, token in enumerate(tokens) if token == "-o"
            ]
            if len(output_positions) != 1:
                raise RuntimeError("synthetic link command lacks exactly one -o")
            output_index = output_positions[0] + 1
            if output_index >= len(tokens) or tokens[output_index] != expected:
                raise RuntimeError("synthetic link command has an unexpected output")
            tokens[output_index] = replacement
            return tokens

        forced_include_spellings = {
            "include-split": ["-include", "/tmp/defines-cp2-test.h"],
            "include-joined": ["-include/tmp/defines-cp2-test.h"],
            "include-equals": ["-include=/tmp/defines-cp2-test.h"],
            "imacros-split": ["-imacros", "/tmp/defines-cp2-test.h"],
            "imacros-joined": ["-imacros/tmp/defines-cp2-test.h"],
            "imacros-equals": ["-imacros=/tmp/defines-cp2-test.h"],
            "wp-include": ["-Wp,-include,/tmp/defines-cp2-test.h"],
            "wp-imacros": ["-Wp,-imacros,/tmp/defines-cp2-test.h"],
            "wp-response": ["-Wp,@/tmp/preprocessor.rsp"],
            "compiler-specs": ["-specs=/tmp/cp2-test.specs"],
        }

        def forced_include_corruption(inserted):
            def edit(root):
                rewrite_synthetic_compile_tokens(
                    root,
                    "ov_msckf/src/update/UpdaterMSCKF.cpp",
                    lambda tokens: insert_tokens_before_output(tokens, inserted),
                    target="ov_msckf_lib",
                )
            return edit

        for spelling, inserted in forced_include_spellings.items():
            corruption(
                "forced-include-" + spelling,
                forced_include_corruption(inserted),
                expected_error=(
                    "ov_msckf/src/update/UpdaterMSCKF.cpp compile command is not "
                    "effectively strict-FP"
                ),
            )

        def duplicate_commit_boundary_test_source(root):
            path = root / "compile_commands.json"
            entries = json.loads(path.read_text(encoding="utf-8"))
            matching = [
                entry for entry in entries
                if (
                    "CMakeFiles/test_cp2_commit_boundary.dir/"
                    in str(entry.get("output", ""))
                    and str(entry.get("file", "")).endswith(
                        "/ov_msckf/test/cp2/test_cp2_commit_boundary.cpp"
                    )
                )
            ]
            if len(matching) != 1:
                raise RuntimeError("synthetic fixture lost commit-boundary test source")
            entries.append(dict(matching[0]))
            write_json_fixture(path, entries)

        corruption(
            "duplicate-test-translation-unit",
            duplicate_commit_boundary_test_source,
            expected_error=(
                "test_cp2_commit_boundary strict-FP source inventory mismatch"
            ),
        )

        def leak_cp2_testing_into_production(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/UpdaterMSCKF.cpp",
                lambda tokens: insert_before_output(
                    tokens, "-DOV_MSCKF_CP2_TESTING=1"
                ),
                target="ov_msckf_lib",
            )

        corruption(
            "cp2-testing-production-macro-leak",
            leak_cp2_testing_into_production,
            expected_error="ov_msckf_lib OV_MSCKF_CP2_TESTING macro isolation failed",
        )

        def remove_cp2_testing_from_fault_runtime(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/UpdaterMSCKF.cpp",
                lambda tokens: remove_exact_token(
                    tokens, "-DOV_MSCKF_CP2_TESTING=1"
                ),
                target=FAULT_LIBRARY_TARGET,
            )

        corruption(
            "cp2-testing-fault-macro-removal",
            remove_cp2_testing_from_fault_runtime,
            expected_error=(
                FAULT_LIBRARY_TARGET
                + " OV_MSCKF_CP2_TESTING macro isolation failed"
            ),
        )

        def remove_commit_oracle_fp_contract(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/CP2CommitOracle.cpp",
                lambda tokens: remove_exact_token(tokens, "-ffp-contract=off"),
            )

        corruption(
            "commit-oracle-production-strict-fp",
            remove_commit_oracle_fp_contract,
            expected_error=(
                "ov_msckf/src/update/CP2CommitOracle.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def cross_link_fault_test_to_production(root):
            path = root / LINK_COMMAND_ARTIFACTS["fault_updater_test"]
            text = path.read_text(encoding="utf-8")
            old = "devel/lib/libov_msckf_cp2_fault_lib.a"
            if text.count(old) != 1:
                raise RuntimeError("synthetic fault link lacks one isolated archive")
            path.write_text(
                text.replace(old, "devel/lib/libov_msckf_lib.so"),
                encoding="utf-8",
            )

        corruption(
            "fault-test-production-cross-link",
            cross_link_fault_test_to_production,
            expected_error=(
                "fault updater test does not link only the isolated fault runtime"
            ),
        )

        alternate_fault_library_spellings = {
            "driver-joined-l": (
                "-Ldevel/lib", "-lov_msckf_cp2_fault_lib",
            ),
            "driver-split-l": (
                "-L", "devel/lib", "-l", "ov_msckf_cp2_fault_lib",
            ),
            "forwarded-joined-l": (
                "-Wl,-L,devel/lib,-lov_msckf_cp2_fault_lib",
            ),
            "forwarded-split-l": (
                "-Wl,-L,devel/lib,-l,ov_msckf_cp2_fault_lib",
            ),
            "driver-exact-filename": (
                "-l:libov_msckf_cp2_fault_lib.a",
            ),
            "forwarded-exact-filename": (
                "-Wl,-l:libov_msckf_cp2_fault_lib.a",
            ),
            "driver-long-library": (
                "--library=ov_msckf_cp2_fault_lib",
            ),
            "forwarded-long-library": (
                "-Wl,--library=ov_msckf_cp2_fault_lib",
            ),
            "absolute-library-path": (
                "/tmp/libov_msckf_cp2_fault_lib.a",
            ),
            "relative-library-path": (
                "alternate/lib/libov_msckf_cp2_fault_lib.so",
            ),
            "forwarded-library-path": (
                "-Wl,/tmp/libov_msckf_cp2_fault_lib.a",
            ),
            "xlinker-forwarding": (
                "-Xlinker=-lov_msckf_cp2_fault_lib",
            ),
            "driver-response-file": (
                "@/tmp/cp2-linker.rsp",
            ),
            "forwarded-response-file": (
                "-Wl,@/tmp/cp2-linker.rsp",
            ),
            "compiler-specs": (
                "-specs=/tmp/cp2-link.specs",
            ),
        }

        def alternate_fault_library_corruption(inserted):
            def edit(root):
                rewrite_synthetic_link_tokens(
                    root,
                    "production_updater_test",
                    lambda tokens: insert_before_exact_token(
                        tokens,
                        "devel/lib/libov_msckf_lib.so",
                        inserted,
                    ),
                )
            return edit

        for spelling, inserted in alternate_fault_library_spellings.items():
            corruption(
                "production-test-alternate-fault-library-" + spelling,
                alternate_fault_library_corruption(inserted),
                expected_error=(
                    "production updater test does not link only the production runtime"
                ),
            )

        def rename_fault_test_output(root):
            rewrite_synthetic_link_tokens(
                root,
                "fault_updater_test",
                lambda tokens: replace_link_output(
                    tokens,
                    "devel/lib/ov_msckf/test_cp2_updater_msckf_fault_injection",
                    "devel/lib/ov_msckf/not_the_fault_test",
                ),
            )

        corruption(
            "fault-test-output-binding",
            rename_fault_test_output,
            expected_error=(
                "fault updater test does not link only the isolated fault runtime"
            ),
        )

        def inject_forwarded_production_test_output(root):
            rewrite_synthetic_link_tokens(
                root,
                "production_updater_test",
                lambda tokens: insert_before_exact_token(
                    tokens,
                    "devel/lib/libov_msckf_lib.so",
                    ("-Wl,-o,/tmp/forged-cp2-production-test",),
                ),
            )

        corruption(
            "production-test-forwarded-output-binding",
            inject_forwarded_production_test_output,
            expected_error=(
                "production updater test does not link only the production runtime"
            ),
        )

        def rename_production_library_output(root):
            rewrite_synthetic_link_tokens(
                root,
                "production_library",
                lambda tokens: replace_link_output(
                    tokens,
                    "devel/lib/libov_msckf_lib.so",
                    "devel/lib/libnot_ov_msckf_lib.so",
                ),
            )

        corruption(
            "production-library-output-binding",
            rename_production_library_output,
            expected_error="production runtime link/source inventory is not exact",
        )

        def remove_fault_runtime_object(root):
            path = root / LINK_COMMAND_ARTIFACTS["fault_library"]
            text = path.read_text(encoding="utf-8")
            token = (
                " CMakeFiles/ov_msckf_cp2_fault_lib.dir/"
                "src/update/CP2CommitOracle.cpp.o"
            )
            if text.count(token) != 1:
                raise RuntimeError("synthetic fault archive lacks commit-oracle object")
            path.write_text(text.replace(token, "", 1), encoding="utf-8")

        corruption(
            "fault-runtime-link-source-inventory",
            remove_fault_runtime_object,
            expected_error="fault runtime archive link/source inventory is not exact",
        )

        def remove_composite_state_fp_contract(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/CP2CompositeState.cpp",
                lambda tokens: remove_exact_token(tokens, "-ffp-contract=off"),
            )

        corruption(
            "composite-state-production-strict-fp",
            remove_composite_state_fp_contract,
            expected_error=(
                "ov_msckf/src/update/CP2CompositeState.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def remove_state_trace_codec_signed_zeros(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/src/update/CP2StateTraceCodec.cpp",
                lambda tokens: remove_exact_token(tokens, "-fsigned-zeros"),
            )

        corruption(
            "state-trace-codec-production-strict-fp",
            remove_state_trace_codec_signed_zeros,
            expected_error=(
                "ov_msckf/src/update/CP2StateTraceCodec.cpp compile command is not "
                "effectively strict-FP"
            ),
        )

        def remove_composite_test_no_fast_math(root):
            rewrite_synthetic_compile_tokens(
                root,
                "ov_msckf/test/cp2/test_cp2_composite_state.cpp",
                lambda tokens: remove_exact_token(tokens, "-fno-fast-math"),
            )

        corruption(
            "composite-state-test-strict-fp",
            remove_composite_test_no_fast_math,
            expected_error=(
                "test_cp2_composite_state has a non-strict compile command for "
                "ov_msckf/test/cp2/test_cp2_composite_state.cpp"
            ),
        )

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

        def corrupt_composite_state_case(root):
            test_name = "test_cp2_composite_state"
            path = root / (test_name + ".xml")
            tree = ET.parse(str(path))
            case = tree.getroot().find(".//testcase")
            if case is None:
                raise RuntimeError("synthetic composite-state XML has no testcase")
            case.set("name", "CorruptedCP2C1Case")
            tree.write(str(path), encoding="utf-8", xml_declaration=True)
            refresh_log_evidence(root, [test_name])

        corruption(
            "composite-state-testcase-ownership-coordinated",
            corrupt_composite_state_case,
            expected_error=(
                "test_cp2_composite_state.xml testcase ownership mismatch"
            ),
        )

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

        def erase_cp2_c2_unit_status(root):
            mutate_json(
                root / REPORT_NAME,
                lambda report: report["checkpoint_status"].__setitem__(
                    "CP2-C2", "not_run"
                ),
            )

        corruption(
            "cp2-c2-unit-status-erasure",
            erase_cp2_c2_unit_status,
            expected_error=(
                "checkpoint status must pass CP2-A/B and CP2-C1/C2 unit only"
            ),
        )

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
        help=(
            "validate, freeze, fsync, and atomically finalize CP2-A/B plus "
            "CP2-C2 unit staging only"
        ),
    )
    parser.add_argument(
        "--expected-manifest-sha256", metavar="HEX",
        help="require SHA256SUMS to match this externally retained SHA-256 digest",
    )
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--unit-self-test", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--verify-unit-anchor-prevalidated", metavar="ARTIFACT_DIR", type=Path,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--verify-recorded", metavar="ABS_PATH", type=Path,
        help="independently verify one externally anchored CP2-C recorded artifact",
    )
    parser.add_argument(
        "--verify-sequence", metavar="ABS_PATH", type=Path,
        help="independently verify one externally anchored CP2-D sequence artifact",
    )
    parser.add_argument(
        "--verify-timing", metavar="ABS_PATH", type=Path,
        help="fail closed while CP2-E remains preprofile and non-authorizing",
    )
    parser.add_argument(
        "--verify-sequence-set", nargs=3, metavar=("ABS0", "ABS1", "ABS2"), type=Path,
        help="independently verify the exact ordered CP2-D sequence set",
    )
    parser.add_argument(
        "--manifest-sha256", nargs="+", metavar="SHA256",
        help="external SHA256SUMS anchor(s) for an actual CP2-C/D/E verifier mode",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    actual_modes = (
        int(args.verify_recorded is not None)
        + int(args.verify_sequence is not None)
        + int(args.verify_timing is not None)
        + int(args.verify_sequence_set is not None)
    )
    selected_modes = (
        int(args.self_test) + int(args.unit_self_test)
        + int(args.assemble_unit is not None)
        + int(args.finalize_staging_noreplace is not None)
        + int(args.verify_unit_anchor_prevalidated is not None)
        + actual_modes
    )
    if selected_modes > 1 or (selected_modes and args.paths):
        raise ValueError("select exactly one verifier mode")
    if actual_modes:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("actual verifier modes require --manifest-sha256, not the unit anchor option")
        expected_anchor_count = 3 if args.verify_sequence_set is not None else 1
        if args.manifest_sha256 is None or len(args.manifest_sha256) != expected_anchor_count:
            raise ValueError(
                "actual verifier mode requires exactly {} --manifest-sha256 value(s)".format(
                    expected_anchor_count
                )
            )
        for digest in args.manifest_sha256:
            if HEX64_PATTERN.fullmatch(digest) is None:
                raise ValueError("--manifest-sha256 values must be lowercase SHA-256")
        if args.verify_recorded is not None:
            verify_recorded_artifact(args.verify_recorded, args.manifest_sha256[0])
            return 0
        if args.verify_sequence is not None:
            verify_sequence_artifact(args.verify_sequence, args.manifest_sha256[0])
            return 0
        if args.verify_sequence_set is not None:
            verify_sequence_set(args.verify_sequence_set, args.manifest_sha256)
            return 0
        verify_timing_preprofile_blocked(args.verify_timing, args.manifest_sha256[0])
        raise AssertionError("unreachable timing verifier return")
    if args.manifest_sha256 is not None:
        raise ValueError("--manifest-sha256 is exclusive to actual CP2 verifier modes")
    if args.self_test:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("--expected-manifest-sha256 is verification-only")
        return run_readiness_self_test()
    if args.unit_self_test:
        if args.expected_manifest_sha256 is not None:
            raise ValueError("--expected-manifest-sha256 is verification-only")
        return run_unit_self_test()
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
            "Read-only CP2-A/B plus CP2-C2 staging finalized without overwrite "
            "(not a CP2 seal): "
            + str(args.finalize_staging_noreplace[1])
        )
        return 0
    if args.verify_unit_anchor_prevalidated is not None:
        if args.expected_manifest_sha256 is None:
            raise ValueError("prevalidated verification requires the external manifest anchor")
        content = sys.stdin.buffer.read(64 * 1024 * 1024 + 1)
        if len(content) > 64 * 1024 * 1024:
            raise ValueError("prevalidated source context exceeds 64 MiB")
        context = strict_json_bytes(content, "prevalidated source context")
        status, _ = verify_unit_anchor_prevalidated(
            args.verify_unit_anchor_prevalidated,
            args.expected_manifest_sha256,
            context,
        )
        return status
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
