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

#include "SchurUpdate.h"

#include <Eigen/QR>
#include <Eigen/SVD>

#include <algorithm>
#include <cmath>
#include <limits>

namespace ov_msckf {

const char *schur_reduction_status_name(SchurReductionStatus status) noexcept {
  switch (status) {
  case SchurReductionStatus::kAccepted:
    return "accepted";
  case SchurReductionStatus::kNonfinite:
    return "nonfinite";
  case SchurReductionStatus::kInsufficientRows:
    return "insufficient_rows";
  case SchurReductionStatus::kRankDeficient:
    return "rank_deficient";
  case SchurReductionStatus::kIllConditioned:
    return "ill_conditioned";
  }
  return "unknown";
}

const char *schur_reduction_stage_name(SchurReductionStage stage) noexcept {
  switch (stage) {
  case SchurReductionStage::kInputDimensions:
    return "input_dimensions";
  case SchurReductionStage::kInsufficientRows:
    return "insufficient_rows";
  case SchurReductionStage::kRawInputs:
    return "raw_inputs";
  case SchurReductionStage::kSigma:
    return "sigma";
  case SchurReductionStage::kWhitening:
    return "whitening";
  case SchurReductionStage::kSvdSpectrum:
    return "svd_spectrum";
  case SchurReductionStage::kLargestSingularValue:
    return "largest_singular_value";
  case SchurReductionStage::kNumericalRank:
    return "numerical_rank";
  case SchurReductionStage::kConditioning:
    return "conditioning";
  case SchurReductionStage::kQrFactors:
    return "qr_factors";
  case SchurReductionStage::kQrTransform:
    return "qr_transform";
  case SchurReductionStage::kReducedOutputs:
    return "reduced_outputs";
  case SchurReductionStage::kStatistics:
    return "statistics";
  case SchurReductionStage::kAccepted:
    return "accepted";
  }
  return "unknown";
}

SchurReductionResult SchurUpdate::Reduce(const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
                                         const Eigen::VectorXd &residual, double sigma_px) {
  SchurReductionResult result;
  result.raw_rows = H_f.rows();

  // CP1 ordered policy, step 1: shape and representation compatibility.
  if (H_f.cols() != 3 || H_x.rows() != H_f.rows() || residual.rows() != H_f.rows()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kInputDimensions;
    return result;
  }

  const Eigen::Index m = H_f.rows();

  // Step 2: a three-dimensional landmark must leave at least one row.
  if (m <= 3) {
    result.status = SchurReductionStatus::kInsufficientRows;
    result.stage = SchurReductionStage::kInsufficientRows;
    return result;
  }

  // Step 3: raw fields and scalar noise must be valid before whitening.
  if (!H_x.allFinite() || !H_f.allFinite() || !residual.allFinite()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kRawInputs;
    return result;
  }
  if (!std::isfinite(sigma_px) || !(sigma_px > 0.0)) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kSigma;
    return result;
  }

  // Step 4: scalar whitening. Evaluate the contract's elementwise divisions
  // directly: 1/sigma may overflow even when every individual quotient is
  // finite (for example when both a raw row and sigma are subnormal).
  const Eigen::MatrixXd A = H_x.array() / sigma_px;
  const Eigen::MatrixXd B = H_f.array() / sigma_px;
  const Eigen::VectorXd b = residual.array() / sigma_px;
  if (!A.allFinite() || !B.allFinite() || !b.allFinite()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kWhitening;
    return result;
  }

  // Step 5: the rank policy is evaluated on B itself, never on B^T B.
  Eigen::JacobiSVD<Eigen::MatrixXd> svd(B, Eigen::ComputeThinU | Eigen::ComputeThinV);
  if (svd.singularValues().size() != 3 || !svd.singularValues().allFinite()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kSvdSpectrum;
    return result;
  }
  result.singular_values = svd.singularValues();
  result.singular_values_available = true;

  // Step 6: no ratio is available until the largest singular value passes.
  const double largest = result.singular_values(0);
  const double smallest = result.singular_values(2);
  if (!(largest > std::numeric_limits<double>::min())) {
    result.status = SchurReductionStatus::kRankDeficient;
    result.stage = SchurReductionStage::kLargestSingularValue;
    return result;
  }

  // The required diagnostic ratio is available whenever s_1 passes,
  // including for a numerical-rank rejection. The reducer does not impose a
  // process-wide floating-point rounding-mode precondition.
  const double rank_scale = static_cast<double>(std::max<Eigen::Index>(m, 3));
  const double numerical_floor = rank_scale * std::numeric_limits<double>::epsilon() * largest;
  const bool numerical_rank_deficient = !(smallest > numerical_floor);
  result.singular_ratio = smallest / largest;
  result.singular_ratio_available = true;

  // Step 7: equality at the numerical-rank floor is rejected.
  if (numerical_rank_deficient) {
    result.status = SchurReductionStatus::kRankDeficient;
    result.stage = SchurReductionStage::kNumericalRank;
    return result;
  }

  // Step 8: equality at the conditioning threshold is accepted.
  if (result.singular_ratio < kMinimumSingularRatio) {
    result.status = SchurReductionStatus::kIllConditioned;
    result.stage = SchurReductionStage::kConditioning;
    return result;
  }

  // The full m-row implicit Householder sequence is applied to [A b]. This is
  // deliberately not a thin-Q projection and not a normal-equation factor.
  Eigen::HouseholderQR<Eigen::MatrixXd> qr(B);
  if (!qr.matrixQR().allFinite() || !qr.hCoeffs().allFinite()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kQrFactors;
    return result;
  }

  Eigen::MatrixXd augmented(m, A.cols() + 1);
  augmented.leftCols(A.cols()) = A;
  augmented.col(A.cols()) = b;
  const Eigen::MatrixXd transformed = qr.householderQ().adjoint() * augmented;
  if (transformed.rows() != m || transformed.cols() != augmented.cols() || !transformed.allFinite()) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kQrTransform;
    return result;
  }

  const Eigen::Index q = m - 3;
  const Eigen::MatrixXd A_reduced_whitened = transformed.bottomRows(q).leftCols(A.cols());
  const Eigen::VectorXd residual_reduced_whitened = transformed.bottomRows(q).col(A.cols());

  // Emit the established OpenVINS unwhitened row convention. Keep these local
  // until all output/statistics checks succeed so rejected results are atomic.
  const Eigen::MatrixXd H_reduced = sigma_px * A_reduced_whitened;
  const Eigen::VectorXd residual_reduced = sigma_px * residual_reduced_whitened;
  const double noise_variance = sigma_px * sigma_px;
  if (!H_reduced.allFinite() || !residual_reduced.allFinite() || !std::isfinite(noise_variance) ||
      !(noise_variance > 0.0)) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kReducedOutputs;
    return result;
  }

  // Basis-invariant whitened sufficient statistics. Record the raw symmetry
  // error first, then apply the explicitly signed-off Lambda symmetrization.
  // This is not an eigenvalue repair and uses no normal-block inverse,
  // pseudo-inverse, damping, or clamping.
  const Eigen::MatrixXd raw_lambda = A_reduced_whitened.transpose() * A_reduced_whitened;
  const double raw_lambda_symmetry_error_inf =
      raw_lambda.size() == 0 ? 0.0 : (raw_lambda - raw_lambda.transpose()).cwiseAbs().rowwise().sum().maxCoeff();
  if (!raw_lambda.allFinite() || !std::isfinite(raw_lambda_symmetry_error_inf)) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kStatistics;
    return result;
  }

  // Symmetrization happens only after the unsymmetrized matrix and its raw
  // error have passed their own finite check. It is not a repair operation.
  const Eigen::MatrixXd lambda = 0.5 * (raw_lambda + raw_lambda.transpose());
  const Eigen::VectorXd eta = A_reduced_whitened.transpose() * residual_reduced_whitened;
  const double gamma = residual_reduced_whitened.squaredNorm();
  if (!lambda.allFinite() || !eta.allFinite() || !std::isfinite(gamma)) {
    result.status = SchurReductionStatus::kNonfinite;
    result.stage = SchurReductionStage::kStatistics;
    return result;
  }

  result.H_reduced = H_reduced;
  result.residual_reduced = residual_reduced;
  result.noise_variance = noise_variance;
  result.lambda = lambda;
  result.eta = eta;
  result.gamma = gamma;
  result.raw_lambda_symmetry_error_inf = raw_lambda_symmetry_error_inf;
  result.degrees_of_freedom = q;
  result.status = SchurReductionStatus::kAccepted;
  result.stage = SchurReductionStage::kAccepted;
  return result;
}

} // namespace ov_msckf
