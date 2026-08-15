/*
 * TurnSafe T1 value-only certificate core.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_TURNSAFE_CERTIFICATE_H
#define OV_MSCKF_TURNSAFE_CERTIFICATE_H

#include <Eigen/Dense>

#include <cstddef>
#include <cstdint>
#include <limits>

namespace ov_msckf {

enum class TurnSafeCertificateStatus : std::uint8_t {
  kAvailable = 0,
  kNonfiniteInput,
  kInvalidCameraCalibration,
  kPixelOutsideImage,
  kCamRadtanJacobianIllConditioned,
  kCamRadtanSolveFailure,
  kCamRadtanNonconvergent,
  kCapturedNormalizedInconsistent,
  kBearingNormalizationFailure,
  kCameraIdentityMismatch,
  kFeatureIdentityMismatch,
  kTimestampIdentityMismatch,
  kStereoGeometryIllConditioned,
  kStereoSolveFailure,
  kStereoNonpositiveRange,
  kRangeVarianceInvalid,
  kRangeLcbNonpositive,
  kCovarianceSymmetryFailure,
  kCovarianceNotPsd,
  kCovarianceEigenFailure,
  kTranslationCovarianceInvalid,
  kQuantileFailure,
  kTranslationNotAcute,
  kResidualCovarianceNotPositiveDefinite,
  kResidualCovarianceIllConditioned,
  kRhoExceeded,
};

const char *turnsafe_certificate_status_name(
    TurnSafeCertificateStatus status) noexcept;

struct TurnSafeCertificateCamera {
  std::size_t camera_id = 0U;
  Eigen::Matrix<double, 8, 1> intrinsics =
      Eigen::Matrix<double, 8, 1>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix3d R_ItoC = Eigen::Matrix3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d p_IinC = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  int width = 0;
  int height = 0;
};

struct TurnSafeCertificateObservation {
  std::uint64_t feature_id = 0U;
  std::uint64_t detached_index = 0U;
  std::size_t camera_id = 0U;
  double timestamp = std::numeric_limits<double>::quiet_NaN();
  Eigen::Vector2d raw_pixel = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector2d captured_normalized = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
};

struct TurnSafeCertificateClonePose {
  Eigen::Matrix3d R_GtoI = Eigen::Matrix3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d p_IinG = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
};

struct TurnSafeCapturedPairCovariance {
  Eigen::Matrix<double, 6, 6> P_ss =
      Eigen::Matrix<double, 6, 6>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 6, 6> P_tt =
      Eigen::Matrix<double, 6, 6>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 6, 6> P_st =
      Eigen::Matrix<double, 6, 6>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 6, 6> P_ts =
      Eigen::Matrix<double, 6, 6>::Constant(
          std::numeric_limits<double>::quiet_NaN());
};

struct TurnSafeCamRadtanForwardResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  Eigen::Vector2d raw_pixel = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d pixel_wrt_normalized = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeCamRadtanInverseResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  Eigen::Vector2d normalized = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d pixel_wrt_normalized = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d normalized_wrt_pixel = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  double reciprocal_condition = std::numeric_limits<double>::quiet_NaN();
  double final_pixel_inf_norm = std::numeric_limits<double>::quiet_NaN();
  int iterations = 0;

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeBearingResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  Eigen::Vector2d normalized = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d bearing = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector2d audited_forward_pixel = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  double captured_forward_pixel_inf_norm =
      std::numeric_limits<double>::quiet_NaN();
  Eigen::Matrix2d pixel_wrt_normalized = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d normalized_wrt_pixel = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 3, 2> bearing_wrt_normalized =
      Eigen::Matrix<double, 3, 2>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 3, 2> bearing_wrt_pixel =
      Eigen::Matrix<double, 3, 2>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  TurnSafeCamRadtanInverseResult checked_inverse;

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeStereoRangeResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  TurnSafeBearingResult main_bearing;
  TurnSafeBearingResult mate_bearing;
  Eigen::Matrix<double, 3, 2> ray_matrix =
      Eigen::Matrix<double, 3, 2>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector2d ray_ranges = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d ray_residual = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector2d ray_singular_values = Eigen::Vector2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  double ray_reciprocal_condition =
      std::numeric_limits<double>::quiet_NaN();
  Eigen::Matrix<double, 1, 4> range_wrt_pixels =
      Eigen::Matrix<double, 1, 4>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  double d_hat = std::numeric_limits<double>::quiet_NaN();
  double range_variance = std::numeric_limits<double>::quiet_NaN();
  double range_standard_deviation =
      std::numeric_limits<double>::quiet_NaN();
  double normal_quantile = std::numeric_limits<double>::quiet_NaN();
  double d_lcb = std::numeric_limits<double>::quiet_NaN();

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeTranslationCertificateResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  Eigen::Vector3d source_camera_center = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d target_camera_center = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Vector3d translation_mean = Eigen::Vector3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 3, 12> translation_jacobian =
      Eigen::Matrix<double, 3, 12>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix<double, 12, 12> captured_pair_covariance =
      Eigen::Matrix<double, 12, 12>::Constant(
          std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix3d translation_covariance = Eigen::Matrix3d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix3d certified_translation_covariance =
      Eigen::Matrix3d::Constant(
          std::numeric_limits<double>::quiet_NaN());
  double covariance_inflation = std::numeric_limits<double>::quiet_NaN();
  double chi_square_quantile = std::numeric_limits<double>::quiet_NaN();
  double chi_square_radius = std::numeric_limits<double>::quiet_NaN();
  double certified_lambda_max = std::numeric_limits<double>::quiet_NaN();
  double t_ucb = std::numeric_limits<double>::quiet_NaN();

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeBearingResidualCovarianceResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  Eigen::Matrix2d source_residual_wrt_pixel = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d target_residual_wrt_pixel = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  Eigen::Matrix2d covariance = Eigen::Matrix2d::Constant(
      std::numeric_limits<double>::quiet_NaN());
  double lambda_min = std::numeric_limits<double>::quiet_NaN();
  double lambda_max = std::numeric_limits<double>::quiet_NaN();
  double reciprocal_condition = std::numeric_limits<double>::quiet_NaN();

  bool available() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

struct TurnSafeAcuteCertificateResult {
  TurnSafeCertificateStatus status =
      TurnSafeCertificateStatus::kNonfiniteInput;
  bool acute = false;
  bool theta_available = false;
  bool rho_available = false;
  double theta_trans_ucb = std::numeric_limits<double>::quiet_NaN();
  double lambda_min = std::numeric_limits<double>::quiet_NaN();
  double lambda_max = std::numeric_limits<double>::quiet_NaN();
  double reciprocal_condition = std::numeric_limits<double>::quiet_NaN();
  double sigma_bearing_worst =
      std::numeric_limits<double>::quiet_NaN();
  double rho_trans = std::numeric_limits<double>::quiet_NaN();

  bool admitted() const noexcept {
    return status == TurnSafeCertificateStatus::kAvailable;
  }
};

class TurnSafeCertificate final {
public:
  static constexpr double kTranslationComponentConfidence = 0.99865;
  static constexpr double kRangeComponentConfidence = 0.99865;
  static constexpr double kJointUnionBoundConfidence = 0.9973;
  static constexpr double kTranslationCovarianceInflation = 2.0;
  static constexpr double kPixelNoiseSigma = 1.2;
  static constexpr double kRhoMaximum = 0.5;
  static constexpr int kCamRadtanNewtonMaximumIterations = 20;
  static constexpr double kCamRadtanNewtonPixelInfNorm = 1.0e-12;
  static constexpr double kCapturedNormalizedAuditPixelInfNorm = 5.0e-4;
  static constexpr double kChecked2x2ReciprocalConditionMinimum = 1.0e-12;
  static constexpr double kStereoRayReciprocalConditionMinimum = 1.0e-6;
  static constexpr double kBearingCovarianceReciprocalConditionMinimum =
      1.0e-12;

  static TurnSafeCamRadtanForwardResult CamRadtanForward(
      const Eigen::Matrix<double, 8, 1> &intrinsics,
      const Eigen::Vector2d &normalized);

  static TurnSafeCamRadtanInverseResult CamRadtanInverse(
      const TurnSafeCertificateCamera &camera,
      const Eigen::Vector2d &raw_pixel);

  static TurnSafeBearingResult BearingFromCapturedNormalized(
      const TurnSafeCertificateCamera &camera,
      const Eigen::Vector2d &raw_pixel,
      const Eigen::Vector2d &captured_normalized);

  static TurnSafeStereoRangeResult TargetTimeStereoRange(
      const TurnSafeCertificateCamera &main_camera,
      const TurnSafeCertificateObservation &main_observation,
      const TurnSafeCertificateCamera &mate_camera,
      const TurnSafeCertificateObservation &mate_observation);

  static TurnSafeTranslationCertificateResult RelativeCameraTranslation(
      const TurnSafeCertificateCamera &camera,
      const TurnSafeCertificateClonePose &source,
      const TurnSafeCertificateClonePose &target,
      const TurnSafeCapturedPairCovariance &captured_covariance);

  static TurnSafeBearingResidualCovarianceResult BearingResidualCovariance(
      const TurnSafeBearingResult &source,
      const TurnSafeBearingResult &target,
      const Eigen::Matrix3d &source_to_target_rotation,
      const Eigen::Matrix<double, 3, 2> &target_tangent_basis);

  static TurnSafeAcuteCertificateResult AcuteAndRho(
      double translation_ucb, double range_lcb,
      const Eigen::Matrix2d &residual_covariance);
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_CERTIFICATE_H
