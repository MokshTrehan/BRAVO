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

#ifndef OV_MSCKF_CP2_FEATURE_GATE_H
#define OV_MSCKF_CP2_FEATURE_GATE_H

#include <Eigen/Core>

#include <cstddef>
#include <limits>
#include <map>
#include <vector>

namespace ov_msckf {

/// Frozen CP2 per-feature gate stages, in first-failure precedence order.
enum class CP2FeatureGateStage {
  kReductionUnavailable,
  kInnovationNonfinite,
  kFactorizationFailed,
  kSolveOrNISNonfinite,
  kThresholdNonfinite,
  kDecision,
};

/// Stable lower-case stage names for machine-readable CP2 evidence.
const char *cp2_feature_gate_stage_name(CP2FeatureGateStage stage) noexcept;

/**
 * @brief One ordered Jacobian-column block and its full-prior covariance range.
 *
 * H_offset is the destination column offset in the reduced Jacobian. Blocks
 * must cover those columns contiguously in vector order. covariance_id is the
 * source row/column offset in the immutable full prior snapshot.
 */
struct CP2FeatureGateLayoutBlock {
  CP2FeatureGateLayoutBlock() = default;
  CP2FeatureGateLayoutBlock(Eigen::Index covariance_id_, Eigen::Index size_, Eigen::Index H_offset_)
      : covariance_id(covariance_id_), size(size_), H_offset(H_offset_) {}

  Eigen::Index covariance_id = 0;
  Eigen::Index size = 0;
  Eigen::Index H_offset = 0;
};

/// Explicit CP2 no-repair/no-fallback accounting.
struct CP2FeatureGateCounters {
  std::size_t jitter_count = 0;
  std::size_t repair_count = 0;
  std::size_t alternate_solve_count = 0;
  std::size_t clamp_count = 0;
  std::size_t regularization_count = 0;
  std::size_t silent_fallback_count = 0;
  std::size_t fallback_count = 0;
};

/**
 * @brief Owning, value-only input for one CP2 feature gate evaluation.
 *
 * Passing this object by value gives the helper a stable prior snapshot and
 * prevents a live State or a caller-constructed marginal covariance from
 * entering the mathematical boundary.
 */
struct CP2FeatureGateInput {
  CP2FeatureGateInput(Eigen::MatrixXd H_, Eigen::VectorXd residual_, Eigen::MatrixXd prior_covariance_,
                      std::vector<CP2FeatureGateLayoutBlock> layout_, double sigma_px_sq_, double chi2_multiplier_,
                      bool evidence_reduction_available_ = true);

  /// Construct an unavailable reduction for which no emitted gate system exists.
  static CP2FeatureGateInput ReductionUnavailable();

  /// Preserve an emitted baseline gate system whose evidence reduction failed.
  static CP2FeatureGateInput EmittedEvidenceUnavailable(
      Eigen::MatrixXd H_, Eigen::VectorXd residual_, Eigen::MatrixXd prior_covariance_,
      std::vector<CP2FeatureGateLayoutBlock> layout_, double sigma_px_sq_, double chi2_multiplier_);

  bool emitted_system_available = false;
  bool evidence_reduction_available = false;
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  Eigen::MatrixXd prior_covariance;
  std::vector<CP2FeatureGateLayoutBlock> layout;
  double sigma_px_sq = std::numeric_limits<double>::quiet_NaN();
  double chi2_multiplier = std::numeric_limits<double>::quiet_NaN();

private:
  CP2FeatureGateInput() = default;
};

/**
 * @brief Exact evidence and live-lifecycle result for one per-feature gate.
 *
 * Evidence decisions are nullable through evidence_decision_available. The
 * lifecycle decision remains defined independently because a dynamically
 * nonfinite threshold must retain the production IEEE-754 comparison result.
 */
struct CP2FeatureGateResult {
  CP2FeatureGateStage stage = CP2FeatureGateStage::kReductionUnavailable;
  Eigen::Index degrees_of_freedom = 0;

  bool chi2_available = false;
  double chi2 = std::numeric_limits<double>::quiet_NaN();
  bool threshold_available = false;
  double threshold = std::numeric_limits<double>::quiet_NaN();

  bool evidence_decision_available = false;
  bool evidence_accept = false;
  bool lifecycle_accept = false;

  CP2FeatureGateCounters counters;
};

/**
 * @brief Shared strict-operation-order CP2 per-feature gate.
 *
 * This helper constructs P_marg itself from the full prior and ordered layout.
 * It performs no repair, alternate solve, clamping, regularization, fallback,
 * or second gate evaluation.
 */
class CP2FeatureGate {
public:
  using ChiSquaredTable = std::map<int, double>;

  static CP2FeatureGateResult Evaluate(CP2FeatureGateInput input, const ChiSquaredTable &chi_squared_table);
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_FEATURE_GATE_H
