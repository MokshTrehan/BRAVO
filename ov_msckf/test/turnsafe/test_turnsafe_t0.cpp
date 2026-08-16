/*
 * TurnSafe Session-1 passive T0 tests.
 * SPDX-License-Identifier: GPL-3.0-or-later
 */

#include "cam/CamRadtan.h"
#include "core/TurnSafeCallbackInstaller.h"
#include "feat/Feature.h"
#include "feat/FeatureInitializer.h"
#include "track/TrackKLT.h"
#include "track/TrackKLTDiagnostics.h"
#include "state/Propagator.h"
#include "state/State.h"
#include "update/TurnSafeDiagnostics.h"
#include "update/UpdaterMSCKF.h"
#include "update/UpdaterMSCKFPreview.h"

#include <gtest/gtest.h>

#include <Eigen/Core>

#include <cstdio>
#include <algorithm>
#include <array>
#include <cmath>
#include <fstream>
#include <limits>
#include <memory>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>
#include <unistd.h>

namespace {

std::string TemporaryPath() {
  char pattern[] = "/tmp/turnsafe_t0_test_XXXXXX";
  const int descriptor = ::mkstemp(pattern);
  EXPECT_GE(descriptor, 0);
  if (descriptor >= 0) ::close(descriptor);
  ::unlink(pattern);
  return pattern;
}

std::string ReadAll(const std::string &path) {
  std::ifstream stream(path, std::ios::binary);
  std::ostringstream output;
  output << stream.rdbuf();
  return output.str();
}

ov_msckf::TurnSafeDiagnosticsOptions Options(const std::string &path) {
  ov_msckf::TurnSafeDiagnosticsOptions options;
  options.capture_requested = true;
  options.output_path = path;
  options.frozen_base_sha = "frozen";
  options.build_manifest_sha256 = "build";
  options.binary_sha256 = "binary";
  options.config_sha256 = "config";
  options.calibration_sha256 = "calibration";
  ov_msckf::TurnSafeResolvedConfiguration configuration;
  configuration.one_pass_schur = true;
  configuration.fej_enabled = true;
  configuration.global_3d_transient = true;
  configuration.all_cameras_radtan = true;
  configuration.camera_extrinsic_calibration_off = true;
  configuration.camera_intrinsic_calibration_off = true;
  configuration.camera_time_offset_calibration_off = true;
  configuration.stereo_enabled = true;
  configuration.stereo_available = true;
  configuration.require_target_stereo_range = true;
  configuration.camera_count = 2U;
  options.resolved_configuration =
      ov_msckf::TurnSafeDiagnostics::EvaluateConfiguration(configuration);
  return options;
}

std::size_t CountSubstring(const std::string &value,
                           const std::string &needle) {
  std::size_t count = 0U;
  std::size_t position = 0U;
  while ((position = value.find(needle, position)) != std::string::npos) {
    ++count;
    position += needle.size();
  }
  return count;
}

ov_msckf::TurnSafeObservationValue Observation(
    std::size_t camera_id, double timestamp, std::size_t ordinal,
    double u, double v, bool clone_available = true) {
  ov_msckf::TurnSafeObservationValue observation;
  observation.camera_id = camera_id;
  observation.timestamp = timestamp;
  observation.baseline_observation_index = ordinal;
  observation.uv_available = true;
  observation.uv = {{u, v}};
  observation.uv_normalized_available = true;
  observation.uv_normalized = {{u / 100.0, v / 100.0}};
  observation.clone_available = clone_available;
  return observation;
}

ov_msckf::TurnSafeFullTrackAttempt PairAttempt(
    std::uint64_t feature_id, std::uint64_t detached_index,
    double source = 1.0, double target = 2.0,
    ov_msckf::TurnSafeFullOutcome outcome =
        ov_msckf::TurnSafeFullOutcome::kInitTooFar) {
  ov_msckf::TurnSafeFullTrackAttempt attempt;
  attempt.feature_id = feature_id;
  attempt.detached_index = detached_index;
  attempt.full_outcome = outcome;
  attempt.full_outcome_mapping = ov_msckf::TurnSafeOutcomeMapping::kExact;
  attempt.ordered_observations = {
      Observation(0U, source, 0U, 10.0 + feature_id, 20.0),
      Observation(0U, target, 1U, 11.0 + feature_id, 21.0),
      Observation(1U, target, 2U, 12.0 + feature_id, 22.0),
  };
  attempt.attempt_has_any_live_same_camera_clone_pair = true;
  attempt.valid_clone_pair_count = 1U;
  attempt.target_time_stereo_available = true;
  return attempt;
}

std::string SerializeAttempts(
    std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts,
    double timestamp = 10.0) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  EXPECT_TRUE(sink && sink->active());
  if (!sink || !sink->active()) return std::string();
  sink->BeginCallback(timestamp);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = timestamp;
  update.native_terminal_status = "TEST";
  update.attempts = std::move(attempts);
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
  EXPECT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  ::unlink(path.c_str());
  return bytes;
}

ov_msckf::MSCKFUpdatePriorSnapshot MinimalPrior() {
  ov_msckf::MSCKFUpdatePriorSnapshot prior;
  prior.timestamp = 2.0;
  prior.filter.covariance = Eigen::MatrixXd::Identity(12, 12);
  prior.filter.state_blocks = {{0, 6, 0}, {6, 6, 6}};
  for (int index = 0; index < 2; ++index) {
    ov_msckf::MSCKFUpdatePriorNominalBlock block;
    block.covariance_id = index * 6;
    block.size = 6;
    block.value = Eigen::MatrixXd::Zero(7, 1);
    block.fej = Eigen::MatrixXd::Zero(7, 1);
    block.value(3, 0) = 1.0;
    block.fej(3, 0) = 1.0;
    prior.nominal_blocks.push_back(block);
  }
  prior.clone_bindings = {{1.0, 0}, {2.0, 6}};
  for (std::size_t camera_id = 0U; camera_id < 2U; ++camera_id) {
    ov_msckf::MSCKFUpdatePriorCamera camera;
    camera.camera_id = camera_id;
    camera.extrinsic_id = static_cast<Eigen::Index>(camera_id * 2U);
    camera.intrinsic_id = static_cast<Eigen::Index>(camera_id * 2U + 1U);
    camera.extrinsic_value = Eigen::MatrixXd::Zero(7, 1);
    camera.extrinsic_fej = camera.extrinsic_value;
    camera.intrinsic_value = Eigen::MatrixXd::Zero(8, 1);
    camera.intrinsic_fej = camera.intrinsic_value;
    camera.cache_value = Eigen::MatrixXd::Identity(3, 3);
    camera.width = 100;
    camera.height = 80;
    camera.model = ov_msckf::MSCKFUpdatePriorCameraModel::kRadtan;
    prior.cameras.push_back(camera);
  }
  return prior;
}

ov_msckf::TurnSafeImuSupportSnapshot ImuSupport(
    const std::vector<double> &timestamps,
    const std::vector<std::array<double, 3U>> &omega) {
  EXPECT_EQ(timestamps.size(), omega.size());
  ov_msckf::TurnSafeImuSupportSnapshot support;
  support.available = true;
  support.reason = "NONE";
  for (std::size_t index = 0U;
       index < timestamps.size() && index < omega.size(); ++index) {
    ov_msckf::TurnSafeImuSampleValue sample;
    sample.timestamp = timestamps[index];
    sample.omega_rad_s = omega[index];
    support.samples.push_back(sample);
  }
  return support;
}

ov_msckf::MSCKFUpdatePriorSnapshot EventReadyPrior(
    const std::vector<double> &timestamps) {
  ov_msckf::MSCKFUpdatePriorSnapshot prior;
  prior.timestamp = timestamps.empty() ? 0.0 : timestamps.back();
  const Eigen::Index dimension =
      static_cast<Eigen::Index>(timestamps.size() * 6U);
  prior.filter.covariance = Eigen::MatrixXd::Identity(dimension, dimension);
  for (std::size_t index = 0U; index < timestamps.size(); ++index) {
    const Eigen::Index covariance_id =
        static_cast<Eigen::Index>(index * 6U);
    prior.filter.state_blocks.push_back({covariance_id, 6, covariance_id});
    ov_msckf::MSCKFUpdatePriorNominalBlock block;
    block.covariance_id = covariance_id;
    block.size = 6;
    block.value = Eigen::MatrixXd::Zero(7, 1);
    block.fej = Eigen::MatrixXd::Zero(7, 1);
    const double angle = 0.05 * static_cast<double>(index + 1U);
    block.value(2, 0) = std::sin(0.5 * angle);
    block.value(3, 0) = std::cos(0.5 * angle);
    block.fej(1, 0) = std::sin(0.5 * (angle + 0.01));
    block.fej(3, 0) = std::cos(0.5 * (angle + 0.01));
    prior.nominal_blocks.push_back(block);
    prior.clone_bindings.push_back({timestamps[index], covariance_id});
  }
  for (std::size_t camera_id = 0U; camera_id < 2U; ++camera_id) {
    ov_msckf::MSCKFUpdatePriorCamera camera;
    camera.camera_id = camera_id;
    camera.extrinsic_id = -1;
    camera.intrinsic_id = -1;
    camera.extrinsic_value = Eigen::MatrixXd::Zero(7, 1);
    camera.extrinsic_value(2, 0) =
        std::sin(0.025 * static_cast<double>(camera_id));
    camera.extrinsic_value(3, 0) =
        std::cos(0.025 * static_cast<double>(camera_id));
    camera.extrinsic_fej = camera.extrinsic_value;
    camera.intrinsic_value = Eigen::MatrixXd::Zero(8, 1);
    camera.intrinsic_value <<
        100.0 + 10.0 * camera_id, 101.0 + 10.0 * camera_id,
        50.0 + camera_id, 40.0 + camera_id,
        0.01 * (camera_id + 1U), -0.001 * (camera_id + 1U),
        0.0001 * (camera_id + 1U), -0.0002 * (camera_id + 1U);
    camera.intrinsic_fej = camera.intrinsic_value;
    camera.cache_value = Eigen::MatrixXd::Identity(3, 3);
    camera.width = camera_id == 0U ? 100 : 120;
    camera.height = camera_id == 0U ? 80 : 90;
    camera.model = ov_msckf::MSCKFUpdatePriorCameraModel::kRadtan;
    prior.cameras.push_back(camera);
  }
  return prior;
}

std::string SerializeEventGroups(
    std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts,
    const std::vector<double> &clone_timestamps) {
  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.capture_group_bearing_provenance = true;
  options.run_identity = "opaque-test-run";
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  EXPECT_TRUE(sink && sink->active());
  if (!sink || !sink->active()) return std::string();
  ov_msckf::TurnSafePriorPrimitives prior;
  ov_msckf::TurnSafeDiagnostics::CapturePriorPrimitives(
      EventReadyPrior(clone_timestamps), attempts, prior);
  EXPECT_TRUE(prior.available);
  sink->BeginCallback(10.0);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 10.0;
  update.native_terminal_status = "TEST";
  update.attempts = std::move(attempts);
  update.prior = std::move(prior);
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
  EXPECT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  ::unlink(path.c_str());
  return bytes;
}

class FaultReset {
public:
  ~FaultReset() {
    ov_core::clear_track_klt_diagnostic_fault_for_test();
    ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
  }
};

class TrackKLTTestPeer : public ov_core::TrackKLT {
public:
  TrackKLTTestPeer()
      : TrackKLT(Cameras(), 40, 0, false, ov_core::TrackBase::NONE,
                 10, 4, 4, 5) {}

  static std::unordered_map<std::size_t, std::shared_ptr<ov_core::CamBase>>
  Cameras() {
    auto camera = std::make_shared<ov_core::CamRadtan>(100, 100);
    Eigen::VectorXd calibration(8);
    calibration << 100.0, 100.0, 50.0, 50.0, 0.0, 0.0, 0.0, 0.0;
    camera->set_value(calibration);
    return {{0U, camera}};
  }

  bool Begin(double timestamp, ov_core::TrackKLTFrameDiagnostics &record) {
    return begin_turnsafe_diagnostic_frame(0U, timestamp, record);
  }

  void Finish(ov_core::TrackKLTFrameDiagnostics &&record,
              const ov_core::TrackKLTMatchingDiagnostics &matching,
              const std::vector<cv::KeyPoint> &points,
              const cv::Mat &mask,
              const std::vector<std::size_t> &accepted_ids) {
    finish_turnsafe_diagnostic_frame(
        std::move(record), matching, points, 100, 100, mask, true,
        ov_core::TrackKLTBoundsRule::kGreaterEqual, accepted_ids,
        accepted_ids.size(), 0U, false,
        ov_core::TrackKLTNativeReason::kNoReseed);
  }

  void Match(const std::vector<cv::Mat> &pyramid,
             std::vector<cv::KeyPoint> &source,
             std::vector<cv::KeyPoint> &target,
             std::vector<uchar> &mask,
             ov_core::TrackKLTMatchingDiagnostics *diagnostics) {
    perform_matching(pyramid, pyramid, source, target, 0U, 0U, mask,
                     diagnostics);
  }
};

struct NativeKltOracle {
  std::vector<uchar> mask;
  std::vector<std::size_t> accepted_ids;
};

NativeKltOracle CaptureNativeKltOracle() {
  cv::Mat image(100, 100, CV_8UC1);
  for (int row = 0; row < image.rows; ++row) {
    for (int col = 0; col < image.cols; ++col) {
      image.at<std::uint8_t>(row, col) =
          static_cast<std::uint8_t>((row * 17 + col * 31) % 251);
    }
  }
  std::vector<cv::Mat> pyramid;
  cv::buildOpticalFlowPyramid(image, pyramid, cv::Size(15, 15), 5);
  std::vector<cv::KeyPoint> source;
  for (int y = 15; y <= 75; y += 15) {
    for (int x = 15; x <= 75; x += 15) {
      source.emplace_back(static_cast<float>(x), static_cast<float>(y), 1.0F);
    }
  }
  auto target = source;
  NativeKltOracle result;
  TrackKLTTestPeer tracker;
  tracker.Match(pyramid, source, target, result.mask, nullptr);
  for (std::size_t index = 0U; index < result.mask.size(); ++index) {
    if (result.mask[index] != 0U)
      result.accepted_ids.push_back(1000U + index);
  }
  return result;
}

void ExpectNativeKltEquals(const NativeKltOracle &expected) {
  const NativeKltOracle actual = CaptureNativeKltOracle();
  EXPECT_EQ(actual.mask, expected.mask);
  EXPECT_EQ(actual.accepted_ids, expected.accepted_ids);
}

void WriteMinimalCallback(
    const std::shared_ptr<ov_msckf::TurnSafeDiagnostics> &sink) {
  sink->BeginCallback(12.5);
  ov_core::TrackKLTFrameDiagnostics frontend;
  frontend.camera_id = 0U;
  frontend.target_timestamp = 12.5;
  frontend.previous_track_count = 4U;
  frontend.klt_attempted_count = 4U;
  frontend.klt_counts_available = true;
  frontend.klt_status_survivors = 3U;
  frontend.klt_status_rejections = 1U;
  frontend.in_bounds_survivors = 2U;
  frontend.mask_survivors = 2U;
  frontend.ransac_counts_available = true;
  frontend.fmatrix_input_points = 4U;
  frontend.fmatrix_inliers = 2U;
  frontend.fmatrix_rejections = 2U;
  frontend.surviving_klt_errors =
      {1.0F, std::numeric_limits<float>::quiet_NaN(), 3.0F};
  sink->RecordFrontend(std::move(frontend));

  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 12.5;
  update.native_terminal_status = "all_rejected";
  update.native_terminal_subreason = "no_features_after_triangulation";
  update.input_feature_count = 2U;
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
}

} // namespace

TEST(TurnSafeT0, CaptureDefaultsEntirelyOff) {
  ov_msckf::TurnSafeDiagnosticsOptions options;
  EXPECT_FALSE(options.capture_requested);
  EXPECT_TRUE(options.output_path.empty());
  EXPECT_EQ(ov_msckf::TurnSafeDiagnostics::Create(options), nullptr);
  options.capture_requested = true;
  EXPECT_EQ(ov_msckf::TurnSafeDiagnostics::Create(options), nullptr);
}

TEST(TurnSafeT0, InvalidAndUnsupportedPathsDisableOnlyTheSink) {
  auto invalid = Options("/proc/turnsafe_t0_forbidden/output.jsonl");
  const auto invalid_sink =
      ov_msckf::TurnSafeDiagnostics::Create(invalid);
  ASSERT_NE(invalid_sink, nullptr);
  EXPECT_FALSE(invalid_sink->active());
  EXPECT_STREQ(invalid_sink->failure_reason(), "OUTPUT_OPEN_FAILED");

  auto unsupported = Options(TemporaryPath());
  unsupported.schema_version = "turnsafe.t0.unsupported";
  const auto unsupported_sink =
      ov_msckf::TurnSafeDiagnostics::Create(unsupported);
  ASSERT_NE(unsupported_sink, nullptr);
  EXPECT_FALSE(unsupported_sink->active());
  EXPECT_STREQ(unsupported_sink->failure_reason(),
               "UNSUPPORTED_SCHEMA_VERSION");
}

TEST(TurnSafeT0, ExtensionFlagsRequireActivePassiveCapture) {
  for (int flag = 0; flag < 3; ++flag) {
    ov_msckf::TurnSafeDiagnosticsOptions options;
    if (flag == 0) options.capture_causal_imu_intervals = true;
    if (flag == 1) options.capture_outcome_association_keys = true;
    if (flag == 2) options.capture_group_bearing_provenance = true;
    const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
    ASSERT_TRUE(sink);
    EXPECT_FALSE(sink->active());
    EXPECT_STREQ(sink->failure_reason(),
                 "EVENT_EXTENSION_REQUIRES_ACTIVE_PASSIVE_CAPTURE");
    EXPECT_EQ(sink->failure_count(), 1U);
  }

  const std::string path = TemporaryPath();
  auto unsupported = Options(path);
  unsupported.capture_causal_imu_intervals = true;
  unsupported.event_extension_schema_version =
      "turnsafe.t0.event_extension.unsupported";
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(unsupported);
  ASSERT_TRUE(sink);
  EXPECT_FALSE(sink->active());
  EXPECT_STREQ(sink->failure_reason(), "UNSUPPORTED_SCHEMA_VERSION");
  ::unlink((path + ".tmp").c_str());
}

TEST(TurnSafeT0, CausalImuConstantRateInterpolationBiasAndSo3AreExact) {
  const auto support = ImuSupport(
      {0.0, 1.0, 2.0},
      {{{2.0, 0.0, 0.0}}, {{2.0, 0.0, 0.0}},
       {{2.0, 0.0, 0.0}}});
  const auto interval =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          7U, {1, 0}, true, 0.5, 1.5, false, true, 0.0, true,
          {{0.5, 0.0, 0.0}}, support);
  ASSERT_TRUE(interval.available) << interval.reason;
  EXPECT_EQ(interval.gyro_interval_id, 7U);
  EXPECT_EQ(interval.camera_frame_ids, (std::vector<int>{0, 1}));
  EXPECT_EQ(interval.start_endpoint.status, "LINEAR_INTERPOLATED");
  EXPECT_EQ(interval.end_endpoint.status, "LINEAR_INTERPOLATED");
  EXPECT_EQ(interval.source_sample_count, 3U);
  ASSERT_EQ(interval.knot_timestamps_s.size(), 3U);
  EXPECT_DOUBLE_EQ(interval.knot_timestamps_s.front(), 0.5);
  EXPECT_DOUBLE_EQ(interval.knot_timestamps_s.back(), 1.5);
  EXPECT_DOUBLE_EQ(interval.duration_s, 1.0);
  EXPECT_DOUBLE_EQ(interval.coverage_fraction, 1.0);
  EXPECT_DOUBLE_EQ(interval.maximum_internal_gap_s, 0.5);
  ASSERT_TRUE(interval.raw_summary.available);
  EXPECT_DOUBLE_EQ(interval.raw_summary.max_norm_rad_s, 2.0);
  EXPECT_DOUBLE_EQ(interval.raw_summary.rms_norm_rad_s, 2.0);
  EXPECT_DOUBLE_EQ(interval.raw_summary.mean_omega_rad_s[0], 2.0);
  EXPECT_DOUBLE_EQ(interval.raw_summary.integral_norm_rad, 2.0);
  EXPECT_DOUBLE_EQ(interval.raw_summary.integral_omega_rad[0], 2.0);
  EXPECT_NEAR(interval.raw_summary.delta_rotation_angle_rad, 2.0, 1.0e-14);
  ASSERT_TRUE(interval.raw_summary.delta_rotation_axis_available);
  EXPECT_NEAR(interval.raw_summary.delta_rotation_axis[0], -1.0, 1.0e-14);
  ASSERT_TRUE(interval.bias_corrected_summary.available);
  EXPECT_DOUBLE_EQ(interval.bias_corrected_summary.max_norm_rad_s, 1.5);
  EXPECT_DOUBLE_EQ(interval.bias_corrected_summary.rms_norm_rad_s, 1.5);
  EXPECT_DOUBLE_EQ(interval.bias_corrected_summary.integral_norm_rad, 1.5);
  EXPECT_NEAR(interval.bias_corrected_summary.delta_rotation_angle_rad,
              1.5, 1.0e-14);
}

TEST(TurnSafeT0, CausalImuRampIrregularCadenceAndOrderAreDeterministic) {
  const auto ramp = ImuSupport(
      {0.0, 0.4, 1.0, 2.0},
      {{{0.0, 0.0, 0.0}}, {{0.4, 0.0, 0.0}},
       {{1.0, 0.0, 0.0}}, {{2.0, 0.0, 0.0}}});
  const auto first =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          8U, {0}, true, 0.2, 1.5, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, ramp);
  const auto second =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          8U, {0}, true, 0.2, 1.5, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, ramp);
  ASSERT_TRUE(first.available) << first.reason;
  ASSERT_TRUE(second.available) << second.reason;
  EXPECT_NEAR(first.raw_summary.integral_omega_rad[0],
              0.5 * (1.5 * 1.5 - 0.2 * 0.2), 1.0e-15);
  EXPECT_NEAR(first.raw_summary.integral_norm_rad,
              first.raw_summary.integral_omega_rad[0], 1.0e-15);
  EXPECT_EQ(first.knot_timestamps_s, second.knot_timestamps_s);
  EXPECT_EQ(first.raw_omega_rad_s, second.raw_omega_rad_s);
  EXPECT_EQ(first.raw_summary.delta_rotation_matrix_row_major,
            second.raw_summary.delta_rotation_matrix_row_major);

  auto regressed = ramp;
  std::swap(regressed.samples[1], regressed.samples[2]);
  const auto rejected =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          8U, {0}, true, 0.2, 1.5, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, regressed);
  EXPECT_FALSE(rejected.available);
  EXPECT_EQ(rejected.reason, "IMU_TIMESTAMP_NOT_STRICTLY_INCREASING");
}

TEST(TurnSafeT0, CausalImuMissingnessAndGapReasonsAreTyped) {
  const auto support = ImuSupport(
      {1.0, 3.0},
      {{{0.0, 1.0, 0.0}}, {{0.0, 1.0, 0.0}}});
  const auto first =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          0U, {0}, false, 0.0, 2.0, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  EXPECT_FALSE(first.available);
  EXPECT_EQ(first.reason, "FIRST_CALLBACK");

  const auto after_initialization =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          1U, {0}, true, 1.0, 2.0, true, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  EXPECT_FALSE(after_initialization.available);
  EXPECT_EQ(after_initialization.reason,
            "FIRST_CALLBACK_AFTER_INITIALIZATION");

  const auto regression =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          2U, {0}, true, 2.0, 1.0, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  EXPECT_FALSE(regression.available);
  EXPECT_EQ(regression.reason, "CALLBACK_TIMESTAMP_REGRESSION");

  const auto missing_start =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          3U, {0}, true, 0.0, 2.0, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  EXPECT_FALSE(missing_start.available);
  EXPECT_EQ(missing_start.reason, "START_ENDPOINT_UNSUPPORTED");
  EXPECT_EQ(missing_start.start_endpoint.status, "UNSUPPORTED");
  EXPECT_GT(missing_start.source_sample_count, 0U);

  const auto missing_end =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          4U, {0}, true, 2.0, 4.0, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  EXPECT_FALSE(missing_end.available);
  EXPECT_EQ(missing_end.reason, "END_ENDPOINT_UNSUPPORTED");
  EXPECT_EQ(missing_end.end_endpoint.status, "UNSUPPORTED");

  const auto large_gap =
      ov_msckf::TurnSafeDiagnostics::EvaluateCausalImuInterval(
          5U, {0}, true, 1.0, 3.0, false, true, 0.0, true,
          {{0.0, 0.0, 0.0}}, support);
  ASSERT_TRUE(large_gap.available) << large_gap.reason;
  EXPECT_DOUBLE_EQ(large_gap.maximum_internal_gap_s, 2.0);
  EXPECT_DOUBLE_EQ(large_gap.coverage_fraction, 1.0);
}

TEST(TurnSafeT0, SerializationIsDeterministicFiniteAndSequenceFree) {
  const std::string first_path = TemporaryPath();
  const std::string second_path = TemporaryPath();
  const auto first =
      ov_msckf::TurnSafeDiagnostics::Create(Options(first_path));
  const auto second =
      ov_msckf::TurnSafeDiagnostics::Create(Options(second_path));
  ASSERT_TRUE(first && first->active());
  ASSERT_TRUE(second && second->active());
  WriteMinimalCallback(first);
  WriteMinimalCallback(second);
  ASSERT_TRUE(first->Finalize());
  ASSERT_TRUE(second->Finalize());
  const std::string first_bytes = ReadAll(first_path);
  const std::string second_bytes = ReadAll(second_path);
  EXPECT_EQ(first_bytes, second_bytes);
  EXPECT_EQ(first_bytes.find("NaN"), std::string::npos);
  EXPECT_EQ(first_bytes.find("Infinity"), std::string::npos);
  EXPECT_EQ(first_bytes.find("sequence_id"), std::string::npos);
  EXPECT_EQ(first_bytes.find("ground_truth"), std::string::npos);
  EXPECT_NE(first_bytes.find("\"nonfinite_count\":1"),
            std::string::npos);
  ::unlink(first_path.c_str());
  ::unlink(second_path.c_str());
}

TEST(TurnSafeT0, StereoFramesShareOneCanonicalCallbackInterval) {
  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.capture_causal_imu_intervals = true;
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  ASSERT_TRUE(sink && sink->active());
  sink->BeginCallback(1.0, {1, 0}, false, false, nullptr, nullptr);
  ov_core::TrackKLTFrameDiagnostics camera_one;
  camera_one.camera_id = 1U;
  camera_one.target_timestamp = 1.0;
  sink->RecordFrontend(std::move(camera_one));
  ov_core::TrackKLTFrameDiagnostics camera_zero;
  camera_zero.camera_id = 0U;
  camera_zero.target_timestamp = 1.0;
  sink->RecordFrontend(std::move(camera_zero));
  sink->EndCallback(false, false, nullptr);
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_EQ(CountSubstring(bytes, "\"causal_imu_interval\""), 1U);
  EXPECT_EQ(CountSubstring(bytes, "\"gyro_interval_id\":0"), 3U);
  EXPECT_NE(bytes.find("\"camera_frame_ids\":[0,1]"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"reason\":\"FIRST_CALLBACK\""),
            std::string::npos);
  const std::size_t camera_zero_position =
      bytes.find("\"camera_id\":0");
  const std::size_t camera_one_position =
      bytes.find("\"camera_id\":1");
  EXPECT_LT(camera_zero_position, camera_one_position);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, FirstCallbackAfterInitializationTransitionIsUnavailable) {
  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.capture_causal_imu_intervals = true;
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  ASSERT_TRUE(sink && sink->active());

  ov_msckf::StateOptions state_options;
  ov_msckf::State state(state_options);
  Eigen::VectorXd offset(1);
  offset << 0.0;
  state._calib_dt_CAMtoIMU->set_value(offset);
  ov_msckf::Propagator propagator(ov_msckf::NoiseManager(), 9.81);
  for (int index = 0; index <= 4; ++index) {
    ov_core::ImuData sample;
    sample.timestamp = static_cast<double>(index);
    sample.wm = Eigen::Vector3d(0.0, 0.0, 1.0);
    sample.am = Eigen::Vector3d::Zero();
    propagator.feed_imu(sample);
  }

  sink->BeginCallback(1.0, {0, 1}, false, false, &state, &propagator);
  sink->EndCallback(true, true, &state);
  sink->BeginCallback(2.0, {0, 1}, true, true, &state, &propagator);
  sink->EndCallback(true, true, &state);
  sink->BeginCallback(3.0, {0, 1}, true, true, &state, &propagator);
  sink->EndCallback(true, true, &state);
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_NE(bytes.find(
                "\"reason\":\"FIRST_CALLBACK_AFTER_INITIALIZATION\""),
            std::string::npos);
  EXPECT_NE(bytes.find(
                "\"gyro_interval_id\":2,\"callback_id\":2,\"status\":\"AVAILABLE\""),
            std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, CallbackAssociationKeysAreCausalStableAndReferenceFree) {
  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.capture_outcome_association_keys = true;
  options.run_identity = "opaque-run-001";
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  ASSERT_TRUE(sink && sink->active());

  ov_msckf::StateOptions state_options;
  ov_msckf::State state(state_options);
  state._timestamp = 4.0;
  Eigen::VectorXd offset(1);
  offset << 0.125;
  state._calib_dt_CAMtoIMU->set_value(offset);
  sink->BeginCallback(4.0, {0, 1}, true, true, &state, nullptr);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 4.0;
  update.baseline_commit_occurred = true;
  update.baseline_accepted_ids = {17U};
  auto attempt = PairAttempt(17U, 0U);
  attempt.accepted_full_factor = true;
  update.attempts.push_back(attempt);
  sink->RecordUpdate(std::move(update));
  sink->EndCallback(true, true, &state, false);
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_NE(bytes.find("\"outcome_association_keys\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"expected_state_row_key\":{\"status\":\"AVAILABLE\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"timestamp_value\":4.125"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"ordinary_accepted_full_factor_count\":{\"status\":\"AVAILABLE\",\"value\":1,\"reason\":\"NONE\"}"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"ordinary_full_visual_update_accepted\":{\"status\":\"AVAILABLE\",\"value\":true,\"reason\":\"NONE\"}"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"run_identity\":\"opaque-run-001\""),
            std::string::npos);
  EXPECT_NE(bytes.find("REFERENCE_ASSOCIATION_OFFLINE_ONLY"),
            std::string::npos);
  EXPECT_EQ(bytes.find("ground_truth"), std::string::npos);
  EXPECT_EQ(bytes.find("reference_path"), std::string::npos);
  EXPECT_EQ(bytes.find("final_error"), std::string::npos);
  EXPECT_EQ(bytes.find("degraded_label"), std::string::npos);
  EXPECT_EQ(bytes.find("outcome_label"), std::string::npos);
  EXPECT_EQ(bytes.find("event_id"), std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, CallbackAssociationUnavailableReasonsAreTypedAndExact) {
  const auto serialize_case = [](bool initialized, bool output_ready,
                                 double state_timestamp, double offset,
                                 bool incomplete) {
    const std::string path = TemporaryPath();
    auto options = Options(path);
    options.capture_outcome_association_keys = true;
    const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
    EXPECT_TRUE(sink && sink->active());
    ov_msckf::StateOptions state_options;
    ov_msckf::State state(state_options);
    state._timestamp = state_timestamp;
    Eigen::VectorXd offset_value(1);
    offset_value << offset;
    state._calib_dt_CAMtoIMU->set_value(offset_value);
    sink->BeginCallback(4.0, {0, 1}, initialized, output_ready, &state,
                        nullptr);
    sink->EndCallback(initialized, output_ready, &state, incomplete);
    EXPECT_TRUE(sink->Finalize());
    const std::string bytes = ReadAll(path);
    ::unlink(path.c_str());
    return bytes;
  };

  const std::string not_ready =
      serialize_case(true, false, 4.0, 0.125, false);
  EXPECT_NE(not_ready.find("\"reason\":\"OUTPUT_NOT_READY\""),
            std::string::npos);
  EXPECT_NE(not_ready.find(
                "\"pose_stream_write_status\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"POSE_WRITE_STATUS_OFFLINE_ONLY\"}"),
            std::string::npos);
  EXPECT_NE(not_ready.find(
                "\"reset_status\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"RESET_STATUS_NOT_EXPOSED_BY_NATIVE_PATH\"}"),
            std::string::npos);
  EXPECT_NE(not_ready.find(
                "\"ordinary_accepted_full_factor_count\":{\"status\":\"NOT_AVAILABLE\",\"reason\":\"UPDATER_NOT_REACHED\"}"),
            std::string::npos);
  EXPECT_NE(not_ready.find(
                "\"ordinary_full_visual_update_accepted\":{\"status\":\"NOT_AVAILABLE\",\"reason\":\"UPDATER_NOT_REACHED\"}"),
            std::string::npos);

  const std::string uninitialized =
      serialize_case(false, false, 4.0, 0.125, false);
  EXPECT_NE(uninitialized.find("\"reason\":\"ESTIMATOR_NOT_INITIALIZED\""),
            std::string::npos);
  const std::string stale =
      serialize_case(true, true, 3.0, 0.125, false);
  EXPECT_NE(stale.find(
                "\"reason\":\"STATE_TIMESTAMP_NOT_CURRENT_CALLBACK\""),
            std::string::npos);
  const std::string incomplete =
      serialize_case(true, true, 4.0, 0.125, true);
  EXPECT_NE(incomplete.find("\"reason\":\"CALLBACK_INCOMPLETE\""),
            std::string::npos);
  EXPECT_NE(incomplete.find(
                "\"completion_reason\":\"EXCEPTION_SCOPE_EXIT\""),
            std::string::npos);
  const std::string nonfinite_state = serialize_case(
      true, true, std::numeric_limits<double>::quiet_NaN(), 0.125, false);
  EXPECT_NE(nonfinite_state.find("STATE_TIMESTAMP_NONFINITE"),
            std::string::npos);
  EXPECT_NE(nonfinite_state.find("\"reason\":\"ESTIMATOR_STATE_INVALID\""),
            std::string::npos);
  const std::string nonfinite_offset = serialize_case(
      true, true, 4.0, std::numeric_limits<double>::quiet_NaN(), false);
  EXPECT_NE(nonfinite_offset.find("\"reason\":\"IMU_TIME_OFFSET_NONFINITE\""),
            std::string::npos);
}

TEST(TurnSafeT0, EventGroupPopulationKeepsN2N3AndN4ButOmitsN1) {
  for (std::size_t count = 1U; count <= 4U; ++count) {
    std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts;
    for (std::size_t index = 0U; index < count; ++index) {
      attempts.push_back(PairAttempt(100U + index, index));
    }
    const std::string bytes = SerializeEventGroups(
        std::move(attempts), {1.0, 2.0});
    if (count == 1U) {
      EXPECT_NE(bytes.find("\"group_count\":0"), std::string::npos);
      EXPECT_EQ(bytes.find("\"member_count\":1"), std::string::npos);
    } else {
      EXPECT_NE(bytes.find("\"group_count\":1"), std::string::npos);
      EXPECT_NE(bytes.find("\"member_count\":" +
                           std::to_string(count)), std::string::npos);
    }
  }

  auto full_accepted = PairAttempt(
      201U, 0U, 1.0, 2.0,
      ov_msckf::TurnSafeFullOutcome::kFullAccepted);
  auto full_nis_rejected = PairAttempt(
      202U, 1U, 1.0, 2.0,
      ov_msckf::TurnSafeFullOutcome::kFullNisRejected);
  const std::string rejected = SerializeEventGroups(
      {full_accepted, full_nis_rejected}, {1.0, 2.0});
  EXPECT_NE(rejected.find("\"group_count\":0"), std::string::npos);
}

TEST(TurnSafeT0, EventGroupProvenanceIsCompactGeometricAndPermutationStable) {
  std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts{
      PairAttempt(301U, 0U), PairAttempt(302U, 1U),
      PairAttempt(303U, 2U)};
  const std::string canonical =
      SerializeEventGroups(attempts, {1.0, 2.0});
  std::reverse(attempts.begin(), attempts.end());
  const std::string permuted =
      SerializeEventGroups(attempts, {1.0, 2.0});
  EXPECT_EQ(canonical, permuted);
  EXPECT_EQ(CountSubstring(canonical, "\"intrinsics_distortion_hash\""),
            2U);  // primary and shared target-stereo calibration
  EXPECT_EQ(CountSubstring(canonical, "\"source_current_R_GtoI\""), 1U);
  EXPECT_EQ(CountSubstring(canonical, "\"target_current_R_GtoI\""), 1U);
  EXPECT_EQ(CountSubstring(canonical, "\"members\""), 1U);
  EXPECT_EQ(CountSubstring(canonical, "\"source_unit_bearing\""), 3U);
  EXPECT_EQ(CountSubstring(canonical, "\"target_unit_bearing\""), 3U);
  EXPECT_EQ(CountSubstring(canonical, "\"target_stereo_raw_pixel\""), 3U);
  EXPECT_EQ(CountSubstring(canonical,
                           "direct_native_normalized_unit_ray.v1"),
            1U);
  EXPECT_EQ(CountSubstring(canonical, "continuous_pixel_center_fraction.v1"),
            10U);  // header plus three cells for each of three members
  EXPECT_NE(canonical.find("\"finite_validation\":{\"status\":\"AVAILABLE\""),
            std::string::npos);
  EXPECT_EQ(canonical.find("consensus_accept"), std::string::npos);
  EXPECT_EQ(canonical.find("spatial_accept"), std::string::npos);
  EXPECT_EQ(canonical.find("conditioning_accept"), std::string::npos);
}

TEST(TurnSafeT0, EventGroupsRetainDistinctTwoCameraCalibrationIdentity) {
  std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts{
      PairAttempt(401U, 0U), PairAttempt(402U, 1U)};
  for (std::uint64_t index = 0U; index < 2U; ++index) {
    auto swapped = PairAttempt(410U + index, 2U + index);
    swapped.ordered_observations = {
        Observation(1U, 1.0, 0U, 20.0 + index, 30.0),
        Observation(1U, 2.0, 1U, 21.0 + index, 31.0),
        Observation(0U, 2.0, 2U, 22.0 + index, 32.0),
    };
    attempts.push_back(swapped);
  }
  const std::string bytes = SerializeEventGroups(attempts, {1.0, 2.0});
  EXPECT_NE(bytes.find("\"group_count\":2"), std::string::npos);
  EXPECT_NE(bytes.find("\"image_width\":100,\"image_height\":80"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"image_width\":120,\"image_height\":90"),
            std::string::npos);
  EXPECT_EQ(CountSubstring(bytes, "\"intrinsics_distortion_hash\""), 4U);
  EXPECT_EQ(CountSubstring(bytes, "\"camera_extrinsic_hash\""), 4U);
  EXPECT_EQ(CountSubstring(bytes, "\"fixed_R_ItoC\""), 2U);
}

TEST(TurnSafeT0, InjectedWriteFailureCannotPublishAClaimedFinalFile) {
  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.test_fail_after_callback_records = 1U;
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  ASSERT_TRUE(sink && sink->active());
  WriteMinimalCallback(sink);
  WriteMinimalCallback(sink);
  EXPECT_FALSE(sink->Finalize());
  EXPECT_STREQ(sink->failure_reason(), "INJECTED_WRITE_FAILURE");
  EXPECT_NE(::access(path.c_str(), F_OK), 0);
  ::unlink((path + ".tmp").c_str());
}

TEST(TurnSafeT0, TrackStageAccountingKeepsNativeMasksIndependent) {
  ov_core::TrackKLTMatchingDiagnostics matching;
  matching.temporal_input_points = 5U;
  matching.klt_performed = true;
  matching.ransac_performed = true;
  matching.klt_status = {1U, 0U, 1U, 1U, 1U};
  matching.ransac_status = {0U, 1U, 1U, 0U, 1U};
  matching.klt_error = {1.0F, 2.0F, 3.0F, 4.0F, 5.0F};
  const std::vector<std::uint8_t> original_klt = matching.klt_status;
  const std::vector<std::uint8_t> original_ransac = matching.ransac_status;
  const std::vector<cv::KeyPoint> points = {
      cv::KeyPoint(5.0F, 5.0F, 1.0F),
      cv::KeyPoint(6.0F, 6.0F, 1.0F),
      cv::KeyPoint(-1.0F, 7.0F, 1.0F),
      cv::KeyPoint(8.0F, 8.0F, 1.0F),
      cv::KeyPoint(9.0F, 9.0F, 1.0F)};
  cv::Mat mask = cv::Mat::zeros(10, 10, CV_8UC1);
  mask.at<std::uint8_t>(8, 8) = 255U;
  const auto summary = ov_core::summarize_track_klt_diagnostics(
      matching, points, 10, 10, mask, true,
      ov_core::TrackKLTBoundsRule::kGreaterEqual);
  EXPECT_EQ(summary.klt_attempted_count, 5U);
  EXPECT_EQ(summary.klt_status_survivors, 4U);
  EXPECT_EQ(summary.klt_status_rejections, 1U);
  EXPECT_EQ(summary.out_of_bounds_rejections, 1U);
  EXPECT_EQ(summary.in_bounds_survivors, 3U);
  EXPECT_EQ(summary.mask_rejections, 1U);
  EXPECT_EQ(summary.mask_survivors, 2U);
  EXPECT_EQ(summary.fmatrix_input_points, 5U);
  EXPECT_EQ(summary.fmatrix_inliers, 3U);
  EXPECT_EQ(summary.fmatrix_rejections, 2U);
  EXPECT_EQ(summary.native_combined_track_survivors, 1U);
  EXPECT_EQ(matching.klt_status, original_klt);
  EXPECT_EQ(matching.ransac_status, original_ransac);
}

TEST(TurnSafeT0, DetachedAttemptOwnsObservationValuesAfterFeatureRemoval) {
  ov_core::Feature feature;
  feature.featid = 42U;
  // Insert the higher camera first: CaptureAttempt must not inherit
  // unordered_map iteration order in its canonical observation ordinals.
  feature.timestamps[5U] = {3.0};
  feature.uvs[5U] = {Eigen::Vector2f(30.0F, 31.0F)};
  feature.uvs_norm[5U] = {Eigen::Vector2f(0.5F, 0.6F)};
  feature.timestamps[0U] = {1.0, 2.0};
  feature.uvs[0U] = {Eigen::Vector2f(10.0F, 11.0F),
                     Eigen::Vector2f(12.0F, 13.0F)};
  feature.uvs_norm[0U] = {Eigen::Vector2f(0.1F, 0.2F),
                          Eigen::Vector2f(0.3F, 0.4F)};
  const auto attempt = ov_msckf::TurnSafeDiagnostics::CaptureAttempt(
      feature, 7U);
  feature.timestamps.clear();
  feature.uvs.clear();
  EXPECT_EQ(attempt.detached_index, 7U);
  EXPECT_EQ(attempt.feature_id, 42U);
  ASSERT_EQ(attempt.ordered_observations.size(), 3U);
  EXPECT_EQ(attempt.ordered_observations[0].camera_id, 0U);
  EXPECT_EQ(attempt.ordered_observations[0].baseline_observation_index, 0U);
  EXPECT_DOUBLE_EQ(attempt.ordered_observations[1].timestamp, 2.0);
  EXPECT_DOUBLE_EQ(attempt.ordered_observations[1].uv[0], 12.0);
  EXPECT_EQ(attempt.ordered_observations[2].camera_id, 5U);
  EXPECT_EQ(attempt.ordered_observations[2].baseline_observation_index, 2U);
}

TEST(TurnSafeT0, AcuteBoundaryIsStrictAndWorstAxisUsesLambdaMin) {
  Eigen::Matrix2d covariance;
  covariance << 4.0, 0.0, 0.0, 9.0;
  const auto equal =
      ov_msckf::TurnSafeDiagnostics::EvaluateAcuteCertificate(
          10.0, 10.0, covariance);
  EXPECT_EQ(equal.status, "TRANSLATION_NOT_ACUTE");
  EXPECT_FALSE(equal.rho_available);

  const auto acute =
      ov_msckf::TurnSafeDiagnostics::EvaluateAcuteCertificate(
          5.0, 10.0, covariance);
  ASSERT_TRUE(acute.rho_available);
  EXPECT_DOUBLE_EQ(acute.lambda_min, 4.0);
  EXPECT_DOUBLE_EQ(acute.lambda_max, 9.0);
  EXPECT_NEAR(acute.rho_trans, std::asin(0.5) / 2.0, 1.0e-15);
}

TEST(TurnSafeT0, SmallGroupsStayShadowOnlyAndThresholdSelectionIsUnavailable) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  sink->BeginCallback(3.0);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 3.0;
  for (std::uint64_t feature_id = 0U; feature_id < 3U; ++feature_id) {
    ov_msckf::TurnSafeFullTrackAttempt attempt;
    attempt.feature_id = feature_id;
    attempt.detached_index = feature_id;
    attempt.full_outcome = ov_msckf::TurnSafeFullOutcome::kInitTooFar;
    attempt.full_outcome_mapping = ov_msckf::TurnSafeOutcomeMapping::kExact;
    for (double timestamp : {1.0, 2.0}) {
      ov_msckf::TurnSafeObservationValue observation;
      observation.camera_id = 0U;
      observation.timestamp = timestamp;
      observation.clone_available = true;
      attempt.ordered_observations.push_back(observation);
    }
    update.attempts.push_back(attempt);
  }
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_NE(bytes.find("\"raw_candidate_count\":3"), std::string::npos);
  EXPECT_NE(bytes.find("SHADOW_NOT_COMPUTED_THRESHOLD_SET_NOT_FROZEN"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"no_runner_up_gate_shopping\":true"),
            std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, ClonePairPopulationPrecedesTypedOutcomeEligibility) {
  std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts;
  attempts.push_back(PairAttempt(
      10U, 0U, 1.0, 2.0,
      ov_msckf::TurnSafeFullOutcome::kFullAccepted));
  attempts.push_back(PairAttempt(
      11U, 1U, 1.0, 2.0,
      ov_msckf::TurnSafeFullOutcome::kFullNisRejected));
  attempts.push_back(PairAttempt(
      12U, 2U, 1.0, 2.0,
      ov_msckf::TurnSafeFullOutcome::kInitTooFar));
  for (auto &attempt : attempts) {
    for (auto &observation : attempt.ordered_observations)
      observation.clone_available = false;
  }
  ov_msckf::TurnSafePriorPrimitives primitives;
  const auto prior = MinimalPrior();
  ov_msckf::TurnSafeDiagnostics::CapturePriorPrimitives(
      prior, attempts, primitives);
  ASSERT_TRUE(primitives.available);
  ASSERT_EQ(primitives.pair_covariances.size(), 1U);
  for (const auto &attempt : attempts) {
    EXPECT_TRUE(attempt.attempt_has_any_live_same_camera_clone_pair);
    EXPECT_EQ(attempt.valid_clone_pair_count, 1U);
  }

  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  sink->BeginCallback(2.0);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 2.0;
  update.attempts = attempts;
  update.prior = primitives;
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_NE(bytes.find("\"terminal_attempt_count\":3"), std::string::npos);
  EXPECT_NE(bytes.find("\"attempt_valid_clone_pair_count_total\":3"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"shadow_pair_candidates\":3"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"source_observation_key\""), std::string::npos);
  EXPECT_NE(bytes.find("\"target_observation_key\""), std::string::npos);
  EXPECT_NE(bytes.find(
                "\"supporting_target_time_stereo_observation_key\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"raw_pixel\":[20,20]"), std::string::npos);
  EXPECT_NE(bytes.find("\"intrinsic_id\":1"), std::string::npos);
  EXPECT_NE(bytes.find("\"candidate_pairs_with_target_stereo\":3"),
            std::string::npos);
  EXPECT_NE(bytes.find(
                "\"attempts_with_at_least_one_target_stereo_pair\":3"),
            std::string::npos);
  EXPECT_NE(bytes.find(
                "\"groups_with_at_least_one_target_stereo_pair\":1"),
            std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, NonfiniteStereoPixelsHaveExactCandidateScopedReasons) {
  struct Case {
    std::size_t observation_index;
    double value;
    const char *reason;
  };
  const std::array<Case, 3U> cases{{
      {0U, std::numeric_limits<double>::quiet_NaN(),
       "SOURCE_RAW_PIXEL_NONFINITE"},
      {1U, std::numeric_limits<double>::infinity(),
       "TARGET_RAW_PIXEL_NONFINITE"},
      {2U, -std::numeric_limits<double>::infinity(),
       "TARGET_STEREO_RAW_PIXEL_NONFINITE"},
  }};
  for (const auto &test_case : cases) {
    SCOPED_TRACE(test_case.reason);
    auto attempt = PairAttempt(20U, 0U);
    attempt.ordered_observations[test_case.observation_index].uv[0] =
        test_case.value;
    const std::string bytes = SerializeAttempts({attempt});
    EXPECT_NE(bytes.find(
                  "\"raw_pixel\":{\"status\":\"NONFINITE\",\"reason\":\"RAW_PIXEL_NONFINITE\"}"),
              std::string::npos);
    EXPECT_NE(bytes.find(std::string("\"target_stereo_rejection_reason\":\"") +
                         test_case.reason + "\""),
              std::string::npos);
    EXPECT_NE(bytes.find(
                  std::string("\"stereo_geometry_primitives\":{\"status\":\"NONFINITE\",\"reason\":\"") +
                  test_case.reason + "\"}"),
              std::string::npos);
    EXPECT_NE(bytes.find("\"target_time_stereo_available\":true"),
              std::string::npos);
  }
}

TEST(TurnSafeT0, GroupCardinalityBoundariesAndMixedGroupsAreExact) {
  std::vector<ov_msckf::TurnSafeFullTrackAttempt> attempts;
  std::uint64_t feature_id = 1U;
  std::uint64_t detached_index = 0U;
  for (std::size_t cardinality = 1U; cardinality <= 4U; ++cardinality) {
    const double source = static_cast<double>(cardinality * 10U);
    const double target = source + 1.0;
    for (std::size_t member = 0U; member < cardinality; ++member) {
      attempts.push_back(PairAttempt(feature_id++, detached_index++, source,
                                     target));
    }
  }
  const std::string bytes = SerializeAttempts(std::move(attempts));
  EXPECT_EQ(CountSubstring(bytes, "\"group_size_status\":\"INSUFFICIENT_FOR_PAIR_GROUP\""), 1U);
  EXPECT_EQ(CountSubstring(bytes, "\"shadow_only_small_group\":true"), 2U);
  EXPECT_EQ(CountSubstring(bytes, "\"group_size_status\":\"PRODUCTION_MINIMUM_MET\""), 1U);
  EXPECT_NE(bytes.find("\"groups_n_lt_2\":1"), std::string::npos);
  EXPECT_NE(bytes.find("\"groups_n_2\":1"), std::string::npos);
  EXPECT_NE(bytes.find("\"groups_n_3\":1"), std::string::npos);
  EXPECT_NE(bytes.find("\"groups_n_ge_4\":1"), std::string::npos);
  EXPECT_NE(bytes.find("\"candidate_pairs_with_target_stereo\":10"),
            std::string::npos);
  EXPECT_NE(bytes.find(
                "\"groups_with_at_least_one_target_stereo_pair\":4"),
            std::string::npos);
}

TEST(TurnSafeT0, CandidateAndGroupBytesIgnoreAttemptPermutation) {
  std::vector<ov_msckf::TurnSafeFullTrackAttempt> forward;
  forward.push_back(PairAttempt(40U, 9U, 2.0, 4.0));
  forward.push_back(PairAttempt(10U, 3U, 1.0, 4.0));
  forward.push_back(PairAttempt(30U, 7U, 2.0, 4.0));
  forward.push_back(PairAttempt(20U, 5U, 1.0, 4.0));
  const std::string canonical = SerializeAttempts(forward);
  std::array<std::size_t, 4U> order{{0U, 1U, 2U, 3U}};
  std::size_t permutation_count = 0U;
  do {
    std::vector<ov_msckf::TurnSafeFullTrackAttempt> permutation;
    permutation.reserve(order.size());
    for (const std::size_t index : order) permutation.push_back(forward[index]);
    EXPECT_EQ(canonical, SerializeAttempts(std::move(permutation)));
    ++permutation_count;
  } while (std::next_permutation(order.begin(), order.end()));
  EXPECT_EQ(permutation_count, 24U);
}

TEST(TurnSafeT0, DuplicateDetachedAttemptIdentityFailsClosed) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  sink->BeginCallback(10.0);
  ov_msckf::TurnSafeUpdateRecord update;
  update.callback_timestamp = 10.0;
  update.attempts.push_back(PairAttempt(40U, 9U, 2.0, 4.0));
  update.attempts.push_back(PairAttempt(40U, 9U, 2.0, 4.0));
  sink->RecordUpdate(std::move(update));
  sink->EndCallback();
  EXPECT_FALSE(sink->active());
  EXPECT_FALSE(sink->Finalize());
  EXPECT_FALSE(std::ifstream(path).good());
}

TEST(TurnSafeT0, RequiredNativeStageDiagnosticsRemainDistinct) {
  auto attempt = PairAttempt(50U, 0U);
  attempt.triangulation.attempted = true;
  attempt.triangulation.predicate_too_far = true;
  attempt.triangulation.refinement_lambda = 0.25;
  attempt.triangulation.refinement_control_epsilon = 0.125;
  attempt.triangulation.termination_reason = "NOT_APPLICABLE";
  attempt.refinement.attempted = true;
  attempt.refinement.native_function = "single_gaussnewton";
  attempt.refinement.native_success = false;
  attempt.refinement.refinement_runs = 4;
  attempt.refinement.refinement_lambda = 0.5;
  attempt.refinement.refinement_last_step_norm_available = true;
  attempt.refinement.refinement_last_step_norm = 0.0625;
  attempt.refinement.refinement_control_epsilon = 0.03125;
  attempt.refinement.termination_reason = "MAXIMUM_RUNS";
  attempt.schur.attempted = true;
  attempt.schur.native_status = "rank_deficient";
  attempt.schur.native_stage = "numerical_rank";
  attempt.schur.raw_rows_available = true;
  attempt.schur.raw_rows = 9;
  attempt.schur.degrees_of_freedom_available = true;
  attempt.schur.degrees_of_freedom = 6;
  attempt.schur.singular_values_available = true;
  attempt.schur.singular_values = {{9.0, 3.0, 0.0}};
  attempt.schur.singular_ratio_available = true;
  attempt.schur.singular_ratio = 0.0;
  attempt.schur.numerical_rank_available = true;
  attempt.schur.numerical_rank = 2;
  attempt.schur.condition_number_available = false;
  attempt.full_nis.attempted = true;
  attempt.full_nis.native_stage = "decision";
  attempt.full_nis.degrees_of_freedom_available = true;
  attempt.full_nis.degrees_of_freedom = 6;
  attempt.full_nis.statistic_available = true;
  attempt.full_nis.statistic = 12.0;
  attempt.full_nis.threshold_available = true;
  attempt.full_nis.threshold = 11.0;
  attempt.full_nis.decision = "REJECTED";
  attempt.accepted_at_native_feature_gate = false;
  attempt.native_feature_row_count_available = true;
  attempt.native_feature_row_count = 6U;
  attempt.accepted_full_factor = false;
  attempt.accepted_full_row_count = 0U;
  const std::string bytes = SerializeAttempts({attempt});
  EXPECT_NE(bytes.find("\"refinement_last_step_norm\":{\"status\":\"AVAILABLE\",\"value\":0.0625"), std::string::npos);
  EXPECT_NE(bytes.find("\"refinement_lambda\":{\"status\":\"AVAILABLE\",\"value\":0.5"), std::string::npos);
  EXPECT_NE(bytes.find("\"refinement_control_epsilon\":{\"status\":\"AVAILABLE\",\"value\":0.03125"), std::string::npos);
  EXPECT_NE(bytes.find("\"termination_reason\":\"MAXIMUM_RUNS\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"singular_values\":[9,3,0]"),
            std::string::npos);
  EXPECT_NE(bytes.find("\"numerical_rank\":2"), std::string::npos);
  EXPECT_NE(bytes.find("\"condition_number\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"), std::string::npos);
  EXPECT_NE(bytes.find("\"degrees_of_freedom\":6,\"statistic\":{\"status\":\"AVAILABLE\",\"value\":12"), std::string::npos);
  EXPECT_NE(bytes.find("\"decision\":\"REJECTED\""), std::string::npos);
  EXPECT_NE(bytes.find("\"triangulation_outcome_counts_by_code\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"refinement_outcome_counts_by_code\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"schur_outcome_counts_by_code\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"full_nis_outcome_counts_by_code\""),
            std::string::npos);

  auto nonfinite = PairAttempt(51U, 1U);
  nonfinite.refinement.attempted = true;
  nonfinite.refinement.native_function = "single_gaussnewton";
  nonfinite.refinement.depth_available = true;
  nonfinite.refinement.depth = std::numeric_limits<double>::infinity();
  nonfinite.refinement.refinement_last_step_norm_available = true;
  nonfinite.refinement.refinement_last_step_norm =
      std::numeric_limits<double>::quiet_NaN();
  nonfinite.schur.attempted = true;
  nonfinite.schur.degrees_of_freedom = 99;
  const std::string nonfinite_bytes = SerializeAttempts({nonfinite});
  EXPECT_NE(nonfinite_bytes.find(
                "\"depth\":{\"status\":\"NONFINITE\",\"reason\":\"NATIVE_VALUE_INFINITE\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "\"refinement_last_step_norm\":{\"status\":\"NONFINITE\",\"reason\":\"NATIVE_VALUE_NAN\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "\"degrees_of_freedom\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "\"raw_rows\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "\"native_feature_row_count\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "\"full_nis\":{\"attempted\":false,\"native_stage\":\"NOT_EXPOSED_BY_NATIVE_PATH\",\"lifecycle_accept\":false,\"degrees_of_freedom\":{\"status\":\"NOT_EXPOSED\",\"reason\":\"NOT_EXPOSED_BY_NATIVE_PATH\"}"),
            std::string::npos);
  EXPECT_NE(nonfinite_bytes.find(
                "SOURCE_CAMERA_CALIBRATION_NOT_EXPOSED_BY_NATIVE_PATH"),
            std::string::npos);
}

TEST(TurnSafeT0, NoFullVisualUpdateDurationUsesOnlyCameraTime) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  const auto callback = [&sink](double timestamp, bool accepted) {
    sink->BeginCallback(timestamp);
    ov_msckf::TurnSafeUpdateRecord update;
    update.callback_timestamp = timestamp;
    update.baseline_commit_occurred = accepted;
    if (accepted) update.baseline_accepted_ids.push_back(7U);
    sink->RecordUpdate(std::move(update));
    sink->EndCallback();
  };
  callback(10.0, false);
  callback(12.0, true);
  callback(15.0, false);
  callback(11.0, false);
  callback(16.0, false);
  callback(20.0, true);
  callback(21.0, false);
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_EQ(CountSubstring(bytes, "\"reason\":\"NO_PRIOR_ACCEPTED_FULL_UPDATE\""), 1U);
  EXPECT_EQ(CountSubstring(bytes, "\"reason\":\"CAMERA_TIMESTAMP_REGRESSION\""), 1U);
  EXPECT_EQ(CountSubstring(bytes, "\"no_full_visual_update_duration\":{\"status\":\"AVAILABLE\",\"value\":0,\"reason\":\"NONE\"}"), 2U);
  EXPECT_NE(bytes.find("\"no_full_visual_update_duration\":{\"status\":\"AVAILABLE\",\"value\":3,\"reason\":\"NONE\"}"), std::string::npos);
  EXPECT_NE(bytes.find("\"no_full_visual_update_duration\":{\"status\":\"AVAILABLE\",\"value\":4,\"reason\":\"NONE\"}"), std::string::npos);
  EXPECT_NE(bytes.find("\"no_full_visual_update_duration\":{\"status\":\"AVAILABLE\",\"value\":1,\"reason\":\"NONE\"}"), std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, UpdaterTimestampMismatchDoesNotAdvanceDurationClock) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  const auto callback = [&sink](double envelope_timestamp,
                                double updater_timestamp, bool accepted) {
    sink->BeginCallback(envelope_timestamp);
    ov_msckf::TurnSafeUpdateRecord update;
    update.callback_timestamp = updater_timestamp;
    update.baseline_commit_occurred = accepted;
    if (accepted) update.baseline_accepted_ids.push_back(7U);
    sink->RecordUpdate(std::move(update));
    sink->EndCallback();
  };
  callback(10.0, 10.0, true);
  callback(12.0, 13.0, false);
  callback(12.0, 12.0, false);
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_EQ(CountSubstring(
                bytes,
                "\"reason\":\"UPDATER_CALLBACK_TIMESTAMP_MISMATCH\""),
            1U);
  EXPECT_NE(bytes.find("\"no_full_visual_update_duration\":{\"status\":\"AVAILABLE\",\"value\":2,\"reason\":\"NONE\"}"), std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, RuntimeSupportIsDerivedWithDeterministicReasons) {
  ov_msckf::TurnSafeResolvedConfiguration supported;
  supported.one_pass_schur = true;
  supported.fej_enabled = true;
  supported.global_3d_transient = true;
  supported.all_cameras_radtan = true;
  supported.camera_extrinsic_calibration_off = true;
  supported.camera_intrinsic_calibration_off = true;
  supported.camera_time_offset_calibration_off = true;
  supported.stereo_enabled = true;
  supported.stereo_available = true;
  supported.require_target_stereo_range = true;
  supported.camera_count = 2U;
  EXPECT_TRUE(ov_msckf::TurnSafeDiagnostics::EvaluateConfiguration(supported)
                  .supported);

  struct Toggle {
    bool ov_msckf::TurnSafeResolvedConfiguration::*member;
    const char *reason;
  };
  const std::array<Toggle, 9U> toggles{{
      {&ov_msckf::TurnSafeResolvedConfiguration::one_pass_schur,
       "ONE_PASS_SCHUR_REQUIRED"},
      {&ov_msckf::TurnSafeResolvedConfiguration::fej_enabled,
       "FEJ_REQUIRED"},
      {&ov_msckf::TurnSafeResolvedConfiguration::global_3d_transient,
       "GLOBAL_3D_REQUIRED"},
      {&ov_msckf::TurnSafeResolvedConfiguration::all_cameras_radtan,
       "CAMRADTAN_REQUIRED"},
      {&ov_msckf::TurnSafeResolvedConfiguration::camera_extrinsic_calibration_off,
       "CAMERA_EXTRINSIC_CALIBRATION_MUST_BE_OFF"},
      {&ov_msckf::TurnSafeResolvedConfiguration::camera_intrinsic_calibration_off,
       "CAMERA_INTRINSIC_CALIBRATION_MUST_BE_OFF"},
      {&ov_msckf::TurnSafeResolvedConfiguration::camera_time_offset_calibration_off,
       "CAMERA_TIME_OFFSET_CALIBRATION_MUST_BE_OFF"},
      {&ov_msckf::TurnSafeResolvedConfiguration::stereo_enabled,
       "STEREO_MUST_BE_ENABLED"},
      {&ov_msckf::TurnSafeResolvedConfiguration::require_target_stereo_range,
       "TARGET_STEREO_RANGE_REQUIREMENT_MUST_BE_CONFIGURED"},
  }};
  for (const auto &toggle : toggles) {
    auto configuration = supported;
    configuration.*(toggle.member) = false;
    const auto evaluated =
        ov_msckf::TurnSafeDiagnostics::EvaluateConfiguration(configuration);
    EXPECT_FALSE(evaluated.supported);
    EXPECT_NE(std::find(evaluated.unsupported_reasons.begin(),
                        evaluated.unsupported_reasons.end(), toggle.reason),
              evaluated.unsupported_reasons.end());
    EXPECT_TRUE(std::is_sorted(evaluated.unsupported_reasons.begin(),
                               evaluated.unsupported_reasons.end()));
  }
  auto missing_stereo = supported;
  missing_stereo.stereo_available = false;
  missing_stereo.camera_count = 1U;
  const auto evaluated =
      ov_msckf::TurnSafeDiagnostics::EvaluateConfiguration(missing_stereo);
  EXPECT_EQ(evaluated.unsupported_reasons,
            std::vector<std::string>{
                "EXACT_TWO_CAMERA_STEREO_MUST_BE_AVAILABLE"});

  const std::string path = TemporaryPath();
  auto options = Options(path);
  options.resolved_configuration = evaluated;
  options.resolved_configuration.supported = true;
  options.resolved_configuration.unsupported_reasons.clear();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
  ASSERT_TRUE(sink && sink->active());
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_NE(bytes.find("\"supported_configuration\":false"),
            std::string::npos);
  EXPECT_NE(bytes.find("EXACT_TWO_CAMERA_STEREO_MUST_BE_AVAILABLE"),
            std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, CallerSourceClaimsCannotReplaceEmbeddedBuildIdentity) {
  const std::string conflict_path = TemporaryPath();
  auto conflict = Options(conflict_path);
  conflict.expected_source_sha = std::string(40U, '0');
  const auto conflict_sink = ov_msckf::TurnSafeDiagnostics::Create(conflict);
  ASSERT_TRUE(conflict_sink);
  EXPECT_FALSE(conflict_sink->active());
  EXPECT_STREQ(conflict_sink->failure_reason(), "PROVENANCE_CONFLICT");

  const std::string path = TemporaryPath();
  auto ignored_claim = Options(path);
  ignored_claim.source_sha = "CALLER_CONTROLLED_SOURCE";
  ignored_claim.source_tree = "CALLER_CONTROLLED_TREE";
  ignored_claim.source_snapshot_sha256 = "CALLER_CONTROLLED_SNAPSHOT";
  ignored_claim.build_provenance_id = "CALLER_CONTROLLED_BUILD";
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(ignored_claim);
  ASSERT_TRUE(sink && sink->active());
  ASSERT_TRUE(sink->Finalize());
  const std::string bytes = ReadAll(path);
  EXPECT_EQ(bytes.find("CALLER_CONTROLLED"), std::string::npos);
  EXPECT_NE(bytes.find("\"source_dirty\":"), std::string::npos);
  EXPECT_NE(bytes.find("\"source_snapshot_sha256\":\""),
            std::string::npos);
  EXPECT_NE(bytes.find("\"build_provenance_id\":\""),
            std::string::npos);
  ::unlink(path.c_str());
}

TEST(TurnSafeT0, FrontendFaultStagesCannotChangeNativeMatchingResults) {
  FaultReset reset;
  cv::Mat image(100, 100, CV_8UC1);
  for (int row = 0; row < image.rows; ++row) {
    for (int col = 0; col < image.cols; ++col) {
      image.at<std::uint8_t>(row, col) =
          static_cast<std::uint8_t>((row * 17 + col * 31) % 251);
    }
  }
  std::vector<cv::Mat> pyramid;
  cv::buildOpticalFlowPyramid(image, pyramid, cv::Size(15, 15), 5);
  std::vector<cv::KeyPoint> baseline_source;
  for (int y = 15; y <= 75; y += 15) {
    for (int x = 15; x <= 75; x += 15) {
      baseline_source.emplace_back(static_cast<float>(x),
                                   static_cast<float>(y), 1.0F);
    }
  }
  std::vector<cv::KeyPoint> baseline_target = baseline_source;
  std::vector<uchar> baseline_mask;
  TrackKLTTestPeer baseline_tracker;
  baseline_tracker.Match(pyramid, baseline_source, baseline_target,
                         baseline_mask, nullptr);
  ASSERT_EQ(baseline_mask.size(), 25U);
  std::vector<std::size_t> native_track_ids(baseline_mask.size());
  for (std::size_t index = 0U; index < native_track_ids.size(); ++index) {
    native_track_ids[index] = 1000U + index;
  }
  const auto accepted_from_mask = [&native_track_ids](
      const std::vector<uchar> &mask) {
    std::vector<std::size_t> accepted;
    for (std::size_t index = 0U;
         index < mask.size() && index < native_track_ids.size(); ++index) {
      if (mask[index] != 0U) accepted.push_back(native_track_ids[index]);
    }
    return accepted;
  };
  const std::vector<std::size_t> baseline_accepted_ids =
      accepted_from_mask(baseline_mask);

  // Capture-off supplies no diagnostic destination. Even an armed test fault
  // is therefore unreachable, and the native KLT/RANSAC result is exact.
  {
    TrackKLTTestPeer capture_off_tracker;
    auto source = baseline_source;
    auto target = baseline_source;
    std::vector<uchar> mask;
    ov_core::set_track_klt_diagnostic_fault_for_test(
        ov_core::TrackKLTDiagnosticFaultStage::kMatchingCopy,
        ov_core::TrackKLTDiagnosticFaultKind::kBadAlloc);
    capture_off_tracker.Match(pyramid, source, target, mask, nullptr);
    EXPECT_EQ(mask, baseline_mask);
    EXPECT_EQ(capture_off_tracker.turnsafe_diagnostic_failure_reason(),
              ov_core::TrackKLTDiagnosticFailureReason::kNone);
    ov_core::clear_track_klt_diagnostic_fault_for_test();
  }

  const std::array<ov_core::TrackKLTDiagnosticFaultKind, 3U> kinds{{
      ov_core::TrackKLTDiagnosticFaultKind::kBadAlloc,
      ov_core::TrackKLTDiagnosticFaultKind::kStdException,
      ov_core::TrackKLTDiagnosticFaultKind::kUnknown,
  }};
  for (const auto kind : kinds) {
    TrackKLTTestPeer tracker;
    ASSERT_TRUE(tracker.set_turnsafe_diagnostics_callback(
        [](ov_core::TrackKLTFrameDiagnostics) {}));
    ov_core::TrackKLTFrameDiagnostics record;
    ov_core::set_track_klt_diagnostic_fault_for_test(
        ov_core::TrackKLTDiagnosticFaultStage::kActivation, kind);
    EXPECT_FALSE(tracker.Begin(1.0, record));
    auto source = baseline_source;
    auto target = baseline_source;
    std::vector<uchar> mask;
    tracker.Match(pyramid, source, target, mask, nullptr);
    EXPECT_EQ(mask, baseline_mask);
    EXPECT_EQ(accepted_from_mask(mask), baseline_accepted_ids);
    EXPECT_NE(tracker.turnsafe_diagnostic_failure_reason(),
              ov_core::TrackKLTDiagnosticFailureReason::kNone);
    EXPECT_EQ(tracker.turnsafe_diagnostic_failure_count(), 1U);
    ov_core::clear_track_klt_diagnostic_fault_for_test();
  }
  for (const auto kind : kinds) {
    TrackKLTTestPeer tracker;
    ASSERT_TRUE(tracker.set_turnsafe_diagnostics_callback(
        [](ov_core::TrackKLTFrameDiagnostics) {}));
    auto source = baseline_source;
    auto target = baseline_source;
    std::vector<uchar> mask;
    ov_core::TrackKLTMatchingDiagnostics diagnostics;
    ov_core::set_track_klt_diagnostic_fault_for_test(
        ov_core::TrackKLTDiagnosticFaultStage::kMatchingCopy, kind);
    tracker.Match(pyramid, source, target, mask, &diagnostics);
    EXPECT_EQ(mask, baseline_mask);
    EXPECT_EQ(accepted_from_mask(mask), baseline_accepted_ids);
    ASSERT_EQ(target.size(), baseline_target.size());
    for (std::size_t index = 0U; index < target.size(); ++index) {
      EXPECT_FLOAT_EQ(target[index].pt.x, baseline_target[index].pt.x);
      EXPECT_FLOAT_EQ(target[index].pt.y, baseline_target[index].pt.y);
    }
    EXPECT_NE(tracker.turnsafe_diagnostic_failure_reason(),
              ov_core::TrackKLTDiagnosticFailureReason::kNone);
    EXPECT_EQ(tracker.turnsafe_diagnostic_failure_count(), 1U);
    ov_core::clear_track_klt_diagnostic_fault_for_test();
  }

  const std::array<ov_core::TrackKLTDiagnosticFaultStage, 4U> finish_stages{{
      ov_core::TrackKLTDiagnosticFaultStage::kSummary,
      ov_core::TrackKLTDiagnosticFaultStage::kAcceptedIdCopy,
      ov_core::TrackKLTDiagnosticFaultStage::kFrameAssembly,
      ov_core::TrackKLTDiagnosticFaultStage::kCallback,
  }};
  for (const auto stage : finish_stages) {
    for (const auto kind : kinds) {
      TrackKLTTestPeer tracker;
      std::size_t callback_count = 0U;
      ASSERT_TRUE(tracker.set_turnsafe_diagnostics_callback(
          [&callback_count](ov_core::TrackKLTFrameDiagnostics) {
            ++callback_count;
          }));
      ov_core::TrackKLTFrameDiagnostics record;
      ASSERT_TRUE(tracker.Begin(1.0, record));
      ov_core::TrackKLTMatchingDiagnostics matching;
      auto source = baseline_source;
      auto target = baseline_source;
      std::vector<uchar> native_output_mask;
      tracker.Match(pyramid, source, target, native_output_mask, &matching);
      ASSERT_EQ(native_output_mask, baseline_mask);
      cv::Mat native_mask = cv::Mat::zeros(100, 100, CV_8UC1);
      const std::vector<std::size_t> accepted_ids =
          accepted_from_mask(native_output_mask);
      ov_core::set_track_klt_diagnostic_fault_for_test(stage, kind);
      tracker.Finish(std::move(record), matching, target, native_mask,
                     accepted_ids);
      EXPECT_EQ(callback_count, 0U);
      EXPECT_EQ(accepted_ids, baseline_accepted_ids);
      EXPECT_EQ(native_output_mask, baseline_mask);
      EXPECT_NE(tracker.turnsafe_diagnostic_failure_reason(),
                ov_core::TrackKLTDiagnosticFailureReason::kNone);
      ov_core::clear_track_klt_diagnostic_fault_for_test();
    }
  }
}

TEST(TurnSafeT0,
     CallbackInstallationFaultsRollbackAndPreserveNativeTracking) {
  FaultReset reset;
  cv::Mat image(100, 100, CV_8UC1);
  for (int row = 0; row < image.rows; ++row) {
    for (int col = 0; col < image.cols; ++col) {
      image.at<std::uint8_t>(row, col) =
          static_cast<std::uint8_t>((row * 17 + col * 31) % 251);
    }
  }
  std::vector<cv::Mat> pyramid;
  cv::buildOpticalFlowPyramid(image, pyramid, cv::Size(15, 15), 5);
  std::vector<cv::KeyPoint> source;
  for (int y = 15; y <= 75; y += 15) {
    for (int x = 15; x <= 75; x += 15) {
      source.emplace_back(static_cast<float>(x), static_cast<float>(y), 1.0F);
    }
  }
  auto baseline_source = source;
  auto baseline_target = source;
  std::vector<uchar> baseline_mask;
  TrackKLTTestPeer baseline_tracker;
  baseline_tracker.Match(pyramid, baseline_source, baseline_target,
                         baseline_mask, nullptr);

  const std::array<ov_msckf::TurnSafeDiagnosticFaultStage, 2U> stages{{
      ov_msckf::TurnSafeDiagnosticFaultStage::kTrackerCallbackInstallation,
      ov_msckf::TurnSafeDiagnosticFaultStage::kUpdaterCallbackInstallation,
  }};
  const std::array<ov_msckf::TurnSafeDiagnosticFaultKind, 3U> kinds{{
      ov_msckf::TurnSafeDiagnosticFaultKind::kBadAlloc,
      ov_msckf::TurnSafeDiagnosticFaultKind::kStdException,
      ov_msckf::TurnSafeDiagnosticFaultKind::kUnknown,
  }};
  for (const auto stage : stages) {
    for (const auto kind : kinds) {
      SCOPED_TRACE(static_cast<int>(stage));
      SCOPED_TRACE(static_cast<int>(kind));
      const std::string path = TemporaryPath();
      auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
      ASSERT_TRUE(sink && sink->active());
      TrackKLTTestPeer tracker;
      ov_msckf::UpdaterOptions updater_options;
      ov_core::FeatureInitializerOptions initializer_options;
      ov_msckf::UpdaterMSCKF updater(updater_options, initializer_options);

      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      ov_msckf::TurnSafeCallbackInstallationResult installation;
      EXPECT_NO_THROW(installation = ov_msckf::install_turnsafe_t0_callbacks(
                          sink, &tracker, &updater));
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();

      EXPECT_FALSE(installation.success);
      EXPECT_FALSE(installation.tracker_callback_retained);
      EXPECT_FALSE(installation.updater_callback_retained);
      EXPECT_TRUE(installation.tracker_rollback_succeeded);
      EXPECT_TRUE(installation.updater_rollback_succeeded);
      EXPECT_EQ(installation.failure_reason,
                stage == ov_msckf::TurnSafeDiagnosticFaultStage::
                             kTrackerCallbackInstallation
                    ? ov_msckf::TurnSafeCaptureDisableReason::
                          kFrontendCaptureFailure
                    : ov_msckf::TurnSafeCaptureDisableReason::
                          kUpdaterCaptureFailure);
      EXPECT_FALSE(sink->active());
      EXPECT_EQ(sink->failure_count(), 1U);

      ov_core::TrackKLTFrameDiagnostics record;
      EXPECT_FALSE(tracker.Begin(1.0, record));
      auto native_source = source;
      auto native_target = source;
      std::vector<uchar> native_mask;
      tracker.Match(pyramid, native_source, native_target, native_mask,
                    nullptr);
      EXPECT_EQ(native_mask, baseline_mask);
      EXPECT_FALSE(sink->Finalize());
      ::unlink(path.c_str());
      ::unlink((path + ".tmp").c_str());
    }
  }
}

TEST(TurnSafeT0,
     EpochFrontendRebindInstallsOnlyTheReplacementTrackerObserver) {
  FaultReset reset;
  const std::string path = TemporaryPath();
  auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  TrackKLTTestPeer old_tracker;
  TrackKLTTestPeer replacement_tracker;
  ov_msckf::UpdaterOptions updater_options;
  ov_core::FeatureInitializerOptions initializer_options;
  ov_msckf::UpdaterMSCKF updater(updater_options, initializer_options);

  const ov_msckf::TurnSafeCallbackInstallationResult initial =
      ov_msckf::install_turnsafe_t0_callbacks(sink, &old_tracker, &updater);
  ASSERT_TRUE(initial.success);
  ov_core::TrackKLTFrameDiagnostics record;
  EXPECT_TRUE(old_tracker.Begin(1.0, record));
  EXPECT_FALSE(replacement_tracker.Begin(1.0, record));

  EXPECT_TRUE(ov_msckf::reinstall_turnsafe_t0_frontend_callback(
      sink, &replacement_tracker));
  EXPECT_TRUE(sink->active());
  EXPECT_TRUE(replacement_tracker.Begin(2.0, record));
  EXPECT_TRUE(sink->Finalize());
  ::unlink(path.c_str());
  ::unlink((path + ".tmp").c_str());
}

TEST(TurnSafeT0,
     EpochFrontendRebindFailureDisablesOnlyPassiveDiagnostics) {
  FaultReset reset;
  const std::string path = TemporaryPath();
  auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  TrackKLTTestPeer replacement_tracker;
  ov_msckf::set_turnsafe_diagnostic_fault_for_test(
      ov_msckf::TurnSafeDiagnosticFaultStage::kTrackerCallbackInstallation,
      ov_msckf::TurnSafeDiagnosticFaultKind::kBadAlloc);
  EXPECT_FALSE(ov_msckf::reinstall_turnsafe_t0_frontend_callback(
      sink, &replacement_tracker));
  ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
  EXPECT_FALSE(sink->active());
  EXPECT_EQ(sink->failure_count(), 1U);
  ov_core::TrackKLTFrameDiagnostics record;
  EXPECT_FALSE(replacement_tracker.Begin(1.0, record));
  EXPECT_FALSE(sink->Finalize());
  ::unlink(path.c_str());
  ::unlink((path + ".tmp").c_str());
}

TEST(TurnSafeT0, SinkFaultInjectionIsContainedAtEveryStage) {
  FaultReset reset;
  const NativeKltOracle native_oracle = CaptureNativeKltOracle();
  const std::array<ov_msckf::TurnSafeDiagnosticFaultStage, 5U> creation_stages{{
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkOpen,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkFdopen,
      ov_msckf::TurnSafeDiagnosticFaultStage::kHeaderSerialization,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkWrite,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkRecordFlush,
  }};
  const std::array<ov_msckf::TurnSafeDiagnosticFaultKind, 3U> kinds{{
      ov_msckf::TurnSafeDiagnosticFaultKind::kBadAlloc,
      ov_msckf::TurnSafeDiagnosticFaultKind::kStdException,
      ov_msckf::TurnSafeDiagnosticFaultKind::kUnknown,
  }};
  for (const auto stage : creation_stages) {
    for (const auto kind : kinds) {
      const std::string path = TemporaryPath();
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
      ASSERT_TRUE(sink);
      EXPECT_FALSE(sink->active());
      EXPECT_NE(std::string(sink->failure_reason()), "NONE");
      EXPECT_GE(sink->failure_count(), 1U);
      EXPECT_FALSE(sink->Finalize());
      EXPECT_FALSE(std::ifstream(path).good());
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
    }
  }

  const std::array<ov_msckf::TurnSafeDiagnosticFaultStage, 4U> record_stages{{
      ov_msckf::TurnSafeDiagnosticFaultStage::kFrontendCopy,
      ov_msckf::TurnSafeDiagnosticFaultStage::kUpdateCopy,
      ov_msckf::TurnSafeDiagnosticFaultStage::kCandidateGrouping,
      ov_msckf::TurnSafeDiagnosticFaultStage::kCallbackSerialization,
  }};
  for (const auto stage : record_stages) {
    for (const auto kind : kinds) {
      const std::string path = TemporaryPath();
      const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
      ASSERT_TRUE(sink && sink->active());
      sink->BeginCallback(1.0);
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      if (stage == ov_msckf::TurnSafeDiagnosticFaultStage::kFrontendCopy) {
        ov_core::TrackKLTFrameDiagnostics frontend;
        sink->RecordFrontend(std::move(frontend));
      } else if (stage ==
                 ov_msckf::TurnSafeDiagnosticFaultStage::kUpdateCopy) {
        ov_msckf::TurnSafeUpdateRecord update;
        sink->RecordUpdate(std::move(update));
      } else {
        ov_msckf::TurnSafeUpdateRecord update;
        update.attempts.push_back(PairAttempt(1U, 0U));
        sink->RecordUpdate(std::move(update));
        sink->EndCallback();
      }
      EXPECT_FALSE(sink->active());
      EXPECT_GE(sink->failure_count(), 1U);
      EXPECT_FALSE(sink->Finalize());
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
    }
  }

  for (const auto stage : {
           ov_msckf::TurnSafeDiagnosticFaultStage::kAttemptCopy,
           ov_msckf::TurnSafeDiagnosticFaultStage::kPriorCopy}) {
    for (const auto kind : kinds) {
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      if (kind == ov_msckf::TurnSafeDiagnosticFaultKind::kBadAlloc) {
        EXPECT_THROW(
        {
          if (stage ==
              ov_msckf::TurnSafeDiagnosticFaultStage::kAttemptCopy) {
            ov_core::Feature feature;
            (void)ov_msckf::TurnSafeDiagnostics::CaptureAttempt(feature, 0U);
          } else {
            auto attempts =
                std::vector<ov_msckf::TurnSafeFullTrackAttempt>{
                    PairAttempt(1U, 0U)};
            ov_msckf::TurnSafePriorPrimitives primitives;
            ov_msckf::TurnSafeDiagnostics::CapturePriorPrimitives(
                MinimalPrior(), attempts, primitives);
          }
        },
        std::bad_alloc);
      } else {
        EXPECT_ANY_THROW({
          if (stage ==
              ov_msckf::TurnSafeDiagnosticFaultStage::kAttemptCopy) {
            ov_core::Feature feature;
            (void)ov_msckf::TurnSafeDiagnostics::CaptureAttempt(feature, 0U);
          } else {
            auto attempts =
                std::vector<ov_msckf::TurnSafeFullTrackAttempt>{
                    PairAttempt(1U, 0U)};
            ov_msckf::TurnSafePriorPrimitives primitives;
            ov_msckf::TurnSafeDiagnostics::CapturePriorPrimitives(
                MinimalPrior(), attempts, primitives);
          }
        });
      }
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
    }
  }

  for (const auto stage : {
           ov_msckf::TurnSafeDiagnosticFaultStage::kInitializerProjection,
           ov_msckf::TurnSafeDiagnosticFaultStage::kSchurProjection,
           ov_msckf::TurnSafeDiagnosticFaultStage::kNisProjection,
           ov_msckf::TurnSafeDiagnosticFaultStage::kUpdaterPublication}) {
    for (const auto kind : kinds) {
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      EXPECT_ANY_THROW(
          ov_msckf::inject_turnsafe_diagnostic_fault_for_test(stage));
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
    }
  }

  const std::array<ov_msckf::TurnSafeDiagnosticFaultStage, 4U> final_stages{{
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkFinalFlush,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkClose,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkRename,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSinkDirectoryFsync,
  }};
  for (const auto stage : final_stages) {
    for (const auto kind : kinds) {
      const std::string final_path = TemporaryPath();
      const auto final_sink =
          ov_msckf::TurnSafeDiagnostics::Create(Options(final_path));
      ASSERT_TRUE(final_sink && final_sink->active());
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      EXPECT_FALSE(final_sink->Finalize());
      EXPECT_FALSE(std::ifstream(final_path).good());
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
    }
  }
}

TEST(TurnSafeT0, EventExtensionFaultsDisableOnlyDiagnostics) {
  FaultReset reset;
  const NativeKltOracle native_oracle = CaptureNativeKltOracle();
  const std::array<ov_msckf::TurnSafeDiagnosticFaultKind, 3U> kinds{{
      ov_msckf::TurnSafeDiagnosticFaultKind::kBadAlloc,
      ov_msckf::TurnSafeDiagnosticFaultKind::kStdException,
      ov_msckf::TurnSafeDiagnosticFaultKind::kUnknown,
  }};
  const std::array<ov_msckf::TurnSafeDiagnosticFaultStage, 4U> interval_stages{{
      ov_msckf::TurnSafeDiagnosticFaultStage::kImuSupportCopy,
      ov_msckf::TurnSafeDiagnosticFaultStage::kEndpointInterpolation,
      ov_msckf::TurnSafeDiagnosticFaultStage::kGyroIntegration,
      ov_msckf::TurnSafeDiagnosticFaultStage::kSo3Composition,
  }};
  for (const auto stage : interval_stages) {
    for (const auto kind : kinds) {
      SCOPED_TRACE(static_cast<int>(stage));
      SCOPED_TRACE(static_cast<int>(kind));
      const std::string path = TemporaryPath();
      auto options = Options(path);
      options.capture_causal_imu_intervals = true;
      const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
      ASSERT_TRUE(sink && sink->active());
      ov_msckf::StateOptions state_options;
      ov_msckf::State state(state_options);
      Eigen::VectorXd offset(1);
      offset << 0.0;
      state._calib_dt_CAMtoIMU->set_value(offset);
      ov_msckf::Propagator propagator(ov_msckf::NoiseManager(), 9.81);
      for (int index = 0; index <= 3; ++index) {
        ov_core::ImuData sample;
        sample.timestamp = static_cast<double>(index);
        sample.wm = Eigen::Vector3d(1.0, 0.0, 0.0);
        sample.am = Eigen::Vector3d::Zero();
        propagator.feed_imu(sample);
      }
      sink->BeginCallback(1.0, {0, 1}, false, false, &state, &propagator);
      sink->EndCallback(false, false, &state);
      ASSERT_TRUE(sink->active());
      ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
      sink->BeginCallback(2.0, {0, 1}, false, false, &state, &propagator);
      EXPECT_FALSE(sink->active());
      EXPECT_EQ(sink->failure_count(), 1U);
      EXPECT_FALSE(sink->Finalize());
      EXPECT_FALSE(std::ifstream(path).good());
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
      ::unlink((path + ".tmp").c_str());
    }
  }

  for (const auto stage : {
           ov_msckf::TurnSafeDiagnosticFaultStage::kCallbackMetadata,
           ov_msckf::TurnSafeDiagnosticFaultStage::kGroupProvenance,
           ov_msckf::TurnSafeDiagnosticFaultStage::kExtensionSerialization}) {
    for (const auto kind : kinds) {
      SCOPED_TRACE(static_cast<int>(stage));
      SCOPED_TRACE(static_cast<int>(kind));
      const std::string path = TemporaryPath();
      auto options = Options(path);
      options.capture_outcome_association_keys =
          stage == ov_msckf::TurnSafeDiagnosticFaultStage::kCallbackMetadata;
      options.capture_group_bearing_provenance =
          stage != ov_msckf::TurnSafeDiagnosticFaultStage::kCallbackMetadata;
      const auto sink = ov_msckf::TurnSafeDiagnostics::Create(options);
      ASSERT_TRUE(sink && sink->active());
      if (stage == ov_msckf::TurnSafeDiagnosticFaultStage::kCallbackMetadata) {
        ov_msckf::StateOptions state_options;
        ov_msckf::State state(state_options);
        ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
        sink->BeginCallback(1.0, {0}, false, false, &state, nullptr);
      } else {
        auto attempts = std::vector<ov_msckf::TurnSafeFullTrackAttempt>{
            PairAttempt(1U, 0U), PairAttempt(2U, 1U)};
        ov_msckf::TurnSafePriorPrimitives prior;
        ov_msckf::TurnSafeDiagnostics::CapturePriorPrimitives(
            EventReadyPrior({1.0, 2.0}), attempts, prior);
        sink->BeginCallback(3.0);
        ov_msckf::TurnSafeUpdateRecord update;
        update.callback_timestamp = 3.0;
        update.attempts = std::move(attempts);
        update.prior = std::move(prior);
        sink->RecordUpdate(std::move(update));
        ov_msckf::set_turnsafe_diagnostic_fault_for_test(stage, kind);
        sink->EndCallback();
      }
      EXPECT_FALSE(sink->active());
      EXPECT_EQ(sink->failure_count(), 1U);
      EXPECT_FALSE(sink->Finalize());
      ExpectNativeKltEquals(native_oracle);
      ov_msckf::clear_turnsafe_diagnostic_fault_for_test();
      ::unlink(path.c_str());
      ::unlink((path + ".tmp").c_str());
    }
  }
}

TEST(TurnSafeT0, ComponentFailureDisablesSinkBeforePartialPublication) {
  const std::string path = TemporaryPath();
  const auto sink = ov_msckf::TurnSafeDiagnostics::Create(Options(path));
  ASSERT_TRUE(sink && sink->active());
  sink->BeginCallback(1.0);
  ov_core::TrackKLTFrameDiagnostics frontend;
  sink->RecordFrontend(std::move(frontend));
  sink->ReportComponentFailure(
      ov_msckf::TurnSafeCaptureDisableReason::kUpdaterCaptureFailure);
  sink->EndCallback();
  EXPECT_FALSE(sink->active());
  EXPECT_STREQ(sink->failure_reason(), "UPDATER_CAPTURE_FAILURE");
  EXPECT_FALSE(sink->Finalize());
  EXPECT_FALSE(std::ifstream(path).good());
}
