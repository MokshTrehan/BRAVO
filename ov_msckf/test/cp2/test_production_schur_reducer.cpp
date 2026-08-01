/*
 * SchurVIO-Lite CP2-A production reducer tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKF.h"

#include <gtest/gtest.h>

#include <Eigen/Cholesky>
#include <Eigen/QR>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <iostream>
#include <limits>

namespace {

constexpr std::uint64_t kMasterSeed = 20260801ULL;
constexpr int kAcceptedFixtureCount = 1024;
constexpr int kRejectedFixtureCount = 128;
constexpr double kStatisticsAbsoluteTolerance = 1.0e-10;
constexpr double kStatisticsRelativeTolerance = 1.0e-8;
constexpr double kPosteriorAbsoluteTolerance = 1.0e-8;
constexpr double kPosteriorRelativeTolerance = 1.0e-6;
constexpr double kConditioningFloor = 1.0e-6;
static_assert(ov_msckf::SchurUpdate::kMinimumSingularRatio == kConditioningFloor,
              "CP2 conditioning threshold changed");

class DeterministicRng {
public:
  explicit DeterministicRng(std::uint64_t seed) : state_(seed) {}

  std::uint64_t next_u64() {
    state_ += 0x9e3779b97f4a7c15ULL;
    std::uint64_t value = state_;
    value = (value ^ (value >> 30U)) * 0xbf58476d1ce4e5b9ULL;
    value = (value ^ (value >> 27U)) * 0x94d049bb133111ebULL;
    return value ^ (value >> 31U);
  }

  double uniform() { return static_cast<double>(next_u64() >> 11U) / 9007199254740992.0; }

  double normalish(double scale = 1.0) {
    double value = 0.0;
    for (int index = 0; index < 12; ++index) {
      value += uniform();
    }
    return scale * (value - 6.0);
  }

  int integer(int lower_inclusive, int upper_inclusive) {
    const std::uint64_t width = static_cast<std::uint64_t>(upper_inclusive - lower_inclusive + 1);
    return lower_inclusive + static_cast<int>(next_u64() % width);
  }

  Eigen::MatrixXd matrix(int rows, int columns, double scale = 1.0) {
    Eigen::MatrixXd value(rows, columns);
    for (int row = 0; row < rows; ++row) {
      for (int column = 0; column < columns; ++column) {
        value(row, column) = normalish(scale);
      }
    }
    return value;
  }

  Eigen::VectorXd vector(int rows, double scale = 1.0) {
    Eigen::VectorXd value(rows);
    for (int row = 0; row < rows; ++row) {
      value(row) = normalish(scale);
    }
    return value;
  }

private:
  std::uint64_t state_;
};

double mixed_tolerance(double absolute, double relative, double reference_norm) {
  return absolute + relative * reference_norm;
}

Eigen::MatrixXd orthonormal_columns(DeterministicRng &rng, int rows, int columns) {
  const Eigen::MatrixXd raw = rng.matrix(rows, columns);
  Eigen::HouseholderQR<Eigen::MatrixXd> qr(raw);
  return qr.householderQ() * Eigen::MatrixXd::Identity(rows, columns);
}

Eigen::MatrixXd landmark_jacobian(DeterministicRng &rng, int rows, int fixture) {
  const Eigen::MatrixXd left = orthonormal_columns(rng, rows, 3);
  const Eigen::Matrix3d right = orthonormal_columns(rng, 3, 3);
  Eigen::Vector3d singular_values;
  if (fixture % 16 == 1) {
    // Accepted, but deliberately close to the conditioning boundary.
    const double ratio = kConditioningFloor * (1.0 + 1.0e-3 * static_cast<double>(1 + fixture % 7));
    singular_values << 2.0, 0.4, 2.0 * ratio;
  } else {
    singular_values << 1.25 + 0.75 * rng.uniform(), 0.65 + 0.35 * rng.uniform(),
        0.20 + 0.25 * rng.uniform();
  }
  return left * singular_values.asDiagonal() * right.transpose();
}

struct NullspaceRows {
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
};

NullspaceRows production_givens_oracle(const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
                                       const Eigen::VectorXd &residual) {
  Eigen::MatrixXd H_f_work = H_f;
  NullspaceRows result{H_x, residual};
  ov_msckf::UpdaterHelper::nullspace_project_inplace(H_f_work, result.H, result.residual);
  return result;
}

struct PosteriorProposal {
  double nis = std::numeric_limits<double>::quiet_NaN();
  Eigen::VectorXd increment;
  Eigen::MatrixXd covariance;
};

PosteriorProposal covariance_form_proposal(const Eigen::MatrixXd &prior_covariance, const Eigen::MatrixXd &H,
                                           const Eigen::VectorXd &residual, double noise_variance) {
  PosteriorProposal result;
  const Eigen::MatrixXd cross = prior_covariance * H.transpose();
  Eigen::MatrixXd innovation = H * cross;
  innovation.diagonal().array() += noise_variance;
  Eigen::LLT<Eigen::MatrixXd> factor(innovation.selfadjointView<Eigen::Upper>());
  EXPECT_EQ(factor.info(), Eigen::Success);
  if (factor.info() != Eigen::Success) {
    return result;
  }

  const Eigen::MatrixXd innovation_inverse =
      factor.solve(Eigen::MatrixXd::Identity(innovation.rows(), innovation.cols()));
  EXPECT_EQ(factor.info(), Eigen::Success);
  if (factor.info() != Eigen::Success || !innovation_inverse.allFinite()) {
    return result;
  }

  const Eigen::MatrixXd gain = cross * innovation_inverse;
  result.increment = gain * residual;
  result.covariance = prior_covariance - gain * cross.transpose();
  result.nis = residual.dot(innovation_inverse * residual);
  return result;
}

void expect_empty_rejected_result(const ov_msckf::SchurReductionResult &result) {
  EXPECT_FALSE(result.accepted());
  EXPECT_EQ(result.degrees_of_freedom, 0);
  EXPECT_EQ(result.H_reduced.size(), 0);
  EXPECT_EQ(result.residual_reduced.size(), 0);
  EXPECT_EQ(result.lambda.size(), 0);
  EXPECT_EQ(result.eta.size(), 0);
  EXPECT_TRUE(std::isnan(result.noise_variance));
  EXPECT_TRUE(std::isnan(result.gamma));
  EXPECT_TRUE(std::isnan(result.raw_lambda_symmetry_error_inf));
  EXPECT_EQ(result.jitter_count, 0U);
  EXPECT_EQ(result.clamp_count, 0U);
  EXPECT_EQ(result.regularization_count, 0U);
  EXPECT_EQ(result.fallback_count, 0U);
}

Eigen::MatrixXd diagonal_landmark_system(int rows, double largest, double middle, double smallest) {
  Eigen::MatrixXd H_f = Eigen::MatrixXd::Zero(rows, 3);
  H_f(0, 0) = largest;
  H_f(1, 1) = middle;
  H_f(2, 2) = smallest;
  return H_f;
}

TEST(CP2ProductionSchurReducer, DeterministicGivensStatisticsNisAndPosteriorParity) {
  DeterministicRng rng(kMasterSeed);
  const std::array<double, 9> sigma_values{{0.125, 0.25, 0.5, 0.9, 1.0, 1.75, 3.0, 6.0, 12.0}};
  int near_column_space_fixtures = 0;
  int near_conditioning_fixtures = 0;
  int nonunit_sigma_fixtures = 0;
  double minimum_sigma = std::numeric_limits<double>::infinity();
  double maximum_sigma = 0.0;
  double worst_lambda_error = 0.0;
  double worst_eta_error = 0.0;
  double worst_gamma_error = 0.0;
  double worst_nis_error = 0.0;
  double worst_increment_error = 0.0;
  double worst_covariance_error = 0.0;

  for (int fixture = 0; fixture < kAcceptedFixtureCount; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int state_size = rng.integer(4, 24);
    const int measurement_size = rng.integer(4, 24);
    const double sigma_px = sigma_values[static_cast<std::size_t>(fixture) % sigma_values.size()];
    minimum_sigma = std::min(minimum_sigma, sigma_px);
    maximum_sigma = std::max(maximum_sigma, sigma_px);
    nonunit_sigma_fixtures += sigma_px == 1.0 ? 0 : 1;

    const Eigen::MatrixXd H_f = landmark_jacobian(rng, measurement_size, fixture);
    Eigen::MatrixXd H_x;
    Eigen::VectorXd residual;
    if (fixture % 8 == 0) {
      // Cancellation-sensitive case: nearly all raw energy is in col(H_f).
      H_x = H_f * rng.matrix(3, state_size, 0.4) + rng.matrix(measurement_size, state_size, 1.0e-7);
      residual = H_f * rng.vector(3, 0.7) + rng.vector(measurement_size, 1.0e-7);
      ++near_column_space_fixtures;
    } else {
      H_x = rng.matrix(measurement_size, state_size, 0.35);
      residual = rng.vector(measurement_size, 0.8);
    }
    if (fixture % 16 == 1) {
      ++near_conditioning_fixtures;
    }

    Eigen::MatrixXd prior_root = rng.matrix(state_size, state_size, 0.08);
    Eigen::MatrixXd prior_covariance = prior_root * prior_root.transpose();
    for (int index = 0; index < state_size; ++index) {
      prior_covariance(index, index) += 0.1 + 0.4 * rng.uniform();
    }

    const ov_msckf::SchurReductionResult schur =
        ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, sigma_px);
    ASSERT_EQ(schur.status, ov_msckf::SchurReductionStatus::kAccepted)
        << ov_msckf::schur_reduction_stage_name(schur.stage);
    ASSERT_EQ(schur.stage, ov_msckf::SchurReductionStage::kAccepted);
    ASSERT_TRUE(schur.singular_values_available);
    ASSERT_TRUE(schur.singular_ratio_available);
    ASSERT_TRUE(schur.singular_values.allFinite());
    ASSERT_TRUE(std::isfinite(schur.singular_ratio));
    ASSERT_EQ(schur.raw_rows, measurement_size);
    ASSERT_EQ(schur.degrees_of_freedom, measurement_size - 3);
    ASSERT_EQ(schur.H_reduced.rows(), measurement_size - 3);
    ASSERT_EQ(schur.H_reduced.cols(), state_size);
    ASSERT_EQ(schur.residual_reduced.rows(), measurement_size - 3);
    EXPECT_DOUBLE_EQ(schur.noise_variance, sigma_px * sigma_px);
    EXPECT_GE(schur.singular_ratio, kConditioningFloor);
    EXPECT_GE(schur.gamma, 0.0);
    EXPECT_GE(schur.raw_lambda_symmetry_error_inf, 0.0);
    EXPECT_EQ(schur.jitter_count, 0U);
    EXPECT_EQ(schur.clamp_count, 0U);
    EXPECT_EQ(schur.regularization_count, 0U);
    EXPECT_EQ(schur.fallback_count, 0U);

    const NullspaceRows givens = production_givens_oracle(H_x, H_f, residual);
    ASSERT_EQ(givens.H.rows(), measurement_size - 3);
    ASSERT_EQ(givens.residual.rows(), measurement_size - 3);
    const double inverse_variance = 1.0 / (sigma_px * sigma_px);
    const Eigen::MatrixXd lambda_reference = inverse_variance * givens.H.transpose() * givens.H;
    const Eigen::VectorXd eta_reference = inverse_variance * givens.H.transpose() * givens.residual;
    const double gamma_reference = inverse_variance * givens.residual.squaredNorm();

    const double lambda_error = (schur.lambda - lambda_reference).norm();
    const double eta_error = (schur.eta - eta_reference).norm();
    const double gamma_error = std::abs(schur.gamma - gamma_reference);
    worst_lambda_error = std::max(worst_lambda_error, lambda_error);
    worst_eta_error = std::max(worst_eta_error, eta_error);
    worst_gamma_error = std::max(worst_gamma_error, gamma_error);
    EXPECT_LE(lambda_error, mixed_tolerance(kStatisticsAbsoluteTolerance, kStatisticsRelativeTolerance,
                                            lambda_reference.norm()));
    EXPECT_LE(eta_error,
              mixed_tolerance(kStatisticsAbsoluteTolerance, kStatisticsRelativeTolerance, eta_reference.norm()));
    EXPECT_LE(gamma_error,
              mixed_tolerance(kStatisticsAbsoluteTolerance, kStatisticsRelativeTolerance, std::abs(gamma_reference)));

    const PosteriorProposal schur_proposal =
        covariance_form_proposal(prior_covariance, schur.H_reduced, schur.residual_reduced, schur.noise_variance);
    const PosteriorProposal givens_proposal =
        covariance_form_proposal(prior_covariance, givens.H, givens.residual, sigma_px * sigma_px);
    ASSERT_TRUE(std::isfinite(schur_proposal.nis));
    ASSERT_TRUE(std::isfinite(givens_proposal.nis));
    ASSERT_TRUE(schur_proposal.increment.allFinite());
    ASSERT_TRUE(givens_proposal.increment.allFinite());
    ASSERT_TRUE(schur_proposal.covariance.allFinite());
    ASSERT_TRUE(givens_proposal.covariance.allFinite());

    const double nis_error = std::abs(schur_proposal.nis - givens_proposal.nis);
    const double increment_error = (schur_proposal.increment - givens_proposal.increment).norm();
    const double covariance_error = (schur_proposal.covariance - givens_proposal.covariance).norm();
    worst_nis_error = std::max(worst_nis_error, nis_error);
    worst_increment_error = std::max(worst_increment_error, increment_error);
    worst_covariance_error = std::max(worst_covariance_error, covariance_error);
    EXPECT_LE(nis_error, mixed_tolerance(kPosteriorAbsoluteTolerance, kPosteriorRelativeTolerance,
                                         std::abs(givens_proposal.nis)));
    EXPECT_LE(increment_error, mixed_tolerance(kPosteriorAbsoluteTolerance, kPosteriorRelativeTolerance,
                                               givens_proposal.increment.norm()));
    EXPECT_LE(covariance_error, mixed_tolerance(kPosteriorAbsoluteTolerance, kPosteriorRelativeTolerance,
                                                givens_proposal.covariance.norm()));
  }

  EXPECT_EQ(near_column_space_fixtures, 128);
  EXPECT_EQ(near_conditioning_fixtures, 64);
  EXPECT_EQ(nonunit_sigma_fixtures, 910);
  EXPECT_DOUBLE_EQ(minimum_sigma, 0.125);
  EXPECT_DOUBLE_EQ(maximum_sigma, 12.0);
  std::cout << "CP2_A_ACCEPTED fixtures=" << kAcceptedFixtureCount
            << " near_column_space_fixtures=" << near_column_space_fixtures
            << " near_conditioning_fixtures=" << near_conditioning_fixtures
            << " nonunit_sigma_fixtures=" << nonunit_sigma_fixtures << " min_sigma=" << minimum_sigma
            << " max_sigma=" << maximum_sigma << " seed=" << kMasterSeed
            << " max_lambda_error=" << worst_lambda_error << " max_eta_error=" << worst_eta_error
            << " max_gamma_error=" << worst_gamma_error << " max_nis_error=" << worst_nis_error
            << " max_increment_error=" << worst_increment_error
            << " max_covariance_error=" << worst_covariance_error << std::endl;
}

TEST(CP2ProductionSchurReducer, DeterministicRejectedCorpusHasNoPublishedOutputs) {
  DeterministicRng rng(kMasterSeed ^ 0x72656a656374ULL);
  int rank_deficient = 0;
  int ill_conditioned = 0;
  int insufficient_rows = 0;
  int nonfinite = 0;

  for (int fixture = 0; fixture < kRejectedFixtureCount; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const int category = fixture % 8;
    const int rows = 4 + fixture % 13;
    const int state_size = 4 + fixture % 11;
    Eigen::MatrixXd H_x = rng.matrix(rows, state_size, 0.25);
    Eigen::MatrixXd H_f = landmark_jacobian(rng, rows, fixture);
    Eigen::VectorXd residual = rng.vector(rows, 0.5);
    double sigma_px = 0.2 + 0.1 * static_cast<double>(fixture % 9);
    ov_msckf::SchurReductionStatus expected_status = ov_msckf::SchurReductionStatus::kNonfinite;
    ov_msckf::SchurReductionStage expected_stage = ov_msckf::SchurReductionStage::kInputDimensions;

    switch (category) {
    case 0:
      H_f.col(2) = H_f.col(1);
      expected_status = ov_msckf::SchurReductionStatus::kRankDeficient;
      expected_stage = ov_msckf::SchurReductionStage::kNumericalRank;
      ++rank_deficient;
      break;
    case 1: {
      const Eigen::MatrixXd left = orthonormal_columns(rng, rows, 3);
      const Eigen::Matrix3d right = orthonormal_columns(rng, 3, 3);
      Eigen::Vector3d singular_values;
      singular_values << 1.0, 0.25, 5.0e-7;
      H_f = left * singular_values.asDiagonal() * right.transpose();
      expected_status = ov_msckf::SchurReductionStatus::kIllConditioned;
      expected_stage = ov_msckf::SchurReductionStage::kConditioning;
      ++ill_conditioned;
      break;
    }
    case 2:
      H_x.conservativeResize(3, state_size);
      H_f.conservativeResize(3, 3);
      residual.conservativeResize(3);
      expected_status = ov_msckf::SchurReductionStatus::kInsufficientRows;
      expected_stage = ov_msckf::SchurReductionStage::kInsufficientRows;
      ++insufficient_rows;
      break;
    case 3:
      H_x(0, 0) = std::numeric_limits<double>::quiet_NaN();
      expected_stage = ov_msckf::SchurReductionStage::kRawInputs;
      ++nonfinite;
      break;
    case 4:
      sigma_px = 0.0;
      expected_stage = ov_msckf::SchurReductionStage::kSigma;
      ++nonfinite;
      break;
    case 5:
      H_x.conservativeResize(rows - 1, state_size);
      expected_stage = ov_msckf::SchurReductionStage::kInputDimensions;
      ++nonfinite;
      break;
    case 6:
      H_f.conservativeResize(rows, 2);
      expected_stage = ov_msckf::SchurReductionStage::kInputDimensions;
      ++nonfinite;
      break;
    case 7:
      sigma_px = std::numeric_limits<double>::denorm_min();
      expected_stage = ov_msckf::SchurReductionStage::kWhitening;
      ++nonfinite;
      break;
    }

    const ov_msckf::SchurReductionResult result = ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, sigma_px);
    EXPECT_EQ(result.status, expected_status);
    EXPECT_EQ(result.stage, expected_stage);
    expect_empty_rejected_result(result);
  }

  EXPECT_EQ(rank_deficient, 16);
  EXPECT_EQ(ill_conditioned, 16);
  EXPECT_EQ(insufficient_rows, 16);
  EXPECT_EQ(nonfinite, 80);
  std::cout << "CP2_A_REJECTED fixtures=" << kRejectedFixtureCount << " rank_deficient=" << rank_deficient
            << " ill_conditioned=" << ill_conditioned << " insufficient_rows=" << insufficient_rows
            << " nonfinite=" << nonfinite << " seed=" << (kMasterSeed ^ 0x72656a656374ULL) << std::endl;
}

TEST(CP2ProductionSchurReducer, OrderedValidityAndRankBoundariesAreExact) {
  constexpr int rows = 8;
  const Eigen::MatrixXd H_x = Eigen::MatrixXd::Identity(rows, rows);
  const Eigen::VectorXd residual = Eigen::VectorXd::LinSpaced(rows, -0.5, 0.5);
  const Eigen::MatrixXd regular = diagonal_landmark_system(rows, 1.0, 0.5, 0.25);

  // Dimension/representation compatibility has highest priority.
  Eigen::MatrixXd wrong_columns = Eigen::MatrixXd::Zero(3, 2);
  Eigen::MatrixXd wrong_rows = Eigen::MatrixXd::Constant(2, rows, std::numeric_limits<double>::quiet_NaN());
  Eigen::VectorXd short_residual = Eigen::VectorXd::Zero(3);
  auto result = ov_msckf::SchurUpdate::Reduce(wrong_rows, wrong_columns, short_residual, 0.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kInputDimensions);
  EXPECT_FALSE(result.singular_values_available);
  EXPECT_FALSE(result.singular_ratio_available);
  expect_empty_rejected_result(result);

  // m<=3 precedes raw-field and sigma checks.
  Eigen::MatrixXd short_H_x = Eigen::MatrixXd::Zero(3, rows);
  Eigen::MatrixXd short_H_f = Eigen::MatrixXd::Zero(3, 3);
  short_H_x(0, 0) = std::numeric_limits<double>::quiet_NaN();
  result = ov_msckf::SchurUpdate::Reduce(short_H_x, short_H_f, Eigen::Vector3d::Zero(), 0.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kInsufficientRows);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kInsufficientRows);
  EXPECT_FALSE(result.singular_values_available);
  EXPECT_FALSE(result.singular_ratio_available);

  // Raw nonfiniteness precedes an invalid sigma.
  Eigen::MatrixXd nonfinite_H_x = H_x;
  nonfinite_H_x(0, 0) = std::numeric_limits<double>::infinity();
  result = ov_msckf::SchurUpdate::Reduce(nonfinite_H_x, regular, residual, 0.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kRawInputs);

  const std::array<double, 4> invalid_sigmas{{0.0, -1.0, std::numeric_limits<double>::quiet_NaN(),
                                              std::numeric_limits<double>::infinity()}};
  for (double sigma_px : invalid_sigmas) {
    result = ov_msckf::SchurUpdate::Reduce(H_x, regular, residual, sigma_px);
    EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
    EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kSigma);
    EXPECT_FALSE(result.singular_values_available);
    EXPECT_FALSE(result.singular_ratio_available);
    expect_empty_rejected_result(result);
  }

  // Direct elementwise quotients that overflow are a whitening failure.
  result = ov_msckf::SchurUpdate::Reduce(H_x, regular, residual, std::numeric_limits<double>::denorm_min());
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kWhitening);
  EXPECT_FALSE(result.singular_values_available);
  EXPECT_FALSE(result.singular_ratio_available);

  // A complete zero spectrum exposes singular values but no ratio.
  result = ov_msckf::SchurUpdate::Reduce(H_x, Eigen::MatrixXd::Zero(rows, 3), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kRankDeficient);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kLargestSingularValue);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_FALSE(result.singular_ratio_available);
  EXPECT_TRUE(std::isnan(result.singular_ratio));
  expect_empty_rejected_result(result);

  // Equality at the largest-singular-value floor is rejected.
  const double minimum_normal = std::numeric_limits<double>::min();
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, minimum_normal, minimum_normal, minimum_normal), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kRankDeficient);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kLargestSingularValue);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_FALSE(result.singular_ratio_available);
  EXPECT_DOUBLE_EQ(result.singular_values(0), minimum_normal);

  // Equality at max(m,3)*epsilon*s1 is rejected; the next double is not.
  const double numerical_floor = static_cast<double>(rows) * std::numeric_limits<double>::epsilon();
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, 1.0, 0.5, numerical_floor), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kRankDeficient);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kNumericalRank);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_TRUE(result.singular_ratio_available);
  EXPECT_DOUBLE_EQ(result.singular_values(2), numerical_floor);

  const double above_numerical_floor =
      std::nextafter(numerical_floor, std::numeric_limits<double>::infinity());
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, 1.0, 0.5, above_numerical_floor), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kIllConditioned);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kConditioning);

  // Equality at rho=1e-6 is accepted; only strict-below is rejected.
  const double conditioning_floor = kConditioningFloor;
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, 1.0, 0.5, conditioning_floor), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kAccepted);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kAccepted);
  EXPECT_DOUBLE_EQ(result.singular_ratio, conditioning_floor);
  EXPECT_EQ(result.degrees_of_freedom, rows - 3);

  const double below_conditioning_floor = std::nextafter(conditioning_floor, 0.0);
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, 1.0, 0.5, below_conditioning_floor), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kIllConditioned);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kConditioning);
  EXPECT_LT(result.singular_ratio, conditioning_floor);
  expect_empty_rejected_result(result);

  const double above_conditioning_floor =
      std::nextafter(conditioning_floor, std::numeric_limits<double>::infinity());
  result = ov_msckf::SchurUpdate::Reduce(
      H_x, diagonal_landmark_system(rows, 1.0, 0.5, above_conditioning_floor), residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kAccepted);
  EXPECT_GT(result.singular_ratio, conditioning_floor);

  // The minimum accepted row count emits exactly one row and q=m-3=1.
  result = ov_msckf::SchurUpdate::Reduce(Eigen::MatrixXd::Identity(4, 4),
                                         diagonal_landmark_system(4, 1.0, 0.5, 0.25),
                                         Eigen::Vector4d::Ones(), 2.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kAccepted);
  EXPECT_EQ(result.degrees_of_freedom, 1);
  EXPECT_EQ(result.H_reduced.rows(), 1);
  EXPECT_EQ(result.residual_reduced.rows(), 1);
  EXPECT_DOUBLE_EQ(result.noise_variance, 4.0);
}

TEST(CP2ProductionSchurReducer, ReducedOutputAndStatisticsOverflowRejectAtomically) {
  constexpr int rows = 6;
  constexpr int state_size = 4;
  const Eigen::VectorXd base_residual = Eigen::VectorXd::LinSpaced(rows, 0.25, 0.75);

  // Whitening is finite and rank-valid, but sigma^2 cannot be represented.
  const double sigma_px = std::numeric_limits<double>::max() / 2.0;
  const Eigen::MatrixXd H_f = sigma_px * diagonal_landmark_system(rows, 1.0, 0.5, 0.25);
  Eigen::MatrixXd H_x = Eigen::MatrixXd::Zero(rows, state_size);
  H_x.bottomRows(3) = sigma_px * Eigen::MatrixXd::Constant(3, state_size, 0.25);
  const Eigen::VectorXd residual = sigma_px * base_residual;
  auto result = ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, sigma_px);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kReducedOutputs);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_TRUE(result.singular_ratio_available);
  expect_empty_rejected_result(result);

  // A subnormal sigma does not fail merely because 1/sigma is unrepresentable:
  // proportionally scaled raw fields have finite direct quotients. It advances
  // through whitening and rank checks, then rejects because sigma^2 is zero.
  const double subnormal_sigma = std::numeric_limits<double>::denorm_min();
  const Eigen::MatrixXd subnormal_H_f =
      diagonal_landmark_system(rows, 4.0 * subnormal_sigma, 3.0 * subnormal_sigma, 2.0 * subnormal_sigma);
  Eigen::MatrixXd subnormal_H_x = Eigen::MatrixXd::Zero(rows, state_size);
  subnormal_H_x.bottomRows(3) =
      Eigen::MatrixXd::Constant(3, state_size, 2.0 * subnormal_sigma);
  const Eigen::VectorXd subnormal_residual =
      Eigen::VectorXd::Constant(rows, 2.0 * subnormal_sigma);
  result = ov_msckf::SchurUpdate::Reduce(subnormal_H_x, subnormal_H_f, subnormal_residual, subnormal_sigma);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kReducedOutputs);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_TRUE(result.singular_ratio_available);
  EXPECT_DOUBLE_EQ(subnormal_sigma * subnormal_sigma, 0.0);
  expect_empty_rejected_result(result);

  // The adjacent policy is positive, not normal: a tiny representable
  // subnormal variance remains valid and the same scaled system is accepted.
  const double tiny_sigma = std::sqrt(std::numeric_limits<double>::denorm_min());
  ASSERT_TRUE(std::isfinite(tiny_sigma));
  ASSERT_GT(tiny_sigma * tiny_sigma, 0.0);
  const Eigen::MatrixXd tiny_H_f =
      diagonal_landmark_system(rows, 4.0 * tiny_sigma, 3.0 * tiny_sigma, 2.0 * tiny_sigma);
  Eigen::MatrixXd tiny_H_x = Eigen::MatrixXd::Zero(rows, state_size);
  tiny_H_x.bottomRows(3) = Eigen::MatrixXd::Constant(3, state_size, 2.0 * tiny_sigma);
  const Eigen::VectorXd tiny_residual = Eigen::VectorXd::Constant(rows, 2.0 * tiny_sigma);
  result = ov_msckf::SchurUpdate::Reduce(tiny_H_x, tiny_H_f, tiny_residual, tiny_sigma);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kAccepted);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kAccepted);
  EXPECT_GT(result.noise_variance, 0.0);
  EXPECT_TRUE(result.H_reduced.allFinite());
  EXPECT_TRUE(result.residual_reduced.allFinite());
  EXPECT_TRUE(result.lambda.allFinite());
  EXPECT_TRUE(result.eta.allFinite());
  EXPECT_TRUE(std::isfinite(result.gamma));
  EXPECT_EQ(result.jitter_count, 0U);
  EXPECT_EQ(result.clamp_count, 0U);
  EXPECT_EQ(result.regularization_count, 0U);
  EXPECT_EQ(result.fallback_count, 0U);

  // Reduced rows remain finite, but forming H_N^T H_N overflows.
  H_x.setZero();
  H_x.bottomRows(3) = Eigen::MatrixXd::Constant(3, state_size, 1.0e200);
  result = ov_msckf::SchurUpdate::Reduce(H_x, diagonal_landmark_system(rows, 1.0, 0.5, 0.25),
                                         base_residual, 1.0);
  EXPECT_EQ(result.status, ov_msckf::SchurReductionStatus::kNonfinite);
  EXPECT_EQ(result.stage, ov_msckf::SchurReductionStage::kStatistics);
  EXPECT_TRUE(result.singular_values_available);
  EXPECT_TRUE(result.singular_ratio_available);
  expect_empty_rejected_result(result);
}

TEST(CP2ProductionSchurReducer, StrictNisDecisionBoundaryContract) {
  // Exercise the exact production predicate used by UpdaterMSCKF: equality is
  // accepted and only a strict excess rejects.
  constexpr double threshold = 7.814727903251179;
  EXPECT_FALSE(ov_msckf::UpdaterMSCKF::chi2_gate_rejects(threshold, threshold));
  EXPECT_TRUE(ov_msckf::UpdaterMSCKF::chi2_gate_rejects(
      std::nextafter(threshold, std::numeric_limits<double>::infinity()), threshold));
  EXPECT_FALSE(ov_msckf::UpdaterMSCKF::chi2_gate_rejects(std::nextafter(threshold, 0.0), threshold));
}

} // namespace
