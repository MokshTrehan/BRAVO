/*
 * SchurVIO-Lite CP2 value-only MSCKF preview kernel tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "state/State.h"
#include "state/StateHelper.h"
#include "types/Type.h"
#include "update/UpdaterMSCKFPreview.h"

#include <gtest/gtest.h>

#include <Eigen/Dense>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <vector>

namespace {

using ov_msckf::MSCKFUpdatePreviewBlock;
using ov_msckf::MSCKFUpdatePreviewDiagnostics;
using ov_msckf::MSCKFUpdatePreviewResult;
using ov_msckf::MSCKFUpdatePreviewSnapshot;
using ov_msckf::MSCKFUpdatePreviewStage;
using ov_msckf::MSCKFUpdatePreviewStatus;
using ov_msckf::UpdaterMSCKFPreview;

std::uint64_t binary64_bits(double value) noexcept {
  static_assert(sizeof(double) == sizeof(std::uint64_t), "CP2 requires binary64 doubles");
  std::uint64_t bits = 0;
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

template <typename LeftDerived, typename RightDerived>
bool coefficient_bits_equal(const Eigen::MatrixBase<LeftDerived> &left,
                            const Eigen::MatrixBase<RightDerived> &right) {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (binary64_bits(left(row, column)) != binary64_bits(right(row, column))) {
        return false;
      }
    }
  }
  return true;
}

void expect_zero_repair_counters(const MSCKFUpdatePreviewDiagnostics &diagnostics) {
  EXPECT_EQ(diagnostics.jitter_count, 0U);
  EXPECT_EQ(diagnostics.repair_count, 0U);
  EXPECT_EQ(diagnostics.alternate_solve_count, 0U);
  EXPECT_EQ(diagnostics.clamp_count, 0U);
  EXPECT_EQ(diagnostics.regularization_count, 0U);
  EXPECT_EQ(diagnostics.fallback_count, 0U);
}

void expect_exact_diagnostics(const MSCKFUpdatePreviewDiagnostics &left,
                              const MSCKFUpdatePreviewDiagnostics &right) {
  EXPECT_EQ(left.status, right.status);
  EXPECT_EQ(left.stage, right.stage);
  EXPECT_EQ(left.state_dimension, right.state_dimension);
  EXPECT_EQ(left.measurement_dimension, right.measurement_dimension);
  EXPECT_EQ(left.jacobian_dimension, right.jacobian_dimension);
  EXPECT_EQ(left.ordered_jacobian_dimension, right.ordered_jacobian_dimension);
  EXPECT_EQ(left.offending_order_index, right.offending_order_index);
  EXPECT_EQ(left.offending_diagonal_index, right.offending_diagonal_index);
  EXPECT_EQ(left.minimum_posterior_diagonal_available,
            right.minimum_posterior_diagonal_available);
  EXPECT_EQ(binary64_bits(left.minimum_posterior_diagonal),
            binary64_bits(right.minimum_posterior_diagonal));
  EXPECT_EQ(left.jitter_count, right.jitter_count);
  EXPECT_EQ(left.repair_count, right.repair_count);
  EXPECT_EQ(left.alternate_solve_count, right.alternate_solve_count);
  EXPECT_EQ(left.clamp_count, right.clamp_count);
  EXPECT_EQ(left.regularization_count, right.regularization_count);
  EXPECT_EQ(left.fallback_count, right.fallback_count);
}

MSCKFUpdatePreviewSnapshot scalar_snapshot(double covariance) {
  MSCKFUpdatePreviewSnapshot snapshot;
  snapshot.covariance = Eigen::MatrixXd::Constant(1, 1, covariance);
  snapshot.state_blocks = {{0, 1, 0}};
  return snapshot;
}

MSCKFUpdatePreviewResult scalar_preview(const MSCKFUpdatePreviewSnapshot &snapshot,
                                        double H_value, double residual_value,
                                        double R_value) {
  const std::vector<MSCKFUpdatePreviewBlock> layout{{0, 1, 0}};
  const Eigen::MatrixXd H = Eigen::MatrixXd::Constant(1, 1, H_value);
  const Eigen::VectorXd residual = Eigen::VectorXd::Constant(1, residual_value);
  const Eigen::MatrixXd R = Eigen::MatrixXd::Constant(1, 1, R_value);
  return UpdaterMSCKFPreview::ComputeFromSnapshot(snapshot, layout, H, residual, R);
}

TEST(CP2PreviewSnapshot, ExactAdapterParityAndInputImmutability) {
  ov_msckf::StateOptions options;
  options.do_fej = true;
  options.num_cameras = 0;
  auto state = std::make_shared<ov_msckf::State>(options);
  std::shared_ptr<ov_type::Type> clone =
      ov_msckf::StateHelper::clone(state, state->_imu->pose());

  const Eigen::Index dimension = state->max_covariance_size();
  ASSERT_EQ(dimension, 21);
  Eigen::MatrixXd root = Eigen::MatrixXd::Zero(dimension, dimension);
  for (Eigen::Index row = 0; row < dimension; ++row) {
    root(row, row) = 0.18 + 0.004 * static_cast<double>(row + 1);
    for (Eigen::Index column = 0; column < row; ++column) {
      root(row, column) =
          0.006 * std::sin(0.19 * static_cast<double>((row + 1) * (column + 2)));
    }
  }
  const Eigen::MatrixXd covariance = root * root.transpose();
  const std::vector<std::shared_ptr<ov_type::Type>> state_order{state->_imu, clone};
  ov_msckf::StateHelper::set_initial_covariance(state, covariance, state_order);

  const std::vector<std::shared_ptr<ov_type::Type>> H_order{state->_imu->q(), clone};
  Eigen::MatrixXd H(5, 9);
  for (Eigen::Index row = 0; row < H.rows(); ++row) {
    for (Eigen::Index column = 0; column < H.cols(); ++column) {
      H(row, column) = 0.015 *
                       std::sin(0.31 * static_cast<double>((row + 2) * (column + 1)));
    }
  }
  Eigen::VectorXd residual(5);
  residual << 0.11, -0.07, 0.035, -0.022, 0.014;
  const Eigen::MatrixXd R = 0.35 * Eigen::MatrixXd::Identity(5, 5);

  MSCKFUpdatePreviewSnapshot snapshot;
  snapshot.covariance = ov_msckf::StateHelper::get_full_covariance(state);
  snapshot.state_blocks = {{state->_imu->id(), state->_imu->size(), state->_imu->id()},
                           {clone->id(), clone->size(), clone->id()}};
  const std::vector<MSCKFUpdatePreviewBlock> jacobian_layout{
      {state->_imu->q()->id(), state->_imu->q()->size(), 0},
      {clone->id(), clone->size(), state->_imu->q()->size()}};

  const Eigen::MatrixXd snapshot_covariance_before = snapshot.covariance;
  const std::vector<MSCKFUpdatePreviewBlock> snapshot_blocks_before = snapshot.state_blocks;
  const std::vector<MSCKFUpdatePreviewBlock> jacobian_layout_before = jacobian_layout;
  const Eigen::MatrixXd H_before = H;
  const Eigen::VectorXd residual_before = residual;
  const Eigen::MatrixXd R_before = R;
  const Eigen::MatrixXd live_covariance_before =
      ov_msckf::StateHelper::get_full_covariance(state);

  const MSCKFUpdatePreviewResult value_only = UpdaterMSCKFPreview::ComputeFromSnapshot(
      snapshot, jacobian_layout, H, residual, R);
  const MSCKFUpdatePreviewResult adapter =
      UpdaterMSCKFPreview::Compute(state, H_order, H, residual, R);

  ASSERT_TRUE(value_only.accepted());
  ASSERT_TRUE(adapter.accepted());
  expect_exact_diagnostics(value_only.diagnostics, adapter.diagnostics);
  EXPECT_TRUE(coefficient_bits_equal(value_only.dx, adapter.dx));
  EXPECT_TRUE(coefficient_bits_equal(value_only.P_plus, adapter.P_plus));
  expect_zero_repair_counters(value_only.diagnostics);
  expect_zero_repair_counters(adapter.diagnostics);

  EXPECT_TRUE(coefficient_bits_equal(snapshot.covariance, snapshot_covariance_before));
  ASSERT_EQ(snapshot.state_blocks.size(), snapshot_blocks_before.size());
  for (std::size_t index = 0; index < snapshot.state_blocks.size(); ++index) {
    EXPECT_EQ(snapshot.state_blocks[index].covariance_id,
              snapshot_blocks_before[index].covariance_id);
    EXPECT_EQ(snapshot.state_blocks[index].size, snapshot_blocks_before[index].size);
    EXPECT_EQ(snapshot.state_blocks[index].offset, snapshot_blocks_before[index].offset);
  }
  ASSERT_EQ(jacobian_layout.size(), jacobian_layout_before.size());
  for (std::size_t index = 0; index < jacobian_layout.size(); ++index) {
    EXPECT_EQ(jacobian_layout[index].covariance_id,
              jacobian_layout_before[index].covariance_id);
    EXPECT_EQ(jacobian_layout[index].size, jacobian_layout_before[index].size);
    EXPECT_EQ(jacobian_layout[index].offset, jacobian_layout_before[index].offset);
  }
  EXPECT_TRUE(coefficient_bits_equal(H, H_before));
  EXPECT_TRUE(coefficient_bits_equal(residual, residual_before));
  EXPECT_TRUE(coefficient_bits_equal(R, R_before));
  EXPECT_TRUE(coefficient_bits_equal(ov_msckf::StateHelper::get_full_covariance(state),
                                     live_covariance_before));
}

TEST(CP2PreviewSnapshot, RejectsMalformedOwningStateLayout) {
  const Eigen::MatrixXd H = Eigen::MatrixXd::Identity(2, 2);
  const Eigen::VectorXd residual = Eigen::VectorXd::Ones(2);
  const Eigen::MatrixXd R = Eigen::MatrixXd::Identity(2, 2);
  const std::vector<MSCKFUpdatePreviewBlock> layout{{0, 2, 0}};

  MSCKFUpdatePreviewSnapshot identity_disconnect;
  identity_disconnect.covariance = Eigen::MatrixXd::Identity(4, 4);
  identity_disconnect.state_blocks = {{1, 2, 0}, {2, 2, 2}};
  const MSCKFUpdatePreviewResult disconnected =
      UpdaterMSCKFPreview::ComputeFromSnapshot(identity_disconnect, layout, H,
                                               residual, R);
  EXPECT_EQ(disconnected.diagnostics.status, MSCKFUpdatePreviewStatus::kInvalidInput);
  EXPECT_EQ(disconnected.diagnostics.stage, MSCKFUpdatePreviewStage::kStateOrder);
  EXPECT_EQ(disconnected.diagnostics.offending_order_index, 0);
  expect_zero_repair_counters(disconnected.diagnostics);

  MSCKFUpdatePreviewSnapshot gap;
  gap.covariance = Eigen::MatrixXd::Identity(4, 4);
  gap.state_blocks = {{0, 1, 0}, {2, 2, 2}};
  const MSCKFUpdatePreviewResult gap_result =
      UpdaterMSCKFPreview::ComputeFromSnapshot(gap, layout, H, residual, R);
  EXPECT_EQ(gap_result.diagnostics.status, MSCKFUpdatePreviewStatus::kInvalidInput);
  EXPECT_EQ(gap_result.diagnostics.stage, MSCKFUpdatePreviewStage::kStateOrder);
  EXPECT_EQ(gap_result.diagnostics.offending_order_index, 1);
  expect_zero_repair_counters(gap_result.diagnostics);
}

TEST(CP2PreviewSnapshot, RejectsMalformedValueOnlyJacobianLayout) {
  MSCKFUpdatePreviewSnapshot snapshot;
  snapshot.covariance = Eigen::MatrixXd::Identity(4, 4);
  snapshot.state_blocks = {{0, 2, 0}, {2, 2, 2}};
  const Eigen::MatrixXd H = Eigen::MatrixXd::Identity(4, 4);
  const Eigen::VectorXd residual = Eigen::VectorXd::Ones(4);
  const Eigen::MatrixXd R = Eigen::MatrixXd::Identity(4, 4);

  const std::vector<MSCKFUpdatePreviewBlock> duplicate_covariance_range{
      {0, 2, 0}, {0, 2, 2}};
  const MSCKFUpdatePreviewResult duplicate = UpdaterMSCKFPreview::ComputeFromSnapshot(
      snapshot, duplicate_covariance_range, H, residual, R);
  EXPECT_EQ(duplicate.diagnostics.status, MSCKFUpdatePreviewStatus::kInvalidInput);
  EXPECT_EQ(duplicate.diagnostics.stage, MSCKFUpdatePreviewStage::kStateOrder);
  EXPECT_EQ(duplicate.diagnostics.offending_order_index, 1);
  expect_zero_repair_counters(duplicate.diagnostics);

  const std::vector<MSCKFUpdatePreviewBlock> crossed_state_boundary{{1, 2, 0}};
  const Eigen::MatrixXd narrow_H = Eigen::MatrixXd::Ones(2, 2);
  const Eigen::VectorXd narrow_residual = Eigen::VectorXd::Ones(2);
  const Eigen::MatrixXd narrow_R = Eigen::MatrixXd::Identity(2, 2);
  const MSCKFUpdatePreviewResult crossed = UpdaterMSCKFPreview::ComputeFromSnapshot(
      snapshot, crossed_state_boundary, narrow_H, narrow_residual, narrow_R);
  EXPECT_EQ(crossed.diagnostics.status, MSCKFUpdatePreviewStatus::kInvalidInput);
  EXPECT_EQ(crossed.diagnostics.stage, MSCKFUpdatePreviewStage::kStateOrder);
  EXPECT_EQ(crossed.diagnostics.offending_order_index, 0);
  expect_zero_repair_counters(crossed.diagnostics);
}

TEST(CP2PreviewSnapshot, FiniteAndLLTBoundariesAreOrdered) {
  const MSCKFUpdatePreviewResult zero_posterior =
      scalar_preview(scalar_snapshot(1.0), 1.0, 0.25, 0.0);
  ASSERT_TRUE(zero_posterior.accepted());
  ASSERT_TRUE(zero_posterior.diagnostics.minimum_posterior_diagonal_available);
  EXPECT_EQ(binary64_bits(zero_posterior.P_plus(0, 0)), binary64_bits(0.0));
  EXPECT_EQ(binary64_bits(zero_posterior.dx(0)), binary64_bits(0.25));
  expect_zero_repair_counters(zero_posterior.diagnostics);

  const MSCKFUpdatePreviewResult singular_innovation =
      scalar_preview(scalar_snapshot(0.0), 1.0, 0.25, 0.0);
  EXPECT_EQ(singular_innovation.diagnostics.status,
            MSCKFUpdatePreviewStatus::kFactorizationFailed);
  EXPECT_EQ(singular_innovation.diagnostics.stage,
            MSCKFUpdatePreviewStage::kInnovationFactorization);
  expect_zero_repair_counters(singular_innovation.diagnostics);

  const MSCKFUpdatePreviewResult nonfinite_input = scalar_preview(
      scalar_snapshot(1.0), std::numeric_limits<double>::infinity(), 0.25, 1.0);
  EXPECT_EQ(nonfinite_input.diagnostics.status, MSCKFUpdatePreviewStatus::kNonfinite);
  EXPECT_EQ(nonfinite_input.diagnostics.stage, MSCKFUpdatePreviewStage::kRawInputs);
  expect_zero_repair_counters(nonfinite_input.diagnostics);
}

} // namespace
