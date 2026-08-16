/*
 * SchurVIO-Lite fail-closed long-gap relocalization tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "core/LongGapRelocalizer.h"

#include <gtest/gtest.h>

#include <Eigen/Geometry>

#include <opencv2/calib3d/calib3d.hpp>
#include <opencv2/core/core.hpp>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

namespace ov_msckf {

class LongGapRelocalizerTestAccess {
public:
  static LongGapRelocalizationResult
  evaluate(const std::vector<LongGapCorrespondence> &correspondences,
           const LongGapCameraCalibration &calibration,
           const Eigen::Matrix3d &imu_predicted_R_GtoI,
           const Eigen::Matrix3d &candidate_R_GtoC,
           const Eigen::Vector3d &candidate_t_GinC,
           const std::vector<int> &inlier_indices) {
    return LongGapRelocalizer::evaluate_candidate(
        correspondences, calibration, imu_predicted_R_GtoI,
        candidate_R_GtoC, candidate_t_GinC, inlier_indices,
        correspondences.size());
  }
};

} // namespace ov_msckf

namespace {

using ov_msckf::LongGapCameraCalibration;
using ov_msckf::LongGapCorrespondence;
using ov_msckf::LongGapRelocalizationReason;
using ov_msckf::LongGapRelocalizationResult;
using ov_msckf::LongGapRelocalizer;
using ov_msckf::LongGapRelocalizerTestAccess;

constexpr double kTestPi = 3.141592653589793238462643383279502884;

struct SyntheticScene {
  LongGapCameraCalibration calibration;
  Eigen::Matrix3d R_GtoI;
  Eigen::Vector3d p_IinG;
  Eigen::Matrix3d R_GtoC;
  Eigen::Vector3d t_GinC;
  std::vector<LongGapCorrespondence> correspondences;
};

cv::Mat to_camera_matrix(const Eigen::Matrix3d &value) {
  cv::Mat output(3, 3, CV_64F);
  for (int row = 0; row < 3; ++row) {
    for (int column = 0; column < 3; ++column) {
      output.at<double>(row, column) = value(row, column);
    }
  }
  return output;
}

cv::Mat to_distortion(const std::vector<double> &value) {
  cv::Mat output(1, static_cast<int>(value.size()), CV_64F);
  for (std::size_t index = 0; index < value.size(); ++index) {
    output.at<double>(0, static_cast<int>(index)) = value.at(index);
  }
  return output;
}

Eigen::Vector2d project(const LongGapCameraCalibration &calibration,
                        const Eigen::Vector3d &point_in_camera) {
  std::vector<cv::Point3d> object_points{
      cv::Point3d(point_in_camera.x(), point_in_camera.y(), point_in_camera.z())};
  std::vector<cv::Point2d> image_points;
  const cv::Mat zero = cv::Mat::zeros(3, 1, CV_64F);
  cv::projectPoints(object_points, zero, zero, to_camera_matrix(calibration.K),
                    to_distortion(calibration.distortion), image_points);
  EXPECT_EQ(image_points.size(), 1U);
  return Eigen::Vector2d(image_points.at(0).x, image_points.at(0).y);
}

std::vector<Eigen::Vector3d> broad_camera_points() {
  std::vector<Eigen::Vector3d> points;
  for (int row = 0; row < 4; ++row) {
    for (int column = 0; column < 5; ++column) {
      points.emplace_back(-1.6 + 0.8 * static_cast<double>(column),
                          -1.2 + 0.8 * static_cast<double>(row),
                          4.5 + 0.25 * static_cast<double>((column + 2 * row) % 4));
    }
  }
  return points;
}

SyntheticScene make_scene(const std::vector<Eigen::Vector3d> &points_in_camera = broad_camera_points()) {
  SyntheticScene scene;
  scene.calibration.K << 420.0, 0.0, 320.0,
                         0.0, 415.0, 240.0,
                         0.0, 0.0, 1.0;
  scene.calibration.distortion = {0.006, -0.009, 0.0003, 0.002};
  scene.calibration.image_width = 640;
  scene.calibration.image_height = 480;
  scene.calibration.R_ItoC =
      Eigen::AngleAxisd(2.0 * kTestPi / 180.0, Eigen::Vector3d::UnitX()).toRotationMatrix() *
      Eigen::AngleAxisd(-1.0 * kTestPi / 180.0, Eigen::Vector3d::UnitZ()).toRotationMatrix();
  scene.calibration.p_IinC = Eigen::Vector3d(0.05, -0.01, 0.02);
  scene.R_GtoI =
      Eigen::AngleAxisd(-4.0 * kTestPi / 180.0, Eigen::Vector3d::UnitZ()).toRotationMatrix() *
      Eigen::AngleAxisd(5.0 * kTestPi / 180.0, Eigen::Vector3d::UnitY()).toRotationMatrix();
  scene.p_IinG = Eigen::Vector3d(0.3, -0.2, 0.5);
  scene.R_GtoC = scene.calibration.R_ItoC * scene.R_GtoI;
  scene.t_GinC = scene.calibration.p_IinC - scene.R_GtoC * scene.p_IinG;

  scene.correspondences.reserve(points_in_camera.size());
  for (std::size_t index = 0; index < points_in_camera.size(); ++index) {
    LongGapCorrespondence correspondence;
    correspondence.id = 1000U + static_cast<std::uint64_t>(index);
    correspondence.p_FinG =
        scene.R_GtoC.transpose() * (points_in_camera.at(index) - scene.calibration.p_IinC) +
        scene.p_IinG;
    correspondence.uv_raw = project(scene.calibration, points_in_camera.at(index));
    scene.correspondences.push_back(correspondence);
  }
  return scene;
}

std::vector<int> first_indices(std::size_t count) {
  std::vector<int> indices(count);
  std::iota(indices.begin(), indices.end(), 0);
  return indices;
}

void expect_neutral_rejection(const LongGapRelocalizationResult &result,
                              LongGapRelocalizationReason expected_reason) {
  EXPECT_FALSE(result.accepted);
  EXPECT_EQ(result.reason, expected_reason);
  EXPECT_TRUE(result.R_GtoI.isApprox(Eigen::Matrix3d::Identity(), 0.0));
  EXPECT_TRUE(result.p_IinG.isZero(0.0));
}

LongGapRelocalizationResult evaluate_exact(const SyntheticScene &scene,
                                           const std::vector<int> &inliers) {
  return LongGapRelocalizerTestAccess::evaluate(
      scene.correspondences, scene.calibration, scene.R_GtoI,
      scene.R_GtoC, scene.t_GinC, inliers);
}

TEST(LongGapRelocalizer, SyntheticRadtanPosePassesEveryFrozenGate) {
  const SyntheticScene scene = make_scene();
  cv::theRNG().state = 0x123456789abcdef0ULL;
  const std::uint64_t saved_rng_state = cv::theRNG().state;

  const LongGapRelocalizationResult first =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  EXPECT_EQ(cv::theRNG().state, saved_rng_state);
  ASSERT_TRUE(first.accepted) << ov_msckf::long_gap_relocalization_reason_string(first.reason);
  EXPECT_EQ(first.reason, LongGapRelocalizationReason::ACCEPTED);
  EXPECT_STREQ(ov_msckf::long_gap_relocalization_reason_string(first.reason), "accepted");
  EXPECT_LT((first.R_GtoI - scene.R_GtoI).norm(), 1e-6)
      << (first.R_GtoI - scene.R_GtoI).norm();
  EXPECT_LT((first.p_IinG - scene.p_IinG).norm(), 1e-6)
      << (first.p_IinG - scene.p_IinG).norm();
  EXPECT_EQ(first.metrics.valid_correspondence_count, scene.correspondences.size());
  EXPECT_EQ(first.metrics.inlier_count, scene.correspondences.size());
  EXPECT_GE(first.metrics.inlier_ratio, LongGapRelocalizer::minimum_inlier_ratio());
  EXPECT_LE(first.metrics.max_reprojection_error_px,
            LongGapRelocalizer::maximum_reprojection_error_px());
  EXPECT_GT(first.metrics.min_depth, 0.0);
  EXPECT_GE(first.metrics.image_span_x_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
  EXPECT_GE(first.metrics.image_span_y_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
  EXPECT_LE(first.metrics.orientation_disagreement_deg,
            LongGapRelocalizer::maximum_orientation_disagreement_deg());

  const LongGapRelocalizationResult second =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  ASSERT_TRUE(second.accepted);
  EXPECT_TRUE(second.R_GtoI.isApprox(first.R_GtoI, 0.0));
  EXPECT_TRUE(second.p_IinG.isApprox(first.p_IinG, 0.0));
  EXPECT_EQ(second.inlier_ids, first.inlier_ids);
  EXPECT_DOUBLE_EQ(second.metrics.max_reprojection_error_px,
                   first.metrics.max_reprojection_error_px);
}

TEST(LongGapRelocalizer, NonfiniteCorrespondencesAreExcludedFromTheValidCount) {
  SyntheticScene scene = make_scene();
  LongGapCorrespondence invalid;
  invalid.id = 99999U;
  invalid.p_FinG.x() = std::numeric_limits<double>::quiet_NaN();
  scene.correspondences.push_back(invalid);

  const LongGapRelocalizationResult result =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  ASSERT_TRUE(result.accepted) << ov_msckf::long_gap_relocalization_reason_string(result.reason);
  EXPECT_EQ(result.metrics.supplied_correspondence_count, 21U);
  EXPECT_EQ(result.metrics.valid_correspondence_count, 20U);
}

TEST(LongGapRelocalizer, ReclassificationAndTwoFixedSetRefinementsRejectGrossOutliers) {
  std::vector<Eigen::Vector3d> camera_points;
  for (int row = 0; row < 5; ++row) {
    for (int column = 0; column < 6; ++column) {
      camera_points.emplace_back(-1.5 + 0.6 * static_cast<double>(column),
                                 -1.0 + 0.5 * static_cast<double>(row),
                                 4.4 + 0.2 * static_cast<double>((column + 2 * row) % 5));
    }
  }
  SyntheticScene scene = make_scene(camera_points);
  for (std::size_t index = 0; index < scene.correspondences.size(); ++index) {
    if (index < 24U) {
      scene.correspondences.at(index).uv_raw +=
          Eigen::Vector2d(0.35 * std::sin(static_cast<double>(index)),
                          0.30 * std::cos(0.7 * static_cast<double>(index)));
    } else {
      scene.correspondences.at(index).uv_raw +=
          Eigen::Vector2d(45.0 + static_cast<double>(index),
                          -38.0 - 0.5 * static_cast<double>(index));
    }
  }

  const LongGapRelocalizationResult result =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  ASSERT_TRUE(result.accepted) << ov_msckf::long_gap_relocalization_reason_string(result.reason);
  EXPECT_GE(result.metrics.inlier_count, 21U);
  EXPECT_LE(result.metrics.inlier_count, 24U);
  EXPECT_GE(result.metrics.inlier_ratio, LongGapRelocalizer::minimum_inlier_ratio());
  EXPECT_LE(result.metrics.max_reprojection_error_px,
            LongGapRelocalizer::maximum_reprojection_error_px());
  for (std::uint64_t id : result.inlier_ids) {
    EXPECT_LT(id, 1024U);
  }
}

TEST(LongGapRelocalizer, InvalidCalibrationAndOrientationPriorRejectWithoutSolving) {
  const SyntheticScene scene = make_scene();
  LongGapCameraCalibration invalid_calibration = scene.calibration;
  invalid_calibration.K(0, 0) = 0.0;
  expect_neutral_rejection(
      LongGapRelocalizer::recover(scene.correspondences, invalid_calibration, scene.R_GtoI),
      LongGapRelocalizationReason::INVALID_CALIBRATION);

  Eigen::Matrix3d invalid_prior = scene.R_GtoI;
  invalid_prior(0, 0) = std::numeric_limits<double>::quiet_NaN();
  expect_neutral_rejection(
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, invalid_prior),
      LongGapRelocalizationReason::INVALID_ORIENTATION_PRIOR);
}

TEST(LongGapRelocalizer, DuplicateIdsRejectAndLeaveEveryInputByteSemanticallyUnchanged) {
  SyntheticScene scene = make_scene();
  scene.correspondences.at(1).id = scene.correspondences.at(0).id;
  const std::vector<LongGapCorrespondence> before = scene.correspondences;
  const LongGapCameraCalibration calibration_before = scene.calibration;
  const Eigen::Matrix3d prior_before = scene.R_GtoI;

  const LongGapRelocalizationResult result =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  expect_neutral_rejection(result, LongGapRelocalizationReason::DUPLICATE_CORRESPONDENCE_ID);
  ASSERT_EQ(scene.correspondences.size(), before.size());
  for (std::size_t index = 0; index < before.size(); ++index) {
    EXPECT_EQ(scene.correspondences.at(index).id, before.at(index).id);
    EXPECT_TRUE(scene.correspondences.at(index).p_FinG.isApprox(before.at(index).p_FinG, 0.0));
    EXPECT_TRUE(scene.correspondences.at(index).uv_raw.isApprox(before.at(index).uv_raw, 0.0));
  }
  EXPECT_TRUE(scene.calibration.K.isApprox(calibration_before.K, 0.0));
  EXPECT_EQ(scene.calibration.distortion, calibration_before.distortion);
  EXPECT_TRUE(scene.calibration.R_ItoC.isApprox(calibration_before.R_ItoC, 0.0));
  EXPECT_TRUE(scene.calibration.p_IinC.isApprox(calibration_before.p_IinC, 0.0));
  EXPECT_TRUE(scene.R_GtoI.isApprox(prior_before, 0.0));
}

TEST(LongGapRelocalizer, TwelveValidCorrespondencesAreMandatory) {
  SyntheticScene scene = make_scene();
  scene.correspondences.resize(LongGapRelocalizer::minimum_correspondences() - 1U);
  const LongGapRelocalizationResult result =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  expect_neutral_rejection(result, LongGapRelocalizationReason::INSUFFICIENT_CORRESPONDENCES);
  EXPECT_EQ(result.metrics.valid_correspondence_count, 11U);
}

TEST(LongGapRelocalizer, GloballyCollinearSupportRejectsBeforeRansac) {
  std::vector<Eigen::Vector3d> line;
  for (int index = 0; index < 20; ++index) {
    const double alpha = -1.0 + 0.1 * static_cast<double>(index);
    line.emplace_back(alpha, 0.25 * alpha, 5.0 + 0.2 * alpha);
  }
  const SyntheticScene scene = make_scene(line);
  const LongGapRelocalizationResult result =
      LongGapRelocalizer::recover(scene.correspondences, scene.calibration, scene.R_GtoI);
  expect_neutral_rejection(result, LongGapRelocalizationReason::NONCOLLINEAR_SUPPORT_REQUIRED);
}

TEST(LongGapRelocalizer, TwelveRansacInliersAreMandatory) {
  const SyntheticScene scene = make_scene();
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(11U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::INSUFFICIENT_INLIERS);
  EXPECT_EQ(result.metrics.inlier_count, 11U);
}

TEST(LongGapRelocalizer, InlierRatioMustBeAtLeastPointSeven) {
  const SyntheticScene scene = make_scene();
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(13U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::INLIER_RATIO_TOO_LOW);
  EXPECT_DOUBLE_EQ(result.metrics.inlier_ratio, 13.0 / 20.0);
}

TEST(LongGapRelocalizer, NonfinitePoseRejectsBeforePublishingAPose) {
  const SyntheticScene scene = make_scene();
  Eigen::Matrix3d nonfinite_rotation = scene.R_GtoC;
  nonfinite_rotation(0, 0) = std::numeric_limits<double>::quiet_NaN();
  const LongGapRelocalizationResult result = LongGapRelocalizerTestAccess::evaluate(
      scene.correspondences, scene.calibration, scene.R_GtoI,
      nonfinite_rotation, scene.t_GinC, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::NONFINITE_POSE);
}

TEST(LongGapRelocalizer, EveryInlierMustHavePositiveDepth) {
  SyntheticScene scene = make_scene();
  const Eigen::Vector3d behind_camera(0.1, -0.2, -3.0);
  scene.correspondences.at(0).p_FinG =
      scene.R_GtoC.transpose() * (behind_camera - scene.calibration.p_IinC) + scene.p_IinG;
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::NONPOSITIVE_DEPTH);
  EXPECT_LT(result.metrics.min_depth, 0.0);
}

TEST(LongGapRelocalizer, NonfiniteProjectionArithmeticRejects) {
  SyntheticScene scene = make_scene();
  scene.correspondences.at(0).p_FinG =
      Eigen::Vector3d::Constant(std::numeric_limits<double>::max());
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::NONFINITE_RESIDUAL);
}

TEST(LongGapRelocalizer, EveryInlierMustRemainWithinTwoPixels) {
  SyntheticScene scene = make_scene();
  scene.correspondences.at(0).uv_raw.x() += 2.01;
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::REPROJECTION_ERROR_TOO_HIGH);
  EXPECT_GT(result.metrics.max_reprojection_error_px,
            LongGapRelocalizer::maximum_reprojection_error_px());
}

TEST(LongGapRelocalizer, RansacInliersThemselvesMustBeNoncollinear) {
  std::vector<Eigen::Vector3d> points;
  for (int index = 0; index < 14; ++index) {
    const double alpha = -1.3 + 0.2 * static_cast<double>(index);
    points.emplace_back(alpha, 0.35 * alpha, 5.0 + 0.1 * alpha);
  }
  const std::vector<Eigen::Vector3d> broad = broad_camera_points();
  points.insert(points.end(), broad.begin(), broad.begin() + 6);
  const SyntheticScene scene = make_scene(points);
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(14U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::NONCOLLINEAR_SUPPORT_REQUIRED);
  EXPECT_DOUBLE_EQ(result.metrics.inlier_ratio, LongGapRelocalizer::minimum_inlier_ratio());
}

TEST(LongGapRelocalizer, InlierImageBoundsMustSpanOneQuarterOfWidth) {
  std::vector<Eigen::Vector3d> narrow_x;
  for (int row = 0; row < 4; ++row) {
    for (int column = 0; column < 5; ++column) {
      narrow_x.emplace_back(-0.12 + 0.06 * static_cast<double>(column),
                            -1.2 + 0.8 * static_cast<double>(row),
                            4.5 + 0.25 * static_cast<double>((column + row) % 4));
    }
  }
  const SyntheticScene scene = make_scene(narrow_x);
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::IMAGE_BOUNDS_X_TOO_SMALL);
  EXPECT_LT(result.metrics.image_span_x_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
  EXPECT_GE(result.metrics.image_span_y_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
}

TEST(LongGapRelocalizer, InlierImageBoundsMustSpanOneQuarterOfHeight) {
  std::vector<Eigen::Vector3d> narrow_y;
  for (int row = 0; row < 4; ++row) {
    for (int column = 0; column < 5; ++column) {
      narrow_y.emplace_back(-1.6 + 0.8 * static_cast<double>(column),
                            -0.09 + 0.06 * static_cast<double>(row),
                            4.5 + 0.25 * static_cast<double>((2 * column + row) % 4));
    }
  }
  const SyntheticScene scene = make_scene(narrow_y);
  const LongGapRelocalizationResult result = evaluate_exact(scene, first_indices(20U));
  expect_neutral_rejection(result, LongGapRelocalizationReason::IMAGE_BOUNDS_Y_TOO_SMALL);
  EXPECT_GE(result.metrics.image_span_x_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
  EXPECT_LT(result.metrics.image_span_y_fraction,
            LongGapRelocalizer::minimum_image_span_fraction());
}

TEST(LongGapRelocalizer, PnpAndImuOrientationMustAgreeWithinFiveDegrees) {
  const SyntheticScene scene = make_scene();
  const Eigen::Matrix3d disagreeing_prior =
      Eigen::AngleAxisd(5.1 * kTestPi / 180.0, Eigen::Vector3d::UnitZ()).toRotationMatrix() *
      scene.R_GtoI;
  const LongGapRelocalizationResult result = LongGapRelocalizerTestAccess::evaluate(
      scene.correspondences, scene.calibration, disagreeing_prior,
      scene.R_GtoC, scene.t_GinC, first_indices(20U));
  expect_neutral_rejection(result,
                           LongGapRelocalizationReason::ORIENTATION_DISAGREEMENT_TOO_LARGE);
  EXPECT_GT(result.metrics.orientation_disagreement_deg,
            LongGapRelocalizer::maximum_orientation_disagreement_deg());
}

TEST(LongGapRelocalizer, MalformedInlierIndicesFailClosed) {
  const SyntheticScene scene = make_scene();
  std::vector<int> malformed = first_indices(20U);
  malformed.back() = 100;
  const LongGapRelocalizationResult result = LongGapRelocalizerTestAccess::evaluate(
      scene.correspondences, scene.calibration, scene.R_GtoI,
      scene.R_GtoC, scene.t_GinC, malformed);
  expect_neutral_rejection(result, LongGapRelocalizationReason::PNP_FAILED);
}

} // namespace
