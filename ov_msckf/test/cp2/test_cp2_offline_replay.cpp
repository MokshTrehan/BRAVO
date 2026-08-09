/*
 * SchurVIO-Lite CP2 offline-replay command boundary tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2OfflineReplay.h"
#include "update/CP2OfflineReplayInternal.inc"

#include <gtest/gtest.h>

#include <array>

using namespace ov_msckf;
namespace replay_internal = ov_msckf::cp2_offline_replay_internal;

namespace {

CP2ShadowMathResult passing_rejected_feature_result() {
  CP2ShadowMathResult result;
  result.traversal_complete = true;
  result.raw_layouts_valid = true;
  result.nullspace_assembly_valid = true;
  result.schur_assembly_valid = true;
  result.nullspace.gamma_status = CP2GammaStatus::kAvailable;
  result.nullspace.retained_gamma = 0.0;
  result.schur.gamma_status = CP2GammaStatus::kAvailable;
  result.schur.retained_gamma = 0.0;
  CP2FeaturePairResult feature;
  feature.raw_layout_valid = true;
  // This is a legitimate reducer/gate rejection: no sufficient-statistic
  // comparison is required, and the default unavailable comparisons must not
  // make detached replay fail it.
  feature.statistics_comparison_required = false;
  result.features.push_back(feature);
  return result;
}

} // namespace

TEST(CP2OfflineReplay, AcceptsOnlyTheExactAbsoluteCommandSurface) {
  const std::array<const char *, 5U> argv = {{
      "/tmp/cp2/bin/ros1_serial_msckf", "--cp2-offline-replay",
      "/tmp/cp2/artifact", "--output", "/tmp/cp2/replay-report.json"}};
  CP2OfflineReplayPaths paths;
  const CP2OfflineReplayResult result = ParseCP2OfflineReplayArguments(
      static_cast<int>(argv.size()), argv.data(), paths);
  ASSERT_TRUE(result.accepted()) << result.detail;
  EXPECT_EQ(paths.executable_path, argv[0]);
  EXPECT_EQ(paths.artifact_directory, argv[2]);
  EXPECT_EQ(paths.output_path, argv[4]);
}

TEST(CP2OfflineReplay, RejectsExtraMissingRelativeAndAliasedArguments) {
  CP2OfflineReplayPaths paths;
  const std::array<const char *, 4U> missing = {{
      "/tmp/cp2/runner", "--cp2-offline-replay", "/tmp/cp2/artifact",
      "--output"}};
  EXPECT_EQ(ParseCP2OfflineReplayArguments(
                static_cast<int>(missing.size()), missing.data(), paths)
                .status,
            CP2OfflineReplayStatus::kInvalidArguments);

  const std::array<const char *, 5U> relative = {{
      "/tmp/cp2/runner", "--cp2-offline-replay", "artifact", "--output",
      "/tmp/cp2/report"}};
  EXPECT_EQ(ParseCP2OfflineReplayArguments(
                static_cast<int>(relative.size()), relative.data(), paths)
                .status,
            CP2OfflineReplayStatus::kInvalidPath);

  const std::array<const char *, 5U> nonnormalized = {{
      "/tmp/cp2/runner", "--cp2-offline-replay", "/tmp/cp2/../artifact",
      "--output", "/tmp/cp2/report"}};
  EXPECT_EQ(ParseCP2OfflineReplayArguments(
                static_cast<int>(nonnormalized.size()), nonnormalized.data(),
                paths)
                .status,
            CP2OfflineReplayStatus::kInvalidPath);

  const std::array<const char *, 5U> alias = {{
      "/tmp/cp2/runner", "--cp2-offline-replay", "/tmp/cp2/artifact",
      "--output", "/tmp/cp2/runner"}};
  EXPECT_EQ(ParseCP2OfflineReplayArguments(
                static_cast<int>(alias.size()), alias.data(), paths)
                .status,
            CP2OfflineReplayStatus::kInvalidPath);
}

TEST(CP2OfflineReplay, StatusNamesAreFrozen) {
  EXPECT_STREQ(cp2_offline_replay_status_name(
                   CP2OfflineReplayStatus::kAccepted),
               "accepted");
  EXPECT_STREQ(cp2_offline_replay_status_name(
                   CP2OfflineReplayStatus::kInvalidArtifact),
               "invalid_artifact");
  EXPECT_STREQ(cp2_offline_replay_status_name(
                   CP2OfflineReplayStatus::kReplayFailed),
               "replay_failed");
}

TEST(CP2OfflineReplay,
     DerivesEveryOnlineShadowMathConditionAndPreservesRejectedFeatures) {
  const CP2ShadowMathResult passing = passing_rejected_feature_result();
  ASSERT_TRUE(replay_internal::InvocationMathEvidencePasses(passing));
  EXPECT_TRUE(
      replay_internal::RecordedAndReplayedMathEvidencePasses(
          true, passing));
  EXPECT_FALSE(
      replay_internal::RecordedAndReplayedMathEvidencePasses(
          false, passing));

  CP2ShadowMathResult changed = passing;
  changed.traversal_complete = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  EXPECT_FALSE(
      replay_internal::RecordedAndReplayedMathEvidencePasses(
          true, changed));
  changed = passing;
  changed.raw_layouts_valid = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.nullspace_assembly_valid = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.schur_assembly_valid = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.duplicate_feature_id = true;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.features[0].raw_layout_valid = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));

  changed = passing;
  changed.nullspace.gamma_status = CP2GammaStatus::kNonfinite;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.schur.gamma_status = CP2GammaStatus::kNonfinite;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));

  changed = passing;
  changed.features[0].nullspace.counters.silent_fallback_count = 1U;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.features[0].schur.regularization_count = 1U;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.nullspace.proposal.diagnostics.alternate_solve_count = 1U;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed = passing;
  changed.schur.proposal.diagnostics.jitter_count = 1U;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));

  changed = passing;
  changed.features[0].statistics_comparison_required = true;
  changed.features[0].lambda_comparison.available = true;
  changed.features[0].lambda_comparison.passed = true;
  changed.features[0].eta_comparison.available = true;
  changed.features[0].eta_comparison.passed = true;
  changed.features[0].gamma_comparison.available = true;
  changed.features[0].gamma_comparison.passed = true;
  ASSERT_TRUE(replay_internal::InvocationMathEvidencePasses(changed));
  changed.features[0].lambda_comparison.available = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed.features[0].lambda_comparison.available = true;
  changed.features[0].eta_comparison.passed = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
  changed.features[0].eta_comparison.passed = true;
  changed.features[0].gamma_comparison.available = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(changed));
}

TEST(CP2OfflineReplay,
     CommitRequiresCandidateAndRecomputedBlocksMustActuallyPass) {
  CP2ShadowMathResult result = passing_rejected_feature_result();
  result.nullspace.proposal_available = true;
  result.schur.proposal_available = false;
  EXPECT_FALSE(replay_internal::InvocationMathEvidencePasses(result));
  result.schur.proposal_available = true;
  EXPECT_TRUE(replay_internal::InvocationMathEvidencePasses(result));

  EXPECT_TRUE(replay_internal::BlockComparisonPasses(true, true));
  EXPECT_FALSE(replay_internal::BlockComparisonPasses(true, false));
  EXPECT_FALSE(replay_internal::BlockComparisonPasses(false, true));
  EXPECT_FALSE(replay_internal::BlockComparisonPasses(false, false));

  EXPECT_TRUE(replay_internal::FrozenSequenceIdentityPasses(
      0U, "MH_01_easy"));
  EXPECT_TRUE(replay_internal::FrozenSequenceIdentityPasses(
      1U, "MH_03_medium"));
  EXPECT_TRUE(replay_internal::FrozenSequenceIdentityPasses(
      2U, "V1_01_easy"));
  EXPECT_FALSE(replay_internal::FrozenSequenceIdentityPasses(
      0U, "MH_03_medium"));
  EXPECT_FALSE(replay_internal::FrozenSequenceIdentityPasses(
      3U, "V1_01_easy"));
  EXPECT_TRUE(replay_internal::UpdatePopulationPasses(0U, 0U));
  EXPECT_TRUE(replay_internal::UpdatePopulationPasses(7U, 7U));
  EXPECT_FALSE(replay_internal::UpdatePopulationPasses(6U, 7U));

  EXPECT_TRUE(replay_internal::ZeroRawTerminalPasses(
      "empty_input", "input_empty", 0U));
  EXPECT_TRUE(replay_internal::ZeroRawTerminalPasses(
      "all_rejected", "no_features_after_cleaning", 1U));
  EXPECT_TRUE(replay_internal::ZeroRawTerminalPasses(
      "all_rejected", "no_features_after_triangulation", 1U));
  EXPECT_TRUE(replay_internal::ZeroRawTerminalPasses(
      "all_rejected", "no_raw_systems", 1U));
  EXPECT_FALSE(replay_internal::ZeroRawTerminalPasses(
      "empty_input", "input_empty", 1U));
  EXPECT_FALSE(replay_internal::ZeroRawTerminalPasses(
      "all_rejected", "no_raw_systems", 0U));
  EXPECT_FALSE(replay_internal::ZeroRawTerminalPasses(
      "empty_input", "no_raw_systems", 0U));
  EXPECT_FALSE(replay_internal::ZeroRawTerminalPasses(
      "all_rejected", "all_baseline_features_rejected", 1U));
  EXPECT_FALSE(replay_internal::ZeroRawTerminalPasses(
      "internal_failure", "trace_invariant_failure", 1U));

  replay_internal::ZeroRawAuxiliaryEvidence auxiliary;
  auxiliary.row_counts_absent = true;
  auxiliary.preview_fields_absent = true;
  auxiliary.candidate_outcome_not_reached = true;
  auxiliary.preview_counters_zero = true;
  auxiliary.candidate_proposal_unavailable = true;
  auxiliary.baseline_commit_oracle_counters_zero = true;
  auxiliary.candidate_write_counters_zero = true;
  ASSERT_TRUE(replay_internal::ZeroRawAuxiliaryEvidencePasses(auxiliary));
  replay_internal::ZeroRawAuxiliaryEvidence changed_auxiliary = auxiliary;
  changed_auxiliary.row_counts_absent = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.preview_fields_absent = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.candidate_outcome_not_reached = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.preview_counters_zero = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.candidate_proposal_unavailable = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.baseline_commit_oracle_counters_zero = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  changed_auxiliary = auxiliary;
  changed_auxiliary.candidate_write_counters_zero = false;
  EXPECT_FALSE(
      replay_internal::ZeroRawAuxiliaryEvidencePasses(changed_auxiliary));
  EXPECT_TRUE(replay_internal::ZeroRawMathEvidencePasses(true));
  EXPECT_FALSE(replay_internal::ZeroRawMathEvidencePasses(false));
}
