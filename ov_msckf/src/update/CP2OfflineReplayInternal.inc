/*
 * SchurVIO-Lite CP2 detached-replay internal predicates.
 * SPDX-License-Identifier: GPL-3.0-or-later
 *
 * This file is intentionally an .inc rather than an installed public header.
 * Production replay and its production-linked protection test include the
 * same inline propositions, so neither a second implementation nor a
 * test-only symbol enters the production shared-library interface.
 */

#ifndef OV_MSCKF_CP2_OFFLINE_REPLAY_INTERNAL_INC
#define OV_MSCKF_CP2_OFFLINE_REPLAY_INTERNAL_INC

#include "CP2ShadowMath.h"

#include <string>

namespace ov_msckf {
namespace cp2_offline_replay_internal {

inline bool PreviewCountersAreZero(
    const MSCKFUpdatePreviewDiagnostics &diagnostics) noexcept {
  return diagnostics.jitter_count == 0U && diagnostics.repair_count == 0U &&
         diagnostics.alternate_solve_count == 0U &&
         diagnostics.clamp_count == 0U &&
         diagnostics.regularization_count == 0U &&
         diagnostics.fallback_count == 0U;
}

inline bool NullspaceReducerCountersAreZero(
    const CP2FeatureGateCounters &counters) noexcept {
  return counters.jitter_count == 0U && counters.repair_count == 0U &&
         counters.alternate_solve_count == 0U && counters.clamp_count == 0U &&
         counters.regularization_count == 0U &&
         counters.silent_fallback_count == 0U &&
         counters.fallback_count == 0U;
}

inline bool SchurReducerCountersAreZero(
    const SchurReductionResult &result) noexcept {
  return result.jitter_count == 0U && result.clamp_count == 0U &&
         result.regularization_count == 0U && result.fallback_count == 0U;
}

// Keep this proposition-for-proposition identical to
// cp2_shadow_math_evidence_passes in UpdaterMSCKF.cpp. Comparisons remain
// optional for a legitimately rejected feature, but when required all three
// must be present and pass.
inline bool ShadowMathEvidencePasses(
    const CP2ShadowMathResult &result) noexcept {
  if (!result.traversal_complete || !result.raw_layouts_valid ||
      !result.nullspace_assembly_valid || !result.schur_assembly_valid ||
      result.duplicate_feature_id ||
      result.nullspace.gamma_status == CP2GammaStatus::kNonfinite ||
      result.schur.gamma_status == CP2GammaStatus::kNonfinite) {
    return false;
  }
  for (const CP2FeaturePairResult &feature : result.features) {
    if (!feature.raw_layout_valid ||
        !NullspaceReducerCountersAreZero(feature.nullspace.counters) ||
        !SchurReducerCountersAreZero(feature.schur) ||
        (feature.statistics_comparison_required &&
         (!feature.lambda_comparison.available ||
          !feature.lambda_comparison.passed ||
          !feature.eta_comparison.available ||
          !feature.eta_comparison.passed ||
          !feature.gamma_comparison.available ||
          !feature.gamma_comparison.passed))) {
      return false;
    }
  }
  return PreviewCountersAreZero(result.nullspace.proposal.diagnostics) &&
         PreviewCountersAreZero(result.schur.proposal.diagnostics);
}

inline bool InvocationMathEvidencePasses(
    const CP2ShadowMathResult &result) noexcept {
  // A baseline proposal is the replay-derived commit predicate. The online
  // committing path requires the independently derived candidate proposal.
  return ShadowMathEvidencePasses(result) &&
         (!result.nullspace.proposal_available ||
          result.schur.proposal_available);
}

inline bool RecordedAndReplayedMathEvidencePasses(
    bool recorded_math_passed, const CP2ShadowMathResult &result) noexcept {
  return recorded_math_passed && InvocationMathEvidencePasses(result);
}

inline bool BlockComparisonPasses(bool comparison_available,
                                  bool passed) noexcept {
  return comparison_available && passed;
}

inline bool FrozenSequenceIdentityPasses(
    std::uint64_t sequence_index, const std::string &sequence_id) noexcept {
  switch (sequence_index) {
  case 0U:
    return sequence_id == "MH_01_easy";
  case 1U:
    return sequence_id == "MH_03_medium";
  case 2U:
    return sequence_id == "V1_01_easy";
  default:
    return false;
  }
}

inline bool UpdatePopulationPasses(std::uint64_t input_feature_count,
                                   std::uint64_t raw_system_count) noexcept {
  return raw_system_count <= input_feature_count;
}

inline bool ZeroRawTerminalPasses(const std::string &terminal,
                                  const std::string &subreason,
                                  std::uint64_t input_feature_count) noexcept {
  return (terminal == "empty_input" && subreason == "input_empty" &&
          input_feature_count == 0U) ||
         (terminal == "all_rejected" &&
          input_feature_count != 0U &&
          (subreason == "no_features_after_cleaning" ||
           subreason == "no_features_after_triangulation" ||
           subreason == "no_raw_systems"));
}

inline bool ZeroRawMathEvidencePasses(bool recorded_math_passed) noexcept {
  return recorded_math_passed;
}

struct ZeroRawAuxiliaryEvidence {
  bool row_counts_absent = false;
  bool preview_fields_absent = false;
  bool candidate_outcome_not_reached = false;
  bool preview_counters_zero = false;
  bool candidate_proposal_unavailable = false;
  bool baseline_commit_oracle_counters_zero = false;
  bool candidate_write_counters_zero = false;
};

inline bool ZeroRawAuxiliaryEvidencePasses(
    const ZeroRawAuxiliaryEvidence &evidence) noexcept {
  return evidence.row_counts_absent && evidence.preview_fields_absent &&
         evidence.candidate_outcome_not_reached &&
         evidence.preview_counters_zero &&
         evidence.candidate_proposal_unavailable &&
         evidence.baseline_commit_oracle_counters_zero &&
         evidence.candidate_write_counters_zero;
}

} // namespace cp2_offline_replay_internal
} // namespace ov_msckf

#endif // OV_MSCKF_CP2_OFFLINE_REPLAY_INTERNAL_INC
