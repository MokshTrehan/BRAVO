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

#include "CP2ShadowMath.h"

#include <Eigen/Eigen>
#include <atomic>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <vector>

#include "feat/FeatureInitializerOptions.h"

#include "UpdaterOptions.h"

namespace ov_core {
class Feature;
class FeatureInitializer;
} // namespace ov_core

namespace ov_msckf {

class State;

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
 * @brief Value-only terminal event for timing and later trace serialization.
 *
 * duration_ns ends before observer execution. On a committing invocation its
 * endpoint is sampled immediately after StateHelper::EKFUpdate returns. The
 * optional shadow payload was fully formed before that sole live commit. The
 * sink receives this event by value and may move it to longer-lived storage.
 */
struct CP2LiveUpdateEvent {
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

  /// Independent capability bit; false keeps raw copies and shadow math off.
  bool cp2_shadow_enabled = false;

  /// Callback configuration is synchronized and frozen at first update entry.
  std::mutex cp2_callback_mutex;
  bool cp2_callback_configuration_frozen = false;

  /// External state serialization is mandatory; reject accidental reentry.
  std::atomic<bool> cp2_update_active{false};
};

} // namespace ov_msckf

#endif // OV_MSCKF_UPDATER_MSCKF_H
