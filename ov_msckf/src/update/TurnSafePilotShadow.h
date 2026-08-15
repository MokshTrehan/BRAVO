/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pilot offline-only shadow orchestration.
 */

#ifndef OV_MSCKF_TURNSAFE_PILOT_SHADOW_H
#define OV_MSCKF_TURNSAFE_PILOT_SHADOW_H

#include "TurnSafeBearingFactor.h"
#include "TurnSafeCertificate.h"
#include "TurnSafePairSelector.h"

#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace ov_msckf {

struct TurnSafePilotPairPrior {
  TurnSafePairGroupKey key;
  Eigen::Vector3d source_position_G = Eigen::Vector3d::Zero();
  Eigen::Vector3d target_position_G = Eigen::Vector3d::Zero();
  TurnSafeCapturedPairCovariance covariance;
};

struct TurnSafePilotMemberInput {
  TurnSafePairMemberKey key;
  std::size_t stereo_camera_id = 0U;
  TurnSafeCertificateObservation source_observation;
  TurnSafeCertificateObservation target_observation;
  TurnSafeCertificateObservation target_stereo_observation;
};

struct TurnSafePilotGroupInput {
  TurnSafePairGroupKey key;
  double source_timestamp = 0.0;
  double target_timestamp = 0.0;
  Eigen::Matrix3d source_current_R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d target_current_R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d source_fej_R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d target_fej_R_GtoI = Eigen::Matrix3d::Identity();
  std::vector<TurnSafePilotMemberInput> members;
};

struct TurnSafePilotCallbackInput {
  std::uint64_t callback_index = 0U;
  double callback_timestamp = 0.0;
  std::size_t raw_group_count = 0U;
  std::vector<TurnSafeCertificateCamera> cameras;
  std::vector<TurnSafePilotPairPrior> pair_priors;
  std::vector<TurnSafePilotGroupInput> groups;
};

struct TurnSafePilotCandidateResult {
  TurnSafePairMemberKey key;
  bool stereo_valid = false;
  bool range_lcb_valid = false;
  bool translation_ucb_valid = false;
  bool acute = false;
  bool rho_passed = false;
  std::string terminal_reason = "NOT_EVALUATED";
  TurnSafeBearingResult source_bearing;
  TurnSafeBearingResult target_bearing;
  TurnSafeStereoRangeResult stereo_range;
  TurnSafeTranslationCertificateResult translation;
  TurnSafeBearingFactorResult factor;
  TurnSafeAcuteCertificateResult certificate;
};

struct TurnSafePilotGroupEvaluation {
  TurnSafePairGroupKey key;
  std::size_t raw_member_count = 0U;
  std::size_t stereo_valid_members = 0U;
  std::size_t range_lcb_valid_members = 0U;
  std::size_t translation_ucb_valid_members = 0U;
  std::size_t acute_members = 0U;
  std::size_t rho_passed_members = 0U;
  std::vector<TurnSafePilotCandidateResult> candidates;
  TurnSafePairGroupInput selector_input;
};

struct TurnSafePilotCallbackCounters {
  std::size_t raw_groups = 0U;
  std::size_t typed_groups = 0U;
  std::size_t raw_members = 0U;
  std::size_t stereo_valid_members = 0U;
  std::size_t range_lcb_valid_members = 0U;
  std::size_t translation_ucb_valid_members = 0U;
  std::size_t acute_members = 0U;
  std::size_t rho_passed_members = 0U;
  std::size_t stereo_valid_groups = 0U;
  std::size_t range_lcb_valid_groups = 0U;
  std::size_t translation_ucb_valid_groups = 0U;
  std::size_t acute_groups = 0U;
  std::size_t rho_groups = 0U;
  std::size_t groups_n_ge_4 = 0U;
  std::size_t consensus_groups = 0U;
  std::size_t spatial_groups = 0U;
  std::size_t rank_groups = 0U;
  std::size_t conditioning_groups = 0U;
  std::size_t winner_count = 0U;
  std::size_t winner_nis_evaluated_count = 0U;
  std::size_t winner_nis_pass_count = 0U;
  std::size_t winner_information_non_negligible_count = 0U;
  std::size_t foregone_eligible_groups = 0U;
  std::size_t foregone_eligible_features = 0U;
  std::map<std::string, std::uint64_t> rejection_reasons;
};

enum class TurnSafePilotShadowStatus : std::uint8_t {
  kAccepted,
  kInvalidInput,
  kUnsupportedCameraCount,
  kDuplicateCamera,
  kDuplicatePairPrior,
  kDuplicateGroup,
  kDuplicateMember,
  kMissingCamera,
  kMissingPairPrior,
  kSelectorFailure,
  kWinnerFactorLookupFailure,
};

const char *turnsafe_pilot_shadow_status_name(
    TurnSafePilotShadowStatus status) noexcept;

struct TurnSafePilotCallbackResult {
  TurnSafePilotShadowStatus status = TurnSafePilotShadowStatus::kInvalidInput;
  std::uint64_t callback_index = 0U;
  double callback_timestamp = 0.0;
  TurnSafePilotCallbackCounters counters;
  std::vector<TurnSafePilotGroupEvaluation> evaluations;
  TurnSafePairSelectionResult selection;
  TurnSafeBearingGroupResult winner_factor;
  TurnSafePairPostNisDecision post_nis;

  bool accepted() const noexcept {
    return status == TurnSafePilotShadowStatus::kAccepted;
  }
};

/**
 * Pure, offline-only callback evaluator. This class has no State, Type,
 * Feature, updater, proposal, commit, or finalization dependency.
 */
class TurnSafePilotShadow final {
public:
  static TurnSafePilotCallbackResult
  Evaluate(const TurnSafePilotCallbackInput &input) noexcept;

  TurnSafePilotShadow() = delete;
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_PILOT_SHADOW_H
