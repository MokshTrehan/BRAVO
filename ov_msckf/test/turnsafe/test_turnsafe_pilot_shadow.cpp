/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Focused TurnSafe T1 offline shadow-orchestration tests.
 */

#include "update/TurnSafePilotShadow.h"

#include <gtest/gtest.h>

#include <Eigen/Geometry>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <string>

namespace {

using ov_msckf::TurnSafeCertificateCamera;
using ov_msckf::TurnSafeCertificateObservation;
using ov_msckf::TurnSafePairSelectionRole;
using ov_msckf::TurnSafePairSelectionStatus;
using ov_msckf::TurnSafePilotCallbackInput;
using ov_msckf::TurnSafePilotGroupInput;
using ov_msckf::TurnSafePilotMemberInput;
using ov_msckf::TurnSafePilotPairPrior;
using ov_msckf::TurnSafePilotShadow;

TurnSafeCertificateCamera camera(std::size_t id, double baseline_x) {
  TurnSafeCertificateCamera value;
  value.camera_id = id;
  value.intrinsics << 200.0, 200.0, 320.0, 240.0,
      0.0, 0.0, 0.0, 0.0;
  value.R_ItoC = Eigen::Matrix3d::Identity();
  value.p_IinC = Eigen::Vector3d(baseline_x, 0.0, 0.0);
  value.width = 640;
  value.height = 480;
  return value;
}

TurnSafeCertificateObservation observation(
    std::uint64_t feature_id, std::uint64_t detached_index,
    std::size_t camera_id, double timestamp,
    const Eigen::Vector2d &normalized) {
  TurnSafeCertificateObservation value;
  value.feature_id = feature_id;
  value.detached_index = detached_index;
  value.camera_id = camera_id;
  value.timestamp = timestamp;
  value.captured_normalized = normalized;
  value.raw_pixel = Eigen::Vector2d(
      200.0 * normalized.x() + 320.0,
      200.0 * normalized.y() + 240.0);
  return value;
}

TurnSafePilotCallbackInput complete_callback() {
  TurnSafePilotCallbackInput callback;
  callback.callback_index = 17U;
  callback.callback_timestamp = 2.0;
  callback.raw_group_count = 1U;
  callback.cameras = {camera(0U, 0.0), camera(1U, 0.2)};

  TurnSafePilotPairPrior prior;
  prior.key.camera_id = 0U;
  prior.key.source_timestamp_key = "s";
  prior.key.target_timestamp_key = "t";
  prior.source_position_G.setZero();
  prior.target_position_G.setZero();
  prior.covariance.P_ss.setZero();
  prior.covariance.P_tt.setZero();
  prior.covariance.P_st.setZero();
  prior.covariance.P_ts.setZero();
  callback.pair_priors.push_back(prior);

  TurnSafePilotGroupInput group;
  group.key = prior.key;
  group.source_timestamp = 1.0;
  group.target_timestamp = 2.0;
  // Keep the synthetic points close enough that the frozen 0.99865 one-sided
  // lower bound remains positive under 1.2 px noise.
  constexpr double depth = 2.0;
  constexpr double stereo_baseline = 0.2;
  const std::array<Eigen::Vector2d, 4> target_normalized{{
      Eigen::Vector2d(-1.2825, -0.9625),
      Eigen::Vector2d(1.2775, -0.9625),
      Eigen::Vector2d(-1.2825, 0.9575),
      Eigen::Vector2d(1.2775, 0.9575),
  }};
  for (std::size_t index = 0U; index < target_normalized.size(); ++index) {
    TurnSafePilotMemberInput member;
    member.key.camera_id = 0U;
    member.key.source_timestamp_key = "s";
    member.key.target_timestamp_key = "t";
    member.key.feature_id = 100U + index;
    member.key.detached_index = index;
    member.key.source_observation_ordinal = 0U;
    member.key.target_observation_ordinal = 1U;
    member.stereo_camera_id = 1U;
    member.source_observation = observation(
        member.key.feature_id, member.key.detached_index, 0U, 1.0,
        target_normalized[index]);
    member.target_observation = observation(
        member.key.feature_id, member.key.detached_index, 0U, 2.0,
        target_normalized[index]);
    const Eigen::Vector2d mate_normalized(
        target_normalized[index].x() + stereo_baseline / depth,
        target_normalized[index].y());
    member.target_stereo_observation = observation(
        member.key.feature_id, member.key.detached_index, 1U, 2.0,
        mate_normalized);
    group.members.push_back(member);
  }
  callback.groups.push_back(group);
  return callback;
}

void append_second_group(TurnSafePilotCallbackInput &callback) {
  TurnSafePilotPairPrior prior = callback.pair_priors.front();
  prior.key.source_timestamp_key = "u";
  prior.key.target_timestamp_key = "v";
  callback.pair_priors.push_back(prior);

  TurnSafePilotGroupInput group = callback.groups.front();
  group.key = prior.key;
  for (auto &member : group.members) {
    member.key.source_timestamp_key = prior.key.source_timestamp_key;
    member.key.target_timestamp_key = prior.key.target_timestamp_key;
  }
  callback.groups.push_back(group);
  callback.raw_group_count = callback.groups.size();
}

void rotate_target_measurements(TurnSafePilotGroupInput &group,
                                double angle) {
  const Eigen::Matrix3d rotation =
      Eigen::AngleAxisd(angle, Eigen::Vector3d::UnitZ()).toRotationMatrix();
  constexpr double normalized_disparity = 0.1;
  const std::array<Eigen::Vector2d, 4U> consensus_jitter{{
      Eigen::Vector2d(-1.0, -2.0), Eigen::Vector2d(2.0, -1.0),
      Eigen::Vector2d(1.0, 3.0), Eigen::Vector2d(-3.0, 2.0)}};
  for (std::size_t index = 0U; index < group.members.size(); ++index) {
    auto &member = group.members[index];
    const Eigen::Vector2d source =
        member.source_observation.captured_normalized;
    const Eigen::Vector3d source_bearing =
        Eigen::Vector3d(source.x(), source.y(), 1.0).normalized();
    const Eigen::Vector3d target_bearing = rotation * source_bearing;
    const Eigen::Vector2d target =
        Eigen::Vector2d(target_bearing.x() / target_bearing.z(),
                        target_bearing.y() / target_bearing.z()) +
        1.0e-4 * consensus_jitter[index];
    member.target_observation = observation(
        member.key.feature_id, member.key.detached_index, 0U,
        group.target_timestamp, target);
    member.target_stereo_observation = observation(
        member.key.feature_id, member.key.detached_index, 1U,
        group.target_timestamp,
        Eigen::Vector2d(target.x() + normalized_disparity, target.y()));
  }
}

TEST(TurnSafePilotShadow, EmptyCallbackIsAcceptedWithoutWinner) {
  TurnSafePilotCallbackInput callback;
  callback.callback_index = 4U;
  callback.callback_timestamp = 1.0;
  const auto result = TurnSafePilotShadow::Evaluate(callback);
  ASSERT_TRUE(result.accepted());
  EXPECT_EQ(result.counters.typed_groups, 0U);
  EXPECT_EQ(result.counters.winner_count, 0U);
  EXPECT_EQ(result.selection.status,
            TurnSafePairSelectionStatus::kNoEligibleGroup);
  EXPECT_FALSE(result.post_nis.frozen_winner_available);
  EXPECT_FALSE(result.post_nis.runner_up_promoted);
}

TEST(TurnSafePilotShadow, CompleteSyntheticGroupFreezesOneWinnerBeforeNis) {
  const auto result = TurnSafePilotShadow::Evaluate(complete_callback());
  ASSERT_TRUE(result.accepted())
      << ov_msckf::turnsafe_pilot_shadow_status_name(result.status);
  EXPECT_EQ(result.counters.raw_groups, 1U);
  EXPECT_EQ(result.counters.typed_groups, 1U);
  EXPECT_EQ(result.counters.raw_members, 4U);
  EXPECT_EQ(result.counters.stereo_valid_members, 4U);
  EXPECT_EQ(result.counters.range_lcb_valid_members, 4U);
  EXPECT_EQ(result.counters.translation_ucb_valid_members, 4U);
  EXPECT_EQ(result.counters.acute_members, 4U);
  EXPECT_EQ(result.counters.rho_passed_members, 4U);
  EXPECT_EQ(result.counters.stereo_valid_groups, 1U);
  EXPECT_EQ(result.counters.range_lcb_valid_groups, 1U);
  EXPECT_EQ(result.counters.translation_ucb_valid_groups, 1U);
  EXPECT_EQ(result.counters.acute_groups, 1U);
  EXPECT_EQ(result.counters.rho_groups, 1U);
  EXPECT_EQ(result.counters.groups_n_ge_4, 1U);
  EXPECT_EQ(result.counters.consensus_groups, 1U);
  EXPECT_EQ(result.counters.spatial_groups, 1U);
  EXPECT_EQ(result.counters.rank_groups, 1U);
  EXPECT_EQ(result.counters.conditioning_groups, 1U);
  EXPECT_EQ(result.counters.winner_count, 1U);
  EXPECT_EQ(result.counters.winner_nis_evaluated_count, 1U);
  EXPECT_EQ(result.counters.winner_nis_pass_count, 1U);
  EXPECT_EQ(result.counters.winner_information_non_negligible_count, 1U);
  ASSERT_TRUE(result.selection.winner_available);
  EXPECT_TRUE(result.selection.winner_frozen_before_nis);
  EXPECT_TRUE(result.selection.no_runner_up_gate_shopping);
  EXPECT_TRUE(result.post_nis.frozen_winner_available);
  EXPECT_TRUE(result.post_nis.winner_nis_passed);
  EXPECT_TRUE(result.post_nis.accepted_winner_available);
  EXPECT_FALSE(result.post_nis.runner_up_promoted);
  ASSERT_TRUE(result.winner_factor.accepted())
      << ov_msckf::turnsafe_bearing_group_status_name(
             result.winner_factor.status);
  EXPECT_GT(result.winner_factor.information_trace, 1.0e-12);
  EXPECT_TRUE(result.winner_factor.information_non_negligible);
}

TEST(TurnSafePilotShadow,
     IdentityIsFirstTerminalReasonAndSurvivalCountersAreCumulative) {
  TurnSafePilotCallbackInput callback = complete_callback();
  TurnSafePilotMemberInput &first = callback.groups[0U].members[0U];
  ++first.source_observation.feature_id;
  first.source_observation.raw_pixel = Eigen::Vector2d(-1.0, -1.0);

  const auto result = TurnSafePilotShadow::Evaluate(callback);
  ASSERT_TRUE(result.accepted());
  ASSERT_EQ(result.evaluations.size(), 1U);
  ASSERT_EQ(result.evaluations[0U].candidates.size(), 4U);
  EXPECT_EQ(result.evaluations[0U].candidates[0U].terminal_reason,
            "SOURCE_FEATURE_IDENTITY_MISMATCH");
  EXPECT_FALSE(result.evaluations[0U].candidates[0U].source_bearing.available());
  EXPECT_EQ(result.counters.stereo_valid_members, 3U);
  EXPECT_EQ(result.counters.range_lcb_valid_members, 3U);
  EXPECT_EQ(result.counters.translation_ucb_valid_members, 3U);
  EXPECT_EQ(result.counters.acute_members, 3U);
  EXPECT_EQ(result.counters.rho_passed_members, 3U);
  EXPECT_EQ(result.counters.stereo_valid_groups, 0U);
  EXPECT_EQ(result.counters.range_lcb_valid_groups, 0U);
  EXPECT_EQ(result.counters.translation_ucb_valid_groups, 0U);
  EXPECT_EQ(result.counters.acute_groups, 0U);
  EXPECT_EQ(result.counters.rho_groups, 0U);
  EXPECT_EQ(result.counters.groups_n_ge_4, 0U);
  EXPECT_EQ(result.counters.consensus_groups, 0U);
  EXPECT_EQ(result.counters.spatial_groups, 0U);
  EXPECT_EQ(result.counters.rank_groups, 0U);
  EXPECT_EQ(result.counters.conditioning_groups, 0U);
  EXPECT_EQ(result.counters.winner_count, 0U);
  ASSERT_EQ(result.counters.rejection_reasons.size(), 2U);
  EXPECT_EQ(result.counters.rejection_reasons.at(
                "SOURCE_FEATURE_IDENTITY_MISMATCH"),
            1U);
  EXPECT_EQ(result.counters.rejection_reasons.at(
                "SHADOW_ONLY_SMALL_GROUP"),
            1U);
}

TEST(TurnSafePilotShadow,
     CanonicalGroupAndMemberOrderMakesWinnerTelemetryPermutationInvariant) {
  TurnSafePilotCallbackInput canonical = complete_callback();
  append_second_group(canonical);
  const auto expected = TurnSafePilotShadow::Evaluate(canonical);
  ASSERT_TRUE(expected.accepted());

  TurnSafePilotCallbackInput permuted = canonical;
  std::reverse(permuted.groups.begin(), permuted.groups.end());
  for (auto &group : permuted.groups)
    std::reverse(group.members.begin(), group.members.end());
  std::reverse(permuted.pair_priors.begin(), permuted.pair_priors.end());
  std::reverse(permuted.cameras.begin(), permuted.cameras.end());
  const auto actual = TurnSafePilotShadow::Evaluate(permuted);
  ASSERT_TRUE(actual.accepted());
  ASSERT_EQ(expected.evaluations.size(), actual.evaluations.size());
  ASSERT_TRUE(expected.selection.winner_available);
  ASSERT_TRUE(actual.selection.winner_available);
  EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
      expected.selection.winner_key, actual.selection.winner_key));
  EXPECT_DOUBLE_EQ(expected.winner_factor.nis, actual.winner_factor.nis);
  EXPECT_DOUBLE_EQ(expected.winner_factor.information_trace,
                   actual.winner_factor.information_trace);
  EXPECT_EQ(expected.counters.rejection_reasons,
            actual.counters.rejection_reasons);
  for (std::size_t group_index = 0U;
       group_index < expected.evaluations.size(); ++group_index) {
    EXPECT_TRUE(ov_msckf::turnsafe_pair_group_key_equal(
        expected.evaluations[group_index].key,
        actual.evaluations[group_index].key));
    ASSERT_EQ(expected.evaluations[group_index].candidates.size(),
              actual.evaluations[group_index].candidates.size());
    for (std::size_t member_index = 0U;
         member_index < expected.evaluations[group_index].candidates.size();
         ++member_index) {
      EXPECT_TRUE(ov_msckf::turnsafe_pair_member_key_equal(
          expected.evaluations[group_index].candidates[member_index].key,
          actual.evaluations[group_index].candidates[member_index].key));
    }
  }
}

TEST(TurnSafePilotShadow,
     FrozenWinnerNisFailureNeverEvaluatesOrPromotesForegoneGroup) {
  TurnSafePilotCallbackInput callback = complete_callback();
  append_second_group(callback);
  callback.groups[0U].target_current_R_GtoI =
      Eigen::AngleAxisd(0.10, Eigen::Vector3d::UnitZ()).toRotationMatrix();
  rotate_target_measurements(callback.groups[0U], 0.15);
  std::reverse(callback.groups.begin(), callback.groups.end());

  const auto result = TurnSafePilotShadow::Evaluate(callback);
  ASSERT_TRUE(result.accepted());
  ASSERT_TRUE(result.selection.winner_available);
  EXPECT_EQ(result.selection.winner_key.source_timestamp_key, "s");
  ASSERT_EQ(result.selection.groups.size(), 2U);
  EXPECT_EQ(result.selection.groups[0U].selection_role,
            TurnSafePairSelectionRole::kWinner);
  EXPECT_EQ(result.selection.groups[1U].selection_role,
            TurnSafePairSelectionRole::kForegoneEligible);
  EXPECT_TRUE(result.selection.groups[1U]
                  .predicted_orientation_information_non_negligible);
  EXPECT_GT(result.selection.groups[1U]
                .predicted_orientation_information_trace,
            1.0e-12);
  EXPECT_EQ(result.counters.winner_count, 1U);
  EXPECT_EQ(result.counters.foregone_eligible_groups, 1U);
  EXPECT_EQ(result.counters.foregone_eligible_features, 4U);
  EXPECT_EQ(result.counters.winner_nis_evaluated_count, 1U);
  EXPECT_EQ(result.counters.winner_nis_pass_count, 0U);
  EXPECT_GT(result.winner_factor.nis, result.winner_factor.nis_threshold);
  EXPECT_FALSE(result.winner_factor.nis_passed);
  EXPECT_TRUE(result.post_nis.frozen_winner_available);
  EXPECT_FALSE(result.post_nis.winner_nis_passed);
  EXPECT_FALSE(result.post_nis.accepted_winner_available);
  EXPECT_FALSE(result.post_nis.runner_up_promoted);
  EXPECT_EQ(result.counters.rejection_reasons.at("WINNER_NIS_REJECTED"),
            1U);
}

TEST(TurnSafePilotShadow, TypedGroupsRequireExactlyTwoConfiguredCameras) {
  TurnSafePilotCallbackInput callback = complete_callback();
  callback.cameras.pop_back();
  const auto result = TurnSafePilotShadow::Evaluate(callback);
  EXPECT_FALSE(result.accepted());
  EXPECT_STREQ(ov_msckf::turnsafe_pilot_shadow_status_name(result.status),
               "UNSUPPORTED_CAMERA_COUNT");
}

TEST(TurnSafePilotShadow, MissingCapturedPairPriorFailsClosed) {
  TurnSafePilotCallbackInput callback = complete_callback();
  callback.pair_priors.clear();
  const auto result = TurnSafePilotShadow::Evaluate(callback);
  EXPECT_FALSE(result.accepted());
  EXPECT_STREQ(ov_msckf::turnsafe_pilot_shadow_status_name(result.status),
               "MISSING_PAIR_PRIOR");
  EXPECT_EQ(result.counters.winner_count, 0U);
}

} // namespace
