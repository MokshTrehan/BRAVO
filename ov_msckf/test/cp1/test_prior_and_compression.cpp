/*
 * SchurVIO-Lite CP1 semidefinite-prior and compression regression tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cp1_fixture_utils.h"

#include <gtest/gtest.h>

#include "update/UpdaterHelper.h"

#include <Eigen/Cholesky>
#include <Eigen/Dense>
#include <Eigen/Eigenvalues>

#include <algorithm>
#include <cmath>
#include <iostream>
#include <limits>

namespace {

double tolerance_ratio(double error, double absolute, double relative, double reference_norm) {
  return error / schurvio_cp1::mixed_tolerance(absolute, relative, reference_norm);
}

TEST(CP1Prior, SemidefiniteCloneAugmentationMatchesInnovationUpdate) {
  constexpr std::uint64_t seed = schurvio_cp1::kMasterSeed ^ 0x707364ULL;
  schurvio_cp1::DeterministicRng rng(seed);
  double worst_known_state_ratio = 0.0;
  double worst_known_covariance_ratio = 0.0;
  double worst_known_nis_ratio = 0.0;
  double worst_spectral_state_ratio = 0.0;
  double worst_spectral_covariance_ratio = 0.0;
  double worst_spectral_nis_ratio = 0.0;
  double worst_zero_eigenvalue_ratio = 0.0;
  double worst_clone_nullspace_ratio = 0.0;
  double minimum_posterior_normalized_eigenvalue = std::numeric_limits<double>::infinity();

  for (int fixture = 0; fixture < 128; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int base_size = rng.integer(9, 24);
    const int clone_size = 6;
    const int state_size = base_size + clone_size;
    const int measurement_size = rng.integer(4, 14);

    // A well-conditioned square root for the pre-clone covariance.  Appending
    // an exact selector copy models StateHelper::clone without artificial
    // clone noise and gives the augmented prior exactly six null directions.
    Eigen::MatrixXd base_factor = Eigen::MatrixXd::Zero(base_size, base_size);
    for (int row = 0; row < base_size; ++row) {
      for (int column = 0; column < row; ++column) {
        base_factor(row, column) = rng.normalish(0.04);
      }
      base_factor(row, row) = 0.8 + 0.4 * rng.uniform();
    }
    const int copied_offset = rng.integer(0, base_size - clone_size);
    Eigen::MatrixXd augmentation = Eigen::MatrixXd::Zero(state_size, base_size);
    augmentation.topRows(base_size).setIdentity();
    augmentation.block(base_size, copied_offset, clone_size, clone_size).setIdentity();
    const Eigen::MatrixXd known_rectangular_factor = augmentation * base_factor;
    const Eigen::MatrixXd prior = known_rectangular_factor * known_rectangular_factor.transpose();
    const Eigen::MatrixXd selector = augmentation.bottomRows(clone_size);
    Eigen::MatrixXd clone_nullspace = Eigen::MatrixXd::Zero(state_size, clone_size);
    clone_nullspace.topRows(base_size) = -selector.transpose();
    clone_nullspace.bottomRows(clone_size).setIdentity();
    const double prior_nullspace_ratio = (prior * clone_nullspace).norm() /
                                         (1.0e-12 * std::max(1.0, prior.norm()));
    worst_clone_nullspace_ratio = std::max(worst_clone_nullspace_ratio, prior_nullspace_ratio);
    EXPECT_LE(prior_nullspace_ratio, 1.0);

    const Eigen::MatrixXd measurement_jacobian = rng.matrix(measurement_size, state_size, 0.18);
    const Eigen::VectorXd residual = rng.vector(measurement_size, 0.35);
    const Eigen::MatrixXd cross = prior * measurement_jacobian.transpose();
    const Eigen::MatrixXd innovation =
        Eigen::MatrixXd::Identity(measurement_size, measurement_size) + measurement_jacobian * cross;
    Eigen::LDLT<Eigen::MatrixXd> innovation_factor(innovation);
    ASSERT_EQ(innovation_factor.info(), Eigen::Success);
    ASSERT_TRUE(innovation_factor.isPositive());
    const Eigen::VectorXd reference_increment = cross * innovation_factor.solve(residual);
    const Eigen::MatrixXd reference_covariance = prior - cross * innovation_factor.solve(cross.transpose());
    const double reference_nis = residual.dot(innovation_factor.solve(residual));
    const double reference_nullspace_ratio = (reference_covariance * clone_nullspace).norm() /
                                             (1.0e-12 * std::max(1.0, reference_covariance.norm()));
    worst_clone_nullspace_ratio = std::max(worst_clone_nullspace_ratio, reference_nullspace_ratio);
    EXPECT_LE(reference_nullspace_ratio, 1.0);

    const auto check_factor = [&](const Eigen::MatrixXd &prior_factor, double &worst_state_ratio,
                                  double &worst_covariance_ratio, double &worst_nis_ratio) {
      const Eigen::MatrixXd projected = measurement_jacobian * prior_factor;
      const Eigen::MatrixXd reduced_information =
          Eigen::MatrixXd::Identity(prior_factor.cols(), prior_factor.cols()) + projected.transpose() * projected;
      Eigen::LDLT<Eigen::MatrixXd> reduced_factor(reduced_information);
      ASSERT_EQ(reduced_factor.info(), Eigen::Success);
      ASSERT_TRUE(reduced_factor.isPositive());
      const Eigen::VectorXd reduced_rhs = projected.transpose() * residual;
      const Eigen::VectorXd increment = prior_factor * reduced_factor.solve(reduced_rhs);
      const Eigen::MatrixXd covariance = prior_factor * reduced_factor.solve(prior_factor.transpose());
      const double nis = residual.squaredNorm() - reduced_rhs.dot(reduced_factor.solve(reduced_rhs));

      const double state_error = (increment - reference_increment).norm();
      const double covariance_error = (covariance - reference_covariance).norm();
      const double nis_error = std::abs(nis - reference_nis);
      const double state_ratio = tolerance_ratio(state_error, 1.0e-9, 1.0e-7, reference_increment.norm());
      const double covariance_ratio =
          tolerance_ratio(covariance_error, 1.0e-9, 1.0e-7, reference_covariance.norm());
      const double nis_ratio = tolerance_ratio(nis_error, 1.0e-10, 1.0e-8, std::abs(reference_nis));
      worst_state_ratio = std::max(worst_state_ratio, state_ratio);
      worst_covariance_ratio = std::max(worst_covariance_ratio, covariance_ratio);
      worst_nis_ratio = std::max(worst_nis_ratio, nis_ratio);
      EXPECT_LE(state_ratio, 1.0);
      EXPECT_LE(covariance_ratio, 1.0);
      EXPECT_LE(nis_ratio, 1.0);
      const double clone_nullspace_ratio = (covariance * clone_nullspace).norm() /
                                           (1.0e-12 * std::max(1.0, covariance.norm()));
      worst_clone_nullspace_ratio = std::max(worst_clone_nullspace_ratio, clone_nullspace_ratio);
      EXPECT_LE(clone_nullspace_ratio, 1.0);

      const double symmetry_error = schurvio_cp1::matrix_inf_norm(covariance - covariance.transpose());
      const double covariance_scale = std::max(1.0, schurvio_cp1::matrix_inf_norm(covariance));
      EXPECT_LE(symmetry_error, 1.0e-10 * covariance_scale);
      const Eigen::MatrixXd symmetric_covariance = 0.5 * (covariance + covariance.transpose());
      Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> posterior_eigensolver(symmetric_covariance);
      ASSERT_EQ(posterior_eigensolver.info(), Eigen::Success);
      const double largest_posterior_eigenvalue = posterior_eigensolver.eigenvalues().maxCoeff();
      const double posterior_scale = std::max(1.0, largest_posterior_eigenvalue);
      const double normalized_minimum = posterior_eigensolver.eigenvalues().minCoeff() / posterior_scale;
      minimum_posterior_normalized_eigenvalue =
          std::min(minimum_posterior_normalized_eigenvalue, normalized_minimum);
      EXPECT_GE(normalized_minimum, -1.0e-10);
    };

    check_factor(known_rectangular_factor, worst_known_state_ratio, worst_known_covariance_ratio,
                 worst_known_nis_ratio);

    // Recover a second rectangular factor from the PSD prior itself.  Only
    // eigenvalues inside a declared roundoff band are classified as zero; no
    // negative eigenvalue is clamped and no diagonal jitter is introduced.
    Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> prior_eigensolver(0.5 * (prior + prior.transpose()));
    ASSERT_EQ(prior_eigensolver.info(), Eigen::Success);
    const Eigen::VectorXd eigenvalues = prior_eigensolver.eigenvalues();
    const double spectral_scale = std::max(1.0, eigenvalues.maxCoeff());
    const double zero_tolerance =
        256.0 * static_cast<double>(state_size) * std::numeric_limits<double>::epsilon() * spectral_scale;
    ASSERT_EQ(eigenvalues.size(), state_size);
    for (int index = 0; index < clone_size; ++index) {
      const double ratio = std::abs(eigenvalues(index)) / zero_tolerance;
      worst_zero_eigenvalue_ratio = std::max(worst_zero_eigenvalue_ratio, ratio);
      EXPECT_LE(ratio, 1.0);
    }
    ASSERT_GT(eigenvalues(clone_size), zero_tolerance);
    const Eigen::VectorXd positive_eigenvalues = eigenvalues.tail(base_size);
    ASSERT_TRUE((positive_eigenvalues.array() > zero_tolerance).all());
    const Eigen::MatrixXd spectral_factor =
        prior_eigensolver.eigenvectors().rightCols(base_size) * positive_eigenvalues.cwiseSqrt().asDiagonal();
    check_factor(spectral_factor, worst_spectral_state_ratio, worst_spectral_covariance_ratio,
                 worst_spectral_nis_ratio);
  }

  // A directly unobserved but correlated state component must still receive
  // the indirect mean and covariance correction.  This catches an updater
  // that commits only the columns named by the measurement matrix.
  Eigen::Matrix2d correlated_prior;
  correlated_prior << 1.0, 0.8, 0.8, 1.0;
  Eigen::LLT<Eigen::Matrix2d> correlated_factor(correlated_prior);
  ASSERT_EQ(correlated_factor.info(), Eigen::Success);
  const Eigen::Matrix2d correlated_factor_dense = correlated_factor.matrixL();
  Eigen::Matrix<double, 1, 2> observed_first_only;
  observed_first_only << 1.0, 0.0;
  const Eigen::Vector2d correlated_root_rhs = correlated_factor_dense.transpose() * observed_first_only.transpose();
  const Eigen::Matrix2d correlated_root_information =
      Eigen::Matrix2d::Identity() + correlated_root_rhs * correlated_root_rhs.transpose();
  Eigen::LDLT<Eigen::Matrix2d> correlated_root_solve(correlated_root_information);
  ASSERT_EQ(correlated_root_solve.info(), Eigen::Success);
  const Eigen::Vector2d correlated_increment =
      correlated_factor_dense * correlated_root_solve.solve(correlated_root_rhs);
  const Eigen::Matrix2d correlated_posterior =
      correlated_factor_dense * correlated_root_solve.solve(correlated_factor_dense.transpose());
  Eigen::Vector2d expected_correlated_increment;
  expected_correlated_increment << 0.5, 0.4;
  Eigen::Matrix2d expected_correlated_posterior;
  expected_correlated_posterior << 0.5, 0.4, 0.4, 0.68;
  EXPECT_LE((correlated_increment - expected_correlated_increment).norm(), 1.0e-14);
  EXPECT_LE((correlated_posterior - expected_correlated_posterior).norm(), 1.0e-14);
  EXPECT_GT(std::abs(correlated_increment(1)), 0.1);

  std::cout << "CP1_PSD_PRIOR fixtures=128 seed=" << seed
            << " max_known_state_tolerance_ratio=" << worst_known_state_ratio
            << " max_known_covariance_tolerance_ratio=" << worst_known_covariance_ratio
            << " max_known_nis_tolerance_ratio=" << worst_known_nis_ratio
            << " max_spectral_state_tolerance_ratio=" << worst_spectral_state_ratio
            << " max_spectral_covariance_tolerance_ratio=" << worst_spectral_covariance_ratio
            << " max_spectral_nis_tolerance_ratio=" << worst_spectral_nis_ratio
            << " max_zero_eigenvalue_tolerance_ratio=" << worst_zero_eigenvalue_ratio
            << " max_clone_nullspace_tolerance_ratio=" << worst_clone_nullspace_ratio
            << " min_posterior_normalized_eigenvalue=" << minimum_posterior_normalized_eigenvalue << std::endl;
}

TEST(CP1Compression, ProductionTruncationPreservesLambdaEtaButNotGamma) {
  constexpr std::uint64_t seed = schurvio_cp1::kMasterSeed ^ 0x67616d6d61ULL;
  schurvio_cp1::DeterministicRng rng(seed);
  double worst_lambda_ratio = 0.0;
  double worst_eta_ratio = 0.0;
  double worst_gamma_reconstruction_ratio = 0.0;
  double minimum_discarded_energy = std::numeric_limits<double>::infinity();

  for (int fixture = 0; fixture < 128; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int columns = rng.integer(3, 9);
    const int rows = columns + rng.integer(1, 12);
    const Eigen::MatrixXd orthogonal = schurvio_cp1::orthonormal_columns(rng, rows, rows);
    const Eigen::MatrixXd right = schurvio_cp1::orthonormal_columns(rng, columns, columns);
    Eigen::VectorXd singular_values(columns);
    for (int index = 0; index < columns; ++index) {
      singular_values(index) = 0.7 + 0.6 * rng.uniform();
    }
    Eigen::MatrixXd state_jacobian =
        orthogonal.leftCols(columns) * singular_values.asDiagonal() * right.transpose();
    const Eigen::VectorXd column_coefficients = rng.vector(columns, 0.3);
    Eigen::VectorXd discarded_coefficients = rng.vector(rows - columns, 0.1);
    discarded_coefficients(0) += 2.0;
    Eigen::VectorXd residual = state_jacobian * column_coefficients +
                               orthogonal.rightCols(rows - columns) * discarded_coefficients;

    const Eigen::MatrixXd lambda_before = state_jacobian.transpose() * state_jacobian;
    const Eigen::VectorXd eta_before = state_jacobian.transpose() * residual;
    const double gamma_before = residual.squaredNorm();
    const double expected_discarded_energy = discarded_coefficients.squaredNorm();
    minimum_discarded_energy = std::min(minimum_discarded_energy, expected_discarded_energy);

    // This calls the actual out-of-line OpenVINS production implementation.
    ov_msckf::UpdaterHelper::measurement_compress_inplace(state_jacobian, residual);
    ASSERT_EQ(state_jacobian.rows(), columns);
    ASSERT_EQ(residual.rows(), columns);
    ASSERT_TRUE(state_jacobian.allFinite());
    ASSERT_TRUE(residual.allFinite());

    const Eigen::MatrixXd lambda_after = state_jacobian.transpose() * state_jacobian;
    const Eigen::VectorXd eta_after = state_jacobian.transpose() * residual;
    const double gamma_after = residual.squaredNorm();
    const double lambda_ratio = tolerance_ratio((lambda_after - lambda_before).norm(), 1.0e-10, 1.0e-8,
                                                lambda_before.norm());
    const double eta_ratio =
        tolerance_ratio((eta_after - eta_before).norm(), 1.0e-10, 1.0e-8, eta_before.norm());
    const double gamma_reconstruction_ratio =
        tolerance_ratio(std::abs(gamma_after + expected_discarded_energy - gamma_before), 1.0e-10, 1.0e-8,
                        gamma_before);
    worst_lambda_ratio = std::max(worst_lambda_ratio, lambda_ratio);
    worst_eta_ratio = std::max(worst_eta_ratio, eta_ratio);
    worst_gamma_reconstruction_ratio = std::max(worst_gamma_reconstruction_ratio, gamma_reconstruction_ratio);
    EXPECT_LE(lambda_ratio, 1.0);
    EXPECT_LE(eta_ratio, 1.0);
    EXPECT_LE(gamma_reconstruction_ratio, 1.0);
    EXPECT_GT(gamma_before - gamma_after, 0.25);
    EXPECT_LE(std::abs((gamma_before - gamma_after) - expected_discarded_energy),
              schurvio_cp1::mixed_tolerance(1.0e-10, 1.0e-8, expected_discarded_energy));
  }

  std::cout << "CP1_COMPRESSION fixtures=128 seed=" << seed
            << " max_lambda_tolerance_ratio=" << worst_lambda_ratio
            << " max_eta_tolerance_ratio=" << worst_eta_ratio
            << " max_gamma_reconstruction_tolerance_ratio=" << worst_gamma_reconstruction_ratio
            << " min_discarded_energy=" << minimum_discarded_energy << std::endl;
}

} // namespace
