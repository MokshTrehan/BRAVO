/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 Patrick Geneva
 * Copyright (C) 2018-2023 Guoquan Huang
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2018-2019 Kevin Eckenhoff
 * Copyright (C) 2026 Moksh Trehan
 * Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef OV_MSCKF_SCHUR_UPDATE_H
#define OV_MSCKF_SCHUR_UPDATE_H

#include <Eigen/Core>

#include <cstddef>
#include <limits>

namespace ov_msckf {

/// Ordered outcome of a transient-landmark Schur reduction.
enum class SchurReductionStatus {
  kAccepted,
  kNonfinite,
  kInsufficientRows,
  kRankDeficient,
  kIllConditioned,
};

/// Exact terminal stage, used to distinguish failures with the same status.
enum class SchurReductionStage {
  kInputDimensions,
  kInsufficientRows,
  kRawInputs,
  kSigma,
  kWhitening,
  kSvdSpectrum,
  kLargestSingularValue,
  kNumericalRank,
  kConditioning,
  kQrFactors,
  kQrTransform,
  kReducedOutputs,
  kStatistics,
  kAccepted,
};

/// Stable lower-case names for machine-readable diagnostics.
const char *schur_reduction_status_name(SchurReductionStatus status) noexcept;
const char *schur_reduction_stage_name(SchurReductionStage stage) noexcept;

/**
 * @brief Result of eliminating one three-dimensional transient landmark.
 *
 * Singular values are available only after a complete finite SVD spectrum is
 * produced. The ratio is available only after the largest singular value has
 * passed its strict floor. Callers must honor the availability flags; the
 * unavailable numeric fields deliberately contain NaN rather than a finite
 * substitute.
 *
 * Reduced rows and sufficient statistics are populated only when status is
 * kAccepted. They are expressed in the unwhitened row convention with scalar
 * noise variance sigma_px^2, while lambda, eta, and gamma are the corresponding
 * whitened sufficient statistics.
 */
struct SchurReductionResult {
  SchurReductionStatus status = SchurReductionStatus::kNonfinite;
  SchurReductionStage stage = SchurReductionStage::kInputDimensions;

  Eigen::Index raw_rows = 0;
  Eigen::Index degrees_of_freedom = 0;

  bool singular_values_available = false;
  bool singular_ratio_available = false;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Constant(std::numeric_limits<double>::quiet_NaN());
  double singular_ratio = std::numeric_limits<double>::quiet_NaN();

  Eigen::MatrixXd H_reduced;
  Eigen::VectorXd residual_reduced;
  double noise_variance = std::numeric_limits<double>::quiet_NaN();

  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = std::numeric_limits<double>::quiet_NaN();
  double raw_lambda_symmetry_error_inf = std::numeric_limits<double>::quiet_NaN();

  std::size_t jitter_count = 0;
  std::size_t clamp_count = 0;
  std::size_t regularization_count = 0;
  std::size_t fallback_count = 0;

  bool accepted() const noexcept { return status == SchurReductionStatus::kAccepted; }
};

/**
 * @brief Production square-root Schur reducer for a GLOBAL_3D MSCKF feature.
 */
class SchurUpdate {
public:
  static constexpr double kMinimumSingularRatio = 1.0e-6;

  /**
   * @brief Eliminate a three-column transient-landmark Jacobian.
   *
   * The operation is transactional: all inputs are read-only and no reduced
   * output is published unless every ordered validity, rank, QR, and finite
   * statistics check succeeds. There is no regularization or fallback path.
   *
   * @param H_x Raw state Jacobian.
   * @param H_f Raw three-column GLOBAL_3D landmark Jacobian.
   * @param residual Raw measurement residual.
   * @param sigma_px Scalar pixel standard deviation.
   */
  static SchurReductionResult Reduce(const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
                                     const Eigen::VectorXd &residual, double sigma_px);
};

} // namespace ov_msckf

#endif // OV_MSCKF_SCHUR_UPDATE_H
