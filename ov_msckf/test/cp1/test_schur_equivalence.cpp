/*
 * SchurVIO-Lite CP1 full/nullspace/Schur equivalence tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cp1_fixture_utils.h"
#include "update/SchurUpdate.h"

#include <gtest/gtest.h>

#include <Eigen/Cholesky>
#include <Eigen/SVD>

#include <algorithm>
#include <cstring>
#include <iostream>

namespace {

using schurvio_cp1::DeterministicRng;
using schurvio_cp1::FactorStatus;
using schurvio_cp1::SchurReduction;

static_assert(ov_msckf::SchurUpdate::kMinimumSingularRatio == schurvio_cp1::kLandmarkRelativeSingularFloor,
              "production and CP1 landmark-conditioning contracts must agree");

TEST(CP1Schur, FullJointNullspaceAndReducedSystemsAgree) {
  DeterministicRng rng(schurvio_cp1::kMasterSeed);
  double worst_state_error = 0.0;
  double worst_landmark_error = 0.0;
  double worst_covariance_error = 0.0;
  double worst_nis_error = 0.0;
  double worst_lambda_error = 0.0;
  double worst_eta_error = 0.0;
  double worst_gamma_error = 0.0;
  double worst_symmetry_ratio = 0.0;
  double minimum_normalized_eigenvalue = 1.0;

  for (int fixture = 0; fixture < 128; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int state_size = rng.integer(6, 30);
    const int measurement_size = rng.integer(8, 30);

    Eigen::MatrixXd prior_sqrt_information = 0.04 * rng.matrix(state_size, state_size);
    for (int index = 0; index < state_size; ++index) {
      prior_sqrt_information(index, index) += 1.5 + 0.5 * rng.uniform();
    }
    const Eigen::MatrixXd prior_information = prior_sqrt_information.transpose() * prior_sqrt_information;
    const Eigen::MatrixXd landmark_jacobian = schurvio_cp1::well_conditioned_landmark_jacobian(rng, measurement_size);
    Eigen::MatrixXd state_jacobian;
    Eigen::VectorXd residual;
    if (fixture % 8 == 0) {
      // Exercise cancellation-sensitive statistics when most state and
      // residual energy lies in the eliminated landmark column space.
      state_jacobian = landmark_jacobian * rng.matrix(3, state_size, 0.25) +
                       rng.matrix(measurement_size, state_size, 1.0e-5);
      residual = landmark_jacobian * rng.vector(3, 0.5) + rng.vector(measurement_size, 1.0e-5);
    } else {
      state_jacobian = rng.matrix(measurement_size, state_size, 0.25);
      residual = rng.vector(measurement_size, 0.5);
    }

    const Eigen::MatrixXd state_jacobian_before = state_jacobian;
    const Eigen::MatrixXd landmark_jacobian_before = landmark_jacobian;
    const Eigen::VectorXd residual_before = residual;
    constexpr double sigma_px = 1.0;
    const ov_msckf::SchurReductionResult production =
        ov_msckf::SchurUpdate::Reduce(state_jacobian, landmark_jacobian, residual, sigma_px);

    EXPECT_EQ(std::memcmp(state_jacobian.data(), state_jacobian_before.data(),
                          static_cast<std::size_t>(state_jacobian.size()) * sizeof(double)),
              0);
    EXPECT_EQ(std::memcmp(landmark_jacobian.data(), landmark_jacobian_before.data(),
                          static_cast<std::size_t>(landmark_jacobian.size()) * sizeof(double)),
              0);
    EXPECT_EQ(std::memcmp(residual.data(), residual_before.data(),
                          static_cast<std::size_t>(residual.size()) * sizeof(double)),
              0);

    ASSERT_TRUE(production.accepted())
        << ov_msckf::schur_reduction_status_name(production.status) << "/"
        << ov_msckf::schur_reduction_stage_name(production.stage);
    EXPECT_EQ(production.status, ov_msckf::SchurReductionStatus::kAccepted);
    EXPECT_EQ(production.stage, ov_msckf::SchurReductionStage::kAccepted);
    EXPECT_EQ(production.raw_rows, measurement_size);
    EXPECT_EQ(production.degrees_of_freedom, measurement_size - 3);
    EXPECT_TRUE(production.singular_values_available);
    EXPECT_TRUE(production.singular_ratio_available);
    EXPECT_TRUE(production.singular_values.allFinite());
    EXPECT_TRUE(std::isfinite(production.singular_ratio));
    EXPECT_GE(production.singular_ratio, schurvio_cp1::kLandmarkRelativeSingularFloor);
    EXPECT_EQ(production.H_reduced.rows(), measurement_size - 3);
    EXPECT_EQ(production.H_reduced.cols(), state_size);
    EXPECT_EQ(production.residual_reduced.rows(), measurement_size - 3);
    EXPECT_EQ(production.lambda.rows(), state_size);
    EXPECT_EQ(production.lambda.cols(), state_size);
    EXPECT_EQ(production.eta.rows(), state_size);
    EXPECT_TRUE(production.H_reduced.allFinite());
    EXPECT_TRUE(production.residual_reduced.allFinite());
    EXPECT_TRUE(production.lambda.allFinite());
    EXPECT_TRUE(production.eta.allFinite());
    EXPECT_TRUE(std::isfinite(production.gamma));
    EXPECT_TRUE(std::isfinite(production.noise_variance));
    EXPECT_TRUE(std::isfinite(production.raw_lambda_symmetry_error_inf));
    EXPECT_GE(production.gamma, 0.0);
    EXPECT_GE(production.raw_lambda_symmetry_error_inf, 0.0);
    EXPECT_DOUBLE_EQ(production.noise_variance, sigma_px * sigma_px);
    EXPECT_EQ(production.jitter_count, 0U);
    EXPECT_EQ(production.clamp_count, 0U);
    EXPECT_EQ(production.regularization_count, 0U);
    EXPECT_EQ(production.fallback_count, 0U);

    const Eigen::MatrixXd row_lambda =
        production.H_reduced.transpose() * production.H_reduced / production.noise_variance;
    const Eigen::VectorXd row_eta =
        production.H_reduced.transpose() * production.residual_reduced / production.noise_variance;
    const double row_gamma = production.residual_reduced.squaredNorm() / production.noise_variance;
    EXPECT_LE((production.lambda - row_lambda).norm(),
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, row_lambda.norm()));
    EXPECT_LE((production.eta - row_eta).norm(),
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, row_eta.norm()));
    EXPECT_LE(std::abs(production.gamma - row_gamma),
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, std::abs(row_gamma)));

    const SchurReduction oracle_reduction =
        schurvio_cp1::reduce_landmark(state_jacobian, landmark_jacobian, residual);
    ASSERT_EQ(oracle_reduction.status, FactorStatus::kAccepted)
        << schurvio_cp1::factor_status_name(oracle_reduction.status);
    EXPECT_EQ(oracle_reduction.degrees_of_freedom, measurement_size - 3);
    EXPECT_GE(oracle_reduction.singular_ratio, schurvio_cp1::kLandmarkRelativeSingularFloor);

    const Eigen::MatrixXd posterior_information = prior_information + production.lambda;
    Eigen::LDLT<Eigen::MatrixXd> posterior_factor(posterior_information);
    ASSERT_EQ(posterior_factor.info(), Eigen::Success);
    const Eigen::VectorXd state_schur = posterior_factor.solve(production.eta);
    const Eigen::MatrixXd covariance_schur_raw =
        posterior_factor.solve(Eigen::MatrixXd::Identity(state_size, state_size));
    ASSERT_EQ(posterior_factor.info(), Eigen::Success);
    ASSERT_TRUE(state_schur.allFinite());
    ASSERT_TRUE(covariance_schur_raw.allFinite());
    const Eigen::Vector3d landmark_schur =
        schurvio_cp1::back_substitute_landmark(oracle_reduction, state_jacobian, residual, state_schur);

    Eigen::MatrixXd stacked = Eigen::MatrixXd::Zero(state_size + measurement_size, state_size + 3);
    stacked.block(0, 0, state_size, state_size) = prior_sqrt_information;
    stacked.block(state_size, 0, measurement_size, state_size) = state_jacobian;
    stacked.block(state_size, state_size, measurement_size, 3) = landmark_jacobian;
    Eigen::VectorXd stacked_rhs = Eigen::VectorXd::Zero(state_size + measurement_size);
    stacked_rhs.tail(measurement_size) = residual;

    Eigen::JacobiSVD<Eigen::MatrixXd> full_oracle(stacked, Eigen::ComputeThinU | Eigen::ComputeThinV);
    const Eigen::VectorXd joint_solution = full_oracle.solve(stacked_rhs);
    ASSERT_TRUE(joint_solution.allFinite());
    const Eigen::VectorXd state_full = joint_solution.head(state_size);
    const Eigen::Vector3d landmark_full = joint_solution.tail(3);
    Eigen::VectorXd inverse_squared = full_oracle.singularValues();
    ASSERT_GT(inverse_squared.minCoeff(), 0.0);
    for (int index = 0; index < inverse_squared.rows(); ++index) {
      inverse_squared(index) = 1.0 / (inverse_squared(index) * inverse_squared(index));
    }
    const Eigen::MatrixXd covariance_joint =
        full_oracle.matrixV() * inverse_squared.asDiagonal() * full_oracle.matrixV().transpose();
    const Eigen::MatrixXd covariance_full = covariance_joint.topLeftCorner(state_size, state_size);

    const double state_error = (state_schur - state_full).norm();
    const double landmark_error = (landmark_schur - landmark_full).norm();
    const double covariance_error = (covariance_schur_raw - covariance_full).norm();
    worst_state_error = std::max(worst_state_error, state_error);
    worst_landmark_error = std::max(worst_landmark_error, landmark_error);
    worst_covariance_error = std::max(worst_covariance_error, covariance_error);
    EXPECT_LE(state_error, schurvio_cp1::mixed_tolerance(1.0e-9, 1.0e-7, state_full.norm()));
    EXPECT_LE(landmark_error, schurvio_cp1::mixed_tolerance(1.0e-9, 1.0e-7, landmark_full.norm()));
    EXPECT_LE(covariance_error, schurvio_cp1::mixed_tolerance(1.0e-9, 1.0e-7, covariance_full.norm()));

    const double residual_norm_schur =
        (residual - state_jacobian * state_schur - landmark_jacobian * landmark_schur).norm();
    const double residual_norm_full =
        (residual - state_jacobian * state_full - landmark_jacobian * landmark_full).norm();
    EXPECT_LE(std::abs(residual_norm_schur - residual_norm_full),
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-7, residual_norm_full));

    Eigen::JacobiSVD<Eigen::MatrixXd> nullspace_oracle(landmark_jacobian, Eigen::ComputeFullU | Eigen::ComputeThinV);
    const Eigen::MatrixXd left_nullspace = nullspace_oracle.matrixU().rightCols(measurement_size - 3);
    const Eigen::MatrixXd null_state = left_nullspace.transpose() * state_jacobian;
    const Eigen::VectorXd null_residual = left_nullspace.transpose() * residual;
    const double lambda_error = (production.lambda - null_state.transpose() * null_state).norm();
    const double eta_error = (production.eta - null_state.transpose() * null_residual).norm();
    const double gamma_error = std::abs(production.gamma - null_residual.squaredNorm());
    worst_lambda_error = std::max(worst_lambda_error, lambda_error);
    worst_eta_error = std::max(worst_eta_error, eta_error);
    worst_gamma_error = std::max(worst_gamma_error, gamma_error);
    EXPECT_LE(lambda_error, schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, production.lambda.norm()));
    EXPECT_LE(eta_error, schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, production.eta.norm()));
    EXPECT_LE(gamma_error,
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, null_residual.squaredNorm()));

    Eigen::LDLT<Eigen::MatrixXd> prior_factor(prior_information);
    ASSERT_EQ(prior_factor.info(), Eigen::Success);
    const Eigen::MatrixXd prior_covariance = prior_factor.solve(Eigen::MatrixXd::Identity(state_size, state_size));
    const Eigen::MatrixXd cross = prior_covariance * null_state.transpose();
    const Eigen::MatrixXd innovation =
        Eigen::MatrixXd::Identity(null_state.rows(), null_state.rows()) + null_state * cross;
    Eigen::LDLT<Eigen::MatrixXd> innovation_factor(innovation);
    ASSERT_EQ(innovation_factor.info(), Eigen::Success);
    const Eigen::VectorXd state_nullspace = cross * innovation_factor.solve(null_residual);
    const Eigen::MatrixXd covariance_nullspace =
        prior_covariance - cross * innovation_factor.solve(cross.transpose());
    EXPECT_LE((state_schur - state_nullspace).norm(),
              schurvio_cp1::mixed_tolerance(1.0e-9, 1.0e-7, state_nullspace.norm()));
    EXPECT_LE((covariance_schur_raw - covariance_nullspace).norm(),
              schurvio_cp1::mixed_tolerance(1.0e-9, 1.0e-7, covariance_nullspace.norm()));

    const double nis_nullspace = null_residual.dot(innovation_factor.solve(null_residual));
    const double nis_schur = production.gamma - production.eta.dot(state_schur);
    const double nis_error = std::abs(nis_schur - nis_nullspace);
    worst_nis_error = std::max(worst_nis_error, nis_error);
    EXPECT_GE(nis_schur, -1.0e-10 * std::max(1.0, std::abs(nis_nullspace)));
    EXPECT_LE(nis_error, schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-7, std::abs(nis_nullspace)));

    const double symmetry_error = schurvio_cp1::matrix_inf_norm(covariance_schur_raw - covariance_schur_raw.transpose());
    const double covariance_scale = std::max(1.0, schurvio_cp1::matrix_inf_norm(covariance_schur_raw));
    const double symmetry_ratio = symmetry_error / covariance_scale;
    worst_symmetry_ratio = std::max(worst_symmetry_ratio, symmetry_ratio);
    EXPECT_LE(symmetry_error, 1.0e-10 * covariance_scale);

    const Eigen::MatrixXd covariance_symmetric = 0.5 * (covariance_schur_raw + covariance_schur_raw.transpose());
    Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> eigen_solver(covariance_symmetric);
    ASSERT_EQ(eigen_solver.info(), Eigen::Success);
    const double largest_eigenvalue = eigen_solver.eigenvalues().maxCoeff();
    const double minimum_eigenvalue = eigen_solver.eigenvalues().minCoeff();
    const double eigen_scale = std::max(1.0, largest_eigenvalue);
    minimum_normalized_eigenvalue = std::min(minimum_normalized_eigenvalue, minimum_eigenvalue / eigen_scale);
    EXPECT_GE(minimum_eigenvalue, -1.0e-10 * eigen_scale);
  }

  std::cout << "CP1_EQUIVALENCE fixtures=128 near_column_space_fixtures=16 seed=" << schurvio_cp1::kMasterSeed
            << " max_state_error=" << worst_state_error << " max_landmark_error=" << worst_landmark_error
            << " max_covariance_error=" << worst_covariance_error << " max_nis_error=" << worst_nis_error
            << " max_lambda_error=" << worst_lambda_error << " max_eta_error=" << worst_eta_error
            << " max_gamma_error=" << worst_gamma_error
            << " max_symmetry_ratio=" << worst_symmetry_ratio
            << " min_normalized_eigenvalue=" << minimum_normalized_eigenvalue << std::endl;
}

} // namespace
