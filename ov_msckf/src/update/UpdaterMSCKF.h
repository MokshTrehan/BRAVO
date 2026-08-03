/*
 * OpenVINS: An Open Platform for Visual-Inertial Research
 * Copyright (C) 2018-2023 Patrick Geneva
 * Copyright (C) 2018-2023 Guoquan Huang
 * Copyright (C) 2018-2023 OpenVINS Contributors
 * Copyright (C) 2018-2019 Kevin Eckenhoff
 * Copyright (C) 2026 Moksh Trehan
 * Modified in 2026 by Moksh Trehan for SchurVIO-Lite CP2.
 *
 * This program is free software: you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

#ifndef OV_MSCKF_UPDATER_MSCKF_H
#define OV_MSCKF_UPDATER_MSCKF_H

#include "CP2CommitOracle.h"
#include "CP2ShadowMath.h"

#include <Eigen/Eigen>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <vector>

#include "feat/FeatureInitializerOptions.h"

#include "UpdaterOptions.h"

namespace ov_core {
class Feature;
class FeatureInitializer;
} // namespace ov_core

namespace ov_msckf {

class State;

#if defined(OV_MSCKF_CP2_TESTING)
enum class CP2UpdaterTestFault : std::uint8_t {
  kNone,
  kCandidateAssemblyFailure,
  kInvalidPhase2,
  kPhase1ValueMismatch,
  kFinalPointerMismatch,
  kInvalidatePostcommitStorage,
  kInvalidatePostcommitPointerToken,
  kPhase3ValueMismatch,
  kPhase3Nonfinite,
  kNoncommitDurationArithmeticFailure,
  kCommittedDurationArithmeticFailure,
  kCommitOracleArithmeticOverflow,
  kCommitOracleInvalidPhase,
  kBaselineProvenanceMismatch,
};
enum class CP2UpdaterTestStage : std::uint8_t {
  kPhase0Capture,
  kLivePreviewCapture,
  kPhase0Projection,
  kPhase2Build,
  kPhase1Capture,
};
class CP2UpdaterTestAccess;
#endif

/**
 * @brief Owning, value-only output from one opt-in CP2 live shadow invocation.
 *
 * The callback surface deliberately contains no live State, Type, Feature, or
 * caller-owned pointer. The raw systems and prior are retained alongside the
 * independent nullspace/Schur result so a later trace writer can serialize
 * the exact mathematical inputs without rebuilding them from live objects.
 */
struct CP2LiveShadowEvidence {
  CP2ShadowMathInput input;
  CP2ShadowMathResult result;
  bool shadow_math_completed = false;
  bool baseline_preview_available = false;
  MSCKFUpdatePreviewResult live_nullspace_proposal;
  bool baseline_commit_planned = false;
};

/// Stable terminal categories for one live updater invocation.
enum class CP2UpdateTerminalStatus {
  kEmptyInput,
  kAllRejected,
  kEmptyAfterCompression,
  kPreflightRejected,
  kCommittedCounted,
  kInternalFailure,
};

/// Frozen terminal subreason; every status has exactly one valid mapping.
enum class CP2UpdateTerminalSubreason {
  kInputEmpty,
  kNoFeaturesAfterCleaning,
  kNoFeaturesAfterTriangulation,
  kNoRawSystems,
  kAllBaselineFeaturesRejected,
  kMeasurementCompressionEmpty,
  kBaselinePreflightRejected,
  kInvalidLiveMode,
  kSnapshotMismatch,
  kTraceInvariantFailure,
  kNone,
};

const char *cp2_update_terminal_status_name(CP2UpdateTerminalStatus status) noexcept;
const char *cp2_update_terminal_subreason_name(CP2UpdateTerminalSubreason subreason) noexcept;

/**
 * @brief One-shot serial-runner identity supplied before an updater call.
 *
 * The updater assigns invocation_id itself at call entry. Keeping that counter
 * out of the caller prevents duplicate or reordered invocation identities.
 */
struct CP2UpdateInvocationContext {
  std::uint64_t sequence_index = 0U;
  std::uint64_t pair_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
};

/**
 * @brief Value-only terminal event for timing and later trace serialization.
 *
 * duration_ns ends before observer execution. On a committing invocation its
 * endpoint is sampled immediately after StateHelper::EKFUpdate returns. The
 * optional shadow payload was fully formed before that sole live commit. The
 * diagnostic observer receives this value after the timing endpoint. In
 * authoritative mode it is also embedded in the immutable owning record.
 */
struct CP2LiveUpdateEvent {
  bool invocation_context_available = false;
  std::uint64_t sequence_index = 0U;
  std::uint64_t pair_index = 0U;
  std::uint64_t camera_timestamp_ns = 0U;
  std::uint64_t invocation_id = 0U;
  std::uint64_t duration_ns = 0;
  CP2UpdateTerminalStatus terminal_status = CP2UpdateTerminalStatus::kInternalFailure;
  CP2UpdateTerminalSubreason terminal_subreason = CP2UpdateTerminalSubreason::kTraceInvariantFailure;
  std::uint64_t input_feature_count = 0;
  std::uint64_t raw_system_count = 0;
  std::vector<std::uint64_t> baseline_accepted_ids;
  CP2GammaStatus baseline_gamma_status = CP2GammaStatus::kNotReached;
  double baseline_gamma = std::numeric_limits<double>::quiet_NaN();
  bool baseline_precompression_system_nonempty = false;
  bool baseline_precompression_rows_available = false;
  std::uint64_t baseline_precompression_rows = 0;
  bool baseline_compressed_system_nonempty = false;
  bool baseline_compressed_rows_available = false;
  std::uint64_t baseline_compressed_rows = 0;
  bool baseline_preflight_attempted = false;
  bool baseline_preflight_accepted = false;
  bool baseline_commit_occurred = false;
  bool shadow_evidence_available = false;
  CP2LiveShadowEvidence shadow;
};

/// Explicit result from the authoritative, synchronous CP2 recorded sink.
enum class CP2RecordedSinkStatus : std::uint8_t {
  kPublished,
  kRejected,
};

/// Sticky out-of-band reason why a complete recorded campaign is impossible.
enum class CP2TraceFatalReason : std::uint8_t {
  kNone,
  kConfigurationInvariant,
  kPhase0Promotion,
  kRequiredEncoding,
  kShadowTrace,
  kPhase1Capture,
  kPostcommitCapture,
  kSinkRejected,
  kArithmeticInvariant,
  kPostcommitException,
};

const char *cp2_recorded_sink_status_name(CP2RecordedSinkStatus status) noexcept;
const char *cp2_trace_fatal_reason_name(CP2TraceFatalReason reason) noexcept;

/**
 * Distinct stop signal for an incomplete authoritative trace.
 *
 * The sticky latch is set before this exception is thrown. The runner must
 * stop the campaign and discard its hidden partial output; broad updater
 * exception handling is forbidden from relabeling this as an ordinary update.
 */
class CP2TraceFatalError : public std::runtime_error {
public:
  CP2TraceFatalError(CP2TraceFatalReason reason, const char *message)
      : std::runtime_error(message), reason_(reason) {}

  CP2TraceFatalReason reason() const noexcept { return reason_; }

private:
  CP2TraceFatalReason reason_;
};

/// One exact, owning state phase and its already encoded canonical payload.
struct CP2EncodedStatePhase {
  CP2StatePhase phase = CP2StatePhase::kPhase0Prior;
  std::unique_ptr<const CP2CompositeStateSnapshot> snapshot;
  std::vector<std::uint8_t> payload;

  CP2EncodedStatePhase() = default;
  CP2EncodedStatePhase(const CP2EncodedStatePhase &) = delete;
  CP2EncodedStatePhase &operator=(const CP2EncodedStatePhase &) = delete;
  CP2EncodedStatePhase(CP2EncodedStatePhase &&) noexcept = default;
  CP2EncodedStatePhase &operator=(CP2EncodedStatePhase &&) noexcept = default;
};

/**
 * Complete value-owning result of one authoritative recorded invocation.
 *
 * The embedded update owns the required sequence, pair, camera timestamp, and
 * updater-assigned invocation identity; authoritative records therefore have
 * update.invocation_context_available=true without duplicating identity state.
 *
 * state_phase_count is exactly 0, 2, or 4. The populated prefix is therefore
 * exactly [], [0,1], or [0,1,2,3]. Pointer identity never enters this object.
 */
struct CP2RecordedUpdateEvent {
  CP2LiveUpdateEvent update;
  std::array<CP2EncodedStatePhase, 4> state_phases;
  std::size_t state_phase_count = 0U;
  std::vector<std::vector<std::uint8_t>> raw_system_payloads;
  bool baseline_proposal_payload_available = false;
  std::vector<std::uint8_t> baseline_proposal_payload;
  bool candidate_proposal_payload_available = false;
  std::vector<std::uint8_t> candidate_proposal_payload;
  bool phase01_canonical_equal = false;
  bool phase01_pointer_graph_equal = false;
  bool baseline_commit_oracle_available = false;
  CP2CommitOracleResult baseline_commit_oracle;
  bool baseline_commit_mismatch = false;
  /**
   * Necessary online math invariant only; it is not the schema's
   * updates.jsonl math_passed field. The C3 sink must additionally derive and
   * conjoin every candidate state/covariance block comparison and row-
   * completeness requirement before it may publish schema math_passed=true.
   */
  bool online_math_evidence_passed = false;
  std::uint64_t baseline_commit_count = 0U;
  std::uint64_t baseline_mean_commit_count = 0U;
  std::uint64_t baseline_covariance_commit_count = 0U;
  std::uint64_t candidate_ekf_update_call_count = 0U;
  std::uint64_t candidate_mean_write_count = 0U;
  std::uint64_t candidate_covariance_write_count = 0U;
  std::uint64_t candidate_type_update_call_count = 0U;
  std::uint64_t candidate_feature_write_count = 0U;

  CP2RecordedUpdateEvent() = default;
  CP2RecordedUpdateEvent(const CP2RecordedUpdateEvent &) = delete;
  CP2RecordedUpdateEvent &operator=(const CP2RecordedUpdateEvent &) = delete;
  CP2RecordedUpdateEvent(CP2RecordedUpdateEvent &&) noexcept = default;
  CP2RecordedUpdateEvent &operator=(CP2RecordedUpdateEvent &&) noexcept =
      default;
};

/** Status-returning authoritative sink, distinct from the diagnostic observer. */
class CP2RecordedUpdateSink {
public:
  virtual ~CP2RecordedUpdateSink() = default;
  virtual CP2RecordedSinkStatus
  Publish(const std::shared_ptr<const CP2RecordedUpdateEvent> &record) noexcept = 0;
};

/**
 * @brief Will compute the system for our sparse features and update the filter.
 *
 * This class is responsible for computing the entire linear system for all features that are going to be used in an update.
 * This follows the original MSCKF, where we first triangulate features, we then nullspace project the feature Jacobian.
 * After this we compress all the measurements to have an efficient update and update the state.
 */
class UpdaterMSCKF {

public:
  using CP2UpdateCallback = std::function<void(CP2LiveUpdateEvent)>;

  /**
   * @brief Default constructor for our MSCKF updater
   *
   * Our updater has a feature initializer which we use to initialize features as needed.
   * Also the options allow for one to tune the different parameters for update.
   *
   * @param options Updater options (include measurement noise value)
   * @param feat_init_options Feature initializer options
   */
  UpdaterMSCKF(UpdaterOptions &options, ov_core::FeatureInitializerOptions &feat_init_options);

  /// Frozen CP2 gate boundary: equality is accepted; only strict excess rejects.
  static bool chi2_gate_rejects(double statistic, double threshold) noexcept;

  /**
   * @brief Install the value-only update observer and optionally enable shadow math.
   *
   * Observer-only timing/terminal events are legal in either live mode. Shadow
   * comparison is legal only while nullspace is the selected live reducer and
   * requires a nonempty callback. Configuration freezes at the first update
   * entry; every later setter call is rejected. An illegal request is rejected
   * without changing the existing callback or capability state. Passing an
   * empty callback with enable_shadow=false disables observation and shadow
   * work before that freeze. The callback runs synchronously after the timing
   * endpoint and must not mutate estimator state; concurrent or reentrant
   * update calls are rejected before estimator work. Callback exceptions are
   * contained and cannot veto the baseline lifecycle or its sole EKF commit.
   *
   * @return true when the requested callback state was installed.
   */
  bool set_cp2_update_callback(CP2UpdateCallback callback, bool enable_shadow = false);

  /**
   * Install or clear the authoritative CP2 recorded sink before first update.
   *
   * A nonnull sink is legal only for the frozen nullspace live baseline and
   * automatically enables the complete dual-path shadow. It may coexist with
   * the diagnostic observer; successful authoritative publication always
   * precedes observer execution. Configuration freezes at first update entry.
   */
  bool set_cp2_recorded_sink(std::shared_ptr<CP2RecordedUpdateSink> sink);

  /**
   * Install exactly one value-only identity for the next updater call.
   *
   * A pending identity cannot be overwritten. The setter is rejected while
   * an update is active or after a fatal trace latch. Every accepted identity
   * is consumed once at the next call entry, where the updater assigns its
   * own checked, contiguous zero-based invocation ID. Authoritative recorded
   * mode requires an identity on every call; ordinary diagnostic/estimator
   * use remains legal without one.
   */
  bool set_cp2_invocation_context(
      const CP2UpdateInvocationContext &context) noexcept;

  bool cp2_trace_fatal_latched() const noexcept;
  CP2TraceFatalReason cp2_trace_fatal_reason() const noexcept;

  /**
   * @brief Given tracked features, this will try to use them to update the state.
   *
   * @param state State of the filter
   * @param feature_vec Features that can be used for update
   */
  void update(std::shared_ptr<State> state, std::vector<std::shared_ptr<ov_core::Feature>> &feature_vec);

protected:
  /// Options used during update
  UpdaterOptions _options;

  /// Feature initializer class object
  std::shared_ptr<ov_core::FeatureInitializer> initializer_feat;

  /// Chi squared 95th percentile table (lookup would be size of residual)
  std::map<int, double> chi_squared_table;

  /// Empty by default; therefore ordinary and CP2-E runs execute no shadow.
  std::shared_ptr<const CP2UpdateCallback> cp2_update_callback;

  /// Empty by default; nonnull activates authoritative recorded mode.
  std::shared_ptr<CP2RecordedUpdateSink> cp2_recorded_sink;

  /// Independent capability bit; false keeps raw copies and shadow math off.
  bool cp2_shadow_enabled = false;

  /// Callback configuration is synchronized and frozen at first update entry.
  std::mutex cp2_callback_mutex;
  bool cp2_callback_configuration_frozen = false;

  /// One-shot caller identity and updater-owned contiguous invocation counter.
  bool cp2_invocation_context_pending = false;
  CP2UpdateInvocationContext cp2_pending_invocation_context;
  std::uint64_t cp2_next_invocation_id = 0U;

  /// External state serialization is mandatory; reject accidental reentry.
  std::atomic<bool> cp2_update_active{false};

  /// First fatal reason wins and cannot be cleared during this updater's life.
  std::atomic<CP2TraceFatalReason> cp2_trace_fatal_reason_value{
      CP2TraceFatalReason::kNone};

private:
  [[noreturn]] void latch_cp2_trace_fatal(CP2TraceFatalReason reason,
                                          const char *message);
#if defined(OV_MSCKF_CP2_TESTING)
  friend class CP2UpdaterTestAccess;
  void cp2_test_note_stage(CP2UpdaterTestStage stage) noexcept {
    if (cp2_test_stage_count < cp2_test_stages.size()) {
      cp2_test_stages[cp2_test_stage_count++] = stage;
    }
  }
  CP2UpdaterTestFault cp2_test_fault = CP2UpdaterTestFault::kNone;
  std::uint64_t cp2_test_raw_assembly_calls = 0U;
  std::array<CP2UpdaterTestStage, 8U> cp2_test_stages{};
  std::size_t cp2_test_stage_count = 0U;
#endif
};

} // namespace ov_msckf

#endif // OV_MSCKF_UPDATER_MSCKF_H
