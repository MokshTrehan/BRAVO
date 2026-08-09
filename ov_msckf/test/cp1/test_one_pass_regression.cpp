/*
 * SchurVIO-Lite pre-refactor production one-pass regression.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cp1_fixture_utils.h"

#include <gtest/gtest.h>

#include "cam/CamRadtan.h"
#include "feat/Feature.h"
#include "feat/FeatureInitializer.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/LandmarkRepresentation.h"
#include "types/Type.h"
#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKF.h"
#include "update/UpdaterMSCKFPreview.h"
#include "utils/quat_ops.h"

#include <Eigen/Cholesky>
#include <Eigen/Eigenvalues>

#include <boost/math/distributions/chi_squared.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <functional>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <unordered_map>
#include <vector>

namespace {

using ov_core::Feature;
using ov_core::FeatureInitializer;
using ov_core::FeatureInitializerOptions;
using ov_msckf::MSCKFUpdatePreviewBlock;
using ov_msckf::MSCKFUpdatePreviewSnapshot;
using ov_msckf::MSCKFUpdatePriorMatchStatus;
using ov_msckf::MSCKFUpdatePriorSnapshot;
using ov_msckf::SchurReductionResult;
using ov_msckf::State;
using ov_msckf::StateHelper;
using ov_msckf::StateOptions;
using ov_msckf::UpdaterHelper;
using ov_msckf::UpdaterMSCKF;
using ov_msckf::UpdaterMSCKFPreview;
using ov_msckf::UpdaterOptions;
using ov_type::Type;

constexpr std::size_t kAcceptedFeatureId = 4242U;
constexpr std::size_t kRejectedFeatureId = 9001U;
// Captured from the ordinary direct-commit updater at db68dc93b8601a0499511a2df80ed55793722646.
constexpr double kExpectedNis = 5.1281265790197955e-05;
constexpr double kExpectedCovarianceTrace = 0.0096956180494561176;
constexpr double kExpectedCovarianceSquaredNorm = 2.8048678394663941e-06;
const std::array<double, 39> kExpectedIncrement = {{
    2.9912807272735746e-07,  -1.0371684947920263e-07,
    -8.1457290832284828e-07, -3.4679310627897098e-07,
    4.3081057876527151e-07,  9.1541823272956051e-07,
    1.9279392658850181e-07,  -2.0929341562963809e-07,
    -7.2790472413021801e-07, 5.8777566966651178e-08,
    -1.24668927000896e-06,   -2.4238298242710958e-07,
    1.7412551025444053e-08,  3.3135882063786084e-07,
    1.3951016167088291e-06,  -4.2025537518468312e-05,
    -3.1292517636092435e-05, 1.0924911304121746e-05,
    -6.6578838907379927e-06, 7.7114042977100629e-06,
    1.5036079680264319e-06,  4.3193335392424579e-05,
    7.4056582075072587e-05,  -1.0143845678351166e-05,
    1.394582923891769e-05,   -8.9031763132728561e-06,
    -2.6208817087993241e-06, 2.8418380299940439e-05,
    -5.0086449845507966e-05, -4.16039690818961e-07,
    -9.7761402449872382e-06, -5.4543289764097874e-06,
    2.0764047242883011e-06,  -2.8104620775130116e-05,
    8.1273006007104664e-06,  8.7109030947243962e-07,
    2.1631117283029831e-06,  5.4713850238138913e-06,
    -9.7356198631762872e-07}};

class InspectingCamRadtan : public ov_core::CamRadtan {
public:
  InspectingCamRadtan(int width, int height)
      : ov_core::CamRadtan(width, height) {}

  Eigen::Vector2f distort_f(const Eigen::Vector2f &uv_norm) override {
    if (inspection) {
      inspection();
    }
    return ov_core::CamRadtan::distort_f(uv_norm);
  }

  void compute_distort_jacobian(const Eigen::Vector2d &uv_norm,
                                Eigen::MatrixXd &H_dz_dzn,
                                Eigen::MatrixXd &H_dz_dzeta) override {
    if (inspection) {
      inspection();
    }
    ov_core::CamRadtan::compute_distort_jacobian(
        uv_norm, H_dz_dzn, H_dz_dzeta);
  }

  std::function<void()> inspection;
};

Eigen::Matrix<double, 7, 1> pose_value(const Eigen::Matrix3d &rotation,
                                       const Eigen::Vector3d &position) {
  Eigen::Matrix<double, 7, 1> value;
  value.head<4>() = ov_core::rot_2_quat(rotation);
  value.tail<3>() = position;
  return value;
}

void expect_same_bytes(const Eigen::MatrixXd &actual,
                       const Eigen::MatrixXd &expected) {
  ASSERT_EQ(actual.rows(), expected.rows());
  ASSERT_EQ(actual.cols(), expected.cols());
  EXPECT_EQ(std::memcmp(actual.data(), expected.data(),
                        static_cast<std::size_t>(actual.size()) * sizeof(double)),
            0);
}

void expect_same_bytes(const Eigen::VectorXd &actual,
                       const Eigen::VectorXd &expected) {
  ASSERT_EQ(actual.rows(), expected.rows());
  EXPECT_EQ(std::memcmp(actual.data(), expected.data(),
                        static_cast<std::size_t>(actual.size()) * sizeof(double)),
            0);
}

void expect_same_layout(const std::vector<MSCKFUpdatePreviewBlock> &actual,
                        const std::vector<MSCKFUpdatePreviewBlock> &expected) {
  ASSERT_EQ(actual.size(), expected.size());
  for (std::size_t index = 0; index < actual.size(); ++index) {
    EXPECT_EQ(actual[index].covariance_id, expected[index].covariance_id);
    EXPECT_EQ(actual[index].size, expected[index].size);
    EXPECT_EQ(actual[index].offset, expected[index].offset);
  }
}

void expect_same_feature_observations(const Feature &actual,
                                      const Feature &expected) {
  EXPECT_EQ(actual.featid, expected.featid);
  EXPECT_EQ(actual.to_delete, expected.to_delete);
  EXPECT_EQ(actual.anchor_cam_id, expected.anchor_cam_id);
  EXPECT_EQ(std::memcmp(&actual.anchor_clone_timestamp,
                        &expected.anchor_clone_timestamp, sizeof(double)),
            0);
  EXPECT_EQ(std::memcmp(actual.p_FinA.data(), expected.p_FinA.data(),
                        3U * sizeof(double)),
            0);
  EXPECT_EQ(std::memcmp(actual.p_FinG.data(), expected.p_FinG.data(),
                        3U * sizeof(double)),
            0);
  ASSERT_EQ(actual.timestamps.size(), expected.timestamps.size());
  ASSERT_EQ(actual.uvs.size(), expected.uvs.size());
  ASSERT_EQ(actual.uvs_norm.size(), expected.uvs_norm.size());
  for (const auto &camera : expected.timestamps) {
    const auto actual_times = actual.timestamps.find(camera.first);
    const auto actual_uvs = actual.uvs.find(camera.first);
    const auto actual_norm = actual.uvs_norm.find(camera.first);
    ASSERT_NE(actual_times, actual.timestamps.end());
    ASSERT_NE(actual_uvs, actual.uvs.end());
    ASSERT_NE(actual_norm, actual.uvs_norm.end());
    ASSERT_EQ(actual_times->second.size(), camera.second.size());
    ASSERT_EQ(actual_uvs->second.size(), expected.uvs.at(camera.first).size());
    ASSERT_EQ(actual_norm->second.size(),
              expected.uvs_norm.at(camera.first).size());
    for (std::size_t index = 0; index < camera.second.size(); ++index) {
      EXPECT_EQ(std::memcmp(&actual_times->second[index], &camera.second[index],
                            sizeof(double)),
                0);
      const auto &actual_uv = actual_uvs->second[index];
      const auto &expected_uv = expected.uvs.at(camera.first)[index];
      ASSERT_EQ(actual_uv.size(), expected_uv.size());
      EXPECT_EQ(std::memcmp(actual_uv.data(), expected_uv.data(),
                            static_cast<std::size_t>(actual_uv.size()) *
                                sizeof(float)),
                0);
      const auto &actual_normalized = actual_norm->second[index];
      const auto &expected_normalized =
          expected.uvs_norm.at(camera.first)[index];
      ASSERT_EQ(actual_normalized.size(), expected_normalized.size());
      EXPECT_EQ(std::memcmp(actual_normalized.data(),
                            expected_normalized.data(),
                            static_cast<std::size_t>(actual_normalized.size()) *
                                sizeof(float)),
                0);
    }
  }
}

struct Fixture {
  std::shared_ptr<State> state;
  std::vector<std::shared_ptr<Type>> state_order;
  std::shared_ptr<Feature> accepted_feature;
  std::shared_ptr<Feature> rejected_feature;
  std::shared_ptr<InspectingCamRadtan> camera;
  UpdaterOptions updater_options;
  FeatureInitializerOptions initializer_options;
};

Fixture make_fixture() {
  Fixture fixture;

  StateOptions state_options;
  state_options.do_fej = true;
  state_options.do_calib_camera_pose = false;
  state_options.do_calib_camera_intrinsics = false;
  state_options.do_calib_camera_timeoffset = false;
  state_options.do_calib_imu_intrinsics = false;
  state_options.num_cameras = 1;
  state_options.feat_rep_msckf =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  fixture.state = std::make_shared<State>(state_options);

  Eigen::Matrix<double, 8, 1> intrinsics;
  intrinsics << 420.0, 415.0, 320.0, 240.0, 0.0, 0.0, 0.0, 0.0;
  fixture.state->_cam_intrinsics.at(0)->set_value(intrinsics);
  fixture.state->_cam_intrinsics.at(0)->set_fej(intrinsics);
  fixture.camera = std::make_shared<InspectingCamRadtan>(640, 480);
  fixture.camera->set_value(intrinsics);
  fixture.state->_cam_intrinsics_cameras[0] = fixture.camera;

  const Eigen::Matrix<double, 7, 1> identity_calibration =
      pose_value(Eigen::Matrix3d::Identity(), Eigen::Vector3d::Zero());
  fixture.state->_calib_IMUtoCAM.at(0)->set_value(identity_calibration);
  fixture.state->_calib_IMUtoCAM.at(0)->set_fej(identity_calibration);

  const std::array<double, 4> timestamps = {{1.0, 2.0, 3.0, 4.0}};
  Eigen::Matrix<double, 7, 1> final_imu_value;
  Eigen::Matrix<double, 7, 1> final_imu_fej;
  for (std::size_t index = 0; index < timestamps.size(); ++index) {
    const double scale = static_cast<double>(index);
    const Eigen::Matrix3d current_rotation =
        ov_core::exp_so3(Eigen::Vector3d(0.002 * scale, -0.001 * scale,
                                        0.003 * scale));
    const Eigen::Vector3d current_position(0.22 * scale,
                                           0.018 * static_cast<double>(index % 2),
                                           0.012 * scale);
    const Eigen::Matrix3d fej_rotation =
        ov_core::exp_so3(Eigen::Vector3d(0.0015 * scale,
                                        -0.0007 * scale,
                                        0.0024 * scale));
    const Eigen::Vector3d fej_position =
        current_position +
        Eigen::Vector3d(0.001 * (scale + 1.0), -0.0005 * scale,
                        0.0003 * (scale + 1.0));

    final_imu_value = pose_value(current_rotation, current_position);
    final_imu_fej = pose_value(fej_rotation, fej_position);
    fixture.state->_imu->pose()->set_value(final_imu_value);
    fixture.state->_imu->pose()->set_fej(final_imu_fej);
    fixture.state->_timestamp = timestamps[index];
    StateHelper::augment_clone(fixture.state, Eigen::Vector3d::Zero());
  }
  Eigen::Matrix<double, 16, 1> imu_value =
      Eigen::Matrix<double, 16, 1>::Zero();
  Eigen::Matrix<double, 16, 1> imu_fej =
      Eigen::Matrix<double, 16, 1>::Zero();
  imu_value.head<7>() = final_imu_value;
  imu_fej.head<7>() = final_imu_fej;
  fixture.state->_imu->set_value(imu_value);
  fixture.state->_imu->set_fej(imu_fej);

  fixture.state_order.push_back(fixture.state->_imu);
  for (const auto &clone : fixture.state->_clones_IMU) {
    fixture.state_order.push_back(clone.second);
  }
  EXPECT_EQ(fixture.state_order.size(), 5U);

  Eigen::Index state_dimension = 0;
  for (const auto &variable : fixture.state_order) {
    EXPECT_EQ(variable->id(), state_dimension);
    state_dimension += variable->size();
  }
  EXPECT_EQ(state_dimension, 39);

  Eigen::MatrixXd prior_factor = Eigen::MatrixXd::Zero(state_dimension,
                                                        state_dimension);
  for (Eigen::Index row = 0; row < state_dimension; ++row) {
    prior_factor(row, row) = 0.015 + 0.0001 * static_cast<double>(row);
    for (Eigen::Index column = 0; column < row; ++column) {
      const int pattern = static_cast<int>(((row + 3) * (column + 5)) % 17) - 8;
      prior_factor(row, column) = 2.0e-5 * static_cast<double>(pattern);
    }
  }
  const Eigen::MatrixXd prior = prior_factor * prior_factor.transpose();
  StateHelper::set_initial_covariance(fixture.state, prior,
                                      fixture.state_order);

  fixture.accepted_feature = std::make_shared<Feature>();
  fixture.accepted_feature->featid = kAcceptedFeatureId;
  fixture.accepted_feature->to_delete = false;
  fixture.accepted_feature->anchor_clone_timestamp = -1.0;
  fixture.accepted_feature->p_FinA.setZero();
  fixture.accepted_feature->p_FinG.setZero();
  const Eigen::Vector3d feature_global(1.15, 0.24, 5.2);
  const std::array<Eigen::Vector2d, 4> pixel_offsets = {{
      Eigen::Vector2d(0.035, -0.020), Eigen::Vector2d(-0.025, 0.018),
      Eigen::Vector2d(0.020, 0.012), Eigen::Vector2d(-0.018, -0.014)}};
  for (std::size_t index = 0; index < timestamps.size(); ++index) {
    const auto &clone = fixture.state->_clones_IMU.at(timestamps[index]);
    const Eigen::Vector3d point_camera =
        clone->Rot() * (feature_global - clone->pos());
    const Eigen::Vector2d normalized(point_camera(0) / point_camera(2),
                                     point_camera(1) / point_camera(2));
    const Eigen::Vector2d measured = fixture.camera->distort_d(normalized) +
                                     pixel_offsets[index];
    Eigen::VectorXf measured_float(2);
    measured_float = measured.cast<float>();
    Eigen::VectorXf normalized_float(2);
    normalized_float <<
        static_cast<float>((measured_float(0) - intrinsics(2)) / intrinsics(0)),
        static_cast<float>((measured_float(1) - intrinsics(3)) / intrinsics(1));
    fixture.accepted_feature->timestamps[0].push_back(timestamps[index]);
    fixture.accepted_feature->uvs[0].push_back(measured_float);
    fixture.accepted_feature->uvs_norm[0].push_back(normalized_float);
  }

  fixture.rejected_feature = std::make_shared<Feature>();
  fixture.rejected_feature->featid = kRejectedFeatureId;
  fixture.rejected_feature->to_delete = false;
  fixture.rejected_feature->anchor_clone_timestamp = -1.0;
  fixture.rejected_feature->p_FinA.setZero();
  fixture.rejected_feature->p_FinG.setZero();
  for (int index = 0; index < 2; ++index) {
    Eigen::VectorXf measured(2);
    measured << 300.0f + static_cast<float>(index),
        220.0f - static_cast<float>(index);
    Eigen::VectorXf normalized(2);
    normalized << (measured(0) - 320.0f) / 420.0f,
        (measured(1) - 240.0f) / 415.0f;
    fixture.rejected_feature->timestamps[0].push_back(90.0 + index);
    fixture.rejected_feature->uvs[0].push_back(measured);
    fixture.rejected_feature->uvs_norm[0].push_back(normalized);
  }

  fixture.updater_options.chi2_multipler = 1.0;
  fixture.updater_options.sigma_pix = 1.0;
  fixture.updater_options.sigma_pix_sq = 1.0;
  fixture.updater_options.landmark_elimination =
      UpdaterOptions::LandmarkElimination::SCHUR;
  fixture.initializer_options.refine_features = true;
  return fixture;
}

std::unordered_map<size_t,
                   std::unordered_map<double, FeatureInitializer::ClonePose>>
camera_clone_map(const std::shared_ptr<State> &state) {
  std::unordered_map<size_t,
                     std::unordered_map<double, FeatureInitializer::ClonePose>>
      clones_camera;
  for (const auto &calibration : state->_calib_IMUtoCAM) {
    std::unordered_map<double, FeatureInitializer::ClonePose> camera_poses;
    for (const auto &clone : state->_clones_IMU) {
      const Eigen::Matrix3d rotation =
          calibration.second->Rot() * clone.second->Rot();
      const Eigen::Vector3d position =
          clone.second->pos() -
          rotation.transpose() * calibration.second->pos();
      camera_poses.insert(
          {clone.first, FeatureInitializer::ClonePose(rotation, position)});
    }
    clones_camera.insert({calibration.first, camera_poses});
  }
  return clones_camera;
}

Eigen::Vector2d mixed_fej_runtime_radtan(
    const Eigen::Vector2d &uv_norm,
    const Eigen::Matrix<double, 8, 1> &intrinsics) {
  const float x_float = static_cast<float>(uv_norm(0));
  const float y_float = static_cast<float>(uv_norm(1));
  const double radius = std::sqrt(x_float * x_float + y_float * y_float);
  const double radius_squared = radius * radius;
  const double radius_fourth = radius_squared * radius_squared;
  const double x_distorted =
      x_float * (1.0 + intrinsics(4) * radius_squared +
                 intrinsics(5) * radius_fourth) +
      2.0 * intrinsics(6) * x_float * y_float +
      intrinsics(7) *
          (radius_squared + 2 * x_float * x_float);
  const double y_distorted =
      y_float * (1.0 + intrinsics(4) * radius_squared +
                 intrinsics(5) * radius_fourth) +
      intrinsics(6) *
          (radius_squared + 2 * y_float * y_float) +
      2.0 * intrinsics(7) * x_float * y_float;
  Eigen::Vector2f pixel;
  pixel(0) = static_cast<float>(intrinsics(0) * x_distorted + intrinsics(2));
  pixel(1) = static_cast<float>(intrinsics(1) * y_distorted + intrinsics(3));
  return pixel.cast<double>();
}

Eigen::Matrix2d mixed_fej_radtan_jacobian(
    const Eigen::Vector2d &uv_norm,
    const Eigen::Matrix<double, 8, 1> &intrinsics) {
  const double x = uv_norm(0);
  const double y = uv_norm(1);
  const double radius = std::sqrt(x * x + y * y);
  const double radius_squared = radius * radius;
  const double radius_fourth = radius_squared * radius_squared;
  const double x_squared = x * x;
  const double y_squared = y * y;
  const double xy = x * y;
  Eigen::Matrix2d jacobian;
  jacobian(0, 0) =
      intrinsics(0) *
      ((1.0 + intrinsics(4) * radius_squared +
        intrinsics(5) * radius_fourth) +
       (2.0 * intrinsics(4) * x_squared +
        4.0 * intrinsics(5) * x_squared * radius_squared) +
       2.0 * intrinsics(6) * y + 6.0 * intrinsics(7) * x);
  jacobian(0, 1) =
      intrinsics(0) *
      (2.0 * intrinsics(4) * xy +
       4.0 * intrinsics(5) * xy * radius_squared +
       2.0 * intrinsics(6) * x + 2.0 * intrinsics(7) * y);
  jacobian(1, 0) =
      intrinsics(1) *
      (2.0 * intrinsics(4) * xy +
       4.0 * intrinsics(5) * xy * radius_squared +
       2.0 * intrinsics(6) * x + 2.0 * intrinsics(7) * y);
  jacobian(1, 1) =
      intrinsics(1) *
      ((1.0 + intrinsics(4) * radius_squared +
        intrinsics(5) * radius_fourth) +
       (2.0 * intrinsics(4) * y_squared +
        4.0 * intrinsics(5) * y_squared * radius_squared) +
       2.0 * intrinsics(7) * x + 6.0 * intrinsics(6) * y);
  return jacobian;
}

Eigen::Matrix<double, 2, 8> mixed_fej_intrinsics_jacobian(
    const Eigen::Vector2d &uv_norm,
    const Eigen::Matrix<double, 8, 1> &intrinsics) {
  const double x = uv_norm(0);
  const double y = uv_norm(1);
  const double radius_squared = x * x + y * y;
  const double radius_fourth = radius_squared * radius_squared;
  const double x_distorted =
      x * (1.0 + intrinsics(4) * radius_squared +
           intrinsics(5) * radius_fourth) +
      2.0 * intrinsics(6) * x * y +
      intrinsics(7) * (radius_squared + 2.0 * x * x);
  const double y_distorted =
      y * (1.0 + intrinsics(4) * radius_squared +
           intrinsics(5) * radius_fourth) +
      intrinsics(6) * (radius_squared + 2.0 * y * y) +
      2.0 * intrinsics(7) * x * y;
  Eigen::Matrix<double, 2, 8> jacobian =
      Eigen::Matrix<double, 2, 8>::Zero();
  jacobian(0, 0) = x_distorted;
  jacobian(0, 2) = 1.0;
  jacobian(0, 4) = intrinsics(0) * x * radius_squared;
  jacobian(0, 5) = intrinsics(0) * x * radius_fourth;
  jacobian(0, 6) = 2.0 * intrinsics(0) * x * y;
  jacobian(0, 7) = intrinsics(0) * (radius_squared + 2.0 * x * x);
  jacobian(1, 1) = y_distorted;
  jacobian(1, 3) = 1.0;
  jacobian(1, 4) = intrinsics(1) * y * radius_squared;
  jacobian(1, 5) = intrinsics(1) * y * radius_fourth;
  jacobian(1, 6) = intrinsics(1) * (radius_squared + 2.0 * y * y);
  jacobian(1, 7) = 2.0 * intrinsics(1) * x * y;
  return jacobian;
}

Fixture make_mixed_fej_fixture() {
  Fixture fixture;
  StateOptions state_options;
  state_options.do_fej = true;
  state_options.do_calib_camera_pose = false;
  state_options.do_calib_camera_intrinsics = true;
  state_options.do_calib_camera_timeoffset = false;
  state_options.do_calib_imu_intrinsics = false;
  state_options.num_cameras = 1;
  state_options.feat_rep_msckf =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  fixture.state = std::make_shared<State>(state_options);

  Eigen::Matrix<double, 8, 1> intrinsics;
  intrinsics << 430.0, 425.0, 321.0, 239.0, -0.12, 0.035, 0.0015,
      -0.0008;
  Eigen::Matrix<double, 8, 1> intrinsics_fej = intrinsics;
  intrinsics_fej(0) -= 2.0;
  intrinsics_fej(4) += 0.015;
  intrinsics_fej(6) -= 0.0004;
  fixture.state->_cam_intrinsics.at(0)->set_value(intrinsics);
  fixture.state->_cam_intrinsics.at(0)->set_fej(intrinsics_fej);
  fixture.camera = std::make_shared<InspectingCamRadtan>(752, 480);
  fixture.camera->set_value(intrinsics);
  fixture.state->_cam_intrinsics_cameras[0] = fixture.camera;

  const Eigen::Matrix<double, 7, 1> calibration =
      pose_value(ov_core::exp_so3(Eigen::Vector3d(0.03, -0.02, 0.01)),
                 Eigen::Vector3d(0.04, -0.015, 0.02));
  const Eigen::Matrix<double, 7, 1> calibration_fej =
      pose_value(ov_core::exp_so3(Eigen::Vector3d(-0.01, 0.015, -0.005)),
                 Eigen::Vector3d(-0.01, 0.005, -0.004));
  fixture.state->_calib_IMUtoCAM.at(0)->set_value(calibration);
  fixture.state->_calib_IMUtoCAM.at(0)->set_fej(calibration_fej);

  const std::array<double, 4> timestamps = {{1.0, 2.0, 3.0, 4.0}};
  Eigen::Matrix<double, 7, 1> final_imu_value;
  Eigen::Matrix<double, 7, 1> final_imu_fej;
  for (std::size_t index = 0; index < timestamps.size(); ++index) {
    const double scale = static_cast<double>(index);
    const Eigen::Matrix3d current_rotation = ov_core::exp_so3(
        Eigen::Vector3d(0.006 * scale, -0.004 * scale, 0.008 * scale));
    const Eigen::Vector3d current_position(
        0.28 * scale, 0.025 * static_cast<double>(index % 2),
        0.015 * scale);
    const Eigen::Matrix3d fej_rotation = ov_core::exp_so3(
        Eigen::Vector3d(0.0035 * scale, -0.002 * scale,
                        0.005 * scale));
    const Eigen::Vector3d fej_position =
        current_position +
        Eigen::Vector3d(0.004 * (scale + 1.0), -0.002 * scale,
                        0.001 * (scale + 1.0));
    final_imu_value = pose_value(current_rotation, current_position);
    final_imu_fej = pose_value(fej_rotation, fej_position);
    fixture.state->_imu->pose()->set_value(final_imu_value);
    fixture.state->_imu->pose()->set_fej(final_imu_fej);
    fixture.state->_timestamp = timestamps[index];
    StateHelper::augment_clone(fixture.state, Eigen::Vector3d::Zero());
  }
  Eigen::Matrix<double, 16, 1> imu_value =
      Eigen::Matrix<double, 16, 1>::Zero();
  Eigen::Matrix<double, 16, 1> imu_fej =
      Eigen::Matrix<double, 16, 1>::Zero();
  imu_value.head<7>() = final_imu_value;
  imu_fej.head<7>() = final_imu_fej;
  fixture.state->_imu->set_value(imu_value);
  fixture.state->_imu->set_fej(imu_fej);

  fixture.state_order.push_back(fixture.state->_imu);
  fixture.state_order.push_back(fixture.state->_cam_intrinsics.at(0));
  for (const auto &clone : fixture.state->_clones_IMU) {
    fixture.state_order.push_back(clone.second);
  }
  Eigen::Index state_dimension = 0;
  for (const auto &variable : fixture.state_order) {
    EXPECT_EQ(variable->id(), state_dimension);
    state_dimension += variable->size();
  }
  EXPECT_EQ(state_dimension, 47);

  Eigen::MatrixXd prior_factor =
      Eigen::MatrixXd::Zero(state_dimension, state_dimension);
  for (Eigen::Index row = 0; row < state_dimension; ++row) {
    prior_factor(row, row) = 0.022 + 0.00012 * static_cast<double>(row);
    for (Eigen::Index column = 0; column < row; ++column) {
      const int pattern =
          static_cast<int>(((row + 7) * (column + 11)) % 19) - 9;
      prior_factor(row, column) = 1.5e-5 * static_cast<double>(pattern);
    }
  }
  StateHelper::set_initial_covariance(
      fixture.state, prior_factor * prior_factor.transpose(),
      fixture.state_order);

  fixture.accepted_feature = std::make_shared<Feature>();
  fixture.accepted_feature->featid = 5252U;
  fixture.accepted_feature->to_delete = false;
  fixture.accepted_feature->anchor_clone_timestamp = -1.0;
  fixture.accepted_feature->p_FinA.setZero();
  fixture.accepted_feature->p_FinG.setZero();
  const Eigen::Vector3d feature_global(1.30, 0.31, 5.6);
  const std::array<Eigen::Vector2d, 4> pixel_offsets = {{
      Eigen::Vector2d(0.18, -0.11), Eigen::Vector2d(-0.14, 0.09),
      Eigen::Vector2d(0.12, 0.07), Eigen::Vector2d(-0.10, -0.08)}};
  const Eigen::Matrix3d rotation_imu_to_camera =
      fixture.state->_calib_IMUtoCAM.at(0)->Rot();
  const Eigen::Vector3d position_imu_in_camera =
      fixture.state->_calib_IMUtoCAM.at(0)->pos();
  for (std::size_t index = 0; index < timestamps.size(); ++index) {
    const auto &clone = fixture.state->_clones_IMU.at(timestamps[index]);
    const Eigen::Vector3d point_camera =
        rotation_imu_to_camera *
            (clone->Rot() * (feature_global - clone->pos())) +
        position_imu_in_camera;
    const Eigen::Vector2d normalized(point_camera(0) / point_camera(2),
                                     point_camera(1) / point_camera(2));
    Eigen::VectorXf measured(2);
    measured = (mixed_fej_runtime_radtan(normalized, intrinsics) +
                pixel_offsets[index])
                   .cast<float>();
    Eigen::VectorXf normalized_float(2);
    normalized_float = normalized.cast<float>();
    fixture.accepted_feature->timestamps[0].push_back(timestamps[index]);
    fixture.accepted_feature->uvs[0].push_back(measured);
    fixture.accepted_feature->uvs_norm[0].push_back(normalized_float);
  }
  fixture.rejected_feature = std::make_shared<Feature>();
  fixture.rejected_feature->featid = 5253U;

  fixture.updater_options.chi2_multipler = 1.0;
  fixture.updater_options.sigma_pix = 1.0;
  fixture.updater_options.sigma_pix_sq = 1.0;
  fixture.updater_options.landmark_elimination =
      UpdaterOptions::LandmarkElimination::SCHUR;
  fixture.initializer_options.refine_features = true;
  return fixture;
}

Eigen::VectorXd mixed_fej_linearization_delta(const Fixture &fixture) {
  const Eigen::Index dimension =
      StateHelper::get_full_covariance(fixture.state).rows();
  Eigen::VectorXd delta = Eigen::VectorXd::Zero(dimension);
  const auto &intrinsics = fixture.state->_cam_intrinsics.at(0);
  delta.segment(intrinsics->id(), 8) << 0.18, -0.14, 0.04, -0.03,
      2.0e-4, -1.2e-4, 4.0e-5, -3.0e-5;
  std::size_t index = 0;
  for (const auto &clone : fixture.state->_clones_IMU) {
    const double scale = static_cast<double>(index + 1U);
    delta.segment<3>(clone.second->id()) =
        Eigen::Vector3d(3.0e-4 * scale, -2.0e-4 * scale,
                        1.5e-4 *
                            (1.0 + static_cast<double>(index % 2U)));
    delta.segment<3>(clone.second->id() + 3) =
        Eigen::Vector3d(1.0e-4 * scale, -7.5e-5 * scale,
                        4.0e-5 * scale);
    ++index;
  }
  return delta;
}

void materialize_absolute_delta(Fixture &fixture,
                                const Eigen::VectorXd &delta) {
  for (const auto &variable : fixture.state_order) {
    variable->update(delta.segment(variable->id(), variable->size()));
  }
  fixture.camera->set_value(fixture.state->_cam_intrinsics.at(0)->value());
}

struct MixedFejRawOracle {
  Eigen::MatrixXd H_f;
  Eigen::MatrixXd H_x;
  Eigen::VectorXd residual;
  Eigen::MatrixXd all_current_H_x;
  Eigen::MatrixXd fej_uv_H_x;
};

MixedFejRawOracle build_mixed_fej_raw_oracle(
    const Fixture &fixture,
    const UpdaterHelper::UpdaterHelperFeature &feature) {
  const std::size_t measurement_count = feature.timestamps.at(0).size();
  MixedFejRawOracle result;
  result.H_f = Eigen::MatrixXd::Zero(2 * measurement_count, 3);
  result.H_x = Eigen::MatrixXd::Zero(2 * measurement_count, 32);
  result.residual = Eigen::VectorXd::Zero(2 * measurement_count);
  result.all_current_H_x = Eigen::MatrixXd::Zero(2 * measurement_count, 32);
  result.fej_uv_H_x = Eigen::MatrixXd::Zero(2 * measurement_count, 32);

  const Eigen::Matrix<double, 8, 1> intrinsics =
      fixture.state->_cam_intrinsics.at(0)->value();
  const Eigen::Matrix3d rotation_imu_to_camera =
      fixture.state->_calib_IMUtoCAM.at(0)->Rot();
  const Eigen::Vector3d position_imu_in_camera =
      fixture.state->_calib_IMUtoCAM.at(0)->pos();
  for (std::size_t index = 0; index < measurement_count; ++index) {
    const auto &clone = fixture.state->_clones_IMU.at(
        feature.timestamps.at(0).at(index));
    const Eigen::Vector3d point_imu_current =
        clone->Rot() * (feature.p_FinG - clone->pos());
    const Eigen::Vector3d point_camera_current =
        rotation_imu_to_camera * point_imu_current + position_imu_in_camera;
    const Eigen::Vector2d uv_current(
        point_camera_current(0) / point_camera_current(2),
        point_camera_current(1) / point_camera_current(2));
    result.residual.segment<2>(2 * index) =
        feature.uvs.at(0).at(index).cast<double>() -
        mixed_fej_runtime_radtan(uv_current, intrinsics);

    const Eigen::Vector3d point_imu_fej =
        clone->Rot_fej() * (feature.p_FinG_fej - clone->pos_fej());
    const Eigen::Vector3d point_camera_fej =
        rotation_imu_to_camera * point_imu_fej + position_imu_in_camera;
    Eigen::Matrix<double, 2, 3> projection_fej;
    projection_fej <<
        1.0 / point_camera_fej(2), 0.0,
        -point_camera_fej(0) /
            (point_camera_fej(2) * point_camera_fej(2)),
        0.0, 1.0 / point_camera_fej(2),
        -point_camera_fej(1) /
            (point_camera_fej(2) * point_camera_fej(2));
    Eigen::Matrix<double, 2, 3> projection_current;
    projection_current <<
        1.0 / point_camera_current(2), 0.0,
        -point_camera_current(0) /
            (point_camera_current(2) * point_camera_current(2)),
        0.0, 1.0 / point_camera_current(2),
        -point_camera_current(1) /
            (point_camera_current(2) * point_camera_current(2));
    const Eigen::Vector2d uv_fej(point_camera_fej(0) / point_camera_fej(2),
                                 point_camera_fej(1) / point_camera_fej(2));
    const Eigen::Matrix<double, 2, 3> pixel_projection =
        mixed_fej_radtan_jacobian(uv_current, intrinsics) * projection_fej;
    const Eigen::Matrix<double, 2, 3> pixel_projection_current =
        mixed_fej_radtan_jacobian(uv_current, intrinsics) *
        projection_current;
    const Eigen::Matrix<double, 2, 3> pixel_projection_fej_uv =
        mixed_fej_radtan_jacobian(uv_fej, intrinsics) * projection_fej;
    const Eigen::Matrix3d global_to_camera_fej =
        rotation_imu_to_camera * clone->Rot_fej();
    const Eigen::Matrix3d global_to_camera_current =
        rotation_imu_to_camera * clone->Rot();
    result.H_f.block<2, 3>(2 * index, 0) =
        pixel_projection * global_to_camera_fej;
    result.H_x.block<2, 8>(2 * index, 0) =
        mixed_fej_intrinsics_jacobian(uv_current, intrinsics);
    result.H_x.block<2, 3>(2 * index, 8 + 6 * index) =
        pixel_projection * rotation_imu_to_camera *
        schurvio_cp1::skew(point_imu_fej);
    result.H_x.block<2, 3>(2 * index, 11 + 6 * index) =
        -pixel_projection * global_to_camera_fej;

    result.all_current_H_x.block<2, 8>(2 * index, 0) =
        mixed_fej_intrinsics_jacobian(uv_current, intrinsics);
    result.all_current_H_x.block<2, 3>(2 * index, 8 + 6 * index) =
        pixel_projection_current * rotation_imu_to_camera *
        schurvio_cp1::skew(point_imu_current);
    result.all_current_H_x.block<2, 3>(2 * index, 11 + 6 * index) =
        -pixel_projection_current * global_to_camera_current;
    result.fej_uv_H_x.block<2, 8>(2 * index, 0) =
        mixed_fej_intrinsics_jacobian(uv_fej, intrinsics);
    result.fej_uv_H_x.block<2, 3>(2 * index, 8 + 6 * index) =
        pixel_projection_fej_uv * rotation_imu_to_camera *
        schurvio_cp1::skew(point_imu_fej);
    result.fej_uv_H_x.block<2, 3>(2 * index, 11 + 6 * index) =
        -pixel_projection_fej_uv * global_to_camera_fej;
  }
  return result;
}

enum class GoldenCandidateFailure {
  kNone,
  kRank,
  kGeometry,
  kNonfinite,
  kProductionGate,
};

struct GoldenCandidate {
  bool valid = false;
  double pixel_cost = std::numeric_limits<double>::quiet_NaN();
  double posterior_cost = std::numeric_limits<double>::quiet_NaN();
  GoldenCandidateFailure failure = GoldenCandidateFailure::kNonfinite;
};

struct GoldenDecision {
  int selected_pass = 0;
  GoldenCandidateFailure reason = GoldenCandidateFailure::kNone;
};

GoldenDecision select_golden_candidate(const GoldenCandidate &pass_one,
                                       const GoldenCandidate &pass_two) {
  if (!pass_one.valid || !std::isfinite(pass_one.pixel_cost) ||
      !std::isfinite(pass_one.posterior_cost)) {
    return {0, pass_one.failure};
  }
  if (!pass_two.valid || !std::isfinite(pass_two.pixel_cost) ||
      !std::isfinite(pass_two.posterior_cost)) {
    return {1, pass_two.failure};
  }
  const double pixel_tolerance =
      1.0e-9 * std::max(1.0, std::abs(pass_one.pixel_cost));
  const double posterior_tolerance =
      1.0e-9 * std::max(1.0, std::abs(pass_one.posterior_cost));
  if (pass_two.pixel_cost <= pass_one.pixel_cost + pixel_tolerance &&
      pass_two.posterior_cost <=
          pass_one.posterior_cost + posterior_tolerance) {
    return {2, GoldenCandidateFailure::kNone};
  }
  return {1, GoldenCandidateFailure::kNone};
}

struct GoldenMeanOnlyPass {
  bool valid = false;
  GoldenCandidateFailure failure = GoldenCandidateFailure::kNonfinite;
  Eigen::MatrixXd H;
  Eigen::VectorXd residual;
  std::vector<MSCKFUpdatePreviewBlock> layout;
  Eigen::VectorXd delta;
  double nis = std::numeric_limits<double>::quiet_NaN();
  Eigen::Index raw_rows = 0;
  Eigen::Index degrees_of_freedom = 0;
  double affine_correction_norm = std::numeric_limits<double>::quiet_NaN();
  double raw_residual_error = std::numeric_limits<double>::quiet_NaN();
  double raw_state_jacobian_error = std::numeric_limits<double>::quiet_NaN();
  double raw_landmark_jacobian_error =
      std::numeric_limits<double>::quiet_NaN();
  std::size_t numerical_repair_count = 0;
  const Eigen::MatrixXd *prior_object = nullptr;
  const double *prior_data = nullptr;
};

GoldenMeanOnlyPass build_golden_mean_only_pass(
    Fixture &fixture, const Eigen::VectorXd &linearization_delta,
    const Eigen::MatrixXd &frozen_prior) {
  GoldenMeanOnlyPass result;
  result.prior_object = &frozen_prior;
  result.prior_data = frozen_prior.data();
  if (linearization_delta.rows() != frozen_prior.rows() ||
      !linearization_delta.allFinite() || !frozen_prior.allFinite()) {
    return result;
  }
  materialize_absolute_delta(fixture, linearization_delta);
  auto detached_feature =
      std::make_shared<Feature>(*fixture.accepted_feature);
  auto clones_camera = camera_clone_map(fixture.state);
  FeatureInitializer initializer(fixture.initializer_options);
  if (!initializer.single_triangulation(detached_feature, clones_camera) ||
      !initializer.single_gaussnewton(detached_feature, clones_camera) ||
      !detached_feature->p_FinG.allFinite()) {
    result.failure = GoldenCandidateFailure::kGeometry;
    return result;
  }

  UpdaterHelper::UpdaterHelperFeature helper_feature;
  helper_feature.featid = detached_feature->featid;
  helper_feature.uvs = detached_feature->uvs;
  helper_feature.uvs_norm = detached_feature->uvs_norm;
  helper_feature.timestamps = detached_feature->timestamps;
  helper_feature.feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  helper_feature.p_FinG = detached_feature->p_FinG;
  helper_feature.p_FinG_fej = detached_feature->p_FinG;
  Eigen::MatrixXd H_f;
  Eigen::MatrixXd H_x;
  Eigen::VectorXd raw_residual;
  std::vector<std::shared_ptr<Type>> jacobian_order;
  UpdaterHelper::get_feature_jacobian_full(
      fixture.state, helper_feature, H_f, H_x, raw_residual,
      jacobian_order);
  if (H_f.rows() != 8 || H_f.cols() != 3 || H_x.rows() != 8 ||
      H_x.cols() != 32 || raw_residual.rows() != 8 ||
      jacobian_order.size() != 5U || !H_f.allFinite() || !H_x.allFinite() ||
      !raw_residual.allFinite()) {
    return result;
  }
  const MixedFejRawOracle oracle =
      build_mixed_fej_raw_oracle(fixture, helper_feature);
  result.raw_residual_error = (raw_residual - oracle.residual).norm();
  result.raw_state_jacobian_error = (H_x - oracle.H_x).norm();
  result.raw_landmark_jacobian_error = (H_f - oracle.H_f).norm();

  Eigen::VectorXd local_delta = Eigen::VectorXd::Zero(H_x.cols());
  Eigen::MatrixXd fixed_chart =
      Eigen::MatrixXd::Identity(H_x.cols(), H_x.cols());
  Eigen::Index local_offset = 0;
  for (const auto &variable : jacobian_order) {
    local_delta.segment(local_offset, variable->size()) =
        linearization_delta.segment(variable->id(), variable->size());
    if (variable->size() == 6) {
      fixed_chart.block<3, 3>(local_offset, local_offset) =
          schurvio_cp1::fixed_chart_orientation_map(
              linearization_delta.segment<3>(variable->id()));
    }
    local_offset += variable->size();
  }
  if (local_offset != H_x.cols()) {
    return result;
  }
  const Eigen::MatrixXd fixed_chart_jacobian = H_x * fixed_chart;
  const Eigen::VectorXd affine_correction =
      fixed_chart_jacobian * local_delta;
  result.affine_correction_norm = affine_correction.norm();
  const Eigen::VectorXd corrected_residual =
      raw_residual + affine_correction;
  const SchurReductionResult reduction = ov_msckf::SchurUpdate::Reduce(
      fixed_chart_jacobian, H_f, corrected_residual,
      fixture.updater_options.sigma_pix);
  result.raw_rows = reduction.raw_rows;
  result.degrees_of_freedom = reduction.degrees_of_freedom;
  result.numerical_repair_count =
      reduction.jitter_count + reduction.clamp_count +
      reduction.regularization_count + reduction.fallback_count;
  if (!reduction.accepted()) {
    result.failure =
        reduction.status == ov_msckf::SchurReductionStatus::kRankDeficient ||
                reduction.status ==
                    ov_msckf::SchurReductionStatus::kIllConditioned
            ? GoldenCandidateFailure::kRank
            : GoldenCandidateFailure::kNonfinite;
    return result;
  }
  result.H = reduction.H_reduced;
  result.residual = reduction.residual_reduced;
  UpdaterHelper::measurement_compress_inplace(result.H, result.residual);
  if (result.H.rows() <= 0 || result.H.cols() != H_x.cols() ||
      result.residual.rows() != result.H.rows() || !result.H.allFinite() ||
      !result.residual.allFinite()) {
    return result;
  }
  local_offset = 0;
  for (const auto &variable : jacobian_order) {
    result.layout.push_back(
        {variable->id(), variable->size(), local_offset});
    local_offset += variable->size();
  }
  Eigen::MatrixXd full_H = Eigen::MatrixXd::Zero(
      result.H.rows(), frozen_prior.rows());
  local_offset = 0;
  for (const auto &variable : jacobian_order) {
    full_H.block(0, variable->id(), result.H.rows(), variable->size()) =
        result.H.block(0, local_offset, result.H.rows(), variable->size());
    local_offset += variable->size();
  }
  const Eigen::MatrixXd cross = frozen_prior * full_H.transpose();
  const Eigen::MatrixXd measurement_covariance =
      fixture.updater_options.sigma_pix_sq *
      Eigen::MatrixXd::Identity(result.residual.rows(),
                                result.residual.rows());
  const Eigen::MatrixXd innovation =
      full_H * cross + measurement_covariance;
  Eigen::LDLT<Eigen::MatrixXd> innovation_factor(innovation);
  if (innovation_factor.info() != Eigen::Success ||
      !innovation_factor.isPositive()) {
    return result;
  }
  const Eigen::VectorXd solved_residual =
      innovation_factor.solve(result.residual);
  result.delta = cross * solved_residual;
  result.nis = result.residual.dot(solved_residual);
  if (!result.delta.allFinite() || !std::isfinite(result.nis)) {
    return result;
  }
  result.valid = true;
  result.failure = GoldenCandidateFailure::kNone;
  return result;
}

struct GoldenTrueCosts {
  bool valid = false;
  double pixel = std::numeric_limits<double>::quiet_NaN();
  double posterior = std::numeric_limits<double>::quiet_NaN();
};

GoldenTrueCosts evaluate_golden_true_costs(
    Fixture &fixture, const Eigen::VectorXd &candidate_delta,
    const Eigen::MatrixXd &frozen_prior) {
  GoldenTrueCosts result;
  if (candidate_delta.rows() != frozen_prior.rows() ||
      !candidate_delta.allFinite()) {
    return result;
  }
  materialize_absolute_delta(fixture, candidate_delta);
  auto detached_feature =
      std::make_shared<Feature>(*fixture.accepted_feature);
  auto clones_camera = camera_clone_map(fixture.state);
  FeatureInitializer initializer(fixture.initializer_options);
  if (!initializer.single_triangulation(detached_feature, clones_camera) ||
      !initializer.single_gaussnewton(detached_feature, clones_camera) ||
      !detached_feature->p_FinG.allFinite()) {
    return result;
  }
  UpdaterHelper::UpdaterHelperFeature helper_feature;
  helper_feature.featid = detached_feature->featid;
  helper_feature.uvs = detached_feature->uvs;
  helper_feature.uvs_norm = detached_feature->uvs_norm;
  helper_feature.timestamps = detached_feature->timestamps;
  helper_feature.feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  helper_feature.p_FinG = detached_feature->p_FinG;
  helper_feature.p_FinG_fej = detached_feature->p_FinG;
  const MixedFejRawOracle oracle =
      build_mixed_fej_raw_oracle(fixture, helper_feature);
  if (!oracle.residual.allFinite()) {
    return result;
  }
  Eigen::LLT<Eigen::MatrixXd> prior_factor(frozen_prior);
  if (prior_factor.info() != Eigen::Success) {
    return result;
  }
  const Eigen::VectorXd prior_coordinate =
      prior_factor.matrixL().solve(candidate_delta);
  if (!prior_coordinate.allFinite()) {
    return result;
  }
  result.pixel = oracle.residual.squaredNorm();
  result.posterior =
      0.5 * prior_coordinate.squaredNorm() +
      0.5 * oracle.residual.squaredNorm() /
          fixture.updater_options.sigma_pix_sq;
  result.valid = std::isfinite(result.pixel) &&
                 std::isfinite(result.posterior);
  return result;
}

TEST(CP1MixedFejGolden,
     ProductionRawSchurAndPreviewMatchFixedChartIndependentOracle) {
  Fixture entry = make_mixed_fej_fixture();
  Fixture working = make_mixed_fej_fixture();
  const MSCKFUpdatePriorSnapshot entry_prior =
      UpdaterMSCKFPreview::CapturePrior(entry.state);
  const Feature live_feature_before = *working.accepted_feature;
  const Eigen::VectorXd absolute_delta =
      mixed_fej_linearization_delta(working);
  ASSERT_EQ(absolute_delta.rows(), 47);
  materialize_absolute_delta(working, absolute_delta);
  const MSCKFUpdatePriorSnapshot working_point =
      UpdaterMSCKFPreview::CapturePrior(working.state);
  const Eigen::MatrixXd camera_cache_before = working.camera->get_value();

  auto detached_feature =
      std::make_shared<Feature>(*working.accepted_feature);
  FeatureInitializer initializer(working.initializer_options);
  auto clones_camera = camera_clone_map(working.state);
  ASSERT_TRUE(initializer.single_triangulation(detached_feature,
                                                clones_camera));
  ASSERT_TRUE(initializer.single_gaussnewton(detached_feature,
                                              clones_camera));
  ASSERT_TRUE(detached_feature->p_FinG.allFinite());

  UpdaterHelper::UpdaterHelperFeature helper_feature;
  helper_feature.featid = detached_feature->featid;
  helper_feature.uvs = detached_feature->uvs;
  helper_feature.uvs_norm = detached_feature->uvs_norm;
  helper_feature.timestamps = detached_feature->timestamps;
  helper_feature.feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  helper_feature.p_FinG = detached_feature->p_FinG;
  helper_feature.p_FinG_fej = detached_feature->p_FinG;

  Eigen::MatrixXd H_f;
  Eigen::MatrixXd H_x;
  Eigen::VectorXd residual;
  std::vector<std::shared_ptr<Type>> jacobian_order;
  UpdaterHelper::get_feature_jacobian_full(
      working.state, helper_feature, H_f, H_x, residual, jacobian_order);
  ASSERT_EQ(H_f.rows(), 8);
  ASSERT_EQ(H_f.cols(), 3);
  ASSERT_EQ(H_x.rows(), 8);
  ASSERT_EQ(H_x.cols(), 32);
  ASSERT_EQ(residual.rows(), 8);
  ASSERT_EQ(jacobian_order.size(), 5U);
  EXPECT_EQ(jacobian_order.front(),
            working.state->_cam_intrinsics.at(0));
  EXPECT_EQ(jacobian_order.front()->id(), 15);
  EXPECT_EQ(jacobian_order.front()->size(), 8);

  const MixedFejRawOracle raw_oracle =
      build_mixed_fej_raw_oracle(working, helper_feature);
  EXPECT_LE((residual - raw_oracle.residual).norm(),
            schurvio_cp1::mixed_tolerance(
                1.0e-12, 1.0e-10, raw_oracle.residual.norm()));
  EXPECT_LE((H_f - raw_oracle.H_f).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-11, 1.0e-10,
                                          raw_oracle.H_f.norm()));
  EXPECT_LE((H_x - raw_oracle.H_x).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-11, 1.0e-10,
                                          raw_oracle.H_x.norm()));
  EXPECT_GT(H_x.leftCols(8).norm(), 1.0);
  EXPECT_GT((raw_oracle.H_x - raw_oracle.all_current_H_x).norm(), 1.0e-4);
  EXPECT_GT((raw_oracle.H_x - raw_oracle.fej_uv_H_x).norm(), 1.0e-4);

  Eigen::VectorXd local_delta = Eigen::VectorXd::Zero(32);
  Eigen::MatrixXd fixed_chart = Eigen::MatrixXd::Identity(32, 32);
  Eigen::Index local_offset = 0;
  for (const auto &variable : jacobian_order) {
    local_delta.segment(local_offset, variable->size()) =
        absolute_delta.segment(variable->id(), variable->size());
    if (variable->size() == 6) {
      fixed_chart.block<3, 3>(local_offset, local_offset) =
          schurvio_cp1::fixed_chart_orientation_map(
              absolute_delta.segment<3>(variable->id()));
    }
    local_offset += variable->size();
  }
  ASSERT_EQ(local_offset, 32);
  const Eigen::MatrixXd fixed_chart_jacobian = H_x * fixed_chart;
  const Eigen::VectorXd corrected_residual =
      residual + fixed_chart_jacobian * local_delta;
  EXPECT_GT((corrected_residual - residual).norm(), 1.0e-6);
  EXPECT_GT((corrected_residual -
             (residual - fixed_chart_jacobian * local_delta))
                .norm(),
            1.0e-6);

  const Eigen::MatrixXd H_f_before = H_f;
  const Eigen::MatrixXd fixed_chart_jacobian_before = fixed_chart_jacobian;
  const Eigen::VectorXd corrected_residual_before = corrected_residual;
  const SchurReductionResult production_reduction =
      ov_msckf::SchurUpdate::Reduce(
          fixed_chart_jacobian, H_f, corrected_residual,
          working.updater_options.sigma_pix);
  expect_same_bytes(H_f, H_f_before);
  expect_same_bytes(fixed_chart_jacobian, fixed_chart_jacobian_before);
  expect_same_bytes(corrected_residual, corrected_residual_before);
  ASSERT_TRUE(production_reduction.accepted())
      << ov_msckf::schur_reduction_status_name(production_reduction.status)
      << "/"
      << ov_msckf::schur_reduction_stage_name(production_reduction.stage);
  EXPECT_EQ(production_reduction.raw_rows, 8);
  EXPECT_EQ(production_reduction.degrees_of_freedom, 5);
  EXPECT_EQ(production_reduction.H_reduced.rows(), 5);
  EXPECT_EQ(production_reduction.H_reduced.cols(), 32);
  EXPECT_EQ(production_reduction.jitter_count, 0U);
  EXPECT_EQ(production_reduction.clamp_count, 0U);
  EXPECT_EQ(production_reduction.regularization_count, 0U);
  EXPECT_EQ(production_reduction.fallback_count, 0U);

  const double sigma = working.updater_options.sigma_pix;
  const schurvio_cp1::SchurReduction independent_reduction =
      schurvio_cp1::reduce_landmark(fixed_chart_jacobian / sigma,
                                    H_f / sigma,
                                    corrected_residual / sigma);
  ASSERT_EQ(independent_reduction.status,
            schurvio_cp1::FactorStatus::kAccepted);
  EXPECT_LE((production_reduction.lambda - independent_reduction.lambda).norm(),
            schurvio_cp1::mixed_tolerance(
                1.0e-10, 1.0e-8, independent_reduction.lambda.norm()));
  EXPECT_LE((production_reduction.eta - independent_reduction.eta).norm(),
            schurvio_cp1::mixed_tolerance(
                1.0e-10, 1.0e-8, independent_reduction.eta.norm()));
  EXPECT_LE(std::abs(production_reduction.gamma -
                     independent_reduction.gamma),
            schurvio_cp1::mixed_tolerance(
                1.0e-10, 1.0e-8,
                std::abs(independent_reduction.gamma)));

  Eigen::MatrixXd compressed_jacobian = production_reduction.H_reduced;
  Eigen::VectorXd compressed_residual =
      production_reduction.residual_reduced;
  UpdaterHelper::measurement_compress_inplace(compressed_jacobian,
                                               compressed_residual);
  ASSERT_EQ(compressed_jacobian.rows(), 5);
  ASSERT_EQ(compressed_jacobian.cols(), 32);
  ASSERT_EQ(compressed_residual.rows(), 5);
  std::vector<MSCKFUpdatePreviewBlock> layout;
  local_offset = 0;
  for (const auto &variable : jacobian_order) {
    layout.push_back({variable->id(), variable->size(), local_offset});
    local_offset += variable->size();
  }
  const Eigen::MatrixXd measurement_covariance =
      sigma * sigma *
      Eigen::MatrixXd::Identity(compressed_residual.rows(),
                                compressed_residual.rows());
  const Eigen::MatrixXd preview_H_before = compressed_jacobian;
  const Eigen::VectorXd preview_r_before = compressed_residual;
  const Eigen::MatrixXd preview_R_before = measurement_covariance;
  const ov_msckf::MSCKFUpdatePreviewResult preview =
      UpdaterMSCKFPreview::ComputeFromSnapshot(
          entry_prior.filter, layout, compressed_jacobian,
          compressed_residual, measurement_covariance);
  expect_same_bytes(compressed_jacobian, preview_H_before);
  expect_same_bytes(compressed_residual, preview_r_before);
  expect_same_bytes(measurement_covariance, preview_R_before);
  ASSERT_TRUE(preview.accepted())
      << ov_msckf::msckf_update_preview_status_name(
             preview.diagnostics.status)
      << "/"
      << ov_msckf::msckf_update_preview_stage_name(preview.diagnostics.stage);
  EXPECT_EQ(preview.diagnostics.state_dimension, 47);
  EXPECT_EQ(preview.diagnostics.measurement_dimension, 5);
  EXPECT_EQ(preview.diagnostics.jacobian_dimension, 32);
  EXPECT_EQ(preview.diagnostics.jitter_count, 0U);
  EXPECT_EQ(preview.diagnostics.repair_count, 0U);
  EXPECT_EQ(preview.diagnostics.alternate_solve_count, 0U);
  EXPECT_EQ(preview.diagnostics.clamp_count, 0U);
  EXPECT_EQ(preview.diagnostics.regularization_count, 0U);
  EXPECT_EQ(preview.diagnostics.fallback_count, 0U);

  Eigen::JacobiSVD<Eigen::MatrixXd> landmark_svd(
      H_f / sigma, Eigen::ComputeFullU | Eigen::ComputeThinV);
  ASSERT_EQ(landmark_svd.singularValues().size(), 3);
  const Eigen::MatrixXd null_basis =
      landmark_svd.matrixU().rightCols(5);
  const Eigen::MatrixXd null_jacobian =
      null_basis.transpose() * (fixed_chart_jacobian / sigma);
  const Eigen::VectorXd null_residual =
      null_basis.transpose() * (corrected_residual / sigma);
  Eigen::MatrixXd full_jacobian = Eigen::MatrixXd::Zero(5, 47);
  local_offset = 0;
  for (const auto &variable : jacobian_order) {
    full_jacobian.block(0, variable->id(), 5, variable->size()) =
        null_jacobian.block(0, local_offset, 5, variable->size());
    local_offset += variable->size();
  }
  const Eigen::MatrixXd &prior = entry_prior.filter.covariance;
  const Eigen::MatrixXd cross = prior * full_jacobian.transpose();
  const Eigen::MatrixXd innovation =
      Eigen::MatrixXd::Identity(5, 5) + full_jacobian * cross;
  Eigen::LDLT<Eigen::MatrixXd> innovation_factor(innovation);
  ASSERT_EQ(innovation_factor.info(), Eigen::Success);
  ASSERT_TRUE(innovation_factor.isPositive());
  const Eigen::VectorXd oracle_increment =
      cross * innovation_factor.solve(null_residual);
  const Eigen::MatrixXd oracle_covariance_raw =
      prior - cross * innovation_factor.solve(cross.transpose());
  const Eigen::MatrixXd oracle_covariance =
      0.5 * (oracle_covariance_raw + oracle_covariance_raw.transpose());
  const double oracle_nis =
      null_residual.dot(innovation_factor.solve(null_residual));
  EXPECT_LE((preview.dx - oracle_increment).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          oracle_increment.norm()));
  EXPECT_LE((preview.P_plus - oracle_covariance).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          oracle_covariance.norm()));

  Eigen::MatrixXd production_full_H =
      Eigen::MatrixXd::Zero(compressed_jacobian.rows(), 47);
  local_offset = 0;
  for (const auto &variable : jacobian_order) {
    production_full_H.block(0, variable->id(), compressed_jacobian.rows(),
                            variable->size()) =
        compressed_jacobian.block(0, local_offset,
                                  compressed_jacobian.rows(),
                                  variable->size());
    local_offset += variable->size();
  }
  const Eigen::MatrixXd production_cross =
      prior * production_full_H.transpose();
  const Eigen::MatrixXd production_innovation =
      production_full_H * production_cross + measurement_covariance;
  Eigen::LDLT<Eigen::MatrixXd> production_innovation_factor(
      production_innovation);
  ASSERT_EQ(production_innovation_factor.info(), Eigen::Success);
  ASSERT_TRUE(production_innovation_factor.isPositive());
  const double production_nis = compressed_residual.dot(
      production_innovation_factor.solve(compressed_residual));
  EXPECT_LE(std::abs(production_nis - oracle_nis),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          std::abs(oracle_nis)));
  const boost::math::chi_squared gate_distribution(
      static_cast<double>(production_reduction.degrees_of_freedom));
  const double gate_threshold =
      working.updater_options.chi2_multipler *
      boost::math::quantile(gate_distribution, 0.95);
  EXPECT_LE(production_nis, gate_threshold);
  EXPECT_TRUE(preview.dx.allFinite());
  EXPECT_TRUE(preview.P_plus.allFinite());
  EXPECT_TRUE(std::isfinite(production_nis));

  const double symmetry_error = schurvio_cp1::matrix_inf_norm(
      preview.P_plus - preview.P_plus.transpose());
  const double covariance_scale =
      std::max(1.0, schurvio_cp1::matrix_inf_norm(preview.P_plus));
  EXPECT_LE(symmetry_error, 1.0e-10 * covariance_scale);
  const Eigen::MatrixXd posterior_symmetric =
      0.5 * (preview.P_plus + preview.P_plus.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> posterior_eigensolver(
      posterior_symmetric);
  ASSERT_EQ(posterior_eigensolver.info(), Eigen::Success);
  EXPECT_GE(posterior_eigensolver.eigenvalues().minCoeff(),
            -1.0e-10 *
                std::max(1.0,
                         posterior_eigensolver.eigenvalues().maxCoeff()));

  EXPECT_TRUE(UpdaterMSCKFPreview::MatchPrior(entry.state, entry_prior)
                  .accepted());
  EXPECT_TRUE(UpdaterMSCKFPreview::MatchPrior(working.state, working_point)
                  .accepted());
  expect_same_feature_observations(*working.accepted_feature,
                                   live_feature_before);
  expect_same_bytes(working.camera->get_value(), camera_cache_before);
}

TEST(CP1MixedFejGolden,
     ActualTwoPassUsesSamePriorEvaluatesTrueCostsAndCommitsOnce) {
  Fixture entry = make_mixed_fej_fixture();
  const MSCKFUpdatePriorSnapshot entry_snapshot =
      UpdaterMSCKFPreview::CapturePrior(entry.state);
  const Eigen::MatrixXd frozen_prior_before =
      entry_snapshot.filter.covariance;
  const double *const frozen_prior_data =
      entry_snapshot.filter.covariance.data();
  const Feature entry_feature_before = *entry.accepted_feature;
  std::vector<Eigen::MatrixXd> entry_fej_before;
  for (const auto &variable : entry.state_order) {
    entry_fej_before.push_back(variable->fej());
  }

  int production_preview_count = 0;
  int live_commit_count = 0;
  int selected_covariance_count = 0;
  Fixture pass_one_fixture = make_mixed_fej_fixture();
  const Eigen::VectorXd zero_delta = Eigen::VectorXd::Zero(47);
  const GoldenMeanOnlyPass pass_one = build_golden_mean_only_pass(
      pass_one_fixture, zero_delta, entry_snapshot.filter.covariance);
  ASSERT_TRUE(pass_one.valid);
  EXPECT_EQ(pass_one.raw_rows, 8);
  EXPECT_EQ(pass_one.degrees_of_freedom, 5);
  EXPECT_EQ(pass_one.numerical_repair_count, 0U);
  EXPECT_EQ(pass_one.affine_correction_norm, 0.0);
  EXPECT_EQ(pass_one.prior_object, &entry_snapshot.filter.covariance);
  EXPECT_EQ(pass_one.prior_data, frozen_prior_data);
  EXPECT_LE(pass_one.raw_residual_error, 1.0e-10);
  EXPECT_LE(pass_one.raw_state_jacobian_error, 1.0e-8);
  EXPECT_LE(pass_one.raw_landmark_jacobian_error, 1.0e-8);
  EXPECT_EQ(production_preview_count, 0);
  EXPECT_EQ(selected_covariance_count, 0);

  Fixture pass_two_fixture = make_mixed_fej_fixture();
  const GoldenMeanOnlyPass pass_two = build_golden_mean_only_pass(
      pass_two_fixture, pass_one.delta,
      entry_snapshot.filter.covariance);
  ASSERT_TRUE(pass_two.valid);
  EXPECT_EQ(pass_two.raw_rows, 8);
  EXPECT_EQ(pass_two.degrees_of_freedom, 5);
  EXPECT_EQ(pass_two.numerical_repair_count, 0U);
  EXPECT_GT(pass_two.affine_correction_norm, 1.0e-8);
  EXPECT_EQ(pass_two.prior_object, &entry_snapshot.filter.covariance);
  EXPECT_EQ(pass_two.prior_data, frozen_prior_data);
  EXPECT_LE(pass_two.raw_residual_error, 1.0e-10);
  EXPECT_LE(pass_two.raw_state_jacobian_error, 1.0e-8);
  EXPECT_LE(pass_two.raw_landmark_jacobian_error, 1.0e-8);
  EXPECT_EQ(production_preview_count, 0);
  EXPECT_EQ(selected_covariance_count, 0);

  const boost::math::chi_squared gate_distribution(5.0);
  const double gate_threshold =
      entry.updater_options.chi2_multipler *
      boost::math::quantile(gate_distribution, 0.95);
  EXPECT_LE(pass_one.nis, gate_threshold);
  EXPECT_LE(pass_two.nis, gate_threshold);

  Fixture pass_one_cost_fixture = make_mixed_fej_fixture();
  Fixture pass_two_cost_fixture = make_mixed_fej_fixture();
  const GoldenTrueCosts pass_one_cost = evaluate_golden_true_costs(
      pass_one_cost_fixture, pass_one.delta,
      entry_snapshot.filter.covariance);
  const GoldenTrueCosts pass_two_cost = evaluate_golden_true_costs(
      pass_two_cost_fixture, pass_two.delta,
      entry_snapshot.filter.covariance);
  ASSERT_TRUE(pass_one_cost.valid);
  ASSERT_TRUE(pass_two_cost.valid);
  ASSERT_TRUE(std::isfinite(pass_one_cost.pixel));
  ASSERT_TRUE(std::isfinite(pass_one_cost.posterior));
  ASSERT_TRUE(std::isfinite(pass_two_cost.pixel));
  ASSERT_TRUE(std::isfinite(pass_two_cost.posterior));

  const GoldenCandidate pass_one_candidate{
      pass_one.nis <= gate_threshold, pass_one_cost.pixel,
      pass_one_cost.posterior,
      pass_one.nis <= gate_threshold
          ? GoldenCandidateFailure::kNone
          : GoldenCandidateFailure::kProductionGate};
  const GoldenCandidate pass_two_candidate{
      pass_two.nis <= gate_threshold, pass_two_cost.pixel,
      pass_two_cost.posterior,
      pass_two.nis <= gate_threshold
          ? GoldenCandidateFailure::kNone
          : GoldenCandidateFailure::kProductionGate};
  const GoldenDecision decision =
      select_golden_candidate(pass_one_candidate, pass_two_candidate);
  ASSERT_TRUE(decision.selected_pass == 1 || decision.selected_pass == 2);
  const GoldenMeanOnlyPass &selected =
      decision.selected_pass == 2 ? pass_two : pass_one;

  // No pass-one covariance exists here: pass two was built while both the
  // production-preview and selected-covariance counters were still zero, and
  // both mean-only systems retained the exact same P-minus object/data address.
  expect_same_bytes(entry_snapshot.filter.covariance,
                    frozen_prior_before);
  EXPECT_EQ(entry_snapshot.filter.covariance.data(), frozen_prior_data);
  EXPECT_TRUE(UpdaterMSCKFPreview::MatchPrior(entry.state, entry_snapshot)
                  .accepted());

  const Eigen::MatrixXd selected_measurement_covariance =
      entry.updater_options.sigma_pix_sq *
      Eigen::MatrixXd::Identity(selected.residual.rows(),
                                selected.residual.rows());
  ++production_preview_count;
  ++selected_covariance_count;
  const ov_msckf::MSCKFUpdatePreviewResult selected_preview =
      UpdaterMSCKFPreview::ComputeFromSnapshot(
          entry_snapshot.filter, selected.layout, selected.H,
          selected.residual, selected_measurement_covariance);
  ASSERT_TRUE(selected_preview.accepted())
      << ov_msckf::msckf_update_preview_status_name(
             selected_preview.diagnostics.status)
      << "/"
      << ov_msckf::msckf_update_preview_stage_name(
             selected_preview.diagnostics.stage);
  EXPECT_EQ(production_preview_count, 1);
  EXPECT_EQ(selected_covariance_count, 1);
  EXPECT_EQ(selected_preview.diagnostics.jitter_count, 0U);
  EXPECT_EQ(selected_preview.diagnostics.repair_count, 0U);
  EXPECT_EQ(selected_preview.diagnostics.alternate_solve_count, 0U);
  EXPECT_EQ(selected_preview.diagnostics.clamp_count, 0U);
  EXPECT_EQ(selected_preview.diagnostics.regularization_count, 0U);
  EXPECT_EQ(selected_preview.diagnostics.fallback_count, 0U);

  Eigen::MatrixXd selected_full_H = Eigen::MatrixXd::Zero(
      selected.H.rows(), entry_snapshot.filter.covariance.rows());
  for (const MSCKFUpdatePreviewBlock &block : selected.layout) {
    selected_full_H.block(0, block.covariance_id, selected.H.rows(),
                          block.size) =
        selected.H.block(0, block.offset, selected.H.rows(), block.size);
  }
  const Eigen::MatrixXd selected_cross =
      entry_snapshot.filter.covariance * selected_full_H.transpose();
  const Eigen::MatrixXd selected_innovation =
      selected_full_H * selected_cross + selected_measurement_covariance;
  Eigen::LDLT<Eigen::MatrixXd> selected_innovation_factor(
      selected_innovation);
  ASSERT_EQ(selected_innovation_factor.info(), Eigen::Success);
  ASSERT_TRUE(selected_innovation_factor.isPositive());
  const Eigen::VectorXd selected_oracle_increment =
      selected_cross *
      selected_innovation_factor.solve(selected.residual);
  const Eigen::MatrixXd selected_oracle_covariance_raw =
      entry_snapshot.filter.covariance -
      selected_cross *
          selected_innovation_factor.solve(selected_cross.transpose());
  const Eigen::MatrixXd selected_oracle_covariance =
      0.5 * (selected_oracle_covariance_raw +
             selected_oracle_covariance_raw.transpose());
  const double increment_oracle_error =
      (selected_preview.dx - selected_oracle_increment).norm();
  const double covariance_oracle_error =
      (selected_preview.P_plus - selected_oracle_covariance).norm();
  EXPECT_LE(increment_oracle_error,
            schurvio_cp1::mixed_tolerance(
                1.0e-12, 1.0e-10, selected_oracle_increment.norm()));
  EXPECT_LE(covariance_oracle_error,
            schurvio_cp1::mixed_tolerance(
                1.0e-12, 1.0e-10, selected_oracle_covariance.norm()));
  EXPECT_LE((selected_preview.dx - selected.delta).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          selected.delta.norm()));

  ASSERT_TRUE(UpdaterMSCKFPreview::MatchPrior(entry.state, entry_snapshot)
                  .accepted());
  ++live_commit_count;
  ASSERT_TRUE(StateHelper::CommitPrecomputedUpdate(
      entry.state, selected_preview.dx, selected_preview.P_plus));
  EXPECT_EQ(live_commit_count, 1);
  EXPECT_EQ(production_preview_count, 1);
  EXPECT_EQ(selected_covariance_count, 1);
  expect_same_bytes(StateHelper::get_full_covariance(entry.state),
                    selected_preview.P_plus);
  for (std::size_t index = 0; index < entry.state_order.size(); ++index) {
    expect_same_bytes(entry.state_order[index]->fej(),
                      entry_fej_before[index]);
  }
  expect_same_feature_observations(*entry.accepted_feature,
                                   entry_feature_before);
  expect_same_bytes(entry_snapshot.filter.covariance,
                    frozen_prior_before);
  EXPECT_EQ(entry_snapshot.filter.covariance.data(), frozen_prior_data);

  std::cout << std::setprecision(17)
            << "CP1_FEJ_TWO_PASS"
            << " p1_raw_r_error=" << pass_one.raw_residual_error
            << " p1_raw_Hx_error=" << pass_one.raw_state_jacobian_error
            << " p1_raw_Hf_error=" << pass_one.raw_landmark_jacobian_error
            << " p2_raw_r_error=" << pass_two.raw_residual_error
            << " p2_raw_Hx_error=" << pass_two.raw_state_jacobian_error
            << " p2_raw_Hf_error=" << pass_two.raw_landmark_jacobian_error
            << " affine_correction_norm="
            << pass_two.affine_correction_norm
            << " p1_nis=" << pass_one.nis
            << " p2_nis=" << pass_two.nis
            << " p1_Cpix=" << pass_one_cost.pixel
            << " p2_Cpix=" << pass_two_cost.pixel
            << " p1_Cpost=" << pass_one_cost.posterior
            << " p2_Cpost=" << pass_two_cost.posterior
            << " selected_pass=" << decision.selected_pass
            << " dx_oracle_error=" << increment_oracle_error
            << " P_oracle_error=" << covariance_oracle_error
            << " preview_count=" << production_preview_count
            << " covariance_count=" << selected_covariance_count
            << " commit_count=" << live_commit_count << std::endl;
}

TEST(CP1MixedFejGolden, FixedTwoPassDecisionCasesAThroughF) {
  const GoldenCandidate pass_one{true, 10.0, 8.0,
                                 GoldenCandidateFailure::kNone};

  // A: both independently evaluated nonlinear objectives improve.
  EXPECT_EQ(select_golden_candidate(
                pass_one,
                {true, 9.0, 7.0, GoldenCandidateFailure::kNone})
                .selected_pass,
            2);

  // B: equality within each cost's own declared tolerance selects pass 2.
  const double pixel_tolerance = 1.0e-9 * 10.0;
  const double posterior_tolerance = 1.0e-9 * 8.0;
  EXPECT_EQ(select_golden_candidate(
                pass_one,
                {true, 10.0 + 0.5 * pixel_tolerance,
                 8.0 + 0.5 * posterior_tolerance,
                 GoldenCandidateFailure::kNone})
                .selected_pass,
            2);

  // C: worsening either objective beyond tolerance falls back to pass 1.
  EXPECT_EQ(select_golden_candidate(
                pass_one,
                {true, 10.0 + 2.0 * pixel_tolerance, 7.0,
                 GoldenCandidateFailure::kNone})
                .selected_pass,
            1);
  EXPECT_EQ(select_golden_candidate(
                pass_one,
                {true, 9.0, 8.0 + 2.0 * posterior_tolerance,
                 GoldenCandidateFailure::kNone})
                .selected_pass,
            1);

  // D: rank, geometry, finite, or whole-proposal production-gate failure
  // invalidates all of pass 2; it never changes the locked feature population.
  const std::array<GoldenCandidateFailure, 4> pass_two_failures = {{
      GoldenCandidateFailure::kRank, GoldenCandidateFailure::kGeometry,
      GoldenCandidateFailure::kNonfinite,
      GoldenCandidateFailure::kProductionGate}};
  for (const GoldenCandidateFailure failure : pass_two_failures) {
    const GoldenDecision decision = select_golden_candidate(
        pass_one, {false, 9.0, 7.0, failure});
    EXPECT_EQ(decision.selected_pass, 1);
    EXPECT_EQ(decision.reason, failure);
  }

  // E: a valid pass-1 proposal selected after pass-2 gate failure remains a
  // single, ordinary, mutation-only commit.
  Fixture fallback_fixture = make_mixed_fej_fixture();
  const Feature fallback_feature_before = *fallback_fixture.accepted_feature;
  std::vector<Eigen::MatrixXd> fallback_fej_before;
  for (const auto &variable : fallback_fixture.state_order) {
    fallback_fej_before.push_back(variable->fej());
  }
  const Eigen::MatrixXd fallback_prior =
      StateHelper::get_full_covariance(fallback_fixture.state);
  Eigen::VectorXd fallback_increment = Eigen::VectorXd::Zero(47);
  fallback_increment(0) = 1.0e-6;
  fallback_increment(15) = 2.0e-4;
  const Eigen::MatrixXd fallback_posterior = 0.999 * fallback_prior;
  const GoldenDecision fallback_decision = select_golden_candidate(
      pass_one,
      {false, 9.0, 7.0, GoldenCandidateFailure::kProductionGate});
  int fallback_commit_count = 0;
  if (fallback_decision.selected_pass == 1) {
    ++fallback_commit_count;
    ASSERT_TRUE(StateHelper::CommitPrecomputedUpdate(
        fallback_fixture.state, fallback_increment, fallback_posterior));
  }
  EXPECT_EQ(fallback_commit_count, 1);
  expect_same_bytes(
      StateHelper::get_full_covariance(fallback_fixture.state),
      fallback_posterior);
  for (std::size_t index = 0; index < fallback_fixture.state_order.size();
       ++index) {
    expect_same_bytes(fallback_fixture.state_order[index]->fej(),
                      fallback_fej_before[index]);
  }
  expect_same_feature_observations(*fallback_fixture.accepted_feature,
                                   fallback_feature_before);

  // F: invalid pass 1 has no fallback and performs no visual commit.
  Fixture rejected_fixture = make_mixed_fej_fixture();
  const MSCKFUpdatePriorSnapshot rejected_prior =
      UpdaterMSCKFPreview::CapturePrior(rejected_fixture.state);
  const Feature rejected_feature_before = *rejected_fixture.accepted_feature;
  const GoldenDecision rejected_decision = select_golden_candidate(
      {false, 10.0, 8.0, GoldenCandidateFailure::kGeometry},
      {true, 9.0, 7.0, GoldenCandidateFailure::kNone});
  int rejected_commit_count = 0;
  if (rejected_decision.selected_pass != 0) {
    ++rejected_commit_count;
  }
  EXPECT_EQ(rejected_decision.selected_pass, 0);
  EXPECT_EQ(rejected_commit_count, 0);
  EXPECT_TRUE(
      UpdaterMSCKFPreview::MatchPrior(rejected_fixture.state, rejected_prior)
          .accepted());
  expect_same_feature_observations(*rejected_fixture.accepted_feature,
                                   rejected_feature_before);
}

TEST(CP1OnePassRegression,
     ProductionSchurUpdaterMatchesFrozenPreRefactorProposal) {
  Fixture fixture = make_fixture();
  const Eigen::MatrixXd prior = StateHelper::get_full_covariance(fixture.state);
  ASSERT_EQ(prior.rows(), 39);
  ASSERT_TRUE(prior.allFinite());
  const MSCKFUpdatePriorSnapshot entry_prior =
      UpdaterMSCKFPreview::CapturePrior(fixture.state);
  const auto entry_match =
      UpdaterMSCKFPreview::MatchPrior(fixture.state, entry_prior);
  ASSERT_TRUE(entry_match.accepted())
      << ov_msckf::msckf_update_prior_match_status_name(entry_match.status);
  EXPECT_EQ(entry_match.status, MSCKFUpdatePriorMatchStatus::kAccepted);

  const Feature accepted_input = *fixture.accepted_feature;
  const Feature rejected_input = *fixture.rejected_feature;

  std::vector<Eigen::MatrixXd> prior_values;
  std::vector<Eigen::MatrixXd> prior_fej;
  std::vector<std::shared_ptr<Type>> expected_variables;
  for (const auto &variable : fixture.state_order) {
    prior_values.push_back(variable->value());
    prior_fej.push_back(variable->fej());
    expected_variables.push_back(variable->clone());
  }

  auto expected_feature = std::make_shared<Feature>(*fixture.accepted_feature);
  auto clones_camera = camera_clone_map(fixture.state);
  FeatureInitializer initializer(fixture.initializer_options);
  ASSERT_TRUE(initializer.single_triangulation(expected_feature, clones_camera));
  ASSERT_TRUE(initializer.single_gaussnewton(expected_feature, clones_camera));
  ASSERT_TRUE(expected_feature->p_FinG.allFinite());

  UpdaterHelper::UpdaterHelperFeature helper_feature;
  helper_feature.featid = expected_feature->featid;
  helper_feature.uvs = expected_feature->uvs;
  helper_feature.uvs_norm = expected_feature->uvs_norm;
  helper_feature.timestamps = expected_feature->timestamps;
  helper_feature.feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  helper_feature.p_FinG = expected_feature->p_FinG;
  helper_feature.p_FinG_fej = expected_feature->p_FinG;

  Eigen::MatrixXd raw_landmark_jacobian;
  Eigen::MatrixXd raw_state_jacobian;
  Eigen::VectorXd raw_residual;
  std::vector<std::shared_ptr<Type>> jacobian_order;
  UpdaterHelper::get_feature_jacobian_full(
      fixture.state, helper_feature, raw_landmark_jacobian,
      raw_state_jacobian, raw_residual, jacobian_order);
  ASSERT_EQ(raw_landmark_jacobian.rows(), 8);
  ASSERT_EQ(raw_landmark_jacobian.cols(), 3);
  ASSERT_EQ(raw_state_jacobian.rows(), 8);
  ASSERT_EQ(raw_state_jacobian.cols(), 24);
  ASSERT_EQ(raw_residual.rows(), 8);
  ASSERT_EQ(jacobian_order.size(), 4U);
  ASSERT_TRUE(raw_landmark_jacobian.allFinite());
  ASSERT_TRUE(raw_state_jacobian.allFinite());
  ASSERT_TRUE(raw_residual.allFinite());

  const Eigen::MatrixXd raw_landmark_before = raw_landmark_jacobian;
  const Eigen::MatrixXd raw_state_before = raw_state_jacobian;
  const Eigen::VectorXd raw_residual_before = raw_residual;
  const SchurReductionResult reduction = ov_msckf::SchurUpdate::Reduce(
      raw_state_jacobian, raw_landmark_jacobian, raw_residual,
      fixture.updater_options.sigma_pix);
  expect_same_bytes(raw_landmark_jacobian, raw_landmark_before);
  expect_same_bytes(raw_state_jacobian, raw_state_before);
  expect_same_bytes(raw_residual, raw_residual_before);

  ASSERT_TRUE(reduction.accepted())
      << ov_msckf::schur_reduction_status_name(reduction.status) << "/"
      << ov_msckf::schur_reduction_stage_name(reduction.stage);
  EXPECT_EQ(reduction.status, ov_msckf::SchurReductionStatus::kAccepted);
  EXPECT_EQ(reduction.stage, ov_msckf::SchurReductionStage::kAccepted);
  EXPECT_EQ(reduction.raw_rows, 8);
  EXPECT_EQ(reduction.degrees_of_freedom, 5);
  ASSERT_EQ(reduction.H_reduced.rows(), 5);
  ASSERT_EQ(reduction.H_reduced.cols(), 24);
  ASSERT_EQ(reduction.residual_reduced.rows(), 5);
  EXPECT_TRUE(reduction.H_reduced.allFinite());
  EXPECT_TRUE(reduction.residual_reduced.allFinite());
  EXPECT_TRUE(reduction.lambda.allFinite());
  EXPECT_TRUE(reduction.eta.allFinite());
  EXPECT_TRUE(std::isfinite(reduction.gamma));
  EXPECT_EQ(reduction.jitter_count, 0U);
  EXPECT_EQ(reduction.clamp_count, 0U);
  EXPECT_EQ(reduction.regularization_count, 0U);
  EXPECT_EQ(reduction.fallback_count, 0U);

  Eigen::MatrixXd compressed_jacobian = reduction.H_reduced;
  Eigen::VectorXd compressed_residual = reduction.residual_reduced;
  UpdaterHelper::measurement_compress_inplace(compressed_jacobian,
                                               compressed_residual);
  ASSERT_EQ(compressed_jacobian.rows(), 5);
  ASSERT_EQ(compressed_jacobian.cols(), 24);
  ASSERT_EQ(compressed_residual.rows(), 5);
  expect_same_bytes(compressed_jacobian, reduction.H_reduced);
  expect_same_bytes(compressed_residual, reduction.residual_reduced);

  std::vector<MSCKFUpdatePreviewBlock> layout;
  Eigen::Index column_offset = 0;
  for (const auto &variable : jacobian_order) {
    layout.push_back(
        {variable->id(), variable->size(), column_offset});
    column_offset += variable->size();
  }
  ASSERT_EQ(column_offset, 24);

  const MSCKFUpdatePreviewSnapshot snapshot =
      UpdaterMSCKFPreview::CaptureSnapshot(fixture.state);
  ASSERT_EQ(snapshot.covariance.rows(), 39);
  ASSERT_EQ(snapshot.state_blocks.size(), 5U);
  const Eigen::MatrixXd measurement_covariance =
      fixture.updater_options.sigma_pix_sq *
      Eigen::MatrixXd::Identity(compressed_residual.rows(),
                                compressed_residual.rows());
  const MSCKFUpdatePreviewSnapshot snapshot_before = snapshot;
  const std::vector<MSCKFUpdatePreviewBlock> layout_before = layout;
  const Eigen::MatrixXd preview_jacobian_before = compressed_jacobian;
  const Eigen::VectorXd preview_residual_before = compressed_residual;
  const Eigen::MatrixXd preview_noise_before = measurement_covariance;

  const ov_msckf::MSCKFUpdatePreviewResult preview =
      UpdaterMSCKFPreview::ComputeFromSnapshot(
          snapshot, layout, compressed_jacobian, compressed_residual,
          measurement_covariance);
  expect_same_bytes(snapshot.covariance, snapshot_before.covariance);
  expect_same_layout(snapshot.state_blocks, snapshot_before.state_blocks);
  expect_same_layout(layout, layout_before);
  expect_same_bytes(compressed_jacobian, preview_jacobian_before);
  expect_same_bytes(compressed_residual, preview_residual_before);
  expect_same_bytes(measurement_covariance, preview_noise_before);
  expect_same_bytes(StateHelper::get_full_covariance(fixture.state), prior);
  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    expect_same_bytes(fixture.state_order[index]->value(), prior_values[index]);
    expect_same_bytes(fixture.state_order[index]->fej(), prior_fej[index]);
  }
  const auto proposal_match =
      UpdaterMSCKFPreview::MatchPrior(fixture.state, entry_prior);
  ASSERT_TRUE(proposal_match.accepted())
      << ov_msckf::msckf_update_prior_match_status_name(proposal_match.status);
  expect_same_feature_observations(*fixture.accepted_feature, accepted_input);
  expect_same_feature_observations(*fixture.rejected_feature, rejected_input);

  ASSERT_TRUE(preview.accepted())
      << ov_msckf::msckf_update_preview_status_name(
             preview.diagnostics.status)
      << "/"
      << ov_msckf::msckf_update_preview_stage_name(preview.diagnostics.stage);
  EXPECT_EQ(preview.diagnostics.status,
            ov_msckf::MSCKFUpdatePreviewStatus::kAccepted);
  EXPECT_EQ(preview.diagnostics.stage,
            ov_msckf::MSCKFUpdatePreviewStage::kAccepted);
  EXPECT_EQ(preview.diagnostics.state_dimension, 39);
  EXPECT_EQ(preview.diagnostics.measurement_dimension, 5);
  EXPECT_EQ(preview.diagnostics.jacobian_dimension, 24);
  EXPECT_EQ(preview.diagnostics.ordered_jacobian_dimension, 24);
  EXPECT_EQ(preview.diagnostics.jitter_count, 0U);
  EXPECT_EQ(preview.diagnostics.repair_count, 0U);
  EXPECT_EQ(preview.diagnostics.alternate_solve_count, 0U);
  EXPECT_EQ(preview.diagnostics.clamp_count, 0U);
  EXPECT_EQ(preview.diagnostics.regularization_count, 0U);
  EXPECT_EQ(preview.diagnostics.fallback_count, 0U);
  ASSERT_TRUE(preview.dx.allFinite());
  ASSERT_TRUE(preview.P_plus.allFinite());

  Eigen::MatrixXd full_jacobian =
      Eigen::MatrixXd::Zero(compressed_jacobian.rows(), prior.rows());
  column_offset = 0;
  for (const auto &variable : jacobian_order) {
    full_jacobian.block(0, variable->id(), full_jacobian.rows(),
                        variable->size()) =
        compressed_jacobian.block(0, column_offset,
                                  compressed_jacobian.rows(),
                                  variable->size());
    column_offset += variable->size();
  }
  const Eigen::MatrixXd cross = prior * full_jacobian.transpose();
  const Eigen::MatrixXd innovation =
      full_jacobian * cross + measurement_covariance;
  Eigen::LDLT<Eigen::MatrixXd> innovation_factor(innovation);
  ASSERT_EQ(innovation_factor.info(), Eigen::Success);
  ASSERT_TRUE(innovation_factor.isPositive());
  const Eigen::VectorXd solved_residual =
      innovation_factor.solve(compressed_residual);
  const Eigen::VectorXd reference_increment = cross * solved_residual;
  const Eigen::MatrixXd reference_covariance_raw =
      prior - cross * innovation_factor.solve(cross.transpose());
  const Eigen::MatrixXd reference_covariance =
      0.5 * (reference_covariance_raw + reference_covariance_raw.transpose());
  const double reference_nis = compressed_residual.dot(solved_residual);
  ASSERT_TRUE(reference_increment.allFinite());
  ASSERT_TRUE(reference_covariance.allFinite());
  ASSERT_TRUE(std::isfinite(reference_nis));

  const Eigen::Map<const Eigen::VectorXd> expected_increment(
      kExpectedIncrement.data(),
      static_cast<Eigen::Index>(kExpectedIncrement.size()));
  EXPECT_LE((reference_increment - expected_increment).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          expected_increment.norm()));
  EXPECT_LE(std::abs(reference_nis - kExpectedNis),
            schurvio_cp1::mixed_tolerance(1.0e-13, 1.0e-10,
                                          std::abs(kExpectedNis)));

  EXPECT_LE((preview.dx - reference_increment).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          reference_increment.norm()));
  EXPECT_LE((preview.P_plus - reference_covariance).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          reference_covariance.norm()));
  EXPECT_GE(reference_nis, 0.0);
  const boost::math::chi_squared distribution(
      static_cast<double>(reduction.degrees_of_freedom));
  const double gate_threshold =
      fixture.updater_options.chi2_multipler *
      boost::math::quantile(distribution, 0.95);
  EXPECT_LE(reference_nis, gate_threshold);

  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    const auto &variable = fixture.state_order[index];
    expected_variables[index]->update(
        reference_increment.segment(variable->id(), variable->size()));
  }

  // The mutation-only seam must commit the exact accepted preview without
  // repeating the Kalman solve and without changing FEJ values.
  Fixture commit_fixture = make_fixture();
  std::vector<Eigen::MatrixXd> commit_prior_fej;
  for (const auto &variable : commit_fixture.state_order) {
    commit_prior_fej.push_back(variable->fej());
  }
  ASSERT_TRUE(StateHelper::CommitPrecomputedUpdate(
      commit_fixture.state, preview.dx, preview.P_plus));
  expect_same_bytes(StateHelper::get_full_covariance(commit_fixture.state),
                    preview.P_plus);
  for (std::size_t index = 0; index < commit_fixture.state_order.size();
       ++index) {
    EXPECT_LE((commit_fixture.state_order[index]->value() -
               expected_variables[index]->value())
                  .norm(),
              schurvio_cp1::mixed_tolerance(
                  1.0e-12, 1.0e-10,
                  expected_variables[index]->value().norm()));
    expect_same_bytes(commit_fixture.state_order[index]->fej(),
                      commit_prior_fej[index]);
  }

  std::vector<std::shared_ptr<Feature>> feature_vector = {
      fixture.accepted_feature, fixture.rejected_feature};
  const Eigen::MatrixXd camera_cache_before =
      fixture.camera->get_value();
  std::size_t proposal_observation_count = 0U;
  fixture.camera->inspection = [&]() {
    ++proposal_observation_count;
    expect_same_bytes(StateHelper::get_full_covariance(fixture.state), prior);
    for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
      expect_same_bytes(fixture.state_order[index]->value(),
                        prior_values[index]);
      expect_same_bytes(fixture.state_order[index]->fej(), prior_fej[index]);
    }
    expect_same_feature_observations(*fixture.accepted_feature,
                                     accepted_input);
    expect_same_feature_observations(*fixture.rejected_feature,
                                     rejected_input);
    expect_same_bytes(fixture.camera->get_value(), camera_cache_before);
    EXPECT_EQ(feature_vector.size(), 2U);
    if (feature_vector.size() == 2U) {
      EXPECT_EQ(feature_vector[0], fixture.accepted_feature);
      EXPECT_EQ(feature_vector[1], fixture.rejected_feature);
    }
  };
  UpdaterMSCKF updater(fixture.updater_options,
                       fixture.initializer_options);
  updater.update(fixture.state, feature_vector);
  fixture.camera->inspection = {};

  EXPECT_GT(proposal_observation_count, 0U);
  ASSERT_EQ(feature_vector.size(), 1U);
  EXPECT_EQ(feature_vector.front(), fixture.accepted_feature);
  EXPECT_EQ(feature_vector.front()->featid, kAcceptedFeatureId);
  EXPECT_TRUE(feature_vector.front()->to_delete);
  EXPECT_TRUE(fixture.rejected_feature->to_delete);
  EXPECT_EQ(fixture.rejected_feature->featid, kRejectedFeatureId);
  EXPECT_LE((fixture.accepted_feature->p_FinG - expected_feature->p_FinG).norm(),
            1.0e-12);
  EXPECT_LE((fixture.accepted_feature->p_FinA - expected_feature->p_FinA).norm(),
            1.0e-12);

  const Eigen::MatrixXd posterior =
      StateHelper::get_full_covariance(fixture.state);
  ASSERT_TRUE(posterior.allFinite());
  EXPECT_GT((posterior - prior).norm(), 0.0);
  EXPECT_LE((posterior - preview.P_plus).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          preview.P_plus.norm()));
  EXPECT_LE((posterior - reference_covariance).norm(),
            schurvio_cp1::mixed_tolerance(1.0e-12, 1.0e-10,
                                          reference_covariance.norm()));
  EXPECT_LE(std::abs(posterior.trace() - kExpectedCovarianceTrace),
            schurvio_cp1::mixed_tolerance(
                1.0e-13, 1.0e-10, std::abs(kExpectedCovarianceTrace)));
  EXPECT_LE(std::abs(posterior.squaredNorm() -
                     kExpectedCovarianceSquaredNorm),
            schurvio_cp1::mixed_tolerance(
                1.0e-14, 1.0e-10,
                std::abs(kExpectedCovarianceSquaredNorm)));

  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "state_block=" << index
                                      << " covariance_id="
                                      << fixture.state_order[index]->id());
    const double value_error =
        (fixture.state_order[index]->value() -
         expected_variables[index]->value())
            .norm();
    EXPECT_LE(value_error,
              schurvio_cp1::mixed_tolerance(
                  1.0e-12, 1.0e-10,
                  expected_variables[index]->value().norm()))
        << "actual=" << fixture.state_order[index]->value().transpose()
        << " expected=" << expected_variables[index]->value().transpose();
    expect_same_bytes(fixture.state_order[index]->fej(), prior_fej[index]);
  }

  const double symmetry_error = schurvio_cp1::matrix_inf_norm(
      posterior - posterior.transpose());
  const double covariance_scale =
      std::max(1.0, schurvio_cp1::matrix_inf_norm(posterior));
  EXPECT_LE(symmetry_error, 1.0e-10 * covariance_scale);
  const Eigen::MatrixXd posterior_symmetric =
      0.5 * (posterior + posterior.transpose());
  Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> posterior_eigensolver(
      posterior_symmetric);
  ASSERT_EQ(posterior_eigensolver.info(), Eigen::Success);
  const double largest_eigenvalue =
      posterior_eigensolver.eigenvalues().maxCoeff();
  const double eigenvalue_scale = std::max(1.0, largest_eigenvalue);
  EXPECT_GE(posterior_eigensolver.eigenvalues().minCoeff(),
            -1.0e-10 * eigenvalue_scale);

  std::cout << std::setprecision(17)
            << "CP1_ONE_PASS feature=" << kAcceptedFeatureId
            << " rejected_feature=" << kRejectedFeatureId
            << " raw_rows=" << reduction.raw_rows
            << " reduced_rows=" << reduction.degrees_of_freedom
            << " state_dimension=" << posterior.rows()
            << " nis=" << reference_nis
            << " dx_norm=" << reference_increment.norm()
            << " covariance_trace=" << posterior.trace()
            << " covariance_squared_norm=" << posterior.squaredNorm()
            << std::endl;
}

TEST(CP1OnePassRegression,
     PriorMismatchAndInvalidPrecomputedCommitHaveZeroWrites) {
  Fixture fixture = make_fixture();
  const MSCKFUpdatePriorSnapshot snapshot =
      UpdaterMSCKFPreview::CapturePrior(fixture.state);
  const Eigen::MatrixXd covariance_before =
      StateHelper::get_full_covariance(fixture.state);
  std::vector<Eigen::MatrixXd> values_before;
  std::vector<Eigen::MatrixXd> fej_before;
  for (const auto &variable : fixture.state_order) {
    values_before.push_back(variable->value());
    fej_before.push_back(variable->fej());
  }

  Eigen::VectorXd invalid_dx =
      Eigen::VectorXd::Zero(covariance_before.rows() - 1);
  EXPECT_FALSE(StateHelper::CommitPrecomputedUpdate(
      fixture.state, invalid_dx, covariance_before));
  expect_same_bytes(StateHelper::get_full_covariance(fixture.state),
                    covariance_before);
  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    expect_same_bytes(fixture.state_order[index]->value(), values_before[index]);
    expect_same_bytes(fixture.state_order[index]->fej(), fej_before[index]);
  }

  Eigen::MatrixXd changed_imu = fixture.state->_imu->value();
  changed_imu(7) += 1.0e-6;
  fixture.state->_imu->set_value(changed_imu);
  const auto mismatch =
      UpdaterMSCKFPreview::MatchPrior(fixture.state, snapshot);
  EXPECT_FALSE(mismatch.accepted());
  EXPECT_EQ(mismatch.status, MSCKFUpdatePriorMatchStatus::kNominal);
  expect_same_bytes(StateHelper::get_full_covariance(fixture.state),
                    covariance_before);
  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    expect_same_bytes(fixture.state_order[index]->fej(), fej_before[index]);
  }

  // PoseJPL exposes child storage that the visual Jacobian reads directly.
  // A child-only mutation must not hide behind an unchanged parent cache.
  Fixture nested_fixture = make_fixture();
  const MSCKFUpdatePriorSnapshot nested_snapshot =
      UpdaterMSCKFPreview::CapturePrior(nested_fixture.state);
  const auto nested_clone = nested_fixture.state->_clones_IMU.begin()->second;
  const Eigen::MatrixXd nested_parent_before = nested_clone->value();
  Eigen::MatrixXd changed_position = nested_clone->p()->value();
  changed_position(0) += 1.0e-6;
  nested_clone->p()->set_value(changed_position);
  expect_same_bytes(nested_clone->value(), nested_parent_before);
  const auto nested_mismatch =
      UpdaterMSCKFPreview::MatchPrior(nested_fixture.state, nested_snapshot);
  EXPECT_FALSE(nested_mismatch.accepted());
  EXPECT_EQ(nested_mismatch.status, MSCKFUpdatePriorMatchStatus::kNominal);
}

TEST(CP1OnePassRegression,
     CleaningOnlyRejectionPreservesZeroStateCommitDisposition) {
  Fixture fixture = make_fixture();
  const Eigen::MatrixXd covariance_before =
      StateHelper::get_full_covariance(fixture.state);
  std::vector<Eigen::MatrixXd> values_before;
  std::vector<Eigen::MatrixXd> fej_before;
  for (const auto &variable : fixture.state_order) {
    values_before.push_back(variable->value());
    fej_before.push_back(variable->fej());
  }

  std::vector<std::shared_ptr<Feature>> features = {
      fixture.rejected_feature};
  UpdaterMSCKF updater(fixture.updater_options,
                       fixture.initializer_options);
  updater.update(fixture.state, features);

  EXPECT_TRUE(features.empty());
  EXPECT_TRUE(fixture.rejected_feature->to_delete);
  expect_same_bytes(StateHelper::get_full_covariance(fixture.state),
                    covariance_before);
  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    expect_same_bytes(fixture.state_order[index]->value(), values_before[index]);
    expect_same_bytes(fixture.state_order[index]->fej(), fej_before[index]);
  }
}

TEST(CP1OnePassRegression,
     UpdaterNestedPoseMismatchRollsBackCallerVectorAndSkipsCommit) {
  Fixture fixture = make_fixture();
  const Eigen::MatrixXd covariance_before =
      StateHelper::get_full_covariance(fixture.state);
  std::vector<Eigen::MatrixXd> values_before;
  std::vector<Eigen::MatrixXd> fej_before;
  for (const auto &variable : fixture.state_order) {
    values_before.push_back(variable->value());
    fej_before.push_back(variable->fej());
  }
  const Feature accepted_before = *fixture.accepted_feature;
  const Feature rejected_before = *fixture.rejected_feature;
  std::vector<std::shared_ptr<Feature>> features = {
      fixture.accepted_feature, fixture.rejected_feature};

  const auto changed_clone = fixture.state->_clones_IMU.begin()->second;
  Eigen::MatrixXd changed_clone_position = changed_clone->p()->value();
  changed_clone_position(0) += 0.25;
  bool changed_nested_pose = false;
  fixture.camera->inspection = [&]() {
    if (!changed_nested_pose) {
      changed_clone->p()->set_value(changed_clone_position);
      changed_nested_pose = true;
    }
  };
  UpdaterMSCKF updater(fixture.updater_options,
                       fixture.initializer_options);
  updater.update(fixture.state, features);
  fixture.camera->inspection = {};

  EXPECT_TRUE(changed_nested_pose);
  EXPECT_TRUE(features.empty());
  expect_same_bytes(changed_clone->p()->value(), changed_clone_position);
  expect_same_feature_observations(*fixture.accepted_feature,
                                   accepted_before);
  expect_same_feature_observations(*fixture.rejected_feature,
                                   rejected_before);
  expect_same_bytes(StateHelper::get_full_covariance(fixture.state),
                    covariance_before);
  for (std::size_t index = 0; index < fixture.state_order.size(); ++index) {
    expect_same_bytes(fixture.state_order[index]->value(),
                      values_before[index]);
    expect_same_bytes(fixture.state_order[index]->fej(),
                      fej_before[index]);
  }
}

} // namespace
