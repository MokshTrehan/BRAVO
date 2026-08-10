/*
 * SchurVIO-Lite research-only update-envelope capture (schema 2).
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_UPDATE_ENVELOPE_CAPTURE_H
#define OV_MSCKF_UPDATE_ENVELOPE_CAPTURE_H

#include <Eigen/Core>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "CP2FeatureGate.h"
#include "SchurUpdate.h"
#include "UpdaterMSCKFPreview.h"
#include "UpdaterOptions.h"

namespace ov_core {
class Feature;
}
namespace ov_type {
class Type;
}

namespace ov_msckf {

class State;

/// Lifecycle reason assigned before the deterministic MSCKF candidate sort.
enum class UpdateEnvelopeCandidateReason : std::uint64_t {
  kUnknown = 0U,
  kLost = 1U,
  kMarginal = 2U,
  kMaximumTrack = 3U,
};

/// Stable terminal state for one propagated camera callback.
enum class UpdateEnvelopeTerminalStatus : std::uint64_t {
  kActive = 0U,
  kInsufficientClones = 1U,
  kEmptyInput = 2U,
  kAllRejected = 3U,
  kEmptyAfterCompression = 4U,
  kPreflightRejected = 5U,
  kCommitted = 6U,
  kInternalFailure = 7U,
  kPropagationFailure = 8U,
};

/// First stage at which a captured group exists.
enum class UpdateEnvelopeStage : std::uint64_t {
  kCallback = 0U,
  kPrefilter = 1U,
  kGeometry = 2U,
  kRawFactor = 3U,
  kReduction = 4U,
  kGate = 5U,
  kAccumulation = 6U,
  kCompression = 7U,
  kPreview = 8U,
  kCommit = 9U,
  kCallbackFinalization = 10U,
  kCaptureOutput = 11U,
};

/// Exclusive callback costs supplied after cleanup/marginalization.
struct UpdateEnvelopeCallbackCosts {
  double tracking_seconds = 0.0;
  double propagation_seconds = 0.0;
  double msckf_seconds = 0.0;
  double slam_update_seconds = 0.0;
  double slam_delay_seconds = 0.0;
  double finalization_seconds = 0.0;
  double total_seconds = 0.0;

  /// Production-stage boundary offsets from callback start, in seconds.
  /// The fixed order is callback begin, tracking end, propagation end,
  /// MSCKF end, SLAM-update end, SLAM-delay end, and finalization end.
  bool stage_timestamps_available = false;
  std::array<double, 7> stage_end_offsets_seconds{{0.0, 0.0, 0.0, 0.0,
                                                   0.0, 0.0, 0.0}};
};

/// Exact column mapping for a per-track or global Jacobian.
struct UpdateEnvelopeLayoutBlock {
  std::uint64_t local_column = 0U;
  std::uint64_t covariance_column = 0U;
  std::uint64_t size = 0U;
};

/**
 * Buffered, fail-open, default-off schema-2 writer.
 *
 * All Record* methods only copy value data into the current in-memory
 * envelope. FinishCallback samples all estimator stage fields first, encodes
 * the immutable buffer, and only then writes the frame. No capture return
 * value participates in an estimator decision.
 */
class UpdateEnvelopeCaptureWriter {
public:
  explicit UpdateEnvelopeCaptureWriter(const UpdaterOptions &options) noexcept;
  ~UpdateEnvelopeCaptureWriter() noexcept;

  UpdateEnvelopeCaptureWriter(const UpdateEnvelopeCaptureWriter &) = delete;
  UpdateEnvelopeCaptureWriter &
  operator=(const UpdateEnvelopeCaptureWriter &) = delete;

  bool active() const noexcept;
  bool failed() const noexcept;
  bool callback_active() const noexcept;
  std::uint64_t envelope_count() const noexcept;
  std::uint64_t callback_record_overhead_ns() const noexcept;

  /// Charge capture-only work performed by the callback owner outside this
  /// writer (for example, construction of lifecycle-reason metadata).
  void AddExternalRecordOverhead(std::uint64_t duration_ns) noexcept;

  /// Begin at the literal post-propagation/pre-feature callback seam.
  void BeginCallback(const std::shared_ptr<State> &state,
                     double camera_timestamp) noexcept;

  /// Refresh the exact P-minus boundary used by the ordinary visual updater.
  void RefreshVisualPrior(const std::shared_ptr<State> &state) noexcept;

  /// Freeze exact candidate population/order before updater-side filtering.
  void SetCandidates(
      const std::vector<std::shared_ptr<ov_core::Feature>> &features,
      const std::vector<UpdateEnvelopeCandidateReason> &reasons) noexcept;

  void RecordPrefilter(const ov_core::Feature &feature, bool accepted,
                       std::uint64_t duration_ns) noexcept;
  void RecordGeometry(const std::shared_ptr<State> &state,
                      const ov_core::Feature &feature,
                      bool triangulation_attempted,
                      bool triangulation_succeeded,
                      bool refinement_attempted,
                      bool refinement_succeeded,
                      std::uint64_t duration_ns) noexcept;
  void RecordRawFactor(
      std::uint64_t feature_id,
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
      const Eigen::VectorXd &residual,
      std::uint64_t duration_ns) noexcept;
  void RecordReduction(std::uint64_t feature_id,
                       UpdaterOptions::LandmarkElimination reducer,
                       const SchurReductionResult *schur,
                       const Eigen::MatrixXd &reduced_H,
                       const Eigen::VectorXd &reduced_residual,
                       std::uint64_t duration_ns) noexcept;
  void RecordGate(std::uint64_t feature_id,
                  const CP2FeatureGateResult &gate,
                  std::uint64_t duration_ns) noexcept;
  void RecordAccumulation(std::uint64_t feature_id,
                          std::uint64_t global_row_start,
                          std::uint64_t global_row_count,
                          std::uint64_t duration_ns) noexcept;

  void RecordSelectedSystem(
      const std::vector<std::uint64_t> &accepted_ids,
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
      const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept;
  void RecordCompressedSystem(
      const std::vector<std::shared_ptr<ov_type::Type>> &order,
      const Eigen::MatrixXd &H, const Eigen::VectorXd &residual,
      const Eigen::MatrixXd &R, std::uint64_t duration_ns) noexcept;
  void RecordPosterior(const MSCKFUpdatePreviewResult &preview,
                       bool global_gate_applied,
                       bool global_nis_available, double global_nis,
                       bool global_gate_accepted,
                       std::uint64_t duration_ns) noexcept;
  void RecordVisualTerminal(UpdateEnvelopeTerminalStatus status,
                            std::uint64_t reason,
                            std::uint64_t mean_commit_count,
                            std::uint64_t covariance_commit_count,
                            std::uint64_t feature_finalization_count,
                            std::uint64_t commit_duration_ns) noexcept;

  /// Encode/write one complete envelope after all estimator stage endpoints.
  void FinishCallback(const UpdateEnvelopeCallbackCosts &costs,
                      UpdateEnvelopeTerminalStatus fallback_status,
                      std::uint64_t fallback_reason) noexcept;

private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

const char *update_envelope_capture_source_commit() noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_UPDATE_ENVELOPE_CAPTURE_H
