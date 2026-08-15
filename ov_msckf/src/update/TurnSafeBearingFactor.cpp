/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe T1 pointer-free rotation-bearing factor.
 */

#include "TurnSafeBearingFactor.h"

#include <Eigen/Cholesky>
#include <Eigen/Geometry>
#include <Eigen/SVD>

#include <boost/math/distributions/chi_squared.hpp>

#include <algorithm>
#include <cmath>
#include <exception>
#include <limits>
#include <new>

namespace ov_msckf {
constexpr double TurnSafeBearingFactor::kFrozenPixelSigma;
constexpr double TurnSafeBearingFactor::kInformationTraceFloor;

namespace {

constexpr double kRotationAuditTolerance = 1.0e-10;
constexpr double kUnitBearingAuditTolerance =
    256.0 * std::numeric_limits<double>::epsilon();

Eigen::Matrix3d skew(const Eigen::Vector3d &value) noexcept {
  Eigen::Matrix3d result;
  result << 0.0, -value.z(), value.y(), value.z(), 0.0, -value.x(),
      -value.y(), value.x(), 0.0;
  return result;
}

template <typename Derived>
bool finite(const Eigen::MatrixBase<Derived> &value) noexcept {
  return value.allFinite();
}

bool unit_bearing(const Eigen::Vector3d &bearing) noexcept {
  if (!finite(bearing)) {
    return false;
  }
  const double squared_norm = bearing.squaredNorm();
  return std::isfinite(squared_norm) &&
         std::fabs(squared_norm - 1.0) <= kUnitBearingAuditTolerance;
}

bool rotation_matrix(const Eigen::Matrix3d &rotation) noexcept {
  if (!finite(rotation)) {
    return false;
  }
  const Eigen::Matrix3d orthogonality =
      rotation * rotation.transpose() - Eigen::Matrix3d::Identity();
  const double determinant = rotation.determinant();
  return finite(orthogonality) && std::isfinite(determinant) &&
         orthogonality.cwiseAbs().maxCoeff() <= kRotationAuditTolerance &&
         determinant > 0.0 &&
         std::fabs(determinant - 1.0) <= kRotationAuditTolerance;
}

template <typename Derived>
double symmetry_bound(const Eigen::MatrixBase<Derived> &matrix) noexcept {
  if (matrix.size() == 0 || !finite(matrix)) {
    return std::numeric_limits<double>::quiet_NaN();
  }
  const double scale = std::max(1.0, matrix.cwiseAbs().maxCoeff());
  return 64.0 * std::numeric_limits<double>::epsilon() *
         static_cast<double>(std::max<Eigen::Index>(1, matrix.rows())) * scale;
}

template <typename Derived>
double symmetry_error(const Eigen::MatrixBase<Derived> &matrix) noexcept {
  if (matrix.rows() != matrix.cols() || matrix.size() == 0 || !finite(matrix)) {
    return std::numeric_limits<double>::quiet_NaN();
  }
  return (matrix - matrix.transpose()).cwiseAbs().maxCoeff();
}

bool make_tangent_basis(const Eigen::Vector3d &target,
                        Eigen::Matrix<double, 3, 2> &basis) noexcept {
  const double absolute_x = std::fabs(target.x());
  const double absolute_y = std::fabs(target.y());
  const double absolute_z = std::fabs(target.z());
  Eigen::Vector3d axis = Eigen::Vector3d::UnitX();
  double smallest = absolute_x;
  if (absolute_y < smallest) {
    axis = Eigen::Vector3d::UnitY();
    smallest = absolute_y;
  }
  if (absolute_z < smallest) {
    axis = Eigen::Vector3d::UnitZ();
  }

  Eigen::Vector3d v1 = axis - target * target.dot(axis);
  const double v1_norm = v1.norm();
  if (!std::isfinite(v1_norm) || !(v1_norm > 0.0)) {
    return false;
  }
  v1 /= v1_norm;
  const Eigen::Vector3d v2 = target.cross(v1);
  if (!finite(v1) || !finite(v2)) {
    return false;
  }
  basis.col(0) = v1;
  basis.col(1) = v2;
  return finite(basis);
}

} // namespace

const char *turnsafe_bearing_factor_status_name(
    TurnSafeBearingFactorStatus status) noexcept {
  switch (status) {
  case TurnSafeBearingFactorStatus::kAccepted:
    return "accepted";
  case TurnSafeBearingFactorStatus::kNonfiniteInput:
    return "nonfinite_input";
  case TurnSafeBearingFactorStatus::kInvalidUnitBearing:
    return "invalid_unit_bearing";
  case TurnSafeBearingFactorStatus::kInvalidRotation:
    return "invalid_rotation";
  case TurnSafeBearingFactorStatus::kUnsupportedPixelNoise:
    return "unsupported_pixel_noise";
  case TurnSafeBearingFactorStatus::kDegenerateTangentBasis:
    return "degenerate_tangent_basis";
  case TurnSafeBearingFactorStatus::kResidualCovarianceNotSymmetric:
    return "residual_covariance_not_symmetric";
  case TurnSafeBearingFactorStatus::kResidualCovarianceNotPositiveDefinite:
    return "residual_covariance_not_positive_definite";
  case TurnSafeBearingFactorStatus::kWhiteningFailure:
    return "whitening_failure";
  }
  return "unknown";
}

const char *turnsafe_bearing_group_status_name(
    TurnSafeBearingGroupStatus status) noexcept {
  switch (status) {
  case TurnSafeBearingGroupStatus::kAccepted:
    return "accepted";
  case TurnSafeBearingGroupStatus::kEmptyInput:
    return "empty_input";
  case TurnSafeBearingGroupStatus::kInvalidFactor:
    return "invalid_factor";
  case TurnSafeBearingGroupStatus::kNonfinitePairCovariance:
    return "nonfinite_pair_covariance";
  case TurnSafeBearingGroupStatus::kPairCovarianceNotSymmetric:
    return "pair_covariance_not_symmetric";
  case TurnSafeBearingGroupStatus::kInnovationNotSymmetric:
    return "innovation_not_symmetric";
  case TurnSafeBearingGroupStatus::kInnovationNotPositiveDefinite:
    return "innovation_not_positive_definite";
  case TurnSafeBearingGroupStatus::kNisFailure:
    return "nis_failure";
  case TurnSafeBearingGroupStatus::kInformationFailure:
    return "information_failure";
  case TurnSafeBearingGroupStatus::kSizeOverflow:
    return "size_overflow";
  case TurnSafeBearingGroupStatus::kAllocationFailure:
    return "allocation_failure";
  }
  return "unknown";
}

TurnSafeBearingFactorResult TurnSafeBearingFactor::Evaluate(
    const TurnSafeBearingFactorInput &input) noexcept {
  TurnSafeBearingFactorResult result;

  if (!finite(input.source_bearing) || !finite(input.target_bearing) ||
      !finite(input.source_bearing_raw_pixel_jacobian) ||
      !finite(input.target_bearing_raw_pixel_jacobian) ||
      !finite(input.current_R_GtoI_source) ||
      !finite(input.current_R_GtoI_target) ||
      !finite(input.fej_R_GtoI_source) ||
      !finite(input.fej_R_GtoI_target) || !finite(input.fixed_R_ItoC) ||
      !std::isfinite(input.sigma_px)) {
    result.status = TurnSafeBearingFactorStatus::kNonfiniteInput;
    return result;
  }
  if (!unit_bearing(input.source_bearing) ||
      !unit_bearing(input.target_bearing)) {
    result.status = TurnSafeBearingFactorStatus::kInvalidUnitBearing;
    return result;
  }
  if (!rotation_matrix(input.current_R_GtoI_source) ||
      !rotation_matrix(input.current_R_GtoI_target) ||
      !rotation_matrix(input.fej_R_GtoI_source) ||
      !rotation_matrix(input.fej_R_GtoI_target) ||
      !rotation_matrix(input.fixed_R_ItoC)) {
    result.status = TurnSafeBearingFactorStatus::kInvalidRotation;
    return result;
  }
  if (input.sigma_px != kFrozenPixelSigma) {
    result.status = TurnSafeBearingFactorStatus::kUnsupportedPixelNoise;
    return result;
  }
  if (!make_tangent_basis(input.target_bearing, result.B)) {
    result.status = TurnSafeBearingFactorStatus::kDegenerateTangentBasis;
    return result;
  }

  const Eigen::Matrix3d &E = input.fixed_R_ItoC;
  result.current_R_ab =
      E * input.current_R_GtoI_target *
      input.current_R_GtoI_source.transpose() * E.transpose();
  const Eigen::Vector3d current_prediction =
      result.current_R_ab * input.source_bearing;
  result.residual =
      result.B.transpose() * (input.target_bearing - current_prediction);

  const Eigen::Vector3d w = E.transpose() * input.source_bearing;
  const Eigen::Vector3d u =
      input.fej_R_GtoI_target * input.fej_R_GtoI_source.transpose() * w;
  const Eigen::Matrix3d J_a =
      -E * input.fej_R_GtoI_target *
      input.fej_R_GtoI_source.transpose() * skew(w);
  const Eigen::Matrix3d J_b = E * skew(u);
  result.H_direct_pose.block<2, 3>(0, 0) = result.B.transpose() * J_a;
  result.H_direct_pose.block<2, 3>(0, 6) = result.B.transpose() * J_b;

  const Eigen::Matrix2d J_za =
      -result.B.transpose() * result.current_R_ab *
      input.source_bearing_raw_pixel_jacobian;
  const Eigen::Matrix2d J_zb =
      result.B.transpose() * input.target_bearing_raw_pixel_jacobian;
  result.Sigma_R = input.sigma_px * input.sigma_px *
                   (J_za * J_za.transpose() + J_zb * J_zb.transpose());
  result.Sigma_R_symmetry_error = symmetry_error(result.Sigma_R);
  result.Sigma_R_symmetry_bound = symmetry_bound(result.Sigma_R);
  if (!finite(result.current_R_ab) || !finite(result.residual) ||
      !finite(result.H_direct_pose) || !finite(result.Sigma_R)) {
    result.status = TurnSafeBearingFactorStatus::kNonfiniteInput;
    return result;
  }
  if (!std::isfinite(result.Sigma_R_symmetry_error) ||
      !std::isfinite(result.Sigma_R_symmetry_bound) ||
      result.Sigma_R_symmetry_error > result.Sigma_R_symmetry_bound) {
    result.status =
        TurnSafeBearingFactorStatus::kResidualCovarianceNotSymmetric;
    return result;
  }

  Eigen::LLT<Eigen::Matrix2d> llt(result.Sigma_R);
  if (llt.info() != Eigen::Success) {
    result.status =
        TurnSafeBearingFactorStatus::kResidualCovarianceNotPositiveDefinite;
    return result;
  }
  result.whitening_lower = llt.matrixL();
  if (!finite(result.whitening_lower) ||
      !(result.whitening_lower(0, 0) > 0.0) ||
      !(result.whitening_lower(1, 1) > 0.0)) {
    result.status =
        TurnSafeBearingFactorStatus::kResidualCovarianceNotPositiveDefinite;
    return result;
  }

  result.residual_whitened =
      result.whitening_lower.triangularView<Eigen::Lower>().solve(
          result.residual);
  result.H_direct_pose_whitened =
      result.whitening_lower.triangularView<Eigen::Lower>().solve(
          result.H_direct_pose);
  const Eigen::Vector3d fej_prediction = E * u;
  const Eigen::Matrix<double, 2, 3> H_relative =
      result.B.transpose() * skew(fej_prediction);
  result.H_relative_whitened =
      result.whitening_lower.triangularView<Eigen::Lower>().solve(H_relative);
  if (!finite(result.residual_whitened) ||
      !finite(result.H_direct_pose_whitened) ||
      !finite(result.H_relative_whitened)) {
    result.status = TurnSafeBearingFactorStatus::kWhiteningFailure;
    return result;
  }

  result.status = TurnSafeBearingFactorStatus::kAccepted;
  return result;
}

TurnSafeBearingGroupResult TurnSafeBearingFactor::EvaluateGroup(
    const std::vector<TurnSafeBearingFactorResult> &factors,
    const TurnSafePairPoseCovariance &pair_covariance) noexcept {
  TurnSafeBearingGroupResult result;
  result.factor_count = factors.size();
  if (factors.empty()) {
    result.status = TurnSafeBearingGroupStatus::kEmptyInput;
    return result;
  }
  const std::size_t eigen_index_max = static_cast<std::size_t>(
      std::numeric_limits<Eigen::Index>::max());
  if (factors.size() > eigen_index_max / 2U) {
    result.status = TurnSafeBearingGroupStatus::kSizeOverflow;
    return result;
  }
  if (!finite(pair_covariance)) {
    result.status = TurnSafeBearingGroupStatus::kNonfinitePairCovariance;
    return result;
  }
  const double pair_symmetry_error = symmetry_error(pair_covariance);
  const double pair_symmetry_bound = symmetry_bound(pair_covariance);
  if (!std::isfinite(pair_symmetry_error) ||
      !std::isfinite(pair_symmetry_bound) ||
      pair_symmetry_error > pair_symmetry_bound) {
    result.status = TurnSafeBearingGroupStatus::kPairCovarianceNotSymmetric;
    return result;
  }
  for (const TurnSafeBearingFactorResult &factor : factors) {
    if (!factor.accepted() || !finite(factor.residual) ||
        !finite(factor.H_direct_pose) || !finite(factor.Sigma_R) ||
        !finite(factor.H_relative_whitened)) {
      result.status = TurnSafeBearingGroupStatus::kInvalidFactor;
      return result;
    }
  }

  try {
    const Eigen::Index rows =
        static_cast<Eigen::Index>(2U * factors.size());
    result.degrees_of_freedom = rows;
    result.stacked_residual = Eigen::VectorXd::Zero(rows);
    result.stacked_H_direct_pose = Eigen::MatrixXd::Zero(rows, 12);
    result.stacked_measurement_covariance =
        Eigen::MatrixXd::Zero(rows, rows);
    result.stacked_H_relative_whitened = Eigen::MatrixXd::Zero(rows, 3);
    for (std::size_t index = 0U; index < factors.size(); ++index) {
      const Eigen::Index row = static_cast<Eigen::Index>(2U * index);
      const TurnSafeBearingFactorResult &factor = factors[index];
      result.stacked_residual.segment<2>(row) = factor.residual;
      result.stacked_H_direct_pose.block<2, 12>(row, 0) =
          factor.H_direct_pose;
      result.stacked_measurement_covariance.block<2, 2>(row, row) =
          factor.Sigma_R;
      result.stacked_H_relative_whitened.block<2, 3>(row, 0) =
          factor.H_relative_whitened;
    }

    result.innovation_covariance =
        result.stacked_H_direct_pose * pair_covariance *
            result.stacked_H_direct_pose.transpose() +
        result.stacked_measurement_covariance;
    result.innovation_symmetry_error =
        symmetry_error(result.innovation_covariance);
    result.innovation_symmetry_bound =
        symmetry_bound(result.innovation_covariance);
    if (!finite(result.innovation_covariance) ||
        !std::isfinite(result.innovation_symmetry_error) ||
        !std::isfinite(result.innovation_symmetry_bound) ||
        result.innovation_symmetry_error > result.innovation_symmetry_bound) {
      result.status = TurnSafeBearingGroupStatus::kInnovationNotSymmetric;
      return result;
    }

    Eigen::LLT<Eigen::MatrixXd> innovation_llt(
        result.innovation_covariance);
    if (innovation_llt.info() != Eigen::Success) {
      result.status =
          TurnSafeBearingGroupStatus::kInnovationNotPositiveDefinite;
      return result;
    }
    const Eigen::VectorXd innovation_solution =
        innovation_llt.solve(result.stacked_residual);
    if (innovation_llt.info() != Eigen::Success ||
        !finite(innovation_solution)) {
      result.status = TurnSafeBearingGroupStatus::kNisFailure;
      return result;
    }
    result.nis = result.stacked_residual.dot(innovation_solution);
    const boost::math::chi_squared distribution(
        static_cast<double>(rows));
    result.nis_threshold = boost::math::quantile(distribution, 0.95);
    if (!std::isfinite(result.nis) || !(result.nis >= 0.0) ||
        !std::isfinite(result.nis_threshold) ||
        !(result.nis_threshold > 0.0)) {
      result.status = TurnSafeBearingGroupStatus::kNisFailure;
      return result;
    }
    result.nis_passed = !(result.nis > result.nis_threshold);

    result.I_rel = result.stacked_H_relative_whitened.transpose() *
                   result.stacked_H_relative_whitened;
    if (!finite(result.I_rel)) {
      result.status = TurnSafeBearingGroupStatus::kInformationFailure;
      return result;
    }
    result.information_trace = result.I_rel.trace();
    result.information_determinant = result.I_rel.determinant();

    Eigen::JacobiSVD<Eigen::MatrixXd> relative_svd(
        result.stacked_H_relative_whitened,
        Eigen::ComputeThinU | Eigen::ComputeThinV);
    if (!finite(relative_svd.singularValues())) {
      result.status = TurnSafeBearingGroupStatus::kInformationFailure;
      return result;
    }
    for (Eigen::Index index = 0;
         index < relative_svd.singularValues().rows() && index < 3; ++index) {
      result.relative_stack_singular_values(index) =
          relative_svd.singularValues()(index);
    }
    result.information_eigenvalues <<
        result.relative_stack_singular_values(2) *
            result.relative_stack_singular_values(2),
        result.relative_stack_singular_values(1) *
            result.relative_stack_singular_values(1),
        result.relative_stack_singular_values(0) *
            result.relative_stack_singular_values(0);
    if (!std::isfinite(result.information_trace) ||
        !std::isfinite(result.information_determinant) ||
        !finite(result.relative_stack_singular_values) ||
        !finite(result.information_eigenvalues)) {
      result.status = TurnSafeBearingGroupStatus::kInformationFailure;
      return result;
    }
    result.information_non_negligible =
        result.information_trace > kInformationTraceFloor;
    result.status = TurnSafeBearingGroupStatus::kAccepted;
    return result;
  } catch (const std::bad_alloc &) {
    result.status = TurnSafeBearingGroupStatus::kAllocationFailure;
    return result;
  } catch (const std::exception &) {
    result.status = TurnSafeBearingGroupStatus::kNisFailure;
    return result;
  } catch (...) {
    result.status = TurnSafeBearingGroupStatus::kNisFailure;
    return result;
  }
}

} // namespace ov_msckf
