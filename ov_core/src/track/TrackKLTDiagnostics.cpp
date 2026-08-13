/*
 * SPDX-License-Identifier: GPL-3.0-or-later
 * TurnSafe Session-1 passive KLT diagnostics.
 */

#include "TrackKLTDiagnostics.h"

#include <algorithm>
#include <atomic>
#include <new>
#include <stdexcept>

namespace ov_core {
namespace {

std::atomic<std::uint8_t> diagnostic_fault_stage{
    static_cast<std::uint8_t>(TrackKLTDiagnosticFaultStage::kNone)};
std::atomic<std::uint8_t> diagnostic_fault_kind{
    static_cast<std::uint8_t>(TrackKLTDiagnosticFaultKind::kNone)};

} // namespace

const char *track_klt_native_reason_name(
    TrackKLTNativeReason reason) noexcept {
  switch (reason) {
  case TrackKLTNativeReason::kNone: return "NONE";
  case TrackKLTNativeReason::kInitialOrEmptyReseed:
    return "INITIAL_OR_EMPTY_RESEED";
  case TrackKLTNativeReason::kTemporalMaskEmpty: return "TEMPORAL_MASK_EMPTY";
  case TrackKLTNativeReason::kTopOffBeforeTemporalKlt:
    return "TOP_OFF_BEFORE_TEMPORAL_KLT";
  case TrackKLTNativeReason::kNoReseed: return "NO_RESEED";
  }
  return "UNKNOWN";
}

const char *track_klt_diagnostic_failure_name(
    TrackKLTDiagnosticFailureReason reason) noexcept {
  switch (reason) {
  case TrackKLTDiagnosticFailureReason::kNone: return "NONE";
  case TrackKLTDiagnosticFailureReason::kBadAlloc: return "BAD_ALLOC";
  case TrackKLTDiagnosticFailureReason::kStdException: return "STD_EXCEPTION";
  case TrackKLTDiagnosticFailureReason::kUnknownException:
    return "UNKNOWN_EXCEPTION";
  }
  return "UNKNOWN_EXCEPTION";
}

void set_track_klt_diagnostic_fault_for_test(
    TrackKLTDiagnosticFaultStage stage,
    TrackKLTDiagnosticFaultKind kind) noexcept {
  diagnostic_fault_kind.store(static_cast<std::uint8_t>(kind),
                              std::memory_order_release);
  diagnostic_fault_stage.store(static_cast<std::uint8_t>(stage),
                               std::memory_order_release);
}

void clear_track_klt_diagnostic_fault_for_test() noexcept {
  diagnostic_fault_stage.store(
      static_cast<std::uint8_t>(TrackKLTDiagnosticFaultStage::kNone),
      std::memory_order_release);
  diagnostic_fault_kind.store(
      static_cast<std::uint8_t>(TrackKLTDiagnosticFaultKind::kNone),
      std::memory_order_release);
}

void inject_track_klt_diagnostic_fault_for_test(
    TrackKLTDiagnosticFaultStage stage) {
  if (diagnostic_fault_stage.load(std::memory_order_acquire) !=
      static_cast<std::uint8_t>(stage)) {
    return;
  }
  switch (static_cast<TrackKLTDiagnosticFaultKind>(
      diagnostic_fault_kind.load(std::memory_order_acquire))) {
  case TrackKLTDiagnosticFaultKind::kNone: return;
  case TrackKLTDiagnosticFaultKind::kBadAlloc: throw std::bad_alloc();
  case TrackKLTDiagnosticFaultKind::kStdException:
    throw std::runtime_error("injected TrackKLT diagnostic failure");
  case TrackKLTDiagnosticFaultKind::kUnknown: throw 1;
  }
}

TrackKLTFrameDiagnostics summarize_track_klt_diagnostics(
    const TrackKLTMatchingDiagnostics &matching,
    const std::vector<cv::KeyPoint> &target_points, int image_rows,
    int image_cols, const cv::Mat &native_mask, bool apply_native_mask,
    TrackKLTBoundsRule bounds_rule) {
  TrackKLTFrameDiagnostics output;
  output.previous_track_count = matching.temporal_input_points;
  output.klt_counts_available = matching.klt_performed;
  output.ransac_counts_available = matching.ransac_performed;
  output.mask_stage_present = apply_native_mask;

  if (matching.klt_performed) {
    output.klt_attempted_count = matching.temporal_input_points;
    const std::size_t count = std::min(
        target_points.size(), matching.klt_status.size());
    for (std::size_t index = 0U; index < matching.temporal_input_points;
         ++index) {
      const bool status_survived =
          index < count && matching.klt_status[index] != 0U;
      if (!status_survived) {
        ++output.klt_status_rejections;
        continue;
      }
      ++output.klt_status_survivors;
      if (index < matching.klt_error.size()) {
        output.surviving_klt_errors.push_back(matching.klt_error[index]);
      }

      const cv::Point2f &point = target_points[index].pt;
      const bool upper_out =
          bounds_rule == TrackKLTBoundsRule::kGreater
              ? static_cast<int>(point.x) > image_cols ||
                    static_cast<int>(point.y) > image_rows
              : static_cast<int>(point.x) >= image_cols ||
                    static_cast<int>(point.y) >= image_rows;
      const bool out_of_bounds =
          point.x < 0.0F || point.y < 0.0F || upper_out;
      if (out_of_bounds) {
        ++output.out_of_bounds_rejections;
        continue;
      }
      ++output.in_bounds_survivors;

      const bool usable_native_mask =
          !apply_native_mask ||
          (native_mask.type() == CV_8UC1 &&
           static_cast<int>(point.x) < native_mask.cols &&
           static_cast<int>(point.y) < native_mask.rows);
      if (!usable_native_mask) {
        ++output.mask_rejections;
        continue;
      }
      if (apply_native_mask &&
          static_cast<int>(native_mask.at<std::uint8_t>(
              static_cast<int>(point.y), static_cast<int>(point.x))) > 127) {
        ++output.mask_rejections;
      } else {
        ++output.mask_survivors;
      }

      const bool ransac_survived =
          !matching.ransac_performed ||
          (index < matching.ransac_status.size() &&
           matching.ransac_status[index] != 0U);
      const bool native_mask_survived =
          !apply_native_mask || (!native_mask.empty() &&
          static_cast<int>(native_mask.at<std::uint8_t>(
              static_cast<int>(point.y), static_cast<int>(point.x))) <= 127);
      if (ransac_survived && native_mask_survived) {
        ++output.native_combined_track_survivors;
      }
    }
  }

  if (matching.ransac_performed) {
    output.fmatrix_input_points = matching.temporal_input_points;
    for (std::size_t index = 0U; index < matching.temporal_input_points;
         ++index) {
      if (index < matching.ransac_status.size() &&
          matching.ransac_status[index] != 0U) {
        ++output.fmatrix_inliers;
      } else {
        ++output.fmatrix_rejections;
      }
    }
  }
  return output;
}

} // namespace ov_core
