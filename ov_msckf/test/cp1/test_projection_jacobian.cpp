/*
 * SchurVIO-Lite CP1 OpenVINS projection/JPL retraction tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cp1_fixture_utils.h"

#include <gtest/gtest.h>

#include "cam/CamRadtan.h"
#include "state/State.h"
#include "types/LandmarkRepresentation.h"
#include "types/PoseJPL.h"
#include "update/UpdaterHelper.h"
#include "utils/quat_ops.h"

#include <Eigen/Dense>

#include <algorithm>
#include <iostream>
#include <memory>
#include <vector>

namespace {

Eigen::Matrix3d random_rotation(schurvio_cp1::DeterministicRng &rng, double maximum_angle) {
  Eigen::Vector3d axis = rng.vector(3);
  if (axis.norm() < 1.0e-6) {
    axis = Eigen::Vector3d::UnitX();
  }
  axis.normalize();
  return ov_core::exp_so3(axis * rng.symmetric(maximum_angle));
}

Eigen::Matrix<double, 7, 1> pose_value(const Eigen::Matrix3d &rotation, const Eigen::Vector3d &position) {
  Eigen::Matrix<double, 7, 1> value;
  value.head<4>() = ov_core::rot_2_quat(rotation);
  value.tail<3>() = position;
  return value;
}

Eigen::Vector2d project_radtan_double(const Eigen::Vector3d &point_camera, const Eigen::Matrix<double, 8, 1> &intrinsics) {
  const double x = point_camera(0) / point_camera(2);
  const double y = point_camera(1) / point_camera(2);
  const double radius_squared = x * x + y * y;
  const double radius_fourth = radius_squared * radius_squared;
  const double radial = 1.0 + intrinsics(4) * radius_squared + intrinsics(5) * radius_fourth;
  const double distorted_x =
      x * radial + 2.0 * intrinsics(6) * x * y + intrinsics(7) * (radius_squared + 2.0 * x * x);
  const double distorted_y =
      y * radial + intrinsics(6) * (radius_squared + 2.0 * y * y) + 2.0 * intrinsics(7) * x * y;
  return Eigen::Vector2d(intrinsics(0) * distorted_x + intrinsics(2), intrinsics(1) * distorted_y + intrinsics(3));
}

Eigen::Vector2d project_feature(const std::shared_ptr<ov_msckf::State> &state, const Eigen::Vector3d &feature_global,
                                const Eigen::Matrix<double, 8, 1> &intrinsics, double timestamp) {
  const auto &clone = state->_clones_IMU.at(timestamp);
  const auto &calibration = state->_calib_IMUtoCAM.at(0);
  const Eigen::Vector3d point_imu = clone->Rot() * (feature_global - clone->pos());
  const Eigen::Vector3d point_camera = calibration->Rot() * point_imu + calibration->pos();
  return project_radtan_double(point_camera, intrinsics);
}

Eigen::Vector2d project_feature_runtime(const std::shared_ptr<ov_msckf::State> &state,
                                        const Eigen::Vector3d &feature_global, double timestamp) {
  const auto &clone = state->_clones_IMU.at(timestamp);
  const auto &calibration = state->_calib_IMUtoCAM.at(0);
  const Eigen::Vector3d point_imu = clone->Rot() * (feature_global - clone->pos());
  const Eigen::Vector3d point_camera = calibration->Rot() * point_imu + calibration->pos();
  const Eigen::Vector2d normalized(point_camera(0) / point_camera(2), point_camera(1) / point_camera(2));
  return state->_cam_intrinsics_cameras.at(0)->distort_d(normalized);
}

double normalized_frobenius_error(const Eigen::MatrixXd &analytic, const Eigen::MatrixXd &finite_difference) {
  return (analytic - finite_difference).norm() / std::max({1.0, analytic.norm(), finite_difference.norm()});
}

Eigen::Vector4d openvins_delta_quaternion(const Eigen::Vector3d &increment) {
  Eigen::Vector4d delta;
  delta << 0.5 * increment, 1.0;
  return ov_core::quatnorm(delta);
}

Eigen::Vector4d retract_orientation(const Eigen::Vector4d &prior, const Eigen::Vector3d &absolute_increment) {
  return ov_core::quat_multiply(openvins_delta_quaternion(absolute_increment), prior);
}

Eigen::Vector3d local_increment_between(const Eigen::Vector4d &updated, const Eigen::Vector4d &current) {
  const Eigen::Vector4d relative = ov_core::quat_multiply(updated, ov_core::Inv(current));
  return 2.0 * relative.head<3>() / relative(3);
}

TEST(CP1Projection, ActualOpenVINSJacobiansMatchAllDoubleFiniteDifferences) {
  schurvio_cp1::DeterministicRng rng(schurvio_cp1::kMasterSeed ^ 0x70726f6aULL);
  double worst_state_error = 0.0;
  double worst_landmark_error = 0.0;
  double worst_residual_definition_error = 0.0;
  double worst_residual_sign_error = 0.0;
  constexpr double epsilon = 1.0e-6;

  for (int fixture = 0; fixture < 256; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    ov_msckf::StateOptions options;
    options.do_fej = false;
    options.do_calib_camera_pose = false;
    options.do_calib_camera_intrinsics = false;
    options.num_cameras = 1;
    auto state = std::make_shared<ov_msckf::State>(options);

    Eigen::Matrix<double, 8, 1> intrinsics;
    intrinsics << 458.0 + rng.symmetric(5.0), 457.0 + rng.symmetric(5.0), 367.0 + rng.symmetric(3.0),
        248.0 + rng.symmetric(3.0), -0.28 + rng.symmetric(0.02), 0.074 + rng.symmetric(0.01),
        rng.symmetric(0.001), rng.symmetric(0.001);
    state->_cam_intrinsics.at(0)->set_value(intrinsics);
    state->_cam_intrinsics.at(0)->set_fej(intrinsics);
    auto camera = std::make_shared<ov_core::CamRadtan>(752, 480);
    camera->set_value(intrinsics);
    state->_cam_intrinsics_cameras[0] = camera;

    const Eigen::Matrix3d rotation_imu_to_camera = random_rotation(rng, 0.25);
    const Eigen::Vector3d position_imu_in_camera = rng.vector(3, 0.05);
    const Eigen::Matrix<double, 7, 1> calibration_value =
        pose_value(rotation_imu_to_camera, position_imu_in_camera);
    state->_calib_IMUtoCAM.at(0)->set_value(calibration_value);
    state->_calib_IMUtoCAM.at(0)->set_fej(calibration_value);

    constexpr double timestamp = 1.0;
    auto clone = std::make_shared<ov_type::PoseJPL>();
    const Eigen::Matrix3d rotation_global_to_imu = random_rotation(rng, 1.2);
    const Eigen::Vector3d position_imu_in_global = rng.vector(3, 2.0);
    const Eigen::Matrix<double, 7, 1> clone_value = pose_value(rotation_global_to_imu, position_imu_in_global);
    clone->set_value(clone_value);
    clone->set_fej(clone_value);
    state->_clones_IMU[timestamp] = clone;

    const double depth = 3.0 + 7.0 * rng.uniform();
    const Eigen::Vector3d point_camera(rng.symmetric(0.55) * depth, rng.symmetric(0.45) * depth, depth);
    const Eigen::Vector3d point_imu = rotation_imu_to_camera.transpose() * (point_camera - position_imu_in_camera);
    const Eigen::Vector3d feature_global =
        position_imu_in_global + rotation_global_to_imu.transpose() * point_imu;

    ov_msckf::UpdaterHelper::UpdaterHelperFeature feature;
    feature.featid = static_cast<std::size_t>(fixture);
    feature.feat_representation = ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
    feature.p_FinG = feature_global;
    feature.p_FinG_fej = feature_global;
    feature.timestamps[0].push_back(timestamp);
    Eigen::VectorXf measured(2);
    measured = project_feature(state, feature_global, intrinsics, timestamp).cast<float>();
    feature.uvs[0].push_back(measured);
    feature.uvs_norm[0].push_back(Eigen::Vector2f(point_camera(0) / depth, point_camera(1) / depth));

    Eigen::MatrixXd landmark_jacobian;
    Eigen::MatrixXd state_jacobian;
    Eigen::VectorXd residual;
    std::vector<std::shared_ptr<ov_type::Type>> state_order;
    ov_msckf::UpdaterHelper::get_feature_jacobian_full(state, feature, landmark_jacobian, state_jacobian, residual, state_order);
    ASSERT_EQ(state_order.size(), 1U);
    EXPECT_EQ(state_order.front(), clone);
    ASSERT_EQ(state_jacobian.rows(), 2);
    ASSERT_EQ(state_jacobian.cols(), 6);
    ASSERT_EQ(landmark_jacobian.rows(), 2);
    ASSERT_EQ(landmark_jacobian.cols(), 3);

    const Eigen::Vector2d measured_double = measured.cast<double>();
    const Eigen::Vector2d expected_residual = measured_double - project_feature_runtime(state, feature_global, timestamp);
    const double residual_definition_error = (residual - expected_residual).norm();
    worst_residual_definition_error = std::max(worst_residual_definition_error, residual_definition_error);
    EXPECT_LE(residual_definition_error, 1.0e-12);

    Eigen::Matrix<double, 2, 6> state_finite_difference;
    Eigen::Matrix<double, 2, 6> state_residual_finite_difference;
    for (int column = 0; column < 6; ++column) {
      Eigen::VectorXd increment = Eigen::VectorXd::Zero(6);
      increment(column) = epsilon;
      clone->set_value(clone_value);
      clone->update(increment);
      const Eigen::Vector2d plus = project_feature(state, feature_global, intrinsics, timestamp);
      clone->set_value(clone_value);
      clone->update(-increment);
      const Eigen::Vector2d minus = project_feature(state, feature_global, intrinsics, timestamp);
      state_finite_difference.col(column) = (plus - minus) / (2.0 * epsilon);
      state_residual_finite_difference.col(column) =
          ((measured_double - plus) - (measured_double - minus)) / (2.0 * epsilon);
    }
    clone->set_value(clone_value);

    Eigen::Matrix<double, 2, 3> landmark_finite_difference;
    Eigen::Matrix<double, 2, 3> landmark_residual_finite_difference;
    for (int column = 0; column < 3; ++column) {
      Eigen::Vector3d increment = Eigen::Vector3d::Zero();
      increment(column) = epsilon;
      const Eigen::Vector2d plus = project_feature(state, feature_global + increment, intrinsics, timestamp);
      const Eigen::Vector2d minus = project_feature(state, feature_global - increment, intrinsics, timestamp);
      landmark_finite_difference.col(column) = (plus - minus) / (2.0 * epsilon);
      landmark_residual_finite_difference.col(column) =
          ((measured_double - plus) - (measured_double - minus)) / (2.0 * epsilon);
    }

    const double state_error = normalized_frobenius_error(state_jacobian, state_finite_difference);
    const double landmark_error = normalized_frobenius_error(landmark_jacobian, landmark_finite_difference);
    const double state_residual_sign_error =
        normalized_frobenius_error(-state_jacobian, state_residual_finite_difference);
    const double landmark_residual_sign_error =
        normalized_frobenius_error(-landmark_jacobian, landmark_residual_finite_difference);
    worst_state_error = std::max(worst_state_error, state_error);
    worst_landmark_error = std::max(worst_landmark_error, landmark_error);
    worst_residual_sign_error =
        std::max({worst_residual_sign_error, state_residual_sign_error, landmark_residual_sign_error});
    EXPECT_LE(state_error, 1.0e-5);
    EXPECT_LE(landmark_error, 1.0e-5);
    EXPECT_LE(state_residual_sign_error, 1.0e-5);
    EXPECT_LE(landmark_residual_sign_error, 1.0e-5);
    EXPECT_TRUE(residual.allFinite());
  }

  std::cout << "CP1_PROJECTION fixtures=256 seed=" << (schurvio_cp1::kMasterSeed ^ 0x70726f6aULL)
            << " max_state_normalized_frobenius=" << worst_state_error
            << " max_landmark_normalized_frobenius=" << worst_landmark_error
            << " max_residual_definition_error=" << worst_residual_definition_error
            << " max_residual_sign_normalized_frobenius=" << worst_residual_sign_error << std::endl;
}

TEST(CP1Retraction, FixedPriorChartDifferentialMatchesExactJPLRetraction) {
  schurvio_cp1::DeterministicRng rng(schurvio_cp1::kMasterSeed ^ 0x72657472616374ULL);
  constexpr double epsilon = 2.0e-7;
  double worst_error = 0.0;

  for (int fixture = 0; fixture < 256; ++fixture) {
    SCOPED_TRACE(::testing::Message() << "fixture=" << fixture);
    const Eigen::Vector4d prior = ov_core::rot_2_quat(random_rotation(rng, 1.5));
    Eigen::Vector3d absolute_increment = rng.vector(3, 0.35);
    if (absolute_increment.norm() > 1.2) {
      absolute_increment *= 1.2 / absolute_increment.norm();
    }
    const Eigen::Vector4d current = retract_orientation(prior, absolute_increment);
    Eigen::Matrix3d finite_difference;
    for (int column = 0; column < 3; ++column) {
      Eigen::Vector3d perturbation = Eigen::Vector3d::Zero();
      perturbation(column) = epsilon;
      const Eigen::Vector3d plus =
          local_increment_between(retract_orientation(prior, absolute_increment + perturbation), current);
      const Eigen::Vector3d minus =
          local_increment_between(retract_orientation(prior, absolute_increment - perturbation), current);
      finite_difference.col(column) = (plus - minus) / (2.0 * epsilon);
    }
    const Eigen::Matrix3d analytic = schurvio_cp1::fixed_chart_orientation_map(absolute_increment);
    const double error = normalized_frobenius_error(analytic, finite_difference);
    worst_error = std::max(worst_error, error);
    EXPECT_LE(error, 1.0e-7);
  }

  std::cout << "CP1_RETRACTION fixtures=256 seed=" << (schurvio_cp1::kMasterSeed ^ 0x72657472616374ULL)
            << " max_normalized_frobenius=" << worst_error << std::endl;
}

} // namespace
