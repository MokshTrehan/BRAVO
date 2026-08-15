/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pilot offline-only shadow orchestration.
 */

#include "TurnSafePilotShadow.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <new>
#include <set>
#include <utility>

namespace ov_msckf {
namespace {

constexpr std::size_t kProductionMinimum = 4U;
constexpr double kSpatialHullMinimum = 0.1;
constexpr double kSpatialCovarianceMinimum = 0.03;
constexpr std::size_t kSpatialCellsMinimum = 4U;
constexpr double kSpatialSpanMinimum = 0.6;
constexpr double kConditioningMinimum = 0.15;

bool finite_rotation(const Eigen::Matrix3d &rotation) {
  if (!rotation.allFinite()) return false;
  const double tolerance = 100.0 * std::numeric_limits<double>::epsilon();
  return (rotation * rotation.transpose() - Eigen::Matrix3d::Identity())
                 .cwiseAbs().maxCoeff() <= tolerance &&
         std::fabs(rotation.determinant() - 1.0) <= tolerance;
}

bool same_binary64(double left, double right) noexcept {
  if (!std::isfinite(left) || !std::isfinite(right)) return false;
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  static_assert(sizeof(left_bits) == sizeof(left),
                "binary64 identity requires 64-bit double");
  std::memcpy(&left_bits, &left, sizeof(left));
  std::memcpy(&right_bits, &right, sizeof(right));
  return left_bits == right_bits;
}

bool member_group_key_matches(const TurnSafePairGroupKey &group,
                              const TurnSafePairMemberKey &member) noexcept {
  return group.camera_id == member.camera_id &&
         group.source_timestamp_key == member.source_timestamp_key &&
         group.target_timestamp_key == member.target_timestamp_key;
}

const char *member_identity_failure(
    const TurnSafePilotGroupInput &group,
    const TurnSafePilotMemberInput &member) noexcept {
  if (!member_group_key_matches(group.key, member.key))
    return "MEMBER_GROUP_KEY_MISMATCH";
  if (member.source_observation.camera_id != group.key.camera_id)
    return "SOURCE_CAMERA_IDENTITY_MISMATCH";
  if (member.source_observation.feature_id != member.key.feature_id ||
      member.source_observation.detached_index != member.key.detached_index)
    return "SOURCE_FEATURE_IDENTITY_MISMATCH";
  if (!same_binary64(member.source_observation.timestamp,
                     group.source_timestamp))
    return "SOURCE_TIMESTAMP_IDENTITY_MISMATCH";
  if (member.target_observation.camera_id != group.key.camera_id)
    return "TARGET_CAMERA_IDENTITY_MISMATCH";
  if (member.target_observation.feature_id != member.key.feature_id ||
      member.target_observation.detached_index != member.key.detached_index)
    return "TARGET_FEATURE_IDENTITY_MISMATCH";
  if (!same_binary64(member.target_observation.timestamp,
                     group.target_timestamp))
    return "TARGET_TIMESTAMP_IDENTITY_MISMATCH";
  if (member.stereo_camera_id == group.key.camera_id ||
      member.target_stereo_observation.camera_id != member.stereo_camera_id)
    return "STEREO_CAMERA_IDENTITY_MISMATCH";
  if (member.target_stereo_observation.feature_id != member.key.feature_id ||
      member.target_stereo_observation.detached_index !=
          member.key.detached_index)
    return "STEREO_FEATURE_IDENTITY_MISMATCH";
  if (!same_binary64(member.target_stereo_observation.timestamp,
                     group.target_timestamp))
    return "STEREO_TIMESTAMP_IDENTITY_MISMATCH";
  return nullptr;
}

double relative_rotation_angle(const Eigen::Matrix3d &rotation) {
  const Eigen::Vector3d skew(rotation(2, 1) - rotation(1, 2),
                             rotation(0, 2) - rotation(2, 0),
                             rotation(1, 0) - rotation(0, 1));
  return std::atan2(0.5 * skew.norm(),
                    0.5 * (rotation.trace() - 1.0));
}

const TurnSafeCertificateCamera *find_camera(
    const std::map<std::size_t, TurnSafeCertificateCamera> &cameras,
    std::size_t camera_id) {
  const auto found = cameras.find(camera_id);
  return found == cameras.end() ? nullptr : &found->second;
}

const TurnSafePilotPairPrior *find_pair(
    const std::vector<TurnSafePilotPairPrior> &pairs,
    const TurnSafePairGroupKey &key) {
  for (const auto &pair : pairs) {
    if (turnsafe_pair_group_key_equal(pair.key, key)) return &pair;
  }
  return nullptr;
}

bool key_in(const std::vector<TurnSafePairMemberKey> &keys,
            const TurnSafePairMemberKey &key) {
  return std::any_of(keys.begin(), keys.end(), [&](const auto &candidate) {
    return turnsafe_pair_member_key_equal(candidate, key);
  });
}

void reject(TurnSafePilotCallbackCounters &counters,
            const std::string &reason) {
  ++counters.rejection_reasons[reason];
}

bool consensus_passed(const TurnSafePairGroupResult &group) {
  // Spatial evaluation is entered only after the one refit and every frozen
  // retained-member threshold check has passed.
  return group.spatial.attempted;
}

bool spatial_passed(const TurnSafePairGroupResult &group) {
  return group.spatial.attempted &&
         std::isfinite(group.spatial.convex_hull_area) &&
         std::isfinite(
             group.spatial.minimum_population_covariance_eigenvalue) &&
         std::isfinite(group.spatial.x_span) &&
         std::isfinite(group.spatial.y_span) &&
         group.spatial.convex_hull_area >= kSpatialHullMinimum &&
         group.spatial.minimum_population_covariance_eigenvalue >=
             kSpatialCovarianceMinimum &&
         group.spatial.occupied_fixed_4x4_cells >= kSpatialCellsMinimum &&
         group.spatial.x_span >= kSpatialSpanMinimum &&
         group.spatial.y_span >= kSpatialSpanMinimum;
}

bool rank_passed(const TurnSafePairGroupResult &group) {
  return group.rotation_stack_rank == 3U;
}

bool conditioning_passed(const TurnSafePairGroupResult &group) {
  return rank_passed(group) &&
         std::isfinite(group.rotation_stack_condition_ratio) &&
         group.rotation_stack_condition_ratio >= kConditioningMinimum;
}

} // namespace

const char *turnsafe_pilot_shadow_status_name(
    TurnSafePilotShadowStatus status) noexcept {
  switch (status) {
  case TurnSafePilotShadowStatus::kAccepted: return "ACCEPTED";
  case TurnSafePilotShadowStatus::kInvalidInput: return "INVALID_INPUT";
  case TurnSafePilotShadowStatus::kUnsupportedCameraCount:
    return "UNSUPPORTED_CAMERA_COUNT";
  case TurnSafePilotShadowStatus::kDuplicateCamera: return "DUPLICATE_CAMERA";
  case TurnSafePilotShadowStatus::kDuplicatePairPrior:
    return "DUPLICATE_PAIR_PRIOR";
  case TurnSafePilotShadowStatus::kDuplicateGroup: return "DUPLICATE_GROUP";
  case TurnSafePilotShadowStatus::kDuplicateMember:
    return "DUPLICATE_MEMBER";
  case TurnSafePilotShadowStatus::kMissingCamera: return "MISSING_CAMERA";
  case TurnSafePilotShadowStatus::kMissingPairPrior:
    return "MISSING_PAIR_PRIOR";
  case TurnSafePilotShadowStatus::kSelectorFailure: return "SELECTOR_FAILURE";
  case TurnSafePilotShadowStatus::kWinnerFactorLookupFailure:
    return "WINNER_FACTOR_LOOKUP_FAILURE";
  }
  return "INVALID_INPUT";
}

TurnSafePilotCallbackResult
TurnSafePilotShadow::Evaluate(const TurnSafePilotCallbackInput &input) noexcept {
  TurnSafePilotCallbackResult output;
  output.callback_index = input.callback_index;
  output.callback_timestamp = input.callback_timestamp;
  output.counters.raw_groups = input.raw_group_count;
  output.counters.typed_groups = input.groups.size();
  try {
    if (!std::isfinite(input.callback_timestamp) ||
        input.raw_group_count < input.groups.size()) {
      return output;
    }
    if (!input.groups.empty() && input.cameras.size() != 2U) {
      output.status = TurnSafePilotShadowStatus::kUnsupportedCameraCount;
      return output;
    }

    std::map<std::size_t, TurnSafeCertificateCamera> cameras;
    for (const auto &camera : input.cameras) {
      if (!cameras.emplace(camera.camera_id, camera).second) {
        output.status = TurnSafePilotShadowStatus::kDuplicateCamera;
        return output;
      }
    }
    std::set<TurnSafePairGroupKey, decltype(&turnsafe_pair_group_key_less)>
        pair_keys(&turnsafe_pair_group_key_less);
    for (const auto &pair : input.pair_priors) {
      if (!pair_keys.insert(pair.key).second) {
        output.status = TurnSafePilotShadowStatus::kDuplicatePairPrior;
        return output;
      }
    }
    std::vector<const TurnSafePilotGroupInput *> ordered_groups;
    ordered_groups.reserve(input.groups.size());
    for (const auto &group : input.groups) ordered_groups.push_back(&group);
    std::sort(ordered_groups.begin(), ordered_groups.end(),
              [](const TurnSafePilotGroupInput *left,
                 const TurnSafePilotGroupInput *right) {
                return turnsafe_pair_group_key_less(left->key, right->key);
              });
    for (std::size_t index = 1U; index < ordered_groups.size(); ++index) {
      if (turnsafe_pair_group_key_equal(ordered_groups[index - 1U]->key,
                                        ordered_groups[index]->key)) {
        output.status = TurnSafePilotShadowStatus::kDuplicateGroup;
        return output;
      }
    }
    std::vector<TurnSafePairGroupInput> selector_inputs;
    selector_inputs.reserve(ordered_groups.size());
    output.evaluations.reserve(ordered_groups.size());

    for (const TurnSafePilotGroupInput *group_pointer : ordered_groups) {
      const TurnSafePilotGroupInput &group = *group_pointer;
      TurnSafePilotGroupEvaluation evaluation;
      evaluation.key = group.key;
      evaluation.raw_member_count = group.members.size();
      output.counters.raw_members += group.members.size();
      evaluation.selector_input.key = group.key;

      const auto *camera = find_camera(cameras, group.key.camera_id);
      if (camera == nullptr) {
        output.status = TurnSafePilotShadowStatus::kMissingCamera;
        return output;
      }
      const auto *pair = find_pair(input.pair_priors, group.key);
      if (pair == nullptr) {
        output.status = TurnSafePilotShadowStatus::kMissingPairPrior;
        return output;
      }
      if (!finite_rotation(group.source_current_R_GtoI) ||
          !finite_rotation(group.target_current_R_GtoI) ||
          !finite_rotation(group.source_fej_R_GtoI) ||
          !finite_rotation(group.target_fej_R_GtoI) ||
          !std::isfinite(group.source_timestamp) ||
          !std::isfinite(group.target_timestamp)) {
        output.status = TurnSafePilotShadowStatus::kInvalidInput;
        return output;
      }

      std::vector<const TurnSafePilotMemberInput *> ordered_members;
      ordered_members.reserve(group.members.size());
      for (const auto &member : group.members)
        ordered_members.push_back(&member);
      std::sort(ordered_members.begin(), ordered_members.end(),
                [](const TurnSafePilotMemberInput *left,
                   const TurnSafePilotMemberInput *right) {
                  return turnsafe_pair_member_key_less(left->key, right->key);
                });
      for (std::size_t index = 1U; index < ordered_members.size(); ++index) {
        if (turnsafe_pair_member_key_equal(ordered_members[index - 1U]->key,
                                           ordered_members[index]->key)) {
          output.status = TurnSafePilotShadowStatus::kDuplicateMember;
          return output;
        }
      }

      TurnSafeCertificateClonePose source_pose;
      source_pose.R_GtoI = group.source_current_R_GtoI;
      source_pose.p_IinG = pair->source_position_G;
      TurnSafeCertificateClonePose target_pose;
      target_pose.R_GtoI = group.target_current_R_GtoI;
      target_pose.p_IinG = pair->target_position_G;
      const TurnSafeTranslationCertificateResult translation =
          TurnSafeCertificate::RelativeCameraTranslation(
              *camera, source_pose, target_pose, pair->covariance);

      const Eigen::Matrix3d current_R_ab =
          camera->R_ItoC * group.target_current_R_GtoI *
          group.source_current_R_GtoI.transpose() * camera->R_ItoC.transpose();
      const double angle = relative_rotation_angle(current_R_ab);
      if (!std::isfinite(angle)) {
        output.status = TurnSafePilotShadowStatus::kInvalidInput;
        return output;
      }
      evaluation.selector_input.current_predicted_relative_camera_rotation_angle_rad =
          angle;
      evaluation.candidates.reserve(group.members.size());

      for (const TurnSafePilotMemberInput *member_pointer : ordered_members) {
        const TurnSafePilotMemberInput &member = *member_pointer;
        TurnSafePilotCandidateResult candidate;
        candidate.key = member.key;
        candidate.translation = translation;
        const auto *stereo_camera =
            find_camera(cameras, member.stereo_camera_id);
        const char *identity_failure =
            member_identity_failure(group, member);
        if (identity_failure != nullptr) {
          candidate.terminal_reason = identity_failure;
        } else if (stereo_camera == nullptr) {
          candidate.terminal_reason = "TARGET_STEREO_CAMERA_MISSING";
        } else {
          candidate.source_bearing =
              TurnSafeCertificate::BearingFromCapturedNormalized(
                  *camera, member.source_observation.raw_pixel,
                  member.source_observation.captured_normalized);
          candidate.target_bearing =
              TurnSafeCertificate::BearingFromCapturedNormalized(
                  *camera, member.target_observation.raw_pixel,
                  member.target_observation.captured_normalized);
          const TurnSafeBearingResult stereo_bearing =
              TurnSafeCertificate::BearingFromCapturedNormalized(
                  *stereo_camera,
                  member.target_stereo_observation.raw_pixel,
                  member.target_stereo_observation.captured_normalized);
          candidate.stereo_valid = candidate.source_bearing.available() &&
                                   candidate.target_bearing.available() &&
                                   stereo_bearing.available();
          if (!candidate.source_bearing.available()) {
            candidate.terminal_reason = std::string("SOURCE_BEARING_") +
                turnsafe_certificate_status_name(
                    candidate.source_bearing.status);
          } else if (!candidate.target_bearing.available()) {
            candidate.terminal_reason = std::string("TARGET_BEARING_") +
                turnsafe_certificate_status_name(
                    candidate.target_bearing.status);
          } else if (!stereo_bearing.available()) {
            candidate.terminal_reason = std::string("STEREO_BEARING_") +
                turnsafe_certificate_status_name(stereo_bearing.status);
          } else {
            ++evaluation.stereo_valid_members;
            ++output.counters.stereo_valid_members;
            candidate.stereo_range =
                TurnSafeCertificate::TargetTimeStereoRange(
                    *camera, member.target_observation, *stereo_camera,
                    member.target_stereo_observation);
            if (!candidate.stereo_range.available()) {
              candidate.terminal_reason = std::string("RANGE_") +
                  turnsafe_certificate_status_name(
                      candidate.stereo_range.status);
            } else {
              candidate.range_lcb_valid = true;
              ++evaluation.range_lcb_valid_members;
              ++output.counters.range_lcb_valid_members;
              if (!translation.available()) {
                candidate.terminal_reason = std::string("TRANSLATION_") +
                    turnsafe_certificate_status_name(translation.status);
              } else {
                candidate.translation_ucb_valid = true;
                ++evaluation.translation_ucb_valid_members;
                ++output.counters.translation_ucb_valid_members;
                TurnSafeBearingFactorInput factor_input;
                factor_input.source_bearing =
                    candidate.source_bearing.bearing;
                factor_input.target_bearing =
                    candidate.target_bearing.bearing;
                factor_input.source_bearing_raw_pixel_jacobian =
                    candidate.source_bearing.bearing_wrt_pixel;
                factor_input.target_bearing_raw_pixel_jacobian =
                    candidate.target_bearing.bearing_wrt_pixel;
                factor_input.current_R_GtoI_source =
                    group.source_current_R_GtoI;
                factor_input.current_R_GtoI_target =
                    group.target_current_R_GtoI;
                factor_input.fej_R_GtoI_source = group.source_fej_R_GtoI;
                factor_input.fej_R_GtoI_target = group.target_fej_R_GtoI;
                factor_input.fixed_R_ItoC = camera->R_ItoC;
                factor_input.sigma_px =
                    TurnSafeCertificate::kPixelNoiseSigma;
                candidate.factor =
                    TurnSafeBearingFactor::Evaluate(factor_input);
                if (!candidate.factor.accepted()) {
                  candidate.terminal_reason = std::string("FACTOR_") +
                      turnsafe_bearing_factor_status_name(
                          candidate.factor.status);
                } else {
                  candidate.certificate = TurnSafeCertificate::AcuteAndRho(
                      translation.t_ucb, candidate.stereo_range.d_lcb,
                      candidate.factor.Sigma_R);
                  candidate.acute = candidate.certificate.acute;
                  if (candidate.acute) {
                    ++evaluation.acute_members;
                    ++output.counters.acute_members;
                  }
                  if (!candidate.certificate.admitted()) {
                    candidate.terminal_reason = std::string("CERTIFICATE_") +
                        turnsafe_certificate_status_name(
                            candidate.certificate.status);
                  } else {
                    candidate.rho_passed = true;
                    candidate.terminal_reason = "NONE";
                    ++evaluation.rho_passed_members;
                    ++output.counters.rho_passed_members;
                    TurnSafePairMemberInput selector_member;
                    selector_member.key = member.key;
                    selector_member.predicted_source_bearing =
                        candidate.factor.current_R_ab *
                        candidate.source_bearing.bearing;
                    selector_member.target_bearing =
                        candidate.target_bearing.bearing;
                    selector_member.target_raw_pixel =
                        member.target_observation.raw_pixel;
                    selector_member.image_width = camera->width;
                    selector_member.image_height = camera->height;
                    selector_member.sigma_R = candidate.factor.Sigma_R;
                    selector_member.whitened_local_rotation_block =
                        candidate.factor.H_relative_whitened;
                    selector_member.rho_trans =
                        candidate.certificate.rho_trans;
                    evaluation.selector_input.members.push_back(
                        std::move(selector_member));
                  }
                }
              }
            }
          }
        }
        if (candidate.terminal_reason != "NONE")
          reject(output.counters, candidate.terminal_reason);
        evaluation.candidates.push_back(std::move(candidate));
      }

      if (evaluation.stereo_valid_members >= kProductionMinimum)
        ++output.counters.stereo_valid_groups;
      if (evaluation.range_lcb_valid_members >= kProductionMinimum)
        ++output.counters.range_lcb_valid_groups;
      if (evaluation.translation_ucb_valid_members >= kProductionMinimum)
        ++output.counters.translation_ucb_valid_groups;
      if (evaluation.acute_members >= kProductionMinimum)
        ++output.counters.acute_groups;
      if (evaluation.rho_passed_members >= kProductionMinimum) {
        ++output.counters.rho_groups;
        ++output.counters.groups_n_ge_4;
      }
      selector_inputs.push_back(evaluation.selector_input);
      output.evaluations.push_back(std::move(evaluation));
    }

    output.selection = TurnSafePairSelector::Select(selector_inputs);
    if (output.selection.status ==
        TurnSafePairSelectionStatus::kDuplicateGroupKey) {
      output.status = TurnSafePilotShadowStatus::kSelectorFailure;
      return output;
    }
    for (const auto &group : output.selection.groups) {
      if (consensus_passed(group)) ++output.counters.consensus_groups;
      if (spatial_passed(group)) ++output.counters.spatial_groups;
      if (rank_passed(group)) ++output.counters.rank_groups;
      if (conditioning_passed(group)) ++output.counters.conditioning_groups;
      if (group.selection_role == TurnSafePairSelectionRole::kForegoneEligible) {
        ++output.counters.foregone_eligible_groups;
        output.counters.foregone_eligible_features +=
            group.score.retained_feature_count;
      }
      if (group.status != TurnSafePairGroupStatus::kEligible)
        reject(output.counters, turnsafe_pair_terminal_reason_name(
                                    group.terminal_reason));
    }

    if (output.selection.winner_available) {
      output.counters.winner_count = 1U;
      const TurnSafePairGroupResult &winner =
          output.selection.groups.at(output.selection.winner_index);
      const TurnSafePilotGroupEvaluation *evaluation = nullptr;
      const TurnSafePilotPairPrior *pair = nullptr;
      for (const auto &candidate : output.evaluations) {
        if (turnsafe_pair_group_key_equal(candidate.key, winner.key)) {
          evaluation = &candidate;
          break;
        }
      }
      pair = find_pair(input.pair_priors, winner.key);
      if (evaluation == nullptr || pair == nullptr) {
        output.status =
            TurnSafePilotShadowStatus::kWinnerFactorLookupFailure;
        return output;
      }
      std::vector<TurnSafeBearingFactorResult> retained_factors;
      for (const auto &candidate : evaluation->candidates) {
        if (candidate.factor.accepted() &&
            key_in(winner.consensus.retained_member_keys, candidate.key)) {
          retained_factors.push_back(candidate.factor);
        }
      }
      if (retained_factors.size() !=
          winner.consensus.retained_member_keys.size()) {
        output.status =
            TurnSafePilotShadowStatus::kWinnerFactorLookupFailure;
        return output;
      }
      TurnSafePairPoseCovariance pair_covariance;
      pair_covariance << pair->covariance.P_ss, pair->covariance.P_st,
                         pair->covariance.P_ts, pair->covariance.P_tt;
      output.winner_factor = TurnSafeBearingFactor::EvaluateGroup(
          retained_factors, pair_covariance);
      const bool nis_evaluated =
          output.winner_factor.degrees_of_freedom > 0 &&
          std::isfinite(output.winner_factor.nis) &&
          output.winner_factor.nis >= 0.0 &&
          std::isfinite(output.winner_factor.nis_threshold) &&
          output.winner_factor.nis_threshold > 0.0;
      if (nis_evaluated) ++output.counters.winner_nis_evaluated_count;
      if (nis_evaluated && output.winner_factor.nis_passed)
        ++output.counters.winner_nis_pass_count;
      if (output.winner_factor.accepted() &&
          output.winner_factor.information_non_negligible) {
        ++output.counters.winner_information_non_negligible_count;
      }
      const bool nis_passed = output.winner_factor.accepted() &&
                              output.winner_factor.nis_passed;
      output.post_nis = TurnSafePairSelector::ApplyFrozenWinnerNis(
          output.selection, nis_passed);
      if (!output.winner_factor.accepted())
        reject(output.counters, std::string("WINNER_FACTOR_") +
            turnsafe_bearing_group_status_name(output.winner_factor.status));
      else if (!output.winner_factor.nis_passed)
        reject(output.counters, "WINNER_NIS_REJECTED");
    } else {
      output.post_nis = TurnSafePairSelector::ApplyFrozenWinnerNis(
          output.selection, false);
    }
    output.status = TurnSafePilotShadowStatus::kAccepted;
    return output;
  } catch (const std::bad_alloc &) {
    output.status = TurnSafePilotShadowStatus::kInvalidInput;
    return output;
  } catch (...) {
    output.status = TurnSafePilotShadowStatus::kInvalidInput;
    return output;
  }
}

} // namespace ov_msckf
