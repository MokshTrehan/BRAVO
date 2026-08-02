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

#include "UpdaterMSCKFPreview.h"

#include "state/State.h"
#include "state/StateHelper.h"
#include "types/Type.h"

#include <Eigen/Cholesky>

#include <cstddef>
#include <vector>

namespace ov_msckf {

const char *msckf_update_preview_status_name(MSCKFUpdatePreviewStatus status) noexcept {
  switch (status) {
  case MSCKFUpdatePreviewStatus::kAccepted:
    return "accepted";
  case MSCKFUpdatePreviewStatus::kInvalidInput:
    return "invalid_input";
  case MSCKFUpdatePreviewStatus::kNonfinite:
    return "nonfinite";
  case MSCKFUpdatePreviewStatus::kFactorizationFailed:
    return "factorization_failed";
  case MSCKFUpdatePreviewStatus::kNegativeDiagonal:
    return "negative_diagonal";
  }
  return "unknown";
}

const char *msckf_update_preview_stage_name(MSCKFUpdatePreviewStage stage) noexcept {
  switch (stage) {
  case MSCKFUpdatePreviewStage::kState:
    return "state";
  case MSCKFUpdatePreviewStage::kInputDimensions:
    return "input_dimensions";
  case MSCKFUpdatePreviewStage::kStateOrder:
    return "state_order";
  case MSCKFUpdatePreviewStage::kRawInputs:
    return "raw_inputs";
  case MSCKFUpdatePreviewStage::kCrossCovariance:
    return "cross_covariance";
  case MSCKFUpdatePreviewStage::kMarginalCovariance:
    return "marginal_covariance";
  case MSCKFUpdatePreviewStage::kInnovation:
    return "innovation";
  case MSCKFUpdatePreviewStage::kInnovationFactorization:
    return "innovation_factorization";
  case MSCKFUpdatePreviewStage::kInnovationInverse:
    return "innovation_inverse";
  case MSCKFUpdatePreviewStage::kKalmanGain:
    return "kalman_gain";
  case MSCKFUpdatePreviewStage::kPosteriorCovariance:
    return "posterior_covariance";
  case MSCKFUpdatePreviewStage::kPosteriorDiagonal:
    return "posterior_diagonal";
  case MSCKFUpdatePreviewStage::kStateIncrement:
    return "state_increment";
  case MSCKFUpdatePreviewStage::kAccepted:
    return "accepted";
  }
  return "unknown";
}

MSCKFUpdatePreviewResult UpdaterMSCKFPreview::Compute(const std::shared_ptr<State> &state,
                                                      const std::vector<std::shared_ptr<ov_type::Type>> &H_order,
                                                      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                                                      const Eigen::MatrixXd &R) {
  MSCKFUpdatePreviewResult result;
  result.diagnostics.measurement_dimension = residual.rows();
  result.diagnostics.jacobian_dimension = H.cols();

  if (!state) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kState;
    return result;
  }

  // This adapter is the only live-object surface. It takes owning value
  // copies, records the exact StateHelper block orders, and delegates all
  // validation and arithmetic to the pointer-free kernel below.
  MSCKFUpdatePreviewSnapshot snapshot;
  snapshot.covariance = StateHelper::get_full_covariance(state);
  snapshot.state_blocks.reserve(state->_variables.size());
  for (const auto &variable : state->_variables) {
    if (!variable) {
      snapshot.state_blocks.push_back({-1, 0, -1});
      continue;
    }
    snapshot.state_blocks.push_back({variable->id(), variable->size(), variable->id()});
  }

  std::vector<MSCKFUpdatePreviewBlock> jacobian_layout;
  jacobian_layout.reserve(H_order.size());
  Eigen::Index ordered_column = 0;
  for (const auto &variable : H_order) {
    if (!variable) {
      jacobian_layout.push_back({-1, 0, ordered_column});
      continue;
    }
    jacobian_layout.push_back({variable->id(), variable->size(), ordered_column});
    ordered_column += variable->size();
  }

  return ComputeFromSnapshot(snapshot, jacobian_layout, H, residual, R);
}

MSCKFUpdatePreviewResult UpdaterMSCKFPreview::ComputeFromSnapshot(
    const MSCKFUpdatePreviewSnapshot &snapshot,
    const std::vector<MSCKFUpdatePreviewBlock> &jacobian_layout,
    const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
    const Eigen::MatrixXd &R) {
  MSCKFUpdatePreviewResult result;
  result.diagnostics.measurement_dimension = residual.rows();
  result.diagnostics.jacobian_dimension = H.cols();

  const Eigen::MatrixXd &P = snapshot.covariance;
  result.diagnostics.state_dimension = P.rows();

  const Eigen::Index n = P.rows();
  const Eigen::Index m = residual.rows();
  if (n <= 0 || m <= 0 || P.cols() != n || H.rows() != m || H.cols() <= 0 ||
      R.rows() != m || R.cols() != m || jacobian_layout.empty()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  // The owning snapshot must describe every covariance coordinate exactly
  // once, in increasing ID and offset order. This is the value-only analogue
  // of State::_variables and fixes the block loop order used by EKFUpdate.
  if (snapshot.state_blocks.empty()) {
    result.diagnostics.offending_order_index = 0;
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }
  Eigen::Index expected_state_offset = 0;
  Eigen::Index previous_covariance_id = -1;
  for (std::size_t block_index = 0; block_index < snapshot.state_blocks.size(); ++block_index) {
    const MSCKFUpdatePreviewBlock &block = snapshot.state_blocks[block_index];
    const bool invalid_range = block.covariance_id < 0 || block.size <= 0 || block.offset < 0 ||
                               block.offset > n - block.size || block.covariance_id != block.offset;
    const bool invalid_order = block.offset != expected_state_offset ||
                               (block_index > 0 && block.covariance_id <= previous_covariance_id);
    if (invalid_range || invalid_order) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(block_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }
    expected_state_offset += block.size;
    previous_covariance_id = block.covariance_id;
  }
  if (expected_state_offset != n) {
    result.diagnostics.offending_order_index =
        static_cast<Eigen::Index>(snapshot.state_blocks.size());
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
    return result;
  }

  // Validate every compressed-Jacobian block before any Eigen block access.
  // The layout may select a top-level state block or any contained
  // subvariable, but it must be injective in covariance coordinates and must
  // account for every column of H exactly.
  Eigen::Index ordered_column = 0;
  std::vector<unsigned char> covered(static_cast<std::size_t>(n), 0);
  for (std::size_t order_index = 0; order_index < jacobian_layout.size(); ++order_index) {
    const MSCKFUpdatePreviewBlock &block = jacobian_layout[order_index];
    if (block.covariance_id < 0 || block.size <= 0 || block.offset != ordered_column ||
        block.covariance_id > n - block.size || block.offset > H.cols() - block.size) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    bool contained_by_state_block = false;
    for (const MSCKFUpdatePreviewBlock &state_block : snapshot.state_blocks) {
      if (block.covariance_id >= state_block.offset &&
          block.covariance_id <= state_block.offset + state_block.size - block.size) {
        contained_by_state_block = true;
        break;
      }
    }
    if (!contained_by_state_block) {
      result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
      result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
      result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
      return result;
    }

    for (Eigen::Index covariance_offset = 0; covariance_offset < block.size; ++covariance_offset) {
      const std::size_t covariance_index =
          static_cast<std::size_t>(block.covariance_id + covariance_offset);
      if (covered[covariance_index] != 0) {
        result.diagnostics.offending_order_index = static_cast<Eigen::Index>(order_index);
        result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
        result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateOrder;
        return result;
      }
      covered[covariance_index] = 1;
    }
    ordered_column += block.size;
  }
  result.diagnostics.ordered_jacobian_dimension = ordered_column;
  if (ordered_column != H.cols()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kInvalidInput;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInputDimensions;
    return result;
  }

  if (!P.allFinite() || !H.allFinite() || !residual.allFinite() || !R.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kRawInputs;
    return result;
  }

  // Match StateHelper::EKFUpdate's block ordering exactly rather than relying
  // on a mathematically equivalent full GEMM. This retains the correction to
  // every directly unobserved state block correlated with the Jacobian layout
  // while also making the preflight's floating-point proposal track the live
  // commit.
  Eigen::MatrixXd M = Eigen::MatrixXd::Zero(n, m);
  for (const MSCKFUpdatePreviewBlock &state_block : snapshot.state_blocks) {
    Eigen::MatrixXd M_block = Eigen::MatrixXd::Zero(state_block.size, m);
    for (const MSCKFUpdatePreviewBlock &measurement_block : jacobian_layout) {
      M_block.noalias() += P.block(state_block.offset, measurement_block.covariance_id,
                                   state_block.size, measurement_block.size) *
                           H.block(0, measurement_block.offset, m,
                                   measurement_block.size)
                               .transpose();
    }
    M.block(state_block.offset, 0, state_block.size, m) = M_block;
  }
  if (!M.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kCrossCovariance;
    return result;
  }

  // Copy P_small in the exact Jacobian block layout used by EKFUpdate. Using
  // one owning covariance snapshot keeps M and S internally consistent.
  Eigen::MatrixXd P_small(H.cols(), H.cols());
  Eigen::Index row = 0;
  for (const MSCKFUpdatePreviewBlock &row_block : jacobian_layout) {
    Eigen::Index col = 0;
    for (const MSCKFUpdatePreviewBlock &column_block : jacobian_layout) {
      P_small.block(row, col, row_block.size, column_block.size) =
          P.block(row_block.covariance_id, column_block.covariance_id,
                  row_block.size, column_block.size);
      col += column_block.size;
    }
    row += row_block.size;
  }
  if (!P_small.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kMarginalCovariance;
    return result;
  }

  // Match StateHelper::EKFUpdate: only the upper triangle defines S and R.
  Eigen::MatrixXd S(m, m);
  S.triangularView<Eigen::Upper>() = H * P_small * H.transpose();
  S.triangularView<Eigen::Upper>() += R;
  if (!S.triangularView<Eigen::Upper>().toDenseMatrix().allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovation;
    return result;
  }

  Eigen::LLT<Eigen::MatrixXd, Eigen::Upper> innovation_llt(S);
  if (innovation_llt.info() != Eigen::Success) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovationFactorization;
    return result;
  }

  Eigen::MatrixXd Sinv = Eigen::MatrixXd::Identity(m, m);
  innovation_llt.solveInPlace(Sinv);
  if (innovation_llt.info() != Eigen::Success || !Sinv.allFinite()) {
    result.diagnostics.status = innovation_llt.info() == Eigen::Success ? MSCKFUpdatePreviewStatus::kNonfinite
                                                                        : MSCKFUpdatePreviewStatus::kFactorizationFailed;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kInnovationInverse;
    return result;
  }

  const Eigen::MatrixXd K = M * Sinv.selfadjointView<Eigen::Upper>();
  if (!K.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kKalmanGain;
    return result;
  }

  // Preserve the live updater's subtractive upper-triangle update and exact
  // upper-to-lower mirror convention. No Joseph form or symmetrization repair.
  Eigen::MatrixXd P_plus = P;
  P_plus.triangularView<Eigen::Upper>() -= K * M.transpose();
  P_plus = P_plus.selfadjointView<Eigen::Upper>();
  if (!P_plus.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kPosteriorCovariance;
    return result;
  }

  const Eigen::VectorXd posterior_diagonal = P_plus.diagonal();
  Eigen::Index minimum_diagonal_index = 0;
  result.diagnostics.minimum_posterior_diagonal = posterior_diagonal.minCoeff(&minimum_diagonal_index);
  result.diagnostics.minimum_posterior_diagonal_available = true;
  if (result.diagnostics.minimum_posterior_diagonal < 0.0) {
    result.diagnostics.offending_diagonal_index = minimum_diagonal_index;
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNegativeDiagonal;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kPosteriorDiagonal;
    return result;
  }

  // EKFUpdate computes the increment only after its covariance/diagonal check.
  const Eigen::VectorXd dx = K * residual;
  if (!dx.allFinite()) {
    result.diagnostics.status = MSCKFUpdatePreviewStatus::kNonfinite;
    result.diagnostics.stage = MSCKFUpdatePreviewStage::kStateIncrement;
    return result;
  }

  result.dx = dx;
  result.P_plus = P_plus;
  result.diagnostics.status = MSCKFUpdatePreviewStatus::kAccepted;
  result.diagnostics.stage = MSCKFUpdatePreviewStage::kAccepted;
  return result;
}

} // namespace ov_msckf
