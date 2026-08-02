/*
 * SchurVIO-Lite CP2 value-only raw-to-proposal kernel tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2ShadowMath.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <utility>

namespace {

std::uint64_t binary64_bits(double value) {
  std::uint64_t bits = 0;
  static_assert(sizeof(bits) == sizeof(value), "CP2 tests require IEEE-754 binary64 storage");
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

template <typename ActualDerived, typename ExpectedDerived>
void expect_coefficient_bits_equal(const Eigen::MatrixBase<ActualDerived> &actual,
                                   const Eigen::MatrixBase<ExpectedDerived> &expected) {
  ASSERT_EQ(actual.rows(), expected.rows());
  ASSERT_EQ(actual.cols(), expected.cols());
  for (Eigen::Index row = 0; row < actual.rows(); ++row) {
    for (Eigen::Index column = 0; column < actual.cols(); ++column) {
      EXPECT_EQ(binary64_bits(actual.derived().coeff(row, column)),
                binary64_bits(expected.derived().coeff(row, column)))
          << "row=" << row << " column=" << column;
    }
  }
}

ov_msckf::CP2FeatureGate::ChiSquaredTable finite_table(double value) {
  ov_msckf::CP2FeatureGate::ChiSquaredTable table;
  for (int degrees_of_freedom = 1; degrees_of_freedom < 500; ++degrees_of_freedom) {
    table.emplace(degrees_of_freedom, value);
  }
  return table;
}

ov_msckf::MSCKFUpdatePreviewSnapshot prior_snapshot(Eigen::Index dimension) {
  ov_msckf::MSCKFUpdatePreviewSnapshot prior;
  prior.covariance = Eigen::MatrixXd::Identity(dimension, dimension);
  prior.state_blocks = {{0, dimension, 0}};
  return prior;
}

ov_msckf::CP2RawFeatureSystem diagonal_raw(std::uint64_t feature_id,
                                           const Eigen::MatrixXd &reduced_H,
                                           const Eigen::VectorXd &reduced_residual) {
  EXPECT_EQ(reduced_H.rows(), reduced_residual.rows());
  ov_msckf::CP2RawFeatureSystem raw;
  raw.feature_id = feature_id;
  raw.H_f = Eigen::MatrixXd::Zero(reduced_H.rows() + 3, 3);
  raw.H_f(0, 0) = 3.0;
  raw.H_f(1, 1) = 2.0;
  raw.H_f(2, 2) = 1.0;
  raw.H_x = Eigen::MatrixXd::Zero(reduced_H.rows() + 3, reduced_H.cols());
  raw.H_x.bottomRows(reduced_H.rows()) = reduced_H;
  raw.residual = Eigen::VectorXd::Zero(reduced_residual.rows() + 3);
  raw.residual.tail(reduced_residual.rows()) = reduced_residual;
  raw.jacobian_layout = {{0, reduced_H.cols(), 0}};
  return raw;
}

ov_msckf::CP2ShadowMathInput normal_input() {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(2);
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(1.0e6);

  Eigen::MatrixXd first_H(3, 2);
  first_H << 0.25, -0.10, 0.05, 0.30, -0.12, 0.08;
  Eigen::Vector3d first_residual;
  first_residual << 0.20, -0.15, 0.07;
  input.raw_systems.push_back(diagonal_raw(7, first_H, first_residual));

  Eigen::MatrixXd second_H(3, 2);
  second_H << -0.08, 0.15, 0.22, -0.05, 0.11, 0.09;
  Eigen::Vector3d second_residual;
  second_residual << -0.04, 0.12, -0.09;
  input.raw_systems.push_back(diagonal_raw(3, second_H, second_residual));
  return input;
}

TEST(CP2ShadowMath, FullRankPathsAgreeAndProduceIndependentGlobalProposals) {
  const ov_msckf::CP2ShadowMathResult result = ov_msckf::CP2ShadowMath::Process(normal_input());

  ASSERT_TRUE(result.input_valid);
  EXPECT_FALSE(result.duplicate_feature_id);
  ASSERT_EQ(result.features.size(), 2U);
  for (const ov_msckf::CP2FeaturePairResult &feature : result.features) {
    ASSERT_TRUE(feature.nullspace.accepted());
    ASSERT_TRUE(feature.schur.accepted());
    ASSERT_EQ(feature.nullspace_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
    ASSERT_EQ(feature.schur_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
    EXPECT_TRUE(feature.nullspace_gate.evidence_accept);
    EXPECT_TRUE(feature.schur_gate.evidence_accept);
    EXPECT_EQ(feature.agreement_class, ov_msckf::CP2GateAgreementClass::kBothMatchAccept);
    EXPECT_EQ(feature.raw_row_match_weight, static_cast<std::uint64_t>(feature.raw_rows));
    EXPECT_TRUE(feature.statistics_comparison_required);
    EXPECT_EQ(feature.lambda_comparison.status,
              ov_msckf::CP2StatisticComparisonStatus::kAvailable);
    EXPECT_EQ(feature.eta_comparison.status,
              ov_msckf::CP2StatisticComparisonStatus::kAvailable);
    EXPECT_EQ(feature.gamma_comparison.status,
              ov_msckf::CP2StatisticComparisonStatus::kAvailable);
    EXPECT_TRUE(feature.lambda_comparison.passed);
    EXPECT_TRUE(feature.eta_comparison.passed);
    EXPECT_TRUE(feature.gamma_comparison.passed);
    EXPECT_LE(feature.lambda_comparison.ratio, 1.0);
    EXPECT_LE(feature.eta_comparison.ratio, 1.0);
    EXPECT_LE(feature.gamma_comparison.ratio, 1.0);
  }

  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{7, 3}));
  EXPECT_EQ(result.schur.accepted_ids, (std::vector<std::uint64_t>{7, 3}));
  EXPECT_EQ(result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_EQ(result.schur.gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_EQ(result.nullspace.precompression_rows, 6);
  EXPECT_EQ(result.schur.precompression_rows, 6);
  EXPECT_EQ(result.nullspace.compressed_rows, 2);
  EXPECT_EQ(result.schur.compressed_rows, 2);
  ASSERT_TRUE(result.nullspace.proposal_available);
  ASSERT_TRUE(result.schur.proposal_available);

  const double increment_error = (result.schur.proposal.dx - result.nullspace.proposal.dx).norm();
  const double increment_tolerance = 1.0e-8 + 1.0e-6 * result.nullspace.proposal.dx.norm();
  const double covariance_error =
      (result.schur.proposal.P_plus - result.nullspace.proposal.P_plus).norm();
  const double covariance_tolerance =
      1.0e-8 + 1.0e-6 * result.nullspace.proposal.P_plus.norm();
  EXPECT_LE(increment_error, increment_tolerance);
  EXPECT_LE(covariance_error, covariance_tolerance);
}

TEST(CP2ShadowMath, CandidateRankFailureIsAOneSidedGateAttempt) {
  ov_msckf::CP2ShadowMathInput input = normal_input();
  input.raw_systems.resize(1);
  input.raw_systems[0].H_f.setZero();

  const ov_msckf::CP2ShadowMathResult result = ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2FeaturePairResult &feature = result.features[0];
  EXPECT_TRUE(feature.nullspace.accepted());
  EXPECT_FALSE(feature.schur.accepted());
  EXPECT_TRUE(feature.nullspace_gate.evidence_decision_available);
  EXPECT_FALSE(feature.schur_gate.evidence_decision_available);
  EXPECT_EQ(feature.agreement_class, ov_msckf::CP2GateAgreementClass::kNullspaceOnly);
  EXPECT_EQ(feature.raw_row_match_weight, 0U);
  EXPECT_FALSE(feature.statistics_comparison_required);
  EXPECT_EQ(feature.lambda_comparison.status,
            ov_msckf::CP2StatisticComparisonStatus::kNotRequired);
  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{7}));
  EXPECT_TRUE(result.schur.accepted_ids.empty());
  EXPECT_TRUE(result.nullspace.proposal_available);
  EXPECT_FALSE(result.schur.proposal_available);
}

TEST(CP2ShadowMath, DuplicateFeatureIdentityInvalidatesButDoesNotShortCircuitMath) {
  ov_msckf::CP2ShadowMathInput input = normal_input();
  input.raw_systems[1].feature_id = input.raw_systems[0].feature_id;

  const ov_msckf::CP2ShadowMathResult result = ov_msckf::CP2ShadowMath::Process(std::move(input));
  EXPECT_FALSE(result.input_valid);
  EXPECT_TRUE(result.duplicate_feature_id);
  EXPECT_EQ(result.features.size(), 2U);
  EXPECT_EQ(result.nullspace.accepted_ids.size(), 2U);
  EXPECT_EQ(result.schur.accepted_ids.size(), 2U);
}

TEST(CP2ShadowMath, RawAndPriorLayoutDisconnectsAreStructurallyInvalid) {
  ov_msckf::CP2ShadowMathInput raw_disconnect = normal_input();
  raw_disconnect.raw_systems.resize(1);
  // These two slices cover H_x and are in bounds, disjoint, and contained by
  // the prior block, but neither is an exact active top-level prior block.
  raw_disconnect.raw_systems[0].jacobian_layout = {{0, 1, 0}, {1, 1, 1}};
  const ov_msckf::CP2ShadowMathResult bad_raw =
      ov_msckf::CP2ShadowMath::Process(std::move(raw_disconnect));
  EXPECT_FALSE(bad_raw.input_valid);
  ASSERT_EQ(bad_raw.features.size(), 1U);
  EXPECT_FALSE(bad_raw.features[0].raw_layout_valid);
  EXPECT_FALSE(bad_raw.features[0].nullspace_gate.evidence_decision_available);
  EXPECT_FALSE(bad_raw.features[0].schur_gate.evidence_decision_available);

  ov_msckf::CP2ShadowMathInput prior_disconnect = normal_input();
  prior_disconnect.prior.state_blocks = {{1, 2, 0}};
  const ov_msckf::CP2ShadowMathResult bad_prior =
      ov_msckf::CP2ShadowMath::Process(std::move(prior_disconnect));
  EXPECT_FALSE(bad_prior.input_valid);
  EXPECT_TRUE(bad_prior.features.empty());
}

TEST(CP2ShadowMath, NullspaceStatisticsFailureCannotChangeLiveLifecycleAcceptance) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0e100;
  input.chi_squared_table = finite_table(1.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0e154;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0e200;
  input.raw_systems.push_back(diagonal_raw(11, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result = ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2FeaturePairResult &feature = result.features[0];
  EXPECT_FALSE(feature.nullspace.accepted());
  EXPECT_TRUE(feature.nullspace.emitted_system_available);
  EXPECT_EQ(feature.nullspace.stage, ov_msckf::CP2NullspaceReductionStage::kStatistics);
  EXPECT_EQ(feature.nullspace_gate.stage,
            ov_msckf::CP2FeatureGateStage::kReductionUnavailable);
  EXPECT_FALSE(feature.nullspace_gate.evidence_decision_available);
  EXPECT_TRUE(feature.nullspace_gate.lifecycle_accept);
  EXPECT_FALSE(feature.schur.accepted());

  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{11}));
  EXPECT_EQ(result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_TRUE(result.nullspace.proposal_available);
  EXPECT_TRUE(result.schur.accepted_ids.empty());
  EXPECT_FALSE(result.schur.proposal_available);
}

TEST(CP2ShadowMath, CandidateGammaOverflowSuppressesOnlyCandidateProposal) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0e308;
  input.chi_squared_table = finite_table(1.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0e154;
  input.raw_systems.push_back(diagonal_raw(1, reduced_H, reduced_residual));
  input.raw_systems.push_back(diagonal_raw(2, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result = ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.nullspace.accepted_ids.size(), 2U);
  ASSERT_EQ(result.schur.accepted_ids.size(), 2U);
  EXPECT_EQ(result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_EQ(result.schur.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_EQ(result.nullspace.precompression_rows, 2);
  EXPECT_EQ(result.schur.precompression_rows, 2);
  EXPECT_EQ(result.nullspace.compressed_rows, 1);
  EXPECT_EQ(result.schur.compressed_rows, 0);
  EXPECT_TRUE(result.nullspace.proposal_available);
  EXPECT_FALSE(result.schur.proposal_available);
}

TEST(CP2ShadowMath, LambdaDiagnosticOverflowCannotRemoveBaselineGateAttempt) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.prior.covariance.setZero();
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(10.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0e154;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0;
  input.raw_systems.push_back(diagonal_raw(41, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2FeaturePairResult &feature = result.features[0];

  ASSERT_TRUE(feature.nullspace.accepted());
  EXPECT_TRUE(feature.nullspace.emitted_system_available);
  EXPECT_TRUE(feature.nullspace.statistics.raw_lambda_symmetry_error_available);
  EXPECT_FALSE(feature.nullspace.statistics.lambda_available);
  EXPECT_TRUE(feature.nullspace.statistics.eta_available);
  EXPECT_TRUE(feature.nullspace.statistics.gamma_available);
  EXPECT_DOUBLE_EQ(feature.nullspace.statistics.gamma, 1.0);
  EXPECT_EQ(feature.nullspace_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
  EXPECT_TRUE(feature.nullspace_gate.evidence_decision_available);
  EXPECT_TRUE(feature.nullspace_gate.evidence_accept);

  EXPECT_FALSE(feature.schur.accepted());
  EXPECT_EQ(feature.agreement_class, ov_msckf::CP2GateAgreementClass::kNullspaceOnly);
  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{41}));
  EXPECT_TRUE(result.nullspace.proposal_available);
  EXPECT_TRUE(result.schur.accepted_ids.empty());
  EXPECT_FALSE(result.schur.proposal_available);
}

TEST(CP2ShadowMath, WhitenedJacobianOverflowCannotRemoveFiniteGammaBaselineGate) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.prior.covariance.setZero();
  input.sigma_px = 1.0e-154;
  input.sigma_px_sq = input.sigma_px * input.sigma_px;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(10.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0e155;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0e-154;
  input.raw_systems.push_back(diagonal_raw(43, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2FeaturePairResult &feature = result.features[0];

  ASSERT_TRUE(feature.nullspace.accepted());
  EXPECT_FALSE(feature.nullspace.statistics.raw_lambda_symmetry_error_available);
  EXPECT_FALSE(feature.nullspace.statistics.lambda_available);
  EXPECT_FALSE(feature.nullspace.statistics.eta_available);
  EXPECT_FALSE(feature.nullspace.statistics.gamma_available);
  EXPECT_TRUE(feature.nullspace.mode_gamma_available);
  EXPECT_DOUBLE_EQ(feature.nullspace.mode_gamma, 1.0);
  EXPECT_EQ(feature.nullspace_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
  EXPECT_TRUE(feature.nullspace_gate.evidence_decision_available);
  EXPECT_TRUE(feature.nullspace_gate.evidence_accept);
  EXPECT_FALSE(feature.schur.accepted());
  EXPECT_EQ(feature.agreement_class, ov_msckf::CP2GateAgreementClass::kNullspaceOnly);
  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{43}));
  EXPECT_DOUBLE_EQ(result.nullspace.retained_gamma, feature.nullspace.mode_gamma);
}

TEST(CP2ShadowMath, NonfiniteWhitenedResidualStopsDiagnosticPipelineInOrder) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.prior.covariance.setZero();
  input.sigma_px = 1.0e-154;
  input.sigma_px_sq = input.sigma_px * input.sigma_px;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(10.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0e-154;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0e155;
  input.raw_systems.push_back(diagonal_raw(47, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2NullspaceReductionResult &nullspace = result.features[0].nullspace;
  EXPECT_FALSE(nullspace.accepted());
  EXPECT_TRUE(nullspace.emitted_system_available);
  EXPECT_EQ(nullspace.stage, ov_msckf::CP2NullspaceReductionStage::kStatistics);
  EXPECT_FALSE(nullspace.statistics.raw_lambda_symmetry_error_available);
  EXPECT_FALSE(nullspace.statistics.lambda_available);
  EXPECT_FALSE(nullspace.statistics.eta_available);
  EXPECT_FALSE(nullspace.statistics.gamma_available);
  EXPECT_FALSE(nullspace.mode_gamma_available);
}

TEST(CP2ShadowMath, RawLambdaOverflowStopsEtaButFiniteGammaDefinesModeValidity) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.prior.covariance.setZero();
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(10.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0e200;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 1.0;
  input.raw_systems.push_back(diagonal_raw(53, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 1U);
  const ov_msckf::CP2FeaturePairResult &feature = result.features[0];
  ASSERT_TRUE(feature.nullspace.accepted());
  EXPECT_FALSE(feature.nullspace.statistics.raw_lambda_symmetry_error_available);
  EXPECT_FALSE(feature.nullspace.statistics.lambda_available);
  EXPECT_FALSE(feature.nullspace.statistics.eta_available);
  EXPECT_FALSE(feature.nullspace.statistics.gamma_available);
  EXPECT_TRUE(feature.nullspace.mode_gamma_available);
  EXPECT_DOUBLE_EQ(feature.nullspace.mode_gamma, 1.0);
  EXPECT_EQ(feature.nullspace_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
  EXPECT_TRUE(feature.nullspace_gate.evidence_decision_available);
  EXPECT_TRUE(feature.nullspace_gate.evidence_accept);
  EXPECT_FALSE(feature.schur.accepted());
  EXPECT_EQ(feature.agreement_class, ov_msckf::CP2GateAgreementClass::kNullspaceOnly);
  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{53}));
  EXPECT_DOUBLE_EQ(result.nullspace.retained_gamma, feature.nullspace.mode_gamma);
}

TEST(CP2ShadowMath, DifferentLocalBlockOrdersUseFirstSeenGlobalLayoutExactly) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior.covariance = Eigen::Matrix2d::Identity();
  input.prior.state_blocks = {{0, 1, 0}, {1, 1, 1}};
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0;
  input.chi_squared_table = finite_table(1.0e6);

  Eigen::Matrix2d first_H;
  first_H << 2.0, 3.0, 0.0, 0.0;
  Eigen::Vector2d first_residual;
  first_residual << 0.25, -0.5;
  ov_msckf::CP2RawFeatureSystem first = diagonal_raw(61, first_H, first_residual);
  // The first feature establishes global columns [covariance 1, covariance 0].
  first.jacobian_layout = {{1, 1, 0}, {0, 1, 1}};
  input.raw_systems.push_back(std::move(first));

  Eigen::Matrix<double, 1, 2> second_H;
  second_H << 7.0, 0.0;
  Eigen::VectorXd second_residual(1);
  second_residual << 0.125;
  ov_msckf::CP2RawFeatureSystem second = diagonal_raw(67, second_H, second_residual);
  // This feature's local columns are [covariance 0, covariance 1], so [7, 0]
  // must be scattered into the established global order as [0, 7].
  second.jacobian_layout = {{0, 1, 0}, {1, 1, 1}};
  input.raw_systems.push_back(std::move(second));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 2U);
  EXPECT_EQ(result.nullspace.accepted_ids, (std::vector<std::uint64_t>{61, 67}));
  EXPECT_EQ(result.schur.accepted_ids, (std::vector<std::uint64_t>{61, 67}));

  Eigen::Matrix2d expected_H;
  expected_H << 2.0, 3.0, 0.0, 7.0;
  Eigen::Vector2d expected_residual;
  // The third row makes compression nontrivial: the exact Givens rotation for
  // p=0,q=7 moves its residual into retained row one and discards the prior
  // zero-Jacobian row's residual-only energy.
  expected_residual << 0.25, 0.125;
  const ov_msckf::CP2GlobalModeResult *globals[] = {&result.nullspace, &result.schur};
  for (const ov_msckf::CP2GlobalModeResult *global : globals) {
    ASSERT_EQ(global->precompression_rows, 3);
    ASSERT_EQ(global->compressed_rows, 2);
    ASSERT_EQ(global->jacobian_layout.size(), 2U);
    EXPECT_EQ(global->jacobian_layout[0].covariance_id, 1);
    EXPECT_EQ(global->jacobian_layout[0].size, 1);
    EXPECT_EQ(global->jacobian_layout[0].offset, 0);
    EXPECT_EQ(global->jacobian_layout[1].covariance_id, 0);
    EXPECT_EQ(global->jacobian_layout[1].size, 1);
    EXPECT_EQ(global->jacobian_layout[1].offset, 1);
    expect_coefficient_bits_equal(global->H_compressed, expected_H);
    expect_coefficient_bits_equal(global->residual_compressed, expected_residual);
    EXPECT_TRUE(global->proposal_available);
  }
}

TEST(CP2ShadowMath, CandidateGammaOverflowStillTraversesAndStacksLaterFeatures) {
  ov_msckf::CP2ShadowMathInput input;
  input.prior = prior_snapshot(1);
  input.sigma_px = 1.0;
  input.sigma_px_sq = 1.0;
  input.chi2_multiplier = 1.0e308;
  input.chi_squared_table = finite_table(1.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0;
  Eigen::VectorXd overflowing_residual(1);
  overflowing_residual << 1.0e154;
  Eigen::VectorXd later_residual(1);
  later_residual << 1.0;
  input.raw_systems.push_back(diagonal_raw(71, reduced_H, overflowing_residual));
  input.raw_systems.push_back(diagonal_raw(73, reduced_H, overflowing_residual));
  input.raw_systems.push_back(diagonal_raw(79, reduced_H, later_residual));

  const ov_msckf::CP2ShadowMathResult result =
      ov_msckf::CP2ShadowMath::Process(std::move(input));
  ASSERT_TRUE(result.input_valid);
  ASSERT_EQ(result.features.size(), 3U);
  const ov_msckf::CP2FeaturePairResult &later = result.features[2];
  EXPECT_TRUE(later.nullspace.accepted());
  EXPECT_TRUE(later.schur.accepted());
  EXPECT_EQ(later.nullspace_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
  EXPECT_EQ(later.schur_gate.stage, ov_msckf::CP2FeatureGateStage::kDecision);
  EXPECT_TRUE(later.nullspace_gate.evidence_accept);
  EXPECT_TRUE(later.schur_gate.evidence_accept);
  EXPECT_EQ(later.agreement_class, ov_msckf::CP2GateAgreementClass::kBothMatchAccept);

  const std::vector<std::uint64_t> expected_ids{71, 73, 79};
  EXPECT_EQ(result.nullspace.accepted_ids, expected_ids);
  EXPECT_EQ(result.schur.accepted_ids, expected_ids);
  EXPECT_EQ(result.nullspace.precompression_rows, 3);
  EXPECT_EQ(result.schur.precompression_rows, 3);
  EXPECT_EQ(result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_EQ(result.schur.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_TRUE(std::isnan(result.nullspace.retained_gamma));
  EXPECT_TRUE(std::isnan(result.schur.retained_gamma));

  // Baseline compression remains lifecycle-authoritative despite diagnostic
  // overflow. Candidate compression and preview stay suppressed, even though
  // the later feature was fully reduced, gated, accepted, and stacked.
  EXPECT_EQ(result.nullspace.compressed_rows, 1);
  EXPECT_TRUE(result.nullspace.proposal_available);
  EXPECT_EQ(result.schur.compressed_rows, 0);
  EXPECT_EQ(result.schur.H_compressed.size(), 0);
  EXPECT_EQ(result.schur.residual_compressed.size(), 0);
  EXPECT_FALSE(result.schur.proposal_available);
}

TEST(CP2ShadowMath, EmptyAndAllRejectedGammaStatesAreExact) {
  ov_msckf::CP2ShadowMathInput empty = normal_input();
  empty.raw_systems.clear();
  const ov_msckf::CP2ShadowMathResult empty_result =
      ov_msckf::CP2ShadowMath::Process(std::move(empty));
  ASSERT_TRUE(empty_result.input_valid);
  EXPECT_TRUE(empty_result.features.empty());
  EXPECT_EQ(empty_result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kNotReached);
  EXPECT_EQ(empty_result.schur.gamma_status, ov_msckf::CP2GammaStatus::kNotReached);
  EXPECT_TRUE(std::isnan(empty_result.nullspace.retained_gamma));
  EXPECT_TRUE(std::isnan(empty_result.schur.retained_gamma));

  ov_msckf::CP2ShadowMathInput all_rejected;
  all_rejected.prior = prior_snapshot(1);
  all_rejected.prior.covariance.setZero();
  all_rejected.sigma_px = 1.0;
  all_rejected.sigma_px_sq = 1.0;
  all_rejected.chi2_multiplier = 1.0;
  all_rejected.chi_squared_table = finite_table(1.0);
  Eigen::Matrix<double, 1, 1> reduced_H;
  reduced_H << 1.0;
  Eigen::VectorXd reduced_residual(1);
  reduced_residual << 2.0;
  all_rejected.raw_systems.push_back(diagonal_raw(83, reduced_H, reduced_residual));

  const ov_msckf::CP2ShadowMathResult rejected_result =
      ov_msckf::CP2ShadowMath::Process(std::move(all_rejected));
  ASSERT_TRUE(rejected_result.input_valid);
  ASSERT_EQ(rejected_result.features.size(), 1U);
  EXPECT_EQ(rejected_result.features[0].agreement_class,
            ov_msckf::CP2GateAgreementClass::kBothMatchReject);
  EXPECT_TRUE(rejected_result.nullspace.accepted_ids.empty());
  EXPECT_TRUE(rejected_result.schur.accepted_ids.empty());
  EXPECT_EQ(rejected_result.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_EQ(rejected_result.schur.gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_EQ(binary64_bits(rejected_result.nullspace.retained_gamma), binary64_bits(0.0));
  EXPECT_EQ(binary64_bits(rejected_result.schur.retained_gamma), binary64_bits(0.0));
  EXPECT_FALSE(rejected_result.nullspace.proposal_available);
  EXPECT_FALSE(rejected_result.schur.proposal_available);
}

TEST(CP2ShadowMath, StatisticComparisonFirstFailurePrecedenceIsExact) {
  using ov_msckf::CP2ShadowMath;
  using ov_msckf::CP2StatisticComparisonStatus;

  const Eigen::MatrixXd zero = Eigen::MatrixXd::Zero(1, 1);
  const Eigen::MatrixXd one = Eigen::MatrixXd::Ones(1, 1);
  EXPECT_EQ(CP2ShadowMath::CompareMatrixStatistic(false, zero, false, zero).status,
            CP2StatisticComparisonStatus::kReferenceUnavailable);
  EXPECT_EQ(CP2ShadowMath::CompareMatrixStatistic(true, zero, false, zero).status,
            CP2StatisticComparisonStatus::kCandidateUnavailable);
  EXPECT_EQ(CP2ShadowMath::CompareMatrixStatistic(
                true, Eigen::MatrixXd::Zero(1, 2), true, zero)
                .status,
            CP2StatisticComparisonStatus::kCandidateUnavailable);

  Eigen::MatrixXd norm_overflow(1, 2);
  norm_overflow << std::numeric_limits<double>::max(),
      std::numeric_limits<double>::max();
  EXPECT_EQ(CP2ShadowMath::CompareMatrixStatistic(
                true, norm_overflow, true, norm_overflow)
                .status,
            CP2StatisticComparisonStatus::kReferenceNormNonfinite);

  Eigen::MatrixXd positive_max(1, 1);
  positive_max << std::numeric_limits<double>::max();
  EXPECT_EQ(CP2ShadowMath::CompareMatrixStatistic(
                true, zero, true, positive_max)
                .status,
            CP2StatisticComparisonStatus::kErrorNonfinite);

  const ov_msckf::CP2StatisticComparison available =
      CP2ShadowMath::CompareMatrixStatistic(true, zero, true, one);
  EXPECT_EQ(available.status, CP2StatisticComparisonStatus::kAvailable);
  EXPECT_TRUE(available.available);
  EXPECT_FALSE(available.passed);
  EXPECT_DOUBLE_EQ(available.reference_norm, 0.0);
  EXPECT_DOUBLE_EQ(available.error, 1.0);
  EXPECT_DOUBLE_EQ(available.tolerance,
                   CP2ShadowMath::kStatisticsAbsoluteTolerance);

  Eigen::VectorXd vector_zero = Eigen::VectorXd::Zero(1);
  Eigen::VectorXd vector_one = Eigen::VectorXd::Ones(1);
  EXPECT_EQ(CP2ShadowMath::CompareVectorStatistic(
                true, vector_zero, true, vector_one)
                .status,
            CP2StatisticComparisonStatus::kAvailable);
  EXPECT_EQ(CP2ShadowMath::CompareScalarStatistic(
                true, -std::numeric_limits<double>::max(), true,
                std::numeric_limits<double>::max())
                .status,
            CP2StatisticComparisonStatus::kErrorNonfinite);
  EXPECT_EQ(CP2ShadowMath::CompareScalarStatistic(
                true, 0.0, true, std::numeric_limits<double>::max())
                .status,
            CP2StatisticComparisonStatus::kRatioNonfinite);
}

} // namespace
