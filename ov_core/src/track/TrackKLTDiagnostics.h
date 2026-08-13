/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 passive KLT diagnostics.
 */

#ifndef OV_CORE_TRACK_KLT_DIAGNOSTICS_H
#define OV_CORE_TRACK_KLT_DIAGNOSTICS_H

#include <opencv2/core.hpp>

#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

namespace ov_core {

/** Value-only masks captured before the native KLT/RANSAC masks are combined. */
struct TrackKLTMatchingDiagnostics {
  std::size_t temporal_input_points = 0U;
  bool klt_performed = false;
  bool ransac_performed = false;
  std::vector<std::uint8_t> klt_status;
  std::vector<std::uint8_t> ransac_status;
  std::vector<float> klt_error;
};

enum class TrackKLTBoundsRule : std::uint8_t {
  kGreaterEqual,
  kGreater,
};

/** Fixed-size native lifecycle reason; string allocation never occurs here. */
enum class TrackKLTNativeReason : std::uint8_t {
  kNone,
  kInitialOrEmptyReseed,
  kTemporalMaskEmpty,
  kTopOffBeforeTemporalKlt,
  kNoReseed,
};

const char *track_klt_native_reason_name(TrackKLTNativeReason reason) noexcept;

/** Deterministic test-only faults, inactive unless explicitly selected. */
enum class TrackKLTDiagnosticFaultStage : std::uint8_t {
  kNone,
  kActivation,
  kMatchingCopy,
  kSummary,
  kAcceptedIdCopy,
  kFrameAssembly,
  kCallback,
};

enum class TrackKLTDiagnosticFaultKind : std::uint8_t {
  kNone,
  kBadAlloc,
  kStdException,
  kUnknown,
};

enum class TrackKLTDiagnosticFailureReason : std::uint8_t {
  kNone,
  kBadAlloc,
  kStdException,
  kUnknownException,
};

const char *track_klt_diagnostic_failure_name(
    TrackKLTDiagnosticFailureReason reason) noexcept;

void set_track_klt_diagnostic_fault_for_test(
    TrackKLTDiagnosticFaultStage stage,
    TrackKLTDiagnosticFaultKind kind) noexcept;
void clear_track_klt_diagnostic_fault_for_test() noexcept;
void inject_track_klt_diagnostic_fault_for_test(
    TrackKLTDiagnosticFaultStage stage);

/** One camera/frame record. It owns no tracker, image, or database object. */
struct TrackKLTFrameDiagnostics {
  std::size_t camera_id = 0U;
  double target_timestamp = std::numeric_limits<double>::quiet_NaN();
  bool source_timestamp_available = false;
  double source_timestamp = std::numeric_limits<double>::quiet_NaN();

  std::size_t previous_track_count = 0U;
  std::size_t reseed_count = 0U;
  std::size_t klt_attempted_count = 0U;
  bool klt_counts_available = false;
  std::size_t klt_status_survivors = 0U;
  std::size_t klt_status_rejections = 0U;
  std::size_t out_of_bounds_rejections = 0U;
  std::size_t in_bounds_survivors = 0U;
  bool mask_stage_present = false;
  std::size_t mask_rejections = 0U;
  std::size_t mask_survivors = 0U;
  bool ransac_counts_available = false;
  std::size_t fmatrix_input_points = 0U;
  std::size_t fmatrix_inliers = 0U;
  std::size_t fmatrix_rejections = 0U;
  std::size_t native_combined_track_survivors = 0U;
  std::size_t database_observations_written = 0U;
  bool reset_too_few_points = false;
  TrackKLTNativeReason reset_native_reason = TrackKLTNativeReason::kNone;
  std::vector<float> surviving_klt_errors;
  std::vector<std::size_t> native_accepted_feature_ids;
};

/**
 * Read-only accounting over already-produced native masks and points.
 *
 * The helper deliberately does not return an acceptance mask. The estimator
 * continues to consume the native control flow in TrackKLT; this function is
 * invoked only on capture-owned copies after both OpenCV calls have returned.
 */
TrackKLTFrameDiagnostics summarize_track_klt_diagnostics(
    const TrackKLTMatchingDiagnostics &matching,
    const std::vector<cv::KeyPoint> &target_points, int image_rows,
    int image_cols, const cv::Mat &native_mask, bool apply_native_mask,
    TrackKLTBoundsRule bounds_rule);

} // namespace ov_core

#endif // OV_CORE_TRACK_KLT_DIAGNOSTICS_H
