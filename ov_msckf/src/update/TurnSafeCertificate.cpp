/*
 * TurnSafe T1 value-only certificate core.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "TurnSafeCertificate.h"

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>
#include <Eigen/LU>
#include <Eigen/SVD>

#include <boost/math/distributions/chi_squared.hpp>
#include <boost/math/distributions/normal.hpp>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>

namespace ov_msckf {

constexpr double TurnSafeCertificate::kTranslationComponentConfidence;
constexpr double TurnSafeCertificate::kRangeComponentConfidence;
constexpr double TurnSafeCertificate::kJointUnionBoundConfidence;
constexpr double TurnSafeCertificate::kTranslationCovarianceInflation;
constexpr double TurnSafeCertificate::kPixelNoiseSigma;
constexpr double TurnSafeCertificate::kRhoMaximum;
constexpr int TurnSafeCertificate::kCamRadtanNewtonMaximumIterations;
constexpr double TurnSafeCertificate::kCamRadtanNewtonPixelInfNorm;
constexpr double TurnSafeCertificate::kCapturedNormalizedAuditPixelInfNorm;
constexpr double TurnSafeCertificate::kChecked2x2ReciprocalConditionMinimum;
constexpr double TurnSafeCertificate::kStereoRayReciprocalConditionMinimum;
constexpr double
    TurnSafeCertificate::kBearingCovarianceReciprocalConditionMinimum;

namespace {

template <typename Derived>
double max_abs(const Eigen::MatrixBase<Derived> &matrix) {
  return matrix.size() == 0 ? 0.0 : matrix.cwiseAbs().maxCoeff();
}

template <typename Derived>
double binary64_audit_tolerance(const Eigen::MatrixBase<Derived> &matrix) {
  return 100.0 * std::numeric_limits<double>::epsilon() *
         std::max(1.0, max_abs(matrix));
}

template <typename Derived>
bool symmetry_audit_passes(const Eigen::MatrixBase<Derived> &matrix) {
  return matrix.rows() == matrix.cols() && matrix.allFinite() &&
         max_abs(matrix - matrix.transpose()) <=
             binary64_audit_tolerance(matrix);
}

Eigen::Matrix3d skew_x(const Eigen::Vector3d &vector) {
  Eigen::Matrix3d output;
  output << 0.0, -vector(2), vector(1), vector(2), 0.0, -vector(0),
      -vector(1), vector(0), 0.0;
  return output;
}

bool rotation_is_valid(const Eigen::Matrix3d &rotation) {
  if (!rotation.allFinite()) return false;
  const Eigen::Matrix3d orthogonality =
      rotation.transpose() * rotation - Eigen::Matrix3d::Identity();
  const double tolerance = binary64_audit_tolerance(rotation);
  return max_abs(orthogonality) <= tolerance &&
         std::isfinite(rotation.determinant()) &&
         std::fabs(rotation.determinant() - 1.0) <= tolerance;
}

bool intrinsic_camera_is_valid(const TurnSafeCertificateCamera &camera) {
  return camera.width > 0 && camera.height > 0 &&
         camera.intrinsics.allFinite() && camera.intrinsics(0) > 0.0 &&
         camera.intrinsics(1) > 0.0;
}

bool extrinsic_camera_is_valid(const TurnSafeCertificateCamera &camera) {
  return intrinsic_camera_is_valid(camera) &&
         rotation_is_valid(camera.R_ItoC) && camera.p_IinC.allFinite();
}

bool raw_pixel_is_valid(const TurnSafeCertificateCamera &camera,
                        const Eigen::Vector2d &raw_pixel) {
  return raw_pixel.allFinite() && raw_pixel(0) >= 0.0 &&
         raw_pixel(0) < static_cast<double>(camera.width) &&
         raw_pixel(1) >= 0.0 &&
         raw_pixel(1) < static_cast<double>(camera.height);
}

bool checked_inverse_2x2(const Eigen::Matrix2d &matrix,
                         double minimum_reciprocal_condition,
                         Eigen::Matrix2d &inverse,
                         double &reciprocal_condition) {
  if (!matrix.allFinite()) return false;
  Eigen::JacobiSVD<Eigen::Matrix2d> singular_value_decomposition(
      matrix, Eigen::ComputeFullU | Eigen::ComputeFullV);
  if (!singular_value_decomposition.singularValues().allFinite()) {
    return false;
  }
  const double maximum = singular_value_decomposition.singularValues()(0);
  const double minimum = singular_value_decomposition.singularValues()(1);
  if (!(maximum > 0.0) || !(minimum > 0.0)) return false;
  reciprocal_condition = minimum / maximum;
  if (!std::isfinite(reciprocal_condition) ||
      reciprocal_condition < minimum_reciprocal_condition) {
    return false;
  }
  Eigen::FullPivLU<Eigen::Matrix2d> factorization(matrix);
  if (!factorization.isInvertible()) return false;
  inverse = factorization.solve(Eigen::Matrix2d::Identity());
  return inverse.allFinite();
}

bool same_binary64(double left, double right) noexcept {
  std::uint64_t left_bits = 0U;
  std::uint64_t right_bits = 0U;
  static_assert(sizeof(left_bits) == sizeof(left),
                "binary64 timestamp required");
  std::memcpy(&left_bits, &left, sizeof(left_bits));
  std::memcpy(&right_bits, &right, sizeof(right_bits));
  return left_bits == right_bits;
}

bool normal_quantile(double probability, double &value) noexcept {
  try {
    const boost::math::normal_distribution<double> distribution;
    value = boost::math::quantile(distribution, probability);
  } catch (...) {
    return false;
  }
  return std::isfinite(value);
}

bool chi_square_3_quantile(double probability, double &value) noexcept {
  try {
    const boost::math::chi_squared_distribution<double> distribution(3.0);
    value = boost::math::quantile(distribution, probability);
  } catch (...) {
    return false;
  }
  return std::isfinite(value) && value > 0.0;
}

TurnSafeCertificateStatus validate_residual_covariance(
    const Eigen::Matrix2d &covariance, double &lambda_min,
    double &lambda_max, double &reciprocal_condition) {
  if (!covariance.allFinite()) {
    return TurnSafeCertificateStatus::kNonfiniteInput;
  }
  if (!symmetry_audit_passes(covariance)) {
    return TurnSafeCertificateStatus::kCovarianceSymmetryFailure;
  }

  Eigen::LLT<Eigen::Matrix2d> cholesky;
  cholesky.compute(covariance.selfadjointView<Eigen::Lower>());
  if (cholesky.info() != Eigen::Success) {
    return TurnSafeCertificateStatus::kResidualCovarianceNotPositiveDefinite;
  }

  Eigen::SelfAdjointEigenSolver<Eigen::Matrix2d> eigen_solver;
  eigen_solver.compute(covariance.selfadjointView<Eigen::Lower>(),
                       Eigen::EigenvaluesOnly);
  if (eigen_solver.info() != Eigen::Success ||
      !eigen_solver.eigenvalues().allFinite()) {
    return TurnSafeCertificateStatus::kCovarianceEigenFailure;
  }
  lambda_min = eigen_solver.eigenvalues()(0);
  lambda_max = eigen_solver.eigenvalues()(1);
  if (!(lambda_min > 0.0) || !(lambda_max > 0.0)) {
    return TurnSafeCertificateStatus::kResidualCovarianceNotPositiveDefinite;
  }
  reciprocal_condition = lambda_min / lambda_max;
  if (!std::isfinite(reciprocal_condition) ||
      reciprocal_condition <
          TurnSafeCertificate::
              kBearingCovarianceReciprocalConditionMinimum) {
    return TurnSafeCertificateStatus::kResidualCovarianceIllConditioned;
  }
  return TurnSafeCertificateStatus::kAvailable;
}

} // namespace

const char *turnsafe_certificate_status_name(
    TurnSafeCertificateStatus status) noexcept {
  switch (status) {
  case TurnSafeCertificateStatus::kAvailable: return "AVAILABLE";
  case TurnSafeCertificateStatus::kNonfiniteInput: return "NONFINITE_INPUT";
  case TurnSafeCertificateStatus::kInvalidCameraCalibration:
    return "INVALID_CAMERA_CALIBRATION";
  case TurnSafeCertificateStatus::kPixelOutsideImage:
    return "PIXEL_OUTSIDE_IMAGE";
  case TurnSafeCertificateStatus::kCamRadtanJacobianIllConditioned:
    return "CAMRADTAN_JACOBIAN_ILL_CONDITIONED";
  case TurnSafeCertificateStatus::kCamRadtanSolveFailure:
    return "CAMRADTAN_SOLVE_FAILURE";
  case TurnSafeCertificateStatus::kCamRadtanNonconvergent:
    return "CAMRADTAN_NONCONVERGENT";
  case TurnSafeCertificateStatus::kCapturedNormalizedInconsistent:
    return "CAPTURED_NORMALIZED_INCONSISTENT";
  case TurnSafeCertificateStatus::kBearingNormalizationFailure:
    return "BEARING_NORMALIZATION_FAILURE";
  case TurnSafeCertificateStatus::kCameraIdentityMismatch:
    return "CAMERA_IDENTITY_MISMATCH";
  case TurnSafeCertificateStatus::kFeatureIdentityMismatch:
    return "FEATURE_IDENTITY_MISMATCH";
  case TurnSafeCertificateStatus::kTimestampIdentityMismatch:
    return "TIMESTAMP_IDENTITY_MISMATCH";
  case TurnSafeCertificateStatus::kStereoGeometryIllConditioned:
    return "STEREO_GEOMETRY_ILL_CONDITIONED";
  case TurnSafeCertificateStatus::kStereoSolveFailure:
    return "STEREO_SOLVE_FAILURE";
  case TurnSafeCertificateStatus::kStereoNonpositiveRange:
    return "STEREO_NONPOSITIVE_RANGE";
  case TurnSafeCertificateStatus::kRangeVarianceInvalid:
    return "RANGE_VARIANCE_INVALID";
  case TurnSafeCertificateStatus::kRangeLcbNonpositive:
    return "RANGE_LCB_NONPOSITIVE";
  case TurnSafeCertificateStatus::kCovarianceSymmetryFailure:
    return "COVARIANCE_SYMMETRY_FAILURE";
  case TurnSafeCertificateStatus::kCovarianceNotPsd:
    return "COVARIANCE_NOT_PSD";
  case TurnSafeCertificateStatus::kCovarianceEigenFailure:
    return "COVARIANCE_EIGEN_FAILURE";
  case TurnSafeCertificateStatus::kTranslationCovarianceInvalid:
    return "TRANSLATION_COVARIANCE_INVALID";
  case TurnSafeCertificateStatus::kQuantileFailure:
    return "QUANTILE_FAILURE";
  case TurnSafeCertificateStatus::kTranslationNotAcute:
    return "TRANSLATION_NOT_ACUTE";
  case TurnSafeCertificateStatus::kResidualCovarianceNotPositiveDefinite:
    return "RESIDUAL_COVARIANCE_NOT_POSITIVE_DEFINITE";
  case TurnSafeCertificateStatus::kResidualCovarianceIllConditioned:
    return "RESIDUAL_COVARIANCE_ILL_CONDITIONED";
  case TurnSafeCertificateStatus::kRhoExceeded: return "RHO_EXCEEDED";
  }
  return "UNKNOWN";
}

TurnSafeCamRadtanForwardResult TurnSafeCertificate::CamRadtanForward(
    const Eigen::Matrix<double, 8, 1> &intrinsics,
    const Eigen::Vector2d &normalized) {
  TurnSafeCamRadtanForwardResult output;
  if (!intrinsics.allFinite() || !normalized.allFinite() ||
      !(intrinsics(0) > 0.0) || !(intrinsics(1) > 0.0)) {
    return output;
  }

  const double x = normalized(0);
  const double y = normalized(1);
  const double radius_squared = x * x + y * y;
  const double radius_fourth = radius_squared * radius_squared;
  const double radial = 1.0 + intrinsics(4) * radius_squared +
                        intrinsics(5) * radius_fourth;
  const double radial_x =
      2.0 * intrinsics(4) * x + 4.0 * intrinsics(5) * x * radius_squared;
  const double radial_y =
      2.0 * intrinsics(4) * y + 4.0 * intrinsics(5) * y * radius_squared;

  const double distorted_x =
      x * radial + 2.0 * intrinsics(6) * x * y +
      intrinsics(7) * (radius_squared + 2.0 * x * x);
  const double distorted_y =
      y * radial + intrinsics(6) * (radius_squared + 2.0 * y * y) +
      2.0 * intrinsics(7) * x * y;

  output.raw_pixel << intrinsics(0) * distorted_x + intrinsics(2),
      intrinsics(1) * distorted_y + intrinsics(3);
  output.pixel_wrt_normalized(0, 0) =
      intrinsics(0) *
      (radial + x * radial_x + 2.0 * intrinsics(6) * y +
       6.0 * intrinsics(7) * x);
  output.pixel_wrt_normalized(0, 1) =
      intrinsics(0) *
      (x * radial_y + 2.0 * intrinsics(6) * x +
       2.0 * intrinsics(7) * y);
  output.pixel_wrt_normalized(1, 0) =
      intrinsics(1) *
      (y * radial_x + 2.0 * intrinsics(6) * x +
       2.0 * intrinsics(7) * y);
  output.pixel_wrt_normalized(1, 1) =
      intrinsics(1) *
      (radial + y * radial_y + 6.0 * intrinsics(6) * y +
       2.0 * intrinsics(7) * x);

  if (!output.raw_pixel.allFinite() ||
      !output.pixel_wrt_normalized.allFinite()) {
    return output;
  }
  output.status = TurnSafeCertificateStatus::kAvailable;
  return output;
}

TurnSafeCamRadtanInverseResult TurnSafeCertificate::CamRadtanInverse(
    const TurnSafeCertificateCamera &camera,
    const Eigen::Vector2d &raw_pixel) {
  TurnSafeCamRadtanInverseResult output;
  if (!intrinsic_camera_is_valid(camera)) {
    output.status = TurnSafeCertificateStatus::kInvalidCameraCalibration;
    return output;
  }
  if (!raw_pixel.allFinite()) return output;
  if (!raw_pixel_is_valid(camera, raw_pixel)) {
    output.status = TurnSafeCertificateStatus::kPixelOutsideImage;
    return output;
  }

  Eigen::Vector2d normalized(
      (raw_pixel(0) - camera.intrinsics(2)) / camera.intrinsics(0),
      (raw_pixel(1) - camera.intrinsics(3)) / camera.intrinsics(1));
  if (!normalized.allFinite()) return output;

  for (int iteration = 0;
       iteration <= kCamRadtanNewtonMaximumIterations; ++iteration) {
    const TurnSafeCamRadtanForwardResult forward =
        CamRadtanForward(camera.intrinsics, normalized);
    if (!forward.available()) return output;
    const Eigen::Vector2d pixel_residual = forward.raw_pixel - raw_pixel;
    const double residual_inf_norm =
        pixel_residual.lpNorm<Eigen::Infinity>();
    if (!std::isfinite(residual_inf_norm)) return output;
    output.final_pixel_inf_norm = residual_inf_norm;

    if (residual_inf_norm <= kCamRadtanNewtonPixelInfNorm) {
      Eigen::Matrix2d inverse;
      double reciprocal_condition =
          std::numeric_limits<double>::quiet_NaN();
      if (!checked_inverse_2x2(
              forward.pixel_wrt_normalized,
              kChecked2x2ReciprocalConditionMinimum, inverse,
              reciprocal_condition)) {
        output.status =
            TurnSafeCertificateStatus::kCamRadtanJacobianIllConditioned;
        return output;
      }
      output.normalized = normalized;
      output.pixel_wrt_normalized = forward.pixel_wrt_normalized;
      output.normalized_wrt_pixel = inverse;
      output.reciprocal_condition = reciprocal_condition;
      output.status = TurnSafeCertificateStatus::kAvailable;
      return output;
    }
    if (iteration == kCamRadtanNewtonMaximumIterations) break;

    Eigen::Matrix2d inverse;
    double reciprocal_condition = std::numeric_limits<double>::quiet_NaN();
    if (!checked_inverse_2x2(
            forward.pixel_wrt_normalized,
            kChecked2x2ReciprocalConditionMinimum, inverse,
            reciprocal_condition)) {
      output.status =
          TurnSafeCertificateStatus::kCamRadtanJacobianIllConditioned;
      return output;
    }
    const Eigen::Vector2d step = inverse * pixel_residual;
    if (!step.allFinite()) {
      output.status = TurnSafeCertificateStatus::kCamRadtanSolveFailure;
      return output;
    }
    normalized -= step;
    if (!normalized.allFinite()) return output;
    output.iterations = iteration + 1;
  }

  output.normalized = normalized;
  output.status = TurnSafeCertificateStatus::kCamRadtanNonconvergent;
  return output;
}

TurnSafeBearingResult TurnSafeCertificate::BearingFromCapturedNormalized(
    const TurnSafeCertificateCamera &camera,
    const Eigen::Vector2d &raw_pixel,
    const Eigen::Vector2d &captured_normalized) {
  TurnSafeBearingResult output;
  output.checked_inverse = CamRadtanInverse(camera, raw_pixel);
  if (!output.checked_inverse.available()) {
    output.status = output.checked_inverse.status;
    return output;
  }
  if (!captured_normalized.allFinite()) return output;

  const TurnSafeCamRadtanForwardResult forward =
      CamRadtanForward(camera.intrinsics, captured_normalized);
  if (!forward.available()) {
    output.status = forward.status;
    return output;
  }
  output.audited_forward_pixel = forward.raw_pixel;
  output.captured_forward_pixel_inf_norm =
      (forward.raw_pixel - raw_pixel).lpNorm<Eigen::Infinity>();
  if (!std::isfinite(output.captured_forward_pixel_inf_norm)) return output;
  if (output.captured_forward_pixel_inf_norm >
      kCapturedNormalizedAuditPixelInfNorm) {
    output.status =
        TurnSafeCertificateStatus::kCapturedNormalizedInconsistent;
    return output;
  }

  Eigen::Matrix2d normalized_wrt_pixel;
  double reciprocal_condition = std::numeric_limits<double>::quiet_NaN();
  if (!checked_inverse_2x2(
          forward.pixel_wrt_normalized,
          kChecked2x2ReciprocalConditionMinimum, normalized_wrt_pixel,
          reciprocal_condition)) {
    output.status =
        TurnSafeCertificateStatus::kCamRadtanJacobianIllConditioned;
    return output;
  }

  Eigen::Vector3d homogeneous(captured_normalized(0),
                              captured_normalized(1), 1.0);
  const double norm = homogeneous.norm();
  if (!(norm > 0.0) || !std::isfinite(norm)) {
    output.status = TurnSafeCertificateStatus::kBearingNormalizationFailure;
    return output;
  }
  const Eigen::Vector3d bearing = homogeneous / norm;
  Eigen::Matrix<double, 3, 2> selector;
  selector << 1.0, 0.0, 0.0, 1.0, 0.0, 0.0;
  const Eigen::Matrix<double, 3, 2> bearing_wrt_normalized =
      (Eigen::Matrix3d::Identity() - bearing * bearing.transpose()) /
      norm * selector;
  const Eigen::Matrix<double, 3, 2> bearing_wrt_pixel =
      bearing_wrt_normalized * normalized_wrt_pixel;
  if (!bearing.allFinite() || !bearing_wrt_normalized.allFinite() ||
      !bearing_wrt_pixel.allFinite()) {
    output.status = TurnSafeCertificateStatus::kBearingNormalizationFailure;
    return output;
  }

  output.normalized = captured_normalized;
  output.bearing = bearing;
  output.pixel_wrt_normalized = forward.pixel_wrt_normalized;
  output.normalized_wrt_pixel = normalized_wrt_pixel;
  output.bearing_wrt_normalized = bearing_wrt_normalized;
  output.bearing_wrt_pixel = bearing_wrt_pixel;
  output.status = TurnSafeCertificateStatus::kAvailable;
  return output;
}

TurnSafeStereoRangeResult TurnSafeCertificate::TargetTimeStereoRange(
    const TurnSafeCertificateCamera &main_camera,
    const TurnSafeCertificateObservation &main_observation,
    const TurnSafeCertificateCamera &mate_camera,
    const TurnSafeCertificateObservation &mate_observation) {
  TurnSafeStereoRangeResult output;
  if (!extrinsic_camera_is_valid(main_camera) ||
      !extrinsic_camera_is_valid(mate_camera)) {
    output.status = TurnSafeCertificateStatus::kInvalidCameraCalibration;
    return output;
  }
  if (main_camera.camera_id == mate_camera.camera_id ||
      main_observation.camera_id != main_camera.camera_id ||
      mate_observation.camera_id != mate_camera.camera_id) {
    output.status = TurnSafeCertificateStatus::kCameraIdentityMismatch;
    return output;
  }
  if (main_observation.feature_id != mate_observation.feature_id ||
      main_observation.detached_index != mate_observation.detached_index) {
    output.status = TurnSafeCertificateStatus::kFeatureIdentityMismatch;
    return output;
  }
  if (!std::isfinite(main_observation.timestamp) ||
      !std::isfinite(mate_observation.timestamp)) {
    return output;
  }
  if (!same_binary64(main_observation.timestamp,
                     mate_observation.timestamp)) {
    output.status = TurnSafeCertificateStatus::kTimestampIdentityMismatch;
    return output;
  }

  output.main_bearing = BearingFromCapturedNormalized(
      main_camera, main_observation.raw_pixel,
      main_observation.captured_normalized);
  if (!output.main_bearing.available()) {
    output.status = output.main_bearing.status;
    return output;
  }
  output.mate_bearing = BearingFromCapturedNormalized(
      mate_camera, mate_observation.raw_pixel,
      mate_observation.captured_normalized);
  if (!output.mate_bearing.available()) {
    output.status = output.mate_bearing.status;
    return output;
  }

  const Eigen::Vector3d main_center =
      -main_camera.R_ItoC.transpose() * main_camera.p_IinC;
  const Eigen::Vector3d mate_center =
      -mate_camera.R_ItoC.transpose() * mate_camera.p_IinC;
  const Eigen::Vector3d main_ray =
      main_camera.R_ItoC.transpose() * output.main_bearing.bearing;
  const Eigen::Vector3d mate_ray =
      mate_camera.R_ItoC.transpose() * output.mate_bearing.bearing;
  output.ray_matrix.col(0) = main_ray;
  output.ray_matrix.col(1) = -mate_ray;

  Eigen::JacobiSVD<Eigen::Matrix<double, 3, 2>> ray_svd(
      output.ray_matrix, Eigen::ComputeFullU | Eigen::ComputeFullV);
  if (!ray_svd.singularValues().allFinite()) {
    output.status =
        TurnSafeCertificateStatus::kStereoGeometryIllConditioned;
    return output;
  }
  output.ray_singular_values = ray_svd.singularValues();
  if (!(output.ray_singular_values(0) > 0.0) ||
      !(output.ray_singular_values(1) > 0.0)) {
    output.status =
        TurnSafeCertificateStatus::kStereoGeometryIllConditioned;
    return output;
  }
  output.ray_reciprocal_condition =
      output.ray_singular_values(1) / output.ray_singular_values(0);
  if (!std::isfinite(output.ray_reciprocal_condition) ||
      output.ray_reciprocal_condition <
          kStereoRayReciprocalConditionMinimum) {
    output.status =
        TurnSafeCertificateStatus::kStereoGeometryIllConditioned;
    return output;
  }

  const Eigen::Vector3d displacement = mate_center - main_center;
  const Eigen::Matrix2d normal_matrix =
      output.ray_matrix.transpose() * output.ray_matrix;
  const Eigen::Vector2d right_hand_side =
      output.ray_matrix.transpose() * displacement;
  Eigen::FullPivLU<Eigen::Matrix2d> normal_factorization(normal_matrix);
  if (!normal_factorization.isInvertible()) {
    output.status = TurnSafeCertificateStatus::kStereoSolveFailure;
    return output;
  }
  output.ray_ranges = normal_factorization.solve(right_hand_side);
  if (!output.ray_ranges.allFinite()) {
    output.status = TurnSafeCertificateStatus::kStereoSolveFailure;
    return output;
  }
  if (!(output.ray_ranges(0) > 0.0) ||
      !(output.ray_ranges(1) > 0.0)) {
    output.status = TurnSafeCertificateStatus::kStereoNonpositiveRange;
    return output;
  }
  output.ray_residual =
      displacement - output.ray_matrix * output.ray_ranges;
  if (!output.ray_residual.allFinite()) {
    output.status = TurnSafeCertificateStatus::kStereoSolveFailure;
    return output;
  }

  for (Eigen::Index pixel_column = 0; pixel_column < 4; ++pixel_column) {
    Eigen::Matrix<double, 3, 2> ray_matrix_derivative =
        Eigen::Matrix<double, 3, 2>::Zero();
    if (pixel_column < 2) {
      ray_matrix_derivative.col(0) =
          main_camera.R_ItoC.transpose() *
          output.main_bearing.bearing_wrt_pixel.col(pixel_column);
    } else {
      ray_matrix_derivative.col(1) =
          -mate_camera.R_ItoC.transpose() *
          output.mate_bearing.bearing_wrt_pixel.col(pixel_column - 2);
    }
    const Eigen::Vector2d derivative_right_hand_side =
        ray_matrix_derivative.transpose() * output.ray_residual -
        output.ray_matrix.transpose() * ray_matrix_derivative *
            output.ray_ranges;
    const Eigen::Vector2d range_derivative =
        normal_factorization.solve(derivative_right_hand_side);
    if (!range_derivative.allFinite()) {
      output.status = TurnSafeCertificateStatus::kStereoSolveFailure;
      return output;
    }
    output.range_wrt_pixels(pixel_column) = range_derivative(0);
  }

  output.d_hat = output.ray_ranges(0);
  output.range_variance = kPixelNoiseSigma * kPixelNoiseSigma *
                          output.range_wrt_pixels.squaredNorm();
  if (!std::isfinite(output.range_variance) ||
      !(output.range_variance > 0.0)) {
    output.status = TurnSafeCertificateStatus::kRangeVarianceInvalid;
    return output;
  }
  output.range_standard_deviation = std::sqrt(output.range_variance);
  if (!normal_quantile(kRangeComponentConfidence, output.normal_quantile)) {
    output.status = TurnSafeCertificateStatus::kQuantileFailure;
    return output;
  }
  output.d_lcb = output.d_hat -
                 output.normal_quantile * output.range_standard_deviation;
  if (!std::isfinite(output.d_lcb) || !(output.d_lcb > 0.0)) {
    output.status = TurnSafeCertificateStatus::kRangeLcbNonpositive;
    return output;
  }
  output.status = TurnSafeCertificateStatus::kAvailable;
  return output;
}

TurnSafeTranslationCertificateResult
TurnSafeCertificate::RelativeCameraTranslation(
    const TurnSafeCertificateCamera &camera,
    const TurnSafeCertificateClonePose &source,
    const TurnSafeCertificateClonePose &target,
    const TurnSafeCapturedPairCovariance &captured_covariance) {
  TurnSafeTranslationCertificateResult output;
  if (!extrinsic_camera_is_valid(camera) ||
      !rotation_is_valid(source.R_GtoI) ||
      !rotation_is_valid(target.R_GtoI)) {
    output.status = TurnSafeCertificateStatus::kInvalidCameraCalibration;
    return output;
  }
  if (!source.p_IinG.allFinite() || !target.p_IinG.allFinite() ||
      !captured_covariance.P_ss.allFinite() ||
      !captured_covariance.P_tt.allFinite() ||
      !captured_covariance.P_st.allFinite() ||
      !captured_covariance.P_ts.allFinite()) {
    return output;
  }

  output.captured_pair_covariance.block<6, 6>(0, 0) =
      captured_covariance.P_ss;
  output.captured_pair_covariance.block<6, 6>(0, 6) =
      captured_covariance.P_st;
  output.captured_pair_covariance.block<6, 6>(6, 0) =
      captured_covariance.P_ts;
  output.captured_pair_covariance.block<6, 6>(6, 6) =
      captured_covariance.P_tt;
  if (!symmetry_audit_passes(output.captured_pair_covariance)) {
    output.status = TurnSafeCertificateStatus::kCovarianceSymmetryFailure;
    return output;
  }

  Eigen::SelfAdjointEigenSolver<Eigen::Matrix<double, 12, 12>> pair_solver;
  pair_solver.compute(
      output.captured_pair_covariance.selfadjointView<Eigen::Lower>(),
      Eigen::EigenvaluesOnly);
  if (pair_solver.info() != Eigen::Success ||
      !pair_solver.eigenvalues().allFinite()) {
    output.status = TurnSafeCertificateStatus::kCovarianceEigenFailure;
    return output;
  }
  if (pair_solver.eigenvalues()(0) < 0.0) {
    output.status = TurnSafeCertificateStatus::kCovarianceNotPsd;
    return output;
  }

  const Eigen::Vector3d lever_arm =
      camera.R_ItoC.transpose() * camera.p_IinC;
  output.source_camera_center =
      source.p_IinG - source.R_GtoI.transpose() * lever_arm;
  output.target_camera_center =
      target.p_IinG - target.R_GtoI.transpose() * lever_arm;
  output.translation_mean =
      output.source_camera_center - output.target_camera_center;
  output.translation_jacobian.setZero();
  output.translation_jacobian.block<3, 3>(0, 0) =
      source.R_GtoI.transpose() * skew_x(lever_arm);
  output.translation_jacobian.block<3, 3>(0, 3).setIdentity();
  output.translation_jacobian.block<3, 3>(0, 6) =
      -target.R_GtoI.transpose() * skew_x(lever_arm);
  output.translation_jacobian.block<3, 3>(0, 9) =
      -Eigen::Matrix3d::Identity();

  output.translation_covariance =
      output.translation_jacobian * output.captured_pair_covariance *
      output.translation_jacobian.transpose();
  if (!output.translation_covariance.allFinite() ||
      !symmetry_audit_passes(output.translation_covariance)) {
    output.status =
        TurnSafeCertificateStatus::kTranslationCovarianceInvalid;
    return output;
  }
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> translation_solver;
  translation_solver.compute(
      output.translation_covariance.selfadjointView<Eigen::Lower>(),
      Eigen::EigenvaluesOnly);
  if (translation_solver.info() != Eigen::Success ||
      !translation_solver.eigenvalues().allFinite()) {
    output.status = TurnSafeCertificateStatus::kCovarianceEigenFailure;
    return output;
  }
  if (translation_solver.eigenvalues()(0) < 0.0) {
    output.status =
        TurnSafeCertificateStatus::kTranslationCovarianceInvalid;
    return output;
  }

  output.covariance_inflation = kTranslationCovarianceInflation;
  output.certified_translation_covariance =
      kTranslationCovarianceInflation * output.translation_covariance;
  if (!output.certified_translation_covariance.allFinite() ||
      !symmetry_audit_passes(output.certified_translation_covariance)) {
    output.status =
        TurnSafeCertificateStatus::kTranslationCovarianceInvalid;
    return output;
  }
  Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> certified_solver;
  certified_solver.compute(
      output.certified_translation_covariance
          .selfadjointView<Eigen::Lower>(),
      Eigen::EigenvaluesOnly);
  if (certified_solver.info() != Eigen::Success ||
      !certified_solver.eigenvalues().allFinite()) {
    output.status = TurnSafeCertificateStatus::kCovarianceEigenFailure;
    return output;
  }
  if (certified_solver.eigenvalues()(0) < 0.0) {
    output.status =
        TurnSafeCertificateStatus::kTranslationCovarianceInvalid;
    return output;
  }
  output.certified_lambda_max = certified_solver.eigenvalues()(2);
  if (!(output.certified_lambda_max >= 0.0) ||
      !chi_square_3_quantile(kTranslationComponentConfidence,
                             output.chi_square_quantile)) {
    output.status = TurnSafeCertificateStatus::kQuantileFailure;
    return output;
  }
  output.chi_square_radius = std::sqrt(output.chi_square_quantile);
  output.t_ucb =
      output.translation_mean.norm() +
      std::sqrt(output.chi_square_quantile * output.certified_lambda_max);
  if (!std::isfinite(output.t_ucb)) {
    output.status =
        TurnSafeCertificateStatus::kTranslationCovarianceInvalid;
    return output;
  }
  output.status = TurnSafeCertificateStatus::kAvailable;
  return output;
}

TurnSafeBearingResidualCovarianceResult
TurnSafeCertificate::BearingResidualCovariance(
    const TurnSafeBearingResult &source,
    const TurnSafeBearingResult &target,
    const Eigen::Matrix3d &source_to_target_rotation,
    const Eigen::Matrix<double, 3, 2> &target_tangent_basis) {
  TurnSafeBearingResidualCovarianceResult output;
  if (!source.available()) {
    output.status = source.status;
    return output;
  }
  if (!target.available()) {
    output.status = target.status;
    return output;
  }
  if (!rotation_is_valid(source_to_target_rotation) ||
      !target_tangent_basis.allFinite()) {
    return output;
  }
  const Eigen::Matrix2d basis_orthogonality =
      target_tangent_basis.transpose() * target_tangent_basis -
      Eigen::Matrix2d::Identity();
  const Eigen::Vector2d basis_tangency =
      target_tangent_basis.transpose() * target.bearing;
  const double basis_tolerance =
      binary64_audit_tolerance(target_tangent_basis);
  if (max_abs(basis_orthogonality) > basis_tolerance ||
      max_abs(basis_tangency) > basis_tolerance) {
    output.status = TurnSafeCertificateStatus::kBearingNormalizationFailure;
    return output;
  }

  output.source_residual_wrt_pixel =
      -target_tangent_basis.transpose() * source_to_target_rotation *
      source.bearing_wrt_pixel;
  output.target_residual_wrt_pixel =
      target_tangent_basis.transpose() * target.bearing_wrt_pixel;
  output.covariance =
      kPixelNoiseSigma * kPixelNoiseSigma *
      (output.source_residual_wrt_pixel *
           output.source_residual_wrt_pixel.transpose() +
       output.target_residual_wrt_pixel *
           output.target_residual_wrt_pixel.transpose());
  output.status = validate_residual_covariance(
      output.covariance, output.lambda_min, output.lambda_max,
      output.reciprocal_condition);
  return output;
}

TurnSafeAcuteCertificateResult TurnSafeCertificate::AcuteAndRho(
    double translation_ucb, double range_lcb,
    const Eigen::Matrix2d &residual_covariance) {
  TurnSafeAcuteCertificateResult output;
  if (!std::isfinite(translation_ucb) || !std::isfinite(range_lcb) ||
      translation_ucb < 0.0 || !(range_lcb > 0.0)) {
    return output;
  }
  if (translation_ucb >= range_lcb) {
    output.status = TurnSafeCertificateStatus::kTranslationNotAcute;
    return output;
  }

  output.status = validate_residual_covariance(
      residual_covariance, output.lambda_min, output.lambda_max,
      output.reciprocal_condition);
  if (output.status != TurnSafeCertificateStatus::kAvailable) return output;

  output.acute = true;
  output.theta_trans_ucb = std::asin(translation_ucb / range_lcb);
  output.theta_available = std::isfinite(output.theta_trans_ucb);
  output.sigma_bearing_worst = std::sqrt(output.lambda_min);
  output.rho_trans =
      output.theta_trans_ucb / output.sigma_bearing_worst;
  output.rho_available = output.theta_available &&
                         std::isfinite(output.sigma_bearing_worst) &&
                         output.sigma_bearing_worst > 0.0 &&
                         std::isfinite(output.rho_trans);
  if (!output.rho_available) {
    output.status =
        TurnSafeCertificateStatus::kResidualCovarianceNotPositiveDefinite;
    return output;
  }
  output.status = output.rho_trans <= kRhoMaximum
                      ? TurnSafeCertificateStatus::kAvailable
                      : TurnSafeCertificateStatus::kRhoExceeded;
  return output;
}

} // namespace ov_msckf
