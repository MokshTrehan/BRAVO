/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2026 Moksh Trehan
 * Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 */

#ifndef OV_MSCKF_CP2_SHADOW_MATH_H
#define OV_MSCKF_CP2_SHADOW_MATH_H

#include "CP2FeatureGate.h"
#include "SchurUpdate.h"
#include "UpdaterMSCKFPreview.h"

#include <Eigen/Core>

#include <cstdint>
#include <limits>
#include <vector>

namespace ov_msckf {

/// Frozen value-only nullspace evidence status.
enum class CP2NullspaceReductionStatus { kAccepted, kInvalidDimensions, kNonfinite };

/// Frozen value-only nullspace evidence stage.
enum class CP2NullspaceReductionStage { kInputDimensions, kGivensOutput, kStatistics, kAccepted };

const char *cp2_nullspace_reduction_status_name(CP2NullspaceReductionStatus status) noexcept;
const char *cp2_nullspace_reduction_stage_name(CP2NullspaceReductionStage stage) noexcept;

/// Exact first-failure status for one CP2 sufficient-statistic comparison.
enum class CP2StatisticComparisonStatus {
  kNotRequired,
  kAvailable,
  kReferenceUnavailable,
  kCandidateUnavailable,
  kReferenceNormNonfinite,
  kErrorNonfinite,
  kToleranceNonfinite,
  kRatioNonfinite,
};

const char *cp2_statistic_comparison_status_name(CP2StatisticComparisonStatus status) noexcept;

/// Exhaustive official gate-agreement classification for one raw system.
enum class CP2GateAgreementClass {
  kBothMatchAccept,
  kBothMatchReject,
  kBooleanNullspaceAcceptSchurReject,
  kBooleanNullspaceRejectSchurAccept,
  kNullspaceOnly,
  kSchurOnly,
  kNeitherDecision,
};

const char *cp2_gate_agreement_class_name(CP2GateAgreementClass agreement) noexcept;

/// Baseline evidence accumulated independently of lifecycle decisions.
enum class CP2GammaStatus { kNotReached, kAvailable, kNonfinite };

const char *cp2_gamma_status_name(CP2GammaStatus status) noexcept;

/**
 * @brief One owning shared-raw-seam feature system.
 *
 * The type deliberately contains no State, Type, Feature, or caller-owned
 * pointer. Layout offsets cover H_x columns in the order emitted by
 * UpdaterHelper::get_feature_jacobian_full.
 */
struct CP2RawFeatureSystem {
  std::uint64_t feature_id = 0;
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
  std::vector<CP2FeatureGateLayoutBlock> jacobian_layout;
};

/// Whitened basis-invariant sufficient statistics for one reduced system.
struct CP2SufficientStatistics {
  bool raw_lambda_symmetry_error_available = false;
  bool lambda_available = false;
  bool eta_available = false;
  bool gamma_available = false;
  Eigen::MatrixXd lambda;
  Eigen::VectorXd eta;
  double gamma = std::numeric_limits<double>::quiet_NaN();
  double raw_lambda_symmetry_error_inf = std::numeric_limits<double>::quiet_NaN();
};

/// Value-only production-Givens reduction and its evidence validity.
struct CP2NullspaceReductionResult {
  CP2NullspaceReductionStatus status = CP2NullspaceReductionStatus::kInvalidDimensions;
  CP2NullspaceReductionStage stage = CP2NullspaceReductionStage::kInputDimensions;
  Eigen::Index raw_rows = 0;
  Eigen::Index reduced_rows = 0;
  bool emitted_system_available = false;
  Eigen::MatrixXd H_reduced;
  Eigen::VectorXd residual_reduced;
  bool mode_gamma_available = false;
  double mode_gamma = std::numeric_limits<double>::quiet_NaN();
  CP2SufficientStatistics statistics;
  CP2FeatureGateCounters counters;

  /// Mode-valid means q finite emitted rows and a finite whitened gamma.
  /// Lambda/eta are independent diagnostics and cannot veto this result.
  bool accepted() const noexcept { return status == CP2NullspaceReductionStatus::kAccepted; }
};

/// Retained arithmetic for one Lambda, eta, or gamma comparison.
struct CP2StatisticComparison {
  CP2StatisticComparisonStatus status = CP2StatisticComparisonStatus::kNotRequired;
  bool available = false;
  double reference_norm = std::numeric_limits<double>::quiet_NaN();
  double error = std::numeric_limits<double>::quiet_NaN();
  double tolerance = std::numeric_limits<double>::quiet_NaN();
  double ratio = std::numeric_limits<double>::quiet_NaN();
  bool passed = false;
};

/// Complete pure-math result for one shared raw feature system.
struct CP2FeaturePairResult {
  std::uint64_t feature_id = 0;
  Eigen::Index raw_rows = 0;
  bool raw_layout_valid = false;
  CP2NullspaceReductionResult nullspace;
  SchurReductionResult schur;
  CP2FeatureGateResult nullspace_gate;
  CP2FeatureGateResult schur_gate;
  bool statistics_comparison_required = false;
  CP2StatisticComparison lambda_comparison;
  CP2StatisticComparison eta_comparison;
  CP2StatisticComparison gamma_comparison;
  CP2GateAgreementClass agreement_class = CP2GateAgreementClass::kNeitherDecision;
  std::uint64_t raw_row_match_weight = 0;
};

/// One independently assembled, compressed, read-only global mode proposal.
struct CP2GlobalModeResult {
  std::vector<std::uint64_t> accepted_ids;
  CP2GammaStatus gamma_status = CP2GammaStatus::kNotReached;
  double retained_gamma = std::numeric_limits<double>::quiet_NaN();
  Eigen::Index precompression_rows = 0;
  Eigen::Index compressed_rows = 0;
  Eigen::MatrixXd H_compressed;
  Eigen::VectorXd residual_compressed;
  std::vector<MSCKFUpdatePreviewBlock> jacobian_layout;
  bool proposal_available = false;
  MSCKFUpdatePreviewResult proposal;
};

/// Owning input to the deterministic live/offline CP2 raw-to-proposal kernel.
struct CP2ShadowMathInput {
  MSCKFUpdatePreviewSnapshot prior;
  std::vector<CP2RawFeatureSystem> raw_systems;
  double sigma_px = std::numeric_limits<double>::quiet_NaN();
  double sigma_px_sq = std::numeric_limits<double>::quiet_NaN();
  double chi2_multiplier = std::numeric_limits<double>::quiet_NaN();
  CP2FeatureGate::ChiSquaredTable chi_squared_table;
};

/// Complete value-only raw-to-proposal output for both independent paths.
struct CP2ShadowMathResult {
  bool input_valid = false;
  bool duplicate_feature_id = false;
  std::vector<CP2FeaturePairResult> features;
  CP2GlobalModeResult nullspace;
  CP2GlobalModeResult schur;
};

/**
 * @brief Strict-FP, value-only CP2 reduction/gate/assembly/proposal kernel.
 *
 * The kernel never commits a proposal and has no access to live estimator
 * objects. The same function is intended for the online shadow and the
 * independent offline raw-to-proposal replay.
 */
class CP2ShadowMath {
public:
  static constexpr double kStatisticsAbsoluteTolerance = 1.0e-10;
  static constexpr double kStatisticsRelativeTolerance = 1.0e-8;

  /// Public value-only entry points for the frozen comparison state machine.
  /// Process uses these same functions; tests and replay do not carry a
  /// second implementation of first-failure precedence.
  static CP2StatisticComparison CompareMatrixStatistic(
      bool reference_available, const Eigen::MatrixXd &reference,
      bool candidate_available, const Eigen::MatrixXd &candidate);
  static CP2StatisticComparison CompareVectorStatistic(
      bool reference_available, const Eigen::VectorXd &reference,
      bool candidate_available, const Eigen::VectorXd &candidate);
  static CP2StatisticComparison CompareScalarStatistic(
      bool reference_available, double reference, bool candidate_available,
      double candidate);

  static CP2ShadowMathResult Process(CP2ShadowMathInput input);
};

} // namespace ov_msckf

#endif // OV_MSCKF_CP2_SHADOW_MATH_H
