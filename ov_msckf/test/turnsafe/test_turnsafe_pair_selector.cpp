/*
 * TurnSafe T1 pilot deterministic pair-selector tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <gtest/gtest.h>

#include "update/TurnSafePairSelector.h"

#include <Eigen/Geometry>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace {

using ov_msckf::TurnSafePairGroupInput;
using ov_msckf::TurnSafePairGroupKey;
using ov_msckf::TurnSafePairGroupResult;
using ov_msckf::TurnSafePairGroupStatus;
using ov_msckf::TurnSafePairMemberInput;
using ov_msckf::TurnSafePairSelectionResult;
using ov_msckf::TurnSafePairSelectionRole;
using ov_msckf::TurnSafePairSelectionStatus;
using ov_msckf::TurnSafePairSelector;
using ov_msckf::TurnSafePairTerminalReason;

TurnSafePairGroupKey group_key(std::size_t camera_id,
                               const std::string &source,
                               const std::string &target) {
  TurnSafePairGroupKey output;
  output.camera_id = camera_id;
  output.source_timestamp_key = source;
  output.target_timestamp_key = target;
  return output;
}

Eigen::Vector3d unit(double x, double y, double z) {
  return Eigen::Vector3d(x, y, z).normalized();
}

Eigen::Matrix<double, 2, 3> local_block(std::size_t index) {
  Eigen::Matrix<double, 2, 3> output;
  switch (index % 4U) {
  case 0U:
    output << 1.0, 0.0, 0.0,
              0.0, 1.0, 0.0;
    break;
  case 1U:
    output << 0.0, 1.0, 0.0,
              0.0, 0.0, 1.0;
    break;
  case 2U:
    output << 1.0, 0.0, 0.0,
              0.0, 0.0, 1.0;
    break;
  default:
    output << 1.0, 1.0, 0.0,
              0.0, 1.0, 1.0;
    break;
  }
  return output;
}

TurnSafePairMemberInput member(
    const TurnSafePairGroupKey &key, std::size_t ordinal,
    const Eigen::Vector3d &predicted, const Eigen::Vector3d &target,
    const Eigen::Vector2d &raw_pixel, double rho = 0.1) {
  TurnSafePairMemberInput output;
  output.key.camera_id = key.camera_id;
  output.key.source_timestamp_key = key.source_timestamp_key;
  output.key.target_timestamp_key = key.target_timestamp_key;
  output.key.feature_id = 100U + static_cast<std::uint64_t>(ordinal);
  output.key.detached_index = 200U + static_cast<std::uint64_t>(ordinal);
  output.key.source_observation_ordinal = ordinal;
  output.key.target_observation_ordinal = ordinal + 10U;
  output.predicted_source_bearing = predicted;
  output.target_bearing = target;
  output.target_raw_pixel = raw_pixel;
  output.image_width = 100;
  output.image_height = 100;
  output.sigma_R = 0.04 * Eigen::Matrix2d::Identity();
  output.whitened_local_rotation_block = local_block(ordinal);
  output.rho_trans = rho;
  return output;
}

TurnSafePairGroupInput passing_group(
    std::size_t camera_id, const std::string &source,
    const std::string &target, double predicted_angle = 0.3) {
  TurnSafePairGroupInput output;
  output.key = group_key(camera_id, source, target);
  output.current_predicted_relative_camera_rotation_angle_rad =
      predicted_angle;
  const std::array<Eigen::Vector3d, 4U> bearings{{
      unit(-0.5, -0.5, 1.0), unit(0.5, -0.5, 1.0),
      unit(0.5, 0.5, 1.0), unit(-0.5, 0.5, 1.0)}};
  const std::array<Eigen::Vector2d, 4U> pixels{{
      Eigen::Vector2d(0.0, 0.0), Eigen::Vector2d(99.0, 0.0),
      Eigen::Vector2d(99.0, 99.0), Eigen::Vector2d(0.0, 99.0)}};
  for (std::size_t index = 0U; index < bearings.size(); ++index) {
    output.members.push_back(member(output.key, index, bearings[index],
                                    bearings[index], pixels[index]));
  }
  return output;
}

void set_fractional_pixel(TurnSafePairMemberInput &input, double x,
                          double y) {
  input.target_raw_pixel = Eigen::Vector2d(
      x * static_cast<double>(input.image_width) - 0.5,
      y * static_cast<double>(input.image_height) - 0.5);
}

void set_identity_score_blocks(TurnSafePairGroupInput &group) {
  for (auto &input : group.members)
    input.whitened_local_rotation_block.setZero();
  ASSERT_GE(group.members.size(), 2U);
  group.members[0U].whitened_local_rotation_block <<
      1.0, 0.0, 0.0,
      0.0, 1.0, 0.0;
  group.members[1U].whitened_local_rotation_block <<
      0.0, 0.0, 1.0,
      0.0, 0.0, 0.0;
}

const TurnSafePairGroupResult *find_group(
    const TurnSafePairSelectionResult &selection,
    const TurnSafePairGroupKey &key) {
  const auto found = std::find_if(
      selection.groups.begin(), selection.groups.end(),
      [&key](const TurnSafePairGroupResult &result) {
        return ov_msckf::turnsafe_pair_group_key_equal(result.key, key);
      });
  return found == selection.groups.end() ? nullptr : &*found;
}

bool is_consensus_rejection(TurnSafePairTerminalReason reason) {
  return reason == TurnSafePairTerminalReason::kConsensusInsufficientInliers ||
         reason ==
             TurnSafePairTerminalReason::kConsensusRefitThresholdFailure;
}

void expect_same_member_key(const ov_msckf::TurnSafePairMemberKey &left,
                            const ov_msckf::TurnSafePairMemberKey &right) {
  EXPECT_TRUE(ov_msckf::turnsafe_pair_member_key_equal(left, right));
}

TEST(TurnSafePairSelectorNames, ExhaustiveAuditNamesAndUnknownValues) {
  static_assert(noexcept(ov_msckf::turnsafe_pair_group_status_name(
                    TurnSafePairGroupStatus::kEligible)),
                "group status names must be noexcept");
  static_assert(noexcept(ov_msckf::turnsafe_pair_terminal_reason_name(
                    TurnSafePairTerminalReason::kNone)),
                "terminal reason names must be noexcept");
  static_assert(noexcept(ov_msckf::turnsafe_pair_selection_status_name(
                    TurnSafePairSelectionStatus::kWinnerFrozen)),
                "selection status names must be noexcept");
  static_assert(noexcept(ov_msckf::turnsafe_pair_selection_role_name(
                    TurnSafePairSelectionRole::kWinner)),
                "selection role names must be noexcept");

  const std::array<TurnSafePairGroupStatus, 4U> statuses{{
      TurnSafePairGroupStatus::kInsufficientGroup,
      TurnSafePairGroupStatus::kShadowOnlySmallGroup,
      TurnSafePairGroupStatus::kRejected,
      TurnSafePairGroupStatus::kEligible}};
  for (const auto value : statuses)
    EXPECT_STRNE(ov_msckf::turnsafe_pair_group_status_name(value), "UNKNOWN");

  const std::array<TurnSafePairTerminalReason, 23U> reasons{{
      TurnSafePairTerminalReason::kNone,
      TurnSafePairTerminalReason::kInsufficientGroup,
      TurnSafePairTerminalReason::kShadowOnlySmallGroup,
      TurnSafePairTerminalReason::kInvalidGroupScore,
      TurnSafePairTerminalReason::kMemberGroupKeyMismatch,
      TurnSafePairTerminalReason::kDuplicateMemberKey,
      TurnSafePairTerminalReason::kMemberPrimitiveInvalid,
      TurnSafePairTerminalReason::kMemberRhoIneligible,
      TurnSafePairTerminalReason::kConsensusSvdFailure,
      TurnSafePairTerminalReason::kConsensusWhiteningFailure,
      TurnSafePairTerminalReason::kConsensusInsufficientInliers,
      TurnSafePairTerminalReason::kConsensusRefitSvdFailure,
      TurnSafePairTerminalReason::kConsensusRefitThresholdFailure,
      TurnSafePairTerminalReason::kSpatialInvalid,
      TurnSafePairTerminalReason::kSpatialHullArea,
      TurnSafePairTerminalReason::kSpatialPixelCovariance,
      TurnSafePairTerminalReason::kSpatialOccupiedCells,
      TurnSafePairTerminalReason::kSpatialSpan,
      TurnSafePairTerminalReason::kRotationStackSvdFailure,
      TurnSafePairTerminalReason::kRotationStackRank,
      TurnSafePairTerminalReason::kRotationStackConditioning,
      TurnSafePairTerminalReason::kWhitenedLocalStackSvdFailure,
      TurnSafePairTerminalReason::kPredictedInformationNonfinite}};
  for (const auto value : reasons)
    EXPECT_STRNE(ov_msckf::turnsafe_pair_terminal_reason_name(value),
                 "UNKNOWN");

  const std::array<TurnSafePairSelectionStatus, 3U> selection_statuses{{
      TurnSafePairSelectionStatus::kNoEligibleGroup,
      TurnSafePairSelectionStatus::kWinnerFrozen,
      TurnSafePairSelectionStatus::kDuplicateGroupKey}};
  for (const auto value : selection_statuses)
    EXPECT_STRNE(ov_msckf::turnsafe_pair_selection_status_name(value),
                 "UNKNOWN");

  const std::array<TurnSafePairSelectionRole, 3U> roles{{
      TurnSafePairSelectionRole::kIneligible,
      TurnSafePairSelectionRole::kWinner,
      TurnSafePairSelectionRole::kForegoneEligible}};
  for (const auto value : roles)
    EXPECT_STRNE(ov_msckf::turnsafe_pair_selection_role_name(value),
                 "UNKNOWN");

  EXPECT_STREQ(ov_msckf::turnsafe_pair_group_status_name(
                   static_cast<TurnSafePairGroupStatus>(255U)),
               "UNKNOWN");
  EXPECT_STREQ(ov_msckf::turnsafe_pair_terminal_reason_name(
                   static_cast<TurnSafePairTerminalReason>(255U)),
               "UNKNOWN");
  EXPECT_STREQ(ov_msckf::turnsafe_pair_selection_status_name(
                   static_cast<TurnSafePairSelectionStatus>(255U)),
               "UNKNOWN");
  EXPECT_STREQ(ov_msckf::turnsafe_pair_selection_role_name(
                   static_cast<TurnSafePairSelectionRole>(255U)),
               "UNKNOWN");
}

TEST(TurnSafePairSelectorGroupSize, N2AndN3RemainShadowOnly) {
  for (const std::size_t count : {2U, 3U}) {
    TurnSafePairGroupInput input = passing_group(0U, "10", "11");
    input.members.resize(count);
    const TurnSafePairGroupResult result =
        TurnSafePairSelector::EvaluateGroup(input);
    EXPECT_EQ(result.status,
              TurnSafePairGroupStatus::kShadowOnlySmallGroup);
    EXPECT_EQ(result.terminal_reason,
              TurnSafePairTerminalReason::kShadowOnlySmallGroup);
    EXPECT_FALSE(result.consensus.attempted);
  }
}

TEST(TurnSafePairSelectorConsensus, PassingN4UsesOneRefit) {
  const TurnSafePairGroupResult result = TurnSafePairSelector::EvaluateGroup(
      passing_group(0U, "10", "11"));
  ASSERT_EQ(result.status, TurnSafePairGroupStatus::kEligible);
  EXPECT_EQ(result.terminal_reason, TurnSafePairTerminalReason::kNone);
  ASSERT_TRUE(result.consensus.first_fit_available);
  ASSERT_TRUE(result.consensus.refit_available);
  EXPECT_EQ(result.consensus.required_retained_count, 3U);
  EXPECT_EQ(result.consensus.retained_member_keys.size(), 4U);
  EXPECT_DOUBLE_EQ(result.consensus.absolute_threshold,
                   3.2848542587702925);
  EXPECT_DOUBLE_EQ(result.consensus.median, 0.0);
  EXPECT_DOUBLE_EQ(result.consensus.mad, 0.0);
  EXPECT_DOUBLE_EQ(result.consensus.robust_threshold, 0.0);
  for (const auto &diagnostic : result.consensus.members) {
    EXPECT_TRUE(diagnostic.retained);
    EXPECT_TRUE(diagnostic.refit_norm_available);
    EXPECT_DOUBLE_EQ(diagnostic.initial_whitened_norm, 0.0);
    EXPECT_DOUBLE_EQ(diagnostic.refit_whitened_norm, 0.0);
  }
  const Eigen::Vector3d &singular =
      result.whitened_local_stack_singular_values;
  EXPECT_DOUBLE_EQ(
      result.predicted_orientation_information_eigenvalues(0),
      singular(2) * singular(2));
  EXPECT_DOUBLE_EQ(
      result.predicted_orientation_information_eigenvalues(1),
      singular(1) * singular(1));
  EXPECT_DOUBLE_EQ(
      result.predicted_orientation_information_eigenvalues(2),
      singular(0) * singular(0));
  EXPECT_DOUBLE_EQ(
      result.predicted_orientation_information_trace,
      result.predicted_orientation_information_eigenvalues.sum());
  EXPECT_DOUBLE_EQ(
      result.predicted_orientation_information_determinant,
      result.predicted_orientation_information_eigenvalues.prod());
  EXPECT_TRUE(result.predicted_orientation_information_non_negligible);
}

TEST(TurnSafePairSelectorConsensus, EvenSampleUsesLowerMedianForQAndMad) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  const std::array<Eigen::Vector3d, 4U> axes{{
      unit(1.0, 2.0, 3.0), unit(-2.0, 1.0, 0.5),
      unit(0.2, -1.0, 2.0), unit(2.0, 0.3, -1.0)}};
  const std::array<double, 4U> angles{{0.01, -0.025, 0.04, -0.07}};
  for (std::size_t index = 0U; index < input.members.size(); ++index) {
    input.members[index].target_bearing =
        Eigen::AngleAxisd(angles[index], axes[index]) *
        input.members[index].predicted_source_bearing;
  }
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  ASSERT_TRUE(result.consensus.first_fit_available);
  ASSERT_EQ(result.consensus.members.size(), 4U);
  std::vector<double> norms;
  for (const auto &diagnostic : result.consensus.members)
    norms.push_back(diagnostic.initial_whitened_norm);
  std::sort(norms.begin(), norms.end());
  EXPECT_DOUBLE_EQ(result.consensus.median, norms[1U]);
  std::vector<double> deviations;
  for (const double norm : norms)
    deviations.push_back(std::fabs(norm - result.consensus.median));
  std::sort(deviations.begin(), deviations.end());
  EXPECT_DOUBLE_EQ(result.consensus.mad, deviations[1U]);
  EXPECT_DOUBLE_EQ(
      result.consensus.robust_threshold,
      result.consensus.median +
          4.0 * 1.482602218505602 * result.consensus.mad);
}

TEST(TurnSafePairSelectorConsensus, N4SingleGrossOutlierRejects) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  input.members[3U].target_bearing = unit(0.91, -0.20, 0.36);
  for (auto &candidate : input.members)
    candidate.sigma_R = 1.0e-10 * Eigen::Matrix2d::Identity();
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.status, TurnSafePairGroupStatus::kRejected);
  EXPECT_TRUE(is_consensus_rejection(result.terminal_reason));
}

TEST(TurnSafePairSelectorConsensus, N4NonRigidCorrespondencesReject) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  const std::array<Eigen::Vector3d, 4U> targets{{
      unit(0.9, 0.1, 0.4), unit(-0.2, 0.95, 0.3),
      unit(-0.8, -0.1, 0.6), unit(0.1, -0.85, 0.5)}};
  for (std::size_t index = 0U; index < input.members.size(); ++index) {
    input.members[index].target_bearing = targets[index];
    input.members[index].sigma_R =
        1.0e-10 * Eigen::Matrix2d::Identity();
  }
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.status, TurnSafePairGroupStatus::kRejected);
  EXPECT_TRUE(is_consensus_rejection(result.terminal_reason));
}

TEST(TurnSafePairSelectorConsensus, NonPositiveDefiniteWhiteningFailsClosed) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  input.members[2U].sigma_R << 1.0, 2.0, 2.0, 1.0;
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.status, TurnSafePairGroupStatus::kRejected);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kConsensusWhiteningFailure);
}

TEST(TurnSafePairSelectorSpatial, HullAreaGateIsFirst) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  set_fractional_pixel(input.members[0U], 0.40, 0.40);
  set_fractional_pixel(input.members[1U], 0.45, 0.40);
  set_fractional_pixel(input.members[2U], 0.45, 0.45);
  set_fractional_pixel(input.members[3U], 0.40, 0.45);
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kSpatialHullArea);
}

TEST(TurnSafePairSelectorSpatial, PopulationCovarianceGateIsInclusiveOrder) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  set_fractional_pixel(input.members[0U], 0.01, 0.45);
  set_fractional_pixel(input.members[1U], 0.99, 0.45);
  set_fractional_pixel(input.members[2U], 0.99, 0.56);
  set_fractional_pixel(input.members[3U], 0.01, 0.56);
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  ASSERT_GE(result.spatial.convex_hull_area, 0.1);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kSpatialPixelCovariance);
}

TEST(TurnSafePairSelectorSpatial, FourFixedCellsAreRequired) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  set_fractional_pixel(input.members[0U], 0.01, 0.01);
  set_fractional_pixel(input.members[1U], 0.24, 0.24);
  set_fractional_pixel(input.members[2U], 0.99, 0.01);
  set_fractional_pixel(input.members[3U], 0.01, 0.99);
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  ASSERT_GE(result.spatial.convex_hull_area, 0.1);
  ASSERT_GE(result.spatial.minimum_population_covariance_eigenvalue, 0.03);
  EXPECT_EQ(result.spatial.occupied_fixed_4x4_cells, 3U);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kSpatialOccupiedCells);
}

TEST(TurnSafePairSelectorSpatial, BothAxisSpansAreRequired) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  set_fractional_pixel(input.members[0U], 0.20, 0.01);
  set_fractional_pixel(input.members[1U], 0.79, 0.01);
  set_fractional_pixel(input.members[2U], 0.79, 0.99);
  set_fractional_pixel(input.members[3U], 0.20, 0.99);
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  ASSERT_EQ(result.spatial.occupied_fixed_4x4_cells, 4U);
  EXPECT_DOUBLE_EQ(result.spatial.x_span, 0.59);
  EXPECT_EQ(result.terminal_reason, TurnSafePairTerminalReason::kSpatialSpan);
}

TEST(TurnSafePairSelectorGeometry, TargetBearingStackMustHaveRankThree) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  for (auto &candidate : input.members) {
    candidate.predicted_source_bearing = Eigen::Vector3d::UnitZ();
    candidate.target_bearing = Eigen::Vector3d::UnitZ();
  }
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.rotation_stack_rank, 2U);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kRotationStackRank);
}

TEST(TurnSafePairSelectorGeometry, PracticalConditionRatioIsBinding) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  const std::array<Eigen::Vector3d, 4U> bearings{{
      unit(-0.01, -0.01, 1.0), unit(0.01, -0.01, 1.0),
      unit(0.01, 0.01, 1.0), unit(-0.01, 0.01, 1.0)}};
  for (std::size_t index = 0U; index < input.members.size(); ++index) {
    input.members[index].predicted_source_bearing = bearings[index];
    input.members[index].target_bearing = bearings[index];
  }
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.rotation_stack_rank, 3U);
  EXPECT_LT(result.rotation_stack_condition_ratio, 0.15);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kRotationStackConditioning);
}

TEST(TurnSafePairSelectorGeometry,
     FiniteWhitenedStackInformationOverflowFailsClosed) {
  TurnSafePairGroupInput input = passing_group(0U, "10", "11");
  for (auto &member : input.members)
    member.whitened_local_rotation_block *= 1.0e200;
  const TurnSafePairGroupResult result =
      TurnSafePairSelector::EvaluateGroup(input);
  EXPECT_EQ(result.status, TurnSafePairGroupStatus::kRejected);
  EXPECT_EQ(result.terminal_reason,
            TurnSafePairTerminalReason::kPredictedInformationNonfinite);
  EXPECT_FALSE(std::isfinite(
      result.predicted_orientation_information_trace));
}

TEST(TurnSafePairSelectorDeterminism,
     MemberPermutationProducesBitExactDiagnosticsAndScore) {
  TurnSafePairGroupInput input = passing_group(3U, "100", "101", 0.4);
  const TurnSafePairGroupResult canonical =
      TurnSafePairSelector::EvaluateGroup(input);
  std::reverse(input.members.begin(), input.members.end());
  const TurnSafePairGroupResult reversed =
      TurnSafePairSelector::EvaluateGroup(input);
  ASSERT_EQ(canonical.status, TurnSafePairGroupStatus::kEligible);
  ASSERT_EQ(reversed.status, TurnSafePairGroupStatus::kEligible);
  EXPECT_EQ(canonical.terminal_reason, reversed.terminal_reason);
  ASSERT_EQ(canonical.consensus.members.size(),
            reversed.consensus.members.size());
  ASSERT_EQ(canonical.consensus.retained_member_keys.size(),
            reversed.consensus.retained_member_keys.size());
  for (std::size_t index = 0U;
       index < canonical.consensus.members.size(); ++index) {
    expect_same_member_key(canonical.consensus.members[index].key,
                           reversed.consensus.members[index].key);
    EXPECT_DOUBLE_EQ(
        canonical.consensus.members[index].initial_whitened_norm,
        reversed.consensus.members[index].initial_whitened_norm);
    EXPECT_DOUBLE_EQ(canonical.consensus.members[index].refit_whitened_norm,
                     reversed.consensus.members[index].refit_whitened_norm);
    expect_same_member_key(canonical.consensus.retained_member_keys[index],
                           reversed.consensus.retained_member_keys[index]);
  }
  for (Eigen::Index index = 0; index < 3; ++index) {
    EXPECT_DOUBLE_EQ(canonical.rotation_stack_singular_values(index),
                     reversed.rotation_stack_singular_values(index));
    EXPECT_DOUBLE_EQ(canonical.whitened_local_stack_singular_values(index),
                     reversed.whitened_local_stack_singular_values(index));
  }
  EXPECT_DOUBLE_EQ(canonical.score.predicted_rotation_angle_rad,
                   reversed.score.predicted_rotation_angle_rad);
  EXPECT_DOUBLE_EQ(canonical.score.minimum_whitened_local_singular_value,
                   reversed.score.minimum_whitened_local_singular_value);
  EXPECT_DOUBLE_EQ(canonical.score.maximum_retained_rho_trans,
                   reversed.score.maximum_retained_rho_trans);
  EXPECT_EQ(canonical.score.retained_feature_count,
            reversed.score.retained_feature_count);
  for (Eigen::Index index = 0; index < 3; ++index) {
    EXPECT_DOUBLE_EQ(
        canonical.predicted_orientation_information_eigenvalues(index),
        reversed.predicted_orientation_information_eigenvalues(index));
  }
  EXPECT_DOUBLE_EQ(canonical.predicted_orientation_information_trace,
                   reversed.predicted_orientation_information_trace);
  EXPECT_DOUBLE_EQ(canonical.predicted_orientation_information_determinant,
                   reversed.predicted_orientation_information_determinant);
}

TEST(TurnSafePairSelectorScore, FrozenLexicographicComponentsAreBinding) {
  TurnSafePairGroupInput base = passing_group(0U, "10", "11", 0.3);
  TurnSafePairGroupInput greater_angle =
      passing_group(9U, "20", "21", 0.4);
  TurnSafePairSelectionResult selection =
      TurnSafePairSelector::Select({base, greater_angle});
  ASSERT_TRUE(selection.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
      selection.winner_key, greater_angle.key));

  TurnSafePairGroupInput greater_information =
      passing_group(9U, "20", "21", 0.3);
  for (auto &candidate : greater_information.members)
    candidate.whitened_local_rotation_block *= 2.0;
  selection = TurnSafePairSelector::Select({base, greater_information});
  ASSERT_TRUE(selection.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
      selection.winner_key, greater_information.key));

  TurnSafePairGroupInput lower_rho =
      passing_group(9U, "20", "21", 0.3);
  for (auto &candidate : base.members) candidate.rho_trans = 0.2;
  for (auto &candidate : lower_rho.members) candidate.rho_trans = 0.1;
  selection = TurnSafePairSelector::Select({base, lower_rho});
  ASSERT_TRUE(selection.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(selection.winner_key,
                                                       lower_rho.key));

  TurnSafePairGroupInput five = passing_group(9U, "20", "21", 0.3);
  TurnSafePairGroupInput four = passing_group(0U, "10", "11", 0.3);
  set_identity_score_blocks(four);
  set_identity_score_blocks(five);
  five.members.push_back(member(five.key, 4U, Eigen::Vector3d::UnitZ(),
                                Eigen::Vector3d::UnitZ(),
                                Eigen::Vector2d(49.0, 49.0)));
  five.members.back().whitened_local_rotation_block.setZero();
  selection = TurnSafePairSelector::Select({four, five});
  ASSERT_TRUE(selection.winner_available);
  const TurnSafePairGroupResult *four_result = find_group(selection, four.key);
  const TurnSafePairGroupResult *five_result = find_group(selection, five.key);
  ASSERT_NE(four_result, nullptr);
  ASSERT_NE(five_result, nullptr);
  ASSERT_EQ(four_result->status, TurnSafePairGroupStatus::kEligible);
  ASSERT_EQ(five_result->status, TurnSafePairGroupStatus::kEligible);
  EXPECT_EQ(five_result->consensus.required_retained_count, 4U);
  EXPECT_DOUBLE_EQ(four_result->score.minimum_whitened_local_singular_value,
                   five_result->score.minimum_whitened_local_singular_value);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(selection.winner_key,
                                                       five.key));
}

TEST(TurnSafePairSelectorScore, ExactTieUsesCanonicalBytewiseGroupKey) {
  const std::string lower_byte(1U, static_cast<char>(0x7f));
  const std::string higher_byte(1U, static_cast<char>(0x80));
  TurnSafePairGroupInput lower =
      passing_group(5U, lower_byte, "target", 0.3);
  TurnSafePairGroupInput higher =
      passing_group(5U, higher_byte, "target", 0.3);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_less(lower.key, higher.key));
  const TurnSafePairSelectionResult selection =
      TurnSafePairSelector::Select({higher, lower});
  ASSERT_TRUE(selection.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(selection.winner_key,
                                                       lower.key));
}

TEST(TurnSafePairSelectorWinner, OneWinnerAndAllOtherEligibleAreForegone) {
  TurnSafePairGroupInput first = passing_group(0U, "10", "11", 0.4);
  TurnSafePairGroupInput second = passing_group(1U, "20", "21", 0.3);
  TurnSafePairGroupInput third = passing_group(2U, "30", "31", 0.2);
  const TurnSafePairSelectionResult selection =
      TurnSafePairSelector::Select({third, first, second});
  ASSERT_EQ(selection.status, TurnSafePairSelectionStatus::kWinnerFrozen);
  ASSERT_TRUE(selection.winner_available);
  EXPECT_TRUE(selection.winner_frozen_before_nis);
  EXPECT_TRUE(selection.no_runner_up_gate_shopping);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(selection.winner_key,
                                                       first.key));
  std::size_t winner_count = 0U;
  std::size_t foregone_count = 0U;
  for (const auto &result : selection.groups) {
    if (result.selection_role == TurnSafePairSelectionRole::kWinner)
      ++winner_count;
    if (result.selection_role == TurnSafePairSelectionRole::kForegoneEligible)
      ++foregone_count;
    if (result.status == TurnSafePairGroupStatus::kEligible) {
      EXPECT_TRUE(
          result.predicted_orientation_information_non_negligible);
    }
  }
  EXPECT_EQ(winner_count, 1U);
  EXPECT_EQ(foregone_count, 2U);

  const TurnSafePairSelectionResult reordered =
      TurnSafePairSelector::Select({first, second, third});
  ASSERT_TRUE(reordered.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
      reordered.winner_key, selection.winner_key));
  for (const auto &result : selection.groups) {
    const TurnSafePairGroupResult *other = find_group(reordered, result.key);
    ASSERT_NE(other, nullptr);
    EXPECT_EQ(other->selection_role, result.selection_role);
  }
}

TEST(TurnSafePairSelectorWinner, FailedFrozenWinnerNeverPromotesRunnerUp) {
  TurnSafePairGroupInput winner = passing_group(0U, "10", "11", 0.4);
  TurnSafePairGroupInput runner_up = passing_group(1U, "20", "21", 0.3);
  const TurnSafePairSelectionResult selection =
      TurnSafePairSelector::Select({runner_up, winner});
  ASSERT_TRUE(selection.winner_available);
  const auto rejected =
      TurnSafePairSelector::ApplyFrozenWinnerNis(selection, false);
  EXPECT_TRUE(rejected.frozen_winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
      rejected.frozen_winner_key, winner.key));
  EXPECT_FALSE(rejected.winner_nis_passed);
  EXPECT_FALSE(rejected.accepted_winner_available);
  EXPECT_FALSE(rejected.runner_up_promoted);

  const TurnSafePairGroupResult *runner_result =
      find_group(selection, runner_up.key);
  ASSERT_NE(runner_result, nullptr);
  EXPECT_EQ(runner_result->selection_role,
            TurnSafePairSelectionRole::kForegoneEligible);
}

TEST(TurnSafePairSelectorWinner, DuplicateGroupKeysFailSelectionClosed) {
  const TurnSafePairGroupInput first = passing_group(0U, "10", "11", 0.4);
  TurnSafePairGroupInput duplicate = first;
  std::reverse(duplicate.members.begin(), duplicate.members.end());
  const TurnSafePairSelectionResult selection =
      TurnSafePairSelector::Select({duplicate, first});
  EXPECT_EQ(selection.status,
            TurnSafePairSelectionStatus::kDuplicateGroupKey);
  EXPECT_FALSE(selection.winner_available);
}

} // namespace
