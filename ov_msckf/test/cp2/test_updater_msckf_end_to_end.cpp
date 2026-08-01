/*
 * SchurVIO-Lite CP2-B production UpdaterMSCKF integration gate.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include <gtest/gtest.h>

#include "cam/CamRadtan.h"
#include "feat/Feature.h"
#include "feat/FeatureInitializer.h"
#include "feat/FeatureInitializerOptions.h"
#include "state/State.h"
#include "state/StateHelper.h"
#include "types/LandmarkRepresentation.h"
#include "types/Type.h"
#include "update/SchurUpdate.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKF.h"
#include "update/UpdaterMSCKFPreview.h"
#include "update/UpdaterOptions.h"
#include "utils/print.h"

#include <Eigen/Dense>

#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <unordered_map>
#include <vector>

namespace {

constexpr double kAbsoluteTolerance = 1.0e-8;
constexpr double kRelativeTolerance = 1.0e-6;
constexpr std::size_t kAcceptedFeatureCount = 6;

Eigen::Matrix<double, 7, 1> pose_value(const Eigen::Vector4d &quaternion,
                                       const Eigen::Vector3d &position) {
  Eigen::Matrix<double, 7, 1> value;
  value.head<4>() = quaternion;
  value.tail<3>() = position;
  return value;
}

Eigen::Vector4d yaw_quaternion(double angle) {
  Eigen::Vector4d quaternion;
  quaternion << 0.0, 0.0, std::sin(0.5 * angle), std::cos(0.5 * angle);
  return quaternion;
}

Eigen::VectorXd imu_value(const Eigen::Vector4d &quaternion, const Eigen::Vector3d &position,
                          double marker) {
  Eigen::VectorXd value = Eigen::VectorXd::Zero(16);
  value.head<4>() = quaternion;
  value.segment<3>(4) = position;
  value.segment<3>(7) << 0.12 + 0.01 * marker, -0.04 + 0.005 * marker,
      0.02 - 0.002 * marker;
  value.segment<3>(10) << 0.001, -0.002, 0.003;
  value.segment<3>(13) << -0.004, 0.005, -0.006;
  return value;
}

struct ProductionFixture {
  std::shared_ptr<ov_msckf::State> state;
  std::shared_ptr<ov_core::CamRadtan> camera;
  std::vector<double> timestamps;
  std::vector<std::shared_ptr<ov_type::Type>> top_level;
};

Eigen::MatrixXd deterministic_psd_prior(int dimension) {
  Eigen::MatrixXd root = Eigen::MatrixXd::Zero(dimension, dimension);
  for (int row = 0; row < dimension; ++row) {
    root(row, row) = 0.0045 + 0.00012 * static_cast<double>((row % 7) + 1);
    for (int column = 0; column < row; ++column) {
      root(row, column) =
          0.00012 * std::sin(0.37 * static_cast<double>((row + 1) * (column + 2)));
    }
  }
  return root * root.transpose();
}

ProductionFixture make_production_fixture(bool invalid_cross_covariance = false) {
  ov_msckf::StateOptions state_options;
  state_options.do_fej = true;
  state_options.do_calib_camera_pose = false;
  state_options.do_calib_camera_intrinsics = false;
  state_options.do_calib_camera_timeoffset = false;
  state_options.feat_rep_msckf =
      ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
  state_options.num_cameras = 1;
  state_options.max_clone_size = 8;

  ProductionFixture fixture;
  fixture.state = std::make_shared<ov_msckf::State>(state_options);

  Eigen::Matrix<double, 8, 1> intrinsics;
  intrinsics << 458.0, 457.0, 367.0, 248.0, -0.24, 0.065, 0.0007, -0.0004;
  fixture.state->_cam_intrinsics.at(0)->set_value(intrinsics);
  fixture.state->_cam_intrinsics.at(0)->set_fej(intrinsics);
  fixture.camera = std::make_shared<ov_core::CamRadtan>(752, 480);
  fixture.camera->set_value(intrinsics);
  fixture.state->_cam_intrinsics_cameras[0] = fixture.camera;

  const Eigen::Matrix<double, 7, 1> calibration =
      pose_value(yaw_quaternion(0.012), Eigen::Vector3d(0.035, -0.018, 0.012));
  fixture.state->_calib_IMUtoCAM.at(0)->set_value(calibration);
  fixture.state->_calib_IMUtoCAM.at(0)->set_fej(calibration);

  fixture.timestamps = {10.0, 20.0, 30.0, 40.0};
  const std::array<Eigen::Vector3d, 4> positions{{
      Eigen::Vector3d(0.00, 0.00, 0.00),
      Eigen::Vector3d(0.32, 0.03, 0.01),
      Eigen::Vector3d(0.67, -0.04, 0.025),
      Eigen::Vector3d(1.03, 0.055, 0.04),
  }};
  const std::array<double, 4> yaws{{0.000, 0.018, -0.012, 0.025}};

  for (std::size_t index = 0; index < fixture.timestamps.size(); ++index) {
    const Eigen::VectorXd current =
        imu_value(yaw_quaternion(yaws.at(index)), positions.at(index), static_cast<double>(index));
    const Eigen::VectorXd first_estimate =
        imu_value(yaw_quaternion(yaws.at(index) + 0.004 * static_cast<double>(index + 1)),
                  positions.at(index) +
                      Eigen::Vector3d(0.006 * static_cast<double>(index + 1), -0.004, 0.003),
                  static_cast<double>(index));
    fixture.state->_imu->set_value(current);
    fixture.state->_imu->set_fej(first_estimate);
    fixture.state->_timestamp = fixture.timestamps.at(index);

    // This is the production stochastic-copy insertion path. It assigns the
    // clone's covariance ID, appends it to State::_variables, copies its pose
    // covariance, and installs the same object in the timestamp map.
    ov_msckf::StateHelper::augment_clone(fixture.state, Eigen::Vector3d::Zero());
  }

  fixture.top_level.push_back(fixture.state->_imu);
  for (const double timestamp : fixture.timestamps) {
    fixture.top_level.push_back(fixture.state->_clones_IMU.at(timestamp));
  }

  EXPECT_EQ(fixture.state->max_covariance_size(), 15 + 4 * 6);
  EXPECT_EQ(fixture.top_level.front()->id(), 0);
  for (std::size_t index = 1; index < fixture.top_level.size(); ++index) {
    EXPECT_EQ(fixture.top_level.at(index)->id(), 15 + 6 * static_cast<int>(index - 1));
  }

  const int dimension = fixture.state->max_covariance_size();
  Eigen::MatrixXd covariance = deterministic_psd_prior(dimension);
  if (invalid_cross_covariance) {
    // Retain an SPD clone marginal so the feature gate succeeds, but make the
    // unobserved IMU/clone cross-covariance deliberately inconsistent. The
    // resulting subtractive proposal has a negative IMU diagonal and must be
    // rejected by the shared read-only preflight before StateHelper writes.
    covariance.topLeftCorner(15, 15) = 1.0e-7 * Eigen::MatrixXd::Identity(15, 15);
    for (int row = 0; row < 15; ++row) {
      for (int column = 15; column < dimension; ++column) {
        const double sign = ((row + column) % 2 == 0) ? 1.0 : -1.0;
        covariance(row, column) = sign * (0.018 + 0.0001 * static_cast<double>(row));
        covariance(column, row) = covariance(row, column);
      }
    }
  }
  ov_msckf::StateHelper::set_initial_covariance(fixture.state, covariance, fixture.top_level);

  const Eigen::MatrixXd installed = ov_msckf::StateHelper::get_full_covariance(fixture.state);
  EXPECT_TRUE(installed.allFinite());
  EXPECT_EQ(installed.rows(), dimension);
  EXPECT_EQ(installed.cols(), dimension);
  if (!invalid_cross_covariance) {
    const Eigen::LLT<Eigen::MatrixXd> factor(installed);
    EXPECT_EQ(factor.info(), Eigen::Success);
  }
  return fixture;
}

Eigen::Vector2f project_feature(const ProductionFixture &fixture,
                                const Eigen::Vector3d &feature_global, double timestamp) {
  const auto &clone = fixture.state->_clones_IMU.at(timestamp);
  const auto &calibration = fixture.state->_calib_IMUtoCAM.at(0);
  const Eigen::Vector3d point_imu = clone->Rot() * (feature_global - clone->pos());
  const Eigen::Vector3d point_camera = calibration->Rot() * point_imu + calibration->pos();
  EXPECT_GT(point_camera(2), 1.0);
  return fixture.camera->distort_f(
      Eigen::Vector2f(static_cast<float>(point_camera(0) / point_camera(2)),
                      static_cast<float>(point_camera(1) / point_camera(2))));
}

std::shared_ptr<ov_core::Feature> make_feature(const ProductionFixture &fixture,
                                               std::size_t feature_id,
                                               bool nonfinite_raw_measurement = false,
                                               std::size_t feature_variant = 0) {
  auto feature = std::make_shared<ov_core::Feature>();
  feature->featid = feature_id;
  feature->to_delete = false;

  const std::array<Eigen::Vector3d, kAcceptedFeatureCount> feature_positions{{
      Eigen::Vector3d(1.34, -0.31, 5.25),
      Eigen::Vector3d(1.58, 0.24, 5.65),
      Eigen::Vector3d(0.91, -0.52, 4.82),
      Eigen::Vector3d(1.83, -0.12, 6.08),
      Eigen::Vector3d(1.11, 0.43, 5.04),
      Eigen::Vector3d(1.47, -0.61, 5.88),
  }};
  const Eigen::Vector3d &feature_global =
      feature_positions.at(feature_variant % feature_positions.size());
  const std::array<Eigen::Vector2f, 4> perturbations{{
      Eigen::Vector2f(0.17F, -0.08F),
      Eigen::Vector2f(-0.11F, 0.13F),
      Eigen::Vector2f(0.09F, 0.06F),
      Eigen::Vector2f(-0.14F, -0.10F),
  }};
  const float variant_scale = 1.0F + 0.07F * static_cast<float>(feature_variant);

  for (std::size_t index = 0; index < fixture.timestamps.size(); ++index) {
    Eigen::Vector2f measured =
        project_feature(fixture, feature_global, fixture.timestamps.at(index)) +
        variant_scale * perturbations.at((index + feature_variant) % perturbations.size());
    const Eigen::Vector2f normalized = fixture.camera->undistort_f(measured);
    if (nonfinite_raw_measurement && index == 0) {
      // Triangulation consumes the finite normalized track, while the actual
      // production Jacobian/residual seam consumes the raw pixel and exposes
      // this NaN to the selected reducer.
      measured(0) = std::numeric_limits<float>::quiet_NaN();
    }
    feature->timestamps[0].push_back(fixture.timestamps.at(index));
    feature->uvs[0].push_back(measured);
    feature->uvs_norm[0].push_back(normalized);
  }
  return feature;
}

std::vector<std::shared_ptr<ov_core::Feature>>
make_accepted_features(const ProductionFixture &fixture, std::size_t first_feature_id) {
  std::vector<std::shared_ptr<ov_core::Feature>> features;
  features.reserve(kAcceptedFeatureCount);
  for (std::size_t index = 0; index < kAcceptedFeatureCount; ++index) {
    features.push_back(make_feature(fixture, first_feature_id + index, false, index));
  }
  return features;
}

ov_core::FeatureInitializerOptions feature_initializer_options() {
  ov_core::FeatureInitializerOptions options;
  options.triangulate_1d = false;
  options.refine_features = true;
  options.max_runs = 10;
  options.max_cond_number = 10000.0;
  options.max_baseline = 40.0;
  return options;
}

struct PreviewExpectation {
  ov_msckf::MSCKFUpdatePreviewResult preview;
  std::vector<Eigen::MatrixXd> nominal_values;
  Eigen::Index precompression_rows = 0;
  Eigen::Index precompression_columns = 0;
  Eigen::Index compressed_rows = 0;
};

PreviewExpectation make_preview_expectation(
    const ProductionFixture &fixture,
    const std::vector<std::shared_ptr<ov_core::Feature>> &source_features,
    ov_msckf::UpdaterOptions::LandmarkElimination mode) {
  PreviewExpectation expectation;
  std::unordered_map<std::size_t,
                     std::unordered_map<double, ov_core::FeatureInitializer::ClonePose>>
      clones_camera;
  for (const auto &calibration : fixture.state->_calib_IMUtoCAM) {
    std::unordered_map<double, ov_core::FeatureInitializer::ClonePose> camera_clones;
    for (const auto &clone : fixture.state->_clones_IMU) {
      const Eigen::Matrix3d rotation = calibration.second->Rot() * clone.second->Rot();
      const Eigen::Vector3d position =
          clone.second->pos() - rotation.transpose() * calibration.second->pos();
      camera_clones.insert(
          {clone.first, ov_core::FeatureInitializer::ClonePose(rotation, position)});
    }
    clones_camera.insert({calibration.first, camera_clones});
  }

  ov_core::FeatureInitializerOptions initializer_configuration =
      feature_initializer_options();
  ov_core::FeatureInitializer initializer(initializer_configuration);

  struct ReducedFeatureSystem {
    Eigen::MatrixXd H;
    Eigen::VectorXd residual;
    std::vector<std::shared_ptr<ov_type::Type>> order;
  };
  std::vector<ReducedFeatureSystem> reduced_systems;
  reduced_systems.reserve(source_features.size());

  for (const auto &source_feature : source_features) {
    auto feature = std::make_shared<ov_core::Feature>(*source_feature);
    const bool triangulated = initializer.single_triangulation(feature, clones_camera);
    const bool refined = triangulated && initializer.single_gaussnewton(feature, clones_camera);
    EXPECT_TRUE(triangulated);
    EXPECT_TRUE(refined);
    if (!triangulated || !refined) {
      return expectation;
    }

    ov_msckf::UpdaterHelper::UpdaterHelperFeature helper_feature;
    helper_feature.featid = feature->featid;
    helper_feature.uvs = feature->uvs;
    helper_feature.uvs_norm = feature->uvs_norm;
    helper_feature.timestamps = feature->timestamps;
    helper_feature.feat_representation =
        ov_type::LandmarkRepresentation::Representation::GLOBAL_3D;
    helper_feature.p_FinG = feature->p_FinG;
    helper_feature.p_FinG_fej = feature->p_FinG;

    Eigen::MatrixXd H_f;
    Eigen::MatrixXd H_x;
    Eigen::VectorXd residual;
    std::vector<std::shared_ptr<ov_type::Type>> H_order;
    ov_msckf::UpdaterHelper::get_feature_jacobian_full(
        fixture.state, helper_feature, H_f, H_x, residual, H_order);
    if (mode == ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR) {
      const ov_msckf::SchurReductionResult reduction =
          ov_msckf::SchurUpdate::Reduce(H_x, H_f, residual, 1.0);
      EXPECT_TRUE(reduction.accepted());
      if (!reduction.accepted()) {
        return expectation;
      }
      H_x = reduction.H_reduced;
      residual = reduction.residual_reduced;
    } else {
      ov_msckf::UpdaterHelper::nullspace_project_inplace(H_f, H_x, residual);
    }
    reduced_systems.push_back({H_x, residual, H_order});
  }

  std::unordered_map<std::shared_ptr<ov_type::Type>, Eigen::Index> column_mapping;
  std::vector<std::shared_ptr<ov_type::Type>> H_order_big;
  Eigen::Index total_rows = 0;
  Eigen::Index total_columns = 0;
  for (const auto &system : reduced_systems) {
    total_rows += system.H.rows();
    for (const auto &variable : system.order) {
      if (column_mapping.find(variable) == column_mapping.end()) {
        column_mapping.insert({variable, total_columns});
        H_order_big.push_back(variable);
        total_columns += variable->size();
      }
    }
  }

  Eigen::MatrixXd H_big = Eigen::MatrixXd::Zero(total_rows, total_columns);
  Eigen::VectorXd residual_big(total_rows);
  Eigen::Index row_offset = 0;
  for (const auto &system : reduced_systems) {
    Eigen::Index feature_column = 0;
    for (const auto &variable : system.order) {
      H_big.block(row_offset, column_mapping.at(variable), system.H.rows(), variable->size()) =
          system.H.block(0, feature_column, system.H.rows(), variable->size());
      feature_column += variable->size();
    }
    residual_big.segment(row_offset, system.residual.rows()) = system.residual;
    row_offset += system.residual.rows();
  }

  expectation.precompression_rows = H_big.rows();
  expectation.precompression_columns = H_big.cols();
  EXPECT_EQ(reduced_systems.size(), source_features.size());
  EXPECT_EQ(expectation.precompression_rows,
            static_cast<Eigen::Index>(source_features.size()) *
                (2 * static_cast<Eigen::Index>(fixture.timestamps.size()) - 3));
  EXPECT_EQ(expectation.precompression_columns,
            6 * static_cast<Eigen::Index>(fixture.timestamps.size()));
  EXPECT_GT(expectation.precompression_rows, expectation.precompression_columns);
  if (expectation.precompression_rows <= expectation.precompression_columns) {
    return expectation;
  }

  ov_msckf::UpdaterHelper::measurement_compress_inplace(H_big, residual_big);
  expectation.compressed_rows = H_big.rows();
  EXPECT_EQ(expectation.compressed_rows, expectation.precompression_columns);
  const Eigen::MatrixXd R =
      Eigen::MatrixXd::Identity(residual_big.rows(), residual_big.rows());

  expectation.preview = ov_msckf::UpdaterMSCKFPreview::Compute(
      fixture.state, H_order_big, H_big, residual_big, R);
  EXPECT_TRUE(expectation.preview.accepted());
  if (!expectation.preview.accepted()) {
    return expectation;
  }

  for (const auto &variable : fixture.top_level) {
    std::shared_ptr<ov_type::Type> expected = variable->clone();
    expected->update(expectation.preview.dx.segment(variable->id(), variable->size()));
    expectation.nominal_values.push_back(expected->value());
  }
  return expectation;
}

struct StateSnapshot {
  Eigen::MatrixXd covariance;
  std::vector<Eigen::MatrixXd> values;
  std::vector<Eigen::MatrixXd> fej_values;
};

StateSnapshot snapshot(const ProductionFixture &fixture) {
  StateSnapshot result;
  result.covariance = ov_msckf::StateHelper::get_full_covariance(fixture.state);
  for (const auto &variable : fixture.top_level) {
    result.values.push_back(variable->value());
    result.fej_values.push_back(variable->fej());
  }
  return result;
}

std::uint64_t ieee_bits(double value) {
  std::uint64_t bits = 0;
  static_assert(sizeof(bits) == sizeof(value), "binary64 and uint64_t must have equal size");
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

bool bitwise_equal(const Eigen::MatrixXd &left, const Eigen::MatrixXd &right) {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (ieee_bits(left(row, column)) != ieee_bits(right(row, column))) {
        return false;
      }
    }
  }
  return true;
}

void expect_exact_state(const ProductionFixture &fixture, const StateSnapshot &expected) {
  EXPECT_TRUE(bitwise_equal(ov_msckf::StateHelper::get_full_covariance(fixture.state),
                            expected.covariance));
  ASSERT_EQ(fixture.top_level.size(), expected.values.size());
  for (std::size_t index = 0; index < fixture.top_level.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "top_level=" << index);
    EXPECT_TRUE(bitwise_equal(fixture.top_level.at(index)->value(), expected.values.at(index)));
    EXPECT_TRUE(
        bitwise_equal(fixture.top_level.at(index)->fej(), expected.fej_values.at(index)));
  }
}

void expect_fej_unchanged(const ProductionFixture &fixture, const StateSnapshot &before) {
  ASSERT_EQ(fixture.top_level.size(), before.fej_values.size());
  for (std::size_t index = 0; index < fixture.top_level.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "top_level=" << index);
    EXPECT_TRUE(
        bitwise_equal(fixture.top_level.at(index)->fej(), before.fej_values.at(index)));
  }
}

std::unique_ptr<ov_msckf::UpdaterMSCKF>
make_updater(ov_msckf::UpdaterOptions::LandmarkElimination mode) {
  ov_msckf::UpdaterOptions updater_options;
  updater_options.landmark_elimination = mode;
  updater_options.sigma_pix = 1.0;
  updater_options.sigma_pix_sq = 1.0;
  updater_options.chi2_multipler = 5.0;

  ov_core::FeatureInitializerOptions initializer_options = feature_initializer_options();
  return std::unique_ptr<ov_msckf::UpdaterMSCKF>(
      new ov_msckf::UpdaterMSCKF(updater_options, initializer_options));
}

double mixed_tolerance(double reference_norm) {
  return kAbsoluteTolerance + kRelativeTolerance * reference_norm;
}

struct SemanticBlock {
  std::string name;
  Eigen::Index error_id;
  Eigen::Index error_size;
  Eigen::VectorXd nominal;
};

std::vector<SemanticBlock>
semantic_blocks_from_values(const ProductionFixture &fixture,
                            const std::vector<Eigen::MatrixXd> &values) {
  EXPECT_EQ(values.size(), fixture.top_level.size());
  std::vector<SemanticBlock> blocks;
  if (values.size() != fixture.top_level.size()) {
    return blocks;
  }
  const Eigen::VectorXd imu = values.at(0);
  blocks.push_back({"imu.theta", fixture.state->_imu->id(), 3, imu.segment(0, 4)});
  blocks.push_back({"imu.p", fixture.state->_imu->id() + 3, 3, imu.segment(4, 3)});
  blocks.push_back({"imu.v", fixture.state->_imu->id() + 6, 3, imu.segment(7, 3)});
  blocks.push_back({"imu.bg", fixture.state->_imu->id() + 9, 3, imu.segment(10, 3)});
  blocks.push_back({"imu.ba", fixture.state->_imu->id() + 12, 3, imu.segment(13, 3)});

  for (std::size_t index = 0; index < fixture.timestamps.size(); ++index) {
    const auto &clone = fixture.state->_clones_IMU.at(fixture.timestamps.at(index));
    const Eigen::VectorXd pose = values.at(index + 1);
    blocks.push_back({"clone[" + std::to_string(index) + "].theta", clone->id(), 3,
                      pose.segment(0, 4)});
    blocks.push_back({"clone[" + std::to_string(index) + "].p", clone->id() + 3, 3,
                      pose.segment(4, 3)});
  }
  return blocks;
}

std::vector<SemanticBlock> semantic_blocks(const ProductionFixture &fixture) {
  std::vector<Eigen::MatrixXd> values;
  for (const auto &variable : fixture.top_level) {
    values.push_back(variable->value());
  }
  return semantic_blocks_from_values(fixture, values);
}

void expect_commit_matches_preview(const ProductionFixture &fixture,
                                   const PreviewExpectation &expectation) {
  ASSERT_TRUE(expectation.preview.accepted());
  ASSERT_EQ(expectation.nominal_values.size(), fixture.top_level.size());
  const std::vector<SemanticBlock> reference_blocks =
      semantic_blocks_from_values(fixture, expectation.nominal_values);
  const std::vector<SemanticBlock> committed_blocks = semantic_blocks(fixture);
  ASSERT_EQ(reference_blocks.size(), committed_blocks.size());

  for (std::size_t index = 0; index < reference_blocks.size(); ++index) {
    SCOPED_TRACE(::testing::Message()
                 << "preview_nominal_block=" << reference_blocks.at(index).name);
    const Eigen::VectorXd &reference = reference_blocks.at(index).nominal;
    EXPECT_LE((committed_blocks.at(index).nominal - reference).norm(),
              mixed_tolerance(reference.norm()));
  }

  const Eigen::MatrixXd committed_covariance =
      ov_msckf::StateHelper::get_full_covariance(fixture.state);
  for (std::size_t row = 0; row < reference_blocks.size(); ++row) {
    for (std::size_t column = 0; column < reference_blocks.size(); ++column) {
      SCOPED_TRACE(::testing::Message()
                   << "preview_covariance_block=" << reference_blocks.at(row).name << ","
                   << reference_blocks.at(column).name);
      const Eigen::MatrixXd reference = expectation.preview.P_plus.block(
          reference_blocks.at(row).error_id, reference_blocks.at(column).error_id,
          reference_blocks.at(row).error_size, reference_blocks.at(column).error_size);
      const Eigen::MatrixXd actual = committed_covariance.block(
          committed_blocks.at(row).error_id, committed_blocks.at(column).error_id,
          committed_blocks.at(row).error_size, committed_blocks.at(column).error_size);
      EXPECT_LE((actual - reference).norm(), mixed_tolerance(reference.norm()));
    }
  }
}

void compare_all_semantic_blocks(const ProductionFixture &baseline,
                                 const ProductionFixture &candidate) {
  const std::vector<SemanticBlock> baseline_blocks = semantic_blocks(baseline);
  const std::vector<SemanticBlock> candidate_blocks = semantic_blocks(candidate);
  ASSERT_EQ(baseline_blocks.size(), candidate_blocks.size());
  ASSERT_EQ(baseline_blocks.size(), 5U + 2U * baseline.timestamps.size());
  for (std::size_t index = 0; index < baseline_blocks.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "nominal_block=" << baseline_blocks.at(index).name);
    EXPECT_EQ(candidate_blocks.at(index).name, baseline_blocks.at(index).name);
    const Eigen::VectorXd &reference = baseline_blocks.at(index).nominal;
    const double error = (candidate_blocks.at(index).nominal - reference).norm();
    EXPECT_LE(error, mixed_tolerance(reference.norm()));
  }

  const Eigen::MatrixXd baseline_covariance =
      ov_msckf::StateHelper::get_full_covariance(baseline.state);
  const Eigen::MatrixXd candidate_covariance =
      ov_msckf::StateHelper::get_full_covariance(candidate.state);
  for (std::size_t row = 0; row < baseline_blocks.size(); ++row) {
    for (std::size_t column = 0; column < baseline_blocks.size(); ++column) {
      SCOPED_TRACE(::testing::Message()
                   << "covariance_block=" << baseline_blocks.at(row).name << ","
                   << baseline_blocks.at(column).name);
      EXPECT_EQ(candidate_blocks.at(row).error_id, baseline_blocks.at(row).error_id);
      EXPECT_EQ(candidate_blocks.at(column).error_id, baseline_blocks.at(column).error_id);
      const Eigen::MatrixXd reference = baseline_covariance.block(
          baseline_blocks.at(row).error_id, baseline_blocks.at(column).error_id,
          baseline_blocks.at(row).error_size, baseline_blocks.at(column).error_size);
      const Eigen::MatrixXd actual = candidate_covariance.block(
          candidate_blocks.at(row).error_id, candidate_blocks.at(column).error_id,
          candidate_blocks.at(row).error_size, candidate_blocks.at(column).error_size);
      EXPECT_LE((actual - reference).norm(), mixed_tolerance(reference.norm()));
    }
  }
}

TEST(CP2UpdaterMSCKFEndToEnd,
     ActualNullspaceAndSchurModesCommitEquivalentFullStateUpdates) {
  ProductionFixture baseline = make_production_fixture();
  ProductionFixture candidate = make_production_fixture();
  const StateSnapshot baseline_before = snapshot(baseline);
  const StateSnapshot candidate_before = snapshot(candidate);
  ASSERT_TRUE(bitwise_equal(baseline_before.covariance, candidate_before.covariance));

  constexpr std::size_t first_feature_id = 0x435032554e00ULL;
  const auto baseline_preview_features =
      make_accepted_features(baseline, first_feature_id);
  const auto candidate_preview_features =
      make_accepted_features(candidate, first_feature_id);
  const PreviewExpectation baseline_expectation = make_preview_expectation(
      baseline, baseline_preview_features,
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  const PreviewExpectation candidate_expectation = make_preview_expectation(
      candidate, candidate_preview_features,
      ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR);
  ASSERT_TRUE(baseline_expectation.preview.accepted());
  ASSERT_TRUE(candidate_expectation.preview.accepted());
  ASSERT_EQ(baseline_expectation.precompression_rows, 30);
  ASSERT_EQ(candidate_expectation.precompression_rows, 30);
  ASSERT_EQ(baseline_expectation.precompression_columns, 24);
  ASSERT_EQ(candidate_expectation.precompression_columns, 24);
  ASSERT_GT(baseline_expectation.precompression_rows,
            baseline_expectation.precompression_columns);
  ASSERT_GT(candidate_expectation.precompression_rows,
            candidate_expectation.precompression_columns);
  ASSERT_EQ(baseline_expectation.compressed_rows, 24);
  ASSERT_EQ(candidate_expectation.compressed_rows, 24);

  auto baseline_features = make_accepted_features(baseline, first_feature_id);
  auto candidate_features = make_accepted_features(candidate, first_feature_id);
  const auto baseline_feature_handles = baseline_features;
  const auto candidate_feature_handles = candidate_features;

  auto baseline_updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto candidate_updater = make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR);

  const ov_core::Printer::PrintLevel saved_level = ov_core::Printer::current_print_level;
  ov_core::Printer::setPrintLevel(ov_core::Printer::PrintLevel::ALL);
  testing::internal::CaptureStdout();
  baseline_updater->update(baseline.state, baseline_features);
  const std::string baseline_diagnostic = testing::internal::GetCapturedStdout();
  testing::internal::CaptureStdout();
  candidate_updater->update(candidate.state, candidate_features);
  const std::string candidate_diagnostic = testing::internal::GetCapturedStdout();
  ov_core::Printer::setPrintLevel(saved_level);

  EXPECT_NE(baseline_diagnostic.find("[MSCKF-REDUCTION]: mode=nullspace"),
            std::string::npos);
  EXPECT_NE(baseline_diagnostic.find("compressed_rows=24\n"), std::string::npos);
  EXPECT_NE(candidate_diagnostic.find("[MSCKF-REDUCTION]: mode=schur"),
            std::string::npos);
  EXPECT_NE(candidate_diagnostic.find("compressed_rows=24\n"), std::string::npos);

  // A retained feature remains in the caller's vector but is marked consumed;
  // an erased feature would demonstrate a triangulation, reduction, or gate
  // rejection instead of the required committing path.
  ASSERT_EQ(baseline_features.size(), kAcceptedFeatureCount);
  ASSERT_EQ(candidate_features.size(), kAcceptedFeatureCount);
  for (std::size_t index = 0; index < kAcceptedFeatureCount; ++index) {
    SCOPED_TRACE(::testing::Message() << "accepted_feature=" << index);
    EXPECT_EQ(baseline_features.at(index), baseline_feature_handles.at(index));
    EXPECT_EQ(candidate_features.at(index), candidate_feature_handles.at(index));
    EXPECT_TRUE(baseline_feature_handles.at(index)->to_delete);
    EXPECT_TRUE(candidate_feature_handles.at(index)->to_delete);
    EXPECT_TRUE(baseline_feature_handles.at(index)->p_FinG.allFinite());
    EXPECT_TRUE(candidate_feature_handles.at(index)->p_FinG.allFinite());
  }

  const StateSnapshot baseline_after = snapshot(baseline);
  const StateSnapshot candidate_after = snapshot(candidate);
  EXPECT_GT((baseline_after.covariance - baseline_before.covariance).norm(), 1.0e-14);
  EXPECT_GT((candidate_after.covariance - candidate_before.covariance).norm(), 1.0e-14);

  double baseline_nominal_change = 0.0;
  double candidate_nominal_change = 0.0;
  for (std::size_t index = 0; index < baseline.top_level.size(); ++index) {
    baseline_nominal_change +=
        (baseline_after.values.at(index) - baseline_before.values.at(index)).squaredNorm();
    candidate_nominal_change +=
        (candidate_after.values.at(index) - candidate_before.values.at(index)).squaredNorm();
  }
  EXPECT_GT(std::sqrt(baseline_nominal_change), 1.0e-12);
  EXPECT_GT(std::sqrt(candidate_nominal_change), 1.0e-12);

  expect_fej_unchanged(baseline, baseline_before);
  expect_fej_unchanged(candidate, candidate_before);
  expect_commit_matches_preview(baseline, baseline_expectation);
  expect_commit_matches_preview(candidate, candidate_expectation);
  compare_all_semantic_blocks(baseline, candidate);
}

TEST(CP2UpdaterMSCKFEndToEnd,
     SelectedReducersRejectNonfiniteProductionRowsWithoutSilentFallback) {
  const std::array<ov_msckf::UpdaterOptions::LandmarkElimination, 2> modes{{
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE,
      ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR,
  }};

  const ov_core::Printer::PrintLevel saved_level = ov_core::Printer::current_print_level;
  ov_core::Printer::setPrintLevel(ov_core::Printer::PrintLevel::WARNING);
  for (const auto mode : modes) {
    SCOPED_TRACE(ov_msckf::UpdaterOptions::landmark_elimination_as_string(mode));
    ProductionFixture fixture = make_production_fixture();
    const StateSnapshot before = snapshot(fixture);
    auto feature = make_feature(fixture, 0x4350324e414eULL, true);
    std::vector<std::shared_ptr<ov_core::Feature>> features{feature};
    auto updater = make_updater(mode);

    testing::internal::CaptureStdout();
    updater->update(fixture.state, features);
    const std::string diagnostic = testing::internal::GetCapturedStdout();

    EXPECT_TRUE(features.empty());
    EXPECT_TRUE(feature->to_delete);
    expect_exact_state(fixture, before);
    EXPECT_EQ(diagnostic.find("[MSCKF-PREFLIGHT]"), std::string::npos);
    EXPECT_EQ(diagnostic.find("accepted_gamma"), std::string::npos);
    if (mode == ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR) {
      EXPECT_NE(diagnostic.find("[MSCKF-SCHUR]"), std::string::npos);
      EXPECT_NE(diagnostic.find("status=nonfinite"), std::string::npos);
      EXPECT_NE(diagnostic.find("stage=raw_inputs"), std::string::npos);
      EXPECT_NE(diagnostic.find("jitter=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("clamp=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("regularization=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("fallback=0"), std::string::npos);
      EXPECT_EQ(diagnostic.find("[MSCKF-REDUCTION]"), std::string::npos);
      EXPECT_EQ(diagnostic.find("mode=nullspace"), std::string::npos);
    } else {
      EXPECT_NE(diagnostic.find("[MSCKF-REDUCTION]"), std::string::npos);
      EXPECT_NE(diagnostic.find("status=nonfinite"), std::string::npos);
      EXPECT_NE(diagnostic.find("jitter=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("clamp=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("regularization=0"), std::string::npos);
      EXPECT_NE(diagnostic.find("fallback=0"), std::string::npos);
      EXPECT_EQ(diagnostic.find("[MSCKF-SCHUR]"), std::string::npos);
      EXPECT_EQ(diagnostic.find("mode=schur"), std::string::npos);
    }
  }
  ov_core::Printer::setPrintLevel(saved_level);
}

TEST(CP2UpdaterMSCKFEndToEnd,
     SharedInvalidPreflightLeavesBothModeStatesBitwiseUnchanged) {
  const std::array<ov_msckf::UpdaterOptions::LandmarkElimination, 2> modes{{
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE,
      ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR,
  }};

  const ov_core::Printer::PrintLevel saved_level = ov_core::Printer::current_print_level;
  ov_core::Printer::setPrintLevel(ov_core::Printer::PrintLevel::WARNING);
  for (const auto mode : modes) {
    SCOPED_TRACE(ov_msckf::UpdaterOptions::landmark_elimination_as_string(mode));
    ProductionFixture fixture = make_production_fixture(true);
    const StateSnapshot before = snapshot(fixture);
    auto feature = make_feature(fixture, 0x435032505246ULL);
    std::vector<std::shared_ptr<ov_core::Feature>> features{feature};
    auto updater = make_updater(mode);

    testing::internal::CaptureStdout();
    updater->update(fixture.state, features);
    const std::string diagnostic = testing::internal::GetCapturedStdout();

    // The feature passed triangulation, reduction, and gating and was consumed
    // before the shared proposal was rejected. The transaction boundary covers
    // state/covariance writes, not the caller-owned feature lifecycle flag.
    ASSERT_EQ(features.size(), 1U);
    EXPECT_TRUE(feature->to_delete);
    expect_exact_state(fixture, before);
    EXPECT_NE(diagnostic.find("[MSCKF-PREFLIGHT]"), std::string::npos);
    EXPECT_NE(diagnostic.find("status=negative_diagonal"), std::string::npos);
    EXPECT_NE(diagnostic.find("stage=posterior_diagonal"), std::string::npos);
    EXPECT_NE(diagnostic.find("jitter=0"), std::string::npos);
    EXPECT_NE(diagnostic.find("repair=0"), std::string::npos);
    EXPECT_NE(diagnostic.find("alternate_solve=0"), std::string::npos);
    EXPECT_NE(diagnostic.find("clamp=0"), std::string::npos);
    EXPECT_NE(diagnostic.find("regularization=0"), std::string::npos);
    EXPECT_NE(diagnostic.find("fallback=0"), std::string::npos);
    EXPECT_EQ(diagnostic.find("accepted_gamma"), std::string::npos);
  }
  ov_core::Printer::setPrintLevel(saved_level);
}

} // namespace
