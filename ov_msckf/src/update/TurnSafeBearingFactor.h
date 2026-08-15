/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pointer-free rotation-bearing factor.
 */

#ifndef OV_MSCKF_TURNSAFE_BEARING_FACTOR_H
#define OV_MSCKF_TURNSAFE_BEARING_FACTOR_H

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

namespace ov_msckf {

using TurnSafeDirectPoseJacobian = Eigen::Matrix<double, 2, 12>;
using TurnSafePairPoseCovariance = Eigen::Matrix<double, 12, 12>;

enum class TurnSafeBearingFactorStatus : std::uint8_t {
  kAccepted,
  kNonfiniteInput,
  kInvalidUnitBearing,
  kInvalidRotation,
  kUnsupportedPixelNoise,
  kDegenerateTangentBasis,
  kResidualCovarianceNotSymmetric,
  kResidualCovarianceNotPositiveDefinite,
  kWhiteningFailure,
};

const char *turnsafe_bearing_factor_status_name(
    TurnSafeBearingFactorStatus status) noexcept;

/**
 * Complete value-only input for one frozen T1 rotation-bearing factor.
 *
 * The two 3x2 Jacobians are derivatives of the measured unit bearings with
 * respect to their own raw pixel coordinates. Every rotation maps the frame
 * named on the right to the frame named on the left. No live estimator object
 * or pointer enters this type.
 */
struct TurnSafeBearingFactorInput {
  Eigen::Vector3d source_bearing = Eigen::Vector3d::Zero();
  Eigen::Vector3d target_bearing = Eigen::Vector3d::Zero();
  Eigen::Matrix<double, 3, 2> source_bearing_raw_pixel_jacobian =
      Eigen::Matrix<double, 3, 2>::Zero();
  Eigen::Matrix<double, 3, 2> target_bearing_raw_pixel_jacobian =
      Eigen::Matrix<double, 3, 2>::Zero();

  Eigen::Matrix3d current_R_GtoI_source = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d current_R_GtoI_target = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d fej_R_GtoI_source = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d fej_R_GtoI_target = Eigen::Matrix3d::Identity();
  Eigen::Matrix3d fixed_R_ItoC = Eigen::Matrix3d::Identity();

  double sigma_px = 1.2;
};

/** Owning output of one checked factor construction and whitening. */
struct TurnSafeBearingFactorResult {
  TurnSafeBearingFactorStatus status =
      TurnSafeBearingFactorStatus::kNonfiniteInput;

  /// Deterministic measured-target tangent basis, B=[v1,v2].
  Eigen::Matrix<double, 3, 2> B = Eigen::Matrix<double, 3, 2>::Zero();
  /// Current R_ab = E R_b R_a^T E^T.
  Eigen::Matrix3d current_R_ab = Eigen::Matrix3d::Identity();
  /// Current production-sign innovation B^T(b_b-R_ab b_a).
  Eigen::Vector2d residual = Eigen::Vector2d::Zero();

  /// FEJ positive measurement-model Jacobian [H_a,0,H_b,0].
  TurnSafeDirectPoseJacobian H_direct_pose =
      TurnSafeDirectPoseJacobian::Zero();

  Eigen::Matrix2d Sigma_R = Eigen::Matrix2d::Zero();
  double Sigma_R_symmetry_error =
      std::numeric_limits<double>::quiet_NaN();
  double Sigma_R_symmetry_bound =
      std::numeric_limits<double>::quiet_NaN();
  Eigen::Matrix2d whitening_lower = Eigen::Matrix2d::Zero();
  Eigen::Vector2d residual_whitened = Eigen::Vector2d::Zero();
  TurnSafeDirectPoseJacobian H_direct_pose_whitened =
      TurnSafeDirectPoseJacobian::Zero();

  /**
   * FEJ block for a target-camera left relative-rotation error. The direct
   * clone errors obey delta_rel=E*delta_b-R_ab*E*delta_a, and this block is
   * L^-1 B^T [R_ab_fej b_a]x.
   */
  Eigen::Matrix<double, 2, 3> H_relative_whitened =
      Eigen::Matrix<double, 2, 3>::Zero();

  bool accepted() const noexcept {
    return status == TurnSafeBearingFactorStatus::kAccepted;
  }
};

enum class TurnSafeBearingGroupStatus : std::uint8_t {
  kAccepted,
  kEmptyInput,
  kInvalidFactor,
  kNonfinitePairCovariance,
  kPairCovarianceNotSymmetric,
  kInnovationNotSymmetric,
  kInnovationNotPositiveDefinite,
  kNisFailure,
  kInformationFailure,
  kSizeOverflow,
  kAllocationFailure,
};

const char *turnsafe_bearing_group_status_name(
    TurnSafeBearingGroupStatus status) noexcept;

/** Checked winner-only shadow NIS and predicted relative-orientation data. */
struct TurnSafeBearingGroupResult {
  TurnSafeBearingGroupStatus status =
      TurnSafeBearingGroupStatus::kEmptyInput;
  std::size_t factor_count = 0U;
  Eigen::Index degrees_of_freedom = 0;

  Eigen::VectorXd stacked_residual;
  Eigen::MatrixXd stacked_H_direct_pose;
  Eigen::MatrixXd stacked_measurement_covariance;
  Eigen::MatrixXd innovation_covariance;
  double innovation_symmetry_error =
      std::numeric_limits<double>::quiet_NaN();
  double innovation_symmetry_bound =
      std::numeric_limits<double>::quiet_NaN();

  double nis = std::numeric_limits<double>::quiet_NaN();
  double nis_threshold = std::numeric_limits<double>::quiet_NaN();
  bool nis_passed = false;

  Eigen::MatrixXd stacked_H_relative_whitened;
  Eigen::Matrix3d I_rel = Eigen::Matrix3d::Zero();
  /// Ascending order, matching Eigen's self-adjoint eigensolver.
  Eigen::Vector3d information_eigenvalues = Eigen::Vector3d::Zero();
  /// Descending order; missing dimensions of a short stack remain zero.
  Eigen::Vector3d relative_stack_singular_values = Eigen::Vector3d::Zero();
  double information_trace = std::numeric_limits<double>::quiet_NaN();
  double information_determinant =
      std::numeric_limits<double>::quiet_NaN();
  bool information_non_negligible = false;

  bool accepted() const noexcept {
    return status == TurnSafeBearingGroupStatus::kAccepted;
  }
};

class TurnSafeBearingFactor final {
public:
  static constexpr double kFrozenPixelSigma = 1.2;
  static constexpr double kInformationTraceFloor = 1.0e-12;

  static TurnSafeBearingFactorResult
  Evaluate(const TurnSafeBearingFactorInput &input) noexcept;

  /**
   * Compute the frozen 0.95 Boost.Math chi-square shadow NIS (multiplier 1)
   * and relative-orientation information from already retained factors.
   * Equality with the NIS threshold passes.
   */
  static TurnSafeBearingGroupResult EvaluateGroup(
      const std::vector<TurnSafeBearingFactorResult> &factors,
      const TurnSafePairPoseCovariance &pair_covariance) noexcept;

  TurnSafeBearingFactor() = delete;
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_BEARING_FACTOR_H
