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
#include "update/CP2CompositeState.h"
#include "update/CP2StateTraceCodec.h"
#include "update/UpdaterHelper.h"
#include "update/UpdaterMSCKF.h"
#include "update/UpdaterMSCKFPreview.h"
#include "update/UpdaterOptions.h"
#include "utils/print.h"

#include <Eigen/Dense>

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <unordered_map>
#include <vector>

#if defined(OV_MSCKF_CP2_TESTING)
namespace ov_msckf {

class CP2UpdaterTestAccess final {
public:
  static void SetFault(UpdaterMSCKF &updater, CP2UpdaterTestFault fault) {
    updater.cp2_test_fault = fault;
  }

  static std::uint64_t RawAssemblyCalls(const UpdaterMSCKF &updater) {
    return updater.cp2_test_raw_assembly_calls;
  }

  static void SetNextInvocationId(UpdaterMSCKF &updater,
                                  std::uint64_t value) {
    updater.cp2_next_invocation_id = value;
  }

  static std::vector<CP2UpdaterTestStage>
  Stages(const UpdaterMSCKF &updater) {
    return std::vector<CP2UpdaterTestStage>(
        updater.cp2_test_stages.begin(),
        updater.cp2_test_stages.begin() +
            static_cast<std::ptrdiff_t>(updater.cp2_test_stage_count));
  }
};

} // namespace ov_msckf
#endif

namespace {

constexpr double kAbsoluteTolerance = 1.0e-8;
constexpr double kRelativeTolerance = 1.0e-6;
constexpr std::size_t kAcceptedFeatureCount = 6;

std::uint64_t binary64_bits(double value) noexcept {
  std::uint64_t bits = 0U;
  static_assert(sizeof(bits) == sizeof(value), "CP2 requires IEEE-754 binary64");
  std::memcpy(&bits, &value, sizeof(bits));
  return bits;
}

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

std::vector<std::shared_ptr<ov_core::Feature>>
make_large_residual_features(const ProductionFixture &fixture, std::size_t first_feature_id,
                             std::size_t feature_count = kAcceptedFeatureCount) {
  if (feature_count == 0U || feature_count > kAcceptedFeatureCount) {
    throw std::invalid_argument("large-residual feature count is outside the frozen fixture");
  }
  std::vector<std::shared_ptr<ov_core::Feature>> features =
      make_accepted_features(fixture, first_feature_id);
  features.resize(feature_count);
  constexpr float kLargePixelResidual = 1.0e38F;
  for (std::size_t feature_index = 0; feature_index < features.size(); ++feature_index) {
    for (std::size_t observation_index = 0;
         observation_index < features.at(feature_index)->uvs.at(0).size();
         ++observation_index) {
      const float sign = ((feature_index + observation_index) % 2U == 0U) ? 1.0F : -1.0F;
      // Keep the already-computed normalized track finite for triangulation,
      // but drive the production raw-pixel residual seam with a large finite
      // value whose nullspace gamma can be tuned to the binary64 sum boundary.
      features.at(feature_index)->uvs.at(0).at(observation_index)(0) =
          sign * kLargePixelResidual;
      features.at(feature_index)->uvs.at(0).at(observation_index)(1) =
          -0.5F * sign * kLargePixelResidual;
    }
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

bool bitwise_equal(const Eigen::MatrixXd &left, const Eigen::MatrixXd &right) {
  if (left.rows() != right.rows() || left.cols() != right.cols()) {
    return false;
  }
  for (Eigen::Index row = 0; row < left.rows(); ++row) {
    for (Eigen::Index column = 0; column < left.cols(); ++column) {
      if (binary64_bits(left(row, column)) != binary64_bits(right(row, column))) {
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
make_updater(ov_msckf::UpdaterOptions::LandmarkElimination mode,
             double sigma_pix = 1.0, double chi2_multiplier = 5.0) {
  ov_msckf::UpdaterOptions updater_options;
  updater_options.landmark_elimination = mode;
  updater_options.sigma_pix = sigma_pix;
  updater_options.sigma_pix_sq = sigma_pix * sigma_pix;
  updater_options.chi2_multipler = chi2_multiplier;

  ov_core::FeatureInitializerOptions initializer_options = feature_initializer_options();
  return std::unique_ptr<ov_msckf::UpdaterMSCKF>(
      new ov_msckf::UpdaterMSCKF(updater_options, initializer_options));
}

ov_msckf::CP2UpdateInvocationContext invocation_context(
    std::uint64_t sequence_index = 3U, std::uint64_t pair_index = 11U,
    std::uint64_t camera_timestamp_ns = 123456789U) {
  ov_msckf::CP2UpdateInvocationContext context;
  context.sequence_index = sequence_index;
  context.pair_index = pair_index;
  context.camera_timestamp_ns = camera_timestamp_ns;
  return context;
}

class RecordingUpdateSink final : public ov_msckf::CP2RecordedUpdateSink {
public:
  ov_msckf::CP2RecordedSinkStatus status =
      ov_msckf::CP2RecordedSinkStatus::kPublished;
  std::size_t call_count = 0U;
  std::shared_ptr<const ov_msckf::CP2RecordedUpdateEvent> record;

  ov_msckf::CP2RecordedSinkStatus Publish(
      const std::shared_ptr<const ov_msckf::CP2RecordedUpdateEvent> &input)
      noexcept override {
    ++call_count;
    record = input;
    return status;
  }
};

void expect_recorded_phase_population(
    const ov_msckf::CP2RecordedUpdateEvent &record,
    const std::vector<ov_msckf::CP2StatePhase> &expected) {
  ASSERT_EQ(record.state_phase_count, expected.size());
  for (std::size_t index = 0U; index < expected.size(); ++index) {
    SCOPED_TRACE(::testing::Message() << "phase_index=" << index);
    ASSERT_TRUE(record.state_phases.at(index).snapshot);
    EXPECT_EQ(record.state_phases.at(index).phase, expected.at(index));
    EXPECT_EQ(record.state_phases.at(index).snapshot->phase,
              expected.at(index));
    EXPECT_EQ(record.state_phases.at(index).payload,
              ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
                  *record.state_phases.at(index).snapshot));
  }
  for (std::size_t index = expected.size();
       index < record.state_phases.size(); ++index) {
    EXPECT_FALSE(record.state_phases.at(index).snapshot);
    EXPECT_TRUE(record.state_phases.at(index).payload.empty());
  }
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
    std::size_t callback_count = 0;
    ov_msckf::CP2LiveUpdateEvent observed;
    ASSERT_TRUE(updater->set_cp2_update_callback(
        [&](const ov_msckf::CP2LiveUpdateEvent &event) {
          ++callback_count;
          observed = event;
        },
        false));

    testing::internal::CaptureStdout();
    updater->update(fixture.state, features);
    const std::string diagnostic = testing::internal::GetCapturedStdout();

    EXPECT_TRUE(features.empty());
    EXPECT_TRUE(feature->to_delete);
    expect_exact_state(fixture, before);
    ASSERT_EQ(callback_count, 1U);
    EXPECT_EQ(observed.terminal_status,
              ov_msckf::CP2UpdateTerminalStatus::kAllRejected);
    EXPECT_EQ(observed.terminal_subreason,
              ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected);
    EXPECT_EQ(observed.input_feature_count, 1U);
    EXPECT_EQ(observed.raw_system_count, 1U);
    EXPECT_TRUE(observed.baseline_accepted_ids.empty());
    EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
    EXPECT_EQ(binary64_bits(observed.baseline_gamma), binary64_bits(0.0));
    EXPECT_FALSE(observed.baseline_precompression_system_nonempty);
    EXPECT_TRUE(observed.baseline_precompression_rows_available);
    EXPECT_EQ(observed.baseline_precompression_rows, 0U);
    EXPECT_FALSE(observed.baseline_compressed_rows_available);
    EXPECT_FALSE(observed.baseline_preflight_attempted);
    EXPECT_FALSE(observed.baseline_commit_occurred);
    EXPECT_FALSE(observed.shadow_evidence_available);
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
    std::size_t callback_count = 0;
    ov_msckf::CP2LiveUpdateEvent observed;
    ASSERT_TRUE(updater->set_cp2_update_callback(
        [&](const ov_msckf::CP2LiveUpdateEvent &event) {
          ++callback_count;
          observed = event;
        },
        false));

    testing::internal::CaptureStdout();
    updater->update(fixture.state, features);
    const std::string diagnostic = testing::internal::GetCapturedStdout();

    // The feature passed triangulation, reduction, and gating and was consumed
    // before the shared proposal was rejected. The transaction boundary covers
    // state/covariance writes, not the caller-owned feature lifecycle flag.
    ASSERT_EQ(features.size(), 1U);
    EXPECT_TRUE(feature->to_delete);
    expect_exact_state(fixture, before);
    ASSERT_EQ(callback_count, 1U);
    EXPECT_EQ(observed.terminal_status,
              ov_msckf::CP2UpdateTerminalStatus::kPreflightRejected);
    EXPECT_EQ(observed.terminal_subreason,
              ov_msckf::CP2UpdateTerminalSubreason::kBaselinePreflightRejected);
    EXPECT_EQ(observed.input_feature_count, 1U);
    EXPECT_EQ(observed.raw_system_count, 1U);
    ASSERT_EQ(observed.baseline_accepted_ids.size(), 1U);
    EXPECT_EQ(observed.baseline_accepted_ids.front(), feature->featid);
    EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
    EXPECT_TRUE(observed.baseline_precompression_system_nonempty);
    EXPECT_TRUE(observed.baseline_precompression_rows_available);
    EXPECT_GT(observed.baseline_precompression_rows, 0U);
    EXPECT_TRUE(observed.baseline_compressed_system_nonempty);
    EXPECT_TRUE(observed.baseline_compressed_rows_available);
    EXPECT_GT(observed.baseline_compressed_rows, 0U);
    EXPECT_TRUE(observed.baseline_preflight_attempted);
    EXPECT_FALSE(observed.baseline_preflight_accepted);
    EXPECT_FALSE(observed.baseline_commit_occurred);
    EXPECT_FALSE(observed.shadow_evidence_available);
    EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(observed.terminal_status),
                 "preflight_rejected");
    EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(observed.terminal_subreason),
                 "baseline_preflight_rejected");
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

TEST(CP2UpdaterMSCKFEndToEnd,
     NullspaceShadowPublishesBothPrecommitProposalsThenOneCommittedEvent) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  constexpr std::size_t first_feature_id = 0x435032534844ULL;
  const auto preview_features = make_accepted_features(fixture, first_feature_id);
  const PreviewExpectation expectation = make_preview_expectation(
      fixture, preview_features,
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  ASSERT_TRUE(expectation.preview.accepted());

  auto features = make_accepted_features(fixture, first_feature_id);
  auto updater = make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  std::size_t callback_count = 0;
  bool callback_saw_committed_state = false;
  ov_msckf::CP2LiveUpdateEvent observed;
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++callback_count;
        observed = event;
        callback_saw_committed_state =
            event.baseline_commit_occurred && event.shadow.baseline_preview_available &&
            bitwise_equal(ov_msckf::StateHelper::get_full_covariance(fixture.state),
                          event.shadow.live_nullspace_proposal.P_plus);
      },
      true));

  updater->update(fixture.state, features);

  ASSERT_EQ(callback_count, 1U);
  EXPECT_TRUE(callback_saw_committed_state);
  EXPECT_EQ(observed.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_EQ(observed.terminal_subreason, ov_msckf::CP2UpdateTerminalSubreason::kNone);
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(observed.terminal_status),
               "committed_counted");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(observed.terminal_subreason),
               "none");
  EXPECT_EQ(observed.input_feature_count, kAcceptedFeatureCount);
  EXPECT_EQ(observed.raw_system_count, kAcceptedFeatureCount);
  ASSERT_EQ(observed.baseline_accepted_ids.size(), kAcceptedFeatureCount);
  for (std::size_t index = 0; index < kAcceptedFeatureCount; ++index) {
    EXPECT_EQ(observed.baseline_accepted_ids.at(index), first_feature_id + index);
  }
  EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_TRUE(observed.baseline_precompression_system_nonempty);
  EXPECT_TRUE(observed.baseline_precompression_rows_available);
  EXPECT_EQ(observed.baseline_precompression_rows,
            static_cast<std::uint64_t>(expectation.precompression_rows));
  EXPECT_TRUE(observed.baseline_compressed_system_nonempty);
  EXPECT_TRUE(observed.baseline_compressed_rows_available);
  EXPECT_EQ(observed.baseline_compressed_rows,
            static_cast<std::uint64_t>(expectation.compressed_rows));
  EXPECT_TRUE(observed.baseline_preflight_attempted);
  EXPECT_TRUE(observed.baseline_preflight_accepted);
  EXPECT_TRUE(observed.baseline_commit_occurred);
  ASSERT_TRUE(observed.shadow_evidence_available);
  EXPECT_TRUE(observed.shadow.shadow_math_completed);
  EXPECT_TRUE(observed.shadow.baseline_preview_available);
  EXPECT_TRUE(observed.shadow.baseline_commit_planned);
  ASSERT_TRUE(observed.shadow.live_nullspace_proposal.accepted());

  ASSERT_EQ(observed.shadow.input.raw_systems.size(), kAcceptedFeatureCount);
  EXPECT_TRUE(bitwise_equal(observed.shadow.input.prior.covariance, before.covariance));
  ASSERT_EQ(observed.shadow.input.prior.state_blocks.size(), fixture.top_level.size());
  for (std::size_t index = 0; index < fixture.top_level.size(); ++index) {
    const ov_msckf::MSCKFUpdatePreviewBlock &block =
        observed.shadow.input.prior.state_blocks.at(index);
    EXPECT_EQ(block.covariance_id, fixture.top_level.at(index)->id());
    EXPECT_EQ(block.size, fixture.top_level.at(index)->size());
    EXPECT_EQ(block.offset, fixture.top_level.at(index)->id());
  }
  for (std::size_t index = 0; index < kAcceptedFeatureCount; ++index) {
    const ov_msckf::CP2RawFeatureSystem &raw = observed.shadow.input.raw_systems.at(index);
    EXPECT_EQ(raw.feature_id, first_feature_id + index);
    EXPECT_EQ(raw.H_x.rows(), raw.H_f.rows());
    EXPECT_EQ(raw.residual.rows(), raw.H_f.rows());
    EXPECT_EQ(raw.H_f.cols(), 3);
    EXPECT_GT(raw.H_x.cols(), 0);
  }

  const ov_msckf::CP2ShadowMathResult &shadow = observed.shadow.result;
  EXPECT_TRUE(shadow.input_valid);
  EXPECT_FALSE(shadow.duplicate_feature_id);
  ASSERT_EQ(shadow.features.size(), kAcceptedFeatureCount);
  ASSERT_TRUE(shadow.nullspace.proposal_available);
  ASSERT_TRUE(shadow.schur.proposal_available);
  ASSERT_TRUE(shadow.nullspace.proposal.accepted());
  ASSERT_TRUE(shadow.schur.proposal.accepted());
  EXPECT_EQ(shadow.nullspace.accepted_ids.size(), kAcceptedFeatureCount);
  EXPECT_EQ(shadow.schur.accepted_ids.size(), kAcceptedFeatureCount);
  EXPECT_EQ(observed.baseline_accepted_ids, shadow.nullspace.accepted_ids);
  EXPECT_EQ(binary64_bits(observed.baseline_gamma),
            binary64_bits(shadow.nullspace.retained_gamma));
  EXPECT_EQ(observed.baseline_precompression_rows,
            static_cast<std::uint64_t>(shadow.nullspace.precompression_rows));
  EXPECT_EQ(observed.baseline_compressed_rows,
            static_cast<std::uint64_t>(shadow.nullspace.compressed_rows));

  // The live preflight and the independently assembled shadow nullspace
  // proposal consume the same immutable snapshot and are byte-exact here.
  EXPECT_TRUE(bitwise_equal(observed.shadow.live_nullspace_proposal.dx,
                            shadow.nullspace.proposal.dx));
  EXPECT_TRUE(bitwise_equal(observed.shadow.live_nullspace_proposal.P_plus,
                            shadow.nullspace.proposal.P_plus));
  EXPECT_TRUE(bitwise_equal(observed.shadow.live_nullspace_proposal.dx,
                            expectation.preview.dx));
  EXPECT_TRUE(bitwise_equal(observed.shadow.live_nullspace_proposal.P_plus,
                            expectation.preview.P_plus));
  EXPECT_TRUE(bitwise_equal(ov_msckf::StateHelper::get_full_covariance(fixture.state),
                            observed.shadow.live_nullspace_proposal.P_plus));

  for (const ov_msckf::CP2FeaturePairResult &feature : shadow.features) {
    EXPECT_EQ(feature.nullspace.counters.jitter_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.repair_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.alternate_solve_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.clamp_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.regularization_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.silent_fallback_count, 0U);
    EXPECT_EQ(feature.nullspace.counters.fallback_count, 0U);
    EXPECT_EQ(feature.schur.jitter_count, 0U);
    EXPECT_EQ(feature.schur.clamp_count, 0U);
    EXPECT_EQ(feature.schur.regularization_count, 0U);
    EXPECT_EQ(feature.schur.fallback_count, 0U);
  }

  expect_fej_unchanged(fixture, before);
  expect_commit_matches_preview(fixture, expectation);
}

TEST(CP2UpdaterMSCKFEndToEnd,
     NonfiniteGammaEvidenceCannotStopLiveTraversalOrBaselineCommit) {
  constexpr std::size_t first_feature_id = 0x43503247414dULL;
  constexpr std::size_t gamma_overflow_feature_count = 2U;
  const double accepting_multiplier = std::numeric_limits<double>::max();

  // First obtain the exact production-nullspace residual vectors at sigma=1.
  // The large finite raw pixels are installed only after their normalized
  // triangulation tracks were formed, so every feature reaches the shared raw
  // seam while retaining a residual-dominant, finite gate system.
  ProductionFixture probe_fixture = make_production_fixture();
  auto probe_features =
      make_large_residual_features(probe_fixture, first_feature_id,
                                   gamma_overflow_feature_count);
  auto probe_updater = make_updater(
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE, 1.0,
      accepting_multiplier);
  std::size_t probe_callback_count = 0;
  ov_msckf::CP2LiveUpdateEvent probe_event;
  ASSERT_TRUE(probe_updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++probe_callback_count;
        probe_event = event;
      },
      true));
  probe_updater->update(probe_fixture.state, probe_features);
  ASSERT_EQ(probe_callback_count, 1U);
  ASSERT_TRUE(probe_event.shadow_evidence_available);
  ASSERT_TRUE(probe_event.shadow.shadow_math_completed);
  ASSERT_EQ(probe_event.shadow.result.features.size(), gamma_overflow_feature_count);

  double raw_gamma_sum = 0.0;
  for (const ov_msckf::CP2FeaturePairResult &feature :
       probe_event.shadow.result.features) {
    ASSERT_TRUE(feature.nullspace.emitted_system_available);
    ASSERT_TRUE(feature.nullspace.residual_reduced.allFinite());
    const double raw_gamma = feature.nullspace.residual_reduced.squaredNorm();
    ASSERT_TRUE(std::isfinite(raw_gamma));
    ASSERT_GT(raw_gamma, 0.0);
    raw_gamma_sum += raw_gamma;
  }
  ASSERT_TRUE(std::isfinite(raw_gamma_sum));

  // Select a representable sigma for which every individual ordered gamma is
  // finite but their production-order binary64 sum overflows. The initial
  // scale targets a total of 4/3*DBL_MAX; the bounded adjustment handles the
  // exact divide/square rounding used by Eigen without changing production.
  double sigma_pix =
      std::sqrt(0.75 * (raw_gamma_sum / std::numeric_limits<double>::max()));
  bool found_ordered_sum_overflow = false;
  for (int attempt = 0; attempt < 64 && !found_ordered_sum_overflow; ++attempt) {
    ASSERT_TRUE(std::isfinite(sigma_pix));
    ASSERT_GT(sigma_pix, 0.0);
    ASSERT_TRUE(std::isfinite(sigma_pix * sigma_pix));
    ASSERT_GT(sigma_pix * sigma_pix, 0.0);

    std::vector<double> ordered_gammas;
    bool every_individual_gamma_finite = true;
    for (const ov_msckf::CP2FeaturePairResult &feature :
         probe_event.shadow.result.features) {
      const Eigen::VectorXd whitened =
          feature.nullspace.residual_reduced.array() / sigma_pix;
      const double gamma = whitened.squaredNorm();
      if (!whitened.allFinite() || !std::isfinite(gamma)) {
        every_individual_gamma_finite = false;
        break;
      }
      ordered_gammas.push_back(gamma);
    }
    if (!every_individual_gamma_finite) {
      sigma_pix *= 1.05;
      continue;
    }

    double ordered_sum = 0.0;
    for (double gamma : ordered_gammas) {
      const double next = ordered_sum + gamma;
      if (!std::isfinite(next)) {
        found_ordered_sum_overflow = true;
        break;
      }
      ordered_sum = next;
    }
    if (!found_ordered_sum_overflow) {
      sigma_pix *= 0.95;
    }
  }
  ASSERT_TRUE(found_ordered_sum_overflow);

  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_large_residual_features(fixture, first_feature_id,
                                               gamma_overflow_feature_count);
  auto updater = make_updater(
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE, sigma_pix,
      accepting_multiplier);
  auto recorded_sink = std::make_shared<RecordingUpdateSink>();
  std::size_t callback_count = 0;
  ov_msckf::CP2LiveUpdateEvent observed;
  ASSERT_TRUE(updater->set_cp2_recorded_sink(recorded_sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++callback_count;
        observed = event;
      },
      true));

  updater->update(fixture.state, features);

  ASSERT_EQ(callback_count, 1U);
  EXPECT_EQ(observed.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_EQ(observed.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kNone);
  EXPECT_EQ(observed.raw_system_count, gamma_overflow_feature_count);
  EXPECT_EQ(observed.baseline_accepted_ids.size(), gamma_overflow_feature_count);
  EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_TRUE(std::isnan(observed.baseline_gamma));
  EXPECT_TRUE(observed.baseline_precompression_system_nonempty);
  EXPECT_TRUE(observed.baseline_compressed_system_nonempty);
  EXPECT_TRUE(observed.baseline_preflight_accepted);
  EXPECT_TRUE(observed.baseline_commit_occurred);
  ASSERT_TRUE(observed.shadow_evidence_available);
  ASSERT_TRUE(observed.shadow.shadow_math_completed);
  const ov_msckf::CP2ShadowMathResult &shadow = observed.shadow.result;
  ASSERT_TRUE(shadow.input_valid);
  EXPECT_EQ(observed.baseline_accepted_ids, shadow.nullspace.accepted_ids);
  EXPECT_EQ(shadow.nullspace.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_TRUE(shadow.nullspace.proposal_available);
  EXPECT_EQ(shadow.schur.accepted_ids.size(), gamma_overflow_feature_count);
  EXPECT_EQ(shadow.schur.gamma_status, ov_msckf::CP2GammaStatus::kNonfinite);
  EXPECT_FALSE(shadow.schur.proposal_available);
  EXPECT_EQ(shadow.features.size(), gamma_overflow_feature_count);
  ASSERT_EQ(recorded_sink->call_count, 1U);
  ASSERT_TRUE(recorded_sink->record);
  EXPECT_EQ(recorded_sink->record->update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_TRUE(recorded_sink->record->baseline_commit_oracle_available);
  EXPECT_TRUE(recorded_sink->record->baseline_commit_oracle.passed);
  EXPECT_FALSE(recorded_sink->record->candidate_proposal_payload_available);
  EXPECT_FALSE(recorded_sink->record->online_math_evidence_passed);
  EXPECT_FALSE(bitwise_equal(before.covariance,
                             ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFEndToEnd,
     SchurModeRejectsShadowEnableWithoutReplacingExistingObserver) {
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(
                   ov_msckf::CP2UpdateTerminalStatus::kEmptyAfterCompression),
               "empty_after_compression");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(
                   ov_msckf::CP2UpdateTerminalStatus::kInternalFailure),
               "internal_failure");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(
                   ov_msckf::CP2UpdateTerminalSubreason::kMeasurementCompressionEmpty),
               "measurement_compression_empty");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(
                   ov_msckf::CP2UpdateTerminalSubreason::kInvalidLiveMode),
               "invalid_live_mode");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(
                   ov_msckf::CP2UpdateTerminalSubreason::kSnapshotMismatch),
               "snapshot_mismatch");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(
                   ov_msckf::CP2UpdateTerminalSubreason::kTraceInvariantFailure),
               "trace_invariant_failure");
  auto updater = make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR);
  std::size_t retained_callback_count = 0;
  std::size_t rejected_callback_count = 0;
  bool reentry_rejected = false;
  ov_msckf::CP2LiveUpdateEvent observed;
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++retained_callback_count;
        observed = event;
        if (!reentry_rejected) {
          std::vector<std::shared_ptr<ov_core::Feature>> nested_features;
          try {
            updater->update(std::shared_ptr<ov_msckf::State>(), nested_features);
          } catch (const std::logic_error &) {
            reentry_rejected = true;
          }
        }
      },
      false));
  EXPECT_FALSE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &) { ++rejected_callback_count; }, true));

  std::vector<std::shared_ptr<ov_core::Feature>> features;
  updater->update(std::shared_ptr<ov_msckf::State>(), features);

  EXPECT_EQ(retained_callback_count, 1U);
  EXPECT_EQ(rejected_callback_count, 0U);
  EXPECT_TRUE(reentry_rejected);
  EXPECT_EQ(observed.terminal_status, ov_msckf::CP2UpdateTerminalStatus::kEmptyInput);
  EXPECT_EQ(observed.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kInputEmpty);
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(observed.terminal_status),
               "empty_input");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(observed.terminal_subreason),
               "input_empty");
  EXPECT_EQ(observed.input_feature_count, 0U);
  EXPECT_TRUE(observed.baseline_accepted_ids.empty());
  EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kNotReached);
  EXPECT_FALSE(observed.baseline_precompression_rows_available);
  EXPECT_FALSE(observed.baseline_compressed_rows_available);
  EXPECT_FALSE(observed.shadow_evidence_available);
  EXPECT_FALSE(observed.baseline_commit_occurred);

  EXPECT_FALSE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &) { ++rejected_callback_count; }, false));
  updater->update(std::shared_ptr<ov_msckf::State>(), features);
  EXPECT_EQ(retained_callback_count, 2U);
  EXPECT_EQ(rejected_callback_count, 0U);
}

TEST(CP2UpdaterMSCKFEndToEnd,
     ObserverExceptionCannotVetoAnAcceptedBaselineCommit) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  constexpr std::size_t first_feature_id = 0x435032455843ULL;
  const auto preview_features = make_accepted_features(fixture, first_feature_id);
  const PreviewExpectation expectation = make_preview_expectation(
      fixture, preview_features,
      ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  ASSERT_TRUE(expectation.preview.accepted());
  auto features = make_accepted_features(fixture, first_feature_id);
  auto updater = make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  std::size_t callback_count = 0;
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++callback_count;
        EXPECT_TRUE(event.baseline_commit_occurred);
        throw std::runtime_error("expected observer failure");
      },
      false));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  EXPECT_EQ(callback_count, 1U);
  EXPECT_TRUE(bitwise_equal(ov_msckf::StateHelper::get_full_covariance(fixture.state),
                            expectation.preview.P_plus));
  EXPECT_FALSE(bitwise_equal(before.covariance, expectation.preview.P_plus));
  expect_fej_unchanged(fixture, before);
  expect_commit_matches_preview(fixture, expectation);
}

TEST(CP2UpdaterMSCKFEndToEnd,
     AllRejectedRawSystemHasExactTerminalTaxonomyAndNoBaselineWrite) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto feature = make_feature(fixture, 0x435032414c4cULL, true);
  std::vector<std::shared_ptr<ov_core::Feature>> features{feature};
  auto updater = make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  std::size_t callback_count = 0;
  ov_msckf::CP2LiveUpdateEvent observed;
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        ++callback_count;
        observed = event;
      },
      true));

  updater->update(fixture.state, features);

  ASSERT_EQ(callback_count, 1U);
  EXPECT_EQ(observed.terminal_status, ov_msckf::CP2UpdateTerminalStatus::kAllRejected);
  EXPECT_EQ(observed.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected);
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_status_name(observed.terminal_status),
               "all_rejected");
  EXPECT_STREQ(ov_msckf::cp2_update_terminal_subreason_name(observed.terminal_subreason),
               "all_baseline_features_rejected");
  EXPECT_EQ(observed.input_feature_count, 1U);
  EXPECT_EQ(observed.raw_system_count, 1U);
  EXPECT_TRUE(observed.baseline_accepted_ids.empty());
  EXPECT_EQ(observed.baseline_gamma_status, ov_msckf::CP2GammaStatus::kAvailable);
  EXPECT_EQ(binary64_bits(observed.baseline_gamma), binary64_bits(0.0));
  EXPECT_FALSE(observed.baseline_precompression_system_nonempty);
  EXPECT_TRUE(observed.baseline_precompression_rows_available);
  EXPECT_EQ(observed.baseline_precompression_rows, 0U);
  EXPECT_FALSE(observed.baseline_compressed_rows_available);
  EXPECT_FALSE(observed.baseline_compressed_system_nonempty);
  EXPECT_FALSE(observed.baseline_preflight_attempted);
  EXPECT_FALSE(observed.baseline_preflight_accepted);
  EXPECT_FALSE(observed.baseline_commit_occurred);
  ASSERT_TRUE(observed.shadow_evidence_available);
  EXPECT_TRUE(observed.shadow.shadow_math_completed);
  EXPECT_FALSE(observed.shadow.baseline_preview_available);
  EXPECT_FALSE(observed.shadow.baseline_commit_planned);
  ASSERT_EQ(observed.shadow.input.raw_systems.size(), 1U);
  EXPECT_EQ(observed.shadow.input.raw_systems.front().feature_id, feature->featid);
  EXPECT_TRUE(features.empty());
  EXPECT_TRUE(feature->to_delete);
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     ZeroRawDiscardsTentativeWithoutValidationOrPhases) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);

  Eigen::MatrixXd invalid_time_offset =
      fixture.state->_calib_dt_CAMtoIMU->value();
  ASSERT_EQ(invalid_time_offset.size(), 1);
  invalid_time_offset(0, 0) = std::numeric_limits<double>::infinity();
  fixture.state->_calib_dt_CAMtoIMU->set_value(invalid_time_offset);
  ov_msckf::CP2CompositeStateCapture complete_before_update;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_before_update),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  const std::vector<std::uint8_t> complete_before_payload =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
          complete_before_update.snapshot);

  auto feature = make_feature(fixture, 0x4350325a4552ULL);
  feature->timestamps.at(0).resize(1U);
  feature->uvs.at(0).resize(1U);
  feature->uvs_norm.at(0).resize(1U);
  std::vector<std::shared_ptr<ov_core::Feature>> features{feature};

  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  std::size_t observer_count = 0U;
  bool observer_saw_published_record = false;
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(
      invocation_context(5U, 17U, 987654321U)));
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &) {
        ++observer_count;
        observer_saw_published_record = sink->call_count == 1U;
        throw std::runtime_error("expected diagnostic observer failure");
      },
      false));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  EXPECT_EQ(observer_count, 1U);
  EXPECT_TRUE(observer_saw_published_record);
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
  EXPECT_TRUE(sink->record->update.invocation_context_available);
  EXPECT_EQ(sink->record->update.sequence_index, 5U);
  EXPECT_EQ(sink->record->update.pair_index, 17U);
  EXPECT_EQ(sink->record->update.camera_timestamp_ns, 987654321U);
  EXPECT_EQ(sink->record->update.invocation_id, 0U);
  EXPECT_EQ(sink->record->update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kAllRejected);
  EXPECT_EQ(sink->record->update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kNoFeaturesAfterCleaning);
  EXPECT_EQ(sink->record->update.raw_system_count, 0U);
  EXPECT_TRUE(sink->record->raw_system_payloads.empty());
  expect_recorded_phase_population(*sink->record, {});
  EXPECT_FALSE(sink->record->baseline_commit_oracle_available);
  EXPECT_EQ(sink->record->baseline_mean_commit_count, 0U);
  EXPECT_EQ(sink->record->baseline_covariance_commit_count, 0U);
#if defined(OV_MSCKF_CP2_TESTING)
  EXPECT_EQ(ov_msckf::CP2UpdaterTestAccess::RawAssemblyCalls(*updater), 0U);
#endif
  ov_msckf::CP2CompositeStateCapture complete_after_update;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_after_update),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(complete_before_payload,
            ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
                complete_after_update.snapshot));
  EXPECT_TRUE(ov_msckf::CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, complete_before_update.pointer_graph));
  EXPECT_TRUE(features.empty());
  EXPECT_TRUE(feature->to_delete);
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     CleanCommitPublishesExactCompositeAndCommitOracle) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  constexpr std::size_t first_feature_id = 0x435032433243ULL;
  auto features = make_accepted_features(fixture, first_feature_id);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(
      invocation_context(8U, 23U, 1122334455U)));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  EXPECT_TRUE(record.update.invocation_context_available);
  EXPECT_EQ(record.update.sequence_index, 8U);
  EXPECT_EQ(record.update.pair_index, 23U);
  EXPECT_EQ(record.update.camera_timestamp_ns, 1122334455U);
  EXPECT_EQ(record.update.invocation_id, 0U);
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit,
               ov_msckf::CP2StatePhase::kPhase2ExpectedPostcommit,
               ov_msckf::CP2StatePhase::kPhase3LivePostcommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kNone);
  EXPECT_TRUE(record.update.baseline_commit_occurred);
  EXPECT_TRUE(record.phase01_canonical_equal);
  EXPECT_TRUE(record.phase01_pointer_graph_equal);
  EXPECT_EQ(record.state_phases[0].payload, record.state_phases[1].payload);
  EXPECT_EQ(record.state_phases[2].payload, record.state_phases[3].payload);
  EXPECT_TRUE(record.baseline_proposal_payload_available);
  EXPECT_TRUE(record.candidate_proposal_payload_available);
  EXPECT_EQ(record.raw_system_payloads.size(), kAcceptedFeatureCount);
  EXPECT_TRUE(record.baseline_commit_oracle_available);
  EXPECT_FALSE(record.baseline_commit_mismatch);
  EXPECT_TRUE(record.baseline_commit_oracle.passed);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_expected_type_update_calls,
            5U);
  EXPECT_EQ(record.baseline_commit_oracle.observed_type_update_calls, 5U);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_verified_nominal_fields,
            44U);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_nominal_mismatches, 0U);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_covariance_mismatches, 0U);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_fej_mismatches, 0U);
  EXPECT_EQ(record.baseline_commit_oracle.state_blocks_expected, 13U);
  EXPECT_EQ(record.baseline_commit_oracle.state_blocks_seen, 13U);
  EXPECT_EQ(record.baseline_commit_oracle.covariance_blocks_expected, 169U);
  EXPECT_EQ(record.baseline_commit_oracle.covariance_blocks_seen, 169U);
  EXPECT_EQ(record.baseline_mean_commit_count, 1U);
  EXPECT_EQ(record.baseline_covariance_commit_count, 1U);
  EXPECT_EQ(record.baseline_commit_count, 1U);
  EXPECT_EQ(record.candidate_ekf_update_call_count, 0U);
  EXPECT_EQ(record.candidate_mean_write_count, 0U);
  EXPECT_EQ(record.candidate_covariance_write_count, 0U);
  EXPECT_EQ(record.candidate_type_update_call_count, 0U);
  EXPECT_EQ(record.candidate_feature_write_count, 0U);
  EXPECT_TRUE(record.online_math_evidence_passed);
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
#if defined(OV_MSCKF_CP2_TESTING)
  EXPECT_EQ(ov_msckf::CP2UpdaterTestAccess::RawAssemblyCalls(*updater),
            kAcceptedFeatureCount);
  EXPECT_EQ(
      ov_msckf::CP2UpdaterTestAccess::Stages(*updater),
      (std::vector<ov_msckf::CP2UpdaterTestStage>{
          ov_msckf::CP2UpdaterTestStage::kPhase0Capture,
          ov_msckf::CP2UpdaterTestStage::kPhase0Projection,
          ov_msckf::CP2UpdaterTestStage::kPhase2Build,
          ov_msckf::CP2UpdaterTestStage::kPhase1Capture}));
#endif

  ASSERT_TRUE(record.update.shadow_evidence_available);
  ASSERT_TRUE(record.update.shadow.shadow_math_completed);
  ov_msckf::MSCKFUpdatePreviewSnapshot projected_phase0;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::ProjectPreview(
                *record.state_phases[0].snapshot, projected_phase0),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  EXPECT_TRUE(bitwise_equal(projected_phase0.covariance,
                            record.update.shadow.input.prior.covariance));
  EXPECT_TRUE(bitwise_equal(
      ov_msckf::StateHelper::get_full_covariance(fixture.state),
      record.state_phases[3].snapshot->covariance));
  EXPECT_FALSE(bitwise_equal(before.covariance,
                             record.state_phases[3].snapshot->covariance));
  expect_fej_unchanged(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     RawNoncommitPublishesOnlyEqualPhaseZeroAndOne) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto feature = make_feature(fixture, 0x43503243324eULL, true);
  std::vector<std::shared_ptr<ov_core::Feature>> features{feature};
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kAllRejected);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kAllBaselineFeaturesRejected);
  EXPECT_EQ(record.update.raw_system_count, 1U);
  EXPECT_EQ(record.raw_system_payloads.size(), 1U);
  EXPECT_TRUE(record.phase01_canonical_equal);
  EXPECT_TRUE(record.phase01_pointer_graph_equal);
  EXPECT_EQ(record.state_phases[0].payload, record.state_phases[1].payload);
  EXPECT_FALSE(record.baseline_commit_oracle_available);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_expected_type_update_calls,
            0U);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_verified_nominal_fields,
            0U);
  EXPECT_EQ(record.baseline_mean_commit_count, 0U);
  EXPECT_EQ(record.baseline_covariance_commit_count, 0U);
  EXPECT_FALSE(record.update.baseline_commit_occurred);
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     PromotionFailureIsFatalBeforeAnyPublishOrBaselineWrite) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  Eigen::MatrixXd invalid_time_offset =
      fixture.state->_calib_dt_CAMtoIMU->value();
  invalid_time_offset(0, 0) = std::numeric_limits<double>::quiet_NaN();
  fixture.state->_calib_dt_CAMtoIMU->set_value(invalid_time_offset);
  ov_msckf::CP2CompositeStateCapture complete_before_failure;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_before_failure),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  const std::vector<std::uint8_t> complete_before_payload =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
          complete_before_failure.snapshot);
  auto features = make_accepted_features(fixture, 0x435032503046ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "invalid tentative phase 0 did not stop recorded mode";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(), ov_msckf::CP2TraceFatalReason::kPhase0Promotion);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kPhase0Promotion);
#if defined(OV_MSCKF_CP2_TESTING)
  EXPECT_EQ(ov_msckf::CP2UpdaterTestAccess::RawAssemblyCalls(*updater), 0U);
#endif
  ov_msckf::CP2CompositeStateCapture complete_after_failure;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_after_failure),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(complete_before_payload,
            ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
                complete_after_failure.snapshot));
  EXPECT_TRUE(ov_msckf::CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, complete_before_failure.pointer_graph));
  expect_exact_state(fixture, before);
  std::vector<std::shared_ptr<ov_core::Feature>> empty_features;
  EXPECT_THROW(updater->update(fixture.state, empty_features),
               ov_msckf::CP2TraceFatalError);
  EXPECT_EQ(sink->call_count, 0U);
}

TEST(CP2UpdaterMSCKFTransaction,
     SinkRejectionAfterCommitLatchesFatalWithoutRollbackOrObserver) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032534e4bULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  sink->status = ov_msckf::CP2RecordedSinkStatus::kRejected;
  std::size_t observer_count = 0U;
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &) { ++observer_count; }, false));

  try {
    updater->update(fixture.state, features);
    FAIL() << "rejected authoritative publication did not stop the campaign";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(), ov_msckf::CP2TraceFatalReason::kSinkRejected);
  }

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  EXPECT_EQ(observer_count, 0U);
  EXPECT_TRUE(sink->record->update.baseline_commit_occurred);
  EXPECT_EQ(sink->record->update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_TRUE(sink->record->baseline_commit_oracle_available);
  EXPECT_TRUE(sink->record->baseline_commit_oracle.passed);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kSinkRejected);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
  EXPECT_TRUE(bitwise_equal(
      sink->record->state_phases[3].snapshot->covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     MissingRecordedContextIsFatalBeforeEstimatorWork) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x4350324d4358ULL);
  const std::vector<std::shared_ptr<ov_core::Feature>> features_before =
      features;
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  std::size_t observer_count = 0U;
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &) { ++observer_count; }, false));

  try {
    updater->update(fixture.state, features);
    FAIL() << "recorded update without invocation context did not fail";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kConfigurationInvariant);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_EQ(observer_count, 0U);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kConfigurationInvariant);
  EXPECT_FALSE(
      updater->set_cp2_invocation_context(invocation_context()));
  EXPECT_EQ(features, features_before);
#if defined(OV_MSCKF_CP2_TESTING)
  EXPECT_EQ(ov_msckf::CP2UpdaterTestAccess::RawAssemblyCalls(*updater), 0U);
#endif
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     InvocationContextIsOneShotContiguousAndSettersFailClosed) {
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  std::vector<ov_msckf::CP2LiveUpdateEvent> observed;
  std::vector<bool> reentrant_setter_results;
  ASSERT_TRUE(updater->set_cp2_update_callback(
      [&](const ov_msckf::CP2LiveUpdateEvent &event) {
        observed.push_back(event);
        reentrant_setter_results.push_back(
            updater->set_cp2_invocation_context(
                invocation_context(99U, 99U, 99U)));
      },
      false));

  const ov_msckf::CP2UpdateInvocationContext first =
      invocation_context(2U, 7U, 1000000001U);
  const ov_msckf::CP2UpdateInvocationContext second =
      invocation_context(2U, 8U, 1000000002U);
  ASSERT_TRUE(updater->set_cp2_invocation_context(first));
  EXPECT_FALSE(updater->set_cp2_invocation_context(second));

  std::vector<std::shared_ptr<ov_core::Feature>> features;
  EXPECT_NO_THROW(
      updater->update(std::shared_ptr<ov_msckf::State>(), features));
  EXPECT_NO_THROW(
      updater->update(std::shared_ptr<ov_msckf::State>(), features));
  ASSERT_TRUE(updater->set_cp2_invocation_context(second));
  EXPECT_FALSE(updater->set_cp2_update_callback(
      ov_msckf::UpdaterMSCKF::CP2UpdateCallback(), false));
  EXPECT_NO_THROW(
      updater->update(std::shared_ptr<ov_msckf::State>(), features));

  ASSERT_EQ(observed.size(), 3U);
  EXPECT_EQ(reentrant_setter_results,
            (std::vector<bool>{false, false, false}));
  EXPECT_TRUE(observed[0].invocation_context_available);
  EXPECT_EQ(observed[0].sequence_index, first.sequence_index);
  EXPECT_EQ(observed[0].pair_index, first.pair_index);
  EXPECT_EQ(observed[0].camera_timestamp_ns, first.camera_timestamp_ns);
  EXPECT_EQ(observed[0].invocation_id, 0U);
  EXPECT_FALSE(observed[1].invocation_context_available);
  EXPECT_EQ(observed[1].sequence_index, 0U);
  EXPECT_EQ(observed[1].pair_index, 0U);
  EXPECT_EQ(observed[1].camera_timestamp_ns, 0U);
  EXPECT_EQ(observed[1].invocation_id, 0U);
  EXPECT_TRUE(observed[2].invocation_context_available);
  EXPECT_EQ(observed[2].sequence_index, second.sequence_index);
  EXPECT_EQ(observed[2].pair_index, second.pair_index);
  EXPECT_EQ(observed[2].camera_timestamp_ns, second.camera_timestamp_ns);
  EXPECT_EQ(observed[2].invocation_id, 1U);
  for (const ov_msckf::CP2LiveUpdateEvent &event : observed) {
    EXPECT_EQ(event.terminal_status,
              ov_msckf::CP2UpdateTerminalStatus::kEmptyInput);
  }
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
}

#if defined(OV_MSCKF_CP2_TESTING)
TEST(CP2UpdaterMSCKFTransaction,
     InvocationIdOverflowIsFatalBeforeEstimatorWork) {
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));
  ov_msckf::CP2UpdaterTestAccess::SetNextInvocationId(
      *updater, std::numeric_limits<std::uint64_t>::max());
  std::vector<std::shared_ptr<ov_core::Feature>> features;

  try {
    updater->update(std::shared_ptr<ov_msckf::State>(), features);
    FAIL() << "overflowed invocation identity was accepted";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  EXPECT_FALSE(
      updater->set_cp2_invocation_context(invocation_context()));
  EXPECT_EQ(ov_msckf::CP2UpdaterTestAccess::RawAssemblyCalls(*updater), 0U);
}

TEST(CP2UpdaterMSCKFTransaction,
     CandidateAssemblyFailureCannotVetoBaselineCommit) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032434146ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kCandidateAssemblyFailure);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit,
               ov_msckf::CP2StatePhase::kPhase2ExpectedPostcommit,
               ov_msckf::CP2StatePhase::kPhase3LivePostcommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_TRUE(record.update.baseline_commit_occurred);
  EXPECT_TRUE(record.update.shadow.result.traversal_complete);
  EXPECT_TRUE(record.update.shadow.result.nullspace_assembly_valid);
  EXPECT_FALSE(record.update.shadow.result.schur_assembly_valid);
  EXPECT_TRUE(record.baseline_proposal_payload_available);
  EXPECT_FALSE(record.candidate_proposal_payload_available);
  EXPECT_TRUE(record.baseline_commit_oracle.passed);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     BaselineProvenanceMismatchSuppressesCommitBeforePhase2) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032505256ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kBaselineProvenanceMismatch);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kInternalFailure);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  EXPECT_FALSE(record.update.baseline_commit_occurred);
  EXPECT_FALSE(record.update.shadow.baseline_commit_planned);
  EXPECT_TRUE(record.update.shadow.result.traversal_complete);
  EXPECT_TRUE(record.update.shadow.result.nullspace_assembly_valid);
  EXPECT_TRUE(record.phase01_canonical_equal);
  EXPECT_TRUE(record.phase01_pointer_graph_equal);
  EXPECT_TRUE(record.baseline_proposal_payload_available);
  EXPECT_TRUE(record.candidate_proposal_payload_available);
  EXPECT_FALSE(record.baseline_commit_oracle_available);
  EXPECT_EQ(record.baseline_mean_commit_count, 0U);
  EXPECT_EQ(record.baseline_covariance_commit_count, 0U);
  EXPECT_EQ(record.baseline_commit_count, 0U);
  EXPECT_EQ(record.candidate_ekf_update_call_count, 0U);
  EXPECT_EQ(record.candidate_mean_write_count, 0U);
  EXPECT_EQ(record.candidate_covariance_write_count, 0U);
  EXPECT_EQ(record.candidate_type_update_call_count, 0U);
  EXPECT_EQ(record.candidate_feature_write_count, 0U);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(
      ov_msckf::CP2UpdaterTestAccess::Stages(*updater),
      (std::vector<ov_msckf::CP2UpdaterTestStage>{
          ov_msckf::CP2UpdaterTestStage::kPhase0Capture,
          ov_msckf::CP2UpdaterTestStage::kPhase0Projection,
          ov_msckf::CP2UpdaterTestStage::kPhase1Capture}));
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     InvalidPhase2IsDiscardedAndCannotCommit) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  ov_msckf::CP2CompositeStateCapture complete_before;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_before),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  const std::vector<std::uint8_t> complete_before_payload =
      ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
          complete_before.snapshot);
  auto features = make_accepted_features(fixture, 0x435032493250ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kInvalidPhase2);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kInternalFailure);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kTraceInvariantFailure);
  EXPECT_FALSE(record.update.baseline_commit_occurred);
  EXPECT_FALSE(record.baseline_commit_oracle_available);
  EXPECT_EQ(record.baseline_commit_count, 0U);
  EXPECT_FALSE(record.online_math_evidence_passed);
  ov_msckf::CP2CompositeStateCapture complete_after;
  ASSERT_EQ(ov_msckf::CP2CompositeStateAdapter::Capture(
                fixture.state, ov_msckf::CP2StatePhase::kPhase0Prior,
                complete_after),
            ov_msckf::CP2CompositeStateStatus::kAccepted);
  EXPECT_EQ(complete_before_payload,
            ov_msckf::CP2StateTraceCodec::EncodeSnapshotPayload(
                complete_after.snapshot));
  EXPECT_TRUE(ov_msckf::CP2CompositeStateAdapter::PointerGraphMatches(
      fixture.state, complete_before.pointer_graph));
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     NonfinitePhase1IsSnapshotMismatchAndDiscardsPhase2) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x43503250314eULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kPhase1ValueMismatch);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kInternalFailure);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kSnapshotMismatch);
  EXPECT_FALSE(record.phase01_canonical_equal);
  EXPECT_TRUE(record.phase01_pointer_graph_equal);
  EXPECT_NE(record.state_phases[0].payload, record.state_phases[1].payload);
  EXPECT_EQ(ov_msckf::CP2CompositeStateAdapter::Validate(
                *record.state_phases[1].snapshot),
            ov_msckf::CP2CompositeStateStatus::kNonfinite);
  EXPECT_FALSE(record.update.baseline_commit_occurred);
  EXPECT_FALSE(record.baseline_commit_oracle_available);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_TRUE(std::isnan(fixture.state->_timestamp));
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     FinalPointerRejectionDiscardsInstalledPhase2) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032503152ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kFinalPointerMismatch);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kInternalFailure);
  EXPECT_EQ(record.update.terminal_subreason,
            ov_msckf::CP2UpdateTerminalSubreason::kSnapshotMismatch);
  EXPECT_TRUE(record.phase01_canonical_equal);
  EXPECT_FALSE(record.phase01_pointer_graph_equal);
  EXPECT_EQ(record.state_phases[0].payload, record.state_phases[1].payload);
  EXPECT_FALSE(record.update.baseline_commit_occurred);
  EXPECT_FALSE(record.baseline_commit_oracle_available);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_FALSE(fixture.state->_cam_intrinsics_cameras.at(0));
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     IncompletePostcommitStorageIsFatalAfterCommitWithoutPublication) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032503346ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kInvalidatePostcommitStorage);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "incomplete committed phase-3 storage was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kPostcommitCapture);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kPostcommitCapture);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     PostcommitPointerTokenFailureIsFatalAfterCommitWithoutPublication) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032503350ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kInvalidatePostcommitPointerToken);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "postcommit pointer-token failure was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kPostcommitCapture);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kPostcommitCapture);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     CompletePhase3ValueMismatchRemainsCountedFailedEvidence) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x43503250334dULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kPhase3ValueMismatch);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit,
               ov_msckf::CP2StatePhase::kPhase2ExpectedPostcommit,
               ov_msckf::CP2StatePhase::kPhase3LivePostcommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_TRUE(record.update.baseline_commit_occurred);
  EXPECT_TRUE(record.baseline_commit_oracle_available);
  EXPECT_EQ(record.baseline_commit_oracle.status,
            ov_msckf::CP2CommitOracleStatus::kComplete);
  EXPECT_TRUE(record.baseline_commit_oracle.phase3_valid);
  EXPECT_FALSE(record.baseline_commit_oracle.passed);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_covariance_mismatches, 1U);
  EXPECT_TRUE(record.baseline_commit_mismatch);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_NE(record.state_phases[2].payload, record.state_phases[3].payload);
  EXPECT_TRUE(bitwise_equal(
      record.state_phases[2].snapshot->covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
  EXPECT_FALSE(bitwise_equal(
      record.state_phases[3].snapshot->covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     CompleteNonfinitePhase3RemainsCountedFailedEvidence) {
  ProductionFixture fixture = make_production_fixture();
  auto features = make_accepted_features(fixture, 0x43503250334eULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kPhase3Nonfinite);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  EXPECT_NO_THROW(updater->update(fixture.state, features));

  ASSERT_EQ(sink->call_count, 1U);
  ASSERT_TRUE(sink->record);
  const ov_msckf::CP2RecordedUpdateEvent &record = *sink->record;
  expect_recorded_phase_population(
      record, {ov_msckf::CP2StatePhase::kPhase0Prior,
               ov_msckf::CP2StatePhase::kPhase1Precommit,
               ov_msckf::CP2StatePhase::kPhase2ExpectedPostcommit,
               ov_msckf::CP2StatePhase::kPhase3LivePostcommit});
  EXPECT_EQ(record.update.terminal_status,
            ov_msckf::CP2UpdateTerminalStatus::kCommittedCounted);
  EXPECT_TRUE(record.update.baseline_commit_occurred);
  EXPECT_EQ(record.baseline_commit_oracle.status,
            ov_msckf::CP2CommitOracleStatus::kComplete);
  EXPECT_FALSE(record.baseline_commit_oracle.phase3_valid);
  EXPECT_FALSE(record.baseline_commit_oracle.complete_finite);
  EXPECT_FALSE(record.baseline_commit_oracle.passed);
  EXPECT_EQ(record.baseline_commit_oracle.baseline_covariance_mismatches, 1U);
  EXPECT_TRUE(record.baseline_commit_mismatch);
  EXPECT_FALSE(record.online_math_evidence_passed);
  EXPECT_TRUE(std::isnan(
      record.state_phases[3].snapshot->covariance(0, 0)));
  EXPECT_FALSE(updater->cp2_trace_fatal_latched());
}

TEST(CP2UpdaterMSCKFTransaction,
     ZeroRawDurationFailureIsArithmeticFatalWithoutPublication) {
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kNoncommitDurationArithmeticFailure);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));
  std::vector<std::shared_ptr<ov_core::Feature>> features;

  try {
    updater->update(std::shared_ptr<ov_msckf::State>(), features);
    FAIL() << "unrepresentable zero-raw duration was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  }

  EXPECT_STREQ(ov_msckf::cp2_trace_fatal_reason_name(
                   ov_msckf::CP2TraceFatalReason::kArithmeticInvariant),
               "arithmetic_invariant");
  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
}

TEST(CP2UpdaterMSCKFTransaction,
     PhasePairDurationFailureIsArithmeticFatalWithoutPublicationOrWrite) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto feature = make_feature(fixture, 0x43503244324eULL, true);
  std::vector<std::shared_ptr<ov_core::Feature>> features{feature};
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kNoncommitDurationArithmeticFailure);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "unrepresentable phase-pair duration was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  expect_exact_state(fixture, before);
}

TEST(CP2UpdaterMSCKFTransaction,
     CommittedDurationFailureIsArithmeticFatalWithoutPublicationOrRollback) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x435032443343ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kCommittedDurationArithmeticFailure);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "unrepresentable committed duration was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     CommitOracleOverflowIsArithmeticFatalWithoutPublicationOrRollback) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x4350324f5646ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater,
      ov_msckf::CP2UpdaterTestFault::kCommitOracleArithmeticOverflow);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "overflowed commit oracle was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kArithmeticInvariant);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}

TEST(CP2UpdaterMSCKFTransaction,
     CommitOracleInvalidPhaseRemainsDistinctPostcommitFatal) {
  ProductionFixture fixture = make_production_fixture();
  const StateSnapshot before = snapshot(fixture);
  auto features = make_accepted_features(fixture, 0x4350324f4950ULL);
  auto updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto sink = std::make_shared<RecordingUpdateSink>();
  ov_msckf::CP2UpdaterTestAccess::SetFault(
      *updater, ov_msckf::CP2UpdaterTestFault::kCommitOracleInvalidPhase);
  ASSERT_TRUE(updater->set_cp2_recorded_sink(sink));
  ASSERT_TRUE(updater->set_cp2_invocation_context(invocation_context()));

  try {
    updater->update(fixture.state, features);
    FAIL() << "invalid-phase commit oracle was published";
  } catch (const ov_msckf::CP2TraceFatalError &error) {
    EXPECT_EQ(error.reason(),
              ov_msckf::CP2TraceFatalReason::kPostcommitException);
  }

  EXPECT_EQ(sink->call_count, 0U);
  EXPECT_FALSE(sink->record);
  EXPECT_TRUE(updater->cp2_trace_fatal_latched());
  EXPECT_EQ(updater->cp2_trace_fatal_reason(),
            ov_msckf::CP2TraceFatalReason::kPostcommitException);
  EXPECT_FALSE(bitwise_equal(
      before.covariance,
      ov_msckf::StateHelper::get_full_covariance(fixture.state)));
}
#endif

TEST(CP2UpdaterMSCKFTransaction,
     RecordedSinkConfigurationIsNullspaceOnlyAndFreezesAtFirstUpdate) {
  auto schur_updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::SCHUR);
  auto rejected_sink = std::make_shared<RecordingUpdateSink>();
  EXPECT_FALSE(schur_updater->set_cp2_recorded_sink(rejected_sink));
  EXPECT_FALSE(schur_updater->cp2_trace_fatal_latched());

  auto nullspace_updater =
      make_updater(ov_msckf::UpdaterOptions::LandmarkElimination::NULLSPACE);
  auto installed_sink = std::make_shared<RecordingUpdateSink>();
  ASSERT_TRUE(nullspace_updater->set_cp2_recorded_sink(installed_sink));
  ASSERT_TRUE(nullspace_updater->set_cp2_invocation_context(
      invocation_context()));
  std::vector<std::shared_ptr<ov_core::Feature>> features;
  nullspace_updater->update(std::shared_ptr<ov_msckf::State>(), features);
  EXPECT_EQ(installed_sink->call_count, 1U);
  EXPECT_FALSE(nullspace_updater->set_cp2_recorded_sink(nullptr));
  EXPECT_FALSE(nullspace_updater->set_cp2_recorded_sink(
      std::make_shared<RecordingUpdateSink>()));
}

} // namespace
