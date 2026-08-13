/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 passive T0 value types.
 */

#ifndef OV_MSCKF_TURNSAFE_TYPES_H
#define OV_MSCKF_TURNSAFE_TYPES_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace ov_msckf {

enum class TurnSafeFullOutcome : std::uint8_t {
  kUnavailable,
  kFullAccepted,
  kInitIllConditioned,
  kInitTooNearOrBehind,
  kInitTooFar,
  kInitNonfinite,
  kInitBaselineRatio,
  kRefinementFailed,
  kSchurInsufficientRows,
  kSchurRankDeficient,
  kSchurIllConditioned,
  kSchurNonfinite,
  kFullNisRejected,
  kUnsupportedOrInvalidInput,
};

enum class TurnSafeOutcomeMapping : std::uint8_t {
  kExact,
  kUnmapped,
  kNotApplicable,
};

const char *turnsafe_full_outcome_name(TurnSafeFullOutcome outcome) noexcept;
const char *turnsafe_outcome_mapping_name(TurnSafeOutcomeMapping mapping) noexcept;
bool turnsafe_shadow_eligible_outcome(TurnSafeFullOutcome outcome) noexcept;

struct TurnSafeMatrixValue {
  std::size_t rows = 0U;
  std::size_t cols = 0U;
  std::vector<double> values;
};

struct TurnSafeObservationValue {
  std::size_t camera_id = 0U;
  double timestamp = std::numeric_limits<double>::quiet_NaN();
  std::size_t baseline_observation_index = 0U;
  bool uv_available = false;
  std::array<double, 2U> uv{{0.0, 0.0}};
  bool uv_normalized_available = false;
  std::array<double, 2U> uv_normalized{{0.0, 0.0}};
  bool clone_available = false;
};

struct TurnSafeInitializerValue {
  bool attempted = false;
  bool native_success = false;
  std::string native_function = "NOT_EXPOSED_BY_NATIVE_PATH";
  bool condition_available = false;
  double condition_number = std::numeric_limits<double>::quiet_NaN();
  bool depth_available = false;
  double depth = std::numeric_limits<double>::quiet_NaN();
  bool baseline_ratio_available = false;
  double baseline_ratio = std::numeric_limits<double>::quiet_NaN();
  bool predicate_ill_conditioned = false;
  bool predicate_too_near_or_behind = false;
  bool predicate_too_far = false;
  bool predicate_baseline_ratio = false;
  bool predicate_native_nan = false;
  int refinement_runs = 0;
  double refinement_lambda = std::numeric_limits<double>::quiet_NaN();
  bool refinement_last_step_norm_available = false;
  double refinement_last_step_norm =
      std::numeric_limits<double>::quiet_NaN();
  double refinement_control_epsilon =
      std::numeric_limits<double>::quiet_NaN();
  std::string termination_reason = "NOT_APPLICABLE";
};

struct TurnSafeSchurValue {
  bool attempted = false;
  bool native_accepted = false;
  std::string native_status = "NOT_EXPOSED_BY_NATIVE_PATH";
  std::string native_stage = "NOT_EXPOSED_BY_NATIVE_PATH";
  bool raw_rows_available = false;
  std::int64_t raw_rows = 0;
  bool degrees_of_freedom_available = false;
  std::int64_t degrees_of_freedom = 0;
  bool singular_values_available = false;
  std::array<double, 3U> singular_values{{0.0, 0.0, 0.0}};
  bool singular_ratio_available = false;
  double singular_ratio = std::numeric_limits<double>::quiet_NaN();
  bool numerical_rank_available = false;
  std::int64_t numerical_rank = 0;
  bool condition_number_available = false;
  double condition_number = std::numeric_limits<double>::quiet_NaN();
  bool reduced_rows_available = false;
  std::int64_t reduced_rows = 0;
  std::uint64_t jitter_count = 0U;
  std::uint64_t clamp_count = 0U;
  std::uint64_t regularization_count = 0U;
  std::uint64_t fallback_count = 0U;
};

struct TurnSafeNisValue {
  bool attempted = false;
  bool lifecycle_accept = false;
  std::string native_stage = "NOT_EXPOSED_BY_NATIVE_PATH";
  bool degrees_of_freedom_available = false;
  std::int64_t degrees_of_freedom = 0;
  bool statistic_available = false;
  double statistic = std::numeric_limits<double>::quiet_NaN();
  bool threshold_available = false;
  double threshold = std::numeric_limits<double>::quiet_NaN();
  std::string decision = "NOT_ATTEMPTED";
};

/** Immutable, detached-index-keyed diagnostic copy; no live pointer exists. */
struct TurnSafeFullTrackAttempt {
  std::uint64_t detached_index = 0U;
  std::uint64_t feature_id = 0U;
  std::vector<TurnSafeObservationValue> ordered_observations;
  bool attempt_has_any_live_same_camera_clone_pair = false;
  std::uint64_t valid_clone_pair_count = 0U;
  TurnSafeInitializerValue triangulation;
  TurnSafeInitializerValue refinement;
  TurnSafeSchurValue schur;
  TurnSafeNisValue full_nis;
  TurnSafeFullOutcome full_outcome = TurnSafeFullOutcome::kUnavailable;
  TurnSafeOutcomeMapping full_outcome_mapping =
      TurnSafeOutcomeMapping::kNotApplicable;
  std::string native_terminal_status = "NOT_REACHED";
  bool target_time_stereo_available = false;
  bool accepted_at_native_feature_gate = false;
  bool native_feature_row_count_available = false;
  std::uint64_t native_feature_row_count = 0U;
  bool accepted_full_factor = false;
  std::uint64_t accepted_full_row_count = 0U;
  std::string finalization_result = "NOT_REACHED";
};

struct TurnSafePriorCloneValue {
  double timestamp = std::numeric_limits<double>::quiet_NaN();
  std::int64_t covariance_id = -1;
  TurnSafeMatrixValue nominal_pose;
  TurnSafeMatrixValue fej_pose;
};

struct TurnSafePriorPairCovariance {
  double source_timestamp = std::numeric_limits<double>::quiet_NaN();
  double target_timestamp = std::numeric_limits<double>::quiet_NaN();
  std::int64_t source_covariance_id = -1;
  std::int64_t target_covariance_id = -1;
  TurnSafeMatrixValue source_source;
  TurnSafeMatrixValue target_target;
  TurnSafeMatrixValue source_target;
  TurnSafeMatrixValue target_source;
};

struct TurnSafePriorCameraValue {
  std::size_t camera_id = 0U;
  std::int64_t extrinsic_id = -1;
  std::int64_t intrinsic_id = -1;
  TurnSafeMatrixValue extrinsic_value;
  TurnSafeMatrixValue extrinsic_fej;
  TurnSafeMatrixValue intrinsic_value;
  TurnSafeMatrixValue intrinsic_fej;
  TurnSafeMatrixValue projection_cache;
  int width = 0;
  int height = 0;
  std::string model;
};

struct TurnSafePriorPrimitives {
  bool available = false;
  std::string reason = "NOT_CAPTURED";
  std::string fingerprint;
  std::size_t covariance_dimension = 0U;
  std::vector<TurnSafePriorCloneValue> clones;
  std::vector<TurnSafePriorPairCovariance> pair_covariances;
  std::vector<TurnSafePriorCameraValue> cameras;
};

struct TurnSafeUpdateRecord {
  double callback_timestamp = std::numeric_limits<double>::quiet_NaN();
  std::string native_terminal_status = "NOT_REACHED";
  std::string native_terminal_subreason = "NOT_REACHED";
  std::uint64_t input_feature_count = 0U;
  std::uint64_t raw_system_count = 0U;
  std::vector<std::uint64_t> baseline_accepted_ids;
  bool proposal_gamma_available = false;
  double proposal_gamma = std::numeric_limits<double>::quiet_NaN();
  bool precompression_rows_available = false;
  std::uint64_t precompression_rows = 0U;
  bool compressed_rows_available = false;
  std::uint64_t compressed_rows = 0U;
  bool proposal_attempted = false;
  bool proposal_accepted = false;
  bool baseline_commit_occurred = false;
  std::uint64_t mean_commit_count = 0U;
  std::uint64_t covariance_commit_count = 0U;
  std::uint64_t feature_finalization_count = 0U;
  bool no_full_visual_update_duration_available = false;
  double no_full_visual_update_duration =
      std::numeric_limits<double>::quiet_NaN();
  std::string no_full_visual_update_duration_reason =
      "NO_PRIOR_ACCEPTED_FULL_UPDATE";
  std::vector<TurnSafeFullTrackAttempt> attempts;
  TurnSafePriorPrimitives prior;
};

struct TurnSafeAcuteCertificateResult {
  std::string status = "INVALID_INPUT";
  bool is_acute = false;
  bool theta_available = false;
  double theta_ucb = std::numeric_limits<double>::quiet_NaN();
  bool eigenvalues_available = false;
  double lambda_min = std::numeric_limits<double>::quiet_NaN();
  double lambda_max = std::numeric_limits<double>::quiet_NaN();
  bool rho_available = false;
  double rho_trans = std::numeric_limits<double>::quiet_NaN();
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_TYPES_H
