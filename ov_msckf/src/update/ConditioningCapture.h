/*
 * SchurVIO-Lite research-only camera conditioning capture.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_CONDITIONING_CAPTURE_H
#define OV_MSCKF_CONDITIONING_CAPTURE_H

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "UpdaterHelper.h"
#include "UpdaterMSCKFPreview.h"
#include "UpdaterOptions.h"

namespace ov_type {
class Type;
} // namespace ov_type

namespace ov_msckf {

class State;

enum class ConditioningCaptureVariableKind : std::uint64_t {
  kUnknown = 0U,
  kClone = 1U,
  kCameraExtrinsics = 2U,
  kCameraIntrinsics = 3U,
  kCameraTimeOffset = 4U,
};

enum class ConditioningCaptureKeyKind : std::uint64_t {
  kNone = 0U,
  kTimestamp = 1U,
  kCameraId = 2U,
};

enum class ConditioningCaptureCameraModel : std::uint64_t {
  kUnknown = 0U,
  kRadtan = 1U,
  kEquidistant = 2U,
};

struct ConditioningCaptureLayoutBlock {
  std::uint64_t local_column = 0U;
  std::uint64_t covariance_column = 0U;
  std::uint64_t size = 0U;
  ConditioningCaptureVariableKind kind =
      ConditioningCaptureVariableKind::kUnknown;
  ConditioningCaptureKeyKind key_kind = ConditioningCaptureKeyKind::kNone;
  std::uint64_t key_u64 = 0U;
  double key_double = 0.0;
};

struct ConditioningCaptureObservation {
  std::uint64_t camera_id = 0U;
  double timestamp = 0.0;
  ConditioningCaptureCameraModel camera_model =
      ConditioningCaptureCameraModel::kUnknown;
  /// Current signed optical-axis depth p_FinC.z in metres.
  double depth = 0.0;
};

/**
 * One owning raw per-track camera system captured before elimination/gating.
 *
 * P_active follows the exact H_x column order. Observation order follows the
 * production unordered-map traversal used to assemble the raw matrix rows.
 */
struct ConditioningCaptureRecord {
  std::uint64_t record_index = 0U;
  std::uint64_t update_index = 0U;
  std::uint64_t feature_ordinal = 0U;
  double update_timestamp = 0.0;
  std::uint64_t feature_id = 0U;
  bool geometry_valid = false;
  bool fej_enabled = false;
  std::uint64_t calibration_flags = 0U;
  std::int64_t feature_representation = -1;
  double sigma_px = 0.0;
  double noise_variance = 0.0;
  double minimum_singular_ratio = 0.0;
  bool singular_values_available = false;
  Eigen::Vector3d singular_values = Eigen::Vector3d::Zero();
  std::int64_t numerical_rank = 0;
  bool singular_ratio_available = false;
  double singular_ratio = 0.0;
  Eigen::MatrixXd H_x;
  Eigen::MatrixXd H_f;
  Eigen::VectorXd residual;
  Eigen::MatrixXd P_active;
  Eigen::Vector3d p_FinG = Eigen::Vector3d::Zero();
  std::vector<ConditioningCaptureLayoutBlock> layout;
  std::vector<ConditioningCaptureObservation> observations;
  double minimum_depth = 0.0;
  /// Maximum pairwise current global-ray angle in radians.
  double maximum_parallax_rad = 0.0;
};

/**
 * Fail-open, default-off deterministic binary writer.
 *
 * Every exception or I/O error is contained and permanently disables this
 * writer. It never reports a decision to estimator code and owns no live
 * estimator object. A failed capture can therefore invalidate only the
 * research artifact, never the visual update.
 */
class ConditioningCaptureWriter {
public:
  explicit ConditioningCaptureWriter(const UpdaterOptions &options) noexcept;
  ~ConditioningCaptureWriter() noexcept;

  ConditioningCaptureWriter(const ConditioningCaptureWriter &) = delete;
  ConditioningCaptureWriter &operator=(const ConditioningCaptureWriter &) =
      delete;

  bool active() const noexcept;
  bool failed() const noexcept;
  std::uint64_t record_count() const noexcept;

  void TryCapture(
      const std::shared_ptr<State> &state,
      const UpdaterHelper::UpdaterHelperFeature &feature,
      const MSCKFUpdatePriorSnapshot &prior,
      const std::vector<std::shared_ptr<ov_type::Type>> &H_x_order,
      const Eigen::MatrixXd &H_x, const Eigen::MatrixXd &H_f,
      const Eigen::VectorXd &residual) noexcept;

  /// Public pure seams for deterministic unit and cross-language tests.
  static bool ValidateRecord(const ConditioningCaptureRecord &record) noexcept;
  static std::vector<std::uint8_t>
  EncodeRecord(const ConditioningCaptureRecord &record);

private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

const char *conditioning_capture_source_commit() noexcept;

} // namespace ov_msckf

#endif // OV_MSCKF_CONDITIONING_CAPTURE_H
