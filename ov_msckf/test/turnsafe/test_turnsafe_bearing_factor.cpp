/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * Focused TurnSafe T1 rotation-bearing factor tests.
 */

#include "update/TurnSafeBearingFactor.h"

#include <gtest/gtest.h>

#include <Eigen/Geometry>

#include <boost/math/distributions/chi_squared.hpp>

#include <algorithm>
#include <cmath>
#include <limits>
#include <vector>

namespace {

using ov_msckf::TurnSafeBearingFactor;
using ov_msckf::TurnSafeBearingFactorInput;
using ov_msckf::TurnSafeBearingFactorResult;
using ov_msckf::TurnSafeBearingFactorStatus;
using ov_msckf::TurnSafeBearingGroupStatus;
using ov_msckf::TurnSafePairPoseCovariance;

Eigen::Matrix3d skew(const Eigen::Vector3d &value) {
  Eigen::Matrix3d result;
  result << 0.0, -value.z(), value.y(), value.z(), 0.0, -value.x(),
      -value.y(), value.x(), 0.0;
  return result;
}

Eigen::Matrix3d rotation(double angle, const Eigen::Vector3d &axis) {
  return Eigen::AngleAxisd(angle, axis.normalized()).toRotationMatrix();
}

// Exact rotation induced by PoseJPL/JPLQuat::update for one local dx. The JPL
// matrix convention turns the positive quaternion vector into Exp(-dx) to
// first order, and update left-multiplies it onto R_GtoI.
Eigen::Matrix3d repository_left_retract(const Eigen::Matrix3d &estimate,
                                        const Eigen::Vector3d &dx) {
  Eigen::Vector4d dq;
  dq << 0.5 * dx, 1.0;
  dq.normalize();
  const Eigen::Vector3d vector = dq.head<3>();
  const double scalar = dq(3);
  const Eigen::Matrix3d increment =
      (2.0 * scalar * scalar - 1.0) * Eigen::Matrix3d::Identity() -
      2.0 * scalar * skew(vector) +
      2.0 * vector * vector.transpose();
  return increment * estimate;
}

Eigen::Matrix<double, 3, 2> tangent_jacobian(
    const Eigen::Vector3d &bearing, double first_scale,
    double second_scale) {
  Eigen::Vector3d axis = Eigen::Vector3d::UnitX();
  if (std::fabs(bearing.y()) < std::fabs(bearing.x())) {
    axis = Eigen::Vector3d::UnitY();
  }
  if (std::fabs(bearing.z()) < std::fabs(bearing.dot(axis))) {
    axis = Eigen::Vector3d::UnitZ();
  }
  Eigen::Vector3d first = axis - bearing * bearing.dot(axis);
  first.normalize();
  const Eigen::Vector3d second = bearing.cross(first);
  Eigen::Matrix<double, 3, 2> jacobian;
  jacobian.col(0) = first_scale * first;
  jacobian.col(1) = second_scale * second;
  return jacobian;
}

TurnSafeBearingFactorInput general_input() {
  TurnSafeBearingFactorInput input;
  input.source_bearing = Eigen::Vector3d(0.21, -0.17, 0.962).normalized();
  input.target_bearing = Eigen::Vector3d(-0.12, 0.27, 0.955).normalized();
  input.source_bearing_raw_pixel_jacobian =
      tangent_jacobian(input.source_bearing, 0.004, 0.006);
  input.target_bearing_raw_pixel_jacobian =
      tangent_jacobian(input.target_bearing, 0.005, 0.007);
  input.current_R_GtoI_source =
      rotation(0.19, Eigen::Vector3d(0.3, -0.5, 0.8));
  input.current_R_GtoI_target =
      rotation(-0.27, Eigen::Vector3d(-0.7, 0.2, 0.4));
  input.fej_R_GtoI_source = input.current_R_GtoI_source;
  input.fej_R_GtoI_target = input.current_R_GtoI_target;
  input.fixed_R_ItoC =
      rotation(0.11, Eigen::Vector3d(0.4, 0.1, -0.6));
  return input;
}

void expect_matrix_near(const Eigen::MatrixXd &left,
                        const Eigen::MatrixXd &right, double tolerance) {
  ASSERT_EQ(left.rows(), right.rows());
  ASSERT_EQ(left.cols(), right.cols());
  EXPECT_LE((left - right).cwiseAbs().maxCoeff(), tolerance);
}

TEST(TurnSafeBearingFactor, PureRotationPredictionHasZeroResidual) {
  TurnSafeBearingFactorInput input = general_input();
  input.target_bearing =
      (input.fixed_R_ItoC * input.current_R_GtoI_target *
       input.current_R_GtoI_source.transpose() *
       input.fixed_R_ItoC.transpose() * input.source_bearing)
          .normalized();
  input.target_bearing_raw_pixel_jacobian =
      tangent_jacobian(input.target_bearing, 0.005, 0.007);

  const TurnSafeBearingFactorResult result =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(result.accepted())
      << ov_msckf::turnsafe_bearing_factor_status_name(result.status);
  EXPECT_LE(result.residual.norm(), 2.0e-15);
  EXPECT_EQ((result.H_direct_pose.block<2, 3>(0, 3).squaredNorm()), 0.0);
  EXPECT_EQ((result.H_direct_pose.block<2, 3>(0, 9).squaredNorm()), 0.0);
  EXPECT_EQ(
      (result.H_direct_pose_whitened.block<2, 3>(0, 3).squaredNorm()), 0.0);
  EXPECT_EQ(
      (result.H_direct_pose_whitened.block<2, 3>(0, 9).squaredNorm()), 0.0);
}

TEST(TurnSafeBearingFactor, CurrentResidualAndFejJacobianStaySplit) {
  const TurnSafeBearingFactorInput base_input = general_input();
  const TurnSafeBearingFactorResult base =
      TurnSafeBearingFactor::Evaluate(base_input);
  ASSERT_TRUE(base.accepted());

  TurnSafeBearingFactorInput current_changed = base_input;
  current_changed.current_R_GtoI_target = repository_left_retract(
      current_changed.current_R_GtoI_target,
      Eigen::Vector3d(0.03, -0.02, 0.01));
  const TurnSafeBearingFactorResult changed_current =
      TurnSafeBearingFactor::Evaluate(current_changed);
  ASSERT_TRUE(changed_current.accepted());
  EXPECT_GT((changed_current.residual - base.residual).norm(), 1.0e-4);
  expect_matrix_near(changed_current.H_direct_pose, base.H_direct_pose,
                     2.0e-15);

  TurnSafeBearingFactorInput fej_changed = base_input;
  fej_changed.fej_R_GtoI_target = repository_left_retract(
      fej_changed.fej_R_GtoI_target,
      Eigen::Vector3d(-0.04, 0.01, 0.02));
  const TurnSafeBearingFactorResult changed_fej =
      TurnSafeBearingFactor::Evaluate(fej_changed);
  ASSERT_TRUE(changed_fej.accepted());
  expect_matrix_near(changed_fej.residual, base.residual, 0.0);
  expect_matrix_near(changed_fej.Sigma_R, base.Sigma_R, 0.0);
  expect_matrix_near(changed_fej.B, base.B, 0.0);
  EXPECT_GT((changed_fej.H_direct_pose - base.H_direct_pose).norm(), 1.0e-4);
}

TEST(TurnSafeBearingFactor,
     AnalyticPositiveMeasurementJacobianIsNegativeResidualDerivative) {
  const TurnSafeBearingFactorInput input = general_input();
  const TurnSafeBearingFactorResult center =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(center.accepted());
  constexpr double epsilon = 1.0e-7;

  for (int clone = 0; clone < 2; ++clone) {
    for (int axis = 0; axis < 3; ++axis) {
      Eigen::Vector3d increment = Eigen::Vector3d::Zero();
      increment(axis) = epsilon;
      TurnSafeBearingFactorInput plus = input;
      TurnSafeBearingFactorInput minus = input;
      if (clone == 0) {
        plus.current_R_GtoI_source = repository_left_retract(
            input.current_R_GtoI_source, increment);
        minus.current_R_GtoI_source = repository_left_retract(
            input.current_R_GtoI_source, -increment);
      } else {
        plus.current_R_GtoI_target = repository_left_retract(
            input.current_R_GtoI_target, increment);
        minus.current_R_GtoI_target = repository_left_retract(
            input.current_R_GtoI_target, -increment);
      }
      const TurnSafeBearingFactorResult plus_result =
          TurnSafeBearingFactor::Evaluate(plus);
      const TurnSafeBearingFactorResult minus_result =
          TurnSafeBearingFactor::Evaluate(minus);
      ASSERT_TRUE(plus_result.accepted());
      ASSERT_TRUE(minus_result.accepted());
      const Eigen::Vector2d residual_derivative =
          (plus_result.residual - minus_result.residual) /
          (2.0 * epsilon);
      const Eigen::Vector2d prediction_derivative =
          (center.B.transpose() * plus_result.current_R_ab *
               input.source_bearing -
           center.B.transpose() * minus_result.current_R_ab *
               input.source_bearing) /
          (2.0 * epsilon);
      const int column = clone == 0 ? axis : 6 + axis;
      expect_matrix_near(residual_derivative,
                         -center.H_direct_pose.col(column), 2.0e-8);
      expect_matrix_near(prediction_derivative,
                         center.H_direct_pose.col(column), 2.0e-8);
    }
  }
}

TEST(TurnSafeBearingFactor,
     CommonGlobalRotationIsInvariantAndGaugeDirectionIsAnnihilated) {
  const TurnSafeBearingFactorInput input = general_input();
  const TurnSafeBearingFactorResult baseline =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(baseline.accepted());

  const Eigen::Matrix3d global =
      rotation(0.37, Eigen::Vector3d(-0.2, 0.9, 0.3));
  TurnSafeBearingFactorInput transformed = input;
  transformed.current_R_GtoI_source *= global.transpose();
  transformed.current_R_GtoI_target *= global.transpose();
  transformed.fej_R_GtoI_source *= global.transpose();
  transformed.fej_R_GtoI_target *= global.transpose();
  const TurnSafeBearingFactorResult invariant =
      TurnSafeBearingFactor::Evaluate(transformed);
  ASSERT_TRUE(invariant.accepted());
  expect_matrix_near(invariant.B, baseline.B, 0.0);
  expect_matrix_near(invariant.current_R_ab, baseline.current_R_ab, 5.0e-16);
  expect_matrix_near(invariant.residual, baseline.residual, 5.0e-16);
  expect_matrix_near(invariant.H_direct_pose, baseline.H_direct_pose, 6.0e-16);

  const Eigen::Matrix<double, 2, 3> gauge =
      baseline.H_direct_pose.block<2, 3>(0, 0) *
          input.fej_R_GtoI_source +
      baseline.H_direct_pose.block<2, 3>(0, 6) *
          input.fej_R_GtoI_target;
  EXPECT_LE(gauge.norm(), 8.0e-16);
}

TEST(TurnSafeBearingFactor, TangentBasisUsesFrozenAxisTieBreaks) {
  TurnSafeBearingFactorInput input;
  input.source_bearing = Eigen::Vector3d::UnitZ();
  input.target_bearing = Eigen::Vector3d::UnitZ();
  input.source_bearing_raw_pixel_jacobian.leftCols<2>() <<
      1.0, 0.0, 0.0, 1.0, 0.0, 0.0;
  input.target_bearing_raw_pixel_jacobian =
      input.source_bearing_raw_pixel_jacobian;
  const TurnSafeBearingFactorResult z = TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(z.accepted());
  expect_matrix_near(z.B.col(0), Eigen::Vector3d::UnitX(), 0.0);
  expect_matrix_near(z.B.col(1), Eigen::Vector3d::UnitY(), 0.0);

  input.source_bearing = Eigen::Vector3d(1.0, 1.0, 1.0).normalized();
  input.target_bearing = input.source_bearing;
  input.source_bearing_raw_pixel_jacobian =
      tangent_jacobian(input.source_bearing, 1.0, 1.0);
  input.target_bearing_raw_pixel_jacobian =
      input.source_bearing_raw_pixel_jacobian;
  const TurnSafeBearingFactorResult tied_first =
      TurnSafeBearingFactor::Evaluate(input);
  const TurnSafeBearingFactorResult tied_second =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(tied_first.accepted());
  ASSERT_TRUE(tied_second.accepted());
  expect_matrix_near(tied_first.B, tied_second.B, 0.0);
  Eigen::Vector3d expected_v1 = Eigen::Vector3d::UnitX() -
      input.target_bearing * input.target_bearing.x();
  expected_v1.normalize();
  expect_matrix_near(tied_first.B.col(0), expected_v1, 2.0e-16);
  expect_matrix_near(tied_first.B.col(1),
                     input.target_bearing.cross(expected_v1), 2.0e-16);
}

TEST(TurnSafeBearingFactor, CovarianceAndLowerTriangularWhiteningAreExact) {
  TurnSafeBearingFactorInput input;
  input.source_bearing = Eigen::Vector3d::UnitZ();
  input.target_bearing = Eigen::Vector3d::UnitZ();
  input.source_bearing_raw_pixel_jacobian <<
      1.0, 0.0, 0.0, 1.0, 0.0, 0.0;
  input.target_bearing_raw_pixel_jacobian =
      input.source_bearing_raw_pixel_jacobian;
  const TurnSafeBearingFactorResult result =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(result.accepted());
  const double variance = 2.0 * 1.2 * 1.2;
  expect_matrix_near(result.Sigma_R,
                     variance * Eigen::Matrix2d::Identity(), 0.0);
  expect_matrix_near(result.whitening_lower,
                     std::sqrt(variance) * Eigen::Matrix2d::Identity(),
                     2.0e-16);
  expect_matrix_near(result.residual_whitened,
                     result.residual / std::sqrt(variance), 0.0);
  expect_matrix_near(result.H_direct_pose_whitened,
                     result.H_direct_pose / std::sqrt(variance), 0.0);

  input.source_bearing_raw_pixel_jacobian.setZero();
  input.target_bearing_raw_pixel_jacobian.setZero();
  const TurnSafeBearingFactorResult singular =
      TurnSafeBearingFactor::Evaluate(input);
  EXPECT_EQ(singular.status,
            TurnSafeBearingFactorStatus::
                kResidualCovarianceNotPositiveDefinite);
}

TEST(TurnSafeBearingFactor, GroupNisAndInformationAreCheckedAndFinite) {
  TurnSafeBearingFactorInput input = general_input();
  const TurnSafeBearingFactorResult factor =
      TurnSafeBearingFactor::Evaluate(input);
  ASSERT_TRUE(factor.accepted());
  std::vector<TurnSafeBearingFactorResult> factors(4U, factor);
  const TurnSafePairPoseCovariance zero_covariance =
      TurnSafePairPoseCovariance::Zero();
  const auto group =
      TurnSafeBearingFactor::EvaluateGroup(factors, zero_covariance);
  ASSERT_TRUE(group.accepted())
      << ov_msckf::turnsafe_bearing_group_status_name(group.status);
  EXPECT_EQ(group.factor_count, 4U);
  EXPECT_EQ(group.degrees_of_freedom, 8);
  EXPECT_NEAR(group.nis,
              4.0 * factor.residual_whitened.squaredNorm(), 1.0e-10);
  const boost::math::chi_squared distribution(8.0);
  EXPECT_DOUBLE_EQ(group.nis_threshold,
                   boost::math::quantile(distribution, 0.95));
  EXPECT_EQ(group.nis_passed, !(group.nis > group.nis_threshold));
  expect_matrix_near(group.I_rel,
                     group.stacked_H_relative_whitened.transpose() *
                         group.stacked_H_relative_whitened,
                     0.0);
  EXPECT_NEAR(group.information_trace, group.I_rel.trace(), 0.0);
  EXPECT_TRUE(group.information_non_negligible);
  EXPECT_TRUE(group.information_eigenvalues.allFinite());
  EXPECT_GE(group.information_eigenvalues.minCoeff(), 0.0);
  EXPECT_GE(group.relative_stack_singular_values(0),
            group.relative_stack_singular_values(1));
  EXPECT_GE(group.relative_stack_singular_values(1),
            group.relative_stack_singular_values(2));
  const Eigen::Vector3d squared_singular_values(
      group.relative_stack_singular_values(2) *
          group.relative_stack_singular_values(2),
      group.relative_stack_singular_values(1) *
          group.relative_stack_singular_values(1),
      group.relative_stack_singular_values(0) *
          group.relative_stack_singular_values(0));
  expect_matrix_near(group.information_eigenvalues,
                     squared_singular_values, 0.0);
  EXPECT_NEAR(group.information_eigenvalues.sum(),
              group.information_trace,
              64.0 * std::numeric_limits<double>::epsilon() *
                  std::max(1.0, std::fabs(group.information_trace)));
}

TEST(TurnSafeBearingFactor, GroupNisStressAndNonfiniteInputsFailClosed) {
  const TurnSafeBearingFactorResult factor =
      TurnSafeBearingFactor::Evaluate(general_input());
  ASSERT_TRUE(factor.accepted());
  std::vector<TurnSafeBearingFactorResult> factors(4U, factor);

  TurnSafePairPoseCovariance large =
      1.0e3 * TurnSafePairPoseCovariance::Identity();
  const auto large_group = TurnSafeBearingFactor::EvaluateGroup(factors, large);
  ASSERT_TRUE(large_group.accepted())
      << ov_msckf::turnsafe_bearing_group_status_name(large_group.status);
  EXPECT_TRUE(std::isfinite(large_group.nis));
  EXPECT_TRUE(large_group.nis_passed);

  std::vector<TurnSafeBearingFactorResult> outlying = factors;
  for (auto &member : outlying) {
    member.residual.setConstant(1.0e6);
  }
  const auto rejected = TurnSafeBearingFactor::EvaluateGroup(
      outlying, TurnSafePairPoseCovariance::Zero());
  ASSERT_TRUE(rejected.accepted());
  EXPECT_FALSE(rejected.nis_passed);
  EXPECT_GT(rejected.nis, rejected.nis_threshold);

  TurnSafePairPoseCovariance nonfinite =
      TurnSafePairPoseCovariance::Zero();
  nonfinite(0, 0) = std::numeric_limits<double>::infinity();
  EXPECT_EQ(TurnSafeBearingFactor::EvaluateGroup(factors, nonfinite).status,
            TurnSafeBearingGroupStatus::kNonfinitePairCovariance);

  TurnSafePairPoseCovariance asymmetric =
      TurnSafePairPoseCovariance::Identity();
  asymmetric(0, 1) = 1.0;
  EXPECT_EQ(TurnSafeBearingFactor::EvaluateGroup(factors, asymmetric).status,
            TurnSafeBearingGroupStatus::kPairCovarianceNotSymmetric);

  TurnSafePairPoseCovariance negative =
      -1.0e6 * TurnSafePairPoseCovariance::Identity();
  EXPECT_EQ(TurnSafeBearingFactor::EvaluateGroup(factors, negative).status,
            TurnSafeBearingGroupStatus::kInnovationNotPositiveDefinite);

  TurnSafeBearingFactorInput bad_input = general_input();
  bad_input.source_bearing(0) =
      std::numeric_limits<double>::quiet_NaN();
  const TurnSafeBearingFactorResult bad =
      TurnSafeBearingFactor::Evaluate(bad_input);
  EXPECT_EQ(bad.status, TurnSafeBearingFactorStatus::kNonfiniteInput);
  factors.front() = bad;
  EXPECT_EQ(TurnSafeBearingFactor::EvaluateGroup(
                factors, TurnSafePairPoseCovariance::Zero())
                .status,
            TurnSafeBearingGroupStatus::kInvalidFactor);
}

} // namespace
