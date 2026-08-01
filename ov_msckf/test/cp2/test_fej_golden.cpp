/*
 * SchurVIO-Lite CP2 FEJ production-path golden fixture.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <gtest/gtest.h>

#include "cam/CamRadtan.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/LandmarkRepresentation.h"
#include "types/PoseJPL.h"
#include "update/UpdaterHelper.h"

#include <Eigen/Dense>

#include <memory>
#include <string>
#include <vector>

namespace {

Eigen::Matrix<double, 7, 1> pose_value(double qx, double qy, double qz, double qw,
                                       double px, double py, double pz) {
  Eigen::Matrix<double, 7, 1> value;
  value << qx, qy, qz, qw, px, py, pz;
  return value;
}

struct FejGoldenFixture {
  std::shared_ptr<ov_msckf::State> state;
  ov_msckf::UpdaterHelper::UpdaterHelperFeature feature;
  std::vector<std::shared_ptr<ov_type::PoseJPL>> clones;
  std::vector<double> timestamps;
};

FejGoldenFixture make_fixture(bool clone_fej_matches_current = false) {
  ov_msckf::StateOptions options;
  options.do_fej = true;
  options.do_calib_camera_pose = false;
  options.do_calib_camera_intrinsics = false;
  options.num_cameras = 1;

  FejGoldenFixture fixture;
  fixture.state = std::make_shared<ov_msckf::State>(options);

  Eigen::Matrix<double, 8, 1> intrinsics;
  intrinsics << 420.5, 418.25, 320.25, 239.75, -0.12, 0.025, 0.0015, -0.0008;
  fixture.state->_cam_intrinsics.at(0)->set_value(intrinsics);
  fixture.state->_cam_intrinsics.at(0)->set_fej(intrinsics);
  auto camera = std::make_shared<ov_core::CamRadtan>(640, 480);
  camera->set_value(intrinsics);
  fixture.state->_cam_intrinsics_cameras[0] = camera;

  const Eigen::Matrix<double, 7, 1> calibration =
      pose_value(0.0, 0.0, 0.0, 1.0, 0.03, -0.02, 0.015);
  fixture.state->_calib_IMUtoCAM.at(0)->set_value(calibration);
  fixture.state->_calib_IMUtoCAM.at(0)->set_fej(calibration);

  fixture.timestamps = {10.0, 20.0, 30.0};
  const std::vector<Eigen::Matrix<double, 7, 1>> current_values{
      pose_value(0.0, 0.0, 5.0 / 13.0, 12.0 / 13.0, 0.20, -0.10, 0.05),
      pose_value(0.0, 0.0, -8.0 / 17.0, 15.0 / 17.0, -0.15, 0.25, -0.02),
      pose_value(0.0, 0.0, 7.0 / 25.0, 24.0 / 25.0, 0.35, 0.18, 0.08),
  };
  const std::vector<Eigen::Matrix<double, 7, 1>> fej_values{
      pose_value(0.0, 0.0, 8.0 / 17.0, 15.0 / 17.0, 0.05, -0.18, 0.02),
      pose_value(0.0, 0.0, -5.0 / 13.0, 12.0 / 13.0, -0.04, 0.12, 0.04),
      pose_value(0.0, 0.0, 9.0 / 41.0, 40.0 / 41.0, 0.22, 0.29, -0.01),
  };

  for (std::size_t i = 0; i < fixture.timestamps.size(); ++i) {
    Eigen::Matrix<double, 16, 1> imu_value = fixture.state->_imu->value();
    Eigen::Matrix<double, 16, 1> imu_fej = fixture.state->_imu->fej();
    imu_value.head<7>() = current_values.at(i);
    imu_fej.head<7>() =
        clone_fej_matches_current ? current_values.at(i) : fej_values.at(i);
    fixture.state->_imu->set_value(imu_value);
    fixture.state->_imu->set_fej(imu_fej);
    fixture.state->_timestamp = fixture.timestamps.at(i);

    // Use the production augmentation path. This copies the IMU pose and its
    // covariance, assigns the clone's active-state ID, appends it to
    // State::_variables, and installs the identical object in _clones_IMU.
    ov_msckf::StateHelper::augment_clone(fixture.state, Eigen::Vector3d::Zero());
    const auto clone = fixture.state->_clones_IMU.at(fixture.timestamps.at(i));
    fixture.clones.push_back(clone);
  }

  fixture.feature.featid = 0x43503246454aULL;
  fixture.feature.feat_representation =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  fixture.feature.p_FinG << 1.10, -0.50, 5.20;
  // UpdaterMSCKF's GLOBAL_3D preparation copies the same triangulated point
  // into both fields. FEJ affects only clone linearization in this fixture.
  fixture.feature.p_FinG_fej = fixture.feature.p_FinG;
  fixture.feature.timestamps[0] = fixture.timestamps;

  const std::vector<Eigen::Vector2f> measurements{
      Eigen::Vector2f(384.125F, 180.75F),
      Eigen::Vector2f(300.5F, 230.25F),
      Eigen::Vector2f(410.75F, 260.125F),
  };
  for (const auto &measurement : measurements) {
    fixture.feature.uvs[0].push_back(measurement);
    fixture.feature.uvs_norm[0].push_back(Eigen::Vector2f::Zero());
  }
  return fixture;
}

void compute(const FejGoldenFixture &fixture, Eigen::MatrixXd &H_f, Eigen::MatrixXd &H_x,
             Eigen::VectorXd &residual,
             std::vector<std::shared_ptr<ov_type::Type>> &Hx_order) {
  auto feature = fixture.feature;
  ov_msckf::UpdaterHelper::get_feature_jacobian_full(fixture.state, feature, H_f, H_x,
                                                     residual, Hx_order);
}

void expect_literal_matrix(const Eigen::MatrixXd &actual, const Eigen::MatrixXd &expected,
                           double absolute_tolerance, const std::string &label) {
  ASSERT_EQ(actual.rows(), expected.rows()) << label;
  ASSERT_EQ(actual.cols(), expected.cols()) << label;
  for (Eigen::Index row = 0; row < actual.rows(); ++row) {
    for (Eigen::Index col = 0; col < actual.cols(); ++col) {
      SCOPED_TRACE(::testing::Message() << label << " row=" << row << " col=" << col);
      EXPECT_NEAR(actual(row, col), expected(row, col), absolute_tolerance);
    }
  }
}

TEST(CP2FejGolden, MixedCurrentAndFirstEstimateProductionJacobianIsFrozen) {
  const FejGoldenFixture fixture = make_fixture();
  ASSERT_TRUE(fixture.state->_options.do_fej);
  ASSERT_EQ(fixture.feature.feat_representation,
            ov_type::LandmarkRepresentation::Representation::GLOBAL_3D);

  // These guards make FEJ materially observable. The global landmark point is
  // intentionally identical, matching UpdaterMSCKF; only clone current and
  // first-estimate poses differ.
  ASSERT_EQ((fixture.feature.p_FinG - fixture.feature.p_FinG_fej).norm(), 0.0);
  ASSERT_EQ(fixture.clones.size(), 3U);
  ASSERT_EQ(fixture.timestamps.size(), fixture.clones.size());
  ASSERT_EQ(fixture.state->_imu->id(), 0);
  ASSERT_EQ(fixture.state->_imu->size(), 15);
  for (std::size_t index = 0; index < fixture.clones.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "clone=" << index);
    EXPECT_EQ(fixture.state->_clones_IMU.at(fixture.timestamps.at(index)),
              fixture.clones.at(index));
    EXPECT_EQ(fixture.clones.at(index)->id(), 15 + 6 * static_cast<int>(index));
    EXPECT_EQ(fixture.clones.at(index)->size(), 6);
    EXPECT_GT((fixture.clones.at(index)->value() - fixture.clones.at(index)->fej()).norm(),
              0.1);
  }
  ASSERT_EQ(fixture.state->max_covariance_size(), 15 + 3 * 6);

  // Contiguous IDs plus equality between the full covariance and the marginal
  // requested in this order protect active State::_variables membership and
  // ordering without exposing State's private storage to the test.
  std::vector<std::shared_ptr<ov_type::Type>> expected_active_order{fixture.state->_imu};
  expected_active_order.insert(expected_active_order.end(), fixture.clones.begin(),
                               fixture.clones.end());
  const Eigen::MatrixXd full_covariance =
      ov_msckf::StateHelper::get_full_covariance(fixture.state);
  const Eigen::MatrixXd ordered_covariance =
      ov_msckf::StateHelper::get_marginal_covariance(fixture.state, expected_active_order);
  EXPECT_EQ(full_covariance.rows(), 33);
  EXPECT_EQ(full_covariance.cols(), 33);
  EXPECT_EQ((full_covariance - ordered_covariance).cwiseAbs().maxCoeff(), 0.0);

  Eigen::MatrixXd H_f;
  Eigen::MatrixXd H_x;
  Eigen::VectorXd residual;
  std::vector<std::shared_ptr<ov_type::Type>> Hx_order;
  compute(fixture, H_f, H_x, residual, Hx_order);

  // Literal oracle captured from the reviewed production seam, not recomputed
  // from a duplicate projection/Jacobian implementation in this test.
  Eigen::Matrix<double, 6, 1> expected_residual;
  expected_residual << 33.137451171875, 16.751983642578125, -127.0726318359375,
      -57.052642822265625, 66.341522216796875, 101.07041931152344;

  Eigen::Matrix<double, 6, 3> expected_H_f;
  expected_H_f <<
      44.540821697862938, 66.930036330461689, -5.3428617761429313,
      -65.746621828079668, 44.46118715784916, 16.331092530017774,
      55.383946891887582, -56.572593355761505, -19.439214322543766,
      56.352807673080747, 56.596571510263786, -5.3217030566805894,
      72.140116036554389, 34.472928572994476, -7.3958365954127938,
      -33.524412962349295, 71.31223919519212, 16.728538166932204;

  Eigen::Matrix<double, 6, 18> expected_H_x;
  expected_H_x <<
      -4.0709454747393545, -418.15382229917964, -84.529601090300943,
      -44.540821697862938, -66.930036330461689, 5.3428617761429313,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      428.27942673800237, 3.6808411191624408, -25.645327530756123,
      65.746621828079668, -44.46118715784916, -16.331092530017774,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      4.6210886265780706, -432.66928944364327, 30.154709352597823,
      -55.383946891887582, 56.572593355761505, 19.439214322543766,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      414.09225448500581, -4.0009342515355062, -99.458832279010778,
      -56.352807673080747, -56.596571510263786, 5.3217030566805894,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      -6.7547122062136697, -419.9348110728717, -87.326868813113094,
      -72.140116036554389, -34.472928572994476, 7.3958365954127938,
      0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
      428.78925677631867, 6.3364522903322884, -36.270484251513125,
      33.524412962349295, -71.31223919519212, -16.728538166932204;

  expect_literal_matrix(residual, expected_residual, 0.0, "residual");
  expect_literal_matrix(H_f, expected_H_f, 1.0e-12, "H_f");
  expect_literal_matrix(H_x, expected_H_x, 1.0e-12, "H_x");

  // Hx_order is part of the contract: one six-state clone block per
  // observation, in measurement order, with pointer identity preserved.
  ASSERT_EQ(Hx_order.size(), fixture.clones.size());
  for (std::size_t index = 0; index < fixture.clones.size(); ++index) {
    EXPECT_EQ(Hx_order.at(index), fixture.clones.at(index));
    EXPECT_EQ(Hx_order.at(index)->id(), 15 + 6 * static_cast<int>(index));
    EXPECT_EQ(Hx_order.at(index)->size(), 6);
  }

  // Control: keep FEJ enabled but make every clone's FEJ pose equal its
  // current pose. Runtime residuals stay unchanged, while both Jacobians must
  // move substantially. This catches a fixture that has become insensitive
  // to the clone current/FEJ split.
  const FejGoldenFixture matched_fej_fixture = make_fixture(true);
  Eigen::MatrixXd matched_H_f;
  Eigen::MatrixXd matched_H_x;
  Eigen::VectorXd matched_residual;
  std::vector<std::shared_ptr<ov_type::Type>> matched_order;
  compute(matched_fej_fixture, matched_H_f, matched_H_x, matched_residual, matched_order);
  ASSERT_EQ(matched_order.size(), matched_fej_fixture.clones.size());
  for (std::size_t index = 0; index < matched_fej_fixture.clones.size(); ++index) {
    EXPECT_EQ((matched_fej_fixture.clones.at(index)->value() -
               matched_fej_fixture.clones.at(index)->fej())
                  .norm(),
              0.0);
  }
  EXPECT_EQ((residual - matched_residual).cwiseAbs().maxCoeff(), 0.0);
  EXPECT_GT((H_f - matched_H_f).norm(), 10.0);
  EXPECT_GT((H_x - matched_H_x).norm(), 10.0);

  // StateHelper::marginalize checks pointer membership in State::_variables.
  // Removing the exact golden clones in reverse order therefore directly
  // proves their active membership while also protecting their append order.
  for (std::size_t remaining = fixture.clones.size(); remaining > 0; --remaining) {
    const std::size_t index = remaining - 1;
    const auto &clone = fixture.clones.at(index);
    ASSERT_EQ(clone->id(), 15 + 6 * static_cast<int>(index));
    ov_msckf::StateHelper::marginalize(fixture.state, clone);
    fixture.state->_clones_IMU.erase(fixture.timestamps.at(index));
    EXPECT_EQ(clone->id(), -1);
    EXPECT_EQ(fixture.state->max_covariance_size(), 15 + 6 * static_cast<int>(index));
  }
  EXPECT_TRUE(fixture.state->_clones_IMU.empty());
}

} // namespace
