/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 default-off T0 sink.
 */

#ifndef OV_MSCKF_TURNSAFE_DIAGNOSTICS_H
#define OV_MSCKF_TURNSAFE_DIAGNOSTICS_H

#include "TurnSafeTypes.h"

#include <Eigen/Core>

#include <cstdint>
#include <cstdio>
#include <atomic>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "track/TrackKLTDiagnostics.h"

namespace ov_core {
class Feature;
}

namespace ov_msckf {

struct MSCKFUpdatePriorSnapshot;

enum class TurnSafeDiagnosticFaultStage : std::uint8_t {
  kNone,
  kAttemptCopy,
  kPriorCopy,
  kInitializerProjection,
  kSchurProjection,
  kNisProjection,
  kUpdaterPublication,
  kTrackerCallbackInstallation,
  kUpdaterCallbackInstallation,
  kFrontendCopy,
  kUpdateCopy,
  kCandidateGrouping,
  kHeaderSerialization,
  kCallbackSerialization,
  kSinkOpen,
  kSinkFdopen,
  kSinkWrite,
  kSinkRecordFlush,
  kSinkFinalFlush,
  kSinkClose,
  kSinkRename,
  kSinkDirectoryFsync,
};

enum class TurnSafeDiagnosticFaultKind : std::uint8_t {
  kNone,
  kBadAlloc,
  kStdException,
  kUnknown,
};

void set_turnsafe_diagnostic_fault_for_test(
    TurnSafeDiagnosticFaultStage stage,
    TurnSafeDiagnosticFaultKind kind) noexcept;
void clear_turnsafe_diagnostic_fault_for_test() noexcept;
void inject_turnsafe_diagnostic_fault_for_test(
    TurnSafeDiagnosticFaultStage stage);

enum class TurnSafeCaptureDisableReason : std::uint8_t {
  kNone,
  kUnsupportedSchemaVersion,
  kProvenanceConflict,
  kOutputOpenFailed,
  kOutputFdopenFailed,
  kOutputWriteFailed,
  kOutputRecordFlushFailed,
  kOutputFinalFlushOrFsyncFailed,
  kOutputCloseFailed,
  kOutputAtomicRenameFailed,
  kOutputDirectoryFsyncFailed,
  kNestedCallbackEnvelope,
  kMultipleUpdaterRecords,
  kUnterminatedCallbackEnvelope,
  kInjectedWriteFailure,
  kDiagnosticBadAlloc,
  kDiagnosticStdException,
  kDiagnosticUnknownException,
  kFrontendCaptureFailure,
  kUpdaterCaptureFailure,
};

const char *turnsafe_capture_disable_reason_name(
    TurnSafeCaptureDisableReason reason) noexcept;

struct TurnSafeResolvedConfiguration {
  bool one_pass_schur = false;
  bool fej_enabled = false;
  bool global_3d_transient = false;
  bool all_cameras_radtan = false;
  bool camera_extrinsic_calibration_off = false;
  bool camera_intrinsic_calibration_off = false;
  bool camera_time_offset_calibration_off = false;
  bool stereo_enabled = false;
  bool stereo_available = false;
  bool require_target_stereo_range = false;
  std::uint64_t camera_count = 0U;
  bool supported = false;
  std::vector<std::string> unsupported_reasons;
};

struct TurnSafeDiagnosticsOptions {
  bool capture_requested = false;
  std::string output_path;
  std::string schema_version = "turnsafe.t0.v1";
  std::string frozen_base_sha;
  // Caller values are expectations only. Runtime identity comes from the
  // generated build-provenance header and conflicts fail closed.
  std::string expected_source_sha;
  std::string expected_source_tree;
  std::string expected_source_snapshot_sha256;
  std::string expected_build_provenance_id;
  std::string source_sha;
  std::string source_tree;
  std::string source_snapshot_sha256;
  bool source_dirty = false;
  std::string build_provenance_id;
  std::string configure_manifest_sha256;
  std::string build_manifest_sha256;
  std::string binary_sha256;
  std::string config_sha256;
  std::string calibration_sha256;
  std::string diagnostic_schema_sha256;
  std::string digest_contract_version = "turnsafe.baseline_digest.v1";
  bool require_target_stereo_range = false;
  bool provenance_conflict = false;
  TurnSafeResolvedConfiguration resolved_configuration;
  std::uint64_t test_fail_after_callback_records =
      std::numeric_limits<std::uint64_t>::max();
};

class TurnSafeDiagnostics {
public:
  static std::shared_ptr<TurnSafeDiagnostics>
  Create(const TurnSafeDiagnosticsOptions &options) noexcept;

  static TurnSafeResolvedConfiguration EvaluateConfiguration(
      TurnSafeResolvedConfiguration configuration);

  ~TurnSafeDiagnostics() noexcept;

  bool active() const noexcept;
  const char *failure_reason() const noexcept;
  std::uint64_t failure_count() const noexcept;

  void BeginCallback(double timestamp) noexcept;
  void RecordFrontend(ov_core::TrackKLTFrameDiagnostics &&record) noexcept;
  void RecordUpdate(TurnSafeUpdateRecord &&record) noexcept;
  void EndCallback() noexcept;
  void ReportComponentFailure(TurnSafeCaptureDisableReason reason) noexcept;
  bool Finalize() noexcept;

  static TurnSafeFullTrackAttempt
  CaptureAttempt(const ov_core::Feature &feature,
                 std::uint64_t detached_index);
  static void CapturePriorPrimitives(
      const MSCKFUpdatePriorSnapshot &prior,
      std::vector<TurnSafeFullTrackAttempt> &attempts,
      TurnSafePriorPrimitives &output);
  static TurnSafeAcuteCertificateResult EvaluateAcuteCertificate(
      double translation_ucb, double range_lcb,
      const Eigen::Matrix2d &bearing_covariance);

private:
  struct CallbackRecord {
    std::uint64_t callback_index = 0U;
    double timestamp = 0.0;
    std::vector<ov_core::TrackKLTFrameDiagnostics> frontend;
    bool update_available = false;
    TurnSafeUpdateRecord update;
    bool no_full_visual_update_duration_available = false;
    double no_full_visual_update_duration = 0.0;
    enum class DurationReason : std::uint8_t {
      kNone,
      kNoPriorAcceptedFullUpdate,
      kTimestampNonfinite,
      kTimestampRegression,
      kUpdaterTimestampMismatch,
    } no_full_visual_update_duration_reason =
        DurationReason::kNoPriorAcceptedFullUpdate;
  };

  explicit TurnSafeDiagnostics(const TurnSafeDiagnosticsOptions &options)
      : options_(options) {}

  bool Initialize() noexcept;
  bool WriteLine(const std::string &line) noexcept;
  void Disable(TurnSafeCaptureDisableReason reason) noexcept;
  void DisableForCurrentException() noexcept;
  std::string SerializeHeader() const;
  std::string SerializeCallback(const CallbackRecord &record);

  TurnSafeDiagnosticsOptions options_;
  mutable std::mutex mutex_;
  std::atomic<bool> active_{false};
  bool finalized_ = false;
  std::atomic<std::uint8_t> failure_reason_{
      static_cast<std::uint8_t>(TurnSafeCaptureDisableReason::kNone)};
  std::atomic<std::uint64_t> failure_count_{0U};
  std::string temporary_path_;
  std::FILE *stream_ = nullptr;
  std::uint64_t next_callback_index_ = 0U;
  std::uint64_t callbacks_written_ = 0U;
  bool current_available_ = false;
  CallbackRecord current_;
  bool last_full_visual_timestamp_available_ = false;
  double last_full_visual_timestamp_ = 0.0;
  bool last_camera_timestamp_available_ = false;
  double last_camera_timestamp_ = 0.0;
};

} // namespace ov_msckf

#endif // OV_MSCKF_TURNSAFE_DIAGNOSTICS_H
