/*
 * SchurVIO-Lite fail-closed long-gap relocalization core.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#ifndef OV_MSCKF_LONG_GAP_RELOCALIZER_H
#define OV_MSCKF_LONG_GAP_RELOCALIZER_H

#include <Eigen/Core>

#include <cstddef>
#include <cstdint>
#include <vector>

namespace ov_msckf {

/** A retained global landmark and its raw-pixel observation in one camera. */
struct LongGapCorrespondence {
  std::uint64_t id = 0U;
  Eigen::Vector3d p_FinG = Eigen::Vector3d::Zero();
  Eigen::Vector2d uv_raw = Eigen::Vector2d::Zero();
};

/**
 * Frozen pinhole/radtan camera calibration used by the recovery solver.
 *
 * The extrinsic convention is ``p_C = R_ItoC * p_I + p_IinC``. ``K`` must
 * have the conventional zero-skew pinhole form, and ``distortion`` must be an
 * OpenCV radtan vector of length 0, 4, 5, 8, 12, or 14.
 */
struct LongGapCameraCalibration {
  Eigen::Matrix3d K = Eigen::Matrix3d::Identity();
  std::vector<double> distortion;
  int image_width = 0;
  int image_height = 0;
  Eigen::Matrix3d R_ItoC = Eigen::Matrix3d::Identity();
  Eigen::Vector3d p_IinC = Eigen::Vector3d::Zero();
};

enum class LongGapRelocalizationReason : std::uint8_t {
  ACCEPTED = 0,
  INVALID_CALIBRATION,
  INVALID_ORIENTATION_PRIOR,
  DUPLICATE_CORRESPONDENCE_ID,
  INSUFFICIENT_CORRESPONDENCES,
  NONCOLLINEAR_SUPPORT_REQUIRED,
  PNP_FAILED,
  INSUFFICIENT_INLIERS,
  INLIER_RATIO_TOO_LOW,
  NONFINITE_POSE,
  NONPOSITIVE_DEPTH,
  NONFINITE_RESIDUAL,
  REPROJECTION_ERROR_TOO_HIGH,
  IMAGE_BOUNDS_X_TOO_SMALL,
  IMAGE_BOUNDS_Y_TOO_SMALL,
  ORIENTATION_DISAGREEMENT_TOO_LARGE,
};

const char *long_gap_relocalization_reason_string(LongGapRelocalizationReason reason) noexcept;

/** Machine-readable measurements for every accepted or rejected candidate. */
struct LongGapRelocalizationMetrics {
  std::size_t supplied_correspondence_count = 0U;
  std::size_t valid_correspondence_count = 0U;
  std::size_t inlier_count = 0U;
  double inlier_ratio = 0.0;
  double max_reprojection_error_px = 0.0;
  double min_depth = 0.0;
  double image_span_x_fraction = 0.0;
  double image_span_y_fraction = 0.0;
  double second_to_first_3d_singular_ratio = 0.0;
  double orientation_disagreement_deg = 0.0;
};

/**
 * Value-only recovery result.
 *
 * ``R_GtoI`` and ``p_IinG`` are publishable only when ``accepted`` is true.
 * They deliberately remain identity/zero after every rejection. Inlier IDs
 * and metrics remain available for diagnostics and cannot mutate live state.
 */
struct LongGapRelocalizationResult {
  bool accepted = false;
  LongGapRelocalizationReason reason = LongGapRelocalizationReason::PNP_FAILED;
  Eigen::Matrix3d R_GtoI = Eigen::Matrix3d::Identity();
  Eigen::Vector3d p_IinG = Eigen::Vector3d::Zero();
  std::vector<std::uint64_t> inlier_ids;
  LongGapRelocalizationMetrics metrics;
};

class LongGapRelocalizerTestAccess;

/** Deterministic, stateless PnP/RANSAC recovery with the frozen C2 gates. */
class LongGapRelocalizer {
public:
  static constexpr std::size_t minimum_correspondences() noexcept { return 12U; }
  static constexpr std::size_t minimum_inliers() noexcept { return 12U; }
  static constexpr double minimum_inlier_ratio() noexcept { return 0.70; }
  static constexpr double maximum_reprojection_error_px() noexcept { return 2.0; }
  static constexpr double minimum_image_span_fraction() noexcept { return 0.25; }
  static constexpr double maximum_orientation_disagreement_deg() noexcept { return 5.0; }

  /**
   * Estimate one left-camera pose without retaining references or mutating any
   * caller-owned object. Nonfinite correspondences are ignored and therefore
   * do not count toward the twelve-valid-correspondence gate.
   */
  static LongGapRelocalizationResult
  recover(const std::vector<LongGapCorrespondence> &correspondences,
          const LongGapCameraCalibration &calibration,
          const Eigen::Matrix3d &imu_predicted_R_GtoI);

private:
  static LongGapRelocalizationResult
  evaluate_candidate(const std::vector<LongGapCorrespondence> &correspondences,
                     const LongGapCameraCalibration &calibration,
                     const Eigen::Matrix3d &imu_predicted_R_GtoI,
                     const Eigen::Matrix3d &candidate_R_GtoC,
                     const Eigen::Vector3d &candidate_t_GinC,
                     const std::vector<int> &inlier_indices,
                     std::size_t supplied_correspondence_count);

  friend class LongGapRelocalizerTestAccess;
};

} // namespace ov_msckf

#endif // OV_MSCKF_LONG_GAP_RELOCALIZER_H
