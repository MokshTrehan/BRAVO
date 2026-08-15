/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pilot deterministic pair-group selector.
 */

#ifndef OV_MSCKF_TURNSAFE_PAIR_SELECTOR_H
#define OV_MSCKF_TURNSAFE_PAIR_SELECTOR_H

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace ov_msckf {

/** Complete canonical candidate identity used for deterministic accumulation. */
struct TurnSafePairMemberKey {
  std::size_t camera_id = 0U;
  std::string source_timestamp_key;
  std::string target_timestamp_key;
  std::uint64_t feature_id = 0U;
  std::uint64_t detached_index = 0U;
  std::size_t source_observation_ordinal = 0U;
  std::size_t target_observation_ordinal = 0U;
};

/** Canonical identity shared by every member of one source/target pair group. */
struct TurnSafePairGroupKey {
  std::size_t camera_id = 0U;
  std::string source_timestamp_key;
  std::string target_timestamp_key;
};

bool turnsafe_pair_member_key_less(const TurnSafePairMemberKey &left,
                                   const TurnSafePairMemberKey &right) noexcept;
bool turnsafe_pair_member_key_equal(const TurnSafePairMemberKey &left,
                                    const TurnSafePairMemberKey &right) noexcept;
bool turnsafe_pair_group_key_less(const TurnSafePairGroupKey &left,
                                  const TurnSafePairGroupKey &right) noexcept;
bool turnsafe_pair_group_key_equal(const TurnSafePairGroupKey &left,
                                   const TurnSafePairGroupKey &right) noexcept;

/** Certificate-complete, value-only input for one consensus member. */
struct TurnSafePairMemberInput {
  TurnSafePairMemberKey key;
  /** Current p_i=R_ab*b_ai, expressed in the target camera frame. */
  Eigen::Vector3d predicted_source_bearing = Eigen::Vector3d::Zero();
  /** Frozen measured target bearing b_bi. */
  Eigen::Vector3d target_bearing = Eigen::Vector3d::Zero();
  Eigen::Vector2d target_raw_pixel = Eigen::Vector2d::Zero();
  int image_width = 0;
  int image_height = 0;
  Eigen::Matrix2d sigma_R = Eigen::Matrix2d::Zero();
  Eigen::Matrix<double, 2, 3> whitened_local_rotation_block =
      Eigen::Matrix<double, 2, 3>::Zero();
  double rho_trans = std::numeric_limits<double>::quiet_NaN();
};

struct TurnSafePairGroupInput {
  TurnSafePairGroupKey key;
  double current_predicted_relative_camera_rotation_angle_rad =
      std::numeric_limits<double>::quiet_NaN();
  std::vector<TurnSafePairMemberInput> members;
};

enum class TurnSafePairGroupStatus : std::uint8_t {
  kInsufficientGroup,
  kShadowOnlySmallGroup,
  kRejected,
  kEligible,
};

enum class TurnSafePairTerminalReason : std::uint8_t {
  kNone,
  kInsufficientGroup,
  kShadowOnlySmallGroup,
  kInvalidGroupScore,
  kMemberGroupKeyMismatch,
  kDuplicateMemberKey,
  kMemberPrimitiveInvalid,
  kMemberRhoIneligible,
  kConsensusSvdFailure,
  kConsensusWhiteningFailure,
  kConsensusInsufficientInliers,
  kConsensusRefitSvdFailure,
  kConsensusRefitThresholdFailure,
  kSpatialInvalid,
  kSpatialHullArea,
  kSpatialPixelCovariance,
  kSpatialOccupiedCells,
  kSpatialSpan,
  kRotationStackSvdFailure,
  kRotationStackRank,
  kRotationStackConditioning,
  kWhitenedLocalStackSvdFailure,
  kPredictedInformationNonfinite,
};

enum class TurnSafePairSelectionRole : std::uint8_t {
  kIneligible,
  kWinner,
  kForegoneEligible,
};

const char *turnsafe_pair_group_status_name(
    TurnSafePairGroupStatus status) noexcept;
const char *turnsafe_pair_terminal_reason_name(
    TurnSafePairTerminalReason reason) noexcept;
const char *turnsafe_pair_selection_role_name(
    TurnSafePairSelectionRole role) noexcept;

struct TurnSafePairMemberConsensusDiagnostic {
  TurnSafePairMemberKey key;
  bool retained = false;
  double initial_whitened_norm =
      std::numeric_limits<double>::quiet_NaN();
  bool refit_norm_available = false;
  double refit_whitened_norm =
      std::numeric_limits<double>::quiet_NaN();
};

struct TurnSafePairConsensusDiagnostics {
  bool attempted = false;
  bool first_fit_available = false;
  Eigen::Matrix3d first_fit_rotation = Eigen::Matrix3d::Identity();
  double first_fit_rotation_angle_rad =
      std::numeric_limits<double>::quiet_NaN();
  double absolute_threshold = 3.2848542587702925;
  double median = std::numeric_limits<double>::quiet_NaN();
  double mad = std::numeric_limits<double>::quiet_NaN();
  double robust_threshold = std::numeric_limits<double>::quiet_NaN();
  std::size_t required_retained_count = 0U;
  std::vector<TurnSafePairMemberConsensusDiagnostic> members;
  std::vector<TurnSafePairMemberKey> retained_member_keys;
  bool refit_available = false;
  Eigen::Matrix3d refit_rotation = Eigen::Matrix3d::Identity();
  double refit_rotation_angle_rad =
      std::numeric_limits<double>::quiet_NaN();
};

struct TurnSafePairSpatialDiagnostics {
  bool attempted = false;
  double convex_hull_area = std::numeric_limits<double>::quiet_NaN();
  double minimum_population_covariance_eigenvalue =
      std::numeric_limits<double>::quiet_NaN();
  std::size_t occupied_fixed_4x4_cells = 0U;
  double x_span = std::numeric_limits<double>::quiet_NaN();
  double y_span = std::numeric_limits<double>::quiet_NaN();
};

struct TurnSafePairScore {
  double predicted_rotation_angle_rad =
      std::numeric_limits<double>::quiet_NaN();
  double minimum_whitened_local_singular_value =
      std::numeric_limits<double>::quiet_NaN();
  double maximum_retained_rho_trans =
      std::numeric_limits<double>::quiet_NaN();
  std::size_t retained_feature_count = 0U;
  TurnSafePairGroupKey group_key;
};

struct TurnSafePairGroupResult {
  TurnSafePairGroupKey key;
  TurnSafePairGroupStatus status = TurnSafePairGroupStatus::kRejected;
  TurnSafePairTerminalReason terminal_reason =
      TurnSafePairTerminalReason::kMemberPrimitiveInvalid;
  TurnSafePairSelectionRole selection_role =
      TurnSafePairSelectionRole::kIneligible;
  std::size_t raw_member_count = 0U;
  TurnSafePairConsensusDiagnostics consensus;
  TurnSafePairSpatialDiagnostics spatial;
  Eigen::Vector3d rotation_stack_singular_values = Eigen::Vector3d::Zero();
  double rotation_stack_rank_tolerance =
      std::numeric_limits<double>::quiet_NaN();
  std::size_t rotation_stack_rank = 0U;
  double rotation_stack_condition_ratio =
      std::numeric_limits<double>::quiet_NaN();
  Eigen::Vector3d whitened_local_stack_singular_values =
      Eigen::Vector3d::Zero();
  /** Ascending eigenvalues of H_rel_whitened^T H_rel_whitened. */
  Eigen::Vector3d predicted_orientation_information_eigenvalues =
      Eigen::Vector3d::Constant(
          std::numeric_limits<double>::quiet_NaN());
  double predicted_orientation_information_trace =
      std::numeric_limits<double>::quiet_NaN();
  double predicted_orientation_information_determinant =
      std::numeric_limits<double>::quiet_NaN();
  bool predicted_orientation_information_non_negligible = false;
  TurnSafePairScore score;
};

enum class TurnSafePairSelectionStatus : std::uint8_t {
  kNoEligibleGroup,
  kWinnerFrozen,
  kDuplicateGroupKey,
};

const char *turnsafe_pair_selection_status_name(
    TurnSafePairSelectionStatus status) noexcept;

struct TurnSafePairSelectionResult {
  TurnSafePairSelectionStatus status =
      TurnSafePairSelectionStatus::kNoEligibleGroup;
  std::vector<TurnSafePairGroupResult> groups;
  bool winner_available = false;
  std::size_t winner_index = 0U;
  TurnSafePairGroupKey winner_key;
  bool winner_frozen_before_nis = true;
  bool no_runner_up_gate_shopping = true;
};

/** Post-NIS reporting value; a failed frozen winner can only coast. */
struct TurnSafePairPostNisDecision {
  bool frozen_winner_available = false;
  TurnSafePairGroupKey frozen_winner_key;
  bool winner_nis_passed = false;
  bool accepted_winner_available = false;
  bool runner_up_promoted = false;
};

class TurnSafePairSelector {
public:
  static TurnSafePairGroupResult
  EvaluateGroup(const TurnSafePairGroupInput &input);

  static TurnSafePairSelectionResult
  Select(const std::vector<TurnSafePairGroupInput> &inputs);

  static TurnSafePairPostNisDecision
  ApplyFrozenWinnerNis(const TurnSafePairSelectionResult &selection,
                       bool winner_nis_passed) noexcept;
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_PAIR_SELECTOR_H
