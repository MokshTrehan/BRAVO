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

#include "CP2FeatureGate.h"

#include <Eigen/Cholesky>

#include <boost/math/distributions/chi_squared.hpp>

#include <cmath>
#include <limits>
#include <utility>

namespace ov_msckf {
namespace {

bool layout_is_valid(const CP2FeatureGateInput &input) {
  if (input.prior_covariance.rows() != input.prior_covariance.cols()) {
    return false;
  }

  Eigen::Index expected_H_offset = 0;
  for (std::size_t block_index = 0; block_index < input.layout.size(); ++block_index) {
    const CP2FeatureGateLayoutBlock &block = input.layout[block_index];
    if (block.size <= 0 || block.covariance_id < 0 || block.H_offset != expected_H_offset) {
      return false;
    }
    if (expected_H_offset > input.H.cols() || block.size > input.H.cols() - expected_H_offset) {
      return false;
    }
    if (block.covariance_id > input.prior_covariance.rows() ||
        block.size > input.prior_covariance.rows() - block.covariance_id) {
      return false;
    }

    // A raw layout may reorder prior blocks, but it may not duplicate or
    // overlap a covariance range. Validated bounds make both sums safe.
    const Eigen::Index block_end = block.covariance_id + block.size;
    for (std::size_t previous_index = 0; previous_index < block_index; ++previous_index) {
      const CP2FeatureGateLayoutBlock &previous = input.layout[previous_index];
      const Eigen::Index previous_end = previous.covariance_id + previous.size;
      if (block.covariance_id < previous_end && previous.covariance_id < block_end) {
        return false;
      }
    }
    expected_H_offset += block.size;
  }
  return expected_H_offset == input.H.cols();
}

} // namespace

const char *cp2_feature_gate_stage_name(CP2FeatureGateStage stage) noexcept {
  switch (stage) {
  case CP2FeatureGateStage::kReductionUnavailable:
    return "reduction_unavailable";
  case CP2FeatureGateStage::kInnovationNonfinite:
    return "innovation_nonfinite";
  case CP2FeatureGateStage::kFactorizationFailed:
    return "factorization_failed";
  case CP2FeatureGateStage::kSolveOrNISNonfinite:
    return "solve_or_nis_nonfinite";
  case CP2FeatureGateStage::kThresholdNonfinite:
    return "threshold_nonfinite";
  case CP2FeatureGateStage::kDecision:
    return "decision";
  }
  return "unknown";
}

CP2FeatureGateInput::CP2FeatureGateInput(Eigen::MatrixXd H_, Eigen::VectorXd residual_,
                                         Eigen::MatrixXd prior_covariance_,
                                         std::vector<CP2FeatureGateLayoutBlock> layout_, double sigma_px_sq_,
                                         double chi2_multiplier_, bool evidence_reduction_available_)
    : emitted_system_available(true), evidence_reduction_available(evidence_reduction_available_),
      H(std::move(H_)), residual(std::move(residual_)), prior_covariance(std::move(prior_covariance_)),
      layout(std::move(layout_)), sigma_px_sq(sigma_px_sq_), chi2_multiplier(chi2_multiplier_) {}

CP2FeatureGateInput CP2FeatureGateInput::ReductionUnavailable() { return CP2FeatureGateInput(); }

CP2FeatureGateInput CP2FeatureGateInput::EmittedEvidenceUnavailable(
    Eigen::MatrixXd H_, Eigen::VectorXd residual_, Eigen::MatrixXd prior_covariance_,
    std::vector<CP2FeatureGateLayoutBlock> layout_, double sigma_px_sq_, double chi2_multiplier_) {
  return CP2FeatureGateInput(std::move(H_), std::move(residual_), std::move(prior_covariance_), std::move(layout_),
                             sigma_px_sq_, chi2_multiplier_, false);
}

CP2FeatureGateResult CP2FeatureGate::Evaluate(CP2FeatureGateInput input,
                                               const ChiSquaredTable &chi_squared_table) {
  CP2FeatureGateResult result;
  result.degrees_of_freedom = input.residual.rows();
  if (!input.emitted_system_available) {
    return result;
  }

  // The baseline Givens path can emit the exact production gate operands even
  // when an observational gamma/statistics check makes its CP2 reduction
  // unavailable as evidence. In that case the evidence stage stays at the
  // first-precedence value while lifecycle still executes this gate once.
  const bool evidence_reduction_available = input.evidence_reduction_available;

  // Layout/range validation is required before Eigen block copies. It is the
  // defensive representation check for an innovation that cannot be formed.
  if (!layout_is_valid(input)) {
    result.stage = evidence_reduction_available ? CP2FeatureGateStage::kInnovationNonfinite
                                                : CP2FeatureGateStage::kReductionUnavailable;
    return result;
  }

  // Construct the exact marginal in raw-layout order from one immutable full
  // prior snapshot; callers cannot inject an independently assembled P_marg.
  Eigen::MatrixXd P_marg = Eigen::MatrixXd::Zero(input.H.cols(), input.H.cols());
  for (const CP2FeatureGateLayoutBlock &row_block : input.layout) {
    for (const CP2FeatureGateLayoutBlock &column_block : input.layout) {
      P_marg.block(row_block.H_offset, column_block.H_offset, row_block.size, column_block.size) =
          input.prior_covariance.block(row_block.covariance_id, column_block.covariance_id, row_block.size,
                                       column_block.size);
    }
  }

  // Frozen production Eigen expression order. Shape and finite observations
  // intentionally occur only after these two expressions have been evaluated.
  Eigen::MatrixXd S = input.H * P_marg * input.H.transpose();
  S.diagonal() += input.sigma_px_sq * Eigen::VectorXd::Ones(S.rows());
  if (input.residual.rows() <= 0 || input.H.rows() != input.residual.rows() || P_marg.rows() != input.H.cols() ||
      P_marg.cols() != input.H.cols() || S.rows() != input.residual.rows() ||
      S.cols() != input.residual.rows() || !input.H.allFinite() || !input.residual.allFinite() ||
      !P_marg.allFinite() || !S.allFinite()) {
    result.stage = evidence_reduction_available ? CP2FeatureGateStage::kInnovationNonfinite
                                                : CP2FeatureGateStage::kReductionUnavailable;
    return result;
  }

  Eigen::LLT<Eigen::MatrixXd> gate_factor(S);
  if (gate_factor.info() != Eigen::Success) {
    result.stage = evidence_reduction_available ? CP2FeatureGateStage::kFactorizationFailed
                                                : CP2FeatureGateStage::kReductionUnavailable;
    return result;
  }

  // Both operations are evaluated before any post-solve observation.
  const Eigen::VectorXd solved_residual = gate_factor.solve(input.residual);
  const double chi2 = input.residual.dot(solved_residual);
  if (gate_factor.info() != Eigen::Success || !solved_residual.allFinite() || !std::isfinite(chi2)) {
    result.stage = evidence_reduction_available ? CP2FeatureGateStage::kSolveOrNISNonfinite
                                                : CP2FeatureGateStage::kReductionUnavailable;
    return result;
  }
  if (evidence_reduction_available) {
    result.chi2_available = true;
    result.chi2 = chi2;
  }

  double chi2_check;
  if (input.residual.rows() < 500) {
    const ChiSquaredTable::const_iterator entry = chi_squared_table.find(static_cast<int>(input.residual.rows()));
    chi2_check = entry == chi_squared_table.end() ? std::numeric_limits<double>::quiet_NaN() : entry->second;
  } else {
    boost::math::chi_squared chi_squared_dist(input.residual.rows());
    chi2_check = boost::math::quantile(chi_squared_dist, 0.95);
  }

  // Exactly one binary64 multiplier operation and one unchanged production
  // comparison. The latter is not re-evaluated for the evidence decision.
  const double threshold = input.chi2_multiplier * chi2_check;
  const bool lifecycle_accept = !(chi2 > threshold);
  result.lifecycle_accept = lifecycle_accept;
  if (!std::isfinite(chi2_check) || !std::isfinite(threshold)) {
    result.stage = evidence_reduction_available ? CP2FeatureGateStage::kThresholdNonfinite
                                                : CP2FeatureGateStage::kReductionUnavailable;
    return result;
  }
  if (!evidence_reduction_available) {
    return result;
  }

  result.threshold_available = true;
  result.threshold = threshold;
  result.evidence_decision_available = true;
  result.evidence_accept = lifecycle_accept;
  result.stage = CP2FeatureGateStage::kDecision;
  return result;
}

} // namespace ov_msckf
