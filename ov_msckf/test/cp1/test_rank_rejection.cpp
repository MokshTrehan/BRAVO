/*
 * SchurVIO-Lite CP1 landmark rank/conditioning rejection tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cp1_fixture_utils.h"

#include <gtest/gtest.h>

#include <Eigen/Dense>

#include <cmath>
#include <iostream>
#include <limits>

namespace {

TEST(CP1Schur, DegenerateLandmarksAreRejectedDeterministically) {
  schurvio_cp1::DeterministicRng rng(schurvio_cp1::kMasterSeed ^ 0x72616e6bULL);
  int rank_deficient = 0;
  int ill_conditioned = 0;

  for (int fixture = 0; fixture < 128; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int rows = rng.integer(8, 30);
    const int state_size = rng.integer(6, 24);
    const Eigen::MatrixXd state_jacobian = rng.matrix(rows, state_size, 0.2);
    const Eigen::VectorXd residual = rng.vector(rows, 0.5);
    Eigen::MatrixXd landmark_jacobian(rows, 3);
    schurvio_cp1::FactorStatus expected;

    if (fixture % 4 == 0) {
      landmark_jacobian.setZero();
      expected = schurvio_cp1::FactorStatus::kRankDeficient;
    } else if (fixture % 4 == 1) {
      landmark_jacobian.leftCols(2) = rng.matrix(rows, 2);
      landmark_jacobian.col(2) = 1.7 * landmark_jacobian.col(0) - 0.4 * landmark_jacobian.col(1);
      expected = schurvio_cp1::FactorStatus::kRankDeficient;
    } else {
      const Eigen::MatrixXd left = schurvio_cp1::orthonormal_columns(rng, rows, 3);
      const Eigen::MatrixXd right = schurvio_cp1::orthonormal_columns(rng, 3, 3);
      Eigen::Vector3d singular_values;
      if (fixture % 4 == 2) {
        singular_values << 1.0, 0.5, 0.0;
        expected = schurvio_cp1::FactorStatus::kRankDeficient;
      } else {
        singular_values << 1.0, 0.5, 1.0e-13;
        expected = schurvio_cp1::FactorStatus::kIllConditioned;
      }
      landmark_jacobian = left * singular_values.asDiagonal() * right.transpose();
    }

    const schurvio_cp1::SchurReduction first =
        schurvio_cp1::reduce_landmark(state_jacobian, landmark_jacobian, residual);
    const schurvio_cp1::SchurReduction second =
        schurvio_cp1::reduce_landmark(state_jacobian, landmark_jacobian, residual);
    EXPECT_EQ(first.status, expected) << schurvio_cp1::factor_status_name(first.status);
    EXPECT_EQ(second.status, first.status);
    EXPECT_TRUE(first.singular_values.allFinite());
    EXPECT_TRUE(second.singular_values.allFinite());
    EXPECT_EQ(first.lambda.size(), 0);
    EXPECT_EQ(first.eta.size(), 0);
    if (first.status == schurvio_cp1::FactorStatus::kRankDeficient) {
      ++rank_deficient;
    } else if (first.status == schurvio_cp1::FactorStatus::kIllConditioned) {
      ++ill_conditioned;
      EXPECT_LT(first.singular_ratio, schurvio_cp1::kLandmarkRelativeSingularFloor);
      EXPECT_GT(first.singular_ratio, 0.0);
    }
  }

  EXPECT_EQ(rank_deficient, 96);
  EXPECT_EQ(ill_conditioned, 32);
  std::cout << "CP1_RANK fixtures=128 seed=" << (schurvio_cp1::kMasterSeed ^ 0x72616e6bULL)
            << " rank_deficient=" << rank_deficient << " ill_conditioned=" << ill_conditioned << std::endl;
}

TEST(CP1Schur, BoundaryAndInvalidInputsHaveExplicitStatus) {
  schurvio_cp1::DeterministicRng rng(schurvio_cp1::kMasterSeed ^ 0x626f756e64617279ULL);
  const int rows = 12;
  const int state_size = 9;
  const Eigen::MatrixXd state_jacobian = rng.matrix(rows, state_size);
  const Eigen::VectorXd residual = rng.vector(rows);
  const Eigen::MatrixXd left = schurvio_cp1::orthonormal_columns(rng, rows, 3);
  const Eigen::MatrixXd right = schurvio_cp1::orthonormal_columns(rng, 3, 3);

  Eigen::MatrixXd exact_boundary = Eigen::MatrixXd::Zero(rows, 3);
  exact_boundary(0, 0) = 1.0;
  exact_boundary(1, 1) = 0.5;
  exact_boundary(2, 2) = schurvio_cp1::kLandmarkRelativeSingularFloor;
  const auto boundary = schurvio_cp1::reduce_landmark(state_jacobian, exact_boundary, residual);
  EXPECT_EQ(boundary.status, schurvio_cp1::FactorStatus::kAccepted);
  EXPECT_DOUBLE_EQ(boundary.singular_ratio, schurvio_cp1::kLandmarkRelativeSingularFloor);

  Eigen::Vector3d accepted_values;
  accepted_values << 1.0, 0.5, 2.0e-6;
  const auto accepted = schurvio_cp1::reduce_landmark(
      state_jacobian, left * accepted_values.asDiagonal() * right.transpose(), residual);
  EXPECT_EQ(accepted.status, schurvio_cp1::FactorStatus::kAccepted);

  Eigen::Vector3d rejected_values;
  rejected_values << 1.0, 0.5, 5.0e-7;
  const auto rejected = schurvio_cp1::reduce_landmark(
      state_jacobian, left * rejected_values.asDiagonal() * right.transpose(), residual);
  EXPECT_EQ(rejected.status, schurvio_cp1::FactorStatus::kIllConditioned);

  const auto too_short = schurvio_cp1::reduce_landmark(state_jacobian.topRows(3), left.topRows(3), residual.head(3));
  EXPECT_EQ(too_short.status, schurvio_cp1::FactorStatus::kInsufficientRows);

  const auto incompatible_dimensions =
      schurvio_cp1::reduce_landmark(state_jacobian.topRows(rows - 1), left, residual);
  EXPECT_EQ(incompatible_dimensions.status, schurvio_cp1::FactorStatus::kNonfinite);

  Eigen::MatrixXd nonfinite = left;
  nonfinite(0, 0) = std::numeric_limits<double>::quiet_NaN();
  const auto invalid = schurvio_cp1::reduce_landmark(state_jacobian, nonfinite, residual);
  EXPECT_EQ(invalid.status, schurvio_cp1::FactorStatus::kNonfinite);

  Eigen::MatrixXd largest_at_floor = Eigen::MatrixXd::Zero(rows, 3);
  largest_at_floor(0, 0) = std::numeric_limits<double>::min();
  const auto zero_scale = schurvio_cp1::reduce_landmark(state_jacobian, largest_at_floor, residual);
  EXPECT_EQ(zero_scale.status, schurvio_cp1::FactorStatus::kRankDeficient);

  const double numerical_rank_floor =
      static_cast<double>(rows) * std::numeric_limits<double>::epsilon();
  Eigen::MatrixXd exactly_numerical_rank_floor = Eigen::MatrixXd::Zero(rows, 3);
  exactly_numerical_rank_floor(0, 0) = 1.0;
  exactly_numerical_rank_floor(1, 1) = 0.5;
  exactly_numerical_rank_floor(2, 2) = numerical_rank_floor;
  const auto rank_floor =
      schurvio_cp1::reduce_landmark(state_jacobian, exactly_numerical_rank_floor, residual);
  EXPECT_EQ(rank_floor.status, schurvio_cp1::FactorStatus::kRankDeficient);

  Eigen::MatrixXd immediately_above_rank_floor = exactly_numerical_rank_floor;
  immediately_above_rank_floor(2, 2) =
      std::nextafter(numerical_rank_floor, std::numeric_limits<double>::infinity());
  const auto above_rank_floor =
      schurvio_cp1::reduce_landmark(state_jacobian, immediately_above_rank_floor, residual);
  EXPECT_EQ(above_rank_floor.status, schurvio_cp1::FactorStatus::kIllConditioned);
}

} // namespace
