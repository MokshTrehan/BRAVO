/*
 * Focused tests for offline camera-derived conditioning comparators.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "CameraConditioningComparators.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace {

using ov_msckf::conditioning::CameraSystem;
using ov_msckf::conditioning::Comparison;
using ov_msckf::conditioning::Method;
using ov_msckf::conditioning::MethodResult;

const MethodResult &find_method(const Comparison &comparison, Method method) {
  for (const MethodResult &result : comparison.methods) {
    if (result.method == method) {
      return result;
    }
  }
  throw std::runtime_error("method missing from comparison");
}

CameraSystem full_rank_system() {
  CameraSystem system;
  system.H_f.resize(8, 3);
  system.H_f << 1.0, 0.0, 0.0,
      0.0, 2.0, 0.0,
      0.0, 0.0, 3.0,
      0.5, -0.2, 0.1,
      -0.4, 0.7, 0.3,
      0.2, 0.1, -0.6,
      0.9, -0.5, 0.8,
      -0.3, 0.4, 0.2;
  system.H_x.resize(8, 5);
  system.H_x << 0.3, -0.2, 0.1, 0.0, 0.4,
      -0.1, 0.7, 0.3, -0.5, 0.2,
      0.5, 0.1, -0.6, 0.2, 0.3,
      0.2, -0.8, 0.4, 0.6, -0.1,
      -0.4, 0.3, 0.2, -0.7, 0.5,
      0.6, -0.1, 0.8, 0.3, -0.2,
      -0.7, 0.4, -0.3, 0.1, 0.9,
      0.1, 0.5, 0.7, -0.4, -0.6;
  system.residual.resize(8);
  system.residual << 0.1, -0.2, 0.05, 0.3, -0.4, 0.2, 0.15, -0.1;
  Eigen::Matrix<double, 5, 3> prior_factor;
  prior_factor << 0.5, 0.0, 0.0,
      0.1, 0.4, 0.0,
      -0.2, 0.1, 0.3,
      0.0, -0.1, 0.2,
      0.3, 0.2, -0.1;
  system.P_active = prior_factor * prior_factor.transpose();
  system.sigma_px = 1.5;
  return system;
}

CameraSystem exact_diagonal_spectrum(double smallest) {
  CameraSystem system;
  system.H_f = Eigen::MatrixXd::Zero(6, 3);
  system.H_f(0, 0) = 1.0;
  system.H_f(1, 1) = 0.25;
  system.H_f(2, 2) = smallest;
  system.H_x = Eigen::MatrixXd::Identity(6, 6);
  system.residual = Eigen::VectorXd::LinSpaced(6, 0.1, 0.6);
  system.P_active = Eigen::MatrixXd::Identity(6, 6);
  system.sigma_px = 1.0;
  return system;
}

TEST(CameraConditioningComparators, FullRankMethodsMatchIndependentOraclesWithPsdPrior) {
  const CameraSystem system = full_rank_system();
  const Comparison comparison = ov_msckf::conditioning::evaluate_all(system);

  ASSERT_FALSE(comparison.caller_input_mutated);
  ASSERT_TRUE(comparison.oracle_agreement.oracle_safe_opportunity);
  ASSERT_EQ(comparison.methods.size(), 7U);
  for (const Method method : {Method::kUpstreamNullspace,
                              Method::kLocalNullspace,
                              Method::kGuardedNullspaceDrop,
                              Method::kRankAwareNullspace,
                              Method::kLocalSchur,
                              Method::kFullJointOracle,
                              Method::kFullUNullspaceOracle}) {
    const MethodResult &result = find_method(comparison, method);
    ASSERT_TRUE(result.accepted) << ov_msckf::conditioning::method_name(method)
                                 << " " << result.status << "/" << result.reason;
    EXPECT_TRUE(result.finite);
    EXPECT_FALSE(result.harmful_accepted);
    EXPECT_LE(result.state_increment_relative_error, 1.0e-10);
    EXPECT_LE(result.posterior_covariance_relative_error, 1.0e-10);
    EXPECT_LE(result.nis_relative_error, 1.0e-10);
    EXPECT_FALSE(result.scaled_psd_failure);
  }
  const MethodResult &full_joint =
      find_method(comparison, Method::kFullJointOracle);
  EXPECT_DOUBLE_EQ(full_joint.state_increment_relative_error, 0.0);
  EXPECT_DOUBLE_EQ(full_joint.posterior_covariance_relative_error, 0.0);
  EXPECT_DOUBLE_EQ(full_joint.nis_relative_error, 0.0);
}

TEST(CameraConditioningComparators, RankTwoSystemSeparatesFixedDropFromRankAwareRetention) {
  CameraSystem system = exact_diagonal_spectrum(0.0);
  // Row two is part of the true left nullspace for rank two, but the fixed
  // production nullspace routine discards three rows unconditionally.
  system.H_x.setZero();
  system.H_x(2, 0) = 8.0;
  system.H_x(3, 1) = 0.5;
  system.H_x(4, 2) = -0.25;
  system.H_x(5, 3) = 0.75;
  system.residual.setZero();
  system.residual(2) = 5.0;
  system.residual(3) = 0.1;
  system.residual(4) = -0.2;
  system.residual(5) = 0.3;

  const Comparison comparison = ov_msckf::conditioning::evaluate_all(system);
  EXPECT_EQ(comparison.rank.numerical_rank, 2);
  EXPECT_FALSE(comparison.rank.frozen_guard_accepts);

  const MethodResult &upstream =
      find_method(comparison, Method::kUpstreamNullspace);
  const MethodResult &guarded =
      find_method(comparison, Method::kGuardedNullspaceDrop);
  const MethodResult &schur = find_method(comparison, Method::kLocalSchur);
  const MethodResult &rank_aware =
      find_method(comparison, Method::kRankAwareNullspace);

  EXPECT_TRUE(upstream.accepted);
  EXPECT_TRUE(upstream.harmful_accepted);
  EXPECT_TRUE(comparison.unguarded_nullspace_harmful);
  EXPECT_FALSE(guarded.accepted);
  EXPECT_FALSE(schur.accepted);
  ASSERT_TRUE(rank_aware.accepted);
  EXPECT_FALSE(rank_aware.harmful_accepted);
  EXPECT_NEAR(rank_aware.information.supported_subspace_trace, 64.875, 1.0e-10);
}

TEST(CameraConditioningComparators, FrozenGuardAcceptsConditionEqualityAndRejectsRankFloorEquality) {
  const CameraSystem condition_boundary =
      exact_diagonal_spectrum(ov_msckf::conditioning::kFrozenMinimumSingularRatio);
  const auto accepted =
      ov_msckf::conditioning::evaluate_frozen_rank_guard(condition_boundary);
  ASSERT_TRUE(accepted.singular_ratio_available);
  EXPECT_DOUBLE_EQ(accepted.singular_ratio,
                   ov_msckf::conditioning::kFrozenMinimumSingularRatio);
  EXPECT_TRUE(accepted.frozen_guard_accepts);

  const double rank_floor =
      6.0 * std::numeric_limits<double>::epsilon();
  const CameraSystem rank_boundary = exact_diagonal_spectrum(rank_floor);
  const auto rejected =
      ov_msckf::conditioning::evaluate_frozen_rank_guard(rank_boundary);
  ASSERT_TRUE(rejected.singular_ratio_available);
  EXPECT_DOUBLE_EQ(rejected.numerical_rank_floor, rank_floor);
  EXPECT_FALSE(rejected.frozen_guard_accepts);
  EXPECT_EQ(rejected.guard_status, "rank_deficient");
  EXPECT_EQ(rejected.guard_stage, "numerical_rank");
}

TEST(CameraConditioningComparators, FrozenHarmPredicateKeepsAllSixClausesIndependent) {
  MethodResult safe;
  safe.accepted = true;
  safe.finite = true;
  safe.state_increment_relative_error = 0.0;
  safe.posterior_covariance_relative_error = 0.0;
  safe.nis_relative_error = 0.0;
  EXPECT_FALSE(ov_msckf::conditioning::frozen_harmful_acceptance(safe));

  MethodResult nonfinite = safe;
  nonfinite.finite = false;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(nonfinite));

  MethodResult state_error = safe;
  state_error.state_increment_relative_error = 1.0e-3 + 1.0e-12;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(state_error));

  MethodResult covariance_error = safe;
  covariance_error.posterior_covariance_relative_error = 1.0e-3 + 1.0e-12;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(covariance_error));

  MethodResult psd = safe;
  psd.scaled_psd_failure = true;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(psd));

  MethodResult nis = safe;
  nis.nis_relative_error = 1.0e-3 + 1.0e-12;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(nis));

  MethodResult mutation = safe;
  mutation.unexplained_input_mutation = true;
  EXPECT_TRUE(ov_msckf::conditioning::frozen_harmful_acceptance(mutation));

  MethodResult equality = safe;
  equality.state_increment_relative_error = 1.0e-3;
  equality.posterior_covariance_relative_error = 1.0e-3;
  equality.nis_relative_error = 1.0e-3;
  EXPECT_FALSE(ov_msckf::conditioning::frozen_harmful_acceptance(equality));

  MethodResult rejected = nonfinite;
  rejected.accepted = false;
  EXPECT_FALSE(ov_msckf::conditioning::frozen_harmful_acceptance(rejected));
}

TEST(CameraConditioningComparators, LocalAndUpstreamLabelsCallTheSamePinnedRoutine) {
  const Comparison comparison =
      ov_msckf::conditioning::evaluate_all(full_rank_system());
  const MethodResult &upstream =
      find_method(comparison, Method::kUpstreamNullspace);
  const MethodResult &local =
      find_method(comparison, Method::kLocalNullspace);
  ASSERT_TRUE(upstream.accepted);
  ASSERT_TRUE(local.accepted);
  EXPECT_EQ(upstream.H_reduced.rows(), local.H_reduced.rows());
  EXPECT_EQ(upstream.H_reduced.cols(), local.H_reduced.cols());
  EXPECT_EQ(0, std::memcmp(upstream.H_reduced.data(), local.H_reduced.data(),
                           static_cast<std::size_t>(upstream.H_reduced.size()) *
                               sizeof(double)));
  EXPECT_EQ(0, std::memcmp(upstream.residual_reduced.data(),
                           local.residual_reduced.data(),
                           static_cast<std::size_t>(upstream.residual_reduced.size()) *
                               sizeof(double)));
}

} // namespace
