/*
 * SchurVIO-Lite fail-closed long-gap relocalization core.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "LongGapRelocalizer.h"

#include <Eigen/LU>
#include <Eigen/SVD>

#include <opencv2/calib3d/calib3d.hpp>
#include <opencv2/core/core.hpp>

#include <algorithm>
#include <cmath>
#include <limits>
#include <mutex>
#include <unordered_set>

namespace ov_msckf {
namespace {

// Match the single predeclared offline diagnostic that motivated C2. These are
// fixed protocol constants, not runtime tuning controls.
constexpr int kRansacIterations = 1000;
constexpr double kRansacConfidence = 0.999;
constexpr std::uint64_t kDeterministicRngState = 0x4c4752504e505631ULL;
constexpr double kRotationTolerance = 1e-6;
constexpr double kAbsoluteSupportTolerance = 1e-9;
constexpr double kRelativeSupportTolerance = 1e-6;
constexpr double kPi = 3.141592653589793238462643383279502884;

std::mutex &opencv_rng_mutex() {
  static std::mutex mutex;
  return mutex;
}

class ScopedOpenCvRngState {
public:
  explicit ScopedOpenCvRngState(std::uint64_t state)
      : lock_(opencv_rng_mutex()), previous_(cv::theRNG().state) {
    cv::theRNG().state = state;
  }

  ~ScopedOpenCvRngState() { cv::theRNG().state = previous_; }

  ScopedOpenCvRngState(const ScopedOpenCvRngState &) = delete;
  ScopedOpenCvRngState &operator=(const ScopedOpenCvRngState &) = delete;

private:
  std::unique_lock<std::mutex> lock_;
  std::uint64_t previous_;
};

bool is_rotation_matrix(const Eigen::Matrix3d &rotation) {
  if (!rotation.allFinite()) {
    return false;
  }
  const Eigen::Matrix3d orthogonality = rotation.transpose() * rotation - Eigen::Matrix3d::Identity();
  const double determinant = rotation.determinant();
  return orthogonality.cwiseAbs().maxCoeff() <= kRotationTolerance &&
         std::isfinite(determinant) && determinant > 0.0 &&
         std::abs(determinant - 1.0) <= kRotationTolerance;
}

bool supported_distortion_size(std::size_t size) {
  return size == 0U || size == 4U || size == 5U || size == 8U || size == 12U || size == 14U;
}

bool valid_calibration(const LongGapCameraCalibration &calibration) {
  if (!calibration.K.allFinite() || !calibration.p_IinC.allFinite() ||
      !is_rotation_matrix(calibration.R_ItoC) || calibration.image_width <= 0 ||
      calibration.image_height <= 0 || calibration.K(0, 0) <= 0.0 ||
      calibration.K(1, 1) <= 0.0 || !supported_distortion_size(calibration.distortion.size())) {
    return false;
  }

  constexpr double kPinholeMatrixTolerance = 1e-12;
  if (std::abs(calibration.K(0, 1)) > kPinholeMatrixTolerance ||
      std::abs(calibration.K(1, 0)) > kPinholeMatrixTolerance ||
      std::abs(calibration.K(2, 0)) > kPinholeMatrixTolerance ||
      std::abs(calibration.K(2, 1)) > kPinholeMatrixTolerance ||
      std::abs(calibration.K(2, 2) - 1.0) > kPinholeMatrixTolerance) {
    return false;
  }
  return std::all_of(calibration.distortion.begin(), calibration.distortion.end(),
                     [](double value) { return std::isfinite(value); });
}

cv::Mat camera_matrix(const LongGapCameraCalibration &calibration) {
  cv::Mat matrix(3, 3, CV_64F);
  for (int row = 0; row < 3; ++row) {
    for (int column = 0; column < 3; ++column) {
      matrix.at<double>(row, column) = calibration.K(row, column);
    }
  }
  return matrix;
}

cv::Mat distortion_vector(const LongGapCameraCalibration &calibration) {
  if (calibration.distortion.empty()) {
    return cv::Mat();
  }
  cv::Mat distortion(1, static_cast<int>(calibration.distortion.size()), CV_64F);
  for (std::size_t index = 0; index < calibration.distortion.size(); ++index) {
    distortion.at<double>(0, static_cast<int>(index)) = calibration.distortion.at(index);
  }
  return distortion;
}

double noncollinear_support_ratio(const std::vector<LongGapCorrespondence> &correspondences,
                                  const std::vector<int> *indices) {
  const std::size_t count = indices == nullptr ? correspondences.size() : indices->size();
  if (count < 3U) {
    return 0.0;
  }

  Eigen::Vector3d mean = Eigen::Vector3d::Zero();
  if (indices == nullptr) {
    for (const auto &correspondence : correspondences) {
      mean += correspondence.p_FinG;
    }
  } else {
    for (int index : *indices) {
      mean += correspondences.at(static_cast<std::size_t>(index)).p_FinG;
    }
  }
  mean /= static_cast<double>(count);

  Eigen::Matrix<double, 3, Eigen::Dynamic> centered(3, static_cast<Eigen::Index>(count));
  for (std::size_t column = 0; column < count; ++column) {
    const std::size_t index = indices == nullptr ? column : static_cast<std::size_t>(indices->at(column));
    centered.col(static_cast<Eigen::Index>(column)) = correspondences.at(index).p_FinG - mean;
  }
  const Eigen::JacobiSVD<Eigen::Matrix<double, 3, Eigen::Dynamic>> svd(centered, Eigen::ComputeThinU);
  const Eigen::VectorXd singular_values = svd.singularValues();
  if (singular_values.size() < 2 || !singular_values.allFinite() || singular_values(0) <= 0.0) {
    return 0.0;
  }
  return singular_values(1) / singular_values(0);
}

bool has_noncollinear_support(double ratio, const std::vector<LongGapCorrespondence> &correspondences,
                              const std::vector<int> *indices) {
  const std::size_t count = indices == nullptr ? correspondences.size() : indices->size();
  if (count < 3U || !std::isfinite(ratio) || ratio <= kRelativeSupportTolerance) {
    return false;
  }

  Eigen::Vector3d minimum = Eigen::Vector3d::Constant(std::numeric_limits<double>::infinity());
  Eigen::Vector3d maximum = Eigen::Vector3d::Constant(-std::numeric_limits<double>::infinity());
  for (std::size_t item = 0; item < count; ++item) {
    const std::size_t index = indices == nullptr ? item : static_cast<std::size_t>(indices->at(item));
    minimum = minimum.cwiseMin(correspondences.at(index).p_FinG);
    maximum = maximum.cwiseMax(correspondences.at(index).p_FinG);
  }
  return (maximum - minimum).norm() > kAbsoluteSupportTolerance;
}

double rotation_disagreement_degrees(const Eigen::Matrix3d &first, const Eigen::Matrix3d &second) {
  const Eigen::Matrix3d relative = first * second.transpose();
  const double cosine = std::max(-1.0, std::min(1.0, 0.5 * (relative.trace() - 1.0)));
  return std::acos(cosine) * 180.0 / kPi;
}

bool opencv_pose_to_eigen(const cv::Mat &rotation_vector,
                          const cv::Mat &translation_vector,
                          Eigen::Matrix3d &R_GtoC,
                          Eigen::Vector3d &t_GinC) {
  if (rotation_vector.total() != 3U || translation_vector.total() != 3U) {
    return false;
  }
  cv::Mat rotation_cv;
  cv::Mat translation_cv;
  try {
    cv::Rodrigues(rotation_vector, rotation_cv);
    rotation_cv.convertTo(rotation_cv, CV_64F);
    translation_vector.convertTo(translation_cv, CV_64F);
  } catch (const cv::Exception &) {
    return false;
  }
  if (rotation_cv.rows != 3 || rotation_cv.cols != 3 || translation_cv.total() != 3U) {
    return false;
  }
  for (int row = 0; row < 3; ++row) {
    t_GinC(row) = translation_cv.at<double>(row);
    for (int column = 0; column < 3; ++column) {
      R_GtoC(row, column) = rotation_cv.at<double>(row, column);
    }
  }
  return is_rotation_matrix(R_GtoC) && t_GinC.allFinite();
}

bool eigen_pose_to_opencv(const Eigen::Matrix3d &R_GtoC,
                          const Eigen::Vector3d &t_GinC,
                          cv::Mat &rotation_vector,
                          cv::Mat &translation_vector) {
  if (!is_rotation_matrix(R_GtoC) || !t_GinC.allFinite()) {
    return false;
  }
  cv::Mat rotation_cv(3, 3, CV_64F);
  translation_vector = cv::Mat(3, 1, CV_64F);
  for (int row = 0; row < 3; ++row) {
    translation_vector.at<double>(row) = t_GinC(row);
    for (int column = 0; column < 3; ++column) {
      rotation_cv.at<double>(row, column) = R_GtoC(row, column);
    }
  }
  try {
    cv::Rodrigues(rotation_cv, rotation_vector);
  } catch (const cv::Exception &) {
    return false;
  }
  return rotation_vector.total() == 3U;
}

bool reclassify_at_frozen_threshold(
    const std::vector<LongGapCorrespondence> &correspondences,
    const LongGapCameraCalibration &calibration,
    const Eigen::Matrix3d &R_GtoC,
    const Eigen::Vector3d &t_GinC,
    std::vector<int> &inlier_indices) {
  inlier_indices.clear();
  if (!is_rotation_matrix(R_GtoC) || !t_GinC.allFinite()) {
    return false;
  }

  std::vector<cv::Point3d> object_points;
  object_points.reserve(correspondences.size());
  for (const auto &correspondence : correspondences) {
    object_points.emplace_back(correspondence.p_FinG.x(),
                               correspondence.p_FinG.y(),
                               correspondence.p_FinG.z());
  }

  cv::Mat rotation_vector;
  cv::Mat translation_vector;
  if (!eigen_pose_to_opencv(R_GtoC, t_GinC, rotation_vector, translation_vector)) {
    return false;
  }
  std::vector<cv::Point2d> projections;
  try {
    cv::projectPoints(object_points, rotation_vector, translation_vector,
                      camera_matrix(calibration), distortion_vector(calibration),
                      projections);
  } catch (const cv::Exception &) {
    return false;
  }
  if (projections.size() != correspondences.size()) {
    return false;
  }

  for (std::size_t index = 0; index < correspondences.size(); ++index) {
    const Eigen::Vector3d point_in_camera =
        R_GtoC * correspondences.at(index).p_FinG + t_GinC;
    const double dx = projections.at(index).x - correspondences.at(index).uv_raw.x();
    const double dy = projections.at(index).y - correspondences.at(index).uv_raw.y();
    const double error = std::hypot(dx, dy);
    if (point_in_camera.allFinite() && point_in_camera.z() > 0.0 &&
        std::isfinite(error) && error <= LongGapRelocalizer::maximum_reprojection_error_px()) {
      inlier_indices.push_back(static_cast<int>(index));
    }
  }
  return true;
}

bool refine_on_fixed_inliers(
    const std::vector<LongGapCorrespondence> &correspondences,
    const LongGapCameraCalibration &calibration,
    const std::vector<int> &inlier_indices,
    Eigen::Matrix3d &R_GtoC,
    Eigen::Vector3d &t_GinC) {
  // OpenCV's iterative PnP with an extrinsic guess needs at least four points.
  if (inlier_indices.size() < 4U) {
    return false;
  }
  std::vector<cv::Point3d> object_points;
  std::vector<cv::Point2d> image_points;
  object_points.reserve(inlier_indices.size());
  image_points.reserve(inlier_indices.size());
  for (int index : inlier_indices) {
    if (index < 0 || static_cast<std::size_t>(index) >= correspondences.size()) {
      return false;
    }
    const LongGapCorrespondence &correspondence =
        correspondences.at(static_cast<std::size_t>(index));
    object_points.emplace_back(correspondence.p_FinG.x(),
                               correspondence.p_FinG.y(),
                               correspondence.p_FinG.z());
    image_points.emplace_back(correspondence.uv_raw.x(), correspondence.uv_raw.y());
  }

  cv::Mat rotation_vector;
  cv::Mat translation_vector;
  if (!eigen_pose_to_opencv(R_GtoC, t_GinC, rotation_vector, translation_vector)) {
    return false;
  }
  bool refined = false;
  try {
    refined = cv::solvePnP(object_points, image_points, camera_matrix(calibration),
                           distortion_vector(calibration), rotation_vector,
                           translation_vector, true, cv::SOLVEPNP_ITERATIVE);
  } catch (const cv::Exception &) {
    refined = false;
  }
  return refined && opencv_pose_to_eigen(rotation_vector, translation_vector,
                                         R_GtoC, t_GinC);
}

LongGapRelocalizationResult rejected(LongGapRelocalizationReason reason,
                                     std::size_t supplied_count,
                                     std::size_t valid_count) {
  LongGapRelocalizationResult result;
  result.reason = reason;
  result.metrics.supplied_correspondence_count = supplied_count;
  result.metrics.valid_correspondence_count = valid_count;
  return result;
}

} // namespace

const char *long_gap_relocalization_reason_string(LongGapRelocalizationReason reason) noexcept {
  switch (reason) {
  case LongGapRelocalizationReason::ACCEPTED:
    return "accepted";
  case LongGapRelocalizationReason::INVALID_CALIBRATION:
    return "invalid_calibration";
  case LongGapRelocalizationReason::INVALID_ORIENTATION_PRIOR:
    return "invalid_orientation_prior";
  case LongGapRelocalizationReason::DUPLICATE_CORRESPONDENCE_ID:
    return "duplicate_correspondence_id";
  case LongGapRelocalizationReason::INSUFFICIENT_CORRESPONDENCES:
    return "insufficient_correspondences";
  case LongGapRelocalizationReason::NONCOLLINEAR_SUPPORT_REQUIRED:
    return "noncollinear_support_required";
  case LongGapRelocalizationReason::PNP_FAILED:
    return "pnp_failed";
  case LongGapRelocalizationReason::INSUFFICIENT_INLIERS:
    return "insufficient_inliers";
  case LongGapRelocalizationReason::INLIER_RATIO_TOO_LOW:
    return "inlier_ratio_too_low";
  case LongGapRelocalizationReason::NONFINITE_POSE:
    return "nonfinite_pose";
  case LongGapRelocalizationReason::NONPOSITIVE_DEPTH:
    return "nonpositive_depth";
  case LongGapRelocalizationReason::NONFINITE_RESIDUAL:
    return "nonfinite_residual";
  case LongGapRelocalizationReason::REPROJECTION_ERROR_TOO_HIGH:
    return "reprojection_error_too_high";
  case LongGapRelocalizationReason::IMAGE_BOUNDS_X_TOO_SMALL:
    return "image_bounds_x_too_small";
  case LongGapRelocalizationReason::IMAGE_BOUNDS_Y_TOO_SMALL:
    return "image_bounds_y_too_small";
  case LongGapRelocalizationReason::ORIENTATION_DISAGREEMENT_TOO_LARGE:
    return "orientation_disagreement_too_large";
  }
  return "unknown";
}

LongGapRelocalizationResult
LongGapRelocalizer::recover(const std::vector<LongGapCorrespondence> &correspondences,
                            const LongGapCameraCalibration &calibration,
                            const Eigen::Matrix3d &imu_predicted_R_GtoI) {
  const std::size_t supplied_count = correspondences.size();
  if (!valid_calibration(calibration)) {
    return rejected(LongGapRelocalizationReason::INVALID_CALIBRATION, supplied_count, 0U);
  }
  if (!is_rotation_matrix(imu_predicted_R_GtoI)) {
    return rejected(LongGapRelocalizationReason::INVALID_ORIENTATION_PRIOR, supplied_count, 0U);
  }

  std::vector<LongGapCorrespondence> valid;
  valid.reserve(correspondences.size());
  std::unordered_set<std::uint64_t> ids;
  bool duplicate_id = false;
  for (const auto &correspondence : correspondences) {
    if (!correspondence.p_FinG.allFinite() || !correspondence.uv_raw.allFinite()) {
      continue;
    }
    duplicate_id = !ids.insert(correspondence.id).second || duplicate_id;
    valid.push_back(correspondence);
  }
  if (duplicate_id) {
    return rejected(LongGapRelocalizationReason::DUPLICATE_CORRESPONDENCE_ID, supplied_count, valid.size());
  }
  if (valid.size() < minimum_correspondences()) {
    return rejected(LongGapRelocalizationReason::INSUFFICIENT_CORRESPONDENCES, supplied_count, valid.size());
  }

  const double full_support_ratio = noncollinear_support_ratio(valid, nullptr);
  if (!has_noncollinear_support(full_support_ratio, valid, nullptr)) {
    LongGapRelocalizationResult result =
        rejected(LongGapRelocalizationReason::NONCOLLINEAR_SUPPORT_REQUIRED, supplied_count, valid.size());
    result.metrics.second_to_first_3d_singular_ratio = full_support_ratio;
    return result;
  }

  std::vector<cv::Point3d> object_points;
  std::vector<cv::Point2d> image_points;
  object_points.reserve(valid.size());
  image_points.reserve(valid.size());
  for (const auto &correspondence : valid) {
    object_points.emplace_back(correspondence.p_FinG.x(), correspondence.p_FinG.y(), correspondence.p_FinG.z());
    image_points.emplace_back(correspondence.uv_raw.x(), correspondence.uv_raw.y());
  }

  cv::Mat rotation_vector;
  cv::Mat translation_vector;
  cv::Mat raw_inliers;
  bool solved = false;
  try {
    ScopedOpenCvRngState deterministic_rng(kDeterministicRngState);
    solved = cv::solvePnPRansac(object_points, image_points, camera_matrix(calibration),
                               distortion_vector(calibration), rotation_vector, translation_vector,
                               false, kRansacIterations,
                               static_cast<float>(maximum_reprojection_error_px()),
                               kRansacConfidence, raw_inliers, cv::SOLVEPNP_EPNP);
  } catch (const cv::Exception &) {
    solved = false;
  }
  if (!solved || rotation_vector.total() != 3U || translation_vector.total() != 3U || raw_inliers.empty()) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }

  Eigen::Matrix3d candidate_R_GtoC;
  Eigen::Vector3d candidate_t_GinC;
  if (!opencv_pose_to_eigen(rotation_vector, translation_vector,
                            candidate_R_GtoC, candidate_t_GinC)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }

  // Do not trust OpenCV's raw RANSAC membership after its final pose fit. The
  // C2 fail-closed post-fit contract recomputes membership at 2 px, refines on
  // that fixed set, reclassifies, and performs one final fixed-set refinement.
  std::vector<int> first_inliers;
  if (!reclassify_at_frozen_threshold(valid, calibration, candidate_R_GtoC,
                                      candidate_t_GinC, first_inliers)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }
  if (first_inliers.size() < 4U) {
    return evaluate_candidate(valid, calibration, imu_predicted_R_GtoI,
                              candidate_R_GtoC, candidate_t_GinC,
                              first_inliers, supplied_count);
  }
  if (!refine_on_fixed_inliers(valid, calibration, first_inliers,
                               candidate_R_GtoC, candidate_t_GinC)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }

  std::vector<int> second_inliers;
  if (!reclassify_at_frozen_threshold(valid, calibration, candidate_R_GtoC,
                                      candidate_t_GinC, second_inliers)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }
  if (second_inliers.size() < 4U) {
    return evaluate_candidate(valid, calibration, imu_predicted_R_GtoI,
                              candidate_R_GtoC, candidate_t_GinC,
                              second_inliers, supplied_count);
  }
  if (!refine_on_fixed_inliers(valid, calibration, second_inliers,
                               candidate_R_GtoC, candidate_t_GinC)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }

  std::vector<int> final_inliers;
  if (!reclassify_at_frozen_threshold(valid, calibration, candidate_R_GtoC,
                                      candidate_t_GinC, final_inliers)) {
    return rejected(LongGapRelocalizationReason::PNP_FAILED, supplied_count, valid.size());
  }
  return evaluate_candidate(valid, calibration, imu_predicted_R_GtoI,
                            candidate_R_GtoC, candidate_t_GinC, final_inliers,
                            supplied_count);
}

LongGapRelocalizationResult
LongGapRelocalizer::evaluate_candidate(const std::vector<LongGapCorrespondence> &correspondences,
                                       const LongGapCameraCalibration &calibration,
                                       const Eigen::Matrix3d &imu_predicted_R_GtoI,
                                       const Eigen::Matrix3d &candidate_R_GtoC,
                                       const Eigen::Vector3d &candidate_t_GinC,
                                       const std::vector<int> &inlier_indices,
                                       std::size_t supplied_correspondence_count) {
  LongGapRelocalizationResult result;
  result.metrics.supplied_correspondence_count = supplied_correspondence_count;
  result.metrics.valid_correspondence_count = correspondences.size();

  std::vector<int> sorted_inliers = inlier_indices;
  std::sort(sorted_inliers.begin(), sorted_inliers.end());
  if (std::adjacent_find(sorted_inliers.begin(), sorted_inliers.end()) != sorted_inliers.end() ||
      std::any_of(sorted_inliers.begin(), sorted_inliers.end(), [&](int index) {
        return index < 0 || static_cast<std::size_t>(index) >= correspondences.size();
      })) {
    result.reason = LongGapRelocalizationReason::PNP_FAILED;
    return result;
  }

  result.metrics.inlier_count = sorted_inliers.size();
  if (!correspondences.empty()) {
    result.metrics.inlier_ratio =
        static_cast<double>(sorted_inliers.size()) / static_cast<double>(correspondences.size());
  }
  result.inlier_ids.reserve(sorted_inliers.size());
  for (int index : sorted_inliers) {
    result.inlier_ids.push_back(correspondences.at(static_cast<std::size_t>(index)).id);
  }

  if (sorted_inliers.size() < minimum_inliers()) {
    result.reason = LongGapRelocalizationReason::INSUFFICIENT_INLIERS;
    return result;
  }
  if (!std::isfinite(result.metrics.inlier_ratio) ||
      result.metrics.inlier_ratio < minimum_inlier_ratio()) {
    result.reason = LongGapRelocalizationReason::INLIER_RATIO_TOO_LOW;
    return result;
  }
  if (!is_rotation_matrix(candidate_R_GtoC) || !candidate_t_GinC.allFinite()) {
    result.reason = LongGapRelocalizationReason::NONFINITE_POSE;
    return result;
  }

  const Eigen::Matrix3d recovered_R_GtoI = calibration.R_ItoC.transpose() * candidate_R_GtoC;
  const Eigen::Vector3d recovered_p_IinG =
      candidate_R_GtoC.transpose() * (calibration.p_IinC - candidate_t_GinC);
  if (!is_rotation_matrix(recovered_R_GtoI) || !recovered_p_IinG.allFinite()) {
    result.reason = LongGapRelocalizationReason::NONFINITE_POSE;
    return result;
  }

  result.metrics.min_depth = std::numeric_limits<double>::infinity();
  double minimum_x = std::numeric_limits<double>::infinity();
  double maximum_x = -std::numeric_limits<double>::infinity();
  double minimum_y = std::numeric_limits<double>::infinity();
  double maximum_y = -std::numeric_limits<double>::infinity();
  std::vector<cv::Point3d> inlier_object_points;
  std::vector<cv::Point2d> inlier_observations;
  inlier_object_points.reserve(sorted_inliers.size());
  inlier_observations.reserve(sorted_inliers.size());
  for (int index : sorted_inliers) {
    const LongGapCorrespondence &correspondence = correspondences.at(static_cast<std::size_t>(index));
    const Eigen::Vector3d point_in_camera = candidate_R_GtoC * correspondence.p_FinG + candidate_t_GinC;
    if (!point_in_camera.allFinite()) {
      result.reason = LongGapRelocalizationReason::NONFINITE_RESIDUAL;
      return result;
    }
    result.metrics.min_depth = std::min(result.metrics.min_depth, point_in_camera.z());
    minimum_x = std::min(minimum_x, correspondence.uv_raw.x());
    maximum_x = std::max(maximum_x, correspondence.uv_raw.x());
    minimum_y = std::min(minimum_y, correspondence.uv_raw.y());
    maximum_y = std::max(maximum_y, correspondence.uv_raw.y());
    inlier_object_points.emplace_back(correspondence.p_FinG.x(), correspondence.p_FinG.y(), correspondence.p_FinG.z());
    inlier_observations.emplace_back(correspondence.uv_raw.x(), correspondence.uv_raw.y());
  }
  if (!std::isfinite(result.metrics.min_depth) || result.metrics.min_depth <= 0.0) {
    result.reason = LongGapRelocalizationReason::NONPOSITIVE_DEPTH;
    return result;
  }

  cv::Mat candidate_rotation_cv(3, 3, CV_64F);
  for (int row = 0; row < 3; ++row) {
    for (int column = 0; column < 3; ++column) {
      candidate_rotation_cv.at<double>(row, column) = candidate_R_GtoC(row, column);
    }
  }
  cv::Mat candidate_rotation_vector;
  cv::Mat candidate_translation_vector(3, 1, CV_64F);
  for (int row = 0; row < 3; ++row) {
    candidate_translation_vector.at<double>(row) = candidate_t_GinC(row);
  }

  std::vector<cv::Point2d> projections;
  try {
    cv::Rodrigues(candidate_rotation_cv, candidate_rotation_vector);
    cv::projectPoints(inlier_object_points, candidate_rotation_vector,
                      candidate_translation_vector, camera_matrix(calibration),
                      distortion_vector(calibration), projections);
  } catch (const cv::Exception &) {
    result.reason = LongGapRelocalizationReason::NONFINITE_RESIDUAL;
    return result;
  }
  if (projections.size() != inlier_observations.size()) {
    result.reason = LongGapRelocalizationReason::NONFINITE_RESIDUAL;
    return result;
  }

  result.metrics.max_reprojection_error_px = 0.0;
  for (std::size_t index = 0; index < projections.size(); ++index) {
    const double dx = projections.at(index).x - inlier_observations.at(index).x;
    const double dy = projections.at(index).y - inlier_observations.at(index).y;
    const double error = std::hypot(dx, dy);
    if (!std::isfinite(error)) {
      result.reason = LongGapRelocalizationReason::NONFINITE_RESIDUAL;
      return result;
    }
    result.metrics.max_reprojection_error_px =
        std::max(result.metrics.max_reprojection_error_px, error);
  }
  if (result.metrics.max_reprojection_error_px > maximum_reprojection_error_px()) {
    result.reason = LongGapRelocalizationReason::REPROJECTION_ERROR_TOO_HIGH;
    return result;
  }

  result.metrics.second_to_first_3d_singular_ratio =
      noncollinear_support_ratio(correspondences, &sorted_inliers);
  if (!has_noncollinear_support(result.metrics.second_to_first_3d_singular_ratio,
                                correspondences, &sorted_inliers)) {
    result.reason = LongGapRelocalizationReason::NONCOLLINEAR_SUPPORT_REQUIRED;
    return result;
  }

  result.metrics.image_span_x_fraction =
      (maximum_x - minimum_x) / static_cast<double>(calibration.image_width);
  result.metrics.image_span_y_fraction =
      (maximum_y - minimum_y) / static_cast<double>(calibration.image_height);
  if (!std::isfinite(result.metrics.image_span_x_fraction) ||
      result.metrics.image_span_x_fraction < minimum_image_span_fraction()) {
    result.reason = LongGapRelocalizationReason::IMAGE_BOUNDS_X_TOO_SMALL;
    return result;
  }
  if (!std::isfinite(result.metrics.image_span_y_fraction) ||
      result.metrics.image_span_y_fraction < minimum_image_span_fraction()) {
    result.reason = LongGapRelocalizationReason::IMAGE_BOUNDS_Y_TOO_SMALL;
    return result;
  }

  result.metrics.orientation_disagreement_deg =
      rotation_disagreement_degrees(recovered_R_GtoI, imu_predicted_R_GtoI);
  if (!std::isfinite(result.metrics.orientation_disagreement_deg) ||
      result.metrics.orientation_disagreement_deg > maximum_orientation_disagreement_deg()) {
    result.reason = LongGapRelocalizationReason::ORIENTATION_DISAGREEMENT_TOO_LARGE;
    return result;
  }

  result.accepted = true;
  result.reason = LongGapRelocalizationReason::ACCEPTED;
  result.R_GtoI = recovered_R_GtoI;
  result.p_IinG = recovered_p_IinG;
  return result;
}

} // namespace ov_msckf
