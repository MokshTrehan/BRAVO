/*
 * SchurVIO-Lite exact CP2 per-feature gate tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "update/CP2FeatureGate.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <array>
#include <cmath>
#include <limits>
#include <map>
#include <utility>
#include <vector>

namespace {

using ov_msckf::CP2FeatureGate;
using ov_msckf::CP2FeatureGateInput;
using ov_msckf::CP2FeatureGateLayoutBlock;
using ov_msckf::CP2FeatureGateResult;
using ov_msckf::CP2FeatureGateStage;

CP2FeatureGateResult evaluate_noise_only(const Eigen::VectorXd &residual, double sigma_px_sq,
                                         double chi2_multiplier, const CP2FeatureGate::ChiSquaredTable &table) {
  const Eigen::MatrixXd H = Eigen::MatrixXd::Zero(residual.rows(), 0);
  return CP2FeatureGate::Evaluate(
      CP2FeatureGateInput(H, residual, Eigen::MatrixXd(0, 0), {}, sigma_px_sq, chi2_multiplier), table);
}

void expect_zero_counters(const CP2FeatureGateResult &result) {
  EXPECT_EQ(result.counters.jitter_count, 0u);
  EXPECT_EQ(result.counters.repair_count, 0u);
  EXPECT_EQ(result.counters.alternate_solve_count, 0u);
  EXPECT_EQ(result.counters.clamp_count, 0u);
  EXPECT_EQ(result.counters.regularization_count, 0u);
  EXPECT_EQ(result.counters.silent_fallback_count, 0u);
  EXPECT_EQ(result.counters.fallback_count, 0u);
}

TEST(CP2FeatureGate, StageNamesAreFrozen) {
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kReductionUnavailable),
               "reduction_unavailable");
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kInnovationNonfinite),
               "innovation_nonfinite");
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kFactorizationFailed),
               "factorization_failed");
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kSolveOrNISNonfinite),
               "solve_or_nis_nonfinite");
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kThresholdNonfinite),
               "threshold_nonfinite");
  EXPECT_STREQ(ov_msckf::cp2_feature_gate_stage_name(CP2FeatureGateStage::kDecision), "decision");
}

TEST(CP2FeatureGate, ReductionUnavailablePublishesNoNumericOrDecisionEvidence) {
  const CP2FeatureGateResult result =
      CP2FeatureGate::Evaluate(CP2FeatureGateInput::ReductionUnavailable(), CP2FeatureGate::ChiSquaredTable{});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kReductionUnavailable);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_TRUE(std::isnan(result.chi2));
  EXPECT_FALSE(result.threshold_available);
  EXPECT_TRUE(std::isnan(result.threshold));
  EXPECT_FALSE(result.evidence_decision_available);
  EXPECT_FALSE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, GammaOverflowCanInvalidateEvidenceWithoutChangingBaselineLifecycleGate) {
  Eigen::MatrixXd H(1, 1);
  H << 1.0;
  Eigen::VectorXd residual(1);
  residual << 1.0e200;
  const Eigen::MatrixXd prior = Eigen::MatrixXd::Constant(1, 1, 1.0e300);
  const std::vector<CP2FeatureGateLayoutBlock> layout{{0, 1, 0}};

  // This is the adversarial baseline split: the observational whitened gamma
  // overflows, while the same emitted H/res has a finite accepting gate NIS.
  ASSERT_TRUE(std::isinf(residual.squaredNorm()));
  const CP2FeatureGateResult result = CP2FeatureGate::Evaluate(
      CP2FeatureGateInput::EmittedEvidenceUnavailable(H, residual, prior, layout, 1.0, 1.0), {{1, 2.0e100}});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kReductionUnavailable);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_TRUE(std::isnan(result.chi2));
  EXPECT_FALSE(result.threshold_available);
  EXPECT_TRUE(std::isnan(result.threshold));
  EXPECT_FALSE(result.evidence_decision_available);
  EXPECT_TRUE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, EvidenceUnavailableEmittedSystemRetainsTheNormalLifecycleDecision) {
  Eigen::VectorXd residual(1);
  residual << 2.0;
  const Eigen::MatrixXd H = Eigen::MatrixXd::Zero(1, 0);
  const Eigen::MatrixXd prior(0, 0);
  const std::vector<CP2FeatureGateLayoutBlock> layout;
  const CP2FeatureGate::ChiSquaredTable table{{1, 4.0}};

  const CP2FeatureGateResult normal =
      CP2FeatureGate::Evaluate(CP2FeatureGateInput(H, residual, prior, layout, 1.0, 1.0), table);
  const CP2FeatureGateResult evidence_unavailable = CP2FeatureGate::Evaluate(
      CP2FeatureGateInput::EmittedEvidenceUnavailable(H, residual, prior, layout, 1.0, 1.0), table);

  ASSERT_EQ(normal.stage, CP2FeatureGateStage::kDecision);
  ASSERT_TRUE(normal.chi2_available);
  ASSERT_TRUE(normal.threshold_available);
  ASSERT_TRUE(normal.evidence_decision_available);
  EXPECT_TRUE(normal.evidence_accept);
  EXPECT_TRUE(normal.lifecycle_accept);

  EXPECT_EQ(evidence_unavailable.stage, CP2FeatureGateStage::kReductionUnavailable);
  EXPECT_FALSE(evidence_unavailable.chi2_available);
  EXPECT_TRUE(std::isnan(evidence_unavailable.chi2));
  EXPECT_FALSE(evidence_unavailable.threshold_available);
  EXPECT_TRUE(std::isnan(evidence_unavailable.threshold));
  EXPECT_FALSE(evidence_unavailable.evidence_decision_available);
  EXPECT_EQ(evidence_unavailable.lifecycle_accept, normal.lifecycle_accept);
  expect_zero_counters(normal);
  expect_zero_counters(evidence_unavailable);
}

TEST(CP2FeatureGate, MarginalIsCopiedFromImmutablePriorInDeclaredLayoutOrder) {
  Eigen::MatrixXd prior = Eigen::MatrixXd::Zero(4, 4);
  prior.diagonal() << 1.0, 2.0, 3.0, 4.0;
  Eigen::MatrixXd H(1, 4);
  H << 1.0, 2.0, 3.0, 4.0;
  Eigen::VectorXd residual(1);
  residual << 1.0;
  std::vector<CP2FeatureGateLayoutBlock> layout;
  layout.emplace_back(2, 2, 0);
  layout.emplace_back(0, 2, 2);

  const CP2FeatureGateResult result = CP2FeatureGate::Evaluate(
      CP2FeatureGateInput(H, residual, prior, layout, 1.0, 1.0), CP2FeatureGate::ChiSquaredTable{{1, 1.0}});

  ASSERT_EQ(result.stage, CP2FeatureGateStage::kDecision);
  ASSERT_TRUE(result.chi2_available);
  EXPECT_NEAR(result.chi2, 1.0 / 61.0, 1.0e-16);
  EXPECT_TRUE(result.evidence_accept);
  EXPECT_TRUE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, InvalidOrOverlappingLayoutCannotFormInnovation) {
  Eigen::MatrixXd prior = Eigen::MatrixXd::Identity(3, 3);
  Eigen::MatrixXd H = Eigen::MatrixXd::Zero(1, 2);
  Eigen::VectorXd residual = Eigen::VectorXd::Zero(1);
  std::vector<CP2FeatureGateLayoutBlock> overlapping;
  overlapping.emplace_back(0, 1, 0);
  overlapping.emplace_back(0, 1, 1);

  const CP2FeatureGateResult result = CP2FeatureGate::Evaluate(
      CP2FeatureGateInput(H, residual, prior, overlapping, 1.0, 1.0), CP2FeatureGate::ChiSquaredTable{{1, 1.0}});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kInnovationNonfinite);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_FALSE(result.evidence_decision_available);
  EXPECT_FALSE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, NonfiniteInnovationPrecedesFactorization) {
  Eigen::VectorXd residual(1);
  residual << 0.0;
  const CP2FeatureGateResult result =
      evaluate_noise_only(residual, std::numeric_limits<double>::quiet_NaN(), 1.0, {{1, 1.0}});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kInnovationNonfinite);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_FALSE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, NegativeVarianceIsRejectedBeforeFactorization) {
  Eigen::VectorXd residual(1);
  residual << 0.0;
  const CP2FeatureGateResult result = evaluate_noise_only(residual, -1.0, 1.0, {{1, 1.0}});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kInnovationNonfinite);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_FALSE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate,
     InvalidVarianceCannotBeMaskedByPositiveStateCovariance) {
  Eigen::MatrixXd H(1, 1);
  H << 1.0;
  Eigen::VectorXd residual(1);
  residual << 1.0;
  const Eigen::MatrixXd prior = Eigen::MatrixXd::Constant(1, 1, 4.0);
  const std::vector<CP2FeatureGateLayoutBlock> layout{{0, 1, 0}};
  const std::array<double, 5> invalid_variances{{
      -1.0,
      0.0,
      std::numeric_limits<double>::quiet_NaN(),
      std::numeric_limits<double>::infinity(),
      -std::numeric_limits<double>::infinity(),
  }};

  for (const double sigma_px_sq : invalid_variances) {
    SCOPED_TRACE(::testing::Message() << "sigma_px_sq=" << sigma_px_sq);
    const CP2FeatureGateResult result = CP2FeatureGate::Evaluate(
        CP2FeatureGateInput(H, residual, prior, layout, sigma_px_sq, 1.0),
        {{1, 100.0}});

    EXPECT_EQ(result.stage, CP2FeatureGateStage::kInnovationNonfinite);
    EXPECT_FALSE(result.chi2_available);
    EXPECT_FALSE(result.threshold_available);
    EXPECT_FALSE(result.evidence_decision_available);
    EXPECT_FALSE(result.evidence_accept);
    EXPECT_FALSE(result.lifecycle_accept);
    expect_zero_counters(result);
  }
}

TEST(CP2FeatureGate, NonfiniteDotIsSolveOrNISFailure) {
  Eigen::VectorXd residual(1);
  residual << std::numeric_limits<double>::max();
  const CP2FeatureGateResult result = evaluate_noise_only(residual, 1.0, 1.0, {{1, 1.0}});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kSolveOrNISNonfinite);
  EXPECT_FALSE(result.chi2_available);
  EXPECT_FALSE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, EqualityIsAcceptedAndStrictExcessRejected) {
  Eigen::VectorXd equality_residual(1);
  equality_residual << 2.0;
  const CP2FeatureGateResult equality = evaluate_noise_only(equality_residual, 1.0, 1.0, {{1, 4.0}});
  ASSERT_EQ(equality.stage, CP2FeatureGateStage::kDecision);
  EXPECT_DOUBLE_EQ(equality.chi2, 4.0);
  EXPECT_DOUBLE_EQ(equality.threshold, 4.0);
  EXPECT_TRUE(equality.evidence_decision_available);
  EXPECT_TRUE(equality.evidence_accept);
  EXPECT_TRUE(equality.lifecycle_accept);

  Eigen::VectorXd excess_residual(1);
  excess_residual << 3.0;
  const CP2FeatureGateResult excess = evaluate_noise_only(excess_residual, 1.0, 1.0, {{1, 4.0}});
  ASSERT_EQ(excess.stage, CP2FeatureGateStage::kDecision);
  EXPECT_DOUBLE_EQ(excess.chi2, 9.0);
  EXPECT_FALSE(excess.evidence_accept);
  EXPECT_FALSE(excess.lifecycle_accept);
  expect_zero_counters(equality);
  expect_zero_counters(excess);
}

TEST(CP2FeatureGate, NonfiniteThresholdNullsEvidenceButPreservesIEEEComparison) {
  Eigen::VectorXd residual(1);
  residual << 2.0;

  const CP2FeatureGateResult nan_threshold =
      evaluate_noise_only(residual, 1.0, 1.0, {{1, std::numeric_limits<double>::quiet_NaN()}});
  ASSERT_EQ(nan_threshold.stage, CP2FeatureGateStage::kThresholdNonfinite);
  EXPECT_TRUE(nan_threshold.chi2_available);
  EXPECT_FALSE(nan_threshold.threshold_available);
  EXPECT_FALSE(nan_threshold.evidence_decision_available);
  EXPECT_TRUE(nan_threshold.lifecycle_accept);

  const CP2FeatureGateResult negative_infinity_threshold =
      evaluate_noise_only(residual, 1.0, 1.0, {{1, -std::numeric_limits<double>::infinity()}});
  ASSERT_EQ(negative_infinity_threshold.stage, CP2FeatureGateStage::kThresholdNonfinite);
  EXPECT_TRUE(negative_infinity_threshold.chi2_available);
  EXPECT_FALSE(negative_infinity_threshold.threshold_available);
  EXPECT_FALSE(negative_infinity_threshold.evidence_decision_available);
  EXPECT_FALSE(negative_infinity_threshold.lifecycle_accept);
  expect_zero_counters(nan_threshold);
  expect_zero_counters(negative_infinity_threshold);
}

TEST(CP2FeatureGate, MissingConstructorTableEntryIsUnavailableNotSubstituted) {
  Eigen::VectorXd residual(1);
  residual << 1.0;
  const CP2FeatureGateResult result = evaluate_noise_only(residual, 1.0, 1.0, {});

  EXPECT_EQ(result.stage, CP2FeatureGateStage::kThresholdNonfinite);
  EXPECT_TRUE(result.chi2_available);
  EXPECT_FALSE(result.threshold_available);
  EXPECT_FALSE(result.evidence_decision_available);
  EXPECT_TRUE(result.lifecycle_accept);
  expect_zero_counters(result);
}

TEST(CP2FeatureGate, FiveHundredRowsUseDynamicBoostQuantile) {
  const Eigen::VectorXd residual = Eigen::VectorXd::Zero(500);
  const CP2FeatureGateResult result = evaluate_noise_only(residual, 1.0, 1.0, {});

  ASSERT_EQ(result.stage, CP2FeatureGateStage::kDecision);
  EXPECT_TRUE(result.chi2_available);
  EXPECT_DOUBLE_EQ(result.chi2, 0.0);
  EXPECT_TRUE(result.threshold_available);
  EXPECT_TRUE(std::isfinite(result.threshold));
  EXPECT_GT(result.threshold, 0.0);
  EXPECT_TRUE(result.evidence_decision_available);
  EXPECT_TRUE(result.evidence_accept);
  EXPECT_TRUE(result.lifecycle_accept);
  expect_zero_counters(result);
}

} // namespace
