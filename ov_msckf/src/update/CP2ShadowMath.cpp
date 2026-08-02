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

#include "CP2ShadowMath.h"

#include "UpdaterHelper.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <set>
#include <type_traits>
#include <utility>

namespace ov_msckf {
namespace {

bool checked_add_index(Eigen::Index left, Eigen::Index right,
                       Eigen::Index &output) noexcept {
  if (left < 0 || right < 0 ||
      right > std::numeric_limits<Eigen::Index>::max() - left) {
    output = 0;
    return false;
  }
  output = left + right;
  return true;
}

bool checked_index_to_u64(Eigen::Index value,
                          std::uint64_t &output) noexcept {
  output = 0U;
  if (value < 0) {
    return false;
  }
  using UnsignedIndex = typename std::make_unsigned<Eigen::Index>::type;
  const UnsignedIndex unsigned_value = static_cast<UnsignedIndex>(value);
  if (std::numeric_limits<UnsignedIndex>::digits >
          std::numeric_limits<std::uint64_t>::digits &&
      unsigned_value > static_cast<UnsignedIndex>(
                           std::numeric_limits<std::uint64_t>::max())) {
    return false;
  }
  output = static_cast<std::uint64_t>(unsigned_value);
  return true;
}

CP2NullspaceReductionResult reduce_nullspace(const CP2RawFeatureSystem &raw, double sigma_px) {
  CP2NullspaceReductionResult result;
  result.raw_rows = raw.H_f.rows();

  const Eigen::Index m = raw.H_f.rows();
  if (raw.H_f.cols() != 3 || raw.H_x.rows() != m || raw.residual.rows() != m || m <= 3) {
    return result;
  }

  Eigen::MatrixXd H_f_work = raw.H_f;
  result.H_reduced = raw.H_x;
  result.residual_reduced = raw.residual;
  UpdaterHelper::nullspace_project_inplace(H_f_work, result.H_reduced, result.residual_reduced);

  result.reduced_rows = result.H_reduced.rows();
  if (result.reduced_rows != m - 3 || result.residual_reduced.rows() != m - 3 ||
      result.H_reduced.cols() != raw.H_x.cols()) {
    result.status = CP2NullspaceReductionStatus::kNonfinite;
    result.stage = CP2NullspaceReductionStage::kGivensOutput;
    result.H_reduced.resize(0, 0);
    result.residual_reduced.resize(0);
    result.reduced_rows = 0;
    return result;
  }

  // A correctly shaped Givens output still exists for the unchanged live
  // lifecycle even if an observational finite/statistics check below fails.
  result.emitted_system_available = true;
  if (!result.H_reduced.allFinite() || !result.residual_reduced.allFinite()) {
    result.status = CP2NullspaceReductionStatus::kNonfinite;
    result.stage = CP2NullspaceReductionStage::kGivensOutput;
    return result;
  }

  // Frozen direct elementwise whitening. Do not form or multiply by the
  // reciprocal variance: that is observably different at binary64 edges.
  const Eigen::MatrixXd A = result.H_reduced.array() / sigma_px;
  const Eigen::VectorXd b = result.residual_reduced.array() / sigma_px;
  const bool A_finite = A.allFinite();
  const bool b_finite = b.allFinite();
  bool evidence_statistics_prerequisites_available = false;

  // Lambda, eta, and gamma are independent evidence diagnostics. In
  // particular, an overflow in Lambda symmetrization must not turn a finite
  // production Givens system and finite whitened gamma into a new live
  // baseline rejection. Preserve the frozen operation order while retaining
  // availability for each statistic separately.
  if (A_finite && b_finite) {
    const Eigen::MatrixXd raw_lambda = A.transpose() * A;
    const double raw_symmetry_error =
        raw_lambda.size() == 0
            ? 0.0
            : (raw_lambda - raw_lambda.transpose()).cwiseAbs().rowwise().sum().maxCoeff();
    if (raw_lambda.allFinite() && std::isfinite(raw_symmetry_error)) {
      evidence_statistics_prerequisites_available = true;
      result.statistics.raw_lambda_symmetry_error_available = true;
      result.statistics.raw_lambda_symmetry_error_inf = raw_symmetry_error;
      result.statistics.lambda = 0.5 * (raw_lambda + raw_lambda.transpose());
      result.statistics.lambda_available = result.statistics.lambda.allFinite();
      if (!result.statistics.lambda_available) {
        result.statistics.lambda.resize(0, 0);
      }
      result.statistics.eta = A.transpose() * b;
      result.statistics.eta_available = result.statistics.eta.allFinite();
      if (!result.statistics.eta_available) {
        result.statistics.eta.resize(0);
      }
    }
  }

  if (b_finite) {
    result.mode_gamma = b.squaredNorm();
    result.mode_gamma_available = std::isfinite(result.mode_gamma);
    if (evidence_statistics_prerequisites_available && result.mode_gamma_available) {
      result.statistics.gamma = result.mode_gamma;
      result.statistics.gamma_available = true;
    }
  }
  if (!result.mode_gamma_available) {
    result.status = CP2NullspaceReductionStatus::kNonfinite;
    result.stage = CP2NullspaceReductionStage::kStatistics;
    return result;
  }

  result.status = CP2NullspaceReductionStatus::kAccepted;
  result.stage = CP2NullspaceReductionStage::kAccepted;
  return result;
}

CP2FeatureGateInput gate_input(const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
                               const CP2ShadowMathInput &input,
                               const std::vector<CP2FeatureGateLayoutBlock> &layout) {
  return CP2FeatureGateInput(H, residual, input.prior.covariance, layout, input.sigma_px_sq,
                             input.chi2_multiplier);
}

template <typename ReferenceDerived, typename CandidateDerived>
CP2StatisticComparison compare_eigen_statistic(bool reference_available,
                                                const Eigen::MatrixBase<ReferenceDerived> &reference,
                                                bool candidate_available,
                                                const Eigen::MatrixBase<CandidateDerived> &candidate) {
  CP2StatisticComparison result;
  if (!reference_available) {
    result.status = CP2StatisticComparisonStatus::kReferenceUnavailable;
    return result;
  }
  if (!candidate_available || reference.rows() != candidate.rows() || reference.cols() != candidate.cols()) {
    result.status = CP2StatisticComparisonStatus::kCandidateUnavailable;
    return result;
  }

  const double reference_norm = reference.norm();
  if (!std::isfinite(reference_norm)) {
    result.status = CP2StatisticComparisonStatus::kReferenceNormNonfinite;
    return result;
  }

  const auto difference = (candidate - reference).eval();
  if (!difference.allFinite()) {
    result.status = CP2StatisticComparisonStatus::kErrorNonfinite;
    return result;
  }
  const double error = difference.norm();
  if (!std::isfinite(error)) {
    result.status = CP2StatisticComparisonStatus::kErrorNonfinite;
    return result;
  }

  const double relative_term = CP2ShadowMath::kStatisticsRelativeTolerance * reference_norm;
  const double tolerance = CP2ShadowMath::kStatisticsAbsoluteTolerance + relative_term;
  if (!std::isfinite(tolerance)) {
    result.status = CP2StatisticComparisonStatus::kToleranceNonfinite;
    return result;
  }

  const double ratio = error / tolerance;
  if (!std::isfinite(ratio)) {
    result.status = CP2StatisticComparisonStatus::kRatioNonfinite;
    return result;
  }

  result.status = CP2StatisticComparisonStatus::kAvailable;
  result.available = true;
  result.reference_norm = reference_norm;
  result.error = error;
  result.tolerance = tolerance;
  result.ratio = ratio;
  result.passed = error <= tolerance;
  return result;
}

CP2StatisticComparison compare_scalar_statistic_impl(bool reference_available, double reference,
                                                      bool candidate_available, double candidate) {
  CP2StatisticComparison result;
  if (!reference_available) {
    result.status = CP2StatisticComparisonStatus::kReferenceUnavailable;
    return result;
  }
  if (!candidate_available) {
    result.status = CP2StatisticComparisonStatus::kCandidateUnavailable;
    return result;
  }

  const double reference_norm = std::abs(reference);
  if (!std::isfinite(reference_norm)) {
    result.status = CP2StatisticComparisonStatus::kReferenceNormNonfinite;
    return result;
  }

  const double difference = candidate - reference;
  if (!std::isfinite(difference)) {
    result.status = CP2StatisticComparisonStatus::kErrorNonfinite;
    return result;
  }
  const double error = std::abs(difference);
  if (!std::isfinite(error)) {
    result.status = CP2StatisticComparisonStatus::kErrorNonfinite;
    return result;
  }

  const double relative_term = CP2ShadowMath::kStatisticsRelativeTolerance * reference_norm;
  const double tolerance = CP2ShadowMath::kStatisticsAbsoluteTolerance + relative_term;
  if (!std::isfinite(tolerance)) {
    result.status = CP2StatisticComparisonStatus::kToleranceNonfinite;
    return result;
  }

  const double ratio = error / tolerance;
  if (!std::isfinite(ratio)) {
    result.status = CP2StatisticComparisonStatus::kRatioNonfinite;
    return result;
  }

  result.status = CP2StatisticComparisonStatus::kAvailable;
  result.available = true;
  result.reference_norm = reference_norm;
  result.error = error;
  result.tolerance = tolerance;
  result.ratio = ratio;
  result.passed = error <= tolerance;
  return result;
}

CP2GateAgreementClass classify_agreement(const CP2FeatureGateResult &nullspace,
                                         const CP2FeatureGateResult &schur) {
  if (nullspace.evidence_decision_available && schur.evidence_decision_available) {
    if (nullspace.evidence_accept == schur.evidence_accept) {
      return nullspace.evidence_accept ? CP2GateAgreementClass::kBothMatchAccept
                                       : CP2GateAgreementClass::kBothMatchReject;
    }
    return nullspace.evidence_accept ? CP2GateAgreementClass::kBooleanNullspaceAcceptSchurReject
                                     : CP2GateAgreementClass::kBooleanNullspaceRejectSchurAccept;
  }
  if (nullspace.evidence_decision_available) {
    return CP2GateAgreementClass::kNullspaceOnly;
  }
  if (schur.evidence_decision_available) {
    return CP2GateAgreementClass::kSchurOnly;
  }
  return CP2GateAgreementClass::kNeitherDecision;
}

bool matching_agreement(CP2GateAgreementClass agreement) {
  return agreement == CP2GateAgreementClass::kBothMatchAccept ||
         agreement == CP2GateAgreementClass::kBothMatchReject;
}

struct GlobalAssembler {
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  std::vector<MSCKFUpdatePreviewBlock> layout;
  std::map<Eigen::Index, std::size_t> covariance_id_to_layout;

  bool append(const Eigen::MatrixXd &local_H, const Eigen::VectorXd &local_residual,
              const std::vector<CP2FeatureGateLayoutBlock> &local_layout) {
    if (local_H.rows() <= 0 || local_residual.rows() != local_H.rows()) {
      return false;
    }

    Eigen::Index expected_local_offset = 0;
    for (const CP2FeatureGateLayoutBlock &block : local_layout) {
      if (block.covariance_id < 0 || block.size <= 0 || block.H_offset != expected_local_offset ||
          block.size > local_H.cols() - expected_local_offset) {
        return false;
      }
      Eigen::Index updated_local_offset = 0;
      if (!checked_add_index(expected_local_offset, block.size,
                             updated_local_offset)) {
        return false;
      }
      expected_local_offset = updated_local_offset;
    }
    if (expected_local_offset != local_H.cols()) {
      return false;
    }

    const Eigen::Index old_rows = H.rows();
    const Eigen::Index old_columns = H.cols();
    Eigen::Index new_columns = old_columns;
    for (const CP2FeatureGateLayoutBlock &block : local_layout) {
      const auto existing = covariance_id_to_layout.find(block.covariance_id);
      if (existing != covariance_id_to_layout.end()) {
        if (layout[existing->second].size != block.size) {
          return false;
        }
        continue;
      }

      Eigen::Index block_end = 0;
      if (!checked_add_index(block.covariance_id, block.size, block_end)) {
        return false;
      }
      for (const MSCKFUpdatePreviewBlock &known : layout) {
        Eigen::Index known_end = 0;
        if (!checked_add_index(known.covariance_id, known.size, known_end)) {
          return false;
        }
        if (block.covariance_id < known_end && known.covariance_id < block_end) {
          return false;
        }
      }
      covariance_id_to_layout.emplace(block.covariance_id, layout.size());
      layout.push_back({block.covariance_id, block.size, new_columns});
      Eigen::Index updated_columns = 0;
      if (!checked_add_index(new_columns, block.size, updated_columns)) {
        return false;
      }
      new_columns = updated_columns;
    }

    Eigen::Index new_rows = 0;
    if (!checked_add_index(old_rows, local_H.rows(), new_rows)) {
      return false;
    }
    H.conservativeResize(new_rows, new_columns);
    if (new_columns > old_columns && old_rows > 0) {
      H.block(0, old_columns, old_rows, new_columns - old_columns).setZero();
    }
    H.bottomRows(local_H.rows()).setZero();
    residual.conservativeResize(new_rows);
    residual.tail(local_residual.rows()) = local_residual;

    for (const CP2FeatureGateLayoutBlock &block : local_layout) {
      const MSCKFUpdatePreviewBlock &destination = layout[covariance_id_to_layout.at(block.covariance_id)];
      H.block(old_rows, destination.offset, local_H.rows(), block.size) =
          local_H.block(0, block.H_offset, local_H.rows(), block.size);
    }
    return true;
  }
};

bool startup_input_is_valid(const CP2ShadowMathInput &input) {
  if (!std::isfinite(input.sigma_px) || !(input.sigma_px > 0.0) ||
      !std::isfinite(input.sigma_px_sq) || !(input.sigma_px_sq > 0.0) ||
      input.sigma_px_sq != input.sigma_px * input.sigma_px ||
      !std::isfinite(input.chi2_multiplier)) {
    return false;
  }
  for (int degrees_of_freedom = 1; degrees_of_freedom < 500; ++degrees_of_freedom) {
    const auto entry = input.chi_squared_table.find(degrees_of_freedom);
    if (entry == input.chi_squared_table.end() || !std::isfinite(entry->second)) {
      return false;
    }
  }
  const Eigen::Index state_dimension = input.prior.covariance.rows();
  if (state_dimension <= 0 || input.prior.covariance.cols() != state_dimension ||
      !input.prior.covariance.allFinite() || input.prior.state_blocks.empty()) {
    return false;
  }

  Eigen::Index expected_offset = 0;
  for (const MSCKFUpdatePreviewBlock &block : input.prior.state_blocks) {
    if (block.covariance_id != expected_offset || block.offset != expected_offset || block.size <= 0 ||
        block.size > state_dimension - expected_offset) {
      return false;
    }
    Eigen::Index updated_offset = 0;
    if (!checked_add_index(expected_offset, block.size, updated_offset)) {
      return false;
    }
    expected_offset = updated_offset;
  }
  return expected_offset == state_dimension;
}

bool raw_layout_is_valid(const CP2RawFeatureSystem &raw,
                         const MSCKFUpdatePreviewSnapshot &prior) {
  Eigen::Index expected_H_offset = 0;
  for (std::size_t index = 0; index < raw.jacobian_layout.size(); ++index) {
    const CP2FeatureGateLayoutBlock &block = raw.jacobian_layout[index];
    if (block.covariance_id < 0 || block.size <= 0 || block.H_offset != expected_H_offset ||
        block.size > raw.H_x.cols() - expected_H_offset ||
        block.size > prior.covariance.rows() - block.covariance_id) {
      return false;
    }

    bool exact_top_level_match = false;
    for (const MSCKFUpdatePreviewBlock &state_block : prior.state_blocks) {
      if (block.covariance_id == state_block.covariance_id && block.size == state_block.size) {
        exact_top_level_match = true;
        break;
      }
    }
    if (!exact_top_level_match) {
      return false;
    }

    Eigen::Index block_end = 0;
    if (!checked_add_index(block.covariance_id, block.size, block_end)) {
      return false;
    }
    for (std::size_t previous_index = 0; previous_index < index; ++previous_index) {
      const CP2FeatureGateLayoutBlock &previous = raw.jacobian_layout[previous_index];
      Eigen::Index previous_end = 0;
      if (!checked_add_index(previous.covariance_id, previous.size,
                             previous_end)) {
        return false;
      }
      if (block.covariance_id < previous_end && previous.covariance_id < block_end) {
        return false;
      }
    }
    Eigen::Index updated_H_offset = 0;
    if (!checked_add_index(expected_H_offset, block.size, updated_H_offset)) {
      return false;
    }
    expected_H_offset = updated_H_offset;
  }
  return expected_H_offset == raw.H_x.cols();
}

void accumulate_gamma(CP2GlobalModeResult &global, double feature_gamma) {
  if (global.gamma_status == CP2GammaStatus::kNonfinite) {
    return;
  }
  const double candidate_sum = global.retained_gamma + feature_gamma;
  if (!std::isfinite(feature_gamma) || !std::isfinite(candidate_sum)) {
    global.gamma_status = CP2GammaStatus::kNonfinite;
    global.retained_gamma = std::numeric_limits<double>::quiet_NaN();
    return;
  }
  global.retained_gamma = candidate_sum;
}

bool build_global_modes(const CP2ShadowMathInput &input, CP2ShadowMathResult &result) {
  result.nullspace_assembly_valid = true;
  result.schur_assembly_valid = true;
  if (input.raw_systems.empty()) {
    return true;
  }

  result.nullspace.gamma_status = CP2GammaStatus::kAvailable;
  result.schur.gamma_status = CP2GammaStatus::kAvailable;
  result.nullspace.retained_gamma = 0.0;
  result.schur.retained_gamma = 0.0;
  GlobalAssembler nullspace_assembler;
  GlobalAssembler schur_assembler;

  for (std::size_t index = 0; index < result.features.size(); ++index) {
    const CP2RawFeatureSystem &raw = input.raw_systems[index];
    const CP2FeaturePairResult &feature = result.features[index];

    if (feature.raw_layout_valid && feature.nullspace.emitted_system_available &&
        feature.nullspace_gate.lifecycle_accept) {
      result.nullspace.accepted_ids.push_back(feature.feature_id);
      accumulate_gamma(result.nullspace, feature.nullspace.mode_gamma);
      if (result.nullspace_assembly_valid &&
          !nullspace_assembler.append(feature.nullspace.H_reduced,
                                      feature.nullspace.residual_reduced,
                                      raw.jacobian_layout)) {
        result.nullspace_assembly_valid = false;
      }
    }

    if (feature.raw_layout_valid && feature.schur.accepted() &&
        feature.schur_gate.lifecycle_accept) {
      result.schur.accepted_ids.push_back(feature.feature_id);
      accumulate_gamma(result.schur, feature.schur.gamma);
      if (result.schur_assembly_valid &&
          !schur_assembler.append(feature.schur.H_reduced,
                                  feature.schur.residual_reduced,
                                  raw.jacobian_layout)) {
        result.schur_assembly_valid = false;
      }
    }
  }

  auto finalize = [&input](GlobalAssembler assembler, bool suppress_for_gamma,
                           CP2GlobalModeResult &global) {
    global.precompression_rows = assembler.H.rows();
    global.jacobian_layout = assembler.layout;
    if (assembler.H.rows() < 1 || suppress_for_gamma) {
      return;
    }

    UpdaterHelper::measurement_compress_inplace(assembler.H, assembler.residual);
    global.compressed_rows = assembler.H.rows();
    global.H_compressed = assembler.H;
    global.residual_compressed = assembler.residual;
    if (assembler.H.rows() < 1) {
      return;
    }

    const Eigen::MatrixXd R =
        input.sigma_px_sq * Eigen::MatrixXd::Identity(assembler.residual.rows(), assembler.residual.rows());
    global.proposal = UpdaterMSCKFPreview::ComputeFromSnapshot(
        input.prior, global.jacobian_layout, assembler.H, assembler.residual, R);
    global.proposal_available = global.proposal.accepted();
  };

  if (result.nullspace_assembly_valid) {
    finalize(std::move(nullspace_assembler), false, result.nullspace);
  }
  if (result.schur_assembly_valid) {
    finalize(std::move(schur_assembler),
             result.schur.gamma_status == CP2GammaStatus::kNonfinite,
             result.schur);
  }
  return result.nullspace_assembly_valid && result.schur_assembly_valid;
}

} // namespace

constexpr double CP2ShadowMath::kStatisticsAbsoluteTolerance;
constexpr double CP2ShadowMath::kStatisticsRelativeTolerance;

const char *cp2_nullspace_reduction_status_name(CP2NullspaceReductionStatus status) noexcept {
  switch (status) {
  case CP2NullspaceReductionStatus::kAccepted:
    return "accepted";
  case CP2NullspaceReductionStatus::kInvalidDimensions:
    return "invalid_dimensions";
  case CP2NullspaceReductionStatus::kNonfinite:
    return "nonfinite";
  }
  return "unknown";
}

const char *cp2_nullspace_reduction_stage_name(CP2NullspaceReductionStage stage) noexcept {
  switch (stage) {
  case CP2NullspaceReductionStage::kInputDimensions:
    return "input_dimensions";
  case CP2NullspaceReductionStage::kGivensOutput:
    return "givens_output";
  case CP2NullspaceReductionStage::kStatistics:
    return "statistics";
  case CP2NullspaceReductionStage::kAccepted:
    return "accepted";
  }
  return "unknown";
}

const char *cp2_statistic_comparison_status_name(CP2StatisticComparisonStatus status) noexcept {
  switch (status) {
  case CP2StatisticComparisonStatus::kNotRequired:
    return "not_required";
  case CP2StatisticComparisonStatus::kAvailable:
    return "available";
  case CP2StatisticComparisonStatus::kReferenceUnavailable:
    return "reference_unavailable";
  case CP2StatisticComparisonStatus::kCandidateUnavailable:
    return "candidate_unavailable";
  case CP2StatisticComparisonStatus::kReferenceNormNonfinite:
    return "reference_norm_nonfinite";
  case CP2StatisticComparisonStatus::kErrorNonfinite:
    return "error_nonfinite";
  case CP2StatisticComparisonStatus::kToleranceNonfinite:
    return "tolerance_nonfinite";
  case CP2StatisticComparisonStatus::kRatioNonfinite:
    return "ratio_nonfinite";
  }
  return "unknown";
}

const char *cp2_gate_agreement_class_name(CP2GateAgreementClass agreement) noexcept {
  switch (agreement) {
  case CP2GateAgreementClass::kBothMatchAccept:
    return "both_match_accept";
  case CP2GateAgreementClass::kBothMatchReject:
    return "both_match_reject";
  case CP2GateAgreementClass::kBooleanNullspaceAcceptSchurReject:
    return "boolean_nullspace_accept_schur_reject";
  case CP2GateAgreementClass::kBooleanNullspaceRejectSchurAccept:
    return "boolean_nullspace_reject_schur_accept";
  case CP2GateAgreementClass::kNullspaceOnly:
    return "nullspace_only";
  case CP2GateAgreementClass::kSchurOnly:
    return "schur_only";
  case CP2GateAgreementClass::kNeitherDecision:
    return "neither_decision";
  }
  return "unknown";
}

const char *cp2_gamma_status_name(CP2GammaStatus status) noexcept {
  switch (status) {
  case CP2GammaStatus::kNotReached:
    return "not_reached";
  case CP2GammaStatus::kAvailable:
    return "available";
  case CP2GammaStatus::kNonfinite:
    return "nonfinite";
  }
  return "unknown";
}

CP2StatisticComparison CP2ShadowMath::CompareMatrixStatistic(
    bool reference_available, const Eigen::MatrixXd &reference,
    bool candidate_available, const Eigen::MatrixXd &candidate) {
  return compare_eigen_statistic(reference_available, reference,
                                 candidate_available, candidate);
}

CP2StatisticComparison CP2ShadowMath::CompareVectorStatistic(
    bool reference_available, const Eigen::VectorXd &reference,
    bool candidate_available, const Eigen::VectorXd &candidate) {
  return compare_eigen_statistic(reference_available, reference,
                                 candidate_available, candidate);
}

CP2StatisticComparison CP2ShadowMath::CompareScalarStatistic(
    bool reference_available, double reference, bool candidate_available,
    double candidate) {
  return compare_scalar_statistic_impl(reference_available, reference,
                                       candidate_available, candidate);
}

CP2ShadowMathResult CP2ShadowMath::Process(CP2ShadowMathInput input) {
  CP2ShadowMathResult result;
  if (!startup_input_is_valid(input)) {
    return result;
  }

  std::set<std::uint64_t> feature_ids;
  bool every_raw_layout_valid = true;
  result.features.reserve(input.raw_systems.size());
  for (const CP2RawFeatureSystem &raw : input.raw_systems) {
    if (!feature_ids.insert(raw.feature_id).second) {
      result.duplicate_feature_id = true;
    }

    CP2FeaturePairResult feature;
    feature.feature_id = raw.feature_id;
    feature.raw_rows = raw.H_f.rows();
    feature.raw_layout_valid = raw_layout_is_valid(raw, input.prior);
    every_raw_layout_valid = every_raw_layout_valid && feature.raw_layout_valid;
    feature.nullspace = reduce_nullspace(raw, input.sigma_px);
    feature.schur = SchurUpdate::Reduce(raw.H_x, raw.H_f, raw.residual, input.sigma_px);

    if (!feature.raw_layout_valid) {
      feature.nullspace_gate =
          CP2FeatureGate::Evaluate(CP2FeatureGateInput::ReductionUnavailable(),
                                   input.chi_squared_table);
    } else if (feature.nullspace.accepted()) {
      feature.nullspace_gate = CP2FeatureGate::Evaluate(
          gate_input(feature.nullspace.H_reduced, feature.nullspace.residual_reduced, input,
                     raw.jacobian_layout),
          input.chi_squared_table);
    } else if (feature.nullspace.emitted_system_available) {
      feature.nullspace_gate = CP2FeatureGate::Evaluate(
          CP2FeatureGateInput::EmittedEvidenceUnavailable(
              feature.nullspace.H_reduced, feature.nullspace.residual_reduced,
              input.prior.covariance, raw.jacobian_layout, input.sigma_px_sq,
              input.chi2_multiplier),
          input.chi_squared_table);
    } else {
      feature.nullspace_gate = CP2FeatureGate::Evaluate(CP2FeatureGateInput::ReductionUnavailable(),
                                                       input.chi_squared_table);
    }

    if (!feature.raw_layout_valid) {
      feature.schur_gate =
          CP2FeatureGate::Evaluate(CP2FeatureGateInput::ReductionUnavailable(),
                                   input.chi_squared_table);
    } else if (feature.schur.accepted()) {
      feature.schur_gate = CP2FeatureGate::Evaluate(
          gate_input(feature.schur.H_reduced, feature.schur.residual_reduced, input,
                     raw.jacobian_layout),
          input.chi_squared_table);
    } else {
      feature.schur_gate = CP2FeatureGate::Evaluate(CP2FeatureGateInput::ReductionUnavailable(),
                                                   input.chi_squared_table);
    }

    feature.statistics_comparison_required = feature.nullspace.accepted() && feature.schur.accepted();
    if (feature.statistics_comparison_required) {
      feature.lambda_comparison = CompareMatrixStatistic(
          feature.nullspace.statistics.lambda_available, feature.nullspace.statistics.lambda,
          feature.schur.accepted(), feature.schur.lambda);
      feature.eta_comparison = CompareVectorStatistic(
          feature.nullspace.statistics.eta_available, feature.nullspace.statistics.eta,
          feature.schur.accepted(), feature.schur.eta);
      feature.gamma_comparison = CompareScalarStatistic(
          feature.nullspace.statistics.gamma_available, feature.nullspace.statistics.gamma,
          feature.schur.accepted(), feature.schur.gamma);
    }

    feature.agreement_class = classify_agreement(feature.nullspace_gate, feature.schur_gate);
    if (matching_agreement(feature.agreement_class) &&
        !checked_index_to_u64(feature.raw_rows,
                              feature.raw_row_match_weight)) {
      // A row population that cannot be represented by the evidence schema
      // is not valid partial evidence. Suppress both global assemblies and
      // make the complete invocation fail closed.
      feature.raw_layout_valid = false;
      every_raw_layout_valid = false;
    }
    result.features.push_back(std::move(feature));
  }

  result.traversal_complete =
      result.features.size() == input.raw_systems.size();
  result.raw_layouts_valid = every_raw_layout_valid;
  const bool assembly_valid = build_global_modes(input, result);
  result.input_valid = result.traversal_complete && assembly_valid &&
                       result.raw_layouts_valid &&
                       !result.duplicate_feature_id;
  return result;
}

} // namespace ov_msckf
