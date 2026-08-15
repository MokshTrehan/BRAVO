/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pilot deterministic pair-group selector.
 */

#include "TurnSafePairSelector.h"

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>
#include <Eigen/SVD>

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <numeric>
#include <utility>

namespace ov_msckf {
namespace {

constexpr double kAbsoluteThreshold = 3.2848542587702925;
constexpr double kMadMultiplier = 4.0;
constexpr double kMadNormalScale = 1.482602218505602;
constexpr double kMinimumHullArea = 0.1;
constexpr double kMinimumPixelCovarianceEigenvalue = 0.03;
constexpr std::size_t kMinimumOccupiedCells = 4U;
constexpr double kMinimumSpan = 0.6;
constexpr double kMinimumConditionRatio = 0.15;
constexpr double kMaximumRho = 0.5;
constexpr double kInformationTraceFloor = 1.0e-12;

bool bytewise_less(const std::string &left, const std::string &right) noexcept {
  const std::size_t common = std::min(left.size(), right.size());
  for (std::size_t index = 0U; index < common; ++index) {
    const unsigned char left_byte =
        static_cast<unsigned char>(left[index]);
    const unsigned char right_byte =
        static_cast<unsigned char>(right[index]);
    if (left_byte != right_byte) return left_byte < right_byte;
  }
  return left.size() < right.size();
}

bool group_key_matches_member(const TurnSafePairGroupKey &group,
                              const TurnSafePairMemberKey &member) noexcept {
  return group.camera_id == member.camera_id &&
         group.source_timestamp_key == member.source_timestamp_key &&
         group.target_timestamp_key == member.target_timestamp_key;
}

Eigen::Matrix3d skew(const Eigen::Vector3d &value) {
  Eigen::Matrix3d output;
  output << 0.0, -value(2), value(1), value(2), 0.0, -value(0),
      -value(1), value(0), 0.0;
  return output;
}

bool tangent_basis(const Eigen::Vector3d &target,
                   Eigen::Matrix<double, 3, 2> &basis) {
  if (!target.allFinite()) return false;
  const double squared_norm = target.squaredNorm();
  if (!(squared_norm > 0.0) || !std::isfinite(squared_norm)) return false;

  std::size_t axis_index = 0U;
  double smallest_dot = std::fabs(target(0));
  for (std::size_t candidate = 1U; candidate < 3U; ++candidate) {
    const double candidate_dot =
        std::fabs(target(static_cast<Eigen::Index>(candidate)));
    if (candidate_dot < smallest_dot) {
      smallest_dot = candidate_dot;
      axis_index = candidate;
    }
  }
  Eigen::Vector3d axis = Eigen::Vector3d::Zero();
  axis(static_cast<Eigen::Index>(axis_index)) = 1.0;
  Eigen::Vector3d first =
      axis - target * (target.dot(axis) / squared_norm);
  const double first_norm = first.norm();
  if (!(first_norm > 0.0) || !std::isfinite(first_norm)) return false;
  first /= first_norm;
  const Eigen::Vector3d second = target.cross(first);
  if (!first.allFinite() || !second.allFinite() ||
      !(second.squaredNorm() > 0.0)) {
    return false;
  }
  basis.col(0) = first;
  basis.col(1) = second;
  return true;
}

bool checked_whitened_norm(const TurnSafePairMemberInput &member,
                           const Eigen::Matrix3d &rotation,
                           double &whitened_norm) {
  if (!member.sigma_R.allFinite() ||
      member.sigma_R(0, 1) != member.sigma_R(1, 0)) {
    return false;
  }
  Eigen::LLT<Eigen::Matrix2d> factor(member.sigma_R);
  if (factor.info() != Eigen::Success ||
      !(factor.matrixL()(0, 0) > 0.0) ||
      !(factor.matrixL()(1, 1) > 0.0) ||
      !std::isfinite(factor.matrixL()(0, 0)) ||
      !std::isfinite(factor.matrixL()(1, 1))) {
    return false;
  }
  Eigen::Matrix<double, 3, 2> basis;
  if (!tangent_basis(member.target_bearing, basis)) return false;
  const Eigen::Vector2d residual = basis.transpose() *
      (member.target_bearing -
       rotation * member.predicted_source_bearing);
  if (!residual.allFinite()) return false;
  const Eigen::Vector2d whitened = factor.matrixL().solve(residual);
  whitened_norm = whitened.norm();
  return whitened.allFinite() && std::isfinite(whitened_norm);
}

double rotation_principal_angle(const Eigen::Matrix3d &rotation) {
  const Eigen::Vector3d skew_vee(
      rotation(2, 1) - rotation(1, 2),
      rotation(0, 2) - rotation(2, 0),
      rotation(1, 0) - rotation(0, 1));
  const double sine = 0.5 * skew_vee.norm();
  const double cosine = 0.5 * (rotation.trace() - 1.0);
  return std::atan2(sine, cosine);
}

bool fit_proper_wahba(const std::vector<TurnSafePairMemberInput> &members,
                      const std::vector<std::size_t> &indices,
                      Eigen::Matrix3d &rotation,
                      double &rotation_angle) {
  if (indices.empty()) return false;
  Eigen::Matrix3d cross_covariance = Eigen::Matrix3d::Zero();
  for (const std::size_t index : indices) {
    if (index >= members.size()) return false;
    const auto &member = members[index];
    if (!member.predicted_source_bearing.allFinite() ||
        !member.target_bearing.allFinite()) {
      return false;
    }
    cross_covariance.noalias() +=
        member.target_bearing * member.predicted_source_bearing.transpose();
  }
  if (!cross_covariance.allFinite()) return false;

  Eigen::JacobiSVD<Eigen::Matrix3d> decomposition(
      cross_covariance, Eigen::ComputeFullU | Eigen::ComputeFullV);
  if (!decomposition.singularValues().allFinite() ||
      !decomposition.matrixU().allFinite() ||
      !decomposition.matrixV().allFinite()) {
    return false;
  }
  const Eigen::Matrix3d uv =
      decomposition.matrixU() * decomposition.matrixV().transpose();
  const double determinant = uv.determinant();
  if (!std::isfinite(determinant) || determinant == 0.0) return false;
  Eigen::Matrix3d proper = Eigen::Matrix3d::Identity();
  proper(2, 2) = determinant < 0.0 ? -1.0 : 1.0;
  rotation = decomposition.matrixU() * proper *
             decomposition.matrixV().transpose();
  if (!rotation.allFinite() || !(rotation.determinant() > 0.0)) return false;
  rotation_angle = rotation_principal_angle(rotation);
  return std::isfinite(rotation_angle);
}

double lower_median(std::vector<double> values) {
  std::sort(values.begin(), values.end());
  return values[(values.size() - 1U) / 2U];
}

struct SpatialPoint {
  double x = 0.0;
  double y = 0.0;
};

bool spatial_point_less(const SpatialPoint &left,
                        const SpatialPoint &right) {
  if (left.x != right.x) return left.x < right.x;
  return left.y < right.y;
}

bool spatial_point_equal(const SpatialPoint &left,
                         const SpatialPoint &right) {
  return left.x == right.x && left.y == right.y;
}

double spatial_cross(const SpatialPoint &origin, const SpatialPoint &left,
                     const SpatialPoint &right) {
  return (left.x - origin.x) * (right.y - origin.y) -
         (left.y - origin.y) * (right.x - origin.x);
}

bool convex_hull_area(std::vector<SpatialPoint> points, double &area) {
  std::sort(points.begin(), points.end(), spatial_point_less);
  points.erase(std::unique(points.begin(), points.end(), spatial_point_equal),
               points.end());
  if (points.size() < 3U) {
    area = 0.0;
    return true;
  }

  std::vector<SpatialPoint> hull;
  hull.reserve(2U * points.size());
  for (const auto &point : points) {
    while (hull.size() >= 2U &&
           spatial_cross(hull[hull.size() - 2U], hull.back(), point) <= 0.0) {
      hull.pop_back();
    }
    hull.push_back(point);
  }
  const std::size_t lower_size = hull.size();
  for (std::size_t reverse = points.size() - 1U; reverse > 0U; --reverse) {
    const auto &point = points[reverse - 1U];
    while (hull.size() > lower_size &&
           spatial_cross(hull[hull.size() - 2U], hull.back(), point) <= 0.0) {
      hull.pop_back();
    }
    hull.push_back(point);
  }
  if (!hull.empty()) hull.pop_back();
  if (hull.size() < 3U) {
    area = 0.0;
    return true;
  }
  double twice_area = 0.0;
  for (std::size_t index = 0U; index < hull.size(); ++index) {
    const auto &first = hull[index];
    const auto &second = hull[(index + 1U) % hull.size()];
    twice_area += first.x * second.y - first.y * second.x;
  }
  area = 0.5 * std::fabs(twice_area);
  return std::isfinite(area);
}

bool evaluate_spatial(
    const std::vector<TurnSafePairMemberInput> &members,
    const std::vector<std::size_t> &retained,
    TurnSafePairSpatialDiagnostics &diagnostics,
    TurnSafePairTerminalReason &failure) {
  diagnostics.attempted = true;
  if (retained.empty()) {
    failure = TurnSafePairTerminalReason::kSpatialInvalid;
    return false;
  }
  std::vector<SpatialPoint> points;
  points.reserve(retained.size());
  std::array<bool, 16U> occupied{{false}};
  double minimum_x = std::numeric_limits<double>::infinity();
  double maximum_x = -std::numeric_limits<double>::infinity();
  double minimum_y = std::numeric_limits<double>::infinity();
  double maximum_y = -std::numeric_limits<double>::infinity();
  Eigen::Vector2d mean = Eigen::Vector2d::Zero();
  for (const std::size_t index : retained) {
    if (index >= members.size()) {
      failure = TurnSafePairTerminalReason::kSpatialInvalid;
      return false;
    }
    const auto &member = members[index];
    if (member.image_width <= 0 || member.image_height <= 0 ||
        !member.target_raw_pixel.allFinite()) {
      failure = TurnSafePairTerminalReason::kSpatialInvalid;
      return false;
    }
    const double x = (member.target_raw_pixel(0) + 0.5) /
                     static_cast<double>(member.image_width);
    const double y = (member.target_raw_pixel(1) + 0.5) /
                     static_cast<double>(member.image_height);
    if (!std::isfinite(x) || !std::isfinite(y) || x < 0.0 || x > 1.0 ||
        y < 0.0 || y > 1.0) {
      failure = TurnSafePairTerminalReason::kSpatialInvalid;
      return false;
    }
    points.push_back({x, y});
    mean += Eigen::Vector2d(x, y);
    minimum_x = std::min(minimum_x, x);
    maximum_x = std::max(maximum_x, x);
    minimum_y = std::min(minimum_y, y);
    maximum_y = std::max(maximum_y, y);
    const int cell_x = x == 1.0 ? 3 : static_cast<int>(std::floor(4.0 * x));
    const int cell_y = y == 1.0 ? 3 : static_cast<int>(std::floor(4.0 * y));
    if (cell_x < 0 || cell_x > 3 || cell_y < 0 || cell_y > 3) {
      failure = TurnSafePairTerminalReason::kSpatialInvalid;
      return false;
    }
    occupied[static_cast<std::size_t>(4 * cell_y + cell_x)] = true;
  }
  mean /= static_cast<double>(retained.size());
  Eigen::Matrix2d covariance = Eigen::Matrix2d::Zero();
  for (const auto &point : points) {
    const Eigen::Vector2d centered(point.x - mean(0), point.y - mean(1));
    covariance.noalias() += centered * centered.transpose();
  }
  covariance /= static_cast<double>(retained.size());
  if (!covariance.allFinite()) {
    failure = TurnSafePairTerminalReason::kSpatialInvalid;
    return false;
  }
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix2d> covariance_solver(
      covariance, Eigen::EigenvaluesOnly);
  if (covariance_solver.info() != Eigen::Success ||
      !covariance_solver.eigenvalues().allFinite()) {
    failure = TurnSafePairTerminalReason::kSpatialInvalid;
    return false;
  }
  if (!convex_hull_area(points, diagnostics.convex_hull_area)) {
    failure = TurnSafePairTerminalReason::kSpatialInvalid;
    return false;
  }
  diagnostics.minimum_population_covariance_eigenvalue =
      covariance_solver.eigenvalues()(0);
  diagnostics.occupied_fixed_4x4_cells = static_cast<std::size_t>(
      std::count(occupied.begin(), occupied.end(), true));
  diagnostics.x_span = maximum_x - minimum_x;
  diagnostics.y_span = maximum_y - minimum_y;

  if (diagnostics.convex_hull_area < kMinimumHullArea) {
    failure = TurnSafePairTerminalReason::kSpatialHullArea;
    return false;
  }
  if (diagnostics.minimum_population_covariance_eigenvalue <
      kMinimumPixelCovarianceEigenvalue) {
    failure = TurnSafePairTerminalReason::kSpatialPixelCovariance;
    return false;
  }
  if (diagnostics.occupied_fixed_4x4_cells < kMinimumOccupiedCells) {
    failure = TurnSafePairTerminalReason::kSpatialOccupiedCells;
    return false;
  }
  if (diagnostics.x_span < kMinimumSpan ||
      diagnostics.y_span < kMinimumSpan) {
    failure = TurnSafePairTerminalReason::kSpatialSpan;
    return false;
  }
  return true;
}

bool member_primitive_valid(const TurnSafePairMemberInput &member) {
  return member.predicted_source_bearing.allFinite() &&
         member.target_bearing.allFinite() &&
         member.predicted_source_bearing.squaredNorm() > 0.0 &&
         member.target_bearing.squaredNorm() > 0.0 &&
         member.target_raw_pixel.allFinite() && member.image_width > 0 &&
         member.image_height > 0 && member.sigma_R.allFinite() &&
         member.whitened_local_rotation_block.allFinite();
}

bool score_better(const TurnSafePairScore &left,
                  const TurnSafePairScore &right) noexcept {
  if (left.predicted_rotation_angle_rad !=
      right.predicted_rotation_angle_rad) {
    return left.predicted_rotation_angle_rad >
           right.predicted_rotation_angle_rad;
  }
  if (left.minimum_whitened_local_singular_value !=
      right.minimum_whitened_local_singular_value) {
    return left.minimum_whitened_local_singular_value >
           right.minimum_whitened_local_singular_value;
  }
  if (left.maximum_retained_rho_trans !=
      right.maximum_retained_rho_trans) {
    return left.maximum_retained_rho_trans <
           right.maximum_retained_rho_trans;
  }
  if (left.retained_feature_count != right.retained_feature_count) {
    return left.retained_feature_count > right.retained_feature_count;
  }
  return turnsafe_pair_group_key_less(left.group_key, right.group_key);
}

} // namespace

bool turnsafe_pair_member_key_less(const TurnSafePairMemberKey &left,
                                   const TurnSafePairMemberKey &right) noexcept {
  if (left.camera_id != right.camera_id)
    return left.camera_id < right.camera_id;
  if (left.source_timestamp_key != right.source_timestamp_key)
    return bytewise_less(left.source_timestamp_key,
                         right.source_timestamp_key);
  if (left.target_timestamp_key != right.target_timestamp_key)
    return bytewise_less(left.target_timestamp_key,
                         right.target_timestamp_key);
  if (left.feature_id != right.feature_id)
    return left.feature_id < right.feature_id;
  if (left.detached_index != right.detached_index)
    return left.detached_index < right.detached_index;
  if (left.source_observation_ordinal != right.source_observation_ordinal) {
    return left.source_observation_ordinal <
           right.source_observation_ordinal;
  }
  return left.target_observation_ordinal < right.target_observation_ordinal;
}

bool turnsafe_pair_member_key_equal(const TurnSafePairMemberKey &left,
                                    const TurnSafePairMemberKey &right) noexcept {
  return !turnsafe_pair_member_key_less(left, right) &&
         !turnsafe_pair_member_key_less(right, left);
}

bool turnsafe_pair_group_key_less(const TurnSafePairGroupKey &left,
                                  const TurnSafePairGroupKey &right) noexcept {
  if (left.camera_id != right.camera_id)
    return left.camera_id < right.camera_id;
  if (left.source_timestamp_key != right.source_timestamp_key)
    return bytewise_less(left.source_timestamp_key,
                         right.source_timestamp_key);
  return bytewise_less(left.target_timestamp_key, right.target_timestamp_key);
}

bool turnsafe_pair_group_key_equal(const TurnSafePairGroupKey &left,
                                   const TurnSafePairGroupKey &right) noexcept {
  return !turnsafe_pair_group_key_less(left, right) &&
         !turnsafe_pair_group_key_less(right, left);
}

const char *turnsafe_pair_group_status_name(
    TurnSafePairGroupStatus status) noexcept {
  switch (status) {
  case TurnSafePairGroupStatus::kInsufficientGroup:
    return "INSUFFICIENT_GROUP";
  case TurnSafePairGroupStatus::kShadowOnlySmallGroup:
    return "SHADOW_ONLY_SMALL_GROUP";
  case TurnSafePairGroupStatus::kRejected: return "REJECTED";
  case TurnSafePairGroupStatus::kEligible: return "ELIGIBLE";
  }
  return "UNKNOWN";
}

const char *turnsafe_pair_terminal_reason_name(
    TurnSafePairTerminalReason reason) noexcept {
  switch (reason) {
  case TurnSafePairTerminalReason::kNone: return "NONE";
  case TurnSafePairTerminalReason::kInsufficientGroup:
    return "INSUFFICIENT_GROUP";
  case TurnSafePairTerminalReason::kShadowOnlySmallGroup:
    return "SHADOW_ONLY_SMALL_GROUP";
  case TurnSafePairTerminalReason::kInvalidGroupScore:
    return "INVALID_GROUP_SCORE";
  case TurnSafePairTerminalReason::kMemberGroupKeyMismatch:
    return "MEMBER_GROUP_KEY_MISMATCH";
  case TurnSafePairTerminalReason::kDuplicateMemberKey:
    return "DUPLICATE_MEMBER_KEY";
  case TurnSafePairTerminalReason::kMemberPrimitiveInvalid:
    return "MEMBER_PRIMITIVE_INVALID";
  case TurnSafePairTerminalReason::kMemberRhoIneligible:
    return "MEMBER_RHO_INELIGIBLE";
  case TurnSafePairTerminalReason::kConsensusSvdFailure:
    return "CONSENSUS_SVD_FAILURE";
  case TurnSafePairTerminalReason::kConsensusWhiteningFailure:
    return "CONSENSUS_WHITENING_FAILURE";
  case TurnSafePairTerminalReason::kConsensusInsufficientInliers:
    return "CONSENSUS_INSUFFICIENT_INLIERS";
  case TurnSafePairTerminalReason::kConsensusRefitSvdFailure:
    return "CONSENSUS_REFIT_SVD_FAILURE";
  case TurnSafePairTerminalReason::kConsensusRefitThresholdFailure:
    return "CONSENSUS_REFIT_THRESHOLD_FAILURE";
  case TurnSafePairTerminalReason::kSpatialInvalid:
    return "SPATIAL_INVALID";
  case TurnSafePairTerminalReason::kSpatialHullArea:
    return "SPATIAL_HULL_AREA";
  case TurnSafePairTerminalReason::kSpatialPixelCovariance:
    return "SPATIAL_PIXEL_COVARIANCE";
  case TurnSafePairTerminalReason::kSpatialOccupiedCells:
    return "SPATIAL_OCCUPIED_CELLS";
  case TurnSafePairTerminalReason::kSpatialSpan: return "SPATIAL_SPAN";
  case TurnSafePairTerminalReason::kRotationStackSvdFailure:
    return "ROTATION_STACK_SVD_FAILURE";
  case TurnSafePairTerminalReason::kRotationStackRank:
    return "ROTATION_STACK_RANK";
  case TurnSafePairTerminalReason::kRotationStackConditioning:
    return "ROTATION_STACK_CONDITIONING";
  case TurnSafePairTerminalReason::kWhitenedLocalStackSvdFailure:
    return "WHITENED_LOCAL_STACK_SVD_FAILURE";
  case TurnSafePairTerminalReason::kPredictedInformationNonfinite:
    return "PREDICTED_INFORMATION_NONFINITE";
  }
  return "UNKNOWN";
}

const char *turnsafe_pair_selection_role_name(
    TurnSafePairSelectionRole role) noexcept {
  switch (role) {
  case TurnSafePairSelectionRole::kIneligible: return "INELIGIBLE";
  case TurnSafePairSelectionRole::kWinner: return "WINNER";
  case TurnSafePairSelectionRole::kForegoneEligible:
    return "FOREGONE_ELIGIBLE";
  }
  return "UNKNOWN";
}

const char *turnsafe_pair_selection_status_name(
    TurnSafePairSelectionStatus status) noexcept {
  switch (status) {
  case TurnSafePairSelectionStatus::kNoEligibleGroup:
    return "NO_ELIGIBLE_GROUP";
  case TurnSafePairSelectionStatus::kWinnerFrozen: return "WINNER_FROZEN";
  case TurnSafePairSelectionStatus::kDuplicateGroupKey:
    return "DUPLICATE_GROUP_KEY";
  }
  return "UNKNOWN";
}

TurnSafePairGroupResult TurnSafePairSelector::EvaluateGroup(
    const TurnSafePairGroupInput &input) {
  TurnSafePairGroupResult result;
  result.key = input.key;
  result.raw_member_count = input.members.size();
  result.score.group_key = input.key;

  if (!std::isfinite(
          input.current_predicted_relative_camera_rotation_angle_rad) ||
      input.current_predicted_relative_camera_rotation_angle_rad < 0.0 ||
      input.current_predicted_relative_camera_rotation_angle_rad >
          std::acos(-1.0)) {
    result.terminal_reason = TurnSafePairTerminalReason::kInvalidGroupScore;
    return result;
  }

  std::vector<TurnSafePairMemberInput> members = input.members;
  std::sort(members.begin(), members.end(),
            [](const TurnSafePairMemberInput &left,
               const TurnSafePairMemberInput &right) {
              return turnsafe_pair_member_key_less(left.key, right.key);
            });
  for (std::size_t index = 0U; index < members.size(); ++index) {
    if (!group_key_matches_member(input.key, members[index].key)) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kMemberGroupKeyMismatch;
      return result;
    }
    if (index > 0U && turnsafe_pair_member_key_equal(
                          members[index - 1U].key, members[index].key)) {
      result.terminal_reason = TurnSafePairTerminalReason::kDuplicateMemberKey;
      return result;
    }
    if (!member_primitive_valid(members[index])) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kMemberPrimitiveInvalid;
      return result;
    }
    if (!std::isfinite(members[index].rho_trans) ||
        members[index].rho_trans < 0.0 ||
        members[index].rho_trans > kMaximumRho) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kMemberRhoIneligible;
      return result;
    }
  }

  if (members.size() < 2U) {
    result.status = TurnSafePairGroupStatus::kInsufficientGroup;
    result.terminal_reason = TurnSafePairTerminalReason::kInsufficientGroup;
    return result;
  }
  if (members.size() < 4U) {
    result.status = TurnSafePairGroupStatus::kShadowOnlySmallGroup;
    result.terminal_reason =
        TurnSafePairTerminalReason::kShadowOnlySmallGroup;
    return result;
  }

  result.consensus.attempted = true;
  result.consensus.absolute_threshold = kAbsoluteThreshold;
  result.consensus.required_retained_count =
      std::max<std::size_t>(3U, members.size() - members.size() / 4U);
  std::vector<std::size_t> all_indices(members.size());
  std::iota(all_indices.begin(), all_indices.end(), 0U);
  if (!fit_proper_wahba(members, all_indices,
                        result.consensus.first_fit_rotation,
                        result.consensus.first_fit_rotation_angle_rad)) {
    result.terminal_reason = TurnSafePairTerminalReason::kConsensusSvdFailure;
    return result;
  }
  result.consensus.first_fit_available = true;

  std::vector<double> initial_norms;
  initial_norms.reserve(members.size());
  result.consensus.members.reserve(members.size());
  for (const auto &member : members) {
    double norm = std::numeric_limits<double>::quiet_NaN();
    if (!checked_whitened_norm(member,
                               result.consensus.first_fit_rotation, norm)) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kConsensusWhiteningFailure;
      return result;
    }
    initial_norms.push_back(norm);
    TurnSafePairMemberConsensusDiagnostic diagnostic;
    diagnostic.key = member.key;
    diagnostic.initial_whitened_norm = norm;
    result.consensus.members.push_back(std::move(diagnostic));
  }
  result.consensus.median = lower_median(initial_norms);
  std::vector<double> deviations;
  deviations.reserve(initial_norms.size());
  for (const double norm : initial_norms)
    deviations.push_back(std::fabs(norm - result.consensus.median));
  result.consensus.mad = lower_median(std::move(deviations));
  result.consensus.robust_threshold =
      result.consensus.median + kMadMultiplier * kMadNormalScale *
                                      result.consensus.mad;
  if (!std::isfinite(result.consensus.median) ||
      !std::isfinite(result.consensus.mad) ||
      !std::isfinite(result.consensus.robust_threshold)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kConsensusWhiteningFailure;
    return result;
  }

  std::vector<std::size_t> retained;
  for (std::size_t index = 0U; index < members.size(); ++index) {
    if (initial_norms[index] <= kAbsoluteThreshold &&
        initial_norms[index] <= result.consensus.robust_threshold) {
      retained.push_back(index);
      result.consensus.members[index].retained = true;
      result.consensus.retained_member_keys.push_back(members[index].key);
    }
  }
  if (retained.size() < result.consensus.required_retained_count) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kConsensusInsufficientInliers;
    return result;
  }

  if (!fit_proper_wahba(members, retained,
                        result.consensus.refit_rotation,
                        result.consensus.refit_rotation_angle_rad)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kConsensusRefitSvdFailure;
    return result;
  }
  result.consensus.refit_available = true;
  for (const std::size_t index : retained) {
    double norm = std::numeric_limits<double>::quiet_NaN();
    if (!checked_whitened_norm(members[index],
                               result.consensus.refit_rotation, norm)) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kConsensusWhiteningFailure;
      return result;
    }
    result.consensus.members[index].refit_norm_available = true;
    result.consensus.members[index].refit_whitened_norm = norm;
    if (norm > kAbsoluteThreshold ||
        norm > result.consensus.robust_threshold) {
      result.terminal_reason =
          TurnSafePairTerminalReason::kConsensusRefitThresholdFailure;
      return result;
    }
  }

  TurnSafePairTerminalReason spatial_failure =
      TurnSafePairTerminalReason::kSpatialInvalid;
  if (!evaluate_spatial(members, retained, result.spatial, spatial_failure)) {
    result.terminal_reason = spatial_failure;
    return result;
  }

  if (retained.size() >
      static_cast<std::size_t>(std::numeric_limits<Eigen::Index>::max() / 3)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kRotationStackSvdFailure;
    return result;
  }
  Eigen::MatrixXd rotation_stack(
      static_cast<Eigen::Index>(3U * retained.size()), 3);
  for (std::size_t row = 0U; row < retained.size(); ++row) {
    rotation_stack.block<3, 3>(static_cast<Eigen::Index>(3U * row), 0) =
        -skew(members[retained[row]].target_bearing);
  }
  Eigen::JacobiSVD<Eigen::MatrixXd> rotation_svd(rotation_stack);
  if (rotation_svd.singularValues().size() != 3 ||
      !rotation_svd.singularValues().allFinite()) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kRotationStackSvdFailure;
    return result;
  }
  result.rotation_stack_singular_values = rotation_svd.singularValues();
  const double sigma_one = result.rotation_stack_singular_values(0);
  result.rotation_stack_rank_tolerance =
      100.0 * std::numeric_limits<double>::epsilon() *
      static_cast<double>(std::max<Eigen::Index>(rotation_stack.rows(), 3)) *
      sigma_one;
  if (!std::isfinite(result.rotation_stack_rank_tolerance)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kRotationStackSvdFailure;
    return result;
  }
  result.rotation_stack_rank = 0U;
  for (Eigen::Index index = 0; index < 3; ++index) {
    if (result.rotation_stack_singular_values(index) >
        result.rotation_stack_rank_tolerance) {
      ++result.rotation_stack_rank;
    }
  }
  if (result.rotation_stack_rank != 3U) {
    result.terminal_reason = TurnSafePairTerminalReason::kRotationStackRank;
    return result;
  }
  if (!(sigma_one > 0.0)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kRotationStackConditioning;
    return result;
  }
  result.rotation_stack_condition_ratio =
      result.rotation_stack_singular_values(2) / sigma_one;
  if (!std::isfinite(result.rotation_stack_condition_ratio) ||
      result.rotation_stack_condition_ratio < kMinimumConditionRatio) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kRotationStackConditioning;
    return result;
  }

  if (retained.size() >
      static_cast<std::size_t>(std::numeric_limits<Eigen::Index>::max() / 2)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kWhitenedLocalStackSvdFailure;
    return result;
  }
  Eigen::MatrixXd whitened_stack(
      static_cast<Eigen::Index>(2U * retained.size()), 3);
  double maximum_rho = 0.0;
  for (std::size_t row = 0U; row < retained.size(); ++row) {
    const auto &member = members[retained[row]];
    whitened_stack.block<2, 3>(static_cast<Eigen::Index>(2U * row), 0) =
        member.whitened_local_rotation_block;
    maximum_rho = std::max(maximum_rho, member.rho_trans);
  }
  Eigen::JacobiSVD<Eigen::MatrixXd> whitened_svd(whitened_stack);
  if (whitened_svd.singularValues().size() != 3 ||
      !whitened_svd.singularValues().allFinite()) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kWhitenedLocalStackSvdFailure;
    return result;
  }
  result.whitened_local_stack_singular_values =
      whitened_svd.singularValues();
  const double information_eigenvalue_minimum =
      result.whitened_local_stack_singular_values(2) *
      result.whitened_local_stack_singular_values(2);
  const double information_eigenvalue_middle =
      result.whitened_local_stack_singular_values(1) *
      result.whitened_local_stack_singular_values(1);
  const double information_eigenvalue_maximum =
      result.whitened_local_stack_singular_values(0) *
      result.whitened_local_stack_singular_values(0);
  const Eigen::Vector3d information_eigenvalues(
      information_eigenvalue_minimum, information_eigenvalue_middle,
      information_eigenvalue_maximum);
  const double information_trace = information_eigenvalues.sum();
  const double information_determinant = information_eigenvalues.prod();
  if (!information_eigenvalues.allFinite() ||
      !std::isfinite(information_trace) ||
      !std::isfinite(information_determinant)) {
    result.terminal_reason =
        TurnSafePairTerminalReason::kPredictedInformationNonfinite;
    return result;
  }
  result.predicted_orientation_information_eigenvalues =
      information_eigenvalues;
  result.predicted_orientation_information_trace = information_trace;
  result.predicted_orientation_information_determinant =
      information_determinant;
  result.predicted_orientation_information_non_negligible =
      information_trace > kInformationTraceFloor;
  result.score.predicted_rotation_angle_rad =
      input.current_predicted_relative_camera_rotation_angle_rad;
  result.score.minimum_whitened_local_singular_value =
      result.whitened_local_stack_singular_values(2);
  result.score.maximum_retained_rho_trans = maximum_rho;
  result.score.retained_feature_count = retained.size();
  result.status = TurnSafePairGroupStatus::kEligible;
  result.terminal_reason = TurnSafePairTerminalReason::kNone;
  return result;
}

TurnSafePairSelectionResult TurnSafePairSelector::Select(
    const std::vector<TurnSafePairGroupInput> &inputs) {
  TurnSafePairSelectionResult selection;
  selection.groups.reserve(inputs.size());
  for (const auto &input : inputs)
    selection.groups.push_back(EvaluateGroup(input));
  std::sort(selection.groups.begin(), selection.groups.end(),
            [](const TurnSafePairGroupResult &left,
               const TurnSafePairGroupResult &right) {
              return turnsafe_pair_group_key_less(left.key, right.key);
            });
  for (std::size_t index = 1U; index < selection.groups.size(); ++index) {
    if (turnsafe_pair_group_key_equal(selection.groups[index - 1U].key,
                                      selection.groups[index].key)) {
      selection.status = TurnSafePairSelectionStatus::kDuplicateGroupKey;
      return selection;
    }
  }

  std::size_t winner = 0U;
  bool winner_available = false;
  for (std::size_t index = 0U; index < selection.groups.size(); ++index) {
    if (selection.groups[index].status != TurnSafePairGroupStatus::kEligible)
      continue;
    if (!winner_available ||
        score_better(selection.groups[index].score,
                     selection.groups[winner].score)) {
      winner = index;
      winner_available = true;
    }
  }
  if (!winner_available) return selection;

  selection.status = TurnSafePairSelectionStatus::kWinnerFrozen;
  selection.winner_available = true;
  selection.winner_index = winner;
  selection.winner_key = selection.groups[winner].key;
  for (std::size_t index = 0U; index < selection.groups.size(); ++index) {
    if (selection.groups[index].status == TurnSafePairGroupStatus::kEligible) {
      selection.groups[index].selection_role =
          index == winner ? TurnSafePairSelectionRole::kWinner
                          : TurnSafePairSelectionRole::kForegoneEligible;
    }
  }
  return selection;
}

TurnSafePairPostNisDecision TurnSafePairSelector::ApplyFrozenWinnerNis(
    const TurnSafePairSelectionResult &selection,
    bool winner_nis_passed) noexcept {
  TurnSafePairPostNisDecision decision;
  decision.frozen_winner_available = selection.winner_available;
  if (!selection.winner_available) return decision;
  decision.frozen_winner_key = selection.winner_key;
  decision.winner_nis_passed = winner_nis_passed;
  decision.accepted_winner_available = winner_nis_passed;
  decision.runner_up_promoted = false;
  return decision;
}

} // namespace ov_msckf
